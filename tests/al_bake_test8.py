# Task 8 verification: upward bake (Replace boundary) + additive merge.
# Run: blender.exe -b --factory-startup --python al_bake_test8.py
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


def make_rig(name):
    arm = bpy.data.armatures.new(name)
    ob = bpy.data.objects.new(name, arm)
    bpy.context.collection.objects.link(ob)
    bpy.context.view_layer.objects.active = ob
    ob.select_set(True)
    bpy.context.view_layer.update()
    bpy.ops.object.mode_set(mode='EDIT')
    for bname, z in (("A", 0.0), ("B", 1.0)):
        eb = arm.edit_bones.new(bname)
        eb.head = (0.0, 0.0, z)
        eb.tail = (0.0, 0.0, z + 1.0)
    bpy.ops.object.mode_set(mode='OBJECT')
    return ob


scene = bpy.context.scene
scene.frame_start, scene.frame_end = 1, 30


def key_bone(ob, bone, frame, vec):
    bpy.context.scene.frame_set(frame)
    pb = ob.pose.bones[bone]
    pb.location = vec
    pb.keyframe_insert("location")


def add_layer(ob, name, blend, keys, bone="A"):
    n = ob.name
    core.al_add_layer(object_name=n, name=name)
    i = len(ob.animation_data.nla_tracks) - 1
    if blend != 'COMBINE':
        core.al_set_layer_state(i, blend_type=blend, object_name=n)
    core.al_select_layer(i, object_name=n)
    for f, vec in keys:
        key_bone(ob, bone, f, vec)
    return i


def build(ob, with_combine=False, small=False):
    n = ob.name
    core.al_add_layer(object_name=n)                       # Base Layer, REPLACE
    core.al_select_layer(0, object_name=n)
    for f, vec in ((1, (0, 0, 0)), (15, (1, 2, 3)), (30, (0.5, -1, 2))):
        key_bone(ob, "A", f, vec)
    add_layer(ob, "AddA", 'ADD', ((1, (0.2, 0, 0)), (30, (-0.4, 0.1, 0))))
    core.al_set_layer_state(1, influence=0.7, object_name=n)
    if small:
        add_layer(ob, "SubA", 'SUBTRACT',
                  ((10, (0.1, 0.3, 0)), (20, (0, 0.2, 0.5))))
        ob.animation_data.use_tweak_mode = False
        return
    if with_combine:
        add_layer(ob, "CombA", 'COMBINE', ((10, (0.1, 0, 0)),))
        ob.animation_data.use_tweak_mode = False
        return
    add_layer(ob, "AddB", 'ADD', ((10, (0.1, 0.3, 0)), (20, (0, 0.2, 0.5))))
    add_layer(ob, "Rep", 'REPLACE',
              ((1, (0.5, 0, 0)), (30, (0, 0.5, 0))), bone="B")
    add_layer(ob, "AddC", 'ADD', ((5, (0, 0, 0.2)), (25, (0.3, 0, 0))))
    ob.animation_data.use_tweak_mode = False


def sample(ob):
    ad = ob.animation_data
    if ad and ad.use_tweak_mode:
        ad.use_tweak_mode = False
    out = []
    for f in range(1, 31):
        bpy.context.scene.frame_set(f)
        row = []
        for bone in ("A", "B"):
            row.extend(float(v) for v in ob.pose.bones[bone].location)
        out.append(row)
    return out


def diff(a, b):
    return max(abs(x - y) for ra, rb in zip(a, b) for x, y in zip(ra, rb))


def names(ob):
    return [t.name for t in ob.animation_data.nla_tracks]


# ------------------------------------------------- T1: UP NEW stops at Replace
u1 = make_rig("U1")
build(u1)
ref1 = sample(u1)
r = core.al_bake(mode='NEW', direction='UP', index=1, object_name="U1")
ok(r["baked"]["merged"] == ["AddA", "AddB"],
   "T1: up-bake stops at the Replace boundary (got %s)" % r["baked"]["merged"])
ok(names(u1) == ["Base Layer", "AddA", "AddB", "Baked Layer", "Rep", "AddC"],
   "T1: result sits above AddB, below Rep (got %s)" % names(u1))
ok(r["baked"]["result_blend"] == 'ADD', "T1: result reported as ADD")
bstrip = u1.animation_data.nla_tracks[3].strips[0]
ok(bstrip.blend_type == 'ADD', "T1: result strip blend is ADD")
tracks = u1.animation_data.nla_tracks
tracks[1].mute = True
tracks[2].mute = True
d = diff(ref1, sample(u1))
ok(d < 1e-4, "T1: baked ADD layer replaces its sources (worst %.2e)" % d)

# ------------------------------------------------- T2: UP MERGE
u2 = make_rig("U2")
build(u2)
ref2 = sample(u2)
r = core.al_bake(mode='MERGE', direction='UP', index=1, object_name="U2")
ok(names(u2) == ["Base Layer", "AddA", "Rep", "AddC"],
   "T2: merge splices at the bottom source (got %s)" % names(u2))
ok(u2.animation_data.nla_tracks[1].strips[0].blend_type == 'ADD',
   "T2: merged layer stays ADD")
d = diff(ref2, sample(u2))
ok(d < 1e-4, "T2: merged additive stack evaluates identically (worst %.2e)" % d)

# ------------------------------------------------- T3: merged delta survives a base edit
u3 = make_rig("U3")
build(u3)                                   # control — same stack, unmerged


def nudge_base(ob):
    strip = ob.animation_data.nla_tracks[0].strips[0]
    for fc in core._al_action_fcurves_ro(strip.action):
        if fc.data_path.endswith("location") and fc.array_index == 0:
            sorted(fc.keyframe_points, key=lambda k: k.co.x)[1].co.y += 0.5
            fc.update()
            break


nudge_base(u2)
nudge_base(u3)
d = diff(sample(u2), sample(u3))
ok(d < 1e-4,
   "T3: merged rig tracks the control after a base-layer edit (worst %.2e)" % d)

# ------------------------------------------------- T4: COMBINE refuses upward
u4 = make_rig("U4")
build(u4, with_combine=True)
try:
    core.al_bake(mode='NEW', direction='UP', index=1, object_name="U4")
    ok(False, "T4: COMBINE above the ref should refuse — no error raised")
except RuntimeError as exc:
    ok("additive" in str(exc).lower(),
       "T4: COMBINE refused with guidance (msg: %s)" % exc)

# ------------------------------------------------- T5: ADD+SUBTRACT merge, no Replace above
u5 = make_rig("U5")
build(u5, small=True)
ref5 = sample(u5)
r = core.al_bake(mode='MERGE', direction='UP', index=1, object_name="U5")
ok(r["baked"]["merged"] == ["AddA", "SubA"],
   "T5: ADD+SUBTRACT both merge upward (got %s)" % r["baked"]["merged"])
ok(names(u5) == ["Base Layer", "AddA"],
   "T5: merged to one additive layer over the base (got %s)" % names(u5))
d = diff(ref5, sample(u5))
ok(d < 1e-4, "T5: subtract folded into the ADD delta (worst %.2e)" % d)

print("\n%d passed, %d failed" % (len(PASS), len(FAIL)), flush=True)
if FAIL:
    for f in FAIL:
        print("FAILED: " + f, flush=True)
    sys.exit(1)
