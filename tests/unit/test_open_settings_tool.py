"""
The executor side of 「打开配置」 (ToolName.OPEN_SETTINGS).

Four paths, all hermetic — no real server, no real browser:

* a live settings server is reused (`open_window`, no child process);
* no live server → a detached `python -m winvoice.webui` child is spawned and
  success is polled from the handshake state file;
* the child never comes up → the refusal says so instead of claiming success;
* a build without winvoice.webui (old installs) says so honestly.

「已经打开」必须意味着一个窗口真的存在 —— 与 open_app 的诚实规则一致。
"""

from __future__ import annotations

import sys

import pytest

from winvoice.tools import builtin


@pytest.fixture
def webui(monkeypatch):
    """Stub the webui server helpers the handler lazily imports."""
    calls: dict = {"opened": [], "spawned": []}

    def install(live_urls, slow_polls: int = 0):
        state = {"polls": 0, "urls": list(live_urls)}

        def fake_find():
            if state["urls"]:
                return state["urls"][0]
            state["polls"] += 1
            if state["polls"] > slow_polls:
                return "http://127.0.0.1:52888/?token=t"
            return None

        def fake_open(url):
            calls["opened"].append(url)

        def fake_popen(cmd, cwd=None, creationflags=0):
            calls["spawned"].append(list(cmd))

        monkeypatch.setattr("winvoice.webui.server.find_live_instance", fake_find)
        monkeypatch.setattr("winvoice.webui.server.open_window", fake_open)
        monkeypatch.setattr(builtin.subprocess, "Popen", fake_popen)

    return install, calls


@pytest.fixture
def fast_clock(monkeypatch):
    """The 10 s readiness poll runs on a fake clock so tests stay instant."""
    now = {"t": 1000.0}

    def fake_time():
        now["t"] += 1.0
        return now["t"]

    monkeypatch.setattr(builtin.time, "time", fake_time)
    monkeypatch.setattr(builtin.time, "sleep", lambda seconds: None)


def test_a_live_server_is_reused_without_spawning_a_child(webui):
    install, calls = webui
    install(live_urls=["http://127.0.0.1:52888/?token=t"])

    result = builtin.open_settings({})

    assert result["success"] is True
    assert calls["opened"] == ["http://127.0.0.1:52888/?token=t"]
    assert calls["spawned"] == []


def test_no_live_server_spawns_the_child_and_polls_until_ready(webui, fast_clock):
    install, calls = webui
    install(live_urls=[], slow_polls=3)

    result = builtin.open_settings({})

    assert result["success"] is True
    assert len(calls["spawned"]) == 1
    spawned = calls["spawned"][0]
    assert spawned[1:3] == ["-m", "winvoice.webui"]
    # The freshly spawned child opens its own window — opening one here too
    # would put two identical windows on screen.
    assert calls["opened"] == []


def test_a_server_that_never_answers_is_reported_honestly(webui, fast_clock):
    install, calls = webui
    install(live_urls=[], slow_polls=10**6)

    result = builtin.open_settings({})

    assert result["success"] is False
    assert calls["opened"] == []
    assert "设置" in result["message"]
    assert not result["message"].endswith("已经打开设置页面了")


def test_a_build_without_the_webui_says_so(webui, monkeypatch):
    install, calls = webui
    install(live_urls=["http://127.0.0.1:52888/?token=t"])
    # `None` in sys.modules makes the lazy import raise ImportError — the
    # shape an install without winvoice.webui produces.
    monkeypatch.setitem(sys.modules, "winvoice.webui.server", None)

    result = builtin.open_settings({})

    assert result["success"] is False
    assert calls["opened"] == [] and calls["spawned"] == []
    assert "升级" in result["message"]


def test_guests_are_refused_by_the_registry():
    from winvoice.tools.registry import get_tool_registry

    registry = get_tool_registry()
    error = registry.validate_call("open_settings", {}, tier="guest")
    assert error is not None, "the settings page edits the live config: owner-only"
    error_owner = registry.validate_call("open_settings", {}, tier="full")
    assert error_owner is None
