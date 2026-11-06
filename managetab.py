"""
the manage tab: every playlist on both accounts, anghami left and spotify right. ticking a playlist
loads its songs under it; songs can be picked one by one (ctrl/shift for many, or their boxes) and
copied, moved or removed, dragged onto any playlist on either side, and whole playlists merged
into each other. merging only ever adds

the song lists draw only the rows on screen, so a playlist of thousands scrolls as fast as one of
ten, and cover art is fetched for those rows only, cached on disk after the first time
"""

import hashlib
import io
import os
import tkinter as tk
import urllib.request
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from tkinter import font as tkfont
from tkinter import ttk

import manage
from browsersessioncommon import AuthError
from matcher import normalize

try:
    from PIL import Image, ImageDraw, ImageTk
except ImportError:  # no cover art then, the rest works
    Image = None

platform_order = ("anghami", "spotify")
platform_names = {"anghami": "Anghami", "spotify": "Spotify"}
thumb_dir = "thumbs"
song_row_height = 52
song_thumb_size = 40
playlist_thumb_size = 22
check_size = 16


def _clock(seconds):
    seconds = int(round(seconds or 0))
    return f"{seconds // 60}:{seconds % 60:02d}" if seconds else ""


def _search_text(track):
    aliases = track.get("aliases") or {}
    parts = [track.get("title", ""), track.get("artist", "")] + aliases.get("titles", []) + aliases.get("artists", [])
    raw = " ".join(parts).casefold()
    return raw + " " + normalize(" ".join(parts))


def _matches(text, query):
    return all(word in text for word in query.split())


# ---------------------------------------------------------------- cover art

class Thumbs:
    """cover art by url, loaded on a few threads, kept in memory and on disk. get() returns the
    image or None, and calls on_ready once it's there"""

    def __init__(self, root, on_ready):
        self.root, self.on_ready = root, on_ready
        self._images = {}  # (url, size) -> PhotoImage, or None while loading / failed
        self._pool = ThreadPoolExecutor(max_workers=6, thread_name_prefix="thumbs") if Image else None
        self._pending_redraw = False
        try:
            os.makedirs(thumb_dir, exist_ok=True)
        except OSError:
            pass

    def get(self, url, size):
        if not url or self._pool is None:
            return None
        key = (url, size)
        if key in self._images:
            return self._images[key]
        self._images[key] = None
        self._pool.submit(self._load, url, size)
        return None

    def _load(self, url, size):
        path = os.path.join(thumb_dir, hashlib.sha1(url.encode()).hexdigest() + ".png")
        try:
            if os.path.exists(path):
                image = Image.open(path)
                image.load()
            else:
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=15) as resp:
                    data = resp.read()
                image = Image.open(io.BytesIO(data)).convert("RGB")
                image.thumbnail((64, 64), Image.LANCZOS)
                try:
                    image.save(path)
                except OSError:
                    pass
            image = image.convert("RGB").resize((size, size), Image.LANCZOS).convert("RGBA")
            mask = Image.new("L", (size, size), 0)
            ImageDraw.Draw(mask).rounded_rectangle((0, 0, size - 1, size - 1), radius=max(3, size // 8), fill=255)
            image.putalpha(mask)
        except Exception:
            return
        self.root.after(0, self._ready, url, size, image)

    def _ready(self, url, size, image):
        try:
            self._images[(url, size)] = ImageTk.PhotoImage(image, master=self.root)
        except tk.TclError:
            return
        if not self._pending_redraw:  # one redraw for a burst of arrivals
            self._pending_redraw = True
            self.root.after(60, self._flush)

    def _flush(self):
        self._pending_redraw = False
        self.on_ready()


# ---------------------------------------------------------------- song list

class SongList(ttk.Frame):
    """a canvas that draws only the visible rows: box, cover, title over a smaller artist, the
    playlist (when there are several) and the length. click picks one, ctrl+click adds or drops
    one, shift+click a range, the box always toggles. dragging picked songs out drops them on a
    playlist"""

    def __init__(self, parent, thumbs, on_selection=None, on_context=None, on_drop=None, on_delete=None,
                 on_key=None, on_open=None):
        super().__init__(parent)
        self.thumbs = thumbs
        self.on_key, self.on_open = on_key, on_open
        self.insert_at = None  # view position the reorder line is drawn above
        self.on_selection, self.on_context, self.on_drop, self.on_delete = on_selection, on_context, on_drop, on_delete
        self.rows = []  # [{"track", "playlist"}]
        self.view = []  # indexes into rows that pass the search
        self.selected = set()  # indexes into rows
        self.query = ""
        self.anchor = None
        self.hover = None
        self.top = 0
        self.empty_text = ""
        self.show_playlist = False
        self.colors = {"bg": "#ffffff", "fg": "#1c1c1c", "muted": "#6b6b6b", "select_bg": "#005fb8",
                       "border": "#e5e5e5", "hover": "#f0f0f0", "selected_row": "#dbe9f7"}
        self.canvas = tk.Canvas(self, highlightthickness=0, borderwidth=0, takefocus=1)
        self.scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.yview)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.scrollbar.pack(side="right", fill="y")
        self.title_font = tkfont.Font(family="Segoe UI", size=10, weight="bold")
        self.artist_font = tkfont.Font(family="Segoe UI", size=9)
        self.small_font = tkfont.Font(family="Segoe UI", size=8)
        self._fit_cache = {}
        self._drag = None
        c = self.canvas
        c.bind("<Configure>", lambda e: self.redraw())
        c.bind("<MouseWheel>", self._wheel)
        c.bind("<Button-1>", self._press)
        c.bind("<B1-Motion>", self._motion)
        c.bind("<ButtonRelease-1>", self._release)
        c.bind("<Button-3>", self._right_click)
        c.bind("<Motion>", self._hover)
        c.bind("<Leave>", lambda e: self._set_hover(None))
        c.bind("<Control-a>", lambda e: (self.select_all(), "break")[1])
        c.bind("<Escape>", lambda e: self.select_none())
        c.bind("<Delete>", lambda e: self.on_delete and self.on_delete())
        c.bind("<Up>", lambda e: self._step(-1, e))
        c.bind("<Down>", lambda e: self._step(1, e))
        c.bind("<Prior>", lambda e: self.yview("scroll", -1, "pages"))
        c.bind("<Next>", lambda e: self.yview("scroll", 1, "pages"))
        c.bind("<Double-Button-1>", self._double)
        for key in ("<Control-c>", "<Control-x>", "<Control-v>", "<Alt-Up>", "<Alt-Down>"):
            c.bind(key, lambda e, k=key: (self.on_key and self.on_key(k.strip("<>")), "break")[1])

    # ------------------------------------------------------------ data

    def set_rows(self, rows, show_playlist=False, keep_selection=False):
        keep = {self._row_key(self.rows[i]) for i in self.selected} if keep_selection else set()
        self.rows = rows
        self.show_playlist = show_playlist
        self.selected = {i for i, r in enumerate(rows) if self._row_key(r) in keep}
        self.anchor = None
        self._filter()

    @staticmethod
    def _row_key(row):
        t = row["track"]
        return (row["playlist"]["url"], t.get("uid") or t.get("uri") or t.get("id"), t["title"], t["artist"])

    def set_query(self, query):
        self.query = query.casefold().strip()
        self._filter()

    def _filter(self):
        if self.query:
            self.view = [i for i, r in enumerate(self.rows)
                         if _matches(r.setdefault("search", _search_text(r["track"])), self.query)]
        else:
            self.view = list(range(len(self.rows)))
        self.top = min(self.top, self._max_top())
        self.redraw()
        self._selection_changed()

    def selected_rows(self):
        return [self.rows[i] for i in sorted(self.selected)]

    def select_all(self):
        self.selected |= set(self.view)
        self.redraw()
        self._selection_changed()

    def select_none(self):
        self.selected.clear()
        self.redraw()
        self._selection_changed()

    def invert(self):
        self.selected = set(self.view) - self.selected | (self.selected - set(self.view))
        self.redraw()
        self._selection_changed()

    def select_indexes(self, indexes):
        self.selected = set(indexes)
        self.redraw()
        self._selection_changed()

    def _selection_changed(self):
        if self.on_selection:
            self.on_selection(len(self.selected), len(self.view), len(self.rows))

    # ------------------------------------------------------------ scrolling

    def _max_top(self):
        return max(0, len(self.view) * song_row_height - self.canvas.winfo_height())

    def yview(self, *args):
        height = max(1, self.canvas.winfo_height())
        total = max(1, len(self.view) * song_row_height)
        if args and args[0] == "moveto":
            self.top = float(args[1]) * total
        elif args and args[0] == "scroll":
            amount, what = int(args[1]), args[2]
            self.top += amount * (height - song_row_height if what == "pages" else song_row_height)
        self.top = int(max(0, min(self.top, self._max_top())))
        self.redraw()

    def _wheel(self, event):
        self.top = int(max(0, min(self.top - event.delta / 120 * song_row_height * 2, self._max_top())))
        self.redraw()

    def _scroll_to(self, view_pos):
        y = view_pos * song_row_height
        height = self.canvas.winfo_height()
        if y < self.top:
            self.top = y
        elif y + song_row_height > self.top + height:
            self.top = y + song_row_height - height
        self.redraw()

    # ------------------------------------------------------------ drawing

    def set_colors(self, bg, fg, muted, border, select_bg, hover, selected_row):
        self.colors = {"bg": bg, "fg": fg, "muted": muted, "border": border, "select_bg": select_bg,
                       "hover": hover, "selected_row": selected_row}
        self.canvas.configure(bg=bg)
        self.redraw()

    def _fit(self, text, font, width):
        # the text cut to width with an ellipsis, measured once per text and width
        key = (text, str(font), width)
        if key in self._fit_cache:
            return self._fit_cache[key]
        if width <= 10:
            out = ""
        elif font.measure(text) <= width:
            out = text
        else:
            lo, hi = 0, len(text)
            while lo < hi:
                mid = (lo + hi + 1) // 2
                if font.measure(text[:mid] + "…") <= width:
                    lo = mid
                else:
                    hi = mid - 1
            out = text[:lo].rstrip() + "…"
        if len(self._fit_cache) > 20000:
            self._fit_cache.clear()
        self._fit_cache[key] = out
        return out

    def redraw(self):
        c, p = self.canvas, self.colors
        c.delete("all")
        width, height = c.winfo_width(), c.winfo_height()
        total = len(self.view) * song_row_height
        if total:
            self.scrollbar.set(self.top / total, min(1.0, (self.top + height) / total))
        else:
            self.scrollbar.set(0, 1)
        if not self.view:
            text = self.empty_text if not self.rows else "No songs match the search."
            c.create_text(width // 2, max(40, height // 3), text=text, fill=p["muted"], font=self.artist_font,
                          width=max(100, width - 40), justify="center")
            return
        first = self.top // song_row_height
        last = min(len(self.view), (self.top + height) // song_row_height + 1)
        for pos in range(first, last):
            index = self.view[pos]
            row = self.rows[index]
            track = row["track"]
            y = pos * song_row_height - self.top
            picked = index in self.selected
            if picked:
                c.create_rectangle(0, y, width, y + song_row_height, fill=p["selected_row"], outline="")
            elif index == self.hover:
                c.create_rectangle(0, y, width, y + song_row_height, fill=p["hover"], outline="")
            mid = y + song_row_height // 2
            # the box
            bx = 10
            c.create_rectangle(bx, mid - check_size // 2, bx + check_size, mid + check_size // 2,
                               outline=p["select_bg"] if picked else p["muted"], width=1,
                               fill=p["select_bg"] if picked else "")
            if picked:
                c.create_line(bx + 4, mid, bx + 7, mid + 3, bx + 12, mid - 3, fill=p["bg"], width=2)
            # the cover
            tx = bx + check_size + 10
            image = self.thumbs.get(track.get("image"), song_thumb_size)
            if image is not None:
                c.create_image(tx, mid, image=image, anchor="w")
            else:
                c.create_rectangle(tx, mid - song_thumb_size // 2, tx + song_thumb_size, mid + song_thumb_size // 2,
                                   fill=p["border"], outline="")
                c.create_text(tx + song_thumb_size // 2, mid, text="♪", fill=p["muted"], font=self.title_font)
            # the length and the playlist on the right
            right = width - 10
            length = _clock(track.get("duration"))
            if length:
                c.create_text(right, mid, text=length, fill=p["muted"], font=self.artist_font, anchor="e")
                right -= self.artist_font.measure("00:00") + 12
            if self.show_playlist:
                name = self._fit(row["playlist"]["name"], self.small_font, min(110, max(0, right - tx - 160)))
                if name:
                    c.create_text(right, mid, text=name, fill=p["muted"], font=self.small_font, anchor="e")
                    right -= self.small_font.measure(name) + 12
            # the title over the artist
            nx = tx + song_thumb_size + 10
            room = right - nx
            c.create_text(nx, mid - 9, text=self._fit(track.get("title") or "?", self.title_font, room),
                          fill=p["fg"], font=self.title_font, anchor="w")
            c.create_text(nx, mid + 10, text=self._fit(track.get("artist") or "", self.artist_font, room),
                          fill=p["muted"], font=self.artist_font, anchor="w")
        if self.insert_at is not None:
            y = self.insert_at * song_row_height - self.top
            c.create_line(6, y, width - 6, y, fill=p["select_bg"], width=3)
            c.create_oval(2, y - 4, 10, y + 4, fill=p["select_bg"], outline="")

    # ------------------------------------------------------------ mouse and keys

    def _index_at(self, y):
        pos = (self.top + y) // song_row_height
        return (pos, self.view[pos]) if 0 <= pos < len(self.view) else (None, None)

    def _hover(self, event):
        self._set_hover(self._index_at(event.y)[1])

    def _set_hover(self, index):
        if index != self.hover:
            self.hover = index
            self.redraw()

    def _press(self, event):
        self.canvas.focus_set()
        pos, index = self._index_at(event.y)
        self._drag = None
        if index is None:
            if not event.state & 0x0004:
                self.select_none()
            return
        ctrl, shift = event.state & 0x0004, event.state & 0x0001
        on_box = event.x < 10 + check_size + 6
        if shift and self.anchor is not None and self.anchor in self.view:
            a = self.view.index(self.anchor)
            span = set(self.view[min(a, pos):max(a, pos) + 1])
            self.selected = (self.selected | span) if ctrl else span
        elif ctrl or on_box:
            self.selected ^= {index}
            self.anchor = index
        elif index in self.selected and len(self.selected) > 1:
            # might be the start of a drag of all of them; a plain click narrows it on release
            self._drag = {"x": event.x_root, "y": event.y_root, "active": False, "narrow": index}
            return
        else:
            self.selected = {index}
            self.anchor = index
        self._drag = {"x": event.x_root, "y": event.y_root, "active": False, "narrow": None}
        self.redraw()
        self._selection_changed()

    def _motion(self, event):
        d = self._drag
        if not d or not self.selected:
            return
        if not d["active"] and abs(event.x_root - d["x"]) + abs(event.y_root - d["y"]) > 8:
            d["active"] = True
            self.canvas.configure(cursor="plus")
        if d["active"] and self.on_drop:
            self.on_drop(event.x_root, event.y_root, final=False, shift=bool(event.state & 0x0001))

    def _release(self, event):
        d, self._drag = self._drag, None
        self.canvas.configure(cursor="")
        if not d:
            return
        if d["active"]:
            if self.on_drop:
                self.on_drop(event.x_root, event.y_root, final=True, shift=bool(event.state & 0x0001))
        elif d["narrow"] is not None:
            self.selected = {d["narrow"]}
            self.anchor = d["narrow"]
            self.redraw()
            self._selection_changed()

    def gap_at(self, y_root):
        """the view position (0..len) between rows nearest the pointer"""
        y = y_root - self.canvas.winfo_rooty() + self.top
        return max(0, min(len(self.view), int(round(y / song_row_height))))

    def set_insert(self, pos):
        if pos != self.insert_at:
            self.insert_at = pos
            self.redraw()

    def _double(self, event):
        _pos, index = self._index_at(event.y)
        if index is not None and self.on_open:
            self.on_open(self.rows[index])
        return "break"

    def _right_click(self, event):
        self.canvas.focus_set()
        _pos, index = self._index_at(event.y)
        if index is not None and index not in self.selected:
            self.selected = {index}
            self.anchor = index
            self.redraw()
            self._selection_changed()
        if self.on_context:
            self.on_context(event)

    def _step(self, direction, event):
        if not self.view:
            return "break"
        current = self.view.index(self.anchor) if self.anchor in self.view else (-1 if direction > 0 else len(self.view))
        pos = max(0, min(len(self.view) - 1, current + direction))
        index = self.view[pos]
        if event.state & 0x0001:
            self.selected.add(index)
        else:
            self.selected = {index}
        self.anchor = index
        self._scroll_to(pos)
        self._selection_changed()
        return "break"


# ---------------------------------------------------------------- one side

class PlatformPane(ttk.Frame):
    def __init__(self, parent, tab, platform):
        super().__init__(parent)
        self.tab, self.platform = tab, platform
        self.playlists = []
        self.checked = []  # urls, in the order they were ticked
        self.items = {}  # tree item -> playlist
        self.loaded = False

        head = ttk.Frame(self)
        head.pack(fill="x", pady=(0, 6))
        ttk.Label(head, text=platform_names[platform], font=("Segoe UI Variable Display", 13, "bold")).pack(side="left")
        self.status_var = tk.StringVar(value="")
        ttk.Label(head, textvariable=self.status_var, style="Muted.TLabel").pack(side="left", padx=(10, 0))
        self.refresh_btn = ttk.Button(head, text="↻", width=3, command=self.load_playlists)
        self.refresh_btn.pack(side="right")
        tab.tip(self.refresh_btn, "Load the playlists again")

        self.playlist_search = tab.search_box(self, "Search playlists", self._fill_playlists)
        self.playlist_search.pack(fill="x", pady=(0, 4))
        box = ttk.Frame(self)
        box.pack(fill="x")
        self.tree = ttk.Treeview(box, columns=("count",), show="tree", height=6, selectmode="none")
        self.tree.column("#0", width=200, stretch=True)
        self.tree.column("count", width=56, stretch=False, anchor="e")
        scroll = ttk.Scrollbar(box, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.tree.bind("<Button-1>", self._tree_click)
        self.tree.bind("<Button-3>", self._tree_menu)
        self.tree.tag_configure("readonly", foreground="#8a8a8a")
        self.tree.tag_configure("drop", background="#3a7bd5", foreground="#ffffff")

        songs_head = ttk.Frame(self)
        songs_head.pack(fill="x", pady=(10, 4))
        self.song_search = tab.search_box(songs_head, "Search songs", self._song_query)
        self.song_search.pack(side="left", fill="x", expand=True)
        self.selection_var = tk.StringVar(value="")
        ttk.Label(songs_head, textvariable=self.selection_var, style="Muted.TLabel",
                  anchor="e").pack(side="right", padx=(8, 0))

        self.songs = SongList(self, tab.thumbs, on_selection=self._selection_changed, on_context=self._song_menu,
                              on_drop=self._drop, on_delete=self.remove_selected, on_key=self._key,
                              on_open=lambda row: self._open_song(row["track"]))
        self.songs.pack(fill="both", expand=True)
        self.songs.empty_text = "Tick a playlist above to see its songs."

        actions = ttk.Frame(self)
        actions.pack(fill="x", pady=(6, 0))
        self.copy_btn = ttk.Button(actions, text="Copy to…", command=lambda: self._target_menu(self.copy_btn, move=False))
        self.move_btn = ttk.Button(actions, text="Move to…", command=lambda: self._target_menu(self.move_btn, move=True))
        self.remove_btn = ttk.Button(actions, text="Remove", command=self.remove_selected)
        self.more_btn = ttk.Button(actions, text="Select ▾", command=self._more_menu)
        for b in (self.copy_btn, self.move_btn, self.remove_btn):
            b.pack(side="left", padx=(0, 6))
        self.more_btn.pack(side="right")
        tab.tip(self.copy_btn, "Add the picked songs to another playlist, on either service")
        tab.tip(self.move_btn, "Add the picked songs to another playlist, then take them out of this one")
        tab.tip(self.remove_btn, "Take the picked songs out of their playlist (Delete key)")
        self._selection_changed(0, 0, 0)

    # ------------------------------------------------------------ playlists

    def load_playlists(self):
        problem = self.tab.session_problem(self.platform)
        if problem:
            self.status_var.set("")
            self.songs.empty_text = problem
            self.songs.set_rows([])
            return
        self.refresh_btn.config(state="disabled")
        self.status_var.set("Loading playlists…")

        def done(playlists):
            self.refresh_btn.config(state="normal")
            self.loaded = True
            known = {p["url"]: p for p in self.playlists}
            for p in playlists:  # spotify's list has no counts, keep the one a read found
                if p.get("count") is None and known.get(p["url"], {}).get("count") is not None:
                    p["count"] = known[p["url"]]["count"]
            self.playlists = playlists
            self.checked = [u for u in self.checked if any(p["url"] == u for p in playlists)]
            self._fill_playlists()
            self._update_status()
            self.tab.playlists_changed()

        def failed(e):
            self.refresh_btn.config(state="normal")
            self.status_var.set("")
            self.tab.show_error(f"Couldn't load the {platform_names[self.platform]} playlists", e)

        self.tab.workers[self.platform].submit(lambda s: s.client().list_playlists(), done, failed)

    def _update_status(self):
        n = len(self.playlists)
        self.status_var.set(f"{n} playlist{'s' if n != 1 else ''}")

    def _fill_playlists(self, *_):
        query = self.playlist_search.query()
        self.tree.delete(*self.tree.get_children())
        self.items = {}
        for p in self.playlists:
            if query and not _matches(p["name"].casefold() + " " + normalize(p["name"]), query):
                continue
            self._insert_playlist(p)
        if not self.items and self.playlists:
            self.tree.insert("", "end", text="   No playlists match.", tags=("readonly",))

    def _insert_playlist(self, p):
        box = "☑" if p["url"] in self.checked else "☐"
        count = "" if p.get("count") is None else str(p["count"])
        image = self.tab.thumbs.get(p.get("image"), playlist_thumb_size)
        kwargs = {"image": image} if image is not None else {}
        name = p["name"] or "(no name)"
        if not p.get("editable"):
            name += "  (read only)"
        item = self.tree.insert("", "end", text=f" {box}  {name}", values=(count,),
                                tags=() if p.get("editable") else ("readonly",), **kwargs)
        self.items[item] = p

    def refresh_images(self):
        for item, p in self.items.items():
            image = self.tab.thumbs.get(p.get("image"), playlist_thumb_size)
            if image is not None and not self.tree.item(item, "image"):
                self.tree.item(item, image=image)

    def _tree_click(self, event):
        item = self.tree.identify_row(event.y)
        p = self.items.get(item)
        if p is None:
            return "break"
        if p["url"] in self.checked:
            self.checked.remove(p["url"])
        else:
            self.checked.append(p["url"])
        self._refresh_item(item)
        self.show_songs()
        return "break"

    def _refresh_item(self, item):
        p = self.items[item]
        box = "☑" if p["url"] in self.checked else "☐"
        text = self.tree.item(item, "text")
        self.tree.item(item, text=f" {box}" + text[2:], values=("" if p.get("count") is None else str(p["count"]),))

    def _item_of(self, url):
        return next((i for i, p in self.items.items() if p["url"] == url), None)

    def playlist(self, url):
        return next((p for p in self.playlists if p["url"] == url), None)

    def show_only(self, url):
        self.checked = [url]
        self._fill_playlists()
        self.show_songs()

    # ------------------------------------------------------------ songs

    def show_songs(self, keep_selection=False):
        """the ticked playlists' songs, loading the ones not read yet"""
        rows, loading = [], []
        for url in self.checked:
            p = self.playlist(url)
            tracks = self.tab.songs.get(url)
            if p is None:
                continue
            if tracks is None:
                loading.append(p)
                continue
            rows += [{"track": t, "playlist": p} for t in tracks]
        if loading:
            self.songs.empty_text = "Loading songs…"
            for p in loading:
                self.load_songs(p)
        elif not self.checked:
            self.songs.empty_text = "Tick a playlist above to see its songs."
        else:
            self.songs.empty_text = "No songs in this playlist."
        self.songs.set_rows(rows, show_playlist=len(self.checked) > 1, keep_selection=keep_selection)
        self.tab.update_pair_button()

    def load_songs(self, p, then=None):
        if p["url"] in self.tab.loading:
            return
        self.tab.loading.add(p["url"])

        def done(tracks):
            self.tab.loading.discard(p["url"])
            self.tab.set_songs(p, tracks)
            if then:
                then(tracks)

        def failed(e):
            self.tab.loading.discard(p["url"])
            if p["url"] in self.checked:
                self.checked.remove(p["url"])
                item = self._item_of(p["url"])
                if item:
                    self._refresh_item(item)
            self.show_songs()
            self.tab.show_error(f"Couldn't load the songs of {p['name']}", e)

        self.tab.workers[self.platform].submit(lambda s: s.client().get_playlist_tracks(p["url"]), done, failed)

    def _song_query(self, *_):
        self.songs.set_query(self.song_search.query())

    def _selection_changed(self, picked, shown, total):
        if total and shown != total:
            self.selection_var.set(f"{picked} picked · {shown}/{total}")
        else:
            self.selection_var.set(f"{picked} picked" if picked else (f"{total} songs" if total else ""))
        state = "normal" if picked else "disabled"
        for b in (self.copy_btn, self.move_btn):
            b.config(state=state)
        editable = picked and all(r["playlist"].get("editable") for r in self.songs.selected_rows())
        self.remove_btn.config(state="normal" if editable else "disabled")

    # ------------------------------------------------------------ menus

    def _menu(self):
        return tk.Menu(self, tearoff=0)

    def _target_menu(self, button, move):
        menu = self.tab.targets_menu(self, lambda p: self.tab.send(self, p, move=move),
                                     exclude={r["playlist"]["url"] for r in self.songs.selected_rows()})
        menu.tk_popup(button.winfo_rootx(), button.winfo_rooty() + button.winfo_height())

    def _more_menu(self):
        menu = self._menu()
        menu.add_command(label="Select all  (Ctrl+A)", command=self.songs.select_all)
        menu.add_command(label="Select none  (Esc)", command=self.songs.select_none)
        menu.add_command(label="Invert selection", command=self.songs.invert)
        menu.add_separator()
        menu.add_command(label="Select duplicates", command=self.select_duplicates)
        menu.add_command(label="Select songs missing from…", command=lambda: self._missing_menu(menu))
        menu.add_separator()
        menu.add_command(label="Copy names to clipboard", command=self.copy_names)
        b = self.more_btn
        menu.tk_popup(b.winfo_rootx(), b.winfo_rooty() + b.winfo_height())

    def _missing_menu(self, _parent):
        menu = self.tab.targets_menu(self, self.select_missing_from, include_readonly=True,
                                     exclude=set(self.checked))
        b = self.more_btn
        menu.tk_popup(b.winfo_rootx(), b.winfo_rooty() + b.winfo_height())

    def _song_menu(self, event):
        rows = self.songs.selected_rows()
        if not rows:
            return
        menu = self._menu()
        exclude = {r["playlist"]["url"] for r in rows}
        menu.add_cascade(label=f"Copy {len(rows)} to", menu=self.tab.targets_menu(
            menu, lambda p: self.tab.send(self, p, move=False), exclude=exclude))
        menu.add_cascade(label=f"Move {len(rows)} to", menu=self.tab.targets_menu(
            menu, lambda p: self.tab.send(self, p, move=True), exclude=exclude))
        editable = all(r["playlist"].get("editable") for r in rows)
        menu.add_command(label="Remove from playlist", command=self.remove_selected,
                         state="normal" if editable else "disabled")
        menu.add_separator()
        menu.add_command(label="Copy names to clipboard", command=self.copy_names)
        if len(rows) == 1:
            t = rows[0]["track"]
            link = (f"https://open.spotify.com/track/{t['uri'].rsplit(':', 1)[-1]}" if t.get("uri")
                    else f"https://play.anghami.com/song/{t['id']}" if t.get("id") else None)
            if link:
                menu.add_command(label=f"Open on {platform_names[self.platform]}", command=lambda: webbrowser.open(link))
        menu.tk_popup(event.x_root, event.y_root)

    def _tree_menu(self, event):
        item = self.tree.identify_row(event.y)
        p = self.items.get(item)
        if p is None:
            return
        menu = self._menu()
        menu.add_command(label="Show only this playlist", command=lambda: self.show_only(p["url"]))
        menu.add_cascade(label="Merge into", menu=self.tab.targets_menu(
            menu, lambda dest: self.tab.merge(p, dest), exclude={p["url"]}))
        menu.add_command(label="Merge…", command=lambda: self.tab.merge_dialog(source=p))
        other = self.tab.panes["spotify" if self.platform == "anghami" else "anghami"]
        pair_menu = tk.Menu(menu, tearoff=0)
        for q in other.playlists:
            pair_menu.add_command(label=q["name"] or "(no name)", command=lambda q=q: self.tab.make_pair(p, q))
        if not other.playlists:
            pair_menu.add_command(label="Not loaded yet", state="disabled")
        menu.add_cascade(label=f"Make sync pair with {platform_names[other.platform]}", menu=pair_menu)
        menu.add_separator()
        menu.add_command(label="Reload its songs", command=lambda: self.reload(p))
        menu.add_command(label=f"Open on {platform_names[self.platform]}", command=lambda: webbrowser.open(p["url"]))
        menu.tk_popup(event.x_root, event.y_root)

    def reload(self, p):
        self.tab.songs.pop(p["url"], None)
        if p["url"] not in self.checked:
            self.checked.append(p["url"])
            item = self._item_of(p["url"])
            if item:
                self._refresh_item(item)
        self.show_songs()

    # ------------------------------------------------------------ tools

    def select_duplicates(self):
        by_playlist = {}
        for i, r in enumerate(self.songs.rows):
            by_playlist.setdefault(r["playlist"]["url"], []).append(i)
        dupes = []
        for indexes in by_playlist.values():
            tracks = [self.songs.rows[i]["track"] for i in indexes]
            dupes += [indexes[j] for j in manage.duplicates(tracks)]
        self.songs.select_indexes(dupes)
        if not dupes:
            self.tab.set_status("No duplicates in the ticked playlists.")
        else:
            self.tab.set_status(f"Picked {len(dupes)} extra cop{'ies' if len(dupes) != 1 else 'y'}; "
                                f"Remove takes them out and keeps the first of each.")

    def select_missing_from(self, other):
        def pick(other_tracks):
            rows = self.songs.rows
            missing = manage.missing_from([r["track"] for r in rows], other_tracks, other["platform"],
                                          self.tab.threshold())
            ids = {id(t) for t in missing}
            self.songs.select_indexes([i for i, r in enumerate(rows) if id(r["track"]) in ids])
            self.tab.set_status(f"Picked {len(ids)} song(s) that {other['name']} doesn't have.")

        tracks = self.tab.songs.get(other["url"])
        if tracks is not None:
            pick(tracks)
        else:
            self.tab.set_status(f"Reading {other['name']}…")
            self.tab.panes[other["platform"]].load_songs(other, then=pick)

    def copy_names(self):
        rows = self.songs.selected_rows() or [self.songs.rows[i] for i in self.songs.view]
        text = "\n".join(f"{r['track']['title']} - {r['track']['artist']}" for r in rows)
        self.clipboard_clear()
        self.clipboard_append(text)
        self.tab.set_status(f"Copied {len(rows)} name(s).")

    def remove_selected(self):
        rows = self.songs.selected_rows()
        if not rows or not all(r["playlist"].get("editable") for r in rows):
            return
        self.tab.remove(self, rows)

    # ------------------------------------------------------------ dragging

    def _drop(self, x_root, y_root, final, shift=False):
        self.tab.drag_over(self, x_root, y_root, final, shift)

    def single_target(self):
        """the one ticked playlist songs dropped or pasted here go into, if there's exactly one"""
        if len(self.checked) == 1:
            p = self.playlist(self.checked[0])
            return p if p and p.get("editable") else None
        return None

    def can_reorder(self):
        return (self.platform == "spotify" and len(self.checked) == 1 and not self.songs.query
                and bool(self.single_target()))

    def _key(self, key):
        rows = self.songs.selected_rows()
        if key in ("Control-c", "Control-x"):
            if rows:
                self.tab.clip = {"rows": rows, "move": key == "Control-x", "pane": self}
                text = "\n".join(f"{r['track']['title']} - {r['track']['artist']}" for r in rows)
                self.clipboard_clear()
                self.clipboard_append(text)
                verb = "cut" if key == "Control-x" else "copied"
                self.tab.set_status(f"{len(rows)} song(s) {verb}. Click the other list (or a playlist's songs) "
                                    f"and press Ctrl+V.")
        elif key == "Control-v":
            self.tab.paste(self)
        elif key in ("Alt-Up", "Alt-Down") and rows:
            self.tab.nudge(self, -1 if key == "Alt-Up" else 1)

    def _open_song(self, t):
        link = (f"https://open.spotify.com/track/{t['uri'].rsplit(':', 1)[-1]}" if t.get("uri")
                else f"https://play.anghami.com/song/{t['id']}" if t.get("id") else None)
        if link:
            webbrowser.open(link)

    def playlist_at(self, x_root, y_root):
        widget = self.winfo_containing(x_root, y_root)
        if widget is not self.tree:
            return None, None
        item = self.tree.identify_row(y_root - self.tree.winfo_rooty())
        return item, self.items.get(item)


# ---------------------------------------------------------------- the tab

class SearchBox(ttk.Frame):
    def __init__(self, parent, placeholder, on_change):
        super().__init__(parent)
        self.var = tk.StringVar()
        self.entry = ttk.Entry(self, textvariable=self.var)
        self.entry.pack(side="left", fill="x", expand=True)
        self.clear = ttk.Button(self, text="✕", width=3, command=lambda: (self.var.set(""), self.entry.focus_set()))
        self.placeholder = placeholder
        self._showing_placeholder = False
        self._after = None
        self.on_change = on_change
        self.entry.bind("<FocusIn>", self._focus_in)
        self.entry.bind("<FocusOut>", self._focus_out)
        self.entry.bind("<Escape>", lambda e: self.var.set(""))
        self.var.trace_add("write", self._changed)
        self._focus_out()

    def _focus_in(self, _event=None):
        if self._showing_placeholder:
            self._showing_placeholder = False
            self.var.set("")
            self.entry.configure(style="TEntry")

    def _focus_out(self, _event=None):
        if not self.var.get():
            self._showing_placeholder = True
            self.var.set(self.placeholder)
            self.entry.configure(style="Placeholder.TEntry")

    def query(self):
        return "" if self._showing_placeholder else self.var.get().casefold().strip()

    def _changed(self, *_):
        if self._showing_placeholder:
            return
        if self.var.get():
            self.clear.pack(side="right", padx=(4, 0))
        else:
            self.clear.pack_forget()
        if self._after:
            self.after_cancel(self._after)
        # a short wait so a fast typist doesn't refilter on every key of a long list
        self._after = self.after(120, self.on_change)


class ManageTab(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        self.songs = {}  # playlist url -> tracks, as last read
        self.loading = set()
        self.thumbs = Thumbs(app.root, self._thumbs_ready)
        self.workers = {p: manage.Worker(p, lambda: app.config, app._manage_begin, app._manage_end, app._ui)
                        for p in platform_order}
        self._tip = None
        self._drop_target = None
        self._shown = False
        self._drag_tip = None
        self.clip = None  # songs copied or cut with ctrl+c / ctrl+x

        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=10, pady=(10, 6))
        self.merge_btn = ttk.Button(bar, text="Merge playlists…", command=self.merge_dialog,
                                    style=app.accent_style)
        self.merge_btn.pack(side="left")
        self.pair_btn = ttk.Button(bar, text="Make sync pair", command=self.make_pair, state="disabled")
        self.pair_btn.pack(side="left", padx=(6, 0))
        self.tip(self.pair_btn, "Tick one playlist on each side, then this adds them as a pair to sync")
        self.tip(self.merge_btn, "Add everything in one playlist to another. Nothing is ever removed.")
        self.progress = ttk.Progressbar(bar, mode="indeterminate", length=80)
        self.status_var = tk.StringVar(value="")
        help_label = ttk.Label(bar, text="ⓘ Controls", style="Muted.TLabel", cursor="question_arrow")
        help_label.pack(side="right", padx=(8, 0))
        self.tip(help_label, "Click a song to pick it, Ctrl+click for more, Shift+click for a range, or tick its box.\n"
                             "Drag picked songs onto a playlist (either side) to copy them. Hold Shift to move them.\n"
                             "Drop them on the other side's songs to add them to its ticked playlist.\n"
                             "Ctrl+C or Ctrl+X, then Ctrl+V in the other list: copy or move.\n"
                             "Drag inside a Spotify playlist, or Alt+Up / Alt+Down, to reorder it.\n"
                             "Delete removes, Ctrl+A picks all, Esc picks none, double-click opens the song.\n"
                             "Right-click a song or a playlist for everything else.", right=True)
        ttk.Label(bar, textvariable=self.status_var, style="Muted.TLabel").pack(side="left", padx=(12, 0))

        panes = ttk.PanedWindow(self, orient="horizontal")
        panes.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        self.panes = {}
        for platform in platform_order:
            pane = PlatformPane(panes, self, platform)
            panes.add(pane, weight=1)
            self.panes[platform] = pane
        # a gap between the two sides
        self.panes["anghami"].configure(padding=(0, 0, 8, 0))
        self.panes["spotify"].configure(padding=(8, 0, 0, 0))
        self._busy_count = 0
        self._tick_busy()

    # ------------------------------------------------------------ helpers for the panes

    def search_box(self, parent, placeholder, on_change):
        return SearchBox(parent, placeholder, on_change)

    def tip(self, widget, text, right=False):
        widget.bind("<Enter>", lambda e: self._show_tip(widget, text, right), add="+")
        widget.bind("<Leave>", lambda e: self._hide_tip(), add="+")

    def _show_tip(self, widget, text, right=False):
        self._hide_tip()
        tip = tk.Toplevel(widget)
        tip.wm_overrideredirect(True)
        p = self.app._colors
        tk.Label(tip, text=text, bg=p.get("field_bg", "#ffffff"), fg=p.get("fg", "#000000"), padx=8, pady=4,
                 font=("Segoe UI", 9), relief="solid", borderwidth=1, justify="left").pack()
        tip.update_idletasks()
        x = widget.winfo_rootx() + widget.winfo_width() - tip.winfo_reqwidth() if right else widget.winfo_rootx()
        tip.geometry(f"+{x}+{widget.winfo_rooty() + widget.winfo_height() + 4}")
        self._tip = tip

    def _hide_tip(self):
        if self._tip is not None:
            self._tip.destroy()
            self._tip = None

    def threshold(self):
        return int(self.app.config.get("match_threshold", 87))

    def session_problem(self, platform):
        return self.app._manage_session_problem(platform)

    def set_status(self, text):
        self.status_var.set(text)

    def show_error(self, what, error):
        message = str(error)
        if isinstance(error, AuthError):
            message += "\n\nGet a fresh session on the Setup tab."
        self.set_status(f"{what}.")
        self.app.messages.showerror("Scrapify", f"{what}:\n{message}")

    def _tick_busy(self):
        busy = any(w.busy for w in self.workers.values()) or any(not w._tasks.empty() for w in self.workers.values())
        if busy and not self.progress.winfo_ismapped():
            self.progress.pack(side="right")
            self.progress.start(12)
        elif not busy and self.progress.winfo_ismapped():
            self.progress.stop()
            self.progress.pack_forget()
        self.after(250, self._tick_busy)

    def on_shown(self):
        """the first time the tab is opened, both lists load"""
        if self._shown:
            return
        self._shown = True
        for pane in self.panes.values():
            pane.load_playlists()

    def stop(self):
        for w in self.workers.values():
            w.stop()

    def _thumbs_ready(self):
        for pane in self.panes.values():
            pane.songs.redraw()
            pane.refresh_images()

    def set_colors(self, dark, bg, fg, muted, border, select_bg):
        hover = "#2d2d2d" if dark else "#f2f2f2"
        selected_row = "#203a52" if dark else "#dceaf8"
        for pane in self.panes.values():
            pane.songs.set_colors(bg, fg, muted, border, select_bg, hover, selected_row)
            pane.tree.tag_configure("drop", background=select_bg, foreground=bg)
        style = ttk.Style(self)
        style.configure("Placeholder.TEntry", foreground=muted)

    def update_pair_button(self):
        ok = all(len(self.panes[p].checked) == 1 for p in platform_order)
        self.pair_btn.config(state="normal" if ok else "disabled")

    def make_pair(self, a=None, b=None):
        if a is None:
            a, b = (self.panes[p].playlist(self.panes[p].checked[0]) if len(self.panes[p].checked) == 1 else None
                    for p in platform_order)
        if not a or not b:
            return
        anghami, spotify = (a, b) if a["platform"] == "anghami" else (b, a)
        pair = self.app._pair_from_playlists(anghami, spotify)
        if pair:
            self.set_status(f"Added the sync pair {pair['name']}. It syncs from the Sync tab.")

    def all_playlists(self):
        return [p for platform in platform_order for p in self.panes[platform].playlists]

    def playlists_changed(self):
        pass

    def set_songs(self, playlist, tracks):
        self.songs[playlist["url"]] = tracks
        playlist["count"] = len(tracks)
        pane = self.panes[playlist["platform"]]
        item = pane._item_of(playlist["url"])
        if item:
            pane._refresh_item(item)
        if playlist["url"] in pane.checked:
            pane.show_songs(keep_selection=True)

    def targets_menu(self, parent, command, exclude=(), include_readonly=False):
        """a menu of every playlist to send to, one submenu per service"""
        menu = tk.Menu(parent, tearoff=0)
        for platform in platform_order:
            sub = tk.Menu(menu, tearoff=0)
            pane = self.panes[platform]
            choices = [p for p in pane.playlists if (include_readonly or p.get("editable")) and p["url"] not in exclude]
            for p in choices:
                count = "" if p.get("count") is None else f"  ({p['count']})"
                sub.add_command(label=f"{p['name'] or '(no name)'}{count}", command=lambda p=p: command(p))
            if not choices:
                sub.add_command(label="Not loaded yet" if not pane.loaded else "No playlists", state="disabled")
            menu.add_cascade(label=platform_names[platform], menu=sub)
        return menu

    # ------------------------------------------------------------ actions

    def send(self, source_pane, dest, move):
        rows = source_pane.songs.selected_rows()
        rows = [r for r in rows if r["playlist"]["url"] != dest["url"]]
        if not rows:
            return
        if move and not all(r["playlist"].get("editable") for r in rows):
            self.app.messages.showinfo("Scrapify", "Some of those songs are in a read only playlist, so they can "
                                                   "only be copied, not moved.")
            return
        tracks = [r["track"] for r in rows]
        verb = "Moving" if move else "Copying"
        cross = any(manage.platform_of(t) != dest["platform"] for t in tracks)
        self.set_status(f"{verb} {len(tracks)} song(s) to {dest['name']}"
                        f"{' (searching ' + platform_names[dest['platform']] + ' for each)' if cross else ''}…")

        def done(result):
            self.set_songs(dest, result["tracks"])
            added, already, failed = result["added"], result["already"], result["failed"]
            parts = [f"{added} added to {dest['name']}"]
            if already:
                parts.append(f"{already} already there")
            if failed:
                parts.append(f"{len(failed)} not found")
            self.set_status(", ".join(parts) + ".")
            if failed:
                self._report_failed(f"Couldn't add these to {dest['name']}", failed)
            if move:
                failed_ids = {id(t) for t in failed}
                moved = [r for r in rows if id(r["track"]) not in failed_ids]
                if moved:
                    self.remove(source_pane, moved, quiet=True)

        def failed(e):
            self.show_error(f"Couldn't add the songs to {dest['name']}", e)

        self.workers[dest["platform"]].submit(lambda s: manage.add_songs(s, dest["url"], tracks), done, failed)

    def remove(self, pane, rows, quiet=False):
        by_playlist = {}
        for r in rows:
            by_playlist.setdefault(r["playlist"]["url"], (r["playlist"], []))[1].append(r["track"])
        if not quiet:
            names = ", ".join(p["name"] for p, _t in by_playlist.values())
            if not self.app.messages.askyesno("Remove songs",
                                              f"Take {len(rows)} song(s) out of {names}?"):
                return
        for p, tracks in by_playlist.values():
            self.set_status(f"Removing {len(tracks)} song(s) from {p['name']}…")

            def done(result, p=p):
                self.set_songs(p, result["tracks"])
                msg = f"{result['removed']} removed from {p['name']}."
                if result["failed"]:
                    msg = msg[:-1] + f", {len(result['failed'])} couldn't be."
                self.set_status(msg)

            def failed(e, p=p):
                self.show_error(f"Couldn't remove songs from {p['name']}", e)

            pane_platform = p["platform"]
            self.workers[pane_platform].submit(lambda s, p=p, tracks=tracks: manage.remove_songs(s, p["url"], tracks),
                                               done, failed)
        pane.songs.select_none()

    def merge(self, source, dest):
        """adds every song in source that dest doesn't have to dest, after saying how many"""
        self.set_status(f"Comparing {source['name']} with {dest['name']}…")

        def compare(source_tracks):
            def work(s):
                dest_tracks = s.client().get_playlist_tracks(dest["url"])
                return dest_tracks, manage.missing_from(source_tracks, dest_tracks, dest["platform"], s.threshold)

            def compared(result):
                dest_tracks, missing = result
                self.set_songs(dest, dest_tracks)
                if not missing:
                    self.set_status(f"{dest['name']} already has everything in {source['name']}.")
                    self.app.messages.showinfo("Merge", f"{dest['name']} already has every song in {source['name']}.")
                    return
                cross = source["platform"] != dest["platform"]
                note = (f"\n\nThey're on different services, so each song is searched for on "
                        f"{platform_names[dest['platform']]}; ones it can't find are listed after.") if cross else ""
                if not self.app.messages.askyesno(
                        "Merge", f"Add {len(missing)} song(s) from {source['name']} to {dest['name']}?\n"
                                 f"{len(source_tracks) - len(missing)} are already there. Nothing is removed "
                                 f"from either playlist.{note}"):
                    self.set_status("")
                    return
                self.set_status(f"Adding {len(missing)} song(s) to {dest['name']}…")

                def added(result):
                    self.set_songs(dest, result["tracks"])
                    self.set_status(f"Merged: {result['added']} added to {dest['name']}"
                                    f"{', ' + str(len(result['failed'])) + ' not found' if result['failed'] else ''}.")
                    if result["failed"]:
                        self._report_failed(f"Couldn't add these to {dest['name']}", result["failed"])

                self.workers[dest["platform"]].submit(lambda s: manage.add_songs(s, dest["url"], missing), added,
                                                      lambda e: self.show_error("The merge stopped", e))

            self.workers[dest["platform"]].submit(work, compared,
                                                  lambda e: self.show_error(f"Couldn't read {dest['name']}", e))

        tracks = self.songs.get(source["url"])
        if tracks is not None:
            compare(tracks)
        else:
            self.panes[source["platform"]].load_songs(source, then=compare)

    def merge_dialog(self, source=None):
        playlists = self.all_playlists()
        if not playlists:
            self.app.messages.showinfo("Merge", "The playlists haven't loaded yet.")
            return
        label = lambda p: f"{platform_names[p['platform']]} · {p['name'] or '(no name)'}"
        labels = [label(p) for p in playlists]
        editable = [label(p) for p in playlists if p.get("editable")]
        by_label = {label(p): p for p in playlists}
        dialog = self.app._dialog("Merge playlists")
        body = ttk.Frame(dialog, padding=(16, 12, 16, 0))
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="Adds every song in the first playlist that the second doesn't have yet.\n"
                             "Nothing is removed from either one. They can be on different services.",
                  style="Muted.TLabel", justify="left").grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 10))
        from_var = tk.StringVar(value=label(source) if source else "")
        into_var = tk.StringVar(value="")
        def resolve(var, choices):
            """the playlist the box means: picked from the list, a name typed in full, or the only
            one containing what's typed"""
            text = var.get().strip()
            if text in by_label:
                return by_label[text]
            q = text.casefold()
            if not q:
                return None
            exact = [c for c in choices if by_label[c]["name"].casefold() == q]
            if len(exact) == 1:
                return by_label[exact[0]]
            hits = [c for c in choices if q in c.casefold()]
            return by_label[hits[0]] if len(hits) == 1 else None

        def picker(row, title, var, choices):
            # typing narrows the dropdown to the names containing it; the line under says which one it means
            ttk.Label(body, text=title).grid(row=row, column=0, sticky="w", padx=(0, 8))
            box = ttk.Combobox(body, textvariable=var, values=choices, width=44)
            box.grid(row=row, column=1, sticky="ew", pady=(3, 0))
            hint = ttk.Label(body, text="", style="Muted.TLabel")
            hint.grid(row=row + 1, column=1, sticky="w", pady=(0, 4))

            def narrow(*_):
                q = var.get().casefold().strip()
                hits = [c for c in choices if q in c.casefold()]
                box.configure(values=choices if var.get() in by_label else (hits or choices))
                found = resolve(var, choices)
                if found:
                    hint.configure(text="" if var.get() in by_label else f"→ {label(found)}")
                elif not q:
                    hint.configure(text="Type part of a name, or open the list")
                else:
                    hint.configure(text=f"{len(hits)} matches, open the list to pick one" if hits else "No playlist matches")
            var.trace_add("write", narrow)
            narrow()
            return box

        from_box = picker(1, "From", from_var, labels)
        into_box = picker(3, "Into", into_var, editable)

        def swap():
            a, b = from_var.get(), into_var.get()
            if a in editable or not a:
                from_var.set(b)
                into_var.set(a)
        ttk.Button(body, text="⇅", width=3, command=swap).grid(row=1, column=2, rowspan=3, padx=(8, 0))
        first = into_box if source else from_box
        dialog.after(50, lambda: (first.focus_force(), first.icursor("end")) if first.winfo_exists() else None)

        def go():
            src, dst = resolve(from_var, labels), resolve(into_var, editable)
            if not src or not dst:
                self.app.messages.showinfo("Merge", "Pick a playlist in both boxes.", parent=dialog)
                return
            if src["url"] == dst["url"]:
                self.app.messages.showinfo("Merge", "Pick two different playlists.")
                return
            dialog.destroy()
            self.merge(src, dst)

        self.app._buttons(dialog, ("Merge", go, True), ("Cancel", dialog.destroy, False))
        self.app._show_dialog(dialog)

    def _report_failed(self, title, tracks):
        dialog = self.app._dialog(title)
        notes = "\n".join(f"- {t['title']} - {t['artist']}" for t in tracks)
        ttk.Label(dialog, text=f"{len(tracks)} song(s) weren't found or didn't go in:",
                  padding=(16, 12, 16, 4)).pack(anchor="w")
        self.app._buttons(dialog, ("OK", dialog.destroy, True))
        self.app._notes_box(dialog, notes, height=min(14, len(tracks) + 2)).pack(fill="both", expand=True, padx=16)
        self.app._show_dialog(dialog)

    # ------------------------------------------------------------ dragging songs onto playlists

    def _drag_label(self, text, x_root, y_root):
        if not text:
            if self._drag_tip is not None:
                self._drag_tip.destroy()
                self._drag_tip = None
            return
        if self._drag_tip is None:
            self._drag_tip = tip = tk.Toplevel(self)
            tip.wm_overrideredirect(True)
            tip.attributes("-topmost", True)
            p = self.app._colors
            self._drag_text = tk.Label(tip, bg=p.get("select_bg", "#0078d4"), fg=p.get("field_bg", "#ffffff"),
                                       padx=8, pady=3, font=("Segoe UI", 9, "bold"))
            self._drag_text.pack()
        self._drag_text.configure(text=text)
        self._drag_tip.geometry(f"+{x_root + 16}+{y_root + 12}")

    def _clear_drop_target(self):
        if self._drop_target:
            pane, item = self._drop_target
            if pane.tree.exists(item):
                p = pane.items.get(item, {})
                pane.tree.item(item, tags=() if p.get("editable") else ("readonly",))
            self._drop_target = None

    def drag_over(self, source_pane, x_root, y_root, final, shift=False):
        """dragging picked songs: onto a playlist (either side) copies them there, shift moves them.
        onto the other side's songs adds them to its ticked playlist. inside their own list, puts
        them somewhere else in the playlist (spotify)"""
        rows = source_pane.songs.selected_rows()
        n = len(rows)
        action, target, label = None, None, "Drop on a playlist to copy  ·  Shift to move"
        self._clear_drop_target()
        for pane in self.panes.values():
            pane.songs.set_insert(None)
        widget = self.winfo_containing(x_root, y_root)
        for pane in self.panes.values():
            item, p = pane.playlist_at(x_root, y_root)
            if p is not None:
                if p.get("editable") and any(r["playlist"]["url"] != p["url"] for r in rows):
                    action, target = ("move" if shift else "copy"), p
                    pane.tree.item(item, tags=("drop",))
                    self._drop_target = (pane, item)
                    label = f"{'Move' if shift else 'Copy'} {n} to {p['name']}"
                else:
                    label = f"{p['name']} is read only" if not p.get("editable") else "Already in this playlist"
                break
            if widget is pane.songs.canvas:
                if pane is source_pane:
                    if pane.can_reorder():
                        pos = pane.songs.gap_at(y_root)
                        pane.songs.set_insert(pos)
                        action, target = "reorder", pos
                        label = f"Move {n} here"
                    elif pane.platform == "anghami" and len(pane.checked) == 1:
                        label = "Anghami can't reorder songs from here"
                    else:
                        label = "To reorder, tick one playlist and clear the search"
                else:
                    p = pane.single_target()
                    if p is not None:
                        action, target = ("move" if shift else "copy"), p
                        label = f"{'Move' if shift else 'Copy'} {n} to {p['name']}"
                    else:
                        label = "Tick one playlist on this side to drop here"
                break
        if not final:
            self._drag_label(label, x_root, y_root)
            return
        self._drag_label(None, 0, 0)
        self._clear_drop_target()
        for pane in self.panes.values():
            pane.songs.set_insert(None)
        if action in ("copy", "move"):
            self.send(source_pane, target, move=action == "move")
        elif action == "reorder":
            self.reorder(source_pane, target)

    # ------------------------------------------------------------ keyboard

    def paste(self, pane):
        clip = self.clip
        if not clip:
            self.set_status("Nothing copied yet: pick songs and press Ctrl+C (or Ctrl+X to move them).")
            return
        target = pane.single_target()
        if target is None:
            menu = self.targets_menu(pane, lambda p: self._paste_into(clip, p),
                                     exclude={r["playlist"]["url"] for r in clip["rows"]})
            c = pane.songs.canvas
            menu.tk_popup(c.winfo_rootx() + 40, c.winfo_rooty() + 40)
            return
        self._paste_into(clip, target)

    def _paste_into(self, clip, target):
        pane = clip["pane"]
        keep = pane.songs.selected
        pane.songs.selected = {i for i, r in enumerate(pane.songs.rows) if any(r is c for c in clip["rows"])}
        self.send(pane, target, move=clip["move"])
        pane.songs.selected = keep
        if clip["move"]:
            self.clip = None  # a cut pastes once

    def nudge(self, pane, step):
        """alt+up / alt+down: the picked songs one place up or down"""
        if not pane.can_reorder():
            self.set_status("Only a Spotify playlist, alone and without a search, can be reordered here.")
            return
        picked = sorted(pane.songs.view.index(i) for i in pane.songs.selected if i in pane.songs.view)
        if not picked:
            return
        pos = picked[0] - 1 if step < 0 else picked[-1] + 2
        if pos < 0 or pos > len(pane.songs.view):
            return
        self.reorder(pane, pos)

    def reorder(self, pane, pos):
        """moves the picked songs of the one ticked playlist to view position pos, right away on
        screen, then on spotify"""
        songs = pane.songs
        p = pane.single_target()
        picked = [songs.rows[i] for i in sorted(songs.selected)]
        order = [songs.rows[i] for i in songs.view]
        if not picked or p is None:
            return
        before = next((r for r in order[pos:] if r not in picked), None)
        rest = [r for r in order if r not in picked]
        at = rest.index(before) if before is not None else len(rest)
        new_order = rest[:at] + picked + rest[at:]
        if new_order == order:
            return
        tracks = [r["track"] for r in new_order]
        self.songs[p["url"]] = tracks
        songs.set_rows(new_order, keep_selection=True)
        songs.anchor = next(iter(songs.selected), None)
        first = new_order.index(picked[0])
        songs._scroll_to(first)
        self.set_status(f"Moving {len(picked)} song(s) in {p['name']}…")

        def done(result):
            self.set_songs(p, result)
            self.set_status(f"Moved {len(picked)} song(s) in {p['name']}.")

        def failed(e):
            self.show_error(f"Couldn't reorder {p['name']}", e)
            pane.reload(p)

        moved = [r["track"] for r in picked]
        before_track = before["track"] if before is not None else None
        self.workers[p["platform"]].submit(lambda s: manage.move_within(s, p["url"], moved, before_track), done, failed)
