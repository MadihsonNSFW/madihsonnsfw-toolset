# Task 6 verification: AL bake engine (down/all, NEW vs MERGE).
# Run: blender.exe -b --factory-startup --python al_bake_test6.py
import importlib.util
import os
import sys

import bpy
from mathutils import Quaternion

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
    bpy.context.view_layer.update()  # 5.2 fresh-object crash guard
    bpy.ops.object.mode_set(mode='EDIT')
    for bname, z in (("A", 0.0), ("B", 1.0)):
        eb = arm.edit_bones.new(bname)
        eb.head = (0.0, 0.0, z)
        eb.tail = (0.0, 0.0, z + 1.0)
    bpy.ops.object.mode_set(mode='OBJECT')
    return ob


CHANNELS = (
    [('pose.bones["A"].location', i) for i in range(3)]
    + [('pose.bones["A"].rotation_quaternion', i) for i in range(4)]
    + [('pose.bones["B"].location', i) for i in range(3)]
    + [('pose.bones["A"]["squish"]', 0)]
)


def sample(ob):
    ad = ob.animation_data
    if ad and ad.use_tweak_mode:
        ad.use_tweak_mode = False
    out = []
    for f in range(1, 31):
        bpy.context.scene.frame_set(f)
        row = []
        for path, idx in CHANNELS:
            try:
                v = ob.path_resolve(path)
            except ValueError:
                row.append(None)
                continue
            row.append(float(v[idx]) if hasattr(v, "__len__") else float(v))
        out.append(row)
    return out


def diff(a, b):
    worst = 0.0
    for ra, rb in zip(a, b):
        for va, vb in zip(ra, rb):
            if va is None or vb is None:
                return float("inf")
            worst = max(worst, abs(va - vb))
    return worst


def build_stack(ob):
    """Base REPLACE (A loc+quat+prop) / Layer 2 COMBINE (A quat, infl 0.5) /
    Layer 3 ADD (B loc)."""
    n = ob.name
    scene = bpy.context.scene
    scene.frame_start, scene.frame_end = 1, 30

    core.al_add_layer(object_name=n)          # Base Layer (REPLACE), tweaked
    pb = ob.pose.bones["A"]
    pb["squish"] = 0.0
    for f, loc, rot, sq in ((1, (0, 0, 0), 0.0, 0.0),
                            (15, (1, 2, 3), 0.8, 2.5),
                            (30, (0.5, -1, 2), -0.4, 1.0)):
        scene.frame_set(f)
        pb.location = loc
        pb.rotation_quaternion = Quaternion((1.0, 0.0, 0.0), rot)
        pb["squish"] = sq
        pb.keyframe_insert("location")
        pb.keyframe_insert("rotation_quaternion")
        pb.keyframe_insert('["squish"]')

    core.al_add_layer(object_name=n)          # Layer 2 (COMBINE), tweaked
    for f, rot in ((1, 0.3), (30, -0.9)):
        scene.frame_set(f)
        pb.rotation_quaternion = Quaternion((0.0, 0.0, 1.0), rot)
        pb.keyframe_insert("rotation_quaternion")
    core.al_set_layer_state(1, influence=0.5, object_name=n)

    core.al_add_layer(object_name=n)          # Layer 3 -> ADD
    core.al_set_layer_state(2, blend_type='ADD', object_name=n)
    core.al_select_layer(2, object_name=n)
    pbB = ob.pose.bones["B"]
    for f, loc in ((1, (0.1, 0.2, 0.3)), (30, (1.0, 0.0, -1.0))):
        scene.frame_set(f)
        pbB.location = loc
        pbB.keyframe_insert("location")
    ob.animation_data.use_tweak_mode = False


def layer_names(ob):
    return [t.name for t in ob.animation_data.nla_tracks]


# ---------------------------------------------------------------- Test A: NEW/ALL
rig1 = make_rig("Rig1")
build_stack(rig1)
ref1 = sample(rig1)
r = core.al_bake(mode='NEW', direction='ALL', object_name="Rig1")
ok(r["baked"]["result"] == "Baked Layer", "A: result layer named Baked Layer")
ok(len(r["layers"]) == 4, "A: stack has 4 rows after NEW bake")
ok(r["layers"][3]["name"] == "Baked Layer", "A: baked layer on top")
ok(r["layers"][3]["blend_type"] == 'REPLACE', "A: baked layer is REPLACE")
ok(r["active_index"] == 3, "A: baked layer selected")
ok(r["baked"]["merged"] == ["Base Layer", "Layer 2", "Layer 3"],
   "A: reports all three sources")
d = diff(ref1, sample(rig1))
ok(d < 1e-5, "A: full stack still evaluates identically (worst %.2e)" % d)
for t in list(rig1.animation_data.nla_tracks)[:3]:
    t.mute = True
d = diff(ref1, sample(rig1))
ok(d < 1e-5, "A: baked layer ALONE reproduces the stack (worst %.2e)" % d)
for t in list(rig1.animation_data.nla_tracks)[:3]:
    t.mute = False

# ---------------------------------------------------------------- Test B: MERGE/ALL
rig2 = make_rig("Rig2")
build_stack(rig2)
ref2 = sample(rig2)
n_actions = len(bpy.data.actions)
r = core.al_bake(mode='MERGE', direction='ALL', object_name="Rig2")
ok(len(r["layers"]) == 1, "B: merge leaves one layer")
ok(r["layers"][0]["name"] == "Base Layer", "B: merged layer takes bottom name")
ok(r["layers"][0]["blend_type"] == 'REPLACE', "B: merged layer is REPLACE")
d = diff(ref2, sample(rig2))
ok(d < 1e-5, "B: merged layer evaluates identically (worst %.2e)" % d)
ok(layer_names(rig2) == ["Base Layer"], "B: track list is just the merge result")

# ---------------------------------------------------------------- Test C: NEW/DOWN
rig3 = make_rig("Rig3")
build_stack(rig3)
ref3 = sample(rig3)
r = core.al_bake(mode='NEW', direction='DOWN', index=1, object_name="Rig3")
ok(layer_names(rig3) == ["Base Layer", "Layer 2", "Baked Layer", "Layer 3"],
   "C: DOWN result sits above layer 2, below layer 3 (got %s)" % layer_names(rig3))
ok(r["baked"]["merged"] == ["Base Layer", "Layer 2"], "C: only layers 0..1 baked")
d = diff(ref3, sample(rig3))
ok(d < 1e-5, "C: stack with DOWN-baked layer evaluates identically (worst %.2e)" % d)
tracks = rig3.animation_data.nla_tracks
tracks[0].mute = True
tracks[1].mute = True
d = diff(ref3, sample(rig3))
ok(d < 1e-5, "C: baked-lower + live ADD layer reproduces (worst %.2e)" % d)

# ---------------------------------------------------------------- Test D: mute excluded
rig4 = make_rig("Rig4")
build_stack(rig4)
core.al_set_layer_state(1, mute=True, object_name="Rig4")
ref4 = sample(rig4)
r = core.al_bake(mode='MERGE', direction='ALL', object_name="Rig4")
ok(layer_names(rig4) == ["Base Layer", "Layer 2"],
   "D: muted layer survives the merge (got %s)" % layer_names(rig4))
ok(r["layers"][1]["mute"] is True, "D: survivor still muted")
ok(r["baked"]["merged"] == ["Base Layer", "Layer 3"],
   "D: muted layer excluded from bake set")
d = diff(ref4, sample(rig4))
ok(d < 1e-5, "D: merge of unmuted layers evaluates identically (worst %.2e)" % d)

# ---------------------------------------------------------------- Test E: errors
def raises(fn, frag, label):
    try:
        fn()
    except RuntimeError as exc:
        ok(frag.lower() in str(exc).lower(),
           label + " (msg: %s)" % exc)
    else:
        ok(False, label + " — no error raised")


raises(lambda: core.al_bake(direction='UP', object_name="Rig4"),
       "additive layers", "E: UP from a REPLACE layer refused")
rig4.animation_data.nla_tracks[1].mute = False
rig4.animation_data.nla_tracks[1].lock = True
raises(lambda: core.al_bake(mode='MERGE', direction='ALL', object_name="Rig4"),
       "can't be merged", "E: MERGE refuses a locked layer")
rig5 = make_rig("Rig5")
raises(lambda: core.al_bake(object_name="Rig5"),
       "no animation layers", "E: bake without layers refused")
raises(lambda: core.al_bake(mode='X', object_name="Rig4"),
       "mode must be", "E: bad mode refused")

print("\n%d passed, %d failed" % (len(PASS), len(FAIL)), flush=True)
if FAIL:
    for f in FAIL:
        print("FAILED: " + f, flush=True)
    sys.exit(1)
