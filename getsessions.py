"""
command line login, same as the app's Get Sessions button

    python getsessions.py
"""

import time

from main import load_config, platform_names  # also moves us into the data folder

import browsersessioncommon as bsc

one_year = 60 * 60 * 24 * 365


def manual_spotify():
    print("\nSpotify: DevTools (F12) > Application > Cookies > open.spotify.com, copy the sp_dc value.")
    sp_dc = input("sp_dc (Enter to skip): ").strip()
    if not sp_dc:
        return None
    return [{"name": "sp_dc", "value": sp_dc, "domain": ".spotify.com", "path": "/",
             "expires": int(time.time()) + one_year, "httpOnly": True, "secure": True, "sameSite": "Lax"}]


def manual_anghami():
    print("\nAnghami: DevTools (F12) > Network > any request to anghami.com > copy the Cookie header.")
    raw = input("Cookie header (Enter to skip): ").strip()
    cookies = []
    for part in raw.split(";"):
        if "=" in part:
            name, value = part.strip().split("=", 1)
            cookies.append({"name": name, "value": value, "domain": ".anghami.com", "path": "/",
                            "expires": int(time.time()) + one_year, "httpOnly": False, "secure": True, "sameSite": "Lax"})
    return cookies or None


manual_entry = {"Spotify": manual_spotify, "Anghami": manual_anghami}


def pick_browser():
    key = bsc.choose_browser()
    exe = bsc.find_browser_exe(key) if key != "other" else None
    if exe:
        return key, exe
    print(f"\nCouldn't find {bsc.browser_display_name(key)} on its own.")
    typed = input("Its .exe path, or its process name while it's open (Enter to type cookies instead): ")
    exe = bsc.resolve_exe(typed) if typed.strip() else None
    if typed.strip() and not exe:
        print("Couldn't find that browser.")
    return key, exe and bsc.register_manual_exe(key, exe)


def main():
    config = load_config()
    key, exe = pick_browser()
    results = {}
    if exe:
        print(f"\nOpening a private {bsc.browser_display_name(key, exe)} window for Spotify and Anghami.")
        try:
            results = bsc.grab_sessions(key, exe, log=print)
        except Exception as e:
            print(f"Couldn't read the sessions: {e}")
    for platform, label in platform_names.items():
        state = results.get(label)
        if not state:
            print(f"\n{label} wasn't captured, enter it by hand instead.")
            state = manual_entry[label]()
        if not state:
            print(f"{label}: skipped.")
            continue
        path = config[platform]["storage_state_file"]
        outcome = bsc.save_session(label, path, state)
        print(f"{label}: not signed in, kept the working session already saved." if outcome == "kept"
              else f"{label}: saved to {path}.")
    print("\nRun python main.py to sync.")


if __name__ == "__main__":
    main()
