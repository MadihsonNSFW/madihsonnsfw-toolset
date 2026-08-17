"""Build the standalone app — ONE recipe, three platforms.

    python tools\\build_app.py            # build here
    python tools/build_app.py --no-dev    # CI: skip the dev conveniences

⚠⚠ **THIS IS THE ONLY BUILD RECIPE.** `app\\build_exe.ps1` is a thin wrapper
that calls this with the venv's Python, and the CI matrix calls it directly.
Two recipes would drift, and the one that drifts is always the one nobody runs
by hand — which, from now on, is Windows.

⚠ **PyInstaller CANNOT CROSS-COMPILE.** A macOS bundle can only be produced on
macOS and a Linux binary on Linux. That is the entire reason the CI matrix
exists; this script's job is to behave identically on whichever machine it
lands on.

What differs per platform, and why:

* **`--add-data` uses `os.pathsep`** — `;` on Windows, `:` everywhere else.
  Hard-coding the semicolon silently produces a build with no CHANGELOG on
  Linux and macOS, and the What's New tab then opens on an apology.
* **The icon is a different FILE, not a different flag.** `.ico` on Windows,
  `.icns` on macOS (`tools\\make_icns.py` writes it), and **nothing at all on
  Linux** — PyInstaller has no icon slot in an ELF binary, and passing one
  there is a warning at best.
* **macOS `--windowed` produces a `.app` BUNDLE** beside the onedir folder.
  The bundle is what ships; the folder is scaffolding.
* **The running-app guard is Windows-only** because the problem is: a running
  exe cannot be replaced there, and PyInstaller does not fail politely about
  it — it dies part way and leaves the OLD binary in place. Two rebuilds were
  silently skipped that way (`docs\\app-shell.md`). POSIX replaces a running
  binary's file happily, so there is nothing to guard.
"""
import argparse
import os
import re
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "app")
NAME = "MadihsonNSFW Toolset"

WINDOWS = sys.platform.startswith("win")
MACOS = sys.platform == "darwin"


def log(msg):
    print(msg, flush=True)


def app_version():
    src = open(os.path.join(APP, "version.py"), encoding="utf-8").read()
    hit = re.search(r'^APP_VERSION\s*=\s*"([^"]+)"', src, re.M)
    return hit.group(1) if hit else "unknown"


def icon_path():
    """The icon for THIS platform, or None where the format has no slot."""
    if WINDOWS:
        return os.path.join(APP, "assets", "app_icon.ico")
    if MACOS:
        icns = os.path.join(APP, "assets", "app_icon.icns")
        if not os.path.isfile(icns):
            # Buildable without it; say so rather than failing the build.
            log("NOTE: no app_icon.icns — run tools/make_icns.py for a "
                "macOS icon. Building without one.")
            return None
        return icns
    return None


def guard_running_exe():
    """Windows only: refuse rather than produce a silently stale build."""
    if not WINDOWS:
        return
    exe = os.path.join(APP, "dist", NAME, NAME + ".exe")
    if not os.path.isfile(exe):
        return
    try:
        # Opening for write with no sharing catches the app itself AND any
        # other holder: an antivirus scan, an Explorer preview, a debugger.
        with open(exe, "r+b"):
            pass
    except OSError:
        log("REFUSING TO BUILD: something is holding the exe:\n  %s\n"
            "Close it (the app, a preview, a scan) and run again." % exe)
        raise SystemExit(1)


def built_path():
    """Where the thing we just built actually is."""
    dist = os.path.join(APP, "dist")
    if MACOS:
        return os.path.join(dist, NAME + ".app")
    return os.path.join(dist, NAME, NAME + (".exe" if WINDOWS else ""))


def beside_the_binary():
    """The folder `updates.py` looks in first — `dirname(sys.executable)`."""
    if MACOS:
        return os.path.join(APP, "dist", NAME + ".app", "Contents", "MacOS")
    return os.path.join(APP, "dist", NAME)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-dev", action="store_true",
                    help="skip the dev-only conveniences (CI uses this)")
    args = ap.parse_args(argv)

    guard_running_exe()

    # ⚠ PACK THE ADD-ON FIRST, from its source folder. Doing it here rather
    # than by hand is the point: the exe can never ship an add-on zip older
    # than the source it was built from, because there is no separate step to
    # forget.
    log("Packing the Blender add-on...")
    rc = subprocess.call([sys.executable,
                          os.path.join(ROOT, "tools", "pack_addon.py")])
    if rc != 0:
        log("REFUSING TO BUILD: could not pack the Blender add-on.")
        return 1

    version = app_version()
    log("Building %s %s for %s" % (NAME, version, sys.platform))

    sep = os.pathsep          # ';' on Windows, ':' elsewhere — see docstring
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
           "--windowed", "--name", NAME,
           # zstandard is imported inside a try/except in blendsize.py, so a
           # build that silently dropped it would look fine and refuse nearly
           # every real .blend — Blender compresses by default. Named
           # explicitly, C backend included, so that cannot happen.
           "--hidden-import", "zstandard",
           "--hidden-import", "zstandard.backend_c",
           # CHANGELOG.md is DATA: PyInstaller does not reach it by following
           # imports, and a build without it opens What's New on an apology.
           "--add-data", os.path.join(APP, "CHANGELOG.md") + sep + ".",
           "--distpath", os.path.join(APP, "dist"),
           "--workpath", os.path.join(APP, "build"),
           "--specpath", os.path.join(APP, "build"),
           os.path.join(APP, "main.py")]

    icon = icon_path()
    if icon:
        # --icon burns it into the binary (Explorer / the Dock / Alt-Tab);
        # --add-data ships the same file so Qt can set the WINDOW icon at run
        # time. Both are needed — neither covers the other's surface.
        cmd[4:4] = ["--icon", icon]
        cmd += ["--add-data", icon + sep + "."]

    rc = subprocess.call(cmd)
    if rc != 0:
        return rc

    # A copy BESIDE the binary, which is the one the tab prefers — that is
    # what makes it possible to correct a typo in the notes without a rebuild.
    dest = beside_the_binary()
    if os.path.isdir(dest):
        shutil.copyfile(os.path.join(APP, "CHANGELOG.md"),
                        os.path.join(dest, "CHANGELOG.md"))
        log("Copied CHANGELOG.md beside the binary.")

    # ⚠ DEV ONLY, AND CI MUST NOT DO IT. `dist\config.json` carries the local
    # library path — the dev machine's own folder — and shipping it relocates
    # whoever unzips the release. The release packer strips it as well; this
    # flag stops it ever being written in the first place.
    if not args.no_dev:
        local = os.path.join(APP, "config.json")
        if os.path.isfile(local) and os.path.isdir(dest):
            shutil.copyfile(local, os.path.join(dest, "config.json"))
            log("Copied local config.json beside the binary (dev only).")

    out = built_path()
    log("Built: %s  (version %s)" % (out, version))
    if not os.path.exists(out):
        log("...but it is NOT THERE. PyInstaller reported success and the "
            "output is missing — treat this as a failed build.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
