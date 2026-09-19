"""
Guided enrolment prompts.

`python -m winvoice.enroll` used to say only "Sample 3/8 - speak for 4s", which
left the speaker to invent eight sentences on the spot. That produced uneven
recordings (and often eight near-identical command phrases), which is exactly
what the threshold derivation then has to compensate for. Each sample now names
a line to read, rotating through digits / commands / small talk.
"""

from __future__ import annotations

import pytest

from winvoice.enroll.session import ENROLLMENT_PROMPTS, EnrollmentSession, prompt_for


def test_eight_prompts_matching_the_default_sample_count() -> None:
    assert len(ENROLLMENT_PROMPTS) == 8


def test_categories_rotate_through_digits_commands_and_small_talk() -> None:
    assert [category for category, _ in ENROLLMENT_PROMPTS] == [
        "数字", "指令", "闲聊",
        "数字", "指令", "闲聊",
        "数字", "指令",
    ]


@pytest.mark.parametrize("sample_num", [1, 2, 3, 4, 5, 6, 7, 8])
def test_each_sample_gets_its_own_prompt_in_order(sample_num: int) -> None:
    assert prompt_for(sample_num) == ENROLLMENT_PROMPTS[sample_num - 1]


def test_prompts_cycle_when_more_samples_are_requested() -> None:
    assert prompt_for(9) == ENROLLMENT_PROMPTS[0]
    assert prompt_for(16) == ENROLLMENT_PROMPTS[7]


def test_every_prompt_is_speakable_chinese_of_a_sane_length() -> None:
    for category, phrase in ENROLLMENT_PROMPTS:
        assert category in ("数字", "指令", "闲聊")
        assert phrase.strip(), "an empty prompt would leave the user silent"
        # ~4 s of speech at a normal pace is roughly 10-30 characters.
        assert 8 <= len(phrase) <= 30, f"{phrase!r} is {len(phrase)} chars"
        assert not any(ch.isascii() and ch.isalpha() for ch in phrase), phrase


def test_record_sample_prints_the_prompt_for_that_sample(monkeypatch, capsys) -> None:
    """The guidance has to actually reach the terminal."""
    from winvoice.enroll import session as session_mod

    class FakeStream:
        def __init__(self, **kwargs) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc) -> bool:
            return False

    monkeypatch.setattr(session_mod.sd, "InputStream", FakeStream)
    monkeypatch.setattr(session_mod.sd, "sleep", lambda ms: None)
    monkeypatch.setattr(session_mod.time, "sleep", lambda s: None)

    # Only the recording loop is under test; skip engine construction.
    session = EnrollmentSession.__new__(EnrollmentSession)
    session.num_samples = 8
    session.sample_duration = 4.0
    session.sample_rate = 16000

    session.record_sample(4)

    printed = capsys.readouterr().out
    category, phrase = ENROLLMENT_PROMPTS[3]
    assert "Sample 4/8" in printed
    assert f"【{category}】{phrase}" in printed
    # The following prompt is previewed so the speaker can read ahead.
    next_category, next_phrase = ENROLLMENT_PROMPTS[4]
    assert f"next up: 【{next_category}】{next_phrase}" in printed


def test_last_sample_has_no_next_preview(monkeypatch, capsys) -> None:
    from winvoice.enroll import session as session_mod

    class FakeStream:
        def __init__(self, **kwargs) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc) -> bool:
            return False

    monkeypatch.setattr(session_mod.sd, "InputStream", FakeStream)
    monkeypatch.setattr(session_mod.sd, "sleep", lambda ms: None)
    monkeypatch.setattr(session_mod.time, "sleep", lambda s: None)

    session = EnrollmentSession.__new__(EnrollmentSession)
    session.num_samples = 8
    session.sample_duration = 4.0
    session.sample_rate = 16000

    session.record_sample(8)

    assert "next up" not in capsys.readouterr().out
