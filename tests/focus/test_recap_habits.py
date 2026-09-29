from datetime import date, datetime, timedelta

from app.focus import habits, recap

DAY = date(2026, 9, 28)  # a Monday


def at(day, hour, minute=0):
    return datetime(day.year, day.month, day.day, hour, minute).timestamp()


def rnd(day, hour, minute=0, minutes=60.0, distractions=0, blocked=None, completed=True):
    start = at(day, hour, minute)
    return {"day": day.isoformat(), "start": start, "end": start + minutes * 60, "minutes": minutes, "planned": int(minutes),
            "distractions": distractions, "blocked": blocked or {}, "completed": completed}


# ------------------------------------------------------------------ formatting
def test_format_minutes():
    assert [recap.format_minutes(m) for m in (0, 25, 60, 75, 120, 195.4)] == ["0 min", "25 min", "1 hour", "1h 15m", "2 hours", "3h 15m"]


# ------------------------------------------------------------------ the recap
def test_recap_says_how_long_when_you_started_and_when_you_finished():
    rounds = [rnd(DAY, 9, 12), rnd(DAY, 10, 30, 45), rnd(DAY, 18, 0, 47.0, distractions=3, blocked={"youtube.com": 2, "Discord": 1})]
    text = recap.recap_text(DAY, rounds, usual_minutes=120, streak=4)
    assert text.splitlines()[0] == "Focus recap · Monday 28 September"
    assert "Focused 2h 32m in 3 rounds." in text
    assert "Started 09:12, finished 18:47." in text
    assert "09:12–10:12  1 hour" in text and "10:30–11:15  45 min" in text and "18:00–18:47  47 min" in text
    assert "Bounced 3 distractions (youtube.com ×2, Discord ×1)." in text
    assert "32 min more than your usual 2 hours" in text and "4-day streak." in text


def test_a_single_round_has_no_round_list():
    text = recap.recap_text(DAY, [rnd(DAY, 9)])
    assert "Focused 1 hour in 1 round." in text and "Started 09:00, finished 10:00." in text
    assert "09:00–10:00" not in text and "Zero distractions bounced." in text


def test_comparison_with_your_usual():
    rounds = [rnd(DAY, 9, minutes=120)]
    assert "Right on your usual 1h 55m." in recap.recap_text(DAY, rounds, usual_minutes=115)
    assert "1 hour less than your usual 3 hours." in recap.recap_text(DAY, rounds, usual_minutes=180)
    assert "usual" not in recap.recap_text(DAY, rounds, usual_minutes=None)


def test_a_round_still_running_is_mentioned():
    text = recap.recap_text(DAY, [rnd(DAY, 9)], running_until=at(DAY, 11, 30))
    assert "Started 09:00. A round is still running until 11:30." in text


def test_no_rounds_and_cancelled_instantly_do_not_count():
    assert "No focus rounds today." in recap.recap_text(DAY, [])
    assert "No focus rounds today." in recap.recap_text(DAY, [rnd(DAY, 9, minutes=0.2)])


def test_day_facts():
    facts = recap.day_facts([rnd(DAY, 9, minutes=30, distractions=1, blocked={"a": 1}), rnd(DAY, 14, minutes=20, distractions=2, blocked={"a": 1, "b": 1})])
    assert facts["minutes"] == 50 and facts["rounds"] == 2 and facts["distractions"] == 3 and facts["blocked"] == {"a": 2, "b": 1}
    assert facts["first_start"] == at(DAY, 9) and facts["last_end"] == at(DAY, 14, 20)
    assert recap.day_facts([])["first_start"] is None


def test_old_history_entries_without_times_still_work():
    old = {"day": DAY.isoformat(), "minutes": 30.0, "planned": 30, "distractions": 0, "completed": True}  # from before times were kept
    facts = recap.day_facts([old])
    assert facts["minutes"] == 30 and facts["first_start"] is None
    text = recap.recap_text(DAY, [old, old])
    assert "Focused 1 hour in 2 rounds." in text and "Started" not in text and "Zero distractions" in text
    assert habits.analyse([old], DAY).rounds == 0  # habits need clock times, so they simply skip these


# ------------------------------------------------------------------ habits
def morning_history(days=10, rounds_per_day=2):
    out = []
    for back in range(days):
        day = DAY - timedelta(days=back)
        for i in range(rounds_per_day):
            out.append(rnd(day, 9 + i, 30, 60.0, distractions=1, blocked={"youtube.com": 1}))
    return out


def test_habits_from_a_few_weeks_of_mornings():
    h = habits.analyse(morning_history(), DAY)
    assert h.active_days == 10 and h.rounds == 20 and h.has_patterns
    assert h.typical_round_minutes == 60
    assert h.typical_start == "09:30" and h.typical_finish == "11:30"
    assert h.best_period == "morning"
    assert h.streak == 10 and h.longest_streak == 10
    assert h.top_distractions == {"youtube.com": 20}
    assert round(h.avg_minutes_per_active_day) == 120 and h.distractions_per_hour == 1.0


def test_evening_studier():
    history = [rnd(DAY - timedelta(days=i), 19, 0, 90.0) for i in range(5)]
    h = habits.analyse(history, DAY)
    assert h.best_period == "evening" and h.typical_start == "19:00" and h.typical_finish == "20:30"


def test_no_patterns_are_claimed_from_too_little_data():
    h = habits.analyse([rnd(DAY, 9), rnd(DAY, 11)], DAY)
    assert h.active_days == 1 and not h.has_patterns
    text = habits.memory_text(h)
    assert "Not enough sessions yet" in text and "typically start" not in text
    assert "still learning" in habits.summary_text(h)


def test_streak_breaks_on_a_missed_day():
    history = [rnd(DAY - timedelta(days=i), 9) for i in (0, 1, 2, 4, 5, 6, 7)]  # missed 3 days ago
    h = habits.analyse(history, DAY)
    assert h.streak == 3 and h.longest_streak == 4


def test_streak_can_end_yesterday():
    history = [rnd(DAY - timedelta(days=i), 9) for i in (1, 2)]
    assert habits.analyse(history, DAY).streak == 2


def test_the_window_is_thirty_days():
    old = [rnd(DAY - timedelta(days=45), 9)]
    assert habits.analyse(old, DAY).rounds == 0
    assert habits.memory_text(habits.analyse(old, DAY)) == ""


def test_best_weekday_needs_real_evidence():
    assert habits.analyse(morning_history(days=4), DAY).best_weekday == ""
    history = []
    for week in range(3):  # long Mondays, short everything else, for three weeks
        for offset, minutes in enumerate([120, 30, 30, 30, 30]):
            day = DAY - timedelta(days=7 * week) + timedelta(days=offset)
            if day <= DAY + timedelta(days=4):
                history.append(rnd(day, 9, minutes=float(minutes)))
    h = habits.analyse(history, DAY + timedelta(days=4))
    assert h.best_weekday == "Monday"


def test_memory_text_is_third_person_plain_facts():
    text = habits.memory_text(habits.analyse(morning_history(), DAY))
    assert text.startswith("User studies in timed focus sessions")
    assert "morning" in text and "09:30" in text and "youtube.com ×20" in text and "10 days" in text
    assert " I " not in text and "your" not in text.lower()


def test_summary_text_is_second_person():
    text = habits.summary_text(habits.analyse(morning_history(), DAY))
    assert "You tend to start around 09:30" in text and "You focus best in the morning" in text
    assert "Streak: 10 days" in text
    assert "I don't have any" in habits.summary_text(habits.Habits())


def test_sub_minute_rounds_are_ignored_by_habits():
    h = habits.analyse([rnd(DAY, 9, minutes=0.2)] * 5, DAY)
    assert h.rounds == 0
