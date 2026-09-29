"""A goal attached to a focus round: "/focus 50 mins, I will finish lecture 6"."""

from datetime import date, datetime

import pytest

from app.focus.duration import clean_goal, resolve_with_goal
from app.focus.recap import goal_lines, recap_text
from app.focus.session import FocusSession
from app.phone import ui

from app.focus import service as service_module

from .test_service import make, start_and_wait
from .test_session import Clock, T0

NOW = datetime(2026, 9, 28, 13, 20)


@pytest.fixture(autouse=True)
def no_sleeping(monkeypatch):
    monkeypatch.setattr(service_module.time, "sleep", lambda s: None)


# ---------------------------------------------------------------------------------------------- reading the request
@pytest.mark.parametrize("text, minutes, goal", [
    ("50 mins, I will finish lecture 6", 50, "finish lecture 6"),
    ("50 mins, I'll finish lecture 6.", 50, "finish lecture 6"),
    ("50 min - do the practice exam", 50, "do the practice exam"),
    ("1 hour: revise chapter 3", 60, "revise chapter 3"),
    ("1h30; write the intro", 90, "write the intro"),
    ("half an hour, to read the paper", 30, "read the paper"),
    ("45 revise chapter 3", 45, "revise chapter 3"),
    ("2 hours and finish the essay", 120, "finish the essay"),
    ("1.5 hours, lecture 6", 90, "lecture 6"),
    ("1,5 hours, lecture 6", 90, "lecture 6"),
    ("1:30, lecture 6", 90, "lecture 6"),
])
def test_length_and_goal_are_told_apart(text, minutes, goal):
    duration, got = resolve_with_goal(text, None, NOW)
    assert duration.ok and duration.minutes == minutes and got == goal


@pytest.mark.parametrize("text", ["45", "1 hour", "half an hour", "for 2 hours please", "1h30", "1,5 hours", "1:15"])
def test_plain_lengths_have_no_goal(text):
    duration, goal = resolve_with_goal(text, None, NOW)
    assert duration.ok and goal == ""


def test_a_goal_after_a_length_the_ai_reads():
    duration, goal = resolve_with_goal("until 3pm, finish the essay", lambda text, now: 100 if text == "until 3pm" else None, NOW)
    assert duration.minutes == 100 and goal == "finish the essay" and duration.via_ai


def test_the_head_of_a_goal_is_not_sent_to_the_ai_as_a_length():
    asked = []
    duration, goal = resolve_with_goal("finish lecture 6, then the quiz", lambda text, now: asked.append(text), NOW)
    assert not duration.ok and goal == ""
    assert "finish lecture 6" not in asked  # only the whole request is put to the AI, as before goals existed


def test_a_bad_length_with_a_goal_is_refused_not_clamped():
    duration, goal = resolve_with_goal("2 min, finish lecture 6", None, NOW)
    assert not duration.ok and "too short" in duration.error
    duration, _ = resolve_with_goal("5 hours, finish lecture 6", None, NOW)
    assert not duration.ok


def test_a_goal_never_reaches_the_ai_as_part_of_the_length():
    asked = []
    duration, goal = resolve_with_goal("3pm sharp finish the essay", lambda t, n: asked.append(t) or 90, NOW)
    assert goal == "" and duration.ok and asked == ["3pm sharp finish the essay"]  # nothing to split on: it is read as before


def test_clean_goal():
    assert clean_goal("  I will   finish lecture 6. ") == "finish lecture 6"
    assert clean_goal("and I want to read chapter 2!") == "read chapter 2"
    assert clean_goal("today's exercises") == "today's exercises"  # "to" inside a word is not a lead-in
    assert clean_goal("software design") == "software design"
    assert clean_goal("") == ""
    assert len(clean_goal("x" * 500)) == 120


# ---------------------------------------------------------------------------------------------- the session
@pytest.fixture
def session(tmp_path):
    clock = Clock()
    return FocusSession(tmp_path / "focus.json", clock=clock), clock


def test_the_goal_is_kept_with_the_round_and_survives_a_restart(session, tmp_path):
    s, clock = session
    s.start(50, goal="finish lecture 6")
    assert s.round.goal == "finish lecture 6" and s.round.goal_done is None
    assert FocusSession(tmp_path / "focus.json", clock=clock).round.goal == "finish lecture 6"


def test_old_state_files_without_a_goal_still_load(tmp_path):
    (tmp_path / "focus.json").write_text('{"phase": "focus", "round": {"id": "ab", "started_at": 1.0, "ends_at": 9e12, "planned_minutes": 60}}')
    s = FocusSession(tmp_path / "focus.json", clock=Clock())
    assert s.round.goal == "" and s.is_focusing


def test_the_goal_lands_in_the_history_with_its_answer(session):
    s, clock = session
    s.start(50, goal="finish lecture 6")
    clock.advance(51)
    s.tick()
    entry = s.history()[-1]
    assert entry["goal"] == "finish lecture 6" and entry["goal_done"] is None and entry["id"]
    assert s.set_goal_result(True) == "finish lecture 6"  # answered during the break
    assert s.history()[-1]["goal_done"] is True and s.round.goal_done is True


def test_answering_after_the_break_is_filed_under_that_round(session):
    s, clock = session
    s.start(50, goal="finish lecture 6")
    clock.advance(51)
    s.tick()
    clock.advance(10)
    s.tick()  # the break is over
    assert not s.is_active
    assert s.set_goal_result(False) == "finish lecture 6"
    assert s.history()[-1]["goal_done"] is False


def test_a_stale_goal_can_no_longer_be_answered(session):
    s, clock = session
    s.start(50, goal="finish lecture 6")
    clock.advance(51)
    s.tick()
    clock.advance(24 * 60)
    s.tick()
    assert s.set_goal_result(True) is None


def test_nothing_to_answer_without_a_goal(session):
    s, clock = session
    s.start(50)
    assert s.set_goal_result(True) is None
    clock.advance(51)
    s.tick()
    assert s.set_goal_result(True) is None


def test_more_time_after_a_break_keeps_an_unreached_goal_only(session):
    s, clock = session
    s.start(50, goal="finish lecture 6")
    clock.advance(51)
    s.tick()
    assert s.extend(15).goal == "finish lecture 6"  # not answered yet: still the goal
    clock.advance(16)
    s.tick()
    s.set_goal_result(True)
    assert s.extend(15).goal == ""  # reached: the next stretch starts fresh


def test_set_goal_needs_a_running_round(session):
    s, clock = session
    assert not s.set_goal("x")
    s.start(50)
    assert s.set_goal("finish lecture 6") and s.round.goal == "finish lecture 6"


def test_stopping_early_still_files_the_goal(session):
    s, clock = session
    s.start(50, goal="finish lecture 6")
    clock.advance(20)
    summary = s.stop()
    assert summary["goal"] == "finish lecture 6" and s.history()[-1]["goal"] == "finish lecture 6"


# ---------------------------------------------------------------------------------------------- the recap
def test_recap_lists_the_goals_and_whether_they_were_reached():
    rounds = [
        {"start": T0, "end": T0 + 3000, "minutes": 50, "goal": "finish lecture 6", "goal_done": True},
        {"start": T0 + 4000, "end": T0 + 7000, "minutes": 50, "goal": "do the quiz", "goal_done": False},
        {"start": T0 + 8000, "end": T0 + 9000, "minutes": 16, "goal": "read notes"},
    ]
    lines = goal_lines(rounds)
    assert lines[0] == "Goals:" and "✓ finish lecture 6" in lines[1] and "✗ do the quiz" in lines[2] and "· read notes" in lines[3]
    assert lines[-1] == "1 of 3 reached."
    assert "Goals:" in recap_text(date(2026, 9, 28), rounds)


def test_recap_without_goals_is_unchanged():
    assert goal_lines([{"start": T0, "end": T0 + 3000, "minutes": 50}]) == []
    assert "Goals" not in recap_text(date(2026, 9, 28), [{"start": T0, "end": T0 + 3000, "minutes": 50}])


# ---------------------------------------------------------------------------------------------- the service
def test_focus_with_a_goal_end_to_end(tmp_path):
    svc, desktop, chrome, pet, clock, events = make(tmp_path)
    reply = svc.start_from_text("50 mins, I will finish lecture 6")
    assert reply.ok and "50 min" in reply.text and "Goal: finish lecture 6" in reply.text
    assert svc._armed.wait(5)
    assert "finish lecture 6" in svc.status_text() and svc.view()["goal"] == "finish lecture 6"
    assert any(call[0] in {"say", "info"} and "finish lecture 6" in call[1] for call in pet.calls)  # the pet mentions it

    clock.advance(51)
    svc.tick()
    assert events[-1].kind == "time_up" and "finish lecture 6" in events[-1].text and "/focus done" in events[-1].text

    assert svc.command("done").startswith("Nice")
    assert svc.session.history()[-1]["goal_done"] is True
    assert svc.view()["goal_done"] is True
    assert "✓ finish lecture 6" in svc.today_text()


def test_not_yet_then_more_time_keeps_the_goal(tmp_path):
    svc, *_rest, clock, events = make(tmp_path)
    svc.start_from_text("50 min, finish lecture 6")
    assert svc._armed.wait(5)
    clock.advance(51)
    svc.tick()
    assert "isn't finished yet" in svc.command("notyet")
    assert svc.extend(15).ok and svc._armed.wait(5)
    assert svc.session.round.goal == "finish lecture 6"


def test_goal_command_sets_the_goal_mid_round(tmp_path):
    svc, *_ = make(tmp_path)
    assert not svc.command("goal finish lecture 6").startswith("Goal set")  # no round yet
    start_and_wait(svc, 50)
    assert svc.command("goal I will finish lecture 6") == "Goal set: finish lecture 6"
    assert svc.command("goal").startswith("What's the goal")


def test_done_without_a_goal_says_so(tmp_path):
    svc, *_ = make(tmp_path)
    start_and_wait(svc, 50)
    assert "no goal" in svc.command("done")


def test_stopping_early_asks_about_the_goal(tmp_path):
    svc, desktop, chrome, pet, clock, events = make(tmp_path)
    svc.start_from_text("50 min, finish lecture 6")
    assert svc._armed.wait(5)
    clock.advance(20)
    assert "Did you finish “finish lecture 6”?" in svc.stop().text


# ---------------------------------------------------------------------------------------------- the phone
def test_break_push_asks_about_the_goal_with_buttons():
    text = "Time for a break! You focused for 50 min.\nGoal check: “finish lecture 6”."
    asked = ui.focus_event_screen("time_up", text, 60, ask_goal=True)
    assert [b["callback_data"] for b in asked.buttons[0]] == ["fc:goal:yes", "fc:goal:no"]
    plain = ui.focus_event_screen("time_up", text, 60)
    assert all(b["callback_data"] not in {"fc:goal:yes", "fc:goal:no"} for row in plain.buttons for b in row)


def test_focus_screen_shows_the_goal():
    view = {"phase": "focus", "minutes_left": 30, "until": "15:00", "distractions": 0, "today_minutes": 20, "streak": 0, "goal": "finish lecture 6"}
    assert "finish lecture 6" in ui.focus_screen(view).text
