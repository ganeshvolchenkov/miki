import json

import pytest

from app.focus.session import BREAK, FOCUS, IDLE, FocusSession

T0 = 1_800_000_000.0  # a fixed "now" (mid-afternoon UTC; the exact day doesn't matter here)


class Clock:
    def __init__(self, now=T0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, minutes):
        self.now += minutes * 60


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def session(tmp_path, clock):
    return FocusSession(tmp_path / "focus.json", clock=clock)


def test_starts_idle(session):
    assert session.phase == IDLE and not session.is_active and session.seconds_left() == 0


def test_start_runs_the_clock(session, clock):
    r = session.start(60)
    assert session.phase == FOCUS and session.is_focusing
    assert r.planned_minutes == 60
    assert session.seconds_left() == 3600
    clock.advance(20)
    assert session.seconds_left() == 2400


def test_cannot_start_twice(session):
    session.start(60)
    with pytest.raises(ValueError):
        session.start(30)


def test_minutes_are_clamped(session):
    assert session.start(1).planned_minutes == 5
    session.stop()
    assert session.start(9999).planned_minutes == 240


def test_warns_ten_minutes_before_the_end_once(session, clock):
    session.start(60)
    clock.advance(49)
    assert session.tick() == []
    clock.advance(1.5)
    events = session.tick()
    assert [e.kind for e in events] == ["warn"] and events[0].minutes_left in (9, 10)
    assert session.tick() == []  # only once


def test_short_rounds_do_not_warn(session, clock):
    session.start(15)
    clock.advance(10)
    assert session.tick() == []


def test_time_up_starts_the_break(session, clock):
    session.start(60)
    session.record_distraction("youtube.com")
    clock.advance(60)
    events = session.tick(break_minutes=5)
    assert [e.kind for e in events] == ["time_up"]
    summary = events[0].summary
    assert summary["minutes"] == 60 and summary["distractions"] == 1 and summary["completed"]
    assert session.phase == BREAK and session.seconds_left() == 300
    assert not session.is_focusing  # the lock is off during the break


def test_break_ends_with_one_notice(session, clock):
    session.start(60)
    clock.advance(60)
    session.tick(5)
    clock.advance(5)
    events = session.tick(5)
    assert [e.kind for e in events] == ["break_over"]
    assert session.phase == IDLE
    assert session.tick(5) == []


def test_a_round_that_ended_long_ago_is_closed_quietly(tmp_path, clock):
    path = tmp_path / "focus.json"
    FocusSession(path, clock=clock).start(60)
    clock.advance(60 + 45)  # Miki was off for 45 minutes after the end
    later = FocusSession(path, clock=clock)
    assert [e.kind for e in later.tick()] == ["expired"]
    assert later.phase == IDLE


def test_extend_adds_time_and_rearms_the_warning(session, clock):
    session.start(60)
    clock.advance(55)
    session.tick()
    r = session.extend(15)
    assert r.planned_minutes == 75
    assert session.seconds_left() == (20 * 60)


def test_extend_from_a_break_starts_a_new_lock(session, clock):
    session.start(60)
    clock.advance(60)
    session.tick(5)
    session.extend(15)
    assert session.phase == FOCUS and session.seconds_left() == 15 * 60


def test_extend_when_idle_is_an_error(session):
    with pytest.raises(ValueError):
        session.extend(15)


def test_stop_summarises_and_records(session, clock):
    session.start(60)
    clock.advance(30)
    session.record_distraction("discord")
    summary = session.stop()
    assert summary["minutes"] == 30 and not summary["completed"] and summary["distractions"] == 1
    assert session.phase == IDLE
    assert session.stop() == {}
    assert session.stats()["today_minutes"] == 30


def test_distractions_only_count_while_focusing(session, clock):
    assert session.record_distraction("x") == 0
    session.start(60)
    assert session.record_distraction("a") == 1
    assert session.record_distraction("a") == 2
    assert session.round.blocked == {"a": 2}
    clock.advance(60)
    session.tick()
    assert session.record_distraction("b") == 2  # on break: nothing to bounce


def test_state_survives_a_restart(tmp_path, clock):
    path = tmp_path / "focus.json"
    first = FocusSession(path, clock=clock)
    first.start(60)
    first.record_distraction("youtube.com")
    clock.advance(10)
    again = FocusSession(path, clock=clock)
    assert again.is_focusing and again.seconds_left() == 50 * 60
    assert again.round.distractions == 1


def test_corrupt_state_file_means_idle(tmp_path, clock):
    path = tmp_path / "focus.json"
    path.write_text("{not json", encoding="utf-8")
    assert FocusSession(path, clock=clock).phase == IDLE
    path.write_text(json.dumps({"phase": "focus", "round": None}), encoding="utf-8")
    assert FocusSession(path, clock=clock).phase == IDLE


def test_streak_counts_consecutive_days(tmp_path, clock):
    session = FocusSession(tmp_path / "f.json", clock=clock)
    for _ in range(3):  # three days in a row, 30 minutes each
        session.start(30)
        clock.advance(30)
        session.tick()
        clock.advance(5)
        session.tick()
        clock.advance(24 * 60 - 35)
    assert session.streak() == 3
    clock.advance(2 * 24 * 60)  # skipped two days
    assert session.streak() == 0


def test_ban_list_changes_are_stored(tmp_path, clock):
    session = FocusSession(tmp_path / "f.json", clock=clock)
    session.set_banned_changes(["tiktok.com", "tiktok.com", "code.exe"], ["reddit.com"])
    again = FocusSession(tmp_path / "f.json", clock=clock)
    assert again.banned_added() == ("tiktok.com", "code.exe") and again.banned_removed() == ("reddit.com",)
    session.start(30)  # the list survives sessions starting and ending
    session.stop()
    assert session.banned_added() == ("tiktok.com", "code.exe")
