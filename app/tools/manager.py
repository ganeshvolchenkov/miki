from __future__ import annotations

import logging
from typing import Any

from app.tools.base import ToolError, ToolResult
from app.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

# Argument keys that should never be written to logs, even though none of
# the tools Miki ships today put secrets in tool-call arguments. Kept as a
# single, generic choke point rather than trusting every future tool to
# remember this itself.
_REDACTED_ARGUMENT_KEYS = {"token", "access_token", "refresh_token", "client_secret", "password", "api_key"}


class ToolManager:
    """Executes a single (tool, operation, arguments) request.

    Contains no tool-specific logic -- it only knows how to look a tool up
    in the registry, validate that the operation exists, execute it, and
    normalize whatever happens (success, a clean ToolError, or an unexpected
    exception) into a ToolResult. It does not implement the multi-turn
    tool-calling loop -- see app/core/tool_runner.py for that.
    """

    def __init__(self, registry: ToolRegistry) -> None:
        self.registry = registry

    def execute(self, tool_name: str, operation: str, arguments: dict[str, Any] | None) -> ToolResult:
        arguments = arguments if isinstance(arguments, dict) else {}

        tool = self.registry.get(tool_name)
        if tool is None:
            logger.info("TOOL CALL %s.%s -> error: tool_not_found", tool_name, operation)
            return ToolResult(
                success=False, operation=operation, error_type="tool_not_found",
                message=f"The '{tool_name}' tool isn't available.",
            )

        if tool.get_operation(operation) is None:
            logger.info("TOOL CALL %s.%s -> error: operation_not_found", tool_name, operation)
            return ToolResult(
                success=False, operation=operation, error_type="operation_not_found",
                message=f"'{operation}' isn't a supported operation for '{tool_name}'.",
            )

        try:
            result = tool.execute(operation, arguments)
        except ToolError as exc:
            result = ToolResult(success=False, operation=operation, error_type=exc.error_type, message=exc.message)
        except Exception:
            logger.exception("Unhandled error executing %s.%s", tool_name, operation)
            result = ToolResult(
                success=False, operation=operation, error_type="tool_error",
                message="Something went wrong running that tool.",
            )

        if result.requires_confirmation:
            outcome = "awaiting_confirmation"
        else:
            outcome = "success" if result.success else f"error:{result.error_type}"
        logger.info("TOOL CALL %s.%s arguments=%s -> %s", tool_name, operation, _safe_arguments(arguments), outcome)
        return result


def _safe_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    return {key: ("<redacted>" if key.lower() in _REDACTED_ARGUMENT_KEYS else value) for key, value in arguments.items()}
