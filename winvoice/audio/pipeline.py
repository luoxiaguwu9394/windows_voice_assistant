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
from typing import Any, Callable, Deque, List, Optional

import numpy as np

from winvoice.config import get_config
from winvoice.contracts import (
    IntentResult,
    SystemState,
    SystemStateName,
    ToolCall,
    ToolResult,
    TtsRequest,
)
from winvoice.logging import clear_trace_context, get_logger, set_trace_context
from .asr import AsrEngine, AsrResult, create_asr_engine
from .kws import KwsEngine, KwsResult, create_kws_engine
from .sv import SvEngine, SvResult, create_sv_engine
from .tts import TtsChunk, TtsEngine, create_tts_engine
from .vad import VadEngine, VadSegment, create_vad_engine

logger = get_logger(__name__)

# How many recent 10 ms frames (~1 s) to keep for speaker verification.
SV_WINDOW_FRAMES = 100


class PipelineState(Enum):
    IDLE = "idle"
    KWS_LISTENING = "kws_listening"
    VAD_ACTIVE = "vad_active"
    ASR_RUNNING = "asr_running"
    LLM_THINKING = "llm_thinking"
    TTS_PLAYING = "tts_playing"


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

        # Behaviour
        self.half_duplex = bool(cfg.get("audio.half_duplex", True))
        self.kws_during_tts = bool(cfg.get("audio.kws_during_tts", True))

        # Wiring (optional collaborators)
        self._intent_router = None
        self._tool_executor = None

        # Runtime
        self._state = PipelineState.IDLE
        self._running = False
        self._audio_queue: Deque[bytes] = deque(maxlen=2000)
        self._recent: Deque[bytes] = deque(maxlen=SV_WINDOW_FRAMES)
        self._context: Optional[PipelineContext] = None

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
                    logger.error("pipeline_tick_failed", error=str(e), state=self._state.value)
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

        elif self._state == PipelineState.TTS_PLAYING:
            # Half-duplex: ASR/VAD pause, but KWS keeps listening for barge-in.
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
            self.tts.interrupt()
            logger.info("tts_barge_in", keyword=result.keyword)

        await self._begin_utterance(result)

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
        self._context = None
        clear_trace_context()
        self.vad.reset()
        self._set_state(PipelineState.KWS_LISTENING)

    # ── VAD -> ASR ─────────────────────────────────────────────

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
            await self._abort_utterance()
            return

        self._context.asr_text = result.text
        await self._route_intent(result)

    # ── intent -> tools -> TTS ─────────────────────────────────

    async def _route_intent(self, asr: AsrResult) -> None:
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

        await self._run_tools(intent)
        self._set_state(PipelineState.TTS_PLAYING)
        await self._play_tts()

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
            if self.on_tool_call is not None:
                out = await _maybe_await(self.on_tool_call(call))
                if isinstance(out, ToolResult):
                    results.append(out)
            elif self._tool_executor is not None:
                results.append(await self._tool_executor.execute(call))

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
            "write_file": ToolName.WRITE_FILE,
            "run_script": ToolName.RUN_SCRIPT,
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
                requires_confirmation=destructive,
            )
        ]

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

    def _default_reply(self, intent: Optional[IntentResult], results: Optional[List[ToolResult]] = None) -> str:
        if intent is None:
            return "抱歉，我没有理解您的请求。"

        results = results or []
        if not results:
            # Nothing ran: either this intent has no tool mapping yet
            # (unknown / get_time / get_weather), or the tool callback returned
            # nothing. "好的。" here would be a false success for a request the
            # assistant never handled.
            return "抱歉，这个请求我还没有实现。"

        if all(r.success for r in results):
            return "好的，已为您完成。"

        reasons = [self._speakable(r) for r in results]
        reasons = [r for r in reasons if r]
        if not reasons:
            return "抱歉，这个操作没有成功。"
        return f"执行遇到问题：{'；'.join(reasons)}"

    # ── TTS ────────────────────────────────────────────────────

    async def _play_tts(self) -> None:
        context = self._context
        text = (context.tts_text if context else "") or ""

        if text.strip():
            request = TtsRequest(
                trace_id=context.trace_id if context else "",
                text=text,
                voice="guest" if (context and context.sv_result and context.sv_result.tier == "guest") else "default",
            )
            async for chunk in self.tts.synthesize(request):
                if self.on_tts_chunk:
                    await _maybe_await(self.on_tts_chunk(chunk))
                if self.tts.is_interrupted():
                    break
                await asyncio.sleep(0)

        await self._abort_utterance()

    # ── shutdown ───────────────────────────────────────────────

    def stop(self) -> None:
        self._running = False
        self.tts.interrupt()
