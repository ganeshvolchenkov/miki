from __future__ import annotations

import logging
import os
from typing import Protocol

from app.core.memory_manager import MemoryManager
from app.core.tool_runner import PendingToolAction, ToolConversationRunner
from app.memory.conversation import ConversationStore

logger = logging.getLogger(__name__)


def _history_limit() -> int:
    """How many recent messages to send the model each turn (MIKI_HISTORY_MESSAGES, default 30).

    A session is never closed, so without a cap every turn re-sent -- and paid for -- the entire
    conversation so far. Older turns stay on disk and remain searchable through RAG."""
    try:
        return max(2, int(os.getenv("MIKI_HISTORY_MESSAGES", "30")))
    except ValueError:
        return 30


class BrainProtocol(Protocol):
    def generate_response(self, user_input: str, system_prompt: str, history: list[dict[str, str]] | None = None) -> str:
        ...


class RAGServiceProtocol(Protocol):
    """What MikiCore needs from the RAG layer.

    MikiCore only depends on this protocol, never on concrete RAG classes --
    the Retriever/Indexer stay decoupled from MikiCore and the Brain.
    """

    def retrieve_context_for_query(self, query: str) -> tuple[str | None, list]:
        ...

    def index_memory(self, memory) -> None:
        ...

    def remove_memory(self, memory_id: str) -> None:
        ...


class MikiCore:
    """Central orchestration layer for Miki."""

    def __init__(
        self,
        brain: BrainProtocol,
        conversation_store: ConversationStore,
        memory_manager: MemoryManager | None = None,
        rag_service: RAGServiceProtocol | None = None,
        tool_runner: ToolConversationRunner | None = None,
    ) -> None:
        self.brain = brain
        self.conversation_store = conversation_store

        # Inject brain into memory manager if it exists
        if memory_manager is not None and memory_manager.brain is None:
            memory_manager.brain = brain

        self.memory_manager = memory_manager
        self.rag_service = rag_service
        self.tool_runner = tool_runner
        # A write/destructive tool call that previewed its effect and is
        # waiting for the user's next message to confirm or decline it. See
        # app/core/tool_runner.py::PendingToolAction.
        self.pending_tool_action: PendingToolAction | None = None
        # Populated whenever a memory is created/updated through this core, so
        # callers (e.g. the CLI) can report specifics -- category, storage
        # location, RAG status -- without re-deriving them. Cleared to None
        # when the most recent operation did not touch a memory.
        self.last_created_memory = None
        self.last_memory_rag_indexed: bool = False
        self.last_memory_candidate = None
        # Every memory created/updated by the most recent message (may be several).
        self.last_memories: list = []
        self.system_prompt = (
            "You are Miki, a personal AI assistant with a genuine passion for learning about your user. "
            "You are helpful, concise, and natural in conversation. "
            "You do not claim to know things you do not know and do not claim to have completed actions you have not performed. "
            "You have persistent long-term memory and use every conversation to understand your user better. "
            "You will respond to messages in a serving manner and always be super respectful like a 7-star hotel reception."
            "Talk like an employee would to their boss but in a playful manner. "
            "End questions in a constructive way, always build more! "
            "\n"
            "You are given a section titled WHAT MIKI REMEMBERS ABOUT THE USER: it is your real long-term memory. "
            "Use it to personalise answers, but never recite it unprompted. "
            "\n"
            "IMPORTANT - Memory Building: You are EAGER to learn and remember facts about the user. "
            "When the user mentions preferences, habits, interests, or personal facts, actively acknowledge them and show interest. "
            "Ask follow-up questions naturally to deepen your understanding. "
            "Be curious and genuinely interested—the more you learn, the better you can serve. "
            "Natural conversation often reveals memory-worthy details—use these moments to build your understanding. "
        )

    def _retrieve_context(self, user_input: str) -> str | None:
        if self.rag_service is None:
            return None
        try:
            context_block, retrieved = self.rag_service.retrieve_context_for_query(user_input)
        except Exception:
            logger.exception("RAG retrieval failed; continuing without retrieved context")
            return None
        if context_block:
            logger.info("RAG retrieved %d document(s) for this query", len(retrieved))
        return context_block

    def _index_memory_safely(self, memory) -> bool:
        """Indexes ``memory`` into RAG, returning whether it actually succeeded."""
        if self.rag_service is None or memory is None:
            return False
        try:
            self.rag_service.index_memory(memory)
            return True
        except Exception:
            logger.exception("Failed to index memory %s into RAG", getattr(memory, "memory_id", "?"))
            return False

    def _record_memory_event(self, memory) -> None:
        """Tracks the most recent memory create/update so callers can report
        specifics (category, storage location, RAG status) right after the
        call, instead of just a bare boolean."""
        self.last_created_memory = memory
        self.last_memory_rag_indexed = self._index_memory_safely(memory)

    def _remove_memory_safely(self, memory_id: str) -> None:
        if self.rag_service is None:
            return
        try:
            self.rag_service.remove_memory(memory_id)
        except Exception:
            logger.exception("Failed to remove memory %s from RAG index", memory_id)

    def process_user_input(self, user_input: str) -> tuple[str, bool]:
        """Process user input and return (response, memory_was_created)."""
        if not user_input or not user_input.strip():
            return "", False

        history = self.conversation_store.read_messages()[-_history_limit():]
        logger.info("Processing user input for Miki Core")

        effective_system_prompt = self.system_prompt
        # Always-on long-term memory (graph-aware, no API call), then any extra
        # semantic retrieval (documents, notes, past conversations).
        memory_block = self.memory_manager.build_recall_block(user_input) if self.memory_manager is not None else None
        if memory_block:
            effective_system_prompt = f"{effective_system_prompt}\n\n{memory_block}"
        context_block = self._retrieve_context(user_input)
        if context_block:
            effective_system_prompt = f"{effective_system_prompt}\n\n{context_block}"

        has_tool_call = False
        if self.tool_runner is not None:
            pending = self.pending_tool_action
            self.pending_tool_action = None
            result = self.tool_runner.run(
                user_input=user_input,
                system_prompt=effective_system_prompt,
                history=history,
                pending_action=pending,
            )
            response = result.text
            self.pending_tool_action = result.pending_action
            has_tool_call = bool(result.has_tool_calls)
        else:
            response = self.brain.generate_response(
                user_input=user_input,
                system_prompt=effective_system_prompt,
                history=history,
            )

        self.conversation_store.append_turn(user_input, response)

        memory_created = False
        self.last_created_memory = None
        self.last_memory_rag_indexed = False
        self.last_memory_candidate = None
        self.last_memories = []
        if self.memory_manager is not None:
            memory = self.memory_manager.extract_and_store_from_interaction(user_input, response, has_tool_call=has_tool_call)
            memory_created = memory is not None
            if memory_created:
                logger.info(f"Memory stored: {memory.content} (category: {memory.category})")
                self._record_memory_event(memory)
                self.last_memories = list(getattr(self.memory_manager, "last_memories", None) or [memory])
                for extra in self.last_memories:
                    if extra.memory_id != memory.memory_id:
                        self._index_memory_safely(extra)
            else:
                self.last_memory_candidate = self.memory_manager.last_candidate

        return response, memory_created

    def create_memory(self, content: str, *, category: str = "general", memory_type: str = "fact", confidence: float = 0.5, source: str = "manual"):
        if self.memory_manager is None:
            raise ValueError("Memory manager is not configured.")
        memory = self.memory_manager.create_memory(content, category=category, memory_type=memory_type, confidence=confidence, source=source)
        self._record_memory_event(memory)
        return memory

    def remember(self, text: str) -> list:
        """Explicitly store what the user asked to remember (may yield several memories)."""
        if self.memory_manager is None:
            raise ValueError("Memory manager is not configured.")
        memories = self.memory_manager.remember(text)
        self.last_memories = list(memories)
        for memory in memories:
            self._record_memory_event(memory)
        return memories

    def list_memories(self, *, active_only: bool = True):
        if self.memory_manager is None:
            return []
        return self.memory_manager.list_memories(active_only=active_only)

    def get_memory(self, memory_id: str):
        if self.memory_manager is None:
            return None
        return self.memory_manager.get_memory(memory_id)

    def update_memory(self, memory_id: str, **updates):
        if self.memory_manager is None:
            raise ValueError("Memory manager is not configured.")
        memory = self.memory_manager.update_memory(memory_id, **updates)
        if memory is not None:
            self._record_memory_event(memory)
        return memory

    def delete_memory(self, memory_id: str):
        if self.memory_manager is None:
            raise ValueError("Memory manager is not configured.")
        memory = self.memory_manager.delete_memory(memory_id)
        if memory is not None:
            self._remove_memory_safely(memory_id)
        return memory

    def delete_memory_by_text(self, lookup: str):
        if self.memory_manager is None:
            return None
        memory = self.memory_manager.delete_memory_by_text(lookup)
        if memory is not None:
            self._remove_memory_safely(memory.memory_id)
        return memory

    def list_memory_candidates(self, *, status: str | None = "pending"):
        if self.memory_manager is None:
            return []
        return self.memory_manager.list_candidates(status=status)

    def approve_memory_candidate(self, candidate_id: str):
        if self.memory_manager is None:
            return None
        memory = self.memory_manager.approve_candidate(candidate_id)
        if memory is not None:
            self._record_memory_event(memory)
        return memory

    def reject_memory_candidate(self, candidate_id: str) -> bool:
        if self.memory_manager is None:
            return False
        return self.memory_manager.reject_candidate(candidate_id)

    def list_tools(self):
        if self.tool_runner is None:
            return []
        return self.tool_runner.tool_manager.registry.list_tools()

    def get_tool(self, name: str):
        if self.tool_runner is None:
            return None
        return self.tool_runner.tool_manager.registry.get(name)

    def call_tool(self, tool_name: str, operation: str, arguments: dict | None = None):
        """Executes a tool operation directly, bypassing the Brain entirely.

        For callers (the GUI's weather panel, CLI debug commands) that need
        structured tool data without spending any LLM tokens or round-trips
        -- natural-language questions still go through process_user_input,
        which lets the Brain decide when a tool call is actually needed.
        """
        if self.tool_runner is None:
            return None
        return self.tool_runner.tool_manager.execute(tool_name, operation, arguments or {})
