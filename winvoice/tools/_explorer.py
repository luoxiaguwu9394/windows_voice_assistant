"""
Closing Explorer *folder windows*, which is not the same as killing Explorer.

`close_app` used to treat 「关闭文件资源管理器」 as `taskkill /f /im explorer.exe`.
That is not "close the file manager" — `explorer.exe` is the Windows **shell**:
the desktop, the taskbar, the Start menu and every folder window are that one
process. Forcing it down takes the whole GUI with it.

The fix is to close the folder *windows* through Explorer's own automation
object, which is exactly the thing the user asked for. `Shell.Application`'s
`Windows()` collection contains open folder windows and nothing else — the
desktop and taskbar are not in it, so `Quit()` on every member cannot reach
them. Measured on this machine:

    shell processes running            : 2  (14576, 18608)
    Windows() members                  : 1  (the folder window that was opened)
    after Quit()                       : 0, with both shell processes still alive

Living here rather than inline in `builtin.py` for the same reason
`_coreaudio.py` does: `close_app` needs to *do* it and
`CloseAppVerifier` needs to *observe* it, and two copies of the same COM
invocation is how the two would come to disagree about what "closed" means.
"""

from __future__ import annotations

import subprocess
from typing import Optional, Tuple

# `Shell.Application` needs a real STA-capable PowerShell; the cost is ~400 ms
# and is only paid by the Explorer path.
_TIMEOUT_S = 15

# Reports the number of open folder windows. Kept as one expression so this and
# the counter below cannot drift apart in what they consider a folder window.
_COUNT_WINDOWS = (
    "$shell = New-Object -ComObject Shell.Application\n"
    "$explorerExe = Join-Path $env:windir 'explorer.exe'\n"
    "$w = @($shell.Windows() | Where-Object { $_.FullName -eq $explorerExe })\n"
    "Write-Output $w.Count\n"
)

# Closes every folder window, then reports what the count *became* so the caller
# can tell "closed two" from "there was nothing to close".
_CLOSE_WINDOWS = (
    "$shell = New-Object -ComObject Shell.Application\n"
    "$explorerExe = Join-Path $env:windir 'explorer.exe'\n"
    "$w = @($shell.Windows() | Where-Object { $_.FullName -eq $explorerExe })\n"
    "foreach ($x in $w) { $x.Quit() }\n"
    "Start-Sleep -Milliseconds 300\n"
    "$shell2 = New-Object -ComObject Shell.Application\n"
    "$after = @($shell2.Windows() | Where-Object { $_.FullName -eq $explorerExe })\n"
    "Write-Output $after.Count\n"
)


def _run(script: str, timeout_s: float = _TIMEOUT_S) -> Tuple[Optional[int], Optional[str]]:
    """Run a snippet and return (trailing integer on stdout | None, error | None)."""
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        return None, "explorer automation timed out"
    except OSError as exc:
        return None, str(exc)

    if proc.returncode != 0:
        return None, (proc.stderr or "explorer automation failed").strip()[:400]

    lines = [line.strip() for line in (proc.stdout or "").splitlines() if line.strip()]
    if not lines or not lines[-1].isdigit():
        return None, "explorer automation returned no count"
    return int(lines[-1]), None


def count_windows(timeout_s: float = _TIMEOUT_S) -> Optional[int]:
    """
    Open folder windows right now, or None when the count cannot be read.

    None rather than raising or guessing: a verifier that cannot observe must
    report `NOT_VERIFIABLE`, and an exception here would fail the tool call it
    was only supposed to be checking.
    """
    count, _error = _run(_COUNT_WINDOWS, timeout_s)
    return count


def close_windows(timeout_s: float = _TIMEOUT_S) -> Tuple[Optional[int], Optional[str]]:
    """
    Close every folder window. Returns (remaining count | None, error | None).

    Never touches the shell process: it only asks each window to close itself.
    """
    return _run(_CLOSE_WINDOWS, timeout_s)


__all__ = ["close_windows", "count_windows"]
