"""Core application layer for Miki."""

__all__ = ["MikiCore"]


def __getattr__(name: str):
    # Lazy, so light users of this package (the laptop's hands agent and thin dashboard only need
    # single_instance) don't load the OpenAI client and the whole brain just to start.
    if name == "MikiCore":
        from .assistant import MikiCore

        return MikiCore
    raise AttributeError(name)
