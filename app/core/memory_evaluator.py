from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Protocol

logger = logging.getLogger(__name__)

# Topical categories (what the memory is about). Not exhaustive -- the
# system can expand this later; a message is never forced into one of these
# if the AI has a good reason to use something else.
MEMORY_CATEGORIES = {
    "identity",
    "preference",
    "habit",
    "goal",
    "project",
    "relationship",
    "skill",
    "routine",
    "constraint",
    "fact",
    "event",
}

# Temporal/durability classification (how long this is likely to stay true).
MEMORY_TYPES = {
    "permanent_fact",
    "long_term_fact",
    "preference",
    "habit",
    "goal",
    "current_project",
    "routine",
    "temporary_context",
    "event",
}


class BrainProtocol(Protocol):
    def generate_response(self, user_input: str, system_prompt: str, history: list[dict[str, str]] | None = None) -> str:
        ...


@dataclass
class MemoryEvaluation:
    """Structured output of the MemoryEvaluator -- a judgment, not a memory."""

    is_memory_worthy: bool
    score: float
    reason: str
    category: str | None = None
    memory_type: str | None = None
    stability: float = 0.5
    usefulness: float = 0.5
    importance: float = 0.5
    explicit_request: bool = False


_EVALUATOR_SYSTEM_PROMPT = """You are Miki's memory evaluator. Your only job is to judge whether a single \
user message contains durable information worth remembering long-term about the user -- based strictly on MEANING and SEMANTIC ROLE (reason about underlying concepts rather than relying on keyword matching).

Judge on these dimensions:
1. Personal relevance: is this specifically a durable fact, habit, goal, project, or preference about the user?
2. NOT AN ACTION/TOOL REQUEST: Requests to Miki to perform an action, schedule an event, delete an item, look up information, open a file, send a message, or answer a question are NOT memories about the user!
3. Long-term stability: will this likely still be true in weeks or months?
4. Explicit intent: explicit requests ("remember that...", "keep in mind...") get high priority if they describe a durable fact/preference.

EXAMPLES THAT ARE NOT MEMORY (MUST RETURN is_memory_worthy: false, score: 0.0, reason: "action_request" or "temporary_state"):
- "Add gym to my calendar at 7pm." -> Action request (DO NOT create a memory like "User wants events added to calendar")
- "Put a meeting on my calendar tomorrow." -> Action request
- "Delete my appointment." -> Action request
- "Search for flights to Amsterdam." -> Action request
- "Look up Nvidia stock." -> Action request
- "Remind me tomorrow." -> Action request / command
- "What's on my calendar?" -> Question / recall request
- "I'm at the gym right now." -> Temporary situational context
- "I'm tired today." -> Temporary state
- "My friend said 'I love going to the gym'." -> Quoted third-party statement

EXAMPLES OF GENUINE DURABLE MEMORY (RETURN is_memory_worthy: true, score >= 0.70):
- "I study Business Analytics." -> Stable personal fact (identity/fact)
- "I usually train four times per week." -> Habit/routine
- "I prefer working out in the evening." -> Preference
- "Schedule my gym sessions at 7pm whenever possible." -> Stated user preference/routine rule
- "Remember that I don't like meetings before 10." -> Explicit preference request
- "I'm working on a personal AI agent project." -> Project

MIXED MESSAGES (ACTION + PERSONAL DISCLOSURE):
- "Add gym tomorrow at 7. I usually train around that time." -> Contains BOTH an action and a durable habit. Evaluate ONLY the habit ("User usually trains around 7pm"). Return is_memory_worthy: true.

UNCERTAIN / HYPOTHETICAL MESSAGES (RETURN score 0.35 - 0.60):
- "I've been thinking about switching my training schedule." -> Tentative idea (candidate)
- "I might start running." -> Hypothetical thought (candidate)

Respond ONLY with valid JSON matching exactly this shape:
{"is_memory_worthy": true or false, "score": 0.0-1.0, "reason": "one short classification sentence", \
"category": one of [identity, preference, habit, goal, project, relationship, skill, routine, constraint, \
fact, event] or null, "memory_type": one of [permanent_fact, long_term_fact, preference, habit, goal, \
current_project, routine, temporary_context, event] or null, "stability": 0.0-1.0, "usefulness": 0.0-1.0, \
"importance": 0.0-1.0, "explicit_request": true or false}"""


class MemoryEvaluator:
    """Decides whether a message is worth remembering, using the Brain to
    reason about meaning rather than matching keywords.

    Deliberately separate from extraction and storage: this component only
    judges; it never writes to any store.
    """

    def __init__(self, brain: BrainProtocol) -> None:
        self.brain = brain

    def evaluate(
        self,
        user_input: str,
        *,
        explicit_request_hint: bool = False,
        related_context: list[str] | None = None,
    ) -> MemoryEvaluation:
        text = (user_input or "").strip()
        if not text:
            return MemoryEvaluation(is_memory_worthy=False, score=0.0, reason="Empty message.")

        prompt = self._build_prompt(text, explicit_request_hint=explicit_request_hint, related_context=related_context)

        try:
            response_text = self.brain.generate_response(
                user_input=prompt,
                system_prompt=_EVALUATOR_SYSTEM_PROMPT,
                history=None,
            )
            payload = _parse_json_response(response_text)
            return self._from_payload(payload)
        except Exception:
            logger.exception("MemoryEvaluator failed to evaluate message; treating as not memory-worthy")
            return MemoryEvaluation(is_memory_worthy=False, score=0.0, reason="Evaluation failed; treated conservatively as not memory-worthy.")

    @staticmethod
    def _build_prompt(text: str, *, explicit_request_hint: bool, related_context: list[str] | None) -> str:
        lines = [f'User message: "{text}"']
        if explicit_request_hint:
            lines.append(
                "Note: this message looks like it may explicitly ask Miki to remember something "
                "(e.g. \"remember that...\", \"keep in mind...\", \"from now on...\"). If so, weigh that heavily."
            )
        if related_context:
            lines.append("Existing related memories (for context only, do not just repeat them back):")
            for item in related_context[:5]:
                lines.append(f"- {item}")
        lines.append("\nEvaluate this message now and respond with the JSON object only.")
        return "\n".join(lines)

    @staticmethod
    def _from_payload(payload: dict) -> MemoryEvaluation:
        category = payload.get("category")
        if category is not None:
            category = str(category).strip().lower() or None
            if category not in MEMORY_CATEGORIES:
                category = category or None

        memory_type = payload.get("memory_type")
        if memory_type is not None:
            memory_type = str(memory_type).strip().lower().replace(" ", "_") or None

        return MemoryEvaluation(
            is_memory_worthy=bool(payload.get("is_memory_worthy", False)),
            score=_clamp(payload.get("score", 0.0)),
            reason=str(payload.get("reason", "")).strip()[:280],
            category=category,
            memory_type=memory_type,
            stability=_clamp(payload.get("stability", 0.5)),
            usefulness=_clamp(payload.get("usefulness", 0.5)),
            importance=_clamp(payload.get("importance", 0.5)),
            explicit_request=bool(payload.get("explicit_request", False)),
        )


def _clamp(value, *, low: float = 0.0, high: float = 1.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return (low + high) / 2
    return max(low, min(high, number))


def _parse_json_response(response_text: str) -> dict:
    cleaned = (response_text or "").strip()
    if cleaned.startswith("```json"):
        cleaned = cleaned[7:]
    if cleaned.startswith("```"):
        cleaned = cleaned[3:]
    if cleaned.endswith("```"):
        cleaned = cleaned[:-3]
    return json.loads(cleaned.strip())
