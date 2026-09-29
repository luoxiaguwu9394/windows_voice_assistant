"""
The player: one stream, a jitter buffer, and pauses written as real silence.

The three defects this replaces, each pinned below:

* the old path opened an 8 kHz **MME** stream on a 44.1 kHz device and wrote
  100 ms chunks into a 200 ms block size, so every write could underrun. Here the
  stream is opened once at the device's own rate and the audio is converted to
  it;
* nothing buffered, so a slow segment was a hole in the audio. Here a starved
  feeder writes silence and counts an underrun instead of leaving a gap;
* a barge-in stopped at a chunk boundary and left the device buffer to play out.
  Here `interrupt()` drops the queue *and* aborts the device buffer.

The fake stream stands in for PortAudio: it records what was written, when, and
whether it was aborted. No device, no timing dependence on the host machine.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import numpy as np

from winvoice.audio.playback import (
    NullSpeechPlayer,
    SpeechPlayer,
    open_output_stream,
)

DEVICE_RATE = 44100


class FakeStream:
    """Records writes instead of playing them."""

    def __init__(self, rate: int, *, write_delay_s: float = 0.0) -> None:
        self.samplerate = rate
        self.write_delay_s = write_delay_s
        self.writes: list[np.ndarray] = []
        self.aborts = 0
        self.starts = 1
        self.closed = False

    def write(self, data: np.ndarray) -> None:
        if self.write_delay_s:
            time.sleep(self.write_delay_s)
        self.writes.append(np.array(data, copy=True))

    def abort(self) -> None:
        self.aborts += 1

    def start(self) -> None:
        self.starts += 1

    def stop(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True

    # ── assertions helpers ──
    @property
    def written_frames(self) -> int:
        return sum(int(chunk.size) for chunk in self.writes)

    @property
    def written_ms(self) -> float:
        return self.written_frames / self.samplerate * 1000

    def non_silent_ms(self) -> float:
        frames = sum(int(np.count_nonzero(chunk)) for chunk in self.writes)
        return frames / self.samplerate * 1000


def pcm(ms: int, rate: int, amplitude: int = 8000) -> bytes:
    """A chunk that is definitely not silence, so it can be told apart from a pad."""
    return np.full(int(rate * ms / 1000), amplitude, dtype=np.int16).tobytes()


def make_player(stream: FakeStream, **kwargs) -> SpeechPlayer:
    defaults = dict(
        sample_rate=DEVICE_RATE,
        blocksize_ms=20,
        prebuffer_ms=0,
        stream_open=lambda rate, blocksize: (stream, rate),
    )
    defaults.update(kwargs)
    return SpeechPlayer(**defaults)


# ──────────────────────────────────────────────────────────────
# Opening the stream
# ──────────────────────────────────────────────────────────────


async def test_the_stream_is_opened_once_for_two_utterances() -> None:
    stream = FakeStream(DEVICE_RATE)
    opened: list[int] = []

    def opener(rate: int, blocksize: int):
        opened.append(blocksize)
        return stream, rate

    player = SpeechPlayer(sample_rate=DEVICE_RATE, stream_open=opener)
    try:
        for _ in range(2):
            await player.enqueue(pcm(100, DEVICE_RATE), DEVICE_RATE)
            await player.wait_drained()
    finally:
        await player.close()

    assert len(opened) == 1, "the device was re-opened per utterance"
    assert stream.closed


async def test_a_device_that_cannot_open_degrades_to_silence() -> None:
    def opener(rate: int, blocksize: int):
        raise RuntimeError("PortAudioError: no device")

    player = SpeechPlayer(sample_rate=DEVICE_RATE, stream_open=opener)
    try:
        await player.start()
        await player.enqueue(pcm(100, DEVICE_RATE), DEVICE_RATE)
        await player.wait_drained()
    finally:
        await player.close()

    assert player.disabled is True
    assert player.stats.frames_written == 0  # nothing crashed, nothing played


def test_the_default_output_rate_is_asked_for_by_kind(monkeypatch) -> None:
    """
    Regression found on the real device: `sd.query_devices(None)` returns the
    whole `DeviceList` (a tuple), not the default device's info dict, so
    `info["default_samplerate"]` raised `TypeError: tuple indices must be
    integers`. The default output device has to be named with `kind="output"`.
    """
    import sounddevice as sd

    from winvoice.audio.playback import _device_default_rate

    calls: list[dict] = []

    def fake_query_devices(device=None, kind=None):
        calls.append({"device": device, "kind": kind})
        if device is None and kind is None:
            # What sounddevice really returns when nothing at all is asked for:
            # every device, as a `DeviceList` (a tuple).
            return ({"name": "Speaker A", "default_samplerate": 44100.0},)
        return {"name": "Speaker A", "default_samplerate": 44100.0}

    monkeypatch.setattr(sd, "query_devices", fake_query_devices)

    rate = _device_default_rate("default")

    assert rate == 44100
    assert calls == [{"device": None, "kind": "output"}], calls


def test_a_named_device_is_queried_directly(monkeypatch) -> None:
    import sounddevice as sd

    from winvoice.audio.playback import _device_default_rate

    seen: list[Any] = []

    def fake_query_devices(device=None, kind=None):
        seen.append(device)
        return {"name": "Headphones", "default_samplerate": 48000.0}

    monkeypatch.setattr(sd, "query_devices", fake_query_devices)

    assert _device_default_rate(3) == 48000
    assert seen == [3]


def test_an_explicitly_configured_device_is_used_as_given(monkeypatch) -> None:
    """A named device is a decision, not a preference: no host-API substitution."""
    import sounddevice as sd

    from winvoice.audio.playback import open_output_stream as opener

    attempts: list[tuple] = []

    class _Stream:
        samplerate = DEVICE_RATE

        def start(self) -> None:
            pass

    def fake_output_stream(**kwargs):
        attempts.append((kwargs.get("device"), kwargs.get("extra_settings")))
        return _Stream()

    monkeypatch.setattr(sd, "OutputStream", fake_output_stream)
    monkeypatch.setattr(
        sd, "query_hostapis", lambda: [{"name": "Windows WASAPI", "default_output_device": 8}]
    )

    opener(DEVICE_RATE, 882, device="Speakers (USB)")

    assert attempts == [("Speakers (USB)", None)]


def test_a_named_device_falls_back_to_the_system_default(monkeypatch) -> None:
    import sounddevice as sd

    from winvoice.audio.playback import open_output_stream as opener

    attempts: list[Any] = []

    class _Stream:
        samplerate = DEVICE_RATE

        def start(self) -> None:
            pass

    def fake_output_stream(**kwargs):
        attempts.append(kwargs.get("device"))
        if kwargs.get("device") is not None:
            raise RuntimeError("device gone")
        return _Stream()

    monkeypatch.setattr(sd, "OutputStream", fake_output_stream)
    monkeypatch.setattr(
        sd, "query_hostapis", lambda: [{"name": "Windows WASAPI", "default_output_device": 8}]
    )

    _, rate = opener(DEVICE_RATE, 882, device=7)

    assert attempts == [7, None]
    assert rate == DEVICE_RATE


def test_each_route_asks_its_own_device_for_the_rate(monkeypatch) -> None:
    """
    Regression from the real machine: the same speakers report 48 000 Hz through
    WASAPI and 44 100 Hz through MME, and WASAPI *refuses* 44 100. Resolving the
    rate once, up front, therefore makes the preferred route fail and silently
    drops to MME.
    """
    import sounddevice as sd

    from winvoice.audio.playback import open_output_stream as opener

    attempts: list[int] = []
    wasapi_attempts = 0

    class _Stream:
        def __init__(self, rate: int) -> None:
            self.samplerate = rate

        def start(self) -> None:
            pass

    def fake_output_stream(**kwargs):
        nonlocal wasapi_attempts
        attempts.append(kwargs["samplerate"])
        if isinstance(kwargs.get("extra_settings"), sd.WasapiSettings):
            wasapi_attempts += 1
            if wasapi_attempts == 1:
                # WASAPI shared refuses anything but the device's own rate; the
                # second WASAPI route (auto_convert) accepts it.
                raise RuntimeError("Invalid sample rate")
        return _Stream(kwargs["samplerate"])

    def fake_query_devices(device=None, kind=None):
        if device == 8:  # the WASAPI default output device
            return {"name": "Speakers (WASAPI)", "default_samplerate": 48000.0}
        return {"name": "Speakers (MME)", "default_samplerate": 44100.0}

    monkeypatch.setattr(sd, "OutputStream", fake_output_stream)
    monkeypatch.setattr(sd, "query_devices", fake_query_devices)
    monkeypatch.setattr(
        sd, "query_hostapis", lambda: [{"name": "MME", "default_output_device": 3},
                                       {"name": "Windows WASAPI", "default_output_device": 8}]
    )

    stream, rate = opener(0, 20)

    assert attempts == [48000, 48000], attempts
    assert rate == 48000


def test_the_system_default_route_uses_its_own_rate(monkeypatch) -> None:
    import sounddevice as sd

    from winvoice.audio.playback import open_output_stream as opener

    attempts: list[int] = []

    class _Stream:
        def __init__(self, rate: int) -> None:
            self.samplerate = rate

        def start(self) -> None:
            pass

    def fake_output_stream(**kwargs):
        attempts.append(kwargs["samplerate"])
        if isinstance(kwargs.get("extra_settings"), sd.WasapiSettings):
            raise RuntimeError("WASAPI unavailable")
        return _Stream(kwargs["samplerate"])

    def fake_query_devices(device=None, kind=None):
        if device == 8:
            return {"name": "Speakers (WASAPI)", "default_samplerate": 48000.0}
        return {"name": "Speakers (MME)", "default_samplerate": 44100.0}

    monkeypatch.setattr(sd, "OutputStream", fake_output_stream)
    monkeypatch.setattr(sd, "query_devices", fake_query_devices)
    monkeypatch.setattr(
        sd, "query_hostapis", lambda: [{"name": "Windows WASAPI", "default_output_device": 8}]
    )

    _, rate = opener(0, 20)

    assert attempts == [48000, 48000, 44100], attempts
    assert rate == 44100


def test_wasapi_is_tried_before_mme(monkeypatch) -> None:
    """
    The host API matters: this device's native rate is 44.1 kHz and MME is the
    path the old implementation used for an 8 kHz stream.

    It is selected through the *device index* — `sd.OutputStream` has no
    `hostapi` argument, which the first version of the opener passed and which
    only a real device caught.
    """
    import sounddevice as sd

    attempts: list[tuple] = []

    class _Stream:
        samplerate = DEVICE_RATE

        def start(self) -> None:
            pass

    def fake_output_stream(**kwargs):
        assert "hostapi" not in kwargs, "sd.OutputStream does not accept hostapi"
        attempts.append((kwargs.get("device"), kwargs.get("extra_settings")))
        if isinstance(kwargs.get("extra_settings"), sd.WasapiSettings):
            raise RuntimeError("WASAPI unavailable")
        return _Stream()

    monkeypatch.setattr(sd, "OutputStream", fake_output_stream)
    monkeypatch.setattr(
        sd,
        "query_hostapis",
        lambda: [
            {"name": "MME", "default_output_device": 3},
            {"name": "Windows DirectSound", "default_output_device": 6},
            {"name": "Windows WASAPI", "default_output_device": 8},
        ],
    )

    stream, rate = open_output_stream(DEVICE_RATE, 882)

    assert rate == DEVICE_RATE
    assert attempts[0][0] == 8, "WASAPI's default output device should be first"
    assert isinstance(attempts[0][1], sd.WasapiSettings)
    assert attempts[-1][0] is None, "the system default device is the last resort"


# ──────────────────────────────────────────────────────────────
# Rate conversion
# ──────────────────────────────────────────────────────────────


async def test_an_8khz_chunk_is_resampled_to_the_device_rate() -> None:
    stream = FakeStream(DEVICE_RATE)
    player = make_player(stream)
    try:
        await player.enqueue(pcm(500, 8000), 8000)
        await player.wait_drained()
    finally:
        await player.close()

    # 500 ms in, 500 ms out — the duration is preserved, the rate is the device's.
    assert 490 <= stream.written_ms <= 510
    assert stream.writes[0].dtype == np.int16


async def test_a_22khz_chunk_keeps_its_duration_at_44khz() -> None:
    stream = FakeStream(DEVICE_RATE)
    player = make_player(stream)
    try:
        await player.enqueue(pcm(300, 22050), 22050)
        await player.wait_drained()
    finally:
        await player.close()

    assert 290 <= stream.written_ms <= 310


async def test_a_matching_rate_is_passed_through_untouched() -> None:
    stream = FakeStream(DEVICE_RATE)
    player = make_player(stream)
    original = pcm(100, DEVICE_RATE)
    try:
        await player.enqueue(original, DEVICE_RATE)
        await player.wait_drained()
    finally:
        await player.close()

    assert np.array_equal(stream.writes[0], np.frombuffer(original, dtype=np.int16))


# ──────────────────────────────────────────────────────────────
# The pause the table asked for
# ──────────────────────────────────────────────────────────────


async def test_the_pause_is_written_as_real_silence_after_the_chunk() -> None:
    stream = FakeStream(DEVICE_RATE)
    player = make_player(stream)
    try:
        await player.enqueue(pcm(100, DEVICE_RATE), DEVICE_RATE, pause_after_ms=140)
        await player.wait_drained()
    finally:
        await player.close()

    assert 235 <= stream.written_ms <= 245, "100 ms of speech plus a 140 ms pause"
    assert 95 <= stream.non_silent_ms() <= 105, "the pause itself is silence"
    assert player.stats.pause_ms == 140


async def test_no_pause_means_no_padding() -> None:
    stream = FakeStream(DEVICE_RATE)
    player = make_player(stream)
    try:
        await player.enqueue(pcm(100, DEVICE_RATE), DEVICE_RATE)
        await player.wait_drained()
    finally:
        await player.close()

    assert 95 <= stream.written_ms <= 105
    assert stream.non_silent_ms() == stream.written_ms


# ──────────────────────────────────────────────────────────────
# Buffering, gaps, and overload
# ──────────────────────────────────────────────────────────────


async def test_the_first_write_waits_for_the_prebuffer() -> None:
    stream = FakeStream(DEVICE_RATE)
    player = make_player(stream, prebuffer_ms=200)
    try:
        await player.enqueue(pcm(100, DEVICE_RATE), DEVICE_RATE)
        await asyncio.sleep(0.05)
        assert stream.writes == [], "audio started before the buffer was ready"

        await player.enqueue(pcm(150, DEVICE_RATE), DEVICE_RATE)
        await player.wait_drained()
    finally:
        await player.close()

    assert stream.writes, "audio never started once the prebuffer was filled"


async def test_the_producer_finishing_starts_playback_before_the_prebuffer() -> None:
    """A short reply must not wait for a prebuffer it can never fill."""
    stream = FakeStream(DEVICE_RATE)
    player = make_player(stream, prebuffer_ms=3000)
    try:
        await player.enqueue(pcm(80, DEVICE_RATE), DEVICE_RATE)
        await player.wait_drained(timeout=2.0)
    finally:
        await player.close()

    assert 70 <= stream.written_ms <= 90


async def test_a_starved_feeder_pads_with_silence_and_counts_it() -> None:
    """
    A hole in the audio is worse than a little silence: the device buffer would
    run dry mid-word and click.
    """
    stream = FakeStream(DEVICE_RATE)
    player = make_player(stream, silence_pad_ms=50)
    try:
        await player.enqueue(pcm(100, DEVICE_RATE), DEVICE_RATE)

        async def producer() -> None:
            await asyncio.sleep(0.2)  # the "synthesis" of the next segment
            await player.enqueue(pcm(100, DEVICE_RATE), DEVICE_RATE)
            await player.wait_drained()

        await asyncio.gather(producer())
        await asyncio.sleep(0.1)
    finally:
        await player.close()

    assert player.stats.underruns >= 1, "a starved feeder must be visible"
    total = stream.written_ms
    assert total >= 200, "the gap was left unrepaired"


async def test_an_overflowing_buffer_drops_blocks_instead_of_blocking() -> None:
    """A stalled device must not deadlock the event loop."""
    stream = FakeStream(DEVICE_RATE, write_delay_s=0.05)
    player = make_player(stream, queue_blocks=4)
    try:
        for _ in range(40):
            await asyncio.wait_for(player.enqueue(pcm(100, DEVICE_RATE), DEVICE_RATE), timeout=1.0)
        assert player.stats.dropped_blocks > 0
    finally:
        await player.close()


async def test_wait_drained_returns_only_after_the_tail_was_written() -> None:
    stream = FakeStream(DEVICE_RATE)
    player = make_player(stream)
    try:
        await player.enqueue(pcm(100, DEVICE_RATE), DEVICE_RATE)
        await player.enqueue(pcm(100, DEVICE_RATE), DEVICE_RATE)
        assert await player.wait_drained(timeout=2.0) is True
    finally:
        await player.close()

    assert stream.written_frames >= int(DEVICE_RATE * 0.19)


# ──────────────────────────────────────────────────────────────
# Barge-in
# ──────────────────────────────────────────────────────────────


async def test_interrupt_drops_the_queue_and_aborts_the_device_buffer() -> None:
    stream = FakeStream(DEVICE_RATE, write_delay_s=0.02)
    player = make_player(stream)
    try:
        for _ in range(20):
            await player.enqueue(pcm(100, DEVICE_RATE), DEVICE_RATE)
        await asyncio.sleep(0.05)

        before = stream.written_ms
        player.interrupt()
        await asyncio.sleep(0.1)

        assert stream.aborts == 1, "the device buffer was left to play out"
        assert player.stats.interrupted == 1
        # Whatever was queued is gone: the writes stop almost immediately.
        assert stream.written_ms - before < 400
    finally:
        await player.close()


async def test_frames_queued_before_an_interrupt_are_never_written() -> None:
    stream = FakeStream(DEVICE_RATE, write_delay_s=0.05)
    player = make_player(stream, prebuffer_ms=1000)
    try:
        await player.enqueue(pcm(100, DEVICE_RATE), DEVICE_RATE)
        await player.enqueue(pcm(100, DEVICE_RATE), DEVICE_RATE)
        player.interrupt()
        await asyncio.sleep(0.1)
    finally:
        await player.close()

    assert stream.writes == []


async def test_a_refusing_restart_is_retried_rather_than_giving_up() -> None:
    """
    Found on the real device: aborting from the event loop while the feeder is
    inside `write()` makes an immediate restart fail on MME ("Cannot perform this
    operation while media data is still playing"). Unhandled, that would leave the
    assistant silent for the rest of the session.
    """

    class ReluctantStream(FakeStream):
        def __init__(self, rate: int) -> None:
            super().__init__(rate)
            self.refusals = 2

        def start(self) -> None:
            if self.aborts and self.refusals:
                self.refusals -= 1
                raise RuntimeError("Cannot perform this operation while media data is still playing")
            super().start()

    stream = ReluctantStream(DEVICE_RATE)
    player = make_player(stream)
    try:
        await player.enqueue(pcm(100, DEVICE_RATE), DEVICE_RATE)
        await asyncio.sleep(0.05)
        player.interrupt()
        await asyncio.sleep(0.15)

        assert player.disabled is False, "the player gave up instead of retrying"
        assert stream.refusals == 0 and stream.starts == 2, (
            f"expected two refused restarts then success, saw refusals="
            f"{stream.refusals} starts={stream.starts}"
        )

        # …and the next utterance still plays.
        await player.enqueue(pcm(100, DEVICE_RATE), DEVICE_RATE)
        assert await player.wait_drained(timeout=2.0) is True
    finally:
        await player.close()

    assert stream.non_silent_ms() >= 90


async def test_a_stream_that_never_restarts_is_replaced() -> None:
    """A device that stops accepting `start()` is re-opened, not abandoned."""
    handed_out: list[FakeStream] = []

    class StubbornStream(FakeStream):
        def start(self) -> None:
            if self.aborts:
                raise RuntimeError("device is stuck")
            super().start()

    def opener(rate: int, blocksize_ms: int):
        # The first call hands back the stream the player already has… 
        stream = handed_out.pop(0) if handed_out else StubbornStream(rate)
        return stream, rate

    first = StubbornStream(DEVICE_RATE)
    handed_out.append(first)
    player = SpeechPlayer(sample_rate=DEVICE_RATE, stream_open=opener)
    try:
        await player.enqueue(pcm(100, DEVICE_RATE), DEVICE_RATE)
        assert player._stream is first
        await asyncio.sleep(0.05)
        player.interrupt()
        await asyncio.sleep(0.3)

        assert player._stream is not first, "the stuck stream was kept"
        assert player.disabled is False, "the player gave up instead of reopening"
        assert first.closed is True, "the stuck stream was never closed"
    finally:
        await player.close()


async def test_a_new_utterance_after_an_interrupt_plays_normally() -> None:
    stream = FakeStream(DEVICE_RATE)
    player = make_player(stream)
    try:
        await player.enqueue(pcm(100, DEVICE_RATE), DEVICE_RATE)
        player.interrupt()
        await player.wait_drained()

        await player.enqueue(pcm(100, DEVICE_RATE), DEVICE_RATE)
        assert await player.wait_drained(timeout=2.0) is True
    finally:
        await player.close()

    assert stream.non_silent_ms() >= 90


# ──────────────────────────────────────────────────────────────
# The silent player (stub runs, and every test that has no device)
# ──────────────────────────────────────────────────────────────


async def test_the_null_player_counts_what_it_was_given() -> None:
    player = NullSpeechPlayer()

    await player.enqueue(pcm(100, 8000), 8000, pause_after_ms=140)
    await player.wait_drained()
    player.interrupt()

    assert player.stats.audio_ms == 100
    assert player.stats.pause_ms == 140
    assert player.stats.frames_written == 800
    assert player.stats.interrupted == 1


# ──────────────────────────────────────────────────────────────
# Device invalidation mid-playback (AUDCLNT_E_DEVICE_INVALIDATED)
# ──────────────────────────────────────────────────────────────


class InvalidatedFirstStream(FakeStream):
    """A stream the endpoint is torn out from under: first write dies."""

    def __init__(self, rate: int) -> None:
        super().__init__(rate)
        self.failed = False

    def write(self, data: np.ndarray) -> None:
        if not self.failed:
            self.failed = True
            raise OSError(
                "Unanticipated host error [PaErrorCode -9999]: "
                "'AUDCLNT_E_DEVICE_INVALIDATED'"
            )
        super().write(data)


async def test_an_invalidated_device_recovers_and_keeps_speaking() -> None:
    """
    Windows can tear the endpoint down mid-playback (device change, driver
    reset, effects pipeline reloading — observed on a second machine's
    built-in speakers). Dying there used to silence the assistant until the
    next app start: `_disabled` was set on the first failure. The stream is
    rebuilt instead, and the session keeps talking.
    """
    first = InvalidatedFirstStream(DEVICE_RATE)
    second = FakeStream(DEVICE_RATE)
    calls = iter([(first, DEVICE_RATE), (second, DEVICE_RATE)])
    player = make_player(first, stream_open=lambda rate, blocksize: next(calls))
    try:
        await player.enqueue(pcm(200, DEVICE_RATE), DEVICE_RATE)
        await player.enqueue(pcm(200, DEVICE_RATE), DEVICE_RATE)
        await player.wait_drained()
    finally:
        await player.close()

    assert first.failed, "the scenario did not run"
    assert second.written_ms >= 190, "the rebuilt stream never spoke"
    assert not player.disabled, "the player disabled itself on a transient"
