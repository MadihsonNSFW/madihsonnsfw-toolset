# The cross-platform port (2026-08-16, PORT_PLAN.md stage 1).
#
# ⚠⚠ THIS SUITE IS THE ONLY COVERAGE THE PORT HAS. It runs on Marty's Windows
# box and there is no Mac and no Linux machine anywhere in this project, so
# every decision the port makes is pinned here by asking the code what it WOULD
# do on a platform, never by doing it. That is why `desktop.py` splits choosing
# a command from running one, and why `config` is re-imported under a faked
# `sys.platform` rather than probed: a function that returns argv can be
# checked from anywhere; a `subprocess.Popen` cannot.
#
# When stage 2 lands the CI matrix, the runners become the real proof and this
# suite stays as the fast one — it catches a wrong branch in a second, without
# waiting for three build jobs.
import importlib
import io
import os
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "app"))

PASS, FAIL = [], []


def ok(cond, label):
    (PASS if cond else FAIL).append(label)
    print(("ok   " if cond else "FAIL ") + label, flush=True)


# --------------------------------------------------------------- 1. desktop
import desktop  # noqa: E402

WIN, MAC, LINUX = "win32", "darwin", "linux"

ok(desktop.open_command("x.blend", WIN) is desktop.SHELL_OPEN,
   "open: Windows has no argv — it goes through the shell API")
ok(desktop.open_command("/a/x.mp4", MAC) == ["open", "/a/x.mp4"],
   "open: macOS uses `open`")
ok(desktop.open_command("/a/x.mp4", LINUX) == ["xdg-open", "/a/x.mp4"],
   "open: Linux uses `xdg-open`")

# ⚠ THE COMMA IS PART OF THE FLAG and the path must stay a separate argument.
# `explorer "/select,C:\x"` as one string silently opens Documents instead.
_rev = desktop.reveal_command(r"C:\lib\pose.blend", WIN)
ok(_rev[:2] == ["explorer", "/select,"] and len(_rev) == 3,
   "reveal: Windows keeps `/select,` and the path as separate argv entries")
ok(desktop.reveal_command("/a/x.blend", MAC) == ["open", "-R", "/a/x.blend"],
   "reveal: macOS uses `open -R`, which highlights the file")
_lin = desktop.reveal_command("/a/b/x.blend", LINUX)
ok(_lin[0] == "xdg-open" and _lin[1].endswith(os.sep + "b"),
   "reveal: Linux opens the CONTAINING FOLDER — no portable way to highlight")
ok(desktop.can_highlight_file(WIN) and desktop.can_highlight_file(MAC)
   and not desktop.can_highlight_file(LINUX),
   "reveal: and can_highlight_file() says so, so a caller can word it honestly")

# ⚠ The whole reason this module exists: `os.startfile` does not EXIST off
# Windows, and `AttributeError` is not an `OSError`, so the four call sites in
# main.py that catch OSError were catching nothing at all.
_main_src = io.open(os.path.join(_ROOT, "app", "main.py"),
                    encoding="utf-8").read()
ok("os.startfile" not in _main_src,
   "⚠ main.py calls no os.startfile — every one goes through desktop.py")
ok(not isinstance(getattr(os, "startfile", None), type(None))
   or sys.platform != "win32",
   "sanity: os.startfile exists on this (Windows) box, so the risk is real")

# --------------------------------------------------------------- 2. DATA_DIR


def _config_as(platform):
    """config re-imported as `platform` would see it.

    ⚠ It decides at IMPORT time, so the module has to be evicted and the fake
    installed first — reloading without evicting keeps the old constants.
    """
    real_platform, real_makedirs = sys.platform, os.makedirs
    sys.platform = platform
    os.makedirs = lambda *a, **k: None   # never really create the mac folder
    try:
        sys.modules.pop("config", None)
        return importlib.import_module("config")
    finally:
        sys.platform, os.makedirs = real_platform, real_makedirs


_win, _lin, _mac = _config_as(WIN), _config_as(LINUX), _config_as(MAC)

ok(_win.DATA_DIR == _win.APP_DIR,
   "⚠ DATA_DIR: Windows is UNCHANGED — data still lives beside the binary, so "
   "nothing of Marty's moves")
ok(_lin.DATA_DIR == _lin.APP_DIR,
   "DATA_DIR: Linux stays portable too, for the same reason")
# ⚠⚠ macOS is the whole point: `dirname(sys.executable)` there is
# `Toolset.app/Contents/MacOS/`, and writing inside the bundle breaks its
# signature and fails outright from /Applications.
ok(_mac.DATA_DIR != _mac.APP_DIR,
   "⚠⚠ DATA_DIR: macOS splits it off APP_DIR — never write inside the .app")
ok(_mac.DATA_DIR.replace("\\", "/").endswith(
    "Library/Application Support/MadihsonNSFW Toolset"),
   "DATA_DIR: and it lands in Application Support (%s)" % _mac.DATA_DIR)
ok(_mac.CONFIG_PATH.startswith(_mac.DATA_DIR),
   "DATA_DIR: config.json follows it")
# Put the real one back for anything imported after this.
_config_as(sys.platform)

# ⚠ EVERY WRITABLE ROOT MUST READ DATA_DIR. One left on APP_DIR is a folder the
# macOS build tries to create inside its own bundle — and the failure surfaces
# as that one feature being broken, not as a startup error.
_WRITERS = {
    "app/bakenodes.py": "baked",
    "app/devedit.py": "dev_edits",
    "app/madiref/ingest.py": "_madiref_cache",
    "app/madiref/notes.py": "_madiref_notes",
    "app/render_deck/util.py": "render_queue",
    "app/render_presets.py": "render presets",
    "app/video_preview.py": "_preview_cache",
}
_stragglers = []
for _rel in _WRITERS:
    _src = io.open(os.path.join(_ROOT, _rel), encoding="utf-8").read()
    for _line in _src.splitlines():
        if "APP_DIR" in _line and "os.path.join" in _line:
            _stragglers.append((_rel, _line.strip()[:60]))
ok(not _stragglers,
   "⚠ every writable root reads DATA_DIR, not APP_DIR (left behind: %r)"
   % (_stragglers,))

# ⚠⚠ `assets\` IS A WRITE TARGET, WHICH THIS SUITE FIRST GOT WRONG.
# It was asserted to stay on APP_DIR as a read-only resource - and then a real
# build produced an `assets\` folder beside the exe holding two GENERATED tick
# SVGs. Nothing reads a bundled asset from there at all (`app_icon.ico` is
# added at the bundle root), so the folder exists only because
# `theme._write_indicator_svgs()` writes it, and on macOS APP_DIR is inside the
# .app. *Ask what WRITES to a path, not what the folder is called.*
_theme_src = io.open(os.path.join(_ROOT, "app", "theme.py"),
                     encoding="utf-8").read()
ok('os.path.join(config.DATA_DIR, "assets")' in _theme_src
   and 'os.path.join(config.APP_DIR, "assets")' not in _theme_src,
   "⚠⚠ the generated tick SVGs are written under DATA_DIR — never "
   "inside the .app bundle")

# Any suite that redirects one root must redirect the other, or it builds its
# caches in the REAL dist folder.
_leaky = []
for _name in sorted(os.listdir(os.path.join(_ROOT, "tests"))):
    if not _name.endswith(".py"):
        continue
    _src = io.open(os.path.join(_ROOT, "tests", _name),
                   encoding="utf-8").read()
    if "config.APP_DIR = " in _src and "config.DATA_DIR = " not in _src:
        _leaky.append(_name)
ok(not _leaky,
   "⚠ no suite redirects APP_DIR without DATA_DIR (would write to dist: %r)"
   % (_leaky,))

# ----------------------------------------------------------- 3. the rest
import madiref.ingest as _ingest  # noqa: E402

ok(all(not h.lower().endswith(".exe") for h in _ingest._FFMPEG_HINTS)
   or sys.platform.startswith("win"),
   "ffmpeg: the hints match this platform")
_ing_src = io.open(os.path.join(_ROOT, "app", "madiref", "ingest.py"),
                   encoding="utf-8").read()
# ⚠ On macOS the hints matter MORE than on Windows: an app launched from
# Finder does not inherit the shell PATH, so `shutil.which` can come back empty
# on a machine where ffmpeg works fine in a terminal.
ok("/opt/homebrew/bin/ffmpeg" in _ing_src and "/usr/bin/ffmpeg" in _ing_src,
   "⚠ ffmpeg: Homebrew and Linux prefixes are hinted, because a GUI app does "
   "not inherit the shell PATH on macOS")
ok("import sys" in _ing_src,
   "ffmpeg: ingest imports sys — the platform branch needs it and it did not")

_addon_src = io.open(os.path.join(_ROOT, "blender_addon", "madi_anim_library",
                                  "__init__.py"), encoding="utf-8").read()
ok('"*.exe;*.bat" if sys.platform.startswith("win") else "*"' in _addon_src,
   "⚠ add-on: the Open Toolset App picker filters *.exe only on Windows — "
   "elsewhere that filter hides the very file it asks for")

import superfocus  # noqa: E402

ok(hasattr(superfocus, "available"),
   "superfocus: reports availability rather than assuming Windows")
ok("if not superfocus.available():" in _main_src
   and "self.superfocus_box.hide()" in _main_src,
   "superfocus: and the status-bar control HIDES where it cannot work")

import chrome as _chrome  # noqa: E402

ok(hasattr(_chrome, "available"),
   "chrome: the custom title bar degrades to the native one off Windows")

print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
for _f in FAIL:
    print("FAIL " + _f)
sys.exit(1 if FAIL else 0)
