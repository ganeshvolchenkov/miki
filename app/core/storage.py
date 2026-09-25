from __future__ import annotations

from pathlib import Path
from typing import Protocol

from app.core.config import Settings
from app.core.memory_candidate import MemoryCandidateStore
from app.core.memory_comparator import MemoryComparator
from app.core.memory_evaluator import MemoryEvaluator
from app.core.memory_extractor import MemoryExtractor
from app.core.memory_feedback import MemoryFeedbackDataset
from app.core.memory_manager import MemoryManager
from app.core.profile import ProfileStore
from app.memory.memory import JSONMemoryStore, MemoryStore
from app.memory.obsidian import ObsidianMemoryStore


class BrainProtocol(Protocol):
    def generate_response(self, user_input: str, system_prompt: str, history: list[dict[str, str]] | None = None) -> str:
        ...


class EmbeddingProvider(Protocol):
    def embed(self, texts):
        ...


def build_memory_store(settings: Settings, *, json_storage_dir: str | Path | None = None) -> MemoryStore:
    if settings.obsidian_vault_path is not None:
        return ObsidianMemoryStore(settings.obsidian_vault_path)
    return JSONMemoryStore(json_storage_dir)


def build_memory_manager(
    settings: Settings,
    *,
    memory_store: MemoryStore,
    brain: BrainProtocol | None = None,
    embedding_client: EmbeddingProvider | None = None,
    candidate_storage_dir: str | Path = "data/memory_candidates",
    feedback_storage_dir: str | Path = "data/memory_training",
    profile_storage_path: str | Path = "data/profile.json",
) -> MemoryManager:
    """Builds a MemoryManager, wiring in the Memory Intelligence pipeline
    (Evaluator/Extractor/Comparator/candidates/feedback) when both a brain
    and ``settings.memory_intelligence_enabled`` are available.

    When intelligence is disabled (or no brain is configured), the returned
    MemoryManager falls back to the exact legacy v0.4/v0.5 behavior -- this
    is the same graceful-fallback pattern used for RAG.
    """
    if brain is None or not settings.memory_intelligence_enabled:
        return MemoryManager(memory_store, brain)

    memory_brain = brain
    if hasattr(brain, "client") and hasattr(brain, "model") and settings.memory_model != getattr(brain, "model", None):
        from app.brain.openai_client import OpenAIClient
        memory_brain = OpenAIClient(
            api_key=settings.openai_api_key,
            model=settings.memory_model,
            client=getattr(brain, "client", None),
        )

    return MemoryManager(
        memory_store,
        brain,
        evaluator=MemoryEvaluator(memory_brain),
        extractor=MemoryExtractor(memory_brain),
        comparator=MemoryComparator(brain=memory_brain, embedding_client=embedding_client),
        candidate_store=MemoryCandidateStore(candidate_storage_dir),
        feedback_dataset=MemoryFeedbackDataset(feedback_storage_dir),
        profile_store=ProfileStore(profile_storage_path),
        candidate_score_threshold=settings.memory_candidate_threshold,
        confident_score_threshold=settings.memory_confident_threshold,
    )
