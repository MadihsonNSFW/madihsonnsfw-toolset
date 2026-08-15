# Task 12 verification: cyclic f-curves + inbetweener.
# Run: blender.exe -b --factory-startup --python al_tools_test12.py
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
LOC = 'pose.bones["%s".replace("%%s","")]'  # placeholder, real paths below


def set_sel(ob, names):
    for pb in ob.pose.bones:
        core.bone_set_selected(pb, pb.name in names)


def curves(ob, layer_i, bone=None):
    strip = ob.animation_data.nla_tracks[layer_i].strips[0]
    out = []
    for fc in core._al_action_fcurves_ro(strip.action):
        if bone is None or core._bone_of_path(fc.data_path) == bone:
            out.append(fc)
    return out


def fc_of(ob, layer_i, bone, prop, idx):
    path = 'pose.bones["%s"].%s' % (bone, prop)
    for fc in curves(ob, layer_i):
        if fc.data_path == path and fc.array_index == idx:
            return fc
    return None


def key_bone(ob, bone, frame, loc):
    scene.frame_set(frame)
    pb = ob.pose.bones[bone]
    pb.location = loc
    pb.keyframe_insert("location")


# ------------------------------------------------- C: cyclic fcurves
c = make_rig("C")
core.al_add_layer(object_name="C")
core.al_select_layer(0, object_name="C")
key_bone(c, "A", 1, (0.0, 0, 0))
key_bone(c, "A", 10, (2.0, 0, 0))
key_bone(c, "B", 1, (0.0, 0, 0))
key_bone(c, "B", 10, (1.0, 0, 0))
c.animation_data.use_tweak_mode = False

# pre-existing Noise on one curve — the order check
target = fc_of(c, 0, "A", "location", 0)
target.modifiers.new(type='NOISE')

set_sel(c, ["A"])
r = core.al_cyclic_fcurves(0, selected_only=True, object_name="C")
ok(r["cyclic"]["curves"] == 3,
   "C: cycles added to A's 3 location curves (got %d)" % r["cyclic"]["curves"])
types = [m.type for m in target.modifiers]
ok(types == ['NOISE', 'CYCLES'],
   "C: cycles appended BELOW the existing noise (got %s)" % types)
b_fc = fc_of(c, 0, "B", "location", 0)
ok(not any(m.type == 'CYCLES' for m in b_fc.modifiers),
   "C: unselected bone B untouched")

# cycling actually repeats the motion past the keyed range
c.animation_data.use_tweak_mode = False
scene.frame_set(15)
v15 = float(c.pose.bones["A"].location.y)   # y has cycles, no noise
scene.frame_set(6)
v6 = float(c.pose.bones["A"].location.y)
ok(abs(v15 - v6) < 1e-4,
   "C: motion repeats — frame 15 == frame 6 (%.4f / %.4f)" % (v15, v6))

# idempotent, and removable
r = core.al_cyclic_fcurves(0, selected_only=True, object_name="C")
ok(r["cyclic"]["curves"] == 0, "C: running again adds nothing")
ok([m.type for m in target.modifiers] == ['NOISE', 'CYCLES'],
   "C: modifier list unchanged on the second run")
r = core.al_cyclic_fcurves(0, enable=False, selected_only=True,
                           object_name="C")
ok(r["cyclic"]["curves"] == 3, "C: disable removes the 3 cycles modifiers")
ok([m.type for m in target.modifiers] == ['NOISE'],
   "C: the pre-existing noise survives removal")

# filter scoping
set_sel(c, ["A", "B"])
r = core.al_cyclic_fcurves(0, selected_only=True, channels=['LOCATION'],
                           axes=['X'], object_name="C")
ok(r["cyclic"]["curves"] == 2,
   "C: X-only filter hits one curve per bone (got %d)" % r["cyclic"]["curves"])

# a single-key curve has no cycle to repeat
c2 = make_rig("C2")
core.al_add_layer(object_name="C2")
core.al_select_layer(0, object_name="C2")
key_bone(c2, "A", 1, (1.0, 0, 0))
c2.animation_data.use_tweak_mode = False
set_sel(c2, ["A"])
r = core.al_cyclic_fcurves(0, selected_only=True, object_name="C2")
ok(r["cyclic"]["curves"] == 0, "C: single-key curves are skipped")

# ------------------------------------------------- I: inbetweener
i1 = make_rig("I1")
core.al_add_layer(object_name="I1")
core.al_select_layer(0, object_name="I1")
key_bone(i1, "A", 1, (0.0, 0, 0))
key_bone(i1, "A", 21, (10.0, 0, 0))
i1.animation_data.use_tweak_mode = False
set_sel(i1, ["A"])

scene.frame_set(11)
r = core.al_inbetween(0.25, 0, selected_only=True, object_name="I1")
fc = fc_of(i1, 0, "A", "location", 0)
key = next(k for k in fc.keyframe_points if abs(k.co.x - 10.0) < 1e-4)
ok(abs(key.co.y - 2.5) < 1e-5,
   "I: 25%% between 0 and 10 = 2.5 (got %.4f)" % key.co.y)
ok(r["inbetween"]["curves"] == 3, "I: all 3 location curves keyed")

scene.frame_set(16)
core.al_inbetween(1.3, 0, selected_only=True, object_name="I1")
fc = fc_of(i1, 0, "A", "location", 0)
key = next(k for k in fc.keyframe_points if abs(k.co.x - 15.0) < 1e-4)
# neighbours at that frame are now the 2.5 key (frame 10) and 10.0 (frame 20)
expected = 2.5 + (10.0 - 2.5) * 1.3
ok(abs(key.co.y - expected) < 1e-5,
   "I: overshoot t=1.3 goes past the next key (got %.4f, want %.4f)"
   % (key.co.y, expected))

# writes only into the SELECTED layer
i2 = make_rig("I2")
core.al_add_layer(object_name="I2")
core.al_select_layer(0, object_name="I2")
key_bone(i2, "A", 1, (0.0, 0, 0))
key_bone(i2, "A", 21, (4.0, 0, 0))
core.al_add_layer(object_name="I2")            # Layer 2 on top
core.al_select_layer(1, object_name="I2")
key_bone(i2, "A", 1, (0.0, 0, 0))
key_bone(i2, "A", 21, (1.0, 0, 0))
i2.animation_data.use_tweak_mode = False
set_sel(i2, ["A"])
base_keys_before = sum(len(fc.keyframe_points) for fc in curves(i2, 0))
# NB a COMBINE layer stores DELTAS: keying a visual 1.0 over a base of 4.0
# writes -3.0. So the expected inbetween comes from the layer's own keys.
top = fc_of(i2, 1, "A", "location", 0)
pre = sorted((k.co.x, k.co.y) for k in top.keyframe_points)
want = pre[0][1] + (pre[-1][1] - pre[0][1]) * 0.5
scene.frame_set(11)
core.al_inbetween(0.5, 1, selected_only=True, object_name="I2")
base_keys_after = sum(len(fc.keyframe_points) for fc in curves(i2, 0))
ok(base_keys_before == base_keys_after,
   "I: the other layer gained no keys (%d -> %d)"
   % (base_keys_before, base_keys_after))
top = fc_of(i2, 1, "A", "location", 0)
key = next(k for k in top.keyframe_points if abs(k.co.x - 10.0) < 1e-4)
ok(abs(key.co.y - want) < 1e-5,
   "I: key landed in the selected layer, halfway between ITS OWN keys "
   "(got %.3f, want %.3f)" % (key.co.y, want))


def raises(fn, frag, label):
    try:
        fn()
    except RuntimeError as exc:
        ok(frag.lower() in str(exc).lower(), label + " (msg: %s)" % exc)
    else:
        ok(False, label + " — no error raised")


scene.frame_set(28)   # past the last key: no neighbour on the right
raises(lambda: core.al_inbetween(0.5, 0, selected_only=True,
                                 object_name="I1"),
       "both sides", "E: inbetween outside the key range refuses")
set_sel(i1, [])
raises(lambda: core.al_inbetween(0.5, 0, selected_only=True,
                                 object_name="I1"),
       "no bones selected", "E: inbetween with nothing selected refuses")
i1.animation_data.nla_tracks[0].lock = True
set_sel(i1, ["A"])
raises(lambda: core.al_cyclic_fcurves(0, selected_only=True,
                                      object_name="I1"),
       "locked", "E: cyclic refuses a locked layer")

print("\n%d passed, %d failed" % (len(PASS), len(FAIL)), flush=True)
if FAIL:
    for f in FAIL:
        print("FAILED: " + f, flush=True)
    sys.exit(1)
