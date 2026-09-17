"""
Context Management.

Session-scoped conversation context with turn tracking.
Cleared on sleep; no cross-session memory in MVP.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional

from winvoice.config import get_config
from winvoice.logging import get_logger

logger = get_logger(__name__)


@dataclass
class ContextTurn:
    """Single conversation turn."""
    user_text: str
    intent: str
    response: str
    timestamp: float = field(default_factory=time.time)
    trace_id: str = ""


class ContextManager:
    """
    Manages conversation context within a session.

    - Stores recent turns (configurable max_turns)
    - Provides context for LLM prompts
    - Cleared on sleep/timeout
    """

    def __init__(self, max_turns: Optional[int] = None, enabled: Optional[bool] = None):
        cfg = get_config()
        self.max_turns = max_turns or cfg.get("context.max_turns", 10)
        self.enabled = enabled if enabled is not None else cfg.get("context.enabled", True)
        self._turns: Deque[ContextTurn] = deque(maxlen=self.max_turns)
        self._session_start = time.time()

    def add_turn(self, user_text: str, intent: str, response: str, trace_id: str = "") -> None:
        """Add a conversation turn."""
        if not self.enabled:
            return
        turn = ContextTurn(
            user_text=user_text,
            intent=intent,
            response=response,
            trace_id=trace_id,
        )
        self._turns.append(turn)
        logger.debug("context_turn_added", trace_id=trace_id, turns=len(self._turns))

    def get_recent_turns(self, n: Optional[int] = None) -> List[ContextTurn]:
        """Get recent turns for context injection."""
        n = n or self.max_turns
        return list(self._turns)[-n:]

    def format_for_prompt(self) -> str:
        """Format context as a string for LLM prompt injection."""
        if not self._turns:
            return ""

        lines = ["Previous conversation:"]
        for turn in self._turns:
            lines.append(f"  User: {turn.user_text}")
            lines.append(f"  Intent: {turn.intent}")
            lines.append(f"  Assistant: {turn.response}")
        return "\n".join(lines)

    def clear(self) -> None:
        """Clear all context (e.g., on sleep)."""
        self._turns.clear()
        logger.info("context_cleared")

    def is_expired(self, timeout_seconds: int = 3600) -> bool:
        """Check if session has expired (default 1 hour)."""
        return (time.time() - self._session_start) > timeout_seconds

    @property
    def turn_count(self) -> int:
        return len(self._turns)


# ──────────────────────────────────────────────────────────────
# Singleton
# ──────────────────────────────────────────────────────────────

_context_manager: Optional[ContextManager] = None

def get_context_manager() -> ContextManager:
    global _context_manager
    if _context_manager is None:
        _context_manager = ContextManager()
    return _context_manager


def reset_context_manager() -> None:
    """For testing."""
    global _context_manager
    _context_manager = None