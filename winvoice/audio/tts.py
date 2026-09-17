"""
Text-to-Speech (TTS) using sherpa-onnx Piper / Kokoro.

Streaming synthesis with interrupt support.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, Optional

import numpy as np

from winvoice.config import get_config
from winvoice.logging import get_logger, observe_latency, inc_request
from winvoice.contracts import TtsRequest, TtsChunk, InterruptTTS

logger = get_logger(__name__)


@dataclass
class TtsChunk:
    data: bytes  # int16 PCM
    is_final: bool = False
    sample_rate: int = 16000


class TtsEngine:
    """
    Streaming TTS engine with interrupt support.

    Usage:
        engine = TtsEngine()
        await engine.initialize()
        async for chunk in engine.synthesize("你好世界"):
            play(chunk.data)
        # To interrupt: engine.interrupt()
    """

    def __init__(
        self,
        model_path: Optional[str] = None,
        voice: str = "default",
        sample_rate: int = 16000,
    ):
        cfg = get_config()
        self.model_path = model_path or cfg.get("tts.model", "models/tts/piper-zh")
        self.voice = voice or cfg.get("tts.voice", "default")
        self.sample_rate = sample_rate

        self._tts = None
        self._interrupted = False

    async def initialize(self) -> None:
        try:
            import sherpa_onnx
        except ImportError:
            logger.warning("sherpa_onnx not installed, using stub TTS")
            return

        if not Path(self.model_path).exists():
            raise FileNotFoundError(f"TTS model not found: {self.model_path}")

        self._tts = sherpa_onnx.OfflineTts(
            model=self.model_path,
            num_threads=2,
            provider="cpu",
        )
        logger.info("tts_initialized", model=self.model_path, voice=self.voice)

    async def synthesize(self, request: TtsRequest) -> AsyncIterator[TtsChunk]:
        """Stream TTS chunks."""
        self._interrupted = False

        if self._tts is None:
            async for chunk in self._stub_synthesize(request):
                yield chunk
            return

        import sherpa_onnx
        start_time = time.perf_counter()

        # Generate audio
        audio = self._tts.generate(request.text, sid=self._voice_to_sid(request.voice))
        samples = np.array(audio.samples, dtype=np.float32)

        latency = time.perf_counter() - start_time
        observe_latency("tts", latency)
        inc_request("tts", "success")

        # Convert to int16
        int16 = (samples * 32767).astype(np.int16)
        pcm_bytes = int16.tobytes()

        # Stream in chunks (~100ms each)
        chunk_samples = self.sample_rate // 10  # 100ms
        for i in range(0, len(int16), chunk_samples):
            if self._interrupted:
                logger.info("tts_interrupted")
                break
            chunk_data = int16[i:i + chunk_samples].tobytes()
            yield TtsChunk(data=chunk_data, is_final=(i + chunk_samples >= len(int16)))
            await asyncio.sleep(0)  # yield control

    def _voice_to_sid(self, voice: str) -> int:
        """Map voice name to speaker ID."""
        voices = {"default": 0, "guest": 1}
        return voices.get(voice, 0)

    async def _stub_synthesize(self, request: TtsRequest) -> AsyncIterator[TtsChunk]:
        """Stub: generate silence."""
        await asyncio.sleep(0.05)
        silence = np.zeros(self.sample_rate // 5, dtype=np.int16)  # 200ms
        yield TtsChunk(data=silence.tobytes(), is_final=True)

    def interrupt(self) -> None:
        """Immediate interrupt (barge-in)."""
        self._interrupted = True

    def is_interrupted(self) -> bool:
        return self._interrupted


class StubTtsEngine(TtsEngine):
    async def initialize(self) -> None:
        logger.info("stub_tts_initialized")


def create_tts_engine(use_stub: bool = False) -> TtsEngine:
    if use_stub or os.getenv("WINVOICE_STUB_AUDIO") == "1":
        return StubTtsEngine()
    return TtsEngine()


import time