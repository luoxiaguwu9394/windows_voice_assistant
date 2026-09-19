"""
Keyword Spotter (KWS) — sherpa-onnx Zipformer transducer.

Wake-word detection over a continuous 16 kHz stream.

The zh-en 3M model uses a `phone+ppinyin` vocabulary (CMU phonemes for
English, partial pinyin for Chinese), so raw words must be converted to
model tokens via `sherpa_onnx.text2token` before being written to a
keywords file. That conversion needs `sentencepiece` and `pypinyin`.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

import numpy as np

from winvoice.config import get_config
from winvoice.logging import get_logger, inc_request, observe_latency
from ._common import (
    ModelNotFoundError,
    float32_to_pcm,
    pick_in_dir,
    resolve_path,
    to_float32,
)

logger = get_logger(__name__)


@dataclass
class KwsResult:
    keyword: str
    confidence: float
    timestamp_ms: int


def _load_sherpa():
    try:
        import sherpa_onnx
    except ImportError as e:  # pragma: no cover - environment issue
        raise ModelNotFoundError(
            "sherpa-onnx is not installed. Install it with:\n"
            "  pip install sherpa-onnx==1.13.8"
        ) from e
    return sherpa_onnx


class KwsEngine:
    """Zipformer keyword spotter with automatic keyword tokenization."""

    def __init__(
        self,
        model_path: Optional[str] = None,
        keywords: Optional[List[str]] = None,
        threshold: Optional[float] = None,
        keywords_file: Optional[str] = None,
        use_int8: bool = True,
    ):
        cfg = get_config()
        self.model_dir = resolve_path(
            model_path or cfg.get("kws.model", "models/kws/sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20"),
            kind="dir",
        )
        self.keywords = list(keywords or cfg.get("kws.keywords", ["assistant", "hey assistant"]))
        self.threshold = float(
            threshold if threshold is not None else cfg.get("kws.threshold", 0.25)
        )
        self.keywords_file = Path(keywords_file) if keywords_file else None
        self.use_int8 = use_int8
        self.sample_rate = 16000
        self._spotter = None
        self._stream = None
        self._ready = False

    # ── model files ────────────────────────────────────────────

    def _resolve_model_files(self) -> dict:
        suffix = "int8.onnx" if self.use_int8 else ".onnx"
        encoder = pick_in_dir(self.model_dir, f"encoder-*.{suffix}", "encoder*.onnx")
        decoder = pick_in_dir(self.model_dir, f"decoder-*.{suffix}", "decoder*.onnx")
        joiner = pick_in_dir(self.model_dir, f"joiner-*.{suffix}", "joiner*.onnx")
        tokens = pick_in_dir(self.model_dir, "tokens.txt")
        return {"encoder": encoder, "decoder": decoder, "joiner": joiner, "tokens": tokens}

    def _lexicon(self) -> Optional[Path]:
        return pick_in_dir(self.model_dir, "en.phone", "lexicon.txt", required=False)

    def build_keywords_file(self, out_path: Optional[Path] = None) -> Path:
        """
        Convert self.keywords into a sherpa-onnx keywords file.

        Format: one line per keyword, `tokens... @LABEL`.
        Cached under <model_dir>/winvoice_keywords.txt keyed by the keyword list.
        """
        sherpa_onnx = _load_sherpa()
        tokens = self._resolve_model_files()["tokens"]
        lexicon = self._lexicon()

        if out_path is None:
            digest = abs(hash(tuple(self.keywords))) % (10**8)
            out_path = self.model_dir / f"winvoice_keywords_{digest}.txt"

        if out_path.exists():
            cached = out_path.read_text(encoding="utf-8").strip()
            if cached:
                logger.info("kws_keywords_cached", file=str(out_path))
                return out_path

        encoded = sherpa_onnx.text2token(
            [k.upper() for k in self.keywords],
            tokens=str(tokens),
            tokens_type="phone+ppinyin",
            lexicon=str(lexicon) if lexicon else None,
        )

        lines: List[str] = []
        for word, toks in zip(self.keywords, encoded):
            if not toks:
                logger.warning("kws_keyword_untokenizable", keyword=word)
                continue
            label = word.upper().replace(" ", "_")
            lines.append(" ".join(toks) + f" @{label}")

        if not lines:
            raise ValueError(
                "No keyword could be tokenized. Check `kws.keywords` in config.yaml "
                "and that the model's tokens.txt/en.phone are present."
            )

        out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        logger.info("kws_keywords_built", file=str(out_path), count=len(lines))
        return out_path

    # ── lifecycle ──────────────────────────────────────────────

    async def initialize(self) -> None:
        sherpa_onnx = _load_sherpa()
        files = self._resolve_model_files()
        keywords_file = self.keywords_file or self.build_keywords_file()

        self._spotter = sherpa_onnx.KeywordSpotter(
            tokens=str(files["tokens"]),
            encoder=str(files["encoder"]),
            decoder=str(files["decoder"]),
            joiner=str(files["joiner"]),
            keywords_file=str(keywords_file),
            num_threads=2,
            keywords_threshold=self.threshold,
            provider="cpu",
        )
        self._stream = self._spotter.create_stream()
        self._ready = True
        logger.info(
            "kws_initialized",
            dir=str(self.model_dir),
            int8=self.use_int8,
            keywords=self.keywords,
            threshold=self.threshold,
        )

    # ── inference ──────────────────────────────────────────────

    def accept_waveform(self, samples: bytes | np.ndarray) -> None:
        """Feed int16 PCM bytes or a float32 array."""
        if not self._ready:
            return
        audio = to_float32(samples) if isinstance(samples, (bytes, bytearray)) else samples
        self._stream.accept_waveform(self.sample_rate, audio)

    def get_result(self) -> Optional[KwsResult]:
        """Return a detection if the spotter fired, else None."""
        if not self._ready:
            return None

        while self._spotter.is_ready(self._stream):
            self._spotter.decode_stream(self._stream)

        keyword = self._spotter.get_result(self._stream)
        if not keyword:
            return None

        label = keyword[1:] if keyword.startswith("@") else keyword
        inc_request("kws", "success")
        return KwsResult(
            keyword=label,
            confidence=1.0,  # sherpa-onnx does not expose a per-hit score
            timestamp_ms=int(time.time() * 1000),
        )

    def reset(self) -> None:
        """Reset the stream after a detection so it can fire again."""
        if self._ready:
            self._spotter.reset_stream(self._stream)

    def stop(self) -> None:
        self._ready = False


class StubKwsEngine(KwsEngine):
    """Model-free stand-in for `--stub-audio` runs."""

    def __init__(self, *args, **kwargs):
        self.model_dir = Path(".")
        self.keywords = kwargs.get("keywords") or ["assistant"]
        self.threshold = 0.25
        self.keywords_file = None
        self.use_int8 = True
        self.sample_rate = 16000
        self._spotter = None
        self._stream = None
        self._ready = True
        self._ticks = 0

    async def initialize(self) -> None:
        logger.info("stub_kws_initialized", keywords=self.keywords)

    def build_keywords_file(self, out_path=None) -> Path:  # pragma: no cover
        raise NotImplementedError("stub KWS has no keywords file")

    def get_result(self) -> Optional[KwsResult]:
        self._ticks += 1
        return None


def create_kws_engine(use_stub: bool = False) -> KwsEngine:
    """Factory honouring the WINVOICE_STUB_AUDIO env var."""
    import os

    if use_stub or os.getenv("WINVOICE_STUB_AUDIO") == "1":
        return StubKwsEngine()
    return KwsEngine()
