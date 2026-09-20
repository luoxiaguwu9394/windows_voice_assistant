"""
Which utterance is currently in flight, and who is speaking it.

The speaker tier has to reach the tool layer, and since the DeepSeek Harness
integration the tool layer is no longer always in this process: DSH spawns
`python -m winvoice.mcp_server` and calls tools over MCP, so a plain function
argument cannot carry the tier across (`UNIMPLEMENTED.md` §1.1 is the original
gap; DSH is what makes it unavoidable).

The pipeline therefore publishes one small record per utterance — trace id, tier
and an expiry — and whichever process is about to run a tool reads it back. This
is a *coarse* mechanism on purpose: it is not a capability token, it only answers
"is this turn a guest's?", and it expires so a crashed turn cannot leave a stale
`full` lying around.

Fail-open, stated plainly: when no record exists the tier is `full`, which is
exactly today's behaviour and what the single-user desktop is configured for. An
absent record means no utterance is in flight — the window between turns — so
there is nothing to protect. A record that is present but expired is treated the
same way, because the turn it described is over.
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from winvoice.logging import get_logger

logger = get_logger(__name__)

#: Where the in-flight utterance is published.
DEFAULT_PATH = "runtime/utterance.json"

#: Generous relative to a turn (LLM + tools), short enough that a crashed turn
#: cannot authorise the next one.
DEFAULT_TTL_S = 120.0


@dataclass(frozen=True)
class UtteranceContext:
    trace_id: str
    tier: str
    expires_at: float


def _resolve(path: Optional[str | Path] = None) -> Path:
    return Path(path or DEFAULT_PATH)


def publish(
    trace_id: str,
    tier: str,
    ttl_s: float = DEFAULT_TTL_S,
    path: Optional[str | Path] = None,
) -> None:
    """
    Record the utterance that is starting. Best effort: never raises.

    A failure to publish degrades the guest check to fail-open rather than
    breaking the voice pipeline, and says so in the log — a security control
    that silently stops working is worse than one that never existed.
    """
    target = _resolve(path)
    payload = {
        "trace_id": trace_id,
        "tier": tier,
        "expires_at": time.time() + ttl_s,
    }
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload), encoding="utf-8")
    except OSError as exc:
        logger.warning("utterance_publish_failed", error=str(exc), path=str(target))


def clear(path: Optional[str | Path] = None) -> None:
    """Forget the in-flight utterance (called when the turn ends)."""
    with contextlib.suppress(OSError):
        _resolve(path).unlink(missing_ok=True)


def current(
    now: Optional[float] = None,
    path: Optional[str | Path] = None,
) -> Optional[UtteranceContext]:
    """The live utterance record, or None when there is none (or it expired)."""
    target = _resolve(path)
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None

    if not isinstance(raw, dict):
        return None

    tier = raw.get("tier")
    expires_at = raw.get("expires_at")
    if not isinstance(tier, str) or not isinstance(expires_at, (int, float)):
        return None

    if (time.time() if now is None else now) >= float(expires_at):
        return None

    return UtteranceContext(
        trace_id=str(raw.get("trace_id", "")),
        tier=tier,
        expires_at=float(expires_at),
    )


def read_policy_path() -> str:
    """Config-resolved path, falling back to the default when unconfigured."""
    try:
        from winvoice.config import get_config

        configured = get_config().get("tools.utterance_file")
        return str(configured) if configured else DEFAULT_PATH
    except Exception:  # config not loaded (unit tests, standalone tools)
        return DEFAULT_PATH


def resolve_from_config() -> str:
    """`read_policy_path`, but honouring an explicit override env var.

    The MCP server is spawned by DSH, which does not know about this project's
    config file layout, so the path can also be pinned through the environment.
    """
    override = os.environ.get("WINVOICE_UTTERANCE_FILE")
    return override or read_policy_path()


__all__ = [
    "DEFAULT_PATH",
    "DEFAULT_TTL_S",
    "UtteranceContext",
    "clear",
    "current",
    "publish",
    "read_policy_path",
    "resolve_from_config",
]
