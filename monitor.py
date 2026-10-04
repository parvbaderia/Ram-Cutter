"""
Core engine: scans real processes via psutil, decides which ones exceed
their configured limit, and hands them to the backend to trim.

This module is deliberately backend-agnostic and pure/testable: given a
list of "fake" processes and a config, decide_trims() always returns the
same decisions, regardless of OS. That's what lets us unit test the actual
decision logic on Linux even though the trim itself is Windows-only.
"""
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable

# pyrefly: ignore [missing-import]
from status import SystemMetrics

logger = logging.getLogger("ram_cutter.monitor")


# System and kernel processes that Windows protects from user-mode modification
# or whose termination/trimming would destabilize the OS or fail with WinError 5.
PROTECTED_SYSTEM_PROCESSES = {
    "memcompression",
    "memory compression",
    "system",
    "system idle process",
    "registry",
    "smss.exe",
    "csrss.exe",
    "wininit.exe",
    "winlogon.exe",
    "services.exe",
    "lsass.exe",
    "dwm.exe",
    "fontdrvhost.exe",
    "ram_cutter.exe",
    "python.exe",
    "pythonw.exe",
}


@dataclass
class ProcInfo:
    pid: int
    name: str
    rss_mb: float
    private_mb: float = 0.0
    cpu_percent: float = 0.0
    count: int = 1
    pids: list[int] = field(default_factory=list)


@dataclass
class TrimDecision:
    pid: int
    name: str
    rss_mb: float
    limit_mb: float


def get_live_processes(psutil_module) -> list[ProcInfo]:
    """Real process scan. Isolated in its own function so tests can bypass it
    and feed synthetic ProcInfo lists directly into decide_trims().

    CPU percent uses interval=None (non-blocking): returns 0.0 on first call
    for any given process, then the correct value on subsequent calls.
    This is intentional -- we never sleep inside the scan loop, so the first
    call after startup will show 0% CPU for everything, then settle correctly
    on the next cycle. The alternative (interval > 0) would stall the entire
    loop for every process, making a 200-process scan take 200+ seconds.
    """
    procs = []
    for p in psutil_module.process_iter(["pid", "name", "memory_info", "cpu_percent"]):
        try:
            info = p.info
            mem = info.get("memory_info")
            if mem is None:
                continue
            # cpu_percent from process_iter attrs cache (non-blocking)
            cpu = info.get("cpu_percent") or 0.0
            procs.append(ProcInfo(
                pid=info["pid"],
                name=(info["name"] or "").lower(),
                rss_mb=mem.rss / (1024 * 1024),
                private_mb=getattr(mem, "private", 0) / (1024 * 1024),
                cpu_percent=cpu if cpu is not None else 0.0,
                count=1,
                pids=[info["pid"]],
            ))
        except (psutil_module.NoSuchProcess, psutil_module.AccessDenied):
            # Process exited mid-scan, or we don't have permission to read it.
            # Either way: skip it, don't crash the whole scan over one process.
            continue
    return procs


def aggregate_processes_by_app(processes: Iterable[ProcInfo]) -> list[ProcInfo]:
    """Groups processes by application name (like Task Manager's Processes tab),
    summing RSS, Private memory, and CPU usage across all instances."""
    groups: dict[str, dict] = {}
    for p in processes:
        if p.name not in groups:
            groups[p.name] = {
                "rss_mb": 0.0,
                "private_mb": 0.0,
                "cpu_percent": 0.0,
                "pids": [],
                "primary_pid": p.pid,
                "max_rss": -1.0,
            }
        g = groups[p.name]
        g["rss_mb"] += p.rss_mb
        g["private_mb"] += p.private_mb
        g["cpu_percent"] += p.cpu_percent
        g["pids"].append(p.pid)
        if p.rss_mb > g["max_rss"]:
            g["max_rss"] = p.rss_mb
            g["primary_pid"] = p.pid

    result = []
    for name, data in groups.items():
        result.append(ProcInfo(
            pid=data["primary_pid"],
            name=name,
            rss_mb=data["rss_mb"],
            private_mb=data["private_mb"],
            cpu_percent=data["cpu_percent"],
            count=len(data["pids"]),
            pids=data["pids"],
        ))
    return result


def decide_trims(
    processes: Iterable[ProcInfo],
    limits_mb: dict,
    whitelist: set,
    last_trim_time: dict,
    cooldown_seconds: float,
    now: float | None = None,
) -> list[TrimDecision]:
    """
    Pure decision logic, no side effects, no OS calls.

    limits_mb: {"chrome.exe": 3000, ...} -- process name -> limit in MB
    whitelist: set of lowercase process names to never trim
    last_trim_time: dict of name -> unix timestamp of last trim (mutated
        externally after a real trim happens; read-only here)
    cooldown_seconds: minimum time between trims of the *same* process name,
        so we don't hammer a process every single check cycle right after
        trimming it (it needs time to actually settle before re-checking)
    """
    if now is None:
        now = time.time()

    # Normalize limits keys to lowercase once, so lookups are case-insensitive
    # (Windows process names are case-insensitive; "Chrome.exe" vs "chrome.exe"
    # was a real bug source in the earlier extension prototype's site-matching)
    limits_lower = {k.lower(): v for k, v in limits_mb.items()}
    whitelist_lower = {w.lower() for w in whitelist}

    # Aggregate RSS per process name first: an app like Chrome runs as MANY
    # OS processes (one per tab/extension). A limit on "chrome.exe" should
    # apply to the SUM of all chrome.exe processes, not treat each one
    # separately -- otherwise a user-set 3000MB limit is meaningless if
    # Chrome is split across 20 processes at 200MB each.
    totals: dict[str, float] = {}
    members: dict[str, list[ProcInfo]] = {}
    for proc in processes:
        totals[proc.name] = totals.get(proc.name, 0.0) + proc.rss_mb
        members.setdefault(proc.name, []).append(proc)

    decisions = []
    for name, total_mb in totals.items():
        if name in PROTECTED_SYSTEM_PROCESSES or name in whitelist_lower:
            continue
        limit = limits_lower.get(name)
        if limit is None:
            continue
        if total_mb <= limit:
            continue

        last = last_trim_time.get(name, 0)
        if now - last < cooldown_seconds:
            continue  # still in cooldown, skip even though it's over limit

        # Trim the single heaviest process instance first, not all of them --
        # gets the most memory back per trim call for the least disruption.
        heaviest = max(members[name], key=lambda p: p.rss_mb)
        decisions.append(TrimDecision(
            pid=heaviest.pid,
            name=name,
            rss_mb=total_mb,
            limit_mb=limit,
        ))

    return decisions


class MonitorLoop:
    """Ties get_live_processes + decide_trims + backend.trim_process together
    into a runnable background loop. Kept thin on purpose -- almost all logic
    lives in decide_trims() above where it can be unit tested directly."""

    def __init__(self, config: dict, backend, psutil_module, on_trim: Callable | None = None, app_state=None):
        self.config = config
        self.backend = backend
        self.psutil = psutil_module
        self.on_trim = on_trim or (lambda decision, result: None)
        self.app_state = app_state  # optional status.AppState -- surfaces real events to the UI
        self.last_trim_time: dict = {}
        self._stop = False
        self.top_n_for_display = config.get("top_n_processes", 30)
        self.refresh_interval = float(
            config.get("refresh_interval_seconds", config.get("check_interval_seconds", 1.0))
        )
        self._wake_event = threading.Event()
        self.is_paused = False
        self.active_game_ready_result = None
        self._last_rearm_time = 0.0

    def stop(self):
        self._stop = True
        self._wake_event.set()

    def set_game_ready_session(self, result):
        """Registers or clears the active Game Ready session for auto-restore and rearm."""
        self.active_game_ready_result = result
        self._last_rearm_time = time.time()

    def request_immediate_refresh(self):
        """Wakes the monitor loop immediately instead of waiting for the timer."""
        self._wake_event.set()

    def set_refresh_interval(self, seconds: float):
        self.refresh_interval = max(0.2, float(seconds))
        self.request_immediate_refresh()

    def pause(self):
        self.is_paused = True

    def resume(self):
        self.is_paused = False
        self.request_immediate_refresh()

    def get_system_metrics(self, process_count: int = 0) -> SystemMetrics:
        """Collects current real-time system memory information matching Task Manager."""
        total_mb = used_mb = available_mb = percent = 0.0
        try:
            vm = self.psutil.virtual_memory()
            total_mb = vm.total / (1024 * 1024)
            available_mb = vm.available / (1024 * 1024)
            used_mb = vm.used / (1024 * 1024)
            percent = float(vm.percent)
        except Exception:
            pass

        ext = {}
        if hasattr(self.backend, "get_extended_memory_metrics"):
            ext = self.backend.get_extended_memory_metrics() or {}

        cache_mb = ext.get("cache_mb", 0.0)
        if not cache_mb and hasattr(self.backend, "get_system_cache_mb"):
            cached = self.backend.get_system_cache_mb()
            if cached is not None:
                cache_mb = cached

        return SystemMetrics(
            total_mb=total_mb,
            used_mb=used_mb,
            available_mb=available_mb,
            cache_mb=cache_mb,
            percent=percent,
            process_count=process_count,
            timestamp=time.time(),
            installed_ram_mb=ext.get("installed_ram_mb", 0.0),
            hardware_reserved_mb=ext.get("hardware_reserved_mb", 0.0),
            committed_mb=ext.get("committed_mb", 0.0),
            commit_limit_mb=ext.get("commit_limit_mb", 0.0),
            paged_pool_mb=ext.get("paged_pool_mb", 0.0),
            nonpaged_pool_mb=ext.get("nonpaged_pool_mb", 0.0),
        )

    def run_once(self) -> list:
        processes = get_live_processes(self.psutil)
        metrics = self.get_system_metrics(process_count=len(processes))

        # Check active Game Ready session: detect if target has exited, or if rearm is due
        if self.active_game_ready_result is not None:
            gr = self.active_game_ready_result
            live_pids = set(p.pid for p in processes)
            target_pids_alive = [pid for pid in gr.target_pids if pid in live_pids]

            # If all target processes exited, trigger auto-restore
            if not target_pids_alive:
                if self.app_state:
                    self.app_state.info(
                        f"Game Ready: target '{gr.target_name}' has exited -- auto-restoring."
                    )
                try:
                    from game_ready import restore_game_ready
                    restore_game_ready(gr, self.backend, self.app_state)
                except Exception as e:
                    logger.debug("Error during auto-restore: %s", e)
                self.active_game_ready_result = None
            else:
                # Target is still running; check optional game_ready_rearm_seconds
                rearm_sec = float(self.config.get("game_ready_rearm_seconds", 0))
                now = time.time()
                if rearm_sec > 0 and (now - self._last_rearm_time) >= rearm_sec:
                    self._last_rearm_time = now
                    try:
                        from game_ready import run_game_ready
                        # Re-run trim pass respecting cooldown and exclusions
                        run_game_ready(
                            gr.target_name,
                            self.backend,
                            self.psutil,
                            self.config,
                            self.app_state,
                            settle_seconds=0.5,
                        )
                    except Exception as e:
                        logger.debug("Error during Game Ready rearm pass: %s", e)

        if self.app_state is not None:
            top_n = self.config.get("top_n_processes", self.top_n_for_display)
            top_detailed = sorted(processes, key=lambda p: p.rss_mb, reverse=True)[:top_n]
            grouped = aggregate_processes_by_app(processes)
            top_grouped = sorted(grouped, key=lambda p: p.rss_mb, reverse=True)[:top_n]
            self.app_state.update_snapshot(top_detailed, metrics, grouped_processes=top_grouped)

        decisions = decide_trims(
            processes=processes,
            limits_mb=self.config.get("process_limits_mb", {}),
            whitelist=set(self.config.get("whitelist", [])),
            last_trim_time=self.last_trim_time,
            cooldown_seconds=self.config.get("cooldown_seconds", 60),
        )
        results = []
        for decision in decisions:
            result = self.backend.trim_process(decision.pid)
            self.last_trim_time[decision.name] = time.time()
            logger.info(
                "Trim %s (pid=%s, %.0fMB > limit %.0fMB): %s",
                decision.name, decision.pid, decision.rss_mb, decision.limit_mb, result,
            )
            if self.app_state:
                msg = (f"{decision.name}: {decision.rss_mb:.0f}MB > {decision.limit_mb:.0f}MB "
                       f"limit -- {result.message}")
                if result.success:
                    self.app_state.success(msg)
                else:
                    self.app_state.error(msg)
            self.on_trim(decision, result)
            results.append((decision, result))
        return results

    def run_forever(self):
        while not self._stop:
            if not self.is_paused:
                try:
                    self.run_once()
                except Exception as e:
                    logger.exception("Unexpected error during monitor cycle -- continuing")
                    if self.app_state:
                        self.app_state.error(f"Monitor cycle crashed: {e} (continuing to retry)")

            self._wake_event.wait(timeout=self.refresh_interval)
            self._wake_event.clear()
