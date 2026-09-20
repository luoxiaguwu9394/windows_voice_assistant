"""
The executor's two new responsibilities: enforce the speaker tier, verify the result.

Both were gaps rather than features. `UNIMPLEMENTED.md` §1.1 records the first:
`ToolExecutor.execute` carried a `# For now, assume full tier` placeholder, so a
`guest` could read files, write files and run scripts even though
`registry.validate_call` had understood tiers all along and `tools.guest_denied`
was declared in configuration with zero references anywhere in the code. The
second gap is the one `new_way.md` is largely about — nothing checked that a
tool's claimed success matched the machine.

The tier tests matter more since DSH arrived: tools are now reachable from a
process DSH spawns, so an unenforced tier would be a way *around* the permission
model rather than a user of it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Optional, Set

import pytest

from winvoice.contracts import SpeakerTier, ToolCall, ToolName
from winvoice.tools.executor import ToolExecutor
from winvoice.tools.state_capture import PathState


class FakeProbe:
    def __init__(self, processes: Optional[Set[str]] = None, files: Optional[Dict[str, str]] = None):
        self.processes = {p.lower() for p in (processes or set())}
        self.files = dict(files or {})

    def running_processes(self) -> Set[str]:
        return set(self.processes)

    def path_state(self, path: str) -> PathState:
        if path in self.files:
            return PathState(path=path, exists=True, is_file=True, size=len(self.files[path]))
        return PathState(path=path, exists=False)

    def read_text(self, path: str) -> Optional[str]:
        return self.files.get(path)


@pytest.fixture(autouse=True)
def _instant_settle(monkeypatch):
    """Skip the real waiting budget: nothing here is asynchronous."""
    from winvoice.tools.verifier import CloseAppVerifier, OpenAppVerifier

    def no_wait(self, predicate, budget_s=None):
        try:
            return bool(predicate())
        except Exception:
            return False

    monkeypatch.setattr(OpenAppVerifier, "_poll", no_wait)
    monkeypatch.setattr(CloseAppVerifier, "_poll", no_wait)


# ──────────────────────────────────────────────────────────────
# Speaker tier
# ──────────────────────────────────────────────────────────────

async def test_guest_cannot_read_a_file() -> None:
    executor = ToolExecutor(probe=FakeProbe())

    result = await executor.execute(
        ToolCall(tool=ToolName.READ_FILE, args={"path": "C:/Users/x/notes.txt"}, tier=SpeakerTier.GUEST)
    )

    assert result.success is False
    assert "guest" in (result.error or "").lower()
    # What the user hears must be speakable Chinese: the TTS lexicon has no
    # Latin entries and would drop the English reason word by word.
    assert result.message and result.message.isascii() is False


async def test_guest_cannot_run_a_script() -> None:
    executor = ToolExecutor(probe=FakeProbe())

    result = await executor.execute(
        ToolCall(tool=ToolName.RUN_SCRIPT, args={"path": "C:/Users/x/thing.py"}, tier=SpeakerTier.GUEST)
    )

    assert result.success is False


async def test_guest_may_still_ask_a_question() -> None:
    """The read-only query tools declare `guest_allowed=True`; that must work."""
    executor = ToolExecutor(probe=FakeProbe())

    result = await executor.execute(ToolCall(tool=ToolName.GET_TIME, args={}, tier=SpeakerTier.GUEST))

    assert result.success is True
    assert result.message


async def test_rejected_speaker_is_refused() -> None:
    executor = ToolExecutor(probe=FakeProbe())

    result = await executor.execute(
        ToolCall(tool=ToolName.GET_TIME, args={}, tier=SpeakerTier.REJECTED)
    )

    assert result.success is False
    assert result.message and result.message.isascii() is False


async def test_tier_defaults_to_full_so_nothing_regresses() -> None:
    """An un-updated caller (`ToolCall` with no tier) keeps today's behaviour."""
    executor = ToolExecutor(probe=FakeProbe())
    call = ToolCall(tool=ToolName.GET_TIME, args={})

    assert call.tier is SpeakerTier.FULL
    assert (await executor.execute(call)).success is True


async def test_the_tier_rides_on_the_call_when_no_argument_is_given() -> None:
    executor = ToolExecutor(probe=FakeProbe())

    result = await executor.execute(
        ToolCall(tool=ToolName.READ_FILE, args={"path": "C:/nope.txt"}, tier=SpeakerTier.GUEST)
    )

    assert result.success is False
    assert "guest" in (result.error or "").lower()


# ──────────────────────────────────────────────────────────────
# Verification is attached, and never rewrites `success`
# ──────────────────────────────────────────────────────────────

async def test_verification_is_attached_to_the_result() -> None:
    executor = ToolExecutor(probe=FakeProbe(processes={"notepad.exe"}))

    result = await executor.execute(ToolCall(tool=ToolName.OPEN_APP, args={"app": "记事本"}))

    assert result.verification is not None
    assert result.verification["status"] == "verified"
    assert result.verification["verified"] is True


async def test_a_verified_goal_does_not_rewrite_an_honest_failure() -> None:
    """
    `close_app` on something that is not running reports failure — a truthful,
    useful answer. The postcondition (the app is not running) nevertheless holds,
    so the task is NOT unresolved and must not escalate.

    Which Chinese sentence the tool chooses depends on `taskkill`'s exit code
    (`128`/`not found` means "it was not running", anything else means "I could
    not close it"), and both are honest. What this test pins is that the
    verifier's `verified` verdict does not overwrite either of them with a false
    claim of success.
    """
    executor = ToolExecutor(probe=FakeProbe(processes={"explorer.exe"}))

    result = await executor.execute(ToolCall(tool=ToolName.CLOSE_APP, args={"app": "记事本"}))

    assert result.success is False
    assert result.message and result.message.isascii() is False
    assert "已经关闭" not in result.message
    assert result.verification is not None
    assert result.verification["status"] == "verified"


async def test_a_mismatch_is_recorded_as_failed() -> None:
    executor = ToolExecutor(probe=FakeProbe(processes=set()))

    result = await executor.execute(ToolCall(tool=ToolName.OPEN_APP, args={"app": "记事本"}))

    # The tool told the truth about what it did...
    assert result.success is True
    # ...but the machine disagrees that the app is there, which is what
    # escalation is allowed to act on.
    assert result.verification["status"] == "failed"
    assert result.verification["retryable"] is True


async def test_tools_with_nothing_to_observe_carry_no_verification() -> None:
    executor = ToolExecutor(probe=FakeProbe())

    result = await executor.execute(ToolCall(tool=ToolName.GET_TIME, args={}))

    assert result.success is True
    assert result.verification is None


async def test_write_file_is_verified_against_the_bytes_on_disk(monkeypatch) -> None:
    """
    The handler is stubbed because the real one writes to `Path.home()`, which
    the test sandbox refuses. What matters is that the result is checked against
    the filesystem rather than taken at its word.
    """
    path = str(Path.home() / "winvoice_verify_test.txt")
    executor = ToolExecutor(probe=FakeProbe(files={path: "你好"}))
    spec = executor.registry.get(ToolName.WRITE_FILE)
    monkeypatch.setattr(
        spec, "handler", lambda args: {"success": True, "message": "已经写好了。"}
    )

    result = await executor.execute(
        ToolCall(tool=ToolName.WRITE_FILE, args={"path": path, "content": "你好"}), confirmed=True
    )

    assert result.verification is not None
    assert result.verification["status"] == "verified"


async def test_write_file_content_mismatch_is_caught(monkeypatch) -> None:
    """The handler claims success; the disk says otherwise."""
    path = str(Path.home() / "winvoice_verify_test.txt")
    executor = ToolExecutor(probe=FakeProbe(files={path: "别的内容"}))
    spec = executor.registry.get(ToolName.WRITE_FILE)
    monkeypatch.setattr(
        spec, "handler", lambda args: {"success": True, "message": "已经写好了。"}
    )

    result = await executor.execute(
        ToolCall(tool=ToolName.WRITE_FILE, args={"path": path, "content": "你好"}), confirmed=True
    )

    assert result.success is True          # the tool still says what it said
    assert result.verification["status"] == "failed"  # the machine disagrees
    assert result.verification["verified"] is False


async def test_verification_can_be_switched_off(monkeypatch) -> None:
    executor = ToolExecutor(probe=FakeProbe(processes=set()))
    executor.verification_enabled = False

    result = await executor.execute(ToolCall(tool=ToolName.OPEN_APP, args={"app": "记事本"}))

    assert result.verification is None


async def test_every_declared_tool_is_registered() -> None:
    """
    The "unknown tool" branch is unreachable while the enum and the registry
    agree, which is the invariant worth pinning: a `ToolName` member with no
    `ToolSpec` would surface to the user as 「这个功能我还不认识。」 for a tool the
    docs promise.
    """
    executor = ToolExecutor(probe=FakeProbe())

    missing = [name.value for name in ToolName if executor.registry.get(name) is None]

    assert missing == []


async def test_missing_required_argument_is_refused_in_chinese() -> None:
    executor = ToolExecutor(probe=FakeProbe())

    result = await executor.execute(ToolCall(tool=ToolName.OPEN_APP, args={}))

    assert result.success is False
    assert "app" in (result.error or "")
    assert result.message and result.message.isascii() is False
