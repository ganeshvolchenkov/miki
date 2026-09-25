from __future__ import annotations

from pathlib import Path

from openai import OpenAI

from app.brain.embeddings import OpenAIEmbeddingClient
from app.core.config import Settings
from app.memory.memory import MemoryStore
from app.rag.chunking import SimpleChunker
from app.rag.context_builder import ContextBuilder
from app.rag.decision import DeterministicRetrievalDecision
from app.rag.index import LocalJSONVectorIndex
from app.rag.indexer import Indexer
from app.rag.loaders import ConversationDocumentLoader, MemoryDocumentLoader, ObsidianDocumentLoader
from app.rag.retriever import Retriever
from app.rag.service import RAGService


def build_rag_service(
    settings: Settings,
    *,
    memory_store: MemoryStore,
    shared_openai_client: OpenAI,
    conversations_dir: str | Path = "data/conversations",
    embedding_client: OpenAIEmbeddingClient | None = None,
) -> RAGService | None:
    """Builds the RAG stack from Settings, or returns None if RAG is disabled.

    Returning None is the graceful-fallback switch: MikiCore treats a missing
    rag_service exactly like v0.4 behaved, with no RAG involved at all.

    ``embedding_client`` can be passed in to share the same embedding client
    used elsewhere (e.g. by memory-intelligence duplicate detection) instead
    of constructing a second one.
    """
    if not settings.rag_enabled:
        return None

    embedding_client = embedding_client or OpenAIEmbeddingClient(client=shared_openai_client, model=settings.embedding_model)
    index = LocalJSONVectorIndex(settings.rag_index_path)
    chunker = SimpleChunker(max_chars=settings.rag_chunk_size, overlap=settings.rag_chunk_overlap)
    indexer = Indexer(
        index=index,
        embedding_client=embedding_client,
        chunker=chunker,
        embedding_model=settings.embedding_model,
    )
    retriever = Retriever(
        index=index,
        embedding_client=embedding_client,
        top_k=settings.rag_top_k,
        similarity_threshold=settings.rag_similarity_threshold,
    )

    obsidian_loader = None
    if settings.obsidian_vault_path is not None:
        obsidian_loader = ObsidianDocumentLoader(settings.obsidian_vault_path / "Miki")

    return RAGService(
        indexer=indexer,
        retriever=retriever,
        decision=DeterministicRetrievalDecision(),
        context_builder=ContextBuilder(),
        memory_loader=MemoryDocumentLoader(memory_store),
        obsidian_loader=obsidian_loader,
        conversation_loader=ConversationDocumentLoader(conversations_dir),
        index=index,
        embedding_model=settings.embedding_model,
        index_path=settings.rag_index_path,
        top_k=settings.rag_top_k,
        similarity_threshold=settings.rag_similarity_threshold,
        enabled=True,
    )
