"""Retrieval-Augmented Generation layer for Miki.

This package is intentionally decoupled from ``app.core`` (MikiCore) and
``app.brain`` (the chat model). It knows nothing about Memory, Obsidian, or
conversation storage formats directly -- it only operates on the common
``KnowledgeDocument`` representation produced by the loaders in
``app.rag.loaders``.
"""
