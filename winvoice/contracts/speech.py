"""
The spoken-text contract: what the Chinese TTS can actually pronounce.

`spec.md` §6.4. The TTS model is `vits-icefall-zh-aishell3`, whose lexicon holds
**zero Latin entries**: sherpa-onnx drops every English word one by one
(`lexicon.cc: OOV ... Ignore it!`) and the user hears a sentence with holes in
it. Digits are fine — `rule_fsts` expand them.

The rule lives here, in the leaf package both layers already depend on, because
both enforce it: the tool layer guards the strings it authors
(`winvoice/tools/builtin.py`), and the pipeline guards everything that reaches
the speaker (`winvoice/audio/pipeline.py`). One predicate means the two cannot
drift apart into a half-checked contract.
"""

from __future__ import annotations

import re
import string

# `TtsEngine.synthesize` synthesises the whole utterance before it yields its
# first chunk, so everything before the first audio is dead air. A message
# written for the speaker is meant to be one short sentence.
MAX_SPEECH_CHARS = 80


def has_latin(text: str) -> bool:
    """True when the Chinese TTS would drop part of `text` as an OOV token."""
    return any(ch.isascii() and ch.isalpha() for ch in text)


def clip_for_speech(text: str, limit: int = MAX_SPEECH_CHARS) -> str:
    """
    Cut `text` down to something worth waiting for.

    Prefers the last sentence boundary inside `limit`; when the only boundary is
    so early that clipping there would throw the content away, it cuts at the
    limit instead (a truncated clause still beats 80 characters of nothing).
    """
    if len(text) <= limit:
        return text

    head = text[:limit]
    boundary = max(head.rfind(mark) for mark in "。！？；")
    if boundary >= limit // 2:
        return head[: boundary + 1]
    return head


# Everything an answer from a language model routinely contains that the lexicon
# cannot say, or that would be read aloud as punctuation soup.
_MARKDOWN_FENCE = re.compile(r"```.*?```", re.DOTALL)
_MARKDOWN_INLINE = re.compile(r"`([^`]*)`")
_MARKDOWN_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_MARKDOWN_HEADING = re.compile(r"^\s{0,3}#{1,6}\s*", re.MULTILINE)
_MARKDOWN_BULLET = re.compile(r"^\s{0,3}(?:[-*+]|\d+[.)])\s+", re.MULTILINE)
_LATIN_WORD = re.compile(r"[A-Za-z]+(?:['’\-][A-Za-z]+)*")
_WHITESPACE = re.compile(r"\s+")
# Every ASCII punctuation character, from `string.punctuation`. Dropping the set
# wholesale (rather than listing the marks that came to mind) is what makes
# 「Done!」 reduce to nothing instead of to a stray `!` the lexicon cannot say.
_ASCII_PUNCTUATION = re.compile("[" + re.escape(string.punctuation) + "]+")


def sanitize_for_tts(text: str, limit: int = MAX_SPEECH_CHARS) -> str:
    """
    Reduce a model's answer to something the Chinese TTS can actually say.

    Every spoken string used to be authored by a tool, which knew the rules;
    since DeepSeek Harness composes the final reply, that is no longer true. The
    agent is instructed to answer in plain Chinese, and this is the safety net
    for when it does not — a markdown list of file paths read aloud is not a
    degraded answer, it is 80 characters of holes.

    Latin words are dropped rather than transliterated (there is nothing to
    transliterate them *with*), digits survive because `number.fst` expands
    them, and an answer that is nothing but dropped words returns `""` so the
    caller can fall back to an honest generic sentence instead of silence.
    """
    if not text:
        return ""

    cleaned = _MARKDOWN_FENCE.sub(" ", text)
    cleaned = _MARKDOWN_LINK.sub(r"\1", cleaned)
    cleaned = _MARKDOWN_INLINE.sub(r"\1", cleaned)
    cleaned = _MARKDOWN_HEADING.sub("", cleaned)
    cleaned = _MARKDOWN_BULLET.sub("", cleaned)
    cleaned = _LATIN_WORD.sub(" ", cleaned)
    cleaned = _ASCII_PUNCTUATION.sub(" ", cleaned)
    cleaned = _WHITESPACE.sub(" ", cleaned).strip()

    return clip_for_speech(cleaned, limit)


__all__ = ["MAX_SPEECH_CHARS", "clip_for_speech", "has_latin", "sanitize_for_tts"]
