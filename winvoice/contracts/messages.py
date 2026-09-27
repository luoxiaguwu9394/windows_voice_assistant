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
# Trace-id helper
#
# Every message carries a `trace_id`. Pydantic v2 has no way to attach a
# "before" validator after class creation, so each model declares trace_id
# with this default_factory: an omitted trace_id is generated, an explicit
# one is preserved.
# ──────────────────────────────────────────────────────────────

def new_trace_id() -> str:
    return uuid.uuid4().hex[:16]


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
    # A destructive call was announced and is waiting for the user's spoken
    # 确认/取消. In this state the microphone feeds VAD/ASR directly, without a
    # wake word — the answer to a spoken question must not need one.
    CONFIRMING = "confirming"
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
    LIST_DIR = "list_dir"
    WRITE_FILE = "write_file"
    RUN_SCRIPT = "run_script"
    # Power actions (shutdown/restart/…): confirmation-gated in the registry and
    # guest-denied, so a spoken 确认 from the owner is the only way through.
    SYSTEM_POWER = "system_power"
    # Read-only queries: no arguments, no confirmation, guest-allowed. They
    # answer with speech (`ToolResult.message`) instead of an action.
    GET_TIME = "get_time"
    GET_WEATHER = "get_weather"


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
    LIST_DIR = "list_dir"
    WRITE_FILE = "write_file"
    RUN_SCRIPT = "run_script"
    GET_TIME = "get_time"
    GET_WEATHER = "get_weather"
    # Power actions (「关机/重启/锁屏」…). Routed by the rule tier and gated by
    # the spoken-confirmation loop; a guest can reach neither.
    SYSTEM_POWER = "system_power"
    # Free-form question / small talk. Text in, text out — the answering model
    # must never get tool capability (spec.md §6.5).
    ASK = "ask"
    # 「没事了」 and friends: acknowledge and fall back to waiting for the wake
    # word, running nothing.
    DISMISS = "dismiss"
    UNKNOWN = "unknown"


# ──────────────────────────────────────────────────────────────
# Audio Pipeline Messages
# ──────────────────────────────────────────────────────────────

class AudioFrame(BaseModel):
    """Raw PCM frame from microphone (16 kHz, mono, int16)."""
    schema_version: int = 1
    trace_id: str = Field(default_factory=new_trace_id)
    span_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:8])
    timestamp_ms: int
    data: bytes  # int16 little-endian
    sample_rate: int = 16000
    channels: int = 1
    frame_ms: int = 10


class KwsTriggered(BaseModel):
    """Wake-word detected."""
    schema_version: int = 1
    trace_id: str = Field(default_factory=new_trace_id)
    span_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:8])
    keyword: str
    confidence: float
    timestamp_ms: int


class SvResult(BaseModel):
    """Speaker verification result."""
    schema_version: int = 1
    trace_id: str = Field(default_factory=new_trace_id)
    span_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:8])
    speaker_id: str  # enrolled speaker label, e.g. "me"
    score: float
    tier: SpeakerTier
    threshold_high: float
    threshold_low: float


class VadSegment(BaseModel):
    """Voice activity segment ready for ASR."""
    schema_version: int = 1
    trace_id: str = Field(default_factory=new_trace_id)
    span_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:8])
    audio_frames: List[AudioFrame]
    start_ms: int
    end_ms: int


class AsrResult(BaseModel):
    """ASR transcription result."""
    schema_version: int = 1
    trace_id: str = Field(default_factory=new_trace_id)
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
    trace_id: str = Field(default_factory=new_trace_id)
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
    trace_id: str = Field(default_factory=new_trace_id)
    span_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:8])
    tool: ToolName
    args: Dict[str, Any]
    requires_confirmation: bool = False
    modified_paths: List[str] = Field(default_factory=list)  # for snapshot
    # Who is asking. Carried on the call rather than passed alongside it because
    # the tier has to survive the hop into the tool executor *and* the hop into
    # the MCP server that DeepSeek Harness talks to; a parameter threaded
    # through callbacks would have been dropped at the second one.
    tier: SpeakerTier = SpeakerTier.FULL


class ToolResult(BaseModel):
    """Tool execution result.

    `error` is for logs and callers: it may name tools, argument keys and file
    paths, so it can contain English. `message` is what the assistant is
    allowed to *say* — plain Chinese, because the TTS model is Chinese-only and
    silently drops anything its lexicon does not contain.
    """
    schema_version: int = 1
    trace_id: str = Field(default_factory=new_trace_id)
    span_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:8])
    tool: ToolName
    success: bool
    result: Any = None
    error: Optional[str] = None
    message: Optional[str] = None
    snapshot_id: Optional[str] = None
    # What the machine showed, when a verifier was able to look (see
    # `winvoice/tools/verifier.py`). Deliberately separate from `success`:
    # `success` is the tool's honest claim and is what the user hears, while
    # this decides whether the task is unresolved and worth escalating.
    verification: Optional[Dict[str, Any]] = None


# ──────────────────────────────────────────────────────────────
# TTS Messages
# ──────────────────────────────────────────────────────────────

class TtsRequest(BaseModel):
    """Request to synthesize speech."""
    schema_version: int = 1
    trace_id: str = Field(default_factory=new_trace_id)
    span_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:8])
    text: str
    voice: str = "default"  # or "guest"
    sample_rate: int = 16000
    # How many characters of `text` may be spoken. The TTS synthesises sentence
    # by sentence now, so length costs latency per segment rather than blocking
    # the first sound — but a long reply is still a long monologue, and the
    # caller is the only one that knows whether this is a tool one-liner (80) or
    # an agent's answer (240). See `winvoice/contracts/speech.py`.
    max_chars: int = 80


class TtsChunk(BaseModel):
    """Streaming audio chunk from TTS."""
    schema_version: int = 1
    trace_id: str = Field(default_factory=new_trace_id)
    span_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:8])
    data: bytes  # int16 PCM
    is_final: bool = False


class InterruptTTS(BaseModel):
    """Immediate TTS interruption (barge-in)."""
    schema_version: int = 1
    trace_id: str = Field(default_factory=new_trace_id)
    span_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:8])
    reason: str = "kws_barge_in"


# ──────────────────────────────────────────────────────────────
# Config & System Messages
# ──────────────────────────────────────────────────────────────

class ConfigChanged(BaseModel):
    """Hot-reloadable config change notification."""
    schema_version: int = 1
    trace_id: str = Field(default_factory=new_trace_id)
    changed_keys: List[str]
    new_values: Dict[str, Any]


class SystemState(BaseModel):
    """Current pipeline state for UI."""
    schema_version: int = 1
    trace_id: str = Field(default_factory=new_trace_id)
    state: SystemStateName
    message: str = ""


class ErrorReport(BaseModel):
    """Structured error for logging/crash reporting."""
    schema_version: int = 1
    trace_id: str = Field(default_factory=new_trace_id)
    span_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:8])
    process: ProcessName
    error_type: str
    message: str
    context: Dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=datetime.utcnow)


