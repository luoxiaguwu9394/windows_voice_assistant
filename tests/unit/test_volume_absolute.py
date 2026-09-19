"""
Regression: "set the volume to X%" is an ABSOLUTE request, not a delta.

Reported symptom: "调到百分之九十" appeared to work, "调到百分之十" did not.

The ASR runs with ITN on, so the pipeline never sees Chinese numerals — a real
TTS -> ASR round trip of 「把音量调到百分之十」 produces `把音量调到10%。`.
`_extract_args` then scraped the digits and handed `set_volume` a *relative*
delta of +10 (and +90 for 90%), because the tool's only argument was `delta`:

    "把音量调到90%。"  ->  current + 90 points  ->  looks like success
    "把音量调到10%。"  ->  current + 10 points  ->  always goes UP

The fix adds an absolute `level` argument end to end (rules -> registry ->
tool), leaving delta wordings such as 「音量调大 20」 relative.
"""

from __future__ import annotations

import pytest

from winvoice.contracts import ToolName
from winvoice.intent.rules import match_rules
from winvoice.tools.registry import get_tool_registry

# Absolute phrasings, written the way the pipeline really receives them.
ABSOLUTE_CASES = [
    ("把音量调到10%。", 10),      # real ASR output for 「把音量调到百分之十」
    ("把音量调到90%。", 90),      # real ASR output for 「把音量调到百分之九十」
    ("音量调到50%。", 50),
    ("把音量调到100%。", 100),
    ("把音量调到0%。", 0),        # must survive as an absolute zero, not be "unset"
    ("把音量调到百分之二十", 20),  # ITN disabled / not applied
    ("音量调到九十", 90),          # Chinese numerals, no 百分之
    ("把音量设为百分之五", 5),
    # Relative phrasings must stay relative.
    ("音量调大 20", None),
    ("音量调小 20", None),
    ("音量调低", None),
]


@pytest.mark.parametrize("text,level", ABSOLUTE_CASES)
def test_absolute_target_is_parsed_as_a_level(text: str, level: int | None) -> None:
    result = match_rules(text)

    assert result is not None, f"no rule matched {text!r}"
    assert result.intent.value == "set_volume", f"{text!r} -> {result.intent.value}"

    if level is None:
        assert "level" not in result.args, f"{text!r} -> {result.args}"
        assert isinstance(result.args.get("delta"), int)
        return

    assert result.args.get("level") == level, f"{text!r} -> {result.args}"
    assert "delta" not in result.args, f"{text!r} misread as a relative change: {result.args}"


def test_relative_direction_is_unchanged_by_the_absolute_support() -> None:
    assert match_rules("音量调大 20").args == {"delta": 20}
    assert match_rules("音量调低").args == {"delta": -10}


def test_registry_accepts_a_level_only_call() -> None:
    """`delta` cannot stay a required argument once `level` exists."""
    registry = get_tool_registry()

    assert registry.validate_call(ToolName.SET_VOLUME, {"level": 10}) is None
    assert registry.validate_call(ToolName.SET_VOLUME, {"delta": -10}) is None
    assert registry.validate_call(ToolName.SET_VOLUME, {}) is not None


def test_tool_sets_an_absolute_target_rather_than_adding(monkeypatch) -> None:
    """The generated PowerShell must not be `$current + ...` for a level."""
    import subprocess

    import winvoice.tools.builtin as builtin

    captured = {}

    def fake_run(cmd, *args, **kwargs):
        captured["script"] = cmd[-1]

        class Done:
            returncode = 0
            stdout = "10"
            stderr = ""

        return Done()

    monkeypatch.setattr(builtin.subprocess, "run", fake_run)
    result = builtin.set_volume({"level": 10})

    assert result["success"] is True, result
    target = [l for l in captured["script"].splitlines() if "$target =" in l][0]
    assert "$current +" not in target, f"level was applied as a delta: {target.strip()}"
    assert "10" in target, target.strip()


def test_tool_rejects_a_missing_instruction() -> None:
    import winvoice.tools.builtin as builtin

    result = builtin.set_volume({})

    assert result["success"] is False
    assert "delta" in result["error"] and "level" in result["error"]
