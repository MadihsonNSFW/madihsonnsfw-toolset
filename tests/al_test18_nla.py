# Task 18 verification: adopting an existing NLA, clearing it, and every op
# refusing a locked / multi-strip row cleanly.
# Run: blender.exe -b --factory-startup --python al_test18_nla.py
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


def refuses(label, fn, *args, **kwargs):
    """The op must raise a RuntimeError that names the problem."""
    try:
        fn(*args, **kwargs)
    except RuntimeError as exc:
        text = str(exc).lower()
        ok("locked" in text or "no layer" in text,
           "%s refuses cleanly (%s)" % (label, exc))
        return
    ok(False, "%s should have refused" % label)


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
scene.frame_start, scene.frame_end = 1, 60


def hand_action(ob, name, frames):
    """An action built the ordinary way (keys at real frame numbers)."""
    act = bpy.data.actions.new(name)
    slot = act.slots.new(ob.id_type, ob.name)
    ad = ob.animation_data or ob.animation_data_create()
    prev, prev_slot = ad.action, ad.action_slot
    ad.action = act
    ad.action_slot = slot
    pb = ob.pose.bones["A"]
    for f, v in frames:
        scene.frame_set(f)
        pb.location = (v, 0.0, 0.0)
        pb.keyframe_insert("location")
    ad.action = prev
    if prev is not None:
        ad.action_slot = prev_slot
    return act, slot


# ================================================== a hand-built NLA stack
x = make_rig("X")
a1, s1 = hand_action(x, "Walk", [(1, 0.0), (11, 2.0)])
a2, s2 = hand_action(x, "Wave", [(1, 0.0), (21, 1.0)])
ad = x.animation_data

t1 = ad.nla_tracks.new()
t1.name = "Walk Track"
st1 = t1.strips.new("Walk", 5, a1)
st1.action_slot = s1
st1.frame_end_ui = 25.0          # deliberately SHORT of the scene end

t2 = ad.nla_tracks.new()
t2.name = "Two Strips"           # the classic un-layerable track
st2a = t2.strips.new("A", 1, a1)
st2a.action_slot = s1
st2b = t2.strips.new("B", 40, a2)
st2b.action_slot = s2

t3 = ad.nla_tracks.new()
t3.name = "Locked Track"
st3 = t3.strips.new("Wave", 5, a2)
st3.action_slot = s2
t3.lock = True

built = {t.name: (t.strips[0].frame_start, t.strips[0].frame_end)
         for t in ad.nla_tracks if len(t.strips) == 1}
st = core.anim_layers_status(object_name="X")
ok(st["foreign_nla"] is True, "status flags foreign NLA before adoption")
ok(st["managed"] is False, "an unadopted stack is not 'managed'")
ok(len(st["layers"]) == 3, "all three tracks are listed (%d)"
   % len(st["layers"]))
ok(st["layers"][1]["locked_reason"] == "2 strips (one per layer)",
   "the 2-strip track is a locked row (%s)" % st["layers"][1]["locked_reason"])
ok(st["layers"][2]["locked_reason"] == "track locked",
   "the locked track is a locked row (%s)" % st["layers"][2]["locked_reason"])

# ================================================== every op refuses row 1
refuses("duplicate", core.al_duplicate_layer, 1, object_name="X")
refuses("delete", core.al_delete_layer, 1, object_name="X")
refuses("move up", core.al_move_layer, 1, "UP", object_name="X")
refuses("blend change", core.al_set_layer_state, 1, blend_type="ADD",
        object_name="X")
refuses("influence", core.al_set_layer_state, 1, influence=0.5,
        object_name="X")
refuses("animated influence", core.al_set_influence_animated, 1, True,
        object_name="X")
refuses("frame range", core.al_set_frame_range, index=1, frame_start=2.0,
        object_name="X")
refuses("multikey", core.al_multikey, 'OFFSET', 1.0, index=1, object_name="X")
refuses("reset layer", core.al_reset_layer, index=1, object_name="X")
refuses("cyclic", core.al_cyclic_fcurves, index=1, object_name="X")
refuses("inbetween", core.al_inbetween, 0.5, index=1, object_name="X")
refuses("extract bones", core.al_extract_bones, index=1, object_name="X")
refuses("share keys", core.al_share_keys, 0, index=1, object_name="X")

# a TRACK-LOCKED row (row 2) keeps its strip, so it refuses on the lock
refuses("frame range on a locked track", core.al_set_frame_range, index=2,
        frame_start=2.0, object_name="X")
refuses("multikey on a locked track", core.al_multikey, 'OFFSET', 1.0,
        index=2, object_name="X")

# mute is allowed on ANY row — that's how you exclude one from view/bakes
core.al_set_layer_state(1, mute=True, object_name="X")
ok(ad.nla_tracks["Two Strips"].mute is True,
   "mute still works on a locked row (it is how you exclude it)")
core.al_set_layer_state(1, mute=False, object_name="X")

# selecting a locked row is allowed, but never enters tweak mode
st = core.al_select_layer(1, object_name="X")
ok(st["in_tweak"] is False, "selecting a locked row does not enter tweak mode")

# ---- ops on a FOREIGN stack must never reshape it (the user has not
# chosen "use as layers" yet)
ends_before = {t.name: (t.strips[0].frame_start, t.strips[0].frame_end)
               for t in ad.nla_tracks if len(t.strips) == 1}
ok(ends_before == built,
   "browsing/refusing on an unadopted stack left every span alone (%s vs %s)"
   % (ends_before, built))
ok(abs(ends_before["Walk Track"][1] - 25.0) < 1e-3,
   "the short strip is still short (%.1f)" % ends_before["Walk Track"][1])

# ================================================== adoption
st = core.al_adopt_nla(object_name="X")
ok(st["adopted"]["layers"] == ["Walk Track"],
   "only the healthy track is adopted (%s)" % st["adopted"]["layers"])
ok([r["name"] for r in st["adopted"]["locked"]]
   == ["Two Strips", "Locked Track"],
   "the unusable tracks are reported as locked (%s)" % st["adopted"]["locked"])
ok(st["foreign_nla"] is False and st["managed"] is True,
   "after adoption the stack is managed")

now = (ad.nla_tracks["Walk Track"].strips[0].frame_start,
       ad.nla_tracks["Walk Track"].strips[0].frame_end)
ok(now == ends_before["Walk Track"],
   "adoption keeps the strip's exact span (%s vs %s)"
   % (now, ends_before["Walk Track"]))
ok(st["layers"][0]["custom_range"] is True,
   "adopted layers are flagged custom-range (that is the exemption)")

# and the auto-extend still leaves them alone on the next op
core.al_select_layer(0, object_name="X")
core.al_set_layer_state(0, mute=False, object_name="X")
ok((ad.nla_tracks["Walk Track"].strips[0].frame_start,
    ad.nla_tracks["Walk Track"].strips[0].frame_end)
   == ends_before["Walk Track"],
   "later ops never restretch an adopted strip (%s)"
   % (ad.nla_tracks["Walk Track"].strips[0].frame_end,))

# a track-locked strip was never touched either
ok(abs(ad.nla_tracks["Locked Track"].strips[0].frame_end - 25.0) < 1e-3,
   "the locked track's strip is untouched (%.1f)"
   % ad.nla_tracks["Locked Track"].strips[0].frame_end)

# adding a layer on top of an adopted stack works normally
st = core.al_add_layer(object_name="X", name="New Layer")
ok([r["name"] for r in st["layers"]][-1] == "New Layer",
   "a new layer lands on top of the adopted stack (%s)"
   % [r["name"] for r in st["layers"]])
ok(st["layers"][-1]["custom_range"] is False,
   "the NEW layer is not custom-range — it follows the scene range")
ok(abs(ad.nla_tracks["New Layer"].strips[0].frame_end - 60.0) < 1e-3,
   "the new layer spans the scene range (%.1f)"
   % ad.nla_tracks["New Layer"].strips[0].frame_end)

# ================================================== clear
try:
    core.al_clear_nla(object_name="X")
    ok(False, "clearing without confirm should refuse")
except RuntimeError as exc:
    ok("confirm" in str(exc).lower(), "clear needs a confirmation (%s)" % exc)

# an active action must survive the clear (the next New Layer adopts it)
ad.use_tweak_mode = False        # ad.action is read-only while tweaking
ad.action = a1
ad.action_slot = s1
st = core.al_clear_nla(confirm=True, object_name="X")
ok(len(ad.nla_tracks) == 0, "clear removed every track (%d left)"
   % len(ad.nla_tracks))
ok(sorted(st["cleared"]["layers"]) == sorted(
       ["Walk Track", "Two Strips", "Locked Track", "New Layer"]),
   "clear reports what it removed (%s)" % st["cleared"]["layers"])
ok(ad.action == a1, "the active action survives the clear")
ok("Walk" in bpy.data.actions and "Wave" in bpy.data.actions,
   "the actions themselves stay in the file")
for prop in (core.AL_SOLO_PROP, core.AL_INFL_PROP, core.AL_RANGE_PROP,
             core.AL_MANAGED_PROP):
    ok(prop not in x.keys(), "clear dropped the %s id-prop" % prop)

st = core.anim_layers_status(object_name="X")
ok(st["foreign_nla"] is False and st["layers"] == [],
   "status is back to an empty stack")

# and the first New Layer adopts the active action as the base layer again
st = core.al_add_layer(object_name="X")
ok(st["adopted_base"] == "Walk",
   "the next New Layer adopts the active action as base (%s)"
   % st["adopted_base"])

# ================================================== guards on empty stacks
y = make_rig("Y")
try:
    core.al_adopt_nla(object_name="Y")
    ok(False, "adopting with no tracks should refuse")
except RuntimeError as exc:
    ok("no nla tracks" in str(exc).lower(),
       "adopt with nothing to adopt refused (%s)" % exc)
try:
    core.al_clear_nla(confirm=True, object_name="Y")
    ok(False, "clearing nothing should refuse")
except RuntimeError as exc:
    ok("no nla tracks" in str(exc).lower(),
       "clear with nothing to clear refused (%s)" % exc)

print("\n%d passed, %d failed" % (len(PASS), len(FAIL)), flush=True)
for f in FAIL:
    print("  FAILED: " + f, flush=True)
sys.exit(1 if FAIL else 0)
