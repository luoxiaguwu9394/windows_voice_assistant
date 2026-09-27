"""
Rule-tier matching for the three new intents: ASK, LIST_DIR, DISMISS.

The interesting cases are the *collisions*: the question vocabulary
(什么/怎么/怎么样) overlaps the topics that must keep winning, the file-listing
vocabulary overlaps the path intents, and the dismissal vocabulary overlaps
everything — 「算了，打开记事本」 must still open Notepad. Each assertion below
pins one of those boundaries.
"""

from __future__ import annotations

import pytest

from winvoice.contracts import IntentName
from winvoice.intent.rules import match_rules


def test_a_definition_question_routes_to_ask() -> None:
    result = match_rules("什么是量子力学")
    assert result is not None and result.intent == IntentName.ASK
    assert result.args["question"] == "什么是量子力学"


def test_a_how_question_routes_to_ask() -> None:
    result = match_rules("为什么天是蓝的")
    assert result is not None and result.intent == IntentName.ASK


def test_the_misrouted_project_question_lands_on_ask() -> None:
    """The measured failure: this used to become a browser search."""
    result = match_rules("帮我看看这个项目里有什么")
    assert result is not None and result.intent == IntentName.ASK


def test_a_greeting_is_deterministic_small_talk() -> None:
    result = match_rules("你好")
    assert result is not None and result.intent == IntentName.ASK
    assert result.args["kind"] == "greet"
    assert "question" not in result.args


def test_thanks_and_bye_are_deterministic_small_talk() -> None:
    assert match_rules("谢谢你").args["kind"] == "thanks"
    assert match_rules("再见").args["kind"] == "bye"


def test_topic_questions_are_not_stolen_by_ask() -> None:
    assert match_rules("今天天气怎么样").intent == IntentName.GET_WEATHER
    assert match_rules("音量怎么调小").intent == IntentName.SET_VOLUME
    assert match_rules("下一首怎么播放").intent == IntentName.MEDIA_CONTROL


def test_list_dir_triggers() -> None:
    for text in ("当前目录下有什么文件", "列出目录", "文件夹里有什么", "看看目录"):
        result = match_rules(text)
        assert result is not None and result.intent == IntentName.LIST_DIR, text


def test_open_file_is_not_stolen_by_list_dir() -> None:
    assert match_rules("打开文件").intent == IntentName.READ_FILE


def test_dismiss_matches_bare_dismissals() -> None:
    for text in ("没事了", "不用了", "算了", "退下"):
        result = match_rules(text)
        assert result is not None and result.intent == IntentName.DISMISS, text


def test_dismiss_does_not_steal_specific_requests() -> None:
    assert match_rules("算了，打开记事本").intent == IntentName.OPEN_APP
    assert match_rules("不用了，帮我查一下天气").intent == IntentName.GET_WEATHER


def test_explicit_search_still_wins_over_ask() -> None:
    """「搜索」 is an explicit verb; 「怎么搜索」 is a question about how."""
    assert match_rules("搜索量子力学").intent == IntentName.SEARCH_WEB


def test_unknown_text_still_falls_through() -> None:
    assert match_rules("完全不相关的随机文本xyz123") is None
