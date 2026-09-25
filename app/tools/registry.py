from __future__ import annotations

import logging

from app.tools.base import Tool

logger = logging.getLogger(__name__)


class ToolRegistry:
    """Registers tools and exposes them to the Brain as structured OpenAI
    function-calling schemas. Miki Core never needs to know about individual
    tools -- it only talks to the registry."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool
        logger.info("Registered tool: %s", tool.name)

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def list_tools(self) -> list[Tool]:
        return list(self._tools.values())

    def has_tools(self) -> bool:
        return bool(self._tools)

    def build_openai_function_schemas(self) -> list[dict]:
        """Flattens every registered tool's operations into OpenAI function
        definitions, named `<tool>__<operation>` (e.g. `calendar__get_events`).

        Tools are exposed even when currently unavailable (e.g. Calendar not
        yet authenticated) -- the Brain can still call them and receive a
        clean, explainable error result rather than the tool disappearing.
        """
        schemas: list[dict] = []
        for tool in self._tools.values():
            for operation in tool.operations:
                schemas.append(
                    {
                        "type": "function",
                        "name": f"{tool.name}__{operation.name}",
                        "description": f"[{tool.name}] {operation.description}",
                        "parameters": operation.parameters,
                    }
                )
        return schemas

    def resolve_function_name(self, function_name: str) -> tuple[str, str] | None:
        """Maps a flat OpenAI function name back to (tool_name, operation_name)."""
        if "__" not in function_name:
            return None
        tool_name, operation_name = function_name.split("__", 1)
        if tool_name in self._tools:
            return tool_name, operation_name
        return None
