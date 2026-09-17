"""
Local Intent Classifier (Tier 2).

Uses small LLM with constrained decoding (GBNF grammar via llama.cpp).
Only emits intent label + structured args, never selects tools.
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
# GBNF Grammar for Constrained Decoding
# ──────────────────────────────────────────────────────────────

INTENT_GRAMMAR = r"""
?start: intent
intent: "{" ws "\"intent\"" ws ":" ws intent_name ws "," ws "\"args\"" ws ":" ws args ws "}"
intent_name: "\"" intent_enum "\""
intent_enum: "open_app" | "close_app" | "set_volume" | "media_control" | "search_web" | "read_file" | "write_file" | "run_script" | "get_time" | "get_weather" | "unknown"
args: "{" ws arg_pair (ws "," ws arg_pair)* ws "}"
arg_pair: string ":" value
value: string | number | boolean | "null" | array | object
string: "\"" ( [^"\\] | "\\" ["\\/bfnrt] | "\\" "u" [0-9a-fA-F]{4} )* "\""
number: "-" ? [0-9]+ ("." [0-9]+) ? ([eE] [+-]? [0-9]+) ?
boolean: "true" | "false"
array: "[" ws (value (ws "," ws value)*)? ws "]"
object: "{" ws (string ":" value (ws "," ws string ":" value)*)? ws "}"
ws: [ \t\n\r]*
"""


# ──────────────────────────────────────────────────────────────
# Data Classes
# ──────────────────────────────────────────────────────────────

@dataclass
class ClassifierResult:
    intent: IntentName
    args: Dict[str, Any]
    confidence: float
    raw: str


# ──────────────────────────────────────────────────────────────
# Classifier
# ──────────────────────────────────────────────────────────────

class IntentClassifier:
    """
    Local intent classifier using llama.cpp server with GBNF grammar.

    Requires llama.cpp server running:
        llama-server -m model.gguf -mgf grammar.gbnf --port 8080

    Or Ollama with structured output (if supported).
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        grammar: str = INTENT_GRAMMAR,
    ):
        cfg = get_config()
        self.base_url = base_url or cfg.get("llm.local.base_url", "http://localhost:11434/v1")
        self.model = model or cfg.get("llm.local.model", "qwen2.5:7b-instruct")
        self.grammar = grammar
        self._client = httpx.AsyncClient(timeout=30.0)

        # Allowed models whitelist
        self.allowed_models = {
            "qwen2.5:7b-instruct",
            "qwen2.5:14b-instruct",
            "qwen2.5:32b-instruct",
        }

    async def classify(self, text: str) -> ClassifierResult:
        """
        Classify text into intent with structured args.

        Returns ClassifierResult with intent, args, confidence.
        """
        start_time = time.perf_counter()

        # Validate model
        if self.model not in self.allowed_models:
            logger.warning("classifier_model_not_allowed", model=self.model)
            raise ValueError(f"Model {self.model} not in allowed list")

        prompt = self._build_prompt(text)

        # Build request for llama.cpp server (OpenAI-compatible)
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": "You are an intent classifier. Output only valid JSON matching the grammar."},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.1,
            "max_tokens": 256,
            "grammar": self.grammar,  # llama.cpp extension for GBNF
        }

        try:
            response = await self._client.post(f"{self.base_url}/chat/completions", json=payload)
            response.raise_for_status()
            data = response.json()
            content = data["choices"][0]["message"]["content"]

            latency = time.perf_counter() - start_time
            observe_latency("intent_classifier", latency)
            inc_request("intent_classifier", "success")

            return self._parse_response(content)

        except httpx.TimeoutException:
            inc_request("intent_classifier", "timeout")
            raise
        except Exception as e:
            inc_request("intent_classifier", "error")
            logger.error("classifier_error", error=str(e))
            raise

    async def health_check(self) -> bool:
        try:
            resp = await self._client.get(f"{self.base_url}/models", timeout=5.0)
            return resp.status_code == 200
        except Exception:
            return False

    def _build_prompt(self, text: str) -> str:
        intents = [i.value for i in IntentName if i != IntentName.UNKNOWN]
        return f"""Classify the user's request into one of these intents: {', '.join(intents)}

User: "{text}"

Output JSON only with 'intent' and 'args' fields. Example:
{{"intent": "open_app", "args": {{"app": "notepad"}}}}"""

    def _parse_response(self, content: str) -> ClassifierResult:
        try:
            parsed = json.loads(content)
            intent_str = parsed.get("intent", "unknown")
            intent = IntentName(intent_str) if intent_str in IntentName.__members__.values() else IntentName.UNKNOWN
            args = parsed.get("args", {})
            # Confidence heuristic: valid JSON + known intent = high confidence
            confidence = 0.8 if intent != IntentName.UNKNOWN else 0.0
            return ClassifierResult(
                intent=intent,
                args=args,
                confidence=confidence,
                raw=content,
            )
        except json.JSONDecodeError:
            logger.warning("classifier_json_parse_failed", content=content[:200])
            return ClassifierResult(
                intent=IntentName.UNKNOWN,
                args={},
                confidence=0.0,
                raw=content,
            )

    async def close(self):
        await self._client.aclose()


# ──────────────────────────────────────────────────────────────
# Factory
# ──────────────────────────────────────────────────────────────

def create_intent_classifier() -> IntentClassifier:
    return IntentClassifier()