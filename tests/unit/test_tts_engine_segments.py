"""
The TTS engine: model selection, per-segment synthesis, and off-loop execution.

Two things replaced the old "synthesise the whole reply in one call, then chunk
the finished audio" design, and both are pinned here:

* **per segment** — one `generate()` per cut, each carrying the pause that
  follows it, so the rhythm comes from the pause table instead of from whatever
  the acoustic model emitted;
* **off the event loop** — `generate()` runs on a worker thread, which is what
  lets the pipeline notice a wake word *while* the assistant is speaking. The old
  implementation blocked the loop from the first call to the last.

The fake engine below is a `generate()` that records what it was asked to say and
returns deterministic noise; no model, no microphone, no audio device.
"""

from __future__ import annotations

import asyncio
import shutil
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pytest

from winvoice.audio._common import ModelNotFoundError
from winvoice.audio.tts import TtsEngine, resolve_tts_model
from winvoice.contracts import TtsRequest
from winvoice.text import SpeechTextConfig

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


@pytest.fixture
def work():
    """Scratch space inside the repo (never the system temp dir)."""
    base = PROJECT_ROOT / "runtime" / "_pytest_work"
    base.mkdir(parents=True, exist_ok=True)
    directory = base / uuid.uuid4().hex[:12]
    directory.mkdir(parents=True, exist_ok=True)
    try:
        yield directory
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def make_vits(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    for name in ("model.onnx", "tokens.txt", "lexicon.txt", "number.fst"):
        (directory / name).write_bytes(b"stub")
    return directory


def make_matcha(directory: Path, *, with_vocoder: bool = True) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    for name in ("model-steps-3.onnx", "tokens.txt", "lexicon.txt"):
        (directory / name).write_bytes(b"stub")
    if with_vocoder:
        (directory / "vocos-22khz-univ.onnx").write_bytes(b"stub")
    return directory


# ──────────────────────────────────────────────────────────────
# Model resolution
# ──────────────────────────────────────────────────────────────


def test_a_vits_model_is_recognised(work: Path) -> None:
    model = resolve_tts_model(str(make_vits(work / "vits")))

    assert model.backend == "vits"
    assert model.model_file.name == "model.onnx"
    assert model.lexicon_file is not None
    assert [path.name for path in model.rule_fsts] == ["number.fst"]


def test_a_matcha_model_is_recognised_with_its_vocoder(work: Path) -> None:
    model = resolve_tts_model(str(make_matcha(work / "matcha")))

    assert model.backend == "matcha"
    assert model.model_file.name == "model-steps-3.onnx"
    assert model.vocoder_file is not None
    assert model.vocoder_file.name == "vocos-22khz-univ.onnx"


def test_the_configured_model_wins_when_several_exist(work: Path) -> None:
    table = make_matcha(work / "matcha")
    make_vits(work / "vits")

    model = resolve_tts_model(str(table), [str(work / "vits")])

    assert model.directory == table


def test_an_incomplete_model_falls_back_instead_of_failing(work: Path) -> None:
    """
    A Matcha acoustic model without its vocoder cannot speak, but the machine
    that also has the old model must keep talking — with a warning, not a crash.
    """
    make_matcha(work / "matcha", with_vocoder=False)
    fallback = make_vits(work / "vits")

    model = resolve_tts_model(str(work / "matcha"), [str(fallback)])

    assert model.backend == "vits"
    assert model.directory == fallback


def test_a_missing_model_names_every_reason_and_the_fix(work: Path) -> None:
    with pytest.raises(ModelNotFoundError) as error:
        resolve_tts_model(str(work / "nope"), [str(work / "also-nope")])

    message = str(error.value)
    assert "nope" in message and "also-nope" in message
    assert "download_models.py" in message


def test_a_missing_vocoder_explains_itself_when_nothing_else_works(work: Path) -> None:
    make_matcha(work / "matcha", with_vocoder=False)

    # An explicit empty fallback list means "this model or nothing".
    with pytest.raises(ModelNotFoundError) as error:
        resolve_tts_model(str(work / "matcha"), [])

    assert "vocoder" in str(error.value).lower()
    assert "vocos" in str(error.value)


def test_a_forced_backend_is_honoured(work: Path) -> None:
    make_vits(work / "vits")

    with pytest.raises(ModelNotFoundError) as error:
        resolve_tts_model(str(work / "vits"), backend="matcha")

    assert "matcha" in str(error.value)


# ──────────────────────────────────────────────────────────────
# A fake sherpa-onnx engine
# ──────────────────────────────────────────────────────────────


class _FakeAudio:
    def __init__(self, samples: np.ndarray, sample_rate: int) -> None:
        self.samples = samples
        self.sample_rate = sample_rate


class _FakeOfflineTts:
    """Records every `generate()` call; returns deterministic noise."""

    def __init__(
        self,
        sample_rate: int = 22050,
        num_speakers: int = 1,
        delay_s: float = 0.0,
        lead_ms: int = 0,
        tail_ms: int = 0,
    ) -> None:
        self.sample_rate = sample_rate
        self.num_speakers = num_speakers
        self.delay_s = delay_s
        self.lead_ms = lead_ms
        self.tail_ms = tail_ms
        self.calls: list[dict] = []
        self.on_call = None

    def generate(self, text: str, sid: int = 0, speed: float = 1.0):
        self.calls.append({"text": text, "sid": sid, "speed": speed})
        if self.delay_s:
            time.sleep(self.delay_s)
        if self.on_call is not None:
            self.on_call(len(self.calls))

        rate = self.sample_rate
        speech = np.full(int(rate * 0.02 * len(text)), 0.3, dtype=np.float32)
        pad_lead = np.full(int(rate * self.lead_ms / 1000), 0.002, dtype=np.float32)
        pad_tail = np.full(int(rate * self.tail_ms / 1000), 0.002, dtype=np.float32)
        return _FakeAudio(np.concatenate([pad_lead, speech, pad_tail]), rate)


def engine_with(fake: _FakeOfflineTts, **speech_kwargs) -> TtsEngine:
    engine = TtsEngine()
    engine._tts = fake
    engine._executor = ThreadPoolExecutor(max_workers=1)
    engine._ready = True
    engine.speech = SpeechTextConfig(**speech_kwargs)
    engine.pitch = 1.0  # pinned: the config file's real value must not leak into these tests
    return engine


async def drain(engine: TtsEngine, text: str, **request_kwargs) -> list:
    return [chunk async for chunk in engine.synthesize(TtsRequest(text=text, **request_kwargs))]


def shutdown(engine: TtsEngine) -> None:
    if engine._executor is not None:
        engine._executor.shutdown(wait=False)


# ──────────────────────────────────────────────────────────────
# One call per segment, and the pause travels with it
# ──────────────────────────────────────────────────────────────


async def test_one_generate_call_per_segment() -> None:
    fake = _FakeOfflineTts()
    engine = engine_with(fake, trim_silence=False)
    try:
        chunks = await drain(engine, "你好。今天天气不错。")
    finally:
        shutdown(engine)

    assert [call["text"] for call in fake.calls] == ["你好。", "今天天气不错。"]
    assert {chunk.text for chunk in chunks} == {"你好。", "今天天气不错。"}


async def test_the_pause_rides_on_the_last_chunk_of_each_segment() -> None:
    fake = _FakeOfflineTts()
    config = SpeechTextConfig(trim_silence=False)
    engine = engine_with(fake, trim_silence=False)
    try:
        chunks = await drain(engine, "你好。今天天气不错。")
    finally:
        shutdown(engine)

    pauses = [chunk.pause_after_ms for chunk in chunks if chunk.pause_after_ms]
    assert pauses == [config.pause_sentence_ms, config.tail_silence_ms]
    # A pause belongs to the last chunk of its segment only: silence inserted
    # mid-sentence would be heard as a stutter.
    for index, chunk in enumerate(chunks):
        last_of_segment = index + 1 == len(chunks) or chunks[index + 1].text != chunk.text
        assert (chunk.pause_after_ms > 0) is last_of_segment


async def test_only_the_very_last_chunk_is_final() -> None:
    fake = _FakeOfflineTts()
    engine = engine_with(fake, trim_silence=False)
    try:
        chunks = await drain(engine, "你好。今天天气不错。")
    finally:
        shutdown(engine)

    assert [chunk.is_final for chunk in chunks].count(True) == 1
    assert chunks[-1].is_final is True


async def test_the_first_segment_is_the_short_one() -> None:
    """Audio has to start early; the segmentation makes the first cut early."""
    fake = _FakeOfflineTts()
    config = SpeechTextConfig(trim_silence=False)
    engine = engine_with(fake, trim_silence=False)
    reply = (
        "好的明白了，我马上就去办这件事情，你别着急，很快就能处理好。"
    )
    try:
        await drain(engine, reply, max_chars=240)
    finally:
        shutdown(engine)

    assert len(fake.calls) > 1
    assert len(fake.calls[0]["text"]) <= config.first_chunk_max_chars


async def test_a_long_reply_is_no_longer_clipped_to_eighty_characters() -> None:
    """
    The 80-character budget existed because the whole utterance was synthesised
    before any sound: length *was* dead air. With per-segment playback the
    caller chooses the budget, and an agent's answer gets the wider one.
    """
    fake = _FakeOfflineTts()
    engine = engine_with(fake, trim_silence=False)
    reply = "我已经把这部分内容处理好了，接下来会继续检查剩下的项目。" * 4
    try:
        await drain(engine, reply, max_chars=240)
        spoken = sum(len(call["text"]) for call in fake.calls)
    finally:
        shutdown(engine)

    assert spoken > 80
    assert spoken <= 240


async def test_the_default_budget_still_clips_a_tool_one_liner() -> None:
    fake = _FakeOfflineTts()
    engine = engine_with(fake, trim_silence=False)
    try:
        await drain(engine, "已经完成了。" * 40)
        spoken = sum(len(call["text"]) for call in fake.calls)
    finally:
        shutdown(engine)

    assert spoken <= 80


async def test_the_sample_rate_of_the_model_reaches_the_chunks() -> None:
    fake = _FakeOfflineTts(sample_rate=22050)
    engine = engine_with(fake, trim_silence=False)
    try:
        chunks = await drain(engine, "你好。")
    finally:
        shutdown(engine)

    assert {chunk.sample_rate for chunk in chunks} == {22050}


async def test_nothing_speakable_produces_no_call_at_all() -> None:
    fake = _FakeOfflineTts()
    engine = engine_with(fake, trim_silence=False)
    try:
        chunks = await drain(engine, "Done! The file was created successfully.")
    finally:
        shutdown(engine)

    assert chunks == []
    assert fake.calls == []


async def test_a_request_without_text_is_ignored() -> None:
    fake = _FakeOfflineTts()
    engine = engine_with(fake, trim_silence=False)
    try:
        assert await drain(engine, "   ") == []
    finally:
        shutdown(engine)

    assert fake.calls == []


# ──────────────────────────────────────────────────────────────
# Off the event loop, and interruptible
# ──────────────────────────────────────────────────────────────


async def test_synthesis_does_not_block_the_event_loop() -> None:
    """
    The regression this prevents: while the old engine called `generate()`
    inline, the pipeline's tick loop could not consume microphone audio, so a
    wake word said during playback was only handled after it finished.
    """
    fake = _FakeOfflineTts(delay_s=0.2)
    engine = engine_with(fake, trim_silence=False)

    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(0.01)

    task = asyncio.create_task(ticker())
    try:
        await drain(engine, "你好。")
    finally:
        task.cancel()
        shutdown(engine)

    assert ticks >= 5, "the event loop was blocked while synthesising"


async def test_interrupt_stops_after_the_segment_in_flight() -> None:
    """
    The guarantee is "no new segment is started", not "the reply is finished".

    The segment whose `generate()` was already running cannot be cancelled, but
    nothing more is produced — and that is the bound that matters, because the
    player has already dropped the audio it had queued.
    """
    fake = _FakeOfflineTts()
    engine = engine_with(fake, trim_silence=False)
    fake.on_call = lambda count: engine.interrupt() if count == 1 else None
    reply = (
        "好的明白了，我马上就去办这件事情，你别着急，"
        "很快就能处理好，请稍等片刻，我这就去做。"
    )
    try:
        chunks = await drain(engine, reply, max_chars=240)
    finally:
        shutdown(engine)

    assert len(fake.calls) == 1, "synthesis carried on after the interrupt"
    assert chunks == [], "no further audio should be yielded after an interrupt"
    assert engine.is_interrupted() is True


async def test_a_new_request_clears_a_previous_interrupt() -> None:
    fake = _FakeOfflineTts()
    engine = engine_with(fake, trim_silence=False)
    engine.interrupt()
    try:
        chunks = await drain(engine, "你好。")
    finally:
        shutdown(engine)

    assert chunks


# ──────────────────────────────────────────────────────────────
# Voices, and trimming
# ──────────────────────────────────────────────────────────────


async def test_the_guest_voice_uses_the_guest_speaker_when_there_is_one() -> None:
    fake = _FakeOfflineTts(num_speakers=4)
    engine = engine_with(fake, trim_silence=False)
    engine.guest_speaker_id = 2
    try:
        await drain(engine, "你好。", voice="guest")
        await drain(engine, "你好。", voice="default")
    finally:
        shutdown(engine)

    assert fake.calls[0]["sid"] == 2
    assert fake.calls[0]["speed"] == engine.speed
    assert fake.calls[1]["sid"] == engine.speaker_id


async def test_a_single_speaker_model_gives_the_guest_a_different_pace() -> None:
    """
    Matcha zh-baker has one voice, so the speaker id cannot tell the guest
    apart; the pace is the only honest difference left.
    """
    fake = _FakeOfflineTts(num_speakers=1)
    engine = engine_with(fake, trim_silence=False)
    engine.guest_speed = 1.2
    engine.speed = 1.0
    try:
        await drain(engine, "你好。", voice="guest")
        await drain(engine, "你好。", voice="default")
    finally:
        shutdown(engine)

    assert fake.calls[0]["speed"] == 1.2
    assert fake.calls[1]["speed"] == 1.0


async def test_the_models_edge_silence_is_trimmed_before_playback() -> None:
    fake = _FakeOfflineTts(lead_ms=120, tail_ms=240)
    trimmed_engine = engine_with(fake, trim_silence=True, trim_ratio=0.05, trim_guard_ms=30)
    raw_engine = engine_with(fake, trim_silence=False)
    try:
        trimmed = await drain(trimmed_engine, "你好。")
        raw = await drain(raw_engine, "你好。")
    finally:
        shutdown(trimmed_engine)
        shutdown(raw_engine)

    trimmed_bytes = sum(len(chunk.data) for chunk in trimmed)
    raw_bytes = sum(len(chunk.data) for chunk in raw)
    assert trimmed_bytes < raw_bytes
    # 120 + 240 ms of floor, minus the two 30 ms guards, is what should go.
    lost_ms = (raw_bytes - trimmed_bytes) / 2 / 22050 * 1000
    assert 240 <= lost_ms <= 340


async def test_trimming_can_be_switched_off() -> None:
    fake = _FakeOfflineTts(lead_ms=200, tail_ms=200)
    engine = engine_with(fake, trim_silence=False)
    try:
        chunks = await drain(engine, "你好。")
    finally:
        shutdown(engine)

    assert sum(len(chunk.data) for chunk in chunks) > 0
    assert engine.speech.trim_silence is False


async def test_pitch_relables_the_rate_and_compensates_the_speed() -> None:
    """
    `tts.pitch` lowers the voice by claiming a lower sample rate for the chunks
    (the player then stretches them) and asking `generate()` for
    `speed / pitch` so the tempo pays the stretch back. The two knobs stay
    independent: `tts.speed` means tempo, `tts.pitch` means pitch.
    """
    fake = _FakeOfflineTts(sample_rate=22050)
    engine = engine_with(fake, trim_silence=False)
    engine.pitch = 0.9
    try:
        chunks = await drain(engine, "你好。")
    finally:
        shutdown(engine)

    assert chunks[0].sample_rate == int(round(22050 * 0.9))
    assert fake.calls[0]["speed"] == pytest.approx(engine.speed / 0.9)


async def test_pitch_one_is_a_no_op() -> None:
    fake = _FakeOfflineTts(sample_rate=22050)
    engine = engine_with(fake, trim_silence=False)
    try:
        chunks = await drain(engine, "你好。")
    finally:
        shutdown(engine)

    assert chunks[0].sample_rate == 22050
    assert fake.calls[0]["speed"] == engine.speed


async def test_pitch_leaves_the_spoken_duration_unchanged() -> None:
    """
    The relabel stretches by 1/pitch and the compensated speed shrinks by
    pitch: end to end the reply takes the same time as at pitch 1.0, it just
    sounds lower.
    """
    class _SpeedAwareTts:
        """The shared fake ignores `speed`; the round trip needs it honoured."""

        sample_rate = 22050
        num_speakers = 1

        def generate(self, text: str, sid: int = 0, speed: float = 1.0):
            rate = self.sample_rate
            speech = np.full(int(rate * 0.02 * len(text) / speed), 0.3, dtype=np.float32)
            return _FakeAudio(speech, rate)

    async def total_ms(pitch: float) -> float:
        fake = _SpeedAwareTts()
        engine = engine_with(fake, trim_silence=False)
        engine.pitch = pitch
        try:
            chunks = await drain(engine, "北京今天晴，气温十到二十度，现在十五度。")
        finally:
            shutdown(engine)
        rate = chunks[0].sample_rate
        return sum(len(c.data) for c in chunks) / 2 / rate * 1000

    plain = await total_ms(1.0)
    lowered = await total_ms(0.9)
    assert lowered == pytest.approx(plain, rel=0.02)
