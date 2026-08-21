"""Bake a mesh's modifier deformation into per-frame shape keys.

Marty, 2026-08-21, after the retopo-cage workflow finally worked end to end:
*"build this tool 'bake deformation to shape keys', make it so it bakes on
current frame range"*.

WHAT IT IS FOR. A Quadify cage follows a character with a Surface Deform, which
is exact but occupies the modifier stack. **Shape keys evaluate BEFORE
modifiers**, so baking the motion into keys frees the stack for Cloth — which is
the whole reason the cage exists. Measured on Marty's own character: the bake
reproduces the modifier's motion at **0.0 error on every frame**.

⚠⚠ **EVERY FRAME IS READ BEFORE ANY KEY IS CREATED, AND THAT ORDER IS THE
WHOLE CORRECTNESS ARGUMENT.** Evaluating an object also evaluates the shape
keys already on it. Create key N and then read frame N+1 and you have folded
key N into the reading — every frame after the first would be wrong, and
wrong in a way that looks like drift rather than like a bug.

⚠ **The keys hold ABSOLUTE positions, not offsets.** `key_blocks[...].data` is
the whole mesh at that key, which is exactly what an evaluated read gives, so
nothing is subtracted anywhere.

⚠ **One key per frame, keyed 1.0 on its own frame and 0.0 at its neighbours,
LINEAR.** That is exact on integer frames; a sub-frame position is a linear
blend of the two frames either side, which is what any per-frame bake gives.

⚠⚠ **BLENDER 5.x ACTIONS ARE SLOTTED.** `action.fcurves` does not exist any
more — the curves live at `action.layers[].strips[].channelbag(slot).fcurves`.
Reaching for `.fcurves` raises `AttributeError` and the bake dies after having
already created the keys, which leaves the object in a half-baked state. See
`_fcurves`, which keeps the old path for older Blenders.

⚠ **The modifiers are DISABLED, never deleted.** A bake that removes the thing
it baked cannot be checked afterwards, and a wrong frame range is otherwise
unrecoverable. The caller asks for removal explicitly.
"""

import bpy


def _fcurves(datablock):
    """Every F-curve on a datablock's action, slotted or not."""
    anim = getattr(datablock, "animation_data", None)
    if anim is None or anim.action is None:
        return []
    action = anim.action
    if hasattr(action, "fcurves"):              # Blender 4.x and earlier
        return list(action.fcurves)
    out = []
    for layer in action.layers:
        for strip in layer.strips:
            if strip.type != "KEYFRAME":
                continue
            bag = strip.channelbag(anim.action_slot)
            if bag is not None:
                out.extend(bag.fcurves)
    return out


def _deforming(ob):
    """Modifiers that are actually contributing something to bake."""
    return [m for m in ob.modifiers if m.show_viewport]


def bake_status(object_name=""):
    """What the tool needs to draw itself. Cheap, and it never writes."""
    scene = bpy.context.scene
    ob = bpy.data.objects.get(object_name) if object_name \
        else bpy.context.active_object
    out = {"object": "", "frame_start": scene.frame_start,
           "frame_end": scene.frame_end, "verts": 0, "modifiers": [],
           "shape_keys": 0, "shared": 0, "running": False}
    if ob is None or ob.type != "MESH":
        return out
    keys = ob.data.shape_keys
    out.update({
        "object": ob.name,
        "verts": len(ob.data.vertices),
        "modifiers": [{"name": m.name, "type": m.type} for m in _deforming(ob)],
        "shape_keys": len(keys.key_blocks) if keys else 0,
        # ⚠ Read here so the DELETE confirmation can say "2 objects share this
        # mesh" BEFORE the keys go, not in the report afterwards. A warning
        # that arrives after the deletion is not a warning.
        "shared": sum(1 for other in bpy.data.objects
                      if other.data is ob.data),
    })
    # ⚠ What the bake will COST, said before it is pressed: one key holds a
    # full copy of the mesh, so this grows with frames x vertices.
    frames = max(0, scene.frame_end - scene.frame_start + 1)
    out["frames"] = frames
    out["estimated_mb"] = round(frames * len(ob.data.vertices) * 12
                                / (1024.0 * 1024.0), 1)
    return out


def bake_to_shape_keys(object_name="", start=None, end=None, step=1,
                       prefix="Bake", disable_modifiers=True,
                       remove_modifiers=False):
    """Bake the evaluated deformation into one shape key per frame.

    Returns a report. ⚠ It says what was DONE — the keys that exist and the
    modifiers that were switched off — never what was asked for.
    """
    scene = bpy.context.scene
    ob = bpy.data.objects.get(object_name) if object_name \
        else bpy.context.active_object
    if ob is None or ob.type != "MESH":
        return {"ok": False, "error": "select a mesh object"}

    first = scene.frame_start if start is None else int(start)
    last = scene.frame_end if end is None else int(end)
    if last < first:
        return {"ok": False, "error": "the frame range ends before it starts"}
    step = max(1, int(step))
    frames = list(range(first, last + 1, step))
    if last not in frames:
        frames.append(last)                     # never drop the last frame

    modifiers = _deforming(ob)
    if not modifiers:
        return {"ok": False,
                "error": "'%s' has no enabled modifiers to bake" % ob.name}

    count = len(ob.data.vertices)
    started = scene.frame_current
    captured = []
    try:
        # ⚠⚠ READ EVERYTHING FIRST. See the module docstring: creating a key
        # changes what the next frame's reading returns.
        for frame in frames:
            scene.frame_set(frame)
            depsgraph = bpy.context.evaluated_depsgraph_get()
            ob_eval = ob.evaluated_get(depsgraph)
            mesh = ob_eval.to_mesh()
            # ⚠ Read the count BEFORE clearing. `to_mesh_clear()` frees the
            # datablock, and touching it afterwards raises "StructRNA of type
            # Mesh has been removed" — so the refusal crashed instead of
            # refusing, which is the worst possible way for a guard to fail.
            got = len(mesh.vertices)
            if got != count:
                ob_eval.to_mesh_clear()
                return {"ok": False, "error":
                        "frame %d evaluates to %d vertices, not %d - a "
                        "modifier is changing the topology, and a shape key "
                        "cannot hold that" % (frame, got, count)}
            flat = [0.0] * (count * 3)
            mesh.vertices.foreach_get("co", flat)
            ob_eval.to_mesh_clear()
            captured.append((frame, flat))
    finally:
        scene.frame_set(started)

    if ob.data.shape_keys is None:
        ob.shape_key_add(name="Basis", from_mix=False)

    made = []
    for frame, flat in captured:
        block = ob.shape_key_add(name="%s_%04d" % (prefix, frame),
                                 from_mix=False)
        block.data.foreach_set("co", flat)
        block.slider_min, block.slider_max = 0.0, 1.0
        block.value = 0.0
        made.append((frame, block))

    # One key on at its own frame, off at the frames either side.
    for index, (frame, block) in enumerate(made):
        previous = made[index - 1][0] if index else frame - step
        following = made[index + 1][0] if index + 1 < len(made) else frame + step
        for at, value in ((previous, 0.0), (frame, 1.0), (following, 0.0)):
            block.value = value
            block.keyframe_insert("value", frame=at)
        block.value = 0.0
    for fcurve in _fcurves(ob.data.shape_keys):
        for point in fcurve.keyframe_points:
            point.interpolation = "LINEAR"

    touched = []
    if remove_modifiers:
        for modifier in modifiers:
            touched.append(modifier.name)
            ob.modifiers.remove(modifier)
    elif disable_modifiers:
        for modifier in modifiers:
            modifier.show_viewport = False
            modifier.show_render = False
            touched.append(modifier.name)

    scene.frame_set(started)
    ob.update_tag()
    _redraw()
    return {"ok": True, "object": ob.name, "keys": len(made),
            "frame_start": first, "frame_end": last, "step": step,
            "modifiers": touched,
            "removed": bool(remove_modifiers),
            "verts": count,
            "first_key": made[0][1].name if made else "",
            "last_key": made[-1][1].name if made else ""}


def clear_shape_keys(object_name=""):
    """Remove every shape key from a mesh, Basis included.

    Marty, 2026-08-21: *"in the 'Bake to shape keys' menu we also need a button
    that delets all shapekeys from selected mesh"* — the way back out of a bake
    with the wrong frame range, and the way to re-bake without 250 dead keys
    underneath the new ones.

    ⚠⚠ **REFUSED IN EDIT MODE, and refusing is not this module being fussy.**
    Blender's own `object.shape_key_remove` polls **False** in edit mode
    (verified live), but `shape_key_clear()` is an RNA call and polls nothing:
    it returns without a word while a bmesh is holding the shape layers. A
    write that succeeds silently under a live bmesh is the quiet kind of data
    loss, so the mode is checked here instead.

    ⚠⚠ **THE MESH RETURNS TO ITS BASE COORDINATES, NOT TO BASIS.** Clearing
    DISCARDS Basis rather than applying it — measured: a Basis moved to z=5
    left the mesh sitting at z=0. After a bake that is exactly right, because
    the base mesh *is* the cage as it was remeshed. On a mesh whose Basis was
    sculpted away from the underlying coordinates it is a visible jump, which
    is why the panel says so before the button is pressed.

    ⚠ **Shape keys belong to the MESH, so every object sharing it loses them.**
    The report says how many objects that is rather than deciding for anyone.

    ⚠ The keys' action is left behind with zero users, exactly as Blender's own
    operator leaves it; it goes at the next save. Removing it here would be
    this module deciding to delete a datablock nobody asked it to touch.

    ⚠ Nothing is pushed onto Blender's undo stack — no route in this add-on
    does — so the report is the only record that this happened.
    """
    ob = bpy.data.objects.get(object_name) if object_name \
        else bpy.context.active_object
    # ⚠ A NAME that is not there and NOTHING SELECTED are different problems
    # and need different sentences. "Select a mesh object" is useless advice
    # to someone who named one and had it renamed or deleted underneath them.
    if ob is None and object_name:
        return {"ok": False,
                "error": "there is no object called '%s'" % object_name}
    if ob is None or ob.type != "MESH":
        return {"ok": False, "error": "select a mesh object"}
    if ob.mode != "OBJECT":
        return {"ok": False, "error":
                "'%s' is in %s mode - shape keys can only be removed in "
                "object mode" % (ob.name, ob.mode.lower())}

    keys = ob.data.shape_keys
    blocks = list(keys.key_blocks) if keys else []
    # ⚠ Not an error. The outcome asked for is already true, and saying "there
    # were none" is more use than a refusal that reads like a failure.
    names = [block.name for block in blocks]
    # ⚠ Counted off the OBJECTS, not `mesh.users` — a fake user is a user too,
    # and "2 objects share this" is the sentence the panel has to be able to
    # say truthfully.
    shared = sum(1 for other in bpy.data.objects if other.data is ob.data)
    if blocks:
        ob.shape_key_clear()
        ob.update_tag()
        _redraw()
    return {"ok": True, "object": ob.name, "removed": len(names),
            "shared": shared, "verts": len(ob.data.vertices),
            "first_key": names[0] if names else "",
            "last_key": names[-1] if names else ""}


def _redraw():
    """⚠ A bridge write has no notifier behind it, so nothing repaints on its
    own — the same lesson `rigprops` learned the hard way."""
    window_manager = bpy.context.window_manager
    if window_manager is None:
        return
    wanted = {"VIEW_3D", "PROPERTIES", "DOPESHEET_EDITOR", "TIMELINE",
              "GRAPH_EDITOR"}
    for window in window_manager.windows:
        screen = getattr(window, "screen", None)
        if screen is None:
            continue
        for area in screen.areas:
            if area.type in wanted:
                area.tag_redraw()
