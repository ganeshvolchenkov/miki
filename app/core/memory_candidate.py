from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"


def _utc_now() -> str:
    return datetime.utcnow().isoformat(timespec="seconds")


@dataclass
class MemoryCandidate:
    """An uncertain memory awaiting human review (see spec section 12/13).

    Candidates never become durable memories automatically -- only
    /memory approve promotes one, via the normal extraction+comparison path.
    """

    candidate_id: str
    raw_message: str
    score: float
    reason: str
    category: str | None = None
    memory_type: str | None = None
    created_at: str = field(default_factory=_utc_now)
    status: str = STATUS_PENDING
    resolved_at: str | None = None
    resulting_memory_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "raw_message": self.raw_message,
            "score": self.score,
            "reason": self.reason,
            "category": self.category,
            "memory_type": self.memory_type,
            "created_at": self.created_at,
            "status": self.status,
            "resolved_at": self.resolved_at,
            "resulting_memory_id": self.resulting_memory_id,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "MemoryCandidate":
        return cls(
            candidate_id=str(payload.get("candidate_id", uuid.uuid4().hex[:12])),
            raw_message=str(payload.get("raw_message", "")),
            score=float(payload.get("score", 0.5)),
            reason=str(payload.get("reason", "")),
            category=payload.get("category"),
            memory_type=payload.get("memory_type"),
            created_at=str(payload.get("created_at", _utc_now())),
            status=str(payload.get("status", STATUS_PENDING)),
            resolved_at=payload.get("resolved_at"),
            resulting_memory_id=payload.get("resulting_memory_id"),
        )


class MemoryCandidateStore:
    """Simple JSON-backed store for memory candidates, mirroring JSONMemoryStore."""

    def __init__(self, storage_dir: str | Path | None = None) -> None:
        self.storage_dir = Path(storage_dir) if storage_dir is not None else Path("data/memory_candidates")
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.file_path = self.storage_dir / "candidates.json"
        if not self.file_path.exists():
            self._save([])

    def _load(self) -> list[dict[str, Any]]:
        with self.file_path.open("r", encoding="utf-8") as file:
            data = json.load(file)
        return data if isinstance(data, list) else []

    def _save(self, payload: list[dict[str, Any]]) -> None:
        with self.file_path.open("w", encoding="utf-8") as file:
            json.dump(payload, file, indent=2)

    def add(
        self,
        *,
        raw_message: str,
        score: float,
        reason: str,
        category: str | None = None,
        memory_type: str | None = None,
    ) -> MemoryCandidate:
        candidate = MemoryCandidate(
            candidate_id=uuid.uuid4().hex[:12],
            raw_message=raw_message,
            score=score,
            reason=reason,
            category=category,
            memory_type=memory_type,
        )
        payload = self._load()
        payload.append(candidate.to_dict())
        self._save(payload)
        return candidate

    def get(self, candidate_id: str) -> MemoryCandidate | None:
        for item in self._load():
            if item.get("candidate_id") == candidate_id:
                return MemoryCandidate.from_dict(item)
        return None

    def list(self, *, status: str | None = STATUS_PENDING) -> list[MemoryCandidate]:
        candidates = [MemoryCandidate.from_dict(item) for item in self._load()]
        if status is not None:
            candidates = [c for c in candidates if c.status == status]
        candidates.sort(key=lambda c: c.created_at, reverse=True)
        return candidates

    def resolve(self, candidate_id: str, *, status: str, resulting_memory_id: str | None = None) -> MemoryCandidate | None:
        payload = self._load()
        for item in payload:
            if item.get("candidate_id") == candidate_id:
                item["status"] = status
                item["resolved_at"] = _utc_now()
                item["resulting_memory_id"] = resulting_memory_id
                self._save(payload)
                return MemoryCandidate.from_dict(item)
        return None
