"""
spotify client. replays the web player's own graphql requests (captured from a logged in page so
they carry its auth), reading the page itself is the fallback for playlists
"""

import copy
import json
import re
import time
import urllib.parse
from pathlib import Path

import browsersessioncommon as bsc
from matcher import best_match, find_many
from browsersessioncommon import AuthError, PlaylistNotFoundError, RateLimitedError

selectors = {
    # the header row is aria-rowindex 1
    "playlist_track_row": "div[role='row'][aria-rowindex]:not([aria-rowindex='1'])",
    "track_title": "a[data-testid='internal-track-link']",
    "track_artist": "a[href^='/artist/']",
    "playlist_header": "h1[class*='encore-text-headline-large']",
    "create_button": "button[aria-label='Create']",
    # a blend's description says "playlist" too
    "create_playlist_item": "[role='menuitem']:has-text('Create a playlist')",
    "edit_details_name": "[data-testid='playlist-edit-details-name-input']",
    "edit_details_save": "[data-testid='playlist-edit-details-save-button']",
    "more_button": "[data-testid='action-bar-row'] [data-testid='more-button']",
    "delete_item": "[role='menuitem']:has-text('Delete')",
    "delete_dialog": "[role='dialog']:not([hidden]):has(button:has-text('Delete'))",
}
# the search request's shape (no login in it), saved so later runs skip loading a search page
_search_template_file = "spotify_search_template.json"
# the cookie banner eats clicks at the bottom of the page. hidden, never answered
_hide_cookie_banner_js = (
    "document.addEventListener('DOMContentLoaded', () => {"
    " const s = document.createElement('style');"
    " s.textContent = '#onetrust-consent-sdk { display: none !important; }';"
    " document.head.appendChild(s); });")


def _mark_failed(details, tracks, message):
    while len(details) < len(tracks):
        details.append({})
    for info in details:
        info["error"] = message


def _smallest_image(sources):
    sources = [s for s in sources or [] if s.get("url")]
    return min(sources, key=lambda s: s.get("width") or 9999)["url"] if sources else ""


def playlist_id_of(playlist_url):
    m = re.search(r"/playlist/([A-Za-z0-9]+)", playlist_url or "")
    return m.group(1) if m else None


class SpotifyScraper(bsc.BrowserClient):
    session_file = "spotify_session.json"
    service = "Spotify"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # taken from the player's own requests
        self._api_url = self._api_headers = None
        self._api_hashes = {}
        self._bundle_url = None
        self._search_template = None
        self._playlist_template = None  # its fetchPlaylist request, replayed for any playlist
        self._playlist_names = {}
        self._light_page = None
        self._library_template = None  # its libraryV3 request, for listing playlists

    def __enter__(self):
        super().__enter__()
        self._context.add_init_script(_hide_cookie_banner_js)
        return self

    def _diagnose_failed_load(self, page, response, playlist_url):
        # AuthError or PlaylistNotFoundError when the page says which, else None
        url = page.url or ""
        try:
            body = page.inner_text("body").lower()
        except Exception:
            body = ""
        if "accounts.spotify.com" in url or "/login" in url or ("playlist" not in url and any(
                p in body for p in ("log in to spotify", "continue with google", "continue with facebook"))):
            return AuthError("Spotify session looks logged out. Get a fresh session on the Setup tab.")
        if (response and response.status == 404) or any(
                p in body for p in ("page not found", "couldn't find that page", "we can't find this page")):
            return PlaylistNotFoundError(f"Spotify playlist link looks invalid or the playlist is gone: {playlist_url}")
        return None

    # ---------------------------------------------------------------- reading

    def get_playlist_tracks(self, playlist_url):
        # [{title, artist, uri, uid, duration}]. only the first read of a run loads the player,
        # later ones replay its fetchPlaylist request for the new id
        playlist_id = playlist_id_of(playlist_url)
        if self._playlist_template and self._api_url and playlist_id:
            body = copy.deepcopy(self._playlist_template)
            body["variables"]["uri"] = f"spotify:playlist:{playlist_id}"
            tracks = self._fetch_playlist(body)
            if tracks is not None:
                return tracks
            # a link the replay can't read goes the long way, which can tell what's wrong

        page = self._context.new_page()
        captured = []
        page.on("request", lambda req: captured.append(req) if (
            "pathfinder" in req.url and '"fetchPlaylist"' in (req.post_data or "")
            and (not playlist_id or playlist_id in (req.post_data or ""))) else None)
        response = page.goto(playlist_url, wait_until="domcontentloaded")  # the player never goes idle
        for attempt in range(2):
            deadline = time.time() + self._t(15000) / 1000
            crashed = False
            while not captured and time.time() < deadline:
                page.wait_for_timeout(300)
                crashed = self._player_crashed(page)
                if crashed:
                    break
            if crashed and attempt == 0:
                # the player's own "Something went wrong" screen, one reload clears it
                self.log.info("Spotify player showed 'Something went wrong', reloading the page.")
                response = page.reload(wait_until="domcontentloaded")
            else:
                break
        if captured:
            req = captured[0]
            self._remember_api(req)
            if not self._bundle_url:
                try:
                    self._bundle_url = page.evaluate(
                        r"() => [...document.scripts].map(s => s.src).find(s => /web-player\.[0-9a-f]+\.js$/.test(s)) || null")
                except Exception:
                    pass
            try:
                body = json.loads(req.post_data)
            except ValueError:
                body = None
            tracks = self._fetch_playlist(body) if body else None
            if tracks is not None:
                if "uri" in body.get("variables", {}):
                    self._playlist_template = body
                page.close()
                return tracks
            self.log.info("Spotify API read failed for %s, reading the page instead.", playlist_url)
        return self._get_playlist_tracks_dom(page, response, playlist_url)

    def _fetch_playlist(self, body):
        # first page, then the rest together. None on failure
        try:
            body["variables"].update(offset=0, limit=100)
            playlist = self._api_post(body)["data"]["playlistV2"]
            content = playlist["content"]
            total, items = content["totalCount"], list(content.get("items") or [])
            if items and len(items) < total:
                pages = []
                for offset in range(len(items), total, 100):
                    page_body = copy.deepcopy(body)
                    page_body["variables"].update(offset=offset, limit=100)
                    pages.append(page_body)
                for result in self._api_post_many(pages):
                    if result is None:
                        raise RuntimeError("a playlist page failed to load")
                    items += result["data"]["playlistV2"]["content"].get("items") or []
        except RateLimitedError:
            raise
        except Exception as e:
            self.log.info("Spotify fetchPlaylist replay failed: %s", e)
            return None
        if playlist.get("name"):
            self._playlist_names[body["variables"].get("uri", "").rsplit(":", 1)[-1]] = playlist["name"]

        tracks, skipped = [], 0
        for item in items:
            data = (item.get("itemV2") or {}).get("data") or {}
            if data.get("__typename") not in ("Track", "LocalTrack") or not data.get("name"):
                skipped += 1
                continue
            artists = [a.get("profile", {}).get("name", "") for a in (data.get("artists") or {}).get("items", [])]
            tracks.append({
                "title": data["name"].strip(),
                "artist": ", ".join(a for a in artists if a),
                "uri": data.get("uri", ""),
                "uid": item.get("uid", ""),
                "duration": ((data.get("trackDuration") or {}).get("totalMilliseconds") or 0) / 1000,
                "image": _smallest_image(((data.get("albumOfTrack") or {}).get("coverArt") or {}).get("sources")),
            })
        if skipped:
            self.log.info("Spotify playlist: skipped %d item(s) that aren't playable songs (podcasts or removed songs).", skipped)
        if total and len(items) < total:
            self.log.warning("Spotify playlist reports %d items but only %d came back.", total, len(items))
        return tracks

    @staticmethod
    def _player_crashed(page):
        try:
            return "something went wrong" in page.inner_text("body", timeout=1000).lower()
        except Exception:
            return False

    def _get_playlist_tracks_dom(self, page, response, playlist_url):
        try:
            page.wait_for_selector(selectors["playlist_track_row"], timeout=self._t(15000))
        except Exception as e:
            error = self._diagnose_failed_load(page, response, playlist_url)
            self._dump(page, "debug_spotify_playlist_load_failed")
            page.close()
            raise (error or e) from e

        def visible_rows():
            # recommended rows look the same and sit below the list. the virtual list scrambles dom
            # order, so cut by on screen position and order by aria-rowindex
            info = page.evaluate(
                """(rowSelector) => {
                    const rows = Array.from(document.querySelectorAll(rowSelector));
                    const marker = Array.from(document.querySelectorAll('h1,h2,h3,h4,div,span')).find(el => {
                        const t = (el.textContent || '').trim();
                        return t.length < 40 && /^(recommended|recommended songs?|you (might|may) also like|suggested for you)$/i.test(t);
                    });
                    return {markerTop: marker ? marker.getBoundingClientRect().top : null,
                            rows: rows.map(r => ({index: parseInt(r.getAttribute('aria-rowindex'), 10),
                                                  top: r.getBoundingClientRect().top}))};
                }""", selectors["playlist_track_row"])
            found = {}
            for el, row in zip(page.query_selector_all(selectors["playlist_track_row"]), info["rows"]):
                if info["markerTop"] is not None and row["top"] >= info["markerTop"]:
                    continue
                title_el = el.query_selector(selectors["track_title"])
                if not title_el:
                    continue
                artist_el = el.query_selector(selectors["track_artist"])
                href = title_el.get_attribute("href") or ""
                found[row["index"]] = {
                    "title": title_el.inner_text().strip(),
                    "artist": artist_el.inner_text().strip() if artist_el else "",
                    "uri": f"spotify:track:{href.split('/')[-1].split('?')[0]}" if href.startswith("/track/") else "",
                }
            return found

        # rows scrolled past leave the dom so collect at every step. scroll the last row into view
        # (the mouse wheel hits wherever the cursor is) until the count holds for two checks
        collected, prev, stable = {}, -1, 0
        for _ in range(30):
            collected.update(visible_rows())
            rows = page.query_selector_all(selectors["playlist_track_row"])
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
                page.mouse.wheel(0, 3000)
            page.wait_for_timeout(500)
        if not collected:
            page.wait_for_timeout(1500)  # rows can be there before their links fill in
            collected.update(visible_rows())
        if not collected:
            self.log.warning("Spotify page read found no songs on %s.", playlist_url)
            self._dump(page, "debug_spotify_empty_read")
        page.close()
        return [collected[i] for i in sorted(collected)]

    def get_playlist_name(self, playlist_url):
        playlist_id = playlist_id_of(playlist_url)
        if playlist_id not in self._playlist_names:
            self.get_playlist_tracks(playlist_url)
        if playlist_id in self._playlist_names:
            return self._playlist_names[playlist_id]
        page = self._context.new_page()
        try:
            page.goto(playlist_url, wait_until="domcontentloaded")
            try:
                return page.wait_for_selector(selectors["playlist_header"], timeout=self._t(15000)).inner_text().strip()
            except Exception:
                # the tab title reads "<name> - playlist by <user> | Spotify"
                return page.title().split(" - playlist")[0].split(" | Spotify")[0].strip()
        finally:
            page.close()

    def list_playlists(self):
        """the playlists in the account's library, folders flattened. editable is whether this
        account can change its songs (its own, or a collaborative one)"""
        template = self._library_template
        if template is None:
            page = self._context.new_page()
            try:
                with page.expect_request(lambda r: "pathfinder" in r.url and '"libraryV3"' in (r.post_data or ""),
                                         timeout=self._t(30000)) as info:
                    page.goto("https://open.spotify.com/", wait_until="domcontentloaded")
                self._remember_api(info.value)
                template = self._library_template = json.loads(info.value.post_data)
            finally:
                page.close()
        limit = 50

        def body(offset):
            b = copy.deepcopy(template)
            b["variables"].update(offset=offset, limit=limit, flatten=True, expandedFolders=[], folderUri=None,
                                  textFilter="", filters=["Playlists"])
            return b

        first = self._api_post(body(0))["data"]["me"]["libraryV3"]
        pages = [first]
        total = first.get("totalCount") or 0
        if total > limit:
            pages += [r["data"]["me"]["libraryV3"] for r in self._api_post_many([body(o) for o in range(limit, total, limit)])
                      if r and (r.get("data") or {}).get("me")]
        out, seen = [], set()
        for page_data in pages:
            for entry in page_data.get("items") or []:
                data = (entry.get("item") or {}).get("data") or {}
                if data.get("__typename") != "Playlist" or not data.get("uri"):
                    continue
                pid = data["uri"].rsplit(":", 1)[-1]
                if pid in seen:
                    continue
                seen.add(pid)
                images = (data.get("images") or {}).get("items") or [{}]
                out.append({"platform": "spotify", "id": pid, "name": (data.get("name") or "").strip(),
                            "url": f"https://open.spotify.com/playlist/{pid}", "count": None,
                            "image": _smallest_image(images[0].get("sources")),
                            "editable": bool((data.get("currentUserCapabilities") or {}).get("canEditItems"))})
        return out

    # -------------------------------------------------------------- api calls

    def _remember_api(self, req):
        skip = {"content-length", "cookie", "host", "accept-encoding"}
        self._api_url = req.url
        self._api_headers = {k: v for k, v in req.all_headers().items() if not k.startswith(":") and k.lower() not in skip}
        self._api_headers["accept-encoding"] = "gzip"  # playwright can't decode the zstd/br chrome asks for
        try:
            data = json.loads(req.post_data)
            self._api_hashes[data["operationName"]] = data["extensions"]["persistedQuery"]["sha256Hash"]
        except Exception:
            pass

    def _api_post(self, body):
        # the token lasts about an hour, a 401 takes a fresh one and retries once. a 429 waits and
        # retries once, a second one stops the sync
        post = lambda: self._context.request.post(self._api_url, data=json.dumps(body), headers=self._api_headers,
                                                  timeout=self._t(20000))
        resp = post()
        if resp.status == 401 and self._refresh_api_auth():
            resp = post()
        if resp.status == 429:
            self._back_off(resp.headers.get("retry-after"))
            resp = post()
            if resp.status == 429:
                raise RateLimitedError(bsc.rate_limited_message(self.service))
        if not resp.ok:
            raise RuntimeError(f"{body.get('operationName')} returned HTTP {resp.status}")
        result = resp.json()
        if result.get("errors"):
            raise RuntimeError(f"{body.get('operationName')} failed: {result['errors'][0].get('message')}")
        return result

    def _api_post_many(self, bodies):
        """several posts side by side from a player origin page, None for any that failed"""
        if not bodies:
            return []
        if self._light_page is None:
            self._light_page = self._context.new_page()
            # any page on the player's origin will do, robots.txt loads instantly
            self._light_page.goto("https://open.spotify.com/robots.txt", wait_until="domcontentloaded")
        for attempt in range(2):
            headers = {k: v for k, v in self._api_headers.items()
                       if k.lower() not in ("user-agent", "accept-encoding", "referer", "origin")
                       and not k.lower().startswith("sec-")}
            raw = self._fetch_many(self._light_page, [(self._api_url, {"method": "POST", "headers": headers,
                                                                     "body": json.dumps(b)}) for b in bodies])
            if attempt == 0 and any(status == 401 for status, _ in raw) and self._refresh_api_auth():
                continue
            break
        results = []
        for body, (status, text) in zip(bodies, raw):
            try:
                data = json.loads(text) if status == 200 else None
                if isinstance(data, dict) and data.get("errors"):
                    data = None
            except ValueError:
                data = None
            if data is None:
                self.log.debug("Spotify %s failed (HTTP %s)", body.get("operationName"), status)
            results.append(data)
        return results

    def _refresh_api_auth(self):
        page = self._context.new_page()
        try:
            with page.expect_request(lambda r: "pathfinder" in r.url, timeout=self._t(20000)) as info:
                page.goto("https://open.spotify.com/", wait_until="domcontentloaded")
            self._remember_api(info.value)
            self.log.info("Spotify: the session token expired mid run, took a fresh one.")
            return True
        except Exception as e:
            self.log.warning("Spotify: couldn't refresh the expired session token: %s", e)
            return False
        finally:
            page.close()

    def _op_hash(self, op):
        if op in self._api_hashes:
            return self._api_hashes[op]
        if not self._bundle_url:
            raise RuntimeError("web player script not found")
        text = self._context.request.get(self._bundle_url, timeout=self._t(30000)).text()
        for name, h in re.findall(r'"(\w+)","(?:query|mutation)","([0-9a-f]{64})"', text):
            self._api_hashes.setdefault(name, h)
        if op not in self._api_hashes:
            raise RuntimeError(f"no hash for {op} in the web player script")
        return self._api_hashes[op]

    def _api_call(self, op, variables):
        return self._api_post({"variables": variables, "operationName": op,
                               "extensions": {"persistedQuery": {"version": 1, "sha256Hash": self._op_hash(op)}}})

    # --------------------------------------------------------------- searching

    def _ensure_search_template(self, query="music"):
        if self._search_template is None and self._api_url:
            try:
                self._search_template = json.loads(Path(_search_template_file).read_text(encoding="utf-8"))
                return
            except (OSError, ValueError):
                pass
        if self._search_template is None:
            self._capture_search_template(query)

    def _capture_search_template(self, query="music"):
        page = self._context.new_page()
        try:
            with page.expect_request(lambda r: "pathfinder" in r.url and '"searchTopResultsList"' in (r.post_data or ""),
                                     timeout=self._t(20000)) as info:
                page.goto(f"https://open.spotify.com/search/{urllib.parse.quote(query)}", wait_until="domcontentloaded")
            self._remember_api(info.value)
            self._search_template = json.loads(info.value.post_data)
        finally:
            page.close()
        try:
            Path(_search_template_file).write_text(json.dumps(self._search_template), encoding="utf-8")
        except OSError:
            pass

    def _search_body(self, query):
        body = copy.deepcopy(self._search_template)
        body["variables"].update(query=query, offset=0, limit=20, numberOfTopResults=20)
        return body

    def _api_search_candidates(self, query):
        if not query.strip():
            return []
        self._ensure_search_template(query)
        return self._search_hits(self._api_post(self._search_body(query)))

    def search_many(self, queries):
        # every search failing means the saved request shape is stale: learn it again, retry once.
        # blank queries never go out, spotify rejects them, which read as a stale request
        out = {q: [] for q in queries if not q.strip()}
        queries = [q for q in queries if q.strip()]
        if not queries:
            return out
        self._ensure_search_template(queries[0])
        results = self._api_post_many([self._search_body(q) for q in queries])
        if all(r is None for r in results):
            self.log.info("Spotify: the saved search request was rejected, learning it again.")
            self._capture_search_template(queries[0])
            results = self._api_post_many([self._search_body(q) for q in queries])
        out.update({q: self._search_hits(r) for q, r in zip(queries, results)})
        return out

    @staticmethod
    def _search_hits(result):
        items = ((((result or {}).get("data") or {}).get("searchV2") or {}).get("topResultsV2") or {}).get("itemsV2") or []
        hits = []
        for it in items:
            data = (it.get("item") or {}).get("data") or {}
            if data.get("__typename") != "Track":
                continue
            artists = [a["profile"]["name"] for a in (data.get("artists") or {}).get("items", [])]
            hits.append({"title": data.get("name", ""), "artist": ", ".join(artists), "uri": data.get("uri", ""),
                         "duration": ((data.get("duration") or data.get("trackDuration") or {})
                                      .get("totalMilliseconds") or 0) / 1000})
        return hits

    # ---------------------------------------------------------------- writing

    def create_playlist(self, name):
        # clicked, not called: the public api rate limits the player's token and the player's own
        # create call is a two step protobuf exchange
        page = self._context.new_page()
        try:
            page.goto("https://open.spotify.com/", wait_until="domcontentloaded")
            page.wait_for_selector(selectors["create_button"], timeout=self._t(20000)).click()
            page.wait_for_selector(selectors["create_playlist_item"], timeout=self._t(5000)).click()
            page.wait_for_url(re.compile(r"/playlist/[A-Za-z0-9]+"), timeout=self._t(15000))
            playlist_url = f"https://open.spotify.com/playlist/{playlist_id_of(page.url)}"
            try:
                page.wait_for_selector(selectors["playlist_header"], timeout=self._t(15000)).click()
                page.wait_for_selector(selectors["edit_details_name"], timeout=self._t(5000)).fill(name)
                page.click(selectors["edit_details_save"])
                page.wait_for_selector(selectors["edit_details_name"], state="detached", timeout=self._t(10000))
                self.log.info("Spotify: created playlist '%s' (%s)", name, playlist_url)
            except Exception as e:
                # it exists either way, keep it rather than make another next sync
                self.log.warning("Spotify: created %s but couldn't rename it to '%s': %s", playlist_url, name, e)
                self._dump(page, "debug_spotify_rename_failed")
            return playlist_url
        finally:
            page.close()

    def rename_playlist(self, playlist_url, name):
        # the player's edit details dialog, like create_playlist
        page = self._context.new_page()
        try:
            page.goto(playlist_url, wait_until="domcontentloaded")
            page.wait_for_selector(selectors["playlist_header"], timeout=self._t(20000)).click()
            page.wait_for_selector(selectors["edit_details_name"], timeout=self._t(5000)).fill(name)
            page.click(selectors["edit_details_save"])
            page.wait_for_selector(selectors["edit_details_name"], state="detached", timeout=self._t(10000))
            self.log.info("Spotify: renamed %s to '%s'", playlist_url, name)
        except Exception:
            self._dump(page, "debug_spotify_rename_failed")
            raise
        finally:
            page.close()

    def delete_playlist(self, playlist_url):
        # the playlist's ... menu, Delete, then Delete again in the dialog it asks with
        page = self._context.new_page()
        try:
            page.goto(playlist_url, wait_until="domcontentloaded")
            page.wait_for_selector(selectors["more_button"], timeout=self._t(20000)).click()
            page.wait_for_selector(selectors["delete_item"], timeout=self._t(5000)).click()
            dialog = page.wait_for_selector(selectors["delete_dialog"], timeout=self._t(5000))
            dialog.query_selector("button:has-text('Delete')").click()
            page.wait_for_selector(selectors["delete_dialog"], state="detached", timeout=self._t(10000))
            self.log.info("Spotify: deleted %s", playlist_url)
        except Exception:
            self._dump(page, "debug_spotify_delete_failed")
            raise
        finally:
            page.close()

    def find_tracks(self, playlist_url, tracks, threshold=87, exact_version=False, details=None, known=None,
                    rejected=None):
        """the song on spotify for each of tracks (None where there's none), read only"""
        if not self._api_url:
            self.get_playlist_tracks(playlist_url)
        # searching the artist's name last brings up their best known songs
        return find_many(tracks, self.search_many, threshold, exact_version, self.log, "Spotify", self.search_many,
                         details=details, known=known, rejected=rejected)

    def add_tracks(self, playlist_url, tracks, threshold=87, exact_version=False, details=None, known=None,
                   rejected=None):
        """adds in one call, checks with one re-read. returns the songs not confirmed added. details
        (a list) gets each song's find_many detail, plus confirmed or an error message"""
        if details is None:
            details = []
        if not tracks:
            return []
        try:
            hits = self.find_tracks(playlist_url, tracks, threshold, exact_version, details, known, rejected)
            found = [(t, hit) for t, hit in zip(tracks, hits) if hit]
            failed = [t for t, hit in zip(tracks, hits) if not hit]
            if not found:
                return failed
            self._api_call("addToPlaylist", {
                "playlistUri": f"spotify:playlist:{playlist_id_of(playlist_url)}",
                "playlistItemUris": [hit["uri"] for _t, hit in found],
                "newPosition": {"moveType": "BOTTOM_OF_PLAYLIST", "fromUid": None},
                "interactionId": None,
            })
            current = {t["uri"] for t in self.get_playlist_tracks(playlist_url)}
        except (AuthError, PlaylistNotFoundError, RateLimitedError):
            raise
        except Exception as e:
            self.log.warning("Spotify: adding failed (%s), nothing was added this run.", e, extra={"feed": False})
            _mark_failed(details, tracks, f"Adding failed: {e}")
            return list(tracks)
        index_of = {id(t): i for i, t in enumerate(tracks)}
        for t, hit in found:
            info = details[index_of[id(t)]] if len(details) == len(tracks) else {}
            if hit["uri"] in current:
                info["confirmed"] = True
                self.log.info("Spotify: added '%s - %s' as '%s - %s'", t["title"], t["artist"], hit["title"], hit["artist"])
            else:
                info["error"] = "Not in the playlist after adding"
                self.log.warning("Spotify: '%s - %s' isn't in the playlist after adding", t["title"], t["artist"],
                                 extra={"feed": False})
                failed.append(t)
        return failed

    def add_exact(self, playlist_url, tracks):
        """adds songs that are already spotify songs (they carry their uri), no searching. returns the
        ones not confirmed added; ones already in the playlist count as added"""
        if not tracks:
            return []
        current = {t["uri"] for t in self.get_playlist_tracks(playlist_url)}
        uris = list(dict.fromkeys(t["uri"] for t in tracks if t.get("uri") and t["uri"] not in current))
        if uris:
            for start in range(0, len(uris), 100):
                self._api_call("addToPlaylist", {
                    "playlistUri": f"spotify:playlist:{playlist_id_of(playlist_url)}",
                    "playlistItemUris": uris[start:start + 100],
                    "newPosition": {"moveType": "BOTTOM_OF_PLAYLIST", "fromUid": None},
                    "interactionId": None,
                })
            current = {t["uri"] for t in self.get_playlist_tracks(playlist_url)}
        failed = [t for t in tracks if t.get("uri") not in current]
        self.log.info("Spotify: added %d song(s) by uri", len(tracks) - len(failed))
        return failed

    def move_tracks(self, playlist_url, uids, before_uid=None):
        """moves playlist items (by uid) to just before before_uid, or to the end without one"""
        position = ({"moveType": "BEFORE_UID", "fromUid": before_uid} if before_uid
                    else {"moveType": "BOTTOM_OF_PLAYLIST", "fromUid": None})
        self._api_call("moveItemsInPlaylist", {"playlistUri": f"spotify:playlist:{playlist_id_of(playlist_url)}",
                                               "uids": list(uids), "newPosition": position})
        return self.get_playlist_tracks(playlist_url)

    def remove_tracks(self, playlist_url, tracks, threshold=87):
        """removes by playlist item uid in one call, checks with one re-read. returns what's still there"""
        if not tracks:
            return []
        current = self.get_playlist_tracks(playlist_url)
        if not self._api_url or not all(t.get("uid") for t in current):
            self.log.warning("Spotify: couldn't read playlist item ids, nothing was removed this run.")
            return list(tracks)
        by_uid = {t["uid"]: t for t in current}
        remaining, targets, failed = list(current), [], []
        for t in tracks:
            match = by_uid.get(t.get("uid")) if t.get("uid") in by_uid else best_match(t, remaining, threshold)
            if match and match in remaining:
                remaining.remove(match)
                targets.append((t, match))
            else:
                self.log.warning("Spotify: couldn't find '%s - %s' on the playlist to remove it", t["title"], t["artist"])
                failed.append(t)
        if not targets:
            return failed
        try:
            self._api_call("removeFromPlaylist", {"playlistUri": f"spotify:playlist:{playlist_id_of(playlist_url)}",
                                                  "uids": [m["uid"] for _t, m in targets]})
            left = {t.get("uid") for t in self.get_playlist_tracks(playlist_url)}
        except (AuthError, PlaylistNotFoundError, RateLimitedError):
            raise
        except Exception as e:
            self.log.warning("Spotify: removing failed (%s).", e)
            return failed + [t for t, _m in targets]
        still = [t for t, m in targets if m["uid"] in left]
        self.log.info("Spotify: removed %d/%d song(s)", len(targets) - len(still), len(tracks))
        return failed + still
