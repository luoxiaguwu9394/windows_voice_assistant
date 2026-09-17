"""
LLM Backends: Local (Ollama) and Remote (OpenAI-compatible).

Both provide constrained decoding via llama.cpp GBNF grammar.
"""

from .local import LocalLlmBackend, create_local_llm_backend
from .remote import RemoteLlmBackend, create_remote_llm_backend
from .router import LlmRouter, create_llm_router

__all__ = [
    "LocalLlmBackend",
    "create_local_llm_backend",
    "RemoteLlmBackend",
    "create_remote_llm_backend",
    "LlmRouter",
    "create_llm_router",
]