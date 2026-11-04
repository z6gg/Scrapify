"""
anghami client. calls the web player's own json gateway from inside a logged in player page,
reading the page itself is the fallback for playlists
"""

import base64
import json
import re
import struct
import urllib.parse

import browsersessioncommon as bsc
from matcher import best_match, find_many, has_arabic, is_same_artist, match_score, normalize
from browsersessioncommon import AuthError, PlaylistNotFoundError

playlist_id_cache = "anghami_playlist_ids.json"
selectors = {"track_row": "a.table-row", "track_title": ".cell-title span", "track_artist": ".cell-artist a"}
_search_path = "/rest/v2/GETSearchResults.view"
_artist_pages = 5  # pages of 20 songs read from an artist page, most played first


def _seconds(value):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0


def _with_aliases(track, keywords):
    # anghami sends keywords with every song like ['Tamally Maak', 'تملى معاك', 'Amr Diab', 'عمرو دياب']:
    # titles, the artist, the artist's other name, sometimes album names. some put the arabic artist
    # first, so anything that reads as the artist never counts as a title or they'd all match
    kws = list(dict.fromkeys(k.strip() for k in keywords or [] if isinstance(k, str) and k.strip()))
    if not kws:
        return track
    artist_key = normalize(track["artist"])
    split = next((i for i, k in enumerate(kws) if normalize(k) == artist_key), None)
    if split is None:
        return track
    other_script = lambda k, name: has_arabic(k) != has_arabic(name)
    is_artist = lambda k: is_same_artist(k, track["artist"])
    after = kws[split + 1:split + 2]
    titles = [k for k in kws[:split] if other_script(k, track["title"]) and not is_artist(k)]
    artists = [k for k in kws[:split] + after if other_script(k, track["artist"]) and (k in after or is_artist(k))]
    if titles or artists:
        track["aliases"] = {"titles": titles, "artists": artists}
    return track


def _song(item):
    return _with_aliases({"title": item.get("title", ""), "artist": item.get("artist", ""),
                          "id": str(item["id"]), "duration": _seconds(item.get("duration"))}, item.get("keywords"))


class AnghamiClient(bsc.BrowserClient):
    session_file = "anghami_session.json"
    block_assets = False  # if its images fail the player thinks its cdn is down and reloads

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._playlist_ids = {}
        self._artist_songs_cache = {}
        self._api_page = self._api_base = self._api_params = None

    def _diagnose_failed_load(self, page, response, playlist_url):
        # AuthError or PlaylistNotFoundError when the page says which, else None
        url = page.url or ""
        try:
            body = page.inner_text("body").lower()
        except Exception:
            body = ""
        try:
            title = (page.title() or "").strip().lower()
        except Exception:
            title = ""
        if title == "client challenge" or "enter the characters seen in the image" in body:
            return AuthError("Anghami showed a bot check captcha instead of the playlist. Reinstall Chromium "
                             "from the Setup tab, or turn on Show browser window while syncing in Settings.")
        if ("do not have permission to access this playlist" in body and "login | signup" in body) \
                or "/login" in url or "signin" in url \
                or ("playlist" not in url and any(p in body for p in ("log in to anghami", "sign in to anghami"))):
            return AuthError("Anghami session is logged out or expired. Log in again with Get Sessions on the Setup tab.")
        if (response and response.status == 404) or any(
                p in body for p in ("page not found", "playlist not found", "this playlist is not available",
                                    "content unavailable")):
            return PlaylistNotFoundError(f"Anghami playlist link looks invalid or the playlist is gone: {playlist_url}")
        return None

    # ------------------------------------------------------------ playlist ids

    def _known_playlist_id(self, playlist_url):
        # share links (open.anghami.com) go through a link service that sometimes fails, so the id
        # one redirected to is remembered
        m = re.search(r"play\.anghami\.com/playlist/(\d+)", playlist_url)
        if m:
            return m.group(1)
        if playlist_url not in self._playlist_ids:
            try:
                with open(playlist_id_cache, encoding="utf-8") as f:
                    self._playlist_ids.update(json.load(f))
            except (OSError, ValueError):
                pass
        return self._playlist_ids.get(playlist_url)

    def _remember_playlist_id(self, playlist_url, playlist_id):
        self._playlist_ids[playlist_url] = playlist_id
        try:
            try:
                with open(playlist_id_cache, encoding="utf-8") as f:
                    saved = json.load(f)
            except (OSError, ValueError):
                saved = {}
            saved[playlist_url] = playlist_id
            with open(playlist_id_cache, "w", encoding="utf-8") as f:
                json.dump(saved, f, indent=2)
        except OSError as e:
            self.log.debug("Couldn't save the Anghami playlist id cache: %s", e)

    # ----------------------------------------------------------------- gateway

    _api_param_keys = {"sid", "appsid", "fingerprint", "lang", "language", "output", "web2", "userlanguageprod"}

    def _open_api(self, playlist_url=None):
        # loads a player page (the playlist when given) and takes the gateway address and session
        # params from the player's own requests. returns the playlist's numeric id
        is_gateway = lambda req: "/gateway.php" in req.url and "sid=" in req.url
        if self._api_page is None:
            self._api_page = self._context.new_page()
        page = self._api_page
        captured = []
        handler = lambda req: captured.append(req.url) if is_gateway(req) else None
        need_params = self._api_params is None
        if need_params:
            page.on("request", handler)
        try:
            known_id = self._known_playlist_id(playlist_url) if playlist_url else None
            if known_id and not need_params:
                return known_id
            target = f"https://play.anghami.com/playlist/{known_id}" if known_id else playlist_url or "https://play.anghami.com/"
            response = page.goto(target, wait_until="domcontentloaded")
            if response and response.status == 404 and "open.anghami.com" in page.url:
                page.wait_for_timeout(1500)  # the share link service sometimes 404s a good link
                response = page.goto(target, wait_until="domcontentloaded")
            playlist_id = None
            if playlist_url:
                try:
                    page.wait_for_url(re.compile(r"/playlist/\d+"), wait_until="commit", timeout=self._t(15000))
                    playlist_id = re.search(r"/playlist/(\d+)", page.url).group(1)
                except Exception:
                    raise self._diagnose_failed_load(page, response, playlist_url) or RuntimeError(
                        f"Anghami link didn't open a playlist (ended on {page.url})")
                self._remember_playlist_id(playlist_url, playlist_id)
            if need_params:
                if not captured:
                    try:
                        page.wait_for_event("request", is_gateway, timeout=self._t(15000))
                    except Exception:
                        pass
                if not captured:
                    raise self._diagnose_failed_load(page, response, target) or RuntimeError(
                        "the web player never called its gateway")
                parts = urllib.parse.urlparse(captured[0])
                self._api_base = f"{parts.scheme}://{parts.netloc}"
                self._api_params = {k: v for k, v in urllib.parse.parse_qsl(parts.query) if k in self._api_param_keys}
            return playlist_id
        finally:
            if need_params:
                page.remove_listener("request", handler)

    def _gateway_url(self, params, path="/gateway.php"):
        return f"{self._api_base}{path}?{urllib.parse.urlencode({**self._api_params, **params})}"

    def _gateway(self, params, post=False, path="/gateway.php"):
        # from inside the player page so it carries its cookies and origin, from outside it's HTTP 406
        if post:
            request = (self._gateway_url({}, path), {
                "credentials": "include", "method": "POST", "body": urllib.parse.urlencode(params),
                "headers": {"content-type": "application/x-www-form-urlencoded;charset=UTF-8"}})
        else:
            request = (self._gateway_url(params, path), {"credentials": "include"})
        status, text = bsc.fetch_many(self._api_page, [request])[0]
        if status != 200:
            raise RuntimeError(f"{params.get('type', path)} returned HTTP {status}")
        data = json.loads(text)
        if isinstance(data, dict) and data.get("error"):
            err = data["error"]
            raise RuntimeError(f"{params.get('type', path)} failed: {err.get('message', err) if isinstance(err, dict) else err}")
        return data

    def _gateway_many(self, calls):
        """several GET calls [(params, path)] side by side, None for any that failed"""
        raw = bsc.fetch_many(self._api_page, [(self._gateway_url(params, path), {"credentials": "include"})
                                              for params, path in calls])
        results = []
        for (params, path), (status, text) in zip(calls, raw):
            try:
                data = json.loads(text) if status == 200 else None
                if isinstance(data, dict) and data.get("error"):
                    data = None
            except ValueError:
                data = None
            if data is None:
                self.log.debug("Anghami %s failed (HTTP %s)", params.get("type", path), status)
            results.append(data)
        return results

    @staticmethod
    def _protobuf_fields(buf):
        # {field number: [values]}, just enough protobuf for songbuffers
        fields, i = {}, 0

        def varint():
            nonlocal i
            shift = result = 0
            while True:
                b = buf[i]
                i += 1
                result |= (b & 0x7F) << shift
                if not b & 0x80:
                    return result
                shift += 7

        while i < len(buf):
            key = varint()
            number, wire = key >> 3, key & 7
            if wire == 0:
                value = varint()
            elif wire == 2:
                size = varint()
                value, i = buf[i:i + size], i + size
            elif wire == 1:
                value, i = buf[i:i + 8], i + 8
            elif wire == 5:
                value, i = buf[i:i + 4], i + 4
            else:
                raise ValueError(f"unsupported protobuf wire type {wire}")
            fields.setdefault(number, []).append(value)
        return fields

    def _decode_song_buffers(self, buffers):
        # an entry is {1: id, 2: song}, a song {1: id, 2: title, 3: album, 5: artist, 9: duration
        # float32, 14: keywords}. a reissued song has two different ids and songorder uses the
        # entry's, so both are indexed
        songs = {}
        for blob in buffers or []:
            for entry_raw in self._protobuf_fields(base64.b64decode(blob)).get(2, []):
                entry = self._protobuf_fields(entry_raw)
                if 2 not in entry:
                    continue
                song = self._protobuf_fields(entry[2][0])
                text = lambda fields, n: fields[n][0].decode("utf-8", "replace").strip() if n in fields else ""
                length = song.get(9, [None])[0]
                info = {"title": text(song, 2), "artist": text(song, 5),
                        "duration": struct.unpack("<f", length)[0] if isinstance(length, bytes) and len(length) == 4 else 0,
                        "keywords": [k.decode("utf-8", "replace") for k in song.get(14, []) if isinstance(k, bytes)]}
                for song_id in (text(song, 1), text(entry, 1)):
                    if song_id:
                        songs[song_id] = info
        return songs

    def _read_playlist_api(self, playlist_id):
        # songorder keeps the recommended section out. buffered=1 like the player, the plain
        # response can lag behind a fresh add
        data = self._gateway({"type": "GETplaylistdata", "playlistid": playlist_id, "buffered": "1"})
        order = [x for x in str(data.get("songorder") or "").split(",") if x]
        songs = self._decode_song_buffers(data.get("songbuffers"))
        for section in data.get("sections") or []:
            if section.get("type") == "song":
                for song in section.get("data") or []:
                    songs.setdefault(str(song.get("id")), song)
        tracks = [_with_aliases({"title": songs[i].get("title", "").strip(), "artist": songs[i].get("artist", "").strip(),
                                 "id": i, "duration": _seconds(songs[i].get("duration"))}, songs[i].get("keywords"))
                  for i in order if i in songs]
        if len(tracks) < len(order):
            # a partial list would read as songs deleted
            raise RuntimeError(f"playlist lists {len(order)} songs but only {len(tracks)} came back")
        return tracks

    # ---------------------------------------------------------------- reading

    def get_playlist_tracks(self, playlist_url):
        try:
            return self._read_playlist_api(self._open_api(playlist_url))
        except (AuthError, PlaylistNotFoundError):
            raise
        except Exception as e:
            self.log.info("Anghami API read failed (%s), reading the page instead.", e)
            return self._get_playlist_tracks_dom(playlist_url)

    def _get_playlist_tracks_dom(self, playlist_url):
        page = self._context.new_page()
        known = self._known_playlist_id(playlist_url)
        response = page.goto(f"https://play.anghami.com/playlist/{known}" if known else playlist_url,
                             wait_until="domcontentloaded")
        try:
            page.wait_for_selector(selectors["track_row"], timeout=self._t(15000))
        except Exception as e:
            error = self._diagnose_failed_load(page, response, playlist_url)
            self._dump(page, "debug_anghami_playlist_load_failed")
            page.close()
            raise (error or e) from e

        # lazy list: scroll the last row into view (the mouse wheel hits wherever the cursor is)
        # until the count holds for two checks
        prev, stable = -1, 0
        for _ in range(30):
            rows = page.query_selector_all(selectors["track_row"])
            stable = stable + 1 if len(rows) == prev else 0
            if stable >= 2:
                break
            prev = len(rows)
            if rows:
                try:
                    rows[-1].scroll_into_view_if_needed(timeout=2000)
                except Exception:
                    pass
            else:
                page.mouse.wheel(0, 2000)
            page.wait_for_timeout(400)

        # recommended songs use the same rows below the real list and the virtual list scrambles
        # dom order, so cut by on screen position
        info = page.evaluate(
            """(rowSelector) => {
                const rows = Array.from(document.querySelectorAll(rowSelector));
                const marker = Array.from(document.querySelectorAll('h1,h2,h3,h4,div,span')).find(el => {
                    const t = (el.textContent || '').trim();
                    return t.length < 40 && /^(recommended songs?|you (might|may) also like|similar (playlists|songs|tracks)|suggested for you)$/i.test(t);
                });
                return {markerTop: marker ? marker.getBoundingClientRect().top : null,
                        tops: rows.map(r => r.getBoundingClientRect().top)};
            }""", selectors["track_row"])
        rows = sorted(zip(page.query_selector_all(selectors["track_row"]), info["tops"]), key=lambda p: p[1])
        marker = info["markerTop"]
        tracks = []
        for row, top in rows:
            if marker is not None and top >= marker:
                continue
            title_el = row.query_selector(selectors["track_title"])
            artist_el = row.query_selector(selectors["track_artist"])
            if title_el:
                tracks.append({"title": title_el.inner_text().strip(),
                               "artist": artist_el.inner_text().strip() if artist_el else ""})
        page.close()
        return tracks

    def get_playlist_name(self, playlist_url):
        data = self._gateway({"type": "GETplaylistdata", "playlistid": self._open_api(playlist_url)})
        return (data.get("title") or data.get("name") or "").strip()

    # --------------------------------------------------------------- searching

    @staticmethod
    def _song_hits(data):
        return [_song(item) for section in (data or {}).get("sections") or [] for item in section.get("data") or []
                if item.get("generictype", item.get("type")) == "song" and item.get("id")]

    def _search_api(self, query):
        return self._song_hits(self._gateway({"query": query, "page": "0", "filter_type": "top",
                                              "simple_results": "true"}, path=_search_path))

    def search_many(self, queries):
        out = {q: [] for q in queries if not q.strip()}
        queries = [q for q in queries if q.strip()]
        datas = self._gateway_many([({"query": q, "page": "0", "filter_type": "top", "simple_results": "true"},
                                     _search_path) for q in queries])
        out.update({q: self._song_hits(d) for q, d in zip(queries, datas)})
        return out

    def artist_songs_many(self, artists):
        """{artist credit: songs of every credited artist}"""
        split = {a: [p for p in re.split(r"\s*(?:,|&|\bfeat\.?|\bft\.?)\s*", a) if p.strip()] for a in artists}
        missing = list(dict.fromkeys(n for names in split.values() for n in names if n not in self._artist_songs_cache))
        if missing:
            self._artist_songs_cache.update(self._read_artists(missing))
        return {a: [t for n in names for t in self._artist_songs_cache.get(n, [])] for a, names in split.items()}

    def _read_artist_songs(self, name):
        return self._read_artists([name])[name]

    def _read_artists(self, names):
        # three batched rounds: name searches, first artist pages, then the rest of the pages
        out = {n: [] for n in names}
        try:
            datas = self._gateway_many([({"query": n, "page": "0", "filter_type": "top", "simple_results": "true"},
                                         _search_path) for n in names])
            ids = {}
            for name, data in zip(names, datas):
                if not data:
                    continue
                # artist page songs carry no keywords and search hits do, so those go first
                out[name] = self._song_hits(data)
                for section in data.get("sections") or []:
                    for item in section.get("data") or []:
                        if item.get("generictype", item.get("type")) == "artist" and item.get("id") and match_score(
                                {"title": "x", "artist": name},  # same title on both sides, only the artist counts
                                _with_aliases({"title": "x", "artist": item.get("title") or item.get("name") or ""},
                                              item.get("keywords"))) >= 90:
                            ids[name] = str(item["id"])
                            break
                    if name in ids:
                        break
            found = list(ids)
            first = self._gateway_many([({"type": "GETartistprofile", "artistId": ids[n]}, "/gateway.php") for n in found])
            more = []
            for name, data in zip(found, first):
                section = next((s for s in (data or {}).get("sections") or [] if s.get("type") == "song"), None)
                batch = (section or {}).get("data") or []
                out[name] += [_song(it) for it in batch if it.get("id")]
                if section and len(batch) >= 20:
                    more += [(name, page, str(section.get("sectionid"))) for page in range(1, _artist_pages)]
            rest = self._gateway_many([({"type": "GETartistprofile", "artistId": ids[n], "page": str(page),
                                         "sectionid": sid}, "/gateway.php") for n, page, sid in more])
            for (name, _page, _sid), data in zip(more, rest):
                section = next((s for s in (data or {}).get("sections") or [] if s.get("type") == "song"), None)
                out[name] += [_song(it) for it in (section or {}).get("data") or [] if it.get("id")]
        except Exception as e:
            self.log.info("Anghami: couldn't read artist pages for %s: %s", ", ".join(names), e)
        for name, songs in out.items():
            seen = set()
            out[name] = [t for t in songs if not (t["id"] in seen or seen.add(t["id"]))]
        return out

    # ---------------------------------------------------------------- writing

    def create_playlist(self, name):
        # private, the same call the player's Create Playlist dialog makes
        self._open_api()

        def own_playlists():
            data = self._gateway({"type": "GETplaylists"})
            section = next((s for s in data.get("sections") or [] if s.get("name") == "playlists"), {})
            return section.get("data") or []

        before = {str(p.get("id")) for p in own_playlists()}
        data = self._gateway({"type": "PUTplaylist", "name": name, "public": "false"}, post=True)
        playlist_id = str((data.get("playlist") or {}).get("id") or data.get("playlistid") or "")
        if not playlist_id:
            new = [p for p in own_playlists() if p.get("name") == name and str(p.get("id")) not in before]
            if not new:
                raise RuntimeError("Anghami didn't return the new playlist")
            playlist_id = str(new[0]["id"])
        self.log.info("Anghami: created playlist '%s' (%s)", name, playlist_id)
        return f"https://play.anghami.com/playlist/{playlist_id}"

    def add_tracks(self, playlist_url, tracks, threshold=87, exact_version=False):
        """appends in one call, checks with one re-read. returns the songs not confirmed added"""
        if not tracks:
            return []
        try:
            playlist_id = self._open_api(playlist_url)
            already = {t["id"] for t in self._read_playlist_api(playlist_id)}
            hits = find_many(tracks, self.search_many, threshold, exact_version, self.log, "Anghami",
                             self.artist_songs_many, rounds=2)
            found = [(t, hit) for t, hit in zip(tracks, hits) if hit]
            failed = [t for t, hit in zip(tracks, hits) if not hit]
            new_ids = list(dict.fromkeys(hit["id"] for _t, hit in found if hit["id"] not in already))
            if new_ids:
                self._gateway({"type": "PUTplaylist", "playlistid": playlist_id,
                               "action": "append", "songID": ",".join(new_ids)}, post=True)
            if not found:
                return failed
            now = {t["id"] for t in self._read_playlist_api(playlist_id)}
        except (AuthError, PlaylistNotFoundError):
            raise
        except Exception as e:
            self.log.warning("Anghami: adding failed (%s), nothing was added this run.", e)
            return list(tracks)
        for t, hit in found:
            if hit["id"] in now:
                self.log.info("Anghami: added '%s - %s' as '%s - %s'", t["title"], t["artist"], hit["title"], hit["artist"])
            else:
                self.log.warning("Anghami: '%s - %s' isn't in the playlist after adding", t["title"], t["artist"])
                failed.append(t)
        return failed

    def remove_tracks(self, playlist_url, tracks, threshold=87):
        """the player's delete call per song, checked with one re-read. returns what's still there"""
        if not tracks:
            return []
        try:
            playlist_id = self._open_api(playlist_url)
            current = self._read_playlist_api(playlist_id)
        except (AuthError, PlaylistNotFoundError):
            raise
        except Exception as e:
            self.log.warning("Anghami: couldn't read the playlist to remove songs (%s).", e)
            return list(tracks)
        by_id = {t["id"]: t for t in current}
        remaining, targets, failed = list(current), [], []
        for t in tracks:
            match = by_id.get(t.get("id")) if t.get("id") in by_id else best_match(t, remaining, threshold)
            if match and match in remaining:
                remaining.remove(match)
                targets.append((t, match))
            else:
                self.log.warning("Anghami: couldn't find '%s - %s' on the playlist to remove it", t["title"], t["artist"])
                failed.append(t)
        for t, match in targets:
            try:
                self._gateway({"type": "PUTplaylist", "playlistid": playlist_id,
                               "action": "delete", "songid": match["id"]}, post=True)
            except Exception as e:
                self.log.warning("Anghami: removing '%s - %s' failed: %s", t["title"], t["artist"], e)
        if not targets:
            return failed
        try:
            left = {t["id"] for t in self._read_playlist_api(playlist_id)}
        except Exception as e:
            self.log.warning("Anghami: couldn't re-read the playlist after removing (%s).", e)
            return failed + [t for t, _m in targets]
        still = [t for t, m in targets if m["id"] in left]
        self.log.info("Anghami: removed %d/%d song(s)", len(targets) - len(still), len(tracks))
        return failed + still
