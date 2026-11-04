"""
builds the windows app into dist/Scrapify/ with chromium inside, then zips it for a release

    python buildexe.py
"""

import os
import shutil
import subprocess
import sys

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
                "--exclude-module", "numpy",  # pillow and rapidfuzz pull it in, nothing here uses it
                "gui.py"], check=True)

print("\nGetting Chromium for the app folder...")
subprocess.run([sys.executable, "-m", "playwright", "install", "chromium", "--no-shell"],
               env={**os.environ, "PLAYWRIGHT_BROWSERS_PATH": browsers_cache}, check=True)
shutil.copytree(browsers_cache, os.path.join(app_dir, "browsers"), ignore=shutil.ignore_patterns(".links"))
shutil.copy2("LICENSE", app_dir)

zip_path = shutil.make_archive(os.path.join(here, "dist", f"Scrapify-{version}-windows"), "zip",
                               os.path.dirname(app_dir), "Scrapify")
print(f"\nDone: {app_dir}\nRelease zip: {zip_path}")
