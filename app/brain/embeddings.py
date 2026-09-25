from __future__ import annotations

import logging
from typing import Sequence

from openai import OpenAI

logger = logging.getLogger(__name__)


class OpenAIEmbeddingClient:
    """Thin abstraction around the OpenAI embeddings API.

    Shares the same underlying OpenAI SDK client instance used by the chat
    brain (``OpenAIClient``) rather than creating an unrelated client.
    """

    def __init__(self, client: OpenAI, model: str) -> None:
        if not model:
            raise ValueError("Embedding model is required.")

        self.client = client
        self.model = model

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        items = list(texts)
        if not items:
            return []

        logger.info("Requesting %d embedding(s) with model=%s", len(items), self.model)

        try:
            response = self.client.embeddings.create(model=self.model, input=items)
        except Exception:
            logger.exception("OpenAI embeddings request failed")
            raise

        return [item.embedding for item in response.data]
