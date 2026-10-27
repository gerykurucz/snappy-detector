from __future__ import annotations

import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any

from lattool_controller import (
    CONSENT_TEXT,
    EventType,
    GuiController,
    PlainFinding,
    apply_confirm_text,
    chain_confirm_text,
    classify_scan,
    issues_message,
    manual_instructions,
    run_confirm_text,
    undo_confirm_text,
)
from input_utils import DEFAULT_THEME, ButtonFactory, Theme

APP_TITLE = "latency"
APP_SUBTITLE = "finds what is actually slowing your game, and fixes what it can"
POLL_MS = 80

WATCH_SECONDS = 20.0

CHAIN_TILE_LABELS = {
    "input": "INPUT (p50 one-way)",
    "render": "RENDER (p95)",
    "network": "NETWORK (p95)",
}

_FALLBACK_STYLE = {
    "plain": "TButton",
    "accent": "Accent.TButton",
    "big": "Big.TButton",
    "danger": "Danger.TButton",
}

# Default fonts used when gui.py's font dict is not provided
_DEFAULT_FONTS = {
    "base":   ("Segoe UI", 11),
    "bold":   ("Segoe UI", 11, "bold"),
    "h1":     ("Segoe UI", 19, "bold"),
    "entry":  ("Segoe UI", 11),
}

class _PalTheme:
    """Attribute-style view over gui.py's pal dict, so lattool_gui.py's
    existing `t.raised` / `t.accent` etc. keep working unchanged."""
    def __init__(self, pal, fonts):
        self.bg       = pal["bg"]
        self.raised   = pal["card"]
        self.inset    = pal["entry"]
        self.fg       = pal["text"]
        self.muted    = pal["muted"]
        self.accent   = pal["accent"]
        self.good     = pal["green"]
        self.warn     = pal["amber"]
        self.bad      = pal["accent"]
        self.border   = pal["entry_bd"]
        self.entry_bg = pal["entry"]
        self.entry_fg = pal["text"]
        self.font        = fonts["base"]
        self.font_bold   = fonts["bold"]
        self.font_h1     = fonts["h1"]
        self.font_h2     = fonts["h1"]
        self.font_big    = fonts["h1"]
        self.font_entry  = fonts["entry"]
        self.font_mono   = ("Consolas", 10)


class DarkCheck(ttk.Frame):
    SIZE = 15

    def __init__(
        self, master, *, text: str, variable: tk.BooleanVar | None = None,
        command: Any = None, surface: str = "bg", theme: Theme | None = None,
    ) -> None:
        t = theme or DEFAULT_THEME
        super().__init__(master, style=("Card.TFrame" if surface == "card" else "TFrame"))
        self.var = variable if variable is not None else tk.BooleanVar(master=self)
        self.command = command
        self._t = t
        bg = t.raised if surface == "card" else t.bg
        self.canvas = tk.Canvas(
            self, width=self.SIZE, height=self.SIZE, bg=bg, highlightthickness=1,
            highlightbackground=t.border, highlightcolor=t.accent, bd=0,
            takefocus=1, cursor="hand2",
        )
        self.canvas.pack(side="left", padx=(0, 8), pady=2)
        self.label = tk.Label(
            self, text=text, bg=bg, cursor="hand2", font=t.font, fg=t.fg,
            anchor="w", justify="left", wraplength=860,
        )
        self.label.pack(side="left", fill="x", expand=True)
        for w in (self.canvas, self.label):
            w.bind("<Button-1>", lambda _e: self.toggle())
            w.bind("<space>", lambda _e: self.toggle())
            w.bind("<Return>", lambda _e: self.toggle())
        self.var.trace_add("write", lambda *_a: self._draw())
        self._draw()

    def toggle(self) -> None:
        self.var.set(not self.var.get())
        if self.command:
            self.command()

    def _draw(self) -> None:
        t = self._t
        c = self.canvas
        c.delete("all")
        s = self.SIZE
        if self.var.get():
            c.create_rectangle(1, 1, s - 1, s - 1, fill=t.accent, outline=t.accent)
            c.create_line(3.5, s / 2, s / 2 - 1, s - 4.5, fill="#ffffff", width=2)
            c.create_line(s / 2 - 1, s - 4.5, s - 3.5, 3.5, fill="#ffffff", width=2)
        else:
            c.create_rectangle(1, 1, s - 1, s - 1, fill=t.inset, outline=t.border)


class SnappyAIApp:
    def __init__(
        self, root: tk.Tk, controller: GuiController | None = None, *,
        embed: dict[str, Any] | None = None,
    ) -> None:
        self.root = root
        self.ctl = controller or GuiController()

        # --- styling sources, in priority order ---
        self._kit = (embed.get("kit") if embed else None)
        self._pal = ((embed.get("pal") if embed else None)
                     or (self._kit.pal if self._kit else None))
        self._fonts = (embed.get("fonts") if embed else None)

        if self._pal is not None:
            self.theme = _PalTheme(self._pal, self._fonts or _DEFAULT_FONTS)
        else:
            self.theme = (embed.get("theme") if embed else None) or DEFAULT_THEME

        self._button_factory = (
            (embed.get("button") if embed else None)
            or (self._kit.button if self._kit else None)
        )
        self._on_timer_persist = (embed.get("on_timer_persist") if embed else None)
        self.last_script_path = ""

        # For embed mode: trace timer var so on_timer_toggle fires when kit.bool_switch changes it
        self._embed_timer_var_traced = False

        if embed is None:
            root.title(APP_TITLE)
            root.geometry("1080x760")
            root.minsize(900, 640)
            root.configure(bg=self.theme.bg)
            self._configure_style()
            self._build()
        else:
            frames = embed["frames"]
            self.tab_home = frames["home"]
            self.tab_monitor = frames["monitor"]
            self.tab_tools = frames["tools"]
            status = embed.get("status")
            if status is None:
                status = ttk.Label(self.tab_home, style="CardDim.TLabel",
                                   anchor="w")
                status.pack(side="bottom", fill="x", padx=14, pady=(0, 8))
            self.status = status
            self._build_home()
            self._build_monitor()
            self._build_tools()
            self._configure_style(safe=True)
            self.header_state = ttk.Label(self.tab_home)
            self.progress = ttk.Progressbar(self.tab_home, mode="indeterminate")
        self._last_series: list[float] = []
        self._show_screen("start")
        if embed is None:
            root.after(POLL_MS, self._pump)

    def _button(
        self, parent, text: str, command: Any = None, kind: str = "plain",
        big: bool = False,
    ):
        # Map lattool_gui's "danger" kind to kit's "amber"
        kit_kind = {"danger": "amber"}.get(kind, kind)
        if self._kit is not None:
            return self._kit.button(parent, text, command, kind=kit_kind, big=big)
        if self._button_factory is not None:
            return self._button_factory(
                parent, text, command, kind=kind, big=big
            )
        return ttk.Button(
            parent, text=text, command=command,
            style=_FALLBACK_STYLE["big" if big else kind],
        )

    def _check(self, parent, text, variable, surface="bg"):
        """Create a checkbox row using kit.bool_switch when available,
        otherwise fall back to DarkCheck."""
        if self._kit is not None:
            row = tk.Frame(parent, bg=self.theme.raised if surface == "card"
                                        else self.theme.bg)
            self._kit.bool_switch(row, variable, word=True).pack(side="left")
            lbl = tk.Label(
                row, text=text, anchor="w", justify="left",
                bg=row.cget("bg"), fg=self.theme.fg,
                font=self.theme.font, wraplength=800, cursor="hand2",
            )
            lbl.pack(side="left", padx=(8, 0))
            # click on the text toggles the switch too
            lbl.bind("<Button-1>", lambda _e: variable.set(not variable.get()))
            return row
        # fallback for standalone mode
        dc = DarkCheck(parent, text=text, variable=variable,
                       surface=surface, theme=self.theme)
        return dc

    def _configure_style(self, safe: bool = False) -> None:
        t = self.theme
        style = ttk.Style(self.root)
        if not safe:
            try:
                style.theme_use("clam")
            except tk.TclError:
                pass
            style.configure(
                ".", background=t.bg, foreground=t.fg, font=t.font,
                bordercolor=t.border, lightcolor=t.bg, darkcolor=t.bg,
            )
        style.configure("TFrame", background=t.bg)
        style.configure("Raised.TFrame", background=t.raised)
        style.configure("Card.TFrame", background=t.raised)
        style.configure("TLabel", background=t.bg, foreground=t.fg, font=t.font)
        style.configure("Dim.TLabel", background=t.bg, foreground=t.muted, font=t.font)
        style.configure(
            "Card.TLabel", background=t.raised, foreground=t.fg, font=t.font
        )
        style.configure(
            "CardDim.TLabel", background=t.raised, foreground=t.muted, font=t.font
        )
        style.configure("H1.TLabel", background=t.bg, foreground=t.fg, font=t.font_h1)
        style.configure("H2.TLabel", background=t.bg, foreground=t.fg, font=t.font_h2)
        style.configure(
            "Big.TLabel", background=t.raised, foreground=t.accent,
            font=t.font_big,
        )
        style.configure(
            "TButton", background=t.raised, foreground=t.fg, padding=(14, 7),
            borderwidth=1, relief="flat", focuscolor=t.accent, font=t.font,
        )
        style.map(
            "TButton", background=[("active", t.border), ("disabled", t.bg)],
            foreground=[("disabled", t.muted)],
        )
        style.configure(
            "Accent.TButton", background=t.accent, foreground=t.bg,
            font=t.font_bold, padding=(20, 10),
        )
        style.map(
            "Accent.TButton", background=[("active", t.accent)],
            foreground=[("disabled", t.muted)],
        )
        style.configure(
            "Big.TButton", background=t.accent, foreground=t.bg,
            font=(t.font_h1[0], 13, "bold"), padding=(28, 16),
        )
        style.map("Big.TButton", background=[("active", t.accent)])
        style.configure("Danger.TButton", background=t.raised, foreground=t.warn)
        style.map("Danger.TButton", background=[("active", t.border)])
        style.configure(
            "TNotebook", background=t.bg, borderwidth=0, tabmargins=(6, 6, 6, 0)
        )
        style.configure(
            "TNotebook.Tab", background=t.bg, foreground=t.muted, padding=(20, 10),
            font=t.font, borderwidth=0,
        )
        style.map(
            "TNotebook.Tab",
            background=[("selected", t.raised), ("active", t.border)],
            foreground=[("selected", t.accent), ("active", t.fg)],
        )
        style.configure(
            "TEntry", fieldbackground=t.entry_bg, foreground=t.entry_fg,
            insertcolor=t.entry_fg, bordercolor=t.border, padding=5, font=t.font_entry,
        )
        style.configure(
            "TCombobox",
            fieldbackground=t.entry_bg, background=t.entry_bg,
            foreground=t.entry_fg, arrowcolor=t.entry_fg,
            bordercolor=t.border, padding=4, font=t.font,
        )
        style.map("TCombobox",
                  fieldbackground=[("readonly", t.entry_bg)],
                  foreground=[("readonly", t.entry_fg)])
        style.configure(
            "Horizontal.TProgressbar", background=t.accent, troughcolor=t.inset,
            bordercolor=t.inset, lightcolor=t.accent, darkcolor=t.accent,
            thickness=8,
        )
        style.configure(
            "TLabelframe", background=t.bg, foreground=t.muted,
            bordercolor=t.border,
        )
        style.configure(
            "TLabelframe.Label", background=t.bg, foreground=t.muted,
            font=t.font_bold,
        )
        style.configure(
            "Card.TLabelframe", background=t.raised, bordercolor=t.border
        )
        style.configure(
            "Card.TLabelframe.Label", background=t.raised, foreground=t.accent,
            font=t.font_bold,
        )
        for orient in ("Vertical", "Horizontal"):
            style.configure(
                f"{orient}.TScrollbar", background=t.raised,
                troughcolor=t.inset, arrowcolor=t.muted,
                bordercolor=t.inset, relief="flat", arrowsize=13, gripcount=0,
            )
            style.map(
                f"{orient}.TScrollbar",
                background=[("active", t.border), ("disabled", t.raised)],
            )

    def retheme(self, theme: Theme) -> None:
        """Live light/dark switch when embedded in a host app: restyle ttk,
        remap the raw tk widgets that captured colours at build time, and
        redraw the canvases."""
        if self._kit is not None:
            # kit.switch_to() repaints every registered surface, but the raw
            # tk.Label/tk.Canvas widgets below are not registered with kit - walk
            # them explicitly.
            theme = _PalTheme(self._kit.pal, self._fonts or _DEFAULT_FONTS)
        old = self.theme
        self.theme = theme
        self._configure_style(safe=True)
        bg_map = {old.bg.lower(): theme.bg, old.raised.lower(): theme.raised,
                  old.inset.lower(): theme.inset}
        fg_map = {old.fg.lower(): theme.fg, old.muted.lower(): theme.muted,
                  old.accent.lower(): theme.accent, old.good.lower(): theme.good,
                  old.warn.lower(): theme.warn, old.bad.lower(): theme.bad}
        bd = old.border.lower()
        checks: list[Any] = []

        def walk(w: Any) -> None:
            try:
                if isinstance(w, DarkCheck):
                    w._t = theme
                    checks.append(w)
                elif isinstance(w, tk.Label):
                    bg = str(w.cget("bg")).lower()
                    if bg in bg_map:
                        w.configure(bg=bg_map[bg])
                    fg = str(w.cget("fg")).lower()
                    if fg in fg_map:
                        w.configure(fg=fg_map[fg])
                elif isinstance(w, tk.Canvas):
                    bg = str(w.cget("bg")).lower()
                    if bg in bg_map:
                        w.configure(bg=bg_map[bg])
                    if str(w.cget("highlightbackground")).lower() == bd:
                        w.configure(highlightbackground=theme.border)
                elif isinstance(w, tk.Entry):
                    bg = str(w.cget("bg")).lower()
                    if bg in bg_map:
                        w.configure(bg=bg_map[bg])
                    fg = str(w.cget("fg")).lower()
                    if fg in fg_map:
                        w.configure(fg=fg_map[fg], insertbackground=fg_map[fg])
                    if str(w.cget("highlightbackground")).lower() == bd:
                        w.configure(highlightbackground=theme.border)
                elif isinstance(w, tk.Text):
                    bg = str(w.cget("bg")).lower()
                    if bg in bg_map:
                        w.configure(bg=bg_map[bg])
                    fg = str(w.cget("fg")).lower()
                    if fg in fg_map:
                        w.configure(fg=fg_map[fg], insertbackground=fg_map[fg])
                    sel = str(w.cget("selectbackground")).lower()
                    if sel in fg_map:
                        w.configure(selectbackground=fg_map[sel],
                                    selectforeground=theme.bg)
                    if str(w.cget("highlightbackground")).lower() == bd:
                        w.configure(highlightbackground=theme.border)
                    for tag, colour in (("h", theme.accent),
                                        ("good", theme.good),
                                        ("warn", theme.warn),
                                        ("bad", theme.bad),
                                        ("dim", theme.muted),
                                        ("head", theme.fg)):
                        w.tag_configure(tag, foreground=colour)
                for child in w.winfo_children():
                    walk(child)
            except tk.TclError:
                pass

        for frame in (self.tab_home, self.tab_monitor, self.tab_tools):
            walk(frame)
        for dc in checks:
            try:
                dc._draw()
            except tk.TclError:
                pass
        try:
            self._draw_spark(list(self._last_series))
        except tk.TclError:
            pass

    def _build(self) -> None:
        t = self.theme
        brand = self.ctl.brand
        outer = ttk.Frame(self.root)
        outer.pack(fill="both", expand=True)
        head = ttk.Frame(outer, style="Raised.TFrame", padding=(18, 12))
        head.pack(fill="x", side="top")
        ttk.Label(
            head, text=brand, style="H2.TLabel", font=(t.font_h1[0], 15),
        ).pack(side="left")
        ttk.Label(head, text="  " + APP_SUBTITLE, style="CardDim.TLabel").pack(
            side="left", padx=(10, 0)
        )
        self.header_state = ttk.Label(head, text="", style="CardDim.TLabel")
        self.header_state.pack(side="right")
        bar = ttk.Frame(outer, style="Raised.TFrame", padding=(14, 7))
        bar.pack(fill="x", side="bottom")
        self.progress = ttk.Progressbar(
            bar, mode="indeterminate", length=150, style="Horizontal.TProgressbar"
        )
        self.progress.pack(side="right", padx=(10, 0))
        self.status = ttk.Label(
            bar,
            text=f"{brand} only reads your system until you say otherwise. "
            "Nothing is ever sent anywhere.",
            style="CardDim.TLabel",
        )
        self.status.pack(side="left", fill="x", expand=True)
        self.notebook = ttk.Notebook(outer)
        self.notebook.pack(fill="both", expand=True, padx=10)
        self.tab_home = ttk.Frame(self.notebook, padding=14)
        self.tab_monitor = ttk.Frame(self.notebook, padding=14)
        self.tab_tools = ttk.Frame(self.notebook, padding=14)
        self.notebook.add(self.tab_home, text="  Fix my PC  ")
        self.notebook.add(self.tab_monitor, text="  Live latency  ")
        self.notebook.add(self.tab_tools, text="  Tools  ")
        self._build_home()
        self._build_monitor()
        self._build_tools()

    def _build_home(self) -> None:
        self.screens: dict[str, ttk.Frame] = {}
        self._screen_name = "start"
        for name in ("start", "analyzing", "results", "fixing", "done"):
            f = ttk.Frame(self.tab_home)
            self.screens[name] = f
        f = self.screens["start"]
        f.columnconfigure(0, weight=1)
        inner = ttk.Frame(f)
        inner.grid(row=0, column=0, sticky="nsew")
        f.rowconfigure(0, weight=1)
        inner.columnconfigure(0, weight=1)
        ttk.Label(inner, text="Is your game lagging?", style="H1.TLabel").grid(
            row=0, column=0, pady=(60, 6)
        )
        ttk.Label(
            inner,
            text="Checks your network, your drivers, your display and your "
            "Windows settings,\nthen tells you in plain words what is wrong - and "
            "fixes the parts it safely can.",
            style="Dim.TLabel",
            justify="center",
        ).grid(row=1, column=0, pady=(0, 34))
        self.btn_review = self._button(
            inner, "I allow to review my PC", self.on_review, big=True
        )
        self.btn_review.grid(row=2, column=0)
        ttk.Label(
            inner,
            text="Read-only review. No settings are changed until you approve them "
            "explicitly.",
            style="Dim.TLabel",
        ).grid(row=3, column=0, pady=(18, 0))
        f = self.screens["analyzing"]
        ttk.Label(f, text="Reviewing your PC", style="H1.TLabel").pack(pady=(70, 18))
        self.analyzing_note = ttk.Label(f, text="", style="Dim.TLabel")
        self.analyzing_note.pack()
        self.analyzing_bar = ttk.Progressbar(f, mode="indeterminate", length=340)
        self.analyzing_bar.pack(pady=(26, 0))
        f = self.screens["results"]
        f.columnconfigure(0, weight=1)
        f.rowconfigure(1, weight=1)
        self.results_title = ttk.Label(f, text="", style="H1.TLabel")
        self.results_title.grid(row=0, column=0, sticky="w", pady=(0, 8))
        wrap = ttk.Frame(f)
        wrap.grid(row=1, column=0, sticky="nsew")
        wrap.columnconfigure(0, weight=1)
        wrap.rowconfigure(0, weight=1)
        t = self.theme
        self.results_canvas = tk.Canvas(wrap, bg=t.bg, highlightthickness=0, bd=0)
        vsb = ttk.Scrollbar(wrap, orient="vertical", command=self.results_canvas.yview)
        self.results_body = ttk.Frame(self.results_canvas)
        self.results_body.bind(
            "<Configure>",
            lambda e: self.results_canvas.configure(
                scrollregion=self.results_canvas.bbox("all")
            ),
        )
        self.results_canvas.create_window((0, 0), window=self.results_body, anchor="nw")
        self.results_canvas.configure(yscrollcommand=vsb.set)
        self.results_canvas.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        self.results_canvas.bind(
            "<MouseWheel>",
            lambda e: self.results_canvas.yview_scroll(
                int(-1 * (e.delta / 120)), "units"
            ),
        )
        btns = ttk.Frame(f)
        btns.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        self.btn_continue = self._button(
            btns, "Continue", self.on_continue_results, kind="accent"
        )
        self.btn_continue.pack(side="left")
        self._button(
            btns, "Not now", lambda: self._show_screen("start")
        ).pack(side="left", padx=(10, 0))
        f = self.screens["fixing"]
        ttk.Label(f, text="Applying the fixes", style="H1.TLabel").pack(pady=(70, 18))
        self.fixing_note = ttk.Label(
            f, text="", style="Dim.TLabel", justify="center", wraplength=800
        )
        self.fixing_note.pack()
        self.fixing_bar = ttk.Progressbar(f, mode="indeterminate", length=340)
        self.fixing_bar.pack(pady=(26, 0))
        self.fixing_bar.start(14)
        f = self.screens["done"]
        f.columnconfigure(0, weight=1)
        f.rowconfigure(1, weight=1)
        self.done_title = ttk.Label(f, text="", style="H1.TLabel")
        self.done_title.grid(row=0, column=0, sticky="w", pady=(0, 8))
        wrap = ttk.Frame(f)
        wrap.grid(row=1, column=0, sticky="nsew")
        wrap.columnconfigure(0, weight=1)
        wrap.rowconfigure(0, weight=1)
        self.done_text = self._make_text(wrap, height=24)
        btns = ttk.Frame(f)
        btns.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        self._button(btns, "Copy this list", self.on_copy_manual).pack(side="left")
        self._button(
            btns, "Open the full report",
            lambda: self._open_file("report.html"),
        ).pack(side="left", padx=8)
        self._button(
            btns, "Open the apply log",
            lambda: self._open_file("apply-run.log"),
        ).pack(side="left")
        self._button(
            btns, "Undo applied changes", self.on_undo, kind="danger"
        ).pack(side="left", padx=8)
        self._button(
            btns, "Start over", lambda: self._show_screen("start"), kind="accent"
        ).pack(side="right")

    def _show_screen(self, name: str) -> None:
        for key, frame in self.screens.items():
            if key != name:
                frame.pack_forget()
        self.screens[name].pack(fill="both", expand=True)
        self.btn_continue.configure(state="normal")
        self._screen_name = name
        if name == "analyzing":
            self.analyzing_bar.start(14)
        else:
            try:
                self.analyzing_bar.stop()
            except tk.TclError:
                pass
        if name == "fixing":
            self.fixing_bar.start(14)
        else:
            try:
                self.fixing_bar.stop()
            except tk.TclError:
                pass

    def _entry(self, parent, textvariable, width: int) -> tk.Entry:
        t = self.theme
        return tk.Entry(
            parent, textvariable=textvariable, width=width, font=t.font_entry,
            relief="solid", borderwidth=1, bg=t.entry_bg, fg=t.entry_fg,
            insertbackground=t.entry_fg, highlightbackground=t.border,
            highlightthickness=1,
        )

    def _build_monitor(self) -> None:
        t = self.theme
        tab = self.tab_monitor
        tab.columnconfigure(0, weight=1)
        nums = ttk.Frame(tab, style="Raised.TFrame", padding=(16, 14))
        nums.grid(row=0, column=0, sticky="ew")
        for i in range(6):
            nums.columnconfigure(i, weight=1)
        self.mon_big = ttk.Label(nums, text="--", style="Big.TLabel")
        self.mon_big.grid(row=0, column=0, rowspan=2, sticky="w", padx=(0, 26))
        self.mon_stats: dict[str, ttk.Label] = {}
        for i, key in enumerate(("p50", "p95", "max", "loss", "jitter")):
            col = i + 1
            tk.Label(
                nums, text=key.upper(), bg=t.raised, fg=t.muted, font=t.font_bold
            ).grid(row=0, column=col, sticky="w")
            val = tk.Label(nums, text="--", bg=t.raised, fg=t.fg, font=t.font_h2)
            val.grid(row=1, column=col, sticky="w")
            self.mon_stats[key] = val
        self.mon_now_lbl = ttk.Label(nums, text="ms now", style="CardDim.TLabel")
        self.mon_now_lbl.grid(
            row=2, column=0, sticky="w"
        )
        self.spark = tk.Canvas(
            tab, height=170, bg=t.inset, highlightthickness=1,
            highlightbackground=t.border, bd=0,
        )
        self.spark.grid(row=1, column=0, sticky="nsew", pady=(12, 0))
        tab.rowconfigure(1, weight=1)
        chain = ttk.LabelFrame(
            tab, text="FULL CHAIN - CLICK TO SERVER TO SCREEN",
            style="Card.TLabelframe", padding=14,
        )
        chain.grid(row=2, column=0, sticky="ew", pady=(12, 0))
        tiles = ttk.Frame(chain, style="Raised.TFrame")
        tiles.pack(fill="x")
        for i in range(4):
            tiles.columnconfigure(i, weight=1)
        self.chain_vals: dict[str, Any] = {}
        for i, key in enumerate(("input", "render", "network")):
            cell = ttk.Frame(tiles, style="Raised.TFrame")
            cell.grid(row=0, column=i, sticky="w", padx=(0, 18))
            # The label names the statistic, because the three legs do not
            # report the same one: input is a p50 one-way, render and network
            # are p95. Reading them as one kind of number is what makes the
            # network tile look like it disagrees with the live watch.
            tk.Label(
                cell, text=CHAIN_TILE_LABELS[key], bg=t.raised, fg=t.muted,
                font=t.font_bold,
            ).pack(anchor="w")
            val = tk.Label(cell, text="--", bg=t.raised, fg=t.fg, font=t.font_h2)
            val.pack(anchor="w")
            self.chain_vals[key] = val
        total_cell = ttk.Frame(tiles, style="Raised.TFrame")
        total_cell.grid(row=0, column=3, sticky="e")
        tk.Label(
            total_cell, text="CHAIN TOTAL", bg=t.raised, fg=t.muted,
            font=t.font_bold,
        ).pack(anchor="e")
        self.chain_total = tk.Label(
            total_cell, text="--", bg=t.raised, fg=t.accent, font=t.font_big
        )
        self.chain_total.pack(anchor="e")
        self.chain_note = ttk.Label(
            chain, text="", style="CardDim.TLabel", justify="left",
            wraplength=980,
        )
        self.chain_note.pack(anchor="w", pady=(8, 0))
        watch = ttk.Frame(tab)
        watch.grid(row=3, column=0, sticky="ew", pady=(12, 0))
        ttk.Label(watch, text="Watch:").pack(side="left")
        self.var_mon_target = tk.StringVar(value="1.1.1.1")
        entry = self._entry(watch, self.var_mon_target, 28)
        entry.pack(side="left", padx=8)
        entry.bind("<Return>", lambda _e: self._restart_watch())
        entry.bind("<FocusOut>", lambda _e: self._restart_watch())
        self._watch_started = None
        self._watch_session = 0
        ttk.Label(
            watch,
            text="p95 and max describe lag. A steady average with a high max "
            "means spikes, not speed.",
            style="Dim.TLabel",
        ).pack(side="left", padx=(16, 0))
        brow = ttk.Frame(tab)
        brow.grid(row=4, column=0, sticky="ew", pady=(10, 0))
        self.btn_chain = self._button(
            brow, "Measure full chain", self.on_measure_chain, kind="accent"
        )
        self.btn_chain.pack(side="left")
        ttk.Label(
            brow,
            text="Input: synthetic click to hook dispatch. Render: present to "
            "vblank on a test surface. Network: ping p95. The total is an "
            "estimate - panel scan-out and USB wire time need a camera.",
            style="Dim.TLabel",
        ).pack(side="left", padx=14)

    def _build_tools(self) -> None:
        t = self.theme
        tab = self.tab_tools
        tab.columnconfigure(0, weight=1)
        mem = ttk.LabelFrame(tab, text="MEMORY", style="Card.TLabelframe", padding=14)
        mem.grid(row=0, column=0, sticky="ew")
        row = ttk.Frame(mem, style="Raised.TFrame")
        row.pack(fill="x")
        self.btn_memclean = self._button(
            row, "Free up memory now", self.on_memory_clean, kind="accent"
        )
        self.btn_memclean.pack(side="left")
        ttk.Label(
            row,
            text="One click. Empties the standby list, flushes modified pages and "
            "trims working sets - every area a RAM cleaner touches.",
            style="CardDim.TLabel",
        ).pack(side="left", padx=14)
        self.mem_result = ttk.Label(
            mem, text="", style="CardDim.TLabel", justify="left", wraplength=980
        )
        self.mem_result.pack(anchor="w", pady=(10, 0))
        timer = ttk.LabelFrame(
            tab, text="SYSTEM TIMER", style="Card.TLabelframe", padding=14,
        )
        timer.grid(row=1, column=0, sticky="ew", pady=(12, 0))
        trow = ttk.Frame(timer, style="Raised.TFrame")
        trow.pack(fill="x")
        self.var_timer = tk.BooleanVar(value=False)
        self.chk_timer = self._check(
            trow, "Hold a 0.5 ms system tick while SnappyAI runs",
            self.var_timer, surface="card",
        )
        self.chk_timer.pack(side="left")
        # kit.bool_switch doesn't take command=; trace the variable instead
        self.var_timer.trace_add("write", lambda *_a: self.on_timer_toggle())
        ttk.Label(
            trow,
            text="Raises the clock interrupt frequency so sub-millisecond waits "
            "are not rounded up to 15.6 ms. Released automatically on exit; "
            "raises idle power draw.",
            style="CardDim.TLabel",
            wraplength=620,
            justify="left",
        ).pack(side="left", padx=14)
        self.timer_result = ttk.Label(
            timer, text="", style="CardDim.TLabel", justify="left", wraplength=980
        )
        self.timer_result.pack(anchor="w", pady=(10, 0))
        gen = ttk.LabelFrame(
            tab, text="SCRIPTS AND MAINTENANCE", style="Card.TLabelframe",
            padding=14,
        )
        gen.grid(row=2, column=0, sticky="ew", pady=(12, 0))
        self.var_del = tk.BooleanVar(value=False)
        self._check(gen, "Cleanup scripts may delete files (otherwise they only report "
            "what could be deleted)",
            self.var_del, surface="card").pack(anchor="w")
        self.var_removals = tk.BooleanVar(value=False)
        self._check(gen, "Include irreversible removals (Edge, Xbox overlay, OneDrive, "
            "Outlook) in the debloat script",
            self.var_removals, surface="card").pack(anchor="w", pady=(6, 0))
        row = ttk.Frame(gen, style="Raised.TFrame")
        row.pack(fill="x", pady=(12, 0))
        self._button(row, "Cleanup script", self.on_gen_cleanup).pack(side="left")
        self._button(row, "Redistributables script", self.on_gen_redists).pack(
            side="left", padx=8
        )
        self._button(row, "Debloat script", self.on_gen_debloat).pack(side="left")
        self._button(row, "Memory script", self.on_gen_memory).pack(
            side="left", padx=8
        )
        self._button(row, "Power script", self.on_gen_power).pack(side="left")
        self._button(row, "Registry payload...", self.on_registry).pack(
            side="left", padx=(8, 0)
        )
        self.script_result = ttk.Label(
            gen, text="No script generated yet.", style="CardDim.TLabel",
            justify="left", wraplength=980,
        )
        self.script_result.pack(anchor="w", pady=(10, 0))
        runrow = ttk.Frame(gen, style="Raised.TFrame")
        runrow.pack(fill="x", pady=(8, 0))
        self.btn_run_script = self._button(
            runrow, "Run the generated script silently", self.on_run_script,
            kind="accent",
        )
        self.btn_run_script.pack(side="left")
        self.btn_run_script.configure(state="disabled")
        ttk.Label(
            runrow,
            text="Runs hidden: no terminal opens. A confirmation popup comes first.",
            style="CardDim.TLabel",
        ).pack(side="left", padx=14)
        diag = ttk.LabelFrame(
            tab, text="IF SOMETHING WENT WRONG", style="Card.TLabelframe",
            padding=14,
        )
        diag.grid(row=3, column=0, sticky="ew", pady=(12, 0))
        row = ttk.Frame(diag, style="Raised.TFrame")
        row.pack(fill="x")
        self._button(
            row, "Create a diagnosis log", self.on_diagnose, kind="accent"
        ).pack(side="left")
        self._button(row, "Open output folder", self.on_open_outdir).pack(
            side="left", padx=10
        )
        ttk.Label(
            row,
            text="The log is a plain text file you can paste anywhere. It holds no "
            "passwords and no personal files.",
            style="CardDim.TLabel",
        ).pack(side="left", padx=10)

    def _make_text(self, parent, height: int = 12) -> tk.Text:
        t = self.theme
        wrap = ttk.Frame(parent)
        wrap.pack(fill="both", expand=True)
        txt = tk.Text(
            wrap, height=height, wrap="word", bg=t.inset, fg=t.fg,
            insertbackground=t.fg, relief="flat", font=t.font, padx=12, pady=10,
            selectbackground=t.accent, selectforeground=t.bg, state="disabled",
            borderwidth=0, highlightthickness=1, highlightbackground=t.border,
            highlightcolor=t.accent, spacing3=4,
        )
        vsb = ttk.Scrollbar(wrap, orient="vertical", command=txt.yview)
        txt.configure(yscrollcommand=vsb.set)
        txt.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        wrap.rowconfigure(0, weight=1)
        wrap.columnconfigure(0, weight=1)
        for tag, colour in (
            ("h", t.accent), ("good", t.good), ("warn", t.warn),
            ("bad", t.bad), ("dim", t.muted), ("head", t.fg),
        ):
            txt.tag_configure(tag, foreground=colour)
        txt.tag_configure("h", font=t.font_h2, spacing1=10, spacing3=4)
        txt.tag_configure("dim", font=(t.font[0], 9))
        txt.tag_configure("head", font=t.font_bold)
        return txt

    @staticmethod
    def _write(txt: tk.Text, blocks: list[tuple[str, str]]) -> None:
        txt.configure(state="normal")
        txt.delete("1.0", "end")
        for tag, content in blocks:
            txt.insert("end", content + "\n", tag)
        txt.configure(state="disabled")

    def _pump(self) -> None:
        self.pump_once()
        self.root.after(POLL_MS, self._pump)

    def pump_once(self) -> None:
        events = self.ctl.poll()
        # The watch ticks ~10x/s; only the NEWEST tick carries information
        # (its series/stats supersede the older ones), so older ticks in the
        # same batch are dropped instead of redrawing the spark per event.
        last_tick = -1
        for i, ev in enumerate(events):
            if ev.kind is EventType.MONITOR_TICK:
                last_tick = i
        for i, ev in enumerate(events):
            if ev.kind is EventType.MONITOR_TICK and i != last_tick:
                continue
            try:
                self._handle(ev)
            except Exception as exc:
                self.status.configure(
                    text=f"Internal error while updating the window: {exc}"
                )

    def _handle(self, ev) -> None:
        k = ev.kind
        if k is EventType.BUSY:
            self.header_state.configure(text=f"working: {ev.message}")
            self.progress.start(12)
        elif k is EventType.IDLE:
            self.header_state.configure(text="")
            self.progress.stop()
        elif k is EventType.PROGRESS:
            self.status.configure(text=ev.message)
            if self._current_screen() == "analyzing":
                self.analyzing_note.configure(text=ev.message)
            elif self._current_screen() == "fixing":
                self.fixing_note.configure(text=ev.message)
        elif k is EventType.LOG:
            self.status.configure(text=ev.message)
        elif k is EventType.SCAN_DONE:
            self._show_results()
        elif k is EventType.APPLY_DONE:
            self._show_done(ev.data or {})
        elif k is EventType.UNDO_DONE:
            self._show_undo(ev.data or {})
        elif k is EventType.MONITOR_TICK:
            self._render_tick(ev.data or {})
        elif k is EventType.CHAIN_DONE:
            self._render_chain(ev.data or {})
        elif k is EventType.MEMORY_CLEAN_DONE:
            self._render_memory(ev.data or {})
        elif k is EventType.TIMER_DONE:
            self.status.configure(text=ev.message)
            self._render_timer(ev.data or {})
        elif k is EventType.SCRIPT_DONE:
            d = ev.data or {}
            if "exit_code" in d:
                self._show_run_result(d)
            elif str(d.get("path", "")).lower().endswith(".ps1"):
                self.last_script_path = d.get("path", "")
                self.script_result.configure(
                    text=f"{ev.message}\n{d.get('path', '')}\n"
                    "Review the file, then run it with the button below."
                )
                self.btn_run_script.configure(state="normal")
                self.status.configure(text=f"{ev.message}  ->  {d.get('path', '')}")
            else:
                self.status.configure(text=f"{ev.message}  ->  {d.get('path', '')}")
        elif k is EventType.ERROR:
            self.status.configure(text=f"Error: {ev.message}")
            messagebox.showerror(self.ctl.brand, ev.message)

    def _current_screen(self) -> str:
        return self._screen_name

    def on_review(self) -> None:
        if not messagebox.askyesno(self.ctl.brand, CONSENT_TEXT):
            return
        self._show_screen("analyzing")
        self.analyzing_note.configure(text="reading your system")
        self.ctl.start_scan(experimental=False, with_maintenance=False)

    def _show_results(self) -> None:
        brand = self.ctl.brand
        t = self.theme
        small = (t.font[0], 9)
        small_b = (t.font[0], 9, "bold")
        scan = self.ctl.last_scan
        if scan is None:
            self._show_screen("start")
            return
        groups = classify_scan(scan, brand=brand)
        self._groups = groups
        n_auto, n_manual = len(groups["auto"]), len(groups["manual"])
        n_total = n_auto + n_manual
        for child in self.results_body.winfo_children():
            child.destroy()
        r = 0

        def heading(text: str, colour: str) -> None:
            nonlocal r
            tk.Label(
                self.results_body, text=text, bg=t.bg, fg=colour, font=t.font_h2,
                anchor="w",
            ).grid(row=r, column=0, sticky="ew", pady=(14 if r else 0, 6))
            r += 1

        def card(pf: PlainFinding) -> None:
            nonlocal r
            box = ttk.Frame(self.results_body, style="Card.TFrame", padding=(14, 10))
            box.grid(row=r, column=0, sticky="ew", pady=3)
            box.columnconfigure(0, weight=1)
            tk.Label(
                box, text=pf.headline, bg=t.raised, fg=t.fg, font=t.font_bold,
                anchor="w",
            ).grid(row=0, column=0, sticky="w")
            tk.Label(
                box, text=pf.detail, bg=t.raised, fg=t.muted, font=small,
                anchor="w", justify="left", wraplength=880,
            ).grid(row=1, column=0, sticky="w", pady=(3, 0))
            tk.Label(
                box, text=pf.fix_text, bg=t.raised,
                fg=t.good if pf.fix_kind == "auto" else t.warn,
                font=small_b, anchor="w",
            ).grid(row=2, column=0, sticky="w", pady=(4, 0))
            r += 1

        if n_total == 0:
            self.results_title.configure(
                text=f"No problems found that {brand} can act on."
            )
            heading(
                f"Everything checked looks fine ({len(groups['fine'])} checks)",
                t.good,
            )
        else:
            self.results_title.configure(
                text=f"{n_total} thing(s) are costing you performance"
            )
        if groups["auto"]:
            heading(f"{brand} can fix these for you ({n_auto})", t.accent)
            for pf in groups["auto"]:
                card(pf)
        if groups["manual"]:
            heading(f"These need your own hands ({n_manual})", t.warn)
            for pf in groups["manual"]:
                card(pf)
        if groups["unsure"]:
            heading("Could not be checked without more measurement", t.warn)
            for pf in groups["unsure"]:
                tk.Label(
                    self.results_body,
                    text="•  " + pf.headline + " - " + pf.detail, bg=t.bg,
                    fg=t.muted, font=small, anchor="w", justify="left",
                    wraplength=940,
                ).grid(row=r, column=0, sticky="ew", pady=2)
                r += 1
        if groups["fine"] and n_total:
            heading(f"Already fine ({len(groups['fine'])} checks)", t.good)
        self.results_canvas.configure(scrollregion=self.results_canvas.bbox("all"))
        self._show_screen("results")
        if n_total:
            if messagebox.askyesno(
                brand, issues_message(n_total, n_auto, n_manual, brand=brand)
            ):
                self._ask_apply()
            else:
                self._show_screen("start")
        else:
            self.btn_continue.configure(state="disabled")

    def _ask_apply(self) -> None:
        if not messagebox.askyesno(self.ctl.brand, apply_confirm_text(self.ctl.brand)):
            self._show_screen("start")
            return
        self._show_screen("fixing")
        self.fixing_note.configure(
            text="running the generated script silently, elevated - "
            "no terminal window opens"
        )
        self.ctl.start_apply()

    def on_continue_results(self) -> None:
        groups = getattr(self, "_groups", None)
        if not groups:
            return
        n_auto, n_manual = len(groups["auto"]), len(groups["manual"])
        if messagebox.askyesno(
            self.ctl.brand,
            issues_message(n_auto + n_manual, n_auto, n_manual,
                           brand=self.ctl.brand),
        ):
            self._ask_apply()
        else:
            self._show_screen("start")

    def _show_done(self, data: dict[str, Any]) -> None:
        brand = self.ctl.brand
        code = data.get("exit_code")
        ok = code == 0
        self.done_title.configure(
            text="Done - here is what still needs you"
            if ok
            else f"The automatic fixes stopped with exit code {code}"
        )
        blocks: list[tuple[str, str]] = []
        if not ok:
            blocks.append(("bad", "The apply script reported a failure. Its log:"))
            blocks.append(("dim", (data.get("log") or "(no output)")[-3000:]))
            blocks.append(("", ""))
        blocks.append(("h", f"What {brand} changed"))
        scan = self.ctl.last_scan
        groups = classify_scan(scan, brand=brand) if scan else {"auto": []}
        if groups["auto"]:
            for pf in groups["auto"]:
                blocks.append(("good", "•  " + pf.headline))
        else:
            blocks.append(("dim", "nothing was applied"))
        blocks.append(("h", "What you need to do yourself"))
        steps = manual_instructions(scan) if scan else []
        if steps:
            for i, st in enumerate(steps, 1):
                blocks.append(("head", f"{i}. {st['title']}"))
                for line in st["instruction"].split("\n"):
                    blocks.append(("", "   " + line))
                blocks.append(("", ""))
        else:
            blocks.append(("dim", "nothing - every finding was automatic"))
        blocks.append(("h", "After you are done"))
        blocks.append(
            (
                "",
                "Run Measure full chain again on the Live latency tab and compare "
                "the totals. If a change did not measurably help, undo it "
                "with the button below: undo.ps1 restores your previous values.",
            )
        )
        self._manual_blocks = blocks
        self._write(self.done_text, blocks)
        self._show_screen("done")

    def _show_undo(self, data: dict[str, Any]) -> None:
        brand = self.ctl.brand
        code = data.get("exit_code")
        if code == 0:
            messagebox.showinfo(
                brand,
                "Undo complete: every recorded change was restored from the snapshot.",
            )
            self.status.configure(text="undo complete: prior values restored")
        else:
            messagebox.showerror(
                brand,
                f"Undo stopped with exit code {code}.\n\n"
                f"{(data.get('log') or '')[-1500:]}\n\n"
                f"Full log: {data.get('log_path', '')}",
            )
            self.status.configure(text=f"undo failed with exit code {code}")

    def _show_run_result(self, data: dict[str, Any]) -> None:
        brand = self.ctl.brand
        code = data.get("exit_code")
        if code == 0:
            messagebox.showinfo(
                brand,
                f"Script finished successfully.\n\nLog: {data.get('log_path', '')}",
            )
        else:
            messagebox.showerror(
                brand,
                f"Script stopped with exit code {code}.\n\n"
                f"{(data.get('log') or '')[-1500:]}\n\n"
                f"Full log: {data.get('log_path', '')}",
            )
        self.status.configure(
            text=f"{data.get('script', 'script')} finished with exit code {code}"
        )

    def on_undo(self) -> None:
        if not messagebox.askyesno(
            self.ctl.brand, undo_confirm_text(self.ctl.brand)
        ):
            return
        self._show_screen("fixing")
        self.fixing_note.configure(
            text="running the undo script silently - no terminal window opens"
        )
        self.ctl.start_undo()

    def on_copy_manual(self) -> None:
        lines = []
        for _tag, text in getattr(self, "_manual_blocks", []):
            if text.strip():
                lines.append(text)
        self.root.clipboard_clear()
        self.root.clipboard_append("\n".join(lines))
        self.status.configure(text="Manual steps copied to the clipboard.")

    def _sync_chain_btn(self) -> None:
        try:
            want = "Stop" if self.ctl.monitoring else "Measure full chain"
            if str(self.btn_chain.cget("text")) != want:
                self.btn_chain.configure(text=want)
        except (AttributeError, tk.TclError):
            pass

    def _start_watch(self, target: str) -> None:
        self.ctl.start_monitor(target, interval=0.1)
        self._watch_started = time.monotonic()
        self._watch_session += 1
        sess = self._watch_session
        self.mon_big.configure(text="--")
        self.mon_now_lbl.configure(text="ms now")
        self._draw_spark([])
        self._sync_chain_btn()
        if hasattr(self, "status"):
            self.status.configure(
                text=f"measuring {target} for {WATCH_SECONDS:g}s (one sample "
                     f"every 0.1s) - press Stop to end early; the graph and "
                     f"stats stay on screen when it finishes")
        try:
            self.root.after(
                int(WATCH_SECONDS * 1000),
                lambda: self._finish_watch(sess, timed_out=True))
        except tk.TclError:
            pass

    def _finish_watch(self, sess: int, timed_out: bool) -> None:
        """End the timed run. A stale timer (superseded by a newer session)
        or an already-stopped monitor is ignored, so restarting mid-run
        cannot have the old timer kill the new measurement. The graph and
        the statistics deliberately STAY on screen: the run is over, its
        result is what the user came for."""
        if sess != self._watch_session or not self.ctl.monitoring:
            return
        self.ctl.stop_monitor()
        elapsed = 0.0
        if self._watch_started is not None:
            elapsed = time.monotonic() - self._watch_started
        self._watch_started = None
        self._sync_chain_btn()
        try:
            self.mon_now_lbl.configure(
                text="ms now - measured %.0fs (%s)"
                     % (elapsed, "complete" if timed_out else "stopped early"))
        except tk.TclError:
            pass
        if hasattr(self, "status"):
            if timed_out:
                self.status.configure(
                    text="measurement complete: %.1f s of samples - the graph "
                         "and stats above stay; press the button again for a "
                         "new run" % elapsed)
            else:
                self.status.configure(
                    text="watch stopped after %.1f s - the graph and stats "
                         "above stay" % elapsed)

    def _stop_watch(self) -> None:
        self._finish_watch(self._watch_session, timed_out=False)

    def _restart_watch(self) -> None:
        """Target-field commit (Enter / focus-out): (re)watch the new target
        without triggering a chain run."""
        target = self.var_mon_target.get().strip()
        if not target:
            if self.ctl.monitoring:
                self._stop_watch()
            if hasattr(self, 'status'):
                self.status.configure(text="Enter an IP to watch.")
            return
        self._start_watch(target)

    def _render_tick(self, d: dict[str, Any]) -> None:
        if d.get("error"):
            self.mon_big.configure(text="--")
            self.status.configure(text=f"monitor: {d['error']}")
            return
        cur = d.get("current")
        self.mon_big.configure(text=f"{cur:.1f}" if cur is not None else "--")
        if self._watch_started is not None:
            el = int(time.monotonic() - self._watch_started)
            try:
                self.mon_now_lbl.configure(
                    text="ms now - measuring %ds / %gs" % (el, WATCH_SECONDS))
            except tk.TclError:
                pass
        p50 = d.get("p50")
        p95 = d.get("p95")
        mx = d.get("max")
        jt = d.get("jitter")
        self.mon_stats["p50"].configure(text=f"{p50:.2f}" if p50 else "--")
        self.mon_stats["p95"].configure(text=f"{p95:.2f}" if p95 else "--")
        self.mon_stats["max"].configure(text=f"{mx:.2f}" if mx else "--")
        self.mon_stats["loss"].configure(text=f"{d.get('loss_pct', 0.0):.1f}%")
        self.mon_stats["jitter"].configure(text=f"{jt:.2f}" if jt else "--")
        series = d.get("series") or []
        self._last_series = list(series)
        self._draw_spark(series)

    def _draw_spark(self, series: list[float]) -> None:
        t = self.theme
        c = self.spark
        c.delete("all")
        c.update_idletasks()
        w = max(50, c.winfo_width())
        h = max(40, c.winfo_height())
        if len(series) < 2:
            c.create_text(
                w / 2, h / 2,
                text="waiting for a second sample..." if series
                else "no samples yet",
                fill=t.muted, font=t.font,
            )
            return
        top = max(series) * 1.15 or 1.0
        bottom = min(series) * 0.85
        span = max(0.5, top - bottom)
        n = len(series)
        pts = []
        for i, v in enumerate(series):
            x = (i / max(1, n - 1)) * (w - 12) + 6
            y = h - 10 - ((v - bottom) / span) * (h - 24)
            pts.extend((x, y))
        c.create_line(*pts, fill=t.accent, width=2, smooth=False)
        c.create_text(
            8, 8, anchor="nw", fill=t.muted, font=t.font_mono,
            text=f"min {min(series):.1f}  max {max(series):.1f}  "
            f"now {series[-1]:.1f} ms",
        )

    def on_measure_chain(self) -> None:
        """The ONE button: first press starts the live watch (top values +
        graph) AND the full-chain snapshot; while the watch runs the button
        is a Stop for the live measurement."""
        if self.ctl.monitoring:
            self._stop_watch()
            return
        brand = self.ctl.brand
        if not messagebox.askyesno(brand, chain_confirm_text(20, brand)):
            return
        target = self.var_mon_target.get().strip() or "1.1.1.1"
        self._start_watch(target)
        path = self.ctl.out_root / "chain.json"
        self.chain_note.configure(
            text="measuring: input, then render, then network... "
                 "(the live graph keeps running while the chain measures)")
        self.ctl.start_chain(target=target, save_path=path)

    def _render_chain(self, d: dict[str, Any]) -> None:
        brand = self.ctl.brand
        iv = d.get("input") or {}
        rv = d.get("render") or {}
        nv = d.get("network") or {}
        one = iv.get("p50_one_way_ms")
        rp = rv.get("p95_ms")
        np_ = nv.get("p95")
        self.chain_vals["input"].configure(
            text=f"{one:.2f} ms" if one is not None else "--"
        )
        self.chain_vals["render"].configure(
            text=f"{rp:.1f} ms" if rp is not None else "--"
        )
        self.chain_vals["network"].configure(
            text=f"{np_:.1f} ms" if np_ is not None else "--"
        )
        total = (d.get("chain") or {}).get("total_ms")
        self.chain_total.configure(
            text=f"{total:.1f} ms" if total is not None else "--"
        )
        if total is None:
            notes = []
            for leg, payload in (("input", iv), ("render", rv), ("network", nv)):
                err = payload.get("error")
                if err:
                    notes.append(f"{leg}: {err}")
            self.chain_note.configure(
                text="Chain incomplete - " + "; ".join(notes)
                if notes else "Chain incomplete."
            )
            messagebox.showwarning(
                brand,
                "The full chain could not be measured:\n\n"
                + "\n".join(notes)
                + f"\n\nFull result: {d.get('saved_to', '')}",
            )
        else:
            dur = d.get("duration_s")
            dur_txt = f" Measured over {dur:.1f} s." if dur else ""
            self.chain_note.configure(
                text=f"chain {total:.1f} ms = input {one:.2f} + render {rp:.1f} + "
                f"network {np_:.1f}.{dur_txt} Saved to {d.get('saved_to', '')}. "
                "Estimate only - see the method notes."
            )

    def on_memory_clean(self) -> None:
        if not messagebox.askyesno(
            self.ctl.brand,
            "Free up memory now? Windows will page trimmed memory back on demand; "
            "expect a short burst of disk activity afterwards.",
        ):
            return
        self.mem_result.configure(text="working...")
        self.ctl.start_memory_clean()

    def _render_memory(self, d: dict[str, Any]) -> None:
        before, after = d.get("before") or {}, d.get("after") or {}
        areas = d.get("areas") or []
        avail_b = before.get("Available MBytes", next(iter(before.values()), None))
        avail_a = after.get("Available MBytes", next(iter(after.values()), None))
        lines = ["Freed memory report:"]
        if avail_b is not None and avail_a is not None:
            lines.append(f"  available: {avail_b} MB before -> {avail_a} MB after")
        for a in areas:
            lines.append(f"  {a.get('area')}: {a.get('status')}")
        lines.append("")
        lines.append(d.get("caveat", ""))
        self.mem_result.configure(text="\n".join(lines))

    def on_timer_toggle(self) -> None:
        want = self.var_timer.get()
        if want:
            self.timer_result.configure(text="requesting a finer tick...")
            started = self.ctl.start_timer_request(500.0)
        else:
            self.timer_result.configure(text="releasing...")
            started = self.ctl.start_timer_stop()
        if not started:
            # Controller was busy; the request never ran, so put the checkbox
            # back to the real state instead of leaving a false indicator.
            self.var_timer.set(self.ctl.timer_active)
            self.timer_result.configure(
                text="busy - wait for the current operation, then try again"
            )
            return
        if self._on_timer_persist:
            self._on_timer_persist(500 if want else 0)

    def _render_timer(self, d: dict[str, Any]) -> None:
        lines: list[str] = []
        if d.get("error"):
            lines.append(f"  {d['error']}")
        actual = d.get("actual_us")
        if actual:
            lines.append(f"  request active: {actual / 1000.0:.4f} ms "
                         f"(for SnappyAI's own waits; process-scoped on "
                         f"Windows 10 2004+)")
            if d.get("clamped"):
                lines.append(
                    f"  (0.5 ms not reachable; platform floor is "
                    f"{(d.get('min_us') or 0) / 1000.0:.4f} ms)"
                )
        elif d.get("current_us") and not d.get("active"):
            lines.append(f"  released; tick now "
                         f"{d['current_us'] / 1000.0:.4f} ms")
        thr = d.get("throttling") or {}
        if thr.get("attempted") and not thr.get("ok"):
            lines.append(
                "  note: background-throttling opt-out unavailable, so Windows may "
                "drop the request while this window is minimized"
            )
        self.timer_result.configure(text="\n".join(lines))
        # The controller reports the authoritative holder state; keep the
        # checkbox honest even if the async request failed after the click.
        self.var_timer.set(bool(d.get("active")))

    def on_gen_cleanup(self) -> None:
        self.ctl.start_generate(kind="cleanup", execute=self.var_del.get())

    def on_gen_redists(self) -> None:
        self.ctl.start_generate(kind="redists")

    def on_gen_debloat(self) -> None:
        selected = None
        if not self.var_removals.get():
            selected = tuple(
                o["id"] for o in self.ctl.debloat_options() if o["reversible"]
            )
        self.ctl.start_generate(kind="debloat", selected=selected)

    def on_gen_memory(self) -> None:
        self.ctl.start_generate(kind="memory", selected=("memory.measure",))

    def on_gen_power(self) -> None:
        self.ctl.start_generate(kind="power")

    def on_registry(self) -> None:
        path = filedialog.askopenfilename(
            title="Choose a registry payload JSON",
            filetypes=[("JSON payload", "*.json"), ("All files", "*.*")],
        )
        if not path:
            return
        self.status.configure(text=f"validating {path}...")
        self.ctl.start_registry(payload=path)

    def on_run_script(self) -> None:
        path = self.last_script_path
        if not path:
            messagebox.showwarning(self.ctl.brand, "Generate a script first.")
            return
        if not messagebox.askyesno(
            self.ctl.brand, run_confirm_text(path, self.ctl.brand)
        ):
            return
        self.status.configure(text=f"running {path} silently...")
        self.ctl.start_run_script(path)

    def on_diagnose(self) -> None:
        self.ctl.start_diagnose()

    def on_open_outdir(self) -> None:
        self._open_path(self.ctl.out_root)

    def _open_file(self, name: str) -> None:
        target = (
            self.ctl.last_scan.out_dir if self.ctl.last_scan else self.ctl.out_root
        )
        path = Path(target) / name
        if not path.exists():
            messagebox.showwarning(
                self.ctl.brand, f"{path.name} does not exist yet."
            )
            return
        self._open_path(path)

    def _open_path(self, path: Path) -> None:
        import os
        import subprocess
        import sys

        path = Path(path)
        if not path.exists():
            messagebox.showwarning(self.ctl.brand, f"{path} does not exist yet.")
            return
        try:
            if sys.platform.startswith("win"):
                os.startfile(str(path))
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except Exception as exc:
            messagebox.showerror(self.ctl.brand, f"Could not open {path}: {exc}")


def build_app(
    root: tk.Tk, *, backend: str = "auto", out_root: str | Path | None = None
) -> SnappyAIApp:
    ctl = GuiController(backend_name=backend, out_root=out_root)
    return SnappyAIApp(root, ctl)


def main(argv: list[str] | None = None) -> int:
    import argparse
    import os

    p = argparse.ArgumentParser(
        prog="latency-gui", description="latency graphical interface"
    )
    p.add_argument(
        "--backend", choices=("auto", "fake"), default="auto",
        help="'fake' drives the whole UI from the built-in Windows fixture",
    )
    p.add_argument("--out", default=None, help="output root directory")
    args = p.parse_args(argv)
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        import sys

        print(f"latency-gui: cannot open a display: {exc}", file=sys.stderr)
        return 1
    app = build_app(root, backend=args.backend, out_root=args.out)
    if args.backend == "fake":
        app.status.configure(
            text="DEMO MODE: built-in fixture, not this machine. "
            "Nothing here is your PC."
        )
    if os.environ.get("LATTOOL_GUI_SMOKE"):
        for _ in range(5):
            root.update()
        root.destroy()
        return 0
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
