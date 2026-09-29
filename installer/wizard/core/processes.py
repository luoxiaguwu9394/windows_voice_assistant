"""
Stop whatever is holding the install directory's files open.

Why this exists (measured, 2026-09-28): re-running the installer to upgrade
stalls the payload extraction at **92 %** — the point where `tools/` begins
(91.9 % of the payload's bytes are `python/` and friends, and `tools/` is the
next tree). The first thing in `tools/` that is locked is
`tools\\llama-b7376-bin-win-cpu-x64\\llama-server.exe`: the assistant reuses a
healthy llama-server *whatever started it* and never owns it, so the process
outlives the assistant and keeps that .exe open. `tarfile.extract` then raises
`PermissionError`, which is not a `TarError` — it escaped the extractor's error
handling and surfaced as a modal dialog on the Tk thread, so the wizard looked
frozen at 92 % with the progress bar never moving again.

Rules for this module:

* **never kill a process we cannot prove is ours** — `llama-server.exe` and
  `llama-cli.exe` are only killed by image name when process enumeration is
  unavailable (their image name alone proves nothing about who started them,
  and a user's own llama-server must be left alone); normally the decision is
  the executable's **path**: inside the install directory or untouched;
* **never raise** — a process that will not die is reported to the caller so it
  can name the locked file in the error instead of failing silently.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Callable, Optional

# Fallback only: shipped by this project's `tools/`, but "image name" is not
# proof of ownership, so these are used only when the path query failed.
_OWNED_IMAGES = ("llama-server.exe", "llama-cli.exe")

_PS_QUERY = (
    # The console encoding is forced to UTF-8 first: a redirected PowerShell on
    # a Chinese Windows writes GBK, and `%LOCALAPPDATA%` usually contains the
    # user's name, so a non-ASCII path is the normal case, not an exotic one.
    # Without this the path comparison silently sees mojibake and stops nothing.
    "[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
    "Get-CimInstance Win32_Process | "
    "Select-Object ProcessId,Name,ExecutablePath | ConvertTo-Csv -NoTypeInformation"
)

_NO_WINDOW = 0x08000000

# Console tools answer in the OEM/ANSI codepage (GBK on a Chinese Windows),
# never UTF-8; a wrong guess raises inside subprocess's reader thread and the
# output is lost. Try the likely encodings, then give up on the text rather
# than on the command.
_DECODE_ORDER = ("utf-8", "mbcs", "gbk")


def _decode(raw: bytes) -> str:
    for encoding in _DECODE_ORDER:
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def _run(command: list[str], timeout: float = 20.0) -> tuple[int, str]:
    """Run a console command hidden; return (returncode, stdout)."""
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            timeout=timeout,
            creationflags=_NO_WINDOW,
        )
        return completed.returncode, _decode(completed.stdout or b"")
    except Exception:
        return -1, ""


def processes_under(root: Path) -> Optional[list[tuple[int, str, str]]]:
    """
    (pid, name, exe_path) for processes whose executable is inside `root`.

    PowerShell's CIM provider is used rather than psutil or `wmic`: psutil is
    not part of the wizard's PyInstaller build, and `wmic` is being removed
    from Windows.

    Returns an **empty list** when nothing matches, and **None** when the
    question could not be asked at all — the caller treats those differently,
    because "nobody is holding the directory" and "I cannot see who is" must
    not lead to the same action.
    """
    target = Path(root).resolve()
    code, output = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", _PS_QUERY])
    if not output.strip():
        return None

    found: list[tuple[int, str, str]] = []
    for line in output.splitlines():
        parts = [piece.strip().strip('"') for piece in line.split('","')]
        if len(parts) < 3:
            continue
        pid_text, name, path = parts[0], parts[1], parts[2]
        if not path or not pid_text.isdigit():
            continue
        try:
            # Path containment, rather than string-prefix matching, keeps
            # C:\\Apps\\WinVoiceBackup outside C:\\Apps\\WinVoice.
            Path(path).resolve().relative_to(target)
        except (OSError, ValueError):
            continue
        found.append((int(pid_text), name, path))
    return found


def _kill_pid(pid: int) -> bool:
    code, _ = _run(["taskkill", "/f", "/pid", str(pid)])
    return code == 0


def _kill_image(image: str) -> bool:
    code, _ = _run(["taskkill", "/f", "/im", image])
    return code == 0


def stop_occupants(
    install_dir: Path,
    *,
    log: Optional[Callable[[str], None]] = None,
    wait_s: float = 1.0,
) -> list[str]:
    """
    Stop the assistant, its llama-server and anything else living in the
    install directory. Returns the names of what was stopped (possibly empty);
    never raises, and never touches a process outside `install_dir` unless
    enumeration itself was impossible (then, and only then, the image-name
    fallback runs).
    """
    say = log or (lambda _message: None)
    stopped: list[str] = []
    mine = os.getpid()

    found = processes_under(install_dir)
    if found is None:
        say("无法枚举进程（PowerShell 不可用），按镜像名尝试停止 llama-server")
        for image in _OWNED_IMAGES:
            if _kill_image(image):
                stopped.append(image)
                say(f"已停止 {image}（按镜像名）")
    else:
        for pid, name, path in found:
            if pid == mine:
                continue
            if _kill_pid(pid):
                stopped.append(f"{name} (pid {pid})")
                say(f"已停止 {name} (pid {pid})：{path}")

    if stopped and wait_s > 0:
        # Windows releases the file handles asynchronously; writing into a file
        # that is still open is exactly the failure this is here to avoid.
        time.sleep(wait_s)
    return stopped
