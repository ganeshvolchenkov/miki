from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Iterable

from app.memory.memory import Memory, MemoryStore
from app.rag.document import KnowledgeDocument

logger = logging.getLogger(__name__)


class MemoryDocumentLoader:
    """Converts active entries of the existing MemoryStore into KnowledgeDocuments.

    Read-only: never writes to the memory store. Memory IDs stay stable, so
    they double as the KnowledgeDocument id, which is what lets the Indexer
    detect updates/deletes across rebuilds.
    """

    def __init__(self, memory_store: MemoryStore) -> None:
        self.memory_store = memory_store

    def load(self) -> list[KnowledgeDocument]:
        return [self.to_document(memory) for memory in self.memory_store.list_memories(active_only=True)]

    @staticmethod
    def to_document(memory: Memory) -> KnowledgeDocument:
        return KnowledgeDocument(
            id=f"memory:{memory.memory_id}",
            content=memory.content,
            source="memory",
            metadata={
                "memory_id": memory.memory_id,
                "category": memory.category,
                "memory_type": memory.memory_type,
                "confidence": memory.confidence,
                "memory_source": memory.source,
                "created_at": memory.created_at,
                "updated_at": memory.updated_at,
                "last_confirmed_at": memory.last_confirmed_at,
                "importance": memory.importance,
                "stability": memory.stability,
                "usefulness": memory.usefulness,
            },
            updated_at=memory.updated_at,
        )


def _strip_frontmatter(text: str) -> str:
    if not text.startswith("---"):
        return text
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return text
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            return "\n".join(lines[index + 1 :]).strip()
    return text


def _extract_indexable_markdown(text: str) -> str:
    """Return the human-readable parts of a note that are useful for retrieval.

    Obsidian memory notes contain frontmatter plus a metadata table. That is
    great for humans, but noisy for semantic search. For indexing we keep the
    title and summary, and fall back to the remaining prose only if a summary
    is missing.
    """
    body = _strip_frontmatter(text).strip()
    if not body:
        return ""

    lines = body.splitlines()
    title: str | None = None
    summary_lines: list[str] = []
    in_summary = False

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if title is None and stripped.startswith("#"):
            title = stripped.lstrip("#").strip()
            continue
        if stripped.lower() == "## summary":
            in_summary = True
            continue
        if stripped.startswith("## ") and in_summary:
            break
        if in_summary:
            if stripped.startswith(">"):
                summary_lines.append(stripped.lstrip("> ").strip())
            elif stripped and not stripped.startswith("|"):
                summary_lines.append(stripped)

    if summary_lines:
        parts = []
        if title:
            parts.append(title)
        parts.append(" ".join(summary_lines).strip())
        return "\n".join(part for part in parts if part).strip()

    cleaned_lines: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("|"):
            continue
        if stripped.startswith("## "):
            continue
        if stripped.startswith("#"):
            if title is None:
                title = stripped.lstrip("#").strip()
            continue
        cleaned_lines.append(stripped)

    if title:
        cleaned_lines.insert(0, title)
    return "\n".join(cleaned_lines).strip()


class ObsidianDocumentLoader:
    """Reads Markdown notes from Miki's own ``Miki/`` vault directory.

    Read-only and scoped strictly to ``miki_dir`` -- it never touches the
    rest of the user's vault. The ``Miki/Memories/`` subtree is excluded by
    default because those notes are already represented via
    ``MemoryDocumentLoader``; indexing them again here would double them up
    under a different source label. ``Miki/Graph/`` (generated entity/hub
    notes) is excluded for the same reason.
    """

    def __init__(self, miki_dir: str | Path, *, exclude_dirs: Iterable[str | Path] | None = None) -> None:
        self.miki_dir = Path(miki_dir)
        if exclude_dirs is not None:
            self.exclude_dirs = {Path(p) for p in exclude_dirs}
        else:
            self.exclude_dirs = {self.miki_dir / "Memories", self.miki_dir / "Graph"}

    def load(self) -> list[KnowledgeDocument]:
        if not self.miki_dir.exists():
            return []

        documents: list[KnowledgeDocument] = []
        for path in sorted(self.miki_dir.rglob("*.md")):
            if not path.is_file():
                continue
            if any(self._is_within(path, excluded) for excluded in self.exclude_dirs):
                continue
            document = self._load_file(path)
            if document is not None:
                documents.append(document)
        return documents

    def _load_file(self, path: Path) -> KnowledgeDocument | None:
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            logger.warning("Could not read Obsidian note %s", path)
            return None

        content = _extract_indexable_markdown(raw)
        if not content:
            return None

        relative = path.relative_to(self.miki_dir.parent)
        try:
            updated_at = datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds")
        except OSError:
            updated_at = None

        return KnowledgeDocument(
            id=f"obsidian:{relative.as_posix()}",
            content=content,
            source="obsidian",
            metadata={"path": relative.as_posix(), "filename": path.name},
            updated_at=updated_at,
        )

    @staticmethod
    def _is_within(path: Path, other: Path) -> bool:
        try:
            path.relative_to(other)
            return True
        except ValueError:
            return False


class ConversationDocumentLoader:
    """Reads Miki's own conversation session storage (``data/conversations/``)
    and produces one KnowledgeDocument per session transcript.

    Read-only and scoped strictly to ``conversations_dir``. Empty messages
    and sessions with no non-empty content are skipped.
    """

    def __init__(self, conversations_dir: str | Path) -> None:
        self.conversations_dir = Path(conversations_dir)

    def load(self) -> list[KnowledgeDocument]:
        if not self.conversations_dir.exists():
            return []

        documents: list[KnowledgeDocument] = []
        for date_dir in sorted(p for p in self.conversations_dir.iterdir() if p.is_dir()):
            for session_file in sorted(date_dir.glob("session_*.json")):
                document = self._load_session(date_dir.name, session_file)
                if document is not None:
                    documents.append(document)
        return documents

    def _load_session(self, date: str, path: Path) -> KnowledgeDocument | None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.warning("Could not read conversation session %s", path)
            return None

        session_id = str(data.get("session_id", path.stem))
        messages = data.get("messages", [])
        lines: list[str] = []
        for message in messages:
            content = str(message.get("content", "")).strip()
            if not content:
                continue
            role = str(message.get("role", "user")).strip().title() or "User"
            lines.append(f"{role}: {content}")

        if not lines:
            return None

        updated_at = data.get("ended_at") or data.get("started_at")
        return KnowledgeDocument(
            id=f"conversation:{date}:{session_id}",
            content="\n".join(lines),
            source="conversation",
            metadata={
                "session_id": session_id,
                "date": date,
                "started_at": data.get("started_at"),
                "ended_at": data.get("ended_at"),
            },
            updated_at=updated_at,
        )
