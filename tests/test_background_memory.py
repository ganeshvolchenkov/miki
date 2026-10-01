"""Replies come back before the memory pipeline runs; memories arrive later, in order, and never overlap a read."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

from app.core.assistant import MikiCore


class Brain:
    def generate_response(self, user_input, system_prompt, history=None):
        return f"reply to {user_input}"


class Conversations:
    def __init__(self):
        self.turns = []

    def read_messages(self):
        return []

    def append_turn(self, user, reply):
        self.turns.append((user, reply))


class Store:
    def __init__(self):
        self.inside = 0
        self.overlaps = 0
        self.memories = []

    def list_memories(self, active_only=True):
        self.inside += 1
        if self.inside > 1:
            self.overlaps += 1
        time.sleep(0.01)
        self.inside -= 1
        return list(self.memories)

    def create_memory(self, content, **kwargs):
        self.inside += 1
        if self.inside > 1:
            self.overlaps += 1
        time.sleep(0.02)  # a slow, non-atomic note write
        memory = SimpleNamespace(memory_id=str(len(self.memories)), content=content, category="fact")
        self.memories.append(memory)
        self.inside -= 1
        return memory


class Manager:
    """Learns one memory per message, slowly (like the real evaluator/extractor/comparator calls)."""

    def __init__(self, delay=0.3):
        self.brain = None
        self.memory_store = Store()
        self.delay = delay
        self.last_memories = []
        self.last_candidate = None
        self.release = threading.Event()
        self.release.set()

    def build_recall_block(self, query):
        self.memory_store.list_memories()
        return None

    def extract_and_store_from_interaction(self, user_input, response, has_tool_call=False):
        self.release.wait(5)
        time.sleep(self.delay)
        memory = self.memory_store.create_memory(f"learned from {user_input}")
        self.last_memories = [memory]
        return memory


def make(delay=0.3):
    manager = Manager(delay)
    return MikiCore(brain=Brain(), conversation_store=Conversations(), memory_manager=manager), manager


def test_without_a_callback_everything_happens_before_returning():
    core, manager = make(delay=0.05)
    reply, created = core.process_user_input("hello")
    assert reply == "reply to hello" and created
    assert [m.content for m in core.last_memories] == ["learned from hello"]


def test_with_a_callback_the_reply_does_not_wait_for_memory():
    core, manager = make(delay=0.5)
    learned = []
    done = threading.Event()
    started = time.perf_counter()
    reply, created = core.process_user_input("I play padel", on_memories=lambda found: (learned.extend(found), done.set()))
    assert reply == "reply to I play padel" and not created
    assert time.perf_counter() - started < 0.3  # the 0.5 s pipeline did not hold up the answer
    assert not learned
    assert done.wait(5)
    assert [m.content for m in learned] == ["learned from I play padel"]


def test_memories_are_filed_in_order_and_reads_never_see_a_half_written_note():
    core, manager = make(delay=0.05)
    order = []
    done = threading.Event()
    for n in range(4):
        core.process_user_input(f"message {n}", on_memories=lambda found: (order.extend(m.content for m in found),
                                                                           done.set() if len(order) == 4 else None))
    assert done.wait(10)
    assert order == [f"learned from message {n}" for n in range(4)]
    assert manager.memory_store.overlaps == 0


def test_nothing_learned_means_no_callback():
    core, manager = make(delay=0.0)
    manager.extract_and_store_from_interaction = lambda *a, **k: None
    called = []
    core.process_user_input("ok", on_memories=called.append)
    time.sleep(0.2)
    assert called == []
