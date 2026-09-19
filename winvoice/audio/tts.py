"""
Text-to-Speech — sherpa-onnx OfflineTts with a VITS Chinese model.

Config points at `vits-icefall-zh-aishell3` (lexicon-based, 174 speakers).
Synthesis is streamed out in ~100 ms chunks so playback can be interrupted
mid-utterance (KWS barge-in).
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, Optional

import numpy as np

from winvoice.config import get_config
from winvoice.contracts import TtsRequest
from winvoice.logging import get_logger, inc_request, observe_latency
from ._common import ModelNotFoundError, float32_to_pcm, resolve_path

logger = get_logger(__name__)

CHUNK_MS = 100


@dataclass
class TtsChunk:
    data: bytes  # int16 PCM
    is_final: bool = False
    sample_rate: int = 16000


def _load_sherpa():
    try:
        import sherpa_onnx
    except ImportError as e:  # pragma: no cover
        raise ModelNotFoundError(
            "sherpa-onnx is not installed. Install it with:\n"
            "  pip install sherpa-onnx==1.13.8"
        ) from e
    return sherpa_onnx


class TtsEngine:
    """VITS Chinese TTS with interruptible streaming synthesis."""

    def __init__(
        self,
        model_path: Optional[str] = None,
        voice: Optional[str] = None,
        speaker_id: int = 0,
        speed: float = 1.0,
        num_threads: int = 2,
    ):
        cfg = get_config()
        self.model_dir = resolve_path(
            model_path or cfg.get("tts.model", "models/tts/vits-icefall-zh-aishell3"),
            kind="dir",
        )
        self.model_file = resolve_path(self.model_dir / "model.onnx", kind="file")
        self.tokens_file = resolve_path(self.model_dir / "tokens.txt", kind="file")

        lexicon = self.model_dir / "lexicon.txt"
        self.lexicon_file = lexicon if lexicon.exists() else None

        # Text-normalisation FSTs shipped with the model. Without them sherpa
        # feeds digits straight to the Chinese lexicon, which has no entry for
        # them, and drops them ("OOV 90. Ignore it!") — so "调到90" came out as
        # "调到" with the number silently missing.
        self.rule_fsts = [
            path
            for path in (
                self.model_dir / "number.fst",
                self.model_dir / "date.fst",
                self.model_dir / "phone.fst",
            )
            if path.exists()
        ]

        self.voice = voice or cfg.get("tts.voice", "default")
        self.speaker_id = speaker_id
        self.speed = speed
        self.num_threads = num_threads

        self._tts = None
        self._ready = False
        self._interrupted = False

    async def initialize(self) -> None:
        sherpa_onnx = _load_sherpa()

        config = sherpa_onnx.OfflineTtsConfig(
            model=sherpa_onnx.OfflineTtsModelConfig(
                vits=sherpa_onnx.OfflineTtsVitsModelConfig(
                    model=str(self.model_file),
                    tokens=str(self.tokens_file),
                    lexicon=str(self.lexicon_file) if self.lexicon_file else "",
                ),
                num_threads=self.num_threads,
                provider="cpu",
                debug=False,
            ),
            max_num_sentences=1,
            rule_fsts=",".join(str(p) for p in self.rule_fsts),
        )
        self._tts = sherpa_onnx.OfflineTts(config)
        self._ready = True

        n_speakers = int(getattr(self._tts, "num_speakers", 1) or 1)
        if self.speaker_id >= n_speakers:
            logger.warning(
                "tts_speaker_id_out_of_range",
                requested=self.speaker_id,
                available=n_speakers,
            )
            self.speaker_id = 0

        logger.info(
            "tts_initialized",
            model=str(self.model_file),
            sample_rate=self.sample_rate,
            num_speakers=n_speakers,
        )

    @property
    def sample_rate(self) -> int:
        return int(getattr(self._tts, "sample_rate", 16000) or 16000)

    @property
    def num_speakers(self) -> int:
        return int(getattr(self._tts, "num_speakers", 1) or 1)

    async def synthesize(self, request: TtsRequest) -> AsyncIterator[TtsChunk]:
        """Yield ~100 ms PCM chunks; stops early when interrupted."""
        self._interrupted = False

        if not self._ready:
            raise RuntimeError("TtsEngine.initialize() must be awaited first")
        if not request.text.strip():
            return

        sid = self._resolve_speaker(request.voice)
        start = time.perf_counter()

        audio = self._tts.generate(request.text, sid=sid, speed=self.speed)
        samples = np.asarray(audio.samples, dtype=np.float32)
        sample_rate = int(audio.sample_rate)

        latency = time.perf_counter() - start
        observe_latency("tts", latency)
        inc_request("tts", "success")
        logger.info(
            "tts_generated",
            text_len=len(request.text),
            audio_ms=int(samples.size / sample_rate * 1000),
            latency_ms=int(latency * 1000),
            sid=sid,
        )

        chunk_samples = max(1, int(sample_rate * CHUNK_MS / 1000))
        total = samples.size
        for offset in range(0, total, chunk_samples):
            if self._interrupted:
                logger.info("tts_interrupted", at_ms=int(offset / sample_rate * 1000))
                return
            piece = samples[offset : offset + chunk_samples]
            is_last = offset + chunk_samples >= total
            yield TtsChunk(
                data=float32_to_pcm(piece),
                is_final=is_last,
                sample_rate=sample_rate,
            )

    def _resolve_speaker(self, voice: str) -> int:
        """Map a voice name to a VITS speaker id."""
        if voice == "guest" and self.num_speakers > 1:
            return 1 % self.num_speakers
        return self.speaker_id

    def interrupt(self) -> None:
        """Signal barge-in; the generator stops at the next chunk boundary."""
        self._interrupted = True

    def is_interrupted(self) -> bool:
        return self._interrupted


class StubTtsEngine(TtsEngine):
    """Model-free stand-in for `--stub-audio` runs (emits 200 ms of silence)."""

    def __init__(self, *args, **kwargs):
        self.model_dir = Path(".")
        self.model_file = Path(".")
        self.tokens_file = Path(".")
        self.lexicon_file = None
        self.voice = "default"
        self.speaker_id = 0
        self.speed = 1.0
        self.num_threads = 1
        self._tts = None
        self._ready = True
        self._interrupted = False

    async def initialize(self) -> None:
        logger.info("stub_tts_initialized")

    async def synthesize(self, request: TtsRequest) -> AsyncIterator[TtsChunk]:
        self._interrupted = False
        silence = np.zeros(int(16000 * 0.2), dtype=np.int16).tobytes()
        yield TtsChunk(data=silence, is_final=True, sample_rate=16000)

    def interrupt(self) -> None:
        self._interrupted = True


def create_tts_engine(use_stub: bool = False) -> TtsEngine:
    """Factory honouring the WINVOICE_STUB_AUDIO env var."""
    if use_stub or os.getenv("WINVOICE_STUB_AUDIO") == "1":
        return StubTtsEngine()
    return TtsEngine()
