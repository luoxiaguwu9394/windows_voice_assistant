"""
Regression: closing File Explorer must not take the Windows desktop with it.

Reported symptom (live, 2026-09-20): saying 「关闭文件资源管理器」 ran
`taskkill /f /im explorer.exe`, and the desktop, taskbar and Start menu all
disappeared. `explorer.exe` is not an application — it is the Windows **shell**,
and the folder windows are only one of the things it owns.

Two independent defences are pinned here, because either one alone would be a
single point of failure:

1. `close_app` routes Explorer to the window-closing path and never builds a
   `taskkill` command for it.
2. `PROTECTED_PROCESSES` refuses to force-kill shell and session processes even
   if a future allowlist edit points at one.

The third property is about `force`: `taskkill /f` skips the application's own
"save changes?" prompt, so it may only be used when the request explicitly asked
for it. 「关闭记事本」 must not silently discard unsaved work.
"""

from __future__ import annotations

from typing import List, Optional

import pytest

import winvoice.tools.builtin as builtin
from winvoice.tools.verifier import (
    CloseAppVerifier,
    VerificationStatus,
    default_verifiers,
)


class RecordedRun:
    """Stands in for `subprocess.run`, remembering the command it was given."""

    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = ""):
        self.commands: List[List[str]] = []
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr

    def __call__(self, cmd, *args, **kwargs):
        self.commands.append(list(cmd))

        class Done:
            pass

        done = Done()
        done.returncode = self.returncode  # type: ignore[attr-defined]
        done.stdout = self.stdout  # type: ignore[attr-defined]
        done.stderr = self.stderr  # type: ignore[attr-defined]
        return done

    @property
    def taskkill_commands(self) -> List[List[str]]:
        return [c for c in self.commands if c and c[0] == "taskkill"]


@pytest.fixture
def explorer_closed(monkeypatch):
    """
    Stub Explorer's automation object.

    `close_windows` shells out to PowerShell, which is not something a unit test
    should do — and the property under test is *which* mechanism is chosen, not
    that COM works (which was measured by hand against the live shell).
    """
    calls: List[bool] = []

    def fake_close(timeout_s: float = 15.0):
        calls.append(True)
        return 0, None  # no windows remaining

    monkeypatch.setattr(builtin, "close_explorer_windows", fake_close)
    return calls


# ──────────────────────────────────────────────────────────────
# Explorer is the shell
# ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "spoken",
    ["资源管理器", "文件管理器", "我的电脑", "此电脑", "文件夹", "explorer"],
)
def test_closing_explorer_never_calls_taskkill(spoken: str, monkeypatch, explorer_closed) -> None:
    """Every alias of Explorer must route to the window path — not one of them."""
    runner = RecordedRun()
    monkeypatch.setattr(builtin.subprocess, "run", runner)

    result = builtin.close_app({"app": spoken})

    assert runner.taskkill_commands == [], f"taskkill was used for {spoken!r}"
    assert explorer_closed, "the window-closing path was not taken"
    assert result["success"] is True
    assert result["windows_remaining"] == 0


def test_closing_explorer_ignores_force(monkeypatch, explorer_closed) -> None:
    """
    Even 「强制关闭资源管理器」 must not force the shell down.

    The request to force is respected for a normal application; for Explorer it
    cannot be honoured at all, because the only way to "force" it is to kill the
    shell. Closing the windows is the closest honest interpretation.
    """
    runner = RecordedRun()
    monkeypatch.setattr(builtin.subprocess, "run", runner)

    result = builtin.close_app({"app": "资源管理器", "force": True})

    assert runner.taskkill_commands == []
    assert result["success"] is True


def test_a_protected_process_is_refused_outright(monkeypatch) -> None:
    """A backstop for the day someone adds a shell-adjacent name to the allowlist."""
    runner = RecordedRun()
    monkeypatch.setattr(builtin.subprocess, "run", runner)
    monkeypatch.setitem(builtin.ALLOWED_APPS, "winlogon_probe", "winlogon.exe")

    result = builtin.close_app({"app": "winlogon_probe", "force": True})

    assert runner.taskkill_commands == []
    assert result["success"] is False
    assert result["message"].isascii() is False  # speakable Chinese


def test_every_protected_process_is_a_real_system_image() -> None:
    """A typo in the list would silently protect nothing."""
    assert "explorer.exe" in builtin.PROTECTED_PROCESSES
    for name in builtin.PROTECTED_PROCESSES:
        assert name.endswith(".exe"), name


# ──────────────────────────────────────────────────────────────
# Force is opt-in
# ──────────────────────────────────────────────────────────────

def test_plain_close_does_not_force(monkeypatch) -> None:
    """「关闭记事本」 must let Notepad ask about unsaved work."""
    runner = RecordedRun()
    monkeypatch.setattr(builtin.subprocess, "run", runner)

    result = builtin.close_app({"app": "记事本"})

    (command,) = runner.taskkill_commands
    assert "/f" not in command, f"a plain close force-killed: {command}"
    assert command == ["taskkill", "/im", "notepad.exe"]
    assert result["success"] is True


def test_explicit_force_does_force(monkeypatch) -> None:
    """「强制关闭记事本」 is a different request and gets /f."""
    runner = RecordedRun()
    monkeypatch.setattr(builtin.subprocess, "run", runner)

    builtin.close_app({"app": "记事本", "force": True})

    (command,) = runner.taskkill_commands
    assert command == ["taskkill", "/f", "/im", "notepad.exe"]


def test_a_waiting_application_is_described_as_waiting(monkeypatch) -> None:
    """A graceful close on dirty state waits for the user; say so, don't cry fault."""
    import subprocess

    def timeout(cmd, *args, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 12)

    monkeypatch.setattr(builtin.subprocess, "run", timeout)

    result = builtin.close_app({"app": "记事本"})

    assert result["success"] is False
    assert result["message"].isascii() is False
    assert "等你确认" in result["message"] or "保存" in result["message"]


def test_the_agent_is_told_force_is_opt_in() -> None:
    """
    The model reads the schema, so the rule has to be in the schema text.

    Without this the agent would helpfully set `force: true` for any
    「关闭 X」 and quietly discard the user's unsaved work.
    """
    from winvoice.tools.registry import get_tool_registry
    from winvoice.contracts import ToolName

    spec = get_tool_registry().get(ToolName.CLOSE_APP)

    assert "force" in spec.schema.properties
    assert spec.schema.properties["force"]["type"] == "boolean"
    assert "ONLY" in spec.description or "only" in spec.description
    assert "unsaved" in spec.description.lower()


# ──────────────────────────────────────────────────────────────
# Spoken force words reach the tool
# ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text", ["强制关闭记事本", "强行关闭记事本", "强制关闭"])
def test_spoken_force_words_set_the_flag(text: str) -> None:
    from winvoice.intent.rules import match_rules

    result = match_rules(text)

    assert result is not None
    assert result.intent.value == "close_app"
    assert result.args.get("force") is True


@pytest.mark.parametrize("text", ["关闭记事本", "退出记事本", "关闭计算器"])
def test_plain_close_words_do_not_set_the_flag(text: str) -> None:
    from winvoice.intent.rules import match_rules

    result = match_rules(text)

    assert result is not None
    assert "force" not in result.args, f"{text!r} was read as a forced close"


# ──────────────────────────────────────────────────────────────
# The verifier checks the user's goal, not the mechanism
# ──────────────────────────────────────────────────────────────

class ExplorerFakeProbe:
    def __init__(self, windows: Optional[int]):
        self.windows = windows

    def running_processes(self):
        return {"explorer.exe"}  # the shell is always alive; that is the point

    def explorer_window_count(self):
        return self.windows


def test_explorer_verifier_checks_windows_not_the_process() -> None:
    """
    The old check — "explorer.exe is absent" — was satisfied by killing the
    shell, so it certified the very outcome the user was complaining about.
    """
    verifier = CloseAppVerifier()

    assert verifier.verify({"app": "资源管理器"}, {"success": True}, None, ExplorerFakeProbe(0)).status is (
        VerificationStatus.VERIFIED
    )
    assert verifier.verify({"app": "资源管理器"}, {"success": True}, None, ExplorerFakeProbe(2)).status is (
        VerificationStatus.FAILED
    )


def test_explorer_verifier_admits_when_it_cannot_observe() -> None:
    result = CloseAppVerifier().verify(
        {"app": "资源管理器"}, {"success": True}, None, ExplorerFakeProbe(None)
    )

    assert result.status is VerificationStatus.NOT_VERIFIABLE


def test_close_app_still_has_a_verifier() -> None:
    assert "close_app" in default_verifiers()


# ── launch command ≠ process image (live report 2026-09-27) ───────────────


def test_closing_vscode_kills_the_real_process_image(monkeypatch) -> None:
    """
    「关闭代码编辑器」 used to answer 「我关不掉代码编辑器。」 without ever
    building a `taskkill`: the handler read the *launch command* (`code`, the
    shim) where it needed the *process image* (`Code.exe`). Opening worked
    because the launch path resolves through App Paths — only closing was dead.
    """
    runner = RecordedRun()
    monkeypatch.setattr(builtin.subprocess, "run", runner)

    result = builtin.close_app({"app": "代码编辑器"})

    assert result["success"] is True, result
    assert runner.taskkill_commands == [["taskkill", "/im", "Code.exe"]], runner.taskkill_commands


def test_closing_vscode_honours_an_explicit_force(monkeypatch) -> None:
    runner = RecordedRun()
    monkeypatch.setattr(builtin.subprocess, "run", runner)

    builtin.close_app({"app": "vscode", "force": True})

    assert runner.taskkill_commands == [["taskkill", "/f", "/im", "Code.exe"]]


def test_closing_vscode_when_it_is_not_running_is_honest(monkeypatch) -> None:
    runner = RecordedRun(returncode=128, stderr='错误: 没有找到进程 "Code.exe"。')
    monkeypatch.setattr(builtin.subprocess, "run", runner)

    result = builtin.close_app({"app": "代码编辑器"})

    assert result["success"] is False
    assert "好像没有在运行" in result["message"]


def test_the_verifier_watches_the_same_image_the_tool_kills() -> None:
    """Open and close verification must agree with the real process name."""
    from winvoice.tools.verifier import _expected_image

    assert _expected_image("代码编辑器") == "code.exe"


def test_the_verifier_certifies_vscode_closed(monkeypatch) -> None:
    from winvoice.tools.state_capture import WindowsProbe  # noqa: F401  (import shape check)

    verifier = CloseAppVerifier()

    class Probe:
        def __init__(self, processes):
            self._processes = {p.lower() for p in processes}

        def running_processes(self):
            return set(self._processes)

    # Still running -> FAILED with the real image name in the reason.
    verdict = verifier.verify({"app": "代码编辑器"}, {"success": False}, None, Probe({"Code.exe"}))
    assert verdict.status is VerificationStatus.FAILED
    assert "code.exe" in verdict.reason

    # Gone -> VERIFIED, even though the graceful taskkill may have reported
    # a non-zero exit (the postcondition is what the verifier is told to check).
    verdict = verifier.verify({"app": "代码编辑器"}, {"success": False}, None, Probe(set()))
    assert verdict.status is VerificationStatus.VERIFIED
