"""What the focus history says about how you study. Pure analysis, no I/O, no AI: just counting.

Patterns are only claimed with enough evidence (a few days and rounds), so Miki never states a "habit"
after one Tuesday.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from app.focus.recap import format_minutes, real_rounds, top_blocked

WINDOW_DAYS = 30
MIN_DAYS_FOR_PATTERNS = 3
MIN_ROUNDS_FOR_PATTERNS = 4
PERIODS = (("morning", 5, 12), ("afternoon", 12, 17), ("evening", 17, 22))  # anything else is "late at night"
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


@dataclass
class Habits:
    days_in_window: int = WINDOW_DAYS
    active_days: int = 0
    rounds: int = 0
    total_minutes: float = 0.0
    avg_minutes_per_active_day: float = 0.0
    typical_round_minutes: int = 0
    typical_start: str = ""  # "09:30"
    typical_finish: str = ""
    best_period: str = ""
    best_weekday: str = ""
    top_distractions: dict[str, int] = field(default_factory=dict)
    distractions_per_hour: float = 0.0
    streak: int = 0
    longest_streak: int = 0

    @property
    def has_patterns(self) -> bool:
        return self.active_days >= MIN_DAYS_FOR_PATTERNS and self.rounds >= MIN_ROUNDS_FOR_PATTERNS


def _period(hour: int) -> str:
    for name, start, end in PERIODS:
        if start <= hour < end:
            return name
    return "late at night"


def _median_clock(values: list[float]) -> str:
    """Median time of day ('09:30') of epoch timestamps."""
    minutes = [datetime.fromtimestamp(v).hour * 60 + datetime.fromtimestamp(v).minute for v in values]
    mid = int(statistics.median(minutes))
    return f"{mid // 60:02d}:{mid % 60:02d}"


def _streaks(days: set[date], today: date) -> tuple[int, int]:
    """(current streak ending today or yesterday, longest streak) over ``days``."""
    longest = run = 0
    for offset in range(WINDOW_DAYS - 1, -1, -1):
        run = run + 1 if (today - timedelta(days=offset)) in days else 0
        longest = max(longest, run)
    day = today if today in days else today - timedelta(days=1)
    current = 0
    while day in days:
        current += 1
        day -= timedelta(days=1)
    return current, longest


def analyse(history: list[dict[str, Any]], today: date | None = None) -> Habits:
    today = today or date.today()
    cutoff = (today - timedelta(days=WINDOW_DAYS - 1)).isoformat()
    rounds = [r for r in real_rounds(history) if cutoff <= str(r.get("day", "")) <= today.isoformat() and r.get("start")]
    habits = Habits()
    if not rounds:
        return habits

    by_day: dict[str, list[dict[str, Any]]] = {}
    for r in rounds:
        by_day.setdefault(r["day"], []).append(r)
    minutes_by_day = {day: sum(float(r["minutes"]) for r in items) for day, items in by_day.items()}
    active = {date.fromisoformat(d) for d, m in minutes_by_day.items() if m >= 10}

    habits.rounds = len(rounds)
    habits.active_days = len(by_day)
    habits.total_minutes = sum(minutes_by_day.values())
    habits.avg_minutes_per_active_day = habits.total_minutes / len(by_day)
    habits.typical_round_minutes = int(round(statistics.median(float(r["minutes"]) for r in rounds) / 5) * 5) or 5
    habits.typical_start = _median_clock([min(float(r["start"]) for r in items) for items in by_day.values()])
    habits.typical_finish = _median_clock([max(float(r.get("end") or r["start"]) for r in items) for items in by_day.values()])
    habits.streak, habits.longest_streak = _streaks(active, today)

    per_period: dict[str, float] = {}
    for r in rounds:  # spread each round over the periods it actually covered
        moment, end = float(r["start"]), float(r.get("end") or r["start"])
        while moment < end:
            step = min(300.0, end - moment)
            name = _period(datetime.fromtimestamp(moment).hour)
            per_period[name] = per_period.get(name, 0.0) + step / 60
            moment += step
    habits.best_period = max(per_period, key=per_period.get) if per_period else ""

    per_weekday: dict[int, list[float]] = {}
    for day, minutes in minutes_by_day.items():
        per_weekday.setdefault(date.fromisoformat(day).weekday(), []).append(minutes)
    eligible = {wd: sum(v) / len(v) for wd, v in per_weekday.items() if len(v) >= 2}
    if habits.active_days >= 6 and len(eligible) >= 3:
        habits.best_weekday = WEEKDAYS[max(eligible, key=eligible.get)]

    blocked: dict[str, int] = {}
    for r in rounds:
        for name, count in (r.get("blocked") or {}).items():
            blocked[name] = blocked.get(name, 0) + int(count)
    habits.top_distractions = dict(sorted(blocked.items(), key=lambda kv: (-kv[1], kv[0]))[:3])
    hours = habits.total_minutes / 60
    habits.distractions_per_hour = sum(int(r.get("distractions", 0)) for r in rounds) / hours if hours else 0.0
    return habits


def memory_text(h: Habits) -> str:
    """The study-habits memory, in Miki's memory style (third person, plain facts). Empty if there is nothing yet."""
    if not h.rounds:
        return ""
    parts = [
        f"User studies in timed focus sessions using Miki's focus mode. In the last {h.days_in_window} days they focused on "
        f"{h.active_days} day{'s' if h.active_days != 1 else ''}, about {format_minutes(h.avg_minutes_per_active_day)} per active day."
    ]
    if h.has_patterns:
        parts.append(f"Their usual round is about {h.typical_round_minutes} minutes.")
        parts.append(f"They typically start around {h.typical_start} and finish around {h.typical_finish}, and get most of their focus in the {h.best_period}.")
        if h.best_weekday:
            parts.append(f"{h.best_weekday} is their strongest study day.")
    else:
        parts.append("Not enough sessions yet to describe a routine.")
    if h.top_distractions:
        parts.append(f"Their biggest distractions during focus are {top_blocked(h.top_distractions)}.")
    if h.streak >= 2:
        parts.append(f"Current focus streak: {h.streak} days (longest in the last {h.days_in_window} days: {h.longest_streak}).")
    return " ".join(parts)


def summary_text(h: Habits) -> str:
    """The same picture for the user, in second person (for /focus habits)."""
    if not h.rounds:
        return "I don't have any focus sessions to learn from yet. Start one with /focus."
    lines = [
        f"Your study habits (last {h.days_in_window} days)",
        f"Focused on {h.active_days} day{'s' if h.active_days != 1 else ''}, {format_minutes(h.total_minutes)} in total, "
        f"about {format_minutes(h.avg_minutes_per_active_day)} per active day.",
    ]
    if h.has_patterns:
        lines.append(f"Usual round: about {h.typical_round_minutes} min. You tend to start around {h.typical_start} and finish around {h.typical_finish}.")
        lines.append(f"You focus best in the {h.best_period}" + (f", and {h.best_weekday} is your strongest day." if h.best_weekday else "."))
    else:
        lines.append("I'm still learning: a few more days and I can tell when you focus best.")
    if h.top_distractions:
        lines.append(f"Biggest distractions: {top_blocked(h.top_distractions)} ({h.distractions_per_hour:.1f} bounced per hour).")
    lines.append(f"Streak: {h.streak} day{'s' if h.streak != 1 else ''} (longest {h.longest_streak}).")
    return "\n".join(lines)
