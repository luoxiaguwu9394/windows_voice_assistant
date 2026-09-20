"""
Tool Registry: allowlist, schemas, destructive marking, irreversible patterns.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Set

from pydantic import BaseModel, Field
from winvoice.contracts import ToolName

from .verifier import Verifier, default_verifiers


# ──────────────────────────────────────────────────────────────
# Irreversible Operation Patterns
# ──────────────────────────────────────────────────────────────

IRREVERSIBLE_PATTERNS: List[str] = [
    r"reg\s+add",
    r"reg\s+delete",
    r"regedit\s+/s",
    r"msiexec.*/uninstall",
    r"msiexec.*/x\s+",
    r"wmic\s+product\s+delete",
    r"powershell.*uninstall",
    r"Remove-Item\s+-Recurse",
    r"rm\s+-rf\s+/",
    r"format\s+[a-zA-Z]:",
    r"diskpart",
    r"bcdedit",
    r"sc\s+delete",
    r"net\s+user\s+/delete",
    r"net\s+localgroup\s+/delete",
    r"schtasks\s+/delete",
    r"taskkill\s+/f\s+/im\s+system",
]


def scan_for_irreversible(script_content: str) -> List[str]:
    """
    Scan script content for irreversible operations.

    Returns the *matched substrings* (not the regexes) so they can be shown
    verbatim in the audit log and in the rejection message.
    """
    matches: List[str] = []
    for pattern in IRREVERSIBLE_PATTERNS:
        for m in re.finditer(pattern, script_content, re.IGNORECASE):
            snippet = m.group(0).strip()
            if snippet and snippet not in matches:
                matches.append(snippet)
    return matches


# ──────────────────────────────────────────────────────────────
# Tool Specification
# ──────────────────────────────────────────────────────────────

class ToolSchema(BaseModel):
    """JSON Schema for tool arguments."""
    type: str = "object"
    properties: Dict[str, Any] = Field(default_factory=dict)
    required: List[str] = Field(default_factory=list)
    # Cross-field rule `required` cannot express: at least one of these keys
    # must be present. Used by tools offering two alternative argument forms
    # (set_volume: absolute `level` or relative `delta`).
    at_least_one: List[str] = Field(default_factory=list)
    additionalProperties: bool = False


@dataclass
class ToolSpec:
    """Tool specification with metadata."""
    name: ToolName
    description: str
    schema: ToolSchema
    handler: Callable
    destructive: bool = False
    guest_allowed: bool = True
    requires_confirmation: bool = False
    modified_paths: List[str] = field(default_factory=list)  # for snapshot
    # Checks the tool's postcondition against real machine state after the
    # handler returns (see `verifier.py`). None means the tool changes nothing
    # observable — a query, a media keypress — so there is nothing to check.
    verifier: Optional["Verifier"] = None


# ──────────────────────────────────────────────────────────────
# Tool Registry
# ──────────────────────────────────────────────────────────────

class ToolRegistry:
    """
    Central tool registry with allowlist enforcement.

    - Validates tool calls against schema
    - Enforces guest/destructive restrictions
    - Tracks modified paths for snapshots
    """

    def __init__(self):
        self._tools: Dict[ToolName, ToolSpec] = {}
        self._register_builtin_tools()

    def _register_builtin_tools(self) -> None:
        """Register all builtin tools."""
        from .builtin import (
            open_app, close_app, set_volume, media_control,
            search_web, read_file, write_file, run_script,
            get_time,
        )
        # The network tool lives in its own module: it is the only one whose
        # reason to change is an external provider.
        from .weather import get_weather

        # One lookup table, so every spec below is written once: a tool that
        # declares `destructive` or `guest_allowed` in one place and is verified
        # in another is how the two drift apart.
        verifiers = default_verifiers()

        self.register(ToolSpec(
            name=ToolName.OPEN_APP,
            description="Open an application",
            schema=ToolSchema(properties={"app": {"type": "string"}}, required=["app"]),
            handler=open_app,
            destructive=False,
            guest_allowed=True,  # filtered by sensitive list
            requires_confirmation=False,
            verifier=verifiers.get(ToolName.OPEN_APP.value),
        ))

        self.register(ToolSpec(
            name=ToolName.CLOSE_APP,
            description="Close an application",
            schema=ToolSchema(properties={"app": {"type": "string"}}, required=["app"]),
            handler=close_app,
            destructive=False,
            guest_allowed=True,
            requires_confirmation=False,
            verifier=verifiers.get(ToolName.CLOSE_APP.value),
        ))

        self.register(ToolSpec(
            name=ToolName.SET_VOLUME,
            description="Set the volume to an absolute level, or change it by a delta",
            # Either form is valid, so neither can be required: `delta` moves the
            # volume by percentage points, `level` is an absolute 0-100 target.
            # Requiring `delta` made every "调到 10%" call fail validation.
            schema=ToolSchema(
                properties={
                    "delta": {"type": "integer", "description": "percentage points to change by, negative to lower"},
                    "level": {"type": "integer", "minimum": 0, "maximum": 100, "description": "absolute target 0-100"},
                },
                required=[],
                at_least_one=["level", "delta"],
            ),
            handler=set_volume,
            destructive=False,
            guest_allowed=True,
            requires_confirmation=False,
            verifier=verifiers.get(ToolName.SET_VOLUME.value),
        ))

        self.register(ToolSpec(
            name=ToolName.MEDIA_CONTROL,
            description="Control media playback",
            schema=ToolSchema(properties={"action": {"type": "string", "enum": ["play", "pause", "next", "prev"]}}, required=["action"]),
            handler=media_control,
            destructive=False,
            guest_allowed=True,
            requires_confirmation=False,
        ))

        self.register(ToolSpec(
            name=ToolName.SEARCH_WEB,
            description="Search the web",
            schema=ToolSchema(properties={"query": {"type": "string"}}, required=["query"]),
            handler=search_web,
            destructive=False,
            guest_allowed=True,
            requires_confirmation=False,
        ))

        self.register(ToolSpec(
            name=ToolName.READ_FILE,
            description="Read a file",
            schema=ToolSchema(properties={"path": {"type": "string"}}, required=["path"]),
            handler=read_file,
            destructive=False,
            guest_allowed=False,
            requires_confirmation=False,
        ))

        self.register(ToolSpec(
            name=ToolName.WRITE_FILE,
            description="Write content to a file",
            schema=ToolSchema(properties={"path": {"type": "string"}, "content": {"type": "string"}}, required=["path", "content"]),
            handler=write_file,
            destructive=True,
            guest_allowed=False,
            requires_confirmation=True,
            modified_paths=["{args.path}"],  # template for snapshot
            verifier=verifiers.get(ToolName.WRITE_FILE.value),
        ))

        self.register(ToolSpec(
            name=ToolName.RUN_SCRIPT,
            description="Run a script file",
            schema=ToolSchema(properties={"path": {"type": "string"}}, required=["path"]),
            handler=run_script,
            destructive=True,
            guest_allowed=False,
            requires_confirmation=True,
            modified_paths=["{args.path}"],
            verifier=verifiers.get(ToolName.RUN_SCRIPT.value),
        ))

        # ── read-only queries ──────────────────────────────────
        # No arguments, nothing to confirm, nothing to snapshot: they answer
        # with speech instead of acting on the machine, so a guest may ask.

        self.register(ToolSpec(
            name=ToolName.GET_TIME,
            description="Report the current local time",
            schema=ToolSchema(properties={}, required=[]),
            handler=get_time,
            destructive=False,
            guest_allowed=True,
            requires_confirmation=False,
        ))

        self.register(ToolSpec(
            name=ToolName.GET_WEATHER,
            description="Report today's weather for a city",
            # `city` is optional: the rule layer fills it when the user named
            # one, and `weather.city` in the config is the fallback.
            schema=ToolSchema(properties={"city": {"type": "string"}}, required=[]),
            handler=get_weather,
            destructive=False,
            guest_allowed=True,
            requires_confirmation=False,
        ))

    def register(self, spec: ToolSpec) -> None:
        self._tools[spec.name] = spec

    def get(self, name: ToolName) -> Optional[ToolSpec]:
        return self._tools.get(name)

    def get_allowed(self, tier: str = "full") -> List[ToolSpec]:
        """Get tools allowed for a speaker tier."""
        allowed = []
        for spec in self._tools.values():
            if tier == "guest" and not spec.guest_allowed:
                continue
            if tier == "rejected":
                continue
            allowed.append(spec)
        return allowed

    def validate_call(self, tool: ToolName, args: Dict[str, Any], tier: str = "full") -> Optional[str]:
        """Validate tool call. Returns error message if invalid, None if OK."""
        spec = self.get(tool)
        if not spec:
            return f"Unknown tool: {tool}"

        if tier == "guest" and not spec.guest_allowed:
            return f"Tool {tool} not allowed for guest tier"

        if tier == "rejected":
            return "Speaker rejected"

        # Schema validation (basic)
        for required in spec.schema.required:
            if required not in args:
                return f"Missing required argument: {required}"

        if spec.schema.at_least_one and not any(k in args for k in spec.schema.at_least_one):
            return f"Missing argument: one of {spec.schema.at_least_one} is required"

        # Check irreversible patterns for run_script
        if tool == ToolName.RUN_SCRIPT and "path" in args:
            try:
                with open(args["path"], "r", encoding="utf-8") as f:
                    content = f.read()
                matches = scan_for_irreversible(content)
                if matches:
                    return f"Script contains irreversible operations: {matches}"
            except Exception:
                pass  # file may not exist yet, let handler handle it

        # Check write_file path restriction
        if tool == ToolName.WRITE_FILE and "path" in args:
            path = Path(args["path"]).resolve()
            user_dir = Path.home()
            try:
                path.relative_to(user_dir)
            except ValueError:
                return f"write_file only allowed under user directory: {user_dir}"

        return None


# ──────────────────────────────────────────────────────────────
# Singleton
# ──────────────────────────────────────────────────────────────

_registry: Optional[ToolRegistry] = None

def get_tool_registry() -> ToolRegistry:
    global _registry
    if _registry is None:
        _registry = ToolRegistry()
    return _registry


from pathlib import Path