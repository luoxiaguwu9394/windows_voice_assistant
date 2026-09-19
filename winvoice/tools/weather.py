"""
Weather Query Tool.

The one tool that talks to a network provider, which is why it lives apart from
the desktop handlers in `builtin.py`: its reason to change is the provider and
its data, not the machine. Everything here exists to turn a provider payload
into **one short Chinese sentence**:

  * `wttr.in` is keyless, so there is nothing to configure but the city and a
    deadline (`weather.*` in `config.yaml`);
  * it returns English descriptions even when asked for `lang=zh` (verified
    against Beijing/Lhasa/Sanya/Mohe, 2026-09-19), so the condition is mapped to
    Chinese from the numeric WWO code — an unmapped code drops the description
    rather than speaking English, which the TTS lexicon would swallow
    word by word (`winvoice/contracts/speech.py`);
  * every failure path — no network, a timeout, an unusable payload — comes back
    as a short Chinese sentence: the user is talking to a speaker, so silence and
    a stack trace are equally useless.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Dict, Optional
from urllib.parse import quote

import httpx

from winvoice.config import get_config
from winvoice.contracts import has_latin
from winvoice.logging import get_logger

logger = get_logger(__name__)

# wttr.in needs no API key. `format=j1` returns JSON; `lang=zh` *asks* for
# Chinese descriptions, which the API does not actually deliver (see above).
WEATHER_URL = "https://wttr.in/{city}?format=j1&lang=zh"

# WWO condition codes → Chinese, the full 60-code list published by the API
# (worldweatheronline.com/feed/wwoConditionCodes.txt, checked 2026-09-19).
# A code missing from this table drops the description instead of guessing:
# a wrong condition is worse than a sentence about temperature alone.
WEATHER_CODE_ZH: Dict[str, str] = {
    "113": "晴", "116": "多云", "119": "阴", "122": "阴天",
    "125": "霾", "128": "浮尘", "131": "扬沙", "134": "沙尘暴",
    "137": "沙暴", "140": "强沙尘暴", "143": "薄雾", "146": "烟",
    "149": "烟霾", "152": "雾霾", "155": "严重雾霾", "158": "浮尘", "161": "浮尘",
    "176": "局部有雨", "179": "局部有雪", "182": "局部有雨夹雪", "185": "局部有冻雨",
    "200": "附近有雷阵雨", "227": "吹雪", "230": "暴风雪",
    "248": "雾", "260": "冻雾",
    "263": "局部有毛毛雨", "266": "毛毛雨", "281": "冻毛毛雨", "284": "强冻毛毛雨",
    "293": "局部小雨", "296": "小雨", "299": "间歇中雨",
    "302": "中雨", "305": "间歇大雨", "308": "大雨",
    "311": "小冻雨", "314": "冻雨", "317": "小雨夹雪", "320": "雨夹雪",
    "323": "局部小雪", "326": "小雪", "329": "局部中雪",
    "332": "中雪", "335": "局部大雪", "338": "大雪",
    "350": "冰粒", "353": "小阵雨", "356": "阵雨", "359": "暴雨",
    "362": "小阵雨夹雪", "365": "阵雨夹雪",
    "368": "小阵雪", "371": "阵雪", "374": "小冰粒", "377": "冰粒",
    "386": "局部雷阵雨", "389": "雷阵雨", "392": "局部雷阵雪", "395": "雷阵雪",
}


def _first_item(value: Any) -> Dict[str, Any]:
    """wttr.in wraps almost everything in a single-element list."""
    if isinstance(value, list) and value and isinstance(value[0], dict):
        return value[0]
    return {}


def _int_field(source: Dict[str, Any], key: str) -> Optional[int]:
    """wttr.in sends numbers as strings ('15'); junk must not reach the sentence."""
    try:
        return int(float(str(source.get(key)).strip()))
    except (TypeError, ValueError):
        return None


def _description(condition: Dict[str, Any]) -> str:
    """The condition in Chinese, or '' — never in Latin script."""
    text = _first_item(condition.get("weatherDesc")).get("value")
    spoken = str(text or "").strip()
    if spoken and not has_latin(spoken):
        return spoken
    return WEATHER_CODE_ZH.get(str(condition.get("weatherCode", "")).strip(), "")


def _midday_condition(today: Dict[str, Any]) -> Dict[str, Any]:
    """The forecast's midday entry: a serviceable 'today' when `current` is absent."""
    hourly = today.get("hourly")
    if isinstance(hourly, list) and hourly:
        middle = hourly[len(hourly) // 2]
        if isinstance(middle, dict):
            return middle
    return {}


@dataclass(frozen=True)
class WeatherSummary:
    """
    The four things worth reading out of a wttr.in payload.

    A named type rather than a loose dict: the spoken sentence and the
    machine-readable result are built from the same instance, so they cannot
    disagree about what the weather is.
    """

    condition: str = ""
    temp_c: Optional[int] = None
    high_c: Optional[int] = None
    low_c: Optional[int] = None

    @classmethod
    def from_payload(cls, payload: Dict[str, Any]) -> "WeatherSummary":
        current = _first_item(payload.get("current_condition"))
        today = _first_item(payload.get("weather"))
        return cls(
            condition=_description(current) or _description(_midday_condition(today)),
            temp_c=_int_field(current, "temp_C"),
            high_c=_int_field(today, "maxtempC"),
            low_c=_int_field(today, "mintempC"),
        )

    def is_empty(self) -> bool:
        return (
            not self.condition
            and self.temp_c is None
            and self.high_c is None
            and self.low_c is None
        )

    def as_result_fields(self) -> Dict[str, Any]:
        """The machine-readable half of a `ToolResult`."""
        return {
            "condition": self.condition,
            "temp_c": self.temp_c,
            "high_c": self.high_c,
            "low_c": self.low_c,
        }

    def speech(self, city: str) -> str:
        """
        One short Chinese sentence about today, or '' if there is nothing to say.

        `city` is named only when it can be spoken: a Latin place name would be
        dropped word by word by the lexicon, and calling it 「当地」 would claim
        the answer is about wherever the user is — which is exactly what is not
        known when they named somewhere else. So it is left out instead.
        """
        if self.is_empty():
            return ""

        who = str(city or "").strip()
        if has_latin(who):
            who = ""

        if self.low_c is not None and self.high_c is not None:
            head = f"{who}今天{self.condition}" if self.condition else f"{who}今天"
            if not self.condition:
                sentence = f"{head}气温 {self.low_c} 到 {self.high_c} 度"
            else:
                sentence = f"{head}，气温 {self.low_c} 到 {self.high_c} 度"
            if self.temp_c is not None:
                sentence += f"，现在 {self.temp_c} 度"
        elif self.temp_c is not None:
            head = f"{who}现在{self.condition}" if self.condition else f"{who}现在"
            if not self.condition:
                sentence = f"{head}气温 {self.temp_c} 度"
            else:
                sentence = f"{head}，气温 {self.temp_c} 度"
        else:
            sentence = f"{who}今天{self.condition}"

        return f"{sentence}。"


def format_spoken_weather(city: str, payload: Dict[str, Any]) -> str:
    """The sentence `get_weather` would speak for `payload` ('' when unusable)."""
    return WeatherSummary.from_payload(payload).speech(city)


async def _fetch_weather_json(url: str, timeout_s: float) -> Any:
    """
    One bounded HTTP GET. This is the only async builtin: a blocking call here
    would freeze the audio loop (KWS, VAD and barge-in all run on it).

    `timeout=` bounds connect and read *separately*, so it is not a bound on the
    whole exchange; `get_weather` wraps this call in a deadline for that.
    """
    async with httpx.AsyncClient(timeout=timeout_s) as client:
        response = await client.get(url)
        response.raise_for_status()
        return response.json()


def _float_or(value: Any, default: float) -> float:
    """A config value that must not be able to crash a tool call."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


async def get_weather(args: Dict[str, Any]) -> Dict[str, Any]:
    """
    Answer 「今天天气怎么样」 with one spoken sentence.

    The city comes from the request when the rule layer found one, otherwise
    from `weather.city` in the config.
    """
    cfg = get_config()
    if not cfg.get("weather.enabled", True):
        return {
            "success": False,
            "error": "weather lookup disabled: set weather.enabled=true in config.yaml",
            "message": "天气查询没有打开。",
        }

    city = str(args.get("city") or "").strip() or str(cfg.get("weather.city") or "北京")
    timeout_s = _float_or(cfg.get("weather.timeout_s", 5.0), 5.0)
    url = WEATHER_URL.format(city=quote(city))

    try:
        # A deadline on the whole exchange, not on one of its phases: the user
        # waits in silence until the sentence is ready.
        payload = await asyncio.wait_for(_fetch_weather_json(url, timeout_s), timeout_s)
    except Exception as e:
        logger.warning("weather_lookup_failed", city=city, error=str(e))
        return {
            "success": False,
            "error": f"{type(e).__name__}: {e}",
            "message": "暂时查不到天气。",
        }

    summary = WeatherSummary.from_payload(payload if isinstance(payload, dict) else {})
    spoken = summary.speech(city)
    if not spoken:
        logger.warning("weather_payload_unusable", city=city)
        return {
            "success": False,
            "error": "weather payload carried nothing usable",
            "message": "暂时查不到天气。",
        }

    return {"success": True, "message": spoken, "city": city, **summary.as_result_fields()}
