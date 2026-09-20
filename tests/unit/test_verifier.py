"""
The verifier contract: the machine decides, not the model.

These tests pin the three decisions that make the verifier worth having, each
of which is easy to undo by accident:

1. `VERIFIED` may coexist with `success=False`. `close_app` legitimately reports
   「好像没有在运行。」 (a truthful refusal) while the *postcondition* — the app is
   not running — genuinely holds. Anyone "simplifying" this into
   `success = ok and verified` breaks that message and is caught here.
2. Only an observed mismatch escalates. `UNCERTAIN` and `NOT_VERIFIABLE` must
   not, or every web search and every media keypress would be sent to the cloud.
3. Query tools get no verifier at all, because they change nothing.

The probe is scripted rather than real: launching Chrome inside a test suite is
not an option, and "the process exists on the dev machine" is not an assertion.
"""

from __future__ import annotations

from typing import Dict, Optional, Set

import pytest

from winvoice.tools.state_capture import PathState
from winvoice.tools.verifier import (
    CloseAppVerifier,
    OpenAppVerifier,
    RunScriptVerifier,
    SetVolumeVerifier,
    VerificationStatus,
    Verifier,
    WriteFileVerifier,
    default_verifiers,
    unresolved,
)


class FakeProbe:
    """A scripted machine: only the three observations verifiers make."""

    def __init__(
        self,
        processes: Optional[Set[str]] = None,
        files: Optional[Dict[str, str]] = None,
        volume: Optional[int] = None,
        absent_paths: bool = False,
    ):
        self.processes = {p.lower() for p in (processes or set())}
        self.files = dict(files or {})
        self.volume = volume
        self.absent_paths = absent_paths

    def running_processes(self) -> Set[str]:
        return set(self.processes)

    def path_state(self, path: str) -> PathState:
        if path in self.files and not self.absent_paths:
            return PathState(path=path, exists=True, is_file=True, size=len(self.files[path]))
        return PathState(path=path, exists=False)

    def read_text(self, path: str) -> Optional[str]:
        return self.files.get(path)

    def volume_percent(self) -> Optional[int]:
        return self.volume


@pytest.fixture(autouse=True)
def _instant_settle(monkeypatch):
    """
    Remove the real settling budget.

    Production polls for up to a second because a launched app takes a moment
    to appear in `tasklist`. Nothing in these tests is asynchronous, so waiting
    would only make the suite slow.
    """

    def no_wait(self, predicate, budget_s=None):
        try:
            return bool(predicate())
        except Exception:
            return False

    monkeypatch.setattr(OpenAppVerifier, "_poll", no_wait)
    monkeypatch.setattr(CloseAppVerifier, "_poll", no_wait)


# ──────────────────────────────────────────────────────────────
# open_app
# ──────────────────────────────────────────────────────────────

def test_open_app_is_verified_when_the_process_is_running() -> None:
    probe = FakeProbe(processes={"notepad.exe"})

    result = OpenAppVerifier().verify(
        {"app": "记事本"}, {"success": True}, None, probe
    )

    assert result.status is VerificationStatus.VERIFIED
    assert result.verified
    assert not unresolved(result.status)


def test_open_app_fails_retryably_when_the_process_never_appears() -> None:
    probe = FakeProbe(processes={"explorer.exe"})

    result = OpenAppVerifier().verify(
        {"app": "记事本"}, {"success": True}, None, probe
    )

    assert result.status is VerificationStatus.FAILED
    # A cold start can outrun the budget, so a retry is worth more than a cloud
    # round trip.
    assert result.retryable is True
    assert unresolved(result.status)


def test_open_app_is_unverifiable_for_a_uri_target() -> None:
    """`ms-settings:` opens a settings page; there is no process to observe."""
    probe = FakeProbe(processes=set())

    result = OpenAppVerifier().verify(
        {"app": "设置"}, {"success": True}, None, probe
    )

    assert result.status is VerificationStatus.NOT_VERIFIABLE
    # Crucially: an unobservable target must NOT escalate.
    assert not unresolved(result.status)


def test_open_app_reports_failure_when_the_launch_already_failed() -> None:
    probe = FakeProbe(processes=set())

    result = OpenAppVerifier().verify(
        {"app": "记事本"}, {"success": False, "error": "boom"}, None, probe
    )

    assert result.status is VerificationStatus.FAILED
    assert not result.retryable


# ──────────────────────────────────────────────────────────────
# close_app — the "failed tool, satisfied goal" case
# ──────────────────────────────────────────────────────────────

def test_close_app_is_verified_even_though_the_tool_reported_failure() -> None:
    """
    The `new_way.md` 「删除文件」 example, in this project's own vocabulary.

    taskkill exits 128 ("no process matches") and the tool honestly says
    「好像没有在运行。」. The postcondition the user asked for — the app is closed —
    nevertheless holds, so the task is NOT unresolved and must not escalate.
    """
    probe = FakeProbe(processes={"explorer.exe"})

    result = CloseAppVerifier().verify(
        {"app": "记事本"}, {"success": False, "error": "no process matches"}, None, probe
    )

    assert result.status is VerificationStatus.VERIFIED
    assert not unresolved(result.status)


def test_close_app_fails_when_the_process_survives() -> None:
    probe = FakeProbe(processes={"notepad.exe"})

    result = CloseAppVerifier().verify(
        {"app": "记事本"}, {"success": True}, None, probe
    )

    assert result.status is VerificationStatus.FAILED
    assert result.retryable is True


# ──────────────────────────────────────────────────────────────
# write_file
# ──────────────────────────────────────────────────────────────

def test_write_file_is_verified_when_content_matches() -> None:
    path = "C:/Users/someone/Desktop/test.txt"
    probe = FakeProbe(files={path: "你好"})

    result = WriteFileVerifier().verify(
        {"path": path, "content": "你好"}, {"success": True}, None, probe
    )

    assert result.status is VerificationStatus.VERIFIED
    assert all(c.passed for c in result.checks)


def test_write_file_fails_when_content_differs() -> None:
    """「已经写好了。」 is not evidence; the bytes on disk are."""
    path = "C:/Users/someone/Desktop/test.txt"
    probe = FakeProbe(files={path: "别的内容"})

    result = WriteFileVerifier().verify(
        {"path": path, "content": "你好"}, {"success": True}, None, probe
    )

    assert result.status is VerificationStatus.FAILED
    assert unresolved(result.status)
    failed = {c.name: c for c in result.checks if not c.passed}
    assert "content_matches" in failed


def test_write_file_fails_when_the_file_is_missing() -> None:
    probe = FakeProbe(files={})

    result = WriteFileVerifier().verify(
        {"path": "C:/Users/someone/Desktop/gone.txt", "content": "x"},
        {"success": True},
        None,
        probe,
    )

    assert result.status is VerificationStatus.FAILED
    assert result.retryable is True


def test_write_file_capture_records_the_previous_state() -> None:
    path = "C:/Users/someone/Desktop/existing.txt"
    probe = FakeProbe(files={path: "old"})

    before = WriteFileVerifier().capture({"path": path}, probe)

    assert before != 0  # a real observation, not the "no capture" sentinel
    assert before.exists is True


# ──────────────────────────────────────────────────────────────
# run_script
# ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "returncode,expected",
    [(0, VerificationStatus.VERIFIED), (1, VerificationStatus.FAILED), (2, VerificationStatus.FAILED)],
)
def test_run_script_verdict_follows_the_exit_status(returncode: int, expected: VerificationStatus) -> None:
    result = RunScriptVerifier().verify(
        {"path": "C:/x.py"}, {"success": returncode == 0, "returncode": returncode}, None, FakeProbe()
    )

    assert result.status is expected


def test_run_script_fails_when_no_exit_status_exists() -> None:
    """No exit status means the process never ran (bad path, timeout)."""
    result = RunScriptVerifier().verify(
        {"path": "C:/missing.py"}, {"success": False}, None, FakeProbe()
    )

    assert result.status is VerificationStatus.FAILED
    assert not result.retryable


# ──────────────────────────────────────────────────────────────
# set_volume
# ──────────────────────────────────────────────────────────────

def test_set_volume_is_verified_for_an_absolute_level() -> None:
    result = SetVolumeVerifier().verify(
        {"level": 50}, {"success": True}, None, FakeProbe(volume=50)
    )

    assert result.status is VerificationStatus.VERIFIED


def test_set_volume_tolerates_rounding() -> None:
    result = SetVolumeVerifier().verify(
        {"level": 50}, {"success": True}, None, FakeProbe(volume=52)
    )

    assert result.status is VerificationStatus.VERIFIED


def test_set_volume_fails_when_the_level_did_not_move() -> None:
    result = SetVolumeVerifier().verify(
        {"level": 50}, {"success": True}, None, FakeProbe(volume=90)
    )

    assert result.status is VerificationStatus.FAILED
    assert unresolved(result.status)


def test_set_volume_uses_the_captured_level_for_a_delta() -> None:
    verifier = SetVolumeVerifier()
    probe = FakeProbe(volume=30)
    before = verifier.capture({"delta": 20}, probe)

    probe.volume = 50
    result = verifier.verify({"delta": 20}, {"success": True}, before, probe)

    assert result.status is VerificationStatus.VERIFIED


def test_set_volume_is_uncertain_for_a_delta_with_no_baseline() -> None:
    """Without a captured level a relative change cannot be checked, and an
    unverifiable delta must not be reported as a failure (it is not evidence)."""
    result = SetVolumeVerifier().verify(
        {"delta": 20}, {"success": True}, None, FakeProbe(volume=50)
    )

    assert result.status is VerificationStatus.UNCERTAIN
    assert not unresolved(result.status)


# ──────────────────────────────────────────────────────────────
# Escalation policy
# ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "status,escalates",
    [
        (VerificationStatus.VERIFIED, False),
        (VerificationStatus.FAILED, True),
        (VerificationStatus.UNCERTAIN, False),
        (VerificationStatus.NOT_VERIFIABLE, False),
    ],
)
def test_only_an_observed_mismatch_escalates(status: VerificationStatus, escalates: bool) -> None:
    assert unresolved(status) is escalates


# ──────────────────────────────────────────────────────────────
# Registry coverage
# ──────────────────────────────────────────────────────────────

def test_only_tools_with_observable_state_have_verifiers() -> None:
    """
    Query tools are deliberately absent: `get_time` and `get_weather` change
    nothing, so a verifier could only ever answer "nothing to check" while
    adding tokens to every tool result the model reads.
    """
    verifiers = default_verifiers()

    assert set(verifiers) == {
        "open_app",
        "close_app",
        "set_volume",
        "write_file",
        "run_script",
    }
    for absent in ("get_time", "get_weather", "read_file", "search_web", "media_control"):
        assert absent not in verifiers


def test_settling_poll_gives_up_within_its_budget() -> None:
    """A predicate that never settles must return False, not hang the tool.

    Called through the base class on purpose: the autouse fixture above stubs
    `_poll` on the concrete verifiers to keep the suite fast, and this test is
    about the real implementation.
    """
    verifier = OpenAppVerifier()
    verifier.settle_budget_s = 0.05
    verifier.settle_interval_s = 0.01

    assert Verifier._poll(verifier, lambda: False) is False


def test_settling_poll_returns_true_once_the_predicate_holds() -> None:
    verifier = OpenAppVerifier()
    verifier.settle_budget_s = 0.5
    verifier.settle_interval_s = 0.01
    calls = {"n": 0}

    def eventually():
        calls["n"] += 1
        return calls["n"] >= 3

    assert Verifier._poll(verifier, eventually) is True
