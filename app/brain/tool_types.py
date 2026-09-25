from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolCallRequest:
    """A single tool call the Brain asked for."""

    call_id: str
    name: str  # flat OpenAI function name, e.g. "calendar__delete_event"
    arguments: dict[str, Any]


@dataclass
class BrainToolTurn:
    """Result of one Brain round-trip in a tool-enabled conversation.

    ``output_items`` are the raw response items (message / function_call),
    already serialized to plain dicts, so the caller can echo them back into
    the next request's ``input`` list to continue the conversation.
    """

    text: str | None
    tool_calls: list[ToolCallRequest] = field(default_factory=list)
    output_items: list[dict[str, Any]] = field(default_factory=list)
