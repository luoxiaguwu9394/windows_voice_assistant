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


__all__ = ["MAX_SPEECH_CHARS", "clip_for_speech", "has_latin"]
