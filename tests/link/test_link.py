"""The brain/hands link: the wire protocol, the hub and client over real sockets, and focus mode split across them."""

from __future__ import annotations

import threading
import time

import pytest

from app.focus import desk as desk_module
from app.focus import service as service_module
from app.focus.desk import FocusDesk
from app.focus.service import FocusConfig, FocusService
from app.focus.session import FocusSession
from app.focus.winapi import Window
from app.hands.agent import GRACE_SECONDS, HandsAgent
from app.interfaces.remote_gui import RemoteDashboard
from app.link import protocol
from app.link.client import LinkClient
from app.link.hub import LinkHub
from app.link.remote import RemoteHands, RemoteWindow, wire_dashboard, wire_focus
from app.link.tunnel import SshTunnel, explain_ssh_error

from tests.focus.fakes import FOCUS_CHROME_PID, PRIMARY, SECOND, FakeChrome, FakeDesktop, FakePet, window

TOKEN = "t" * 40


def wait_for(condition, seconds=5.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def hub():
    h = LinkHub(TOKEN, port=0)
    h.start()
    yield h
    h.stop()


def client(hub, roles, *, token=TOKEN, state=dict, on_message=None, statuses=None):
    received = []
    c = LinkClient(("127.0.0.1", hub.port), token, roles, on_message=on_message or received.append,
                   on_status=lambda up, why: statuses.append((up, why)) if statuses is not None else None, state=state)
    c.received = received
    c.start()
    return c


# ---------------------------------------------------------------------- protocol
def test_signature_needs_the_same_token_nonce_and_roles():
    mac = protocol.sign(TOKEN, "abc", ["hands"])
    assert protocol.verify(TOKEN, "abc", ["hands"], mac)
    assert not protocol.verify("x" * 40, "abc", ["hands"], mac)
    assert not protocol.verify(TOKEN, "abd", ["hands"], mac)
    assert not protocol.verify(TOKEN, "abc", ["dashboard"], mac)  # a hands login can't be replayed as a dashboard
    assert not protocol.verify("", "abc", ["hands"], protocol.sign("", "abc", ["hands"]))


def test_short_tokens_are_not_accepted(monkeypatch):
    monkeypatch.setenv("MIKI_LINK_TOKEN", "short")
    assert protocol.link_token() == ""
    monkeypatch.setenv("MIKI_LINK_TOKEN", TOKEN)
    assert protocol.link_token() == TOKEN
    assert len(protocol.new_token()) >= protocol.MIN_TOKEN_LENGTH


def test_the_hub_refuses_to_start_without_a_token():
    with pytest.raises(ValueError):
        LinkHub("")


# ---------------------------------------------------------------------- hub and client over real sockets
def test_messages_flow_both_ways(hub):
    got = []
    hub.on_message("dashboard", lambda role, msg: got.append((role, msg)))
    c = client(hub, ["dashboard"])
    assert wait_for(lambda: hub.connected("dashboard"))
    assert hub.send("dashboard", {"type": "js", "code": "hello()"})
    assert wait_for(lambda: c.received)
    assert c.received[0] == {"type": "js", "code": "hello()"}
    c.send({"type": "call", "method": "process_message", "args": ["hi"]})
    assert wait_for(lambda: got)
    assert got[0] == ("dashboard", {"type": "call", "method": "process_message", "args": ["hi"]})
    c.stop()
    assert wait_for(lambda: not hub.connected("dashboard"))


def test_a_wrong_token_is_turned_away_with_a_reason(hub):
    statuses = []
    c = client(hub, ["hands"], token="w" * 40, statuses=statuses)
    assert wait_for(lambda: statuses)
    assert statuses[0][0] is False and "MIKI_LINK_TOKEN" in statuses[0][1]
    assert not hub.connected("hands")
    c.stop()


def test_a_stranger_speaking_nonsense_gets_nothing(hub):
    import socket

    with socket.create_connection(("127.0.0.1", hub.port), timeout=3) as sock:
        sock.recv(4096)  # hello
        sock.sendall(b'{"type":"auth","roles":["root"],"mac":"x"}\n')
        assert sock.recv(4096) == b""  # closed, no welcome
    assert not hub.connected("hands") and not hub.connected("dashboard")


def test_the_state_sent_at_login_is_visible_to_the_brain(hub):
    c = client(hub, ["hands"], state=lambda: {"focus_armed": True})
    assert wait_for(lambda: hub.connected("hands"))
    assert hub.state("hands") == {"focus_armed": True}
    c.stop()


def test_a_newer_connection_takes_over_the_role(hub):
    first = client(hub, ["dashboard"])
    assert wait_for(lambda: hub.connected("dashboard"))
    second = client(hub, ["dashboard"])
    assert wait_for(lambda: second.connected)
    hub.send("dashboard", {"type": "js", "code": "x"})
    assert wait_for(lambda: second.received)
    assert not first.received
    first.stop()
    second.stop()


def test_the_client_reconnects_after_the_server_drops_it(hub):
    statuses = []
    c = client(hub, ["hands"], statuses=statuses)
    assert wait_for(lambda: hub.connected("hands"))
    hub._conns["hands"].close()  # the server side of the line breaks
    assert wait_for(lambda: any(not up for up, _ in statuses))
    assert wait_for(lambda: [up for up, _ in statuses].count(True) >= 2, seconds=8)
    c.stop()


# ---------------------------------------------------------------------- the dashboard across the link
class FakeApi:
    def __init__(self):
        self.calls = []
        self.repaints = 0

    def process_message(self, text):
        self.calls.append(("process_message", text))

    def open_mail(self, thread_id):
        self.calls.append(("open_mail", thread_id))

    def dismiss_mail(self, message_id):
        self.calls.append(("dismiss_mail", message_id))

    def dashboard_connected(self):
        self.repaints += 1

    def _handle_command(self, text):  # must never be reachable from the laptop
        self.calls.append(("_handle_command", text))


def test_the_dashboard_may_call_only_the_three_page_actions(hub):
    api = FakeApi()
    wire_dashboard(hub, api)
    c = client(hub, ["dashboard"])
    assert wait_for(lambda: api.repaints == 1)  # a new window gets painted
    c.send({"type": "call", "method": "_handle_command", "args": ["/forget everything"]})
    c.send({"type": "call", "method": "process_message", "args": [123]})
    c.send({"type": "call", "method": "process_message", "args": ["hello"]})
    c.send({"type": "repaint"})
    assert wait_for(lambda: api.calls and api.repaints == 2)
    assert api.calls == [("process_message", "hello")]
    c.stop()


def test_the_remote_window_forwards_javascript_and_links(hub):
    window = RemoteWindow(hub)
    assert not window.connected
    c = client(hub, ["dashboard"])
    assert wait_for(lambda: window.connected)
    window.evaluate_js("setBusy(true)")
    window.open_url("https://mail.google.com/mail/u/0/#all/abc123")
    assert wait_for(lambda: len(c.received) == 2)
    assert c.received[0] == {"type": "js", "code": "setBusy(true)"}
    c.stop()


def test_the_laptop_only_opens_gmail_links(monkeypatch):
    opened = []
    monkeypatch.setattr("app.interfaces.remote_gui.webbrowser.open", opened.append)

    class Window:
        def __init__(self):
            self.js = []

        def evaluate_js(self, code):
            self.js.append(code)

    win = Window()
    dash = RemoteDashboard(win, api=None)
    dash.on_message({"type": "open_url", "url": "https://mail.google.com/mail/u/0/#all/abc"})
    dash.on_message({"type": "open_url", "url": "file:///C:/Windows/System32/calc.exe"})
    dash.on_message({"type": "open_url", "url": "https://evil.example/https://mail.google.com/"})
    dash.on_message({"type": "js", "code": "renderFace()"})
    assert opened == ["https://mail.google.com/mail/u/0/#all/abc"]
    assert win.js == ["renderFace()"]


def test_the_dashboard_says_each_new_connection_problem_once():
    class Window:
        def __init__(self):
            self.js = []

        def evaluate_js(self, code):
            self.js.append(code)

    win = Window()
    dash = RemoteDashboard(win, api=None)
    for _ in range(3):
        dash.on_status(False, "Can't reach the server")
    assert sum("appendMessage" in code for code in win.js) == 1
    dash.on_status(True, "")
    dash.on_status(False, "Can't reach the server")
    dash.on_status(True, "")
    assert any("Connected to my brain again" in code for code in win.js)


# ---------------------------------------------------------------------- focus mode split over the link
_real_sleep = time.sleep


@pytest.fixture(autouse=True)
def quick_windows(monkeypatch):
    """Opening a (fake) Chrome window polls with sleeps; keep them short but real, the link threads need to run."""
    monkeypatch.setattr(service_module.time, "sleep", lambda s: _real_sleep(min(s, 0.01)))


def laptop(discord_open=True):
    desktop = FakeDesktop([window(50, "discord.exe", "Discord")] if discord_open else [])
    chrome = FakeChrome(running=False)
    counter = [100]

    def new_window(url):
        counter[0] += 1
        desktop.windows.append(Window(counter[0], FOCUS_CHROME_PID, "Google Gemini" if "gemini" in url else "Claude",
                                      "Chrome_WidgetWin_1", "chrome.exe"))

    chrome.on_open_window = new_window
    pet = FakePet()
    beeps = []
    agent = HandsAgent(FocusConfig(), beep=lambda: beeps.append(1))
    agent.desk = FocusDesk(agent.config, desktop=desktop, chrome=chrome, pet=pet,
                           banned_sites=lambda: agent._sites, banned_apps=lambda: agent._apps, on_bounce=agent._bounced)
    agent.beeps = beeps
    return agent, desktop, chrome, pet


def connect_laptop(hub, agent):
    link = LinkClient(("127.0.0.1", hub.port), TOKEN, ["hands"], on_message=agent.on_message, state=agent.state)
    agent.start(link)
    return link


def brain(hub, tmp_path):
    svc = FocusService(FocusConfig(state_path=tmp_path / "focus.json"), session=FocusSession(tmp_path / "focus.json"),
                       lock_name=None, hands=RemoteHands(hub))
    wire_focus(hub, svc)
    events = []
    svc.add_listener(events.append)
    return svc, events


def test_focus_from_the_phone_sets_up_the_laptop_screens(hub, tmp_path):
    svc, _ = brain(hub, tmp_path)
    agent, desktop, chrome, pet = laptop()
    connect_laptop(hub, agent)
    assert wait_for(lambda: hub.connected("hands"))

    reply = svc.start_focus(30)
    assert reply.ok, reply.text
    assert wait_for(lambda: agent.armed.is_set())
    assert [w[0] for w in chrome.windows_opened] == ["https://gemini.google.com/app", "https://claude.ai/new"]
    placed = [rect for _hwnd, rect in desktop.placed]
    assert placed == [PRIMARY.work, SECOND.work]
    assert 50 in desktop.minimized  # Discord tucked away on the laptop
    assert wait_for(lambda: any(c[0] == "timeline" for c in pet.calls))
    timeline = next(c for c in pet.calls if c[0] == "timeline")
    assert round(timeline[2] - timeline[1]) == 30 * 60
    assert ("start", PRIMARY.work) in pet.calls
    agent.stop()


def test_a_bounce_on_the_laptop_is_counted_by_the_brain(hub, tmp_path):
    svc, _ = brain(hub, tmp_path)
    agent, desktop, chrome, pet = laptop(discord_open=False)
    connect_laptop(hub, agent)
    assert wait_for(lambda: hub.connected("hands"))
    assert svc.start_focus(30).ok
    assert wait_for(lambda: agent.armed.is_set())

    steam = window(60, "steam.exe", "Steam")
    desktop.windows.append(steam)
    desktop.front = steam
    agent.tick()
    assert 60 in desktop.minimized
    assert wait_for(lambda: svc.session.round.distractions == 1)
    assert wait_for(lambda: any("Steam can wait" in text for text in pet.said()))  # the brain's reaction, shown on the laptop
    agent.stop()


def test_stopping_on_the_brain_stands_the_laptop_down(hub, tmp_path):
    svc, _ = brain(hub, tmp_path)
    agent, *_ = laptop()
    connect_laptop(hub, agent)
    assert wait_for(lambda: hub.connected("hands"))
    assert svc.start_focus(30).ok
    assert wait_for(lambda: agent.armed.is_set())
    assert svc.stop().ok
    assert wait_for(lambda: not agent.armed.is_set())
    agent.stop()


def test_a_ban_added_mid_round_reaches_the_laptop(hub, tmp_path):
    svc, _ = brain(hub, tmp_path)
    agent, *_ = laptop()
    connect_laptop(hub, agent)
    assert wait_for(lambda: hub.connected("hands"))
    assert svc.start_focus(30).ok
    assert wait_for(lambda: agent.armed.is_set())
    assert svc.ban("9anime.to").ok
    assert wait_for(lambda: "9anime.to" in agent._sites)
    agent.stop()


def test_no_laptop_means_a_clear_answer_and_no_round(hub, tmp_path):
    svc, _ = brain(hub, tmp_path)
    reply = svc.start_focus(30)
    assert not reply.ok and "laptop isn't connected" in reply.text
    assert not svc.session.is_active


def test_a_laptop_without_chrome_says_so(hub, tmp_path, monkeypatch):
    monkeypatch.setattr(desk_module, "find_chrome", lambda explicit=None: None)
    svc, _ = brain(hub, tmp_path)
    agent = HandsAgent(FocusConfig())
    agent.desk = FocusDesk(agent.config, desktop=FakeDesktop(), pet=FakePet(),
                           banned_sites=lambda: (), banned_apps=lambda: frozenset(), on_bounce=agent._bounced)
    connect_laptop(hub, agent)
    assert wait_for(lambda: hub.connected("hands"))
    reply = svc.start_focus(30)
    assert not reply.ok and "Chrome" in reply.text
    agent.stop()


def test_a_laptop_that_comes_back_mid_round_is_set_up_again(hub, tmp_path):
    svc, _ = brain(hub, tmp_path)
    first, *_ = laptop()
    link = connect_laptop(hub, first)
    assert wait_for(lambda: hub.connected("hands"))
    assert svc.start_focus(30).ok
    assert wait_for(lambda: first.armed.is_set())
    first.stop()  # the laptop restarted (a fresh agent that knows nothing)
    assert wait_for(lambda: not hub.connected("hands"))

    second, desktop, chrome, pet = laptop()
    connect_laptop(hub, second)
    assert wait_for(lambda: second.armed.is_set())
    assert chrome.windows_opened  # the screens were put back
    second.stop()


def test_a_round_that_ended_while_the_laptop_was_away_is_stood_down(hub, tmp_path):
    svc, _ = brain(hub, tmp_path)
    agent, _desktop, _chrome, pet = laptop()
    agent.armed.set()  # it was still guarding when it lost the brain
    agent.until = time.time() + 600
    connect_laptop(hub, agent)  # ...and the brain has no round running any more
    assert wait_for(lambda: not agent.armed.is_set())
    assert wait_for(lambda: ("stop",) in pet.calls)
    agent.stop()


def test_the_laptop_stops_guarding_by_itself_when_the_brain_goes_quiet():
    agent, desktop, *_ = laptop()
    agent.armed.set()
    agent.until = time.time() - GRACE_SECONDS - 1  # the round ended long ago and nobody said so
    steam = window(60, "steam.exe", "Steam")
    desktop.windows.append(steam)
    desktop.front = steam
    agent.tick()
    assert not agent.armed.is_set()
    assert 60 not in desktop.minimized  # nothing bounced after the round


def test_the_laptop_ignores_malformed_orders():
    agent, desktop, chrome, pet = laptop()
    agent.handle({"type": "focus.arm", "sites": "youtube.com", "apps": [], "until": time.time() + 60})
    agent.handle({"type": "focus.arm", "sites": [], "apps": ["notepad"], "until": time.time() + 60})  # apps must be .exe
    agent.handle({"type": "focus.pet", "op": "__del__"})
    assert not agent.armed.is_set() and not chrome.windows_opened and not pet.calls


def test_the_break_beep_and_the_pet_mood_travel_to_the_laptop(hub, tmp_path):
    svc, events = brain(hub, tmp_path)
    agent, _desktop, _chrome, pet = laptop()
    connect_laptop(hub, agent)
    assert wait_for(lambda: hub.connected("hands"))
    assert svc.start_focus(30).ok
    assert wait_for(lambda: agent.armed.is_set())
    svc.session._round.ends_at = time.time() - 1  # the round is over
    svc.tick_clock()
    assert wait_for(lambda: agent.beeps and not agent.armed.is_set())
    assert wait_for(lambda: "cheer" in pet.moods())
    assert any(e.kind == "time_up" for e in events)
    agent.stop()


# ---------------------------------------------------------------------- the ssh tunnel
def test_the_tunnel_command_never_prompts_and_only_listens_on_loopback():
    cmd = SshTunnel("miki@203.0.113.7", local_port=18765, remote_port=8765, ssh_port=2222, key="C:/k").command("ssh")
    assert cmd[0] == "ssh" and cmd[-1] == "miki@203.0.113.7"
    assert "BatchMode=yes" in cmd and "ExitOnForwardFailure=yes" in cmd
    assert "127.0.0.1:18765:127.0.0.1:8765" in cmd
    assert cmd[cmd.index("-p") + 1] == "2222" and cmd[cmd.index("-i") + 1] == "C:/k"


def test_ssh_errors_are_explained_in_plain_words():
    assert "refused the SSH key" in explain_ssh_error("miki@1.2.3.4: Permission denied (publickey).", "miki@1.2.3.4")
    assert "answer yes" in explain_ssh_error("Host key verification failed.", "miki@1.2.3.4")
    assert "online" in explain_ssh_error("ssh: Could not resolve hostname x", "miki@x")
