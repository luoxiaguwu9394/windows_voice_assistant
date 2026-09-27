"""
Segmentation and pauses: what is said, where it is cut, and how long the silence.

This is the part of the speech path the listener actually notices. The TTS model
offers no help with it — measured (`scripts/calibrate_tts_pauses.py`):

* its lexicon has **no punctuation entries**, and sherpa-onnx's `silence_scale`
  changed nothing at all (0.2 and 0.0 were byte-identical), so a comma in the
  text buys no comma pause;
* every synthesis call begins and ends with 80–250 ms of near-silence (a noise
  floor, not digital zero), so naively concatenated sentences carry 0.3–0.4 s of
  dead air each.

Everything the listener hears as rhythm is therefore decided **here**, in two
pure functions, and only reproduced downstream. These tests pin the decisions:

* units are spoken («25℃» → «25摄氏度»), decimals and clock times survive;
* the marks that mean a pause are kept, the ones that mean nothing are not;
* a cut lands on a semantic boundary, never inside a number, a bracket pair, an
  attributive (`的`) or a trailing particle (`了`);
* a sentence with no punctuation is *not* sawn in half just to hit a character
  count;
* the pause after each cut is the table's number, not a guess.
"""

from __future__ import annotations

from winvoice.text import (
    DEFAULT_CONFIG,
    SpeechTextConfig,
    normalize_for_speech,
    segment_for_speech,
    speech_text_config,
)
from winvoice.text.normalize import MAX_SPEECH_CHARS


def joined(segments) -> str:
    return "".join(segment.text for segment in segments)


def squeeze(text: str) -> str:
    """Text without whitespace, for comparing a reply with its segmentation."""
    return text.replace(" ", "").replace("\u3000", "")


# ──────────────────────────────────────────────────────────────
# Normalization
# ──────────────────────────────────────────────────────────────


def test_plain_chinese_is_untouched() -> None:
    assert normalize_for_speech("我已经把记事本打开了。") == "我已经把记事本打开了。"


def test_markdown_fences_are_dropped_with_their_contents() -> None:
    cleaned = normalize_for_speech("结果如下：\n```python\nprint('hi')\n```\n完成了。")

    assert "print" not in cleaned
    assert "```" not in cleaned
    assert "完成了" in cleaned


def test_latin_words_are_dropped() -> None:
    cleaned = normalize_for_speech("已经打开 Chrome 了。")

    assert "Chrome" not in cleaned
    assert "已经打开" in cleaned


def test_temperature_unit_is_spoken() -> None:
    """The model cannot say 「℃」 and the Latin strip cannot say it either."""
    assert "25摄氏度" in normalize_for_speech("现在温度 25℃。")


def test_percent_is_spoken_the_way_a_person_says_it() -> None:
    assert "百分之45" in normalize_for_speech("电量 45%。")


def test_latin_units_are_spoken_not_dropped() -> None:
    cleaned = normalize_for_speech("距离 5km，重量 2kg，频率 50Hz。")

    assert "5公里" in cleaned
    assert "2公斤" in cleaned
    assert "50赫兹" in cleaned


def test_a_decimal_number_keeps_its_point() -> None:
    """「3.5」 must not become 「3。5」 nor be stripped down to 「3 5」."""
    assert "3.5" in normalize_for_speech("耗时 3.5 秒。")


def test_a_clock_time_keeps_its_colon() -> None:
    assert "15:30" in normalize_for_speech("会议在 15:30 开始。")


def test_halfwidth_punctuation_becomes_the_marks_that_carry_pauses() -> None:
    assert normalize_for_speech("你好,世界!") == "你好，世界！"


def test_brackets_survive_so_the_splitter_can_see_the_pair() -> None:
    assert "（第一项）" in normalize_for_speech("请看(第一项)。")


def test_repeated_marks_collapse_to_the_last_one() -> None:
    assert normalize_for_speech("好的。。。") == "好的。"
    assert normalize_for_speech("注意！！！") == "注意！"


def test_an_all_english_answer_becomes_empty_rather_than_a_husk() -> None:
    assert normalize_for_speech("Done! The file was created successfully.") == ""


def test_a_punctuation_only_fragment_is_dropped_with_its_mark() -> None:
    assert normalize_for_speech("Done! 已经完成了。") == "已经完成了。"


def test_an_unpronounceable_answer_becomes_empty() -> None:
    assert normalize_for_speech("") == ""
    assert normalize_for_speech("   ") == ""


def test_the_default_budget_is_the_tool_message_budget() -> None:
    long_answer = "这是一段很长的回答。" * 40

    assert len(normalize_for_speech(long_answer)) <= MAX_SPEECH_CHARS


def test_a_wider_budget_keeps_a_longer_answer() -> None:
    """An agent's answer gets more room now that playback is per sentence."""
    long_answer = "这是一段很长的回答。" * 40

    kept = normalize_for_speech(long_answer, max_chars=240)

    assert MAX_SPEECH_CHARS < len(kept) <= 240
    assert kept.endswith("。")


# ──────────────────────────────────────────────────────────────
# Sentence splitting
# ──────────────────────────────────────────────────────────────


def test_each_sentence_becomes_its_own_segment_with_its_pause() -> None:
    segments = segment_for_speech("你好。今天天气不错。")

    assert [s.text for s in segments] == ["你好。", "今天天气不错。"]
    assert segments[0].pause_after_ms == DEFAULT_CONFIG.pause_sentence_ms
    assert segments[0].kind == "sentence"


def test_a_question_pause_is_longer_than_a_statement() -> None:
    (question,) = segment_for_speech("你确定吗？")
    (statement,) = segment_for_speech("我确定了。")

    # The last segment carries the tail silence instead of its own mark's pause,
    # so the difference is read off the table directly.
    assert DEFAULT_CONFIG.pause_question_ms > DEFAULT_CONFIG.pause_sentence_ms
    assert question.pause_after_ms == statement.pause_after_ms == DEFAULT_CONFIG.tail_silence_ms


def test_the_question_pause_reaches_the_playback_layer() -> None:
    segments = segment_for_speech("我先看一下。你确定吗？")

    assert segments[0].pause_after_ms == DEFAULT_CONFIG.pause_sentence_ms
    # …and the question, being last, hands over to the tail silence — but the
    # pause it *would* have earned is what the table says.
    assert DEFAULT_CONFIG.pause_question_ms > segments[0].pause_after_ms


def test_a_quoted_sentence_is_not_split_at_its_own_full_stop() -> None:
    """A cut inside 「」 would chop the quote in half; the pair is one unit."""
    segments = segment_for_speech("他说「第一句。第二句。」然后就走了。")

    assert len(segments) == 1
    assert segments[0].text == "他说「第一句。第二句。」然后就走了。"


def test_a_newline_survives_normalization() -> None:
    """
    `sanitize_for_tts` flattens whitespace, which would erase the only signal
    that the reply began a new paragraph — and with it the longest pause in the
    table.
    """
    normalized = normalize_for_speech("好的，我马上办。\n还有其他事吗？")

    assert "\n" in normalized


def test_a_newline_is_a_paragraph_and_gets_the_longest_pause() -> None:
    text = normalize_for_speech("好的，我马上办。\n还有其他事吗？")
    segments = segment_for_speech(text)

    assert len(segments) == 2
    assert segments[0].pause_after_ms == DEFAULT_CONFIG.pause_paragraph_ms
    assert segments[0].text == "好的，我马上办。"
    assert "\n" not in segments[0].text


def test_an_unpunctuated_sentence_is_not_sawn_in_half() -> None:
    """17 characters, no mark: cutting it would only make the prosody worse."""
    segments = segment_for_speech("这是一份很重要的文件我马上就去办理")

    assert len(segments) == 1
    assert segments[0].text == "这是一份很重要的文件我马上就去办理"


def test_an_empty_reply_produces_no_segments() -> None:
    assert segment_for_speech("") == []
    assert segment_for_speech("   ") == []


# ──────────────────────────────────────────────────────────────
# Cutting a long sentence — where the comma pauses come from
# ──────────────────────────────────────────────────────────────


LONG_REPLY = (
    "我先把桌面上那份重要的文件保存好，"
    "接下来我会把处理结果告诉你，"
    "最后再帮你检查一遍所有的设置。"
)


def test_a_long_sentence_is_cut_at_its_commas_so_they_are_audible() -> None:
    segments = segment_for_speech(LONG_REPLY)

    assert [s.text for s in segments] == [
        "我先把桌面上那份重要的文件保存好，",
        "接下来我会把处理结果告诉你，",
        "最后再帮你检查一遍所有的设置。",
    ]
    assert [s.kind for s in segments] == ["clause", "clause", "sentence"]
    assert [s.pause_after_ms for s in segments] == [
        DEFAULT_CONFIG.pause_comma_ms,
        DEFAULT_CONFIG.pause_comma_ms,
        DEFAULT_CONFIG.tail_silence_ms,
    ]


def test_every_piece_of_a_punctuated_sentence_stays_under_the_chunk_target() -> None:
    for segment in segment_for_speech(LONG_REPLY):
        assert segment.chars <= DEFAULT_CONFIG.chunk_max_chars


def test_the_first_chunk_is_short_when_a_boundary_is_available_early() -> None:
    """Audio has to start early, so the first unit aims at first_chunk_max_chars."""
    segments = segment_for_speech(
        "好的明白了，我马上就去办这件事情，你别着急，很快就能处理好。"
    )

    assert segments[0].chars <= DEFAULT_CONFIG.first_chunk_max_chars
    assert segments[0].text == "好的明白了，"
    assert segments[0].pause_after_ms == DEFAULT_CONFIG.pause_comma_ms


def test_a_deliberately_short_first_chunk_is_not_merged_away() -> None:
    """
    The first chunk is short *by design* (audio starts while the rest is being
    synthesised), so the merge rule must leave it alone.
    """
    segments = segment_for_speech(
        "好的明白了，我马上就去办这件事情，你别着急，很快就能处理好。"
    )

    assert segments[0].chars < DEFAULT_CONFIG.chunk_min_chars
    assert len(segments) == 2


def test_a_tiny_tail_is_folded_back_into_its_sentence() -> None:
    """A two-character orphan is a whole synthesis call for nothing."""
    segments = segment_for_speech("好的，我马上就去办这件事情，你别着急。")

    assert len(segments) == 1
    assert segments[0].text == "好的，我马上就去办这件事情，你别着急。"


def test_a_cut_prefers_a_conjunction_over_a_far_away_comma() -> None:
    """
    With no comma near the target, the conjunction is where a reader breathes.
    """
    segments = segment_for_speech(LONG_REPLY)

    assert segments[1].text.startswith("接下来我会把处理结果告诉你，")
    assert "最后" in segments[2].text


def test_an_explicit_comma_beats_a_conjunction_just_past_it() -> None:
    """
    Regression: scoring a conjunction above a comma produced
    「好的明白了，我」｜「马上就去办…」 — a stray pronoun left hanging at the end
    of the first chunk, three characters after a perfectly good comma.
    """
    segments = segment_for_speech(
        "好的明白了，我马上就去办这件事情，你别着急，很快就能处理好。"
    )

    assert segments[0].text == "好的明白了，"
    assert not segments[0].text.endswith("我")
    assert segments[0].pause_after_ms == DEFAULT_CONFIG.pause_comma_ms


def test_a_forced_cut_over_the_hard_ceiling_gets_the_shortest_pause() -> None:
    """180 characters and not one mark: it has to be broken, so break it quietly."""
    run_on = "甲" * 180
    segments = segment_for_speech(run_on)

    assert len(segments) > 1
    assert all(segment.pause_after_ms in (DEFAULT_CONFIG.pause_forced_ms,
                                          DEFAULT_CONFIG.tail_silence_ms)
               for segment in segments)
    assert all(segment.chars <= DEFAULT_CONFIG.hard_max_chars for segment in segments)


# ──────────────────────────────────────────────────────────────
# Refusals — the cuts that must never happen
# ──────────────────────────────────────────────────────────────


def test_a_cut_never_lands_right_after_a_possessive_de() -> None:
    """Splitting 「…的」 from its noun is what makes TTS sound broken."""
    text = "甲" * 15 + "的" + "乙" * 100
    segments = segment_for_speech(text)

    assert len(segments) > 1
    assert not any(segment.text.endswith("的") for segment in segments)
    assert not any(segment.text.startswith("的") for segment in segments)


def test_a_cut_never_lands_inside_a_decimal_number() -> None:
    """The number stays whole, whichever side of the seam it ends up on."""
    text = "甲" * 14 + "3.5" + "乙" * 70
    segments = segment_for_speech(text)

    assert any("3.5" in segment.text for segment in segments)
    assert not any(segment.text.endswith("3.") for segment in segments)
    assert not any(segment.text.startswith("5") for segment in segments)


def test_a_cut_never_lands_inside_a_bracket_pair() -> None:
    """
    A 30-character run inside 「（）」 is over `clause_max_chars` and still not
    cut: the pair is one unit, and the only boundary outside it is the sentence
    end.
    """
    text = "他说（" + "甲" * 30 + "）好。"
    segments = segment_for_speech(text)

    assert len(segments) == 1
    assert "甲" * 30 in segments[0].text


def test_no_piece_starts_with_a_particle_that_belongs_to_the_word_before() -> None:
    text = "甲" * 12 + "很长的内容" + "了" + "乙" * 80
    segments = segment_for_speech(text)

    assert not any(segment.text.startswith("了") for segment in segments)


# ──────────────────────────────────────────────────────────────
# Invariants
# ──────────────────────────────────────────────────────────────


def test_segmentation_never_loses_or_reorders_text() -> None:
    reply = (
        "我把文件保存好了，接下来我会把结果告诉你。"
        "另外还有一件事，你需要我顺便检查一下设置吗？"
        "最后提醒一下，桌面上还有一份草稿没有处理。"
    )
    normalized = normalize_for_speech(reply, max_chars=240)
    segments = segment_for_speech(normalized)

    assert squeeze(joined(segments)) == squeeze(normalized)


def test_a_two_hundred_char_reply_stays_within_its_budget_and_bounds() -> None:
    reply = "我已经把这部分内容处理好了，接下来会继续检查剩下的项目。" * 8
    normalized = normalize_for_speech(reply, max_chars=240)
    segments = segment_for_speech(normalized)

    assert len(joined(segments)) <= 240
    assert all(segment.chars <= DEFAULT_CONFIG.hard_max_chars for segment in segments)
    # Sentence ends and commas both produce cuts, so this is nothing like one
    # long breath:
    assert len(segments) >= 4


def test_the_last_segment_hands_over_to_the_tail_silence() -> None:
    segments = segment_for_speech(LONG_REPLY)

    assert segments[-1].pause_after_ms == DEFAULT_CONFIG.tail_silence_ms


# ──────────────────────────────────────────────────────────────
# Config plumbing
# ──────────────────────────────────────────────────────────────


class _FakeConfig:
    def __init__(self, values: dict) -> None:
        self._values = values

    def get(self, key, default=None):
        return self._values.get(key, default)


def test_the_pause_table_is_read_from_config() -> None:
    cfg = _FakeConfig(
        {
            "tts.pause_comma_ms": 111,
            "tts.pause_sentence_ms": 222,
            "tts.first_chunk_max_chars": 8,
            "tts.trim_ratio": 0.02,
        }
    )

    speech = speech_text_config(cfg)

    assert speech.pause_comma_ms == 111
    assert speech.pause_sentence_ms == 222
    assert speech.first_chunk_max_chars == 8
    assert speech.trim_ratio == 0.02
    # Untouched keys keep the measured defaults.
    assert speech.pause_question_ms == DEFAULT_CONFIG.pause_question_ms


def test_a_config_pause_reaches_the_segmentation() -> None:
    config = SpeechTextConfig(pause_comma_ms=99)

    segments = segment_for_speech(LONG_REPLY, config)

    assert segments[0].pause_after_ms == 99
