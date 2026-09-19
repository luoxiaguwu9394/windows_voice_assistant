"""
Local LLM backend — llama.cpp `llama-server` over its OpenAI-compatible API.

Structured output is requested two ways, in order of preference:

1. `grammar` (GBNF) in the request body — a llama.cpp extension, so no server
   flag is needed: the b7376 CPU build accepts it per-request even when
   started without `-mgf`.
2. `response_format: {"type": "json_object"}` — used when the server rejects
   the grammar (`400 Failed to parse grammar`), so a grammar/build mismatch
   degrades instead of taking the whole assistant down.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

import httpx

from winvoice.config import get_config
from winvoice.contracts import IntentName
from winvoice.logging import get_logger, inc_request, observe_latency
from .grammar import INTENT_GRAMMAR, build_intent_prompt, parse_intent_json

logger = get_logger(__name__)

# Models known to produce usable structured output for intent classification.
# Below ~1.5B the argument keys drift badly.
ALLOWED_MODELS = {
    "qwen2.5-0.5b-instruct",
    "qwen2.5-1.5b-instruct",
    "qwen2.5-3b-instruct",
    "qwen2.5-7b-instruct",
    "qwen2.5-14b-instruct",
    "qwen2.5-32b-instruct",
    "qwen2.5:3b-instruct",
    "qwen2.5:7b-instruct",
    "phi-3-mini-4k-instruct",
    "phi-3.5-mini-instruct",
    "gemma-2-2b-instruct",
    "gemma-2-9b-instruct",
    "llama-3.2-1b-instruct",
    "llama-3.2-3b-instruct",
    "llama-3.1-8b-instruct",
}


@dataclass
class LlmResponse:
    intent: IntentName
    args: Dict[str, Any]
    confidence: float
    raw: str
    source: str = "local"


class LocalLlmBackend:
    """
    Local model behind `llama-server`.

    Start the server with:

        llama-server -m models/llm/qwen2.5-3b-instruct-q4_k_m.gguf --port 8080 -c 4096
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        grammar: str = INTENT_GRAMMAR,
        timeout: float = 60.0,
    ):
        cfg = get_config()
        self.base_url = (
            base_url or cfg.get("llm.local.base_url", "http://localhost:8080/v1")
        ).rstrip("/")
        self.model = model or cfg.get("llm.local.model", "qwen2.5-3b-instruct")
        self.grammar = grammar
        self._client = httpx.AsyncClient(timeout=timeout)
        # Set once the server rejects a grammar, so we stop retrying it.
        self._grammar_supported: Optional[bool] = None

    # ── public API ─────────────────────────────────────────────

    async def complete(self, prompt: str, grammar: Optional[str] = None) -> LlmResponse:
        """
        Classify into an intent.

        `prompt` may be a bare utterance or an already-built prompt; a bare
        utterance is wrapped with the argument schema.
        """
        if self.model not in ALLOWED_MODELS:
            logger.warning("local_model_not_in_allowlist", model=self.model)

        if "Intents and their argument objects" not in prompt:
            prompt = build_intent_prompt(prompt)

        start = time.perf_counter()
        content = await self._chat(prompt, grammar or self.grammar)
        latency = time.perf_counter() - start

        observe_latency("llm_local", latency)
        inc_request("llm_local", "success")

        intent, args, confidence = parse_intent_json(content)
        logger.info(
            "local_llm_result",
            intent=intent.value,
            args=args,
            latency_ms=int(latency * 1000),
        )
        return LlmResponse(
            intent=intent,
            args=args,
            confidence=confidence,
            raw=content,
            source="local",
        )

    async def classify_text(self, text: str) -> LlmResponse:
        """Convenience wrapper for a raw user utterance."""
        return await self.complete(build_intent_prompt(text))

    async def health_check(self) -> bool:
        """True when llama-server answers on the configured base_url."""
        for path in ("/models", "/health"):
            try:
                resp = await self._client.get(f"{self.base_url}{path}", timeout=5.0)
                if resp.status_code < 500:
                    return True
            except Exception:
                continue
        return False

    async def close(self) -> None:
        await self._client.aclose()

    # ── transport ──────────────────────────────────────────────

    async def _chat(self, prompt: str, grammar: Optional[str]) -> str:
        """POST the chat request, degrading gracefully if the grammar is refused."""
        messages = [
            {
                "role": "system",
                "content": (
                    "You are an intent classifier. Reply with a single JSON object "
                    "matching the required schema. No prose, no markdown."
                ),
            },
            {"role": "user", "content": prompt},
        ]

        attempts = []
        if grammar and self._grammar_supported is not False:
            attempts.append("grammar")
        attempts.append("json_object")

        last_status = None
        last_body = ""

        for attempt in attempts:
            payload: Dict[str, Any] = {
                "model": self.model,
                "messages": messages,
                "temperature": 0.1,
                "max_tokens": 256,
            }
            if attempt == "grammar":
                payload["grammar"] = grammar
            else:
                payload["response_format"] = {"type": "json_object"}

            try:
                resp = await self._client.post(f"{self.base_url}/chat/completions", json=payload)
            except httpx.TimeoutException:
                inc_request("llm_local", "timeout")
                raise
            except httpx.HTTPError as e:
                inc_request("llm_local", "error")
                logger.error("local_llm_transport_error", error=str(e))
                raise

            if resp.status_code == 200:
                if attempt == "grammar":
                    self._grammar_supported = True
                return resp.json()["choices"][0]["message"]["content"]

            last_status, last_body = resp.status_code, _short(resp.text)

            # A grammar this build cannot parse: remember it and fall back once.
            if attempt == "grammar" and resp.status_code == 400 and "grammar" in last_body.lower():
                self._grammar_supported = False
                logger.warning(
                    "local_llm_grammar_rejected",
                    status=resp.status_code,
                    detail=last_body,
                    action="falling back to response_format=json_object",
                )
                continue

            inc_request("llm_local", "error")
            logger.error("local_llm_error", status=resp.status_code, detail=last_body)
            raise RuntimeError(f"llama-server returned HTTP {last_status}: {last_body}")

        inc_request("llm_local", "error")
        raise RuntimeError(
            f"llama-server rejected every output mode (last: HTTP {last_status}: {last_body})"
        )


def _short(text: str, limit: int = 300) -> str:
    return " ".join((text or "").split())[:limit]


def create_local_llm_backend() -> LocalLlmBackend:
    return LocalLlmBackend()
