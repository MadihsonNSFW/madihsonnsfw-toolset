"""MadihsonNSFW Toolset app — settings (config.json next to main.py / the exe)."""

import json
import os
import sys

if getattr(sys, "frozen", False):
    # PyInstaller build: config + default library live next to the exe
    APP_DIR = os.path.dirname(sys.executable)
    DEFAULT_LIBRARY = os.path.join(APP_DIR, "library")
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))
    DEFAULT_LIBRARY = os.path.normpath(os.path.join(APP_DIR, "..", "library"))
CONFIG_PATH = os.path.join(APP_DIR, "config.json")

DEFAULTS = {
    "libraries": [{"name": "Main", "path": DEFAULT_LIBRARY}],
    "port": 9877,
    "icon_size": 110,
    "current_tab": 0,   # library tab (inner)
    "main_tab": 0,      # toolset section (outer): 0 = Studio Library, 1 = Rendering
    # Watch every library folder and rescan when something changes on disk.
    # Global (all library tabs) and OFF by default — the manual rescan is ~39 ms
    # at 1000 dirs, so this is convenience, not a speed fix, and a watcher on a
    # network library is a cost you should opt into.
    "auto_refresh": False,
    # Developer console (⚙ Library Settings): shows this session's log —
    # errors, warnings, tracebacks, anything printed. OFF by default; the
    # recorder always runs, this only controls the window + its status-bar
    # button (see dev_console.py for why).
    # Colour theme (⚙ Library Settings). One of theme.THEMES; an unknown name
    # falls back rather than raising, so a config carried back from a newer
    # build cannot stop the app opening.
    "theme": "midnight",
    # MadiRef keeps prepared clips on disk so reopening one is instant. ⚠ The
    # budget is in GIGABYTES, not a file count: a proxy is ~36 MB per minute of
    # footage, so four long references reached 3.9 GB while sitting far under
    # the old 60-file cap. Trimmed oldest-first after each ingest.
    "madiref_cache_gb": 6.0,
    "dev_console": False,
    # Developer mode: edit (⚙ Library Settings). Right-click a tab, button,
    # label or rail entry to rename it; the renames go to dev_edits.json so they
    # can be made permanent in the source later. OFF by default, and note this
    # gates the EDITING only — renames already made stay applied either way
    # (devedit.py explains why).
    "dev_edit": False,
    # Keep the window above every other application (the 📌 button in the
    # status bar). Persisted so it survives a restart, and applied BEFORE the
    # first show so the window is never re-created just to set it.
    "always_on_top": False,
    # Super focus (the tickbox in the status bar): while it is on, the window
    # under the mouse takes focus — but ONLY this app or Blender, never anything
    # else. OFF by default, because it changes what a click does everywhere.
    "super_focus": False,
    # Export Abc options, remembered between exports (main.AbcExportDialog).
    # Empty = every default; the dialog fills it in on the first export. ⚠ NOT
    # seeded with the defaults here on purpose — this group is merged with its
    # default on load, so listing them twice would make main.py's table stop
    # being the one that decides.
    "abc_export": {},
    # Look for a newer version shortly after launch (⚙ Library Settings). ON by
    # ⚠ `auto_update` WAS REMOVED IN 1.19.0 along with the updater itself. An
    # existing config.json may still carry the key; nothing reads it, and
    # `save` no longer writes it, so it ages out on the next save.
    # Anim Layers tab preferences (see anim_layers.LayerOptionsTool)
    "anim_layers": {
        "sync_names": True,        # layer name <-> its action's name
        "auto_blend": True,        # pick Add/Replace when loading an action
        "default_blend": "COMBINE",  # blend type for a new layer
    },
    # Node Editor tab (see nodecanvas.py). "remember" is the ⚙ Library
    # Settings tickbox — while it is on, "last" holds each node TYPE's
    # last-used field values and new nodes (and the starting graph) pre-fill
    # from it. Off by default, and nothing is written while it is off.
    "nodeeditor": {
        "remember": False,
        "last": {},
    },
    # Optimization tab (see optimizer.py). These live HERE rather than in the
    # add-on's preferences, on purpose: the engine takes every dial as an
    # argument so it can be driven headless, there is only one store so nothing
    # can drift, and settings still follow the user rather than the .blend.
    # The one thing the add-on decides for itself is where to re-make a
    # stand-in when a file is opened — and it reads that off the image's own
    # path, so a .blend stays portable between machines.
    "optimizer": {
        "target": "SCENE",
        "quality": 1.0,          # 1.0 = exactly the on-screen size
        "min_size": 256,
        "max_size": 4096,
        "fixed_size": 1024,      # the Fixed size tool's one dial
        "animation": False,      # size for the whole frame range
        "frame_step": 1,
        "meshes": False,         # also decimate distant meshes
        "face_floor": 5000,      # meshes below this are left alone
        "full_distance": 20.0,   # closer than this = no decimation
        "low_distance": 200.0,   # past this = the lowest ratio
        "low_ratio": 0.2,
        "cache_dir": "",         # "" = the add-on's default folder
    },
}


def load():
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg.update(json.load(f))
    except (OSError, ValueError):
        pass
    if not cfg["libraries"]:
        cfg["libraries"] = [dict(DEFAULTS["libraries"][0])]
    # nested groups: a config written before a key existed must still get its
    # default, so fill in rather than replace
    for group, defaults in DEFAULTS.items():
        if isinstance(defaults, dict):
            merged = dict(defaults)
            if isinstance(cfg.get(group), dict):
                merged.update(cfg[group])
            cfg[group] = merged
    return cfg


def save(cfg):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=1)
    except OSError:
        pass
