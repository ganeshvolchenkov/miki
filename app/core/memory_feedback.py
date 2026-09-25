from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path


def _utc_now() -> str:
    return datetime.utcnow().isoformat(timespec="seconds")


class MemoryFeedbackDataset:
    """Appends human approve/reject decisions as JSONL training examples.

    This is infrastructure for eventually building a Miki-specific dataset --
    it does NOT fine-tune anything. Each line is a self-contained example:
    {"message": "...", "memory_decision": true, "category": "habit",
     "confidence": 0.91, "user_feedback": "approved", "recorded_at": "..."}
    """

    def __init__(self, storage_dir: str | Path | None = None) -> None:
        self.storage_dir = Path(storage_dir) if storage_dir is not None else Path("data/memory_training")
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.file_path = self.storage_dir / "examples.jsonl"

    def record(
        self,
        *,
        message: str,
        memory_decision: bool,
        user_feedback: str,
        category: str | None = None,
        confidence: float | None = None,
    ) -> None:
        example = {
            "message": message,
            "memory_decision": memory_decision,
            "category": category,
            "confidence": confidence,
            "user_feedback": user_feedback,
            "recorded_at": _utc_now(),
        }
        with self.file_path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(example) + "\n")

    def count(self) -> int:
        if not self.file_path.exists():
            return 0
        with self.file_path.open("r", encoding="utf-8") as file:
            return sum(1 for line in file if line.strip())
