"""Memory-graph views for the Miki Command Center.

Builds a MemoryGraph from the Obsidian vault (wikilinks plus a folder-based
"category" hub per memory folder, so notes without wikilinks still form a
readable constellation) and renders it with plain Tk canvases:

- ``GraphCard``   -- compact dashboard card: counts, top hubs, mini graph.
- ``GraphWindow`` -- full interactive graph: search, drag, click for details.
"""

from __future__ import annotations

import logging
import math
import os
import re
import queue
import threading
import tkinter as tk
from typing import Callable

import networkx as nx

from app.memory.graph import MemoryGraph
from app.memory.models import MemoryEdge
from app.memory.vault import VaultManager

logger = logging.getLogger(__name__)

# Palette mirrors app/interfaces/gui.py (kept local to avoid a circular import).
BG = "#0a0705"
PANEL = "#16110b"
PANEL_ALT = "#211a10"
BORDER_DIM = "#3d2e18"
ACCENT = "#ffb238"
ACCENT_DIM = "#7a5420"
ACCENT_SOFT = "#e0b374"
TEXT = "#fbf3e6"
TEXT_DIM = "#8c7a61"

_SKIP_HUB_PARTS = {"miki", "memories"}
_HEADING_RE = re.compile(r"^#\s+(.+)$", re.MULTILINE)
_SUMMARY_RE = re.compile(r"^>\s+(.+)$", re.MULTILINE)


def _hub_name(vault_path: str, filepath: str) -> str | None:
    rel = os.path.relpath(os.path.dirname(filepath), vault_path)
    parts = [p for p in rel.replace("\\", "/").split("/") if p and p != "." and p.lower() not in _SKIP_HUB_PARTS]
    return " / ".join(parts) if parts else None


def build_memory_graph(vault_path: str) -> MemoryGraph:
    """Scan the vault and add a hub node per memory folder."""
    vault = VaultManager(vault_path)
    vault.scan_vault()
    graph = vault.graph
    for node in list(graph.nodes_data.values()):
        hub = _hub_name(vault_path, node.path)
        if hub is None:
            continue
        graph.add_edge(MemoryEdge(source_title=node.title, target_title=hub, type="category", context=hub))
        graph.graph.nodes[graph._normalize_title(hub)]["kind"] = "hub"
    return graph


def node_label(graph: MemoryGraph, key: str) -> str:
    """Human-readable label: note heading, else the original title."""
    data = graph.graph.nodes[key]
    note = graph.nodes_data.get(key)
    if note is not None:
        match = _HEADING_RE.search(note.content)
        if match:
            return match.group(1).strip()
    return data.get("original_title", key)


def node_summary(graph: MemoryGraph, key: str) -> str:
    note = graph.nodes_data.get(key)
    if note is None:
        return ""
    match = _SUMMARY_RE.search(note.content)
    return match.group(1).strip() if match else note.content.strip()[:200]


def compute_layout(graph: MemoryGraph) -> dict[str, tuple[float, float]]:
    """Per-component spring layout, components spread on a ring, roughly within [-1, 1]."""
    g = graph.graph.to_undirected()
    if len(g) == 0:
        return {}
    comps = sorted(nx.connected_components(g), key=len, reverse=True)
    pos: dict[str, tuple[float, float]] = {}
    for i, comp in enumerate(comps):
        sub = g.subgraph(comp)
        if len(sub) == 1:
            local = {next(iter(sub.nodes)): (0.0, 0.0)}
        else:
            raw = nx.spring_layout(sub, seed=7, iterations=80 if len(sub) < 300 else 30)
            local = {n: (float(x), float(y)) for n, (x, y) in raw.items()}
        radius = min(0.08 + 0.05 * math.sqrt(len(sub)), 0.4)
        if len(comps) == 1:
            cx = cy = 0.0
            radius = 0.9
        else:
            angle = 2 * math.pi * i / len(comps)
            cx, cy = 0.62 * math.cos(angle), 0.62 * math.sin(angle)
        for n, (x, y) in local.items():
            pos[n] = (cx + x * radius, cy + y * radius)
    return pos


class GraphCanvas(tk.Canvas):
    """Draws a MemoryGraph. In ``mini`` mode it is static; otherwise draggable/clickable."""

    def __init__(self, parent: tk.Widget, *, mini: bool = False, on_select: Callable[[str | None], None] | None = None, **kw) -> None:
        super().__init__(parent, bg=kw.pop("bg", PANEL), highlightthickness=0, **kw)
        self.mini = mini
        self.on_select = on_select
        self.graph: MemoryGraph | None = None
        self.pos: dict[str, tuple[float, float]] = {}
        self.selected: str | None = None
        self.highlight: set[str] | None = None
        self._drag: str | None = None
        self._pagerank: dict[str, float] = {}
        self.bind("<Configure>", lambda _e: self.redraw())
        if not mini:
            self.bind("<Button-1>", self._on_press)
            self.bind("<B1-Motion>", self._on_motion)
            self.bind("<ButtonRelease-1>", lambda _e: setattr(self, "_drag", None))

    # -- data -------------------------------------------------------------
    def set_graph(self, graph: MemoryGraph, pos: dict[str, tuple[float, float]]) -> None:
        self.graph, self.pos = graph, pos
        self._pagerank = nx.pagerank(graph.graph) if len(graph.graph) else {}
        if self.selected not in graph.graph:
            self.selected = None
        self.redraw()

    def set_highlight(self, keys: set[str] | None) -> None:
        self.highlight = keys
        self.redraw()

    def select(self, key: str | None) -> None:
        self.selected = key
        self.redraw()
        if self.on_select:
            self.on_select(key)

    # -- geometry ---------------------------------------------------------
    def _to_px(self, key: str) -> tuple[float, float]:
        w, h = max(self.winfo_width(), 2), max(self.winfo_height(), 2)
        pad = 14 if self.mini else 46
        x, y = self.pos[key]
        return pad + (x + 1) / 2 * (w - 2 * pad), pad + (y + 1) / 2 * (h - 2 * pad)

    def _from_px(self, px: float, py: float) -> tuple[float, float]:
        w, h = max(self.winfo_width(), 2), max(self.winfo_height(), 2)
        pad = 14 if self.mini else 46
        return ((px - pad) / (w - 2 * pad)) * 2 - 1, ((py - pad) / (h - 2 * pad)) * 2 - 1

    def _radius(self, key: str) -> float:
        base = 3 if self.mini else 6
        scale = 1 + 6 * self._pagerank.get(key, 0.0) * (1 if self.mini else 2)
        if self.graph and self.graph.graph.nodes[key].get("kind") == "hub":
            base += 2
        return min(base * scale, base * 3)

    def _node_at(self, px: float, py: float) -> str | None:
        best, best_d = None, 1e9
        for key in self.pos:
            x, y = self._to_px(key)
            d = math.hypot(x - px, y - py)
            if d <= self._radius(key) + 6 and d < best_d:
                best, best_d = key, d
        return best

    # -- drawing ----------------------------------------------------------
    def redraw(self) -> None:
        self.delete("all")
        g = self.graph.graph if self.graph else None
        if g is None or len(g) == 0:
            self.create_text(self.winfo_width() / 2, self.winfo_height() / 2, text="No memories in the vault yet",
                             fill=TEXT_DIM, font=("Segoe UI", 9))
            return
        dim = self.highlight is not None
        for a, b, data in g.edges(data=True):
            if a not in self.pos or b not in self.pos:
                continue
            (x1, y1), (x2, y2) = self._to_px(a), self._to_px(b)
            lit = not dim or (a in self.highlight and b in self.highlight)
            color = ACCENT_DIM if data.get("type") == "wikilink" else "#5a4426"
            self.create_line(x1, y1, x2, y2, fill=color if lit else PANEL_ALT, width=1)
        show_all_labels = (not self.mini) and len(g) <= 12
        for key in self.pos:
            x, y = self._to_px(key)
            r = self._radius(key)
            is_hub = g.nodes[key].get("kind") == "hub"
            is_stub = g.nodes[key].get("type") == "stub" and not is_hub
            fill = ACCENT if is_hub else (TEXT_DIM if is_stub else ACCENT_SOFT)
            if dim and key not in self.highlight:
                fill = BORDER_DIM
            outline = TEXT if key == self.selected else ""
            self.create_oval(x - r, y - r, x + r, y + r, fill=fill, outline=outline, width=2)
            if not self.mini and (show_all_labels or is_hub or key == self.selected or (dim and key in self.highlight)):
                label = node_label(self.graph, key)
                self.create_text(x, y + r + 9, text=label[:32], fill=TEXT if key == self.selected else TEXT_DIM,
                                 font=("Segoe UI", 8))

    # -- interaction ------------------------------------------------------
    def _on_press(self, event: tk.Event) -> None:
        key = self._node_at(event.x, event.y)
        self._drag = key
        self.select(key)

    def _on_motion(self, event: tk.Event) -> None:
        if self._drag:
            self.pos[self._drag] = self._from_px(event.x, event.y)
            self.redraw()


class GraphCard:
    """Dashboard card. Owns the graph, refreshes it in the background, opens GraphWindow."""

    def __init__(self, parent: tk.Widget, root: tk.Tk, vault_path: str | None, *, mono_font: str, ui_font: str,
                 pack_kwargs: dict | None = None) -> None:
        self.root, self.vault_path = root, vault_path
        self.mono_font, self.ui_font = mono_font, ui_font
        self.graph: MemoryGraph | None = None
        self.pos: dict[str, tuple[float, float]] = {}
        self._window: GraphWindow | None = None
        self._loading = False
        self._results: queue.Queue = queue.Queue()

        card = tk.Frame(parent, bg=PANEL, highlightthickness=1, highlightbackground=BORDER_DIM)
        card.pack(**(pack_kwargs or {"side": "bottom", "fill": "x", "pady": (10, 0)}))
        title_bar = tk.Frame(card, bg=PANEL)
        title_bar.pack(fill="x", padx=14, pady=(10, 4))
        tk.Label(title_bar, text="BRAIN", bg=PANEL, fg=ACCENT, font=(mono_font, 9, "bold")).pack(side="left")
        self.open_btn = tk.Label(title_bar, text="OPEN ↗", bg=PANEL_ALT, fg=ACCENT, font=(ui_font, 7, "bold"),
                                 padx=6, pady=1, cursor="hand2")
        self.open_btn.pack(side="right")
        self.open_btn.bind("<Button-1>", lambda _e: self.open_window())

        self.mini = GraphCanvas(card, mini=True, height=110)
        self.mini.pack(fill="x", padx=14)
        self.mini.bind("<Button-1>", lambda _e: self.open_window())
        self.stats_var = tk.StringVar(value="Loading graph...")
        tk.Label(card, textvariable=self.stats_var, bg=PANEL, fg=TEXT_DIM, font=(ui_font, 8), anchor="w").pack(fill="x", padx=14, pady=(4, 0))
        self.hubs_var = tk.StringVar(value="")
        tk.Label(card, textvariable=self.hubs_var, bg=PANEL, fg=TEXT_DIM, font=(ui_font, 8), anchor="w",
                 justify="left").pack(fill="x", padx=14, pady=(0, 10))
        self.refresh()

    def refresh(self) -> None:
        """Rebuild the graph off the UI thread, then repaint."""
        if self._loading:
            return
        if not self.vault_path:
            self.stats_var.set("Set OBSIDIAN_VAULT_PATH to enable")
            return
        self._loading = True

        def work() -> None:
            # Worker threads never touch Tk; results are polled from the UI thread.
            try:
                graph = build_memory_graph(self.vault_path)
                self._results.put(("ok", graph, compute_layout(graph)))
            except Exception as exc:  # pragma: no cover - defensive UI guard
                logger.exception("Memory graph refresh failed")
                self._results.put(("err", exc, None))

        threading.Thread(target=work, daemon=True).start()
        self.root.after(100, self._poll)

    def _poll(self) -> None:
        try:
            kind, a, b = self._results.get_nowait()
        except queue.Empty:
            self.root.after(100, self._poll)
            return
        if kind == "ok":
            self._apply(a, b)
        else:
            self._fail(a)

    def _fail(self, exc: Exception) -> None:
        self._loading = False
        self.stats_var.set(f"Graph unavailable: {exc}")

    def _apply(self, graph: MemoryGraph, pos: dict[str, tuple[float, float]]) -> None:
        self._loading = False
        self.graph, self.pos = graph, pos
        self.mini.set_graph(graph, dict(pos))
        memories = len(graph.nodes_data)
        hubs = sum(1 for _, d in graph.graph.nodes(data=True) if d.get("kind") == "hub")
        self.stats_var.set(f"{memories} memories · {hubs} categories · {len(graph.graph.edges)} links")
        top = [(k, s) for k, s in self._top_keys(graph)]
        self.hubs_var.set("Top: " + ", ".join(node_label(graph, k) for k, _ in top) if top else "")
        if self._window is not None and self._window.winfo_exists():
            self._window.load(graph, pos)

    @staticmethod
    def _top_keys(graph: MemoryGraph, n: int = 3) -> list[tuple[str, float]]:
        if len(graph.graph) == 0:
            return []
        ranks = nx.pagerank(graph.graph)
        keys = [k for k in graph.nodes_data if k in ranks]  # memories only; hubs always top otherwise
        return sorted(((k, ranks[k]) for k in keys), key=lambda kv: kv[1], reverse=True)[:n]

    def open_window(self) -> None:
        if self.graph is None:
            return
        if self._window is not None and self._window.winfo_exists():
            self._window.lift()
            return
        self._window = GraphWindow(self.root, self, mono_font=self.mono_font, ui_font=self.ui_font)
        self._window.load(self.graph, self.pos)


class GraphWindow(tk.Toplevel):
    def __init__(self, root: tk.Tk, card: GraphCard, *, mono_font: str, ui_font: str) -> None:
        super().__init__(root)
        self.card, self.mono_font, self.ui_font = card, mono_font, ui_font
        self.title("MIKI — Memory Graph")
        self.geometry("1000x680")
        self.minsize(720, 480)
        self.configure(bg=BG)

        top = tk.Frame(self, bg=PANEL, highlightthickness=1, highlightbackground=BORDER_DIM)
        top.pack(fill="x", padx=14, pady=(14, 8))
        tk.Label(top, text="MEMORY GRAPH", bg=PANEL, fg=ACCENT, font=(mono_font, 10, "bold")).pack(side="left", padx=12, pady=8)
        refresh = tk.Label(top, text="↻ REFRESH", bg=PANEL_ALT, fg=ACCENT, font=(ui_font, 8, "bold"), padx=8, pady=3, cursor="hand2")
        refresh.pack(side="right", padx=12)
        refresh.bind("<Button-1>", lambda _e: card.refresh())
        self.search_var = tk.StringVar()
        entry = tk.Entry(top, textvariable=self.search_var, bg=PANEL_ALT, fg=TEXT, insertbackground=ACCENT,
                         relief="flat", font=(ui_font, 10), width=32)
        entry.pack(side="right", ipady=4, padx=(0, 10))
        tk.Label(top, text="Search", bg=PANEL, fg=TEXT_DIM, font=(ui_font, 9)).pack(side="right")
        self.search_var.trace_add("write", lambda *_: self._on_search())

        body = tk.Frame(self, bg=BG)
        body.pack(fill="both", expand=True, padx=14, pady=(0, 14))
        side = tk.Frame(body, bg=PANEL, width=270, highlightthickness=1, highlightbackground=BORDER_DIM)
        side.pack(side="right", fill="y", padx=(10, 0))
        side.pack_propagate(False)
        self.detail_title = tk.Label(side, text="Select a node", bg=PANEL, fg=TEXT, font=(ui_font, 12, "bold"),
                                     wraplength=240, justify="left", anchor="w")
        self.detail_title.pack(fill="x", padx=12, pady=(12, 4))
        self.detail_body = tk.Label(side, text="Click a memory to see its summary and connections. Drag nodes to rearrange.",
                                    bg=PANEL, fg=TEXT_DIM, font=(ui_font, 9), wraplength=240, justify="left", anchor="nw")
        self.detail_body.pack(fill="both", expand=True, padx=12, pady=(0, 12))

        self.canvas = GraphCanvas(body, on_select=self._on_select, bg=PANEL)
        self.canvas.pack(side="left", fill="both", expand=True)
        legend = tk.Frame(self, bg=BG)
        legend.pack(pady=(0, 8))
        for color, text in ((ACCENT, "category"), (ACCENT_SOFT, "memory"), (TEXT_DIM, "unresolved link")):
            tk.Label(legend, text="●", bg=BG, fg=color, font=(ui_font, 9)).pack(side="left", padx=(10, 2))
            tk.Label(legend, text=text, bg=BG, fg=TEXT_DIM, font=(ui_font, 8)).pack(side="left")

    def load(self, graph: MemoryGraph, pos: dict[str, tuple[float, float]]) -> None:
        self.canvas.set_graph(graph, dict(pos))
        self._on_search()

    def _on_search(self) -> None:
        graph = self.canvas.graph
        query = self.search_var.get().strip()
        if graph is None or not query:
            self.canvas.set_highlight(None)
            return
        keys = {graph._normalize_title(n.title) for n in graph.search(query)}
        self.canvas.set_highlight(keys)

    def _on_select(self, key: str | None) -> None:
        graph = self.canvas.graph
        if key is None or graph is None:
            self.detail_title.config(text="Select a node")
            self.detail_body.config(text="Click a memory to see its summary and connections. Drag nodes to rearrange.")
            return
        related = sorted(graph.get_related(graph.graph.nodes[key].get("original_title", key), depth=1))
        related_labels = [node_label(graph, graph._normalize_title(r)) for r in related]
        note = graph.nodes_data.get(key)
        lines = []
        summary = node_summary(graph, key)
        if summary:
            lines.append(summary)
        elif graph.graph.nodes[key].get("kind") == "hub":
            lines.append("Category")
        else:
            lines.append("Referenced but not written yet.")
        if note and note.tags:
            lines.append("Tags: " + ", ".join(sorted(note.tags)))
        if related_labels:
            lines.append("Connected to: " + ", ".join(related_labels[:12]) + ("…" if len(related_labels) > 12 else ""))
        self.detail_title.config(text=node_label(graph, key))
        self.detail_body.config(text="\n\n".join(lines))
