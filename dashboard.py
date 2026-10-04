"""
Single-window dashboard UI with real-time memory telemetry, continuous live refresh,
and active process management (End Task, manual Trim, context menu, process inspection).

New in this version:
  - CPU% column in process table
  - RAM history sparkline (pure Tkinter Canvas, no extra dependencies)
  - 'Trim All Over Limit' button
  - 'Export Log' to file
  - Tooltips on metric cards
  - Keyboard shortcuts: Ctrl+R refresh, Ctrl+F focus filter, Ctrl+P pause/resume

Runs Tk mainloop on the main thread; monitor runs on a background thread and
communicates safely through AppState.
"""
import collections
import os
import subprocess
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

# pyrefly: ignore [missing-import]
from backend import get_backend
# pyrefly: ignore [missing-import]
from config import load_config, save_config
# pyrefly: ignore [missing-import]
from monitor import MonitorLoop, PROTECTED_SYSTEM_PROCESSES
# pyrefly: ignore [missing-import]
from status import AppState, SystemMetrics
# pyrefly: ignore [missing-import]
from game_ready import run_game_ready, restore_game_ready, GameReadyResult

POLL_MS = 250         # UI polling interval to check for event drain & state updates
SPARKLINE_POINTS = 120  # ~2 minutes of history at 1s refresh


# ---------------------------------------------------------------------------
# Tooltip helper (pure stdlib, no extra deps)
# ---------------------------------------------------------------------------

class _Tooltip:
    """Minimal hover tooltip that attaches to any Tk widget."""

    def __init__(self, widget, text: str):
        self._widget = widget
        self._text = text
        self._tip_window = None
        widget.bind("<Enter>", self._show)
        widget.bind("<Leave>", self._hide)

    def _show(self, _event=None):
        if self._tip_window:
            return
        x = self._widget.winfo_rootx() + 4
        y = self._widget.winfo_rooty() + self._widget.winfo_height() + 2
        self._tip_window = tw = tk.Toplevel(self._widget)
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f"+{x}+{y}")
        tk.Label(
            tw, text=self._text, justify="left",
            background="#fffbe6", relief="solid", borderwidth=1,
            font=("Segoe UI", 8), wraplength=280, padx=6, pady=4
        ).pack()

    def _hide(self, _event=None):
        if self._tip_window:
            self._tip_window.destroy()
            self._tip_window = None


# ---------------------------------------------------------------------------
# RAM sparkline canvas widget
# ---------------------------------------------------------------------------

class _Sparkline(tk.Canvas):
    """Pure-Tkinter canvas that draws a rolling RAM usage history line chart.
    No matplotlib or other dependencies required.
    """

    def __init__(self, parent, maxpoints: int = SPARKLINE_POINTS, **kwargs):
        kwargs.setdefault("height", 52)
        kwargs.setdefault("bg", "#f6f8fa")
        kwargs.setdefault("highlightthickness", 1)
        kwargs.setdefault("highlightbackground", "#d0d7de")
        super().__init__(parent, **kwargs)
        self._data: collections.deque = collections.deque(maxlen=maxpoints)
        self._maxpoints = maxpoints
        self.bind("<Configure>", lambda _e: self._redraw())

    def push(self, percent: float):
        """Add a new data point (0–100) and redraw."""
        self._data.append(max(0.0, min(100.0, percent)))
        self._redraw()

    def _redraw(self):
        self.delete("all")
        w = self.winfo_width()
        h = self.winfo_height()
        if w < 4 or h < 4 or not self._data:
            return

        pad_top, pad_bot, pad_left, pad_right = 4, 4, 2, 2
        draw_w = w - pad_left - pad_right
        draw_h = h - pad_top - pad_bot

        # Grid lines at 25 / 50 / 75%
        for pct in (25, 50, 75):
            y = pad_top + draw_h * (1.0 - pct / 100.0)
            self.create_line(pad_left, y, w - pad_right, y,
                             fill="#e0e0e0", dash=(2, 4))

        pts = list(self._data)
        n = len(pts)
        if n < 2:
            return

        # Build polygon points (filled area under the line)
        coords = []
        for i, v in enumerate(pts):
            x = pad_left + (i / (self._maxpoints - 1)) * draw_w
            y = pad_top + draw_h * (1.0 - v / 100.0)
            coords.extend([x, y])

        # Close the polygon at the bottom
        x_last = pad_left + ((n - 1) / (self._maxpoints - 1)) * draw_w
        coords.extend([x_last, pad_top + draw_h, pad_left, pad_top + draw_h])

        self.create_polygon(coords, fill="#d0e8ff", outline="", smooth=True)

        # Outline line on top
        line_coords = coords[:n * 2]
        self.create_line(line_coords, fill="#0969da", width=1.5, smooth=True)

        # Current value label
        cur = pts[-1]
        self.create_text(
            w - pad_right - 2, pad_top + 2,
            text=f"{cur:.0f}%", anchor="ne",
            font=("Segoe UI", 7, "bold"), fill="#0969da"
        )


# ---------------------------------------------------------------------------
# Main dashboard window
# ---------------------------------------------------------------------------

class Dashboard(tk.Tk):
    def __init__(self, app_state: AppState, is_windows: bool, is_admin: bool):
        super().__init__()
        self.app_state = app_state
        self.is_windows = is_windows
        self.is_admin = is_admin

        self.title("RAM Cutter — Real-Time Memory Monitor")
        self.geometry("920x820")
        self.minsize(800, 620)

        self.config_data = load_config(app_state=self.app_state)
        self.backend = get_backend()
        self.psutil = __import__("psutil")

        self.monitor = MonitorLoop(
            self.config_data, self.backend, self.psutil, app_state=self.app_state
        )
        self._monitor_thread = threading.Thread(target=self.monitor.run_forever, daemon=True)

        self._last_seen_seq = -1
        self.sort_col = "private"
        self.sort_reverse = True
        self._current_rendered_procs = {}
        self.view_mode_var = tk.StringVar(value="Grouped (Apps)")

        # Game Ready state
        self.game_ready_active = False
        self.game_ready_result: GameReadyResult | None = None
        self.game_ready_target_var = tk.StringVar(
            value=self.config_data.get("last_game_ready_target", "")
        )
        self._game_ready_worker_running = False

        self._build_ui()
        self._bind_shortcuts()
        self._monitor_thread.start()
        self.after(POLL_MS, self._poll)

        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------
    # Keyboard shortcuts
    # ------------------------------------------------------------------

    def _bind_shortcuts(self):
        self.bind_all("<Control-r>", lambda _e: self._on_refresh_now())
        self.bind_all("<Control-R>", lambda _e: self._on_refresh_now())
        self.bind_all("<Control-f>", lambda _e: self.filter_entry.focus_set())
        self.bind_all("<Control-F>", lambda _e: self.filter_entry.focus_set())
        self.bind_all("<Control-p>", lambda _e: self._on_toggle_pause())
        self.bind_all("<Control-P>", lambda _e: self._on_toggle_pause())

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self):
        # ── 1. Top status & admin bar ──────────────────────────────────
        status = ttk.Frame(self, padding=8)
        status.pack(fill="x")

        admin_text = "Admin: YES" if self.is_admin else "Admin: NO (trims disabled)"
        admin_color = "#1a7f37" if self.is_admin else "#c0392b"
        self.admin_label = tk.Label(
            status, text=admin_text, fg=admin_color, font=("Segoe UI", 10, "bold")
        )
        self.admin_label.pack(side="left", padx=(0, 12))

        plat_text = "Windows" if self.is_windows else "Not Windows (stub)"
        plat_color = "#1a7f37" if self.is_windows else "#c0392b"
        tk.Label(
            status, text=f"OS: {plat_text}", fg=plat_color, font=("Segoe UI", 9)
        ).pack(side="left", padx=(0, 14))

        # Live status badge
        self.live_badge = tk.Label(
            status, text="● LIVE (1.0s)", fg="#1a7f37", font=("Segoe UI", 9, "bold")
        )
        self.live_badge.pack(side="left", padx=(0, 8))

        self.last_update_label = tk.Label(
            status, text="Waiting for initial scan…", fg="#666666", font=("Segoe UI", 9)
        )
        self.last_update_label.pack(side="left", padx=(0, 8))

        # Quick action: Free cache
        tk.Button(
            status, text="⚡ Free Standby Cache", command=self._on_free_cache,
            bg="#0969da", fg="white", activebackground="#054da7", activeforeground="white",
            relief="groove", padx=8, pady=2, font=("Segoe UI", 9, "bold")
        ).pack(side="right")

        if not self.is_admin and self.is_windows:
            warn = tk.Label(
                self,
                text=(
                    "Not running as administrator. Trimming, cache-clearing, and elevated task "
                    "termination will fail until you relaunch this app as admin."
                ),
                fg="white", bg="#c0392b", padx=8, pady=3, anchor="w", justify="left",
            )
            warn.pack(fill="x")

        # ── 2. System Memory Overview Card ────────────────────────────
        mem_frame = ttk.LabelFrame(self, text="System Memory (Real-Time)", padding=8)
        mem_frame.pack(fill="x", padx=8, pady=(4, 4))

        cards_and_spark = ttk.Frame(mem_frame)
        cards_and_spark.pack(fill="x", pady=(0, 4))

        # 5 metric cards
        cards_frame = ttk.Frame(cards_and_spark)
        cards_frame.pack(side="left", fill="x", expand=True)

        CARD_TOOLTIPS = {
            "total":     "Physical RAM installed in the machine. 'Usable' is slightly less due to hardware reservations.",
            "in_use":    "RAM actively used by running processes. Matches Task Manager 'In Use' = Total − Available.",
            "available": "RAM immediately available for new allocations without paging. Apps can claim this instantly.",
            "standby":   "Pages cached from recently closed apps. Windows reclaims this automatically when apps need RAM.",
            "committed": "Virtual memory committed by all running processes. Can exceed physical RAM by using the pagefile.",
        }

        # Total RAM
        c1 = ttk.Frame(cards_frame, padding=4)
        c1.pack(side="left", expand=True, fill="x")
        ttk.Label(c1, text="Total RAM", font=("Segoe UI", 8, "bold")).pack(anchor="w")
        self.val_total_ram = ttk.Label(c1, text="--", font=("Segoe UI", 12, "bold"))
        self.val_total_ram.pack(anchor="w")
        self.val_total_sub = ttk.Label(c1, text="Physical RAM", font=("Segoe UI", 7), foreground="#666666")
        self.val_total_sub.pack(anchor="w")
        _Tooltip(c1, CARD_TOOLTIPS["total"])

        # In Use RAM
        c2 = ttk.Frame(cards_frame, padding=4)
        c2.pack(side="left", expand=True, fill="x")
        ttk.Label(c2, text="In Use (Task Mgr)", font=("Segoe UI", 8, "bold")).pack(anchor="w")
        self.val_used_ram = ttk.Label(c2, text="--", font=("Segoe UI", 12, "bold"), foreground="#0969da")
        self.val_used_ram.pack(anchor="w")
        self.val_used_sub = ttk.Label(c2, text="Utilization %", font=("Segoe UI", 7), foreground="#666666")
        self.val_used_sub.pack(anchor="w")
        _Tooltip(c2, CARD_TOOLTIPS["in_use"])

        # Available RAM
        c3 = ttk.Frame(cards_frame, padding=4)
        c3.pack(side="left", expand=True, fill="x")
        ttk.Label(c3, text="Available RAM", font=("Segoe UI", 8, "bold")).pack(anchor="w")
        self.val_avail_ram = ttk.Label(c3, text="--", font=("Segoe UI", 12, "bold"), foreground="#1a7f37")
        self.val_avail_ram.pack(anchor="w")
        self.val_avail_sub = ttk.Label(c3, text="Ready for apps", font=("Segoe UI", 7), foreground="#666666")
        self.val_avail_sub.pack(anchor="w")
        _Tooltip(c3, CARD_TOOLTIPS["available"])

        # Standby Cache
        c4 = ttk.Frame(cards_frame, padding=4)
        c4.pack(side="left", expand=True, fill="x")
        ttk.Label(c4, text="Standby Cache", font=("Segoe UI", 8, "bold")).pack(anchor="w")
        self.val_cache_ram = ttk.Label(c4, text="--", font=("Segoe UI", 12, "bold"), foreground="#9a6700")
        self.val_cache_ram.pack(anchor="w")
        self.val_cache_sub = ttk.Label(c4, text="Cached pages", font=("Segoe UI", 7), foreground="#666666")
        self.val_cache_sub.pack(anchor="w")
        _Tooltip(c4, CARD_TOOLTIPS["standby"])

        # Committed Memory
        c5 = ttk.Frame(cards_frame, padding=4)
        c5.pack(side="left", expand=True, fill="x")
        ttk.Label(c5, text="Committed RAM", font=("Segoe UI", 8, "bold")).pack(anchor="w")
        self.val_commit_ram = ttk.Label(c5, text="--", font=("Segoe UI", 12, "bold"), foreground="#6f42c1")
        self.val_commit_ram.pack(anchor="w")
        self.val_commit_sub = ttk.Label(c5, text="Pagefile commit", font=("Segoe UI", 7), foreground="#666666")
        self.val_commit_sub.pack(anchor="w")
        _Tooltip(c5, CARD_TOOLTIPS["committed"])

        # Sparkline chart (right side)
        spark_frame = ttk.Frame(cards_and_spark, padding=(8, 0, 0, 0))
        spark_frame.pack(side="right", fill="both")
        ttk.Label(spark_frame, text="RAM Usage History (2 min)", font=("Segoe UI", 7), foreground="#666666").pack(anchor="w")
        self.sparkline = _Sparkline(spark_frame, width=200)
        self.sparkline.pack(fill="both", expand=True)

        # Progress bar + summary
        bar_row = ttk.Frame(mem_frame)
        bar_row.pack(fill="x", pady=(2, 2))
        self.mem_progress = ttk.Progressbar(bar_row, orient="horizontal", mode="determinate", maximum=100)
        self.mem_progress.pack(fill="x", side="left", expand=True)

        self.mem_summary_label = ttk.Label(
            mem_frame, text="RAM Utilization: --% | Active processes: --", font=("Segoe UI", 8)
        )
        self.mem_summary_label.pack(anchor="w", pady=(2, 0))

        # ── 3. Live Process Table Frame ────────────────────────────────
        proc_frame = ttk.LabelFrame(self, text="Top Processes (Live)", padding=6)
        proc_frame.pack(fill="both", expand=True, padx=8, pady=(4, 4))

        # Controls toolbar above table
        ctrl_row = ttk.Frame(proc_frame)
        ctrl_row.pack(fill="x", pady=(0, 4))

        ttk.Label(ctrl_row, text="Filter:").pack(side="left", padx=(0, 4))
        self.filter_var = tk.StringVar()
        self.filter_var.trace_add("write", lambda *args: self._on_filter_changed())
        self.filter_entry = ttk.Entry(ctrl_row, textvariable=self.filter_var, width=14)
        self.filter_entry.pack(side="left", padx=(0, 2))
        _Tooltip(self.filter_entry, "Filter by process name or PID. Keyboard shortcut: Ctrl+F")

        tk.Button(
            ctrl_row, text="✕", command=lambda: self.filter_var.set(""),
            relief="flat", padx=3
        ).pack(side="left", padx=(0, 8))

        ttk.Label(ctrl_row, text="View:").pack(side="left", padx=(0, 2))
        view_cb = ttk.Combobox(
            ctrl_row, textvariable=self.view_mode_var,
            values=["Grouped (Apps)", "Detailed (PIDs)"],
            state="readonly", width=14
        )
        view_cb.pack(side="left", padx=(0, 8))
        view_cb.bind("<<ComboboxSelected>>", lambda e: self._on_filter_changed())

        ttk.Label(ctrl_row, text="Rate:").pack(side="left", padx=(0, 2))
        self.interval_var = tk.StringVar(value="1.0s")
        interval_cb = ttk.Combobox(
            ctrl_row, textvariable=self.interval_var,
            values=["0.5s", "1.0s", "2.0s", "5.0s"],
            state="readonly", width=5
        )
        interval_cb.pack(side="left", padx=(0, 8))
        interval_cb.bind("<<ComboboxSelected>>", self._on_interval_changed)

        self.pause_btn = tk.Button(
            ctrl_row, text="⏸ Pause", command=self._on_toggle_pause,
            relief="groove", padx=5, pady=1, font=("Segoe UI", 8)
        )
        self.pause_btn.pack(side="left", padx=(0, 4))
        _Tooltip(self.pause_btn, "Pause / Resume live refresh. Keyboard shortcut: Ctrl+P")

        tk.Button(
            ctrl_row, text="⟳ Refresh", command=self._on_refresh_now,
            relief="groove", padx=5, pady=1, font=("Segoe UI", 8)
        ).pack(side="left", padx=(0, 8))

        ttk.Separator(ctrl_row, orient="vertical").pack(side="left", fill="y", padx=4)

        self.trim_btn = tk.Button(
            ctrl_row, text="✂ Trim Selected", command=self._on_trim_selected,
            relief="groove", padx=6, pady=1, font=("Segoe UI", 8, "bold"),
            bg="#f6f8fa", activebackground="#eaeef2"
        )
        self.trim_btn.pack(side="left", padx=(4, 4))

        self.trim_all_btn = tk.Button(
            ctrl_row, text="✂✂ Trim All Over Limit", command=self._on_trim_all_over_limit,
            relief="groove", padx=6, pady=1, font=("Segoe UI", 8, "bold"),
            bg="#f6f8fa", activebackground="#eaeef2"
        )
        self.trim_all_btn.pack(side="left", padx=(0, 4))
        _Tooltip(self.trim_all_btn, "Immediately trim every process that is currently over its configured RAM limit.")

        self.end_task_btn = tk.Button(
            ctrl_row, text="⛔ End Task", command=self._on_end_task,
            relief="groove", padx=8, pady=1, font=("Segoe UI", 8, "bold"),
            bg="#cf222e", fg="white", activebackground="#a40e26", activeforeground="white"
        )
        self.end_task_btn.pack(side="left", padx=(0, 6))

        ttk.Label(
            proc_frame,
            text="💡 'Task Mgr RAM' = Private Memory (matches Task Manager). 'Working Set' includes shared libs. "
                 "Keyboard: Ctrl+R refresh, Ctrl+F filter, Ctrl+P pause, Delete = End Task.",
            font=("Segoe UI", 8), foreground="#555555"
        ).pack(anchor="w", pady=(0, 4))

        # Treeview with scrollbar
        tree_container = ttk.Frame(proc_frame)
        tree_container.pack(fill="both", expand=True)

        cols = ("name", "inst", "cpu", "private", "rss", "pct", "limit")
        self.proc_tree = ttk.Treeview(tree_container, columns=cols, show="headings", height=8)
        self.proc_tree.heading("name",    text="Application / Process", command=lambda: self._sort_by("name"))
        self.proc_tree.heading("inst",    text="Instances / PID",       command=lambda: self._sort_by("inst"))
        self.proc_tree.heading("cpu",     text="CPU %",                  command=lambda: self._sort_by("cpu"))
        self.proc_tree.heading("private", text="Task Mgr RAM (MB) ▼",   command=lambda: self._sort_by("private"))
        self.proc_tree.heading("rss",     text="Working Set (MB)",       command=lambda: self._sort_by("rss"))
        self.proc_tree.heading("pct",     text="RAM %",                  command=lambda: self._sort_by("pct"))
        self.proc_tree.heading("limit",   text="Configured Limit",       command=lambda: self._sort_by("limit"))

        self.proc_tree.column("name",    width=200, anchor="w")
        self.proc_tree.column("inst",    width=90,  anchor="center")
        self.proc_tree.column("cpu",     width=62,  anchor="e")
        self.proc_tree.column("private", width=120, anchor="e")
        self.proc_tree.column("rss",     width=105, anchor="e")
        self.proc_tree.column("pct",     width=65,  anchor="e")
        self.proc_tree.column("limit",   width=105, anchor="center")

        tree_scroll = ttk.Scrollbar(tree_container, orient="vertical", command=self.proc_tree.yview)
        self.proc_tree.configure(yscrollcommand=tree_scroll.set)
        self.proc_tree.pack(side="left", fill="both", expand=True)
        tree_scroll.pack(side="right", fill="y")

        self.proc_tree.bind("<Double-1>", lambda ev: self._on_fill_from_selection())
        self.proc_tree.bind("<Delete>",   lambda ev: self._on_end_task())

        # Right-click context menu
        self.context_menu = tk.Menu(self, tearoff=0)
        self.context_menu.add_command(label="✂ Trim Process RAM Now",   command=self._on_trim_selected)
        self.context_menu.add_command(label="⛔ End Task (Delete)",      command=self._on_end_task)
        self.context_menu.add_separator()
        self.context_menu.add_command(label="🎯 Set RAM Limit",          command=self._on_fill_from_selection)
        self.context_menu.add_command(label="📁 Open File Location",     command=self._on_open_file_location)
        self.context_menu.add_command(label="ℹ Process Details",        command=self._on_process_details)

        self.proc_tree.bind("<Button-3>", self._show_context_menu)
        self.proc_tree.bind("<Button-2>", self._show_context_menu)

        # ── 4. Limits editor ──────────────────────────────────────────
        limit_frame = ttk.LabelFrame(self, text="Process RAM Limits & Auto-Trim", padding=6)
        limit_frame.pack(fill="x", padx=8, pady=4)

        entry_row = ttk.Frame(limit_frame)
        entry_row.pack(fill="x")
        tk.Label(entry_row, text="Process:").pack(side="left")
        self.name_entry = tk.Entry(entry_row, width=18)
        self.name_entry.pack(side="left", padx=4)
        tk.Label(entry_row, text="Limit MB:").pack(side="left")
        self.limit_entry = tk.Entry(entry_row, width=8)
        self.limit_entry.pack(side="left", padx=4)
        tk.Button(entry_row, text="Add / Update",           command=self._on_add_limit,          relief="groove").pack(side="left", padx=4)
        tk.Button(entry_row, text="Fill from Selected Row", command=self._on_fill_from_selection, relief="groove").pack(side="left", padx=4)

        self.limits_list = tk.Listbox(limit_frame, height=3)
        self.limits_list.pack(fill="x", pady=(4, 0))
        tk.Button(limit_frame, text="Remove Selected Limit", command=self._on_remove_limit, relief="groove").pack(anchor="e", pady=(4, 0))
        self._refresh_limits_list()

        # ── 5. Game Ready Mode Panel ──────────────────────────────────
        gr_frame = ttk.LabelFrame(self, text="⚡ Game Ready Mode", padding=6)
        gr_frame.pack(fill="x", padx=8, pady=4)

        gr_ctrl = ttk.Frame(gr_frame)
        gr_ctrl.pack(fill="x")

        ttk.Label(gr_ctrl, text="Target Game / App:", font=("Segoe UI", 9, "bold")).pack(side="left", padx=(0, 4))

        self.game_ready_cb = ttk.Combobox(
            gr_ctrl, textvariable=self.game_ready_target_var, width=22
        )
        self.game_ready_cb.pack(side="left", padx=(0, 4))
        _Tooltip(self.game_ready_cb, "Select a running application or type an executable name (e.g. game.exe).")

        self.gr_refresh_btn = tk.Button(
            gr_ctrl, text="⟳", command=self._refresh_game_ready_targets,
            relief="groove", padx=4, font=("Segoe UI", 8)
        )
        self.gr_refresh_btn.pack(side="left", padx=(0, 8))
        _Tooltip(self.gr_refresh_btn, "Refresh target process list sorted by RAM usage.")

        if not self.is_admin and self.is_windows:
            self.game_ready_btn = tk.Button(
                gr_ctrl, text="⚡ Game Ready (Requires Admin)",
                state="disabled", bg="#cccccc", fg="#666666",
                relief="groove", padx=10, pady=2, font=("Segoe UI", 9, "bold")
            )
            _Tooltip(
                self.game_ready_btn,
                "Game Ready is disabled because RAM Cutter is not running as administrator. "
                "Restart RAM Cutter elevated to enable trimming, cache clearing, and process priority boost."
            )
        else:
            self.game_ready_btn = tk.Button(
                gr_ctrl, text="⚡ Game Ready", command=self._on_toggle_game_ready,
                bg="#1a7f37", fg="white", activebackground="#14632b", activeforeground="white",
                relief="groove", padx=10, pady=2, font=("Segoe UI", 9, "bold")
            )
            _Tooltip(
                self.game_ready_btn,
                "Click to free maximum RAM for the target, boost its priority to High, and clear standby cache."
            )
        self.game_ready_btn.pack(side="left", padx=(0, 10))

        # Real-time results summary label
        self.gr_result_label = ttk.Label(
            gr_ctrl, text="Select target game/task and click 'Game Ready'.",
            font=("Segoe UI", 8), foreground="#555555"
        )
        self.gr_result_label.pack(side="left", fill="x", expand=True)

        # Honesty notice label
        gr_note = ttk.Label(
            gr_frame,
            text="ℹ Note: Working set trimming moves inactive pages to standby/pagefile; apps page them back in "
                 "as they actively touch them. Freed MB is measured live at execution, not guaranteed permanently.",
            font=("Segoe UI", 7), foreground="#666666"
        )
        gr_note.pack(anchor="w", pady=(3, 0))

        # ── 6. Activity log ───────────────────────────────────────────
        log_frame = ttk.LabelFrame(self, text="Activity & Event Log", padding=6)
        log_frame.pack(fill="both", expand=True, padx=8, pady=(4, 6))

        log_ctrl = ttk.Frame(log_frame)
        log_ctrl.pack(fill="x", pady=(0, 4))
        tk.Button(
            log_ctrl, text="📋 Export Log", command=self._on_export_log,
            relief="groove", padx=6, pady=1, font=("Segoe UI", 8)
        ).pack(side="right")
        tk.Button(
            log_ctrl, text="🗑 Clear", command=self._on_clear_log,
            relief="groove", padx=6, pady=1, font=("Segoe UI", 8)
        ).pack(side="right", padx=(0, 4))

        log_container = ttk.Frame(log_frame)
        log_container.pack(fill="both", expand=True)
        self.log_text = tk.Text(log_container, height=6, state="disabled", wrap="word", font=("Consolas", 8))
        log_scroll = ttk.Scrollbar(log_container, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_scroll.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        log_scroll.pack(side="right", fill="y")

        self.log_text.tag_config("error",   foreground="#c0392b")
        self.log_text.tag_config("warning", foreground="#b8860b")
        self.log_text.tag_config("success", foreground="#1a7f37")
        self.log_text.tag_config("info",    foreground="#333333")

    # ------------------------------------------------------------------
    # Actions & Event Handlers
    # ------------------------------------------------------------------

    def _get_selected_proc(self):
        sel = self.proc_tree.selection()
        if not sel:
            messagebox.showinfo("RAM Cutter", "Please select an item from the table first.")
            return None
        return self._current_rendered_procs.get(sel[0])

    def _show_context_menu(self, event):
        item = self.proc_tree.identify_row(event.y)
        if item:
            self.proc_tree.selection_set(item)
            try:
                self.context_menu.tk_popup(event.x_root, event.y_root)
            finally:
                self.context_menu.grab_release()

    def _on_end_task(self):
        proc = self._get_selected_proc()
        if not proc:
            return

        if proc.name.lower() in PROTECTED_SYSTEM_PROCESSES or proc.pid <= 4:
            messagebox.showwarning(
                "RAM Cutter",
                f"Cannot terminate '{proc.name}'.\n\n"
                f"This is a protected Windows system process essential for system stability."
            )
            return

        if proc.count > 1:
            if not messagebox.askyesno(
                "RAM Cutter",
                f"Are you sure you want to end task for all {proc.count} processes of '{proc.name}'?\n\n"
                f"Any unsaved work in these processes will be lost.",
                default="no",
            ):
                return
            killed = 0
            for pid in proc.pids:
                try:
                    self.psutil.Process(pid).kill()
                    killed += 1
                except Exception:
                    pass
            self.app_state.success(f"Ended {killed}/{proc.count} instances of {proc.name}")
            self.monitor.request_immediate_refresh()
        else:
            if not messagebox.askyesno(
                "RAM Cutter",
                f"Are you sure you want to end task for '{proc.name}' (PID: {proc.pid})?\n\n"
                f"Any unsaved work in this process may be lost.",
                default="no",
            ):
                return
            try:
                p = self.psutil.Process(proc.pid)
                p.kill()
                self.app_state.success(f"Ended task: {proc.name} (PID: {proc.pid})")
                self.monitor.request_immediate_refresh()
            except self.psutil.NoSuchProcess:
                self.app_state.info(f"Process {proc.name} (PID: {proc.pid}) has already terminated.")
                self.monitor.request_immediate_refresh()
            except self.psutil.AccessDenied:
                self.app_state.error(
                    f"Cannot end {proc.name} (PID: {proc.pid}): Access Denied. "
                    f"Run RAM Cutter as administrator to terminate elevated tasks."
                )
            except Exception as e:
                self.app_state.error(f"Failed to end {proc.name} (PID: {proc.pid}): {e}")

    def _on_trim_selected(self):
        proc = self._get_selected_proc()
        if not proc:
            return

        if proc.name.lower() in PROTECTED_SYSTEM_PROCESSES or proc.pid <= 4:
            messagebox.showwarning(
                "RAM Cutter",
                f"Cannot trim '{proc.name}'.\n\n"
                f"Windows protects kernel memory processes from user-mode trimming."
            )
            return

        if proc.count > 1:
            successes = 0
            for pid in proc.pids:
                res = self.backend.trim_process(pid)
                if res.success:
                    successes += 1
            if successes > 0:
                self.app_state.success(f"Trimmed working set for {successes}/{proc.count} instances of {proc.name}")
            else:
                self.app_state.error(f"Trim failed for {proc.name} (requires admin privileges)")
        else:
            result = self.backend.trim_process(proc.pid)
            if result.success:
                self.app_state.success(f"Trimmed {proc.name} (PID: {proc.pid}): {result.message}")
            else:
                self.app_state.error(f"Trim failed for {proc.name} (PID: {proc.pid}): {result.message}")
        self.monitor.request_immediate_refresh()

    def _on_trim_all_over_limit(self):
        """Immediately trim every process that is currently over its configured RAM limit."""
        limits = self.config_data.get("process_limits_mb", {})
        if not limits:
            messagebox.showinfo(
                "RAM Cutter",
                "No process RAM limits are configured.\n\n"
                "Add limits in the 'Process RAM Limits' panel below, then use this button."
            )
            return

        processes = getattr(self.app_state, "latest_processes", [])
        if not processes:
            self.app_state.warning("No process data yet — wait for first scan to complete.")
            return

        trimmed = 0
        skipped = 0
        for p in processes:
            name_lower = p.name.lower()
            if name_lower in PROTECTED_SYSTEM_PROCESSES:
                continue
            limit = limits.get(name_lower)
            if limit is None:
                continue
            # Use rss_mb to be consistent with how decide_trims() aggregates totals
            mem_mb = p.rss_mb
            if mem_mb <= limit:
                continue
            result = self.backend.trim_process(p.pid)
            if result.success:
                trimmed += 1
                self.app_state.success(
                    f"Trim All: {p.name} (PID {p.pid}) {mem_mb:.0f}MB > {limit:.0f}MB limit — {result.message}"
                )
            else:
                skipped += 1
                self.app_state.error(
                    f"Trim All: {p.name} (PID {p.pid}) failed — {result.message}"
                )

        if trimmed == 0 and skipped == 0:
            self.app_state.info("Trim All: no processes are currently over their configured limits.")
        else:
            self.app_state.info(f"Trim All complete: {trimmed} trimmed, {skipped} failed.")
        self.monitor.request_immediate_refresh()

    def _on_open_file_location(self):
        proc = self._get_selected_proc()
        if not proc:
            return
        name, pid = proc.name, proc.pid
        try:
            p = self.psutil.Process(pid)
            exe = p.exe()
            if exe and os.path.exists(exe):
                exe_norm = os.path.normpath(exe)
                subprocess.Popen(["explorer.exe", f"/select,{exe_norm}"])
            else:
                messagebox.showinfo(
                    "RAM Cutter",
                    f"Executable path for '{name}' is not accessible (may be a protected system service)."
                )
        except self.psutil.AccessDenied:
            messagebox.showwarning("RAM Cutter", f"Access denied reading executable path for '{name}'.")
        except Exception as e:
            messagebox.showerror("RAM Cutter", f"Could not open file location: {e}")

    def _on_process_details(self):
        proc = self._get_selected_proc()
        if not proc:
            return
        name, pid = proc.name, proc.pid
        try:
            p = self.psutil.Process(pid)
            mem = p.memory_info()
            cpu = p.cpu_percent(interval=0.1)
            create_time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(p.create_time()))
            try:
                exe = p.exe()
            except Exception:
                exe = "N/A (Access Denied or Protected)"
            try:
                username = p.username()
            except Exception:
                username = "N/A"
            try:
                threads = p.num_threads()
            except Exception:
                threads = "N/A"
            status = p.status()

            details = (
                f"Application / Process: {name}\n"
                f"Instances: {proc.count} (Primary PID: {pid})\n"
                f"Status: {status}\n"
                f"User: {username}\n"
                f"Threads: {threads}\n"
                f"CPU Usage: {cpu:.1f}%\n"
                f"Task Manager Private Memory: {proc.private_mb:.1f} MB\n"
                f"Total Working Set (RSS): {proc.rss_mb:.1f} MB\n"
                f"Virtual RAM (VMS): {mem.vms / (1024*1024):.1f} MB\n"
                f"Started: {create_time}\n\n"
                f"Executable Path:\n{exe}"
            )
            messagebox.showinfo(f"Process Details — {name}", details)
        except self.psutil.NoSuchProcess:
            messagebox.showinfo("RAM Cutter", f"Process {name} (PID: {pid}) has already exited.")
        except Exception as e:
            messagebox.showerror("RAM Cutter", f"Error querying process {name}: {e}")

    def _refresh_game_ready_targets(self):
        """Populates the Game Ready target combobox with running processes, sorted by RAM (highest first)."""
        procs = getattr(self.app_state, "latest_grouped_processes", [])
        if not procs:
            procs = getattr(self.app_state, "latest_processes", [])
        # Sort by RSS descending (most RAM first — those are the likeliest games/targets)
        sorted_procs = sorted(procs, key=lambda p: p.rss_mb, reverse=True)
        names = [p.name for p in sorted_procs if p.name]
        self.game_ready_cb["values"] = names
        # Keep existing typed value if it isn't in the list (user may have typed a name manually)
        current = self.game_ready_target_var.get()
        if current and current not in names:
            pass  # preserve the user's custom entry
        elif names and not current:
            self.game_ready_target_var.set(names[0])

    def _on_toggle_game_ready(self):
        """Toggles Game Ready mode on or off. Runs in a background worker thread."""
        if self.game_ready_active:
            # Restore mode: revert priority and clear state
            self._do_restore_game_ready()
            return

        target = self.game_ready_target_var.get().strip()
        if not target:
            self.app_state.warning("Game Ready: please select or type a target process name first.")
            return

        if self._game_ready_worker_running:
            self.app_state.warning("Game Ready: an operation is already in progress — please wait.")
            return

        # Persist the chosen target
        self.config_data["last_game_ready_target"] = target
        save_config(self.config_data, app_state=self.app_state)

        # Update button to show work-in-progress
        self.game_ready_btn.configure(
            text="⚡ Working…", state="disabled",
            bg="#9a6700", activebackground="#7a5200"
        )
        self.gr_result_label.configure(text=f"Running Game Ready for '{target}'… please wait.")
        self._game_ready_worker_running = True

        def _worker():
            try:
                result = run_game_ready(
                    target_name=target,
                    backend=self.backend,
                    psutil_module=self.psutil,
                    config=self.config_data,
                    app_state=self.app_state,
                    settle_seconds=2.0,
                )
            except Exception as exc:
                self.app_state.error(f"Game Ready: unexpected error — {exc}")
                self.after(0, self._game_ready_reset_button)
                return

            # All UI updates must happen on the main thread via self.after()
            self.after(0, lambda r=result: self._on_game_ready_complete(r))

        threading.Thread(target=_worker, daemon=True).start()

    def _on_game_ready_complete(self, result):
        """Called on the main thread once the Game Ready worker finishes."""
        self._game_ready_worker_running = False

        if not result.success:
            # Target wasn't found or other hard failure — reset to idle state
            self._game_ready_reset_button()
            self.gr_result_label.configure(
                text=f"Game Ready failed: {result.message}",
                foreground="#c0392b"
            )
            return

        # Activate state
        self.game_ready_active = True
        self.game_ready_result = result
        self.monitor.set_game_ready_session(result)

        target_display = result.target_name or self.game_ready_target_var.get()
        self.game_ready_btn.configure(
            text=f"🔄 Game Ready: ON — {target_display} (click to restore)",
            state="normal",
            bg="#c0392b", fg="white",
            activebackground="#a40e26", activeforeground="white",
        )

        # Build the result summary line
        freed = result.freed_mb
        if freed < 50.0:
            freed_str = f"{freed:.1f} MB freed (system already had most of this free)"
        else:
            freed_str = f"{freed:.1f} MB freed"

        summary = (
            f"✅ {freed_str} | "
            f"{result.trimmed_count} trimmed, {result.skipped_count} skipped | "
            f"Target RAM: {result.target_ram_mb:.1f} MB"
        )
        self.gr_result_label.configure(text=summary, foreground="#1a7f37")
        self.monitor.request_immediate_refresh()

    def _game_ready_reset_button(self):
        """Resets the Game Ready button to its idle state (main thread)."""
        self._game_ready_worker_running = False
        self.game_ready_active = False
        self.game_ready_result = None
        self.game_ready_btn.configure(
            text="⚡ Game Ready", state="normal",
            bg="#1a7f37", fg="white",
            activebackground="#14632b", activeforeground="white",
        )
        self.gr_result_label.configure(
            text="Select target game/task and click 'Game Ready'.",
            foreground="#555555"
        )

    def _do_restore_game_ready(self):
        """Restores Game Ready: reverts priority, clears session state, resets button."""
        result = self.game_ready_result
        self.game_ready_active = False
        self.game_ready_result = None
        self.monitor.set_game_ready_session(None)

        if result:
            def _restore_worker():
                restore_game_ready(result, self.backend, self.app_state)
                self.after(0, self._game_ready_reset_button)
            threading.Thread(target=_restore_worker, daemon=True).start()
        else:
            self._game_ready_reset_button()

    def _on_free_cache(self):
        result = self.backend.clear_standby_list()
        if result.success:
            self.app_state.success(f"Free Cache: {result.message}")
        else:
            self.app_state.error(f"Free Cache failed: {result.message}")
        self.monitor.request_immediate_refresh()

    def _on_refresh_now(self):
        self.monitor.request_immediate_refresh()

    def _on_toggle_pause(self):
        if self.monitor.is_paused:
            self.monitor.resume()
            self.pause_btn.configure(text="⏸ Pause")
            interval_txt = self.interval_var.get()
            self.live_badge.configure(text=f"● LIVE ({interval_txt})", fg="#1a7f37")
        else:
            self.monitor.pause()
            self.pause_btn.configure(text="▶ Resume")
            self.live_badge.configure(text="⏸ PAUSED", fg="#9a6700")

    def _on_interval_changed(self, event=None):
        val = self.interval_var.get().replace("s", "")
        try:
            sec = float(val)
            self.monitor.set_refresh_interval(sec)
            if not self.monitor.is_paused:
                self.live_badge.configure(text=f"● LIVE ({sec:.1f}s)")
        except ValueError:
            pass

    def _on_filter_changed(self):
        self._render_processes(
            getattr(self.app_state, "latest_processes", []),
            getattr(self.app_state, "latest_metrics", None),
        )

    def _sort_by(self, col: str):
        if self.sort_col == col:
            self.sort_reverse = not self.sort_reverse
        else:
            self.sort_col = col
            self.sort_reverse = col in ("private", "rss", "pct", "inst", "cpu")

        symbols = {
            "name":    "Application / Process",
            "inst":    "Instances / PID",
            "cpu":     "CPU %",
            "private": "Task Mgr RAM (MB)",
            "rss":     "Working Set (MB)",
            "pct":     "RAM %",
            "limit":   "Configured Limit",
        }
        for k, v in symbols.items():
            if k == self.sort_col:
                arrow = " ▲" if not self.sort_reverse else " ▼"
                self.proc_tree.heading(k, text=f"{v}{arrow}")
            else:
                self.proc_tree.heading(k, text=v)

        self._render_processes(
            getattr(self.app_state, "latest_processes", []),
            getattr(self.app_state, "latest_metrics", None),
        )

    def _on_add_limit(self):
        name = self.name_entry.get().strip().lower()
        limit_str = self.limit_entry.get().strip()
        if not name:
            messagebox.showerror("RAM Cutter", "Process name can't be empty.")
            return

        if name in PROTECTED_SYSTEM_PROCESSES:
            messagebox.showwarning(
                "RAM Cutter",
                f"'{name}' is a protected Windows system process. "
                f"Windows kernel does not allow user-mode trimming or limits on it."
            )
            return

        try:
            limit_mb = float(limit_str)
            if limit_mb <= 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("RAM Cutter", f"'{limit_str}' isn't a valid positive number of MB.")
            return

        self.config_data.setdefault("process_limits_mb", {})[name] = limit_mb
        save_config(self.config_data, app_state=self.app_state)
        self.app_state.info(f"Limit set: {name} -> {limit_mb:.0f}MB")
        self._refresh_limits_list()
        self.monitor.request_immediate_refresh()

    def _on_fill_from_selection(self):
        proc = self._get_selected_proc()
        if proc:
            self.name_entry.delete(0, tk.END)
            self.name_entry.insert(0, proc.name)

    def _on_remove_limit(self):
        sel = self.limits_list.curselection()
        if not sel:
            return
        line = self.limits_list.get(sel[0])
        name = line.split(" -> ")[0]
        self.config_data.get("process_limits_mb", {}).pop(name, None)
        save_config(self.config_data, app_state=self.app_state)
        self.app_state.info(f"Limit removed: {name}")
        self._refresh_limits_list()
        self.monitor.request_immediate_refresh()

    def _refresh_limits_list(self):
        self.limits_list.delete(0, tk.END)
        for name, limit in self.config_data.get("process_limits_mb", {}).items():
            self.limits_list.insert(tk.END, f"{name} -> {limit:.0f}MB")

    def _on_export_log(self):
        """Save the current log to a .txt file chosen by the user."""
        path = filedialog.asksaveasfilename(
            defaultextension=".txt",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
            initialfile="ram_cutter_log.txt",
            title="Export Activity Log",
        )
        if not path:
            return
        try:
            content = self.log_text.get("1.0", tk.END)
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
            self.app_state.success(f"Log exported to: {path}")
        except Exception as e:
            messagebox.showerror("RAM Cutter", f"Could not save log: {e}")

    def _on_clear_log(self):
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", tk.END)
        self.log_text.configure(state="disabled")
        self.app_state.history.clear()

    # ------------------------------------------------------------------
    # Real-Time Polling & Differential Rendering
    # ------------------------------------------------------------------

    def _poll(self):
        for ev in self.app_state.drain():
            self._append_log(ev)

        seq = getattr(self.app_state, "update_sequence", 0)
        if seq != self._last_seen_seq:
            self._last_seen_seq = seq
            metrics = getattr(self.app_state, "latest_metrics", None)
            processes = getattr(self.app_state, "latest_processes", [])

            self._render_metrics(metrics)
            self._render_processes(processes, metrics)

            if metrics and metrics.timestamp:
                self.last_update_label.configure(
                    text=f"Updated: {metrics.formatted_time()}"
                )

        self.after(POLL_MS, self._poll)

    def _render_metrics(self, m: SystemMetrics | None):
        if not m or m.total_mb <= 0:
            return

        def fmt_gb_or_mb(mb: float) -> str:
            if mb >= 1024:
                return f"{mb / 1024:.2f} GB"
            return f"{mb:.0f} MB"

        # Card 1: Total RAM (Physical Installed)
        if m.installed_ram_mb > 0:
            self.val_total_ram.configure(text=fmt_gb_or_mb(m.installed_ram_mb))
            res_str = f", {fmt_gb_or_mb(m.hardware_reserved_mb)} reserved" if m.hardware_reserved_mb > 0 else ""
            self.val_total_sub.configure(text=f"{fmt_gb_or_mb(m.total_mb)} usable{res_str}")
        else:
            self.val_total_ram.configure(text=fmt_gb_or_mb(m.total_mb))
            self.val_total_sub.configure(text="Physical RAM")

        # Card 2: Used RAM (In Use matching Task Manager)
        self.val_used_ram.configure(text=fmt_gb_or_mb(m.used_mb))
        self.val_used_sub.configure(text=f"{m.percent:.1f}% utilization")

        # Card 3: Available RAM
        self.val_avail_ram.configure(text=fmt_gb_or_mb(m.available_mb))
        self.val_avail_sub.configure(text="Available for apps")

        # Card 4: Standby Cache
        if m.cache_mb > 0:
            self.val_cache_ram.configure(text=fmt_gb_or_mb(m.cache_mb))
            self.val_cache_sub.configure(text="Standby cache")
        else:
            self.val_cache_ram.configure(text="N/A")
            self.val_cache_sub.configure(text="Cache unavailable")

        # Card 5: Committed Virtual Memory
        if m.committed_mb > 0 and m.commit_limit_mb > 0:
            self.val_commit_ram.configure(
                text=f"{m.committed_mb / 1024:.1f}/{m.commit_limit_mb / 1024:.1f} GB"
            )
            self.val_commit_sub.configure(text="Pagefile commit")
        else:
            self.val_commit_ram.configure(text="--")
            self.val_commit_sub.configure(text="Commit total")

        self.mem_progress["value"] = m.percent

        # Push data point into sparkline
        self.sparkline.push(m.percent)

        commit_str = ""
        if m.committed_mb > 0 and m.commit_limit_mb > 0:
            commit_str = f" | Committed: {m.committed_mb / 1024:.1f}/{m.commit_limit_mb / 1024:.1f} GB"

        pool_str = ""
        if m.paged_pool_mb > 0 or m.nonpaged_pool_mb > 0:
            pool_str = f" | Pools (Paged: {m.paged_pool_mb:.0f}MB, Non-paged: {m.nonpaged_pool_mb:.0f}MB)"

        self.mem_summary_label.configure(
            text=f"RAM In Use: {m.percent:.1f}% ({fmt_gb_or_mb(m.used_mb)} / {fmt_gb_or_mb(m.total_mb)})"
                 f"{commit_str}{pool_str} | Active Processes: {m.process_count}"
        )

    def _render_processes(self, processes: list, metrics: SystemMetrics | None):
        """Differential update of Treeview to preserve row selection, scroll position, and prevent flicker."""
        total_system_mb = metrics.total_mb if (metrics and metrics.total_mb > 0) else 1.0
        query = self.filter_var.get().strip().lower()
        limits = self.config_data.get("process_limits_mb", {})

        is_grouped = "Grouped" in self.view_mode_var.get()
        source_processes = getattr(
            self.app_state,
            "latest_grouped_processes" if is_grouped else "latest_processes",
            processes,
        )

        curr_sel = self.proc_tree.selection()
        selected_iid = curr_sel[0] if curr_sel else None

        items = []
        for p in source_processes:
            if query and (query not in p.name.lower() and query not in str(p.pid)):
                continue
            items.append(p)

        if self.sort_col:
            rev = self.sort_reverse
            if self.sort_col == "name":
                items.sort(key=lambda p: p.name.lower(), reverse=rev)
            elif self.sort_col == "inst":
                items.sort(key=lambda p: (p.count, p.pid), reverse=rev)
            elif self.sort_col == "cpu":
                items.sort(key=lambda p: p.cpu_percent, reverse=rev)
            elif self.sort_col == "private":
                items.sort(key=lambda p: p.private_mb, reverse=rev)
            elif self.sort_col == "rss":
                items.sort(key=lambda p: p.rss_mb, reverse=rev)
            elif self.sort_col == "pct":
                items.sort(key=lambda p: p.private_mb if p.private_mb > 0 else p.rss_mb, reverse=rev)
            elif self.sort_col == "limit":
                items.sort(key=lambda p: limits.get(p.name, 0.0), reverse=rev)

        new_iids = []
        self._current_rendered_procs.clear()

        for p in items:
            if is_grouped:
                iid = f"app_{p.name}"
                inst_str = f"{p.count} procs" if p.count > 1 else f"PID {p.pid}"
            else:
                iid = f"p_{p.pid}"
                inst_str = str(p.pid)

            new_iids.append(iid)
            self._current_rendered_procs[iid] = p

            ref_mb = p.private_mb if p.private_mb > 0 else p.rss_mb
            pct = (ref_mb / total_system_mb) * 100.0
            pct_str = f"{pct:.1f}%"

            cpu_str = f"{p.cpu_percent:.1f}%" if p.cpu_percent > 0 else "—"

            if p.name.lower() in PROTECTED_SYSTEM_PROCESSES:
                lim_str = "Protected"
            else:
                lim = limits.get(p.name)
                lim_str = f"{lim:.0f} MB" if lim is not None else "—"

            private_str = f"{p.private_mb:.1f}" if p.private_mb > 0 else "—"
            rss_str = f"{p.rss_mb:.1f}"

            vals = (p.name, inst_str, cpu_str, private_str, rss_str, pct_str, lim_str)

            if self.proc_tree.exists(iid):
                # Treeview.item() always returns strings; stringify vals for comparison
                str_vals = tuple(str(v) for v in vals)
                if self.proc_tree.item(iid, "values") != str_vals:
                    self.proc_tree.item(iid, values=vals)
            else:
                self.proc_tree.insert("", tk.END, iid=iid, values=vals)

        existing_children = set(self.proc_tree.get_children())
        for old_iid in existing_children - set(new_iids):
            self.proc_tree.delete(old_iid)

        if new_iids:
            self.proc_tree.set_children("", *new_iids)

        if selected_iid and self.proc_tree.exists(selected_iid):
            self.proc_tree.selection_set(selected_iid)

    def _append_log(self, ev):
        self.log_text.configure(state="normal")
        self.log_text.insert(tk.END, f"[{ev.formatted_time()}] {ev.message}\n", ev.level)
        self.log_text.see(tk.END)
        self.log_text.configure(state="disabled")

    def _on_close(self):
        self.monitor.stop()
        self.destroy()


def run_dashboard(is_windows: bool, is_admin: bool):
    app_state = AppState()
    app_state.is_windows = is_windows
    app_state.is_admin = is_admin
    if not is_admin and is_windows:
        app_state.error("Not running as administrator -- trim and cache-clear actions will fail.")
    app = Dashboard(app_state, is_windows, is_admin)
    app.mainloop()
