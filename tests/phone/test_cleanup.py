import time
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.phone.service import PhoneService, _hours
from app.phone.state import PhoneState
from app.phone.telegram_api import TelegramApi


def paired_state(tmp_path):
    state = PhoneState(tmp_path / "phone.json")
    state.try_pair(state.new_pairing_code(), 111, "Ahmet")
    return state


def service_for(state, api, hours=24.0):
    return SimpleNamespace(state=state, api=api, cleanup_hours=hours)


def test_old_messages_are_deleted_and_recent_ones_kept(tmp_path):
    state = paired_state(tmp_path)
    now = time.time()
    state._data["messages"] = [[111, 1, now - 30 * 3600], [111, 2, now - 25 * 3600], [111, 3, now - 2 * 3600]]
    api = MagicMock()
    api.delete_message.return_value = True
    assert PhoneService.purge_old_messages(service_for(state, api)) == 2
    assert [c.args for c in api.delete_message.call_args_list] == [(111, 1), (111, 2)]
    assert [m for _, m, _ in state._data["messages"]] == [3]


def test_a_message_telegram_refuses_to_delete_is_not_retried_forever(tmp_path):
    state = paired_state(tmp_path)
    state._data["messages"] = [[111, 1, time.time() - 60 * 3600]]  # older than the 48 hours bots may delete
    api = MagicMock()
    api.delete_message.return_value = False
    assert PhoneService.purge_old_messages(service_for(state, api)) == 0
    assert state._data["messages"] == []


def test_cleanup_can_be_switched_off(tmp_path):
    state = paired_state(tmp_path)
    state._data["messages"] = [[111, 1, time.time() - 99 * 3600]]
    api = MagicMock()
    assert PhoneService.purge_old_messages(service_for(state, api, hours=0)) == 0
    api.delete_message.assert_not_called()
    assert len(state._data["messages"]) == 1


def test_sent_messages_are_tracked_by_the_api():
    seen = []
    api = TelegramApi("123:abc", opener=lambda request, timeout: _Response({"ok": True, "result": {"message_id": 42}}))
    api.on_sent = lambda chat, message: seen.append((chat, message))
    api.send_message(111, "hi")
    assert seen == [(111, 42)]


def test_delete_message_calls_telegram():
    calls = []

    def opener(request, timeout):
        calls.append(request.full_url.rsplit("/", 1)[1])
        return _Response({"ok": True, "result": True})

    assert TelegramApi("123:abc", opener=opener).delete_message(111, 5) is True and calls == ["deleteMessage"]


def test_hours_setting():
    assert _hours(None, 24.0) == 24.0 and _hours("12", 24.0) == 12.0 and _hours("0", 24.0) == 0.0 and _hours("x", 24.0) == 24.0


class _Response:
    def __init__(self, payload):
        import json

        self._body = json.dumps(payload).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False
