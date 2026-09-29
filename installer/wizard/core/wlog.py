"""
A tiny append-only JSONL log for the wizard.

The wizard deliberately swallows most of its failures — a failed update check
is a banner that never appears, never an error dialog. That silence is good
UX and terrible diagnosability, so the few steps worth remembering (update
checks, downloads, hash verdicts) append one JSON line each to
`%LOCALAPPDATA%\\WinVoice\\logs\\setup-wizard.jsonl`, inside the default
install directory that a full uninstall already removes.

Logging must never break the flow it observes: `log_event` swallows every
exception on purpose. Core modules stay log-free — they hand reasons out via
callbacks/exceptions and it is the UI layer that calls this.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any


def log_path() -> Path:
    return (
        Path(os.environ.get("LOCALAPPDATA") or Path.home())
        / "WinVoice" / "logs" / "setup-wizard.jsonl"
    )


def log_event(event: str, **fields: Any) -> None:
    """Append one JSON line; never raises."""
    record = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": event, **fields}
    try:
        path = log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass
