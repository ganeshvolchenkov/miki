"""Every screen of the phone bot, as pure functions: data in, ``Screen`` (text + buttons) out.

No network, no state: the bot (controller) fetches data and decides where to show a screen; this module only
decides what it looks like. All dynamic text is HTML-escaped (email subjects, memories and names are untrusted).

Callback data scheme (Telegram allows 64 bytes):
    nav:home | nav:mail | nav:today:<offset> | nav:mem:<page> | nav:profile | nav:settings | nav:model | nav:add
    mem:<id>:<page> | memf:<id>:<page> | memy:<id>:<page>     open / ask to forget / confirm forget a memory
    md:<mail id> | mz:<mail id>                                dismiss / snooze an email
    set:urgent | set:brief | set:voice | set:quiet:<preset> | set:btime:<HHMM>
    model:<index> | iv:start | iv:skip | iv:stop | cf:yes | cf:no | ask:<event|remember|chat> | tts:<key>
    nav:focus | fc:start:<minutes> | fc:more:<minutes> | fc:goal:yes | fc:goal:no | fc:stop | fc:stopy | fc:status | fc:stats | fc:today | fc:habits | fc:banned | fc:done
    pl:ok | pl:edit | pl:drop | pl:show | pl:cancel | pl:cancely | pl:fc:<block index>     the day plan (/plan)
"""

from __future__ import annotations

import html
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

Buttons = list[list[dict[str, str]]]

MEMORIES_PER_PAGE = 5

QUIET_PRESETS: list[tuple[str, str]] = [("off", "Off"), ("23:00-08:00", "23–08"), ("22:00-07:00", "22–07"), ("00:00-09:00", "00–09")]
BRIEF_TIMES = ["07:30", "08:30", "09:30"]

# The keyboard pinned at the bottom of the chat (its buttons arrive as ordinary text messages).
REPLY_KEYBOARD: dict[str, Any] = {
    "keyboard": [
        [{"text": "📬 Mail"}, {"text": "📅 Today"}, {"text": "🧠 Memory"}],
        [{"text": "🏠 Home"}, {"text": "➕ Add"}, {"text": "⚙️ Settings"}],
        [{"text": "🎯 Focus"}],
    ],
    "resize_keyboard": True,
    "is_persistent": True,
    "input_field_placeholder": "Message Miki…",
}
KEYBOARD_LABELS: dict[str, str] = {
    "📬 Mail": "mail", "📅 Today": "today", "🧠 Memory": "memory", "🏠 Home": "home", "➕ Add": "add", "⚙️ Settings": "settings", "🎯 Focus": "focus",
}


@dataclass
class Screen:
    text: str
    buttons: Buttons | None = None
    html: bool = True


def esc(value: object) -> str:
    return html.escape(str(value), quote=False)


def _b(text: str, data: str) -> dict[str, str]:
    return {"text": text, "callback_data": data}


def _home_row() -> list[dict[str, str]]:
    return [_b("🏠 Home", "nav:home")]


def _onoff(value: bool) -> str:
    return "ON" if value else "OFF"


# ------------------------------------------------------------------------------------------------ home
@dataclass
class HomeData:
    now: datetime
    name: str = ""
    weather: str = ""
    next_event: str = ""  # e.g. "Gym 19:00 (in 4h 28m)"
    events_today: int = 0
    mail_attention: int = 0
    mail_urgent: int = 0
    memories: int = 0
    coverage: float | None = None
    model: str = ""
    lines: list[str] = field(default_factory=list)


def _greeting(now: datetime) -> str:
    hour = now.hour
    return "Good morning" if 5 <= hour < 12 else "Good afternoon" if 12 <= hour < 18 else "Good evening" if hour >= 18 else "Still up"


def home_lines(data: HomeData) -> list[str]:
    lines = []
    if data.weather:
        lines.append(f"⛅ {esc(data.weather)}")
    if data.next_event:
        extra = f" · {data.events_today} event{'s' if data.events_today != 1 else ''} today" if data.events_today else ""
        lines.append(f"📅 Next: {esc(data.next_event)}{extra}")
    elif data.events_today == 0:
        lines.append("📅 Nothing on your calendar today")
    if data.mail_attention:
        urgent = f" ({data.mail_urgent} urgent)" if data.mail_urgent else ""
        lines.append(f"📬 {data.mail_attention} need attention{urgent}")
    else:
        lines.append("📬 Inbox is calm")
    if data.memories:
        know = f" · I know you {data.coverage:.0%}" if data.coverage else ""
        lines.append(f"🧠 {data.memories} memories{know}")
    return lines


def home_screen(data: HomeData) -> Screen:
    who = f", {esc(data.name)}" if data.name else ""
    header = f"🏠 <b>{_greeting(data.now)}{who}</b>\n<i>{data.now.strftime('%A %d %B · %H:%M')}</i>"
    body = "\n".join(home_lines(data))
    footer = f"\n\n<code>{esc(data.model)}</code>" if data.model else ""
    mail_label = f"📬 Mail ({data.mail_attention})" if data.mail_attention else "📬 Mail"
    buttons: Buttons = [
        [_b(mail_label, "nav:mail"), _b("📅 Today", "nav:today:0")],
        [_b("🧠 Memory", "nav:mem:0"), _b("👤 Profile", "nav:profile")],
        [_b("➕ Add", "nav:add"), _b("⚙️ Settings", "nav:settings")],
        [_b("🎯 Focus", "nav:focus"), _b("🔄 Refresh", "nav:home")],
    ]
    return Screen(f"{header}\n\n{body}{footer}", buttons)


def brief_screen(data: HomeData, events: list[dict[str, Any]], urgent: list[Any]) -> Screen:
    """"Your day in one message": the same facts as the home card, plus the schedule and what's urgent."""
    lines = [f"☀️ <b>{_greeting(data.now)}{', ' + esc(data.name) if data.name else ''}</b>", f"<i>{data.now.strftime('%A %d %B')}</i>", ""]
    lines += home_lines(data)
    if events:
        lines += ["", "📅 <b>Schedule</b>"]
        for event in events[:8]:
            when = "All day" if event.get("all_day") else f"{str(event.get('start', ''))[11:16]}–{str(event.get('end', ''))[11:16]}"
            lines.append(f"<code>{esc(when):<11}</code> {esc(event.get('title', ''))}")
    if urgent:
        lines += ["", "🔴 <b>Urgent</b>"]
        for item in urgent[:3]:
            lines.append(f"• {esc(item.sender_name[:24])} — {esc(item.subject[:50])}")
    return Screen(chr(10).join(lines), [[_b("📬 Mail", "nav:mail"), _b("📅 Today", "nav:today:0")], _home_row()])


# ------------------------------------------------------------------------------------------------ mail
_PRIORITY_BADGE = {"urgent": "🔴 <b>URGENT</b>", "important": "🟠 <b>Important</b>"}


def mail_header(count: int, shown: int) -> Screen:
    if count == 0:
        return Screen("✨ <b>Inbox looks calm.</b>\nNothing needs you right now.", [[_b("🔄 Check again", "nav:mail"), *_home_row()]])
    more = f"\n<i>Showing the top {shown}.</i>" if count > shown else ""
    return Screen(f"📬 <b>{count} need{'s' if count == 1 else ''} your attention</b>{more}", [[_b("🔄 Refresh", "nav:mail"), *_home_row()]])


def mail_card(item: Any, gmail_link: str) -> Screen:
    detail = item.reason or item.snippet[:100]
    if item.due:
        detail += f" · due {item.due}"
    badge = _PRIORITY_BADGE.get(item.priority, esc(item.priority))
    text = f"{badge} · {esc(item.sender_name[:40])}\n{esc(item.subject[:120])}\n<i>{esc(detail[:200])}</i>"
    return Screen(text, [[
        {"text": "📨 Open in Gmail", "url": gmail_link},
        _b("✓ Dismiss", f"md:{item.id}"),
        _b("💤 Snooze", f"mz:{item.id}"),
    ]])


def mail_done_text(item_text: str, verb: str) -> str:
    """The card's text after acting on it (buttons are removed by the caller)."""
    return f"<s>{item_text}</s>\n<i>{verb}</i>" if item_text else f"<i>{verb}</i>"


# ------------------------------------------------------------------------------------------------ calendar
def _day_label(offset: int, day: datetime) -> str:
    named = {0: "Today", 1: "Tomorrow", -1: "Yesterday"}.get(offset)
    return f"{named} · {day.strftime('%A %d %B')}" if named else day.strftime("%A %d %B")


def today_screen(offset: int, day: datetime, events: list[dict[str, Any]]) -> Screen:
    lines = [f"📅 <b>{esc(_day_label(offset, day))}</b>", ""]
    if not events:
        lines.append("<i>Nothing scheduled.</i>")
    for event in events:
        if event.get("all_day"):
            when = "All day"
        else:
            start, end = str(event.get("start", ""))[11:16], str(event.get("end", ""))[11:16]
            when = f"{start}–{end}" if start and end else start
        place = f"\n      📍 {esc(event['location'])}" if event.get("location") else ""
        lines.append(f"<code>{esc(when):<11}</code> {esc(event.get('title', ''))}{place}")
    buttons: Buttons = [
        [_b("◀ Prev", f"nav:today:{offset - 1}"), _b("Next ▶", f"nav:today:{offset + 1}")],
        [_b("➕ Add event", "ask:event"), *_home_row()],
    ]
    return Screen("\n".join(lines), buttons)


# ------------------------------------------------------------------------------------------------ memory
def memory_list_screen(memories: list[Any], page: int) -> Screen:
    pages = max(1, -(-len(memories) // MEMORIES_PER_PAGE))
    page = min(max(0, page), pages - 1)
    chunk = memories[page * MEMORIES_PER_PAGE : (page + 1) * MEMORIES_PER_PAGE]
    if not memories:
        return Screen("🧠 <b>I don't remember anything yet.</b>\nTell me about yourself, or tap Add ▸ Remember.", [[_b("➕ Add", "nav:add"), *_home_row()]])
    buttons: Buttons = [[_b(f"• {(m.title or m.content)[:46]}", f"mem:{m.memory_id}:{page}")] for m in chunk]
    if pages > 1:
        buttons.append([
            _b("◀", f"nav:mem:{(page - 1) % pages}"),
            _b(f"{page + 1}/{pages}", f"nav:mem:{page}"),
            _b("▶", f"nav:mem:{(page + 1) % pages}"),
        ])
    buttons.append([_b("👤 Profile", "nav:profile"), *_home_row()])
    return Screen(f"🧠 <b>What I remember</b> · {len(memories)} memories\n<i>Tap one to see it or forget it.</i>", buttons)


def memory_detail_screen(memory: Any, related: list[Any], page: int) -> Screen:
    lines = [f"🧠 <b>{esc(memory.title or memory.content[:60])}</b>", "", esc(memory.content)]
    meta = [memory.category]
    if memory.entities:
        meta.append(" ".join(f"#{esc(e.replace(' ', '_'))}" for e in memory.entities[:5]))
    lines += ["", f"<i>{' · '.join(esc(m) if i == 0 else m for i, m in enumerate(meta))}</i>"]
    if related:
        lines.append("Linked to: " + ", ".join(esc((r.title or r.content)[:30]) for r in related[:3]))
    return Screen("\n".join(lines), [[_b("🗑 Forget", f"memf:{memory.memory_id}:{page}"), _b("◀ Back", f"nav:mem:{page}")]])


def forget_confirm_screen(memory: Any, page: int) -> Screen:
    return Screen(
        f"Forget this?\n\n<b>{esc(memory.title or memory.content[:60])}</b>\n{esc(memory.content)}",
        [[_b("✅ Yes, forget it", f"memy:{memory.memory_id}:{page}"), _b("✖ Keep", f"mem:{memory.memory_id}:{page}")]],
    )


# ------------------------------------------------------------------------------------------------ profile & interview
def profile_screen(profile: Any) -> Screen:
    if profile is None or (not profile.portrait and not profile.memory_count):
        return Screen("👤 <b>I don't know you well enough yet.</b>\nLet me ask you some questions.", [[_b("🎙 Start interview", "iv:start"), *_home_row()]])
    lines = ["👤 <b>Who I think you are</b>", ""]
    if profile.portrait:
        lines += [f"<i>{esc(profile.portrait)}</i>", ""]
    lines.append(f"<b>How well I know you: {profile.coverage:.0%}</b>")
    rows = []
    for domain in profile.domains:
        filled = round(domain.confidence * 5)
        rows.append(f"{esc(domain.title[:20]):<20} {'▰' * filled}{'▱' * (5 - filled)} {domain.confidence:>4.0%}")
    lines.append("<pre>" + "\n".join(rows) + "</pre>")
    weakest = profile.weakest(2)
    if weakest:
        lines.append("Least known: " + ", ".join(esc(d.title) for d in weakest))
    return Screen("\n".join(lines), [[_b("🎙 Interview me", "iv:start"), _b("🔄 Refresh", "nav:profile")], [_b("🧠 Memory", "nav:mem:0"), *_home_row()]])


def interview_screen(text: str, *, finished: bool = False) -> Screen:
    if finished:
        return Screen(esc(text), [[_b("👤 Profile", "nav:profile"), *_home_row()]])
    return Screen(esc(text), [[_b("⏭ Skip", "iv:skip"), _b("⏹ Stop", "iv:stop")]])


# ------------------------------------------------------------------------------------------------ settings & model
def settings_screen(prefs: dict[str, Any], model: str) -> Screen:
    quiet = str(prefs["quiet_hours"])
    quiet_text = "Off" if quiet in {"off", "", "none"} else quiet.replace("-", "–")
    text = (
        "⚙️ <b>Settings</b>\n\n"
        f"🔔 Urgent mail alerts: <b>{_onoff(prefs['urgent_push'])}</b>\n"
        f"🌙 Quiet hours: <b>{esc(quiet_text)}</b>\n"
        f"☀️ Morning brief: <b>{_onoff(prefs['morning_brief'])}</b> ({esc(prefs['brief_time'])})\n"
        f"🔊 Voice replies to voice notes: <b>{_onoff(prefs['voice_replies'])}</b>\n"
        f"🤖 Model: <code>{esc(model)}</code>"
    )
    quiet_row = [_b(("🌙 " if i == 0 else "") + ("• " if quiet == value else "") + label, f"set:quiet:{value}") for i, (value, label) in enumerate(QUIET_PRESETS)]
    time_row = [_b(("☀️ " if i == 0 else "") + ("• " if prefs["brief_time"] == t else "") + t, f"set:btime:{t.replace(':', '')}") for i, t in enumerate(BRIEF_TIMES)]
    buttons: Buttons = [
        [_b(f"🔔 Alerts: {_onoff(prefs['urgent_push'])}", "set:urgent"), _b(f"🔊 Voice: {_onoff(prefs['voice_replies'])}", "set:voice")],
        quiet_row,
        [_b(f"☀️ Morning brief: {_onoff(prefs['morning_brief'])}", "set:brief")],
        time_row,
        [_b("🤖 Change model", "nav:model"), *_home_row()],
    ]
    return Screen(text, buttons)


def model_screen(current: str, choices: list[tuple[str, str, str]]) -> Screen:
    tier_icon = {"cheapest": "🟢", "cheap": "🟢", "balanced": "🟡", "premium": "🔴"}
    buttons: Buttons = [
        [_b(f"{'✅ ' if name == current else ''}{name} · {tier_icon.get(tier, '')}{tier}", f"model:{i}")] for i, (name, tier, _) in enumerate(choices)
    ]
    buttons.append([_b("◀ Settings", "nav:settings"), *_home_row()])
    return Screen(f"🤖 <b>Choose a model</b>\nCheaper tiers cost far less per message.\nNow: <code>{esc(current)}</code>", buttons)


# ------------------------------------------------------------------------------------------------ add, help, prompts
def add_screen() -> Screen:
    return Screen("➕ <b>What would you like to add?</b>", [
        [_b("📅 Calendar event", "ask:event")],
        [_b("🧠 Remember something", "ask:remember")],
        [_b("💬 Just ask Miki", "ask:chat"), *_home_row()],
    ])


PROMPTS: dict[str, tuple[str, str]] = {
    # kind -> (message, input placeholder)
    "event": ("📅 <b>What's the event?</b>\nFor example: <i>gym tomorrow 7–8:30pm at Trainmore</i>", "gym tomorrow 7pm…"),
    "remember": ("🧠 <b>What should I remember?</b>", "I'm allergic to…"),
    "chat": ("💬 <b>What's on your mind?</b>", "Ask me anything…"),
    "plan": ("✏️ <b>What should change?</b>\nFor example: <i>gym in the morning</i> or <i>only 2 hours of calculus</i>", "gym in the morning…"),
}


def prompt_markup(kind: str) -> dict[str, Any]:
    return {"force_reply": True, "input_field_placeholder": PROMPTS[kind][1], "selective": True}


def help_screen() -> Screen:
    return Screen(
        "🤖 <b>I'm Miki.</b> Talk to me, send a voice note, or use the buttons.\n\n"
        "📬 <b>Mail</b>: what needs your attention, with Open / Dismiss / Snooze\n"
        "📅 <b>Today</b>: your calendar, day by day\n"
        "🧠 <b>Memory</b>: browse, open and forget what I know\n"
        "➕ <b>Add</b>: events and memories, step by step\n"
        "👤 <b>Profile</b>: who I think you are, plus a quick interview\n"
        "🎯 <b>/focus</b>: study mode, e.g. <code>/focus 1 hour</code>, or with a goal: <code>/focus 50 min, finish lecture 6</code>. Screens set up, distractions blocked, a break, a daily recap\n"
        "📋 <b>/plan</b>: plan your day in plain English, e.g. <code>/plan study 5h linear algebra, 3h calculus, 50 min lunch, gym, home by 8pm</code>. Travel, rounds and your calendar worked out, nudges through the day\n"
        "☀️ <b>/brief</b>: your day in one message\n"
        "⚙️ <b>Settings</b>: alerts, quiet hours, voice, model\n\n"
        "Type <code>/remember …</code>, <code>/forget …</code> or <code>/memory …</code> any time.",
        [[_b("🏠 Home", "nav:home"), _b("⚙️ Settings", "nav:settings")]],
    )


def confirm_buttons() -> Buttons:
    return [[_b("✅ Confirm", "cf:yes"), _b("✖ Cancel", "cf:no")]]


def listen_button(key: str) -> dict[str, str]:
    return _b("🔊 Listen", f"tts:{key}")


def welcome_text(name: str) -> str:
    return f"👋 <b>Linked, {esc(name)}.</b> I'm Miki.\nUse the buttons below, or just talk to me."


# ------------------------------------------------------------------------------------------------ focus mode
FOCUS_ROUNDS = (25, 45, 60)


def focus_buttons(phase: str, minutes: int = 60) -> Buttons:
    """The buttons that fit each moment: start, keep going or stop while focusing, what next on a break."""
    if phase == "focus":
        return [[_b("🔄 Status", "fc:status"), _b("➕ 15 min", "fc:more:15"), _b("⏹ End", "fc:stop")]]
    if phase == "break":
        return [[_b("➕ 15 more minutes", "fc:more:15"), _b(f"▶️ New {minutes} min round", f"fc:start:{minutes}")], [_b("✅ I'm done", "fc:stop")]]
    return [[_b(f"▶️ {m} min", f"fc:start:{m}") for m in FOCUS_ROUNDS], [_b("📅 Today", "fc:today"), _b("📊 Stats", "fc:stats"), _b("🧠 Habits", "fc:habits")], [_b("🚫 Banned", "fc:banned"), *_home_row()]]


def focus_screen(view: dict[str, Any]) -> Screen:
    """``view``: phase, minutes_left, until, distractions, today_minutes, streak, minutes (default round)."""
    phase = view.get("phase", "idle")
    if phase == "focus":
        n = view["distractions"]
        head = (f"🎯 <b>Focusing</b> · {view['minutes_left']} min left\n"
                f"Until {esc(view['until'])} · {n} distraction{'s' if n != 1 else ''} bounced")
        if view.get("goal"):
            head += f"\n🏁 {esc(view['goal'])}" + (" ✓" if view.get("goal_done") else "")
    elif phase == "break":
        head = f"☕ <b>Break time</b> · {view['minutes_left']} min left\nThe lock is off. Stretch, drink some water."
        if view.get("goal"):
            head += f"\n🏁 {esc(view['goal'])}" + {True: " ✓", False: " (not yet)"}.get(view.get("goal_done"), "")
    else:
        head = "🎯 <b>Focus mode</b>\nGemini on Screen 1, Claude on Screen 2, distractions (YouTube, TikTok, Discord...) blocked. I'll call a break when the time is up."
    tail = f"\n\n📈 Today: {view['today_minutes']} min" + (f" · 🔥 {view['streak']}-day streak" if view.get("streak", 0) >= 2 else "")
    return Screen(head + tail, focus_buttons(phase, int(view.get("minutes", 60))))


def plan_buttons(status: str) -> Buttons | None:
    """Under a plan: approve or change a draft; change or cancel the day's plan."""
    if status == "draft":
        return [[_b("✅ Looks good", "pl:ok"), _b("✏️ Change", "pl:edit")], [_b("✖ Discard", "pl:drop")]]
    if status == "active":
        return [[_b("🔄 Show", "pl:show"), _b("✏️ Change", "pl:edit"), _b("✖ Cancel plan", "pl:cancel")]]
    return None


def plan_cancel_confirm() -> Screen:
    return Screen("✖ <b>Cancel today's plan?</b>\nI'll take its blocks out of your calendar too.",
                  [[_b("✅ Yes, cancel it", "pl:cancely"), _b("↩️ Keep it", "pl:show")]])


def plan_nudge_buttons(block: int | None, minutes: int) -> Buttons | None:
    """A subject is starting: one tap starts a focus round for it."""
    return [[_b(f"▶️ Focus {minutes} min", f"pl:fc:{block}")]] if block is not None else None


def inbox_buttons(batch: str, link: str = "") -> Buttons | None:
    """Under something Miki did on its own from mail or a photo: undo it in one tap, or open the link it found."""
    row: list[dict[str, str]] = []
    if link.startswith("https://"):
        row.append({"text": "🔗 Open link", "url": link})
    if batch:
        row.append(_b("↩️ Undo", f"ib:undo:{batch}"))
    return [row] if row else None


def focus_stop_confirm() -> Screen:
    return Screen("⏹ <b>End focus early?</b>\nYou still have time left.", [[_b("✅ Yes, end it", "fc:stopy"), _b("💪 Keep going", "fc:status")]])


def focus_event_screen(kind: str, text: str, minutes: int = 60, ask_goal: bool = False) -> Screen:
    """A push for something the clock decided: 10 minutes left, break time, break over.

    ``ask_goal``: the round had a goal nobody has answered for yet, so the break screen asks "did you finish it?" first.
    """
    icon = {"warn": "⏳", "time_up": "☕", "break_over": "🔔", "recap": "🌙"}.get(kind, "🎯")
    if kind in {"time_up", "recap"}:
        first, _, rest = text.partition("\n")
        buttons = focus_buttons("break", minutes) if kind == "time_up" else None
        if buttons is not None and ask_goal:
            buttons = [[_b("✅ Got it done", "fc:goal:yes"), _b("⏳ Not yet", "fc:goal:no")], *buttons]
        return Screen(f"{icon} <b>{esc(first)}</b>\n{esc(rest)}", buttons)
    if kind == "break_over":
        return Screen(f"{icon} <b>{esc(text)}</b>", [[_b(f"▶️ Focus {minutes} min", f"fc:start:{minutes}"), _b("✅ I'm done", "fc:done")]])
    return Screen(f"{icon} {esc(text)}", None)
