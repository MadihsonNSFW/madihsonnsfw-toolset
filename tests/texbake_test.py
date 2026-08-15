# Texture bake (texbake.py): NATIVE since add-on 0.29.0 — headless.
#
#   blender.exe -b --factory-startup --python tests\texbake_test.py
#
# The bake is Blender's own operator with the panel's own options. The
# acceptance check of the native rebuild is section 13: a margin-16 band
# EXISTS in the baked file and CARRIES ISLAND COLOUR (0.28.x forced the
# operator margin to 0 and hand-padded — then Marty baked through the real
# Bake panel, saw none of the seams, and ordered this native, 2026-08-08).
# Still pinned: the three 5.2 facts (empty pass_filter default bakes
# black; sRGB encodes a data map; native UV bake writes zeros), full
# restore, multi-material isolation, UDIM, warnings, view transform.
# The suite sets the SCENE's samples low up front — the native contract is
# that the scene's own sampling / denoising / device bake, suite included
# (factory samples are 4096, which is minutes per map at these sizes; the
# factory device is CPU, which keeps the suite deterministic).
import importlib.util
import os
import sys
import tempfile

import bpy

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

ROOT = _ROOT
spec = importlib.util.spec_from_file_location(
    "madi_texbake", os.path.join(ROOT, "blender_addon", "madi_anim_library",
                                 "texbake.py"))
texbake = importlib.util.module_from_spec(spec)
spec.loader.exec_module(texbake)

PASS = []
FAIL = []


def ok(cond, label):
    (PASS if cond else FAIL).append(label)
    print(("ok   " if cond else "FAIL ") + label, flush=True)


def expect_error(fn, needle, label):
    try:
        fn()
    except Exception as err:
        ok(needle.lower() in str(err).lower(),
           label + " (got: %s)" % str(err)[:80])
    else:
        ok(False, label + " (no error raised)")


TMP = tempfile.mkdtemp(prefix="madi_texbake_")
cube = bpy.data.objects["Cube"]
mat = bpy.data.materials.new("BakeMat")
mat.use_nodes = True
if cube.data.materials:
    cube.data.materials[0] = mat
else:
    cube.data.materials.append(mat)

# ⚠ NATIVE CONTRACT (0.29.0): the bake runs on the SCENE's own sampling,
# denoising and device, exactly like the panel's button — so the suite,
# like a user, sets its scene up first and then expects the echo to match.
SCENE_SAMPLES = 4
bpy.context.scene.cycles.samples = SCENE_SAMPLES

# ------------------------------------------------- 1. list_materials
reply = texbake.list_materials()
mats = {m["name"]: m for m in reply["materials"]}
ok("BakeMat" in mats, "list: the scene material is listed")
ok("Cube" in mats["BakeMat"]["objects"],
   "list: it knows which mesh uses the material")
ok(mats["BakeMat"]["has_nodes"] is True, "list: node-tree flag is real")

# ------------------------------------------------- 2. validation
expect_error(lambda: texbake.bake_texture("BakeMat", "SPARKLE", 64, 64),
             "unknown bake type", "validate: a made-up type is refused")
expect_error(lambda: texbake.bake_texture("BakeMat", "NORMAL", 4, 64),
             "resolution", "validate: a 4px side is refused")
expect_error(lambda: texbake.bake_texture("BakeMat", "NORMAL", 64, 99999),
             "resolution", "validate: a 99999px side is refused")
expect_error(lambda: texbake.bake_texture("NoSuchMat", "NORMAL", 64, 64),
             "not found", "validate: a missing material is refused")
expect_error(lambda: texbake.bake_texture("BakeMat", "NORMAL", 64, 64,
                                          object_name="NoSuchOb"),
             "not found", "validate: a missing object is refused")
expect_error(lambda: texbake.bake_texture("BakeMat", "NORMAL", 64, 64),
             "unsaved", "validate: no path + unsaved blend says SAVE, "
             "not a traceback")

# an object that uses the material but has no UVs: the error teaches
bald = bpy.data.meshes.new("bald")
bald.from_pydata([(0, 0, 0), (1, 0, 0), (0, 1, 0)], [], [(0, 1, 2)])
ob_bald = bpy.data.objects.new("Bald", bald)
bpy.context.scene.collection.objects.link(ob_bald)
ob_bald.data.materials.append(bpy.data.materials.new("LonelyMat"))
expect_error(lambda: texbake.bake_texture("LonelyMat", "NORMAL", 64, 64,
                                          out_path=os.path.join(TMP, "x.png")),
             "unwrap", "validate: material whose only mesh lacks UVs "
             "says unwrap")

# ------------------------------------------------- 3. the maps themselves


def load_stats(path, non_color=False):
    # ⚠ load() assigns sRGB to a PNG, and img.pixels DECODES through the
    # colorspace — a data map read without this reports 0.214 for a stored
    # 0.5. The file was right; the reader has to be told it holds data.
    img = bpy.data.images.load(path)
    if non_color:
        img.colorspace_settings.name = "Non-Color"
    px = list(img.pixels)
    rgb = [px[i] for i in range(len(px)) if i % 4 != 3]
    nz = sum(1 for v in rgb if v > 0.004)
    stats = (min(rgb), max(rgb), sum(rgb) / len(rgb), nz / len(rgb))
    bpy.data.images.remove(img)
    return stats


saved_engine = bpy.context.scene.render.engine
saved_samples = bpy.context.scene.cycles.samples
sel_before = {o.name for o in bpy.context.view_layer.objects if o.select_get()}

r = texbake.bake_texture("BakeMat", "NORMAL", 128, 128,
                         out_path=os.path.join(TMP, "n.png"))
ok(os.path.isfile(r["path"]) and os.path.getsize(r["path"]) > 500,
   "normal: the file is on disk")
ok(r["samples"] == SCENE_SAMPLES,
   "normal: the bake ran at the SCENE's own samples — native leaves "
   "sampling to the scene (the fast engine's 1-sample rule is gone)")
ok(r["device"] == "CPU" and r["bake_type"] == "NORMAL"
   and r["material"] == "BakeMat" and r["object"] == "Cube"
   and r["width"] == 128 and isinstance(r["seconds"], float),
   "normal: the reply echoes every input (the save_abc rule) — device is "
   "the scene's own, read not managed")
mn, mx, mean, nz = load_stats(r["path"])
ok(0.4 < mean < 0.75 and mx > 0.9,
   "normal: the file really holds a normal map (mean %.2f max %.2f)"
   % (mean, mx))

r = texbake.bake_texture("BakeMat", "ROUGHNESS", 128, 128,
                         out_path=os.path.join(TMP, "r.png"))
mn, mx, mean, nz = load_stats(r["path"], non_color=True)
ok(abs(mx - 0.5) < 0.02,
   "roughness: 0.5 stays 0.5 in the file — Non-Color float target "
   "(sRGB would read 0.737; got max %.3f)" % mx)

r = texbake.bake_texture("BakeMat", "UV", 128, 128,
                         out_path=os.path.join(TMP, "u.png"))
mn, mx, mean, nz = load_stats(r["path"])
ok(mx > 0.9 and nz > 0.2,
   "uv: the emission-rewire produces a real gradient (native 5.2 UV bake "
   "reads zero; got max %.2f nz %.2f)" % (mx, nz))
ok(r["samples"] == SCENE_SAMPLES,
   "uv: the rewire bakes at the scene's samples too")
out_node = next(n for n in mat.node_tree.nodes if n.type == "OUTPUT_MATERIAL")
feeder = out_node.inputs["Surface"].links[0].from_socket.node
ok(feeder.type == "BSDF_PRINCIPLED",
   "uv: the material's own shader is wired back afterwards")
ok(all(n.type != "TEX_IMAGE" for n in mat.node_tree.nodes),
   "cleanup: no temp image node is left in the material")

r = texbake.bake_texture("BakeMat", "COMBINED", 128, 128,
                         out_path=os.path.join(TMP, "c.png"))
mn, mx, mean, nz = load_stats(r["path"])
ok(mx > 0.1 and nz > 0.2,
   "combined: the explicit pass_filter produces light, not black "
   "(empty-filter default bakes black; got max %.2f)" % mx)
ok(r["samples"] == SCENE_SAMPLES,
   "combined: a lit map also bakes at the scene's samples — no fast "
   "profile any more")

r = texbake.bake_texture("BakeMat", "POSITION", 64, 64,
                         out_path=os.path.join(TMP, "p.png"))
ok(r["path"].lower().endswith(".exr"),
   "position: forced to EXR — PNG would clamp the negative half")
ok(os.path.isfile(r["path"]), "position: the exr is on disk")

# ------------------------------------------------- 4. restore
ok(bpy.context.scene.render.engine == saved_engine,
   "restore: the render engine is put back (%s)" % saved_engine)
ok(bpy.context.scene.cycles.samples == saved_samples,
   "restore: cycles samples are put back")
sel_after = {o.name for o in bpy.context.view_layer.objects if o.select_get()}
ok(sel_after == sel_before, "restore: the selection is put back")
ok(bpy.data.images.get("MADI_bake_tmp") is None,
   "restore: the temp bake image is gone from the file")

# margin / explicit object still answer
r = texbake.bake_texture("BakeMat", "AO", 64, 64,
                         out_path=os.path.join(TMP, "ao.png"),
                         object_name="Cube", margin=4)
ok(r["object"] == "Cube" and os.path.isfile(r["path"]),
   "ao: explicit object + margin path works")

# --------------------------------- 5. multi-material isolation (0.24.1)
# Marty's live find: "I chose torso but it baked Face". A G8 body is ONE
# mesh whose materials' UV layouts OVERLAP in the same 0-1 tile, and Cycles
# bakes EVERY face through its own material's active image node — so with
# the real image in every slot, the other materials' faces overwrote the
# asked-for map. The fixture is the smallest version of that body: two
# quads, BOTH mapped to the full tile, one per material.
mesh = bpy.data.meshes.new("twin")
mesh.from_pydata(
    [(-1, 0, 0), (0, 0, 0), (0, 1, 0), (-1, 1, 0),
     (1, 0, 0), (2, 0, 0), (2, 1, 0), (1, 1, 0)],
    [], [(0, 1, 2, 3), (4, 5, 6, 7)])
uv = mesh.uv_layers.new(name="UVMap")
quad_uv = ((0, 0), (1, 0), (1, 1), (0, 1))
for i in range(len(mesh.loops)):
    uv.data[i].uv = quad_uv[i % 4]
ob2 = bpy.data.objects.new("Twin", mesh)
bpy.context.scene.collection.objects.link(ob2)
mat_a = bpy.data.materials.new("TwinA")
mat_b = bpy.data.materials.new("TwinB")
for m, rough in ((mat_a, 0.25), (mat_b, 0.75)):
    m.use_nodes = True
    bsdf = next(n for n in m.node_tree.nodes if n.type == "BSDF_PRINCIPLED")
    bsdf.inputs["Roughness"].default_value = rough
mesh.materials.append(mat_a)
mesh.materials.append(mat_b)
mesh.polygons[1].material_index = 1

r = texbake.bake_texture("TwinA", "ROUGHNESS", 64, 64,
                         out_path=os.path.join(TMP, "twin.png"),
                         object_name="Twin")
ok(r["isolated_slots"] == 1,
   "isolate: the reply says one other material was shunted aside")
img = bpy.data.images.load(r["path"])
img.colorspace_settings.name = "Non-Color"
px = list(img.pixels)
rgb = [px[i] for i in range(len(px)) if i % 4 != 3]
bpy.data.images.remove(img)
leaked = sum(1 for v in rgb if abs(v - 0.75) < 0.05)
held = sum(1 for v in rgb if abs(v - 0.25) < 0.02)
ok(leaked == 0,
   "isolate: NOT ONE pixel of the other material's 0.75 leaked in "
   "(the bug painted the whole tile with it)")
ok(held > len(rgb) * 0.5,
   "isolate: the asked-for material's 0.25 fills the tile (%d of %d)"
   % (held, len(rgb)))
ok(bpy.data.images.get("MADI_bake_void") is None,
   "isolate: the sacrificial image is cleaned up")

# --------------------------------- 6. empty results become WORDS (0.24.2)
# Marty's second live find: a COMBINED bake of an unlit picker scene,
# through a material whose ACTIVE output renders transparent, writes
# (0,0,0,0) on every covered texel, reports FINISHED, and the app said
# "Baked ✓". Blender behaves identically — the fix is that the reply
# MEASURES its result and names the cause.

# a lit COMBINED on the factory scene carries no warning
r = texbake.bake_texture("BakeMat", "COMBINED", 64, 64,
                         out_path=os.path.join(TMP, "w0.png"))
ok(r["warning"] is None and r["content"]["rgb_max"] > 0.05,
   "warn: a lit bake with real content stays quiet (rgb_max %.3f)"
   % r["content"]["rgb_max"])

# the factory light off AND the world blacked -> the warning names it
# (the factory world alone emits a dim 0.05, which honestly lights a bake)
light = bpy.data.objects.get("Light")
light.hide_render = True
wbg = next(n for n in bpy.context.scene.world.node_tree.nodes
           if n.type == "BACKGROUND")
saved_strength = wbg.inputs["Strength"].default_value
wbg.inputs["Strength"].default_value = 0.0
r = texbake.bake_texture("BakeMat", "COMBINED", 64, 64,
                         out_path=os.path.join(TMP, "w1.png"))
ok(r["warning"] is not None and "no enabled lights" in r["warning"],
   "warn: an unlit COMBINED says the scene has no lights")
ok(r["content"]["rgb_max"] < 0.001,
   "warn: and the content stats agree it is empty")
light.hide_render = False
wbg.inputs["Strength"].default_value = saved_strength

# a transparent surface -> named, plus the two-output diagnosis
ghost = bpy.data.materials.new("GhostMat")
ghost.use_nodes = True
gtree = ghost.node_tree
gout = next(n for n in gtree.nodes if n.type == "OUTPUT_MATERIAL")
n_trans = gtree.nodes.new("ShaderNodeBsdfTransparent")
gtree.links.new(n_trans.outputs[0], gout.inputs["Surface"])
gout2 = gtree.nodes.new("ShaderNodeOutputMaterial")
gout.is_active_output = True
cube.data.materials[0] = ghost
# ⚠ margin 0 here ON PURPOSE: this probes the transparency DIAGNOSIS,
# which reads the surface's own alpha — a native margin would write its
# band over exactly the texels the diagnosis reads
r = texbake.bake_texture("GhostMat", "COMBINED", 64, 64,
                         out_path=os.path.join(TMP, "w2.png"), margin=0)
ok(r["warning"] is not None
   and "fully transparent" in r["warning"]
   and "2 Material Output nodes" in r["warning"],
   "warn: a transparent active output names BOTH the transparency and "
   "the second output node")
cube.data.materials[0] = mat

# a data map that is honestly black never warns
r = texbake.bake_texture("BakeMat", "ROUGHNESS", 64, 64,
                         out_path=os.path.join(TMP, "w3.png"))
ok(r["warning"] is None,
   "warn: a black data map is a fact, not a warning")
ok(r["uv_tile"] == [0, 0],
   "udim: an in-tile material reports tile [0,0] and is left alone")

# --------------------------------- 7. UDIM islands bake anyway (0.24.3)
# Marty's G8 lays each material on its own tile (Face 0-1, Torso 1-2,
# Legs 2-3, Arms 3-4). A bake image covers 0-1, so an off-tile material
# rasterised NOTHING — a fully transparent "success" that survived every
# shader-level diagnosis of the night. The bake now shifts the target
# material's islands into 0-1 and puts them back.
tile_mesh = bpy.data.meshes.new("tilequad")
tile_mesh.from_pydata(
    [(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)], [], [(0, 1, 2, 3)])
tuv = tile_mesh.uv_layers.new(name="UVMap")
for i, (u, v) in enumerate(((1.1, 0.1), (1.9, 0.1), (1.9, 0.9), (1.1, 0.9))):
    tuv.data[i].uv = (u, v)
ob3 = bpy.data.objects.new("TileQuad", tile_mesh)
bpy.context.scene.collection.objects.link(ob3)
mat_t = bpy.data.materials.new("TileMat")
mat_t.use_nodes = True
tile_mesh.materials.append(mat_t)
uv_before = [tuple(tuv.data[i].uv) for i in range(4)]

r = texbake.bake_texture("TileMat", "ROUGHNESS", 64, 64,
                         out_path=os.path.join(TMP, "tile.png"),
                         object_name="TileQuad")
ok(r["uv_tile"] == [1, 0],
   "udim: the off-tile material reports the tile it was shifted from")
mn, mx, mean, nz = load_stats(r["path"], non_color=True)
ok(abs(mx - 0.5) < 0.02 and nz > 0.5,
   "udim: the off-tile island really bakes now (max %.3f nz %.2f — it "
   "rasterised nothing before the shift)" % (mx, nz))
uv_after = [tuple(tuv.data[i].uv) for i in range(4)]
ok(all(abs(a[0] - b[0]) < 1e-6 and abs(a[1] - b[1]) < 1e-6
       for a, b in zip(uv_before, uv_after)),
   "udim: the mesh's UVs are back exactly where they were")

# --------------------------------- 8. the rest of Blender's panel (0.25.0)
# Marty asked for "all the bake options we have in blender" plus a sample
# count. They are GROWN PARAMETERS on a command that already existed, so
# the `options` echo is the only thing an app can check.

def png(name):
    return os.path.join(TMP, name)


def pixels_differ(path_a, path_b):
    """How many channel values differ between two baked files.

    ⚠ Statistics are NOT enough to tell these maps apart: flipping G on an
    object-space normal bake of a cube swaps the +Y and -Y faces' values,
    which have equal area — so min, max AND mean all come out identical.
    Only a per-pixel comparison sees it."""
    img_a = bpy.data.images.load(path_a)
    img_b = bpy.data.images.load(path_b)
    px_a, px_b = list(img_a.pixels), list(img_b.pixels)
    bpy.data.images.remove(img_a)
    bpy.data.images.remove(img_b)
    return sum(1 for a, b in zip(px_a, px_b) if abs(a - b) > 0.01)


bpy.context.scene.camera = None      # the factory scene ships one

expect_error(lambda: texbake.bake_texture("BakeMat", "DIFFUSE", 64, 64,
                                          out_path=png("v.png"),
                                          view_from="SIDEWAYS"),
             "unknown view from", "options: a made-up View From is refused")
expect_error(lambda: texbake.bake_texture("BakeMat", "NORMAL", 64, 64,
                                          out_path=png("v.png"),
                                          normal_space="WORLD"),
             "unknown normal space", "options: a made-up space is refused")
expect_error(lambda: texbake.bake_texture("BakeMat", "NORMAL", 64, 64,
                                          out_path=png("v.png"),
                                          normal_swizzle=("POS_X", "POS_Y")),
             "three axes", "options: a two-axis swizzle is refused")
expect_error(lambda: texbake.bake_texture("BakeMat", "NORMAL", 64, 64,
                                          out_path=png("v.png"),
                                          normal_swizzle=("A", "B", "C")),
             "unknown swizzle", "options: a made-up swizzle axis is refused")
expect_error(lambda: texbake.bake_texture("BakeMat", "DIFFUSE", 64, 64,
                                          out_path=png("v.png"),
                                          margin_type="FEATHER"),
             "unknown margin type", "options: a made-up margin type is "
             "refused")
expect_error(lambda: texbake.bake_texture("BakeMat", "DIFFUSE", 64, 64,
                                          out_path=png("v.png"),
                                          pass_filter=[]),
             "switched off", "options: an all-off filter is refused in "
             "words, not baked black")
expect_error(lambda: texbake.bake_texture("BakeMat", "DIFFUSE", 64, 64,
                                          out_path=png("v.png"),
                                          view_from="ACTIVE_CAMERA"),
             "needs a camera", "options: Active Camera without a camera is "
             "refused rather than quietly baked above-surface")

# an explicit sample count is HONOURED, even on a data map
r = texbake.bake_texture("BakeMat", "COMBINED", 64, 64,
                         out_path=png("s.png"), samples=3)
ok(r["samples"] == 3 and r["options"]["samples_auto"] is False,
   "samples: an explicit count is used and flagged as not automatic")
r = texbake.bake_texture("BakeMat", "NORMAL", 64, 64,
                         out_path=png("sn.png"), samples=5)
ok(r["samples"] == 5,
   "samples: honoured on a DATA map too — baking 1 when 5 was asked for "
   "would be the quiet substitution this module exists to avoid")
r = texbake.bake_texture("BakeMat", "NORMAL", 64, 64,
                         out_path=png("sa.png"))
ok(r["samples"] == SCENE_SAMPLES and r["options"]["samples_auto"] is True,
   "samples: no explicit count = the SCENE's render samples, echoed as "
   "automatic — the native contract")
r = texbake.bake_texture("BakeMat", "COMBINED", 64, 64,
                         out_path=png("sc.png"), samples=99999)
ok(r["samples"] == texbake.MAX_SAMPLES,
   "samples: an absurd count is clamped to the cap")

# the contributions really reach the operator: DIRECT-only vs everything
lamp = bpy.data.lights.new("BakeSun", type="SUN")
lamp.energy = 5
sun = bpy.data.objects.new("BakeSun", lamp)
bpy.context.scene.collection.objects.link(sun)
r_all = texbake.bake_texture("BakeMat", "DIFFUSE", 64, 64,
                             out_path=png("d_all.png"))
r_col = texbake.bake_texture("BakeMat", "DIFFUSE", 64, 64,
                             out_path=png("d_col.png"),
                             pass_filter=["COLOR"])
ok(sorted(r_all["options"]["pass_filter"]) == ["COLOR", "DIRECT", "INDIRECT"],
   "influence: no filter given means every contribution, Blender's default")
ok(r_col["options"]["pass_filter"] == ["COLOR"],
   "influence: an explicit filter is echoed exactly as sent")
ok(pixels_differ(r_all["path"], r_col["path"]) > 100,
   "influence: colour-only really bakes a different map from the lit one")
r_mixed = texbake.bake_texture("BakeMat", "DIFFUSE", 64, 64,
                               out_path=png("d_mix.png"),
                               pass_filter=["COLOR", "DIFFUSE", "EMIT"])
ok(r_mixed["options"]["pass_filter"] == ["COLOR"],
   "influence: flags this type does not offer are dropped, not passed on")
r_ao = texbake.bake_texture("BakeMat", "AO", 64, 64, out_path=png("ao2.png"),
                            pass_filter=["DIRECT"])
ok(r_ao["options"]["pass_filter"] is None,
   "influence: a type with no Influence panel sends no filter at all")

# View From is a SCENE setting — it must be applied and PUT BACK
bpy.context.scene.render.bake.view_from = "ABOVE_SURFACE"
cam = bpy.data.objects.new("BakeCam", bpy.data.cameras.new("BakeCam"))
bpy.context.scene.collection.objects.link(cam)
bpy.context.scene.camera = cam
r = texbake.bake_texture("BakeMat", "DIFFUSE", 64, 64, out_path=png("vf.png"),
                         view_from="ACTIVE_CAMERA")
ok(r["options"]["view_from"] == "ACTIVE_CAMERA",
   "view: the option is echoed as the one that was used")
ok(bpy.context.scene.render.bake.view_from == "ABOVE_SURFACE",
   "view: and the SCENE setting is put back — it is the only bake option "
   "that has to be written into the user's file")
r = texbake.bake_texture("BakeMat", "ROUGHNESS", 64, 64,
                         out_path=png("vr.png"),
                         view_from="ACTIVE_CAMERA")
ok(r["options"]["view_from"] is None,
   "view: a type Blender exempts reports it did NOT use View From")

# normal space + swizzle change the map, and only report on a normal bake
r_t = texbake.bake_texture("BakeMat", "NORMAL", 64, 64,
                           out_path=png("nt.png"))
r_o = texbake.bake_texture("BakeMat", "NORMAL", 64, 64,
                           out_path=png("no.png"),
                           normal_space="OBJECT")
ok(r_t["options"]["normal_space"] == "TANGENT"
   and r_o["options"]["normal_space"] == "OBJECT",
   "normal: the space is echoed")
ok(pixels_differ(r_t["path"], r_o["path"]) > 100,
   "normal: object space really bakes a different map from tangent")
# ⚠ the swizzle test HAS to be object space: a flat-shaded quad's TANGENT
# normal is exactly (0,0,1), so G is 0.5 and flipping it changes nothing.
r_s = texbake.bake_texture("BakeMat", "NORMAL", 64, 64,
                           out_path=png("ns.png"), normal_space="OBJECT",
                           normal_swizzle=("POS_X", "NEG_Y", "POS_Z"))
ok(r_s["options"]["normal_swizzle"] == ["POS_X", "NEG_Y", "POS_Z"],
   "normal: the swizzle is echoed as sent")
ok(pixels_differ(r_s["path"], r_o["path"]) > 100,
   "normal: flipping G really flips the green channel in the file")
ok(r_col["options"]["normal_space"] is None
   and r_col["options"]["normal_swizzle"] is None,
   "normal: a non-normal bake reports no normal options")

# margin type travels, and the 0.29.0 panel options echo
r = texbake.bake_texture("BakeMat", "ROUGHNESS", 64, 64,
                         out_path=png("mt.png"), margin=7,
                         margin_type="EXTEND")
ok(r["options"]["margin"] == 7 and r["options"]["margin_type"] == "EXTEND",
   "output: margin size and type are echoed")
ok(r["options"]["use_clear"] is True
   and r["options"]["target"] == "IMAGE_TEXTURES",
   "output: Clear Image and Target echo their panel defaults")
s2a_echo = r["options"]["selected_to_active"]
ok(s2a_echo["on"] is False and s2a_echo["cage"] is False
   and s2a_echo["cage_object"] is None,
   "output: the Selected-to-Active family is echoed even when off — the "
   "only capability check a grown parameter gets")
ok("padded" not in r["options"] and "denoise" not in r["options"],
   "output: ⚠ the 0.28.x keys (padded / denoise) are GONE — a native "
   "reply must not carry the old engine's echoes")

bpy.context.scene.camera = None

# ------------------------------------------------- 9. bake_targets (0.26.0)
# The enumeration behind "Bake all slots" and the Bulk bake node — a NEW
# command (not a grown parameter), so the app really can capability-check
# it. Pure reads: nothing here may bake, select or rename anything.

mat2 = bpy.data.materials.new("SecondSlot")
mat2.use_nodes = True
cube.data.materials.append(mat2)
cube.data.materials.append(mat)     # BakeMat AGAIN, in a third slot

r = texbake.bake_targets("material", material="BakeMat")
ok(r["mode"] == "material" and len(r["targets"]) == 1
   and r["targets"][0]["object"] == "Cube",
   "targets: material mode names the object bake_texture would pick")
ok(r["targets"][0]["materials"] == ["BakeMat", "SecondSlot"],
   "targets: all slots in slot order, the duplicate slot deduped (got %r)"
   % (r["targets"][0]["materials"],))

expect_error(lambda: texbake.bake_targets("material", material="Nope"),
             "not found", "targets: a missing material is refused")
expect_error(lambda: texbake.bake_targets("wat"),
             "unknown mode", "targets: a made-up mode is refused")

# selected: meshes with UVs+materials pass, everything else is COUNTED
for o in bpy.context.view_layer.objects:
    o.select_set(False)
cube.select_set(True)
ob_bald.select_set(True)            # mesh, material, NO UVs -> skipped
sun.select_set(True)                # a lamp -> skipped
r = texbake.bake_targets("selected")
ok([t["object"] for t in r["targets"]] == ["Cube"] and r["skipped"] == 2,
   "targets: selected mode keeps the bakeable mesh and counts the lamp "
   "and the UV-less mesh as skipped (got %r / %d)"
   % ([t["object"] for t in r["targets"]], r["skipped"]))
for o in bpy.context.view_layer.objects:
    o.select_set(False)
r = texbake.bake_targets("selected")
ok(r["targets"] == [] and r["skipped"] == 0,
   "targets: an empty selection is an empty answer, not an error")

# collection: recursive through children, deduped, same filter
col = bpy.data.collections.new("Props")
bpy.context.scene.collection.children.link(col)
col.objects.link(cube)
sub = bpy.data.collections.new("SubProps")
col.children.link(sub)
sub.objects.link(ob_bald)
r = texbake.bake_targets("collection", collection="Props")
ok([t["object"] for t in r["targets"]] == ["Cube"] and r["skipped"] == 1,
   "targets: collection mode reaches through the child collection and "
   "still filters (got %r / %d)"
   % ([t["object"] for t in r["targets"]], r["skipped"]))
expect_error(lambda: texbake.bake_targets("collection", collection="Nope"),
             "not found", "targets: a missing collection is refused")

rows = {c["name"]: c for c in texbake.list_collections()["collections"]}
ok("Props" in rows and "SubProps" in rows,
   "collections: both new collections are listed")
ok(rows["Props"]["depth"] == 0 and rows["SubProps"]["depth"] == 1,
   "collections: the child carries its outliner depth for menu indenting")
ok(rows["Props"]["meshes"] == 1 and rows["SubProps"]["meshes"] == 0,
   "collections: mesh counts are BAKEABLE meshes — the UV-less one does "
   "not count, so a doomed folder bake is visible before it runs")

# ------------------------------------ 10. apply_baked_material (0.27.0)
# The Output image node's "Replace shader" tickbox. Marty's correction to
# the first version: *"replace shader should just PLACE the node in the
# material > respective slots and attach it to material output (one of them
# if many)"* — so the map goes INTO the material that was baked, wired to
# its active output. No new material, no slot reassignment. The one command
# in this module that WRITES.

def slot_names():
    return [s.material.name if s.material else None
            for s in cube.material_slots]


def node_of(material, node_type):
    return next((n for n in material.node_tree.nodes if n.type == node_type),
                None)


def feeds(socket, node):
    """Is `socket` linked from `node`? ⚠ Compare node NAMES, never `is`:
    a NODE is not an ID datablock, so bpy hands out a fresh Python wrapper
    on every access and `link.from_node is node` reads False for the very
    link that exists (materials and objects DO cache, which is why `is`
    works on those)."""
    return bool(socket.links) and socket.links[0].from_node.name == node.name


def baked_node(material):
    return next((n for n in material.node_tree.nodes
                 if n.type == "TEX_IMAGE" and n.get(texbake.BAKED_MARK)), None)


# the material starts with a real network: Principled -> Output
mats_before = len(bpy.data.materials)
out_node = node_of(mat, "OUTPUT_MATERIAL")
principled = node_of(mat, "BSDF_PRINCIPLED")
ok(out_node is not None and principled is not None
   and feeds(out_node.inputs["Surface"], principled),
   "replace: (the fixture material really is Principled -> Output first)")

r_rough = texbake.bake_texture("BakeMat", "ROUGHNESS", 64, 64,
                               out_path=png("rep_rough.png"))
reply = texbake.apply_baked_material([
    {"object": "Cube", "material": "BakeMat", "path": r_rough["path"],
     "bake_type": "ROUGHNESS"}])
ok(reply["count"] == 1 and reply["applied"][0]["material"] == "BakeMat",
   "replace: the map goes into the material that was baked (got %r)"
   % (reply["applied"],))
ok(slot_names() == ["BakeMat", "SecondSlot", "BakeMat"],
   "replace: ⚠ NO slot is reassigned and NO new material appears — Marty's "
   "correction: place the node, do not swap the shader (got %r)"
   % (slot_names(),))
ok(len(bpy.data.materials) == mats_before,
   "replace: and the blend gained no material at all")

tex_node = baked_node(mat)
ok(tex_node is not None and os.path.normcase(os.path.abspath(
    bpy.path.abspath(tex_node.image.filepath)))
   == os.path.normcase(os.path.abspath(r_rough["path"])),
   "replace: the placed node holds the file that was just baked")
ok(feeds(out_node.inputs["Surface"], tex_node),
   "replace: and it is wired straight to the Material Output")
ok(reply["applied"][0]["was_fed_by"] == principled.name,
   "replace: the reply names what USED to feed the output, so it is "
   "obvious what was unplugged (got %r)"
   % reply["applied"][0]["was_fed_by"])
ok(principled.name in [n.name for n in mat.node_tree.nodes],
   "replace: ⚠ the original network is still THERE — just unplugged, so "
   "one re-drag undoes this")
ok(tex_node.image.colorspace_settings.name == "Non-Color",
   "replace: a data map is read as data — the same sRGB trap the bake "
   "itself works around")
ok(not tex_node.inputs["Vector"].links,
   "replace: with the islands in 0-1 the node needs no UV wiring — an "
   "image node with an empty Vector samples the active RENDER uv map, "
   "which is the one the bake used")

# re-running reuses OUR node instead of stacking image nodes
before_nodes = len(mat.node_tree.nodes)
texbake.apply_baked_material([
    {"object": "Cube", "material": "BakeMat", "path": r_rough["path"],
     "bake_type": "ROUGHNESS"}])
ok(len(mat.node_tree.nodes) == before_nodes
   and len([n for n in mat.node_tree.nodes
            if n.type == "TEX_IMAGE" and n.get(texbake.BAKED_MARK)]) == 1,
   "replace: baking twice reuses the placed node, it does not stack a pile "
   "of image nodes (got %d nodes, was %d)"
   % (len(mat.node_tree.nodes), before_nodes))

# many outputs: the ACTIVE one wins, which is the one the bake followed
second_out = mat.node_tree.nodes.new("ShaderNodeOutputMaterial")
second_out.location = (600, -300)
second_out.is_active_output = True
texbake.apply_baked_material([
    {"object": "Cube", "material": "BakeMat", "path": r_rough["path"],
     "bake_type": "ROUGHNESS"}])
ok(feeds(second_out.inputs["Surface"], baked_node(mat)),
   "replace: with two Material Outputs it attaches to the ACTIVE one — "
   "the same output the bake itself renders through")
mat.node_tree.nodes.remove(second_out)
out_node = node_of(mat, "OUTPUT_MATERIAL")
out_node.is_active_output = True

# ⚠ a UDIM material: the map is 0-1, the mesh is not
uv_layer = texbake._bake_uv_layer(cube)
saved_uv = [(d.uv[0], d.uv[1]) for d in uv_layer.data]
for d in uv_layer.data:
    d.uv[0] += 1.0                      # everything onto tile 1-2
r_udim = texbake.bake_texture("BakeMat", "ROUGHNESS", 64, 64,
                              out_path=png("rep_udim.png"))
ok(r_udim["uv_tile"] == [1, 0],
   "replace: (the bake still shifts the off-tile material into 0-1)")
texbake.apply_baked_material([
    {"object": "Cube", "material": "BakeMat", "path": r_udim["path"],
     "bake_type": "ROUGHNESS"}])
mapping = node_of(mat, "MAPPING")
tex_node = baked_node(mat)
ok(mapping is not None
   and round(mapping.inputs["Location"].default_value[0], 3) == -1.0
   and feeds(tex_node.inputs["Vector"], mapping),
   "replace: ⚠ a UDIM material gets a UV Map + Mapping pair that subtracts "
   "its tile — the map is 0-1 while the mesh is not, so a bare image node "
   "would sample the wrong square")
uvm = node_of(mat, "UVMAP")
ok(uvm is not None and uvm.uv_map == texbake._bake_uv_layer(cube).name,
   "replace: and that UV Map node names the layer the bake sampled")
for d, pair in zip(uv_layer.data, saved_uv):
    d.uv = pair

# the bake and the replacement agree on WHICH uv layer
second_uv = cube.data.uv_layers.new(name="Bake_UV")
second_uv.active_render = True
cube.data.uv_layers.active = cube.data.uv_layers[0]
ok(texbake._bake_uv_layer(cube).name == "Bake_UV",
   "replace: the UV layer is the ACTIVE RENDER one — an image node with no "
   "Vector input samples that, not the one merely selected for editing")
cube.data.uv_layers.remove(second_uv)

# refusals and skips: one bad row must not sink the rest
expect_error(lambda: texbake.apply_baked_material([]),
             "nothing to replace", "replace: an empty list is refused")
reply = texbake.apply_baked_material([
    {"object": "Cube", "material": "BakeMat", "path": r_rough["path"],
     "bake_type": "ROUGHNESS"},
    {"object": "Ghost", "material": "BakeMat", "path": r_rough["path"],
     "bake_type": "ROUGHNESS"},
    {"object": "Cube", "material": "SecondSlot",
     "path": png("never_baked.png"), "bake_type": "ROUGHNESS"}])
ok(reply["count"] == 1 and len(reply["skipped"]) == 2,
   "replace: a missing object and a missing file are SKIPPED with reasons, "
   "the good row still lands (got %r)" % (reply["skipped"],))
ok(any("gone" in s["reason"] for s in reply["skipped"])
   and any("missing" in s["reason"] for s in reply["skipped"]),
   "replace: and each skip says which problem it hit")
ok(baked_node(bpy.data.materials["SecondSlot"]) is None,
   "replace: the material whose file was missing was not touched")

# ------------------------------ 11. matching the view transform (0.28.0)
# Marty: "can we somehow match the baked view transform to whatever is
# chosen in blender?" — his baked map read far brighter than his viewport,
# and it WAS: a lit bake clips at 1.0 in an 8-bit buffer before anything
# else happens, and save() never applies colour management at all.

bpy.context.scene.view_settings.view_transform = "AgX"

# a material bright enough that AgX has something to roll off
bright = bpy.data.materials.new("BrightMat")
bright.use_nodes = True
btree = bright.node_tree
bout = next(n for n in btree.nodes if n.type == "OUTPUT_MATERIAL")
bem = btree.nodes.new("ShaderNodeEmission")
bem.inputs["Color"].default_value = (1.0, 0.5, 0.25, 1.0)
bem.inputs["Strength"].default_value = 4.0
btree.links.new(bem.outputs["Emission"], bout.inputs["Surface"])
cube.data.materials[0] = bright


def read_raw(path):
    """Centre pixel of a file, read as DATA. ⚠ img.pixels DECODES through
    the colorspace, so an sRGB PNG read normally comes back linearised and
    the two files would not be comparable."""
    img = bpy.data.images.load(path)
    img.colorspace_settings.name = "Non-Color"
    w, h = img.size
    i = ((h // 2) * w + (w // 2)) * 4
    px = [round(v, 4) for v in img.pixels[i:i + 3]]
    bpy.data.images.remove(img)
    return px


r_raw = texbake.bake_texture("BrightMat", "EMIT", 64, 64,
                             out_path=png("xf_raw.png"))
r_xf = texbake.bake_texture("BrightMat", "EMIT", 64, 64,
                            out_path=png("xf_agx.png"),
                            view_transform=True)
raw_px, xf_px = read_raw(r_raw["path"]), read_raw(r_xf["path"])
ok(raw_px[0] >= 0.999 and raw_px[1] >= 0.999,
   "viewxf: ⚠ the RAW path clips a bright map flat — an 8-bit buffer holds "
   "1.0 where the render shows a rolled-off highlight (got %r)" % raw_px)
ok(xf_px[0] < 0.99 and xf_px[0] > 0.5,
   "viewxf: through the view transform the highlight is ROLLED OFF, not "
   "clipped (got %r)" % xf_px)
ok(xf_px[0] > xf_px[1] > xf_px[2],
   "viewxf: and the warm tint survives as a gradient instead of three "
   "clipped channels (got %r)" % xf_px)
ok(r_xf["options"]["view_transform"] == "AgX"
   and r_raw["options"]["view_transform"] is None,
   "viewxf: the reply NAMES the transform it applied — the only proof a "
   "grown parameter landed (got %r / %r)"
   % (r_xf["options"]["view_transform"], r_raw["options"]["view_transform"]))

# Standard must differ from AgX, or "matching Blender" means nothing
bpy.context.scene.view_settings.view_transform = "Standard"
r_std = texbake.bake_texture("BrightMat", "EMIT", 64, 64,
                             out_path=png("xf_std.png"),
                             view_transform=True)
ok(r_std["options"]["view_transform"] == "Standard"
   and read_raw(r_std["path"]) != xf_px,
   "viewxf: it follows WHATEVER is chosen — Standard gives a different "
   "file from AgX (got %r vs %r)" % (read_raw(r_std["path"]), xf_px))
bpy.context.scene.view_settings.view_transform = "AgX"

# the user's image settings are borrowed, not taken
KEYS = ("media_type", "file_format", "color_management", "color_mode")
before_is = {k: getattr(bpy.context.scene.render.image_settings, k, None)
             for k in KEYS}
texbake.bake_texture("BrightMat", "EMIT", 64, 64,
                     out_path=png("xf_restore.png"),
                     view_transform=True)
after_is = {k: getattr(bpy.context.scene.render.image_settings, k, None)
            for k in KEYS}
ok(before_is == after_is,
   "viewxf: ⚠ scene.render.image_settings is restored — save_render reads "
   "it, so it has to be written into the user's scene first (%r -> %r)"
   % (before_is, after_is))

# ⚠ A SCENE SET UP FOR VIDEO OUTPUT — Marty's normal state, and the first
# real thing he tried. `file_format` is filtered by `media_type`, so
# assigning "PNG" while the scene says VIDEO RAISES: the bake produced no
# file at all and the reason only reached the status line.
img_set = bpy.context.scene.render.image_settings
if hasattr(img_set, "media_type"):
    img_set.media_type = "VIDEO"
    ok(img_set.file_format == "FFMPEG",
       "viewxf: (a VIDEO scene really does hold a movie format: %r)"
       % img_set.file_format)
    vid_path = png("xf_video.png")
    try:
        r_vid = texbake.bake_texture("BrightMat", "EMIT", 64, 64,
                                     out_path=vid_path,
                                     view_transform=True)
        raised = None
    except Exception as err:                       # noqa: BLE001
        r_vid, raised = None, err
    ok(raised is None and os.path.isfile(r_vid["path"]),
       "viewxf: ⚠ a scene set to VIDEO output still bakes and still writes "
       "the file — media_type has to be switched to IMAGE before the format "
       "can be (got %r)" % (raised,))
    ok(img_set.media_type == "VIDEO" and img_set.file_format == "FFMPEG",
       "viewxf: ⚠ ...and the VIDEO setup is put back — restoring the format "
       "before the media type would fail the same way round (got %s / %s)"
       % (img_set.media_type, img_set.file_format))
    img_set.media_type = "IMAGE"
    img_set.file_format = "PNG"

expect_error(lambda: texbake.bake_texture("BrightMat", "ROUGHNESS", 64, 64,
                                          out_path=png("xf_data.png"),
                                          view_transform=True),
             "cannot be applied",
             "viewxf: ⚠ a DATA map refuses it — a view transform would "
             "re-encode values the shader reads back as numbers")
cube.data.materials[0] = mat

# ------------------------- 12. raw clipping and the EXR escape (0.28.2)
# Marty's chest ran 1.5–4.0 scene-referred; a raw PNG pinned 15% of the
# map flat at 1.0 and the flat patch + its hard edge read as "a seam and
# the torso area is weird" — clipping does not LOOK like an error. So a
# byte-buffer bake measures its clipped fraction and says so, and naming
# the file .exr gets a float buffer that keeps the real values.

cube.data.materials[0] = bright              # the 4.0-strength emission

r_clip = texbake.bake_texture("BrightMat", "EMIT", 64, 64,
                              out_path=png("clip_raw.png"))
ok(r_clip["content"]["clipped"] > 0.5,
   "clip: the reply MEASURES the clipped fraction of the covered texels "
   "(got %r)" % (r_clip["content"]["clipped"],))
ok("CLIPPED" in (r_clip["warning"] or "")
   and ".exr" in (r_clip["warning"] or ""),
   "clip: ⚠ a mostly-clipped raw PNG warns in words and names the escape "
   "(got %r)" % (r_clip["warning"],))

r_exr = texbake.bake_texture("BrightMat", "EMIT", 64, 64,
                             out_path=png("clip_raw.exr"))
ok(r_exr["path"].lower().endswith(".exr")
   and "CLIPPED" not in (r_exr["warning"] or ""),
   "clip: the same bake to .exr does not warn — nothing was lost")
img_exr = bpy.data.images.load(r_exr["path"])
exr_max = max(img_exr.pixels)
bpy.data.images.remove(img_exr)
ok(exr_max > 1.5,
   "clip: ⚠ the EXR really holds scene values ABOVE 1.0 (max %.2f) — a "
   "raw .exr bake gets a FLOAT buffer, not just a float container around "
   "clipped bytes" % exr_max)

# the view-transform file never warns about clipping: it rolls off instead
r_xf2 = texbake.bake_texture("BrightMat", "EMIT", 64, 64,
                             out_path=png("clip_xf.png"),
                             view_transform=True)
ok("CLIPPED" not in (r_xf2["warning"] or ""),
   "clip: a view-transform bake does not warn — the transform rolled the "
   "values off instead of losing them")
cube.data.materials[0] = mat

# -------------------- 13. THE NATIVE MARGIN CARRIES COLOUR (0.29.0)
# The acceptance check of the native rebuild. 0.28.x forced the operator
# margin to 0 and hand-padded the islands, because fixture measurements
# read 5.2's own margin as a black alpha band. Marty then baked the same
# character through Blender's real Bake panel — NO seams — and ordered the
# pipeline native ("do it exactly the way it is done in blender ...
# calling their functions"). So the suite measures the one thing that
# matters now: a native margin-16 bake grows COLOURED texels past the
# islands, and that band holds ISLAND colour, not black.

pad_ob_me = bpy.data.meshes.new("padme")
# two quads, together spanning the tile, with a UV GAP between them — a
# guaranteed seam down the middle of the image
pad_ob_me.from_pydata(
    [(-2, 0, 0), (0, 0, 0), (0, 2, 0), (-2, 2, 0),
     (0.5, 0, 0), (2.5, 0, 0), (2.5, 2, 0), (0.5, 2, 0)],
    [], [(0, 1, 2, 3), (4, 5, 6, 7)])
pad_ob = bpy.data.objects.new("PadOb", pad_ob_me)
bpy.context.scene.collection.objects.link(pad_ob)
uvl = pad_ob_me.uv_layers.new(name="UVMap")
# quad 2 is deliberately ROTATED in UV space: diagonal island edges are
# where seams live on a real character
quad_uvs = [(0.05, 0.05), (0.42, 0.05), (0.42, 0.95), (0.05, 0.95),
            (0.60, 0.08), (0.93, 0.15), (0.88, 0.92), (0.55, 0.85)]
for li, loop in enumerate(pad_ob_me.loops):
    uvl.data[li].uv = quad_uvs[loop.vertex_index]
pad_mat = bpy.data.materials.new("PadMat")
pad_mat.use_nodes = True
pb = next(n for n in pad_mat.node_tree.nodes if n.type == "BSDF_PRINCIPLED")
pb.inputs["Base Color"].default_value = (0.8, 0.55, 0.45, 1.0)
# ⚠ append() is not assignment: a mesh built from scratch defaults every
# face to slot 0, so appending to a FRESH mesh is fine — but two probe
# fixtures once appended to the factory cube and baked an empty image
pad_ob_me.materials.append(pad_mat)

import numpy as _np


def _file_px(path):
    img = bpy.data.images.load(path)
    img.colorspace_settings.name = "Non-Color"
    w, h = img.size
    buf = _np.empty(w * h * 4, dtype=_np.float32)
    img.pixels.foreach_get(buf)
    bpy.data.images.remove(img)
    px = buf.reshape(h, w, 4)
    return px[..., 3], px[..., :3]


def _coloured_frac(path):
    _a, rgb = _file_px(path)
    return float((rgb.max(axis=-1) > 0.05).mean())


# margin 0 first: the BASELINE the banded files are measured against
r_m0 = texbake.bake_texture("PadMat", "DIFFUSE", 128, 128,
                            out_path=png("nm0.png"),
                            pass_filter=["COLOR"], margin=0)
col0 = _coloured_frac(r_m0["path"])

r_m16 = texbake.bake_texture("PadMat", "DIFFUSE", 128, 128,
                             out_path=png("nm16.png"),
                             pass_filter=["COLOR"], margin=16)
col16 = _coloured_frac(r_m16["path"])
ok(col16 > col0 + 0.05,
   "native margin: ⚠ THE acceptance check — a margin-16 bake grows "
   "coloured texels past the islands (%.1f%% vs %.1f%% at margin 0); if "
   "this ever fails, Blender's own margin regressed and the answer starts "
   "HERE, not at a new padding pipeline"
   % (100 * col16, 100 * col0))
_a16, _rgb16 = _file_px(r_m16["path"])
_a0, _rgb0 = _file_px(r_m0["path"])
_band = (_rgb16.max(axis=-1) > 0.05) & ~(_rgb0.max(axis=-1) > 0.05)
_islands = _rgb0.max(axis=-1) > 0.05
_band_mean = float(_rgb16.mean(axis=-1)[_band].mean())
_island_mean = float(_rgb0.mean(axis=-1)[_islands].mean())
ok(_band_mean > 0.5 * _island_mean,
   "native margin: the band holds ISLAND colour, not the black stitching "
   "0.28.x measured (band %.3f vs islands %.3f)"
   % (_band_mean, _island_mean))

# the second margin type works the same way
r_ext = texbake.bake_texture("PadMat", "DIFFUSE", 128, 128,
                             out_path=png("nmext.png"),
                             pass_filter=["COLOR"], margin=16,
                             margin_type="EXTEND")
ok(_coloured_frac(r_ext["path"]) > col0 + 0.05,
   "native margin: Extend grows the band too — both of Blender's margin "
   "types pass through and work")

# ------------------------------- 14. Clear Image, natively (0.29.0)
# The panel bakes over the EXISTING image when Clear is off; our image
# datablock is fresh each run, so the previous FILE is preloaded. The
# margin band from the bake above is the perfect marker: a re-bake at
# margin 0 with Clear OFF must keep it, with Clear ON must erase it.
p_keep = png("nmkeep.png")
texbake.bake_texture("PadMat", "DIFFUSE", 128, 128, out_path=p_keep,
                     pass_filter=["COLOR"], margin=16)
r_keep = texbake.bake_texture("PadMat", "DIFFUSE", 128, 128,
                              out_path=p_keep, pass_filter=["COLOR"],
                              margin=0, use_clear=False)
ok(r_keep["options"]["use_clear"] is False,
   "clear: the option is echoed")
ok(_coloured_frac(p_keep) > col0 + 0.05,
   "clear: ⚠ Clear OFF keeps the previous file's band under a margin-0 "
   "re-bake — the previous pixels really are preloaded")
r_wipe = texbake.bake_texture("PadMat", "DIFFUSE", 128, 128,
                              out_path=p_keep, pass_filter=["COLOR"],
                              margin=0, use_clear=True)
ok(abs(_coloured_frac(p_keep) - col0) < 0.03,
   "clear: Clear ON starts fresh — the old band is gone")
r_size = texbake.bake_texture("PadMat", "DIFFUSE", 64, 64,
                              out_path=p_keep, pass_filter=["COLOR"],
                              margin=0, use_clear=False)
ok(os.path.isfile(r_size["path"]),
   "clear: a resolution change with Clear OFF starts clear instead of "
   "erroring — there is nothing honest to keep")

# ------------------------- 15. Selected to Active, natively (0.29.0)
# A red source quad floats above a blue target quad; the projection bakes
# the SOURCE's colour into the TARGET's map. The suite keeps the user's
# selection as the source list, exactly like the panel.
s2a_src_me = bpy.data.meshes.new("s2asrc")
s2a_src_me.from_pydata(
    [(-2, -2, 0.5), (2, -2, 0.5), (2, 2, 0.5), (-2, 2, 0.5)],
    [], [(0, 1, 2, 3)])
s2a_src = bpy.data.objects.new("S2ASource", s2a_src_me)
bpy.context.scene.collection.objects.link(s2a_src)
src_mat = bpy.data.materials.new("S2ASrcMat")
src_mat.use_nodes = True
sb = next(n for n in src_mat.node_tree.nodes if n.type == "BSDF_PRINCIPLED")
sb.inputs["Base Color"].default_value = (0.9, 0.05, 0.05, 1.0)
s2a_src_me.materials.append(src_mat)

s2a_tgt_me = bpy.data.meshes.new("s2atgt")
s2a_tgt_me.from_pydata(
    [(-1, -1, 0), (1, -1, 0), (1, 1, 0), (-1, 1, 0)], [], [(0, 1, 2, 3)])
tuv2 = s2a_tgt_me.uv_layers.new(name="UVMap")
for i, (u, v) in enumerate(((0.02, 0.02), (0.98, 0.02),
                            (0.98, 0.98), (0.02, 0.98))):
    tuv2.data[i].uv = (u, v)
s2a_tgt = bpy.data.objects.new("S2ATarget", s2a_tgt_me)
bpy.context.scene.collection.objects.link(s2a_tgt)
tgt_mat = bpy.data.materials.new("S2ATgtMat")
tgt_mat.use_nodes = True
tb = next(n for n in tgt_mat.node_tree.nodes if n.type == "BSDF_PRINCIPLED")
tb.inputs["Base Color"].default_value = (0.05, 0.05, 0.9, 1.0)
s2a_tgt_me.materials.append(tgt_mat)

for o in bpy.context.view_layer.objects:
    o.select_set(False)
s2a_src.select_set(True)                    # the SOURCE is the selection
# ⚠ extrusion 1.0 is LOAD-BEARING: the rays START on the target surface
# pushed OUT along its normal by the extrusion, then travel back INWARD —
# with extrusion 0 a source floating ABOVE the target is simply never on
# the ray's path and the bake comes back black (measured on this very
# fixture). This is Blender's own behaviour; the panel calls it Extrusion.
r_s2a = texbake.bake_texture("S2ATgtMat", "DIFFUSE", 64, 64,
                             out_path=png("s2a.png"),
                             pass_filter=["COLOR"],
                             object_name="S2ATarget",
                             use_selected_to_active=True,
                             cage_extrusion=1.0,
                             max_ray_distance=2.0)
_a_s, _rgb_s = _file_px(r_s2a["path"])
centre = _rgb_s[32, 32]
ok(float(centre[0]) > 0.5 and float(centre[2]) < 0.3,
   "s2a: ⚠ the projection really baked the SOURCE's red onto the target "
   "(centre %r) — the blue target colour would mean the option never "
   "reached the operator" % [round(float(v), 3) for v in centre])
s2a_opts = r_s2a["options"]["selected_to_active"]
ok(s2a_opts["on"] is True and s2a_opts["max_ray_distance"] == 2.0
   and s2a_opts["cage_extrusion"] == 1.0,
   "s2a: the family is echoed as used, extrusion included")

for o in bpy.context.view_layer.objects:
    o.select_set(False)
expect_error(lambda: texbake.bake_texture("S2ATgtMat", "DIFFUSE", 64, 64,
                                          out_path=png("s2a2.png"),
                                          object_name="S2ATarget",
                                          use_selected_to_active=True),
             "only the bake target",
             "s2a: ⚠ no sources selected is refused in words BEFORE the "
             "bake — the operator's own error names nothing")
expect_error(lambda: texbake.bake_texture("S2ATgtMat", "DIFFUSE", 64, 64,
                                          out_path=png("s2a3.png"),
                                          object_name="S2ATarget",
                                          use_cage=True,
                                          cage_object="NoSuchCage"),
             "not found", "s2a: a missing cage object is refused by name")
bpy.data.objects.remove(s2a_src, do_unlink=True)
bpy.data.objects.remove(s2a_tgt, do_unlink=True)

# --------------------- 16. Target: Active Color Attribute (0.29.0)
# The panel's second Output target: no image, no file — the map lands on
# the mesh's vertices.
# ⚠ Section 10's replace tests left BakeMat's output fed by the BAKED
# IMAGE NODE — a surface with no BSDF has no diffuse component, and this
# probe baked pure black through it before the rewire below (measured).
_out = node_of(mat, "OUTPUT_MATERIAL")
_bsdf = node_of(mat, "BSDF_PRINCIPLED")
mat.node_tree.links.new(_bsdf.outputs[0], _out.inputs["Surface"])
vc = cube.data.color_attributes.new(name="BakeCol", type="FLOAT_COLOR",
                                    domain="CORNER")
cube.data.color_attributes.active_color = vc
r_vc = texbake.bake_texture("BakeMat", "DIFFUSE", 64, 64,
                            pass_filter=["COLOR"],
                            target="VERTEX_COLORS")
ok(r_vc["path"] is None and r_vc["target"] == "VERTEX_COLORS"
   and r_vc["color_attribute"] == "BakeCol",
   "vcol: no file, and the reply names the attribute the map landed on — "
   "note it needed NO out_path even on an unsaved blend")
vc_vals = [cube.data.color_attributes["BakeCol"].data[i].color[0]
           for i in range(0, len(cube.data.loops), 5)]
ok(max(vc_vals) > 0.5,
   "vcol: the attribute really holds the baked colour (max R %.3f — the "
   "factory base colour is 0.8)" % max(vc_vals))
expect_error(lambda: texbake.bake_texture("BakeMat", "UV", 64, 64,
                                          target="VERTEX_COLORS"),
             "image target",
             "vcol: ⚠ UV to vertices is refused — the 5.2 UV workaround "
             "bakes an emission into an IMAGE")
cube.data.color_attributes.remove(vc)
expect_error(lambda: texbake.bake_texture("BakeMat", "DIFFUSE", 64, 64,
                                          target="VERTEX_COLORS"),
             "needs one",
             "vcol: no colour attribute on the mesh is refused in words")

bpy.data.objects.remove(pad_ob, do_unlink=True)

# ---------------- 17. Cycles preferences are NEVER touched (0.29.0)
# 0.27.0–0.28.4 managed compute devices itself (and once crashed Blender
# inside the CPU worker of a hybrid device it had switched on). Native
# means the scene's own settings bake — nothing in preferences may move.
addon = bpy.context.preferences.addons.get("cycles")
if addon is not None:
    cprefs = addon.preferences
    # ⚠ the list is EMPTY until Blender is asked to enumerate — snapshot
    # after get_devices() or "before" is {} and proves nothing.
    cprefs.get_devices()
    before = (cprefs.compute_device_type,
              {(d.type, d.name): d.use for d in cprefs.devices})
    texbake.bake_texture("BakeMat", "ROUGHNESS", 64, 64,
                         out_path=png("dev.png"))
    cprefs.get_devices()
    after = (cprefs.compute_device_type,
             {(d.type, d.name): d.use for d in cprefs.devices})
    ok(before == after,
       "native: ⚠ a bake leaves the Cycles device preferences EXACTLY "
       "alone — the 0.27.0 device manager (and the hybrid-device crash it "
       "chased) cannot come back if nothing touches them "
       "(before %r / after %r)" % (before, after))

# ------------- 18. All slots + the render-engine output rule (0.30.0)
# Marty, 2026-08-08: *"a tickbox that will automatically place and connect
# baked result to Active material output of EVERY material slot. (If two
# material outputs - always pick the one with render engine of whatever the
# initial material had as active material output"*.
second_mat = bpy.data.materials["SecondSlot"]
for _m in (mat, second_mat):
    for _n in list(_m.node_tree.nodes):
        if _n.type == "TEX_IMAGE" and _n.get(texbake.BAKED_MARK):
            _m.node_tree.nodes.remove(_n)

r_all = texbake.bake_texture("BakeMat", "ROUGHNESS", 64, 64,
                             out_path=png("allslots.png"))
row_all = {"object": "Cube", "material": "BakeMat", "path": r_all["path"],
           "bake_type": "ROUGHNESS"}

reply = texbake.apply_baked_material([dict(row_all)])
ok(reply.get("all_slots") is False,
   "allslots: ⚠ the reply ECHOES the grown parameter even when it is off — "
   "that echo is the only way an app can tell a pre-0.30.0 add-on ignored it")
ok(baked_node(second_mat) is None,
   "allslots: off, a slot that was not baked is left completely alone")

# two Material Outputs on the OTHER material, the inactive one on CYCLES
mat_out = node_of(mat, "OUTPUT_MATERIAL")
mat_out.is_active_output = True
mat_out.target = "CYCLES"
ok(texbake._output_target(mat) == "CYCLES",
   "allslots: (the initial material renders through a CYCLES output)")
eevee_out = node_of(second_mat, "OUTPUT_MATERIAL")
eevee_out.target = "EEVEE"
eevee_out.is_active_output = True
cycles_out = second_mat.node_tree.nodes.new("ShaderNodeOutputMaterial")
cycles_out.location = (600, -300)
cycles_out.target = "CYCLES"
cycles_out.is_active_output = False

reply = texbake.apply_baked_material([dict(row_all)], all_slots=True)
ok(reply.get("all_slots") is True and reply["count"] == 2,
   "allslots: on, EVERY slot of the baked object is wired (got %r)"
   % (reply["count"],))
other = baked_node(second_mat)
ok(other is not None,
   "allslots: the slot that was never baked now carries the map too")
ok(feeds(cycles_out.inputs["Surface"], other)
   and not feeds(eevee_out.inputs["Surface"], other),
   "allslots: ⚠ and it lands on the CYCLES output, matching the engine the "
   "INITIAL material rendered through — not this material's own active one")
ok(any(e.get("source_material") == "BakeMat"
       for e in reply["applied"] if e["material"] == "SecondSlot"),
   "allslots: the reply says whose map a borrowed slot got")
ok(all("source_material" not in e for e in reply["applied"]
       if e["material"] == "BakeMat"),
   "allslots: while the material that was actually baked carries no such "
   "note — it got its own map")

# ⚠ a slot with its OWN baked map keeps it
r_second = texbake.bake_texture("SecondSlot", "ROUGHNESS", 32, 32,
                                out_path=png("allslots_second.png"))
reply = texbake.apply_baked_material(
    [dict(row_all),
     {"object": "Cube", "material": "SecondSlot", "path": r_second["path"],
      "bake_type": "ROUGHNESS"}], all_slots=True)
own = baked_node(second_mat)
ok(os.path.normcase(os.path.abspath(bpy.path.abspath(own.image.filepath)))
   == os.path.normcase(os.path.abspath(r_second["path"])),
   "allslots: ⚠ a slot whose OWN material was baked keeps its OWN map — "
   "overwriting a correct map with a neighbour's would be a downgrade "
   "(got %r)" % (own.image.filepath,))
ok(reply["count"] == 2 and not any("source_material" in e
                                   for e in reply["applied"]),
   "allslots: with every slot baked, nothing is borrowed at all")

second_mat.node_tree.nodes.remove(cycles_out)

# ⚠ 0.31.0, Marty's follow-up: *"if we don't have that kind of material
# output in another slot we can just make one and wire it in"*. Before
# this, a slot with only an EEVEE output took the map on that output while
# the bake had come from a CYCLES one — the map landed somewhere that never
# renders, and nothing said so.
for _n in list(second_mat.node_tree.nodes):
    if _n.type == "TEX_IMAGE" and _n.get(texbake.BAKED_MARK):
        second_mat.node_tree.nodes.remove(_n)
eevee_out.target = "EEVEE"
eevee_out.is_active_output = True
outs_before = len([n for n in second_mat.node_tree.nodes
                   if n.type == "OUTPUT_MATERIAL"])
ok(outs_before == 1 and texbake._output_target(mat) == "CYCLES",
   "allslots: (the other slot has ONLY an EEVEE output, the baked one is "
   "CYCLES)")
reply = texbake.apply_baked_material([dict(row_all)], all_slots=True)
outs_after = [n for n in second_mat.node_tree.nodes
              if n.type == "OUTPUT_MATERIAL"]
made_one = [n for n in outs_after if n.target == "CYCLES"]
ok(len(outs_after) == outs_before + 1 and made_one,
   "allslots: ⚠ a slot with no CYCLES output GETS one made for it rather "
   "than the map going onto the EEVEE output that never renders it "
   "(outputs %d -> %d)" % (outs_before, len(outs_after)))
ok(feeds(made_one[0].inputs["Surface"], baked_node(second_mat)),
   "allslots: and the baked node is wired into the one that was made")
ok(made_one[0].is_active_output,
   "allslots: the new output is made ACTIVE for its engine")
entry = next(e for e in reply["applied"] if e["material"] == "SecondSlot")
ok(entry.get("output_created") is True
   and entry.get("output_target") == "CYCLES",
   "allslots: and the reply SAYS it made one, so a scene gaining a node is "
   "never silent (got %r)" % {k: entry.get(k) for k in
                              ("output_created", "output_target")})
first = next(e for e in reply["applied"] if e["material"] == "BakeMat")
ok(first.get("output_created") is False,
   "allslots: the material that was actually baked already had its output "
   "— nothing is created for it")

second_mat.node_tree.nodes.remove(made_one[0])
eevee_out.target = "ALL"
eevee_out.is_active_output = True
mat_out.target = "ALL"

print("%d passed, %d failed" % (len(PASS), len(FAIL)), flush=True)
sys.exit(1 if FAIL else 0)
