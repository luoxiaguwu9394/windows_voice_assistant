#!/usr/bin/env python3
"""
Build the WinVoice runtime payload: an xz tarball containing an embedded
Python 3.12 with every dependency preinstalled, the llama.cpp server, the
winvoice source tree and the DSH `.pylibs`. This is what the setup wizard
extracts onto the user's machine.

Run on the DEV machine (needs network + this repo):

    python installer/build_runtime.py            # full build
    python installer/build_runtime.py --reuse-pylibs   # copy the repo's .pylibs instead of reinstalling

Outputs:
    installer/payload/runtime.tar.xz   the payload (~a few hundred MB)

Design notes:
- The embedded interpreter is patched (`python312._pth`) so that, at user
  time, `..` (the install root) is on sys.path — that is why the extracted
  install dir can look exactly like the repo root.
- Dependencies are pinned to the versions in the DEV environment (the ones
  actually tested with this checkout), not to loose ranges.
- `.pylibs` is installed by the embedded interpreter itself so every compiled
  wheel matches the runtime ABI (copying the dev machine's .pylibs would drag
  cp314 .pyd files the 3.12 runtime cannot load if anything ever imported
  them).
"""

from __future__ import annotations

import argparse
import importlib.metadata
import io
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

INSTALLER_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = INSTALLER_DIR.parent
sys.path.insert(0, str(INSTALLER_DIR))

from wizard.core import pins  # noqa: E402

CACHE_DIR = INSTALLER_DIR / "_cache"
STAGING_DIR = INSTALLER_DIR / "_staging"
PAYLOAD_DIR = INSTALLER_DIR / "payload"
PAYLOAD_PATH = PAYLOAD_DIR / "runtime.tar.xz"

# Runtime dependencies shipped in the embedded interpreter's site-packages
# (pinned from the dev environment at build time). Deliberately NOT included:
# pytest/mypy/ruff (dev-only) and PySide6 (the ui extra is unused).
RUNTIME_PACKAGES = [
    "sherpa-onnx",
    "numpy",
    "scipy",
    "sounddevice",
    "pydantic",
    "pyyaml",
    "watchdog",
    "structlog",
    "orjson",
    "httpx",
    "tqdm",
    "requests",
    "pywin32",
    "pyautogui",
    "sentencepiece",
    "pypinyin",
]

_COPY_IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo")


def log(message: str) -> None:
    print(message, flush=True)


def download(url: str, dest: Path) -> Path:
    """Download once into the cache; reuse afterwards."""
    if dest.is_file():
        log(f"  cached: {dest.name}")
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    log(f"  downloading {url}")
    request = urllib.request.Request(url, headers={"User-Agent": "winvoice-build"})
    with urllib.request.urlopen(request, timeout=120) as response, open(dest, "wb") as file:
        shutil.copyfileobj(response, file)
    return dest


def run(cmd: list, cwd: Path = PROJECT_ROOT) -> None:
    log(f"  $ {' '.join(str(part) for part in cmd)}")
    result = subprocess.run([str(part) for part in cmd], cwd=str(cwd))
    if result.returncode != 0:
        raise SystemExit(f"[X] command failed (exit {result.returncode}): {cmd}")


def stage_python() -> Path:
    """Embedded Python + pip + `._pth` patch. Returns the interpreter path."""
    python_dir = STAGING_DIR / "python"
    python_dir.mkdir(parents=True, exist_ok=True)
    zip_path = download(pins.PYTHON_EMBED_URL, CACHE_DIR / Path(pins.PYTHON_EMBED_URL).name)
    log(f"  extracting {zip_path.name}")
    with zipfile.ZipFile(zip_path) as archive:
        archive.extractall(python_dir)

    major_minor = "".join(pins.PYTHON_VERSION.split(".")[:2])  # "3.12.10" -> "312"
    pth = python_dir / f"python{major_minor}._pth"
    content = pth.read_text(encoding="utf-8")
    if "#import site" in content:
        content = content.replace("#import site", "import site")
    if "\n.." not in content:
        content = content.rstrip("\n") + "\n..\n"
    pth.write_text(content, encoding="utf-8")

    interpreter = python_dir / "python.exe"
    get_pip = download(pins.GET_PIP_URL, CACHE_DIR / "get-pip.py")
    log("  bootstrapping pip")
    run([interpreter, str(get_pip), "--no-warn-script-location"], cwd=python_dir)
    # setuptools is required at user-build time only, but some pinned packages
    # publish sdist-only releases (pyautogui) — without it pip cannot install
    # them into the embedded runtime at all.
    run([interpreter, "-m", "pip", "install", "--no-warn-script-location",
         "--upgrade", "setuptools", "wheel"], cwd=python_dir)
    return interpreter


def dev_version(name: str) -> str:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError as error:
        raise SystemExit(
            f"[X] {name} is not installed in THIS dev environment — the build pins "
            "its version to what was actually tested. Install it first."
        ) from error


def stage_requirements(interpreter: Path) -> None:
    pins_args = [f"{name}=={dev_version(name)}" for name in RUNTIME_PACKAGES]
    log("  installing pinned runtime dependencies: " + ", ".join(pins_args))
    run([interpreter, "-m", "pip", "install", "--no-warn-script-location",
         "--upgrade"] + pins_args)


def stage_pylibs(interpreter: Path, reuse: bool) -> None:
    target = STAGING_DIR / ".pylibs"
    repo_pylibs = PROJECT_ROOT / ".pylibs"
    if reuse and repo_pylibs.is_dir():
        log("  copying dev machine's .pylibs (--reuse-pylibs)")
        shutil.copytree(repo_pylibs, target, ignore=_COPY_IGNORE, dirs_exist_ok=True)
        return
    log(f"  installing {', '.join(pins.DSH_PYLIBS_PACKAGES)} into .pylibs")
    run([interpreter, "-m", "pip", "install", "--no-warn-script-location",
         "--target", str(target), "--upgrade"] + list(pins.DSH_PYLIBS_PACKAGES))


def stage_llama() -> None:
    dest = STAGING_DIR / "tools" / pins.LLAMA_DIR
    if dest.is_dir():
        log("  llama.cpp already staged")
        return
    repo_dir = PROJECT_ROOT / "tools" / pins.LLAMA_DIR
    if repo_dir.is_dir():
        log(f"  copying local {repo_dir.name}")
        shutil.copytree(repo_dir, dest, ignore=_COPY_IGNORE)
        return
    zip_path = download(pins.LLAMA_DOWNLOAD_URL, CACHE_DIR / pins.LLAMA_ASSET)
    extract_dir = STAGING_DIR / "tools" / "_llama_extract"
    if extract_dir.exists():
        shutil.rmtree(extract_dir)
    log(f"  extracting {zip_path.name}")
    with zipfile.ZipFile(zip_path) as archive:
        archive.extractall(extract_dir)
    # Release zips may wrap the binaries in one top-level folder — find it.
    candidates = [p for p in extract_dir.iterdir() if p.is_dir()]
    source = candidates[0] if len(candidates) == 1 else extract_dir
    source.rename(dest)


def stage_app() -> None:
    log("  copying winvoice/, scripts/, config template")
    shutil.copytree(PROJECT_ROOT / "winvoice", STAGING_DIR / "winvoice", ignore=_COPY_IGNORE,
                    dirs_exist_ok=True)
    shutil.copytree(PROJECT_ROOT / "scripts", STAGING_DIR / "scripts", ignore=_COPY_IGNORE,
                    dirs_exist_ok=True)
    config_dir = STAGING_DIR / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(PROJECT_ROOT / "config" / "config.template.yaml",
                 config_dir / "config.template.yaml")
    for name in ("runtime", "logs", "snapshots"):
        (STAGING_DIR / name).mkdir(parents=True, exist_ok=True)


def smoke_test(interpreter: Path) -> None:
    log("  smoke test: imports + CLI")
    probe = (
        "import sherpa_onnx, numpy, scipy, sounddevice, pydantic, yaml, watchdog, "
        "structlog, orjson, httpx, tqdm, requests, win32api, pyautogui, sentencepiece, "
        "pypinyin; "
        "import winvoice, winvoice.__main__, winvoice.audio.playback, "
        "winvoice.llm.server, winvoice.mcp_server; "
        "from winvoice import _vendor; _vendor.ensure('deepseek_harness'); "
        "import deepseek_harness; print('smoke-ok')"
    )
    result = subprocess.run([str(interpreter), "-c", probe], cwd=str(STAGING_DIR),
                            capture_output=True, text=True, encoding="utf-8",
                            errors="replace")
    if result.returncode != 0 or "smoke-ok" not in result.stdout:
        log(result.stdout[-2000:])
        log(result.stderr[-2000:])
        raise SystemExit("[X] staging smoke test failed — payload not built")
    log("  " + result.stdout.strip().splitlines()[-1])
    run([interpreter, "-m", "winvoice", "--help"], cwd=STAGING_DIR)
    run([interpreter, "scripts/download_models.py", "--list"], cwd=STAGING_DIR)


def pack_payload() -> Path:
    PAYLOAD_DIR.mkdir(parents=True, exist_ok=True)
    log(f"  packing {STAGING_DIR} -> {PAYLOAD_PATH}")
    with tarfile.open(PAYLOAD_PATH, "w:xz") as tar:
        for entry in sorted(STAGING_DIR.iterdir()):
            tar.add(entry, arcname=entry.name)
    return PAYLOAD_PATH


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reuse-pylibs", action="store_true",
                        help="copy the repo's .pylibs instead of reinstalling (offline builds)")
    parser.parse_args()

    if STAGING_DIR.exists():
        log(f"cleaning {STAGING_DIR}")
        shutil.rmtree(STAGING_DIR)
    STAGING_DIR.mkdir(parents=True)

    log("[1/6] embedded Python")
    interpreter = stage_python()
    log("[2/6] runtime dependencies")
    stage_requirements(interpreter)
    log("[3/6] DSH .pylibs")
    stage_pylibs(interpreter, reuse=parser.parse_args().reuse_pylibs)
    log("[4/6] llama.cpp")
    stage_llama()
    log("[5/6] application tree")
    stage_app()
    log("[6/6] smoke test + packing")
    smoke_test(interpreter)
    payload = pack_payload()

    size_mb = payload.stat().st_size / (1024 * 1024)
    staged_mb = sum(f.stat().st_size for f in STAGING_DIR.rglob("*") if f.is_file()) / (1024 * 1024)
    log(f"\n[OK] payload ready: {payload}")
    log(f"     staged {staged_mb:.0f} MB -> compressed {size_mb:.0f} MB")
    log("next: python installer/build_installer.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
