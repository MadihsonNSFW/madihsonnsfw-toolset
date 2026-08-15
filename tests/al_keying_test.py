# Keying from the app (Marty, 2026-08-05): "in Anim Layers section i should be
# able to keyframe whatever i hover over (the key depends on whatever it is set
# in blender for the user), also ability to remove keyframe with another
# buttonpress".
#
#   blender.exe -b --factory-startup --python tests\al_keying_test.py
#
# ⚠ BACKGROUND BLENDER 5.2 DOES HAVE A WINDOW, A SCREEN AND AREAS - including a
# VIEW_3D. The old rule of thumb ("-b has no windows, so anything needing an
# area cannot be tested headless") is simply not true here, and believing it
# would have left this whole branch untested behind a comment. Measured:
#     windows: 1 | areas: PROPERTIES, OUTLINER, DOPESHEET_EDITOR, VIEW_3D
#     bpy.context.area: None       <- still None, so the override is still needed
#     bare bpy.ops.anim.keyframe_insert() -> poll() failed, context is incorrect
# So the OBJECT branch runs for real below. It was ALSO proven in a throwaway
# --factory-startup GUI instance driven from a bpy TIMER (the exact situation
# the bridge runs in): override insert -> {'FINISHED'}, 9 keys (loc/rot/scale),
# delete_v3d -> {'FINISHED'} back to 0, and nothing-selected -> {'CANCELLED'},
# which is a return value and NOT a raise - hence the guard in the engine.
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
server = sys.modules["madi_pkg.server"]

# ------------------------------------------------------- the command exists
ok(hasattr(core, "al_key_selection"),
   "keying: core.al_key_selection exists")
caps = server.BridgeServer.capabilities()
ok("anim_layers_key_selection" in caps,
   "keying: and the add-on ADVERTISES it - a route the app cannot see is a "
   "route the app will not offer")

# ⚠ The version had to move with it, in all three places. Checked here as well
# as in bridge_version_test because this is the change that bumped it.
# ⚠ Compared as a TUPLE, never pinned to a string — two suites once asserted an
# exact version and the next bump failed them both for no real reason
# (docs\testing.md).
ok(tuple(int(n) for n in core.ADDON_VERSION.split(".")) >= (0, 14, 0),
   "keying: the add-on is at least 0.14.0, the version that introduced it "
   "(got %s)" % core.ADDON_VERSION)

# ------------------------------------------------------- OBJECT branch, live
bpy.ops.wm.read_homefile(use_empty=True)
bpy.ops.mesh.primitive_cube_add()
cube = bpy.context.active_object
cube.name = "Hero"
ok(bpy.context.area is None,
   "keying: context.area really is None here - so this IS exercising the "
   "override path the bridge needs, not sneaking round it")


def key_count(datablock):
    """5.x actions are SLOTTED - `Action.fcurves` is gone and the curves live in
    layer > strip > channelbag. Both shapes handled so the count means the same
    thing on either."""
    ad = datablock.animation_data
    if not ad or not ad.action:
        return 0
    flat = getattr(ad.action, "fcurves", None)
    if flat is not None:
        return sum(len(fc.keyframe_points) for fc in flat)
    total = 0
    for layer in ad.action.layers:
        for strip in layer.strips:
            for bag in (getattr(strip, "channelbags", None) or ()):
                total += sum(len(fc.keyframe_points) for fc in bag.fcurves)
    return total


status = core.al_key_selection()
ok(key_count(cube) > 0,
   "keying: Blender's own I really keyed the selection (%d keys)"
   % key_count(cube))
ok(status.get("keyed", {}).get("objects") == 1,
   "keying: and the reply says what it was aimed at")
before = key_count(cube)
core.al_key_selection()
ok(key_count(cube) == before,
   "keying: pressing it twice on the same frame is not two keys (%d)"
   % key_count(cube))

gone = core.al_key_selection(delete=True)
ok(key_count(cube) == 0,
   "keying: and Alt+I's operator takes them all off again (%d left)"
   % key_count(cube))
ok(gone.get("keyed", {}).get("deleted") is True,
   "keying: reported as a removal, so the status line cannot claim otherwise")

# ⚠ Nothing selected must be refused BEFORE the operator, which only CANCELS.
bpy.ops.object.select_all(action='DESELECT')
try:
    core.al_key_selection()
    empty = ""
except RuntimeError as exc:
    empty = str(exc)
ok("Nothing is selected" in empty,
   "keying: with nothing selected it says so (%r)" % empty)
cube.select_set(True)
bpy.context.view_layer.objects.active = cube

# ⚠ The no-viewport refusal, forced. A user with only a Graph Editor and a
# Properties panel open is a real layout, and the operators cannot poll there.
_real_find = core._find_view3d
core._find_view3d = lambda: (None, None, None)
try:
    try:
        core.al_key_selection()
        refused = ""
    except RuntimeError as exc:
        refused = str(exc)
finally:
    core._find_view3d = _real_find
ok("3D viewport" in refused,
   "keying: with no 3D viewport the refusal is a SENTENCE, not a traceback "
   "(%r)" % refused)
ok(key_count(cube) == 0, "keying: and nothing was keyed on the way to refusing")

# The channel report is a pure read - it is what the app puts in its status line.
info = core._al_key_channel_info(False)
ok(info["objects"] == 1 and info["deleted"] is False,
   "keying: the report counts the selection (%d object)" % info["objects"])
ok(info["frame"] == bpy.context.scene.frame_current,
   "keying: and names the frame it is about")
ok(info["keying_set"] is None and info["channels"],
   "keying: with no active keying set it reports the USER'S default channels "
   "(%s) - the whole point is that Blender chooses, not us"
   % ", ".join(info["channels"]))
bpy.context.scene.keying_sets.new(idname="MADITestKS", name="Test KS")
named = core._al_key_channel_info(False)
ok(named["keying_set"] == "Test KS" and not named["channels"],
   "keying: an ACTIVE keying set wins and is NAMED instead of listing "
   "channels it does not control (%r)" % named["keying_set"])

# ------------------------------------------------------ SHAPEKEY branch, live
bpy.ops.wm.read_homefile(use_empty=True)
bpy.ops.mesh.primitive_uv_sphere_add()
ball = bpy.context.active_object
ball.name = "Blob"
ball.shape_key_add(name="Basis")
smile = ball.shape_key_add(name="Smile")
smile.value = 0.4
key = ball.data.shape_keys

ball.active_shape_key_index = 0
try:
    core.al_key_selection(data_type='SHAPEKEY')
    basis = ""
except RuntimeError as exc:
    basis = str(exc)
ok("Basis" in basis,
   "shapekey: the Basis is refused and says why (%r)" % basis)
ok(key.animation_data is None,
   "shapekey: and refusing wrote nothing")

ball.active_shape_key_index = 1
status = core.al_key_selection(data_type='SHAPEKEY')
ok(status.get("keyed", {}).get("shape_key") == "Smile",
   "shapekey: keying the ACTIVE key reports which one it was")
ok(key.animation_data is not None and key.animation_data.action is not None,
   "shapekey: and Blender really made an action for it")
ok(key_count(key) == 1,
   "shapekey: exactly ONE key, on the active shape only (%d)" % key_count(key))
ok(status.get("data_type") == "SHAPEKEY",
   "shapekey: the reply is the whole layer status, so the app's stack refreshes "
   "from the same round trip")

# ⚠ Deleting has to be the same button's opposite, not a near miss: it must
# report `deleted` so the status line cannot claim a key was ADDED.
gone = core.al_key_selection(delete=True, data_type='SHAPEKEY')
ok(gone.get("keyed", {}).get("deleted") is True,
   "shapekey: removing reports itself as a removal")
ok(key_count(key) == 0,
   "shapekey: and the key is really gone (%d left)" % key_count(key))

try:
    core.al_key_selection(delete=True, data_type='SHAPEKEY')
    twice = ""
except RuntimeError as exc:
    twice = str(exc)
ok("No key" in twice,
   "shapekey: removing again says there was nothing there (%r)" % twice)

bpy.ops.wm.read_homefile(use_empty=True)
bpy.ops.mesh.primitive_cube_add()
bpy.context.active_object.name = "Plain"
try:
    core.al_key_selection(data_type='SHAPEKEY')
    none = ""
except RuntimeError as exc:
    none = str(exc)
ok("no shape keys" in none,
   "shapekey: a mesh with no shape keys is told so plainly (%r)" % none)

print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
for label in FAIL:
    print("  FAILED: " + label)
sys.exit(1 if FAIL else 0)
