"""Words about a day of focus: the end-of-day recap. Pure functions over the rounds in the session history."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

MIN_ROUND_MINUTES = 1.0  # a round shorter than this (started, then cancelled at once) doesn't count as a round


def format_minutes(minutes: float) -> str:
    """25 -> '25 min', 60 -> '1 hour', 75 -> '1h 15m'."""
    total = int(round(minutes))
    hours, mins = divmod(total, 60)
    if not hours:
        return f"{mins} min"
    if not mins:
        return f"{hours} hour{'s' if hours != 1 else ''}"
    return f"{hours}h {mins:02d}m"


def hm(epoch: float) -> str:
    return datetime.fromtimestamp(epoch).strftime("%H:%M")


def real_rounds(rounds: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in rounds if float(r.get("minutes", 0)) >= MIN_ROUND_MINUTES]


def day_facts(rounds: list[dict[str, Any]]) -> dict[str, Any]:
    """Totals for one day's rounds: minutes, how many, first start, last finish, what was bounced."""
    rounds = real_rounds(rounds)
    blocked: dict[str, int] = {}
    for r in rounds:
        for name, count in (r.get("blocked") or {}).items():
            blocked[name] = blocked.get(name, 0) + int(count)
    starts = [float(r["start"]) for r in rounds if r.get("start")]
    ends = [float(r["end"]) for r in rounds if r.get("end")]
    return {
        "minutes": sum(float(r.get("minutes", 0)) for r in rounds),
        "rounds": len(rounds),
        "first_start": min(starts) if starts else None,
        "last_end": max(ends) if ends else None,
        "distractions": sum(int(r.get("distractions", 0)) for r in rounds),
        "blocked": blocked,
    }


def top_blocked(blocked: dict[str, int], limit: int = 3) -> str:
    return ", ".join(f"{name} ×{count}" for name, count in sorted(blocked.items(), key=lambda kv: (-kv[1], kv[0]))[:limit])


def goal_lines(rounds: list[dict[str, Any]]) -> list[str]:
    """One line per goal you set today: reached (✓), not reached (✗) or never answered (·). Empty when there were none."""
    goals = [r for r in sorted(real_rounds(rounds), key=lambda r: r.get("start", 0)) if r.get("goal")]
    if not goals:
        return []
    mark = {True: "✓", False: "✗"}
    lines = ["Goals:"] + [f"  {mark.get(r.get('goal_done'), '·')} {r['goal']}" for r in goals[:8]]
    done = sum(1 for r in goals if r.get("goal_done") is True)
    return lines + [f"{done} of {len(goals)} reached."] if len(goals) > 1 else lines


def recap_text(
    day: date,
    rounds: list[dict[str, Any]],
    *,
    usual_minutes: float | None = None,
    streak: int = 0,
    running_until: float | None = None,
) -> str:
    """The end-of-day message: how long you focused, when you started, when you finished, and how it went."""
    facts = day_facts(rounds)
    title = f"Focus recap · {day.strftime('%A %d %B')}"
    if not facts["rounds"]:
        return f"{title}\nNo focus rounds today."
    lines = [title, f"Focused {format_minutes(facts['minutes'])} in {facts['rounds']} round{'s' if facts['rounds'] != 1 else ''}."]
    if facts["first_start"] is not None:  # rounds logged before times were kept have no clock times
        if running_until:
            lines.append(f"Started {hm(facts['first_start'])}. A round is still running until {hm(running_until)}.")
        elif facts["last_end"] is not None:
            lines.append(f"Started {hm(facts['first_start'])}, finished {hm(facts['last_end'])}.")
    elif running_until:
        lines.append(f"A round is still running until {hm(running_until)}.")
    timed = [r for r in real_rounds(rounds) if r.get("start") and r.get("end")]
    if facts["rounds"] > 1:
        for r in sorted(timed, key=lambda r: r["start"])[:8]:
            lines.append(f"  {hm(r['start'])}–{hm(r['end'])}  {format_minutes(r['minutes'])}")
    lines.extend(goal_lines(rounds))
    if facts["distractions"]:
        plural = "s" if facts["distractions"] != 1 else ""
        lines.append(f"Bounced {facts['distractions']} distraction{plural} ({top_blocked(facts['blocked'])}).")
    else:
        lines.append("Zero distractions bounced.")
    tail = []
    if usual_minutes and usual_minutes >= 10:
        diff = facts["minutes"] - usual_minutes
        if abs(diff) < 10:
            tail.append(f"Right on your usual {format_minutes(usual_minutes)}.")
        else:
            tail.append(f"That's {format_minutes(abs(diff))} {'more' if diff > 0 else 'less'} than your usual {format_minutes(usual_minutes)}.")
    if streak >= 2:
        tail.append(f"{streak}-day streak.")
    if tail:
        lines.append(" ".join(tail))
    return "\n".join(lines)
