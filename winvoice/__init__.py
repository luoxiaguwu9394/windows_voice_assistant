"""
Windows Voice Assistant - Local-first voice assistant for Windows.

Architecture:
- Audio Pipeline: KWS → SV → VAD → ASR (sherpa-onnx)
- Intent Routing: Rules → Local LLM → Cloud LLM
- Tool Execution: Allowlist + Snapshot rollback
- Speaker Verification: CAM++ embeddings with 3-tier permissions
"""

__version__ = "0.1.0-dev"
__author__ = "Windows Voice Assistant Team"

from .config import get_config, ConfigManager
from .logging import configure_logging, get_logger
from .contracts import *
from .audio import *
from .llm import *
from .intent import *
from .tools import *
from .context import ContextManager, get_context_manager

__all__ = [
    "get_config",
    "ConfigManager",
    "configure_logging",
    "get_logger",
    "AudioPipeline",
    "PipelineState",
    "PipelineContext",
    "AudioStreamManager",
    "StreamConfig",
    "create_intent_router",
    "create_tool_executor",
    "create_local_llm_backend",
    "create_remote_llm_backend",
    "create_llm_router",
    "ContextManager",
    "get_context_manager",
]