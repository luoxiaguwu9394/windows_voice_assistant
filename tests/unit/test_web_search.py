"""
Regression: an explicit 「搜索 X」 must open a browser URL that works.

`search_web` pasted the raw query into the URL:

    webbrowser.open(f"https://www.bing.com/search?q={query}")

Chinese is what ASR produces, so the URL carried raw UTF-8 (`?q=天气`). That is
at best browser-dependent and at worst a broken request, and the confirmation
the user hears has to stay Chinese (the TTS lexicon has no Latin entries).

Routing is pinned in `tests/unit/test_weather_speech.py`: an explicit search
verb goes to the browser even when the topic is one the assistant can answer.
Opening the browser *without* asking remains an open item — see
`UNIMPLEMENTED.md` §2.4.
"""

from __future__ import annotations

from winvoice.intent.rules import match_rules
from winvoice.tools import builtin
from winvoice.tools.builtin import search_web


class _OpeningBrowser:
    """Captures the URL instead of opening a window."""

    def __init__(self, succeeds: bool = True) -> None:
        self.urls: list[str] = []
        self._succeeds = succeeds

    def open(self, url: str) -> bool:
        self.urls.append(url)
        return self._succeeds


def _search(query: str, monkeypatch, succeeds: bool = True):
    browser = _OpeningBrowser(succeeds)
    monkeypatch.setattr(builtin, "webbrowser", browser)
    result = search_web({"query": query})
    return result, browser


def test_a_chinese_query_is_percent_encoded(monkeypatch):
    result, browser = _search("今天新闻", monkeypatch)

    assert result["success"] is True, result
    # Spelled out rather than derived from `quote()`, so an equally valid
    # switch to `urlencode` is judged on its output, not on a tautology.
    assert browser.urls == [
        "https://www.bing.com/search?q=%E4%BB%8A%E5%A4%A9%E6%96%B0%E9%97%BB"
    ], browser.urls


def test_an_ascii_query_still_encodes_spaces(monkeypatch):
    result, browser = _search("python tutorial", monkeypatch)

    assert result["success"] is True, result
    assert browser.urls == ["https://www.bing.com/search?q=python%20tutorial"], browser.urls


def test_the_confirmation_names_a_chinese_query_and_stays_speakable(monkeypatch):
    result, _ = _search("今天新闻", monkeypatch)

    assert result["message"] == "已经在浏览器里搜索今天新闻了。", result
    assert not any(c.isascii() and c.isalpha() for c in result["message"]), result["message"]


def test_a_latin_query_is_not_put_into_the_spoken_confirmation(monkeypatch):
    result, browser = _search("python tutorial", monkeypatch)

    assert result["success"] is True, result
    assert result["message"] == "已经打开浏览器了。", result
    assert not any(c.isascii() and c.isalpha() for c in result["message"]), result["message"]


def test_the_routed_query_reaches_the_search_tool():
    """What the rule layer extracts is what the tool opens (no dropped words)."""
    intent = match_rules("用浏览器搜索天气")

    assert intent is not None and intent.intent.value == "search_web"
    assert intent.args == {"query": "天气"}, intent.args


def test_no_browser_is_reported_rather_than_claimed(monkeypatch):
    """`webbrowser.open` returns False when no browser could be launched."""
    result, browser = _search("今天新闻", monkeypatch, succeeds=False)

    assert browser.urls, "the attempt must still be recorded"
    assert result["success"] is False, result
    assert result["message"] == "我没能打开浏览器。"
    assert not any(c.isascii() and c.isalpha() for c in result["message"]), result["message"]
