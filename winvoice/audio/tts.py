"""
Text-to-Speech — sherpa-onnx OfflineTts, per sentence, off the event loop.

The pipeline this replaces synthesised the whole reply in one `generate()` call
and then chunked the finished audio. Two things were wrong with that, and both
are audible:

* **latency** — nothing is heard until the *entire* reply has been synthesised,
  which is why the reply budget had to stay at 80 characters;
* **rhythm** — one call means the model decides where the pauses go, and the
  model has no opinion: its lexicon holds no punctuation at all and
  sherpa-onnx's `silence_scale` changed nothing (measured, see
  `scripts/calibrate_tts_pauses.py`).

So the text is now normalised and segmented first (`winvoice.text`), synthesised
per segment, and each segment carries the pause that follows it:

    normalize → segment → generate(segment) → trim edges → yield PCM + pause

Everything that makes the result sound right is upstream of this file (the cut
points and the pause table) or downstream of it (the player writes the pause as
real silence). What happens here is the plumbing: the right backend for the
model on disk, one `generate()` per segment on a worker thread so that wake-word
barge-in still works while synthesising, and the model's own 0.3–0.4 s of edge
noise floor trimmed off so the pause table is what the listener hears.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, Optional, Sequence

import numpy as np

from winvoice.config import get_config
from winvoice.contracts import TtsRequest
from winvoice.logging import get_logger, inc_request, observe_latency
from winvoice.text import (
    SpeechSegment,
    SpeechTextConfig,
    normalize_for_speech,
    segment_for_speech,
    speech_text_config,
)
from ._common import (
    ModelNotFoundError,
    collapse_internal_gaps,
    float32_to_pcm,
    trim_edge_silence,
)

logger = get_logger(__name__)

CHUNK_MS = 100

# 22.05 kHz through the vocos vocoder, and the 8 kHz icefall model kept as a
# fallback so a machine that has not downloaded the new one still speaks.
DEFAULT_MODEL = "models/tts/matcha-icefall-zh-baker"
DEFAULT_FALLBACK_MODELS = ("models/tts/vits-icefall-zh-aishell3",)
VITS_MODEL_FILES = ("model.onnx", "model.int8.onnx")
MATCHA_MODEL_FILES = ("model-steps-3.onnx", "model-steps-2.onnx", "model-steps-1.onnx")
VOCODER_FILES = (
    "vocos-22khz-univ.onnx",
    "vocos-24khz-univ.onnx",
    "hifigan_v3.onnx",
    "hifigan_v2.onnx",
)
RULE_FST_FILES = ("number.fst", "date.fst", "phone.fst")

VOCODER_HINT = (
    "  Download it with:  python scripts/download_models.py --tts\n"
    "  (a Matcha model needs its vocoder: vocos-22khz-univ.onnx)"
)


@dataclass
class TtsChunk:
    """
    One piece of speakable audio, plus the pause that follows it.

    `pause_after_ms` is the whole point of the type: the model does not produce
    the pause, so it has to survive the trip from the segmenter to the playback
    layer as data. It is attached to the **last** chunk of each segment, which is
    where a player should insert the silence.
    """

    data: bytes  # int16 PCM
    is_final: bool = False
    sample_rate: int = 24000
    pause_after_ms: int = 0
    text: str = ""


@dataclass(frozen=True)
class TtsModel:
    """The model on disk, and everything the engine needs to load it."""

    directory: Path
    backend: str  # "vits" | "matcha"
    model_file: Path
    tokens_file: Path
    lexicon_file: Optional[Path] = None
    dict_dir: Optional[Path] = None
    vocoder_file: Optional[Path] = None
    rule_fsts: tuple[Path, ...] = ()


def _load_sherpa():
    try:
        import sherpa_onnx
    except ImportError as e:  # pragma: no cover
        raise ModelNotFoundError(
            "sherpa-onnx is not installed. Install it with:\n"
            "  pip install sherpa-onnx==1.13.8"
        ) from e
    return sherpa_onnx


def _as_dir(value: str | Path) -> Path:
    """A configured model path as an absolute directory (existence not checked)."""
    path = Path(value).expanduser()
    return path if path.is_absolute() else Path.cwd() / path


def _first_existing(directory: Path, names: Sequence[str]) -> Optional[Path]:
    for name in names:
        candidate = directory / name
        if candidate.exists():
            return candidate
    return None


def _detect_backend(directory: Path, requested: str = "auto") -> Optional[str]:
    """`vits` or `matcha` for the files present, or None if it is neither."""
    if requested in ("vits", "matcha"):
        return requested
    if _first_existing(directory, MATCHA_MODEL_FILES) is not None:
        return "matcha"
    if _first_existing(directory, VITS_MODEL_FILES) is not None:
        return "vits"
    return None


def resolve_tts_model(
    primary: Optional[str] = None,
    fallbacks: Optional[Sequence[str]] = None,
    *,
    backend: str = "auto",
    vocoder: str = "",
) -> TtsModel:
    """
    Find the best model that is actually on disk.

    Preference order is explicit rather than clever: the configured model first,
    then each fallback in order. A model directory that exists but is incomplete
    (a Matcha acoustic model with no vocoder, say) is reported as such only if
    nothing else works — a machine that downloaded the fallback and not the
    primary must keep speaking, not refuse to start.

    `fallbacks=None` means "use the built-in default"; an explicit **empty** list
    means "this model or nothing", which is what the engine's own config
    expresses when `tts.fallback_models` is empty.
    """
    if fallbacks is None:
        fallbacks = DEFAULT_FALLBACK_MODELS

    candidates: list[str] = []
    for value in (primary or DEFAULT_MODEL, *fallbacks):
        if value and value not in candidates:
            candidates.append(value)

    problems: list[str] = []
    for value in candidates:
        directory = _as_dir(value)
        if not directory.is_dir():
            problems.append(f"{value}: directory not found")
            continue

        detected = _detect_backend(directory, backend)
        if detected is None:
            problems.append(
                f"{value}: no model file found "
                f"(looked for {', '.join(VITS_MODEL_FILES + MATCHA_MODEL_FILES)})"
            )
            continue

        tokens_file = directory / "tokens.txt"
        if not tokens_file.exists():
            problems.append(f"{value}: tokens.txt not found")
            continue

        wanted = MATCHA_MODEL_FILES if detected == "matcha" else VITS_MODEL_FILES
        model_file = _first_existing(directory, wanted)
        if model_file is None:
            problems.append(
                f"{value}: backend '{detected}' requested but none of "
                f"{', '.join(wanted)} is present"
            )
            continue

        vocoder_file: Optional[Path] = None
        if detected == "matcha":
            vocoder_file = _as_dir(vocoder) if vocoder else _first_existing(directory, VOCODER_FILES)
            if vocoder_file is None or not vocoder_file.exists():
                problems.append(f"{value}: vocoder not found\n{VOCODER_HINT}")
                continue

        lexicon = directory / "lexicon.txt"
        dict_dir = directory / "dict"
        return TtsModel(
            directory=directory,
            backend=detected,
            model_file=model_file,
            tokens_file=tokens_file,
            lexicon_file=lexicon if lexicon.exists() else None,
            dict_dir=dict_dir if dict_dir.is_dir() else None,
            vocoder_file=vocoder_file,
            rule_fsts=tuple(
                directory / name for name in RULE_FST_FILES if (directory / name).exists()
            ),
        )

    raise ModelNotFoundError(
        "No usable TTS model found. Tried:\n"
        + "".join(f"  - {problem}\n" for problem in problems)
        + "  Download models with:  python scripts/download_models.py --tts\n"
        + "  List available models: python scripts/download_models.py --list"
    )


def build_offline_tts(model: TtsModel, num_threads: int = 2, silence_scale: float = 0.2):
    """
    Build the sherpa-onnx engine for a resolved model.

    Shared with `scripts/calibrate_tts_pauses.py`, which needs raw `generate()`
    output (before trimming and segmentation) to measure a model's own silences.
    """
    sherpa_onnx = _load_sherpa()

    if model.backend == "matcha":
        model_config = sherpa_onnx.OfflineTtsModelConfig(
            matcha=sherpa_onnx.OfflineTtsMatchaModelConfig(
                acoustic_model=str(model.model_file),
                vocoder=str(model.vocoder_file),
                lexicon=str(model.lexicon_file) if model.lexicon_file else "",
                tokens=str(model.tokens_file),
                dict_dir=str(model.dict_dir) if model.dict_dir else "",
            ),
            num_threads=num_threads,
            provider="cpu",
            debug=False,
        )
    else:
        model_config = sherpa_onnx.OfflineTtsModelConfig(
            vits=sherpa_onnx.OfflineTtsVitsModelConfig(
                model=str(model.model_file),
                tokens=str(model.tokens_file),
                lexicon=str(model.lexicon_file) if model.lexicon_file else "",
                dict_dir=str(model.dict_dir) if model.dict_dir else "",
            ),
            num_threads=num_threads,
            provider="cpu",
            debug=False,
        )

    return sherpa_onnx.OfflineTts(
        sherpa_onnx.OfflineTtsConfig(
            model=model_config,
            max_num_sentences=1,
            rule_fsts=",".join(str(path) for path in model.rule_fsts),
            silence_scale=silence_scale,
        )
    )


class TtsEngine:
    """
    Chinese TTS: one synthesis call per segment, interruptible between them.

    `synthesize` is an async generator that yields ~100 ms PCM chunks. It runs
    `generate()` on a worker thread, which is what keeps the pipeline's tick loop
    free to notice a wake word while the assistant is talking — the previous
    implementation blocked the loop for the whole utterance.
    """

    def __init__(
        self,
        model_path: Optional[str] = None,
        fallback_models: Optional[Sequence[str]] = None,
        backend: Optional[str] = None,
        vocoder: Optional[str] = None,
        voice: Optional[str] = None,
        speaker_id: Optional[int] = None,
        guest_speaker_id: Optional[int] = None,
        speed: Optional[float] = None,
        guest_speed: Optional[float] = None,
        pitch: Optional[float] = None,
        num_threads: Optional[int] = None,
        speech: Optional[SpeechTextConfig] = None,
        chunk_ms: int = CHUNK_MS,
        silence_scale: float = 0.2,
    ):
        cfg = get_config()

        self.model_path = model_path or cfg.get("tts.model", DEFAULT_MODEL)
        self.fallback_models = list(
            fallback_models
            if fallback_models is not None
            else (cfg.get("tts.fallback_models", list(DEFAULT_FALLBACK_MODELS)) or [])
        )
        self.backend = backend or cfg.get("tts.backend", "auto")
        self.vocoder = vocoder if vocoder is not None else cfg.get("tts.vocoder", "")
        self.voice = voice or cfg.get("tts.voice", "default")
        self.speaker_id = int(speaker_id if speaker_id is not None else cfg.get("tts.speaker_id", 0) or 0)
        self.guest_speaker_id = int(
            guest_speaker_id
            if guest_speaker_id is not None
            else cfg.get("tts.guest_speaker_id", 1) or 1
        )
        self.speed = float(speed if speed is not None else cfg.get("tts.speed", 1.0) or 1.0)
        self.guest_speed = float(
            guest_speed if guest_speed is not None else cfg.get("tts.guest_speed", 1.06) or 1.06
        )
        # Voice pitch, as a frequency ratio (0.9 = ~1.5 semitones down). Neither
        # shipped model exposes a pitch control and sherpa's `speed` is
        # pitch-preserving (measured: log-frequency spectra of speed 1.0 vs 0.75
        # align at ratio 1.000), so the shift is done by relabelling the audio's
        # sample rate and compensating the tempo in `generate()` — see
        # `synthesize`.
        self.pitch = float(
            pitch if pitch is not None else cfg.get("tts.pitch", 1.0) or 1.0
        )
        self.pitch = min(2.0, max(0.5, self.pitch))
        self.num_threads = int(
            num_threads if num_threads is not None else cfg.get("tts.num_threads", 2) or 2
        )
        self.silence_scale = silence_scale
        self.chunk_ms = chunk_ms

        # Segmentation, pauses and trim thresholds all live in one config object
        # so the engine and `scripts/show_segmentation.py` cannot disagree.
        self.speech = speech if speech is not None else speech_text_config(cfg)

        self.model: Optional[TtsModel] = None
        self._tts = None
        self._executor: Optional[ThreadPoolExecutor] = None
        self._ready = False
        self._interrupted = threading.Event()

    # ── lifecycle ──────────────────────────────────────────────

    async def initialize(self) -> None:
        self.model = resolve_tts_model(
            self.model_path,
            self.fallback_models,
            backend=self.backend,
            vocoder=self.vocoder,
        )
        self._tts = build_offline_tts(self.model, self.num_threads, self.silence_scale)
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tts")
        self._ready = True

        n_speakers = int(getattr(self._tts, "num_speakers", 1) or 1)
        if self.speaker_id >= n_speakers:
            logger.warning(
                "tts_speaker_id_out_of_range",
                requested=self.speaker_id,
                available=n_speakers,
            )
            self.speaker_id = 0
        if self.guest_speaker_id >= n_speakers:
            self.guest_speaker_id = n_speakers - 1 if n_speakers > 1 else 0

        if self.model.directory.resolve() != _as_dir(self.model_path).resolve():
            # Not an error: the fallback model exists and works. But the user
            # asked for a specific model and should know they are not hearing it.
            logger.warning(
                "tts_primary_model_missing",
                requested=str(self.model_path),
                selected=str(self.model.directory),
                backend=self.model.backend,
            )

        logger.info(
            "tts_initialized",
            model=str(self.model.model_file),
            backend=self.model.backend,
            sample_rate=self.sample_rate,
            num_speakers=n_speakers,
            speed=self.speed,
            pitch=self.pitch,
            trim_silence=self.speech.trim_silence,
            max_internal_gap_ms=self.speech.max_internal_gap_ms,
            first_chunk_max_chars=self.speech.first_chunk_max_chars,
            clause_max_chars=self.speech.clause_max_chars,
        )

    async def close(self) -> None:
        """Release the worker thread; the model itself is left loaded."""
        executor, self._executor = self._executor, None
        if executor is not None:
            executor.shutdown(wait=False)

    @property
    def sample_rate(self) -> int:
        return int(getattr(self._tts, "sample_rate", 0) or 24000)

    @property
    def num_speakers(self) -> int:
        return int(getattr(self._tts, "num_speakers", 1) or 1)

    @property
    def ready(self) -> bool:
        return self._ready

    # ── synthesis ──────────────────────────────────────────────

    async def synthesize(self, request: TtsRequest) -> AsyncIterator[TtsChunk]:
        """
        Yield the reply as ~100 ms PCM chunks, segment by segment.

        Each segment's final chunk carries `pause_after_ms`; the caller's player
        writes that much silence before the next one starts.
        """
        self._interrupted.clear()

        if not self._ready:
            raise RuntimeError("TtsEngine.initialize() must be awaited first")
        if not request.text or not request.text.strip():
            return

        text = normalize_for_speech(request.text, max_chars=request.max_chars)
        if not text.strip():
            logger.info("tts_nothing_speakable", chars=len(request.text))
            return

        segments: list[SpeechSegment] = segment_for_speech(text, self.speech)
        if not segments:
            logger.info("tts_no_segments", chars=len(text))
            return

        sid = self._resolve_speaker(request.voice)
        # The pitch shift is a relabelled sample rate: chunks claim
        # `model_rate × pitch`, so the player stretches them by 1/pitch (pitch
        # moves by `pitch`, tempo by 1/pitch). Generating at `speed / pitch`
        # pays the tempo back, which is what keeps `tts.speed` meaning tempo
        # and `tts.pitch` meaning pitch — two independent knobs.
        rate = int(round(self.sample_rate * self.pitch)) or self.sample_rate
        speed = self._speed_for(request.voice) / self.pitch
        loop = asyncio.get_running_loop()
        started = time.perf_counter()
        spoken_ms = 0
        first_segment_ms: Optional[float] = None

        for index, segment in enumerate(segments):
            if self._interrupted.is_set():
                logger.info("tts_interrupted", at_segment=index, of=len(segments))
                return

            begin = time.perf_counter()
            samples = await loop.run_in_executor(
                self._executor, self._generate, segment.text, sid, speed
            )
            latency = time.perf_counter() - begin
            if first_segment_ms is None:
                first_segment_ms = latency
                observe_latency("tts_first_segment", latency)
            observe_latency("tts_segment", latency)

            audio_ms = int(samples.size / rate * 1000) if rate else 0
            spoken_ms += audio_ms
            logger.info(
                "tts_segment",
                index=index,
                of=len(segments),
                chars=segment.chars,
                kind=segment.kind,
                pause_after_ms=segment.pause_after_ms,
                audio_ms=audio_ms,
                latency_ms=int(latency * 1000),
            )

            chunk_samples = max(1, int(self.sample_rate * self.chunk_ms / 1000))
            total = samples.size
            for offset in range(0, total, chunk_samples):
                if self._interrupted.is_set():
                    logger.info("tts_interrupted", at_segment=index, of=len(segments))
                    return
                piece = samples[offset : offset + chunk_samples]
                last_of_segment = offset + chunk_samples >= total
                yield TtsChunk(
                    data=float32_to_pcm(piece),
                    is_final=last_of_segment and index == len(segments) - 1,
                    sample_rate=rate,
                    pause_after_ms=segment.pause_after_ms if last_of_segment else 0,
                    text=segment.text,
                )

        latency = time.perf_counter() - started
        inc_request("tts", "success")
        logger.info(
            "tts_generated",
            chars=len(text),
            segments=len(segments),
            audio_ms=spoken_ms,
            pause_ms=sum(segment.pause_after_ms for segment in segments),
            latency_ms=int(latency * 1000),
            first_segment_ms=int((first_segment_ms or 0.0) * 1000),
        )

    def _generate(self, text: str, sid: int, speed: float) -> np.ndarray:
        """One `generate()` plus the edge trim and gap collapse — worker thread."""
        audio = self._tts.generate(text, sid=sid, speed=speed)
        samples = np.asarray(audio.samples, dtype=np.float32)
        true_rate = int(getattr(audio, "sample_rate", 0) or self.sample_rate)
        if self.speech.trim_silence:
            samples = trim_edge_silence(
                samples,
                true_rate,
                ratio=self.speech.trim_ratio,
                guard_ms=self.speech.trim_guard_ms,
            )
        # After the edges, the model's own mid-sentence silences: Matcha leaves
        # 1–7 of them per segment (40–240 ms) wherever it pleases, and heard
        # through the pause table's rhythm they read as stuttering.
        return collapse_internal_gaps(
            samples,
            true_rate,
            max_gap_ms=self.speech.max_internal_gap_ms,
            keep_ms=self.speech.internal_gap_keep_ms,
            ratio=self.speech.trim_ratio,
        )

    def _resolve_speaker(self, voice: str) -> int:
        """Map a voice name to a speaker id (the owner's voice by default)."""
        if voice == "guest" and self.num_speakers > 1:
            return self.guest_speaker_id % self.num_speakers
        return self.speaker_id

    def _speed_for(self, voice: str) -> float:
        """
        The guest's speed, but only when the voice cannot differ by speaker.

        A single-speaker model (matcha zh-baker) gives the guest the same voice,
        and a slightly different pace is the only honest way left to tell the two
        apart. When the model *does* have multiple speakers the id already
        distinguishes them, and changing the pace as well would just sound like
        the guest is being hurried.
        """
        if voice == "guest" and self.num_speakers <= 1:
            return self.guest_speed
        return self.speed

    # ── interruption ───────────────────────────────────────────

    def interrupt(self) -> None:
        """
        Signal barge-in: production stops at the next chunk boundary.

        A `generate()` call already running on the worker thread cannot be
        cancelled from outside sherpa-onnx, so that one call finishes (its result
        is then dropped rather than yielded). What this guarantees is that **no
        new segment is started** and that the remaining audio stops within one
        chunk — the bound on how long the assistant keeps producing speech is one
        segment, not the whole reply.
        """
        self._interrupted.set()

    def is_interrupted(self) -> bool:
        return self._interrupted.is_set()


class StubTtsEngine(TtsEngine):
    """
    Model-free stand-in for `--stub-audio` runs.

    It runs the *real* segmentation and emits silence of the right length per
    segment, so the pause plumbing (and everything downstream of it) is
    exercised by tests that have no model.
    """

    def __init__(self, speech: Optional[SpeechTextConfig] = None, chunk_ms: int = CHUNK_MS):
        self.model_path = "stub"
        self.fallback_models: list[str] = []
        self.backend = "stub"
        self.vocoder = ""
        self.voice = "default"
        self.speaker_id = 0
        self.guest_speaker_id = 0
        self.speed = 1.0
        self.guest_speed = 1.0
        self.num_threads = 1
        self.silence_scale = 0.0
        self.chunk_ms = chunk_ms
        self.speech = speech if speech is not None else SpeechTextConfig()
        self.model: Optional[TtsModel] = None
        self._tts = None
        self._executor = None
        self._ready = True
        self._interrupted = threading.Event()

    async def initialize(self) -> None:
        logger.info("stub_tts_initialized")

    @property
    def sample_rate(self) -> int:
        return 16000

    @property
    def num_speakers(self) -> int:
        return 1

    async def synthesize(self, request: TtsRequest) -> AsyncIterator[TtsChunk]:
        self._interrupted.clear()

        text = normalize_for_speech(request.text or "", max_chars=request.max_chars)
        segments = segment_for_speech(text, self.speech) if text.strip() else []
        if not segments:
            return

        rate = self.sample_rate
        for index, segment in enumerate(segments):
            if self._interrupted.is_set():
                return
            # 100 ms of silence per ~10 characters, so the stub's timing is at
            # least in the same order of magnitude as real speech.
            audio_ms = max(100, segment.chars * 25)
            samples = np.zeros(int(rate * audio_ms / 1000), dtype=np.float32)
            chunk_samples = max(1, int(rate * self.chunk_ms / 1000))
            total = samples.size
            for offset in range(0, total, chunk_samples):
                if self._interrupted.is_set():
                    return
                piece = samples[offset : offset + chunk_samples]
                last_of_segment = offset + chunk_samples >= total
                yield TtsChunk(
                    data=float32_to_pcm(piece),
                    is_final=last_of_segment and index == len(segments) - 1,
                    sample_rate=rate,
                    pause_after_ms=segment.pause_after_ms if last_of_segment else 0,
                    text=segment.text,
                )


def create_tts_engine(use_stub: bool = False) -> TtsEngine:
    """Factory honouring the WINVOICE_STUB_AUDIO env var."""
    cfg = get_config()
    speech = speech_text_config(cfg)
    if use_stub or os.getenv("WINVOICE_STUB_AUDIO") == "1":
        return StubTtsEngine(speech=speech)
    return TtsEngine(speech=speech)


__all__ = [
    "CHUNK_MS",
    "DEFAULT_FALLBACK_MODELS",
    "DEFAULT_MODEL",
    "StubTtsEngine",
    "TtsChunk",
    "TtsEngine",
    "TtsModel",
    "build_offline_tts",
    "create_tts_engine",
    "resolve_tts_model",
]
