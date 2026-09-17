"""
Tool System: Registry, Execution, Snapshots.

- Tool allowlist with destructive action protection
- Double confirmation + file-level snapshot rollback
- Irreversible operation blocklist
"""

from .registry import ToolRegistry, ToolSpec, IRREVERSIBLE_PATTERNS
from .snapshot import SnapshotManager
from .executor import ToolExecutor, ToolResult

__all__ = [
    "ToolRegistry",
    "ToolSpec",
    "IRREVERSIBLE_PATTERNS",
    "SnapshotManager",
    "ToolExecutor",
    "ToolResult",
]