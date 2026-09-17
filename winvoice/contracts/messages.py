"""
Message definitions for the voice assistant pipeline.

Schema versioning:
- Every message has `schema_version: int = 1`
- Consumers MUST use `model_validator(mode='before')` to provide defaults for missing fields
- Breaking changes increment major version and require coordinated deployment
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, Field, model_validator


# ──────────────────────────────────────────────────────────────
# Base & Enums
# ──────────────────────────────────────────────────────────────

class ProcessName(str, Enum):
    MAIN = "main"
    AUDIO = "audio"
    LLM = "llm"
    EXECUTION = "execution"


class SystemStateName(str, Enum):
    IDLE = "idle"
    KWS_LISTENING = "kws_listening"
    VAD_ACTIVE = "vad_active"
    ASR_RUNNING = "asr_running"
    LLM_THINKING = "llm_thinking"
    TTS_PLAYING = "tts_playing"
    ERROR = "error"


class SpeakerTier(str, Enum):
    FULL = "full"
    GUEST = "guest"
    REJECTED = "rejected"


class ToolName(str, Enum):
    OPEN_APP = "open_app"
    CLOSE_APP = "close_app"
    SET_VOLUME = "set_volume"
    MEDIA_CONTROL = "media_control"
    SEARCH_WEB = "search_web"
    READ_FILE = "read_file"
    WRITE_FILE = "write_file"
    RUN_SCRIPT = "run_script"


class MediaAction(str, Enum):
    PLAY = "play"
    PAUSE = "pause"
    NEXT = "next"
    PREV = "prev"


class IntentName(str, Enum):
    OPEN_APP = "open_app"
    CLOSE_APP = "close_app"
    SET_VOLUME = "set_volume"
    MEDIA_CONTROL = "media_control"
    SEARCH_WEB = "search_web"
    READ_FILE = "read_file"
    WRITE_FILE = "write_file"
    RUN_SCRIPT = "run_script"
    GET_TIME = "get_time"
    GET_WEATHER = "get_weather"
    UNKNOWN = "unknown"


# ──────────────────────────────────────────────────────────────
# Audio Pipeline Messages
# ──────────────────────────────────────────────────────────────

class AudioFrame(BaseModel):
    """Raw PCM frame from microphone (16 kHz, mono, int16)."""
    schema_version: int = 1
    trace_id: str
    span_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:8])
    timestamp_ms: int
    data: bytes  # int16 little-endian
    sample_rate: int = 16000
    channels: int = 1
    frame_ms: int = 10


class KwsTriggered(BaseModel):
    """Wake-word detected."""
    schema_version: int = 1
    trace_id: str
    span_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:8])
    keyword: str
    confidence: float
    timestamp_ms: int


class SvResult(BaseModel):
    """Speaker verification result."""
    schema_version: int = 1
    trace_id: str
    span_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:8])
    speaker_id: str  # enrolled speaker label, e.g. "me"
    score: float
    tier: SpeakerTier
    threshold_high: float
    threshold_low: float


class VadSegment(BaseModel):
    """Voice activity segment ready for ASR."""
    schema_version: int = 1
    trace_id: str
    span_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:8])
    audio_frames: List[AudioFrame]
    start_ms: int
    end_ms: int


class AsrResult(BaseModel):
    """ASR transcription result."""
    schema_version: int = 1
    trace_id: str
    span_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:8])
    text: str
    language: str
    confidence: float
    is_final: bool = True


# ──────────────────────────────────────────────────────────────
# Intent & LLM Messages
# ──────────────────────────────────────────────────────────────

class IntentResult(BaseModel):
    """Structured intent from rules / classifier / cloud."""
    schema_version: int = 1
    trace_id: str
    span_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:8])
    intent: IntentName
    args: Dict[str, Any]
    confidence: float
    source: Literal["rules", "local", "cloud"]
    needs_cloud: bool = False
    raw_text: str = ""


# ──────────────────────────────────────────────────────────────
# Tool Execution Messages
# ──────────────────────────────────────────────────────────────

class ToolCall(BaseModel):
    """Tool invocation request."""
    schema_version: int = 1
    trace_id: str
    span_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:8])
    tool: ToolName
    args: Dict[str, Any]
    requires_confirmation: bool = False
    modified_paths: List[str] = Field(default_factory=list)  # for snapshot


class ToolResult(BaseModel):
    """Tool execution result."""
    schema_version: int = 1
    trace_id: str
    span_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:8])
    tool: ToolName
    success: bool
    result: Any = None
    error: Optional[str] = None
    snapshot_id: Optional[str] = None


# ──────────────────────────────────────────────────────────────
# TTS Messages
# ──────────────────────────────────────────────────────────────

class TtsRequest(BaseModel):
    """Request to synthesize speech."""
    schema_version: int = 1
    trace_id: str
    span_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:8])
    text: str
    voice: str = "default"  # or "guest"
    sample_rate: int = 16000


class TtsChunk(BaseModel):
    """Streaming audio chunk from TTS."""
    schema_version: int = 1
    trace_id: str
    span_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:8])
    data: bytes  # int16 PCM
    is_final: bool = False


class InterruptTTS(BaseModel):
    """Immediate TTS interruption (barge-in)."""
    schema_version: int = 1
    trace_id: str
    span_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:8])
    reason: str = "kws_barge_in"


# ──────────────────────────────────────────────────────────────
# Config & System Messages
# ──────────────────────────────────────────────────────────────

class ConfigChanged(BaseModel):
    """Hot-reloadable config change notification."""
    schema_version: int = 1
    trace_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:16])
    changed_keys: List[str]
    new_values: Dict[str, Any]


class SystemState(BaseModel):
    """Current pipeline state for UI."""
    schema_version: int = 1
    trace_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:16])
    state: SystemStateName
    message: str = ""


class ErrorReport(BaseModel):
    """Structured error for logging/crash reporting."""
    schema_version: int = 1
    trace_id: str
    span_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:8])
    process: ProcessName
    error_type: str
    message: str
    context: Dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=datetime.utcnow)


# ──────────────────────────────────────────────────────────────
# Validation Helpers
# ──────────────────────────────────────────────────────────────

def ensure_trace_id(data: Dict[str, Any]) -> Dict[str, Any]:
    """Validator to ensure trace_id exists (backward compat)."""
    if "trace_id" not in data:
        data["trace_id"] = uuid.uuid4().hex[:16]
    return data


# Apply to all models
for model in [
    AudioFrame, KwsTriggered, SvResult, VadSegment, AsrResult,
    IntentResult, ToolCall, ToolResult, TtsRequest, TtsChunk,
    InterruptTTS, ConfigChanged, SystemState, ErrorReport
]:
    model.model_validator(mode="before")(ensure_trace_id)