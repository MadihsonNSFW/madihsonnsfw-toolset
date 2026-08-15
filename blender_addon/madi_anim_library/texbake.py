"""Texture baking for the Node Editor tab (0.24.0; options 0.25.0;
enumeration 0.26.0; replacing the shader 0.27.0; NATIVE bake 0.29.0).

One command does one map: the app's Bake node names a material, a type, a
resolution and a file, and this module bakes it EXACTLY the way Blender's
own Bake panel does — the same operator with the same arguments, the
panel's own margin, and sampling / denoising / compute device left to the
scene's render settings, untouched.

⚠ It was not always native, and the difference is the whole story of
0.29.0. 0.24.0–0.28.4 was a "fast engine": pinned low samples, forced
denoising, GPU device management, operator margin 0 with hand-rolled
island-mask padding — the last two chasing UV seams that Blender's own
margin was believed (from fixture measurements) to cause. Then Marty
baked the same character through the REAL Bake panel and saw no seams,
and the pipeline was rebuilt to native on his instruction (2026-08-08):
"do it exactly the way it is done in blender ... with all the bake
options they have ... calling their functions". What stays ours is
plumbing the panel does not have: per-material isolation on a
multi-material mesh, the UDIM tile shift, creating the image and saving
the file, and `apply_baked_material`.

The panel options that were already here (0.25.0: Influence, View From,
margin type, samples) are GROWN PARAMETERS on a command that already
existed, and 0.29.0 grows the rest (Selected to Active, Target, Clear
Image) the same way — no capability check can see any of them, so the
reply carries an `options` block instead and the app says so when it
comes back missing (the save_abc rule).

0.26.0 adds the pure READS behind bulk baking (`bake_targets`,
`list_collections`), and 0.27.0 the one command here that WRITES to the
user's scene: `apply_baked_material` places each baked map into the
material it came from, wired to that material's active Material Output.
Everything else in this module restores whatever it touches.

Kept out of core.py like cage/jiggle/picker/optimizer: self-contained,
imports nothing from the package (the test suite loads it standalone).

⚠ Three 5.2 facts this module exists around (probed 2026-08-07, logged in
BLENDER_NOTES.md): `bpy.ops.object.bake` from Python defaults pass_filter
to an EMPTY set (COMBINED bakes black without the explicit filter); a data
bake into an sRGB byte image stores ENCODED values (0.5 reads 0.737); and
the native UV bake type writes all zeros — UV here is a temp
TexCoord->Emission rewire baked as EMIT, restored afterwards.
"""

import math
import os
import time

import bpy

BAKE_TYPES = (
    "COMBINED", "AO", "SHADOW", "POSITION", "NORMAL", "UV", "ROUGHNESS",
    "EMIT", "ENVIRONMENT", "DIFFUSE", "GLOSSY", "TRANSMISSION")

# Deterministic maps: 1 sample, float pixels, no colour transform.
DATA_TYPES = {"NORMAL", "ROUGHNESS", "UV", "POSITION"}

# Which bake type offers which options is Blender's OWN rule, read off
# cycles/ui.py in 5.2 rather than guessed (CYCLES_RENDER_PT_bake and its
# Influence sub-panel). Keep these in step with app\bakenodes.py, which
# draws exactly the same rows.
#
# Influence: NORMAL gets space + swizzle (below); COMBINED gets Lighting
# (direct/indirect) plus Contributions (diffuse/glossy/transmission/emit);
# the three component types get direct/indirect/colour. Every other type
# has no Influence panel at all.
INFLUENCE = {
    "COMBINED": ("DIRECT", "INDIRECT", "DIFFUSE", "GLOSSY", "TRANSMISSION",
                 "EMIT"),
    "DIFFUSE": ("DIRECT", "INDIRECT", "COLOR"),
    "GLOSSY": ("DIRECT", "INDIRECT", "COLOR"),
    "TRANSMISSION": ("DIRECT", "INDIRECT", "COLOR"),
}
PASS_FLAGS = ("EMIT", "DIRECT", "INDIRECT", "COLOR", "DIFFUSE", "GLOSSY",
              "TRANSMISSION")

# ⚠ The operator's pass_filter DEFAULTS TO EMPTY from Python — without
# these, COMBINED and the three component types bake pure black and report
# FINISHED. The UI quietly fills them from scene.render.bake. Everything
# on is also what a fresh Blender ticks, so this doubles as the default.
PASS_FILTER = {btype: set(flags) for btype, flags in INFLUENCE.items()}

# View From is drawn for every type EXCEPT these six, and Blender greys it
# even then unless the scene has a camera.
NO_VIEW_FROM = {"AO", "POSITION", "NORMAL", "UV", "ROUGHNESS", "ENVIRONMENT"}
VIEW_FROM = ("ABOVE_SURFACE", "ACTIVE_CAMERA")

NORMAL_SPACES = ("TANGENT", "OBJECT")
SWIZZLE = ("POS_X", "POS_Y", "POS_Z", "NEG_X", "NEG_Y", "NEG_Z")
SWIZZLE_DEFAULT = ("POS_X", "POS_Y", "POS_Z")
MARGIN_TYPES = ("ADJACENT_FACES", "EXTEND")
# The panel's Output > Target: an image, or the mesh's active color
# attribute (no image, no file — the map lands on the vertices).
TARGETS = ("IMAGE_TEXTURES", "VERTEX_COLORS")

MAX_SAMPLES = 4096
MAX_SIZE = 8192            # a socket can ask; it cannot ask for a 4 GB image
MIN_SIZE = 16


def _one_of(value, allowed, what):
    """Uppercased `value` if Blender knows it, else an error that lists
    what it does know."""
    got = str(value or "").upper()
    if got not in allowed:
        raise RuntimeError("unknown %s '%s' (know: %s)"
                           % (what, value, ", ".join(allowed)))
    return got


def list_materials():
    """Every material a bake could target, with the mesh objects using it —
    a pure read for the app's Shader-name dropdown."""
    got = []
    for mat in bpy.data.materials:
        if mat.library is not None:
            continue            # linked-in materials are not ours to bake
        objects = [ob.name for ob in bpy.data.objects
                   if ob.type == "MESH" and mat.name in
                   [m.name for m in ob.data.materials if m]]
        got.append({"name": mat.name, "users": mat.users,
                    "objects": objects,
                    "has_nodes": mat.node_tree is not None})
    return {"materials": got}


def _bakeable_materials(ob):
    """The object's slot materials a bake can target, in SLOT ORDER, deduped
    by name (the same material sitting in two slots is still one map)."""
    seen, got = set(), []
    for m in ob.data.materials:
        if m is not None and m.name not in seen and m.node_tree is not None:
            seen.add(m.name)
            got.append(m.name)
    return got


def _target_of(ob):
    """A bake-targets row for one mesh object, or None when a bake could not
    run on it (not a mesh / no UVs / no materials with nodes) — the caller
    counts those as `skipped` rather than erroring, because "select a lamp
    too and it is ignored" is the whole point of bulk mode."""
    if ob.type != "MESH" or not ob.data.uv_layers:
        return None
    materials = _bakeable_materials(ob)
    if not materials:
        return None
    return {"object": ob.name, "materials": materials}


def bake_targets(mode, material=None, collection=None):
    """What a bake run would cover, WITHOUT baking anything (0.26.0).

    Three modes, all pure reads:
      material   — the object `bake_texture` would pick for this material,
                   plus ALL of that object's slot materials ("Bake all slots")
      selected   — every selected mesh with UVs and materials; everything
                   else selected is counted in `skipped`, not errored
      collection — same filter over a named collection, children included

    The app turns the rows into one `bake_texture` call per (object,
    material); resolving targets HERE keeps the app's idea of "all slots"
    and the add-on's bake-time `_resolve_object` from ever disagreeing."""
    mode = str(mode or "").lower()
    if mode == "material":
        mat = bpy.data.materials.get(material or "")
        if mat is None:
            raise RuntimeError("material '%s' not found" % material)
        ob = _resolve_object(mat, None)
        return {"mode": mode,
                "targets": [{"object": ob.name,
                             "materials": _bakeable_materials(ob)}],
                "skipped": 0}
    if mode == "selected":
        pool = [ob for ob in bpy.context.view_layer.objects if ob.select_get()]
    elif mode == "collection":
        col = bpy.data.collections.get(collection or "")
        if col is None:
            raise RuntimeError("collection '%s' not found" % collection)
        # all_objects reaches through child collections; an object linked
        # into two of them shows up twice, so dedupe by name.
        seen = set()
        pool = []
        for ob in col.all_objects:
            if ob.name not in seen:
                seen.add(ob.name)
                pool.append(ob)
    else:
        raise RuntimeError("unknown mode '%s' (know: material, selected, "
                           "collection)" % mode)
    targets, skipped = [], 0
    for ob in pool:
        row = _target_of(ob)
        if row is None:
            skipped += 1
        else:
            targets.append(row)
    return {"mode": mode, "targets": targets, "skipped": skipped}


def list_collections():
    """Every collection in the scene, depth-first with depth so the app can
    indent a menu the way the outliner does. `meshes` counts the objects a
    folder bake would actually touch — a 0 tells the user why the bake would
    do nothing BEFORE they run it."""
    got = []

    def walk(col, depth):
        meshes = sum(1 for ob in col.all_objects if _target_of(ob))
        got.append({"name": col.name, "depth": depth, "meshes": meshes})
        for child in col.children:
            walk(child, depth + 1)

    for child in bpy.context.scene.collection.children:
        walk(child, 0)
    return {"collections": got}


def _resolve_object(mat, object_name):
    """The mesh the bake runs on: the named one, or the first mesh using the
    material that has UVs. Errors name the actual problem — 'no valid
    object' teaches nothing."""
    if object_name:
        ob = bpy.data.objects.get(object_name)
        if ob is None:
            raise RuntimeError("object '%s' not found" % object_name)
        if ob.type != "MESH":
            raise RuntimeError("object '%s' is not a mesh" % object_name)
        if mat.name not in [m.name for m in ob.data.materials if m]:
            raise RuntimeError("object '%s' does not use material '%s'"
                               % (object_name, mat.name))
        if not ob.data.uv_layers:
            raise RuntimeError("object '%s' has no UV map — unwrap it first"
                               % object_name)
        return ob
    candidates = [ob for ob in bpy.data.objects
                  if ob.type == "MESH"
                  and mat.name in [m.name for m in ob.data.materials if m]]
    if not candidates:
        raise RuntimeError("no mesh object uses material '%s'" % mat.name)
    with_uv = [ob for ob in candidates if ob.data.uv_layers]
    if not with_uv:
        raise RuntimeError(
            "no object using '%s' has a UV map — unwrap one first (checked: %s)"
            % (mat.name, ", ".join(ob.name for ob in candidates[:5])))
    return with_uv[0]


def _resolve_path(out_path, mat_name, bake_type):
    """Absolute save path. POSITION goes to EXR — its values run -1..1 and a
    PNG is unsigned, so saving it as PNG would silently clamp the negative
    half of the scene."""
    ext = ".exr" if bake_type == "POSITION" else ".png"
    if not out_path:
        if not bpy.data.filepath:
            raise RuntimeError(
                "no output path given and the .blend is unsaved — save it "
                "or set a path on the Output node")
        safe = "".join(c if c.isalnum() or c in "-_ " else "_"
                       for c in "%s_%s" % (mat_name, bake_type.lower()))
        out_path = "//bakes/%s%s" % (safe, ext)
    path = bpy.path.abspath(out_path)
    root, have_ext = os.path.splitext(path)
    if have_ext.lower() not in (".png", ".exr"):
        path = root + ext
    elif bake_type == "POSITION" and have_ext.lower() == ".png":
        path = root + ".exr"
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    return path


# ⚠ NO DEVICE MANAGEMENT since 0.29.0 — the scene's own Cycles device
# settings bake, exactly like pressing the panel's button. The 0.27.0
# device picker (OPTIX -> CUDA -> HIP with per-device use flags) is gone
# with the rest of the fast engine; the hybrid-CPU+GPU crash it existed to
# avoid (BLENDER_NOTES, 2026-08-07) cannot come back because nothing here
# touches Cycles preferences at all any more.


def bake_texture(material, bake_type, width, height, out_path=None,
                 object_name=None, samples=None, margin=16,
                 margin_type="ADJACENT_FACES", use_clear=True,
                 target="IMAGE_TEXTURES", pass_filter=None,
                 view_from="ABOVE_SURFACE", normal_space="TANGENT",
                 normal_swizzle=None, use_selected_to_active=False,
                 use_cage=False, cage_object=None, cage_extrusion=0.0,
                 max_ray_distance=0.0, view_transform=False):
    """Bake one map of one material, EXACTLY as Blender's Bake panel does
    (0.29.0). Blocking — the caller runs it on a worker thread with a long
    timeout, like every other long command. Restores every scene/selection
    setting it touches.

    ⚠ The operator is called with EXPLICIT kwargs mirroring the panel —
    never bare. From the UI, OBJECT_OT_bake's invoke copies the scene's
    bake settings into the operator's own properties; a bare Python call
    skips invoke and runs on the OPERATOR defaults instead (empty
    pass_filter, margin 16 whatever the panel says…). Passing every panel
    value as a kwarg IS that invoke copy, done by hand — "calling their
    functions" faithfully. The kwarg set was verified against 5.2's
    OBJECT_OT_bake RNA (2026-08-08): everything the panel draws is an
    operator argument except View From, which stays scene-only.

    Sampling, denoising, adaptive sampling and the compute device are the
    scene's own render settings, UNTOUCHED — pressing the panel's button
    changes none of them and neither does this. `samples` (the node's own
    row) overrides scene.cycles.samples for the bake when given; None
    means the scene's value, NOT the old fast-engine 1/16.

    `view_transform` (0.28.0) is kept for the save step — the file goes
    through the scene's colour management instead of raw — but no UI
    sends it since 0.29.0; the parameter stays honest for old callers."""
    bake_type = _one_of(bake_type, BAKE_TYPES, "bake type")
    target = _one_of(target, TARGETS, "bake target")
    try:
        width, height = int(width), int(height)
    except (TypeError, ValueError):
        raise RuntimeError("resolution must be whole pixels")
    if not (MIN_SIZE <= width <= MAX_SIZE and MIN_SIZE <= height <= MAX_SIZE):
        raise RuntimeError("resolution must be %d..%d px per side"
                           % (MIN_SIZE, MAX_SIZE))
    margin_type = _one_of(margin_type, MARGIN_TYPES, "margin type")
    view_from = _one_of(view_from, VIEW_FROM, "view from")
    normal_space = _one_of(normal_space, NORMAL_SPACES, "normal space")
    swizzle = list(normal_swizzle or SWIZZLE_DEFAULT)
    if len(swizzle) != 3:
        raise RuntimeError("normal swizzle takes exactly three axes")
    swizzle = [_one_of(axis, SWIZZLE, "swizzle axis") for axis in swizzle]
    try:
        use_margin = max(0, int(margin))
    except (TypeError, ValueError):
        raise RuntimeError("margin must be a whole number of pixels")
    try:
        cage_extrusion = max(0.0, float(cage_extrusion or 0.0))
        max_ray_distance = max(0.0, float(max_ray_distance or 0.0))
    except (TypeError, ValueError):
        raise RuntimeError("cage extrusion and max ray distance must be "
                           "numbers")

    # The contributions. None means "everything this type has", which is
    # both Blender's own default and the ⚠ above; an explicit list is
    # narrowed to the flags this type actually offers, and an empty result
    # is refused rather than baked black.
    offered = INFLUENCE.get(bake_type)
    use_filter = set(PASS_FILTER[bake_type]) if offered else None
    if pass_filter is not None and offered:
        want = [_one_of(flag, PASS_FLAGS, "contribution")
                for flag in pass_filter]
        use_filter = {flag for flag in want if flag in offered}
        if not use_filter:
            raise RuntimeError(
                "every contribution is switched off — a %s bake would come "
                "out black; switch at least one of %s on"
                % (bake_type, ", ".join(offered)))

    mat = bpy.data.materials.get(material or "")
    if mat is None:
        raise RuntimeError("material '%s' not found" % material)
    ob = _resolve_object(mat, object_name)
    to_image = target == "IMAGE_TEXTURES"
    path = _resolve_path(out_path, mat.name, bake_type) if to_image else None

    color_attr = None
    if not to_image:
        ca = getattr(ob.data, "color_attributes", None)
        active = ca.active_color if ca is not None else None
        if active is None:
            raise RuntimeError(
                "baking to a color attribute needs one on '%s' — add it "
                "under Object Data Properties > Color Attributes first"
                % ob.name)
        color_attr = active.name
        if bake_type == "UV":
            # the 5.2 UV workaround bakes a temp emission INTO AN IMAGE —
            # it has nowhere to land on vertices
            raise RuntimeError("a UV bake needs an image target — bake it "
                               "to an image file instead")

    is_data = bake_type in DATA_TYPES
    use_view_xf = bool(view_transform)
    if use_view_xf and is_data:
        # ⚠ A view transform on a data map is a CORRUPTED map, not a
        # prettier one: AgX would re-encode roughness/normal values that
        # the shader reads back as numbers. Refuse rather than produce a
        # file that looks plausible and is wrong (the module's whole point).
        raise RuntimeError(
            "a view transform cannot be applied to a %s map — it stores "
            "data, not a picture, and the values would be re-encoded"
            % bake_type)
    use_view_xf = use_view_xf and to_image      # no file, nothing to write

    if samples is not None:
        try:
            samples = max(1, min(int(samples), MAX_SAMPLES))
        except (TypeError, ValueError):
            raise RuntimeError("samples must be a whole number")

    cage_name = str(cage_object or "")
    if use_cage and cage_name:
        cage_ob = bpy.data.objects.get(cage_name)
        if cage_ob is None:
            raise RuntimeError("cage object '%s' not found" % cage_name)
        if cage_ob.type != "MESH":
            raise RuntimeError("cage object '%s' is not a mesh" % cage_name)

    scene = bpy.context.scene
    use_view_from = None
    if bake_type not in NO_VIEW_FROM:
        if view_from == "ACTIVE_CAMERA" and scene.camera is None:
            raise RuntimeError(
                "View From: Active Camera needs a camera in the scene — "
                "Blender greys the option out without one")
        use_view_from = view_from
    saved_scene = {
        "engine": scene.render.engine,
        # only borrowed when the node's Samples row overrides it — None
        # otherwise, and None is also the "leave it alone" flag in finally
        "samples": (scene.cycles.samples
                    if samples is not None and hasattr(scene, "cycles")
                    else None),
        # ⚠ View From is a SCENE setting, not an operator argument — the
        # only bake option that has to be written into the user's scene and
        # put back afterwards (checked against OBJECT_OT_bake's RNA).
        "view_from": scene.render.bake.view_from,
    }
    saved_sel = [o for o in bpy.context.view_layer.objects if o.select_get()]
    saved_active = bpy.context.view_layer.objects.active
    saved_mode = saved_active.mode if saved_active else "OBJECT"

    if use_selected_to_active and not [o for o in saved_sel if o != ob]:
        raise RuntimeError(
            "Selected to Active needs the SOURCE objects selected in the "
            "viewport — only the bake target ('%s') is selected" % ob.name)

    slot_mats = []
    if to_image:
        for m in ob.data.materials:
            if m and m not in slot_mats:
                slot_mats.append(m)
        for m in slot_mats:
            if m.node_tree is None:
                raise RuntimeError(
                    "material '%s' on '%s' has no node tree — enable nodes "
                    "on it" % (m.name, ob.name))

    img = None
    dummy = None         # the sacrificial target for the OTHER materials
    if to_image:
        # ⚠ A FLOAT buffer is not a nicety when a view transform is coming:
        # a lit bake produces values above 1.0, and an 8-bit buffer clips
        # them BEFORE the transform ever runs — measured, a 4.0 emission
        # stored (1,1,1) where the render showed 0.93. ⚠ And a RAW bake
        # whose name says .exr must be float for the same reason (0.28.2):
        # the whole point of asking for EXR is keeping the scene values
        # above 1.0 — Marty's chest ran 1.5–4.0 and a raw PNG pinned 15%
        # of the map flat at 1.0, a "weird" flat patch with a hard seam.
        wants_float = is_data or use_view_xf or path.lower().endswith(".exr")
        img = bpy.data.images.new("MADI_bake_tmp", width, height, alpha=True,
                                  float_buffer=wants_float)
        if is_data:
            img.colorspace_settings.name = "Non-Color"
        if not use_clear and os.path.exists(path):
            # Clear Image OFF, natively: the panel bakes over whatever the
            # image already holds. Our datablock is fresh every run, so the
            # previous FILE is loaded into it first — the same result,
            # without keeping stale datablocks around. A different size
            # means there is nothing honest to keep: the bake starts clear.
            prev = None
            try:
                prev = bpy.data.images.load(path)
                if tuple(prev.size) == (width, height):
                    try:
                        import numpy as np
                        buf = np.empty(width * height * 4, dtype=np.float32)
                        prev.pixels.foreach_get(buf)
                        img.pixels.foreach_set(buf)
                    except ImportError:
                        img.pixels[:] = prev.pixels[:]
            except RuntimeError:
                pass                   # unreadable file = a clear start
            finally:
                if prev is not None:
                    try:
                        bpy.data.images.remove(prev)
                    except (ReferenceError, RuntimeError):
                        pass

    temp_nodes = []      # (tree, node)
    uv_restores = []     # (tree, surface_socket, prev_from_socket)
    saved_actives = []   # (tree, previously active node)
    uv_tile = (0, 0)     # the UDIM shift; set inside the try
    uv_saved = uv_loops = uv_layer = None
    saved_img_settings = None
    samples_used = None
    device_used = None
    content = None
    warning = None
    t0 = time.time()
    try:
        scene.render.engine = "CYCLES"
        if samples is not None and hasattr(scene, "cycles"):
            scene.cycles.samples = samples
        # what the bake really runs with — the reply echoes the SCENE's
        # values, because since 0.29.0 the scene's values are the truth
        samples_used = getattr(getattr(scene, "cycles", None),
                               "samples", None)
        device_used = getattr(getattr(scene, "cycles", None),
                              "device", "CPU")
        if use_view_from is not None:
            scene.render.bake.view_from = use_view_from

        op_type = bake_type
        if to_image:
            for m in slot_mats:
                tree = m.node_tree
                saved_actives.append((tree, tree.nodes.active))
                node = tree.nodes.new("ShaderNodeTexImage")
                # ⚠ ONLY the target material writes into the real image.
                # Cycles bakes EVERY face of the object through its own
                # material's active image node, so on a multi-material mesh
                # (a G8 body: Face/Torso/Arms… on ONE mesh, with
                # OVERLAPPING uv layouts) every other material's faces
                # would land in — and overwrite — the map that was asked
                # for ("I chose torso but it baked Face", Marty
                # 2026-08-07). The other slots get a sacrificial image
                # instead: bake cost scales with texels, so shunting them
                # there is free.
                if m is mat:
                    node.image = img
                else:
                    if dummy is None:
                        # ⚠ not 4×4 (2026-08-07): the margin is applied per
                        # BAKE IMAGE, and a 16..64 px dilation on a 4 px
                        # image is a fill reading past its own buffer — an
                        # unnecessary oddity to hand a kernel that crashed
                        # once already. Still ~1% of a real map's texels.
                        side = max(64, use_margin * 4)
                        dummy = bpy.data.images.new("MADI_bake_void",
                                                    side, side)
                    node.image = dummy
                tree.nodes.active = node
                temp_nodes.append((tree, node))
                if bake_type == "UV" and m is mat:
                    # ⚠ native UV bake writes zeros in 5.2 — rewire UV
                    # through an emission shader and bake EMIT instead.
                    out_node = next(
                        (n for n in tree.nodes if n.type == "OUTPUT_MATERIAL"
                         and n.is_active_output), None) or next(
                        (n for n in tree.nodes if n.type == "OUTPUT_MATERIAL"),
                        None)
                    if out_node is None:
                        raise RuntimeError("material '%s' has no output node"
                                           % m.name)
                    surface = out_node.inputs["Surface"]
                    prev = (surface.links[0].from_socket if surface.links
                            else None)
                    n_tc = tree.nodes.new("ShaderNodeTexCoord")
                    n_em = tree.nodes.new("ShaderNodeEmission")
                    tree.links.new(n_tc.outputs["UV"], n_em.inputs["Color"])
                    tree.links.new(n_em.outputs["Emission"], surface)
                    temp_nodes.append((tree, n_tc))
                    temp_nodes.append((tree, n_em))
                    uv_restores.append((tree, surface, prev))
            if bake_type == "UV":
                op_type = "EMIT"

        if saved_active and saved_mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")
        if use_selected_to_active:
            # the user's viewport selection IS the source list — keep it,
            # exactly as pressing the panel's button over it would
            ob.select_set(True)
        else:
            for o in saved_sel:
                o.select_set(False)
            ob.select_set(True)
        bpy.context.view_layer.objects.active = ob

        if to_image:
            # ⚠ UDIM bodies (Marty's G8: Face on tile 0-1, Torso 1-2, Legs
            # 2-3, Arms 3-4): a bake image covers 0-1, so a material whose
            # islands live on another tile rasterises NOTHING — a fully
            # transparent "success". Shift the TARGET material's loops into
            # 0-1 for the bake and put them back after; everything else
            # stays where it is (their faces go to the sacrificial image).
            uv_tile, uv_saved, uv_loops, uv_layer = _shift_uv_tile(ob, mat)

        # ⚠ The margin passes through UNTOUCHED since 0.29.0 — Blender's
        # own dilation, both types, exactly what the panel runs. 0.28.x
        # forced it to 0 and padded by hand, believing 5.2's margin padded
        # alpha without colour; Marty then baked through the real panel
        # with no seams, so whatever those fixtures measured, the native
        # margin is the behaviour to ship (and the suite now measures a
        # native margin-16 band CARRYING COLOUR as its acceptance check).
        kwargs = {"type": op_type, "target": target,
                  "use_clear": bool(use_clear),
                  "margin": use_margin, "margin_type": margin_type,
                  "use_selected_to_active": bool(use_selected_to_active),
                  "use_cage": bool(use_cage),
                  "cage_extrusion": cage_extrusion,
                  "max_ray_distance": max_ray_distance}
        if use_cage and cage_name:
            kwargs["cage_object"] = cage_name
        if use_filter:
            kwargs["pass_filter"] = use_filter
        if op_type == "NORMAL":
            kwargs["normal_space"] = normal_space
            kwargs["normal_r"], kwargs["normal_g"], kwargs["normal_b"] = \
                swizzle
        bpy.ops.object.bake(**kwargs)

        if to_image:
            img.filepath_raw = path
            img.file_format = ("OPEN_EXR" if path.lower().endswith(".exr")
                               else "PNG")
            # ⚠ The stats are read BEFORE saving through a view transform:
            # they describe the RAW bake (that is what the empty-map
            # warning reasons about). Since 0.29.0 they use the alpha
            # heuristic again — margin texels count as covered, which only
            # nudges the fractions; the warnings stay honest.
            content = _content_stats(img, width, height)
            warning = _empty_bake_reason(bake_type, content, mat)
            # ⚠ CLIPPING IS INVISIBLE BY EYE — it looks like a flat bright
            # patch with a seam around it, and Marty spent an evening on
            # exactly that. Only a BYTE buffer can have silently lost data
            # (float holds >1).
            if (not wants_float and content
                    and content.get("clipped", 0.0) > 0.05):
                clip = ("%d%% of this map is CLIPPED flat at 1.0 — the "
                        "surface is brighter than a PNG can hold; name the "
                        "Output file .exr to keep the real values"
                        % round(content["clipped"] * 100))
                warning = clip if not warning else warning + "; " + clip
            if use_view_xf:
                # ⚠ save() writes the raw buffer through the IMAGE's
                # colorspace; only save_render() runs the SCENE's colour
                # management (view transform, look, exposure, gamma) — the
                # same path F12's Save As uses. It reads
                # scene.render.image_settings, so those are written into
                # the user's scene and put back in the finally, exactly
                # like view_from.
                settings = scene.render.image_settings
                # ⚠ ORDER MATTERS, both ways round. `file_format` is
                # FILTERED BY `media_type` in 5.x: assigning "PNG" while
                # the scene is set to VIDEO raises TypeError, and assigning
                # "FFMPEG" back while it says IMAGE raises the same.
                # Marty's scene is normally on VIDEO (FFMPEG, for
                # playblasts), so the first view-transform bake he ran died
                # right here. media_type is therefore set FIRST going in
                # and restored FIRST coming back (dicts keep insertion
                # order).
                saved_img_settings = {
                    "media_type": getattr(settings, "media_type", None),
                    "file_format": settings.file_format,
                    "color_management": settings.color_management,
                    "color_mode": settings.color_mode}
                if hasattr(settings, "media_type"):
                    settings.media_type = "IMAGE"
                settings.file_format = img.file_format
                settings.color_management = "FOLLOW_SCENE"
                settings.color_mode = "RGBA"
                img.save_render(path, scene=scene)
            else:
                img.save()
    finally:
        if uv_saved is not None:
            try:
                _restore_uv(uv_layer, uv_loops, uv_saved)
            except (ReferenceError, RuntimeError):
                pass
        for tree, node in temp_nodes:
            try:
                tree.nodes.remove(node)
            except (ReferenceError, RuntimeError):
                pass
        for tree, surface, prev in uv_restores:
            try:
                if prev is not None:
                    tree.links.new(prev, surface)
            except (ReferenceError, RuntimeError):
                pass
        for tree, prev_active in saved_actives:
            try:
                if prev_active is not None:
                    tree.nodes.active = prev_active
            except (ReferenceError, RuntimeError):
                pass
        if img is not None:
            try:
                bpy.data.images.remove(img)
            except (ReferenceError, RuntimeError):
                pass
        if dummy is not None:
            try:
                bpy.data.images.remove(dummy)
            except (ReferenceError, RuntimeError):
                pass
        scene.render.engine = saved_scene["engine"]
        if saved_scene["samples"] is not None:
            scene.cycles.samples = saved_scene["samples"]
        scene.render.bake.view_from = saved_scene["view_from"]
        if saved_img_settings is not None:
            for key, value in saved_img_settings.items():
                if value is None:
                    continue          # media_type on a build without it
                try:
                    setattr(scene.render.image_settings, key, value)
                except (AttributeError, TypeError):
                    pass
        for o in bpy.context.view_layer.objects:
            o.select_set(o in saved_sel)
        bpy.context.view_layer.objects.active = saved_active
        if saved_active and saved_mode != "OBJECT":
            try:
                bpy.ops.object.mode_set(mode=saved_mode)
            except RuntimeError:
                pass

    # Echo every input (the save_abc rule: a reply that names what it did is
    # the only capability check a grown parameter can ever have).
    return {"material": mat.name, "object": ob.name, "bake_type": bake_type,
            "width": width, "height": height, "samples": samples_used,
            "device": device_used, "path": path,
            "target": target, "color_attribute": color_attr,
            "isolated_slots": (len(slot_mats) - 1) if to_image else 0,
            "content": content, "uv_tile": list(uv_tile),
            # ⚠ The whole options block is a GROWN PARAMETER SET (0.25.0,
            # grown again 0.29.0) — `supports()` reads command names, so it
            # would answer "yes" on an add-on that ignores every one of
            # these. Its presence in the reply is the only proof they were
            # honoured; the app watches for it going missing, and reads
            # `target` in it as "this add-on bakes natively".
            "options": {
                "samples_auto": samples is None,
                "pass_filter": sorted(use_filter) if use_filter else None,
                "view_from": use_view_from,
                "normal_space": normal_space if bake_type == "NORMAL"
                                else None,
                "normal_swizzle": list(swizzle) if bake_type == "NORMAL"
                                  else None,
                "margin": use_margin, "margin_type": margin_type,
                "use_clear": bool(use_clear),
                "target": target,
                "selected_to_active": {
                    "on": bool(use_selected_to_active),
                    "cage": bool(use_cage),
                    "cage_object": cage_name or None,
                    "cage_extrusion": cage_extrusion,
                    "max_ray_distance": max_ray_distance},
                # ⚠ NOT the same thing as view_from above: this is the
                # scene's COLOUR MANAGEMENT (AgX / Filmic / Standard…), and
                # the name it echoes is proof the file was written through
                # it rather than raw.
                "view_transform": (scene.view_settings.view_transform
                                   if use_view_xf else None)},
            "warning": warning, "seconds": round(time.time() - t0, 2)}


def _bake_uv_layer(ob):
    """The UV map a bake actually samples: the ACTIVE RENDER one (the camera
    icon in the list), which is what an Image Texture node with no Vector
    input uses — NOT necessarily `uv_layers.active`, the one merely selected
    for editing. The tile shift below and the replacement material further
    down both key off this, so they can never disagree about which layer was
    baked."""
    layers = ob.data.uv_layers
    return next((l for l in layers if l.active_render), None) or layers.active


def _material_loops(ob, mat):
    """Loop indices of the faces wearing `mat` — the same set the bake
    rasterises."""
    slots = {i for i, s in enumerate(ob.material_slots) if s.material is mat}
    loops = []
    for poly in ob.data.polygons:
        if poly.material_index in slots:
            loops.extend(range(poly.loop_start,
                               poly.loop_start + poly.loop_total))
    return loops


def _uv_tile_of(ob, mat):
    """Which UDIM tile the material's islands sit on, as whole (u, v) —
    (0, 0) when they are already in the bake's 0-1 square. Returns
    (tile, loops, uv_layer)."""
    uv = _bake_uv_layer(ob)
    if uv is None:
        return (0, 0), [], None
    loops = _material_loops(ob, mat)
    if not loops:
        return (0, 0), loops, uv
    tu = int(math.floor(min(uv.data[i].uv[0] for i in loops)))
    tv = int(math.floor(min(uv.data[i].uv[1] for i in loops)))
    return (tu, tv), loops, uv


# ⚠ NO HAND-ROLLED MARGIN since 0.29.0. 0.28.3/0.28.4 rasterised an
# island mask off the mesh (`_island_mask`) and flood-padded it
# (`_pad_islands`) with the operator margin forced to 0, because fixture
# measurements said 5.2's own margin padded alpha without colour. Marty
# then baked the same character through Blender's real Bake panel and saw
# NO seams — so the native margin ships, the two functions are gone, and
# the suite's acceptance check now measures a native margin-16 band
# CARRYING COLOUR on the fixture. If a black band ever comes back, start
# from that check, not from a new padding pipeline.


def _shift_uv_tile(ob, mat):
    """Move the target material's UV islands into the 0-1 tile for the
    bake. Returns (tile, saved_pairs, loop_indices, uv_layer) —
    saved_pairs is None when nothing needed moving."""
    tile, loops, uv = _uv_tile_of(ob, mat)
    if uv is None or not loops or tile == (0, 0):
        return (0, 0), None, None, None
    tu, tv = tile
    saved = [(uv.data[i].uv[0], uv.data[i].uv[1]) for i in loops]
    for i in loops:
        d = uv.data[i].uv
        d[0] -= tu
        d[1] -= tv
    return tile, saved, loops, uv


def _restore_uv(uv, loops, saved):
    for i, pair in zip(loops, saved):
        uv.data[i].uv = pair


# ------------------------------------- replacing the shader with the bake
# Marty, 2026-08-07: a tickbox on the Output image node "that replaces the
# shader (and slots if there are any) of where the shaders were baked on
# their respective uvs" — and then, on seeing the first version: *"replace
# shader should just PLACE the node in the material > respective slots and
# attach it to material output (one of them if many)"*.
#
# So it does exactly that and nothing more: the baked map goes in as an
# Image Texture node INSIDE the material that was baked, wired straight to
# that material's ACTIVE Material Output. No new material, no slot
# reassignment. ⚠ The first version built a `<material>_baked` material and
# repointed the slots; it worked, but it moved the user's material out from
# under his object, and everything downstream of a slot (drivers, linked
# duplicates, anything naming the material) moved with it. Placing a node
# leaves the user's shader network intact — merely unplugged from the
# output — so undoing it is one Ctrl+Z or one re-drag.
#
# It is a SEPARATE command, not a parameter on bake_texture, for two
# reasons: `supports()` can then answer for it honestly (a grown parameter
# on an existing command cannot be capability-checked — the save_abc rule),
# and bake_texture stays a pure read of the scene, restoring everything it
# touched. ⚠ It also has to run AFTER the whole queue: rewiring a material
# while later maps still have to bake would hand those bakes a different
# shader than the one they were asked for.

BAKED_MARK = "madi_baked"      # on OUR node, so a re-bake reuses it


def _baked_image(path):
    """The baked file as an image datablock. ⚠ `check_existing` hands back
    the datablock from a PREVIOUS bake of the same path, whose pixels are
    the old map — `reload()` is what makes a re-bake actually show."""
    img = bpy.data.images.load(path, check_existing=True)
    try:
        img.reload()
    except RuntimeError:
        pass
    return img


def _active_output(tree, prefer_target=None, create=False):
    """The Material Output the material actually renders through — "one of
    them if many" (Marty). Same rule the bake itself follows, so the node
    lands where the map came from.

    ⚠ `prefer_target` is Marty's 2026-08-08 rule: *"if two material outputs
    — always pick the one with render engine of whatever the initial
    material had as active material output"*. A Material Output carries a
    `target` (ALL / EEVEE / CYCLES), and a material can hold one per
    engine; when the map is being placed into a SECOND material, that
    material's own "active" flag is not the question — matching the engine
    the first material rendered through is.

    ⚠ `create=True` is his follow-up the same day: *"if we don't have that
    kind of material output in another slot we can just make one and wire
    it in"*. Without it, a slot with only an EEVEE output silently took the
    map on that output while the bake had come from a CYCLES one — the map
    landed somewhere that never renders. So when a target is ASKED FOR and
    no output carries it, we add one rather than fall back. ⚠ The new node
    is made ACTIVE for its engine, and a fallback (no target asked, or
    nothing to match) still returns the existing active output."""
    outputs = [n for n in tree.nodes if n.type == "OUTPUT_MATERIAL"]
    if prefer_target:
        same = [n for n in outputs
                if getattr(n, "target", "ALL") == prefer_target]
        if same:
            return next((n for n in same if n.is_active_output), same[0])
        if create:
            made = tree.nodes.new("ShaderNodeOutputMaterial")
            made.target = prefer_target
            # sit it clear of whatever is already there, not on top of it
            right = max([n.location[0] for n in tree.nodes] or [0.0])
            top = max([n.location[1] for n in outputs] or [300.0])
            made.location = (right + 220, top - 260)
            made.is_active_output = True
            return made
    if not outputs:
        return None
    return next((n for n in outputs if n.is_active_output), outputs[0])


def _output_target(mat):
    """The render-engine target of `mat`'s active Material Output ('ALL',
    'EEVEE', 'CYCLES'), or None when the material has no output at all."""
    tree = getattr(mat, "node_tree", None)
    out = _active_output(tree) if tree else None
    return getattr(out, "target", None) if out is not None else None


def _our_node(tree):
    """The Image Texture node a previous run of this command left behind,
    if any — reused so baking the same material twice does not stack up a
    pile of image nodes on top of each other."""
    return next((n for n in tree.nodes
                 if n.type == "TEX_IMAGE" and n.get(BAKED_MARK)), None)


def _place_baked_node(mat, image, bake_type, uv_name, tile,
                      prefer_target=None):
    """Put the baked map into `mat` and wire it to the Material Output.
    Returns (tex_node, output_node, previous_surface_node_name).

    Only the image node is required: an Image Texture with nothing in its
    Vector input samples the active RENDER UV map, which is the one the
    bake used. ⚠ The exception is a UDIM material, which gets a UV Map +
    Mapping pair — the bake moved its islands into 0-1 and put them back,
    so the MAP is 0-1 while the MESH is not, and without the offset the
    node samples the wrong square.

    `prefer_target` picks between several Material Outputs by render
    engine, and MAKES one when the material has none of that engine — see
    `_active_output`."""
    tree = mat.node_tree
    made_output = False
    before = sum(1 for n in tree.nodes if n.type == "OUTPUT_MATERIAL")
    out = _active_output(tree, prefer_target, create=True)
    if out is None:
        out = tree.nodes.new("ShaderNodeOutputMaterial")
        out.location = (300, 300)
        made_output = True
    else:
        made_output = sum(1 for n in tree.nodes
                          if n.type == "OUTPUT_MATERIAL") > before

    tex = _our_node(tree)
    if tex is None:
        tex = tree.nodes.new("ShaderNodeTexImage")
        tex.location = (out.location[0] - 340, out.location[1] - 300)
    tex[BAKED_MARK] = True
    tex.label = "Baked %s" % bake_type.title()
    tex.image = image
    if bake_type in DATA_TYPES:
        # same rule as the bake itself: a data map read through sRGB is a
        # wrong number, not a wrong look (0.5 roughness reads 0.737)
        image.colorspace_settings.name = "Non-Color"

    if tuple(tile) != (0, 0):
        uv_node = next((l.from_node for l in tree.links
                        if l.to_node.name == tex.name
                        and l.to_socket.name == "Vector"), None)
        if uv_node is None or uv_node.type != "MAPPING":
            mapping = tree.nodes.new("ShaderNodeMapping")
            mapping.location = (tex.location[0] - 220, tex.location[1])
            uvm = tree.nodes.new("ShaderNodeUVMap")
            uvm.location = (mapping.location[0] - 220, tex.location[1])
            if uv_name:
                uvm.uv_map = uv_name
            tree.links.new(uvm.outputs["UV"], mapping.inputs["Vector"])
            tree.links.new(mapping.outputs["Vector"], tex.inputs["Vector"])
            uv_node = mapping
        uv_node.inputs["Location"].default_value = (-tile[0], -tile[1], 0.0)

    surface = out.inputs["Surface"]
    previous = (surface.links[0].from_node.name if surface.links else None)
    # An input takes one link, so this REPLACES whatever fed the output —
    # the old network stays in the tree, just unplugged.
    tree.links.new(tex.outputs["Color"], surface)
    tree.nodes.active = tex
    return tex, out, previous, made_output


def apply_baked_material(items, all_slots=False):
    """Place each baked map into the material it came from and wire it to
    that material's active Material Output (0.27.0). `items` is one row per
    map the run produced:

        {"object", "material", "path", "bake_type"}

    A row that cannot be applied is SKIPPED with a reason, never raised —
    one missing file must not undo the rest of a bulk run, the same rule
    the bake queue itself follows. The reply names every material it
    rewired and what used to feed its output.

    ⚠ **`all_slots` is a GROWN PARAMETER** (0.30.0, Marty's *"place and
    connect baked result to Active material output of EVERY material
    slot"*), so `supports("apply_baked_material")` cannot tell you whether
    it is honoured — the reply ECHOES it, and the app warns when the echo
    is missing (the save_abc rule). Ticked, every OTHER slot of the same
    object gets the map too; a slot whose own material was baked keeps its
    OWN map, because overwriting a correct map with a neighbour's would be
    a downgrade. Which Material Output each one is wired to follows the
    render engine of the first baked material's active output —
    `_active_output`'s `prefer_target`."""
    if not isinstance(items, (list, tuple)) or not items:
        raise RuntimeError("nothing to replace — the run produced no maps")
    applied, skipped = [], []

    def skip(ob_name, mat_name, reason):
        skipped.append({"object": ob_name, "material": mat_name,
                        "reason": reason})

    def place(ob, mat, image, bake_type, prefer_target, source=None):
        """One material rewired, or a skip reason. `source` names the
        material the map was baked from when it is not this one."""
        if mat.node_tree is None:
            skip(ob.name, mat.name, "material '%s' has no node tree"
                 % mat.name)
            return
        if mat.library is not None:
            # a linked material is not ours to rewire — say so rather than
            # let Blender raise halfway through the list
            skip(ob.name, mat.name, "material '%s' is linked from a library"
                 % mat.name)
            return
        slots = [i for i, s in enumerate(ob.material_slots)
                 if s.material is not None and s.material.name == mat.name]
        if not slots:
            skip(ob.name, mat.name, "'%s' no longer has a slot using '%s'"
                 % (ob.name, mat.name))
            return
        try:
            tile, _loops, uv = _uv_tile_of(ob, mat)
            tex, out, previous, made = _place_baked_node(
                mat, image, bake_type, uv.name if uv else None, tile,
                prefer_target)
        except (RuntimeError, AttributeError, TypeError) as err:
            skip(ob.name, mat.name, str(err))
            return
        entry = {"object": ob.name, "material": mat.name,
                 "slots": slots, "node": tex.name,
                 "output": out.name, "was_fed_by": previous,
                 "output_target": getattr(out, "target", None),
                 "output_created": bool(made),
                 "uv_layer": uv.name if uv else None,
                 "uv_tile": list(tile), "bake_type": bake_type,
                 "image": image.name}
        if source is not None:
            entry["source_material"] = source
        applied.append(entry)

    # object name -> what the all-slots pass needs: the object, the render
    # engine the FIRST baked material rendered through, that material's map,
    # and every material already handled.
    seen_objects = {}
    for row in items:
        row = row or {}
        ob_name = str(row.get("object") or "")
        mat_name = str(row.get("material") or "")
        path = str(row.get("path") or "")
        ob = bpy.data.objects.get(ob_name)
        mat = bpy.data.materials.get(mat_name)
        if ob is None or ob.type != "MESH":
            skip(ob_name, mat_name,
                 "object '%s' is gone or is not a mesh" % ob_name)
            continue
        if mat is None:
            skip(ob_name, mat_name, "material '%s' is gone" % mat_name)
            continue
        if not path or not os.path.exists(path):
            skip(ob_name, mat_name,
                 "the baked file is missing (%s)" % (path or "no path"))
            continue
        bake_type = _one_of(row.get("bake_type") or "COMBINED", BAKE_TYPES,
                            "bake type")
        try:
            image = _baked_image(path)
        except (RuntimeError, OSError) as err:
            skip(ob_name, mat_name, str(err))
            continue
        info = seen_objects.get(ob.name)
        if info is None:
            # the INITIAL material of this object decides the engine every
            # placement on it targets
            info = {"object": ob, "target": _output_target(mat),
                    "image": image, "bake_type": bake_type,
                    "material": mat.name, "done": set()}
            seen_objects[ob.name] = info
        info["done"].add(mat.name)
        place(ob, mat, image, bake_type, info["target"])

    if all_slots:
        for info in seen_objects.values():
            ob = info["object"]
            for slot in ob.material_slots:
                other = slot.material
                if other is None or other.name in info["done"]:
                    continue
                info["done"].add(other.name)
                place(ob, other, info["image"], info["bake_type"],
                      info["target"], source=info["material"])

    return {"applied": applied, "skipped": skipped, "count": len(applied),
            "all_slots": bool(all_slots)}


# Lit types where an all-black result means something went WRONG for the
# user, not "the input really is zero". EMIT / TRANSMISSION black is a
# plain fact (nothing emits / transmits); SHADOW and ENVIRONMENT have
# legitimate flat results; every data map can honestly be black.
_WARN_TYPES = {"COMBINED", "AO", "DIFFUSE", "GLOSSY"}


def _content_stats(img, width, height):
    """{'rgb_max', 'alpha_max', 'clipped'} of the baked image, or None
    above the size cap (an 8k float readback is a gigabyte of buffer — not
    worth it). "Covered" is the alpha>0.5 heuristic — approximate on
    purpose: ⚠ baked alpha is pass-dependent (an EMIT-family bake fills it
    across the whole image while COMBINED keeps alpha-0 background, both
    measured 2026-08-07) and the native margin writes island colours
    outside the islands too. These numbers only feed warnings, and the
    warnings stay honest at that precision."""
    if width * height > 4096 * 4096:
        return None
    try:
        import numpy as np
    except ImportError:
        return None
    buf = np.empty(width * height * 4, dtype=np.float32)
    img.pixels.foreach_get(buf)
    px = buf.reshape(-1, 4)
    covered = px[px[:, 3] > 0.5] if (px[:, 3] > 0.5).any() else px
    return {"rgb_max": round(float(covered[:, :3].max()), 4),
            "alpha_max": round(float(px[:, 3].max()), 4),
            # fraction of COVERED channels pinned at 1.0 — on a byte
            # buffer that is data already lost, and 15% of it looks like a
            # flat white patch with a seam, not like an error (2026-08-07)
            "clipped": round(float((covered[:, :3] >= 0.999).mean()), 4)}


def _empty_bake_reason(bake_type, content, mat):
    """Why a lit map came out empty, in words — or None when it didn't.

    ⚠ Found live on Marty's picker file (2026-08-07): a COMBINED bake of an
    unlit scene, through a material whose ACTIVE output renders transparent,
    writes (0,0,0,0) on every covered texel and reports FINISHED — which an
    app then presents as a successful bake of nothing. Blender's own bake
    behaves identically; the difference is that we SAY it."""
    if content is None or bake_type not in _WARN_TYPES:
        return None
    if content["rgb_max"] > 0.001:
        return None
    reasons = []
    lights = [o for o in bpy.data.objects
              if o.type == "LIGHT" and not o.hide_render]
    world_on = False
    world = bpy.context.scene.world
    if world is not None and world.node_tree is not None:
        bg = next((n for n in world.node_tree.nodes
                   if n.type == "BACKGROUND"), None)
        if bg is not None:
            colour = bg.inputs["Color"].default_value
            world_on = (bg.inputs["Strength"].default_value
                        * max(colour[0], colour[1], colour[2])) > 0.05
    if bake_type != "AO" and not lights and not world_on:
        reasons.append("the scene has no enabled lights and the world is "
                       "nearly black — a %s bake renders the surface LIT"
                       % bake_type)
    if content["alpha_max"] < 0.01:
        reasons.append("the surface rendered fully transparent")
    outputs = [n for n in mat.node_tree.nodes if n.type == "OUTPUT_MATERIAL"]
    if len(outputs) > 1:
        active = next((n for n in outputs if n.is_active_output), outputs[0])
        feeder = (active.inputs["Surface"].links[0].from_node
                  if active.inputs["Surface"].links else None)
        fed_by = getattr(getattr(feeder, "node_tree", None), "name", None) \
            or getattr(feeder, "name", "nothing")
        reasons.append("'%s' has %d Material Output nodes and the bake "
                       "follows the ACTIVE one (fed by '%s') — the other "
                       "may hold the real shader"
                       % (mat.name, len(outputs), fed_by))
    if not reasons:
        reasons.append("the %s pass evaluated to black everywhere on "
                       "'%s' — check the material's shader" %
                       (bake_type, mat.name))
    return "the map baked EMPTY: " + "; ".join(reasons)
