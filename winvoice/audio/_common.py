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
    """Concatenate AudioFrame.data (int16 bytes) into one float32 array."""
    if not frames:
        return np.zeros(0, dtype=np.float32)
    raw = b"".join(f.data for f in frames)
    return to_float32(raw)


def float32_to_pcm(samples: np.ndarray) -> bytes:
    """Convert float32 in [-1, 1] to int16 little-endian PCM bytes."""
    clipped = np.clip(samples, -1.0, 1.0)
    return (clipped * 32767.0).astype(np.int16).tobytes()


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
    "resolve_path",
    "pick_in_dir",
    "read_wav",
]
