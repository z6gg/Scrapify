"""
a song's link pasted in by the user, for a song scrapify can't find itself: what song it is. read
from the song's public page, no login or browser: spotify's page names the song, the artist and its
length, anghami's share links (open.anghami.com) lead to a page with the song's id
"""

import html
import re
import urllib.request

_spotify_track = re.compile(r"(?:open\.spotify\.com/(?:intl-[\w-]+/)?track/|spotify:track:)([A-Za-z0-9]{22})")
_anghami_song = re.compile(r"(?:play|open)\.anghami\.com/song/(\d+)")
_anghami_share = re.compile(r"https?://open\.anghami\.com/[A-Za-z0-9]+/?(?:[?#].*)?$")
# a plain one: spotify answers a full browser's with the bare player, no song details in it
_headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
_direction_marks = dict.fromkeys(map(ord, "\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069\u061c"))  # invisible, they only steer reading direction
examples = {"Spotify": "https://open.spotify.com/track/<id>", "Anghami": "https://play.anghami.com/song/<id>"}


def looks_like(text, service):
    """whether text is a song link for service ("Spotify" or "Anghami"), before any lookup"""
    text = (text or "").strip()
    if service == "Spotify":
        return bool(_spotify_track.search(text))
    return bool(_anghami_song.search(text) or _anghami_share.match(text))


def _get(url, timeout=15):
    with urllib.request.urlopen(urllib.request.Request(url, headers=_headers), timeout=timeout) as resp:
        return resp.read(2_000_000).decode("utf-8", "replace")


def _meta(page, name):
    # anghami leaves the quotes off
    m = re.search(r"""<meta[^>]*?(?:property|name)=["']?%s["']?[^>]*?content=(?:"([^"]*)"|'([^']*)'|([^\s>]+))"""
                  % re.escape(name), page)
    if not m:
        return ""
    return html.unescape(next(g for g in m.groups() if g is not None)).translate(_direction_marks).strip()


def _read_song_page(song, page):
    # og:description is "Abadi Al Johar · Al Jarh Arham · Song · 2007": the artists, then the album
    # (spotify) or the kind (anghami)
    song["title"] = _meta(page, "og:title")
    song["artist"] = _meta(page, "og:description").split(" · ")[0].strip()
    seconds = _meta(page, "music:duration")
    if seconds.isdigit():
        song["duration"] = int(seconds)


def resolve(text, service, fetch=_get):
    """{title, artist, duration, uri or id} for a song link on service. raises ValueError when it
    isn't one. the names are best effort: a page that can't be read still gives the song's id"""
    text = (text or "").strip()
    if service == "Spotify":
        m = _spotify_track.search(text)
        if not m:
            raise ValueError(f"That isn't a Spotify song link.\n\nExpected something like:\n{examples['Spotify']}")
        song = {"uri": f"spotify:track:{m.group(1)}", "title": "", "artist": ""}
        try:
            _read_song_page(song, fetch(f"https://open.spotify.com/track/{m.group(1)}"))
        except Exception:
            pass
        return song
    m = _anghami_song.search(text)
    if not m and _anghami_share.match(text):
        try:
            m = _anghami_song.search(fetch(text))
        except Exception as e:
            raise ValueError(f"Couldn't open that Anghami link: {e}") from e
        if not m:
            raise ValueError("That Anghami link isn't a song's link.")
    if not m:
        raise ValueError(f"That isn't an Anghami song link.\n\nExpected something like:\n{examples['Anghami']}")
    song = {"id": m.group(1), "title": "", "artist": ""}
    try:
        _read_song_page(song, fetch(f"https://play.anghami.com/song/{m.group(1)}"))
    except Exception:
        pass
    return song
