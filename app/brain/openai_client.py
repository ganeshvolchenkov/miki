from __future__ import annotations

import logging
from typing import Sequence

from openai import OpenAI

logger = logging.getLogger(__name__)


class OpenAIClient:
    """Thin abstraction around the OpenAI Responses API."""

    def __init__(self, api_key: str, model: str) -> None:
        if not api_key:
            raise ValueError("OpenAI API key is required.")

        self.client = OpenAI(api_key=api_key)
        self.model = model

    def generate_response(self, user_input: str, system_prompt: str, history: Sequence[dict[str, str]] | None = None) -> str:
        messages = [{"role": "system", "content": system_prompt}]

        if history:
            messages.extend(history)

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
