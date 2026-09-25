from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Access-level metadata (spec section 16). Deliberately just three labels
# attached to each operation -- not a full permission system. Future tools
# reuse the same three levels; nothing here is Calendar-specific.
ACCESS_READ = "read"
ACCESS_WRITE = "write"
ACCESS_DESTRUCTIVE = "destructive"


@dataclass(frozen=True)
class ToolOperation:
    """Metadata for a single operation a Tool exposes, e.g. calendar.get_events.

    ``parameters`` is a JSON Schema object, handed to the Brain as-is when
    building an OpenAI function-calling tool definition.
    """

    name: str
    description: str
    parameters: dict[str, Any]
    access: str = ACCESS_READ


@dataclass
class ToolResult:
    """Normalized outcome of a tool execution. Never a raw provider response
    (e.g. a raw Google Calendar API payload) -- always Miki's own shape, so
    the Brain gets a consistent structure regardless of which tool ran."""

    success: bool
    operation: str
    data: dict[str, Any] | None = None
    message: str | None = None
    error_type: str | None = None
    requires_confirmation: bool = False

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"success": self.success, "operation": self.operation}
        if self.data is not None:
            payload["data"] = self.data
        if self.message is not None:
            payload["message"] = self.message
        if self.error_type is not None:
            payload["error_type"] = self.error_type
        if self.requires_confirmation:
            payload["requires_confirmation"] = True
        return payload


class ToolError(Exception):
    """Raised by a Tool to signal a clean, user-safe failure -- invalid
    arguments, not found, permission denied, etc. ToolManager catches this
    and turns it into a ToolResult instead of a stack trace reaching the
    conversation loop."""

    def __init__(self, error_type: str, message: str) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.message = message


@dataclass
class Tool:
    """Base shape every tool implements.

    A concrete tool (see app/tools/calendar/tool.py) sets ``name``,
    ``description``, and ``operations`` and overrides ``execute``. The rest
    of Miki -- the registry, the manager, the Brain -- only ever depends on
    this interface, never on a specific tool's implementation.
    """

    name: str = ""
    description: str = ""
    operations: list[ToolOperation] = field(default_factory=list)

    def get_operation(self, name: str) -> ToolOperation | None:
        return next((op for op in self.operations if op.name == name), None)

    def execute(self, operation: str, arguments: dict[str, Any]) -> ToolResult:
        raise NotImplementedError

    def is_available(self) -> bool:
        """Whether this tool is currently usable (e.g. credentials configured
        and valid). Used by debugging commands, not by the Brain -- the tool
        is still offered to the Brain even when unavailable, so a normal
        `execute()` call can return a clean, explainable error instead of
        the tool silently disappearing."""
        return True

    def status(self) -> dict[str, Any]:
        """Human-readable connection/availability status for debugging
        commands like /tools and /calendar status."""
        return {"available": self.is_available()}
