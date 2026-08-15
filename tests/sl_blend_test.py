# Studio Library blend semantics on all three item types: pose blend 0/0.5/1
# (lerp + quaternion slerp + numeric custom props), anim blend (keys pulled t
# of the way from the PRE-PASTE state, quats slerp, F-modifiers ride along,
# blend=1 exact), shapes blend (stored deltas x t, slider value/min/max as
# saved, vert-count guard). Behaviour-level: values read back via frame_set.
# Run: blender.exe -b --factory-startup --python sl_blend_test.py
import importlib.util
import json
import math
import os
import sys
import tempfile

import bpy
from mathutils import Quaternion

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


def close(a, b, tol=1e-4):
    return abs(a - b) <= tol


LIB = tempfile.mkdtemp(prefix="madi_blend_")

# --- rig ---------------------------------------------------------------------
arm = bpy.data.armatures.new("R")
ob = bpy.data.objects.new("R", arm)
bpy.context.collection.objects.link(ob)
bpy.context.view_layer.objects.active = ob
ob.select_set(True)
bpy.context.view_layer.update()
bpy.ops.object.mode_set(mode='EDIT')
eb = arm.edit_bones.new("A")
eb.head, eb.tail = (0, 0, 0), (0, 0, 1)
bpy.ops.object.mode_set(mode='POSE')
pb = ob.pose.bones["A"]
pb.rotation_mode = 'QUATERNION'

ROTX90 = Quaternion((1.0, 0.0, 0.0), math.radians(90))
ROTX45 = Quaternion((1.0, 0.0, 0.0), math.radians(45))

# ============================================================ pose blend ===
pb.location.x = 2.0
pb.rotation_quaternion = ROTX90
pb["amt"] = 2.0
core.save_pose(LIB, "", "p", use_selected=False)
item = os.path.join(LIB, "p.pose")
ok(os.path.isdir(item), "pose item saved")
with open(os.path.join(item, "pose.json"), encoding="utf-8") as f:
    meta = json.load(f)["metadata"]
ok("fps" in meta, "metadata carries fps")

def reset_pose():
    pb.location = (0.0, 0.0, 0.0)
    pb.rotation_quaternion = Quaternion((1.0, 0.0, 0.0, 0.0))
    pb["amt"] = 0.0


reset_pose()
core.apply_pose(item, blend=0.0)
ok(close(pb.location.x, 0.0) and close(pb.rotation_quaternion.w, 1.0)
   and close(pb["amt"], 0.0), "blend=0 leaves the pose untouched")

core.apply_pose(item, blend=0.5)
ok(close(pb.location.x, 1.0), "blend=0.5: location halfway")
q = pb.rotation_quaternion
ok(close(q.w, ROTX45.w) and close(q.x, ROTX45.x),
   "blend=0.5: quaternion SLERPED to 45 deg (not lerped)")
ok(close(pb["amt"], 1.0), "blend=0.5: numeric custom prop halfway")

reset_pose()
core.apply_pose(item, blend=1.0)
q = pb.rotation_quaternion
ok(close(pb.location.x, 2.0) and close(q.w, ROTX90.w) and close(q.x, ROTX90.x)
   and close(pb["amt"], 2.0), "blend=1.0: exact restore")

# key option writes keys
core.apply_pose(item, blend=1.0, key=True)
ok(ob.animation_data is not None and ob.animation_data.action is not None
   and any(fc.data_path.endswith("location")
           for fc in core._action_fcurves(ob)),
   "key=True inserts keyframes")
ob.animation_data_clear()

# ============================================================ anim blend ===
scene = bpy.context.scene
scene.frame_start, scene.frame_end = 1, 10

# source anim: loc.x 2 -> 4, quat 90 deg at f1, a Noise modifier on loc.x
scene.frame_set(1)
pb.location.x = 2.0
pb.rotation_quaternion = ROTX90
pb.keyframe_insert("location")
pb.keyframe_insert("rotation_quaternion")
scene.frame_set(10)
pb.location.x = 4.0
pb.keyframe_insert("location")
# noise on Y so the X value reads stay clean (modifiers EVALUATE on top of
# keys — putting it on the checked channel poisons the frame_set reads)
for fc in core._action_fcurves(ob):
    if fc.data_path.endswith("location") and fc.array_index == 1:
        fc.modifiers.new('NOISE')
core.save_anim(LIB, "", "a", frame_start=1, frame_end=10, use_selected=False)
anim_item = os.path.join(LIB, "a.anim")
ok(os.path.isdir(anim_item), "anim item saved")


def reset_anim():
    ob.animation_data_clear()
    reset_pose()
    scene.frame_set(1)
    pb.keyframe_insert("location")
    pb.keyframe_insert("rotation_quaternion")
    scene.frame_set(10)
    pb.keyframe_insert("location")


reset_anim()   # existing state: flat zero keys at 1 and 10
core.apply_anim(anim_item, mode='replace', start_at='original', blend=0.5)
scene.frame_set(1)
ok(close(pb.location.x, 1.0),
   "anim blend=0.5: key pulled halfway from the pre-paste value (f1)")
q = pb.rotation_quaternion
ok(close(q.w, ROTX45.w, 1e-3) and close(q.x, ROTX45.x, 1e-3),
   "anim blend=0.5: quaternion key SLERPED whole")
scene.frame_set(10)
ok(close(pb.location.x, 2.0),
   "anim blend=0.5: second key halfway too (f10)")
mods = [m for fc in core._action_fcurves(ob)
        if fc.data_path.endswith("location") and fc.array_index == 1
        for m in fc.modifiers]
ok(len(mods) == 1 and mods[0].type == 'NOISE',
   "F-modifier copied onto the result at full strength")

reset_anim()
core.apply_anim(anim_item, mode='replace', start_at='original', blend=1.0)
scene.frame_set(1)
v1 = pb.location.x
q = pb.rotation_quaternion
scene.frame_set(10)
ok(close(v1, 2.0) and close(pb.location.x, 4.0),
   "anim blend=1.0: exact key values")
ok(close(q.w, ROTX90.w, 1e-3) and close(q.x, ROTX90.x, 1e-3),
   "anim blend=1.0: exact quaternion")

# =========================================================== shapes blend ==
bpy.ops.object.mode_set(mode='OBJECT')
mesh = bpy.data.meshes.new("P")
mesh.from_pydata([(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)],
                 [], [(0, 1, 2, 3)])
mob = bpy.data.objects.new("P", mesh)
bpy.context.collection.objects.link(mob)
bpy.context.view_layer.objects.active = mob
mob.select_set(True)
bpy.context.view_layer.update()
mob.shape_key_add(name="Basis")
puff = mob.shape_key_add(name="Puff", from_mix=False)
puff.data[0].co.z += 1.0
puff.data[1].co.z += 0.5
puff.value = 0.7
puff.slider_min = -0.2
puff.slider_max = 1.5
core.save_shapes(LIB, "", "s", objects=["P"])
shape_item = os.path.join(LIB, "s.shapes")
ok(os.path.isdir(shape_item), "shapes item saved")


def drop_key():
    kb = mob.data.shape_keys.key_blocks.get("Puff")
    if kb is not None:
        mob.shape_key_remove(kb)


drop_key()
core.apply_shapes(shape_item, blend=0.5)
kb = mob.data.shape_keys.key_blocks.get("Puff")
ok(kb is not None, "key recreated")
ok(close(kb.data[0].co.z, 0.5) and close(kb.data[1].co.z, 0.25),
   "blend=0.5: stored deltas scaled by t (baked in)")
ok(close(kb.value, 0.7) and close(kb.slider_min, -0.2)
   and close(kb.slider_max, 1.5),
   "slider value/min/max restore as SAVED (not scaled)")

drop_key()
core.apply_shapes(shape_item, blend=1.0)
kb = mob.data.shape_keys.key_blocks.get("Puff")
ok(close(kb.data[0].co.z, 1.0) and close(kb.data[1].co.z, 0.5),
   "blend=1.0: exact deltas")

# vert-count guard: a different mesh under the same name is refused
bpy.data.objects.remove(mob)
tri = bpy.data.meshes.new("P")
tri.from_pydata([(0, 0, 0), (1, 0, 0), (0, 1, 0)], [], [(0, 1, 2)])
tob = bpy.data.objects.new("P", tri)
bpy.context.collection.objects.link(tob)
bpy.context.view_layer.objects.active = tob
tob.select_set(True)
tob.shape_key_add(name="Basis")
bpy.context.view_layer.update()
# the guard is a SKIP with a "(refused)" report, not an exception
res = core.apply_shapes(shape_item, blend=1.0)
ok(res["applied"] == 0
   and any("vertex count" in s for s in res["skipped"]),
   "vert-count mismatch REFUSED (skipped with a clear report)")

print("\n%d passed, %d failed" % (len(PASS), len(FAIL)), flush=True)
sys.exit(1 if FAIL else 0)
