"""
Central place for anything the UI needs to display honestly:
- elevation/admin status
- whether we're even on Windows (trim features are no-ops otherwise)
- a running log of real events (successes AND failures), each tagged
  with a level, so the UI can show a permission error as an actual
  visible error instead of it disappearing into a console that closes
  itself.

Thread-safe via a plain queue.Queue: the monitor thread pushes events,
the UI thread drains them on a timer. This is the standard safe pattern
for combining a background thread with a Tk mainloop -- Tk itself is not
thread-safe, so the UI must never be touched directly from the monitor
thread, only via this queue.
"""
import queue
import time
from dataclasses import dataclass, field


@dataclass
class Event:
    level: str      # "info" | "success" | "warning" | "error"
    message: str
    timestamp: float = field(default_factory=time.time)

    def formatted_time(self) -> str:
        return time.strftime("%H:%M:%S", time.localtime(self.timestamp))


@dataclass
class SystemMetrics:
    total_mb: float = 0.0
    used_mb: float = 0.0
    available_mb: float = 0.0
    cache_mb: float = 0.0
    percent: float = 0.0
    process_count: int = 0
    timestamp: float = field(default_factory=time.time)
    installed_ram_mb: float = 0.0
    hardware_reserved_mb: float = 0.0
    committed_mb: float = 0.0
    commit_limit_mb: float = 0.0
    paged_pool_mb: float = 0.0
    nonpaged_pool_mb: float = 0.0

    def formatted_time(self) -> str:
        return time.strftime("%H:%M:%S", time.localtime(self.timestamp))


class AppState:
    def __init__(self):
        self.events: "queue.Queue[Event]" = queue.Queue()
        self.is_windows: bool = False
        self.is_admin: bool = False
        self.backend_name: str = "unknown"
        self.history: list = []  # UI-side accumulated log, filled by draining events
        self.latest_processes: list = []
        self.latest_grouped_processes: list = []
        self.latest_metrics: SystemMetrics = SystemMetrics()
        self.update_sequence: int = 0
        self.last_updated_time: float = 0.0

    def update_snapshot(self, processes: list, metrics: SystemMetrics, grouped_processes: list | None = None):
        """Called by background monitor thread to publish real-time telemetry."""
        self.latest_processes = processes
        self.latest_grouped_processes = grouped_processes if grouped_processes is not None else []
        self.latest_metrics = metrics
        self.last_updated_time = time.time()
        self.update_sequence += 1

    def emit(self, level: str, message: str):
        self.events.put(Event(level, message))

    def info(self, message: str):
        self.emit("info", message)

    def success(self, message: str):
        self.emit("success", message)

    def warning(self, message: str):
        self.emit("warning", message)

    def error(self, message: str):
        self.emit("error", message)

    def drain(self, max_items: int = 100) -> list:
        """Called from the UI thread only. Pulls all pending events off the
        queue without blocking, appends them to history, and returns the
        newly-drained batch so the caller can render just the new lines."""
        drained = []
        for _ in range(max_items):
            try:
                ev = self.events.get_nowait()
            except queue.Empty:
                break
            drained.append(ev)
            self.history.append(ev)
        self.history = self.history[-500:]  # cap memory growth over a long-running session
        return drained
