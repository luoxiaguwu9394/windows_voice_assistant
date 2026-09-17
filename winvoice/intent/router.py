"""
Intent Router (Three-Tier Waterfall).

Combines rules, local classifier, and cloud fallback.
Respects speaker tier for cloud access.
"""

from __future__ import annotations

from typing import Optional

from winvoice.config import get_config
from winvoice.logging import get_logger
from winvoice.contracts import IntentResult, IntentName, SpeakerTier, SvResult
from .rules import match_rules
from .classifier import IntentClassifier
from winvoice.llm.router import LlmRouter

logger = get_logger(__name__)


class IntentRouter:
    """
    Three-tier intent routing:
    1. Rules (regex) - zero latency
    2. Local LLM (constrained decoding) - ~200-500ms
    3. Cloud LLM - ~1-3s

    Respects speaker tier: Guest cannot trigger cloud.
    """

    def __init__(self):
        self.classifier = IntentClassifier()
        self.llm_router = LlmRouter()
        self.confidence_threshold = get_config().get("llm.local.confidence_threshold", 0.70)

    async def route(self, text: str, sv_result: Optional[SvResult] = None) -> IntentResult:
        """
        Route text through three tiers.

        Args:
            text: User utterance from ASR
            sv_result: Speaker verification result (for tier checks)

        Returns:
            IntentResult with intent, args, confidence, source
        """
        trace_id = getattr(self, "_current_trace_id", "")

        # Tier 1: Rules
        rule_result = match_rules(text)
        if rule_result:
            rule_result.trace_id = trace_id
            rule_result.raw_text = text
            logger.info("intent_rule_matched", intent=rule_result.intent.value, confidence=rule_result.confidence)
            return rule_result

        # Tier 2: Local LLM
        try:
            if await self.classifier.health_check():
                classifier_result = await self.classifier.classify(text)
                if classifier_result.confidence >= self.confidence_threshold:
                    return IntentResult(
                        trace_id=trace_id,
                        intent=classifier_result.intent,
                        args=classifier_result.args,
                        confidence=classifier_result.confidence,
                        source="local",
                        raw_text=text,
                    )
                logger.info("local_confidence_low", confidence=classifier_result.confidence, threshold=self.confidence_threshold)
            else:
                logger.warning("local_classifier_unhealthy")
        except Exception as e:
            logger.warning("local_classifier_failed", error=str(e))

        # Tier 3: Cloud (if allowed)
        tier = sv_result.tier if sv_result else "full"
        if tier == "full" and self.llm_router.remote_enabled:
            logger.info("escalating_to_cloud")
            try:
                cloud_result = await self.llm_router.remote.complete(self._build_prompt(text))
                return IntentResult(
                    trace_id=trace_id,
                    intent=cloud_result.intent,
                    args=cloud_result.args,
                    confidence=cloud_result.confidence,
                    source="cloud",
                    needs_cloud=True,
                    raw_text=text,
                )
            except Exception as e:
                logger.error("cloud_llm_failed", error=str(e))

        # Both failed or cloud not allowed
        logger.info("intent_low_confidence_no_cloud", tier=tier)
        return IntentResult(
            trace_id=trace_id,
            intent=IntentName.UNKNOWN,
            args={},
            confidence=0.0,
            source="local",
            needs_cloud=True,
            raw_text=text,
        )

    def _build_prompt(self, text: str) -> str:
        intents = [i.value for i in IntentName if i != IntentName.UNKNOWN]
        return f"""Classify the user's request into one of these intents: {', '.join(intents)}

User: "{text}"

Output JSON only with 'intent' and 'args' fields."""  # noqa: E501

    async def close(self):
        await self.classifier.close()
        await self.llm_router.close()


def create_intent_router() -> IntentRouter:
    return IntentRouter()