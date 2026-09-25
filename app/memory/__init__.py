"""Memory package for Miki.

``MemoryGraph`` and ``VaultManager`` pull in networkx and PyYAML (~12 MB), and only the Obsidian
graph viewer / CLI use them, so they are loaded on first access instead of at import time.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .conversation import JSONConversationStore
from .memory import JSONMemoryStore, Memory, MemoryStore
from .models import MemoryEdge, MemoryNode
from .obsidian import ObsidianMemoryStore

if TYPE_CHECKING:  # pragma: no cover
    from .graph import MemoryGraph
    from .vault import VaultManager

_LAZY = {"MemoryGraph": ".graph", "VaultManager": ".vault"}


def __getattr__(name: str):
    module_name = _LAZY.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    value = getattr(import_module(module_name, __name__), name)
    globals()[name] = value
    return value


__all__ = [
    "JSONConversationStore",
    "JSONMemoryStore",
    "Memory",
    "MemoryStore",
    "ObsidianMemoryStore",
    "MemoryEdge",
    "MemoryGraph",
    "MemoryNode",
    "VaultManager",
]
