"""
builds the windows app into dist/Scrapify/ with chromium inside, then zips it for a release

    python buildexe.py
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import zipfile

from version import version

if sys.platform != "win32":
    sys.exit("The app only builds on Windows for now.")

here = os.path.dirname(os.path.abspath(__file__))
os.chdir(here)
build_dir = os.path.join(here, "build")
app_dir = os.path.join(here, "dist", "Scrapify")
# kept between builds so chromium only downloads once
browsers_cache = os.path.join(build_dir, "browsers")
icon = os.path.join(here, "icon.ico")

print("Making sure requirements.txt is installed...")
subprocess.run([sys.executable, "-m", "pip", "install", "-r", "requirements.txt"], check=True)

# the version shown in the exe's Properties > Details
numbers = ", ".join((version.split(".") + ["0", "0", "0"])[:4])
version_info = os.path.join(build_dir, "version_info.txt")
os.makedirs(build_dir, exist_ok=True)
with open(version_info, "w", encoding="utf-8") as f:
    f.write(f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers=({numbers}), prodvers=({numbers})),
  kids=[StringFileInfo([StringTable('040904B0', [
    StringStruct('ProductName', 'Scrapify'),
    StringStruct('FileDescription', 'Scrapify'),
    StringStruct('ProductVersion', '{version}'),
    StringStruct('FileVersion', '{version}'),
    StringStruct('LegalCopyright', 'GPLv3'),
  ])]), VarFileInfo([VarStruct('Translation', [1033, 1200])])]
)
""")

subprocess.run([sys.executable, "-m", "PyInstaller", "--onedir", "--windowed", "--noconfirm",
                "--name", "Scrapify", "--distpath", os.path.dirname(app_dir),
                "--workpath", os.path.join(build_dir, "pyinstaller"), "--specpath", build_dir,
                "--version-file", version_info, "--icon", icon, "--add-data", f"{icon}{os.pathsep}.",
                "--collect-data", "sv_ttk",  # its tcl theme files
                "--exclude-module", "numpy",  # pillow and rapidfuzz pull it in, nothing here uses it
                "gui.py"], check=True)

print("\nGetting Chromium for the app folder...")
subprocess.run([sys.executable, "-m", "playwright", "install", "chromium", "--no-shell"],
               env={**os.environ, "PLAYWRIGHT_BROWSERS_PATH": browsers_cache}, check=True)
shutil.copytree(browsers_cache, os.path.join(app_dir, "browsers"), ignore=shutil.ignore_patterns(".links"))
shutil.copy2("LICENSE", app_dir)

zip_path = shutil.make_archive(os.path.join(here, "dist", f"Scrapify-{version}-windows"), "zip",
                               os.path.dirname(app_dir), "Scrapify")

# what the in-app updater downloads: the same folder without chromium, a fraction of the size.
# update.json says which chromium this version needs, so the updater knows when only the full zip will do
print("\nMaking the update zip...")
update_zip = os.path.join(here, "dist", f"Scrapify-{version}-update.zip")
with zipfile.ZipFile(update_zip, "w", zipfile.ZIP_DEFLATED) as z:
    for folder, dirs, files in os.walk(app_dir):
        rel = os.path.relpath(folder, os.path.dirname(app_dir))
        if folder == app_dir:
            dirs[:] = [d for d in dirs if d != "browsers"]
        for name in files:
            z.write(os.path.join(folder, name), os.path.join(rel, name))
# the installer reads it too: which version is the latest, and the checksum of its app zip
digest = hashlib.sha256()
with open(zip_path, "rb") as f:
    for chunk in iter(lambda: f.read(1 << 20), b""):
        digest.update(chunk)
zip_sha256 = digest.hexdigest()
with open(os.path.join(here, "dist", "update.json"), "w", encoding="utf-8") as f:
    json.dump({"version": version, "chromium": sorted(os.listdir(os.path.join(app_dir, "browsers"))),
               "windows_zip_sha256": zip_sha256}, f)


# the installer, when Inno Setup 6 is around. --installer makes it a must (the release build).
# it doesn't contain the app: it downloads the latest release when it runs
def find_iscc():
    candidates = [os.environ.get("ISCC"), shutil.which("iscc")]
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"),
                 os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs")):
        if base:
            candidates.append(os.path.join(base, "Inno Setup 6", "ISCC.exe"))
    return next((c for c in candidates if c and os.path.isfile(c)), None)


setup_path = os.path.join(here, "dist", "Scrapify-Setup.exe")  # downloads the latest release itself
iscc = find_iscc()
if iscc:
    print("\nMaking the installer...")
    subprocess.run([iscc, "/Q", f"/DAppVersion={version}", os.path.join(here, "installer.iss")], check=True)
elif "--installer" in sys.argv:
    sys.exit("Inno Setup 6 isn't installed, so there's no installer. Get it from jrsoftware.org/isdl.php")
else:
    setup_path = None
    print("\nSkipping the installer: Inno Setup 6 isn't installed (jrsoftware.org/isdl.php).")

print(f"\nDone: {app_dir}\nRelease zip: {zip_path}\nUpdate zip: {update_zip}"
      + (f"\nInstaller: {setup_path}" if setup_path else ""))
