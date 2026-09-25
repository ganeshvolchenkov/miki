from __future__ import annotations

import re
from typing import Protocol

from app.rag.document import DocumentChunk, KnowledgeDocument, hash_text


class Chunker(Protocol):
    def chunk(self, document: KnowledgeDocument) -> list[DocumentChunk]:
        ...


class SimpleChunker:
    """Paragraph-aware chunker.

    Short content (the common case for a memory such as "User goes to the gym
    4 times per week.") is kept as a single chunk. Longer documents (Obsidian
    notes, conversation transcripts) are split on paragraph boundaries up to
    ``max_chars``, with a hard fallback split (with overlap) for any single
    paragraph that is itself too long.
    """

    def __init__(self, max_chars: int = 800, overlap: int = 100) -> None:
        if max_chars <= 0:
            raise ValueError("max_chars must be positive")
        if overlap < 0 or overlap >= max_chars:
            overlap = 0
        self.max_chars = max_chars
        self.overlap = overlap

    def chunk(self, document: KnowledgeDocument) -> list[DocumentChunk]:
        text = document.content.strip()
        if not text:
            return []

        if len(text) <= self.max_chars:
            pieces = [text]
        else:
            pieces = self._split_text(text)

        return [self._make_chunk(document, piece, index) for index, piece in enumerate(pieces)]

    def _split_text(self, text: str) -> list[str]:
        paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
        if not paragraphs:
            paragraphs = [text]

        chunks: list[str] = []
        current = ""
        for paragraph in paragraphs:
            candidate = f"{current}\n\n{paragraph}" if current else paragraph
            if len(candidate) <= self.max_chars:
                current = candidate
                continue

            if current:
                chunks.append(current)
                current = ""

            if len(paragraph) <= self.max_chars:
                current = paragraph
            else:
                chunks.extend(self._hard_split(paragraph))

        if current:
            chunks.append(current)

        return chunks

    def _hard_split(self, text: str) -> list[str]:
        pieces = []
        start = 0
        step = self.max_chars - self.overlap if self.overlap else self.max_chars
        while start < len(text):
            end = start + self.max_chars
            pieces.append(text[start:end])
            if end >= len(text):
                break
            start += step
        return pieces

    def _make_chunk(self, document: KnowledgeDocument, content: str, index: int) -> DocumentChunk:
        metadata = dict(document.metadata)
        metadata["chunk_index"] = index
        if document.updated_at is not None and "source_updated_at" not in metadata:
            metadata["source_updated_at"] = document.updated_at
        return DocumentChunk(
            chunk_id=f"{document.id}::chunk::{index}",
            document_id=document.id,
            content=content,
            source=document.source,
            metadata=metadata,
            chunk_index=index,
            content_hash=hash_text(content),
        )
