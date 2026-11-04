# Scrapify

Keeps your playlists in sync between Spotify and Anghami.

Pick a playlist on each side and Scrapify makes one match the other, adding and removing songs until they're the same. You choose the direction (Spotify to Anghami, or Anghami to Spotify), and the source playlist is never touched. No developer accounts or API keys needed: it uses your own logins and drives the two web players in a hidden browser.

**Windows only for now.**

## Features

- One way sync in either direction, for as many playlist pairs as you want
- Recognizes the same song across Arabic, Franco (`3met 3en`) and English names
- Tells versions apart (live, remix, slowed) and won't swap one for another unless the exact one is missing
- Leave one link of a pair empty and the first sync creates that playlist for you
- Exclude songs you never want touched
- Sync on a timer while the app is open or in the tray, or in the background with the app closed
- Pauses automatic syncs while a game or any fullscreen app is running
- Dark mode

## Install

Download `Scrapify-<version>-setup.exe` from the [latest release](https://github.com/z6gg/Scrapify/releases/latest) and run it. It installs just for you, with no admin needed, into `%LOCALAPPDATA%\Programs\Scrapify`. It adds Scrapify to the Start menu (and to the desktop if you tick the box), and lists it in Settings > Apps.

**Or the portable way:** download `Scrapify-<version>-windows.zip`, unzip it anywhere and open `Scrapify.exe`. Keep the folder together; the exe needs the files next to it. Skip the `-update.zip`: that one is only for the app's own updater and has no Chromium.

Either way, everything it needs comes with it, including its own copy of Chromium.

**Scrapify isn't signed.** Windows SmartScreen will warn about an unknown app the first time you run the installer or the exe: click More info > Run anyway. Some antivirus programs may also flag it. Unsigned apps packed with PyInstaller get flagged often, and Scrapify reads your browser's saved logins when you use Get Sessions, which antivirus heuristics can mistake for something worse. If you'd rather not trust it, run it from source instead (below).

After that, Scrapify checks for a new version each time it opens and asks before updating itself. It shows what changed once it restarts. Most updates are small, since Chromium is only downloaded again when it changes. On the Settings tab you can turn the check off, check by hand, or let it install updates without asking.

## Setup

1. **Setup** tab: pick the browser you normally use and click **Get Sessions**. A private window opens; log into Spotify and Anghami there, then click OK in Scrapify. You only do this once, or again if a sync says your login expired.
2. Click **Add** and paste a Spotify playlist link and an Anghami playlist link.
3. **Sync** tab: click a direction. The log shows what got added and removed.

## Using It

- **Setup**: logins and playlist pairs. **Exclude Songs** marks songs Scrapify never adds, removes or matches.
- **Sync**: sync the selected pairs, with the log underneath.
- **Automation**: a timer sync while the app runs, or a background sync that starts with Windows and runs with the app closed. Use one or the other. Automatic syncs never pop up dialogs; problems show up in the log and as a tray notification.
- **Settings**: matching strictness, timeouts, retries, dark mode, debug files and browser flags.

Only one sync runs at a time, wherever it was started from.

## Your Files

Settings, logins and the log live in `%LOCALAPPDATA%\Scrapify`, not in the app folder, so updating means replacing the folder. **Open Folder** on the Settings tab takes you there.

`spotify_session.json` and `anghami_session.json` in there are your logins. Don't share them.

## Uninstall

**Installed:** uninstall Scrapify from Settings > Apps. It closes Scrapify and removes the start-with-Windows and background sync entries. At the end it asks whether to delete your settings and logins too.

**Portable:**

1. On the **Automation** tab, remove the background sync and turn off **Start with Windows** if you used them. Both add an entry that starts Scrapify when you log on.
2. Close Scrapify, including from the tray, and delete its folder.
3. To remove your settings, logins and log too, delete `%LOCALAPPDATA%\Scrapify`.

## How Matching Works

Songs are compared by title, artist and length. Anghami gives every song's name in both scripts, and when that's missing, both spellings get cut down to their consonants, so `Tamally Maak` and `تملي معاك` still match. If search doesn't find a song, the artist's own song list is checked too.

## Run from Source

Needs Python 3.10 or newer from python.org.

```
pip install -r requirements.txt
python -m playwright install chromium --no-shell
python gui.py
```

Without the window:

- `python getsessions.py` saves both logins
- `python main.py` runs one sync in the direction set in the app
- `python main.py --dry-run` logs what a sync would change, and changes nothing

A copy run from source doesn't update itself. Pull the latest code instead.

## Build

```
python buildexe.py
```

Builds `dist/Scrapify/` with Chromium inside (downloaded once, then reused) and zips it as `dist/Scrapify-<version>-windows.zip`, plus a smaller `Scrapify-<version>-update.zip` without Chromium for the in-app updater. With [Inno Setup 6](https://jrsoftware.org/isdl.php) installed, it also builds the installer, `Scrapify-<version>-setup.exe`, from `installer.iss`. The version comes from `version.py`, and changes are listed in `CHANGELOG.md`.

To release, bump `version.py`, add that version's section to `CHANGELOG.md` and push. GitHub Actions builds the installer and both zips and publishes the release, with that CHANGELOG section as its notes, which is also what the app shows after updating.

## Disclaimer

Scrapify isn't affiliated with, endorsed by or connected to Spotify or Anghami. It works by using their web players the same way your browser does, through requests they don't publish or support. That's very likely against their terms of service.

Use it on your own account and at your own risk. Either service could change its player and break Scrapify at any time, or restrict accounts that use tools like this. Your logins stay on your computer and are only ever sent to Spotify and Anghami themselves. The only other site Scrapify contacts is GitHub, to check for and download updates.

## License

GPLv3, see LICENSE.

---

*Human directed, made with Claude Opus 5.5.*
