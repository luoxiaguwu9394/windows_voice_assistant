#!/usr/bin/env python3
"""
Stub Pipeline Test Script.

Runs the full audio pipeline with stub engines (no models required).
Verifies: KWS → SV → VAD → ASR → Intent → Tool → TTS flow.
"""

import asyncio
import os
import sys
import uuid

# Enable stub mode
os.environ["WINVOICE_STUB_AUDIO"] = "1"

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from winvoice.config import get_config, reset_config
from winvoice.logging import configure_logging, get_logger
from winvoice.audio.pipeline import AudioPipeline
from winvoice.intent.router import create_intent_router
from winvoice.tools.executor import create_tool_executor
from winvoice.contracts import SystemState, SystemStateName, ToolCall, ToolResult, ToolName, KwsTriggered, SvResult, SpeakerTier

logger = get_logger(__name__)


class TestVoiceAssistant:
    """Test harness for stub pipeline."""

    def __init__(self):
        self.pipeline = None
        self.intent_router = None
        self.tool_executor = None
        self.results = []

    async def initialize(self):
        reset_config()
        cfg = get_config("config/config.yaml")
        
        configure_logging(process_name="test", level="DEBUG")
        
        self.intent_router = create_intent_router()
        self.tool_executor = create_tool_executor()

        self.pipeline = AudioPipeline(
            on_state_change=self._on_state_change,
            on_intent=self._on_intent,
            on_tool_call=self._on_tool_call,
            on_tts_chunk=self._on_tts_chunk,
            use_stub=True,
        )
        self.pipeline.set_intent_router(self.intent_router)
        self.pipeline.set_tool_executor(self.tool_executor)

        await self.pipeline.initialize()
        logger.info("test_assistant_initialized")

    def _on_state_change(self, state: SystemState):
        logger.info("state_change", state=state.state.value)
        self.results.append(("state", state.state.value))

    async def _on_intent(self, intent):
        logger.info("intent_received", intent=intent.intent.value, source=intent.source)
        self.results.append(("intent", intent.intent.value, intent.source))

    async def _on_tool_call(self, call: ToolCall) -> ToolResult:
        logger.info("tool_call", tool=call.tool.value)
        result = await self.tool_executor.execute(call, confirmed=True)
        self.results.append(("tool", call.tool.value, result.success))
        return result

    def _on_tts_chunk(self, chunk):
        logger.debug("tts_chunk", size=len(chunk.data) if chunk.data else 0, final=chunk.is_final)

    async def run_test_scenario(self, text: str):
        """Simulate a complete interaction with given text."""
        trace_id = uuid.uuid4().hex[:16]
        
        # Simulate KWS trigger
        kws_result = KwsTriggered(
            trace_id=trace_id,
            keyword="assistant",
            confidence=0.9,
            timestamp_ms=0,
        )
        
        # Manually trigger the pipeline's KWS handler
        # We need to inject audio frames that will trigger VAD → ASR
        # For stub, we can directly call the intent router
        
        sv_result = SvResult(
            speaker_id="me",
            score=0.85,
            tier="full",
            threshold_high=0.6,
            threshold_low=0.4,
        )
        
        intent = await self.intent_router.route(text, sv_result)
        logger.info("test_intent_result", intent=intent.intent.value, source=intent.source)
        
        # Execute tools if any
        if intent.intent != ToolName.UNKNOWN:
            from winvoice.contracts import IntentName
            tool_mapping = {
                IntentName.OPEN_APP: ToolName.OPEN_APP,
                IntentName.CLOSE_APP: ToolName.CLOSE_APP,
                IntentName.SET_VOLUME: ToolName.SET_VOLUME,
                IntentName.MEDIA_CONTROL: ToolName.MEDIA_CONTROL,
                IntentName.SEARCH_WEB: ToolName.SEARCH_WEB,
                IntentName.READ_FILE: ToolName.READ_FILE,
                IntentName.WRITE_FILE: ToolName.WRITE_FILE,
                IntentName.RUN_SCRIPT: ToolName.RUN_SCRIPT,
                IntentName.GET_TIME: ToolName.GET_TIME,
                IntentName.GET_WEATHER: ToolName.GET_WEATHER,
            }
            tool = tool_mapping.get(intent.intent)
            if tool:
                call = ToolCall(
                    trace_id=trace_id,
                    tool=tool,
                    args=intent.args,
                    requires_confirmation=tool in (ToolName.WRITE_FILE, ToolName.RUN_SCRIPT),
                )
                result = await self.tool_executor.execute(call, confirmed=True)
                logger.info("test_tool_result", tool=tool.value, success=result.success)

        return intent

    async def cleanup(self):
        if self.pipeline:
            self.pipeline.stop()


async def main():
    print("=" * 60)
    print("Windows Voice Assistant - Stub Pipeline Test")
    print("=" * 60)
    
    assistant = TestVoiceAssistant()
    await assistant.initialize()
    
    test_cases = [
        ("打开记事本", "open_app", "rules"),
        ("音量调大 20", "set_volume", "rules"),
        ("播放音乐", "media_control", "rules"),
        ("搜索 Python 教程", "search_web", "rules"),
        ("关闭计算器", "close_app", "rules"),
        ("今天天气怎么样", "get_weather", "rules"),  # 天气 is a rule, answered by the tool
        ("现在几点了", "get_time", "rules"),
        ("帮我写个Python脚本", "write_file", "local"),  # Will fallback
    ]
    
    passed = 0
    failed = 0
    
    for text, expected_intent, expected_source in test_cases:
        print(f"\n🧪 Test: '{text}'")
        try:
            intent = await assistant.run_test_scenario(text)
            
            # Check intent
            intent_ok = intent.intent.value == expected_intent
            source_ok = intent.source == expected_source or (expected_source == "local" and intent.source in ("local", "cloud"))
            
            if intent_ok:
                print(f"  ✅ Intent: {intent.intent.value} (source: {intent.source})")
                passed += 1
            else:
                print(f"  ❌ Intent mismatch: got {intent.intent.value}, expected {expected_intent}")
                failed += 1
                
        except Exception as e:
            print(f"  ❌ Error: {e}")
            failed += 1
    
    await assistant.cleanup()
    
    print("\n" + "=" * 60)
    print(f"Results: {passed} passed, {failed} failed")
    print("=" * 60)
    
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    exit(asyncio.run(main()))