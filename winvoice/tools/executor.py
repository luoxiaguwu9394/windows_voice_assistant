"""
Tool Executor: validates, confirms, snapshots, executes, rolls back.

Handles the full destructive action flow:
1. Validate against registry
2. Double confirmation (if required)
3. Snapshot target files
4. Execute
5. On failure: auto-restore from snapshot
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from winvoice.config import get_config
from winvoice.logging import get_logger
from winvoice.contracts import ToolCall, ToolResult, ToolName
from .registry import get_tool_registry, ToolSpec
from .snapshot import get_snapshot_manager, Snapshot

logger = get_logger(__name__)


@dataclass
class ToolResult:
    """Tool execution result."""
    tool: ToolName
    success: bool
    result: Any = None
    error: Optional[str] = None
    snapshot_id: Optional[str] = None


class ToolExecutor:
    """
    Executes tool calls with validation, confirmation, and snapshot rollback.
    """

    def __init__(self):
        self.registry = get_tool_registry()
        self.snapshot_mgr = get_snapshot_manager()
        self.confirm_required = get_config().get("tools.confirm_required", True)
        self._pending_confirmation: Optional[ToolCall] = None

    async def execute(self, call: ToolCall, confirmed: bool = False) -> ToolResult:
        """
        Execute a tool call.

        If confirmation required and not confirmed, returns error asking for confirmation.
        Call again with confirmed=True to proceed.
        """
        # Validate
        spec = self.registry.get(call.tool)
        if not spec:
            return ToolResult(tool=call.tool, success=False, error=f"Unknown tool: {call.tool}")

        # Check speaker tier (would be passed via context)
        # For now, assume full tier

        # Validate args
        error = self.registry.validate_call(call.tool, call.args)
        if error:
            return ToolResult(tool=call.tool, success=False, error=error)

        # Confirmation check
        if spec.requires_confirmation and self.confirm_required and not confirmed:
            self._pending_confirmation = call
            return ToolResult(
                tool=call.tool,
                success=False,
                error=f"CONFIRMATION_REQUIRED: {spec.description}. Call again with confirmed=true.",
            )

        # Create snapshot for destructive tools
        snapshot_id = None
        if spec.destructive and self.snapshot_mgr.enabled:
            paths = self._resolve_modified_paths(spec, call.args)
            if paths:
                snapshot = self.snapshot_mgr.create_snapshot(paths)
                if snapshot:
                    snapshot_id = snapshot.snapshot_id

        # Execute
        try:
            result = spec.handler(call.args)
            success = result.get("success", False)
            error = result.get("error") if not success else None

            # On failure, restore snapshot
            if not success and snapshot_id:
                logger.warning("tool_failed_restoring_snapshot", tool=call.tool, snapshot_id=snapshot_id)
                self.snapshot_mgr.restore_snapshot(snapshot_id)

            return ToolResult(
                tool=call.tool,
                success=success,
                result=result,
                error=error,
                snapshot_id=snapshot_id,
            )
        except Exception as e:
            logger.error("tool_execution_exception", tool=call.tool, error=str(e))
            if snapshot_id:
                self.snapshot_mgr.restore_snapshot(snapshot_id)
            return ToolResult(
                tool=call.tool,
                success=False,
                error=f"Execution error: {e}",
                snapshot_id=snapshot_id,
            )

    def _resolve_modified_paths(self, spec: ToolSpec, args: Dict[str, Any]) -> List[str]:
        """Resolve modified_paths templates with actual args."""
        from pathlib import Path
        paths = []
        for template in spec.modified_paths:
            # Simple template substitution: {args.key}
            for key, value in args.items():
                template = template.replace(f"{{args.{key}}}", str(value))
            paths.append(template)
        return paths

    def get_pending_confirmation(self) -> Optional[ToolCall]:
        return self._pending_confirmation

    def clear_pending_confirmation(self):
        self._pending_confirmation = None


def create_tool_executor() -> ToolExecutor:
    return ToolExecutor()