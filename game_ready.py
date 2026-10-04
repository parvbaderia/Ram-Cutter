"""
Game Ready Mode Orchestration for RAM Cutter.

Frees as much RAM as safely possible for a chosen target application (game or task),
temporarily raises its process priority to High, and honestly measures and reports
the memory difference before and after.

Honesty notice:
Trimming a working set releases pages to the system standby list or pagefile.
Applications page them back in as soon as they actively touch them again. The freed
RAM is real at the moment of trimming, but naturally drifts as background applications
resume work. No permanent or guaranteed savings are claimed.
"""
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from monitor import PROTECTED_SYSTEM_PROCESSES, get_live_processes

logger = logging.getLogger("ram_cutter.game_ready")


@dataclass
class GameReadyResult:
    target_name: str
    target_pids: list[int]
    before_mb: float
    after_mb: float
    freed_mb: float
    trimmed_count: int
    skipped_count: int
    skipped_reasons: list[str] = field(default_factory=list)
    original_priorities: dict[int, int] = field(default_factory=dict)
    target_ram_mb: float = 0.0
    standby_cleared: bool = False
    success: bool = True
    message: str = ""


def resolve_target_processes(target_name: str, processes: list) -> list:
    """Finds all running process instances matching target_name (case-insensitive, with or without .exe)."""
    clean_target = (target_name or "").strip().lower()
    if not clean_target:
        return []

    # If target has no extension, also match target + .exe
    variants = {clean_target}
    if not clean_target.endswith(".exe"):
        variants.add(f"{clean_target}.exe")
    else:
        variants.add(clean_target[:-4])

    matched = []
    for p in processes:
        p_name = p.name.lower()
        if p_name in variants or p_name.replace(".exe", "") in variants:
            matched.append(p)
    return matched


def run_game_ready(
    target_name: str,
    backend: Any,
    psutil_module: Any,
    config: dict,
    app_state: Any = None,
    settle_seconds: float = 2.0,
    before_avail_mb: float | None = None,
    after_avail_mb: float | None = None,
) -> GameReadyResult:
    """
    Executes the Game Ready sequence:
    1. Resolve target processes matching target_name.
    2. Snapshot available memory (BEFORE).
    3. Clear standby list via backend.clear_standby_list().
    4. Trim working set of every other process (except target, foreground, whitelist, never_touch).
    5. Raise target's priority to High, recording original priority.
    6. Wait settle_seconds (or inject test values).
    7. Snapshot available memory (AFTER).
    8. Emit honest result summary via app_state.
    """
    clean_target = (target_name or "").strip()
    if not clean_target:
        msg = "No target application specified for Game Ready."
        if app_state:
            app_state.error(msg)
        return GameReadyResult(
            target_name="",
            target_pids=[],
            before_mb=0.0,
            after_mb=0.0,
            freed_mb=0.0,
            trimmed_count=0,
            skipped_count=0,
            success=False,
            message=msg,
        )

    # 1. Resolve running target processes
    all_procs = get_live_processes(psutil_module)
    target_procs = resolve_target_processes(clean_target, all_procs)
    if not target_procs:
        msg = f"Target '{clean_target}' is not currently running. Launch it first, then click Game Ready."
        if app_state:
            app_state.error(msg)
        return GameReadyResult(
            target_name=clean_target,
            target_pids=[],
            before_mb=0.0,
            after_mb=0.0,
            freed_mb=0.0,
            trimmed_count=0,
            skipped_count=0,
            success=False,
            message=msg,
        )

    target_pids = set(p.pid for p in target_procs)
    target_ram_mb = sum(p.rss_mb for p in target_procs)
    canonical_target_name = target_procs[0].name

    if app_state:
        app_state.info(
            f"Game Ready: activating for '{canonical_target_name}' "
            f"({len(target_pids)} process instance(s), {target_ram_mb:.1f} MB current RAM)..."
        )

    # 2. Snapshot available RAM (BEFORE)
    if before_avail_mb is not None:
        before_mb = float(before_avail_mb)
    else:
        try:
            vm = psutil_module.virtual_memory()
            before_mb = vm.available / (1024 * 1024)
        except Exception:
            before_mb = 0.0

    # 3. Clear system standby cache
    standby_res = backend.clear_standby_list()
    standby_cleared = bool(standby_res.success)
    if app_state:
        if standby_cleared:
            app_state.success("Game Ready: cleared Windows standby cache.")
        else:
            app_state.warning(f"Game Ready: standby cache clear skipped/failed ({standby_res.message})")

    # 4. Determine exclusions
    whitelist = set(w.lower() for w in config.get("whitelist", []))
    never_touch = set(n.lower() for n in config.get("never_touch", []))
    protected = set(p.lower() for p in PROTECTED_SYSTEM_PROCESSES)

    fg_pid = backend.get_foreground_pid()

    trimmed_count = 0
    skipped_count = 0
    skipped_reasons: list[str] = []

    # Trim every other process with per-process error handling
    for proc in all_procs:
        pid = proc.pid
        name_lower = proc.name.lower()

        # Exclusions:
        if pid in target_pids:
            continue
        if fg_pid is not None and pid == fg_pid:
            skipped_count += 1
            skipped_reasons.append(f"{proc.name} (PID {pid}): skipped (foreground window process)")
            continue
        if name_lower in protected:
            skipped_count += 1
            skipped_reasons.append(f"{proc.name} (PID {pid}): skipped (protected system process)")
            continue
        if name_lower in whitelist:
            skipped_count += 1
            skipped_reasons.append(f"{proc.name} (PID {pid}): skipped (whitelisted)")
            continue
        if name_lower in never_touch:
            skipped_count += 1
            skipped_reasons.append(f"{proc.name} (PID {pid}): skipped (user never-touch list)")
            continue

        # Execute trim
        try:
            res = backend.trim_process(pid)
            if res.success:
                trimmed_count += 1
            else:
                skipped_count += 1
                skipped_reasons.append(f"{proc.name} (PID {pid}): {res.message}")
        except Exception as e:
            skipped_count += 1
            skipped_reasons.append(f"{proc.name} (PID {pid}): unexpected error {e}")

    # 5. Raise target's priority to High, recording original priority
    original_priorities = {}
    for pid in target_pids:
        orig = backend.get_priority(pid)
        if orig is not None:
            original_priorities[pid] = orig
        prio_res = backend.set_priority(pid, "high")
        if app_state:
            if prio_res.success:
                app_state.success(f"Game Ready: set '{canonical_target_name}' (PID {pid}) priority to High.")
            else:
                app_state.warning(
                    f"Game Ready: could not raise priority for PID {pid} ({prio_res.message})"
                )

    # 6. Wait for memory management to settle
    if settle_seconds > 0 and after_avail_mb is None:
        time.sleep(settle_seconds)

    # 7. Snapshot available RAM (AFTER)
    if after_avail_mb is not None:
        after_mb = float(after_avail_mb)
    else:
        try:
            vm = psutil_module.virtual_memory()
            after_mb = vm.available / (1024 * 1024)
        except Exception:
            after_mb = before_mb

    freed_mb = max(0.0, after_mb - before_mb)

    # 8. Report results honestly
    if freed_mb < 50.0:
        summary_msg = (
            f"Game Ready activated: freed {freed_mb:.1f} MB (your system already had most of this free). "
            f"Trimmed {trimmed_count} processes, skipped {skipped_count}. "
            f"Target RAM: {target_ram_mb:.1f} MB."
        )
    else:
        summary_msg = (
            f"Game Ready activated: freed {freed_mb:.1f} MB available RAM. "
            f"Trimmed {trimmed_count} processes, skipped {skipped_count}. "
            f"Target RAM: {target_ram_mb:.1f} MB."
        )

    if app_state:
        app_state.success(summary_msg)

    return GameReadyResult(
        target_name=canonical_target_name,
        target_pids=list(target_pids),
        before_mb=before_mb,
        after_mb=after_mb,
        freed_mb=freed_mb,
        trimmed_count=trimmed_count,
        skipped_count=skipped_count,
        skipped_reasons=skipped_reasons,
        original_priorities=original_priorities,
        target_ram_mb=target_ram_mb,
        standby_cleared=standby_cleared,
        success=True,
        message=summary_msg,
    )


def restore_game_ready(
    result: GameReadyResult,
    backend: Any,
    app_state: Any = None,
) -> bool:
    """Restores the target's original priority class."""
    if not result or not result.original_priorities:
        if app_state:
            app_state.info("Game Ready: restored (no priority changes to revert).")
        return True

    restored = 0
    for pid, orig_prio in result.original_priorities.items():
        res = backend.set_priority(pid, orig_prio)
        if res.success:
            restored += 1
        elif app_state:
            app_state.warning(f"Game Ready restore: failed to revert priority for PID {pid} ({res.message})")

    msg = f"Game Ready restored: reverted priority for {restored}/{len(result.original_priorities)} process(es) of '{result.target_name}'."
    if app_state:
        app_state.info(msg)
    return True
