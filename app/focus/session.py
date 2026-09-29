"""The focus timeline: focus -> break -> done. No windows, no network, only time and a small JSON file.

Phases
    idle    nothing running
    focus   the lock is on until ``ends_at``
    break   the lock is off; a short rest until ``break_ends_at``, then the round is over

Time is wall-clock (``time.time``) so a session survives a restart or the laptop sleeping. ``tick()`` is
called about once a second and returns the moments worth telling the user about.
"""

from __future__ import annotations

import json
import logging
import shutil
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

IDLE, FOCUS, BREAK = "idle", "focus", "break"

DEFAULT_MINUTES = 60
MIN_MINUTES, MAX_MINUTES = 5, 240
DEFAULT_BREAK_MINUTES = 5
WARN_MINUTES = 10  # "10 minutes left"
STALE_AFTER = 15 * 60  # a round that ended this long ago (Miki was off) is closed quietly, not announced
HISTORY_DAYS = 120
GOAL_ANSWER_WINDOW = 12 * 3600  # you can still say whether you reached the goal this long after the round
STREAK_MIN_MINUTES = 20  # a day counts toward the streak with at least this much focus


@dataclass
class Event:
    kind: str  # "warn" | "time_up" | "break_over" | "expired"
    minutes_left: int = 0
    summary: dict[str, Any] = field(default_factory=dict)


@dataclass
class Round:
    id: str = ""
    started_at: float = 0.0
    ends_at: float = 0.0
    planned_minutes: int = 0
    distractions: int = 0
    blocked: dict[str, int] = field(default_factory=dict)  # what was bounced, and how often
    warned: bool = False
    break_ends_at: float = 0.0
    goal: str = ""  # what you said you would get done ("finish lecture 6")
    goal_done: bool | None = None  # your answer to "did you?"; None until you say


class FocusSession:
    """Thread-safe; state lives in ``data/focus.json`` next to the phone state."""

    def __init__(self, path: str | Path = "data/focus.json", *, clock: Callable[[], float] = time.time) -> None:
        self.path = Path(path)
        self._clock = clock
        self._lock = threading.RLock()
        self._data: dict[str, Any] = self._load()
        self._round = Round(**self._data["round"]) if isinstance(self._data.get("round"), dict) else Round()
        self._phase: str = self._data.get("phase", IDLE) if self._data.get("phase") in {IDLE, FOCUS, BREAK} else IDLE
        if self._phase != IDLE and not self._round.id:
            self._phase = IDLE

    # ------------------------------------------------------------------ storage
    @property
    def _backup_path(self) -> Path:
        return self.path.with_suffix(".bak")

    @property
    def log_path(self) -> Path:
        """``focus_log.jsonl``: every finished round, appended and never trimmed (the file you can always fall back on)."""
        return self.path.with_name("focus_log.jsonl")

    @staticmethod
    def _read(path: Path) -> dict[str, Any] | None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        data = self._read(self.path)
        if data is not None:
            return data
        # The file is damaged. Keep it for inspection and go on from the last good copy instead of starting empty
        # (starting empty would overwrite the whole history on the next save).
        logger.warning("focus.json is unreadable; restoring from the backup")
        try:
            self.path.replace(self.path.with_suffix(".corrupt"))
        except OSError:
            pass
        return self._read(self._backup_path) or {}

    def _save(self) -> None:
        self._data["phase"] = self._phase
        self._data["round"] = asdict(self._round) if self._phase != IDLE else None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_suffix(".tmp")
            temp.write_text(json.dumps(self._data), encoding="utf-8")
            if self.path.exists() and self._read(self.path) is not None:  # only a good file becomes the backup
                shutil.copyfile(self.path, self._backup_path)
            temp.replace(self.path)
        except OSError:
            logger.warning("Could not save focus state", exc_info=True)

    def _append_log(self, entry: dict[str, Any]) -> None:
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            logger.warning("Could not append to the focus log", exc_info=True)

    # ------------------------------------------------------------------ what is going on
    @property
    def phase(self) -> str:
        with self._lock:
            return self._phase

    @property
    def is_focusing(self) -> bool:
        return self.phase == FOCUS

    @property
    def is_active(self) -> bool:
        return self.phase != IDLE

    @property
    def round(self) -> Round:
        with self._lock:
            return Round(**asdict(self._round))

    def seconds_left(self) -> int:
        """Seconds until the current phase ends (0 when idle)."""
        with self._lock:
            end = self._round.ends_at if self._phase == FOCUS else self._round.break_ends_at if self._phase == BREAK else 0.0
            return max(0, int(end - self._clock() + 0.999)) if end else 0

    # ------------------------------------------------------------------ control
    @staticmethod
    def clamp_minutes(minutes: int | float) -> int:
        return max(MIN_MINUTES, min(MAX_MINUTES, int(minutes)))

    def start(self, minutes: int | float = DEFAULT_MINUTES, goal: str = "") -> Round:
        """Begin a focus round (also from a break: that is the "another round" button)."""
        with self._lock:
            if self._phase == FOCUS:
                raise ValueError("A focus session is already running.")
            now = self._clock()
            planned = self.clamp_minutes(minutes)
            self._round = Round(id=uuid.uuid4().hex[:8], started_at=now, ends_at=now + planned * 60, planned_minutes=planned, goal=goal.strip())
            self._phase = FOCUS
            self._save()
            return self.round

    def extend(self, minutes: int | float) -> Round:
        """More time: adds to the running round, or restarts the lock for ``minutes`` if it was on a break."""
        with self._lock:
            if self._phase == IDLE:
                raise ValueError("There's no session to extend.")
            extra = self.clamp_minutes(minutes)
            if self._phase == BREAK:  # more time on a goal you haven't reached keeps that goal
                return self.start(extra, goal=self._round.goal if self._round.goal_done is not True else "")
            self._round.ends_at += extra * 60
            self._round.planned_minutes += extra
            self._round.warned = False
            self._save()
            return self.round

    def stop(self) -> dict[str, Any]:
        """End everything now. Returns the summary of what was done (empty dict if nothing was running)."""
        with self._lock:
            if self._phase == IDLE:
                return {}
            summary = self._finish_round(completed=False) if self._phase == FOCUS else self._summary(completed=True)
            self._phase = IDLE
            self._save()
            return summary

    def set_goal(self, goal: str) -> bool:
        """Name (or rename) what this round is for. False when no round is running."""
        with self._lock:
            if self._phase != FOCUS:
                return False
            self._round.goal, self._round.goal_done = goal.strip(), None
            self._save()
            return True

    def set_goal_result(self, done: bool) -> str | None:
        """Your answer to "did you get it done?". Returns the goal it was about, or None when there is nothing to answer.

        Works during the round and the break, and for a while afterwards (the answer is filed under that round in the history).
        """
        with self._lock:
            history = self._data.get("history", [])
            if self._phase != IDLE and self._round.goal:
                self._round.goal_done = done
                for entry in history:
                    if entry.get("id") == self._round.id:
                        entry["goal_done"] = done
                        self._append_log(entry)  # the later line for an id replaces the earlier one
                self._save()
                return self._round.goal
            recent = self._clock() - GOAL_ANSWER_WINDOW
            for entry in reversed(history):
                if entry.get("goal") and float(entry.get("end", 0)) >= recent:
                    entry["goal_done"] = done
                    self._append_log(entry)
                    self._save()
                    return str(entry["goal"])
            return None

    def record_distraction(self, label: str) -> int:
        """Count one bounced attempt to leave. Returns the running total for this round."""
        with self._lock:
            if self._phase != FOCUS:
                return self._round.distractions
            self._round.distractions += 1
            key = (label or "something")[:60]
            self._round.blocked[key] = self._round.blocked.get(key, 0) + 1
            self._save()
            return self._round.distractions

    # ------------------------------------------------------------------ time
    def tick(self, break_minutes: int = DEFAULT_BREAK_MINUTES) -> list[Event]:
        with self._lock:
            now = self._clock()
            events: list[Event] = []
            if self._phase == FOCUS:
                if now >= self._round.ends_at:
                    if now - self._round.ends_at > STALE_AFTER:
                        summary = self._finish_round(completed=True)
                        self._phase = IDLE
                        events.append(Event("expired", summary=summary))
                    else:
                        summary = self._finish_round(completed=True)
                        self._phase = BREAK
                        self._round.break_ends_at = now + max(1, int(break_minutes)) * 60
                        events.append(Event("time_up", summary=summary))
                    self._save()
                elif not self._round.warned and self._round.planned_minutes >= 2 * WARN_MINUTES:
                    left = self._round.ends_at - now
                    if left <= WARN_MINUTES * 60:
                        self._round.warned = True
                        events.append(Event("warn", minutes_left=max(1, round(left / 60))))
                        self._save()
            elif self._phase == BREAK and now >= self._round.break_ends_at:
                summary = self._summary(completed=True)
                self._phase = IDLE
                events.append(Event("break_over", summary=summary))
                self._save()
            return events

    # ------------------------------------------------------------------ bookkeeping
    def _today(self) -> date:
        return datetime.fromtimestamp(self._clock()).date()

    def _summary(self, *, completed: bool) -> dict[str, Any]:
        r = self._round
        focused = max(0.0, min(self._clock(), r.ends_at) - r.started_at)
        return {
            "minutes": int(focused // 60),
            "planned": r.planned_minutes,
            "distractions": r.distractions,
            "blocked": dict(r.blocked),
            "completed": completed,
            "goal": r.goal,
            "goal_done": r.goal_done,
            "today_minutes": self.minutes_on(self._today()),
            "streak": self.streak(),
        }

    def _finish_round(self, *, completed: bool) -> dict[str, Any]:
        """Write the round into history (once) and return its summary."""
        r = self._round
        focused = max(0.0, min(self._clock(), r.ends_at) - r.started_at)
        history = self._data.setdefault("history", [])
        entry = {
            "id": r.id,
            "day": datetime.fromtimestamp(r.started_at).date().isoformat(),
            "start": r.started_at,
            "end": r.started_at + focused,  # when it really stopped (early stops end early)
            "minutes": round(focused / 60, 1),
            "planned": r.planned_minutes,
            "distractions": r.distractions,
            "blocked": dict(r.blocked),
            "completed": completed,
            "goal": r.goal,
            "goal_done": r.goal_done,
        }
        history.append(entry)
        self._append_log(entry)
        cutoff = (self._today() - timedelta(days=HISTORY_DAYS)).isoformat()
        self._data["history"] = [h for h in history if h.get("day", "") >= cutoff]
        summary = self._summary(completed=completed)
        return summary

    # ------------------------------------------------------------------ stats
    def minutes_on(self, day: date) -> int:
        with self._lock:
            return int(sum(h.get("minutes", 0) for h in self._data.get("history", []) if h.get("day") == day.isoformat()))

    def streak(self, today: date | None = None) -> int:
        """Days in a row (ending today, or yesterday if today hasn't got its focus yet) with real focus time."""
        today = today or self._today()
        with self._lock:
            per_day: dict[str, float] = {}
            for h in self._data.get("history", []):
                per_day[h.get("day", "")] = per_day.get(h.get("day", ""), 0) + float(h.get("minutes", 0))
        day = today if per_day.get(today.isoformat(), 0) >= STREAK_MIN_MINUTES else today - timedelta(days=1)
        count = 0
        while per_day.get(day.isoformat(), 0) >= STREAK_MIN_MINUTES:
            count += 1
            day -= timedelta(days=1)
        return count

    def stats(self) -> dict[str, Any]:
        with self._lock:
            history = list(self._data.get("history", []))
        today = self._today()
        week_days = {(today - timedelta(days=i)).isoformat() for i in range(7)}
        today_rounds = [h for h in history if h.get("day") == today.isoformat()]
        return {
            "today_minutes": int(sum(h.get("minutes", 0) for h in today_rounds)),
            "today_rounds": len(today_rounds),
            "week_minutes": int(sum(h.get("minutes", 0) for h in history if h.get("day") in week_days)),
            "week_distractions": int(sum(h.get("distractions", 0) for h in history if h.get("day") in week_days)),
            "streak": self.streak(today),
            "best_day_minutes": int(max(self._per_day(history).values(), default=0)),
        }

    @staticmethod
    def _per_day(history: list[dict[str, Any]]) -> dict[str, float]:
        totals: dict[str, float] = {}
        for h in history:
            totals[h.get("day", "")] = totals.get(h.get("day", ""), 0) + float(h.get("minutes", 0))
        return totals

    # ------------------------------------------------------------------ the log of finished rounds
    def history(self) -> list[dict[str, Any]]:
        """Every finished round (last ~4 months), oldest first."""
        with self._lock:
            return [dict(h) for h in self._data.get("history", [])]

    def rounds_on(self, day: date) -> list[dict[str, Any]]:
        """The rounds that started on ``day``, in order."""
        return sorted((h for h in self.history() if h.get("day") == day.isoformat()), key=lambda h: h.get("start", 0))

    def recap_sent_on(self) -> str:
        """The date (ISO) the end-of-day recap was last sent, or ''."""
        with self._lock:
            return str(self._data.get("recap_sent", ""))

    def mark_recap_sent(self, day: date) -> None:
        with self._lock:
            self._data["recap_sent"] = day.isoformat()
            self._save()

    def habit_state(self) -> dict[str, Any]:
        """Bookkeeping for the study-habits memory: its id, and whether the user removed it."""
        with self._lock:
            return dict(self._data.get("habit_memory", {}))

    def set_habit_state(self, state: dict[str, Any]) -> None:
        with self._lock:
            self._data["habit_memory"] = dict(state)
            self._save()

    # ------------------------------------------------------------------ your changes to the banned list
    def banned_added(self) -> tuple[str, ...]:
        """Sites and programs you banned yourself ("youtube.com", "discord.exe")."""
        with self._lock:
            return tuple(str(s) for s in self._data.get("banned_added", []))

    def banned_removed(self) -> tuple[str, ...]:
        """Built-in bans you took off the list."""
        with self._lock:
            return tuple(str(s) for s in self._data.get("banned_removed", []))

    def set_banned_changes(self, added: list[str], removed: list[str]) -> None:
        with self._lock:
            self._data["banned_added"] = list(dict.fromkeys(added))
            self._data["banned_removed"] = list(dict.fromkeys(removed))
            self._save()
