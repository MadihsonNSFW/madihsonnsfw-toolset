# Marty's 2026-08-05 anim/render batch, Blender side: the two new `save_anim`
# options, previews shot with overlays OFF, where Blender says its renders go,
# and the last-render record the app and the add-on SHARE.
#
#   blender.exe -b --factory-startup --python tests\anim_options_test.py
#
# ⚠ WHY REAL BLENDER. Every claim here is about Blender's own state: whether a
# stored custom property comes back onto a pose bone, whether the mirror carries
# it to the other side, whether the viewport is left with its overlays exactly
# as they were found. A stub would assert that the code calls the functions it
# visibly calls and would pass on a version that left Marty's viewport with the
# overlays switched off.
#
# ⚠ ONLY THE PIXEL WRITE IS FAKED (same reason as vgroup_preview_test.py):
# background Blender has a 3D Viewport but no OpenGL CONTEXT, so
# `render.opengl` refuses outright. The stub records the overlay state it was
# called WITH, which is the one thing this suite exists to prove and the one
# thing a screenshot could not show.
import importlib.util
import json
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
server = sys.modules["madi_pkg.server"]

LIB = tempfile.mkdtemp(prefix="madi_animopt_")

# ---------------------------------------------------------------- a rig to save
bpy.ops.wm.read_homefile(use_empty=True)
arm_data = bpy.data.armatures.new("Rig")
rig = bpy.data.objects.new("Rig", arm_data)
bpy.context.collection.objects.link(rig)
bpy.context.view_layer.objects.active = rig
rig.select_set(True)
bpy.ops.object.mode_set(mode='EDIT')
for name, x in (("hand.L", 1.0), ("hand.R", -1.0), ("switch", 0.0)):
    eb = arm_data.edit_bones.new(name)
    eb.head = (x, 0.0, 0.0)
    eb.tail = (x, 0.0, 1.0)
bpy.ops.object.mode_set(mode='POSE')

pb_l = rig.pose.bones["hand.L"]
pb_r = rig.pose.bones["hand.R"]
switch = rig.pose.bones["switch"]
# The properties this feature exists for: nobody keyframes an IK/FK switch, and
# an animation pasted onto a rig set the other way looks broken for no visible
# reason.
switch["ik_fk"] = 1.0
switch["twist"] = 0.25
pb_l["stretch"] = 0.75

# …and one that IS keyed, which was always saved as a curve
pb_l.location = (0.0, 0.0, 0.0)
pb_l.keyframe_insert("location", frame=1)
pb_l.location = (0.0, 0.0, 2.0)
pb_l.keyframe_insert("location", frame=10)

fc = rig.animation_data.action.layers[0].strips[0].channelbags[0].fcurves[0]
noise = fc.modifiers.new('NOISE')
noise.strength = 0.5

# ======================================================= save_anim: the options
res = core.save_anim(LIB, "", "with_everything", frame_start=1, frame_end=10,
                     use_selected=False, keep_modifiers=True,
                     include_props=True)
with open(os.path.join(res["path"], "anim.json"), encoding="utf-8") as f:
    kept = json.load(f)

ok(res.get("options") == {"keep_modifiers": True, "include_props": True},
   "the reply ECHOES the options - the only way an old add-on is detectable, "
   "since save_anim has always existed (%r)" % (res.get("options"),))
ok(any(c["modifiers"] for c in kept["curves"]),
   "keep_modifiers=True stores the F-modifiers")
ok(kept["metadata"].get("fcurve_modifiers") is True
   and kept["metadata"].get("bone_props") is True,
   "…and both are recorded in metadata, which is what draws the tile badges")
ok(kept["bones"].get("switch", {}).get("props") == {"ik_fk": 1.0, "twist": 0.25},
   "include_props stores a bone with NO CURVES AT ALL - the switch case this "
   "was asked for (%r)" % (kept["bones"].get("switch"),))
ok(kept["bones"]["hand.L"].get("props") == {"stretch": 0.75},
   "…and props on a bone that does have curves")

res_off = core.save_anim(LIB, "", "plain", frame_start=1, frame_end=10,
                         use_selected=False, keep_modifiers=False,
                         include_props=False)
with open(os.path.join(res_off["path"], "anim.json"), encoding="utf-8") as f:
    plain = json.load(f)
ok(all(c["modifiers"] == [] for c in plain["curves"]),
   "keep_modifiers=False writes an EMPTY modifier list, not a missing key - "
   "apply_anim reads 'modifiers' in cur, so a missing one would mean 'leave "
   "whatever is on the curve'")
ok(not any("props" in b for b in plain["bones"].values()),
   "include_props=False stores no properties")
ok(plain["metadata"].get("fcurve_modifiers") is False
   and plain["metadata"].get("bone_props") is False,
   "…and the metadata says so, so the tile shows no badges")

# The defaults must not change what the button did before the dialog existed.
res_def = core.save_anim(LIB, "", "defaults", frame_start=1, frame_end=10,
                         use_selected=False)
with open(os.path.join(res_def["path"], "anim.json"), encoding="utf-8") as f:
    default_data = json.load(f)
ok(any(c["modifiers"] for c in default_data["curves"])
   and not any("props" in b for b in default_data["bones"].values()),
   "the DEFAULTS are the old behaviour: modifiers kept, no properties")

# ⚠ Baking destroys what keep_modifiers would keep, and the file must say the
# truth rather than repeating what was asked for.
res_bake = core.save_anim(LIB, "", "baked", frame_start=1, frame_end=5,
                          use_selected=False, bake=True, keep_modifiers=True,
                          include_props=True)
with open(os.path.join(res_bake["path"], "anim.json"), encoding="utf-8") as f:
    baked = json.load(f)
ok(res_bake["options"]["keep_modifiers"] is False,
   "baking reports keep_modifiers FALSE however it was asked - a baked curve "
   "is a key per frame, there is nothing left for a modifier to shape")
ok(baked["metadata"].get("baked") is True
   and baked["metadata"].get("fcurve_modifiers") is False,
   "…and the metadata agrees, so the badges cannot claim otherwise")
ok(baked["bones"].get("switch", {}).get("props") == {"ik_fk": 1.0, "twist": 0.25},
   "include_props still works on the bake path (a property is not a curve)")

# ======================================================= apply_anim: properties
switch["ik_fk"] = 0.0
switch["twist"] = 0.0
pb_l["stretch"] = 0.0
info = core.apply_anim(res["path"], mode='replace', start_at='original')
ok(info.get("props") == 3, "apply reports how many properties it set (%r)"
   % info.get("props"))
ok(switch["ik_fk"] == 1.0 and switch["twist"] == 0.25,
   "the un-keyed switch is restored - the whole point of the option")
ok(pb_l["stretch"] == 0.75, "and a property on an animated bone too")

# ⚠ A bone with props and NO curves is not in `touched_bones`. Restoring props
# only for touched bones would silently skip exactly the bones this exists for.
ok("switch" not in (info.get("missing_names") or []),
   "the property-only bone is not reported missing")

switch["ik_fk"] = 0.0
core.apply_anim(res["path"], mode='replace', start_at='original',
                selected_only=True)
ok(switch["ik_fk"] == 0.0,
   "selected_only still scopes properties (nothing is selected here)")

# a property the rig no longer has must not take the paste down
del switch["twist"]
switch["ik_fk"] = 0.0
info2 = core.apply_anim(res["path"], mode='replace', start_at='original')
ok(switch["ik_fk"] == 1.0 and switch.get("twist") == 0.25,
   "a property missing from the rig is simply re-created, never an error")

# ======================================================= mirror carries them
mirror_map, _c, _u = core.build_mirror_map(rig)
curves, bones, skipped = core.mirror_anim_curves(rig, kept, mirror_map)
ok(bones.get("hand.R", {}).get("props") == {"stretch": 0.75},
   "a mirrored anim carries the bone's properties to the other side (%r)"
   % (bones.get("hand.R"),))
ok(bones.get("switch", {}).get("props") == {"ik_fk": 1.0, "twist": 0.25},
   "⚠ and a PROPERTY-ONLY bone survives the mirror - it has no curves, so the "
   "curve loop never sees it and it used to vanish")

flip = core._prop_flip_matcher(["*twist*"])
_c2, bones2, _s2 = core.mirror_anim_curves(rig, kept, mirror_map, prop_flip=flip)
ok(bones2["switch"]["props"]["twist"] == -0.25
   and bones2["switch"]["props"]["ik_fk"] == 1.0,
   "prop_flips negates matching properties on the mirror, same rule as a pose")

# ======================================================= previews: overlays OFF
window, area, region = core._find_view3d()
ok(area is not None, "background Blender still has a 3D viewport to render from")
space = area.spaces.active
space.overlay.show_overlays = True          # as Marty works

seen = []


def fake_opengl(*_a, **_k):
    """Stands in for the GL render: records the overlay state it was called
    with, then writes the file."""
    seen.append(space.overlay.show_overlays)
    with open(bpy.context.scene.render.filepath, "wb") as fh:
        fh.write(b"\xff\xd8\xff\xdb")
    return {'FINISHED'}


# ⚠ `bpy.ops.render = ...`, NOT `bpy.ops.render.opengl = ...` — `bpy.ops.render`
# is rebuilt by __getattr__ on every access, so assigning to an attribute of it
# patches an object discarded on the same line, and it LOOKS like it worked
# because reading it back builds another real one. (Same trap as
# vgroup_preview_test.py; it cost a run here too.)
real_render_ops = bpy.ops.render
bpy.ops.render = types.SimpleNamespace(opengl=fake_opengl)
try:
    item = os.path.join(LIB, "shot.pose")
    os.makedirs(item, exist_ok=True)
    core.capture_preview(item)
finally:
    bpy.ops.render = real_render_ops

ok(seen and all(v is False for v in seen),
   "⚠ the preview is rendered with overlays OFF - bones, wires and gizmos are "
   "overlays, and a rigged character's thumbnail was mostly bone (%r)" % seen)
ok(space.overlay.show_overlays is True,
   "⚠ …and they are put BACK. Leaving them off would silently change how "
   "Marty's viewport looks, from a thumbnail")


def boom_opengl(*_a, **_k):
    raise RuntimeError("no GL context")


bpy.ops.render = types.SimpleNamespace(opengl=boom_opengl)
try:
    core.capture_preview(item)
except RuntimeError:
    pass
finally:
    bpy.ops.render = real_render_ops
ok(space.overlay.show_overlays is True,
   "overlays are restored through a FAILED capture too (it is in the finally)")

# ======================================================= scene_output_dir
rd = bpy.context.scene.render
outdir = tempfile.mkdtemp(prefix="madi_out_")
rd.filepath = outdir + os.sep
ok(core.scene_output_dir() == os.path.normpath(outdir),
   "a trailing separator means the whole path is the folder")

rd.filepath = os.path.join(outdir, "shot_")
ok(core.scene_output_dir() == os.path.normpath(outdir),
   "⚠ …and without one the last component is a FILE PREFIX, not a folder - "
   "reading it as one invents a folder named after his file prefix (%r)"
   % core.scene_output_dir())

rd.filepath = "//renders/"
ok(core.scene_output_dir() is None,
   "a // path in an unsaved file resolves to nothing real, so it says so and "
   "the app keeps its own default")

rd.filepath = ""
ok(core.scene_output_dir() is None, "an empty output path is not a folder")

rd.filepath = outdir + os.sep
reply = server.BridgeServer._handle(server.server, {"cmd": "status"})
ok(reply.get("output_dir") == os.path.normpath(outdir),
   "status carries it, so the playblast dialog can default to it")

# ======================================================= the shared record
sys.path.insert(0, os.path.join(ROOT, "app"))
import lastrender  # noqa: E402

ok(lastrender.state_dir() == core._shared_state_dir(),
   "⚠ THE APP AND THE ADD-ON AGREE ON THE FOLDER. They are two processes "
   "writing ONE file; a silent disagreement is two Watch buttons that each "
   "work perfectly and never see the same render (%r vs %r)"
   % (lastrender.state_dir(), core._shared_state_dir()))
ok(lastrender.LAST_RENDER_FILE == core.LAST_RENDER_FILE,
   "…and on the file name")

mp4 = os.path.join(LIB, "take01.mp4")
with open(mp4, "wb") as f:
    f.write(b"x")

saved_record = None
record_path = lastrender.state_path()
if os.path.isfile(record_path):          # never clobber Marty's real record
    with open(record_path, "rb") as f:
        saved_record = f.read()
try:
    core.note_last_render(mp4)
    ok(lastrender.last() == mp4,
       "the APP reads what the ADD-ON wrote (a blocking playblast)")
    lastrender.note(mp4)
    ok(core.last_render(max_age=0) == mp4,
       "and the ADD-ON reads what the APP wrote (a background one)")

    os.remove(mp4)
    ok(core.last_render(max_age=0) is None and lastrender.last() is None,
       "⚠ a record whose file has gone reads as NO record on both sides - a "
       "Watch button that opens a missing file is worse than a dead one")

    # the cache exists because a panel poll calls this on every redraw
    with open(mp4, "wb") as f:
        f.write(b"x")
    core.last_render(max_age=0)
    os.remove(mp4)
    ok(core.last_render(max_age=60) == mp4,
       "last_render is CACHED - poll runs on every panel redraw, and an "
       "uncached read would stat the disk on every mouse move")
    ok(core.last_render(max_age=0) is None, "max_age=0 forces a fresh read")
finally:
    if saved_record is None:
        try:
            os.remove(record_path)
        except OSError:
            pass
    else:
        with open(record_path, "wb") as f:
            f.write(saved_record)

# ======================================================= the N-panel button
ok(hasattr(pkg, "MADILIB_OT_watch_last_render"),
   "the N-panel has a Watch operator")
ok(pkg.MADILIB_OT_watch_last_render in pkg._classes,
   "…and it is REGISTERED - a class missing from _classes draws as a broken "
   "row rather than as an error")

shutil.rmtree(LIB, ignore_errors=True)
shutil.rmtree(outdir, ignore_errors=True)

print("\n%d passed, %d failed" % (len(PASS), len(FAIL)), flush=True)
sys.exit(1 if FAIL else 0)
