"""How long should the round be? Turns what you typed into minutes.

    /focus 45            45 minutes
    /focus 1 hour        60      /focus 30 mins   30      /focus 1h30      90      /focus 1.5 hours   90
    /focus half an hour  30      /focus an hour and a half   90       /focus 1:15      75

Plain patterns are handled here, offline and instantly. Anything stranger ("until 3pm", "the length of a
lecture") is handed to an optional ``ai`` callable that gets the question and the current time. Whatever comes
back is checked against the allowed range, so a confused model can never start a 12-hour lock.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

MIN_MINUTES, MAX_MINUTES = 5, 240

_WORDS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15, "twenty": 20, "thirty": 30, "forty": 40,
    "fifty": 50, "sixty": 60, "ninety": 90, "half": 0.5, "quarter": 0.25,
}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50}
_UNITS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9}
_NUMBER = r"\d+(?:[.,]\d+)?|" + "|".join(sorted((re.escape(w) for w in _WORDS), key=len, reverse=True))
_HOURS = r"(?:hours?|hrs?|h|uur)"
_MINUTES = r"(?:minutes?|mins?|m)"


@dataclass(frozen=True)
class Duration:
    minutes: int | None  # None: not understood or out of range, see ``error``
    error: str = ""
    via_ai: bool = False

    @property
    def ok(self) -> bool:
        return self.minutes is not None


def _number(token: str) -> float:
    token = token.lower()
    return float(_WORDS[token]) if token in _WORDS else float(token.replace(",", "."))


def parse_offline(text: str) -> float | None:
    """Minutes (maybe fractional) for the common ways of saying a length of time, else None."""
    raw = (text or "").strip().lower()
    raw = re.sub(r"(\d),(\d)", r"\1.\2", raw)  # a decimal comma: 1,5 hours
    raw = re.sub(  # "forty five" / "forty-five" -> 45
        r"\b(twenty|thirty|forty|fifty)[ -](one|two|three|four|five|six|seven|eight|nine)\b",
        lambda m: str(_TENS[m[1]] + _UNITS[m[2]]), raw,
    )
    raw = re.sub(r"\b(for|about|around|approximately|roughly|please|focus|session|of|the|in)\b|[~,]", " ", raw)
    raw = re.sub(r"\s+", " ", raw).strip()
    if not raw:
        return None

    if re.fullmatch(r"\d+", raw):
        return float(raw)  # a bare number means minutes
    if match := re.fullmatch(r"(\d{1,2}):(\d{2})", raw):
        return int(match[1]) * 60 + int(match[2])  # 1:30

    # "an hour and a half", "2 hours and a half"
    if match := re.fullmatch(rf"({_NUMBER}) {_HOURS} and (?:a )?half", raw):
        return (_number(match[1]) + 0.5) * 60
    if match := re.fullmatch(rf"({_NUMBER}) and (?:a )?half {_HOURS}", raw):  # "two and a half hours"
        return (_number(match[1]) + 0.5) * 60
    if re.fullmatch(r"hour and (?:a )?half", raw):
        return 90.0
    # "half an hour", "quarter hour", "three quarters of an hour" (the fillers are already stripped)
    if re.fullmatch(r"half (?:an? )?hour", raw):
        return 30.0
    if re.fullmatch(r"(?:a )?quarter (?:an? )?hour", raw):
        return 15.0
    if re.fullmatch(r"(?:three|3) quarters? (?:an? )?hour", raw):
        return 45.0
    # "1h30", "1 hour 30"
    if match := re.fullmatch(rf"(\d+) ?{_HOURS} ?(\d{{1,2}})", raw):
        return int(match[1]) * 60 + int(match[2])

    total, matched = 0.0, False
    consumed = raw
    for match in re.finditer(rf"({_NUMBER}) ?({_HOURS}|{_MINUTES})\b", raw):
        value = _number(match[1])
        total += value * (60 if re.fullmatch(_HOURS, match[2]) else 1)
        matched = True
        consumed = consumed.replace(match[0], " ", 1)
    if not matched:
        return None
    leftover = re.sub(r"\b(and|a|an)\b|[+&]", " ", consumed)
    if re.search(r"[a-z0-9]", leftover):
        return None  # something we don't understand is left ("30 min then a break"): ask the AI rather than guess
    return total


def check_range(minutes: float, *, bare: bool = False) -> Duration:
    rounded = int(round(minutes))
    if rounded < MIN_MINUTES:
        plural = "s" if rounded != 1 else ""
        hint = f" Did you mean {rounded} hour{plural}? Say /focus {rounded} hour{plural}." if bare and rounded >= 1 else ""
        return Duration(None, f"{rounded} minute{plural} is too short for a focus round (the minimum is {MIN_MINUTES}).{hint}")
    if rounded > MAX_MINUTES:
        return Duration(None, f"That's {rounded // 60}h {rounded % 60:02d}m. A round can be at most {MAX_MINUTES // 60} hours: start one now and another after the break.")
    return Duration(rounded)


def resolve(text: str, ai: Callable[[str, datetime], int | None] | None = None, now: datetime | None = None) -> Duration:
    """Minutes for ``text``: offline first, then the AI (if given). Never raises."""
    text = (text or "").strip()[:120]
    if not text:
        return Duration(None, "How long? For example /focus 45 min or /focus 1 hour.")
    offline = parse_offline(text)
    if offline is not None:
        return check_range(offline, bare=bool(re.fullmatch(r"\d+", text)))
    if ai is not None and re.search(r"[a-z0-9]", text.lower()):
        try:
            guessed = ai(text, now or datetime.now())
        except Exception:
            guessed = None
        if guessed is not None:
            result = check_range(guessed)
            return Duration(result.minutes, result.error, via_ai=result.ok)
    return Duration(None, "I couldn't tell how long you mean. Try /focus 45 min or /focus 1 hour.")


# ------------------------------------------------------------------------------------------------ a goal after the length
#   /focus 50 mins, I will finish lecture 6     ->  50 minutes, goal "finish lecture 6"
#   /focus 1h30 - do the practice exam          ->  90 minutes, goal "do the practice exam"
#   /focus 45 revise chapter 3                  ->  45 minutes, goal "revise chapter 3"
#   /focus until 3pm, finish the essay          ->  the AI reads "until 3pm", goal "finish the essay"
MAX_GOAL_CHARS = 120
_SEPARATOR = re.compile(r"\s*(?:(?<!\d)[,;:]|[,;:](?!\d)|\s[-–—]\s|\.\s)\s*")  # not the comma or colon inside 1,5 or 1:30
_LENGTHY = re.compile(r"(?:until|till|for|about|around)\b|.*\b(?:hours?|hrs?|mins?|minutes?|pomodoro)\b", re.I)
_GOAL_LEAD = re.compile(
    r"(?:and|so|then|to|that|i will|i'll|i want to|i need to|i have to|i must|i'm going to|i am going to|we will|let's)(?![\w'])\s*", re.I
)


def clean_goal(text: str) -> str:
    """"I will finish lecture 6." -> "finish lecture 6": no lead-in, no trailing full stop, a sane length."""
    goal = re.sub(r"\s+", " ", (text or "").strip()).strip(" .!-–—,;:")
    while lead := _GOAL_LEAD.match(goal):
        goal = goal[lead.end():]
    goal = goal.strip(" .!-–—,;:")
    if len(goal) > MAX_GOAL_CHARS:
        goal = goal[: MAX_GOAL_CHARS - 1].rstrip() + "…"
    return goal


def resolve_with_goal(text: str, ai: Callable[[str, datetime], int | None] | None = None, now: datetime | None = None) -> tuple[Duration, str]:
    """Minutes and the goal from ``"50 mins, I will finish lecture 6"``. The goal is "" when there isn't one. Never raises."""
    text = (text or "").strip()[:300]
    if not text or parse_offline(text) is not None:
        return resolve(text, ai, now), ""

    if (split := _SEPARATOR.search(text)) and text[: split.start()].strip() and text[split.end():].strip():
        head, goal = text[: split.start()].strip(), clean_goal(text[split.end():])
        if goal:
            offline = parse_offline(head)
            if offline is not None:
                return check_range(offline, bare=bool(re.fullmatch(r"\d+", head))), goal
            if ai is not None and _LENGTHY.match(head):
                result = resolve(head, ai, now)
                if result.ok:
                    return result, goal

    words = text.split()  # no separator ("45 revise chapter 3"): the longest opening that reads as a length
    for count in range(min(len(words) - 1, 6), 0, -1):
        head = " ".join(words[:count])
        offline = parse_offline(head)
        goal = clean_goal(" ".join(words[count:]))
        if offline is not None and goal:
            return check_range(offline, bare=bool(re.fullmatch(r"\d+", head))), goal
    return resolve(text[:120], ai, now), ""


# ------------------------------------------------------------------------------------------------ the AI fallback
_SYSTEM_PROMPT = """You turn a request for a focus-session length into whole minutes.
The current local time is {now}.
Reply ONLY with JSON: {{"minutes": <integer>}} or {{"minutes": null}} if no length can be worked out.
Examples: "an hour and ten" -> 70; "until 3pm" at 13:20 -> 100; "as long as a lecture" -> 90; "a pomodoro" -> 25; "forever" -> null."""


def make_ai_minutes(brain: object) -> Callable[[str, datetime], int | None]:
    """An ``ai`` callable for :func:`resolve`, backed by Miki's brain (anything with ``generate_response``)."""

    def ask(text: str, now: datetime) -> int | None:
        reply = brain.generate_response(  # type: ignore[attr-defined]
            user_input=text, system_prompt=_SYSTEM_PROMPT.format(now=now.strftime("%A %H:%M")), history=None
        )
        match = re.search(r"\{.*?\}", reply or "", re.S)
        if not match:
            return None
        value = json.loads(match.group(0)).get("minutes")
        return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None

    return ask
