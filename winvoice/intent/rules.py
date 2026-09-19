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
        r"音量|volume|调大|调小|大声|小声",
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
        # The number may sit anywhere: "音量调大 20", "把音量加 20", "volume up 20".
        num = re.search(r"[+-]?\d+", text)
        args["delta"] = int(num.group(0)) if num else 10
        # "调小/降低/down" implies a negative delta when no sign was given.
        if num and not num.group(0).startswith(("+", "-")):
            if re.search(r"调小|减小|降低|小声|down|lower|decrease", text, re.IGNORECASE):
                args["delta"] = -abs(args["delta"])

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