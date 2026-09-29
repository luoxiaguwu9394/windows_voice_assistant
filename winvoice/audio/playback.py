"""
Continuous playback: one stream, a jitter buffer, and pauses as real silence.

Three defects in the old path, all audible:

* `blocksize=1600` was requested at 8 kHz (200 ms) while 100 ms chunks were
  written into it, on the **MME** host API — the device's native rate here is
  44.1 kHz, so a telephone-grade stream was handed to the OS to resample, with an
  underrun available at every write;
* nothing buffered, so a gap in synthesis was a gap in the audio;
* a barge-in stopped playback at the next chunk *boundary* and left whatever was
  already in the device buffer to play out.

What replaces it: the output stream is opened **once**, at the device's own
sample rate (WASAPI first, MME only as a fallback), and fed by a dedicated thread
from a bounded queue. The player owns the pauses — a segment arrives with
`pause_after_ms` and the silence is written as real frames, which is the only way
the pause the listener hears is the pause the table asked for.

The thread is deliberate: writing to a sound device blocks, and the event loop is
busy noticing wake words. Nothing in the pipeline's hot path touches PortAudio.
"""

from __future__ import annotations

import asyncio
import math
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

import numpy as np

from winvoice.config import get_config
from winvoice.logging import get_logger

try:  # scipy is a declared dependency; the fallback keeps a stale env speaking
    from scipy.signal import resample_poly as _resample_poly
except Exception:  # pragma: no cover - exercised only on a broken install
    _resample_poly = None

logger = get_logger(__name__)

INT16_SCALE = 32768.0
DEFAULT_BLOCKSIZE_MS = 20
DEFAULT_PREBUFFER_MS = 250
DEFAULT_LATENCY_S = 0.1
# How long a starved feeder pads with silence before it gives up waiting: long
# enough to cover a slow synthesis of the next segment, short enough that a
# genuinely dead stream is noticed.
SILENCE_PAD_MS = 60
QUEUE_BLOCKS = 512  # ~5 s at 20 ms blocks
DRAIN_POLL_S = 0.01


class AudioOutputUnavailable(RuntimeError):
    """No output device could be opened at any rate or host API."""


@dataclass
class PlaybackStats:
    """What actually happened to the audio, for the utterance log."""

    frames_written: int = 0
    audio_ms: int = 0
    pause_ms: int = 0
    underruns: int = 0
    dropped_blocks: int = 0
    interrupted: int = 0
    first_audio_ms: float = 0.0
    peak: int = 0

    def as_dict(self) -> dict:
        return {
            "frames": self.frames_written,
            "audio_ms": self.audio_ms,
            "pause_ms": self.pause_ms,
            "underruns": self.underruns,
            "dropped_blocks": self.dropped_blocks,
            "interrupted": self.interrupted,
            "first_audio_ms": int(self.first_audio_ms),
            "peak": self.peak,
        }


@dataclass
class _Item:
    generation: int
    samples: np.ndarray
    is_pause: bool = False


@dataclass
class _Utterance:
    """Per-utterance bookkeeping, reset when the producer starts again."""

    started_at: float = field(default_factory=time.monotonic)
    producer_done: bool = False


def _default_output_device(device: Any) -> Any:
    return None if device in ("", "default", None, "None") else device


def _device_default_rate(device: Any) -> int:
    """
    The sample rate a device+host-API pair is actually running at.

    Note that the answer depends on the **host API**: on this machine the same
    physical speakers report 48 000 Hz through WASAPI and 44 100 Hz through MME,
    and asking WASAPI for 44 100 fails outright ("Invalid sample rate"). So the
    rate has to be resolved per route, which is why the opener below asks this
    question for each route it tries rather than once up front.

    `sd.query_devices(None)` does **not** mean "the default device" either — it
    means "no device was named" and returns the whole `DeviceList` (a tuple), so
    the default output device has to be asked for by `kind`.
    """
    import sounddevice as sd

    chosen = _default_output_device(device)
    info = sd.query_devices(chosen) if chosen is not None else sd.query_devices(kind="output")
    return int(info["default_samplerate"])


def _wasapi_output_device(sd: Any) -> tuple[Optional[int], Optional[int]]:
    """The WASAPI host API index and its default output device, if either exists."""
    try:
        for index, api in enumerate(sd.query_hostapis()):
            if "wasapi" in str(api.get("name", "")).lower():
                device = int(api.get("default_output_device", -1))
                return index, (device if device >= 0 else None)
    except Exception:  # pragma: no cover - host API enumeration failure
        pass
    return None, None


def open_output_stream(
    desired_rate: int,
    blocksize_ms: int,
    *,
    device: Any = None,
    host_api: str = "auto",
    latency: float = DEFAULT_LATENCY_S,
) -> tuple[Any, int]:
    """
    Open the speaker, preferring WASAPI and the rate the device really runs at.

    MME (sounddevice's default host API on Windows) does not resample and has the
    worst latency of the three; WASAPI in shared mode does. Two things this
    function learned from a real device rather than from the documentation:

    * the host API is chosen through the **device index** — `sd.OutputStream`
      takes no `hostapi` argument;
    * the rate is **per route**, because the same speakers report 48 kHz on
      WASAPI and 44.1 kHz on MME, and the "wrong" one is refused.

    Order: an explicitly configured `audio.output_device` first (a named device
    is a decision, not a preference), then WASAPI at its own rate, then WASAPI
    with `auto_convert` (Windows resamples — worse than ours, but it always
    opens), then the system default. The rate that was actually opened is
    returned, because the resampler has to match it.
    """
    import sounddevice as sd

    chosen = _default_output_device(device)
    _, wasapi_device = _wasapi_output_device(sd)

    routes: list[tuple[Any, Any, str]] = []
    if chosen is not None:
        routes.append((chosen, None, f"configured device {chosen}"))
        routes.append((None, None, "system default"))
    else:
        if host_api in ("auto", "wasapi") and wasapi_device is not None:
            routes.append((wasapi_device, sd.WasapiSettings(auto_convert=False), "wasapi shared"))
            routes.append(
                (wasapi_device, sd.WasapiSettings(auto_convert=True), "wasapi auto-convert")
            )
        if host_api != "wasapi":
            routes.append((None, None, "system default"))

    errors: list[str] = []
    for route_device, extra, label in routes:
        try:
            rate = desired_rate or _device_default_rate(route_device)
            blocksize = max(64, int(rate * blocksize_ms / 1000))
            stream = sd.OutputStream(
                samplerate=rate,
                channels=1,
                dtype="int16",
                blocksize=blocksize,
                latency=latency,
                device=route_device,
                extra_settings=extra,
            )
            stream.start()
            actual = int(getattr(stream, "samplerate", rate) or rate)
            logger.info(
                "audio_output_route", route=label, sample_rate=actual, blocksize=blocksize
            )
            return stream, actual
        except Exception as error:  # try the next route
            errors.append(f"{label}: {error}")

    raise AudioOutputUnavailable("; ".join(errors) or "no output device")


class SpeechPlayer:
    """
    Plays PCM chunks back to back, with the pauses the segments asked for.

    Usage from the pipeline: `await start()`, then `await enqueue(...)` per
    chunk, then `await wait_drained()`. `interrupt()` is safe to call from the
    event loop at any point (a wake word does exactly that).
    """

    def __init__(
        self,
        *,
        sample_rate: int = 0,
        device: Any = None,
        host_api: str = "auto",
        blocksize_ms: int = DEFAULT_BLOCKSIZE_MS,
        latency_s: float = DEFAULT_LATENCY_S,
        prebuffer_ms: int = DEFAULT_PREBUFFER_MS,
        silence_pad_ms: int = SILENCE_PAD_MS,
        queue_blocks: int = QUEUE_BLOCKS,
        stream_open: Optional[Callable[[int, int], tuple[Any, int]]] = None,
    ) -> None:
        self.requested_rate = int(sample_rate or 0)
        self.device = device
        self.host_api = host_api
        self.blocksize_ms = max(1, int(blocksize_ms))
        self.latency_s = float(latency_s)
        self.prebuffer_ms = max(0, int(prebuffer_ms))
        self.silence_pad_ms = max(0, int(silence_pad_ms))
        self.queue_blocks = max(2, int(queue_blocks))

        self.stats = PlaybackStats()
        self.out_rate = self.requested_rate

        self._stream_open = stream_open
        self._stream: Any = None
        self._thread: Optional[threading.Thread] = None
        self._disabled = False
        self._closing = False

        self._condition = threading.Condition()
        self._queue: list[_Item] = []
        self._pending_ms = 0.0
        self._playing = False
        self._generation = 0
        self._utterance = _Utterance(producer_done=True)
        self._drained = threading.Event()
        self._drained.set()
        # A segment's 100 ms chunks, held until the segment's pause arrives so
        # the rate conversion runs ONCE per segment — see `_to_device_rate`.
        self._segment_buf: list[np.ndarray] = []
        self._segment_rate = 0
        # Set via `attach_diag` (or WINVOICE_DIAG_PLAYBACK): every sample handed
        # to the device is appended here, so a playback artifact can be checked
        # against what the application actually wrote.
        self._diag_path: Optional[Path] = None
        self._diag_file = None

    # ── lifecycle ──────────────────────────────────────────────

    async def start(self) -> None:
        """Open the output device once; failures degrade to silence, not to a crash."""
        if self._stream is not None or self._disabled or self._closing:
            return

        loop = asyncio.get_running_loop()
        opener = self._stream_open
        try:
            if opener is None:
                stream, actual = await loop.run_in_executor(
                    None,
                    lambda: open_output_stream(
                        self.requested_rate,
                        self.blocksize_ms,
                        device=self.device,
                        host_api=self.host_api,
                        latency=self.latency_s,
                    ),
                )
            else:
                rate = self.requested_rate
                if not rate:
                    rate = await loop.run_in_executor(None, _device_default_rate, None)
                stream, actual = await loop.run_in_executor(
                    None, opener, rate, self.blocksize_ms
                )
        except Exception as error:
            self._disabled = True
            logger.warning(
                "audio_output_unavailable",
                error=f"{type(error).__name__}: {error}",
                device=str(self.device),
                requested_rate=self.requested_rate,
            )
            return

        self._stream = stream
        self.out_rate = int(actual or self.requested_rate or 0)
        if not self.out_rate:
            self._disabled = True
            logger.warning("audio_output_unavailable", error="no usable sample rate")
            return
        self._thread = threading.Thread(target=self._run, name="speech-player", daemon=True)
        self._thread.start()
        logger.info(
            "audio_output_started",
            sample_rate=self.out_rate,
            blocksize_ms=self.blocksize_ms,
            host_api=self.host_api,
            prebuffer_ms=self.prebuffer_ms,
        )

    async def close(self) -> None:
        """
        Release the feeder thread and the device.

        Closing is final for this player: a restart means a new `SpeechPlayer`,
        which is what keeps the "one stream for the session" invariant easy to
        reason about (and why `start()` refuses once closed).
        """
        with self._condition:
            self._generation += 1
            self._queue.clear()
            self._pending_ms = 0.0
            self._closing = True
            self._segment_buf.clear()
            self._segment_rate = 0
            self._condition.notify_all()

        thread, self._thread = self._thread, None
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.0)

        stream, self._stream = self._stream, None
        if self._diag_file is not None:
            try:
                self._diag_file.close()
            except Exception:  # pragma: no cover - diagnostics only
                pass
            self._diag_file = None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception as error:  # pragma: no cover - device teardown
                logger.warning("audio_output_close_failed", error=str(error))

    # ── producer side (event loop) ─────────────────────────────

    async def enqueue(self, pcm: bytes, sample_rate: int, pause_after_ms: int = 0) -> None:
        """Queue one chunk, followed by `pause_after_ms` of real silence."""
        if self._disabled:
            return
        if self._stream is None:
            await self.start()
        if self._stream is None:
            return

        with self._condition:
            if self._utterance.producer_done:
                # First chunk of a new utterance. A half-accumulated segment can
                # only be a leftover from an interrupted turn — not this one.
                self._segment_buf.clear()
                self._segment_rate = 0
                self._utterance = _Utterance()
                self._drained.clear()

        rate = int(sample_rate or 0)
        converted: Optional[np.ndarray] = None
        if rate and rate != self.out_rate and _resample_poly is not None:
            # Hold the chunk until its segment ends (the pause that follows is
            # the segment boundary), then convert the whole segment in one call.
            self._segment_buf.append(np.frombuffer(pcm, dtype=np.int16))
            self._segment_rate = rate
            if pause_after_ms <= 0:
                return
            pcm = b"".join(part.tobytes() for part in self._segment_buf)
            self._segment_buf.clear()
            converted = self._to_device_rate(pcm, rate)
        else:
            converted = self._to_device_rate(pcm, rate)
        pause_samples = (
            int(self.out_rate * pause_after_ms / 1000) if pause_after_ms > 0 else 0
        )

        with self._condition:
            if converted.size:
                self._enqueue_locked(converted, is_pause=False)
            if pause_samples:
                self._enqueue_locked(
                    np.zeros(pause_samples, dtype=np.int16), is_pause=True
                )
            self._condition.notify_all()

    def _enqueue_locked(self, samples: np.ndarray, *, is_pause: bool) -> None:
        if len(self._queue) >= self.queue_blocks:
            # The device is not draining at all. Dropping audio is bad; blocking
            # the event loop is worse, and a deadlock would be worst of all.
            self.stats.dropped_blocks += 1
            logger.warning(
                "audio_buffer_overflow", blocks=len(self._queue), dropped=self.stats.dropped_blocks
            )
            return
        self._queue.append(_Item(self._generation, samples, is_pause))
        duration_ms = samples.size / self.out_rate * 1000 if self.out_rate else 0.0
        self._pending_ms += duration_ms
        if is_pause:
            self.stats.pause_ms += int(duration_ms)
        else:
            self.stats.audio_ms += int(duration_ms)

    async def wait_drained(self, timeout: float = 10.0) -> bool:
        """
        Wait until everything queued has been handed to the device and played.

        The final `blocksize + latency` is included: returning as soon as the
        last write lands would let the half-duplex pipeline re-open the
        microphone on top of its own last phoneme.
        """
        await self._flush_segment_buffer()

        with self._condition:
            self._utterance.producer_done = True
            self._condition.notify_all()

        deadline = time.monotonic() + timeout
        while not self._drained.is_set():
            if time.monotonic() > deadline:
                logger.warning(
                    "audio_drain_timeout", pending_ms=int(self._pending_ms), timeout_s=timeout
                )
                return False
            await asyncio.sleep(DRAIN_POLL_S)
        return True

    def interrupt(self) -> None:
        """
        Stop now: drop everything queued *and* what the device already holds.

        `abort()` is the difference between this and the old behaviour — it
        discards PortAudio's internal buffer, so the 200 ms that had already been
        written is not played out after the wake word.

        Restarting has to be defensive. The abort happens from the event loop
        while the feeder thread may be *inside* `write()`, and on MME that race
        makes the immediate restart fail with "Cannot perform this operation while
        media data is still playing" — which, unhandled, would leave the
        conversation silent for the rest of the session.
        """
        with self._condition:
            self._generation += 1
            self._queue.clear()
            self._pending_ms = 0.0
            self._playing = False
            self._utterance.producer_done = True
            self._segment_buf.clear()
            self._segment_rate = 0
            self.stats.interrupted += 1
            self._condition.notify_all()

        self._restart_stream()
        self._drained.set()

    def _restart_stream(self) -> None:
        """Abort the device buffer, then make sure the stream runs again."""
        stream = self._stream
        if stream is None:
            return
        try:
            stream.abort()
        except Exception as error:  # pragma: no cover - device teardown
            logger.warning("audio_abort_failed", error=str(error))

        for attempt, delay in enumerate((0.0, 0.02, 0.05, 0.1)):
            if delay:
                time.sleep(delay)
            try:
                stream.start()
                return
            except Exception as error:
                if attempt == 3:
                    logger.warning("audio_restart_failed", error=str(error))
        self._reopen()

    def _recover_stream(self) -> bool:
        """
        Rebuild the output stream after the device invalidated it mid-write.

        `AUDCLNT_E_DEVICE_INVALIDATED` fires when Windows tears the endpoint
        down underneath the stream (device change, driver reset, an effects
        pipeline reloading — observed on a second machine's built-in speakers).
        The stream object is dead but the device usually is not: close, reopen,
        and keep speaking. One retry with a short delay covers a reset that is
        still settling; `_disabled` stays with the caller so a recovered player
        is never silenced by a transient.
        """
        for attempt, delay in enumerate((0.0, 0.25)):
            if delay:
                time.sleep(delay)
            old, self._stream = self._stream, None
            if old is not None:
                try:
                    old.close()
                except Exception:  # pragma: no cover - device teardown
                    pass
            try:
                if self._stream_open is not None:
                    stream, rate = self._stream_open(self.out_rate, self.blocksize_ms)
                else:
                    stream, rate = open_output_stream(
                        self.out_rate,
                        self.blocksize_ms,
                        device=self.device,
                        host_api=self.host_api,
                        latency=self.latency_s,
                    )
            except Exception as error:
                logger.warning(
                    "audio_output_recover_failed",
                    attempt=attempt,
                    error=str(error),
                )
                continue
            self._stream = stream
            self.out_rate = int(rate or self.out_rate)
            logger.info("audio_output_recovered", sample_rate=self.out_rate)
            return True
        return False

    def _reopen(self) -> None:
        """Last resort: a stream that will not restart is replaced."""
        old, self._stream = self._stream, None
        if old is not None:
            try:
                old.close()
            except Exception:  # pragma: no cover - device teardown
                pass
        try:
            if self._stream_open is not None:
                stream, rate = self._stream_open(self.out_rate, self.blocksize_ms)
            else:
                stream, rate = open_output_stream(
                    self.out_rate,
                    self.blocksize_ms,
                    device=self.device,
                    host_api=self.host_api,
                    latency=self.latency_s,
                )
        except Exception as error:
            self._disabled = True
            logger.warning("audio_output_unavailable", error=str(error), reason="reopen")
            return
        self._stream = stream
        self.out_rate = int(rate or self.out_rate)
        logger.info("audio_output_reopened", sample_rate=self.out_rate)

    def attach_diag(self, path: Path) -> None:
        """Record every sample written to the device into `path` (raw int16)."""
        self._diag_path = Path(path)
        self._diag_path.parent.mkdir(parents=True, exist_ok=True)
        self._diag_file = open(self._diag_path, "ab")
        logger.info("audio_diag_attached", path=str(self._diag_path))

    @property
    def disabled(self) -> bool:
        return self._disabled

    @property
    def pending_ms(self) -> float:
        with self._condition:
            return self._pending_ms

    # ── consumer side (player thread) ──────────────────────────

    def _run(self) -> None:
        """
        The feeder: one item at a time, decided under the lock, acted on outside.

        Every branch is a single decision so the states cannot interleave — the
        bug this replaces (a prebuffer that was not honoured, or an idle loop
        that spun) is invisible until the device is under load.
        """
        while True:
            action: Optional[tuple[str, Optional[_Item]]] = None

            with self._condition:
                if self._closing:
                    return

                ready = self._playing or self._utterance.producer_done
                if self._queue and (ready or self._pending_ms >= self.prebuffer_ms):
                    item = self._queue.pop(0)
                    self._pending_ms = max(0.0, self._pending_ms - self._duration_ms(item))
                    action = ("write", item)
                elif self._utterance.producer_done:
                    if self._drained.is_set():
                        self._condition.wait(timeout=0.05)
                        continue
                    action = ("finish", None)
                elif self._playing:
                    # Mid-utterance and nothing queued: the next segment is still
                    # being synthesised, so keep the device fed with silence
                    # rather than letting the buffer run dry under a click.
                    action = ("pad", None)
                else:
                    self._condition.wait(timeout=0.05)
                    continue

            kind, item = action
            if kind == "write":
                if item is not None and item.generation == self._generation:
                    self._write(item.samples)
            elif kind == "finish":
                self._finish_utterance()
            else:
                self._pad()

    def _duration_ms(self, item: _Item) -> float:
        return item.samples.size / self.out_rate * 1000 if self.out_rate else 0.0

    def _write(self, samples: np.ndarray) -> None:
        stream = self._stream
        if stream is None or samples.size == 0:
            return
        with self._condition:
            first = not self._playing
            if first:
                self._playing = True
                self.stats.first_audio_ms = (
                    time.monotonic() - self._utterance.started_at
                ) * 1000.0
                self._drained.clear()
        try:
            stream.write(samples)
        except Exception as error:
            # An endpoint can be invalidated underneath us mid-playback
            # (AUDCLNT_E_DEVICE_INVALIDATED: device change, driver reset, an
            # effects pipeline reloading). Dying here would silence the
            # assistant until the next app start, so the stream is rebuilt and
            # the session continues — `_disabled` only after recovery fails
            # repeatedly.
            logger.warning(
                "audio_write_failed",
                error=str(error),
                recovery="reopen",
            )
            if not self._recover_stream():
                self._disabled = True
            return
        if self._diag_file is not None:
            try:
                samples.tofile(self._diag_file)
            except Exception as error:  # pragma: no cover - diagnostics only
                logger.debug("audio_diag_write_failed", error=str(error))
        peak = int(np.abs(samples).max()) if samples.size else 0
        with self._condition:
            self.stats.frames_written += samples.size
            self.stats.peak = max(self.stats.peak, peak)

    def _pad(self) -> None:
        """
        Keep the device fed while the next segment is still being synthesised.

        The pad is written and then *waited out* before another one can be
        written. Without the wait the feeder spins as fast as the stream accepts
        frames: invisible on a real device, where `write()` blocks at the device's
        rate, and a runaway on a stream that returns immediately (a test double,
        or a driver with a huge buffer) — where the timeline would run away from
        the audio instead of following it.
        """
        padding = int(self.out_rate * self.silence_pad_ms / 1000)
        with self._condition:
            self.stats.underruns += 1
        if padding:
            self._write(np.zeros(padding, dtype=np.int16))
        with self._condition:
            # Woken by the next `enqueue`, so a real segment is never delayed by
            # this wait.
            self._condition.wait(timeout=self.silence_pad_ms / 1000)

    def _finish_utterance(self) -> None:
        """Everything is written; let the device buffer play out, then say so."""
        with self._condition:
            if self._drained.is_set():
                return
            self._playing = False
        time.sleep(min(0.5, self.blocksize_ms / 1000 + self.latency_s))
        with self._condition:
            self._drained.set()
            self._condition.notify_all()
        logger.info("audio_playback_done", **self.stats.as_dict())

    # ── rate conversion ────────────────────────────────────────

    def _to_device_rate(self, pcm: bytes, sample_rate: int) -> np.ndarray:
        """
        Bring audio to the rate the device is actually running at.

        Opening the stream at the model's rate instead would be simpler and
        worse: the OS would resample anyway, and on Windows that is the MME
        path this class exists to avoid.

        This runs **once per segment**, not per 100 ms chunk: resampling a block
        independently makes the polyphase filter see the signal stop at the
        block's edge, and the block's first samples after a seam carry a ringing
        transient (measured: steps of 20–50 % of peak at every 100 ms boundary —
        inside a sustained vowel that is a tick ten times a second, heard as the
        sentence shattering into pieces). The segments are converted whole, and
        the only seams left in the output are segment boundaries, where a
        designed pause follows and masks the residual.
        """
        samples = np.frombuffer(pcm, dtype=np.int16)
        rate = int(sample_rate or self.out_rate)
        if rate == self.out_rate or samples.size == 0 or rate <= 0:
            return samples

        if _resample_poly is not None:
            divisor = math.gcd(self.out_rate, rate)
            up, down = self.out_rate // divisor, rate // divisor
            converted = _resample_poly(samples.astype(np.float32) / INT16_SCALE, up, down)
            return (np.clip(converted, -1.0, 1.0) * (INT16_SCALE - 1.0)).astype(np.int16)

        target = max(1, int(round(samples.size * self.out_rate / rate)))
        grid = np.linspace(0, samples.size - 1, target)
        return np.interp(grid, np.arange(samples.size), samples).astype(np.int16)

    async def _flush_segment_buffer(self) -> None:
        """
        Convert and queue whatever half-accumulated segment is still held.

        Normally nothing: every segment ends with a pause, which converts and
        releases the buffer. A turn that ends without one (or a lone-chunk
        caller) still deserves its last samples, so `wait_drained` flushes
        before declaring the utterance drained.
        """
        if self._disabled or not self._segment_buf:
            return
        parts, self._segment_buf = self._segment_buf, []
        rate, self._segment_rate = self._segment_rate, 0
        pcm = b"".join(part.tobytes() for part in parts)
        samples = self._to_device_rate(pcm, rate)
        if samples.size:
            with self._condition:
                self._enqueue_locked(samples, is_pause=False)
                self._condition.notify_all()


class NullSpeechPlayer:
    """
    A player that does not need a device, and still counts what it was given.

    Used by `--stub-audio` runs and by every test that constructs a pipeline
    directly: the pause plumbing is exercised (the counters are what the tests
    assert on) without opening audio hardware.
    """

    def __init__(self, **_: Any) -> None:
        self.stats = PlaybackStats()
        self.out_rate = 0
        self.disabled = True

    async def start(self) -> None:
        return None

    async def enqueue(self, pcm: bytes, sample_rate: int, pause_after_ms: int = 0) -> None:
        rate = int(sample_rate or 0)
        frames = len(pcm) // 2
        self.stats.frames_written += frames
        self.stats.audio_ms += int(frames / rate * 1000) if rate else 0
        self.stats.pause_ms += int(pause_after_ms)
        if pcm:
            self.stats.peak = max(self.stats.peak, int(np.abs(np.frombuffer(pcm, dtype=np.int16)).max()))

    async def wait_drained(self, timeout: float = 10.0) -> bool:
        return True

    def interrupt(self) -> None:
        self.stats.interrupted += 1

    async def close(self) -> None:
        return None

    @property
    def pending_ms(self) -> float:
        return 0.0


def create_speech_player(use_stub: bool = False) -> SpeechPlayer | NullSpeechPlayer:
    """Build the configured player, or a silent one for stub runs."""
    if use_stub or os.getenv("WINVOICE_STUB_AUDIO") == "1":
        return NullSpeechPlayer()

    cfg = get_config()
    player = SpeechPlayer(
        sample_rate=int(cfg.get("audio.output_sample_rate", 0) or 0),
        device=cfg.get("audio.output_device", None),
        host_api=cfg.get("audio.output_host_api", "auto"),
        blocksize_ms=int(cfg.get("audio.output_blocksize_ms", DEFAULT_BLOCKSIZE_MS) or DEFAULT_BLOCKSIZE_MS),
        prebuffer_ms=int(
            cfg.get("audio.output_prebuffer_ms", DEFAULT_PREBUFFER_MS) or DEFAULT_PREBUFFER_MS
        ),
    )
    diag = os.getenv("WINVOICE_DIAG_PLAYBACK", "")
    if diag:
        player.attach_diag(Path(diag))
    return player


__all__ = [
    "AudioOutputUnavailable",
    "NullSpeechPlayer",
    "PlaybackStats",
    "SpeechPlayer",
    "create_speech_player",
    "open_output_stream",
]
