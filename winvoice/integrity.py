"""
Model integrity: a local seal over what was downloaded, checked at startup.

The download script verifies SHA256 *of the archives* when the MANIFEST pins a
hash — and only one entry does. Once extracted, nothing ever re-checked the
models: a truncated copy, a bad disk sector or a half-finished move silently
became 「the model is broken」 with no way to tell when it broke.

The seal is **trust-on-first-use**, by explicit choice: `--seal` fingerprints
the files as they exist on this machine (`models/integrity.json`), and every
later startup compares against it. That catches corruption, truncation and
accidental replacement after the fact; it cannot detect a bad download that
was sealed as-is — official archive hashes would need re-downloading
everything, which is recorded as the remaining gap in `UNIMPLEMENTED.md` §3.

Enforcement is report-only, deliberately: the spec's original sketch renamed
broken files to `.corrupt/`, which yanks files out from under a running
assistant and turns a diagnosable state into a missing one. A failed check
logs `model_integrity_failed` per file, and `python -m winvoice --check`
exits non-zero — the human decides what to do (re-download, restore, reseal).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from winvoice.logging import get_logger

logger = get_logger(__name__)

DEFAULT_MODELS_DIR = Path("models")
SEAL_FILE_NAME = "integrity.json"

# Not sealed: user-generated data and runtime scratch that are *supposed* to
# change (re-enrollment rewrites profiles; the KWS keyword cache is rebuilt
# from config; partial downloads are transient).
SEAL_EXCLUDE_DIRS = ("sv/profiles",)
SEAL_EXCLUDE_PATTERNS = ("winvoice_keywords_*.txt", "*.part", SEAL_FILE_NAME)


def sha256_of(path: Path, chunk_size: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def _is_excluded(rel: Path) -> bool:
    posix = rel.as_posix()
    if any(posix.startswith(d) for d in SEAL_EXCLUDE_DIRS):
        return True
    return any(rel.match(p) for p in SEAL_EXCLUDE_PATTERNS)


def seal_models(models_dir: Path = DEFAULT_MODELS_DIR, sub_dir: Optional[Path] = None) -> Optional[Path]:
    """
    Fingerprint the models tree into `models/integrity.json`.

    Without `sub_dir` the whole tree is re-fingerprinted (stale records for
    deleted files drop out). With `sub_dir` (e.g. `models/tts/...` from
    `--seal --model <key>`) only that subtree is re-fingerprinted and the rest
    of an existing seal is kept — so a re-downloaded model does not force
    resealing everything.
    """
    models_dir = Path(models_dir)
    seal_path = models_dir / SEAL_FILE_NAME
    sub_posix = sub_dir.as_posix() if sub_dir is not None else None

    files: Dict[str, Dict[str, object]] = {}
    if sub_posix is not None and seal_path.exists():
        for rel, record in (load_seal(models_dir) or {}).items():
            if not rel.startswith(sub_posix + "/"):
                files[rel] = record

    for path in sorted(models_dir.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(models_dir)
        if _is_excluded(rel):
            continue
        if sub_posix is not None and not rel.as_posix().startswith(sub_posix + "/"):
            continue
        files[rel.as_posix()] = {"sha256": sha256_of(path), "size": path.stat().st_size}

    if not files:
        return None

    payload = {
        "sealed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "models_dir": str(models_dir),
        "files": files,
    }
    seal_path.write_text(json.dumps(payload, indent=1, sort_keys=True), encoding="utf-8")
    logger.info("model_integrity_sealed", files=len(files), path=str(seal_path))
    return seal_path


@dataclass
class IntegrityReport:
    """What one verification pass saw, grouped the way --check prints it."""

    #: False until a seal was actually loaded and checked — an unchecked
    #: report must never read as healthy.
    sealed: bool = False
    checked: int = 0
    #: `{"path", "reason", "expected", "actual"}` — reason is missing|size|sha256.
    failures: List[Dict[str, object]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.sealed and not self.failures


def load_seal(models_dir: Path = DEFAULT_MODELS_DIR) -> Optional[Dict[str, Dict[str, object]]]:
    """The sealed file records, or None when the repo has never been sealed."""
    seal_path = Path(models_dir) / SEAL_FILE_NAME
    if not seal_path.exists():
        return None
    try:
        payload = json.loads(seal_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("model_integrity_seal_unreadable", error=str(e))
        return None
    files = payload.get("files")
    return files if isinstance(files, dict) else None


def verify_sealed_models(
    models_dir: Path = DEFAULT_MODELS_DIR, deep: bool = False
) -> IntegrityReport:
    """
    Compare the models tree against the seal.

    Quick mode (`deep=False`, the startup path) compares **sizes only** — it
    is a stat per file, catches truncation and wholesale replacement, and
    never hashes gigabytes while the microphone waits. Deep mode (`--check`)
    hashes everything.
    """
    report = IntegrityReport()
    records = load_seal(models_dir)
    if records is None:
        report.sealed = False
        logger.info("model_integrity_unsealed", hint="python scripts/download_models.py --seal")
        return report
    report.sealed = True

    models_dir = Path(models_dir)
    for rel, record in sorted(records.items()):
        report.checked += 1
        path = models_dir / rel
        expected = record.get("sha256")
        expected_size = record.get("size")

        if not path.exists():
            report.failures.append(
                {"path": rel, "reason": "missing", "expected": expected_size, "actual": None}
            )
            logger.warning("model_integrity_failed", path=rel, reason="missing")
            continue

        actual_size = path.stat().st_size
        if actual_size != expected_size:
            if not deep:
                # Quick path (startup): a stat is the whole verdict — report
                # and move on, never hash gigabytes while the mic waits.
                report.failures.append(
                    {"path": rel, "reason": "size", "expected": expected_size, "actual": actual_size}
                )
                logger.warning(
                    "model_integrity_failed", path=rel, reason="size",
                    expected=expected_size, actual=actual_size,
                )
                continue
            # Deep path (--check): hash for the record — "the bytes changed"
            # is more actionable than "the size changed".
            actual_hash = sha256_of(path)
            if actual_hash == expected:
                continue  # bytes intact; only the seal's size field is stale
            report.failures.append(
                {"path": rel, "reason": "sha256", "expected": expected, "actual": actual_hash}
            )
            logger.warning(
                "model_integrity_failed", path=rel, reason="sha256",
                expected=expected, actual=actual_hash,
            )
            continue

        if deep:
            actual_hash = sha256_of(path)
            if actual_hash != expected:
                report.failures.append(
                    {"path": rel, "reason": "sha256", "expected": expected, "actual": actual_hash}
                )
                logger.warning(
                    "model_integrity_failed", path=rel, reason="sha256",
                    expected=expected, actual=actual_hash,
                )

    if report.ok:
        logger.info("model_integrity_ok", files=report.checked, deep=deep)
    return report


def failures_by_model(report: IntegrityReport) -> Dict[str, List[Dict[str, object]]]:
    """Group failures by their model directory (`models/tts/...` → `tts/...`)."""
    grouped: Dict[str, List[Dict[str, object]]] = {}
    for failure in report.failures:
        parts = str(failure["path"]).split("/")
        key = "/".join(parts[:-1]) if len(parts) > 1 else "."
        grouped.setdefault(key, []).append(failure)
    return grouped
