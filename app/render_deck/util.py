"""Cross-platform helpers and data storage for the Render Queue tool.

Queue settings and session live in a `render_queue\\` folder next to the
toolset's config.json (config.DATA_DIR) — beside the exe when frozen, beside
main.py from source, and under `~/Library/Application Support` on macOS — so
the dev app and the built exe keep separate state, exactly like config.json
itself. Falls back to `~/.madi_render_queue` if that folder can't be written.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import webbrowser
from typing import Optional

import config as _app_config


def is_frozen() -> bool:
    """True when running as a PyInstaller-built executable."""
    return getattr(sys, "frozen", False)


def _writable(d: str) -> bool:
    try:
        os.makedirs(d, exist_ok=True)
        probe = os.path.join(d, ".rdeck_write_test")
        with open(probe, "w", encoding="utf-8") as fh:
            fh.write("ok")
        os.remove(probe)
        return True
    except OSError:
        return False


def data_dir() -> str:
    """Folder for the queue's settings.json / session.json."""
    beside_config = os.path.join(_app_config.DATA_DIR, "render_queue")
    if _writable(beside_config):
        return beside_config
    home = os.path.join(os.path.expanduser("~"), ".madi_render_queue")
    if _writable(home):
        return home
    # Last resort: current working directory.
    return os.path.abspath(".")


def open_path(path: str) -> None:
    """Open a file/folder in the OS file manager (Windows/macOS/Linux)."""
    try:
        if sys.platform.startswith("win"):
            os.startfile(path)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception:
        pass


# One headless expression that reports everything we want to know about a
# .blend, as a single JSON marker line. Everything is read defensively with
# getattr: `scene.cycles` only exists when Cycles is enabled, `bpy.data.version`
# is the version the FILE was saved with (not the Blender running the probe),
# and packed data can live on images/sounds/fonts.
PROBE_EXPR = (
    "import bpy,os,json;"
    "s=bpy.context.scene;r=s.render;"
    "c=getattr(s,'cycles',None);e=getattr(s,'eevee',None);"
    "p=bpy.path.abspath(r.filepath);"
    "d=(p if p.endswith(('/','\\\\')) else os.path.dirname(p));"
    "pk=any(getattr(x,'packed_file',None) for n in ('images','sounds','fonts')"
    " for x in getattr(bpy.data,n,[]));"
    "print('RDECK_JSON<'+json.dumps({"
    "'start':s.frame_start,'end':s.frame_end,'out':d,"
    "'engine':r.engine,'fps':round(r.fps/(r.fps_base or 1),3),"
    "'res':[r.resolution_x,r.resolution_y,r.resolution_percentage],"
    "'version':list(bpy.data.version),'packed':bool(pk),"
    "'time_limit':getattr(c,'time_limit',0) or 0,"
    "'samples':getattr(c,'samples',0) or 0,"
    "'eevee_samples':getattr(e,'taa_render_samples',0) or 0,"
    "'adaptive':bool(getattr(c,'use_adaptive_sampling',False)),"
    "'noise':getattr(c,'adaptive_threshold',0) or 0,"
    # Every collection in the scene, so a job can be told which ones to leave
    # out. Names only - the queue never opens the .blend itself.
    "'collections':[x.name for x in bpy.data.collections]"
    "})+'>')"
)


def disable_collections_expr(names):
    """A --python-expr that excludes the named collections from the render.

    ⚠ `hide_render`, NOT `exclude`. `LayerCollection.exclude` unlinks the
    collection from the view layer, which drops its objects out of the depsgraph
    entirely — anything else depending on them (a boolean modifier's operand, a
    shrinkwrap target, a particle instance source) breaks rather than merely
    disappearing. `hide_render` is exactly "do not render this", which is what
    was asked for.

    ⚠ Applied to the COLLECTION, not the layer collection, so it holds for every
    view layer in the scene rather than only the active one.
    """
    wanted = [str(n) for n in (names or []) if str(n).strip()]
    if not wanted:
        return None
    return ("import bpy;_n=%r;"
            "[setattr(c,'hide_render',True) for c in bpy.data.collections "
            "if c.name in _n];"
            "print('RDECK_HIDDEN<'+','.join(sorted(_n))+'>')" % (wanted,))
_JSON_RE = re.compile(rb"RDECK_JSON<(.*?)>", re.S)


def probe_args(blend_path: str) -> list:
    """CLI args to headlessly read everything we display about a .blend — frame
    range, output dir, engine, saved-with version, packed data, samples, noise
    threshold and Cycles time limit. Run on Blender via QProcess (async, so the
    UI never blocks) and feed the collected output to `parse_probe`."""
    return ["-b", blend_path, "--python-expr", PROBE_EXPR]


# Kept under the old name too — the export-folder button only wants the path.
export_probe_args = probe_args


def parse_probe(data: bytes) -> Optional[dict]:
    """Pull the probe's JSON payload out of Blender's stdout, or None.

    Blender prints plenty of unrelated noise (add-on chatter, warnings), and a
    marker can straddle two reads, so callers should buffer the whole stream
    and parse once at exit."""
    m = _JSON_RE.search(data or b"")
    if not m:
        return None
    try:
        out = json.loads(m.group(1).decode("utf-8", "replace"))
    except (ValueError, UnicodeDecodeError):
        return None
    return out if isinstance(out, dict) else None


# ---- solid-mode (Workbench) H.264 preview render ---------------------
# NOTE: this is NOT bpy.ops.render.opengl() — that needs a GL context and is
# unreliable headless. The Workbench *engine* is what "solid mode" shading is,
# it renders from the scene camera through the normal pipeline, and it works
# under -b. See BLENDER_NOTES.md.
#
# On Blender 5.x 'FFMPEG' only appears in image_settings.file_format once
# media_type is 'VIDEO' — setting the format first raises. hasattr keeps this
# working on 4.x, which has no media_type.
PREVIEW_EXPR = (
    "import bpy;r=bpy.context.scene.render;im=r.image_settings;"
    "hasattr(im,'media_type') and setattr(im,'media_type','VIDEO');"
    "setattr(im,'file_format','FFMPEG');"
    "setattr(r.ffmpeg,'format','MPEG4');"
    "setattr(r.ffmpeg,'codec','H264');"
    "setattr(r.ffmpeg,'constant_rate_factor','MEDIUM');"
    "setattr(r.ffmpeg,'ffmpeg_preset','GOOD');"
    "setattr(r.ffmpeg,'audio_codec','NONE')"
)

def remove_snapshot(path: str) -> list:
    """Delete a playblast snapshot .blend AND its siblings.

    Blender's "Save Versions" preference (Preferences ▸ Save & Load, default 1)
    renames any existing target to `<name>.blend1` when saving, so removing
    just the .blend can leave an orphaned half-megabyte backup behind in
    %TEMP% — found exactly that way, 2026-08-02. `.blend2`+ appear when the
    preference is raised, so sweep a few.

    Returns the paths actually removed (for logging/tests).
    """
    gone = []
    for candidate in [path] + ["%s%d" % (path, n) for n in range(1, 4)]:
        try:
            os.remove(candidate)
            gone.append(candidate)
        except OSError:
            pass
    return gone


def playblast_expr(percent: int) -> str:
    """PREVIEW_EXPR plus the playblast dialog's resolution %.

    Same Workbench-not-render.opengl reasoning as above: the background
    playblast renders the SNAPSHOT of the live scene from its active camera,
    which is the only thing a headless Blender can do faithfully.
    """
    pct = max(1, min(400, int(percent)))
    return (PREVIEW_EXPR
            + ";setattr(r,'resolution_percentage',%d)" % pct
            + ";setattr(r,'use_file_extension',True)"
            + ";setattr(r,'use_overwrite',True)")


# Blender names video output "<-o prefix><start>-<end>.mp4", so this prefix
# yields e.g. "shot01_preview_0001-0250.mp4".
PREVIEW_SUFFIX = "_preview_"
# ⚠ THESE MUST COVER EVERY FORMAT BLENDER CAN WRITE, not just the popular
# ones. They are what Preview Video, View Anim. and the output scan match on,
# so a format missing here renders perfectly and is INVISIBLE to the tool —
# the same failure as the missing `-x 1` (2026-08-14), reached a different way.
# Anyone rendering Cineon, JPEG-2000, IRIS or an MPEG/FLV container hit it.
#
# Sources: Blender's `image_settings.file_format` enum and the FFmpeg
# container list. Keep both tuples in step with them, and remember the
# standalone render-manager carries its own copy.
VIDEO_EXTS = (".mp4", ".mkv", ".webm", ".avi", ".mov", ".ogv", ".ogg",
              ".mpg", ".mpeg", ".m4v", ".flv", ".dv", ".vob", ".m2v")
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".exr", ".tif", ".tiff", ".bmp",
              ".tga", ".webp", ".avif", ".dpx", ".hdr", ".rgb", ".jp2",
              ".j2c", ".cin", ".sgi", ".rad", ".psd")


def format_seconds(seconds) -> str:
    """Seconds -> M:SS / H:MM:SS; em-dash when unset."""
    if not seconds or seconds <= 0:
        return "—"
    s = int(round(seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"


def format_bytes(size) -> str:
    """Byte count -> a short human size (e.g. '48.2 MB'); em-dash when unknown."""
    if not size or size < 0:
        return "—"
    size = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return "—"


def newest_render(folder: str, prefer_prefix: str = ""):
    """Best guess at the animation to hand Blender's player for `folder`.

    Prefers a video file, else the FIRST frame of the newest image sequence (so
    the player starts at the beginning, not wherever the newest file landed).
    Files whose name starts with `prefer_prefix` win ties, which keeps one
    .blend's output from opening another's when they share an output folder.
    Returns None when there's nothing playable."""
    try:
        entries = [os.path.join(folder, n) for n in os.listdir(folder)]
    except OSError:
        return None
    files = [f for f in entries if os.path.isfile(f)]
    if not files:
        return None

    def rank(path):
        name = os.path.basename(path)
        ext = os.path.splitext(name)[1].lower()
        return (name.startswith(prefer_prefix) if prefer_prefix else False,
                ext in VIDEO_EXTS, os.path.getmtime(path))

    videos = [f for f in files if os.path.splitext(f)[1].lower() in VIDEO_EXTS]
    images = [f for f in files if os.path.splitext(f)[1].lower() in IMAGE_EXTS]
    if videos:
        return max(videos, key=rank)
    if not images:
        return None
    newest = max(images, key=rank)
    # Rewind to the first frame of that sequence: same directory, same name up
    # to the trailing frame digits, same extension.
    stem, ext = os.path.splitext(os.path.basename(newest))
    prefix = stem.rstrip("0123456789")
    if prefix != stem:
        siblings = [f for f in images
                    if os.path.basename(f).startswith(prefix)
                    and os.path.splitext(f)[1] == ext]
        if siblings:
            return min(siblings)
    return newest


def player_args(path: str) -> list:
    """CLI args to open Blender's built-in animation player on `path`.
    `-a` is the player only when NOT combined with -b (`blender --help`:
    "ignored if '-b' is set")."""
    return ["-a", path]


def open_url(url: str) -> None:
    """Open a URL in the user's default web browser (all platforms)."""
    webbrowser.open(url)


# ---- keep the PC awake -------------------------------------------------
# A long render is CPU/GPU work with no input, so Windows' idle timer happily
# suspends the machine mid-queue. These flags are the documented way to say
# "I'm busy":
#   ES_CONTINUOUS       make it a sticky state rather than a one-shot nudge —
#                       it stays until we clear it or the process exits.
#   ES_SYSTEM_REQUIRED  don't sleep the system.
# ES_DISPLAY_REQUIRED is deliberately NOT set: the monitor is free to blank,
# which is what you want overnight. Only the machine stays up.
#
# Because the state is owned by the process, closing (or crashing) the app
# always restores normal sleep behaviour — we can't strand the setting.
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001

# Linux: the equivalent is holding a systemd-inhibit process open. Stored here
# so we can kill it to release the lock.
_inhibitor = None


def set_keep_awake(on: bool) -> bool:
    """Block (or release) automatic sleep. True if the request took effect.

    Windows uses SetThreadExecutionState, which is per-thread — call this from
    one thread only (the GUI thread) or the release won't match the request.
    """
    global _inhibitor
    if sys.platform.startswith("win"):
        try:
            import ctypes
            fn = ctypes.windll.kernel32.SetThreadExecutionState
            # Pin the signature: the flag value exceeds INT_MAX, so leaving
            # ctypes to guess the argument type is asking for trouble.
            fn.argtypes = [ctypes.c_uint]
            fn.restype = ctypes.c_uint
            flags = ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0)
            # Returns the PREVIOUS state, or 0 on failure.
            return bool(fn(flags))
        except Exception:
            return False
    # Linux/macOS: hold `systemd-inhibit` open for as long as we want the lock.
    if not on:
        if _inhibitor is not None:
            try:
                _inhibitor.terminate()
            except Exception:
                pass
            _inhibitor = None
        return True
    if _inhibitor is not None:
        return True
    try:
        _inhibitor = subprocess.Popen(
            ["systemd-inhibit", "--what=idle:sleep", "--who=Madi Render Tool",
             "--why=Rendering", "--mode=block", "sleep", "infinity"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:
        _inhibitor = None
        return False


def shutdown_now() -> None:
    """Force a full-system shutdown, OS-appropriate.

    Windows: `shutdown /s /f /t 0` (/f force-closes apps blocking shutdown).
    Linux/macOS: try `systemctl poweroff`, then `shutdown -h now` (may need
    privileges / passwordless sudo on some distros).
    """
    if sys.platform.startswith("win"):
        subprocess.Popen(["shutdown", "/s", "/f", "/t", "0"])
        return
    for cmd in (["systemctl", "poweroff"], ["shutdown", "-h", "now"],
                ["sudo", "shutdown", "-h", "now"]):
        try:
            subprocess.Popen(cmd)
            return
        except Exception:
            continue
