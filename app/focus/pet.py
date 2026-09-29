"""Miki's desktop pet: a tiny pixel-art Miki that shows how far through your focus round you are.

It stands on a slim progress bar along the bottom of your primary screen. The bar fills from the left edge to the
right edge over the round, so when Miki reaches the right corner, the round is done. It stays put (blinking now and
then) so it never pulls your eye, reacts only when you wander off, and a click opens Windows' snipping tool.

Runs as its own little process (``python -m app.focus.pet``) so it costs the dashboard nothing and works when
Miki runs headless. The parent talks to it with one JSON object per line on stdin; when the parent goes away the
pipe closes and the pet leaves too, so it can never be left behind.

    {"cmd": "timeline", "start": 1790000000.0, "end": 1790003600.0}   the round to draw (epoch seconds)
    {"cmd": "mood", "mood": "focus|sleep|alert|cheer|wave"}
    {"cmd": "say",  "text": "Back to work!", "seconds": 6}
    {"cmd": "info", "text": "42 min left"}        shown when you right-click the pet
    {"cmd": "quit"}

Left click: snipping tool (drag to capture a region; it lands on your clipboard). Right click: time left.

The window is a transparent strip (a colour key, so every empty pixel is click-through): only the pet itself
can be clicked, and it never takes keyboard focus.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import random
import sys
import threading
import time

# ------------------------------------------------------------------------------------------ art
SCALE = 4
KEY = "#fe01fe"  # the transparent colour key
PALETTE = {"O": "#ece8df", "B": "#1c1b17", "H": "#3a372e", "A": "#ff5b2e", "E": "#ff5b2e", "M": "#ff5b2e", "F": "#ff5b2e", "Z": "#ece8df"}

BODY = [
    ".......AA.......",
    ".......OO.......",
    "....OOOOOOOO....",
    "..OOBBBBBBBBOO..",
    ".OBBHHBBBBBBBBO.",
    ".OBBBBBBBBBBBBO.",
    ".OBBBBBBBBBBBBO.",
    ".OBBBBBBBBBBBBO.",
    ".OBBBBBBBBBBBBO.",
    ".OBBBBBBBBBBBBO.",
    ".OBBBBBBBBBBBBO.",
    "..OOBBBBBBBBOO..",
    "....OOOOOOOO....",
]
WIDTH, HEIGHT = 16, 15  # the body plus two rows of feet

# Drawn facing right; facing left is the mirror image.
EYE_X = (4, 10)  # left column of each eye
EYES = {
    "open": lambda c: [(c, 6), (c + 1, 6), (c, 7), (c + 1, 7), (c, 8), (c + 1, 8)],
    "blink": lambda c: [(c, 8), (c + 1, 8), (c + 2, 8)],
    "happy": lambda c: [(c + 1, 7), (c, 8), (c + 2, 8)],
    "sleep": lambda c: [(c, 8), (c + 1, 8), (c + 2, 8)],
    "alert": lambda c: [(c, 5), (c + 1, 5), (c, 6), (c + 1, 6), (c, 7), (c + 1, 7), (c, 8), (c + 1, 8)],
}
MOUTHS = {
    "open": [(6, 9), (9, 9), (7, 10), (8, 10)],
    "blink": [(6, 9), (9, 9), (7, 10), (8, 10)],
    "happy": [(6, 9), (7, 10), (8, 10), (9, 9), (7, 9), (8, 9)],
    "sleep": [(7, 10), (8, 10)],
    "alert": [(7, 9), (8, 9), (7, 10), (8, 10)],
}
# Foot blocks (left column, top row, width). "A"/"B": one foot lifted and swung forward.
FEET = {
    "stand": [(3, 13, 3, 2), (10, 13, 3, 2)],
    "A": [(4, 12, 3, 1), (10, 13, 3, 2)],
    "B": [(3, 13, 3, 2), (11, 12, 3, 1)],
}


def build_grid(feet: str, eyes: str) -> list[list[str]]:
    """The 16x15 sprite as a grid of palette letters ('.' = transparent), facing right."""
    grid = [list(row) for row in BODY] + [list("." * WIDTH) for _ in range(HEIGHT - len(BODY))]
    shift = 0 if eyes in {"sleep", "blink"} else 1  # look ahead while walking
    for c in EYE_X:
        for x, y in EYES[eyes](c + shift):
            grid[y][x] = "E"
    for x, y in MOUTHS[eyes]:
        grid[y][x + shift] = "M"
    for x, y, w, h in FEET[feet]:
        for dy in range(h):
            for dx in range(w):
                grid[y + dy][x + dx] = "F"
    return grid



# ------------------------------------------------------------------------------------------ the pet
BAR_TRACK = "#5a564a"
BAR_FILL = "#ff5b2e"
BAR_HEIGHT = 4
SNIP_URI = "ms-screenclip:"  # Windows' own "drag to capture" overlay (Win+Shift+S)


def open_snipping_tool() -> bool:
    """Start the snip overlay. Windows keeps what you capture on the clipboard and offers to save or edit it."""
    try:
        os.startfile(SNIP_URI)  # type: ignore[attr-defined]  # Windows only
        return True
    except (AttributeError, OSError):
        return False


def restore_foreground(previous: int, *, foreground, owner_pid, set_foreground, own_pid: int) -> bool:
    """The pet must never keep your keyboard: if one of its own windows ended up in front, give the focus back.

    ``previous`` is the window that was in front before the pet appeared. Nothing happens when somebody else (you, clicking
    on something) already has the front, or when the old window is gone. Returns True when the focus was handed back.
    """
    if not previous:
        return False
    front = foreground()
    if not front or owner_pid(front) != own_pid or owner_pid(previous) in (0, own_pid):
        return False
    set_foreground(previous)
    return True


def _hand_focus_back(previous: int) -> bool:
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        user32.GetForegroundWindow.restype = wintypes.HWND

        def owner_pid(hwnd) -> int:
            pid = wintypes.DWORD(0)
            user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(pid))
            return int(pid.value)

        return restore_foreground(
            previous, foreground=lambda: user32.GetForegroundWindow(), owner_pid=owner_pid,
            set_foreground=lambda hwnd: user32.SetForegroundWindow(wintypes.HWND(hwnd)), own_pid=os.getpid(),
        )
    except Exception:
        return False


def _foreground_now() -> int:
    if sys.platform != "win32":
        return 0
    try:
        import ctypes

        return int(ctypes.windll.user32.GetForegroundWindow() or 0)
    except Exception:
        return 0


def progress(now: float, start: float | None, end: float | None) -> float:
    """How far through the round we are, 0..1 (0 before a timeline arrives)."""
    if start is None or end is None or end <= start:
        return 0.0
    return min(1.0, max(0.0, (now - start) / (end - start)))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--x", type=int, required=True)
    parser.add_argument("--y", type=int, required=True)
    parser.add_argument("--w", type=int, required=True)
    parser.add_argument("--h", type=int, default=150)
    args = parser.parse_args(argv)

    if sys.platform == "win32":  # physical pixels, so the strip lines up with what the parent measured
        try:
            import ctypes

            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            pass

    previous_front = _foreground_now()  # whatever you were working in when the pet was asked for
    import tkinter as tk

    inbox: "queue.Queue[dict]" = queue.Queue()

    def read_stdin() -> None:
        try:
            for line in sys.stdin:
                try:
                    inbox.put(json.loads(line))
                except ValueError:
                    continue
        except Exception:
            pass
        inbox.put({"cmd": "quit"})  # the parent is gone (or closed the pipe)

    threading.Thread(target=read_stdin, daemon=True).start()

    root = tk.Tk()
    root.overrideredirect(True)
    root.geometry(f"{args.w}x{args.h}+{args.x}+{args.y}")
    root.configure(bg=KEY)
    root.attributes("-topmost", True)
    root.attributes("-transparentcolor", KEY)
    canvas = tk.Canvas(root, width=args.w, height=args.h, bg=KEY, highlightthickness=0, bd=0)
    canvas.pack()
    root.update_idletasks()
    _no_focus_no_taskbar(root)

    bar_y = args.h - BAR_HEIGHT
    floor = bar_y - 1  # the pet stands on the bar
    sprite_w = WIDTH * SCALE
    lo, hi = sprite_w // 2, args.w - sprite_w // 2
    frames: dict[tuple[str, str, int], "tk.PhotoImage"] = {}

    def frame(feet: str, eyes: str, facing: int) -> "tk.PhotoImage":
        key = (feet, eyes, facing)
        if key not in frames:
            grid = build_grid(feet, eyes)
            image = tk.PhotoImage(width=WIDTH, height=HEIGHT)
            for y, row in enumerate(grid):
                cells = row if facing > 0 else row[::-1]
                for x, letter in enumerate(cells):
                    if letter != ".":
                        image.put(PALETTE[letter], to=(x, y))
            frames[key] = image.zoom(SCALE)
        return frames[key]

    canvas.create_rectangle(0, bar_y + 1, args.w, args.h, fill=BAR_TRACK, width=0)  # the empty bar
    fill = canvas.create_rectangle(0, bar_y, 0, args.h, fill=BAR_FILL, width=0)  # how far you've come
    body = canvas.create_image(lo, floor, anchor="s", image=frame("stand", "open", 1))
    overlay: list[int] = []  # bubble / zzz items, cleared together

    state = {
        "mood": "focus", "start": None, "end": None, "info": "", "say_until": 0.0, "alert_until": 0.0,
        "blink_until": 0.0, "next_blink": time.monotonic() + 4, "quit_at": 0.0, "snip_at": 0.0, "shown": None,
    }

    def pet_x(now: float) -> float:
        return lo + progress(time.time(), state["start"], state["end"]) * (hi - lo)

    def clear_overlay() -> None:
        for item in overlay:
            canvas.delete(item)
        overlay.clear()

    def say(text: str, seconds: float = 6.0) -> None:
        clear_overlay()
        if not text:
            return
        px = pet_x(time.monotonic())
        cx = min(max(px, 150), args.w - 150)
        top = floor - HEIGHT * SCALE - 14
        label = canvas.create_text(cx, top - 10, text=text, font=("Segoe UI", 11, "bold"), fill="#1c1b17", width=250, justify="center", anchor="s")
        x1, y1, x2, y2 = canvas.bbox(label)
        pad = 9
        box = canvas.create_polygon(  # a chamfered "pixel" bubble
            x1 - pad + 4, y1 - pad, x2 + pad - 4, y1 - pad, x2 + pad, y1 - pad + 4, x2 + pad, y2 + pad - 4,
            x2 + pad - 4, y2 + pad, x1 - pad + 4, y2 + pad, x1 - pad, y2 + pad - 4, x1 - pad, y1 - pad + 4,
            fill="#ece8df", outline="#1c1b17", width=2,
        )
        tail_x = min(max(px, x1 + 14), x2 - 14)
        tail = canvas.create_polygon(tail_x - 7, y2 + pad, tail_x + 7, y2 + pad, tail_x, y2 + pad + 10, fill="#ece8df", outline="#1c1b17", width=2)
        cover = canvas.create_line(tail_x - 5, y2 + pad, tail_x + 5, y2 + pad, fill="#ece8df", width=3)
        canvas.tag_lower(box, label)
        overlay.extend([box, tail, cover, label])
        state["say_until"] = time.monotonic() + seconds

    def zzz(now: float) -> None:
        clear_overlay()
        px = pet_x(now)
        for i in range(int(now * 1.5) % 3 + 1):
            zx, zy = px + 24 + i * 12, floor - HEIGHT * SCALE - 4 - i * 12
            for dx, dy in ((0, 0), (1, 0), (2, 0), (2, 1), (1, 2), (0, 3), (0, 4), (1, 4), (2, 4)):
                overlay.append(canvas.create_rectangle(zx + dx * 3, zy + dy * 3, zx + dx * 3 + 3, zy + dy * 3 + 3, fill="#ece8df", width=0))

    def on_left_click(_event) -> None:
        now = time.monotonic()
        if now - state["snip_at"] < 2.0:  # one click, one snip
            return
        state["snip_at"] = now
        if not open_snipping_tool():
            say("I couldn't open the snipping tool.", 3)

    def on_right_click(_event) -> None:
        say(state["info"] or "I'm keeping you company.", 5)

    for sequence, handler in (("<Button-1>", on_left_click), ("<Button-3>", on_right_click)):
        canvas.tag_bind(body, sequence, handler)
    canvas.tag_bind(body, "<Enter>", lambda _e: canvas.configure(cursor="hand2"))
    canvas.tag_bind(body, "<Leave>", lambda _e: canvas.configure(cursor=""))

    def handle(message: dict) -> None:
        cmd = message.get("cmd")
        now = time.monotonic()
        if cmd == "timeline":
            try:
                state["start"], state["end"] = float(message["start"]), float(message["end"])
            except (KeyError, TypeError, ValueError):
                return
            state["mood"] = "focus"
            clear_overlay()
        elif cmd == "mood":
            mood = str(message.get("mood", "focus"))
            if mood == "alert":
                state["alert_until"] = now + 1.6
            else:
                state["mood"] = mood if mood in {"focus", "sleep", "cheer", "wave"} else "focus"
                if mood != "sleep":
                    clear_overlay()
        elif cmd == "say":
            say(str(message.get("text", ""))[:200], float(message.get("seconds", 6)))
        elif cmd == "info":
            state["info"] = str(message.get("text", ""))[:120]
        elif cmd == "quit" and not state["quit_at"]:
            state["mood"] = "wave"
            state["quit_at"] = now + 2.2
            say("See you!", 2.2)

    def tick() -> None:
        now = time.monotonic()
        try:
            while True:
                handle(inbox.get_nowait())
        except queue.Empty:
            pass
        if state["quit_at"] and now >= state["quit_at"]:
            root.destroy()
            return
        if state["say_until"] and now >= state["say_until"] and state["mood"] != "sleep":
            clear_overlay()
            state["say_until"] = 0.0

        mood, eyes, hop, shake = state["mood"], "open", 0.0, 0
        alerting = now < state["alert_until"]
        if alerting:
            eyes, shake = "alert", (4 if int(now * 18) % 2 else -4)
        elif mood == "sleep":
            eyes = "sleep"
            if not state["say_until"]:
                zzz(now)
        elif mood in {"cheer", "wave"}:
            eyes = "happy"
            hop = abs(((now * 2.2) % 1.0) * 2 - 1)
            hop = (1 - hop * hop) * 34
        else:  # focus: stand still, blink now and then
            if now >= state["next_blink"]:
                state["blink_until"] = now + 0.15
                state["next_blink"] = now + random.uniform(3.0, 7.0)
            if now < state["blink_until"]:
                eyes = "blink"

        x = pet_x(now)
        signature = (round(x), round(hop), shake, eyes, mood)
        if signature != state["shown"]:  # redraw only when something moved: the bar creeps about a pixel every few seconds
            state["shown"] = signature
            canvas.itemconfig(body, image=frame("A" if hop > 20 else "stand", eyes, 1))
            canvas.coords(body, x + shake, floor - hop)
            canvas.coords(fill, 0, bar_y, x, args.h)
        lively = alerting or mood in {"cheer", "wave"} or now < state["blink_until"]
        root.after(45 if lively else 250, tick)

    def keep_on_top() -> None:
        root.attributes("-topmost", True)
        root.after(5000, keep_on_top)

    def give_focus_back() -> None:
        # Windows activates a new window when it is first shown, before the "never activate" style can stop it.
        _hand_focus_back(previous_front)

    root.after(30, tick)
    root.after(5000, keep_on_top)
    for delay in (50, 300, 900, 2000):
        root.after(delay, give_focus_back)
    root.mainloop()
    return 0


def _no_focus_no_taskbar(root) -> None:
    """Never steal keyboard focus, never show in the taskbar or Alt-Tab."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        user32 = ctypes.windll.user32
        hwnd = user32.GetAncestor(root.winfo_id(), 2) or root.winfo_id()  # GA_ROOT
        style = user32.GetWindowLongW(hwnd, -20)
        user32.SetWindowLongW(hwnd, -20, style | 0x08000000 | 0x80)  # WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW
    except Exception:
        pass


if __name__ == "__main__":
    sys.exit(main())
