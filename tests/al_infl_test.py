# Influence-fix verification: static influence must stay ONE key no matter
# at which frame it's changed; keying only happens with the animated flag on;
# toggling off collapses; legacy multi-key stacks report animated.
# Run: blender.exe -b --factory-startup --python al_infl_test.py
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


arm = bpy.data.armatures.new("R")
ob = bpy.data.objects.new("R", arm)
bpy.context.collection.objects.link(ob)
bpy.context.view_layer.objects.active = ob
ob.select_set(True)
bpy.context.view_layer.update()
bpy.ops.object.mode_set(mode='EDIT')
eb = arm.edit_bones.new("A")
eb.head, eb.tail = (0, 0, 0), (0, 0, 1)
bpy.ops.object.mode_set(mode='OBJECT')

scene = bpy.context.scene
scene.frame_start, scene.frame_end = 1, 30
core.al_add_layer(object_name="R")
core.al_add_layer(object_name="R")
strip = ob.animation_data.nla_tracks[1].strips[0]


def infl_keys():
    fc = strip.fcurves.find("influence")
    return len(fc.keyframe_points) if fc else 0


def row():
    return core.anim_layers_status(object_name="R")["layers"][1]


# --- static writes at DIFFERENT frames stay one key (the reported bug) -----
scene.frame_set(1)
core.al_set_layer_state(1, influence=0.8, object_name="R")
ok(infl_keys() == 1, "static write -> 1 influence key")
scene.frame_set(20)
core.al_set_layer_state(1, influence=0.5, object_name="R")
ok(infl_keys() == 1,
   "static write at another frame STILL 1 key (was the playback-drift bug)")
ok(row()["animated_influence"] is False,
   "status: + toggle reads OFF for a static value")
vals = []
for f in (1, 10, 20, 30):
    scene.frame_set(f)
    vals.append(round(strip.influence, 4))
ok(all(abs(v - 0.5) < 1e-4 for v in vals),
   "influence constant over playback (got %s)" % vals)

# --- flag on -> writes key ---------------------------------------------------
scene.frame_set(5)
core.al_set_influence_animated(1, True, object_name="R")
ok(row()["animated_influence"] is True, "toggle on reports animated")
scene.frame_set(25)
core.al_set_layer_state(1, influence=0.9, object_name="R")
ok(infl_keys() == 2, "with flag on, a write at a new frame keys (2 keys)")
scene.frame_set(5)
v5 = strip.influence
scene.frame_set(25)
v25 = strip.influence
ok(abs(v5 - 0.5) < 1e-3 and abs(v25 - 0.9) < 1e-3,
   "animated influence interpolates between its keys (%.2f / %.2f)"
   % (v5, v25))

# --- toggle off collapses to the current value ------------------------------
scene.frame_set(25)
core.al_set_influence_animated(1, False, object_name="R")
ok(infl_keys() == 1, "toggle off collapses to one key")
ok(abs(strip.influence - 0.9) < 1e-3,
   "collapsed static keeps the current value (%.2f)" % strip.influence)
scene.frame_set(5)
ok(abs(strip.influence - 0.9) < 1e-3,
   "value now constant at every frame")
ok(row()["animated_influence"] is False, "status back to static")

# --- rename follows the flag, delete prunes it ------------------------------
core.al_set_influence_animated(1, True, object_name="R")
core.al_rename_layer(1, "Wobble", object_name="R")
ok(row()["animated_influence"] is True, "rename keeps the animated flag")
core.al_delete_layer(1, object_name="R")
core.al_add_layer(object_name="R", name="Wobble")
ok(core.anim_layers_status(object_name="R")["layers"][1][
       "animated_influence"] is False,
   "recreated same-named layer does NOT inherit the old flag")

# --- legacy fallback: multi-key strip without a flag reports animated -------
strip2 = ob.animation_data.nla_tracks[1].strips[0]
strip2.use_animated_influence = True
scene.frame_set(1)
strip2.influence = 0.3
strip2.keyframe_insert("influence")
scene.frame_set(30)
strip2.influence = 1.0
strip2.keyframe_insert("influence")
ok(core.anim_layers_status(object_name="R")["layers"][1][
       "animated_influence"] is True,
   "legacy multi-key influence (no flag) reports animated")

print("\n%d passed, %d failed" % (len(PASS), len(FAIL)), flush=True)
if FAIL:
    for f in FAIL:
        print("FAILED: " + f, flush=True)
    sys.exit(1)
