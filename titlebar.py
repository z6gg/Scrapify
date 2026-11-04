"""No title bar: the window buttons and the logo live in scrapify's tab row instead.

The window stays a normal window (taskbar, alt tab, snap, the minimize animation and the
resize edges all keep working); windows is only told the caption is part of the client area
so it stops drawing it. Same trick windows terminal and the browsers use.
"""
import ctypes
import sys
import tkinter as tk
from ctypes import wintypes
from tkinter import font as tkfont

on_windows = sys.platform == "win32"

WM_NCCALCSIZE = 0x0083
WM_NCLBUTTONDOWN = 0x00A1
HTCAPTION, HTTOP = 2, 12
SM_CYSIZEFRAME, SM_CXPADDEDBORDER = 33, 92
SWP_FRAMECHANGED = 0x0001 | 0x0002 | 0x0004 | 0x0010 | 0x0020  # nosize nomove nozorder noactivate framechanged

# segoe fluent icons on 11, segoe mdl2 assets on 10, same code points
glyphs = {"minimize": "", "maximize": "", "restore": "", "close": ""}
button_width, button_height = 46, 32  # windows 11's own caption buttons, at 100%

if on_windows:
    LRESULT = ctypes.c_ssize_t
    SUBCLASSPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM,
                                      wintypes.LPARAM, ctypes.c_size_t, ctypes.c_size_t)

    class NCCALCSIZE_PARAMS(ctypes.Structure):
        _fields_ = [("rgrc", wintypes.RECT * 3), ("lppos", ctypes.c_void_p)]

    user32, comctl32 = ctypes.windll.user32, ctypes.windll.comctl32
    comctl32.SetWindowSubclass.argtypes = (wintypes.HWND, SUBCLASSPROC, ctypes.c_size_t, ctypes.c_size_t)
    comctl32.DefSubclassProc.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
    comctl32.DefSubclassProc.restype = LRESULT
    user32.GetParent.argtypes = (wintypes.HWND,)
    user32.GetParent.restype = wintypes.HWND
    user32.PostMessageW.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
    user32.IsZoomed.argtypes = (wintypes.HWND,)
    user32.SetWindowPos.argtypes = (wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                    ctypes.c_int, ctypes.c_int, wintypes.UINT)


def _frame_thickness(hwnd):
    # how far a maximized window hangs off the screen at the top
    try:
        dpi = user32.GetDpiForWindow(hwnd) or 96
        return user32.GetSystemMetricsForDpi(SM_CYSIZEFRAME, dpi) + user32.GetSystemMetricsForDpi(SM_CXPADDEDBORDER, dpi)
    except AttributeError:  # before windows 10 1607
        return user32.GetSystemMetrics(SM_CYSIZEFRAME) + user32.GetSystemMetrics(SM_CXPADDEDBORDER)


class Chrome:
    """Hides the windows caption and hands out the pieces that replace it."""

    def __init__(self, root):
        self.root = root
        self.scale = root.winfo_fpixels("1i") / 96
        self.hwnd = None
        self._proc = None  # the callback has to outlive the subclass
        self.colors = {}
        self._painted = []  # widgets recolored by set_colors
        families = set(tkfont.families(root))
        self.icon_font = "Segoe Fluent Icons" if "Segoe Fluent Icons" in families else "Segoe MDL2 Assets"
        self.buttons = {}
        root.bind("<Configure>", self._sync_maximize_glyph, add="+")
        root.bind("<FocusIn>", lambda e: self._paint(), add="+")
        root.bind("<FocusOut>", lambda e: self.root.after(1, self._paint), add="+")

    def px(self, value):
        return round(value * self.scale)

    # ------------------------------------------------------------- pieces

    def caption_buttons(self, parent):
        """Minimize, maximize and close, flush to whatever corner they're placed in."""
        frame = tk.Frame(parent, bd=0, highlightthickness=0)
        self._painted.append(frame)
        for name, command in (("minimize", self._minimize), ("maximize", self._toggle_maximize),
                              ("close", self._close)):
            box = tk.Frame(frame, width=self.px(button_width), height=self.px(button_height), bd=0, highlightthickness=0)
            box.pack(side="left")
            box.pack_propagate(False)
            button = tk.Label(box, text=glyphs[name], font=(self.icon_font, 8), bd=0)
            button.pack(fill="both", expand=True)
            button.bind("<Enter>", lambda e, n=name: self._hover(n, True))
            button.bind("<Leave>", lambda e, n=name: self._hover(n, False))
            button.bind("<ButtonRelease-1>", lambda e, c=command, b=button: c() if self._inside(e, b) else None)
            self.buttons[name] = button
            self._painted.append(box)
        return frame

    def logo(self, parent, image):
        label = tk.Label(parent, image=image, bd=0)
        self._painted.append(label)
        self.drag_area(label)
        return label

    def drag_area(self, widget, where=lambda event: True):
        """Pressing on widget moves the window (where(event) says which bits of it count)."""
        def press(event):
            if where(event):
                self.start_drag(HTTOP if self._on_top_edge(event) else HTCAPTION)
                return "break"
        def double(event):
            if where(event) and not self._on_top_edge(event):
                self._toggle_maximize()
                return "break"
        def motion(event):
            cursor = "size_ns" if where(event) and self._on_top_edge(event) else ""
            if str(widget.cget("cursor")) != cursor:
                widget.configure(cursor=cursor)
        widget.bind("<ButtonPress-1>", press, add="+")
        widget.bind("<Double-Button-1>", double, add="+")
        widget.bind("<Motion>", motion, add="+")

    # ------------------------------------------------------------- colors

    def set_colors(self, bg, fg, muted, hover_bg):
        self.colors = {"bg": bg, "fg": fg, "muted": muted, "hover": hover_bg}
        self._paint()

    def _paint(self):
        if not self.colors:
            return
        p = self.colors
        for widget in self._painted:
            widget.configure(bg=p["bg"])
        fg = p["fg"] if self._is_active() else p["muted"]
        for button in self.buttons.values():
            button.configure(bg=p["bg"], fg=fg)

    def _is_active(self):
        try:
            focused = self.root.focus_displayof()
            return focused is not None and focused.winfo_toplevel() is self.root
        except (KeyError, tk.TclError):
            return True

    def _hover(self, name, inside):
        if not self.colors:
            return
        if inside:
            bg, fg = ("#c42b1c", "#ffffff") if name == "close" else (self.colors["hover"], self.colors["fg"])
        else:
            bg, fg = self.colors["bg"], self.colors["fg"] if self._is_active() else self.colors["muted"]
        self.buttons[name].configure(bg=bg, fg=fg)

    @staticmethod
    def _inside(event, widget):
        return 0 <= event.x < widget.winfo_width() and 0 <= event.y < widget.winfo_height()

    # ------------------------------------------------------------- window actions

    def maximized(self):
        if self.hwnd:
            return bool(user32.IsZoomed(self.hwnd))
        return self.root.state() == "zoomed"

    def _toggle_maximize(self):
        self.root.state("normal" if self.maximized() else "zoomed")

    def _minimize(self):
        self.root.iconify()

    def _close(self):
        # whatever WM_DELETE_WINDOW does (scrapify goes to the tray)
        handler = self.root.protocol("WM_DELETE_WINDOW")
        if handler:
            self.root.tk.call(handler)
        else:
            self.root.destroy()

    def _sync_maximize_glyph(self, event=None):
        if (event is not None and event.widget is not self.root) or "maximize" not in self.buttons:
            return
        glyph = glyphs["restore" if self.maximized() else "maximize"]
        if self.buttons["maximize"].cget("text") != glyph:
            self.buttons["maximize"].configure(text=glyph)

    def _on_top_edge(self, event):
        # the top few pixels resize, the way the windows caption's top edge does
        return not self.maximized() and event.y_root - self.root.winfo_rooty() < max(2, self.px(5))

    def start_drag(self, hit=HTCAPTION):
        if not self.hwnd:
            return
        # hand the drag to windows: native moving, aero snap and shake all come with it.
        # posted, not sent: a sent one runs windows' move loop inside this tk callback, and any
        # other tk callback that fires during the drag then crashes python
        user32.ReleaseCapture()
        user32.PostMessageW(self.hwnd, WM_NCLBUTTONDOWN, hit, 0)

    # ------------------------------------------------------------- the windows side

    def install(self):
        """Hide the windows caption. Returns False (and leaves it) if that can't be done."""
        if not on_windows:
            return False
        try:
            self.root.update_idletasks()
            self.hwnd = user32.GetParent(self.root.winfo_id())
            if not self.hwnd:
                return False

            def proc(hwnd, msg, wparam, lparam, _id, _data):
                if msg == WM_NCCALCSIZE and wparam:
                    params = ctypes.cast(lparam, ctypes.POINTER(NCCALCSIZE_PARAMS)).contents
                    top = params.rgrc[0].top
                    result = comctl32.DefSubclassProc(hwnd, msg, wparam, lparam)
                    # keep windows' side and bottom borders (resizing, the shadow), drop only the caption
                    params.rgrc[0].top = top + (_frame_thickness(hwnd) if user32.IsZoomed(hwnd) else 0)
                    return result
                return comctl32.DefSubclassProc(hwnd, msg, wparam, lparam)

            self._proc = SUBCLASSPROC(proc)
            if not comctl32.SetWindowSubclass(self.hwnd, self._proc, 1, 0):
                self._proc, self.hwnd = None, None
                return False
            user32.SetWindowPos(self.hwnd, None, 0, 0, 0, 0, SWP_FRAMECHANGED)
            self._sync_maximize_glyph()
            return True
        except Exception:
            self.hwnd = None
            return False
