"""
The spoken confirmation loop (`UNIMPLEMENTED.md` §2.3 / §2.4).

A destructive call (`write_file`, `run_script`) — and, behind the
`tools.search_web_confirm` switch, an explicit web search — is announced out
loud and then *waited for*: the microphone feeds ASR without a wake word while
the pipeline sits in `CONFIRMING`. 确认 replays the **same** `ToolCall` with
`confirmed=True`; 取消 does not run it; anything else cancels the call and is
handled as a brand-new utterance; silence expires it (TTL); and a different
speaker cannot confirm.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from winvoice.audio.asr import AsrResult
from winvoice.audio.pipeline import AudioPipeline, PipelineContext, PipelineState
from winvoice.contracts import IntentName, IntentResult, SpeakerTier, ToolCall, ToolName, ToolResult


AUDIO_BLOCK = b"\x00\x01" * 160  # 10 ms of int16 at 16 kHz

CONFIRMATION_REQUIRED = (
    "CONFIRMATION_REQUIRED: Write content to a file. Call again with confirmed=true."
)

# The spoken answer as the VAD delivered it (speech-only audio).
_SEGMENT = SimpleNamespace(samples=b"" * 3200, duration_ms=600)


class _RecordingTts:
    def __init__(self) -> None:
        self.spoken: list[str] = []

    async def synthesize(self, request):
        self.spoken.append(request.text)
        return
        yield  # pragma: no cover - makes this an async generator

    def is_interrupted(self) -> bool:
        return False

    def interrupt(self) -> None:
        pass


class _FakeExecutor:
    """Confirms like the real one for write_file; everything else succeeds."""

    def __init__(self) -> None:
        self.calls: list[tuple[ToolCall, bool]] = []

    async def execute(self, call, confirmed=False):
        self.calls.append((call, confirmed))
        if call.tool == ToolName.WRITE_FILE and not confirmed:
            return ToolResult(
                tool=call.tool, success=False,
                error=CONFIRMATION_REQUIRED,
                message="这个操作需要你先确认，暂时不能执行。",
            )
        return ToolResult(tool=call.tool, success=True, message="已经写好了。")


class _QueueRouter:
    def __init__(self, *intents: IntentResult) -> None:
        self._intents = list(intents)

    async def route(self, text, sv_result=None) -> IntentResult:
        return self._intents.pop(0) if len(self._intents) > 1 else self._intents[0]


class _EmittingVad:
    """Emits one fixed segment per block, so a tick reaches ASR."""

    def __init__(self, samples) -> None:
        self._samples = samples

    def accept_waveform(self, chunk):
        return [SimpleNamespace(samples=self._samples, duration_ms=100)]

    def reset(self) -> None:
        pass


class _FixedAsr:
    def __init__(self, text: str) -> None:
        self.text = text

    async def transcribe(self, samples) -> AsrResult:
        return AsrResult(text=self.text, language="zh", confidence=0.9)


def write_intent() -> IntentResult:
    return IntentResult(
        trace_id="t",
        intent=IntentName.WRITE_FILE,
        args={"path": "C:/Users/xinxi/notes.txt", "content": "hello"},
        confidence=0.9,
        source="local",
        raw_text="写入文件",
    )


async def _arm_write_confirmation() -> tuple[AudioPipeline, _FakeExecutor]:
    pipe = AudioPipeline(use_stub=True)
    await pipe.initialize()
    executor = _FakeExecutor()
    pipe.set_tool_executor(executor)
    pipe.set_intent_router(_QueueRouter(write_intent()))
    pipe.tts = _RecordingTts()
    pipe._context = PipelineContext(trace_id="t")

    await pipe._route_intent(AsrResult(text="写入文件", language="zh", confidence=0.9))
    return pipe, executor


# ── the question ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_destructive_call_announces_itself_and_waits() -> None:
    pipe, executor = await _arm_write_confirmation()

    assert len(executor.calls) == 1 and executor.calls[0][1] is False, "ran without asking"
    assert pipe._pending_confirmation is not None, "nothing is waiting for an answer"
    assert pipe.state == PipelineState.CONFIRMING, "the microphone must be listening for 确认"
    assert "确认请说确认" in pipe.tts.spoken[-1]
    assert "notes" not in pipe.tts.spoken[-1], "the Latin path must not be spoken"


@pytest.mark.asyncio
async def test_the_pending_call_is_replayed_verbatim() -> None:
    pipe, executor = await _arm_write_confirmation()
    original = pipe._pending_confirmation.call

    await pipe._resolve_confirmation(AsrResult(text="确认", language="zh", confidence=0.9), _SEGMENT)

    assert len(executor.calls) == 2
    replayed, confirmed = executor.calls[1]
    assert confirmed is True
    assert replayed is original, "a re-parsed call could change the arguments after the yes"
    assert pipe.state == PipelineState.KWS_LISTENING
    assert pipe._pending_confirmation is None


# ── the answers ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cancel_does_not_execute() -> None:
    pipe, executor = await _arm_write_confirmation()

    await pipe._resolve_confirmation(AsrResult(text="取消", language="zh", confidence=0.9), _SEGMENT)

    assert len(executor.calls) == 1, "the cancelled call ran anyway"
    assert "先不做了" in pipe.tts.spoken[-1]
    assert pipe._pending_confirmation is None


@pytest.mark.asyncio
async def test_deny_words_win_over_confirm_words() -> None:
    pipe, executor = await _arm_write_confirmation()

    await pipe._resolve_confirmation(AsrResult(text="不要确认", language="zh", confidence=0.9), _SEGMENT)

    assert len(executor.calls) == 1


@pytest.mark.asyncio
async def test_an_unrelated_answer_cancels_and_reroutes() -> None:
    pipe, executor = await _arm_write_confirmation()
    time_intent = IntentResult(
        trace_id="t", intent=IntentName.GET_TIME, args={},
        confidence=0.95, source="rules", raw_text="现在几点了",
    )
    pipe.set_intent_router(_QueueRouter(time_intent))
    pipe.asr = _FixedAsr("现在几点了")
    pipe.vad = _EmittingVad(b"\x00\x01" * 160)

    # The echo-decay window has passed; the answer arrives through the real tick.
    pipe._pending_confirmation.opened_at -= 1
    await pipe._tick(AUDIO_BLOCK)
    await asyncio.sleep(0.05)
    if pipe._turn_task is not None:
        await asyncio.wait_for(pipe._turn_task, timeout=2.0)

    assert pipe._pending_confirmation is None, "the pending call outlived the topic change"
    assert len(executor.calls) == 2, "the new utterance was not handled as a fresh request"
    assert executor.calls[1][0].tool == ToolName.GET_TIME


@pytest.mark.asyncio
async def test_silence_expires_the_pending_call() -> None:
    pipe, executor = await _arm_write_confirmation()
    pipe._pending_confirmation.deadline = time.monotonic() - 0.001

    await pipe._tick(AUDIO_BLOCK)

    assert pipe._pending_confirmation is None
    assert pipe.state == PipelineState.KWS_LISTENING
    assert len(executor.calls) == 1


@pytest.mark.asyncio
async def test_the_confirmer_is_verified_on_the_spoken_segment() -> None:
    """
    Live bug (2026-09-27): the owner's short 确认 was graded as **guest** on
    every attempt. The verifier ran over the rolling microphone window, which
    at confirmation time holds mostly the VAD's trailing silence (and a
    possible speaker echo) — the embedding degraded and the owner dropped a
    tier. The spoken VAD segment is what must be verified.
    """
    pipe, _executor = await _arm_write_confirmation()
    seen = {}

    def fake_verify(frames, speaker_id="me", adaptive=True):
        seen["frames"] = frames
        return SimpleNamespace(tier="full", score=0.7)

    pipe.sv = SimpleNamespace(enabled=True, verify=fake_verify)

    await pipe._resolve_confirmation(
        AsrResult(text="确认", language="zh", confidence=0.9), _SEGMENT
    )

    assert seen["frames"] is _SEGMENT.samples, "verified the room window, not the speech"


@pytest.mark.asyncio
async def test_a_speech_only_verification_no_longer_downgrades_the_owner() -> None:
    """Window grading said guest; the spoken segment says full → confirm runs."""
    pipe, executor = await _arm_write_confirmation()

    def downgrade_windows(frames, speaker_id="me", adaptive=True):
        if frames is _SEGMENT.samples:
            return SimpleNamespace(tier="full", score=0.7)
        return SimpleNamespace(tier="guest", score=0.5)

    pipe.sv = SimpleNamespace(enabled=True, verify=downgrade_windows)

    await pipe._resolve_confirmation(AsrResult(text="确认", language="zh", confidence=0.9), _SEGMENT)

    assert len(executor.calls) == 2 and executor.calls[1][1] is True, "the owner was refused again"


@pytest.mark.asyncio
async def test_a_rejected_voiceprint_asks_once_more_and_keeps_the_request() -> None:
    """
    A voiceprint below every band is a misread of a short utterance, not an
    authenticated impostor: the request stays armed and the assistant asks
    again (once). A guest simply fails again — nobody is promoted by a retry.
    """
    pipe, executor = await _arm_write_confirmation()
    pipe.sv = SimpleNamespace(
        enabled=True,
        verify=lambda frames, adaptive=True: SimpleNamespace(tier="rejected", score=0.3),
    )

    await pipe._resolve_confirmation(AsrResult(text="确认", language="zh", confidence=0.9), _SEGMENT)

    assert len(executor.calls) == 1, "the call ran on an unverified voice"
    assert pipe._pending_confirmation is not None, "the request was dropped"
    assert pipe._pending_confirmation.retries == 1
    assert "没有听清" in pipe.tts.spoken[-1]
    assert pipe.state == PipelineState.CONFIRMING


@pytest.mark.asyncio
async def test_a_second_rejection_ends_the_flow() -> None:
    pipe, executor = await _arm_write_confirmation()
    pipe.sv = SimpleNamespace(
        enabled=True,
        verify=lambda frames, adaptive=True: SimpleNamespace(tier="rejected", score=0.3),
    )

    await pipe._resolve_confirmation(AsrResult(text="确认", language="zh", confidence=0.9), _SEGMENT)
    await pipe._resolve_confirmation(AsrResult(text="确认", language="zh", confidence=0.9), _SEGMENT)

    assert pipe._pending_confirmation is None
    assert len(executor.calls) == 1


@pytest.mark.asyncio
async def test_the_confirmation_verifier_never_teaches_the_profile() -> None:
    """
    The EMA drift from gate-only verifications poisoned the profile until the
    owner scored 0.32 against their own voice: grading a confirmation must
    never update the enrolled embeddings.
    """
    pipe, _executor = await _arm_write_confirmation()
    kwargs_seen = {}

    def fake_verify(frames, speaker_id="me", adaptive=True):
        kwargs_seen["adaptive"] = adaptive
        return SimpleNamespace(tier="full", score=0.7)

    pipe.sv = SimpleNamespace(enabled=True, verify=fake_verify)

    await pipe._resolve_confirmation(AsrResult(text="确认", language="zh", confidence=0.9), _SEGMENT)

    assert kwargs_seen["adaptive"] is False


@pytest.mark.asyncio
async def test_the_mic_ignores_the_echo_window_right_after_the_question() -> None:
    """
    The tail of the spoken question reaches the microphone as room echo; if
    VAD is fed immediately, the echo is transcribed as (and voiceprinted as)
    the beginning of the answer.
    """
    pipe, _executor = await _arm_write_confirmation()
    fed = []

    class _CountingVad:
        def accept_waveform(self, chunk):
            fed.append(chunk)
            return []

        def reset(self):
            pass

    pipe.vad = _CountingVad()
    # opened_at is "now" (just armed) — the echo guard must hold.
    await pipe._tick(AUDIO_BLOCK)
    assert fed == [], "VAD was fed during the echo-decay window"

    pipe._pending_confirmation.opened_at -= 1  # decay passed
    await pipe._tick(AUDIO_BLOCK)
    assert fed, "VAD never fed after the echo window"


@pytest.mark.asyncio
async def test_a_second_speaker_cannot_confirm() -> None:
    pipe, executor = await _arm_write_confirmation()
    pipe.sv = SimpleNamespace(
        enabled=True,
        verify=lambda frames, adaptive=True: SimpleNamespace(tier="guest", score=0.5),
    )

    await pipe._resolve_confirmation(AsrResult(text="确认", language="zh", confidence=0.9), _SEGMENT)

    assert len(executor.calls) == 1, "the call ran for the wrong speaker"
    assert "确认" in pipe.tts.spoken[-1] or "先不做了" in pipe.tts.spoken[-1]


# ── search_web asks first (config `tools.search_web_confirm`) ──


@pytest.mark.asyncio
async def test_an_explicit_search_asks_before_opening_the_browser() -> None:
    pipe = AudioPipeline(use_stub=True)
    await pipe.initialize()
    executor = _FakeExecutor()
    pipe.set_tool_executor(executor)
    pipe.set_intent_router(_QueueRouter(IntentResult(
        trace_id="t", intent=IntentName.SEARCH_WEB, args={"query": "量子力学"},
        confidence=0.95, source="rules", raw_text="搜索量子力学",
    )))
    pipe.tts = _RecordingTts()
    pipe._context = PipelineContext(trace_id="t")

    await pipe._route_intent(AsrResult(text="搜索量子力学", language="zh", confidence=0.9))

    assert executor.calls == [], "the browser opened without asking"
    assert pipe.state == PipelineState.CONFIRMING
    assert "量子力学" in pipe.tts.spoken[-1]

    await pipe._resolve_confirmation(AsrResult(text="确认", language="zh", confidence=0.9), _SEGMENT)
    assert len(executor.calls) == 1 and executor.calls[0][1] is True


@pytest.mark.asyncio
async def test_search_confirmation_can_be_switched_off(monkeypatch) -> None:
    from winvoice.audio import pipeline as pipeline_module

    class _OffConfig:
        def get(self, key, default=None):
            if key == "tools.search_web_confirm":
                return False
            return default

    monkeypatch.setattr(pipeline_module, "get_config", lambda *a, **k: _OffConfig())

    pipe = AudioPipeline(use_stub=True)
    await pipe.initialize()
    executor = _FakeExecutor()
    pipe.set_tool_executor(executor)
    pipe.set_intent_router(_QueueRouter(IntentResult(
        trace_id="t", intent=IntentName.SEARCH_WEB, args={"query": "量子力学"},
        confidence=0.95, source="rules", raw_text="搜索量子力学",
    )))
    pipe.tts = _RecordingTts()
    pipe._context = PipelineContext(trace_id="t")

    await pipe._route_intent(AsrResult(text="搜索量子力学", language="zh", confidence=0.9))

    assert len(executor.calls) == 1 and executor.calls[0][1] is False, "the switch did not"
    assert pipe._pending_confirmation is None


# ── the executor contract stays intact for the MCP path ───────


@pytest.mark.asyncio
async def test_an_executor_without_a_confirmed_kwarg_fails_honestly() -> None:
    pipe = AudioPipeline(use_stub=True)
    await pipe.initialize()

    class _OldExecutor:
        async def execute(self, call):
            return ToolResult(tool=call.tool, success=True, message="已经写好了。")

    pipe.set_tool_executor(_OldExecutor())
    from winvoice.audio.pipeline import _PendingConfirmation

    pipe._pending_confirmation = _PendingConfirmation(
        call=ToolCall(
            trace_id="t", tool=ToolName.WRITE_FILE,
            args={"path": "x", "content": "y"}, tier=SpeakerTier.FULL,
        ),
        deadline=time.monotonic() + 15,
    )

    result = await pipe._execute_confirmed(pipe._pending_confirmation.call)

    assert result.success is False, "retrying without the flag would only re-ask"
