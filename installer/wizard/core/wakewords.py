"""
Wake-word validation.

The KWS engine compiles the configured keywords into a cache file keyed by
their digest (`winvoice/audio/kws.py`), so *any* change to the list takes
effect on the next start — the wizard only has to write a correct list.

English keywords go through the model's phoneme tokens, Chinese ones through
pinyin; what actually matters for usability is that a word is long enough not
to false-trigger and short enough to say naturally.
"""

from __future__ import annotations

import re
from typing import List, Tuple

MIN_LEN = 2
MAX_LEN = 12
MAX_WORDS = 8

_LATIN = re.compile(r"[A-Za-z]")
_CJK = re.compile(r"[\u4e00-\u9fff]")


def normalize(raw_words: List[str]) -> List[str]:
    """Trim, drop empties and duplicates (order preserved)."""
    seen: set[str] = set()
    out: List[str] = []
    for word in raw_words:
        word = word.strip()
        if not word or word in seen:
            continue
        seen.add(word)
        out.append(word)
    return out


def validate(raw_words: List[str]) -> Tuple[List[str], List[str]]:
    """
    Returns (accepted, warnings). Accepted words meet the length rules and
    contain at least one letter the model can speak (Latin phonemes or CJK
    pinyin tokens) — punctuation-only strings would compile into nothing.
    """
    warnings: List[str] = []
    accepted: List[str] = []
    for word in normalize(raw_words):
        if len(word) < MIN_LEN:
            warnings.append(f"「{word}」太短（至少 {MIN_LEN} 个字符），已忽略")
            continue
        if len(word) > MAX_LEN:
            warnings.append(f"「{word}」太长（最多 {MAX_LEN} 个字符），已忽略")
            continue
        if not _LATIN.search(word) and not _CJK.search(word):
            warnings.append(f"「{word}」不含可朗读的文字，已忽略")
            continue
        accepted.append(word)
    if len(accepted) > MAX_WORDS:
        warnings.append(f"最多 {MAX_WORDS} 个唤醒词，多余的已忽略")
        accepted = accepted[:MAX_WORDS]
    return accepted, warnings
