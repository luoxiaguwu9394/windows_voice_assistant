"""
Barge-in and the non-blocking turn.

The defect these pin down was not visible in any log: while a turn was running,
the pipeline's tick loop was *inside* it — `_tick` awaited `_route_intent`, which
awaited the agent, which awaited synthesis. Nothing consumed `_audio_queue`, so a
wake word said during playback was not noticed until playback ended, and by then
the microphone buffer held a backlog that no longer lined up with anything. The
documented claim "KWS stays active during TTS for barge-in" was true only on
paper; `UNIMPLEMENTED.md` §2.0 recorded the agent case, and playback had it too.

The turn is a task now, and a barge-in does two things:

* **stops the sound** — `player.interrupt()` aborts the device buffer, so the
  audio does not play out to the end of the chunk;
* **disowns the turn** — bumping `_turn_generation` means the reply being
  composed is dropped instead of spoken over the user's new command.

The harness turn itself is deliberately *not* cancelled: DSH has already been
handed the prompt, and cancelling leaves it in an unknown state for a problem
that discarding the output already solves.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from winvoice.audio.asr import AsrResult
from winvoice.audio.pipeline import AudioPipeline, PipelineContext, PipelineState
from winvoice.audio.playback import PlaybackStats
from winvoice.contracts import IntentName, IntentResult

AUDIO_BLOCK = b"\x00\x01" * 160  # 10 ms of int16 at 16 kHz


class _FakeKws:
    """A wake-word detector that fires when the test says so."""

    def __init__(self) -> None:
        self.trigger = False
        self.accepted = 0
        self.resets = 0

    def accept_waveform(self, chunk) -> None:
        self.accepted += 1

    def get_result(self):
        if not self.trigger:
            return None
        self.trigger = False
        return SimpleNamespace(keyword="assistant", confidence=0.9)

    def reset(self) -> None:
        self.resets += 1


class _RecordingPlayer:
    """Stands in for the speaker: records order, pauses and interruptions."""

    def __init__(self) -> None:
        self.enqueued: list[tuple[int, int, int]] = []
        self.started = 0
        self.drained = 0
        self.interrupted = 0
        self.closed = 0
        self.stats = PlaybackStats()

    async def start(self) -> None:
        self.started += 1

    async def enqueue(self, pcm: bytes, sample_rate: int, pause_after_ms: int = 0) -> None:
        self.enqueued.append((len(pcm), sample_rate, pause_after_ms))

    async def wait_drained(self, timeout: float = 10.0) -> bool:
        self.drained += 1
        return True

    def interrupt(self) -> None:
        self.interrupted += 1

    async def close(self) -> None:
        self.closed += 1

    @property
    def pauses(self) -> list[int]:
        return [pause for _, _, pause in self.enqueued if pause]


class _FixedRouter:
    def __init__(self, intent: IntentResult) -> None:
        self._intent = intent

    async def route(self, text, sv_result=None) -> IntentResult:
        return self._intent


class _GatedAgent:
    """An agent that is 'thinking' until the test opens the gate."""

    enabled = True

    def __init__(self, gate: asyncio.Event, response: str) -> None:
        self.gate = gate
        self.response = response

    async def route(self, text, trace_id: str = "", tier: str = "full"):
        await self.gate.wait()
        return SimpleNamespace(
            resolved=True,
            response=self.response,
            source="local",
            failure=None,
            escalation_reason=None,
            assessment=None,
        )


def unknown_intent() -> IntentResult:
    return IntentResult(
        trace_id="t", intent=IntentName.UNKNOWN, args={}, confidence=0.0,
        source="local", raw_text="随便说点什么",
    )


def build_pipeline(agent=None, player=None) -> AudioPipeline:
    pipe = AudioPipeline(use_stub=True)
    pipe.kws = _FakeKws()
    pipe.set_intent_router(_FixedRouter(unknown_intent()))
    if player is not None:
        pipe.set_speech_player(player)
    if agent is not None:
        pipe.set_dsh_router(agent)
    return pipe


# ──────────────────────────────────────────────────────────────
# Barge-in
# ──────────────────────────────────────────────────────────────


async def test_a_wake_word_during_playback_stops_the_sound_and_disowns_the_turn() -> None:
    player = _RecordingPlayer()
    pipe = build_pipeline(player=player)
    pipe._state = PipelineState.TTS_PLAYING
    generation = pipe._turn_generation
    pipe.kws.trigger = True

    await pipe._tick(AUDIO_BLOCK)

    assert player.interrupted == 1, "the speaker was left playing"
    assert pipe.tts.is_interrupted() is True, "synthesis was left running"
    assert pipe._turn_generation == generation + 1, "the turn still owns the output"


async def test_the_speaker_is_interrupted_before_the_new_utterance_starts() -> None:
    """Order matters: the user must not be talked over while being listened to."""
    player = _RecordingPlayer()
    pipe = build_pipeline(player=player)
    pipe._state = PipelineState.TTS_PLAYING
    pipe.kws.trigger = True

    await pipe._tick(AUDIO_BLOCK)

    assert player.interrupted == 1
    assert pipe.state is PipelineState.VAD_ACTIVE  # the new utterance is listening


async def test_a_turn_superseded_while_the_agent_thinks_is_never_spoken() -> None:
    gate = asyncio.Event()
    player = _RecordingPlayer()
    agent = _GatedAgent(gate, "我已经把文件保存好了。接下来告诉你结果。")
    pipe = build_pipeline(agent=agent, player=player)
    pipe._context = PipelineContext(trace_id="t")

    pipe._start_turn(AsrResult(text="帮我保存文件", language="zh", confidence=0.9))
    await asyncio.sleep(0.05)
    assert pipe.state is PipelineState.AGENT_THINKING

    pipe._barge_in("assistant")  # the wake word arrives mid-planning
    gate.set()  # …the agent still finishes its work
    await asyncio.wait_for(pipe._turn_task, timeout=2.0)

    assert player.enqueued == [], "the abandoned turn talked over the new command"
    assert pipe._context is not None, "the superseded turn wiped the new utterance's context"


async def test_an_uninterrupted_turn_speaks_normally() -> None:
    gate = asyncio.Event()
    gate.set()
    player = _RecordingPlayer()
    agent = _GatedAgent(gate, "第一步完成了。第二步完成了。第三步也完成了。")
    pipe = build_pipeline(agent=agent, player=player)
    pipe._context = PipelineContext(trace_id="t")

    pipe._start_turn(AsrResult(text="三步都做了吗", language="zh", confidence=0.9))
    await asyncio.wait_for(pipe._turn_task, timeout=2.0)

    assert player.started == 1
    assert player.drained == 1
    assert len(player.pauses) >= 2, "the sentences did not become separate segments"
    assert pipe.state is PipelineState.KWS_LISTENING


# ──────────────────────────────────────────────────────────────
# The turn must not hold the tick loop
# ──────────────────────────────────────────────────────────────


async def test_audio_is_still_consumed_while_the_agent_thinks() -> None:
    """
    The regression: the queue used to grow during a turn, and the wake word in it
    was only handled after the turn ended.
    """
    gate = asyncio.Event()
    pipe = build_pipeline(agent=_GatedAgent(gate, "好的。"))
    pipe._context = PipelineContext(trace_id="t")
    pipe._start_turn(AsrResult(text="随便说点什么", language="zh", confidence=0.9))
    await asyncio.sleep(0.05)

    accepted_before = pipe.kws.accepted
    pipe._running = True
    runner = asyncio.create_task(pipe.run())
    for _ in range(3):
        pipe.push_audio(AUDIO_BLOCK)
    await asyncio.sleep(0.1)

    accepted = pipe.kws.accepted - accepted_before
    pipe.stop()
    gate.set()
    await asyncio.wait_for(runner, timeout=2.0)

    assert accepted >= 3, "the tick loop did not consume audio during the agent turn"


async def test_a_wake_word_while_the_agent_thinks_starts_a_new_utterance() -> None:
    gate = asyncio.Event()
    player = _RecordingPlayer()
    pipe = build_pipeline(agent=_GatedAgent(gate, "好的。"), player=player)
    pipe._context = PipelineContext(trace_id="t")
    pipe._start_turn(AsrResult(text="随便说点什么", language="zh", confidence=0.9))
    await asyncio.sleep(0.05)

    pipe._running = True
    runner = asyncio.create_task(pipe.run())
    pipe.kws.trigger = True
    pipe.push_audio(AUDIO_BLOCK)
    await asyncio.sleep(0.1)

    listening = pipe.state in (PipelineState.VAD_ACTIVE, PipelineState.KWS_LISTENING)
    pipe.stop()
    gate.set()
    await asyncio.wait_for(runner, timeout=2.0)

    assert listening, "the wake word was queued behind the agent's turn instead of handled"


async def test_the_audio_queue_does_not_grow_without_bound() -> None:
    gate = asyncio.Event()
    pipe = build_pipeline(agent=_GatedAgent(gate, "好的。"))
    pipe._context = PipelineContext(trace_id="t")

    for _ in range(500):
        pipe.push_audio(AUDIO_BLOCK)
    pipe._running = True
    runner = asyncio.create_task(pipe.run())
    await asyncio.sleep(0.1)

    depth = len(pipe._audio_queue)
    pipe.stop()
    gate.set()
    await asyncio.wait_for(runner, timeout=2.0)

    assert depth < 100, f"the queue grew to {depth}: the loop is not consuming"


# ──────────────────────────────────────────────────────────────
# The pipeline -> player contract
# ──────────────────────────────────────────────────────────────


async def test_segments_reach_the_player_in_order_with_their_pauses() -> None:
    """
    End to end through the real (stub) engine: three sentences in, three pauses
    out, in the order the pause table prescribes.
    """
    gate = asyncio.Event()
    gate.set()
    player = _RecordingPlayer()
    agent = _GatedAgent(gate, "第一句已经完成了。第二句也完成了。第三句正在处理。")
    pipe = build_pipeline(agent=agent, player=player)
    pipe._context = PipelineContext(trace_id="t")

    pipe._start_turn(AsrResult(text="做完了吗", language="zh", confidence=0.9))
    await asyncio.wait_for(pipe._turn_task, timeout=3.0)

    assert player.pauses == [240, 240, 150], player.pauses
    assert all(rate == 16000 for _, rate, _ in player.enqueued)


async def test_an_english_answer_is_replaced_by_an_honest_sentence_not_by_silence() -> None:
    """
    The agent answered in English, which the Chinese lexicon cannot pronounce.
    Saying nothing would leave the user unsure whether the assistant heard them;
    the contract is to say something honest instead.
    """
    gate = asyncio.Event()
    gate.set()
    player = _RecordingPlayer()
    spoken: list[str] = []
    pipe = build_pipeline(agent=_GatedAgent(gate, "Done! The file was created."), player=player)
    pipe.on_tts_chunk = lambda chunk: spoken.append(getattr(chunk, "text", ""))
    pipe._context = PipelineContext(trace_id="t")

    pipe._start_turn(AsrResult(text="做了吗", language="zh", confidence=0.9))
    await asyncio.wait_for(pipe._turn_task, timeout=3.0)

    said = "".join(spoken)
    assert "Done" not in said and "created" not in said, said
    assert "说不清楚" in said, f"the honest fallback was not spoken: {said!r}"
    assert player.enqueued, "nothing was played at all"


async def test_shutdown_releases_the_player_and_the_engine() -> None:
    player = _RecordingPlayer()
    pipe = build_pipeline(player=player)

    await pipe.shutdown()

    assert player.closed == 1
    assert player.interrupted == 1


@pytest.mark.parametrize("state", [PipelineState.TTS_PLAYING, PipelineState.AGENT_THINKING])
async def test_kws_is_fed_in_every_talking_state(state: PipelineState) -> None:
    pipe = build_pipeline()
    pipe._state = state
    pipe.kws.trigger = False

    await pipe._tick(AUDIO_BLOCK)

    assert pipe.kws.accepted == 1
