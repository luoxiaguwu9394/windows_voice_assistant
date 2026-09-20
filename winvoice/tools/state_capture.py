"""
Observed Windows state, for verifying that a tool actually changed something.

`ToolResult.success` is what a tool *claims*; a `VerificationResult` is what the
machine *shows*. The two are checked separately (see `verifier.py`) because the
history of this project contains two bugs where they disagreed — `open_app`
reported 「已经打开谷歌浏览器了。」 while cmd.exe printed 「不是内部或外部命令」, and
`severity` was read off a source that always said "info".

Everything here is read-only observation through a small surface (`SystemProbe`)
so a verifier can be tested against a scripted machine instead of the real one —
launching Chrome in CI is not an option, and "the process exists on the
developer's laptop" is not an assertion.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Protocol, Set

# `tasklist` is the only process lister present on every Windows SKU without
# extra dependencies (psutil is not a dependency of this project, and adding one
# to answer "is notepad running?" is not a trade worth making).
_TASKLIST_TIMEOUT_S = 5


@dataclass(frozen=True)
class PathState:
    """What the filesystem said about one path at one moment."""

    path: str
    exists: bool
    is_file: bool = False
    size: Optional[int] = None


class SystemProbe(Protocol):
    """
    The machine, as far as verifying a tool result is concerned.

    Only methods verifiers actually call appear here. A Protocol member with a
    default body is still a *required* member for structural typing, so a
    convenience helper declared here would silently stop `WindowsProbe` from
    satisfying the protocol — a subclass check that fails at type-check time
    rather than at runtime, which is the good case, but noise all the same.
    """

    def running_processes(self) -> Set[str]:
        """Lowercased image names of every running process (e.g. `chrome.exe`)."""
        ...

    def path_state(self, path: str) -> PathState:
        ...

    def read_text(self, path: str) -> Optional[str]:
        """File contents, or None when unreadable/absent/binary-ish."""
        ...


class WindowsProbe:
    """The real machine. Every method here is best-effort and never raises."""

    def running_processes(self) -> Set[str]:
        """
        Image names currently running, lowercased.

        An unavailable `tasklist` returns an empty set rather than raising:
        "I could not look" and "it is not running" are different answers, and
        returning a guess would turn an unobservable host into a stream of
        reported failures.
        """
        try:
            proc = subprocess.run(
                ["tasklist", "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                timeout=_TASKLIST_TIMEOUT_S,
                errors="replace",
            )
        except (OSError, subprocess.SubprocessError):
            return set()

        if proc.returncode != 0:
            return set()

        names: Set[str] = set()
        for line in proc.stdout.splitlines():
            # CSV rows look like: "chrome.exe","1234","Console","1","123,456 K"
            head = line.strip().split(",", 1)[0].strip().strip('"')
            if head:
                names.add(head.lower())
        return names

    def path_state(self, path: str) -> PathState:
        candidate = Path(str(path)).expanduser()
        try:
            stat = candidate.stat()
        except (OSError, ValueError):
            return PathState(path=str(candidate), exists=False)
        return PathState(
            path=str(candidate),
            exists=True,
            is_file=candidate.is_file(),
            size=stat.st_size,
        )

    def read_text(self, path: str) -> Optional[str]:
        try:
            return Path(str(path)).expanduser().read_text(encoding="utf-8")
        except (OSError, ValueError, UnicodeDecodeError):
            return None

    def volume_percent(self) -> Optional[int]:
        """
        The master endpoint volume 0-100, or None when it cannot be read.

        Only the volume verifier calls this, and it costs a PowerShell process
        (~300 ms), so it is not part of the generic snapshot.
        """
        from ._coreaudio import read_volume_percent

        return read_volume_percent()


__all__ = ["PathState", "SystemProbe", "WindowsProbe"]
