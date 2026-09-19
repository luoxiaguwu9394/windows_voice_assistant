"""
Voice Activity Detection (VAD) — sherpa-onnx Silero VAD v5.

Segments a continuous 16 kHz stream into speech regions. The detector
buffers audio internally; completed segments are drained from a queue via
`front` / `pop`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

import numpy as np

from winvoice.config import get_config
from winvoice.logging import get_logger, inc_request
from ._common import ModelNotFoundError, float32_to_pcm, resolve_path, to_float32

logger = get_logger(__name__)

SAMPLE_RATE = 16000
WINDOW_SIZE = 512  # Silero v5 requires exactly 512 samples per call


@dataclass
class VadSegment:
    """A completed speech region."""

    samples: np.ndarray          # float32 mono at 16 kHz
    start_sample: int
    duration_ms: int

    @property
    def pcm(self) -> bytes:
        """Segment as int16 little-endian PCM bytes."""
        return float32_to_pcm(self.samples)


def _load_sherpa():
    try:
        import sherpa_onnx
    except ImportError as e:  # pragma: no cover
        raise ModelNotFoundError(
            "sherpa-onnx is not installed. Install it with:\n"
            "  pip install sherpa-onnx==1.13.8"
        ) from e
    return sherpa_onnx


class VadEngine:
    """Silero VAD v5 wrapper."""

    def __init__(
        self,
        model_path: Optional[str] = None,
        min_silence_ms: Optional[int] = None,
        min_speech_ms: Optional[int] = None,
        threshold: float = 0.5,
        buffer_size_seconds: float = 30.0,
    ):
        cfg = get_config()
        self.model_path = resolve_path(
            model_path or cfg.get("vad.model", "models/vad/silero_vad_v5.onnx"),
            kind="file",
        )
        self.min_silence_ms = int(
            min_silence_ms if min_silence_ms is not None else cfg.get("vad.min_silence_ms", 500)
        )
        self.min_speech_ms = int(
            min_speech_ms if min_speech_ms is not None else cfg.get("vad.min_speech_ms", 250)
        )
        self.threshold = threshold
        self.buffer_size_seconds = buffer_size_seconds
        self.sample_rate = SAMPLE_RATE

        self._vad = None
        self._ready = False
        self._pending = np.zeros(0, dtype=np.float32)  # leftover < WINDOW_SIZE samples
        self._samples_seen = 0
        self._segment_count = 0

    async def initialize(self) -> None:
        sherpa_onnx = _load_sherpa()

        config = sherpa_onnx.VadModelConfig(
            silero_vad=sherpa_onnx.SileroVadModelConfig(
                model=str(self.model_path),
                threshold=self.threshold,
                min_silence_duration=self.min_silence_ms / 1000.0,
                min_speech_duration=self.min_speech_ms / 1000.0,
                window_size=WINDOW_SIZE,
            ),
            sample_rate=self.sample_rate,
            num_threads=1,
            provider="cpu",
        )
        self._vad = sherpa_onnx.VoiceActivityDetector(config, buffer_size_in_seconds=self.buffer_size_seconds)
        self._ready = True
        logger.info(
            "vad_initialized",
            model=str(self.model_path),
            min_silence_ms=self.min_silence_ms,
            min_speech_ms=self.min_speech_ms,
        )

    def accept_waveform(self, samples: bytes | np.ndarray) -> List[VadSegment]:
        """
        Feed PCM bytes or float32 audio; return any segments completed
        by this call.

        sherpa-onnx requires exact 512-sample windows, so leftover samples
        are buffered and prepended to the next call.
        """
        if not self._ready:
            return []

        audio = to_float32(samples) if isinstance(samples, (bytes, bytearray)) else np.asarray(samples, dtype=np.float32)

        if self._pending.size:
            audio = np.concatenate([self._pending, audio])

        n_windows = audio.size // WINDOW_SIZE
        consumed = n_windows * WINDOW_SIZE
        self._pending = audio[consumed:].copy()

        segments: List[VadSegment] = []
        for i in range(n_windows):
            window = audio[i * WINDOW_SIZE : (i + 1) * WINDOW_SIZE]
            self._vad.accept_waveform(window)
            self._samples_seen += WINDOW_SIZE
            segments.extend(self._drain())

        return segments

    def _drain(self) -> List[VadSegment]:
        """Pull every completed segment currently queued."""
        out: List[VadSegment] = []
        while not self._vad.empty():
            seg = self._vad.front
            samples = np.asarray(seg.samples, dtype=np.float32)
            duration_ms = int(samples.size / self.sample_rate * 1000)
            out.append(
                VadSegment(
                    samples=samples,
                    start_sample=int(getattr(seg, "start", 0)),
                    duration_ms=duration_ms,
                )
            )
            self._vad.pop()
            self._segment_count += 1
            inc_request("vad", "success")
            logger.debug("vad_segment", duration_ms=duration_ms)
        return out

    def flush(self) -> List[VadSegment]:
        """Force-emit any buffered speech (call at end of stream)."""
        if not self._ready:
            return []
        self._vad.flush()
        return self._drain()

    def reset(self) -> None:
        if self._ready:
            self._vad.reset()
        self._pending = np.zeros(0, dtype=np.float32)

    @property
    def segment_count(self) -> int:
        return self._segment_count


class StubVadEngine(VadEngine):
    """Amplitude-threshold stand-in for `--stub-audio` runs."""

    def __init__(self, *args, **kwargs):
        self.model_path = Path(".")
        self.min_silence_ms = 500
        self.min_speech_ms = 250
        self.threshold = 0.5
        self.buffer_size_seconds = 30.0
        self.sample_rate = SAMPLE_RATE
        self._vad = None
        self._ready = True
        self._pending = np.zeros(0, dtype=np.float32)
        self._samples_seen = 0
        self._segment_count = 0
        self._buf: List[np.ndarray] = []
        self._silence = 0
        self._amplitude_threshold = 0.01

    async def initialize(self) -> None:
        logger.info("stub_vad_initialized")

    def accept_waveform(self, samples) -> List[VadSegment]:
        audio = to_float32(samples) if isinstance(samples, (bytes, bytearray)) else np.asarray(samples, dtype=np.float32)
        frame_ms = max(1, int(audio.size / self.sample_rate * 1000))
        rms = float(np.sqrt(np.mean(audio ** 2))) if audio.size else 0.0

        if rms > self._amplitude_threshold:
            self._buf.append(audio)
            self._silence = 0
            return []

        if not self._buf:
            return []

        self._silence += frame_ms
        if self._silence * 1 < self.min_silence_ms:
            return []

        combined = np.concatenate(self._buf)
        self._buf = []
        self._silence = 0
        if combined.size / self.sample_rate * 1000 < self.min_speech_ms:
            return []

        self._segment_count += 1
        return [VadSegment(samples=combined, start_sample=0,
                           duration_ms=int(combined.size / self.sample_rate * 1000))]

    def flush(self) -> List[VadSegment]:
        if not self._buf:
            return []
        combined = np.concatenate(self._buf)
        self._buf = []
        return [VadSegment(samples=combined, start_sample=0,
                           duration_ms=int(combined.size / self.sample_rate * 1000))]

    def reset(self) -> None:
        self._buf = []
        self._silence = 0


def create_vad_engine(use_stub: bool = False) -> VadEngine:
    """Factory honouring the WINVOICE_STUB_AUDIO env var."""
    if use_stub or os.getenv("WINVOICE_STUB_AUDIO") == "1":
        return StubVadEngine()
    return VadEngine()
