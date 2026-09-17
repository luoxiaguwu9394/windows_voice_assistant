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

RULE_PATTERNS: Dict[IntentName, List[str]] = {
    IntentName.OPEN_APP: [
        r"打开|启动|运行|open|launch|start",
    ],
    IntentName.CLOSE_APP: [
        r"关闭|退出|close|quit|exit",
    ],
    IntentName.SET_VOLUME: [
        r"音量|volume|大声|小声|调节音量",
    ],
    IntentName.MEDIA_CONTROL: [
        r"播放|暂停|下一首|上一首|play|pause|next|previous",
    ],
    IntentName.SEARCH_WEB: [
        r"搜索|查一下|search|google|百度",
    ],
    IntentName.READ_FILE: [
        r"读取|查看文件|read.*file",
    ],
    IntentName.WRITE_FILE: [
        r"写入|保存|创建文件|write.*file",
    ],
    IntentName.RUN_SCRIPT: [
        r"运行脚本|执行脚本|run.*script",
    ],
    IntentName.GET_TIME: [
        r"几点|什么时间|time|now",
    ],
    IntentName.GET_WEATHER: [
        r"天气|weather",
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
    """Extract arguments from text for rule-matched intents."""
    import re
    args = {}

    if intent == IntentName.OPEN_APP:
        match = re.search(r"(?:打开|启动|open|launch)\s+(.+)", text, re.IGNORECASE)
        if match:
            args["app"] = match.group(1).strip()

    elif intent == IntentName.CLOSE_APP:
        match = re.search(r"(?:关闭|退出|close|quit)\s+(.+)", text, re.IGNORECASE)
        if match:
            args["app"] = match.group(1).strip()

    elif intent == IntentName.SET_VOLUME:
        match = re.search(r"(?:音量|volume)\s*([+-]?\d+)", text)
        if match:
            args["delta"] = int(match.group(1))
        else:
            args["delta"] = 10

    elif intent == IntentName.MEDIA_CONTROL:
        for action, pattern in [
            ("play", r"播放|play"),
            ("pause", r"暂停|pause"),
            ("next", r"下一首|next"),
            ("prev", r"上一首|previous|prev"),
        ]:
            if re.search(pattern, text, re.IGNORECASE):
                args["action"] = action
                break

    elif intent == IntentName.SEARCH_WEB:
        match = re.search(r"(?:搜索|查一下|search)\s+(.+)", text, re.IGNORECASE)
        if match:
            args["query"] = match.group(1).strip()

    elif intent == IntentName.READ_FILE:
        match = re.search(r"(?:读取|查看)\s+(.+)", text, re.IGNORECASE)
        if match:
            args["path"] = match.group(1).strip()

    elif intent == IntentName.WRITE_FILE:
        # For rule-based, we can't reliably extract path+content
        # Defer to LLM
        pass

    elif intent == IntentName.RUN_SCRIPT:
        match = re.search(r"(?:运行|执行)\s+(.+)", text, re.IGNORECASE)
        if match:
            args["path"] = match.group(1).strip()

    return args