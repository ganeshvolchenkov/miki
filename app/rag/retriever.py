from __future__ import annotations

import logging

from app.rag.document import RetrievedDocument
from app.rag.embeddings import EmbeddingProvider
from app.rag.index import VectorIndex
from app.rag.ranking import RankingWeights, rank_retrieved_documents

logger = logging.getLogger(__name__)

__all__ = ["RetrievedDocument", "Retriever"]


class Retriever:
    """Semantic retrieval over the local vector index.

    Deliberately separate from MikiCore and the Brain: it only knows about
    embeddings and the ``VectorIndex``/``KnowledgeDocument`` abstractions.
    """

    def __init__(
        self,
        *,
        index: VectorIndex,
        embedding_client: EmbeddingProvider,
        top_k: int = 5,
        similarity_threshold: float = 0.2,
        ranking_weights: RankingWeights | None = None,
        overfetch_factor: int = 4,
    ) -> None:
        self.index = index
        self.embedding_client = embedding_client
        self.top_k = top_k
        self.similarity_threshold = similarity_threshold
        self.ranking_weights = ranking_weights or RankingWeights()
        self.overfetch_factor = max(1, overfetch_factor)

    def retrieve(
        self,
        query: str,
        *,
        top_k: int | None = None,
        similarity_threshold: float | None = None,
    ) -> list[RetrievedDocument]:
        text = (query or "").strip()
        if not text:
            return []

        k = top_k if top_k is not None else self.top_k
        threshold = similarity_threshold if similarity_threshold is not None else self.similarity_threshold

        embeddings = self.embedding_client.embed([text])
        if not embeddings:
            return []
        query_embedding = embeddings[0]

        # Overfetch a larger candidate pool by raw similarity so that
        # important-but-slightly-less-similar memories aren't cut off before
        # metadata-aware re-ranking even gets to see them.
        pool_size = max(k * self.overfetch_factor, k)
        scored = self.index.search(query_embedding, top_k=pool_size)
        candidates = [
            RetrievedDocument(
                document_id=item.record.document_id,
                chunk_id=item.record.chunk_id,
                content=item.record.content,
                source=item.record.source,
                metadata=dict(item.record.metadata),
                score=item.score,
            )
            for item in scored
            if item.score >= threshold
        ]

        ranked = rank_retrieved_documents(candidates, self.ranking_weights)
        results = ranked[:k]

        logger.info(
            "RAG retrieval returned %d/%d candidate(s) above threshold=%.2f",
            len(results),
            len(scored),
            threshold,
        )
        return results
