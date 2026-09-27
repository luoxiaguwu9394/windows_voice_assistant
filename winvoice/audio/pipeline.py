"""
Audio Pipeline Orchestrator (single-process asyncio).

    KWS -> SV verify -> VAD segment -> ASR -> intent routing -> tools -> TTS

Half-duplex: ASR/VAD are paused while TTS plays, but KWS stays fed so the
wake word can interrupt playback (barge-in).

Audio arrives through `push_audio()` (called from the microphone callback)
and is consumed by `run()` in a cooperative loop.
"""

from __future__ import annotations

import asyncio
import inspect
import re
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Deque, List, Optional, Union

from winvoice.config import get_config
from winvoice.contracts import (
    IntentName,
    IntentResult,
    MAX_SPEECH_CHARS,
    SpeakerTier,
    SystemState,
    SystemStateName,
    ToolCall,
    ToolName,
    ToolResult,
    TtsRequest,
    clip_for_speech,
    has_latin,
    sanitize_for_tts,
)
from winvoice.logging import clear_trace_context, get_logger, set_trace_context
from winvoice.text import speech_text_config
from .asr import AsrEngine, AsrResult, create_asr_engine
from .kws import KwsEngine, KwsResult, create_kws_engine
from .playback import NullSpeechPlayer, SpeechPlayer
from .sv import SvEngine, SvResult, create_sv_engine
from .tts import TtsChunk, TtsEngine, create_tts_engine
from .vad import VadEngine, VadSegment, create_vad_engine

logger = get_logger(__name__)

# How many recent 10 ms frames (~1 s) to keep for speaker verification.
SV_WINDOW_FRAMES = 100

# How long to wait for the speaker to finish before giving up on the drain. A
# reply longer than this is not being played, it is stuck.
PLAYBACK_DRAIN_TIMEOUT_S = 60.0

# The system prompt for the Q&A path (`IntentName.ASK`). It is the one place
# that tells the answering model what the TTS can say — the pipeline sanitises
# the answer anyway, but a prompt that asks for short spoken Chinese gets a
# sentence through instead of 80 characters reduced to fragments.
ASK_SYSTEM_PROMPT = (
    "你是一个Windows语音助手的问答模块。"
    "用中文口语回答用户的问题：一两句话，直接给答案，不超过60个字。"
    "不要英文单词，不要markdown，不要列表，不要代码。"
    "不知道答案就直说不知道。"
)

# How long the microphone ignores the room after a confirmation question
# finishes playing: its echo tail would otherwise be transcribed as (and
# voiceprinted as) the beginning of the answer, mixing the TTS voice into the
# owner's embedding.
_CONFIRM_ECHO_GUARD_S = 0.4

# The spoken protocol for the confirmation loop. A question is asked out loud,
# so the answer must not need the wake word — the words to listen for are
# stated in the question itself.
_CONFIRM_YES_RE = re.compile(
    r"确认|确定|是的?|好[的呀啊吧]|可以|行[的吧]?|继续|执行|做吧|没问题|对了?|嗯|符合|没错"
    r"|\b(?:yes|ok|okay|confirm|sure)\b",
    re.IGNORECASE,
)
_CONFIRM_NO_RE = re.compile(
    r"取消|不用|不要|别弄|别做|算了|先不做|不行|停止|停下|住手|打住|没事了|否定|不对"
    r"|\b(?:no|cancel|stop|nevermind|never\s?mind)\b",
    re.IGNORECASE,
)


class PipelineState(Enum):
    IDLE = "idle"
    KWS_LISTENING = "kws_listening"
    VAD_ACTIVE = "vad_active"
    ASR_RUNNING = "asr_running"
    LLM_THINKING = "llm_thinking"
    # The agent plans and calls tools for seconds at a time. The state exists so
    # the tick loop keeps feeding KWS while it happens: a wake word during
    # planning has to be noticed, not queued behind the turn.
    AGENT_THINKING = "agent_thinking"
    TTS_PLAYING = "tts_playing"
    # A destructive call was announced out loud and is waiting for the user's
    # spoken 确认/取消. The microphone feeds VAD/ASR directly here — answering
    # a spoken question must not require saying the wake word again.
    CONFIRMING = "confirming"


@dataclass
class _PendingConfirmation:
    """
    A tool call that was announced and awaits the user's spoken 确认/取消.

    The call is replayed verbatim (`confirmed=True`) — re-parsing the request
    through the model would give it a second chance to change the arguments
    after the user agreed to something else. The deadline is absolute
    (`time.monotonic()`), so a hung flow expires instead of lingering until the
    next utterance trips over it.
    """

    call: ToolCall
    deadline: float
    #: When the question finished being spoken (monotonic) — the microphone
    #: ignores this much echo-decay before VAD is fed, so the tail of the
    #: question does not get transcribed as (and voiceprinted as) the answer.
    opened_at: float = 0.0
    #: One retries allowance when the voiceprint came back `rejected` — a
    #: transient misread of a short utterance, not an authenticated other
    #: speaker. A second rejection ends the flow.
    retries: int = 0


@dataclass
class PipelineContext:
    """State carried across the stages of one utterance."""

    trace_id: str
    state: PipelineState = PipelineState.IDLE
    speaker_id: str = "me"
    sv_result: Optional[SvResult] = None
    asr_text: str = ""
    intent: Optional[IntentResult] = None
    tool_calls: List[ToolCall] = field(default_factory=list)
    tts_text: str = ""
    # How many characters of `tts_text` may be spoken: 80 for a tool one-liner,
    # `tts.reply_max_chars` for an agent's answer. Set by whichever tier composed
    # the reply, because only it knows how much there is to say.
    speech_budget: int = MAX_SPEECH_CHARS


async def _maybe_await(value: Any) -> Any:
    """Await `value` if it is awaitable, so sync and async callbacks both work."""
    if inspect.isawaitable(value):
        return await value
    return value


class AudioPipeline:
    """Coordinates the audio engines for one voice-assistant session."""

    def __init__(
        self,
        on_state_change: Optional[Callable[[SystemState], Any]] = None,
        on_intent: Optional[Callable[[IntentResult], Any]] = None,
        on_tool_call: Optional[Callable[[ToolCall], Any]] = None,
        on_tts_chunk: Optional[Callable[[TtsChunk], Any]] = None,
        use_stub: bool = False,
        speech_player: Optional[Union[SpeechPlayer, NullSpeechPlayer]] = None,
    ):
        cfg = get_config()
        self.use_stub = use_stub

        self.on_state_change = on_state_change
        self.on_intent = on_intent
        self.on_tool_call = on_tool_call
        self.on_tts_chunk = on_tts_chunk

        # Engines
        self.kws: KwsEngine = create_kws_engine(use_stub)
        self.vad: VadEngine = create_vad_engine(use_stub)
        self.asr: AsrEngine = create_asr_engine(use_stub)
        self.sv: SvEngine = create_sv_engine(use_stub)
        self.tts: TtsEngine = create_tts_engine(use_stub)

        # Where the audio goes. A silent player by default so that constructing a
        # pipeline (every test does) never opens an audio device; `__main__`
        # attaches the real one.
        self.speech_player: Union[SpeechPlayer, NullSpeechPlayer] = (
            speech_player if speech_player is not None else NullSpeechPlayer()
        )

        # Segmentation and pause settings, read per utterance so that tuning the
        # pause table in config.yaml applies to the next reply without a restart.
        self.speech_text = speech_text_config(cfg)

        # Behaviour
        self.half_duplex = bool(cfg.get("audio.half_duplex", True))
        self.kws_during_tts = bool(cfg.get("audio.kws_during_tts", True))

        # Wiring (optional collaborators)
        self._intent_router: Any = None
        self._tool_executor: Any = None
        self._dsh_router: Any = None

        # Runtime
        self._state = PipelineState.IDLE
        self._running = False
        self._audio_queue: Deque[bytes] = deque(maxlen=2000)
        self._recent: Deque[bytes] = deque(maxlen=SV_WINDOW_FRAMES)
        self._context: Optional[PipelineContext] = None
        # The in-flight turn runs as a task so the tick loop keeps consuming
        # audio while the agent thinks or the speaker talks. `_turn_generation`
        # is how a barge-in says "that turn's output is no longer wanted".
        self._turn_task: Optional[asyncio.Task] = None
        self._turn_generation = 0
        # A destructive call announced out loud, waiting for the spoken
        # 确认/取消 (state CONFIRMING). None when no confirmation is open.
        self._pending_confirmation: Optional[_PendingConfirmation] = None
        # The free-form backend behind the Q&A path. Created on first use so a
        # deployment that never asks questions never builds an HTTP client;
        # replaceable via `set_ask_backend` for tests.
        self._ask_backend: Any = None

    # ── wiring ─────────────────────────────────────────────────

    async def initialize(self) -> None:
        """Initialize every engine (sequentially, for clear error messages)."""
        await self.kws.initialize()
        await self.vad.initialize()
        await self.asr.initialize()
        await self.sv.initialize()
        await self.tts.initialize()
        logger.info("pipeline_initialized", stub=self.use_stub)

    def set_intent_router(self, router) -> None:
        self._intent_router = router

    def set_tool_executor(self, executor) -> None:
        self._tool_executor = executor

    def set_dsh_router(self, router) -> None:
        """
        Attach the DeepSeek Harness agent for requests the rule tier cannot serve.

        Optional on purpose: with no router attached the pipeline behaves exactly
        as it did before DSH existed, which keeps `--stub-audio` and every
        existing integration test meaningful.
        """
        self._dsh_router = router

    def set_ask_backend(self, backend) -> None:
        """
        Attach the free-form LLM that answers `ASK` questions.

        Optional: without one, questions are answered deterministically when the
        rule tier knows how (small talk) and honestly refused otherwise.
        """
        self._ask_backend = backend

    def set_speech_player(self, player: Union[SpeechPlayer, NullSpeechPlayer]) -> None:
        """Attach the speaker (a silent one is used until this is called)."""
        self.speech_player = player

    @property
    def dsh_enabled(self) -> bool:
        return bool(self._dsh_router is not None and getattr(self._dsh_router, "enabled", False))

    # ── state ──────────────────────────────────────────────────

    @property
    def state(self) -> PipelineState:
        return self._state

    def _set_state(self, state: PipelineState) -> None:
        if state == self._state:
            return
        self._state = state
        logger.debug("pipeline_state", state=state.value)
        if self.on_state_change:
            try:
                self.on_state_change(
                    SystemState(state=SystemStateName(state.value), message=f"pipeline: {state.value}")
                )
            except Exception as e:  # callbacks must never kill the pipeline
                logger.error("state_callback_failed", error=str(e))

    # ── audio ingress ──────────────────────────────────────────

    def push_audio(self, pcm_bytes: bytes) -> None:
        """Enqueue one block of int16 PCM from the microphone callback."""
        self._audio_queue.append(pcm_bytes)

    # ── main loop ──────────────────────────────────────────────

    async def run(self) -> None:
        """Consume queued audio forever, driving the state machine."""
        self._running = True
        self._set_state(PipelineState.KWS_LISTENING)
        logger.info("pipeline_started")

        try:
            while self._running:
                if not self._audio_queue:
                    await asyncio.sleep(0.005)
                    continue

                chunk = self._audio_queue.popleft()
                self._recent.append(chunk)

                try:
                    await self._tick(chunk)
                except Exception as e:
                    # `exc_info` matters here: several bugs in this project
                    # (a silent assistant after a wake word, tool results
                    # vanishing) were hidden by a traceback-free one-liner. See
                    # `UNIMPLEMENTED.md` §1.2.
                    logger.error(
                        "pipeline_tick_failed",
                        error=str(e),
                        error_type=type(e).__name__,
                        state=self._state.value,
                        exc_info=e,
                    )
                    await self._abort_utterance()
        finally:
            self._running = False
            logger.info("pipeline_stopped")

    async def _tick(self, chunk: bytes) -> None:
        """Route one audio block according to the current state."""
        if self._state in (PipelineState.IDLE, PipelineState.KWS_LISTENING):
            await self._tick_kws(chunk)

        elif self._state == PipelineState.VAD_ACTIVE:
            await self._tick_vad(chunk)

        elif self._state == PipelineState.CONFIRMING:
            await self._tick_confirming(chunk)

        elif self._state in (PipelineState.TTS_PLAYING, PipelineState.AGENT_THINKING):
            # Half-duplex: ASR/VAD pause, but KWS keeps listening so the wake word
            # can interrupt. This is only reachable because the turn runs as a
            # task — while it was awaited inline, these blocks were never
            # consumed and barge-in silently did not work.
            if self.half_duplex and self.kws_during_tts:
                await self._tick_kws(chunk, barge_in=True)

        # ASR_RUNNING / LLM_THINKING fall through: those stages are
        # driven by awaited calls, not by inbound audio.

    # ── KWS ────────────────────────────────────────────────────

    async def _tick_kws(self, chunk: bytes, barge_in: bool = False) -> None:
        self._set_state(PipelineState.KWS_LISTENING)
        self.kws.accept_waveform(chunk)

        result = self.kws.get_result()
        if not result:
            return

        logger.info("kws_triggered", keyword=result.keyword, barge_in=barge_in)
        self.kws.reset()

        if barge_in:
            self._barge_in(result.keyword)

        await self._begin_utterance(result)

    def _barge_in(self, keyword: str) -> None:
        """
        Stop the assistant mid-sentence, and disown the rest of that turn.

        Both halves are needed: `interrupt()` on the player stops the audio
        immediately (the device buffer is aborted, not played out), and bumping
        the turn generation stops the *producer* — the agent's answer would
        otherwise be spoken as soon as it arrived, over the user's new command.

        The agent turn itself is deliberately not cancelled: the harness has
        already been handed the prompt, and cancelling it leaves it in an unknown
        state for a problem that discarding the output already solves.
        """
        self.tts.interrupt()
        self.speech_player.interrupt()
        self._turn_generation += 1
        logger.info("tts_barge_in", keyword=keyword, superseded_turn=self._turn_generation)

    # ── utterance lifecycle ────────────────────────────────────

    async def _begin_utterance(self, trigger: KwsResult) -> None:
        """Start a new utterance: fresh trace, speaker check, then VAD."""
        trace_id = uuid.uuid4().hex[:16]
        set_trace_context(trace_id)
        self._context = PipelineContext(trace_id=trace_id)
        self.vad.reset()

        # Speaker verification over the last ~1 s of audio.
        # The engine returns None when nobody is enrolled yet, in which case
        # the utterance proceeds without a speaker tier.
        if getattr(self.sv, "enabled", True):
            sv_result = self.sv.verify(list(self._recent))
            if sv_result:
                self._context.sv_result = sv_result
                if sv_result.tier == "rejected":
                    logger.info("speaker_rejected", score=round(sv_result.score, 4))
                    await self._abort_utterance()
                    return

        self._set_state(PipelineState.VAD_ACTIVE)

    async def _abort_utterance(self) -> None:
        """Drop the current utterance and go back to listening."""
        if self._pending_confirmation is not None:
            # An abort ends the confirmation flow as well: whatever the reason
            # (silence, a failed turn, shutdown), a question nobody can answer
            # must not survive to ambush the next utterance.
            logger.info(
                "confirmation_dropped",
                tool=self._pending_confirmation.call.tool.value,
            )
            self._pending_confirmation = None
        self._context = None
        clear_trace_context()
        self.vad.reset()
        self._set_state(PipelineState.KWS_LISTENING)

    # ── VAD -> ASR ─────────────────────────────────────────────

    async def _tick_confirming(self, chunk: bytes) -> None:
        """
        Wait for the answer to a spoken confirmation question.

        The microphone feeds VAD/ASR directly — no wake word — because the user
        is replying to a question the assistant just asked. The deadline is
        checked per block: the question must not stay armed long enough to fire
        on an unrelated utterance minutes later.
        """
        pending = self._pending_confirmation
        if pending is None:
            await self._abort_utterance()
            return
        if time.monotonic() >= pending.deadline:
            logger.info(
                "confirmation_expired",
                tool=pending.call.tool.value,
                trace_id=pending.call.trace_id,
            )
            self._pending_confirmation = None
            await self._abort_utterance()
            return
        if time.monotonic() < pending.opened_at + _CONFIRM_ECHO_GUARD_S:
            # The question just finished playing; whatever the mic hears right
            # now is its echo, not an answer. Feed VAD only after the decay.
            return
        await self._tick_vad(chunk)

    async def _tick_vad(self, chunk: bytes) -> None:
        segments = self.vad.accept_waveform(chunk)
        for segment in segments:
            await self._on_segment(segment)

    async def _on_segment(self, segment: VadSegment) -> None:
        if self._context is None:
            return

        logger.info("vad_segment", duration_ms=segment.duration_ms)
        self._set_state(PipelineState.ASR_RUNNING)

        result = await self.asr.transcribe(segment.samples)
        if not result.text.strip():
            logger.info("asr_empty")
            if self._pending_confirmation is not None:
                # A cough or a breath while waiting for 确认/取消: keep the
                # question armed and keep waiting until its deadline.
                self.vad.reset()
                self._set_state(PipelineState.CONFIRMING)
                return
            await self._abort_utterance()
            return

        if self._pending_confirmation is not None:
            await self._resolve_confirmation(result, segment)
            return

        self._context.asr_text = result.text
        self._start_turn(result)

    # ── intent -> tools -> TTS ─────────────────────────────────

    def _start_turn(self, asr: AsrResult) -> None:
        """
        Run this utterance's turn in the background.

        The turn has to be a task, not an awaited call: it lasts seconds (agent
        planning, tool calls, synthesis) and the tick loop that feeds KWS is the
        coroutine that would otherwise be waiting for it. That is why barge-in
        did not work during playback, and why `UNIMPLEMENTED.md` §2.0 existed.

        `_route_intent` is kept as the awaited, whole-turn form for callers that
        want the result inline (tests, and anything driving the pipeline stage by
        stage).
        """
        self._turn_generation += 1
        generation = self._turn_generation
        task = asyncio.create_task(self._run_turn(asr, generation=generation))
        # A background turn that raises must be seen: an unretrieved task
        # exception is a warning printed at some later point, with no trace of
        # which utterance it belonged to.
        task.add_done_callback(self._turn_finished)
        self._turn_task = task

    def _turn_finished(self, task: asyncio.Task) -> None:
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.error(
                "turn_failed",
                error=str(error),
                error_type=type(error).__name__,
                exc_info=error,
            )

    async def _route_intent(self, asr: AsrResult) -> None:
        """Run the whole turn inline (no background task, no generation check)."""
        await self._run_turn(asr, generation=None)

    async def _run_turn(self, asr: AsrResult, generation: Optional[int] = None) -> None:
        context = self._context
        if context is None:
            return

        self._set_state(PipelineState.LLM_THINKING)

        intent: Optional[IntentResult] = None
        if self._intent_router is not None:
            intent = await self._intent_router.route(asr.text, context.sv_result)
            context.intent = intent
            if self.on_intent:
                await _maybe_await(self.on_intent(intent))

        # Two tiers, one of which handles every request: the rule tier answers
        # the deterministic high-frequency commands itself, and the agent handles
        # everything the rules declined. When the agent is configured, it *is* the
        # model layer — `routes_to_agent` is what stops the legacy classifier from
        # also running and producing a second, competing answer.
        #
        # Two intents are intercepted before that check, because neither may
        # reach the agent: `ASK` must stay text-in/text-out (the answering model
        # must never get tool capability — the agent's tools are exactly how
        # 「什么是量子力学」 used to end up as a browser window), and `DISMISS`
        # means "nothing, at ease".
        if intent is not None and intent.intent == IntentName.DISMISS:
            context.tts_text = "好的。"
        elif intent is not None and intent.intent == IntentName.ASK:
            await self._run_ask(intent, context)
        elif self._routes_to_agent(intent):
            await self._run_agent(asr.text, context)
        else:
            await self._run_tools(intent)

        if self._superseded(generation):
            # A wake word arrived while this turn was being composed. Speaking the
            # answer now would talk over the user's new command, so the reply is
            # dropped — the turn itself has already done its work.
            logger.info(
                "turn_superseded",
                generation=generation,
                current=self._turn_generation,
                chars=len(context.tts_text or ""),
            )
            return

        self._set_state(PipelineState.TTS_PLAYING)
        await self._speak(context)

        if self._superseded(generation):
            return
        if self._pending_confirmation is not None:
            # The confirmation question was just spoken. Wait for 确认/取消
            # directly — the microphone feeds ASR without a wake word here.
            self.vad.reset()
            self._set_state(PipelineState.CONFIRMING)
            return
        await self._abort_utterance()

    def _superseded(self, generation: Optional[int]) -> bool:
        """True when a barge-in has disowned the turn that is running."""
        return generation is not None and generation != self._turn_generation

    @staticmethod
    def _speaker_tier(context: PipelineContext) -> str:
        """
        The speaker tier as the plain string the tool layer validates against.

        `PipelineContext.sv_result` is the audio engine's own result type, whose
        `tier` has been both a bare string and a `SpeakerTier` member at
        different points in this project's life, so both shapes are accepted
        rather than assumed.
        """
        result = context.sv_result
        if result is None:
            return "full"
        tier = getattr(result, "tier", "full")
        return str(getattr(tier, "value", tier) or "full")

    def _routes_to_agent(self, intent: Optional[IntentResult]) -> bool:
        """
        True when this utterance should go to the agent instead of the tools.

        A rule match is never overridden: `match_rules` returning a result means
        the request is one of the ten things the assistant answers
        deterministically, and 「音量调大 20」 must not acquire a model's latency.
        Everything else — an `unknown` intent from a rule miss — is the agent's.
        """
        if not self.dsh_enabled:
            return False
        return intent is None or intent.intent == IntentName.UNKNOWN

    async def _run_agent(self, text: str, context: PipelineContext) -> None:
        """Let the agent answer, and speak what it says it did."""
        tier = self._speaker_tier(context)
        # Planning takes seconds, so say so: the tick loop keeps feeding KWS in
        # this state, which is what makes the wake word interruptible here.
        self._set_state(PipelineState.AGENT_THINKING)

        try:
            outcome = await self._dsh_router.route(
                text, trace_id=context.trace_id, tier=tier
            )
        except Exception as e:
            # The pipeline must survive one failed agent task (new_way.md §15).
            logger.error("dsh_route_failed", error=str(e), error_type=type(e).__name__)
            context.tts_text = "抱歉，处理这个请求的时候出错了。"
            return

        if outcome.resolved:
            # An agent's answer is longer than a tool's one-liner by nature, and
            # the old 80-character budget was a symptom of whole-utterance
            # synthesis. The engine speaks this as separate sentences with real
            # pauses, so the budget can be the one config asks for.
            budget = self.speech_text.reply_max_chars
            spoken = sanitize_for_tts(outcome.response, budget)
            if spoken:
                context.tts_text = spoken
                context.speech_budget = budget
                return
            # The agent answered, but nothing in the answer could be spoken: it
            # was all English, or all formatting. Saying 「好了」 here would be
            # claiming a result nobody can verify.
            logger.warning(
                "dsh_response_unspeakable",
                source=outcome.source,
                response_len=len(outcome.response or ""),
            )
            context.tts_text = "我完成了操作，但是结果说不清楚，请看屏幕。"
            return

        logger.info(
            "dsh_unresolved",
            source=outcome.source,
            reason=outcome.failure,
            escalation_reason=outcome.escalation_reason,
        )
        context.tts_text = self._agent_failure_reply(outcome)

    @staticmethod
    def _agent_failure_reply(outcome) -> str:
        """
        What to say when the agent could not finish.

        Deliberately not 「好的，已为您完成。」 — the whole point of the
        verification layer is that a failure is reported as one (new_way.md §15:
        「"I couldn't open VS Code because it wasn't found" — not "Done."」).

        Only a **failed** step's sentence is eligible. A multi-step turn can have
        succeeded at step one and failed at step two, and speaking the first
        step's 「已经打开记事本了。」 would announce success for the request that
        did not complete — the exact lie this layer exists to prevent.
        """
        assessment = getattr(outcome, "assessment", None)
        if assessment is not None:
            for activity in assessment.tool_activity:
                if activity.speak and (activity.verification_failed or activity.success is False):
                    return clip_for_speech(activity.speak)

        if outcome.failure in ("dsh_disabled",):
            return "抱歉，这个请求我还没有实现。"
        if outcome.failure and str(outcome.failure).startswith("dsh_"):
            return "我现在联系不上负责执行的程序，请稍后再试。"
        return "抱歉，这个操作没有成功。"

    # ── question / small talk (IntentName.ASK) ─────────────────

    def _ask_llm(self):
        """The free-form backend for questions, built on first use."""
        if self._ask_backend is None:
            from winvoice.llm.local import LocalLlmBackend

            self._ask_backend = LocalLlmBackend()
        return self._ask_backend

    async def _run_ask(self, intent: IntentResult, context: PipelineContext) -> None:
        """
        Answer a question or a greeting — text in, text out, no tools.

        This is the path 「什么是量子力学」 takes so that it can never again be
        misrouted into `get_weather` or a browser window: the answering model is
        called directly, without the agent, the tool registry, or the intent
        grammar (`LocalLlmBackend.generate`, not `complete`). When the model is
        unreachable the question is refused *out loud* — a silent assistant
        looks broken, and falling through to the agent here would hand the
        question tool capability after all.
        """
        # Small talk the rule tier already classified needs no model, so it
        # answers even with no LLM server configured at all.
        kind = str(intent.args.get("kind", "")).strip()
        if kind == "greet":
            context.tts_text = "你好！有什么可以帮你的吗？"
            return
        if kind == "thanks":
            context.tts_text = "不客气。"
            return
        if kind == "bye":
            context.tts_text = "好的，回见。"
            return

        cfg = get_config()
        if not bool(cfg.get("llm.ask.enabled", True)):
            context.tts_text = "抱歉，问答功能没有打开。"
            return

        question = str(
            intent.args.get("question") or context.asr_text or intent.raw_text or ""
        ).strip()
        if not question:
            context.tts_text = "抱歉，我没有听清你的问题。"
            return

        # Generation takes seconds on CPU; sit in the same state the agent uses
        # so the tick loop keeps feeding KWS and a wake word can interrupt.
        self._set_state(PipelineState.AGENT_THINKING)

        budget = max(1, int(cfg.get("llm.ask.max_chars", MAX_SPEECH_CHARS)))
        try:
            answer = await self._ask_llm().generate(
                question,
                system=ASK_SYSTEM_PROMPT,
                max_tokens=max(64, int(cfg.get("llm.ask.max_tokens", 128))),
                temperature=0.6,
                timeout_s=float(cfg.get("llm.ask.timeout_s", 20.0)),
            )
        except Exception as e:
            logger.warning(
                "ask_llm_failed", error=str(e), error_type=type(e).__name__
            )
            context.tts_text = "抱歉，这个问题我现在答不上来。"
            return

        # The answer must survive the Chinese-only lexicon: Latin words and
        # markdown scaffolding are dropped, and an answer that reduces to
        # nothing gets the honest refusal instead of silence.
        spoken = sanitize_for_tts(answer, budget)
        if not spoken:
            logger.warning("ask_answer_unspeakable", chars=len(answer or ""))
            context.tts_text = "抱歉，这个问题我现在答不上来。"
            return
        logger.info("ask_answered", question_chars=len(question), spoken_chars=len(spoken))
        context.tts_text = spoken
        context.speech_budget = budget

    async def _run_tools(self, intent: Optional[IntentResult]) -> None:
        context = self._context
        if context is None:
            return

        if intent is None:
            context.tts_text = self._default_reply(None)
            return

        calls = self._intent_to_tool_calls(intent)
        context.tool_calls = calls

        results: List[ToolResult] = []
        for call in calls:
            # 「搜索 X」 opens a browser the moment it executes, so it asks
            # first (config `tools.search_web_confirm`). Intercepted here, not
            # in the registry, so the agent's own `search_web` over MCP is
            # unaffected: the confirmation loop is a voice interaction.
            if self._search_confirmation_applies(call):
                self._arm_confirmation(call)
                context.tts_text = self._confirmation_question(call)
                return

            if self.on_tool_call is not None:
                out = await _maybe_await(self.on_tool_call(call))
                if isinstance(out, ToolResult):
                    results.append(out)
            elif self._tool_executor is not None:
                results.append(await self._tool_executor.execute(call))

            # A destructive tool refused to run until it is confirmed. The
            # question is spoken now and the same call is replayed with
            # `confirmed=True` once the user agrees — the state machine waits
            # for that in CONFIRMING.
            if results and self._needs_confirmation(results[-1]):
                self._arm_confirmation(call)
                context.tts_text = self._confirmation_question(call)
                return

        context.tts_text = self._default_reply(intent, results)

    def _intent_to_tool_calls(self, intent: IntentResult) -> List[ToolCall]:
        """Map an intent to tool invocations (tool choice lives in code, not the LLM)."""
        from winvoice.contracts import ToolName

        mapping = {
            "open_app": ToolName.OPEN_APP,
            "close_app": ToolName.CLOSE_APP,
            "set_volume": ToolName.SET_VOLUME,
            "media_control": ToolName.MEDIA_CONTROL,
            "search_web": ToolName.SEARCH_WEB,
            "read_file": ToolName.READ_FILE,
            "list_dir": ToolName.LIST_DIR,
            "write_file": ToolName.WRITE_FILE,
            "run_script": ToolName.RUN_SCRIPT,
            "system_power": ToolName.SYSTEM_POWER,
            "get_time": ToolName.GET_TIME,
            "get_weather": ToolName.GET_WEATHER,
        }
        tool = mapping.get(intent.intent.value)
        if tool is None:
            return []

        destructive = tool in (ToolName.WRITE_FILE, ToolName.RUN_SCRIPT)
        return [
            ToolCall(
                trace_id=self._context.trace_id if self._context else "",
                tool=tool,
                args=intent.args,
                # The registry spec is the authority on confirmation; this flag
                # only keeps the call honest for callers that read it.
                requires_confirmation=destructive or tool is ToolName.SYSTEM_POWER,
                # The tier has to be on the call, not only in the agent path:
                # `ToolExecutor.execute` validates against it, and a rule-matched
                # command from a guest is exactly the case that used to be
                # checked as `full` (`UNIMPLEMENTED.md` §1.1).
                tier=self._context_tier(),
            )
        ]

    def _context_tier(self) -> SpeakerTier:
        """The current utterance's speaker tier, as the enum the contracts use."""
        context = self._context
        if context is None or context.sv_result is None:
            return SpeakerTier.FULL
        try:
            return SpeakerTier(getattr(context.sv_result, "tier", "full"))
        except ValueError:
            # An unrecognised tier must not become `full` by accident; the tool
            # layer would then validate a stranger's call as the owner's.
            logger.warning(
                "unrecognised_speaker_tier",
                tier=str(getattr(context.sv_result, "tier", None)),
            )
            return SpeakerTier.REJECTED

    # ── confirmation loop ──────────────────────────────────────

    @staticmethod
    def _needs_confirmation(result: ToolResult) -> bool:
        """True when the executor refused to run until the user confirms."""
        return bool(result.error) and str(result.error).startswith("CONFIRMATION_REQUIRED")

    def _search_confirmation_applies(self, call: ToolCall) -> bool:
        """`tools.search_web_confirm` gates the ask-first behaviour."""
        if call.tool != ToolName.SEARCH_WEB:
            return False
        return bool(get_config().get("tools.search_web_confirm", True))

    def _arm_confirmation(self, call: ToolCall) -> None:
        """
        Park the call until the user answers the question being spoken.

        The deadline comes from config at arming time, so tuning the window
        needs no restart. The call itself carries the tier and trace of the
        utterance that made the request — that binding is what stops a second
        speaker (or a later utterance on a stale flow) from confirming it.
        """
        timeout_s = float(get_config().get("tools.confirm_timeout_s", 15.0))
        self._pending_confirmation = _PendingConfirmation(
            call=call,
            deadline=time.monotonic() + timeout_s,
            opened_at=time.monotonic(),
        )
        logger.info(
            "confirmation_armed",
            tool=call.tool.value,
            trace_id=call.trace_id,
            tier=str(getattr(call.tier, "value", call.tier)),
            timeout_s=timeout_s,
        )

    @staticmethod
    def _confirmation_question(call: ToolCall) -> str:
        """
        What is said instead of executing the call.

        Paths are deliberately never spoken (Latin, long), and the protocol —
        确认 or 取消 — is spelled out in the question, because the answer has
        to be recognisable from speech alone.
        """
        # 「确认执行」 is deliberately longer than a bare 确认: the
        # voiceprint grades the answer, and a two-syllable utterance embeds
        # far worse than a four-syllable one (live bug 2026-09-27).
        tail = "确认请说确认执行，取消请说取消。"
        if call.tool == ToolName.WRITE_FILE:
            return f"我将要写入一个文件，{tail}"
        if call.tool == ToolName.RUN_SCRIPT:
            return f"我将要运行一个脚本，{tail}"
        if call.tool == ToolName.SYSTEM_POWER:
            # The action's Chinese name comes from the same table the handler's
            # own messages use, so the question and the outcome cannot drift.
            from winvoice.tools.builtin import POWER_ACTION_SPEECH

            action = str(call.args.get("action", "")).strip().lower()
            spoken = POWER_ACTION_SPEECH.get(action, "执行电源操作")
            return f"我将要{spoken}，{tail}"
        if call.tool == ToolName.SEARCH_WEB:
            query = str(call.args.get("query", "")).strip()
            if query and not has_latin(query):
                return f"你要我搜索{query}吗？{tail}"
            return f"你要我打开浏览器搜索吗？{tail}"
        return f"这个操作需要你的确认，{tail}"

    def _current_speaker_tier(self, samples=None) -> Optional[str]:
        """
        The tier of whoever is speaking *right now*, as a plain string.

        Confirmation answers arrive without a wake word, so this runs its own
        verification rather than reusing the requesting utterance's result —
        that reuse is exactly the hole a second voice would walk through.

        `samples` is the VAD segment's **speech-only** audio. Verifying the
        rolling microphone window instead was the live bug (2026-09-27): by
        the time a short 「确认」 ends its segment, that window holds mostly
        the trailing silence the VAD required plus a possible speaker echo,
        the embedding degrades, and the owner dropped into the guest band —
        every confirmation refused. Verify what was said, not what the room
        last heard. None means the engine could not judge the audio at all —
        the caller treats that as a retryable misread, never as a pass. The
        verification is a gate, not a lesson: it never EMA-drifts the profile
        (that drift poisoned `me.json` until the owner scored 0.32 against
        their own voice — live bug 2026-09-27).
        """
        frames = samples if samples is not None else list(self._recent)
        if getattr(self.sv, "enabled", True):
            sv_result = self.sv.verify(frames, adaptive=False)
            if sv_result is not None:
                tier = getattr(sv_result, "tier", "full")
                return str(getattr(tier, "value", tier))
            return None  # unjudgeable audio — the caller retries, fail-closed
        return "full"

    async def _resolve_confirmation(self, asr: AsrResult, segment: VadSegment) -> None:
        """
        Act on the answer to a confirmation question.

        Denial wins over confirmation (「不要确认」 must not confirm), the
        confirmer must be the same speaker who made the request, and anything
        that is neither yes nor no cancels the pending call and is handled as a
        brand-new utterance — changing one's mind mid-question must not leave
        the loop stuck.
        """
        pending = self._pending_confirmation
        if pending is None:
            return
        call = pending.call

        if time.monotonic() >= pending.deadline:
            self._pending_confirmation = None
            logger.info(
                "confirmation_expired", tool=call.tool.value, trace_id=call.trace_id
            )
            await self._abort_utterance()
            return

        # Verify the spoken answer itself (speech-only VAD audio) — see
        # `_current_speaker_tier` for why the rolling window downgraded the
        # owner's short 确认 to guest.
        tier = self._current_speaker_tier(segment.samples)
        pending_tier = str(getattr(call.tier, "value", call.tier))

        if tier in (None, "rejected") and pending.retries < 1:
            # Not judgeable, or below every band: read as a misread of a short
            # utterance rather than an impostor — ask once more with the
            # request still armed. A genuine guest just fails again and the
            # flow dies with the TTL; nobody is promoted by a retry.
            pending.retries += 1
            logger.info(
                "confirmation_retry", tool=call.tool.value, tier=tier,
                speaker=pending_tier,
            )
            await self._speak_line("没有听清，请再说一遍确认。", back_to_confirming=True)
            return

        if tier != pending_tier:
            self._pending_confirmation = None
            logger.info(
                "confirmation_speaker_mismatch",
                tool=call.tool.value,
                pending_tier=pending_tier,
                speaker_tier=tier,
            )
            await self._speak_line("这个操作需要刚才说话的人来确认，先不做了。")
            return

        text = asr.text.strip()
        if _CONFIRM_NO_RE.search(text):
            self._pending_confirmation = None
            logger.info("confirmation_cancelled", tool=call.tool.value, text=text)
            await self._speak_line("好的，先不做了。")
            return

        if _CONFIRM_YES_RE.search(text):
            self._pending_confirmation = None
            logger.info("confirmation_granted", tool=call.tool.value, text=text)
            result = await self._execute_confirmed(call)
            await self._speak_line(self._results_reply([result]))
            return

        # Neither yes nor no: the user said something else. The pending call
        # dies here (「下一句非确认话即作废」), and this utterance is treated
        # exactly as if the wake word had preceded it.
        self._pending_confirmation = None
        logger.info(
            "confirmation_superseded", tool=call.tool.value, text=text
        )
        await self._reroute_utterance(asr, segment)

    async def _execute_confirmed(self, call: ToolCall) -> ToolResult:
        """Replay the parked call with `confirmed=True` — same call, same args."""
        executor = self._tool_executor
        if executor is not None:
            try:
                result = await executor.execute(call, confirmed=True)
            except TypeError as e:
                # Retrying *without* the flag would only re-ask for
                # confirmation; an executor that cannot take it cannot run the
                # call at all.
                logger.error("confirmed_execute_unsupported", error=str(e))
                return ToolResult(
                    tool=call.tool,
                    success=False,
                    error=str(e),
                    message="这个操作没法执行。",
                )
            clear = getattr(executor, "clear_pending_confirmation", None)
            if clear is not None:
                clear()
            return result

        if self.on_tool_call is not None:
            out = await _maybe_await(self.on_tool_call(call))
            if isinstance(out, ToolResult):
                return out

        logger.error("confirmed_execute_unwired", tool=call.tool.value)
        return ToolResult(
            tool=call.tool,
            success=False,
            error="no tool executor is wired",
            message="这个操作没法执行。",
        )

    async def _speak_line(self, text: str, *, back_to_confirming: bool = False) -> None:
        """
        Speak one short line and go back to listening (no wake word needed).

        With `back_to_confirming` the pending confirmation survives the line
        (「没有听清，请再说一遍确认。」) and the microphone returns to the
        confirming state instead of dropping the request.
        """
        trace_id = getattr(self._context, "trace_id", "") or uuid.uuid4().hex[:16]
        context = PipelineContext(trace_id=trace_id, tts_text=text)
        self._set_state(PipelineState.TTS_PLAYING)
        await self._speak(context)
        if back_to_confirming and self._pending_confirmation is not None:
            self.vad.reset()
            self._set_state(PipelineState.CONFIRMING)
            return
        await self._abort_utterance()

    async def _reroute_utterance(self, asr: AsrResult, segment: VadSegment) -> None:
        """
        Handle an utterance that killed a pending confirmation as a fresh one.

        Fresh trace, fresh speaker check over the spoken audio (same reasoning
        as the confirmation tier check): the previous context belonged to the
        confirmed request, and a topic change is a new turn in every respect.
        A rejected speaker ends the flow instead of getting their words routed.
        """
        sv_result = None
        if getattr(self.sv, "enabled", True):
            sv_result = self.sv.verify(segment.samples, adaptive=False)
            if sv_result is not None:
                tier = getattr(sv_result, "tier", "full")
                tier = str(getattr(tier, "value", tier))
                if tier == "rejected":
                    logger.info("speaker_rejected", score=round(sv_result.score, 4))
                    await self._abort_utterance()
                    return

        trace_id = uuid.uuid4().hex[:16]
        set_trace_context(trace_id)
        context = PipelineContext(trace_id=trace_id, sv_result=sv_result)
        self._context = context
        self.vad.reset()
        context.asr_text = asr.text
        self._start_turn(asr)

    # ── speech ─────────────────────────────────────────────────

    @staticmethod
    def _speakable(result: ToolResult) -> str:
        """
        The part of a failed tool result that the TTS model can pronounce.

        `ToolResult.message` is authored for speech and is used as-is. `error`
        is for logs and callers, so it may contain English words, argument keys
        and bracketed path lists — none of which the Chinese VITS lexicon
        contains. sherpa-onnx drops such tokens one by one ("OOV ... Ignore
        it!"), so pasting an error straight into TTS produced a sentence with
        holes in it. Anything unpronounceable is stripped here, and if nothing
        readable survives the caller falls back to a generic sentence.
        """
        if result.message:
            return result.message

        text = result.error or ""
        logger.warning("unspeakable_tool_error", tool=result.tool.value, error=text)
        # Latin words, bracketed reprs and stray ASCII punctuation.
        cleaned = re.sub(r"[A-Za-z]+", "", text)
        cleaned = re.sub(r"[\[\]{}()<>\"'`|\\^~*_=+/]+", " ", cleaned)
        cleaned = re.sub(r"\s+", " ", cleaned).strip(" \t:：,，.。-—")
        return cleaned

    @staticmethod
    def _spoken_success(results: List[ToolResult]) -> Optional[str]:
        """
        What to say when the tools succeeded, or None to fall back.

        A tool that *did* something still has something to report — the time,
        the temperature, the fact that the volume moved — and it reports it in
        `ToolResult.message`, exactly like a failure does. Requiring an
        explicit message keeps the old 「好的，已为您完成。」 for tools that have
        nothing to add, instead of turning a confirmation into a guess.

        A message with Latin letters in it is refused rather than spoken: it
        was written for the speaker, so English there is a bug in the tool
        (see `tools.builtin`), and the safe answer is the generic sentence.
        """
        spoken: List[str] = []
        for result in results:
            text = (result.message or "").strip()
            if not text:
                continue
            if has_latin(text):
                logger.warning(
                    "unspeakable_tool_message", tool=result.tool.value, message=text
                )
                continue
            spoken.append(text)

        if not spoken:
            return None
        if len(spoken) == 1:
            return clip_for_speech(spoken[0])

        # One utterance maps to one tool call today, but clipping the *joined*
        # text would let a long first message drop every later outcome. Give
        # each result its own share of the budget instead.
        share = max(1, MAX_SPEECH_CHARS // len(spoken))
        return clip_for_speech("；".join(clip_for_speech(text, share) for text in spoken))

    def _default_reply(self, intent: Optional[IntentResult], results: Optional[List[ToolResult]] = None) -> str:
        if intent is None:
            return "抱歉，我没有理解您的请求。"

        results = results or []
        if not results:
            # Nothing ran: either this intent has no tool mapping yet
            # (unknown), or the tool callback returned nothing. "好的。" here
            # would be a false success for a request the assistant never
            # handled.
            return "抱歉，这个请求我还没有实现。"

        return self._results_reply(results)

    def _results_reply(self, results: List[ToolResult]) -> str:
        """What to say about tool results — shared by turns and the confirm loop."""
        if all(r.success for r in results):
            return self._spoken_success(results) or "好的，已为您完成。"

        reasons = [self._speakable(r) for r in results]
        reasons = [r for r in reasons if r]
        if not reasons:
            return "抱歉，这个操作没有成功。"
        return clip_for_speech(f"执行遇到问题：{'；'.join(reasons)}")

    # ── TTS ────────────────────────────────────────────────────

    async def _speak(self, context: PipelineContext) -> None:
        """
        Speak the composed reply: produce chunks, hand them to the player, drain.

        The engine splits the reply into sentences and synthesises them one at a
        time, so this loop is a producer while the player consumes — the first
        sentence is heard while the rest is still being generated. Each chunk
        carries the pause that follows it; the player writes that silence, which
        is the only reason the pauses the listener hears are the ones the pause
        table asked for (the model contributes none of its own).
        """
        text = (context.tts_text or "").strip()
        if not text:
            return

        player = self.speech_player
        await player.start()

        request = TtsRequest(
            trace_id=context.trace_id,
            text=text,
            voice="guest" if self._is_guest(context) else "default",
            max_chars=context.speech_budget,
        )
        async for chunk in self.tts.synthesize(request):
            if self.on_tts_chunk:
                await _maybe_await(self.on_tts_chunk(chunk))
            if chunk.data and not self.tts.is_interrupted():
                await player.enqueue(chunk.data, chunk.sample_rate, chunk.pause_after_ms)
            if self.tts.is_interrupted():
                break

        await player.wait_drained(timeout=PLAYBACK_DRAIN_TIMEOUT_S)

    @staticmethod
    def _is_guest(context: Optional[PipelineContext]) -> bool:
        if context is None or context.sv_result is None:
            return False
        return str(getattr(context.sv_result, "tier", "")) == "guest"

    async def _play_tts(self) -> None:
        """Speak the current context's reply (kept for direct callers/tests)."""
        context = self._context
        if context is None:
            return
        await self._speak(context)
        await self._abort_utterance()

    # ── shutdown ───────────────────────────────────────────────

    def stop(self) -> None:
        self._running = False
        self.tts.interrupt()
        self.speech_player.interrupt()

    async def shutdown(self) -> None:
        """Release the worker thread, the output stream and the turn task."""
        self.stop()
        task, self._turn_task = self._turn_task, None
        if task is not None and not task.done():
            task.cancel()
        close_tts = getattr(self.tts, "close", None)
        if close_tts is not None:
            await _maybe_await(close_tts())
        await self.speech_player.close()
