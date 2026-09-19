"""
Automatic Speech Recognition — sherpa-onnx offline recognizer.

Config points at SenseVoice (int8), which covers zh / en / ja / ko / yue
and supports inverse text normalization (punctuation). The pipeline feeds
complete VAD segments, so an offline (non-streaming) recognizer is the
right fit; `asr.streaming_model` optionally selects the streaming
Zipformer transducer instead.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence

import numpy as np

from winvoice.config import get_config
from winvoice.logging import get_logger, inc_request, observe_latency
from ._common import (
    ModelNotFoundError,
    frames_to_float32,
    resolve_path,
)

logger = get_logger(__name__)

SAMPLE_RATE = 16000


@dataclass
class AsrResult:
    text: str
    language: str
    confidence: float
    is_final: bool = True


def _load_sherpa():
    try:
        import sherpa_onnx
    except ImportError as e:  # pragma: no cover
        raise ModelNotFoundError(
            "sherpa-onnx is not installed. Install it with:\n"
            "  pip install sherpa-onnx==1.13.8"
        ) from e
    return sherpa_onnx


class AsrEngine:
    """Offline SenseVoice recognizer (or streaming Zipformer fallback)."""

    def __init__(
        self,
        model_path: Optional[str] = None,
        tokens_path: Optional[str] = None,
        language: Optional[str] = None,
        use_itn: Optional[bool] = None,
        streaming: bool = False,
        num_threads: int = 2,
    ):
        cfg = get_config()
        self.model_path = resolve_path(
            model_path
            or cfg.get(
                "asr.model",
                "models/asr/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17/model.int8.onnx",
            ),
            kind="file",
        )
        tokens_cfg = tokens_path or cfg.get("asr.tokens")
        self.tokens_path = resolve_path(
            tokens_cfg if tokens_cfg else self.model_path.parent / "tokens.txt",
            kind="file",
        )
        self.language = language or cfg.get("asr.language", "auto")
        self.use_itn = bool(use_itn if use_itn is not None else cfg.get("asr.use_itn", True))
        self.streaming = streaming
        self.num_threads = num_threads
        self.sample_rate = SAMPLE_RATE

        self._recognizer = None
        self._stream = None
        self._ready = False

    async def initialize(self) -> None:
        sherpa_onnx = _load_sherpa()

        if self.streaming:
            # Streaming transducer needs encoder/decoder/joiner siblings.
            model_dir = self.model_path.parent
            self._recognizer = sherpa_onnx.OnlineRecognizer.from_transducer(
                tokens=str(self.tokens_path),
                encoder=str(_find(model_dir, "encoder-*.onnx", "encoder*.onnx")),
                decoder=str(_find(model_dir, "decoder-*.onnx", "decoder*.onnx")),
                joiner=str(_find(model_dir, "joiner-*.onnx", "joiner*.onnx")),
                num_threads=self.num_threads,
                sample_rate=self.sample_rate,
                feature_dim=80,
                decoding_method="greedy_search",
                provider="cpu",
            )
            logger.info("asr_initialized", mode="streaming", model=str(self.model_path))
        else:
            self._recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
                model=str(self.model_path),
                tokens=str(self.tokens_path),
                num_threads=self.num_threads,
                use_itn=self.use_itn,
                language=self.language,
                provider="cpu",
                debug=False,
            )
            logger.info(
                "asr_initialized",
                mode="sense_voice",
                model=str(self.model_path),
                language=self.language,
                use_itn=self.use_itn,
            )

        self._ready = True

    async def transcribe(self, frames: Sequence) -> AsrResult:
        """
        Transcribe AudioFrame objects or a raw float32 array.

        Returns an AsrResult; empty text is a valid (silence) outcome.
        """
        if not self._ready:
            raise RuntimeError("AsrEngine.initialize() must be awaited first")

        if isinstance(frames, np.ndarray):
            samples = np.asarray(frames, dtype=np.float32)
        else:
            samples = frames_to_float32(frames)

        if samples.size == 0:
            return AsrResult(text="", language=self.language, confidence=0.0)

        start = time.perf_counter()

        if self.streaming:
            text, lang = self._transcribe_streaming(samples)
        else:
            stream = self._recognizer.create_stream()
            stream.accept_waveform(self.sample_rate, samples)
            self._recognizer.decode_stream(stream)
            result = stream.result
            text = (result.text or "").strip()
            lang = _clean_lang(getattr(result, "lang", self.language))

        latency = time.perf_counter() - start
        observe_latency("asr", latency)
        inc_request("asr", "success")
        logger.info("asr_result", text=text, language=lang, latency_ms=int(latency * 1000))

        return AsrResult(
            text=text,
            language=lang,
            confidence=0.0,  # sherpa-onnx exposes no calibrated score
            is_final=True,
        )

    def _transcribe_streaming(self, samples: np.ndarray) -> tuple[str, str]:
        stream = self._recognizer.create_stream()
        stream.accept_waveform(self.sample_rate, samples)
        stream.input_finished()

        while self._recognizer.is_ready(stream):
            self._recognizer.decode_stream(stream)

        text = (self._recognizer.get_result(stream) or "").strip()
        return text, self.language

    def reset(self) -> None:
        """No-op for offline mode; kept for pipeline symmetry."""
        self._stream = None


def _clean_lang(value) -> str:
    """SenseVoice returns '<|zh|>'-style tags; strip to a bare code."""
    if not value:
        return "auto"
    return str(value).strip("<|>").strip() or "auto"


def _find(model_dir: Path, *patterns: str) -> Path:
    for pattern in patterns:
        matches = sorted(model_dir.glob(pattern))
        if matches:
            return matches[0]
    raise ModelNotFoundError(
        f"none of {patterns} found in {model_dir}\n"
        f"  Re-download with: python scripts/download_models.py --asr"
    )


class StubAsrEngine(AsrEngine):
    """Model-free stand-in for `--stub-audio` runs."""

    def __init__(self, *args, **kwargs):
        self.model_path = Path(".")
        self.tokens_path = Path(".")
        self.language = "auto"
        self.use_itn = True
        self.streaming = False
        self.num_threads = 1
        self.sample_rate = SAMPLE_RATE
        self._recognizer = None
        self._stream = None
        self._ready = True
        self._transcript = kwargs.get("stub_text") or "打开记事本"

    async def initialize(self) -> None:
        logger.info("stub_asr_initialized")

    async def transcribe(self, frames) -> AsrResult:
        return AsrResult(text=self._transcript, language="zh", confidence=0.9, is_final=True)


def create_asr_engine(use_stub: bool = False) -> AsrEngine:
    """Factory honouring the WINVOICE_STUB_AUDIO env var."""
    if use_stub or os.getenv("WINVOICE_STUB_AUDIO") == "1":
        return StubAsrEngine()
    return AsrEngine()
