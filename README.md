# RAM Cutter v2.2

> **Real-time Windows RAM monitor, process trimmer, standby cache cleaner, and Game Ready mode.**
> A single dashboard window — no tray icon, no popups, no hidden state.

![Windows](https://img.shields.io/badge/platform-Windows-blue)
![License: MIT](https://img.shields.io/badge/license-MIT-green)

---

## ⬇️ Download & Run (no coding required)

> **Just want to use the app?** Follow these three steps — no Python knowledge needed.

### Step 1 — Download the installer

Go to the [Releases page](../../releases/latest) and download **`RAM Cutter v2.2.0 Setup.exe`**.

Run the setup wizard and choose the installation directory. Setup creates Start
Menu and Desktop shortcuts and adds an uninstall entry to Windows Settings.

### Step 2 — Launch RAM Cutter

Leave **Launch RAM Cutter** checked on the Finish screen, or start it from the
Start Menu or Desktop shortcut after setup.

Windows will show a **blue UAC prompt** asking:
> *"Do you want to allow this app to make changes to your device?"*

Click **Yes** — admin rights are required to trim memory and clear the standby cache.

### Step 3 — Done ✅

The RAM Cutter dashboard opens. End users do not need Python, pip, or separate
runtime dependencies.

> **Windows SmartScreen warning?**
> If Windows shows *"Windows protected your PC"*, click **More info → Run anyway**.
> The setup and app executables are unsigned. The source code is fully open —
> you can audit every line in this repo before running.

---

## What it does

| Feature | Detail |
|---|---|
| **Live RAM telemetry** | Total (installed), In Use, Available, Standby Cache, Committed — all matching Task Manager |
| **RAM history sparkline** | 2-minute rolling line chart, pure Tkinter (no matplotlib dep) |
| **Process table** | Task Mgr Private Memory + Working Set + CPU% per app, grouped or by-PID |
| **Auto-trim** | Set per-process MB limits; monitor trims automatically when exceeded |
| **Manual trim** | Right-click → Trim, or toolbar button; trims selected process working set immediately |
| **Trim All Over Limit** | One-click trims every process currently over its configured limit |
| **Free Standby Cache** | Clears Windows standby list via `NtSetSystemInformation` (admin required) |
| **⚡ Game Ready Mode** | One-click frees maximum RAM for a chosen target app; auto-restore on exit |
| **End Task** | Terminate any process; protected system processes are blocked |
| **Export Log** | Save the activity log to a .txt file |
| **Keyboard shortcuts** | `Ctrl+R` refresh · `Ctrl+F` focus filter · `Ctrl+P` pause/resume |

---

## RAM metrics — why they match Task Manager

| Card | How it's computed | Task Manager equivalent |
|---|---|---|
| **Total RAM** | `GetPhysicallyInstalledSystemMemory()` | Performance > Memory > Total |
| **In Use** | `psutil.virtual_memory().used` = total − available | Performance > Memory > In Use |
| **Available** | `psutil.virtual_memory().available` | Performance > Memory > Available |
| **Standby Cache** | `GetPerformanceInfo().SystemCache × PageSize` | Performance > Memory > Standby |
| **Committed** | `GetPerformanceInfo().CommitTotal × PageSize` | Performance > Memory > Committed |

The `Task Mgr RAM (MB)` column in the process table uses `PROCESS_MEMORY_COUNTERS_EX.PrivateUsage`
(the `private` field on Windows). This matches what Task Manager's "Memory (Private Working Set)"
column reports for individual processes.

---

## ⚡ Game Ready Mode

Game Ready frees as much RAM as safely possible for a chosen target application (game or
any specific task), then boosts that app's process priority to **High**.

### How it works (in order)

1. **Resolve target** — finds all processes matching the chosen name (games often spawn
   several). If not running, shows a clear error and stops.
2. **Snapshot available RAM** (BEFORE).
3. **Clear the system standby list** via `NtSetSystemInformation` (same technique as ISLC).
4. **Trim the working set** of every other process _except_:
   - the target process tree
   - the foreground window's process (whichever window has focus at click time)
   - the existing whitelist (system-critical processes, this app itself)
   - anything in the `never_touch` config list
5. **Raise the target's priority** to High (NOT Realtime — Realtime can freeze the system).
   The original priority is saved for restore.
6. **Wait ~2 seconds** for the OS memory manager to settle.
7. **Snapshot available RAM** (AFTER) and report the measured difference.

### Undo / Restore

- While active the button turns red: **"Game Ready: ON — target.exe (click to restore)"**.
- Clicking restores the original priority.
- If the target exits, priority is **auto-restored** by the monitor loop within ~1 second.
- Optional: set `game_ready_rearm_seconds` > 0 in config to re-run the trim pass
  periodically while the target is running (default: off).

### Honesty notice

> **Working set trimming moves inactive pages to standby / pagefile.** Applications page
> them back in as soon as they touch those addresses again. The freed MB shown in the
> results panel is **measured live at the moment of execution** — it is real at that
> instant but naturally drifts as background applications resume their normal work.
> No permanent or guaranteed savings are claimed.
>
> If the freed amount is under ~50 MB, the summary says so plainly
> ("your system already had most of this free") rather than dressing it up.

### Game Ready config keys

```json
{
  "never_touch": ["code.exe"],
  "last_game_ready_target": "game.exe",
  "game_ready_rearm_seconds": 0
}
```

| Key | Default | Description |
|---|---|---|
| `never_touch` | `[]` | Process names never trimmed by Game Ready (in addition to the whitelist) |
| `last_game_ready_target` | `""` | Persisted last-used target name (auto-filled in the dropdown) |
| `game_ready_rearm_seconds` | `0` | Re-run the trim pass every N seconds while the target is alive (0 = off) |

---

## 🔨 Building a release installer (for developers)

> **You only need this section to build a release installer.**

### Build-machine requirements
- Python 3.11 or newer — download from [python.org](https://python.org) (tick **"Add Python to PATH"**)
- Inno Setup 6 — download from [jrsoftware.org](https://jrsoftware.org/isinfo.php)
- Internet connection when build dependencies in `requirements-build.txt` are not already installed

### Build steps

Run **`build.bat`** from the project folder, or from PowerShell:

```powershell
.\build.bat
```

The script creates or reuses `.venv`, installs the exact versions in
`requirements-build.txt`, builds the standalone executable from
`RAM Cutter.spec`, and compiles the Inno Setup wizard. It stops with a clear
error if Python or Inno Setup is unavailable.

The upload-ready installer is **`dist\RAM Cutter v2.2.0 Setup.exe`**. The
standalone **`dist\RAM Cutter.exe`** is also produced. The installer bundles the
executable and MIT license locally; it has no download or network install step.
Users can install and run it offline after downloading the single Setup.exe.
The default Program Files location requires administrator approval during setup.
Launching the application separately continues to request elevation through its
existing UAC manifest. Uninstalling leaves `%APPDATA%\RAMCutter\config.json` intact.

To release a new version, update `version` in `pyproject.toml`; the build script
uses that value for the installer filename and Windows installed-app version.

### Create a GitHub Release

1. Update `version` in `pyproject.toml` and run `build.bat`.
2. Push a matching version tag (for example, `v2.2.0`) and open **Releases → Draft a new release** on GitHub.
3. Select that tag, use a matching release title, and attach only
   `dist\RAM Cutter v2.2.0 Setup.exe` as the end-user download.
4. Publish the release. GitHub may also provide its standard source archives;
   users do not need them to install or run the application.

The spec file (`RAM Cutter.spec`) is pre-configured with:
- `--onefile` — single portable `.exe`, no extra folders
- `--windowed` — no console window
- `uac_admin=True` — Windows shows the UAC elevation prompt automatically
- `assets\RAM Cutter.ico` — embedded application icon

---

## Developer setup (run from source)

```powershell
# 1. (Recommended) Create a virtual environment
python -m venv .venv
.venv\Scripts\activate

# 2. Install the only runtime dependency
pip install psutil

# 3. Run (will prompt for UAC elevation automatically)
python main.py
```

> **Admin rights are required** for memory trimming, standby list clearing, and
> Game Ready priority changes. RAM Cutter will prompt for elevation via UAC on startup.

---

## Files

| File | Purpose |
|---|---|
| `main.py` | Entry point. UAC elevation, then opens the dashboard. |
| `dashboard.py` | UI: sparkline, process table, limits editor, Game Ready panel, log. |
| `monitor.py` | Core engine: process scan, grouping, trim decisions, Game Ready auto-restore. |
| `backend.py` | Windows ctypes calls: `EmptyWorkingSet`, `NtSetSystemInformation`, `SetPriorityClass`. |
| `game_ready.py` | Game Ready orchestration: `run_game_ready()`, `restore_game_ready()`, `GameReadyResult`. |
| `config.py` | Loads/saves `%APPDATA%\RAMCutter\config.json`. |
| `status.py` | Thread-safe `AppState` queue (monitor thread → UI thread). |
| `build.bat` | Builds the standalone executable and versioned Windows installer. |
| `RAM Cutter.spec` | PyInstaller configuration for the standalone build. |
| `installer\RAM Cutter.iss` | Inno Setup wizard, shortcuts, install registration, and uninstall configuration. |
| `requirements-build.txt` | Pinned Python build dependencies for repeatable releases. |
| `assets\RAM Cutter.ico` | Icon embedded in the executable and used by the installer. |
| `test_live.py` | Live tests: real process scan, grouping, decision logic, Game Ready (tests 11-16). |

---

## Configuration

Config is stored at `%APPDATA%\RAMCutter\config.json` and auto-created on first run.

```json
{
  "refresh_interval_seconds": 1.0,
  "cooldown_seconds": 60,
  "top_n_processes": 30,
  "process_limits_mb": {
    "chrome.exe": 3000
  },
  "whitelist": ["explorer.exe", "svchost.exe"],
  "never_touch": [],
  "game_ready_rearm_seconds": 0
}
```

---

## Contributing

1. Fork the repo and create a feature branch.
2. Run `python test_live.py` — all tests must pass before opening a PR.
3. Keep changes minimal and documented.

---

## License

MIT — see [LICENSE](LICENSE).
