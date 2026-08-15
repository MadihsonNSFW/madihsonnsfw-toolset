# Task 21 verification: shape-key layers (data_type='SHAPEKEY') — the whole
# stack on the shape-key AnimData: CRUD, tweak keying, blend/influence, bake,
# reset, multikey, extract, frame range, and independence from the object's
# own stack.
# Run: blender.exe -b --factory-startup --python al_test21_shapekeys.py
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


scene = bpy.context.scene
scene.frame_start, scene.frame_end = 1, 40


def make_mesh(name, keys=("Smile", "Frown", "Blink")):
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata([(0, 0, 0), (1, 0, 0), (0, 1, 0)], [], [(0, 1, 2)])
    mesh.update()
    ob = bpy.data.objects.new(name, mesh)
    bpy.context.collection.objects.link(ob)
    bpy.context.view_layer.objects.active = ob
    ob.select_set(True)
    # 5.2: a never-evaluated object CRASHES on an action assign — sync first
    bpy.context.view_layer.update()
    ob.shape_key_add(name="Basis", from_mix=False)
    for i, k in enumerate(keys):
        kb = ob.shape_key_add(name=k, from_mix=False)
        kb.data[0].co.z += float(i) + 1.0     # give each key real geometry
    return ob


def sk(ob):
    return ob.data.shape_keys


def value(ob, name):
    return round(sk(ob).key_blocks[name].value, 6)


def key_shape(ob, name, frame, val):
    scene.frame_set(frame)
    kb = sk(ob).key_blocks[name]
    kb.value = val
    kb.keyframe_insert("value")


def layer_curves(ob, i):
    strip = sk(ob).animation_data.nla_tracks[i].strips[0]
    return {(fc.data_path, fc.array_index): fc
            for fc in core._al_action_fcurves_ro(strip.action)}


def sample(ob, names, frames=range(1, 41)):
    ad = sk(ob).animation_data
    was = ad.use_tweak_mode
    if was:
        ad.use_tweak_mode = False
    out = []
    for f in frames:
        scene.frame_set(f)
        out.append([round(sk(ob).key_blocks[n].value, 6) for n in names])
    return out


def tracks(ob):
    return [t.name for t in sk(ob).animation_data.nla_tracks]


SMILE = ('key_blocks["Smile"].value', 0)
FROWN = ('key_blocks["Frown"].value', 0)

# ==================================================== status before anything
m = make_mesh("Head")
st = core.anim_layers_status('SHAPEKEY', object_name="Head")
ok(st["error"] is None and st["has_shapekeys"] is True,
   "status reads a shape-key target with no stack yet (%s)" % st.get("error"))
ok(st["layers"] == [] and st["data_type"] == "SHAPEKEY",
   "empty shape-key stack reports cleanly")

plain = bpy.data.objects.new("NoKeys", bpy.data.meshes.new("NoKeys"))
bpy.context.collection.objects.link(plain)
st = core.anim_layers_status('SHAPEKEY', object_name="NoKeys")
ok(st.get("error") and "shape keys" in st["error"],
   "a mesh with no shape keys errors as a FIELD, not a raise (%s)"
   % st.get("error"))

# ==================================================== first-run adoption
# give the shape keys an existing action, the way an animator would have one
ad = sk(m).animation_data_create()
act = bpy.data.actions.new("Head Shapes")
slot = act.slots.new(sk(m).id_type, sk(m).name)
ad.action = act
ad.action_slot = slot
key_shape(m, "Smile", 1, 0.0)
key_shape(m, "Smile", 20, 1.0)

st = core.al_add_layer('SHAPEKEY', object_name="Head")
ok(st["adopted_base"] == "Head Shapes",
   "the existing shape-key action becomes the base layer (%s)"
   % st["adopted_base"])
ok(tracks(m) == ["Head Shapes", "Layer 2"],
   "and a fresh layer lands on top (%s)" % tracks(m))
# the adopted action is now a STRIP's; ad.action is whatever layer tweak
# mode is editing (Blender points it at the tweaked strip's action)
adopted_gone = sk(m).animation_data.action is not act
tweaked = sk(m).animation_data.action
ok(adopted_gone and tracks(m)[0] == "Head Shapes",
   "the adopted action moved into the stack, not left active (active is %s)"
   % (tweaked.name if tweaked else None))
ok(tweaked is None or tweaked.name == "Layer 2",
   "the active action is just the layer being tweaked (%s)"
   % (tweaked.name if tweaked else None))
ok(st["layers"][1]["blend_type"] == "COMBINE",
   "the new layer defaults to Combine (%s)" % st["layers"][1]["blend_type"])

# ==================================================== keys land in the layer
core.al_select_layer(1, 'SHAPEKEY', object_name="Head")
ok(sk(m).animation_data.use_tweak_mode is True,
   "selecting a shape-key layer enters tweak mode")
key_shape(m, "Frown", 5, 0.0)
key_shape(m, "Frown", 30, 0.8)
base = layer_curves(m, 0)
top = layer_curves(m, 1)
ok(SMILE in base and FROWN not in base,
   "the base layer still holds only Smile (%s)" % list(base))
ok(FROWN in top and SMILE not in top,
   "the new keys landed in LAYER 2 (%s)" % list(top))

# ==================================================== blend / influence / solo
st = core.al_set_layer_state(1, blend_type="ADD", data_type='SHAPEKEY',
                            object_name="Head")
ok(st["layers"][1]["blend_type"] == "ADD", "blend type round-trips")
st = core.al_set_layer_state(1, influence=0.5, data_type='SHAPEKEY',
                            object_name="Head")
ok(abs(st["layers"][1]["influence"] - 0.5) < 1e-4,
   "influence round-trips (%.3f)" % st["layers"][1]["influence"])
scene.frame_set(30)
half = value(m, "Frown")
core.al_set_layer_state(1, influence=1.0, data_type='SHAPEKEY',
                        object_name="Head")
scene.frame_set(30)
full = value(m, "Frown")
ok(half < full - 1e-4,
   "influence really scales the shape-key layer (%.3f vs %.3f)" % (half, full))

st = core.al_solo(0, 'SHAPEKEY', object_name="Head")
ok(st["solo"] == "Head Shapes" and st["layers"][1]["mute"] is True,
   "solo mutes the other shape-key layers (%s)" % st["solo"])
st = core.al_solo(None, 'SHAPEKEY', object_name="Head")
ok(st["solo"] is None and st["layers"][1]["mute"] is False,
   "un-solo restores them")

# ==================================================== independence
core.al_add_layer('OBJECT', object_name="Head")     # object stack on the mesh
ok(len(m.animation_data.nla_tracks) == 1 and len(tracks(m)) == 2,
   "the OBJECT stack is separate from the SHAPEKEY stack (%d vs %d)"
   % (len(m.animation_data.nla_tracks), len(tracks(m))))
st = core.anim_layers_status('SHAPEKEY', object_name="Head")
ok(len(st["layers"]) == 2, "shape-key status still shows its own 2 layers")
st = core.anim_layers_status('OBJECT', object_name="Head")
ok(len(st["layers"]) == 1, "object status shows its own 1 layer")

# ==================================================== bake
before = sample(m, ["Smile", "Frown"])
st = core.al_bake(mode='NEW', direction='ALL', bake_type='AL', smart=False,
                  data_type='SHAPEKEY', object_name="Head")
after = sample(m, ["Smile", "Frown"])
worst = max(abs(a - b) for ra, rb in zip(before, after) for a, b in zip(ra, rb))
ok(worst < 1e-4,
   "a shape-key stack bakes to the same evaluated values (worst %.2e)" % worst)
baked = layer_curves(m, 2)
ok(SMILE in baked and FROWN in baked,
   "the baked layer holds both shape-key channels (%s)" % list(baked))
ok(st["baked"]["result_blend"] == "REPLACE",
   "the bake result is a Replace layer")

# smart bake keeps the original key count
core.al_delete_layer(2, 'SHAPEKEY', object_name="Head")
st = core.al_bake(mode='NEW', direction='ALL', bake_type='AL', smart=True,
                  data_type='SHAPEKEY', object_name="Head")
after = sample(m, ["Smile", "Frown"])
worst = max(abs(a - b) for ra, rb in zip(before, after) for a, b in zip(ra, rb))
ok(worst < 1e-4, "smart bake matches too (worst %.2e)" % worst)
ok(len(layer_curves(m, 2)[SMILE].keyframe_points) < 40,
   "smart bake did NOT key every frame (%d keys)"
   % len(layer_curves(m, 2)[SMILE].keyframe_points))

# the NLA-native path can't key shape-key channels — it must say so
try:
    core.al_bake(mode='NEW', bake_type='NLA', data_type='SHAPEKEY',
                 object_name="Head")
    ok(False, "the NLA bake path should refuse shape keys")
except RuntimeError as exc:
    ok("shape-key" in str(exc).lower() or "shape key" in str(exc).lower(),
       "NLA bake refuses shape keys with guidance (%s)" % exc)
core.al_delete_layer(2, 'SHAPEKEY', object_name="Head")

# ==================================================== reset
core.al_select_layer(1, 'SHAPEKEY', object_name="Head")
m.active_shape_key_index = sk(m).key_blocks.find("Frown")
scene.frame_set(30)
st = core.al_reset_layer(index=1, selected_only=True, data_type='SHAPEKEY',
                         object_name="Head")
info = st["reset"]
ok(info["channels"] == 1,
   "reset with 'only the active shape key' touches ONE channel (%d)"
   % info["channels"])
fc = layer_curves(m, 1)[FROWN]
at30 = [kp for kp in fc.keyframe_points if abs(kp.co.x - 30.0) < 1.5]
ok(at30 and abs(at30[0].co.y) < 1e-6,
   "it keyed the active shape key back to 0 (%s)"
   % [(round(k.co.x, 1), round(k.co.y, 3)) for k in fc.keyframe_points])
ok(SMILE not in layer_curves(m, 1),
   "the other shape keys were left alone")

# reset must never key the mesh OBJECT's transforms into a shape-key layer
ok(not any(p.startswith("location") or p.startswith("rotation")
           or p.startswith("scale") for p, _i in layer_curves(m, 1)),
   "no object transform channels leaked into the shape-key layer (%s)"
   % list(layer_curves(m, 1)))

m.active_shape_key_index = 0            # Basis
try:
    core.al_reset_layer(index=1, selected_only=True, data_type='SHAPEKEY',
                        object_name="Head")
    ok(False, "resetting with only the Basis active should refuse")
except RuntimeError as exc:
    ok("basis" in str(exc).lower(), "Basis-only reset refused (%s)" % exc)

# ==================================================== multikey
m.active_shape_key_index = sk(m).key_blocks.find("Frown")
fc = layer_curves(m, 1)[FROWN]
for kp in fc.keyframe_points:
    kp.select_control_point = True
vals = [round(kp.co.y, 6) for kp in fc.keyframe_points]
core.al_multikey('OFFSET', 0.1, index=1, selected_only=True,
                 data_type='SHAPEKEY', object_name="Head")
ok([round(kp.co.y, 6) for kp in layer_curves(m, 1)[FROWN].keyframe_points]
   == [round(v + 0.1, 6) for v in vals],
   "multikey offsets shape-key values (%s)"
   % [round(kp.co.y, 3) for kp in layer_curves(m, 1)[FROWN].keyframe_points])

# a TRANSFORM filter has nothing to say about shape keys — nothing in scope
try:
    core.al_multikey('OFFSET', 0.1, index=1, channels=['LOCATION'],
                     data_type='SHAPEKEY', object_name="Head")
    ok(False, "a Loc/Rot/Scale filter should leave no shape-key curves")
except RuntimeError as exc:
    ok("scope" in str(exc).lower(),
       "a transform filter scopes shape keys out (%s)" % exc)

# the active-key scope really excludes the others
core.al_select_layer(0, 'SHAPEKEY', object_name="Head")
m.active_shape_key_index = sk(m).key_blocks.find("Frown")
try:
    core.al_multikey('OFFSET', 1.0, index=0, selected_only=True,
                     data_type='SHAPEKEY', object_name="Head")
    ok(False, "Frown active, but layer 0 only animates Smile — no scope")
except RuntimeError as exc:
    ok("scope" in str(exc).lower(),
       "the active shape key scopes a layer that doesn't animate it out (%s)"
       % exc)
m.active_shape_key_index = sk(m).key_blocks.find("Smile")
smile_before = [round(kp.co.y, 6)
                for kp in layer_curves(m, 0)[SMILE].keyframe_points]
for kp in layer_curves(m, 0)[SMILE].keyframe_points:
    kp.select_control_point = True
core.al_multikey('SCALE', 0.5, index=0, pivot='ZERO', selected_only=True,
                 data_type='SHAPEKEY', object_name="Head")
ok([round(kp.co.y, 6) for kp in layer_curves(m, 0)[SMILE].keyframe_points]
   == [round(v * 0.5, 6) for v in smile_before],
   "the active shape key IS edited (%s)"
   % [round(kp.co.y, 3) for kp in layer_curves(m, 0)[SMILE].keyframe_points])

# ==================================================== extract + frame range
core.al_select_layer(1, 'SHAPEKEY', object_name="Head")
ref = sample(m, ["Smile", "Frown"])
st = core.al_extract_bones(index=1, selected_only=False,
                           data_type='SHAPEKEY', object_name="Head")
ok(st["extracted"]["curves"] >= 1,
   "extract moves the shape-key curves into a new layer (%s)"
   % st["extracted"])
after = sample(m, ["Smile", "Frown"])
worst = max(abs(a - b) for ra, rb in zip(ref, after) for a, b in zip(ra, rb))
ok(worst < 1e-4, "the animation is unchanged by the extract (worst %.2e)"
   % worst)

st = core.al_set_frame_range(index=0, custom=True, frame_start=5.0,
                             frame_end=25.0, data_type='SHAPEKEY',
                             object_name="Head")
strip = sk(m).animation_data.nla_tracks[0].strips[0]
ok(abs(strip.frame_start - 5.0) < 1e-3 and abs(strip.frame_end - 25.0) < 1e-3,
   "a custom frame range works on a shape-key layer (%.1f..%.1f)"
   % (strip.frame_start, strip.frame_end))
core.al_select_layer(0, 'SHAPEKEY', object_name="Head")
ok(abs(sk(m).animation_data.nla_tracks[0].strips[0].frame_end - 25.0) < 1e-3,
   "and the auto-repair exempts it here too")

# ==================================================== select-bones refuses
try:
    core.al_select_bones_in_layer(index=0, data_type='SHAPEKEY',
                                  object_name="Head")
    ok(False, "select-bones should refuse on a mesh")
except RuntimeError as exc:
    ok("armature" in str(exc).lower(),
       "select-bones refuses on a shape-key target (%s)" % exc)

# ==================================================== CRUD round-trip
st = core.al_rename_layer(0, "Base Shapes", data_type='SHAPEKEY',
                          object_name="Head")
ok(tracks(m)[0] == "Base Shapes", "rename works (%s)" % tracks(m))
st = core.al_duplicate_layer(0, data_type='SHAPEKEY', object_name="Head")
ok(len(tracks(m)) == 4, "duplicate works (%s)" % tracks(m))
st = core.al_move_layer(1, "UP", data_type='SHAPEKEY', object_name="Head")
ok(len(tracks(m)) == 4, "move works (%s)" % tracks(m))
st = core.al_delete_layer(1, data_type='SHAPEKEY', object_name="Head")
ok(len(tracks(m)) == 3, "delete works (%s)" % tracks(m))

print("\n%d passed, %d failed" % (len(PASS), len(FAIL)), flush=True)
for f in FAIL:
    print("  FAILED: " + f, flush=True)
sys.exit(1 if FAIL else 0)
