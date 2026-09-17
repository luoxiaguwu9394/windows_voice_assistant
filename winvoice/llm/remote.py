"""
Remote LLM Backend (OpenAI-compatible API).
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

import httpx

from winvoice.config import get_config
from winvoice.logging import get_logger, observe_latency, inc_request
from winvoice.contracts import IntentName

logger = get_logger(__name__)


# ──────────────────────────────────────────────────────────────
# Data Classes
# ──────────────────────────────────────────────────────────────

@dataclass
class LlmResponse:
    intent: IntentName
    args: Dict[str, Any]
    confidence: float
    raw: str
    source: str  # "local" or "cloud"


# ──────────────────────────────────────────────────────────────
# Remote LLM Backend
# ──────────────────────────────────────────────────────────────

class RemoteLlmBackend:
    """Remote LLM via OpenAI-compatible API."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        grammar: str = "",  # not used for remote, uses JSON mode
    ):
        cfg = get_config()
        self.base_url = base_url or cfg.get("llm.remote.base_url")
        self.api_key = api_key or os.getenv("REMOTE_API_KEY") or cfg.get("llm.remote.api_key")
        self.model = model or cfg.get("llm.remote.model", "gpt-4o")
        self._client = httpx.AsyncClient(timeout=30.0)

        if not self.base_url:
            raise ValueError("Remote LLM base_url not configured")
        if not self.api_key:
            raise ValueError("Remote LLM api_key not configured")

    async def complete(self, prompt: str, grammar: Optional[str] = None) -> LlmResponse:
        start_time = time.perf_counter()

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": "You are an intent classifier. Output only valid JSON."},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.1,
            "max_tokens": 256,
            "response_format": {"type": "json_object"},  # OpenAI JSON mode
        }

        headers = {"Authorization": f"Bearer {self.api_key}"}

        try:
            response = await self._client.post(
                f"{self.base_url}/chat/completions",
                json=payload,
                headers=headers,
            )
            response.raise_for_status()
            data = response.json()
            content = data["choices"][0]["message"]["content"]

            latency = time.perf_counter() - start_time
            observe_latency("llm_remote", latency)
            inc_request("llm_remote", "success")

            return self._parse_response(content, "cloud")

        except httpx.TimeoutException:
            inc_request("llm_remote", "timeout")
            raise
        except Exception as e:
            inc_request("llm_remote", "error")
            logger.error("remote_llm_error", error=str(e))
            raise

    async def health_check(self) -> bool:
        try:
            headers = {"Authorization": f"Bearer {self.api_key}"}
            resp = await self._client.get(f"{self.base_url}/models", headers=headers, timeout=5.0)
            return resp.status_code == 200
        except Exception:
            return False

    def _parse_response(self, content: str, source: str) -> LlmResponse:
        try:
            parsed = json.loads(content)
            intent_str = parsed.get("intent", "unknown")
            intent = IntentName(intent_str) if intent_str in IntentName.__members__.values() else IntentName.UNKNOWN
            args = parsed.get("args", {})
            return LlmResponse(
                intent=intent,
                args=args,
                confidence=0.9,  # cloud model confidence estimate
                raw=content,
                source=source,
            )
        except json.JSONDecodeError:
            logger.warning("remote_llm_json_parse_failed", content=content[:200])
            return LlmResponse(
                intent=IntentName.UNKNOWN,
                args={},
                confidence=0.0,
                raw=content,
                source=source,
            )

    async def close(self):
        await self._client.aclose()


def create_remote_llm_backend() -> RemoteLlmBackend:
    return RemoteLlmBackend()