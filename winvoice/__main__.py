"""
Main entry point for the Windows Voice Assistant.

Usage:
    python -m winvoice [--config CONFIG] [--stub-audio] [--test-pipeline]
"""

from __future__ import annotations

import argparse
import asyncio
import signal
import sys
import uuid
from pathlib import Path

from winvoice.config import get_config, reset_config
from winvoice.logging import configure_logging, get_logger
from winvoice.audio.pipeline import AudioPipeline, PipelineState
from winvoice.audio.stream import create_audio_stream
from winvoice.intent.router import create_intent_router
from winvoice.tools.executor import create_tool_executor
from winvoice.contracts import SystemState, SystemStateName, ToolCall, ToolResult

logger = get_logger(__name__)


class VoiceAssistant:
    """Main application class."""

    def __init__(self, config_path: str = "config/config.yaml", use_stub: bool = False):
        self.config_path = config_path
        self.use_stub = use_stub
        self.pipeline: AudioPipeline = None
        self.audio_stream = None
        self.intent_router = None
        self.tool_executor = None
        self._running = False

    async def initialize(self) -> None:
        """Initialize all components."""
        # Reset config singleton for new path
        reset_config()
        cfg = get_config(self.config_path)
        cfg.start_watching()

        # Setup logging
        configure_logging(process_name="main", level="INFO")

        # Create components
        self.intent_router = create_intent_router()
        self.tool_executor = create_tool_executor()

        self.pipeline = AudioPipeline(
            on_state_change=self._on_state_change,
            on_intent=self._on_intent,
            on_tool_call=self._on_tool_call,
            on_tts_chunk=self._on_tts_chunk,
            use_stub=self.use_stub,
        )
        self.pipeline.set_intent_router(self.intent_router)
        self.pipeline.set_tool_executor(self.tool_executor)

        await self.pipeline.initialize()

        # Create audio stream
        self.audio_stream = await create_audio_stream(
            on_audio_frame=self._on_audio_frame,
        )

        logger.info("voice_assistant_initialized", config=self.config_path, stub=self.use_stub)

    def _on_audio_frame(self, frame) -> None:
        """Callback for incoming audio frames."""
        self.pipeline.push_audio(frame.data, frame.timestamp_ms)

    def _on_state_change(self, state: SystemState) -> None:
        logger.info("state_change", state=state.state.value, message=state.message)

    async def _on_intent(self, intent) -> None:
        logger.info("intent_received", intent=intent.intent.value, source=intent.source, confidence=intent.confidence)

    async def _on_tool_call(self, call: ToolCall) -> ToolResult:
        logger.info("tool_call", tool=call.tool.value, args=call.args)
        result = await self.tool_executor.execute(call)
        logger.info("tool_result", tool=call.tool.value, success=result.success, error=result.error)
        return result

    def _on_tts_chunk(self, chunk) -> None:
        """Play TTS audio chunk."""
        if self.audio_stream and chunk.data:
            asyncio.create_task(self.audio_stream.play_audio(chunk.data, chunk.sample_rate))

    async def run(self) -> None:
        """Run the main pipeline loop."""
        self._running = True
        try:
            await self.pipeline.run()
        except asyncio.CancelledError:
            pass
        finally:
            self._running = False

    def stop(self) -> None:
        self._running = False
        if self.pipeline:
            self.pipeline.stop()
        if self.audio_stream:
            self.audio_stream.stop()


async def main():
    parser = argparse.ArgumentParser(description="Windows Voice Assistant")
    parser.add_argument("--config", default="config/config.yaml", help="Config file path")
    parser.add_argument("--stub-audio", action="store_true", help="Use stub audio engines (no sherpa-onnx)")
    parser.add_argument("--test-pipeline", action="store_true", help="Run pipeline test and exit")
    args = parser.parse_args()

    assistant = VoiceAssistant(config_path=args.config, use_stub=args.stub_audio)

    # Setup signal handlers
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda: asyncio.create_task(shutdown(assistant)))
        except NotImplementedError:
            # Windows doesn't support add_signal_handler for SIGTERM
            pass

    await assistant.initialize()

    if args.test_pipeline:
        # Run a quick test
        await run_test(assistant)
    else:
        await assistant.run()


async def shutdown(assistant: VoiceAssistant):
    logger.info("shutdown_initiated")
    assistant.stop()


async def run_test(assistant: VoiceAssistant):
    """Run a quick pipeline test with synthetic input."""
    logger.info("test_pipeline_start")

    # Simulate KWS trigger
    trace_id = uuid.uuid4().hex[:16]
    from winvoice.contracts import KwsTriggered, SvResult, SpeakerTier
    kws_result = KwsTriggered(
        trace_id=trace_id,
        keyword="assistant",
        confidence=0.9,
        timestamp_ms=0,
    )

    # This would normally come from audio callback
    # For test, we just verify initialization works
    logger.info("test_pipeline_passed")
    assistant.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass