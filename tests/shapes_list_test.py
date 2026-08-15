# `list_shape_keys` - the source for the Save Shape Keys checklist.
#
#   blender.exe -b --factory-startup --python tests\shapes_list_test.py
#
# ⚠ WHY THIS SUITE EXISTS. Marty hit *"'Action' object has no attribute
# 'fcurves'"* the moment he pressed Export shapekeys on a mesh whose keys were
# KEYFRAMED (2026-08-05). `Action.fcurves` is gone on 5.x - slotted actions put
# the curves in slot > layer > strip > channelbag - and `_key_is_animated` read
# it directly. It had NO Blender-side coverage at all: `app_shapes_test.py`
# feeds the dialog a hand-written dict, so the whole read path was stubbed out
# and every test passed while the real thing raised.
#
# The lesson worth keeping: a suite that stubs the data source cannot test the
# data source. Where the app has a stub, Blender needs a suite.
import importlib.util
import os
import sys

import bpy

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

ROOT = _ROOT
ADDON = os.path.join(ROOT, "blender_addon", "madi_anim_library")
PASS, FAIL = [], []


def ok(cond, label):
    (PASS if cond else FAIL).append(label)
    print(("ok   " if cond else "FAIL ") + label, flush=True)


spec = importlib.util.spec_from_file_location(
    "madi_pkg", os.path.join(ADDON, "__init__.py"),
    submodule_search_locations=[ADDON])
pkg = importlib.util.module_from_spec(spec)
sys.modules["madi_pkg"] = pkg
spec.loader.exec_module(pkg)
core = sys.modules["madi_pkg.core"]


def keys_of(rows, obj):
    for row in rows:
        if row["object"] == obj:
            return {k["name"]: k for k in row["keys"]}
    return {}


# --------------------------------------------------- the shape it must survive
bpy.ops.wm.read_homefile(use_empty=True)
bpy.ops.mesh.primitive_uv_sphere_add()
ball = bpy.context.active_object
ball.name = "Blob"
ball.shape_key_add(name="Basis")
smile = ball.shape_key_add(name="Smile")
frown = ball.shape_key_add(name="Frown")
blink = ball.shape_key_add(name="Blink")

# KEYFRAMED - this is the one that raised. It gives the Key datablock an action,
# and on 5.x that action has no `.fcurves` at all.
ball.active_shape_key_index = 1
smile.keyframe_insert("value")
ok(not hasattr(ball.data.shape_keys.animation_data.action, "fcurves"),
   "5.x: the action really has NO .fcurves attribute - if this ever fails, the "
   "bug it guards cannot happen and the compat branch is dead code")

rows = core.list_shape_keys(["Blob"])
ok(bool(rows), "list: it returns something instead of raising")
found = keys_of(rows, "Blob")
ok(set(found) == {"Basis", "Smile", "Frown", "Blink"},
   "list: every key is listed (%s)" % sorted(found))
ok(found["Smile"]["has_animation"] is True,
   "list: the KEYFRAMED key is reported as animated - and this has never once "
   "been true on 5.x before now, it raised")
ok(found["Frown"]["has_animation"] is False,
   "list: an untouched key is not")
ok(found["Basis"]["is_basis"] is True and found["Smile"]["is_basis"] is False,
   "list: the Basis is marked")

# ⚠ An empty CHANNEL is not animation. A curve with no keys would light the
# filter up for a key nothing actually moves.
ball.active_shape_key_index = 3
blink.keyframe_insert("value")
container = None
for layer in ball.data.shape_keys.animation_data.action.layers:
    for strip in layer.strips:
        bag = strip.channelbag(ball.data.shape_keys.animation_data.action_slot)
        if bag is None:
            continue
        for fc in bag.fcurves:
            if fc.data_path == 'key_blocks["Blink"].value':
                container = fc
while container is not None and len(container.keyframe_points):
    container.keyframe_points.remove(container.keyframe_points[0])
found = keys_of(core.list_shape_keys(["Blob"]), "Blob")
ok(found["Blink"]["has_animation"] is False,
   "list: a channel with its keys removed is NOT animated - an empty curve "
   "moves nothing")

# ---------------------------------------------------------------- drivers
driven = ball.data.shape_keys.driver_add('key_blocks["Frown"].value')
driven.driver.type = 'SCRIPTED'
driven.driver.expression = "0.5"
found = keys_of(core.list_shape_keys(["Blob"]), "Blob")
ok(found["Frown"]["has_driver"] is True,
   "drivers: a driven key is reported as driven")
ok(found["Frown"]["has_animation"] is False,
   "drivers: and NOT as animated - they live in different places and mean "
   "different things (that is why the dialog offers two filters)")
ok(found["Smile"]["has_driver"] is False,
   "drivers: a keyframed key is not reported as driven either")

# ------------------------------------------------------ animated inside an NLA
# A key animated only inside a strip is still animated; reporting it free would
# send it to the exporter as a static value.
bpy.ops.wm.read_homefile(use_empty=True)
bpy.ops.mesh.primitive_cube_add()
box = bpy.context.active_object
box.name = "Boxy"
box.shape_key_add(name="Basis")
squash = box.shape_key_add(name="Squash")
box.active_shape_key_index = 1
squash.keyframe_insert("value")
key = box.data.shape_keys
ad = key.animation_data
pushed = ad.action
ad.nla_tracks.new().strips.new("pushed", 1, pushed)
ad.action = None
found = keys_of(core.list_shape_keys(["Boxy"]), "Boxy")
ok(found["Squash"]["has_animation"] is True,
   "nla: a key animated only inside a strip still counts as animated")
ok(found["Basis"]["has_animation"] is False,
   "nla: and the Basis beside it does not")

# ------------------------------------------------------------- the plain cases
bpy.ops.wm.read_homefile(use_empty=True)
bpy.ops.mesh.primitive_cube_add()
bpy.context.active_object.name = "Plain"
rows = core.list_shape_keys(["Plain"])
ok(rows and rows[0]["keys"] == [],
   "plain: a mesh with no shape keys lists none, and does not raise")
ok(rows[0]["verts"] == 8,
   "plain: the vertex count comes back too (the exporter's match check)")

print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
for label in FAIL:
    print("  FAILED: " + label)
sys.exit(1 if FAIL else 0)
