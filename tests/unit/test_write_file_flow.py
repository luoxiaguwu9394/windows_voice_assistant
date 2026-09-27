"""
The spoken file-creation flow: 「帮我在桌面建立一个txt文件」 must work.

Live report (2026-09-27): the classifier classified the utterance correctly
(write_file, path=桌面\新建.txt) and the call still died — `content` was
mandatory although the speaker had named no content (and 「建立一个txt文件」
means Windows' 新建文本文档: an *empty* file), the spoken refusal was the
tier sentence (「这个操作我暂时不能替你做。」 — a non-answer for the owner),
and 「桌面」 would have resolved to a *folder named 桌面 inside the repo*
because nothing mapped spoken folder words onto the real folders.

Three behaviours are pinned here: folder-word mapping, content-optional
creation (with the overwrite guard), and refusal sentences that match the
failure kind. Plus the router fallthrough: a rule-matched write_file with no
extractable arguments defers to the classifier instead of refusing.
"""

from __future__ import annotations

import shutil
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from winvoice.contracts import IntentName, SpeakerTier, ToolCall, ToolName
from winvoice.intent.router import IntentRouter
from winvoice.intent.rules import match_rules
from winvoice.tools.builtin import resolve_user_path, write_file
from winvoice.tools.executor import ToolExecutor
from winvoice.tools.registry import ToolRegistry

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def scratch():
    base = PROJECT_ROOT / "runtime" / "_pytest_work"
    directory = base / f"writeflow_{uuid.uuid4().hex[:10]}"
    directory.mkdir(parents=True)
    try:
        yield directory
    finally:
        shutil.rmtree(directory, ignore_errors=True)


# ── spoken folder words map onto real folders ─────────────────


def test_spoken_desktop_maps_to_the_real_desktop():
    assert resolve_user_path("桌面\\新建.txt") == Path.home() / "Desktop" / "新建.txt"
    assert resolve_user_path("桌面") == Path.home() / "Desktop"


@pytest.mark.parametrize(
    "spoken,expected",
    [
        ("下载", "Downloads"),
        ("文档", "Documents"),
        ("图片\\照片.png", "Pictures/照片.png"),
    ],
)
def test_other_folder_words_map_too(spoken, expected):
    assert resolve_user_path(spoken) == Path.home() / Path(*expected.split("/"))


def test_absolute_paths_pass_through_unchanged():
    raw = str(PROJECT_ROOT / "some.txt")
    assert resolve_user_path(raw) == Path(raw)


# ── content-optional creation ─────────────────────────────────


def test_no_content_creates_an_empty_file(scratch: Path):
    target = scratch / "新建.txt"

    result = write_file({"path": str(target)})

    assert result["success"] is True, result
    assert target.exists() and target.read_text(encoding="utf-8") == ""


def test_no_content_never_truncates_an_existing_file(scratch: Path):
    target = scratch / "已存在.txt"
    target.write_text("珍贵内容", encoding="utf-8")

    result = write_file({"path": str(target)})

    assert result["success"] is False, "an unspecified write wiped a file"
    assert "已经存在" in result["message"]
    assert target.read_text(encoding="utf-8") == "珍贵内容", "the content was destroyed"


def test_explicit_content_still_writes(scratch: Path):
    target = scratch / "内容.txt"

    result = write_file({"path": str(target), "content": "你好"})

    assert result["success"] is True
    assert target.read_text(encoding="utf-8") == "你好"


def test_the_registry_no_longer_demands_content():
    """「建立一个txt文件」 carries no dictation; the schema must not demand one."""
    registry = ToolRegistry()

    assert registry.validate_call(
        ToolName.WRITE_FILE, {"path": "whatever.txt"}, tier=SpeakerTier.FULL.value
    ) is None


# ── refusal sentences match the failure kind ──────────────────


async def test_a_schema_mistake_is_not_spoken_as_a_tier_refusal():
    """The owner who simply didn't dictate content must not hear 「不能替你做」."""
    executor = ToolExecutor()

    result = await executor.execute(
        ToolCall(tool=ToolName.WRITE_FILE, args={}, tier=SpeakerTier.FULL)
    )

    assert result.success is False
    assert "没听清" in result.message, result.message
    assert "不能替你做" not in result.message


async def test_a_guest_still_hears_the_tier_refusal():
    executor = ToolExecutor()

    result = await executor.execute(
        ToolCall(tool=ToolName.READ_FILE, args={"path": "x.txt"}, tier=SpeakerTier.GUEST)
    )

    assert result.success is False
    assert result.message == "这个操作我暂时不能替你做。"


# ── the router fallthrough ────────────────────────────────────


def test_rules_still_shield_the_write_path_from_the_search_hijack():
    intent = match_rules("写入文件 search.py")
    assert intent.intent == IntentName.WRITE_FILE and intent.args == {}


async def test_rule_matched_write_file_without_args_falls_through_to_the_classifier(
    monkeypatch,
):
    """
    「写入文件 桌面x.txt」: the rule tier matches WRITE_FILE but extracts no
    arguments. Returning it refused a request the classifier fills in a
    second; the router now defers and lets tier 2 speak.
    """
    router = IntentRouter()

    async def healthy(self=None):
        return True

    async def classify(self, text):
        return SimpleNamespace(
            intent=IntentName.WRITE_FILE,
            args={"path": "桌面x.txt"},
            confidence=0.8,
            raw=text,
        )

    monkeypatch.setattr(router.classifier, "health_check", healthy)
    monkeypatch.setattr(router.classifier, "classify", classify.__get__(router.classifier))

    result = await router.route("写入文件 桌面x.txt")

    assert result.source == "local", "the empty rule result was returned instead"
    assert result.intent == IntentName.WRITE_FILE
    assert result.args == {"path": "桌面x.txt"}


def test_the_rule_result_still_wins_when_it_carries_arguments():
    """A normal rule match (e.g. 打开记事本) must not be delayed by the fallthrough."""
    assert match_rules("打开记事本").source == "rules"
