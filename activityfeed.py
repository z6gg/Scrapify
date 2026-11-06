"""
the sync tab's activity feed: each sync as a card per playlist pair with a one line summary and
every song's outcome under it, instead of raw log lines. sync.log keeps the full detail.

newest first. songs a semi-automatic sync wasn't sure about wait in a card pinned at the top
with a yes and a no button. hovering a song's icon says what the icon means
"""

import time
import tkinter as tk
from tkinter import font as tkfont

from matcher import normalize

# icon, what hovering it says, sort order in a card (problems first)
statuses = {
    "unsure": ("◇", "Needs your answer: Scrapify isn't sure this is the same song. Answer at the top of the feed.", 0),
    "error": ("⚠", "Error: something went wrong with this song. It's tried again on the next sync.", 1),
    "missing": ("?", "Possibly not on the other platform: no match was found. The closest song is shown "
                     "if there was one.", 2),
    "version": ("↺", "Different version: only another version (live, remix, another cut) was found, "
                     "and that one was added.", 3),
    "kept": ("↺", "Different version: only another version exists there, so the one already in the "
                  "playlist was kept.", 3),
    "added": ("✓", "Added: the same song was found and added.", 4),
    "removed": ("−", "Removed: it's no longer in the source playlist, so it was taken out.", 5),
}
chip_words = {"added": "added", "version": "other version", "kept": "other version kept", "unsure": "need your answer",
              "missing": "possibly missing", "error": "failed", "removed": "removed"}
dry_chip_words = {"added": "to add", "version": "other version", "kept": "other version kept",
                  "unsure": "would ask", "missing": "possibly missing", "error": "failed", "removed": "to remove"}
rows_shown = 6  # per card until "Show more"
runs_kept = 30

palette = {
    False: {"added": "#0f7b0f", "version": "#005fb8", "kept": "#005fb8", "missing": "#9d5d00", "unsure": "#7a3fd1",
            "error": "#c42b1c", "removed": "#6b6b6b", "link": "#005fb8", "button": "#e9e9e9",
            "button_hover": "#dcdcdc", "card_line": "#e5e5e5", "tip_bg": "#f9f9f9", "tip_border": "#d0d0d0"},
    True: {"added": "#6ccb5f", "version": "#57c8ff", "kept": "#57c8ff", "missing": "#f2c661", "unsure": "#c6a7ff",
           "error": "#ff99a4", "removed": "#9d9d9d", "link": "#57c8ff", "button": "#383838",
           "button_hover": "#454545", "card_line": "#3a3a3a", "tip_bg": "#2b2b2b", "tip_border": "#4a4a4a"},
}


def _clock(seconds):
    seconds = int(round(seconds or 0))
    return f"{seconds // 60}:{seconds % 60:02d}"


def _when(stamp):
    t = time.localtime(stamp)
    if time.strftime("%Y%m%d", t) == time.strftime("%Y%m%d"):
        return time.strftime("%H:%M", t)
    return time.strftime("%b %d, %H:%M", t).replace(" 0", " ")


def _gap(source, match):
    a, b = (source or {}).get("duration"), (match or {}).get("duration")
    if not a or not b or abs(a - b) <= 3:
        return ""
    diff = b - a
    amount = f"{abs(diff):.0f}s" if abs(diff) < 90 else _clock(abs(diff))
    return f" ({amount} {'longer' if diff > 0 else 'shorter'})"


class Tooltip:
    """a small label just above the mouse while it's over something"""

    def __init__(self, master):
        self.master = master
        self.window = None
        self.colors = {"bg": "#2b2b2b", "fg": "#fafafa", "border": "#4a4a4a"}
        self.font = None

    def show(self, text, x_root, y_root):
        self.hide()
        w = self.window = tk.Toplevel(self.master)
        w.overrideredirect(True)
        try:
            w.attributes("-topmost", True)
        except tk.TclError:
            pass
        frame = tk.Frame(w, bg=self.colors["border"], bd=0)
        frame.pack()
        tk.Label(frame, text=text, bg=self.colors["bg"], fg=self.colors["fg"], font=self.font, justify="left",
                 wraplength=320, padx=8, pady=5, bd=0).pack(padx=1, pady=1)
        w.update_idletasks()
        width, height = w.winfo_reqwidth(), w.winfo_reqheight()
        x = max(0, min(x_root - width // 2, w.winfo_screenwidth() - width))
        y = y_root - height - 8
        if y < 0:
            y = y_root + 22
        w.geometry(f"+{x}+{y}")

    def hide(self):
        if self.window is not None:
            self.window.destroy()
            self.window = None


class ActivityFeed(tk.Frame):
    def __init__(self, parent, on_answer=None, on_sync_now=None, load_pending=None):
        super().__init__(parent, bd=0, highlightthickness=0)
        self.on_answer, self.on_sync_now, self.load_pending = on_answer, on_sync_now, load_pending
        self.items = []  # newest first: {"type": "run", ...} or {"type": "note", ...}
        self.pending = []
        self.answered = 0  # answers given since the last sync, so the card can offer to sync now
        self.dark = False
        self.colors = {}
        families = set(tkfont.families(self))
        face = "Segoe UI Variable Text" if "Segoe UI Variable Text" in families else "Segoe UI"
        self.fonts = {"body": tkfont.Font(self, family=face, size=10),
                      "bold": tkfont.Font(self, family=face, size=10, weight="bold"),
                      "small": tkfont.Font(self, family=face, size=9),
                      "icon": tkfont.Font(self, family="Segoe UI Symbol" if "Segoe UI Symbol" in families else face,
                                          size=10, weight="bold"),
                      "big": tkfont.Font(self, family=face, size=11, weight="bold")}
        self.text = tk.Text(self, wrap="word", state="disabled", cursor="arrow", bd=0, relief="flat",
                            highlightthickness=1, padx=14, pady=10, font=self.fonts["body"], takefocus=0)
        self.scrollbar = None
        self.text.pack(side="left", fill="both", expand=True)
        self.tooltip = Tooltip(self)
        self.tooltip.font = self.fonts["small"]
        self._widgets = []  # embedded buttons, rebuilt with every render
        self._configure_tags()
        self.text.bind("<MouseWheel>", lambda e: self.tooltip.hide(), add="+")
        self.text.bind("<Leave>", lambda e: self.tooltip.hide(), add="+")

    def attach_scrollbar(self, scrollbar):
        self.scrollbar = scrollbar
        self.text.configure(yscrollcommand=scrollbar.set)
        scrollbar.configure(command=self.text.yview)

    # ------------------------------------------------------------- look

    def set_colors(self, dark, bg, fg, muted, border, select_bg):
        self.dark = dark
        self.colors = {"bg": bg, "fg": fg, "muted": muted, "border": border, **palette[dark]}
        self.configure(bg=bg)
        self.text.configure(bg=bg, fg=fg, insertbackground=fg, highlightbackground=border,
                            highlightcolor=border, selectbackground=select_bg, inactiveselectbackground=select_bg)
        self.tooltip.colors = {"bg": palette[dark]["tip_bg"], "fg": fg, "border": palette[dark]["tip_border"]}
        self._configure_tags()
        self.render()

    def _configure_tags(self):
        c, f, t = self.colors, self.fonts, self.text
        fg, muted = c.get("fg", "#000"), c.get("muted", "#666")
        t.tag_configure("muted", foreground=muted, font=f["small"])
        t.tag_configure("note", foreground=muted, font=f["small"], lmargin1=0, lmargin2=44, spacing1=4)
        t.tag_configure("note_warn", foreground=c.get("error", "#c42b1c"), font=f["small"], lmargin2=44, spacing1=4)
        t.tag_configure("run", font=f["small"], foreground=muted, spacing1=16, spacing3=2)
        t.tag_configure("run_first", spacing1=2)
        t.tag_configure("pair", font=f["bold"], foreground=fg, spacing1=8)
        t.tag_configure("dir", font=f["small"], foreground=muted)
        t.tag_configure("summary", font=f["small"], foreground=muted, spacing1=1, spacing3=4)
        t.tag_configure("row", lmargin1=6, lmargin2=28, spacing1=3)
        t.tag_configure("sub", lmargin1=28, lmargin2=28, font=f["small"], foreground=muted, spacing1=0)
        t.tag_configure("title", foreground=fg)
        t.tag_configure("artist", foreground=muted)
        t.tag_configure("sep", foreground=muted)
        t.tag_configure("mtitle", foreground=fg, font=f["small"])
        t.tag_configure("martist", foreground=muted, font=f["small"])
        t.tag_configure("link", foreground=c.get("link", "#005fb8"), font=f["small"], lmargin1=28, spacing1=3)
        t.tag_configure("error_line", foreground=c.get("error", "#c42b1c"), font=f["small"], lmargin1=6,
                        lmargin2=28, spacing1=3)
        t.tag_configure("pending_head", font=f["big"], foreground=fg, spacing1=2)
        t.tag_configure("pending_hint", font=f["small"], foreground=muted, spacing3=4)
        t.tag_configure("empty", foreground=muted, font=f["body"], spacing1=8)
        t.tag_configure("buttons", lmargin1=28, spacing1=4, spacing3=4)
        t.tag_configure("rule", font=("Segoe UI", 2), spacing1=10)
        for status in statuses:
            t.tag_configure(f"icon_{status}", foreground=c.get(status, fg), font=f["icon"])
            t.tag_configure(f"chip_{status}", foreground=c.get(status, fg), font=f["small"])
            t.tag_bind(f"icon_{status}", "<Enter>", lambda e, s=status: self._show_tip(e, statuses[s][1]))
            t.tag_bind(f"icon_{status}", "<Leave>", lambda e: self.tooltip.hide())
        t.tag_bind("link", "<Enter>", lambda e: (t.configure(cursor="hand2"),
                                                 t.tag_configure("link", underline=True)))
        t.tag_bind("link", "<Leave>", lambda e: (t.configure(cursor="arrow"),
                                                 t.tag_configure("link", underline=False)))
        t.tag_raise("sel")

    def _show_tip(self, event, text):
        # above the icon itself, not the mouse: centered on it so it reads as its label
        index = self.text.index(f"@{event.x},{event.y}")
        box = self.text.bbox(index)
        if box:
            x = self.text.winfo_rootx() + box[0] + box[2] // 2
            y = self.text.winfo_rooty() + box[1]
        else:
            x, y = event.x_root, event.y_root
        self.tooltip.show(text, x, y)

    # ------------------------------------------------------------- events

    def add_event(self, event, render=True):
        kind = event.get("kind")
        stamp = event.get("time") or time.time()
        run = self.items[0] if self.items and self.items[0]["type"] == "run" and not self.items[0]["done"] else None
        if kind == "run_start":
            self.items.insert(0, {"type": "run", "time": stamp, "dry_run": event.get("dry_run"), "pairs": [],
                                  "notes": [], "done": False})
        elif kind == "pair_start":
            if run is None:
                run = {"type": "run", "time": stamp, "dry_run": event.get("dry_run"), "pairs": [], "notes": [],
                       "done": False}
                self.items.insert(0, run)
            run["pairs"].append({"name": event.get("pair", ""), "source": event.get("source", ""),
                                 "dest": event.get("dest", ""), "time": stamp, "songs": None, "error": None,
                                 "notes": [], "expanded": False, "done": False})
        elif kind in ("pair_end", "pair_note"):
            pair = self._open_pair(run, event.get("pair"))
            if pair is None:
                return
            if kind == "pair_note":
                pair["notes"].append(event.get("note", ""))
            else:
                pair.update(songs=event.get("songs"), error=event.get("error"), total=event.get("total"), done=True)
        elif kind == "run_end":
            if run is not None:
                run["done"] = True
                if not run["pairs"] and not run["notes"]:
                    self.items.remove(run)  # nothing happened worth a card (busy, no pairs: said in a note)
        del self.items[runs_kept:]
        if render:
            self.render()

    @staticmethod
    def _open_pair(run, name):
        if run is None:
            return None
        for pair in reversed(run["pairs"]):
            if pair["name"] == name and not pair["done"]:
                return pair
        return None

    def add_note(self, text, warning=False, render=True):
        run = self.items[0] if self.items and self.items[0]["type"] == "run" and not self.items[0]["done"] else None
        note = {"time": time.time(), "text": text, "warning": warning}
        if run is not None:
            run["notes"].append(note)
        else:
            self.items.insert(0, {"type": "note", **note})
            del self.items[runs_kept:]
        if render:
            self.render()

    def set_pending(self, pending, answered=0):
        self.pending, self.answered = list(pending or []), answered
        self.render()

    def clear(self):
        self.items = [item for item in self.items if item["type"] == "run" and not item["done"]]
        self.render()

    # ------------------------------------------------------------- drawing

    def render(self):
        t = self.text
        top = t.yview()[0]
        self.tooltip.hide()
        t.configure(state="normal")
        t.delete("1.0", "end")
        for widget in self._widgets:
            widget.destroy()
        self._widgets = []
        first = True
        if self.pending or self.answered:
            self._render_pending()
            first = False
        if not self.items and not self.pending and not self.answered:
            t.insert("end", "Nothing yet. Sync results show up here, newest first.", ("empty",))
        for item in self.items:
            if item["type"] == "note":
                self._render_note(item, standalone=True, first=first)
            else:
                self._render_run(item, first)
            first = False
        t.configure(state="disabled")
        t.yview_moveto(top)

    def _insert(self, text, *tags):
        self.text.insert("end", text, tags)

    def _render_note(self, note, standalone=False, first=False):
        tags = ("note_warn" if note.get("warning") else "note",) + (("run_first",) if first else ())
        if standalone:
            self._insert("\n" if not first else "", *tags)
            self._insert(f"{_when(note['time'])}   ", *tags)
        else:
            self._insert("\n", *tags)
        self._insert(("⚠ " if note.get("warning") else "") + note["text"], *tags)

    def _render_run(self, run, first):
        label = "Preview, nothing was changed" if run.get("dry_run") else "Sync"
        if not run["done"]:
            label += " · running"
        if not first:
            self._insert("\n")
        self._insert(f"{_when(run['time'])}   {label}", "run", *(("run_first",) if first else ()))
        for note in run["notes"]:
            self._render_note(note)
        for pair in run["pairs"]:
            self._render_pair(pair, run.get("dry_run"))

    def _render_pair(self, pair, dry_run):
        self._insert("\n")
        self._insert(pair["name"] or "Playlist pair", "pair")
        if pair["source"]:
            self._insert(f"   {pair['source']} → {pair['dest']}", "dir")
        for note in pair["notes"]:
            self._insert("\n")
            self._insert(note, "summary")
        if pair.get("error"):
            self._insert("\n")
            self._insert(f"⚠  {pair['error']}", "error_line")
            return
        if not pair["done"]:
            self._insert("\n")
            self._insert("Checking songs…", "summary")
            return
        songs = pair.get("songs") or []
        self._insert("\n")
        if not songs:
            total = pair.get("total")
            self._insert(f"Already in sync{f', {total} songs' if total else ''}. Nothing to change.", "summary")
            return
        counts = {}
        for song in songs:
            counts[song["status"]] = counts.get(song["status"], 0) + 1
        words = dry_chip_words if dry_run else chip_words
        chips = [s for s in ("added", "version", "kept", "removed", "unsure", "missing", "error") if s in counts]
        for i, status in enumerate(chips):
            if i:
                self._insert("  ·  ", "summary")
            self._insert(f"{counts[status]} {words[status]}", "summary", f"chip_{status}")
        ordered = sorted(songs, key=lambda s: statuses[s["status"]][2])
        shown = ordered if pair["expanded"] else ordered[:rows_shown]
        for song in shown:
            self._render_song(song, pair, dry_run)
        hidden = len(ordered) - len(shown)
        if hidden or (pair["expanded"] and len(ordered) > rows_shown):
            self._insert("\n")
            tag = f"fold_{id(pair)}"
            self._insert(f"Show {hidden} more" if hidden else "Show less", "link", tag)
            self.text.tag_bind(tag, "<Button-1>", lambda e, p=pair: self._toggle(p))

    def _toggle(self, pair):
        pair["expanded"] = not pair["expanded"]
        self.render()

    def _names(self, track, title_tag, artist_tag, extra=()):
        # title and artist in tags of their own so tk draws them as separate runs: an arabic name then
        # comes out right to left in place, and the words around it stay in order
        self._insert(track.get("title") or "?", title_tag, *extra)
        if track.get("artist"):
            self._insert(" - ", "sep", *extra)
            self._insert(track["artist"], artist_tag, *extra)

    def _render_song(self, song, pair, dry_run):
        status, match = song["status"], song.get("match")
        self._insert("\n", "row")
        self._insert(statuses[status][0], "row", f"icon_{status}")
        self._insert("  ", "row")
        self._names(song, "title", "artist", ("row",))
        dest = pair.get("dest") or "the other platform"
        note = song.get("note")
        sub = []
        if status == "added":
            if match and (normalize(match.get("title", "")) != normalize(song["title"])
                          or normalize(match.get("artist", "")) != normalize(song["artist"])):
                sub = [("→ ", None), match]
            if note:
                sub += [("  ·  " if sub else "", None), (note, None)]
        elif status in ("version", "kept"):
            sub = [("Only a different version: " if status == "version" else "Kept the other version: ", None)]
            sub += [match] if match else []
            sub += [(_gap(song, match), None)]
        elif status == "missing":
            sub = [(f"Possibly not on {dest}", None)]
            if match:
                sub += [("  ·  closest: ", None), match, (_gap(song, match), None)]
        elif status == "unsure":
            sub = [("Is it ", None)] + ([match] if match else []) + [(_gap(song, match), None),
                                                                    ("?  Answer at the top.", None)]
        elif status == "error":
            sub = [(note or "Something went wrong", "error")]
        elif status == "removed":
            sub = [(f"{'Would be removed' if dry_run else 'Removed'} from {dest}", None)]
        if not sub:
            return
        self._insert("\n", "sub")
        for part in sub:
            if isinstance(part, dict):
                self._names(part, "mtitle", "martist", ("sub",))
            else:
                text, kind = part
                if text:
                    self._insert(text, "sub", *(("chip_error",) if kind == "error" else ()))

    def _render_pending(self):
        t = self.text
        n = len(self.pending)
        if n:
            self._insert(f"Needs your answer  ·  {n}", "pending_head", "run_first")
            self._insert("\n")
            self._insert("Scrapify wasn't sure these are the same song. Your answer is remembered and used on "
                         "every sync from now on.", "pending_hint")
        else:
            self._insert("All answered", "pending_head", "run_first")
            self._insert("\n")
            self._insert(f"{self.answered} answer{'s' if self.answered != 1 else ''} saved. They're used on the "
                         "next sync.", "pending_hint")
        for item in self.pending:
            source, candidate = item.get("source") or {}, item.get("candidate") or {}
            self._insert("\n", "row")
            self._insert(statuses["unsure"][0], "row", "icon_unsure")
            self._insert("  ", "row")
            self._names(source, "title", "artist", ("row",))
            if source.get("duration"):
                self._insert(f"  {_clock(source['duration'])}", "row", "muted")
            self._insert("\n", "sub")
            self._insert(f"{item.get('service', '')} has: ", "sub")
            self._names(candidate, "mtitle", "martist", ("sub",))
            if candidate.get("duration"):
                self._insert(f"  {_clock(candidate['duration'])}", "sub")
            if item.get("pair"):
                self._insert(f"   ·   {item['pair']}", "sub")
            self._insert("\n", "buttons")
            for label, same in (("✓  Same song", True), ("✗  Not the same", False)):
                button = self._button(label, lambda i=item["id"], s=same: self.on_answer and self.on_answer(i, s))
                t.window_create("end", window=button, padx=0, pady=2)
                self._insert("  ", "buttons")
        if self.on_sync_now and self.answered:
            self._insert("\n")
            if n:
                self._insert(f"{self.answered} answered so far.  ", "sub")
            self._insert("Sync now", "link", "sync_now")
            t.tag_bind("sync_now", "<Button-1>", lambda e: self.on_sync_now())

    def _button(self, label, command):
        c = self.colors
        button = tk.Label(self.text, text=label, font=self.fonts["small"], bg=c.get("button", "#e9e9e9"),
                          fg=c.get("fg", "#000"), padx=10, pady=3, cursor="hand2", bd=0)
        button.bind("<Enter>", lambda e: button.configure(bg=c.get("button_hover", "#ddd")))
        button.bind("<Leave>", lambda e: button.configure(bg=c.get("button", "#e9e9e9")))
        button.bind("<ButtonRelease-1>", lambda e: command())
        self._widgets.append(button)
        return button
