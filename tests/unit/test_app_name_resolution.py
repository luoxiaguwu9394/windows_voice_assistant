"""
Regression: apps must be resolvable by the name the user actually says.

Reported symptom: 「打开记事本」 was refused outright.

    tool_call  args={'app': '记事本'}
    open_app_rejected  app='记事本' allowed=['notepad', 'calculator', ...]

The whitelist is keyed by English ids, but the rule layer hands over whatever
the user said — Chinese, and after ASR sometimes a mangled English word
(`打开Notpa。`). Nothing ever matched, so *no* app could be opened by voice.

`resolve_app` now maps the spoken name onto an allowlisted id: exact Chinese
label, known alias, or a fuzzy match for ASR slips — and still refuses anything
that is genuinely not on the list.
"""

from __future__ import annotations

import pytest

from winvoice.tools import builtin


@pytest.mark.parametrize(
    "spoken,expected",
    [
        # English id, as the LLM tier tends to emit it.
        ("notepad", "notepad"),
        ("Chrome", "chrome"),
        # The Chinese labels, which is what ASR actually produces.
        ("记事本", "notepad"),
        ("计算器", "calculator"),
        ("资源管理器", "explorer"),
        ("命令提示符", "cmd"),
        ("命令行窗口", "powershell"),
        ("代码编辑器", "vscode"),
        ("谷歌浏览器", "chrome"),
        ("微软浏览器", "edge"),
        ("系统设置", "settings"),
        # Common synonyms.
        ("笔记本", "notepad"),
        ("文件管理器", "explorer"),
        ("我的电脑", "explorer"),
        ("浏览器", "chrome"),
        ("终端", "cmd"),
        ("设置", "settings"),
        # ASR slips seen in the live log: 'Notpa'.
        ("Notpa", "notepad"),
        ("notpad", "notepad"),
        # Trailing particle the rule layer does not strip.
        ("记事本吧", "notepad"),
    ],
)
def test_spoken_names_resolve_to_allowlisted_ids(spoken: str, expected: str) -> None:
    assert builtin.resolve_app(spoken) == expected


@pytest.mark.parametrize("spoken", ["微信", "QQ", "网易云音乐", "画图", "", "随便什么"])
def test_names_outside_the_allowlist_still_resolve_to_nothing(spoken: str) -> None:
    assert builtin.resolve_app(spoken) is None


def test_open_app_takes_the_chinese_name_and_launches_the_right_executable(monkeypatch) -> None:
    launched = []
    monkeypatch.setattr(builtin.subprocess, "Popen", lambda cmd, **kw: launched.append(cmd))

    result = builtin.open_app({"app": "记事本"})

    assert result["success"] is True, result
    assert launched == ["notepad.exe"]


def test_open_app_still_refuses_an_unknown_app() -> None:
    result = builtin.open_app({"app": "微信"})

    assert result["success"] is False
    assert result["message"], "a refusal must say something the user can hear"


def test_close_app_takes_the_chinese_name_too(monkeypatch) -> None:
    killed = []
    monkeypatch.setattr(builtin.subprocess, "run", lambda cmd, **kw: killed.append(cmd))

    result = builtin.close_app({"app": "计算器"})

    assert result["success"] is True, result
    assert killed and killed[0][:3] == ["taskkill", "/f", "/im"]
    assert killed[0][3] == "calc.exe"
