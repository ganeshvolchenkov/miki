"""Finished rounds must survive closing Miki, a damaged state file, and the 120-day trim."""

import json

from app.focus.session import FocusSession

from .test_goal import no_sleeping  # noqa: F401  (autouse: keeps these tests off the real clock)
import threading

from .test_service import make, start_and_wait
from .test_session import Clock


def _finish_round(session, clock, minutes=50, goal=""):
    session.start(minutes, goal=goal)
    clock.advance(minutes + 1)
    session.tick()


def test_a_finished_round_is_known_after_reopening(tmp_path):
    clock = Clock()
    first = FocusSession(tmp_path / "focus.json", clock=clock)
    _finish_round(first, clock, 50)
    first.stop()  # ends the break: "closing Miki" for the day
    reopened = FocusSession(tmp_path / "focus.json", clock=clock)
    assert reopened.stats()["today_minutes"] == 50 and len(reopened.history()) == 1


def test_three_hours_over_several_rounds_add_up_after_reopening(tmp_path):
    clock = Clock()
    session = FocusSession(tmp_path / "focus.json", clock=clock)
    for _ in range(3):
        _finish_round(session, clock, 60)
        clock.advance(10)
        session.tick()  # break over
    reopened = FocusSession(tmp_path / "focus.json", clock=clock)
    assert reopened.stats()["today_minutes"] == 180 and reopened.stats()["today_rounds"] == 3


def test_every_round_is_also_appended_to_the_log(tmp_path):
    clock = Clock()
    session = FocusSession(tmp_path / "focus.json", clock=clock)
    _finish_round(session, clock, 50, goal="finish lecture 6")
    lines = [json.loads(line) for line in session.log_path.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 1 and lines[0]["minutes"] == 50 and lines[0]["goal"] == "finish lecture 6"
    session.set_goal_result(True)  # the answer is appended as a newer line for the same round
    lines = [json.loads(line) for line in session.log_path.read_text(encoding="utf-8").splitlines()]
    assert [line["id"] for line in lines] == [lines[0]["id"]] * 2 and lines[-1]["goal_done"] is True


def test_a_damaged_state_file_is_restored_from_the_backup(tmp_path):
    clock = Clock()
    session = FocusSession(tmp_path / "focus.json", clock=clock)
    _finish_round(session, clock, 50)
    session.stop()
    (tmp_path / "focus.json").write_text("{ this is not json", encoding="utf-8")
    reopened = FocusSession(tmp_path / "focus.json", clock=clock)
    assert reopened.stats()["today_minutes"] == 50  # not an empty history
    assert (tmp_path / "focus.corrupt").exists()  # the broken file is kept, not overwritten


def test_a_missing_state_file_just_starts_empty(tmp_path):
    session = FocusSession(tmp_path / "nothing" / "focus.json", clock=Clock())
    assert session.history() == [] and not session.is_active


def test_a_stuck_bouncer_cannot_stop_the_timer(tmp_path):
    """A hung call into another program's window used to freeze the one loop that also ends the round."""
    svc, desktop, chrome, pet, clock, events = make(tmp_path)
    start_and_wait(svc, 50)
    release = threading.Event()
    svc.guard.tick = lambda: release.wait(30)  # the bouncer hangs
    stuck = threading.Thread(target=svc.tick_guard, daemon=True)
    stuck.start()
    try:
        clock.advance(51)
        svc.tick_clock()
        assert events[-1].kind == "time_up" and svc.session.phase == "break"
    finally:
        release.set()
        stuck.join(2)


def test_a_round_that_ended_while_miki_was_closed_is_remembered(tmp_path):
    svc, desktop, chrome, pet, clock, events = make(tmp_path)
    saved = []
    svc.memory = type("Memory", (), {"record_round": lambda self, day: saved.append(day)})()
    svc.session.start(50)
    clock.advance(50 + 60)  # Miki was off well past the end of the round
    svc.tick()
    assert svc.session.stats()["today_minutes"] == 50
    for thread in __import__("threading").enumerate():
        if thread.name == "miki-focus-memory":
            thread.join(2)
    assert saved  # the day note and study-habits memory hear about it too


def test_a_hanging_listener_cannot_stop_the_timer(tmp_path, monkeypatch):
    """A Telegram send that never returns (network dropped, laptop just woke up) used to freeze the clock thread."""
    from app.focus import service as service_module

    monkeypatch.setattr(service_module, "LISTENER_WAIT_SECONDS", 0.2)
    svc, desktop, chrome, pet, clock, events = make(tmp_path)
    release = threading.Event()
    svc.add_listener(lambda event: release.wait(30))  # hangs
    ok = []
    svc.add_listener(ok.append)  # a later listener must still hear about it
    start_and_wait(svc, 50)
    try:
        clock.advance(51)
        svc.tick_clock()  # returns even though the first listener never does
        assert svc.session.phase == "break" and [e.kind for e in ok] == ["time_up"]
        clock.advance(6)
        svc.tick_clock()  # and the clock is still alive afterwards
        assert svc.session.phase == "idle" and [e.kind for e in ok] == ["time_up", "break_over"]
    finally:
        release.set()


def test_starting_twice_does_not_double_the_threads(tmp_path):
    def mine():
        return {t for t in threading.enumerate() if t.name in {"miki-focus", "miki-focus-guard"} and t.is_alive()}

    svc, *_ = make(tmp_path)
    before = mine()  # clock threads other tests left running
    assert svc.start()
    added = mine() - before
    assert svc.start()  # a second start changes nothing
    try:
        assert mine() - before == added and sorted(t.name for t in added) == ["miki-focus", "miki-focus-guard"]
    finally:
        svc.close()
