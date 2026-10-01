import time
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.phone import ui
from app.phone.bot import PhoneBot
from app.phone.state import PhoneState
from app.secretary import radar
from app.secretary.service import SecretaryService
from tests.plan.fakes import Clock, FakeCalendar, make as make_plan

DAY = date(2026, 10, 1)  # a Thursday
PREFS = {"secretary": True, "morning_brief": True, "brief_time": "07:30", "evening_review": True, "evening_time": "21:00",
         "weekly_review": True, "catchup": True, "leave_alerts": True}


def ev(title, start, end, *, day=0, id=None, description="", location=""):
    d = DAY + timedelta(days=day)
    return {"id": id or f"{title}-{day}-{start}", "title": title, "start": f"{d}T{start}:00", "end": f"{d}T{end}:00", "all_day": False,
            "description": description, "location": location}


def all_day(title, day):
    d = DAY + timedelta(days=day)
    return {"id": f"{title}-{day}", "title": title, "start": str(d), "end": str(d + timedelta(days=1)), "all_day": True}


class FakeSession:
    def __init__(self):
        self.rounds = []
        self.today = DAY
        self.streak_days = 0

    def add(self, minutes, goal="", day=DAY):
        self.rounds.append({"day": day.isoformat(), "minutes": minutes, "goal": goal, "start": 0})

    def rounds_on(self, day):
        return [r for r in self.rounds if r["day"] == day.isoformat()]

    def history(self):
        return list(self.rounds)

    def stats(self):
        return {"today_minutes": sum(r["minutes"] for r in self.rounds if r["day"] == self.today.isoformat()),
                "week_minutes": sum(r["minutes"] for r in self.rounds)}

    def streak(self, day):
        return self.streak_days


class Rig:
    """A secretary on fakes: a calendar you fill, a focus log you add to, a real plan service, and every notice it sends."""

    def __init__(self, tmp_path, *, events=None, at=datetime(2026, 10, 1, 7, 30), prefs=None, deliver=True, plan_reply=None):
        self.events = list(events or [])
        self.plan, self.ai, self.plan_calendar, self.focus_fake, self.clock = make_plan(tmp_path / "plan", plan_reply or {"items": []}, at=at)
        self.session = FakeSession()
        self.focus = SimpleNamespace(session=self.session, is_focusing=False, config=self.focus_fake.config)
        self.sent: list = []
        self.deliver = deliver
        self.mail = []
        self.prefs = {**PREFS, **(prefs or {})}
        self.service = SecretaryService(
            tmp_path / "secretary.json", calendar=lambda start, days: list(self.events), weather=lambda: "14°C, cloudy",
            urgent_mail=lambda: self.mail, focus=self.focus, plan=self.plan, name=lambda: "Ahmet", pref=self.prefs.get, clock=self.clock)
        self.service.add_listener(lambda notice: self.sent.append(notice) or self.deliver)

    def at(self, hour, minute=0, day=0):
        moment = datetime.combine(DAY + timedelta(days=day), datetime.min.time()).replace(hour=hour, minute=minute)
        self.clock.now = moment.timestamp()
        self.service._cache = None
        return moment

    def tick(self, hour, minute=0, day=0):
        return self.service.tick(self.at(hour, minute, day))

    def lock_in_plan(self, text="study 3 hours"):
        assert self.plan.make(text, fresh=True).ok
        return self.plan.confirm()


STUDY3 = {"items": [{"kind": "study", "title": "Linear algebra", "minutes": 180}], "study_place": "home"}


# ------------------------------------------------------------------------------------------------ reading the calendar
def test_events_leave_out_miki_own_blocks_and_skipped_lectures():
    raw = [ev("Study: Calculus", "09:00", "10:00", description="Planned by Miki (/plan)."), ev("Lecture", "10:00", "12:00"),
           ev("Skipped: Lecture", "10:00", "12:00"), ev("Dentist", "14:00", "15:00")]
    kept = radar.parse_events(raw, skipped=[{"title": "Lecture", "start": 600, "end": 720}])
    assert [e.title for e in kept] == ["Dentist"]


def test_deadlines_are_sorted_with_days_left():
    events = radar.parse_events([ev("Calculus exam", "09:00", "11:00", day=3), all_day("Thesis deadline", 1), ev("Gym", "18:00", "19:00", day=1),
                                 ev("Old exam", "09:00", "10:00", day=-2)])
    found = [(e.title, d) for e, d in radar.deadlines(events, DAY)]
    assert found == [("Thesis deadline", 1), ("Calculus exam", 3)]


def test_the_leave_time_needs_a_known_trip():
    lecture = radar.parse_events([ev("Linear Algebra Lecture", "10:00", "12:00")])[0]
    routes = {("home", "school"): 50}
    assert radar.leave_time(lecture, routes, "school") == (datetime(2026, 10, 1, 9, 0), 50)  # 50 min trip + 10 spare
    assert radar.leave_time(lecture, {}, "school") is None and radar.leave_time(lecture, routes, "home") is None
    assert radar.leave_time(radar.parse_events([ev("Dentist", "10:00", "11:00")])[0], routes, "school") is None


# ------------------------------------------------------------------------------------------------ the morning briefing
def test_the_morning_brief_has_the_day_what_is_coming_and_what_to_do(tmp_path):
    rig = Rig(tmp_path, events=[ev("Linear Algebra Lecture", "10:00", "12:00"), ev("Calculus exam", "09:00", "11:00", day=2)])
    rig.service.plan.store.save_book(rig.plan.store.book())
    assert rig.tick(7, 30) == 1
    notice = rig.sent[0]
    assert notice.text.startswith("☀️ Good morning, Ahmet") and "14°C, cloudy" in notice.text
    assert "10:00–12:00" in notice.text and "leave by 09:00 (50 min)" in notice.text
    assert "📌 Coming up" in notice.text and "Calculus exam: in 2 days" in notice.text and "⚠️" in notice.text
    assert "No plan for today yet" in notice.text
    assert [a for _, a in notice.actions] == ["plan"]  # no "same as usual" before there is a usual


def test_the_brief_comes_once_a_day_inside_its_window(tmp_path):
    rig = Rig(tmp_path)
    assert rig.tick(7, 0) == 0  # not yet
    assert rig.tick(7, 31) == 1 and rig.tick(7, 40) == 0 and rig.tick(8, 0) == 0
    assert rig.tick(11, 0, day=1) == 0  # tomorrow's, but past its window
    assert rig.tick(7, 30, day=1) == 1


def test_a_brief_that_could_not_be_delivered_is_tried_again_later(tmp_path):
    rig = Rig(tmp_path, deliver=False)
    assert rig.tick(7, 30) == 0 and len(rig.sent) == 1
    rig.tick(7, 32)
    assert len(rig.sent) == 1  # not hammered every minute
    rig.deliver = True
    assert rig.tick(7, 40) == 1 and len(rig.sent) == 2


def test_urgent_mail_and_the_weather_are_part_of_it(tmp_path):
    rig = Rig(tmp_path)
    rig.mail = [SimpleNamespace(sender_name="Professor", subject="Your resit", priority="urgent")]
    rig.tick(7, 30)
    assert "🔴 1 urgent email" in rig.sent[0].text and "Professor: Your resit" in rig.sent[0].text


def test_with_a_plan_it_shows_the_plan_instead_of_offering_one(tmp_path):
    rig = Rig(tmp_path, plan_reply=STUDY3)
    rig.lock_in_plan()
    rig.tick(7, 30)
    text = rig.sent[0].text
    assert "You already have a plan for today: 3h of study" in text and rig.sent[0].actions == []


def test_after_a_locked_in_day_same_as_usual_is_offered(tmp_path):
    rig = Rig(tmp_path, plan_reply=STUDY3)
    rig.lock_in_plan()
    rig.tick(8, 0)  # remembers the usual day
    rig.tick(7, 30, day=1)
    assert [a for _, a in rig.sent[-1].actions] == ["plan", "again"]


def test_it_never_invents_a_calendar(tmp_path):
    rig = Rig(tmp_path)
    rig.service._calendar = lambda start, days: None
    rig.tick(7, 30)
    assert "I couldn't read your calendar" in rig.sent[0].text


# ------------------------------------------------------------------------------------------------ the evening check-in
def test_the_evening_compares_plan_and_focus_and_carries_what_is_left(tmp_path):
    reply = {"items": [{"kind": "study", "title": "Linear algebra", "minutes": 180}, {"kind": "study", "title": "Calculus", "minutes": 120}],
             "study_place": "home"}
    rig = Rig(tmp_path, plan_reply=reply, events=[ev("Lecture", "10:00", "12:00", day=1)])
    rig.lock_in_plan()
    rig.session.add(120, "Linear algebra")
    rig.session.add(30, "Calculus")
    rig.tick(21, 0)
    text = rig.sent[-1].text
    assert text.startswith("🌙 Evening check-in") and "2h 30m focused of 5h planned" in text
    assert "Linear algebra: 2h of 3h → 1h left" in text and "Calculus: 30 min of 2h → 1h 30m left" in text
    assert "🗓 Tomorrow (Friday)" in text and "10:00" in text
    assert [a for _, a in rig.sent[-1].actions] == ["tomorrow", "dismiss"]
    assert rig.service._carry(DAY) == {"Linear algebra": 60, "Calculus": 90}


def test_days_left_are_counted_from_today_not_from_tomorrow(tmp_path):
    rig = Rig(tmp_path, plan_reply=STUDY3, events=[ev("Calculus exam", "09:00", "11:00", day=2)])
    rig.lock_in_plan()
    rig.tick(21, 0)
    assert "Calculus exam: in 2 days (Sat 3 Oct)" in rig.sent[-1].text


def test_a_day_that_went_to_plan_gets_praise(tmp_path):
    rig = Rig(tmp_path, plan_reply=STUDY3)
    rig.lock_in_plan()
    rig.session.add(180, "Linear algebra")
    rig.tick(21, 0)
    assert "You hit your plan" in rig.sent[-1].text and "Linear algebra: 3h of 3h ✓" in rig.sent[-1].text


def test_no_evening_message_when_nothing_happened_and_nothing_is_coming(tmp_path):
    rig = Rig(tmp_path)
    assert rig.tick(21, 0) == 0 and rig.sent == []


def test_the_evening_waits_while_you_are_in_a_focus_round(tmp_path):
    rig = Rig(tmp_path, plan_reply=STUDY3)
    rig.lock_in_plan()
    rig.focus.is_focusing = True
    assert rig.tick(21, 0) == 0
    rig.focus.is_focusing = False
    assert rig.tick(21, 10) == 1


# ------------------------------------------------------------------------------------------------ the buttons
def test_plan_tomorrow_adds_the_leftovers_to_your_usual_day(tmp_path):
    rig = Rig(tmp_path, plan_reply=STUDY3)
    rig.lock_in_plan()
    rig.session.add(120, "Linear algebra")
    rig.tick(21, 0)
    rig.at(21, 5)
    result = rig.service.action("tomorrow")
    assert result.reply.ok and result.reply.status == "draft"
    draft = rig.plan.store.get("draft")
    assert draft["day"] == "2026-10-02" and draft["study_minutes"] == 240  # 3h usual + the 1h left over


def test_same_as_usual_repeats_the_locked_in_day_without_skipped_lectures(tmp_path):
    rig = Rig(tmp_path, plan_reply={**STUDY3, "skip": ["lecture"]},
              events=[ev("Linear Algebra Lecture", "10:00", "12:00")])
    rig.lock_in_plan("study 3 hours, skip the lecture")
    rig.tick(8, 0)
    rig.at(7, 30, day=1)
    result = rig.service.action("again")
    assert result.reply.ok
    assert rig.plan.store.get("draft")["request"]["skip"] == []


def test_same_as_usual_without_a_usual_day_says_how_to_get_one(tmp_path):
    result = Rig(tmp_path).service.action("again")
    assert "lock in a plan once" in result.text


def test_plan_my_day_asks_what_you_want(tmp_path):
    rig = Rig(tmp_path)
    assert rig.service.action("plan").ask == "plannew"
    assert rig.service.action("dismiss").text == "👍 Okay."


def test_replan_the_rest_drops_what_is_done_and_what_is_behind_you(tmp_path):
    reply = {"items": [{"kind": "study", "title": "Study", "minutes": 240}, {"kind": "activity", "title": "Gym", "place": "gym"}],
             "study_place": "school"}
    rig = Rig(tmp_path, plan_reply=reply, at=datetime(2026, 10, 1, 7, 0))
    rig.lock_in_plan("study 4 hours and gym")
    plan = rig.plan.store.get("active")
    gym = next(b for b in plan["blocks"] if b["title"] == "Gym")
    rig.session.add(90, "Study")
    rig.at(gym["end"] // 60 + 1, gym["end"] % 60)
    request = rig.service._replan_request(rig.service._now())
    assert [i["title"] for i in request["items"]] == ["Study"] and request["items"][0]["minutes"] == 150  # 4h - 1h30 done; gym is behind you
    result = rig.service.action("replan")
    assert result.reply.ok and rig.plan.store.get("draft")["study_minutes"] == 150


# ------------------------------------------------------------------------------------------------ catching up
def behind_rig(tmp_path):
    rig = Rig(tmp_path, plan_reply={"items": [{"kind": "study", "title": "Study", "minutes": 240}], "study_place": "home"},
              at=datetime(2026, 10, 1, 8, 0))
    rig.lock_in_plan("study 4 hours")
    rig.session.add(10, "Study", day=DAY - timedelta(days=1))  # you do use focus rounds
    return rig


def test_it_says_when_you_are_behind_and_offers_to_re_plan(tmp_path):
    rig = behind_rig(tmp_path)
    assert rig.tick(13, 0) == 1
    notice = rig.sent[-1]
    assert notice.text.startswith("⏱ You're about") and "behind your plan" in notice.text
    assert [a for _, a in notice.actions] == ["replan", "dismiss"]


def test_catch_up_is_rare_quiet_and_only_if_you_use_focus(tmp_path):
    rig = behind_rig(tmp_path)
    rig.tick(13, 0)
    assert rig.tick(13, 30) == 0  # spaced out
    assert rig.tick(15, 5) == 1 and rig.tick(17, 30) == 0  # twice a day at most
    quiet = Rig(tmp_path / "x", plan_reply=STUDY3, at=datetime(2026, 10, 1, 8, 0))
    quiet.lock_in_plan()
    assert quiet.tick(15, 0) == 0  # never used focus rounds: nothing to compare, so no nagging


def test_catch_up_never_interrupts_a_round_or_when_you_are_on_track(tmp_path):
    rig = behind_rig(tmp_path)
    rig.focus.is_focusing = True
    assert rig.tick(13, 0) == 0
    rig.focus.is_focusing = False
    rig.session.add(240, "Study")
    assert rig.tick(13, 5) == 0


# ------------------------------------------------------------------------------------------------ leave-now and heads-ups
def test_it_tells_you_when_to_leave_for_a_class(tmp_path):
    rig = Rig(tmp_path, events=[ev("Linear Algebra Lecture", "10:00", "12:00", id="L1")], prefs={"morning_brief": False})
    rig.plan.store.save_book(_book(rig.plan, study_place="school"))
    assert rig.tick(8, 40) == 0  # too early (leave by 09:00, told at 08:55)
    assert rig.tick(8, 56) == 1
    assert "Time to head out for Linear Algebra Lecture at 10:00" in rig.sent[-1].text and "leave by 09:00" in rig.sent[-1].text
    assert rig.sent[-1].respect_quiet and rig.sent[-1].expires is not None
    assert rig.tick(8, 58) == 0  # once


def test_the_plans_own_leave_nudge_is_not_repeated(tmp_path):
    reply = {"items": [{"kind": "study", "title": "Study", "minutes": 60}], "study_place": "school"}
    rig = Rig(tmp_path, plan_reply=reply, events=[ev("Lecture", "10:00", "12:00")], at=datetime(2026, 10, 1, 7, 0))
    rig.plan.store.save_book(_book(rig.plan, study_place="school"))
    rig.lock_in_plan("study 1 hour at school")
    rig.service._cache = None
    assert not any(n.key.startswith("leave:") for n in _alerts(rig, 8, 56))


def _alerts(rig, hour, minute):
    rig.tick(hour, minute)
    return rig.sent


def test_other_events_get_a_heads_up_15_minutes_before(tmp_path):
    rig = Rig(tmp_path, events=[ev("Dentist", "14:00", "15:00", location="Main St 5")], prefs={"morning_brief": False})
    assert rig.tick(13, 30) == 0
    assert rig.tick(13, 46) == 1
    assert rig.sent[-1].text == "⏰ In 14 min: Dentist at Main St 5 (14:00)."


def test_a_late_alert_is_dropped_and_the_day_is_capped(tmp_path):
    rig = Rig(tmp_path, events=[ev(f"Meeting {i}", f"{10 + i}:00", f"{10 + i}:30") for i in range(8)], prefs={"morning_brief": False})
    for hour in range(9, 18):
        rig.tick(hour, 50)
    assert len([n for n in rig.sent if n.key.startswith("soon:")]) == 6  # at most six heads-ups a day
    late = Rig(tmp_path / "late", events=[ev("Standup", "09:00", "09:15")], prefs={"morning_brief": False})
    assert late.tick(9, 5) == 0  # it already started


def test_quiet_hours_hold_back_a_heads_up_but_not_the_brief(tmp_path):
    rig = Rig(tmp_path, events=[ev("Early run", "07:40", "08:30")], deliver=True)
    seen = []
    rig.service._listeners.clear()
    rig.service.add_listener(lambda notice: seen.append(notice) or not notice.respect_quiet)  # what the phone does in quiet hours
    rig.tick(7, 30)
    assert [n.key.split(":")[0] for n in seen] == ["brief", "soon"]
    assert "brief" in rig.service._section("sent") or any(k.startswith("brief") for k in rig.service._section("sent"))
    assert not any(k.startswith("soon") for k in rig.service._section("sent"))  # not delivered: tried again, not lost


# ------------------------------------------------------------------------------------------------ the week
def test_the_sunday_review_has_the_week_in_numbers(tmp_path):
    rig = Rig(tmp_path, events=[ev("Calculus exam", "09:00", "11:00", day=3)])
    sunday = DAY + timedelta(days=3)
    for back, minutes in ((0, 100), (1, 120), (2, 60), (8, 100)):
        rig.session.add(minutes, "Calculus" if back else "Linear algebra", day=sunday - timedelta(days=back))
    rig.session.streak_days = 3
    rig.clock.now = datetime.combine(sunday, datetime.min.time()).replace(hour=18, minute=5).timestamp()
    rig.service._cache = None
    assert rig.service.tick() >= 1
    text = next(n.text for n in rig.sent if n.key.startswith("weekly:"))
    assert text.startswith("📊 Your week") and "Total 4h 40m" in text and "+" in text and "🔥 3-day focus streak" in text
    assert "Most time on: Calculus" in text


def test_the_master_switch_silences_everything(tmp_path):
    rig = Rig(tmp_path, prefs={"secretary": False}, events=[ev("Dentist", "07:40", "08:30")])
    assert rig.tick(7, 30) == 0 and rig.sent == []


def test_state_survives_a_restart(tmp_path):
    rig = Rig(tmp_path)
    rig.tick(7, 30)
    again = SecretaryService(tmp_path / "secretary.json", calendar=lambda s, d: [], pref=PREFS.get, clock=rig.clock)
    again.add_listener(lambda notice: rig.sent.append(notice) or True)
    again.tick(rig.at(7, 45))
    assert len(rig.sent) == 1  # today's brief isn't sent a second time


# ------------------------------------------------------------------------------------------------ the phone
OWNER = 111


def _book(plan, **changes):
    book = plan.store.book()
    for name, value in changes.items():
        setattr(book, name, value)
    return book


def bot_with(tmp_path, rig):
    state = PhoneState(tmp_path / "phone.json")
    state.try_pair(state.new_pairing_code(), OWNER, "Ahmet")
    api = MagicMock()
    api.send_message.return_value = {"message_id": 7}
    backend = MagicMock()
    backend.interview_active = False
    return PhoneBot(api, state, backend, plan=rig.plan, focus=rig.focus_fake, secretary=rig.service), api


def press(bot, data):
    bot.handle_update({"callback_query": {"id": "cb", "data": data, "message": {"chat": {"id": OWNER}, "message_id": 9, "text": "x"}}})


def say(bot, text):
    bot.handle_update({"message": {"chat": {"id": OWNER, "type": "private"}, "from": {"first_name": "A"}, "text": text, "message_id": 1, "date": time.time()}})


def test_the_buttons_under_a_secretary_message_lead_to_a_plan(tmp_path):
    rig = Rig(tmp_path, plan_reply=STUDY3)
    rig.lock_in_plan()
    rig.tick(8, 0)
    rig.at(7, 30, day=1)
    bot, api = bot_with(tmp_path, rig)
    press(bot, "sec:again")
    call = api.send_message.call_args_list[-1]
    assert call.args[1].startswith("📋 Here's a plan for Friday") and "pl:ok" in [b.get("callback_data") for row in call.kwargs["buttons"] for b in row]


def test_plan_my_day_asks_then_plans_from_scratch(tmp_path):
    rig = Rig(tmp_path, plan_reply=STUDY3)
    bot, api = bot_with(tmp_path, rig)
    press(bot, "sec:plan")
    assert "How should today go?" in api.send_message.call_args_list[-1].args[1]
    say(bot, "study 3 hours")
    assert rig.ai.calls[-1][0] == "study 3 hours"
    assert api.send_message.call_args_list[-1].args[1].startswith("📋 Here's a plan")


def test_secretary_buttons_and_settings():
    buttons = ui.secretary_buttons([("📋 Plan my day", "plan"), ("🔁 Same as usual", "again"), ("👍 Not now", "dismiss")])
    assert [[b["callback_data"] for b in row] for row in buttons] == [["sec:plan", "sec:again"], ["sec:dismiss"]]
    assert ui.secretary_buttons([]) is None
    settings = ui.settings_screen({"urgent_push": True, "quiet_hours": "off", "morning_brief": True, "brief_time": "07:30", "voice_replies": True,
                                   "secretary": True, "evening_review": False, "evening_time": "21:00"}, "gpt")
    assert "Secretary: <b>on</b>" in settings.text.replace("On", "on") or "Secretary" in settings.text
