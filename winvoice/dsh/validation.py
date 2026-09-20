"""
Deciding whether a local agent turn actually succeeded — from evidence.

`new_way.md` is emphatic that the local model must not be trusted to grade its
own homework:

    「尤其是不要让本地模型自己说"我很确定"然后决定自己是否可信。
      应该由你的程序检查它的输出和实际 Tool Result。」

and

    「Never treat: LLM says "Done!" as proof that the operation succeeded.」

So this module reads the *events* a turn produced. It never asks the model how
it felt about them, and it never inspects the model's prose for confidence words
— a small model that hallucinates success is equally fluent when it hallucinates
certainty.

**On parsing tolerance.** The exact envelope DSH wraps tool results in is part of
a pre-release surface (0.1.5rc1) that this project cannot pin by reading alone,
and a parser that hard-codes a guessed shape would silently stop detecting
failures — the worst possible failure mode for a safety check, because everything
would look like a success. Instead of guessing the envelope, the extractor walks
the event tree for JSON text and recognises *our own* payload, which
`winvoice/mcp_server.py:render_tool_result` authored and therefore defines: it
always contains `success`, and carries `speak` / `error` / `verification` when
they exist. That contract is ours, stable, and tested from both ends.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from winvoice.logging import get_logger

logger = get_logger(__name__)

#: Beyond this depth the extractor stops descending. Session events nest, and
#: an unbounded walk over a large event list is a latency problem on the voice
#: path (the pipeline waits for this before it can speak).
_MAX_DEPTH = 8

#: Cap on candidate strings inspected per turn, for the same reason.
_MAX_STRINGS = 400


@dataclass(frozen=True)
class ToolActivity:
    """One tool call the agent made, as far as its result revealed."""

    name: str = ""
    success: Optional[bool] = None
    speak: Optional[str] = None
    error: Optional[str] = None
    verification_status: Optional[str] = None
    verification_reason: Optional[str] = None
    retryable: bool = False

    @property
    def verification_failed(self) -> bool:
        return self.verification_status == "failed"

    @property
    def unresolved(self) -> bool:
        """
        True when this call leaves the task genuinely unfinished.

        Mirrors `winvoice/tools/verifier.py:unresolved`: only an *observed
        mismatch* counts. A tool that failed with no verification verdict is a
        deterministic refusal (an app outside the allowlist, a destructive
        action with no confirmation path) — the cloud model would be refused
        identically, so escalating it only burns a round trip and adds latency
        to a request that was never going to succeed.
        """
        return self.verification_failed


@dataclass
class TurnAssessment:
    """What the events say about one turn, and why it did or did not resolve."""

    resolved: bool
    reason: Optional[str] = None
    final_response: str = ""
    tool_activity: List[ToolActivity] = field(default_factory=list)
    retryable: bool = False

    @property
    def verification_failures(self) -> List[ToolActivity]:
        return [a for a in self.tool_activity if a.verification_failed]


def _iter_strings(node: Any, depth: int = 0, budget: Optional[List[int]] = None):
    """Yield every string in a nested event structure, depth- and count-bounded."""
    if budget is None:
        budget = [_MAX_STRINGS]
    if budget[0] <= 0 or depth > _MAX_DEPTH:
        return
    if isinstance(node, str):
        budget[0] -= 1
        yield node
    elif isinstance(node, dict):
        for value in node.values():
            yield from _iter_strings(value, depth + 1, budget)
    elif isinstance(node, (list, tuple)):
        for value in node:
            yield from _iter_strings(value, depth + 1, budget)


def _as_payload(text: str) -> Optional[Dict[str, Any]]:
    """
    `text` as a tool-result payload, or None.

    The `success` key is the discriminator: it is what our renderer always
    emits, and requiring it keeps ordinary prose (or a filename that happens to
    be JSON) from being mistaken for a tool result.
    """
    stripped = text.strip()
    if not stripped.startswith("{") or '"success"' not in stripped:
        return None
    try:
        parsed = json.loads(stripped)
    except (ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) and "success" in parsed else None


def extract_tool_activity(events: Any) -> List[ToolActivity]:
    """Every tool result visible in `events`, in order, de-duplicated."""
    activities: List[ToolActivity] = []
    seen: set = set()

    for raw in _iter_strings(events):
        payload = _as_payload(raw)
        if payload is None:
            continue

        key = raw.strip()
        if key in seen:
            continue
        seen.add(key)

        verification = payload.get("verification")
        status = reason = None
        retryable = False
        if isinstance(verification, dict):
            status = verification.get("status")
            reason = verification.get("reason")
            retryable = bool(verification.get("retryable"))

        activities.append(
            ToolActivity(
                name=str(payload.get("tool") or payload.get("name") or ""),
                success=payload.get("success") if isinstance(payload.get("success"), bool) else None,
                speak=payload.get("speak") if isinstance(payload.get("speak"), str) else None,
                error=payload.get("error") if isinstance(payload.get("error"), str) else None,
                verification_status=status if isinstance(status, str) else None,
                verification_reason=reason if isinstance(reason, str) else None,
                retryable=retryable,
            )
        )

    return activities


def assess_turn(
    final_response: str,
    finish_reason: Optional[str],
    events: Any,
) -> TurnAssessment:
    """
    Judge one local turn against the evidence it produced.

    The order of the checks is the order of trustworthiness: an observed state
    mismatch outranks a truncated turn, which outranks an empty answer.
    """
    activities = extract_tool_activity(events)

    failed = [a for a in activities if a.verification_failed]
    if failed:
        # Carried into the escalation context, not shown to the user: the user
        # hears the tool's own Chinese sentence, which already says what went
        # wrong in words the TTS can pronounce.
        return TurnAssessment(
            resolved=False,
            reason="verification_failed",
            final_response=final_response,
            tool_activity=activities,
            retryable=any(a.retryable for a in failed),
        )

    # A turn that ended for any reason other than `completed` did not finish
    # reasoning; `max-tokens` in particular yields a plausible half-sentence.
    if finish_reason is not None and finish_reason != "completed":
        return TurnAssessment(
            resolved=False,
            reason=f"turn_ended_{finish_reason}",
            final_response=final_response,
            tool_activity=activities,
            retryable=finish_reason == "max-tokens",
        )

    if not final_response.strip():
        return TurnAssessment(
            resolved=False,
            reason="empty_response",
            final_response=final_response,
            tool_activity=activities,
            retryable=True,
        )

    return TurnAssessment(
        resolved=True,
        final_response=final_response,
        tool_activity=activities,
    )


__all__ = ["ToolActivity", "TurnAssessment", "assess_turn", "extract_tool_activity"]
