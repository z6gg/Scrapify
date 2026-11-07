"""
sync engine. mirrors each playlist pair one way: the destination is changed to match the source,
the source is never touched

    python main.py                          one sync pass, direction from config.json
    python main.py --dry-run                log what would change, change nothing
    python main.py --daemon --interval 30   keep syncing in the background
    python main.py --version
"""

import copy
import json
import logging
import os
import sys
import time

import sysutil

sysutil.use_data_dir()

from playwright.sync_api import sync_playwright

import browsersessioncommon as bsc
import choices
from anghamiclient import AnghamiClient
from matcher import match_pairs, pair_tracks, track_key
from browsersessioncommon import AuthError, PlaylistNotFoundError
from spotifyscraper import SpotifyScraper
from version import version

# a cp1252 console can't print arabic titles
for _stream in (sys.stdout, sys.stderr):
    if _stream is not None and hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass


log_file = "sync.log"


def trim_log(limit=2_000_000):
    # every scrapify process appends to it, so it gets cut to its newest half instead of rotated
    try:
        if os.path.getsize(log_file) <= limit:
            return
        with open(log_file, "rb") as f:
            f.seek(-limit // 2, os.SEEK_END)
            tail = f.read()
        with open(log_file, "wb") as f:
            f.write(tail[tail.find(b"\n") + 1:])
    except OSError:
        pass


trim_log()
_handlers = [logging.FileHandler(log_file, encoding="utf-8")]
if sys.stderr is not None:
    _handlers.append(logging.StreamHandler())
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", handlers=_handlers)
log = logging.getLogger("main")
# what the window's activity feed shows: one record per pair with every song's outcome. not written
# to sync.log, which keeps its own lines
activity = logging.getLogger("scrapify.activity")
activity.propagate = False

config_file = "config.json"
pid_file = "scrapify_autosync.pid"
platform_names = {"spotify": "Spotify", "anghami": "Anghami"}
directions = {"spotify_to_anghami": ("spotify", "anghami"), "anghami_to_spotify": ("anghami", "spotify")}

default_config = {
    "spotify": {"storage_state_file": "spotify_session.json"},
    "anghami": {"storage_state_file": "anghami_session.json"},
    "playlist_pairs": [],
    "match_threshold": 87,
    "sync_direction": "spotify_to_anghami",
    "retry_attempts": 2,
    "request_speed": bsc.default_request_speed,
    "pause_while_running": [],
    "pause_while_fullscreen": False,
    "show_browser": False,
    "debug_dumps": False,
    "sync_timeout_multiplier": 1.0,
    "notifications": True,  # every notification
    "notifications_enabled": True,  # sync started and finished
    "ask_notifications": True,  # semi-automatic matching found songs to ask about
    "match_mode": "automatic",  # or "semi": unsure songs wait for a yes or no
    **{f"opt_{key}": on for key, on in bsc.default_browser_flags.items()},
}


def direction_label(direction):
    source, dest = directions[direction]
    return f"{platform_names[source]} -> {platform_names[dest]}"


def load_config(path=config_file):
    # an unreadable config is kept as config.json.broken and started over
    config = None
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                config = json.load(f)
            if not isinstance(config, dict):
                raise ValueError("not a JSON object")
        except (OSError, ValueError) as e:
            log.warning("config.json couldn't be read (%s), kept it as config.json.broken and started over.", e)
            try:
                os.replace(path, path + ".broken")
            except OSError:
                pass
            config = None
    if config is None:
        config = copy.deepcopy(default_config)
        save_config(config, path)
        return config
    for key, value in default_config.items():
        config.setdefault(key, copy.deepcopy(value))
    return config


def save_config(config, path=config_file):
    # write then swap, so nothing ever reads a half written file
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def missing_sessions(config):
    return [platform_names[p] for p in platform_names if not bsc.session_file_logged_in(platform_names[p], config[p]["storage_state_file"])]


def make_client(platform, playwright, config):
    client_cls = SpotifyScraper if platform == "spotify" else AnghamiClient
    return client_cls(
        playwright,
        storage_state_file=config[platform]["storage_state_file"],
        headless=not config.get("show_browser", False),
        debug_dumps=config.get("debug_dumps", False),
        timeout_multiplier=config.get("sync_timeout_multiplier", 1.0),
        optimization_flags={k: config.get(f"opt_{k}", on) for k, on in bsc.default_browser_flags.items()},
        request_speed=config.get("request_speed", bsc.default_request_speed),
    )


def exclusion_keys(track):
    # the platform id too, so an exclusion survives changes to name normalizing
    keys = [track_key(track["title"], track["artist"])]
    if track.get("uri"):
        keys.append(track["uri"])
    if track.get("id"):
        keys.append(f"anghami:{track['id']}")
    return keys


def is_excluded(track, excluded):
    return bool(excluded) and any(k in excluded for k in exclusion_keys(track))


def _emit(kind, **data):
    activity.info(kind, extra={"activity": {"kind": kind, "time": time.time(), **data}})


def _brief(track):
    return {k: track[k] for k in ("title", "artist", "duration") if track.get(k)} if track else None


def _row(status, track, match=None, note=None, **extra):
    return {"status": status, "title": track.get("title", ""), "artist": track.get("artist", ""),
            "duration": track.get("duration"), "match": _brief(match), "note": note, **extra}


def diff_and_apply(pair_name, source_tracks, dest_tracks, dest_client, threshold, dest_url, dest_label, dry_run=False,
                   mode="automatic", source_label=""):
    """returns the songs' outcomes, for the activity feed"""
    to_add, to_remove = pair_tracks(source_tracks, dest_tracks, threshold)
    # another version of the same song (live vs studio) is only swapped when the exact one exists,
    # or every sync would remove the close one and fail to add the exact one
    swaps, to_add, to_remove = match_pairs(to_add, to_remove, threshold, versions=False)
    log.info("[%s] +%s: %d, -%s: %d, version swaps to try: %d",
             pair_name, dest_label, len(to_add), dest_label, len(to_remove), len(swaps))
    data = choices.load()
    known, rejected = choices.known(dest_label, data), choices.rejected(dest_label, data)
    rows, questions = [], 0
    if not dry_run:
        choices.clear_weak(pair_name, dest_label)  # last sync's closest-match offers, nobody took them

    if swaps:
        swap_src = [s for s, _d in swaps]
        details = []
        if dry_run:
            hits = dest_client.find_tracks(dest_url, swap_src, threshold, True, details, known, rejected)
            failed = {id(t) for t, hit in zip(swap_src, hits) if not hit}
        else:
            failed = {id(t) for t in dest_client.add_tracks(dest_url, swap_src, threshold, True, details, known, rejected)}
        for (s, d), info in zip(swaps, details + [{}] * (len(swaps) - len(details))):
            if id(s) in failed:
                log.info("[%s] Kept '%s - %s' on %s as the closest version of '%s - %s'",
                         pair_name, d["title"], d["artist"], dest_label, s["title"], s["artist"])
                rows.append(_row("kept", s, d, f"Only a different version is on {dest_label}, kept it"))
            else:
                if not dry_run:
                    to_remove.append(d)
                rows.append(_row("added", s, info.get("hit"), f"Replaced '{d['title']}' with the exact version"))

    details = []
    if dry_run:
        hits = dest_client.find_tracks(dest_url, to_add, threshold, False, details, known, rejected) if to_add else []
        failed_ids = {id(t) for t, hit in zip(to_add, hits) if not hit}
    else:
        failed = dest_client.add_tracks(dest_url, to_add, threshold, False, details, known, rejected)
        failed_ids = {id(t) for t in failed}
        for t in failed:
            log.warning("[%s] Couldn't add on %s: %s - %s", pair_name, dest_label, t["title"], t["artist"],
                        extra={"feed": False})
    details += [{}] * (len(to_add) - len(details))
    for t, info in zip(to_add, details):
        hit, closest = info.get("hit"), info.get("closest")
        if info.get("error"):
            rows.append(_row("error", t, hit, info["error"]))
        elif hit and id(t) not in failed_ids:
            if info.get("same_version", True):
                rows.append(_row("added", t, hit, "Your earlier answer" if info.get("remembered") else None))
            else:
                rows.append(_row("version", t, hit, f"Only a different version is on {dest_label}"))
        elif mode == "semi" and info.get("unsure") and closest:
            if not dry_run and choices.add_pending(pair_name, dest_label, t, closest, info.get("score")):
                questions += 1
            rows.append(_row("unsure", t, closest, "Is this the same song? Answer at the top of the feed"))
        else:
            near = closest if info.get("score", 0) >= 40 else None
            if mode == "semi" and near and not dry_run:
                choices.add_pending(pair_name, dest_label, t, near, info.get("score"), weak=True)
            rows.append(_row("missing", t, near, f"Possibly not on {dest_label}"))

    if to_remove:
        if dry_run:
            still = set()
        else:
            still_tracks = dest_client.remove_tracks(dest_url, to_remove, threshold)
            still = {id(t) for t in still_tracks}
            for t in still_tracks:
                log.warning("[%s] Couldn't remove from %s: %s - %s", pair_name, dest_label, t["title"], t["artist"],
                            extra={"feed": False})
        for t in to_remove:
            if id(t) in still:
                rows.append(_row("error", t, None, f"Couldn't remove it from {dest_label}"))
            else:
                rows.append(_row("removed", t, None, None))

    if dry_run:
        for row in rows:
            verb = {"added": "add", "version": "add another version of", "kept": "keep the other version of",
                    "unsure": "ask about", "missing": "not find", "removed": "remove", "error": "fail on"}[row["status"]]
            log.info("[%s] would %s: %s - %s", pair_name, verb, row["title"], row["artist"])
    return rows, questions


def _save_new_link(pair_name, platform, url):
    # re-read fresh so edits made in the window meanwhile aren't lost
    config = load_config()
    for pair in config["playlist_pairs"]:
        if pair["name"] == pair_name:
            pair[f"{platform}_playlist_url"] = url
    save_config(config)


def _create_missing_playlist(pair, clients, confirm_create, summary, dry_run):
    # a pair with one link blank gets that playlist created, named like the other, if the caller
    # confirms. it's empty so it's this run's destination whatever the direction. None skips the pair
    name = pair["name"]
    blank = [p for p in platform_names if not pair.get(f"{p}_playlist_url")]
    if len(blank) == 2:
        summary["errors"].append({"pair": name, "type": "missing_link", "message": "No playlist links set."})
        log.warning("[%s] No playlist links set, skipping.", name)
        return None
    target = blank[0]
    source = "anghami" if target == "spotify" else "spotify"
    label = platform_names[target]
    if confirm_create is None:
        message = f"No {label} playlist link set. Run a manual sync to create it."
    elif not confirm_create(name, label):
        message = f"No {label} playlist link set, skipped."
    else:
        message = None
    if message:
        log.warning("[%s] %s", name, message)
        summary["errors"].append({"pair": name, "type": "missing_link", "message": message})
        return None
    if dry_run:
        log.info("[%s] would create a %s playlist", name, label)
        return None
    try:
        title = clients[source].get_playlist_name(pair[f"{source}_playlist_url"]) or name
        url = clients[target].create_playlist(title)
    except (AuthError, PlaylistNotFoundError):
        raise
    except Exception as e:
        log.error("[%s] Couldn't create the %s playlist: %s", name, label, e)
        summary["errors"].append({"pair": name, "type": "other", "message": f"Couldn't create the {label} playlist: {e}"})
        return None
    pair[f"{target}_playlist_url"] = url
    _save_new_link(name, target, url)
    summary["created"].append({"pair": name, "platform": label, "title": title})
    log.info("[%s] Created %s playlist '%s'.", name, label, title, extra={"feed": True})
    return f"{source}_to_{target}"


def _sync_pair(pair, direction, clients, config, summary, dry_run, confirm_create):
    name = pair["name"]
    if not (pair.get("spotify_playlist_url") and pair.get("anghami_playlist_url")):
        direction = _create_missing_playlist(pair, clients, confirm_create, summary, dry_run)
        if direction is None:
            return
    source, dest = directions[direction]
    log.info("Syncing pair: %s (%s)", name, direction_label(direction))
    _emit("pair_start", pair=name, source=platform_names[source], dest=platform_names[dest], dry_run=dry_run)
    retries = max(0, int(config.get("retry_attempts", 2)))
    for attempt in range(retries + 1):
        try:
            tracks = {}
            for platform in (source, dest):
                excluded = set(pair.get(f"excluded_{platform}", []))
                read = clients[platform].get_playlist_tracks(pair[f"{platform}_playlist_url"])
                tracks[platform] = [t for t in read if not is_excluded(t, excluded)]
            if not tracks[source]:
                # an empty read is far more likely a failed scrape than a playlist someone emptied
                log.error("[%s] %s read back 0 songs, likely a failed read. Skipping this pair this run.",
                          name, platform_names[source], extra={"feed": False})
                _emit("pair_end", pair=name, error=f"{platform_names[source]} read back no songs, so this pair "
                                                   "was skipped. Probably a failed read; it's tried again next sync.")
                return
            rows, questions = diff_and_apply(
                name, tracks[source], tracks[dest], clients[dest], config.get("match_threshold", 87),
                pair[f"{dest}_playlist_url"], platform_names[dest], dry_run,
                config.get("match_mode", "automatic"), platform_names[source])
            summary["questions"] = summary.get("questions", 0) + questions
            _emit("pair_end", pair=name, songs=rows, total=len(tracks[source]))
            return
        except AuthError as e:
            log.error("[%s] Auth problem: %s", name, e, extra={"feed": False})
            summary["errors"].append({"pair": name, "type": "auth", "message": str(e)})
            _emit("pair_end", pair=name, error=f"Login problem: {e}")
            return
        except PlaylistNotFoundError as e:
            log.error("[%s] Invalid playlist link: %s", name, e, extra={"feed": False})
            summary["errors"].append({"pair": name, "type": "invalid_link", "message": str(e)})
            _emit("pair_end", pair=name, error=f"Invalid playlist link: {e}")
            return
        except bsc.RateLimitedError as e:
            # not retried, and the run stops: every pair would hit the same limit
            log.error("[%s] %s", name, e, extra={"feed": False})
            summary["errors"].append({"pair": name, "type": "rate_limited", "message": str(e)})
            _emit("pair_end", pair=name, error=str(e))
            raise
        except Exception as e:
            if attempt < retries:
                log.warning("[%s] Sync attempt %d failed, retrying: %s", name, attempt + 1, e, extra={"feed": False})
                _emit("pair_note", pair=name, note=f"Attempt {attempt + 1} failed, trying again")
                continue
            log.exception("Sync failed for pair %s after %d attempt(s): %s", name, attempt + 1, e, extra={"feed": False})
            summary["errors"].append({"pair": name, "type": "other", "message": str(e)})
            _emit("pair_end", pair=name, error=f"Sync failed after {attempt + 1} attempt(s): {e}")


def main(direction=None, pair_names=None, dry_run=False, confirm_create=None):
    """one sync pass, every run goes through here: the buttons, the tray, both automatic syncs.
    pair_names None means all. confirm_create(pair_name, platform_label) is asked before creating a
    playlist for a blank link, without it (automatic runs) those pairs are skipped"""
    summary = {"busy": False, "no_pairs": False, "no_pairs_selected": False, "missing_chromium": False,
               "missing_sessions": [], "created": [], "errors": []}
    lock = sysutil.SyncLock()
    if not lock.acquire():
        log.warning("Another sync is already running, skipping this one.")
        summary["busy"] = True
        return summary
    _emit("run_start", dry_run=dry_run)
    try:
        _run(summary, direction, pair_names, dry_run, confirm_create)
    finally:
        lock.release()
        _emit("run_end", questions=summary.get("questions", 0))
    return summary


def _run(summary, direction, pair_names, dry_run, confirm_create):
    config = load_config()
    direction = direction or config.get("sync_direction", "spotify_to_anghami")
    if direction not in directions:
        log.error("Unknown sync direction: %s", direction)
        return
    pairs = config["playlist_pairs"]
    if not pairs:
        log.error("No playlist pairs configured. Add one on the Setup tab first.")
        summary["no_pairs"] = True
        return
    if pair_names is not None:
        pairs = [p for p in pairs if p["name"] in pair_names]
        if not pairs:
            log.error("No playlist pairs selected to sync.")
            summary["no_pairs_selected"] = True
            return
    missing = missing_sessions(config)
    if missing:
        log.error("Not logged in to %s (or the session expired). Use Get Sessions on the Setup tab.",
                  " and ".join(missing))
        summary["missing_sessions"] = missing
        return
    if not bsc.chromium_installed():
        log.error("The sync browser (Chromium) isn't installed. Install it on the Setup tab.")
        summary["missing_chromium"] = True
        return

    with sync_playwright() as p:
        with make_client("spotify", p, config) as sp, make_client("anghami", p, config) as ag:
            clients = {"spotify": sp, "anghami": ag}
            try:
                for pair in pairs:
                    _sync_pair(pair, direction, clients, config, summary, dry_run, confirm_create)
            except bsc.RateLimitedError:
                log.error("Stopped this sync run early so the account isn't pushed any further.")
    log.info("Sync run complete.")


def run_daemon(interval_minutes=30):
    # started once at log on. config.json is re-read every pass so changes in the window apply
    # without reinstalling
    sysutil.lower_own_priority()
    try:
        with open(pid_file, "w") as f:
            f.write(str(os.getpid()))
    except OSError:
        pass
    log.info("Background sync %s started (pid %d), syncing every %d minute(s).", version, os.getpid(), interval_minutes)
    while True:
        try:
            trim_log()
            config = load_config()
            reason = sysutil.pause_reason(config)
            if reason:
                log.info("Skipping scheduled sync: %s.", reason)
            else:
                main(pair_names=config.get("automation_pair_names"))
        except Exception:
            log.exception("Background sync pass failed")
        time.sleep(interval_minutes * 60)


def cli_int(flag, default):
    if flag in sys.argv:
        i = sys.argv.index(flag)
        if i + 1 < len(sys.argv):
            try:
                return int(sys.argv[i + 1])
            except ValueError:
                pass
    return default


if __name__ == "__main__":
    if "--version" in sys.argv:
        print(f"Scrapify {version}")
    elif "--daemon" in sys.argv:
        run_daemon(interval_minutes=cli_int("--interval", 30))
    else:
        main(dry_run="--dry-run" in sys.argv)
