#!/usr/bin/env python3
"""
Playback diagnosis: is the artifact ours or the device's?

Run this while the "layer of noise over the answer" is audible, and again when
it is clear, then compare the verdicts:

    python scripts/diagnose_playback.py

It synthesises a fixed reply through the real engine and the real output
device, while recording the Windows WASAPI render mix (loopback — needs
`pip install soundcard`). This is a digital endpoint capture, not an acoustic
recording of the physical speaker. Both signals are saved under
`runtime/playback_diag/` and analysed:

* **our data** — repeated fragments, per-segment resample seams, write stalls;
* **the WASAPI loopback** — stuck-buffer repeats (the driver replaying the same
  fragment: frame-to-frame correlation ≈ 1.0), glitches, timeline fidelity.

If our data is clean but the WASAPI loopback shows stuck repeats, the artifact
is produced below the application (driver state, another stream on the
endpoint, Bluetooth profile switch, enhancements) — no code change fixes that,
and the recording pair is the evidence to bring to the next session.

No microphone is needed; the reply is synthesised. This check cannot identify
acoustic-only clicks introduced by the speaker, room, or a phone microphone,
and its 20 ms repeat checks do not rule out sub-millisecond transients.
"""

from __future__ import annotations

import asyncio
import sys
import threading
import time
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

OUT_RATE = 48000
FRAME = int(OUT_RATE * 0.02)

REPLY = (
    "好的，已经帮你把记事本打开了。北京今天晴，气温十到二十度，现在十五度。"
    "我完成了操作，但是结果说不清楚，请看屏幕。接下来我会帮你检查一遍所有的设置，请稍等一下。"
)


def stuck_repeat_report(signal: np.ndarray) -> tuple[int, int]:
    """Frames whose content is ~identical to the previous frame's (replay signature)."""
    n = signal.size // FRAME
    if n < 3:
        return 0, 0
    frames = signal[: n * FRAME].reshape(n, FRAME)
    energy = np.sqrt((frames**2).mean(axis=1))
    voiced = energy > 0.02 * energy.max()
    a = frames[:-1] - frames[:-1].mean(axis=1, keepdims=True)
    b = frames[1:] - frames[1:].mean(axis=1, keepdims=True)
    na = np.linalg.norm(a, axis=1)
    nb = np.linalg.norm(b, axis=1)
    ok = (na > 1e-6) & (nb > 1e-6) & voiced[:-1] & voiced[1:]
    if not ok.any():
        return 0, 0
    corr = np.full(n - 1, np.nan)
    corr[ok] = (a[ok] * b[ok]).sum(axis=1) / (na[ok] * nb[ok])
    stuck = np.nan_to_num(corr > 0.98)
    longest = run = 0
    runs = 0
    for value in stuck:
        run = run + 1 if value else 0
        longest = max(longest, run)
        if run == 5:
            runs += 1
    return int(stuck.sum()), longest * 20  # ms


def repeated_fragments(signal: np.ndarray, probes: int = 40) -> int:
    """How many probe windows reappear elsewhere in the signal (correlation > 0.95)."""
    rng = np.random.default_rng(7)
    hits = 0
    window = int(OUT_RATE * 0.04)
    for start in rng.integers(window, max(window + 1, signal.size - 2 * OUT_RATE), probes):
        template = signal[start : start + window]
        if np.sqrt((template**2).sum()) < 1e-6:
            continue
        region = signal[start + window : start + window + OUT_RATE]
        for lag in range(0, region.size - window, window // 2):
            piece = region[lag : lag + window]
            denom = np.linalg.norm(piece) * np.linalg.norm(template)
            if denom > 1e-9 and float(piece @ template / denom) > 0.95:
                hits += 1
                break
    return hits


async def main() -> int:
    from soundcard import get_microphone, default_speaker  # noqa: PLC0415

    from winvoice.audio.playback import SpeechPlayer, open_output_stream  # noqa: PLC0415
    from winvoice.audio.tts import create_tts_engine  # noqa: PLC0415

    out_dir = REPO_ROOT / "runtime" / "playback_diag"
    out_dir.mkdir(parents=True, exist_ok=True)

    class Tee:
        def __init__(self, inner) -> None:
            self.inner = inner
            self.writes: list[np.ndarray] = []

        @property
        def samplerate(self) -> int:
            return self.inner.samplerate

        @property
        def channels(self) -> int:
            """The player writes a device buffer, so the wrapper must report the
            device's channel count — the speech is mono, the stream is not."""
            return int(getattr(self.inner, "channels", 1))

        def write(self, data: np.ndarray) -> None:
            self.writes.append(np.array(data, copy=True))
            self.inner.write(data)

        def start(self) -> None:
            self.inner.start()

        def abort(self) -> None:
            self.inner.abort()

        def stop(self) -> None:
            self.inner.stop()

        def close(self) -> None:
            self.inner.close()

    engine = create_tts_engine()
    await engine.initialize()
    box: dict[str, Tee] = {}

    def opener(rate: int, blocksize_ms: int) -> tuple[object, int]:
        stream, actual = open_output_stream(rate, blocksize_ms)
        box["tee"] = Tee(stream)
        return box["tee"], actual

    player = SpeechPlayer(sample_rate=0, device=None, host_api="auto", stream_open=opener)
    await player.start()

    speaker = default_speaker()
    loopback = get_microphone(speaker.id, include_loopback=True)
    captured: list[np.ndarray] = []
    stop = threading.Event()

    def capture() -> None:
        # One blocking record per second keeps the Python side out of the way
        # while letting the loop run until stopped.
        with loopback.recorder(samplerate=OUT_RATE, channels=2) as recorder:
            while not stop.is_set():
                captured.append(recorder.record(numframes=OUT_RATE)[:, 0])

    tape = threading.Thread(target=capture, daemon=True)
    tape.start()
    time.sleep(0.4)

    request = type("R", (), {"text": REPLY, "voice": "default", "max_chars": 240})()
    async for chunk in engine.synthesize(request):
        await player.enqueue(chunk.data, chunk.sample_rate, chunk.pause_after_ms)
    await player.wait_drained(timeout=60)
    await player.close()
    time.sleep(0.3)
    stop.set()
    tape.join(timeout=10)

    written = np.concatenate(box["tee"].writes).astype(np.float32) / 32767.0
    # The stream is opened multi-channel and each mono sample was replicated, so
    # unwrap one channel before analysing: the reports and both saved arrays are
    # mono and stay comparable with the archived diagnostics.
    channels = int(getattr(box["tee"], "channels", 1))
    if channels > 1:
        written = written[: (written.size // channels) * channels].reshape(-1, channels)[:, 0]
    device_out = np.concatenate(captured).astype(np.float32)
    np.save(out_dir / "written.npy", written)
    np.save(out_dir / "device_out.npy", device_out)
    stats = player.stats

    written_stuck, written_longest = stuck_repeat_report(written)
    device_stuck, device_longest = stuck_repeat_report(device_out)
    device_repeats = repeated_fragments(device_out)
    written_repeats = repeated_fragments(written)

    print(f"written  {written.size / OUT_RATE:.2f}s -> runtime/playback_diag/written.npy")
    print(f"device   {device_out.size / OUT_RATE:.2f}s -> runtime/playback_diag/device_out.npy")
    print(f"player: underruns={stats.underruns} dropped_blocks={stats.dropped_blocks}")
    print(
        f"our data   : repeated fragments {written_repeats}/40, "
        f"stuck frames {written_stuck} (longest {written_longest} ms)"
    )
    print(
        f"device out : repeated fragments {device_repeats}/40, "
        f"stuck frames {device_stuck} (longest {device_longest} ms)"
    )

    ours_bad = written_repeats > 2 or written_longest >= 100
    device_bad = device_repeats > 2 or device_longest >= 100 or stats.underruns > 0
    if ours_bad:
        print("VERDICT: artifact present in OUR data — bring runtime/playback_diag/ back to the code session")
        return 1
    if device_bad:
        print(
            "VERDICT: our data is clean; the DEVICE is replaying/glitching. "
            "Check: Bluetooth profile switch (A2DP<->HFP), audio enhancements, "
            "another app changing the endpoint format, USB power management."
        )
        return 2
    print("VERDICT: clean — no artifact in our data or the device output at this moment")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
