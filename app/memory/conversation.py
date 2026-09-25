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
    """Abstract storage contract for conversation sessions and memory."""

    def append_turn(self, user_message: str, assistant_message: str) -> None:
        raise NotImplementedError

    def read_messages(self) -> list[dict[str, str]]:
        raise NotImplementedError

    def close_session(self) -> None:
        raise NotImplementedError


class JSONConversationStore(ConversationStore):
    """Simple JSON-backed session store for the initial version."""

    def __init__(self, storage_dir: str | Path | None = None) -> None:
        self.storage_dir = Path(storage_dir) if storage_dir is not None else Path("data/conversations")
        self.date_dir = self.storage_dir / datetime.utcnow().strftime("%Y-%m-%d")
        self.date_dir.mkdir(parents=True, exist_ok=True)
        self.session_id = self._get_or_create_session_id()
        self.file_path = self.date_dir / f"{self.session_id}.json"

        if not self.file_path.exists():
            self._save_data(
                {
                    "session_id": self.session_id,
                    "started_at": datetime.utcnow().isoformat(timespec="seconds"),
                    "ended_at": None,
                    "messages": [],
                }
            )

    def _get_or_create_session_id(self) -> str:
        existing_sessions = sorted(self.date_dir.glob("session_*.json"))
        if existing_sessions:
            latest = existing_sessions[-1]
            data = self._read_json(latest)
            if data.get("ended_at") is None:
                self.file_path = latest
                return data.get("session_id", latest.stem)

            next_number = len(existing_sessions) + 1
        else:
            next_number = 1

        return f"session_{next_number:03d}"

    def append_turn(self, user_message: str, assistant_message: str) -> None:
        data = self._load_data()
        data.setdefault("messages", [])

        data["messages"].extend(
            [
                {"role": "user", "content": user_message, "timestamp": datetime.utcnow().isoformat(timespec="seconds")},
                {"role": "assistant", "content": assistant_message, "timestamp": datetime.utcnow().isoformat(timespec="seconds")},
            ]
        )

        if data.get("started_at") is None:
            data["started_at"] = datetime.utcnow().isoformat(timespec="seconds")

        self._save_data(data)
        logger.info("Saved conversation turn to %s", self.file_path)

    def read_messages(self) -> list[dict[str, str]]:
        try:
            data = self._load_data()
        except FileNotFoundError:
            return []
        return data.get("messages", [])

    def close_session(self) -> None:
        try:
            data = self._load_data()
        except FileNotFoundError:
            return

        if data.get("ended_at") is None:
            data["ended_at"] = datetime.utcnow().isoformat(timespec="seconds")
            self._save_data(data)
            logger.info("Closed session %s", self.session_id)

    def _load_data(self) -> dict[str, Any]:
        if not self.file_path.exists():
            raise FileNotFoundError(self.file_path)
        return self._read_json(self.file_path)

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        with path.open("r", encoding="utf-8") as file:
            return json.load(file)

    def _save_data(self, data: dict[str, Any]) -> None:
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self.file_path.open("w", encoding="utf-8") as file:
                json.dump(data, file, indent=2)
        except OSError:
            logger.exception("Could not write conversation history to %s", self.file_path)
            raise
