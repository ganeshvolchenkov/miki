from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.utcnow().isoformat(timespec="seconds")


# Fields a store's create_memory/update_memory accept beyond the required core
# ones. Kept in one place so JSONMemoryStore and ObsidianMemoryStore agree on
# what's writable.
MEMORY_METADATA_FIELDS = {"importance", "stability", "usefulness", "last_confirmed_at"}
# Knowledge-graph fields: a short statement title, the entities a memory mentions
# (name -> type in ``entity_types``), and explicit links to related memory ids.
MEMORY_GRAPH_FIELDS = {"title", "entities", "entity_types", "related"}
MEMORY_UPDATABLE_FIELDS = {"content", "category", "memory_type", "confidence", "source", "active", *MEMORY_METADATA_FIELDS, *MEMORY_GRAPH_FIELDS}


@dataclass
class Memory:
    memory_id: str
    content: str
    category: str
    memory_type: str
    confidence: float
    source: str
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    active: bool = True
    # Memory-intelligence metadata (v0.6). Defaulted so old records without
    # these fields still load and behave sensibly (neutral 0.5 signal).
    importance: float = 0.5
    stability: float = 0.5
    usefulness: float = 0.5
    last_confirmed_at: str = field(default_factory=utc_now)
    # Knowledge-graph metadata. Empty for legacy records, which still work.
    title: str = ""
    entities: list[str] = field(default_factory=list)
    entity_types: dict[str, str] = field(default_factory=dict)
    related: list[str] = field(default_factory=list)

    @property
    def status(self) -> str:
        return "active" if self.active else "inactive"

    def to_dict(self) -> dict[str, Any]:
        return {
            "memory_id": self.memory_id,
            "content": self.content,
            "category": self.category,
            "memory_type": self.memory_type,
            "confidence": self.confidence,
            "source": self.source,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "active": self.active,
            "importance": self.importance,
            "stability": self.stability,
            "usefulness": self.usefulness,
            "last_confirmed_at": self.last_confirmed_at,
            "title": self.title,
            "entities": list(self.entities),
            "entity_types": dict(self.entity_types),
            "related": list(self.related),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "Memory":
        updated_at = str(payload.get("updated_at", utc_now()))
        return cls(
            memory_id=str(payload.get("memory_id", uuid.uuid4().hex[:12])),
            content=str(payload.get("content", "")),
            category=str(payload.get("category", "general")),
            memory_type=str(payload.get("memory_type", "fact")),
            confidence=float(payload.get("confidence", 0.5)),
            source=str(payload.get("source", "unknown")),
            created_at=str(payload.get("created_at", utc_now())),
            updated_at=updated_at,
            active=bool(payload.get("active", True)),
            importance=_safe_float(payload.get("importance"), default=0.5),
            stability=_safe_float(payload.get("stability"), default=0.5),
            usefulness=_safe_float(payload.get("usefulness"), default=0.5),
            last_confirmed_at=str(payload.get("last_confirmed_at") or updated_at),
            title=str(payload.get("title") or ""),
            entities=_string_list(payload.get("entities")),
            entity_types=_string_dict(payload.get("entity_types")),
            related=_string_list(payload.get("related")),
        )


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _string_dict(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(key).strip(): str(item).strip() for key, item in value.items() if str(key).strip()}


def _safe_float(value: Any, *, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class MemoryStore:
    def create_memory(self, *, content: str, category: str, memory_type: str, confidence: float, source: str, **metadata: Any) -> Memory:
        raise NotImplementedError

    def get_memory(self, memory_id: str) -> Memory | None:
        raise NotImplementedError

    def list_memories(self, *, active_only: bool = True) -> list[Memory]:
        raise NotImplementedError

    def update_memory(self, memory_id: str, **updates: Any) -> Memory | None:
        raise NotImplementedError

    def delete_memory(self, memory_id: str) -> Memory | None:
        raise NotImplementedError

    def count_memories(self, *, active_only: bool = True) -> int:
        raise NotImplementedError


class JSONMemoryStore(MemoryStore):
    """Simple JSON-based persistent store for long-term memory."""

    def __init__(self, storage_dir: str | Path | None = None) -> None:
        self.storage_dir = Path(storage_dir) if storage_dir is not None else Path("data/memories")
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.file_path = self.storage_dir / "memories.json"
        if not self.file_path.exists():
            self._save_store([])

    def _load_store(self) -> list[dict[str, Any]]:
        with self.file_path.open("r", encoding="utf-8") as file:
            data = json.load(file)
        return data if isinstance(data, list) else []

    def _save_store(self, payload: list[dict[str, Any]]) -> None:
        with self.file_path.open("w", encoding="utf-8") as file:
            json.dump(payload, file, indent=2)

    def create_memory(
        self,
        *,
        content: str,
        category: str = "general",
        memory_type: str = "fact",
        confidence: float = 0.5,
        source: str = "conversation",
        importance: float = 0.5,
        stability: float = 0.5,
        usefulness: float = 0.5,
        title: str = "",
        entities: list[str] | None = None,
        entity_types: dict[str, str] | None = None,
        related: list[str] | None = None,
    ) -> Memory:
        if not content or not content.strip():
            raise ValueError("Memory content cannot be empty.")

        normalized = self._normalize_content(content)
        existing = self.list_memories(active_only=True)
        for memory in existing:
            if self._normalize_content(memory.content) == normalized and memory.category == category:
                return memory

        memory = Memory(
            memory_id=uuid.uuid4().hex[:12],
            content=content.strip(),
            category=category,
            memory_type=memory_type,
            confidence=max(0.0, min(1.0, float(confidence))),
            source=source,
            importance=max(0.0, min(1.0, float(importance))),
            stability=max(0.0, min(1.0, float(stability))),
            usefulness=max(0.0, min(1.0, float(usefulness))),
            title=title or "",
            entities=_string_list(entities),
            entity_types=_string_dict(entity_types),
            related=_string_list(related),
        )

        payload = self._load_store()
        payload.append(memory.to_dict())
        self._save_store(payload)
        return memory

    def get_memory(self, memory_id: str) -> Memory | None:
        for memory in self.list_memories(active_only=False):
            if memory.memory_id == memory_id:
                return memory
        return None

    def list_memories(self, *, active_only: bool = True) -> list[Memory]:
        memories = [Memory.from_dict(item) for item in self._load_store()]
        if active_only:
            memories = [memory for memory in memories if memory.active]
        return memories

    def update_memory(self, memory_id: str, **updates: Any) -> Memory | None:
        payload = self._load_store()
        for item in payload:
            if item.get("memory_id") == memory_id:
                for key, value in updates.items():
                    if key in MEMORY_UPDATABLE_FIELDS:
                        item[key] = value
                item["updated_at"] = utc_now()
                self._save_store(payload)
                return Memory.from_dict(item)
        return None

    def delete_memory(self, memory_id: str) -> Memory | None:
        payload = self._load_store()
        for item in payload:
            if item.get("memory_id") == memory_id:
                item["active"] = False
                item["updated_at"] = utc_now()
                self._save_store(payload)
                return Memory.from_dict(item)
        return None

    def count_memories(self, *, active_only: bool = True) -> int:
        return len(self.list_memories(active_only=active_only))

    @staticmethod
    def _normalize_content(value: str) -> str:
        return " ".join(value.strip().lower().split())
