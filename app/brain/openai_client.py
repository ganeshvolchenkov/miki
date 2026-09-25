from __future__ import annotations

import json
import logging
from typing import Any, Sequence

from openai import OpenAI

from app.brain.tool_types import BrainToolTurn, ToolCallRequest

logger = logging.getLogger(__name__)


class OpenAIClient:
    """Thin abstraction around the OpenAI Responses API."""

    def __init__(self, api_key: str, model: str, *, client: OpenAI | None = None) -> None:
        if not api_key:
            raise ValueError("OpenAI API key is required.")

        self.client = client or OpenAI(api_key=api_key)
        self.model = model

    def generate_response(self, user_input: str, system_prompt: str, history: Sequence[dict[str, str]] | None = None) -> str:
        messages = [{"role": "system", "content": system_prompt}]

        if history:
            for item in history:
                cleaned = {"role": item.get("role", "user"), "content": item.get("content", "")}
                if cleaned["role"] in {"user", "assistant", "system"}:
                    messages.append(cleaned)

        messages.append({"role": "user", "content": user_input})

        logger.info("Calling OpenAI model=%s with %d messages", self.model, len(messages))

        try:
            response = self.client.responses.create(
                model=self.model,
                input=messages,
            )
        except Exception:
            logger.exception("OpenAI API request failed")
            raise

        if hasattr(response, "output_text"):
            return response.output_text

        if hasattr(response, "output"):
            collected = []
            for item in response.output:
                if getattr(item, "type", None) == "message":
                    for content in getattr(item, "content", []):
                        if getattr(content, "type", None) == "output_text":
                            collected.append(content.text)
            if collected:
                return "".join(collected)

        raise ValueError("OpenAI response did not include text output.")

    def generate_tool_turn(self, *, input_items: list[dict[str, Any]], tools: list[dict[str, Any]]) -> BrainToolTurn:
        """One round-trip against the Responses API with function-calling
        tools attached. Returns either final text, or one or more requested
        tool calls plus the raw output items needed to continue the
        conversation on the next round (see app/core/tool_runner.py).

        This is additive: `generate_response` above is unchanged and remains
        the only method the Memory Intelligence components use.
        """
        logger.info("Calling OpenAI model=%s with tools (%d tool definitions, %d input items)", self.model, len(tools), len(input_items))

        try:
            response = self.client.responses.create(
                model=self.model,
                input=input_items,
                tools=tools,
            )
        except Exception:
            logger.exception("OpenAI tool-enabled request failed")
            raise

        text_parts: list[str] = []
        tool_calls: list[ToolCallRequest] = []
        output_items: list[dict[str, Any]] = []

        for item in getattr(response, "output", []) or []:
            item_type = getattr(item, "type", None)
            output_items.append(_serialize_output_item(item))

            if item_type == "message":
                for content in getattr(item, "content", []):
                    if getattr(content, "type", None) == "output_text":
                        text_parts.append(content.text)
            elif item_type == "function_call":
                arguments: dict[str, Any] = {}
                raw_arguments = getattr(item, "arguments", None)
                if raw_arguments:
                    try:
                        arguments = json.loads(raw_arguments)
                    except (TypeError, json.JSONDecodeError):
                        logger.warning("Could not parse tool call arguments for %s: %r", getattr(item, "name", "?"), raw_arguments)
                tool_calls.append(ToolCallRequest(call_id=getattr(item, "call_id", ""), name=getattr(item, "name", ""), arguments=arguments))

        text = "".join(text_parts) if text_parts else (getattr(response, "output_text", None) or None)
        return BrainToolTurn(text=text, tool_calls=tool_calls, output_items=output_items)


def _serialize_output_item(item: Any) -> dict[str, Any]:
    if hasattr(item, "model_dump"):
        return item.model_dump(exclude_none=True)
    if isinstance(item, dict):
        return item
    return dict(item)
