from datetime import datetime
from unittest.mock import MagicMock

from app.phone.notifier import PhoneNotifier
from app.phone.state import DEFAULT_PREFS, PhoneState


def notifier_at(tmp_path, hour, minute):
    state = PhoneState(tmp_path / "phone.json")
    state.try_pair(state.new_pairing_code(), 111, "Ahmet")
    api = MagicMock()
    api.send_message.return_value = {"message_id": 1}
    return PhoneNotifier(api, state, now=lambda: datetime(2026, 10, 1, hour, minute)), api


def test_the_defaults_fit_a_six_oclock_riser_who_meditates_until_half_past():
    assert DEFAULT_PREFS["brief_time"] == "06:30" and DEFAULT_PREFS["evening_time"] == "20:30" and DEFAULT_PREFS["no_phone"] == "06:00-06:30"


def test_nothing_is_sent_during_meditation_not_even_a_timer(tmp_path):
    notifier, api = notifier_at(tmp_path, 6, 15)
    assert notifier.send("hello") is False
    assert notifier.send("plan nudge", direct=True) is False
    api.send_message.assert_not_called()


def test_it_goes_out_as_soon_as_meditation_is_over(tmp_path):
    notifier, api = notifier_at(tmp_path, 6, 30)
    assert notifier.send("☀️ Good morning", direct=True) is True
    notifier, _ = notifier_at(tmp_path, 5, 59)
    assert notifier.send("x", direct=True) is True  # before it starts is fine too
