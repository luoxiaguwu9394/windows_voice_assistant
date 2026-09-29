"""
Unit tests for winvoice.config_edit — the settings UI's config writer.

The scalar/list fixtures are cut from the real `config/config.template.yaml`
(where they exist) so a structural drift in the template breaks these tests
instead of breaking the settings window at runtime. The `tools.apps` list of
dicts has no template section yet (this branch adds it), so it uses a
faithful synthetic snippet.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from winvoice.config_edit import save_config, update_keys

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
TEMPLATE = PROJECT_ROOT / "config" / "config.template.yaml"


def rendered_template() -> str:
    """The template as the wizard would render it (every placeholder filled)."""
    text = TEMPLATE.read_text(encoding="utf-8")
    text = text.replace("@KWS_KEYWORDS@", '    - "你好小智"')
    # Remaining tokens sit both quoted and bare; a bare-word replacement is a
    # valid YAML scalar in either position.
    return re.sub(r"@([A-Z_0-9]+)@", r"rendered-\1", text)


def test_scalar_replacement_keeps_comment_and_neighbours():
    text = rendered_template()
    new = update_keys(text, {"tts.speed": 1.05})

    assert new is not None
    assert yaml.safe_load(new)["tts"]["speed"] == 1.05
    # the speed line has no inline comment; the explanation block above it is
    # ordinary neighbouring lines and must survive untouched
    lines_old, lines_new = text.splitlines(), new.splitlines()
    speed_at = next(i for i, line in enumerate(lines_new) if line.strip().startswith("speed:"))
    assert "语速是强杠杆" in "\n".join(lines_new[:speed_at])
    # every other line is byte-identical
    assert [line for line in text.splitlines() if line.strip() != "speed: 0.9"] == [
        line for line in lines_new if line.strip() != "speed: 1.05"
    ]


def test_quoted_string_value_and_its_comment_survive():
    text = rendered_template().replace("@WEATHER_CITY@", "北京")
    new = update_keys(text, {"weather.city": "上海"})

    assert new is not None
    assert yaml.safe_load(new)["weather"]["city"] == "上海"
    city_line = next(line for line in new.splitlines() if line.strip().startswith("city:"))
    assert "used when the sentence does not name a city" in city_line


def test_keyword_list_block_is_replaced_in_place():
    text = rendered_template()
    new = update_keys(text, {"kws.keywords": ["你好小智", "在吗"]})

    assert new is not None
    data = yaml.safe_load(new)
    assert data["kws"]["keywords"] == ["你好小智", "在吗"]
    lines = new.splitlines()
    keywords_at = next(i for i, line in enumerate(lines) if line.strip() == "keywords:")
    threshold_at = next(i for i, line in enumerate(lines) if line.strip().startswith("threshold:"))
    block = lines[keywords_at + 1 : threshold_at]
    assert block == ['    - "你好小智"', '    - "在吗"']
    # the explanation above the block is untouched
    assert any("安装向导把用户挑选的唤醒词" in line for line in lines[:keywords_at])


def test_list_of_dicts_round_trips_through_yaml():
    text = (
        "tools:\n"
        "  confirm_required: true       # comment stays\n"
        "  apps:\n"
        "    - id: wechat\n"
        "      说名: 微信\n"
        "      command: 'C:\\Tencent\\WeChat\\Weixin.exe'\n"
        "      image: Weixin.exe\n"
        "      guest: false\n"
    )
    value = [
        {"id": "wechat", "说名": "微信", "command": "C:\\Tencent\\WeChat\\Weixin.exe",
         "image": "Weixin.exe", "guest": False},
        {"id": "qq", "说名": "QQ", "command": "C:\\Program Files\\Tencent\\QQ.exe",
         "image": "QQ.exe", "guest": False},
    ]
    new = update_keys(text, {"tools.apps": value})

    assert new is not None
    assert yaml.safe_load(new)["tools"]["apps"] == value
    assert "comment stays" in new


def test_empty_list_becomes_inline_empty():
    text = "kws:\n  keywords:\n    - \"你好小智\"\n  threshold: 0.25\n"
    new = update_keys(text, {"kws.keywords": []})

    assert new is not None
    data = yaml.safe_load(new)
    assert data["kws"]["keywords"] == []
    assert data["kws"]["threshold"] == 0.25


def test_unlocatable_path_yields_none_without_partial_edits():
    text = rendered_template()
    new = update_keys(text, {"tts.speed": 1.0, "nope.missing": 1})

    assert new is None


def test_deeper_than_two_levels_is_refused():
    assert update_keys(rendered_template(), {"a.b.c": 1}) is None


def test_crlf_file_keeps_its_endings():
    text = rendered_template().replace("\n", "\r\n")
    new = update_keys(text, {"tts.pitch": 0.9})

    assert new is not None
    assert "\r\n" in new
    assert yaml.safe_load(new)["tts"]["pitch"] == 0.9


def test_save_config_surgical_mode_is_atomic_and_preserves_comments(tmp_path):
    target = tmp_path / "config.yaml"
    target.write_text(rendered_template(), encoding="utf-8")

    result = save_config(target, {"tts.speed": 1.1, "weather.city": "广州"})

    assert result["mode"] == "surgical"
    data = yaml.safe_load(target.read_text(encoding="utf-8"))
    assert data["tts"]["speed"] == 1.1
    assert data["weather"]["city"] == "广州"
    assert "语速是强杠杆" in target.read_text(encoding="utf-8")
    leftovers = [p.name for p in tmp_path.iterdir() if p.name != "config.yaml"]
    assert leftovers == []  # no temp files left behind


def test_save_config_falls_back_to_full_dump_when_structure_is_unknown(tmp_path):
    target = tmp_path / "config.yaml"
    target.write_text("tts: [broken, structure]\n", encoding="utf-8")

    result = save_config(target, {"tts.speed": 1.1})

    assert result["mode"] == "full"
    assert yaml.safe_load(target.read_text(encoding="utf-8"))["tts"]["speed"] == 1.1
