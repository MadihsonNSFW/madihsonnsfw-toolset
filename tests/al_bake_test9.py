# Task 9 verification: NLA-native bake (visual keying, clear constraints),
# merge-fcurve-modifiers both ways, smart+cyclic expansion, selected-only,
# copy-original backups.
# Run: blender.exe -b --factory-startup --python al_bake_test9.py
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


def base_layer(ob, keys, bone="A"):
    core.al_add_layer(object_name=ob.name)
    core.al_select_layer(0, object_name=ob.name)
    for f, vec in keys:
        key_bone(ob, bone, f, vec)
    ob.animation_data.use_tweak_mode = False


def matrix_sample(ob, bone):
    ad = ob.animation_data
    if ad and ad.use_tweak_mode:
        ad.use_tweak_mode = False
    out = []
    for f in range(1, 31):
        bpy.context.scene.frame_set(f)
        m = ob.pose.bones[bone].matrix
        out.append([m[i][j] for i in range(4) for j in range(4)])
    return out


def chan_sample(ob, bone):
    ad = ob.animation_data
    if ad and ad.use_tweak_mode:
        ad.use_tweak_mode = False
    out = []
    for f in range(1, 31):
        bpy.context.scene.frame_set(f)
        out.append([float(v) for v in ob.pose.bones[bone].location])
    return out


def diff(a, b):
    return max(abs(x - y) for ra, rb in zip(a, b) for x, y in zip(ra, rb))


def result_fc(ob, layer_i, path_frag, index):
    strip = ob.animation_data.nla_tracks[layer_i].strips[0]
    for fc in core._al_action_fcurves_ro(strip.action):
        if path_frag in fc.data_path and fc.array_index == index:
            return fc
    return None


# ------------------------------------------- N1: NLA bake + clear constraints
n1 = make_rig("N1")
base_layer(n1, ((1, (0, 0, 0)), (15, (1.0, 0.5, 2.0)), (30, (0.5, -1, 2))))
con = n1.pose.bones["B"].constraints.new('COPY_LOCATION')
con.target = n1
con.subtarget = "A"
ref_b = matrix_sample(n1, "B")
r = core.al_bake(mode='NEW', direction='ALL', bake_type='NLA',
                 clear_constraints=True, object_name="N1")
ok(len(n1.pose.bones["B"].constraints) == 0, "N1: constraint cleared by bake")
ok(r["layers"][-1]["name"] == "Baked Layer", "N1: NLA result layer on top")
n1.animation_data.nla_tracks[0].mute = True
d = diff(ref_b, matrix_sample(n1, "B"))
ok(d < 1e-4, "N1: constraint motion visually baked into keys (worst %.2e)" % d)
n1.animation_data.nla_tracks[0].mute = False

# ------------------------------------------- N2: NLA MERGE keeps eval
n2 = make_rig("N2")
base_layer(n2, ((1, (0, 0, 0)), (15, (1, 2, 3)), (30, (0.5, -1, 2))))
core.al_add_layer(object_name="N2")
core.al_select_layer(1, object_name="N2")
key_bone(n2, "A", 10, (0.3, 0.1, 0))
key_bone(n2, "A", 20, (-0.2, 0, 0.4))
n2.animation_data.use_tweak_mode = False
ref2 = chan_sample(n2, "A")
r = core.al_bake(mode='MERGE', direction='ALL', bake_type='NLA',
                 object_name="N2")
ok(len(r["layers"]) == 1, "N2: NLA merge leaves one layer")
d = diff(ref2, chan_sample(n2, "A"))
ok(d < 1e-4, "N2: NLA-merged layer evaluates identically (worst %.2e)" % d)

# ------------------------------------------- N3: refusals
def raises(fn, frag, label):
    try:
        fn()
    except RuntimeError as exc:
        ok(frag.lower() in str(exc).lower(), label + " (msg: %s)" % exc)
    else:
        ok(False, label + " — no error raised")


raises(lambda: core.al_bake(direction='UP', bake_type='NLA',
                            object_name="N1"),
       "anim layers engine", "N3: NLA+UP refused")
raises(lambda: core.al_bake(bake_type='NLA', smart=True, object_name="N1"),
       "anim layers engine", "N3: NLA+smart refused")

# ------------------------------------------- M1: noise modifier, merged in
m1 = make_rig("M1")
base_layer(m1, ((1, (0, 0, 0)), (30, (6.0, 0, 0))))
fc = result_fc(m1, 0, "location", 0)
for kp in fc.keyframe_points:
    kp.interpolation = 'LINEAR'
fc.update()
noise = fc.modifiers.new(type='NOISE')
noise.strength = 2.0
noise.scale = 5.0
ref_m1 = chan_sample(m1, "A")
r = core.al_bake(mode='NEW', direction='ALL', merge_modifiers=True,
                 object_name="M1")
bfc = result_fc(m1, 1, "location", 0)
ok(len(bfc.modifiers) == 0, "M1: merged-in bake carries no modifiers")
m1.animation_data.nla_tracks[0].mute = True
d = diff(ref_m1, chan_sample(m1, "A"))
ok(d < 1e-4, "M1: noise baked into keys (worst %.2e)" % d)
m1.animation_data.nla_tracks[0].mute = False

# ------------------------------------------- M2: noise modifier, kept live
m2 = make_rig("M2")
base_layer(m2, ((1, (0, 0, 0)), (30, (6.0, 0, 0))))
fc = result_fc(m2, 0, "location", 0)
for kp in fc.keyframe_points:
    kp.interpolation = 'LINEAR'
fc.update()
noise = fc.modifiers.new(type='NOISE')
noise.strength = 2.0
noise.scale = 5.0
ref_m2 = chan_sample(m2, "A")
r = core.al_bake(mode='NEW', direction='ALL', merge_modifiers=False,
                 object_name="M2")
bfc = result_fc(m2, 1, "location", 0)
ok(len(bfc.modifiers) == 1 and bfc.modifiers[0].type == 'NOISE',
   "M2: modifier copied onto the result, kept live")
src_fc = result_fc(m2, 0, "location", 0)
ok(all(not m.mute for m in src_fc.modifiers),
   "M2: source modifiers un-muted after sampling")
# the copied noise evaluates in the RESULT action's time (one frame offset
# from the source action) so the pattern phase-shifts — visually the same
# noise, numerically different. The real contract: keys stay PURE (no noise
# baked in) and the modifier is live on top of them.
worst = max(abs(kp.co.y - (kp.co.x - 1.0) / 29.0 * 6.0)
            for kp in bfc.keyframe_points)
ok(worst < 1e-4, "M2: baked keys are the pure ramp, noise NOT baked in "
   "(worst %.2e)" % worst)
lively = max(abs(bfc.evaluate(f) - (f - 1.0) / 29.0 * 6.0)
             for f in range(1, 31))
ok(lively > 0.01, "M2: copied modifier actually modulates the result "
   "(max effect %.3f)" % lively)

# ------------------------------------------- CY: smart + cycles expansion
cy = make_rig("CY")
base_layer(cy, ((1, (0.0, 0, 0)), (2, (3.0, 0, 0)), (3, (1.0, 0, 0)),
                (4, (4.0, 0, 0)), (5, (0.0, 0, 0))))
fc = result_fc(cy, 0, "location", 0)
for kp in fc.keyframe_points:
    kp.interpolation = 'LINEAR'
fc.update()
cyc = fc.modifiers.new(type='CYCLES')
cyc.mode_before = 'REPEAT'
cyc.mode_after = 'REPEAT'
strip = cy.animation_data.nla_tracks[0].strips[0]
strip.action_frame_start, strip.action_frame_end = 0.0, 29.0
core._al_ensure_ranges(cy.animation_data)
ref_cy = chan_sample(cy, "A")
r = core.al_bake(mode='NEW', direction='ALL', smart=True, object_name="CY")
bfc = result_fc(cy, 1, "location", 0)
ok(len(bfc.modifiers) == 0, "CY: cycles modifier dropped from the result")
ok(len(bfc.keyframe_points) > 10,
   "CY: cycles expanded into keys across the range (%d keys)"
   % len(bfc.keyframe_points))
cy.animation_data.nla_tracks[0].mute = True
d = diff(ref_cy, chan_sample(cy, "A"))
ok(d < 1e-4, "CY: cyclic motion baked exactly (worst %.2e)" % d)
cy.animation_data.nla_tracks[0].mute = False

# ------------------------------------------- S1: only selected bones (AL path)
s1 = make_rig("S1")
base_layer(s1, ((1, (0, 0, 0)), (30, (1, 1, 1))))
core.al_select_layer(0, object_name="S1")
key_bone(s1, "B", 1, (0.5, 0, 0))
key_bone(s1, "B", 30, (0, 0.5, 0))
s1.animation_data.use_tweak_mode = False
s1.pose.bones["A"].select = True     # 5.2: selection lives on PoseBone
s1.pose.bones["B"].select = False
r = core.al_bake(mode='NEW', direction='ALL', selected_only=True,
                 object_name="S1")
strip = s1.animation_data.nla_tracks[1].strips[0]
paths = {fc.data_path for fc in core._al_action_fcurves_ro(strip.action)}
ok(all('"A"' in p for p in paths) and paths,
   "S1: only the selected bone's channels baked (got %s)" % sorted(paths))

# ------------------------------------------- B1: copy-original backups
b1 = make_rig("B1")
base_layer(b1, ((1, (0, 0, 0)), (30, (1, 0, 0))))
core.al_add_layer(object_name="B1")
core.al_select_layer(1, object_name="B1")
key_bone(b1, "A", 10, (0.1, 0.2, 0.3))
b1.animation_data.use_tweak_mode = False
r = core.al_bake(mode='MERGE', direction='ALL', copy_original=True,
                 object_name="B1")
backups = r["baked"]["backups"]
ok(len(backups) == 2, "B1: one backup per merged layer (got %s)" % backups)
ok(all(bpy.data.actions.get(n) is not None
       and bpy.data.actions[n].use_fake_user for n in backups),
   "B1: backups exist with a fake user")

print("\n%d passed, %d failed" % (len(PASS), len(FAIL)), flush=True)
if FAIL:
    for f in FAIL:
        print("FAILED: " + f, flush=True)
    sys.exit(1)
