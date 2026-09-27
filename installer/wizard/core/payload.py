"""
Extract the bundled runtime payload (tar.xz) into the install directory.

The payload contains ONLY runtime-owned trees — `python/`, `winvoice/`,
`scripts/`, `config/config.template.yaml`, `tools/`, `.pylibs/` — never
`models/`, `config/config.yaml`, `logs/`, `runtime/` or `snapshots/`. That
split is what makes re-extraction (修复/升级) safe: user data is not in the
archive, so it cannot be clobbered by it.
"""

from __future__ import annotations

import tarfile
from pathlib import Path
from typing import Callable, Optional

from ..core.buildinfo import PAYLOAD_RELPATH


class PayloadError(RuntimeError):
    pass


def payload_path(base_dir: Path) -> Path:
    """Where the bundled archive lives (inside _MEIPASS when frozen)."""
    return base_dir / PAYLOAD_RELPATH


def extract_payload(archive: Path, dest_dir: Path,
                    on_progress: Optional[Callable[[int, int], None]] = None) -> int:
    """
    Extract `archive` under `dest_dir`; returns the number of members.
    Progress is cumulative bytes / total bytes across all regular files.
    """
    if not archive.is_file():
        raise PayloadError(f"找不到运行时载荷: {archive}")
    dest_dir.mkdir(parents=True, exist_ok=True)

    try:
        with tarfile.open(archive, "r:xz") as tar:
            members = tar.getmembers()
            total = sum(m.size for m in members if m.isfile())
            done = 0

            def _report() -> None:
                if on_progress is not None and total:
                    on_progress(done, total)

            _report()
            for member in members:
                try:
                    tar.extract(member, dest_dir, filter="data")
                except TypeError:  # Python < 3.12 without the filter argument
                    tar.extract(member, dest_dir)
                if member.isfile():
                    done += member.size
                    _report()
    except tarfile.TarError as error:
        raise PayloadError(f"运行时载荷损坏: {error}") from error
    return len(members)
