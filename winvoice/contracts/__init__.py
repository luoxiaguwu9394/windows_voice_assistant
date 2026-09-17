"""
Pydantic message contracts for inter-process communication.
All messages carry schema_version for forward compatibility.
"""

from .messages import *

__all__ = [
    "AudioFrame",
    "KwsTriggered",
    "SvResult",
    "VadSegment",
    "AsrResult",
    "IntentResult",
    "ToolCall",
    "ToolResult",
    "TtsRequest",
    "TtsChunk",
    "InterruptTTS",
    "ConfigChanged",
    "SystemState",
    "ErrorReport",
]