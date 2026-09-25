"""Which OpenAI model Miki uses, and the curated list offered by /model (dashboard and phone)."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

MODEL_PREFERENCE_PATH = Path("data/model.json")

# (model id, tier, note) ordered cheapest -> most expensive.
MODEL_CHOICES: list[tuple[str, str, str]] = [
    ("gpt-4.1-nano", "cheapest", "Fastest and lowest cost"),
    ("gpt-5-nano", "cheapest", "Tiny reasoning model"),
    ("gpt-4o-mini", "cheap", "Default. Solid all-rounder"),
    ("gpt-4.1-mini", "cheap", "Better instruction following"),
    ("gpt-5-mini", "cheap", "Smarter, still inexpensive"),
    ("gpt-4.1", "balanced", "Strong general model"),
    ("gpt-4o", "balanced", "Multimodal flagship"),
    ("gpt-5", "premium", "Most capable, highest cost"),
]


def load_saved_model() -> str | None:
    try:
        return json.loads(MODEL_PREFERENCE_PATH.read_text(encoding="utf-8")).get("model") or None
    except (OSError, ValueError, AttributeError):
        return None


def save_model_preference(model: str) -> None:
    try:
        MODEL_PREFERENCE_PATH.parent.mkdir(parents=True, exist_ok=True)
        MODEL_PREFERENCE_PATH.write_text(json.dumps({"model": model}), encoding="utf-8")
    except OSError:
        logger.warning("Could not save model preference", exc_info=True)


def switch_model(brain: Any, name: str) -> str | None:
    """Validate ``name`` with the API, switch the shared brain to it and remember the choice.

    Returns None on success, or a short human-readable reason on failure (nothing changes then).
    """
    try:
        brain.client.models.retrieve(name)
    except Exception as exc:
        return f"Can't use '{name}': {exc}"
    brain.model = name
    save_model_preference(name)
    return None
