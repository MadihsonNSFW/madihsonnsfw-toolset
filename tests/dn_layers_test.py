# Denoising setup, per-VIEW-LAYER mode (split='LAYERS'): engine guard, tree
# build + tagging, socket wiring, image source choice, multi-layer combine,
# rebuild-in-place, and never clobbering a user's same-named tree.
# Run: blender.exe -b --factory-startup --python dn_layers_test.py
import importlib.util
import os
import sys

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


scene = bpy.context.scene

# --- engine guard ----------------------------------------------------------
scene.render.engine = 'BLENDER_EEVEE'
try:
    core.setup_denoise(split='LAYERS')
    ok(False, "EEVEE refused")
except RuntimeError as e:
    ok("Cycles" in str(e), "EEVEE refused with a Cycles-only message")

scene.render.engine = 'CYCLES'

# --- single layer, render denoise disabled --------------------------------
res = core.setup_denoise(split='LAYERS', disable_render_denoise=True)
ng = bpy.data.node_groups.get(res["tree"])
vl = scene.view_layers[0]

ok(ng is not None, "tree exists in bpy.data.node_groups")
ok(bool(ng.get("madi_denoise")), "tree carries the madi_denoise tag")
ok(scene.compositing_node_group == ng, "scene.compositing_node_group set")
ok(scene.use_nodes, "use_nodes on")
ok(res["split"] == "LAYERS", "result reports split=LAYERS")
ok(res["denoise_nodes"] == 1, "one Denoise node for one layer")
ok(vl.cycles.denoising_store_passes, "denoising_store_passes enabled")
ok(not vl.cycles.use_denoising, "render-time denoise switched OFF")

outs = [n for n in ng.nodes if n.bl_idname == 'NodeGroupOutput']
ok(len(outs) == 1, "exactly one NodeGroupOutput")
ok(any(getattr(s, "in_out", None) == 'OUTPUT'
       for s in ng.interface.items_tree), "interface has an OUTPUT socket")
ok(outs[0].inputs and outs[0].inputs[0].links,
   "something is linked into the group output")

dn = [n for n in ng.nodes if n.bl_idname == 'CompositorNodeDenoise']
ok(len(dn) == 1, "node count: 1 Denoise")
rl = [n for n in ng.nodes if n.bl_idname == 'CompositorNodeRLayers']
ok(len(rl) == 1 and rl[0].layer == vl.name, "Render Layers node on the layer")

img_link = dn[0].inputs["Image"].links
ok(img_link and img_link[0].from_socket.name == "Image",
   "render denoise OFF -> Denoise input is the beauty 'Image'")
ok(res["image_sources"].get(vl.name) == "Image",
   "image_sources reports 'Image'")
ok(dn[0].inputs["Normal"].links and dn[0].inputs["Albedo"].links,
   "Denoising Normal + Albedo guide the denoise")
hdr = dn[0].inputs.get("HDR")
ok(hdr is not None and bool(hdr.default_value), "HDR socket on")
ok(outs[0].inputs[0].links[0].from_node == dn[0],
   "Denoise output feeds the group output")

# --- render denoise left ON -> 'Noisy Image' is the input ------------------
core.clear_denoise()
vl.cycles.use_denoising = True
res2 = core.setup_denoise(split='LAYERS', disable_render_denoise=False)
ng2 = bpy.data.node_groups.get(res2["tree"])
ok(vl.cycles.use_denoising, "render denoise left ON when asked")
src = res2["image_sources"].get(vl.name)
ok(src == "Noisy Image",
   "render denoise ON -> input is 'Noisy Image' (got %s)" % src)
dn2 = [n for n in ng2.nodes if n.bl_idname == 'CompositorNodeDenoise'][0]
ok(dn2.inputs["Image"].links[0].from_socket.name == "Noisy Image",
   "and the link really comes from that socket")

# --- rebuild in place: run again, no duplicate trees -----------------------
n_before = len([g for g in bpy.data.node_groups if g.get("madi_denoise")])
core.setup_denoise(split='LAYERS')
n_after = len([g for g in bpy.data.node_groups if g.get("madi_denoise")])
ok(n_before == 1 and n_after == 1,
   "re-running setup rebuilds the SAME tree (no duplicates)")

# --- two view layers -> Alpha Over combine ---------------------------------
core.clear_denoise()
vl2 = scene.view_layers.new("Second")
res3 = core.setup_denoise(split='LAYERS')
ng3 = bpy.data.node_groups.get(res3["tree"])
dns = [n for n in ng3.nodes if n.bl_idname == 'CompositorNodeDenoise']
ok(len(dns) == 2, "two layers -> two Denoise nodes")
ok(sorted(res3["layers"]) == sorted([scene.view_layers[0].name, "Second"]),
   "both layers reported")
overs = [n for n in ng3.nodes if n.bl_idname == 'CompositorNodeAlphaOver']
ok(len(overs) == 1, "one Alpha Over combines them")
bg = overs[0].inputs.get("Background")
fg = overs[0].inputs.get("Foreground")
ok(bg is not None and fg is not None and bg.links and fg.links,
   "Alpha Over Background+Foreground both wired (5.x names)")
out3 = [n for n in ng3.nodes if n.bl_idname == 'NodeGroupOutput'][0]
ok(out3.inputs[0].links[0].from_node == overs[0],
   "Alpha Over feeds the group output")
ok(res3["combined"] == "ALPHA_OVER", "result reports the combine mode")

# --- combine='NONE' leaves the output unwired ------------------------------
core.clear_denoise()
res4 = core.setup_denoise(split='LAYERS', combine='NONE')
ng4 = bpy.data.node_groups.get(res4["tree"])
out4 = [n for n in ng4.nodes if n.bl_idname == 'NodeGroupOutput'][0]
ok(not out4.inputs[0].links,
   "combine=NONE: per-layer results left for manual wiring")

# --- bad layer selection ---------------------------------------------------
try:
    core.setup_denoise(view_layers=["nope"], split='LAYERS')
    ok(False, "unknown view layer refused")
except RuntimeError as e:
    ok("No enabled view layers" in str(e), "unknown view layer refused clearly")

# --- a USER tree with our name is never clobbered --------------------------
core.clear_denoise()
user = bpy.data.node_groups.new("MADI Denoise", 'CompositorNodeTree')
marker = user.nodes.new('NodeGroupOutput')
res5 = core.setup_denoise(split='LAYERS')
ok(res5["tree"] != "MADI Denoise" and res5["tree"].endswith("(MADI)"),
   "ours got renamed instead of clobbering the user's tree")
ok("MADI Denoise" in bpy.data.node_groups
   and len(bpy.data.node_groups["MADI Denoise"].nodes) == 1,
   "user's same-named tree untouched")
core.clear_denoise()
ok("MADI Denoise" in bpy.data.node_groups,
   "undo removed OUR tree, not the user's")

print("\n%d passed, %d failed" % (len(PASS), len(FAIL)), flush=True)
sys.exit(1 if FAIL else 0)
