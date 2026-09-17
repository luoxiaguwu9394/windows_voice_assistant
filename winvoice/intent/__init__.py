"""
Intent processing: rules, classifier, router.

Three-tier waterfall: Rules → Local LLM → Cloud LLM.
"""

from .rules import match_rules, RULE_PATTERNS
from .classifier import IntentClassifier
from .router import IntentRouter

__all__ = [
    "match_rules",
    "RULE_PATTERNS",
    "IntentClassifier",
    "IntentRouter",
]