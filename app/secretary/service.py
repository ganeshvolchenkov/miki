"""The secretary: Miki speaks first, at the right moments, about your day.

    morning     your day (calendar, when to leave, deadlines coming up, urgent mail) and one tap to plan it
    midday      "you're an hour behind your plan": one tap to re-plan what's left of today
    heads-up    time to leave for a class / something starts in 15 minutes
    evening     what you planned vs did, what carries over, tomorrow, one tap to plan tomorrow with the leftovers
    sunday      your week in numbers and what next week holds

Everything is worked out by code from your calendar, your focus rounds and your plan; no AI writes these (so no invented
facts) and the plans it offers are built from saved requests, so they are exact. It sends little on purpose: a briefing, a
check-in, at most two catch-ups and six heads-ups a day. Switch it all off with the Secretary setting on the phone
(or ``MIKI_SECRETARY=0``). State: ``data/secretary.json``.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from app.plan.schedule import STUDY, TRAVEL, Block, hm, span
from app.secretary import radar

logger = logging.getLogger(__name__)

CALENDAR_DAYS = 15  # how far ahead the calendar is read (deadlines within two weeks)
EVENT_CACHE_SECONDS = 300
BRIEF_WINDOW = timedelta(hours=3)  # a late briefing is still sent, a stale one (after lunch) isn't
EVENING_WINDOW = timedelta(hours=2, minutes=30)
WEEKLY_HOUR = 18  # Sunday
CATCHUP_GAP = 45  # minutes behind the plan before it says anything
CATCHUP_FROM, CATCHUP_UNTIL = 9 * 60, 20 * 60
MAX_CATCHUPS, CATCHUP_SPACING = 2, 2 * 3600
HEADS_UP = 15  # minutes before an event
LEAVE_LEAD = 5  # "time to leave" goes out this long before you must set off
MAX_ALERTS_PER_DAY = 6
RETRY_SECONDS = 300  # a notice that couldn't be delivered (focus, offline) is tried again after this
MIN_LEFTOVER = 15  # less than this unfinished isn't carried over
KEEP_DAYS = 14


@dataclass
class Notice:
    text: str
    key: str
    actions: list[tuple[str, str]] = field(default_factory=list)  # (button label, action id) -> the phone's "sec:<id>"
    respect_quiet: bool = False  # alerts wait out quiet hours; the briefing and check-in are at times you chose
    expires: float | None = None  # epoch seconds after which it is pointless (a "leave now" for an event that started)
    after: Callable[[], None] | None = None  # runs once it was actually delivered


@dataclass
class Action:
    """What a tapped button leads to: some text, a plan draft to show (with the plan's own buttons), or a question to ask."""

    text: str = ""
    reply: Any = None
    ask: str = ""


def _clock_text(value: Any) -> tuple[int, int] | None:
    try:
        hour, minute = (int(part) for part in str(value).split(":"))
        return (hour, minute) if 0 <= hour < 24 and 0 <= minute < 60 else None
    except ValueError:
        return None


class SecretaryService:
    def __init__(
        self,
        path: str | Path = "data/secretary.json",
        *,
        calendar: Callable[[date, int], list[dict[str, Any]] | None] | None = None,
        weather: Callable[[], str] | None = None,
        urgent_mail: Callable[[], list[Any]] | None = None,
        focus: Any = None,
        plan: Any = None,
        name: Callable[[], str] | None = None,
        pref: Callable[[str], Any] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.path = Path(path)
        self._calendar = calendar  # (first day, how many days) -> calendar events, or None when it can't be read
        self._weather = weather
        self._urgent_mail = urgent_mail
        self.focus = focus
        self.plan = plan
        self._name = name
        self._pref = pref or (lambda key: None)
        self._clock = clock
        self._lock = threading.RLock()
        self._listeners: list[Callable[[Notice], bool]] = []
        self._cache: tuple[float, list[radar.Event]] | None = None
        self._data = self._load()

    # ------------------------------------------------------------------ storage
    def _load(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            logger.warning("secretary.json is unreadable; keeping it as secretary.corrupt and starting fresh")
            try:
                self.path.replace(self.path.with_suffix(".corrupt"))
            except OSError:
                pass
        return {}

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_suffix(".tmp")
            temp.write_text(json.dumps(self._data, ensure_ascii=False), encoding="utf-8")
            temp.replace(self.path)
        except OSError:
            logger.warning("Could not save the secretary's state", exc_info=True)

    def _section(self, name: str) -> dict[str, Any]:
        if not isinstance(self._data.get(name), dict):
            self._data[name] = {}
        return self._data[name]

    def add_listener(self, listener: Callable[[Notice], bool]) -> None:
        """``listener(notice) -> True`` if it was delivered; False means "not now" and it is tried again soon."""
        self._listeners.append(listener)

    def _now(self) -> datetime:
        return datetime.fromtimestamp(self._clock())

    def _on(self, name: str, default: bool = True) -> bool:
        value = self._pref(name)
        return default if value is None else bool(value)

    # ------------------------------------------------------------------ what the secretary can see
    def _active_today(self, now: datetime) -> dict[str, Any] | None:
        plan = self.plan.store.get("active") if self.plan is not None else None
        return plan if plan and plan.get("day") == now.date().isoformat() else None

    def _events(self, now: datetime) -> list[radar.Event] | None:
        """The calendar from today on (cached a few minutes), or None if it can't be read."""
        if self._calendar is None:
            return None
        stamp = self._clock()
        if self._cache is not None and stamp - self._cache[0] < EVENT_CACHE_SECONDS:
            return self._cache[1]
        try:
            raw = self._calendar(now.date(), CALENDAR_DAYS)
        except Exception:
            logger.debug("Secretary: calendar failed", exc_info=True)
            return None
        if raw is None:
            return None
        active = self.plan.store.get("active") if self.plan is not None else None
        events = radar.parse_events(raw, (active or {}).get("skipped"))
        self._cache = (stamp, events)
        return events

    def _routes(self) -> tuple[dict[tuple[str, str], int], str]:
        if self.plan is None:
            return {}, radar.HOME
        book = self.plan.store.book()
        return dict(book.routes), book.study_place or radar.HOME

    def _planned(self, plan: dict[str, Any] | None) -> dict[str, int]:
        """Study minutes by subject in a plan."""
        totals: dict[str, int] = {}
        for raw in (plan or {}).get("blocks") or []:
            block = Block.from_dict(raw)
            if block.kind == STUDY:
                totals[block.title] = totals.get(block.title, 0) + block.minutes
        return totals

    def _done(self, day: date, subjects: list[str]) -> tuple[dict[str, int], int]:
        """Focus minutes on ``day`` by planned subject (a round's goal is the subject), and what matched none."""
        done: dict[str, int] = {}
        other = 0
        if self.focus is None:
            return done, other
        for entry in self.focus.session.rounds_on(day):
            minutes = int(round(float(entry.get("minutes", 0) or 0)))
            goal = str(entry.get("goal") or "").strip().lower()
            subject = next((s for s in subjects if goal and (goal == s.lower() or goal in s.lower() or s.lower() in goal)), None)
            if subject is None:
                other += minutes
            else:
                done[subject] = done.get(subject, 0) + minutes
        return done, other

    def _name_text(self) -> str:
        try:
            return (self._name() if self._name else "") or ""
        except Exception:
            return ""

    # ------------------------------------------------------------------ the tick
    def tick(self, now: datetime | None = None) -> int:
        """Send what's due. Called every minute or so by the phone's watcher. Returns how many notices were delivered."""
        if not self._on("secretary"):
            return 0
        now = now or self._now()
        sent = 0
        try:
            self._remember_usual(now)
        except Exception:
            logger.exception("Secretary: remembering the usual day failed")
        for builder in (self._morning, self._evening, self._weekly, self._catchup, self._alerts):
            try:
                for notice in builder(now):
                    sent += self._send(notice)
            except Exception:
                logger.exception("Secretary: %s failed", builder.__name__)
        self._prune(now)
        return sent

    def _send(self, notice: Notice) -> int:
        with self._lock:
            sent = self._section("sent")
            retry = self._section("retry")
            if notice.key in sent or self._clock() < float(retry.get(notice.key, 0)):
                return 0
        if notice.expires is not None and self._clock() > notice.expires:
            return 0
        delivered = False
        for listener in list(self._listeners):
            try:
                delivered = bool(listener(notice)) or delivered
            except Exception:
                logger.exception("Secretary listener failed")
        with self._lock:
            if delivered:
                self._section("sent")[notice.key] = self._clock()
                self._section("retry").pop(notice.key, None)
                if notice.after is not None:
                    notice.after()
            else:
                self._section("retry")[notice.key] = self._clock() + RETRY_SECONDS
            self._save()
        return 1 if delivered else 0

    def _prune(self, now: datetime) -> None:
        cutoff = self._clock() - KEEP_DAYS * 86400
        with self._lock:
            sent = self._section("sent")
            old = [key for key, stamp in sent.items() if float(stamp) < cutoff]
            for key in old:
                del sent[key]
            if old:
                self._save()

    def _in_window(self, now: datetime, at: Any, window: timedelta) -> bool:
        clock = _clock_text(at)
        if clock is None:
            return False
        due = now.replace(hour=clock[0], minute=clock[1], second=0, microsecond=0)
        return due <= now <= min(due + window, now.replace(hour=23, minute=59, second=59))

    # ------------------------------------------------------------------ remembering your usual day
    def _remember_usual(self, now: datetime) -> None:
        """The day you actually locked in is what "same as usual" repeats."""
        plan = self._active_today(now)
        if not plan or not plan.get("request"):
            return
        with self._lock:
            usual = self._section("usual")
            if usual.get("created") != plan.get("created"):
                usual.update({"created": plan.get("created"), "request": deepcopy(plan["request"])})
                self._save()

    # ------------------------------------------------------------------ morning
    def _morning(self, now: datetime) -> list[Notice]:
        if not self._on("morning_brief") or not self._in_window(now, self._pref("brief_time") or "07:30", BRIEF_WINDOW):
            return []
        key = f"brief:{now.date().isoformat()}"
        if key in self._section("sent"):
            return []
        return [self.build_brief(now, key)]

    def build_brief(self, now: datetime | None = None, key: str = "") -> Notice:
        now = now or self._now()
        today = now.date()
        events = self._events(now)
        plan = self._active_today(now)
        routes, study_place = self._routes()
        name = self._name_text()
        lines = [f"☀️ Good morning{', ' + name if name else ''}", f"{today.strftime('%A')} {today.day} {today.strftime('%B')}"]
        try:
            weather = (self._weather() if self._weather else "") or ""
        except Exception:
            weather = ""
        if weather:
            lines[1] += f" · {weather}"

        todays = radar.on_day(events or [], today)
        timed = [e for e in todays if not e.all_day]
        lines += ["", "📅 Today"]
        if events is None:
            lines.append("I couldn't read your calendar just now.")
        elif not todays:
            lines.append("Nothing in your calendar.")
        for event in todays[:8]:
            when = "All day" if event.all_day else f"{event.start:%H:%M}–{event.end:%H:%M}"
            lines.append(f"{when:<11} {event.title}")
            leave = radar.leave_time(event, routes, study_place)
            if leave is not None and event.start > now:
                lines.append(f"            🚶 leave by {leave[0]:%H:%M} ({leave[1]} min)")
        if len(todays) > 8:
            lines.append(f"…and {len(todays) - 8} more")

        if plan:
            blocks = [Block.from_dict(b) for b in plan.get("blocks") or []]
            first = next((b for b in blocks if b.kind == STUDY and b.end > now.hour * 60 + now.minute), None)
            lines += ["", f"📋 You already have a plan for today: {span(plan.get('study_minutes', 0))} of study" +
                      (f", starting {hm(first.start)} with {first.title}." if first else ".")]
        else:
            lines += ["", "📋 No plan for today yet."]

        coming = radar.deadlines(events or [], today)
        if coming:
            lines += ["", "📌 Coming up"]
            for event, days in coming[:5]:
                lines.append(f"{'⚠️' if days <= 2 else '•'} {event.title}: {radar.days_text(days, event.start.date())}")

        urgent = self._urgent()
        if urgent:
            lines += ["", f"🔴 {len(urgent)} urgent email{'s' if len(urgent) != 1 else ''}"]
            lines += [f"• {str(getattr(i, 'sender_name', ''))[:24]}: {str(getattr(i, 'subject', ''))[:50]}" for i in urgent[:3]]

        carry = self._carry(today - timedelta(days=1))
        if carry and not plan:
            lines += ["", "↪️ Left over from yesterday: " + ", ".join(f"{s} {span(m)}" for s, m in carry.items())]

        note = self._suggestion(todays, timed, coming, plan, carry)
        if note:
            lines += ["", f"💡 {note}"]

        actions: list[tuple[str, str]] = []
        if not plan:
            actions.append(("📋 Plan my day", "plan"))
            if self._section("usual").get("request"):
                actions.append(("🔁 Same as usual", "again"))
        return Notice("\n".join(lines), key or f"brief:{today.isoformat()}", actions)

    def _urgent(self) -> list[Any]:
        try:
            return list(self._urgent_mail()) if self._urgent_mail else []
        except Exception:
            logger.debug("Secretary: mail failed", exc_info=True)
            return []

    @staticmethod
    def _suggestion(todays: list[radar.Event], timed: list[radar.Event], coming: list[tuple[radar.Event, int]],
                    plan: dict[str, Any] | None, carry: dict[str, int]) -> str:
        """One practical sentence, or nothing (never filler)."""
        close = next(((e, d) for e, d in coming if d <= 2), None)
        if close is not None:
            event, days = close
            return f"{event.title} is {radar.days_text(days, event.start.date())}: worth making today a study-heavy one."
        busy = sum(int((e.end - e.start).total_seconds() // 60) for e in timed)
        if busy >= 4 * 60:
            return f"{span(busy)} of classes and events today, so I'd keep the studying short and put it around them."
        if not plan and carry:
            return "Yesterday's leftovers fit in nicely; I can plan today around them."
        if not plan and not timed:
            return "Your calendar is empty, a good day to get ahead. Want me to plan it?"
        return ""

    # ------------------------------------------------------------------ evening
    def _carry(self, day: date) -> dict[str, int]:
        carry = self._section("carry")
        return dict(carry.get("items") or {}) if carry.get("day") == day.isoformat() else {}

    def _evening(self, now: datetime) -> list[Notice]:
        if not self._on("evening_review") or not self._in_window(now, self._pref("evening_time") or "21:00", EVENING_WINDOW):
            return []
        today = now.date()
        key = f"evening:{today.isoformat()}"
        if key in self._section("sent"):
            return []
        if self.focus is not None and getattr(self.focus, "is_focusing", False):
            return []  # not in the middle of a round
        plan = self._active_today(now)
        planned = self._planned(plan)
        done, other = self._done(today, list(planned))
        focused = sum(done.values()) + other
        events = self._events(now) or []
        tomorrow = today + timedelta(days=1)
        tomorrows = [e for e in radar.on_day(events, tomorrow)]
        coming = [(e, d) for e, d in radar.deadlines(events, today, 5) if d >= 1]  # days left, counted from today
        if not plan and not focused and not tomorrows and not coming:
            return []  # nothing happened and nothing's coming: no message for the sake of a message

        lines = ["🌙 Evening check-in"]
        carry: dict[str, int] = {}
        if planned:
            total_planned, total_done = sum(planned.values()), sum(done.values())
            lines += ["", f"📚 Study: {span(focused)} focused of {span(total_planned)} planned"]
            for subject, minutes in planned.items():
                got = done.get(subject, 0)
                left = max(0, minutes - got)
                lines.append(f"• {subject}: {span(got)} of {span(minutes)}" + (f" → {span(left)} left" if left >= MIN_LEFTOVER else " ✓"))
                if left >= MIN_LEFTOVER:
                    carry[subject] = left
            ratio = total_done / total_planned if total_planned else 1
            lines += ["", "🎉 You hit your plan, well done." if ratio >= 1 else "👍 A solid day." if ratio >= 0.7 else
                      "It slipped today. That happens: I can fit what's left into tomorrow." if focused else
                      "Today didn't go to plan. Want to start fresh tomorrow?"]
        elif focused:
            lines += ["", f"📚 You focused for {span(focused)} today."]
        if tomorrows or coming:
            lines += ["", f"🗓 Tomorrow ({tomorrow.strftime('%A')})"]
            routes, study_place = self._routes()
            for event in tomorrows[:6]:
                when = "All day" if event.all_day else f"{event.start:%H:%M}"
                leave = radar.leave_time(event, routes, study_place)
                lines.append(f"{when:<8} {event.title}" + (f" · leave by {leave[0]:%H:%M}" if leave else ""))
            if not tomorrows:
                lines.append("Nothing in your calendar.")
            for event, days in coming[:3]:
                lines.append(f"{'⚠️' if days <= 2 else '📌'} {event.title}: {radar.days_text(days, event.start.date())}")
        actions = [("📋 Plan tomorrow" + (" with the leftovers" if carry else ""), "tomorrow"), ("👍 Not now", "dismiss")]

        def remember_carry() -> None:
            self._section("carry").update({"day": today.isoformat(), "items": carry})

        return [Notice("\n".join(lines), key, actions, after=remember_carry)]

    # ------------------------------------------------------------------ the week
    def _weekly(self, now: datetime) -> list[Notice]:
        if not self._on("weekly_review") or self.focus is None or now.weekday() != 6 or now.hour < WEEKLY_HOUR:
            return []
        iso = now.isocalendar()
        key = f"weekly:{iso[0]}-{iso[1]}"
        if key in self._section("sent"):
            return []
        today = now.date()
        history = self.focus.session.history()
        per_day: dict[str, float] = {}
        subjects: dict[str, float] = {}
        for entry in history:
            per_day[entry.get("day", "")] = per_day.get(entry.get("day", ""), 0) + float(entry.get("minutes", 0) or 0)
        week = [today - timedelta(days=6 - i) for i in range(7)]
        minutes = [int(per_day.get(d.isoformat(), 0)) for d in week]
        before = sum(int(per_day.get((today - timedelta(days=7 + i)).isoformat(), 0)) for i in range(7))
        week_days = {d.isoformat() for d in week}
        for entry in history:
            goal = str(entry.get("goal") or "").strip()
            if entry.get("day") in week_days and goal:
                subjects[goal.title()] = subjects.get(goal.title(), 0) + float(entry.get("minutes", 0) or 0)
        events = self._events(now) or []
        ahead = [e for e in events if today < e.start.date() <= today + timedelta(days=7)]
        coming = [(e, d) for e, d in radar.deadlines(events, today, 8) if d >= 1]
        total = sum(minutes)
        if not total and not ahead:
            return []
        top = max(minutes) or 1
        lines = ["📊 Your week", ""]
        for day, got in zip(week, minutes):
            lines.append(f"{day.strftime('%a')} {'█' * round(10 * got / top):<10} {span(got) if got else '–'}")
        lines += ["", f"Total {span(total)}" + (f" ({'+' if total >= before else '−'}{span(abs(total - before))} vs the week before)" if before else "")]
        streak = self.focus.session.streak(today)
        if streak > 1:
            lines.append(f"🔥 {streak}-day focus streak")
        if subjects:
            subject, got = max(subjects.items(), key=lambda pair: pair[1])
            lines.append(f"Most time on: {subject} ({span(int(got))})")
        if ahead or coming:
            lines += ["", f"🗓 Next week: {len(ahead)} event{'s' if len(ahead) != 1 else ''}"]
            lines += [f"{'⚠️' if days <= 2 else '📌'} {event.title}: {radar.days_text(days, event.start.date())}" for event, days in coming[:4]]
        return [Notice("\n".join(lines), key, [("📋 Plan tomorrow", "tomorrow")])]

    # ------------------------------------------------------------------ catching up
    def _catchup(self, now: datetime) -> list[Notice]:
        if not self._on("catchup") or self.focus is None or getattr(self.focus, "is_focusing", False):
            return []
        plan = self._active_today(now)
        minute = now.hour * 60 + now.minute
        if not plan or not CATCHUP_FROM <= minute <= CATCHUP_UNTIL:
            return []
        stats = self.focus.session.stats()
        if stats.get("week_minutes", 0) <= 0:
            return []  # you don't track studying with focus rounds, so there's nothing to compare
        expected = sum(b.minutes for b in (Block.from_dict(r) for r in plan.get("blocks") or []) if b.kind == STUDY and b.end <= minute - 10)
        done = int(stats.get("today_minutes", 0))
        gap = expected - done
        if gap < CATCHUP_GAP:
            return []
        state = self._section("catchups")
        count = int(state.get("n", 0)) if state.get("day") == now.date().isoformat() else 0
        if count >= MAX_CATCHUPS or self._clock() - float(state.get("last", 0)) < CATCHUP_SPACING:
            return []
        text = (f"⏱ You're about {span(gap)} behind your plan: by now it had {span(expected)} of study, and I count {span(done)} of focus rounds. "
                "Want me to re-plan what's left of the day?")

        def count_it() -> None:
            self._section("catchups").update({"day": now.date().isoformat(), "n": count + 1, "last": self._clock()})

        return [Notice(text, f"catchup:{now.date().isoformat()}:{count + 1}", [("🔄 Re-plan the rest", "replan"), ("👍 I'm fine", "dismiss")], after=count_it)]

    # ------------------------------------------------------------------ alerts
    def _alerts(self, now: datetime) -> list[Notice]:
        if not self._on("leave_alerts"):
            return []
        events = self._events(now)
        if not events:
            return []
        state = self._section("alerts")
        today = now.date().isoformat()
        count = int(state.get("n", 0)) if state.get("day") == today else 0
        if count >= MAX_ALERTS_PER_DAY:
            return []
        plan = self._active_today(now)
        travel_ends = [b.end for b in (Block.from_dict(r) for r in (plan or {}).get("blocks") or []) if b.kind == TRAVEL]
        routes, study_place = self._routes()
        notices: list[Notice] = []
        for event in radar.on_day(events, now.date()):
            if event.all_day or event.start <= now:
                continue
            covered = any(0 <= event.minute - end <= 20 for end in travel_ends)  # the plan already tells you when to leave
            leave = radar.leave_time(event, routes, study_place)
            if covered:
                continue
            if leave is not None:
                if now >= leave[0] - timedelta(minutes=LEAVE_LEAD):
                    text = (f"🚶 Time to head out for {event.title} at {event.start:%H:%M}: it's about {leave[1]} min away, "
                            f"so leave by {leave[0]:%H:%M}.")
                    notices.append(self._alert(event, "leave", text))
            elif event.start - now <= timedelta(minutes=HEADS_UP):
                where = f" at {event.location}" if event.location else ""
                notices.append(self._alert(event, "soon", f"⏰ In {max(1, int((event.start - now).total_seconds() // 60))} min: {event.title}{where} ({event.start:%H:%M})."))
        room = MAX_ALERTS_PER_DAY - count
        notices = notices[:room]
        for notice in notices:
            notice.after = self._counter(today)
        return notices

    @staticmethod
    def _alert(event: radar.Event, kind: str, text: str) -> Notice:
        return Notice(text, f"{kind}:{event.id or event.title}:{event.start.isoformat()}", respect_quiet=True, expires=event.start.timestamp())

    def _counter(self, today: str) -> Callable[[], None]:
        def bump() -> None:
            state = self._section("alerts")
            state.update({"day": today, "n": (int(state.get("n", 0)) if state.get("day") == today else 0) + 1})

        return bump

    # ------------------------------------------------------------------ what the buttons do
    def action(self, name: str) -> Action:
        """A tapped button: "plan", "again", "tomorrow", "replan" or "dismiss"."""
        if name == "dismiss":
            return Action("👍 Okay.")
        if self.plan is None:
            return Action("Day planning isn't available.")
        if name == "plan":
            return Action(ask="plannew")
        now = self._now()
        if name == "again":
            usual = self._section("usual").get("request")
            if not usual:
                return Action("I don't know your usual day yet: lock in a plan once with /plan and I'll remember it.")
            base = self._clean(usual)
            return Action(reply=self.plan.draft_from_json(base, day="today"))
        if name == "tomorrow":
            request = self._tomorrow_request(now)
            if request is None:
                return Action(ask="plannew")
            return Action(reply=self.plan.draft_from_json(request, day="tomorrow"))
        if name == "replan":
            request = self._replan_request(now)
            if request is None:
                return Action("There's no plan for today to re-plan.")
            return Action(reply=self.plan.draft_from_json(request, day="today"))
        return Action()

    @staticmethod
    def _clean(request: dict[str, Any]) -> dict[str, Any]:
        """A saved day without what only made sense on that day (skipped lectures, the time it started, where you were)."""
        base = deepcopy(request)
        base.update({"skip": [], "start": None, "start_place": None})
        base.setdefault("items", [])
        return base

    def _tomorrow_request(self, now: datetime) -> dict[str, Any] | None:
        usual = self._section("usual").get("request")
        carry = self._carry(now.date())
        if not usual and not carry:
            return None
        base = self._clean(usual) if usual else {"items": []}
        items = base["items"]
        for subject, minutes in carry.items():  # today's leftovers go on top of the usual study
            existing = next((i for i in items if i.get("kind") == "study" and str(i.get("title", "")).lower() == subject.lower()), None)
            if existing is not None:
                existing["minutes"] = int(existing.get("minutes") or 0) + minutes
            else:
                items.append({"kind": "study", "title": subject, "minutes": minutes})
        return base

    def _replan_request(self, now: datetime) -> dict[str, Any] | None:
        """What's left of today: study minus what you did, and nothing that's already behind you."""
        plan = self._active_today(now)
        if not plan or not plan.get("request"):
            return None
        minute = now.hour * 60 + now.minute
        blocks = [Block.from_dict(b) for b in plan.get("blocks") or []]
        planned = self._planned(plan)
        done, other = self._done(now.date(), list(planned))
        request = self._clean(plan["request"])
        kept: list[dict[str, Any]] = []
        for item in request["items"]:
            title = str(item.get("title", ""))
            if item.get("kind") == "study":
                match = next((s for s in planned if s.lower() == title.lower()), title)
                total = int(item.get("minutes") or planned.get(match, 0))
                left = total - done.get(match, 0)
                if left >= MIN_LEFTOVER:
                    item["minutes"] = left
                    kept.append(item)
            elif any(b.title == title and b.kind != STUDY and b.end <= minute for b in blocks) and not item.get("ends_day"):
                continue  # the gym is behind you
            else:
                kept.append(item)
        if other:  # focus rounds that matched no subject count against the biggest one left
            biggest = max((i for i in kept if i.get("kind") == "study"), key=lambda i: i.get("minutes", 0), default=None)
            if biggest is not None:
                biggest["minutes"] = max(0, int(biggest["minutes"]) - other)
                kept = [i for i in kept if i.get("kind") != "study" or int(i.get("minutes", 0)) >= MIN_LEFTOVER]
        request["items"] = kept
        return request


def build_secretary(core: Any, backend: Any, state: Any, *, focus: Any = None, plan: Any = None, **kwargs: Any) -> SecretaryService | None:
    """The secretary, or None when switched off (``MIKI_SECRETARY=0``)."""
    import os

    if os.getenv("MIKI_SECRETARY", "1").strip().lower() in {"0", "false", "no", "off"}:
        return None
    return SecretaryService(
        calendar=backend.calendar_range,
        weather=lambda: backend.home_data(state.owner_name).weather,
        urgent_mail=lambda: [i for i in backend.mail_items(force=False) if i.priority == "urgent"],
        focus=focus,
        plan=plan,
        name=lambda: state.owner_name,
        pref=state.pref,
        **kwargs,
    )
