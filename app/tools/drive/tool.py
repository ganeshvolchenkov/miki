from __future__ import annotations

import logging
from typing import Any, Callable

from app.tools.base import ACCESS_READ, Tool, ToolError, ToolOperation, ToolResult
from app.tools.drive.client import DriveApiError, DriveClientProtocol
from app.tools.calendar.auth import CalendarAuthRequired, CalendarUnavailable

logger = logging.getLogger(__name__)

class DriveTool(Tool):
    def __init__(self, *, client_factory: Callable[[], DriveClientProtocol]) -> None:
        super().__init__(
            name="drive",
            description="Search and view files in the user's Google Drive.",
            operations=[
                ToolOperation(
                    name="search_files",
                    description="Search for files in Google Drive by name, type, or content.",
                    access=ACCESS_READ,
                    parameters={
                        "type": "object",
                        "properties": {
                            "query": {"type": "string", "description": "Google Drive search query (e.g. \"name contains 'budget'\"). If empty, returns recent files."},
                            "max_results": {"type": "integer", "description": "Number of results to return (max 20)."}
                        }
                    }
                )
            ]
        )
        self._client_factory = client_factory

    def execute(self, operation: str, arguments: dict[str, Any]) -> ToolResult:
        try:
            client = self._client_factory()
        except CalendarAuthRequired as exc:
            raise ToolError("auth_required", str(exc)) from exc
        except CalendarUnavailable as exc:
            raise ToolError("unavailable", str(exc)) from exc

        try:
            if operation == "search_files":
                return self._search_files(client, arguments)
            else:
                raise ToolError("unknown_operation", f"Unknown operation: {operation}")
        except DriveApiError as exc:
            raise ToolError(exc.error_type, exc.message) from exc

    def _search_files(self, client: DriveClientProtocol, arguments: dict[str, Any]) -> ToolResult:
        query = arguments.get("query", "")
        max_results = min(int(arguments.get("max_results", 10)), 20)
        files = client.search_files(query=query, max_results=max_results)
        return ToolResult(success=True, operation="search_files", data={"files": files, "count": len(files)})

    def is_available(self) -> bool:
        try:
            self._client_factory()
            return True
        except Exception:
            return False

    def status(self) -> dict[str, Any]:
        try:
            self._client_factory()
            return {"connected": True}
        except CalendarAuthRequired as exc:
            return {"connected": False, "reason": str(exc)}
        except Exception as exc:
            return {"connected": False, "reason": f"Error: {exc}"}
