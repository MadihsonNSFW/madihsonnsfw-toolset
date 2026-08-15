# Headless checks for core.py anim-layers Task 2 (layer CRUD).
# Run: blender.exe -b --factory-startup --python test_al_crud.py
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
    bpy.context.view_layer.update()  # crash guard: never key a fresh object
    bpy.ops.object.mode_set(mode='EDIT')
    eb = arm.edit_bones.new("Bone")
    eb.head, eb.tail = (0, 0, 0), (0, 0, 1)
    bpy.ops.object.mode_set(mode='OBJECT')
    return ob


def names(s):
    return [r["name"] for r in s["layers"]]


# ------------------------------------------------- 1: add on a bare rig
ob = make_rig()
s = core.al_add_layer()
check("bare add: 1 layer", len(s["layers"]) == 1, json.dumps(names(s)))
check("bare add: no adoption", s["adopted_base"] is None)
check("bare add: base is REPLACE", s["layers"][0]["blend_type"] == 'REPLACE')
check("bare add: named Base Layer", s["layers"][0]["name"] == "Base Layer")
check("bare add: tweaked onto it", s["in_tweak"] and s["active_index"] == 0,
      "%s %s" % (s["in_tweak"], s["active_index"]))
check("bare add: managed", s["managed"] is True)

# keying now must land in the base layer's action
pb = ob.pose.bones["Bone"]
pb.location = (5, 0, 0)
pb.keyframe_insert("location", frame=10)
base_action = ob.animation_data.nla_tracks[0].strips[0].action
def keycount(a):
    n = 0
    for lay in a.layers:
        for st in lay.strips:
            for cb in st.channelbags:
                for fc in cb.fcurves:
                    n += len(fc.keyframe_points)
    return n
check("key lands in base layer", keycount(base_action) == 3,
      keycount(base_action))

# ------------------------------------------------- 2: second layer on top
s = core.al_add_layer()
check("add 2: two layers", len(s["layers"]) == 2)
check("add 2: on top", s["layers"][1]["name"] == "Layer 2")
check("add 2: COMBINE default", s["layers"][1]["blend_type"] == 'COMBINE')
check("add 2: tweak moved to top", s["in_tweak"] and s["active_index"] == 1)
check("add 2: strip spans scene", s["layers"][1]["frame_start"] <= 1
      and s["layers"][1]["frame_end"] >= 250,
      "%s-%s" % (s["layers"][1]["frame_start"], s["layers"][1]["frame_end"]))

# ------------------------------------------------- 3: adoption path
ob = make_rig()
ad = ob.animation_data_create()
act = bpy.data.actions.new("WalkCycle")
ad.action = act
if ad.action_slot is None and act.slots:
    ad.action_slot = act.slots[0]
pb = ob.pose.bones["Bone"]
pb.location = (1, 2, 3)
pb.keyframe_insert("location", frame=1)
pb.location = (4, 5, 6)
pb.keyframe_insert("location", frame=20)
s = core.al_add_layer()
check("adopt: base created", s["adopted_base"] == "WalkCycle",
      s["adopted_base"])
check("adopt: two layers", names(s) == ["WalkCycle", "Layer 2"], names(s))
check("adopt: base holds the action", s["layers"][0]["action"] == "WalkCycle")
check("adopt: base REPLACE", s["layers"][0]["blend_type"] == 'REPLACE')
check("adopt: active action moved off", ob.animation_data.action ==
      ob.animation_data.nla_tracks[1].strips[0].action)

# stack result must still equal the original animation
scn = bpy.context.scene
scn.frame_set(20)
dg = bpy.context.evaluated_depsgraph_get()
loc = ob.evaluated_get(dg).pose.bones["Bone"].location
check("adopt: evaluation preserved", (loc - (type(loc)((4, 5, 6)))).length < 1e-4,
      tuple(loc))

# ------------------------------------------------- 4: rename (+solo name sync)
ob_id = ob
ob_id[core.AL_SOLO_PROP] = json.dumps({"track": "Layer 2", "restore": {}})
s = core.al_rename_layer(1, "Polish", sync_action=True)
check("rename: track", s["layers"][1]["name"] == "Polish")
check("rename: action synced", s["layers"][1]["action"] == "Polish")
check("rename: solo prop follows", s["solo"] == "Polish", s["solo"])
s = core.al_rename_layer(1, "Polish2", sync_action=False)
check("rename no-sync: action kept", s["layers"][1]["action"] == "Polish")
core.al_rename_layer(1, "Polish", sync_action=False)

# ------------------------------------------------- 5: duplicate linked / copy
s = core.al_duplicate_layer(0, linked=True)
check("dup linked: 3 layers", len(s["layers"]) == 3, names(s))
check("dup linked: sits above source", s["layers"][1]["name"].startswith("WalkCycle"),
      names(s))
check("dup linked: same action", s["layers"][1]["action"] == "WalkCycle")
check("dup linked: selected", s["active_index"] == 1)
# tweak mode itself holds a user on the tweaked action — measure untweaked
core._al_exit_tweak(ob.animation_data)
users_before = bpy.data.actions["WalkCycle"].users
s = core.al_duplicate_layer(0, linked=False)
check("dup copy: 4 layers", len(s["layers"]) == 4)
check("dup copy: new action", s["layers"][1]["action"] != "WalkCycle",
      s["layers"][1]["action"])
core._al_exit_tweak(ob.animation_data)
check("dup copy: source users unchanged",
      bpy.data.actions["WalkCycle"].users == users_before,
      "%d vs %d" % (bpy.data.actions["WalkCycle"].users, users_before))
# clean the two duplicates (top-most dup first)
core.al_delete_layer(1)
s = core.al_delete_layer(1)
check("deletes leave 2", names(s) == ["WalkCycle", "Polish"], names(s))

# ------------------------------------------------- 6: influence keys survive move
ad = ob.animation_data
top_strip = ad.nla_tracks[1].strips[0]
top_strip.use_animated_influence = True
top_strip.keyframe_insert("influence", frame=1)
top_strip.influence = 0.3
top_strip.keyframe_insert("influence", frame=30)
top_strip.repeat = 2.0
top_strip.mute = False

s = core.al_move_layer(1, 'DOWN')
check("move down: order swapped", names(s) == ["Polish", "WalkCycle"], names(s))
moved = ad.nla_tracks[0].strips[0]
check("move: influence keys kept",
      moved.use_animated_influence and
      len(moved.fcurves.find("influence").keyframe_points) == 2)
check("move: repeat kept", abs(moved.repeat - 2.0) < 1e-6, moved.repeat)
s = core.al_move_layer(0, 'UP')
check("move up: order restored", names(s) == ["WalkCycle", "Polish"], names(s))

# ------------------------------------------------- 7: move edges + locked rows
try:
    core.al_move_layer(1, 'UP')
    check("move top up refused", False)
except RuntimeError as exc:
    check("move top up refused", "top layer" in str(exc), str(exc))
try:
    core.al_move_layer(0, 'DOWN')
    check("move bottom down refused", False)
except RuntimeError as exc:
    check("move bottom down refused", "bottom layer" in str(exc), str(exc))

# a foreign 2-strip track between them
mid = ad.nla_tracks.new(prev=ad.nla_tracks[0])
mid.name = "Foreign"
fa = bpy.data.actions.new("F")
fa.slots.new('OBJECT', "Rig")
mid.strips.new("F1", 1, fa)
mid.strips.new("F2", 400, fa)
s = core.anim_layers_status()
check("foreign row locked", s["layers"][1]["locked_reason"], json.dumps(s["layers"][1]))
s = core.al_move_layer(2, 'DOWN')   # Polish over the locked row: rebuild self
check("move down past locked", names(s) == ["WalkCycle", "Polish", "Foreign"],
      names(s))
s = core.al_move_layer(1, 'UP')     # back up over it
check("move up past locked", names(s) == ["WalkCycle", "Foreign", "Polish"],
      names(s))
try:
    core.al_move_layer(1, 'DOWN')
    sx = core.anim_layers_status()
    check("swap with locked below ok", names(sx) == ["Foreign", "WalkCycle", "Polish"],
          names(sx))
except RuntimeError as exc:
    check("swap with locked below ok", False, str(exc))

# stack is now [Foreign, WalkCycle, Polish]: a locked track at the very
# bottom pins everything above it (nothing can be inserted below) — both
# escape hatches must refuse with a clear message
try:
    core.al_move_layer(1, 'DOWN')
    check("move below locked bottom refused", False)
except RuntimeError as exc:
    check("move below locked bottom refused", "locked" in str(exc), str(exc))
try:
    core.al_move_layer(0, 'UP')
    check("move locked layer refused", False)
except RuntimeError as exc:
    check("move locked layer refused", "locked" in str(exc), str(exc))
try:
    core.al_delete_layer(0)
    check("delete locked refused", False)
except RuntimeError as exc:
    check("delete locked refused", "locked" in str(exc), str(exc))
ad.nla_tracks.remove(ad.nla_tracks["Foreign"])
s = core.anim_layers_status()
check("cleanup: two layers left", names(s) == ["WalkCycle", "Polish"], names(s))

# ------------------------------------------------- 8: delete active falls back
core._al_activate(ad, 1, tweak=True)
s = core.al_delete_layer(1)
check("delete active: one left", names(s) == ["WalkCycle"], names(s))
check("delete active: fell back", s["active_index"] == 0, s["active_index"])
check("delete active: still tweaked", s["in_tweak"] is True)

# ------------------------------------------------- 9: custom name + blend
s = core.al_add_layer(name="Fix Foot", blend_type='ADD')
check("named add", s["layers"][1]["name"] == "Fix Foot")
check("ADD blend honoured", s["layers"][1]["blend_type"] == 'ADD')
try:
    core.al_add_layer(blend_type='NONSENSE')
    check("bad blend refused", False)
except RuntimeError:
    check("bad blend refused", True)

print("\n%d passed, %d failed" % (PASS, len(FAIL)))
if FAIL:
    print("FAILED:", FAIL)
    sys.exit(1)
print("ALL OK")
