"""
Power actions: 「关机」 reaches the machine only through the owner's spoken yes.

The gates under test, in the order a request meets them:

* the rule tier must route 「关机/重启/锁屏」 to `system_power` — and must NOT
  let 「怎么关机」 (a question) or 「重新启动」's 启动 (an app verb) steal it;
* the registry refuses a guest outright;
* the executor refuses to run it without `confirmed=True`, which the agent's
  MCP path can never supply — so no model tier can power the machine down;
* the spoken confirmation question names the action (「我将要关机…」).
"""

from __future__ import annotations

import pytest

from winvoice.audio.pipeline import AudioPipeline
from winvoice.contracts import SpeakerTier, ToolCall, ToolName
from winvoice.intent.rules import match_rules
from winvoice.tools import builtin
from winvoice.tools.builtin import system_power
from winvoice.tools.executor import ToolExecutor
from winvoice.tools.registry import ToolRegistry


class _RecordingSubprocess:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple, dict]] = []

    def Popen(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return None


# ── the rule tier ─────────────────────────────────────────────


@pytest.mark.parametrize(
    "utterance,action",
    [
        ("帮我关机", "shutdown"),
        ("重启电脑", "restart"),
        ("重新启动电脑", "restart"),
        ("进入睡眠", "sleep"),
        ("休眠", "hibernate"),
        ("锁屏", "lock"),
        ("锁定屏幕", "lock"),
        ("注销", "signout"),
    ],
)
def test_power_phrases_route_with_their_action(utterance, action):
    intent = match_rules(utterance)

    assert intent is not None and intent.intent.value == "system_power", utterance
    assert intent.args.get("action") == action, f"{utterance!r} -> {intent.args!r}"


@pytest.mark.parametrize(
    "utterance,intent_name",
    [
        # 「怎么关机」 is a question about how, not a command.
        ("怎么关机", "ask"),
        # 「重新启动」 contains 启动 — OPEN_APP must not steal it.
        ("重新启动电脑", "system_power"),
        # 「关闭 X」 is still an app close; SYSTEM_POWER has no 关闭 token.
        ("关闭记事本", "close_app"),
    ],
)
def test_power_phrases_do_not_hijack_or_get_hijacked(utterance, intent_name):
    assert match_rules(utterance).intent.value == intent_name, utterance


# ── the registry: tier and confirmation gates ────────────────


def test_guests_cannot_power_the_machine():
    registry = ToolRegistry()

    error = registry.validate_call(
        ToolName.SYSTEM_POWER, {"action": "shutdown"}, tier=SpeakerTier.GUEST.value
    )

    assert error is not None, "a guest reached the power switch"


def test_the_tool_demands_confirmation():
    registry = ToolRegistry()

    spec = registry.get(ToolName.SYSTEM_POWER)

    assert spec.requires_confirmation is True
    assert spec.guest_allowed is False


# ── the executor: no run without the spoken yes ───────────────


async def test_the_executor_refuses_to_run_without_confirmation():
    executor = ToolExecutor()

    result = await executor.execute(
        ToolCall(tool=ToolName.SYSTEM_POWER, args={"action": "shutdown"})
    )

    assert result.success is False
    assert result.error and result.error.startswith("CONFIRMATION_REQUIRED")


async def test_a_confirmed_shutdown_fires_the_command(monkeypatch):
    fake = _RecordingSubprocess()
    monkeypatch.setattr(builtin.subprocess, "Popen", fake.Popen)
    executor = ToolExecutor()

    result = await executor.execute(
        ToolCall(tool=ToolName.SYSTEM_POWER, args={"action": "shutdown"}),
        confirmed=True,
    )

    assert result.success is True, result
    assert fake.calls, "the shutdown command never ran"
    positional, kwargs = fake.calls[0]
    assert list(positional[0]) == ["shutdown", "/s", "/t", "5"], positional


async def test_an_unknown_action_is_refused_without_running_anything(monkeypatch):
    fake = _RecordingSubprocess()
    monkeypatch.setattr(builtin.subprocess, "Popen", fake.Popen)

    result = system_power({"action": "格式化硬盘"})

    assert result["success"] is False
    assert fake.calls == []
    assert "没听清" in result["message"]


# ── the spoken confirmation question ──────────────────────────


def test_the_confirmation_question_names_the_action():
    pipe = AudioPipeline(use_stub=True)

    question = pipe._confirmation_question(
        ToolCall(tool=ToolName.SYSTEM_POWER, args={"action": "shutdown"})
    )

    assert "关机" in question
    assert "确认请说确认" in question and "取消请说取消" in question
