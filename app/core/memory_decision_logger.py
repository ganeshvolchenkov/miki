from __future__ import annotations

import logging

from app.core.memory_evaluator import MemoryEvaluation

logger = logging.getLogger("app.core.memory_decisions")


class MemoryDecisionLogger:
    """Structured, low-noise logging of memory decisions for debugging.

    Only a short classification reason is logged -- never verbose
    chain-of-thought, never shown to the user during normal operation.
    """

    def log_evaluation(self, user_input: str, evaluation: MemoryEvaluation) -> None:
        decision = "MEMORY" if evaluation.is_memory_worthy else "NOT MEMORY"
        logger.info(
            "Memory decision: %s | score=%.2f | category=%s | reason=%s | input=%r",
            decision,
            evaluation.score,
            evaluation.category,
            evaluation.reason,
            _truncate(user_input),
        )

    def log_candidate(self, user_input: str, evaluation: MemoryEvaluation) -> None:
        logger.info(
            "Memory decision: CANDIDATE (uncertain) | score=%.2f | reason=%s | input=%r",
            evaluation.score,
            evaluation.reason,
            _truncate(user_input),
        )

    def log_comparison(self, relationship: str, reason: str) -> None:
        logger.info("Memory comparison: relationship=%s | reason=%s", relationship, reason)


def _truncate(text: str, limit: int = 120) -> str:
    text = text or ""
    return text if len(text) <= limit else text[: limit - 1] + "…"
