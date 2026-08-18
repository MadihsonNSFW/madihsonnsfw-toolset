"""Organize — object SETS saved in the .blend, and a one-press Isolate.

A set is a named bag of objects: a rig with its meshes, the lights for a shot,
the props on a table. Pick one and **Isolate** puts the viewport into LOCAL
VIEW holding just that set — the same thing as selecting those objects and
pressing `/`. Press again to come back out.

The app's Organize tab and the N-panel here are two windows onto the SAME
bytes — `Scene.madi_sets`. Neither tells the other anything: Blender redraws
its panel constantly, and the app polls `sets_list` and compares `revision`.
That is the Timeline Markers design, for the same reason (`markers.py`).

⚠ **`sets_list` IS A PURE READ AND MUST STAY ONE.** The app polls it while the
tab is open. Writing anything from it — including lazily handing out a uid —
would mark Marty's file dirty just because the app is running. Uids are
assigned when a set is CREATED, which is already a write.

⚠ **A NAME IS NOT A KEY.** Two sets may be called "Props", and a set may be
renamed between two polls. Everything addresses a set by `uid`.

⚠ **MEMBERS ARE `PointerProperty(type=Object)`, NOT NAMES** — a pointer
follows the object through a rename, which is exactly what Marty asked for
("tied to .blend file even if .blend file is renamed later" applies to the
objects too). Measured 2026-08-18: the pointer adds a user, the object is
still deletable, and the member then reads `None` — which is reported as
`missing` and cleaned on request, never silently dropped. One side effect
worth knowing: an object unlinked from every collection but still named by a
set survives Purge Orphans, because the set holds a user.

===========================================================================
⚠⚠ ISOLATE IS BLENDER'S LOCAL VIEW (the `/` key) — NOT HIDING
===========================================================================
Marty, 2026-08-18, on the first version: *"when saying 'isolate' i mean like
this (local), as in when you select multiple items and press /"*.

That first version hid every non-member (view-layer eyes plus collection
flags) and kept a JSON snapshot so it could put the scene back. It worked,
and it was the wrong feature: it changed the file's visibility state, it
showed in renders, and being exactly reversible needed a pile of machinery —
most of it to survive EXCLUDED COLLECTIONS, where `hide_get()` lies and an
exclude/include cycle destroys the per-object eye. **All of that is deleted.**
Local view is a property of the VIEWPORT: nothing about the objects changes,
so there is nothing to snapshot and leaving it is exact by construction.

Two consequences worth knowing before changing anything here:
  * ⚠ **`local_view_set` needs `bpy.context.view_layer.update()`** before the
    change is visible to anything, including a read-back (measured).
  * ⚠ **Entering goes through the SELECTION**, because that is what
    `view3d.localview` acts on — so isolating a set selects it, exactly as
    pressing `/` on those objects would have.

The excluded-collection findings still matter in ONE place: `select_set`
RAISES for an object that is not in the view layer, so `_blind_objects` is
kept for Select. `BLENDER_NOTES.md` has the full write-up.
"""

import json
import uuid

import bpy
from bpy.props import (BoolProperty, CollectionProperty, IntProperty,
                       PointerProperty, StringProperty)
from bpy.types import Operator, Panel, PropertyGroup, UIList

# Longest name we will store. The app caps its own field; this is the backstop
# for anything else that reaches the socket (`docs\\security.md`).
NAME_MAX = 80
# How many sets one scene may hold. Not a licence limit — a guard against a
# runaway caller filling a .blend over the bridge.
SETS_MAX = 512

# Snapshot bit masks. Absent from the dict == 0 == "nothing hidden", so a
# tidy scene stores almost nothing and a 10k-object scene stays small.
OB_HIDE = 1          # hide_get()  — the per-view-layer eye
OB_VIEWPORT = 2      # hide_viewport — the monitor icon (object data)
LC_EXCLUDE = 1       # LayerCollection.exclude — the checkbox
LC_HIDE = 2          # LayerCollection.hide_viewport — the eye on the row
LC_COLL_HIDE = 4     # Collection.hide_viewport — the monitor icon


# =========================================================== data =========
class MADILIB_SetMember(PropertyGroup):
    obj: PointerProperty(type=bpy.types.Object)


class MADILIB_Set(PropertyGroup):
    name: StringProperty(name="Name", default="Set")
    uid: StringProperty(name="Id", default="")
    members: CollectionProperty(type=MADILIB_SetMember)


# =========================================================== helpers ======
def _sets(scene):
    return scene.madi_sets


def _find(scene, uid):
    for index, item in enumerate(scene.madi_sets):
        if item.uid == uid:
            return index, item
    return -1, None


def _new_uid():
    return uuid.uuid4().hex[:12]


def _clean_name(text, fallback="Set"):
    text = (text or "").strip().replace("\n", " ")[:NAME_MAX]
    return text or fallback


def _unique_name(scene, wanted):
    """`wanted`, or `wanted 2`, `wanted 3`… Names are not keys, but two sets
    with the same name are miserable to work with."""
    existing = {item.name for item in scene.madi_sets}
    if wanted not in existing:
        return wanted
    n = 2
    while "%s %d" % (wanted, n) in existing:
        n += 1
    return "%s %d" % (wanted, n)


def _members(item):
    """Live objects only, in order, de-duplicated."""
    seen = set()
    out = []
    for member in item.members:
        obj = member.obj
        if obj is None or obj.name in seen:
            continue
        seen.add(obj.name)
        out.append(obj)
    return out


def _missing(item):
    return sum(1 for member in item.members if member.obj is None)


def _kind(obj):
    return obj.type


def _view_layer(context=None):
    context = context or bpy.context
    layer = getattr(context, "view_layer", None)
    if layer is not None:
        return layer
    return context.scene.view_layers[0]


def _walk_layers(view_layer):
    """Every LayerCollection under the root, root included.

    ⚠ Keyed by NAME in the snapshot, which is what Blender gives us; two
    collections cannot share a name in one file, so it is a key here even
    though a SET name is not.
    """
    out = []

    def walk(layer):
        out.append(layer)
        for child in layer.children:
            walk(child)

    walk(view_layer.layer_collection)
    return out


def _blind_objects(view_layer):
    """Names of objects inside an EXCLUDED collection.

    ⚠ Kept after isolate stopped hiding things, because `select_set` RAISES
    for an object that is not in the view layer ("cannot be selected because
    it is not in View Layer") where most of the API merely no-ops. `sets_select`
    would take the whole command down on a set with one member in an excluded
    collection — found by benchmarking, not by the suite.
    """
    blind = set()
    for layer in _walk_layers(view_layer):
        if layer.exclude:
            for obj in layer.collection.all_objects:
                blind.add(obj.name)
    return blind


# =========================================================== isolate ======
# ⚠⚠ **ISOLATE IS BLENDER'S LOCAL VIEW — the `/` key.** Marty, 2026-08-18,
# after seeing the first version: *"when saying 'isolate' i mean like this
# (local), as in when you select multiple items and press /"*.
#
# The first version HID everything that was not a member (view-layer eyes and
# collection flags) and kept a snapshot so it could put the scene back. That
# is a different feature: it changes the file's visibility state, it shows up
# in renders, and being reversible needed a small mountain of machinery —
# including the excluded-collection trap that phase 0 existed to find. Local
# view needs none of it: it is a property of the VIEWPORT, nothing about the
# objects changes, and leaving it is exact by construction.
#
# What that buys, and what it costs:
#   * Nothing is hidden, so nothing has to be restored. The snapshot, the
#     blind-object rules and the collection walk are all gone.
#   * It does NOT affect renders, and it is not saved as visibility state —
#     it is where you are LOOKING, not what the file contains.
#   * It works through the SELECTION, because that is how `/` works: the
#     operator acts on what is selected. So isolating a set selects it, which
#     is exactly what pressing `/` on those objects would have done.


def _view3d_spaces(context=None):
    """Every 3D viewport, in every window: (window, area, region, space).

    ⚠ `/` acts on ONE viewport — the one under the pointer. A button in an
    app on the other monitor has no pointer to speak of, so this acts on ALL
    of them: Isolate has to mean the same thing every time it is pressed, and
    in the single-viewport layout almost everybody uses they are the same
    rule anyway.
    """
    context = context or bpy.context
    out = []
    for window in context.window_manager.windows:
        screen = window.screen
        if screen is None:
            continue
        for area in screen.areas:
            if area.type != 'VIEW_3D':
                continue
            region = next((r for r in area.regions if r.type == 'WINDOW'),
                          None)
            if region is not None:
                out.append((window, area, region, area.spaces.active))
    return out


def _in_local_view(context=None):
    """Is any 3D viewport in local view?

    ⚠ This — not the stored uid — is the truth about whether anything is
    isolated. The user can leave local view with `/` at any moment and the
    add-on hears nothing about it, so the stored uid alone would make the
    toggle press the wrong way.
    """
    return any(space.local_view is not None
               for _w, _a, _r, space in _view3d_spaces(context))


def _set_local_members(space, keep, scene):
    """Make `space`'s local view hold exactly `keep` (a set of names).

    ⚠⚠ **`local_view_set` NEEDS `view_layer.update()`.** Measured
    2026-08-18: the write lands, but nothing reflects it — not
    `local_view_get`, not `tag_redraw` — until the view layer is updated.
    Without it a second pass in the same command reads the pre-change
    membership and undoes its own work.
    """
    if space.local_view is None:
        return 0
    shown = 0
    for obj in scene.objects:
        want = obj.name in keep
        try:
            if obj.local_view_get(space) != want:
                obj.local_view_set(space, want)
        except RuntimeError:
            # Not in this view layer, so it cannot be in its local view.
            continue
        if want:
            shown += 1
    bpy.context.view_layer.update()
    return shown


def _enter_local_view(scene, keep, frame=False, context=None):
    """Enter local view holding `keep`, the way `/` does — through the
    selection, because that is what the operator acts on."""
    for obj in scene.objects:
        try:
            obj.select_set(obj.name in keep)
        except RuntimeError:
            continue                # not in this view layer; not selectable
    context = context or bpy.context
    entered = 0
    for window, area, region, space in _view3d_spaces(context):
        if space.local_view is not None:
            # Already in one — correct its contents rather than bouncing out
            # and back in, which would re-frame the view for no reason.
            _set_local_members(space, keep, scene)
            entered += 1
            continue
        with context.temp_override(window=window, area=area, region=region):
            bpy.ops.view3d.localview(frame_selected=bool(frame))
        entered += 1
    return entered


def _leave_local_view(context=None):
    context = context or bpy.context
    left = 0
    for window, area, region, space in _view3d_spaces(context):
        if space.local_view is None:
            continue
        with context.temp_override(window=window, area=area, region=region):
            bpy.ops.view3d.localview(frame_selected=False)
        left += 1
    return left


def _refresh_isolated(scene, item, context=None):
    """The isolated set gained or lost a member — make every local view that
    is currently open match it again. Does NOT enter or leave one."""
    keep = {obj.name for obj in _members(item)}
    touched = 0
    for _w, _a, _r, space in _view3d_spaces(context):
        if space.local_view is not None:
            _set_local_members(space, keep, scene)
            touched += 1
    return touched


def _restore_legacy_snapshot(scene):
    """⚠ ONE-SHOT MIGRATION — DELETE ME once Marty confirms.

    1.23.0's first cut hid objects and kept a JSON snapshot on the scene so it
    could put them back. It was never released, but it WAS installed in the
    5.2 Blender for an afternoon, and a file saved while it was isolating has
    real objects hidden with no button left that would unhide them. If such a
    snapshot turns up, it is applied once and cleared.
    """
    raw = getattr(scene, "madi_sets_snapshot", "")
    if not raw:
        return False
    try:
        data = json.loads(raw)
    except ValueError:
        scene.madi_sets_snapshot = ""
        return False
    view_layer = _view_layer()
    objs = data.get("o", {})
    colls = data.get("c", {})
    blind = set(data.get("b") or ())
    for layer in _walk_layers(view_layer):
        if layer.exclude:
            layer.exclude = False
    for obj in scene.objects:
        mask = objs.get(obj.name, 0)
        if obj.hide_viewport != bool(mask & OB_VIEWPORT):
            obj.hide_viewport = bool(mask & OB_VIEWPORT)
        if obj.name not in blind:
            want = bool(mask & OB_HIDE)
            if obj.hide_get(view_layer=view_layer) != want:
                obj.hide_set(want, view_layer=view_layer)
    for layer in _walk_layers(view_layer):
        mask = colls.get(layer.name, 0)
        if layer.hide_viewport != bool(mask & LC_HIDE):
            layer.hide_viewport = bool(mask & LC_HIDE)
        if layer.collection.hide_viewport != bool(mask & LC_COLL_HIDE):
            layer.collection.hide_viewport = bool(mask & LC_COLL_HIDE)
        if layer.exclude != bool(mask & LC_EXCLUDE):
            layer.exclude = bool(mask & LC_EXCLUDE)
    scene.madi_sets_snapshot = ""
    return True


def set_isolated(scene, uid, view_layer=None, frame=None, context=None):
    """The one entry point both the operator and the bridge route use.

    `uid` None or "" leaves local view. Passing the uid that is ALREADY
    isolated also leaves it — that is what makes the button a toggle.
    """
    context = context or bpy.context
    legacy = _restore_legacy_snapshot(scene)
    current = scene.madi_sets_isolated
    # ⚠ TRUE, because that is what `/` does: `view3d.localview` frames the
    # selection by default, and matching the key is the whole point. Switching
    # from one set to another does NOT re-frame — that path goes through
    # `_set_local_members`, which leaves the view where it is rather than
    # yanking it on every press.
    if frame is None:
        frame = True

    # ⚠ If the user left local view with `/`, the stored uid is stale, and
    # treating this press as "turn it off" would do nothing visible at all.
    if not _in_local_view(context):
        current = ""

    if not uid or uid == current:
        left = _leave_local_view(context)
        scene.madi_sets_isolated = ""
        return {"isolated": None, "left": left, "legacy_restored": legacy}

    _index, item = _find(scene, uid)
    if item is None:
        return {"error": "no such set"}

    keep = {obj.name for obj in _members(item)}
    if not keep:
        return {"error": "that set is empty"}
    spaces = _enter_local_view(scene, keep, frame=frame, context=context)
    if not spaces:
        return {"error": "no 3D viewport to isolate in"}
    scene.madi_sets_isolated = uid
    return {"isolated": uid, "shown": len(keep), "viewports": spaces,
            "legacy_restored": legacy}


# =========================================================== reads ========
def revision(scene=None):
    """A cheap hash of everything the app's list draws.

    ⚠ It must move whenever the DISPLAYED state moves and not otherwise —
    that is the entire contract with the poll. Measured 2026-08-18 on 40 sets
    x 25 members: 0.019 ms, against 0.339 ms for the full read, so the poll
    costs about 1/18th of what it would if it rebuilt every time.
    """
    scene = scene or bpy.context.scene
    parts = [scene.madi_sets_isolated or ""]
    for item in scene.madi_sets:
        parts.append(item.uid)
        parts.append(item.name)
        # ⚠ len(members) is NOT enough: swapping a member for another keeps
        # the count. The names are what the members pane draws.
        for member in item.members:
            parts.append(member.obj.name if member.obj is not None else "-")
    return hash("\x00".join(parts)) & 0x7FFFFFFF


def sets_list(include_scene=False):
    """PURE READ. Every set, its members, and what is isolated.

    `include_scene` adds the scene's objects — the app asks for that only
    when it needs the picker, never on the poll.
    """
    scene = bpy.context.scene
    out = []
    for item in scene.madi_sets:
        members = []
        for member in item.members:
            obj = member.obj
            if obj is None:
                members.append({"name": None, "type": "MISSING"})
            else:
                members.append({"name": obj.name, "type": _kind(obj)})
        out.append({
            "uid": item.uid,
            "name": item.name,
            "members": members,
            "count": len(members),
            "missing": _missing(item),
        })
    reply = {
        "sets": out,
        "active": int(scene.madi_sets_active),
        "isolated": scene.madi_sets_isolated or None,
        "revision": revision(scene),
        "scene": scene.name,
        "file": bpy.data.filepath,
        "selected": [obj.name for obj in bpy.context.selected_objects],
    }
    if include_scene:
        reply["objects"] = [{"name": obj.name, "type": obj.type}
                            for obj in scene.objects]
    return reply


# =========================================================== writes =======
def sets_new(name=None, from_selection=True):
    scene = bpy.context.scene
    if len(scene.madi_sets) >= SETS_MAX:
        return {"error": "too many sets (%d)" % SETS_MAX}
    objects = list(bpy.context.selected_objects) if from_selection else []
    wanted = _clean_name(name, "Set %d" % (len(scene.madi_sets) + 1))
    item = scene.madi_sets.add()
    item.uid = _new_uid()
    item.name = _unique_name(scene, wanted)
    for obj in objects:
        item.members.add().obj = obj
    scene.madi_sets_active = len(scene.madi_sets) - 1
    return {"uid": item.uid, "name": item.name, "added": len(objects),
            "revision": revision(scene)}


def sets_delete(uid):
    scene = bpy.context.scene
    index, item = _find(scene, uid)
    if item is None:
        return {"error": "no such set"}
    # ⚠ Deleting the set that is ISOLATED must restore first, or the scene is
    # left hidden with nothing left to press to bring it back.
    if scene.madi_sets_isolated == uid:
        set_isolated(scene, None)
    scene.madi_sets.remove(index)
    scene.madi_sets_active = max(0, min(int(scene.madi_sets_active),
                                        len(scene.madi_sets) - 1))
    return {"deleted": uid, "revision": revision(scene)}


def sets_rename(uid, name):
    scene = bpy.context.scene
    _index, item = _find(scene, uid)
    if item is None:
        return {"error": "no such set"}
    item.name = _unique_name(scene, _clean_name(name, item.name))
    return {"uid": uid, "name": item.name, "revision": revision(scene)}


def sets_move(uid, delta):
    scene = bpy.context.scene
    index, item = _find(scene, uid)
    if item is None:
        return {"error": "no such set"}
    target = max(0, min(len(scene.madi_sets) - 1, index + int(delta)))
    if target != index:
        scene.madi_sets.move(index, target)
        scene.madi_sets_active = target
    return {"uid": uid, "index": target, "revision": revision(scene)}


def sets_add_selected(uid, names=None):
    scene = bpy.context.scene
    _index, item = _find(scene, uid)
    if item is None:
        return {"error": "no such set"}
    if names is None:
        objects = list(bpy.context.selected_objects)
    else:
        objects = [scene.objects[n] for n in names if n in scene.objects]
    have = {obj.name for obj in _members(item)}
    added = 0
    for obj in objects:
        if obj.name in have:
            continue
        item.members.add().obj = obj
        have.add(obj.name)
        added += 1
    # ⚠ Adding to the set that is on screen must widen the local view too,
    # or the new member is invisible and the tool looks broken.
    if added and scene.madi_sets_isolated == uid:
        _refresh_isolated(scene, item)
    return {"uid": uid, "added": added, "count": len(_members(item)),
            "revision": revision(scene)}


def sets_remove(uid, names=None):
    """Remove named objects, or the SELECTED ones when `names` is None."""
    scene = bpy.context.scene
    _index, item = _find(scene, uid)
    if item is None:
        return {"error": "no such set"}
    if names is None:
        names = {obj.name for obj in bpy.context.selected_objects}
    else:
        names = set(names)
    removed = 0
    for i in range(len(item.members) - 1, -1, -1):
        obj = item.members[i].obj
        if obj is not None and obj.name in names:
            item.members.remove(i)
            removed += 1
    if removed and scene.madi_sets_isolated == uid:
        _refresh_isolated(scene, item)
    return {"uid": uid, "removed": removed, "count": len(_members(item)),
            "revision": revision(scene)}


def sets_clean(uid=None):
    """Drop members whose object no longer exists. Never automatic: a missing
    member is shown, and dropping it is the user's call."""
    scene = bpy.context.scene
    items = list(scene.madi_sets) if uid is None else []
    if uid is not None:
        _index, item = _find(scene, uid)
        if item is None:
            return {"error": "no such set"}
        items = [item]
    dropped = 0
    for item in items:
        for i in range(len(item.members) - 1, -1, -1):
            if item.members[i].obj is None:
                item.members.remove(i)
                dropped += 1
    return {"dropped": dropped, "revision": revision(scene)}


def sets_select(uid, extend=False):
    scene = bpy.context.scene
    view_layer = _view_layer()
    _index, item = _find(scene, uid)
    if item is None:
        return {"error": "no such set"}
    # ⚠⚠ `select_set` RAISES for an object that is not in the view layer —
    # "cannot be selected because it is not in View Layer" — where `hide_set`
    # on the same object silently does nothing. So the blind set is needed
    # here too, and it is needed on the DESELECT pass as well: a set whose
    # member sits in an excluded collection would otherwise take the whole
    # command down. Found by benchmarking on a scene with excluded
    # collections, not by the suite, which had none in its Select case.
    blind = _blind_objects(view_layer)
    if not extend:
        # ⚠ Guarded, like every other bulk write here: `select_set(False)` on
        # an already-deselected object is charged at full price, and in a big
        # scene almost nothing is selected.
        for obj in scene.objects:
            if obj.name not in blind and obj.select_get(view_layer=view_layer):
                obj.select_set(False, view_layer=view_layer)
    members = _members(item)
    selected = 0
    active = None
    for obj in members:
        # ⚠ A hidden object cannot be selected either, and THAT is a silent
        # no-op — so the count reports what really happened rather than how
        # many members there are.
        if (obj.name in blind or obj.hide_get(view_layer=view_layer)
                or obj.hide_viewport):
            continue
        obj.select_set(True, view_layer=view_layer)
        selected += 1
        if active is None or (obj.type == 'ARMATURE'
                              and active.type != 'ARMATURE'):
            active = obj
    if active is not None:
        view_layer.objects.active = active
    return {"uid": uid, "selected": selected, "skipped": len(members) - selected,
            "active": active.name if active else None}


def sets_isolate(uid=None):
    return set_isolated(bpy.context.scene, uid)


# =========================================================== operators ====
class _Op:
    """Shared plumbing. Every operator here is UNDO-able, because each one is
    a thing the user did and Ctrl+Z must take it back."""
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.scene is not None

    def report_reply(self, reply):
        if isinstance(reply, dict) and reply.get("error"):
            self.report({'ERROR'}, str(reply["error"]))
            return {'CANCELLED'}
        return {'FINISHED'}


def _active_uid(context):
    scene = context.scene
    index = int(scene.madi_sets_active)
    if 0 <= index < len(scene.madi_sets):
        return scene.madi_sets[index].uid
    return ""


class MADILIB_OT_set_new(_Op, Operator):
    bl_idname = "madilib.set_new"
    bl_label = "New Set from Selection"
    bl_description = ("Make a new set from the selected objects. With nothing "
                      "selected it makes an empty set")

    def execute(self, context):
        return self.report_reply(sets_new())


class MADILIB_OT_set_delete(_Op, Operator):
    bl_idname = "madilib.set_delete"
    bl_label = "Delete Set"
    bl_description = "Delete the active set. The objects are not touched"

    def execute(self, context):
        return self.report_reply(sets_delete(_active_uid(context)))


class MADILIB_OT_set_move(_Op, Operator):
    bl_idname = "madilib.set_move"
    bl_label = "Move Set"
    bl_description = "Move the active set up or down the list"
    delta: IntProperty(default=-1)

    def execute(self, context):
        return self.report_reply(sets_move(_active_uid(context), self.delta))


class MADILIB_OT_set_add_selected(_Op, Operator):
    bl_idname = "madilib.set_add_selected"
    bl_label = "Add Selected"
    bl_description = "Add the selected objects to the active set"

    def execute(self, context):
        return self.report_reply(sets_add_selected(_active_uid(context)))


class MADILIB_OT_set_remove_selected(_Op, Operator):
    bl_idname = "madilib.set_remove_selected"
    bl_label = "Remove Selected"
    bl_description = ("Remove the selected objects from the active set. The "
                      "objects are not deleted")

    def execute(self, context):
        return self.report_reply(sets_remove(_active_uid(context)))


class MADILIB_OT_set_remove_one(_Op, Operator):
    bl_idname = "madilib.set_remove_one"
    bl_label = "Remove From Set"
    bl_description = "Remove this object from the set"
    name: StringProperty()

    def execute(self, context):
        return self.report_reply(
            sets_remove(_active_uid(context), [self.name]))


class MADILIB_OT_set_clean(_Op, Operator):
    bl_idname = "madilib.set_clean"
    bl_label = "Clean Missing"
    bl_description = ("Drop members whose object has been deleted from the "
                      "file")

    def execute(self, context):
        return self.report_reply(sets_clean(_active_uid(context)))


class MADILIB_OT_set_select(_Op, Operator):
    bl_idname = "madilib.set_select"
    bl_label = "Select Set"
    bl_description = "Select this set's objects in the viewport"
    uid: StringProperty()
    extend: BoolProperty(default=False)

    def execute(self, context):
        uid = self.uid or _active_uid(context)
        return self.report_reply(sets_select(uid, self.extend))


class MADILIB_OT_set_isolate(_Op, Operator):
    bl_idname = "madilib.set_isolate"
    bl_label = "Isolate Set"
    bl_description = ("Local View on this set - the same as selecting it and "
                      "pressing /. Press again to come back out")
    uid: StringProperty()

    def execute(self, context):
        uid = self.uid or _active_uid(context)
        return self.report_reply(set_isolated(context.scene, uid,
                                              _view_layer(context)))


class MADILIB_OT_set_show_all(_Op, Operator):
    bl_idname = "madilib.set_show_all"
    bl_label = "Leave Local View"
    bl_description = "Come back out of Local View and see the whole scene"

    def execute(self, context):
        return self.report_reply(set_isolated(context.scene, None,
                                              _view_layer(context)))


# =========================================================== UI ===========
ICON_FOR = {
    'ARMATURE': 'OUTLINER_OB_ARMATURE',
    'MESH': 'OUTLINER_OB_MESH',
    'LIGHT': 'OUTLINER_OB_LIGHT',
    'CAMERA': 'OUTLINER_OB_CAMERA',
    'EMPTY': 'OUTLINER_OB_EMPTY',
    'CURVE': 'OUTLINER_OB_CURVE',
    'SURFACE': 'OUTLINER_OB_SURFACE',
    'META': 'OUTLINER_OB_META',
    'FONT': 'OUTLINER_OB_FONT',
    'VOLUME': 'OUTLINER_OB_VOLUME',
    'GPENCIL': 'OUTLINER_OB_GREASEPENCIL',
    'GREASEPENCIL': 'OUTLINER_OB_GREASEPENCIL',
    'LATTICE': 'OUTLINER_OB_LATTICE',
    'SPEAKER': 'OUTLINER_OB_SPEAKER',
    'LIGHT_PROBE': 'OUTLINER_OB_LIGHTPROBE',
}


def _set_icon(item):
    """One glyph for a whole set: a rig if it has one, else lights/cameras if
    that is all it is, else a mesh."""
    kinds = [member.obj.type for member in item.members
             if member.obj is not None]
    if 'ARMATURE' in kinds:
        return ICON_FOR['ARMATURE']
    if kinds and all(kind in ('LIGHT', 'CAMERA') for kind in kinds):
        return (ICON_FOR['LIGHT'] if kinds.count('LIGHT') >= kinds.count(
            'CAMERA') else ICON_FOR['CAMERA'])
    return ICON_FOR['MESH']


class MADILIB_UL_sets(UIList):
    """One row: the isolate star, the name, a warning if anything is missing,
    and the count.

    ⚠ **draw_item MUST NOT WRITE** — the same rule as the Markers and Anim
    Layers panels. Blender redraws constantly; a write here would dirty the
    file for as long as the panel is on screen.
    """

    def draw_item(self, context, layout, data, item, icon, active_data,
                  active_propname, index):
        scene = context.scene
        isolated = scene.madi_sets_isolated == item.uid
        row = layout.row(align=True)
        star = row.operator("madilib.set_isolate", text="", emboss=False,
                            icon='SOLO_ON' if isolated else 'SOLO_OFF')
        star.uid = item.uid
        row.prop(item, "name", text="", emboss=False, icon=_set_icon(item))
        tail = row.row(align=True)
        tail.alignment = 'RIGHT'
        if _missing(item):
            tail.label(text="", icon='ERROR')
        tail.label(text=str(len(item.members)))


class MADILIB_PT_organize(Panel):
    bl_label = "Organize"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "MadihsonNSFW"

    def draw(self, context):
        scene = context.scene
        layout = self.layout
        uid = scene.madi_sets_isolated
        row = layout.row()
        if uid:
            _index, item = _find(scene, uid)
            row.label(text="Local View: %s" % (item.name if item else "?"),
                      icon='SOLO_ON')
            row.operator("madilib.set_show_all", text="", icon='HIDE_OFF')
        else:
            row.label(text="Not in Local View", icon='SOLO_OFF')


class MADILIB_PT_organize_sets(Panel):
    bl_label = "Sets"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "MadihsonNSFW"
    bl_parent_id = "MADILIB_PT_organize"

    def draw(self, context):
        scene = context.scene
        layout = self.layout
        row = layout.row()
        row.template_list("MADILIB_UL_sets", "", scene, "madi_sets",
                          scene, "madi_sets_active", rows=5)
        col = row.column(align=True)
        col.operator("madilib.set_new", text="", icon='ADD')
        col.operator("madilib.set_delete", text="", icon='REMOVE')
        col.separator()
        col.operator("madilib.set_move", text="", icon='TRIA_UP').delta = -1
        col.operator("madilib.set_move", text="", icon='TRIA_DOWN').delta = 1

        index = int(scene.madi_sets_active)
        if not (0 <= index < len(scene.madi_sets)):
            layout.label(text="No set selected", icon='INFO')
            return
        item = scene.madi_sets[index]

        row = layout.row(align=True)
        row.operator("madilib.set_select", text="Select",
                     icon='RESTRICT_SELECT_OFF').uid = item.uid
        row.operator("madilib.set_add_selected", text="Add Sel.", icon='ADD')
        row.operator("madilib.set_remove_selected", text="Remove",
                     icon='REMOVE')

        isolated = scene.madi_sets_isolated == item.uid
        big = layout.row()
        big.scale_y = 1.25
        big.operator("madilib.set_isolate",
                     text="Isolate  %s" % item.name,
                     icon='SOLO_ON', depress=isolated).uid = item.uid

        if _missing(item):
            row = layout.row()
            row.alert = True
            row.label(text="%d object no longer in the file"
                      % _missing(item) if _missing(item) == 1 else
                      "%d objects no longer in the file" % _missing(item),
                      icon='ERROR')
            row.operator("madilib.set_clean", text="Clean")


class MADILIB_PT_organize_members(Panel):
    bl_label = "Members"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "MadihsonNSFW"
    bl_parent_id = "MADILIB_PT_organize_sets"

    @classmethod
    def poll(cls, context):
        scene = context.scene
        return 0 <= int(scene.madi_sets_active) < len(scene.madi_sets)

    def draw_header(self, context):
        scene = context.scene
        item = scene.madi_sets[int(scene.madi_sets_active)]
        self.layout.label(text="  %s  ·  %d" % (item.name, len(item.members)))

    def draw(self, context):
        scene = context.scene
        item = scene.madi_sets[int(scene.madi_sets_active)]
        col = self.layout.column(align=True)
        if not len(item.members):
            col.label(text="Empty — select objects and press Add Sel.",
                      icon='INFO')
            return
        for member in item.members:
            obj = member.obj
            row = col.row(align=True)
            if obj is None:
                sub = row.row()
                sub.alert = True
                sub.label(text="(deleted)", icon='ERROR')
                continue
            row.label(text=obj.name,
                      icon=ICON_FOR.get(obj.type, 'OBJECT_DATA'))
            row.operator("madilib.set_remove_one", text="", icon='X',
                         emboss=False).name = obj.name


# =========================================================== register =====
_classes = (
    MADILIB_SetMember,
    MADILIB_Set,
    MADILIB_OT_set_new,
    MADILIB_OT_set_delete,
    MADILIB_OT_set_move,
    MADILIB_OT_set_add_selected,
    MADILIB_OT_set_remove_selected,
    MADILIB_OT_set_remove_one,
    MADILIB_OT_set_clean,
    MADILIB_OT_set_select,
    MADILIB_OT_set_isolate,
    MADILIB_OT_set_show_all,
    MADILIB_UL_sets,
    MADILIB_PT_organize,
    MADILIB_PT_organize_sets,
    MADILIB_PT_organize_members,
)


def register():
    # ⚠ THE PROPERTY GROUPS COME FIRST — a CollectionProperty of a type that
    # is not registered yet throws, and the traceback names the Scene rather
    # than the ordering.
    for cls in _classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.madi_sets = CollectionProperty(type=MADILIB_Set)
    bpy.types.Scene.madi_sets_active = IntProperty(
        name="Active set", default=0,
        description="Which set the members list is showing")
    bpy.types.Scene.madi_sets_isolated = StringProperty(
        name="Isolated", default="",
        description="Id of the set currently isolated, if any")
    bpy.types.Scene.madi_sets_snapshot = StringProperty(
        name="Snapshot", default="",
        description="What was hidden before isolating, so it can be put back")


def unregister():
    # ⚠ The Scene properties are NOT deleted, for the reason markers.py gives:
    # the VALUES live in the .blend either way, but removing the definition
    # while a file is open makes every stored set unreachable — including to
    # the save that may be about to happen.
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
