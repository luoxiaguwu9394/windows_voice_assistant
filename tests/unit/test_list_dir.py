"""
The `list_dir` tool: confined like `read_file`, spoken like every other message.

The speech side is the part that can go quietly wrong: file names are where
Latin text lives, the Chinese TTS lexicon drops every Latin word, and a message
built by naive name-joining comes out of the speaker full of holes. These tests
pin the count-plus-speakable-names format and the refusals.
"""

from __future__ import annotations

import shutil
import uuid
from pathlib import Path

import pytest

from winvoice.contracts import SpeakerTier, ToolName
from winvoice.tools.builtin import list_dir
from winvoice.tools.registry import ToolRegistry


@pytest.fixture
def folder():
    """
    A scratch folder under the user's home directory (the tool's confinement).

    Not pytest's `tmp_path`: that lives outside the workspace *and* outside the
    home directory the tool allows, so it could not be listed even if it were
    writable. The folder is removed afterwards.
    """
    target = Path.home() / f"winvoice_list_dir_test_{uuid.uuid4().hex[:8]}"
    target.mkdir(parents=True)
    try:
        yield target
    finally:
        shutil.rmtree(target, ignore_errors=True)


def test_lists_count_and_speakable_names(folder: Path) -> None:
    (folder / "文档").mkdir()
    (folder / "音乐").mkdir()
    (folder / "说明.txt").write_text("x", encoding="utf-8")
    (folder / "setup.py").write_text("x", encoding="utf-8")

    result = list_dir({"path": str(folder)})

    assert result["success"] is True
    assert result["count"] == 4
    # Directories are spoken first; Latin-bearing names are not spoken at all.
    assert "文档、音乐" in result["message"]
    assert "一共有4项" in result["message"]
    assert "还有其他" in result["message"], "the two unspoken files are part of the count"
    for silent in ("说明", "setup", "txt", "py"):
        assert silent not in result["message"]


def test_all_names_pronounceable_is_spoken_without_ellipsis(folder: Path) -> None:
    (folder / "文档").mkdir()
    (folder / "图片").mkdir()

    result = list_dir({"path": str(folder)})

    assert result["success"] is True
    # Names are spoken alphabetically (Unicode code-point order here).
    assert result["message"] == "一共有2项，前面几项是图片、文档。"


def test_no_pronounceable_names_says_so(folder: Path) -> None:
    (folder / "setup.py").write_text("x", encoding="utf-8")

    result = list_dir({"path": str(folder)})

    assert result["success"] is True
    assert "一共有1项" in result["message"]
    assert "念不出来" in result["message"]
    assert "setup" not in result["message"]


def test_empty_folder(folder: Path) -> None:
    result = list_dir({"path": str(folder)})

    assert result["success"] is True
    assert result["count"] == 0
    assert result["message"] == "这个文件夹是空的。"


def test_without_a_path_lists_the_user_directory() -> None:
    result = list_dir({})

    assert result["success"] is True
    assert result["path"] == str(Path.home())
    assert isinstance(result["count"], int)


def test_outside_the_user_directory_is_refused() -> None:
    result = list_dir({"path": "C:/Windows"})

    assert result["success"] is False
    assert result["message"] == "我只能列出你自己目录下的文件夹。"


def test_missing_and_non_directory_paths(folder: Path) -> None:
    missing = list_dir({"path": str(folder / "不存在")})
    assert missing["success"] is False
    assert missing["message"] == "没有找到这个文件夹。"

    target = folder / "文件.txt"
    target.write_text("x", encoding="utf-8")
    not_dir = list_dir({"path": str(target)})
    assert not_dir["success"] is False
    assert not_dir["message"] == "这个路径不是文件夹。"


def test_guests_cannot_list_directories() -> None:
    registry = ToolRegistry()

    assert registry.validate_call(ToolName.LIST_DIR, {}, tier=SpeakerTier.GUEST.value) is not None
    assert registry.validate_call(ToolName.LIST_DIR, {}, tier=SpeakerTier.FULL.value) is None


def test_directory_names_come_before_file_names(folder: Path) -> None:
    (folder / "歌词").write_text("x", encoding="utf-8")
    (folder / "阿宝").mkdir()

    result = list_dir({"path": str(folder)})

    assert result["message"].index("阿宝") < result["message"].index("歌词")
