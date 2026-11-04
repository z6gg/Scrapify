import json
import logging
import os
import queue
import re
import shlex
import shutil
import struct
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

if getattr(sys, "frozen", False) and "PLAYWRIGHT_BROWSERS_PATH" not in os.environ:
    # before playwright is imported. the built app ships chromium in its browsers folder,
    # without this it would look inside its own bundle
    bundled = os.path.join(os.path.dirname(sys.executable), "browsers")
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = bundled if os.path.isdir(bundled) else os.path.join(
        os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "ms-playwright")

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

try:
    from playwright.sync_api import sync_playwright

    import browsersessioncommon as bsc
    import main as sync_main
    import sysutil
except ImportError as e:
    # a source run without the requirements, pythonw would otherwise just not open
    messagebox.showerror("Scrapify", f"{e.name} isn't installed.\n\nRun: pip install -r requirements.txt")
    sys.exit(1)

from main import load_config, save_config
import updater
from version import version

# the tray is for the built app people leave running, a source run closes like a normal window
has_tray = False
if sysutil.frozen:
    try:
        import pystray
        from PIL import Image as PILImage
        has_tray = True
    except ImportError:
        pass

app_name = "Scrapify"
config_file = sync_main.config_file
settings_file = "scrapify_settings.json"
busy_message = "Scrapify is already working elsewhere."
# names under HKCU\...\Run, which starts things at log on without admin
run_key = r"Software\Microsoft\Windows\CurrentVersion\Run"
background_entry = "Scrapify Background Sync"
autostart_entry = "Scrapify"
cron_marker = "# scrapify-autosync"
pid_file = sync_main.pid_file

# loose on purpose; anghami share links can also be a bare id (open.anghami.com/2WMVrpQy54b)
spotify_link = re.compile(r"^https://open\.spotify\.com/playlist/[A-Za-z0-9]+")
anghami_link = re.compile(r"^https://[\w-]*\.?anghami\.com/(?:playlist/[\w-]+|[A-Za-z0-9]{6,})(?:[/?].*)?$")
link_patterns = {"spotify": spotify_link, "anghami": anghami_link}
platform_names = sync_main.platform_names

default_settings = {
    "mode": "off",  # off, open (in app timer) or always (background sync)
    "interval_minutes": 30,
    "custom_browser_exe": "",
    "selected_browser": "Chrome",
    "launch_minimized": False,
    "dark_mode": False,
    "minimize_on_close": True,
    "check_updates": True,
    "auto_update": False,
}
update_check_interval_ms = 12 * 60 * 60 * 1000  # while scrapify stays open in the tray
muted_gray = "#606060"
dark_colors = {"bg": "#1e1e1e", "fg": "#e6e6e6", "field_bg": "#2b2b2b", "button_bg": "#3c3c3c",
               "button_active": "#4a4a4a", "select_bg": "#3d6fb4", "select_fg": "#ffffff", "muted": "#9a9a9a",
               "border": "#555555"}
browser_flag_labels = (
    ("disable_gpu", "Disable GPU acceleration"),
    ("mute_audio", "Mute audio"),
    ("disable_extensions", "Disable browser extensions"),
    ("disable_background_networking", "Disable background networking"),
    ("disable_default_apps", "Disable default apps setup"),
    ("skip_first_run", "Skip first run setup"),
)


def resource_path(relative_path):
    return os.path.join(getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__))), relative_path)


icon_path = resource_path("icon.ico")


def load_settings():
    try:
        with open(settings_file, encoding="utf-8") as f:
            settings = json.load(f)
        if isinstance(settings, dict):
            return {**default_settings, **settings}
    except (OSError, ValueError):
        pass
    return dict(default_settings)


def save_settings(settings):
    tmp = settings_file + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2)
    os.replace(tmp, settings_file)


def _load_ico_png_frames(ico_path):
    # tk's iconbitmap only reads bmp frames and modern icons keep their big sizes as png
    frames = []
    try:
        data = Path(ico_path).read_bytes()
        reserved, kind, count = struct.unpack_from("<HHH", data, 0)
        if reserved or kind != 1:
            return frames
        for i in range(count):
            *_, size, offset = struct.unpack_from("<BBBBHHII", data, 6 + i * 16)
            chunk = data[offset:offset + size]
            if chunk[:8] == b"\x89PNG\r\n\x1a\n":
                try:
                    frames.append(tk.PhotoImage(data=chunk))
                except tk.TclError:
                    pass
    except Exception:
        return frames
    return sorted(frames, key=lambda img: img.width(), reverse=True)


# ---------------------------------------------------- background sync, autostart

def _launch_command(script, extra_args=""):
    # the exe itself, or pythonw so no console shows up
    if sysutil.frozen:
        return f'"{sys.executable}" {extra_args}'.strip()
    pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    program = pythonw if os.path.exists(pythonw) else sys.executable
    return f'"{program}" "{os.path.join(sysutil.app_dir(), script)}" {extra_args}'.strip()


def _run_entry(name):
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, run_key) as key:
            return winreg.QueryValueEx(key, name)[0]
    except OSError:
        return None


def _set_run_entry(name, command):
    import winreg
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, run_key) as key:
        if command:
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, command)
        else:
            try:
                winreg.DeleteValue(key, name)
            except FileNotFoundError:
                pass


def _daemon_pid():
    try:
        return int(Path(pid_file).read_text().strip())
    except (OSError, ValueError):
        return None


def is_daemon_running():
    return sysutil.pid_running(_daemon_pid(), "scrapify" if sysutil.frozen else "python")


def install_background_sync(interval_minutes):
    # one long lived background sync started at every log on, and right away
    if is_daemon_running():
        sysutil.kill_pid(_daemon_pid())  # or two would sync side by side
    command = _launch_command("main.py", f"--daemon --interval {interval_minutes}")
    if sysutil.on_windows:
        _set_run_entry(background_entry, command)
        subprocess.Popen(command, cwd=sysutil.data_dir(), close_fds=True,
                         creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)
        return
    try:
        existing = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    except FileNotFoundError:
        raise RuntimeError("No crontab command found on this system.")
    lines = [l for l in (existing.stdout if existing.returncode == 0 else "").splitlines() if cron_marker not in l]
    cmd = f"cd {shlex.quote(sysutil.data_dir())} && nohup nice -n 10 {command} >> scrapify_autosync.log 2>&1 &"
    lines.append(f"@reboot {cmd} {cron_marker}")
    result = subprocess.run(["crontab", "-"], input="\n".join(lines) + "\n", text=True, capture_output=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "Couldn't write the crontab.")
    subprocess.Popen(cmd, shell=True)


def uninstall_background_sync():
    if is_daemon_running():
        sysutil.kill_pid(_daemon_pid())
    try:
        os.remove(pid_file)
    except OSError:
        pass
    if sysutil.on_windows:
        _set_run_entry(background_entry, None)
        return
    try:
        existing = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    except FileNotFoundError:
        return
    if existing.returncode == 0:
        lines = [l for l in existing.stdout.splitlines() if cron_marker not in l]
        subprocess.run(["crontab", "-"], input="\n".join(lines) + ("\n" if lines else ""), text=True, capture_output=True)


def is_background_sync_installed():
    if sysutil.on_windows:
        return _run_entry(background_entry) is not None
    try:
        result = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    except FileNotFoundError:
        return False
    return result.returncode == 0 and cron_marker in result.stdout


def install_autostart():
    if not sysutil.on_windows:
        raise RuntimeError("Auto start with Windows only works on Windows.")
    _set_run_entry(autostart_entry, _launch_command("gui.py"))


def uninstall_autostart():
    if sysutil.on_windows:
        _set_run_entry(autostart_entry, None)


def is_autostart_installed():
    return sysutil.on_windows and _run_entry(autostart_entry) is not None


# ------------------------------------------------------------------ widgets

class QueueLogHandler(logging.Handler):

    def __init__(self, q):
        super().__init__()
        self.q = q

    def emit(self, record):
        self.q.put(self.format(record))


class _ChecklistDropdown(ttk.Frame):
    # a checklist popup that stays open while ticking, a tk.Menu closes on every pick

    def __init__(self, parent, initial_text):
        super().__init__(parent)
        self.vars = {}
        self._popup = None
        self.menubutton = ttk.Button(self, text=f"{initial_text} ▾", command=self._toggle_menu)
        self.menubutton.pack(fill="x")

    def _items(self):
        raise NotImplementedError

    def _update_label(self):
        raise NotImplementedError

    def _toggle_menu(self):
        if self._popup is not None:
            self._close_menu()
        else:
            self._open_menu()

    def _open_menu(self):
        popup = tk.Toplevel(self)
        popup.overrideredirect(True)
        popup.wm_attributes("-topmost", True)
        # a solid background and a tk managed fade keep translucency tools from drawing it black
        popup.configure(bg=ttk.Style().lookup("TFrame", "background") or "SystemButtonFace")
        popup.wm_attributes("-alpha", 0.0)
        border = ttk.Frame(popup, relief="solid", borderwidth=1)
        border.pack(fill="both", expand=True)
        for key, label in self._items():
            ttk.Checkbutton(border, text=label, variable=self.vars[key], command=self._changed).pack(anchor="w", padx=6, pady=2)
        self._popup, self._last_pos = popup, None
        self._follow_button()
        self._fade_in(popup, 0.0)
        self.menubutton.bind_all("<Button-1>", self._on_global_click, add="+")

    def _follow_button(self):
        # the popup is its own window, so keep it under the button when the tab scrolls
        if self._popup is None or not self._popup.winfo_exists():
            return
        pos = (self.menubutton.winfo_rootx(), self.menubutton.winfo_rooty() + self.menubutton.winfo_height())
        if pos != self._last_pos:
            self._popup.geometry(f"+{pos[0]}+{pos[1]}")
            self._last_pos = pos
        self._popup.after(16, self._follow_button)

    def _fade_in(self, popup, alpha):
        if popup.winfo_exists():
            alpha = min(alpha + 0.25, 1.0)
            popup.wm_attributes("-alpha", alpha)
            if alpha < 1.0:
                popup.after(15, lambda: self._fade_in(popup, alpha))

    def _close_menu(self):
        if self._popup is not None:
            self._popup.destroy()
            self._popup = None
        self.menubutton.unbind_all("<Button-1>")

    def _on_global_click(self, event):
        if self._popup is None or event.widget == self.menubutton:
            return  # the button's own command toggles it
        if self._popup.winfo_exists() and str(event.widget).startswith(str(self._popup)):
            return
        self._close_menu()

    def _changed(self):
        self._update_label()

    def selected(self):
        return [key for key, var in self.vars.items() if var.get()]


class PairSelectDropdown(_ChecklistDropdown):
    def __init__(self, parent, get_pairs, on_change=None):
        self.get_pairs = get_pairs
        self.on_change = on_change
        super().__init__(parent, "All pairs")
        self.refresh()

    def _items(self):
        return [(name, name) for name in self.vars]

    def refresh(self, selected_names=None):
        # without selected_names a pair keeps its tick and new ones start ticked
        old = self.vars
        self.vars = {}
        for pair in self.get_pairs():
            name = pair["name"]
            if selected_names is not None:
                checked = name in selected_names
            else:
                checked = old[name].get() if name in old else True
            self.vars[name] = tk.BooleanVar(value=checked)
        if self._popup is not None:
            self._close_menu()
        self._update_label()

    def _changed(self):
        self._update_label()
        if self.on_change:
            self.on_change(self.selected())

    def _update_label(self):
        total, picked = len(self.vars), len(self.selected())
        text = ("No pairs configured" if not total else "All pairs" if picked == total
                else "None selected" if not picked else f"{picked} of {total} selected")
        self.menubutton.config(text=f"{text} ▾")


class BrowserSelectDropdown(_ChecklistDropdown):
    # starts empty since clearing can't be undone

    def __init__(self, parent):
        super().__init__(parent, "No browsers selected")
        for key in bsc.browser_order:
            self.vars[key] = tk.BooleanVar(value=False)

    def _items(self):
        return [(key, bsc.browser_names[key]) for key in bsc.browser_order]

    def _update_label(self):
        total, picked = len(self.vars), len(self.selected())
        text = "No browsers selected" if not picked else "All browsers" if picked == total else f"{picked} of {total} selected"
        self.menubutton.config(text=f"{text} ▾")


class SongChecklist(ttk.LabelFrame):
    # one treeview so long lists show at once. excluded and cached_tracks are changed in place

    def __init__(self, parent, title, excluded, cached_tracks, on_refresh):
        super().__init__(parent, text=title)
        self.excluded = excluded
        self.cached_tracks = cached_tracks
        self.on_refresh = on_refresh
        self.rows = {}  # tree item -> track

        top = ttk.Frame(self)
        top.pack(fill="x", padx=6, pady=(6, 4))
        self.refresh_btn = ttk.Button(top, text="Refresh", command=self.on_refresh)
        self.refresh_btn.pack(side="left")
        self.status_var = tk.StringVar(value="Not loaded yet.")
        ttk.Label(top, textvariable=self.status_var).pack(side="left", padx=8)

        box = ttk.Frame(self)
        box.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        self.tree = ttk.Treeview(box, columns=("mark", "song"), show="headings", selectmode="none")
        self.tree.heading("mark", text="Excluded")
        self.tree.heading("song", text="Song")
        self.tree.column("mark", width=70, stretch=False, anchor="center")
        self.tree.column("song", width=260, anchor="w")
        scrollbar = ttk.Scrollbar(box, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self.tree.bind("<Button-1>", self._clicked)
        if self.cached_tracks:
            self.set_tracks(self.cached_tracks, from_cache=True)

    def set_loading(self):
        self.refresh_btn.config(state="disabled")
        self.status_var.set("Loading...")

    def set_error(self, message):
        self.refresh_btn.config(state="normal")
        self.status_var.set(message)

    def set_tracks(self, tracks, from_cache=False):
        self.refresh_btn.config(state="normal")
        self.status_var.set(f"{len(tracks)} song(s){' (from last refresh)' if from_cache else ''}.")
        if not from_cache:
            self.cached_tracks[:] = tracks
        self.tree.delete(*self.tree.get_children())
        self.rows = {}
        for track in tracks:
            label = f"{track['title']} - {track['artist']}" if track.get("artist") else track["title"]
            item = self.tree.insert("", "end", values=(self._mark(track), label))
            self.rows[item] = track

    def _mark(self, track):
        return "✔" if sync_main.is_excluded(track, self.excluded) else ""

    def _clicked(self, event):
        item = self.tree.identify_row(event.y)
        if item not in self.rows:
            return
        track = self.rows[item]
        keys = sync_main.exclusion_keys(track)
        if sync_main.is_excluded(track, self.excluded):
            self.excluded.difference_update(keys)
        else:
            self.excluded.update(keys)
        self.tree.set(item, "mark", self._mark(track))


# ---------------------------------------------------------------------- app

class ScrapifyApp:
    def __init__(self, root):
        self.root = root
        root.title(f"{app_name} {version}")
        if os.path.exists(icon_path):
            # iconphoto alone: mixing it with iconbitmap leaves the title bar icon blank
            self._icons = _load_ico_png_frames(icon_path)
            try:
                if self._icons:
                    root.iconphoto(True, *self._icons)
                else:
                    root.iconbitmap(default=icon_path)
            except tk.TclError:
                pass
        root.geometry("760x560")
        root.minsize(640, 480)

        self.config = load_config()
        self.settings = load_settings()
        if not os.path.exists(settings_file):
            save_settings(self.settings)
        self._config_mtime = self._mtime(config_file)
        self._session_mtimes = {}

        self.log_queue = queue.Queue()
        self.sync_lock = threading.Lock()
        # exclude list refreshes by platform: the two can run together, neither during a sync
        self._busy_guard = threading.Lock()
        self._refreshing = set()
        self._chromium_ok = None
        self.auto_stop = threading.Event()
        self.auto_thread = None
        self.next_run_at = None
        self.tray_icon = None
        self._tray_notified = False
        self._theme_canvases = []
        self._native_theme = ttk.Style(root).theme_use()

        notebook = ttk.Notebook(root)
        notebook.pack(fill="both", expand=True, padx=8, pady=8)
        # clicking a tab focuses the notebook and draws a focus ring; nothing needs it focused
        notebook.bind("<<NotebookTabChanged>>",
                      lambda e: root.focus_set() if root.focus_get() is notebook else None, add="+")
        self.setup_tab, self.sync_tab = ttk.Frame(notebook), ttk.Frame(notebook)
        self.automation_tab, self.settings_tab = ttk.Frame(notebook), ttk.Frame(notebook)
        for tab, text in ((self.setup_tab, "Setup"), (self.sync_tab, "Sync"),
                          (self.automation_tab, "Automation"), (self.settings_tab, "Settings")):
            notebook.add(tab, text=text)
        self._build_setup_tab()
        self._build_sync_tab()
        self._build_automation_tab()
        self._build_settings_tab()
        self._apply_theme(self.settings.get("dark_mode", False))

        handler = QueueLogHandler(self.log_queue)
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        logging.getLogger().addHandler(handler)
        logging.getLogger().setLevel(logging.INFO)

        self.root.after(150, self._poll_log_queue)
        self.root.after(1000, self._tick_automation_status)
        self.root.after(1000, self._tick_file_watch)
        self.root.after(500, self._tick_show_requests)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        if has_tray:
            self._start_tray_icon()
            if self.settings.get("launch_minimized"):
                self.root.after(200, self._minimize_to_tray)  # after the tray icon registers
        if self.settings.get("mode") == "open":
            self._start_in_app_scheduler(int(self.settings.get("interval_minutes", 30)))
        self._updating = False
        self._declined_version = None
        if sysutil.frozen:
            self.root.after(300, self._after_update)
            self.root.after(3000, self._tick_update_check)

    # ---------------------------------------------------------------- helpers

    @staticmethod
    def _mtime(path):
        try:
            return os.path.getmtime(path)
        except OSError:
            return None

    def _save_config(self):
        save_config(self.config)
        self._config_mtime = self._mtime(config_file)  # our own write, not an outside edit

    def _ui(self, fn, *args):
        # tk isn't thread safe
        self.root.after(0, lambda: fn(*args))

    def _ask_yes_no(self, title, message):
        answer, done = {}, threading.Event()

        def ask():
            try:
                answer["yes"] = messagebox.askyesno(title, message)
            finally:
                done.set()
        self.root.after(0, ask)
        done.wait()
        return answer.get("yes", False)

    def _scrollable(self, outer):
        canvas = tk.Canvas(outer, highlightthickness=0)
        self._theme_canvases.append(canvas)
        scrollbar = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        frame = ttk.Frame(canvas)
        frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        window = canvas.create_window((0, 0), window=frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.bind("<Configure>", lambda e: canvas.itemconfig(window, width=e.width))
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        wheel = lambda e: canvas.yview_scroll(int(-e.delta / 120), "units")
        canvas.bind("<Enter>", lambda e: canvas.bind_all("<MouseWheel>", wheel))
        canvas.bind("<Leave>", lambda e: canvas.unbind_all("<MouseWheel>"))
        return frame

    @staticmethod
    def _section(parent, title, text=None):
        box = ttk.LabelFrame(parent, text=title)
        box.pack(fill="x", padx=8, pady=6)
        if text:
            ttk.Label(box, text=text, wraplength=680, justify="left").pack(anchor="w", padx=8, pady=(6, 0))
        return box

    def _number_setting(self, var, key, cast, valid):
        # half typed values are left alone
        def changed(*_):
            try:
                value = cast(var.get())
            except ValueError:
                return
            if valid(value) and self.config.get(key) != value:
                self.config[key] = value
                self._save_config()
        var.trace_add("write", changed)

    def _toggle_setting(self, var, key):
        def changed():
            self.config[key] = var.get()
            self._save_config()
        return changed

    # -------------------------------------------------------------- setup tab

    def _build_setup_tab(self):
        pad = {"padx": 8, "pady": 4}
        box = ttk.LabelFrame(self.setup_tab, text="Accounts")
        box.pack(fill="x", **pad)
        box.grid_columnconfigure(1, minsize=260)  # so the Other row doesn't push the buttons sideways

        ttk.Label(box, text="Sync Browser (Chromium):").grid(row=0, column=0, sticky="w", **pad)
        self.chromium_status_var = tk.StringVar(value="Checking...")
        ttk.Label(box, textvariable=self.chromium_status_var).grid(row=0, column=1, sticky="w", **pad)
        self.chromium_install_btn = ttk.Button(box, text="Install", command=self._install_chromium_clicked)
        self.chromium_install_btn.grid(row=0, column=2, **pad)
        self._refresh_chromium_status()

        self.session_vars = {p: tk.StringVar() for p in platform_names}
        for row, platform in ((1, "spotify"), (2, "anghami")):
            ttk.Label(box, text=f"{platform_names[platform]} Session:").grid(row=row, column=0, sticky="w", **pad)
            ttk.Label(box, textvariable=self.session_vars[platform]).grid(row=row, column=1, sticky="w", **pad)

        ttk.Label(box, text="Main Browser (Grab Sessions):").grid(row=3, column=0, sticky="w", **pad)
        self.browser_var = tk.StringVar(value=self.settings.get("selected_browser", "Chrome"))
        self.browser_var.trace_add("write", lambda *_: self._save_setting("selected_browser", self.browser_var.get()))
        menu = ttk.Combobox(box, textvariable=self.browser_var, state="readonly", width=16,
                            values=[bsc.browser_names[k] for k in bsc.browser_order])
        menu.grid(row=3, column=1, sticky="w", **pad)
        self.get_sessions_btn = ttk.Button(box, text="Get Sessions", command=self._get_sessions_clicked)
        self.get_sessions_btn.grid(row=3, column=2, **pad)
        ttk.Label(box, text="You only need Get Sessions the first time, or when a sync fails with a login error.",
                  style="Muted.TLabel", wraplength=460, justify="left").grid(row=5, column=0, columnspan=3,
                                                                             sticky="w", padx=8, pady=(0, 4))

        self.custom_exe_var = tk.StringVar(value=self.settings.get("custom_browser_exe", ""))
        self.custom_exe_var.trace_add("write", lambda *_: self._save_setting("custom_browser_exe", self.custom_exe_var.get()))
        other_widgets = (
            (ttk.Label(box, text="Browser process/path:"), {"row": 6, "column": 0, "sticky": "w", **pad}),
            (ttk.Entry(box, textvariable=self.custom_exe_var, width=34), {"row": 6, "column": 1, "sticky": "w", **pad}),
            (ttk.Button(box, text="Browse", command=self._browse_custom_exe_clicked), {"row": 6, "column": 2, **pad}),
            (ttk.Label(box, text="Easiest: type your browser's process name while it's open.",
                       style="Muted.TLabel", wraplength=460, justify="left"),
             {"row": 7, "column": 0, "columnspan": 3, "sticky": "w", "padx": 8, "pady": (0, 4)}),
        )

        def show_other(*_):
            for widget, grid in other_widgets:
                if self.browser_var.get() == bsc.browser_names["other"]:
                    widget.grid(**grid)
                else:
                    widget.grid_remove()
        menu.bind("<<ComboboxSelected>>", show_other)
        show_other()
        self._refresh_session_status()

        pairs_box = ttk.LabelFrame(self.setup_tab, text="Playlist pairs")
        pairs_box.pack(fill="both", expand=True, **pad)
        tree_row = ttk.Frame(pairs_box)
        tree_row.pack(fill="both", expand=True, padx=8, pady=(8, 4))
        self.pairs_tree = ttk.Treeview(tree_row, columns=("name", "spotify", "anghami"), show="headings", height=8)
        for col, label, width in (("name", "Pair Name", 120), ("spotify", "Spotify playlist URL", 260),
                                  ("anghami", "Anghami playlist URL", 260)):
            self.pairs_tree.heading(col, text=label)
            self.pairs_tree.column(col, width=width, anchor="w")
        scrollbar = ttk.Scrollbar(tree_row, orient="vertical", command=self.pairs_tree.yview)
        self.pairs_tree.configure(yscrollcommand=scrollbar.set)
        self.pairs_tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self.pairs_tree.bind("<Double-1>", lambda e: self._edit_pair_dialog())

        buttons = ttk.Frame(pairs_box)
        buttons.pack(fill="x", padx=8, pady=(0, 8))
        for text, command in (("Add", self._add_pair_dialog), ("Edit", self._edit_pair_dialog),
                              ("Remove", self._remove_selected_pair), ("Exclude Songs", self._exclude_songs_dialog)):
            ttk.Button(buttons, text=text, command=command).pack(side="left", padx=(0, 6), ipady=2)
        self._reload_pairs_tree()

    def _save_setting(self, key, value):
        if self.settings.get(key) != value:
            self.settings[key] = value
            save_settings(self.settings)

    def _reload_pairs_tree(self):
        self.pairs_tree.delete(*self.pairs_tree.get_children())
        for pair in self.config["playlist_pairs"]:
            self.pairs_tree.insert("", "end", values=(pair["name"], pair.get("spotify_playlist_url", ""),
                                                      pair.get("anghami_playlist_url", "")))
        if hasattr(self, "manual_pair_select"):
            self.manual_pair_select.refresh(selected_names=self.config.get("manual_pair_names"))
        if hasattr(self, "automation_pair_select"):
            self.automation_pair_select.refresh(selected_names=self.config.get("automation_pair_names"))

    def _selected_pair_index(self, action):
        selection = self.pairs_tree.selection()
        if not selection:
            messagebox.showinfo(app_name, f"Select a pair to {action} first.")
            return None
        return self.pairs_tree.index(selection[0])

    def _pair_dialog(self, initial=None):
        initial = initial or {}
        dialog = tk.Toplevel(self.root)
        dialog.title("Playlist pair")
        dialog.configure(bg=ttk.Style().lookup("TFrame", "background") or "SystemButtonFace")
        dialog.transient(self.root)
        dialog.grab_set()
        fields = {"name": tk.StringVar(value=initial.get("name", "")),
                  "spotify": tk.StringVar(value=initial.get("spotify_playlist_url", "")),
                  "anghami": tk.StringVar(value=initial.get("anghami_playlist_url", ""))}
        for row, (key, label) in enumerate((("name", "Pair Name"), ("spotify", "Spotify playlist URL"),
                                            ("anghami", "Anghami playlist URL"))):
            ttk.Label(dialog, text=label + ":").grid(row=row, column=0, sticky="w", padx=8, pady=6)
            ttk.Entry(dialog, textvariable=fields[key], width=48).grid(row=row, column=1, padx=8, pady=6)
        ttk.Label(dialog, text="Note: if you leave a playlist link empty, one will be created for you.",
                  style="Muted.TLabel", wraplength=480, justify="left").grid(
            row=3, column=0, columnspan=2, sticky="w", padx=8, pady=(0, 4))
        result = {}

        def on_ok():
            name = fields["name"].get().strip()
            urls = {p: fields[p].get().strip() for p in platform_names}
            if not name or not any(urls.values()):
                messagebox.showerror(app_name, "A name and at least one playlist link are required.", parent=dialog)
                return
            if any(p["name"] == name for p in self.config["playlist_pairs"] if p is not initial):
                messagebox.showerror(app_name, f"There's already a pair called {name}.", parent=dialog)
                return
            for platform, url in urls.items():
                if url and not link_patterns[platform].match(url):
                    example = ("https://open.spotify.com/playlist/<id>" if platform == "spotify"
                               else "https://play.anghami.com/playlist/<id>")
                    messagebox.showerror(app_name, f"That doesn't look like a {platform_names[platform]} playlist link.\n\n"
                                         f"Expected something like:\n{example}", parent=dialog)
                    return
            result.update(initial)
            result["name"] = name
            for platform, url in urls.items():
                if url != initial.get(f"{platform}_playlist_url", ""):
                    result.pop(f"cached_{platform}_tracks", None)  # a new link means a new song list
                result[f"{platform}_playlist_url"] = url
            dialog.destroy()

        buttons = ttk.Frame(dialog)
        buttons.grid(row=4, column=0, columnspan=2, pady=8)
        ttk.Button(buttons, text="Cancel", command=dialog.destroy).pack(side="left", padx=6)
        ttk.Button(buttons, text="OK", command=on_ok).pack(side="left")
        dialog.bind("<Return>", lambda e: on_ok())
        dialog.wait_window()
        return result or None

    def _add_pair_dialog(self):
        pair = self._pair_dialog()
        if pair:
            self.config["playlist_pairs"].append(pair)
            self._save_config()
            self._reload_pairs_tree()

    def _edit_pair_dialog(self):
        index = self._selected_pair_index("edit")
        if index is None:
            return
        old = self.config["playlist_pairs"][index]
        pair = self._pair_dialog(initial=old)
        if not pair:
            return
        if pair["name"] != old["name"]:
            # keep the renamed pair in the saved pair selections
            for key in ("manual_pair_names", "automation_pair_names"):
                if key in self.config:
                    self.config[key] = [pair["name"] if n == old["name"] else n for n in self.config[key]]
        self.config["playlist_pairs"][index] = pair
        self._save_config()
        self._reload_pairs_tree()

    def _remove_selected_pair(self):
        index = self._selected_pair_index("remove")
        if index is None:
            return
        name = self.config["playlist_pairs"][index]["name"]
        if not messagebox.askyesno(app_name, f"Remove the pair {name}?\n\nThe playlists themselves are not touched."):
            return
        del self.config["playlist_pairs"][index]
        for key in ("manual_pair_names", "automation_pair_names"):
            if key in self.config:
                self.config[key] = [n for n in self.config[key] if n != name]
        self._save_config()
        self._reload_pairs_tree()

    def _exclude_songs_dialog(self):
        index = self._selected_pair_index("exclude songs from")
        if index is None:
            return
        pair = self.config["playlist_pairs"][index]
        excluded = {p: set(pair.get(f"excluded_{p}", [])) for p in platform_names}
        cached = {p: list(pair.get(f"cached_{p}_tracks", [])) for p in platform_names}

        win = tk.Toplevel(self.root)
        win.title(f"Exclude Songs: {pair['name']}")
        win.configure(bg=ttk.Style().lookup("TFrame", "background") or "SystemButtonFace")
        win.transient(self.root)
        win.geometry("760x500")
        win.grab_set()
        panels = ttk.Frame(win)
        panels.pack(fill="both", expand=True, padx=8, pady=8)
        panels.grid_rowconfigure(0, weight=1)
        lists = {}
        for column, platform in enumerate(platform_names):
            panels.grid_columnconfigure(column, weight=1)
            lists[platform] = SongChecklist(
                panels, f"{platform_names[platform]} songs", excluded[platform], cached[platform],
                on_refresh=lambda p=platform: self._refresh_pair_songs(p, pair.get(f"{p}_playlist_url", ""), lists[p]))
            lists[platform].grid(row=0, column=column, sticky="nsew", padx=4)
            if not cached[platform]:  # only fetch on its own the first time
                self._refresh_pair_songs(platform, pair.get(f"{platform}_playlist_url", ""), lists[platform])
        ttk.Label(win, text="Click a song to exclude it. Excluded songs are never added, removed or matched by Scrapify.",
                  style="Muted.TLabel").pack(pady=(0, 8))

        def finish():
            for platform in platform_names:
                pair[f"excluded_{platform}"] = sorted(excluded[platform])
                pair[f"cached_{platform}_tracks"] = cached[platform]
            self._save_config()
            win.destroy()

        ttk.Button(win, text="Done", command=finish).pack(pady=(0, 8))
        win.protocol("WM_DELETE_WINDOW", finish)

    def _refresh_pair_songs(self, platform, url, panel):
        panel.set_loading()
        label = platform_names[platform]
        if not url:
            panel.set_error(f"No {label} playlist link for this pair yet.")
        elif not link_patterns[platform].match(url):
            panel.set_error("That playlist link doesn't look right, fix it with Edit.")
        elif label in sync_main.missing_sessions(self.config):
            panel.set_error(f"Not logged in to {label} (or the session expired). Use Get Sessions first.")
        else:
            threading.Thread(target=self._refresh_pair_songs_worker, args=(platform, url, panel), daemon=True).start()

    def _refresh_pair_songs_worker(self, platform, url, panel):
        with self._busy_guard:
            busy = self.sync_lock.locked() or platform in self._refreshing
            if not busy:
                self._refreshing.add(platform)
        if busy:
            self._ui(panel.set_error, busy_message)
            return
        try:
            with sync_playwright() as p:
                with sync_main.make_client(platform, p, self.config) as client:
                    tracks = client.get_playlist_tracks(url)
            self._ui(lambda: panel.winfo_exists() and panel.set_tracks(tracks))
        except Exception as e:
            message = f"Couldn't load songs: {e}"
            self._ui(lambda: panel.winfo_exists() and panel.set_error(message))
        finally:
            with self._busy_guard:
                self._refreshing.discard(platform)

    # ------------------------------------------------------- files and status

    def _tick_file_watch(self):
        # session files dropped in and config.json edited outside the app
        try:
            changed = False
            for platform in platform_names:
                mtime = self._mtime(self.config[platform]["storage_state_file"])
                if mtime != self._session_mtimes.get(platform):
                    self._session_mtimes[platform] = mtime
                    changed = True
            if changed:
                self._refresh_session_status()
            mtime = self._mtime(config_file)
            if mtime != self._config_mtime:
                if mtime is None:
                    self._save_config()  # deleted while we run: write ours back
                else:
                    self._config_mtime = mtime
                    self._reload_config()
        except Exception:
            logging.getLogger().exception("File watch failed")
        self.root.after(1000, self._tick_file_watch)

    def _reload_config(self):
        self.config = load_config()
        self._config_mtime = self._mtime(config_file)
        self._reload_pairs_tree()
        self._refresh_session_status()
        self.threshold_var.set(str(self.config["match_threshold"]))
        self.timeout_var.set(str(self.config["sync_timeout_multiplier"]))
        self.retry_var.set(str(self.config["retry_attempts"]))
        self.sync_direction_var.set(self.config["sync_direction"])
        self.pause_processes_var.set(", ".join(self.config["pause_while_running"]))
        for key, var in self.config_toggles.items():
            var.set(self.config.get(key, False))

    def _refresh_session_status(self):
        for platform, var in self.session_vars.items():
            path = self.config[platform]["storage_state_file"]
            if not os.path.exists(path):
                var.set("not logged in")
            else:
                var.set("logged in" if bsc.session_file_logged_in(platform_names[platform], path) else "expired, log in again")

    def _refresh_chromium_status(self):
        self._chromium_ok = bsc.chromium_installed()
        self.chromium_status_var.set("installed" if self._chromium_ok else "not installed")
        self.chromium_install_btn.config(state="disabled" if self._chromium_ok else "normal")

    def _install_chromium_clicked(self):
        self.chromium_install_btn.config(state="disabled")
        self.chromium_status_var.set("Installing (about 150MB, may take a minute)...")

        def worker():
            try:
                ok = bsc.install_chromium(self.log_queue.put,
                                          lambda text: self._ui(self.chromium_status_var.set, f"Installing: {text}"))
            except Exception as e:
                self.log_queue.put(f"Chromium install failed: {e}")
                ok = False
            self._ui(self._refresh_chromium_status)
            if ok:
                self._ui(messagebox.showinfo, app_name, "Chromium installed.")
            else:
                self._ui(messagebox.showerror, app_name, "Chromium install failed. The Sync tab's log has details.")
        threading.Thread(target=worker, daemon=True).start()

    # ------------------------------------------------------------------ login

    def _browse_custom_exe_clicked(self):
        path = filedialog.askopenfilename(title="Select your browser", filetypes=[("Programs", "*.exe"), ("All files", "*.*")])
        if path:
            self.custom_exe_var.set(path)

    def _resolve_browser_choice(self):
        key = next((k for k, v in bsc.browser_names.items() if v == self.browser_var.get()), "other")
        if key != "other":
            return key, bsc.find_browser_exe(key)
        typed = self.custom_exe_var.get().strip()
        exe = bsc.resolve_exe(typed) if typed else None
        return key, exe and bsc.register_manual_exe(key, exe)

    def _get_sessions_clicked(self):
        key, exe = self._resolve_browser_choice()
        if not exe:
            hint = ("Type its process name while it's open, or browse to it." if key == "other"
                    else "Pick another browser, or choose Other and point to it.")
            messagebox.showwarning(app_name, f"Couldn't find {bsc.browser_display_name(key)}. {hint}")
            return
        name = bsc.browser_display_name(key, exe)
        first = True
        while bsc.is_browser_running(exe):
            note = "" if first else f"\n\n{name} is still open."
            if not messagebox.askokcancel(app_name, f"{name} is open. Close it to continue.{note}"):
                return
            first = False
            time.sleep(1)  # clicking OK doesn't mean it has finished closing
        self.get_sessions_btn.config(state="disabled")
        self.log_queue.put(f"Opening {name} for Spotify and Anghami login...")
        threading.Thread(target=self._get_sessions_worker, args=(key, exe), daemon=True).start()

    def _wait_for_login(self):
        done = threading.Event()
        self._ui(lambda: (messagebox.showinfo(
            app_name, "Log into Spotify and Anghami in the window that just opened.\n\n"
                      "Leave it open and click OK here once both show you logged in."), done.set()))
        done.wait()

    def _get_sessions_worker(self, key, exe):
        try:
            results = bsc.grab_sessions(key, exe, log=self.log_queue.put, wait_for_user=self._wait_for_login)
        except Exception as e:
            self.log_queue.put(f"Reading the sessions failed: {e}")
            results = {}
        confirmed = []
        for platform, label in platform_names.items():
            state = results.get(label)
            if not state:
                continue
            outcome = bsc.save_session(label, self.config[platform]["storage_state_file"], state)
            if outcome == "kept":
                self.log_queue.put(f"{label}: kept the working session already saved.")
            elif outcome == "signed_in":
                confirmed.append(label)
        self._ui(self.get_sessions_btn.config, {"state": "normal"})
        self._ui(self._refresh_session_status)
        missing = [label for label in platform_names.values() if label not in confirmed]
        if not missing:
            self._ui(messagebox.showinfo, app_name, "Logged in to Spotify and Anghami.")
        else:
            saved = f"Saved: {', '.join(confirmed)}\n\n" if confirmed else ""
            self._ui(messagebox.showwarning, app_name, f"{saved}Not confirmed: {', '.join(missing)}\n\n"
                     "Click Get Sessions again for those. The Sync tab's log has details.")

    # --------------------------------------------------------------- sync tab

    def _build_sync_tab(self):
        top = ttk.Frame(self.sync_tab)
        top.pack(fill="x", padx=8, pady=8)
        self.sync_buttons = []
        for direction in sync_main.directions:
            button = ttk.Button(top, text=f"Sync {sync_main.direction_label(direction)}",
                                command=lambda d=direction: self._sync_clicked(d))
            button.pack(side="left", padx=(0, 6))
            self.sync_buttons.append(button)
        self.manual_pair_select = PairSelectDropdown(top, lambda: self.config["playlist_pairs"],
                                                     lambda names: self._save_pair_selection("manual_pair_names", names))
        self.manual_pair_select.pack(side="left", padx=(6, 0))
        self.manual_pair_select.refresh(selected_names=self.config.get("manual_pair_names"))
        self.sync_status_var = tk.StringVar(value="Idle.")
        ttk.Label(top, textvariable=self.sync_status_var).pack(side="left", padx=12)

        self.log_text = tk.Text(self.sync_tab, height=20, state="disabled", wrap="word")
        self.log_text.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        scrollbar = ttk.Scrollbar(self.log_text, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")

    def _save_pair_selection(self, key, names):
        if set(names) == {p["name"] for p in self.config["playlist_pairs"]}:
            self.config.pop(key, None)  # all is stored as absent, so new pairs join in
        else:
            self.config[key] = names
        self._save_config()

    def _check_ready(self, action):
        # say so now instead of failing quietly later
        if not self._chromium_ok:
            self._refresh_chromium_status()
        if not self._chromium_ok:
            messagebox.showerror(app_name, f"The sync browser (Chromium) isn't installed.\n\n"
                                 f"Click Install on the Setup tab before {action}.")
            return False
        if not self.config["playlist_pairs"]:
            messagebox.showerror(app_name, f"No playlist pairs yet.\n\nAdd one on the Setup tab before {action}.")
            return False
        missing = sync_main.missing_sessions(self.config)
        if missing:
            messagebox.showerror(app_name, f"Not logged in to {' and '.join(missing)} (or the session expired).\n\n"
                                 f"Click Get Sessions on the Setup tab before {action}.")
            return False
        return True

    def _sync_clicked(self, direction):
        if not self._check_ready("syncing"):
            return
        names = self.manual_pair_select.selected()
        if not names:
            messagebox.showerror(app_name, "Select at least one pair to sync.")
            return
        threading.Thread(target=self._run_sync, args=(direction, names, True), daemon=True).start()

    def _confirm_create(self, pair_name, label):
        return self._ask_yes_no(f"{app_name} - {pair_name}", f"{label} playlist not found, would you like to create one?")

    def _run_sync(self, direction, pair_names=None, interactive=False):
        # interactive: someone clicked for it, so it may stop and ask. automatic runs never do and
        # report problems in the log and the tray instead of popups
        with self._busy_guard:
            acquired = not self._refreshing and self.sync_lock.acquire(blocking=False)
        if not acquired:
            self.log_queue.put(busy_message)
            return
        label = sync_main.direction_label(direction)
        try:
            self._ui(self.sync_status_var.set, f"Syncing ({label})...")
            for button in self.sync_buttons:
                self._ui(button.config, {"state": "disabled"})
            self._notify(f"Sync started ({label}).")
            summary = sync_main.main(direction=direction, pair_names=pair_names,
                                     confirm_create=self._confirm_create if interactive else None)
            self._ui(self._reload_config)  # the sync may have saved a new playlist link
            self._ui(self._report_sync_summary, summary, interactive)
            self._notify(f"Sync finished ({label}).")
        except Exception as e:
            self.log_queue.put(f"Sync crashed: {e}")
            self._ui(messagebox.showerror, app_name, f"Sync crashed:\n{e}\n\nThe log below has details.")
            self._notify("Sync ended with an error.")
        finally:
            self.sync_lock.release()
            self._ui(self.sync_status_var.set, "Idle.")
            for button in self.sync_buttons:
                self._ui(button.config, {"state": "normal"})

    def _notify(self, message, always=False):
        if (always or self.config.get("notifications_enabled")) and self.tray_icon is not None:
            try:
                self.tray_icon.notify(message, app_name)
            except Exception:
                pass

    @staticmethod
    def _summary_problems(summary):
        if summary.get("no_pairs"):
            return ["No playlist pairs yet.\n\nAdd one on the Setup tab, then sync again."]
        if summary.get("no_pairs_selected"):
            return ["No playlist pairs were selected to sync."]
        if summary.get("missing_sessions"):
            return [f"Not logged in to {' and '.join(summary['missing_sessions'])} "
                    "(or the session expired).\n\nClick Get Sessions on the Setup tab first."]
        if summary.get("missing_chromium"):
            return ["The sync browser (Chromium) isn't installed.\n\nClick Install on the Setup tab."]
        problems = []
        errors = summary.get("errors") or []
        auth = [e["pair"] for e in errors if e["type"] == "auth"]
        links = [f"{e['pair']}: {e['message']}" for e in errors if e["type"] == "invalid_link"]
        if auth:
            problems.append(f"Login problem syncing {', '.join(auth)}.\n\n"
                            "The saved session looks expired. Get a fresh one on the Setup tab.")
        if links:
            problems.append("Some playlist links look wrong:\n\n" + "\n".join(links) + "\n\nCheck them on the Setup tab.")
        return problems

    def _report_sync_summary(self, summary, interactive):
        if summary.get("busy"):
            if interactive:
                messagebox.showinfo(app_name, busy_message)
            return
        if summary.get("missing_chromium"):
            self._refresh_chromium_status()
        for problem in self._summary_problems(summary):
            if interactive:
                messagebox.showerror(app_name, problem)
            else:
                self._notify(f"Automatic sync: {problem.splitlines()[0]}", always=True)

    def _poll_log_queue(self):
        lines = []
        try:
            while True:
                lines.append(self.log_queue.get_nowait())
        except queue.Empty:
            pass
        if lines:
            self.log_text.configure(state="normal")
            self.log_text.insert("end", "\n".join(lines) + "\n")
            self.log_text.see("end")
            self.log_text.configure(state="disabled")
        self.root.after(150, self._poll_log_queue)

    # --------------------------------------------------------- automation tab

    def _build_automation_tab(self):
        frame = self._scrollable(self.automation_tab)

        box = self._section(frame, "Sync direction", "Direction for automatic syncs and the tray's Sync Now.")
        row = ttk.Frame(box)
        row.pack(fill="x", padx=8, pady=8)
        self.sync_direction_var = tk.StringVar(value=self.config["sync_direction"])
        for direction in sync_main.directions:
            ttk.Radiobutton(row, text=sync_main.direction_label(direction), variable=self.sync_direction_var,
                            value=direction, command=self._on_direction_changed).pack(side="left", padx=(0, 12))

        box = self._section(frame, "Pause when process running",
                            "Comma separated process names (e.g. game1.exe, game2.exe).\n"
                            "Automatic syncs wait quietly while any of them is running.")
        row = ttk.Frame(box)
        row.pack(fill="x", padx=8, pady=8)
        self.pause_processes_var = tk.StringVar(value=", ".join(self.config["pause_while_running"]))
        ttk.Entry(row, textvariable=self.pause_processes_var, width=50).pack(side="left")
        ttk.Button(row, text="Save", command=self._save_pause_processes).pack(side="left", padx=6)
        self.config_toggles = {"pause_while_fullscreen": tk.BooleanVar(value=self.config["pause_while_fullscreen"])}
        check = ttk.Checkbutton(box, text="Pause while any app is fullscreen" + ("" if sysutil.on_windows else " (Windows only)"),
                                variable=self.config_toggles["pause_while_fullscreen"],
                                command=self._toggle_setting(self.config_toggles["pause_while_fullscreen"], "pause_while_fullscreen"))
        check.pack(anchor="w", padx=8, pady=(0, 8))
        if not sysutil.on_windows:
            check.state(["disabled"])

        box = self._section(frame, "Pairs to sync", "Used by both kinds of automatic sync below.")
        row = ttk.Frame(box)
        row.pack(fill="x", padx=8, pady=8)
        self.automation_pair_select = PairSelectDropdown(row, lambda: self.config["playlist_pairs"],
                                                         lambda names: self._save_pair_selection("automation_pair_names", names))
        self.automation_pair_select.pack(side="left")
        self.automation_pair_select.refresh(selected_names=self.config.get("automation_pair_names"))

        self.interval_var = tk.StringVar(value=str(self.settings.get("interval_minutes", 30)))
        box = self._section(frame, "Automation", "Syncs on a timer while the app is open (it can sit in the tray).")
        row = ttk.Frame(box)
        row.pack(fill="x", padx=8, pady=8)
        ttk.Label(row, text="Sync every").pack(side="left")
        ttk.Entry(row, textvariable=self.interval_var, width=5).pack(side="left", padx=4)
        ttk.Label(row, text="minutes").pack(side="left")
        self.in_app_btn = ttk.Button(row, text="Enable", command=self._toggle_in_app_scheduler)
        self.in_app_btn.pack(side="left", padx=(12, 0))
        self.in_app_status_var = tk.StringVar(value="Off.")
        ttk.Label(row, textvariable=self.in_app_status_var).pack(side="left", padx=6)

        ttk.Separator(box, orient="horizontal").pack(fill="x", padx=8, pady=(0, 8))
        ttk.Label(box, text="Startup:").pack(anchor="w", padx=8)
        self.launch_minimized_var = tk.BooleanVar(value=self.settings.get("launch_minimized", False))
        check = ttk.Checkbutton(box, text="Launch minimized to tray" + ("" if has_tray else " (built app only)"),
                                variable=self.launch_minimized_var,
                                command=lambda: self._save_setting("launch_minimized", self.launch_minimized_var.get()))
        check.pack(anchor="w", padx=8, pady=(4, 0))
        if not has_tray:
            check.state(["disabled"])
        self.autostart_var = tk.BooleanVar(value=is_autostart_installed())
        check = ttk.Checkbutton(box, text="Start with Windows" + ("" if sysutil.on_windows else " (Windows only)"),
                                variable=self.autostart_var, command=self._on_autostart_changed)
        check.pack(anchor="w", padx=8, pady=(2, 8))
        if not sysutil.on_windows:
            check.state(["disabled"])

        box = self._section(frame, "Silent Automation",
                            "A background sync that keeps running with the app closed and starts with your computer.")
        row = ttk.Frame(box)
        row.pack(fill="x", padx=8, pady=8)
        ttk.Label(row, text="Sync every").pack(side="left")
        ttk.Entry(row, textvariable=self.interval_var, width=5).pack(side="left", padx=4)
        ttk.Label(row, text="minutes").pack(side="left")
        ttk.Button(row, text="Install", command=self._install_background_clicked).pack(side="left", padx=(12, 0))
        ttk.Button(row, text="Remove", command=self._remove_background_clicked).pack(side="left", padx=6)
        self.bg_status_var = tk.StringVar(value="Checking...")
        ttk.Label(row, textvariable=self.bg_status_var).pack(side="left", padx=6)
        ttk.Label(box, text="Use one of the two at a time; the app asks before running both.",
                  style="Muted.TLabel", wraplength=680, justify="left").pack(anchor="w", padx=8, pady=(0, 8))
        self._refresh_background_status()

    def _on_direction_changed(self):
        self.config["sync_direction"] = self.sync_direction_var.get()
        self._save_config()

    def _save_pause_processes(self):
        names = [n.strip() for n in self.pause_processes_var.get().split(",") if n.strip()]
        self.config["pause_while_running"] = names
        self._save_config()
        messagebox.showinfo(app_name, f"Saved. Automatic syncs wait while any of these run: {', '.join(names)}"
                            if names else "Saved. Automatic syncs won't wait for anything.")

    def _on_autostart_changed(self):
        want = self.autostart_var.get()
        try:
            install_autostart() if want else uninstall_autostart()
        except Exception as e:
            messagebox.showerror(app_name, f"Couldn't change starting with Windows:\n{e}")
        self.autostart_var.set(is_autostart_installed())

    def _interval(self):
        try:
            value = int(self.interval_var.get())
            if value >= 1:
                return value
        except ValueError:
            pass
        messagebox.showerror(app_name, "The interval must be a whole number of minutes, 1 or more.")
        return None

    def _toggle_in_app_scheduler(self):
        if self.auto_thread and self.auto_thread.is_alive():
            self._stop_in_app_scheduler()
            return
        if not self._check_ready("turning on automatic sync"):
            return
        interval = self._interval()
        if interval is None:
            return
        if is_background_sync_installed() and not messagebox.askyesno(
                app_name, "The background sync is installed too, so both would run. Continue?"):
            return
        self._start_in_app_scheduler(interval)

    def _start_in_app_scheduler(self, interval):
        self.auto_stop.clear()
        self.auto_thread = threading.Thread(target=self._in_app_scheduler_loop, args=(interval,), daemon=True)
        self.auto_thread.start()
        self.in_app_btn.config(text="Disable")
        self.settings.update(mode="open", interval_minutes=interval)
        save_settings(self.settings)

    def _stop_in_app_scheduler(self):
        self.auto_stop.set()
        self.next_run_at = None
        self.in_app_btn.config(text="Enable")
        self.in_app_status_var.set("Off.")
        if self.settings.get("mode") == "open":
            self._save_setting("mode", "off")

    def _in_app_scheduler_loop(self, interval):
        # same as the background sync: wait out the interval, check the pause rules, sync
        while not self.auto_stop.is_set():
            self.next_run_at = time.time() + interval * 60
            if self.auto_stop.wait(interval * 60):
                break
            reason = sysutil.pause_reason(self.config)
            if reason:
                self.log_queue.put(f"Skipping scheduled sync: {reason}.")
            else:
                self._run_sync(self.config["sync_direction"], self.config.get("automation_pair_names"))

    def _tick_automation_status(self):
        if self.auto_thread and self.auto_thread.is_alive() and self.next_run_at:
            mins, secs = divmod(max(0, int(self.next_run_at - time.time())), 60)
            self.in_app_status_var.set(f"On. Next sync in {mins:02d}:{secs:02d}.")
        self.root.after(1000, self._tick_automation_status)

    def _install_background_clicked(self):
        if not self._check_ready("installing the background sync"):
            return
        interval = self._interval()
        if interval is None:
            return
        if self.auto_thread and self.auto_thread.is_alive() and not messagebox.askyesno(
                app_name, "The in app sync is on too, so both would run. Continue?"):
            return
        try:
            install_background_sync(interval)
        except Exception as e:
            messagebox.showerror(app_name, f"Couldn't install the background sync:\n{e}")
            return
        self.settings.update(mode="always", interval_minutes=interval)
        save_settings(self.settings)
        self.root.after(1500, self._refresh_background_status)
        messagebox.showinfo(app_name, "Background sync installed and started.")

    def _remove_background_clicked(self):
        uninstall_background_sync()
        if self.settings.get("mode") == "always":
            self._save_setting("mode", "off")
        self._refresh_background_status()
        messagebox.showinfo(app_name, "Background sync removed and stopped.")

    def _refresh_background_status(self):
        if not is_background_sync_installed():
            self.bg_status_var.set("Not installed.")
        elif is_daemon_running():
            self.bg_status_var.set("Installed, running.")
        else:
            self.bg_status_var.set("Installed, starts at next log on.")

    # ----------------------------------------------------------- settings tab

    def _build_settings_tab(self):
        frame = self._scrollable(self.settings_tab)

        box = self._section(frame, "Appearance")
        self.dark_mode_var = tk.BooleanVar(value=self.settings.get("dark_mode", False))
        ttk.Checkbutton(box, text="Dark mode", variable=self.dark_mode_var,
                        command=self._on_dark_mode_changed).pack(anchor="w", padx=8, pady=8)

        box = self._section(frame, "Closing the window",
                            "Keeps Scrapify and its automatic syncing running in the tray when you close the window."
                            + ("" if has_tray else " (built app only)"))
        self.minimize_on_close_var = tk.BooleanVar(value=self.settings.get("minimize_on_close", True))
        check = ttk.Checkbutton(box, text="Minimize to tray instead of exiting", variable=self.minimize_on_close_var,
                                command=lambda: self._save_setting("minimize_on_close", self.minimize_on_close_var.get()))
        check.pack(anchor="w", padx=8, pady=(4, 8))
        if not has_tray:
            check.state(["disabled"])

        for title, text, label, key, cast, valid, hint in (
            ("Matching", "How close a title and artist must be to count as the same song. Lower catches more "
             "near misses but risks matching two different songs.",
             "Match threshold (0 to 100):", "match_threshold", int, lambda v: 0 <= v <= 100, "Recommended: 87"),
            ("Sync timeout", "Scales how long pages get to load. Raise it on a slow connection if syncs time out.",
             "Slow connection multiplier:", "sync_timeout_multiplier", float, lambda v: v > 0, "Recommended: 1.0"),
            ("Retries", "How many more times a pair is tried if its sync fails partway, before it's logged as an error.",
             "Retry attempts:", "retry_attempts", int, lambda v: v >= 0, "Recommended: 2"),
        ):
            box = self._section(frame, title, text)
            row = ttk.Frame(box)
            row.pack(fill="x", padx=8, pady=8)
            ttk.Label(row, text=label).pack(side="left")
            var = tk.StringVar(value=str(self.config[key]))
            self._number_setting(var, key, cast, valid)
            ttk.Entry(row, textvariable=var, width=6).pack(side="left", padx=4)
            ttk.Label(row, text=hint).pack(side="left", padx=(8, 0))
            setattr(self, {"match_threshold": "threshold_var", "sync_timeout_multiplier": "timeout_var",
                           "retry_attempts": "retry_var"}[key], var)

        toggles = (
            ("Browser visibility", "Shows the browser during syncs, to watch what it does. Slower.",
             "Show browser window while syncing", "show_browser"),
            ("Debugging", "When a page fails to load, saves a screenshot and a copy of it next to the app.",
             "Save debug screenshots and pages on errors", "debug_dumps"),
            ("Notifications", "A Windows notification when a sync starts and ends." + ("" if has_tray else " (built app only)"),
             "Notify when a sync starts and ends", "notifications_enabled"),
        )
        for title, text, label, key in toggles:
            box = self._section(frame, title, text)
            var = self.config_toggles[key] = tk.BooleanVar(value=self.config.get(key, False))
            check = ttk.Checkbutton(box, text=label, variable=var, command=self._toggle_setting(var, key))
            check.pack(anchor="w", padx=8, pady=(4, 8))
            if key == "notifications_enabled" and not has_tray:
                check.state(["disabled"])
            if key == "debug_dumps":
                ttk.Button(box, text="Clear Debug Files", command=self._clear_debug_files).pack(anchor="w", padx=8, pady=(0, 4))
                ttk.Button(box, text="Clear Sync Log", command=self._clear_sync_log).pack(anchor="w", padx=8, pady=(0, 8))

        box = self._section(frame, "Updates",
                            f"You have version {version}. Scrapify looks for a new version on GitHub when it opens "
                            "and twice a day while it runs. Settings and logins stay as they are when it updates."
                            + ("" if sysutil.frozen else " (built app only)"))
        self.check_updates_var = tk.BooleanVar(value=self.settings.get("check_updates", True))
        self.auto_update_var = tk.BooleanVar(value=self.settings.get("auto_update", False))
        check = ttk.Checkbutton(box, text="Check for updates automatically", variable=self.check_updates_var,
                                command=self._on_check_updates_changed)
        check.pack(anchor="w", padx=8, pady=(4, 4))
        self.auto_update_check = ttk.Checkbutton(
            box, text="Install updates without asking (Scrapify restarts on its own once no sync is running)",
            variable=self.auto_update_var,
            command=lambda: self._save_setting("auto_update", self.auto_update_var.get()))
        self.auto_update_check.pack(anchor="w", padx=(28, 8), pady=(0, 4))
        ttk.Button(box, text="Check for Updates Now", command=lambda: self._check_for_updates(quiet=False)).pack(
            anchor="w", padx=8, pady=(4, 8))
        if not sysutil.frozen:
            check.state(["disabled"])
            self.auto_update_check.state(["disabled"])
        elif not self.check_updates_var.get():
            self.auto_update_check.state(["disabled"])

        box = self._section(frame, "Your Files",
                            f"Settings, logins and the sync log are kept in {sysutil.data_dir()}, not in the app folder.")
        ttk.Button(box, text="Open Folder", command=lambda: os.startfile(sysutil.data_dir())
                   if sysutil.on_windows else None).pack(anchor="w", padx=8, pady=8)

        box = self._section(frame, "Login Profiles",
                            "Each browser used for Get Sessions keeps a private login profile in the LoginProfiles "
                            "folder there. Clearing one signs it out and deletes its data.")
        row = ttk.Frame(box)
        row.pack(fill="x", padx=8, pady=8)
        self.profile_select = BrowserSelectDropdown(row)
        self.profile_select.pack(side="left")
        ttk.Button(row, text="Clear", command=self._clear_login_profiles).pack(side="left", padx=(8, 0))

        box = self._section(frame, "Optimization", "Chromium flags that trim what a sync starts up. "
                                                   "All on by default; turn one off only if it causes trouble.")
        for key, label in browser_flag_labels:
            var = self.config_toggles[f"opt_{key}"] = tk.BooleanVar(value=self.config[f"opt_{key}"])
            ttk.Checkbutton(box, text=label, variable=var,
                            command=self._toggle_setting(var, f"opt_{key}")).pack(anchor="w", padx=8, pady=(0, 4))

    # ---------------------------------------------------------------- updates

    def _check_for_updates(self, quiet):
        # quiet is the check at launch: nothing shows unless there's an update
        if not sysutil.frozen:
            if not quiet:
                messagebox.showinfo(app_name, "Running from source, so there's nothing to update here. "
                                              "Pull the latest code instead.")
            return
        if self._updating:
            return

        def work():
            try:
                found = updater.check()
            except Exception as e:
                logging.getLogger(__name__).info(f"Couldn't check for updates: {e}")
                if not quiet:
                    self._ui(messagebox.showerror, app_name, f"Couldn't check for updates:\n{e}")
                return
            if found:
                self._ui(self._offer_update, found, quiet)
            elif not quiet:
                self._ui(messagebox.showinfo, app_name, f"You have the latest version ({version}).")
        threading.Thread(target=work, daemon=True).start()

    def _on_check_updates_changed(self):
        on = self.check_updates_var.get()
        self._save_setting("check_updates", on)
        self.auto_update_check.state(["!disabled"] if on else ["disabled"])

    def _tick_update_check(self):
        if self.settings.get("check_updates", True):
            self._check_for_updates(quiet=True)
        self.root.after(update_check_interval_ms, self._tick_update_check)

    def _offer_update(self, found, quiet=False):
        if self._updating:
            return
        if quiet and found.version == self._declined_version:
            return  # already said no to this one since opening
        if quiet and self.settings.get("auto_update") and updater.can_update():
            self._auto_update(found)
            return
        self._restore_from_tray()
        size = f"{found.size / 1_000_000:.0f} MB" if found.size else "the update"
        if not updater.can_update():
            if messagebox.askyesno(app_name, f"Scrapify {found.version} is out (you have {version}), but Scrapify "
                                             f"can't write to its own folder:\n{sysutil.app_dir()}\n\n"
                                             "Open the download page to update by hand?"):
                webbrowser.open(updater.releases_page)
            return
        if not messagebox.askyesno(app_name, f"Scrapify {found.version} is out. You have {version}.\n\n"
                                             f"Update now? It downloads {size} and restarts Scrapify. "
                                             "Your settings, logins and playlist pairs stay as they are."):
            self._declined_version = found.version
            return
        # nothing may sync while files are swapped: hold the in app lock and the one every process shares
        if self._refreshing or not self.sync_lock.acquire(blocking=False):
            messagebox.showinfo(app_name, "A sync is running. Update once it's done, from Settings > Updates.")
            return
        shared_lock = sysutil.SyncLock()
        if not shared_lock.acquire():
            self.sync_lock.release()
            messagebox.showinfo(app_name, "The background sync is in the middle of a sync. "
                                          "Update once it's done, from Settings > Updates.")
            return
        self._updating = True
        self.auto_stop.set()
        self._download_update(found, shared_lock)

    def _download_update(self, found, shared_lock):
        dialog = tk.Toplevel(self.root)
        dialog.title(f"Updating {app_name}")
        dialog.resizable(False, False)
        dialog.transient(self.root)
        dialog.grab_set()
        status = tk.StringVar(value=f"Downloading Scrapify {found.version}...")
        ttk.Label(dialog, textvariable=status, width=50).pack(padx=16, pady=(16, 8), anchor="w")
        bar = ttk.Progressbar(dialog, length=360, mode="determinate", maximum=100)
        bar.pack(padx=16, pady=(0, 8))
        cancelled = threading.Event()
        cancel = ttk.Button(dialog, text="Cancel", command=cancelled.set)
        cancel.pack(padx=16, pady=(0, 16), anchor="e")
        dialog.protocol("WM_DELETE_WINDOW", cancelled.set)

        def progress(done, total):
            if total:
                self._ui(lambda: (bar.configure(value=done * 100 / total),
                                  status.set(f"Downloading Scrapify {found.version}... "
                                             f"{done / 1_000_000:.0f} of {total / 1_000_000:.0f} MB")))

        def give_up(message=None):
            dialog.destroy()
            self._abort_update(shared_lock)
            if message:
                messagebox.showerror(app_name, message)

        def work():
            try:
                new_app = updater.download(found, progress, cancelled)
            except updater.Cancelled:
                self._ui(give_up)
                return
            except Exception as e:
                self._ui(give_up, f"The update didn't download:\n{e}\n\nNothing was changed.")
                return
            self._ui(finish, new_app)

        def finish(new_app):
            status.set("Restarting Scrapify...")
            cancel.state(["disabled"])
            problem = self._swap_and_restart(found, new_app, shared_lock)
            if problem:
                give_up(f"Couldn't start the update:\n{problem}\n\nNothing was changed.")

        threading.Thread(target=work, daemon=True).start()

    def _auto_update(self, found):
        # downloads in the background, then installs at the first moment no sync is running
        self._updating = True
        log = logging.getLogger(__name__)
        log.info(f"Downloading Scrapify {found.version} in the background")

        def work():
            try:
                new_app = updater.download(found)
            except Exception as e:
                log.warning(f"The automatic update to {found.version} didn't download: {e}")
                self._ui(self._abort_update, None)
                return
            self._ui(self._install_when_idle, found, new_app)
        threading.Thread(target=work, daemon=True).start()

    def _install_when_idle(self, found, new_app):
        retry = lambda: self.root.after(60_000, lambda: self._install_when_idle(found, new_app))
        if self._refreshing or not self.sync_lock.acquire(blocking=False):
            retry()
            return
        shared_lock = sysutil.SyncLock()
        if not shared_lock.acquire():
            self.sync_lock.release()
            retry()
            return
        self.auto_stop.set()
        self._notify(f"Updating to Scrapify {found.version}. It restarts in a moment.", always=True)
        problem = self._swap_and_restart(found, new_app, shared_lock)
        if problem:
            logging.getLogger(__name__).warning(f"The automatic update to {found.version} didn't start: {problem}")
            self._abort_update(shared_lock)

    def _swap_and_restart(self, found, new_app, shared_lock):
        # hands the swap to the update script and quits. returns what went wrong, or None
        # the background sync runs from this same exe, so it has to stop too, and comes back after
        restart_background = None
        if is_background_sync_installed() and is_daemon_running():
            restart_background = int(self.settings.get("interval_minutes", 30))
        others = updater.other_app_processes()
        for proc in others:
            sysutil.kill_pid(proc.pid)
        try:
            updater.apply(new_app, found, [os.getpid()] + [p.pid for p in others], restart_background)
        except Exception as e:
            if restart_background:
                try:
                    install_background_sync(restart_background)
                except Exception:
                    pass
            return str(e) or type(e).__name__
        shared_lock.release()
        _release_single_instance()  # or the new version would see us and quit
        self._shutdown()
        return None

    def _abort_update(self, shared_lock):
        # back to normal after an update that was cancelled or failed
        if shared_lock is not None:
            shared_lock.release()
            self.sync_lock.release()
        self._updating = False
        self.auto_stop.clear()
        if self.settings.get("mode") == "open":
            self._start_in_app_scheduler(int(self.settings.get("interval_minutes", 30)))
        updater.clean_leftovers()

    def _after_update(self):
        # the first launch after an update: say what changed, or that it didn't go through
        info = updater.take_whats_new()
        updater.clean_leftovers(min_age_seconds=300)
        if not info:
            return
        interval = info.get("restart_background")
        if interval and is_background_sync_installed() and not is_daemon_running():
            try:
                install_background_sync(int(interval))
            except Exception as e:
                logging.getLogger(__name__).warning(f"Couldn't restart the background sync after updating: {e}")
            self.root.after(1500, self._refresh_background_status)
        if not info.get("applied"):
            self._restore_from_tray()
            if messagebox.askyesno(app_name, f"The update to {info.get('version')} didn't go through, so you're "
                                             f"still on {version}. {updater.update_log} in the data folder has "
                                             "details.\n\nOpen the download page to update by hand?"):
                webbrowser.open(updater.releases_page)
            return
        self._show_whats_new(info.get("from"), info.get("notes") or "")

    def _show_whats_new(self, old_version, notes):
        self._restore_from_tray()
        dialog = tk.Toplevel(self.root)
        dialog.title(f"What's new in {app_name} {version}")
        dialog.transient(self.root)
        dialog.geometry("560x420")
        ttk.Label(dialog, text=f"Updated from {old_version} to {version}." if old_version else
                  f"Updated to {version}.").pack(anchor="w", padx=12, pady=(12, 6))
        # packed before the notes so a long list can't push it off the window
        ttk.Button(dialog, text="OK", command=dialog.destroy).pack(side="bottom", anchor="e", padx=12, pady=12)
        body = ttk.Frame(dialog)
        body.pack(fill="both", expand=True, padx=12)
        text = tk.Text(body, wrap="word", relief="flat", padx=8, pady=8, height=10, font=("Segoe UI", 10))
        scroll = ttk.Scrollbar(body, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        text.pack(side="left", fill="both", expand=True)
        if self.settings.get("dark_mode"):
            p = dark_colors
            dialog.configure(background=p["bg"])
            text.configure(background=p["field_bg"], foreground=p["fg"], insertbackground=p["fg"])
        text.tag_configure("heading", font=("Segoe UI", 11, "bold"), spacing1=6, spacing3=4)
        for line in notes.splitlines():
            if re.fullmatch(r"Scrapify \d+\.\d+\.\d+", line.strip()):
                text.insert("end", line + "\n", "heading")
            else:
                text.insert("end", (line[2:] and "•  " + line[2:] if line.startswith(("- ", "* ")) else line) + "\n")
        text.configure(state="disabled")

    def _on_dark_mode_changed(self):
        self._save_setting("dark_mode", self.dark_mode_var.get())
        # applied at startup only: switching themes live leaves bits of the old one behind
        if messagebox.askyesno(app_name, "Restart Scrapify now for the new appearance?"):
            self._restart_app()

    def _restart_app(self):
        program = sys.executable
        args = sys.argv[1:] if sysutil.frozen else [os.path.join(sysutil.app_dir(), "gui.py")] + sys.argv[1:]
        _release_single_instance()  # or the new copy would see us and quit
        try:
            subprocess.Popen([program, *args], **sysutil.no_console_kwargs())
        except Exception as e:
            _acquire_single_instance()
            self.log_queue.put(f"Couldn't restart: {e}")
            return
        self._shutdown()

    def _apply_theme(self, dark):
        style = ttk.Style(self.root)
        if not dark:
            # the native theme untouched; root and canvases keep tk's default so translucency tools work
            style.theme_use(self._native_theme)
            style.configure("Muted.TLabel", foreground=muted_gray)
            return
        # windows' native themes ignore color overrides, clam honors them
        p = dark_colors
        style.theme_use("clam")
        self.root.configure(bg=p["bg"])
        style.configure(".", background=p["bg"], foreground=p["fg"], fieldbackground=p["field_bg"])
        for widget in ("TFrame", "TLabelframe", "TPanedwindow"):
            style.configure(widget, background=p["bg"])
        for widget in ("TLabel", "TLabelframe.Label", "TCheckbutton", "TRadiobutton"):
            style.configure(widget, background=p["bg"], foreground=p["fg"])
        style.configure("Muted.TLabel", background=p["bg"], foreground=p["muted"])
        style.configure("TButton", background=p["button_bg"], foreground=p["fg"], bordercolor=p["border"])
        style.map("TButton", background=[("active", p["button_active"]), ("disabled", p["bg"])])
        style.map("TCheckbutton", background=[("active", p["bg"])],
                  indicatorcolor=[("selected", p["select_bg"]), ("!selected", p["field_bg"])])
        style.map("TRadiobutton", background=[("active", p["bg"])])
        style.configure("TEntry", fieldbackground=p["field_bg"], foreground=p["fg"], insertcolor=p["fg"], bordercolor=p["border"])
        style.configure("TCombobox", fieldbackground=p["field_bg"], foreground=p["fg"], background=p["button_bg"], arrowcolor=p["fg"])
        style.map("TCombobox", fieldbackground=[("readonly", p["field_bg"])], foreground=[("readonly", p["fg"])])
        style.configure("TNotebook", background=p["bg"], bordercolor=p["border"])
        style.configure("TNotebook.Tab", background=p["button_bg"], foreground=p["fg"])
        style.map("TNotebook.Tab", background=[("selected", p["bg"])])
        style.configure("Treeview", background=p["field_bg"], fieldbackground=p["field_bg"], foreground=p["fg"], bordercolor=p["border"])
        style.map("Treeview", background=[("selected", p["select_bg"])], foreground=[("selected", p["select_fg"])])
        style.configure("Treeview.Heading", background=p["button_bg"], foreground=p["fg"])
        style.configure("TScrollbar", background=p["button_bg"], troughcolor=p["bg"], bordercolor=p["border"], arrowcolor=p["fg"])
        self.log_text.configure(bg=p["field_bg"], fg=p["fg"], insertbackground=p["fg"])
        for canvas in self._theme_canvases:
            canvas.configure(bg=p["bg"])

    def _clear_debug_files(self):
        files = sorted(Path().glob("debug_*"))
        if not files:
            messagebox.showinfo(app_name, "No debug files to clear.")
            return
        if not messagebox.askyesno(app_name, f"Delete {len(files)} debug file(s)?"):
            return
        errors = []
        for path in files:
            try:
                path.unlink()
            except OSError as e:
                errors.append(f"{path.name}: {e}")
        if errors:
            messagebox.showerror(app_name, "Couldn't remove:\n" + "\n".join(errors))
        else:
            messagebox.showinfo(app_name, f"Removed {len(files)} debug file(s).")

    def _clear_sync_log(self):
        path = sync_main.log_file
        if not os.path.exists(path):
            messagebox.showinfo(app_name, "No sync log to clear.")
            return
        if not messagebox.askyesno(app_name, "Clear sync.log?"):
            return
        try:
            open(path, "w", encoding="utf-8").close()  # emptied, not deleted: the logger keeps it open
        except OSError as e:
            messagebox.showerror(app_name, f"Couldn't clear sync.log:\n{e}")
            return
        messagebox.showinfo(app_name, "sync.log cleared.")

    def _clear_login_profiles(self):
        keys = self.profile_select.selected()
        if not keys:
            messagebox.showinfo(app_name, "Select at least one browser first.")
            return
        names = ", ".join(bsc.browser_names[k] for k in keys)
        if not messagebox.askyesno(app_name, f"Clear the login profile for {names}?\n\nYou'll log in again next time."):
            return
        errors = []
        for key in keys:
            folder = bsc.login_profile_dir(key)
            sysutil.kill_processes_using(folder)  # a login window left open keeps it locked
            try:
                if os.path.isdir(folder):
                    shutil.rmtree(folder)
            except OSError as e:
                errors.append(f"{bsc.browser_names[key]}: {e}")
        if errors:
            messagebox.showerror(app_name, "Couldn't clear:\n" + "\n".join(errors))
        else:
            messagebox.showinfo(app_name, f"Cleared: {names}.")

    # ------------------------------------------------------------------- tray

    def _start_tray_icon(self):
        # the tray runs its own message loop on its own thread, menu actions hand work back to tk
        try:
            image = PILImage.open(icon_path)
        except Exception:
            image = PILImage.new("RGB", (32, 32), "#1db954")

        def set_direction(direction):
            def handler(icon, item):
                self.config["sync_direction"] = direction
                self._ui(self._save_config)
                self._ui(self.sync_direction_var.set, direction)
            return handler

        def toggle_fullscreen_pause(icon, item):
            var = self.config_toggles["pause_while_fullscreen"]
            self._ui(lambda: (var.set(not var.get()), self._toggle_setting(var, "pause_while_fullscreen")()))

        def sync_now(icon, item):
            # same pairs as the Sync tab, direction from the tray's own setting
            threading.Thread(target=self._run_sync, daemon=True,
                             args=(self.config["sync_direction"], self.config.get("manual_pair_names"), True)).start()

        self.tray_icon = pystray.Icon("scrapify", image, app_name, menu=pystray.Menu(
            pystray.MenuItem("Open Scrapify", lambda icon, item: self._ui(self._restore_from_tray), default=True),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Sync Now", sync_now),
            *[pystray.MenuItem(sync_main.direction_label(d), set_direction(d), radio=True,
                               checked=lambda item, d=d: self.config["sync_direction"] == d) for d in sync_main.directions],
            pystray.MenuItem("Pause while fullscreen", toggle_fullscreen_pause,
                             checked=lambda item: self.config.get("pause_while_fullscreen", False)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Exit", lambda icon, item: self._ui(self._shutdown)),
        ))
        threading.Thread(target=self.tray_icon.run, daemon=True).start()

    def _minimize_to_tray(self):
        self.root.withdraw()
        if not self._tray_notified:  # once per run
            self._tray_notified = True
            try:
                self.tray_icon.notify(f"{app_name} is still running in the background.", app_name)
            except Exception:
                pass

    def _tick_show_requests(self):
        if _show_requested():  # someone launched Scrapify again
            self._restore_from_tray()
        self.root.after(500, self._tick_show_requests)

    def _restore_from_tray(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def _on_close(self):
        if has_tray and self.tray_icon is not None and self.settings.get("minimize_on_close", True):
            self._minimize_to_tray()
        else:
            self._shutdown()

    def _shutdown(self):
        self.auto_stop.set()
        if self.tray_icon is not None:
            try:
                self.tray_icon.stop()
            except Exception:
                pass
        self.root.destroy()


# one window at a time. a named mutex frees itself however the holder dies. a second launch
# signals the named event instead and the open window shows itself
_mutex_name = "Local\\ScrapifyGUISingleInstance"
_show_event_name = "Local\\ScrapifyGUIShowWindow"
_instance_mutex = None
_show_event = None


def _acquire_single_instance():
    global _instance_mutex, _show_event
    if not sysutil.on_windows:
        return True
    import ctypes
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CreateEventW.restype = ctypes.c_void_p
    handle = kernel32.CreateMutexW(None, False, _mutex_name)
    if not handle:
        return True
    if kernel32.GetLastError() == 183:  # already exists
        kernel32.CloseHandle(ctypes.c_void_p(handle))
        event = kernel32.CreateEventW(None, False, False, _show_event_name)
        if event:
            kernel32.SetEvent(ctypes.c_void_p(event))
            kernel32.CloseHandle(ctypes.c_void_p(event))
        return False
    _instance_mutex = handle
    if _show_event is None:
        _show_event = kernel32.CreateEventW(None, False, False, _show_event_name)  # auto reset
    return True


def _release_single_instance():
    global _instance_mutex
    if _instance_mutex:
        import ctypes
        ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(_instance_mutex))
        _instance_mutex = None


def _show_requested():
    if not _show_event:
        return False
    import ctypes
    return ctypes.windll.kernel32.WaitForSingleObject(ctypes.c_void_p(_show_event), 0) == 0


def main():
    if "--daemon" in sys.argv:
        sync_main.run_daemon(interval_minutes=sync_main.cli_int("--interval", 30))
        return
    if not _acquire_single_instance():
        return
    root = tk.Tk()
    # a plain click shouldn't leave a dotted focus ring on buttons and checkboxes
    for widget in ("TButton", "TCheckbutton", "TRadiobutton", "TMenubutton"):
        root.option_add(f"*{widget}.takeFocus", 0)
    ScrapifyApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
