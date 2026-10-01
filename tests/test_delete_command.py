import time
from unittest.mock import MagicMock

from app.core.assistant import MikiCore
from app.memory.conversation import JSONConversationStore
from app.phone.bot import PhoneBot
from app.phone.state import PhoneState

OWNER = 111


def test_reset_starts_an_empty_session_and_keeps_the_old_one_on_disk(tmp_path):
    store = JSONConversationStore(tmp_path)
    store.append_turn("hi", "hello")
    store.append_turn("how are you", "fine")
    first = store.file_path
    assert store.reset() == 4
    assert store.read_messages() == [] and store.file_path != first
    assert len(first.read_text()) > 0 and '"ended_at": null' not in first.read_text()  # archived, closed
    store.append_turn("new", "chat")
    assert [m["content"] for m in store.read_messages()] == ["new", "chat"]


def test_core_reset_forgets_the_pending_question_too():
    core = MikiCore.__new__(MikiCore)
    core.conversation_store = MagicMock()
    core.conversation_store.reset.return_value = 6
    core.pending_tool_action = object()
    core.last_memories = [1]
    assert core.reset_conversation() == 6 and core.pending_tool_action is None and core.last_memories == []


def bot_for(tmp_path):
    state = PhoneState(tmp_path / "phone.json")
    state.try_pair(state.new_pairing_code(), OWNER, "Ahmet")
    api = MagicMock()
    api.send_message.return_value = {"message_id": 7}
    api.delete_messages.return_value = True
    backend = MagicMock()
    backend.interview_active = False
    backend.reset_conversation.return_value = 12
    return PhoneBot(api, state, backend), api, backend, state


def test_delete_on_the_phone_resets_and_wipes_the_chat(tmp_path):
    bot, api, backend, state = bot_for(tmp_path)
    for message_id in (1, 2, 3):
        state.track_message(OWNER, message_id)
    bot.handle_update({"message": {"chat": {"id": OWNER, "type": "private"}, "from": {"first_name": "A"}, "text": "/delete", "message_id": 4,
                                   "date": time.time()}})
    for _ in range(50):  # the wipe runs in the background
        if api.delete_messages.called:
            break
        time.sleep(0.05)
    backend.reset_conversation.assert_called_once()
    chat, ids = api.delete_messages.call_args.args
    assert chat == OWNER and sorted(ids) == [1, 2, 3, 4]  # the command message itself too
    assert "Fresh start" in api.send_message.call_args.args[1] and "12 messages" in api.send_message.call_args.args[1]
    assert state.messages_older_than(0) == [(OWNER, 7)] or state.messages_older_than(0) == []  # only the new note may be tracked


def test_a_stranger_cannot_delete_anything(tmp_path):
    bot, api, backend, _ = bot_for(tmp_path)
    bot.handle_update({"message": {"chat": {"id": 999, "type": "private"}, "from": {"first_name": "X"}, "text": "/delete", "message_id": 1,
                                   "date": time.time()}})
    backend.reset_conversation.assert_not_called()
    api.delete_messages.assert_not_called()
