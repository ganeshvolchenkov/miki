import io
import json
import urllib.error
from pathlib import Path

from app.focus.chrome import FocusChrome, find_chrome


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeHttp:
    """Records requests and answers them from a table of path -> body."""

    def __init__(self, answers=None, fail=False):
        self.answers = answers or {}
        self.fail = fail
        self.requests = []

    def __call__(self, request, timeout=None):
        if self.fail:
            raise urllib.error.URLError("connection refused")
        path = request.full_url.split("127.0.0.1:", 1)[1].split("/", 1)[1]
        self.requests.append((request.get_method(), "/" + path))
        body = self.answers.get("/" + path, "")
        return Response((body if isinstance(body, str) else json.dumps(body)).encode())


def chrome(http, launcher=None, tmp="focus-profile"):
    return FocusChrome(Path("C:/fake/chrome.exe"), Path(tmp), port=9444, opener=http, launcher=launcher or (lambda *a, **k: None))


def test_lists_only_real_pages():
    http = FakeHttp({"/json/list": [
        {"id": "1", "type": "page", "url": "https://claude.ai/"},
        {"id": "2", "type": "service_worker", "url": "https://claude.ai/sw.js"},
        {"id": "3", "type": "background_page", "url": "chrome-extension://x"},
        {"id": "4", "type": "page", "url": "https://youtube.com/"},
    ]})
    tabs = chrome(http).tabs()
    assert [t.id for t in tabs] == ["1", "4"] and tabs[1].url == "https://youtube.com/"


def test_unreachable_chrome_means_no_tabs_not_an_error():
    c = chrome(FakeHttp(fail=True))
    assert c.tabs() == [] and not c.is_running()
    assert not c.close_tab("abc") and not c.open_tab("https://claude.ai")


def test_running_check():
    assert chrome(FakeHttp({"/json/version": {"Browser": "Chrome/140"}})).is_running()
    assert not chrome(FakeHttp({"/json/version": "nonsense"})).is_running()


def test_close_tab_uses_the_close_endpoint_and_refuses_odd_ids():
    http = FakeHttp()
    c = chrome(http)
    assert c.close_tab("A1B2-c3")
    assert http.requests == [("GET", "/json/close/A1B2-c3")]
    for bad in ("", "../version", "a/b", "x?y=1", "a b"):
        assert not c.close_tab(bad)
    assert len(http.requests) == 1


def test_open_tab_uses_put_and_encodes_the_url():
    http = FakeHttp()
    chrome(http).open_tab("https://claude.ai/new?a=b c")
    method, path = http.requests[0]
    assert method == "PUT" and path.startswith("/json/new?https://claude.ai/new?a=b%20c")


def test_open_window_command_line(tmp_path):
    launched = []
    c = chrome(FakeHttp(), lambda args, **kw: launched.append(args), tmp=str(tmp_path / "profile"))
    c.open_window("https://gemini.google.com/app", 0, 0, 2560, 1540)
    args = launched[0]
    assert args[0].endswith("chrome.exe") and args[-1] == "https://gemini.google.com/app"
    assert f"--user-data-dir={(tmp_path / 'profile').resolve()}" in args  # never the everyday profile
    assert "--remote-debugging-port=9444" in args
    assert "--remote-debugging-address=127.0.0.1" in args  # never reachable from the network
    assert "--window-position=0,0" in args and "--window-size=2560,1540" in args and "--new-window" in args
    assert (tmp_path / "profile").is_dir()


def test_find_chrome_prefers_an_explicit_path(tmp_path):
    exe = tmp_path / "my-chrome.exe"
    exe.write_bytes(b"")
    assert find_chrome(str(exe)) == exe
    assert find_chrome(str(tmp_path / "missing.exe")) != tmp_path / "missing.exe"
