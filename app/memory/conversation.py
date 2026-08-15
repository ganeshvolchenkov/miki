from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class ConversationTurn:
    role: str
    content: str
    timestamp: str = field(default_factory=lambda: datetime.utcnow().isoformat(timespec="seconds"))


class ConversationStore:
    """Abstract storage contract for conversation memory."""

    def append_turn(self, user_message: str, assistant_message: str) -> None:
        raise NotImplementedError

    def read_messages(self) -> list[dict[str, str]]:
        raise NotImplementedError


class JSONConversationStore(ConversationStore):
    """Simple JSON-backed local conversation store for the initial version."""

    def __init__(self, storage_dir: str | Path | None = None) -> None:
        self.storage_dir = Path(storage_dir) if storage_dir is not None else Path("data/conversations")
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.file_path = self.storage_dir / "conversation.json"

    @staticmethod
    def _filename_for_today() -> str:
        return "conversation.json"

    def append_turn(self, user_message: str, assistant_message: str) -> None:
        try:
            data = self._load_data()
        except FileNotFoundError:
            data = {"date": datetime.utcnow().strftime("%Y-%m-%d"), "messages": []}

        conversation = [
            {"role": "user", "content": user_message, "timestamp": datetime.utcnow().isoformat(timespec="seconds")},
            {"role": "assistant", "content": assistant_message, "timestamp": datetime.utcnow().isoformat(timespec="seconds")},
        ]

        data.setdefault("messages", []).extend(conversation)
        self._save_data(data)
        logger.info("Saved conversation turn to %s", self.file_path)

    def read_messages(self) -> list[dict[str, str]]:
        try:
            data = self._load_data()
        except FileNotFoundError:
            return []
        return data.get("messages", [])

    def _load_data(self) -> dict[str, Any]:
        if not self.file_path.exists():
            raise FileNotFoundError(self.file_path)
        with self.file_path.open("r", encoding="utf-8") as file:
            return json.load(file)

    def _save_data(self, data: dict[str, Any]) -> None:
        try:
            with self.file_path.open("w", encoding="utf-8") as file:
                json.dump(data, file, indent=2)
        except OSError:
            logger.exception("Could not write conversation history to %s", self.file_path)
            raise
