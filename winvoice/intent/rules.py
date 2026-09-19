"""
Rule-based intent matching (Tier 1).

Fast regex/keyword matching for high-frequency commands.
Zero latency, no LLM required.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional

from winvoice.contracts import IntentName, IntentResult

# ──────────────────────────────────────────────────────────────
# Volume direction vocabulary
# ──────────────────────────────────────────────────────────────

# The rule pattern and the sign derivation are built from these same two
# lists, so a word that means "volume" and a word that means "which way"
# cannot drift apart: adding a phrase here teaches both the matcher and the
# sign logic. English alternatives are word-bounded so "support"/"shutdown"
# cannot be read as "up"/"down".
_VOLUME_DOWN = (
    r"调低|调小|降低|压低|减小|变小|弄小|关小|小声|小一点|小一些|小点|轻一点|低一点"
    r"|\b(?:down|lower|decrease|quieter|softer|reduce)\b"
)
_VOLUME_UP = (
    r"调高|调大|提高|增大|变大|大声|大一点|大一些|大点|高一点"
    r"|\b(?:up|raise|increase|louder|higher|boost)\b"
)

DEFAULT_VOLUME_STEP = 10

# "调到/设为 X" asks for an absolute target, never a change *by* X. Scraping
# the digits into a delta is what made 「调到百分之十」 raise the volume: it
# became "+10 points" instead of "go to 10%".
_VOLUME_SET_VERB = r"调到|调至|调成|调整到|调整成|设为|设成|设置到|设置成|变成"

_CN_DIGITS = {
    "零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}


def _chinese_number(token: str) -> Optional[int]:
    """
    Parse 0-100 written in Chinese numerals ("十", "十五", "九十", "一百").

    ASR runs with ITN on, so 「百分之十」 normally arrives as `10%`; this keeps
    the volume command working when ITN is switched off or misses a case.
    """
    if not token or any(ch not in _CN_DIGITS and ch not in "十百" for ch in token):
        return None
    if token in ("百", "一百"):
        return 100
    if "百" in token:
        return None  # "一百二十" is not a volume anyone sets

    if "十" in token:
        head, _, tail = token.partition("十")
        tens = _CN_DIGITS.get(head, 1) if head else 1
        ones = _CN_DIGITS.get(tail, 0) if tail else 0
        return tens * 10 + ones
    return _CN_DIGITS[token] if len(token) == 1 else None


def _volume_target(text: str) -> Optional[int]:
    """Absolute percentage if the utterance says "set the volume to X", else None."""
    patterns = [
        rf"(?:{_VOLUME_SET_VERB})\s*(?:百分之|[%％])?\s*(\d{{1,3}})",
        rf"(?:{_VOLUME_SET_VERB})\s*(?:百分之)?\s*([零一二两三四五六七八九十百]+)",
        r"(\d{1,3})\s*[%％]",  # bare "音量 90%"
        r"百分之\s*([零一二两三四五六七八九十百]+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if not match:
            continue
        token = match.group(1)
        value = int(token) if token.isdigit() else _chinese_number(token)
        if value is not None:
            return max(0, min(100, value))
    return None


def _volume_delta(text: str) -> int:
    """
    Signed volume delta for an utterance.

    The number may sit anywhere ("音量调大 20", "把音量加 20"); when it is
    absent the step is `DEFAULT_VOLUME_STEP`. An explicit sign written as
    "+20"/"-20" always wins. Otherwise the direction words decide, and they
    must be consulted *whether or not* a number was given — an earlier version
    only applied them when a digit was present, so every "lower" phrasing
    without a number silently fell through to +10 and raised the volume.
    """
    num = re.search(r"[+-]?\d+", text)
    magnitude = abs(int(num.group(0))) if num else DEFAULT_VOLUME_STEP

    if num and num.group(0).startswith("-"):
        return -magnitude
    if re.search(_VOLUME_DOWN, text, re.IGNORECASE):
        return -magnitude
    return magnitude

# ──────────────────────────────────────────────────────────────
# Rule Patterns
# ──────────────────────────────────────────────────────────────

# Verbs that explicitly ask for a web search. A sentence containing one of
# these wants the browser whatever else it mentions: 「用浏览器搜索天气」 was
# answered by the weather tool instead, and the city extractor then read the
# four characters in front of 「天气」 — the fragment 「览器搜索」 — as a place
# name. 「用浏览器…」 counts on its own; 「浏览器」 alone does not, because
# 「打开浏览器」 is an app to open, not a search.
#
# The Latin verbs must not be part of a path: a bare match on `search` turned
# 「运行脚本 search.py」 into a browser search for 「py」, and 「读取文件
# C:/google/notes.txt」 into one for 「/notes.txt」.
_LATIN_SEARCH = r"(?:google|bing|search)(?![./\\])"
_EXPLICIT_SEARCH = rf"用浏览器|搜索|搜一下|搜一搜|百度一下|百度|{_LATIN_SEARCH}"

# The weak cousins: 「查一下天气」 is a question this assistant can answer, so
# these only reach SEARCH_WEB when no more specific intent matched.
_WEAK_SEARCH = r"查一下|查询"

# Every way a user can ask for a search, explicit ones first so the extractor
# never leaves a verb fragment in the query.
_SEARCH_VERBS = rf"{_EXPLICIT_SEARCH}|{_WEAK_SEARCH}"

# Patterns are evaluated in dict order and the first hit wins, so the more
# specific intents are listed first. In particular `运行脚本` must be checked
# before OPEN_APP, whose verb list also contains `运行`.
RULE_PATTERNS: Dict[IntentName, List[str]] = {
    # --- most specific first ---
    IntentName.RUN_SCRIPT: [
        r"运行脚本|执行脚本|跑一下脚本|run\s+.*script",
    ],
    IntentName.READ_FILE: [
        r"读取文件|查看文件|打开文件|read\s+.*file",
    ],
    IntentName.WRITE_FILE: [
        r"写入文件|保存文件|创建文件|新建文件|write\s+.*file",
    ],
    # --- then the general command families ---
    IntentName.SET_VOLUME: [
        rf"音量|volume|{_VOLUME_DOWN}|{_VOLUME_UP}|{_VOLUME_SET_VERB}|[%％]|百分之",
    ],
    IntentName.MEDIA_CONTROL: [
        r"播放|暂停|停止播放|下一首|上一首|play|pause|resume|next|previous|prev",
    ],
    # Before SEARCH_WEB so that 「查一下天气」 — a weak verb, a topic we can
    # answer — reaches the weather tool instead of opening a browser. An
    # explicit search is matched ahead of this table entirely.
    IntentName.GET_WEATHER: [
        r"天气|weather",
    ],
    IntentName.GET_TIME: [
        r"几点|什么时间|现在时间|time|clock",
    ],
    IntentName.SEARCH_WEB: [
        _SEARCH_VERBS,
    ],
    IntentName.OPEN_APP: [
        # `运行` alone is deliberately excluded to avoid stealing RUN_SCRIPT.
        r"打开|启动|open|launch|start",
    ],
    IntentName.CLOSE_APP: [
        r"关闭|退出|close|quit|exit",
    ],
}


# Intents whose argument is a literal path or name. They are decided *before*
# the explicit-search check, because their argument may contain a search word
# (`运行脚本 search.py`, `读取文件 我的搜索记录.txt`): reading those as search
# requests would open a browser instead of using the file the user named.
_PATH_ARGUMENT_INTENTS: tuple[IntentName, ...] = (
    IntentName.RUN_SCRIPT,
    IntentName.READ_FILE,
    IntentName.WRITE_FILE,
)


# ──────────────────────────────────────────────────────────────
# Weather city extraction
# ──────────────────────────────────────────────────────────────

# Time and filler words that sit next to 「天气」 but are not places. Stripped
# from both ends of a captured candidate, so 「上海的今天天气」 still yields 上海.
_WEATHER_NOISE = (
    "今天|明天|后天|昨天|现在|目前|最近|早上|上午|中午|下午|晚上|这边|当地"
)
# A candidate *containing* any of these is a verb phrase, not a city name:
# 「查一下天气」 and 「帮我看看天气」 must fall back to the configured city.
# Rejecting is safe — the configured city is the default answer — whereas a
# wrong city silently gives the user a forecast for the wrong place.
_WEATHER_NOT_A_CITY = set("看查问帮我知道想要了解说讲下吗呢么呀的了")


# A city named in a weather question is the run of characters immediately
# before 「天气」 — Chinese (2-4 characters, which covers 北京/牡丹江/乌鲁木齐)
# or Latin for a name ASR left in Latin script (`New York天气`). Latin names
# are matched too because falling back to the configured city there would
# answer about the wrong place without saying so.
_WEATHER_CITY_PATTERN = r"([\u4e00-\u9fa5]{2,4}|[A-Za-z][A-Za-z.'\- ]{0,23}?)\s*天气"


def _weather_city(text: str) -> Optional[str]:
    """
    The city named in a weather question, or None.

    Only the characters immediately before 「天气」 are considered, and only
    when they survive the noise-word and verb-word filters. Everything else
    (「今天天气怎么样」, 「查一下天气」) names no place at all, so `get_weather`
    falls back to `weather.city` from the config.
    """
    match = re.search(_WEATHER_CITY_PATTERN, text)
    if not match:
        return None

    candidate = re.sub(rf"^(?:{_WEATHER_NOISE})+", "", match.group(1))
    candidate = re.sub(rf"(?:{_WEATHER_NOISE})+$", "", candidate).strip(" 的.")
    if not candidate or any(ch in _WEATHER_NOT_A_CITY for ch in candidate):
        return None
    return candidate


# ──────────────────────────────────────────────────────────────
# Matching
# ──────────────────────────────────────────────────────────────

def _rule_result(intent: IntentName, text: str) -> IntentResult:
    """A rule-tier match for `intent`, with its arguments extracted."""
    return IntentResult(
        trace_id="",
        intent=intent,
        args=_extract_args(intent, text),
        confidence=0.95,
        source="rules",
        raw_text=text,
    )


def _matches(intent: IntentName, text_lower: str) -> bool:
    return any(re.search(p, text_lower, re.IGNORECASE) for p in RULE_PATTERNS[intent])


def match_rules(text: str) -> Optional[IntentResult]:
    """
    Match text against rule patterns.

    Three tiers of precedence, because the interesting cases conflict:

    1. intents whose argument is a path (`_PATH_ARGUMENT_INTENTS`) — the path
       may itself contain a search word;
    2. an explicit search request, which names the tool it wants;
    3. everything else, in declaration order (specific topics before the weak
       search verbs — see `RULE_PATTERNS`).

    Returns IntentResult if matched, None otherwise.
    """
    text_lower = text.lower()

    for intent in _PATH_ARGUMENT_INTENTS:
        if _matches(intent, text_lower):
            return _rule_result(intent, text)

    if re.search(_EXPLICIT_SEARCH, text_lower, re.IGNORECASE):
        return _rule_result(IntentName.SEARCH_WEB, text)

    for intent, patterns in RULE_PATTERNS.items():
        if intent in _PATH_ARGUMENT_INTENTS:
            continue  # already decided above
        if any(re.search(pattern, text_lower, re.IGNORECASE) for pattern in patterns):
            return _rule_result(intent, text)
    return None


def _search_query(text: str) -> Optional[str]:
    """
    What to search for: everything after the *last* verb, not the first.

    `after_verb`-style matching takes the leftmost verb, so 「google 搜索天气」
    left 「搜索天气」 as the query — a verb fragment, the same shape as the
    「览器搜索」 that reached the weather provider. Leading verbs are stripped
    here, and a phrase that is nothing but verbs yields None (the tool then
    says it did not catch a query instead of searching for the word "搜索").
    """
    match = re.search(rf"(?:{_SEARCH_VERBS})\s*(.+)", text, re.IGNORECASE)
    if not match:
        return None
    value = re.sub(rf"^(?:(?:{_SEARCH_VERBS})\s*)+", "", match.group(1), flags=re.IGNORECASE)
    value = value.strip(" ，,。.！!？?")
    return value or None


def _extract_args(intent: IntentName, text: str) -> dict:
    """
    Extract arguments for a rule-matched intent.

    Chinese utterances are normally written without spaces (`打开记事本`), so
    every verb/argument boundary uses `\\s*` rather than `\\s+`; otherwise the
    argument would never be captured.
    """
    import re
    args: dict = {}

    def after_verb(patterns: str) -> str | None:
        m = re.search(rf"(?:{patterns})\s*(.+)", text, re.IGNORECASE)
        if not m:
            return None
        value = m.group(1).strip(" ，,。.！!？?")
        return value or None

    if intent == IntentName.OPEN_APP:
        value = after_verb(r"打开|启动|运行|open|launch|start")
        if value:
            args["app"] = value

    elif intent == IntentName.CLOSE_APP:
        value = after_verb(r"关闭|退出|close|quit|exit")
        if value:
            args["app"] = value

    elif intent == IntentName.SET_VOLUME:
        target = _volume_target(text)
        if target is None:
            args["delta"] = _volume_delta(text)
        else:
            args["level"] = target

    elif intent == IntentName.MEDIA_CONTROL:
        # Order matters: check the more specific phrases before the generic ones.
        for action, pattern in [
            ("prev", r"上一首|上一个|previous|prev"),
            ("next", r"下一首|下一个|next"),
            ("pause", r"暂停|停止|pause|stop"),
            ("play", r"播放|继续|play|resume"),
        ]:
            if re.search(pattern, text, re.IGNORECASE):
                args["action"] = action
                break

    elif intent == IntentName.SEARCH_WEB:
        query = _search_query(text)
        if query:
            args["query"] = query

    elif intent == IntentName.GET_WEATHER:
        # No city named → `get_weather` uses `weather.city` from the config.
        city = _weather_city(text)
        if city:
            args["city"] = city

    elif intent == IntentName.READ_FILE:
        # Longest alternative first, so "读取文件 X" yields X, not "文件 X".
        value = after_verb(r"读取文件|查看文件|打开文件|读取|查看|read\s+file|read")
        if value:
            args["path"] = value

    elif intent == IntentName.WRITE_FILE:
        # Writing needs both a path and content; rules cannot infer them
        # reliably, so this intentionally defers to the local LLM tier.
        pass

    elif intent == IntentName.RUN_SCRIPT:
        value = after_verb(r"运行脚本|执行脚本|跑一下脚本|运行|执行|run\s+script|run")
        if value:
            args["path"] = value

    return args