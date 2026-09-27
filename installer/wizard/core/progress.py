"""
Parse the machine progress protocol of `download_models.py --progress-fmt machine`.

The wizard streams the download subprocess's output line by line: human-readable
lines go to the log box untouched, tab-separated protocol lines become events.
Anything unparseable is forwarded as a plain line — the protocol must never be
the reason a download looks stuck.
"""

from __future__ import annotations

from typing import Optional

PROTOCOL_PREFIXES = ("MODEL\t", "PROGRESS\t", "RESULT\t")


class DownloadEvents:
    """Callbacks for the three protocol events; default implementations no-op."""

    def on_model(self, key: str, note: str) -> None:  # pragma: no cover - default no-op
        pass

    def on_progress(self, key: str, done: int, total: int) -> None:  # pragma: no cover
        pass

    def on_result(self, key: str, ok: bool, detail: str) -> None:  # pragma: no cover
        pass


def feed_line(line: str, events: DownloadEvents) -> bool:
    """
    Consume one output line. Returns True when the line was protocol.
    Malformed protocol lines (wrong field counts, non-numeric bytes) are
    reported as `result(key, False, reason)` so a failure can never be silent.
    """
    stripped = line.strip()
    if stripped.startswith("MODEL\t"):
        parts = stripped.split("\t")
        if len(parts) >= 2:
            events.on_model(parts[1], parts[2] if len(parts) > 2 else "")
            return True
        return False
    if stripped.startswith("PROGRESS\t"):
        parts = stripped.split("\t")
        if len(parts) >= 4:
            try:
                events.on_progress(parts[1], int(parts[2]), int(parts[3]))
                return True
            except ValueError:
                pass
        return False
    if stripped.startswith("RESULT\t"):
        parts = stripped.split("\t")
        if len(parts) >= 3:
            ok = parts[2].strip().lower() == "ok"
            events.on_result(parts[1], ok, parts[3] if len(parts) > 3 else "")
            return True
        return False
    return False


def failures(results: dict[str, tuple[bool, str]], expected_keys: list[str]) -> list[str]:
    """Manifest keys that reported failure — or never reported at all."""
    failed = [k for k, (ok, _) in results.items() if not ok]
    missing = [k for k in expected_keys if k not in results]
    return failed + missing


def parse_probe_result(line: str) -> Optional[dict]:
    """
    Extract the JSON payload of an `audio_probe.py` RESULT line.
    Returns None for anything that is not a well-formed probe line.
    """
    import json

    stripped = line.strip()
    if not stripped.startswith("RESULT "):
        return None
    try:
        payload = json.loads(stripped[len("RESULT ") :])
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


__all__ = [
    "DownloadEvents",
    "PROTOCOL_PREFIXES",
    "failures",
    "feed_line",
    "parse_probe_result",
]
