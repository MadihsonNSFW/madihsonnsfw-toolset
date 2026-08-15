# Task 19 verification: influence key settings (select/hide/mute/lock,
# Local vs Global) — and that they land on the STRIP influence curve only.
# Run: blender.exe -b --factory-startup --python al_test19_influence.py
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


def make_rig(name, bones=("A",)):
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
scene.frame_start, scene.frame_end = 1, 40


def key_bone(ob, bone, frame, loc):
    scene.frame_set(frame)
    pb = ob.pose.bones[bone]
    pb.location = loc
    pb.keyframe_insert("location")


def infl(ob, i):
    return ob.animation_data.nla_tracks[i].strips[0].fcurves.find("influence")


def action_curves(ob, i):
    strip = ob.animation_data.nla_tracks[i].strips[0]
    return list(core._al_action_fcurves_ro(strip.action))


# ============================================================ build 3 layers
x = make_rig("X")
core.al_add_layer(object_name="X")                     # 0 Base Layer
core.al_select_layer(0, object_name="X")
key_bone(x, "A", 1, (0.0, 0.0, 0.0))
key_bone(x, "A", 20, (2.0, 0.0, 0.0))
core.al_add_layer(object_name="X", name="Layer 2")     # 1
core.al_select_layer(1, object_name="X")
key_bone(x, "A", 5, (0.3, 0.0, 0.0))
core.al_add_layer(object_name="X", name="Layer 3")     # 2
x.animation_data.use_tweak_mode = False

# layer 0 + 1 get influence; layer 2 stays untouched (no influence curve yet)
core.al_set_layer_state(0, influence=0.8, object_name="X")
core.al_set_influence_animated(1, True, object_name="X")
scene.frame_set(10)
core.al_set_layer_state(1, influence=0.5, key_influence=True, object_name="X")
scene.frame_set(30)
core.al_set_layer_state(1, influence=1.0, key_influence=True, object_name="X")

ok(infl(x, 0) is not None and len(infl(x, 0).keyframe_points) == 1,
   "layer 0 has a static influence (1 key)")
ok(infl(x, 1) is not None and len(infl(x, 1).keyframe_points) >= 2,
   "layer 1 has a keyed influence (%d keys)"
   % len(infl(x, 1).keyframe_points))
ok(infl(x, 2) is None, "layer 2 has no influence curve at all")

def deselect_everything(ob):
    """Blender selects every key it inserts, so clear the slate before
    testing what the op itself selected."""
    for i, track in enumerate(ob.animation_data.nla_tracks):
        for fc in list(core._al_action_fcurves_ro(track.strips[0].action)) \
                + list(track.strips[0].fcurves):
            for kp in fc.keyframe_points:
                kp.select_control_point = False
                kp.select_left_handle = False
                kp.select_right_handle = False


# ============================================================ LOCAL
deselect_everything(x)
st = core.al_influence_keys(index=1, scope='LOCAL', select=True,
                            object_name="X")
fc = infl(x, 1)
ok(all(kp.select_control_point for kp in fc.keyframe_points),
   "LOCAL select ticks every influence key")
ok(all(kp.select_left_handle and kp.select_right_handle
       for kp in fc.keyframe_points),
   "select takes the handles too (a transform grabs the whole key)")
ok(st["influence_settings"]["layers"] == ["Layer 2"],
   "LOCAL touched exactly one layer (%s)"
   % st["influence_settings"]["layers"])
ok(not any(kp.select_control_point for kp in infl(x, 0).keyframe_points),
   "LOCAL left the other layer's influence keys alone")

core.al_influence_keys(index=1, scope='LOCAL', select=False, object_name="X")
ok(not any(kp.select_control_point for kp in infl(x, 1).keyframe_points),
   "select=False deselects them again")

# ---- hide / mute / lock
core.al_influence_keys(index=1, scope='LOCAL', hide=True, mute=True,
                       object_name="X")
fc = infl(x, 1)
ok(fc.hide is True and fc.mute is True, "hide + mute land on the curve")
core.al_influence_keys(index=1, scope='LOCAL', lock=True, object_name="X")
ok(infl(x, 1).lock is True, "lock lands on the curve")

# a locked curve must still be unlockable
core.al_influence_keys(index=1, scope='LOCAL', lock=False, hide=False,
                       mute=False, object_name="X")
fc = infl(x, 1)
ok(fc.lock is False and fc.hide is False and fc.mute is False,
   "everything can be turned back off (lock is written last)")

# ---- the layer's ANIMATION curves are never touched
for i in (0, 1):
    ok(all(not c.hide and not c.mute and not c.lock
           for c in action_curves(x, i)),
       "layer %d's animation curves keep their own flags" % i)
ok(not any(kp.select_control_point
           for c in action_curves(x, 1) for kp in c.keyframe_points),
   "influence select never selects the animation keys")

# ============================================================ GLOBAL
st = core.al_influence_keys(scope='GLOBAL', select=True, hide=True,
                            object_name="X")
ok(sorted(st["influence_settings"]["layers"]) == ["Base Layer", "Layer 2"],
   "GLOBAL hits every layer that HAS an influence curve (%s)"
   % st["influence_settings"]["layers"])
ok(st["influence_settings"]["skipped"] == ["Layer 3"],
   "layers with no influence curve are reported as skipped (%s)"
   % st["influence_settings"]["skipped"])
ok(infl(x, 0).hide and infl(x, 1).hide, "GLOBAL hide reached both curves")
ok(all(kp.select_control_point for kp in infl(x, 0).keyframe_points)
   and all(kp.select_control_point for kp in infl(x, 1).keyframe_points),
   "GLOBAL select reached both curves")
ok(st["influence_settings"]["keys"]
   == len(infl(x, 0).keyframe_points) + len(infl(x, 1).keyframe_points),
   "the reported key count matches (%d)" % st["influence_settings"]["keys"])
core.al_influence_keys(scope='GLOBAL', select=False, hide=False,
                       object_name="X")

# ---- GLOBAL skips a locked LAYER (its track lock wins)
x.animation_data.nla_tracks[0].lock = True
st = core.al_influence_keys(scope='GLOBAL', mute=True, object_name="X")
ok(st["influence_settings"]["layers"] == ["Layer 2"],
   "GLOBAL skips a track-locked layer (%s)"
   % st["influence_settings"]["layers"])
ok(infl(x, 0).mute is False, "the locked layer's influence curve is untouched")
core.al_influence_keys(scope='GLOBAL', mute=False, object_name="X")

try:
    core.al_influence_keys(index=0, scope='LOCAL', mute=True, object_name="X")
    ok(False, "LOCAL on a locked layer should refuse")
except RuntimeError as exc:
    ok("locked" in str(exc).lower(), "LOCAL on a locked layer refused (%s)"
       % exc)
x.animation_data.nla_tracks[0].lock = False

# ============================================================ guards
try:
    core.al_influence_keys(index=1, scope='NOPE', mute=True, object_name="X")
    ok(False, "bad scope should refuse")
except RuntimeError as exc:
    ok("scope" in str(exc).lower(), "bad scope refused (%s)" % exc)
try:
    core.al_influence_keys(index=1, object_name="X")
    ok(False, "no-op call should refuse")
except RuntimeError as exc:
    ok("nothing to change" in str(exc).lower(),
       "a call that changes nothing is refused (%s)" % exc)
try:
    core.al_influence_keys(index=2, scope='LOCAL', mute=True, object_name="X")
    ok(False, "a layer with no influence curve should refuse")
except RuntimeError as exc:
    ok("influence" in str(exc).lower(),
       "a layer with no influence curve refused (%s)" % exc)

# ============================================================ no side effects
before = [(round(kp.co.x, 4), round(kp.co.y, 4))
          for kp in infl(x, 1).keyframe_points]
core.al_influence_keys(scope='GLOBAL', select=True, hide=True, mute=True,
                       lock=True, object_name="X")
core.al_influence_keys(scope='GLOBAL', select=False, hide=False, mute=False,
                       lock=False, object_name="X")
after = [(round(kp.co.x, 4), round(kp.co.y, 4))
         for kp in infl(x, 1).keyframe_points]
ok(before == after, "no setting ever moves an influence key (%s vs %s)"
   % (before, after))
st = core.anim_layers_status(object_name="X")
ok(st["layers"][1]["animated_influence"] is True,
   "the layer is still marked animated-influence afterwards")
ok(abs(st["layers"][0]["influence"] - 0.8) < 1e-4,
   "the static influence value is unchanged (%.3f)"
   % st["layers"][0]["influence"])

print("\n%d passed, %d failed" % (len(PASS), len(FAIL)), flush=True)
for f in FAIL:
    print("  FAILED: " + f, flush=True)
sys.exit(1 if FAIL else 0)
