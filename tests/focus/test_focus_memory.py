"""The study-habits memory against Miki's real memory manager and a real Obsidian vault (in a temp folder)."""

from datetime import date, datetime, timedelta

import pytest

from app.core.memory_manager import MemoryManager
from app.focus.memory import FocusMemory
from app.focus.session import FocusSession
from app.memory.obsidian import ObsidianMemoryStore

from .test_recap_habits import DAY, morning_history, rnd


@pytest.fixture
def session(tmp_path):
    s = FocusSession(tmp_path / "focus.json", clock=lambda: datetime(2026, 9, 28, 20, 0).timestamp())
    s._data["history"] = morning_history()
    return s


@pytest.fixture
def manager(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    return MemoryManager(ObsidianMemoryStore(vault), brain=None)


def test_the_first_update_creates_one_memory_in_the_graph(session, manager):
    memory = FocusMemory(manager, session).update_habits(DAY)
    stored = manager.get_memory(memory.memory_id)
    assert stored.category == "habit" and stored.memory_type == "habit" and stored.source == "focus"
    assert stored.title == "Study habits" and stored.entities == ["Studying"]
    assert "timed focus sessions" in stored.content and "morning" in stored.content
    assert manager.count_memories() == 1
    labels = [n.get("label") for n in manager.graph_snapshot()["nodes"]]
    assert "Study habits" in labels and "Studying" in labels  # it is part of the linked brain, not a loose note


def test_later_updates_rewrite_it_in_place(session, manager):
    fm = FocusMemory(manager, session)
    first = fm.update_habits(DAY)
    assert fm.update_habits(DAY).memory_id == first.memory_id and manager.count_memories() == 1  # nothing changed: nothing rewritten
    session._data["history"].append(rnd(DAY, 20, 0, 30.0))
    again = fm.update_habits(DAY)
    assert again.memory_id == first.memory_id and manager.count_memories() == 1
    assert again.content != first.content


def test_nothing_is_created_before_the_first_round(tmp_path, manager):
    s = FocusSession(tmp_path / "f.json")
    assert FocusMemory(manager, s).update_habits(DAY) is None
    assert manager.count_memories() == 0


def test_if_you_delete_it_miki_does_not_bring_it_back(session, manager):
    fm = FocusMemory(manager, session)
    memory = fm.update_habits(DAY)
    manager.delete_memory(memory.memory_id)
    assert fm.update_habits(DAY) is None and manager.count_memories() == 0
    assert "you removed it" in fm.status()
    session._data["history"].append(rnd(DAY, 20))
    assert fm.update_habits(DAY) is None  # still respected after more sessions


def test_reset_starts_a_fresh_memory(session, manager):
    fm = FocusMemory(manager, session)
    old = fm.update_habits(DAY)
    manager.delete_memory(old.memory_id)
    fm.update_habits(DAY)
    fm.reset()
    fresh = fm.update_habits(DAY)
    assert fresh is not None and fresh.memory_id != old.memory_id and manager.count_memories() == 1


def test_a_day_note_is_written_to_the_vault_journal(session, manager, tmp_path):
    path = FocusMemory(manager, session).write_day_note(DAY, usual_minutes=90)
    assert path.name == "2026-09-28 focus.md" and path.parent.name == "Journal"
    text = path.read_text(encoding="utf-8")
    assert "type: focus" in text and "focus_minutes: 120" in text and "rounds: 2" in text
    assert "Started 09:30, finished 11:30." in text and "09:30–10:30" in text


def test_the_day_note_links_to_the_conversation_journal_when_there_is_one(session, manager):
    manager.memory_store.write_journal_note("2026-09-28", summary="Talked about exams.", topics=[], open_loops=[])
    path = FocusMemory(manager, session).write_day_note(DAY)
    assert "[[2026-09-28]]" in path.read_text(encoding="utf-8")


def test_writing_the_focus_note_never_touches_the_conversation_journal(session, manager):
    journal = manager.memory_store.write_journal_note("2026-09-28", summary="Talked about exams.", topics=["exams"], open_loops=[])
    before = journal.read_text(encoding="utf-8")
    FocusMemory(manager, session).write_day_note(DAY)
    assert journal.read_text(encoding="utf-8") == before
    assert manager.memory_store.journal_exists("2026-09-28")


def test_no_note_for_a_day_without_rounds(session, manager):
    assert FocusMemory(manager, session).write_day_note(DAY + timedelta(days=3)) is None


def test_a_store_without_notes_and_a_missing_manager_are_fine(session):
    class Bare:
        memory_store = object()

        def create_memory(self, *a, **k):
            raise RuntimeError("boom")

    assert FocusMemory(Bare(), session).write_day_note(DAY) is None
    FocusMemory(Bare(), session).record_round(DAY)  # swallows the failure: focus never breaks because memory did
    assert FocusMemory(None, session).update_habits(DAY) is None
    FocusMemory(None, session).record_round(DAY)
