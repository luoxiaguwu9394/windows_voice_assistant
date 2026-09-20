"""
MCP stdio server: the tool registry, as DeepSeek Harness sees it.

DeepSeek Harness runs its agent loop in Node and calls tools over MCP. This
module is the bridge, and it is deliberately thin: it converts a `tools/call`
into the *same* `ToolCall` the in-process pipeline builds and hands it to the
*same* `ToolExecutor`. Nothing is re-implemented for the agent.

That is the whole security argument for the integration. `new_way.md` requires
one tool registry and one permission boundary shared by every model tier, and
`docs/dsh_integration_design.md` first proposed generating a TypeScript plugin
per tool plus an HTTP bridge back into Python. A second tool implementation
living in another language is exactly what the requirement forbids, and it would
have been a second place for the allowlist, the snapshot logic and the verifier
to drift out of step. MCP already exists to carry a tool surface across that
boundary, DSH already ships a client for it (`@deepseek-ai/dsh-mcp-client`), so
the bridge is a protocol rather than a hand-written shim.

Wire shape: JSON-RPC over stdio. **Never write to stdout** — that is the wire.
`configure_logging` is called before any tool module is imported so the loggers
those modules create at import time bind to the file handler rather than to
structlog's stdout default.

Tier enforcement: the tool *list* is deliberately not tier-filtered. Filtering it
would make the model's tool schema change between an owner's turn and a guest's,
which churns the prompt prefix and, because DSH only re-syncs an MCP server's
tools on a `list_changed` notification, would leave the agent holding a stale
list. The tier is instead enforced where it matters — at `tools/call` — using the
in-flight utterance record (`winvoice/tools/utterance.py`).
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any, Dict, Optional

# Two things must be true before anything else is imported:
#
# 1. Logging goes to a FILE, never stdout. `structlog.BytesLoggerFactory()`
#    writes to `sys.stdout.buffer` when given no file, and stdout here is the
#    JSON-RPC wire — the events used to be written straight onto it, and only
#    the `mcp` SDK's own fd diversion kept the protocol alive.
# 2. The config loads leniently. DeepSeek Harness spawns this process with the
#    environment scrubbed of anything matching /KEY|PASSWORD|SECRET|TOKEN/i, so
#    `DEEPSEEK_API_KEY` is *supposed* to be absent; a strict load would abort
#    the tool server over a secret it does not need.
import os as _os

_os.environ.setdefault("WINVOICE_CONFIG_TOLERANT", "1")

from winvoice.logging import configure_logging, get_logger  # noqa: E402

configure_logging(process_name="mcp", stdout_is_a_wire=True)

from winvoice import _vendor  # noqa: E402

_vendor.ensure("mcp")

import mcp.server.stdio  # noqa: E402
import mcp_types as types  # noqa: E402
from mcp.server.lowlevel import Server  # noqa: E402

from winvoice.contracts import SpeakerTier, ToolCall, ToolName, ToolResult  # noqa: E402
from winvoice.tools import utterance  # noqa: E402
from winvoice.tools.executor import ToolExecutor  # noqa: E402

logger = get_logger(__name__)

SERVER_NAME = "winvoice"

#: Longest string a tool result may contribute to the model's context. Scripts
#: and file reads can produce megabytes; the model needs the gist, and the whole
#: value would otherwise ride in every subsequent request.
MAX_FIELD_CHARS = 2000

#: Fields of a raw handler result that are forwarded verbatim, bounded.
_FORWARDED_RESULT_FIELDS = ("stdout", "stderr", "returncode", "path", "time", "date", "content")


def to_json_schema(schema: Any) -> Dict[str, Any]:
    """
    `ToolSchema` (this project's shape) as the JSON Schema MCP advertises.

    `at_least_one` has no JSON-Schema-`required` equivalent — it is the
    cross-field rule `set_volume` needs, because `level` and `delta` are
    alternatives and neither can be required — so it becomes `anyOf`.
    """
    out: Dict[str, Any] = {
        "type": schema.type,
        "properties": schema.properties,
        "additionalProperties": schema.additionalProperties,
    }
    if schema.required:
        out["required"] = list(schema.required)
    if getattr(schema, "at_least_one", None):
        out["anyOf"] = [{"required": [key]} for key in schema.at_least_one]
    return out


def _bound(value: Any) -> Any:
    if isinstance(value, str) and len(value) > MAX_FIELD_CHARS:
        return value[:MAX_FIELD_CHARS] + f"...[+{len(value) - MAX_FIELD_CHARS} chars]"
    return value


def render_tool_result(result: ToolResult) -> str:
    """
    The tool result as the model reads it.

    `speak` is the important field: it carries the sentence the *tool* authored
    for the speaker. The pipeline has always spoken `ToolResult.message`
    directly, and that is how the Chinese-only TTS contract is kept — the tool
    knows whether it is allowed to say a path or an English word, and the model
    does not. The agent's instruction is therefore to relay `speak` verbatim
    rather than to paraphrase it, which would put an English token back in front
    of the speaker.

    `error` and `data` exist so the model can reason about a failure and decide
    what to do next; `verification` is what the machine said, which is the only
    thing escalation is allowed to act on.
    """
    payload: Dict[str, Any] = {"success": result.success}

    if result.message:
        payload["speak"] = result.message
    if result.error:
        payload["error"] = _bound(result.error)
    if result.verification:
        payload["verification"] = result.verification

    if isinstance(result.result, dict):
        data = {
            key: _bound(result.result[key])
            for key in _FORWARDED_RESULT_FIELDS
            if key in result.result
        }
        if data:
            payload["data"] = data

    return json.dumps(payload, ensure_ascii=False)


class WinvoiceMcpServer:
    """Adapts the tool registry onto the MCP request surface."""

    def __init__(self, executor: Optional[ToolExecutor] = None):
        self.executor = executor or ToolExecutor()
        self.registry = self.executor.registry

    # ── tools/list ─────────────────────────────────────────────

    def list_tools(self) -> list:
        tools = []
        for spec in self.registry.get_allowed(tier="full"):
            tools.append(
                types.Tool(
                    name=spec.name.value,
                    description=spec.description,
                    input_schema=to_json_schema(spec.schema),
                )
            )
        return tools

    # ── tools/call ─────────────────────────────────────────────

    async def call_tool(self, name: str, arguments: Dict[str, Any]) -> types.CallToolResult:
        try:
            tool = ToolName(name)
        except ValueError:
            logger.warning("mcp_unknown_tool", tool=name)
            return self._error(f"Unknown tool: {name}")

        context = utterance.current(path=utterance.resolve_from_config())
        tier = context.tier if context else "full"
        trace_id = context.trace_id if context else ""

        try:
            tier_enum = SpeakerTier(tier)
        except ValueError:
            tier_enum = SpeakerTier.FULL

        call_kwargs: Dict[str, Any] = {"tool": tool, "args": arguments, "tier": tier_enum}
        if trace_id:
            call_kwargs["trace_id"] = trace_id
        call = ToolCall(**call_kwargs)

        logger.info("mcp_tool_call", tool=name, tier=tier, args=arguments)
        result = await self.executor.execute(call, tier=tier)
        logger.info(
            "mcp_tool_result",
            tool=name,
            success=result.success,
            verification=(result.verification or {}).get("status"),
        )

        return types.CallToolResult(
            content=[types.TextContent(type="text", text=render_tool_result(result))],
            # A failed tool must reach the model as a failure. DSH's MCP client
            # turns `isError` into a failed tool result, which is the behaviour
            # this project wants: the model may not report success for a call
            # that did not succeed (see UNIMPLEMENTED.md §0).
            is_error=not result.success,
        )

    @staticmethod
    def _error(text: str) -> types.CallToolResult:
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=json.dumps({"success": False, "error": text}))],
            is_error=True,
        )


def build_server(executor: Optional[ToolExecutor] = None) -> Server:
    """The low-level MCP `Server`, wired to a registry-backed adapter."""
    adapter = WinvoiceMcpServer(executor=executor)

    async def on_list_tools(_ctx, _params):
        return types.ListToolsResult(tools=adapter.list_tools())

    async def on_call_tool(_ctx, params):
        return await adapter.call_tool(params.name, dict(params.arguments or {}))

    return Server(
        SERVER_NAME,
        version="0.1.0",
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
    )


async def serve() -> None:
    server = build_server()
    logger.info("mcp_server_starting", server=SERVER_NAME)
    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main() -> int:
    try:
        asyncio.run(serve())
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        # stderr, never stdout: stdout is the JSON-RPC wire.
        print(f"[winvoice-mcp] fatal: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
