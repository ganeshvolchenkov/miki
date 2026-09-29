import threading
from datetime import date, datetime, timedelta

import pytest

from app.focus import service as service_module
from app.focus.service import FocusConfig

from .test_recap_habits import rnd
from .test_service import make, start_and_wait


@pytest.fixture(autouse=True)
def no_sleeping(monkeypatch):
    monkeypatch.setattr(service_module.time, "sleep", lambda s: None)


class FakeMemory:
    """Stands in for FocusMemory: records when a round was logged."""

    def __init__(self):
        self.days, self.resets, self.done = [], 0, threading.Event()

    def record_round(self, day):
        self.days.append(day)
        self.done.set()

    def reset(self):
        self.resets += 1

    def status(self):
        return "I keep a 'Study habits' memory."


def today_of(svc):
    return datetime.fromtimestamp(svc._clock()).date()


def seed_today(svc, *rounds_spec):
    """Put finished rounds for 'today' (by the service's clock) into the history."""
    day = today_of(svc)
    svc.session._data["history"] = [rnd(day, hour, minute, minutes, **kw) for hour, minute, minutes, kw in rounds_spec]
    return day


# ------------------------------------------------------------------ "/focus 1 hour" and friends
@pytest.mark.parametrize("text, minutes", [
    ("1 hour", 60), ("30 mins", 30), ("1h30", 90), ("half an hour", 30), ("45", 45), ("an hour and a half", 90), ("for 2 hours", 120),
])
def test_start_from_text_reads_natural_durations(tmp_path, text, minutes):
    svc, *_ = make(tmp_path)
    reply = svc.start_from_text(text)
    assert reply.ok, reply.text
    assert svc.session.round.planned_minutes == minutes
    assert "I read that as" not in reply.text  # no AI was involved, so nothing to explain


def test_no_text_means_the_default_round(tmp_path):
    svc, *_ = make(tmp_path)
    assert svc.start_from_text("").ok and svc.session.round.planned_minutes == 60


@pytest.mark.parametrize("text, fragment", [("2", "Did you mean 2 hours"), ("10 hours", "at most 4 hours"), ("banana", "couldn't tell")])
def test_bad_durations_do_not_start_anything(tmp_path, text, fragment):
    svc, *_ = make(tmp_path)
    reply = svc.start_from_text(text)
    assert not reply.ok and fragment in reply.text
    assert not svc.session.is_active


def test_the_ai_reads_odd_durations_and_says_what_it_understood(tmp_path):
    asked = []

    def ai(text, now):
        asked.append((text, now))
        return 100

    svc, *_ = make(tmp_path, ai_minutes=ai)
    reply = svc.start_from_text("until 3pm")
    assert reply.ok and svc.session.round.planned_minutes == 100
    assert reply.text.startswith("I read that as 1h 40m.")
    assert asked[0][0] == "until 3pm" and isinstance(asked[0][1], datetime)


def test_the_ai_is_not_asked_when_the_text_is_plain(tmp_path):
    svc, *_ = make(tmp_path, ai_minutes=lambda text, now: pytest.fail("the AI should not be called"))
    assert svc.start_from_text("1 hour").ok


def test_an_ai_answer_out_of_range_is_refused(tmp_path):
    svc, *_ = make(tmp_path, ai_minutes=lambda text, now: 900)
    reply = svc.start_from_text("all night")
    assert not reply.ok and "at most 4 hours" in reply.text and not svc.session.is_active


def test_typed_commands_understand_durations(tmp_path):
    svc, *_ = make(tmp_path)
    assert "Focus mode is on for 1 hour" in svc.command("1 hour")
    assert svc._armed.wait(5)
    assert "Added time" in svc.command("more 30 mins")
    assert svc.session.round.planned_minutes == 90
    assert "Added time" in svc.command("more")  # no amount: 15 minutes
    assert svc.session.round.planned_minutes == 105
    assert "couldn't tell" in svc.command("more banana")


def test_extend_from_text(tmp_path):
    svc, *_ = make(tmp_path)
    start_and_wait(svc, 30)
    assert svc.extend_from_text("half an hour").ok and svc.session.round.planned_minutes == 60
    assert not svc.extend_from_text("2").ok


# ------------------------------------------------------------------ the end-of-day recap
def at(day, hour, minute=0):
    return datetime(day.year, day.month, day.day, hour, minute)


def test_the_recap_is_due_in_the_evening_only_on_days_you_focused(tmp_path):
    svc, *_ = make(tmp_path)
    day = seed_today(svc, (9, 12, 60.0, {}), (18, 0, 47.0, {}))
    assert not svc.recap_due(at(day, 20, 59))  # before 21:00
    assert svc.recap_due(at(day, 21, 0))
    assert svc.recap_due(at(day, 23, 30))
    assert not svc.recap_due(at(day, 23, 59, ) + timedelta(minutes=2))  # past midnight is another day
    seed_today(svc)  # no rounds today
    assert not svc.recap_due(at(day, 21, 30))


def test_the_recap_window_ends_three_hours_after_its_time(tmp_path):
    config = FocusConfig(state_path=tmp_path / "f.json", recap_time="18:00")
    svc, *_ = make(tmp_path, config=config)
    day = seed_today(svc, (9, 0, 60.0, {}))
    assert svc.recap_due(at(day, 20, 59)) and not svc.recap_due(at(day, 21, 1))  # a stale recap is worse than none


def test_the_recap_is_sent_once_with_start_and_finish_times(tmp_path):
    svc, desktop, chrome, pet, clock, events = make(tmp_path)
    day = seed_today(svc, (9, 12, 60.0, {}), (18, 0, 47.0, {"blocked": {"youtube.com": 2}, "distractions": 2}))
    assert svc.send_recap_if_due(at(day, 21, 5))
    assert [e.kind for e in events] == ["recap"]
    text = events[0].text
    assert "Focus recap" in text and "Focused 1h 47m in 2 rounds." in text
    assert "Started 09:12, finished 18:47." in text and "youtube.com ×2" in text
    assert not svc.send_recap_if_due(at(day, 21, 6)) and len(events) == 1  # once a day
    assert svc.session.recap_sent_on() == day.isoformat()


def test_the_recap_waits_for_a_running_round_to_finish(tmp_path):
    svc, *_ = make(tmp_path)
    start_and_wait(svc, 60)
    day = today_of(svc)
    svc.session._data["history"] = [rnd(day, 9, 0, 60.0)]
    assert not svc.recap_due(at(day, 21, 30))  # still focusing


def test_the_recap_can_be_switched_off(tmp_path):
    config = FocusConfig(state_path=tmp_path / "f.json", recap_time="off")
    svc, *_ = make(tmp_path, config=config)
    day = seed_today(svc, (9, 0, 60.0, {}))
    assert not svc.recap_due(at(day, 21, 30))
    svc.config = FocusConfig(state_path=tmp_path / "f.json", recap_time="banana")
    assert not svc.recap_due(at(day, 21, 30))  # an unreadable time never crashes anything


def test_the_recap_is_checked_by_the_clock_tick_once_a_minute(tmp_path):
    svc, desktop, chrome, pet, clock, events = make(tmp_path)
    day = today_of(svc)
    svc.session._data["history"] = [rnd(day, 9, 0, 60.0)]
    calls = []
    svc.send_recap_if_due = lambda now=None: calls.append(1) or False
    for _ in range(3):
        svc.tick()
    assert len(calls) == 1  # not every second
    clock.advance(1.5)
    svc.tick()
    assert len(calls) == 2


def test_today_text_is_available_on_demand_and_shows_a_running_round(tmp_path):
    svc, *_ = make(tmp_path)
    seed_today(svc, (9, 0, 60.0, {}))
    assert "Started 09:00, finished 10:00." in svc.today_text()
    assert "Started 09:00, finished 10:00." in svc.command("today")
    start_and_wait(svc, 30)
    assert "still running until" in svc.today_text()


def test_stats_show_when_today_started_and_finished(tmp_path):
    svc, *_ = make(tmp_path)
    seed_today(svc, (9, 12, 60.0, {}), (18, 0, 47.0, {}))
    assert "09:12 to 18:47" in svc.stats_text()


def test_the_usual_comparison_needs_a_few_days_of_history(tmp_path):
    svc, *_ = make(tmp_path)
    day = seed_today(svc, (9, 0, 90.0, {}))
    assert "usual" not in svc.today_text()
    history = [rnd(day - timedelta(days=i), 9, 0, 60.0) for i in range(1, 5)]
    svc.session._data["history"] += history
    assert "more than your usual 1 hour" in svc.today_text()


# ------------------------------------------------------------------ memory
def test_finishing_a_round_teaches_the_memory(tmp_path):
    memory = FakeMemory()
    svc, desktop, chrome, pet, clock, events = make(tmp_path, memory=memory)
    start_and_wait(svc)
    clock.advance(60)
    svc.tick()  # time is up
    assert memory.done.wait(5) and memory.days == [today_of(svc)]


def test_stopping_early_teaches_it_too_but_a_cancelled_start_does_not(tmp_path):
    memory = FakeMemory()
    svc, desktop, chrome, pet, clock, events = make(tmp_path, memory=memory)
    start_and_wait(svc)
    svc.stop()  # cancelled straight away: under a minute, nothing to learn
    assert not memory.done.wait(0.3)
    start_and_wait(svc)
    clock.advance(20)
    svc.stop()
    assert memory.done.wait(5)


def test_habits_command_and_reset(tmp_path):
    memory = FakeMemory()
    svc, *_ = make(tmp_path, memory=memory)
    assert "don't have any focus sessions" in svc.command("habits")
    seed_today(svc, (9, 0, 60.0, {}))
    text = svc.command("habits")
    assert "Your study habits" in text and "Study habits' memory" in text
    assert "fresh study-habits memory" in svc.command("habits reset") and memory.resets == 1
    assert memory.done.wait(5)  # a reset refreshes the memory right away


def test_everything_works_without_a_memory(tmp_path):
    svc, *_ = make(tmp_path)
    assert svc.memory is None
    start_and_wait(svc)
    svc.stop()
    assert "don't have any focus sessions" in svc.command("habits")  # nothing to learn from, and no memory to crash
    seed_today(svc, (9, 0, 60.0, {}))
    text = svc.command("habits")
    assert "Your study habits" in text and "Study habits' memory" not in text  # no memory attached: no claim about one


def test_attach_connects_the_brain_and_the_memory(tmp_path):
    class Brain:
        def generate_response(self, user_input, system_prompt, history=None):
            return '{"minutes": 55}'

    class Core:
        brain = Brain()
        memory_manager = object()

    svc, *_ = make(tmp_path)
    svc.attach(Core())
    assert svc.memory is not None and svc.memory.manager is Core.memory_manager
    reply = svc.start_from_text("as long as my next class")
    assert reply.ok and svc.session.round.planned_minutes == 55 and "I read that as 55 min" in reply.text
