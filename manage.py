"""
the manage tab's background work. one worker thread per service keeps that service's browser open
between clicks (opening one costs seconds, a call on an open one well under a second) and closes it
after a while of nothing to do. what's in the playlists is the services' own; nothing here is saved
"""

import logging
import queue
import threading

from playwright.sync_api import sync_playwright

import main as sync_main
from browsersessioncommon import AuthError
from matcher import match_pairs, track_key

log = logging.getLogger("scrapify.manage")
idle_close_seconds = 120


def platform_of(track):
    # a song read from spotify carries its uri, one from anghami its id
    return "spotify" if str(track.get("uri", "")).startswith("spotify:") else "anghami"


def _own_key(track):
    return track.get("uri") if platform_of(track) == "spotify" else track.get("id")


def missing_from(source, dest, dest_platform, threshold):
    """the songs of source that dest doesn't have yet, each once. songs from dest's own service
    compare by id, then by name; from the other service the matcher decides, like a sync does"""
    seen, unique = set(), []
    for t in source:
        k = _own_key(t) or track_key(t["title"], t["artist"])
        if k not in seen:
            seen.add(k)
            unique.append(t)
    if not dest:
        return unique
    same = [t for t in unique if platform_of(t) == dest_platform]
    other = [t for t in unique if platform_of(t) != dest_platform]
    left = []
    if same:
        have_ids = {_own_key(d) for d in dest}
        have_names = {track_key(d["title"], d["artist"]) for d in dest}
        left += [t for t in same if _own_key(t) not in have_ids and track_key(t["title"], t["artist"]) not in have_names]
    if other:
        _pairs, other_left, _dest_left = match_pairs(other, dest, threshold)
        left += other_left
    order = {id(t): i for i, t in enumerate(unique)}
    return sorted(left, key=lambda t: order[id(t)])


def duplicates(tracks):
    """indexes of every later copy of a song already earlier in the list"""
    seen, out = set(), []
    for i, t in enumerate(tracks):
        keys = {track_key(t["title"], t["artist"])}
        if _own_key(t):
            keys.add(_own_key(t))
        if keys & seen:
            out.append(i)
        seen |= keys
    return out


class Session:
    """what a task gets: the service's client, opened the first time it's asked for"""

    def __init__(self, worker):
        self._worker = worker

    def client(self):
        return self._worker._open_client()

    @property
    def threshold(self):
        return int(self._worker.get_config().get("match_threshold", 87))


class Worker:
    def __init__(self, platform, get_config, begin, end, post):
        """begin() -> bool claims scrapify for a task (False while a sync runs), end() frees it.
        post(fn, *args) runs fn on the ui thread"""
        self.platform = platform
        self.get_config, self.begin, self.end, self.post = get_config, begin, end, post
        self._tasks = queue.Queue()
        self._lock = threading.Lock()
        self._thread = None
        self._pw = self._client = None
        self.busy = False

    def submit(self, fn, done=None, failed=None):
        """fn(session) runs on this service's thread; done(result) or failed(error) on the ui's"""
        self._tasks.put((fn, done, failed))
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, name=f"manage-{self.platform}", daemon=True)
                self._thread.start()

    def stop(self):
        self._tasks.put(None)

    def _open_client(self):
        if self._client is None:
            if self._pw is None:
                self._pw = sync_playwright().start()
            client = sync_main.make_client(self.platform, self._pw, self.get_config())
            client.__enter__()
            self._client = client
        return self._client

    def _close_client(self):
        client, self._client = self._client, None
        if client is not None:
            try:
                client.__exit__(None, None, None)
            except Exception as e:
                log.debug("closing the %s browser: %s", self.platform, e)

    def _run(self):
        try:
            while True:
                try:
                    item = self._tasks.get(timeout=idle_close_seconds)
                except queue.Empty:
                    with self._lock:
                        if self._tasks.empty():
                            self._thread = None
                            return
                    continue
                if item is None:
                    return
                fn, done, failed = item
                if not self.begin():
                    if failed:
                        self.post(failed, RuntimeError("A sync is running. Try again when it finishes."))
                    continue
                self.busy = True
                try:
                    result = fn(Session(self))
                except Exception as e:
                    log.info("%s task failed: %s", self.platform, e)
                    if isinstance(e, AuthError):
                        self._close_client()  # a fresh session file is read on the next try
                    if failed:
                        self.post(failed, e)
                else:
                    if done:
                        self.post(done, result)
                finally:
                    self.busy = False
                    self.end()
        finally:
            self._close_client()
            if self._pw is not None:
                try:
                    self._pw.stop()
                except Exception:
                    pass
                self._pw = None


# ------------------------------------------------------------------ tasks
# each takes the session and runs on the destination service's thread

def add_songs(session, dest_url, tracks):
    """adds the songs dest doesn't have yet. songs from the same service go in by id, the others
    are searched for and matched. returns {added, already, failed: [tracks]}"""
    client = session.client()
    current = client.get_playlist_tracks(dest_url)
    platform = session._worker.platform
    todo = missing_from(tracks, current, platform, session.threshold)
    already = len(tracks) - len(todo)
    same = [t for t in todo if platform_of(t) == platform]
    other = [t for t in todo if platform_of(t) != platform]
    failed = []
    if same:
        failed += client.add_exact(dest_url, same)
    if other:
        failed += client.add_tracks(dest_url, other, session.threshold)
    return {"added": len(todo) - len(failed), "already": already, "failed": failed,
            "tracks": client.get_playlist_tracks(dest_url)}


def remove_songs(session, url, tracks):
    client = session.client()
    before = client.get_playlist_tracks(url)
    still = client.remove_tracks(url, tracks, session.threshold)
    after = client.get_playlist_tracks(url)
    if session._worker.platform == "anghami":
        # anghami deletes a song by its id, so every copy of it goes. a copy that wasn't picked goes back in
        picked = {}
        for t in tracks:
            picked[t.get("id")] = picked.get(t.get("id"), 0) + 1
        kept = [t for t in before if picked.get(t["id"], 0) < sum(1 for b in before if b["id"] == t["id"])]
        have = {t["id"] for t in after}
        lost = list({t["id"]: t for t in kept if t["id"] not in have}.values())
        if lost:
            client.add_exact(url, lost)
            after = client.get_playlist_tracks(url)
    return {"removed": len(tracks) - len(still), "failed": still, "tracks": after}


def move_within(session, url, tracks, before=None):
    """puts tracks just before the song before (the end without one)"""
    key = "uid" if session._worker.platform == "spotify" else "id"
    return session.client().move_tracks(url, [t[key] for t in tracks if t.get(key)],
                                        before.get(key) if before else None)


def create_playlist(session, name, tracks=()):
    """a new playlist, with tracks in it when given (copies of another one's songs). returns its
    entry for the playlist list"""
    client = session.client()
    url = client.create_playlist(name)
    if tracks:
        add_songs(session, url, list(tracks))
    found = next((p for p in client.list_playlists() if p["url"].rstrip("/") == url.rstrip("/")), None)
    return found or {"platform": session._worker.platform, "id": url.rsplit("/", 1)[-1], "name": name,
                     "url": url, "count": len(tracks), "image": "", "editable": True}
