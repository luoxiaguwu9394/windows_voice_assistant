"""
Snapshot Manager — file-level backup and restore for destructive actions.

A snapshot records the *absolute* original path of every file it copies,
so a restore puts each file back exactly where it came from rather than
relative to whatever the process' current working directory happens to be.

Layout:

    snapshots/<snapshot_id>/
        manifest.json      # {version, snapshot_id, timestamp, files: [...]}
        files/0000__name   # archived copies
"""

from __future__ import annotations

import json
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from winvoice.config import get_config
from winvoice.logging import get_logger

logger = get_logger(__name__)

MANIFEST_NAME = "manifest.json"
MANIFEST_VERSION = 1


@dataclass
class SnapshotEntry:
    """One archived file."""

    original: Path
    stored: str          # path relative to the snapshot directory
    size: int


@dataclass
class Snapshot:
    """Metadata for a snapshot."""

    snapshot_id: str
    timestamp: float
    entries: List[SnapshotEntry] = field(default_factory=list)

    @property
    def files(self) -> List[Path]:
        """Original absolute paths captured in this snapshot."""
        return [e.original for e in self.entries]

    @property
    def total_size(self) -> int:
        return sum(e.size for e in self.entries)


class SnapshotManager:
    """Creates and restores file snapshots for destructive tool calls."""

    def __init__(self, base_path: Optional[Path] = None, max_size_gb: Optional[float] = None):
        cfg = get_config()
        self.base_path = Path(base_path or cfg.get("snapshot.path", "snapshots"))
        self.max_size_gb = float(
            max_size_gb if max_size_gb is not None else cfg.get("snapshot.max_size_gb", 10.0)
        )
        self.enabled = bool(cfg.get("snapshot.enabled", True))
        self.base_path.mkdir(parents=True, exist_ok=True)

    # ── create ─────────────────────────────────────────────────

    def create_snapshot(self, paths: List[Path]) -> Optional[Snapshot]:
        """
        Archive `paths` before a destructive operation.

        Returns the Snapshot, or None when disabled / nothing was saved.
        """
        if not self.enabled:
            return None

        base_id = time.strftime("%Y%m%d_%H%M%S")
        snapshot_dir = self.base_path / base_id
        # Guard against two snapshots landing inside the same second.
        suffix = 0
        while snapshot_dir.exists():
            suffix += 1
            snapshot_dir = self.base_path / f"{base_id}_{suffix}"

        files_dir = snapshot_dir / "files"
        files_dir.mkdir(parents=True, exist_ok=True)

        entries: List[SnapshotEntry] = []
        for index, raw in enumerate(paths):
            path = Path(raw).expanduser()
            if not path.is_absolute():
                path = (Path.cwd() / path).resolve()

            if not path.is_file():
                logger.warning("snapshot_source_missing", path=str(path))
                continue

            stored_name = f"{index:04d}__{path.name}"
            dest = files_dir / stored_name
            shutil.copy2(path, dest)

            entries.append(
                SnapshotEntry(
                    original=path,
                    stored=f"files/{stored_name}",
                    size=dest.stat().st_size,
                )
            )
            logger.info("snapshot_file_copied", source=str(path), stored=stored_name)

        if not entries:
            shutil.rmtree(snapshot_dir, ignore_errors=True)
            logger.warning("snapshot_empty", requested=[str(p) for p in paths])
            return None

        snapshot = Snapshot(
            snapshot_id=snapshot_dir.name,
            timestamp=time.time(),
            entries=entries,
        )
        self._write_manifest(snapshot_dir, snapshot)
        self._cleanup_old_snapshots()

        logger.info(
            "snapshot_created",
            snapshot_id=snapshot.snapshot_id,
            files=len(entries),
            size_kb=round(snapshot.total_size / 1024, 1),
        )
        return snapshot

    # ── restore ────────────────────────────────────────────────

    def restore_snapshot(self, snapshot_id: str) -> bool:
        """Copy every archived file back to its recorded original path."""
        snapshot_dir = self.base_path / snapshot_id
        manifest = snapshot_dir / MANIFEST_NAME
        if not manifest.exists():
            logger.error("snapshot_not_found", snapshot_id=snapshot_id, dir=str(snapshot_dir))
            return False

        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            logger.error("snapshot_manifest_unreadable", snapshot_id=snapshot_id, error=str(e))
            return False

        restored = 0
        failed = 0
        for item in data.get("files", []):
            original = Path(item["original"])
            stored = snapshot_dir / item["stored"]
            if not stored.exists():
                logger.error("snapshot_archive_missing", snapshot_id=snapshot_id, stored=item["stored"])
                failed += 1
                continue
            try:
                original.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(stored, original)
                restored += 1
                logger.info("snapshot_file_restored", original=str(original))
            except OSError as e:
                logger.error("snapshot_restore_failed", original=str(original), error=str(e))
                failed += 1

        logger.info("snapshot_restored", snapshot_id=snapshot_id, restored=restored, failed=failed)
        return failed == 0 and restored > 0

    # ── listing / housekeeping ─────────────────────────────────

    def list_snapshots(self) -> List[Snapshot]:
        """Return known snapshots, newest first."""
        snapshots: List[Snapshot] = []
        for dir_path in sorted(self.base_path.iterdir(), reverse=True):
            if not dir_path.is_dir():
                continue
            manifest = dir_path / MANIFEST_NAME
            if not manifest.exists():
                continue
            try:
                data = json.loads(manifest.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            snapshots.append(
                Snapshot(
                    snapshot_id=dir_path.name,
                    timestamp=float(data.get("timestamp", dir_path.stat().st_mtime)),
                    entries=[
                        SnapshotEntry(
                            original=Path(f["original"]),
                            stored=f["stored"],
                            size=int(f.get("size", 0)),
                        )
                        for f in data.get("files", [])
                    ],
                )
            )
        return snapshots

    def _write_manifest(self, snapshot_dir: Path, snapshot: Snapshot) -> None:
        payload = {
            "version": MANIFEST_VERSION,
            "snapshot_id": snapshot.snapshot_id,
            "timestamp": snapshot.timestamp,
            "files": [
                {"original": str(e.original), "stored": e.stored, "size": e.size}
                for e in snapshot.entries
            ],
        }
        (snapshot_dir / MANIFEST_NAME).write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def _cleanup_old_snapshots(self) -> None:
        """Delete oldest snapshots once the total exceeds max_size_gb."""
        max_bytes = self.max_size_gb * 1024 * 1024 * 1024
        snapshots = self.list_snapshots()  # newest first
        total = sum(s.total_size for s in snapshots)
        if total <= max_bytes:
            return

        for snapshot in reversed(snapshots):  # oldest first
            if total <= max_bytes:
                break
            shutil.rmtree(self.base_path / snapshot.snapshot_id, ignore_errors=True)
            total -= snapshot.total_size
            logger.info("snapshot_cleaned_up", snapshot_id=snapshot.snapshot_id)


# ──────────────────────────────────────────────────────────────
# Singleton
# ──────────────────────────────────────────────────────────────

_snapshot_manager: Optional[SnapshotManager] = None


def get_snapshot_manager() -> SnapshotManager:
    global _snapshot_manager
    if _snapshot_manager is None:
        _snapshot_manager = SnapshotManager()
    return _snapshot_manager
