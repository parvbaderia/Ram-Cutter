"""
RAM Cutter entry point (flat file layout -- no package folder).
"""
import logging
import os
import sys

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)

_venv_site = os.path.join(_THIS_DIR, ".venv", "Lib", "site-packages")
if os.path.isdir(_venv_site) and _venv_site not in sys.path:
    sys.path.insert(0, _venv_site)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("ram_cutter.main")


def _check_dependencies():
    """Check required dependencies are available and give a clear error if not."""
    try:
        import psutil  # noqa: F401
    except ModuleNotFoundError:
        msg = (
            "RAM Cutter requires 'psutil' which is not installed.\n\n"
            "Fix:\n"
            "  1. Open a command prompt in this folder\n"
            "  2. Run:  pip install psutil\n"
            "  3. Then launch RAM Cutter again\n\n"
            "If you have a .venv folder, activate it first:\n"
            "  .venv\\Scripts\\activate\n"
            "  pip install psutil"
        )
        # Try to show a GUI error box (tkinter is stdlib, always available)
        try:
            import tkinter as tk
            from tkinter import messagebox
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror("RAM Cutter — Missing Dependency", msg)
            root.destroy()
        except Exception:
            pass
        logger.error("psutil is not installed. %s", msg)
        _pause("psutil is not installed -- see the console for instructions.")
        sys.exit(1)


def _pause(reason: str = ""):
    """Safe replacement for input() that works even without a console (e.g. UAC-relaunched process)."""
    if reason:
        logger.info(reason)
    try:
        if sys.stdin is not None and not getattr(sys.stdin, "closed", False):
            input("Press Enter to close this window...")
    except (EOFError, OSError, RuntimeError):
        # stdin is not available (headless UAC relaunch, pythonw.exe, etc.)
        # Just return; the process will exit naturally.
        pass


def is_admin_windows() -> bool:
    import ctypes
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def relaunch_as_admin_windows():
    import ctypes
    script_path = os.path.abspath(sys.argv[0])
    other_args = sys.argv[1:]
    params = " ".join([f'"{script_path}"'] + [f'"{a}"' for a in other_args])
    original_cwd = os.getcwd()

    python_exe = sys.executable
    venv_exe = os.path.join(_THIS_DIR, ".venv", "Scripts", "python.exe")
    if os.path.isfile(venv_exe):
        python_exe = venv_exe

    result = ctypes.windll.shell32.ShellExecuteW(
        None, "runas", python_exe, params, original_cwd, 1
    )
    if result <= 32:
        logger.error(
            "UAC relaunch failed (ShellExecuteW returned %s). "
            "If you clicked 'No' on the UAC prompt, that's expected -- "
            "run again and click 'Yes'. Otherwise, right-click the script "
            "and choose 'Run as administrator' manually.",
            result,
        )
        return False
    return True


def main():
    _check_dependencies()

    is_windows = sys.platform == "win32"

    if is_windows and not is_admin_windows():
        logger.warning(
            "Not running elevated. Attempting to relaunch with a UAC prompt "
            "(trim/cache-clear need admin rights to function at all)..."
        )
        try:
            relaunch_as_admin_windows()
        except Exception:
            logger.exception(
                "Could not trigger UAC relaunch automatically -- "
                "please right-click and 'Run as administrator' manually."
            )
        # The original (non-elevated) process exits here.
        # _pause() is safe even without a console (e.g. UAC-relaunched context).
        _pause()
        sys.exit(0)

    is_admin = is_admin_windows() if is_windows else False

    try:
        # pyrefly: ignore [missing-import]
        from dashboard import run_dashboard
        run_dashboard(is_windows=is_windows, is_admin=is_admin)
    except Exception:
        # Elevated/relaunched processes were vanishing instantly on any
        # crash with no way to see why. Catch, log, and pause so the real
        # error is always readable.
        logger.exception("RAM Cutter crashed. Full error above.")
        _pause()
        sys.exit(1)


if __name__ == "__main__":
    main()