"""Build the sendable Outbreak Field Guide package.

Produces  dist/Outbreak Field Guide/  containing one double-clickable .exe and a
short read-me -- then zips it.  The person you send it to needs nothing
installed: no Python, no LOVE, no tracker.

    py package.py            build the folder and the zip
    py package.py --no-zip   just the folder

The exe is ONE file with the guide built in, so it still works when someone
double-clicks it inside the zip without extracting (Windows then runs that one
file from a temp folder and nothing beside it).  A newer guide still wins: one
dropped next to the exe, or one the bridge downloads at startup once
bridge.UPDATE_URL is set, is picked over the built-in one by version.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
# Two layouts. Here in the research repo: _tools/bridge/, guides in "_Field Guide/".
# In the public repo: bridge/, the guide in guide/, the name table beside this file.
PUBLIC = (HERE.parent / "guide").is_dir()
if PUBLIC:
    ROOT = HERE.parent
GUIDE_DIR = ROOT / ("guide" if PUBLIC else "_Field Guide")
NAMES = (HERE / "enemy-type-names.json") if PUBLIC else \
    (ROOT / "extracted_data" / "combat" / "enemy-type-names.json")
NAME = "Outbreak Field Guide"
DIST = HERE / "dist"

READ_ME = """\
OUTBREAK FIELD GUIDE
====================

  1.  Start PCSX2 and load Outbreak (File 1 or File 2).
  2.  Double-click "Outbreak Field Guide.exe".

That is the whole thing.  Your browser opens with the guide, and it follows
along with the game.  You do not have to tell it anything.

HOW YOU KNOW IT IS WORKING
  Look at the top right of the guide.  A teal tag appears, like:

      DESPERATE TIMES - VH - PATTERN 1

  That means it has found your game, opened the right level, set the right
  difficulty, and -- the useful part -- worked out which of the four item
  patterns this run rolled.  No more clicking through P1/P2/P3/P4 to find out
  which one you are on.  Click the tag to make it stop following you.

  Grey tag = it is connected but you are not in a level yet.
  No tag at all = no game found; the guide still works, you just drive it.

Order does not matter.  Start this first and it waits for the game; start the
game first and it picks it up.  Either way it checks every few seconds.

Leave the little black window open while you play.  Closing it stops the guide.


PLAYING WITH FRIENDS
--------------------
Whoever runs this can share it with everyone else on the same network -- they
do not need to download anything.  Run "Outbreak Field Guide (share).bat"
instead, and send people the http://192.168.x.x address it prints.  Works on a
phone or a second monitor.


IS THIS SAFE TO RUN ONLINE?
---------------------------
It only reads.  It never writes anything into the game or the emulator, so it
cannot change what happens in your run or anyone else's.  It is a fancy way of
looking at the screen.


IF NOTHING HAPPENS
------------------
- The browser opened but the guide is not following the game:  make sure the
  game is actually loaded into a level, not sitting on the title screen.
- Windows SmartScreen warning:  "More info" then "Run anyway".  This happens to
  every unsigned program.
- Nothing opened at all:  open http://localhost:8777 in your browser yourself.
"""

BAT_SHARE = """\
@echo off
title Outbreak Field Guide - sharing on your network
cd /d "%~dp0"
echo.
echo   Starting the guide so other people on your network can open it.
echo   Send them the http://192.168... address printed below.
echo.
"Outbreak Field Guide.exe" --lan
pause
"""


def need_pyinstaller() -> None:
    try:
        import PyInstaller  # noqa: F401
        return
    except ModuleNotFoundError:
        pass
    print("PyInstaller is not installed.  Installing it now...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "pyinstaller"])


def find(name: str) -> Path:
    for b in (HERE, ROOT / "Outbreak_Tracker", ROOT):
        p = b / name
        if p.is_file():
            return p
    raise SystemExit(f"could not find {name} -- looked in {HERE}, {ROOT/'Outbreak_Tracker'}")


def find_guide() -> Path:
    import re
    best, key = None, ()
    for p in GUIDE_DIR.glob("obsrv-field-guide-v*.html"):
        m = re.search(r"-v(\d+)\.(\d+)", p.name)
        k = (int(m.group(1)), int(m.group(2))) if m else (0, 0)
        if k > key:
            best, key = p, k
    if not best:
        raise SystemExit(f"no obsrv-field-guide-v*.html in {GUIDE_DIR}")
    return best


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-zip", action="store_true")
    ap.add_argument("--with-tracker", action="store_true",
                    help="also bundle the Outbreak Tracker DLLs, for a PRIVATE build that "
                         "shadows our reader against them. Never publish this one: the "
                         "tracker has no licence (_docs/bridge-standalone-plan.md)")
    args = ap.parse_args()

    need_pyinstaller()

    guide = find_guide()
    names = NAMES
    binaries = []
    if args.with_tracker:
        for n in ("lua51.dll", "luaoutbreaktracker.dll"):
            p = find(n)
            binaries += ["--add-binary", f"{p};."]
            print(f"  {n:26}{p}")
    else:
        print("  reader                    our own (no third-party DLL)")
    print(f"  guide                     {guide.name}")

    build = HERE / "build"
    for d in (build, DIST):
        shutil.rmtree(d, ignore_errors=True)

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--name", NAME,
        "--distpath", str(DIST),
        "--workpath", str(build),
        "--specpath", str(build),
        "--onefile",
        "--console",
        "--add-data", f"{guide}{ ';' }.",
        *binaries,
        "--add-data", f"{names};.",
        "--add-data", f"{HERE / 'attack-virus.json'}{ ';' }.",
        str(HERE / "bridge.py"),
    ]
    print("\n  running PyInstaller...\n")
    subprocess.check_call(cmd)

    out = DIST / NAME
    out.mkdir(parents=True, exist_ok=True)
    (DIST / f"{NAME}.exe").replace(out / f"{NAME}.exe")
    (out / "READ ME FIRST.txt").write_text(READ_ME, encoding="utf-8")
    (out / f"{NAME} (share).bat").write_text(BAT_SHARE, encoding="utf-8")

    size = sum(f.stat().st_size for f in out.rglob("*") if f.is_file())
    print(f"\n  built {out}  ({size/1e6:.1f} MB)")

    if not args.no_zip:
        zp = DIST / f"{NAME}.zip"
        with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            for f in sorted(out.rglob("*")):
                if f.is_file():
                    z.write(f, Path(NAME) / f.relative_to(out))
        print(f"  zipped {zp}  ({zp.stat().st_size/1e6:.1f} MB)  <-- send this")

    shutil.rmtree(build, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
