"""
Keyword Spotter (KWS) using sherpa-onnx Zipformer.

Single-process async wrapper around sherpa-onnx KWS stream.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, Optional

from winvoice.config import get_config
from winvoice.logging import get_logger, observe_latency, inc_request


logger = get_logger(__name__)


@dataclass
class KwsResult:
    keyword: str
    confidence: float
    timestamp_ms: int


class KwsEngine:
    """
    Wake-word detection engine.

    Usage:
        engine = KwsEngine()
        async for result in engine.stream():
            if result:  # keyword detected
                handle_trigger(result)
    """

    def __init__(self, model_path: Optional[str] = None, keywords: Optional[list[str]] = None, threshold: Optional[float] = None):
        cfg = get_config()
        self.model_path = model_path or cfg.get("kws.model", "models/kws/zipformer-zh-en")
        self.keywords = keywords or cfg.get("kws.keywords", ["assistant", "hey assistant"])
        self.threshold = threshold or cfg.get("kws.threshold", 0.25)

        self._stream = None
        self._running = False
        self._sample_rate = 16000

    async def initialize(self) -> None:
        """Load model and create KWS stream."""
        try:
            import sherpa_onnx
        except ImportError:
            logger.warning("sherpa_onnx not installed, using stub KWS")
            return

        if not Path(self.model_path).exists():
            raise FileNotFoundError(f"KWS model not found: {self.model_path}")

        self._stream = sherpa_onnx.KeywordSpotter(
            model=self.model_path,
            keywords=self.keywords,
            threshold=self.threshold,
            num_threads=2,
            provider="cpu",
        ).create_stream()

        logger.info("kws_initialized", model=self.model_path, keywords=self.keywords, threshold=self.threshold)

    def accept_waveform(self, samples: bytes) -> None:
        """Feed PCM int16 bytes to the stream."""
        if self._stream is None:
            return  # stub mode

        import numpy as np
        audio = np.frombuffer(samples, dtype=np.int16).astype(np.float32) / 32768.0
        self._stream.accept_waveform(16000, audio)

    def get_result(self) -> Optional[KwsResult]:
        """Check for keyword detection."""
        if self._stream is None:
            return None

        if self._stream.ready():
            result = self._stream.get_result()
            if result:
                return KwsResult(
                    keyword=result.keyword,
                    confidence=result.score,
                    timestamp_ms=int(time.time() * 1000),  # approximate
                )
        return None

    def reset(self) -> None:
        """Reset stream after detection."""
        if self._stream:
            import sherpa_onnx
            sherpa_onnx.KeywordSpotter(
                model=self.model_path,
                keywords=self.keywords,
                threshold=self.threshold,
            ).create_stream()  # recreate stream

    async def stream(self) -> AsyncIterator[Optional[KwsResult]]:
        """Async generator yielding KwsResult on detection, None otherwise."""
        self._running = True
        try:
            while self._running:
                # In real use, this is fed by audio callback
                # Here we just yield None; real implementation pushes via accept_waveform
                await asyncio.sleep(0.01)
                result = self.get_result()
                if result:
                    yield result
                    self.reset()
        finally:
            self._running = False

    def stop(self) -> None:
        self._running = False


# ──────────────────────────────────────────────────────────────
# Stub for testing without sherpa-onnx
# ──────────────────────────────────────────────────────────────

class StubKwsEngine(KwsEngine):
    """Stub KWS that triggers on a special test keyword in audio."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._trigger_count = 0

    async def initialize(self) -> None:
        logger.info("stub_kws_initialized", keywords=self.keywords)

    def accept_waveform(self, samples: bytes) -> None:
        # Detect test pattern: 100ms of 1kHz tone (simplified)
        pass

    def get_result(self) -> Optional[KwsResult]:
        # For testing: return a trigger every N calls
        self._trigger_count += 1
        if self._trigger_count % 1000 == 0:  # simulate trigger
            return KwsResult(
                keyword=self.keywords[0],
                confidence=0.95,
                timestamp_ms=int(time.time() * 1000),
            )
        return None


# ──────────────────────────────────────────────────────────────
# Factory
# ──────────────────────────────────────────────────────────────

def create_kws_engine(use_stub: bool = False) -> KwsEngine:
    """Factory to create appropriate KWS engine."""
    if use_stub or os.getenv("WINVOICE_STUB_AUDIO") == "1":
        return StubKwsEngine()
    return KwsEngine()


import time  # for timestamp