"""
Regression: the spoken reply must be pronounceable by the Chinese VITS.

Reported symptom: asking for an app outside the whitelist produced a wall of

    lexicon.cc:ConvertTextToTokenIdsChinese:242 OOV 'notepad'. Ignore it!

The reply the assistant speaks was the raw tool error:

    执行遇到问题：App not allowed: 微信. Allowed: ['notepad', 'calculator', ...]

The TTS model is `vits-icefall-zh-aishell3`: its lexicon holds **zero Latin
entries**, so sherpa-onnx drops every English word on the floor ("Ignore it!").
The user hears a sentence with holes punched in it, and the console floods.

These tests use that lexicon as ground truth: anything the assistant decides to
say must consist of characters the model can actually pronounce.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from winvoice.audio.pipeline import AudioPipeline
from winvoice.contracts import IntentName, IntentResult, ToolCall, ToolName
from winvoice.tools.executor import create_tool_executor

REPO_ROOT = Path(__file__).resolve().parents[2]
LEXICON = REPO_ROOT / "models" / "tts" / "vits-icefall-zh-aishell3" / "lexicon.txt"

pytestmark = pytest.mark.skipif(not LEXICON.exists(), reason="TTS model not downloaded")


@pytest.fixture(scope="module")
def lexicon() -> set[str]:
    return {l.split()[0] for l in LEXICON.read_text(encoding="utf-8").splitlines() if l.strip()}


@pytest.fixture(scope="module")
def pipeline() -> AudioPipeline:
    """A stub-engine pipeline: `_default_reply` needs no models loaded."""
    return AudioPipeline(use_stub=True)


async def spoken_reply(pipeline: AudioPipeline, tool: ToolName, args: dict, intent_name: IntentName, raw: str) -> str:
    """Run tool -> _default_reply exactly as the live pipeline does."""
    call = ToolCall(trace_id="test", tool=tool, args=args)
    result = await create_tool_executor().execute(call)
    intent = IntentResult(
        trace_id="test", intent=intent_name, args=args, confidence=0.95, source="rules", raw_text=raw
    )
    return pipeline._default_reply(intent, [result])


def assert_speakable(spoken: str, lexicon: set[str]) -> None:
    """
    Latin letters are dropped outright; CJK characters must be in the lexicon.

    Punctuation is exempt: sherpa-onnx strips it before the lexicon lookup and
    logs no OOV for it (verified with 「好的，已为您完成。」). Digits are exempt
    too — they are expanded by the model's `number.fst`/`date.fst` rules.
    """
    latin = "".join(dict.fromkeys(c for c in spoken if c.isascii() and c.isalpha()))
    assert not latin, (
        f"the TTS lexicon has no Latin entries, so {latin!r} would be dropped: {spoken!r}"
    )

    unpronounceable = sorted({c for c in spoken if c.isalpha() and not c.isascii() and c not in lexicon})
    assert not unpronounceable, (
        f"characters missing from the lexicon would be skipped: {unpronounceable} in {spoken!r}"
    )


@pytest.mark.asyncio
async def test_app_outside_the_whitelist_names_the_alternatives_in_chinese(pipeline, lexicon):
    spoken = await spoken_reply(pipeline, ToolName.OPEN_APP, {"app": "微信"}, IntentName.OPEN_APP, "打开微信")

    assert_speakable(spoken, lexicon)
    assert "微信" in spoken, f"the user should hear which app was refused: {spoken!r}"
    assert "记事本" in spoken, f"the whitelist should be offered in Chinese: {spoken!r}"


@pytest.mark.asyncio
async def test_missing_file_failure_is_speakable(pipeline, lexicon):
    spoken = await spoken_reply(
        pipeline, ToolName.READ_FILE, {"path": "current directory"}, IntentName.READ_FILE, "读取文件"
    )

    assert_speakable(spoken, lexicon)
    assert "文件" in spoken


@pytest.mark.asyncio
async def test_confirmation_required_is_speakable(pipeline, lexicon):
    spoken = await spoken_reply(
        pipeline,
        ToolName.WRITE_FILE,
        {"path": str(REPO_ROOT / "x.txt"), "content": "hi"},
        IntentName.WRITE_FILE,
        "写入文件",
    )

    assert_speakable(spoken, lexicon)


# Every failure path of every builtin, as the user would hear it.
BUILTIN_FAILURES = [
    (ToolName.OPEN_APP, {"app": "微信"}, IntentName.OPEN_APP),
    (ToolName.CLOSE_APP, {"app": "微信"}, IntentName.CLOSE_APP),
    (ToolName.OPEN_APP, {"app": "wechat"}, IntentName.OPEN_APP),  # Latin name from ASR
    (ToolName.READ_FILE, {"path": "no_such_file.txt"}, IntentName.READ_FILE),
    (ToolName.READ_FILE, {"path": "C:/Windows/not_allowed.txt"}, IntentName.READ_FILE),
    (ToolName.WRITE_FILE, {"path": "C:/Windows/not_allowed.txt", "content": "x"}, IntentName.WRITE_FILE),
    (ToolName.RUN_SCRIPT, {"path": "no_such_script.py"}, IntentName.RUN_SCRIPT),
    (ToolName.RUN_SCRIPT, {"path": "C:/Windows/not_allowed.exe"}, IntentName.RUN_SCRIPT),
    (ToolName.SEARCH_WEB, {"query": ""}, IntentName.SEARCH_WEB),
    (ToolName.SET_VOLUME, {}, IntentName.SET_VOLUME),
    (ToolName.SET_VOLUME, {"level": "abc"}, IntentName.SET_VOLUME),
    (ToolName.SET_VOLUME, {"delta": "abc"}, IntentName.SET_VOLUME),
    (ToolName.MEDIA_CONTROL, {"action": "rewind"}, IntentName.MEDIA_CONTROL),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("tool,args,intent_name", BUILTIN_FAILURES)
async def test_every_builtin_failure_is_speakable(pipeline, lexicon, tool, args, intent_name):
    spoken = await spoken_reply(pipeline, tool, args, intent_name, str(args))

    assert_speakable(spoken, lexicon)


# ── the success side of the same contract ─────────────────────────────────
#
# A successful `message` is spoken now, so it must clear the same bar as a
# failure one: plain Chinese, no Latin, no markdown. `get_weather` is checked
# in tests/unit/test_weather_speech.py because it needs a payload, not network.
# `write_file` / `run_script` are absent on purpose: their confirmation round
# trip does not exist yet, so their success path is unreachable.

BUILTIN_SUCCESSES = [
    (ToolName.OPEN_APP, {"app": "notepad"}, IntentName.OPEN_APP),
    (ToolName.OPEN_APP, {"app": "记事本"}, IntentName.OPEN_APP),
    (ToolName.CLOSE_APP, {"app": "notepad"}, IntentName.CLOSE_APP),
    (ToolName.MEDIA_CONTROL, {"action": "play"}, IntentName.MEDIA_CONTROL),
    (ToolName.MEDIA_CONTROL, {"action": "prev"}, IntentName.MEDIA_CONTROL),
    (ToolName.SET_VOLUME, {"delta": 20}, IntentName.SET_VOLUME),
    (ToolName.SET_VOLUME, {"level": 30}, IntentName.SET_VOLUME),
    (ToolName.SEARCH_WEB, {"query": "今天新闻"}, IntentName.SEARCH_WEB),
    (ToolName.GET_TIME, {}, IntentName.GET_TIME),
]


class _FakeProc:
    returncode = 0
    stdout = "45\n"
    stderr = ""


class _FakeSubprocess:
    """Replaces the `subprocess` module: a test must not change the machine."""

    @staticmethod
    def run(*args, **kwargs):
        return _FakeProc()

    @staticmethod
    def Popen(*args, **kwargs):
        return _FakeProc()


class _FakeWebbrowser:
    @staticmethod
    def open(*args, **kwargs):
        return True


@pytest.fixture
def harmless(monkeypatch):
    """Stop the handlers from opening windows, pressing keys or moving sliders."""
    from winvoice.tools import builtin

    monkeypatch.setattr(builtin, "subprocess", _FakeSubprocess)
    monkeypatch.setattr(builtin, "webbrowser", _FakeWebbrowser)


@pytest.mark.asyncio
@pytest.mark.parametrize("tool,args,intent_name", BUILTIN_SUCCESSES)
async def test_every_builtin_success_is_speakable(pipeline, lexicon, harmless, tool, args, intent_name):
    spoken = await spoken_reply(pipeline, tool, args, intent_name, str(args))

    assert_speakable(spoken, lexicon)
    # "好的，已为您完成。" is the fallback, and it means the tool said nothing
    # the pipeline could use — for these tools that is a bug in the tool.
    assert spoken != "好的，已为您完成。", f"{tool.value} had nothing speakable to report"


def test_every_weather_condition_translation_is_pronounceable(lexicon):
    """
    The condition table is a fallback *for* the Chinese-only rule, so a typo in
    it would reintroduce exactly the defect it exists to prevent.

    `wttr.in` returns English descriptions no matter what `lang` is asked for
    (verified live), so these strings are what the user actually hears.
    """
    from winvoice.tools.weather import WEATHER_CODE_ZH

    assert len(WEATHER_CODE_ZH) >= 60, "the published WWO list has 60 codes"
    for code, text in WEATHER_CODE_ZH.items():
        assert_speakable(text, lexicon)
        assert text.strip() == text and text, f"code {code} is not a clean phrase"


