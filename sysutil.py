"""process and os helpers"""

import os
import shutil
import subprocess
import sys

import psutil

on_windows = sys.platform == "win32"
frozen = getattr(sys, "frozen", False)

# what scrapify writes about the person using it. none of it belongs in the app folder
user_files = ["config.json", "scrapify_settings.json", "spotify_session.json", "anghami_session.json",
              "browser_paths.json", "anghami_playlist_ids.json", "spotify_search_template.json", "sync.log"]


def app_dir():
    return os.path.dirname(sys.executable if frozen else os.path.abspath(__file__))


def data_dir():
    if on_windows:
        path = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "Scrapify")
    else:
        path = os.path.join(os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"), "scrapify")
    os.makedirs(path, exist_ok=True)
    return path


def use_data_dir():
    # every file is opened by a relative name, so working from the data folder puts them all there.
    # older versions kept them next to the app, those get moved over once
    for name in user_files:
        old, new = os.path.join(app_dir(), name), os.path.join(data_dir(), name)
        if os.path.isfile(old) and not os.path.exists(new):
            try:
                shutil.move(old, new)
            except OSError:
                pass  # sync.log held open by a running background sync
    os.chdir(data_dir())


def no_console_kwargs():
    # stops a console window flashing up from the windowed app
    return {"creationflags": subprocess.CREATE_NO_WINDOW} if on_windows else {}


def running_process_names():
    names = set()
    for proc in psutil.process_iter(["name"]):
        if proc.info["name"]:
            names.add(proc.info["name"].lower())
    return names


def first_running(patterns):
    # first pattern that's part of a running process name, any case
    patterns = [p.strip().lower() for p in patterns or [] if p.strip()]
    if not patterns:
        return None
    running = running_process_names()
    return next((p for p in patterns if any(p in name for name in running)), None)


def processes_named(exe_name):
    found = []
    for proc in psutil.process_iter(["name", "exe"]):
        if (proc.info["name"] or "").lower() == exe_name.lower():
            found.append(proc)
    return found


def exe_paths_named(exe_name):
    paths = [p.info["exe"] for p in processes_named(exe_name) if p.info["exe"]]
    return sorted(set(paths), key=paths.count, reverse=True)


def pid_running(pid, name_hint=None):
    # name_hint since pids get reused
    try:
        proc = psutil.Process(pid)
        return proc.is_running() and (not name_hint or name_hint.lower() in proc.name().lower())
    except (psutil.Error, TypeError, ValueError):
        return False


def kill_pid(pid):
    try:
        proc = psutil.Process(pid)
        for child in proc.children(recursive=True):
            child.kill()
        proc.kill()
    except (psutil.Error, TypeError, ValueError):
        pass


def kill_processes_using(text):
    # every process whose command line mentions text (a browser profile folder). returns how many
    killed = 0
    needle = text.lower()
    for proc in psutil.process_iter(["cmdline"]):
        try:
            if any(needle in part.lower() for part in proc.info["cmdline"] or []):
                proc.kill()
                killed += 1
        except psutil.Error:
            pass
    return killed


def friendly_app_name(exe_path):
    # "Helium" from the exe's version info, several chromium forks ship a chrome.exe
    if on_windows and exe_path and os.path.isfile(exe_path):
        try:
            import win32api
            lang, codepage = win32api.GetFileVersionInfo(exe_path, "\\VarFileInfo\\Translation")[0]
            for field in ("ProductName", "FileDescription"):
                value = win32api.GetFileVersionInfo(
                    exe_path, f"\\StringFileInfo\\{lang:04x}{codepage:04x}\\{field}")
                if value and value.strip():
                    return value.strip()
        except Exception:
            pass
    stem = os.path.splitext(os.path.basename(exe_path or ""))[0]
    return stem[:1].upper() + stem[1:] if stem else "your browser"


def foreground_window_is_fullscreen():
    # the focused window covers its whole monitor, borderless or exclusive
    if not on_windows:
        return False
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        if not hwnd or hwnd == user32.GetShellWindow():
            return False
        win = wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(win)):
            return False

        class MonitorInfo(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                        ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]

        info = MonitorInfo(cbSize=ctypes.sizeof(MonitorInfo))
        monitor = user32.MonitorFromWindow(hwnd, 2)  # nearest monitor
        if not user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
            return False
        mon = info.rcMonitor
        return win.left <= mon.left and win.top <= mon.top and win.right >= mon.right and win.bottom >= mon.bottom
    except Exception:
        return False


def lower_own_priority():
    # the browsers this process starts inherit it
    try:
        psutil.Process().nice(psutil.BELOW_NORMAL_PRIORITY_CLASS if on_windows else 10)
    except (psutil.Error, OSError):
        pass


def pause_reason(config):
    # why an automatic sync should wait right now, or None
    running = first_running(config.get("pause_while_running"))
    if running:
        return f"'{running}' is running"
    if config.get("pause_while_fullscreen") and foreground_window_is_fullscreen():
        return "a fullscreen app is open"
    return None


class SyncLock:
    # one sync at a time across every scrapify process. a locked byte in a file frees itself if
    # the holder crashes

    def __init__(self, path="scrapify_sync.lock"):
        self.path = path
        self._file = None

    def acquire(self):
        handle = open(self.path, "a+")
        try:
            if on_windows:
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        self._file = handle
        return True

    def release(self):
        if not self._file:
            return
        try:
            if on_windows:
                import msvcrt
                self._file.seek(0)
                msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._file, fcntl.LOCK_UN)
        except OSError:
            pass
        self._file.close()
        self._file = None

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *exc):
        self.release()
