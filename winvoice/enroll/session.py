#!/usr/bin/env python3
"""
Speaker Enrollment CLI.

Records N samples, computes CAM++ embeddings, and derives the two
verification thresholds.

Usage:
    python -m winvoice.enroll --speaker me --samples 8
"""

from __future__ import annotations

import argparse
import asyncio
import time
from pathlib import Path

import numpy as np
import sounddevice as sd

from winvoice.audio import create_kws_engine, create_sv_engine
from winvoice.config import get_config, reset_config
from winvoice.logging import configure_logging, get_logger

logger = get_logger(__name__)

SAMPLE_RATE = 16000
CHUNK = 1600  # 100 ms at 16 kHz


class EnrollmentSession:
    """Drives the interactive enrollment flow."""

    def __init__(self, speaker_id: str, num_samples: int = 8, sample_duration: float = 4.0):
        self.speaker_id = speaker_id
        self.num_samples = num_samples
        self.sample_duration = sample_duration
        self.sample_rate = SAMPLE_RATE
        self.sv_engine = create_sv_engine(use_stub=False)
        # KWS is initialized too so a broken wake-word model surfaces here
        # rather than at first run of the assistant.
        self.kws_engine = create_kws_engine(use_stub=False)

    async def initialize(self, force: bool = False) -> None:
        await self.sv_engine.initialize()
        await self.kws_engine.initialize()
        self.sv_engine.enroll_start(self.speaker_id, force=force)
        logger.info("enrollment_started", speaker_id=self.speaker_id, samples=self.num_samples)

    def record_sample(self, sample_num: int) -> np.ndarray:
        """Record one sample and return float32 mono audio."""
        print(f"\n[*] Sample {sample_num}/{self.num_samples} - speak for {self.sample_duration:.0f}s")
        print("    (vary wording, volume and distance)")

        for i in range(3, 0, -1):
            print(f"    starting in {i}...", end="\r", flush=True)
            time.sleep(1)
        print("    recording...          ", flush=True)

        blocks: list[np.ndarray] = []
        n_chunks = max(1, int(self.sample_rate * self.sample_duration / CHUNK))

        def callback(indata, _frames, _time, status):
            if status:
                logger.warning("audio_callback_status", status=str(status))
            blocks.append(indata.copy())

        with sd.InputStream(
            samplerate=self.sample_rate,
            channels=1,
            dtype="int16",
            blocksize=CHUNK,
            callback=callback,
        ):
            for _ in range(n_chunks):
                sd.sleep(100)

        print("    done.")

        if not blocks:
            return np.zeros(0, dtype=np.float32)

        audio = np.concatenate(blocks, axis=0).reshape(-1).astype(np.float32) / 32768.0
        return audio

    def add_sample(self, audio: np.ndarray) -> bool:
        return self.sv_engine.enroll_sample(self.speaker_id, audio)

    def finalize(self, max_inter: float):
        return self.sv_engine.enroll_finalize(self.speaker_id, max_inter=max_inter)


async def main() -> int:
    parser = argparse.ArgumentParser(description="Speaker enrollment for Windows Voice Assistant")
    parser.add_argument("--speaker", default="me", help="speaker id (default: me)")
    parser.add_argument("--samples", type=int, default=8, help="number of samples (default: 8)")
    parser.add_argument("--duration", type=float, default=4.0, help="seconds per sample (default: 4.0)")
    parser.add_argument("--max-inter", type=float, default=0.45,
                        help="estimated max similarity to a non-target speaker (default: 0.45)")
    parser.add_argument("--config", default="config/config.yaml", help="config file path")
    parser.add_argument("--force", action="store_true", help="overwrite an existing enrollment")
    args = parser.parse_args()

    reset_config()
    cfg = get_config(args.config)
    configure_logging(process_name="enroll", level="INFO")

    print("=" * 64)
    print("Speaker enrollment")
    print(f"  speaker          : {args.speaker}")
    print(f"  samples          : {args.samples} x {args.duration:.0f}s")
    print(f"  min speech floor : 0.40 (samples below this are rejected)")
    print("=" * 64)

    session = EnrollmentSession(args.speaker, args.samples, args.duration)

    try:
        await session.initialize(force=args.force)
    except Exception as e:
        print(f"\n[X] Initialization failed: {e}")
        return 2

    for i in range(1, args.samples + 1):
        attempts = 0
        while True:
            attempts += 1
            audio = session.record_sample(i)
            if audio.size == 0:
                print("    [!] no audio captured - is the microphone working?")
                if attempts >= 3:
                    return 3
                continue
            if session.add_sample(audio):
                break
            print("    [!] embedding failed, retrying...")
            if attempts >= 3:
                return 3

    try:
        profile = session.finalize(args.max_inter)
    except ValueError as e:
        print(f"\n[X] Enrollment failed: {e}")
        return 1

    print("\n" + "=" * 64)
    print(f"[OK] Enrollment complete for '{args.speaker}'")
    print(f"     samples         : {len(profile.embeddings)}")
    print(f"     threshold HIGH  : {profile.threshold_high:.3f}")
    print(f"     threshold LOW   : {profile.threshold_low:.3f}")
    print(f"     profile saved   : models/sv/profiles/{args.speaker}.json")
    print("=" * 64)
    return 0
