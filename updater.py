"""
checks github for a newer release and updates the app folder in place

the app can't overwrite its own files while it runs, so the new version is unpacked into a
.update folder next to it and a small powershell script swaps it in once every scrapify
process from this folder has exited, then starts the new exe. settings and logins live in the
data folder and are never touched. most updates skip chromium, the biggest part of the app,
and only download the full zip when the new version needs a different chromium
"""

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
import urllib.request
import zipfile
from dataclasses import dataclass

import sysutil
from version import version

repo = "z6gg/Scrapify"
releases_api = f"https://api.github.com/repos/{repo}/releases?per_page=30"
releases_page = f"https://github.com/{repo}/releases/latest"
# written in the data folder right before the swap, read by the new version on its first launch
whats_new_file = "whats_new.json"
update_log = "update.log"
staging_name = ".update"
exe_name = "Scrapify.exe"


@dataclass
class Update:
    version: str
    notes: str  # every release newer than this one, newest first
    url: str
    size: int
    sha256: str  # empty when github didn't give one
    full: bool  # the whole app with chromium, not just the update zip


def parse_version(text):
    parts = []
    for part in str(text).strip().lstrip("vV").split(".")[:3]:
        digits = ""
        for ch in part:
            if not ch.isdigit():
                break
            digits += ch
        parts.append(int(digits or 0))
    return tuple((parts + [0, 0, 0])[:3])


def _get(url, timeout=15):
    request = urllib.request.Request(url, headers={"User-Agent": f"Scrapify/{version}",
                                                   "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def _browsers_dir():
    return os.path.join(sysutil.app_dir(), "browsers")


def _have_chromium(manifest_asset):
    # whether the chromium the new version wants is already in the app folder
    if not manifest_asset:
        return False
    try:
        needed = json.loads(_get(manifest_asset["browser_download_url"])).get("chromium") or []
        have = set(os.listdir(_browsers_dir()))
    except (OSError, ValueError, AttributeError):
        return False
    return bool(needed) and all(name in have for name in needed)


def check():
    """the newest release above this version, or None. raises OSError or ValueError when github can't be read"""
    releases = json.loads(_get(releases_api))
    current = parse_version(version)
    newer = [r for r in releases if isinstance(r, dict) and not r.get("draft") and not r.get("prerelease")
             and parse_version(r.get("tag_name", "")) > current]
    if not newer:
        return None
    newer.sort(key=lambda r: parse_version(r["tag_name"]), reverse=True)
    latest = newer[0]
    new_version = latest["tag_name"].lstrip("vV")
    assets = {a.get("name"): a for a in latest.get("assets") or []}
    full = assets.get(f"Scrapify-{new_version}-windows.zip")
    partial = assets.get(f"Scrapify-{new_version}-update.zip")
    if partial and _have_chromium(assets.get("update.json")):
        asset, is_full = partial, False
    elif full:
        asset, is_full = full, True
    else:
        return None  # still uploading, or a release without the app in it
    notes = "\n\n".join(f"Scrapify {r['tag_name'].lstrip('vV')}\n{(r.get('body') or '').strip()}".strip()
                        for r in newer)
    digest = asset.get("digest") or ""
    return Update(version=new_version, notes=notes, url=asset["browser_download_url"], size=asset.get("size") or 0,
                  sha256=digest.split(":", 1)[1] if digest.startswith("sha256:") else "", full=is_full)


def can_update():
    # only the built windows app updates itself, and only from a folder it can write to
    if not (sysutil.frozen and sysutil.on_windows):
        return False
    try:
        fd, path = tempfile.mkstemp(dir=sysutil.app_dir(), prefix=".write_test")
        os.close(fd)
        os.remove(path)
        return True
    except OSError:
        return False


def staging_dir():
    return os.path.join(sysutil.app_dir(), staging_name)


def clean_leftovers(min_age_seconds=0):
    # a finished or abandoned update leaves its folder behind when a file was still locked.
    # min_age_seconds leaves a swap that's happening right now alone
    stage = staging_dir()
    try:
        if time.time() - os.path.getmtime(stage) < min_age_seconds:
            return
    except OSError:
        return
    shutil.rmtree(stage, ignore_errors=True)


class Cancelled(Exception):
    pass


def download(update, progress=None, cancelled=None):
    """
    downloads and unpacks the update. returns the unpacked Scrapify folder.
    progress(stage, done, total) is called along the way, stage is "download" or "unpack"
    """
    clean_leftovers()
    stage = staging_dir()
    os.makedirs(stage, exist_ok=True)
    zip_path = os.path.join(stage, "update.zip")
    sha = hashlib.sha256()

    def check_cancel():
        if cancelled is not None and cancelled.is_set():
            raise Cancelled()

    request = urllib.request.Request(update.url, headers={"User-Agent": f"Scrapify/{version}"})
    with urllib.request.urlopen(request, timeout=30) as response, open(zip_path, "wb") as f:
        total = int(response.headers.get("Content-Length") or update.size or 0)
        done = 0
        while True:
            check_cancel()
            chunk = response.read(256 * 1024)
            if not chunk:
                break
            f.write(chunk)
            sha.update(chunk)
            done += len(chunk)
            if progress:
                progress("download", done, total)
    if total and done != total:
        raise OSError(f"The download stopped early ({done // 1_000_000} of {total // 1_000_000} MB). "
                      "Check your internet connection and try again.")
    if update.sha256 and sha.hexdigest() != update.sha256.lower():
        raise OSError("The download came through damaged. Try again.")
    unpacked = os.path.join(stage, "new")
    with zipfile.ZipFile(zip_path) as z:
        members = z.infolist()
        total = sum(m.file_size for m in members) or 1
        done = 0
        for member in members:
            check_cancel()
            z.extract(member, unpacked)
            done += member.file_size
            if progress:
                progress("unpack", done, total)
    os.remove(zip_path)
    app = os.path.join(unpacked, "Scrapify")
    if not os.path.isfile(os.path.join(app, exe_name)):
        raise OSError("The update doesn't contain Scrapify.exe.")
    return app


def other_app_processes():
    # every Scrapify.exe running from this folder except us: the background sync, a manual run
    mine = os.path.normcase(os.path.abspath(sysutil.app_dir()))
    found = []
    for proc in sysutil.processes_named(exe_name):
        try:
            exe = proc.info.get("exe") or ""
            if proc.pid != os.getpid() and os.path.normcase(os.path.dirname(os.path.abspath(exe))) == mine:
                found.append(proc)
        except (OSError, ValueError):
            pass
    return found


def _ps(text):
    return "'" + str(text).replace("'", "''") + "'"


_script = r"""
$ErrorActionPreference = 'Stop'
$app = __APP__
$new = __NEW__
$old = __OLD__
$stage = __STAGE__
$log = __LOG__
$data = __DATA__
$relaunch = __RELAUNCH__
function Log($m) { try { Add-Content -LiteralPath $log -Value "$(Get-Date -Format s) $m" -Encoding UTF8 } catch {} }

# a small window so there's something on screen between the app closing and opening again
$form = $null
try {
    Add-Type -AssemblyName System.Windows.Forms, System.Drawing
    [System.Windows.Forms.Application]::EnableVisualStyles()
    $form = New-Object System.Windows.Forms.Form
    $form.Text = 'Updating Scrapify'
    $form.FormBorderStyle = 'FixedDialog'
    $form.ControlBox = $false
    $form.StartPosition = 'CenterScreen'
    $form.TopMost = $true
    $form.ShowInTaskbar = $true
    $form.ClientSize = New-Object System.Drawing.Size(380, 110)
    $form.Font = New-Object System.Drawing.Font('Segoe UI', 9.5)
    try { $form.Icon = [System.Drawing.Icon]::ExtractAssociatedIcon((Join-Path $new '__EXE__')) } catch {}
    $title = New-Object System.Windows.Forms.Label
    $title.Text = 'Installing Scrapify __VERSION__...'
    $title.Font = New-Object System.Drawing.Font('Segoe UI', 11, [System.Drawing.FontStyle]::Bold)
    $title.AutoSize = $true
    $title.Location = New-Object System.Drawing.Point(18, 14)
    $sub = New-Object System.Windows.Forms.Label
    $sub.Text = $(if ($relaunch) { 'Scrapify will open again in a moment.' } else { 'It will be ready the next time you open Scrapify.' })
    $sub.AutoSize = $true
    $sub.Location = New-Object System.Drawing.Point(19, 42)
    $bar = New-Object System.Windows.Forms.ProgressBar
    $bar.Style = 'Marquee'
    $bar.MarqueeAnimationSpeed = 25
    $bar.Location = New-Object System.Drawing.Point(20, 72)
    $bar.Size = New-Object System.Drawing.Size(340, 18)
    $form.Controls.AddRange(@($title, $sub, $bar))
    $form.Show()
} catch { $form = $null }
function Pump { if ($form) { [System.Windows.Forms.Application]::DoEvents() } }
function Pause($ms) { $until = (Get-Date).AddMilliseconds($ms); while ((Get-Date) -lt $until) { Pump; Start-Sleep -Milliseconds 30 } }

foreach ($p in @(__PIDS__)) {
    $until = (Get-Date).AddSeconds(120)
    while ((Get-Process -Id $p -ErrorAction SilentlyContinue) -and (Get-Date) -lt $until) { Pause 100 }
}
Pause 500

$moved = @()
$ok = $true
try {
    New-Item -ItemType Directory -Force -Path $old | Out-Null
    foreach ($item in @(Get-ChildItem -LiteralPath $new -Force)) {
        $target = Join-Path $app $item.Name
        if (Test-Path -LiteralPath $target) {
            # a file can stay locked for a moment after its process exits
            for ($i = 0; ; $i++) {
                try { Move-Item -LiteralPath $target -Destination (Join-Path $old $item.Name) -Force; break }
                catch { if ($i -ge 30) { throw }; Pause 500 }
            }
            $moved += $item.Name
        }
        Pump
        Move-Item -LiteralPath $item.FullName -Destination $target
    }
    Log "updated to __VERSION__"
} catch {
    $ok = $false
    Log "update to __VERSION__ failed, putting the old version back: $_"
    foreach ($name in $moved) {
        $target = Join-Path $app $name
        if (Test-Path -LiteralPath $target) { Remove-Item -LiteralPath $target -Recurse -Force -ErrorAction SilentlyContinue }
        try { Move-Item -LiteralPath (Join-Path $old $name) -Destination $target } catch { Log "couldn't restore ${name}: $_" }
    }
}

if ($relaunch) {
    Start-Process -FilePath (Join-Path $app '__EXE__') -WorkingDirectory $data
    Pause 1500  # until the app's own window is up
}
if ($form) { $form.Close() }
if ($ok) { Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue }
"""


def apply(new_app, update, wait_pids, restart_background=None, relaunch=True):
    """
    starts the swap. the caller exits right after, the script waits for every pid in wait_pids.
    restart_background is the background sync's interval when it was running, so the new
    version can start it again. relaunch=False installs without opening the app afterwards
    """
    with open(whats_new_file, "w", encoding="utf-8") as f:
        json.dump({"version": update.version, "from": version, "notes": update.notes,
                   "restart_background": restart_background}, f, indent=2)
    stage = staging_dir()
    script = _script
    for key, value in (("__APP__", _ps(sysutil.app_dir())), ("__NEW__", _ps(new_app)),
                       ("__OLD__", _ps(os.path.join(stage, "old"))), ("__STAGE__", _ps(stage)),
                       ("__LOG__", _ps(os.path.join(sysutil.data_dir(), update_log))),
                       ("__DATA__", _ps(sysutil.data_dir())),
                       ("__PIDS__", ",".join(str(int(p)) for p in wait_pids) or ""),
                       ("__RELAUNCH__", "$true" if relaunch else "$false"),
                       ("__VERSION__", update.version), ("__EXE__", exe_name)):
        script = script.replace(key, value)
    # outside the staging folder so the script can delete that folder when it's done
    script_path = os.path.join(tempfile.gettempdir(), "scrapify_update.ps1")
    with open(script_path, "w", encoding="utf-8-sig") as f:  # the bom keeps non english paths intact
        f.write(script)
    subprocess.Popen(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden",
                      "-File", script_path], close_fds=True, cwd=tempfile.gettempdir(),
                     creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP)


def take_whats_new():
    """what the last update left for this launch, removed once read. None when there's nothing"""
    try:
        with open(whats_new_file, encoding="utf-8") as f:
            info = json.load(f)
    except (OSError, ValueError):
        return None
    try:
        os.remove(whats_new_file)
    except OSError:
        pass
    if not isinstance(info, dict):
        return None
    info["applied"] = parse_version(info.get("version", "")) == parse_version(version)
    return info
