"""The newest viewport render made through the Toolset — a record SHARED with
the Blender add-on.

Marty, 2026-08-05: "in blender UI panel/bridge add a button to watch latest
viewport render that is done trough the app, add a button like this too in the
app itself."

⚠ WHY A FILE AND NOT A BRIDGE CALL. Two different processes make these renders.
A blocking playblast is rendered by Blender itself, so the add-on knows about it
first-hand. A BACKGROUND playblast is rendered by a headless blender.exe the
Render Queue drives — the Blender the add-on lives in never sees that one at
all, and may not even be running. One small file both sides write and read is
the only arrangement where each button finds every render.

⚠ NOT `render_deck.util.data_dir()`. That one deliberately splits dev from
frozen so the app and the exe keep separate settings; this record is the
opposite — it has to be the SAME file for the exe, a source run and Blender.
It therefore lives beside license.bin in %LOCALAPPDATA%, mirroring
`core._shared_state_dir()` on the add-on side. `tests\anim_options_test.py`
asserts the two agree, because a silent disagreement here is two buttons that
each work perfectly and never see the same render.

Qt-free on purpose, like importer.py — it is imported by a Blender-side suite.
"""

import json
import os
import time

APP_FOLDER = "MadihsonNSFW Toolset"
LAST_RENDER_FILE = "last_render.json"


def state_dir():
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, APP_FOLDER)


def state_path():
    return os.path.join(state_dir(), LAST_RENDER_FILE)


def note(path):
    """Record `path` as the newest viewport render. Never raises: failing to
    write this costs a greyed-out button, and it must not take a finished
    render down with it."""
    try:
        os.makedirs(state_dir(), exist_ok=True)
        with open(state_path(), "w", encoding="utf-8") as f:
            json.dump({"path": path, "written": time.time()}, f)
        return True
    except (OSError, TypeError):
        return False


def last():
    """The recorded newest render, or None.

    ⚠ Checks the file still EXISTS. The record outlives the render: Marty
    tidies `_playblasts` out, or the mp4 was on a drive that is not plugged in
    today. A Watch button that opens a missing file is worse than one that is
    greyed out, so "gone" reads the same as "never was"."""
    try:
        with open(state_path(), "r", encoding="utf-8") as f:
            path = json.load(f).get("path")
    except (OSError, ValueError):
        return None
    return path if path and os.path.isfile(path) else None
