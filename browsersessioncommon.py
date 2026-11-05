"""
logins for spotify and anghami, plus the browser syncs run in

login never automates anything: the person's own browser opens as a plain process on a private
profile (%LOCALAPPDATA%\\Scrapify\\LoginProfiles\\<browser>) so the sites show no captcha. once
they're logged in the session is read back and saved as a storage_state file
"""

import base64
import codecs
import glob
import hashlib
import json
import logging
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import sysutil


class AuthError(Exception):
    """logged out, expired, or a captcha wall"""


class PlaylistNotFoundError(Exception):
    """logged in, but the link doesn't lead to a readable playlist"""


class RateLimitedError(Exception):
    """the site kept answering "too many requests", so the sync stops instead of pushing on"""


def rate_limited_message(service):
    return (f"{service} is limiting how fast Scrapify can make requests, so this sync stopped to protect your "
            "account. Try again later, or pick a gentler request speed in Settings.")


# requests at once, seconds between batches, seconds before searching again for songs not found
request_speeds = {"gentle": (2, 1.0, 10), "balanced": (3, 0.4, 5), "fast": (6, 0.0, 0)}
default_request_speed = "balanced"
rate_limit_wait = 30  # when "too many requests" doesn't say how long to wait
rate_limit_max_wait = 120

exe_cache_file = "browser_paths.json"

chromium_engine = "chromium"
gecko_engine = "gecko"

chromium_browsers = {
    "chrome": [r"%PROGRAMFILES%\Google\Chrome\Application\chrome.exe",
               r"%PROGRAMFILES(X86)%\Google\Chrome\Application\chrome.exe",
               r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"],
    "brave": [r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\Application\brave.exe",
              r"%PROGRAMFILES%\BraveSoftware\Brave-Browser\Application\brave.exe"],
    # helium ships a chrome.exe too, only its folder says helium
    "helium": [r"%LOCALAPPDATA%\Helium\Application\chrome.exe",
               r"%PROGRAMFILES%\Helium\Application\chrome.exe"],
}
firefox_browsers = {
    "firefox": [r"%PROGRAMFILES%\Mozilla Firefox\firefox.exe",
                r"%PROGRAMFILES(X86)%\Mozilla Firefox\firefox.exe"],
    "zen": [r"%LOCALAPPDATA%\zen\zen.exe", r"%PROGRAMFILES%\Zen Browser\zen.exe"],
}
browser_names = {"chrome": "Chrome", "brave": "Brave", "helium": "Helium",
                 "firefox": "Firefox", "zen": "Zen", "other": "Other"}
browser_order = ["chrome", "brave", "helium", "firefox", "zen", "other"]
exe_names = {"chrome": "chrome.exe", "brave": "brave.exe", "helium": "chrome.exe",
             "firefox": "firefox.exe", "zen": "zen.exe"}
_search_roots = [r"%LOCALAPPDATA%", r"%LOCALAPPDATA%\Programs", r"%PROGRAMFILES%", r"%PROGRAMFILES(X86)%"]

login_targets = [("Spotify", "https://accounts.spotify.com/en/login", "spotify.com"),
                 ("Anghami", "https://play.anghami.com/login", "anghami.com")]

default_browser_flags = {
    "disable_gpu": True,
    "mute_audio": True,
    "disable_extensions": True,
    "disable_background_networking": True,
    "disable_default_apps": True,
    "skip_first_run": True,
}
_browser_flag_args = {
    "disable_gpu": "--disable-gpu",
    "mute_audio": "--mute-audio",
    "disable_extensions": "--disable-extensions",
    "disable_background_networking": "--disable-background-networking",
    "disable_default_apps": "--disable-default-apps",
    "skip_first_run": "--no-first-run",
}
_hide_webdriver_js = "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
# cover art and audio are never read. anghami's cdn stays unblocked or its player reloads itself
_heavy_assets = re.compile(
    r"^https://(i\.scdn\.co|mosaic\.scdn\.co|image-cdn-[a-z0-9-]+\.spotifycdn\.com)/"
    r"|\.(png|jpe?g|webp|gif|avif|mp3|mp4|m4a|webm)(\?|$)",
    re.IGNORECASE,
)

# cookies that only exist once someone is signed in
signin_cookies = {"Spotify": {"sp_dc"}, "Anghami": {"sss", "ssss", "resss", "appsidsave"}}
signin_storage_keys = {"Anghami": {"user"}}  # anghami keeps the profile in localStorage too

_fetch_many_js = """async ([requests, parallel]) => {
    const out = new Array(requests.length);
    let next = 0;
    const worker = async () => {
        while (next < requests.length) {
            const i = next++;
            try {
                const r = await fetch(requests[i][0], requests[i][1]);
                out[i] = [r.status, await r.text(), r.headers.get('retry-after')];
            } catch (e) {
                out[i] = [0, String(e), null];
            }
        }
    };
    await Promise.all(Array.from({length: Math.min(parallel, requests.length)}, worker));
    return out;
}"""


# ------------------------------------------------------------ sync browser

def _playwright_browsers_dir():
    custom = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if custom:
        return custom
    if sysutil.on_windows:
        return os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "ms-playwright")
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Caches/ms-playwright")
    return os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"), "ms-playwright")


def chromium_installed():
    # reads the revision playwright wants from its own manifest instead of starting its driver.
    # an install only writes INSTALLATION_COMPLETE once it finished
    import playwright
    manifest = Path(playwright.__file__).parent / "driver" / "package" / "browsers.json"
    try:
        browsers = json.loads(manifest.read_text(encoding="utf-8"))["browsers"]
        revision = next(b["revision"] for b in browsers if b["name"] == "chromium")
    except (OSError, ValueError, KeyError, StopIteration):
        revision = None
    if revision and os.environ.get("PLAYWRIGHT_BROWSERS_PATH") != "0":
        return os.path.isfile(os.path.join(_playwright_browsers_dir(), f"chromium-{revision}", "INSTALLATION_COMPLETE"))
    from playwright.sync_api import sync_playwright
    try:
        with sync_playwright() as p:
            return os.path.exists(p.chromium.executable_path)
    except Exception:
        return False


def install_chromium(log, live=None):
    """playwright install chromium through the bundled driver, the built app has no python to call"""
    from playwright._impl._driver import compute_driver_executable, get_driver_env
    node, cli = compute_driver_executable()
    # syncs only use the full build, the headless shell would be another 100MB for nothing
    process = subprocess.Popen([node, cli, "install", "chromium", "--no-shell"], env=get_driver_env(),
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **sysutil.no_console_kwargs())
    # raw bytes: progress redraws itself with \r, which text mode would swallow
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    buf, last_live = "", 0.0
    while True:
        raw = process.stdout.read(1024)
        if not raw:
            break
        for ch in decoder.decode(raw):
            if ch not in "\r\n":
                buf += ch
                continue
            text, buf = buf.strip(), ""
            if not text:
                continue
            if ch == "\n":
                log(text)
            if live and time.time() - last_live > 0.2:
                live(text)
                last_live = time.time()
    if buf.strip():
        log(buf.strip())
    process.wait()
    log(f"Chromium install finished with code {process.returncode}")
    return process.returncode == 0


def launch_sync_browser(playwright, headless=True, optimization_flags=None):
    # the full build even headless: anghami answers playwright's headless shell with a captcha
    flags = optimization_flags or {}
    args = ["--disable-blink-features=AutomationControlled"] + [
        arg for key, arg in _browser_flag_args.items() if flags.get(key, default_browser_flags[key])]
    return playwright.chromium.launch(headless=headless, channel="chromium", args=args)


def _desktop_user_agent(version="149"):
    # headless says HeadlessChrome in its UA, which anghami refuses
    major = (version or "149").split(".")[0]
    if sys.platform == "darwin":
        system = "Macintosh; Intel Mac OS X 10_15_7"
    elif sys.platform.startswith("linux"):
        system = "X11; Linux x86_64"
    else:
        system = "Windows NT 10.0; Win64; x64"
    return f"Mozilla/5.0 ({system}) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36"


def new_sync_context(browser, storage_state_file, headless=True, log=print, block_assets=True):
    kwargs = {"user_agent": _desktop_user_agent(browser.version)} if headless else {}
    context = _context_with_session(browser, storage_state_file, log, **kwargs)
    context.add_init_script(_hide_webdriver_js)
    if block_assets:
        context.route(_heavy_assets, lambda route: route.abort())
    return context


def _context_with_session(browser, storage_state_file, log=print, **kwargs):
    # chromium rejects the whole batch over one bad cookie, so retry one by one and skip the bad ones
    try:
        return browser.new_context(storage_state=storage_state_file, **kwargs)
    except Exception as e:
        if "cookie" not in str(e).lower():
            raise
        log(f"The browser rejected a saved cookie ({e}), retrying without the bad ones.")
    state = json.loads(Path(storage_state_file).read_text(encoding="utf-8"))
    context = browser.new_context(**kwargs)
    skipped = []
    for cookie in state.get("cookies", []):
        try:
            context.add_cookies([cookie])
        except Exception as e:
            skipped.append(f"{cookie.get('name', '?')} ({e})")
    if skipped:
        log("Skipped cookies the browser wouldn't accept:\n  " + "\n  ".join(skipped))
    return context


def retry_after_seconds(values):
    # the longest wait any "too many requests" answer asked for, within reason
    asked = []
    for v in values:
        try:
            asked.append(float(v))
        except (TypeError, ValueError):
            pass
    return min(max(asked), rate_limit_max_wait) if asked else rate_limit_wait


def fetch_many(page, requests, parallel=6, pause=0.0, service="The site", log=None):
    """[(status, text)] for [(url, fetch options)], run from inside the page `parallel` at a time
    with `pause` seconds between batches. "too many requests" (429) waits, then retries those one
    at a time; a second 429 raises RateLimitedError rather than keep pushing"""
    if not requests:
        return []
    out = []
    for start in range(0, len(requests), parallel):
        if start and pause:
            time.sleep(pause)
        out += page.evaluate(_fetch_many_js, [[list(r) for r in requests[start:start + parallel]], parallel])
    limited = [i for i, r in enumerate(out) if r[0] == 429]
    if limited:
        wait = retry_after_seconds([out[i][2] for i in limited])
        (log or logging.getLogger(__name__)).warning(
            "%s asked Scrapify to slow down, waiting %d seconds before carrying on.", service, wait)
        time.sleep(wait)
        for n, i in enumerate(limited):
            if n:
                time.sleep(max(pause, 1.0))
            out[i] = page.evaluate(_fetch_many_js, [[list(requests[i])], 1])[0]
            if out[i][0] == 429:
                raise RateLimitedError(rate_limited_message(service))
    return [(r[0], r[1]) for r in out]


class BrowserClient:
    """what both platform clients share: a sync browser with the saved session loaded"""

    session_file = None
    block_assets = True
    service = "The site"

    def __init__(self, playwright=None, storage_state_file=None, headless=True,
                 debug_dumps=False, timeout_multiplier=1.0, optimization_flags=None,
                 request_speed=default_request_speed):
        # pass a running sync_playwright() to share it between both clients (one per thread)
        self.storage_state_file = storage_state_file or self.session_file
        self._parallel, self._pause, self.recheck_wait = request_speeds.get(
            request_speed, request_speeds[default_request_speed])
        self.headless = headless
        self.debug_dumps = debug_dumps
        self.timeout_multiplier = timeout_multiplier
        self.optimization_flags = optimization_flags
        self.log = logging.getLogger(self.__module__)
        self._playwright = playwright
        self._owns_playwright = playwright is None
        self._browser = self._context = None

    def _t(self, ms):
        return int(ms * self.timeout_multiplier)

    def _fetch_many(self, page, requests):
        return fetch_many(page, requests, self._parallel, self._pause, self.service, self.log)

    def _back_off(self, retry_after):
        # one "too many requests" on a single call: wait as asked, the caller retries once
        wait = retry_after_seconds([retry_after])
        self.log.warning("%s asked Scrapify to slow down, waiting %d seconds before carrying on.", self.service, wait)
        time.sleep(wait)

    def _wait_between_rounds(self):
        if self.recheck_wait:
            time.sleep(self.recheck_wait)

    def __enter__(self):
        if self._owns_playwright:
            from playwright.sync_api import sync_playwright
            self._playwright = sync_playwright().start()
        self._browser = launch_sync_browser(self._playwright, self.headless, self.optimization_flags)
        self._context = new_sync_context(self._browser, self.storage_state_file, self.headless,
                                         log=self.log.warning, block_assets=self.block_assets)
        return self

    def __exit__(self, *exc):
        try:
            self._context.close()
            self._browser.close()
        finally:
            if self._owns_playwright:
                self._playwright.stop()

    def _dump(self, page, name):
        if not self.debug_dumps:
            return
        try:
            page.screenshot(path=f"{name}.png", full_page=True)
            Path(f"{name}.html").write_text(page.content(), encoding="utf-8")
        except Exception:
            pass


# ---------------------------------------------------------------- sessions

def session_state_logged_in(label, state):
    """true if a storage_state (or plain cookie list) has a live sign in cookie or storage key"""
    wanted = signin_cookies.get(label)
    if not wanted or not state:
        return bool(state)
    cookies = state.get("cookies", []) if isinstance(state, dict) else state
    now = time.time()
    if any(c.get("name") in wanted and c.get("value")
           and (c.get("expires", -1) in (-1, None) or c["expires"] <= 0 or c["expires"] > now)
           for c in cookies):
        return True
    keys = signin_storage_keys.get(label, set())
    origins = state.get("origins", []) if isinstance(state, dict) else []
    return any(item.get("name") in keys and len(item.get("value") or "") > 20
               for o in origins for item in o.get("localStorage", []))


def session_file_logged_in(label, path):
    try:
        return session_state_logged_in(label, json.loads(Path(path).read_text(encoding="utf-8")))
    except Exception:
        return False


def save_session(label, path, state):
    """"signed_in", "unconfirmed", or "kept" when it doesn't look signed in and the saved one does"""
    if isinstance(state, list):
        state = {"cookies": state, "origins": []}
    signed_in = session_state_logged_in(label, state)
    if not signed_in and session_file_logged_in(label, path):
        return "kept"
    Path(path).write_text(json.dumps(state, indent=2), encoding="utf-8")
    return "signed_in" if signed_in else "unconfirmed"


def login_profile_dir(browser_key):
    return os.path.join(sysutil.data_dir(), "LoginProfiles", browser_key)


def browser_engine(browser_key, exe_path=None):
    if browser_key in chromium_browsers:
        return chromium_engine
    if browser_key in firefox_browsers:
        return gecko_engine
    name = os.path.basename(exe_path or "").lower()
    if any(fork in name for fork in ("firefox", "zen", "librewolf", "waterfox", "floorp")):
        return gecko_engine
    return chromium_engine


def browser_display_name(browser_key, exe=None):
    # "Helium", never "chrome.exe"
    if browser_key != "other":
        return browser_names[browser_key]
    return sysutil.friendly_app_name(exe) if exe else "your browser"


# -------------------------------------------------------------- find a browser

def _load_exe_cache():
    try:
        return json.loads(Path(exe_cache_file).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def register_manual_exe(browser_key, exe_path):
    cache = _load_exe_cache()
    cache[browser_key] = exe_path
    try:
        Path(exe_cache_file).write_text(json.dumps(cache, indent=2), encoding="utf-8")
    except OSError:
        pass
    return exe_path


def _belongs_to(browser_key, path):
    lower = path.lower()
    if browser_key == "helium":
        return "helium" in lower
    if browser_key == "chrome":
        return "helium" not in lower
    return True


def _registry_app_path(exe_name):
    if not sysutil.on_windows:
        return None
    import winreg
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(hive, rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{exe_name}") as key:
                path = os.path.expandvars(winreg.QueryValue(key, None).strip('"'))
                if os.path.isfile(path):
                    return path
        except OSError:
            pass
    return None


def find_browser_exe(browser_key):
    # last used path, usual install folders, App Paths, a running copy, then a shallow search
    cached = _load_exe_cache().get(browser_key)
    if isinstance(cached, str) and os.path.isfile(cached):
        return cached
    exe_name = exe_names[browser_key]
    candidates = [os.path.expandvars(p) for p in chromium_browsers.get(browser_key, firefox_browsers.get(browser_key, []))]
    candidates.append(_registry_app_path(exe_name))
    candidates += sysutil.exe_paths_named(exe_name)
    for root in _search_roots:
        root = os.path.expandvars(root)
        for pattern in (rf"*\Application\{exe_name}", rf"*\*\Application\{exe_name}", rf"*\{exe_name}"):
            candidates += glob.glob(os.path.join(root, pattern))
    for path in candidates:
        if path and os.path.isfile(path) and _belongs_to(browser_key, path):
            return register_manual_exe(browser_key, path)
    return None


def resolve_exe(typed):
    """a path, or the process name of a browser that's open right now"""
    typed = typed.strip().strip('"')
    if os.path.isfile(typed):
        return typed
    running = sysutil.exe_paths_named(typed if typed.lower().endswith(".exe") else typed + ".exe")
    return running[0] if running else None


def is_browser_running(exe):
    # by full path, so an open chrome doesn't count as helium
    target = os.path.normcase(os.path.abspath(exe))
    return any(os.path.normcase(p) == target for p in sysutil.exe_paths_named(os.path.basename(exe)))


# ------------------------------------------------------------------- login

def _wait_for_browser_to_close(exe, log=print, timeout=20):
    # a second launch of an open browser joins the open window and ignores our profile
    if not is_browser_running(exe):
        return True
    name = browser_display_name("other", exe)
    log(f"{name} is open. Close all of its windows so the private login window can open on its own.")
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not is_browser_running(exe):
            return True
        time.sleep(1)
    log(f"{name} still seems to be open, trying anyway.")
    return False


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def launch_plain_login(exe, profile_dir, engine, urls, log=print, wait_for_user=None, debug_port=None):
    # no automation flags, and the debug port isn't touched until they confirm
    os.makedirs(profile_dir, exist_ok=True)
    _wait_for_browser_to_close(exe, log=log)
    if engine == gecko_engine:
        args = [exe, "-profile", profile_dir, "-no-remote", "-new-instance", *urls]
    else:
        # background mode keeps the profile locked after the window closes
        args = [exe, f"--user-data-dir={profile_dir}", "--no-first-run", "--no-default-browser-check",
                "--disable-background-mode"]
        if debug_port:
            args.append(f"--remote-debugging-port={debug_port}")
        args += list(urls)
    log("Opening a private login window, separate from your normal browser. "
        "Log into Spotify and Anghami there, leave it open, then confirm.")
    try:
        subprocess.Popen(args, **sysutil.no_console_kwargs())
    except Exception as e:
        log(f"Couldn't open the browser: {e}")
        return False
    if wait_for_user is not None:
        wait_for_user()
    else:
        input("Press Enter here once you're logged into both (leave the window open): ")
    return True


def _secure_prefix_fix(cookie):
    # __Secure- and __Host- cookies must be secure or chromium rejects the whole batch
    if cookie.get("name", "").startswith(("__Secure-", "__Host-")):
        cookie["secure"] = True
    return cookie


def _split_state(cookies, origins, targets):
    """{label: storage_state} from one cookie list and playwright style origins"""
    results = {}
    for label, _url, domain in targets:
        mine = [_secure_prefix_fix(c) for c in cookies if domain in c.get("domain", "")]
        theirs = [o for o in origins if domain in o.get("origin", "")]
        results[label] = {"cookies": mine, "origins": theirs} if mine else None
    return results


def read_state_live(port, targets, log=print):
    # from the still open login window over its debug port. chromium writes cookies to disk lazily
    # and some sign in cookies never get there, so this beats reading the profile
    from playwright.sync_api import sync_playwright
    try:
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}", timeout=10000)
            context = browser.contexts[0]
            cookies, storage = context.cookies(), {}

            def collect(page):
                try:
                    storage.setdefault(page.evaluate("location.origin"), {}).update(
                        dict(page.evaluate("Object.entries(localStorage)")))
                except Exception:
                    pass

            for page in context.pages:
                collect(page)
            for _label, url, domain in targets:
                if not any(domain in origin for origin in storage):
                    page = context.new_page()
                    try:
                        page.goto(url, wait_until="domcontentloaded", timeout=15000)
                        collect(page)
                    except Exception:
                        pass
            try:
                browser.new_browser_cdp_session().send("Browser.close")
            except Exception:
                pass
    except Exception as e:
        log(f"Couldn't read the login window directly ({e}), reading its saved files instead.")
        return None
    origins = [{"origin": o, "localStorage": [{"name": k, "value": v} for k, v in items.items()]}
               for o, items in storage.items()]
    return _split_state(cookies, origins, targets)


def read_state_chromium_playwright(exe, profile_dir, targets, log=print):
    # reopens the profile headless in their own browser, which decrypts its own cookies
    # (app bound encryption too)
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        try:
            context = p.chromium.launch_persistent_context(
                profile_dir, executable_path=exe, headless=True, user_agent=_desktop_user_agent(),
                args=["--no-first-run", "--no-default-browser-check", "--disable-background-mode"])
        except Exception as e:
            log(f"Couldn't reopen the private profile headless ({e}).")
            return None
        try:
            for _label, url, _domain in targets:
                page = context.new_page()
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=20000)
                    page.wait_for_timeout(1500)
                except Exception:
                    pass
            state = context.storage_state()
        finally:
            try:
                context.close()
            except Exception:
                pass
    return _split_state(state.get("cookies", []), state.get("origins", []), targets)


def _copy_sqlite(src, log=print, timeout=8):
    # fresh cookies may still sit in the wal, so it comes along
    tmp = os.path.join(tempfile.gettempdir(), f"scrapify_{os.getpid()}_{os.path.basename(src)}.sqlite")
    deadline, last = time.time() + timeout, None
    while time.time() < deadline:
        try:
            shutil.copy2(src, tmp)
            for suffix in ("-wal", "-shm"):
                if os.path.isfile(src + suffix):
                    shutil.copy2(src + suffix, tmp + suffix)
            return tmp
        except OSError as e:
            last = e
            time.sleep(0.4)
    log(f"Cookie database still locked ({last}), the browser may still be closing. Try again in a moment.")
    return None


def _remove_sqlite_copy(tmp):
    for path in (tmp, tmp + "-wal", tmp + "-shm"):
        try:
            os.remove(path)
        except OSError:
            pass


def _read_cookie_rows(db_path, query, targets, make_cookie, log):
    tmp = _copy_sqlite(db_path, log)
    if not tmp:
        return {label: None for label, _u, _d in targets}
    try:
        conn = sqlite3.connect(tmp)
        results = {}
        for label, _url, domain in targets:
            cookies = []
            for row in conn.execute(query, (f"%{domain}%",)).fetchall():
                cookie = make_cookie(row)
                if cookie:
                    cookies.append(_secure_prefix_fix(cookie))
            results[label] = cookies or None
            log(f"{label}: {'found session' if cookies else 'no cookies found'}")
        conn.close()
        return results
    finally:
        _remove_sqlite_copy(tmp)


def _chromium_key(profile_dir):
    # None when app bound encryption guards the key instead of plain DPAPI
    data = json.loads(Path(profile_dir, "Local State").read_text(encoding="utf-8"))
    encrypted = base64.b64decode(data["os_crypt"]["encrypted_key"])
    if not encrypted.startswith(b"DPAPI"):
        return None
    import win32crypt
    return win32crypt.CryptUnprotectData(encrypted[5:], None, None, None, 0)[1]


def _decrypt_chromium_value(key, value, host):
    if not value:
        return ""
    if value[:3] in (b"v10", b"v11"):
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        plain = AESGCM(key).decrypt(value[3:15], value[15:], None)
        if len(plain) > 32 and plain[:32] == hashlib.sha256(host.encode()).digest():
            plain = plain[32:]  # chrome 130+ prefixes a hash of the host
        return plain.decode("utf-8", "replace")
    import win32crypt
    return win32crypt.CryptUnprotectData(value, None, None, None, 0)[1].decode("utf-8", "replace")


def _chrome_time_to_unix(expires_utc):
    # microseconds since 1601, 0 is a session cookie
    if not expires_utc or expires_utc <= 0:
        return -1
    seconds = expires_utc / 1_000_000 - 11644473600
    return int(seconds) if seconds > 0 else -1


def read_cookies_chromium_disk(profile_dir, targets, log=print):
    try:
        key = _chromium_key(profile_dir)
    except Exception as e:
        log(f"Couldn't read this profile's encryption key ({e}).")
        return None
    db = next((p for p in (os.path.join(profile_dir, "Default", "Network", "Cookies"),
                           os.path.join(profile_dir, "Default", "Cookies")) if os.path.isfile(p)), None)
    if key is None or db is None:
        return None

    def make_cookie(row):
        name, enc, host, path, expires, secure, httponly = row
        try:
            value = _decrypt_chromium_value(key, enc, host)
        except Exception:
            return None
        return {"name": name, "value": value, "domain": host, "path": path,
                "expires": _chrome_time_to_unix(expires), "httpOnly": bool(httponly),
                "secure": bool(secure), "sameSite": "Lax"}

    return _read_cookie_rows(db, "SELECT name, encrypted_value, host_key, path, expires_utc, is_secure, is_httponly "
                                 "FROM cookies WHERE host_key LIKE ?", targets, make_cookie, log)


def read_cookies_firefox_profile(profile_dir, targets, log=print):
    db = os.path.join(profile_dir, "cookies.sqlite")
    if not os.path.isfile(db):
        return {label: None for label, _u, _d in targets}

    def make_cookie(row):
        name, value, host, path, expiry, secure, httponly = row
        return {"name": name, "value": value, "domain": host, "path": path or "/", "expires": expiry or -1,
                "httpOnly": bool(httponly), "secure": bool(secure), "sameSite": "Lax"}

    return _read_cookie_rows(db, "SELECT name, value, host, path, expiry, isSecure, isHttpOnly "
                                 "FROM moz_cookies WHERE host LIKE ?", targets, make_cookie, log)


def grab_sessions(browser_key, exe, targets=login_targets, log=print, wait_for_user=None):
    """opens a login window, returns {label: storage_state or None}. states that don't look signed
    in come back too so the caller decides"""
    engine = browser_engine(browser_key, exe)
    profile_dir = login_profile_dir(browser_key)
    port = _free_port() if engine == chromium_engine else None
    if not launch_plain_login(exe, profile_dir, engine, [url for _l, url, _d in targets],
                              log=log, wait_for_user=wait_for_user, debug_port=port):
        return {}

    states = read_state_live(port, targets, log) if port else None
    if states is not None:
        time.sleep(1)  # let it shut down on its own first
    if sysutil.kill_processes_using(profile_dir):
        time.sleep(0.5)  # files unlock a moment after
    if states is None:
        if engine == gecko_engine:
            cookies = read_cookies_firefox_profile(profile_dir, targets, log)
        else:
            states = read_state_chromium_playwright(exe, profile_dir, targets, log)
            cookies = None if states is not None else read_cookies_chromium_disk(profile_dir, targets, log)
        if states is None:
            states = {label: ({"cookies": c, "origins": []} if c else None) for label, c in (cookies or {}).items()}

    for label, _url, _domain in targets:
        state = states.get(label)
        if not state:
            log(f"{label}: nothing captured. Make sure you're logged in before confirming.")
        elif session_state_logged_in(label, state):
            log(f"{label}: signed in.")
        else:
            names = ", ".join(sorted({c["name"] for c in state["cookies"]}))
            log(f"{label}: saved, but no sign in cookie found (cookies: {names}).")
    return states


def choose_browser():
    print("Which browser do you want to log in with?")
    for i, key in enumerate(browser_order, 1):
        print(f"  {i}. {browser_names[key]}")
    try:
        return browser_order[int(input("> ").strip()) - 1]
    except (ValueError, IndexError):
        return "other"
