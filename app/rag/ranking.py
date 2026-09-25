from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from app.rag.document import RetrievedDocument

_DEFAULT_SOURCE_MULTIPLIERS = {"memory": 1.0, "obsidian": 0.95, "conversation": 0.85}

# Keys checked in priority order to find "how recently was this true/confirmed".
# Different sources populate different keys (see loaders.py / chunking.py).
_RECENCY_METADATA_KEYS = ("last_confirmed_at", "updated_at", "source_updated_at", "ended_at", "started_at", "created_at")


@dataclass
class RankingWeights:
    """Weights for blending raw semantic similarity with memory metadata.

    Kept as a simple weighted sum on purpose (per spec: "do not overcomplicate
    the ranking formula"). Weights don't need to sum to 1 -- they're relative.
    """

    similarity: float = 0.55
    importance: float = 0.2
    confidence: float = 0.1
    recency: float = 0.1
    stability: float = 0.05
    recency_half_life_days: float = 180.0
    source_multipliers: dict[str, float] = field(default_factory=lambda: dict(_DEFAULT_SOURCE_MULTIPLIERS))


def rank_retrieved_documents(
    retrieved: list[RetrievedDocument],
    weights: RankingWeights | None = None,
    *,
    now: datetime | None = None,
) -> list[RetrievedDocument]:
    """Re-ranks retrieved chunks using similarity + available memory metadata.

    Sets `.final_score` on each item and returns a new list sorted by it,
    descending. `.score` (raw similarity) is left untouched, so existing
    provenance/debugging expectations still hold.
    """
    weights = weights or RankingWeights()
    now = now or datetime.utcnow()

    scored = []
    for item in retrieved:
        item.final_score = _blended_score(item, weights, now)
        scored.append(item)

    scored.sort(key=lambda entry: entry.final_score if entry.final_score is not None else entry.score, reverse=True)
    return scored


def _blended_score(item: RetrievedDocument, weights: RankingWeights, now: datetime) -> float:
    metadata = item.metadata or {}
    importance = _neutral_float(metadata.get("importance"))
    confidence = _neutral_float(metadata.get("confidence"))
    stability = _neutral_float(metadata.get("stability"))
    recency = _recency_score(metadata, weights.recency_half_life_days, now)

    base = (
        weights.similarity * item.score
        + weights.importance * importance
        + weights.confidence * confidence
        + weights.recency * recency
        + weights.stability * stability
    )
    multiplier = weights.source_multipliers.get(item.source, 1.0)
    return base * multiplier


def _neutral_float(value, *, default: float = 0.5) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return default


def _recency_score(metadata: dict, half_life_days: float, now: datetime) -> float:
    timestamp = None
    for key in _RECENCY_METADATA_KEYS:
        value = metadata.get(key)
        if value:
            timestamp = _parse_timestamp(value)
            if timestamp is not None:
                break

    if timestamp is None:
        return 0.5  # neutral: unknown recency shouldn't penalize or boost

    age_days = max(0.0, (now - timestamp).total_seconds() / 86400.0)
    if half_life_days <= 0:
        return 1.0
    return 0.5 ** (age_days / half_life_days)


def _parse_timestamp(value) -> datetime | None:
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
