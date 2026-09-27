"""
Semantic segmentation: where to cut a reply, and how long to pause there.

The problem this solves is audible and specific. The shipped TTS is a Chinese
VITS whose lexicon contains **no punctuation** — measured with
`scripts/calibrate_tts_pauses.py`: sherpa-onnx drops the marks before the model
ever sees them, and its `silence_scale` knob does nothing (0.2 and 0.0 gave
byte-identical audio). Every synthesis call also begins and ends with 80–250 ms
of near-silence (a noise floor, not digital zero). So if the assistant
synthesises "A，B。C" in one call, the pauses the listener hears are whatever the
model happened to emit — none at the commas, and a different amount of dead air
behind every sentence.

The fix is to stop treating a pause as something the acoustic model produces. The
cut points are chosen **semantically**, each one's pause is looked up in the
table in `normalize.SpeechTextConfig`, and the playback layer writes that pause
as real silence frames. Same reply, same rhythm, every time.

Cut priority (this is the semantic part, and the reason a sentence is not cut at
a character count):

1. sentence end — `。！？；……`, and a newline as a paragraph;
2. inside a run that has grown past `clause_max_chars`, the clause mark —
   `，、：` — nearest the target, which is what puts a breath where a comma is
   instead of reading four clauses in one go;
3. before a conjunction or sentence adverb (`但是`, `所以`, `然后` …) — a
   boundary the model was never told about but a reader would place there;
4. only when a run has no marks at all *and* is longer than `hard_max_chars`: an
   arbitrary cut with the shortest pause, so it is not heard as two sentences.

The three kinds are tried in that order, never mixed: **explicit punctuation
always beats an inferred boundary.** Weighting them against each other instead
produced cuts like 「好的明白了，我」｜「马上就去办…」 — a conjunction three
characters past a comma, chosen because it scored better, leaves a stray pronoun
hanging at the end of the first chunk.

The refusals matter as much as the cuts. A cut is only considered when it is not
inside a number (`3.5`, `15:30`), not inside a bracket pair, not right after
`的/地/得` (which would split an attributive from its noun), not right after a
leading modifier (`一`, `很`, `非常` …), and not right before a trailing particle
(`了`, `吗`, `呢` …).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Literal

from winvoice.text.normalize import DEFAULT_CONFIG, SpeechTextConfig

SegmentKind = Literal["sentence", "clause", "forced"]

# ──────────────────────────────────────────────────────────────
# Marks and words the splitter knows about
# ──────────────────────────────────────────────────────────────

SENTENCE_MARKS = "。！？；!?;…"
CLAUSE_MARKS = "，、：,:"
OPENERS = "「『（【《〈“‘"
CLOSERS = "」』）】》〉”’"
PARAGRAPH = "paragraph"

# A reader breaks *before* these, so the splitter may too, even though the text
# carries no mark there. This is what keeps a long sentence sounding like speech
# rather than like a tape splice.
CONJUNCTIONS = (
    "但是",
    "不过",
    "所以",
    "然后",
    "另外",
    "因此",
    "而且",
    "如果",
    "因为",
    "还是",
    "或者",
    "以及",
    "同时",
    "其实",
    "当然",
    "可能",
    "已经",
    "正在",
    "需要",
    "可以",
    "应该",
    "必须",
    "马上",
    "稍后",
    "最后",
    "首先",
)

# Cutting after one of these separates it from the word it modifies.
FORBIDDEN_AFTER = (
    "的",
    "地",
    "得",
    "一",
    "不",
    "很",
    "最",
    "更",
    "挺",
    "非常",
    "所有",
    "这个",
    "那个",
    "这样",
    "那样",
    "多少",
)

# These can never start a chunk: they belong to the word in front of them.
FORBIDDEN_BEFORE = "了吗呢吧啊呀哦嘛的地得"

# The floor used while choosing a cut: a piece is never *created* smaller than
# this. (`chunk_min_chars` is the floor for keeping one — see `_merge_short` —
# and the first chunk is exempt from both, because being short is its job.)
MIN_CHUNK_FLOOR = 6

_NUMBER_RUN = re.compile(r"\d+(?:[.:：．]\d+)*")


@dataclass(frozen=True)
class SpeechSegment:
    """One synthesis unit: the text to say, and the silence that follows it."""

    text: str
    pause_after_ms: int = 0
    kind: SegmentKind = "sentence"

    @property
    def chars(self) -> int:
        return len(self.text)


# ──────────────────────────────────────────────────────────────
# Pause table
# ──────────────────────────────────────────────────────────────

_PAUSE_ATTR = {
    "。": "pause_sentence_ms",
    "！": "pause_exclaim_ms",
    "!": "pause_exclaim_ms",
    "？": "pause_question_ms",
    "?": "pause_question_ms",
    "；": "pause_semicolon_ms",
    ";": "pause_semicolon_ms",
    "…": "pause_ellipsis_ms",
    "，": "pause_comma_ms",
    ",": "pause_comma_ms",
    "、": "pause_enumeration_ms",
    "：": "pause_colon_ms",
    ":": "pause_colon_ms",
    PARAGRAPH: "pause_paragraph_ms",
}


def pause_for_mark(mark: str, config: SpeechTextConfig = DEFAULT_CONFIG) -> int:
    """The pause a mark earns, in milliseconds."""
    attribute = _PAUSE_ATTR.get(mark)
    if attribute is None:
        return config.pause_sentence_ms
    return int(getattr(config, attribute))


# ──────────────────────────────────────────────────────────────
# Sentence splitting
# ──────────────────────────────────────────────────────────────


def _depth_profile(text: str) -> list[int]:
    """
    Bracket depth *before* each index, so a cut can be refused inside a pair.

    An unmatched closer clamps at 0 rather than going negative: one stray 「」 in
    model output must not disable every cut for the rest of the reply.
    """
    depth = [0] * (len(text) + 1)
    current = 0
    for index, ch in enumerate(text):
        depth[index] = current
        if ch in OPENERS:
            current += 1
        elif ch in CLOSERS:
            current = max(0, current - 1)
    depth[len(text)] = current
    return depth


def _protected_ranges(text: str) -> list[tuple[int, int]]:
    """Spans that must survive as one unit — `3.5`, `15:30`, `1.2.3`."""
    return [(m.start(), m.end()) for m in _NUMBER_RUN.finditer(text)]


def _inside(pos: int, ranges: list[tuple[int, int]]) -> bool:
    return any(start < pos < end for start, end in ranges)


def _split_sentences(text: str) -> list[tuple[str, str]]:
    """
    Split into `(sentence, boundary)` pairs.

    The boundary travels with the sentence because it *is* the pause: `。` and
    `？` are both ends, and they are not the same silence. Closers are absorbed
    into the sentence they close, and nothing is split inside a bracket pair —
    a quoted sentence is spoken as one unit rather than chopped at its own full
    stop.
    """
    sentences: list[tuple[str, str]] = []
    start = 0
    index = 0
    length = len(text)
    depth = 0

    while index < length:
        ch = text[index]

        if ch in OPENERS:
            depth += 1
        elif ch in CLOSERS:
            depth = max(0, depth - 1)

        if ch == "\n":
            boundary = PARAGRAPH
            end = index  # the newline itself is not spoken
            resume = index + 1
            depth = 0
        elif depth == 0 and ch in SENTENCE_MARKS:
            # «……» is one ellipsis and «？！» is one exclamation: absorb the run
            # and keep the last mark, which is the one that earns the pause.
            last = index
            while last + 1 < length and text[last + 1] in SENTENCE_MARKS:
                last += 1
            boundary = text[last]
            end = last + 1
            while end < length and text[end] in CLOSERS:
                end += 1
            resume = end
        else:
            index += 1
            continue

        piece = text[start:end].strip()
        if piece:
            sentences.append((piece, boundary))
        elif boundary == PARAGRAPH and sentences and sentences[-1][1] != PARAGRAPH:
            # A line ending in 。 followed by a newline: the sentence mark
            # already closed the sentence, but the newline says "new paragraph",
            # and that is the pause the listener should get. Without this, the
            # empty piece would simply be dropped and a paragraph break would be
            # indistinguishable from a full stop.
            previous, _ = sentences[-1]
            sentences[-1] = (previous, PARAGRAPH)
        # `resume` is always past the character that ended the sentence — a
        # newline ends a sentence *before* itself, so resuming at the newline
        # would scan it forever.
        start = resume
        index = resume

    tail = text[start:].strip()
    if tail:
        sentences.append((tail, ""))
    return sentences


# ──────────────────────────────────────────────────────────────
# Cutting a long sentence
# ──────────────────────────────────────────────────────────────


def _starts_conjunction(text: str, pos: int) -> bool:
    return any(text.startswith(word, pos) for word in CONJUNCTIONS)


def _cut_candidates(text: str, *, allow_gaps: bool) -> list[tuple[int, str]]:
    """
    Positions where `text` may be divided, each with the reason it qualifies.

    The reason is a *kind*, not a score — see `_best_cut` for why the three
    kinds are never traded off against each other. `gap` (an arbitrary character
    boundary) is only offered when the text is over `hard_max_chars` and
    therefore has to be broken somewhere.
    """
    depth = _depth_profile(text)
    protected = _protected_ranges(text)
    candidates: list[tuple[int, str]] = []

    for pos in range(MIN_CHUNK_FLOOR, len(text) - MIN_CHUNK_FLOOR + 1):
        if depth[pos] != 0 or _inside(pos, protected):
            continue
        # A piece must not start on whitespace: the leading space would be
        # stripped, and the reply would lose a character at the seam.
        if text[pos].isspace():
            continue
        # A mark that would *start* the next chunk is worse than no cut at all.
        if text[pos] in SENTENCE_MARKS or text[pos] in CLAUSE_MARKS:
            continue
        if text[pos] in CLOSERS:
            continue
        if any(text[:pos].endswith(word) for word in FORBIDDEN_AFTER):
            continue
        if text[pos] in FORBIDDEN_BEFORE:
            continue

        if text[pos - 1] in CLAUSE_MARKS:
            candidates.append((pos, "clause"))
        elif _starts_conjunction(text, pos):
            candidates.append((pos, "conjunction"))
        elif allow_gaps:
            candidates.append((pos, "gap"))

    return candidates


def _best_cut(text: str, *, target: int, ceiling: int, allow_gaps: bool) -> int | None:
    """
    The best boundary at or before `ceiling`, aiming at `target`.

    `ceiling` decides what is *allowed* (and therefore bounds latency); `target`
    decides what is *preferred*. The two differ for the first chunk on purpose:
    if nothing ends within `first_chunk_max_chars`, the search widens to
    `chunk_max_chars` but still prefers the boundary nearest the first-chunk
    target, because a natural boundary slightly late beats a hard cut on time.

    A clause mark is only ever compared with another clause mark, a conjunction
    with a conjunction: mixing them lets a conjunction outscore an adjacent comma
    and leaves a phrase hanging mid-clause.
    """
    candidates = _cut_candidates(text, allow_gaps=allow_gaps)

    pool: list[tuple[int, str]] = []
    for kind in ("clause", "conjunction", "gap"):
        pool = [item for item in candidates if item[1] == kind and item[0] <= ceiling]
        if pool:
            break
    if not pool:
        return None

    return min(pool, key=lambda item: (abs(item[0] - target), item[0]))[0]


def _pause_at_cut(text: str, pos: int, config: SpeechTextConfig) -> tuple[int, SegmentKind]:
    """The pause (and the reason) for a cut at `pos`."""
    mark = text[pos - 1]
    if mark in CLAUSE_MARKS:
        return pause_for_mark(mark, config), "clause"
    if _starts_conjunction(text, pos):
        # A boundary a reader would place. Short enough that it is not heard as
        # a sentence end.
        return config.pause_conjunction_ms, "clause"
    return config.pause_forced_ms, "forced"


def _split_long(
    sentence: str,
    config: SpeechTextConfig,
    sentence_pause: int,
    first_limit: int,
) -> list[SpeechSegment]:
    """
    Break one over-long sentence into speakable pieces.

    The first piece aims at `first_limit` (audio starts early); every later piece
    aims at `clause_max_chars`, which is what puts the pauses at the commas
    instead of at the sentence's end only.
    """
    pieces: list[SpeechSegment] = []
    rest = sentence

    while True:
        ceiling = first_limit if not pieces else config.clause_max_chars
        if len(rest) <= ceiling:
            break

        # A run with no punctuation at all is only broken past the hard ceiling:
        # sawing a clause in half costs more than it buys.
        over_wall = len(rest) > config.hard_max_chars
        pos = _best_cut(
            rest, target=ceiling, ceiling=ceiling, allow_gaps=over_wall
        )
        if pos is None:
            pos = _best_cut(
                rest,
                target=ceiling,
                ceiling=config.chunk_max_chars,
                allow_gaps=over_wall,
            )

        if pos is None:
            if not over_wall:
                break
            pos = min(ceiling, len(rest) - MIN_CHUNK_FLOOR)
            pause, kind = config.pause_forced_ms, "forced"
        else:
            pause, kind = _pause_at_cut(rest, pos, config)

        head = rest[:pos].strip()
        if not head:
            break
        pieces.append(SpeechSegment(head, pause, kind))
        rest = rest[pos:].lstrip()

    remainder = rest.strip()
    if not remainder:
        if not pieces:
            return [SpeechSegment(sentence, sentence_pause, "sentence")]
        last = pieces[-1]
        pieces[-1] = SpeechSegment(last.text, sentence_pause, "sentence")
        return pieces

    pieces.append(SpeechSegment(remainder, sentence_pause, "sentence"))
    return pieces


def _merge_short(
    segments: list[SpeechSegment], config: SpeechTextConfig
) -> list[SpeechSegment]:
    """
    Fold tiny pieces back into their neighbour — with two exceptions.

    1. Merging never crosses a sentence end (`prev.kind != "sentence"`): that
       would throw away the one pause the listener definitely expects.
    2. A first chunk that is *itself* below `chunk_min_chars` is never merged
       into: it is short so that audio starts while the rest of the reply is
       still being synthesised, and a merge would undo exactly that. Once the
       first chunk has reached `chunk_min_chars` the protection lifts, because
       the merge is then the thing that keeps a small tail from becoming a
       synthesis call of its own.

    The size ceiling for a merge is `hard_max_chars` rather than
    `chunk_max_chars`, so a long piece followed by a two-character tail becomes
    one chunk instead of a 40-character chunk plus an orphan.
    """
    if config.chunk_min_chars <= 0:
        return list(segments)

    merged: list[SpeechSegment] = []
    for segment in segments:
        if merged:
            previous = merged[-1]
            first_chunk_at_risk = (
                len(merged) == 1
                and previous.kind != "sentence"
                and len(previous.text) < config.chunk_min_chars
            )
            tiny = (
                len(segment.text) < config.chunk_min_chars
                or len(previous.text) < config.chunk_min_chars
            )
            same_sentence = previous.kind != "sentence"
            fits = len(previous.text) + len(segment.text) <= config.hard_max_chars
            if tiny and same_sentence and fits and not first_chunk_at_risk:
                merged[-1] = SpeechSegment(
                    previous.text + segment.text,
                    segment.pause_after_ms,
                    segment.kind,
                )
                continue
        merged.append(segment)
    return merged


def segment_for_speech(
    text: str, config: SpeechTextConfig = DEFAULT_CONFIG
) -> list[SpeechSegment]:
    """
    Turn one reply into the synthesis units the speaker will say in order.

    The last segment carries `tail_silence_ms` instead of its own boundary
    pause: nothing follows it, and the listener needs the room to answer.
    """
    if not text or not text.strip():
        return []

    sentences = _split_sentences(text)
    if not sentences:
        return []

    segments: list[SpeechSegment] = []
    first = True
    for sentence, boundary in sentences:
        pause = pause_for_mark(boundary, config) if boundary else config.pause_sentence_ms
        limit = config.first_chunk_max_chars if first else config.clause_max_chars
        if len(sentence) <= max(limit, MIN_CHUNK_FLOOR):
            segments.append(SpeechSegment(sentence, pause, "sentence"))
        else:
            segments.extend(_split_long(sentence, config, pause, limit))
        first = False

    segments = _merge_short(segments, config)
    if segments:
        segments[-1] = replace(segments[-1], pause_after_ms=config.tail_silence_ms)
    return segments


__all__ = [
    "CLAUSE_MARKS",
    "CLOSERS",
    "CONJUNCTIONS",
    "OPENERS",
    "SENTENCE_MARKS",
    "SegmentKind",
    "SpeechSegment",
    "pause_for_mark",
    "segment_for_speech",
]
