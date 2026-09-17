"""
Automatic Speech Recognition (ASR) using sherpa-onnx SenseVoice / Zipformer streaming.

Transcribes audio segments to text.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np

from winvoice.config import get_config
from winvoice.logging import get_logger, observe_latency, inc_request
from winvoice.contracts import AudioFrame

logger = get_logger(__name__)


@dataclass
class AsrResult:
    text: str
    language: str
    confidence: float
    is_final: bool = True


class AsrEngine:
    """
    Streaming ASR engine.

    Usage:
        engine = AsrEngine()
        await engine.initialize()
        result = await engine.transcribe(vad_segment.frames)
    """

    def __init__(
        self,
        model_path: Optional[str] = None,
        language: Optional[str] = None,
    ):
        cfg = get_config()
        self.model_path = model_path or cfg.get("asr.model", "models/asr/sense-voice")
        self.language = language or cfg.get("asr.language", "auto")

        self._recognizer = None
        self._stream = None

    async def initialize(self) -> None:
        try:
            import sherpa_onnx
        except ImportError:
            logger.warning("sherpa_onnx not installed, using stub ASR")
            return

        if not Path(self.model_path).exists():
            raise FileNotFoundError(f"ASR model not found: {self.model_path}")

        self._recognizer = sherpa_onnx.OnlineRecognizer(
            model=self.model_path,
            num_threads=2,
            provider="cpu",
            decoding_method="greedy_search",
        )
        self._stream = self._recognizer.create_stream()
        logger.info("asr_initialized", model=self.model_path, language=self.language)

    async def transcribe(self, frames: list[AudioFrame]) -> AsrResult:
        """Transcribe a complete VAD segment."""
        if self._stream is None:
            return await self._stub_transcribe(frames)

        import sherpa_onnx
        start_time = time.perf_counter()

        # Feed all frames
        for frame in frames:
            audio = np.frombuffer(frame.data, dtype=np.int16).astype(np.float32) / 32768.0
            self._stream.accept_waveform(frame.sample_rate, audio)

        # Get result
        self._recognizer.decode_stream(self._stream)
        result = self._stream.get_result()

        latency = time.perf_counter() - start_time
        observe_latency("asr", latency)
        inc_request("asr", "success")

        return AsrResult(
            text=result.text.strip(),
            language=result.lang if hasattr(result, "lang") else self.language,
            confidence=1.0,  # sherpa-onnx doesn't always provide confidence
            is_final=True,
        )

    async def _stub_transcribe(self, frames: list[AudioFrame]) -> AsrResult:
        """Stub for testing without model."""
        await asyncio.sleep(0.1)  # simulate processing
        return AsrResult(
            text="测试语音识别结果",
            language="zh",
            confidence=0.95,
            is_final=True,
        )

    def reset(self) -> None:
        if self._stream:
            import sherpa_onnx
            self._stream = self._recognizer.create_stream()


class StubAsrEngine(AsrEngine):
    async def initialize(self) -> None:
        logger.info("stub_asr_initialized")


def create_asr_engine(use_stub: bool = False) -> AsrEngine:
    if use_stub or os.getenv("WINVOICE_STUB_AUDIO") == "1":
        return StubAsrEngine()
    return AsrEngine()


import asyncio
import time