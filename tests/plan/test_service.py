from datetime import datetime

from app.plan.schedule import STUDY, TRAVEL, Block
from app.plan.service import MARKER, calendar_blocks, plan_text
from app.plan.store import PlanStore

from .fakes import FakeCalendar, make


def test_remember_teaches_places_and_shows_them(tmp_path):
    service, *_ = make(tmp_path)
    text = service.places_text()
    assert "gym ↔ home: 50 min" in text and "gym ↔ school: 15 min" in text and "home ↔ school: 50 min" in text
    assert "gym: 1h 15m" in text and "You study at: school" in text
    assert PlanStore(tmp_path / "plan.json").book().study_place == "school"  # it's on disk


def test_only_new_facts_are_mentioned(tmp_path):
    from .fakes import OWNER_FACTS

    service, ai, *_ = make(tmp_path)
    ai.reply = OWNER_FACTS  # the model repeating everything Miki already knows
    assert not service.remember("same again").ok
    ai.reply = {"items": [], "learn": {"durations": {"gym": 90}, "travel": [{"from": "gym", "to": "school", "minutes": 15}]}}
    assert service.remember("I spend 1h30 at the gym now").text == "🧠 Noted: gym 1h 30m."


def test_planning_uses_its_own_model_on_the_same_connection(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from app.plan.service import PlanService

    connection = object()
    core = SimpleNamespace(brain=SimpleNamespace(model="gpt-4.1-nano", client=connection, generate_response=None), get_tool=lambda name: None)
    service = PlanService(lock_name=None)
    monkeypatch.delenv("MIKI_PLAN_MODEL", raising=False)
    service.attach(core)
    assert service._ai.brain.model == "gpt-4.1-mini" and service._ai.brain.client is connection
    monkeypatch.setenv("MIKI_PLAN_MODEL", "gpt-4.1-nano")
    service.attach(core)
    assert service._ai.brain is core.brain  # same model as chat: no second client


def test_the_owner_day_from_7_is_a_draft_that_fits(tmp_path):
    service, ai, *_ = make(tmp_path)
    reply = service.make("I want to study 8 hours, 5h linear algebra, 3h calculus, 50 min lunch, the gym, home at 8pm for dinner")
    assert reply.ok and reply.status == "draft"
    assert "07:00  🚶 To gym (50 min)" in reply.text
    assert "📚 Linear algebra" in reply.text and "🍽 Lunch" in reply.text and "🏋️ Gym, until 09:05" in reply.text
    assert "8h study · 1h 55m travel · home at 19:30" in reply.text
    assert "doesn't fit" not in reply.text
    assert "gym 75" in ai.calls[-1][1]  # the AI is told what Miki knows


def test_the_owner_day_from_0830_says_it_doesnt_fit_and_how_to_fix_it(tmp_path):
    service, *_ = make(tmp_path, at=datetime(2026, 10, 1, 8, 30))
    text = service.make("same day, but now it's 8:30").text
    assert "⚠️ This doesn't fit: 1h late for dinner at 20:00." in text
    assert "• Study 7h instead of 8h: home by" in text and "• Skip gym: home by 19:30" in text


def test_unknown_lengths_are_guessed_out_loud(tmp_path):
    service, *_ = make(tmp_path, {"items": [{"kind": "activity", "title": "Swim", "place": "pool"}]}, taught=False)
    text = service.make("go swimming").text
    assert "I guessed 1h for swim" in text and "I don't know how far home is from pool" in text


def test_ok_makes_it_active_and_puts_it_in_the_calendar(tmp_path):
    service, _, calendar, *_ = make(tmp_path)
    service.make("the owner's day")
    reply = service.confirm()
    assert reply.ok and reply.status == "active" and "Added" in reply.text
    titles = [e["title"] for e in calendar.created]
    assert titles[:3] == ["Travel to gym", "Gym", "Travel to school"]
    assert "Study: Linear algebra" in titles and "Lunch" in titles and "Study: Calculus" in titles
    assert "Dinner" not in titles  # a moment, not a block
    assert all(e["description"] == MARKER for e in calendar.created)
    assert all(e["start"].endswith(datetime(2026, 10, 1, 12).astimezone().isoformat()[-6:]) for e in calendar.created)
    active = PlanStore(tmp_path / "plan.json").get("active")
    assert active["calendar_ids"] == [e["id"] for e in calendar.created]
    assert PlanStore(tmp_path / "plan.json").get("draft") is None


def test_replacing_the_plan_swaps_its_calendar_events(tmp_path):
    service, ai, calendar, *_ = make(tmp_path)
    service.make("the owner's day")
    service.confirm()
    first = [e["id"] for e in calendar.created]
    ai.reply = {"items": [{"kind": "study", "title": "Calculus", "minutes": 120}]}
    draft = service.make("only 2 hours of calculus")
    assert "doesn't fit" not in draft.text and "Gym" not in draft.text  # its own old blocks don't count as busy
    assert '"Linear algebra"' in ai.calls[-1][1]  # the AI saw the plan it's changing
    service.confirm()
    assert calendar.deleted == first


def test_planning_tomorrow_leaves_todays_calendar_blocks_alone(tmp_path):
    service, ai, calendar, _, clock = make(tmp_path)
    service.make("the owner's day")
    service.confirm()
    clock.set(21, 0)
    ai.reply = {"day": "tomorrow", "items": [{"kind": "study", "title": "Calculus", "minutes": 120}]}
    service.make("tomorrow 2 hours of calculus")
    service.confirm()
    assert calendar.deleted == []


def test_the_calendar_is_planned_around(tmp_path):
    lecture = {"id": "x", "title": "Lecture", "start": "2026-10-01T10:00:00", "end": "2026-10-01T12:00:00", "all_day": False}
    birthday = {"id": "y", "title": "Birthday", "start": "2026-10-01", "end": "2026-10-02", "all_day": True}
    service, *_ = make(tmp_path, {"items": [{"kind": "study", "title": "Calculus", "minutes": 180}], "study_place": "home"},
                       calendar=FakeCalendar([lecture, birthday]))
    text = service.make("3 hours of calculus at home").text
    assert "10:00  📅 Lecture, until 12:00" in text and "Birthday" not in text


def test_no_calendar_still_plans(tmp_path):
    service, *_ = make(tmp_path, calendar=FakeCalendar(available=False))
    service.make("the owner's day")
    reply = service.confirm()
    assert reply.ok and "lives in Miki only" in reply.text


def test_cancel_clears_everything_and_its_calendar_events(tmp_path):
    service, _, calendar, *_ = make(tmp_path)
    service.make("the owner's day")
    service.confirm()
    reply = service.cancel()
    assert reply.ok and "took" in reply.text and set(calendar.deleted) == {e["id"] for e in calendar.created}
    assert not service.show().ok and not service.cancel().ok


def test_discarding_a_draft_keeps_the_active_plan(tmp_path):
    service, *_ = make(tmp_path)
    service.make("the owner's day")
    service.confirm()
    service.make("something else")
    assert service.discard_draft().ok
    assert service.show().status == "active"


def test_nudges_go_out_once_at_the_right_moments(tmp_path):
    service, _, _, focus, clock = make(tmp_path)
    sent = []
    service.add_listener(sent.append)
    service.make("the owner's day")
    service.confirm()

    clock.set(6, 50)
    assert service.tick() == []
    clock.set(6, 55)  # 5 minutes before leaving
    nudges = service.tick()
    assert len(nudges) == 1 and nudges[0].text.startswith("🚶 Time to leave for gym at 07:00")
    assert service.tick() == []  # only once

    clock.set(9, 20)  # arrived at school: linear algebra starts
    study = service.tick()
    assert len(study) == 1 and study[0].text.startswith("📚 Linear algebra now") and study[0].focus_minutes == 60
    assert service.focus_for(study[0].focus_block) == (60, "Linear algebra")
    assert sent == nudges + study

    clock.set(10, 25)  # the next round of the same subject: focus mode calls those, the plan stays quiet
    assert service.tick() == []


def test_missed_nudges_are_skipped_not_sent_late(tmp_path):
    service, _, _, _, clock = make(tmp_path)
    service.make("the owner's day")
    service.confirm()
    clock.set(13, 0)  # Miki was off all morning
    assert all("Time to leave for gym" not in n.text for n in service.tick())


def test_no_study_nudge_while_already_focusing(tmp_path):
    service, _, _, focus, clock = make(tmp_path)
    service.make("the owner's day")
    service.confirm()
    clock.set(7, 0)
    service.tick()
    focus.is_focusing = True
    clock.set(9, 20)
    assert service.tick() == []


def test_typed_commands(tmp_path):
    service, *_ = make(tmp_path)
    assert "Tell me how you want your day to go" in service.command("")
    assert "/plan ok to lock it in" in service.command("the owner's day")
    assert service.command("ok").startswith("✅ Plan locked in.")
    assert "📋 Your day" in service.command("show")
    assert "What I know about your places" in service.command("places")
    assert service.command("cancel").startswith("Plan cancelled.")


def test_tomorrow_starts_at_8_and_says_so(tmp_path):
    reply = {**{"items": [{"kind": "study", "title": "Calculus", "minutes": 60}]}, "day": "tomorrow"}
    service, *_ = make(tmp_path, reply, at=datetime(2026, 10, 1, 22, 0))
    text = service.make("tomorrow, 1 hour of calculus").text
    assert "Friday 2 October" in text and "I started the day at 08:00" in text


def test_ai_trouble_is_a_friendly_message(tmp_path):
    service, ai, *_ = make(tmp_path)

    def broken(text):
        raise RuntimeError("timeout")

    ai.reply = broken
    assert not service.make("plan my day").ok
    ai.reply = None
    assert "couldn't make a plan" in service.make("plan my day").text


def test_text_merges_rounds_and_marks_now():
    blocks = [Block(TRAVEL, "To school", 480, 530, "school"), Block(STUDY, "Calculus", 530, 590), Block("break", "Break", 590, 595),
              Block(STUDY, "Calculus", 595, 655)]
    plan = {"day": "2026-10-01", "status": "active", "blocks": [b.to_dict() for b in blocks], "study_minutes": 120, "travel": 50}
    text = plan_text(plan, now_minute=600)
    assert "▶ 08:50  📚 Calculus, until 10:55 (2 rounds)" in text
    assert [b.title for b in calendar_blocks(blocks)] == ["To school", "Calculus"]
