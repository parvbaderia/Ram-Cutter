"""
OS-specific memory operations, isolated behind a common interface so the
rest of the app (monitor.py) never needs to know which platform it's on.

WINDOWS (real implementation):
  - trim_process(pid): calls kernel32.EmptyWorkingSet on the process handle.
    This pages out the process's unused memory. It does NOT kill or restart
    the process, and does NOT permanently reduce what the process *can* use --
    if the process touches that memory again, Windows pages it back in.
  - clear_standby_list(): calls the semi-documented NtSetSystemInformation
    with SystemMemoryListInformation to flush the system-wide standby
    (cached) list. This is the same technique used by tools like ISLC.
    Requires admin privileges.

OTHER PLATFORMS (stub, used for development/testing on Linux/macOS):
  - Same interface, but trim/clear are no-ops that just log what *would*
    have happened. This lets the monitoring/decision logic be developed
    and unit-tested without a Windows machine, without ever pretending
    a trim happened when it didn't.
"""
import logging
import platform

logger = logging.getLogger("ram_cutter.backend")

IS_WINDOWS = platform.system() == "Windows"


class TrimResult:
    def __init__(self, success: bool, message: str):
        self.success = success
        self.message = message

    def __repr__(self):
        return f"TrimResult(success={self.success}, message={self.message!r})"


class WindowsBackend:
    """Real implementation. Only imported/instantiated on actual Windows."""

    def __init__(self):
        import ctypes
        import ctypes.wintypes as wintypes
        self.ctypes = ctypes
        self.wintypes = wintypes
        self.kernel32 = ctypes.windll.kernel32
        self.ntdll = ctypes.windll.ntdll
        self.psapi = ctypes.windll.psapi
        self.advapi32 = ctypes.windll.advapi32

        # Process access rights needed for EmptyWorkingSet
        self.PROCESS_QUERY_INFORMATION = 0x0400
        self.PROCESS_SET_QUOTA = 0x0100

        # IMPORTANT: explicitly declare argtypes/restype for every Windows API
        # call below. Without this, ctypes guesses argument marshaling from
        # the Python types passed in (defaulting ints to 32-bit C int), which
        # is a well-known source of silent corruption on 64-bit Windows for
        # APIs expecting HANDLE / SIZE_T (64-bit) parameters -- e.g. passing
        # -1 as SIZE_T relies on correct sign-extension that isn't guaranteed
        # unless the type is declared. Declaring types here removes that risk
        # entirely rather than relying on implicit behavior I can't test.
        self.kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.kernel32.OpenProcess.restype = wintypes.HANDLE

        self.kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel32.CloseHandle.restype = wintypes.BOOL

        self.kernel32.GetCurrentProcess.argtypes = []
        self.kernel32.GetCurrentProcess.restype = wintypes.HANDLE

        # SIZE_T is pointer-sized; use c_size_t explicitly so -1 sign-extends
        # correctly to SIZE_MAX on both 32- and 64-bit Windows.
        self.kernel32.SetProcessWorkingSetSize.argtypes = [
            wintypes.HANDLE, ctypes.c_size_t, ctypes.c_size_t
        ]
        self.kernel32.SetProcessWorkingSetSize.restype = wintypes.BOOL

        self.psapi.EmptyWorkingSet.argtypes = [wintypes.HANDLE]
        self.psapi.EmptyWorkingSet.restype = wintypes.BOOL

        self.ntdll.NtSetSystemInformation.argtypes = [
            ctypes.c_int, ctypes.c_void_p, ctypes.c_ulong
        ]
        self.ntdll.NtSetSystemInformation.restype = ctypes.c_long  # NTSTATUS

        # Priority APIs
        self.PROCESS_SET_INFORMATION = 0x0200
        self.kernel32.GetPriorityClass.argtypes = [wintypes.HANDLE]
        self.kernel32.GetPriorityClass.restype = wintypes.DWORD
        self.kernel32.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self.kernel32.SetPriorityClass.restype = wintypes.BOOL

        # Foreground window APIs (user32)
        try:
            self.user32 = ctypes.windll.user32
            self.user32.GetForegroundWindow.argtypes = []
            self.user32.GetForegroundWindow.restype = wintypes.HWND
            self.user32.GetWindowThreadProcessId.argtypes = [
                wintypes.HWND, ctypes.POINTER(wintypes.DWORD)
            ]
            self.user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        except Exception as e:
            logger.debug("Failed initializing user32 window APIs: %s", e)
            self.user32 = None

        class LUID(ctypes.Structure):
            _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]

        class TOKEN_PRIVILEGES(ctypes.Structure):
            _fields_ = [
                ("PrivilegeCount", wintypes.DWORD),
                ("Privilege", LUID),
                ("Attributes", wintypes.DWORD),
            ]

        class PERFORMANCE_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("CommitTotal", ctypes.c_size_t),
                ("CommitLimit", ctypes.c_size_t),
                ("CommitPeak", ctypes.c_size_t),
                ("PhysicalTotal", ctypes.c_size_t),
                ("PhysicalAvailable", ctypes.c_size_t),
                ("SystemCache", ctypes.c_size_t),
                ("KernelTotal", ctypes.c_size_t),
                ("KernelPaged", ctypes.c_size_t),
                ("KernelNonpaged", ctypes.c_size_t),
                ("PageSize", ctypes.c_size_t),
                ("HandleCount", wintypes.DWORD),
                ("ProcessCount", wintypes.DWORD),
                ("ThreadCount", wintypes.DWORD),
            ]

        self.LUID = LUID
        self.TOKEN_PRIVILEGES = TOKEN_PRIVILEGES
        self.PERFORMANCE_INFORMATION = PERFORMANCE_INFORMATION

        self.psapi.GetPerformanceInfo.argtypes = [
            ctypes.POINTER(PERFORMANCE_INFORMATION), wintypes.DWORD
        ]
        self.psapi.GetPerformanceInfo.restype = wintypes.BOOL

        self.advapi32.OpenProcessToken.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)
        ]
        self.advapi32.OpenProcessToken.restype = wintypes.BOOL
        self.advapi32.LookupPrivilegeValueW.argtypes = [
            wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.POINTER(LUID)
        ]
        self.advapi32.LookupPrivilegeValueW.restype = wintypes.BOOL
        self.advapi32.AdjustTokenPrivileges.argtypes = [
            wintypes.HANDLE, wintypes.BOOL, ctypes.POINTER(TOKEN_PRIVILEGES),
            wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p,
        ]
        self.advapi32.AdjustTokenPrivileges.restype = wintypes.BOOL

        # Enable privileges when running elevated
        self._enable_privilege("SeDebugPrivilege")
        self._enable_privilege("SeProfileSingleProcessPrivilege")

    def _enable_privilege(self, priv_name: str) -> bool:
        """Enables a specific privilege in the current process token if available."""
        TOKEN_ADJUST_PRIVILEGES = 0x0020
        TOKEN_QUERY = 0x0008
        SE_PRIVILEGE_ENABLED = 0x0002
        ERROR_NOT_ALL_ASSIGNED = 1300

        token = self.wintypes.HANDLE()
        if not self.advapi32.OpenProcessToken(
            self.kernel32.GetCurrentProcess(),
            TOKEN_ADJUST_PRIVILEGES | TOKEN_QUERY,
            self.ctypes.byref(token),
        ):
            return False
        try:
            luid = self.LUID()
            if not self.advapi32.LookupPrivilegeValueW(None, priv_name, self.ctypes.byref(luid)):
                return False

            privileges = self.TOKEN_PRIVILEGES(1, luid, SE_PRIVILEGE_ENABLED)
            if not self.advapi32.AdjustTokenPrivileges(
                token, False, self.ctypes.byref(privileges), 0, None, None
            ):
                return False
            return self.ctypes.GetLastError() != ERROR_NOT_ALL_ASSIGNED
        except Exception:
            return False
        finally:
            self.kernel32.CloseHandle(token)

    def _enable_cache_privilege(self) -> TrimResult | None:
        if not self._enable_privilege("SeProfileSingleProcessPrivilege"):
            return TrimResult(
                False,
                "SeProfileSingleProcessPrivilege is not available; run RAM Cutter as administrator",
            )
        return None

    def trim_process(self, pid: int) -> TrimResult:
        PROCESS_ACCESS = self.PROCESS_QUERY_INFORMATION | self.PROCESS_SET_QUOTA
        handle = self.kernel32.OpenProcess(PROCESS_ACCESS, False, pid)
        if not handle:
            err = self.ctypes.GetLastError()
            # WinError 5 = ERROR_ACCESS_DENIED -- almost always means either
            # not running elevated, or the target is a protected system
            # process this app should never touch anyway. Naming the code
            # explicitly here means the UI can show something actionable
            # instead of a bare number.
            hint = " (access denied -- process is protected or requires elevated admin rights)" if err == 5 else ""
            return TrimResult(False, f"OpenProcess failed: WinError {err}{hint}")
        try:
            result = self.kernel32.SetProcessWorkingSetSize(handle, -1, -1)
            # A non-Windows-conformant but real quirk: SetProcessWorkingSetSize(-1,-1)
            # is the documented way to trigger a trim on modern Windows;
            # psapi.EmptyWorkingSet is the older equivalent, kept as fallback below.
            if not result:
                try:
                    result = self.psapi.EmptyWorkingSet(handle)
                except Exception as e:
                    return TrimResult(False, f"Both trim APIs failed: {e}")
            if result:
                return TrimResult(True, "Working set trimmed")
            else:
                err = self.ctypes.GetLastError()
                return TrimResult(False, f"Trim call failed (WinError {err})")
        finally:
            self.kernel32.CloseHandle(handle)

    def clear_standby_list(self) -> TrimResult:
        # SYSTEM_MEMORY_LIST_COMMAND enum, MemoryPurgeStandbyList = 4
        MemoryPurgeStandbyList = 4
        SystemMemoryListInformation = 80  # SYSTEM_INFORMATION_CLASS value

        privilege_error = self._enable_cache_privilege()
        if privilege_error:
            return privilege_error

        command = self.ctypes.c_int(MemoryPurgeStandbyList)
        status = self.ntdll.NtSetSystemInformation(
            SystemMemoryListInformation,
            self.ctypes.byref(command),
            self.ctypes.sizeof(command),
        )
        # NTSTATUS 0 == STATUS_SUCCESS
        if status == 0:
            return TrimResult(True, "Standby list cleared")
        if (status & 0xFFFFFFFF) == 0xC0000061:
            return TrimResult(
                False,
                "Required Windows privilege is not enabled; restart RAM Cutter as administrator",
            )
        else:
            return TrimResult(
                False,
                f"NtSetSystemInformation returned status {status:#x} -- "
                f"almost always means the process is not running elevated (as admin)",
            )

    def get_system_cache_mb(self) -> float | None:
        """Returns the system standby / file cache in MB via GetPerformanceInfo."""
        try:
            pi = self.PERFORMANCE_INFORMATION()
            pi.cb = self.ctypes.sizeof(pi)
            if self.psapi.GetPerformanceInfo(self.ctypes.byref(pi), self.ctypes.sizeof(pi)):
                page_mb = pi.PageSize / (1024 * 1024)
                return float(pi.SystemCache * page_mb)
        except Exception as e:
            logger.debug("Failed to query GetPerformanceInfo: %s", e)
        return None

    def get_extended_memory_metrics(self) -> dict:
        """Returns physical installed RAM, committed virtual memory, and kernel pool sizes matching Task Manager."""
        result = {}
        try:
            installed_kb = self.ctypes.c_uint64()
            if self.kernel32.GetPhysicallyInstalledSystemMemory(self.ctypes.byref(installed_kb)):
                result["installed_ram_mb"] = float(installed_kb.value / 1024.0)
        except Exception:
            pass

        try:
            pi = self.PERFORMANCE_INFORMATION()
            pi.cb = self.ctypes.sizeof(pi)
            if self.psapi.GetPerformanceInfo(self.ctypes.byref(pi), self.ctypes.sizeof(pi)):
                page_mb = pi.PageSize / (1024 * 1024)
                result["committed_mb"] = float(pi.CommitTotal * page_mb)
                result["commit_limit_mb"] = float(pi.CommitLimit * page_mb)
                result["cache_mb"] = float(pi.SystemCache * page_mb)
                result["paged_pool_mb"] = float(pi.KernelPaged * page_mb)
                result["nonpaged_pool_mb"] = float(pi.KernelNonpaged * page_mb)
                total_phys_mb = float(pi.PhysicalTotal * page_mb)
                inst = result.get("installed_ram_mb", 0.0)
                if inst > total_phys_mb:
                    result["hardware_reserved_mb"] = inst - total_phys_mb
        except Exception as e:
            logger.debug("Failed to query extended memory metrics: %s", e)
        return result

    def get_foreground_pid(self) -> int | None:
        """Returns the process ID owning the currently focused foreground window, or None."""
        if not hasattr(self, "user32") or self.user32 is None:
            return None
        try:
            hwnd = self.user32.GetForegroundWindow()
            if not hwnd:
                return None
            pid = self.wintypes.DWORD()
            self.user32.GetWindowThreadProcessId(hwnd, self.ctypes.byref(pid))
            return int(pid.value) if pid.value > 0 else None
        except Exception as e:
            logger.debug("Failed getting foreground window pid: %s", e)
            return None

    def get_priority(self, pid: int) -> int | None:
        """Queries the Win32 PriorityClass for a process handle."""
        handle = self.kernel32.OpenProcess(self.PROCESS_QUERY_INFORMATION, False, pid)
        if not handle:
            return None
        try:
            val = self.kernel32.GetPriorityClass(handle)
            return int(val) if val != 0 else None
        finally:
            self.kernel32.CloseHandle(handle)

    def set_priority(self, pid: int, level: str | int = "high") -> TrimResult:
        """Sets the priority class of a process. level can be 'high', 'normal', or a Win32 priority class DWORD."""
        PRIORITY_MAP = {
            "idle": 0x00000040,
            "below_normal": 0x00004000,
            "normal": 0x00000020,
            "above_normal": 0x00008000,
            "high": 0x00000080,
            "realtime": 0x00000100,
        }
        if isinstance(level, str):
            prio_val = PRIORITY_MAP.get(level.lower(), 0x00000080)
        else:
            prio_val = int(level)

        access = self.PROCESS_SET_INFORMATION | self.PROCESS_QUERY_INFORMATION
        handle = self.kernel32.OpenProcess(access, False, pid)
        if not handle:
            err = self.ctypes.GetLastError()
            hint = " (access denied -- requires administrator rights)" if err == 5 else ""
            return TrimResult(False, f"OpenProcess failed: WinError {err}{hint}")
        try:
            success = self.kernel32.SetPriorityClass(handle, prio_val)
            if success:
                return TrimResult(True, f"Priority set to {level} ({prio_val:#x})")
            else:
                err = self.ctypes.GetLastError()
                return TrimResult(False, f"SetPriorityClass failed (WinError {err})")
        finally:
            self.kernel32.CloseHandle(handle)


class StubBackend:
    """
    Used automatically on non-Windows platforms. Lets the monitoring/decision
    logic run and be tested honestly -- it reports what it WOULD do, and
    never claims a trim succeeded when nothing actually happened.
    """

    def trim_process(self, pid: int) -> TrimResult:
        logger.info("[STUB] Would call EmptyWorkingSet on pid=%s (no-op on this OS)", pid)
        return TrimResult(False, "Trim not available on this platform (Windows-only feature)")

    def clear_standby_list(self) -> TrimResult:
        logger.info("[STUB] Would clear standby list (no-op on this OS)")
        return TrimResult(False, "Standby-list clear not available on this platform (Windows-only feature)")

    def get_system_cache_mb(self) -> float | None:
        return None

    def get_extended_memory_metrics(self) -> dict:
        return {}

    def get_foreground_pid(self) -> int | None:
        return None

    def get_priority(self, pid: int) -> int | None:
        return None

    def set_priority(self, pid: int, level: str | int = "high") -> TrimResult:
        logger.info("[STUB] Would set priority for pid=%s to %s (no-op on this OS)", pid, level)
        return TrimResult(False, "Priority adjustment not available on this platform (Windows-only feature)")


def get_backend():
    if IS_WINDOWS:
        return WindowsBackend()
    return StubBackend()
