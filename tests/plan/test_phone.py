import time
from unittest.mock import MagicMock

from app.phone import ui
from app.phone.bot import PhoneBot
from app.phone.notifier import PhoneNotifier
from app.phone.state import PhoneState

from .fakes import make

OWNER = 111


def bot_for(tmp_path, plan, focus):
    state = PhoneState(tmp_path / "phone.json")
    state.try_pair(state.new_pairing_code(), OWNER, "Ahmet")
    api = MagicMock()
    api.send_message.return_value = {"message_id": 7}
    backend = MagicMock()
    backend.interview_active = False
    return PhoneBot(api, state, backend, focus=focus, plan=plan), api, state


def say(bot, text):
    bot.handle_update({"message": {"chat": {"id": OWNER, "type": "private"}, "from": {"first_name": "A"}, "text": text, "message_id": 1, "date": time.time()}})


def press(bot, data, message_id=9):
    bot.handle_update({"callback_query": {"id": "cb", "data": data, "message": {"chat": {"id": OWNER}, "message_id": message_id, "text": "x"}}})


def last(api):
    call = api.send_message.call_args_list[-1]
    return call.args[1], [b.get("callback_data") for row in (call.kwargs.get("buttons") or []) for b in row]


def test_plan_command_shows_a_draft_with_buttons(tmp_path):
    plan, *_, focus, _ = make(tmp_path)
    bot, api, _ = bot_for(tmp_path, plan, focus)
    say(bot, "/plan study 8 hours, 5 linear algebra 3 calculus, 50 min lunch, gym, home by 8 for dinner")
    text, buttons = last(api)
    assert text.startswith("📋 Here's a plan for Thursday 1 October")
    assert buttons == ["pl:ok", "pl:edit", "pl:drop"]


def test_looks_good_locks_it_in(tmp_path):
    plan, _, calendar, focus, _ = make(tmp_path)
    bot, api, _ = bot_for(tmp_path, plan, focus)
    say(bot, "/plan my day")
    press(bot, "pl:ok")
    text, buttons = last(api)
    assert text.startswith("✅ Plan locked in.") and calendar.created
    assert buttons == ["pl:show", "pl:edit", "pl:cancel"]


def test_change_takes_the_next_message_as_a_correction(tmp_path):
    plan, ai, _, focus, _ = make(tmp_path)
    bot, api, _ = bot_for(tmp_path, plan, focus)
    say(bot, "/plan my day")
    press(bot, "pl:edit")
    assert "What should change?" in api.send_message.call_args_list[-1].args[1]
    say(bot, "gym in the morning")
    assert ai.calls[-1][0] == "gym in the morning" and '"Linear algebra"' in ai.calls[-1][1]
    assert last(api)[0].startswith("📋 Here's a plan")


def test_cancel_asks_first_when_tapped(tmp_path):
    plan, _, calendar, focus, _ = make(tmp_path)
    bot, api, _ = bot_for(tmp_path, plan, focus)
    say(bot, "/plan my day")
    press(bot, "pl:ok")
    press(bot, "pl:cancel")
    assert calendar.deleted == []  # just the question so far
    press(bot, "pl:cancely")
    assert last(api)[0].startswith("Plan cancelled.") and calendar.deleted


def test_a_nudge_reaches_the_phone_and_its_button_starts_focus(tmp_path):
    from app.phone.service import PhoneService

    plan, _, _, focus, clock = make(tmp_path)
    bot, api, state = bot_for(tmp_path, plan, focus)
    service = PhoneService.__new__(PhoneService)  # just the nudge handler, without a real bot or Telegram
    service.focus, service.notifier = focus, PhoneNotifier(api, state)
    plan.add_listener(service._plan_nudge)

    say(bot, "/plan my day")
    press(bot, "pl:ok")
    clock.set(9, 20)
    nudges = plan.tick()
    text, buttons = last(api)
    assert text.startswith("📚 Linear algebra now") and buttons == ["pl:fc:3"]
    assert state.already_notified(nudges[0].key)  # never sent twice, even after a restart

    press(bot, "pl:fc:3")
    assert focus.started == [(60, "Linear algebra")]
    press(bot, "pl:fc:99")
    assert len(focus.started) == 1


def test_without_a_planner_the_bot_says_so(tmp_path):
    bot, api, _ = bot_for(tmp_path, None, None)
    say(bot, "/plan anything")
    assert last(api)[0] == "Day planning isn't available."


def test_buttons_fit_the_moment():
    assert ui.plan_buttons("") is None
    assert ui.plan_nudge_buttons(None, 60) is None
    assert ui.plan_nudge_buttons(4, 50) == [[{"text": "▶️ Focus 50 min", "callback_data": "pl:fc:4"}]]
