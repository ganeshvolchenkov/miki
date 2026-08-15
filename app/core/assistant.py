from __future__ import annotations

import logging
from typing import Protocol

from app.memory.conversation import ConversationStore

logger = logging.getLogger(__name__)


class BrainProtocol(Protocol):
    def generate_response(self, user_input: str, system_prompt: str, history: list[dict[str, str]] | None = None) -> str:
        ...


class MikiCore:
    """Central orchestration layer for Miki."""

    def __init__(self, brain: BrainProtocol, conversation_store: ConversationStore) -> None:
        self.brain = brain
        self.conversation_store = conversation_store
        self.system_prompt = (
            "You are Miki, a personal AI assistant. "
            "You are helpful, concise, and natural in conversation. "
            "You do not claim to know things you do not know and do not claim to have completed actions you have not performed. "
            "You are designed to eventually have persistent memory and tools."
        )

    def process_user_input(self, user_input: str) -> str:
        if not user_input or not user_input.strip():
            return ""

        history = self.conversation_store.read_messages()
        logger.info("Processing user input for Miki Core")

        response = self.brain.generate_response(
            user_input=user_input,
            system_prompt=self.system_prompt,
            history=history,
        )

        self.conversation_store.append_turn(user_input, response)
        return response
