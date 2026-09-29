"""
Config loading robustness: the file users actually edit.

`config/config.yaml` is hand-edited with Notepad and patched with PowerShell —
both are encoding hazards on a Chinese Windows, and each has produced a real
broken startup:

* **BOM** — PowerShell 5.1's `Set-Content -Encoding utf8` writes a BOM; plain
  utf-8 decoding turned it into a YAML ParserError at line 1 and a dead
  assistant (measured 2026-09-29). The loader reads `utf-8-sig` now;
* **not UTF-8 at all** — Notepad's "ANSI" save is GBK, undecodable as UTF-8;
* **structurally broken YAML** — a botched manual edit. The error has to name
  the file and point at 「重新配置」, not spray a raw yaml traceback.
"""

from __future__ import annotations

import shutil
import uuid
from pathlib import Path

import pytest

from winvoice.config import ConfigManager

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

MINIMAL = "tts:\n  speed: 0.9\n  pitch: 1.0\n"


@pytest.fixture
def work():
    """Scratch directory inside the repo (never the system temp dir)."""
    base = PROJECT_ROOT / "runtime" / "_pytest_work"
    base.mkdir(parents=True, exist_ok=True)
    directory = base / uuid.uuid4().hex[:12]
    directory.mkdir(parents=True, exist_ok=True)
    try:
        yield directory
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def _write(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _manager(path: Path) -> ConfigManager:
    return ConfigManager(config_path=path)


def test_a_utf8_bom_is_tolerated(work):
    """`Set-Content -Encoding utf8` on Windows PowerShell 5.1 leaves a BOM."""
    path = _write(work / "config.yaml", b"\xef\xbb\xbf" + MINIMAL.encode("utf-8"))

    manager = _manager(path)

    assert manager.get("tts.speed") == 0.9
    assert manager.get("tts.pitch") == 1.0


def test_a_bom_is_not_passed_through_to_values(work):
    """The BOM must not corrupt the first key's name."""
    path = _write(work / "config.yaml", b"\xef\xbb\xbf" + MINIMAL.encode("utf-8"))

    manager = _manager(path)

    assert manager.get("tts.speed") is not None


def test_gbk_saved_config_gets_a_friendly_error(work):
    """
    Notepad's 「ANSI」 save on a Chinese Windows writes GBK — undecodable as
    UTF-8 as soon as the file carries any Chinese (city, wake words: it always
    does). The error must say what happened and what to do, in Chinese.
    """
    path = _write(work / "config.yaml",
                  (MINIMAL + "weather:\n  city: 北京\n").encode("gbk"))

    with pytest.raises(ValueError) as caught:
        _manager(path)

    message = str(caught.value)
    assert "config.yaml" in message
    assert "UTF-8" in message and "重新" in message


def test_broken_yaml_gets_a_friendly_error_with_the_path(work):
    """A tab in the indentation is the classic botched-edit YAML error."""
    path = _write(work / "config.yaml", b"tts:\n\tspeed: 0.9\n")

    with pytest.raises(ValueError) as caught:
        _manager(path)

    message = str(caught.value)
    assert "config.yaml" in message
    assert "无法解析" in message
    assert "重新配置" in message


def test_a_good_file_still_loads(work):
    path = _write(work / "config.yaml", MINIMAL.encode("utf-8"))

    manager = _manager(path)

    assert manager.get("tts.speed") == 0.9
