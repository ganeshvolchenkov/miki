"""/plan: say how you want your day to go, in plain English, and get a timeline that actually fits.

    /plan I want to study 8 hours, 5 linear algebra and 3 calculus, with a 50 minute lunch break. I also want to
          hit the gym, and be home at 8pm for dinner.

1. The AI reads the sentence into wishes (``request.py``). Travel facts you mention are remembered for next time.
2. ``schedule.py`` lays the day out: trips between places, study as focus-sized rounds, lunch, your calendar's events.
   If it can't fit, you're told by how much and which changes would make it fit.
3. You see a draft. "Looks good" (``/plan ok``) makes it the day's plan and puts it in your Google Calendar;
   "Change" takes a correction in plain English ("gym in the morning", "only 2 hours of calculus").
4. During the day Miki nudges you: time to leave, a subject starting (with a one-tap focus round).

Everything runs on the brain (the server). State lives in ``data/plan.json`` (see ``store.py``).
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from datetime import time as dtime
from pathlib import Path
from typing import Any, Callable

from app.core import single_instance
from app.plan import request as req
from app.plan.schedule import (
    ACTIVITY, BREAK, BUSY, DEFAULT_TRAVEL, FIXED, FREE, LUNCH_AT, MEAL, STUDY, TRAVEL, Block, Day, Task, hm, plan_day, route_key, span,
    ways_to_fit,
)
from app.plan.store import HOME, Book, PlanStore

logger = logging.getLogger(__name__)

LOCK_NAME = "MikiPlan"
DEFAULT_PLAN_MODEL = "gpt-4.1-mini"
TICK_SECONDS = 20
LEAVE_LEAD = 5  # "time to leave" goes out this many minutes before the trip
STALE_MINUTES = 10  # a nudge this late (Miki was off) is skipped, not sent
LISTENER_WAIT_SECONDS = 2.0
DEFAULT_LENGTHS = {STUDY: 60, MEAL: 45, ACTIVITY: 60, FIXED: 0}
MEAL_LENGTHS = {"breakfast": 30, "lunch": 45, "dinner": 60}
DINNER_AFTER = 17 * 60 + 30  # a dinner with no time isn't before this
AUTO_LUNCH_STUDY = 4 * 60  # this much study (or more) starting before lunchtime gets a lunch break even if you didn't ask
AUTO_LUNCH_BEFORE = 13 * 60
EVENING = 17 * 60 + 30  # study that would start after this happens at home, not a trip away
DEFAULT_DAY_START = 8 * 60  # a plan for tomorrow starts here unless you say otherwise
EARLIEST_START = 5 * 60
MARKER = "Planned by Miki (/plan)."  # in every calendar event Miki adds, so it never plans around its own blocks
USAGE = ("Tell me how you want your day to go, e.g.\n"
         "/plan study 8 hours: 5 linear algebra, 3 calculus, a 50 min lunch break. Gym too, and home by 8pm for dinner.")
DRAFT_HINT = "Type /plan ok to lock it in (it goes into your calendar too), or tell me what to change, e.g. /plan gym in the morning."

OK_WORDS = {"ok", "okay", "yes", "confirm", "go", "lock", "looks good"}
CANCEL_WORDS = {"cancel", "clear", "stop", "discard", "delete", "off"}


@dataclass
class PlanReply:
    ok: bool
    text: str
    status: str = ""  # "draft" | "active" | "": which buttons fit under it


@dataclass
class PlanNudge:
    """Something to tell you right now: time to leave, a subject starting."""

    text: str
    key: str  # unique per plan and block, so each nudge goes out once
    focus_block: int | None = None  # a study block a focus round can be started for
    focus_minutes: int = 0
    focus_goal: str = ""


# ------------------------------------------------------------------------------------------------ the plan as text
_ICONS = {TRAVEL: "🚶", STUDY: "📚", MEAL: "🍽", ACTIVITY: "🏃", BUSY: "📅", FREE: "☕"}


def _icon(block: Block) -> str:
    if block.kind == FIXED:
        return "🏠" if block.place == HOME else "📌"
    if block.kind == ACTIVITY and "gym" in block.title.lower():
        return "🏋️"
    return _ICONS.get(block.kind, "•")


def runs(blocks: list[Block]) -> list[tuple[int, Block, int]]:
    """The timeline as you'd read it: back-to-back rounds of one subject (and their short breaks) become one line.
    Returns (index of the first block, the line's block, how many rounds)."""
    lines: list[tuple[int, Block, int]] = []
    for index, block in enumerate(blocks):
        if block.kind == BREAK:
            continue
        if block.kind == STUDY and lines:
            first, last, count = lines[-1]
            if last.kind == STUDY and last.title == block.title and block.start - last.end <= 15:
                lines[-1] = (first, Block(STUDY, last.title, last.start, block.end, last.place), count + 1)
                continue
        lines.append((index, Block(block.kind, block.title, block.start, block.end, block.place), 1))
    return lines


def _line(block: Block, rounds: int) -> str:
    if block.kind == TRAVEL:
        detail = f"{block.title} ({span(block.minutes)})"
    elif block.kind == STUDY:
        detail = f"{block.title}, until {hm(block.end)}" + (f" ({rounds} rounds)" if rounds > 1 else "")
    elif block.kind in {MEAL, ACTIVITY, BUSY} or (block.kind == FIXED and block.minutes):
        detail = f"{block.title}, until {hm(block.end)}"
    elif block.kind == FREE:
        detail = f"Free time, until {hm(block.end)}"
    else:
        detail = block.title
    return f"{hm(block.start)}  {_icon(block)} {detail}"


def plan_text(plan: dict[str, Any], now_minute: int | None = None) -> str:
    """The plan for a chat message (plain text). ``now_minute`` marks what's going on right now with ▶."""
    blocks = [Block.from_dict(b) for b in plan.get("blocks", [])]
    day = date.fromisoformat(plan["day"])
    label = "Your day" if plan.get("status") == "active" else "Here's a plan"
    lines = [f"📋 {label} for {day.strftime('%A %d %B').replace(' 0', ' ')}:", ""]
    for _, block, rounds in runs(blocks):
        here = now_minute is not None and block.start <= now_minute < max(block.end, block.start + 1)
        lines.append(("▶ " if here else "") + _line(block, rounds))
    if not blocks:
        lines.append("Nothing to schedule.")
    summary = [f"{span(plan.get('study_minutes', 0))} study"] if plan.get("study_minutes") else []
    if plan.get("travel"):
        summary.append(f"{span(plan['travel'])} travel")
    if plan.get("home_at") is not None:
        summary.append(f"home at {hm(plan['home_at'])}")
    lines += ["", " · ".join(summary)] if summary else []
    if plan.get("skipped"):
        lines += [""] + [f"⏭ Skipping: {e['title']} ({hm(e['start'])}–{hm(e['end'])})" for e in plan["skipped"]]
    if plan.get("problems"):
        lines += ["", "⚠️ This doesn't fit: " + "; ".join(plan["problems"]) + "."]
        if plan.get("ways"):
            lines += ["Ways to make it fit:"] + [f"• {way}" for way in plan["ways"]]
        else:
            lines.append("Tell me what to drop or shorten.")
    if plan.get("notes"):
        lines += [""] + [f"ℹ️ {note}" for note in plan["notes"]]
    return "\n".join(lines)


def calendar_blocks(blocks: list[Block]) -> list[Block]:
    """What goes into Google Calendar: one event per line of the timeline, not per round. Never what was already
    there (busy), free time, or a zero-length moment ("home by 8")."""
    return [block for _, block, _ in runs(blocks) if block.kind not in {BUSY, FREE} and block.minutes > 0]


def _calendar_title(block: Block) -> str:
    if block.kind == STUDY:
        return f"Study: {block.title}"
    if block.kind == TRAVEL:
        return f"Travel {block.title[:1].lower()}{block.title[1:]}"
    return block.title


# ------------------------------------------------------------------------------------------------ the service
class PlanService:
    def __init__(
        self,
        store: PlanStore | None = None,
        *,
        ai: Callable[[str, str], Any] | None = None,
        calendar: Callable[[], Any] | None = None,
        focus: Any = None,
        clock: Callable[[], float] = time.time,
        lock_name: str | None = LOCK_NAME,
    ) -> None:
        self.store = store or PlanStore()
        self._ai = ai  # (message, system prompt) -> the model's JSON
        self._calendar = calendar  # () -> the calendar tool, or None
        self.focus = focus  # the FocusService: round length, and "start a focus round" from a nudge
        self._clock = clock
        self._lock_name = lock_name
        self._lock = threading.RLock()
        self._listeners: list[Callable[[PlanNudge], None]] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def attach(self, core: Any) -> None:
        """Connect Miki's brain (to read requests) and the calendar.

        Reading a day into JSON needs a model that follows instructions well: tested live, gpt-4.1-nano dropped the
        gym and dinner from a request while gpt-4.1-mini got every item. It runs a few times a day, so it gets its
        own model (``MIKI_PLAN_MODEL``) on the same OpenAI connection, whatever /model picked for chat.
        """
        brain = getattr(core, "brain", None)
        if brain is not None and hasattr(brain, "generate_response"):
            model = os.getenv("MIKI_PLAN_MODEL", "").strip() or DEFAULT_PLAN_MODEL
            if getattr(brain, "model", model) != model and getattr(brain, "client", None) is not None:
                from app.brain.openai_client import OpenAIClient

                brain = OpenAIClient("", model, client=brain.client)
            self._ai = req.make_ai_reader(brain)
        if hasattr(core, "get_tool"):
            self._calendar = lambda: core.get_tool("calendar")

    # ------------------------------------------------------------------ lifecycle
    def add_listener(self, listener: Callable[[PlanNudge], None]) -> None:
        self._listeners.append(listener)

    def start(self) -> bool:
        """Start the nudge clock (one per machine, like the focus clock)."""
        if self._thread is not None and self._thread.is_alive():
            return True
        if self._lock_name and not single_instance.acquire_named(self._lock_name):
            logger.info("Day plans are handled by another Miki process")
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="miki-plan", daemon=True)
        self._thread.start()
        return True

    def close(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(TICK_SECONDS):
            try:
                self.tick()
            except Exception:
                logger.exception("Plan tick failed")

    def _now(self) -> datetime:
        return datetime.fromtimestamp(self._clock())

    # ------------------------------------------------------------------ making a plan
    def _current(self, today: date) -> tuple[str, dict[str, Any]] | None:
        """The plan you're working on: a waiting draft first, else the active one (today or later)."""
        for slot in ("draft", "active"):
            plan = self.store.get(slot)
            if plan and plan.get("day", "") >= today.isoformat():
                return slot, plan
        return None

    def make(self, text: str, *, fresh: bool = False) -> PlanReply:
        """``/plan <how you want your day to go>``: a draft, or a change to the plan you're working on."""
        text = (text or "").strip()[:2000]
        if not text:
            return PlanReply(False, USAGE)
        if self._ai is None:
            return PlanReply(False, "Planning needs the AI, and it isn't connected right now.")
        now = self._now()
        with self._lock:
            current = None if fresh else self._current(now.date())
            book = self.store.book()
            prompt = req.build_prompt(now, _routes_text(book), _durations_text(book), book.study_place or HOME,
                                      current[1].get("request") if current else None)
            try:
                request = req.parse(self._ai(text, prompt))
            except Exception:
                logger.exception("Plan: the AI couldn't read the request")
                return PlanReply(False, "I couldn't reach the AI to read that. Try again in a moment.")
            if request is None:
                return PlanReply(False, "I couldn't make a plan out of that.\n" + USAGE)
            learned = self._learn(request.facts)
            if not request.wishes and not request.skip:
                if learned:
                    return PlanReply(True, f"🧠 Noted: {learned}.")
                return PlanReply(False, "I didn't find anything to plan there.\n" + USAGE)
            plan = self._build(request, now, text, current[1] if current else None)
            self.store.put("draft", plan)
        text = plan_text(plan)
        if learned:
            text += f"\n🧠 Noted for next time: {learned}."
        return PlanReply(True, text, "draft")

    def draft_from_json(self, request_json: dict[str, Any], *, day: str = "today") -> PlanReply:
        """A draft straight from a saved request (the secretary's "same as usual", "plan tomorrow", "re-plan the rest"):
        no AI involved, so it is exact. ``day`` is "today" or "tomorrow"."""
        request = req.parse(request_json)
        if request is None or not (request.wishes or request.skip):
            return PlanReply(False, "I don't have anything to plan from.")
        request.day_offset = 1 if day == "tomorrow" else 0
        if request.day_offset:
            request.start = request.start if request.start is not None else None
        with self._lock:
            plan = self._build(request, self._now(), "", None)
            self.store.put("draft", plan)
        return PlanReply(True, plan_text(plan), "draft")

    def _task(self, wish: req.Wish, book: Book, notes: list[str], has_study: bool) -> Task:
        meal = req.meal_kind(wish.title)
        length = wish.minutes
        if length is None:
            known = book.durations.get(req.place_name(wish.title) or "") or (book.durations.get(wish.place) if wish.place else None)
            if known is not None:
                length = known
            elif wish.kind == FIXED and (meal is None or wish.ends_day):
                length = 0  # "be home by 8": a moment, not a stretch of time
            else:
                length = wish.estimate or MEAL_LENGTHS.get(meal or "") or DEFAULT_LENGTHS[wish.kind]
                if meal is None:
                    name = wish.title.lower()
                    notes.append(f"I guessed {span(length)} for {name}. If that's wrong: /plan remember {name} takes {length} min")
        at, after, during, before_study = wish.at, wish.after, wish.during_study, wish.before_study
        if wish.kind == MEAL and at is None and after is None and wish.before is None and not during and not before_study and not wish.after_study:
            if meal == "breakfast":
                before_study = True  # breakfast comes first
            elif meal == "dinner":
                after = DINNER_AFTER
            elif has_study:
                during = True  # lunch is the long break inside the study
            else:
                at = LUNCH_AT
        return Task(wish.kind, wish.title, length, wish.place, at, after, wish.before, during, wish.ends_day, wish.after_study, before_study)

    def _build(self, request: req.Request, now: datetime, text: str = "", previous: dict[str, Any] | None = None) -> dict[str, Any]:
        day = now.date() + timedelta(days=request.day_offset)
        book = self.store.book()
        notes: list[str] = []
        if request.day_offset == 0:
            earliest = -(-(now.hour * 60 + now.minute) // 5) * 5  # now, rounded up to 5 minutes
            start = max(earliest, request.start or 0)
        else:
            earliest = EARLIEST_START
            start = request.start if request.start is not None else DEFAULT_DAY_START
            if request.start is None:
                notes.append(f"I started the day at {hm(start)}. Say e.g. \"start at 9\" to change it.")
        start_place = request.start_place or HOME
        if request.start_place is None and request.day_offset == 0:
            notes.append("I assumed you're at home now. If not, say e.g. \"I'm at school\".")
        busy, calendar_ok, skipped = self._busy(day, request.skip)
        if not calendar_ok:
            notes.append("I couldn't read your calendar, so I didn't plan around what's in it.")
        elif request.skip:
            for term in request.skip:
                if any(term in w.title.lower() or (w.place and term == w.place) for w in request.wishes):
                    continue  # "no gym" misread as a skip of something Miki was asked to plan: not about the calendar
                if not any(_skip_matches(term, event["title"], event["start"]) for event in skipped):
                    notes.append(f"I couldn't find \"{term}\" in your calendar that day, so nothing was skipped for it.")
        has_study = any(w.kind == STUDY for w in request.wishes)
        tasks = [self._task(w, book, notes, has_study) for w in request.wishes]
        study_total = sum(t.minutes for t in tasks if t.kind == STUDY)
        if (study_total >= AUTO_LUNCH_STUDY and start < AUTO_LUNCH_BEFORE and not request.skip_lunch
                and not any(req.meal_kind(t.title) == "lunch" for t in tasks)):
            tasks.append(Task(MEAL, "Lunch", MEAL_LENGTHS["lunch"], during_study=True))
            notes.append("I added a 45 min lunch to your study day. Say \"no lunch\" if you don't want it.")
        config = getattr(self.focus, "config", None)
        rounds = dict(round_minutes=int(getattr(config, "minutes", 60) or 60), break_minutes=int(getattr(config, "break_minutes", 5) or 5))

        def lay(place: str) -> tuple[Day, Any]:
            plan = Day(tasks, start, start_place, place, HOME, busy=busy, routes=dict(book.routes), earliest=earliest, **rounds)
            return plan, plan_day(plan)

        usual = request.study_place or book.study_place or HOME
        said = bool(request.study_place and request.study_place in text.lower()) or bool((previous or {}).get("study_fixed"))
        plan_input, schedule = lay(usual)
        if study_total and usual != HOME and not said:  # common sense: is the trip worth it?
            home_input, home_schedule = lay(HOME)
            first_study = next((b.start for b in schedule.blocks if b.kind == STUDY), None)
            reason = None
            if not schedule.ok and home_schedule.ok:
                reason = f"studying at {usual} wouldn't fit your day"
            elif schedule.ok and home_schedule.ok:
                if schedule.travel - home_schedule.travel >= study_total:
                    reason = f"going to {usual} for {span(study_total)} of study would cost {span(schedule.travel - home_schedule.travel)} of travel"
                elif first_study is not None and first_study >= EVENING:
                    reason = "it's evening"
            if reason:
                plan_input, schedule = home_input, home_schedule
                notes.append(f"I kept your study at home: {reason}. Say \"study at {usual}\" if you'd rather go.")
        for a, b in schedule.guessed_routes:
            notes.append(f"I don't know how far {a} is from {b}, so I guessed {DEFAULT_TRAVEL} min. "
                         f"Tell me: /plan remember {a} to {b} is 25 min")
        return {
            "day": day.isoformat(), "created": round(self._clock(), 3), "status": "draft",
            "request": req.to_json(request), "study_fixed": said,
            "blocks": [b.to_dict() for b in schedule.blocks],
            "problems": schedule.problems, "ways": [text_ for text_, _ in ways_to_fit(plan_input, schedule)], "notes": notes,
            "study_minutes": schedule.study_minutes, "travel": schedule.travel, "home_at": schedule.home_at,
            "skipped": skipped, "calendar_ids": [], "nudged": [],
        }

    # ------------------------------------------------------------------ places you taught Miki
    def _learn(self, facts: req.Facts) -> str:
        if facts.empty:
            return ""
        book = self.store.book()
        said = []  # only what's new or changed: the model often repeats what it was told Miki knows
        for a, b, minutes in facts.travel:
            if book.routes.get(route_key(a, b)) != minutes:
                book.routes[route_key(a, b)] = minutes
                said.append(f"{a} ↔ {b} {span(minutes)}")
        for name, minutes in facts.durations.items():
            if book.durations.get(name) != minutes:
                book.durations[name] = minutes
                said.append(f"{name} {span(minutes)}")
        if facts.study_place and facts.study_place != book.study_place:
            book.study_place = facts.study_place
            said.append(f"you study at {facts.study_place}")
        if said:
            self.store.save_book(book)
        return ", ".join(said)

    def remember(self, text: str) -> PlanReply:
        """``/plan remember the gym is 15 min from school``: facts only, no plan."""
        if not (text or "").strip():
            return PlanReply(False, "What should I remember? For example: /plan remember the gym is 15 min from school, and I spend 1h15 there")
        if self._ai is None:
            return PlanReply(False, "That needs the AI, and it isn't connected right now.")
        book = self.store.book()
        prompt = req.build_prompt(self._now(), _routes_text(book), _durations_text(book), book.study_place or HOME, None)
        try:
            parsed = req.parse(self._ai("Only facts to learn, nothing to plan: " + text.strip()[:1000], prompt))
        except Exception:
            logger.exception("Plan: the AI couldn't read the facts")
            return PlanReply(False, "I couldn't reach the AI to read that. Try again in a moment.")
        learned = self._learn(parsed.facts) if parsed else ""
        if not learned:
            return PlanReply(False, "I didn't find a travel time or a usual length in that. Try: /plan remember the gym is 15 min from school")
        return PlanReply(True, f"🧠 Noted: {learned}.")

    def places_text(self) -> str:
        book = self.store.book()
        if not book.routes and not book.durations and not book.study_place:
            return ("I don't know your places yet. Teach me once, e.g.\n"
                    "/plan remember school and the gym are 50 min from home, the gym is 15 min from school, I spend 1h15 at the gym, and I study at school")
        lines = ["What I know about your places:"]
        lines += [f"🚶 {a} ↔ {b}: {span(m)}" for (a, b), m in sorted(book.routes.items())]
        lines += [f"⏱ {name}: {span(m)}" for name, m in sorted(book.durations.items())]
        lines.append(f"📚 You study at: {book.study_place or HOME}")
        lines.append("Change anything with /plan remember …")
        return "\n".join(lines)

    # ------------------------------------------------------------------ the calendar
    def _calendar_tool(self) -> Any:
        try:
            tool = self._calendar() if self._calendar is not None else None
            return tool if tool is not None and tool.is_available() else None
        except Exception:
            logger.debug("Plan: calendar unavailable", exc_info=True)
            return None

    def _busy(self, day: date, skip: list[str] | None = None) -> tuple[list[tuple[int, int, str]], bool, list[dict[str, Any]]]:
        """What's already in the calendar that day, as (start, end, title) in minutes. Miki's own plan blocks don't count,
        and neither do events you said you'd skip (a lecture on a read-only calendar can't be deleted, so it's set aside
        here and ``confirm`` marks the slot on your own calendar). Third value: the skipped events, as plan data."""
        tool = self._calendar_tool()
        if tool is None:
            return [], self._calendar is None, []  # no calendar set up at all isn't worth a warning
        midnight = datetime.combine(day, dtime.min).astimezone()
        try:
            result = tool.execute("get_events", {"start": midnight.isoformat(), "end": (midnight + timedelta(days=1)).isoformat()})
        except Exception:
            logger.warning("Plan: reading the calendar failed", exc_info=True)
            return [], False, []
        if not result.success:
            return [], False, []
        active = self.store.get("active") or {}
        own = set(active.get("calendar_ids") or [])
        busy: list[tuple[int, int, str]] = []
        skipped: list[dict[str, Any]] = []
        for event in (result.data or {}).get("events", []):
            if event.get("all_day") or event.get("id") in own or MARKER in str(event.get("description") or ""):
                continue
            try:
                start, end = _minute_of(event["start"], day), _minute_of(event["end"], day)
            except (KeyError, TypeError, ValueError):
                continue
            start, end = max(0, start), min(24 * 60, end)
            if end <= start:
                continue
            title = str(event.get("title") or "Busy")[:60]
            if any(_skip_matches(term, title, start) for term in skip or []):
                skipped.append({"title": title, "start": start, "end": end})
            else:
                busy.append((start, end, title))
        return busy, True, skipped

    def _add_to_calendar(self, plan: dict[str, Any]) -> tuple[list[str], str]:
        tool = self._calendar_tool()
        if tool is None:
            return [], "Your calendar isn't connected, so the plan lives in Miki only."
        day = date.fromisoformat(plan["day"])
        ids, failed = [], 0
        for block in calendar_blocks([Block.from_dict(b) for b in plan["blocks"]]):
            arguments = {"title": _calendar_title(block), "start": _iso(day, block.start), "end": _iso(day, block.end),
                         "description": MARKER, "confirm": True}  # you confirmed the whole plan with "Looks good"
            try:
                result = tool.execute("create_event", arguments)
                event_id = ((result.data or {}).get("event") or {}).get("id") if result.success else None
            except Exception:
                logger.warning("Plan: adding a calendar event failed", exc_info=True)
                event_id = None
            if event_id:
                ids.append(str(event_id))
            else:
                failed += 1
        for event in plan.get("skipped") or []:  # the lecture stays on its read-only calendar: mark it skipped on yours
            arguments = {"title": f"Skipped: {event['title']}", "start": _iso(day, event["start"]), "end": _iso(day, event["end"]),
                         "description": MARKER + " You said you'd skip this.", "confirm": True}
            try:
                result = tool.execute("create_event", arguments)
                event_id = ((result.data or {}).get("event") or {}).get("id") if result.success else None
            except Exception:
                logger.warning("Plan: marking a skipped event failed", exc_info=True)
                event_id = None
            if event_id:
                ids.append(str(event_id))
            else:
                failed += 1
        if not ids:
            return [], "I couldn't add it to your calendar, so the plan lives in Miki only."
        text = f"Added {len(ids)} block{'s' if len(ids) != 1 else ''} to your calendar."
        return ids, text + (f" {failed} didn't go in." if failed else "")

    def _remove_from_calendar(self, plan: dict[str, Any] | None) -> int:
        """Take a replaced or cancelled plan's events out of the calendar (only the ones Miki added)."""
        ids = (plan or {}).get("calendar_ids") or []
        tool = self._calendar_tool() if ids else None
        removed = 0
        for event_id in ids if tool is not None else []:
            try:
                if tool.execute("delete_event", {"event_id": str(event_id), "confirm": True}).success:
                    removed += 1
            except Exception:
                logger.warning("Plan: removing a calendar event failed", exc_info=True)
        return removed

    # ------------------------------------------------------------------ OK, cancel, show
    def confirm(self) -> PlanReply:
        """"Looks good": the draft becomes the day's plan, goes into the calendar, and the nudges start."""
        today = self._now().date().isoformat()
        with self._lock:
            draft = self.store.get("draft")
            if not draft or draft.get("day", "") < today:
                return PlanReply(False, "There's no plan waiting for your OK. " + USAGE)
            replaced = self.store.get("active")
            if replaced and replaced.get("day") == draft["day"]:  # the same day re-planned: swap its calendar blocks
                self._remove_from_calendar(replaced)  # (tomorrow's plan never wipes today's from the calendar)
            draft["status"] = "active"
            draft["calendar_ids"], calendar_note = self._add_to_calendar(draft)
            self.store.put("active", draft)
            self.store.put("draft", None)
        nudges = "I'll nudge you when it's time to leave and when each subject starts." if draft["day"] == today else "I'll nudge you through the day."
        return PlanReply(True, f"✅ Plan locked in. {calendar_note} {nudges}", "active")

    def cancel(self) -> PlanReply:
        with self._lock:
            draft, active = self.store.get("draft"), self.store.get("active")
            if not draft and not active:
                return PlanReply(False, "There's no plan to cancel.")
            removed = self._remove_from_calendar(active)
            self.store.put("draft", None)
            self.store.put("active", None)
        tail = f" I took {removed} block{'s' if removed != 1 else ''} out of your calendar." if removed else ""
        return PlanReply(True, "Plan cancelled." + tail)

    def discard_draft(self) -> PlanReply:
        """Drop the draft only (the day's plan, if there is one, stays)."""
        with self._lock:
            if not self.store.get("draft"):
                return PlanReply(False, "There's no draft to discard.")
            self.store.put("draft", None)
        return PlanReply(True, "Okay, I threw that draft away." + (" Your day's plan is unchanged." if self.store.get("active") else ""))

    def show(self) -> PlanReply:
        now = self._now()
        current = self._current(now.date())
        if current is None:
            return PlanReply(False, "No plan yet. " + USAGE)
        slot, plan = current
        minute = now.hour * 60 + now.minute if plan["day"] == now.date().isoformat() and slot == "active" else None
        return PlanReply(True, plan_text(plan, minute), slot)

    def command(self, argument: str) -> str:
        """Run ``/plan <argument>`` as typed text (the dashboard; the phone uses the same calls, with buttons)."""
        word, _, rest = argument.strip().partition(" ")
        lowered = argument.strip().lower()
        if not lowered or lowered in {"show", "today", "status"}:
            reply = self.show()
        elif lowered in OK_WORDS:
            reply = self.confirm()
        elif lowered in CANCEL_WORDS:
            reply = self.cancel()
        elif lowered in {"places", "place"}:
            return self.places_text()
        elif word.lower() in {"remember", "learn"}:
            reply = self.remember(rest)
        elif word.lower() == "new":
            reply = self.make(rest, fresh=True)
        else:
            reply = self.make(argument)
        return reply.text + ("\n\n" + DRAFT_HINT if reply.status == "draft" else "")

    # ------------------------------------------------------------------ during the day
    def focus_for(self, index: int) -> tuple[int, str] | None:
        """(minutes, goal) for a focus round on study block ``index`` of today's plan."""
        plan = self.store.get("active")
        if not plan or plan.get("day") != self._now().date().isoformat():
            return None
        blocks = plan.get("blocks") or []
        if not 0 <= index < len(blocks) or blocks[index].get("kind") != STUDY:
            return None
        block = Block.from_dict(blocks[index])
        return max(5, min(240, block.minutes)), block.title

    def tick(self) -> list[PlanNudge]:
        """Send what's due: called every few seconds by the plan clock. Returns what it sent (for tests)."""
        now = self._now()
        with self._lock:
            plan = self.store.get("active")
            if not plan or plan.get("day") != now.date().isoformat():
                return []
            minute = now.hour * 60 + now.minute
            blocks = [Block.from_dict(b) for b in plan.get("blocks") or []]
            nudged = set(plan.get("nudged") or [])
            due: list[PlanNudge] = []
            for index, block in enumerate(blocks):
                at = block.start - (LEAVE_LEAD if block.kind == TRAVEL else 0)
                if index in nudged or at > minute:
                    continue
                nudged.add(index)
                if minute - at <= STALE_MINUTES:
                    nudge = self._nudge(plan, blocks, index)
                    if nudge is not None:
                        due.append(nudge)
            if len(nudged) != len(plan.get("nudged") or []):
                plan["nudged"] = sorted(nudged)
                self.store.put("active", plan)
        for nudge in due:
            self._notify(nudge)
        return due

    def _nudge(self, plan: dict[str, Any], blocks: list[Block], index: int) -> PlanNudge | None:
        block = blocks[index]
        key = f"plan:{plan['day']}:{plan.get('created', 0)}:{index}"
        upcoming = next((b for b in blocks[index + 1:] if b.kind not in {BREAK, FREE, TRAVEL}), None)
        before = next((b for b in reversed(blocks[:index]) if b.kind not in {BREAK, FREE}), None)
        if block.kind == TRAVEL:
            text = f"🚶 Time to leave for {block.place} at {hm(block.start)}: {span(block.minutes)}, you'll be there at {hm(block.end)}."
            if upcoming is not None:
                text += f" Next: {upcoming.title} at {hm(max(upcoming.start, block.end))}."
            return PlanNudge(text, key)
        if block.kind == STUDY:
            if before is not None and before.kind == STUDY and before.title == block.title:
                return None  # the same subject carrying on after a short break: focus mode already calls those
            if self.focus is not None and getattr(self.focus, "is_focusing", False):
                return None
            until = next(line.end for first, line, _ in runs(blocks) if first == index)
            nudge = PlanNudge(f"📚 {block.title} now, until {hm(until)}.", key)
            if self.focus is not None:
                nudge.focus_block, nudge.focus_minutes, nudge.focus_goal = index, max(5, min(240, block.minutes)), block.title
            return nudge
        if block.kind == MEAL:
            text = f"🍽 {block.title} now, {span(block.minutes)}."
            return PlanNudge(text + (f" Then {upcoming.title} at {hm(block.end)}." if upcoming is not None else ""), key)
        if block.kind == ACTIVITY and (before is None or before.kind != TRAVEL):  # after a trip, "time to leave" said it
            return PlanNudge(f"{_icon(block)} {block.title} now, until {hm(block.end)}.", key)
        return None

    def _notify(self, nudge: PlanNudge) -> None:
        """Tell every listener, never letting a slow one (a hanging Telegram send) hold up the next nudge for long."""
        def call(listener: Callable[[PlanNudge], None]) -> None:
            try:
                listener(nudge)
            except Exception:
                logger.exception("Plan listener failed")

        for listener in list(self._listeners):
            worker = threading.Thread(target=call, args=(listener,), name="miki-plan-notify", daemon=True)
            worker.start()
            worker.join(LISTENER_WAIT_SECONDS)


# ------------------------------------------------------------------------------------------------ helpers
def _minute_of(value: str, day: date) -> int:
    """An ISO timestamp from the calendar -> minutes since ``day``'s local midnight (may be < 0 or > 1440)."""
    moment = datetime.fromisoformat(str(value))
    if moment.tzinfo is not None:
        moment = moment.astimezone().replace(tzinfo=None)  # the process's local time (MIKI_TIMEZONE on the server)
    return int((moment - datetime.combine(day, dtime.min)).total_seconds() // 60)


def _skip_matches(term: str, title: str, start: int) -> bool:
    """Does "skip ``term``" mean this event? A "HH:MM" term matches the start time; otherwise every word of the term
    must be in the title ("lecture" matches "MATH101 Lecture"; "linear algebra" matches "Linear Algebra Lecture")."""
    at = req.clock(term)
    if at is not None:
        return at == start
    words = term.lower().split()
    return bool(words) and all(word in title.lower() for word in words)


def _iso(day: date, minute: int) -> str:
    return (datetime.combine(day, dtime.min) + timedelta(minutes=minute)).astimezone().isoformat(timespec="seconds")


def _routes_text(book: Book) -> str:
    return ", ".join(f"{a}-{b} {m}" for (a, b), m in sorted(book.routes.items()))


def _durations_text(book: Book) -> str:
    return ", ".join(f"{name} {m}" for name, m in sorted(book.durations.items()))


def build_plan_service(core: Any = None, *, focus: Any = None, **kwargs: Any) -> PlanService | None:
    """The day planner, or None when switched off (``MIKI_PLAN=0``)."""
    if os.getenv("MIKI_PLAN", "1").strip().lower() in {"0", "false", "no", "off"}:
        return None
    service = PlanService(focus=focus, **kwargs)
    if core is not None:
        service.attach(core)
    return service
