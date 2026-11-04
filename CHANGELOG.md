# Changelog

## 1.2.0

- New installer, Scrapify-Setup.exe: installs for just you with no admin, adds Scrapify to the Start menu (and the desktop if you like) and to Settings > Apps. It always downloads the newest version, and running it again replaces the old version with a clean copy
- Uninstalling from Settings > Apps closes Scrapify, removes its start-with-Windows and background sync entries, and asks whether to keep your settings and logins
- A fresh Windows 11 look, in light and dark, with blue buttons for the main actions. Dark mode now switches instantly, no restart
- Scrapify's own title bar: the logo and window buttons sit in the tab row like a browser, with rounded buttons that light up on hover. Prefer the usual one? Turn on "Use the Windows title bar" in Settings
- Updating is friendlier: see what's new before you update, follow the download with its size, speed and time left, and a small window covers the few seconds while Scrapify restarts
- Automatic updates count down 15 seconds before restarting, so you can choose Later and have it install when you quit instead
- Settings > Updates shows when it last checked and what it's doing

## 1.1.0

- Scrapify now updates itself. It checks for a new version when it opens and twice a day while it runs, asks before updating, and shows what changed after it restarts
- Option to install updates without asking, once no sync is running
- Check for Updates Now button on the Settings tab
- Updates skip Chromium unless it changed, so most are a small download
- If an update can't finish, the previous version is put back

## 1.0.0

First public release, Windows only.

- One way playlist sync between Spotify and Anghami, in either direction
- Matching across Arabic, Franco and English names
- Exclude songs, create a missing playlist on the first sync
- Timer sync while the app is open, or a background sync that runs with it closed
- Chromium ships inside the app folder, nothing else to install
- Runs without admin
- Settings, logins and the log live in `%LOCALAPPDATA%\Scrapify`
