"""
Pydantic message contracts for inter-process communication.
All messages carry schema_version for forward compatibility.

`speech.py` sits alongside them: what the Chinese TTS can actually pronounce is
a contract every layer has to honour, not a private detail of whichever tool
happened to author the sentence.
"""

from .messages import *
from .speech import MAX_SPEECH_CHARS, clip_for_speech, has_latin, sanitize_for_tts

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
    "MAX_SPEECH_CHARS",
    "clip_for_speech",
    "has_latin",
    "sanitize_for_tts",
]
