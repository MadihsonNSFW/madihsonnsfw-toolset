# Task 11 verification: select-bones-in-layer, reset-key-layer, and the
# shared channel/axis filter.
# Run: blender.exe -b --factory-startup --python al_tools_test11.py
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


def make_rig(name, bones=("A", "B", "C")):
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


def sel(ob):
    return sorted(pb.name for pb in ob.pose.bones if core.bone_is_selected(pb))


def set_sel(ob, names):
    for pb in ob.pose.bones:
        core.bone_set_selected(pb, pb.name in names)


def pose_sample(ob, bones=("A", "B", "C")):
    ad = ob.animation_data
    if ad and ad.use_tweak_mode:
        ad.use_tweak_mode = False
    out = []
    for f in range(1, 31):
        bpy.context.scene.frame_set(f)
        row = []
        for b in bones:
            pb = ob.pose.bones[b]
            row.extend(list(pb.location) + list(pb.rotation_quaternion)
                       + list(pb.scale))
        out.append([float(v) for v in row])
    return out


def diff(a, b):
    return max(abs(x - y) for ra, rb in zip(a, b) for x, y in zip(ra, rb))


# ---------------------------------------------------------------- filter unit
LOC = 'pose.bones["A"].location'
QUAT = 'pose.bones["A"].rotation_quaternion'
SCL = 'pose.bones["A"].scale'
PROP = 'pose.bones["A"]["squish"]'
ok(core._al_channel_of(LOC) == 'LOCATION'
   and core._al_channel_of(QUAT) == 'ROTATION'
   and core._al_channel_of(SCL) == 'SCALE'
   and core._al_channel_of(PROP) is None, "filter: channel classification")
ok(core._al_axis_of(QUAT, 0) == "W" and core._al_axis_of(QUAT, 3) == "Z"
   and core._al_axis_of(LOC, 0) == "X" and core._al_axis_of(LOC, 2) == "Z",
   "filter: quaternion carries W at 0, location starts at X")
ok(core._al_filter_ok(LOC, 0) and core._al_filter_ok(PROP, 0),
   "filter: no filter passes everything incl. custom props")
ok(core._al_filter_ok(LOC, 0, channels=['LOCATION'])
   and not core._al_filter_ok(SCL, 0, channels=['LOCATION'])
   and not core._al_filter_ok(PROP, 0, channels=['LOCATION']),
   "filter: channel filter drops other channels and custom props")
ok(core._al_filter_ok(LOC, 0, axes=['X']) and not core._al_filter_ok(
       LOC, 1, axes=['X']), "filter: axis filter")
ok(core._al_filter_ok(QUAT, 0, axes=['W'])
   and not core._al_filter_ok(QUAT, 1, axes=['W']),
   "filter: W axis only matches quaternion index 0")

# ------------------------------------------------- S: select bones in layer
s = make_rig("S")
core.al_add_layer(object_name="S")                 # Base Layer
core.al_select_layer(0, object_name="S")
for bone, chan in (("A", "location"), ("B", "scale")):
    pb = s.pose.bones[bone]
    scene.frame_set(1)
    pb.keyframe_insert(chan)
s.animation_data.use_tweak_mode = False
set_sel(s, [])
r = core.al_select_bones_in_layer(0, object_name="S")
ok(sel(s) == ["A", "B"], "S: selects exactly the layer's bones (got %s)" % sel(s))
ok(r["selected_bones"]["layer"] == "Base Layer", "S: reports the layer name")
set_sel(s, [])
core.al_select_bones_in_layer(0, channels=['LOCATION'], object_name="S")
ok(sel(s) == ["A"], "S: channel filter narrows to A (got %s)" % sel(s))
set_sel(s, ["C"])
core.al_select_bones_in_layer(0, channels=['SCALE'], extend=True,
                              object_name="S")
ok(sel(s) == ["B", "C"], "S: extend keeps prior selection (got %s)" % sel(s))
set_sel(s, ["C"])
core.al_select_bones_in_layer(0, channels=['SCALE'], object_name="S")
ok(sel(s) == ["B"], "S: without extend the old selection is cleared")

# ------------------------------------------------- R: reset key layer
r1 = make_rig("R1")
core.al_add_layer(object_name="R1")                # Base Layer, REPLACE
core.al_select_layer(0, object_name="R1")
for f, loc in ((1, (0.0, 0, 0)), (30, (4.0, 0, 0))):
    scene.frame_set(f)
    pb = r1.pose.bones["A"]
    pb.location = loc
    pb.keyframe_insert("location")
core.al_add_layer(object_name="R1")                # Layer 2, COMBINE
core.al_select_layer(1, object_name="R1")
for f, loc in ((1, (1.0, 0, 0)), (30, (2.0, 0, 0)))  :
    scene.frame_set(f)
    pb = r1.pose.bones["A"]
    pb.location = loc
    pb.keyframe_insert("location")
r1.animation_data.use_tweak_mode = False

scene.frame_set(15)
base_only = []
r1.animation_data.nla_tracks[1].mute = True
for f in range(1, 31):
    scene.frame_set(f)
    base_only.append(float(r1.pose.bones["A"].location.x))
r1.animation_data.nla_tracks[1].mute = False

set_sel(r1, ["A"])
scene.frame_set(15)
res = core.al_reset_layer(1, selected_only=True, object_name="R1")
ok(res["reset"]["channels"] == 10,
   "R: reset writes loc(3)+quat(4)+scale(3) = 10 channels (got %d)"
   % res["reset"]["channels"])
scene.frame_set(15)
ok(abs(float(r1.pose.bones["A"].location.x) - base_only[14]) < 1e-5,
   "R: at the reset frame the layer contributes nothing (base value shows)")
ok(abs(float(r1.pose.bones["A"].location.x) - base_only[14]) < 1e-5,
   "R: base layer itself is untouched")
strip0 = r1.animation_data.nla_tracks[0].strips[0]
ok(len(list(core._al_action_fcurves_ro(strip0.action))) == 3,
   "R: base layer's action still has just its 3 location curves")

# repeated reset at the same frame must not pile up duplicate keys
before = sum(len(fc.keyframe_points) for fc in core._al_action_fcurves_ro(
    r1.animation_data.nla_tracks[1].strips[0].action))
core.al_reset_layer(1, selected_only=True, object_name="R1")
after = sum(len(fc.keyframe_points) for fc in core._al_action_fcurves_ro(
    r1.animation_data.nla_tracks[1].strips[0].action))
ok(before == after, "R: resetting twice at one frame adds no duplicate keys "
   "(%d -> %d)" % (before, after))

# filter-scoped reset only touches the filtered channels
r2 = make_rig("R2")
core.al_add_layer(object_name="R2")
core.al_add_layer(object_name="R2")
set_sel(r2, ["A"])
scene.frame_set(10)
res = core.al_reset_layer(1, selected_only=True, channels=['LOCATION'],
                          object_name="R2")
ok(res["reset"]["channels"] == 3,
   "R: LOCATION-only reset writes 3 channels (got %d)"
   % res["reset"]["channels"])
res = core.al_reset_layer(1, selected_only=True, channels=['LOCATION'],
                          axes=['X'], object_name="R2")
ok(res["reset"]["channels"] == 1, "R: location+X reset writes 1 channel")

# rest values are the RNA defaults (scale 1, quat W 1)
r3 = make_rig("R3")
core.al_add_layer(object_name="R3")
set_sel(r3, ["A"])
scene.frame_set(5)
core.al_reset_layer(0, selected_only=True, object_name="R3")
strip = r3.animation_data.nla_tracks[0].strips[0]
vals = {(fc.data_path, fc.array_index): fc.keyframe_points[0].co.y
        for fc in core._al_action_fcurves_ro(strip.action)}
ok(abs(vals[(SCL, 0)] - 1.0) < 1e-6 and abs(vals[(QUAT, 0)] - 1.0) < 1e-6
   and abs(vals[(LOC, 0)]) < 1e-6,
   "R: rest values = scale 1, quat W 1, location 0")

# ------------------------------------------------- E: errors
def raises(fn, frag, label):
    try:
        fn()
    except RuntimeError as exc:
        ok(frag.lower() in str(exc).lower(), label + " (msg: %s)" % exc)
    else:
        ok(False, label + " — no error raised")


set_sel(r3, [])
raises(lambda: core.al_reset_layer(0, selected_only=True, object_name="R3"),
       "no bones in scope", "E: reset with nothing selected refuses")
r3.animation_data.nla_tracks[0].lock = True
set_sel(r3, ["A"])
raises(lambda: core.al_reset_layer(0, selected_only=True, object_name="R3"),
       "locked", "E: reset refuses a locked layer")
r3.animation_data.nla_tracks[0].lock = False
raises(lambda: core.al_reset_layer(0, selected_only=True,
                                   channels=['LOCATION'], axes=['W'],
                                   object_name="R3"),
       "nothing to reset", "E: impossible filter refuses")

print("\n%d passed, %d failed" % (len(PASS), len(FAIL)), flush=True)
if FAIL:
    for f in FAIL:
        print("FAILED: " + f, flush=True)
    sys.exit(1)
