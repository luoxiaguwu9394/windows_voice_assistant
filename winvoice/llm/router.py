"""
LLM Router - combines local and remote backends with fallback logic.
"""

from __future__ import annotations

from typing import Optional

from winvoice.config import get_config
from winvoice.logging import get_logger
from winvoice.contracts import IntentName
from .local import LocalLlmBackend, LlmResponse
from .remote import RemoteLlmBackend

logger = get_logger(__name__)


class LlmRouter:
    """
    Routes requests: local first, fallback to remote on failure or low confidence.
    """

    def __init__(self):
        cfg = get_config()
        self.local = LocalLlmBackend()
        self.remote_enabled = cfg.get("llm.remote.enabled", False)
        self.remote = RemoteLlmBackend() if self.remote_enabled else None
        self.confidence_threshold = cfg.get("llm.local.confidence_threshold", 0.70)

    async def route(self, prompt: str) -> LlmResponse:
        """
        Try local first; if unavailable or confidence < threshold, try remote.
        """
        # Try local
        try:
            if await self.local.health_check():
                response = await self.local.complete(prompt)
                if response.confidence >= self.confidence_threshold:
                    return response
                logger.info("local_confidence_low", confidence=response.confidence, threshold=self.confidence_threshold)
            else:
                logger.warning("local_llm_unhealthy")
        except Exception as e:
            logger.warning("local_llm_failed", error=str(e))

        # Fallback to remote
        if self.remote and self.remote_enabled:
            try:
                if await self.remote.health_check():
                    return await self.remote.complete(prompt)
            except Exception as e:
                logger.error("remote_llm_failed", error=str(e))

        # Both failed
        return LlmResponse(
            intent=IntentName.UNKNOWN,
            args={},
            confidence=0.0,
            raw="",
            source="none",
        )

    async def close(self):
        await self.local.close()
        if self.remote:
            await self.remote.close()


def create_llm_router() -> LlmRouter:
    return LlmRouter()