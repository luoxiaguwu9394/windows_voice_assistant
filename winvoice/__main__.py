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

from winvoice.audio import AudioPipeline, create_audio_stream, create_speech_player
from winvoice.config import get_config, reset_config
from winvoice.contracts import SystemState, ToolCall, ToolResult
from winvoice.integrity import failures_by_model, verify_sealed_models
from winvoice.intent.router import create_intent_router
from winvoice.llm.server import LlamaServerManager
from winvoice.logging import configure_logging, get_logger
from winvoice.text import normalize_for_speech, segment_for_speech, speech_text_config
from winvoice.tools.executor import create_tool_executor

logger = get_logger(__name__)


class VoiceAssistant:
    """Owns the pipeline, the audio stream and the helper subsystems."""

    def __init__(self, config_path: str = "config/config.yaml", use_stub: bool = False):
        self.config_path = config_path
        self.use_stub = use_stub
        self.pipeline: AudioPipeline | None = None
        self.audio_stream = None
        self.speech_player = None
        self.intent_router = None
        self.tool_executor = None
        self.dsh_router = None
        self.server_manager: LlamaServerManager | None = None
        self._running = False

    # ── lifecycle ──────────────────────────────────────────────

    async def initialize(self, with_audio: bool = True) -> None:
        """Load config, engines and (optionally) the microphone stream."""
        reset_config()
        cfg = get_config(self.config_path)
        cfg.start_watching()

        configure_logging(process_name="main", level="INFO")

        # Integrity quick check (sizes only) before any engine loads a model:
        # a truncated model should be named *before* it fails to load.
        report = verify_sealed_models(deep=False)
        if not report.ok:
            print("[!] Model integrity check failed - see the model_integrity_failed log entries.")

        # The local model server. Reuses one that is already running; spawns
        # our own when `llm.local.auto_start` allows and none answers. The
        # first spawn includes the model load, so this may block for a while
        # *before* the audio engines initialize — nothing else runs yet.
        # Stub mode is model-free by definition and never touches the server.
        self.server_manager = None
        if not self.use_stub:
            self.server_manager = LlamaServerManager()
            self.server_manager.ensure_running()

        self.intent_router = create_intent_router()
        self.tool_executor = create_tool_executor()

        # The agent is optional and failure-tolerant: a broken DSH install must
        # not stop the assistant from starting, because the rule tier and the
        # tools still work without it.
        self.dsh_router = self._create_dsh_router()

        self.pipeline = AudioPipeline(
            on_state_change=self._on_state_change,
            on_intent=self._on_intent,
            on_tool_call=self._on_tool_call,
            on_tts_chunk=self._on_tts_chunk,
            use_stub=self.use_stub,
        )
        self.pipeline.set_intent_router(self.intent_router)
        self.pipeline.set_tool_executor(self.tool_executor)
        if self.dsh_router is not None:
            self.pipeline.set_dsh_router(self.dsh_router)

        # The speaker. The pipeline owns playback through this object; the audio
        # stream below stays input-only, which is what stops a chunk from being
        # written straight into a device without a buffer or a pause.
        self.speech_player = create_speech_player(use_stub=self.use_stub)
        self.pipeline.set_speech_player(self.speech_player)

        await self.pipeline.initialize()
        logger.info("engines_ready", stub=self.use_stub)

        if with_audio:
            self.audio_stream = await create_audio_stream(on_audio_frame=self._on_audio_frame)
            logger.info("microphone_open")

        logger.info("voice_assistant_initialized", config=self.config_path, stub=self.use_stub)

    def _create_dsh_router(self):
        """
        Build the agent router, or None when DSH is not configured.

        `winvoice.dsh` is imported lazily so that a deployment without the
        optional dependencies never touches the module that reaches for them.
        """
        try:
            from winvoice.dsh import DSHRouter, load_settings

            settings = load_settings()
        except Exception as e:
            logger.warning("dsh_settings_unavailable", error=str(e))
            return None

        if not settings.enabled or not settings.local.enabled:
            logger.info("dsh_not_enabled")
            return None

        router = DSHRouter(settings)
        logger.info(
            "dsh_enabled",
            model=settings.local.model or None,
            provider=settings.local.provider,
            escalation=settings.escalation_enabled and settings.cloud.enabled,
            max_local_attempts=settings.max_local_attempts,
        )
        return router

    async def shutdown(self) -> None:
        """Release the agent's subprocess, our llama-server and the audio output device."""
        if self.pipeline is not None:
            try:
                await self.pipeline.shutdown()
            except Exception as e:
                logger.warning("pipeline_shutdown_failed", error=str(e))
        if self.dsh_router is not None:
            try:
                await self.dsh_router.close()
            except Exception as e:
                logger.warning("dsh_shutdown_failed", error=str(e))
        if self.server_manager is not None:
            # Only a server WE spawned is terminated; a reused one stays.
            self.server_manager.shutdown()

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
        """
        Observe chunks; do **not** write them to a device.

        Playback belongs to the pipeline's player now, which buffers, converts to
        the device's own sample rate and writes the pauses between sentences.
        Writing a chunk straight to an `OutputStream` here is exactly the defect
        this replaced: no buffer, no pause, an 8 kHz MME stream on a 44.1 kHz
        device.
        """
        logger.debug(
            "tts_chunk",
            bytes=len(chunk.data),
            sample_rate=chunk.sample_rate,
            pause_after_ms=getattr(chunk, "pause_after_ms", 0),
        )


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

    try:
        pipe = assistant.pipeline
        rows = [
            ("KWS  (wake word)", getattr(pipe.kws, "model_dir", None)),
            ("VAD  (silence)", getattr(pipe.vad, "model_path", None)),
            ("ASR  (speech->text)", getattr(pipe.asr, "model_path", None)),
            ("SV   (speaker ID)", getattr(pipe.sv, "model_path", None)),
            ("TTS  (text->speech)", getattr(getattr(pipe.tts, "model", None), "model_file", None)
             or getattr(pipe.tts, "model_dir", None)),
        ]

        print("\n  Engines loaded:")
        for label, path in rows:
            print(f"    [OK] {label:<22} {path}")

        if not use_stub:
            print("\n  Details:")
            print(f"    SV embedding dim   : {pipe.sv.embedding_dim}")
            print(f"    TTS backend        : {getattr(getattr(pipe.tts, 'model', None), 'backend', '?')}")
            print(f"    TTS sample rate    : {pipe.tts.sample_rate} Hz")
            print(f"    TTS speakers       : {pipe.tts.num_speakers}")
            print(f"    ASR mode           : {'streaming' if pipe.asr.streaming else 'sense-voice (offline)'}")

        # The local model server the assistant just ensured is running
        # (spawned by initialize, or reused from a terminal you started).
        print("\n  Local LLM server:")
        server = assistant.server_manager
        if server is not None and server.is_healthy():
            print(f"    [OK] llama-server     : {server.server_root} reachable")
        else:
            print(f"    [ ] llama-server     : not reachable ({server.server_root if server else 'n/a'}) - "
                  "rule tier and small talk still work")

        # Integrity report (deep: every sealed file is hashed here, so a
        # same-size corruption is caught too).
        print("\n  Model integrity:")
        report = verify_sealed_models(deep=True)
        if not report.sealed:
            print("    [ ] not sealed - run: python scripts/download_models.py --seal")
        elif report.ok:
            print(f"    [OK] {report.checked} files match the seal")
        else:
            for _model_dir, fails in failures_by_model(report).items():
                for failure in fails:
                    print(f"    [X] {failure['reason']}: models/{failure['path']}")
            print("    Re-download the listed model(s) or re-seal if the change was intended.")
            return 1

        # Segmentation is what decides how the assistant sounds, so the check prints
        # it: the same information `scripts/show_segmentation.py` gives, for the
        # configuration that is actually loaded.
        speech = speech_text_config()
        sample = "我先把桌面上那份重要的文件保存好，接下来我会把处理结果告诉你。"
        print("\n  Spoken output (segmentation and pauses):")
        print(f"    budget             : {speech.max_chars} chars "
              f"(agent replies {speech.reply_max_chars}), tail silence {speech.tail_silence_ms}ms")
        print(f"    trim edge silence  : {speech.trim_silence} "
              f"(ratio {speech.trim_ratio}, guard {speech.trim_guard_ms}ms)")
        print(f"    seg {sample}")
        for index, segment in enumerate(segment_for_speech(normalize_for_speech(sample, max_chars=240), speech)):
            print(f"      {index}: {segment.chars:>3} chars  pause {segment.pause_after_ms:>4}ms  "
                  f"[{segment.kind}] {segment.text}")

        print("\n  Intent rules:")
        from winvoice.intent.rules import match_rules

        for probe in ("打开记事本", "音量调大 20", "播放音乐"):
            hit = match_rules(probe)
            print(f"    {probe:<14} -> {hit.intent.value if hit else 'no rule match'}")

        print("\n" + "=" * 68)
        print("[OK] Startup check PASSED - all engines initialize with the current config")
        print("=" * 68)
        return 0
    finally:
        # Cleans up engines *and* any llama-server this check spawned; a
        # server you started yourself is reused and left alone.
        await assistant.shutdown()


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
    try:
        await assistant.run()
    finally:
        await assistant.shutdown()
    return 0


async def _shutdown(assistant: VoiceAssistant) -> None:
    logger.info("shutdown_initiated")
    assistant.stop()


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\nInterrupted.")
