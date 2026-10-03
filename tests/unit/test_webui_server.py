"""
The settings server, driven for real: a ThreadingHTTPServer on an ephemeral
port, a temp config file, a temp state file. Covers the token gate, the
managed-key boundary, save→file semantics, effect labels and the
single-instance handshake.
"""

from __future__ import annotations

import json
import threading
import urllib.request
from pathlib import Path

import pytest
import yaml

from winvoice.webui import server as webui

CONFIG_TEXT = """\
# 顶层注释必须在
tts:
  speed: 0.9                   # 语速是强杠杆
  pitch: 1.0
  guest_speed: 1.0
kws:
  keywords:
    - "你好小智"
weather:
  enabled: true
  city: "北京"                  # used when the sentence does not name a city
tools:
  apps: []
  sensitive_apps:
    - "cmd"
llm:
  remote:
    api_key: "${REMOTE_API_KEY}"   # must never leave the process
"""


@pytest.fixture
def server(tmp_path, monkeypatch):
    config = tmp_path / "config.yaml"
    config.write_text(CONFIG_TEXT, encoding="utf-8")
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("<html><body>设置</body></html>", encoding="utf-8")

    instance = webui.SettingsServer(
        config_path=config, state_path=tmp_path / "runtime" / "webui.json", static_dir=static,
    )
    instance.bind()
    thread = threading.Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    yield instance
    instance.shutdown()
    thread.join(timeout=5)


def _get(url: str) -> tuple[int, dict | bytes]:
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            body = response.read()
            status = response.status
    except urllib.error.HTTPError as error:
        body, status = error.read(), error.code
    try:
        return status, json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return status, body


def _api(server, path: str) -> str:
    """Authorized API URL for `path` (e.g. "/api/v1/config")."""
    base = server.url.split("?")[0]
    return f"{base}{path}?token={server.token}"


def _post(server, path: str, payload: dict) -> tuple[int, dict]:
    request = urllib.request.Request(
        _api(server, path), data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read().decode("utf-8"))


def test_requests_without_a_valid_token_are_refused(server):
    base = server.url.split("?")[0]
    status, _ = _get(f"{base}api/v1/config")
    assert status == 403
    status, _ = _get(f"{base}api/v1/config?token=wrong")
    assert status == 403


def test_meta_reports_effect_labels(server):
    status, payload = _get(_api(server, "/api/v1/meta"))
    assert status == 200
    assert payload["effects"]["weather.city"] == "hot"
    assert payload["effects"]["tts.speed"] == "restart"


def test_config_read_returns_managed_values_only(server):
    status, payload = _get(_api(server, "/api/v1/config"))
    assert status == 200
    values = payload["values"]
    assert values["tts.speed"] == 0.9
    assert values["weather.city"] == "北京"
    assert values["kws.keywords"] == ["你好小智"]
    # the ${VAR} credential placeholder stays inside the process
    assert all("api_key" not in key for key in values)


def test_post_change_writes_the_file_and_labels_the_effect(server):
    status, payload = _post(server, "/api/v1/config",
                            {"changes": {"tts.speed": 1.05, "weather.city": "上海"}})
    assert status == 200 and payload["saved"] is True
    assert payload["mode"] == "surgical"
    assert payload["effects"] == {"tts.speed": "restart", "weather.city": "hot"}

    text = server.config_path.read_text(encoding="utf-8")
    data = yaml.safe_load(text)
    assert data["tts"]["speed"] == 1.05
    assert data["weather"]["city"] == "上海"
    assert "语速是强杠杆" in text  # comments survived the save


def test_post_rejects_unknown_keys_and_bad_values(server):
    status, payload = _post(server, "/api/v1/config", {"changes": {"llm.remote.api_key": "steal"}})
    assert status == 400 and any("unknown key" in e for e in payload["errors"])

    status, payload = _post(server, "/api/v1/config", {"changes": {"tts.speed": 99}})
    assert status == 400 and any("out of range" in e for e in payload["errors"])

    status, payload = _post(server, "/api/v1/config", {"changes": {"kws.keywords": []}})
    assert status == 400 and any("1-8 keywords" in e for e in payload["errors"])


def test_post_writes_a_config_app_list(server):
    entry = {"id": "wechat", "label": "微信", "command": r"C:\Tencent\Weixin.exe",
             "image": "Weixin.exe", "guest": False}

    status, payload = _post(server, "/api/v1/config", {"changes": {"tools.apps": [entry]}})
    assert status == 200 and payload["effects"]["tools.apps"] == "hot"

    data = yaml.safe_load(server.config_path.read_text(encoding="utf-8"))
    assert data["tools"]["apps"] == [entry]


def test_apps_scan_endpoint_returns_candidates(server, monkeypatch):
    candidates = [{"label": "微信", "path": r"C:\T\Weixin.exe", "image": "Weixin.exe"}]
    monkeypatch.setattr(webui.appdiscovery, "scan_installed_apps", lambda: candidates)

    status, payload = _get(_api(server, "/api/v1/apps/scan"))

    assert status == 200 and payload["candidates"] == candidates


def test_post_dedupes_wake_words(server):
    status, payload = _post(server, "/api/v1/config",
                            {"changes": {"kws.keywords": ["小云", "小云", "你好小云", " 小云 "]}})
    assert status == 200

    data = yaml.safe_load(server.config_path.read_text(encoding="utf-8"))
    assert data["kws"]["keywords"] == ["小云", "你好小云"]


def test_config_read_of_a_broken_file_is_a_readable_500(server):
    server.config_path.write_text("kws:\n  keywords:\n  - 旧词\n  threshold: [", encoding="utf-8")

    status, payload = _get(_api(server, "/api/v1/config"))

    assert status == 500
    assert "YAML" in payload["error"]


def test_post_onto_a_broken_file_is_refused_and_writes_nothing(server):
    server.config_path.write_text("kws:\n  keywords:\n  - 旧词\n  threshold: [", encoding="utf-8")
    before = server.config_path.read_bytes()

    status, payload = _post(server, "/api/v1/config", {"changes": {"weather.city": "上海"}})

    assert status == 500
    assert "error" in payload
    assert server.config_path.read_bytes() == before  # disk untouched


def test_static_index_is_served(server):
    with urllib.request.urlopen(server.url.split("?")[0], timeout=5) as response:
        body = response.read().decode("utf-8")
    assert "设置" in body


def test_find_live_instance_finds_a_running_server(server, tmp_path):
    server.write_state()
    assert webui.find_live_instance(server.state_path) == server.url


def test_find_live_instance_ignores_a_stale_state_file(tmp_path):
    stale = tmp_path / "webui.json"
    stale.write_text(json.dumps({"port": 1, "token": "x"}), encoding="utf-8")

    assert webui.find_live_instance(stale) is None


def test_index_serves_with_cwd_relative_paths(tmp_path, monkeypatch):
    """The shipped entry point runs from the install root with CWD-relative
    defaults — a relative static base must still pass the traversal guard."""
    import os

    monkeypatch.chdir(tmp_path)
    (tmp_path / "static").mkdir()
    (tmp_path / "static" / "index.html").write_text("<html>ok</html>", encoding="utf-8")
    (tmp_path / "config.yaml").write_text("weather:\n  city: 北京\n", encoding="utf-8")

    instance = webui.SettingsServer(
        config_path=Path("config.yaml"), state_path=Path("runtime/webui.json"),
        static_dir=Path("static"),
    )
    instance.bind()
    thread = threading.Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    try:
        base = instance.url.split("?")[0]
        with urllib.request.urlopen(base, timeout=5) as response:
            assert b"ok" in response.read()
        status, payload = _get(_api(instance, "/api/v1/config"))
        assert status == 200 and payload["values"]["weather.city"] == "北京"
    finally:
        instance.shutdown()
        thread.join(timeout=5)
