"""
The ASK path: questions answered text-in/text-out, never through the agent.

`UNIMPLEMENTED.md` §2.1 measured the failure this pins down: with the 3B model
routing, 「什么是量子力学？」 became `get_weather` (city=量子力学) and 「帮我看看
这个项目里有什么」 became a browser search. Both are questions, and the rule
tier now sends questions to `IntentName.ASK` — which the pipeline answers with
the free-form local model (`LocalLlmBackend.generate`), a path with **no tool
capability**, even when the DSH agent is configured and would otherwise take
every rule miss.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from winvoice.audio.asr import AsrResult
from winvoice.audio import pipeline as pipeline_module
from winvoice.audio.pipeline import AudioPipeline, PipelineContext, PipelineState
from winvoice.contracts import IntentName, IntentResult


class _RecordingTts:
    """Captures the text the assistant would actually speak."""

    def __init__(self) -> None:
        self.spoken: list[str] = []
        self.budgets: list[int] = []

    async def synthesize(self, request):
        self.spoken.append(request.text)
        self.budgets.append(request.max_chars)
        return
        yield  # pragma: no cover - makes this an async generator

    def is_interrupted(self) -> bool:
        return False

    def interrupt(self) -> None:
        pass


class _RecordingAgent:
    """A DSH stand-in that records whether it was consulted."""

    enabled = True

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def route(self, text, trace_id: str = "", tier: str = "full"):
        self.calls.append(text)
        return SimpleNamespace(
            resolved=True, response="代理的回答。", source="local",
            failure=None, escalation_reason=None, assessment=None,
        )


class _RecordingExecutor:
    def __init__(self) -> None:
        self.calls: list = []

    async def execute(self, call, confirmed=False):
        self.calls.append((call, confirmed))
        from winvoice.contracts import ToolResult

        return ToolResult(tool=call.tool, success=True, message="已经完成。")


class _FixedRouter:
    def __init__(self, *intents: IntentResult) -> None:
        self._intents = list(intents)

    async def route(self, text, sv_result=None) -> IntentResult:
        return self._intents.pop(0) if self._intents else self._intents[-1]


class _FakeAsk:
    """The free-form backend, scripted per test."""

    def __init__(self, answer: str | None = None, error: Exception | None = None) -> None:
        self.prompts: list[str] = []
        self.kwargs: list[dict] = []
        self.answer = answer
        self.error = error

    async def generate(self, prompt, *, system=None, max_tokens=256, temperature=0.7, timeout_s=None):
        self.prompts.append(prompt)
        self.kwargs.append(
            {"system": system, "max_tokens": max_tokens, "temperature": temperature, "timeout_s": timeout_s}
        )
        if self.error is not None:
            raise self.error
        return self.answer


def ask_intent(text: str, args: dict | None = None) -> IntentResult:
    return IntentResult(
        trace_id="t",
        intent=IntentName.ASK,
        args=args if args is not None else {"question": text},
        confidence=0.8,
        source="rules",
        raw_text=text,
    )


async def _turn_reply(pipe: AudioPipeline, text: str) -> str:
    pipe.tts = _RecordingTts()
    pipe._context = PipelineContext(trace_id="t")
    await pipe._route_intent(AsrResult(text=text, language="zh", confidence=0.9))
    assert pipe.tts.spoken, "nothing was spoken"
    return pipe.tts.spoken[-1]


# ── deterministic small talk (no model involved) ─────────────


@pytest.mark.asyncio
async def test_a_greeting_is_answered_without_any_model() -> None:
    pipe = AudioPipeline(use_stub=True)
    await pipe.initialize()
    pipe.set_intent_router(_FixedRouter(ask_intent("你好", {"kind": "greet"})))

    spoken = await _turn_reply(pipe, "你好")

    assert spoken == "你好！有什么可以帮你的吗？"
    assert pipe._ask_backend is None, "small talk must not build an LLM client"


@pytest.mark.asyncio
async def test_thanks_and_bye_are_deterministic() -> None:
    pipe = AudioPipeline(use_stub=True)
    await pipe.initialize()
    pipe.set_intent_router(_FixedRouter(ask_intent("谢谢你", {"kind": "thanks"})))
    assert await _turn_reply(pipe, "谢谢你") == "不客气。"


# ── the free-form Q&A path ────────────────────────────────────


@pytest.mark.asyncio
async def test_a_question_is_answered_by_the_free_form_backend() -> None:
    pipe = AudioPipeline(use_stub=True)
    await pipe.initialize()
    backend = _FakeAsk(answer="量子力学是研究微观粒子运动的物理学分支。")
    pipe.set_ask_backend(backend)
    pipe.set_intent_router(_FixedRouter(ask_intent("什么是量子力学")))

    spoken = await _turn_reply(pipe, "什么是量子力学")

    assert backend.prompts == ["什么是量子力学"]
    assert "量子力学" in spoken
    assert pipe.tts.budgets[-1] == 80, "the ask budget, not the agent budget, applies"


@pytest.mark.asyncio
async def test_the_answer_is_sanitised_for_the_chinese_lexicon() -> None:
    pipe = AudioPipeline(use_stub=True)
    await pipe.initialize()
    pipe.set_ask_backend(_FakeAsk(
        answer="量子力学很有用。\n- atom 是英文\n```print(1)``` 结束"
    ))
    pipe.set_intent_router(_FixedRouter(ask_intent("什么是量子力学")))

    spoken = await _turn_reply(pipe, "什么是量子力学")

    assert "量子力学很有用" in spoken
    for silent in ("atom", "```", "print"):
        assert silent not in spoken, spoken


@pytest.mark.asyncio
async def test_an_unspeakable_answer_gets_the_honest_refusal() -> None:
    pipe = AudioPipeline(use_stub=True)
    await pipe.initialize()
    pipe.set_ask_backend(_FakeAsk(answer="Sure, it works fine!"))
    pipe.set_intent_router(_FixedRouter(ask_intent("什么是量子力学")))

    spoken = await _turn_reply(pipe, "什么是量子力学")

    assert "答不上来" in spoken


@pytest.mark.asyncio
async def test_a_backend_failure_is_spoken_not_silent() -> None:
    pipe = AudioPipeline(use_stub=True)
    await pipe.initialize()
    pipe.set_ask_backend(_FakeAsk(error=RuntimeError("llama-server is down")))
    pipe.set_intent_router(_FixedRouter(ask_intent("什么是量子力学")))

    spoken = await _turn_reply(pipe, "什么是量子力学")

    assert "答不上来" in spoken


@pytest.mark.asyncio
async def test_ask_disabled_says_so(monkeypatch) -> None:
    pipe = AudioPipeline(use_stub=True)
    await pipe.initialize()
    pipe.set_ask_backend(_FakeAsk(answer="不会被用到"))
    pipe.set_intent_router(_FixedRouter(ask_intent("什么是量子力学")))
    monkeypatch.setattr(
        pipeline_module, "get_config",
        lambda *a, **k: SimpleNamespace(get=lambda key, default=None: False),
    )

    spoken = await _turn_reply(pipe, "什么是量子力学")

    assert "没有打开" in spoken


# ── the hard constraint: no tool capability for questions ─────


@pytest.mark.asyncio
async def test_ask_never_reaches_the_agent_or_the_tools() -> None:
    pipe = AudioPipeline(use_stub=True)
    await pipe.initialize()
    agent = _RecordingAgent()
    executor = _RecordingExecutor()
    pipe.set_ask_backend(_FakeAsk(answer="量子力学研究微观粒子。"))
    pipe.set_intent_router(_FixedRouter(ask_intent("什么是量子力学")))
    pipe.set_dsh_router(agent)
    pipe.set_tool_executor(executor)

    spoken = await _turn_reply(pipe, "什么是量子力学")

    assert agent.calls == [], "the question reached the agent, which has tool capability"
    assert executor.calls == []
    assert "量子力学" in spoken


# ── 没事了: dismiss and fall back to waiting ─────────────────


@pytest.mark.asyncio
async def test_dismiss_speaks_a_short_ack_and_runs_nothing() -> None:
    pipe = AudioPipeline(use_stub=True)
    await pipe.initialize()
    agent = _RecordingAgent()
    executor = _RecordingExecutor()
    dismiss = IntentResult(
        trace_id="t", intent=IntentName.DISMISS, args={},
        confidence=0.95, source="rules", raw_text="没事了",
    )
    pipe.set_intent_router(_FixedRouter(dismiss))
    pipe.set_dsh_router(agent)
    pipe.set_tool_executor(executor)

    spoken = await _turn_reply(pipe, "没事了")

    assert spoken == "好的。"
    assert agent.calls == [] and executor.calls == [], "a dismissal must not run anything"
    assert pipe.state == PipelineState.KWS_LISTENING, "back to waiting for the wake word"
