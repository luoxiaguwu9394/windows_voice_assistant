"""
Regression: a tool outcome must reach the spoken reply.

Every tool-based utterance answered "好的。" regardless of what happened —
including the failure in the reported log:

    tool_result  error='File not found' success=False tool=read_file
    tts_generated text_len=3                      <- "好的。"

Two independent defects produced that identical lie:

1. `ToolExecutor.execute` returns the `ToolResult` dataclass defined inside
   `winvoice/tools/executor.py`, but `AudioPipeline._run_tools` accepts it only
   when it `isinstance(..., contracts.ToolResult)`. Each result was silently
   dropped, so `_default_reply` always saw an empty result list.
2. `_default_reply` treats "no results" as success. Intents with no tool
   mapping (`unknown`, `get_time`, `get_weather`) therefore also claim
   "好的。" for a question that was never handled.

Both are asserted here at the production seam: the text handed to TTS.
"""

from __future__ import annotations

import pytest

from winvoice.audio.asr import AsrResult
from winvoice.audio.pipeline import AudioPipeline, PipelineContext
from winvoice.contracts import IntentName, IntentResult
from winvoice.tools.executor import create_tool_executor


class _FixedRouter:
    """Intent router stand-in that always returns one fixed intent."""

    def __init__(self, intent: IntentResult) -> None:
        self._intent = intent

    async def route(self, text, sv_result=None) -> IntentResult:
        return self._intent


class _RecordingTts:
    """Captures the text the assistant would actually speak."""

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


async def _spoken_for(intent: IntentResult) -> str:
    """Run one utterance through intent -> tools -> TTS and return the reply."""
    pipe = AudioPipeline(use_stub=True)
    await pipe.initialize()

    executor = create_tool_executor()

    async def on_tool_call(call):
        # Same shape as VoiceAssistant._on_tool_call in __main__.
        return await executor.execute(call)

    pipe.on_tool_call = on_tool_call
    pipe.set_intent_router(_FixedRouter(intent))
    pipe.tts = _RecordingTts()
    pipe._context = PipelineContext(trace_id="test")

    await pipe._route_intent(
        AsrResult(text=intent.raw_text or "test", language="zh", confidence=0.0)
    )

    assert pipe.tts.spoken, "nothing was spoken"
    return pipe.tts.spoken[-1]


@pytest.mark.asyncio
async def test_failed_tool_speaks_the_error_not_success():
    """The reported case: read_file on a path that does not exist."""
    spoken = await _spoken_for(
        IntentResult(
            trace_id="t",
            intent=IntentName.READ_FILE,
            args={"path": "current directory"},
            confidence=0.8,
            source="local",
            raw_text="当前目录下有什么文件？",
        )
    )

    # The failure must reach the reply, in words the Chinese TTS can say.
    assert spoken != "好的，已为您完成。", f"claimed success for a failed tool: {spoken!r}"
    assert "文件" in spoken, f"the reason never reached the reply: {spoken!r}"


@pytest.mark.asyncio
async def test_intent_without_a_tool_does_not_claim_success():
    """`get_time` has no tool mapping; '好的。' would be a false success."""
    spoken = await _spoken_for(
        IntentResult(
            trace_id="t",
            intent=IntentName.GET_TIME,
            args={},
            confidence=0.8,
            source="local",
            raw_text="现在几点了？",
        )
    )

    assert "好的" not in spoken, f"claimed success for an unhandled intent: {spoken!r}"
