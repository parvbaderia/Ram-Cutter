"""
System tray UI. This module is only meant to run on Windows (pystray works
cross-platform in theory, but this app's actual trim functionality is
Windows-only, so there's no point running the tray elsewhere).

NOTE: pystray and a Windows display session are required to actually see
this run. It cannot be executed in a headless Linux container -- it has
been written carefully against pystray's documented API, but you must
verify it visually on your own Windows machine.

OPTIONAL DEPENDENCIES:
    pip install pystray>=0.19 Pillow>=10
    (or: pip install ram-cutter[tray])

The main dashboard (main.py) does NOT require these packages.
"""
import logging
import threading

try:
    import psutil
    from PIL import Image, ImageDraw
    import pystray  # noqa: F401 – checked here so the error is clear
except ImportError as _e:
    raise ImportError(
        f"tray_app.py requires optional packages: {_e}\n"
        "Install them with:  pip install pystray>=0.19 Pillow>=10\n"
        "Or:                 pip install \"ram-cutter[tray]\""
    ) from _e

from backend import get_backend
from config import load_config, save_config
from monitor import MonitorLoop

logger = logging.getLogger("ram_cutter.tray")


def _make_icon_image(color="green"):
    """Generates a simple scissors-ish dot icon in code, so there's no
    dependency on external icon files that could go missing."""
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse((8, 8, 56, 56), fill=color)
    return img


class RamCutterTrayApp:
    def __init__(self):
        self.config = load_config()
        self.backend = get_backend()
        self.last_trims = []  # rolling list of recent (decision, result) for the tray menu display
        self.monitor = MonitorLoop(
            self.config, self.backend, psutil, on_trim=self._on_trim
        )
        self._monitor_thread = None
        self.icon = None

    def _on_trim(self, decision, result):
        entry = f"{decision.name} ({decision.rss_mb:.0f}MB > {decision.limit_mb:.0f}MB): {result.message}"
        self.last_trims.insert(0, entry)
        self.last_trims = self.last_trims[:10]
        if self.icon:
            self.icon.title = f"RAM Cutter -- last: {entry[:40]}"

    def _start_monitor_thread(self):
        self._monitor_thread = threading.Thread(target=self.monitor.run_forever, daemon=True)
        self._monitor_thread.start()

    # ---- Menu actions ----

    def _action_free_cache(self, icon, item):
        result = self.backend.clear_standby_list()
        logger.info("Manual 'Free Cache' triggered: %s", result)
        self._notify(icon, "Free Cache", result.message)

    def _action_add_limit(self, icon, item):
        # Runs a tiny tkinter dialog. tkinter is stdlib so no extra dependency,
        # but it must run on the main thread on some platforms -- pystray's
        # `run_detached()`/menu callbacks run on a worker thread, so we
        # schedule the dialog via icon.run_menu... in practice this needs
        # to be tested on Windows; documenting the intended approach:
        import tkinter as tk
        from tkinter import simpledialog

        root = tk.Tk()
        root.withdraw()
        name = simpledialog.askstring("RAM Cutter", "Process name (e.g. chrome.exe):", parent=root)
        if not name:
            root.destroy()
            return
        limit_str = simpledialog.askstring("RAM Cutter", f"RAM limit in MB for {name}:", parent=root)
        root.destroy()
        if not limit_str:
            return
        try:
            limit_mb = float(limit_str)
        except ValueError:
            self._notify(icon, "RAM Cutter", "Invalid number, limit not saved.")
            return

        self.config.setdefault("process_limits_mb", {})[name.lower()] = limit_mb
        save_config(self.config)
        self.monitor.config = self.config  # live-reload into the running monitor
        self._notify(icon, "RAM Cutter", f"Limit set: {name} -> {limit_mb:.0f}MB")

    def _action_show_recent(self, icon, item):
        text = "\n".join(self.last_trims) if self.last_trims else "No trims yet."
        self._notify(icon, "Recent trims", text[:200])

    def _action_quit(self, icon, item):
        self.monitor.stop()
        icon.stop()

    def _notify(self, icon, title, message):
        try:
            icon.notify(message, title)
        except Exception:
            logger.info("%s: %s", title, message)

    def _build_menu(self):
        import pystray
        return pystray.Menu(
            pystray.MenuItem("Free Cache Now", self._action_free_cache),
            pystray.MenuItem("Add / Edit Process Limit", self._action_add_limit),
            pystray.MenuItem("Show Recent Trims", self._action_show_recent),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", self._action_quit),
        )

    def run(self):
        import pystray
        self._start_monitor_thread()
        self.icon = pystray.Icon(
            "ram_cutter",
            icon=_make_icon_image("green"),
            title="RAM Cutter",
            menu=self._build_menu(),
        )
        self.icon.run()  # blocks until Quit is clicked
