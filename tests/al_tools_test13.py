# Task 13 verification: Extract Selected Bones + Share Layer Keys.
# Run: blender.exe -b --factory-startup --python al_tools_test13.py
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


def key_bone(ob, bone, frame, loc, rot=None):
    scene.frame_set(frame)
    pb = ob.pose.bones[bone]
    pb.location = loc
    pb.keyframe_insert("location")
    if rot is not None:
        pb.rotation_quaternion = rot
        pb.keyframe_insert("rotation_quaternion")


def sample(ob, bones=("A", "B")):
    ad = ob.animation_data
    if ad and ad.use_tweak_mode:
        ad.use_tweak_mode = False
    out = []
    for f in range(1, 31):
        scene.frame_set(f)
        row = []
        for b in bones:
            pb = ob.pose.bones[b]
            row.extend(list(pb.location) + list(pb.rotation_quaternion))
        out.append([float(v) for v in row])
    return out


def diff(a, b):
    return max(abs(x - y) for ra, rb in zip(a, b) for x, y in zip(ra, rb))


def layer_curves(ob, i):
    strip = ob.animation_data.nla_tracks[i].strips[0]
    return {(fc.data_path, fc.array_index): fc
            for fc in core._al_action_fcurves_ro(strip.action)}


def names(ob):
    return [t.name for t in ob.animation_data.nla_tracks]


# ------------------------------------------------- X: extract selected bones
x = make_rig("X")
core.al_add_layer(object_name="X")
core.al_select_layer(0, object_name="X")
key_bone(x, "A", 1, (0.0, 0, 0), (1, 0, 0, 0))
key_bone(x, "A", 20, (3.0, 1.0, 0), (0.7071, 0.7071, 0, 0))
key_bone(x, "B", 1, (0.0, 0, 0))
key_bone(x, "B", 25, (0, 2.0, 0))
x.animation_data.use_tweak_mode = False
# a modifier on one curve must travel with it
src = layer_curves(x, 0)[('pose.bones["A"].location', 0)]
src.modifiers.new(type='NOISE')
ref = sample(x)

set_sel(x, ["A"])
r = core.al_extract_bones(0, object_name="X")
ok(names(x) == ["Base Layer", "Base Layer Extracted"],
   "X: extracted layer sits above the source (got %s)" % names(x))
ok(r["extracted"]["curves"] == 7,
   "X: 3 location + 4 quaternion curves moved (got %d)"
   % r["extracted"]["curves"])
d = diff(ref, sample(x))
ok(d < 1e-5, "X: combined result unchanged after extract (worst %.2e)" % d)

src_curves = layer_curves(x, 0)
new_curves = layer_curves(x, 1)
ok(not any(core._bone_of_path(p) == "A" for p, _i in src_curves),
   "X: A's curves are GONE from the source layer")
ok(all(core._bone_of_path(p) == "B" for p, _i in src_curves) and src_curves,
   "X: B's curves stayed behind")
ok(all(core._bone_of_path(p) == "A" for p, _i in new_curves),
   "X: the new layer holds only A")
moved = new_curves[('pose.bones["A"].location', 0)]
ok(any(m.type == 'NOISE' for m in moved.modifiers),
   "X: the f-modifier travelled with the curve")
ok(len(moved.keyframe_points) == 2, "X: keys came across intact")

# filter-scoped extract
x2 = make_rig("X2")
core.al_add_layer(object_name="X2")
core.al_select_layer(0, object_name="X2")
key_bone(x2, "A", 1, (0.0, 0, 0), (1, 0, 0, 0))
key_bone(x2, "A", 20, (3.0, 0, 0), (0.7071, 0.7071, 0, 0))
x2.animation_data.use_tweak_mode = False
ref2 = sample(x2)
set_sel(x2, ["A"])
r = core.al_extract_bones(0, name="Rot Only", channels=['ROTATION'],
                          object_name="X2")
ok(r["extracted"]["curves"] == 4, "X: ROTATION filter moves only the 4 quats")
ok(names(x2) == ["Base Layer", "Rot Only"], "X: custom layer name honoured")
ok(all(core._al_channel_of(p) == 'LOCATION' for p, _i in layer_curves(x2, 0)),
   "X: only location left in the source")
d = diff(ref2, sample(x2))
ok(d < 1e-5, "X: filtered extract keeps the result (worst %.2e)" % d)

# ------------------------------------------------- S: share layer keys
s = make_rig("S")
core.al_add_layer(object_name="S")
core.al_select_layer(0, object_name="S")
for f in (1, 7, 19, 28):
    key_bone(s, "A", f, (f / 10.0, 0, 0))
core.al_add_layer(object_name="S")
core.al_select_layer(1, object_name="S")
key_bone(s, "A", 1, (0.0, 0, 0))
key_bone(s, "A", 30, (1.0, 0, 0))
s.animation_data.use_tweak_mode = False
ref_s = sample(s)

set_sel(s, ["A"])
r = core.al_share_keys(0, 1, object_name="S")
top = layer_curves(s, 1)[('pose.bones["A"].location', 0)]
frames = sorted(round(kp.co.x, 2) for kp in top.keyframe_points)
ok(frames == [0.0, 6.0, 18.0, 27.0, 29.0],
   "S: target now carries the source's key frames (got %s)" % frames)
d = diff(ref_s, sample(s))
ok(d < 1e-5,
   "S: sharing timing did NOT change the animation (worst %.2e)" % d)
base_frames = sorted(round(kp.co.x, 2) for kp in
                     layer_curves(s, 0)[('pose.bones["A"].location', 0)]
                     .keyframe_points)
ok(base_frames == [0.0, 6.0, 18.0, 27.0], "S: the source layer is untouched")

before = len(top.keyframe_points)
core.al_share_keys(0, 1, object_name="S")
top = layer_curves(s, 1)[('pose.bones["A"].location', 0)]
ok(len(top.keyframe_points) == before,
   "S: sharing twice adds no duplicates (%d)" % before)


def raises(fn, frag, label):
    try:
        fn()
    except RuntimeError as exc:
        ok(frag.lower() in str(exc).lower(), label + " (msg: %s)" % exc)
    else:
        ok(False, label + " — no error raised")


raises(lambda: core.al_share_keys(1, 1, object_name="S"),
       "different layer", "E: sharing a layer with itself refuses")
set_sel(s, [])
raises(lambda: core.al_share_keys(0, 1, object_name="S"),
       "no bones selected", "E: share with nothing selected refuses")
set_sel(s, ["A"])
s.animation_data.nla_tracks[1].lock = True
raises(lambda: core.al_share_keys(0, 1, object_name="S"),
       "locked", "E: share refuses a locked target")
raises(lambda: core.al_extract_bones(1, object_name="S"),
       "locked", "E: extract refuses a locked layer")
s.animation_data.nla_tracks[1].lock = False
set_sel(s, ["B"])
raises(lambda: core.al_extract_bones(1, object_name="S"),
       "nothing in scope", "E: extract with no matching curves refuses")

print("\n%d passed, %d failed" % (len(PASS), len(FAIL)), flush=True)
if FAIL:
    for f in FAIL:
        print("FAILED: " + f, flush=True)
    sys.exit(1)
