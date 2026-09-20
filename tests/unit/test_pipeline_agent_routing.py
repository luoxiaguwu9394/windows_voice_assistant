"""
Routing: which tier answers an utterance, and what the user hears when it fails.

With DeepSeek Harness enabled the pipeline has exactly two tiers — the rules,
which answer the ten deterministic commands, and the agent, which answers
everything else. The tests here pin the seam between them, because the failure
modes are quiet ones:

* a rule match must never acquire a model's latency (「音量调大 20」 is the whole
  reason the rule tier exists), and
* an unresolved agent turn must never be spoken as success. `new_way.md` §15 is
  explicit: 「"I couldn't open VS Code because it wasn't found" —
  not "Done."」

The agent is a `RecordingBackend`, so no API key, Node runtime or network is
involved.
"""

from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path

import pytest

from winvoice.audio.pipeline import AudioPipeline, PipelineContext
from winvoice.contracts import IntentName, IntentResult, SpeakerTier, SvResult, ToolName
from winvoice.dsh.backend import DSHTurn, RecordingBackend
from winvoice.dsh.router import DSHRouter
from winvoice.dsh.settings import BridgeSettings, DSHSettings, TierSettings

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


@pytest.fixture
def work():
    base = PROJECT_ROOT / "runtime" / "_pytest_work"
    base.mkdir(parents=True, exist_ok=True)
    directory = base / uuid.uuid4().hex[:12]
    directory.mkdir(parents=True, exist_ok=True)
    try:
        yield directory
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def tool_result_event(success: bool, status: str | None = None, speak: str = "", reason: str = "") -> dict:
    payload = {"success": success}
    if speak:
        payload["speak"] = speak
    if status:
        payload["verification"] = {"status": status, "verified": status == "verified",
                                   "reason": reason, "retryable": False, "checks": []}
    return {"type": "tool/result",
            "data": {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}]}}


def agent_pipeline(work: Path, responses: list[DSHTurn], cloud: bool = False):
    def tier(name: str, enabled: bool) -> TierSettings:
        return TierSettings(name=name, enabled=enabled, dsh_home=work / name, workspace=PROJECT_ROOT)

    settings = DSHSettings(
        enabled=True,
        local=tier("local", True),
        cloud=tier("cloud", cloud),
        bridge=BridgeSettings(bundle_dir=work / "b", utterance_file=work / "u.json"),
        escalation_enabled=cloud,
        max_local_attempts=1,
    )
    backend = RecordingBackend("local", responses)
    router = DSHRouter(settings, local=backend, cloud=RecordingBackend("cloud", []))
    pipeline = AudioPipeline(use_stub=True)
    pipeline.set_dsh_router(router)
    return pipeline, backend


def intent(name: IntentName, source: str = "rules") -> IntentResult:
    return IntentResult(intent=name, args={}, confidence=0.95, source=source, raw_text="x")


# ──────────────────────────────────────────────────────────────
# Tier selection
# ──────────────────────────────────────────────────────────────

def test_a_rule_match_never_reaches_the_agent(work: Path) -> None:
    pipeline, backend = agent_pipeline(work, [DSHTurn(final_response="不该被调用")])

    assert pipeline._routes_to_agent(intent(IntentName.OPEN_APP)) is False


def test_a_rule_miss_is_the_agents(work: Path) -> None:
    pipeline, _ = agent_pipeline(work, [])

    assert pipeline._routes_to_agent(intent(IntentName.UNKNOWN)) is True
    assert pipeline._routes_to_agent(None) is True


def test_without_an_agent_everything_goes_to_the_tools(work: Path) -> None:
    """The pre-DSH behaviour must be exactly preserved when it is switched off."""
    pipeline = AudioPipeline(use_stub=True)

    assert pipeline.dsh_enabled is False
    assert pipeline._routes_to_agent(intent(IntentName.UNKNOWN)) is False
    assert pipeline._routes_to_agent(None) is False


# ──────────────────────────────────────────────────────────────
# The rule path carries the tier too
# ──────────────────────────────────────────────────────────────

def test_a_rule_matched_call_carries_the_speakers_tier() -> None:
    """
    Regression: the tier used to reach the tool layer only on the *agent* path.

    `_intent_to_tool_calls` built a `ToolCall` with no tier, and `ToolCall.tier`
    defaults to `FULL`, so a guest saying 「读取文件 X」 — which the rule layer
    matches directly, without any model involved — was validated as the owner and
    **executed**. The tier has to be on the call because that is what
    `ToolExecutor.execute` validates against.
    """
    pipeline = AudioPipeline(use_stub=True)
    pipeline._context = PipelineContext(trace_id="t-tier")
    pipeline._context.sv_result = SvResult(
        speaker_id="guest", score=0.5, tier=SpeakerTier.GUEST, threshold_high=0.6, threshold_low=0.4
    )

    (call,) = pipeline._intent_to_tool_calls(intent(IntentName.READ_FILE))

    assert call.tool is ToolName.READ_FILE
    assert call.tier is SpeakerTier.GUEST, "a guest's rule-matched call was validated as the owner"


def test_the_rule_path_defaults_to_full_when_nobody_is_identified() -> None:
    """No speaker verification result means no tier restriction — as before."""
    pipeline = AudioPipeline(use_stub=True)
    pipeline._context = PipelineContext(trace_id="t-tier")

    (call,) = pipeline._intent_to_tool_calls(intent(IntentName.GET_TIME))

    assert call.tier is SpeakerTier.FULL


def test_an_unrecognised_tier_does_not_become_full() -> None:
    """
    Fail closed. A tier nobody recognises must not silently promote the caller to
    the owner's permissions — the one direction a permission check must never
    guess in.

    Driven with a stub rather than the contracts' `SvResult`, because that model
    validates the enum at construction: the unvalidated shape is the audio
    engine's own result type, which is what `PipelineContext.sv_result` actually
    holds at runtime (see `_speaker_tier`).
    """
    from types import SimpleNamespace

    pipeline = AudioPipeline(use_stub=True)
    pipeline._context = PipelineContext(trace_id="t-tier")
    pipeline._context.sv_result = SimpleNamespace(tier="mystery")

    (call,) = pipeline._intent_to_tool_calls(intent(IntentName.READ_FILE))

    assert call.tier is SpeakerTier.REJECTED


async def test_a_guest_cannot_read_a_file_through_the_rule_path() -> None:
    """End to end through the executor: the rule path is the one that matters,
    because it runs without any model in the loop."""
    from winvoice.tools.executor import ToolExecutor

    pipeline = AudioPipeline(use_stub=True)
    pipeline._context = PipelineContext(trace_id="t-tier")
    pipeline._context.sv_result = SvResult(
        speaker_id="guest", score=0.5, tier=SpeakerTier.GUEST, threshold_high=0.6, threshold_low=0.4
    )
    executor = ToolExecutor()

    (call,) = pipeline._intent_to_tool_calls(intent(IntentName.READ_FILE))
    result = await executor.execute(call)

    assert result.success is False
    assert "guest" in (result.error or "").lower()


# ──────────────────────────────────────────────────────────────
# What gets spoken
# ──────────────────────────────────────────────────────────────

async def test_agent_answer_is_spoken(work: Path) -> None:
    pipeline, _ = agent_pipeline(
        work, [DSHTurn(final_response="我已经把记事本打开了。", finish_reason="completed")]
    )
    context = PipelineContext(trace_id="t1")

    await pipeline._run_agent("打开记事本", context)

    assert context.tts_text == "我已经把记事本打开了。"


async def test_agent_answer_with_english_is_cleaned_before_speech(work: Path) -> None:
    """
    The agent is told to answer in plain Chinese, but a small local model will
    not always comply, and the TTS drops Latin words one by one — so a raw
    「已经打开 Chrome 了」 would be spoken as a sentence with a hole in it.
    """
    pipeline, _ = agent_pipeline(
        work, [DSHTurn(final_response="已经打开 Chrome 了。", finish_reason="completed")]
    )
    context = PipelineContext(trace_id="t2")

    await pipeline._run_agent("打开浏览器", context)

    assert "Chrome" not in context.tts_text
    assert "已经打开" in context.tts_text


async def test_a_markdown_answer_is_not_read_aloud(work: Path) -> None:
    pipeline, _ = agent_pipeline(
        work,
        [DSHTurn(final_response="完成：\n- 创建 `test.txt`\n- 写入内容\n", finish_reason="completed")],
    )
    context = PipelineContext(trace_id="t3")

    await pipeline._run_agent("创建文件", context)

    assert "`" not in context.tts_text
    assert "test.txt" not in context.tts_text


async def test_unresolved_turn_is_never_spoken_as_success(work: Path) -> None:
    """
    The machine said the app never appeared. Saying 「已经打开了」 here is the
    exact failure `new_way.md` is written to prevent.
    """
    pipeline, _ = agent_pipeline(
        work,
        [
            DSHTurn(
                final_response="已经打开谷歌浏览器了。",
                finish_reason="completed",
                events=[tool_result_event(True, "failed", reason="chrome.exe did not appear")],
            )
        ],
    )
    context = PipelineContext(trace_id="t4")

    await pipeline._run_agent("打开谷歌浏览器", context)

    assert "已经打开" not in context.tts_text
    assert context.tts_text  # but it does say *something*


async def test_failure_falls_back_to_the_tools_own_chinese_sentence(work: Path) -> None:
    """The tool authors speech the TTS can pronounce; prefer it over a generic line."""
    pipeline, _ = agent_pipeline(
        work,
        [
            DSHTurn(
                final_response="已经打开了。",
                finish_reason="completed",
                events=[
                    tool_result_event(True, "failed", reason="not found",
                                      speak="我没找到谷歌浏览器的安装位置。")
                ],
            )
        ],
    )
    context = PipelineContext(trace_id="t5")

    await pipeline._run_agent("打开谷歌浏览器", context)

    assert context.tts_text == "我没找到谷歌浏览器的安装位置。"


async def test_agent_subprocess_failure_is_reported_not_swallowed(work: Path) -> None:
    pipeline, _ = agent_pipeline(work, [DSHTurn(failure="dsh_unavailable: FileNotFoundError")])
    context = PipelineContext(trace_id="t6")

    await pipeline._run_agent("打开记事本", context)

    assert context.tts_text
    assert context.tts_text.isascii() is False  # speakable Chinese, not the error text


async def test_a_raising_agent_does_not_kill_the_pipeline(work: Path) -> None:
    class Exploding:
        name = "boom"
        enabled = True

        async def run(self, prompt, session_id):
            raise RuntimeError("agent exploded")

        async def close(self):
            pass

    def tier(name: str) -> TierSettings:
        return TierSettings(name=name, enabled=True, dsh_home=work / name, workspace=PROJECT_ROOT)

    settings = DSHSettings(
        enabled=True, local=tier("local"), cloud=tier("cloud"),
        bridge=BridgeSettings(utterance_file=work / "u.json"),
    )
    pipeline = AudioPipeline(use_stub=True)
    pipeline.set_dsh_router(DSHRouter(settings, local=Exploding(), cloud=RecordingBackend("cloud", [])))
    context = PipelineContext(trace_id="t7")

    await pipeline._run_agent("随便说点什么", context)

    assert context.tts_text
    assert context.tts_text.isascii() is False


async def test_the_speaker_tier_is_published_for_the_agent(work: Path) -> None:
    """
    The agent runs tools in its own process, so the tier has to travel through
    the published utterance record — if it did not, DSH would be a way *around*
    the permission model rather than a user of it.

    Observed from inside the turn, because the record is cleared once the turn
    ends (a stale `full` is what a guest's next command would otherwise read).
    """
    seen: list[dict] = []
    file_path = work / "u.json"

    class WatchingBackend(RecordingBackend):
        async def run(self, prompt: str, session_id: str) -> DSHTurn:
            seen.append(json.loads(file_path.read_text(encoding="utf-8")))
            return await super().run(prompt, session_id)

    def tier(name: str) -> TierSettings:
        return TierSettings(name=name, enabled=True, dsh_home=work / name, workspace=PROJECT_ROOT)

    settings = DSHSettings(
        enabled=True, local=tier("local"), cloud=tier("cloud"),
        bridge=BridgeSettings(utterance_file=file_path),
        max_local_attempts=1,
    )
    router = DSHRouter(
        settings,
        local=WatchingBackend("local", [DSHTurn(final_response="好的。", finish_reason="completed")]),
        cloud=RecordingBackend("cloud", []),
    )
    pipeline = AudioPipeline(use_stub=True)
    pipeline.set_dsh_router(router)

    context = PipelineContext(trace_id="t8")
    context.sv_result = SvResult(
        speaker_id="guest", score=0.5, tier=SpeakerTier.GUEST, threshold_high=0.6, threshold_low=0.4
    )

    await pipeline._run_agent("读取文件", context)

    assert len(seen) == 1
    assert seen[0]["tier"] == "guest"
    assert seen[0]["trace_id"] == "t8"
    assert not file_path.exists()
