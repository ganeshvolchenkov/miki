from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class Event:
    name: str
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.utcnow)


class EventBus:
    """Minimal event abstraction for future Miki event-driven features."""

    def __init__(self) -> None:
        self._handlers: dict[str, list] = {}

    def subscribe(self, event_name: str, handler) -> None:
        self._handlers.setdefault(event_name, []).append(handler)

    def emit(self, event_name: str, payload: dict[str, Any] | None = None) -> None:
        event = Event(name=event_name, payload=payload or {})
        for handler in self._handlers.get(event_name, []):
            handler(event)
