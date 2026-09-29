from app.focus import rules
from app.focus.guard import FocusGuard
from app.focus.winapi import Monitor, Rect, order_screens, split_rect

from .fakes import FOCUS_CHROME_PID, PRIMARY, SECOND, FakeChrome, FakeDesktop, tab, window

SITES = rules.DEFAULT_BANNED_SITES
APPS = frozenset(rules.DEFAULT_BANNED_APPS)
GEMINI = window(1, "chrome.exe", "Google Gemini", pid=FOCUS_CHROME_PID)
CLAUDE = window(2, "chrome.exe", "Claude", pid=FOCUS_CHROME_PID)
MIKI = window(3, "python.exe", "Miki Command Center", pid=4242)
DISCORD = window(4, "discord.exe", "Discord")
EVERYDAY_TUBE = window(5, "chrome.exe", "Funny cats - YouTube - Google Chrome", pid=999)
EVERYDAY_PDF = window(6, "chrome.exe", "Lecture5_Eigenvalues.pdf - Google Chrome", pid=999)
VSCODE = window(7, "code.exe", "service.py - Miki - Visual Studio Code")
EXPLORER = window(8, "explorer.exe", "Documents - File Explorer")


def guard(desktop, chrome=None, clock=None, sites=SITES, apps=APPS, **kwargs):
    ticks = iter(range(0, 10_000, 10))  # every call to the clock is 10 seconds later: repeats never coalesce by accident
    return FocusGuard(
        desktop, chrome if chrome is not None else FakeChrome(), banned_sites=lambda: sites, banned_apps=lambda: apps,
        own_pids=lambda: {4242}, clock=clock or (lambda: next(ticks)), **kwargs,
    )


def test_normal_apps_are_left_alone():
    """The whole point of a ban list: your editor, notes, file manager and PDFs are none of Miki's business."""
    desktop = FakeDesktop([GEMINI, CLAUDE, MIKI, VSCODE, EXPLORER, EVERYDAY_PDF])
    for front in desktop.windows:
        desktop.front = front
        assert guard(desktop).tick() == []
    assert desktop.minimized == []


def test_a_banned_app_is_minimised_and_focus_comes_back():
    desktop = FakeDesktop([GEMINI, DISCORD])
    desktop.front = DISCORD
    bounces = guard(desktop).tick()
    assert [(b.kind, b.label) for b in bounces] == [("app", "Discord")]
    assert desktop.minimized == [DISCORD.hwnd]
    assert desktop.activated == [GEMINI.hwnd]


def test_an_everyday_browser_on_a_banned_site_is_minimised():
    desktop = FakeDesktop([GEMINI, EVERYDAY_TUBE])
    desktop.front = EVERYDAY_TUBE
    bounces = guard(desktop).tick()
    assert [(b.kind, b.label) for b in bounces] == [("site", "youtube.com")]
    assert desktop.minimized == [EVERYDAY_TUBE.hwnd]


def test_edge_and_firefox_are_checked_by_title_too():
    for exe in ("msedge.exe", "firefox.exe"):
        desktop = FakeDesktop([GEMINI])
        w = window(9, exe, "Reddit - Dive into anything", pid=77)
        desktop.windows.append(w)
        desktop.front = w
        assert [b.label for b in guard(desktop).tick()] == ["reddit.com"]


def test_a_pdf_or_lecture_in_the_everyday_chrome_is_fine_until_you_switch_tab():
    desktop = FakeDesktop([GEMINI, EVERYDAY_PDF])
    desktop.front = EVERYDAY_PDF
    g = guard(desktop)
    assert g.tick() == [] and desktop.minimized == []
    tube = window(6, "chrome.exe", "Funny cats - YouTube - Google Chrome", pid=999)  # same window, other tab
    desktop.windows = [GEMINI, tube]
    desktop.front = tube
    assert [b.label for b in g.tick()] == ["youtube.com"]
    assert desktop.minimized == [6]


def test_the_watched_chrome_is_judged_by_url_not_title():
    tricky = window(10, "chrome.exe", "YouTube tutorial notes - Google Chrome", pid=FOCUS_CHROME_PID)
    desktop = FakeDesktop([tricky])
    desktop.front = tricky
    assert guard(desktop).tick() == []  # its tabs are policed by URL, so a title alone doesn't count


def test_the_same_distraction_is_counted_once_within_a_few_seconds():
    desktop = FakeDesktop([GEMINI, DISCORD])
    now = [0.0]
    g = guard(desktop, clock=lambda: now[0])
    desktop.front = DISCORD
    assert len(g.tick()) == 1
    now[0] += 1
    desktop.front = DISCORD  # still in front for a moment while Windows minimises it
    assert g.tick() == []
    assert desktop.minimized == [4, 4]  # but it is minimised again, just not counted twice
    now[0] += 30
    desktop.front = DISCORD
    assert len(g.tick()) == 1


def test_your_own_bans_apply():
    desktop = FakeDesktop([GEMINI, VSCODE])
    desktop.front = VSCODE
    assert guard(desktop).tick() == []
    desktop.front = VSCODE
    assert [b.label for b in guard(desktop, apps=APPS | {"code.exe"}).tick()] == ["Code"]


def test_banned_tabs_are_closed_everything_else_stays():
    chrome = FakeChrome([
        tab("a", "https://gemini.google.com/app"),
        tab("b", "https://www.youtube.com/watch?v=1"),
        tab("c", "https://en.wikipedia.org/wiki/Matrix"),
        tab("d", "file:///C:/notes/lecture.pdf"),
        tab("e", "https://www.reddit.com/"),
    ])
    desktop = FakeDesktop([GEMINI])
    desktop.front = GEMINI
    bounces = guard(desktop, chrome).tick()
    assert sorted(b.label for b in bounces) == ["www.reddit.com", "www.youtube.com"]
    assert sorted(chrome.closed) == ["b", "e"]
    assert sorted(t.id for t in chrome.tabs()) == ["a", "c", "d"]


def test_closing_the_last_tab_leaves_a_study_page_behind():
    chrome = FakeChrome([tab("x", "https://www.youtube.com/")])
    desktop = FakeDesktop([GEMINI])
    desktop.front = GEMINI
    guard(desktop, chrome, home_url="https://claude.ai/new").tick()
    assert chrome.opened == ["https://claude.ai/new"]
    assert chrome.closed == ["x"]


def test_a_broken_chrome_never_crashes_the_guard():
    class Broken(FakeChrome):
        def tabs(self):
            raise RuntimeError("boom")

    desktop = FakeDesktop([DISCORD, GEMINI])
    desktop.front = DISCORD
    assert len(guard(desktop, Broken()).tick()) == 1  # the app check still worked


def test_clean_slate_tucks_away_only_banned_windows():
    desktop = FakeDesktop([GEMINI, MIKI, DISCORD, EVERYDAY_TUBE, EVERYDAY_PDF, VSCODE, EXPLORER])
    count = guard(desktop).clean_slate()
    assert count == 2
    assert sorted(desktop.minimized) == [DISCORD.hwnd, EVERYDAY_TUBE.hwnd]


# ------------------------------------------------------------------ screens
def test_screen_one_is_the_primary_monitor():
    ordered = order_screens([SECOND, PRIMARY])
    assert ordered[0].primary and ordered[1] is SECOND


def test_screens_can_be_swapped():
    assert order_screens([PRIMARY, SECOND], swap=True)[0] is SECOND


def test_three_monitors_go_left_to_right_after_the_primary():
    far = Monitor("c", Rect(5000, 0, 1000, 1000), Rect(5000, 0, 1000, 1000), False)
    near = Monitor("b", Rect(2560, 0, 1000, 1000), Rect(2560, 0, 1000, 1000), False)
    assert order_screens([far, near, PRIMARY]) == [PRIMARY, near, far]


def test_split_rect_gives_side_by_side_halves():
    work = Rect(0, 0, 2560, 1540)
    assert split_rect(work, 0) == Rect(0, 0, 1280, 1540)
    assert split_rect(work, 1) == Rect(1280, 0, 1280, 1540)
