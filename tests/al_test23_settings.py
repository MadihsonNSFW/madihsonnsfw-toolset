# Task 23 verification (headless): Sync Layer/Action Name (both directions),
# the Auto Blend heuristic, and the default blend type for new layers.
# Run: blender.exe -b --factory-startup --python al_test23_settings.py
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
scene.frame_start, scene.frame_end = 1, 30


def tracks(ob):
    return [t.name for t in ob.animation_data.nla_tracks]


def actions(ob):
    return [t.strips[0].action.name if t.strips and t.strips[0].action else None
            for t in ob.animation_data.nla_tracks]


def key_bone(ob, bone, frame, loc=None, scale=None, quat=None):
    scene.frame_set(frame)
    pb = ob.pose.bones[bone]
    if loc is not None:
        pb.location = loc
        pb.keyframe_insert("location")
    if scale is not None:
        pb.scale = scale
        pb.keyframe_insert("scale")
    if quat is not None:
        pb.rotation_quaternion = quat
        pb.keyframe_insert("rotation_quaternion")


# ============================================== default blend type
x = make_rig("X")
core.al_add_layer(object_name="X")                       # base -> REPLACE
st = core.al_add_layer(object_name="X", blend_type='ADD')
ok(st["layers"][0]["blend_type"] == "REPLACE",
   "the base layer is always Replace (%s)" % st["layers"][0]["blend_type"])
ok(st["layers"][1]["blend_type"] == "ADD",
   "a new layer takes the requested default blend (%s)"
   % st["layers"][1]["blend_type"])
st = core.al_add_layer(object_name="X", blend_type='MULTIPLY')
ok(st["layers"][2]["blend_type"] == "MULTIPLY",
   "and again for another type (%s)" % st["layers"][2]["blend_type"])
try:
    core.al_add_layer(object_name="X", blend_type='NOPE')
    ok(False, "a bad default blend should refuse")
except RuntimeError as exc:
    ok("blend type" in str(exc).lower(), "bad blend type refused (%s)" % exc)

# ============================================== rename -> action (sync on)
core.al_rename_layer(1, "Wave", sync_action=True, object_name="X")
ok(tracks(x)[1] == "Wave" and actions(x)[1] == "Wave",
   "rename with sync renames the action too (%s / %s)"
   % (tracks(x)[1], actions(x)[1]))

core.al_rename_layer(2, "Blink", sync_action=False, object_name="X")
ok(tracks(x)[2] == "Blink" and actions(x)[2] != "Blink",
   "rename with sync OFF leaves the action alone (%s / %s)"
   % (tracks(x)[2], actions(x)[2]))

# ============================================== action -> layer (the other way)
strip = x.animation_data.nla_tracks[1].strips[0]
strip.action.name = "Wave Cycle"
ok(tracks(x)[1] == "Wave",
   "renaming the action alone does not move the layer name yet")
renamed = core.al_sync_layer_names(object_name="X")
ok(tracks(x)[1] == "Wave Cycle",
   "sync pulls the layer name back from the action (%s)" % tracks(x)[1])
ok(renamed and renamed[0]["from"] == "Wave"
   and renamed[0]["to"] == "Wave Cycle",
   "and reports what it renamed (%s)" % renamed)
ok(x.animation_data.nla_tracks[1].strips[0].name == "Wave Cycle",
   "the strip name follows too")

ok(core.al_sync_layer_names(object_name="X") == [],
   "a second sync has nothing to do (idempotent)")

# ---- the flag dicts must travel with the rename
core.al_set_frame_range(index=1, custom=True, frame_start=3.0,
                        frame_end=20.0, object_name="X")
core.al_set_influence_animated(1, True, object_name="X")
x.animation_data.nla_tracks[1].strips[0].action.name = "Wave Final"
core.al_sync_layer_names(object_name="X")
ok("Wave Final" in core._al_range_flags(x),
   "the custom-range flag followed the sync rename (%s)"
   % core._al_range_flags(x))
ok(core._al_infl_flags(x).get("Wave Final") is True,
   "the animated-influence flag followed too (%s)" % core._al_infl_flags(x))
core.al_select_layer(1, object_name="X")
ok(abs(x.animation_data.nla_tracks[1].strips[0].frame_end - 20.0) < 1e-3,
   "so the custom range still holds after the sync (%.1f)"
   % x.animation_data.nla_tracks[1].strips[0].frame_end)

# ---- the layer being TWEAKED still syncs (tweak holds a user ref of its
# own on the action, and a fake user counts too — neither means "shared")
ok(x.animation_data.use_tweak_mode is True,
   "layer 1 is being tweaked right now")
tweaked_action = x.animation_data.nla_tracks[1].strips[0].action
ok(tweaked_action.users > 1,
   "so its action reports %d users" % tweaked_action.users)
tweaked_action.name = "Wave Tweaked"
core.al_sync_layer_names(object_name="X")
ok(tracks(x)[1] == "Wave Tweaked",
   "the tweaked layer still syncs (%s)" % tracks(x)[1])
tweaked_action.use_fake_user = True
tweaked_action.name = "Wave Fake"
core.al_sync_layer_names(object_name="X")
ok(tracks(x)[1] == "Wave Fake",
   "a fake user does not make it look shared either (%s)" % tracks(x)[1])
tweaked_action.use_fake_user = False

# ---- the solo snapshot follows as well
core.al_solo(1, object_name="X")
x.animation_data.nla_tracks[1].strips[0].action.name = "Wave Solo"
core.al_sync_layer_names(object_name="X")
st = core.anim_layers_status(object_name="X")
ok(st["solo"] == "Wave Solo",
   "the solo state followed the sync rename (%s)" % st["solo"])
core.al_solo(None, object_name="X")

# ---- a SHARED action must never be chased (two layers would fight forever)
core.al_duplicate_layer(1, linked=True, object_name="X")
shared = x.animation_data.nla_tracks[1].strips[0].action
ok(shared.users >= 2, "the duplicate shares the action (%d users)"
   % shared.users)
before = tracks(x)
shared.name = "Shared Name"
renamed = core.al_sync_layer_names(object_name="X")
ok(renamed == [] and tracks(x) == before,
   "a shared action is left alone (%s -> %s)" % (before, tracks(x)))
core.al_delete_layer(2, object_name="X")

# ---- locked layers are skipped
x.animation_data.nla_tracks[1].lock = True
x.animation_data.nla_tracks[1].strips[0].action.name = "Locked Rename"
ok(core.al_sync_layer_names(object_name="X") == [],
   "a locked layer is never renamed by the sync")
x.animation_data.nla_tracks[1].lock = False

# ============================================== auto blend heuristic
y = make_rig("Y")
core.al_add_layer(object_name="Y")
core.al_select_layer(0, object_name="Y")
# an ABSOLUTE pose action: scale ~1, quaternion w ~1
key_bone(y, "A", 1, loc=(0, 0, 0), scale=(1.0, 1.0, 1.0), quat=(1, 0, 0, 0))
key_bone(y, "A", 20, loc=(2, 0, 0), scale=(1.1, 1.0, 1.0), quat=(0.99, 0.1, 0, 0))
y.animation_data.use_tweak_mode = False
absolute = x.animation_data.nla_tracks[0].strips[0].action
absolute = y.animation_data.nla_tracks[0].strips[0].action
ok(core.al_guess_blend(absolute) == 'REPLACE',
   "an absolute pose action guesses Replace (%s)"
   % core.al_guess_blend(absolute))

# a DELTA action: scale ~0, quaternion w ~0 (what an additive layer stores)
delta = bpy.data.actions.new("Delta")
delta.slots.new(y.id_type, y.name)
fcs = core._al_action_fcurve_container(delta, y)
for path, idx, val in (('pose.bones["A"].scale', 0, 0.05),
                       ('pose.bones["A"].scale', 1, 0.0),
                       ('pose.bones["A"].rotation_quaternion', 0, 0.02)):
    fc = fcs.new(path, index=idx)
    fc.keyframe_points.insert(1.0, val)
    fc.keyframe_points.insert(20.0, val)
    fc.update()
ok(core.al_guess_blend(delta) == 'ADD',
   "a delta action guesses Add (%s)" % core.al_guess_blend(delta))

empty = bpy.data.actions.new("Empty")
empty.slots.new(y.id_type, y.name)
ok(core.al_guess_blend(empty) is None,
   "an action with no scale/quaternion signal guesses nothing (%s)"
   % core.al_guess_blend(empty))

# ---- applied through al_set_layer_action
core.al_add_layer(object_name="Y", blend_type='COMBINE')
st = core.al_set_layer_action(1, "Delta", auto_blend=True, object_name="Y")
ok(st["layers"][1]["blend_type"] == "ADD",
   "loading a delta action with auto blend sets Add (%s)"
   % st["layers"][1]["blend_type"])
ok(st["action_set"]["auto_blend"] == "ADD",
   "and reports the guess (%s)" % st["action_set"])

core.al_set_layer_state(1, blend_type='COMBINE', object_name="Y")
st = core.al_set_layer_action(1, "Delta", auto_blend=False, object_name="Y")
ok(st["layers"][1]["blend_type"] == "COMBINE",
   "auto blend OFF leaves the blend type alone (%s)"
   % st["layers"][1]["blend_type"])
ok(st["action_set"]["auto_blend"] is None, "and reports no guess")

# ---- sync_name on set_layer_action
st = core.al_set_layer_action(1, "Delta", sync_name=True, object_name="Y")
ok(tracks(y)[1] == "Delta",
   "loading an action with name sync renames the layer (%s)" % tracks(y)[1])

print("\n%d passed, %d failed" % (len(PASS), len(FAIL)), flush=True)
for f in FAIL:
    print("  FAILED: " + f, flush=True)
sys.exit(1 if FAIL else 0)
