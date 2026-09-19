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
    IntentName.SEARCH_WEB: [
        r"搜索|查一下|搜一下|search|google|百度",
    ],
    IntentName.GET_WEATHER: [
        r"天气|weather",
    ],
    IntentName.GET_TIME: [
        r"几点|什么时间|现在时间|time|clock",
    ],
    IntentName.OPEN_APP: [
        # `运行` alone is deliberately excluded to avoid stealing RUN_SCRIPT.
        r"打开|启动|open|launch|start",
    ],
    IntentName.CLOSE_APP: [
        r"关闭|退出|close|quit|exit",
    ],
}


# ──────────────────────────────────────────────────────────────
# Matching
# ──────────────────────────────────────────────────────────────

def match_rules(text: str) -> Optional[IntentResult]:
    """
    Match text against rule patterns.

    Returns IntentResult if matched, None otherwise.
    """
    text_lower = text.lower()

    for intent, patterns in RULE_PATTERNS.items():
        for pattern in patterns:
            if re.search(pattern, text_lower, re.IGNORECASE):
                args = _extract_args(intent, text)
                return IntentResult(
                    trace_id="",
                    intent=intent,
                    args=args,
                    confidence=0.95,
                    source="rules",
                    raw_text=text,
                )
    return None


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
        value = after_verb(r"搜索|搜一下|查一下|查询|search|google|百度")
        if value:
            args["query"] = value

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