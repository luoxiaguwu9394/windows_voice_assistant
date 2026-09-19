"""
Local Intent Classifier (Tier 2).

Thin wrapper over `LocalLlmBackend`: the transport, grammar and response
normalisation all live in `winvoice.llm`, so there is a single definition of
the constrained-decoding contract rather than two that can drift apart.

The classifier only emits an intent label plus structured args; it never
selects tools.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

from winvoice.config import get_config
from winvoice.contracts import IntentName
from winvoice.llm.grammar import INTENT_GRAMMAR, build_intent_prompt
from winvoice.llm.local import LocalLlmBackend
from winvoice.logging import get_logger

logger = get_logger(__name__)

__all__ = ["ClassifierResult", "IntentClassifier", "create_intent_classifier", "INTENT_GRAMMAR"]


@dataclass
class ClassifierResult:
    intent: IntentName
    args: Dict[str, Any]
    confidence: float
    raw: str


class IntentClassifier:
    """
    Classify an utterance into an intent using the local model.

    Requires llama-server, e.g.:

        llama-server -m models/llm/qwen2.5-3b-instruct-q4_k_m.gguf --port 8080 -c 4096
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        grammar: str = INTENT_GRAMMAR,
    ):
        cfg = get_config()
        self._backend = LocalLlmBackend(
            base_url=base_url or cfg.get("llm.local.base_url"),
            model=model or cfg.get("llm.local.model"),
            grammar=grammar,
        )

    # Expose the transport knobs the router/health checks look at.
    @property
    def base_url(self) -> str:
        return self._backend.base_url

    @property
    def model(self) -> str:
        return self._backend.model

    async def classify(self, text: str) -> ClassifierResult:
        """Classify `text`, returning the intent and its normalised args."""
        response = await self._backend.complete(build_intent_prompt(text))
        return ClassifierResult(
            intent=response.intent,
            args=response.args,
            confidence=response.confidence,
            raw=response.raw,
        )

    async def health_check(self) -> bool:
        return await self._backend.health_check()

    async def close(self) -> None:
        await self._backend.close()


def create_intent_classifier() -> IntentClassifier:
    return IntentClassifier()
