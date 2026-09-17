"""
End-to-end tests (marked as manual, require real hardware/models).
"""

import pytest
from winvoice.audio.pipeline import AudioPipeline
from winvoice.intent.router import create_intent_router


@pytest.mark.manual
class TestE2E:
    """End-to-end tests requiring real audio hardware and models."""

    @pytest.mark.asyncio
    async def test_full_pipeline_with_real_audio(self):
        """Test complete pipeline with real microphone input."""
        pipeline = AudioPipeline(use_stub=False)
        await pipeline.initialize()

        # This would require actual audio input
        # Run for a few seconds then stop
        pipeline._running = True
        # In real test, we'd feed audio and verify output
        pipeline.stop()

    @pytest.mark.asyncio
    async def test_intent_router_with_real_llm(self):
        """Test intent router with actual Ollama/llama.cpp server."""
        router = create_intent_router()
        result = await router.route("打开记事本", None)
        # With real LLM, should get structured intent
        assert result.intent is not None


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-m manual"])