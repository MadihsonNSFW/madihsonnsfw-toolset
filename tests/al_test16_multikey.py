# Task 16 verification: Multikey ops (offset / replace / scale / randomize).
# Run: blender.exe -b --factory-startup --python al_test16_multikey.py
import importlib.util
import os
import sys

import bpy

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CORE = os.path.join(_ROOT, "blender_addon", "madi_anim_library", "core.py")
spec = importlib.util.spec_from_file_location("madi_core", CORE)
core = importlib.util.module_from_spec(spec)
spec.loader.exec_module(core)

PASS = []
FAIL = []


def ok(cond, label):
    (PASS if cond else FAIL).append(label)
    print(("ok   " if cond else "FAIL ") + label, flush=True)


def make_rig(name, bones=("A", "B")):
    arm = bpy.data.armatures.new(name)
    ob = bpy.data.objects.new(name, arm)
    bpy.context.collection.objects.link(ob)
    bpy.context.view_layer.objects.active = ob
    ob.select_set(True)
    bpy.context.view_layer.update()
    bpy.ops.object.mode_set(mode='EDIT')
    for i, bname in enumerate(bones):
        eb = arm.edit_bones.new(bname)
        eb.head = (0.0, 0.0, float(i))
        eb.tail = (0.0, 0.0, float(i) + 1.0)
    bpy.ops.object.mode_set(mode='OBJECT')
    return ob


scene = bpy.context.scene
scene.frame_start, scene.frame_end = 1, 30


def set_sel(ob, names):
    for pb in ob.pose.bones:
        core.bone_set_selected(pb, pb.name in names)


def key_bone(ob, bone, frame, loc=None, rot=None, scale=None):
    scene.frame_set(frame)
    pb = ob.pose.bones[bone]
    if loc is not None:
        pb.location = loc
        pb.keyframe_insert("location")
    if rot is not None:
        pb.rotation_quaternion = rot
        pb.keyframe_insert("rotation_quaternion")
    if scale is not None:
        pb.scale = scale
        pb.keyframe_insert("scale")


def layer_curves(ob, i):
    strip = ob.animation_data.nla_tracks[i].strips[0]
    return {(fc.data_path, fc.array_index): fc
            for fc in core._al_action_fcurves_ro(strip.action)}


def vals(fc):
    return [round(kp.co.y, 6) for kp in fc.keyframe_points]


def reset_curve(fc, pairs):
    """Rebuild a curve's keys. Removing by held reference is the classic
    reallocation trap — always drop the FIRST point until empty."""
    while len(fc.keyframe_points):
        fc.keyframe_points.remove(fc.keyframe_points[0])
    for f, v in pairs:
        fc.keyframe_points.insert(float(f), float(v))
    fc.update()
    return fc


def select_keys(fc, which=None):
    """which = list of key indices to select (None = all)."""
    for i, kp in enumerate(fc.keyframe_points):
        kp.select_control_point = (which is None or i in which)


# =============================================================== rig X: math
x = make_rig("X")
core.al_add_layer(object_name="X")
core.al_select_layer(0, object_name="X")
key_bone(x, "A", 1, loc=(0.0, 0.0, 0.0), scale=(1.0, 1.0, 1.0))
key_bone(x, "A", 10, loc=(2.0, 0.0, 0.0))
key_bone(x, "A", 20, loc=(4.0, 0.0, 0.0))
key_bone(x, "B", 1, loc=(0.0, 0.0, 0.0))
key_bone(x, "B", 15, loc=(1.0, 0.0, 0.0))
x.animation_data.use_tweak_mode = False

CX = ('pose.bones["A"].location', 0)
CB = ('pose.bones["B"].location', 0)

# ---------------------------------------------------------------- OFFSET
cur = layer_curves(x, 0)
select_keys(cur[CX])
select_keys(cur[CB])
set_sel(x, ["A", "B"])
before_a = vals(cur[CX])
r = core.al_multikey('OFFSET', 0.5, index=0, selected_only=True,
                     object_name="X")
cur = layer_curves(x, 0)
ok(vals(cur[CX]) == [round(v + 0.5, 6) for v in before_a],
   "OFFSET adds the value to every selected key (got %s)" % vals(cur[CX]))
ok(r["multikey"]["keys"] > 0 and r["multikey"]["layer"] == "Base Layer",
   "OFFSET reports the layer + key count (%s)" % r["multikey"])

# ---------------------------------------------------------------- REPLACE
core.al_multikey('REPLACE', 1.25, index=0, object_name="X")
cur = layer_curves(x, 0)
ok(set(vals(cur[CX])) == {1.25},
   "REPLACE sets every selected key to the value (got %s)" % vals(cur[CX]))

# ---------------------------------------------------------------- SCALE avg
# rebuild a known curve
fc = layer_curves(x, 0)[CX]
reset_curve(fc, ((1.0, 0.0), (10.0, 2.0), (20.0, 4.0)))
select_keys(fc)
core.al_multikey('SCALE', 2.0, index=0, pivot='AVERAGE', object_name="X")
cur = layer_curves(x, 0)
# mean is 2.0 -> 0,2,4 becomes -2,2,6
ok(vals(cur[CX]) == [-2.0, 2.0, 6.0],
   "SCALE x2 about the AVERAGE pivot (got %s)" % vals(cur[CX]))

core.al_multikey('SCALE', 0.5, index=0, pivot='AVERAGE', object_name="X")
cur = layer_curves(x, 0)
ok(vals(cur[CX]) == [0.0, 2.0, 4.0],
   "SCALE x0.5 puts it back (got %s)" % vals(cur[CX]))

core.al_multikey('SCALE', 0.0, index=0, pivot='AVERAGE', object_name="X")
cur = layer_curves(x, 0)
ok(set(vals(cur[CX])) == {2.0},
   "SCALE x0 flattens to the average (got %s)" % vals(cur[CX]))

# ---------------------------------------------------------------- SCALE zero
fc = layer_curves(x, 0)[CX]
reset_curve(fc, ((1.0, 1.0), (10.0, 2.0), (20.0, 3.0)))
select_keys(fc)
core.al_multikey('SCALE', 2.0, index=0, pivot='ZERO', object_name="X")
cur = layer_curves(x, 0)
ok(vals(cur[CX]) == [2.0, 4.0, 6.0],
   "SCALE about ZERO multiplies raw values (got %s)" % vals(cur[CX]))

# ---------------------------------------------------------------- handles
fc = layer_curves(x, 0)[CX]
reset_curve(fc, [])
kp = fc.keyframe_points.insert(10.0, 2.0)
kp.handle_left_type = kp.handle_right_type = 'FREE'
kp.handle_left = (8.0, 1.0)
kp.handle_right = (12.0, 3.0)
fc.update()
select_keys(fc)
core.al_multikey('OFFSET', 1.0, index=0, object_name="X")
kp = layer_curves(x, 0)[CX].keyframe_points[0]
ok(abs(kp.co.y - 3.0) < 1e-5 and abs(kp.handle_left.y - 2.0) < 1e-5
   and abs(kp.handle_right.y - 4.0) < 1e-5,
   "OFFSET shifts the handles with the key (co %.3f, hl %.3f, hr %.3f)"
   % (kp.co.y, kp.handle_left.y, kp.handle_right.y))
ok(abs(kp.handle_left.x - 8.0) < 1e-5 and abs(kp.handle_right.x - 12.0) < 1e-5,
   "OFFSET never moves a key in TIME (hl.x %.3f, hr.x %.3f)"
   % (kp.handle_left.x, kp.handle_right.x))

core.al_multikey('SCALE', 2.0, index=0, pivot='ZERO', object_name="X")
kp = layer_curves(x, 0)[CX].keyframe_points[0]
ok(abs(kp.co.y - 6.0) < 1e-5 and abs(kp.handle_left.y - 4.0) < 1e-5
   and abs(kp.handle_right.y - 8.0) < 1e-5,
   "SCALE scales the handles about the pivot too (co %.3f, hl %.3f, hr %.3f)"
   % (kp.co.y, kp.handle_left.y, kp.handle_right.y))

# ---------------------------------------------------------------- RANDOMIZE
fc = layer_curves(x, 0)[CX]
reset_curve(fc, [(f, 1.0) for f in (1.0, 5.0, 10.0, 15.0, 20.0)])
select_keys(fc)
core.al_multikey('RANDOMIZE', 0.5, index=0, seed=7, object_name="X")
first = vals(layer_curves(x, 0)[CX])
ok(all(abs(v - 1.0) <= 0.5 + 1e-9 for v in first),
   "RANDOMIZE stays inside +/- the amount (got %s)" % first)
ok(len(set(first)) > 1, "RANDOMIZE actually varies the keys")

fc = layer_curves(x, 0)[CX]
reset_curve(fc, [(f, 1.0) for f in (1.0, 5.0, 10.0, 15.0, 20.0)])
select_keys(fc)
core.al_multikey('RANDOMIZE', 0.5, index=0, seed=7, object_name="X")
ok(vals(layer_curves(x, 0)[CX]) == first,
   "RANDOMIZE with the same seed repeats exactly")

fc = layer_curves(x, 0)[CX]
reset_curve(fc, [(f, 1.0) for f in (1.0, 5.0, 10.0, 15.0, 20.0)])
select_keys(fc)
core.al_multikey('RANDOMIZE', 0.5, index=0, seed=8, object_name="X")
ok(vals(layer_curves(x, 0)[CX]) != first,
   "a different seed gives a different result")

# ---------------------------------------------- selected keys only
fc = layer_curves(x, 0)[CX]
reset_curve(fc, ((1.0, 0.0), (10.0, 1.0), (20.0, 2.0)))
select_keys(fc, which=[1])          # only the middle key
core.al_multikey('OFFSET', 10.0, index=0, object_name="X")
ok(vals(layer_curves(x, 0)[CX]) == [0.0, 11.0, 2.0],
   "only SELECTED keys move (got %s)" % vals(layer_curves(x, 0)[CX]))

for _fc in layer_curves(x, 0).values():   # nothing selected ANYWHERE
    select_keys(_fc, which=[])
try:
    core.al_multikey('OFFSET', 1.0, index=0, object_name="X")
    ok(False, "no selected keys should refuse")
except RuntimeError as exc:
    ok("select keys" in str(exc).lower(),
       "no selected keys -> clear guidance (%s)" % exc)

r = core.al_multikey('OFFSET', 1.0, index=0, selected_keys=False,
                     object_name="X")
ok(vals(layer_curves(x, 0)[CX]) == [1.0, 12.0, 3.0],
   "selected_keys=False touches every key (got %s)"
   % vals(layer_curves(x, 0)[CX]))
ok(r["multikey"]["selected_keys"] is False,
   "status reports the selected-keys mode")

# ---------------------------------------------- filter scoping
fc_scale = layer_curves(x, 0)[('pose.bones["A"].scale', 0)]
select_keys(fc_scale)
select_keys(layer_curves(x, 0)[CX])
before_scale = vals(fc_scale)
before_loc = vals(layer_curves(x, 0)[CX])
core.al_multikey('OFFSET', 1.0, index=0, channels=['LOCATION'],
                 object_name="X")
ok(vals(layer_curves(x, 0)[('pose.bones["A"].scale', 0)]) == before_scale,
   "a LOCATION-only filter leaves scale keys alone")
ok(vals(layer_curves(x, 0)[CX]) == [round(v + 1.0, 6) for v in before_loc],
   "the location keys still moved")

# axis filter
cur = layer_curves(x, 0)
for i in (0, 1, 2):
    select_keys(cur[('pose.bones["A"].location', i)])
by_axis = {i: vals(cur[('pose.bones["A"].location', i)]) for i in (0, 1, 2)}
core.al_multikey('OFFSET', 2.0, index=0, axes=['Y'], object_name="X")
cur = layer_curves(x, 0)
ok(vals(cur[('pose.bones["A"].location', 0)]) == by_axis[0],
   "axis filter Y leaves X alone")
ok(vals(cur[('pose.bones["A"].location', 1)])
   == [round(v + 2.0, 6) for v in by_axis[1]],
   "axis filter Y moves Y")

# bone scoping
set_sel(x, ["B"])
cur = layer_curves(x, 0)
for key in (CX, CB):
    select_keys(cur[key])
a_before = vals(cur[CX])
b_before = vals(cur[CB])
core.al_multikey('OFFSET', 5.0, index=0, selected_only=True, object_name="X")
cur = layer_curves(x, 0)
ok(vals(cur[CX]) == a_before, "only-selected-bones leaves unselected bone A")
ok(vals(cur[CB]) == [round(v + 5.0, 6) for v in b_before],
   "only-selected-bones moves the selected bone B")

# ---------------------------------------------- guards
try:
    core.al_multikey('BOGUS', 1.0, index=0, object_name="X")
    ok(False, "unknown op should refuse")
except RuntimeError as exc:
    ok("multikey op" in str(exc).lower(), "unknown op refused (%s)" % exc)

try:
    core.al_multikey('SCALE', 1.0, index=0, pivot='NOPE', object_name="X")
    ok(False, "bad pivot should refuse")
except RuntimeError as exc:
    ok("pivot" in str(exc).lower(), "bad pivot refused (%s)" % exc)

x.animation_data.nla_tracks[0].lock = True
try:
    core.al_multikey('OFFSET', 1.0, index=0, object_name="X")
    ok(False, "locked layer should refuse")
except RuntimeError as exc:
    ok("locked" in str(exc).lower(), "locked layer refused (%s)" % exc)
x.animation_data.nla_tracks[0].lock = False

set_sel(x, [])
try:
    core.al_multikey('OFFSET', 1.0, index=0, selected_only=True,
                     object_name="X")
    ok(False, "no bones selected should refuse")
except RuntimeError as exc:
    ok("bones" in str(exc).lower(), "no bones selected refused (%s)" % exc)

# ---------------------------------------------- a locked CURVE is skipped
set_sel(x, ["A", "B"])
cur = layer_curves(x, 0)
select_keys(cur[CX])
select_keys(cur[CB])
cur[CX].lock = True
a_before = vals(cur[CX])
b_before = vals(cur[CB])
core.al_multikey('OFFSET', 1.0, index=0, object_name="X")
cur = layer_curves(x, 0)
ok(vals(cur[CX]) == a_before, "a locked F-CURVE is left alone")
ok(vals(cur[CB]) == [round(v + 1.0, 6) for v in b_before],
   "unlocked curves in the same op still move")
cur[CX].lock = False

# ---------------------------------------------- second layer isolation
core.al_add_layer(object_name="X", name="Layer 2")
core.al_select_layer(1, object_name="X")
key_bone(x, "A", 5, loc=(0.5, 0.0, 0.0))
key_bone(x, "A", 25, loc=(1.5, 0.0, 0.0))
x.animation_data.use_tweak_mode = False
base_before = vals(layer_curves(x, 0)[CX])
cur1 = layer_curves(x, 1)
select_keys(cur1[CX])
l2_before = vals(cur1[CX])
core.al_multikey('OFFSET', 3.0, index=None, selected_only=False,
                 object_name="X")     # index=None -> the active layer
ok(vals(layer_curves(x, 0)[CX]) == base_before,
   "index=None edits the ACTIVE layer, base layer untouched")
ok(vals(layer_curves(x, 1)[CX]) == [round(v + 3.0, 6) for v in l2_before],
   "index=None moved layer 2's keys (got %s)" % vals(layer_curves(x, 1)[CX]))

# ---------------------------------------------- timing never changes
frames = [round(kp.co.x, 4) for kp in layer_curves(x, 1)[CX].keyframe_points]
core.al_multikey('SCALE', 3.0, index=1, pivot='ZERO', object_name="X")
ok([round(kp.co.x, 4)
    for kp in layer_curves(x, 1)[CX].keyframe_points] == frames,
   "no multikey op ever moves a key in time")

print("\n%d passed, %d failed" % (len(PASS), len(FAIL)), flush=True)
for f in FAIL:
    print("  FAILED: " + f, flush=True)
sys.exit(1 if FAIL else 0)
