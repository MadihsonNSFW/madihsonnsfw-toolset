# Headless checks for core.py anim-layers Task 4 (mute/solo/lock/blend/influence).
# Run: blender.exe -b --factory-startup --python test_al_state.py
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

core.al_add_layer()          # Base Layer
core.al_add_layer()          # Layer 2
core.al_add_layer()          # Layer 3
ad = ob.animation_data

# ---------------------------------------------------------------- mute
s = core.al_set_layer_state(0, mute=True)
check("mute on", s["layers"][0]["mute"] is True)
s = core.al_set_layer_state(0, mute=False)
check("mute off", s["layers"][0]["mute"] is False)

# ---------------------------------------------------------------- blend
s = core.al_set_layer_state(1, blend_type='MULTIPLY')
check("blend set", s["layers"][1]["blend_type"] == 'MULTIPLY')
try:
    core.al_set_layer_state(1, blend_type='BOGUS')
    check("bad blend refused", False)
except RuntimeError:
    check("bad blend refused", True)
core.al_set_layer_state(1, blend_type='COMBINE')

# ---------------------------------------------------------------- influence
s = core.al_set_layer_state(1, influence=0.35)
check("influence set", abs(s["layers"][1]["influence"] - 0.35) < 1e-4,
      s["layers"][1]["influence"])
s = core.al_set_layer_state(1, influence=7.0)
check("influence clamped", abs(s["layers"][1]["influence"] - 1.0) < 1e-6)

# animated influence toggle seeds a key (empty fcurve would evaluate to 0)
bpy.context.scene.frame_set(5)
s = core.al_set_influence_animated(1, True)
check("animated influence on", s["layers"][1]["animated_influence"] is True)
strip = ad.nla_tracks[1].strips[0]
fc = strip.fcurves.find("influence")
check("toggle seeded a key", fc is not None and len(fc.keyframe_points) == 1,
      len(fc.keyframe_points) if fc else None)

bpy.context.scene.frame_set(20)
s = core.al_set_layer_state(1, influence=0.5, key_influence=True)
check("influence keyed", len(fc.keyframe_points) == 2, len(fc.keyframe_points))
s = core.al_key_influence(1)
check("re-key same frame no dup", s["influence_keys"] == 2, s["influence_keys"])
s = core.al_key_influence(1, delete=True)
check("key deleted", s["influence_keys"] == 1, s["influence_keys"])
try:
    core.al_key_influence(0)
    check("key on non-animated refused", False)
except RuntimeError as exc:
    check("key on non-animated refused", "animated" in str(exc), str(exc))
s = core.al_set_influence_animated(1, False)
check("animated influence off", s["layers"][1]["animated_influence"] is False)

# ---------------------------------------------------------------- solo
core.al_set_layer_state(0, mute=True)   # pre-existing mute to restore later
s = core.al_solo(2)
check("solo on: flag", s["solo"] == ad.nla_tracks[2].name, s["solo"])
check("solo on: others muted", s["layers"][0]["mute"] and s["layers"][1]["mute"]
      and not s["layers"][2]["mute"],
      json.dumps([r["mute"] for r in s["layers"]]))
s = core.al_solo(1)   # switch solo to another layer directly
check("solo switch", s["solo"] == ad.nla_tracks[1].name
      and not s["layers"][1]["mute"] and s["layers"][2]["mute"])
s = core.al_solo(1)   # same index = off
check("solo off via same index", s["solo"] is None)
check("solo restore: pre-mute back", s["layers"][0]["mute"] is True
      and s["layers"][1]["mute"] is False and s["layers"][2]["mute"] is False,
      json.dumps([r["mute"] for r in s["layers"]]))
core.al_set_layer_state(0, mute=False)

# solo survives a rename (both the flag and the restore snapshot)
core.al_solo(2)
s = core.al_rename_layer(2, "Star Layer")
check("solo follows rename", s["solo"] == "Star Layer", s["solo"])
s = core.al_rename_layer(0, "Renamed Base")
s = core.al_solo(None)   # off via None
check("solo off via None", s["solo"] is None)
check("renamed base restored from snapshot",
      s["layers"][0]["mute"] is False,
      json.dumps([r["mute"] for r in s["layers"]]))

# ---------------------------------------------------------------- lock
core.al_select_layer(1)
s = core.al_set_layer_state(1, lock=True)
check("locking selected layer exits tweak", s["in_tweak"] is False)
check("lock reported", s["layers"][1]["lock"] is True
      and s["layers"][1]["locked_reason"] == "track locked")
try:
    core.al_set_layer_state(1, blend_type='ADD')
    check("blend on locked refused", False)
except RuntimeError as exc:
    check("blend on locked refused", "locked" in str(exc), str(exc))
try:
    core.al_set_layer_state(1, influence=0.1)
    check("influence on locked refused", False)
except RuntimeError as exc:
    check("influence on locked refused", "locked" in str(exc))
s = core.al_set_layer_state(1, mute=True)
check("mute on locked allowed", s["layers"][1]["mute"] is True)
core.al_set_layer_state(1, mute=False)
s = core.al_set_layer_state(1, lock=False)
check("unlock re-tweaks selected", s["in_tweak"] is True
      and s["active_index"] == 1, json.dumps(
          {"tweak": s["in_tweak"], "idx": s["active_index"]}))

print("\n%d passed, %d failed" % (PASS, len(FAIL)))
if FAIL:
    print("FAILED:", FAIL)
    sys.exit(1)
print("ALL OK")
