"""
Text normalization: from whatever a model wrote to something the Chinese TTS
can say — **with the pauses still in it**.

Why this exists next to `contracts/speech.py` (which already strips Latin words
and markdown): that guard answers "can the lexicon pronounce this?". It does not
answer "where does the sentence end and how long should the silence be?", and
that second question is the one the user actually hears. So the split is:

| Layer | Owns |
|---|---|
| `contracts/speech.py` | pronounceability: no Latin, no markdown scaffolding, length budget |
| this module | **prosody-preserving** clean-up: unit words, punctuation that survives, no orphan fragments |
| `winvoice/text/segment.py` | where to cut, and how long the pause after each cut is |

Measured on the shipped 8 kHz model (`vits-icefall-zh-aishell3`, see
`scripts/calibrate_tts_pauses.py`): its lexicon has **no punctuation entries at
all**, and sherpa-onnx's `silence_scale` had *no effect whatsoever* (0.2 and 0.0
produced byte-identical audio). Punctuation therefore buys no pause of its own —
which is why the pause has to travel as data (`SpeechSegment.pause_after_ms`)
from the cut point to the playback layer, and why this module's job is to keep
the marks that mean a pause instead of flattening them into nothing.

The order of the passes below is load-bearing:

1. markdown scaffolding out — before anything counts characters;
2. unit words (`℃`, `%`, `km` …) expanded — **before** Latin is stripped, or
   the unit is gone and only the bare number is spoken («25℃» → «25» today);
3. ASCII punctuation → the Chinese marks the splitter reads, with decimals and
   clock times held aside and put back afterwards («3.5», «15:30» must not
   become «3.5» read out as three-five, and they survive the ASCII strip that
   would otherwise delete the separator);
4. the contract's `sanitize_for_tts` guard, **unclipped** — the length budget is
   applied at the very end instead, so clipping can never cut a protected
   number in half;
5. repeated marks collapsed and fragments with nothing pronounceable dropped.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Any

from winvoice.contracts.speech import (
    MAX_SPEECH_CHARS,
    clip_for_speech,
    sanitize_for_tts,
    strip_markdown,
)

# ──────────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SpeechTextConfig:
    """
    Every knob the speech path shares, from the cut point to the speaker.

    `pause_*` are **the** pause table: the model contributes no silence of its
    own, so these numbers are what the listener hears between phrases. The
    defaults come from measurement rather than taste (10 ms-RMS envelope over
    real synthesis): natural intra-sentence pauses in this voice are 40–130 ms,
    so a comma at 140 ms lands inside that band and a full stop at 240 ms sits
    clearly above it.

    `trim_*` are consumed by the TTS engine (`winvoice/audio/tts.py`), which
    trims each segment's own leading/trailing near-silence (measured: 80–130 ms
    in front, 180–250 ms behind — a noise floor, not digital zero) so that what
    the listener hears after a segment is the pause in this table and not that
    plus 0.3–0.4 s of dead air.
    """

    # Length budgets. Tool one-liners keep the contract's 80; an agent's answer
    # gets `reply_max_chars` (see `pipeline._run_agent`).
    max_chars: int = MAX_SPEECH_CHARS
    reply_max_chars: int = 240

    # Cut lengths.
    #   first_chunk_max_chars — the first synthesis unit aims to be this short,
    #     so audio starts early and the rest can be synthesised while it plays;
    #   clause_max_chars — a spoken run between two audible pauses aims to stay
    #     under this, which is what puts a pause at a comma instead of reading
    #     four clauses in one breath;
    #   chunk_max_chars — how far the search for a boundary may reach when the
    #     sentence has none inside `clause_max_chars`;
    #   hard_max_chars — the wall. A run with no punctuation at all is *not* cut
    #     at `chunk_max_chars`: sawing a clause in half costs more in prosody
    #     than it buys in latency, so it is only broken past this ceiling.
    first_chunk_max_chars: int = 16
    clause_max_chars: int = 24
    chunk_max_chars: int = 40
    chunk_min_chars: int = 10
    hard_max_chars: int = 80

    # Silence the playback layer writes after the last segment, before the
    # microphone is re-opened (half-duplex: gives the room time to go quiet).
    tail_silence_ms: int = 150

    # The pause table.
    pause_sentence_ms: int = 240  # 。
    pause_question_ms: int = 280  # ？
    pause_exclaim_ms: int = 260  # ！
    pause_ellipsis_ms: int = 420  # ……
    pause_semicolon_ms: int = 200  # ；
    pause_comma_ms: int = 140  # ，
    pause_enumeration_ms: int = 100  # 、
    pause_colon_ms: int = 180  # ：
    pause_paragraph_ms: int = 420  # 换行
    # A cut we had to make without any punctuation, and a cut placed before a
    # conjunction: short on purpose, so the listener does not hear two sentences
    # where the model wrote one.
    pause_forced_ms: int = 60
    pause_conjunction_ms: int = 90

    # Edge-silence trimming (TTS engine).
    trim_silence: bool = True
    # Above every measured noise floor (Matcha 0.24–1.68 %, the 8 kHz fallback
    # 0.78–1.74 % of peak) and far below the quietest onset (~35 %), so every
    # segment is trimmed by the same rule instead of some being left with their
    # dead air intact.
    trim_ratio: float = 0.02
    trim_guard_ms: int = 30

    # Internal-gap collapse (TTS engine). Matcha scatters 1–7 mid-segment
    # silences of 40–240 ms per generate() — its lexicon holds no punctuation,
    # so it pauses wherever it likes, and holes past ~70 ms are what the
    # listener reports as stuttering. Runs longer than `max_internal_gap_ms`
    # are shortened to `internal_gap_keep_ms`; shorter runs (natural phrasing
    # and stop closures, 30–60 ms) are left alone. 0 disables.
    max_internal_gap_ms: int = 60
    internal_gap_keep_ms: int = 30


DEFAULT_CONFIG = SpeechTextConfig()


# ──────────────────────────────────────────────────────────────
# Passes
# ──────────────────────────────────────────────────────────────

# Control characters are not speakable and would confuse the splitter.
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_FULLWIDTH_DIGITS = str.maketrans("０１２３４５６７８９", "0123456789")

# A unit has to be attached to a number to be a unit at all. The lookbehind is
# what keeps `bookmark` from turning into `book公里ark` — the Latin strip would
# then leave a spoken 「公里」 behind for a word nobody said.
_PERCENT = re.compile(r"(\d+(?:\.\d+)?)\s*%")
_UNIT_AFTER: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?<=\d)\s*(?:°C|℃)"), "摄氏度"),
    (re.compile(r"(?<=\d)\s*km/h", re.IGNORECASE), "公里每小时"),
    (re.compile(r"(?<=\d)\s*m/s", re.IGNORECASE), "米每秒"),
    (re.compile(r"(?<=\d)\s*km(?![a-z])", re.IGNORECASE), "公里"),
    (re.compile(r"(?<=\d)\s*kg(?![a-z])", re.IGNORECASE), "公斤"),
    (re.compile(r"(?<=\d)\s*cm(?![a-z])", re.IGNORECASE), "厘米"),
    (re.compile(r"(?<=\d)\s*mm(?![a-z])", re.IGNORECASE), "毫米"),
    (re.compile(r"(?<=\d)\s*Hz(?![a-z])", re.IGNORECASE), "赫兹"),
)

# Decimals, clock times and version numbers are held aside as a group: `.` and
# `:` inside one are part of the number, not punctuation.
_NUMERIC_GROUP = re.compile(r"\d+(?:[.:：]\d+)+")
# Line breaks are held aside too. `sanitize_for_tts` collapses all whitespace to
# spaces — correct for pronunciation, fatal for prosody: a newline is the only
# signal that the reply started a new paragraph, and the splitter turns it into
# the longest pause in the table. Restored before segmentation.
_NEWLINES = re.compile(r"[ \t]*\r?\n[ \t]*")
_PUNCTUATION_MAP = {
    ",": "，",
    "!": "！",
    "?": "？",
    ";": "；",
    ":": "：",
    "~": "～",
    # Brackets become Chinese pairs so the splitter can see the pair and refuse
    # to cut inside it. Straight quotes carry no pair information and are left
    # to the ASCII strip.
    "(": "（",
    ")": "）",
    "[": "【",
    "]": "】",
}

# Runs of marks collapse to their *last* one: «，。» is a full stop, «！？» is a
# question, and «。。。» is one ellipsis.
_REPEATED_MARKS = re.compile(r"[。！？；，、：…]*([。！？；，、：…])")
_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
# Fragments are split *after* the mark that ends them, so the mark travels with
# the text it belongs to.
_FRAGMENT_SPLIT = re.compile(r"(?<=[。！？；…\n])")
# Marks that may not start or end a spoken line: they are noise without text.
# Leading marks include the sentence-enders («Done!» leaves a bare «！» behind),
# trailing ones do not — a closing 。！？ is what carries the pause.
_LEADING_MARKS = "，、：；…~！？。 \t\u3000"
_TRAILING_MARKS = "，、：；…~ \t\u3000"


def _expand_units(text: str) -> str:
    """Say the unit instead of dropping it («25℃» → «25摄氏度»)."""
    text = _PERCENT.sub(r"百分之\1", text)
    for pattern, replacement in _UNIT_AFTER:
        text = pattern.sub(replacement, text)
    # A unit with no number in front of it still means something.
    return text.replace("%", "百分之").replace("℃", "摄氏度").replace("°C", "摄氏度")


def _protect(text: str) -> tuple[str, list[str]]:
    """
    Hold aside the spans that must survive the pronunciation guard.

    Two kinds, for the same reason: `sanitize_for_tts` is allowed to delete ASCII
    punctuation and to flatten whitespace, and both would destroy something the
    listener hears — a decimal point, and the line break that means "new
    paragraph". Sentinels are indices, so nothing can be split or reordered
    while they are in place, and the length budget is applied only after they
    are restored.
    """
    held: list[str] = []

    def stash(match: re.Match[str]) -> str:
        held.append(match.group(0))
        return f"\x00{len(held) - 1}\x00"

    text = _NEWLINES.sub(stash, text)
    return _NUMERIC_GROUP.sub(stash, text), held


def _restore(text: str, held: list[str]) -> str:
    for index, value in enumerate(held):
        text = text.replace(f"\x00{index}\x00", value)
    return text


def _map_ascii_punctuation(text: str) -> str:
    """
    Turn ASCII punctuation into the Chinese marks the splitter reads.

    `sanitize_for_tts` deliberately *deletes* ASCII punctuation (it exists to
    make text pronounceable, and punctuation soup is not). Deleting is the wrong
    answer here: `,` and `。` are the difference between a comma pause and a full
    stop. So the marks that mean a pause are translated first, and only what is
    left reaches the stripper.
    """
    return "".join(_PUNCTUATION_MAP.get(ch, ch) for ch in text)


def _collapse_repeats(text: str) -> str:
    """«好的。。。» is one full stop; «注意！！！» is one exclamation mark."""
    return _REPEATED_MARKS.sub(r"\1", text)


def _drop_silent_fragments(text: str) -> str:
    """
    Drop the fragments that have nothing left to say.

    After the Latin strip, «Done!» has become «！» — punctuation with no text.
    Speaking it is impossible and keeping it wastes a pause, so a fragment with
    neither a Chinese character nor a digit is removed whole (together with the
    mark that ended it, which belongs to it).

    A fragment made only of whitespace is kept, because that is not noise: it is
    the line break between two paragraphs, and the splitter turns it into the
    longest pause in the table.
    """
    if not text:
        return ""

    kept: list[str] = []
    for fragment in _FRAGMENT_SPLIT.split(text):
        if not fragment:
            continue
        pronounceable = _CJK.search(fragment) or any(ch.isdigit() for ch in fragment)
        # …and a fragment made only of whitespace is not noise either: it is the
        # line break between two paragraphs.
        if pronounceable or not fragment.strip():
            kept.append(fragment)
    return "".join(kept)


def _tidy_edges(text: str) -> str:
    """No line starts on a leftover mark, and none ends on a dangling comma."""
    return text.strip().lstrip(_LEADING_MARKS).rstrip(_TRAILING_MARKS).strip()


def normalize_for_speech(text: str, *, max_chars: int = MAX_SPEECH_CHARS) -> str:
    """
    Clean up text for the speaker without throwing its pauses away.

    Returns `""` when nothing pronounceable survives — the caller is expected to
    say something honest instead of silence (see `pipeline._run_agent`).
    """
    if not text:
        return ""

    cleaned = _CONTROL.sub("", text)
    cleaned = strip_markdown(cleaned)
    cleaned = _expand_units(cleaned)
    cleaned = cleaned.translate(_FULLWIDTH_DIGITS)

    cleaned, held = _protect(cleaned)
    cleaned = _map_ascii_punctuation(cleaned)
    # The contract's guard, deliberately without its length budget: clipping
    # here could cut a protected number in half, so the budget is applied last.
    cleaned = sanitize_for_tts(cleaned, max(len(cleaned), 1))
    cleaned = _restore(cleaned, held)

    cleaned = _collapse_repeats(cleaned)
    cleaned = _drop_silent_fragments(cleaned)
    cleaned = _tidy_edges(cleaned)
    return clip_for_speech(cleaned, max_chars)


# ──────────────────────────────────────────────────────────────
# Config plumbing
# ──────────────────────────────────────────────────────────────


def _read(cfg: Any, key: str, default: Any) -> Any:
    """One `tts.<key>` lookup, tolerant of a missing config object."""
    if cfg is None:
        return default
    value = cfg.get(f"tts.{key}", default)
    return default if value is None else value


def speech_text_config(cfg: Any = None) -> SpeechTextConfig:
    """
    Build the speech config from a `ConfigManager` (duck-typed `.get`).

    Everything here is read once per utterance rather than once per process, so
    editing the pause table or a chunk size applies to the next reply without a
    restart — which is the difference between tuning prosody and guessing at it.
    """
    base = DEFAULT_CONFIG
    if cfg is None:
        from winvoice.config import get_config

        cfg = get_config()

    return replace(
        base,
        reply_max_chars=int(_read(cfg, "reply_max_chars", base.reply_max_chars)),
        first_chunk_max_chars=int(
            _read(cfg, "first_chunk_max_chars", base.first_chunk_max_chars)
        ),
        clause_max_chars=int(_read(cfg, "clause_max_chars", base.clause_max_chars)),
        chunk_max_chars=int(_read(cfg, "chunk_max_chars", base.chunk_max_chars)),
        chunk_min_chars=int(_read(cfg, "chunk_min_chars", base.chunk_min_chars)),
        hard_max_chars=int(_read(cfg, "hard_max_chars", base.hard_max_chars)),
        tail_silence_ms=int(_read(cfg, "tail_silence_ms", base.tail_silence_ms)),
        pause_sentence_ms=int(_read(cfg, "pause_sentence_ms", base.pause_sentence_ms)),
        pause_question_ms=int(_read(cfg, "pause_question_ms", base.pause_question_ms)),
        pause_exclaim_ms=int(_read(cfg, "pause_exclaim_ms", base.pause_exclaim_ms)),
        pause_ellipsis_ms=int(_read(cfg, "pause_ellipsis_ms", base.pause_ellipsis_ms)),
        pause_semicolon_ms=int(_read(cfg, "pause_semicolon_ms", base.pause_semicolon_ms)),
        pause_comma_ms=int(_read(cfg, "pause_comma_ms", base.pause_comma_ms)),
        pause_enumeration_ms=int(
            _read(cfg, "pause_enumeration_ms", base.pause_enumeration_ms)
        ),
        pause_colon_ms=int(_read(cfg, "pause_colon_ms", base.pause_colon_ms)),
        pause_paragraph_ms=int(_read(cfg, "pause_paragraph_ms", base.pause_paragraph_ms)),
        pause_forced_ms=int(_read(cfg, "pause_forced_ms", base.pause_forced_ms)),
        pause_conjunction_ms=int(
            _read(cfg, "pause_conjunction_ms", base.pause_conjunction_ms)
        ),
        trim_silence=bool(_read(cfg, "trim_silence", base.trim_silence)),
        trim_ratio=float(_read(cfg, "trim_ratio", base.trim_ratio)),
        trim_guard_ms=int(_read(cfg, "trim_guard_ms", base.trim_guard_ms)),
        max_internal_gap_ms=int(
            _read(cfg, "max_internal_gap_ms", base.max_internal_gap_ms)
        ),
        internal_gap_keep_ms=int(
            _read(cfg, "internal_gap_keep_ms", base.internal_gap_keep_ms)
        ),
    )


__all__ = [
    "DEFAULT_CONFIG",
    "SpeechTextConfig",
    "normalize_for_speech",
    "speech_text_config",
]
