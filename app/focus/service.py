"""Focus mode, end to end.

    /focus  ->  Screen 1: Gemini, Screen 2: Claude (both in a Miki-watched Chrome), everything else tucked away,
                a little pet walking along the taskbar, distractions bounced, and after an hour a break.

The service owns the timeline (``FocusSession``), the bouncer (``FocusGuard``), the Chrome windows and the pet.
It reports the moments that matter (10 minutes left, break time, break over) to whoever listens: the phone bot
turns them into Telegram messages, the dashboard into chat lines.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
import traceback
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from app.core import single_instance
from app.focus import duration, habits as habit_module, rules
from app.focus.chrome import DEFAULT_PORT, FocusChrome, find_chrome
from app.focus.guard import Bounce, FocusGuard
from app.focus.memory import FocusMemory
from app.focus.pet_host import PetHost
from app.focus.recap import day_facts, format_minutes, recap_text
from app.focus.session import BREAK, DEFAULT_BREAK_MINUTES, DEFAULT_MINUTES, FOCUS, Event, FocusSession
from app.focus.winapi import Rect, Window, create_desktop, order_screens, split_rect

logger = logging.getLogger(__name__)

LOCK_NAME = "MikiFocus"
TICK_SECONDS = 1.0
GUARD_STUCK_SECONDS = 10
LISTENER_WAIT_SECONDS = 2.0  # how long the clock waits for one listener (a Telegram send) before moving on
CHEER_SECONDS = 20  # how long the pet celebrates before curling up for the break


def _truthy(raw: str | None, default: bool) -> bool:
    return default if raw is None or not raw.strip() else raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class FocusConfig:
    enabled: bool = True
    minutes: int = DEFAULT_MINUTES
    break_minutes: int = DEFAULT_BREAK_MINUTES
    screen1_url: str = "https://gemini.google.com/app"
    screen2_url: str = "https://claude.ai/new"
    banned_sites: tuple[str, ...] = rules.DEFAULT_BANNED_SITES
    banned_apps: tuple[str, ...] = rules.DEFAULT_BANNED_APPS
    env_sites: tuple[str, ...] = ()  # MIKI_FOCUS_BAN (can't be removed with /focus unban)
    env_apps: tuple[str, ...] = ()
    chrome_path: str | None = None
    profile_dir: Path = Path("data/focus-chrome")
    state_path: Path = Path("data/focus.json")
    port: int = DEFAULT_PORT
    swap_screens: bool = False
    recap_time: str = "21:00"  # end-of-day recap, sent only on days you focused; "off" disables it

    @classmethod
    def from_env(cls) -> "FocusConfig":
        def integer(name: str, default: int) -> int:
            try:
                return int(os.getenv(name, "").strip() or default)
            except ValueError:
                return default

        env_sites, env_apps = rules.parse_targets(os.getenv("MIKI_FOCUS_BAN"))
        return cls(
            enabled=_truthy(os.getenv("MIKI_FOCUS"), True),
            minutes=integer("MIKI_FOCUS_MINUTES", DEFAULT_MINUTES),
            break_minutes=integer("MIKI_FOCUS_BREAK_MINUTES", DEFAULT_BREAK_MINUTES),
            screen1_url=os.getenv("MIKI_FOCUS_SCREEN1_URL", "").strip() or cls.screen1_url,
            screen2_url=os.getenv("MIKI_FOCUS_SCREEN2_URL", "").strip() or cls.screen2_url,
            env_sites=tuple(env_sites),
            env_apps=tuple(env_apps),
            chrome_path=os.getenv("MIKI_CHROME_PATH", "").strip() or None,
            port=integer("MIKI_FOCUS_PORT", DEFAULT_PORT),
            swap_screens=_truthy(os.getenv("MIKI_FOCUS_SWAP_SCREENS"), False),
            recap_time=os.getenv("MIKI_FOCUS_RECAP_TIME", "").strip().lower() or cls.recap_time,
        )


@dataclass
class FocusEvent:
    kind: str  # "warn" | "time_up" | "break_over" | "expired"
    text: str  # a plain-text version, good enough for any listener
    minutes_left: int = 0
    summary: dict[str, Any] = field(default_factory=dict)


@dataclass
class FocusReply:
    ok: bool
    text: str


def _hm(when: float) -> str:
    return datetime.fromtimestamp(when).strftime("%H:%M")


def _short(text: str, limit: int = 36) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


GOAL_DONE_WORDS = {"done", "finished", "reached", "yes"}
GOAL_NOT_DONE_WORDS = {"notyet", "not", "unfinished", "no"}

RECAP_WINDOW_HOURS = 3  # the recap goes out from its time until this much later (never a stale one)


def _parse_clock(text: str) -> tuple[int, int] | None:
    try:
        hour, minute = (int(part) for part in text.strip().split(":"))
    except ValueError:
        return None
    return (hour, minute) if 0 <= hour < 24 and 0 <= minute < 60 else None


class FocusService:
    def __init__(
        self,
        config: FocusConfig | None = None,
        *,
        session: FocusSession | None = None,
        desktop: Any = None,
        chrome: Any = None,
        pet: Any = None,
        clock: Callable[[], float] = time.time,
        lock_name: str | None = LOCK_NAME,
        beep: Callable[[], None] | None = None,
        memory: Any = None,
        ai_minutes: Callable[[str, datetime], int | None] | None = None,
    ) -> None:
        self.config = config or FocusConfig.from_env()
        self.session = session or FocusSession(self.config.state_path, clock=clock)
        self.desktop = desktop if desktop is not None else create_desktop()
        chrome_path = find_chrome(self.config.chrome_path)
        self.chrome = chrome if chrome is not None else (
            FocusChrome(chrome_path, self.config.profile_dir, port=self.config.port) if chrome_path else None
        )
        self.pet = pet if pet is not None else PetHost()
        self.guard = FocusGuard(
            self.desktop, self.chrome, banned_sites=self.banned_sites, banned_apps=self.banned_apps,
            own_pids=self._own_pids, home_url=self.config.screen2_url, on_bounce=self._on_bounce,
        ) if self.desktop is not None else None
        self.memory = memory  # a FocusMemory: keeps the study-habits memory and the daily notes
        self._ai_minutes = ai_minutes  # understands durations the plain parser can't ("until 3pm")
        self._clock = clock
        self._lock_name = lock_name
        self._beep = beep or _default_beep
        self._listeners: list[Callable[[FocusEvent], None]] = []
        self._rlock = threading.RLock()
        self._armed = threading.Event()  # the bouncer only works once the screens are set up
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._owns = False
        self._cheer_until = 0.0
        self._last_info = 0.0
        self._pet_off_at = 0.0
        self._last_recap_check = 0.0
        self._guard_thread: threading.Thread | None = None
        self._guard_started = 0.0  # monotonic time the current bouncer pass began (0: not in a pass)
        self._guard_reported = 0.0

    # ------------------------------------------------------------------ what's banned
    def _ban_entries(self) -> list[str]:
        """Every banned site and program, after your additions and removals."""
        removed = set(self.session.banned_removed())
        defaults = [e for e in (*self.config.banned_sites, *self.config.banned_apps) if e not in removed]
        return list(dict.fromkeys([*defaults, *self.config.env_sites, *self.config.env_apps, *self.session.banned_added()]))

    def banned_sites(self) -> tuple[str, ...]:
        return tuple(e for e in self._ban_entries() if not e.endswith(".exe"))

    def banned_apps(self) -> frozenset[str]:
        return frozenset(e for e in self._ban_entries() if e.endswith(".exe"))

    def _own_pids(self) -> set[int]:
        pids = {os.getpid()}
        pet_pid = getattr(self.pet, "pid", None)
        if pet_pid:
            pids.add(int(pet_pid))
        return pids

    # ------------------------------------------------------------------ lifecycle
    @property
    def available(self) -> bool:
        return self.config.enabled and self.desktop is not None

    @property
    def is_focusing(self) -> bool:
        return self.session.is_focusing

    def add_listener(self, listener: Callable[[FocusEvent], None]) -> None:
        self._listeners.append(listener)

    def start(self) -> bool:
        """Start the clock thread (and pick up a round that was running when Miki last stopped)."""
        if not self.available:
            return False
        if self._owns and self._thread is not None and self._thread.is_alive():
            return True  # already running: a second start must not add a second clock and a second bouncer
        if self._lock_name and not single_instance.acquire_named(self._lock_name):
            logger.info("Focus mode is handled by another Miki process")
            return False
        self._owns = True
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="miki-focus", daemon=True)
        self._thread.start()
        # The bouncer talks to other programs' windows, which can hang. It gets its own thread so that a stuck call
        # can never stop the timer (the break message, the pet, the recap).
        self._guard_thread = threading.Thread(target=self._run_guard, name="miki-focus-guard", daemon=True)
        self._guard_thread.start()
        if self.session.is_active:
            threading.Thread(target=self._resume, name="miki-focus-resume", daemon=True).start()
        return True

    def close(self) -> None:
        self._stop.set()
        self.pet.stop()

    def _resume(self) -> None:
        """Miki restarted mid-round: put the lock and the pet back (unless the round ended while Miki was closed)."""
        self.tick_clock()  # first settle the clock: a round that is already over must not put the screens back up
        if self.session.phase == FOCUS and self.session.seconds_left() > 0:
            self._arm()
        elif self.session.phase == BREAK:
            self._start_pet()
            self.pet.mood("sleep")

    def _run(self) -> None:
        while not self._stop.wait(TICK_SECONDS):
            try:
                self.tick_clock()
                self._watch_guard()
            except Exception:
                logger.exception("Focus tick failed")

    def _run_guard(self) -> None:
        while not self._stop.wait(TICK_SECONDS):
            try:
                self.tick_guard()
            except Exception:
                logger.exception("Focus guard failed")

    def _watch_guard(self) -> None:
        """If one bouncer pass has been stuck for a while, say so once, with where it is stuck."""
        started = self._guard_started
        if not started or self._guard_reported == started or time.monotonic() - started < GUARD_STUCK_SECONDS:
            return
        self._guard_reported = started
        thread = getattr(self, "_guard_thread", None)
        frame = sys._current_frames().get(thread.ident) if thread is not None and thread.ident else None
        where = "".join(traceback.format_stack(frame)[-6:]) if frame is not None else "(unknown)"
        logger.warning("The focus bouncer has been stuck for %d s; the timer keeps running without it. It is at:\n%s", GUARD_STUCK_SECONDS, where)

    def tick(self) -> None:
        """One second of upkeep: the timeline, the bouncer, the pet's mood (the two threads' work, in one call)."""
        self.tick_clock()
        self.tick_guard()

    def tick_guard(self) -> None:
        """The bouncer's pass (and the pet's status line). May block on another program's window: never call it from the clock."""
        if not (self.session.phase == FOCUS and self._armed.is_set() and self.guard is not None):
            return
        self._guard_started = time.monotonic()
        try:
            self.guard.tick()
        finally:
            self._guard_started = 0.0
        now = self._clock()
        if now - self._last_info > 20:
            self._last_info = now
            self.pet.info(self._pet_info())

    def tick_clock(self) -> None:
        """The timeline: warnings, the end of a round, the break, the pet's mood, the daily recap."""
        for event in self.session.tick(self.config.break_minutes):
            self._handle(event)
        now = self._clock()
        if self.session.phase == BREAK and self._cheer_until and now >= self._cheer_until:
            self._cheer_until = 0.0
            self.pet.mood("sleep")
        if self._pet_off_at and now >= self._pet_off_at and not self.session.is_active:
            self._pet_off_at = 0.0
            self.pet.stop()
        if now - self._last_recap_check >= 60:
            self._last_recap_check = now
            self.send_recap_if_due()

    def attach(self, core: Any) -> None:
        """Connect Miki's brain (to read odd durations) and memory (to keep study habits)."""
        manager = getattr(core, "memory_manager", None)
        brain = getattr(core, "brain", None)
        if manager is not None:
            self.memory = FocusMemory(manager, self.session)
        if brain is not None and hasattr(brain, "generate_response"):
            self._ai_minutes = duration.make_ai_minutes(brain)

    # ------------------------------------------------------------------ how long?
    def parse_duration(self, text: str) -> "duration.Duration":
        """Minutes for '1 hour', '30 mins', 'half an hour', 'until 3pm' ... (plain patterns first, then the AI)."""
        return duration.resolve(text, self._ai_minutes, datetime.fromtimestamp(self._clock()))

    def start_from_text(self, text: str) -> FocusReply:
        """``/focus <how long>``: an empty text means the default round."""
        if not text.strip():
            return self.start_focus()
        parsed, goal = duration.resolve_with_goal(text, self._ai_minutes, datetime.fromtimestamp(self._clock()))
        if not parsed.ok:
            return FocusReply(False, parsed.error)
        reply = self.start_focus(parsed.minutes, goal=goal)
        if reply.ok and parsed.via_ai:
            reply = FocusReply(True, f"I read that as {format_minutes(parsed.minutes)}.\n{reply.text}")
        return reply

    def extend_from_text(self, text: str) -> FocusReply:
        """``/focus more <how long>``: an empty text means 15 minutes."""
        if not text.strip():
            return self.extend(15)
        parsed = self.parse_duration(text)
        return self.extend(parsed.minutes) if parsed.ok else FocusReply(False, parsed.error)

    # ------------------------------------------------------------------ commands
    def start_focus(self, minutes: int | float | None = None, goal: str = "") -> FocusReply:
        if not self.config.enabled:
            return FocusReply(False, "Focus mode is switched off (MIKI_FOCUS=0).")
        if self.desktop is None or self.guard is None:
            return FocusReply(False, "Focus mode needs Windows.")
        if self.chrome is None:
            return FocusReply(False, "I couldn't find Google Chrome. Install it, or set MIKI_CHROME_PATH in .env.")
        if not self._owns and not self.start():
            return FocusReply(False, "Focus mode is running in another Miki window, so I can't start it from here.")
        with self._rlock:
            try:
                round_ = self.session.start(minutes if minutes is not None else self.config.minutes, goal=goal)
            except ValueError as exc:
                return FocusReply(False, f"{exc} {self.status_text()}")
            self._pet_off_at = 0.0
            self._cheer_until = 0.0
        threading.Thread(target=self._arm, name="miki-focus-arm", daemon=True).start()
        end = _hm(round_.ends_at)
        return FocusReply(True, (
            f"Focus mode is on for {format_minutes(round_.planned_minutes)}, until {end}.\n"
            + (f"Goal: {round_.goal}\n" if round_.goal else "")
            + f"Screen 1: Gemini. Screen 2: Claude. Off limits: {rules.describe_banned(self.banned_sites(), sorted(self.banned_apps()))}.\n"
            f"I'll tell you when it's time for a break."
        ))

    # ------------------------------------------------------------------ the goal of a round
    def set_goal(self, text: str) -> FocusReply:
        """``/focus goal <what>``: name what the running round is for."""
        goal = duration.clean_goal(text)
        if not goal:
            return FocusReply(False, "What's the goal? For example /focus goal finish lecture 6")
        if not self.session.set_goal(goal):
            return FocusReply(False, "Start a round first, e.g. /focus 50 min, finish lecture 6")
        self.pet.info(self._pet_info())
        return FocusReply(True, f"Goal set: {goal}")

    def goal_result(self, done: bool) -> FocusReply:
        """``/focus done`` or ``/focus notyet``: did you reach the goal?"""
        goal = self.session.set_goal_result(done)
        if goal is None:
            return FocusReply(False, "There's no goal to check. Start one with /focus 50 min, finish lecture 6")
        self._log_round({}, force=True)  # the day's note lists which goals were reached
        if not done:
            return FocusReply(True, f"Okay, “{goal}” isn't finished yet. ➕ more time, or pick it up next round.")
        if self.session.phase == FOCUS:
            self.pet.mood("cheer")
            return FocusReply(True, f"Nice, “{goal}” is done! Keep the round going, or /focus stop to end it early.")
        return FocusReply(True, f"Nice, “{goal}” is done! ✓")

    def extend(self, minutes: int | float = 15) -> FocusReply:
        with self._rlock:
            was_break = self.session.phase == BREAK
            try:
                round_ = self.session.extend(minutes)
            except ValueError as exc:
                return FocusReply(False, str(exc))
        if was_break:
            threading.Thread(target=self._arm, name="miki-focus-arm", daemon=True).start()
        else:
            self.pet.timeline(round_.started_at, round_.ends_at)
            self.pet.say(f"{int(minutes)} more minutes. Let's go!", 5)
        return FocusReply(True, f"Added time. Focus now ends at {_hm(round_.ends_at)}.")

    def stop(self) -> FocusReply:
        with self._rlock:
            was_focus = self.session.phase == FOCUS
            summary = self.session.stop()
            if not summary:
                return FocusReply(False, "There's no focus session running.")
            self._armed.clear()
        self.pet.say("See you next round!", 3)
        self._pet_off_at = self._clock() + 3
        self._log_round(summary)
        return FocusReply(True, ("Focus mode ended. " if was_focus else "Done for now. ") + self.summary_line(summary))

    # ------------------------------------------------------------------ bringing the screens up
    def _arm(self) -> None:
        """Windows on the right screens, clutter tucked away, pet out, then the bouncer goes on duty."""
        self._armed.clear()
        try:
            self._layout_windows()
        except Exception:
            logger.exception("Could not set up the focus screens")
        try:
            if self.guard is not None:
                self.guard.clean_slate()
        except Exception:
            logger.debug("clean slate failed", exc_info=True)
        self._start_pet()
        round_ = self.session.round
        self.pet.timeline(round_.started_at, round_.ends_at)
        self.pet.say(f"Goal: {_short(round_.goal)}. Let's go!" if round_.goal else "Focus time! I'll keep watch.", 5)
        self.pet.info(self._pet_info())
        if self.session.phase == FOCUS:
            self._armed.set()

    def _start_pet(self) -> None:
        try:
            with self.desktop.physical_pixels():
                screens = order_screens(self.desktop.monitors(), swap=self.config.swap_screens)
            if screens:
                self.pet.start(screens[0].work)
        except Exception:
            logger.debug("pet start failed", exc_info=True)

    def _focus_windows(self) -> list[Window]:
        pids = self.chrome.browser_pids()
        return [w for w in self.desktop.app_windows() if w.exe == "chrome.exe" and w.pid in pids]

    def _layout_windows(self) -> None:
        with self.desktop.physical_pixels():
            screens = order_screens(self.desktop.monitors(), swap=self.config.swap_screens)
        if not screens:
            return
        if len(screens) >= 2:
            rects = [screens[0].work, screens[1].work]
        else:  # one monitor: side by side
            rects = [split_rect(screens[0].work, 0), split_rect(screens[0].work, 1)]
        urls = [self.config.screen1_url, self.config.screen2_url]

        existing = self._focus_windows() if self.chrome.is_running() else []
        gemini = next((w for w in existing if "gemini" in w.title.lower()), None)
        first = gemini or (existing[0] if existing else None)
        rest = [w for w in existing if w is not first]
        second = rest[0] if rest else None
        chosen: list[Window | None] = [first, second]

        for index, (url, rect) in enumerate(zip(urls, rects)):
            window = chosen[index]
            if window is None:
                window = self._open_window(url, rect)
            if window is not None:
                self.desktop.place(window.hwnd, rect)
        # leave Screen 1's window in front: that is where Gemini is
        front = chosen[0]
        if front is not None:
            self.desktop.activate(front.hwnd)

    def _open_window(self, url: str, rect: Rect) -> Window | None:
        """Open ``url`` in a new focus-Chrome window and return it once Windows shows it."""
        before = {w.hwnd for w in self._focus_windows()} if self.chrome.is_running() else set()
        self.chrome.open_window(url, rect.x, rect.y, rect.w, rect.h)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            time.sleep(0.4)
            fresh = [w for w in self._focus_windows() if w.hwnd not in before]
            if fresh:
                return fresh[0]
        return None

    # ------------------------------------------------------------------ what happened
    def _on_bounce(self, bounce: Bounce) -> None:
        count = self.session.record_distraction(bounce.label)
        self.pet.mood("alert")
        self.pet.say(f"Nope, {bounce.label} can wait! Back to work.", 4)
        self.pet.info(self._pet_info())
        logger.info("Focus: bounced %s %s (#%d)", bounce.kind, bounce.label, count)

    def _handle(self, event: Event) -> None:
        summary = event.summary
        if event.kind == "warn":
            left = format_minutes(event.minutes_left)
            self.pet.say(f"{left} left. You've got this!", 6)
            text = f"{left} left in this focus round. Finish strong."
        elif event.kind == "time_up":
            self._armed.clear()
            self._beep()
            self._start_pet()
            self.pet.mood("cheer")
            self.pet.say("Time for a break! You did it!", 15)
            self._cheer_until = self._clock() + CHEER_SECONDS
            self._log_round(summary)
            text = (
                f"Time for a break! You focused for {format_minutes(summary.get('minutes', 0))}. "
                f"The lock is off for {self.config.break_minutes} minutes: stand up, stretch, drink some water.\n"
                f"{self._praise(summary)}"
                + (f"\nGoal check: “{summary['goal']}”. Did you get it done? (/focus done or /focus notyet)"
                   if summary.get("goal") and summary.get("goal_done") is None else "")
            )
        elif event.kind == "break_over":
            self._beep()
            self.pet.say("Break's over. Ready for another round?", 10)
            self._pet_off_at = self._clock() + 12
            text = "Break's over. Ready for another round?"
        else:  # expired: Miki was off when the round ended; close it quietly (but still remember it)
            self.pet.stop()
            self._log_round(summary)
            return
        self._notify(FocusEvent(event.kind, text, event.minutes_left, summary))

    def _notify(self, event: FocusEvent) -> None:
        """Tell every listener (Telegram, the dashboard chat) without ever letting one of them stall the clock.

        A listener runs on its own short-lived thread and gets ``LISTENER_WAIT_SECONDS`` to finish. A send that hangs
        (the network dropped, the laptop woke up) is left behind on its thread; the timer carries on.
        """
        def call(listener: Callable[[FocusEvent], None]) -> None:
            try:
                listener(event)
            except Exception:
                logger.exception("Focus listener failed")

        for listener in list(self._listeners):
            worker = threading.Thread(target=call, args=(listener,), name="miki-focus-notify", daemon=True)
            worker.start()
            worker.join(LISTENER_WAIT_SECONDS)
            if worker.is_alive():
                logger.warning("A focus listener is taking more than %.0f s (%s); the timer will not wait for it", LISTENER_WAIT_SECONDS, event.kind)

    # ------------------------------------------------------------------ words
    def _pet_info(self) -> str:
        left = self.session.seconds_left()
        current = self.session.round
        text = f"{max(1, round(left / 60))} min left"
        if current.distractions:
            text += f" · {current.distractions} bounced"
        return (f"{_short(current.goal)} · " if current.goal else "") + text

    @staticmethod
    def _praise(summary: dict[str, Any]) -> str:
        bounced = int(summary.get("distractions", 0))
        blocked = summary.get("blocked") or {}
        if not bounced:
            line = "Zero distractions. Impressive."
        else:
            top = ", ".join(f"{name} ×{n}" for name, n in sorted(blocked.items(), key=lambda kv: -kv[1])[:3])
            line = f"I bounced {bounced} distraction{'s' if bounced != 1 else ''} ({top})."
        streak = int(summary.get("streak", 0))
        today = int(summary.get("today_minutes", 0))
        if today <= 0:
            return line
        return f"{line} That's {format_minutes(today)} focused today" + (f" and a {streak}-day streak." if streak >= 2 else ".")

    def summary_line(self, summary: dict[str, Any]) -> str:
        if int(summary.get("minutes", 0)) < 1:
            return "That ended before it really started. Whenever you're ready, /focus again."
        text = f"You focused for {format_minutes(summary.get('minutes', 0))}. " + self._praise(summary)
        if summary.get("goal") and summary.get("goal_done") is None:
            text += f"\nDid you finish “{summary['goal']}”? Say /focus done or /focus notyet."
        return text

    def status_text(self) -> str:
        phase = self.session.phase
        if phase == FOCUS:
            r = self.session.round
            return (f"Focusing: {format_minutes(round(self.session.seconds_left() / 60))} left (until {_hm(r.ends_at)}). "
                    f"{r.distractions} distraction{'s' if r.distractions != 1 else ''} bounced."
                    + (f"\nGoal: {r.goal}" if r.goal else ""))
        if phase == BREAK:
            return f"On a break: {format_minutes(round(self.session.seconds_left() / 60))} left."
        return "No focus session running."

    def view(self) -> dict[str, Any]:
        """Plain data for a screen (the phone builds its own layout from this)."""
        phase = self.session.phase
        r = self.session.round
        stats = self.session.stats()
        end = r.ends_at if phase == FOCUS else r.break_ends_at
        return {
            "phase": phase,
            "minutes_left": max(1, round(self.session.seconds_left() / 60)) if phase != "idle" else 0,
            "until": _hm(end) if phase != "idle" else "",
            "distractions": r.distractions if phase != "idle" else 0,
            "today_minutes": stats["today_minutes"],
            "streak": stats["streak"],
            "minutes": self.config.minutes,
            "goal": r.goal if phase != "idle" else "",
            "goal_done": r.goal_done if phase != "idle" else None,
        }

    def stats_text(self) -> str:
        s = self.session.stats()
        facts = day_facts(self.session.rounds_on(self._today()))
        when = f", {_hm(facts['first_start'])} to {_hm(facts['last_end'])}" if facts["first_start"] else ""
        return (
            f"Today: {format_minutes(s['today_minutes'])} in {s['today_rounds']} round{'s' if s['today_rounds'] != 1 else ''}{when}\n"
            f"This week: {format_minutes(s['week_minutes'])}, {s['week_distractions']} bounced\n"
            f"Streak: {s['streak']} day{'s' if s['streak'] != 1 else ''} · Best day: {format_minutes(s['best_day_minutes'])}"
        )

    # ------------------------------------------------------------------ the day, and how you study
    def _today(self) -> date:
        return datetime.fromtimestamp(self._clock()).date()

    def _usual_minutes(self, day: date) -> float | None:
        """Your typical focus per active day *before* ``day`` (needs a few days of history to mean anything)."""
        before = habit_module.analyse(self.session.history(), day - timedelta(days=1))
        return before.avg_minutes_per_active_day if before.active_days >= habit_module.MIN_DAYS_FOR_PATTERNS else None

    def today_text(self, day: date | None = None) -> str:
        """How long you focused today, when you started and when you finished, round by round."""
        day = day or self._today()
        running_until = self.session.round.ends_at if self.session.phase == FOCUS and day == self._today() else None
        return recap_text(day, self.session.rounds_on(day), usual_minutes=self._usual_minutes(day),
                          streak=self.session.streak(day), running_until=running_until)

    def habits_text(self, *, reset: bool = False) -> str:
        if reset and self.memory is not None:
            self.memory.reset()
            self._log_round({}, force=True)
            return "Okay, I'll start a fresh study-habits memory.\n" + habit_module.summary_text(habit_module.analyse(self.session.history(), self._today()))
        text = habit_module.summary_text(habit_module.analyse(self.session.history(), self._today()))
        return text + ("\n" + self.memory.status() if self.memory is not None else "")

    def _log_round(self, summary: dict[str, Any], *, force: bool = False) -> None:
        """A round just ended: teach the memory about it (off the clock thread: it writes files)."""
        if self.memory is None or (not force and int(summary.get("minutes", 0)) < 1):
            return
        threading.Thread(target=self.memory.record_round, args=(self._today(),), name="miki-focus-memory", daemon=True).start()

    def recap_due(self, now: datetime | None = None) -> bool:
        """Time for the end-of-day message? Only on days you focused, once a day, never in the middle of a round."""
        if self.config.recap_time in {"", "off", "none", "no"}:
            return False
        clock = _parse_clock(self.config.recap_time)
        now = now or datetime.fromtimestamp(self._clock())
        if clock is None or self.session.phase == FOCUS:
            return False
        due = now.replace(hour=clock[0], minute=clock[1], second=0, microsecond=0)
        end_of_day = now.replace(hour=23, minute=59, second=59)
        if not due <= now <= min(due + timedelta(hours=RECAP_WINDOW_HOURS), end_of_day):
            return False
        if self.session.recap_sent_on() == now.date().isoformat():
            return False
        return day_facts(self.session.rounds_on(now.date()))["rounds"] > 0

    def send_recap_if_due(self, now: datetime | None = None) -> bool:
        if not self.recap_due(now):
            return False
        day = (now or datetime.fromtimestamp(self._clock())).date()
        self.session.mark_recap_sent(day)  # first: a slow listener must never cause a second recap
        text = self.today_text(day)
        self._log_round({}, force=True)
        event = FocusEvent("recap", text, summary=day_facts(self.session.rounds_on(day)))
        self._notify(event)
        return True

    def command(self, argument: str) -> str:
        """Run ``/focus <argument>`` as typed text and return the reply (the dashboard uses this; the phone has buttons too)."""
        word, _, rest = argument.strip().partition(" ")
        word, rest = word.lower(), rest.strip()
        if word in {"stop", "end", "off", "quit", "cancel"}:
            return self.stop().text
        if word == "status":
            return self.status_text()
        if word == "stats":
            return self.stats_text()
        if word == "today":
            return self.today_text()
        if word in {"habits", "habit", "routine"}:
            return self.habits_text(reset=rest.lower() == "reset")
        if word in {"banned", "bans", "sites", "list"}:
            return self.banned_text()
        if word in {"ban", "block"}:
            return self.ban(rest).text
        if word in {"unban", "allow"}:
            return self.unban(rest).text
        if word in {"more", "extend", "+"}:
            return self.extend_from_text(rest).text
        if word == "goal":
            return self.set_goal(rest).text
        if word in GOAL_DONE_WORDS:
            return self.goal_result(True).text
        if word in GOAL_NOT_DONE_WORDS:
            return self.goal_result(False).text
        return self.start_from_text(argument).text

    # ------------------------------------------------------------------ the banned list
    def banned_text(self) -> str:
        sites, apps = self.banned_sites(), sorted(self.banned_apps())
        lines = [f"Banned during focus ({len(sites)} sites, {len(apps)} apps):",
                 "Sites: " + ", ".join(sites),
                 "Apps: " + ", ".join(a.removesuffix(".exe") for a in apps)]
        mine = self.session.banned_added()
        if mine:
            lines.append("Added by you: " + ", ".join(mine))
        lines.append("Ban more with /focus ban <site or app>, lift one with /focus unban <site or app>.")
        return "\n".join(lines)

    sites_text = banned_text  # older name, used by the phone's Sites button

    def ban(self, text: str) -> FocusReply:
        target = rules.parse_target(text)
        if target is None:
            return FocusReply(False, "Tell me a website or a program, e.g. /focus ban tiktok.com or /focus ban discord")
        if target.value in self._ban_entries():
            return FocusReply(True, f"{target.value} is already banned.")
        added, removed = list(self.session.banned_added()), list(self.session.banned_removed())
        if target.value in removed:  # a built-in ban you had lifted comes back
            removed.remove(target.value)
        else:
            added.append(target.value)
        self.session.set_banned_changes(added, removed)
        return FocusReply(True, f"Banned {target.value} during focus.")

    def unban(self, text: str) -> FocusReply:
        if self.session.is_active:
            return FocusReply(False, "Bans can only be lifted between sessions. Otherwise it would be too easy to talk yourself out of focus.")
        target = rules.parse_target(text)
        if target is None or target.value not in self._ban_entries():
            return FocusReply(False, "That isn't on the banned list. See /focus banned.")
        if target.value in (*self.config.env_sites, *self.config.env_apps):
            return FocusReply(False, f"{target.value} is banned in your .env file (MIKI_FOCUS_BAN), so it can only be lifted there.")
        added = [e for e in self.session.banned_added() if e != target.value]
        removed = list(self.session.banned_removed())
        if target.value in (*self.config.banned_sites, *self.config.banned_apps):
            removed.append(target.value)
        self.session.set_banned_changes(added, removed)
        return FocusReply(True, f"{target.value} is no longer banned.")


def _default_beep() -> None:
    try:
        import winsound

        winsound.MessageBeep(winsound.MB_ICONASTERISK)
    except Exception:
        pass


def build_focus_service(core: Any = None, **kwargs: Any) -> FocusService | None:
    """The focus service, or None when it can't work here (switched off, or not Windows).

    Given Miki's core, focus mode can ask the AI what an odd duration means and can keep study habits in memory.
    """
    config = FocusConfig.from_env()
    if not config.enabled:
        return None
    try:
        service = FocusService(config, **kwargs)
    except Exception:
        logger.exception("Focus mode is unavailable")
        return None
    if service.desktop is None:
        return None
    if core is not None:
        service.attach(core)
    return service
