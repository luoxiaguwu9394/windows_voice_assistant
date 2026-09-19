"""
Regression: 「今天天气怎么样」 must be answered, offline or online.

`get_weather` was routed but had no tool, so every weather request ended in
「抱歉，这个请求我还没有实现。」 The interesting parts of adding it are not the
HTTP call but the two contracts around it:

  * **the spoken sentence is Chinese** — the TTS lexicon has no Latin entries
    (§0 of UNIMPLEMENTED.md), and `wttr.in` happily returns `"Sunny"` even when
    it was asked for `lang=zh`, so the description needs a guard and a fallback
    table keyed by the numeric weather code;
  * **failure is spoken, never silent** — no network, a timeout or an
    unparseable payload all have to come back as a short Chinese sentence;
  * **it must not block the event loop** — the handler is `async` and bounded
    by `weather.timeout_s` (≤ 5 s).
"""

from __future__ import annotations

import asyncio
import time
from urllib.parse import quote

import httpx
import pytest

from winvoice.intent.rules import match_rules
from winvoice.tools import weather
from winvoice.tools.weather import format_spoken_weather, get_weather

# ── wttr.in `format=j1` payloads (trimmed to the fields that are read) ──────

FULL_PAYLOAD = {
    "current_condition": [
        {"temp_C": "15", "weatherCode": "113", "weatherDesc": [{"value": "晴"}]}
    ],
    "weather": [
        {
            "date": "2026-09-19",
            "maxtempC": "20",
            "mintempC": "10",
            "hourly": [{"weatherCode": "113", "weatherDesc": [{"value": "晴"}]}],
        }
    ],
    "nearest_area": [{"areaName": [{"value": "Beijing"}]}],
}

# `lang=zh` is a request, not a guarantee: this is the shape seen when the
# translation is missing.
ENGLISH_DESC_PAYLOAD = {
    "current_condition": [
        {"temp_C": "15", "weatherCode": "116", "weatherDesc": [{"value": "Partly cloudy"}]}
    ],
    "weather": [{"maxtempC": "20", "mintempC": "10"}],
}


class _FakeConfig:
    """Stand-in for ConfigManager: `get_weather` only ever calls `.get()`."""

    def __init__(self, values: dict) -> None:
        self._values = values

    def get(self, key: str, default=None):
        return self._values.get(key, default)


DEFAULT_CONFIG = {"weather.enabled": True, "weather.city": "北京", "weather.timeout_s": 5.0}


@pytest.fixture
def spoken_payload(monkeypatch):
    """Capture the request the handler builds, then replay a fixed payload."""
    captured: dict = {}

    async def fake_fetch(url: str, timeout_s: float) -> dict:
        captured["url"] = url
        captured["timeout_s"] = timeout_s
        payload = captured.get("payload")
        if payload is None:
            raise AssertionError("test did not set a payload")
        return payload

    monkeypatch.setattr(weather, "_fetch_weather_json", fake_fetch)
    return captured


# ── the spoken sentence ────────────────────────────────────────────────────

def test_one_sentence_with_todays_range_and_the_current_temperature():
    assert format_spoken_weather("北京", FULL_PAYLOAD) == "北京今天晴，气温 10 到 20 度，现在 15 度。"


def test_an_english_description_is_replaced_by_the_code_table():
    """`Partly cloudy` would be dropped word by word by the Chinese lexicon."""
    spoken = format_spoken_weather("北京", ENGLISH_DESC_PAYLOAD)
    assert spoken == "北京今天多云，气温 10 到 20 度，现在 15 度。"
    assert not any(ch.isascii() and ch.isalpha() for ch in spoken), spoken


def test_an_unknown_code_drops_the_description_instead_of_saying_it_in_english():
    payload = {
        "current_condition": [
            {"temp_C": "15", "weatherCode": "999", "weatherDesc": [{"value": "Mystery"}]}
        ],
        "weather": [{"maxtempC": "20", "mintempC": "10"}],
    }
    assert format_spoken_weather("北京", payload) == "北京今天气温 10 到 20 度，现在 15 度。"


def test_the_haze_code_that_wttr_in_actually_returned_is_translated():
    """
    Live check (2026-09-19): Beijing came back as code 149, `Smoky haze`.

    `lang=zh` does not translate this payload at all — `weatherDesc` and
    `lang_zh` are both English — so the table is what stands between the user
    and 「北京今天气温 25 度」 with the sky silently missing.
    """
    payload = {
        "current_condition": [
            {"temp_C": "25", "weatherCode": "149", "weatherDesc": [{"value": "Smoky haze"}]}
        ],
        "weather": [{"maxtempC": "29", "mintempC": "18"}],
    }

    spoken = format_spoken_weather("北京", payload)

    assert spoken == "北京今天烟霾，气温 18 到 29 度，现在 25 度。", spoken


def test_without_a_daily_forecast_it_reports_only_the_current_condition():
    payload = {"current_condition": [{"temp_C": "15", "weatherDesc": [{"value": "晴"}]}]}
    assert format_spoken_weather("北京", payload) == "北京现在晴，气温 15 度。"


def test_without_temperatures_it_still_describes_the_sky():
    payload = {"current_condition": [{"weatherDesc": [{"value": "小雨"}]}]}
    assert format_spoken_weather("北京", payload) == "北京今天小雨。"


def test_a_latin_city_name_is_not_pasted_into_the_sentence():
    """
    A Latin place name is dropped rather than mispronounced or faked.

    Not 「当地」 either: the query went to the city the user named, so claiming
    the answer is about wherever they are would be a lie.
    """
    spoken = format_spoken_weather("Beijing", FULL_PAYLOAD)
    assert spoken == "今天晴，气温 10 到 20 度，现在 15 度。"
    assert not any(ch.isascii() and ch.isalpha() for ch in spoken), spoken


@pytest.mark.parametrize("payload", [{}, {"current_condition": [], "weather": []}, {"weather": [{}]}])
def test_an_unusable_payload_produces_nothing_to_say(payload):
    assert format_spoken_weather("北京", payload) == ""


# ── the handler ────────────────────────────────────────────────────────────

async def test_online_lookup_speaks_one_short_sentence(monkeypatch, spoken_payload):
    monkeypatch.setattr(weather, "get_config", lambda: _FakeConfig(DEFAULT_CONFIG))
    spoken_payload["payload"] = FULL_PAYLOAD

    result = await get_weather({})

    assert result["success"] is True, result
    assert result["message"] == "北京今天晴，气温 10 到 20 度，现在 15 度。"
    assert quote("北京") in spoken_payload["url"]
    assert "wttr.in" in spoken_payload["url"]


async def test_the_named_city_wins_over_the_configured_one(monkeypatch, spoken_payload):
    monkeypatch.setattr(weather, "get_config", lambda: _FakeConfig(DEFAULT_CONFIG))
    spoken_payload["payload"] = FULL_PAYLOAD

    result = await get_weather({"city": "上海"})

    assert result["message"].startswith("上海"), result
    assert quote("上海") in spoken_payload["url"]


async def test_the_request_is_bounded_by_the_configured_timeout(monkeypatch, spoken_payload):
    """The TTS answer has to arrive promptly; a hanging request is worse than none."""
    monkeypatch.setattr(weather, "get_config", lambda: _FakeConfig(DEFAULT_CONFIG))
    spoken_payload["payload"] = FULL_PAYLOAD

    await get_weather({})

    assert spoken_payload["timeout_s"] <= 5.0, "the documented ceiling is 5 s"


async def test_a_lookup_that_never_answers_gives_up(monkeypatch):
    """
    `httpx`'s `timeout` bounds connect and read *separately*, so it alone does
    not cap the exchange: a host that connects quickly and then dribbles bytes
    would hold the answer open. `get_weather` must impose a deadline of its own
    — the user hears nothing at all until the sentence is ready.
    """
    monkeypatch.setattr(
        weather,
        "get_config",
        lambda: _FakeConfig({**DEFAULT_CONFIG, "weather.timeout_s": 0.1}),
    )

    async def never_answers(url: str, timeout_s: float) -> dict:
        await asyncio.sleep(30)
        raise AssertionError("the deadline should have fired")

    monkeypatch.setattr(weather, "_fetch_weather_json", never_answers)

    started = time.monotonic()
    result = await get_weather({})
    elapsed = time.monotonic() - started

    assert result["success"] is False
    assert result["message"] == "暂时查不到天气。"
    assert elapsed < 2, f"the deadline did not fire: waited {elapsed:.1f}s"


async def test_offline_says_so_in_chinese(monkeypatch):
    monkeypatch.setattr(weather, "get_config", lambda: _FakeConfig(DEFAULT_CONFIG))

    async def offline(url: str, timeout_s: float) -> dict:
        raise OSError("getaddrinfo failed")

    monkeypatch.setattr(weather, "_fetch_weather_json", offline)

    result = await get_weather({})

    assert result["success"] is False
    assert result["message"] == "暂时查不到天气。"
    assert result["error"], "the machine-readable reason must survive for the log"


async def test_a_broken_payload_is_a_spoken_failure_not_a_crash(monkeypatch):
    monkeypatch.setattr(weather, "get_config", lambda: _FakeConfig(DEFAULT_CONFIG))

    async def garbage(url: str, timeout_s: float) -> dict:
        return {"unexpected": True}

    monkeypatch.setattr(weather, "_fetch_weather_json", garbage)

    result = await get_weather({})

    assert result["success"] is False
    assert result["message"] == "暂时查不到天气。"


async def test_a_disabled_weather_section_says_so_without_any_request(monkeypatch):
    monkeypatch.setattr(
        weather, "get_config", lambda: _FakeConfig({**DEFAULT_CONFIG, "weather.enabled": False})
    )

    async def must_not_be_called(url: str, timeout_s: float) -> dict:
        raise AssertionError("the network must not be touched when weather is disabled")

    monkeypatch.setattr(weather, "_fetch_weather_json", must_not_be_called)

    result = await get_weather({})

    assert result["success"] is False
    assert "打开" in result["message"], result


# ── the rule layer ─────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "utterance,city",
    [
        ("今天天气怎么样", None),          # 今天 is not a city
        ("明天天气", None),
        ("天气怎么样", None),
        ("查一下天气", None),              # 查一下 is a verb, not a place
        ("帮我看看天气", None),
        ("北京的天气", "北京"),
        ("北京天气", "北京"),
        ("上海天气怎么样", "上海"),
        ("上海明天天气", "上海"),          # the trailing time word is stripped
        ("牡丹江天气", "牡丹江"),
        ("乌鲁木齐天气", "乌鲁木齐"),
        # A Latin name must be used, not dropped: falling back to the configured
        # city would answer about the wrong place without saying so.
        ("New York天气", "New York"),
        ("查一下Shanghai天气", "Shanghai"),
    ],
)
def test_the_city_is_taken_from_the_utterance_only_when_it_is_one(utterance, city):
    intent = match_rules(utterance)
    assert intent is not None and intent.intent.value == "get_weather", utterance
    assert intent.args.get("city") == city, f"{utterance!r} -> {intent.args!r}"


@pytest.mark.parametrize(
    "utterance,query",
    [
        ("用浏览器搜索天气", "天气"),
        ("搜索今天新闻", "今天新闻"),
        ("搜一下天气", "天气"),
        ("百度一下明天天气", "明天天气"),
        ("google 天气", "天气"),
        # The leftmost verb wins the match, so the *later* verb must not stay
        # in the query — 「搜索天气」 was a verb fragment, not a search term.
        ("google 搜索天气", "天气"),
        # 「用浏览器查天气」 is deliberately *not* here: the weak stem 查 is
        # indistinguishable from the first character of a name (查尔斯顿), so
        # the query keeps it. Searching for 「查天气」 finds the weather anyway.
    ],
)
def test_an_explicit_search_goes_to_the_browser_not_the_weather_tool(utterance, query):
    """
    Live report: 「用浏览器搜索天气」 was answered by the weather tool.

    The city extractor then read the four characters before 「天气」 — the
    fragment 「览器搜索」 — as a place name and sent it to the provider. An
    explicit search verb has to outrank the topic, because that is what it asks
    for: the browser, not an answer.
    """
    intent = match_rules(utterance)

    assert intent is not None
    assert intent.intent.value == "search_web", f"{utterance!r} -> {intent.intent.value}"
    assert intent.args.get("query") == query, f"{utterance!r} -> {intent.args!r}"


@pytest.mark.parametrize(
    "utterance,intent_name,args",
    [
        # A search word *inside a path* is part of the file name, not a request
        # to open a browser: both of these used to be stolen by the explicit
        # search check and answered with a verb fragment.
        ("运行脚本 search.py", "run_script", {"path": "search.py"}),
        ("读取文件 C:/google/notes.txt", "read_file", {"path": "C:/google/notes.txt"}),
        ("查看文件 我的搜索记录.txt", "read_file", {"path": "我的搜索记录.txt"}),
        ("打开浏览器", "open_app", {"app": "浏览器"}),
        ("退出浏览器", "close_app", {"app": "浏览器"}),
        # 「用浏览器」 asks for a search even with a weak verb, but the query
        # keeps the bare 查 — see the note in the search table above.
        ("用浏览器查天气", "search_web", {"query": "查天气"}),
    ],
)
def test_a_search_word_in_an_argument_does_not_hijack_the_intent(utterance, intent_name, args):
    intent = match_rules(utterance)

    assert intent is not None, utterance
    assert intent.intent.value == intent_name, f"{utterance!r} -> {intent.intent.value}"
    assert intent.args == args, f"{utterance!r} -> {intent.args!r}"


@pytest.mark.parametrize(
    "utterance",
    ["查一下天气", "今天天气怎么样", "天气如何", "北京的天气"],
)
def test_a_weak_query_verb_still_reaches_the_weather_tool(utterance):
    """「查一下」 may be a query or a command; the topic decides, so no browser."""
    intent = match_rules(utterance)

    assert intent is not None and intent.intent.value == "get_weather", utterance


# ── a city the provider cannot place ───────────────────────────────────────

async def test_a_city_the_provider_cannot_place_falls_back_to_the_configured_one(
    monkeypatch,
):
    """
    Live report, second half: the fragment 「览器搜索」 made wttr.in answer 500.

    A token that is not a place (a mistranscribed fragment, 「外面」) should not
    cost the user their answer — the configured city is what the same question
    without a city token would have used.
    """
    monkeypatch.setattr(weather, "get_config", lambda: _FakeConfig(DEFAULT_CONFIG))

    async def fake_fetch(url: str, timeout_s: float) -> dict:
        captured_urls.append(url)
        if quote("北京") in url:
            return FULL_PAYLOAD
        request = httpx.Request("GET", url)
        raise httpx.HTTPStatusError("500", request=request, response=httpx.Response(500, request=request))

    captured_urls: list[str] = []
    monkeypatch.setattr(weather, "_fetch_weather_json", fake_fetch)

    result = await get_weather({"city": "览器搜索"})

    assert result["success"] is True, result
    assert result["message"] == "北京今天晴，气温 10 到 20 度，现在 15 度。"
    assert len(captured_urls) == 2, captured_urls


async def test_a_network_failure_never_substitutes_another_city(monkeypatch):
    """Being offline is not a reason to answer about a different place."""
    monkeypatch.setattr(weather, "get_config", lambda: _FakeConfig(DEFAULT_CONFIG))
    urls: list[str] = []

    async def offline(url: str, timeout_s: float) -> dict:
        urls.append(url)
        raise OSError("getaddrinfo failed")

    monkeypatch.setattr(weather, "_fetch_weather_json", offline)

    result = await get_weather({"city": "上海"})

    assert result["success"] is False
    assert result["message"] == "暂时查不到天气。"
    assert len(urls) == 1, f"a transport failure must not be retried elsewhere: {urls}"

