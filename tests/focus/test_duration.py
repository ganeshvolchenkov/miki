from datetime import datetime

import pytest

from app.focus import duration
from app.focus.duration import make_ai_minutes, parse_offline, resolve

NOW = datetime(2026, 9, 28, 13, 20)


@pytest.mark.parametrize("text, minutes", [
    ("45", 45), ("60", 60), ("  25  ", 25),
    ("1 hour", 60), ("1 hr", 60), ("2 hours", 120), ("2h", 120), ("1h", 60),
    ("30 mins", 30), ("30 min", 30), ("30 minutes", 30), ("30m", 30), ("90 min", 90),
    ("1h30", 90), ("1h 30", 90), ("1h 30m", 90), ("1 hour 15", 75), ("1 hour and 30 minutes", 90), ("1 hour 30 minutes", 90),
    ("1.5 hours", 90), ("1,5 hours", 90), ("0.5 hours", 30), ("1:15", 75), ("1:30", 90),
    ("half an hour", 30), ("half hour", 30), ("a half hour", 30), ("an hour", 60), ("a hour", 60),
    ("an hour and a half", 90), ("hour and a half", 90), ("2 hours and a half", 150), ("two and a half hours", 150), ("1 and a half hours", 90),
    ("quarter of an hour", 15), ("a quarter hour", 15), ("three quarters of an hour", 45),
    ("two hours", 120), ("forty five minutes", 45), ("forty-five minutes", 45), ("twenty five min", 25), ("ninety minutes", 90),
    ("for 2 hours please", 120), ("about 45 minutes", 45), ("for an hour", 60), ("in 2 hours", 120), ("HOUR", None),
])
def test_offline_patterns(text, minutes):
    got = parse_offline(text)
    assert (None if got is None else round(got)) == minutes


@pytest.mark.parametrize("text", ["", "banana", "until 3pm", "30 min then a break", "as long as a lecture", "soon", "1 hour 30 minutes tomorrow"])
def test_things_the_plain_parser_leaves_to_the_ai(text):
    assert parse_offline(text) is None


def test_resolve_accepts_the_common_forms_without_any_ai():
    def boom(text, now):
        raise AssertionError("the AI must not be asked for something plain")

    for text, minutes in [("1 hour", 60), ("30 mins", 30), ("45", 45), ("half an hour", 30)]:
        result = resolve(text, boom, NOW)
        assert result.ok and result.minutes == minutes and not result.via_ai


@pytest.mark.parametrize("text, fragment", [
    ("2", "Did you mean 2 hours"), ("1", "Did you mean 1 hour? Say /focus 1 hour."), ("3 min", "too short"),
    ("0", "too short"), ("5 hours", "at most 4 hours"), ("10 hours", "at most 4 hours"), ("241", "at most 4 hours"),
])
def test_out_of_range_is_refused_with_a_helpful_message(text, fragment):
    result = resolve(text)
    assert not result.ok and fragment in result.error


def test_bounds_are_inclusive():
    assert resolve("5").minutes == 5 and resolve("240").minutes == 240 and resolve("4 hours").minutes == 240


def test_the_hours_hint_is_only_for_bare_numbers():
    assert "Did you mean" not in resolve("3 min").error
    assert "Did you mean" in resolve("3").error


def test_unreadable_without_an_ai_says_how_to_ask():
    result = resolve("banana")
    assert not result.ok and "/focus 45 min" in result.error
    assert "How long?" in resolve("").error


def test_the_ai_reads_the_rest_and_is_flagged():
    seen = []

    def ai(text, now):
        seen.append((text, now))
        return 100

    result = resolve("until 3pm", ai, NOW)
    assert result.ok and result.minutes == 100 and result.via_ai
    assert seen == [("until 3pm", NOW)]  # it is told the current time, so "until 3pm" can be worked out


@pytest.mark.parametrize("answer, ok", [(45, True), (4, False), (600, False), (None, False), (-30, False)])
def test_ai_answers_are_checked_like_any_other_input(answer, ok):
    result = resolve("as long as a lecture", lambda text, now: answer, NOW)
    assert result.ok is ok
    assert result.via_ai is ok


def test_a_failing_ai_is_a_polite_error_not_a_crash():
    def broken(text, now):
        raise RuntimeError("network down")

    result = resolve("until 3pm", broken, NOW)
    assert not result.ok and "couldn't tell" in result.error


def test_very_long_text_is_cut_before_it_reaches_the_ai():
    seen = []
    resolve("x" * 5000, lambda text, now: seen.append(text) or 30, NOW)
    assert len(seen[0]) <= 120


# ------------------------------------------------------------------ the AI adapter
class Brain:
    def __init__(self, reply):
        self.reply, self.calls = reply, []

    def generate_response(self, user_input, system_prompt, history=None):
        self.calls.append((user_input, system_prompt))
        return self.reply


@pytest.mark.parametrize("reply, expected", [
    ('{"minutes": 70}', 70),
    ('```json\n{"minutes": 70}\n```', 70),
    ('Sure! {"minutes": 25}', 25),
    ('{"minutes": 12.0}', 12),
    ('{"minutes": null}', None),
    ('{"minutes": "soon"}', None),
    ('{"minutes": true}', None),
    ("I have no idea", None),
    ("", None),
])
def test_ai_adapter_parses_the_reply(reply, expected):
    brain = Brain(reply)
    assert make_ai_minutes(brain)("an hour and ten", NOW) == expected
    user_input, system = brain.calls[0]
    assert user_input == "an hour and ten" and "Monday 13:20" in system  # it is given the local day and time


def test_ai_adapter_end_to_end_through_resolve():
    result = resolve("an hour and ten", make_ai_minutes(Brain('{"minutes": 70}')), NOW)
    assert result.minutes == 70 and result.via_ai
    assert duration.MIN_MINUTES == 5 and duration.MAX_MINUTES == 240
