"""
Snapshot Manager: file-level backup and restore for destructive actions.

- Backs up only declared modified paths
- Stores under snapshots/{timestamp}/
- Automatic cleanup by max_size_gb
"""

from __future__ import annotations

import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from winvoice.config import get_config
from winvoice.logging import get_logger

logger = get_logger(__name__)


@dataclass
class Snapshot:
    """Snapshot metadata."""
    snapshot_id: str
    timestamp: float
    files: List[Path]
    total_size: int


class SnapshotManager:
    """
    Manages file snapshots for destructive action rollback.

    Only backs up files declared in tool's modified_paths.
    """

    def __init__(self, base_path: Optional[Path] = None, max_size_gb: Optional[float] = None):
        cfg = get_config()
        self.base_path = base_path or Path(cfg.get("snapshot.path", "snapshots"))
        self.max_size_gb = max_size_gb or cfg.get("snapshot.max_size_gb", 10.0)
        self.enabled = cfg.get("snapshot.enabled", True)

        self.base_path.mkdir(parents=True, exist_ok=True)

    def create_snapshot(self, paths: List[Path]) -> Optional[Snapshot]:
        """Create snapshot of given paths before destructive operation."""
        if not self.enabled:
            return None

        snapshot_id = time.strftime("%Y%m%d_%H%M%S")
        snapshot_dir = self.base_path / snapshot_id
        snapshot_dir.mkdir(parents=True, exist_ok=True)

        files_copied = []
        total_size = 0

        for path in paths:
            path = Path(path).resolve()
            if not path.exists():
                logger.warning("snapshot_file_missing", path=str(path))
                continue

            # Preserve directory structure
            rel_path = path.relative_to(Path.cwd()) if path.is_relative_to(Path.cwd()) else Path(path.name)
            dest = snapshot_dir / rel_path
            dest.parent.mkdir(parents=True, exist_ok=True)

            shutil.copy2(path, dest)
            files_copied.append(path)
            total_size += path.stat().st_size
            logger.info("snapshot_file_copied", source=str(path), dest=str(dest))

        snapshot = Snapshot(
            snapshot_id=snapshot_id,
            timestamp=time.time(),
            files=files_copied,
            total_size=total_size,
        )

        self._cleanup_old_snapshots()
        logger.info("snapshot_created", snapshot_id=snapshot_id, files=len(files_copied), size_mb=total_size/1024/1024)
        return snapshot

    def restore_snapshot(self, snapshot_id: str) -> bool:
        """Restore all files from a snapshot."""
        snapshot_dir = self.base_path / snapshot_id
        if not snapshot_dir.exists():
            logger.error("snapshot_not_found", snapshot_id=snapshot_id)
            return False

        restored = 0
        for src in snapshot_dir.rglob("*"):
            if src.is_file():
                rel = src.relative_to(snapshot_dir)
                dest = Path.cwd() / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dest)
                restored += 1
                logger.info("snapshot_file_restored", file=str(dest))

        logger.info("snapshot_restored", snapshot_id=snapshot_id, files=restored)
        return True

    def list_snapshots(self) -> List[Snapshot]:
        """List available snapshots."""
        snapshots = []
        for dir_path in sorted(self.base_path.iterdir(), reverse=True):
            if dir_path.is_dir():
                files = list(dir_path.rglob("*"))
                files = [f for f in files if f.is_file()]
                total_size = sum(f.stat().st_size for f in files)
                snapshots.append(Snapshot(
                    snapshot_id=dir_path.name,
                    timestamp=dir_path.stat().st_mtime,
                    files=[f.relative_to(dir_path) for f in files],
                    total_size=total_size,
                ))
        return snapshots

    def _cleanup_old_snapshots(self) -> None:
        """Remove old snapshots if total size exceeds max_size_gb."""
        max_bytes = self.max_size_gb * 1024 * 1024 * 1024
        total_size = 0
        snapshots = self.list_snapshots()

        for snap in snapshots:
            total_size += snap.total_size
            if total_size > max_bytes:
                # Delete this and older snapshots
                for to_delete in snapshots[snapshots.index(snap):]:
                    snap_dir = self.base_path / to_delete.snapshot_id
                    shutil.rmtree(snap_dir, ignore_errors=True)
                    logger.info("snapshot_cleaned_up", snapshot_id=to_delete.snapshot_id)
                break


# ──────────────────────────────────────────────────────────────
# Singleton
# ──────────────────────────────────────────────────────────────

_snapshot_manager: Optional[SnapshotManager] = None

def get_snapshot_manager() -> SnapshotManager:
    global _snapshot_manager
    if _snapshot_manager is None:
        _snapshot_manager = SnapshotManager()
    return _snapshot_manager