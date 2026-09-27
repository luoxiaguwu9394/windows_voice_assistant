"""
Shared helpers for the sherpa-onnx audio engines.

Centralises model-file resolution, int16<->float32 conversion, and WAV
loading so every engine behaves identically and reports clean errors when
a model is missing.
"""

from __future__ import annotations

import wave
from pathlib import Path
from typing import List, Optional, Sequence

import numpy as np

from winvoice.logging import get_logger

logger = get_logger(__name__)

INT16_SCALE = 32768.0


class ModelNotFoundError(FileNotFoundError):
    """Raised when a required model file or directory is absent."""


def to_float32(pcm: bytes) -> np.ndarray:
    """Convert int16 little-endian PCM bytes to float32 in [-1, 1]."""
    return np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / INT16_SCALE


def frames_to_float32(frames: Sequence) -> np.ndarray:
    """
    Concatenate int16 little-endian PCM into one float32 array.

    Callers hand this function either a sequence of raw `bytes` blocks (the
    pipeline's microphone buffer, `AudioPipeline._recent`) or a sequence of
    objects exposing `.data` (`AudioFrame`, used by the offline paths), so
    both are accepted. Passing a float32 `np.ndarray` is deliberately NOT
    supported: it would be reinterpreted as int16 and silently corrupted.
    """
    if frames is None:
        return np.zeros(0, dtype=np.float32)
    if isinstance(frames, (bytes, bytearray, memoryview)):
        return to_float32(bytes(frames))

    blocks: List[bytes] = []
    for frame in frames:
        if isinstance(frame, (bytes, bytearray, memoryview)):
            blocks.append(bytes(frame))
        elif hasattr(frame, "data"):
            blocks.append(bytes(frame.data))
        else:
            raise TypeError(
                f"frame must be bytes or expose .data, got {type(frame).__name__}"
            )

    if not blocks:
        return np.zeros(0, dtype=np.float32)
    return to_float32(b"".join(blocks))


def float32_to_pcm(samples: np.ndarray) -> bytes:
    """Convert float32 in [-1, 1] to int16 little-endian PCM bytes."""
    clipped = np.clip(samples, -1.0, 1.0)
    return (clipped * 32767.0).astype(np.int16).tobytes()


def trim_edge_silence(
    samples: np.ndarray,
    sample_rate: int,
    *,
    ratio: float = 0.02,
    guard_ms: int = 30,
    window_ms: int = 10,
    min_keep_ms: int = 50,
) -> np.ndarray:
    """
    Drop the near-silence a TTS model puts in front of and behind its speech.

    Measured on both shipped models (`scripts/calibrate_tts_pauses.py`): every
    synthesis call begins and ends with something that is *not* digital silence
    but a noise floor — Matcha 60–110 ms per end at 0.24–1.68 % of peak, the
    8 kHz VITS 80–250 ms at 0.78–1.74 %. A per-sample threshold does not see it
    at all; a 10 ms RMS envelope does. Left in place, that dead air is added to
    every pause the reply is supposed to have, so the rhythm stops being the one
    the pause table asked for.

    Only the *edges* are touched: a 40–190 ms gap inside a sentence is the
    model's own phrasing and is left alone. `guard_ms` keeps a little of the
    ramp around the detected speech so an onset is never clipped, and an input
    that is quiet all the way through is returned unchanged — trimming away a
    segment the analysis does not understand would be worse than a little dead
    air.
    """
    if samples is None or samples.size == 0 or ratio <= 0:
        return samples

    peak = float(np.abs(samples).max())
    if peak <= 0.0:
        return samples

    window = max(1, int(sample_rate * window_ms / 1000))
    windows = samples.size // window
    if windows < 2:
        return samples

    frames = samples[: windows * window].reshape(windows, window)
    envelope = np.sqrt((frames**2).mean(axis=1))

    voiced = np.flatnonzero(envelope >= ratio * peak)
    if voiced.size == 0:
        # Nothing stands out as speech: the input is not understood well enough
        # to be trimmed, so it is passed through untouched.
        return samples

    guard = max(0, int(sample_rate * guard_ms / 1000))
    start = max(0, int(voiced[0]) * window - guard)
    end = min(samples.size, (int(voiced[-1]) + 1) * window + guard)
    if end - start < int(sample_rate * min_keep_ms / 1000):
        return samples
    return samples[start:end]


def collapse_internal_gaps(
    samples: np.ndarray,
    sample_rate: int,
    *,
    max_gap_ms: int = 110,
    keep_ms: int = 80,
    ratio: float = 0.02,
    window_ms: int = 10,
) -> np.ndarray:
    """
    Shorten the *internal* silences a TTS model scatters through a segment.

    Measured on Matcha (`scripts/calibrate_tts_pauses.py` and gap analysis on
    typical replies): one `generate()` carries 1–7 internal gaps of 40–240 ms,
    placed wherever the model feels like pausing — its lexicon holds no
    punctuation, so it cannot see the comma it is pausing for. A 180 ms hole in
    the middle of 「但是结果说不清楚」 is what the listener reports as
    "stuttering, many breakpoints in one sentence".

    Every quiet run **strictly inside** the spoken span that is longer than
    `max_gap_ms` is cut down to `keep_ms`; shorter runs are the voice's natural
    phrasing (40–80 ms) and are left alone, and the edges belong to
    `trim_edge_silence`. Cuts land on 10 ms window boundaries of a region that
    is already below `ratio` × peak — a step between two near-silence
    amplitudes is inaudible, so no crossfade is needed. `max_gap_ms=0` disables
    the collapse entirely.
    """
    if samples is None or samples.size == 0 or max_gap_ms <= 0:
        return samples

    peak = float(np.abs(samples).max())
    if peak <= 0.0:
        return samples

    window = max(1, int(sample_rate * window_ms / 1000))
    windows = samples.size // window
    if windows < 3:
        return samples

    envelope = np.sqrt(
        (samples[: windows * window].reshape(windows, window) ** 2).mean(axis=1)
    )
    quiet = envelope < ratio * peak
    voiced = np.flatnonzero(~quiet)
    if voiced.size < 2:
        return samples

    # Only gaps between the first and last voiced window count as internal; the
    # head and tail are the trimmer's business.
    lo, hi = int(voiced[0]), int(voiced[-1])
    keep_windows = max(1, int(keep_ms / window_ms))
    max_windows = max(keep_windows, int(max_gap_ms / window_ms))

    # One pass: note the over-long gaps, then rebuild without their middles.
    cuts: list[tuple[int, int]] = []  # [keep_up_to_window, resume_window)
    index = lo
    while index < hi:
        if quiet[index]:
            end = index
            while end < hi and quiet[end]:
                end += 1
            if end > index and (end - index) > max_windows:
                # Keep the first `keep_windows` of the gap and drop the rest:
                # the onset after a pause prefers rising out of the same floor.
                cuts.append((min(index + keep_windows, end), end))
            index = end
        else:
            index += 1

    if not cuts:
        return samples

    pieces: list[np.ndarray] = []
    cursor = 0
    for keep_to, resume in cuts:
        pieces.append(samples[cursor : keep_to * window])
        cursor = resume * window
    pieces.append(samples[cursor:])
    return np.concatenate(pieces)


def resolve_path(value: str | Path, *, kind: str = "file") -> Path:
    """
    Resolve a configured model path, raising ModelNotFoundError with a
    helpful message (including the exact download command) when absent.
    """
    p = Path(value).expanduser()
    if not p.is_absolute():
        p = Path.cwd() / p

    exists = p.exists() if kind == "file" else p.is_dir()
    if not exists:
        raise ModelNotFoundError(
            f"{kind} not found: {p}\n"
            f"  Download models with:  python scripts/download_models.py --all\n"
            f"  List available models: python scripts/download_models.py --list"
        )
    return p


def pick_in_dir(model_dir: Path, *patterns: str, required: bool = True) -> Optional[Path]:
    """
    Return the first file in `model_dir` matching any glob pattern.
    Patterns are tried in order, so list preferred (e.g. fp32) names first.
    """
    for pattern in patterns:
        matches = sorted(model_dir.glob(pattern))
        if matches:
            return matches[0]
    if required:
        raise ModelNotFoundError(
            f"none of {patterns} found in {model_dir}\n"
            f"  Re-download with: python scripts/download_models.py --all"
        )
    return None


def read_wav(path: str | Path) -> tuple[np.ndarray, int]:
    """
    Read a mono/stereo 16-bit PCM WAV as float32 mono.

    Returns (samples, sample_rate). Used by tests and offline tools; the live
    pipeline feeds frames straight from the microphone instead.
    """
    with wave.open(str(path), "rb") as w:
        n_channels = w.getnchannels()
        sample_width = w.getsampwidth()
        sample_rate = w.getframerate()
        n_frames = w.getnframes()
        raw = w.readframes(n_frames)

    if sample_width != 2:
        raise ValueError(f"{path}: expected 16-bit PCM, got {sample_width * 8}-bit")

    samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / INT16_SCALE
    if n_channels > 1:
        samples = samples.reshape(-1, n_channels).mean(axis=1)

    return samples, sample_rate


__all__ = [
    "ModelNotFoundError",
    "to_float32",
    "frames_to_float32",
    "float32_to_pcm",
    "collapse_internal_gaps",
    "trim_edge_silence",
    "resolve_path",
    "pick_in_dir",
    "read_wav",
]
