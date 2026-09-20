"""
Rule tier → Local DSH → Cloud DSH, with escalation decided by evidence.

The shape `new_way.md` asks for, and the reason it asks for it:

    「不能单纯因为"本地模型回答得像胡说八道"就凭感觉切云端。
      最好给本地 Agent 一个明确的"是否可信"的判定机制。」

So the decision to escalate is made here, in Python, from
`validation.assess_turn` — which reads the tool results the machine produced.
The local model is never asked whether it is confident.

What escalation costs, and why the predicate is deliberately narrow:

* `VERIFIED` never escalates, **even when the tool reported failure**. `close_app`
  on an app that was not running leaves the postcondition satisfied; escalating
  that to the cloud would pay a round trip to re-derive 「它没有在运行」.
* `UNCERTAIN` / `NOT_VERIFIABLE` never escalate. "My layer cannot observe this"
  is not evidence of failure, and escalating on it would send every web search
  and every media keypress to the cloud.
* A tool that failed with **no** verification verdict is a deterministic refusal
  (an app outside the allowlist, a destructive action awaiting the confirmation
  flow that does not exist yet). The cloud model is refused identically, so this
  does not escalate either — it is reported honestly instead.

Only an observed state mismatch, a turn that ended abnormally, or an outright
backend failure escalates.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from winvoice.logging import get_logger
from winvoice.tools import utterance
from .backend import DSHBackend, build_backend
from .settings import DSHSettings
from .validation import TurnAssessment, assess_turn

logger = get_logger(__name__)


@dataclass
class RoutedRequest:
    """The outcome of the whole three-tier decision."""

    resolved: bool
    response: str = ""
    source: str = ""
    escalation_reason: Optional[str] = None
    attempts: int = 0
    assessment: Optional[TurnAssessment] = None
    #: Present when nothing succeeded, so the caller can say something honest
    #: instead of a bare 「好的」.
    failure: Optional[str] = None


def build_escalation_context(
    user_request: str,
    assessment: TurnAssessment,
    reason: str,
    backend_name: str,
) -> str:
    """
    What the cloud agent is told about the local attempt.

    `new_way.md`: 「Escalation Must Preserve Context … Avoid unnecessarily
    repeating successful operations.」 Without this the cloud agent starts from
    zero and re-runs the half of the plan that already worked — which for a
    filesystem task means doing it twice.

    Written as a compact brief rather than the raw event log: the events are
    large, model-specific, and mostly irrelevant, while the tool outcomes are
    exactly what the next agent needs.
    """
    lines = [
        "A local model already attempted this request and did not complete it.",
        "Continue from where it stopped. Do not repeat steps that already succeeded.",
        "",
        f"User request: {user_request}",
        f"Local agent: {backend_name}",
        f"Escalation reason: {reason}",
    ]

    if assessment.tool_activity:
        lines.append("")
        lines.append("What the local attempt did:")
        for i, activity in enumerate(assessment.tool_activity, 1):
            outcome = "succeeded" if activity.success else "FAILED"
            lines.append(f"  {i}. tool={activity.name or 'unknown'} -> {outcome}")
            if activity.error:
                lines.append(f"     error: {activity.error}")
            if activity.verification_status:
                lines.append(
                    f"     machine check: {activity.verification_status}"
                    + (f" ({activity.verification_reason})" if activity.verification_reason else "")
                )
    else:
        lines.append("")
        lines.append("The local attempt produced no tool results.")

    if assessment.final_response:
        lines.append("")
        lines.append(f"Local agent's attempted answer: {assessment.final_response}")

    lines += [
        "",
        "Complete the request. Reply in plain Chinese only, one short spoken",
        "sentence, with no English words and no formatting.",
    ]
    return "\n".join(lines)


class DSHRouter:
    """Owns both model tiers and the escalation policy between them."""

    def __init__(
        self,
        settings: DSHSettings,
        local: Optional[DSHBackend] = None,
        cloud: Optional[DSHBackend] = None,
    ):
        self.settings = settings
        self.local = local if local is not None else build_backend("local", settings.local)
        self.cloud = cloud if cloud is not None else build_backend("cloud", settings.cloud)

    @property
    def enabled(self) -> bool:
        return self.settings.enabled and self.settings.local.enabled

    async def route(self, text: str, trace_id: str = "", tier: str = "full") -> RoutedRequest:
        """
        Run one request through the tiers.

        The tier is published before the attempt so the MCP server — which runs
        in DSH's own child process and therefore cannot be handed a Python
        argument — can enforce it when the agent calls a tool.
        """
        if not self.enabled:
            return RoutedRequest(
                resolved=False,
                failure="dsh_disabled",
                source="none",
            )

        session_id = trace_id or "winvoice-session"
        utterance.publish(
            trace_id=session_id,
            tier=tier,
            path=self.settings.bridge.utterance_file,
        )

        try:
            return await self._attempt_tiers(text, session_id, tier)
        finally:
            # The record describes the turn that just ended. Leaving it behind
            # would let a stale `full` outlive its utterance for the length of
            # its TTL — exactly the window a guest's next command arrives in.
            # The TTL is the backstop, not the mechanism.
            utterance.clear(path=self.settings.bridge.utterance_file)

    async def _attempt_tiers(self, text: str, session_id: str, tier: str) -> RoutedRequest:
        reason: Optional[str] = None
        assessment: Optional[TurnAssessment] = None
        attempts = 0

        for attempt in range(1, max(1, self.settings.max_local_attempts) + 1):
            attempts = attempt
            turn = await self.local.run(text, session_id=session_id)

            if not turn.ran:
                reason = turn.failure or "backend_failure"
                logger.warning("dsh_local_unavailable", reason=reason, attempt=attempt)
                break  # a broken backend does not improve on a retry

            assessment = assess_turn(turn.final_response, turn.finish_reason, turn.events)
            if assessment.resolved:
                logger.info("dsh_resolved_locally", attempt=attempt)
                return RoutedRequest(
                    resolved=True,
                    response=assessment.final_response,
                    source="local",
                    attempts=attempt,
                    assessment=assessment,
                )

            reason = assessment.reason
            logger.info(
                "dsh_local_unresolved",
                reason=reason,
                retryable=assessment.retryable,
                attempt=attempt,
            )
            if not assessment.retryable:
                break

        # ── escalate ───────────────────────────────────────────
        if not self.settings.escalation_enabled or not self.settings.cloud.enabled:
            return RoutedRequest(
                resolved=False,
                source="local",
                escalation_reason=reason,
                attempts=attempts,
                assessment=assessment,
                failure=reason,
            )

        # `new_way.md`: 「The cloud model must NOT automatically receive broader
        # permissions simply because it is more capable.」 The cloud tier is gated
        # on the same speaker tier as everything else — a guest does not reach it
        # unless `dsh.cloud.guest_allowed` says so, exactly like
        # `llm.remote.guest_allowed` gates the legacy cloud path.
        if tier != "full" and not self.settings.cloud.guest_allowed:
            logger.info("dsh_escalation_denied_for_tier", tier=tier, reason=reason)
            return RoutedRequest(
                resolved=False,
                source="local",
                escalation_reason=reason,
                attempts=attempts,
                assessment=assessment,
                failure=reason,
            )

        prompt = text
        if assessment is not None:
            prompt = build_escalation_context(text, assessment, reason or "unknown", self.local.name)

        logger.info("dsh_escalating", reason=reason, from_backend=self.local.name)
        cloud_turn = await self.cloud.run(prompt, session_id=f"{session_id}-cloud")

        if not cloud_turn.ran:
            logger.error("dsh_cloud_failed", failure=cloud_turn.failure)
            return RoutedRequest(
                resolved=False,
                source="cloud",
                escalation_reason=reason,
                attempts=attempts,
                assessment=assessment,
                failure=cloud_turn.failure,
            )

        cloud_assessment = assess_turn(
            cloud_turn.final_response, cloud_turn.finish_reason, cloud_turn.events
        )
        return RoutedRequest(
            resolved=cloud_assessment.resolved,
            response=cloud_assessment.final_response,
            source="cloud",
            escalation_reason=reason,
            attempts=attempts,
            assessment=cloud_assessment,
            failure=None if cloud_assessment.resolved else cloud_assessment.reason,
        )

    async def close(self) -> None:
        await self.local.close()
        await self.cloud.close()


__all__ = ["DSHRouter", "RoutedRequest", "build_escalation_context"]
