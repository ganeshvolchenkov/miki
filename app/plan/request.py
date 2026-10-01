"""Reading "I want to study 8 hours, 5 linear algebra, 3 calculus, lunch 50 min, gym, home by 8 for dinner".

The AI turns the sentence into a list of wishes (JSON); it never schedules anything, ``schedule.py`` does. Its
answer is treated like input from outside: every field is checked, clamped or dropped here, so a confused model
can produce a bad plan at worst (which you see before anything happens), never a crash or a 30-hour study block.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from app.plan.schedule import ACTIVITY, FIXED, MEAL, STUDY

MAX_ITEMS = 12
MAX_TITLE = 60
MAX_PLACE = 30
MAX_STUDY_MINUTES = 16 * 60
MAX_FACTS = 20
_PLACE = re.compile(r"[a-z0-9][a-z0-9 '&-]*")
_CLOCK = re.compile(r"(\d{1,2})[:.h](\d{2})")
DEFAULT_TITLES = {STUDY: "Study", MEAL: "Meal", ACTIVITY: "Activity", FIXED: "Appointment"}


@dataclass
class Wish:
    """One thing you asked for, as read; ``minutes`` may still be unknown (None) until the service fills it in."""

    kind: str
    title: str
    minutes: int | None = None
    place: str | None = None
    at: int | None = None
    after: int | None = None
    before: int | None = None
    during_study: bool = False
    ends_day: bool = False
    after_study: bool = False
    before_study: bool = False


@dataclass
class Facts:
    """General truths you mentioned: trips, how long things take, where you study."""

    travel: list[tuple[str, str, int]] = field(default_factory=list)
    durations: dict[str, int] = field(default_factory=dict)
    study_place: str | None = None

    @property
    def empty(self) -> bool:
        return not self.travel and not self.durations and not self.study_place


@dataclass
class Request:
    wishes: list[Wish]
    day_offset: int = 0  # 0 today, 1 tomorrow
    start: int | None = None  # minutes since midnight, when you said when the day starts
    start_place: str | None = None
    study_place: str | None = None
    facts: Facts = field(default_factory=Facts)


# ------------------------------------------------------------------------------------------------ checking fields
def clock(value: Any) -> int | None:
    """'20:00' / '8:30' / '20.00' -> minutes since midnight; anything else -> None."""
    match = _CLOCK.fullmatch(str(value or "").strip())
    if not match:
        return None
    hour, minute = int(match[1]), int(match[2])
    return hour * 60 + minute if 0 <= hour < 24 and 0 <= minute < 60 else None


def place_name(value: Any) -> str | None:
    """A short lowercase place name ("gym", "school"), or None."""
    if not isinstance(value, str):
        return None
    text = re.sub(r"\s+", " ", value).strip().lower()
    text = re.sub(r"^(the|my) ", "", text)
    return text if 0 < len(text) <= MAX_PLACE and _PLACE.fullmatch(text) else None


def minutes(value: Any, low: int, high: int) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = int(round(value))
    return number if low <= number <= high else None


def title_text(value: Any, kind: str) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip(" .,-")
    text = text[:1].upper() + text[1:]
    return (text if len(text) <= MAX_TITLE else text[: MAX_TITLE - 1].rstrip() + "…") or DEFAULT_TITLES[kind]


def _wish(raw: Any) -> Wish | None:
    if not isinstance(raw, dict):
        return None
    kind = str(raw.get("kind", "")).strip().lower()
    if kind not in DEFAULT_TITLES:
        return None
    wish = Wish(
        kind=kind,
        title=title_text(raw.get("title"), kind),
        minutes=minutes(raw.get("minutes"), 0 if kind == FIXED else 5, 12 * 60),
        place=place_name(raw.get("place")),
        at=clock(raw.get("at")),
        after=clock(raw.get("after")),
        before=clock(raw.get("before")),
        during_study=raw.get("during_study") is True and kind == MEAL,
        ends_day=raw.get("ends_day") is True,
        after_study=str(raw.get("after", "")).strip().lower() == "study",
        before_study=str(raw.get("before", "")).strip().lower() == "study",
    )
    if wish.kind == STUDY:
        wish.place = wish.at = None  # study happens at the day's study place, in rounds
        wish.after_study = wish.before_study = False
    elif wish.kind == FIXED and wish.at is None:
        wish.kind = ACTIVITY  # "dinner with Sam" without a time is just something to fit in
    elif wish.at is not None and not wish.during_study:
        wish.kind = FIXED  # "gym at 6pm" is a time to keep
    wish.ends_day = wish.ends_day and wish.kind == FIXED
    return wish


def _facts(raw: Any) -> Facts:
    facts = Facts()
    if not isinstance(raw, dict):
        return facts
    for trip in (raw.get("travel") or [])[:MAX_FACTS] if isinstance(raw.get("travel"), list) else []:
        if not isinstance(trip, dict):
            continue
        a, b, length = place_name(trip.get("from")), place_name(trip.get("to")), minutes(trip.get("minutes"), 1, 300)
        if a and b and a != b and length is not None:
            facts.travel.append((a, b, length))
    durations = raw.get("durations")
    if isinstance(durations, dict):
        for name, value in list(durations.items())[:MAX_FACTS]:
            key, length = place_name(name), minutes(value, 5, 10 * 60)
            if key and length is not None:
                facts.durations[key] = length
    facts.study_place = place_name(raw.get("study_place"))
    return facts


def parse(raw: Any) -> Request | None:
    """The model's JSON -> a checked ``Request`` (None if it isn't one at all)."""
    if not isinstance(raw, dict):
        return None
    wishes = [w for w in (_wish(item) for item in (raw.get("items") or [])[:MAX_ITEMS] if isinstance(raw.get("items"), list)) if w]
    budget = MAX_STUDY_MINUTES
    for wish in wishes:  # never more than 16 hours of study, however the model added it up
        if wish.kind == STUDY and wish.minutes is not None:
            wish.minutes = min(wish.minutes, budget)
            budget -= wish.minutes
    wishes = [w for w in wishes if not (w.kind == STUDY and w.minutes == 0)]
    fixed = [w for w in wishes if w.kind == FIXED]
    if len([w for w in fixed if w.ends_day]) > 1:  # only the latest one can end the day
        latest = max((w for w in fixed if w.ends_day), key=lambda w: w.at)
        for w in fixed:
            w.ends_day = w is latest
    return Request(
        wishes=wishes,
        day_offset=1 if str(raw.get("day", "")).strip().lower() == "tomorrow" else 0,
        start=clock(raw.get("start")),
        start_place=place_name(raw.get("start_place")),
        study_place=place_name(raw.get("study_place")),
        facts=_facts(raw.get("learn")),
    )


def to_json(request: Request) -> dict[str, Any]:
    """The request back in the model's own shape: shown to it when you ask to change the plan."""
    def hhmm(value: int | None) -> str | None:
        return None if value is None else f"{value // 60:02d}:{value % 60:02d}"

    items = []
    for wish in request.wishes:
        item: dict[str, Any] = {"kind": wish.kind, "title": wish.title, "minutes": wish.minutes}
        for name in ("place", "at", "after", "before"):
            value = getattr(wish, name)
            if value is not None:
                item[name] = hhmm(value) if name != "place" else value
        if wish.after_study:
            item["after"] = "study"
        if wish.before_study:
            item["before"] = "study"
        if wish.during_study:
            item["during_study"] = True
        if wish.ends_day:
            item["ends_day"] = True
        items.append(item)
    return {"day": "tomorrow" if request.day_offset else "today", "start": hhmm(request.start), "start_place": request.start_place,
            "study_place": request.study_place, "items": items}


# ------------------------------------------------------------------------------------------------ asking the AI
SYSTEM_PROMPT = """You read how someone wants to spend their day and turn it into JSON for a scheduler.
You do NOT schedule anything and do NOT add things up: list exactly what they asked for.
Now: {now}.
Places Miki knows (minutes of travel): {routes}. Usual lengths: {durations}. They usually study at: {study_place}.
{previous}
Reply ONLY with JSON in this shape:
{{"day": "today" or "tomorrow",
 "start": "HH:MM" or null,
 "start_place": place or null,
 "study_place": place or null,
 "items": [
  {{"kind": "study", "title": "Linear algebra", "minutes": 300}},
  {{"kind": "meal", "title": "Lunch", "minutes": 50, "during_study": true, "at": null}},
  {{"kind": "activity", "title": "Gym", "place": "gym", "minutes": null, "after": null, "before": null}},
  {{"kind": "fixed", "title": "Dinner", "place": "home", "at": "20:00", "minutes": null, "ends_day": true}}
 ],
 "learn": {{"travel": [{{"from": "home", "to": "school", "minutes": 50}}], "durations": {{"gym": 75}}, "study_place": null}}}}
Rules:
- One study item per subject with its own minutes. "8 hours: 5 linear algebra, 3 calculus" is two items (300 and 180), never a third 480 one. Study without a subject: title "Study".
- A meal or break taken in the middle of studying has "during_study": true.
- "fixed" = something at a set time. "Home by 8 for dinner" is fixed, place "home", at "20:00", "ends_day": true (everything else happens before it). A bare "be home by 8" is the same with title "Home" and minutes 0.
- minutes: null when they don't say how long. place: one short lowercase noun ("gym", "school", "home", "library"), or null if it can be done anywhere.
- at/after/before/start: 24-hour "HH:MM", only when they say so ("gym in the evening" -> after "17:00"; "start at 9" -> start "09:00").
  after/before can also be "study": "gym after studying" -> after "study"; "gym before I study" -> before "study".
- Every activity, meal and appointment in the message must be an item. Never drop one, never turn one into another kind (the gym is never "study").
- start_place / study_place: only if they say where they are now / where they will study today.
- "learn": only facts they state as generally true (travel times, how long something usually takes, where they usually study). Otherwise {{}}.
- If a previous plan is shown, the message may change it ("move the gym to the morning", "only 2 hours of calculus"): return the whole updated day. If it describes a completely new day, ignore the previous plan.
- If the message only states facts (nothing to plan), return "items": [] with the facts in "learn"."""


def build_prompt(now: datetime, routes: str, durations: str, study_place: str, previous: dict[str, Any] | None) -> str:
    earlier = (f"Their current plan request (JSON): {json.dumps(previous, ensure_ascii=False)}" if previous else "There is no previous plan.")
    return SYSTEM_PROMPT.format(now=now.strftime("%A %d %B %Y, %H:%M"), routes=routes or "none yet", durations=durations or "none yet",
                                study_place=study_place, previous=earlier)


def make_ai_reader(brain: object) -> Callable[[str, str], Any]:
    """``(message, system prompt) -> parsed JSON or None``, backed by Miki's brain (anything with ``generate_response``)."""

    def ask(text: str, system_prompt: str) -> Any:
        reply = brain.generate_response(user_input=text, system_prompt=system_prompt, history=None)  # type: ignore[attr-defined]
        start = (reply or "").find("{")
        if start < 0:
            return None
        try:  # the first whole JSON object; small models sometimes add chatter or a stray "}" after it
            return json.JSONDecoder().raw_decode(reply[start:])[0]
        except ValueError:
            return None

    ask.brain = brain  # type: ignore[attr-defined]  # which model reads requests (shown in tests and logs)
    return ask
