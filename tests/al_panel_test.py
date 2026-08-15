# Animation Layers inside Blender: the N-panel, its operators, and the settings
# it shares with the app.
#
#   blender.exe -b --factory-startup --python tests\al_panel_test.py
#
# The panel is registered for real and every operator is called, because the way
# this breaks is a bl_idname typo or a property that does not exist - neither of
# which any amount of reading catches.
#
# ⚠ draw() must never write. That is asserted here by drawing the panel against
# a real stack and checking the scene is untouched afterwards.
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


def load(name):
    spec = importlib.util.spec_from_file_location(
        "madi_%s" % name, os.path.join(ADDON, "%s.py" % name))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


core = load("core")

# ------------------------------------------------------------------ versions

with open(os.path.join(ADDON, "blender_manifest.toml"), encoding="utf-8") as fh:
    manifest = fh.read()
ok('version = "%s"' % core.ADDON_VERSION in manifest,
   "version: core.ADDON_VERSION %s matches the manifest" % core.ADDON_VERSION)
with open(os.path.join(ROOT, "app", "bridge.py"), encoding="utf-8") as fh:
    app_src = fh.read()
ok('EXPECTED_ADDON_VERSION = "%s"' % core.ADDON_VERSION in app_src,
   "version: and the app expects the same")
ok('"anim_layers_set_prefs"' in app_src,
   "version: the app knows the shared-settings command")

# --------------------------------------------------------------- the module
# The extension is not installed in this Blender, so the package is loaded from
# disk under a name of its own. `submodule_search_locations` is what lets its
# `from . import core` resolve.
spec = importlib.util.spec_from_file_location(
    "madi_pkg", os.path.join(ADDON, "__init__.py"),
    submodule_search_locations=[ADDON])
pkg = importlib.util.module_from_spec(spec)
sys.modules["madi_pkg"] = pkg
spec.loader.exec_module(pkg)
alui = sys.modules["madi_pkg.anim_layers_ui"]

ok(hasattr(alui, "register") and hasattr(alui, "unregister"),
   "module: the panel registers itself, like Bone Jiggle does")

# ⚠ The two sides of the settings mirror must list the SAME keys. This is the
# check that stops one UI quietly meaning something different by a switch.
prefs_props = set(pkg.MADILIB_Prefs.__annotations__)
shared = {"al_sync_names", "al_auto_blend", "al_default_blend"}
ok(shared <= prefs_props,
   "settings: the add-on preferences carry all three shared settings")
app_main = open(os.path.join(ROOT, "app", "main.py"), encoding="utf-8").read()
ok('SHARED_LAYER_PREFS = ("sync_names", "auto_blend", "default_blend")' in app_main,
   "settings: and the app names exactly the same three")

# ------------------------------------------------------------- registration

pkg.register()
# AddonPreferences only get an entry when the add-on is ENABLED, and nothing
# enabled it here - register_class alone is not enough. Making the entry by hand
# is what lets the shared-settings half of this suite run at all.
if "madi_pkg" not in bpy.context.preferences.addons:
    bpy.context.preferences.addons.new().module = "madi_pkg"

ok("MADILIB_PT_anim_layers" in dir(bpy.types),
   "register: the Animation Layers panel exists")
ok("MADILIB_PT_anim_layers_options" in dir(bpy.types),
   "register: with the Options sub-panel under it")
ok("MADILIB_MT_al_tools" in dir(bpy.types),
   "register: and the Layer Tools dropdown Marty asked for")
ok(bpy.types.MADILIB_PT_anim_layers.bl_options == {'DEFAULT_CLOSED'},
   "register: the panel starts collapsed, so it does not crowd the tab")
ok(bpy.types.MADILIB_PT_anim_layers.bl_category == "MadihsonNSFW",
   "register: in the same N-panel tab as everything else")
ok(bpy.types.MADILIB_PT_anim_layers_options.bl_parent_id
   == "MADILIB_PT_anim_layers",
   "register: Options nests inside it rather than adding a third panel")

props = bpy.context.window_manager.madilib_al
ok(props is not None, "register: the panel's property group is on the WM")

# ------------------------------------------------- no gate, and no gate module
# ⚠ THE ENTITLEMENT MODULE IS GONE ENTIRELY (add-on 0.47.0). Anim Layers was
# freed on 2026-08-06; the machinery that used to gate it was deleted with the
# rest of the licensing subsystem in 1.19.0. So the proof is no longer "locked
# and it still works" — it is that there is nothing left to lock with.
ok("madi_pkg.entitlement" not in sys.modules,
   "gate: the add-on imports no entitlement module at all")
ok(not os.path.exists(os.path.join(ADDON, "entitlement.py")),
   "gate: and entitlement.py is not in the package")
ok(not os.path.exists(os.path.join(ADDON, "ed25519.py")),
   "gate: nor the signature verifier it was the only user of")

ok(not hasattr(alui.MADILIB_OT_al_add, "poll"),
   "free: Anim Layers operators carry no poll gate any more")
ok(not hasattr(alui, "LOCKED_HINT"),
   "free: and no locked message survives in the module")
# The real proof: execute() must REACH run(). The gate used to return CANCELLED
# before ever getting there, so a stub whose run() records being called is what
# tells "the gate is gone" apart from "the operator happens to fail anyway".
_reached = []
_alstub = type("_S", (), {
    "report": lambda self, kind, msg: None,
    "run": lambda self, ctx, props: _reached.append(True) and None,
    "status": None,
})()
alui.MADILIB_OT_al_add.execute(_alstub, bpy.context)
ok(bool(_reached),
   "free: execute() REACHES run() - the real proof, since the gate used to "
   "refuse in execute() as well as poll()")
# ⚠⚠ EVERYTHING FROM HERE TO THE OPERATOR CHECK USED TO EXERCISE THE SIGNED
# LICENCE BLOB — malformed payloads, bad signatures, expiry, revocation, seat
# conflicts, the shared public key, and the "unlock state rides on the poll"
# rule that a whole evening was spent learning. All of it is gone with the
# subsystem in 1.19.0, and none of it can regress because none of it exists.
#
# What replaces it is the only thing still worth asserting: nothing anywhere
# claims to be licensed, and no route offers to unlock anything.
server_poll = open(os.path.join(ADDON, "server.py"), encoding="utf-8").read()
ok('"licensed"' not in server_poll,
   "gate: the status poll no longer reports a licensed flag")
ok("license_unlock" not in server_poll and "license_state" not in server_poll
   and "license_lock" not in server_poll,
   "gate: and the dispatcher routes no license_* command")
# ⚠ Comments are stripped LINE BY LINE, not by deleting "# " everywhere: the
# word survives in five explanatory comments recording that the gate is gone,
# and a crude strip counted those as live code.
_live = "".join(l for l in server_poll.splitlines(True)
                if not l.strip().startswith("#"))
ok("entitlement" not in _live,
   "gate: server.py does not reach for an entitlement module in live code")
# Every operator the panel and the menu reference must actually exist - a typo
# in a bl_idname is invisible until someone clicks it.
referenced = set()
for source in ("anim_layers_ui",):
    text = open(os.path.join(ADDON, "%s.py" % source), encoding="utf-8").read()
import re
for m in re.finditer(r'"(madilib\.[a-z_]+)"', text):
    referenced.add(m.group(1))
missing = [name for name in sorted(referenced)
           if not hasattr(bpy.ops.madilib, name.split(".", 1)[1])]
ok(not missing, "register: every operator the panel calls exists (%s)"
   % (missing or "all present"))

# --------------------------------------------------------------- a real stack

arm_data = bpy.data.armatures.new("A")
arm = bpy.data.objects.new("Rig", arm_data)
bpy.context.scene.collection.objects.link(arm)
bpy.context.view_layer.objects.active = arm
bpy.context.view_layer.update()

bpy.ops.object.mode_set(mode='EDIT')
eb = arm_data.edit_bones.new("bone")
eb.head, eb.tail = (0, 0, 0), (0, 0, 1)
bpy.ops.object.mode_set(mode='POSE')

props.data_type = 'OBJECT'
res = bpy.ops.madilib.al_add()
ok(res == {'FINISHED'}, "stack: Add Layer runs")
st = core.anim_layers_status(data_type='OBJECT')
ok(len(st["layers"]) == 1, "stack: and there is one layer (%d)" % len(st["layers"]))

bpy.ops.madilib.al_add()
st = core.anim_layers_status(data_type='OBJECT')
ok(len(st["layers"]) == 2, "stack: a second one lands on top")

# The new layer takes the SHARED default blend, not a hard-coded one.
prefs = bpy.context.preferences.addons["madi_pkg"].preferences
prefs.al_default_blend = 'ADD'
bpy.ops.madilib.al_add()
st = core.anim_layers_status(data_type='OBJECT')
ok(st["layers"][-1].get("blend_type") == 'ADD',
   "settings: a new layer uses the shared default blend (got %s)"
   % st["layers"][-1].get("blend_type"))

bpy.ops.madilib.al_select(index=1)
ok(props.active_index == 1, "stack: selecting a layer sets the active index")
ok(props.blend_type == st["layers"][1].get("blend_type"),
   "stack: and seeds the blend widget from the layer, not the other way round")

# ------------------------------------------- selecting must not block the click
# The expensive half of a select (entering NLA tweak mode) is deferred, so the
# highlight and widgets land immediately and the click feels instant.
alui._PENDING_SELECT.clear()
alui._PENDING.clear()
bpy.ops.madilib.al_select(index=2)
ok(props.active_index == 2,
   "select: the highlight moves IMMEDIATELY, before any tweak-mode work")
ok(alui._PENDING_SELECT.get("index") == 2,
   "select: while the tweak-mode switch is queued, not done inline")

# Clicking through layers coalesces - only the one you settle on is paid for.
bpy.ops.madilib.al_select(index=0)
bpy.ops.madilib.al_select(index=1)
ok(alui._PENDING_SELECT.get("index") == 1 and len(alui._PENDING_SELECT) == 2,
   "select: rapid clicks collapse to ONE pending target (%s)" % alui._PENDING_SELECT)

alui._flush_now()
ok(not alui._PENDING_SELECT, "select: flushing applies and clears it")
ok(core.anim_layers_status(data_type='OBJECT').get("active_index") == 1,
   "select: and Blender really is on that layer afterwards")

# ⚠ Any other operator must flush first, or a queued select would land AFTER the
# operator it was supposed to precede.
bpy.ops.madilib.al_select(index=0)
ok(alui._PENDING_SELECT.get("index") == 0, "select: queued again")
bpy.ops.madilib.al_state(index=0, field='mute', value=False)
ok(not alui._PENDING_SELECT,
   "select: another operator flushes the pending select before it runs")

# Re-clicking the layer that is already the target costs nothing at all.
alui._flush_now()
_invalidated = alui._status(bpy.context, fresh=True)
bpy.ops.madilib.al_select(index=props.active_index)
ok(not alui._PENDING_SELECT,
   "select: re-clicking the active layer queues nothing - tweak mode is the "
   "most expensive thing here, so it is never paid twice")

bpy.ops.madilib.al_state(index=0, field='mute', value=True)
st = core.anim_layers_status(data_type='OBJECT')
ok(st["layers"][0]["mute"] is True, "stack: mute toggles")
bpy.ops.madilib.al_state(index=0, field='lock', value=True)
st = core.anim_layers_status(data_type='OBJECT')
ok(st["layers"][0]["lock"] is True, "stack: lock toggles")
bpy.ops.madilib.al_state(index=0, field='lock', value=False)

bpy.ops.madilib.al_solo(index=1, off=False)
st = core.anim_layers_status(data_type='OBJECT')
ok(st.get("solo") is not None, "stack: solo engages")
bpy.ops.madilib.al_solo(index=1, off=True)
st = core.anim_layers_status(data_type='OBJECT')
ok(st.get("solo") is None, "stack: and releases")

before = [l["name"] for l in st["layers"]]
props.active_index = 0
bpy.ops.madilib.al_move(direction='UP')
after = [l["name"] for l in core.anim_layers_status(data_type='OBJECT')["layers"]]
ok(before != after, "stack: move reorders (%s -> %s)" % (before, after))

n_before = len(after)
bpy.ops.madilib.al_delete()
ok(len(core.anim_layers_status(data_type='OBJECT')["layers"]) == n_before - 1,
   "stack: delete removes one")

# ------------------------------------------------- draw() must not write

props.active_index = 0
st_before = core.anim_layers_status(data_type='OBJECT')
sig_before = [(l["name"], l["mute"], l["lock"]) for l in st_before["layers"]]
actions_before = len(bpy.data.actions)


class _FakeLayout:
    """Swallows every layout call, recording nothing. Enough for draw()."""

    def __getattr__(self, _name):
        return self

    def __call__(self, *a, **k):
        return self


class _Stub:
    """Stands in for the panel instance. A bpy_struct cannot be constructed
    from Python, so draw() is called unbound against something that only has to
    carry a `layout` - which is all draw() ever touches."""


def _draw(cls):
    stub = _Stub()
    stub.layout = _FakeLayout()
    cls.draw(stub, bpy.context)


try:
    _draw(bpy.types.MADILIB_PT_anim_layers)
    drew = True
except Exception as exc:                       # noqa: BLE001
    drew = False
    print("draw raised: %r" % exc, flush=True)
ok(drew, "draw: the panel draws against a real stack without raising")

st_after = core.anim_layers_status(data_type='OBJECT')
sig_after = [(l["name"], l["mute"], l["lock"]) for l in st_after["layers"]]
ok(sig_before == sig_after, "draw: and changed nothing about the layers")
ok(len(bpy.data.actions) == actions_before,
   "draw: and created no actions - draw() is a pure read")

# ⚠ There is no locked state to draw any more (1.19.0): the panel has exactly
# one appearance, and the "locked, so draw the explanation instead" branch went
# with the gate along with everything that could produce it.

try:
    _draw(bpy.types.MADILIB_PT_anim_layers_options)
    ok(True, "draw: the Options sub-panel draws too")
except Exception as exc:                       # noqa: BLE001
    ok(False, "draw: the Options sub-panel draws too (%r)" % exc)

try:
    _draw(bpy.types.MADILIB_MT_al_tools)
    ok(True, "draw: the Layer Tools dropdown draws")
except Exception as exc:                       # noqa: BLE001
    ok(False, "draw: the Layer Tools dropdown draws (%r)" % exc)

# ------------------------------------------------------- the settings mirror

prefs.al_sync_names = False
prefs.al_auto_blend = True
prefs.al_default_blend = 'REPLACE'
shared_now = alui.shared_prefs()
ok(shared_now == {"sync_names": False, "auto_blend": True,
                  "default_blend": "REPLACE"},
   "mirror: shared_prefs() reports exactly what the panel shows (%s)" % shared_now)

back = alui.apply_prefs({"sync_names": True, "default_blend": "ADD"})
ok(prefs.al_sync_names is True and prefs.al_default_blend == 'ADD',
   "mirror: the app's copy is taken")
ok(back["auto_blend"] is True,
   "mirror: a field the app did not send is left alone, not reset")
ok(alui.apply_prefs({"default_blend": "NONSENSE"})["default_blend"] == "ADD",
   "mirror: a value that is not a real blend type is ignored, not stored")
ok(alui.apply_prefs("not a dict") == {},
   "mirror: and a malformed payload cannot corrupt it")

# The status reply is what carries Blender's copy to the app, so it must be in
# there - this is the whole sync channel.
app_bridge = open(os.path.join(ROOT, "app", "bridge.py"), encoding="utf-8").read()
server_src = open(os.path.join(ADDON, "server.py"), encoding="utf-8").read()
ok('status["prefs"] = anim_layers_ui.shared_prefs()' in server_src,
   "mirror: anim_layers_status carries the settings, so the poll is the channel")
ok('cmd == "anim_layers_set_prefs"' in server_src,
   "mirror: and there is a command for the app to push its own")
ok("_prefs_synced" in app_main and "PREFS_ECHO_GUARD_S" in app_main,
   "mirror: the app has both guards - first-contact wins, and echo suppression")

pkg.unregister()
ok(not hasattr(bpy.context.window_manager, "madilib_al"),
   "unregister: the panel takes its property group with it")

print("")
print("%d passed, %d failed" % (len(PASS), len(FAIL)))
for f in FAIL:
    print("FAIL " + f)
sys.exit(1 if FAIL else 0)
