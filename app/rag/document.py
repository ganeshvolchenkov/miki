from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any


def hash_text(text: str) -> str:
    """Stable content hash used to detect whether a chunk needs re-embedding."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class KnowledgeDocument:
    """Common representation of a piece of Miki knowledge, regardless of source.

    The Retriever and Indexer only ever deal with ``KnowledgeDocument`` (and
    the chunks derived from it) -- they never touch Memory, Obsidian, or
    conversation storage directly.
    """

    id: str
    content: str
    source: str  # "memory" | "obsidian" | "conversation"
    metadata: dict[str, Any] = field(default_factory=dict)
    updated_at: str | None = None


@dataclass(frozen=True)
class DocumentChunk:
    """A chunk of a ``KnowledgeDocument`` ready for embedding/indexing."""

    chunk_id: str
    document_id: str
    content: str
    source: str
    metadata: dict[str, Any]
    chunk_index: int
    content_hash: str


@dataclass
class RetrievedDocument:
    """A retrieved chunk with full provenance and its similarity score."""

    document_id: str
    chunk_id: str
    content: str
    source: str
    metadata: dict[str, Any]
    score: float
    # Set by app.rag.ranking after re-ranking with memory metadata (importance,
    # confidence, recency, stability, source). None until that step runs;
    # `.score` (raw cosine similarity) is never overwritten.
    final_score: float | None = None
