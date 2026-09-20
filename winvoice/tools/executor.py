"""
Tool Executor: validates, confirms, snapshots, executes, verifies, rolls back.

The full destructive action flow:

1. Validate against registry (schema **and** speaker tier)
2. Confirmation (if required)
3. Snapshot target files
4. Execute
5. Verify the postcondition against real machine state
6. On failure: auto-restore from snapshot

Steps 1 and 5 are the two security-relevant changes since the first version.
Tier used to be a placeholder (`# For now, assume full tier`), which meant a
guest could read files, write files and run scripts; and nothing used to check
that a tool's claimed success matched the machine, which is how
「已经打开谷歌浏览器了。」 survived a launch that cmd.exe had refused.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any, Dict, List, Optional

from winvoice.config import get_config
from winvoice.logging import get_logger
from winvoice.contracts import SpeakerTier, ToolCall, ToolResult
from .registry import get_tool_registry, ToolSpec
from .snapshot import get_snapshot_manager
from .state_capture import SystemProbe, WindowsProbe
from .verifier import VerificationResult, VerificationStatus, Verifier, unresolved

logger = get_logger(__name__)


async def _maybe_await(value: Any) -> Any:
    """
    Await a handler's return value when it is awaitable.

    Most builtins are synchronous, but `get_weather` does HTTP and must not
    block the audio loop, so `execute` accepts both shapes. Returning the
    coroutine unawaited would hand the pipeline a `<coroutine object>` and
    produce a reply about nothing.
    """
    if inspect.isawaitable(value):
        return await value
    return value


class ToolExecutor:
    """
    Executes tool calls with validation, confirmation, verification, and
    snapshot rollback.
    """

    def __init__(self, probe: Optional[SystemProbe] = None):
        self.registry = get_tool_registry()
        self.snapshot_mgr = get_snapshot_manager()
        self.confirm_required = get_config().get("tools.confirm_required", True)
        self.verification_enabled = get_config().get("tools.verification_enabled", True)
        self._pending_confirmation: Optional[ToolCall] = None
        # Injectable so tests can script the machine instead of driving it.
        self._probe: SystemProbe = probe if probe is not None else WindowsProbe()

    async def execute(
        self,
        call: ToolCall,
        confirmed: bool = False,
        tier: Optional[str] = None,
    ) -> ToolResult:
        """
        Execute a tool call.

        If confirmation required and not confirmed, returns error asking for
        confirmation. Call again with confirmed=True to proceed.

        `tier` defaults to the tier carried on the call. It is accepted
        separately as well because the MCP server knows the tier from the
        in-flight utterance record rather than from the model's request.
        """
        effective_tier = self._normalise_tier(tier if tier is not None else call.tier)

        # Validate
        spec = self.registry.get(call.tool)
        if not spec:
            return ToolResult(
                tool=call.tool,
                success=False,
                error=f"Unknown tool: {call.tool}",
                message="这个功能我还不认识。",
            )

        # Validate args and tier. `validate_call` has always understood tiers;
        # until now nobody passed one in, so every call was checked as `full`.
        error = self.registry.validate_call(call.tool, call.args, tier=effective_tier)
        if error:
            logger.info(
                "tool_call_rejected",
                tool=call.tool.value,
                tier=effective_tier,
                reason=error,
            )
            return ToolResult(
                tool=call.tool,
                success=False,
                error=error,
                message=self._tier_message(effective_tier),
            )

        # Confirmation check
        if spec.requires_confirmation and self.confirm_required and not confirmed:
            self._pending_confirmation = call
            return ToolResult(
                tool=call.tool,
                success=False,
                error=f"CONFIRMATION_REQUIRED: {spec.description}. Call again with confirmed=true.",
                # The confirmation round trip does not exist yet, so this is
                # what the user hears every time they ask for a write or a
                # script: say so plainly instead of reading the English spec.
                message="这个操作需要你先确认，我还没有实现确认的流程。",
            )

        # Create snapshot for destructive tools
        snapshot_id = None
        if spec.destructive and self.snapshot_mgr.enabled:
            paths = self._resolve_modified_paths(spec, call.args)
            if paths:
                snapshot = self.snapshot_mgr.create_snapshot(paths)
                if snapshot:
                    snapshot_id = snapshot.snapshot_id

        # Capture pre-state for the verifier *before* the machine changes.
        before = None
        if self.verification_enabled and spec.verifier is not None:
            try:
                before = spec.verifier.capture(call.args, self._probe)
            except Exception as exc:  # capture must never block the action
                logger.warning("verifier_capture_failed", tool=call.tool.value, error=str(exc))

        # Execute
        try:
            result = await _maybe_await(spec.handler(call.args))
            success = result.get("success", False)
            error = result.get("error") if not success else None
            message = result.get("message") if isinstance(result, dict) else None

            verification = self._verify(spec, call.args, result, before)

            # On failure, restore snapshot. A `VERIFIED` verdict does not
            # rescue a snapshot restore decision: if the tool reported failure
            # we still undo its partial writes, because verification only knows
            # about the postcondition, not about collateral damage.
            if not success and snapshot_id:
                logger.warning("tool_failed_restoring_snapshot", tool=call.tool, snapshot_id=snapshot_id)
                self.snapshot_mgr.restore_snapshot(snapshot_id)

            return ToolResult(
                tool=call.tool,
                success=success,
                result=result,
                error=error,
                message=message,
                snapshot_id=snapshot_id,
                verification=verification,
            )
        except Exception as e:
            logger.error("tool_execution_exception", tool=call.tool, error=str(e))
            if snapshot_id:
                self.snapshot_mgr.restore_snapshot(snapshot_id)
            return ToolResult(
                tool=call.tool,
                success=False,
                error=f"Execution error: {e}",
                message="执行这个操作的时候出错了。",
                snapshot_id=snapshot_id,
                # Built through the same type as every other verdict, so the
                # error path cannot drift into a different payload shape than
                # the one the escalation reader understands.
                verification=VerificationResult(
                    status=VerificationStatus.FAILED,
                    reason=f"execution raised: {type(e).__name__}",
                    retryable=False,
                ).to_json(),
            )

    # ── verification ───────────────────────────────────────────

    @staticmethod
    def _normalise_tier(tier: Optional[SpeakerTier | str]) -> str:
        """
        A recognised tier string, or `rejected` when it is not one.

        Fail-closed on purpose. `validate_call` compares the tier to the literal
        `"guest"`, so a misspelt or unexpected value would fall through every
        restriction and be *promoted* to full access — the one direction a
        permission check must never guess in. An unrecognised tier therefore
        refuses the call and says so in the log rather than running it.
        """
        if tier is None:
            return SpeakerTier.FULL.value
        try:
            return SpeakerTier(tier).value
        except ValueError:
            logger.warning("unrecognised_tier_treated_as_rejected", tier=str(tier))
            return SpeakerTier.REJECTED.value

    def _verify(
        self,
        spec: ToolSpec,
        args: Dict[str, Any],
        execution_result: Any,
        before: Any,
    ) -> Optional[Dict[str, Any]]:
        """
        Ask the tool's verifier what the machine says, and log the verdict.

        Returned as a plain dict so it can ride on `ToolResult` (a Pydantic
        contract) and be handed straight to the agent as JSON evidence.
        """
        verifier: Optional[Verifier] = spec.verifier
        if not self.verification_enabled or verifier is None:
            return None
        if not isinstance(execution_result, dict):
            return None

        try:
            verdict = verifier.verify(args, execution_result, before, self._probe)
        except Exception as exc:
            logger.error("verification_failed", tool=spec.name.value, error=str(exc))
            return None

        payload = verdict.to_json()
        logger.info(
            "tool_verified",
            tool=spec.name.value,
            status=verdict.status.value,
            reason=verdict.reason,
            escalatable=unresolved(verdict.status),
        )
        return payload

    # ── messages ───────────────────────────────────────────────

    @staticmethod
    def _tier_message(tier: str) -> str:
        """
        What a refused call says out loud.

        Must be plain Chinese: the TTS lexicon has no Latin entries and drops
        English word by word (spec.md §6.4). A guest is told the assistant
        cannot do that for them, not which internal flag refused it.
        """
        if tier == "rejected":
            return "我不能确认是您本人，所以先不操作了。"
        return "这个操作我暂时不能替你做。"

    def _resolve_modified_paths(self, spec: ToolSpec, args: Dict[str, Any]) -> List[Path]:
        """
        Resolve `modified_paths` templates with actual args.

        Returns `Path`s because that is what `SnapshotManager.create_snapshot`
        declares; passing the strings through happened to work, but left the
        call permanently untypeable.
        """
        paths: List[Path] = []
        for template in spec.modified_paths:
            # Simple template substitution: {args.key}
            for key, value in args.items():
                template = template.replace(f"{{args.{key}}}", str(value))
            paths.append(Path(template))
        return paths

    def get_pending_confirmation(self) -> Optional[ToolCall]:
        return self._pending_confirmation

    def clear_pending_confirmation(self):
        self._pending_confirmation = None


def create_tool_executor(probe: Optional[Any] = None) -> ToolExecutor:
    return ToolExecutor(probe=probe)
