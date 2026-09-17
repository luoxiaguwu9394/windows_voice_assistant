"""
Voice Activity Detection (VAD) using sherpa-onnx Silero VAD.

Segments continuous audio into speech/non-speech regions.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, List, Optional

import numpy as np

from winvoice.config import get_config
from winvoice.logging import get_logger, observe_latency, inc_request
from winvoice.contracts import AudioFrame

logger = get_logger(__name__)


@dataclass
class VadSegment:
    """Contiguous speech segment ready for ASR."""
    frames: List[AudioFrame]
    start_ms: int
    end_ms: int
    duration_ms: int


class VadEngine:
    """
    VAD engine using Silero via sherpa-onnx.

    Feeds 10ms frames, yields VadSegment when speech ends.
    """

    def __init__(
        self,
        model_path: Optional[str] = None,
        min_silence_ms: Optional[int] = None,
        min_speech_ms: Optional[int] = None,
    ):
        cfg = get_config()
        self.model_path = model_path or cfg.get("vad.model", "models/vad/silero")
        self.min_silence_ms = min_silence_ms or cfg.get("vad.min_silence_ms", 500)
        self.min_speech_ms = min_speech_ms or cfg.get("vad.min_speech_ms", 250)

        self._stream = None
        self._buffer: List[AudioFrame] = []
        self._in_speech = False
        self._silence_frames = 0
        self._speech_frames = 0
        self._frame_ms = 10

    async def initialize(self) -> None:
        try:
            import sherpa_onnx
        except ImportError:
            logger.warning("sherpa_onnx not installed, using stub VAD")
            return

        if not Path(self.model_path).exists():
            raise FileNotFoundError(f"VAD model not found: {self.model_path}")

        self._stream = sherpa_onnx.Vad(self.model_path).create_stream()
        logger.info("vad_initialized", model=self.model_path,
                   min_silence_ms=self.min_silence_ms, min_speech_ms=self.min_speech_ms)

    def accept_frame(self, frame: AudioFrame) -> List[VadSegment]:
        """Process one 10ms frame, return completed segments."""
        if self._stream is None:
            return []  # stub mode

        import sherpa_onnx
        audio = np.frombuffer(frame.data, dtype=np.int16).astype(np.float32) / 32768.0
        self._stream.accept_waveform(frame.sample_rate, audio)

        segments = []
        while self._stream.is_speech_detected():
            # Speech detected - accumulate frames
            self._buffer.append(frame)
            self._speech_frames += 1
            self._silence_frames = 0
            self._in_speech = True
            # Can't return segment yet, need silence to close
            break
        else:
            # No speech detected in this frame
            if self._in_speech:
                self._silence_frames += 1
                if self._silence_frames * self._frame_ms >= self.min_silence_ms:
                    # Silence threshold reached - emit segment if long enough
                    if self._speech_frames * self._frame_ms >= self.min_speech_ms:
                        segments.append(self._emit_segment())
                    self._reset_state()

        return segments

    def _emit_segment(self) -> VadSegment:
        segment = VadSegment(
            frames=self._buffer.copy(),
            start_ms=self._buffer[0].timestamp_ms,
            end_ms=self._buffer[-1].timestamp_ms + self._frame_ms,
            duration_ms=len(self._buffer) * self._frame_ms,
        )
        return segment

    def _reset_state(self) -> None:
        self._buffer.clear()
        self._in_speech = False
        self._silence_frames = 0
        self._speech_frames = 0

    def flush(self) -> Optional[VadSegment]:
        """Emit any pending segment on stream end."""
        if self._in_speech and self._speech_frames * self._frame_ms >= self.min_speech_ms:
            return self._emit_segment()
        return None


class StubVadEngine(VadEngine):
    """Stub VAD for testing - segments on amplitude threshold."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._amplitude_threshold = 0.01

    async def initialize(self) -> None:
        logger.info("stub_vad_initialized")

    def accept_frame(self, frame: AudioFrame) -> List[VadSegment]:
        import numpy as np
        audio = np.frombuffer(frame.data, dtype=np.int16).astype(np.float32) / 32768.0
        rms = np.sqrt(np.mean(audio ** 2))
        is_speech = rms > self._amplitude_threshold

        segments = []
        if is_speech:
            self._buffer.append(frame)
            self._in_speech = True
            self._silence_frames = 0
        elif self._in_speech:
            self._silence_frames += 1
            if self._silence_frames * self._frame_ms >= self.min_silence_ms:
                if len(self._buffer) * self._frame_ms >= self.min_speech_ms:
                    segments.append(self._emit_segment())
                self._reset_state()

        return segments


def create_vad_engine(use_stub: bool = False) -> VadEngine:
    if use_stub or os.getenv("WINVOICE_STUB_AUDIO") == "1":
        return StubVadEngine()
    return VadEngine()