"""
Config handling for RAM Cutter.
Stores per-process RAM limits (in MB) and global settings in a JSON file
next to the executable / in the user's app-data folder.
"""
import json
import os
from pathlib import Path

DEFAULT_CONFIG = {
    "refresh_interval_seconds": 1.0, # live memory & process scan interval
    "check_interval_seconds": 1.0,   # backward compatibility
    "cooldown_seconds": 60,          # don't re-trim the same process more than once per cooldown
    "top_n_processes": 30,           # number of top RAM consumers to show in dashboard
    "process_limits_mb": {
        # "chrome.exe": 3000,
    },
    "whitelist": [
        # processes that should NEVER be trimmed, regardless of limits
        "system", "system idle process", "memcompression", "memory compression",
        "registry", "csrss.exe", "wininit.exe", "winlogon.exe", "services.exe",
        "lsass.exe", "smss.exe", "svchost.exe", "dwm.exe", "explorer.exe",
        "ram_cutter.exe", "python.exe", "pythonw.exe",  # never trim ourselves
    ],
    "never_touch": [],               # custom user list of processes to never touch in Game Ready / trim
    "last_game_ready_target": "",    # persisted last-used target for Game Ready
    "game_ready_rearm_seconds": 0,   # optional re-run interval (seconds) while Game Ready is active (0=off)
    "notify_on_trim": True,
}


def _config_path() -> Path:
    """
    Cross-platform config location.
    Windows: %APPDATA%\\RAMCutter\\config.json
    Other (used for dev/testing on Linux): ~/.ram_cutter/config.json
    """
    appdata = os.environ.get("APPDATA")
    if appdata:
        base = Path(appdata) / "RAMCutter"
    else:
        base = Path.home() / ".ram_cutter"
    base.mkdir(parents=True, exist_ok=True)
    return base / "config.json"


def load_config(app_state=None) -> dict:
    path = _config_path()
    if not path.exists():
        save_config(DEFAULT_CONFIG, app_state=app_state)
        return dict(DEFAULT_CONFIG)

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        # Corrupt config file -- fall back to defaults rather than crashing
        # the app, but this MUST be visible to the user, not silent --
        # otherwise their saved limits just vanish with no explanation.
        if app_state:
            app_state.error(f"Config file at {path} is corrupted ({e}); using defaults instead. "
                             f"Your previous limits may be lost -- check the file manually.")
        return dict(DEFAULT_CONFIG)

    # Merge with defaults so new keys added in future versions don't break old configs
    merged = dict(DEFAULT_CONFIG)
    merged.update(data)

    # Sanitize limits: remove any protected system processes that cannot be trimmed
    # (e.g. memcompression, system, registry)
    try:
        from monitor import PROTECTED_SYSTEM_PROCESSES
        limits = merged.get("process_limits_mb", {})
        removed_protected = []
        if not isinstance(limits, dict):
            limits = {}
        for proc_name in list(limits.keys()):
            if proc_name.lower() in PROTECTED_SYSTEM_PROCESSES:
                removed_protected.append(proc_name)
                del limits[proc_name]

        if removed_protected:
            save_config(merged, app_state=app_state)
            if app_state:
                app_state.info(
                    f"Notice: Excluded protected system process(es) {removed_protected} from RAM limits "
                    f"-- Windows kernel protects them from user-mode trimming."
                )
    except Exception:
        pass

    return merged


def save_config(config: dict, app_state=None) -> None:
    path = _config_path()
    tmp_path = path.with_suffix(".tmp")
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)
        tmp_path.replace(path)  # atomic-ish write, avoids corrupting config on crash mid-write
    except OSError as e:
        if app_state:
            app_state.error(f"Could not save config to {path}: {e}")
        else:
            raise
