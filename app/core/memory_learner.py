"""Learn from conversations Miki already had.

Older chats hold plenty of durable facts that were never stored (they predate the current memory
pipeline). The learner replays what the *user* said through the normal pipeline; the comparator
prevents duplicates, and a small state file remembers which messages were already processed so the
job is safe to re-run and never pays twice for the same message.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

_MIN_CHARS = 12


class ConversationLearner:
    def __init__(
        self,
        manager: Any,
        conversations_dir: str | Path = "data/conversations",
        state_path: str | Path = "data/learned_messages.json",
    ) -> None:
        self.manager = manager
        self.conversations_dir = Path(conversations_dir)
        self.state_path = Path(state_path)

    # ------------------------------------------------------------------ state
    def _load_done(self) -> set[str]:
        try:
            return set(json.loads(self.state_path.read_text(encoding="utf-8")).get("done", []))
        except (OSError, ValueError):
            return set()

    def _save_done(self, done: set[str]) -> None:
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(json.dumps({"done": sorted(done)}), encoding="utf-8")
        except OSError:
            logger.warning("Could not save learner state", exc_info=True)

    @staticmethod
    def _key(text: str) -> str:
        return hashlib.sha1(" ".join(text.lower().split()).encode("utf-8")).hexdigest()[:16]

    # ------------------------------------------------------------------ discovery
    def user_messages(self) -> list[str]:
        """Every distinct user message across all saved sessions, oldest first."""
        seen: set[str] = set()
        messages: list[str] = []
        for path in sorted(self.conversations_dir.glob("*/session_*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            for item in data.get("messages", []):
                if item.get("role") != "user":
                    continue
                text = str(item.get("content", "")).strip()
                key = self._key(text)
                if len(text) >= _MIN_CHARS and not text.startswith("/") and key not in seen:
                    seen.add(key)
                    messages.append(text)
        return messages

    def pending(self) -> list[str]:
        done = self._load_done()
        return [m for m in self.user_messages() if self._key(m) not in done]

    # ------------------------------------------------------------------ work
    def learn(self, *, limit: int | None = None, on_progress: Callable[[int, int], None] | None = None) -> dict[str, int]:
        pending = self.pending()
        if limit is not None:
            pending = pending[:limit]
        done = self._load_done()
        stored = 0
        for index, message in enumerate(pending, 1):
            try:
                self.manager.extract_and_store_from_interaction(message, None)
                stored += len(getattr(self.manager, "last_memories", []) or [])
            except Exception:
                logger.exception("Learner failed on a message; continuing")
            done.add(self._key(message))
            if index % 10 == 0:
                self._save_done(done)
            if on_progress is not None:
                on_progress(index, len(pending))
        self._save_done(done)
        return {"messages_read": len(pending), "memories_stored_or_updated": stored, "remaining": len(self.pending())}
