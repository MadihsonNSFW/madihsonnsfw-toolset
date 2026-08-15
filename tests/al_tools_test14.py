# Task 14 verification: Extract Marked Keyframes (mocap cleanup).
# Run: blender.exe -b --factory-startup --python al_tools_test14.py
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


def make_rig(name, bones=("A", "B")):
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
MARKERS = (1, 8, 17, 30)
for f in MARKERS:
    scene.timeline_markers.new("M%d" % f, frame=f)


def set_sel(ob, names):
    for pb in ob.pose.bones:
        core.bone_set_selected(pb, pb.name in names)


def layer_curves(ob, i):
    strip = ob.animation_data.nla_tracks[i].strips[0]
    return {(fc.data_path, fc.array_index): fc
            for fc in core._al_action_fcurves_ro(strip.action)}


def names(ob):
    return [t.name for t in ob.animation_data.nla_tracks]


# a dense "mocap" layer: every frame keyed, wiggly
m = make_rig("M")
core.al_add_layer(object_name="M")
core.al_select_layer(0, object_name="M")
import math
for f in range(1, 31):
    scene.frame_set(f)
    pb = m.pose.bones["A"]
    pb.location = (math.sin(f * 0.7) * 2.0, f * 0.1, 0.0)
    pb.keyframe_insert("location")
pbB = m.pose.bones["B"]
scene.frame_set(1)
pbB.location = (1.0, 0, 0)
pbB.keyframe_insert("location")
m.animation_data.use_tweak_mode = False

# what the mocap layer alone shows at the marker frames
ref = {}
for f in MARKERS:
    scene.frame_set(f)
    ref[f] = [float(v) for v in m.pose.bones["A"].location]

set_sel(m, ["A"])
r = core.al_extract_markers(0, object_name="M")
ok(names(m) == ["Base Layer", "Base Layer Markers"],
   "M: marker layer sits above the source (got %s)" % names(m))
ok(r["markers"]["markers"] == 4, "M: reports 4 markers")
ok(r["markers"]["curves"] == 3, "M: A's 3 location curves extracted")
ok(r["markers"]["keys"] == 12, "M: 3 curves x 4 markers = 12 keys")

new_c = layer_curves(m, 1)
fc = new_c[('pose.bones["A"].location', 0)]
ok(len(fc.keyframe_points) == 4,
   "M: key count == marker count (got %d)" % len(fc.keyframe_points))
frames = sorted(round(kp.co.x, 2) for kp in fc.keyframe_points)
ok(frames == [0.0, 7.0, 16.0, 29.0],
   "M: keys land at the marker frames in action time (got %s)" % frames)
ok(all(kp.interpolation == 'BEZIER' for kp in fc.keyframe_points),
   "M: extracted keys are smooth BEZIER")

ok(m.animation_data.nla_tracks[0].mute is True, "M: source layer muted")
ok(len(layer_curves(m, 0)) == 6,
   "M: source layer still has all its curves (not destroyed)")

# the new layer alone reproduces the marked poses exactly
m.animation_data.use_tweak_mode = False
worst = 0.0
for f in MARKERS:
    scene.frame_set(f)
    got = [float(v) for v in m.pose.bones["A"].location]
    worst = max(worst, max(abs(a - b) for a, b in zip(got, ref[f])))
ok(worst < 1e-5,
   "M: marker-frame poses exact vs the mocap layer (worst %.2e)" % worst)

ok(all(core._bone_of_path(p) == "A" for p, _i in new_c),
   "M: unselected bone B was not extracted")

# mute_source=False leaves the source audible
m2 = make_rig("M2")
core.al_add_layer(object_name="M2")
core.al_select_layer(0, object_name="M2")
for f in range(1, 31):
    scene.frame_set(f)
    pb = m2.pose.bones["A"]
    pb.location = (f * 0.2, 0, 0)
    pb.keyframe_insert("location")
m2.animation_data.use_tweak_mode = False
set_sel(m2, ["A"])
core.al_extract_markers(0, name="Keys", mute_source=False, object_name="M2")
ok(m2.animation_data.nla_tracks[0].mute is False,
   "M: mute_source=False leaves the source playing")
ok(names(m2) == ["Base Layer", "Keys"], "M: custom name honoured")

# filter scoping
m3 = make_rig("M3")
core.al_add_layer(object_name="M3")
core.al_select_layer(0, object_name="M3")
for f in (1, 15, 30):
    scene.frame_set(f)
    pb = m3.pose.bones["A"]
    pb.location = (f * 0.1, f * 0.2, f * 0.3)
    pb.keyframe_insert("location")
    pb.rotation_quaternion = (1, 0, 0, f * 0.01)
    pb.keyframe_insert("rotation_quaternion")
m3.animation_data.use_tweak_mode = False
set_sel(m3, ["A"])
r = core.al_extract_markers(0, channels=['LOCATION'], axes=['X'],
                            object_name="M3")
ok(r["markers"]["curves"] == 1,
   "M: location+X filter extracts one curve (got %d)" % r["markers"]["curves"])


def raises(fn, frag, label):
    try:
        fn()
    except RuntimeError as exc:
        ok(frag.lower() in str(exc).lower(), label + " (msg: %s)" % exc)
    else:
        ok(False, label + " — no error raised")


set_sel(m3, [])
raises(lambda: core.al_extract_markers(0, object_name="M3"),
       "no bones selected", "E: nothing selected refuses")
set_sel(m3, ["A"])
for mk in list(scene.timeline_markers):
    scene.timeline_markers.remove(mk)
raises(lambda: core.al_extract_markers(0, object_name="M3"),
       "no timeline markers", "E: no markers refuses with guidance")

print("\n%d passed, %d failed" % (len(PASS), len(FAIL)), flush=True)
if FAIL:
    for f in FAIL:
        print("FAILED: " + f, flush=True)
    sys.exit(1)
