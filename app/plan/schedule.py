"""The day on the clock: what you asked for, laid out in order. Pure Python: no AI, no network, no files.

The AI only *reads* your request (``request.py``). Every minute here is worked out by code, because models are bad
at clock arithmetic and a plan that is 40 minutes off is worse than no plan. Times are minutes since midnight of the
planned day.

The day is a trip between places (home, school, the gym ...). The scheduler tries every sensible order of the
stops (studying may also be split in two around an errand), lays each order out on the clock (travel, focus rounds
with their breaks, lunch inside the study, around what is already in your calendar) and keeps the order that is on
time with the least travel. When nothing is on time it keeps the least-late order, and ``ways_to_fit`` works out
which changes would make the day fit.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from itertools import permutations
from typing import Any, Iterator

STUDY, MEAL, ACTIVITY, FIXED = "study", "meal", "activity", "fixed"  # what you ask for
TRAVEL, BREAK, BUSY, FREE = "travel", "break", "busy", "free"  # what the scheduler adds around it

MIN_CHUNK = 15  # a study piece shorter than this isn't worth sitting down for
DEFAULT_TRAVEL = 20  # a trip nobody told Miki about
LUNCH_AT = 12 * 60 + 30  # lunch goes to the study break closest to this, unless you said when
DAY_END = 24 * 60
SHOW_FREE = 15  # gaps shorter than this aren't shown as free time
MAX_PERMUTED = 3  # with more errands than this, the order you said them in is kept (the search would get slow)


def hm(minute: int) -> str:
    minute = max(0, int(minute))
    return f"{minute // 60 % 24:02d}:{minute % 60:02d}"


def span(minutes: int) -> str:
    """75 -> '1h 15m', 50 -> '50 min', 480 -> '8h'."""
    hours, mins = divmod(int(minutes), 60)
    if not hours:
        return f"{mins} min"
    return f"{hours}h {mins:02d}m" if mins else f"{hours}h"


@dataclass(frozen=True)
class Task:
    """One thing you asked for, with every length already known."""

    kind: str  # STUDY | MEAL | ACTIVITY | FIXED
    title: str
    minutes: int
    place: str | None = None  # None: wherever you are (study happens at the day's study place)
    at: int | None = None  # FIXED: when it starts. MEAL: when you'd like it (a preference)
    after: int | None = None  # not before this
    before: int | None = None  # finished by this
    during_study: bool = False  # a meal taken as a long break inside the study session (lunch)
    ends_day: bool = False  # FIXED: everything else happens before it ("home by 8 for dinner")
    after_study: bool = False  # "gym after studying"
    before_study: bool = False  # "gym before I start studying"


@dataclass
class Block:
    kind: str  # a Task kind, or TRAVEL | BREAK | BUSY | FREE
    title: str
    start: int
    end: int
    place: str | None = None

    @property
    def minutes(self) -> int:
        return self.end - self.start

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Block":
        return cls(str(data["kind"]), str(data["title"]), int(data["start"]), int(data["end"]), data.get("place"))


def route_key(a: str, b: str) -> tuple[str, str]:
    return (a, b) if a <= b else (b, a)


@dataclass
class Day:
    tasks: list[Task]
    start: int
    start_place: str = "home"
    study_place: str = "home"
    home: str = "home"
    round_minutes: int = 60
    break_minutes: int = 5
    busy: list[tuple[int, int, str]] = field(default_factory=list)  # what's already in the calendar
    routes: dict[tuple[str, str], int] = field(default_factory=dict)  # route_key(a, b) -> minutes, either way
    earliest: int | None = None  # the earliest this day could start (for "leave earlier" advice); None: it can't

    def route(self, a: str, b: str) -> tuple[int, bool]:
        """(minutes, whether Miki actually knows them) for the trip from ``a`` to ``b``."""
        if a == b:
            return 0, True
        known = self.routes.get(route_key(a, b))
        return (known, True) if known is not None else (DEFAULT_TRAVEL, False)


@dataclass
class Schedule:
    blocks: list[Block]
    late: int = 0  # minutes late, summed over everything with a deadline (0: the day fits)
    problems: list[str] = field(default_factory=list)
    travel: int = 0
    guessed_routes: list[tuple[str, str]] = field(default_factory=list)  # trips timed with DEFAULT_TRAVEL
    end: int = 0
    end_place: str = ""

    @property
    def ok(self) -> bool:
        return self.late == 0

    @property
    def home_at(self) -> int | None:
        """When you walk in the door at the end of the day (None if you never leave home)."""
        trips = [b for b in self.blocks if b.kind == TRAVEL]
        return trips[-1].end if trips and trips[-1].place == self.end_place else None

    @property
    def study_minutes(self) -> int:
        return sum(b.minutes for b in self.blocks if b.kind == STUDY)


# ------------------------------------------------------------------------------------------------ laying one order out
class _Layout:
    """Walks through one order of the day, minute by minute, and notes everything that's late."""

    def __init__(self, day: Day) -> None:
        self.day = day
        self.t = day.start
        self.place = day.start_place
        self.blocks: list[Block] = []
        self.late = 0
        self.problems: list[str] = []
        self.travel = 0
        self.soft = 0  # how far things drift from when you'd like them (lunch at 12:30)
        self.guessed: list[tuple[str, str]] = []
        self.boundaries: list[int] = []  # when each study piece ends: where lunch can go

    def _clear(self, start: int, length: int) -> int:
        """The earliest time from ``start`` when ``length`` minutes fit between the calendar's events."""
        moved = True
        while moved:
            moved = False
            for b0, b1, _ in self.day.busy:
                if start < b1 and b0 < start + max(length, 1):
                    start, moved = b1, True
        return start

    def _next_busy(self, t: int) -> tuple[int, int] | None:
        upcoming = [(b0, b1) for b0, b1, _ in self.day.busy if b1 > t]
        return min(upcoming) if upcoming else None

    def _is_late(self, minutes: int, text: str) -> None:
        self.late += minutes
        self.problems.append(text)

    def go(self, place: str | None) -> None:
        """Travel as soon as the last thing is done (any spare time is spent where you're going, e.g. at home)."""
        if not place or place == self.place:
            return
        leg, known = self.day.route(self.place, place)
        if not known and route_key(self.place, place) not in self.guessed:
            self.guessed.append(route_key(self.place, place))
        leave = self._clear(self.t, leg)
        self.blocks.append(Block(TRAVEL, f"To {place}", leave, leave + leg, place))
        self.t, self.place = leave + leg, place
        self.travel += leg

    def task(self, task: Task) -> None:
        place = task.place or self.place
        if task.kind == FIXED:
            self.go(place)
            if self.t > task.at:
                self._is_late(self.t - task.at, f"{span(self.t - task.at)} late for {task.title.lower()} at {hm(task.at)}")
            start = max(self.t, task.at)
        else:
            self.go(place)
            start = self._clear(max(self.t, task.after or 0), task.minutes)
        end = start + task.minutes
        if task.before is not None and end > task.before:
            self._is_late(end - task.before, f"{task.title} would only finish at {hm(end)}, after {hm(task.before)}")
        if task.kind == MEAL and task.at is not None:
            self.soft += abs(start - task.at) // 2
        self.blocks.append(Block(task.kind, task.title, start, end, place))
        self.t, self.place = end, place

    def study(self, pieces: list[tuple[str, int]], lunch: Task | None, lunch_slot: int | None) -> None:
        """Rounds of at most ``round_minutes``, a short break between them, the calendar's events left alone."""
        self.go(self.day.study_place)
        queue = [[subject, minutes] for subject, minutes in pieces if minutes > 0]
        while queue:
            subject, left = queue[0]
            busy = self._next_busy(self.t)
            if busy and busy[0] <= self.t:  # something in the calendar right now: wait it out
                self.t = busy[1]
                continue
            chunk = min(self.day.round_minutes, left)
            if left - chunk < MIN_CHUNK:  # don't leave a 10-minute scrap for later
                chunk = left
            if busy and busy[0] < self.t + chunk:
                room = busy[0] - self.t
                if room < MIN_CHUNK:
                    self.t = busy[1]
                    continue
                chunk = room
            self.blocks.append(Block(STUDY, subject, self.t, self.t + chunk, self.place))
            self.t += chunk
            queue[0][1] -= chunk
            if queue[0][1] <= 0:
                queue.pop(0)
            slot = len(self.boundaries)
            self.boundaries.append(self.t)
            if lunch is not None and slot == lunch_slot:
                self.task(lunch)  # lunch is the long break
            elif queue:
                end = self.t + self.day.break_minutes
                busy = self._next_busy(self.t)
                if busy and busy[0] < end:
                    end = max(self.t, busy[0])
                if end > self.t:
                    self.blocks.append(Block(BREAK, "Break", self.t, end, self.place))
                    self.t = end

    def finish(self) -> None:
        self.go(self.day.home)  # every day ends at home
        if self.t > DAY_END:
            self._is_late(self.t - DAY_END, f"the day would only end at {hm(self.t)}, after midnight")


# ------------------------------------------------------------------------------------------------ trying every order
def _split(day: Day) -> tuple[list[Task], Task | None, list[Task]]:
    """(the study subjects, lunch inside the study if any, every other stop)."""
    studies = [t for t in day.tasks if t.kind == STUDY and t.minutes > 0]
    lunch = next((t for t in day.tasks if t.kind == MEAL and t.during_study), None) if studies else None
    stops = [t for t in day.tasks if t.kind != STUDY and t is not lunch]
    if lunch is not None:
        lunch = replace(lunch, place=None, at=lunch.at if lunch.at is not None else LUNCH_AT)
    return studies, lunch, stops


def _rounds(studies: list[Task], size: int) -> list[tuple[str, int]]:
    """The study as focus-sized rounds, subject by subject (5h of linear algebra = five 60-minute rounds)."""
    rounds: list[tuple[str, int]] = []
    for task in studies:
        left = task.minutes
        while left > 0:
            chunk = left if left - min(size, left) < MIN_CHUNK else size
            rounds.append((task.title, chunk))
            left -= chunk
    return rounds


def _merge(rounds: list[tuple[str, int]]) -> list[tuple[str, int]]:
    merged: list[tuple[str, int]] = []
    for subject, minutes in rounds:
        if merged and merged[-1][0] == subject:
            merged[-1] = (subject, merged[-1][1] + minutes)
        else:
            merged.append((subject, minutes))
    return merged


def _orders(stops: list[Task], rounds: list[tuple[str, int]], study_first: bool) -> Iterator[tuple[tuple[Any, ...], int | None]]:
    """Every sensible order of the stops. "S" is the whole study session; "S1" and "S2" its two halves, split after
    ``k`` rounds (so an errand can sit in the middle). Things with a fixed time stay in time order. The order you
    said things in comes first, so it wins a tie."""
    variants: list[tuple[list[str], int | None]] = [(["S"], None)] if rounds else [([], None)]
    if len(rounds) > 1 and stops:
        variants += [(["S1", "S2"], k) for k in range(1, len(rounds))]
    for study_units, k in variants:
        items = [*study_units, *stops] if study_first else [*stops, *study_units]
        if len(stops) > MAX_PERMUTED:
            fixed = sorted((t for t in stops if t.kind == FIXED), key=lambda t: t.at)
            first = [t for t in stops if t.kind != FIXED and t.before_study]
            loose = [t for t in stops if t.kind != FIXED and not t.before_study]
            candidates: Any = [tuple(first + study_units + loose + fixed)] if k is None else []
        else:
            candidates = permutations(items)
        for order in candidates:
            if k is not None and order.index("S2") <= order.index("S1") + 1:
                continue  # the halves must be in order, with something in between (else it's just "S")
            times = [u.at for u in order if isinstance(u, Task) and u.kind == FIXED]
            if times != sorted(times):
                continue
            enders = [i for i, u in enumerate(order) if isinstance(u, Task) and u.ends_day]
            if enders and enders[0] != len(order) - 1:
                continue  # nothing may come after "home by 8 for dinner"
            study_at = [i for i, u in enumerate(order) if isinstance(u, str)]
            if study_at and any(isinstance(u, Task) and ((u.after_study and i < study_at[-1]) or (u.before_study and i > study_at[0]))
                                for i, u in enumerate(order)):
                continue  # "gym after studying" means after all of it
            yield order, k


def _lay(day: Day, order: tuple[Any, ...], k: int | None, rounds: list[tuple[str, int]], lunch: Task | None, slot: int | None) -> _Layout:
    halves = {"S": _merge(rounds), "S1": _merge(rounds[:k or 0]), "S2": _merge(rounds[k or 0:])}
    layout = _Layout(day)
    for unit in order:
        if isinstance(unit, str):
            layout.study(halves[unit], lunch, slot)
        else:
            layout.task(unit)
    layout.finish()
    return layout


def _decorate(blocks: list[Block], day: Day) -> list[Block]:
    """Add what's already in the calendar and the free time in between, in time order."""
    end = max((b.end for b in blocks), default=day.start)
    timeline = sorted([*blocks, *(Block(BUSY, title, b0, b1) for b0, b1, title in day.busy if b1 > day.start and b0 < end)],
                      key=lambda b: (b.start, b.end))
    result: list[Block] = []
    cursor = day.start
    for block in timeline:
        if block.start - cursor >= SHOW_FREE:
            result.append(Block(FREE, "Free time", cursor, block.start, result[-1].place if result else day.start_place))
        result.append(block)
        cursor = max(cursor, block.end)
    return result


def plan_day(day: Day) -> Schedule:
    """The best layout of ``day``: on time first, then the least travel, then the earliest finish."""
    studies, lunch, stops = _split(day)
    rounds = _rounds(studies, max(MIN_CHUNK, day.round_minutes))
    best: tuple[tuple[int, ...], _Layout] | None = None
    study_first = bool(day.tasks) and day.tasks[0].kind == STUDY
    for index, (order, k) in enumerate(_orders(stops, rounds, study_first)):
        layout = _lay(day, order, k, rounds, None, None)
        if lunch is not None:
            if layout.boundaries:  # second pass: lunch replaces the break closest to lunchtime
                slot = min(range(len(layout.boundaries)), key=lambda i: abs(layout.boundaries[i] - lunch.at))
                layout = _lay(day, order, k, rounds, lunch, slot)
            else:
                layout = _lay(day, (*order, lunch), k, rounds, None, None)
        score = (layout.late, layout.travel + layout.soft, layout.t, index)
        if best is None or score < best[0]:
            best = (score, layout)
    if best is None:  # nothing at all to do
        return Schedule([], end=day.start, end_place=day.start_place)
    layout = best[1]
    return Schedule(_decorate(layout.blocks, day), layout.late, layout.problems, layout.travel, layout.guessed, layout.t, layout.place)


# ------------------------------------------------------------------------------------------------ when it doesn't fit
def _less_study(tasks: list[Task], cut: int) -> list[Task]:
    """Take ``cut`` minutes of study away, from the last subject backwards."""
    result = list(tasks)
    for index in range(len(result) - 1, -1, -1):
        task = result[index]
        if cut <= 0 or task.kind != STUDY:
            continue
        taken = min(cut, task.minutes)
        result[index] = replace(task, minutes=task.minutes - taken)
        cut -= taken
    return [t for t in result if t.kind != STUDY or t.minutes > 0]


def ways_to_fit(day: Day, schedule: Schedule, limit: int = 3) -> list[tuple[str, Schedule]]:
    """Changes that would make a late day fit, each checked by planning it again: ("Skip gym", that day), ..."""
    if schedule.ok:
        return []
    tries: list[tuple[str, Day]] = []
    if day.earliest is not None:
        start = (day.start - schedule.late) // 5 * 5
        if start >= day.earliest:
            tries.append((f"Start at {hm(start)} instead of {hm(day.start)}", replace(day, start=start)))
    total = sum(t.minutes for t in day.tasks if t.kind == STUDY)
    for cut in range(30, total // 2 + 1, 30):
        if plan_day(less := replace(day, tasks=_less_study(day.tasks, cut))).ok:
            tries.append((f"Study {span(total - cut)} instead of {span(total)}", less))
            break
    for task in day.tasks:
        if task.kind == ACTIVITY:
            tries.append((f"Skip {task.title.lower()}", replace(day, tasks=[t for t in day.tasks if t is not task])))
        elif task.kind == MEAL and task.minutes > 30:
            shorter = [replace(t, minutes=30) if t is task else t for t in day.tasks]
            tries.append((f"Make {task.title.lower()} 30 min", replace(day, tasks=shorter)))
    if day.study_place != day.home and total:
        tries.append((f"Study at {day.home} instead of {day.study_place}", replace(day, study_place=day.home)))

    found: list[tuple[str, Schedule]] = []
    for text, variant in tries:
        result = plan_day(variant)
        if result.ok:
            home_at = result.home_at
            found.append((f"{text}: home by {hm(home_at)}" if home_at is not None else f"{text}: done by {hm(result.end)}", result))
        if len(found) >= limit:
            break
    return found
