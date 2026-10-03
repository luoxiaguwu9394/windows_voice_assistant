#!/usr/bin/env python3
"""
Headless end-to-end run of the wizard pipeline (no UI, no user-machine side
effects): extracts the real payload into a scratch install dir and drives the
exact core functions the wizard pages call, in wizard order.

    python installer/e2e_pipeline.py

Steps verified:
  1. payload extraction (the tar.xz the setup exe ships)
  2. default config rendering (boots before any user input)
  3. model step idempotency (download_models --only skips present files)
  4. user config rendering (wake words / city / devices / DSH / cloud switch)
  5. DSH bridge install via the bundled runtime (--prefer-bundled ignores any
     PATH `dsh`, so the bundled branch is what actually gets exercised)
  6. `python -m winvoice --check` exit code 0 against the generated config
  7. install marker + shortcuts module importability
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
from pathlib import Path

INSTALLER_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = INSTALLER_DIR.parent
sys.path.insert(0, str(INSTALLER_DIR))

from wizard.core import flow, marker, modelplan, wakewords  # noqa: E402
from wizard.core.payload import extract_payload, payload_path  # noqa: E402
from wizard.core.state import InstallState  # noqa: E402

SCRATCH = PROJECT_ROOT / "runtime" / "_wizard_e2e"
INSTALL = SCRATCH / "install"


def log(message: str) -> None:
    print(message, flush=True)


def step(number: int, name: str) -> None:
    log(f"\n[{number}] {name} {'-' * max(0, 58 - len(name))}")


def main() -> int:
    if SCRATCH.exists():
        log(f"cleaning {SCRATCH}")
        shutil.rmtree(SCRATCH)
    INSTALL.mkdir(parents=True)

    state = InstallState(
        install_dir=INSTALL,
        llm_tier="3b",
        wake_words=wakewords.validate(["小助手", "你好助手", "你好"])[0],
        city="佛山",
        dsh_enabled=True,
        audio_input="default",
        audio_output="default",
    )

    step(1, "extract payload")
    started = time.monotonic()
    count = extract_payload(payload_path(INSTALLER_DIR), INSTALL,
                            on_progress=lambda done, total: None)
    log(f"  {count} members in {time.monotonic() - started:.0f}s")
    assert (INSTALL / "python" / "python.exe").is_file(), "embedded python missing"
    assert (INSTALL / "tools").is_dir() and (INSTALL / ".pylibs").is_dir()
    assert not (INSTALL / "models").exists(), "payload must not ship models"

    step(2, "default config")
    target = flow.write_default_config(INSTALL)
    assert target and target.is_file()
    text = target.read_text(encoding="utf-8")
    assert "dsh:" in text, "template lost its DSH section"
    assert "@AUDIO_INPUT_DEVICE@" not in text, "tokens were not rendered"

    step(3, "model step (idempotent skip; models copied from the dev checkout)")
    dev_models = PROJECT_ROOT / "models"
    if dev_models.is_dir():
        log("  copying dev models/ (simulates a completed download)")
        shutil.copytree(dev_models, INSTALL / "models", dirs_exist_ok=True)
    else:
        raise SystemExit("dev machine has no models/ — run the download first once")
    python = flow.embedded_python(INSTALL)
    argv = modelplan.download_argv("models", state.llm_tier, False)
    proc = subprocess.run([str(python), *argv], cwd=str(INSTALL),
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace")
    log("  " + (proc.stdout.strip().splitlines() or ["<no output>"])[-1])
    assert proc.returncode == 0, f"download step failed: {proc.stderr[-800:]}"
    assert "Already present" in proc.stdout, "present models were re-downloaded!"

    step(4, "user config render")
    flow.render_user_config(INSTALL, state)
    config_text = flow.config_path(INSTALL).read_text(encoding="utf-8")
    assert 'city: "佛山"' in config_text
    assert '- "你好"' in config_text
    assert "enabled: true" in config_text and "dsh:" in config_text
    backups = list((INSTALL / "config").glob("config.yaml.bak-*"))
    assert len(backups) == 1, "existing config was not backed up"

    step(5, "DSH bridge install (bundled runtime, PATH dsh ignored)")
    lines: list = []
    code = flow.install_dsh(INSTALL, state, on_line=lambda line: lines.append(line))
    for line in lines[-6:]:
        log("  " + line)
    assert code == 0, f"install_dsh_bridge exited {code}"
    assert (INSTALL / "runtime" / "dsh_bridge").is_dir(), "bundle dir missing"

    step(6, "winvoice --check (loads real models; llama-server auto-start)")
    check_lines: list = []
    code = flow.run_check(INSTALL, state, on_line=lambda line: check_lines.append(line))
    for line in check_lines[-12:]:
        log("  " + line)
    assert code == 0, f"--check exited {code}"

    step(7, "marker + shortcuts plumbing")
    marker.write_marker(INSTALL, "0.1.0-dev")
    assert marker.marker_version(INSTALL) == "0.1.0-dev"
    from wizard.core.shortcuts import create_shortcut  # noqa: F401
    log("  shortcuts module importable (real .lnk creation left to the UI run)")

    log("\n[E2E OK] wizard pipeline verified end to end against the real payload")
    return 0


if __name__ == "__main__":
    sys.exit(main())
