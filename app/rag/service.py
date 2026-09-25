from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from app.memory.memory import Memory
from app.rag.context_builder import ContextBuilder
from app.rag.decision import RetrievalDecision
from app.rag.index import VectorIndex
from app.rag.indexer import Indexer
from app.rag.loaders import ConversationDocumentLoader, MemoryDocumentLoader, ObsidianDocumentLoader
from app.rag.retriever import RetrievedDocument, Retriever

logger = logging.getLogger(__name__)


@dataclass
class RagStatus:
    enabled: bool
    total_chunks: int
    total_documents: int
    memory_documents: int
    obsidian_documents: int
    conversation_documents: int
    embedding_model: str
    index_path: str
    top_k: int
    similarity_threshold: float


class RAGService:
    """Facade used by MikiCore/CLI for everything RAG-related.

    Composes the Indexer, Retriever, retrieval-decision heuristic, context
    builder, and the three source loaders. This is the only RAG object
    MikiCore is aware of (via a small local Protocol), keeping the Retriever
    itself decoupled from MikiCore and the Brain.
    """

    def __init__(
        self,
        *,
        indexer: Indexer,
        retriever: Retriever,
        decision: RetrievalDecision,
        context_builder: ContextBuilder,
        memory_loader: MemoryDocumentLoader,
        conversation_loader: ConversationDocumentLoader,
        index: VectorIndex,
        embedding_model: str,
        index_path: str | Path,
        top_k: int,
        similarity_threshold: float,
        obsidian_loader: ObsidianDocumentLoader | None = None,
        enabled: bool = True,
    ) -> None:
        self.indexer = indexer
        self.retriever = retriever
        self.decision = decision
        self.context_builder = context_builder
        self.memory_loader = memory_loader
        self.obsidian_loader = obsidian_loader
        self.conversation_loader = conversation_loader
        self.index = index
        self.embedding_model = embedding_model
        self.index_path = index_path
        self.top_k = top_k
        self.similarity_threshold = similarity_threshold
        self.enabled = enabled

    def status(self) -> RagStatus:
        memory_docs = len(self.index.document_ids_for_source("memory"))
        obsidian_docs = len(self.index.document_ids_for_source("obsidian"))
        conversation_docs = len(self.index.document_ids_for_source("conversation"))
        return RagStatus(
            enabled=self.enabled,
            total_chunks=self.index.count(),
            total_documents=memory_docs + obsidian_docs + conversation_docs,
            memory_documents=memory_docs,
            obsidian_documents=obsidian_docs,
            conversation_documents=conversation_docs,
            embedding_model=self.embedding_model,
            index_path=str(self.index_path),
            top_k=self.top_k,
            similarity_threshold=self.similarity_threshold,
        )

    def rebuild(self) -> RagStatus:
        logger.info("Rebuilding RAG index from source data")
        self.indexer.sync_source("memory", self.memory_loader.load())

        obsidian_documents = self.obsidian_loader.load() if self.obsidian_loader is not None else []
        self.indexer.sync_source("obsidian", obsidian_documents)

        self.indexer.sync_source("conversation", self.conversation_loader.load())
        return self.status()

    def retrieve_context_for_query(self, query: str) -> tuple[str | None, list[RetrievedDocument]]:
        if not self.enabled:
            return None, []
        if not self.decision.should_retrieve(query):
            return None, []

        retrieved = self.retriever.retrieve(query)
        if not retrieved:
            return None, []

        return self.context_builder.build(retrieved), retrieved

    def index_memory(self, memory: Memory) -> None:
        if not self.enabled:
            return
        document = self.memory_loader.to_document(memory)
        self.indexer.index_documents([document])

    def remove_memory(self, memory_id: str) -> None:
        if not self.enabled:
            return
        self.indexer.remove_document(f"memory:{memory_id}")
