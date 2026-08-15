# Denoising undo (clear_denoise): snapshot written ONCE, exact restore of a
# deliberately non-standard pre-state, foreign trees survive, keep-passes
# mode, and the "nothing to undo" guard.
# Run: blender.exe -b --factory-startup --python dn_undo_test.py
import importlib.util
import json
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

# --- a deliberately NON-STANDARD pre-state ---------------------------------
vl.use_pass_glossy_direct = True          # some passes on…
vl.use_pass_emit = True
vl.use_pass_diffuse_direct = False        # …others off
vl.cycles.denoising_store_passes = False
vl.cycles.use_denoising = True
user_ng = bpy.data.node_groups.new("My Comp", 'CompositorNodeTree')
scene.compositing_node_group = user_ng
scene.use_nodes = False
# 5.x may force use_nodes back on once a compositor group is assigned —
# the restore contract is "put back what WAS there", so record what stuck.
pre_use_nodes = bool(scene.use_nodes)

before = {p: getattr(vl, p) for p in core._PASS_PROPS if hasattr(vl, p)}
before_cy = {p: getattr(vl.cycles, p) for p in core._CYCLES_PASS_PROPS
             if hasattr(vl.cycles, p)}

# --- setup changes things --------------------------------------------------
core.setup_denoise(split='PASSES')
ok(vl.use_pass_diffuse_direct, "setup really flipped a pass ON")
ok(vl.cycles.denoising_store_passes, "setup enabled store passes")
ok(not vl.cycles.use_denoising, "setup disabled render denoise")
ok(scene.use_nodes, "setup enabled use_nodes")
ok(scene.compositing_node_group != user_ng, "setup swapped in OUR tree")
ok(bool(scene.get("madi_denoise_backup")), "snapshot id-prop written")

snap1 = json.loads(scene["madi_denoise_backup"])
ok(snap1["prev_group"] == "My Comp", "snapshot remembers the user's group")
ok(snap1["use_nodes"] == pre_use_nodes,
   "snapshot remembers the pre-setup use_nodes (%s)" % pre_use_nodes)
ok(snap1["layers"][vl.name]["use_denoising"] is True,
   "snapshot holds the ORIGINAL use_denoising")
ok(snap1["layers"][vl.name]["passes"]["use_pass_diffuse_direct"] is False,
   "snapshot holds the ORIGINAL pass state")

# --- snapshot is written ONCE: a second setup must not overwrite it --------
core.setup_denoise(split='LAYERS')
snap2 = json.loads(scene["madi_denoise_backup"])
ok(snap2 == snap1,
   "re-running setup (even another mode) does NOT overwrite the snapshot")

# --- undo: exact restore ---------------------------------------------------
res = core.clear_denoise()
ok(res["removed_tree"] is not None, "undo removed our tree")
ok(res["passes_restored"], "undo reports passes restored")
ok(not any(g.get("madi_denoise") for g in bpy.data.node_groups),
   "no tagged tree left")
ok(scene.compositing_node_group == user_ng,
   "user's compositor group re-selected")
ok(bool(scene.use_nodes) == pre_use_nodes,
   "use_nodes back to its pre-setup value")
after = {p: getattr(vl, p) for p in core._PASS_PROPS if hasattr(vl, p)}
after_cy = {p: getattr(vl.cycles, p) for p in core._CYCLES_PASS_PROPS
            if hasattr(vl.cycles, p)}
ok(after == before, "every pass toggle exactly restored")
ok(after_cy == before_cy, "cycles volume passes exactly restored")
ok(vl.cycles.use_denoising is True, "use_denoising restored")
ok(vl.cycles.denoising_store_passes is False, "store passes restored")
ok(scene.get("madi_denoise_backup") is None, "snapshot dropped")

# --- nothing to undo -> clear error ----------------------------------------
try:
    core.clear_denoise()
    ok(False, "second undo refused")
except RuntimeError as e:
    ok("Nothing to undo" in str(e), "second undo says 'Nothing to undo'")

# --- a foreign tree named like ours survives the undo ----------------------
foreign = bpy.data.node_groups.new("MADI Denoise", 'CompositorNodeTree')
foreign.nodes.new('NodeGroupOutput')
core.setup_denoise(split='LAYERS')
core.clear_denoise()
ok("MADI Denoise" in bpy.data.node_groups
   and len(bpy.data.node_groups["MADI Denoise"].nodes) == 1,
   "untagged same-name tree survives setup+undo")
bpy.data.node_groups.remove(foreign)

# --- restore_passes=False: tree goes, toggles stay -------------------------
core.setup_denoise(split='LAYERS')
res2 = core.clear_denoise(restore_passes=False)
ok(res2["passes_restored"] is False, "keep-passes mode reported")
ok(not any(g.get("madi_denoise") for g in bpy.data.node_groups),
   "tree removed in keep-passes mode too")
ok(vl.cycles.denoising_store_passes is True,
   "store passes LEFT ON (that's the point of keep-passes)")
ok(scene.get("madi_denoise_backup") is None,
   "snapshot still dropped (undo is spent)")

print("\n%d passed, %d failed" % (len(PASS), len(FAIL)), flush=True)
sys.exit(1 if FAIL else 0)
