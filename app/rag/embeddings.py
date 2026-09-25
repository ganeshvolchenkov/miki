from __future__ import annotations

from typing import Protocol, Sequence


class EmbeddingProvider(Protocol):
    """What the RAG layer needs from an embedding backend.

    Deliberately minimal so it can be satisfied by ``OpenAIEmbeddingClient``
    (app.brain.embeddings) or by a fake in tests, without either side
    importing the other.
    """

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        ...
