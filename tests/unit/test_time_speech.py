"""
Regression: 「现在几点了」 must be answered.

The rule layer has matched `get_time` (「几点|什么时间|现在时间|time|clock」)
since it was written, but `_intent_to_tool_calls` had no entry for it, so the
reply was always 「抱歉，这个请求我还没有实现。」

The tool is trivial (`datetime.now()`); what needs pinning down is the *spoken
form*, because the Chinese TTS drops anything its lexicon does not contain:

  * the hour must never be spoken as 24 (23:59 is 晚上 11 点 59 分);
  * the 12-hour clock needs its period word (凌晨/上午/中午/下午/晚上), otherwise
    「1 点」 at 13:00 is simply wrong;
  * digits are allowed — `number.fst` expands them (see UNIMPLEMENTED §0).
"""

from __future__ import annotations

from datetime import datetime

import pytest

from winvoice.tools.builtin import format_spoken_time, get_time

# (hour, minute, spoken) — one case per period boundary plus both ends of 12/24.
TIME_CASES = [
    (0, 0, "现在是凌晨 12 点整。"),
    (0, 30, "现在是凌晨 12 点 30 分。"),
    (5, 59, "现在是凌晨 5 点 59 分。"),
    (6, 0, "现在是上午 6 点整。"),
    (9, 5, "现在是上午 9 点 5 分。"),  # no leading zero: 9 点 05 分 would be read oddly
    (11, 59, "现在是上午 11 点 59 分。"),
    (12, 0, "现在是中午 12 点整。"),
    (12, 1, "现在是中午 12 点 1 分。"),
    (13, 20, "现在是下午 1 点 20 分。"),
    (17, 59, "现在是下午 5 点 59 分。"),
    (18, 0, "现在是晚上 6 点整。"),
    (23, 59, "现在是晚上 11 点 59 分。"),
]


@pytest.mark.parametrize("hour,minute,expected", TIME_CASES)
def test_spoken_time_uses_a_12_hour_clock_with_a_period_word(hour, minute, expected):
    assert format_spoken_time(datetime(2026, 9, 19, hour, minute)) == expected


def test_spoken_time_never_says_24():
    """Crossing midnight in 24-hour form yields 「24 点」, which nobody says."""
    for minute in (0, 30, 59):
        spoken = format_spoken_time(datetime(2026, 9, 19, 0, minute))
        assert "24" not in spoken, spoken


def test_every_hour_of_the_day_is_covered_and_speakable():
    """
    No hour may fall through, and no two hours may sound the same.

    The 12-hour clock reuses the numbers 1-12 across its period words, so the
    *pair* (period, number) has to identify the hour: 00:30 is 凌晨 12 点 30 分
    while 12:30 is 中午 12 点 30 分, and 13:00 is 下午 1 点 while 01:00 is
    凌晨 1 点. This test is what keeps that mapping honest.
    """
    seen = set()
    for hour in range(24):
        spoken = format_spoken_time(datetime(2026, 9, 19, hour, 7))
        assert spoken.startswith("现在是"), spoken
        assert spoken.endswith("。"), spoken
        assert "24" not in spoken, spoken
        seen.add(spoken)
    assert len(seen) == 24, f"two different hours produced the same sentence: {sorted(seen)}"


def test_get_time_reports_the_machine_readable_clock_alongside_the_speech():
    """
    `time` is the caller's copy of the clock, and it is the one that was spoken.

    The two boundaries are compared as *clock readings*, not as minute-of-day
    counts: the counts wrap at midnight (23:59 → 0:00), which made this test
    fail once a day.
    """
    before = datetime.now()
    result = get_time({})
    after = datetime.now()

    assert result["success"] is True, result
    assert result["time"] in (before.strftime("%H:%M"), after.strftime("%H:%M")), result
    assert result["date"] in (before.strftime("%Y-%m-%d"), after.strftime("%Y-%m-%d"))


def test_get_time_message_is_the_spoken_form_of_that_same_clock():
    """`message` is what the pipeline says; it must describe `time`, not a date."""
    result = get_time({})
    hour, minute = (int(part) for part in result["time"].split(":"))
    assert result["message"] == format_spoken_time(datetime(2026, 1, 1, hour, minute))
