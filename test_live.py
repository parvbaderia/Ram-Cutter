"""
Live integration test -- run on real processes, not mocks.
Spins up actual Python subprocesses that allocate known amounts of RAM,
then verifies the real monitor engine (using real psutil) correctly
identifies them as over/under limit.
"""
import subprocess
import sys
import time
import psutil


# pyrefly: ignore [missing-import]
from monitor import get_live_processes, decide_trims, MonitorLoop, ProcInfo, aggregate_processes_by_app
# pyrefly: ignore [missing-import]
from backend import get_backend, StubBackend

HOG_SCRIPT = """
import time
# Allocate ~150MB and hold it (a real, measurable RSS increase)
block = bytearray(150 * 1024 * 1024)
for i in range(0, len(block), 4096):
    block[i] = 1  # touch every page so it's really resident, not just virtual
time.sleep(30)
"""


def main():
    print("=== TEST 1: spinning up 3 real 150MB memory-hog processes ===")
    procs = []
    for i in range(3):
        p = subprocess.Popen([sys.executable, "-c", HOG_SCRIPT])
        procs.append(p)

    time.sleep(4)  # bumped from 2s -- under container CPU scheduling contention with 3
                   # concurrent hogs, 2s was occasionally too tight for all pages to be
                   # touched/resident, causing a flaky false failure, not a real bug

    try:
        live = get_live_processes(psutil)
        # Find our hog processes by matching pids (including any child processes spawned by venv launchers)
        hog_pids = set()
        for p in procs:
            hog_pids.add(p.pid)
            try:
                for child in psutil.Process(p.pid).children(recursive=True):
                    hog_pids.add(child.pid)
            except Exception:
                pass

        hogs_seen = [p for p in live if p.pid in hog_pids and p.rss_mb > 50]

        print(f"Spawned {len(procs)} hog processes, psutil found {len(hogs_seen)} of them")
        for h in hogs_seen:
            print(f"  pid={h.pid} name={h.name!r} rss={h.rss_mb:.1f}MB")

        assert len(hogs_seen) == 3, f"BUG: expected to find 3 hog processes, found {len(hogs_seen)}"
        for h in hogs_seen:
            assert h.rss_mb > 100, f"BUG: expected ~150MB RSS, got {h.rss_mb:.1f}MB -- measurement is wrong"
        print("PASS: real memory scan correctly reads actual RSS\n")

        print("=== TEST 2: aggregation across multiple processes with same name ===")
        # All 3 hogs run under the same interpreter name (e.g. "python3.12" or "python")
        name = hogs_seen[0].name
        totals_check = sum(h.rss_mb for h in hogs_seen)
        limits = {name: 300}  # set limit below combined total (~450MB) but above any single one (~150MB)
        decisions = decide_trims(
            processes=hogs_seen,
            limits_mb=limits,
            whitelist=set(),
            last_trim_time={},
            cooldown_seconds=60,
        )
        print(f"Combined RSS for {name!r}: {totals_check:.1f}MB, limit={limits[name]}MB")
        print(f"Decisions returned: {decisions}")
        assert len(decisions) == 1, f"BUG: expected exactly 1 trim decision, got {len(decisions)}"
        assert decisions[0].name == name
        print("PASS: correctly aggregates multiple processes under one name before comparing to limit\n")

        print("=== TEST 3: cooldown prevents re-trimming immediately ===")
        last_trim_time = {name: time.time()}  # simulate "just trimmed 0 seconds ago"
        decisions2 = decide_trims(
            processes=hogs_seen,
            limits_mb=limits,
            whitelist=set(),
            last_trim_time=last_trim_time,
            cooldown_seconds=60,
        )
        assert len(decisions2) == 0, f"BUG: cooldown should have blocked this, got {decisions2}"
        print("PASS: cooldown correctly blocks re-trim within cooldown window\n")

        print("=== TEST 4: whitelist protection ===")
        decisions3 = decide_trims(
            processes=hogs_seen,
            limits_mb=limits,
            whitelist={name},  # whitelist the exact process we're testing
            last_trim_time={},
            cooldown_seconds=60,
        )
        assert len(decisions3) == 0, f"BUG: whitelisted process should never be trimmed, got {decisions3}"
        print("PASS: whitelist correctly blocks trimming\n")

        print("=== TEST 5: under-limit process is left alone ===")
        limits_high = {name: 10000}
        decisions4 = decide_trims(
            processes=hogs_seen,
            limits_mb=limits_high,
            whitelist=set(),
            last_trim_time={},
            cooldown_seconds=60,
        )
        assert len(decisions4) == 0, f"BUG: process under limit should not be flagged, got {decisions4}"
        print("PASS: process under its limit is correctly left alone\n")

        print("=== TEST 6: full MonitorLoop.run_once() end-to-end on real processes ===")
        backend = StubBackend()
        config = {
            "process_limits_mb": {name: 300},
            "whitelist": [],
            "cooldown_seconds": 60,
            "check_interval_seconds": 10,
        }
        trims_fired = []
        loop = MonitorLoop(config, backend, psutil, on_trim=lambda d, r: trims_fired.append((d, r)))
        loop.run_once()
        print(f"MonitorLoop fired {len(trims_fired)} trim attempt(s)")
        assert len(trims_fired) >= 1, "BUG: end-to-end loop should have fired at least one trim attempt"
        decision, result = trims_fired[0]
        assert result.success is False, "Expected StubBackend to report success=False honestly"
        print(f"  Decision: {decision}")
        print(f"  Result:   {result}")
        print("PASS: full loop runs end-to-end, and stub backend honestly reports it did NOT actually trim\n")

        print("=== TEST 7: process that exits mid-scan doesn't crash the scanner ===")
        dying = subprocess.Popen([sys.executable, "-c", "pass"])
        dying.wait()
        # Now scan again -- psutil may still briefly list it as a zombie/gone process
        try:
            live2 = get_live_processes(psutil)
            print(f"PASS: scan completed without crashing ({len(live2)} processes found) even with a process that just exited\n")
        except Exception as e:
            print(f"FAIL: scanner crashed on exited process: {e}")
            raise

        print("=== TEST 8: ProcInfo has cpu_percent field ===")
        live3 = get_live_processes(psutil)
        for p in live3[:5]:
            assert hasattr(p, "cpu_percent"), "BUG: ProcInfo missing cpu_percent field"
            assert isinstance(p.cpu_percent, float), f"BUG: cpu_percent should be float, got {type(p.cpu_percent)}"
            assert p.cpu_percent >= 0.0, f"BUG: cpu_percent should be >= 0, got {p.cpu_percent}"
        print(f"PASS: ProcInfo.cpu_percent exists and is a non-negative float on all scanned processes\n")

        print("=== TEST 9: aggregate_processes_by_app sums CPU correctly ===")
        synthetic = [
            ProcInfo(pid=1001, name="myapp.exe", rss_mb=100.0, private_mb=80.0, cpu_percent=12.5),
            ProcInfo(pid=1002, name="myapp.exe", rss_mb=200.0, private_mb=150.0, cpu_percent=7.3),
            ProcInfo(pid=2001, name="other.exe", rss_mb=50.0,  private_mb=40.0,  cpu_percent=1.0),
        ]
        grouped = aggregate_processes_by_app(synthetic)
        grouped_dict = {g.name: g for g in grouped}
        assert "myapp.exe" in grouped_dict, "BUG: myapp.exe not in grouped output"
        myapp = grouped_dict["myapp.exe"]
        assert myapp.count == 2, f"BUG: expected count=2, got {myapp.count}"
        assert abs(myapp.rss_mb - 300.0) < 0.01, f"BUG: expected rss_mb=300, got {myapp.rss_mb}"
        assert abs(myapp.private_mb - 230.0) < 0.01, f"BUG: expected private_mb=230, got {myapp.private_mb}"
        assert abs(myapp.cpu_percent - 19.8) < 0.01, f"BUG: expected cpu_percent=19.8, got {myapp.cpu_percent}"
        print("PASS: aggregate_processes_by_app correctly sums rss_mb, private_mb, and cpu_percent\n")

        print("=== TEST 10: system metrics -- used_mb ~= total_mb - available_mb ===")
        vm = psutil.virtual_memory()
        total_mb  = vm.total     / (1024 * 1024)
        avail_mb  = vm.available / (1024 * 1024)
        used_mb   = vm.used      / (1024 * 1024)
        computed  = total_mb - avail_mb
        diff_mb   = abs(used_mb - computed)
        # psutil.used = total - available on Windows (within 1MB rounding)
        assert diff_mb < 5.0, f"BUG: used_mb={used_mb:.1f} but total-available={computed:.1f}, diff={diff_mb:.1f}MB"
        print(f"  Total:     {total_mb:.1f} MB")
        print(f"  Available: {avail_mb:.1f} MB")
        print(f"  Used:      {used_mb:.1f} MB  (total - available = {computed:.1f} MB, diff = {diff_mb:.2f} MB)")
        print("PASS: used_mb matches total_mb - available_mb (Task Manager formula)\n")

        # -----------------------------------------------------------------------
        # Game Ready tests (11-16) -- all use StubBackend / injected fakes so
        # they run cross-platform.  The following behaviours CANNOT be verified
        # by automated tests and must be covered by the manual checklist below:
        #   - actual working-set reduction (EmptyWorkingSet kernel call)
        #   - actual standby list flush (NtSetSystemInformation)
        #   - actual priority change (SetPriorityClass / Task Manager shows High)
        #   - GetForegroundWindow PID exclusion with a real graphical session
        # -----------------------------------------------------------------------

        print("=== TEST 11: Game Ready -- target resolution ===")
        # pyrefly: ignore [missing-import]
        from game_ready import resolve_target_processes, run_game_ready, restore_game_ready, GameReadyResult

        fake_procs = [
            ProcInfo(pid=200, name="notepad.exe",  rss_mb=80.0,  private_mb=60.0),
            ProcInfo(pid=201, name="notepad.exe",  rss_mb=20.0,  private_mb=15.0),
            ProcInfo(pid=300, name="chrome.exe",   rss_mb=500.0, private_mb=400.0),
            ProcInfo(pid=400, name="svchost.exe",  rss_mb=30.0,  private_mb=20.0),
            ProcInfo(pid=401, name="explorer.exe", rss_mb=120.0, private_mb=90.0),
        ]

        matched = resolve_target_processes("notepad.exe", fake_procs)
        assert len(matched) == 2, f"BUG: expected 2 notepad matches, got {len(matched)}"
        assert all(p.name == "notepad.exe" for p in matched), "BUG: wrong processes matched"

        matched2 = resolve_target_processes("notepad", fake_procs)
        assert len(matched2) == 2, f"BUG: expected 2 notepad matches (no ext), got {len(matched2)}"

        matched3 = resolve_target_processes("game.exe", fake_procs)
        assert len(matched3) == 0, f"BUG: expected 0 matches for non-running target, got {len(matched3)}"
        print("PASS: resolve_target_processes correctly finds target by name (with and without .exe)\n")

        print("=== TEST 12: Game Ready -- target-not-running emits error and returns failure ===")
        from status import AppState

        class _FakeVM:
            available = 2000 * 1024 * 1024

        class _FakePsutil12:
            @staticmethod
            def virtual_memory():
                return _FakeVM()
            @staticmethod
            def process_iter(attrs=None):
                return []  # empty -- target won't be found
            NoSuchProcess = Exception
            AccessDenied = Exception

        state12 = AppState()
        r12 = run_game_ready(
            target_name="notrunning.exe",
            backend=StubBackend(),
            psutil_module=_FakePsutil12,
            config={"whitelist": [], "never_touch": []},
            app_state=state12,
            settle_seconds=0,
            before_avail_mb=2000.0,
            after_avail_mb=2500.0,
        )
        assert r12.success is False, "BUG: should fail when target is not running"
        assert r12.freed_mb == 0.0, f"BUG: freed_mb should be 0 for failed run, got {r12.freed_mb}"
        evs12 = state12.drain()
        assert any(e.level == "error" for e in evs12), "BUG: must emit at least one error event"
        print("PASS: run_game_ready emits error event and returns success=False when target not found\n")

        print("=== TEST 13: Game Ready -- whitelist and target are never in the trim set ===")

        def _make_fake_proc(pid, nm, rss):
            """Returns an object that get_live_processes() can parse via .info dict."""
            import types
            mem = type("M", (), {"rss": int(rss * 1024 * 1024), "private": 0})()
            return types.SimpleNamespace(
                pid=pid,
                name=nm,
                info={
                    "pid": pid,
                    "name": nm,
                    "memory_info": mem,
                    "cpu_percent": 0.0,
                },
            )

        fake_list13 = [
            _make_fake_proc(200, "notepad.exe", 80.0),   # target
            _make_fake_proc(300, "chrome.exe", 500.0),   # should be trimmed
            _make_fake_proc(400, "svchost.exe", 30.0),   # whitelisted
            _make_fake_proc(401, "explorer.exe", 120.0), # whitelisted
        ]

        class _FakePsutil13:
            @staticmethod
            def virtual_memory():
                return type("VM", (), {"available": 2000 * 1024 * 1024})()
            @staticmethod
            def process_iter(attrs=None):
                return fake_list13
            NoSuchProcess = Exception
            AccessDenied = Exception

        trimmed_pids13 = []

        class _TrackingStub13:
            def trim_process(self, pid):
                from backend import TrimResult
                trimmed_pids13.append(pid)
                return TrimResult(True, "stub trim")
            def clear_standby_list(self):
                from backend import TrimResult
                return TrimResult(True, "stub clear")
            def get_foreground_pid(self):
                return None
            def get_priority(self, pid):
                return None
            def set_priority(self, pid, level="high"):
                from backend import TrimResult
                return TrimResult(False, "stub")

        state13 = AppState()
        run_game_ready(
            target_name="notepad.exe",
            backend=_TrackingStub13(),
            psutil_module=_FakePsutil13,
            config={"whitelist": ["svchost.exe", "explorer.exe"], "never_touch": []},
            app_state=state13,
            settle_seconds=0,
            before_avail_mb=2000.0,
            after_avail_mb=2500.0,
        )
        assert 200 not in trimmed_pids13, f"BUG: target (PID 200) must never be trimmed: {trimmed_pids13}"
        assert 400 not in trimmed_pids13, f"BUG: svchost.exe (PID 400) whitelisted, must not trim: {trimmed_pids13}"
        assert 401 not in trimmed_pids13, f"BUG: explorer.exe (PID 401) whitelisted, must not trim: {trimmed_pids13}"
        assert 300 in trimmed_pids13, f"BUG: chrome.exe (PID 300) should have been trimmed: {trimmed_pids13}"
        print(f"  Trimmed PIDs: {trimmed_pids13} (expected [300] only)")
        print("PASS: whitelist and target are never included in the trim pass\n")

        print("=== TEST 14: Game Ready -- a failing trim on one PID does not abort the rest ===")
        trimmed_ok14 = []

        class _FailOnePid14:
            def trim_process(self, pid):
                from backend import TrimResult
                if pid == 300:  # chrome.exe fails
                    return TrimResult(False, "Simulated WinError 5 (access denied)")
                trimmed_ok14.append(pid)
                return TrimResult(True, "stub trim")
            def clear_standby_list(self):
                from backend import TrimResult
                return TrimResult(True, "stub clear")
            def get_foreground_pid(self):
                return None
            def get_priority(self, pid):
                return None
            def set_priority(self, pid, level="high"):
                from backend import TrimResult
                return TrimResult(False, "stub")

        fake_list14 = [
            _make_fake_proc(200, "notepad.exe",  80.0),  # target
            _make_fake_proc(300, "chrome.exe",  500.0),  # will fail
            _make_fake_proc(400, "svchost.exe",  30.0),  # whitelisted
            _make_fake_proc(500, "winword.exe", 200.0),  # should succeed
        ]

        class _FakePsutil14:
            @staticmethod
            def virtual_memory():
                return type("VM", (), {"available": 2000 * 1024 * 1024})()
            @staticmethod
            def process_iter(attrs=None):
                return fake_list14
            NoSuchProcess = Exception
            AccessDenied = Exception

        state14 = AppState()
        r14 = run_game_ready(
            target_name="notepad.exe",
            backend=_FailOnePid14(),
            psutil_module=_FakePsutil14,
            config={"whitelist": ["svchost.exe"], "never_touch": []},
            app_state=state14,
            settle_seconds=0,
            before_avail_mb=2000.0,
            after_avail_mb=2300.0,
        )
        assert 500 in trimmed_ok14, f"BUG: winword.exe (PID 500) must still be trimmed after chrome.exe failure: {trimmed_ok14}"
        assert r14.success is True, "BUG: overall result must be success even when individual trims fail"
        assert r14.skipped_count >= 1, f"BUG: failed PIDs counted in skipped_count: {r14.skipped_count}"
        assert r14.trimmed_count >= 1, f"BUG: successful PIDs counted in trimmed_count: {r14.trimmed_count}"
        print(f"  trimmed_count={r14.trimmed_count}, skipped_count={r14.skipped_count}")
        print("PASS: failing trim on one PID does not abort the rest; skipped_count is honest\n")

        print("=== TEST 15: Game Ready -- freed_mb math uses injected before/after values ===")
        fake_list15 = [
            _make_fake_proc(200, "notepad.exe", 80.0),
            _make_fake_proc(300, "other.exe", 100.0),
        ]

        class _FakePsutil15:
            @staticmethod
            def virtual_memory():
                return type("VM", (), {"available": 0})()
            @staticmethod
            def process_iter(attrs=None):
                return fake_list15
            NoSuchProcess = Exception
            AccessDenied = Exception

        class _SilentStub15:
            def trim_process(self, pid):
                from backend import TrimResult
                return TrimResult(True, "stub")
            def clear_standby_list(self):
                from backend import TrimResult
                return TrimResult(True, "stub")
            def get_foreground_pid(self):
                return None
            def get_priority(self, pid):
                return None
            def set_priority(self, pid, level="high"):
                from backend import TrimResult
                return TrimResult(False, "stub")

        state15 = AppState()
        r15 = run_game_ready(
            target_name="notepad.exe",
            backend=_SilentStub15(),
            psutil_module=_FakePsutil15,
            config={"whitelist": [], "never_touch": []},
            app_state=state15,
            settle_seconds=0,
            before_avail_mb=1000.0,
            after_avail_mb=1350.0,
        )
        assert r15.success is True, f"BUG: should succeed when target is found: {r15}"
        assert abs(r15.before_mb - 1000.0) < 0.01, f"BUG: before_mb={r15.before_mb}"
        assert abs(r15.after_mb  - 1350.0) < 0.01, f"BUG: after_mb={r15.after_mb}"
        assert abs(r15.freed_mb  -  350.0) < 0.01, f"BUG: freed_mb={r15.freed_mb} (expected 350)"
        print(f"  before={r15.before_mb:.1f} MB, after={r15.after_mb:.1f} MB, freed={r15.freed_mb:.1f} MB")
        print("PASS: freed_mb math is correct: max(0, after_mb - before_mb)\n")

        print("=== TEST 16: Game Ready -- restore reverts original_priorities via stub ===")
        from game_ready import restore_game_ready, GameReadyResult
        restore_calls16 = []

        class _RestoreStub16:
            def set_priority(self, pid, level="high"):
                from backend import TrimResult
                restore_calls16.append((pid, level))
                return TrimResult(True, "stub restore")

        fake_gr_result = GameReadyResult(
            target_name="notepad.exe",
            target_pids=[200, 201],
            before_mb=1000.0,
            after_mb=1300.0,
            freed_mb=300.0,
            trimmed_count=5,
            skipped_count=2,
            original_priorities={200: 0x00000020, 201: 0x00000020},  # NORMAL_PRIORITY_CLASS
            target_ram_mb=100.0,
            success=True,
        )
        state16 = AppState()
        ok16 = restore_game_ready(fake_gr_result, _RestoreStub16(), state16)
        assert ok16 is True, "BUG: restore_game_ready should return True"
        restored_pids16 = [p for p, _ in restore_calls16]
        assert 200 in restored_pids16, f"BUG: PID 200 not restored: {restore_calls16}"
        assert 201 in restored_pids16, f"BUG: PID 201 not restored: {restore_calls16}"
        print(f"  Restore calls: {restore_calls16}")
        print("PASS: restore_game_ready calls set_priority for each recorded original priority\n")

        print("ALL TESTS PASSED")

    finally:
        for p in procs:
            p.terminate()
        for p in procs:
            p.wait(timeout=5)


if __name__ == "__main__":
    main()


# =============================================================================
# MANUAL TESTING CHECKLIST (Windows, requires admin)
# =============================================================================
#
# Automated tests 11-16 above cover: target resolution, error reporting,
# whitelist/target exclusion, per-process failure isolation, freed_mb math,
# and restore logic -- all using injected fakes so they run cross-platform.
#
# The following CANNOT be verified without a real Windows graphical session
# running as administrator. Run python main.py elevated, then:
#
#  1. ADMIN GATE
#     - Without admin: Game Ready button must be DISABLED ("Requires Admin").
#       Button must be inert. Do NOT allow the operation to proceed.
#
#  2. DROPDOWN POPULATION
#     - Click the refresh (rotate) button next to the target dropdown.
#       Verify the combobox fills with currently running process names,
#       sorted highest RAM first (chrome.exe / code.exe before tiny procs).
#
#  3. TARGET NOT RUNNING
#     - Type "definitely_not_running.exe" and click Game Ready.
#       Verify: red error in log, no trim pass, button stays green.
#
#  4. GAME READY ACTIVATION (use Notepad as a safe target)
#     a. Open Notepad (notepad.exe).
#     b. Select "notepad.exe" in the dropdown.
#     c. Open Task Manager > Details tab, note working-set sizes for a few
#        background apps (e.g. chrome.exe, Teams).
#     d. Click Game Ready.  Wait ~3 seconds.
#     e. Log must show:
#          - "cleared Windows standby cache" (or a warning if already small)
#          - Per-process trim messages
#          - Final summary with measured freed MB (not an estimate)
#     f. In Task Manager Details, verify background apps' working sets dropped.
#     g. Verify notepad.exe Priority column shows "High" in Task Manager.
#     h. Button turns red: "Game Ready: ON -- notepad.exe (click to restore)".
#     i. If freed MB < 50, log says "system already had most of this free" --
#        NOT a made-up large number.
#
#  5. RESTORE
#     - Click the (now red) button.
#     - Verify notepad.exe priority returns to "Normal" in Task Manager.
#     - Button returns to green "Game Ready".
#
#  6. AUTO-RESTORE ON TARGET EXIT
#     a. Activate Game Ready with Notepad as target.
#     b. Close Notepad.
#     c. Within ~2 seconds, log shows "auto-restoring" and button resets green.
#
#  7. FOREGROUND WINDOW EXCLUSION
#     - Click on another app (e.g. chrome) to make it foreground just before
#       clicking Game Ready.  Verify that app is listed as "skipped (foreground
#       window process)" in the log and its working set was NOT trimmed.
#
#  8. NEVER-TOUCH LIST
#     - Add "code.exe" to "never_touch" in config.json. Restart.
#       Activate Game Ready and verify code.exe is "skipped (user never-touch list)".
#
#  9. REARM (optional)
#     - Set "game_ready_rearm_seconds": 30 in config.json.
#       Activate Game Ready. Wait 30+ seconds.
#       Verify a second trim pass runs and appears in the log automatically.
# =============================================================================
