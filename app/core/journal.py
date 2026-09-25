"""Daily journal: Miki's episodic memory.

Semantic memory says *who you are* ("plays basketball"); the journal says *what happened*
("Tuesday: talked about the exam, planned basketball with Dad"). One short note per day, linked to the
people and interests it mentions, written into the Obsidian vault -- where Miki's search index
(RAG) picks it up, so "what did we talk about last week?" can actually be answered.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime
from pathlib import Path
from typing import Any, Protocol

logger = logging.getLogger(__name__)


class BrainProtocol(Protocol):
    def generate_response(self, user_input: str, system_prompt: str, history: list[dict[str, str]] | None = None) -> str:
        ...


_SYSTEM_PROMPT = """You write one day's entry in a personal AI assistant's journal about its user, from that day's \
conversation. Be concrete and brief. Use ONLY what was said; never invent anything.

Respond ONLY with valid JSON (no markdown):
{"summary": "3-5 sentences on what the user talked about, planned, felt or decided that day (third person)", \
"topics": ["short topic", "..."], "open_loops": ["things the user said they would do or wanted to follow up on"]}"""


def day_messages(conversations_dir: str | Path, day: date) -> list[dict[str, str]]:
    """All messages saved for ``day`` (sessions are stored in per-day folders)."""
    messages: list[dict[str, str]] = []
    for path in sorted((Path(conversations_dir) / day.isoformat()).glob("session_*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        messages.extend(m for m in data.get("messages", []) if m.get("content"))
    return messages


class DailyJournal:
    def __init__(self, brain: BrainProtocol, store: Any, conversations_dir: str | Path = "data/conversations") -> None:
        self.brain = brain
        self.store = store  # an ObsidianMemoryStore (anything with write_journal_note)
        self.conversations_dir = conversations_dir

    def has_entry(self, day: date) -> bool:
        exists = getattr(self.store, "journal_exists", None)
        return bool(exists(day.isoformat())) if callable(exists) else False

    def write(self, day: date | None = None) -> Path | None:
        """Summarise ``day`` (default: today). Returns the note path, or None if there was nothing to write."""
        write_note = getattr(self.store, "write_journal_note", None)
        if not callable(write_note):
            return None
        day = day or date.today()
        messages = day_messages(self.conversations_dir, day)
        if not any(m.get("role") == "user" for m in messages):
            return None

        transcript = "\n".join(f"{m.get('role', 'user').upper()}: {str(m.get('content', ''))[:600]}" for m in messages[-60:])
        response = self.brain.generate_response(
            user_input=f"Date: {day.isoformat()}\n\nConversation:\n{transcript}\n\nWrite the journal JSON now.",
            system_prompt=_SYSTEM_PROMPT,
            history=None,
        )
        payload = _parse_json(response)
        return write_note(
            day.isoformat(),
            summary=str(payload.get("summary", "")).strip(),
            topics=[str(t).strip() for t in payload.get("topics", []) if str(t).strip()][:8],
            open_loops=[str(t).strip() for t in payload.get("open_loops", []) if str(t).strip()][:6],
            message_count=sum(1 for m in messages if m.get("role") == "user"),
        )


def _parse_json(text: str) -> dict[str, Any]:
    cleaned = (text or "").strip()
    if cleaned.startswith("```json"):
        cleaned = cleaned[7:]
    if cleaned.startswith("```"):
        cleaned = cleaned[3:]
    if cleaned.endswith("```"):
        cleaned = cleaned[:-3]
    return json.loads(cleaned.strip())


def yesterday_entry_missing(journal: DailyJournal, today: date | None = None) -> date | None:
    """The most recent past day that has conversations but no journal entry (looks back a week)."""
    today = today or date.today()
    for back in range(1, 8):
        day = date.fromordinal(today.toordinal() - back)
        if day_messages(journal.conversations_dir, day) and not journal.has_entry(day):
            return day
    return None
