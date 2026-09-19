"""
Entry point for the Windows Voice Assistant.

Usage:
    python -m winvoice                  # run with real models + microphone
    python -m winvoice --stub-audio     # run with model-free stubs
    python -m winvoice --check          # load every real model, report, exit
"""

from __future__ import annotations

import argparse
import asyncio
import signal
from pathlib import Path

from winvoice.audio import AudioPipeline, create_audio_stream
from winvoice.config import get_config, reset_config
from winvoice.contracts import SystemState, ToolCall, ToolResult
from winvoice.intent.router import create_intent_router
from winvoice.logging import configure_logging, get_logger
from winvoice.tools.executor import create_tool_executor

logger = get_logger(__name__)


class VoiceAssistant:
    """Owns the pipeline, the audio stream and the helper subsystems."""

    def __init__(self, config_path: str = "config/config.yaml", use_stub: bool = False):
        self.config_path = config_path
        self.use_stub = use_stub
        self.pipeline: AudioPipeline | None = None
        self.audio_stream = None
        self.intent_router = None
        self.tool_executor = None
        self._running = False

    # ── lifecycle ──────────────────────────────────────────────

    async def initialize(self, with_audio: bool = True) -> None:
        """Load config, engines and (optionally) the microphone stream."""
        reset_config()
        cfg = get_config(self.config_path)
        cfg.start_watching()

        configure_logging(process_name="main", level="INFO")

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
        logger.info("engines_ready", stub=self.use_stub)

        if with_audio:
            self.audio_stream = await create_audio_stream(on_audio_frame=self._on_audio_frame)
            logger.info("microphone_open")

        logger.info("voice_assistant_initialized", config=self.config_path, stub=self.use_stub)

    async def run(self) -> None:
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

    # ── callbacks ──────────────────────────────────────────────

    def _on_audio_frame(self, frame) -> None:
        """Microphone callback -> pipeline ingress."""
        self.pipeline.push_audio(frame.data)

    def _on_state_change(self, state: SystemState) -> None:
        logger.debug("state_change", state=state.state.value)

    async def _on_intent(self, intent) -> None:
        logger.info(
            "intent_received",
            intent=intent.intent.value,
            source=intent.source,
            confidence=intent.confidence,
        )

    async def _on_tool_call(self, call: ToolCall) -> ToolResult:
        logger.info("tool_call", tool=call.tool.value, args=call.args)
        result = await self.tool_executor.execute(call)
        logger.info("tool_result", tool=call.tool.value, success=result.success, error=result.error)
        return result

    def _on_tts_chunk(self, chunk) -> None:
        if self.audio_stream and chunk.data:
            asyncio.create_task(self.audio_stream.play_audio(chunk.data, chunk.sample_rate))


# ──────────────────────────────────────────────────────────────
# Self-check
# ──────────────────────────────────────────────────────────────

async def run_check(config_path: str, use_stub: bool) -> int:
    """
    Load every configured model and report, without opening the microphone
    or entering the main loop. This is the fastest way to answer
    "are the models wired up correctly?".
    """
    print("=" * 68)
    print("Windows Voice Assistant - startup check")
    print("=" * 68)

    assistant = VoiceAssistant(config_path=config_path, use_stub=use_stub)
    try:
        await assistant.initialize(with_audio=False)
    except Exception as e:
        print(f"\n[X] Startup check FAILED: {type(e).__name__}: {e}")
        return 1

    pipe = assistant.pipeline
    rows = [
        ("KWS  (wake word)", getattr(pipe.kws, "model_dir", None)),
        ("VAD  (silence)", getattr(pipe.vad, "model_path", None)),
        ("ASR  (speech->text)", getattr(pipe.asr, "model_path", None)),
        ("SV   (speaker ID)", getattr(pipe.sv, "model_path", None)),
        ("TTS  (text->speech)", getattr(pipe.tts, "model_dir", None)),
    ]

    print("\n  Engines loaded:")
    for label, path in rows:
        print(f"    [OK] {label:<22} {path}")

    if not use_stub:
        print("\n  Details:")
        print(f"    SV embedding dim   : {pipe.sv.embedding_dim}")
        print(f"    TTS sample rate    : {pipe.tts.sample_rate} Hz")
        print(f"    TTS speakers       : {pipe.tts.num_speakers}")
        print(f"    ASR mode           : {'streaming' if pipe.asr.streaming else 'sense-voice (offline)'}")

    print("\n  Intent rules:")
    from winvoice.intent.rules import match_rules

    for probe in ("打开记事本", "音量调大 20", "播放音乐"):
        hit = match_rules(probe)
        print(f"    {probe:<14} -> {hit.intent.value if hit else 'no rule match'}")

    print("\n" + "=" * 68)
    print("[OK] Startup check PASSED - all engines initialize with the current config")
    print("=" * 68)
    return 0


# ──────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────

async def main() -> int:
    parser = argparse.ArgumentParser(description="Windows Voice Assistant")
    parser.add_argument("--config", default="config/config.yaml", help="config file path")
    parser.add_argument("--stub-audio", action="store_true",
                        help="use model-free stub engines (development)")
    parser.add_argument("--check", action="store_true",
                        help="load every model, report, and exit (no microphone needed)")
    args = parser.parse_args()

    if args.check:
        return await run_check(args.config, args.stub_audio)

    assistant = VoiceAssistant(config_path=args.config, use_stub=args.stub_audio)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda: asyncio.create_task(_shutdown(assistant)))
        except NotImplementedError:
            pass  # Windows does not support add_signal_handler for SIGTERM

    try:
        await assistant.initialize(with_audio=True)
    except Exception as e:
        logger.error("initialization_failed", error=str(e), error_type=type(e).__name__)
        print(f"\n[X] Could not start: {type(e).__name__}: {e}")
        print("    Run `python -m winvoice --check` to diagnose model/config problems.")
        return 1

    print("Assistant is listening. Say the wake word (default: 'assistant'). Ctrl+C to stop.")
    await assistant.run()
    return 0


async def _shutdown(assistant: VoiceAssistant) -> None:
    logger.info("shutdown_initiated")
    assistant.stop()


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\nInterrupted.")
