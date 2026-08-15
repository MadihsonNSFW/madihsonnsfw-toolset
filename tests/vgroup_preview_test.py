# `capture_vgroup_preview` - the weight-paint thumbnail for a .vgroups item,
# plus the render-settings helpers it now shares with `capture_preview`.
#
#   blender.exe -b --factory-startup --python tests\vgroup_preview_test.py
#
# ⚠ WHY THIS RUNS IN REAL BLENDER. The whole feature IS the mode switch: enter
# Weight Paint, render, come back out and leave the scene exactly as it was
# found. None of that has any meaning against a stub - a stubbed test would
# assert that the code calls the functions it obviously calls, and would still
# pass on a version that left the user in Weight Paint on the wrong object.
#
# ⚠ ONLY THE PIXEL WRITE IS FAKED, and it has to be: background Blender 5.2 does
# have a 3D Viewport (the old "-b has no windows" rule is wrong), but it has no
# OpenGL CONTEXT, so `render.opengl` refuses outright with "Cannot use OpenGL
# render in background mode". So `bpy.ops.render.opengl` is swapped for a stub
# that writes a file and records the mode it was called in. Everything that
# matters is still the real thing: real objects, real vertex groups, real
# mode_set transitions, real restoration. The stub is also what lets the suite
# assert the one claim that a screenshot could not - that the render happened
# WHILE in Weight Paint with the right group active.
import importlib.util
import os
import shutil
import sys
import tempfile
import types

import bpy

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

ROOT = _ROOT
ADDON = os.path.join(ROOT, "blender_addon", "madi_anim_library")
PASS, FAIL = [], []


def ok(cond, label):
    (PASS if cond else FAIL).append(label)
    print(("ok   " if cond else "FAIL ") + label, flush=True)


spec = importlib.util.spec_from_file_location(
    "madi_pkg", os.path.join(ADDON, "__init__.py"),
    submodule_search_locations=[ADDON])
pkg = importlib.util.module_from_spec(spec)
sys.modules["madi_pkg"] = pkg
spec.loader.exec_module(pkg)
core = sys.modules["madi_pkg.core"]

LIB = tempfile.mkdtemp(prefix="madi_vgp_")

# ------------------------------------------------------------------- the scene
bpy.ops.wm.read_homefile(use_empty=True)
bpy.ops.mesh.primitive_uv_sphere_add()
ball = bpy.context.active_object
ball.name = "Blob"
for name, weight in (("Head", 1.0), ("Neck", 0.5), ("Chest", 0.25)):
    vg = ball.vertex_groups.new(name=name)
    vg.add([v.index for v in ball.data.vertices], weight, 'REPLACE')

bpy.ops.mesh.primitive_cube_add(location=(4, 0, 0))
box = bpy.context.active_object
box.name = "Box"
box.vertex_groups.new(name="Lid").add([0, 1], 1.0, 'REPLACE')

listing = core.list_vertex_groups(objects=["Blob"])
ok(listing and listing[0]["groups"] == ["Head", "Neck", "Chest"],
   "listing: the app's checklist source names every group in order (%s)"
   % (listing[0]["groups"] if listing else None))

# ------------------------------------------------------- saving a SUBSET only
# Marty, 2026-08-05: "an option menu to select witch vertex paint to export but
# also an ability to export multiple vertex paints".
res = core.save_vgroups(LIB, "", "two_of_three", objects=["Blob"],
                        groups={"Blob": ["Head", "Chest"]})
item = res["path"]
ok(res["groups"] == 2,
   "subset: only the chosen groups are written (%d)" % res["groups"])
import json  # noqa: E402
with open(os.path.join(item, "vgroups.json"), encoding="utf-8") as f:
    stored = json.load(f)
names = [g["name"] for g in stored["meshes"][0]["groups"]]
ok(names == ["Head", "Chest"],
   "subset: and they are the RIGHT ones, in mesh order (%s)" % names)

# ------------------------------------------------------------- what it refuses
try:
    core.capture_vgroup_preview(LIB)
except RuntimeError as exc:
    ok("vertex group item" in str(exc).lower(),
       "refusal: a folder that is not a vgroups item says so (%s)" % exc)
else:
    ok(False, "refusal: a folder with no vgroups.json should raise")

# An item whose mesh has gone: there is nothing to paint, and inventing a grey
# viewport shot would read as "the weights did not save".
gone = core.save_vgroups(LIB, "", "orphan", objects=["Box"])["path"]
bpy.data.objects.remove(box, do_unlink=True)
try:
    core.capture_vgroup_preview(gone)
except RuntimeError as exc:
    ok("scene right now" in str(exc),
       "refusal: an item whose mesh is gone is named as such, not previewed "
       "as an empty viewport (%s)" % exc)
else:
    ok(False, "refusal: a missing mesh should raise")

# --------------------------------------------------------- state to be restored
bpy.ops.object.select_all(action='DESELECT')
ball.select_set(True)
bpy.context.view_layer.objects.active = ball
ball.vertex_groups.active_index = 1            # "Neck"
scene = bpy.context.scene
scene.frame_set(37)
scene.render.filepath = "//somewhere_else"
scene.render.resolution_x = 1920
scene.render.resolution_y = 1080
before_format = scene.render.image_settings.file_format

_win, area, _region = core._find_view3d()
ok(area is not None,
   "context: background Blender does have a VIEW_3D to override into")

SHOTS = []           # (mode, active object, active group) at each render call


def fake_opengl(*_a, **_k):
    """Stands in for the GL render: writes the file and records the state."""
    ob = bpy.context.view_layer.objects.active
    group = None
    if ob is not None and len(ob.vertex_groups):
        group = ob.vertex_groups[ob.vertex_groups.active_index].name
    SHOTS.append((ob.mode if ob else None, ob.name if ob else None, group))
    with open(bpy.context.scene.render.filepath, "wb") as fh:
        fh.write(b"\xff\xd8\xff\xdb")
    return {'FINISHED'}


# ⚠ `bpy.ops.render = ...`, NOT `bpy.ops.render.opengl = ...`. `bpy.ops.render`
# is built fresh by __getattr__ on every access, so assigning to an attribute of
# it patches an object that is discarded on the same line - and it LOOKS like it
# worked, because reading it back builds another real one. Replacing the
# submodule on `bpy.ops` itself is what sticks.
bpy.ops.render = types.SimpleNamespace(opengl=fake_opengl)

captured = core.capture_vgroup_preview(item, width=64, height=64)
ok(captured["written"] == 2 and captured["groups"] == 2,
   "capture: one still per stored group (%s)" % captured)
ok(os.path.isfile(os.path.join(item, "thumbnail.jpg")),
   "capture: and a thumbnail lands in the item folder")
seq = os.path.join(item, "sequence")
frames = sorted(os.listdir(seq)) if os.path.isdir(seq) else []
ok(len(frames) == 2,
   "capture: TWO groups also get a sequence/, so the tile can be hovered to "
   "play through them - a single still of one group would be a lie about what "
   "the item holds (%s)" % frames)

# ⚠ THE CLAIM THE FEATURE MAKES. Not "a picture was written" but "the picture
# is of that weight paint": in Weight Paint mode, on the stored mesh, with the
# stored group active. Any of the three wrong and the thumbnail is a confident
# picture of the wrong thing.
ok([s[0] for s in SHOTS] == ['WEIGHT_PAINT', 'WEIGHT_PAINT'],
   "capture: every still was rendered while IN Weight Paint (%s)"
   % [s[0] for s in SHOTS])
ok([s[2] for s in SHOTS] == ["Head", "Chest"],
   "capture: with the item's own groups active, in order - and NOT 'Neck', "
   "which is what the mesh had active going in (%s)" % [s[2] for s in SHOTS])
ok(all(s[1] == "Blob" for s in SHOTS),
   "capture: on the mesh the groups came from")

# ⚠ THE OTHER HALF: the scene comes back untouched.
ok(bpy.context.view_layer.objects.active is ball,
   "restore: the active object is the one we started on")
ok(ball.mode == 'OBJECT',
   "restore: and it is NOT left in Weight Paint (%s)" % ball.mode)
ok(ball.vertex_groups.active_index == 1,
   "restore: the active vertex group is put back - it drives what the user's "
   "own weight paint edits, so moving it is a real edit (%d)"
   % ball.vertex_groups.active_index)
ok(scene.frame_current == 37,
   "restore: the frame never moved - a vertex group has no frame, so this "
   "capture must not touch the timeline at all (%d)" % scene.frame_current)
ok(scene.render.resolution_x == 1920 and scene.render.resolution_y == 1080,
   "restore: the render resolution is the user's again (%dx%d)"
   % (scene.render.resolution_x, scene.render.resolution_y))
ok(scene.render.filepath == "//somewhere_else",
   "restore: including the output path (%r)" % scene.render.filepath)
ok(scene.render.image_settings.file_format == before_format,
   "restore: and the file format (%s)" % scene.render.image_settings.file_format)

# One group -> one still, no sequence: there is nothing to play through.
solo = core.save_vgroups(LIB, "", "just_head", objects=["Blob"],
                         groups={"Blob": ["Head"]})["path"]
r = core.capture_vgroup_preview(solo, width=64, height=64)
ok(r["written"] == 1 and not os.path.isdir(os.path.join(solo, "sequence")),
   "capture: a single group is a thumbnail and nothing else (%s)" % r)

# ⚠ The cap is on FRAMES, never on what the item stored. Every group is still
# in the json; the preview just stops. Each frame is a real viewport render, so
# an uncapped preview of a rigged character would lock Blender up for minutes
# to produce a picture nobody can read.
r = core.capture_vgroup_preview(item, width=64, height=64, max_groups=1)
ok(r["capped"] is True and r["written"] == 1,
   "cap: it stops early and SAYS it stopped (%s)" % r)
with open(os.path.join(item, "vgroups.json"), encoding="utf-8") as f:
    after = json.load(f)
ok(len(after["meshes"][0]["groups"]) == 2,
   "cap: and the item still holds every group it was saved with - the cap is "
   "on the picture, not the data")

# A mesh that cannot enter Weight Paint is named and stepped over, rather than
# costing the previews of every other group in the item.
ball.hide_viewport = True
try:
    r = core.capture_vgroup_preview(item, width=64, height=64)
except RuntimeError as exc:
    ok("Weight Paint" in str(exc) and "Blob" in str(exc),
       "awkward mesh: it says which mesh it could not paint (%s)" % exc)
else:
    ok(bool(r.get("missing")),
       "awkward mesh: it is listed as missing rather than passed off as done "
       "(%s)" % r)
ball.hide_viewport = False
ok(bpy.context.view_layer.objects.active is ball and ball.mode == 'OBJECT',
   "awkward mesh: and the scene is still put back afterwards")

# The mature path still works after the render-settings helpers were split out
# of it - that extraction is the only reason this check is here.
pose_item = os.path.join(LIB, "shot.pose")
os.makedirs(pose_item, exist_ok=True)
core.capture_preview(pose_item, width=64, height=64)
ok(os.path.isfile(os.path.join(pose_item, "thumbnail.jpg")),
   "shared: capture_preview still captures after the settings snapshot was "
   "split into _preview_render_state/_begin/_end")
ok(scene.render.resolution_x == 1920 and scene.frame_current == 37,
   "shared: and still restores everything it touched")

# --------------------------------------------------------------- the gate side
import re  # noqa: E402

server_src = open(os.path.join(ADDON, "server.py"), encoding="utf-8").read()
ok('cmd == "capture_vgroup_preview"' in server_src,
   "gate: it is its OWN command, not a mode flag on capture_preview - the "
   "app's capability gate reads command NAMES, so a new parameter on an old "
   "command is invisible to it")
manifest = open(os.path.join(ADDON, "blender_manifest.toml"), encoding="utf-8").read()
# ⚠ A FLOOR, not an equality. This started life as `== "0.16.0"` — the version
# the feature shipped in — and broke the moment the add-on went to 0.17.0 for
# something unrelated. What this check is actually for is "the version was
# bumped when this landed", and that stays true forever. The three-way
# agreement (manifest / core / app) is `bridge_version_test.py`'s job.
def _ver(text):
    return tuple(int(p) if p.isdigit() else 0 for p in text.split("."))


# ⚠ Anchored to the start of a line: the FIRST `version = "..."` in the
# manifest is `schema_version = "1.0.0"`, and an unanchored search reads that.
manifest_version = re.search(r'^version\s*=\s*"([0-9.]+)"', manifest,
                             re.M).group(1)
ok(manifest_version == core.ADDON_VERSION
   and _ver(core.ADDON_VERSION) >= (0, 16, 0),
   "gate: manifest and core.ADDON_VERSION agree, at or past the 0.16.0 this "
   "feature shipped in (%s / %s)" % (manifest_version, core.ADDON_VERSION))

shutil.rmtree(LIB, ignore_errors=True)
print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
for label in FAIL:
    print("  FAILED: " + label)
sys.exit(1 if FAIL else 0)
