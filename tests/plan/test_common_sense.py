"""The silly things /plan used to do, each as a test: the AI's answer is faked, the common sense is real."""

from datetime import datetime

from app.plan import request as req
from app.plan.schedule import MEAL, STUDY

from .fakes import FakeCalendar, make


def reply(*items, **extra):
    return {"day": "today", "items": list(items), "learn": {}, **extra}


def blocks(service):
    return service.store.get("draft")["blocks"]


def titles(service):
    return [b["title"] for b in blocks(service) if b["kind"] != "break"]


def test_a_fixed_lunch_keeps_a_real_length_instead_of_zero_minutes(tmp_path):
    service, *_ = make(tmp_path, reply({"kind": "study", "title": "Physics", "minutes": 120}, {"kind": "meal", "title": "Lunch", "at": "13:00"},
                                       {"kind": "activity", "title": "Call mom", "estimate": 15}), at=datetime(2026, 10, 1, 9, 0))
    service.make("physics, lunch at 1, call mom")
    lunch = next(b for b in blocks(service) if b["title"] == "Lunch")
    assert (lunch["start"], lunch["end"]) == (13 * 60, 13 * 60 + 45)
    mom = next(b for b in blocks(service) if b["title"] == "Call mom")
    assert mom["end"] - mom["start"] == 15  # the AI's estimate, not a blanket hour


def test_the_ais_estimate_is_used_but_what_you_taught_miki_wins(tmp_path):
    service, *_ = make(tmp_path, reply({"kind": "activity", "title": "Gym", "place": "gym", "estimate": 30}), at=datetime(2026, 10, 1, 9, 0))
    service.make("gym")
    gym = next(b for b in blocks(service) if b["title"] == "Gym")
    assert gym["end"] - gym["start"] == 75  # "1h15 at the gym" was taught


def test_dinner_is_at_home_and_not_before_evening(tmp_path):
    service, *_ = make(tmp_path, reply({"kind": "study", "title": "Study", "minutes": 120}, {"kind": "meal", "title": "Dinner"}),
                       at=datetime(2026, 10, 1, 9, 0))
    service.make("study 2 hours and dinner")
    dinner = next(b for b in blocks(service) if b["title"] == "Dinner")
    assert dinner["place"] == "home" and dinner["start"] >= 17 * 60 + 30


def test_breakfast_comes_first_and_lunch_sits_inside_the_study(tmp_path):
    service, *_ = make(tmp_path, reply({"kind": "meal", "title": "Breakfast", "estimate": 30}, {"kind": "study", "title": "Study", "minutes": 300},
                                       {"kind": "meal", "title": "Lunch", "estimate": 45}), at=datetime(2026, 10, 1, 7, 0))
    service.make("breakfast, study 5 hours, lunch")
    order = [(b["title"], b["kind"]) for b in blocks(service) if b["kind"] in {"meal", "study"}]
    assert order[0] == ("Breakfast", MEAL) and ("Lunch", MEAL) in order
    lunch = order.index(("Lunch", MEAL))
    assert order[lunch - 1][1] == STUDY and order[lunch + 1][1] == STUDY


def test_a_long_study_day_gets_a_lunch_unless_you_said_no_lunch(tmp_path):
    study = {"kind": "study", "title": "Study", "minutes": 360}
    service, *_ = make(tmp_path, reply(study), at=datetime(2026, 10, 1, 7, 0))
    text = service.make("study 6 hours").text
    assert "Lunch" in text and "I added a 45 min lunch" in text
    service, *_ = make(tmp_path / "b", reply(study, skip_lunch=True), at=datetime(2026, 10, 1, 7, 0))
    assert "Lunch" not in service.make("study 6 hours, no lunch").text


def test_a_short_study_stays_home_instead_of_travelling_1h40_for_1h(tmp_path):
    service, *_ = make(tmp_path, reply({"kind": "study", "title": "Study", "minutes": 60}), at=datetime(2026, 10, 1, 7, 0))
    text = service.make("1 hour of study").text
    assert "To school" not in text and "I kept your study at home" in text and 'Say "study at school"' in text


def test_a_long_day_still_goes_to_school(tmp_path):
    service, *_ = make(tmp_path, reply({"kind": "study", "title": "Study", "minutes": 300}), at=datetime(2026, 10, 1, 7, 0))
    assert "To school" in service.make("study 5 hours").text


def test_you_can_insist_on_school_and_it_sticks_through_changes(tmp_path):
    answers = reply({"kind": "study", "title": "Study", "minutes": 60}, study_place="school")
    service, *_ = make(tmp_path, answers, at=datetime(2026, 10, 1, 7, 0))
    assert "To school" in service.make("1 hour of study at school").text
    assert "To school" in service.make("make it 1 hour of study").text  # a change: still school


def test_evening_study_is_at_home(tmp_path):
    service, *_ = make(tmp_path, reply({"kind": "study", "title": "Study", "minutes": 120}), at=datetime(2026, 10, 1, 18, 30))
    text = service.make("study 2 hours tonight").text
    assert "To school" not in text and "it's evening" in text


def test_school_is_dropped_when_the_trip_would_squeeze_the_evening(tmp_path):
    answers = reply({"kind": "study", "title": "Study", "minutes": 180}, {"kind": "fixed", "title": "Dinner", "place": "home", "at": "19:00"})
    service, *_ = make(tmp_path, answers, at=datetime(2026, 10, 1, 14, 20))
    text = service.make("3 hours of study then dinner at 7").text
    assert "I kept your study at home" in text and "To school" not in text and "This doesn't fit" not in text


def test_a_lecture_at_school_means_a_trip_to_school_before_it(tmp_path):
    lecture = {"id": "x", "title": "Linear Algebra Lecture", "start": "2026-10-01T10:00:00", "end": "2026-10-01T12:00:00", "all_day": False}
    service, *_ = make(tmp_path, reply({"kind": "activity", "title": "Gym", "place": "gym", "estimate": 75}),
                       calendar=FakeCalendar([lecture]), at=datetime(2026, 10, 1, 7, 0))
    service.make("gym")
    plan = blocks(service)
    event = next(b for b in plan if b["title"] == "Linear Algebra Lecture")
    assert event["start"] == 10 * 60 and event["place"] == "school"
    assert not service.store.get("draft")["problems"]


def test_the_reader_treats_meals_and_estimates_sensibly():
    request = req.parse({"items": [{"kind": "activity", "title": "Dinner with the team"}, {"kind": "meal", "title": "Breakfast"},
                                   {"kind": "activity", "title": "Run", "estimate": 9999}]})
    assert [(w.kind, w.place) for w in request.wishes][:2] == [("meal", "home"), ("meal", "home")]
    assert request.wishes[2].estimate is None  # absurd guesses are dropped


def test_asks_are_repeatable(tmp_path):
    class Brain:
        def __init__(self):
            self.seen = []

        def generate_response(self, user_input, system_prompt, history=None, temperature=None):
            self.seen.append(temperature)
            return "{}"

    brain = Brain()
    req.make_ai_reader(brain)("x", "y")
    assert brain.seen == [0]
