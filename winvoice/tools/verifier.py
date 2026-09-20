"""
Verification: did the machine actually end up in the state we asked for?

The rule this file exists to enforce:

    An LLM's "done!" is not evidence. Only the machine's own state is.

`new_way.md` requires that escalation to the cloud model be driven by
*observable* conditions rather than by asking the local model whether it feels
confident. This module supplies those observations. It never asks a model
anything; it looks at processes, files and the volume endpoint.

Three deliberate decisions, each of which deviates from the first draft of
`docs/dsh_integration_design.md`, and each for a reason:

1. **A verifier owns its own state capture.** The draft had a separate
   `StateCapture` layer guessing what a verifier would want to see. Asking the
   verifier directly (`capture`) keeps the two from disagreeing and avoids
   paying for a full-memory snapshot on every call.

2. **Verification does not overwrite `ToolResult.success`.** The draft set
   `success = execution.ok and verification.verified`. That would have destroyed
   honest refusals: `close_app` currently reports 「好像没有在运行。」 when nothing
   was running, which is *true and useful*, and the goal state ("not running")
   is genuinely satisfied — so `VERIFIED` would have rewritten a truthful
   message into a false 「已经关闭了」. Instead the execution result stays the
   honest claim shown to the user, and the *verification* decides whether the
   task is unresolved and therefore worth escalating.

3. **Only tools with observable state get a verifier.** `get_time`,
   `get_weather` and `read_file` change nothing on the machine, so a verifier
   for them could only ever answer "nothing to check" while costing tokens on
   every tool result. Their absence is the honest encoding of that.

A verifier may also *upgrade* the meaning of a failure: `close_app` returning
"no process matches" leaves the postcondition ("not running") satisfied, which is
`VERIFIED`, which means "do not escalate" even though `success` is False.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from winvoice.logging import get_logger
from .state_capture import PathState, SystemProbe

logger = get_logger(__name__)


class VerificationStatus(str, Enum):
    """How the machine's state relates to the action's expected postcondition."""

    #: Observed to hold. Nothing left to do; never escalate.
    VERIFIED = "verified"
    #: Observed NOT to hold. The action did not achieve its goal.
    FAILED = "failed"
    #: Something happened, but whether it is what the user wanted cannot be
    #: read off machine state (e.g. a web search runs, but the *results* are
    #: not observable here).
    UNCERTAIN = "uncertain"
    #: The goal is not expressible as machine state at all (e.g. "play music"
    #: — there is no queryable playback state without UI automation).
    NOT_VERIFIABLE = "not_verifiable"


@dataclass(frozen=True)
class VerificationCheck:
    """One named observation, so a failure says *which* thing was wrong."""

    name: str
    passed: bool
    detail: str = ""

    def to_json(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"name": self.name, "passed": self.passed}
        if self.detail:
            out["detail"] = self.detail
        return out


@dataclass(frozen=True)
class VerificationResult:
    """The verdict on one action, plus the evidence behind it."""

    status: VerificationStatus
    reason: str
    checks: Tuple[VerificationCheck, ...] = ()
    #: True when trying the same action again could plausibly work (a slow app
    #: that has not appeared yet), false when it cannot (the executable does not
    #: exist at all). Drives local retry before cloud escalation.
    retryable: bool = False

    @property
    def verified(self) -> bool:
        return self.status is VerificationStatus.VERIFIED

    @property
    def failed(self) -> bool:
        return self.status is VerificationStatus.FAILED

    def to_json(self) -> Dict[str, Any]:
        return {
            "status": self.status.value,
            "verified": self.verified,
            "reason": self.reason,
            "retryable": self.retryable,
            "checks": [c.to_json() for c in self.checks],
        }


class Verifier(ABC):
    """Checks one action against observable machine state."""

    #: Budget, in seconds, for post-conditions that settle asynchronously
    #: (a launched process takes a moment to appear in `tasklist`).
    settle_budget_s: float = 1.0
    settle_interval_s: float = 0.1

    @abstractmethod
    def verify(
        self,
        args: Dict[str, Any],
        execution_result: Dict[str, Any],
        before: Any,
        probe: SystemProbe,
    ) -> VerificationResult:
        ...

    def capture(self, args: Dict[str, Any], probe: SystemProbe) -> Any:
        """State this verifier needs from *before* the action ran."""
        return None

    # ── shared helpers ─────────────────────────────────────────

    def _poll(self, predicate, budget_s: Optional[float] = None) -> bool:
        """Wait for a postcondition to settle; never raises."""
        deadline = time.monotonic() + (self.settle_budget_s if budget_s is None else budget_s)
        while True:
            try:
                if predicate():
                    return True
            except Exception:  # observation must never break the tool path
                return False
            if time.monotonic() >= deadline:
                return False
            time.sleep(self.settle_interval_s)


# ──────────────────────────────────────────────────────────────
# Application launch / close
# ──────────────────────────────────────────────────────────────

def _expected_image(app_arg: str) -> Optional[str]:
    """
    The process image an `app` argument should create, or None.

    Resolution goes through the same table `open_app` launches from, so the
    verifier cannot disagree with the tool about what a spoken name means.
    A URI target (`ms-settings:`) has no process and returns None.
    """
    from .builtin import ALLOWED_APPS, resolve_app

    app_id = resolve_app(app_arg)
    if app_id is None:
        return None
    command = ALLOWED_APPS.get(app_id)
    if not command or not command.lower().endswith(".exe"):
        return None
    return command.lower()


class OpenAppVerifier(Verifier):
    """A launched application is a running process."""

    def verify(self, args, execution_result, before, probe):
        if not execution_result.get("success"):
            # The launch already failed and said why; re-checking would only
            # restate it. Report the flow as unresolved so the router decides.
            return VerificationResult(
                status=VerificationStatus.FAILED,
                reason="launch reported failure",
                checks=(VerificationCheck("launch_accepted", False),),
                retryable=False,
            )

        image = _expected_image(str(args.get("app", "")))
        if image is None:
            return VerificationResult(
                status=VerificationStatus.NOT_VERIFIABLE,
                reason="target has no process to observe (unresolved or URI target)",
                checks=(VerificationCheck("resolvable_image", False),),
            )

        checks: List[VerificationCheck] = [VerificationCheck("resolvable_image", True, image)]
        found = self._poll(lambda: image in probe.running_processes())

        if found:
            checks.append(VerificationCheck("process_running", True, image))
            return VerificationResult(
                status=VerificationStatus.VERIFIED,
                reason=f"{image} is running",
                checks=tuple(checks),
            )

        checks.append(VerificationCheck("process_running", False, image))
        return VerificationResult(
            status=VerificationStatus.FAILED,
            # Retryable: a cold start of a large app can outrun the budget, and
            # a second attempt is cheap compared with a cloud round trip.
            reason=f"{image} did not appear within {self.settle_budget_s:g}s",
            checks=tuple(checks),
            retryable=True,
        )


class CloseAppVerifier(Verifier):
    """A closed application is no longer a running process."""

    def verify(self, args, execution_result, before, probe):
        image = _expected_image(str(args.get("app", "")))
        if image is None:
            return VerificationResult(
                status=VerificationStatus.NOT_VERIFIABLE,
                reason="target has no process to observe (unresolved or URI target)",
                checks=(VerificationCheck("resolvable_image", False),),
            )

        checks: List[VerificationCheck] = [VerificationCheck("resolvable_image", True, image)]
        gone = self._poll(lambda: image not in probe.running_processes())

        if gone:
            checks.append(VerificationCheck("process_absent", True, image))
            # Note this is VERIFIED even when `execution_result["success"]` is
            # False ("no process matches") — the postcondition genuinely holds,
            # so there is nothing to escalate. The tool still gets to *say*
            # 「好像没有在运行。」, which is the truthful thing to tell a user.
            return VerificationResult(
                status=VerificationStatus.VERIFIED,
                reason=f"{image} is not running",
                checks=tuple(checks),
            )

        checks.append(VerificationCheck("process_absent", False, image))
        return VerificationResult(
            status=VerificationStatus.FAILED,
            reason=f"{image} is still running",
            checks=tuple(checks),
            retryable=True,
        )


# ──────────────────────────────────────────────────────────────
# Filesystem
# ──────────────────────────────────────────────────────────────

class WriteFileVerifier(Verifier):
    """A written file exists, is a regular file, and holds what was asked for."""

    def capture(self, args, probe):
        return probe.path_state(str(args.get("path", "")))

    def verify(self, args, execution_result, before, probe):
        path = str(args.get("path", ""))
        expected_content = args.get("content", "")

        if not execution_result.get("success"):
            return VerificationResult(
                status=VerificationStatus.FAILED,
                reason="write reported failure",
                checks=(VerificationCheck("write_accepted", False),),
            )

        state: PathState = probe.path_state(path)
        checks: List[VerificationCheck] = [
            VerificationCheck("exists", state.exists, path),
            VerificationCheck("is_file", state.is_file, path),
        ]
        if not state.exists or not state.is_file:
            return VerificationResult(
                status=VerificationStatus.FAILED,
                reason="file was not created",
                checks=tuple(checks),
                retryable=True,
            )

        # Content is read back rather than trusted: `write_file` reported
        # 「已经写好了。」 for years while the same path was written elsewhere.
        actual = probe.read_text(path)
        matches = actual is not None and actual == expected_content
        checks.append(
            VerificationCheck(
                "content_matches",
                matches,
                f"expected {len(str(expected_content))} chars, read "
                f"{'unreadable' if actual is None else str(len(actual)) + ' chars'}",
            )
        )
        if not matches:
            return VerificationResult(
                status=VerificationStatus.FAILED,
                reason="file content does not match what was requested",
                checks=tuple(checks),
                retryable=False,
            )

        return VerificationResult(
            status=VerificationStatus.VERIFIED,
            reason="file exists with the requested content",
            checks=tuple(checks),
        )


# ──────────────────────────────────────────────────────────────
# Script execution
# ──────────────────────────────────────────────────────────────

class RunScriptVerifier(Verifier):
    """
    A script's exit status is the machine's verdict, not the tool's opinion.

    The handler already derives `success` from this same number, so this check
    is what keeps that derivation honest if the handler is ever refactored: the
    verifier reads the exit code out of the execution result independently.
    """

    def verify(self, args, execution_result, before, probe):
        code = execution_result.get("returncode")
        if code is None:
            # No exit status at all means the process never ran (bad path,
            # unsupported suffix, timeout) — the handler's message covers it.
            return VerificationResult(
                status=VerificationStatus.FAILED,
                reason="script produced no exit status",
                checks=(VerificationCheck("exit_status_present", False),),
                retryable=False,
            )

        ok = code == 0
        return VerificationResult(
            status=VerificationStatus.VERIFIED if ok else VerificationStatus.FAILED,
            reason=f"exit status {code}",
            checks=(VerificationCheck("exit_status_zero", ok, f"returncode={code}"),),
            retryable=not ok,
        )


# ──────────────────────────────────────────────────────────────
# Volume
# ──────────────────────────────────────────────────────────────

class SetVolumeVerifier(Verifier):
    """
    The endpoint volume really moved to the requested level.

    Tolerance is 3 points of the 0-100 scale: the Core Audio scalar is a float
    and the tool rounds, so an exact match would fail on rounding alone.
    """

    tolerance = 3

    def capture(self, args, probe):
        getter = getattr(probe, "volume_percent", None)
        return getter() if callable(getter) else None

    def verify(self, args, execution_result, before, probe):
        if not execution_result.get("success"):
            return VerificationResult(
                status=VerificationStatus.FAILED,
                reason="volume change reported failure",
                checks=(VerificationCheck("change_accepted", False),),
            )

        getter = getattr(probe, "volume_percent", None)
        if not callable(getter):
            return VerificationResult(
                status=VerificationStatus.NOT_VERIFIABLE,
                reason="no volume endpoint to observe",
                checks=(VerificationCheck("endpoint_observable", False),),
            )

        actual = getter()
        if actual is None:
            return VerificationResult(
                status=VerificationStatus.NOT_VERIFIABLE,
                reason="volume endpoint did not answer",
                checks=(VerificationCheck("endpoint_observable", False),),
            )

        expected = self._expected(args, before)
        if expected is None:
            return VerificationResult(
                status=VerificationStatus.UNCERTAIN,
                reason=f"volume reads {actual}, but no absolute target to compare against",
                checks=(VerificationCheck("target_known", False, f"actual={actual}"),),
            )

        target = max(0, min(100, expected))
        close_enough = abs(actual - target) <= self.tolerance
        checks = (
            VerificationCheck("endpoint_observable", True),
            VerificationCheck(
                "level_matches",
                close_enough,
                f"expected≈{target}, actual={actual}",
            ),
        )
        if close_enough:
            return VerificationResult(
                status=VerificationStatus.VERIFIED,
                reason=f"volume is {actual}",
                checks=checks,
            )
        return VerificationResult(
            status=VerificationStatus.FAILED,
            reason=f"volume is {actual}, expected about {target}",
            checks=checks,
            retryable=True,
        )

    @staticmethod
    def _expected(args: Dict[str, Any], before: Any) -> Optional[int]:
        """Absolute target implied by `level`, or `delta` applied to `before`."""
        if args.get("level") is not None:
            try:
                return int(round(float(args["level"])))
            except (TypeError, ValueError):
                return None
        if args.get("delta") is not None and isinstance(before, int):
            try:
                return before + int(args["delta"])
            except (TypeError, ValueError):
                return None
        return None


# ──────────────────────────────────────────────────────────────
# Registry
# ──────────────────────────────────────────────────────────────

def default_verifiers() -> Dict[str, Verifier]:
    """
    Verifiers for the tools whose postcondition is machine-observable.

    Tools absent from this table have no postcondition to observe (queries) or
    none that Windows exposes without UI automation (media control, browser
    content); `ToolExecutor` records that as "not verified" rather than
    inventing a verdict.
    """
    from winvoice.contracts import ToolName

    return {
        ToolName.OPEN_APP.value: OpenAppVerifier(),
        ToolName.CLOSE_APP.value: CloseAppVerifier(),
        ToolName.SET_VOLUME.value: SetVolumeVerifier(),
        ToolName.WRITE_FILE.value: WriteFileVerifier(),
        ToolName.RUN_SCRIPT.value: RunScriptVerifier(),
    }


def unresolved(result_status: VerificationStatus) -> bool:
    """
    True when this verification leaves the task genuinely unresolved.

    Drives escalation. `VERIFIED` never escalates — the goal holds even if the
    tool's own `success` flag said otherwise. `UNCERTAIN` and `NOT_VERIFIABLE`
    do not escalate on their own either: they mean "this layer cannot tell", and
    escalating every such call would send every web search to the cloud. Only an
    observed mismatch is grounds for escalation.
    """
    return result_status is VerificationStatus.FAILED


__all__ = [
    "CloseAppVerifier",
    "OpenAppVerifier",
    "RunScriptVerifier",
    "SetVolumeVerifier",
    "VerificationCheck",
    "VerificationResult",
    "VerificationStatus",
    "Verifier",
    "WriteFileVerifier",
    "default_verifiers",
    "unresolved",
]
