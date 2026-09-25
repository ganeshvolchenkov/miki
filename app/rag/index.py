from __future__ import annotations

import json
import logging
import math
from array import array
from dataclasses import dataclass, field
from operator import mul
from pathlib import Path
from typing import Any, Protocol

logger = logging.getLogger(__name__)


def cosine_similarity(a, b) -> float:
    if not len(a) or not len(b) or len(a) != len(b):
        return 0.0
    dot = sum(map(mul, a, b))
    norm_a = math.sqrt(sum(map(mul, a, a)))
    norm_b = math.sqrt(sum(map(mul, b, b)))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


@dataclass
class IndexRecord:
    """One embedded chunk, with full provenance back to its source document."""

    chunk_id: str
    document_id: str
    source: str
    content: str
    embedding: Any  # stored as array('f'): ~6 KB per 1536-d vector instead of ~49 KB as a list of floats
    content_hash: str
    embedding_model: str
    metadata: dict[str, Any] = field(default_factory=dict)
    updated_at: str | None = None
    _norm: float = field(default=0.0, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.embedding, array):
            self.embedding = array("f", self.embedding)
        self._norm = math.sqrt(sum(map(mul, self.embedding, self.embedding)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "chunk_id": self.chunk_id,
            "document_id": self.document_id,
            "source": self.source,
            "content": self.content,
            "embedding": self.embedding.tolist(),
            "content_hash": self.content_hash,
            "embedding_model": self.embedding_model,
            "metadata": self.metadata,
            "updated_at": self.updated_at,
        }


@dataclass
class ScoredRecord:
    record: IndexRecord
    score: float


class VectorIndex(Protocol):
    """Minimal contract a local (or future hosted) vector backend must satisfy."""

    def add(self, record: IndexRecord) -> None: ...
    def update(self, record: IndexRecord) -> None: ...
    def get_chunk(self, chunk_id: str) -> IndexRecord | None: ...
    def delete_chunk(self, chunk_id: str) -> bool: ...
    def delete_document(self, document_id: str) -> int: ...
    def chunk_ids_for_document(self, document_id: str) -> set[str]: ...
    def document_ids_for_source(self, source: str) -> set[str]: ...
    def search(self, query_embedding: list[float], top_k: int) -> list[ScoredRecord]: ...
    def count(self) -> int: ...
    def count_by_source(self) -> dict[str, int]: ...
    def clear(self) -> None: ...
    def save(self) -> None: ...


class LocalJSONVectorIndex:
    """Local, file-backed vector index using brute-force cosine similarity.

    Appropriate for a personal knowledge base (hundreds to low thousands of
    chunks). The backend is fully swappable later -- any class implementing
    the ``VectorIndex`` protocol above can replace this one without touching
    the Indexer or Retriever.
    """

    def __init__(self, index_path: str | Path) -> None:
        self.index_path = Path(index_path)
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        self._records: dict[str, IndexRecord] = {}
        self._by_document: dict[str, set[str]] = {}
        self._load()

    def _load(self) -> None:
        if not self.index_path.exists():
            return
        try:
            raw = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.warning("Could not read RAG index at %s; starting fresh.", self.index_path)
            return

        for item in raw.get("records", []):
            try:
                record = IndexRecord(**item)
            except TypeError:
                continue
            self._put(record)

    def _put(self, record: IndexRecord) -> None:
        previous = self._records.get(record.chunk_id)
        if previous is not None and previous.document_id != record.document_id:
            self._by_document.get(previous.document_id, set()).discard(record.chunk_id)
        self._records[record.chunk_id] = record
        self._by_document.setdefault(record.document_id, set()).add(record.chunk_id)

    def _drop(self, chunk_id: str) -> None:
        record = self._records.pop(chunk_id, None)
        if record is not None:
            ids = self._by_document.get(record.document_id)
            if ids is not None:
                ids.discard(chunk_id)
                if not ids:
                    del self._by_document[record.document_id]

    def save(self) -> None:
        payload = {"records": [record.to_dict() for record in self._records.values()]}
        # Compact JSON (no indent): the pretty-printed form put every float on its own line.
        tmp_path = self.index_path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        tmp_path.replace(self.index_path)

    def add(self, record: IndexRecord) -> None:
        self._put(record)

    def update(self, record: IndexRecord) -> None:
        self._put(record)

    def get_chunk(self, chunk_id: str) -> IndexRecord | None:
        return self._records.get(chunk_id)

    def delete_chunk(self, chunk_id: str) -> bool:
        if chunk_id in self._records:
            self._drop(chunk_id)
            return True
        return False

    def delete_document(self, document_id: str) -> int:
        stale = list(self._by_document.get(document_id, ()))
        for chunk_id in stale:
            self._drop(chunk_id)
        return len(stale)

    def chunk_ids_for_document(self, document_id: str) -> set[str]:
        return set(self._by_document.get(document_id, ()))

    def document_ids_for_source(self, source: str) -> set[str]:
        return {record.document_id for record in self._records.values() if record.source == source}

    def search(self, query_embedding: list[float], top_k: int) -> list[ScoredRecord]:
        query = array("f", query_embedding)
        query_norm = math.sqrt(sum(map(mul, query, query)))
        if not len(query) or query_norm == 0.0:
            return []
        scored = []
        for record in self._records.values():
            if len(record.embedding) != len(query) or record._norm == 0.0:
                score = 0.0
            else:
                score = sum(map(mul, query, record.embedding)) / (query_norm * record._norm)
            scored.append(ScoredRecord(record=record, score=score))
        scored.sort(key=lambda item: item.score, reverse=True)
        return scored[:top_k]

    def count(self) -> int:
        return len(self._records)

    def count_by_source(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for record in self._records.values():
            counts[record.source] = counts.get(record.source, 0) + 1
        return counts

    def clear(self) -> None:
        self._records = {}
        self._by_document = {}
