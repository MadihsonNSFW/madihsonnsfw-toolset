# Denoising setup, per-LIGHT-PASS mode (split='PASSES', the default): 8
# Denoise nodes per layer (Direct+Indirect x Diffuse/Glossy/Transmission/
# Volume), colour passes NEVER denoised, beauty rebuilt (D+I)*Colour + flat
# passes, alpha restored, and a graph walk from the group output proving
# every pass actually reaches it.
# Run: blender.exe -b --factory-startup --python dn_passes_test.py
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
scene.render.engine = 'CYCLES'
vl = scene.view_layers[0]

res = core.setup_denoise(split='PASSES')
ng = bpy.data.node_groups.get(res["tree"])
ok(ng is not None and bool(ng.get("madi_denoise")), "tagged tree built")
ok(res["split"] == "PASSES", "split=PASSES reported")
ok(res["image_sources"].get(vl.name) == "light passes",
   "image_sources says 'light passes'")

# --- passes enabled on the layer ------------------------------------------
for prop in core._PASS_PROPS:
    if hasattr(vl, prop):
        ok(getattr(vl, prop), "pass enabled: %s" % prop)
for prop in core._CYCLES_PASS_PROPS:
    if hasattr(vl.cycles, prop):
        ok(getattr(vl.cycles, prop), "cycles pass enabled: %s" % prop)
ok(vl.cycles.denoising_store_passes, "denoising_store_passes on")

# --- 8 denoise nodes, one per light pass ----------------------------------
DENOISED = ["Diffuse Direct", "Diffuse Indirect", "Glossy Direct",
            "Glossy Indirect", "Transmission Direct", "Transmission Indirect",
            "Volume Direct", "Volume Indirect"]
COLOURS = ["Diffuse Color", "Glossy Color", "Transmission Color"]
FLAT = ["Emission", "Environment"]

dns = [n for n in ng.nodes if n.bl_idname == 'CompositorNodeDenoise']
ok(len(dns) == 8, "8 Denoise nodes (got %d)" % len(dns))
ok(res["denoise_nodes"] == 8, "result reports 8")

rl = [n for n in ng.nodes if n.bl_idname == 'CompositorNodeRLayers'][0]
fed = {}
for dn in dns:
    links = dn.inputs["Image"].links
    if links:
        fed[links[0].from_socket.name] = dn
    ok(dn.inputs["Normal"].links and dn.inputs["Albedo"].links,
       "denoise guided by Normal+Albedo (%s)" % dn.label)
for name in DENOISED:
    ok(name in fed, "denoised: %s" % name)

# --- colour / flat passes never pass through a Denoise node ----------------
for name in COLOURS + FLAT:
    sock = rl.outputs.get(name)
    ok(sock is not None and sock.links, "%s wired somewhere" % name)
    if sock is not None:
        ok(all(l.to_node.bl_idname != 'CompositorNodeDenoise'
               for l in sock.links),
           "%s NEVER goes into a Denoise node" % name)

# --- rebuild shape: (Direct+Indirect) x Colour per component ---------------
def mix_nodes(blend):
    return [n for n in ng.nodes if n.bl_idname == 'ShaderNodeMix'
            and n.blend_type == blend]


adds = mix_nodes('ADD')
muls = mix_nodes('MULTIPLY')
ok(len(muls) == 3, "3 MULTIPLY mixes (components with a colour pass), got %d"
   % len(muls))
for mul in muls:
    a, b, _ = core._mix_io(mul)
    ok(b.links and b.links[0].from_socket.name in COLOURS,
       "MULTIPLY B input is a colour pass (%s)" % mul.label)
    ok(a.links and a.links[0].from_node.bl_idname == 'ShaderNodeMix',
       "MULTIPLY A input is the D+I light sum (%s)" % mul.label)
comp_adds = [n for n in adds if n.label.endswith(" light")]
ok(len(comp_adds) == 4, "4 D+I ADD mixes (one per component), got %d"
   % len(comp_adds))
for add in comp_adds:
    a, b, _ = core._mix_io(add)
    ok(a.links and b.links
       and a.links[0].from_node.bl_idname == 'CompositorNodeDenoise'
       and b.links[0].from_node.bl_idname == 'CompositorNodeDenoise',
       "%s sums two Denoise outputs" % add.label)

# --- alpha restored at the end --------------------------------------------
sas = [n for n in ng.nodes if n.bl_idname == 'CompositorNodeSetAlpha']
ok(len(sas) == 1, "one Set Alpha node")
if sas:
    a_in = sas[0].inputs.get("Alpha") or sas[0].inputs[1]
    ok(a_in.links and a_in.links[0].from_socket.name == "Alpha",
       "Set Alpha takes the layer's real Alpha")

# --- graph walk: from the group output, every pass must be reachable -------
out = [n for n in ng.nodes if n.bl_idname == 'NodeGroupOutput'][0]
ok(out.inputs[0].links, "group output wired")

reached = set()
seen = set()
stack = [out]
while stack:
    node = stack.pop()
    if node.name in seen:
        continue
    seen.add(node.name)
    for sock in node.inputs:
        for link in sock.links:
            if link.from_node.bl_idname == 'CompositorNodeRLayers':
                reached.add(link.from_socket.name)
            stack.append(link.from_node)

for name in DENOISED + COLOURS + FLAT + ["Alpha"]:
    ok(name in reached, "reaches the output: %s" % name)
ok("Denoising Normal" in reached and "Denoising Albedo" in reached,
   "guide passes flow in too")

# --- pass_info bookkeeping -------------------------------------------------
info = res["passes"][vl.name]
ok(sorted(info["denoised_passes"]) == sorted(DENOISED),
   "pass_info lists the 8 denoised passes")
ok(sorted(info["flat_passes"]) == sorted(FLAT),
   "pass_info lists Emission+Environment as flat")
ok(info["skipped_components"] == [], "nothing skipped")

# --- two layers: whole tree per layer + Alpha Over -------------------------
core.clear_denoise()
scene.view_layers.new("Second")
res2 = core.setup_denoise(split='PASSES')
ng2 = bpy.data.node_groups.get(res2["tree"])
dns2 = [n for n in ng2.nodes if n.bl_idname == 'CompositorNodeDenoise']
ok(len(dns2) == 16, "two layers -> 16 Denoise nodes (got %d)" % len(dns2))
rls2 = [n for n in ng2.nodes if n.bl_idname == 'CompositorNodeRLayers']
ok(len(rls2) == 2, "two Render Layers nodes")
overs = [n for n in ng2.nodes if n.bl_idname == 'CompositorNodeAlphaOver']
ok(len(overs) == 1, "Alpha Over combines the two layers")
out2 = [n for n in ng2.nodes if n.bl_idname == 'NodeGroupOutput'][0]
ok(out2.inputs[0].links[0].from_node == overs[0],
   "combined result feeds the output")
sas2 = [n for n in ng2.nodes if n.bl_idname == 'CompositorNodeSetAlpha']
ok(len(sas2) == 2, "each layer restores its own alpha")
core.clear_denoise()

print("\n%d passed, %d failed" % (len(PASS), len(FAIL)), flush=True)
sys.exit(1 if FAIL else 0)
