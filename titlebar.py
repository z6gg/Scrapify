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

try:
    from PIL import Image, ImageDraw, ImageTk  # smooth rounded corners, tk's canvas draws them jagged
except ImportError:
    Image = None

on_windows = sys.platform == "win32"

WM_NCCALCSIZE = 0x0083
WM_NCLBUTTONDOWN = 0x00A1
HTCAPTION, HTTOP = 2, 12
SM_CYSIZEFRAME, SM_CXPADDEDBORDER = 33, 92
SWP_FRAMECHANGED = 0x0001 | 0x0002 | 0x0004 | 0x0010 | 0x0020  # nosize nomove nozorder noactivate framechanged

# segoe fluent icons on 11, segoe mdl2 assets on 10, same code points
glyphs = {"minimize": "", "maximize": "", "restore": "", "close": ""}
# at 100%: each button's cell, and the rounded frame that fades in behind it on hover
button_width, button_height = 38, 30
pill_width, pill_height, pill_radius = 32, 28, 6
fade_steps, fade_ms = 4, 18
close_red = "#c42b1c"


def _mix(a, b, t):
    """The color t of the way from hex a to hex b."""
    a, b = [tuple(int(c[i:i + 2], 16) for i in (1, 3, 5)) for c in (a, b)]
    return "#%02x%02x%02x" % tuple(round(x + (y - x) * t) for x, y in zip(a, b))

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

    def __init__(self, root, maximizable=True):
        self.root = root
        self.maximizable = maximizable  # dialogs only close: no maximize on double click or top edge resize
        self.scale = root.winfo_fpixels("1i") / 96
        self.hwnd = None
        self._proc = None  # the callback has to outlive the subclass
        self.colors = {}
        self._painted = []  # widgets recolored by set_colors
        self._texts = []  # labels drawn in the title color, dimmed while the window isn't active
        families = set(tkfont.families(root))
        self.icon_font = "Segoe Fluent Icons" if "Segoe Fluent Icons" in families else "Segoe MDL2 Assets"
        self.buttons = {}
        root.bind("<Configure>", self._sync_maximize_glyph, add="+")
        root.bind("<FocusIn>", lambda e: self._paint(), add="+")
        root.bind("<FocusOut>", lambda e: self.root.after(1, self._paint), add="+")

    def px(self, value):
        return round(value * self.scale)

    # ------------------------------------------------------------- pieces

    def caption_buttons(self, parent, names=("minimize", "maximize", "close")):
        """Minimize, maximize and close (or just some); a rounded frame fades in behind each on hover."""
        frame = tk.Frame(parent, bd=0, highlightthickness=0)
        self._painted.append(frame)
        w, h = self.px(button_width), self.px(button_height)
        commands = {"minimize": self._minimize, "maximize": self._toggle_maximize, "close": self._close}
        for name, command in ((n, commands[n]) for n in names):
            canvas = tk.Canvas(frame, width=w, height=h, bd=0, highlightthickness=0)
            canvas.pack(side="left")
            canvas.create_image(w // 2, h // 2, tags="pill", state="hidden")
            canvas.create_text(w // 2, h // 2, text=glyphs[name], font=(self.icon_font, 8), tags="glyph")
            canvas.step = 0  # how far the hover frame has faded in, 0 to fade_steps
            canvas.target = 0
            canvas.fading = False
            canvas.bind("<Enter>", lambda e, n=name: self._hover(n, True))
            canvas.bind("<Leave>", lambda e, n=name: self._hover(n, False))
            canvas.bind("<ButtonRelease-1>", lambda e, c=command, b=canvas: c() if self._inside(e, b) else None)
            self.buttons[name] = canvas
        return frame

    def header(self, parent, title, icon=None):
        """
        A dialog's whole title bar: its icon, its title and a close button, on a strip that
        moves the window. Pack it first, at the top, before anything else goes in.
        """
        bar = tk.Frame(parent, bd=0, highlightthickness=0, height=self.px(button_height + 2))
        bar.pack_propagate(False)
        self._painted.append(bar)
        buttons = self.caption_buttons(bar, names=("close",))
        buttons.pack(side="right", padx=(0, self.px(4)))
        if icon is not None:
            self.logo(bar, icon).pack(side="left", padx=(self.px(12), 0))
        text = tk.Label(bar, text=title, bd=0, padx=self.px(8 if icon is not None else 12), anchor="w",
                        font=("Segoe UI Variable Text", 9) if "Segoe UI Variable Text" in tkfont.families(self.root)
                        else ("Segoe UI", 9))
        text.pack(side="left", fill="x", expand=True)
        self._painted.append(text)
        self._texts.append(text)
        self.drag_area(bar)
        self.drag_area(text)
        return bar

    def logo(self, parent, image):
        label = tk.Label(parent, image=image, bd=0, padx=0, pady=0, highlightthickness=0)
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
            if self.maximizable and where(event) and not self._on_top_edge(event):
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
        # every fade step drawn once up front, so hovering just swaps images
        self._pills = {kind: [self._pill(_mix(bg, color, i / fade_steps), bg) for i in range(fade_steps + 1)]
                       for kind, color in (("normal", hover_bg), ("close", close_red))}
        self._paint()

    def _pill(self, color, bg):
        if Image is None:
            return None
        w, h, big = self.px(pill_width), self.px(pill_height), 4  # drawn 4x then shrunk, that's the antialiasing
        image = Image.new("RGB", (w * big, h * big), bg)
        ImageDraw.Draw(image).rounded_rectangle((0, 0, w * big - 1, h * big - 1),
                                                radius=self.px(pill_radius) * big, fill=color)
        return ImageTk.PhotoImage(image.resize((w, h), Image.LANCZOS), master=self.root)

    def _paint(self):
        if not self.colors:
            return
        for widget in self._painted:
            widget.configure(bg=self.colors["bg"])
        for text in self._texts:
            text.configure(fg=self.colors["fg"] if self._is_active() else self.colors["muted"])
        for name, canvas in self.buttons.items():
            canvas.configure(bg=self.colors["bg"])
            self._draw_button(name)

    def _draw_button(self, name):
        canvas, p = self.buttons[name], self.colors
        step, kind = canvas.step, "close" if name == "close" else "normal"
        canvas.delete("flat")
        if step == 0:
            canvas.itemconfigure("pill", state="hidden")
        elif self._pills[kind][step] is not None:
            canvas.itemconfigure("pill", image=self._pills[kind][step], state="normal")
        else:  # no pillow: a plain rectangle
            w, h = canvas.winfo_reqwidth(), canvas.winfo_reqheight()
            pw, ph = self.px(pill_width), self.px(pill_height)
            color = _mix(p["bg"], close_red if kind == "close" else p["hover"], step / fade_steps)
            canvas.create_rectangle((w - pw) // 2, (h - ph) // 2, (w + pw) // 2, (h + ph) // 2,
                                    outline="", fill=color, tags="flat")
            canvas.tag_lower("flat")
        fg = p["fg"] if self._is_active() or step else p["muted"]
        if kind == "close":
            fg = _mix(fg, "#ffffff", step / fade_steps)
        canvas.itemconfigure("glyph", fill=fg)

    def _is_active(self):
        try:
            focused = self.root.focus_displayof()
            return focused is not None and focused.winfo_toplevel() is self.root
        except (KeyError, tk.TclError):
            return True

    def _hover(self, name, inside):
        canvas = self.buttons[name]
        canvas.target = fade_steps if inside else 0
        if self.colors and not canvas.fading:
            self._fade(name)

    def _fade(self, name):
        canvas = self.buttons[name]
        if canvas.step == canvas.target:
            canvas.fading = False
            return
        canvas.fading = True
        canvas.step += 1 if canvas.target > canvas.step else -1
        self._draw_button(name)
        canvas.after(fade_ms, self._fade, name)

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
        canvas = self.buttons["maximize"]
        if canvas.itemcget("glyph", "text") != glyph:
            canvas.itemconfigure("glyph", text=glyph)

    def _on_top_edge(self, event):
        # the top few pixels resize, the way the windows caption's top edge does
        return (self.maximizable and not self.maximized()
                and event.y_root - self.root.winfo_rooty() < max(2, self.px(5)))

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
