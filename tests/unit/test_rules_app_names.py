"""
§2.6 acceptance: app names survive the rule table.

Two contracts, both from the 2026-10-01 probe:

* an utterance that *begins* by opening a registered app outranks every other
  family — 「打开百度网盘」 was answered by the search tool, 「打开VLC media
  player」 pressed play, 「打开音量控制台」 raised the volume, and
  「打开文件管理器」 reached read_file (which read 「管理器」 as a file);
* name resolution tolerates a slip or a fragment — 「微信电脑版」 and
  「weixin」 and 「记事版」 all reach their app.

The fixture isolates the app table: without it the ambient config (the user's
own entries) would decide the outcome, which is how the old suite rotted.
"""

from __future__ import annotations

import pytest

from winvoice.contracts import IntentName
from winvoice.intent import rules as rules_mod
from winvoice.intent.rules import match_rules
from winvoice.tools import builtin


@pytest.fixture(autouse=True)
def apps(monkeypatch):
    table = {
        "notepad": builtin.BUILTIN_APPS["notepad"],
        "calculator": builtin.BUILTIN_APPS["calculator"],
        "explorer": builtin.BUILTIN_APPS["explorer"],
        "cmd": builtin.BUILTIN_APPS["cmd"],
        "powershell": builtin.BUILTIN_APPS["powershell"],
        "chrome": builtin.BUILTIN_APPS["chrome"],
        "wechat": builtin.AppEntry("wechat", "微信", r"C:\fake\Weixin.exe", image="Weixin.exe"),
        "cloudmusic": builtin.AppEntry("cloudmusic", "网易云音乐", r"C:\fake\cloudmusic.exe"),
        "baidupan": builtin.AppEntry("baidupan", "百度网盘", r"C:\fake\BaiduNetdisk.exe"),
        "vlc": builtin.AppEntry("vlc", "VLC media player", r"C:\fake\vlc.exe"),
        "volctl": builtin.AppEntry("volctl", "音量控制台", r"C:\fake\ctl.exe"),
        "weathertv": builtin.AppEntry("weathertv", "天气预报", r"C:\fake\weather.exe"),
    }
    monkeypatch.setattr(builtin, "current_apps", lambda: dict(table))
    return table


@pytest.mark.parametrize(
    "text,name",
    [
        ("打开百度网盘", "百度网盘"),      # was: search_web, searched 「网盘」
        ("打开文件管理器", "文件管理器"),  # was: read_file, 「没有找到这个文件。」
        ("打开文件资源管理器", "文件资源管理器"),
        ("打开VLC media player", "VLC media player"),  # was: media_control play
        ("打开音量控制台", "音量控制台"),  # was: set_volume, volume +10
        ("打开天气预报", "天气预报"),      # was: get_weather(city=「打开」)
        ("把微信打开", "微信"),            # was: open_app with no name at all
        ("把微信打开吧", "微信"),
        ("把微信打开一下", "微信"),
        ("帮我打开微信", "微信"),
        ("打开一下微信", "微信"),
    ],
)
def test_open_verbs_reach_registered_apps(text: str, name: str) -> None:
    result = match_rules(text)
    assert result is not None, text
    assert result.intent == IntentName.OPEN_APP, (text, result.intent)
    assert result.args["app"] == name


@pytest.mark.parametrize(
    "text,intent",
    [
        ("打开浏览器搜索天气", IntentName.SEARCH_WEB),  # a search is still a search
        ("用浏览器搜索天气", IntentName.SEARCH_WEB),
        ("重新启动电脑", IntentName.SYSTEM_POWER),      # 启动 inside 重新启动
        ("打开文件", IntentName.READ_FILE),             # the deliberate boundary
    ],
)
def test_the_pre_check_does_not_steal(text: str, intent: IntentName) -> None:
    assert match_rules(text).intent == intent


def test_an_unknown_name_falls_through_to_the_normal_order() -> None:
    # 原神 is not on the list — the pre-check steps aside, the table still
    # routes by the verb, and the tool layer delivers the refusal.
    result = match_rules("打开原神")
    assert result is not None and result.intent == IntentName.OPEN_APP
    assert result.args["app"] == "原神"


@pytest.mark.parametrize(
    "spoken,expected",
    [
        ("微信", "wechat"),
        ("weixin", "wechat"),          # the process-image stem
        ("微信电脑版", "wechat"),       # said more than the name
        ("网易云", "cloudmusic"),       # said part of the name
        ("网易云音乐电脑版", "cloudmusic"),
        ("记事版", "notepad"),          # one character off
        ("Notpa", "notepad"),          # the slip from the live log
        ("浏览器", "chrome"),           # built-in synonym
        ("VLC media player", "vlc"),
        ("vlc", "vlc"),
    ],
)
def test_names_survive_a_slip_or_a_fragment(spoken: str, expected: str) -> None:
    assert builtin.resolve_app(spoken) == expected


def test_unknown_names_still_refuse() -> None:
    assert builtin.resolve_app("原神") is None
    assert builtin.resolve_app("完全没听说过的程序") is None
    assert builtin.resolve_app("") is None


def test_strict_resolution_keeps_generic_short_words_unresolved() -> None:
    # The rule pre-check must not map 「文件」→ explorer via 文件夹, or
    # 「打开文件」 stops meaning READ_FILE (`test_intent_rules_qa.py`).
    assert builtin.resolve_app("文件", partial=False) is None
    assert builtin.resolve_app("文件管理器", partial=False) == "explorer"


def test_weather_city_ignores_command_verbs() -> None:
    # 「打开天气预报」 used to ask wttr.in about the city 「打开」.
    assert rules_mod._weather_city("打开天气预报") is None
    assert rules_mod._weather_city("开封天气") == "开封"  # a real place, not a verb


def test_open_app_asks_for_the_name_when_it_is_missing() -> None:
    result = builtin.open_app({})
    assert result["success"] is False
    assert "要打开哪个" in result["message"]


def test_close_app_asks_for_the_name_when_it_is_missing() -> None:
    result = builtin.close_app({})
    assert result["success"] is False
    assert "要关闭哪个" in result["message"]


def test_resolved_but_not_installed_is_reported_honestly() -> None:
    # The fixture's 微信 points at C:\fake — resolution succeeds, the launch
    # target does not exist, and the refusal says so instead of blaming the list.
    result = builtin.open_app({"app": "微信"})
    assert result["success"] is False
    assert "安装位置" in result["message"]
