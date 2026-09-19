"""
Regression: the speaker-verification path must accept the audio the pipeline
actually hands it.

`AudioPipeline._recent` / `_audio_queue` hold int16 PCM `bytes` blocks (see
`push_audio` and `run`), and `_begin_utterance` passes them straight to
`SvEngine.verify`. `frames_to_float32` assumed every element exposed `.data`
(an `AudioFrame`), so the live path died with

    pipeline_tick_failed error="'bytes' object has no attribute 'data'"
    module=winvoice.audio.pipeline state=kws_listening

on every wake word once speaker verification was enabled and a speaker was
enrolled. The wake word was heard, the utterance was silently dropped, and the
error was swallowed by the per-tick handler, so the assistant just went deaf.

Engine inputs are bytes-first everywhere else (`KwsEngine.accept_waveform`,
`VadEngine.accept_waveform`, `AsrEngine.transcribe`), so the shared conversion
helper is where the two conventions have to meet.
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import numpy as np
import pytest
import structlog

from winvoice.audio._common import frames_to_float32
from winvoice.audio.kws import KwsResult
from winvoice.audio.pipeline import AudioPipeline
from winvoice.audio.sv import SpeakerProfile, SvEngine

REPO_ROOT = Path(__file__).resolve().parents[2]

# 100 ms at 16 kHz, matching audio.blocksize / the microphone callback.
BLOCK_SAMPLES = 1600
BLOCK_BYTES = BLOCK_SAMPLES * 2

SV_MODEL = REPO_ROOT / "models" / "sv" / "3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx"

# Scratch profile dir for the real engine. pytest's own tmp_path is unusable
# when the sandbox redirects temp, and this must never touch the real
# models/sv/profiles.
SCRATCH = REPO_ROOT / ".pytmp_sv_frames"


# ──────────────────────────────────────────────────────────────
# The conversion helper
# ──────────────────────────────────────────────────────────────

def test_frames_to_float32_accepts_raw_pcm_bytes():
    """The pipeline's buffer holds `bytes`; the helper must accept them."""
    block = np.arange(BLOCK_SAMPLES, dtype=np.int16).tobytes()

    samples = frames_to_float32([block, block])

    assert samples.dtype == np.float32
    assert samples.size == 2 * BLOCK_SAMPLES
    assert np.allclose(samples[:BLOCK_SAMPLES], np.arange(BLOCK_SAMPLES, dtype=np.float32) / 32768.0)


def test_frames_to_float32_still_accepts_frame_objects():
    """The enrolled CLI path passes objects exposing `.data`."""

    class Frame:
        def __init__(self, data: bytes) -> None:
            self.data = data

    block = np.arange(BLOCK_SAMPLES, dtype=np.int16).tobytes()

    assert frames_to_float32([Frame(block)]).size == BLOCK_SAMPLES


def test_frames_to_float32_handles_empty_input():
    assert frames_to_float32([]).size == 0
    assert frames_to_float32(b"").size == 0


# ──────────────────────────────────────────────────────────────
# The call site: wake word -> speaker verification
# ──────────────────────────────────────────────────────────────

class _FiringKws:
    """KWS stand-in that fires the wake word after `fire_after` blocks."""

    def __init__(self, fire_after: int = 15) -> None:
        self.fire_after = fire_after
        self.blocks = 0
        self.fired = False

    def accept_waveform(self, chunk) -> None:
        self.blocks += 1

    def get_result(self):
        if self.fired or self.blocks < self.fire_after:
            return None
        self.fired = True
        return KwsResult(keyword="assistant", confidence=1.0, timestamp_ms=0)

    def reset(self) -> None:
        pass


@pytest.mark.skipif(not SV_MODEL.exists(), reason="SV model not downloaded")
@pytest.mark.asyncio
async def test_wake_word_survives_real_speaker_verification():
    """
    Drive `AudioPipeline.run` with the real SvEngine, exactly as the app wires
    it. Red before the fix: every block raised AttributeError inside
    `_tick_kws` -> `_begin_utterance`, and `run` logged `pipeline_tick_failed`
    while the state was still `kws_listening`.
    """
    shutil.rmtree(SCRATCH, ignore_errors=True)
    SCRATCH.mkdir(parents=True, exist_ok=True)

    sv = SvEngine(profiles_dir=str(SCRATCH), adaptive_update=False)
    await sv.initialize()
    assert sv.embedding_dim > 0

    rng = np.random.default_rng(0)
    emb = rng.normal(size=sv.embedding_dim).astype(np.float32)
    sv._profiles["me"] = SpeakerProfile(
        speaker_id="me",
        embeddings=[emb / np.linalg.norm(emb)],
        threshold_high=0.60,
        threshold_low=0.40,
    )

    pipe = AudioPipeline(use_stub=True)
    await pipe.initialize()
    pipe.sv = sv  # production wiring: real speaker verification
    pipe.kws = _FiringKws()

    # Spy on the boundary the pipeline calls, so the test can tell "verify ran"
    # apart from "verify was never reached".
    calls: list[tuple[list, object]] = []
    real_verify = sv.verify

    def spy(frames, speaker_id: str = "me"):
        result = real_verify(frames, speaker_id)
        calls.append((list(frames), result))
        return result

    sv.verify = spy

    pcm = (rng.normal(size=BLOCK_SAMPLES * 25) * 8000).astype(np.int16).tobytes()
    for offset in range(0, len(pcm), BLOCK_BYTES):
        pipe.push_audio(pcm[offset:offset + BLOCK_BYTES])

    task = asyncio.create_task(pipe.run())
    with structlog.testing.capture_logs() as logs:
        for _ in range(400):
            await asyncio.sleep(0.01)
            if calls:
                break
        pipe.stop()
        await asyncio.wait_for(task, timeout=5)

    failures = [entry for entry in logs if entry.get("event") == "pipeline_tick_failed"]
    assert failures == [], f"pipeline tick failed: {failures}"

    assert calls, "the wake word never reached speaker verification"
    frames, result = calls[0]
    assert frames and all(isinstance(f, bytes) for f in frames)
    # Non-None proves the real extractor consumed the converted frames rather
    # than the helper quietly returning silence.
    assert result is not None, "speaker verification produced no result"


@pytest.fixture(autouse=True, scope="module")
def _cleanup_scratch():
    yield
    shutil.rmtree(SCRATCH, ignore_errors=True)
