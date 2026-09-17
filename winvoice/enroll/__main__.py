#!/usr/bin/env python3
"""
Speaker Enrollment CLI.

Records 8 samples, computes embeddings, derives thresholds.
Usage: python -m winvoice.enroll --speaker me --samples 8
"""

from __future__ import annotations

import argparse
import asyncio
import time
import uuid
from pathlib import Path

import numpy as np
import sounddevice as sd

from winvoice.config import get_config, reset_config
from winvoice.logging import configure_logging, get_logger
from winvoice.audio.sv import create_sv_engine
from winvoice.audio.kws import create_kws_engine
from winvoice.contracts import AudioFrame

logger = get_logger(__name__)


class EnrollmentSession:
    """Manages speaker enrollment process."""

    def __init__(self, speaker_id: str, num_samples: int = 8, sample_duration: float = 4.0):
        self.speaker_id = speaker_id
        self.num_samples = num_samples
        self.sample_duration = sample_duration
        self.sample_rate = 16000
        self.sv_engine = create_sv_engine(use_stub=False)
        self.kws_engine = create_kws_engine(use_stub=False)

    async def initialize(self) -> None:
        await self.sv_engine.initialize()
        await self.kws_engine.initialize()
        self.sv_engine.enroll_start(self.speaker_id)
        logger.info("enrollment_started", speaker_id=self.speaker_id, samples=self.num_samples)

    def record_sample(self, sample_num: int) -> AudioFrame:
        """Record one enrollment sample."""
        print(f"\n🎤 Sample {sample_num}/{self.num_samples} - Speak for {self.sample_duration}s...")
        print("   (Vary content, volume, and distance)")

        # Countdown
        for i in range(3, 0, -1):
            print(f"   Starting in {i}...", end="\r")
            time.sleep(1)
        print("   Recording!          ")

        # Record audio
        frames = []
        chunk_size = 1600  # 100ms at 16kHz
        num_chunks = int(self.sample_rate * self.sample_duration / chunk_size)

        def callback(indata, frames_count, time_info, status):
            if status:
                logger.warning("audio_callback_status", status=str(status))
            frames.append(indata.copy())

        with sd.InputStream(
            samplerate=self.sample_rate,
            channels=1,
            dtype="int16",
            blocksize=chunk_size,
            callback=callback,
        ):
            for _ in range(num_chunks):
                sd.sleep(100)

        print("   Done recording.")

        # Combine frames
        audio_data = np.concatenate(frames, axis=0).tobytes()
        timestamp_ms = int(time.time() * 1000)

        return AudioFrame(
            trace_id=uuid.uuid4().hex[:16],
            timestamp_ms=timestamp_ms,
            data=audio_data,
        )

    def add_sample(self, frame: AudioFrame) -> bool:
        """Add sample to enrollment."""
        success = self.sv_engine.enroll_sample(self.speaker_id, [frame])
        if success:
            count = len(self.sv_engine._profiles[self.speaker_id].embeddings)
            logger.info("enrollment_sample_added", speaker_id=self.speaker_id, count=count)
        return success

    def finalize(self, max_inter: float = 0.45):
        """Finalize enrollment and compute thresholds."""
        profile = self.sv_engine.enroll_finalize(self.speaker_id, max_inter)
        return profile


async def main():
    parser = argparse.ArgumentParser(description="Speaker enrollment for Windows Voice Assistant")
    parser.add_argument("--speaker", default="me", help="Speaker ID")
    parser.add_argument("--samples", type=int, default=8, help="Number of samples (default: 8)")
    parser.add_argument("--duration", type=float, default=4.0, help="Duration per sample in seconds (default: 4.0)")
    parser.add_argument("--max-inter", type=float, default=0.45, help="Estimated max inter-speaker similarity")
    parser.add_argument("--config", default="config/config.yaml", help="Config file path")
    parser.add_argument("--stub-audio", action="store_true", help="Use stub audio engines")
    args = parser.parse_args()

    # Setup
    reset_config()
    get_config(args.config)
    configure_logging(process_name="enroll", level="INFO")

    if args.stub_audio:
        import os
        os.environ["WINVOICE_STUB_AUDIO"] = "1"

    session = EnrollmentSession(
        speaker_id=args.speaker,
        num_samples=args.samples,
        sample_duration=args.duration,
    )

    await session.initialize()

    # Record samples
    for i in range(1, args.samples + 1):
        while True:
            frame = session.record_sample(i)
            if session.add_sample(frame):
                break
            else:
                print("   ⚠️  Sample failed, retrying...")

    # Finalize
    try:
        profile = session.finalize(args.max_inter)
        print(f"\n✅ Enrollment complete for '{args.speaker}'")
        print(f"   Threshold HIGH: {profile.threshold_high:.3f}")
        print(f"   Threshold LOW:  {profile.threshold_low:.3f}")
        print(f"   Samples: {len(profile.embeddings)}")
    except ValueError as e:
        print(f"\n❌ Enrollment failed: {e}")
        print("   Please re-run enrollment with better samples.")
        return 1

    return 0


if __name__ == "__main__":
    exit(asyncio.run(main()))