"""
DeepSeek Harness as the agent layer of the voice assistant.

`new_way.md` places DSH between the audio pipeline and the machine:

    Speech → ASR → [rules] → Local DSH → Cloud DSH → tools → Windows → TTS

The existing audio pipeline keeps KWS / VAD / ASR / TTS; DSH takes over
reasoning, planning and tool calling. The tool layer underneath it is unchanged —
`winvoice/tools/` remains the only implementation of the allowlist, the snapshot
logic, the permission tiers and the verifiers, and DSH reaches it over MCP
(`winvoice/mcp_server.py`) rather than through a parallel implementation.

This is the design recorded in `docs/dsh_integration_design.md`, with the tool
bridge changed from generated TypeScript plugins to MCP; see that document's
"Bridge" note for why.
"""

from .backend import DSHBackend, DSHTurn, HarnessBackend, RecordingBackend, build_backend
from .bridge import write_bundle
from .router import DSHRouter, RoutedRequest, build_escalation_context
from .settings import DSHSettings, TierSettings, load_settings
from .validation import ToolActivity, TurnAssessment, assess_turn, extract_tool_activity

__all__ = [
    "DSHBackend",
    "DSHRouter",
    "DSHSettings",
    "DSHTurn",
    "HarnessBackend",
    "RecordingBackend",
    "RoutedRequest",
    "TierSettings",
    "ToolActivity",
    "TurnAssessment",
    "assess_turn",
    "build_backend",
    "build_escalation_context",
    "extract_tool_activity",
    "load_settings",
    "write_bundle",
]
