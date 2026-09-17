"""
Audio Pipeline Orchestrator.

Single-process async pipeline: KWS → SV → VAD → ASR → Intent → LLM → Tool → TTS.
Manages half-duplex (ASR paused during TTS) and KWS barge-in.
"""

from __future__ import annotations

import asyncio
import uuid
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import AsyncIterator, Callable, Deque, List, Optional

import numpy as np

from winvoice.config import get_config
from winvoice.logging import get_logger, set_trace_context, clear_trace_context, bind_trace_context
from winvoice.contracts import (
    AudioFrame, KwsTriggered, SvResult, VadSegment, AsrResult,
    IntentResult, ToolCall, ToolResult, TtsRequest, TtsChunk,
    InterruptTTS, SystemState, SystemStateName, ProcessName,
)
from .kws import create_kws_engine, KwsEngine
from .vad import create_vad_engine, VadEngine
from .asr import create_asr_engine, AsrEngine
from .sv import create_sv_engine, SvEngine
from .tts import create_tts_engine, TtsEngine

logger = get_logger(__name__)


class PipelineState(Enum):
    IDLE = "idle"
    KWS_LISTENING = "kws_listening"
    VAD_ACTIVE = "vad_active"
    ASR_RUNNING = "asr_running"
    LLM_THINKING = "llm_thinking"
    TTS_PLAYING = "tts_playing"


@dataclass
class PipelineContext:
    """Carries state across pipeline stages for one request."""
    trace_id: str
    state: PipelineState = PipelineState.IDLE
    speaker_id: str = "me"
    sv_result: Optional[SvResult] = None
    vad_frames: List[AudioFrame] = field(default_factory=list)
    asr_text: str = ""
    intent: Optional[IntentResult] = None
    tool_calls: List[ToolCall] = field(default_factory=list)
    tts_text: str = ""


class AudioPipeline:
    """
    Main audio processing pipeline.

    Coordinates KWS, SV, VAD, ASR, and TTS with half-duplex and barge-in.
    """

    def __init__(
        self,
        on_state_change: Optional[Callable[[SystemState], None]] = None,
        on_intent: Optional[Callable[[IntentResult], None]] = None,
        on_tool_call: Optional[Callable[[ToolCall], ToolResult]] = None,
        on_tts_chunk: Optional[Callable[[TtsChunk], None]] = None,
        use_stub: bool = False,
    ):
        self.on_state_change = on_state_change
        self.on_intent = on_intent
        on_tool_call = on_tool_call or (lambda _: None)
        self.on_tts_chunk = on_tts_chunk
        self.use_stub = use_stub

        # Engines
        self.kws: KwsEngine = create_kws_engine(use_stub)
        self.vad: VadEngine = create_vad_engine(use_stub)
        self.asr: AsrEngine = create_asr_engine(use_stub)
        self.sv: SvEngine = create_sv_engine(use_stub)
        self.tts: TtsEngine = create_tts_engine(use_stub)

        # State
        self._state = PipelineState.IDLE
        self._running = False
        self._half_duplex = get_config().get("audio.half_duplex", True)
        self._kws_during_tts = get_config().get("audio.kws_during_tts", True)
        self._audio_queue: Deque[AudioFrame] = deque()
        self._current_context: Optional[PipelineContext] = None

        # Callbacks for external integration
        self._intent_router = None
        self._tool_executor = None

    async def initialize(self) -> None:
        """Initialize all engines."""
        await asyncio.gather(
            self.kws.initialize(),
            self.vad.initialize(),
            self.asr.initialize(),
            self.sv.initialize(),
            self.tts.initialize(),
        )
        logger.info("pipeline_initialized", use_stub=self.use_stub)

    def set_intent_router(self, router) -> None:
        self._intent_router = router

    def set_tool_executor(self, executor) -> None:
        self._tool_executor = executor

    def _set_state(self, state: PipelineState) -> None:
        self._state = state
        if self.on_state_change:
            self.on_state_change(SystemState(
                state=SystemStateName(state.value),
                message=f"Pipeline: {state.value}",
            ))

    def push_audio(self, pcm_bytes: bytes, timestamp_ms: int) -> None:
        """Push raw PCM from microphone callback."""
        frame = AudioFrame(
            trace_id=self._current_context.trace_id if self._current_context else uuid.uuid4().hex[:16],
            timestamp_ms=timestamp_ms,
            data=pcm_bytes,
        )
        self._audio_queue.append(frame)

    async def run(self) -> None:
        """Main pipeline loop."""
        self._running = True
        self._set_state(PipelineState.IDLE)

        try:
            while self._running:
                if self._state == PipelineState.IDLE:
                    await self._run_kws()
                elif self._state == PipelineState.VAD_ACTIVE:
                    await self._run_vad_asr()
                elif self._state == PipelineState.LLM_THINKING:
                    await self._run_llm_and_tools()
                elif self._state == PipelineState.TTS_PLAYING:
                    await self._run_tts()
                else:
                    await asyncio.sleep(0.01)
        finally:
            self._running = False

    async def _run_kws(self) -> None:
        """KWS listening loop."""
        self._set_state(PipelineState.KWS_LISTENING)

        while self._running and self._state == PipelineState.KWS_LISTENING:
            # Process queued audio
            while self._audio_queue:
                frame = self._audio_queue.popleft()
                self.kws.accept_waveform(frame.data)

                # Also feed VAD if in half-duplex TTS pause (KWS stays active)
                if self._half_duplex and self._kws_during_tts and self._state == PipelineState.TTS_PLAYING:
                    pass  # KWS runs independently

            result = self.kws.get_result()
            if result:
                await self._on_kws_triggered(result)

            await asyncio.sleep(0.01)

    async def _on_kws_triggered(self, result: KwsTriggered) -> None:
        """KWS triggered - start SV + VAD."""
        logger.info("kws_triggered", keyword=result.keyword, confidence=result.confidence)
        self.kws.reset()

        trace_id = uuid.uuid4().hex[:16]
        set_trace_context(trace_id)
        self._current_context = PipelineContext(trace_id=trace_id)

        # Speaker Verification
        if self.sv.enabled:
            # Collect ~1s audio for SV (reuse recent frames)
            sv_frames = list(self._audio_queue)[-100:]  # last 1s
            if sv_frames:
                sv_result = self.sv.verify(sv_frames, self._current_context.speaker_id)
                if sv_result:
                    self._current_context.sv_result = sv_result
                    logger.info("sv_result", **sv_result.__dict__)
                    if sv_result.tier == "rejected":
                        self._set_state(PipelineState.IDLE)
                        self._current_context = None
                        clear_trace_context()
                        return

        self._set_state(PipelineState.VAD_ACTIVE)
        self._current_context.vad_frames.clear()

    async def _run_vad_asr(self) -> None:
        """VAD segmentation + ASR."""
        while self._running and self._state == PipelineState.VAD_ACTIVE:
            while self._audio_queue:
                frame = self._audio_queue.popleft()
                self._current_context.vad_frames.append(frame)
                segments = self.vad.accept_frame(frame)

                for segment in segments:
                    await self._on_vad_segment(segment)

            await asyncio.sleep(0.01)

    async def _on_vad_segment(self, segment: VadSegment) -> None:
        """VAD segment complete - run ASR."""
        logger.info("vad_segment", duration_ms=segment.duration_ms, frames=len(segment.frames))
        self._set_state(PipelineState.ASR_RUNNING)

        asr_result = await self.asr.transcribe(segment.frames)
        logger.info("asr_result", text=asr_result.text, confidence=asr_result.confidence)

        if asr_result.text.strip():
            self._current_context.asr_text = asr_result.text
            await self._route_intent(asr_result.text)
        else:
            self._set_state(PipelineState.KWS_LISTENING)

    async def _route_intent(self, text: str) -> None:
        """Route text through intent router (rules → local → cloud)."""
        self._set_state(PipelineState.LLM_THINKING)

        if self._intent_router:
            intent = await self._intent_router.route(text, self._current_context.sv_result)
            self._current_context.intent = intent
            if self.on_intent:
                self.on_intent(intent)

    async def _run_llm_and_tools(self) -> None:
        """Execute tool calls from intent."""
        if not self._current_context.intent:
            self._set_state(PipelineState.KWS_LISTENING)
            return

        intent = self._current_context.intent

        # Convert intent to tool calls
        tool_calls = self._intent_to_tool_calls(intent)
        self._current_context.tool_calls = tool_calls

        results = []
        for call in tool_calls:
            if self._tool_executor:
                result = await self._tool_executor.execute(call)
                results.append(result)
                if not result.success:
                    logger.warning("tool_failed", tool=call.tool, error=result.error)
                    break

        # Generate response text
        response = self._generate_response(intent, results)
        self._current_context.tts_text = response

        self._set_state(PipelineState.TTS_PLAYING)

    def _intent_to_tool_calls(self, intent: IntentResult) -> List[ToolCall]:
        """Map intent to tool calls (simplified)."""
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
        if not tool:
            return []
        return [ToolCall(
            trace_id=self._current_context.trace_id,
            tool=tool,
            args=intent.args,
            requires_confirmation=tool in (ToolName.WRITE_FILE, ToolName.RUN_SCRIPT),
        )]

    def _generate_response(self, intent: IntentResult, results: List[ToolResult]) -> str:
        """Generate natural language response from tool results."""
        if not results:
            return "抱歉，我没有理解您的请求。"

        success = all(r.success for r in results)
        if success:
            return "好的，已为您完成。"
        else:
            errors = [r.error for r in results if r.error]
            return f"执行遇到问题：{'; '.join(errors)}"

    async def _run_tts(self) -> None:
        """Play TTS with barge-in support."""
        if not self._current_context.tts_text:
            self._set_state(PipelineState.KWS_LISTENING)
            return

        request = TtsRequest(
            trace_id=self._current_context.trace_id,
            text=self._current_context.tts_text,
            voice="guest" if self._current_context.sv_result and self._current_context.sv_result.tier == "guest" else "default",
        )

        async for chunk in self.tts.synthesize(request):
            if self.on_tts_chunk:
                self.on_tts_chunk(chunk)

            # Check for barge-in (KWS during TTS)
            if self._kws_during_tts and self.tts.is_interrupted():
                logger.info("tts_barge_in")
                break

            await asyncio.sleep(0)

        # TTS complete or interrupted
        self.tts.interrupt()  # reset
        self._set_state(PipelineState.KWS_LISTENING)
        self._current_context = None
        clear_trace_context()

    def stop(self) -> None:
        self._running = False
        self.tts.interrupt()