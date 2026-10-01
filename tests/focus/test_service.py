import pytest

from app.focus import desk as desk_module
from app.focus import service as service_module
from app.focus.service import FocusConfig, FocusService
from app.focus.session import FocusSession
from app.focus.winapi import Window

from .fakes import FOCUS_CHROME_PID, PRIMARY, SECOND, FakeChrome, FakeDesktop, FakePet, tab, window

T0 = 1_800_000_000.0


class Clock:
    def __init__(self):
        self.now = T0

    def __call__(self):
        return self.now

    def advance(self, minutes):
        self.now += minutes * 60


@pytest.fixture(autouse=True)
def no_sleeping(monkeypatch):
    monkeypatch.setattr(service_module.time, "sleep", lambda s: None)


def make(tmp_path, *, windows=None, tabs=None, chrome_running=False, config=None, **extra):
    clock = Clock()
    desktop = FakeDesktop(windows)
    chrome = FakeChrome(tabs, running=chrome_running)
    counter = [100]

    def new_window(url):  # opening a Chrome window makes a window appear on the fake desktop
        counter[0] += 1
        title = "Google Gemini" if "gemini" in url else "Claude"
        desktop.windows.append(Window(counter[0], FOCUS_CHROME_PID, title, "Chrome_WidgetWin_1", "chrome.exe"))

    chrome.on_open_window = new_window
    pet = FakePet()
    beeps = []
    svc = FocusService(
        config or FocusConfig(state_path=tmp_path / "focus.json"),
        session=FocusSession(tmp_path / "focus.json", clock=clock), desktop=desktop, chrome=chrome, pet=pet,
        clock=clock, lock_name=None, beep=lambda: beeps.append(1), **extra,
    )
    events = []
    svc.add_listener(events.append)
    svc.beeps = beeps
    return svc, desktop, chrome, pet, clock, events


def start_and_wait(svc, minutes=60):
    reply = svc.start_focus(minutes)
    assert reply.ok, reply.text
    assert svc._armed.wait(5)
    return reply


def test_start_sets_up_both_screens(tmp_path):
    svc, desktop, chrome, pet, clock, events = make(tmp_path, windows=[window(50, "discord.exe", "Discord")])
    reply = start_and_wait(svc)
    assert "1 hour" in reply.text and "Gemini" in reply.text and "Claude" in reply.text
    urls = [w[0] for w in chrome.windows_opened]
    assert urls == ["https://gemini.google.com/app", "https://claude.ai/new"]
    placed = {hwnd: rect for hwnd, rect in desktop.placed}
    assert list(placed.values()) == [PRIMARY.work, SECOND.work]  # Gemini on screen 1, Claude on screen 2
    assert 50 in desktop.minimized  # the clutter is tucked away
    assert pet.running and ("start", PRIMARY.work) in pet.calls
    timeline = [c for c in pet.calls if c[0] == "timeline"]  # the pet shows the round as a progress bar
    assert len(timeline) == 1 and timeline[0][2] - timeline[0][1] == 3600
    assert "Off limits" in reply.text and "YouTube" in reply.text
    assert svc.is_focusing


def test_with_one_monitor_the_windows_sit_side_by_side(tmp_path):
    svc, desktop, chrome, *_ = make(tmp_path)
    desktop._monitors = [PRIMARY]
    start_and_wait(svc)
    rects = [rect for _, rect in desktop.placed]
    assert rects[0].w == rects[1].w == PRIMARY.work.w // 2 and rects[1].x == rects[0].w


def test_existing_focus_windows_are_reused_not_duplicated(tmp_path):
    gemini = Window(1, FOCUS_CHROME_PID, "Google Gemini", "Chrome_WidgetWin_1", "chrome.exe")
    other = Window(2, FOCUS_CHROME_PID, "Some Notes", "Chrome_WidgetWin_1", "chrome.exe")
    svc, desktop, chrome, *_ = make(tmp_path, windows=[other, gemini], chrome_running=True)
    start_and_wait(svc)
    assert chrome.windows_opened == []
    assert [hwnd for hwnd, _ in desktop.placed] == [1, 2]  # Gemini (found by title) on screen 1, the other on screen 2


def test_cannot_start_while_running(tmp_path):
    svc, *_ = make(tmp_path)
    start_and_wait(svc)
    again = svc.start_focus(30)
    assert not again.ok and "already running" in again.text


def test_no_chrome_means_a_clear_message_and_no_session(tmp_path, monkeypatch):
    monkeypatch.setattr(desk_module, "find_chrome", lambda explicit=None: None)
    svc = FocusService(FocusConfig(state_path=tmp_path / "f.json"), desktop=FakeDesktop(), pet=FakePet(), lock_name=None)
    reply = svc.start_focus()
    assert not reply.ok and "Chrome" in reply.text
    assert not svc.session.is_active


def test_off_switch(tmp_path):
    svc, *_ = make(tmp_path, config=FocusConfig(enabled=False, state_path=tmp_path / "f.json"))
    reply = svc.start_focus()
    assert not reply.ok and "off" in reply.text
    assert not svc.available


def test_the_guard_bounces_a_distraction_and_the_pet_reacts(tmp_path):
    discord = window(9, "discord.exe", "Discord")
    svc, desktop, chrome, pet, clock, events = make(tmp_path)
    start_and_wait(svc)
    desktop.windows.append(discord)
    desktop.front = discord
    svc.tick()
    assert desktop.minimized.count(9) >= 1
    assert svc.session.round.distractions == 1
    assert svc.session.round.blocked == {"Discord": 1}
    assert "alert" in pet.moods() and any("Discord" in s for s in pet.said())


def test_distracting_tabs_are_closed(tmp_path):
    svc, desktop, chrome, pet, clock, events = make(tmp_path)
    start_and_wait(svc)
    from app.focus.chrome import Tab

    chrome._tabs = [Tab(tab("ok", "https://claude.ai/new")), Tab(tab("bad", "https://www.youtube.com/"))]
    svc.tick()
    assert chrome.closed == ["bad"]
    assert svc.session.round.blocked == {"www.youtube.com": 1}


def test_the_guard_waits_until_the_screens_are_ready(tmp_path):
    svc, desktop, chrome, pet, clock, events = make(tmp_path)
    svc.session.start(60)  # a round is running, but arming hasn't finished
    discord = window(9, "discord.exe", "Discord")
    desktop.windows.append(discord)
    desktop.front = discord
    svc.tick()
    assert desktop.minimized == []


def test_ten_minute_warning_reaches_listeners_and_the_pet(tmp_path):
    svc, desktop, chrome, pet, clock, events = make(tmp_path)
    start_and_wait(svc)
    clock.advance(50.5)
    svc.tick()
    assert [e.kind for e in events] == ["warn"]
    assert "10 min left" in events[0].text
    assert any("10 min left" in s for s in pet.said())


def test_after_an_hour_miki_calls_a_break_and_the_lock_lifts(tmp_path):
    svc, desktop, chrome, pet, clock, events = make(tmp_path)
    start_and_wait(svc)
    svc.session.record_distraction("youtube.com")
    clock.advance(60)
    svc.tick()
    kinds = [e.kind for e in events]
    assert kinds == ["time_up"]
    assert "break" in events[0].text.lower() and "1 hour" in events[0].text and "youtube.com" in events[0].text
    assert svc.beeps == [1]
    assert "cheer" in pet.moods() and any("break" in s.lower() for s in pet.said())
    assert not svc.is_focusing

    # the lock is off: leaving is fine now
    discord = window(9, "discord.exe", "Discord")
    desktop.windows.append(discord)
    desktop.front = discord
    before = list(desktop.minimized)
    svc.tick()
    assert desktop.minimized == before

    # the pet curls up after celebrating, then break ends
    clock.advance(1)
    svc.tick()
    assert pet.moods()[-1] == "sleep"
    clock.advance(5)
    svc.tick()
    assert [e.kind for e in events] == ["time_up", "break_over"]
    clock.advance(1)
    svc.tick()
    assert not pet.running  # the pet says goodbye and leaves


def test_extending_during_the_break_locks_again(tmp_path):
    svc, desktop, chrome, pet, clock, events = make(tmp_path)
    start_and_wait(svc)
    clock.advance(60)
    svc.tick()
    reply = svc.extend(15)
    assert reply.ok and svc.is_focusing
    assert svc._armed.wait(5)


def test_stop_ends_early_with_a_summary(tmp_path):
    svc, desktop, chrome, pet, clock, events = make(tmp_path)
    start_and_wait(svc)
    clock.advance(20)
    reply = svc.stop()
    assert reply.ok and "20 min" in reply.text
    assert not svc.session.is_active and not svc._armed.is_set()
    assert not svc.stop().ok


def test_status_and_stats_text(tmp_path):
    svc, desktop, chrome, pet, clock, events = make(tmp_path)
    assert svc.status_text() == "No focus session running."
    start_and_wait(svc)
    assert "Focusing" in svc.status_text() and "1 hour" in svc.status_text()
    clock.advance(30)
    svc.stop()
    assert "30 min" in svc.stats_text()
    assert svc.view()["phase"] == "idle"


# ------------------------------------------------------------------ the banned list
def test_defaults_are_banned_and_study_sites_are_not(tmp_path):
    svc, *_ = make(tmp_path)
    assert "youtube.com" in svc.banned_sites() and "discord.exe" in svc.banned_apps()
    assert "claude.ai" not in svc.banned_sites() and "code.exe" not in svc.banned_apps()


def test_you_can_ban_a_site_or_a_program(tmp_path):
    svc, *_ = make(tmp_path)
    assert svc.ban("https://www.9anime.to/x").ok and "9anime.to" in svc.banned_sites()
    assert svc.ban("obsidian").ok and "obsidian.exe" in svc.banned_apps()
    assert "already banned" in svc.ban("youtube.com").text
    assert not svc.ban("!!!").ok
    assert svc.session.banned_added() == ("9anime.to", "obsidian.exe")


def test_you_can_lift_your_own_ban_and_a_built_in_one(tmp_path):
    svc, *_ = make(tmp_path)
    svc.ban("9anime.to")
    assert svc.unban("9anime.to").ok and "9anime.to" not in svc.banned_sites()
    assert svc.unban("reddit.com").ok and "reddit.com" not in svc.banned_sites()
    assert svc.unban("discord").ok and "discord.exe" not in svc.banned_apps()
    assert not svc.unban("reddit.com").ok  # already lifted
    assert svc.ban("reddit.com").ok and "reddit.com" in svc.banned_sites()  # banning again brings it back
    assert svc.session.banned_removed() == ("discord.exe",)


def test_bans_can_be_added_mid_session_but_not_lifted(tmp_path):
    svc, *_ = make(tmp_path)
    start_and_wait(svc)
    assert svc.ban("tiktok.com").ok  # stricter is always fine
    reply = svc.unban("youtube.com")
    assert not reply.ok and "between sessions" in reply.text
    assert "youtube.com" in svc.banned_sites()


def test_bans_from_the_env_file_count_and_stick(tmp_path):
    config = FocusConfig(state_path=tmp_path / "f.json", env_sites=("arxiv.org",), env_apps=("notepad.exe",))
    svc, *_ = make(tmp_path, config=config)
    assert "arxiv.org" in svc.banned_sites() and "notepad.exe" in svc.banned_apps()
    assert ".env" in svc.unban("arxiv.org").text and "arxiv.org" in svc.banned_sites()


def test_a_new_ban_takes_effect_in_the_guard_immediately(tmp_path):
    svc, desktop, *_ = make(tmp_path)
    start_and_wait(svc)
    notes = window(9, "obsidian.exe", "Vault - Obsidian")
    desktop.windows.append(notes)
    desktop.front = notes
    svc.tick()
    assert svc.session.round.distractions == 0
    svc.ban("obsidian")
    desktop.front = notes
    svc.tick()
    assert svc.session.round.blocked == {"Obsidian": 1}


def test_the_progress_bar_follows_the_round(tmp_path):
    svc, desktop, chrome, pet, clock, events = make(tmp_path)
    start_and_wait(svc)
    assert pet.calls.count(("timeline", T0, T0 + 3600)) == 1
    svc.extend(15)
    assert ("timeline", T0, T0 + 3600 + 900) in pet.calls  # the bar stretches; the pet steps back a little
    clock.advance(75)
    svc.tick()
    assert "walk" not in pet.moods()  # no wandering, ever


def test_a_new_round_after_a_break_restarts_the_bar(tmp_path):
    svc, desktop, chrome, pet, clock, events = make(tmp_path)
    start_and_wait(svc)
    clock.advance(60)
    svc.tick()
    svc.extend(15)
    assert svc._armed.wait(5)
    timelines = [c for c in pet.calls if c[0] == "timeline"]
    assert timelines[-1][1] == T0 + 3600 and timelines[-1][2] == T0 + 3600 + 900


# ------------------------------------------------------------------ typed commands
def test_typed_commands(tmp_path):
    svc, *_ = make(tmp_path)
    assert "Focus mode is on for 45 min" in svc.command("45")
    assert svc._armed.wait(5)
    assert "Focusing" in svc.command("status")
    assert "Try /focus" in svc.command("banana")
    assert "Focus mode ended" in svc.command("stop")
    assert "Today" in svc.command("stats")
    assert "Banned during focus" in svc.command("banned")
    assert "Banned during focus" in svc.command("sites")
    assert "Banned 9anime.to" in svc.command("ban 9anime.to")
    assert "no longer banned" in svc.command("unban 9anime.to")


def test_config_from_env(monkeypatch):
    monkeypatch.setenv("MIKI_FOCUS_MINUTES", "45")
    monkeypatch.setenv("MIKI_FOCUS_BAN", "9anime.to, arxiv.org notepad Obsidian.exe")
    monkeypatch.setenv("MIKI_FOCUS_SCREEN2_URL", "https://canvas.uva.nl/")
    monkeypatch.setenv("MIKI_FOCUS_SWAP_SCREENS", "1")
    config = FocusConfig.from_env()
    assert config.minutes == 45 and config.env_sites == ("9anime.to", "arxiv.org")
    assert config.env_apps == ("notepad.exe", "obsidian.exe")
    assert config.screen2_url == "https://canvas.uva.nl/" and config.swap_screens
    monkeypatch.setenv("MIKI_FOCUS_MINUTES", "banana")
    assert FocusConfig.from_env().minutes == 60
    monkeypatch.setenv("MIKI_FOCUS", "0")
    assert not FocusConfig.from_env().enabled


def test_resume_after_a_restart_puts_the_lock_back(tmp_path):
    svc, desktop, chrome, pet, clock, events = make(tmp_path)
    start_and_wait(svc)
    clock.advance(10)
    # a new process picks up the same file
    svc2 = FocusService(
        svc.config, session=FocusSession(tmp_path / "focus.json", clock=clock), desktop=desktop, chrome=chrome,
        pet=FakePet(), clock=clock, lock_name=None,
    )
    assert svc2.is_focusing
    svc2._resume()
    assert svc2._armed.is_set() and svc2.pet.running


def _reopen(svc, desktop, chrome, clock, tmp_path):
    """A fresh Miki process (its own Chrome handle, pet and clock thread) on the same state file."""
    chrome2 = FakeChrome(running=False)
    chrome2.on_open_window = lambda url: desktop.windows.append(
        Window(900 + len(chrome2.windows_opened), FOCUS_CHROME_PID, "Google Gemini" if "gemini" in url else "Claude", "Chrome_WidgetWin_1", "chrome.exe")
    )
    return FocusService(
        svc.config, session=FocusSession(tmp_path / "focus.json", clock=clock), desktop=desktop, chrome=chrome2,
        pet=FakePet(), clock=clock, lock_name=None,
    )


def test_a_round_that_ended_while_miki_was_closed_does_not_reopen_chrome(tmp_path):
    """Opening Miki must not put the focus screens up for a round that is already over."""
    svc, desktop, chrome, pet, clock, events = make(tmp_path)
    start_and_wait(svc, 50)
    clock.advance(50 + 60)  # Miki was closed, and the round ended an hour ago
    svc2 = _reopen(svc, desktop, chrome, clock, tmp_path)
    svc2._resume()
    assert svc2.chrome.windows_opened == []
    assert not svc2.pet.running and not svc2._armed.is_set()
    assert not svc2.session.is_active


def test_a_round_that_just_ended_while_miki_was_closed_goes_to_the_break_not_the_lock(tmp_path):
    svc, desktop, chrome, pet, clock, events = make(tmp_path)
    start_and_wait(svc, 50)
    clock.advance(50 + 3)  # ended three minutes ago: still worth a "time for a break"
    svc2 = _reopen(svc, desktop, chrome, clock, tmp_path)
    seen = []
    svc2.add_listener(seen.append)
    svc2._resume()
    assert svc2.chrome.windows_opened == [] and not svc2._armed.is_set()
    assert svc2.session.phase == "break" and [e.kind for e in seen] == ["time_up"]


def test_a_round_still_running_when_miki_reopens_gets_its_screens_back(tmp_path):
    svc, desktop, chrome, pet, clock, events = make(tmp_path)
    start_and_wait(svc, 50)
    clock.advance(10)
    svc2 = _reopen(svc, desktop, chrome, clock, tmp_path)
    svc2._resume()
    assert svc2._armed.is_set() and len(svc2.chrome.windows_opened) >= 1
