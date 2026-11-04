# Changelog

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
