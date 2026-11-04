"""
finds Windhawk mods that restyle Scrapify's window

Windhawk (windhawk.net) injects mods into other programs. Some restyle every window they reach:
the Translucent Windows mod makes backgrounds see-through and draws its own window buttons,
which on Scrapify's custom title bar look like a second, broken set. Nothing is wrong with
Scrapify then, so it says what's going on instead of leaving people confused.

the installed Windhawk keeps each mod under HKLM\\SOFTWARE\\Windhawk\\Engine\\Mods\\<id>, with
Disabled, and process lists (Include, Exclude, IncludeCustom, ExcludeCustom) of exe names or
paths, | separated, * and ? allowed
"""

import fnmatch
import os
import sys

mods_key = r"SOFTWARE\Windhawk\Engine\Mods"

# mods that change how other programs' windows look, by id, with the name Windhawk shows
known_mods = {
    "translucent-windows": "Translucent Windows",
    "hide-titlebar-elements": "Hide Titlebar Elements",
}
# other mods whose id says they restyle windows the same way
look_alike_words = ("translucent", "titlebar", "title-bar", "acrylic", "mica")


def _names(value):
    return [part.strip() for part in (value or "").split("|") if part.strip()]


def _matches(patterns, exe_path):
    name = os.path.basename(exe_path).lower()
    full = os.path.normcase(os.path.abspath(exe_path))
    for pattern in patterns:
        pattern = pattern.strip('"').lower()
        if "\\" in pattern or "/" in pattern:  # a path, else just an exe name
            if fnmatch.fnmatchcase(full, os.path.normcase(pattern)):
                return True
        elif fnmatch.fnmatchcase(name, pattern):
            return True
    return False


def _read(key, name):
    import winreg
    try:
        return winreg.QueryValueEx(key, name)[0]
    except OSError:
        return None


def applies_to(include, exclude, exe_path):
    return _matches(include, exe_path) and not _matches(exclude, exe_path)


def mods_affecting(exe_path=None):
    """
    display names of the enabled window styling mods that reach exe_path (this program by
    default). [] when there's no Windhawk, or nothing reaches it
    """
    if sys.platform != "win32":
        return []
    import winreg
    exe_path = exe_path or sys.executable
    found = []
    try:
        mods = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, mods_key, 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY)
    except OSError:
        return []
    with mods:
        index = 0
        while True:
            try:
                mod_id = winreg.EnumKey(mods, index)
            except OSError:
                break
            index += 1
            plain_id = mod_id.split("@")[-1].lower()  # local@... is a mod someone wrote themselves
            if plain_id not in known_mods and not any(w in plain_id for w in look_alike_words):
                continue
            try:
                with winreg.OpenKey(mods, mod_id) as mod:
                    if _read(mod, "Disabled"):
                        continue
                    include = _names(_read(mod, "Include")) + _names(_read(mod, "IncludeCustom"))
                    exclude = _names(_read(mod, "Exclude")) + _names(_read(mod, "ExcludeCustom"))
            except OSError:
                continue
            if applies_to(include, exclude, exe_path):
                found.append(known_mods.get(plain_id) or plain_id.replace("-", " ").title())
    return found
