"""Stand-ins for the AI, the calendar tool and the focus service. Nothing here touches the network or real data."""

from datetime import datetime
from types import SimpleNamespace

from app.plan.service import PlanService
from app.plan.store import PlanStore


class FakeAI:
    """Answers every request with ``reply`` (a dict, or a callable of the message) and remembers the prompts."""

    def __init__(self, reply):
        self.reply = reply
        self.calls = []

    def __call__(self, text, prompt):
        self.calls.append((text, prompt))
        return self.reply(text) if callable(self.reply) else self.reply


class FakeCalendar:
    def __init__(self, events=None, available=True):
        self.events = list(events or [])  # what was already there (get_events returns these)
        self.created = []
        self.deleted = []
        self.available = available
        self._next = 0

    def is_available(self):
        return self.available

    def execute(self, operation, arguments):
        if operation == "get_events":
            return SimpleNamespace(success=True, data={"events": self.events + [dict(e) for e in self.created if e["id"] not in self.deleted]})
        if operation == "create_event":
            assert arguments["confirm"] is True
            self._next += 1
            event = {"id": f"ev{self._next}", "title": arguments["title"], "start": arguments["start"], "end": arguments["end"],
                     "description": arguments["description"], "all_day": False}
            self.created.append(event)
            return SimpleNamespace(success=True, data={"event": event})
        if operation == "delete_event":
            assert arguments["confirm"] is True
            self.deleted.append(arguments["event_id"])
            return SimpleNamespace(success=True, data={})
        raise AssertionError(operation)


class FakeFocus:
    def __init__(self, minutes=60, break_minutes=5):
        self.config = SimpleNamespace(minutes=minutes, break_minutes=break_minutes)
        self.is_focusing = False
        self.started = []

    def start_focus(self, minutes=None, goal=""):
        self.started.append((minutes, goal))
        self.is_focusing = True
        return SimpleNamespace(ok=True, text=f"Focus mode is on for {minutes} min. Goal: {goal}")


class Clock:
    def __init__(self, when: datetime):
        self.now = when.timestamp()

    def __call__(self):
        return self.now

    def set(self, hour, minute=0):
        moment = datetime.fromtimestamp(self.now).replace(hour=hour, minute=minute, second=0)
        self.now = moment.timestamp()


OWNER_JSON = {
    "day": "today",
    "items": [
        {"kind": "study", "title": "Linear algebra", "minutes": 300},
        {"kind": "study", "title": "Calculus", "minutes": 180},
        {"kind": "meal", "title": "Lunch", "minutes": 50, "during_study": True},
        {"kind": "activity", "title": "Gym", "place": "gym", "minutes": None},
        {"kind": "fixed", "title": "Dinner", "place": "home", "at": "20:00", "ends_day": True},
    ],
    "learn": {},
}

OWNER_FACTS = {
    "items": [],
    "learn": {
        "travel": [{"from": "home", "to": "school", "minutes": 50}, {"from": "home", "to": "gym", "minutes": 50},
                   {"from": "gym", "to": "school", "minutes": 15}],
        "durations": {"gym": 75}, "study_place": "school",
    },
}


def make(tmp_path, reply=OWNER_JSON, *, at=datetime(2026, 10, 1, 7, 0), calendar=None, focus=None, taught=True):
    """A plan service on a scratch file, at a fixed time, that already knows the owner's places (unless ``taught=False``)."""
    clock = Clock(at)
    calendar = calendar if calendar is not None else FakeCalendar()
    focus = focus if focus is not None else FakeFocus()
    ai = FakeAI(OWNER_FACTS)
    service = PlanService(PlanStore(tmp_path / "plan.json"), ai=ai, calendar=lambda: calendar, focus=focus, clock=clock, lock_name=None)
    if taught:
        assert service.remember("school and gym are 50 min from home, the gym is 15 from school, 1h15 at the gym, I study at school").ok
    ai.reply = reply
    return service, ai, calendar, focus, clock
