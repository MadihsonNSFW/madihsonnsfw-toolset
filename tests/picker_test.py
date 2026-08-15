# The Bone picker, inside Blender: registration as a package submodule, the
# licence gate, the picker_* bridge API and the .picker library item.
#
#   blender.exe -b --factory-startup --python tests\picker_test.py
#
# ONE suite rather than the two the plan sketched: the setup (load the package,
# register, fake an add-on entry) is identical for all of it, and a second
# Blender launch buys nothing.
#
# ⚠ This can only exist because the picker's GPU shaders are built on FIRST USE.
# `gpu.shader.from_builtin()` at module level raises SystemError under
# `blender -b`, which would take registration down with it and put the picker
# permanently out of reach of any test, preset conversion or CI.
import importlib.util
import json
import os
import shutil
import sys
import tempfile

import bpy

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

ROOT = _ROOT
ADDON = os.path.join(ROOT, "blender_addon", "madi_anim_library")

PASS, FAIL = [], []


def ok(cond, label):
    (PASS if cond else FAIL).append(label)
    print(("ok   " if cond else "FAIL ") + label, flush=True)


# The extension is not installed here, so the package is loaded from disk under
# a name of its own. `submodule_search_locations` is what lets `from . import
# core` resolve.
spec = importlib.util.spec_from_file_location(
    "madi_pkg", os.path.join(ADDON, "__init__.py"),
    submodule_search_locations=[ADDON])
pkg = importlib.util.module_from_spec(spec)
sys.modules["madi_pkg"] = pkg
spec.loader.exec_module(pkg)
picker = sys.modules["madi_pkg.picker"]
core = sys.modules["madi_pkg.core"]
ent = sys.modules["madi_pkg.entitlement"]
server = sys.modules["madi_pkg.server"]

# ------------------------------------------------------------------ versions
with open(os.path.join(ADDON, "blender_manifest.toml"), encoding="utf-8") as fh:
    manifest = fh.read()
ok('version = "%s"' % core.ADDON_VERSION in manifest,
   "version: core.ADDON_VERSION %s matches the manifest" % core.ADDON_VERSION)
with open(os.path.join(ROOT, "app", "bridge.py"), encoding="utf-8") as fh:
    app_bridge = fh.read()
ok('EXPECTED_ADDON_VERSION = "%s"' % core.ADDON_VERSION in app_bridge,
   "version: and the app expects the same")
ok('"bone_picker"' in app_bridge and '"picker_status", "0.10.0"' in app_bridge,
   "version: the picker declares a FEATURE_REQUIREMENTS entry, so an older "
   "add-on costs this one tab and not the app")

# ------------------------------------------------------------- registration
pkg.register()
if "madi_pkg" not in bpy.context.preferences.addons:
    # AddonPreferences only get an entry when the add-on is ENABLED, and nothing
    # enabled it here - register_class alone is not enough.
    bpy.context.preferences.addons.new().module = "madi_pkg"

ok("MADI_PT_picker" in dir(bpy.types), "register: the Image Editor panel exists")
ok(bpy.types.MADI_PT_picker.bl_space_type == 'IMAGE_EDITOR',
   "register: it stays in the Image Editor, where the canvas is")
ok(hasattr(bpy.types.Object, "madi_picker"),
   "register: buttons still live on the armature, so old .blends keep theirs")
ops = [n for n in dir(bpy.ops.madi_picker) if not n.startswith("_")]
ok(len(ops) == 21, "register: all 21 operators are reachable (got %d)" % len(ops))
# ⚠ Named, not just counted. The count above catches a class that failed to
# register; only naming it catches the operator being registered under a
# DIFFERENT id, which is what a copy-paste bl_idname produces - and the panel
# would then draw a button that raises on click.
ok("viewport_overlays" in ops,
   "register: the bones/extras viewport toggle is registered under its own id")

# ⚠ THE PREFS TRAP. As a single-file add-on the picker carried its own
# AddonPreferences keyed `bl_idname = __name__`; inside a package that is a
# MODULE PATH, not an add-on key, so the lookup returns None - and every reader
# falls back to a constant WITHOUT RAISING. The three appearance settings would
# silently stop working and read as settings that reset themselves. This
# assertion is the only thing that catches it.
prefs = picker._prefs()
ok(prefs is not None,
   "prefs: _prefs() RESOLVES (the silent trap - a None here means Button "
   "Opacity / Corner Roundness / Darken Background quietly do nothing)")
ok(not hasattr(picker, "MADI_PickerPreferences"),
   "prefs: the picker's own preferences class is gone, not shadowing this")
ok(abs(prefs.pk_btn_round - picker.BTN_ROUND * 100.0) < 1e-4,
   "prefs: roundness still defaults to %.0f%%, the value it always had"
   % (picker.BTN_ROUND * 100.0))
ok(hasattr(prefs, "pk_btn_alpha") and hasattr(prefs, "pk_bg_darken"),
   "prefs: all three moved onto the extension's real preferences class")

# ------------------------------------------------------- handler hygiene
lp = [f.__name__ for f in bpy.app.handlers.load_post]
fc = [f.__name__ for f in bpy.app.handlers.frame_change_post]
ok(lp.count("_on_load_post") == 1 and fc.count("_on_frame_change") == 1,
   "handlers: registered exactly once")
ok(picker._DRAW_KEY in bpy.app.driver_namespace,
   "handlers: the draw handle is parked in driver_namespace - `_state` is "
   "replaced by a module reload and Blender cannot enumerate draw handlers, "
   "so a handle lost there could never be removed")

# ⚠ THE RELOAD CASE. A dev reload purges sys.modules, so the reloaded
# functions are different objects and an identity check cannot find the old
# ones - they would keep firing against a dead module, once per frame.
picker.unregister()
picker.register()
lp = [f.__name__ for f in bpy.app.handlers.load_post]
fc = [f.__name__ for f in bpy.app.handlers.frame_change_post]
ok(lp.count("_on_load_post") == 1 and fc.count("_on_frame_change") == 1,
   "handlers: an unregister/register cycle does NOT double them up")

# ------------------------------------------------------------- the bridge API
srv = server.BridgeServer()


def call(cmd, **params):
    return srv._handle({"cmd": cmd, "params": params})


caps = [c for c in srv.capabilities() if c.startswith("picker_")]
ok(len(caps) == 15,
   "bridge: 15 picker commands advertised, derived from the dispatcher's own "
   "source so the list cannot go stale (got %d)" % len(caps))

# ⚠ PURITY. picker_status is POLLED, so anything it wrote would land on a timer
# for as long as the app is open. In particular it must never call
# _ensure_tabs(), which CREATES a tab.
bpy.context.scene.madi_picker_tab_index = 0
while len(bpy.context.scene.madi_picker_tabs):
    bpy.context.scene.madi_picker_tabs.remove(0)
st = call("picker_status")
ok(len(bpy.context.scene.madi_picker_tabs) == 0,
   "bridge: picker_status is a PURE READ - asking what is there must not be "
   "what creates a scene's first tab")
ok(st["tabs"] == [] and st["armature"] is None and st["buttons"] == [],
   "bridge: and it answers sensibly on an empty scene")


def make_rig(name, bones):
    data = bpy.data.armatures.new(name)
    obj = bpy.data.objects.new(name, data)
    bpy.context.scene.collection.objects.link(obj)
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.mode_set(mode='EDIT')
    for bone in bones:
        eb = data.edit_bones.new(bone)
        eb.head, eb.tail = (0, 0, 0), (0, 0, 1)
    bpy.ops.object.mode_set(mode='OBJECT')
    return obj


rig_a = make_rig("Rig A", ["root", "spine", "head"])
rig_b = make_rig("Rig B", ["root", "spine"])
bpy.data.images.new("ref", 64, 64)

st = call("picker_add_tab", name="Body")
ok(st["added"] == "Body" and len(st["tabs"]) == 1, "bridge: add_tab")
st = call("picker_add_tab", name="Face")
ok([t["uid"] for t in st["tabs"]] == [0, 1],
   "bridge: uids are a monotonic high-water mark, never reused")
st = call("picker_set_tab", index=0)
ok(st["active_index"] == 0, "bridge: set_tab switches")
st = call("picker_rename_tab", name="Body v2")
ok(st["renamed"] == ["Body", "Body v2"], "bridge: rename reports both names")
st = call("picker_set_tab_rig", object="Rig A")
ok(st["rig"] == "Rig A" and st["armature"] == "Rig A", "bridge: set the rig")
st = call("picker_set_tab_image", image="ref")
ok(st["image"] == "ref", "bridge: set the background")
ok(st["armatures"] == ["Rig A", "Rig B"] and "ref" in st["images"],
   "bridge: the app is handed the real choices, so it never guesses a name")
ok(st["bones"] == ["head", "root", "spine"],
   "bridge: and the rig's bones, which is what makes retargeting possible")

uid = st["tabs"][0]["uid"]
for bone in ("root", "spine", "MISSING"):
    b = rig_a.madi_picker.buttons.add()
    b.kind, b.bone, b.label, b.tab_uid = 'BONE', bone, bone, uid
grp = rig_a.madi_picker.buttons.add()
grp.kind, grp.label, grp.tab_uid = 'GROUP', "arm", uid
grp.members.add().bone = "head"
grp.members.add().bone = "NOPE"

st = call("picker_status")
ok(len(st["buttons"]) == 4 and st["unmatched"] == 2,
   "bridge: buttons are reported with what does not resolve")
ok(st["buttons"][2]["missing"] == ["MISSING"]
   and st["buttons"][3]["missing"] == ["NOPE"],
   "bridge: a GROUP reports the MEMBER that is missing, not the whole button")

st = call("picker_set_button", index=0, label="ROOT", scale=2.0, blank=True,
          color=[1.0, 0.0, 0.0])
b0 = st["buttons"][0]
ok(b0["label"] == "ROOT" and b0["scale"] == 2.0 and b0["blank"] is True
   and b0["color"] == [1.0, 0.0, 0.0], "bridge: edit a button")
st = call("picker_set_button", index=2, bone="head")
ok(st["buttons"][2]["missing"] == [], "bridge: retarget a bone")
st = call("picker_set_button", index=3, member_index=1, member_bone="spine")
ok(st["unmatched"] == 0, "bridge: retarget one member of a group")

st = call("picker_remove_buttons", indices=[0])
ok(st["deleted"] == 1 and len(st["buttons"]) == 3, "bridge: remove a button")

st = call("picker_set_prefs", prefs={"btn_alpha": 80.0})
ok(st["prefs"]["btn_alpha"] == 80.0 and picker._prefs().pk_btn_alpha == 80.0,
   "bridge: prefs write through to the ONE store (the add-on preferences) - "
   "there is deliberately no second copy in the app to drift from")

refused = False
try:
    call("picker_start")
except RuntimeError:
    refused = True
ok(refused and picker._state["running"] is False,
   "bridge: starting with no Image Editor refuses with a reason and starts "
   "nothing (a modal session cannot be proven headless beyond this)")

before = len(rig_a.madi_picker.buttons)
st = call("picker_remove_tab", index=0)
ok(st["buttons_removed"] == 3 and len(st["tabs"]) == 1,
   "bridge: removing a tab takes ITS buttons, by uid across every armature")
refused = False
try:
    call("picker_remove_tab")
except RuntimeError:
    refused = True
ok(refused, "bridge: the last tab cannot be removed")

# ⚠ THE BRIDGE IS A SECOND WAY IN. The operators are gated, but these routes
# call the picker's API functions directly - so without a check here a ten-line
# socket client would drive the whole picker with no licence, which is EASIER
# than editing the add-on and would make the operator gate decorative.
ent.lock("test")
ok(isinstance(call("picker_status"), dict),
   "bridge: status stays readable while locked - it is a pure read of the "
   "user's own scene and withholding it protects nothing")
# ⚠ THIS BLOCK ASSERTED THE OPPOSITE UNTIL 2026-08-06: it called all 14
# `picker_*` writes with entitlement LOCKED and required every one to raise. The
# picker is free now, so a WRITE MUST GO THROUGH while locked. (It also stopped
# working as written the moment the gate went: `picker_set_button` then reached
# real code and raised IndexError, which `except RuntimeError` does not catch,
# so the suite died rather than failing an assertion.)
before = len(bpy.context.scene.madi_picker_tabs)
st = call("picker_add_tab", name="FreeWhileLocked")
ok(len(st["tabs"]) == before + 1,
   "bridge: a picker WRITE succeeds with no licence at all")
call("picker_remove_tab")
ok(len(bpy.context.scene.madi_picker_tabs) == before,
   "bridge: (and the scene is put back for the tests below)")

# ⚠ FLIPPED 2026-08-14: the Optimizer's gate is GONE — every tab went free
# and all three prefix gates left server.py (premium packs are the paid
# thing now, gated in the app's licence server). `opt_apply` is still a name
# no dispatcher serves, so the failure it must produce is UNKNOWN COMMAND,
# never a licence refusal — which is exactly what proves nothing intercepts
# the prefix before dispatch any more.
opt_error = ""
try:
    call("opt_apply")
except Exception as exc:                    # noqa: BLE001
    opt_error = str(exc).lower()
ok("unknown" in opt_error and "locked" not in opt_error,
   "bridge: a fake opt_* name dies as UNKNOWN, not as a licence refusal - "
   "no gate sits in front of the dispatcher (%r)" % opt_error)

# ⚠ MadiRef's gate went the same way, and the probe stays BEHAVIOURAL for
# the same reason the old one was: a source check proves a gate was written
# (or deleted), this proves what the dispatcher actually DOES. Opening a
# nonsense segment must fail for a REAL reason now, never with "locked".
mr_error = ""
try:
    call("madiref_open", name="madiref_nope")
except Exception as exc:                    # noqa: BLE001
    mr_error = str(exc).lower()
ok("locked" not in mr_error,
   "bridge: madiref_open fails for a real reason (no such segment), not a "
   "licence one - its gate is gone too (%r)" % mr_error)

# ⚠ ...and the two EXEMPTIONS really are exempt. `madiref_close` restores the
# scene's own sync_mode as well as removing the draw handlers, so a lapsed
# licence that refused it would strand a user with a reference painted over
# their viewport and no way out (the opt_revert_* principle).
close_ok = status_ok = False
try:
    call("madiref_close")
    close_ok = True
except RuntimeError as exc:
    close_ok = "locked" not in str(exc).lower()
except Exception:
    close_ok = True          # any non-licence error still means it got through
try:
    status_ok = isinstance(call("madiref_status"), dict)
except RuntimeError as exc:
    status_ok = "locked" not in str(exc).lower()
except Exception:
    pass
ok(close_ok, "bridge: madiref_close is EXEMPT - undoing our own change must "
   "never be what a lapsed licence takes away")
ok(status_ok, "bridge: madiref_status is EXEMPT - the app needs it to notice "
   "a stale overlay it must then close")

ent._STATE.update(unlocked=True, not_after=99999999999, reason="ok")

# ------------------------------------------------------ the .picker item
ok(".picker" in core.ITEM_EXTS, "item: .picker is a library item type")
sys.path.insert(0, os.path.join(ROOT, "app"))
import library as applib                                        # noqa: E402
# ⚠ The app scans the same tree WITHOUT a bridge, so it carries its own copy of
# this tuple. A type in one and not the other saves fine and never appears.
ok(tuple(applib.ITEM_EXTS) == tuple(core.ITEM_EXTS),
   "item: the app's extension list matches core's exactly (silent otherwise)")
ok(applib.DATA_FILES.get("picker") == "picker.json",
   "item: and the app knows which file carries the payload")

lib_root = tempfile.mkdtemp(prefix="madi_picker_lib_")
try:
    call("picker_set_tab_rig", object="Rig A")
    call("picker_set_tab_image", image="ref")
    uid = call("picker_status")["tabs"][0]["uid"]
    for bone in ("root", "spine", "head"):
        b = rig_a.madi_picker.buttons.add()
        b.kind, b.bone, b.label, b.tab_uid = 'BONE', bone, bone.upper(), uid

    st = call("picker_save_item", library_root=lib_root, folder="Lily",
              name="body layout")
    item_dir = st["saved_path"]
    ok("tabs" in st and st["saved_buttons"] == 3,
       "item: save answers with the WHOLE STATUS plus saved_* keys - the app "
       "broadcasts every reply to its tools, so a bare result dict would read "
       "as a status with no tabs and blank the tab list")
    ok(item_dir.endswith(".picker")
       and os.path.isfile(os.path.join(item_dir, "picker.json")),
       "item: it is a normal item folder with a picker.json payload")
    ok(st["saved_thumbnail"]
       and os.path.isfile(os.path.join(item_dir, "thumbnail.jpg")),
       "item: the thumbnail is the tab's REFERENCE PICTURE - a viewport render "
       "would show the character, not the thing being saved")

    with open(os.path.join(item_dir, "picker.json"), encoding="utf-8") as fh:
        data = json.load(fh)
    ok(data["format"] == "madi_picker_preset" and data["version"] == 6,
       "item: the payload is the UNCHANGED v6 preset, so an existing .json "
       "converts by being dropped in and old readers still understand it")
    ok(data["metadata"]["source_armature"] == "Rig A",
       "item: with the usual library metadata alongside it")

    found = [i for i in core.list_items(lib_root) if i["type"] == "picker"]
    ok(len(found) == 1, "item: the add-on's scanner finds it")
    app_found = [i for i in applib.scan(lib_root)[1] if i.type == "picker"]
    ok(len(app_found) == 1, "item: and so does the app's")

    call("picker_add_tab", name="Onto B")
    call("picker_set_tab_rig", object="Rig B")
    st = call("picker_apply_item", path=item_dir)
    ok(st["added"] == 3 and st["missing"] == ["head"],
       "item: applying onto a DIFFERENT rig loads every button and names what "
       "that rig does not have")

    refused = False
    try:
        call("picker_save_item", library_root=lib_root, folder="Lily",
             name="body layout")
    except RuntimeError:
        refused = True
    ok(refused, "item: saving over an existing item needs overwrite")
    call("picker_set_tab", index=0)
    call("picker_save_item", library_root=lib_root, folder="Lily",
         name="body layout", overwrite=True)
    ok(os.path.isdir(os.path.join(item_dir, "versions", "v001")),
       "item: and an overwrite versions the old one, like every other type")

    # ⚠ WAS "loading one is REFUSED while locked". Studio Library was already
    # free, so a .picker item was VISIBLE without a licence but could not be
    # APPLIED - a deliberate, slightly awkward split. Now that the picker itself
    # is free the split is gone: seeing it and using it need the same nothing.
    ent.lock("test")
    applied = False
    try:
        call("picker_apply_item", path=item_dir)
        applied = True
    except RuntimeError as exc:
        print("   apply raised: %s" % exc)
    ok(applied,
       "item: a .picker item LOADS with no licence - Studio Library could "
       "always show it, and now the picker can actually use it")
    ok(len([i for i in core.list_items(lib_root) if i["type"] == "picker"]) == 1,
       "item: but it stays LISTED - these are the user's own files, and hiding "
       "them would make the library differ from one machine to the next")
    ent._STATE.update(unlocked=True, not_after=99999999999, reason="ok")
finally:
    shutil.rmtree(lib_root, ignore_errors=True)

# ------------------------------------------------------------ preset maths
btn = rig_a.madi_picker.buttons[0]
btn.blank = False
btn.label = "TEST"
h1 = picker._fit_height(btn, 1.0)
btn.h = h1
h2 = picker._fit_height(btn, 1.0)
ok(abs(h1 - h2) < 1e-9,
   "maths: _fit_height is idempotent - it only ever shrinks, so re-running it "
   "must be a no-op")


# NOTE: this section runs LAST on purpose. Now that the picker is FREE its
# operators really execute, and creating a tab consumes a uid from a monotonic
# high-water mark that never reuses one - which broke the bridge tests'
# expected `[0, 1]` when this block sat above them.
# ------------------------------------------------ NO licence gate any more
# ⚠ THIS SECTION ASSERTED THE EXACT OPPOSITE UNTIL 2026-08-06. The picker was
# a paid tool: every operator was wrapped with an `entitlement.unlocked()` check
# on its poll AND its execute/invoke, the panel drew a "Locked" card, and
# `madi_picker.stop` was the single exemption. Marty freed it ("Bone pickers,
# Anim Layers and Node setup Tabs should be free and not pay gated"), so the
# gate was REMOVED rather than left returning True - and these checks exist to
# prove it is really gone rather than merely switched off.
ops = [n for n in dir(picker) if n.startswith("MADI_OT_picker_")]
ok(len(ops) > 10, "free: (there are %d picker operators to check)" % len(ops))
ok(not any(getattr(picker, n).__dict__.get("_madi_gated") for n in ops),
   "free: NO operator carries the gate marker any more")
ok(not hasattr(picker, "_install_gate") and not hasattr(picker, "_UNGATED"),
   "free: and the gate machinery itself is gone, not just unused")
ok(not hasattr(picker, "LOCKED_HINT"),
   "free: no locked message survives either - a free tool must not keep the "
   "words that would tell someone to go and pay")

# The real proof: with entitlement LOCKED, the picker still works end to end.
ent._STATE.update(unlocked=False, sub=None, not_after=0, reason="locked")
ok(ent.unlocked() is False, "free: (entitlement really is locked for this test)")


class _Report:
    def __init__(self):
        self.msgs = []

    def report(self, kind, msg):
        self.msgs.append((tuple(kind)[0], msg))


tabs_before = len(bpy.context.scene.madi_picker_tabs)
stub = _Report()
res = picker.MADI_OT_picker_add_tab.execute(stub, bpy.context)
ok(res == {'FINISHED'},
   "free: add_tab RUNS with no licence at all (%r)" % (res,))
ok(len(bpy.context.scene.madi_picker_tabs) == tabs_before + 1,
   "free: ...and really added the tab")
ok(not any(k == 'ERROR' for k, _m in stub.msgs),
   "free: with no error reported")

raised = None
try:
    bpy.ops.madi_picker.add_tab()
except RuntimeError as exc:
    raised = str(exc)
ok(raised is None,
   "free: and the bpy.ops path is open too - that was the whole point of "
   "gating the operators rather than the panel, so it is the whole point of "
   "ungating them (%r)" % raised)

ok(bool(bpy.ops.madi_picker.stop.poll()),
   "free: Stop still pressable, as it always was")

# ⚠ PUT THE SCENE BACK. While the picker was gated these calls were all
# refused, so this section changed nothing and the rest of the suite inherited
# a clean scene. Now they SUCCEED - two tabs' worth - and the bridge tests
# below index into `madi_picker_tabs` by position. Leaving them behind made
# `picker_set_button` raise `IndexError: No button at index 0`.
tabs = bpy.context.scene.madi_picker_tabs
while len(tabs) > tabs_before:
    tabs.remove(len(tabs) - 1)
ok(len(tabs) == tabs_before,
   "free: (scene restored, so the bridge tests below start where they used to)")


# ---- the panel draws the real thing while locked, and writes nothing -------
class _FakeLayout:
    """Swallows every layout call. Enough for draw()."""

    def __getattr__(self, _n):
        return self

    def __call__(self, *a, **k):
        return self


class _Panel:
    pass


def _draw():
    p = _Panel()
    p.layout = _FakeLayout()
    picker.MADI_PT_picker.draw(p, bpy.context)


tabs_before = len(bpy.context.scene.madi_picker_tabs)
drew = True
try:
    _draw()
except Exception as exc:                # noqa: BLE001
    drew = False
    print("   draw raised: %s" % exc)
ok(drew, "free: the panel draws with no licence (and no locked card)")
ok(len(bpy.context.scene.madi_picker_tabs) == tabs_before,
   "free: and draw() WRITES NOTHING - a Panel may not touch ID data")


# --------------------------------------------------------------------------
# Playback: a frame change must only repaint when something on screen actually
# follows the frame (v0.24.2, perf — Marty: "the picker works slow with many
# buttons"). It used to repaint unconditionally, so playing an animation
# rebuilt every button and re-uploaded both vertex buffers once per frame to
# draw an identical picture.
arm = bpy.data.objects.get("rig_a") or rig_a
ctx = bpy.context
for i in range(len(arm.madi_picker.buttons) - 1, -1, -1):
    arm.madi_picker.buttons.remove(i)

# ⚠ POINT THE ACTIVE TAB AT THIS RIG. `_target` resolves through the tab, not
# through the active object, so without this the scan reads whichever rig an
# earlier test left on the tab - and the checks below would pass or fail on
# somebody else's buttons.
_tab = picker._active_tab(ctx)
_tab.armature = arm
tab_uid = picker._active_uid(ctx)
ok(picker._target(ctx) == arm, "playback: the active tab points at the test rig")

b = arm.madi_picker.buttons.add()
b.kind = 'BONE'
b.bone = arm.pose.bones[0].name
b.tab_uid = tab_uid

redraws = []
_real_tag = picker._tag_redraw
picker._tag_redraw = lambda c: redraws.append(1)
picker._state["running"] = True


def _frame_repaints():
    """Repaints caused by ONE frame change, and nothing else.

    ⚠ Cleared immediately before the call on purpose. Writing to a button's
    `bone` / `sk_key` fires that property's `update=` callback, which tags a
    redraw of its own - so a counter left running since the last edit is
    measuring the EDIT, not the frame change. Cost twenty minutes of blaming
    the handler for a test that was counting the setup.
    """
    redraws.clear()
    picker._on_frame_change(bpy.context.scene)
    return len(redraws)


try:
    ok(picker._frame_dependent(ctx) is False,
       "playback: a tab of BONE buttons does not follow the frame")
    ok(_frame_repaints() == 0,
       "playback: ...so a frame change repaints NOTHING (it used to repaint all)")

    s = arm.madi_picker.buttons.add()
    s.kind = 'SLIDER'
    s.tab_uid = tab_uid
    s.sk_object = "nope"
    s.sk_key = "nope"
    ok(picker._frame_dependent(ctx) is True,
       "playback: ONE slider on the tab makes it frame-dependent again")
    ok(_frame_repaints() == 1,
       "playback: ...and the repaint comes back - sliders still track the frame")

    # ...and none of it happens at all with the picker stopped.
    picker._state["running"] = False
    ok(_frame_repaints() == 0,
       "playback: a stopped picker never repaints, slider or not")
finally:
    picker._tag_redraw = _real_tag
    picker._state["running"] = False

# --------------------------------------------------------------------------
# Label size ceiling (v0.23.0 — the ZOOM CRASH). Labels are fitted in screen
# px and the fit used to be handed straight to blf.size(); the px size tracks
# zoom, the Image Editor's zoom is unbounded, and a deep zoom asked blf for
# glyphs thousands of px tall — Blender hard-crashed with no crash.txt (an
# abort inside a huge glyph allocation). _label_draw_size is the choke point:
# everything blf rasterises goes through it, so these checks ARE the crash
# regression, headless.
ok(picker.MAX_LABEL_RASTER_PX <= 256.0,
   "labels: the rasterisation ceiling exists and is sane (%.0f px)"
   % picker.MAX_LABEL_RASTER_PX)

_seen_rungs = set()
_bad = None
px = 5.0
while px < 1.5e6 and _bad is None:          # the sweep crosses the crash zone
    d, s = picker._label_draw_size(px)
    _seen_rungs.add(round(d, 4))
    if d > picker.MAX_LABEL_RASTER_PX + 1e-9:
        _bad = "draw_px %.2f over the ceiling at fitted %.1f" % (d, px)
    elif d > px + 1e-9:
        _bad = "draw_px %.2f exceeds the fitted %.1f (padding contract)" % (d, px)
    elif s < 1.0 - 1e-9:
        _bad = "scale %.4f below 1 at fitted %.1f" % (s, px)
    elif px > picker.MAX_LABEL_RASTER_PX and abs(d * s - px) > px * 1e-6:
        _bad = "draw_px*scale %.2f != fitted %.1f past the ceiling" % (d * s, px)
    elif px <= picker.MAX_LABEL_RASTER_PX and s != 1.0:
        _bad = "scale %.4f engaged under the ceiling at fitted %.1f" % (s, px)
    px *= 1.03                              # finer than the 6% rungs
ok(_bad is None, "labels: sweep 5px..1.5Mpx holds every contract (%s)" % _bad)
ok(len(_seen_rungs) <= 70,
   "labels: the whole sweep lands on few distinct blf sizes (%d rungs) — a "
   "zoom drag re-uses glyph caches instead of minting one per tick"
   % len(_seen_rungs))
d1, _ = picker._label_draw_size(100.0)
d2, _ = picker._label_draw_size(101.0)
ok(d1 == d2, "labels: neighbouring fitted sizes share a rung (%.3f)" % d1)
d3, s3 = picker._label_draw_size(1e6)
ok(d3 <= picker.MAX_LABEL_RASTER_PX and d3 * s3 == 1e6,
   "labels: the size that crashed Blender now rasterises at %.1fpx, GPU x%.0f"
   % (d3, s3))
d4, s4 = picker._label_draw_size(5.0)
ok(d4 == 5.0 and s4 == 1.0,
   "labels: the mush floor passes through exactly (%.2f)" % d4)
# determinism: same input, same rung, every time (the cache-reuse guarantee)
ok(picker._label_draw_size(87.3) == picker._label_draw_size(87.3),
   "labels: the ladder is deterministic")
# the draw path really uses the choke point — a plain constant in blf.size()
# would pass every check above while the callback kept crashing
_src = open(picker.__file__, encoding="utf-8").read()
_lbl = _src[_src.index("for btn, x0, y0, x1, y1, state, text, label_px in labels"):]
_lbl = _lbl[:_lbl.index("# keyframe pips")]
ok("_label_draw_size(label_px)" in _lbl and "blf.size(font_id, label_px)" not in _lbl,
   "labels: the label pass draws through _label_draw_size, never the raw fit")

# ------------------------------------------------- the walls are gone (0.34.0)
# Marty, 2026-08-10: "remove collision in Pickers buttons, they should not
# collide with each other". Job 16's clamp is deleted, not disabled — so the
# check is that the machinery is ABSENT, not that it lets things through.
for _dead in ("_collide_statics", "_collide_gap", "_clamp_axis", "_clamp_step",
              "_clamp_edge", "_grow_range"):
    ok(not hasattr(picker, _dead),
       "walls: %s is gone, not left dormant" % _dead)
# ⚠ The modal is the thing that has to have forgotten them. A leftover
# `self._statics` would be a clamp waiting to be wired back in by accident.
for _attr in ("_statics", "_movers", "_wall_gap", "_wall_delta"):
    ok(not hasattr(picker.MADI_OT_picker_session, _attr),
       "walls: the modal no longer carries %s" % _attr)
_drag = _src[_src.index("if self._mode == 'DRAG':"):]
_drag = _drag[:_drag.index("if self._mode == 'SCALE':")]
ok("dx = cx - self._start_canvas[0]" in _drag,
   "walls: a drag is the plain delta from the grab point now")
ok("Ctrl+Drag=move (they may overlap)" in _src,
   "walls: the cheat sheet no longer promises solid buttons")
# ⚠ ALIGN KEPT ITS OWN de-overlap — that one is an action the user asked for,
# not a wall appearing under the cursor. Removing it with the walls would have
# been the easy over-reach.
ok(hasattr(picker.MADI_OT_picker_align, "_spread")
   or "_spread" in _src, "walls: Align still spreads its run apart")

# ------------------------------------------- scale ONE button (0.34.0, job 4)
# Marty: "i just want a way to be able to scale group buttons individually
# because i can't do it now". Measured cause: with the member bones selected in
# the VIEWPORT the handle and every member button are all state 1, so the brush
# scales the lot; with the handle merely highlighted in the list it is state 0
# and the brush scales nothing.
_ctx = bpy.context
_arm_data = bpy.data.armatures.new("WallsA")
_arm = bpy.data.objects.new("WallsRig", _arm_data)
_ctx.scene.collection.objects.link(_arm)
_ctx.view_layer.objects.active = _arm
bpy.ops.object.mode_set(mode='EDIT')
for _i, _n in enumerate(("w1", "w2")):
    _eb = _arm_data.edit_bones.new(_n)
    _eb.head = (_i, 0, 0)
    _eb.tail = (_i, 0, 1)
bpy.ops.object.mode_set(mode='POSE')
picker._ensure_tabs(_ctx)
_uid = picker._active_uid(_ctx)
picker._active_tab(_ctx).armature = _arm
_coll = _arm.madi_picker.buttons


def _mkbtn(kind, label, bone="", members=()):
    b = _coll.add()
    b.kind, b.tab_uid, b.label = kind, _uid, label
    b.x = b.y = 0.5
    b.w = b.h = 0.05
    b.scale = 1.0
    b.w0, b.h0, b.scale0 = b.w, b.h, b.scale
    if bone:
        b.bone = bone
    for m in members:
        b.members.add().bone = m
    return b


_b1 = _mkbtn('BONE', "w1", bone="w1")
_grp = _mkbtn('GROUP', "G1", members=("w1", "w2"))
_gi = list(_coll).index(_grp)

# the state that made the brush useless, pinned so it can't quietly change
picker._select_bones(_arm, ["w1", "w2"], False)
_sel = picker._selected_bone_names(_arm)
_claimed = picker._claimed_bones(_ctx, _arm)
ok(picker._btn_sel_state(_grp, _sel, _claimed) == 1
   and picker._btn_sel_state(_b1, _sel, _claimed) == 1,
   "scale-one: bones picked in the VIEWPORT select the handle AND its member "
   "button together — which is why the brush could not scale a group alone")
_before = (_b1.scale, _grp.scale)
picker._scale_selected(_ctx, 2.0, 1.0)
ok(_b1.scale != _before[0] and _grp.scale != _before[1],
   "scale-one: ...and the brush duly moves both (the behaviour being worked "
   "around, not a bug)")

_arm.madi_picker.active_index = _gi
# ⚠ `==`, NOT `is`. Blender hands back a FRESH python wrapper on every
# collection access, so an identity test fails on the very same button —
# bpy_struct implements __eq__ against the underlying pointer, `is` cannot.
ok(picker._list_active_button(_ctx, _arm) == _grp,
   "scale-one: the list's active row resolves to that button")
_was = _grp.scale
_grp.scale = _was * 3.0
ok(abs(_grp.scale - _was * 3.0) < 1e-5 and _b1.scale == 2.0,
   "scale-one: writing it moves the GROUP and nothing else")

# ⚠ the tab guard — active_index addresses the WHOLE collection
_tab2 = _ctx.scene.madi_picker_tabs.add()
_tab2.uid = _uid + 1
_tab2.armature = _arm
_ctx.scene.madi_picker_tab_index = len(_ctx.scene.madi_picker_tabs) - 1
ok(picker._list_active_button(_ctx, _arm) is None,
   "scale-one: ⚠ a row belonging to ANOTHER tab is refused — active_index "
   "survives a tab switch, so without this the panel would resize a button "
   "the user cannot see")
_ctx.scene.madi_picker_tab_index = 0
ok(picker._list_active_button(_ctx, None) is None,
   "scale-one: no armature, no active button")
_arm.madi_picker.active_index = 999
ok(picker._list_active_button(_ctx, _arm) is None,
   "scale-one: an out-of-range index is refused, not raised on")
_arm.madi_picker.active_index = _gi

# ------------------------------------- bones & extras toggle (0.34.0, job 5)
ok("viewport_overlays" in dir(bpy.ops.madi_picker),
   "overlays: the operator is reachable")
_ov = bpy.types.View3DOverlay.bl_rna.properties
ok("show_bones" in _ov and "show_extras" in _ov,
   "overlays: both flags exist on View3DOverlay in this Blender (%s)"
   % bpy.app.version_string)
# ⚠ `blender -b` DOES have a window, with a VIEW_3D in it — so this is the real
# path, not a refusal stub. (Assumed otherwise when this suite was written; the
# probe said one window, areas PROPERTIES/OUTLINER/DOPESHEET/VIEW_3D.)
_spaces = picker._view3d_spaces(_ctx)
ok(len(_spaces) == 1,
   "overlays: background mode still has a 3D Viewport to act on (%d)"
   % len(_spaces))
for _s in _spaces:
    _s.overlay.show_bones = True
    _s.overlay.show_extras = True
ok(picker._overlays_on(_ctx) is True, "overlays: reads ON when both are on")
bpy.ops.madi_picker.viewport_overlays()
ok(all(not s.overlay.show_bones and not s.overlay.show_extras
       for s in _spaces),
   "overlays: one press hides BOTH bones and extras")
ok(picker._overlays_on(_ctx) is False, "overlays: ...and now reads OFF")
bpy.ops.madi_picker.viewport_overlays()
ok(all(s.overlay.show_bones and s.overlay.show_extras for s in _spaces),
   "overlays: pressing again brings both back")
# ⚠ THE MIXED CASE. One flag on, one off is not "on" — a per-flag flip would
# leave it just as mixed and the button would look broken. One press must
# settle every viewport into the same state.
_spaces[0].overlay.show_bones = False
ok(picker._overlays_on(_ctx) is False,
   "overlays: half-on reads OFF, so the next press turns everything ON")
bpy.ops.madi_picker.viewport_overlays()
ok(all(s.overlay.show_bones and s.overlay.show_extras for s in _spaces),
   "overlays: ...and it does, instead of flipping the mix over")
ok("madi_picker.viewport_overlays" in _src
   and 'text="Bones & Extras: %s"' in _src,
   "overlays: the panel draws it, labelled with the state it is in")

print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
for f in FAIL:
    print("  FAILED: " + f)
sys.exit(1 if FAIL else 0)
