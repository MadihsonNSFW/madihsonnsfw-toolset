# Task 17 verification: per-layer custom frame range — round-trip of every
# strip property, the ensure_ranges exemption, sync math, flag maintenance,
# and a bake of a repeated+reversed layer.
# Run: blender.exe -b --factory-startup --python al_test17_range.py
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
scene.frame_start, scene.frame_end = 1, 60


def strip_of(ob, i):
    return ob.animation_data.nla_tracks[i].strips[0]


def key_bone(ob, bone, frame, loc):
    scene.frame_set(frame)
    pb = ob.pose.bones[bone]
    pb.location = loc
    pb.keyframe_insert("location")


def row(status, i):
    return status["layers"][i]


def win(strip):
    """Real action-window length. NOT the action's own length: keys inserted
    in tweak mode land at action frames offset from scene frames, and the
    scene-range auto-extend grows the window (BLENDER_NOTES 2026-08-01)."""
    return strip.action_frame_end - strip.action_frame_start


# =============================================================== setup
x = make_rig("X")
core.al_add_layer(object_name="X")          # Base Layer
core.al_select_layer(0, object_name="X")
key_bone(x, "A", 1, (0.0, 0.0, 0.0))
key_bone(x, "A", 11, (2.0, 0.0, 0.0))
x.animation_data.use_tweak_mode = False

# id_data is how ensure_ranges finds the owning ID (strips can't hold props)
ok(x.animation_data.id_data == x,
   "AnimData.id_data gives the owning ID (%s)" % x.animation_data.id_data.name)

s = strip_of(x, 0)
ok(abs(s.frame_end - 60.0) < 1e-4,
   "a managed layer starts spanning the scene range (end %.1f)" % s.frame_end)

# =============================================================== custom range
st = core.al_set_frame_range(index=0, custom=True, frame_start=10.0,
                            frame_end=30.0, object_name="X")
s = strip_of(x, 0)
ok(abs(s.frame_start - 10.0) < 1e-4 and abs(s.frame_end - 30.0) < 1e-4,
   "start/end round-trip (%.1f..%.1f)" % (s.frame_start, s.frame_end))
ok(row(st, 0)["custom_range"] is True, "status flags the layer as custom-range")
ok(st["frame_range"]["frame_end"] == s.frame_end,
   "the op reports the RESULTING values")

# ---- THE trap: the auto-repair must not extend it back to the scene end
core.al_select_layer(0, object_name="X")            # runs _al_ensure_ranges
core.al_set_layer_state(0, mute=False, object_name="X")
s = strip_of(x, 0)
ok(abs(s.frame_end - 30.0) < 1e-4,
   "ensure_ranges leaves a custom range alone (end %.1f)" % s.frame_end)

# ---- and repeat survives the same ops (this is what it would have rewritten)
core.al_set_frame_range(index=0, repeat=2.0, object_name="X")
before = strip_of(x, 0).repeat
core.al_select_layer(0, object_name="X")
core.al_solo(0, object_name="X")
core.al_solo(None, object_name="X")
s = strip_of(x, 0)
ok(abs(s.repeat - before) < 1e-4 and abs(s.repeat - 2.0) < 1e-4,
   "repeat survives select/solo/state ops (%.3f)" % s.repeat)
ok(abs(s.frame_end - (s.frame_start + win(s) * s.scale * s.repeat)) < 1e-3,
   "end == start + action window x speed x repeat (%.1f, window %.1f)"
   % (s.frame_end, win(s)))

# ---- speed
span_before = strip_of(x, 0).frame_end - strip_of(x, 0).frame_start
core.al_set_frame_range(index=0, scale=2.0, object_name="X")
s = strip_of(x, 0)
ok(abs(s.scale - 2.0) < 1e-4
   and abs((s.frame_end - s.frame_start) - span_before * 2.0) < 1e-3,
   "speed x2 doubles the span (%.1f..%.1f)" % (s.frame_start, s.frame_end))

# ---- start moves the WHOLE strip, keeping the length
length = s.frame_end - s.frame_start
core.al_set_frame_range(index=0, frame_start=25.0, object_name="X")
s = strip_of(x, 0)
ok(abs(s.frame_start - 25.0) < 1e-4
   and abs((s.frame_end - s.frame_start) - length) < 1e-3,
   "moving the start keeps the length (%.1f..%.1f)"
   % (s.frame_start, s.frame_end))

# ---- reverse + extrapolation
core.al_set_frame_range(index=0, reverse=True, extrapolation='HOLD_FORWARD',
                        object_name="X")
s = strip_of(x, 0)
ok(s.use_reverse and s.extrapolation == 'HOLD_FORWARD',
   "reverse + extrapolation round-trip")
st = core.anim_layers_status(object_name="X")
ok(row(st, 0)["reversed"] is True
   and row(st, 0)["extrapolation"] == 'HOLD_FORWARD',
   "status mirrors reverse/extrapolation")
core.al_set_frame_range(index=0, reverse=False, extrapolation='HOLD',
                        object_name="X")

# =============================================================== sync
core.al_set_frame_range(index=0, scale=1.0, repeat=1.0, frame_start=5.0,
                        object_name="X")
s = strip_of(x, 0)
s.frame_end_ui = 55.0                      # stretch the action window by hand
ok(s.action_frame_end > 11.5,
   "hand-stretching the end grew the action window (afe %.1f)"
   % s.action_frame_end)
act_range = tuple(strip_of(x, 0).action.frame_range)
st = core.al_set_frame_range(index=0, sync=True, object_name="X")
s = strip_of(x, 0)
ok(abs(s.action_frame_start - act_range[0]) < 1e-3
   and abs(s.action_frame_end - act_range[1]) < 1e-3,
   "sync resets the action window to the action's own range (%.1f..%.1f vs "
   "%.1f..%.1f)" % (s.action_frame_start, s.action_frame_end,
                    act_range[0], act_range[1]))
alen = act_range[1] - act_range[0]
ok(abs(s.frame_end - (5.0 + alen)) < 1e-3,
   "sync span = start + length x speed x repeat (%.1f..%.1f, len %.1f)"
   % (s.frame_start, s.frame_end, alen))

core.al_set_frame_range(index=0, scale=1.5, repeat=2.0, object_name="X")
core.al_set_frame_range(index=0, sync=True, object_name="X")
s = strip_of(x, 0)
ok(abs(s.frame_end - (5.0 + alen * 1.5 * 2.0)) < 1e-2,
   "sync honours speed x repeat (%.1f..%.1f)" % (s.frame_start, s.frame_end))

# ---- always sync: the action grows, the next op re-spans the strip
st = core.al_set_frame_range(index=0, always_sync=True, object_name="X")
ok(row(st, 0)["always_sync"] is True, "status reports always_sync")
act = strip_of(x, 0).action
fc = next(core._al_action_fcurves_ro(act))
fc.keyframe_points.insert(act_range[1] + 10.0, 5.0)   # action 10 longer
fc.update()
alen2 = act.frame_range[1] - act.frame_range[0]
core.al_select_layer(0, object_name="X")   # any op re-syncs
s = strip_of(x, 0)
ok(abs(s.frame_end - (5.0 + alen2 * 1.5 * 2.0)) < 1e-2,
   "always-sync re-spans after the action grew (%.1f..%.1f)"
   % (s.frame_start, s.frame_end))
core.al_set_frame_range(index=0, always_sync=False, object_name="X")
s = strip_of(x, 0)
end_now = s.frame_end
fc = next(core._al_action_fcurves_ro(strip_of(x, 0).action))
fc.keyframe_points.insert(act.frame_range[1] + 10.0, 7.0)
fc.update()
core.al_select_layer(0, object_name="X")
ok(abs(strip_of(x, 0).frame_end - end_now) < 1e-3,
   "always-sync off stops re-spanning (%.1f)" % strip_of(x, 0).frame_end)

# =============================================================== custom off
core.al_set_frame_range(index=0, custom=False, object_name="X")
s = strip_of(x, 0)
ok(abs(s.frame_start - 1.0) < 1e-3 and abs(s.frame_end - 60.0) < 1e-3
   and abs(s.repeat - 1.0) < 1e-4 and abs(s.scale - 1.0) < 1e-4,
   "turning the custom range OFF resets to the scene range "
   "(%.1f..%.1f rep %.2f speed %.2f)"
   % (s.frame_start, s.frame_end, s.repeat, s.scale))
st = core.anim_layers_status(object_name="X")
ok(row(st, 0)["custom_range"] is False, "status clears the custom flag")

# =============================================================== guards
try:
    core.al_set_frame_range(index=0, repeat=0.0, object_name="X")
    ok(False, "repeat 0 should refuse")
except RuntimeError as exc:
    ok("repeat" in str(exc).lower(), "repeat 0 refused (%s)" % exc)
try:
    core.al_set_frame_range(index=0, scale=0.0, object_name="X")
    ok(False, "speed 0 should refuse")
except RuntimeError as exc:
    ok("speed" in str(exc).lower(), "speed 0 refused (%s)" % exc)
try:
    core.al_set_frame_range(index=0, frame_start=30.0, frame_end=10.0,
                            object_name="X")
    ok(False, "end before start should refuse")
except RuntimeError as exc:
    ok("end frame" in str(exc).lower(), "end<start refused (%s)" % exc)
try:
    core.al_set_frame_range(index=0, extrapolation='NOPE', object_name="X")
    ok(False, "bad extrapolation should refuse")
except RuntimeError as exc:
    ok("extrapolation" in str(exc).lower(), "bad extrapolation refused (%s)"
       % exc)
x.animation_data.nla_tracks[0].lock = True
try:
    core.al_set_frame_range(index=0, frame_start=2.0, object_name="X")
    ok(False, "locked layer should refuse")
except RuntimeError as exc:
    ok("locked" in str(exc).lower(), "locked layer refused (%s)" % exc)
x.animation_data.nla_tracks[0].lock = False

# =============================================================== flags follow
core.al_set_frame_range(index=0, custom=True, frame_start=5.0,
                        frame_end=25.0, object_name="X")
core.al_rename_layer(0, "Renamed Base", object_name="X")
ok("Renamed Base" in core._al_range_flags(x),
   "the custom-range flag follows a rename (%s)" % core._al_range_flags(x))
core.al_select_layer(0, object_name="X")
ok(abs(strip_of(x, 0).frame_end - 25.0) < 1e-3,
   "the range still holds after the rename (%.1f)" % strip_of(x, 0).frame_end)

core.al_duplicate_layer(0, object_name="X")
names = [t.name for t in x.animation_data.nla_tracks]
ok(names[1] in core._al_range_flags(x),
   "a duplicate inherits the custom-range flag (%s)" % names)
core.al_select_layer(1, object_name="X")
ok(abs(strip_of(x, 1).frame_end - 25.0) < 1e-3,
   "the duplicate keeps its span (%.1f)" % strip_of(x, 1).frame_end)

core.al_delete_layer(1, object_name="X")
ok(names[1] not in core._al_range_flags(x),
   "deleting a layer prunes its flag (%s)" % core._al_range_flags(x))

# =============================================================== bake honours
y = make_rig("Y")
core.al_add_layer(object_name="Y")
core.al_select_layer(0, object_name="Y")
key_bone(y, "A", 1, (0.0, 0.0, 0.0))
key_bone(y, "A", 11, (2.0, 0.0, 0.0))
y.animation_data.use_tweak_mode = False
core.al_set_frame_range(index=0, custom=True, sync=True, frame_start=1.0,
                        object_name="Y")
core.al_set_frame_range(index=0, repeat=2.0, reverse=True, object_name="Y")
s = strip_of(y, 0)
ok(abs(s.repeat - 2.0) < 1e-4 and s.use_reverse,
   "Y: repeated + reversed layer built (%.1f..%.1f rep %.1f)"
   % (s.frame_start, s.frame_end, s.repeat))

scene.frame_end = 21


def sample(ob):
    ad = ob.animation_data
    if ad and ad.use_tweak_mode:
        ad.use_tweak_mode = False
    out = []
    for f in range(1, 22):
        scene.frame_set(f)
        out.append(round(ob.pose.bones["A"].location.x, 5))
    return out


before = sample(y)
core.al_bake(mode='NEW', direction='ALL', bake_type='AL', smart=False,
             object_name="Y")
after = sample(y)
# the baked layer is on top in REPLACE, so the result must match exactly
worst = max(abs(a - b) for a, b in zip(before, after))
ok(worst < 1e-4,
   "bake of a repeated+reversed layer matches evaluation (worst %.2e)" % worst)
ok(before[0] > before[5],
   "the reversed layer really does play backwards (%s...)" % before[:4])
ok(len(set(before)) > 3, "the sampled motion is not flat")

# the baked layer itself follows the scene range, not the source's custom one
baked = strip_of(y, 1)
ok(abs(baked.frame_end - 21.0) < 1e-3 and abs(baked.repeat - 1.0) < 1e-4,
   "the bake RESULT is a plain scene-range layer (%.1f..%.1f rep %.2f)"
   % (baked.frame_start, baked.frame_end, baked.repeat))

print("\n%d passed, %d failed" % (len(PASS), len(FAIL)), flush=True)
for f in FAIL:
    print("  FAILED: " + f, flush=True)
sys.exit(1 if FAIL else 0)
