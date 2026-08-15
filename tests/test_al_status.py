# Headless checks for core.py anim-layers Task 1 (status + listing).
# Run: blender.exe -b --factory-startup --python test_al_status.py
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
        print("FAIL %s  %s" % (name, extra))


def clean():
    bpy.ops.wm.read_factory_settings(use_empty=True)


def make_armature(name="Rig"):
    arm = bpy.data.armatures.new(name)
    ob = bpy.data.objects.new(name, arm)
    bpy.context.collection.objects.link(ob)
    bpy.context.view_layer.objects.active = ob
    ob.select_set(True)
    # one bone so keyframes have somewhere to live
    bpy.ops.object.mode_set(mode='EDIT')
    eb = arm.edit_bones.new("Bone")
    eb.head = (0, 0, 0)
    eb.tail = (0, 0, 1)
    bpy.ops.object.mode_set(mode='OBJECT')
    return ob


def keyed_action(ob, name, frame=1, loc=(0, 0, 0)):
    """A real slotted action: made the way Blender makes them (via keying)."""
    ad = ob.animation_data_create()
    old = ad.action
    action = bpy.data.actions.new(name)
    ad.action = action
    if ad.action_slot is None and action.slots:
        ad.action_slot = action.slots[0]
    pb = ob.pose.bones["Bone"]
    pb.location = loc
    pb.keyframe_insert("location", frame=frame)
    ad.action = old
    return action


# ---------------------------------------------------------------- 1: no object
clean()
for o in list(bpy.data.objects):
    bpy.data.objects.remove(o)
s = core.anim_layers_status()
check("no-object -> error", s.get("error"))

# ------------------------------------------------- 2: armature, no animdata
clean()
ob = make_armature()
s = core.anim_layers_status()
check("bare rig: no error", s.get("error") is None, s.get("error"))
check("bare rig: no animdata", s.get("has_animdata") is False)
check("bare rig: empty layers", s.get("layers") == [])
check("bare rig: managed", s.get("managed") is True)

# ------------------------------------------------- 3: action, no tracks yet
act = keyed_action(ob, "Base", frame=1, loc=(1, 0, 0))
ob.animation_data.action = act
s = core.anim_layers_status()
check("action visible", s.get("active_action") == "Base", s.get("active_action"))
check("still no layers", s.get("layers") == [])

# ------------------------------------------------- 4: hand-built 3-row stack
ad = ob.animation_data
ad.action = None
a0 = act
a1 = keyed_action(ob, "Tweaks", frame=1, loc=(0, 2, 0))
a2 = keyed_action(ob, "Extra", frame=1, loc=(0, 0, 3))

t0 = ad.nla_tracks.new()
t0.name = "Base"
s0 = t0.strips.new("Base", 1, a0)
s0.blend_type = 'REPLACE'

t1 = ad.nla_tracks.new()
t1.name = "Tweaks"
s1 = t1.strips.new("Tweaks", 1, a1)
s1.blend_type = 'COMBINE'
s1.use_animated_influence = True
s1.influence = 0.5

t2 = ad.nla_tracks.new()
t2.name = "Broken"
s2a = t2.strips.new("A", 1, a2)
s2b = t2.strips.new("B", 200, a2)

s = core.anim_layers_status()
lay = s["layers"]
check("stack: 3 rows", len(lay) == 3, json.dumps(s))
check("stack order bottom-first", [r["name"] for r in lay] == ["Base", "Tweaks", "Broken"])
check("base is REPLACE", lay[0]["blend_type"] == 'REPLACE')
check("mid is COMBINE", lay[1]["blend_type"] == 'COMBINE')
check("mid influence 0.5", abs(lay[1]["influence"] - 0.5) < 1e-6, lay[1]["influence"])
# Since 0.3.7 `animated_influence` is USER INTENT (our id-prop), never
# `use_animated_influence` — which is True for static influence too. A
# hand-built strip with no intent flag and <=1 influence key reads static.
check("mid animated_influence", lay[1]["animated_influence"] is False)
check("multi-strip locked", "strips" in (lay[2]["locked_reason"] or ""), lay[2]["locked_reason"])
check("locked row has no blend field", "blend_type" not in lay[2])
check("stack not managed (foreign tracks)", s["managed"] is False)

# slot auto-assignment probe (recorded for task 3)
check("strip got an action_slot", s0.action_slot is not None,
      "slots=%d" % len(a0.slots))

# ------------------------------------------------- 5: lock / mute / solo prop
t1.lock = True
t0.mute = True
ob[core.AL_MANAGED_PROP] = True
ob[core.AL_SOLO_PROP] = json.dumps({"track": "Tweaks", "restore": {}})
s = core.anim_layers_status()
lay = s["layers"]
check("track lock -> locked_reason", lay[1]["locked_reason"] == "track locked")
check("mute reflected", lay[0]["mute"] is True)
check("solo flag on row", lay[1]["solo"] is True and lay[0]["solo"] is False)
check("solo name at top level", s["solo"] == "Tweaks")
check("managed id-prop respected", s["managed"] is True)
t1.lock = False
t0.mute = False

# ------------------------------------------------- 6: selection -> active_index
for tr in ad.nla_tracks:
    for st in tr.strips:
        st.select = False
s1.select = True
s = core.anim_layers_status()
check("selected strip -> active_index", s["active_index"] == 1, s["active_index"])

# ------------------------------------------------- 7: strip prop mirror
s1.use_animated_influence = True
s1.repeat = 2.0
s1.scale = 0.5
s1.use_reverse = True
s1.extrapolation = 'NOTHING'
s = core.anim_layers_status()
r = s["layers"][1]
check("repeat", abs(r["repeat"] - 2.0) < 1e-6)
check("scale", abs(r["scale"] - 0.5) < 1e-6)
check("reversed", r["reversed"] is True)
check("extrapolation", r["extrapolation"] == 'NOTHING')

# ------------------------------------------------- 8: tweak-mode probe (task 3 signal)
s1.use_reverse = False
s1.repeat = 1.0
s1.scale = 1.0
# verified recipe: selection AND collection-active must point at the layer
for tr in ad.nla_tracks:
    tr.select = False
    for st in tr.strips:
        st.select = False
t1.select = True
s1.select = True
ad.nla_tracks.active = t1
ad.use_tweak_mode = True
s = core.anim_layers_status()
check("tweak: in_tweak", s["in_tweak"] is True)
check("tweak: active_index", s["active_index"] == 1, s["active_index"])
check("tweak: layer action is active", s["active_action"] == "Tweaks",
      s["active_action"])
ad.use_tweak_mode = False
s = core.anim_layers_status()
check("tweak off again", s["in_tweak"] is False)

# ------------------------------------------------- 9: shapekey data type
clean()
bpy.ops.mesh.primitive_cube_add()
cube = bpy.context.active_object
s = core.anim_layers_status(data_type='SHAPEKEY')
check("no shapekeys -> error", s.get("error"), s.get("error"))
cube.shape_key_add(name="Basis")
sk = cube.shape_key_add(name="Smile")
key = cube.data.shape_keys
kad = key.animation_data_create()
ka = bpy.data.actions.new("KeyAct")
key.animation_data.action = ka
sk.value = 0.7
key.keyframe_insert('key_blocks["Smile"].value', frame=1)
key.animation_data.action = None
kt = kad.nla_tracks.new()
kt.name = "SK Base"
kt.strips.new("SK Base", 1, ka)
s = core.anim_layers_status(data_type='SHAPEKEY')
check("shapekey stack: no error", s.get("error") is None, s.get("error"))
check("shapekey stack: 1 layer", len(s["layers"]) == 1)
check("shapekey layer action", s["layers"][0].get("action") == "KeyAct")
check("object stack untouched by SK query",
      core.anim_layers_status(data_type='OBJECT')["layers"] == [])

# ---------------------------------------------------------------- summary
print("\n%d passed, %d failed" % (PASS, len(FAIL)))
if FAIL:
    print("FAILED:", FAIL)
    sys.exit(1)
print("ALL OK")
