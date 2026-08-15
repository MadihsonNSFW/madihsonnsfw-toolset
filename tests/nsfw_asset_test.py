# NSFW Tools: building the Affector Torus rig from the embedded spec.
#
#   blender.exe -b --factory-startup --python tests\nsfw_asset_test.py
#
# The point of these checks is that the rig BUILDS AND DEFORMS, not that it has
# the right number of nodes. A node tree can differ in ways that look harmless -
# one wrong enum, one dropped socket default - and still deform completely
# differently, so the load-bearing checks here evaluate geometry and compare it.
#
# Runs against a factory-startup scene: no dependency on Marty's .blend, and it
# writes nothing.
import importlib.util
import os
import sys

import bmesh
import bpy

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

ROOT = _ROOT
sys.path.insert(0, os.path.join(ROOT, "app"))

spec = importlib.util.spec_from_file_location(
    "madi_assets", os.path.join(ROOT, "blender_addon", "madi_anim_library", "assets.py"))
assets = importlib.util.module_from_spec(spec)
spec.loader.exec_module(assets)

import nsfw_spec  # noqa: E402

PASS = []
FAIL = []


def ok(cond, label):
    (PASS if cond else FAIL).append(label)
    print(("ok   " if cond else "FAIL ") + label, flush=True)


def evaluated(ob):
    dg = bpy.context.evaluated_depsgraph_get()
    dg.update()
    me = ob.evaluated_get(dg).to_mesh()
    return [v.co.copy() for v in me.vertices]


def make_collider(name, radius, length, collection):
    """A capsule to push into the tube, built HERE rather than by the add-on.

    The add-on used to ship an "add a test affector" helper; the app dropped it,
    so this suite builds its own probe. That is the right way round anyway - the
    thing under test is the rig, and the probe has no business being product
    code.
    """
    me = bpy.data.meshes.new(name)
    bm = bmesh.new()
    bmesh.ops.create_uvsphere(bm, u_segments=24, v_segments=12, radius=radius)
    half = max(0.0, (length - 2.0 * radius) / 2.0)
    if half:
        for v in bm.verts:
            v.co.z += half if v.co.z > 1e-9 else (-half if v.co.z < -1e-9 else 0.0)
    bm.to_mesh(me)
    bm.free()
    me.update()
    coll = bpy.data.collections.new(collection)
    bpy.context.scene.collection.children.link(coll)
    ob = bpy.data.objects.new(name, me)
    coll.objects.link(ob)
    return ob, coll


# ------------------------------------------------------------------ the spec

asset = nsfw_spec.asset()
ok(asset["asset_id"] == "madi_affector_torus", "spec: unpacks to the expected asset")
ok(asset["group"]["counts"]["nodes"] == 97 and asset["group"]["counts"]["links"] == 142,
   "spec: carries the captured 97-node / 142-link group")
ok(len(asset["object"]["mesh"]["verts"]) == 9216 * 3, "spec: carries the 9216-vert mesh")
ok(isinstance(nsfw_spec.packed(), str) and len(nsfw_spec.packed()) > 1000,
   "spec: packed() is the wire form, not the plaintext")
ok("Thickness" in asset["defaults"] and "Affectors" not in asset["defaults"],
   "spec: ships numeric defaults but NOT an Affectors collection (that is the user's)")

# ----------------------------------------------------------------- building

built = assets.build_asset(nsfw_spec.packed())
ok(not built["problems"],
   "build: every property, socket and link applied (%d problems)" % len(built["problems"]))
ok(built["nodes"] == 97 and built["links"] == 142, "build: the group is complete")
ok(built["verts"] == 9216, "build: the mesh is complete")

ob = bpy.data.objects.get(built["object"])
ok(ob is not None, "build: the object is in the scene")

# It must arrive as an ORDINARY rig, exactly as it was in the file it came from:
# the group under its own name, the modifier with its whole UI. The app no
# longer mirrors these settings, so the modifier panel is the only place to tune
# it and anything hidden here would be unreachable.
ok(built["group"] == asset["group"]["group_name"],
   "build: the group keeps its real name, undecorated (%s)" % built["group"])
gn = ob.modifiers["Affector Deform"]
ok(all(getattr(gn, f, True) for f in ("show_group_selector", "show_manage_panel")),
   "build: the modifier keeps its full UI - nothing is hidden from the user")

kinds = [(m.type, m.name) for m in ob.modifiers]
ok(kinds == [("DECIMATE", "Unsubdivide"), ("NODES", "Affector Deform")],
   "build: DECIMATE runs BEFORE the node group - the rig is tuned on the coarsened mesh")

live = assets.get_inputs(built["object"], "Affector Deform")
ok(abs(live.get("Thickness", 0) - asset["defaults"]["Thickness"]) < 1e-6,
   "build: defaults are applied")
ok(live.get("Smooth") == 20, "build: including the int ones")
ok(live.get("Affectors") in (None, ""), "build: with no affector assigned yet")

# ----------------------------------------------------------- setting inputs

res = assets.set_inputs(built["object"], "Affector Deform", {"Keep Width": 0.25})
ok(res["applied"].get("Keep Width") == 0.25 and not res["skipped"], "inputs: set by NAME")
ok(abs(assets.get_inputs(built["object"], "Affector Deform")["Keep Width"] - 0.25) < 1e-6,
   "inputs: and it reads back")

res = assets.set_inputs(built["object"], "Affector Deform", {"Nonexistent": 1.0})
ok("Nonexistent" in res["skipped"] and not res["applied"],
   "inputs: an unknown name is REPORTED, not silently dropped")
assets.set_inputs(built["object"], "Affector Deform",
                  {"Keep Width": asset["defaults"]["Keep Width"]})

# A rebuilt group gets its OWN Socket_N numbering, so anything driving it by
# identifier would land values on the wrong inputs without erroring.
mod = ob.modifiers["Affector Deform"]
ids = [i.identifier for i in mod.node_group.interface.items_tree
       if i.item_type == "SOCKET" and i.in_out == "INPUT"]
ok(len(set(ids)) == len(ids), "inputs: the rebuilt interface has unique identifiers")

# ------------------------------------------------------- does it DEFORM?
# The checks that actually matter.

rest = evaluated(ob)
ok(len(rest) == 9216, "deform: evaluates with no affector")

aspec = asset["affector"]
af, af_coll = make_collider("Probe", aspec["radius"], aspec["length"], "Probes")
ok(len(af.data.vertices) > 100, "affector: the probe capsule is built")
assets.set_inputs(built["object"], "Affector Deform", {"Affectors": af_coll.name})
ok(assets.get_inputs(built["object"], "Affector Deform").get("Affectors")
   == af_coll.name,
   "affector: a collection can be assigned to the Affectors socket")

# Park it inside the tube so it must bite.
af.location = (asset["object"]["geometry"]["major_radius"], 0.0, 0.0)
bpy.context.view_layer.update()
hit = evaluated(ob)
moved = sum(1 for a, b in zip(rest, hit) if (a - b).length > 1e-6)
ok(moved > 100, "deform: an affector inside the tube MOVES the mesh (%d verts)" % moved)

# Emptying the socket must return it to the undeformed state exactly - that is
# how the handoff isolates the collider's contribution, and it proves the
# deformation really comes from the affector and not from something baked in.
assets.set_inputs(built["object"], "Affector Deform", {"Affectors": None})
bpy.context.view_layer.update()
back = evaluated(ob)
worst = max((a - b).length for a, b in zip(rest, back)) if back else 1.0
ok(worst < 1e-6, "deform: removing the affector restores it exactly (worst %.2e)" % worst)

# An input must visibly change the result, or the modifier panel is decorative.
assets.set_inputs(built["object"], "Affector Deform", {"Affectors": af_coll.name})
bpy.context.view_layer.update()
before = evaluated(ob)
assets.set_inputs(built["object"], "Affector Deform", {"Thickness": 0.03})
bpy.context.view_layer.update()
after = evaluated(ob)
ok(max((a - b).length for a, b in zip(before, after)) > 1e-5,
   "deform: changing Thickness changes the geometry")

# ------------------------------------------------------------------ status

st = assets.asset_status(built["object"], "Affector Deform")
ok(st["present"] and st["inputs"].get("Affectors") == af_coll.name,
   "status: reports what is in the scene")
missing = assets.asset_status("No Such Object", "Affector Deform")
ok(missing["present"] is False, "status: and says so when the rig is absent")

print("")
print("%d passed, %d failed" % (len(PASS), len(FAIL)))
for f in FAIL:
    print("FAIL " + f)
sys.exit(1 if FAIL else 0)
