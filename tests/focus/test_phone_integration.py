import time
from datetime import datetime
from unittest.mock import MagicMock

import pytest

from app.phone import ui
from app.phone.bot import PhoneBot
from app.phone.notifier import PhoneNotifier
from app.phone.state import PhoneState

from .test_service import make, start_and_wait

OWNER, STRANGER = 111, 222


@pytest.fixture(autouse=True)
def no_sleeping(monkeypatch):
    from app.focus import service as service_module

    monkeypatch.setattr(service_module.time, "sleep", lambda s: None)


def bot_for(tmp_path, focus):
    state = PhoneState(tmp_path / "phone.json")
    state.try_pair(state.new_pairing_code(), OWNER, "Ahmet")
    api = MagicMock()
    api.send_message.return_value = {"message_id": 7}
    backend = MagicMock()
    backend.interview_active = False
    return PhoneBot(api, state, backend, focus=focus), api


def say(bot, text, chat=OWNER):
    bot.handle_update({"message": {"chat": {"id": chat, "type": "private"}, "from": {"first_name": "A"}, "text": text, "message_id": 1, "date": time.time()}})


def press(bot, data, chat=OWNER, message_id=9):
    bot.handle_update({"callback_query": {"id": "cb", "data": data, "message": {"chat": {"id": chat}, "message_id": message_id, "text": "x"}}})


def sent_texts(api):
    return [c.args[1] for c in api.send_message.call_args_list]


def sent_buttons(api):
    return [c.kwargs.get("buttons") for c in api.send_message.call_args_list]


def button_data(buttons):
    return [b.get("callback_data") for row in (buttons or []) for b in row]


def test_focus_command_starts_a_session_and_offers_controls(tmp_path):
    svc, *_ = make(tmp_path)
    bot, api = bot_for(tmp_path, svc)
    say(bot, "/focus")
    assert svc._armed.wait(5) and svc.is_focusing
    assert "Focus mode is on for 1 hour" in sent_texts(api)[-1]
    assert "fc:stop" in button_data(sent_buttons(api)[-1])


def test_focus_with_minutes(tmp_path):
    svc, *_ = make(tmp_path)
    bot, api = bot_for(tmp_path, svc)
    say(bot, "/focus 45")
    assert svc._armed.wait(5)
    assert svc.session.round.planned_minutes == 45


def test_typed_stop_ends_it(tmp_path):
    svc, *_ = make(tmp_path)
    bot, api = bot_for(tmp_path, svc)
    say(bot, "/focus")
    assert svc._armed.wait(5)
    say(bot, "/focus stop")
    assert not svc.is_focusing
    assert "Focus mode ended" in sent_texts(api)[-1]


def test_a_stranger_cannot_start_focus(tmp_path):
    svc, *_ = make(tmp_path)
    bot, api = bot_for(tmp_path, svc)
    say(bot, "/focus", chat=STRANGER)
    press(bot, "fc:start:60", chat=STRANGER)
    assert not svc.session.is_active
    assert api.send_message.call_count == 0


def test_the_end_button_asks_before_ending(tmp_path):
    svc, *_ = make(tmp_path)
    bot, api = bot_for(tmp_path, svc)
    say(bot, "/focus")
    assert svc._armed.wait(5)
    press(bot, "fc:stop")
    assert svc.is_focusing  # one tap is not enough
    assert "End focus early" in api.edit_message.call_args.args[2]
    press(bot, "fc:stopy")
    assert not svc.is_focusing


def test_keeping_going_goes_back_to_the_status(tmp_path):
    svc, *_ = make(tmp_path)
    bot, api = bot_for(tmp_path, svc)
    say(bot, "/focus")
    assert svc._armed.wait(5)
    press(bot, "fc:status")
    assert "Focusing" in api.edit_message.call_args.args[2]


def test_focus_screen_from_the_bottom_keyboard(tmp_path):
    svc, *_ = make(tmp_path)
    bot, api = bot_for(tmp_path, svc)
    say(bot, "🎯 Focus")
    assert "Focus mode" in sent_texts(api)[-1]
    assert "fc:start:25" in button_data(sent_buttons(api)[-1])
    assert not svc.session.is_active  # opening the screen doesn't start anything


def test_start_button_from_the_screen(tmp_path):
    svc, *_ = make(tmp_path)
    bot, api = bot_for(tmp_path, svc)
    press(bot, "fc:start:25")
    assert svc._armed.wait(5)
    assert svc.session.round.planned_minutes == 25


def test_break_buttons_after_time_up(tmp_path):
    svc, desktop, chrome, pet, clock, events = make(tmp_path)
    bot, api = bot_for(tmp_path, svc)
    start_and_wait(svc)
    clock.advance(60)
    svc.tick()
    screen = ui.focus_event_screen(events[0].kind, events[0].text, 60)
    data = button_data(screen.buttons)
    assert "fc:more:15" in data and "fc:start:60" in data and "fc:stop" in data
    press(bot, "fc:more:15")
    assert svc.is_focusing  # "15 more minutes" from the break locks again


def test_ban_list_is_managed_from_the_phone(tmp_path):
    svc, *_ = make(tmp_path)
    bot, api = bot_for(tmp_path, svc)
    say(bot, "/focus ban 9anime.to")
    assert "9anime.to" in svc.banned_sites()
    say(bot, "/focus ban obsidian")
    assert "Banned obsidian.exe" in sent_texts(api)[-1]
    say(bot, "/focus banned")
    assert "9anime.to" in sent_texts(api)[-1]
    say(bot, "/focus unban 9anime.to")
    assert "9anime.to" not in svc.banned_sites()


def test_the_banned_button_lists_the_bans(tmp_path):
    svc, *_ = make(tmp_path)
    bot, api = bot_for(tmp_path, svc)
    press(bot, "fc:banned")
    assert "Banned during focus" in sent_texts(api)[-1]


def test_no_focus_service_is_handled_politely(tmp_path):
    bot, api = bot_for(tmp_path, None)
    say(bot, "/focus")
    assert "isn't available" in sent_texts(api)[-1]
    press(bot, "fc:start:60")  # nothing to crash on


def test_unknown_focus_word_gets_help(tmp_path):
    svc, *_ = make(tmp_path)
    bot, api = bot_for(tmp_path, svc)
    say(bot, "/focus banana")
    assert "Try /focus" in sent_texts(api)[-1]
    assert not svc.session.is_active


# ------------------------------------------------------------------ pushes
def notifier(tmp_path, *, hold=False, quiet=False):
    state = PhoneState(tmp_path / "phone.json")
    state.try_pair(state.new_pairing_code(), OWNER, "A")
    api = MagicMock()
    hour = 3 if quiet else 14
    return PhoneNotifier(api, state, quiet_hours="23:00-08:00", hold=lambda: hold, now=lambda: datetime(2026, 1, 1, hour, 0)), api


def test_ordinary_pushes_wait_while_focusing(tmp_path):
    n, api = notifier(tmp_path, hold=True)
    assert not n.send("urgent mail", key="mail:1")
    assert api.send_message.call_count == 0
    assert not n.state.already_notified("mail:1")  # not lost: it goes out after focus


def test_pushes_flow_normally_when_not_focusing(tmp_path):
    n, api = notifier(tmp_path)
    assert n.send("hi", key="k") and api.send_message.call_count == 1


def test_focus_timers_bypass_quiet_hours_and_the_focus_hold(tmp_path):
    n, api = notifier(tmp_path, hold=True, quiet=True)
    assert not n.send("normal")
    assert n.send("Time for a break!", direct=True)
    assert api.send_message.call_count == 1


def test_direct_pushes_are_not_rate_limited_but_still_owner_only(tmp_path):
    n, api = notifier(tmp_path)
    n.max_per_hour = 1
    assert n.send("one") and not n.send("two")
    assert n.send("timer", direct=True)
    n.state.unpair()
    assert not n.send("timer", direct=True)  # nobody is linked: nothing is sent


def test_focus_event_screens():
    up = ui.focus_event_screen("time_up", "Time for a break! You focused.\nZero distractions.", 60)
    assert "<b>Time for a break! You focused.</b>" in up.text and "Zero distractions" in up.text
    over = ui.focus_event_screen("break_over", "Break's over. Ready?", 45)
    assert "fc:start:45" in button_data(over.buttons)
    warn = ui.focus_event_screen("warn", "10 minutes left <b>", 60)
    assert "&lt;b&gt;" in warn.text and warn.buttons is None  # text is escaped


# ------------------------------------------------------------------ "/focus 1 hour" and the end-of-day recap
@pytest.mark.parametrize("text, minutes", [("1 hour", 60), ("30 mins", 30), ("half an hour", 30), ("1h30", 90), ("45", 45)])
def test_focus_understands_natural_durations_from_the_phone(tmp_path, text, minutes):
    svc, *_ = make(tmp_path)
    bot, api = bot_for(tmp_path, svc)
    say(bot, f"/focus {text}")
    assert svc._armed.wait(5)
    assert svc.session.round.planned_minutes == minutes
    assert "Focus mode is on for" in sent_texts(api)[-1]


def test_a_confusing_duration_is_explained_not_started(tmp_path):
    svc, *_ = make(tmp_path)
    bot, api = bot_for(tmp_path, svc)
    say(bot, "/focus 2")
    assert "Did you mean 2 hours" in sent_texts(api)[-1] and not svc.session.is_active
    say(bot, "/focus 10 hours")
    assert "at most 4 hours" in sent_texts(api)[-1] and not svc.session.is_active


def test_the_ai_reads_a_strange_duration_from_the_phone(tmp_path):
    svc, *_ = make(tmp_path, ai_minutes=lambda text, now: 100)
    bot, api = bot_for(tmp_path, svc)
    say(bot, "/focus until 3pm")
    assert svc._armed.wait(5) and svc.session.round.planned_minutes == 100
    assert "I read that as 1h 40m" in sent_texts(api)[-1]


def test_more_time_can_be_asked_for_in_words(tmp_path):
    svc, *_ = make(tmp_path)
    bot, api = bot_for(tmp_path, svc)
    say(bot, "/focus 30")
    assert svc._armed.wait(5)
    say(bot, "/focus more half an hour")
    assert svc.session.round.planned_minutes == 60


def test_today_and_habits_commands_and_buttons(tmp_path):
    svc, *_ = make(tmp_path)
    bot, api = bot_for(tmp_path, svc)
    from datetime import datetime

    from .test_recap_habits import rnd

    day = datetime.fromtimestamp(svc._clock()).date()
    svc.session._data["history"] = [rnd(day, 9, 12, 60.0), rnd(day, 18, 0, 47.0)]
    say(bot, "/focus today")
    assert "Started 09:12, finished 18:47." in sent_texts(api)[-1]
    say(bot, "/focus habits")
    assert "Your study habits" in sent_texts(api)[-1]
    press(bot, "fc:today")
    assert "Focused 1h 47m in 2 rounds." in sent_texts(api)[-1]
    press(bot, "fc:habits")
    assert "Your study habits" in sent_texts(api)[-1]


def test_the_idle_screen_offers_today_stats_habits_and_banned(tmp_path):
    svc, *_ = make(tmp_path)
    bot, api = bot_for(tmp_path, svc)
    say(bot, "🎯 Focus")
    data = button_data(sent_buttons(api)[-1])
    assert {"fc:today", "fc:stats", "fc:habits", "fc:banned", "fc:start:60"} <= set(data)


def test_the_recap_message_is_a_calm_evening_note_without_buttons():
    text = "Focus recap · Monday 28 September\nFocused 3h 15m in 4 rounds.\nStarted 09:12, finished 18:47."
    screen = ui.focus_event_screen("recap", text, 60)
    assert screen.text.startswith("🌙 <b>Focus recap · Monday 28 September</b>")
    assert "Started 09:12, finished 18:47." in screen.text and screen.buttons is None
    assert "&lt;" in ui.focus_event_screen("recap", "Focus recap\n<script>", 60).text  # anything dynamic is escaped


def test_the_phone_service_pushes_the_recap_directly(tmp_path):
    """The recap event reaches Telegram through the notifier as a direct push (quiet hours and the focus hold don't apply)."""
    from app.focus.service import FocusEvent
    from app.phone.service import PhoneService

    svc, *_ = make(tmp_path)
    sent = []

    class Notifier:
        def send(self, text, *, key=None, buttons=None, direct=False):
            sent.append((text, direct))
            return True

    fake = PhoneService.__new__(PhoneService)
    fake.focus, fake.notifier = svc, Notifier()
    fake._focus_event(FocusEvent("recap", "Focus recap · Monday\nFocused 1 hour in 1 round.", summary={}))
    assert sent and sent[0][1] is True and "Focused 1 hour" in sent[0][0]
