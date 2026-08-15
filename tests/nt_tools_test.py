# Node Setup tools (core.py "node tools" section): relink swap / multi-input /
# copy_inputs / donor auto-detect / error paths, sequence frame math, output
# path building (_active stripped, padding dropped, '//' style mirrored),
# gaps, toggles, and node_tools_status previews. Background mode has no
# windows, so core._open_node_trees is monkeypatched like the original suite.
# Run: blender.exe -b --factory-startup --python nt_tools_test.py
import importlib.util
import os
import sys
import tempfile

import bpy

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CORE = os.path.join(_ROOT, "blender_addon", "madi_anim_library", "core.py")
spec = importlib.util.spec_from_file_location("madi_core", CORE)
core = importlib.util.module_from_spec(spec)
spec.loader.exec_module(core)

PASS = []
FAIL = []


def ok(cond, label):
    (PASS if cond else FAIL).append(label)
    print(("ok   " if cond else "FAIL ") + label, flush=True)


def deselect(tree):
    for n in tree.nodes:
        n.select = False


# ============================================================== relink =====
shader = bpy.data.node_groups.new("S", 'ShaderNodeTree')
rgb1 = shader.nodes.new('ShaderNodeRGB')
rgb2 = shader.nodes.new('ShaderNodeRGB')
emit = shader.nodes.new('ShaderNodeEmission')
shader.links.new(rgb1.outputs["Color"], emit.inputs["Color"])

core._open_node_trees = lambda: [shader]

# --- single-input swap ------------------------------------------------------
deselect(shader)
rgb1.select = rgb2.select = True
res = core.relink_nodes()
link = emit.inputs["Color"].links[0]
ok(link.from_node == rgb2, "single input: link swapped onto the free node")
ok(res["made"] == 1 and res["source"] == rgb1.name, "result reports the move")
ok(res["tree"] == "S" and res["tree_type"] == 'ShaderNodeTree',
   "tree identity reported")
ok(not any(l.to_node == emit for s in rgb1.outputs for l in s.links),
   "the old source is unplugged")

# --- donor auto-detect: only the free node selected ------------------------
shader.links.new(rgb1.outputs["Color"], emit.inputs["Color"])  # rewire rgb1
deselect(shader)
rgb2.select = True
src, targets, err = core.resolve_relink_pair(shader)
ok(err is None and src == rgb1 and targets == [rgb2],
   "lone wired same-type node found as donor")

# --- error paths ------------------------------------------------------------
deselect(shader)
_, _, err = core.resolve_relink_pair(shader)
ok(err == "Select the connected node and the unconnected one",
   "no selection -> guidance")

rgb3 = shader.nodes.new('ShaderNodeRGB')
emit2 = shader.nodes.new('ShaderNodeEmission')
shader.links.new(rgb3.outputs["Color"], emit2.inputs["Color"])
deselect(shader)
rgb1.select = rgb3.select = True
_, _, err = core.resolve_relink_pair(shader)
ok(err == "More than one selected node has outgoing links",
   "two wired selected -> refuse")

deselect(shader)
rgb2.select = True   # free; rgb1 AND rgb3 both wired candidates
_, _, err = core.resolve_relink_pair(shader)
ok(err is not None and "select the source too" in err,
   "several possible donors -> refuse")

val = shader.nodes.new('ShaderNodeValue')
deselect(shader)
rgb2.select = val.select = True   # both free, different types
_, _, err = core.resolve_relink_pair(shader)
ok(err is not None and "different types" in err,
   "mixed-type free selection -> refuse")

try:
    deselect(shader)
    core.relink_nodes()
    ok(False, "relink with no selection raises")
except RuntimeError:
    ok(True, "relink with no selection raises")

# --- multi-input keeps both -------------------------------------------------
geo = bpy.data.node_groups.new("G", 'GeometryNodeTree')
cube1 = geo.nodes.new('GeometryNodeMeshCube')
cube2 = geo.nodes.new('GeometryNodeMeshCube')
join = geo.nodes.new('GeometryNodeJoinGeometry')
geo.links.new(cube1.outputs["Mesh"], join.inputs["Geometry"])
core._open_node_trees = lambda: [geo]
deselect(geo)
cube1.select = cube2.select = True
core.relink_nodes()
feeders = {l.from_node.name for l in join.inputs["Geometry"].links}
ok(feeders == {cube1.name, cube2.name},
   "multi-input target keeps BOTH sources (got %s)" % sorted(feeders))

# --- copy_inputs ------------------------------------------------------------
mixt = bpy.data.node_groups.new("M", 'ShaderNodeTree')
src_rgb = mixt.nodes.new('ShaderNodeRGB')
mix1 = mixt.nodes.new('ShaderNodeMix')
mix2 = mixt.nodes.new('ShaderNodeMix')
mix1.data_type = mix2.data_type = 'RGBA'
sink = mixt.nodes.new('ShaderNodeEmission')
a1 = [s for s in mix1.inputs if s.type == 'RGBA'][0]
mixt.links.new(src_rgb.outputs["Color"], a1)
out1 = [s for s in mix1.outputs if s.type == 'RGBA'][0]
mixt.links.new(out1, sink.inputs["Color"])
core._open_node_trees = lambda: [mixt]
deselect(mixt)
mix1.select = mix2.select = True
res = core.relink_nodes(copy_inputs=True)
a2 = [s for s in mix2.inputs if s.type == 'RGBA'][0]
ok(sink.inputs["Color"].links[0].from_node == mix2,
   "copy_inputs: outgoing link still moved")
ok(a2.links and a2.links[0].from_node == src_rgb,
   "copy_inputs: input link copied onto the matching RGBA socket")
ok(res["made"] == 2, "both moves counted")

# --- several editors --------------------------------------------------------
core._open_node_trees = lambda: [shader, geo]
deselect(shader)
deselect(geo)
try:
    core._relink_tree()
    ok(False, "two editors, no selection -> refuse")
except RuntimeError as e:
    ok("Several Node Editors" in str(e), "two editors, no selection -> refuse")
rgb1.select = True
ok(core._relink_tree() == shader,
   "two editors, one with a selection -> that one wins")

core._open_node_trees = lambda: []
try:
    core._relink_tree()
    ok(False, "no editor -> refuse")
except RuntimeError as e:
    ok("Open a Node Editor" in str(e), "no editor -> clear guidance")

# ======================================================== sequence setup ===
# Project on disk: <tmp>\proj\file.blend + Render\<shot>\exr\<frames>
tmp = tempfile.mkdtemp(prefix="madi_nt_")
proj = os.path.join(tmp, "proj")
shot = "sq01_sc02.030_active"
seq_dir = os.path.join(proj, "Render", shot, "exr")
os.makedirs(seq_dir)
for n in range(5, 11):                       # myshot_0005..0010 = 6 frames
    open(os.path.join(seq_dir, "myshot_%04d.png" % n), "wb").close()
open(os.path.join(seq_dir, "other_v2.txt"), "wb").close()   # stray file
bpy.ops.wm.save_as_mainfile(filepath=os.path.join(proj, "file.blend"))

scene = bpy.context.scene
ng = bpy.data.node_groups.new("Comp", 'CompositorNodeTree')
scene.compositing_node_group = ng
img_node = ng.nodes.new('CompositorNodeImage')
img = bpy.data.images.new("seq", 4, 4)
img.filepath = os.path.join(seq_dir, "myshot_0005.png")
img_node.image = img
for n in ng.nodes:
    n.select = False
img_node.select = True
ng.nodes.active = img_node

core._open_node_trees = lambda: []   # no editor -> falls back to scene group
ok(core._sequence_tree() == ng, "sequence tree falls back to the scene group")

# --- scan -------------------------------------------------------------------
info, err = core.scan_image_sequence(img.filepath)
ok(err is None, "scan finds the sequence")
ok(info["count"] == 6 and info["first"] == 5 and info["last"] == 10,
   "count/first/last right (stray file ignored)")
ok(info["gaps"] is False, "no gaps reported")

# --- the full setup ---------------------------------------------------------
scene.render.filepath = "//out\\"     # relative style must be mirrored
res = core.setup_image_sequence()
ok(img.source == 'SEQUENCE', "image switched to SEQUENCE")
ok(img_node.frame_start == 1, "node frame_start = 1 (start_at_one)")
ok(img_node.frame_duration == 6, "node frame_duration = frame count")
ok(img_node.frame_offset == 4,
   "frame_offset = first-1 (file number = cfra - start + 1 + offset)")
ok(img_node.use_auto_refresh, "auto refresh on")
ok(scene.frame_start == 1 and scene.frame_end == 6, "scene range set 1..6")
expect_tail = os.path.join("Render", shot, "exr_composited",
                           "sq01_sc02.30_exr_composited_")
ok(res["output"] is not None and res["output"].startswith("//"),
   "'//' relative output style mirrored")
ok(res["output"] is not None
   and os.path.normpath(res["output"].lstrip("/")).endswith(
       os.path.normpath(expect_tail)),
   "output path: shot folder kept, filename drops _active AND the padding")
ok(scene.render.filepath == res["output"], "scene output actually set")
ok(res["notes"] == [], "no notes on a clean sequence")

# --- absolute style ---------------------------------------------------------
scene.render.filepath = r"C:\renders\x"
res = core.setup_image_sequence()
ok(res["output"] is not None and not res["output"].startswith("//"),
   "absolute scene path -> absolute output")

# --- start_at_one=False + set_scene_range=False -----------------------------
scene.frame_start = 11
res = core.setup_image_sequence(start_at_one=False)
ok(img_node.frame_start == 11 and scene.frame_end == 16,
   "start at the scene's frame_start instead")
scene.frame_start, scene.frame_end = 2, 99
core.setup_image_sequence(set_scene_range=False)
ok(scene.frame_start == 2 and scene.frame_end == 99,
   "set_scene_range=False leaves the range alone")

# --- gaps -------------------------------------------------------------------
gap_dir = os.path.join(proj, "Render", shot, "exr_gap")
os.makedirs(gap_dir)
for n in (1, 2, 4):
    open(os.path.join(gap_dir, "g_%04d.png" % n), "wb").close()
img.filepath = os.path.join(gap_dir, "g_0001.png")
res = core.setup_image_sequence()
ok(any("gaps" in n for n in res["notes"]), "gap sequence gets a note")
ok(res["count"] == 3, "gap sequence still counts real files")

# --- error paths ------------------------------------------------------------
# NB "other_v2.txt" would be a legal 1-frame sequence (trailing digits =
# frame number, by design) — the refusal needs a digit-free name.
open(os.path.join(seq_dir, "plain.txt"), "wb").close()
img.filepath = os.path.join(seq_dir, "plain.txt")
try:
    core.setup_image_sequence()
    ok(False, "digit-free filename refused")
except RuntimeError as e:
    ok("No frame number" in str(e), "digit-free filename refused clearly")
img.filepath = os.path.join(tmp, "nowhere", "x_0001.png")
try:
    core.setup_image_sequence()
    ok(False, "missing folder refused")
except RuntimeError as e:
    ok("Folder not found" in str(e), "missing folder refused clearly")

img_node.select = False
img.filepath = os.path.join(seq_dir, "myshot_0005.png")
try:
    core.setup_image_sequence()
    ok(False, "no selected Image node refused")
except RuntimeError as e:
    ok("Select one Image node" in str(e), "no selected Image node -> guidance")

# ============================================================== status =====
img_node.select = True
ng.nodes.active = img_node
core._open_node_trees = lambda: [ng]
st = core.node_tools_status()
ok(st["editors"] == [{"name": "Comp", "type": 'CompositorNodeTree'}],
   "status lists the open editor")
ok(st["sequence"]["error"] is None
   and st["sequence"]["file"] == "myshot_0005.png",
   "sequence preview shows the file")
ok(st["sequence"]["output_preview"] is not None
   and "sq01_sc02.30" in st["sequence"]["output_preview"],
   "output preview is pure string math and already correct")
deselect(ng)
st = core.node_tools_status()
ok(st["relink"]["error"] is not None, "relink preview reports why not ready")

print("\n%d passed, %d failed" % (len(PASS), len(FAIL)), flush=True)
sys.exit(1 if FAIL else 0)
