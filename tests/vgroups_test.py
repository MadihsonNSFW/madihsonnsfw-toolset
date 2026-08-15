# Vertex groups as a library item (Marty, 2026-08-04): store them, restore them
# exactly, and transfer them onto a different mesh.
#
#   blender.exe -b --factory-startup --python tests\vgroups_test.py
#
# ⚠ The two apply modes are the whole point of this suite. EXACT is a lossless
# index-based restore; TRANSFER is a spatial estimate. A test that treated them
# as interchangeable would be missing the only thing that matters here.
import importlib.util
import os
import shutil
import sys
import tempfile

import bpy

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

ROOT = _ROOT
ADDON = os.path.join(ROOT, "blender_addon", "madi_anim_library")
PASS, FAIL = [], []


def ok(cond, label):
    (PASS if cond else FAIL).append(label)
    print(("ok   " if cond else "FAIL ") + label, flush=True)


spec = importlib.util.spec_from_file_location(
    "madi_pkg", os.path.join(ADDON, "__init__.py"),
    submodule_search_locations=[ADDON])
pkg = importlib.util.module_from_spec(spec)
sys.modules["madi_pkg"] = pkg
spec.loader.exec_module(pkg)
core = sys.modules["madi_pkg.core"]

TMP = tempfile.mkdtemp(prefix="madi_vg_test_")

bpy.ops.wm.read_homefile(use_empty=True)
bpy.ops.mesh.primitive_cube_add()
cube = bpy.context.active_object
cube.name = "Body"
vg = cube.vertex_groups.new(name="Left")
vg.add([0, 1, 2], 0.75, 'REPLACE')
vg2 = cube.vertex_groups.new(name="Right")
vg2.add([4, 5], 0.25, 'REPLACE')

listing = core.list_vertex_groups(["Body"])
ok(listing[0]["groups"] == ["Left", "Right"],
   "list: both groups are reported (%s)" % listing[0]["groups"])

result = core.save_vgroups(TMP, "", "body_weights", objects=["Body"])
ok(result["groups"] == 2 and os.path.isdir(result["path"]),
   "save: an item is written with both groups")
ok(result["path"].endswith(".vgroups"),
   "save: with its own extension (%s)" % os.path.basename(result["path"]))
item = result["path"]

# ⚠ Only vertices actually IN a group are stored - an unassigned vertex is
# absent, not written as a zero. On a 100k mesh with three small groups that is
# the difference between a small file and a huge one.
import json  # noqa: E402
with open(os.path.join(item, "vgroups.json"), encoding="utf-8") as fh:
    payload = json.load(fh)
left = next(g for g in payload["meshes"][0]["groups"] if g["name"] == "Left")
ok(left["indices"] == [0, 1, 2] and len(left["weights"]) == 3,
   "save: only assigned vertices are stored (%s)" % left["indices"])

# ---------------------------------------------------------------- EXACT
cube.vertex_groups.clear()
ok(len(cube.vertex_groups) == 0, "restore: the groups are gone from the mesh")
r = core.apply_vgroups(item, mode="EXACT")
ok(r["applied"] == 2, "restore: both groups come back")
names = sorted(vg.name for vg in cube.vertex_groups)
ok(names == ["Left", "Right"], "restore: by name (%s)" % names)
back = cube.vertex_groups["Left"]
weights = sorted(round(g.weight, 3) for v in cube.data.vertices
                 for g in v.groups if g.group == back.index)
ok(weights == [0.75, 0.75, 0.75],
   "restore: with the EXACT weights - this is lossless, not an estimate (%s)"
   % weights)

# ⚠ A vertex-count mismatch REFUSES rather than doing its best. Weights that
# look plausible and are wrong are far worse than being told no.
bpy.ops.mesh.primitive_uv_sphere_add()
sphere = bpy.context.active_object
# ⚠ Free the name FIRST. Assigning a name Blender already has hands back
# "Body.001" instead — the sphere then never answers to "Body", the lookup
# finds nothing, and the test proves something entirely different from what it
# claims to.
cube.name = "Body_original"
sphere.name = "Body"          # same name, different topology
refused = False
try:
    core.apply_vgroups(item, mode="EXACT")
except RuntimeError as exc:
    refused = "vertex count" in str(exc).lower()
ok(refused,
   "restore: a mesh with a different vertex count is REFUSED, and told to use "
   "Transfer instead")
sphere.name = "Target"
cube.name = "Body"

# ---------------------------------------------------------------- TRANSFER
bpy.context.view_layer.objects.active = sphere
sphere.select_set(True)
r = core.apply_vgroups(item, mode="TRANSFER")
ok(r.get("approximate") is True,
   "transfer: the reply says plainly that it is approximate")
ok(r.get("transferred_from") == "Body",
   "transfer: and names the mesh it sampled from")
ok(sorted(v.name for v in sphere.vertex_groups) == ["Left", "Right"],
   "transfer: the groups land on the DIFFERENT mesh (%s)"
   % sorted(v.name for v in sphere.vertex_groups))

# ⚠ The honest limitation: this item stores weights per vertex, not geometry,
# so a transfer needs the source mesh present. It must SAY so, not guess.
bpy.data.objects.remove(cube, do_unlink=True)
bpy.context.view_layer.objects.active = sphere
explained = False
try:
    core.apply_vgroups(item, mode="TRANSFER")
except RuntimeError as exc:
    explained = "in the scene" in str(exc)
ok(explained,
   "transfer: with the source mesh gone it explains why it cannot, instead of "
   "silently doing nothing")

shutil.rmtree(TMP, ignore_errors=True)
print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
for label in FAIL:
    print("  FAILED: " + label)
