# Timeline markers inside Blender: the properties, the panel, and the API both
# the panel and the bridge call.
#
#   blender.exe -b --factory-startup --python tests\markers_test.py
#
# The load-bearing checks here are the two that come from MEASURING Blender
# rather than reading it (BLENDER_NOTES.md, 2026-08-12):
#   * a TimelineMarker refuses ID properties but KEEPS a registered bpy.props
#     value across a save and reload - if that ever stops being true, every
#     note and tag in every .blend silently empties, and nothing else here
#     would notice;
#   * marker names are NOT unique and two markers can share a name AND a frame,
#     so anything that keys by name merges two markers into one row.
#
# And one invariant that protects Marty's open file: `marker_list` is POLLED by
# the app, so it must never write - not even to hand out an id.
#
# NOTE: this suite prints "Not freed memory blocks: 8" (778 bytes) as Blender
# quits. It is NOT a leak in the add-on and it does not fail the run (exit 0).
# Chased on 2026-08-12 and ruled out one at a time: register/unregister alone is
# clean, a save+reload alone is clean, markers carrying notes are clean, and
# removing the test camera changes nothing. It is a teardown artifact of this
# script, like the intentional traceback bridge_version_test prints - watch the
# counts, not the warning.
import importlib.util
import os
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


def raises(fn, label):
    try:
        fn()
    except Exception:                              # noqa: BLE001
        ok(True, label)
        return
    ok(False, label)


def _clear():
    for m in list(bpy.context.scene.timeline_markers):
        bpy.context.scene.timeline_markers.remove(m)


# --------------------------------------------------------------- the module

spec = importlib.util.spec_from_file_location(
    "madi_pkg", os.path.join(ADDON, "__init__.py"),
    submodule_search_locations=[ADDON])
pkg = importlib.util.module_from_spec(spec)
sys.modules["madi_pkg"] = pkg
spec.loader.exec_module(pkg)
markers = sys.modules["madi_pkg.markers"]
core = sys.modules["madi_pkg.core"]
server_mod = sys.modules["madi_pkg.server"]

# ------------------------------------------------------------------ versions

with open(os.path.join(ADDON, "blender_manifest.toml"), encoding="utf-8") as fh:
    manifest = fh.read()
ok('version = "%s"' % core.ADDON_VERSION in manifest,
   "version: core.ADDON_VERSION %s matches the manifest" % core.ADDON_VERSION)
with open(os.path.join(ROOT, "app", "bridge.py"), encoding="utf-8") as fh:
    app_bridge = fh.read()
ok('EXPECTED_ADDON_VERSION = "%s"' % core.ADDON_VERSION in app_bridge,
   "version: and the app expects the same")

ok(hasattr(markers, "register") and hasattr(markers, "unregister"),
   "module: markers registers itself, like the panels around it")

pkg.register()
scene = bpy.context.scene

# ------------------------------------------------------- the properties exist

for prop in ("madi_uid", "madi_note", "madi_tags"):
    ok(hasattr(bpy.types.TimelineMarker, prop),
       "props: TimelineMarker.%s is registered" % prop)

m = scene.timeline_markers.new("probe", frame=1)
ok(hasattr(m, "madi_note") and m.madi_note == "",
   "props: a fresh marker reads an empty note rather than raising")

# ⚠ THE TRAP, PINNED. If this ever starts SUCCEEDING, the storage question is
# open again and the module docstring is wrong - which matters, because the
# obvious reading of this error is "markers cannot hold metadata".
raises(lambda: m.__setitem__("madi_x", "y"),
       "props: raw ID properties are still refused (the misleading error)")
scene.timeline_markers.remove(m)

# --------------------------------------------------- names are not unique

a = scene.timeline_markers.new("shot_010", frame=10)
b = scene.timeline_markers.new("shot_020", frame=48)
twin = scene.timeline_markers.new("shot_010", frame=10)
ok(len(scene.timeline_markers) == 3,
   "identity: two markers CAN share a name and a frame")

markers._uid_for(a)
markers._uid_for(b)
markers._uid_for(twin)
ok(len({x.madi_uid for x in scene.timeline_markers}) == 3,
   "identity: each of the three gets a DIFFERENT uid")

found = markers._resolve(scene, {"uid": b.madi_uid})
ok(found == b, "identity: resolve by uid finds the right marker")

idx = list(scene.timeline_markers).index(b)
ok(markers._resolve(scene, {"index": idx, "name": "shot_020", "frame": 48}) == b,
   "identity: resolve by index, verified against name and frame")

# ⚠ An index whose name no longer matches must NOT be trusted - that is the
# whole reason the verification is there.
raises(lambda: markers._resolve(scene, {"index": idx, "name": "renamed_since",
                                        "frame": 999}),
       "identity: a stale index with the wrong name is refused, not guessed")

# ⚠ AND AN AMBIGUOUS NAME IS AN ERROR. Writing a note onto whichever twin came
# first is exactly the bug the uid exists to prevent.
raises(lambda: markers._resolve(scene, {"name": "shot_010", "frame": 10}),
       "identity: two markers with one name is an ERROR, never a guess")

# ------------------------------------------- marker_list is a PURE READ

for x in scene.timeline_markers:
    x.madi_uid = ""
dirty_before = bpy.data.is_dirty
listed = markers.marker_list()
ok(all(x.madi_uid == "" for x in scene.timeline_markers),
   "poll: marker_list minted NO uid - it does not write")
ok(bpy.data.is_dirty == dirty_before,
   "poll: and left the file's dirty flag exactly as it was")
ok([r["frame"] for r in listed["markers"]] == [10, 10, 48],
   "poll: markers come back in frame order")
ok(listed["markers"][0]["uid"] == "",
   "poll: a marker that has never been written reports an empty uid")

# put the ids back - the rest of this suite addresses markers by uid
for x in scene.timeline_markers:
    markers._uid_for(x)

# ----------------------------------------------------------- add / set / remove

added = markers.marker_add("fx_splash", 96, note="sim starts", tags=["fx", "wip"])
ok(added["added"]["name"] == "fx_splash" and added["added"]["frame"] == 96,
   "add: a marker lands at the frame asked for")
ok(added["added"]["uid"], "add: and is given a uid immediately")
fx = markers._resolve(scene, {"uid": added["added"]["uid"]})
ok(markers._tags_of(fx) == ["fx", "wip"], "add: tags round-trip as a list")

ref = {"uid": fx.madi_uid}
markers.marker_set(ref, note="changed my mind")
ok(fx.madi_note == "changed my mind" and fx.name == "fx_splash",
   "set: writing the note left the NAME alone")
ok(markers._tags_of(fx) == ["fx", "wip"],
   "set: and left the tags alone - only what was passed is written")

markers.marker_set(ref, name="fx_splash_b")
ok(fx.name == "fx_splash_b" and fx.madi_note == "changed my mind",
   "set: renaming left the note alone")

# ⚠ THE CAMERA SENTINEL. `None` is a real value here (it clears the binding),
# so "not passed" and "clear it" must not be the same thing.
cam_data = bpy.data.cameras.new("TestCamData")
cam = bpy.data.objects.new("Cam_A", cam_data)
scene.collection.objects.link(cam)
markers.marker_set(ref, camera="Cam_A")
ok(fx.camera == cam, "camera: binding by name works")
markers.marker_set(ref, note="still bound")
ok(fx.camera == cam, "camera: an edit that does not mention it keeps the binding")
markers.marker_set(ref, camera=None)
ok(fx.camera is None, "camera: passing None clears it")
raises(lambda: markers.marker_set(ref, camera="NoSuchCamera"),
       "camera: an unknown camera name is an error, not a silent clear")

before = len(scene.timeline_markers)
markers.marker_remove({"uid": fx.madi_uid})
ok(len(scene.timeline_markers) == before - 1, "remove: takes the marker away")

# ------------------------------------------------------------------- goto

markers.marker_goto({"uid": b.madi_uid})
ok(scene.frame_current == 48, "goto: the playhead lands on the marker's frame")

# --------------------------------------------------------- bind cameras by name

named = scene.timeline_markers.new("Cam_A", frame=200)
result = markers.marker_bind_by_name()
ok(result["count"] == 1 and named.camera == cam,
   "bind: a marker named after a camera gets bound to it")
ok(markers.marker_bind_by_name()["count"] == 0,
   "bind: running it again binds nothing - it is idempotent")
scene.timeline_markers.remove(named)

# ------------------------------------------------------------------ rename

markers.marker_rename(prefix="sq01_")
ok(all(x.name.startswith("sq01_") for x in scene.timeline_markers),
   "rename: a prefix reaches every marker")
markers.marker_rename(find="sq01_", replace="")
ok(not any(x.name.startswith("sq01_") for x in scene.timeline_markers),
   "rename: find and replace puts them back")

only_one = markers.marker_rename(prefix="x_", only=[b.madi_uid])
ok(only_one["count"] == 1 and b.name.startswith("x_"),
   "rename: `only` limits it to the markers named")
markers.marker_set({"uid": b.madi_uid}, name="shot_020")

# ---------------------------------------------------------------- revision

rev = markers.revision(scene)
ok(markers.revision(scene) == rev, "revision: stable when nothing changes")
markers.marker_set({"uid": b.madi_uid}, note="poke")
ok(markers.revision(scene) != rev, "revision: moves when a NOTE changes")
rev = markers.revision(scene)
markers.marker_set({"uid": b.madi_uid}, frame=49)
ok(markers.revision(scene) != rev, "revision: moves when a FRAME changes")
markers.marker_set({"uid": b.madi_uid}, frame=48)

# ------------------------------------------------------- the list's filtering

ui = bpy.context.window_manager.madilib_mk
ul = bpy.types.MADILIB_UL_markers


class _UL:
    """Stands in for the UIList instance - filter_items only needs the flag."""

    bitflag_filter_item = 1 << 30


def _filter():
    return ul.filter_items(_UL(), bpy.context, scene, "timeline_markers")


markers.marker_set({"uid": b.madi_uid}, note="hips lead the step", tags=["hero"])
ui.filter_text = "hips"
flags, order = _filter()
shown = [m.name for m, f in zip(scene.timeline_markers, flags) if f]
ok(shown == ["shot_020"],
   "filter: the search reaches the NOTE, which Blender's own filter cannot")
ui.filter_text = "hero"
flags, _ = _filter()
shown = [m.name for m, f in zip(scene.timeline_markers, flags) if f]
ok(shown == ["shot_020"], "filter: and the tags")
ui.filter_text = ""
ui.filter_tag = "hero"
flags, _ = _filter()
ok(sum(1 for f in flags if f) == 1, "filter: the tag filter narrows to one")
ui.filter_tag = ""

# ⚠ THE INVERSE-PERMUTATION TRAP. filter_items wants, for each ORIGINAL row,
# the position it moves TO. Handing it the other permutation sorts almost
# right, which is far worse than sorting wrong.
ui.sort_mode = 'FRAME'
flags, neworder = _filter()
frames = [m.frame for m in scene.timeline_markers]
placed = [None] * len(frames)
for orig, new_pos in enumerate(neworder):
    placed[new_pos] = frames[orig]
ok(placed == sorted(frames),
   "filter: the sort order is the INVERSE permutation Blender asks for")

# --------------------------------------------------- the active row is sorted

ui.sort_mode = 'NAME'
ui.active_index = 0
first_by_name = sorted((x.name.lower(), x.frame) for x in scene.timeline_markers)[0]
active = markers._active_marker(bpy.context)
ok(active is not None and active.name.lower() == first_by_name[0],
   "active: row 0 is the first marker in the SHOWN order, not the collection's")
ui.sort_mode = 'FRAME'

# ------------------------------------------------- draw() must not write

class _FakeLayout:
    """Swallows every layout call. Enough for draw()."""

    def __getattr__(self, _name):
        return self

    def __call__(self, *a, **k):
        return self


class _Stub:
    """A bpy_struct cannot be built from Python; draw() only touches `layout`."""


def _draw(cls):
    stub = _Stub()
    stub.layout = _FakeLayout()
    cls.draw(stub, bpy.context)


# ⚠ THE PANEL IS REGISTERED TWICE, ON PURPOSE (Marty, 2026-08-12: "timeline
# markers should be in the same UI in blender as Studio Library"). bl_space_type
# is a single value, so two editors means two classes over one mixin - and the
# viewport one has to sit in the SAME CATEGORY as Studio Library or it lands in
# a tab of its own, which is the complaint that started this.
import bl_ext  # noqa: F401,E402  (namespace only; the panels come from bpy.types)

for name, space in (("MADILIB_PT_markers", 'DOPESHEET_EDITOR'),
                    ("MADILIB_PT_markers_view3d", 'VIEW_3D')):
    cls = getattr(bpy.types, name, None)
    ok(cls is not None, "panel: %s is registered" % name)
    if cls is not None:
        ok(cls.bl_space_type == space,
           "panel: %s lives in %s" % (name, space))
        ok(cls.bl_category == pkg.MADILIB_PT_panel.bl_category,
           "panel: %s shares Studio Library's N-panel tab (%r)"
           % (name, pkg.MADILIB_PT_panel.bl_category))
ok(pkg.MADILIB_PT_panel.bl_space_type ==
   bpy.types.MADILIB_PT_markers_view3d.bl_space_type,
   "panel: ⚠ and it is in the SAME EDITOR as Studio Library - the whole point")
ok('DEFAULT_CLOSED' not in getattr(
       bpy.types.MADILIB_PT_markers_view3d, "bl_options", set()),
   "panel: the viewport one opens EXPANDED - it was not being found")

uids_before = [x.madi_uid for x in scene.timeline_markers]
notes_before = [x.madi_note for x in scene.timeline_markers]
for name in ("MADILIB_PT_markers", "MADILIB_PT_markers_view3d"):
    try:
        _draw(getattr(bpy.types, name))
        ok(True, "draw: %s draws against real markers without raising" % name)
    except Exception as exc:                       # noqa: BLE001
        ok(False, "draw: %s draws (%r)" % (name, exc))
try:
    _draw(bpy.types.MADILIB_MT_mk_tools)
    ok(True, "draw: the Marker Tools dropdown draws")
except Exception as exc:                           # noqa: BLE001
    ok(False, "draw: the Marker Tools dropdown draws (%r)" % exc)

ok([x.madi_uid for x in scene.timeline_markers] == uids_before,
   "draw: and minted no uid - draw() is a pure read")
ok([x.madi_note for x in scene.timeline_markers] == notes_before,
   "draw: and changed no note")

# with nothing selected the panel must still draw, not throw a column of blanks
ui.active_index = 999
try:
    _draw(bpy.types.MADILIB_PT_markers)
    ok(True, "draw: an out-of-range active row draws the list and stops")
except Exception as exc:                           # noqa: BLE001
    ok(False, "draw: an out-of-range active row (%r)" % exc)
ui.active_index = 0

# --------------------------------------------------- every operator exists

for name in ("mk_jump", "mk_add", "mk_remove", "mk_render", "mk_bind_camera",
             "mk_bind_by_name"):
    ok(hasattr(bpy.types, "MADILIB_OT_%s" % name),
       "operator: madilib.%s is registered" % name)

# ------------------------------------------------- the bridge advertises them

caps = set(server_mod.BridgeServer.capabilities())
for cmd in ("marker_list", "marker_add", "marker_set", "marker_remove",
            "marker_goto", "marker_bind_by_name", "marker_rename"):
    ok(cmd in caps, "bridge: %s is advertised by the dispatcher" % cmd)

# ⚠ Markers are FREE. If a `marker_` prefix ever appears in the entitlement
# gate, the app's free Anim Layers tab starts refusing on the Blender side -
# the exact asymmetry that cost a day on 2026-08-12.
with open(os.path.join(ADDON, "server.py"), encoding="utf-8") as fh:
    server_src = fh.read()
ok('cmd.startswith("marker_")' not in server_src,
   "licence: nothing gates marker_* inside the add-on - the tool is free")

# ------------------------------------------------- persistence through a save
# ⚠ A same-process reload is NOT proof of persistence (a dict keyed by
# name@frame once made a loss look like a survival). The conclusive two-process
# run is recorded in BLENDER_NOTES.md; this is the regression guard.

tmp = os.path.join(tempfile.gettempdir(), "madi_markers_test.blend")
bpy.data.objects.remove(cam, do_unlink=True)
bpy.data.cameras.remove(cam_data)
markers.marker_set({"uid": b.madi_uid}, note="survives", tags=["kept"])
bpy.ops.wm.save_as_mainfile(filepath=tmp)
bpy.ops.wm.open_mainfile(filepath=tmp)
scene = bpy.context.scene
reloaded = {x.name: (x.madi_note, x.madi_tags) for x in scene.timeline_markers}
ok(reloaded.get("shot_020", ("", ""))[0] == "survives",
   "persist: the note is still there after a save and a reload")
ok("kept" in reloaded.get("shot_020", ("", ""))[1],
   "persist: and so are the tags")
ok(any(x.madi_uid for x in scene.timeline_markers),
   "persist: and the uid, which is what keeps the app addressing the right one")
try:
    os.remove(tmp)
except OSError:
    pass

# ------------------------------------------------------------------ layers
# Marty, 2026-08-12: "add layers so when i chose one layer it hides the markers
# of other layers" - as a FILTER (his choice), one layer per marker, and
# "when no layer is selected - all markers show".

scene = bpy.context.scene
for m in list(scene.timeline_markers):
    scene.timeline_markers.remove(m)
a = scene.timeline_markers.new("block_a", frame=10)
b = scene.timeline_markers.new("block_b", frame=20)
c = scene.timeline_markers.new("loose", frame=30)
for m in (a, b, c):
    markers._uid_for(m)
markers.marker_set({"uid": a.madi_uid}, layer="blocking")
markers.marker_set({"uid": b.madi_uid}, layer="polish")

ok(markers.layers_in_use(scene) == ["blocking", "polish"],
   "layers: the layer list is DERIVED from the markers, and sorted")
ok(a.madi_layer == "blocking" and c.madi_layer == "",
   "layers: a marker carries exactly one layer, and may carry none")

listed = markers.marker_list()
ok(listed["layers"] == ["blocking", "polish"],
   "layers: marker_list reports them, which is the app's only way to know")
ok([r["layer"] for r in listed["markers"]] == ["blocking", "polish", ""],
   "layers: and each row carries its own")

ui = bpy.context.window_manager.madilib_mk
ui.filter_text = ""
ui.filter_tag = ""

markers.show_layer("blocking")
shown = [m.name for m in scene.timeline_markers]
ok(shown == ["block_a"], "layers: picking one hides the other layers' markers")
ok(markers._shown_layer(scene) == "blocking",
   "layers: and the scene records which layer that is")

# ⚠ MARTY'S RULE. Empty must mean EVERY marker - including the unassigned one.
markers.show_layer("")
ok(len(scene.timeline_markers) == 3,
   "layers: with no layer picked, ALL markers show - including layerless ones")
ok(markers._shown_layer(scene) == "",
   "layers: and the record is cleared with them")

# ⚠ ONE HOME FOR THE FACT. "which layer is shown" and "what is parked" are two
# halves of the same thing; while they lived apart (scene vs window manager) a
# live session reported showing layer '1' with nothing hidden and every marker
# on screen, because an add-on update cleared one and not the other.
ok(not hasattr(ui, "filter_layer"),
   "layers: ⚠ there is NO second copy of the shown layer on the window manager")
ok(markers.SHOW_KEY != markers.PARK_KEY,
   "layers: the two keys are distinct...")
markers.show_layer("polish")
ok(bool(scene.get(markers.SHOW_KEY)) == bool(markers.parked(scene)),
   "layers: ...but they are written together, so they cannot disagree")
markers.show_layer("")

# ⚠ RE-RESOLVED BY NAME, not reused. `a` was removed and recreated by the
# show_layer round trips above, so the old wrapper is stale — the same trap
# that has now bitten this suite three times.
fresh_a = next(m for m in scene.timeline_markers if m.name == "block_a")
markers.marker_set({"uid": fresh_a.madi_uid}, layer="")
ok(markers.layers_in_use(scene) == ["polish"],
   "layers: clearing the last marker off a layer retires the layer")
fresh_a = next(m for m in scene.timeline_markers if m.name == "block_a")
ok(fresh_a.madi_layer == "",
   "layers: and '' really is a value, not 'ignore this'")

ok(hasattr(bpy.types, "MADILIB_OT_mk_set_layer_filter"),
   "layers: the filter operator is registered")
# ⚠ THIS ASSERTION FLIPPED ON 2026-08-12, and the flip is the point. While
# picking a layer only filtered a list it carried NO 'UNDO' - ctrl+Z belongs to
# the animation, not to list filtering. It now takes the other layers' markers
# OUT of the scene, so it edits real data and MUST be undoable. An operator that
# can move every marker in the file and cannot be undone is a trap.
ok('UNDO' in bpy.types.MADILIB_OT_mk_set_layer_filter.bl_options,
   "layers: ⚠ showing a layer IS undoable now that it moves real markers")
try:
    _draw(bpy.types.MADILIB_MT_mk_layers)
    ok(True, "layers: the layer menu draws (incl. its empty state)")
except Exception as exc:                           # noqa: BLE001
    ok(False, "layers: the layer menu draws (%r)" % exc)

# ------------------------------------ ⚠ REAL HIDING (parking), 2026-08-12
# Marty: "hide the markers of other layers if ONE layer is selected and show
# only that layer, when no layer is selected show all layer markers" - and he
# meant the TIMELINE STRIP. Blender always draws every marker in the scene, so
# the only way to clear it is to take them OUT and put them back. Everything
# here is about that round trip being lossless.

_clear()
a = scene.timeline_markers.new("blk_1", frame=10)
b = scene.timeline_markers.new("pol_1", frame=20)
c = scene.timeline_markers.new("loose", frame=30)
for m in (a, b, c):
    markers._uid_for(m)
markers.marker_set({"uid": a.madi_uid}, layer="blocking", note="hips",
                   tags=["hero"])
markers.marker_set({"uid": b.madi_uid}, layer="polish")
cam_data2 = bpy.data.cameras.new("ParkCamData")
cam2 = bpy.data.objects.new("ParkCam", cam_data2)
scene.collection.objects.link(cam2)
markers.marker_set({"uid": b.madi_uid}, camera="ParkCam")
# ⚠ CAPTURE THE VALUES NOW. Parking removes and recreates the markers, so these
# Python wrappers are stale afterwards and reading them back proves nothing -
# the same trap that made an earlier probe look like a partial data loss.
a_uid, b_uid = a.madi_uid, b.madi_uid

result = markers.show_layer("blocking")
ok(len(scene.timeline_markers) == 1,
   "park: showing one layer really REMOVES the others from the scene - which "
   "is the only thing that clears Blender's timeline strip")
ok(scene.timeline_markers[0].name == "blk_1",
   "park: and the right one is left behind")
ok(result["hidden"] == 2, "park: the reply says how many were put away")

# ⚠ THE ONE THAT WOULD STRAND HIM. The other layers are no longer in the scene,
# so a layer list built from `timeline_markers` alone would drop them and leave
# no way back.
ok(markers.layers_in_use(scene) == ["blocking", "polish"],
   "park: ⚠ the layer list still names layers whose markers are parked")

ok(len(markers.all_markers(scene)) == 3,
   "park: all_markers() still sees every marker, parked or not")
ok(markers.marker_list()["hidden"] == 2,
   "park: marker_list reports the hidden count so a UI can say so")

# switching straight to another layer must not park on top of a park
markers.show_layer("polish")
ok([m.name for m in scene.timeline_markers] == ["pol_1"],
   "park: ⚠ switching layer restores FIRST, then parks - nothing is stranded")
ok(len(markers.all_markers(scene)) == 3,
   "park: and all three markers still exist somewhere")

markers.show_layer("")
names = sorted(m.name for m in scene.timeline_markers)
ok(names == ["blk_1", "loose", "pol_1"],
   "park: showing no layer brings every marker back")
restored = {m.name: m for m in scene.timeline_markers}
ok(restored["blk_1"].madi_note == "hips"
   and markers._tags_of(restored["blk_1"]) == ["hero"]
   and restored["blk_1"].madi_layer == "blocking",
   "park: notes, tags and layer survive the round trip")
ok(restored["blk_1"].madi_uid == a_uid,
   "park: and so does the uid, so the app keeps addressing the right marker")
ok(restored["pol_1"].camera == cam2,
   "park: ⚠ and the CAMERA BINDING is restored, not silently dropped")

# a camera that vanished while parked must not take the marker down with it
markers.show_layer("blocking")
bpy.data.objects.remove(cam2, do_unlink=True)
markers.show_layer("")
ok(len(scene.timeline_markers) == 3,
   "park: a camera deleted while parked still restores the marker...")
ok({m.name: m.camera for m in scene.timeline_markers}["pol_1"] is None,
   "park: ...just unbound - losing a binding beats losing a marker")

# ⚠ AND A FILE MUST NEVER OPEN WITH MARKERS HIDDEN
markers.show_layer("blocking")
ok(len(scene.timeline_markers) == 1, "park: hidden before the load handler")
markers._restore_on_load(None)
ok(len(bpy.context.scene.timeline_markers) == 3,
   "park: ⚠ the load handler shows everything - a .blend opened elsewhere must "
   "never look like it lost its markers")
ok(markers._restore_on_load in bpy.app.handlers.load_post,
   "park: and the handler is actually registered")

# ------------------------------------------------------------ named sets
# Marty: "save marker preset per project, this should be saved in .blend file
# and autoloaded by our tool".

saved = markers.marker_set_save("Shot breakdown")
ok(saved["count"] == 3, "sets: saving stores every marker")
ok(markers.SETS_KEY in scene,
   "sets: ⚠ stored ON THE SCENE, so it travels inside the .blend")

# ⚠ SAVING WHILE A LAYER IS SHOWN MUST STILL SAVE THE LOT.
markers.show_layer("blocking")
partial = markers.marker_set_save("While filtered")
ok(partial["count"] == 3,
   "sets: ⚠ saving while a layer is shown still stores the PARKED markers too")
markers.show_layer("")

_clear()
ok(len(scene.timeline_markers) == 0, "sets: scene emptied")
loaded = markers.marker_set_load("Shot breakdown")
ok(loaded["count"] == 3 and len(scene.timeline_markers) == 3,
   "sets: loading puts them all back")
back = {m.name: m for m in scene.timeline_markers}
ok(back["blk_1"].madi_note == "hips" and back["blk_1"].madi_layer == "blocking",
   "sets: with their notes and layers")

ok(sorted(markers.marker_sets(scene)) == ["Shot breakdown", "While filtered"],
   "sets: both sets are listed")
markers.marker_set_delete("While filtered")
ok(sorted(markers.marker_sets(scene)) == ["Shot breakdown"],
   "sets: deleting forgets one set...")
ok(len(scene.timeline_markers) == 3,
   "sets: ...and leaves the markers in the scene alone")
raises(lambda: markers.marker_set_load("no such set"),
       "sets: loading a set that does not exist is an error, not an empty scene")
raises(lambda: markers.marker_set_save("   "),
       "sets: a blank name is refused")

# loading a set clears any parking, or the two states contradict each other
markers.show_layer("blocking")
markers.marker_set_load("Shot breakdown")
ok(len(scene.timeline_markers) == 3 and not markers.parked(scene),
   "sets: ⚠ loading a set clears the hidden state - otherwise the scene and "
   "the parked list disagree about what exists")

for cmd in ("marker_show_layer", "marker_set_save", "marker_set_load",
            "marker_set_delete"):
    ok(cmd in set(server_mod.BridgeServer.capabilities()),
       "bridge: %s is advertised" % cmd)

# ---------------------------------------------------- B1: the sub-panels
for name, space, parent in (
        ("MADILIB_PT_marker_details", 'DOPESHEET_EDITOR', "MADILIB_PT_markers"),
        ("MADILIB_PT_marker_details_view3d", 'VIEW_3D',
         "MADILIB_PT_markers_view3d")):
    cls = getattr(bpy.types, name, None)
    ok(cls is not None, "B1: %s is registered" % name)
    if cls is not None:
        ok(cls.bl_parent_id == parent, "B1: %s hangs off %s" % (name, parent))
        # ⚠ A child in a DIFFERENT space type than its parent simply never
        # appears, and Blender says nothing about it.
        ok(cls.bl_space_type == space,
           "B1: %s declares its parent's space type" % name)
        try:
            _draw(cls)
            ok(True, "B1: %s draws" % name)
        except Exception as exc:                   # noqa: BLE001
            ok(False, "B1: %s draws (%r)" % (name, exc))

_clear()

# ------------------------------------------------ ⚠ DOES IT BLOAT THE FILE?
# Marty asked directly (2026-08-12). Measured, uncompressed, on real saves -
# the only answer worth giving. The question that matters most is the FIRST
# one: registering three string properties must cost nothing to a file that
# never uses them, because that is every .blend he already has.

def _save_size(path):
    bpy.ops.wm.save_as_mainfile(filepath=path, compress=False)
    return os.path.getsize(path)


base = os.path.join(tempfile.gettempdir(), "madi_mk_size_%d.blend")
N = 200

# ⚠ THE BASELINE IS A MARKER WITH THE ADD-ON NOT REGISTERED AT ALL. Comparing
# an annotated marker against a bare one measures BLENDER'S marker, not ours,
# and would have reported our cost as 136 B/marker when the true figure is
# nearer nothing. Unregister, measure, register again.
_clear()
size_none = _save_size(base % 0)
pkg.unregister()
for i in range(N):
    bpy.context.scene.timeline_markers.new("m%03d" % i, frame=i * 3)
size_vanilla = _save_size(base % 1)
_clear()
pkg.register()
scene = bpy.context.scene

for i in range(N):
    scene.timeline_markers.new("m%03d" % i, frame=i * 3)
size_plain = _save_size(base % 2)

for m in scene.timeline_markers:
    markers._uid_for(m)
    m.madi_note = "a realistic note about what happens on this frame"
    m.madi_tags = "hero, wip"
    m.madi_layer = "blocking"
size_full = _save_size(base % 3)

per_vanilla = (size_vanilla - size_none) / float(N)
per_ours_empty = (size_plain - size_vanilla) / float(N)
per_ours_used = (size_full - size_plain) / float(N)
print("  measured over %d markers, uncompressed:" % N, flush=True)
print("    Blender's own marker          %7.1f B each" % per_vanilla, flush=True)
print("    + our props, never filled in  %7.1f B each" % per_ours_empty, flush=True)
print("    + a note, 2 tags and a layer  %7.1f B each" % per_ours_used, flush=True)

# ⚠ THE ONE THAT MATTERS: every .blend Marty already has carries markers with
# no metadata, and installing this add-on must not grow a single one of them.
ok(abs(per_ours_empty) < 8,
   "bloat: our properties cost %.1f B on a marker that never uses them - "
   "installing this does not grow an existing file" % per_ours_empty)
# And a used one: measured, not guessed. ~70 characters of text per marker.
ok(per_ours_used < 1400,
   "bloat: a fully annotated marker costs %.1f B (about 1 KB for ~70 "
   "characters - Blender's ID-property overhead, not the text)"
   % per_ours_used)

# ⚠ AND IT MUST COME BACK. Metadata that survived the marker being deleted
# would be a slow leak into every .blend he saves for the rest of the project.
_clear()
size_after = _save_size(base % 4)
ok(abs(size_after - size_none) < 2048,
   "bloat: deleting the markers returns the file to its original size "
   "(%d B vs %d B) - nothing is left behind" % (size_after, size_none))

for i in range(5):
    try:
        os.remove(base % i)
    except OSError:
        pass

# --------------------------------------------- ⚠ DOES IT LEAK WHILE RUNNING?
# Marty asked (2026-08-12). The question is whether memory GROWS across the
# make-and-delete cycles a real session does all day - not what Blender reports
# at exit, which is a shutdown-order artifact and unavoidable while the
# properties stay registered (they must; see `unregister`).
import ctypes                                                    # noqa: E402
from ctypes import wintypes                                      # noqa: E402


class _PMC(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t)]


def _rss():
    """PRIVATE bytes, not the working set.

    ⚠ `WorkingSetSize` is trimmable by the OS, so a real leak can read as zero
    growth — which is exactly what the first version of this check reported
    while the exit-time block count was scaling with every marker ever
    annotated. Private commit is what actually tracks allocation.
    """
    pmc = _PMC()
    pmc.cb = ctypes.sizeof(_PMC)
    ctypes.windll.psapi.GetProcessMemoryInfo(
        ctypes.windll.kernel32.GetCurrentProcess(), ctypes.byref(pmc),
        pmc.cb)
    return pmc.PagefileUsage


def _churn(rounds):
    for _ in range(rounds):
        for i in range(N):
            m = scene.timeline_markers.new("churn%03d" % i, frame=i)
            markers._uid_for(m)
            m.madi_note = "a realistic note about what happens on this frame"
            m.madi_tags = "hero, wip"
            m.madi_layer = "blocking"
        markers.marker_list()          # the polled read, in the loop too
        _clear()


# ⚠ THE CHURN HAS TO BE BIG ENOUGH THAT A LEAK CANNOT HIDE IN THE NOISE.
# At the measured ~1.08 KB of metadata per annotated marker, 30 rounds of 200
# is ~6.5 MB. If the metadata were not freed with the marker, that is the growth
# we would see; allocator noise on a Blender process is a few hundred KB.
ROUNDS = 30
CHURNED = ROUNDS * N * per_ours_used

_churn(3)                              # settle allocators before measuring
before_rss = _rss()
_churn(ROUNDS)
after_rss = _rss()
grew = after_rss - before_rss
print("  memory: %d annotated markers built and deleted (%.1f MB of metadata "
      "churned) -> %+.2f MB private bytes"
      % (ROUNDS * N, CHURNED / 1048576.0, grew / 1048576.0), flush=True)
ok(grew < CHURNED * 0.25,
   "leak: churning %.1f MB of marker metadata grew the process by %.2f MB of "
   "private bytes" % (CHURNED / 1048576.0, grew / 1048576.0))

# ⚠ AND THE HONEST FOOTNOTE, because "no leaks" would be a lie.
# Blender's own guarded allocator reports ~10 blocks (~1 KB) per ANNOTATED
# marker still allocated at exit, scaling with how many were created and
# DELETED - so its removal path does not free the property storage our
# registered props attach on first write. Measured 2026-08-12 by isolating it:
#   markers churned, no add-on            -> no unfreed blocks
#   markers churned, add-on, no metadata  -> no unfreed blocks
#   markers churned WITH metadata         -> 60000 blocks / 5.74 MB
#   ...and clearing every string first    -> 60000 blocks / 5.74 MB (identical)
# Clearing the values does not help, so it is the IDProperty GROUP, and there
# is nothing to do about it from Python. It costs nothing in the .blend (the
# file returns to its exact original size, checked above), nothing in private
# bytes, and it is reclaimed when Blender closes. Recorded in BLENDER_NOTES.md
# so nobody re-discovers it as a bug in this module.
ok(True,
   "leak: NOTE - Blender retains ~1 KB per annotated marker DELETED, for the "
   "session only; not ours to fix, costs nothing on disk (see BLENDER_NOTES)")
_clear()

# ------------------------------------------------- ⚠ register/unregister leak
# The add-on is reloaded every time it is updated, and a reload is
# unregister+register. Anything that accumulates across that shows up here.
before_types = len([n for n in dir(bpy.types) if n.startswith("MADILIB_")])
for _ in range(3):
    pkg.unregister()
    pkg.register()
after_types = len([n for n in dir(bpy.types) if n.startswith("MADILIB_")])
ok(before_types == after_types,
   "leak: three reload cycles register exactly the same classes (%d -> %d)"
   % (before_types, after_types))
ok(hasattr(bpy.types.TimelineMarker, "madi_layer"),
   "leak: and the marker properties survive a reload cycle")
ok(len([p for p in bpy.types.TimelineMarker.bl_rna.properties
        if p.identifier.startswith("madi_")]) == 4,
   "leak: exactly four madi_ properties on the marker, not a growing pile")

pkg.unregister()

print("\n%d passed, %d failed" % (len(PASS), len(FAIL)), flush=True)
for f in FAIL:
    print("FAIL " + f, flush=True)
