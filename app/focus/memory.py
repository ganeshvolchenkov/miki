"""Focus mode's link to Miki's long-term memory.

Two things are kept:

* one living "Study habits" memory in the memory graph, rewritten in place as the picture sharpens (never a new
  memory per day). If you delete it, Miki respects that and stops; ``/focus habits reset`` starts it again.
* a note per day in the vault's journal folder with the day's rounds and times, so search can answer
  "how long did I study last Tuesday?".

Everything here is best-effort: focus mode never fails because memory is unavailable.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

from app.focus import habits as habit_module
from app.focus.recap import day_facts, recap_text
from app.memory.memory import utc_now

logger = logging.getLogger(__name__)

HABIT_TITLE = "Study habits"


class FocusMemory:
    def __init__(self, manager: Any, session: Any) -> None:
        self.manager = manager  # a MemoryManager (or anything with get/create/update_memory and .memory_store)
        self.session = session  # a FocusSession: history and the bookkeeping of the habit memory

    # ------------------------------------------------------------------ the habits memory
    def habits(self, today: date | None = None) -> habit_module.Habits:
        return habit_module.analyse(self.session.history(), today)

    def update_habits(self, today: date | None = None) -> Any | None:
        """Create the study-habits memory, or rewrite it if the numbers changed. Returns the memory (or None)."""
        if self.manager is None:
            return None
        content = habit_module.memory_text(self.habits(today))
        state = self.session.habit_state()
        if not content or state.get("stopped"):
            return None
        memory_id = state.get("id")
        if memory_id:
            memory = self.manager.get_memory(memory_id)
            if memory is None or not getattr(memory, "active", True):
                self.session.set_habit_state({"stopped": True})  # you removed it: don't bring it back uninvited
                return None
            if memory.content == content:
                return memory
            return self.manager.update_memory(memory_id, content=content, last_confirmed_at=utc_now())
        memory = self.manager.create_memory(content, category="habit", memory_type="habit", confidence=0.8, source="focus")
        self.manager.update_memory(
            memory.memory_id, title=HABIT_TITLE, importance=0.6, stability=0.5, usefulness=0.7,
            entities=["Studying"], entity_types={"Studying": "interest"},
        )
        self.session.set_habit_state({"id": memory.memory_id})
        return memory

    def reset(self) -> None:
        """Start the habits memory afresh on the next update (used after you deleted it on purpose)."""
        self.session.set_habit_state({})

    def status(self) -> str:
        state = self.session.habit_state()
        if state.get("stopped"):
            return "I stopped updating my study-habits memory because you removed it. /focus habits reset brings it back."
        if state.get("id"):
            return "I keep a 'Study habits' memory and update it after each round."
        return "I'll start a 'Study habits' memory after your first round."

    # ------------------------------------------------------------------ the day's note
    def write_day_note(self, day: date, usual_minutes: float | None = None) -> Any | None:
        write = getattr(getattr(self.manager, "memory_store", None), "write_focus_note", None)
        if not callable(write):
            return None
        rounds = self.session.rounds_on(day)
        facts = day_facts(rounds)
        if not facts["rounds"]:
            return None
        body = recap_text(day, rounds, usual_minutes=usual_minutes, streak=self.session.streak(day))
        return write(day.isoformat(), minutes=int(round(facts["minutes"])), rounds=facts["rounds"], body=body)

    def record_round(self, day: date | None = None) -> None:
        """After a round: refresh the habits memory and today's note. Never raises."""
        day = day or date.today()
        try:
            self.update_habits(day)
            self.write_day_note(day, usual_minutes=self.habits(day).avg_minutes_per_active_day or None)
        except Exception:
            logger.warning("Could not update the study-habits memory", exc_info=True)
