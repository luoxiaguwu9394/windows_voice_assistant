"""
`sanitize_for_tts`: the safety net between a model's answer and the speaker.

Every spoken string used to be authored by a tool, which knew the rule — the TTS
lexicon (`vits-icefall-zh-aishell3`, 66 377 entries) contains **zero Latin
entries**, so sherpa-onnx drops English word by word and the user hears a
sentence with holes in it (spec.md §6.4). DeepSeek Harness composes the final
reply now, so the guarantee has to be enforced *after* the model rather than
assumed of it.

The agent is instructed to answer in plain Chinese. These tests exist for when it
does not.
"""

from __future__ import annotations

from winvoice.contracts import MAX_SPEECH_CHARS
from winvoice.contracts.speech import sanitize_for_tts


def test_plain_chinese_survives_untouched() -> None:
    text = "我已经把记事本打开了。"

    assert sanitize_for_tts(text) == text


def test_digits_survive_because_they_are_speakable() -> None:
    """`number.fst` expands digits, so they must not be stripped with ASCII."""
    assert "45" in sanitize_for_tts("音量已经调到百分之45。")


def test_latin_words_are_dropped() -> None:
    cleaned = sanitize_for_tts("已经打开 Chrome 了。")

    assert "Chrome" not in cleaned
    assert "已经打开" in cleaned


def test_markdown_fences_and_their_contents_go() -> None:
    cleaned = sanitize_for_tts("结果如下：\n```python\nprint('hi')\n```\n完成了。")

    assert "print" not in cleaned
    assert "```" not in cleaned
    assert "完成了" in cleaned


def test_inline_code_and_links_keep_their_text() -> None:
    cleaned = sanitize_for_tts("请查看 [报告](https://example.com/a) 里的 `结论` 部分。")

    assert "报告" in cleaned
    assert "结论" in cleaned
    assert "https" not in cleaned


def test_bullet_markers_are_not_read_aloud() -> None:
    cleaned = sanitize_for_tts("好的：\n- 第一项\n- 第二项")

    assert "-" not in cleaned
    assert "第一项" in cleaned


def test_emphasis_markers_and_code_punctuation_go() -> None:
    cleaned = sanitize_for_tts("**重点** 是 <这个> 和 [那个]。")

    assert "*" not in cleaned and "<" not in cleaned and "[" not in cleaned
    assert "重点" in cleaned


def test_an_all_english_answer_becomes_empty_rather_than_a_husk() -> None:
    """
    Returning "" is the useful answer: the caller can then say something honest
    instead of speaking a sentence made only of punctuation scraps.
    """
    assert sanitize_for_tts("Done! The file was created successfully.") == ""


def test_a_markdown_only_answer_becomes_empty() -> None:
    assert sanitize_for_tts("```\nfoo\n```") == ""


def test_output_is_clipped_to_the_speech_budget() -> None:
    """TTS synthesises the whole utterance before playing, so length is dead air."""
    long_answer = "这是一段很长的回答。" * 40

    assert len(sanitize_for_tts(long_answer)) <= MAX_SPEECH_CHARS


def test_empty_input_is_empty() -> None:
    assert sanitize_for_tts("") == ""
    assert sanitize_for_tts("   ") == ""


def test_paths_are_not_spoken() -> None:
    """A path is long, Latin, and the user already knows which file they meant."""
    cleaned = sanitize_for_tts("已经写入 C:/Users/me/Desktop/notes.txt 了。")

    assert "notes.txt" not in cleaned
    assert "已经写入" in cleaned
