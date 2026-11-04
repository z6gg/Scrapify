<p align="center">
  <img src="assets/demo.gif" alt="Scrapify demo" width="900">
</p>

<h1 align="center">Scrapify</h1>

<p align="center">
  Sync your playlists from Spotify to Anghami or vice versa <b>without Spotify Premium</b> via your own logins.
</p>

<hr>

<p align="center">
  <a href="https://github.com/z6gg/Scrapify/releases/latest/download/Scrapify-Setup.exe">
    <img src="assets/download.svg" alt="Download" width="240">
  </a>
</p>

<p align="center">
  <a href="https://github.com/z6gg/Scrapify/releases/latest"><img src="https://img.shields.io/github/v/release/z6gg/Scrapify?style=for-the-badge&label=version&color=a33ff0&labelColor=1b1d23" alt="Latest version"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-GPL--3.0-1ed760?style=for-the-badge&labelColor=1b1d23" alt="License"></a>
</p>

## Features

- One way sync in either direction, for as many playlist pairs as you want
- Matches the same song across Arabic, Franco (`3met 3en`) and English names (Arabic/Franco is not 100% accurate)
- Creates the other playlist on the first sync if you leave its link empty
- Exclude songs you never want touched
- Sync on a timer, or in the background with the app closed; pauses while a game or fullscreen app runs
- Updates itself, light and dark mode

## Install

Run [Scrapify-Setup.exe](https://github.com/z6gg/Scrapify/releases/latest/download/Scrapify-Setup.exe) and it installs the latest version for you.

Portable: download `Scrapify-<version>-windows-portable.zip` from the [latest release](https://github.com/z6gg/Scrapify/releases/latest), unzip it anywhere and open `Scrapify.exe`.

Scrapify isn't signed, so Windows may warn the first time: click **More info > Run anyway**. Some antivirus programs flag it too. If you'd rather not trust the exe, [run it from source](#run-from-source).

## Getting Started

1. **Setup** tab: pick your browser and click **Get Sessions**, log into Spotify and Anghami in the window that opens, then click OK. Needed once, or again if a sync says your login expired.
2. Click **Add** and paste a Spotify and an Anghami playlist link.
3. **Sync** tab: click a direction. The log shows what changed.

Right-click a pair for Edit, Exclude Songs and Remove. Automatic syncs are on the **Automation** tab, everything else on **Settings**.

Your settings, logins and log are in `%LOCALAPPDATA%\Scrapify` (**Settings > Open Folder**). The two `*_session.json` files there are your logins: don't share them.

## Uninstall

Setup: remove it from **Settings > Apps** in Windows. It asks whether to keep your settings and logins.

Portable: turn off the background sync and **Start with Windows** on the Automation tab, quit Scrapify from the tray, then delete its folder (and `%LOCALAPPDATA%\Scrapify` to remove your data).

## Run from Source

Needs Python 3.10 or newer.

```
pip install -r requirements.txt
python -m playwright install chromium --no-shell
python gui.py
```

`python main.py` runs one sync without the window, `--dry-run` only logs what would change. A source copy doesn't update itself.

## Build and Release

`python buildexe.py` builds `dist/Scrapify/` and its zips, and `Scrapify-Setup.exe` too if [Inno Setup 6](https://jrsoftware.org/isdl.php) is installed.

To release, bump `version.py`, add its section to `CHANGELOG.md` and push. GitHub Actions builds and publishes it, with that section as the release notes.

## Disclaimer

Scrapify isn't affiliated with Spotify or Anghami. It uses their web players through requests they don't publish or support, which is very likely against their terms. Use it on your own account and at your own risk: either service could break it or restrict accounts that use it. Your logins stay on your computer and only go to Spotify and Anghami; the only other site Scrapify contacts is GitHub, for updates.

## License

GPLv3, see [LICENSE](LICENSE).

---

*Human directed, made with Claude Opus 5.5.*
