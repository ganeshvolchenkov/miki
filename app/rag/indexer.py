from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

from app.rag.chunking import Chunker
from app.rag.document import DocumentChunk, KnowledgeDocument
from app.rag.embeddings import EmbeddingProvider
from app.rag.index import IndexRecord, VectorIndex

logger = logging.getLogger(__name__)


def _utc_now() -> str:
    return datetime.utcnow().isoformat(timespec="seconds")


@dataclass
class IndexSyncResult:
    added: int = 0
    updated: int = 0
    skipped: int = 0
    removed: int = 0


class Indexer:
    """Converts KnowledgeDocuments into embeddings and keeps the index in sync.

    Change detection is per-chunk: a chunk is only re-embedded if its content
    hash changed or the configured embedding model changed. Unchanged chunks
    are skipped entirely, avoiding unnecessary OpenAI calls.
    """

    def __init__(
        self,
        *,
        index: VectorIndex,
        embedding_client: EmbeddingProvider,
        chunker: Chunker,
        embedding_model: str,
    ) -> None:
        self.index = index
        self.embedding_client = embedding_client
        self.chunker = chunker
        self.embedding_model = embedding_model

    def index_documents(self, documents: Iterable[KnowledgeDocument]) -> IndexSyncResult:
        """Add/update documents in the index. Does not remove documents that
        used to exist in the index but are no longer present in ``documents``
        -- use ``sync_source`` for that."""
        added = updated = skipped = removed = 0

        for document in documents:
            chunks = self.chunker.chunk(document)
            existing_chunk_ids = self.index.chunk_ids_for_document(document.id)
            current_chunk_ids: set[str] = set()
            chunks_to_embed: list[DocumentChunk] = []

            for chunk in chunks:
                current_chunk_ids.add(chunk.chunk_id)
                existing = self.index.get_chunk(chunk.chunk_id)
                if (
                    existing is not None
                    and existing.content_hash == chunk.content_hash
                    and existing.embedding_model == self.embedding_model
                ):
                    skipped += 1
                    continue
                chunks_to_embed.append(chunk)

            if chunks_to_embed:
                embeddings = self.embedding_client.embed([chunk.content for chunk in chunks_to_embed])
                for chunk, embedding in zip(chunks_to_embed, embeddings):
                    is_update = self.index.get_chunk(chunk.chunk_id) is not None
                    record = IndexRecord(
                        chunk_id=chunk.chunk_id,
                        document_id=document.id,
                        source=document.source,
                        content=chunk.content,
                        embedding=embedding,
                        content_hash=chunk.content_hash,
                        embedding_model=self.embedding_model,
                        metadata=chunk.metadata,
                        updated_at=_utc_now(),
                    )
                    if is_update:
                        self.index.update(record)
                        updated += 1
                    else:
                        self.index.add(record)
                        added += 1

            for stale_chunk_id in existing_chunk_ids - current_chunk_ids:
                self.index.delete_chunk(stale_chunk_id)
                removed += 1

        self.index.save()
        return IndexSyncResult(added=added, updated=updated, skipped=skipped, removed=removed)

    def sync_source(self, source: str, documents: Iterable[KnowledgeDocument]) -> IndexSyncResult:
        """Full reconciliation for one source: add/update present documents and
        remove index entries for documents of that source that no longer
        exist (e.g. a deleted memory, a renamed/removed Obsidian note)."""
        documents = list(documents)
        result = self.index_documents(documents)

        seen_ids = {document.id for document in documents}
        stale_document_ids = self.index.document_ids_for_source(source) - seen_ids
        removed_docs = 0
        for document_id in stale_document_ids:
            removed_docs += self.index.delete_document(document_id)

        if stale_document_ids:
            self.index.save()

        return IndexSyncResult(
            added=result.added,
            updated=result.updated,
            skipped=result.skipped,
            removed=result.removed + removed_docs,
        )

    def remove_document(self, document_id: str) -> int:
        removed = self.index.delete_document(document_id)
        if removed:
            self.index.save()
        return removed
