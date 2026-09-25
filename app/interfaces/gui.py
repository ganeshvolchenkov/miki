from __future__ import annotations

import logging
import math
import random
import sys
import threading
import tkinter as tk
import tkinter.font as tkfont
from datetime import datetime, timedelta
from pathlib import Path
from tkinter import messagebox, scrolledtext
from typing import Any, Callable

from openai import OpenAI

from app.brain.embeddings import OpenAIEmbeddingClient
from app.brain.openai_client import OpenAIClient
from app.core.assistant import MikiCore
from app.core.config import Settings
from app.core.rag_wiring import build_rag_service
from app.core.storage import build_memory_manager, build_memory_store
from app.core.tool_wiring import build_tool_runner
from app.memory.conversation import JSONConversationStore
from app.interfaces.graph_view import GraphCard
from app.memory.obsidian import ObsidianMemoryStore
from app.tools.calendar.auth import CalendarUnavailable, run_oauth_flow

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Theme: warm amber-on-graphite "cockpit" palette -- deliberately not blue,
# not purple. The amber-on-dark instrument-panel look is a direct nod to the
# "command-center / cockpit" brief.
# ---------------------------------------------------------------------------
BG = "#0a0705"
PANEL = "#16110b"
PANEL_ALT = "#211a10"
BORDER_DIM = "#3d2e18"
ACCENT = "#ffb238"
ACCENT_BUSY = "#ffd479"
ACCENT_DIM = "#7a5420"
ACCENT_SOFT = "#e0b374"
TEXT = "#fbf3e6"
TEXT_DIM = "#8c7a61"
GREEN = "#8fd35a"
AMBER = "#ffcf6e"
RED = "#ff5f5f"

PLACEHOLDER_TEXT = "Ask Miki anything..."

_CHIP_DEFS: list[tuple[str, str, bool]] = [
    ("Memories", "/memories", False),
    ("Reviews", "/candidates", False),
    ("Search...", "/search ", True),
    ("Knowledge", "/rag", False),
    ("Stats", "/memory stats", False),
    ("Sync", "/sync", False),
    ("Obsidian", "/obsidian", False),
    ("Help", "/help", False),
]

# ---------------------------------------------------------------------------
# Bundled display font (Caprasimo -- SIL OFL, shipped in assets/fonts/).
# Registered as a private, process-local GDI font resource on Windows so it
# renders correctly without the user installing anything. Used sparingly --
# only for the "MIKI" wordmark -- never for body/UI text.
# ---------------------------------------------------------------------------
_ASSETS_DIR = Path(__file__).resolve().parent.parent.parent / "assets"
_CAPRASIMO_PATH = _ASSETS_DIR / "fonts" / "Caprasimo-Regular.ttf"
_display_font_load_attempted = False
_display_font_name: str | None = None


def _load_bundled_display_font() -> str | None:
    global _display_font_load_attempted, _display_font_name
    if _display_font_load_attempted:
        return _display_font_name
    _display_font_load_attempted = True

    if sys.platform == "win32" and _CAPRASIMO_PATH.exists():
        try:
            import ctypes

            FR_PRIVATE = 0x10
            added = ctypes.windll.gdi32.AddFontResourceExW(str(_CAPRASIMO_PATH), FR_PRIVATE, 0)
            if added > 0:
                _display_font_name = "Caprasimo"
        except Exception:
            logger.debug("Could not load bundled Caprasimo font", exc_info=True)

    return _display_font_name


def _pick_font(root: tk.Misc, candidates: list[str], fallback: str) -> str:
    try:
        available = set(tkfont.families(root))
    except tk.TclError:
        return fallback
    for name in candidates:
        if name in available:
            return name
    return fallback



def _apply_acrylic(root: tk.Tk) -> None:
    import platform
    if platform.release() == '10':
        try:
            import ctypes
            from ctypes import c_int, byref, sizeof, Structure, windll, POINTER
            
            class ACCENT_POLICY(Structure):
                _fields_ = [('AccentState', c_int), ('AccentFlags', c_int), ('GradientColor', c_int), ('AnimationId', c_int)]

            class WINDOWCOMPOSITIONATTRIBDATA(Structure):
                _fields_ = [('Attribute', c_int), ('Data', POINTER(ACCENT_POLICY)), ('SizeOfData', c_int)]

            hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
            policy = ACCENT_POLICY()
            policy.AccentState = 0 # ACCENT_ENABLE_ACRYLICBLURBEHIND
            # 60% opacity black: 0x99000000
            policy.GradientColor = 0x88000000
            
            data = WINDOWCOMPOSITIONATTRIBDATA()
            data.Attribute = 19
            data.Data = ctypes.pointer(policy)
            data.SizeOfData = ctypes.sizeof(policy)
            
            # disabled acrylic
        except Exception:
            pass

def _apply_windows_dark_titlebar(root: tk.Tk) -> None:
    if sys.platform != "win32":
        return
    try:
        import ctypes

        hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
        value = ctypes.c_int(1)
        for attribute in (20, 19):
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attribute, ctypes.byref(value), ctypes.sizeof(value))
    except Exception:
        logger.debug("Could not apply dark title bar", exc_info=True)


def _greeting() -> str:
    hour = datetime.now().hour
    if 5 <= hour < 12:
        return "Good morning."
    if 12 <= hour < 18:
        return "Good afternoon."
    if 18 <= hour < 22:
        return "Good evening."
    return "Still up? I'm here whenever you need me."


def _day_abbrev(date_str: str) -> str:
    try:
        return datetime.strptime(date_str, "%Y-%m-%d").strftime("%a").upper()
    except (ValueError, TypeError):
        return date_str[:3].upper() if date_str else "--"


from app.core.timeutil import local_utc_offset as _local_utc_offset  # noqa: E402


def _rounded_rect_points(x1: float, y1: float, x2: float, y2: float, radius: float) -> list[float]:
    radius = max(2.0, min(radius, (x2 - x1) / 2, (y2 - y1) / 2))
    return [
        x1 + radius, y1,
        x2 - radius, y1,
        x2, y1,
        x2, y1 + radius,
        x2, y2 - radius,
        x2, y2,
        x2 - radius, y2,
        x1 + radius, y2,
        x1, y2,
        x1, y2 - radius,
        x1, y1 + radius,
        x1, y1,
    ]


def _rounded_canvas(parent: tk.Widget, width: int, height: int, *, radius: float, fill: str, bg: str) -> tk.Canvas:
    canvas = tk.Canvas(parent, width=width, height=height, bg=bg, highlightthickness=0)
    canvas.create_polygon(_rounded_rect_points(1, 1, width - 1, height - 1, radius), smooth=True, fill=fill, outline="")
    return canvas


def _make_avatar(parent: tk.Widget, *, bg: str = PANEL) -> tk.Canvas:
    avatar = tk.Canvas(parent, width=22, height=22, bg=bg, highlightthickness=0)
    cx = cy = 11
    for r, color in ((10, ACCENT_DIM), (7, "#a3711f"), (4, ACCENT_BUSY)):
        avatar.create_oval(cx - r, cy - r, cx + r, cy + r, outline="", fill=color)
    return avatar


from app.interfaces.face import (  # noqa: F401  (pixel-face data lives in face.py)
    _EYE_BLINK,
    _EYE_HAPPY,
    _EYE_LOOK_LEFT,
    _EYE_LOOK_RIGHT,
    _EYE_L_LEFT,
    _EYE_L_TOP,
    _EYE_OPEN,
    _EYE_R_LEFT,
    _EYE_R_TOP,
    _EYE_WIDE,
    _FACE_BODY,
    _FACE_BODY_COLOR,
    _FACE_GRID_H,
    _FACE_GRID_W,
    _FACE_HIGHLIGHT_COLOR,
    _FACE_SEQUENCES,
    _FACE_STATE_BOUNCE,
    _FACE_STATE_EYE_COLOR,
    _MOUTH_CLOSED,
    _MOUTH_LEFT,
    _MOUTH_SMALL,
    _MOUTH_TOP,
    _MOUTH_WIDE,
    _SPEAKING_MOUTH_WEIGHTS,
    _SUPER_IDLE_SEQUENCE,
    _compose_face_frame,
    _random_speaking_frame,
)

def _lerp_color(color_a: str, color_b: str, t: float) -> str:
    t = max(0.0, min(1.0, t))
    r1, g1, b1 = int(color_a[1:3], 16), int(color_a[3:5], 16), int(color_a[5:7], 16)
    r2, g2, b2 = int(color_b[1:3], 16), int(color_b[3:5], 16), int(color_b[5:7], 16)
    r = round(r1 + (r2 - r1) * t)
    g = round(g1 + (g2 - g1) * t)
    b = round(b1 + (b2 - b1) * t)
    return f"#{r:02x}{g:02x}{b:02x}"



class ScrollableFrame(tk.Frame):
    def __init__(self, container, bg=BG, *args, **kwargs):
        super().__init__(container, bg=bg, *args, **kwargs)
        self.canvas = tk.Canvas(self, bg=bg, highlightthickness=0)
        self.scrollable_frame = tk.Frame(self.canvas, bg=bg)
        
        self.scrollable_frame.bind(
            "<Configure>",
            lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        )
        self.canvas.bind(
            "<Configure>",
            lambda e: self.canvas.itemconfig(self.window_id, width=e.width)
        )
        
        self.window_id = self.canvas.create_window((0, 0), window=self.scrollable_frame, anchor="nw", tags="frame")
        self.canvas.pack(side="left", fill="both", expand=True)
        
        # Windows mouse wheel support
        self.bind_all("<MouseWheel>", self._on_mousewheel, add="+")

    def _on_mousewheel(self, event):
        x, y = self.winfo_pointerxy()
        widget = self.winfo_containing(x, y)
        if widget and str(widget).startswith(str(self)):
            self.canvas.yview_scroll(int(-1*(event.delta/120)), "units")

class PixelFace(tk.Canvas):
    """Miki's animated mascot -- a small pixel-art blob with glowing eyes
    (and a mouth while speaking). Same state interface as the presence
    indicator it replaces: idle / listening / thinking / speaking.

    Two bits of personality beyond the basic per-state loops:
    - "speaking" flaps its mouth through randomized shapes/timing (not a
      fixed cycle) with a pulsing eye glow, closer to real talking rhythm.
    - a one-shot "super idle" animation (wink, look around) plays after
      SUPER_IDLE_DELAY_MS of uninterrupted idle, then hands back to the
      normal idle loop -- and the clock restarts, so it can repeat.
    """

    SUPER_IDLE_DELAY_MS = 10_000

    def __init__(self, master: tk.Misc, *, pixel_size: int = 14, bg: str = BG) -> None:
        width = _FACE_GRID_W * pixel_size
        height = _FACE_GRID_H * pixel_size
        super().__init__(master, width=width, height=height, bg=bg, highlightthickness=0)
        self.pixel_size = pixel_size
        self.state = "idle"
        self._sequence = _FACE_SEQUENCES["idle"]
        self._frame_index = 0
        self._current_frame = self._sequence[0]
        self._frame_elapsed = 0.0
        self._idle_elapsed_ms = 0
        self._playing_super_idle = False
        self._phase = 0.0
        self._stopped = False
        self._tick_ms = 60
        self._redraw()
        self.after(self._tick_ms, self._tick)

    def set_state(self, state: str) -> None:
        if state == self.state or state not in _FACE_SEQUENCES:
            return
        self.state = state
        self._sequence = _FACE_SEQUENCES[state]
        self._frame_index = 0
        self._frame_elapsed = 0.0
        self._idle_elapsed_ms = 0
        self._playing_super_idle = False
        self._current_frame = _random_speaking_frame() if state == "speaking" else self._sequence[0]

    def set_busy(self, busy: bool) -> None:
        """Back-compat convenience: maps the old boolean busy flag onto the
        thinking/idle states."""
        self.set_state("thinking" if busy else "idle")

    def stop(self) -> None:
        self._stopped = True

    def _tick(self) -> None:
        if self._stopped or not self.winfo_exists():
            return

        amplitude, speed = _FACE_STATE_BOUNCE.get(self.state, (0.06, 2.0))
        self._phase = (self._phase + speed * (self._tick_ms / 1000)) % (2 * math.pi)

        if self.state == "idle" and not self._playing_super_idle:
            self._idle_elapsed_ms += self._tick_ms
            if self._idle_elapsed_ms >= self.SUPER_IDLE_DELAY_MS:
                self._playing_super_idle = True
                self._frame_index = 0
                self._frame_elapsed = 0.0
                self._current_frame = _SUPER_IDLE_SEQUENCE[0]

        self._frame_elapsed += self._tick_ms
        _, _, _, duration = self._current_frame
        if self._frame_elapsed >= duration:
            self._frame_elapsed = 0.0
            if self.state == "speaking":
                self._current_frame = _random_speaking_frame()
            elif self._playing_super_idle:
                self._frame_index += 1
                if self._frame_index >= len(_SUPER_IDLE_SEQUENCE):
                    self._playing_super_idle = False
                    self._idle_elapsed_ms = 0
                    self._frame_index = 0
                    self._current_frame = _FACE_SEQUENCES["idle"][0]
                else:
                    self._current_frame = _SUPER_IDLE_SEQUENCE[self._frame_index]
            else:
                self._frame_index = (self._frame_index + 1) % len(self._sequence)
                self._current_frame = self._sequence[self._frame_index]

        self._redraw()
        self.after(self._tick_ms, self._tick)

    def _redraw(self) -> None:
        self.delete("all")
        eye_l, eye_r, mouth, _ = self._current_frame
        grid = _compose_face_frame(eye_l, eye_r, mouth)

        amplitude, _ = _FACE_STATE_BOUNCE.get(self.state, (0.06, 2.0))
        bounce = math.sin(self._phase) * amplitude * self.pixel_size * 3

        eye_color = _FACE_STATE_EYE_COLOR.get(self.state, ACCENT)
        if self.state == "speaking":
            pulse = (math.sin(self._phase * 2) + 1) / 2
            eye_color = _lerp_color(ACCENT, ACCENT_BUSY, pulse)

        ps = self.pixel_size

        for row_index, row in enumerate(grid):
            for col_index, cell in enumerate(row):
                if cell == ".":
                    continue
                color = {"B": _FACE_BODY_COLOR, "H": _FACE_HIGHLIGHT_COLOR, "E": eye_color, "M": eye_color}.get(cell)
                if color is None:
                    continue
                x0 = col_index * ps
                y0 = row_index * ps + bounce
                self.create_rectangle(x0, y0, x0 + ps, y0 + ps, outline="", fill=color)


def _describe_memory_storage(memory_store, memory) -> str:
    if isinstance(memory_store, ObsidianMemoryStore):
        note_path = memory_store.get_note_path(memory.memory_id)
        if note_path is not None:
            try:
                relative = note_path.relative_to(memory_store.vault_path)
            except ValueError:
                relative = note_path
            return f"Obsidian note ({relative.as_posix()}, id {memory.memory_id})"
        return f"Obsidian vault ({memory_store.vault_path.name})"

    file_path = getattr(memory_store, "file_path", None)
    if file_path is not None:
        return f"local JSON store ({file_path})"

    return type(memory_store).__name__


def _describe_rag_status(core: MikiCore) -> str:
    if core.rag_service is None:
        return "disabled"
    if not core.last_memory_rag_indexed:
        return "not indexed (RAG error -- check logs)"
    status = core.rag_service.status()
    return f"indexed for retrieval ({status.embedding_model})"


def _format_startup_summary(memory_store, core: MikiCore) -> str:
    obsidian_connected = isinstance(memory_store, ObsidianMemoryStore) and memory_store.is_connected()
    backend = "Obsidian" if isinstance(memory_store, ObsidianMemoryStore) else "local JSON"
    intelligence = "on" if getattr(core.memory_manager, "evaluator", None) is not None else "legacy"
    rag_state = "on" if core.rag_service is not None else "off"
    tool_names = ", ".join(tool.name for tool in core.list_tools()) or "none"
    return (
        f"Ready. Backend: {backend} · Obsidian: {'linked' if obsidian_connected else 'offline'} · "
        f"RAG: {rag_state} · Memory AI: {intelligence} · Tools: {tool_names}"
    )


class MikiChatWindow:
    def __init__(self, root: tk.Tk, core: MikiCore, memory_store, settings: Settings, *, startup_summary: str) -> None:
        self.root = root
        self.core = core
        self.memory_store = memory_store
        self.settings = settings
        self.root.title("MIKI — Command Center")
        self.root.geometry("1440x920")
        self.root.minsize(1180, 760)
        self.root.configure(bg="#000000")
        _apply_acrylic(self.root)

        self.display_font = _load_bundled_display_font() or _pick_font(root, ["Segoe UI Black", "Arial Black"], "Segoe UI")
        self.mono_font = _pick_font(root, ["Cascadia Mono", "Consolas", "Courier New"], "Consolas")
        self.ui_font = _pick_font(root, ["Segoe UI Semibold", "Segoe UI", "Arial"], "Segoe UI")

        self.voice_state_var = tk.StringVar(value="Ready to help.")
        self.voice_caption_var = tk.StringVar(value="")
        self.input_var = tk.StringVar()
        self.calendar_status_var = tk.StringVar(value="Checking...")
        self.calendar_date_var = tk.StringVar(value=datetime.now().strftime("%A, %b %d"))
        self._calendar_view_mode = "today"

        self._mode = "voice"
        self._history_visible = False
        self._busy = False
        self._placeholder_active = False
        self._bubble_rows: list[tk.Frame] = []
        self._log_width = 600
        self._typing_job: str | None = None
        self._typing_range: tuple[str, str] | None = None
        self._typing_canvas: tk.Canvas | None = None
        self._typing_dots: list[int] = []
        self._typing_frame_index = 0
        self._send_pill = None
        self._send_label = None
        self._speaking_job: str | None = None

        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._set_placeholder()
        self._refresh_status_bar()
        self._refresh_calendar_panel()
        self._refresh_weather_panel()
        self._refresh_mail_panel()
        self._refresh_drive_panel()
        self._show_startup_summary(startup_summary)
        self.input_entry.focus_set()

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        container = tk.Frame(self.root, bg="#000000")
        container.pack(fill="both", expand=True, padx=18, pady=18)

        self._build_top_bar(container)

        body = tk.Frame(container, bg=BG)
        body.pack(fill="both", expand=True, pady=(12, 12))

        left_col_container = tk.Frame(body, bg=BG, width=300)
        left_col_container.pack(side="left", fill="y", padx=(0, 12))
        left_col_container.pack_propagate(False)
        left_col_scroll = ScrollableFrame(left_col_container, bg=BG)
        left_col_scroll.pack(fill="both", expand=True)
        left_col = left_col_scroll.scrollable_frame
        #
        
        
        self._build_left_column(left_col)

        right_col_container = tk.Frame(body, bg=BG, width=300)
        right_col_container.pack(side="right", fill="y", padx=(12, 0))
        right_col_container.pack_propagate(False)
        right_col_scroll = ScrollableFrame(right_col_container, bg=BG)
        right_col_scroll.pack(fill="both", expand=True)
        right_col = right_col_scroll.scrollable_frame
        #
        
        
        self._build_right_column(right_col)

        self.center_zone = tk.Frame(body, bg=BG)
        self.center_zone.pack(side="left", fill="both", expand=True)
        self._build_voice_frame(self.center_zone)
        self._build_chat_frame(self.center_zone)

        self._build_bottom_bar(container)
        self._build_history_overlay(container)

    # -- Top bar ----------------------------------------------------------

    def _build_top_bar(self, parent: tk.Widget) -> None:
        bar = tk.Frame(parent, bg=PANEL, highlightthickness=0, )
        bar.pack(fill="x")

        row = tk.Frame(bar, bg=PANEL)
        row.pack(fill="x", padx=16, pady=10)

        tk.Label(row, text="MIKI", bg=PANEL, fg=ACCENT, font=(self.display_font, 22)).pack(side="left", padx=(0, 20))

        self._build_mode_toggle(row)

        history_btn = self._build_pill_button(row, "History", self._toggle_history_panel)
        history_btn.pack(side="right", padx=(10, 0))

        self._build_status_strip(row)

    def _build_mode_toggle(self, parent: tk.Widget) -> None:
        wrap = tk.Frame(parent, bg=PANEL)
        wrap.pack(side="left")

        self.voice_pill_canvas, self._voice_pill, self._voice_pill_label = self._build_toggle_pill(wrap, "VOICE", lambda: self._set_mode("voice"))
        self.voice_pill_canvas.pack(side="left", padx=(0, 6))

        self.chat_pill_canvas, self._chat_pill, self._chat_pill_label = self._build_toggle_pill(wrap, "CHAT", lambda: self._set_mode("chat"))
        self.chat_pill_canvas.pack(side="left")

        self._update_mode_toggle_visuals()

    def _build_toggle_pill(self, parent: tk.Widget, label: str, on_click: Callable[[], None]) -> tuple[tk.Canvas, int, int]:
        font = (self.ui_font, 9, "bold")
        measure = tk.Label(parent, text=label, font=font)
        measure.update_idletasks()
        w = measure.winfo_reqwidth() + 26
        h = measure.winfo_reqheight() + 12
        measure.destroy()

        canvas = tk.Canvas(parent, width=w, height=h, bg=PANEL, highlightthickness=0, cursor="hand2")
        pill = canvas.create_polygon(_rounded_rect_points(1, 1, w - 1, h - 1, h / 2), smooth=True, fill=PANEL_ALT, outline="")
        label_id = canvas.create_text(w / 2, h / 2, text=label, fill=ACCENT_SOFT, font=font)
        canvas.bind("<Button-1>", lambda _event: on_click())
        return canvas, pill, label_id

    def _update_mode_toggle_visuals(self) -> None:
        voice_active = self._mode == "voice"
        self.voice_pill_canvas.itemconfigure(self._voice_pill, fill=ACCENT if voice_active else PANEL_ALT)
        self.voice_pill_canvas.itemconfigure(self._voice_pill_label, fill=BG if voice_active else ACCENT_SOFT)
        self.chat_pill_canvas.itemconfigure(self._chat_pill, fill=ACCENT if not voice_active else PANEL_ALT)
        self.chat_pill_canvas.itemconfigure(self._chat_pill_label, fill=BG if not voice_active else ACCENT_SOFT)

    def _build_pill_button(self, parent: tk.Widget, label: str, on_click: Callable[[], None]) -> tk.Canvas:
        font = (self.ui_font, 9, "bold")
        measure = tk.Label(parent, text=label, font=font)
        measure.update_idletasks()
        w = measure.winfo_reqwidth() + 24
        h = measure.winfo_reqheight() + 12
        measure.destroy()

        canvas = tk.Canvas(parent, width=w, height=h, bg=PANEL, highlightthickness=0, cursor="hand2")
        pill = canvas.create_polygon(_rounded_rect_points(1, 1, w - 1, h - 1, h / 2), smooth=True, fill=PANEL_ALT, outline="")
        label_id = canvas.create_text(w / 2, h / 2, text=label, fill=ACCENT_SOFT, font=font)

        def on_enter(_event: object) -> None:
            canvas.itemconfigure(pill, fill=ACCENT_DIM)
            canvas.itemconfigure(label_id, fill=TEXT)

        def on_leave(_event: object) -> None:
            canvas.itemconfigure(pill, fill=PANEL_ALT)
            canvas.itemconfigure(label_id, fill=ACCENT_SOFT)

        canvas.bind("<Enter>", on_enter)
        canvas.bind("<Leave>", on_leave)
        canvas.bind("<Button-1>", lambda _event: on_click())
        return canvas

    def _build_status_strip(self, parent: tk.Widget) -> None:
        row = tk.Frame(parent, bg=PANEL)
        row.pack(side="right", padx=(0, 14))
        self.memory_dot, self.memory_var = self._make_status_chip(row, "MEMORY --")
        self.rag_dot, self.rag_var = self._make_status_chip(row, "RAG --")
        self.obsidian_dot, self.obsidian_var = self._make_status_chip(row, "OBSIDIAN --")

    def _make_status_chip(self, parent: tk.Widget, label: str) -> tuple[tk.Canvas, tk.StringVar]:
        group = tk.Frame(parent, bg=PANEL)
        group.pack(side="left", padx=(12, 0))
        dot = tk.Canvas(group, width=8, height=8, bg=PANEL, highlightthickness=0)
        dot.create_oval(1, 1, 7, 7, fill="#556677", outline="", tags="dot")
        dot.pack(side="left", padx=(0, 5))
        var = tk.StringVar(value=label)
        tk.Label(group, textvariable=var, bg=PANEL, fg=TEXT_DIM, font=(self.ui_font, 10)).pack(side="left")
        return dot, var

    def _set_dot(self, dot: tk.Canvas, color: str) -> None:
        dot.itemconfigure("dot", fill=color)

    # -- Voice / Chat mode ------------------------------------------------

    def _build_voice_frame(self, parent: tk.Widget) -> None:
        self.voice_frame = tk.Frame(parent, bg=BG)
        self.voice_frame.pack(fill="both", expand=True)

        tk.Frame(self.voice_frame, bg=BG).pack(fill="both", expand=True)

        face_wrap = tk.Frame(self.voice_frame, bg=BG)
        face_wrap.pack()
        self.face = PixelFace(face_wrap, pixel_size=15, bg=BG)
        self.face.pack()

        tk.Label(self.voice_frame, textvariable=self.voice_state_var, bg=BG, fg=ACCENT_SOFT, font=(self.ui_font, 13)).pack(pady=(20, 0))
        tk.Label(
            self.voice_frame, textvariable=self.voice_caption_var, bg=BG, fg=TEXT,
            font=(self.ui_font, 11), wraplength=560, justify="center",
        ).pack(pady=(10, 0), padx=40)

        mic_wrap = tk.Frame(self.voice_frame, bg=BG)
        mic_wrap.pack(pady=(24, 0))
        self.mic_button = self._build_mic_button(mic_wrap)
        self.mic_button.pack()

        tk.Label(self.voice_frame, text="Tap to speak, or type below", bg=BG, fg=TEXT_DIM, font=(self.ui_font, 11)).pack(pady=(10, 0))

        tk.Frame(self.voice_frame, bg=BG).pack(fill="both", expand=True)

    def _build_mic_button(self, parent: tk.Widget) -> tk.Canvas:
        size = 68
        canvas = tk.Canvas(parent, width=size, height=size, bg=BG, highlightthickness=0, cursor="hand2")
        self._mic_circle = canvas.create_oval(2, 2, size - 2, size - 2, fill=ACCENT_DIM, outline=ACCENT, width=2)
        self._mic_label = canvas.create_text(size / 2, size / 2, text="TALK", fill=TEXT, font=(self.ui_font, 12, "bold"))

        def on_enter(_event: object) -> None:
            if not self._busy:
                canvas.itemconfigure(self._mic_circle, fill=ACCENT)
                canvas.itemconfigure(self._mic_label, fill=BG)

        def on_leave(_event: object) -> None:
            if not self._busy:
                canvas.itemconfigure(self._mic_circle, fill=ACCENT_DIM)
                canvas.itemconfigure(self._mic_label, fill=TEXT)

        canvas.bind("<Enter>", on_enter)
        canvas.bind("<Leave>", on_leave)
        canvas.bind("<Button-1>", lambda _event: self._on_mic_click())
        return canvas

    def _on_mic_click(self) -> None:
        if self._busy:
            return
        self.face.set_state("listening")
        self.voice_state_var.set("Listening... (voice input is coming soon -- type below for now)")
        self.root.after(1400, self._end_mic_placeholder)

    def _end_mic_placeholder(self) -> None:
        if self._busy or self.face.state != "listening":
            return
        self.face.set_state("idle")
        self.voice_state_var.set("Ready to help.")
        self.input_entry.focus_set()

    def _build_chat_frame(self, parent: tk.Widget) -> None:
        self.chat_frame = tk.Frame(parent, bg=BG)
        # Not packed here -- voice mode is the default; _set_mode() shows it.

        log_wrap = tk.Frame(self.chat_frame, bg=BORDER_DIM, highlightthickness=0, )
        log_wrap.pack(fill="both", expand=True)

        self.chat_log = scrolledtext.ScrolledText(
            log_wrap, wrap="word", bg=PANEL, fg=TEXT, insertbackground=ACCENT,
            font=(self.ui_font, 14), relief="flat", padx=20, pady=20, bd=0,
        )
        self.chat_log.pack(fill="both", expand=True, padx=0, pady=0)
        self.chat_log.configure(state="disabled")
        self.chat_log.frame.configure(bg=PANEL)
        self.chat_log.vbar.configure(
            bg=PANEL_ALT, troughcolor=BG, activebackground=ACCENT_DIM, relief="flat", bd=0, width=10,
        )
        self.chat_log.bind("<Configure>", self._on_log_configure)

    def _set_mode(self, mode: str) -> None:
        if mode == self._mode:
            return
        self._mode = mode
        if mode == "voice":
            self.chat_frame.pack_forget()
            self.voice_frame.pack(fill="both", expand=True)
        else:
            self.voice_frame.pack_forget()
            self.chat_frame.pack(fill="both", expand=True)
        self._update_mode_toggle_visuals()

    # -- History overlay ----------------------------------------------------

    def _build_history_overlay(self, parent: tk.Widget) -> None:
        self.history_overlay = tk.Frame(parent, bg=PANEL, highlightthickness=0, highlightbackground=ACCENT_DIM)
        # Not placed -- _toggle_history_panel() places it on demand.

        header = tk.Frame(self.history_overlay, bg=PANEL)
        header.pack(fill="x", padx=16, pady=(14, 8))
        tk.Label(header, text="CONVERSATION HISTORY", bg=PANEL, fg=ACCENT, font=(self.mono_font, 10, "bold")).pack(side="left")
        close_btn = self._build_pill_button(header, "Close", self._toggle_history_panel)
        close_btn.pack(side="right")

        tk.Frame(self.history_overlay, bg=BORDER_DIM, height=1).pack(fill="x", padx=16)

        body_wrap = tk.Frame(self.history_overlay, bg=PANEL)
        body_wrap.pack(fill="both", expand=True, padx=16, pady=12)

        self.history_text = scrolledtext.ScrolledText(
            body_wrap, wrap="word", bg=PANEL_ALT, fg=TEXT, relief="flat", bd=0, font=(self.ui_font, 14),
        )
        self.history_text.pack(fill="both", expand=True)
        self.history_text.configure(state="disabled")
        self.history_text.vbar.configure(bg=PANEL, troughcolor=PANEL_ALT, activebackground=ACCENT_DIM, relief="flat", bd=0, width=10)

    def _toggle_history_panel(self) -> None:
        if self._history_visible:
            self.history_overlay.place_forget()
            self._history_visible = False
            return
        self._render_history_overlay()
        self.history_overlay.place(relx=0.5, rely=0.5, anchor="center", relwidth=0.55, relheight=0.7)
        self._history_visible = True

    def _render_history_overlay(self) -> None:
        messages = self.core.conversation_store.read_messages()
        self.history_text.configure(state="normal")
        self.history_text.delete("1.0", "end")
        if not messages:
            self.history_text.insert("end", "No conversation yet.")
        else:
            for message in messages:
                role = "You" if message.get("role") == "user" else "Miki"
                self.history_text.insert("end", f"{role}: {message.get('content', '')}\n\n")
        self.history_text.configure(state="disabled")

    # -- Left / Right widget columns ----------------------------------------

    def _build_left_column(self, parent: tk.Widget) -> None:
        self._build_weather_card(parent)
        self._build_calendar_card(parent)
        self._build_maps_card(parent)

    def _build_right_column(self, parent: tk.Widget) -> None:
        self.graph_card = GraphCard(
            parent, self.root,
            str(self.settings.obsidian_vault_path) if self.settings.obsidian_vault_path else None,
            mono_font=self.mono_font, ui_font=self.ui_font,
        )
        self._build_mail_card(parent)
        self._build_drive_card(parent)

    def _build_placeholder_card(self, parent: tk.Widget, *, title: str, icon: str, lines: list[str]) -> None:
        card = tk.Frame(parent, bg=PANEL, highlightthickness=0, )
        card.pack(side="bottom", fill="x", pady=(10, 0))

        title_bar = tk.Frame(card, bg=PANEL)
        title_bar.pack(fill="x", padx=14, pady=(10, 4))
        tk.Label(title_bar, text=title, bg=PANEL, fg=TEXT_DIM, font=(self.mono_font, 9, "bold")).pack(side="left")
        tk.Label(title_bar, text="SOON", bg=PANEL_ALT, fg=TEXT_DIM, font=(self.ui_font, 7, "bold"), padx=6, pady=1).pack(side="right")

        body = tk.Frame(card, bg=PANEL)
        body.pack(fill="x", padx=14, pady=(0, 10))
        tk.Label(body, text=icon, bg=PANEL, fg=TEXT_DIM, font=(self.ui_font, 18)).pack(side="left", padx=(0, 10))
        text_col = tk.Frame(body, bg=PANEL)
        text_col.pack(side="left", fill="x", expand=True)
        for line in lines:
            tk.Label(text_col, text=line, bg=PANEL, fg=TEXT_DIM, font=(self.ui_font, 10)).pack(anchor="w")

    # ------------------------------------------------------------------
    # Calendar Card (works)
    # ------------------------------------------------------------------
    def _build_calendar_card(self, parent: tk.Widget) -> None:
        card = tk.Frame(parent, bg=PANEL, highlightthickness=0, )
        card.pack(side="top", fill="both", expand=True, pady=(0, 10))

        title_bar = tk.Frame(card, bg=PANEL)
        title_bar.pack(fill="x", padx=14, pady=(12, 4))

        tk.Label(title_bar, text="CALENDAR", bg=PANEL, fg=ACCENT, font=(self.mono_font, 9, "bold")).pack(side="left")

        self.calendar_dot = tk.Canvas(title_bar, width=8, height=8, bg=PANEL, highlightthickness=0)
        self.calendar_dot.create_oval(1, 1, 7, 7, fill=TEXT_DIM, outline="", tags="dot")
        self.calendar_dot.pack(side="right")

        sub_bar = tk.Frame(card, bg=PANEL)
        sub_bar.pack(fill="x", padx=14, pady=(0, 8))

        tk.Label(sub_bar, textvariable=self.calendar_date_var, bg=PANEL, fg=TEXT, font=(self.ui_font, 12, "bold")).pack(anchor="w")
        tk.Label(sub_bar, textvariable=self.calendar_status_var, bg=PANEL, fg=TEXT_DIM, font=(self.ui_font, 10)).pack(anchor="w")

        mode_bar = tk.Frame(card, bg=PANEL)
        mode_bar.pack(fill="x", padx=14, pady=(0, 10))

        self.btn_today = self._make_sidebar_btn(mode_bar, "Today", lambda: self._set_calendar_mode("today"))
        self.btn_today.pack(side="left", padx=(0, 4))

        self.btn_tomorrow = self._make_sidebar_btn(mode_bar, "Tomorrow", lambda: self._set_calendar_mode("tomorrow"))
        self.btn_tomorrow.pack(side="left", padx=(0, 4))

        self.btn_refresh = self._make_sidebar_btn(mode_bar, "Refresh", lambda: self._refresh_calendar_panel())
        self.btn_refresh.pack(side="left")

        tk.Frame(card, bg=BORDER_DIM, height=1).pack(fill="x", padx=14, pady=(0, 10))

        events_wrap = tk.Frame(card, bg=PANEL)
        events_wrap.pack(fill="both", expand=True, padx=14, pady=(0, 10))

        self.events_scroll = scrolledtext.ScrolledText(
            events_wrap, wrap="word", bg=PANEL, relief="flat", bd=0, highlightthickness=0,
        )
        self.events_scroll.pack(fill="both", expand=True)
        self.events_scroll.configure(state="disabled")
        self.events_scroll.vbar.configure(
            bg=PANEL_ALT, troughcolor=PANEL, activebackground=ACCENT_DIM, relief="flat", bd=0, width=8,
        )

        self.events_list_frame = tk.Frame(self.events_scroll, bg=PANEL)
        self.events_scroll.window_create("1.0", window=self.events_list_frame)

        chips_frame = tk.Frame(card, bg=PANEL)
        chips_frame.pack(fill="x", padx=14, pady=10)

        tk.Label(chips_frame, text="QUICK ACTIONS", bg=PANEL, fg=TEXT_DIM, font=(self.ui_font, 8, "bold")).pack(anchor="w", pady=(0, 4))

        quick_queries = [
            ("What do I have today?", "What do I have today?"),
            ("Am I free tomorrow?", "Am I free tomorrow afternoon?"),
            ("Connect Calendar", "/calendar connect"),
        ]

        for lbl, q in quick_queries:
            btn = tk.Label(chips_frame, text=f"• {lbl}", bg=PANEL_ALT, fg=ACCENT_SOFT, font=(self.ui_font, 10), anchor="w", cursor="hand2", padx=8, pady=4)
            btn.pack(fill="x", pady=2)
            btn.bind("<Enter>", lambda e, b=btn: b.configure(bg=ACCENT_DIM, fg=TEXT))
            btn.bind("<Leave>", lambda e, b=btn: b.configure(bg=PANEL_ALT, fg=ACCENT_SOFT))
            btn.bind("<Button-1>", lambda e, query=q: self._trigger_quick_query(query))

    def _make_sidebar_btn(self, parent: tk.Widget, label: str, command: Callable[[], None]) -> tk.Label:
        lbl = tk.Label(parent, text=label, bg=PANEL_ALT, fg=TEXT, font=(self.ui_font, 8, "bold"), cursor="hand2", padx=8, pady=3)
        lbl.bind("<Enter>", lambda _e: lbl.configure(bg=ACCENT_DIM))
        lbl.bind("<Leave>", lambda _e: lbl.configure(bg=PANEL_ALT))
        lbl.bind("<Button-1>", lambda _e: command())
        return lbl

    def _set_calendar_mode(self, mode: str) -> None:
        self._calendar_view_mode = mode
        self._refresh_calendar_panel()

    def _trigger_quick_query(self, query: str) -> None:
        if self._busy:
            return
        self._placeholder_active = False
        self.input_entry.configure(fg=TEXT)
        self.input_var.set(query)
        self._on_send()

    def _refresh_calendar_panel(self) -> None:
        tool = self.core.get_tool("calendar")
        if tool is None or not tool.is_available():
            self.calendar_status_var.set("Not Connected")
            self._set_dot(self.calendar_dot, RED)
            self._render_calendar_placeholder("Google Calendar not connected.\nRun /calendar connect to link.")
            return

        self.calendar_status_var.set("Connected")
        self._set_dot(self.calendar_dot, GREEN)

        mode = getattr(self, "_calendar_view_mode", "today")
        now = datetime.now()
        if mode == "tomorrow":
            target_date = now + timedelta(days=1)
        else:
            target_date = now

        self.calendar_date_var.set(target_date.strftime("%A, %b %d"))

        offset = _local_utc_offset()
        start_iso = target_date.strftime("%Y-%m-%dT00:00:00") + offset
        end_iso = target_date.strftime("%Y-%m-%dT23:59:59") + offset

        def fetch() -> None:
            try:
                res = tool.execute("get_events", {"start": start_iso, "end": end_iso})
                events = (res.data or {}).get("events", []) if res.success else []
                self.root.after(0, lambda: self._render_calendar_events(events))
            except Exception as err:
                logger.warning("Calendar refresh failed: %s", err)
                self.root.after(0, lambda: self._render_calendar_placeholder("Could not load events."))

        threading.Thread(target=fetch, daemon=True).start()

    def _render_calendar_placeholder(self, text: str) -> None:
        for child in self.events_list_frame.winfo_children():
            child.destroy()

        card = tk.Frame(self.events_list_frame, bg=PANEL_ALT, padx=12, pady=12)
        card.pack(fill="x", pady=4)
        tk.Label(card, text=text, bg=PANEL_ALT, fg=TEXT_DIM, font=(self.ui_font, 11), justify="left", wraplength=260).pack(anchor="w")

    def _render_calendar_events(self, events: list[dict[str, Any]]) -> None:
        for child in self.events_list_frame.winfo_children():
            child.destroy()

        if not events:
            card = tk.Frame(self.events_list_frame, bg=PANEL_ALT, padx=12, pady=12)
            card.pack(fill="x", pady=4)
            tk.Label(card, text="No events scheduled", bg=PANEL_ALT, fg=ACCENT_SOFT, font=(self.ui_font, 11, "bold")).pack(anchor="w")
            tk.Label(card, text="Your schedule is clear for this day!", bg=PANEL_ALT, fg=TEXT_DIM, font=(self.ui_font, 10)).pack(anchor="w", pady=(2, 0))
            return

        for evt in events:
            card = tk.Frame(self.events_list_frame, bg=PANEL_ALT, highlightthickness=0, )
            card.pack(fill="x", pady=4, padx=2)

            stripe = tk.Frame(card, bg=ACCENT, width=3)
            stripe.pack(side="left", fill="y")

            content = tk.Frame(card, bg=PANEL_ALT, padx=10, pady=8)
            content.pack(side="left", fill="both", expand=True)

            start_raw = evt.get("start", "")
            end_raw = evt.get("end", "")
            all_day = evt.get("all_day", False)

            if all_day:
                time_str = "All Day"
            else:
                t_start = start_raw[11:16] if len(start_raw) >= 16 else start_raw
                t_end = end_raw[11:16] if len(end_raw) >= 16 else end_raw
                time_str = f"{t_start} - {t_end}" if t_start and t_end else (t_start or "Scheduled")

            tk.Label(content, text=time_str, bg=PANEL_ALT, fg=ACCENT, font=(self.mono_font, 9, "bold")).pack(anchor="w")
            tk.Label(content, text=evt.get("title", "(untitled)"), bg=PANEL_ALT, fg=TEXT, font=(self.ui_font, 11, "bold"), wraplength=250, justify="left").pack(anchor="w", pady=(2, 0))

            if evt.get("location"):
                tk.Label(content, text=f"@ {evt['location']}", bg=PANEL_ALT, fg=TEXT_DIM, font=(self.ui_font, 10), wraplength=250, justify="left").pack(anchor="w", pady=(2, 0))

    # ------------------------------------------------------------------
    # Weather Card (works)
    # ------------------------------------------------------------------

    def _build_mail_card(self, parent: tk.Widget) -> None:
        card = tk.Frame(parent, bg=PANEL, highlightthickness=0, )
        card.pack(fill="x", pady=(0, 12))
        
        header = tk.Frame(card, bg=PANEL)
        header.pack(fill="x", padx=12, pady=(12, 4))
        tk.Label(header, text="✉", bg=PANEL, fg=TEXT_DIM, font=(self.ui_font, 14)).pack(side="left", padx=(0, 8))
        tk.Label(header, text="MAIL", bg=PANEL, fg=TEXT_DIM, font=(self.ui_font, 12, "bold")).pack(side="left")
        
        self.mail_body = tk.Frame(card, bg=PANEL)
        self.mail_body.pack(fill="x", padx=12, pady=(0, 12))
        tk.Label(self.mail_body, text="Loading emails...", bg=PANEL, fg=TEXT_DIM, font=(self.ui_font, 11)).pack(anchor="w")

    def _refresh_mail_panel(self) -> None:
        def fetch():
            try:
                tool = self.core.get_tool("mail")
                if not tool: return
                res = tool.execute("list_emails", {"max_results": 3, "query": "is:unread"})
                def update():
                    for w in self.mail_body.winfo_children(): w.destroy()
                    if not res.success or not res.data.get("emails"):
                        tk.Label(self.mail_body, text="Inbox Zero!", bg=PANEL, fg=GREEN, font=(self.ui_font, 11)).pack(anchor="w")
                        return
                    for mail in res.data["emails"]:
                        tk.Label(self.mail_body, text=f"• {mail.get('sender', '')[:15]}", bg=PANEL, fg=TEXT, font=(self.ui_font, 11, "bold")).pack(anchor="w")
                        tk.Label(self.mail_body, text=mail.get("subject", "")[:30], bg=PANEL, fg=TEXT_DIM, font=(self.ui_font, 11)).pack(anchor="w")
                self.root.after(0, update)
            except Exception:
                pass
        threading.Thread(target=fetch, daemon=True).start()

    def _build_drive_card(self, parent: tk.Widget) -> None:
        card = tk.Frame(parent, bg=PANEL, highlightthickness=0, )
        card.pack(fill="x", pady=(0, 12))
        
        header = tk.Frame(card, bg=PANEL)
        header.pack(fill="x", padx=12, pady=(12, 4))
        tk.Label(header, text="☁", bg=PANEL, fg=TEXT_DIM, font=(self.ui_font, 14)).pack(side="left", padx=(0, 8))
        tk.Label(header, text="DRIVE", bg=PANEL, fg=TEXT_DIM, font=(self.ui_font, 12, "bold")).pack(side="left")
        
        self.drive_body = tk.Frame(card, bg=PANEL)
        self.drive_body.pack(fill="x", padx=12, pady=(0, 12))
        tk.Label(self.drive_body, text="Loading files...", bg=PANEL, fg=TEXT_DIM, font=(self.ui_font, 11)).pack(anchor="w")

    def _refresh_drive_panel(self) -> None:
        def fetch():
            try:
                tool = self.core.get_tool("drive")
                if not tool: return
                res = tool.execute("search_files", {"max_results": 3})
                def update():
                    for w in self.drive_body.winfo_children(): w.destroy()
                    if not res.success or not res.data.get("files"):
                        tk.Label(self.drive_body, text="No recent files", bg=PANEL, fg=TEXT_DIM, font=(self.ui_font, 11)).pack(anchor="w")
                        return
                    for file in res.data["files"]:
                        tk.Label(self.drive_body, text=f"📄 {file.get('name', '')[:25]}", bg=PANEL, fg=TEXT, font=(self.ui_font, 11)).pack(anchor="w")
                self.root.after(0, update)
            except Exception:
                pass
        threading.Thread(target=fetch, daemon=True).start()

    def _build_maps_card(self, parent: tk.Widget) -> None:
        card = tk.Frame(parent, bg=PANEL, highlightthickness=0, )
        card.pack(fill="x", pady=(0, 12))
        header = tk.Frame(card, bg=PANEL)
        header.pack(fill="x", padx=12, pady=(12, 4))
        tk.Label(header, text="🗺", bg=PANEL, fg=TEXT_DIM, font=(self.ui_font, 14)).pack(side="left", padx=(0, 8))
        tk.Label(header, text="MAPS", bg=PANEL, fg=TEXT_DIM, font=(self.ui_font, 12, "bold")).pack(side="left")
        
        body = tk.Frame(card, bg=PANEL)
        body.pack(fill="x", padx=12, pady=(0, 12))
        tk.Label(body, text="📍 Amsterdam Centraal", bg=PANEL, fg=ACCENT, font=(self.ui_font, 11, "bold")).pack(anchor="w")
        tk.Label(body, text="Ready for directions", bg=PANEL, fg=TEXT_DIM, font=(self.ui_font, 11)).pack(anchor="w")

    def _build_weather_card(self, parent: tk.Widget) -> None:
        card = tk.Frame(parent, bg=PANEL, highlightthickness=0, )
        card.pack(side="bottom", fill="x")

        title_bar = tk.Frame(card, bg=PANEL)
        title_bar.pack(fill="x", padx=14, pady=(12, 4))
        tk.Label(title_bar, text="WEATHER", bg=PANEL, fg=ACCENT, font=(self.mono_font, 9, "bold")).pack(side="left")
        self.weather_dot = tk.Canvas(title_bar, width=8, height=8, bg=PANEL, highlightthickness=0)
        self.weather_dot.create_oval(1, 1, 7, 7, fill=TEXT_DIM, outline="", tags="dot")
        self.weather_dot.pack(side="right")

        location_row = tk.Frame(card, bg=PANEL)
        location_row.pack(fill="x", padx=14, pady=(0, 8))
        self.weather_location_var = tk.StringVar(value=self.settings.weather_default_location)
        location_entry = tk.Entry(
            location_row, textvariable=self.weather_location_var, bg=PANEL_ALT, fg=TEXT,
            insertbackground=ACCENT, relief="flat", font=(self.ui_font, 11),
        )
        location_entry.pack(side="left", fill="x", expand=True, ipady=4, padx=(0, 6))
        location_entry.bind("<Return>", lambda _event: self._refresh_weather_panel())
        self._make_sidebar_btn(location_row, "Go", self._refresh_weather_panel).pack(side="left")

        tk.Frame(card, bg=BORDER_DIM, height=1).pack(fill="x", padx=14, pady=(0, 8))

        current_row = tk.Frame(card, bg=PANEL)
        current_row.pack(fill="x", padx=14, pady=(0, 10))

        self.weather_symbol_var = tk.StringVar(value="?")
        tk.Label(current_row, textvariable=self.weather_symbol_var, bg=PANEL, fg=ACCENT, font=(self.ui_font, 26)).pack(side="left", padx=(0, 10))

        text_col = tk.Frame(current_row, bg=PANEL)
        text_col.pack(side="left", fill="x", expand=True)
        self.weather_temp_var = tk.StringVar(value="--°C")
        tk.Label(text_col, textvariable=self.weather_temp_var, bg=PANEL, fg=TEXT, font=(self.ui_font, 18, "bold")).pack(anchor="w")
        self.weather_condition_var = tk.StringVar(value="Loading...")
        tk.Label(text_col, textvariable=self.weather_condition_var, bg=PANEL, fg=ACCENT_SOFT, font=(self.ui_font, 11)).pack(anchor="w")
        self.weather_detail_var = tk.StringVar(value="")
        tk.Label(text_col, textvariable=self.weather_detail_var, bg=PANEL, fg=TEXT_DIM, font=(self.ui_font, 10)).pack(anchor="w")

        self.weather_daily_row = tk.Frame(card, bg=PANEL)
        self.weather_daily_row.pack(fill="x", padx=14, pady=(0, 8))

        self.weather_status_var = tk.StringVar(value="")
        tk.Label(card, textvariable=self.weather_status_var, bg=PANEL, fg=TEXT_DIM, font=(self.ui_font, 7)).pack(anchor="w", padx=14, pady=(0, 10))

    def _refresh_weather_panel(self) -> None:
        tool = self.core.get_tool("weather")
        if tool is None:
            self.weather_status_var.set("Weather tool not enabled.")
            self.weather_condition_var.set("Not enabled")
            self._set_dot(self.weather_dot, TEXT_DIM)
            return

        location = self.weather_location_var.get().strip() or None
        self.weather_status_var.set("Refreshing...")

        def fetch() -> None:
            current_args = {"location": location} if location else {}
            daily_args = {"location": location, "days": 3} if location else {"days": 3}
            current_result = tool.execute("get_current_weather", current_args)
            daily_result = tool.execute("get_daily_forecast", daily_args)
            self.root.after(0, lambda: self._apply_weather_results(current_result, daily_result))

        threading.Thread(target=fetch, daemon=True).start()

    def _apply_weather_results(self, current_result, daily_result) -> None:
        for child in self.weather_daily_row.winfo_children():
            child.destroy()

        if not current_result.success:
            self.weather_status_var.set(f"Unavailable: {current_result.message}")
            self.weather_condition_var.set("Weather unavailable")
            self.weather_temp_var.set("--°C")
            self.weather_detail_var.set("")
            self.weather_symbol_var.set("?")
            self._set_dot(self.weather_dot, RED)
            return

        current = current_result.data["current"]
        self._set_dot(self.weather_dot, GREEN)
        self.weather_symbol_var.set(current.get("condition_symbol") or "?")
        temperature = current.get("temperature")
        self.weather_temp_var.set(f"{temperature:.0f}°C" if temperature is not None else "--°C")
        self.weather_condition_var.set(current.get("condition") or "")

        detail_bits = []
        feels_like = current.get("feels_like")
        if feels_like is not None:
            detail_bits.append(f"feels {feels_like:.0f}°C")
        precipitation = current.get("precipitation_probability")
        if precipitation is not None:
            detail_bits.append(f"rain {precipitation:.0f}%")
        wind = current.get("wind_speed")
        if wind is not None:
            detail_bits.append(f"wind {wind:.0f} km/h")
        self.weather_detail_var.set(" · ".join(detail_bits))
        self.weather_status_var.set(f"{current_result.data['location']} · updated just now")

        if daily_result.success:
            for day in daily_result.data["daily"][:3]:
                self._render_weather_day_card(day)

    def _render_weather_day_card(self, day: dict[str, Any]) -> None:
        card = tk.Frame(self.weather_daily_row, bg=PANEL_ALT)
        card.pack(side="left", fill="x", expand=True, padx=(0, 6), ipady=6)
        tk.Label(card, text=_day_abbrev(day.get("date", "")), bg=PANEL_ALT, fg=TEXT_DIM, font=(self.ui_font, 7, "bold")).pack()
        tk.Label(card, text=day.get("condition_symbol") or "?", bg=PANEL_ALT, fg=ACCENT, font=(self.ui_font, 13)).pack()
        high, low = day.get("temperature_high"), day.get("temperature_low")
        temps = f"{high:.0f}°/{low:.0f}°" if high is not None and low is not None else "--"
        tk.Label(card, text=temps, bg=PANEL_ALT, fg=TEXT, font=(self.ui_font, 8, "bold")).pack()

    # ------------------------------------------------------------------
    # Bottom bar: music placeholder + chip row + input
    # ------------------------------------------------------------------
    def _build_bottom_bar(self, parent: tk.Widget) -> None:
        bottom = tk.Frame(parent, bg=BG)
        bottom.pack(fill="x")

        chip_row = tk.Frame(bottom, bg=BG)
        chip_row.pack(fill="x", pady=(0, 8))
        for label, cmd, needs_arg in _CHIP_DEFS:
            self._make_chip(chip_row, label, lambda c=cmd, a=needs_arg: self._on_chip(c, a))

        input_row = tk.Frame(bottom, bg=BG)
        input_row.pack(fill="x")

        self._build_music_bar(input_row)

        self.input_wrap = tk.Frame(input_row, bg=BORDER_DIM)
        self.input_wrap.pack(side="left", fill="x", expand=True, padx=(0, 10))

        self.input_entry = tk.Entry(
            self.input_wrap, textvariable=self.input_var, bg=PANEL, fg=TEXT,
            insertbackground=ACCENT, relief="flat", font=(self.mono_font, 11), bd=0,
        )
        self.input_entry.pack(fill="x", expand=True, ipady=9, padx=2, pady=2)
        self.input_entry.bind("<Return>", lambda _event: self._on_send())
        self.input_entry.bind("<FocusIn>", self._on_entry_focus_in)
        self.input_entry.bind("<FocusOut>", self._on_entry_focus_out)

        self.send_button = self._build_send_control(input_row)
        self.send_button.pack(side="right")

    def _build_music_bar(self, parent: tk.Widget) -> None:
        bar = tk.Frame(parent, bg=PANEL, highlightthickness=0, )
        bar.pack(side="left", padx=(0, 10))

        tk.Label(bar, text="♪", bg=PANEL, fg=TEXT_DIM, font=(self.ui_font, 14)).pack(side="left", padx=(12, 8), pady=8)

        text_col = tk.Frame(bar, bg=PANEL)
        text_col.pack(side="left", padx=(0, 10))
        tk.Label(text_col, text="Nothing playing", bg=PANEL, fg=TEXT_DIM, font=(self.ui_font, 11, "bold")).pack(anchor="w")
        tk.Label(text_col, text="Not connected", bg=PANEL, fg=TEXT_DIM, font=(self.ui_font, 7)).pack(anchor="w")

        controls = tk.Frame(bar, bg=PANEL)
        controls.pack(side="left", padx=(0, 12))
        for symbol in ("<<", "▶", ">>"):
            tk.Label(controls, text=symbol, bg=PANEL, fg=TEXT_DIM, font=(self.ui_font, 11, "bold")).pack(side="left", padx=3)

    def _make_chip(self, parent: tk.Widget, label: str, on_click: Callable[[], None]) -> None:
        font = (self.ui_font, 9, "bold")
        measure = tk.Label(parent, text=label, font=font)
        measure.update_idletasks()
        w = measure.winfo_reqwidth() + 24
        h = measure.winfo_reqheight() + 12
        measure.destroy()

        canvas = tk.Canvas(parent, width=w, height=h, bg=BG, highlightthickness=0, cursor="hand2")
        pill = canvas.create_polygon(_rounded_rect_points(1, 1, w - 1, h - 1, h / 2), smooth=True, fill=PANEL_ALT, outline="")
        label_id = canvas.create_text(w / 2, h / 2, text=label, fill=ACCENT_SOFT, font=font)
        canvas.pack(side="left", padx=(0, 6))

        def on_enter(_event: object) -> None:
            canvas.itemconfigure(pill, fill=ACCENT_DIM)
            canvas.itemconfigure(label_id, fill=TEXT)

        def on_leave(_event: object) -> None:
            canvas.itemconfigure(pill, fill=PANEL_ALT)
            canvas.itemconfigure(label_id, fill=ACCENT_SOFT)

        canvas.bind("<Enter>", on_enter)
        canvas.bind("<Leave>", on_leave)
        canvas.bind("<Button-1>", lambda _event: on_click())

    def _build_send_control(self, parent: tk.Widget) -> tk.Canvas:
        label = "Send"
        font = (self.ui_font, 10, "bold")
        measure = tk.Label(parent, text=label, font=font)
        measure.update_idletasks()
        w = measure.winfo_reqwidth() + 46
        measure.destroy()
        h = 42

        canvas = tk.Canvas(parent, width=w, height=h, bg=BG, highlightthickness=0, cursor="hand2")
        self._send_pill = canvas.create_polygon(_rounded_rect_points(1, 1, w - 1, h - 1, h / 2), smooth=True, fill=ACCENT_DIM, outline="")
        self._send_label = canvas.create_text(w / 2, h / 2, text=label, fill=TEXT, font=font)

        def on_enter(_event: object) -> None:
            if not self._busy:
                canvas.itemconfigure(self._send_pill, fill=ACCENT)
                canvas.itemconfigure(self._send_label, fill=BG)

        def on_leave(_event: object) -> None:
            if not self._busy:
                canvas.itemconfigure(self._send_pill, fill=ACCENT_DIM)
                canvas.itemconfigure(self._send_label, fill=TEXT)

        canvas.bind("<Enter>", on_enter)
        canvas.bind("<Leave>", on_leave)
        canvas.bind("<Button-1>", lambda _event: self._on_send())
        return canvas

    # ------------------------------------------------------------------
    # Chat log (used by Chat mode; still receives every message in Voice
    # mode too, so switching modes never loses history)
    # ------------------------------------------------------------------
    def _on_log_configure(self, event: tk.Event) -> None:
        width = event.width
        if width <= 1 or width == self._log_width:
            return
        self._log_width = width
        for row in self._bubble_rows:
            try:
                row.configure(width=width)
            except tk.TclError:
                pass

    def _wrap_row(self, build) -> tk.Frame:
        row = tk.Frame(self.chat_log, bg=PANEL)
        build(row)
        row.update_idletasks()
        height = row.winfo_reqheight()
        row.configure(width=self._log_width, height=height)
        row.pack_propagate(False)
        return row

    def _append_row(self, row: tk.Frame) -> tuple[str, str]:
        self._bubble_rows.append(row)
        self.chat_log.configure(state="normal")
        start_index = self.chat_log.index("end-1c")
        self.chat_log.window_create("end", window=row)
        self.chat_log.insert("end", "\n")
        self.chat_log.see("end")
        self.chat_log.configure(state="disabled")
        end_index = self.chat_log.index("end-1c")
        return start_index, end_index

    def _build_bubble_row(self, text: str, *, side: str, bubble_fill: str, text_color: str, show_avatar: bool = False) -> tk.Frame:
        def build(row: tk.Frame) -> None:
            max_width = max(240, int(self._log_width * 0.65))
            holder = tk.Frame(row, bg=PANEL)
            holder.pack(side=side, padx=10, pady=3)

            if show_avatar:
                _make_avatar(holder).pack(side="left", padx=(0, 6), anchor="n")

            bubble_col = tk.Frame(holder, bg=PANEL)
            bubble_col.pack(side="left")

            measure = tk.Label(bubble_col, text=text, font=(self.ui_font, 14), wraplength=max_width - 26, justify="left")
            measure.update_idletasks()
            w = min(max_width, measure.winfo_reqwidth() + 26)
            h = measure.winfo_reqheight() + 18
            measure.destroy()

            canvas = _rounded_canvas(bubble_col, w, h, radius=14, fill=bubble_fill, bg=PANEL)
            canvas.create_text(13, h / 2, anchor="w", text=text, fill=text_color, font=(self.ui_font, 14), width=w - 26, justify="left")
            canvas.pack()

            tk.Label(
                bubble_col, text=datetime.now().strftime("%H:%M"), bg=PANEL, fg=TEXT_DIM, font=(self.ui_font, 10),
            ).pack(anchor="e" if side == "right" else "w", pady=(2, 0))

        return self._wrap_row(build)

    def _build_notice_row(self, text: str, *, accent_color: str) -> tk.Frame:
        def build(row: tk.Frame) -> None:
            max_width = max(260, int(self._log_width * 0.75))
            holder = tk.Frame(row, bg=PANEL)
            holder.pack(anchor="center", pady=4)

            card = tk.Frame(holder, bg=PANEL_ALT)
            card.pack()
            tk.Frame(card, bg=accent_color, width=3).pack(side="left", fill="y")
            tk.Label(
                card, text=text, bg=PANEL_ALT, fg=TEXT, font=(self.ui_font, 11),
                justify="left", wraplength=max_width, padx=10, pady=7,
            ).pack(side="left")

        return self._wrap_row(build)

    def _append_system(self, text: str) -> None:
        self._append_row(self._build_notice_row(text, accent_color=ACCENT_DIM))

    def _append_user(self, text: str) -> None:
        self._append_row(self._build_bubble_row(text, side="right", bubble_fill=ACCENT, text_color=BG))

    def _append_assistant(self, text: str) -> None:
        self._append_row(self._build_bubble_row(text, side="left", bubble_fill=PANEL_ALT, text_color=TEXT, show_avatar=True))

    def _append_error(self, text: str) -> None:
        self._append_row(self._build_notice_row(text, accent_color=RED))

    def _append_memory(self, text: str) -> None:
        self._append_row(self._build_notice_row(text, accent_color=GREEN))

    def _append_candidate(self, text: str) -> None:
        self._append_row(self._build_notice_row(text, accent_color=AMBER))

    def _show_startup_summary(self, summary: str) -> None:
        def reveal() -> None:
            if self.root.winfo_exists():
                self._append_system(summary)

        self.root.after(300, reveal)

    # ------------------------------------------------------------------
    # Typing indicator
    # ------------------------------------------------------------------
    def _show_typing_indicator(self) -> None:
        def build(row: tk.Frame) -> None:
            holder = tk.Frame(row, bg=PANEL)
            holder.pack(side="left", padx=10, pady=3)
            _make_avatar(holder).pack(side="left", padx=(0, 6), anchor="n")

            canvas = _rounded_canvas(holder, 56, 32, radius=14, fill=PANEL_ALT, bg=PANEL)
            canvas.pack(side="left")
            dots = [canvas.create_oval(12 + i * 14, 12, 19 + i * 14, 19, fill=TEXT_DIM, outline="") for i in range(3)]

            self._typing_canvas = canvas
            self._typing_dots = dots

        row = self._wrap_row(build)
        self._typing_range = self._append_row(row)
        self._typing_frame_index = 0
        self._animate_typing_dots()

    def _animate_typing_dots(self) -> None:
        if self._typing_canvas is None:
            return
        active = self._typing_frame_index % 3
        try:
            for i, dot in enumerate(self._typing_dots):
                self._typing_canvas.itemconfigure(dot, fill=ACCENT_BUSY if i == active else TEXT_DIM)
        except tk.TclError:
            return
        self._typing_frame_index += 1
        self._typing_job = self.root.after(260, self._animate_typing_dots)

    def _hide_typing_indicator(self) -> None:
        if self._typing_job is not None:
            try:
                self.root.after_cancel(self._typing_job)
            except tk.TclError:
                pass
            self._typing_job = None
        if self._typing_range is not None:
            start, end = self._typing_range
            try:
                self.chat_log.configure(state="normal")
                self.chat_log.delete(start, end)
                self.chat_log.configure(state="disabled")
            except tk.TclError:
                pass
            self._typing_range = None
        self._typing_canvas = None
        self._typing_dots = []
        self._bubble_rows = [row for row in self._bubble_rows if self._widget_alive(row)]

    @staticmethod
    def _widget_alive(widget: tk.Widget) -> bool:
        try:
            return bool(widget.winfo_exists())
        except tk.TclError:
            return False

    # ------------------------------------------------------------------
    # Input helpers
    # ------------------------------------------------------------------
    def _set_placeholder(self) -> None:
        self._placeholder_active = True
        self.input_var.set(PLACEHOLDER_TEXT)
        self.input_entry.configure(fg=TEXT_DIM)

    def _on_entry_focus_in(self, _event: object) -> None:
        self.input_wrap.configure(bg=ACCENT)
        if self._placeholder_active:
            self._placeholder_active = False
            self.input_var.set("")
            self.input_entry.configure(fg=TEXT)
        if not self._busy:
            self.voice_state_var.set("Go ahead, I'm listening...")

    def _on_entry_focus_out(self, _event: object) -> None:
        self.input_wrap.configure(bg=BORDER_DIM)
        if not self.input_var.get().strip():
            self._set_placeholder()
        if not self._busy:
            self.voice_state_var.set("Ready to help.")

    def _on_chip(self, cmd: str, needs_arg: bool) -> None:
        if self._busy:
            return
        self._placeholder_active = False
        self.input_entry.configure(fg=TEXT)
        self.input_var.set(cmd)
        if needs_arg:
            self.input_entry.focus_set()
            self.input_entry.icursor("end")
            return
        self._on_send()

    # ------------------------------------------------------------------
    # Status bar / busy state
    # ------------------------------------------------------------------
    def _refresh_status_bar(self) -> None:
        memory_count = len(self.core.list_memories(active_only=True))
        self.memory_var.set(f"MEMORY: {memory_count} active")
        self._set_dot(self.memory_dot, GREEN if memory_count else TEXT_DIM)

        if self.core.rag_service is not None:
            try:
                status = self.core.rag_service.status()
                self.rag_var.set(f"RAG: {status.total_documents} docs")
                self._set_dot(self.rag_dot, GREEN)
            except Exception:
                self.rag_var.set("RAG: error")
                self._set_dot(self.rag_dot, RED)
        else:
            self.rag_var.set("RAG: disabled")
            self._set_dot(self.rag_dot, TEXT_DIM)

        obsidian_connected = isinstance(self.memory_store, ObsidianMemoryStore) and self.memory_store.is_connected()
        self.obsidian_var.set(f"OBSIDIAN: {'linked' if obsidian_connected else 'offline'}")
        self._set_dot(self.obsidian_dot, GREEN if obsidian_connected else TEXT_DIM)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.input_entry.configure(state="disabled" if busy else "normal")
        self.send_button.itemconfigure(self._send_pill, fill=BORDER_DIM if busy else ACCENT_DIM)
        self.send_button.itemconfigure(self._send_label, fill=TEXT_DIM if busy else TEXT)
        if busy:
            if self._speaking_job is not None:
                try:
                    self.root.after_cancel(self._speaking_job)
                except tk.TclError:
                    pass
                self._speaking_job = None
            self.face.set_state("thinking")
            self.voice_state_var.set("Thinking...")
            self._show_typing_indicator()
        else:
            # Face state is intentionally left alone here -- a successful
            # reply hands off to _speak_response() for the talking
            # animation; error paths set it back to idle themselves.
            self.voice_state_var.set("Ready to help.")
            self.input_entry.focus_set()

    def _speak_response(self, text: str) -> None:
        if self._speaking_job is not None:
            try:
                self.root.after_cancel(self._speaking_job)
            except tk.TclError:
                pass
        words = max(1, len(text.split()))
        duration_ms = max(900, min(6000, words * 220))
        self.face.set_state("speaking")
        self._speaking_job = self.root.after(duration_ms, self._end_speaking)

    def _end_speaking(self) -> None:
        self._speaking_job = None
        if not self._busy and self.face.state == "speaking":
            self.face.set_state("idle")

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------
    def _help_text(self) -> str:
        return (
            "Here's what I can do:\n"
            "/help - show this help\n"
            "/memories - list active memories\n"
            "/search <keyword> - search memories\n"
            "/remember <text> - save a memory manually\n"
            "/delete <id_or_text> - delete a memory\n"
            "/forget <text> - alias for /delete\n"
            "/candidates - show pending uncertain memory candidates\n"
            "/memory approve <id> - approve a candidate and store it\n"
            "/memory reject <id> - reject a candidate\n"
            "/rag [status|rebuild] - show RAG index status or rebuild it\n"
            "/tools - list available tools\n"
            "/calendar status - show Google Calendar connection status\n"
            "/calendar connect - connect your Google account (opens a browser)\n"
            "/weather [location] - show current weather + 3-day forecast\n"
            "/obsidian - show vault status\n"
            "/sync - sync memory storage\n"
            "/clear - clear the chat\n"
            "/exit - close the window"
        )

    def _tools_text(self) -> str:
        tools = self.core.list_tools()
        if not tools:
            return "No tools are configured."
        lines = ["Available tools:"]
        for tool in tools:
            connected = "connected" if tool.is_available() else "not connected"
            lines.append(f"{tool.name}: {tool.description} ({connected})")
            for operation in tool.operations:
                lines.append(f"  {operation.name} [{operation.access}] -- {operation.description}")
        return "\n".join(lines)

    def _calendar_status_text(self) -> str:
        tool = self.core.get_tool("calendar")
        if tool is None:
            return "The Calendar tool is not enabled (set MIKI_CALENDAR_ENABLED=true and restart Miki)."
        status = tool.status()
        lines = [f"Google Calendar: {'Connected' if status.get('connected') else 'Not connected'}"]
        if not status.get("connected") and status.get("reason"):
            lines.append(f"Reason: {status['reason']}")
            lines.append("Type /calendar connect to connect your Google account.")
        return "\n".join(lines)

    def _connect_calendar(self) -> None:
        if self.core.get_tool("calendar") is None:
            self._append_system("The Calendar tool is not enabled (set MIKI_CALENDAR_ENABLED=true and restart Miki).")
            return

        def worker() -> None:
            try:
                run_oauth_flow(self.settings.google_calendar_credentials_path, self.settings.google_calendar_token_path)
            except CalendarUnavailable as err:
                err_msg = str(err)
                self.root.after(0, lambda: self._append_error(f"Could not connect Google Calendar.\n{err_msg}"))
                return
            except Exception as err:
                err_msg = str(err)
                self.root.after(0, lambda: self._append_error(f"Google Calendar authentication failed.\n{err_msg}"))
                logger.exception("Google Calendar OAuth flow failed")
                return
            self.root.after(0, lambda: (self._append_system("Google Calendar connected."), self._refresh_calendar_panel()))

        self._append_system("Opening your browser to connect Google Calendar...")
        threading.Thread(target=worker, daemon=True).start()

    def _weather_command_text(self, command: str) -> str:
        tool = self.core.get_tool("weather")
        if tool is None:
            return "The Weather tool is not enabled (set MIKI_WEATHER_ENABLED=true and restart Miki)."

        location = command[len("/weather"):].strip() or None
        current_result = tool.execute("get_current_weather", {"location": location} if location else {})
        if not current_result.success:
            return f"Weather unavailable -- {current_result.message}"

        current = current_result.data["current"]
        lines = [
            f"Weather for {current_result.data['location']}",
            f"{current['condition']} ({current['condition_symbol']}), {current['temperature']}°C (feels like {current['feels_like']}°C)",
            f"Precipitation: {current['precipitation_probability']}%   Wind: {current['wind_speed']} km/h   Humidity: {current['humidity']}%",
        ]

        daily_result = tool.execute("get_daily_forecast", {"location": location, "days": 3} if location else {"days": 3})
        if daily_result.success:
            lines.append("Next days:")
            for day in daily_result.data["daily"]:
                lines.append(f"  {day['date']}  {day['condition']} ({day['condition_symbol']})  high {day['temperature_high']}°C / low {day['temperature_low']}°C  rain {day['precipitation_probability']}%")

        return "\n".join(lines)

    def _rag_command_text(self, command: str) -> str:
        if self.core.rag_service is None:
            return "RAG is disabled or not configured (set MIKI_RAG_ENABLED=true to enable it)."

        parts = command.split()
        subcommand = parts[1] if len(parts) > 1 else "status"

        if subcommand == "rebuild":
            try:
                status = self.core.rag_service.rebuild()
            except Exception as exc:
                logger.exception("RAG rebuild failed")
                return f"RAG rebuild failed: {exc}"
            return (
                "Rebuild complete.\n"
                f"{status.total_documents} document(s) indexed as {status.total_chunks} chunk(s)\n"
                f"(memory: {status.memory_documents}, obsidian: {status.obsidian_documents}, "
                f"conversation: {status.conversation_documents})"
            )

        status = self.core.rag_service.status()
        return (
            "RAG status\n"
            f"Enabled: {status.enabled}\n"
            f"Indexed documents: {status.total_documents} ({status.total_chunks} chunk(s))\n"
            f"  Memory: {status.memory_documents}  Obsidian: {status.obsidian_documents}  "
            f"Conversation: {status.conversation_documents}\n"
            f"Embedding model: {status.embedding_model}\n"
            f"Top-K: {status.top_k}  Similarity threshold: {status.similarity_threshold}"
        )

    def _candidates_text(self) -> str:
        candidates = self.core.list_memory_candidates(status="pending")
        if not candidates:
            return "No pending memory candidates."
        lines = [f"{len(candidates)} pending memory candidate(s):"]
        for i, candidate in enumerate(candidates, 1):
            lines.append(f'{i}. [{candidate.candidate_id}] "{candidate.raw_message}" (score: {candidate.score:.0%})')
            if candidate.reason:
                lines.append(f"   Reason: {candidate.reason}")
        lines.append("Use /memory approve <id> or /memory reject <id>.")
        return "\n".join(lines)

    def _memory_command_result(self, command: str) -> tuple[str, bool]:
        parts = command.split()
        if len(parts) < 3 or parts[1] not in {"approve", "reject"}:
            return "Usage: /memory approve <id>  or  /memory reject <id>", False

        action, candidate_id = parts[1], parts[2]
        if action == "approve":
            memory = self.core.approve_memory_candidate(candidate_id)
            if memory is None:
                return f"Could not approve candidate '{candidate_id}' (not found or already resolved).", False
            return f"Approved. Stored as a {memory.category} memory: {memory.content}", True

        rejected = self.core.reject_memory_candidate(candidate_id)
        if rejected:
            return f"Rejected candidate '{candidate_id}'. Thanks, I'll remember this for next time.", False
        return f"Could not reject candidate '{candidate_id}' (not found or already resolved).", False

    def _handle_command(self, raw_text: str) -> bool:
        command = raw_text.strip()
        lower = command.lower()

        if lower in {"/help", "help"}:
            self._append_system(self._help_text())
            return True

        if lower in {"/clear", "clear"}:
            self.chat_log.configure(state="normal")
            self.chat_log.delete("1.0", "end")
            self.chat_log.configure(state="disabled")
            self._bubble_rows = []
            self._append_system("Chat cleared.")
            return True

        if lower in {"/exit", "/quit", "exit", "quit"}:
            self.root.after(50, self._on_close)
            return True

        if lower == "/obsidian":
            if isinstance(self.memory_store, ObsidianMemoryStore) and self.memory_store.is_connected():
                info = self.memory_store.describe()
                self._append_system(
                    f"Obsidian: Connected\nVault: {info.vault_name}\nMiki directory: {info.display_miki_dir}"
                )
            else:
                self._append_system("Obsidian: Not connected\nVault: Not configured\nMiki directory: Miki/")
            return True

        if lower == "/sync":
            result = self.core.memory_manager.sync_memories()
            self._append_system(
                "Sync complete.\n"
                f"Active memories: {result.get('active', 0)}\n"
                f"Inactive memories: {result.get('inactive', 0)}\n"
                f"Total: {result.get('total', 0)}"
            )
            self._refresh_status_bar()
            self.graph_card.refresh()
            return True

        if lower == "/rag" or lower.startswith("/rag "):
            self._append_system(self._rag_command_text(lower))
            self._refresh_status_bar()
            return True

        if lower in {"/candidates", "/memory_candidates"}:
            self._append_system(self._candidates_text())
            return True

        if lower == "/tools":
            self._append_system(self._tools_text())
            return True

        if lower == "/calendar" or lower == "/calendar status":
            self._append_system(self._calendar_status_text())
            self._refresh_calendar_panel()
            return True

        if lower == "/calendar connect":
            self._connect_calendar()
            return True

        if lower == "/weather" or lower.startswith("/weather "):
            self._append_system(self._weather_command_text(command))
            return True

        if lower in {"/memory stats", "/memory_stats", "/stats"}:
            if self.core.memory_manager is not None:
                self._append_system(self.core.memory_manager.format_telemetry_stats())
            else:
                self._append_system("Memory manager is not configured.")
            return True

        if lower.startswith("/memory "):
            message, was_memory_event = self._memory_command_result(lower)
            if was_memory_event:
                self._append_memory(message)
                self._refresh_status_bar()
            else:
                self._append_system(message)
            return True

        if lower.startswith("/memories"):
            memories = self.core.list_memories(active_only=True)
            if not memories:
                self._append_system("No active memories yet.")
            else:
                lines = ["Active memories:"]
                for i, memory in enumerate(memories, 1):
                    lines.append(f"{i}. [{memory.category}] {memory.content} ({memory.confidence:.0%})")
                self._append_system("\n".join(lines))
            return True

        if lower.startswith("/search "):
            keyword = command[len("/search "):].strip()
            results = self.core.memory_manager.search_memories(keyword) if keyword else []
            if not results:
                self._append_system(f"No memories found matching '{keyword}'.")
            else:
                lines = [f"Found {len(results)} matching memories:"]
                for i, memory in enumerate(results, 1):
                    lines.append(f"{i}. [{memory.category}] {memory.content}")
                self._append_system("\n".join(lines))
            return True

        if lower.startswith("/remember "):
            content = command[len("/remember "):].strip()
            if not content:
                self._append_error("Please provide memory content after /remember.")
                return True
            memory = self.core.create_memory(content, category="general", memory_type="fact", confidence=0.8, source="manual")
            self._append_memory(
                f"Memory saved -- {memory.category} / {memory.memory_type} (confidence {memory.confidence:.0%})\n"
                f"Stored in: {_describe_memory_storage(self.memory_store, memory)}"
            )
            self._refresh_status_bar()
            return True

        if lower.startswith("/delete "):
            lookup = command[len("/delete "):].strip()
            memory = self.core.delete_memory_by_text(lookup) if lookup else None
            if memory is None:
                self._append_error("I could not find a matching memory to delete.")
            else:
                self._append_system(f"Memory deleted: {memory.content}")
                self._refresh_status_bar()
            return True

        if lower.startswith("/forget "):
            lookup = command[len("/forget "):].strip()
            memory = self.core.delete_memory_by_text(lookup) if lookup else None
            if memory is None:
                self._append_error("I could not find a matching memory to forget.")
            else:
                self._append_system(f"Forgotten memory: {memory.content}")
                self._refresh_status_bar()
            return True

        return False

    # ------------------------------------------------------------------
    # Freeform chat
    # ------------------------------------------------------------------
    def _process_freeform_message(self, raw_text: str) -> None:
        def worker() -> None:
            try:
                response, memory_created = self.core.process_user_input(raw_text)
            except Exception as exc:
                error_message = str(exc)

                def on_error() -> None:
                    self._hide_typing_indicator()
                    self._append_error(f"I ran into a problem responding just now.\n{error_message}")
                    self._set_busy(False)
                    self.face.set_state("idle")

                self.root.after(0, on_error)
                return

            def update_ui() -> None:
                self._hide_typing_indicator()
                if response:
                    self._append_assistant(response)
                    self.voice_caption_var.set(response)
                if memory_created and self.core.last_created_memory is not None:
                    memory = self.core.last_created_memory
                    self._append_memory(
                        f"Memory saved -- {memory.category} / {memory.memory_type} "
                        f"(confidence {memory.confidence:.0%})\n"
                        f"Stored in: {_describe_memory_storage(self.memory_store, memory)}\n"
                        f"RAG: {_describe_rag_status(self.core)}"
                    )
                elif self.core.last_memory_candidate is not None:
                    candidate = self.core.last_memory_candidate
                    self._append_candidate(
                        f"I'm not fully sure about this one yet (score {candidate.score:.0%}).\n"
                        f'"{candidate.raw_message}"\n'
                        f"Review with /candidates, then /memory approve {candidate.candidate_id} "
                        f"or /memory reject {candidate.candidate_id}."
                    )
                self._refresh_status_bar()
                self._refresh_calendar_panel()
                if memory_created:
                    self.graph_card.refresh()
                self._set_busy(False)
                if response:
                    self._speak_response(response)
                else:
                    self.face.set_state("idle")

            self.root.after(0, update_ui)

        self._set_busy(True)
        threading.Thread(target=worker, daemon=True).start()

    def _on_send(self) -> None:
        if self._busy:
            return
        raw_text = self.input_var.get().strip()
        if not raw_text or raw_text == PLACEHOLDER_TEXT:
            return
        self.input_var.set("")
        self._append_user(raw_text)

        if self._handle_command(raw_text):
            return

        self._process_freeform_message(raw_text)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def _on_close(self) -> None:
        self._hide_typing_indicator()
        self.face.stop()
        self.root.destroy()

    def run(self) -> int:
        self.root.mainloop()
        return 0


def run_gui() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
        handlers=[logging.StreamHandler()],
    )
    logger.info("Miki desktop UI starting up")

    try:
        settings = Settings.from_env()
    except ValueError as exc:
        messagebox.showerror("Miki configuration error", str(exc))
        return 1

    try:
        shared_openai_client = OpenAI(api_key=settings.openai_api_key)
        brain = OpenAIClient(api_key=settings.openai_api_key, model=settings.model, client=shared_openai_client)
    except Exception as exc:
        messagebox.showerror("Miki startup error", f"Unable to initialize AI provider:\n{exc}")
        logger.exception("AI provider initialization failed")
        return 1

    conversation_store = JSONConversationStore("data/conversations")
    memory_store = build_memory_store(settings, json_storage_dir="data/memories")
    embedding_client = OpenAIEmbeddingClient(client=shared_openai_client, model=settings.embedding_model)
    memory_manager = build_memory_manager(settings, memory_store=memory_store, brain=brain, embedding_client=embedding_client)
    rag_service = build_rag_service(
        settings,
        memory_store=memory_store,
        shared_openai_client=shared_openai_client,
        conversations_dir="data/conversations",
        embedding_client=embedding_client,
    )
    tool_runner = build_tool_runner(settings, brain)
    core = MikiCore(brain=brain, conversation_store=conversation_store, memory_manager=memory_manager, rag_service=rag_service, tool_runner=tool_runner)

    root = tk.Tk()
    root.update_idletasks()
    _apply_windows_dark_titlebar(root)
    app = MikiChatWindow(root, core, memory_store, settings, startup_summary=_format_startup_summary(memory_store, core))
    return app.run()
