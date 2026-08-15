"""MADI Scene Optimizer — fit a heavy scene into VRAM without touching originals.

Three jobs, all reversible in one click:

1. **Textures are downscaled to the size they actually need.** The original file
   on disk is never modified. A smaller copy — a *stand-in* — is written into a
   cache folder and the existing `bpy.types.Image` datablock has only its
   `filepath` re-pointed at it. Because the datablock survives, every node tree,
   UV map, colour-space setting and user count carries on working untouched.
2. **Distant meshes get a managed Decimate modifier**, its ratio driven by how
   far the object is from the camera.
3. **Memory is estimated per datablock**, so it is possible to see what is
   actually eating the RAM rather than guessing.

HOW BIG SHOULD A TEXTURE BE?
Project the object's world-space bounding box through the render camera, measure
how many pixels across it lands, multiply by a quality factor, round up to a
power of two. An image used by several objects takes the LARGEST of their
answers, so a texture shared by a near object and a far one keeps the near one's
detail.

⚠ **This is blind to UV density.** An object using 10% of a texture atlas is
still sized by its screen bounds, so an atlas or an extreme close-up can soften.
The quality factor is the dial for that. Say so in the UI — do not let it be
described as "the optimal size".

WHAT IS STORED IN THE .BLEND
Three custom properties on each managed Image (see PROP_*), and nothing else.
That is what makes this survive a save/reload and travel with the file. The
stand-ins themselves live outside the .blend and are regenerated on demand.

⚠ **NOTHING EVER RESIZES A STAND-IN.** Every generation reads PROP_ORIGINAL, so
running the optimizer twice cannot produce a copy of a copy. That single rule is
what makes the whole thing idempotent, and it is why revert can always get back
to exactly the file the user started with.

⚠ **NOTHING EVER UPSCALES.** An image already at or below the target is left
alone, and an adaptive size is capped at the source's real resolution. Asking for
a size larger than the original while on a stand-in reverts the image instead —
which is the only sensible reading of "make it bigger than it can be".

HOW PROGRESS GETS OUT
A run owns Blender's main thread from start to finish, so it cannot answer the
bridge while it works. It publishes counters into a plain module-level record
instead, and `opt_progress()` is dispatched on the SOCKET thread to read them —
see the Progress section for why that record is shaped the way it is.
"""

import contextlib
import glob
import hashlib
import json
import os
import re
import time

import bpy
from bpy.types import Operator
from bpy.app.handlers import persistent
from mathutils import Matrix, Vector
from bpy_extras.object_utils import world_to_camera_view

# ⚠ `entitlement` is no longer imported here: the Optimization tab went FREE
# with everything else on 2026-08-14 (premium packs are the paid thing now,
# gated in the app's licence server). The preview operator below used to gate
# its poll() and invoke() on entitlement.unlocked() — restore both if the tab
# is ever gated again, because bpy.ops is reachable without the app.

# ---------------------------------------------------------------------------
# Bookkeeping written into the .blend
# ---------------------------------------------------------------------------
# Custom properties on the Image datablock. Three, deliberately: where the
# original lives, WHICH stand-in we put there, and how big we asked for.
PROP_ORIGINAL = "madi_opt_original_path"
PROP_STANDIN = "madi_opt_standin_file"
PROP_SIZE = "madi_opt_size"
# ⚠ THE FOURTH MARK EXISTS BECAUSE OF "SAVE AS", and it is the difference
# between Restore working and someone losing their textures.
# PROP_ORIGINAL keeps the path in the form the user wrote it, and a `//relative`
# path is resolved AGAINST THE .BLEND. Save the file into a different folder and
# that same string silently points somewhere else — so Restore would happily
# write it back and the texture would come up missing, with the real one no
# longer recorded anywhere. This holds the absolute path as it was at stamping
# time, purely as the fallback for when the relative one stops resolving.
# Added in add-on 0.12.0: images stamped by 0.11.0 simply do not have it, and
# everything below treats that as "only the raw form is known".
PROP_ORIGINAL_ABS = "madi_opt_original_abs"

# The one modifier this module owns. Matched BY NAME, so a Decimate the user
# added themselves is never touched, moved or removed.
DECIMATE_MOD = "MADI_Opt_Decimate"

# No texture is ever generated smaller than this. 16x16 files save nothing
# measurable and look like a bug when one turns up in a render.
MIN_SIDE = 32

# What OpenImageIO can read AND write here. Anything else is left alone with a
# reason rather than being quietly skipped.
RESIZABLE_EXTS = frozenset((
    ".bmp", ".exr", ".jpg", ".jpeg", ".jpe", ".jif", ".jfif", ".png",
    ".tga", ".hdr", ".tif", ".tiff", ".webp"))

# Image sources that can never have a stand-in: there is no file to shrink.
UNSHRINKABLE_SOURCES = frozenset(("GENERATED", "VIEWER", "MOVIE"))

HDR_EXTS = frozenset((".exr", ".hdr"))

# Which datablocks a run acts on.
TARGETS = (
    "SELECTED",       # selected objects
    "SCENE",          # every object in the current scene   <- default
    "ALL_OBJECTS",    # every object in the file
    "IMAGES_NO_HDR",  # every image except HDR/EXR
    "IMAGES_HDR",     # only HDR/EXR (the usual "cap the world HDRI" case)
    "ALL_IMAGES",     # every image in the file
)
DEFAULT_TARGET = "SCENE"

# Objects have no say over the world's environment texture, so an adaptive run
# never sees an HDRI. IMAGES_HDR is how it gets capped — one click, one size.
OBJECT_TARGETS = frozenset(("SELECTED", "SCENE", "ALL_OBJECTS"))

# Defaults, all overridable per call. The app owns the user's copies (its
# config.json) and passes them in; these are what a headless call gets.
DEFAULT_QUALITY = 1.0
DEFAULT_MIN_SIZE = 256
DEFAULT_MAX_SIZE = 4096
DEFAULT_FACE_FLOOR = 5000
DEFAULT_FULL_DISTANCE = 20.0
DEFAULT_LOW_DISTANCE = 200.0
DEFAULT_LOW_RATIO = 0.2

_INSTANCE_DEPTH = 8       # collection instances nest; this stops a cycle dead

# Where the draw handle for the preview overlay is parked. It CANNOT live in
# module state: a reload replaces the module dict, and Blender exposes no way to
# enumerate registered draw handlers, so a lost handle can never be removed and
# the callback paints over every viewport for the rest of the session. The
# driver namespace belongs to Blender and survives a reload. (Same lesson as
# picker.py — see docs\addon-bridge.md.)
_DRAW_KEY = "_madi_optimizer_draw_handle"

_preview = {"running": False, "rows": [], "note": ""}


# ---------------------------------------------------------------------------
# Progress
# ---------------------------------------------------------------------------
# A run holds Blender's main thread for as long as it takes — that is not
# something this module can change, because every line of it touches bpy data.
# What it CAN do is say how far along it is, and the only route out while the
# main thread is busy is a socket thread. So:
#
# ⚠ THIS RECORD IS READ FROM A SOCKET THREAD WHILE THE MAIN THREAD IS MID-RUN,
# and that is the entire reason it is shaped the way it is:
#
#   * It is REPLACED WHOLESALE, never mutated in place. Rebinding a module
#     global is atomic under the GIL, so a reader either sees the whole previous
#     record or the whole next one — never a half-written one. Mutating
#     `_progress["done"] += 1` would be two bytecodes with a visible gap.
#   * `opt_progress()` TOUCHES NO bpy WHATSOEVER. Reading bpy data from a socket
#     thread while the main thread is writing it is how you crash Blender, not
#     how you report progress. Everything the app needs is copied into plain
#     ints and strings here, on the main thread, as the work happens.
#
# `serial` rises once per run so a reader can tell one run's numbers from the
# next one's. The app's bar does not need it today — its poll is synchronous, so
# no reply can outlive the run it belongs to — but any reader that polls
# asynchronously would have no other way to reject a stale reply, and it costs
# one integer.
_progress = {"active": False, "phase": "", "done": 0, "total": 0,
             "item": "", "serial": 0, "started": 0.0}


def _progress_set(**changes):
    """Publish a new record. Never mutates the old one — see the note above."""
    global _progress
    record = dict(_progress)
    record.update(changes)
    _progress = record


def _progress_begin(phase):
    _progress_set(active=True, phase=phase, done=0, total=0, item="",
                  serial=_progress["serial"] + 1, started=time.time())


def _progress_end():
    _progress_set(active=False, phase="", done=0, total=0, item="")


def _progress_phase(phase, total):
    """Start counting a new stage of the SAME run. No-op outside a run.

    The no-op matters: `plan_adaptive` and friends are called directly by tests
    and headless callers, and a stage marker left behind by one of those would
    show an 'active' run that nothing is ever going to finish.
    """
    if _progress["active"]:
        _progress_set(phase=phase, done=0, total=int(total), item="")


def _progress_step(item=""):
    if _progress["active"]:
        _progress_set(done=_progress["done"] + 1, item=str(item))


@contextlib.contextmanager
def _progress_run(phase):
    _progress_begin(phase)
    try:
        yield
    finally:
        _progress_end()


def opt_progress():
    """How far the run in flight has got. **Answered off the main thread.**

    ⚠ NO bpy ACCESS HERE, EVER. This is dispatched on the socket thread
    (server.py routes it before the main-thread queue) precisely because the
    main thread is busy — that is the only way an answer can come back at all
    while a resize is running. Touching bpy from here would be reading
    Blender's data while it is being written.
    """
    snapshot = _progress                    # one atomic read of the global
    elapsed = (time.time() - snapshot["started"]) if snapshot["active"] else 0.0
    out = dict(snapshot)
    out["elapsed"] = round(max(0.0, elapsed), 2)
    return out


# ---------------------------------------------------------------------------
# Texture sets ("material groups")
# ---------------------------------------------------------------------------
# Marty, 2026-08-04: "whenever a user change size of some textures, a material
# group is created (that can be renamed) ... so users can cycle between them".
# The use case is one resolution for one scene and a different one for another.
#
# A group is a NAMED RECORD OF A CHOICE — which images, at which sizes — and
# nothing more. It is not a copy of anything and it owns no files:
#
# ⚠ A GROUP IS NOT A BACKUP, AND MUST NEVER BECOME THE ROUTE HOME. Restoring
# originals goes through PROP_ORIGINAL exactly as before, group or no group.
# Marty was explicit: "this has nothing to do with Original textures (since we
# always need to have the ability to restore originals)". Deleting every group
# must therefore still leave every original reachable — there is a test for it.
#
# Stored as JSON on the Scene so it saves with the .blend and travels with it.
# JSON rather than nested ID properties because the shape is a list of dicts of
# mixed types, which is exactly where IDProperty round-tripping gets fiddly.
SCENE_GROUPS = "madi_opt_texture_sets"
SCENE_ACTIVE_GROUP = "madi_opt_active_set"


def _load_groups(scene):
    raw = scene.get(SCENE_GROUPS)
    if not raw:
        return []
    try:
        groups = json.loads(raw)
    except (ValueError, TypeError):
        return []                      # corrupt: behave as if there were none
    return groups if isinstance(groups, list) else []


def _save_groups(scene, groups):
    scene[SCENE_GROUPS] = json.dumps(groups)


def _unique_group_name(groups, wanted):
    """`wanted`, or `wanted (2)`, `wanted (3)`… — names are how the user picks
    one, so two groups may never share one."""
    taken = {g.get("name") for g in groups}
    if wanted not in taken:
        return wanted
    n = 2
    while "%s (%d)" % (wanted, n) in taken:
        n += 1
    return "%s (%d)" % (wanted, n)


def group_capture(scene, name, entries, cache_dir, replace=None):
    """Record a resize as a named set. Returns the stored group.

    `replace` names an existing group to overwrite (used when a queued job
    extends the set it just made); otherwise a fresh, uniquely-named one.
    """
    groups = _load_groups(scene)
    payload = {
        "name": name,
        "cache_dir": cache_dir,
        "created": time.time(),
        # The cache folder is stored ON THE GROUP rather than looked up per
        # call: a group knows where its own files live, so listing it can check
        # they are still there without being told anything.
        "entries": entries,
    }
    for index, existing in enumerate(groups):
        if replace and existing.get("name") == replace:
            payload["name"] = replace
            payload["created"] = existing.get("created", payload["created"])
            groups[index] = payload
            break
    else:
        payload["name"] = _unique_group_name(groups, name)
        groups.append(payload)
    _save_groups(scene, groups)
    scene[SCENE_ACTIVE_GROUP] = payload["name"]
    return payload


def group_entries_for(images_and_sizes):
    """[(image, side)] -> the stored form, with each original recorded.

    The original is kept so a group can still say WHICH texture it meant after
    the image datablock has been renamed or removed.
    """
    entries = []
    for image, side in images_and_sizes:
        entries.append({"image": image.name,
                        "original": original_of(image),
                        "size": int(side)})
    return entries


def group_state(scene):
    """Every set, with whether its cached files are actually still on disk.

    ⚠ The missing count is the whole reason this is reported rather than just
    the names. Marty: "when caching is missing or cleared user should be told
    about this". A set whose files have been deleted still looks perfectly
    valid in a list of names, and switching to it would silently do nothing.
    """
    out = []
    active = scene.get(SCENE_ACTIVE_GROUP) or ""
    for group in _load_groups(scene):
        cache_dir = group.get("cache_dir") or default_cache_dir()
        missing = 0
        for entry in group.get("entries") or []:
            image = bpy.data.images.get(entry.get("image", ""))
            original_abs = (resolve_original(image)[1] if image is not None
                            else _abs_path_raw(entry.get("original", "")))
            if not original_abs:
                missing += 1
                continue
            target = expected_standin(image, original_abs, cache_dir,
                                      int(entry.get("size") or 0))
            if not target or not os.path.isfile(target):
                missing += 1
        out.append({
            "name": group.get("name", ""),
            "cache_dir": cache_dir,
            "created": group.get("created", 0),
            "count": len(group.get("entries") or []),
            "missing": missing,
            "sizes": sorted({int(e.get("size") or 0)
                             for e in group.get("entries") or []}),
            "active": group.get("name", "") == active,
        })
    return out


def _abs_path_raw(raw):
    """Absolutise a stored path with no Image to resolve it against."""
    if not raw:
        return ""
    try:
        return os.path.normpath(bpy.path.abspath(raw))
    except Exception:                       # noqa: BLE001
        return ""


def group_apply(scene, name, tally=None):
    """Switch the scene to a set — every image in it back to its recorded size.

    Regenerates anything whose cached file has gone, so a cleared cache costs
    time rather than correctness.
    """
    for group in _load_groups(scene):
        if group.get("name") != name:
            continue
        cache_dir = group.get("cache_dir") or default_cache_dir()
        entries = group.get("entries") or []
        wanted = {e.get("image"): int(e.get("size") or 0) for e in entries}

        # ⚠ ANYTHING MANAGED AND NOT IN THIS SET IS PUT BACK FIRST, and that is
        # what makes a set a set rather than a patch. Without it, switching from
        # a 64 px set to a 128 px one leaves every texture the 128 set never
        # mentioned sitting at 64 - so the scene is a mixture of two sets and
        # matches neither. Marty asked to "cycle between them", which only means
        # anything if arriving at a set gets you that set.
        strays = [im for im in bpy.data.images
                  if is_managed(im) and im.name not in wanted]
        _progress_phase("Switching texture set", len(strays) + len(entries))
        for image in strays:
            _progress_step(image.name)
            revert_image(image, tally=tally)

        for entry in entries:
            _progress_step(entry.get("image", ""))
            image = bpy.data.images.get(entry.get("image", ""))
            if image is None:
                if tally is not None:
                    tally.skipped.append((entry.get("image", "?"),
                                          "no longer in this file"))
                continue
            set_image_size(image, int(entry.get("size") or 0), cache_dir,
                           tally=tally)
        scene[SCENE_ACTIVE_GROUP] = name
        return True
    raise RuntimeError("no texture set called %r" % name)


def group_rename(scene, old, new):
    groups = _load_groups(scene)
    if not any(g.get("name") == old for g in groups):
        raise RuntimeError("no texture set called %r" % old)
    new = (new or "").strip()
    if not new:
        raise RuntimeError("a texture set needs a name")
    for group in groups:
        if group.get("name") == old:
            group["name"] = _unique_group_name(
                [g for g in groups if g is not group], new)
            if scene.get(SCENE_ACTIVE_GROUP) == old:
                scene[SCENE_ACTIVE_GROUP] = group["name"]
            break
    _save_groups(scene, groups)
    return True


def group_delete(scene, name):
    """Forget a set. ⚠ Touches no image and no file — the textures stay exactly
    as they are and every original stays reachable through PROP_ORIGINAL. This
    deletes a note, not a state."""
    groups = _load_groups(scene)
    kept = [g for g in groups if g.get("name") != name]
    if len(kept) == len(groups):
        raise RuntimeError("no texture set called %r" % name)
    _save_groups(scene, kept)
    if scene.get(SCENE_ACTIVE_GROUP) == name:
        scene[SCENE_ACTIVE_GROUP] = ""
    return True


def default_cache_dir():
    """Where stand-ins go unless the caller says otherwise."""
    return os.path.join(os.path.expanduser("~"), "madi_optimizer_cache")


# ---------------------------------------------------------------------------
# Result tallies
# ---------------------------------------------------------------------------

class Tally(object):
    """What a batch did, in the shape the UI reports it.

    Every per-item failure lands here instead of aborting the run: one unreadable
    texture in a scene of four hundred must not stop the other 399.
    """

    def __init__(self):
        self.changed = []
        self.unchanged = []
        self.failed = []       # [(name, reason)]
        self.skipped = []      # [(name, reason)]
        self.bytes_before = 0
        self.bytes_after = 0

    def as_dict(self):
        return {
            "changed": list(self.changed),
            "unchanged": list(self.unchanged),
            "failed": [{"name": n, "reason": r} for n, r in self.failed],
            "skipped": [{"name": n, "reason": r} for n, r in self.skipped],
            "counts": {"changed": len(self.changed),
                       "unchanged": len(self.unchanged),
                       "failed": len(self.failed),
                       "skipped": len(self.skipped)},
            "bytes_before": self.bytes_before,
            "bytes_after": self.bytes_after,
            "bytes_saved": max(0, self.bytes_before - self.bytes_after),
            "summary": self.summary(),
        }

    def summary(self):
        bits = []
        if self.changed:
            bits.append("%d changed" % len(self.changed))
        if self.unchanged:
            bits.append("%d already right" % len(self.unchanged))
        if self.skipped:
            bits.append("%d skipped" % len(self.skipped))
        if self.failed:
            bits.append("%d failed" % len(self.failed))
        if not bits:
            return "Nothing to do."
        saved = max(0, self.bytes_before - self.bytes_after)
        if saved:
            bits.append("about %s less in memory" % human_bytes(saved))
        return ", ".join(bits).capitalize() + "."


def human_bytes(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024.0 or unit == "GB":
            return "%.0f %s" % (n, unit) if unit == "B" else "%.1f %s" % (n, unit)
        n /= 1024.0
    return "%.1f GB" % n


# ---------------------------------------------------------------------------
# Image bookkeeping
# ---------------------------------------------------------------------------

def _abs_path(image, path=None):
    """Absolutise a path the way BLENDER would resolve it.

    ⚠ `library=` is load-bearing: a relative `//tex.png` inside a linked
    datablock resolves against the LIBRARY file, not the open one. Without it
    every linked texture absolutises to the wrong folder.
    """
    raw = image.filepath if path is None else path
    if not raw:
        return ""
    return os.path.normpath(bpy.path.abspath(raw, library=image.library))


def is_managed(image):
    """True when this image is currently pointed at one of our stand-ins."""
    return bool(image.get(PROP_ORIGINAL)) and int(image.get(PROP_SIZE) or 0) > 0


def managed_size(image):
    return int(image.get(PROP_SIZE) or 0)


def original_of(image):
    """The path the user's image REALLY lives at, managed or not.

    Kept in whatever form it was stored (a `//relative` path stays relative), so
    reverting writes back the exact string Blender had.
    """
    stored = image.get(PROP_ORIGINAL)
    return stored if stored else image.filepath


def _stamp(image, original_raw, standin_path, side):
    image[PROP_ORIGINAL] = original_raw
    image[PROP_STANDIN] = os.path.basename(standin_path)
    image[PROP_SIZE] = int(side)
    # Resolved and stored NOW, while the .blend is still where it was when the
    # relative path was written. This is the only moment it is knowably correct.
    image[PROP_ORIGINAL_ABS] = _abs_path(image, original_raw)


def forget(image):
    """Stop managing an image — drops every mark, touches no files."""
    for key in (PROP_ORIGINAL, PROP_STANDIN, PROP_SIZE, PROP_ORIGINAL_ABS):
        if key in image:
            del image[key]


def resolve_original(image):
    """Where the user's own file really is, and what to write back.

    Returns `(raw, absolute)`. Normally these are the two forms of the same
    path. They come apart when the .blend has been SAVED SOMEWHERE ELSE since
    the image was stamped: the stored `//relative` path now resolves against the
    new location, so it points at a file that is not there.

    ⚠ The relative form is preferred WHEN IT STILL RESOLVES, because that is
    what the user wrote and keeping .blends relocatable is the whole point of
    relative paths. It is only abandoned when following it would produce a
    missing texture — at which point an absolute path that works beats a
    relative one that does not.
    """
    raw = original_of(image)
    stored_abs = image.get(PROP_ORIGINAL_ABS) or ""
    live_abs = _abs_path(image, raw)
    if live_abs and (os.path.isfile(live_abs) or tile_token(live_abs)):
        return raw, live_abs
    if stored_abs and (os.path.isfile(stored_abs) or tile_token(stored_abs)):
        # The relative path has gone stale — almost always a Save As into
        # another folder. Write the absolute one so the image actually loads.
        return stored_abs, stored_abs
    # Neither resolves: the texture itself has been moved or deleted. Hand back
    # the user's own form so the reported path is the one they recognise.
    return raw, live_abs or stored_abs


def check_tampered(image):
    """Has the user re-pointed this image by hand? Then let go of it.

    The stand-in's basename is stored precisely so this is answerable. If the
    filepath no longer ends in the file we put there, somebody chose a different
    image deliberately and the last thing they want is for us to swap it back.
    Returns True when the marks were dropped.
    """
    if not is_managed(image):
        return False
    expected = image.get(PROP_STANDIN)
    if expected and os.path.basename(image.filepath) != expected:
        forget(image)
        return True
    return False


# ---------------------------------------------------------------------------
# Where a stand-in lives
# ---------------------------------------------------------------------------

_SEQUENCE_DIGITS = re.compile(r"\d+")
_TILE_TOKEN = re.compile(r"<(UDIM|UVTILE)>")


def _digest(text):
    """Cache key for an original path. Hashing the PATH (not the pixels) means
    a file that changes on disk keeps its name and is caught by mtime instead —
    which is far cheaper than re-reading every texture to notice nothing moved.
    """
    return hashlib.sha256(text.encode("utf-8", "surrogateescape")).hexdigest()


def split_frame_number(filename):
    """`('walk_0042', '.png')` -> `('walk_', 42, 4, '.png')`, else None.

    Blender picks the LAST run of digits as the frame counter, so `cam2_0007`
    numbers on `0007` and not on the `2`. Scanning from the end is what mirrors
    that; scanning from the front renumbers half of everyone's footage.
    """
    stem, ext = os.path.splitext(filename)
    runs = list(_SEQUENCE_DIGITS.finditer(stem))
    if not runs:
        return None
    last = runs[-1]
    return stem[:last.start()], int(last.group()), len(last.group()), \
        stem[last.end():] + ext


def tile_token(path):
    """`<UDIM>` / `<UVTILE>` in a filepath, or None."""
    found = _TILE_TOKEN.search(os.path.basename(path))
    return found.group(0) if found else None


def standin_path(cache_dir, original_abs, side, frame=None, keep_token=True):
    """The file a stand-in for *original_abs* at *side* px belongs in.

    `<digest>_<side><ext>` — the digest keys the ORIGINAL PATH and the size is in
    the name, so one source can have 512 and 2048 stand-ins side by side and
    switching between them costs nothing.
    """
    base = os.path.basename(original_abs)
    token = tile_token(base)
    if token:
        # A tiled image keeps its token, so Blender still resolves each tile.
        stem, ext = os.path.splitext(base.replace(token, ""))
        stem = stem.rstrip(". _")
        key = os.path.join(os.path.dirname(original_abs), stem + ext)
        name = "%s_%d%s%s" % (_digest(key), side,
                              ("." + token) if keep_token else "", ext)
        return os.path.join(cache_dir, name)
    if frame is not None:
        parts = split_frame_number(base)
        if parts is not None:
            prefix, _number, width, tail = parts
            key = os.path.join(os.path.dirname(original_abs), prefix + tail)
            return os.path.join(
                cache_dir, "%s_%d_%0*d%s" % (_digest(key), side, width, frame,
                                             os.path.splitext(base)[1]))
    ext = os.path.splitext(base)[1]
    return os.path.join(cache_dir, "%s_%d%s" % (_digest(original_abs), side, ext))


def sequence_files(original_abs):
    """Every frame sitting next to *original_abs*, as (path, frame number).

    An image sequence is one datablock pointing at one file, but the render
    reads all of them — so every sibling needs a stand-in or the sequence turns
    half-resolution half way through.
    """
    folder = os.path.dirname(original_abs)
    parts = split_frame_number(os.path.basename(original_abs))
    if parts is None or not os.path.isdir(folder):
        return []
    prefix, _number, width, tail = parts
    pattern = os.path.join(folder, prefix + "[0-9]" * width + tail)
    out = []
    for path in sorted(glob.glob(pattern)):
        found = split_frame_number(os.path.basename(path))
        if found is not None:
            out.append((path, found[1]))
    return out


def tile_files(original_abs):
    """Every UDIM tile actually on disk for a tokenised path."""
    base = os.path.basename(original_abs)
    token = tile_token(base)
    if not token:
        return []
    folder = os.path.dirname(original_abs)
    wild = base.replace(token, "[0-9]" * 4 if token == "<UDIM>" else "u*_v*")
    return sorted(glob.glob(os.path.join(folder, wild)))


# ---------------------------------------------------------------------------
# The resize backend (OpenImageIO — ships inside Blender)
# ---------------------------------------------------------------------------

def _oiio():
    import OpenImageIO
    return OpenImageIO


def read_dimensions(path):
    """(width, height) from a file's HEADER, or None.

    Header-only on purpose: sizing a scene asks this of every texture, and
    decoding pixels to learn a number in the first 32 bytes would make a big
    scene take minutes.
    """
    try:
        oiio = _oiio()
    except ImportError:
        return None
    try:
        handle = oiio.ImageInput.open(path)
    except Exception:                       # noqa: BLE001 - any bad file
        return None
    if handle is None:
        return None
    try:
        spec = handle.spec()
        return int(spec.width), int(spec.height)
    finally:
        handle.close()


def generate_standin(source, destination, side):
    """Write a copy of *source* whose long side is *side*. -> None, or a reason.

    Lanczos3, aspect preserved, same file format and pixel depth as the source.
    Verified 2026-08-04: OIIO's `resize` carries the source spec's attributes
    (ICC profile included) onto the result, so nothing has to be copied by hand.
    """
    try:
        oiio = _oiio()
    except ImportError:
        return "OpenImageIO is not available in this Blender build"
    buf = oiio.ImageBuf(source)
    if not buf.read():
        return (buf.geterror() or "could not read the file").splitlines()[0]
    spec = buf.spec()
    width, height = int(spec.width), int(spec.height)
    if width <= 0 or height <= 0:
        return "the file reports no size"
    longest = max(width, height)
    if side >= longest:
        # Never upscale. The caller treats this as "use the original".
        return "already %d px" % longest
    scale = float(side) / float(longest)
    new_w = max(1, int(round(width * scale)))
    new_h = max(1, int(round(height * scale)))
    roi = oiio.ROI(0, new_w, 0, new_h, 0, 1, 0, spec.nchannels)
    out = oiio.ImageBufAlgo.resize(buf, filtername="lanczos3", roi=roi)
    if out.has_error:
        return (out.geterror() or "resize failed").splitlines()[0]
    folder = os.path.dirname(destination)
    if folder and not os.path.isdir(folder):
        try:
            os.makedirs(folder)
        except OSError as exc:
            return "could not create the cache folder: %s" % exc
    if not out.write(destination):
        return (out.geterror() or "could not write the copy").splitlines()[0]
    return None


def _is_stale(standin, original):
    """A stand-in is stale when it is missing, or no newer than its source.

    `<=` and not `<`: a file copied or checked out in the same second as its
    source would otherwise be trusted forever. Regenerating one texture too
    often is cheap; shipping a render made from a stale one is not.
    """
    if not os.path.isfile(standin):
        return True
    try:
        return os.path.getmtime(standin) <= os.path.getmtime(original)
    except OSError:
        return True


# ---------------------------------------------------------------------------
# One image
# ---------------------------------------------------------------------------

def can_manage(image):
    """(True, None) if this image could take a stand-in, else (False, reason)."""
    if image.packed_file is not None:
        return False, "packed into the .blend"
    if image.source in UNSHRINKABLE_SOURCES:
        return False, "%s images have no file to shrink" % image.source.lower()
    # resolve_original, not `_abs_path(original_of(...))`: for an image already
    # managed in a .blend that has since been saved elsewhere, the raw relative
    # path resolves to nothing and this would report the user's own texture as
    # missing.
    raw, path = resolve_original(image)
    if not raw:
        return False, "no file path"
    ext = os.path.splitext(path)[1].lower()
    if ext not in RESIZABLE_EXTS:
        return False, "%s is not a format we can rewrite" % (ext or "this file")
    if image.source == 'TILED' or tile_token(path):
        return True, None
    if not os.path.isfile(path):
        return False, "the file is missing"
    return True, None


def source_dimensions(image):
    """The ORIGINAL's (w, h) — never the stand-in's.

    ⚠ `image.size` is the size of whatever is loaded RIGHT NOW, which for a
    managed image is the stand-in. Sizing off that would ratchet every image
    smaller on each run.
    """
    path = resolve_original(image)[1]
    token = tile_token(path)
    if token:
        tiles = tile_files(path)
        path = tiles[0] if tiles else path
    dims = read_dimensions(path)
    if dims is not None:
        return dims
    try:
        return int(image.size[0]), int(image.size[1])
    except Exception:                       # noqa: BLE001
        return 0, 0


def image_bytes(image, width=None, height=None):
    """Roughly what this image costs in RAM, decompressed.

    Approximate by design: no mip chains, no GPU-side compression, no render
    buffers. It is for comparing datablocks with each other, not for predicting
    a VRAM figure.
    """
    if width is None or height is None:
        try:
            width, height = int(image.size[0]), int(image.size[1])
        except Exception:                   # noqa: BLE001
            return 0
    depth = int(getattr(image, "depth", 32)) or 32
    return int(width) * int(height) * depth // 8


def set_image_size(image, side, cache_dir, tally=None):
    """Point *image* at a stand-in *side* px on its long edge.

    ALWAYS generated from PROP_ORIGINAL, so this can be called any number of
    times with any sizes and never produces a copy of a copy.
    """
    name = image.name
    check_tampered(image)
    ok, reason = can_manage(image)
    if not ok:
        if tally is not None:
            tally.skipped.append((name, reason))
        return False
    raw_original, original_abs = resolve_original(image)
    side = max(MIN_SIDE, int(side))

    src_w, src_h = source_dimensions(image)
    before = image_bytes(image)
    if src_w and src_h and side >= max(src_w, src_h):
        # Asking for at least what the source already is. If we are currently on
        # a stand-in, that means going back to the original — not making a
        # bigger copy of a smaller one.
        if is_managed(image):
            return revert_image(image, tally=tally)
        if tally is not None:
            tally.unchanged.append(name)
            tally.bytes_before += before
            tally.bytes_after += before
        return False
    if is_managed(image) and managed_size(image) == side and \
            os.path.basename(image.filepath) == image.get(PROP_STANDIN) and \
            os.path.isfile(_abs_path(image)):
        if not _is_stale(_abs_path(image), original_abs):
            if tally is not None:
                tally.unchanged.append(name)
                tally.bytes_before += before
                tally.bytes_after += before
            return False

    target = standin_path(cache_dir, original_abs, side)
    problem = _build_all_parts(image, original_abs, cache_dir, side, target)
    if problem is not None:
        # Generation failed. If the image is on a stand-in right now, put it back
        # on the original rather than leaving it pointing at a file we could not
        # write — a missing texture is a much worse outcome than a large one.
        if is_managed(image):
            revert_image(image)
        if tally is not None:
            tally.failed.append((name, problem))
        return False

    _point_at(image, raw_original, target)
    _stamp(image, raw_original, target, side)
    image.reload()
    if tally is not None:
        tally.changed.append(name)
        tally.bytes_before += before
        after_w, after_h = _scaled_dimensions(src_w, src_h, side)
        tally.bytes_after += image_bytes(image, after_w, after_h)
    return True


def _scaled_dimensions(width, height, side):
    if not width or not height:
        return width, height
    longest = max(width, height)
    if side >= longest:
        return width, height
    scale = float(side) / float(longest)
    return max(1, int(round(width * scale))), max(1, int(round(height * scale)))


def _build_all_parts(image, original_abs, cache_dir, side, target):
    """Generate every file this image needs. -> None, or a reason.

    A plain image is one file. A sequence is one per frame on disk, and a UDIM
    set is one per tile — Blender resolves those itself at render time, so they
    all have to exist before the filepath is swapped.
    """
    token = tile_token(original_abs)
    if token:
        tiles = tile_files(original_abs)
        if not tiles:
            return "no tiles found on disk"
        for tile in tiles:
            # Each tile keeps its own number in the same slot the source used,
            # so Blender's own <UDIM> resolution still finds every one of them.
            tile_target = _tile_destination(cache_dir, original_abs, side,
                                            tile, token)
            problem = _ensure_one(tile, tile_target, side)
            if problem is not None:
                return problem
        return None
    if image.source == 'SEQUENCE':
        frames = sequence_files(original_abs)
        if not frames:
            return "no frames found next to that file"
        for path, number in frames:
            problem = _ensure_one(
                path, standin_path(cache_dir, original_abs, side, frame=number),
                side)
            if problem is not None:
                return problem
        return None
    return _ensure_one(original_abs, target, side)


def expected_standin(image, original_abs, cache_dir, side):
    """ONE cached file that must exist if this image is really at `side`.

    ⚠ Deliberately next to `_build_all_parts`, and it has to be kept in step
    with it: a plain image is one file, but a SEQUENCE is one file per frame
    and a UDIM set is one per tile, so for those the path built from the bare
    original never exists. Checking that path reported every sequence and every
    tiled image as permanently "cache missing" — which is exactly the warning
    that is supposed to mean something.

    Returns "" when the image has no parts on disk to point at.
    """
    token = tile_token(original_abs)
    if token:
        tiles = tile_files(original_abs)
        return (_tile_destination(cache_dir, original_abs, side, tiles[0],
                                  token) if tiles else "")
    if image is not None and image.source == 'SEQUENCE':
        frames = sequence_files(original_abs)
        return (standin_path(cache_dir, original_abs, side, frame=frames[0][1])
                if frames else "")
    return standin_path(cache_dir, original_abs, side)


def _tile_destination(cache_dir, original_abs, side, tile_path, token):
    """Where one real tile's stand-in goes, keeping the tile's own number."""
    template = standin_path(cache_dir, original_abs, side, keep_token=True)
    source_base = os.path.basename(original_abs)
    tile_base = os.path.basename(tile_path)
    # What the token stands for in THIS tile, e.g. "1002" or "u1_v2".
    prefix, _sep, suffix = source_base.partition(token)
    number = tile_base[len(prefix):len(tile_base) - len(suffix)] or "1001"
    return os.path.join(os.path.dirname(template),
                        os.path.basename(template).replace(token, number))


def _ensure_one(source, destination, side):
    """Generate one stand-in if it is missing or stale. -> None, or a reason."""
    if not os.path.isfile(source):
        return "the file is missing"
    if not _is_stale(destination, source):
        return None
    return generate_standin(source, destination, side)


def revert_image(image, tally=None):
    """Put the image back on the user's own file. Stand-ins stay in the cache.

    Leaving them there is deliberate: re-optimising later then costs nothing,
    and nothing this module does should ever delete a file it did not just
    write.
    """
    name = image.name
    if not is_managed(image):
        if tally is not None:
            tally.unchanged.append(name)
        return False
    before = image_bytes(image)
    # ⚠ NOT `image.get(PROP_ORIGINAL)` straight. After a Save As into another
    # folder the stored `//relative` path resolves somewhere else entirely, and
    # writing it back would hand the user a missing texture with no record left
    # of where the real one is. See resolve_original().
    original, _abs = resolve_original(image)
    forget(image)
    image.filepath = original
    image.reload()
    if tally is not None:
        tally.changed.append(name)
        tally.bytes_before += before
        tally.bytes_after += image_bytes(image)
    return True


def regenerate_image(image, cache_dir=None, tally=None):
    """Re-make a missing or out-of-date stand-in for an already-managed image.

    `cache_dir=None` means "wherever the image is already pointing", which is
    what makes a .blend portable: opened on another machine the folder may be
    somewhere else entirely, and re-homing it is not this function's business.
    Passing a folder forces the move, which is what "Check & Regenerate" does
    after the cache folder is changed.
    """
    name = image.name
    if check_tampered(image) or not is_managed(image):
        if tally is not None:
            tally.skipped.append((name, "not managed by the optimizer"))
        return False
    side = managed_size(image)
    _raw, original_abs = resolve_original(image)
    if not os.path.isfile(original_abs) and not tile_token(original_abs):
        if tally is not None:
            tally.failed.append((name, "the original file is missing"))
        return False
    folder = cache_dir or os.path.dirname(_abs_path(image))
    if not folder or not os.path.isdir(folder):
        if tally is not None:
            tally.failed.append(
                (name, "the cache folder does not exist: %s" % (folder or "?")))
        return False
    target = standin_path(folder, original_abs, side)
    if cache_dir is None and not _is_stale(_abs_path(image), original_abs):
        if tally is not None:
            tally.unchanged.append(name)
        return False
    problem = _build_all_parts(image, original_abs, folder, side, target)
    if problem is not None:
        if tally is not None:
            tally.failed.append((name, problem))
        return False
    raw_original = resolve_original(image)[0]
    _point_at(image, raw_original, target)
    _stamp(image, raw_original, target, side)
    image.reload()
    if tally is not None:
        tally.changed.append(name)
    return True


# A file this module could have written, and nothing else. Every name
# `standin_path` produces is `<64 hex>_<side>` plus, for a sequence, `_<frame>`,
# plus for a UDIM set either the `<UDIM>`/`<UVTILE>` token or the real tile
# number, and then the extension.
_STANDIN_NAME = re.compile(
    r"^[0-9a-f]{64}_\d+(?:_\d+)?(?:\.(?:<UDIM>|<UVTILE>|\d{4}|u\d+_v\d+))?"
    r"\.[^.]+$")


def clear_cache(cache_dir=None, tally=None):
    """Empty the cache folder of stand-ins. Returns what it did.

    ⚠ TWO RULES HERE, AND BOTH ARE LOAD-BEARING.

    1. EVERY MANAGED IMAGE IS PUT BACK FIRST. The images point AT the files
       about to be deleted, so deleting them underneath a live scene turns
       every optimized texture pink. Someone clearing a cache is asking for
       disk space back, not for their materials to break, so the revert is not
       optional and not a checkbox.

    2. ONLY FILES MATCHING OUR OWN NAMING SCHEME ARE DELETED. The cache folder
       is a path the user can type into a box, and one day somebody will point
       it at a real folder of theirs. Everything else in there is counted and
       left exactly where it is — this must be able to do nothing rather than
       do harm.

    Texture sets are deliberately NOT deleted. A set is a note about sizes, its
    files are regenerated from the originals on demand, and `group_state`
    already reports the missing count honestly.
    """
    folder = cache_dir or default_cache_dir()
    managed = [im for im in bpy.data.images if is_managed(im)]
    _progress_phase("Restoring textures", len(managed))
    for image in managed:
        _progress_step(image.name)
        revert_image(image, tally=tally)

    removed = 0
    freed = 0
    kept = 0
    failed = []
    if os.path.isdir(folder):
        names = sorted(os.listdir(folder))
        _progress_phase("Clearing the cache", len(names))
        for name in names:
            _progress_step(name)
            path = os.path.join(folder, name)
            if not os.path.isfile(path) or not _STANDIN_NAME.match(name):
                kept += 1
                continue
            try:
                size = os.path.getsize(path)
                os.remove(path)
            except OSError as exc:
                failed.append((name, str(exc)))
                continue
            removed += 1
            freed += size
    return {"folder": folder, "removed": removed, "bytes": freed,
            "bytes_human": human_bytes(freed), "kept": kept,
            "restored": len(managed),
            "failed": [{"name": n, "reason": r} for n, r in failed]}


def _point_at(image, raw_original, target):
    """Aim the datablock at *target*, matching how the original was written.

    ⚠ `bpy.path.relpath` RAISES in a file that has never been saved — there is
    no `//` to be relative to. An unsaved file with a relative original is odd
    but perfectly possible (linked data, or a file opened and not yet re-saved),
    and an exception here would abort a whole batch over a cosmetic choice. The
    absolute path is always correct; only its tidiness is at stake.
    """
    if raw_original.startswith("//"):
        try:
            image.filepath = bpy.path.relpath(target)
            return
        except (ValueError, RuntimeError):
            pass
    image.filepath = target


# ---------------------------------------------------------------------------
# Which datablocks a run acts on
# ---------------------------------------------------------------------------

def target_objects(target, context=None):
    context = context or bpy.context
    if target == "SELECTED":
        return [ob for ob in context.selected_objects]
    if target == "SCENE":
        return list(context.scene.objects)
    if target == "ALL_OBJECTS":
        return list(bpy.data.objects)
    return []


def target_images(target, context=None):
    """Every image a run should consider, for the image-set targets."""
    context = context or bpy.context
    if target in OBJECT_TARGETS:
        found = set()
        for ob in target_objects(target, context):
            found.update(images_for_object(ob))
        return sorted(found, key=lambda im: im.name)
    pool = list(bpy.data.images)
    if target == "ALL_IMAGES":
        return pool
    hdr = [im for im in pool
           if os.path.splitext(original_of(im))[1].lower() in HDR_EXTS]
    if target == "IMAGES_HDR":
        return hdr
    hdr_set = set(hdr)
    return [im for im in pool if im not in hdr_set]


def images_for_names(names, missing=None):
    """Every image used by the NAMED objects — a queued job's own object list.

    ⚠ THIS IS WHAT MAKES A QUEUE OF SEVERAL JOBS MEAN ANYTHING. "SELECTED" is
    resolved when a run STARTS, so two jobs both queued as "the selected
    objects" both see whatever happens to be selected by the time Run is
    pressed. The second then re-sizes the first one's images from their
    originals and wins outright, so queuing two different selections produced
    one result at one size. Marty hit exactly that: "after queing two jobs it
    only gave me one entry i can switch on".

    A queued job therefore carries the object names it was queued WITH, and a
    job that carries them never consults the live selection again.
    """
    found = set()
    for name in names or ():
        obj = bpy.data.objects.get(name)
        if obj is None:
            # Queued, then deleted before the run. Reported rather than
            # ignored: a job quietly doing less than it was asked to is how a
            # half-optimized scene gets blamed on the optimizer.
            if missing is not None:
                missing.append(name)
            continue
        found.update(images_for_object(obj))
    return sorted(found, key=lambda im: im.name)


# ---------------------------------------------------------------------------
# Which images an object actually uses
# ---------------------------------------------------------------------------

def _walk_tree(tree, images, seen):
    """Collect images from a node tree, recursing into groups.

    ⚠ `hasattr(node, "image")` on purpose, rather than a list of node types.
    That is what makes third-party render engines (Octane, and anything else
    with its own image node) work without knowing anything about them.
    """
    if tree is None or tree.as_pointer() in seen:
        return
    seen.add(tree.as_pointer())
    for node in tree.nodes:
        image = getattr(node, "image", None)
        if isinstance(image, bpy.types.Image):
            images.add(image)
        # ⚠ A GEOMETRY NODES image texture has NO `.image` PROPERTY — the image
        # arrives on an input SOCKET. Checking only `node.image` finds every
        # shader texture and not one geonode texture, which would leave the
        # whole point of scanning GN trees quietly doing nothing. Sockets are
        # also how a Group node carries an image into a nested tree.
        for socket in node.inputs:
            value = getattr(socket, "default_value", None)
            if isinstance(value, bpy.types.Image):
                images.add(value)
        _walk_tree(getattr(node, "node_tree", None), images, seen)


def modifier_inputs(mod):
    """Every value fed to a geometry-nodes modifier's own inputs.

    ⚠ **Blender moved these.** Up to 4.x they were ID properties on the modifier
    (`mod[identifier]`); in 5.x they live on `mod.properties.inputs`, and the old
    form raises *"this type doesn't support IDProperties"*. The manifest still
    allows 4.2, so both are read — and `assets.py` hit exactly this when it
    started writing modifier values.

    ⚠ `keys()` RAISES rather than returning nothing when there are no inputs at
    all, so one image-free geonode modifier anywhere in the scene would abort
    the whole scan if this were not guarded.

    ⚠ **Each 5.x entry is an IDPropertyGroup, not the value.** It holds `value`
    and `type` (and `attribute_name` for the ones that can be driven by an
    attribute), so the value has to be unwrapped. Reading the entry itself finds
    an IDPropertyGroup and never an Image — and a test that *writes* the image
    the short way builds a shape Blender itself never produces, so it would pass
    while real files found nothing.
    """
    holder = getattr(getattr(mod, "properties", None), "inputs", None)
    if holder is None:
        holder = mod                        # Blender 4.x: plain ID properties
    try:
        keys = list(holder.keys())
    except TypeError:
        return []
    values = []
    for key in keys:
        try:
            entry = holder[key]
        except (KeyError, TypeError):
            continue
        if hasattr(entry, "keys"):
            try:
                if "value" in entry.keys():
                    entry = entry["value"]
            except (KeyError, TypeError):
                continue
        values.append(entry)
    return values


def images_for_object(obj, images=None, seen=None, depth=0):
    """Every image this object puts on screen.

    Materials and their node groups, particle instances, collection instances —
    and, unlike the tool this replaces, **geometry nodes**: both image texture
    nodes inside a GN tree and Image sockets fed on the modifier itself. Marty's
    scenes are geonode-heavy, so without that half his textures would look
    unused and get sized as if nothing referenced them.
    """
    images = set() if images is None else images
    seen = set() if seen is None else seen
    if obj is None or depth > _INSTANCE_DEPTH:
        return images

    for slot in obj.material_slots:
        material = slot.material
        if material is not None and material.use_nodes:
            _walk_tree(material.node_tree, images, seen)

    data = getattr(obj, "data", None)
    for material in (getattr(data, "materials", None) or []):
        if material is not None and material.use_nodes:
            _walk_tree(material.node_tree, images, seen)

    for mod in obj.modifiers:
        if mod.type != 'NODES':
            continue
        _walk_tree(getattr(mod, "node_group", None), images, seen)
        # An image fed straight into the modifier never appears in the tree.
        for value in modifier_inputs(mod):
            if isinstance(value, bpy.types.Image):
                images.add(value)

    for psys in getattr(obj, "particle_systems", []):
        settings = psys.settings
        if settings is None:
            continue
        images_for_object(getattr(settings, "instance_object", None),
                          images, seen, depth + 1)
        for child in (getattr(settings, "instance_collection", None)
                      and settings.instance_collection.all_objects or []):
            images_for_object(child, images, seen, depth + 1)

    collection = getattr(obj, "instance_collection", None)
    if collection is not None:
        for child in collection.all_objects:
            images_for_object(child, images, seen, depth + 1)

    return images


# ---------------------------------------------------------------------------
# How big the object lands on screen
# ---------------------------------------------------------------------------

def world_corners(obj, parent=None, out=None, depth=0):
    """The 8 corners of the object's world-space bounding box.

    ⚠ Walked by hand rather than read off `bound_box`, because **an instancer's
    own bound_box is all zeros**. A collection-instance empty that fills the
    frame would otherwise measure as a point and every texture in it would come
    back at the minimum size.
    """
    out = [] if out is None else out
    if obj is None or depth > _INSTANCE_DEPTH:
        return out
    world = obj.matrix_world if parent is None else parent @ obj.matrix_world
    collection = getattr(obj, "instance_collection", None)
    if collection is not None:
        base = world @ Matrix.Translation(-collection.instance_offset)
        for child in collection.objects:
            world_corners(child, base, out, depth + 1)
        return out
    box = obj.bound_box
    if all(abs(v) < 1e-9 for corner in box for v in corner):
        return out                       # cameras, lights, plain empties
    for corner in box:
        out.append(world @ Vector(corner))
    return out


def project_corners(scene, camera, corners):
    """Screen bounds + nearest depth for a set of world points, or None.

    None means "contributes nothing to this frame" — entirely behind the camera
    or entirely off one edge. The caller gives those the MINIMUM size rather
    than zero: an object out of frame can still show up in a reflection, a
    shadow or a bounce, and a 32 px texture in a mirror is a visible bug.
    """
    if not corners:
        return None
    render = scene.render
    scale = render.resolution_percentage / 100.0
    res_x = render.resolution_x * scale
    res_y = render.resolution_y * scale
    min_x = min_y = float("inf")
    max_x = max_y = float("-inf")
    near = float("inf")
    in_front = False
    for point in corners:
        view = world_to_camera_view(scene, camera, point)
        x, y, depth = view.x, view.y, view.z
        if depth < 0.0:
            # Blender mirrors the frame behind the camera; without flipping,
            # a point behind and to the left reads as in front and to the right.
            x, y = -x, -y
        else:
            in_front = True
        px = x * res_x
        py = (1.0 - y) * res_y
        min_x = min(min_x, px)
        max_x = max(max_x, px)
        min_y = min(min_y, py)
        max_y = max(max_y, py)
        near = min(near, depth)
    if not in_front:
        return None
    if max_x < 0.0 or min_x > res_x or max_y < 0.0 or min_y > res_y:
        return None
    return {"min_x": min_x, "min_y": min_y, "max_x": max_x, "max_y": max_y,
            "width": max_x - min_x, "height": max_y - min_y,
            "depth": max(0.0, near)}


def next_power_of_two(value):
    value = int(value)
    if value <= 1:
        return 1
    return 1 << (value - 1).bit_length()


def size_for_bounds(bounds, quality, min_size, max_size):
    """Screen bounds -> the texture size that object deserves."""
    if bounds is None:
        return int(min_size)
    largest = max(bounds["width"], bounds["height"])
    side = int(round(largest * float(quality)))
    side = max(int(min_size), min(int(max_size), side))
    return max(MIN_SIDE, next_power_of_two(side))


def decimate_ratio_for(depth, full_distance, low_distance, low_ratio):
    """Distance -> decimate ratio, on a straight ramp between the two dials."""
    if depth <= full_distance:
        return 1.0
    if low_distance <= full_distance or depth >= low_distance:
        return float(low_ratio)
    span = (depth - full_distance) / (low_distance - full_distance)
    return 1.0 + span * (float(low_ratio) - 1.0)


# ---------------------------------------------------------------------------
# The adaptive pass
# ---------------------------------------------------------------------------

def _accumulate_frame(scene, camera, objects, sizes, depths, quality,
                      min_size, max_size):
    """One frame's contribution to the image sizes and the per-mesh distances."""
    for obj in objects:
        _progress_step(obj.name)
        bounds = project_corners(scene, camera, world_corners(obj))
        side = size_for_bounds(bounds, quality, min_size, max_size)
        for image in images_for_object(obj):
            # A shared texture takes the LARGEST claim on it. Raising only is the
            # whole rule — a far wall must never shrink the close-up's skin.
            if sizes.get(image.name, 0) < side:
                sizes[image.name] = side
        data = getattr(obj, "data", None)
        if bounds is not None and isinstance(data, bpy.types.Mesh):
            near = bounds["depth"]
            if data.name not in depths or near < depths[data.name]:
                depths[data.name] = near


def plan_adaptive(target="SCENE", quality=DEFAULT_QUALITY,
                  min_size=DEFAULT_MIN_SIZE, max_size=DEFAULT_MAX_SIZE,
                  animation=False, frame_step=1, context=None):
    """Work out what every image and mesh SHOULD be, without changing anything.

    Split from the applying half on purpose: the preview overlay shows exactly
    what a run would do, and it can only promise that by calling the same
    function the run calls.
    """
    context = context or bpy.context
    scene = context.scene
    camera = scene.camera
    if camera is None:
        raise RuntimeError(
            "This scene has no active camera - the optimizer measures how big "
            "things look through it.")
    objects = [ob for ob in target_objects(target, context)
               if ob.type != 'CAMERA']
    sizes = {}
    depths = {}
    if animation:
        # "Closest appearance wins": an object that walks up to the camera on
        # frame 90 must be sized for frame 90, not for frame 1.
        start = scene.frame_current
        step = max(1, int(frame_step))
        frames = len(range(scene.frame_start, scene.frame_end + 1, step))
        _progress_phase("Measuring the animation", len(objects) * frames)
        try:
            for frame in range(scene.frame_start, scene.frame_end + 1, step):
                scene.frame_set(frame)
                context.view_layer.update()
                _accumulate_frame(scene, camera, objects, sizes, depths,
                                  quality, min_size, max_size)
        finally:
            scene.frame_set(start)
            context.view_layer.update()
    else:
        _progress_phase("Measuring the scene", len(objects))
        context.view_layer.update()
        _accumulate_frame(scene, camera, objects, sizes, depths, quality,
                          min_size, max_size)

    # Never ask for more than the file actually holds.
    # ⚠ Counted as its own stage, not folded into the measuring one: it reads
    # every source image's HEADER OFF DISK, so on a scene of 400 textures it is
    # seconds of work that would otherwise sit at 100% looking wedged.
    _progress_phase("Reading texture sizes", len(sizes))
    capped = {}
    for name, side in sizes.items():
        _progress_step(name)
        image = bpy.data.images.get(name)
        if image is None:
            continue
        ok, _reason = can_manage(image)
        if not ok:
            capped[name] = side
            continue
        src_w, src_h = source_dimensions(image)
        longest = max(src_w, src_h)
        capped[name] = min(side, longest) if longest else side
    return {"images": capped, "depths": depths,
            "objects": [ob.name for ob in objects], "camera": camera.name}


# ---------------------------------------------------------------------------
# Mesh decimation
# ---------------------------------------------------------------------------

def _managed_modifier(obj):
    return obj.modifiers.get(DECIMATE_MOD)


def clear_decimation(objects, tally=None):
    """Take our Decimate off. Anything else in the stack is left exactly alone."""
    _progress_phase("Removing decimation", len(objects))
    for obj in objects:
        _progress_step(obj.name)
        mod = _managed_modifier(obj)
        if mod is None:
            if tally is not None:
                tally.unchanged.append(obj.name)
            continue
        obj.modifiers.remove(mod)
        if tally is not None:
            tally.changed.append(obj.name)
    return tally


def plan_decimation(objects, depths, face_floor=DEFAULT_FACE_FLOOR,
                    full_distance=DEFAULT_FULL_DISTANCE,
                    low_distance=DEFAULT_LOW_DISTANCE,
                    low_ratio=DEFAULT_LOW_RATIO):
    """mesh name -> ratio, for meshes that should be decimated.

    Decided PER MESH DATABLOCK, not per object: two objects sharing a mesh
    cannot each want a different ratio, and whichever ran last would win
    silently. The nearest user decides for all of them.

    ⚠ **One linked user vetoes the whole mesh.** A modifier stack on a linked
    object cannot be edited, so decimating for the local users only would leave
    the same mesh at two different densities depending on which object you look
    through.
    """
    users = {}
    for obj in objects:
        data = getattr(obj, "data", None)
        if not isinstance(data, bpy.types.Mesh):
            continue
        users.setdefault(data.name, []).append(obj)

    plan = {}
    for name, objs in users.items():
        mesh = bpy.data.meshes.get(name)
        if mesh is None or len(mesh.polygons) < int(face_floor):
            continue
        if mesh.library is not None or any(ob.library is not None for ob in objs):
            continue
        depth = depths.get(name)
        if depth is None:
            continue
        ratio = decimate_ratio_for(depth, full_distance, low_distance, low_ratio)
        if ratio >= 1.0:
            continue
        plan[name] = ratio
    return plan


def apply_decimation(objects, plan, tally=None):
    """Put the plan on the objects — and SWEEP the ones no longer in it.

    ⚠ The sweep is the whole point of this function's shape. Applying only what
    is in the plan leaves a stale modifier on anything that has since moved
    closer, dropped below the face floor or left the target set: it would keep
    the decimation from a run that no longer applies, invisibly, forever. So the
    loop is over the TARGET OBJECTS, not over the plan.
    """
    _progress_phase("Decimating meshes", len(objects))
    for obj in objects:
        _progress_step(obj.name)
        data = getattr(obj, "data", None)
        ratio = plan.get(data.name) if isinstance(data, bpy.types.Mesh) else None
        mod = _managed_modifier(obj)
        if ratio is None:
            if mod is not None:
                obj.modifiers.remove(mod)
                if tally is not None:
                    tally.changed.append(obj.name)
            elif tally is not None:
                tally.unchanged.append(obj.name)
            continue
        if obj.library is not None:
            if tally is not None:
                tally.skipped.append((obj.name, "linked from another file"))
            continue
        if mod is None:
            mod = obj.modifiers.new(DECIMATE_MOD, 'DECIMATE')
            if mod is None:
                if tally is not None:
                    tally.skipped.append((obj.name, "not a mesh object"))
                continue
        mod.decimate_type = 'COLLAPSE'
        if abs(mod.ratio - ratio) < 1e-6:
            if tally is not None:
                tally.unchanged.append(obj.name)
            continue
        mod.ratio = ratio
        if tally is not None:
            tally.changed.append(obj.name)
    return tally


# ---------------------------------------------------------------------------
# Memory estimate
# ---------------------------------------------------------------------------

_ATTR_BYTES = {
    "FLOAT": 4, "INT": 4, "INT8": 1, "INT32_2D": 8, "FLOAT_VECTOR": 12,
    "FLOAT_COLOR": 16, "BYTE_COLOR": 4, "STRING": 16, "BOOLEAN": 1,
    "FLOAT2": 8, "QUATERNION": 16, "FLOAT4X4": 64,
}


def mesh_bytes(mesh):
    """About what a mesh costs in RAM. Approximate, and labelled as such.

    Counts what actually scales: positions, the edge/corner/face topology, UVs,
    colour layers and any custom attributes. `.`-prefixed attributes are
    Blender's own internal ones and `position` is already counted, so including
    them would double the biggest number in the sum.
    """
    verts = len(mesh.vertices)
    total = verts * 12
    if getattr(mesh, "has_custom_normals", False):
        total += verts * 12
    total += len(mesh.edges) * 8
    total += len(mesh.loops) * 8
    total += len(mesh.polygons) * 8
    for layer in mesh.uv_layers:
        total += len(mesh.loops) * 8
    for attribute in mesh.attributes:
        if attribute.name.startswith(".") or attribute.name == "position":
            continue
        if attribute.name in {uv.name for uv in mesh.uv_layers}:
            continue
        size = _ATTR_BYTES.get(attribute.data_type, 4)
        try:
            total += size * len(attribute.data)
        except (AttributeError, TypeError):
            pass
    return total


def estimate_memory(context=None, limit=200):
    """What is eating the memory in this scene, biggest first.

    Walks the render-visible scene rather than the whole file — an object hidden
    from the render costs nothing, and listing it would send people optimising
    things that are not in the picture.

    ⚠ **This is an estimate, not a measurement.** No mip chains, no GPU-side
    compression, no render buffers. Comparing two rows is meaningful; treating
    the total as a VRAM figure is not.
    """
    context = context or bpy.context
    scene = context.scene
    rows = {}
    seen = set()

    def add(kind, datablock, size):
        key = (kind, datablock.name)
        if key in rows:
            return
        rows[key] = {"kind": kind, "name": datablock.name, "bytes": int(size)}

    images = set()
    tree_seen = set()

    def visit(obj, depth=0):
        if obj is None or depth > _INSTANCE_DEPTH:
            return
        pointer = obj.as_pointer()
        if pointer in seen or obj.hide_render:
            return
        seen.add(pointer)
        data = getattr(obj, "data", None)
        if isinstance(data, bpy.types.Mesh):
            add("Mesh", data, mesh_bytes(data))
        images.update(images_for_object(obj))
        collection = getattr(obj, "instance_collection", None)
        if collection is not None:
            for child in collection.all_objects:
                visit(child, depth + 1)
        for child in obj.children:
            visit(child, depth + 1)

    _progress_phase("Measuring memory", len(scene.objects))
    for obj in scene.objects:
        _progress_step(obj.name)
        visit(obj)

    world = scene.world
    if world is not None and world.use_nodes:
        _walk_tree(world.node_tree, images, tree_seen)

    for image in images:
        add("Image", image, image_bytes(image))

    ordered = sorted(rows.values(), key=lambda r: r["bytes"], reverse=True)
    total = sum(r["bytes"] for r in ordered)
    for row in ordered:
        row["share"] = (row["bytes"] / total) if total else 0.0
        row["human"] = human_bytes(row["bytes"])
    out = {"rows": ordered[:int(limit)], "total_bytes": total,
           "total_human": human_bytes(total), "counted": len(ordered),
           "shown": min(len(ordered), int(limit))}
    out.update(vram_estimate(scene, total))
    return out


# How much a GPU render needs ON TOP of the scene data itself.
#
# ⚠ EVERY NUMBER BELOW IS A RULE OF THUMB, and the UI has to keep saying so.
# What actually lands in VRAM depends on the render engine's build, the driver,
# what else is on the card and whether textures get compressed on upload — none
# of which is knowable from inside a .blend. What this IS good for is the
# question Marty asked: roughly how much headroom does this scene need, and
# does rendering from the command line need less than rendering here.
#
# Cycles keeps BVH acceleration structures alongside the geometry; the usual
# working figure is around half the mesh data again.
_BVH_OVERHEAD = 0.5
# Bytes per pixel of render buffer, per sample-accumulating pass: RGBA float.
_BUFFER_BPP = 16
# Cycles holds tile/accumulation buffers as well as the final result. Three
# frames' worth is the conservative end of what has been measured.
_BUFFER_COPIES = 3
# What Blender's own interface costs on the GPU before a render starts: the
# viewport's own copy of the scene, the UI, and the OS compositor's share.
# ⚠ THIS IS THE WHOLE DIFFERENCE between the two figures Marty asked for. A
# background render (`blender -b`, which is what the Render Queue runs) opens no
# window and draws no viewport, so it simply does not pay this.
_UI_OVERHEAD_BYTES = 700 * 1024 * 1024


def vram_estimate(scene, data_bytes):
    """Roughly what a render of this scene needs on the GPU.

    Two figures, because they genuinely differ and the difference is the reason
    the Render Queue exists: rendering **from inside Blender** carries the
    viewport and the interface on the card as well, while a **command-line**
    render (`blender -b`) has no window at all.
    """
    render = scene.render
    scale = max(0.0, render.resolution_percentage) / 100.0
    width = int(render.resolution_x * scale)
    height = int(render.resolution_y * scale)
    buffers = width * height * _BUFFER_BPP * _BUFFER_COPIES

    # Meshes need their acceleration structure; images do not.
    mesh_bytes_total = sum(
        mesh_bytes(m) for m in bpy.data.meshes if m.users)
    bvh = int(mesh_bytes_total * _BVH_OVERHEAD)

    headless = int(data_bytes + bvh + buffers)
    interactive = int(headless + _UI_OVERHEAD_BYTES)
    return {
        "vram": {
            "headless_bytes": headless,
            "headless_human": human_bytes(headless),
            "interactive_bytes": interactive,
            "interactive_human": human_bytes(interactive),
            "buffer_bytes": buffers,
            "buffer_human": human_bytes(buffers),
            "bvh_bytes": bvh,
            "bvh_human": human_bytes(bvh),
            "ui_bytes": _UI_OVERHEAD_BYTES,
            "ui_human": human_bytes(_UI_OVERHEAD_BYTES),
            "resolution": [width, height],
            "engine": render.engine,
        }
    }


# ---------------------------------------------------------------------------
# The preview overlay (a modal operator + a POST_PIXEL draw handler)
# ---------------------------------------------------------------------------

def _draw_preview():
    """Paint what a run WOULD do, next to each object it would touch."""
    if not _preview["running"]:
        return
    try:
        import blf
        from bpy_extras.view3d_utils import location_3d_to_region_2d
    except ImportError:
        return
    context = bpy.context
    region = context.region
    rv3d = getattr(context.space_data, "region_3d", None)
    if region is None or rv3d is None:
        return
    font = 0
    blf.size(font, 12)
    for row in _preview["rows"]:
        anchor = location_3d_to_region_2d(region, rv3d, Vector(row["at"]))
        if anchor is None:
            continue
        y = anchor.y
        for line, colour in row["lines"]:
            blf.color(font, colour[0], colour[1], colour[2], 1.0)
            blf.position(font, anchor.x, y, 0.0)
            blf.draw(font, line)
            y -= 14
    if _preview["note"]:
        blf.color(font, 1.0, 1.0, 1.0, 1.0)
        blf.position(font, 20, 30, 0.0)
        blf.draw(font, _preview["note"])


def _enable_overlay():
    """Add the draw handler, clearing any handle a reload stranded first."""
    namespace = bpy.app.driver_namespace
    stale = namespace.get(_DRAW_KEY)
    if stale is not None:
        try:
            bpy.types.SpaceView3D.draw_handler_remove(stale, 'WINDOW')
        except (ValueError, TypeError):
            pass
        namespace.pop(_DRAW_KEY, None)
    namespace[_DRAW_KEY] = bpy.types.SpaceView3D.draw_handler_add(
        _draw_preview, (), 'WINDOW', 'POST_PIXEL')


def _disable_overlay():
    namespace = bpy.app.driver_namespace
    handle = namespace.pop(_DRAW_KEY, None)
    if handle is not None:
        try:
            bpy.types.SpaceView3D.draw_handler_remove(handle, 'WINDOW')
        except (ValueError, TypeError):
            pass


def _tag_view3d(context=None):
    context = context or bpy.context
    window = getattr(context, "window", None)
    screens = [window.screen] if window is not None else []
    for screen in screens or bpy.data.screens:
        for area in screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()


def build_preview_rows(target="SCENE", quality=DEFAULT_QUALITY,
                       min_size=DEFAULT_MIN_SIZE, max_size=DEFAULT_MAX_SIZE,
                       meshes=True, face_floor=DEFAULT_FACE_FLOOR,
                       full_distance=DEFAULT_FULL_DISTANCE,
                       low_distance=DEFAULT_LOW_DISTANCE,
                       low_ratio=DEFAULT_LOW_RATIO, context=None):
    """The overlay's text, computed ONCE — the same plan the run would use."""
    context = context or bpy.context
    plan = plan_adaptive(target=target, quality=quality, min_size=min_size,
                         max_size=max_size, context=context)
    objects = [bpy.data.objects[name] for name in plan["objects"]
               if name in bpy.data.objects]
    decimation = plan_decimation(
        objects, plan["depths"], face_floor=face_floor,
        full_distance=full_distance, low_distance=low_distance,
        low_ratio=low_ratio) if meshes else {}

    rows = []
    for obj in objects:
        corners = world_corners(obj)
        if not corners:
            continue
        centre = sum(corners, Vector((0.0, 0.0, 0.0))) / len(corners)
        lines = []
        for image in sorted(images_for_object(obj), key=lambda im: im.name):
            wanted = plan["images"].get(image.name)
            if wanted is None:
                continue
            ok, reason = can_manage(image)
            if not ok:
                lines.append(("%s  -  %s" % (image.name, reason),
                              (1.0, 0.55, 0.35)))
                continue
            src_w, src_h = source_dimensions(image)
            longest = max(src_w, src_h)
            if not longest or wanted >= longest:
                lines.append(("%s  %dpx  -  left alone" % (image.name, longest),
                              (0.7, 0.75, 0.8)))
            else:
                lines.append(
                    ("%s  %dpx -> %dpx  (%d%%)"
                     % (image.name, longest, wanted,
                        round(100.0 * wanted / longest)), (0.55, 0.85, 0.6)))
        data = getattr(obj, "data", None)
        if isinstance(data, bpy.types.Mesh) and data.name in decimation:
            lines.append(("Mesh  -  %d%% decimation"
                          % round(100.0 * (1.0 - decimation[data.name])),
                          (0.6, 0.75, 1.0)))
        if lines:
            rows.append({"object": obj.name, "at": tuple(centre),
                         "lines": lines})
    return rows


class MADI_OT_optimizer_preview(Operator):
    """Show what optimising would do to everything in view, without doing it"""
    bl_idname = "madi_optimizer.preview"
    bl_label = "Preview Optimization"

    @classmethod
    def poll(cls, context):
        return context.scene.camera is not None

    def invoke(self, context, event):
        if context.area is None or context.area.type != 'VIEW_3D':
            self.report({'ERROR'}, "The preview draws in a 3D Viewport.")
            return {'CANCELLED'}
        try:
            rows = build_preview_rows(context=context, **_preview.get("args", {}))
        except RuntimeError as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        _preview["rows"] = rows
        _preview["note"] = "Optimization preview - press Esc to close"
        _preview["running"] = True
        _enable_overlay()
        context.window_manager.modal_handler_add(self)
        _tag_view3d(context)
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        if not _preview["running"]:
            return self._finish(context)
        if event.type in {'ESC', 'RIGHTMOUSE'} and event.value == 'PRESS':
            return self._finish(context)
        return {'PASS_THROUGH'}

    def _finish(self, context):
        stop_preview()
        _tag_view3d(context)
        return {'CANCELLED'}


def stop_preview():
    _preview["running"] = False
    _preview["rows"] = []
    _preview["note"] = ""
    _disable_overlay()


# ---------------------------------------------------------------------------
# Self-healing when a file is opened
# ---------------------------------------------------------------------------

@persistent
def _optimizer_load_post(_dummy):
    """Re-make any stand-in that has gone missing or out of date.

    ⚠ The cache folder is taken from **where the image already points**, not
    from a setting. A .blend opened on another machine keeps working: its
    stand-ins are regenerated into the folder that file refers to, whatever the
    person opening it happens to have configured. If that folder does not exist
    the image is left alone and reported — showing a missing texture is honest,
    silently re-homing someone's cache is not.
    """
    missing = []
    for image in bpy.data.images:
        if check_tampered(image) or not is_managed(image):
            continue
        original = resolve_original(image)[1]
        current = _abs_path(image)
        if not os.path.isfile(original) and not tile_token(original):
            continue
        if os.path.isfile(current) and not _is_stale(current, original):
            continue
        folder = os.path.dirname(current)
        if not folder or not os.path.isdir(folder):
            missing.append(image.name)
            continue
        regenerate_image(image)
    if missing:
        print("[MadihsonNSFW] Scene Optimizer: no cache folder for %d image(s): "
              "%s" % (len(missing), ", ".join(sorted(missing)[:6])))


# ⚠ NOT called `_on_load_post`. picker.py already has a handler by that name, and
# two modules in one package sharing a handler name makes every by-name sweep
# and every by-name count ambiguous — the sweeps below filter on the module too,
# but a counter that does not (picker_test.py's did) reads one module's handler
# as a duplicate of the other's.
_HANDLERS = (("load_post", _optimizer_load_post),)


def _strip_stale_handlers():
    """Drop handlers a previous load of this module left behind.

    ⚠ Matched by QUALIFIED NAME, not by identity. The dev reload purges
    `sys.modules`, so the reloaded function is a DIFFERENT object from the one
    still sitting in `bpy.app.handlers` — an identity check cannot find it, and
    the stale one keeps firing against a dead module. jiggle.py and picker.py
    learned this the same way (docs\\addon-bridge.md).
    """
    ours = {fn.__name__ for _name, fn in _HANDLERS}
    for name, _fn in _HANDLERS:
        handlers = getattr(bpy.app.handlers, name)
        for existing in list(handlers):
            if (getattr(existing, "__name__", None) in ours
                    and getattr(existing, "__module__", "").endswith("optimizer")):
                handlers.remove(existing)


# ---------------------------------------------------------------------------
# Bridge API
# ---------------------------------------------------------------------------
# ⚠ EVERY MUTATING COMMAND ANSWERS WITH THE WHOLE STATUS, its own result under
# its own key. The app broadcasts each reply to every tool in the tab, so a
# command replying with a bare result dict would look like a status with no
# scene in it and blank the panels. (Exactly the trap picker_save_item fell into
# — docs\bone-picker.md.)

def _settings(params):
    """Pull the dials out of a command's params, falling back to the defaults."""
    params = params or {}
    return {
        "target": params.get("target", DEFAULT_TARGET),
        "quality": float(params.get("quality", DEFAULT_QUALITY)),
        "min_size": int(params.get("min_size", DEFAULT_MIN_SIZE)),
        "max_size": int(params.get("max_size", DEFAULT_MAX_SIZE)),
        "animation": bool(params.get("animation", False)),
        "frame_step": int(params.get("frame_step", 1)),
        "meshes": bool(params.get("meshes", False)),
        "face_floor": int(params.get("face_floor", DEFAULT_FACE_FLOOR)),
        "full_distance": float(params.get("full_distance",
                                          DEFAULT_FULL_DISTANCE)),
        "low_distance": float(params.get("low_distance", DEFAULT_LOW_DISTANCE)),
        "low_ratio": float(params.get("low_ratio", DEFAULT_LOW_RATIO)),
        "cache_dir": params.get("cache_dir") or default_cache_dir(),
    }


def opt_status(context=None):
    """PURE READ — what the scene looks like right now.

    ⚠ Polled by the app, so it must stay cheap and must never change anything.
    It does not touch the disk for image sizes; `image.size` is what Blender
    already has loaded.
    """
    context = context or bpy.context
    scene = context.scene
    managed = []
    total_images = 0
    for image in bpy.data.images:
        total_images += 1
        if is_managed(image):
            # ⚠ `original_missing` is reported so the user finds out their own
            # file is unreachable BEFORE they press Restore, rather than by
            # pressing it and getting a pink texture. It is one extra isfile()
            # per managed image on a polled command, which is worth it for the
            # one question this tab must never get wrong.
            _raw, original_abs = resolve_original(image)
            managed.append({
                "name": image.name,
                "size": managed_size(image),
                "original": image.get(PROP_ORIGINAL, ""),
                "resolved": original_abs,
                "current": image.filepath,
                "missing": not os.path.isfile(_abs_path(image)),
                "original_missing": bool(original_abs)
                and not os.path.isfile(original_abs)
                and not tile_token(original_abs),
            })
    decimated = [ob.name for ob in scene.objects
                 if _managed_modifier(ob) is not None]
    selected = [ob.name for ob in
                (getattr(context, "selected_objects", None) or [])]
    return {
        "scene": scene.name,
        "camera": scene.camera.name if scene.camera else None,
        "resolution": [scene.render.resolution_x, scene.render.resolution_y,
                       scene.render.resolution_percentage],
        "frame_range": [scene.frame_start, scene.frame_end],
        "objects": len(scene.objects),
        "selected": len(selected),
        # The NAMES, not just the count, because a queued job has to remember
        # which objects it was queued with — "SELECTED" resolved at run time is
        # the same selection for every job in the queue. See images_for_names.
        "selected_objects": selected,
        "images": total_images,
        "managed": sorted(managed, key=lambda d: d["name"]),
        "decimated": sorted(decimated),
        "preview_running": bool(_preview["running"]),
        # The named texture sets, each with how many of its cached files have
        # gone. Carried on the POLL so a cleared cache is noticed by itself,
        # rather than only when someone tries to switch to a set and nothing
        # happens.
        "groups": group_state(scene),
        "active_group": scene.get(SCENE_ACTIVE_GROUP) or "",
        "default_cache": default_cache_dir(),
        "targets": list(TARGETS),
        "addon_can_resize": _resize_available(),
    }


def _resize_available():
    try:
        _oiio()
    except ImportError:
        return False
    return True


def _resize_one_job(job, settings, tally, done):
    """One entry of a resize queue. Returns the [(image, side)] it touched."""
    target = job.get("target") or settings["target"]
    side = int(job.get("size") or 1024)
    cache_dir = job.get("cache_dir") or settings["cache_dir"]
    # A job that was queued with a list of objects uses THAT list, not the live
    # selection — see images_for_names. An empty list means the job was queued
    # with nothing selected and so has nothing to do; it must NOT fall through
    # to the target, which would silently resize the whole scene instead.
    objects = job.get("objects")
    if objects is None:
        images = target_images(target)
    else:
        gone = []
        images = images_for_names(objects, missing=gone)
        for name in gone:
            tally.skipped.append((name, "not in this file any more"))
    _progress_phase("Resizing to %d px" % side, len(images))
    touched = []
    for image in images:
        _progress_step(image.name)
        set_image_size(image, side, cache_dir, tally=tally)
        # ⚠ RECORD WHAT IS TRUE AFTERWARDS, not what the call returned. An
        # image smaller than the target is REVERTED rather than resized (that
        # is rule 2 - nothing is ever upscaled), so it ends up unmanaged with
        # no cached file. Recording it would put an entry in the set that can
        # never be satisfied, and the set would report itself as permanently
        # "missing" files it was never going to have.
        if is_managed(image) and managed_size(image) == side:
            touched.append((image, side))
    done.append((target, side, len(images)))
    return touched


def opt_resize(params=None):
    """Every image in the target set to ONE chosen size.

    Takes either one resize or a QUEUE of them (`jobs`).

    ⚠ EACH JOB BECOMES ITS OWN NAMED SET. The queue used to record the whole
    run as a single set, on the reasoning that it was one decision. Marty
    queued two and found he could not do the one thing the sets exist for:
    "after queing two jobs it only gave me one entry i can switch on, when i
    queued two, i need to be able to switch inbetween them." A queued job is a
    PRESET — its own objects, its own size, its own name, its own row.

    It also fixes a bug the single set could not survive. Two jobs whose images
    overlap both end up in that one set, at different sizes, for the same
    image: contradictory entries the set can never satisfy. Now the second job
    simply makes a second set, and the two are alternatives — which is what
    the user was describing all along.
    """
    params = params or {}
    settings = _settings(params)
    jobs = params.get("jobs")
    single = not jobs
    if single:
        jobs = [{"target": settings["target"],
                 "size": int(params.get("size", 1024)),
                 "name": params.get("group_name") or ""}]
    tally = Tally()
    done = []
    made = []
    keep = bool(params.get("group", True))
    with _progress_run("Resizing textures"):
        for job in jobs:
            touched = _resize_one_job(job, settings, tally, done)
            if not keep or not touched:
                # Nothing was left managed — every image was already smaller
                # than the target and got reverted. A set with no entries is
                # not a preset, it is a row that does nothing when clicked.
                continue
            # Capture is per job and happens HERE, right after the job, not
            # once at the end: `touched` records what was true at that moment,
            # and a later job with overlapping images changes it.
            label = (job.get("name") or "").strip() or "%d px" % (
                int(job.get("size") or 0))
            group = group_capture(
                bpy.context.scene, label, group_entries_for(touched),
                job.get("cache_dir") or settings["cache_dir"],
                replace=params.get("group_replace") if single else None)
            made.append(group["name"])

    status = opt_status()
    status["result"] = tally.as_dict()
    status["result"]["size"] = int(jobs[-1].get("size") or 0)
    status["result"]["jobs"] = [{"target": t, "size": s, "images": n}
                                for t, s, n in done]
    status["result"]["groups"] = made
    if made:
        # Kept alongside `groups` for anything reading the old single-set key.
        status["result"]["group"] = made[-1]
    return status


def opt_group_apply(params=None):
    params = params or {}
    tally = Tally()
    with _progress_run("Switching texture set"):
        group_apply(bpy.context.scene, params["name"], tally=tally)
    status = opt_status()
    status["result"] = tally.as_dict()
    return status


def opt_group_rename(params=None):
    params = params or {}
    group_rename(bpy.context.scene, params["name"], params.get("new_name"))
    return opt_status()


def opt_group_delete(params=None):
    params = params or {}
    group_delete(bpy.context.scene, params["name"])
    return opt_status()


def opt_adaptive(params=None):
    """Size every texture by how big its object lands in the camera."""
    settings = _settings(params)
    if settings["target"] not in OBJECT_TARGETS:
        raise RuntimeError(
            "Adaptive sizing needs an object target (Selected, Scene or All "
            "objects) - the image sets have nothing to measure against a "
            "camera. Use a fixed size for those.")
    tally = Tally()
    mesh_tally = None
    with _progress_run("Measuring the scene"):
        plan = plan_adaptive(target=settings["target"],
                             quality=settings["quality"],
                             min_size=settings["min_size"],
                             max_size=settings["max_size"],
                             animation=settings["animation"],
                             frame_step=settings["frame_step"])
        planned = sorted(plan["images"].items())
        _progress_phase("Resizing textures", len(planned))
        for name, side in planned:
            _progress_step(name)
            image = bpy.data.images.get(name)
            if image is not None:
                set_image_size(image, side, settings["cache_dir"], tally=tally)

        if settings["meshes"]:
            objects = [bpy.data.objects[n] for n in plan["objects"]
                       if n in bpy.data.objects]
            decimation = plan_decimation(
                objects, plan["depths"], face_floor=settings["face_floor"],
                full_distance=settings["full_distance"],
                low_distance=settings["low_distance"],
                low_ratio=settings["low_ratio"])
            mesh_tally = Tally()
            apply_decimation(objects, decimation, tally=mesh_tally)

    status = opt_status()
    status["result"] = tally.as_dict()
    status["result"]["planned"] = plan["images"]
    if mesh_tally is not None:
        status["mesh_result"] = mesh_tally.as_dict()
    return status


def opt_decimate(params=None):
    """Decimation on its own — the mesh half of an adaptive run."""
    settings = _settings(params)
    if settings["target"] not in OBJECT_TARGETS:
        raise RuntimeError("Decimation needs an object target.")
    tally = Tally()
    with _progress_run("Measuring distances"):
        plan = plan_adaptive(target=settings["target"],
                             quality=settings["quality"],
                             min_size=settings["min_size"],
                             max_size=settings["max_size"],
                             animation=settings["animation"],
                             frame_step=settings["frame_step"])
        objects = [bpy.data.objects[n] for n in plan["objects"]
                   if n in bpy.data.objects]
        decimation = plan_decimation(
            objects, plan["depths"], face_floor=settings["face_floor"],
            full_distance=settings["full_distance"],
            low_distance=settings["low_distance"],
            low_ratio=settings["low_ratio"])
        apply_decimation(objects, decimation, tally=tally)
    status = opt_status()
    status["mesh_result"] = tally.as_dict()
    status["mesh_result"]["planned"] = decimation
    return status


def opt_revert_images(params=None):
    """Put every managed image back on the user's own file."""
    settings = _settings(params)
    tally = Tally()
    with _progress_run("Restoring textures"):
        images = [im for im in target_images(settings["target"])
                  if is_managed(im)]
        _progress_phase("Restoring textures", len(images))
        for image in images:
            _progress_step(image.name)
            revert_image(image, tally=tally)
    status = opt_status()
    status["result"] = tally.as_dict()
    return status


def opt_revert_meshes(params=None):
    """Remove every Decimate this module added. Other modifiers are untouched."""
    settings = _settings(params)
    objects = target_objects(settings["target"])
    if not objects:
        objects = list(bpy.data.objects)
    tally = Tally()
    with _progress_run("Removing decimation"):
        clear_decimation(objects, tally=tally)
    status = opt_status()
    status["mesh_result"] = tally.as_dict()
    return status


def opt_regenerate(params=None):
    """Re-make missing or stale stand-ins, forcing the chosen cache folder.

    Forcing it is the difference from the automatic heal on file-open: this is
    how a user re-homes their stand-ins after changing where the cache lives.
    """
    settings = _settings(params)
    tally = Tally()
    with _progress_run("Re-making copies"):
        images = [im for im in bpy.data.images if is_managed(im)]
        _progress_phase("Re-making copies", len(images))
        for image in images:
            _progress_step(image.name)
            regenerate_image(image, cache_dir=settings["cache_dir"],
                             tally=tally)
    status = opt_status()
    status["result"] = tally.as_dict()
    return status


def opt_clear_cache(params=None):
    """Put every texture back, then delete the stand-ins from the cache folder.

    The restore is part of the command rather than something the caller is
    trusted to do first — see clear_cache for why the order is not negotiable.
    """
    settings = _settings(params)
    tally = Tally()
    with _progress_run("Clearing the cache"):
        result = clear_cache(settings["cache_dir"], tally=tally)
    status = opt_status()
    status["result"] = tally.as_dict()
    status["cache"] = result
    return status


def opt_estimate(params=None):
    params = params or {}
    with _progress_run("Measuring memory"):
        estimate = estimate_memory(limit=int(params.get("limit", 200)))
    status = opt_status()
    status["estimate"] = estimate
    return status


def opt_plan(params=None):
    """What a run WOULD do — the numbers, with nothing changed."""
    settings = _settings(params)
    if settings["target"] not in OBJECT_TARGETS:
        raise RuntimeError("A preview needs an object target.")
    rows = []
    saved = 0
    with _progress_run("Measuring the scene"):
        plan = plan_adaptive(target=settings["target"],
                             quality=settings["quality"],
                             min_size=settings["min_size"],
                             max_size=settings["max_size"],
                             animation=settings["animation"],
                             frame_step=settings["frame_step"])
        planned = sorted(plan["images"].items())
        _progress_phase("Working out the savings", len(planned))
        for name, side in planned:
            _progress_step(name)
            image = bpy.data.images.get(name)
            if image is None:
                continue
            ok, reason = can_manage(image)
            src_w, src_h = source_dimensions(image)
            longest = max(src_w, src_h)
            row = {"name": name, "from": longest, "to": side,
                   "ok": ok, "reason": reason}
            if ok and longest and side < longest:
                after_w, after_h = _scaled_dimensions(src_w, src_h, side)
                saved += max(0, image_bytes(image, src_w, src_h)
                             - image_bytes(image, after_w, after_h))
            rows.append(row)
        objects = [bpy.data.objects[n] for n in plan["objects"]
                   if n in bpy.data.objects]
        decimation = plan_decimation(
            objects, plan["depths"], face_floor=settings["face_floor"],
            full_distance=settings["full_distance"],
            low_distance=settings["low_distance"],
            low_ratio=settings["low_ratio"]) if settings["meshes"] else {}
    status = opt_status()
    status["plan"] = {"images": rows, "meshes": decimation,
                      "camera": plan["camera"],
                      "bytes_saved": saved,
                      "human_saved": human_bytes(saved)}
    return status


def opt_preview_start(params=None):
    """Start the in-viewport overlay.

    ⚠ Like `picker_start`, this CANNOT be proven headless — `blender -b` has no
    modal loop, so a test only ever reaches the refusal path.
    """
    settings = _settings(params)
    _preview["args"] = {
        "target": settings["target"], "quality": settings["quality"],
        "min_size": settings["min_size"], "max_size": settings["max_size"],
        "meshes": settings["meshes"], "face_floor": settings["face_floor"],
        "full_distance": settings["full_distance"],
        "low_distance": settings["low_distance"],
        "low_ratio": settings["low_ratio"],
    }
    override = _view3d_override()
    if override is None:
        raise RuntimeError(
            "Open a 3D Viewport in Blender first - the preview draws in it.")
    window, area, region = override
    with bpy.context.temp_override(window=window, area=area, region=region):
        bpy.ops.madi_optimizer.preview('INVOKE_DEFAULT')
    return opt_status()


def opt_preview_stop(params=None):
    stop_preview()
    _tag_view3d()
    return opt_status()


def _view3d_override():
    for window in getattr(bpy.context.window_manager, "windows", []):
        for area in window.screen.areas:
            if area.type != 'VIEW_3D':
                continue
            for region in area.regions:
                if region.type == 'WINDOW':
                    return window, area, region
    return None


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

_classes = (MADI_OT_optimizer_preview,)


def register():
    _strip_stale_handlers()
    for cls in _classes:
        bpy.utils.register_class(cls)
    bpy.app.handlers.load_post.append(_optimizer_load_post)


def unregister():
    stop_preview()
    _strip_stale_handlers()
    for cls in reversed(_classes):
        try:
            bpy.utils.unregister_class(cls)
        except RuntimeError:
            pass
