# Task 7 verification: smart bake (key union, strip-time mapping, key-type
# restore) + steps grid.
# Run: blender.exe -b --factory-startup --python al_bake_test7.py
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


def make_rig(name):
    arm = bpy.data.armatures.new(name)
    ob = bpy.data.objects.new(name, arm)
    bpy.context.collection.objects.link(ob)
    bpy.context.view_layer.objects.active = ob
    ob.select_set(True)
    bpy.context.view_layer.update()
    bpy.ops.object.mode_set(mode='EDIT')
    eb = arm.edit_bones.new("A")
    eb.head, eb.tail = (0, 0, 0), (0, 0, 1)
    bpy.ops.object.mode_set(mode='OBJECT')
    return ob


def loc_x_eval(ob, frames):
    ad = ob.animation_data
    if ad and ad.use_tweak_mode:
        ad.use_tweak_mode = False
    out = []
    for f in frames:
        bpy.context.scene.frame_set(f)
        out.append(float(ob.pose.bones["A"].location.x))
    return out


def layer_action_fc(ob, layer_i, path, index):
    strip = ob.animation_data.nla_tracks[layer_i].strips[0]
    for fc in core._al_action_fcurves_ro(strip.action):
        if fc.data_path == path and fc.array_index == index:
            return fc
    return None


def key_loc(ob, frame, vec):
    bpy.context.scene.frame_set(frame)
    pb = ob.pose.bones["A"]
    pb.location = vec
    pb.keyframe_insert("location")


def set_interp(fc, mode):
    for kp in fc.keyframe_points:
        kp.interpolation = mode
    fc.update()


scene = bpy.context.scene
scene.frame_start, scene.frame_end = 1, 30
LOC = 'pose.bones["A"].location'

# ------------------------------------------------- R1: smart union + key types
r1 = make_rig("R1")
core.al_add_layer(object_name="R1")            # Base Layer, tweaked
for f, v in ((1, (0, 0, 0)), (15, (1, 2, 3)), (30, (0.5, -1, 2))):
    key_loc(r1, f, v)
fc = layer_action_fc(r1, 0, LOC, 0)
for kp in fc.keyframe_points:
    kp.interpolation = 'BEZIER'
# NB action frames are offset from scene frames (empty-action strips start at
# action frame 0) — grab the middle key by order, not by frame number
mid = sorted(fc.keyframe_points, key=lambda k: k.co.x)[1]
mid.handle_left_type = mid.handle_right_type = 'VECTOR'
fc.update()
core.al_add_layer(object_name="R1")            # Layer 2 -> ADD, CONSTANT keys
core.al_set_layer_state(1, blend_type='ADD', object_name="R1")
core.al_select_layer(1, object_name="R1")
for f, v in ((10, (0.2, 0, 0)), (20, (-0.3, 0, 0.1))):
    key_loc(r1, f, v)
set_interp(layer_action_fc(r1, 1, LOC, 0), 'CONSTANT')
r1.animation_data.use_tweak_mode = False

ref = loc_x_eval(r1, range(1, 31))
r = core.al_bake(mode='NEW', direction='ALL', smart=True, object_name="R1")
ok(r["baked"]["smart"] is True, "R1: report says smart")
bfc = layer_action_fc(r1, 2, LOC, 0)
frames = [round(kp.co.x, 4) for kp in bfc.keyframe_points]
ok(frames == [1.0, 10.0, 15.0, 20.0, 30.0],
   "R1: baked key times = source union (got %s)" % frames)
interps = [kp.interpolation for kp in bfc.keyframe_points]
ok(interps == ['BEZIER', 'CONSTANT', 'BEZIER', 'CONSTANT', 'BEZIER'],
   "R1: interpolation restored, higher layer wins (got %s)" % interps)
k15 = bfc.keyframe_points[2]
ok(k15.handle_left_type == 'VECTOR' and k15.handle_right_type == 'VECTOR',
   "R1: handle types restored on the VECTOR key")
worst = max(abs(bfc.evaluate(f) - ref[f - 1]) for f in (1, 10, 15, 20, 30))
ok(worst < 1e-5, "R1: values at keys exact (worst %.2e)" % worst)
kc = r["baked"]["keys"]
ok(kc == 15, "R1: 3 loc channels x 5 keys reported (got %d)" % kc)

# ------------------------------------------------- R2: repeat + scale mapping
r2 = make_rig("R2")
core.al_add_layer(object_name="R2")
for f, v in ((1, 0.0), (2, 3.0), (3, 1.0), (4, 4.0), (5, 0.0)):  # loop-safe
    key_loc(r2, f, (v, 0, 0))
set_interp(layer_action_fc(r2, 0, LOC, 0), 'LINEAR')
set_interp(layer_action_fc(r2, 0, LOC, 1), 'LINEAR')
set_interp(layer_action_fc(r2, 0, LOC, 2), 'LINEAR')
r2.animation_data.use_tweak_mode = False
strip = r2.animation_data.nla_tracks[0].strips[0]
# tweak-inserted keys live at ACTION frames 0..4 — align the window to them
# so the repeat cycle is continuous (v at window start == v at window end)
strip.action_frame_start, strip.action_frame_end = 0.0, 4.0
strip.scale = 2.0
strip.repeat = 2.0
# every live op runs the range auto-repair, which extends the strip to the
# scene end — settle that BEFORE the reference so ref and bake see one state
core._al_ensure_ranges(r2.animation_data)
ref2 = loc_x_eval(r2, range(1, 31))
core.al_bake(mode='NEW', direction='ALL', smart=True, object_name="R2")
r2.animation_data.nla_tracks[0].mute = True
got2 = loc_x_eval(r2, range(1, 31))
worst = max(abs(a - b) for a, b in zip(ref2, got2))
ok(worst < 1e-4,
   "R2: repeat+scale strip baked exactly at every frame (worst %.2e)" % worst)
bfc = layer_action_fc(r2, 1, LOC, 0)
bframes = {round(kp.co.x, 3) for kp in bfc.keyframe_points}
ok(1.0 in bframes and 9.0 in bframes,
   "R2: cycle keys mapped (1 and 9 in %s)" % sorted(bframes))
r2.animation_data.nla_tracks[0].mute = False

# ------------------------------------------------- R3: reversed strip
r3 = make_rig("R3")
core.al_add_layer(object_name="R3")
for f, v in ((1, 0.0), (2, 3.0), (3, 1.0), (4, 4.0), (5, 0.0)):
    key_loc(r3, f, (v, 0, 0))
for i in range(3):
    set_interp(layer_action_fc(r3, 0, LOC, i), 'LINEAR')
r3.animation_data.use_tweak_mode = False
strip = r3.animation_data.nla_tracks[0].strips[0]
strip.action_frame_start, strip.action_frame_end = 0.0, 4.0
strip.scale = 2.0
strip.repeat = 2.0
strip.use_reverse = True
core._al_ensure_ranges(r3.animation_data)
ref3 = loc_x_eval(r3, range(1, 31))
core.al_bake(mode='NEW', direction='ALL', smart=True, object_name="R3")
r3.animation_data.nla_tracks[0].mute = True
got3 = loc_x_eval(r3, range(1, 31))
worst = max(abs(a - b) for a, b in zip(ref3, got3))
ok(worst < 1e-4,
   "R3: reversed strip baked exactly at every frame (worst %.2e)" % worst)
r3.animation_data.nla_tracks[0].mute = False

# ------------------------------------------------- R4: offset strip
r4 = make_rig("R4")
core.al_add_layer(object_name="R4")
for f, v in ((1, 0.0), (2, 3.0), (3, 1.0), (4, 4.0), (5, 0.0)):
    key_loc(r4, f, (v, 0, 0))
for i in range(3):
    set_interp(layer_action_fc(r4, 0, LOC, i), 'LINEAR')
r4.animation_data.use_tweak_mode = False
strip = r4.animation_data.nla_tracks[0].strips[0]
strip.action_frame_start, strip.action_frame_end = 1.0, 5.0
strip.frame_start = 11.0
core._al_ensure_ranges(r4.animation_data)
ref4 = loc_x_eval(r4, range(1, 31))
core.al_bake(mode='NEW', direction='ALL', smart=True, object_name="R4")
r4.animation_data.nla_tracks[0].mute = True
got4 = loc_x_eval(r4, range(1, 31))
worst = max(abs(a - b) for a, b in zip(ref4, got4))
ok(worst < 1e-4,
   "R4: offset strip baked exactly at every frame (worst %.2e)" % worst)
bfc = layer_action_fc(r4, 1, LOC, 0)
bframes = {round(kp.co.x, 3) for kp in bfc.keyframe_points}
ok(11.0 in bframes, "R4: shifted key mapped to 11 (got %s)" % sorted(bframes))
r4.animation_data.nla_tracks[0].mute = False

# ------------------------------------------------- R5: steps grid
r5 = make_rig("R5")
core.al_add_layer(object_name="R5")
key_loc(r5, 1, (0.0, 0, 0.5))
key_loc(r5, 30, (6.0, 0, 0.5))
for i in range(3):
    set_interp(layer_action_fc(r5, 0, LOC, i), 'LINEAR')
r5.animation_data.use_tweak_mode = False
ref5 = loc_x_eval(r5, range(1, 31))
r = core.al_bake(mode='NEW', direction='ALL', smart=False, steps=7,
                 object_name="R5")
ok(r["baked"]["steps"] == 7, "R5: report says steps=7")
bfc = layer_action_fc(r5, 1, LOC, 0)
frames = [round(kp.co.x, 3) for kp in bfc.keyframe_points]
ok(frames == [1.0, 8.0, 15.0, 22.0, 29.0, 30.0],
   "R5: steps grid + forced range end (got %s)" % frames)
zfc = layer_action_fc(r5, 1, LOC, 2)
ok(len(zfc.keyframe_points) == 1,
   "R5: constant channel still collapses to one key")
worst = max(abs(bfc.evaluate(f) - ref5[f - 1]) for f in (1, 8, 15, 22, 29, 30))
ok(worst < 1e-5, "R5: values exact at grid keys (worst %.2e)" % worst)
r5.animation_data.nla_tracks[0].mute = True
got5 = loc_x_eval(r5, range(1, 31))
worst = max(abs(a - b) for a, b in zip(ref5, got5))
ok(worst < 1e-4, "R5: linear ramp survives steps bake (worst %.2e)" % worst)

print("\n%d passed, %d failed" % (len(PASS), len(FAIL)), flush=True)
if FAIL:
    for f in FAIL:
        print("FAILED: " + f, flush=True)
    sys.exit(1)
