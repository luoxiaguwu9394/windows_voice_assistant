"""
Regression: a tool outcome must reach the spoken reply — success included.

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
   mapping (`unknown`) therefore also claim "好的。" for a question that was
   never handled. (`get_time` and `get_weather` were the examples at the time;
   they have tool mappings now — see the second half of this file.)

The first half of this file pins the failure side. The second half pins the
mirror image: a tool that *did* something has a result to report, and
`ToolResult.message` is the field it reports it in — so a successful `message`
must be spoken too, while a success with nothing to say keeps the old generic
acknowledgement, and English never reaches the speaker.

The same seam carries the two query tools (`get_time`, `get_weather`): what
matters here is only that their answer arrives at TTS.
"""

from __future__ import annotations

import pytest

from winvoice.audio.asr import AsrResult
from winvoice.audio.pipeline import AudioPipeline, PipelineContext
from winvoice.contracts import IntentName, IntentResult, ToolName, ToolResult
from winvoice.tools import weather
from winvoice.tools.executor import create_tool_executor


class _FakeWeatherConfig:
    """`get_weather` only ever calls `.get()` on the config object."""

    def __init__(self, values: dict) -> None:
        self._values = values

    def get(self, key: str, default=None):
        return self._values.get(key, default)


def _fake_weather_config() -> _FakeWeatherConfig:
    return _FakeWeatherConfig(
        {"weather.enabled": True, "weather.city": "北京", "weather.timeout_s": 5.0}
    )


async def _online_weather(url: str, timeout_s: float) -> dict:
    return {
        "current_condition": [
            {"temp_C": "15", "weatherCode": "113", "weatherDesc": [{"value": "晴"}]}
        ],
        "weather": [{"maxtempC": "20", "mintempC": "10"}],
    }



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
    """`unknown` has no tool mapping; '好的。' would be a false success."""
    spoken = await _spoken_for(
        IntentResult(
            trace_id="t",
            intent=IntentName.UNKNOWN,
            args={},
            confidence=0.0,
            source="local",
            raw_text="完全不相关的随机文本",
        )
    )

    assert "好的" not in spoken, f"claimed success for an unhandled intent: {spoken!r}"


# ── 1.1: a successful result has something to say ──────────────────────────

GENERIC_SUCCESS = "好的，已为您完成。"


@pytest.mark.asyncio
async def test_a_successful_result_with_a_message_is_spoken():
    """The reported symptom: the tool succeeded and the user heard 好的。"""
    spoken = await _spoken_for(
        IntentResult(
            trace_id="t",
            intent=IntentName.GET_TIME,
            args={},
            confidence=0.95,
            source="rules",
            raw_text="现在几点了？",
        )
    )

    assert spoken != GENERIC_SUCCESS, f"the tool's own message never reached TTS: {spoken!r}"
    assert spoken.startswith("现在是") and spoken.endswith("。"), spoken
    assert "点" in spoken, spoken


@pytest.mark.asyncio
async def test_a_query_answer_reaches_tts_through_the_async_handler(monkeypatch):
    """`get_weather` is `async`; the executor must await it, not store a coroutine."""
    monkeypatch.setattr(weather, "get_config", _fake_weather_config)
    monkeypatch.setattr(weather, "_fetch_weather_json", _online_weather)

    spoken = await _spoken_for(
        IntentResult(
            trace_id="t",
            intent=IntentName.GET_WEATHER,
            args={},
            confidence=0.95,
            source="rules",
            raw_text="今天天气怎么样",
        )
    )

    assert spoken == "北京今天晴，气温 10 到 20 度，现在 15 度。", spoken


@pytest.mark.asyncio
async def test_an_offline_weather_lookup_is_spoken_as_a_failure(monkeypatch):
    async def offline(url: str, timeout_s: float) -> dict:
        raise OSError("no network")

    monkeypatch.setattr(weather, "get_config", _fake_weather_config)
    monkeypatch.setattr(weather, "_fetch_weather_json", offline)

    spoken = await _spoken_for(
        IntentResult(
            trace_id="t",
            intent=IntentName.GET_WEATHER,
            args={},
            confidence=0.95,
            source="rules",
            raw_text="今天天气怎么样",
        )
    )

    assert "天气" in spoken and "查不到" in spoken, spoken


def test_a_success_without_a_message_keeps_the_generic_acknowledgement():
    """A success with nothing to say keeps today's behaviour (spec.md §6.4)."""
    pipe = AudioPipeline(use_stub=True)
    intent = IntentResult(
        trace_id="t",
        intent=IntentName.READ_FILE,
        args={"path": "x"},
        confidence=0.95,
        source="rules",
        raw_text="读取文件 x",
    )
    silent_success = ToolResult(trace_id="t", tool=ToolName.READ_FILE, success=True, result={})

    assert pipe._default_reply(intent, [silent_success]) == GENERIC_SUCCESS


def test_an_english_success_message_is_never_spoken():
    """
    A tool that writes `message` for the speaker must write Chinese.

    The lexicon has no Latin entries, so an English message would be spoken
    with holes in it; falling back to the generic sentence is the safe answer.
    """
    pipe = AudioPipeline(use_stub=True)
    intent = IntentResult(
        trace_id="t",
        intent=IntentName.OPEN_APP,
        args={"app": "notepad"},
        confidence=0.95,
        source="rules",
        raw_text="打开记事本",
    )
    result = ToolResult(
        trace_id="t", tool=ToolName.OPEN_APP, success=True, message="Opened notepad"
    )

    spoken = pipe._default_reply(intent, [result])

    assert spoken == GENERIC_SUCCESS, spoken


def test_an_overlong_success_message_is_clipped():
    """The TTS synthesises a whole sentence before its first chunk: keep it short."""
    pipe = AudioPipeline(use_stub=True)
    intent = IntentResult(
        trace_id="t",
        intent=IntentName.GET_TIME,
        args={},
        confidence=0.95,
        source="rules",
        raw_text="现在几点了？",
    )
    result = ToolResult(
        trace_id="t", tool=ToolName.GET_TIME, success=True, message="现在是三点整。" * 40
    )

    spoken = pipe._default_reply(intent, [result])

    assert len(spoken) <= 80, f"too long to speak comfortably: {len(spoken)} chars"
    assert spoken.endswith("。"), spoken


def test_a_long_message_does_not_clip_a_later_result_away():
    """
    Clipping the joined text would let a long first message silence the rest.

    One utterance maps to one tool call today, so this is about the shape
    staying honest if that ever changes — every result keeps a share of the
    budget rather than vanishing.
    """
    pipe = AudioPipeline(use_stub=True)
    intent = IntentResult(
        trace_id="t",
        intent=IntentName.GET_TIME,
        args={},
        confidence=0.95,
        source="rules",
        raw_text="现在几点了？",
    )
    results = [
        ToolResult(
            trace_id="t", tool=ToolName.GET_TIME, success=True, message="现在是下午 3 点 25 分。" * 20
        ),
        ToolResult(
            trace_id="t", tool=ToolName.GET_WEATHER, success=True, message="北京今天晴。"
        ),
    ]

    spoken = pipe._default_reply(intent, results)

    assert "北京今天晴。" in spoken, f"the second result was clipped away: {spoken!r}"
    assert len(spoken) <= 80, f"too long to speak comfortably: {len(spoken)} chars"

