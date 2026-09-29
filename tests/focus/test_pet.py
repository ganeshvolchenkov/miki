import os

import pytest

from app.focus import pet
from app.focus.pet_host import PetHost
from app.focus.winapi import Rect


def test_progress_runs_from_the_left_edge_to_the_right_edge():
    assert pet.progress(100, 100, 3700) == 0.0
    assert pet.progress(1900, 100, 3700) == pytest.approx(0.5)
    assert pet.progress(3700, 100, 3700) == 1.0


def test_progress_is_clamped_and_safe():
    assert pet.progress(50, 100, 3700) == 0.0  # before the start
    assert pet.progress(9999, 100, 3700) == 1.0  # after the end: waiting at the right corner
    assert pet.progress(500, None, None) == 0.0  # no timeline yet
    assert pet.progress(500, 100, 100) == 0.0  # a zero-length round can't divide by zero


def test_every_sprite_frame_is_well_formed():
    for feet in pet.FEET:
        for eyes in pet.EYES:
            grid = pet.build_grid(feet, eyes)
            assert len(grid) == pet.HEIGHT and all(len(row) == pet.WIDTH for row in grid)
            assert {c for row in grid for c in row} <= set(pet.PALETTE) | {"."}


def test_clicking_the_pet_opens_the_snipping_tool(monkeypatch):
    opened = []
    monkeypatch.setattr(os, "startfile", opened.append, raising=False)
    assert pet.open_snipping_tool() is True
    assert opened == ["ms-screenclip:"]  # Windows' own drag-to-capture overlay


def test_a_missing_snipping_tool_does_not_crash_the_pet(monkeypatch):
    def broken(_uri):
        raise OSError("no handler")

    monkeypatch.setattr(os, "startfile", broken, raising=False)
    assert pet.open_snipping_tool() is False


def test_host_sends_the_timeline_as_one_json_line():
    written = []

    class Stdin:
        def write(self, text):
            written.append(text)

        def flush(self):
            pass

    class Proc:
        stdin = Stdin()

        def poll(self):
            return None

    host = PetHost()
    host._proc = Proc()
    host.timeline(1000.0, 4600.0)
    assert written == ['{"cmd": "timeline", "start": 1000.0, "end": 4600.0}\n']


def test_host_without_a_pet_is_silent():
    host = PetHost()
    host.timeline(1.0, 2.0)
    host.say("hi")
    host.stop()
    assert not host.running and host.pid is None


def test_the_pet_sits_along_the_bottom_of_the_work_area(monkeypatch):
    started = []

    class FakePopen:
        def __init__(self, args, **kwargs):
            started.append(args)
            self.stdin = None

        def poll(self):
            return None

    monkeypatch.setattr("app.focus.pet_host.subprocess.Popen", FakePopen)
    assert PetHost().start(Rect(0, 0, 2560, 1540))
    args = started[0]
    assert args[args.index("--y") + 1] == str(1540 - 150) and args[args.index("--w") + 1] == "2560"


# ---- the pet never keeps your keyboard ---------------------------------------------------------------------------
def _handback(front, previous=100, own_pid=7, pids=None):
    pids = pids or {100: 55, 200: 55, 300: 7}  # windows -> owning process (300 is the pet's own)
    calls = []
    done = pet.restore_foreground(
        previous, foreground=lambda: front, owner_pid=lambda h: pids.get(h, 0), set_foreground=calls.append, own_pid=own_pid,
    )
    return done, calls


def test_focus_goes_back_to_your_window_when_the_pet_grabs_it():
    assert _handback(front=300) == (True, [100])


def test_focus_is_left_alone_when_you_already_moved_on():
    assert _handback(front=200) == (False, [])  # you clicked another window: not ours to touch


def test_focus_is_not_handed_to_a_window_that_is_gone_or_ours():
    assert _handback(front=300, previous=999) == (False, [])  # the old window no longer exists
    assert _handback(front=300, previous=300) == (False, [])  # never hand the focus to ourselves
    assert _handback(front=300, previous=0) == (False, [])  # we never knew what was in front
    assert _handback(front=0) == (False, [])
