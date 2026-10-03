"""
High-level wizard operations, wired from the other core modules.

Each function here is the unit the UI pages await inside their worker
threads: run a subprocess, stream its output through callbacks, return a
plain result. No tkinter imports — the UI drives these, never the reverse.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, List, Optional

from . import pins
from .modelplan import download_argv
from .progress import DownloadEvents, failures, feed_line, parse_probe_result
from .runner import CANCEL_CHECK, LINE_CALLBACK, run_streaming
from .state import InstallState
from .template import render_template

Cancel = Optional[CANCEL_CHECK]


class FlowError(RuntimeError):
    """An operation failed in a way the wizard should show verbatim."""


# ──────────────────────────────────────────────────────────────
# Paths
# ──────────────────────────────────────────────────────────────

def embedded_python(install_dir: Path) -> Path:
    return install_dir / "python" / "python.exe"


def config_template_path(install_dir: Path) -> Path:
    return install_dir / "config" / "config.template.yaml"


def config_path(install_dir: Path) -> Path:
    return install_dir / "config" / "config.yaml"


def enroll_profile_path(install_dir: Path) -> Path:
    return install_dir / "models" / "sv" / "profiles" / "me.json"


def llama_server_path(install_dir: Path) -> Path:
    return install_dir / "tools" / pins.LLAMA_DIR / "llama-server.exe"


def update_env(state: InstallState, base: Optional[dict] = None) -> dict:
    """Environment for download/probe subprocesses: proxy + HF mirror."""
    env = dict(base if base is not None else os.environ)
    if state.proxy_url:
        proxy = state.proxy_url.strip()
        env["HTTP_PROXY"] = proxy
        env["HTTPS_PROXY"] = proxy
        env["NO_PROXY"] = "localhost,127.0.0.1"
    else:
        # An explicitly empty value stops requests from inheriting the shell's
        # proxy when the user picked 直连.
        env.pop("HTTP_PROXY", None)
        env.pop("HTTPS_PROXY", None)
    if state.hf_endpoint:
        env[pins.HF_ENDPOINT_ENV] = state.hf_endpoint
    return env


# ──────────────────────────────────────────────────────────────
# Config rendering
# ──────────────────────────────────────────────────────────────

def write_default_config(install_dir: Path) -> Optional[Path]:
    """Render the template with pure defaults; skip when a config already exists."""
    target = config_path(install_dir)
    if target.exists():
        return None
    template = config_template_path(install_dir)
    if not template.is_file():
        raise FlowError(f"缺少配置模板: {template}")
    rendered = render_template(template.read_text(encoding="utf-8"), _defaults())
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(rendered, encoding="utf-8")
    return target


def _defaults():
    # Imported here to avoid a circular import at module load (state imports
    # template, not flow).
    from .state import default_config_values

    return default_config_values()


def render_user_config(install_dir: Path, state: InstallState) -> Path:
    """Back up an existing config.yaml, then write the user's choices."""
    template = config_template_path(install_dir)
    if not template.is_file():
        raise FlowError(f"缺少配置模板: {template}")
    target = config_path(install_dir)
    if target.exists():
        backup = target.with_name(f"config.yaml.bak-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(target, backup)
    rendered = render_template(template.read_text(encoding="utf-8"), state.config_values())
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(rendered, encoding="utf-8")
    return target


# ──────────────────────────────────────────────────────────────
# Models
# ──────────────────────────────────────────────────────────────

class _EventBridge(DownloadEvents):
    """Forwards protocol events to the UI and records results for verification."""

    def __init__(self, on_model: Callable[[str, str], None],
                 on_progress: Callable[[str, int, int], None],
                 on_result: Callable[[str, bool, str], None]):
        self._on_model = on_model
        self._on_progress = on_progress
        self._on_result = on_result
        self.results: dict = {}

    def on_model(self, key: str, note: str) -> None:
        self._on_model(key, note)

    def on_progress(self, key: str, done: int, total: int) -> None:
        self._on_progress(key, done, total)

    def on_result(self, key: str, ok: bool, detail: str) -> None:
        self.results[key] = (ok, detail)
        self._on_result(key, ok, detail)


def run_model_download(install_dir: Path, state: InstallState,
                       on_line: LINE_CALLBACK,
                       on_model: Callable[[str, str], None],
                       on_progress: Callable[[str, int, int], None],
                       on_result: Callable[[str, bool, str], None],
                       cancel: Cancel = None) -> List[str]:
    """
    Download the wizard's model set via the embedded interpreter, then seal
    models/ for the startup integrity check. Returns the list of failed keys
    (empty = success). A key that never reported a result counts as failed —
    a silently skipped model must not pass for a completed download.
    """
    python = embedded_python(install_dir)
    if not python.is_file():
        raise FlowError(f"找不到内嵌解释器: {python}")

    events = _EventBridge(on_model, on_progress, on_result)

    def pump(line: str) -> None:
        on_line(line)
        feed_line(line, events)

    argv = download_argv("models", state.llm_tier, state.include_streaming_asr)
    code = run_streaming([str(python), *argv], cwd=install_dir,
                         on_line=pump, env=update_env(state), cancel=cancel)
    if code != 0:
        raise FlowError(f"模型下载失败（退出码 {code}），已保留断点，可重试。")

    failed = failures(events.results, expected_model_keys(state))
    if failed:
        raise FlowError("以下模型未确认完成: " + ", ".join(failed))

    # Seal what just landed: startup does a size check, --check deep-hashes.
    on_line(" sealing models/ → models/integrity.json")
    seal_code = run_streaming(
        [str(python), "scripts/download_models.py", "--models-dir", "models", "--seal"],
        cwd=install_dir, on_line=on_line, env=update_env(state), cancel=cancel,
    )
    if seal_code != 0:
        on_line(" [!] 封存失败（不影响使用，仅启动完整性检查退化为跳过）")
    return []


def expected_model_keys(state: InstallState) -> List[str]:
    from .modelplan import model_keys

    return model_keys(state.llm_tier, state.include_streaming_asr)


# ──────────────────────────────────────────────────────────────
# Audio probes
# ──────────────────────────────────────────────────────────────

def _run_probe(install_dir: Path, state: InstallState, argv: List[str],
               on_line: LINE_CALLBACK, cancel: Cancel = None) -> dict:
    python = embedded_python(install_dir)
    result: dict = {}

    def pump(line: str) -> None:
        on_line(line)
        parsed = parse_probe_result(line)
        if parsed is not None:
            result.clear()
            result.update(parsed)

    code = run_streaming([str(python), "scripts/audio_probe.py", *argv],
                         cwd=install_dir, on_line=pump, env=update_env(state),
                         cancel=cancel)
    if not result:
        raise FlowError(f"音频探针无输出（退出码 {code}）")
    return result


def probe_devices(install_dir: Path, state: InstallState,
                  on_line: LINE_CALLBACK, cancel: Cancel = None) -> dict:
    return _run_probe(install_dir, state, ["list"], on_line, cancel)


def probe_play_test(install_dir: Path, state: InstallState, device: str,
                    host_api: str, rate: int, on_line: LINE_CALLBACK,
                    cancel: Cancel = None) -> dict:
    return _run_probe(
        install_dir, state,
        ["play-test", "--device", device or "default",
         "--host-api", host_api or "auto", "--rate", str(int(rate or 0))],
        on_line, cancel,
    )


def probe_record_level(install_dir: Path, state: InstallState, device: str,
                       seconds: float, on_line: LINE_CALLBACK,
                       cancel: Cancel = None) -> dict:
    return _run_probe(
        install_dir, state,
        ["record-level", "--device", device or "default",
         "--seconds", str(float(seconds or 4.0))],
        on_line, cancel,
    )


# ──────────────────────────────────────────────────────────────
# Self-check, enrollment, DSH, keys, launch
# ──────────────────────────────────────────────────────────────

def run_check(install_dir: Path, state: InstallState, on_line: LINE_CALLBACK,
              cancel: Cancel = None) -> int:
    python = embedded_python(install_dir)
    return run_streaming(
        [str(python), "-m", "winvoice", "--check"],
        cwd=install_dir, on_line=on_line, env=update_env(state), cancel=cancel,
    )


def start_enroll(install_dir: Path) -> subprocess.Popen:
    from .runner import spawn_console

    python = embedded_python(install_dir)
    # `--force`: an upgraded install keeps the previous profile, and without it
    # enroll_start refuses with "already enrolled" the moment the engines have
    # loaded — the console then closes itself and nothing tells the user why.
    # Clicking 立即注册 means "record (again)", so re-enrolling overwrites.
    return spawn_console(
        [str(python), "-m", "winvoice.enroll", "--speaker", "me", "--force"],
        cwd=install_dir,
    )


def profile_mtime_ns(install_dir: Path) -> Optional[int]:
    """`me.json`'s mtime in ns, or None while there is no profile file."""
    try:
        return enroll_profile_path(install_dir).stat().st_mtime_ns
    except OSError:
        return None


def install_dsh(install_dir: Path, state: InstallState, on_line: LINE_CALLBACK,
                cancel: Cancel = None) -> int:
    python = embedded_python(install_dir)
    return run_streaming(
        [str(python), "scripts/install_dsh_bridge.py", "--install"],
        cwd=install_dir, on_line=on_line, env=update_env(state), cancel=cancel,
    )


def persist_keys(state: InstallState) -> List[str]:
    """setx the provided cloud keys; returns the variables that were set."""
    from .runner import setx

    written: List[str] = []
    if state.remote_key:
        if setx("REMOTE_API_KEY", state.remote_key):
            written.append("REMOTE_API_KEY")
    if state.deepseek_key:
        if setx("DEEPSEEK_API_KEY", state.deepseek_key):
            written.append("DEEPSEEK_API_KEY")
    return written


def launch_assistant(install_dir: Path) -> subprocess.Popen:
    from .runner import spawn_console

    python = embedded_python(install_dir)
    return spawn_console([str(python), "-m", "winvoice"], cwd=install_dir)


def copy_setup_exe(install_dir: Path) -> Optional[Path]:
    """
    Park a copy of the running setup exe in the install dir so the wizard can
    be re-entered by double-clicking there (升级/修复 without hunting for the
    original download). No-op in a source-tree run.
    """
    if not getattr(sys, "frozen", False):
        return None
    try:
        source = Path(sys.executable)
        dest = install_dir / source.name
        if source.resolve() == dest.resolve():
            return dest
        shutil.copy2(source, dest)
        return dest
    except Exception:
        return None
