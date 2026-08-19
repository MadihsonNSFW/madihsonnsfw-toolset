"""Rig properties — a rig's custom properties, read and keyed over the bridge.

The app's Organize ▸ Rig properties page ("Channels", the option Marty picked
from four mockups on 2026-08-19) shows every custom property of an armature:
its value, WHERE its keys are on the timeline, and a diamond per row. This
module is the whole Blender side of it — reads, writes, and nothing else.
There is **no panel and no operator here on purpose**: Blender already draws
these in Item ▸ Properties, and the app was what Marty asked to add.

===========================================================================
THE PROTOCOL, AND WHY IT IS NOT `sets_list`'s
===========================================================================
Organize polls `sets_list`, which returns everything every time. That is fine
for a handful of sets. A Daz rig has **775 custom properties** and the full
answer is **97 KB of JSON** — sending that every 1.5 s would make the tab the
most expensive thing in the app, and almost every poll would be identical.

So this route is a **three-tier read**, and the caller says what it already
has:

  1. **`revision` matches** → `{"revision": r, "unchanged": True}`, about 60
     bytes. The common poll. Costs one cheap scan (~0.25 ms) and no JSON.
  2. **`shape` matches, revision moved** → values and keys only, positionally
     aligned to the rows the caller already has. ~15 KB.
  3. **neither** → the full answer, including the per-property ranges,
     defaults and labels. ~97 KB, and the only tier that pays for them.

⚠ **`shape` IS THE ALIGNMENT CONTRACT.** `values` is a bare array lined up
with `rows`, so a caller that reuses cached rows against a mismatched array
would show every value against the wrong name. `shape` hashes the names and
kinds, and `count` is sent beside it so the caller can refuse a mismatch
rather than draw a lie.

⚠ **A SOFT RANGE EDIT DOES NOT MOVE `shape`.** Editing a property's min/max
in Blender's Edit Property dialog changes neither its name nor its kind, and
reading every range costs 0.65 ms — too much to do on a poll just to notice
something that happens once a month. `full=True` re-reads everything; the
app's Refresh button sends it.

===========================================================================
⚠⚠ SETTING AN ID PROPERTY DOES NOT TAG THE OBJECT — DRIVERS DO NOT SEE IT
===========================================================================
`obj["Morph"] = 0.5` on its own leaves every driver reading `["Morph"]`
evaluating the OLD value, however many depsgraph updates go past. Measured on
5.2.0 LTS, 2026-08-19: with a driver `m * 2` on a mesh, a plain write left the
mesh at the old location and `obj.update_tag()` moved it. Every Daz morph is
driven this way, so a write without the tag would look like the app was
ignoring the slider.

⚠ **`update_tag()`, NOT `view_layer.update()`.** The tag alone is enough, and
it costs **0.0013 ms** against **0.70 ms** for a view-layer update — 500×.
That gap is the whole difference between a slider that drags smoothly and one
that stutters, because a drag sends a write per mouse move.

===========================================================================
Other things that were measured rather than assumed (5.2.0 LTS, 2026-08-19)
===========================================================================
  * **Which properties are "custom"** is `obj.keys()` minus the RUNTIME rna
    properties — exactly what `rna_prop_ui.draw` hides unless Developer Extras
    is on. The runtime set is a property of `bpy.types.Object`, identical for
    every object, so it is cached (`_registered_names`).
  * **`Action.fcurves` does not exist in 5.2** — slotted actions only. The
    path is `action.layers[0].strips[0].channelbag(slot).fcurves`
    (`BLENDER_NOTES.md`, 2026-07-31).
  * **Look the F-curves up by INDEXING, not by `find()`.** One dict pass over
    the channelbag is 0.14 ms; calling `fcurves.find()` once per property is
    0.78 ms for 775 — and that gap grows with the product of the two counts,
    not their sum.
  * **`id_properties_ui()` RAISES `TypeError`** for a property that cannot
    carry UI data (an `IDPropertyGroup` — Simplicage keeps one on Marty's
    rigs). Caught, not guarded by type.
  * **A bool's UI dict has no min/max at all**, only `default` and `subtype`.
  * Daz hard ranges are ±FLT_MAX on essentially every morph, so the SOFT
    range is the one to draw — the same choice Blender's own slider makes.
  * `bpy.utils.escape_identifier` exists and is what makes `["a\\"b"]` a legal
    data path. Every path here goes through it.
"""

import math

import bpy

# The channel group new keys land in, so the Dope Sheet stays readable next to
# the bone channels rather than growing 775 loose rows.
KEY_GROUP = "Rig properties"

# Caps. The bridge is reachable by anything that can open the socket
# (`docs\\security.md`), so every route is written as if a stranger were
# calling: this bounds one request, not the user's own work.
NAMES_MAX = 4096
# Frames outside this are refused rather than clamped — a caller asking for
# frame 1e9 has made a mistake worth reporting.
FRAME_MIN, FRAME_MAX = -1000000, 1000000
# Beyond this a "soft range" is Blender's ±FLT_MAX stand-in for "unbounded",
# and a slider drawn across it would move by 1e36 per pixel.
UNBOUNDED = 1e30

# The registered (runtime) property names of bpy.types.Object, cached.
# ⚠ Keyed on the property COUNT: registering an add-on's Object property
# changes it, and that is the only way this set moves. Two add-ons swapping
# one property for another between two polls would go unnoticed — `full=True`
# rebuilds it, and rebuilding costs 0.04 ms if that ever matters.
_RNA_CACHE = {"count": -1, "names": frozenset()}


def _registered_names(force=False):
    props = bpy.types.Object.bl_rna.properties
    count = len(props)
    if force or _RNA_CACHE["count"] != count:
        _RNA_CACHE["count"] = count
        _RNA_CACHE["names"] = frozenset(p.identifier for p in props
                                        if p.is_runtime)
    return _RNA_CACHE["names"]


def _redraw(keys=False):
    """Ask the editors to repaint. ⚠⚠ **THIS IS HALF OF A WRITE, NOT POLISH.**

    `update_tag()` marks the ID dirty; it does not ask anything to DRAW. When
    Blender's own UI edits a custom property it does both — the RNA update
    path tags the ID *and* sends a window notifier. A write over the bridge
    comes off a timer with no notifier behind it, so the value lands, the
    depsgraph re-evaluates on the next flush, and the 3D viewport goes on
    showing the old shape until the user happens to move the mouse over it.

    Marty, 2026-08-19: *"when changing values in app it changes values in
    blender but they don't do what they meant to, but when i try in blender UI
    it works"* — that is exactly this, and it is why the value being provably
    correct in the data was not the same as the feature working.

    `keys=True` also repaints the animation editors, which is what makes a key
    inserted from the app appear in the Dope Sheet without a click.
    """
    window_manager = bpy.context.window_manager
    if window_manager is None:
        return
    wanted = {'VIEW_3D', 'PROPERTIES'}
    if keys:
        wanted |= {'DOPESHEET_EDITOR', 'GRAPH_EDITOR', 'TIMELINE', 'NLA_EDITOR'}
    for window in window_manager.windows:
        screen = getattr(window, "screen", None)
        if screen is None:
            continue
        for area in screen.areas:
            if area.type in wanted:
                area.tag_redraw()


def _kind(value):
    """float | int | bool for what we can drive, None for what we cannot.

    ⚠ **bool IS CHECKED FIRST.** `isinstance(True, int)` is True in Python, so
    the obvious order silently turns every switch on the rig into a number
    field.
    """
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    return None


def _path(name):
    """The data path of a custom property, escaped. ⚠ Not `'["%s"]' % name` —
    a property called `a"b` is legal and would build a broken path."""
    return '["%s"]' % bpy.utils.escape_identifier(name)


def _rigs(scene=None):
    scene = scene or bpy.context.scene
    return [obj.name for obj in scene.objects if obj.type == 'ARMATURE']


def _resolve(rig=None, scene=None):
    """Which armature to report on: the one asked for, else the active object
    when it IS an armature, else the first in the scene.

    ⚠ Clicking a MESH does not change the rig. That is deliberate — a
    character's meshes are what you click most, and following them would empty
    the page constantly. "Follow active" means "follow the active RIG".
    """
    scene = scene or bpy.context.scene
    if rig:
        obj = scene.objects.get(rig)
        if obj is None or obj.type != 'ARMATURE':
            return None
        return obj
    active = bpy.context.view_layer.objects.active
    if active is not None and active.type == 'ARMATURE':
        return active
    for obj in scene.objects:
        if obj.type == 'ARMATURE':
            return obj
    return None


def _channelbag(obj):
    """The F-curve holder for an object's own action, or None.

    ⚠ `Action.fcurves` is GONE in 5.2 — slotted actions only. This is the
    documented replacement, and every part of it can be absent: no animation
    data, no action, no slot, no layer.
    """
    ad = obj.animation_data
    if ad is None or ad.action is None:
        return None
    slot = ad.action_slot
    if slot is None:
        return None
    try:
        layer = ad.action.layers[0]
        strip = layer.strips[0]
    except IndexError:
        return None
    try:
        return strip.channelbag(slot)
    except (AttributeError, TypeError, RuntimeError):
        return None


def _key_index(obj):
    """`{property name: [frames]}` for the object's CUSTOM-PROPERTY curves.

    ⚠ ONE PASS, building a dict — not `fcurves.find()` per property. Measured
    2026-08-19 on a 484-curve action: 0.14 ms here against 0.78 ms for 775
    `find()` calls, and the second number is O(properties × curves).

    ⚠ **UNESCAPE THE FEW PATHS, do not escape the many names.** The obvious
    way round is to build `{_path(name): name}` for all 775 properties on
    every poll; this walks the handful of curves that exist instead. Same
    answer, and it does no work at all on a rig with nothing animated.

    ⚠ `foreach_get` reads the coordinates in one C call per curve; looping
    `keyframe_points` in Python is what makes a heavily-keyed rig crawl.
    """
    bag = _channelbag(obj)
    if bag is None:
        return {}
    out = {}
    for curve in bag.fcurves:
        path = curve.data_path
        # Custom-property curves are the only ones whose path starts `["`;
        # everything else on a rig is `pose.bones[...]` and is not ours.
        if not path.startswith('["') or not path.endswith('"]'):
            continue
        points = curve.keyframe_points
        count = len(points)
        if not count:
            continue
        try:
            name = bpy.utils.unescape_identifier(path[2:-2])
        except (RuntimeError, TypeError):
            continue
        flat = [0.0] * (count * 2)
        points.foreach_get("co", flat)
        # co is (frame, value); the strip only draws the frame.
        # ⚠ One curve per property here, but an object CAN carry two curves
        # for the same path in a broken file — merge rather than overwrite,
        # so the strip shows every key that really exists.
        frames = [int(round(f)) for f in flat[0::2]]
        if name in out:
            out[name] = sorted(set(out[name]) | set(frames))
        else:
            out[name] = frames
    return out


def _scan(obj, scene, force_rna=False):
    """The CHEAP pass: names, kinds, values, keys, and the two hashes.

    Everything the poll needs and nothing it does not — no ranges, no
    defaults, no labels, because those cost 0.65 ms and never move. Measured
    at ~0.25 ms for 775 properties and a 484-curve action.
    """
    registered = _registered_names(force_rna)
    names, kinds, values = [], [], []
    skipped = 0
    for key in obj.keys():
        if key in registered:
            continue
        kind = _kind(obj[key])
        if kind is None:
            # Groups, arrays, strings and datablock pointers. Counted so the
            # app can say so, never half-drawn.
            skipped += 1
            continue
        names.append(key)
        kinds.append(kind)
        values.append(obj[key])
    # ⚠ KEYS ARE NARROWED TO PROPERTIES THAT STILL EXIST. Deleting a custom
    # property in Blender leaves its F-curve behind (measured 2026-08-19 — the
    # curve outlives the property), so the raw index can name things the rows
    # do not. Left wide, the app would step the playhead to the keys of a
    # property nobody can see.
    live = set(names)
    keys = {name: frames for name, frames in _key_index(obj).items()
            if name in live}
    # ⚠ The revision must move whenever anything DRAWN moves, and not
    # otherwise — that is the whole contract with the poll. Drawn: the rig,
    # every value, the playhead, the strip's axis, and every key position.
    revision = hash((
        obj.name,
        scene.frame_current, scene.frame_start, scene.frame_end,
        tuple(names), tuple(values),
        tuple(sorted((name, tuple(frames)) for name, frames in keys.items())),
    )) & 0x7FFFFFFF
    # `shape` is the ALIGNMENT contract for the values array — names and kinds
    # only, so it survives a value change and a re-key.
    shape = hash((tuple(names), tuple(kinds))) & 0x7FFFFFFF
    return {"names": names, "kinds": kinds, "values": values, "keys": keys,
            "skipped": skipped, "revision": revision, "shape": shape}


def _describe(obj, names, kinds):
    """The EXPENSIVE pass: one row per property with its range, default and
    label. 0.65 ms for the ranges plus 0.18 ms for the Daz labels — paid only
    when the property set itself has changed.
    """
    labels = _labels(obj)
    rows = []
    for name, kind in zip(names, kinds):
        try:
            ui = obj.id_properties_ui(name).as_dict()
        except TypeError:
            # "does not support UI data" — real, and reached by Simplicage's
            # settings group on Marty's rigs.
            ui = {}
        row = {"name": name, "kind": kind}
        label = labels.get(name)
        if label and label != name:
            row["label"] = label
        default = ui.get("default")
        if isinstance(default, (bool, int, float)):
            row["default"] = default
        if kind != "bool":
            smin, smax = ui.get("soft_min"), ui.get("soft_max")
            bounded = (isinstance(smin, (int, float))
                       and isinstance(smax, (int, float))
                       and abs(smin) < UNBOUNDED and abs(smax) < UNBOUNDED
                       and smax > smin)
            if bounded:
                row["smin"], row["smax"] = smin, smax
            else:
                # ⚠ NOT invented bounds. The app draws an unbounded row as a
                # number you type or drag, never as a filled bar — a bar
                # implies a range, and Daz hard ranges are ±FLT_MAX.
                row["bounded"] = False
        rows.append(row)
    return rows


def _labels(obj):
    """Daz's human labels for its morphs, when the rig carries them.

    Diffeomorphic keeps parallel collections of `(name, text)` on the object.
    Absent on any other rig, which is what the getattr guards are for — and
    a plain `except TypeError` beside them, because an attribute of the same
    name that is NOT iterable (a bool, which `DazCustomMorphs` really is) must
    not take the read down.
    """
    out = {}
    for attr in ("DazUnits", "DazFacs", "DazBody", "DazFlexions",
                 "DazExpressions", "DazVisemes"):
        coll = getattr(obj, attr, None)
        if coll is None:
            continue
        try:
            for item in coll:
                out[item.name] = item.text
        except (TypeError, AttributeError):
            continue
    cats = getattr(obj, "DazMorphCats", None)
    if cats is not None:
        try:
            for cat in cats:
                for item in cat.morphs:
                    out[item.name] = item.text
        except (TypeError, AttributeError):
            pass
    return out


# =========================================================== read =========
def rig_props_list(rig=None, shape=None, revision=None, full=False):
    """PURE READ, and polled — it must never write. See the module docstring
    for the three tiers."""
    scene = bpy.context.scene
    obj = _resolve(rig, scene)
    if obj is None:
        return {"rig": None, "rigs": _rigs(scene), "rows": [], "values": [],
                "keys": {}, "revision": 0, "shape": 0, "count": 0,
                "frame": scene.frame_current, "start": scene.frame_start,
                "end": scene.frame_end, "skipped": 0,
                "reason": "No armature in this scene."}
    scan = _scan(obj, scene, force_rna=bool(full))
    reply = {"rig": obj.name, "revision": scan["revision"],
             "frame": scene.frame_current}
    if not full and revision is not None and int(revision) == scan["revision"]:
        # TIER 1 — the common poll. Nothing else is built, nothing is sent.
        reply["unchanged"] = True
        return reply
    reply.update({
        "rigs": _rigs(scene),
        "start": scene.frame_start, "end": scene.frame_end,
        "shape": scan["shape"], "count": len(scan["names"]),
        "skipped": scan["skipped"],
        "values": scan["values"],
        "keys": scan["keys"],
        "active": (bpy.context.view_layer.objects.active.name
                   if bpy.context.view_layer.objects.active else None),
    })
    if full or shape is None or int(shape) != scan["shape"]:
        # TIER 3 — the property set itself changed (or the caller has nothing).
        reply["rows"] = _describe(obj, scan["names"], scan["kinds"])
    # else TIER 2 — the caller's cached rows still line up with `values`.
    return reply


# =========================================================== writes =======
def _write_target(rig, name):
    """Resolve (object, value) for a write, or raise ValueError with a reason
    the app can show. One place, so every writer refuses the same things."""
    obj = _resolve(rig)
    if obj is None:
        raise ValueError("no such rig")
    if not isinstance(name, str) or not name:
        raise ValueError("no property named")
    if name in _registered_names():
        raise ValueError("not a custom property")
    try:
        value = obj[name]
    except KeyError:
        raise ValueError("no such property")
    if _kind(value) is None:
        raise ValueError("that property is not a number or a switch")
    return obj, value


def _names_arg(names):
    if names is None:
        return []
    if isinstance(names, str):
        names = [names]
    if len(names) > NAMES_MAX:
        raise ValueError("too many properties in one call")
    return [n for n in names if isinstance(n, str) and n]


def _frame_arg(frame, scene):
    if frame is None:
        return scene.frame_current
    try:
        frame = int(frame)
    except (TypeError, ValueError):
        raise ValueError("bad frame")
    if not FRAME_MIN <= frame <= FRAME_MAX:
        raise ValueError("frame out of range")
    return frame


def rig_props_set(rig, name, value):
    """Drive one property. ⚠ `update_tag()` is not optional — see the module
    docstring: without it every driver reading this property keeps the old
    value."""
    obj, current = _write_target(rig, name)
    kind = _kind(current)
    try:
        if kind == "bool":
            new = bool(value)
        elif kind == "int":
            new = int(value)
        else:
            new = float(value)
    except (TypeError, ValueError):
        raise ValueError("bad value")
    if kind == "float" and not math.isfinite(new):
        raise ValueError("bad value")
    if kind != "bool":
        # Respect the property's own HARD limits when it has real ones. The
        # soft range is what the app draws; the hard one is what the file
        # says is legal, and a caller is not the app.
        try:
            ui = obj.id_properties_ui(name).as_dict()
        except TypeError:
            ui = {}
        low, high = ui.get("min"), ui.get("max")
        if isinstance(low, (int, float)) and abs(low) < UNBOUNDED:
            new = max(new, low)
        if isinstance(high, (int, float)) and abs(high) < UNBOUNDED:
            new = min(new, high)
    obj[name] = new
    obj.update_tag()
    _redraw()
    return {"ok": True, "rig": obj.name, "name": name, "value": new,
            "revision": _scan(obj, bpy.context.scene)["revision"]}


def rig_props_key(rig, names=None, frame=None):
    """Insert a key on each named property at `frame` (default: the playhead)."""
    obj = _resolve(rig)
    if obj is None:
        raise ValueError("no such rig")
    scene = bpy.context.scene
    frame = _frame_arg(frame, scene)
    registered = _registered_names()
    done, missed = [], []
    for name in _names_arg(names):
        if name in registered or name not in obj.keys():
            missed.append(name)
            continue
        if _kind(obj[name]) is None:
            missed.append(name)
            continue
        try:
            if obj.keyframe_insert(_path(name), frame=frame, group=KEY_GROUP):
                done.append(name)
            else:
                missed.append(name)
        except (TypeError, RuntimeError):
            missed.append(name)
    _redraw(keys=True)
    return {"ok": True, "rig": obj.name, "frame": frame, "keyed": done,
            "missed": missed,
            "revision": _scan(obj, scene)["revision"]}


def rig_props_unkey(rig, names=None, frame=None, whole=False):
    """Delete this frame's key on each named property, or (`whole`) every key
    it has.

    ⚠ `whole` removes the F-CURVE, it does not loop the frames: one call
    against one per key, and it leaves no empty channel behind. The VALUE is
    left exactly where it is — un-keying is not undoing.
    """
    obj = _resolve(rig)
    if obj is None:
        raise ValueError("no such rig")
    scene = bpy.context.scene
    frame = None if whole else _frame_arg(frame, scene)
    names = _names_arg(names)
    done, missed = [], []
    if whole:
        bag = _channelbag(obj)
        wanted = {_path(name): name for name in names}
        if bag is not None:
            for curve in list(bag.fcurves):
                name = wanted.get(curve.data_path)
                if name is not None:
                    bag.fcurves.remove(curve)
                    done.append(name)
        missed = [n for n in names if n not in done]
    else:
        for name in names:
            try:
                if obj.keyframe_delete(_path(name), frame=frame):
                    done.append(name)
                else:
                    missed.append(name)
            except (TypeError, RuntimeError):
                # No F-curve at all: nothing to delete is not an error.
                missed.append(name)
    _redraw(keys=True)
    return {"ok": True, "rig": obj.name, "frame": frame, "unkeyed": done,
            "missed": missed,
            "revision": _scan(obj, scene)["revision"]}


def rig_props_reset(rig, names=None):
    """Put each named property back to the default its UI data records."""
    obj = _resolve(rig)
    if obj is None:
        raise ValueError("no such rig")
    registered = _registered_names()
    done = []
    for name in _names_arg(names):
        if name in registered or name not in obj.keys():
            continue
        kind = _kind(obj[name])
        if kind is None:
            continue
        try:
            default = obj.id_properties_ui(name).as_dict().get("default")
        except TypeError:
            default = None
        if not isinstance(default, (bool, int, float)):
            default = False if kind == "bool" else (0 if kind == "int" else 0.0)
        obj[name] = bool(default) if kind == "bool" else (
            int(default) if kind == "int" else float(default))
        done.append(name)
    if done:
        obj.update_tag()           # once for the batch, not once per property
        _redraw()
    return {"ok": True, "rig": obj.name, "reset": done,
            "revision": _scan(obj, bpy.context.scene)["revision"]}


def rig_props_frame(frame):
    """Move the playhead — what the strip's diamonds and ◀ ▶ do."""
    scene = bpy.context.scene
    frame = _frame_arg(frame, scene)
    scene.frame_set(frame)
    _redraw(keys=True)
    return {"ok": True, "frame": scene.frame_current}
