from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import tzinfo
from typing import Any, Protocol

from app.brain.tool_types import BrainToolTurn
from app.core.timeutil import format_current_time
from app.tools.base import ACCESS_READ, ToolResult
from app.tools.manager import ToolManager

logger = logging.getLogger(__name__)

DEFAULT_MAX_TOOL_ITERATIONS = 5


class ToolCallingBrainProtocol(Protocol):
    def generate_tool_turn(self, *, input_items: list[dict[str, Any]], tools: list[dict[str, Any]]) -> BrainToolTurn:
        ...


@dataclass
class PendingToolAction:
    """A write/destructive tool call that previewed its effect and is
    waiting for the user's explicit confirmation, carried across exactly one
    turn.

    The model decides *when* to re-issue the call (from conversation
    context, e.g. the user saying "yes") -- but it never decides whether
    that counts as real confirmation. ToolConversationRunner is the sole
    authority on that: a call's `confirm` argument is only ever honored when
    it matches this exact pending action (see `_gate_confirmation` below).
    A model that sets confirm=true unprompted, on its first attempt at a
    write/destructive operation, is always overridden back to false -- this
    is a real failure mode observed with gpt-4o-mini treating a direct,
    unambiguous user request as self-evidently confirmed.
    """

    tool_name: str
    operation: str
    arguments: dict[str, Any]
    summary: str

    def as_prompt_note(self) -> str:
        confirm_arguments = dict(self.arguments)
        confirm_arguments["confirm"] = True
        return (
            "There is a pending action awaiting the user's explicit confirmation:\n"
            f"{self.summary}\n"
            f"If the user's latest message clearly confirms it (e.g. yes, confirm, do it, go ahead), call "
            f"{self.tool_name}__{self.operation} again with exactly these arguments: {json.dumps(confirm_arguments)}\n"
            "If they decline, change their mind, or ask about something else, do not call it -- just respond naturally."
        )


@dataclass
class ToolRunResult:
    text: str
    pending_action: PendingToolAction | None
    has_tool_calls: bool = False


class ToolConversationRunner:
    """Owns the bounded "Brain requests tool -> Miki executes -> result back
    to Brain" loop (spec section 5). Neither the Brain nor the ToolManager
    know this loop exists -- it is the only place that does.
    """

    def __init__(
        self,
        *,
        brain: ToolCallingBrainProtocol,
        tool_manager: ToolManager,
        tzinfo: tzinfo,
        max_iterations: int = DEFAULT_MAX_TOOL_ITERATIONS,
    ) -> None:
        self.brain = brain
        self.tool_manager = tool_manager
        self.tzinfo = tzinfo
        self.max_iterations = max(1, max_iterations)

    def run(
        self,
        *,
        user_input: str,
        system_prompt: str,
        history: list[dict[str, str]] | None,
        pending_action: PendingToolAction | None = None,
    ) -> ToolRunResult:
        tools_schema = self.tool_manager.registry.build_openai_function_schemas()

        effective_system_prompt = f"{system_prompt}\n\n{self._time_context_note()}"
        if pending_action is not None:
            effective_system_prompt = f"{effective_system_prompt}\n\n{pending_action.as_prompt_note()}"

        input_items: list[dict[str, Any]] = [{"role": "system", "content": effective_system_prompt}]
        for item in history or []:
            role = item.get("role", "user")
            if role in {"user", "assistant", "system"}:
                input_items.append({"role": role, "content": item.get("content", "")})
        input_items.append({"role": "user", "content": user_input})

        new_pending: PendingToolAction | None = None
        has_tool_calls = False

        for _ in range(self.max_iterations):
            turn = self.brain.generate_tool_turn(input_items=input_items, tools=tools_schema)
            if not turn.tool_calls:
                return ToolRunResult(text=turn.text or "", pending_action=new_pending, has_tool_calls=has_tool_calls)

            has_tool_calls = True
            input_items.extend(turn.output_items)

            for call in turn.tool_calls:
                resolved = self.tool_manager.registry.resolve_function_name(call.name)
                if resolved is None:
                    result = ToolResult(success=False, operation=call.name, error_type="unknown_tool", message="That tool isn't available.")
                else:
                    tool_name, operation = resolved
                    arguments = self._gate_confirmation(tool_name, operation, call.arguments, pending_action)
                    result = self.tool_manager.execute(tool_name, operation, arguments)
                    if result.requires_confirmation:
                        new_pending = PendingToolAction(
                            tool_name=tool_name,
                            operation=operation,
                            arguments={k: v for k, v in arguments.items() if k != "confirm"},
                            summary=result.message or f"{tool_name}.{operation}",
                        )
                    elif result.success:
                        new_pending = None

                input_items.append(
                    {
                        "type": "function_call_output",
                        "call_id": call.call_id,
                        "output": json.dumps(result.to_dict()),
                    }
                )

        logger.warning("Tool conversation hit max_iterations=%d without a final answer", self.max_iterations)
        final_turn = self.brain.generate_tool_turn(input_items=input_items, tools=[])
        fallback_text = final_turn.text or "I wasn't able to finish that after a few tries -- could you try rephrasing or simplifying the request?"
        return ToolRunResult(text=fallback_text, pending_action=new_pending)

    def _time_context_note(self) -> str:
        return (
            f"Current date/time: {format_current_time(self.tzinfo)}. "
            "When the user refers to relative dates or times (today, tomorrow, this week, 3pm, Saturday), "
            "compute the exact date/time yourself using this reference before calling a tool. "
            "Always pass ISO 8601 datetimes with a UTC offset to tools."
        )

    def _gate_confirmation(
        self,
        tool_name: str,
        operation: str,
        arguments: dict[str, Any],
        pending_action: PendingToolAction | None,
    ) -> dict[str, Any]:
        """Ensures a model cannot execute a write/destructive operation by setting
        confirm=True unprompted on its first try.

        confirm=True is only preserved if there is an active pending_action
        matching this exact tool, operation, and base arguments. Otherwise, confirm
        is forced to False.
        """
        gated_args = dict(arguments)
        if gated_args.get("confirm"):
            if (
                pending_action is not None
                and pending_action.tool_name == tool_name
                and pending_action.operation == operation
                and pending_action.arguments == {k: v for k, v in gated_args.items() if k != "confirm"}
            ):
                gated_args["confirm"] = True
            else:
                gated_args["confirm"] = False
        return gated_args

