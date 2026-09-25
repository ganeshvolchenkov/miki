from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.core.memory_evaluator import MEMORY_CATEGORIES, MemoryEvaluation
from app.memory.brain import derive_entities, display_name, entity_key, is_junk_entity

logger = logging.getLogger(__name__)


class BrainProtocol(Protocol):
    def generate_response(self, user_input: str, system_prompt: str, history: list[dict[str, str]] | None = None) -> str:
        ...


@dataclass
class MemoryExtraction:
    """Canonical memory content produced from a raw message, plus the
    classification carried over from the evaluation."""

    content: str
    category: str
    memory_type: str
    # Knowledge-graph metadata (empty when the model didn't supply any).
    title: str = ""
    entities: list[str] = field(default_factory=list)
    entity_types: dict[str, str] = field(default_factory=dict)


_MAX_MEMORIES_PER_MESSAGE = 4
_MAX_ENTITIES_PER_MEMORY = 5
_ENTITY_TYPES = {"person", "place", "organization", "interest", "subject", "object", "event", "other"}

_EXTRACTOR_SYSTEM_PROMPT = """You turn a user's natural-language message into concise, canonical \
long-term memory statements for Miki (a personal AI assistant), and describe how each connects to the \
people, places and interests in the user's life (Miki stores these as a knowledge graph).

Rules:
- Third person ("User usually..."), not first/second person.
- Concise and factual -- strip filler, hedging, and conversational framing.
- Self-contained: understandable months later, with no reference to "this conversation" or "that".
- Do not invent details that weren't in the message.
- If the message gives an approximate/range figure (e.g. "four or five times"), keep the range rather than \
picking one number arbitrarily, unless the message clearly settles on one.
- If the message states several independent durable facts, output one memory per fact (at most 4). \
Otherwise output exactly one memory. Never split one fact into pieces.
- "title": a 2-6 word statement title, not a bare noun and never starting with "User" (e.g. "Loves hiking", \
"Lives in Utrecht", "Mom's name is Maria").
- "category": one of identity, preference, habit, goal, project, relationship, skill, routine, constraint, fact, event.
- "entities": the specific people, places, organizations, interests/activities, objects or subjects the memory \
is about (at most 5), always including the main activity or interest when there is one. Never use days, dates, \
times or generic words as entities. Use the noun itself ("Hiking", "Utrecht", "Maria"), never "User". If a relative or \
friend is mentioned, include the role as its own person entity too ("Dad"). Entity "type" is one of: person, \
place, organization, interest, subject, object, event, other.

Example:
User: "I've been going to the gym around four or five times every week recently."
{"memories": [{"content": "User usually goes to the gym 4-5 times per week.", "title": "Goes to the gym often", \
"category": "habit", "entities": [{"name": "Gym", "type": "interest"}]}]}

Example:
User: "I love hiking, and my mom Maria studies law in Utrecht."
{"memories": [{"content": "User loves hiking.", "title": "Loves hiking", "category": "preference", "entities": [{"name": "Hiking", "type": "interest"}]}, {"content": "User's mom Maria studies law in Utrecht.", "title": "Mom Maria studies law", "category": "relationship", "entities": [{"name": "Maria", "type": "person"}, {"name": "Mom", "type": "person"}, {"name": "Law", "type": "subject"}, {"name": "Utrecht", "type": "place"}]}]}

Respond ONLY with valid JSON (no markdown, no extra text):
{"memories": [{"content": "...", "title": "...", "category": "...", "entities": [{"name": "...", "type": "..."}]}]}"""


class MemoryExtractor:
    """Converts a message the MemoryEvaluator already judged worthy into concise
    canonical memories (usually one, several for compound messages) with a
    title and graph entities. Never decides worthiness itself."""

    def __init__(self, brain: BrainProtocol) -> None:
        self.brain = brain

    def extract(self, user_input: str, evaluation: MemoryEvaluation) -> MemoryExtraction:
        """The primary memory only (kept for callers that expect a single result)."""
        return self.extract_all(user_input, evaluation)[0]

    def extract_all(self, user_input: str, evaluation: MemoryEvaluation) -> list[MemoryExtraction]:
        text = (user_input or "").strip()
        category = evaluation.category or "fact"
        memory_type = evaluation.memory_type or "long_term_fact"

        if not text:
            return [MemoryExtraction(content="", category=category, memory_type=memory_type)]

        try:
            prompt = f'User message: "{text}"\n\nExtract the canonical memories now and respond with the JSON object only.'
            response_text = self.brain.generate_response(
                user_input=prompt,
                system_prompt=_EXTRACTOR_SYSTEM_PROMPT,
                history=None,
            )
            payload = _parse_json_response(response_text)
            items = payload.get("memories") if isinstance(payload.get("memories"), list) else [payload]
            extractions = [
                built
                for built in (
                    self._build(item, default_category=category, memory_type=memory_type)
                    for item in items[:_MAX_MEMORIES_PER_MESSAGE]
                )
                if built is not None
            ]
            if not extractions:
                raise ValueError("empty content")
            return extractions
        except Exception:
            logger.warning("MemoryExtractor failed to produce canonical content; falling back to normalized raw text")
            content = _normalize_fallback(text)
            entities, types = derive_entities(content)
            return [MemoryExtraction(content=content, category=category, memory_type=memory_type, entities=entities, entity_types=types)]

    @staticmethod
    def _build(item: Any, *, default_category: str, memory_type: str) -> MemoryExtraction | None:
        if not isinstance(item, dict):
            return None
        content = str(item.get("content", "")).strip()
        if not content:
            return None

        category = str(item.get("category") or "").strip().lower()
        if category not in MEMORY_CATEGORIES:
            category = default_category

        entities, types = _clean_entities(item.get("entities"))
        if not entities:
            entities, types = derive_entities(content)

        return MemoryExtraction(
            content=content,
            category=category,
            memory_type=memory_type,
            title=_clean_title(item.get("title")),
            entities=entities,
            entity_types=types,
        )


def _clean_title(value: Any) -> str:
    title = re.sub(r"\s+", " ", str(value or "")).strip(" .\"'")
    return title[:80]


def _clean_entities(raw: Any) -> tuple[list[str], dict[str, str]]:
    entities: list[str] = []
    types: dict[str, str] = {}
    seen: set[str] = set()
    for item in raw if isinstance(raw, list) else []:
        name, kind = (item.get("name"), item.get("type")) if isinstance(item, dict) else (item, None)
        name = display_name(str(name or ""))
        key = entity_key(name)
        if is_junk_entity(key) or key in seen:
            continue
        seen.add(key)
        entities.append(name)
        kind = str(kind or "other").strip().lower()
        types[name] = kind if kind in _ENTITY_TYPES else "other"
        if len(entities) >= _MAX_ENTITIES_PER_MEMORY:
            break
    return entities, types


def _normalize_fallback(text: str) -> str:
    cleaned = re.sub(r"\s+", " ", text).strip()
    return cleaned.rstrip("?!. ")


def _parse_json_response(response_text: str) -> dict:
    cleaned = (response_text or "").strip()
    if cleaned.startswith("```json"):
        cleaned = cleaned[7:]
    if cleaned.startswith("```"):
        cleaned = cleaned[3:]
    if cleaned.endswith("```"):
        cleaned = cleaned[:-3]
    return json.loads(cleaned.strip())
