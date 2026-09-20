"""
The MCP bridge, exercised over a real stdio pipe.

This is the contract that matters for the DeepSeek Harness integration: DSH
spawns `python -m winvoice.mcp_server` as a child process and talks JSON-RPC to
it, so the thing worth testing is the *process boundary* — handshake, tool list,
tool call — not an in-process call to the adapter class. An adapter that works
when called directly can still fail to start as a program (a stray import that
writes to stdout, a config lookup that assumes a different working directory).

Two behaviours are pinned beyond the happy path, because both are load-bearing:

* a refused call comes back as `isError`, so the model cannot report success for
  something that did not happen;
* the speaker tier published by the audio process is honoured *inside the child
  process*, which is the only reason `guest` is enforced at all once tools run
  behind DSH.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

import pytest

from winvoice import _vendor

# `mcp` is an optional dependency (installed into `.pylibs`, deliberately not in
# `requirements.txt` — see deployment.md §2.4). Skip rather than error when it is
# absent, like every other test in this repo that needs a resource that may not
# be installed.
_vendor.ensure("mcp")
pytest.importorskip("mcp", reason="needs the optional `mcp` package (deployment.md §2.4)")

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
PROTOCOL_VERSION = "2024-11-05"
HANDSHAKE_TIMEOUT_S = 40.0


class McpChild:
    """Minimal MCP-over-stdio client: enough for initialize/list/call."""

    def __init__(self, utterance_file: Optional[Path] = None):
        env = dict(os.environ)
        env["WINVOICE_CONSOLE_LOG"] = "0"
        if utterance_file is not None:
            env["WINVOICE_UTTERANCE_FILE"] = str(utterance_file)

        self.proc = subprocess.Popen(
            [sys.executable, "-m", "winvoice.mcp_server"],
            cwd=str(REPO_ROOT),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
        self._next_id = 0

    # ── transport ──────────────────────────────────────────────

    def send(self, method: str, params: Optional[Dict[str, Any]] = None) -> int:
        self._next_id += 1
        message = {"jsonrpc": "2.0", "id": self._next_id, "method": method, "params": params or {}}
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(message) + "\n")
        self.proc.stdin.flush()
        return self._next_id

    def notify(self, method: str, params: Optional[Dict[str, Any]] = None) -> None:
        message = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(message) + "\n")
        self.proc.stdin.flush()

    def await_id(self, wanted: int) -> Dict[str, Any]:
        """Read lines until the response carrying `wanted` arrives."""
        assert self.proc.stdout is not None
        deadline = time.monotonic() + HANDSHAKE_TIMEOUT_S
        while time.monotonic() < deadline:
            line = self.proc.stdout.readline()
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                # Anything that is not JSON on stdout is a protocol violation:
                # a logger or a print() escaping onto the wire.
                raise AssertionError(f"non-JSON on stdout: {line[:200]!r}") from exc
            if payload.get("id") == wanted:
                return payload
        raise AssertionError(f"no reply to id={wanted}; stderr={self.stderr_tail()}")

    def call(self, method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return self.await_id(self.send(method, params))

    def stderr_tail(self, limit: int = 800) -> str:
        try:
            if self.proc.poll() is not None and self.proc.stderr is not None:
                return self.proc.stderr.read()[-limit:]
        except Exception:
            pass
        return "<no stderr>"

    def close(self) -> None:
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
            self.proc.wait(timeout=10)
        except Exception:
            self.proc.kill()


@pytest.fixture
def child():
    instance = McpChild()
    try:
        yield instance
    finally:
        instance.close()


def _handshake(child: McpChild) -> Dict[str, Any]:
    reply = child.call(
        "initialize",
        {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "winvoice-tests", "version": "0.1"},
        },
    )
    child.notify("notifications/initialized")
    return reply


# ──────────────────────────────────────────────────────────────
# Protocol
# ──────────────────────────────────────────────────────────────

def test_server_completes_the_mcp_handshake(child: McpChild) -> None:
    reply = _handshake(child)

    assert "error" not in reply, reply
    result = reply["result"]
    assert result["serverInfo"]["name"] == "winvoice"
    assert "tools" in result["capabilities"]


def test_tools_list_exposes_the_registry(child: McpChild) -> None:
    _handshake(child)

    reply = child.call("tools/list", {})
    tools = {t["name"]: t for t in reply["result"]["tools"]}

    assert set(tools) == {
        "open_app",
        "close_app",
        "set_volume",
        "media_control",
        "search_web",
        "read_file",
        "write_file",
        "run_script",
        "get_time",
        "get_weather",
    }
    # The schema the model sees must carry the cross-field rule, or the model
    # has no way to know `set_volume` accepts `level` *or* `delta`.
    assert tools["set_volume"]["inputSchema"]["anyOf"] == [
        {"required": ["level"]},
        {"required": ["delta"]},
    ]


def test_unknown_tool_is_reported_as_an_error(child: McpChild) -> None:
    _handshake(child)

    reply = child.call("tools/call", {"name": "definitely_not_a_tool", "arguments": {}})

    assert reply["result"]["isError"] is True


# ──────────────────────────────────────────────────────────────
# Execution
# ──────────────────────────────────────────────────────────────

def test_a_real_tool_runs_through_the_bridge(child: McpChild) -> None:
    """`get_time` is the one tool with no side effects and no network."""
    _handshake(child)

    reply = child.call("tools/call", {"name": "get_time", "arguments": {}})

    result = reply["result"]
    assert result.get("isError") in (False, None), result
    payload = json.loads(result["content"][0]["text"])
    assert payload["success"] is True
    # `speak` is what the assistant will say; it must be the Chinese sentence
    # the tool authored, not something the model has to invent.
    assert "现在" in payload["speak"]


def test_missing_required_argument_is_refused(child: McpChild) -> None:
    _handshake(child)

    reply = child.call("tools/call", {"name": "open_app", "arguments": {}})

    result = reply["result"]
    assert result["isError"] is True
    payload = json.loads(result["content"][0]["text"])
    assert payload["success"] is False
    assert "app" in payload["error"]


# ──────────────────────────────────────────────────────────────
# Tier enforcement across the process boundary
# ──────────────────────────────────────────────────────────────

def test_guest_tier_is_enforced_inside_the_child_process() -> None:
    """
    A `guest` must not be able to read files just because the call arrives from
    the agent rather than from the audio loop.

    The tier travels through the in-flight utterance file, which is the only
    channel that survives DSH's process boundary; without it, DSH becomes a way
    around the permission model instead of a user of it.
    """
    utterance_file = REPO_ROOT / "runtime" / "test_utterance_guest.json"
    utterance_file.parent.mkdir(parents=True, exist_ok=True)
    utterance_file.write_text(
        json.dumps(
            {"trace_id": "t-guest", "tier": "guest", "expires_at": time.time() + 300}
        ),
        encoding="utf-8",
    )

    child = McpChild(utterance_file=utterance_file)
    try:
        _handshake(child)
        reply = child.call(
            "tools/call",
            {"name": "read_file", "arguments": {"path": "C:/Users/someone/notes.txt"}},
        )

        result = reply["result"]
        assert result["isError"] is True
        payload = json.loads(result["content"][0]["text"])
        assert payload["success"] is False
        assert "guest" in payload["error"].lower()
        # The refusal must be speakable Chinese, not the English reason key.
        assert payload["speak"].isascii() is False
    finally:
        child.close()
        utterance_file.unlink(missing_ok=True)


def test_full_tier_is_not_restricted_by_the_same_path() -> None:
    """The control for the test above: same call, owner tier, different answer."""
    utterance_file = REPO_ROOT / "runtime" / "test_utterance_full.json"
    utterance_file.parent.mkdir(parents=True, exist_ok=True)
    utterance_file.write_text(
        json.dumps(
            {"trace_id": "t-full", "tier": "full", "expires_at": time.time() + 300}
        ),
        encoding="utf-8",
    )

    child = McpChild(utterance_file=utterance_file)
    try:
        _handshake(child)
        reply = child.call(
            "tools/call",
            {"name": "read_file", "arguments": {"path": "C:/Users/someone/definitely_absent.txt"}},
        )

        payload = json.loads(reply["result"]["content"][0]["text"])
        # A missing file is a failure, but *not* a permission failure.
        assert payload["success"] is False
        assert "guest" not in payload["error"].lower()
    finally:
        child.close()
        utterance_file.unlink(missing_ok=True)


# ──────────────────────────────────────────────────────────────
# The two halves speak one dialect
# ──────────────────────────────────────────────────────────────

def test_the_escalation_reader_understands_what_the_bridge_writes() -> None:
    """
    The bridge and the escalation policy are separate modules joined only by a
    payload shape: `mcp_server.render_tool_result` writes it, and
    `dsh.validation.extract_tool_activity` reads it back out of a DSH session
    event. Nothing else enforces that agreement, and a silent drift would mean
    escalation stops firing — quietly, because every turn would simply look fine.

    So the reader is pointed at the *real* wire bytes a real child process
    produced, not at a fixture someone wrote to match.
    """
    from winvoice.dsh.validation import assess_turn, extract_tool_activity

    child = McpChild()
    try:
        _handshake(child)
        reply = child.call("tools/call", {"name": "get_time", "arguments": {}})

        wire_text = reply["result"]["content"][0]["text"]
        # A session event as DSH delivers it: the payload arrives inside an
        # event tree, which is exactly how the extractor has to find it.
        events = [{"type": "tool/result", "data": {"content": [{"type": "text", "text": wire_text}]}}]

        activity = extract_tool_activity(events)
        assert len(activity) == 1, "the bridge's payload was not recognised"
        assert activity[0].success is True
        assert activity[0].speak and "现在" in activity[0].speak

        # A clean tool turn resolves, so it does not escalate.
        assessment = assess_turn("现在是下午三点。", "completed", events)
        assert assessment.resolved is True
        assert assessment.verification_failures == []
    finally:
        child.close()


def test_a_refused_call_reaches_the_reader_without_a_verification_verdict() -> None:
    """
    A refusal (here: a missing argument) must reach the escalation policy as
    "no verdict", not as an observed mismatch — a deterministic refusal is not
    worth a cloud round trip, because the cloud model is refused identically.
    """
    from winvoice.dsh.validation import assess_turn, extract_tool_activity

    child = McpChild()
    try:
        _handshake(child)
        reply = child.call("tools/call", {"name": "open_app", "arguments": {}})

        assert reply["result"]["isError"] is True
        wire_text = reply["result"]["content"][0]["text"]
        events = [{"type": "tool/result", "data": {"content": [{"type": "text", "text": wire_text}]}}]

        activity = extract_tool_activity(events)
        assert len(activity) == 1
        assert activity[0].success is False
        assert activity[0].verification_status is None

        assessment = assess_turn("这个请求缺少必要的信息。", "completed", events)
        assert assessment.resolved is True
        assert assessment.reason is None
    finally:
        child.close()
