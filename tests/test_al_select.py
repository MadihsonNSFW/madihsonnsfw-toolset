# Headless checks for core.py anim-layers Task 3 (selection/tweak + actions).
# Run: blender.exe -b --factory-startup --python test_al_select.py
import importlib.util
import json
import os
import sys

import bpy

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CORE = os.path.join(_ROOT, "blender_addon", "madi_anim_library", "core.py")
spec = importlib.util.spec_from_file_location("madi_core", CORE)
core = importlib.util.module_from_spec(spec)
sys.modules["madi_core"] = core
spec.loader.exec_module(core)

PASS = 0
FAIL = []


def check(name, cond, extra=""):
    global PASS
    if cond:
        PASS += 1
        print("ok  %s" % name)
    else:
        FAIL.append(name)
        print("FAIL %s  %s" % (name, extra), flush=True)


def make_rig():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    arm = bpy.data.armatures.new("Rig")
    ob = bpy.data.objects.new("Rig", arm)
    bpy.context.collection.objects.link(ob)
    bpy.context.view_layer.objects.active = ob
    ob.select_set(True)
    bpy.context.view_layer.update()
    bpy.ops.object.mode_set(mode='EDIT')
    eb = arm.edit_bones.new("Bone")
    eb.head, eb.tail = (0, 0, 0), (0, 0, 1)
    bpy.ops.object.mode_set(mode='OBJECT')
    return ob


def keycount(a):
    n = 0
    for lay in a.layers:
        for st in lay.strips:
            for cb in st.channelbags:
                for fc in cb.fcurves:
                    n += len(fc.keyframe_points)
    return n


# ---------------------------------------------------------------- stack of 3
ob = make_rig()
core.al_add_layer()                     # Base Layer (+tweak)
core.al_add_layer()                     # Layer 2
s = core.al_add_layer()                 # Layer 3
check("3 layers up", len(s["layers"]) == 3)
ad = ob.animation_data
actions = [t.strips[0].action for t in ad.nla_tracks]

# select sweep: every layer, keys land in ITS action
pb = ob.pose.bones["Bone"]
for i in range(3):
    s = core.al_select_layer(i)
    check("select %d: tweaked" % i, s["in_tweak"] and s["active_index"] == i,
          json.dumps({"tweak": s["in_tweak"], "idx": s["active_index"]}))
    check("select %d: action active" % i,
          ad.action == actions[i],
          ad.action.name if ad.action else None)
    before = keycount(actions[i])
    pb.location = (i, i, i)
    pb.keyframe_insert("location", frame=10 + i)
    check("select %d: key landed" % i, keycount(actions[i]) == before + 3,
          "%d -> %d" % (before, keycount(actions[i])))

# re-select same layer is stable
s = core.al_select_layer(2)
check("re-select stable", s["in_tweak"] and s["active_index"] == 2)

# ---------------------------------------------------------------- locked rows
ad.nla_tracks[1].lock = True
s = core.al_select_layer(1)
check("locked select: no tweak", s["in_tweak"] is False, s["in_tweak"])
check("locked select: selected anyway", s["selected"] == ad.nla_tracks[1].name)
check("locked select: reason reported",
      s["layers"][1]["locked_reason"] == "track locked")
ad.nla_tracks[1].lock = False
s = core.al_select_layer(1)
check("unlocked select: tweak back", s["in_tweak"] is True)

# ---------------------------------------------------------------- set action
walk = bpy.data.actions.new("Walk")
walk.slots.new('OBJECT', "Rig")
# author Walk with real keys via a temp assignment
core._al_exit_tweak(ad)
ad.action = walk
ad.action_slot = walk.slots[0]
pb.location = (7, 7, 7)
pb.keyframe_insert("location", frame=1)
pb.location = (8, 8, 8)
pb.keyframe_insert("location", frame=20)
ad.action = None

s = core.al_set_layer_action(2, "Walk")
check("set action: assigned", s["layers"][2]["action"] == "Walk")
check("set action: reported", s["action_set"]["action"] == "Walk")
st2 = ad.nla_tracks[2].strips[0]
check("set action: slot assigned", st2.action_slot is not None)
try:
    core.al_set_layer_action(2, "NoSuchAction")
    check("missing action refused", False)
except RuntimeError as exc:
    check("missing action refused", "not found" in str(exc), str(exc))

# keys keep landing in the NEW action after a swap while tweaked
core.al_select_layer(2)
before = keycount(walk)
pb.location = (1, 2, 1)
pb.keyframe_insert("location", frame=30)
check("post-swap keys land in Walk", keycount(walk) == before + 3,
      "%d -> %d" % (before, keycount(walk)))

# ---------------------------------------------------------------- auto blend
absolute = bpy.data.actions.new("AbsPose")
absolute.slots.new('OBJECT', "Rig")
core._al_exit_tweak(ad)
ad.action = absolute
ad.action_slot = absolute.slots[0]
pb.scale = (1.0, 1.0, 1.0)
pb.keyframe_insert("scale", frame=1)
pb.rotation_quaternion = (1.0, 0, 0, 0)
pb.keyframe_insert("rotation_quaternion", frame=1)
ad.action = None

delta = bpy.data.actions.new("DeltaPose")
delta.slots.new('OBJECT', "Rig")
ad.action = delta
ad.action_slot = delta.slots[0]
pb.scale = (0.02, 0.0, 0.05)
pb.keyframe_insert("scale", frame=1)
pb.rotation_quaternion = (0.0, 0, 0, 0)
pb.keyframe_insert("rotation_quaternion", frame=1)
ad.action = None

loconly = bpy.data.actions.new("LocOnly")
loconly.slots.new('OBJECT', "Rig")
ad.action = loconly
ad.action_slot = loconly.slots[0]
pb.location = (3, 3, 3)
pb.keyframe_insert("location", frame=1)
ad.action = None

check("guess: absolute -> REPLACE", core.al_guess_blend(absolute) == 'REPLACE',
      core.al_guess_blend(absolute))
check("guess: delta -> ADD", core.al_guess_blend(delta) == 'ADD',
      core.al_guess_blend(delta))
check("guess: no signal -> None", core.al_guess_blend(loconly) is None,
      core.al_guess_blend(loconly))

s = core.al_set_layer_action(2, "AbsPose", auto_blend=True)
check("auto blend applied", s["layers"][2]["blend_type"] == 'REPLACE'
      and s["action_set"]["auto_blend"] == 'REPLACE')
s = core.al_set_layer_action(2, "DeltaPose", auto_blend=True)
check("auto blend ADD applied", s["layers"][2]["blend_type"] == 'ADD')
s = core.al_set_layer_action(2, "LocOnly", auto_blend=True, sync_name=True)
check("no-signal keeps blend", s["layers"][2]["blend_type"] == 'ADD')
check("sync_name renames layer", s["layers"][2]["name"] == "LocOnly",
      s["layers"][2]["name"])

# ---------------------------------------------------------------- action list
acts = core.al_list_actions()
names = [a["name"] for a in acts]
check("action list has all", all(n in names for n in
      ("Walk", "AbsPose", "DeltaPose", "LocOnly")), names)
walk_row = next(a for a in acts if a["name"] == "Walk")
check("action list ranges", walk_row["frame_start"] <= 1.0
      and walk_row["frame_end"] >= 20.0, json.dumps(walk_row))

# ---------------------------------------------------------------- exit clean
s = core.al_select_layer(0)
check("final: tweak on base", s["in_tweak"] and s["active_index"] == 0)
core._al_exit_tweak(ad)
s = core.anim_layers_status()
check("final: exit clean", s["in_tweak"] is False)

print("\n%d passed, %d failed" % (PASS, len(FAIL)))
if FAIL:
    print("FAILED:", FAIL)
    sys.exit(1)
print("ALL OK")
