"""
Speaker enrollment module.

Usage:
    python -m winvoice.enroll --speaker me --samples 8

The implementation lives in ``session.py``; ``__main__.py`` is a thin
entry point so the package stays importable without side effects.
"""

from .session import EnrollmentSession, main

__all__ = ["EnrollmentSession", "main"]
