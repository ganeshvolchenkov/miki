import json
from pathlib import Path

import pytest

from app.core.assistant import MikiCore
from app.core.config import Settings
from app.memory.conversation import JSONConversationStore


def test_settings_loads_from_environment(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("MIKI_MODEL", "gpt-4o-mini")
    monkeypatch.setenv("MIKI_ENV", "testing")

    settings = Settings.from_env()

    assert settings.openai_api_key == "test-key"
    assert settings.model == "gpt-4o-mini"
    assert settings.environment == "testing"


def test_settings_requires_api_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("MIKI_MODEL", "gpt-4o-mini")
    monkeypatch.setenv("MIKI_ENV", "testing")

    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        Settings.from_env()


def test_conversation_store_persists_messages(tmp_path):
    store = JSONConversationStore(storage_dir=tmp_path)

    store.append_turn("hello", "hi there")

    data = json.loads((tmp_path / "conversation.json").read_text(encoding="utf-8"))
    assert len(data["messages"]) == 2
    assert data["messages"][0]["role"] == "user"
    assert data["messages"][1]["role"] == "assistant"


def test_miki_core_processes_input_without_openai(monkeypatch):
    class FakeBrain:
        def __init__(self):
            self.calls = []

        def generate_response(self, user_input: str, system_prompt: str, history: list[dict[str, str]] | None = None) -> str:
            self.calls.append(user_input)
            return "Test response"

    store = JSONConversationStore(storage_dir=Path("data/conversations"))
    core = MikiCore(brain=FakeBrain(), conversation_store=store)

    response = core.process_user_input("hello")

    assert response == "Test response"
    assert len(store.read_messages()) >= 2
