"""
The settings UI's app table: `tools.apps` config drives `open_app`/`close_app`
and the verifiers, built-ins cannot be shadowed, and `tools.sensitive_apps`
shuts the door for every tier below full.

These tests drive the real handlers with the launcher stubbed (same pattern as
test_app_launch.py), so nothing is actually opened. The config side is
injected via `builtin._config_apps` — the get_config() wiring itself is covered
by the repo config load in the full suite.
"""

from __future__ import annotations

import pytest

from winvoice.tools import builtin
from winvoice.tools.registry import ToolName, ToolRegistry


@pytest.fixture
def no_config(monkeypatch):
    """No settings-UI apps at all: built-ins only, sensitive gate at default."""
    monkeypatch.setattr(builtin, "_config_apps", lambda: [])
    monkeypatch.setattr(builtin, "sensitive_app_ids", lambda: {"cmd", "powershell"})


@pytest.fixture
def launcher(monkeypatch):
    """Record `_launch` calls instead of starting anything."""
    launched: list[str] = []
    monkeypatch.setattr(builtin, "_launch", lambda path: launched.append(path))
    return launched


class _FakeProc:
    returncode = 0
    stdout = ""
    stderr = ""


# ── current_apps: merging, validation, shadowing ─────────────────────────────


def test_config_apps_extend_the_builtins(monkeypatch):
    monkeypatch.setattr(builtin, "_config_apps", lambda: [
        {"id": "wechat", "label": "微信", "command": r"C:\Tencent\Weixin.exe"},
    ])
    apps = builtin.current_apps()

    assert set(builtin.BUILTIN_APPS) <= set(apps)
    assert apps["wechat"].label == "微信"
    assert apps["wechat"].guest is False  # custom apps default host-only


def test_a_config_entry_cannot_shadow_a_builtin(monkeypatch):
    monkeypatch.setattr(builtin, "_config_apps", lambda: [
        {"id": "cmd", "label": "假命令提示符", "command": r"C:\evil\evil.exe"},
    ])
    apps = builtin.current_apps()

    assert apps["cmd"].command == "cmd.exe"
    assert apps["cmd"].label == "命令提示符"


def test_malformed_config_entries_are_skipped(monkeypatch):
    monkeypatch.setattr(builtin, "_config_apps", lambda: [
        "not a dict",
        {"id": "", "command": "x.exe"},
        {"id": "wechat"},  # no command
        {"id": "qq", "label": "QQ", "command": r"C:\QQ.exe"},
    ])
    apps = builtin.current_apps()

    assert set(apps) == set(builtin.BUILTIN_APPS) | {"qq"}


def test_sensitive_ids_fail_closed_without_config(monkeypatch):
    """No config at all → the shipped default gate stands (cmd, powershell)."""
    import winvoice.config

    def boom(*args, **kwargs):
        raise RuntimeError("no config")

    monkeypatch.setattr(winvoice.config, "get_config", boom)

    assert builtin.sensitive_app_ids() == {"cmd", "powershell"}


# ── resolve_app: spoken names reach config apps ──────────────────────────────


def test_spoken_label_reaches_a_config_app(monkeypatch, no_config):
    monkeypatch.setattr(builtin, "_config_apps", lambda: [
        {"id": "wechat", "label": "微信", "command": r"C:\Tencent\Weixin.exe"},
    ])
    assert builtin.resolve_app("微信") == "wechat"
    assert builtin.resolve_app("wechat") == "wechat"
    assert builtin.resolve_app("微信吧") == "wechat"  # trailing particle ignored


def test_unknown_app_still_refused(monkeypatch, no_config):
    monkeypatch.setattr(builtin, "_config_apps", lambda: [
        {"id": "wechat", "label": "微信", "command": r"C:\Tencent\Weixin.exe"},
    ])
    assert builtin.resolve_app("网易云音乐") is None


# ── open_app / close_app with config entries ────────────────────────────────


def test_open_app_launches_an_absolute_config_path(monkeypatch, no_config, launcher, tmp_path):
    exe = tmp_path / "Weixin.exe"
    exe.write_bytes(b"MZ")
    monkeypatch.setattr(builtin, "_config_apps", lambda: [
        {"id": "wechat", "label": "微信", "command": str(exe)},
    ])

    result = builtin.open_app({"app": "微信"})

    assert result["success"] is True
    assert "已经打开微信了" in result["message"]
    assert launcher == [str(exe)]


def test_open_app_reports_a_vanished_path_honestly(monkeypatch, no_config):
    monkeypatch.setattr(builtin, "_config_apps", lambda: [
        {"id": "wechat", "label": "微信", "command": r"C:\Tencent\Gone\Weixin.exe"},
    ])

    result = builtin.open_app({"app": "微信"})

    assert result["success"] is False
    assert "我没找到微信的安装位置" in result["message"]


def test_close_app_uses_the_configured_process_image(monkeypatch, no_config, tmp_path):
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return _FakeProc()

    monkeypatch.setattr(builtin, "_config_apps", lambda: [
        {"id": "wechat", "label": "微信", "command": r"C:\Tencent\Weixin.exe"},
    ])
    monkeypatch.setattr(builtin.subprocess, "run", fake_run)

    result = builtin.close_app({"app": "微信"})

    assert result["success"] is True
    assert calls == [["taskkill", "/im", "Weixin.exe"]]


def test_close_app_infers_the_image_from_an_exe_command(monkeypatch, no_config):
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return _FakeProc()

    monkeypatch.setattr(builtin, "_config_apps", lambda: [
        {"id": "qq", "label": "QQ", "command": r"C:\Tools\QQ.exe"},
    ])
    monkeypatch.setattr(builtin.subprocess, "run", fake_run)

    builtin.close_app({"app": "QQ"})

    assert calls == [["taskkill", "/im", "QQ.exe"]]


def test_verifier_sees_the_configured_image(monkeypatch, no_config):
    monkeypatch.setattr(builtin, "_config_apps", lambda: [
        {"id": "wechat", "label": "微信", "command": r"C:\Tencent\Weixin.exe"},
    ])
    from winvoice.tools.verifier import _expected_image

    assert _expected_image("微信") == "weixin.exe"
    assert _expected_image("不存在的应用") is None


# ── the sensitive gate in registry.validate_call ─────────────────────────────


@pytest.fixture
def registry():
    return ToolRegistry()


def test_guest_cannot_open_a_sensitive_builtin(registry, no_config):
    error = registry.validate_call(ToolName.OPEN_APP, {"app": "命令提示符"}, tier="guest")

    assert error is not None and "not allowed for guest tier" in error


def test_full_tier_opens_sensitive_apps(registry, no_config):
    assert registry.validate_call(ToolName.OPEN_APP, {"app": "cmd"}, tier="full") is None


def test_guest_cannot_open_a_sensitive_config_app(monkeypatch, registry, no_config):
    monkeypatch.setattr(builtin, "_config_apps", lambda: [
        {"id": "wechat", "label": "微信", "command": r"C:\Tencent\Weixin.exe",
         "guest": True},
    ])
    monkeypatch.setattr(builtin, "sensitive_app_ids", lambda: {"wechat"})

    error = registry.validate_call(ToolName.OPEN_APP, {"app": "微信"}, tier="guest")

    assert error is not None and "wechat is sensitive" in error


def test_guest_still_opens_a_normal_app(registry, no_config):
    assert registry.validate_call(ToolName.OPEN_APP, {"app": "记事本"}, tier="guest") is None


def test_sensitive_gate_covers_close_app_too(registry, no_config):
    error = registry.validate_call(ToolName.CLOSE_APP, {"app": "powershell"}, tier="guest")

    assert error is not None and "not allowed for guest tier" in error
