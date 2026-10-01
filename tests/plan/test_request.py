from datetime import datetime

from app.plan import request as req
from app.plan.schedule import ACTIVITY, FIXED, MEAL, STUDY


def owner_json():
    return {
        "day": "today", "start": None, "start_place": None, "study_place": None,
        "items": [
            {"kind": "study", "title": "linear algebra", "minutes": 300},
            {"kind": "study", "title": "Calculus", "minutes": 180},
            {"kind": "meal", "title": "Lunch", "minutes": 50, "during_study": True},
            {"kind": "activity", "title": "Gym", "place": "The Gym", "minutes": None},
            {"kind": "fixed", "title": "Dinner", "place": "home", "at": "20:00", "minutes": None, "ends_day": True},
        ],
        "learn": {"travel": [{"from": "home", "to": "school", "minutes": 50}], "durations": {"gym": 75}, "study_place": "school"},
    }


def test_the_owner_request_reads_cleanly():
    request = req.parse(owner_json())
    kinds = [(w.kind, w.title, w.minutes) for w in request.wishes]
    assert kinds == [(STUDY, "Linear algebra", 300), (STUDY, "Calculus", 180), (MEAL, "Lunch", 50), (ACTIVITY, "Gym", None), (FIXED, "Dinner", None)]
    assert request.wishes[2].during_study and request.wishes[3].place == "gym"
    assert request.wishes[4].at == 20 * 60 and request.wishes[4].ends_day
    assert request.facts.travel == [("home", "school", 50)] and request.facts.durations == {"gym": 75}
    assert request.facts.study_place == "school"


def test_garbage_is_not_a_request():
    assert req.parse(None) is None and req.parse("text") is None and req.parse([1]) is None
    assert req.parse({"items": "nope"}).wishes == []


def test_bad_fields_are_dropped_or_clamped():
    request = req.parse({"items": [
        {"kind": "teleport", "title": "x"},
        {"kind": "study", "title": "A" * 200, "minutes": 9999},
        {"kind": "study", "minutes": True},
        {"kind": "activity", "title": "Run", "place": "<script>", "after": "25:00", "before": "18:30"},
        "not a dict",
    ], "day": "someday", "start": "7am"})
    assert len(request.wishes) == 3
    assert request.wishes[0].minutes is None and len(request.wishes[0].title) <= req.MAX_TITLE
    assert request.wishes[1].title == "Study" and request.wishes[1].minutes is None
    run = request.wishes[2]
    assert run.place is None and run.after is None and run.before == 18 * 60 + 30
    assert request.day_offset == 0 and request.start is None


def test_study_is_capped_at_16_hours_in_total():
    request = req.parse({"items": [{"kind": "study", "title": s, "minutes": 600} for s in "ABC"]})
    assert sum(w.minutes for w in request.wishes) == req.MAX_STUDY_MINUTES
    assert len(request.wishes) == 2  # the third had nothing left


def test_a_time_makes_it_fixed_and_no_time_makes_it_loose():
    request = req.parse({"items": [
        {"kind": "activity", "title": "Gym", "at": "18:00"},
        {"kind": "fixed", "title": "Coffee with Sam"},
        {"kind": "meal", "title": "Lunch", "at": "13:00", "during_study": True},
    ]})
    assert [w.kind for w in request.wishes] == [FIXED, ACTIVITY, MEAL]


def test_only_the_latest_fixed_item_can_end_the_day():
    request = req.parse({"items": [
        {"kind": "fixed", "title": "Call", "at": "18:00", "ends_day": True},
        {"kind": "fixed", "title": "Dinner", "at": "20:00", "ends_day": True},
        {"kind": "activity", "title": "Gym", "ends_day": True},
    ]})
    assert [w.ends_day for w in request.wishes] == [False, True, False]


def test_after_and_before_study_round_trip():
    request = req.parse({"items": [{"kind": "activity", "title": "Gym", "after": "study"}, {"kind": "activity", "title": "Run", "before": "Study"},
                                   {"kind": "study", "title": "Maths", "minutes": 60, "after": "study"}]})
    assert [(w.after_study, w.before_study) for w in request.wishes] == [(True, False), (False, True), (False, False)]
    assert req.to_json(request)["items"][0]["after"] == "study"


def test_bad_facts_are_ignored():
    facts = req.parse({"items": [], "learn": {
        "travel": [{"from": "home", "to": "home", "minutes": 5}, {"from": "a", "to": "b", "minutes": 9000}, {"from": "a", "to": "b", "minutes": 12}],
        "durations": {"gym": 75, "": 30, "x": "long"}, "study_place": 42}}).facts
    assert facts.travel == [("a", "b", 12)] and facts.durations == {"gym": 75} and facts.study_place is None


def test_round_trip_to_json_for_changes():
    request = req.parse(owner_json())
    again = req.parse(req.to_json(request))
    assert [(w.kind, w.title, w.minutes, w.at, w.place) for w in again.wishes] == [(w.kind, w.title, w.minutes, w.at, w.place) for w in request.wishes]


def test_prompt_mentions_what_miki_knows_and_the_previous_plan():
    prompt = req.build_prompt(datetime(2026, 10, 1, 8, 30), "home-school 50", "gym 75", "school", {"items": []})
    assert "Thursday 01 October 2026, 08:30" in prompt and "home-school 50" in prompt and "gym 75" in prompt
    assert '"items": []' in prompt


def test_ai_reader_pulls_json_out_of_chatter():
    class Brain:
        def generate_response(self, user_input, system_prompt, history=None):
            return 'Sure! {"items": [{"kind": "study", "minutes": 60}]} hope that helps'

    assert req.make_ai_reader(Brain())("study", "prompt") == {"items": [{"kind": "study", "minutes": 60}]}


def test_ai_reader_survives_a_stray_closing_brace():
    """Seen live from gpt-4.1-nano: one "}" too many at the end."""
    class Brain:
        def generate_response(self, user_input, system_prompt, history=None):
            return '{"items": [], "learn": {"durations": {"gym": 75}}}}'

    assert req.make_ai_reader(Brain())("x", "p") == {"items": [], "learn": {"durations": {"gym": 75}}}
