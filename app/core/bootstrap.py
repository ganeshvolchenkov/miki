"""Builds Miki's runtime (brain, memory, RAG, tools, core) in one place.

The dashboard and the headless phone service both need exactly the same object graph; keeping the wiring
here means they can never drift apart.
"""

from __future__ import annotations

from dataclasses import dataclass

from openai import OpenAI

from app.brain.embeddings import OpenAIEmbeddingClient
from app.brain.openai_client import OpenAIClient
from app.core.assistant import MikiCore
from app.core.config import Settings
from app.core.rag_wiring import build_rag_service
from app.core.storage import build_memory_manager, build_memory_store
from app.core.tool_wiring import build_tool_runner
from app.memory.conversation import JSONConversationStore


@dataclass
class MikiRuntime:
    settings: Settings
    core: MikiCore
    brain: OpenAIClient
    openai_client: OpenAI


def build_runtime(settings: Settings | None = None, *, saved_model: str | None = None) -> MikiRuntime:
    settings = settings or Settings.from_env()
    client = OpenAI(api_key=settings.openai_api_key)
    brain = OpenAIClient(api_key=settings.openai_api_key, model=settings.model, client=client)

    conversation_store = JSONConversationStore("data/conversations")
    memory_store = build_memory_store(settings, json_storage_dir="data/memories")
    embedding_client = OpenAIEmbeddingClient(client=client, model=settings.embedding_model)
    memory_manager = build_memory_manager(settings, memory_store=memory_store, brain=brain, embedding_client=embedding_client)
    rag_service = build_rag_service(
        settings,
        memory_store=memory_store,
        shared_openai_client=client,
        conversations_dir="data/conversations",
        embedding_client=embedding_client,
    )
    tool_runner = build_tool_runner(settings, brain)
    core = MikiCore(
        brain=brain,
        conversation_store=conversation_store,
        memory_manager=memory_manager,
        rag_service=rag_service,
        tool_runner=tool_runner,
    )

    # Apply a saved /model choice only after the memory manager is built, so the memory pipeline keeps
    # sharing this brain (and therefore follows later /model switches).
    if saved_model:
        brain.model = saved_model
    return MikiRuntime(settings=settings, core=core, brain=brain, openai_client=client)
