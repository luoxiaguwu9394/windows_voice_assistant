"""
Regression: direction words must decide the SIGN of the volume delta.

Reported symptom: both "调高音量" and "调低音量" turned the volume UP.

`_extract_args` defaults to a positive delta and then flips it only when one of
a short list of negative markers appears. That list was missing `调低` (and
`小一点`, `声音…` phrasings), so every "lower the volume" phrasing that did not
happen to contain `调小`/`降低`/`小声` fell through to the default and raised the
volume instead.

The second half of the bug is in the rule pattern itself: a volume command that
never says `音量` (`把声音调低`) matched no rule at all and was handed to the
local LLM, which is free to pick either sign.
"""

from __future__ import annotations

import pytest

from winvoice.intent.rules import match_rules

# (utterance, expected direction) -- direction only, magnitude is asserted apart.
DIRECTION_CASES = [
    # The reported pair.
    ("调高音量", +1),
    ("调低音量", -1),
    # Verb after the noun.
    ("把音量调高", +1),
    ("把音量调低", -1),
    # Comparative phrasing (no verb at all).
    ("音量大一点", +1),
    ("音量小一点", -1),
    ("声音大一点", +1),
    ("声音小一点", -1),
    # `声音` without `音量` must still be recognised as a volume command.
    ("把声音调高", +1),
    ("把声音调低", -1),
    # Already-working markers, to pin them against the fix.
    ("音量调大", +1),
    ("音量调小", -1),
    ("音量降低", -1),
    ("小声一点", -1),
    ("volume up", +1),
    ("volume down", -1),
    ("turn the volume up", +1),
    ("turn the volume down", -1),
    ("lower the volume", -1),
]


@pytest.mark.parametrize("text,direction", DIRECTION_CASES)
def test_volume_direction_matches_the_wording(text: str, direction: int) -> None:
    result = match_rules(text)

    assert result is not None, f"no rule matched {text!r}"
    assert result.intent.value == "set_volume", f"{text!r} -> {result.intent.value}"

    delta = result.args.get("delta")
    assert isinstance(delta, int) and delta != 0, f"{text!r} -> delta={delta!r}"
    assert (delta > 0) == (direction > 0), f"{text!r} -> delta={delta}"


@pytest.mark.parametrize(
    "text,direction",
    [("音量调大 20", +20), ("音量调小 20", -20), ("把音量调低 30", -30)],
)
def test_explicit_amount_keeps_its_magnitude(text: str, direction: int) -> None:
    result = match_rules(text)

    assert result is not None, f"no rule matched {text!r}"
    assert result.args.get("delta") == direction
