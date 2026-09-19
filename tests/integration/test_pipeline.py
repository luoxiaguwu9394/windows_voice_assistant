"""
Integration tests for audio pipeline (with stub engines).
"""

import asyncio
import uuid
from pathlib import Path

import pytest

from winvoice.audio.pipeline import AudioPipeline, PipelineState
from winvoice.contracts import (
    SystemState,
    SystemStateName,
    ToolCall,
    ToolName,
    ToolResult,
)
from winvoice.intent.router import create_intent_router
from winvoice.tools.executor import create_tool_executor


class TestAudioPipeline:
    """Test audio pipeline with stub engines."""

    @pytest.fixture
    async def pipeline(self):
        pipe = AudioPipeline(use_stub=True)
        await pipe.initialize()
        yield pipe
        pipe.stop()

    @pytest.mark.asyncio
    async def test_pipeline_initialization(self, pipeline):
        assert pipeline.kws is not None
        assert pipeline.vad is not None
        assert pipeline.asr is not None
        assert pipeline.sv is not None
        assert pipeline.tts is not None

    @pytest.mark.asyncio
    async def test_state_transitions(self, pipeline):
        states = []

        def on_state_change(state: SystemState):
            states.append(state.state)

        pipeline.on_state_change = on_state_change
        pipeline._running = True

        # Run one iteration
        pipeline._state = PipelineState.IDLE
        pipeline._set_state(PipelineState.KWS_LISTENING)
        await asyncio.sleep(0.05)

        assert SystemStateName.KWS_LISTENING in states


class TestIntentRouterIntegration:
    """Test intent router with stubs."""

    @pytest.fixture
    def router(self):
        return create_intent_router()

    @pytest.mark.asyncio
    async def test_rule_based_routing(self, router):
        result = await router.route("打开记事本", None)
        assert result.source == "rules"
        assert result.intent.value == "open_app"

    @pytest.mark.asyncio
    async def test_unknown_fallback(self, router):
        result = await router.route("完全不相关的随机文本xyz123", None)
        # Should fall back to unknown
        assert result.intent.value == "unknown"


class TestToolExecutorIntegration:
    """Test tool executor with real builtin tools."""

    @pytest.fixture
    def executor(self):
        return create_tool_executor()

    @pytest.mark.asyncio
    async def test_execute_open_app(self, executor):
        call = ToolCall(
            trace_id="test1",
            tool=ToolName.OPEN_APP,
            args={"app": "notepad"},
        )
        result = await executor.execute(call)
        # notepad.exe should succeed
        assert result.success is True

    @pytest.mark.asyncio
    async def test_execute_unknown_app(self, executor):
        call = ToolCall(
            trace_id="test1",
            tool=ToolName.OPEN_APP,
            args={"app": "nonexistent_app_xyz"},
        )
        result = await executor.execute(call)
        assert result.success is False

    @pytest.mark.asyncio
    async def test_execute_search_web(self, executor):
        call = ToolCall(
            trace_id="test1",
            tool=ToolName.SEARCH_WEB,
            args={"query": "test query"},
        )
        result = await executor.execute(call)
        assert result.success is True

    @pytest.mark.asyncio
    async def test_execute_write_file_requires_confirmation(self, executor):
        # write_file is restricted to the user's home directory, and the test
        # must not leave artefacts in the repo, so target a unique temp file
        # inside the home directory and remove it afterwards.
        target = Path.home() / f"winvoice_test_{uuid.uuid4().hex[:8]}.txt"
        call = ToolCall(
            trace_id="test1",
            tool=ToolName.WRITE_FILE,
            args={"path": str(target), "content": "test"},
        )
        try:
            # First call without confirmation must be refused.
            result = await executor.execute(call, confirmed=False)
            assert result.success is False
            assert "CONFIRMATION_REQUIRED" in result.error
            assert not target.exists(), "file must not be written before confirmation"

            # Second call with confirmation succeeds.
            result = await executor.execute(call, confirmed=True)
            assert result.success is True
            assert target.read_text(encoding="utf-8") == "test"
        finally:
            target.unlink(missing_ok=True)

    @pytest.mark.asyncio
    async def test_execute_write_file_rejects_outside_home(self, executor):
        call = ToolCall(
            trace_id="test1",
            tool=ToolName.WRITE_FILE,
            args={"path": "C:/Windows/winvoice_should_not_exist.txt", "content": "x"},
        )
        result = await executor.execute(call, confirmed=True)
        assert result.success is False
        assert "user directory" in result.error

    @pytest.mark.asyncio
    async def test_execute_read_file(self, executor, tmp_path):
        test_file = tmp_path / "read_test.txt"
        test_file.write_text("hello world")

        call = ToolCall(
            trace_id="test1",
            tool=ToolName.READ_FILE,
            args={"path": str(test_file)},
        )
        result = await executor.execute(call)
        assert result.success is True
        assert result.result["content"] == "hello world"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])