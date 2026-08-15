"""Timeline markers with notes and tags — the Blender half.

The same markers the Toolset app's Markers tool drives, edited without leaving
Blender. Lives in the Timeline / Dope Sheet sidebar (N key), because that is
where markers already are.

WHY THERE IS NO SYNC MECHANISM
Notes and tags are stored ON THE MARKER, in the .blend, so this panel and the
app are reading the same bytes. Neither tells the other anything; the app polls
`marker_list` and Blender redraws the panel constantly. The only thing the app
needs is to know WHEN to re-read, which is what `revision()` is for.

⚠ THE ERROR MESSAGE HERE IS A LIAR — MEASURED 2026-08-12.
`marker["note"] = "x"` raises `this type doesn't support IDProperties`, and so
does `marker.get("note")`. Read that alone and you conclude markers cannot hold
metadata and go build a name-keyed side table on the Scene. They can: a
REGISTERED `bpy.props` property works, and the value survives a save, a fresh
Blender process, and even a re-save by a Blender that has never had this add-on
installed (all three measured, two separate processes — a same-session
`open_mainfile` proves nothing). That is why the data lives here and not in a
dict beside it. Full story in BLENDER_NOTES.md.

⚠ MARKER NAMES ARE NOT UNIQUE, AND TWO MARKERS CAN SHARE A NAME *AND* A FRAME.
`timeline_markers.new("shot_010", frame=10)` twice gives two markers, and
Blender is perfectly happy about it. So a name — or even name+frame — is NOT a
key, and anything that builds a dict out of one silently merges two markers into
one row. That is almost certainly why the addon this was modelled on ships a
"clean overlapping markers" tool: it is cleaning up after its own keying. We
address markers by `madi_uid` instead, and need no such tool.

⚠ `marker_list` IS A PURE READ AND MUST STAY ONE. It is polled by the app, and
writing anything from it — including lazily handing out a uid — would mark
Marty's file dirty just because the app is open. A uid is therefore assigned on
the first WRITE to a marker, and until then the app addresses a marker by index
verified against its name and frame (`_resolve`). Blender only reorders
`timeline_markers` when one is added or removed, so an index is good for the
round trip that a single command takes.

⚠ draw() MUST NOT WRITE, for the same reason it must not in the Anim Layers
panel: `_uid_for` is never called from a draw path.
"""

import json

import bpy
from bpy.app.handlers import persistent
from bpy.props import EnumProperty, IntProperty, StringProperty
from bpy.types import Menu, Operator, Panel, PropertyGroup, UIList

# Both live on the SCENE, which — unlike a marker — does take ID properties.
# JSON rather than a PropertyGroup collection on purpose: it is readable by
# anything, it survives the add-on being absent, and there is no registration
# order to get wrong.
PARK_KEY = "madi_marker_parked"
SETS_KEY = "madi_marker_sets"
# ⚠ WHICH LAYER IS SHOWN LIVES BESIDE THE PARKED LIST, ON THE SCENE — they are
# two halves of one fact and must not be storable separately. It was on the
# WindowManager for one build, and that was long enough to produce a live
# session reporting `showing: "1"` with `hidden: 0` and every marker present:
# the parked list had been cleared by an add-on update, the UI copy had not.
# One home, written and cleared together, and they cannot disagree.
SHOW_KEY = "madi_marker_showing"

# ⚠ No `entitlement` import. Markers are FREE, and an unused import of the gate
# module is how a free tool starts reading as gated to the next person here.

SORT_ITEMS = [
    ('FRAME', "Frame", "Earliest frame first"),
    ('NAME', "Name", "Alphabetical by name"),
]


# ------------------------------------------------------------------ identity


def _uid_for(marker):
    """This marker's stable id, minted on first use.

    ⚠ WRITES. Never call it from `draw()` or from a polled read — see the
    module docstring. Mutating commands and operators only.
    """
    uid = marker.madi_uid
    if not uid:
        # Frame and a counter, not a random string: it stays readable in an
        # exported file, and two markers made in the same second still differ.
        base = "mk%d" % marker.frame
        taken = {m.madi_uid for m in bpy.context.scene.timeline_markers}
        uid = base
        n = 1
        while uid in taken:
            n += 1
            uid = "%s_%d" % (base, n)
        marker.madi_uid = uid
    return uid


def _tags_of(marker):
    """The tag string as a clean list. Comma-separated on purpose: it survives
    export to JSON, to a spreadsheet, and to the addon's own `.marker` files."""
    return [t.strip() for t in (marker.madi_tags or "").split(",") if t.strip()]


def layers_in_use(scene=None):
    """Every layer name currently on a marker, sorted.

    ⚠ LAYERS ARE DERIVED, NOT A LIST WE KEEP. A layer exists exactly as long as
    a marker names it, so there is no second store to fall out of step with the
    markers, nothing to migrate, and no way to end up with a layer the markers
    have never heard of. The cost is that an empty layer cannot be kept around,
    which is the right trade for a filter.
    """
    scene = scene or bpy.context.scene
    names = {m.madi_layer for m in scene.timeline_markers if m.madi_layer}
    # ⚠ PARKED MARKERS COUNT. Once a layer is being shown the others are out of
    # the scene, so reading only `timeline_markers` would drop every other layer
    # from the menu the instant you picked one — leaving no way back to them.
    names.update(r.get("layer") or "" for r in parked(scene))
    names.discard("")
    return sorted(names)


# ------------------------------------------------- parking (real hiding)
# Marty, 2026-08-12: "hide the markers of other layers if ONE layer is
# selected and show only that layer, when no layer is selected show all layer
# markers" — and he meant the TIMELINE STRIP, not just our lists.
#
# ⚠ BLENDER DRAWS EVERY MARKER IN THE SCENE, ALWAYS. There is no per-marker
# hide flag, so the only way to clear that strip is to take the markers OUT of
# `scene.timeline_markers` and put them back later. Everything below exists to
# make that round trip lossless.


def _payload(marker):
    """A marker as plain data — what parking and named sets both store."""
    return {"name": marker.name, "frame": marker.frame,
            "camera": marker.camera.name if marker.camera else None,
            "uid": marker.madi_uid, "note": marker.madi_note,
            "tags": marker.madi_tags, "layer": marker.madi_layer}


def _spawn(scene, row):
    """Put a payload back as a real marker, metadata and all."""
    marker = scene.timeline_markers.new(row.get("name") or "Marker",
                                        frame=int(row.get("frame") or 0))
    marker.madi_uid = row.get("uid") or ""
    marker.madi_note = row.get("note") or ""
    marker.madi_tags = row.get("tags") or ""
    marker.madi_layer = row.get("layer") or ""
    cam = row.get("camera")
    if cam:
        # ⚠ Resolved by NAME on the way back, and a camera that has since been
        # deleted or renamed simply leaves the marker unbound rather than
        # raising — losing a binding is recoverable, losing the marker is not.
        obj = bpy.data.objects.get(cam)
        if obj is not None and obj.type == 'CAMERA':
            marker.camera = obj
    return marker


def _read_json(scene, key, default):
    raw = scene.get(key)
    if not raw:
        return default
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return default
    return value if isinstance(value, type(default)) else default


def _write_json(scene, key, value):
    if value:
        scene[key] = json.dumps(value)
    elif key in scene:
        del scene[key]


def parked(scene=None):
    return _read_json(scene or bpy.context.scene, PARK_KEY, [])


def all_markers(scene=None):
    """Live markers PLUS parked ones — the whole set, whatever is on screen.

    ⚠ Anything that saves or exports must use this, not `timeline_markers`.
    Saving a named set while a layer is shown would otherwise quietly store a
    third of the file's markers and look like it had worked.
    """
    scene = scene or bpy.context.scene
    rows = [_payload(m) for m in scene.timeline_markers]
    rows.extend(parked(scene))
    rows.sort(key=lambda r: (r["frame"], r["name"]))
    return rows


def show_layer(layer="", scene=None):
    """Show only `layer`; "" shows everything. Returns what changed.

    ⚠ RESTORE FIRST, THEN PARK. Switching straight from one layer to another
    otherwise parks on top of a park and strands the first layer's markers in
    a list nothing reads back.
    """
    scene = scene or bpy.context.scene
    for row in parked(scene):
        _spawn(scene, row)
    _write_json(scene, PARK_KEY, [])

    hidden = []
    if layer:
        for marker in list(scene.timeline_markers):
            if marker.madi_layer != layer:
                hidden.append(_payload(marker))
                scene.timeline_markers.remove(marker)
    _write_json(scene, PARK_KEY, hidden)
    if layer:
        scene[SHOW_KEY] = layer
    elif SHOW_KEY in scene:
        del scene[SHOW_KEY]
    return {"layer": layer, "hidden": len(hidden),
            "showing": len(scene.timeline_markers), "revision": revision(scene)}


@persistent
def _restore_on_load(_dummy):
    """Every file opens showing ALL its markers.

    ⚠ DELIBERATE, and it is a safety rule rather than a preference. A .blend
    saved mid-filter has most of its markers in a Scene property that only this
    add-on understands; opening it anywhere else — an older build, a machine
    without the extension, a buyer's copy — would look exactly like the file had
    lost them. Hiding is a VIEW, so it lasts as long as the session and no
    longer.
    """
    try:
        scene = bpy.context.scene
        if scene is None or not parked(scene):
            return
        show_layer("", scene)
    except Exception:                              # noqa: BLE001
        pass


# ------------------------------------------------------------- named sets
# Marty, 2026-08-12: "save marker preset per project, this should be saved
# in .blend file and autoloaded by our tool" -> named sets of markers, stored
# on the scene, so they travel with the project and the app sees them the
# moment it connects.


def marker_sets(scene=None):
    return _read_json(scene or bpy.context.scene, SETS_KEY, {})


def marker_set_save(name, scene=None):
    scene = scene or bpy.context.scene
    name = (name or "").strip()
    if not name:
        raise RuntimeError("give the set a name")
    sets = marker_sets(scene)
    sets[name] = all_markers(scene)          # ⚠ parked ones included
    _write_json(scene, SETS_KEY, sets)
    return {"saved": name, "count": len(sets[name]), "sets": sorted(sets)}


def marker_set_load(name, scene=None):
    """Replace every marker with the named set. Parked ones go too."""
    scene = scene or bpy.context.scene
    sets = marker_sets(scene)
    if name not in sets:
        raise RuntimeError("there is no marker set called '%s'" % name)
    _write_json(scene, PARK_KEY, [])
    for marker in list(scene.timeline_markers):
        scene.timeline_markers.remove(marker)
    for row in sets[name]:
        _spawn(scene, row)
    if SHOW_KEY in scene:
        del scene[SHOW_KEY]
    return {"loaded": name, "count": len(sets[name]), "revision": revision(scene)}


def marker_set_delete(name, scene=None):
    scene = scene or bpy.context.scene
    sets = marker_sets(scene)
    if name in sets:
        del sets[name]
    _write_json(scene, SETS_KEY, sets)
    return {"deleted": name, "sets": sorted(sets)}


def _resolve(scene, ref):
    """Find the marker a command is talking about, or raise.

    Three ways in, most exact first, because the app may be holding any of them
    depending on whether this marker has ever been written to:
      1. `uid`  — exact, once the marker has metadata;
      2. `index` VERIFIED against name and frame — safe for one round trip;
      3. a unique (name, frame) pair.
    An ambiguous match is an ERROR, never a guess: writing a note onto the wrong
    marker is precisely the bug the uid exists to prevent.
    """
    markers = scene.timeline_markers
    uid = (ref.get("uid") or "").strip()
    if uid:
        for m in markers:
            if m.madi_uid == uid:
                return m
    name = ref.get("name")
    frame = ref.get("frame")
    idx = ref.get("index")
    if isinstance(idx, int) and 0 <= idx < len(markers):
        m = markers[idx]
        if (name is None or m.name == name) and (frame is None or m.frame == frame):
            return m
    if name is not None:
        hits = [m for m in markers
                if m.name == name and (frame is None or m.frame == frame)]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            raise RuntimeError(
                "%d markers are called '%s' at that frame - refresh the list "
                "and try again" % (len(hits), name))
    raise RuntimeError("that marker is no longer there - refresh the list")


# --------------------------------------------------------------- bridge API
# Everything below is called BOTH by the panel's operators and by the bridge,
# so the two UIs cannot drift apart: there is only one implementation.


def _as_dict(marker, index):
    return {
        "index": index,
        "uid": marker.madi_uid,
        "name": marker.name,
        "frame": marker.frame,
        "camera": marker.camera.name if marker.camera else None,
        "note": marker.madi_note,
        "tags": _tags_of(marker),
        "layer": marker.madi_layer,
        "select": bool(marker.select),
    }


def revision(scene=None):
    """A cheap value that changes whenever anything a list would show changes.

    The app compares it on the health poll and only rebuilds its list when it
    moves, so a Blender-side edit shows up without the app polling the whole
    marker set several times a second.
    """
    scene = scene or bpy.context.scene
    live = tuple(
        (m.name, m.frame, m.camera.name if m.camera else "",
         m.madi_note, m.madi_tags, m.madi_layer)
        for m in scene.timeline_markers)
    # Parking changes what is on screen without changing any marker, so it has
    # to move the revision or neither UI would notice a layer being shown.
    return hash((live, len(parked(scene)), tuple(sorted(marker_sets(scene)))))


def _shown_layer(scene=None):
    """Which layer is being shown, or "". Read from the SCENE — see SHOW_KEY."""
    scene = scene or bpy.context.scene
    return scene.get(SHOW_KEY) or ""


def marker_list(scene=None):
    """Every marker, in frame order. ⚠ PURE READ — polled. See the docstring.

    ⚠ Reports the LIVE markers only — the parked ones are deliberately absent,
    because both UIs are showing what the timeline is showing. `hidden` says how
    many are put away; `all_markers()` is what to use when you need the lot.
    """
    scene = scene or bpy.context.scene
    rows = [_as_dict(m, i) for i, m in enumerate(scene.timeline_markers)]
    rows.sort(key=lambda r: (r["frame"], r["name"]))
    known = set()
    for r in rows:
        for t in r["tags"]:
            known.add(t)
    return {"markers": rows, "tags": sorted(known),
            "layers": layers_in_use(scene), "revision": revision(scene),
            # What the app needs to draw the layer and preset controls honestly:
            # which layer is being SHOWN (not merely filtered), how many markers
            # that is hiding, and the named sets stored in this .blend.
            "showing_layer": _shown_layer(scene),
            "hidden": len(parked(scene)),
            "sets": sorted(marker_sets(scene)),
            "frame_current": scene.frame_current,
            "frame_start": scene.frame_start, "frame_end": scene.frame_end}


def marker_add(name="Marker", frame=None, note="", tags=None, layer=""):
    scene = bpy.context.scene
    frame = scene.frame_current if frame is None else int(frame)
    marker = scene.timeline_markers.new(name or "Marker", frame=frame)
    _uid_for(marker)
    if note:
        marker.madi_note = note
    if tags:
        marker.madi_tags = ", ".join(tags) if isinstance(tags, list) else str(tags)
    if layer:
        marker.madi_layer = str(layer)
    return {"added": _as_dict(marker, len(scene.timeline_markers) - 1),
            "revision": revision(scene)}


def marker_set(ref, name=None, frame=None, note=None, tags=None, camera=...,
               layer=None):
    """Edit one marker. Only the fields actually passed are touched — the app
    sends a single field per edit, so a stale copy of the others cannot be
    written back over a change made in Blender."""
    scene = bpy.context.scene
    marker = _resolve(scene, ref or {})
    _uid_for(marker)
    if name is not None and name != marker.name:
        marker.name = str(name)
    if frame is not None and int(frame) != marker.frame:
        marker.frame = int(frame)
    if note is not None:
        marker.madi_note = str(note)
    if tags is not None:
        marker.madi_tags = (", ".join(tags) if isinstance(tags, list)
                            else str(tags))
    if layer is not None:
        # "" is a real value: it means "no layer", which is how a marker goes
        # back to showing whatever the filter is set to.
        marker.madi_layer = str(layer)
    if camera is not ...:
        # None clears the binding; a name binds. An unknown name is an error
        # rather than a silent clear — the app would show "unbound" and the
        # user would think the click worked.
        if camera is None:
            marker.camera = None
        else:
            obj = bpy.data.objects.get(str(camera))
            if obj is None or obj.type != 'CAMERA':
                raise RuntimeError("no camera called '%s' in this file" % camera)
            marker.camera = obj
    idx = list(scene.timeline_markers).index(marker)
    return {"marker": _as_dict(marker, idx), "revision": revision(scene)}


def marker_remove(ref):
    scene = bpy.context.scene
    marker = _resolve(scene, ref or {})
    gone = _as_dict(marker, list(scene.timeline_markers).index(marker))
    scene.timeline_markers.remove(marker)
    return {"removed": gone, "revision": revision(scene)}


def marker_goto(ref):
    scene = bpy.context.scene
    marker = _resolve(scene, ref or {})
    scene.frame_set(marker.frame)
    return {"frame": marker.frame, "name": marker.name}


def marker_bind_by_name(exact=True):
    """Bind every marker to the camera that shares its name.

    The utility from the addon this was modelled on, and the one place a NAME
    is the right key — the user chose to name them the same on purpose.
    """
    scene = bpy.context.scene
    bound = []
    for marker in scene.timeline_markers:
        obj = bpy.data.objects.get(marker.name)
        if obj is None and not exact:
            obj = next((o for o in bpy.data.objects
                        if o.type == 'CAMERA'
                        and o.name.lower() == marker.name.lower()), None)
        if obj is not None and obj.type == 'CAMERA' and marker.camera != obj:
            marker.camera = obj
            bound.append({"marker": marker.name, "camera": obj.name})
    return {"bound": bound, "count": len(bound), "revision": revision(scene)}


def marker_rename(find="", replace="", prefix="", suffix="", only=None):
    """Batch rename. `only` is a list of uids/indices to limit it to; without
    one it touches every marker."""
    scene = bpy.context.scene
    targets = list(scene.timeline_markers)
    if only:
        wanted = {str(o) for o in only}
        targets = [m for i, m in enumerate(targets)
                   if m.madi_uid in wanted or str(i) in wanted]
    changed = []
    for marker in targets:
        new = marker.name
        if find:
            new = new.replace(find, replace)
        new = "%s%s%s" % (prefix, new, suffix)
        if new and new != marker.name:
            _uid_for(marker)
            changed.append({"from": marker.name, "to": new})
            marker.name = new
    return {"renamed": changed, "count": len(changed), "revision": revision(scene)}


# ---------------------------------------------------------------- UI state


class MADILIB_MarkerUI(PropertyGroup):
    """Panel state only. On the WINDOW MANAGER, not the Scene: a search string
    and a highlighted row are not worth dirtying the user's file for, and they
    should not travel with it either."""

    active_index: IntProperty(name="Active Marker", default=0, min=0)
    filter_text: StringProperty(
        name="Search", default="",
        description="Filter by name, tag or note",
        options={'TEXTEDIT_UPDATE'})
    filter_tag: StringProperty(
        name="Tag", default="",
        description="Show only markers carrying this tag")
    # ⚠ NO `filter_layer` HERE. Which layer is shown is a property of the SCENE
    # (SHOW_KEY), because it has to agree with the parked list that lives beside
    # it. A second copy on the window manager is exactly what produced a session
    # claiming to show one layer while every marker was visible.
    sort_mode: EnumProperty(name="Sort", items=SORT_ITEMS, default='FRAME')


def _active_marker(context):
    """The highlighted marker, or None.

    ⚠ The index addresses the SORTED order the list draws, not the collection's
    own order, so it goes back through the same sort the UIList used. Reading
    `timeline_markers[active_index]` directly returns a different marker as soon
    as the markers are not already in frame order — which is most of the time.
    """
    markers = _sorted_markers(context)
    ui = context.window_manager.madilib_mk
    if 0 <= ui.active_index < len(markers):
        return markers[ui.active_index][1]
    return None


def _sorted_markers(context):
    """[(original_index, marker)] in the order the panel shows them."""
    ui = context.window_manager.madilib_mk
    pairs = list(enumerate(context.scene.timeline_markers))
    if ui.sort_mode == 'NAME':
        pairs.sort(key=lambda p: (p[1].name.lower(), p[1].frame))
    else:
        pairs.sort(key=lambda p: (p[1].frame, p[1].name.lower()))
    return pairs


# ------------------------------------------------------------------- list


class MADILIB_UL_markers(UIList):
    """One row per marker: name, tags, frame, and a camera icon when bound."""

    def draw_item(self, context, layout, data, item, icon, active_data,
                  active_propname, index):
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            row = layout.row(align=True)
            row.prop(item, "name", text="", emboss=False, icon='MARKER_HLT')
            # The layer is only worth a column while you are looking at ALL of
            # them — once the list is filtered to one, every row would repeat it.
            if item.madi_layer and not _shown_layer(context.scene):
                sub = row.row()
                sub.alignment = 'RIGHT'
                sub.label(text=item.madi_layer, icon='OUTLINER')
            tags = _tags_of(item)
            if tags:
                sub = row.row()
                sub.alignment = 'RIGHT'
                sub.label(text=", ".join(tags[:2]))
            if item.madi_note:
                row.label(icon='TEXT')
            if item.camera:
                row.label(icon='OUTLINER_OB_CAMERA')
            num = row.row()
            num.alignment = 'RIGHT'
            num.scale_x = 0.45
            num.label(text=str(item.frame))
        else:
            layout.label(text=item.name)

    def filter_items(self, context, data, propname):
        """Search across name, tags AND note — Blender's own filter only ever
        looks at `name`, which would miss the two fields this panel adds."""
        markers = getattr(data, propname)
        ui = context.window_manager.madilib_mk
        needle = (ui.filter_text or "").lower().strip()
        tag = (ui.filter_tag or "").lower().strip()
        # ⚠ NO LAYER FILTER HERE. A layer being shown means the other markers
        # are not in the scene at all, so there is nothing to filter — and a
        # second copy of the rule is a second place for it to go wrong.
        flags = [self.bitflag_filter_item] * len(markers)
        for i, m in enumerate(markers):
            hay = "%s %s %s %s" % (m.name, m.madi_tags, m.madi_note,
                                   m.madi_layer)
            ok = needle in hay.lower() if needle else True
            if ok and tag:
                ok = tag in [t.lower() for t in _tags_of(m)]
            if not ok:
                flags[i] = 0
        order = [p[0] for p in _sorted_markers(context)]
        # ⚠ filter_items wants, for each ORIGINAL row, the position it should
        # move to — not the original index sitting at each new position. Those
        # are inverse permutations, and swapping them sorts almost right, which
        # is the worst kind of wrong.
        neworder = [0] * len(order)
        for new_pos, orig in enumerate(order):
            neworder[orig] = new_pos
        return flags, neworder


# -------------------------------------------------------------- operators


class MADILIB_OT_mk_jump(Operator):
    bl_idname = "madilib.mk_jump"
    bl_label = "Jump to Marker"
    bl_description = "Set the playhead to this marker's frame"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        marker = _active_marker(context)
        if marker is None:
            self.report({'WARNING'}, "No marker selected")
            return {'CANCELLED'}
        context.scene.frame_set(marker.frame)
        return {'FINISHED'}


class MADILIB_OT_mk_add(Operator):
    bl_idname = "madilib.mk_add"
    bl_label = "Add Marker"
    bl_description = "Add a marker at the current frame"
    bl_options = {'REGISTER', 'UNDO'}

    name: StringProperty(name="Name", default="Marker")

    def execute(self, context):
        marker_add(self.name, context.scene.frame_current)
        return {'FINISHED'}

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)


class MADILIB_OT_mk_remove(Operator):
    bl_idname = "madilib.mk_remove"
    bl_label = "Remove Marker"
    bl_description = "Delete this marker, its note and its tags"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        marker = _active_marker(context)
        if marker is None:
            self.report({'WARNING'}, "No marker selected")
            return {'CANCELLED'}
        context.scene.timeline_markers.remove(marker)
        ui = context.window_manager.madilib_mk
        ui.active_index = max(0, ui.active_index - 1)
        return {'FINISHED'}


class MADILIB_OT_mk_render(Operator):
    bl_idname = "madilib.mk_render"
    bl_label = "Render at Marker"
    bl_description = "Jump to this marker's frame and render it"

    def execute(self, context):
        marker = _active_marker(context)
        if marker is None:
            self.report({'WARNING'}, "No marker selected")
            return {'CANCELLED'}
        context.scene.frame_set(marker.frame)
        # INVOKE_DEFAULT so it opens the render window and stays interruptible,
        # exactly as pressing F12 would.
        bpy.ops.render.render('INVOKE_DEFAULT')
        return {'FINISHED'}


class MADILIB_OT_mk_bind_camera(Operator):
    bl_idname = "madilib.mk_bind_camera"
    bl_label = "Bind Camera"
    bl_description = ("Bind the active camera to this marker, or clear the "
                      "binding if it already has one")
    bl_options = {'REGISTER', 'UNDO'}

    clear: bpy.props.BoolProperty(default=False, options={'HIDDEN'})

    def execute(self, context):
        marker = _active_marker(context)
        if marker is None:
            self.report({'WARNING'}, "No marker selected")
            return {'CANCELLED'}
        if self.clear:
            marker.camera = None
            return {'FINISHED'}
        obj = context.active_object
        if obj is None or obj.type != 'CAMERA':
            obj = context.scene.camera
        if obj is None:
            self.report({'WARNING'}, "Select a camera, or set the scene camera")
            return {'CANCELLED'}
        _uid_for(marker)
        marker.camera = obj
        return {'FINISHED'}


class MADILIB_OT_mk_bind_by_name(Operator):
    bl_idname = "madilib.mk_bind_by_name"
    bl_label = "Bind Cameras by Name"
    bl_description = ("Bind every marker to the camera that shares its name")
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        result = marker_bind_by_name()
        self.report({'INFO'}, "Bound %d marker(s)" % result["count"])
        return {'FINISHED'}


class MADILIB_OT_mk_set_layer_filter(Operator):
    """Show one layer's markers and put the rest away. Empty shows them all."""

    bl_idname = "madilib.mk_set_layer_filter"
    bl_label = "Show Layer"
    bl_description = ("Show only this layer's markers — the others are put "
                      "away, including on the timeline, until you show them "
                      "again")
    # ⚠ 'UNDO' IS NOW ON, and that is a change from when this only filtered a
    # list. It moves real markers in and out of the scene, so ctrl+Z has to be
    # able to reach it — an operator that edits the scene and is not undoable is
    # a trap, and this one can move every marker in the file.
    bl_options = {'REGISTER', 'UNDO', 'INTERNAL'}

    layer: StringProperty(default="", options={'HIDDEN'})

    def execute(self, context):
        result = show_layer(self.layer, context.scene)
        if result["hidden"]:
            self.report({'INFO'}, "Showing '%s' — %d marker(s) put away"
                        % (self.layer, result["hidden"]))
        return {'FINISHED'}


class MADILIB_OT_mk_set_save(Operator):
    bl_idname = "madilib.mk_set_save"
    bl_label = "Save Marker Set"
    bl_description = ("Save every marker in this file under a name, inside the "
                      ".blend")
    bl_options = {'REGISTER', 'UNDO'}

    name: StringProperty(name="Name", default="Markers")

    def execute(self, context):
        try:
            result = marker_set_save(self.name, context.scene)
        except RuntimeError as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        self.report({'INFO'}, "Saved '%s' (%d markers)"
                    % (result["saved"], result["count"]))
        return {'FINISHED'}

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)


class MADILIB_OT_mk_set_load(Operator):
    bl_idname = "madilib.mk_set_load"
    bl_label = "Load Marker Set"
    bl_description = "Replace every marker in the scene with this saved set"
    bl_options = {'REGISTER', 'UNDO'}

    name: StringProperty(default="", options={'HIDDEN'})

    def execute(self, context):
        try:
            result = marker_set_load(self.name, context.scene)
        except RuntimeError as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        self.report({'INFO'}, "Loaded '%s' (%d markers)"
                    % (result["loaded"], result["count"]))
        return {'FINISHED'}

    def invoke(self, context, event):
        # ⚠ It REPLACES every marker in the file, so it asks. The undo step
        # exists too, but a confirmation is what stops the mistake.
        return context.window_manager.invoke_confirm(self, event)


class MADILIB_OT_mk_set_delete(Operator):
    bl_idname = "madilib.mk_set_delete"
    bl_label = "Delete Marker Set"
    bl_description = "Forget this saved set (the markers in the scene stay)"
    bl_options = {'REGISTER', 'UNDO'}

    name: StringProperty(default="", options={'HIDDEN'})

    def execute(self, context):
        marker_set_delete(self.name, context.scene)
        return {'FINISHED'}


class MADILIB_MT_mk_sets(Menu):
    """The marker sets saved in this .blend."""

    bl_idname = "MADILIB_MT_mk_sets"
    bl_label = "Marker Sets"

    def draw(self, context):
        layout = self.layout
        layout.operator("madilib.mk_set_save", text="Save current markers as…",
                        icon='FILE_TICK')
        names = sorted(marker_sets(context.scene))
        if not names:
            layout.separator()
            layout.label(text="Nothing saved in this file yet", icon='INFO')
            return
        layout.separator()
        for name in names:
            layout.operator("madilib.mk_set_load", text=name,
                            icon='IMPORT').name = name
        layout.separator()
        for name in names:
            layout.operator("madilib.mk_set_delete", text="Delete %s" % name,
                            icon='X').name = name


class MADILIB_MT_mk_layers(Menu):
    """The layers currently in use, plus the way back to all of them."""

    bl_idname = "MADILIB_MT_mk_layers"
    bl_label = "Layer"

    def draw(self, context):
        layout = self.layout
        current = _shown_layer(context.scene)
        op = layout.operator("madilib.mk_set_layer_filter", text="All layers",
                             icon='CHECKMARK' if not current else 'BLANK1')
        op.layer = ""
        names = layers_in_use(context.scene)
        if not names:
            layout.separator()
            # An empty state that explains itself beats a menu with one entry.
            layout.label(text="No layers yet — type one on a marker",
                         icon='INFO')
            return
        layout.separator()
        for name in names:
            op = layout.operator(
                "madilib.mk_set_layer_filter", text=name,
                icon='CHECKMARK' if name == current else 'BLANK1')
            op.layer = name


class MADILIB_MT_mk_tools(Menu):
    bl_idname = "MADILIB_MT_mk_tools"
    bl_label = "Marker Tools"

    def draw(self, context):
        ui = context.window_manager.madilib_mk
        layout = self.layout
        layout.prop(ui, "sort_mode")
        layout.separator()
        layout.operator("madilib.mk_bind_by_name", icon='OUTLINER_OB_CAMERA')
        layout.operator("madilib.mk_bind_camera", text="Clear Camera",
                        icon='X').clear = True


# ----------------------------------------------------------------- panel


class _MarkerPanel:
    """The panel body, shared by both places it appears.

    ⚠ A PANEL CANNOT BE IN TWO EDITORS AT ONCE — `bl_space_type` is a single
    value, so appearing in both the Dope Sheet and the 3D viewport means two
    registered classes over one mixin. Don't "simplify" this back into one
    class; the second editor silently disappears if you do.
    """

    bl_label = "Timeline Markers"
    bl_region_type = 'UI'
    bl_category = "MadihsonNSFW"

    def draw(self, context):
        """The list and nothing else — Marty's "B1", 2026-08-12.

        Everything about ONE marker moved into the child panel below, because
        the detail box roughly doubled this panel's height and it now lives in
        the viewport sidebar where that space is contested. The child collapses;
        the list is what you always want.
        """
        layout = self.layout
        scene = context.scene
        ui = context.window_manager.madilib_mk

        row = layout.row(align=True)
        row.menu("MADILIB_MT_mk_sets", text="Marker sets", icon='PRESET')
        row.menu("MADILIB_MT_mk_tools", text="", icon='DOWNARROW_HLT')

        shown = _shown_layer(scene)
        row = layout.row(align=True)
        row.menu("MADILIB_MT_mk_layers", text=shown or "All layers",
                 icon='OUTLINER')
        if shown:
            row.operator("madilib.mk_set_layer_filter", text="",
                         icon='X').layer = ""

        hidden = len(parked(scene))
        if hidden:
            # Say it out loud. Markers missing from the timeline with nothing
            # explaining why is indistinguishable from having lost them.
            row = layout.row()
            row.alert = True
            row.label(text="%d marker(s) hidden by this layer" % hidden,
                      icon='INFO')

        layout.prop(ui, "filter_text", text="", icon='VIEWZOOM')
        layout.template_list("MADILIB_UL_markers", "", scene, "timeline_markers",
                             ui, "active_index", rows=5)

        row = layout.row(align=True)
        row.operator("madilib.mk_add", text="Add", icon='ADD')
        row.operator("madilib.mk_remove", text="Remove", icon='REMOVE')


class _MarkerDetails:
    """Everything about the selected marker, as a collapsible child panel."""

    bl_label = "Marker details"
    bl_region_type = 'UI'
    bl_category = "MadihsonNSFW"

    def draw(self, context):
        layout = self.layout
        marker = _active_marker(context)
        if marker is None:
            layout.label(text="No marker selected")
            return

        col = layout.column(align=True)
        col.prop(marker, "frame", text="Frame")
        row = col.row(align=True)
        if marker.camera:
            row.label(text=marker.camera.name, icon='OUTLINER_OB_CAMERA')
            row.operator("madilib.mk_bind_camera", text="",
                         icon='X').clear = True
        else:
            row.operator("madilib.mk_bind_camera", text="Bind camera",
                         icon='OUTLINER_OB_CAMERA').clear = False

        col = layout.column(align=True)
        col.label(text="Layer")
        col.prop(marker, "madi_layer", text="")
        col.separator()
        col.label(text="Tags")
        col.prop(marker, "madi_tags", text="")
        col.separator()
        col.label(text="Note")
        col.prop(marker, "madi_note", text="")

        row = layout.row(align=True)
        row.operator("madilib.mk_jump", text="Jump", icon='TIME')
        row.operator("madilib.mk_render", text="Render", icon='RENDER_STILL')

class MADILIB_PT_markers(_MarkerPanel, Panel):
    """In the Timeline / Dope Sheet sidebar — where the markers themselves are.

    ⚠ `bl_space_type = 'DOPESHEET_EDITOR'` covers the TIMELINE too — Blender's
    Timeline is a Dope Sheet in disguise, so this one registration serves both,
    and registering a third panel for a 'TIMELINE' space type would silently do
    nothing (there is no such space type).
    """

    bl_idname = "MADILIB_PT_markers"
    bl_space_type = 'DOPESHEET_EDITOR'


class MADILIB_PT_markers_view3d(_MarkerPanel, Panel):
    """And in the 3D viewport's N-panel, beside Studio Library.

    Marty, 2026-08-12: *"timeline markers should be in the same UI in blender as
    Studio Library"* — he had already failed to find the Dope Sheet one twice,
    and every other MadihsonNSFW panel lives here, so this is where his hand
    goes. Deliberately NOT `DEFAULT_CLOSED`: the complaint being fixed is that
    the panel could not be found, and a collapsed panel is the same complaint
    again. He can fold it himself.
    """

    bl_idname = "MADILIB_PT_markers_view3d"
    bl_space_type = 'VIEW_3D'


# ⚠ A CHILD PANEL IS PINNED TO ONE PARENT BY ID, so two parents means two
# children as well — `bl_parent_id` cannot name both. They must also declare
# the same `bl_space_type` as their parent or they simply never appear, with no
# error to explain it.
class MADILIB_PT_marker_details(_MarkerDetails, Panel):
    bl_idname = "MADILIB_PT_marker_details"
    bl_space_type = 'DOPESHEET_EDITOR'
    bl_parent_id = "MADILIB_PT_markers"


class MADILIB_PT_marker_details_view3d(_MarkerDetails, Panel):
    bl_idname = "MADILIB_PT_marker_details_view3d"
    bl_space_type = 'VIEW_3D'
    bl_parent_id = "MADILIB_PT_markers_view3d"


_classes = (
    MADILIB_MarkerUI,
    MADILIB_UL_markers,
    MADILIB_OT_mk_jump,
    MADILIB_OT_mk_add,
    MADILIB_OT_mk_remove,
    MADILIB_OT_mk_render,
    MADILIB_OT_mk_bind_camera,
    MADILIB_OT_mk_bind_by_name,
    MADILIB_OT_mk_set_layer_filter,
    MADILIB_OT_mk_set_save,
    MADILIB_OT_mk_set_load,
    MADILIB_OT_mk_set_delete,
    MADILIB_MT_mk_layers,
    MADILIB_MT_mk_sets,
    MADILIB_MT_mk_tools,
    # ⚠ PARENTS BEFORE CHILDREN. A child panel whose `bl_parent_id` is not
    # registered yet fails to register at all.
    MADILIB_PT_markers,
    MADILIB_PT_markers_view3d,
    MADILIB_PT_marker_details,
    MADILIB_PT_marker_details_view3d,
)


def register():
    # ⚠ THE PROPERTIES COME FIRST. A panel that draws `marker.madi_note` before
    # the property exists throws on the first redraw, and the traceback names
    # the panel rather than this ordering.
    bpy.types.TimelineMarker.madi_uid = StringProperty(
        name="Id", default="",
        description="Stable id, so the app can address this marker even "
                    "though marker names are not unique")
    bpy.types.TimelineMarker.madi_note = StringProperty(
        name="Note", default="",
        description="A note that travels with this marker")
    bpy.types.TimelineMarker.madi_tags = StringProperty(
        name="Tags", default="",
        description="Comma-separated tags, for filtering")
    bpy.types.TimelineMarker.madi_layer = StringProperty(
        name="Layer", default="",
        description="The layer this marker belongs to. Pick a layer above to "
                    "show only its markers; with none picked they all show")
    for cls in _classes:
        bpy.utils.register_class(cls)
    bpy.types.WindowManager.madilib_mk = bpy.props.PointerProperty(
        type=MADILIB_MarkerUI)
    # ⚠ Guarded against a double-add: `register()` is also what an add-on
    # RELOAD calls, and a handler added twice restores twice.
    if _restore_on_load not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(_restore_on_load)


def unregister():
    # ⚠ Removed FIRST and unconditionally. A load handler that outlives its
    # module keeps firing from dead code and takes the next file open with it —
    # the same rule the picker and MadiRef follow for their handlers.
    if _restore_on_load in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_restore_on_load)
    del bpy.types.WindowManager.madilib_mk
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
    # ⚠ The per-marker properties are NOT deleted from the type on unregister.
    # The VALUES live in the .blend either way (measured), but removing the
    # property definition while a file is open makes every stored note
    # unreachable until the add-on is registered again — including to the
    # save that is about to happen. Leaving the definition costs nothing.
