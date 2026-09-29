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
                    on_progress: Optional[Callable[[int, int], None]] = None,
                    on_member: Optional[Callable[[str], None]] = None) -> int:
    """
    Extract `archive` under `dest_dir`; returns the number of members.

    Progress is cumulative bytes / total bytes across all regular files.
    `on_member` is told the archive-relative name about to be written, so the
    UI can show *what* it is doing: writing 100 MB of executables can take a
    while under a virus scanner, and a progress bar alone reads as a hang.

    A file that cannot be written (in use by a running assistant or its
    llama-server) is reported **by name**, with the remedy — `PermissionError`
    used to escape as a bare OSError into a modal dialog, which is how an
    upgrade came to look frozen at 92 %.
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
                if on_member is not None and member.isfile():
                    on_member(member.name)
                try:
                    tar.extract(member, dest_dir, filter="data")
                except TypeError as error:
                    raise PayloadError(
                        "当前 Python 不支持安全解压过滤器；请重新下载新版安装器"
                    ) from error
                except OSError as error:
                    raise PayloadError(
                        f"写入失败：{member.name}\n"
                        f"  {error}\n"
                        f"  该文件正被占用（助手或它的 llama-server 还在运行）。"
                        f"请关闭助手窗口，或在任务管理器里结束 "
                        f"llama-server.exe / python.exe 后重试。"
                    ) from error
                if member.isfile():
                    done += member.size
                    _report()
    except tarfile.TarError as error:
        raise PayloadError(f"运行时载荷损坏: {error}") from error
    return len(members)
