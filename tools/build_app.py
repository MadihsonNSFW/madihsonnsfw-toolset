"""Build the standalone app — **Windows only**.

    python tools\\build_app.py            # build here
    python tools\\build_app.py --no-dev   # skip the dev conveniences

⚠⚠ **THIS IS THE ONLY BUILD RECIPE.** `app\\build_exe.ps1` is a thin wrapper
that calls this with the venv's Python. Two recipes would drift, and the one
that drifts is always the one nobody runs by hand.

⚠ **THE LINUX/macOS PORT WAS CANCELLED on 2026-08-17** (Marty: *"we cancel ALL
porting of linux and mac and ONLY focus on windows"*). The per-platform icon,
the `.app` bundle handling and the CI build matrix all went with it. This
script now **refuses to run off Windows** rather than producing something
nobody tests: PyInstaller cannot cross-compile, so a non-Windows run could
only ever make a binary this project does not support.

⚠ **The running-app guard matters**: a running exe cannot be replaced on
Windows, and PyInstaller does not fail politely about it — it dies part way
and leaves the OLD binary in place. Two rebuilds were silently skipped that
way (`docs\\app-shell.md`).
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


def log(msg):
    print(msg, flush=True)


def app_version():
    src = open(os.path.join(APP, "version.py"), encoding="utf-8").read()
    hit = re.search(r'^APP_VERSION\s*=\s*"([^"]+)"', src, re.M)
    return hit.group(1) if hit else "unknown"


def icon_path():
    """The app icon."""
    return os.path.join(APP, "assets", "app_icon.ico")


def guard_running_exe():
    """Refuse rather than produce a silently stale build."""
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
    return os.path.join(APP, "dist", NAME, NAME + ".exe")


def beside_the_binary():
    """The folder `updates.py` looks in first — `dirname(sys.executable)`."""
    return os.path.join(APP, "dist", NAME)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-dev", action="store_true",
                    help="skip the dev-only conveniences")
    args = ap.parse_args(argv)

    # ⚠ Refuse off Windows rather than emit an untested binary. PyInstaller
    # cannot cross-compile, so a run here on Linux or macOS would not be a
    # Windows build going wrong — it would be a build of a platform this
    # project deliberately does not support (port cancelled 2026-08-17).
    if not WINDOWS:
        log("REFUSING TO BUILD: this is a Windows-only application and "
            "%s is not Windows." % sys.platform)
        return 1

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

    sep = os.pathsep          # ';' on Windows — PyInstaller's --add-data
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
