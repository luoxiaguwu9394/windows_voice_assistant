"""Subprocess plumbing shared by every long operation in the wizard."""

from __future__ import annotations

import subprocess
import sys
import threading
from pathlib import Path
from typing import Callable, Optional

LINE_CALLBACK = Callable[[str], None]
CANCEL_CHECK = Callable[[], bool]


class OperationCancelled(RuntimeError):
    """The user pressed 取消 while a subprocess was running."""


def _no_window_flags() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def run_streaming(cmd: list, cwd: Path, on_line: LINE_CALLBACK,
                  env: Optional[dict] = None, cancel: Optional[CANCEL_CHECK] = None,
                  timeout_s: Optional[float] = None) -> int:
    """
    Run `cmd` with stdout+stderr merged, delivering text lines to `on_line`.

    Returns the exit code. Raises OperationCancelled when `cancel()` turns
    true — the child is killed first, so a cancelled download does not keep
    writing into models/. UTF-8 with replacement keeps a GBK console from
    killing the reader mid-stream.
    """
    process = subprocess.Popen(
        cmd,
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        creationflags=_no_window_flags(),
    )

    watchdog: Optional[threading.Timer] = None
    if timeout_s is not None:
        watchdog = threading.Timer(timeout_s, process.kill)
        watchdog.daemon = True
        watchdog.start()

    try:
        assert process.stdout is not None
        for raw in process.stdout:
            line = raw.rstrip("\r\n")
            on_line(line)
            if cancel is not None and cancel():
                process.kill()
                raise OperationCancelled()
        code = process.wait()
    except OperationCancelled:
        process.wait(timeout=5)
        raise
    finally:
        if watchdog is not None:
            watchdog.cancel()

    if cancel is not None and cancel():
        raise OperationCancelled()
    return code


def setx(name: str, value: str) -> bool:
    """Persist a user environment variable for future sessions (and this one)."""
    import os

    try:
        code = subprocess.run(
            ["setx", name, value],
            capture_output=True,
            text=True,
            creationflags=_no_window_flags(),
            timeout=30,
        ).returncode
    except Exception:
        return False
    if code == 0:
        os.environ[name] = value
    return code == 0


def spawn_console(cmd: list, cwd: Path, env: Optional[dict] = None) -> subprocess.Popen:
    """Start a child with its OWN console window (enrollment, the assistant)."""
    flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
    return subprocess.Popen(
        cmd,
        cwd=str(cwd),
        env=env,
        creationflags=flags,
        stdout=sys.stdout,
        stderr=sys.stderr,
    )
