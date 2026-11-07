# Changelog

## Unreleased

- Semi-automatic matching now also shows the closest song for each "possibly missing" song, with a tick to add it or a cross for each. Leave them unanswered and nothing waits on it; a later sync shows them again for whatever is still missing

## 1.4.0

- A Manage tab: see both accounts' playlists side by side with their songs and covers. Copy, move, remove and reorder songs, create, rename, duplicate and delete playlists, merge playlists, and make sync pairs from them. A Shortcuts page lists the keys
- The accent is now the logo's green to purple instead of the stock blue: buttons, links, badges, selected rows and ticks
- The title row has Scrapify's name beside the logo, with the tabs centered between it and the window buttons
- A loading bar shows wherever Scrapify waits: Exclude Songs, sync, the update check and download, the Chromium install and Get Sessions
- Songs one platform lists with a shortened title no longer pass as a match when their Arabic titles conflict
- Settings fall back to their defaults when a key is missing from the settings file
- The window can't be resized shorter than its content
- The Sync tab's log is now an Activity feed: a card per playlist pair with a one-line summary and every song's outcome (added, other version, possibly missing, needs your answer, removed, failed). Hover an icon to see what it means. Settings > Open Sync Log still has the full detail
- Settings > Matching > Judgement: Automatic, or Semi-automatic, where songs Scrapify isn't sure about wait on the Sync tab for a yes or no. Answers are remembered for every later sync
- Settings > Notifications: one switch for all of them, with "When a sync starts and finishes" and "Ask me when unsure" under it
- A small built-in model, trained on Spotify and Anghami's own names in both scripts, settles Arabic songs the rules weren't sure of. It runs offline and needs nothing extra
- A song one platform lists with its full title and the other with a shorter one now matches when their lengths agree (ليالي الشمال = Laialy El Shemal El Hazeena)
- Arabic names ending in a spoken-vowel ه now match their Latin spelling (محمد عبده = Mohammed Abdu, قالوا نسيته = Qalou Naseto)
- An Anghami playlist with one unavailable song no longer loses every song's length and Latin name, which made many Arabic songs fail to match
- Maximizing and then restoring the window no longer hides the tabs or leaves the window buttons stuck, and resizing is much smoother

## 1.3.3

- Settings > Request speed: Safer, Balanced (the default) or Fast. Syncs no longer send as many requests as possible at once
- If Spotify or Anghami says to slow down, Scrapify waits and tries once more, then stops the sync instead of pushing on

## 1.3.2

- Arabic songs match far more reliably between Spotify and Anghami: spellings that barely share letters ("El Layaly" and "Ellayali"), Gulf spellings ("Galo Tara" for قالوا ترى), and titles one side shortened
- A song with no artist no longer stops a whole sync from adding anything to Spotify
- Songs Anghami's search misses at first get searched again before they're reported as missing

## 1.3.1

- Notes and hints show in grey again, instead of the same color as the main text
- The Get Sessions note on the Setup tab fits on one line
- An About section at the bottom of Settings, with the version, the license and who made Scrapify

## 1.3.0

- Right-click a playlist pair to edit it, exclude songs, remove it or add a new one, the same as the buttons under the list
- The tabs sit centered between the logo and the window buttons, and the selected one is a rounded box around its name
- Everything in the top row lines up: logo, tabs and window buttons
- Pages have rounded corners, and spacing is even across every tab
- The add, update, what's new and Exclude Songs windows, and the pop-up messages, get the same title bar and look as the main window
- The tray icon's menu follows dark and light mode
- If a Windhawk mod is restyling Scrapify's windows, Scrapify explains the odd look once and shows how to exclude it, or switches to the Windows title bar for you

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
