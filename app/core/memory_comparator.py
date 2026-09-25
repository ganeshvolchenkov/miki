from __future__ import annotations

import difflib
import json
import logging
import math
from dataclasses import dataclass
from typing import Protocol, Sequence

from app.memory.memory import Memory

logger = logging.getLogger(__name__)

# Possible relationships between a new memory candidate and an existing memory.
RELATIONSHIP_DUPLICATE = "duplicate"
RELATIONSHIP_UPDATE = "update"
RELATIONSHIP_REFINEMENT = "refinement"
RELATIONSHIP_CONTRADICTION = "contradiction"
RELATIONSHIP_UNRELATED = "unrelated"

_UPDATE_LIKE_RELATIONSHIPS = {RELATIONSHIP_UPDATE, RELATIONSHIP_REFINEMENT, RELATIONSHIP_CONTRADICTION}


class BrainProtocol(Protocol):
    def generate_response(self, user_input: str, system_prompt: str, history: list[dict[str, str]] | None = None) -> str:
        ...


class EmbeddingProvider(Protocol):
    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        ...


@dataclass
class MemoryComparison:
    relationship: str  # duplicate | update | refinement | contradiction | unrelated
    existing_memory_id: str | None
    reason: str
    confidence: float

    @property
    def should_update_existing(self) -> bool:
        return self.relationship in _UPDATE_LIKE_RELATIONSHIPS


_COMPARATOR_SYSTEM_PROMPT = """You compare a new personal-memory statement about a user against one existing \
memory about the same user, and classify their relationship.

Relationships:
- "duplicate": they express the same information, just worded differently.
- "update": the new statement changes/supersedes the existing one (e.g. a habit's frequency changed).
- "refinement": the new statement adds detail/precision to the existing one without contradicting it.
- "contradiction": they directly conflict and cannot both be true as stated.
- "unrelated": they are not meaningfully about the same underlying fact.

For "update", "refinement", or "contradiction", the existing memory should be updated with the new \
information rather than a duplicate memory being created.

Respond ONLY with valid JSON (no markdown, no extra text):
{"relationship": "duplicate|update|refinement|contradiction|unrelated", "reason": "one short sentence", \
"confidence": 0.0-1.0}"""


class MemoryComparator:
    """Detects semantic duplicates and updates/conflicts against existing memories.

    Three tiers, cheapest first, to avoid an expensive LLM call against every
    existing memory:
      1. Cheap lexical/category candidate retrieval (no API calls).
      2. Semantic similarity via embeddings, if an embedding client is
         available (bounded to the small candidate set from step 1).
      3. AI comparison, only for the single most ambiguous candidate.
    """

    def __init__(
        self,
        *,
        brain: BrainProtocol | None = None,
        embedding_client: EmbeddingProvider | None = None,
        candidate_pool_size: int = 25,
        duplicate_threshold: float = 0.9,
        semantic_threshold: float = 0.5,
        lexical_threshold: float = 0.35,
    ) -> None:
        self.brain = brain
        self.embedding_client = embedding_client
        self.candidate_pool_size = candidate_pool_size
        self.duplicate_threshold = duplicate_threshold
        self.semantic_threshold = semantic_threshold
        self.lexical_threshold = lexical_threshold
        # text -> embedding, so each memory is embedded once, not on every comparison
        self._embed_cache: dict[str, list[float]] = {}

    def compare(self, new_content: str, category: str | None, existing_memories: list[Memory]) -> MemoryComparison:
        text = (new_content or "").strip()
        if not text or not existing_memories:
            return MemoryComparison(RELATIONSHIP_UNRELATED, None, "No existing memories to compare against.", 1.0)

        lexical_ranked = self._cheap_candidates(text, category, existing_memories)
        if not lexical_ranked:
            return MemoryComparison(RELATIONSHIP_UNRELATED, None, "No lexically related existing memories.", 1.0)

        if self.embedding_client is not None:
            ranked = self._rank_by_semantic_similarity(text, lexical_ranked)
        else:
            ranked = lexical_ranked

        top_memory, top_score = ranked[0]

        threshold = self.semantic_threshold if self.embedding_client is not None else self.lexical_threshold
        if top_score < threshold:
            return MemoryComparison(RELATIONSHIP_UNRELATED, None, "No sufficiently similar existing memory.", 1.0 - top_score)

        # Text/embedding similarity alone cannot reliably distinguish a true
        # duplicate from an update ("...3 times a week" vs "...5 times a
        # week" is near-identical text but a real factual change) -- so once
        # something clears the relevance bar, let the AI adjudicate exactly
        # what the relationship is, rather than auto-declaring "duplicate"
        # from a raw score. This is still bounded to a single AI call for the
        # one best candidate, never one call per existing memory.
        if self.brain is not None:
            return self._ai_compare(text, top_memory)

        # No AI available: fall back to a similarity-only heuristic, which
        # can only safely recognize very-high-similarity as a duplicate.
        if top_score >= self.duplicate_threshold:
            return MemoryComparison(RELATIONSHIP_DUPLICATE, top_memory.memory_id, "Near-identical to an existing memory.", top_score)

        return MemoryComparison(
            RELATIONSHIP_UNRELATED,
            None,
            "Ambiguous similarity but no AI available to resolve; treated as unrelated to avoid an unsafe merge.",
            top_score,
        )

    def _cheap_candidates(self, text: str, category: str | None, existing_memories: list[Memory]) -> list[tuple[Memory, float]]:
        # Every memory is a candidate (the same fact is often filed under a different category, e.g.
        # "studies X" as both identity and fact); same-category ones just sort first on a tie.
        ranked: list[tuple[Memory, float, float]] = []
        for memory in existing_memories:
            ratio = difflib.SequenceMatcher(None, text.lower(), memory.content.lower()).ratio()
            bonus = 0.05 if category is not None and memory.category == category else 0.0
            ranked.append((memory, ratio, ratio + bonus))

        if self.embedding_client is None:
            # Lexical-only mode has no semantic backstop, so stay strict about what counts as related.
            ranked = [item for item in ranked if item[1] >= self.lexical_threshold]
        # With embeddings, keep candidates that share no words too: paraphrases and contradictions
        # ("origin is Ukraine" vs "originally Turkish") are exactly the ones text similarity misses.
        ranked.sort(key=lambda item: item[2], reverse=True)
        return [(memory, ratio) for memory, ratio, _ in ranked[: self.candidate_pool_size]]

    def _rank_by_semantic_similarity(self, text: str, candidates: list[tuple[Memory, float]]) -> list[tuple[Memory, float]]:
        wanted = [text] + [memory.content for memory, _ in candidates]
        missing = [t for t in dict.fromkeys(wanted) if t not in self._embed_cache]
        if missing:
            try:
                embeddings = self.embedding_client.embed(missing)
            except Exception:
                logger.exception("Embedding call failed during memory comparison; falling back to lexical ranking")
                return candidates
            if len(embeddings) != len(missing):
                return candidates
            self._embed_cache.update(zip(missing, embeddings))
            if len(self._embed_cache) > 2000:  # keep the cache bounded
                self._embed_cache = dict(list(self._embed_cache.items())[-1000:])

        query_embedding = self._embed_cache[text]
        ranked = [
            (memory, _cosine_similarity(query_embedding, self._embed_cache[memory.content]))
            for memory, _lexical_score in candidates
        ]
        ranked.sort(key=lambda pair: pair[1], reverse=True)
        return ranked

    def _ai_compare(self, new_content: str, existing_memory: Memory) -> MemoryComparison:
        prompt = f'New statement: "{new_content}"\nExisting memory: "{existing_memory.content}"\n\nClassify their relationship and respond with the JSON object only.'
        try:
            response_text = self.brain.generate_response(
                user_input=prompt,
                system_prompt=_COMPARATOR_SYSTEM_PROMPT,
                history=None,
            )
            payload = _parse_json_response(response_text)
            relationship = str(payload.get("relationship", RELATIONSHIP_UNRELATED)).strip().lower()
            if relationship not in {
                RELATIONSHIP_DUPLICATE,
                RELATIONSHIP_UPDATE,
                RELATIONSHIP_REFINEMENT,
                RELATIONSHIP_CONTRADICTION,
                RELATIONSHIP_UNRELATED,
            }:
                relationship = RELATIONSHIP_UNRELATED
            reason = str(payload.get("reason", "")).strip()[:280]
            confidence = _clamp(payload.get("confidence", 0.5))
            existing_id = existing_memory.memory_id if relationship != RELATIONSHIP_UNRELATED else None
            return MemoryComparison(relationship, existing_id, reason, confidence)
        except Exception:
            logger.exception("MemoryComparator AI comparison failed; treating as unrelated")
            return MemoryComparison(RELATIONSHIP_UNRELATED, None, "AI comparison failed; treated conservatively as unrelated.", 0.0)


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


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
