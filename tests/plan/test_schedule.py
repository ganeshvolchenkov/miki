from app.plan.schedule import (
    ACTIVITY, BREAK, BUSY, FIXED, FREE, MEAL, STUDY, TRAVEL, Day, Task, hm, plan_day, route_key, span, ways_to_fit,
)

H = 60
ROUTES = {route_key("home", "school"): 50, route_key("home", "gym"): 50, route_key("gym", "school"): 15}


def owner_day(start=8 * H + 30, **kwargs):
    """The owner's example: 5h linear algebra, 3h calculus, 50 min lunch inside it, the gym, home by 8pm for dinner."""
    tasks = [
        Task(STUDY, "Linear algebra", 300),
        Task(STUDY, "Calculus", 180),
        Task(MEAL, "Lunch", 50, during_study=True),
        Task(ACTIVITY, "Gym", 75, place="gym"),
        Task(FIXED, "Dinner", 0, place="home", at=20 * H, ends_day=True),
    ]
    return Day(tasks, start, study_place="school", routes=ROUTES, earliest=start, **kwargs)


def kinds(schedule):
    return [b.kind for b in schedule.blocks]


def test_formatting_helpers():
    assert hm(9 * H + 5) == "09:05"
    assert span(75) == "1h 15m" and span(50) == "50 min" and span(480) == "8h"


def test_owner_day_from_7_fits_with_all_study_and_dinner_last():
    schedule = plan_day(owner_day(7 * H))
    assert schedule.ok and schedule.problems == []
    assert schedule.study_minutes == 480
    assert schedule.blocks[-1].kind == FIXED and schedule.blocks[-1].start == 20 * H
    assert schedule.home_at is not None and schedule.home_at <= 20 * H
    assert schedule.travel == 115  # 50 + 15 + 50: the gym on the way, never an extra trip home


def test_rounds_follow_the_focus_length_and_breaks_sit_between_them():
    schedule = plan_day(owner_day(7 * H))
    study = [b for b in schedule.blocks if b.kind == STUDY]
    assert all(b.minutes == 60 for b in study) and len(study) == 8
    assert [b.title for b in study] == ["Linear algebra"] * 5 + ["Calculus"] * 3
    assert all(b.minutes == 5 for b in schedule.blocks if b.kind == BREAK)


def test_lunch_replaces_the_break_closest_to_lunchtime():
    schedule = plan_day(owner_day(7 * H))
    lunch = next(b for b in schedule.blocks if b.kind == MEAL)
    assert lunch.minutes == 50 and abs(lunch.start - (12 * H + 30)) <= 30
    i = schedule.blocks.index(lunch)
    assert schedule.blocks[i - 1].kind == STUDY and schedule.blocks[i + 1].kind == STUDY


def test_nothing_happens_after_dinner_even_if_that_would_be_on_time():
    """Leaving at 08:30 can't fit; the gym must not be pushed after dinner to make it look on time."""
    schedule = plan_day(owner_day(8 * H + 30))
    assert not schedule.ok
    assert schedule.blocks[-1].kind == FIXED
    assert schedule.late == 60 and "late for dinner at 20:00" in schedule.problems[0]


def test_ways_to_fit_are_checked_and_concrete():
    day = owner_day(8 * H + 30)
    ways = [text for text, result in ways_to_fit(day, plan_day(day)) if result.ok]
    assert any(w.startswith("Study 7h instead of 8h: home by") for w in ways)
    assert any(w.startswith("Skip gym: home by 19:30") for w in ways)
    assert not any("lunch" in w for w in ways)  # 30 min lunch saves only 20: not enough, so not offered


def test_starting_earlier_is_offered_when_there_is_time():
    day = owner_day(8 * H + 30)
    day.earliest = 6 * H
    ways = [text for text, _ in ways_to_fit(day, plan_day(day))]
    assert ways[0].startswith("Start at 07:30 instead of 08:30")


def test_calendar_events_are_left_alone_and_study_flows_around_them():
    day = Day([Task(STUDY, "Calculus", 180)], 9 * H, busy=[(10 * H + 30, 12 * H, "Lecture")])
    schedule = plan_day(day)
    busy = next(b for b in schedule.blocks if b.kind == BUSY)
    assert (busy.start, busy.end, busy.title) == (10 * H + 30, 12 * H, "Lecture")
    for block in schedule.blocks:
        if block.kind == STUDY:
            assert block.end <= 10 * H + 30 or block.start >= 12 * H
    assert schedule.study_minutes == 180


def test_unknown_trips_are_guessed_and_reported():
    day = Day([Task(ACTIVITY, "Groceries", 30, place="supermarket")], 17 * H)
    schedule = plan_day(day)
    assert schedule.guessed_routes == [("home", "supermarket")]
    assert [b.kind for b in schedule.blocks] == [TRAVEL, ACTIVITY, TRAVEL]


def test_a_fixed_time_is_kept_and_free_time_is_shown():
    day = Day([Task(FIXED, "Dentist", 45, place="dentist", at=15 * H)], 9 * H, routes={route_key("home", "dentist"): 20})
    schedule = plan_day(day)
    dentist = next(b for b in schedule.blocks if b.kind == FIXED)
    assert dentist.start == 15 * H and schedule.ok
    assert FREE in kinds(schedule)


def test_after_and_before_windows():
    day = Day([Task(ACTIVITY, "Run", 45, after=17 * H)], 9 * H)
    assert next(b for b in plan_day(day).blocks if b.kind == ACTIVITY).start == 17 * H
    late = plan_day(Day([Task(STUDY, "Essay", 240), Task(ACTIVITY, "Call", 30, before=10 * H)], 9 * H + 45))
    assert not late.ok and "after 10:00" in late.problems[0]


def test_a_day_past_midnight_is_late():
    schedule = plan_day(Day([Task(STUDY, "Study", 180)], 22 * H))
    assert not schedule.ok and "after midnight" in schedule.problems[0]


def test_a_fixed_time_can_sit_in_the_middle_of_the_study():
    """4 hours of study at school from 09:00 and a 13:00 meeting there: study, meeting, more study, one trip each way."""
    routes = {route_key("home", "school"): 30}
    tasks = [Task(STUDY, "Physics", 240), Task(FIXED, "Meeting", 60, place="school", at=13 * H)]
    schedule = plan_day(Day(tasks, 9 * H, study_place="school", routes=routes))
    assert schedule.ok and schedule.travel == 60
    meeting = next(b for b in schedule.blocks if b.kind == FIXED)
    assert any(b.kind == STUDY and b.start >= meeting.end for b in schedule.blocks)


def test_gym_after_studying_comes_after_all_of_it():
    day = owner_day(7 * H)
    day.tasks = [t if t.kind != ACTIVITY else Task(ACTIVITY, "Gym", 75, place="gym", after_study=True) for t in day.tasks]
    schedule = plan_day(day)
    gym = next(b for b in schedule.blocks if b.kind == ACTIVITY)
    assert schedule.ok and all(b.end <= gym.start for b in schedule.blocks if b.kind == STUDY)
    assert schedule.home_at == 19 * H + 30  # 07:50 school ... 17:10, gym 17:25-18:40, home 19:30


def test_gym_before_studying_comes_first():
    tasks = [Task(STUDY, "Calculus", 120), Task(ACTIVITY, "Gym", 60, place="gym", before_study=True)]
    schedule = plan_day(Day(tasks, 9 * H, routes={route_key("home", "gym"): 10}))
    assert next(b for b in schedule.blocks if b.kind != TRAVEL).kind == ACTIVITY


def test_empty_day():
    schedule = plan_day(Day([], 9 * H))
    assert schedule.blocks == [] and schedule.ok
