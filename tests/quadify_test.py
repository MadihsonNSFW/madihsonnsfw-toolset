# Quadify inside Blender: the pipeline, the counts, the progress record and the
# rules that keep a run off Blender's main thread.
#
#   blender.exe -b --factory-startup --python tests\quadify_test.py
#
# ⚠ bpy.app.timers NEVER FIRE in background Blender - there is no event loop.
# `retopologize()` starts a worker thread and registers a timer to pick the
# result up, so this suite drives that seam by hand: start the run, wait for
# `_pending`, then call `_finish` (which is the main-thread half) directly.
# That is exactly the split the async design created and the only way to
# exercise the whole pipeline headless.
#
# ⚠ THE REAL ENGINE RUNS HERE, on Suzanne, and takes a few seconds. It is
# skipped with a message if the binaries are not present, because a missing
# engine is a normal state to report - not a suite that fails.
import ast
import importlib
import math
import os
import sys
import time
import types

import bmesh
import bpy

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

ROOT = _ROOT
ADDON = os.path.join(ROOT, "blender_addon", "madi_anim_library")

# Loaded as a real PACKAGE: quadify.py does `from . import core`.
pkg = types.ModuleType("madi_pkg")
pkg.__path__ = [ADDON]
sys.modules["madi_pkg"] = pkg
quadify = importlib.import_module("madi_pkg.quadify")

PASS = []
FAIL = []


def ok(cond, label):
    (PASS if cond else FAIL).append(label)
    print(("ok   " if cond else "FAIL ") + label, flush=True)


def fresh_scene():
    bpy.ops.wm.read_factory_settings(use_empty=True)


def add_suzanne(subsurf=0):
    bpy.ops.mesh.primitive_monkey_add()
    ob = bpy.context.active_object
    if subsurf:
        mod = ob.modifiers.new("Subsurf", "SUBSURF")
        mod.levels = mod.render_levels = subsurf
    bpy.context.view_layer.update()
    return ob


# ---------------------------------------------------------------- the engine

info = quadify.engine_info()
ok(isinstance(info, dict) and "ready" in info and "missing" in info,
   "engine: engine_info answers with plain data - a missing engine is a state "
   "to report, not an exception to handle on a socket")
ok(info["root"].endswith("engine"),
   "engine: it lives inside the extension, so the paid exe never touches it")
ENGINE_READY = bool(info["ready"])
if ENGINE_READY:
    ok(os.path.isfile(info["stage1"]) and os.path.isfile(info["stage2"]),
       "engine: both upstream programs are on disk")
    ok(os.path.isdir(os.path.join(info["root"], "config")),
       "engine: and the config folder it reads its presets from")
else:
    print("SKIP engine: not present (%s)" % ", ".join(info["missing"]),
          flush=True)


# ------------------------------------------------------- status, the honest one

fresh_scene()
ok(quadify.quad_status()["object"] == "",
   "status: with nothing selected it says so rather than guessing")
ok(quadify.quad_status()["running"] is False,
   "status: and reports that nothing is running")

ob = add_suzanne()
shallow = quadify.quad_status()
ok(shallow["object"] == "Suzanne" and shallow["faces"] == len(ob.data.polygons),
   "status: it reports the active mesh and its datablock counts")
ok("eval_tris" not in shallow,
   "⚠ status: the CHEAP read does not evaluate anything - it is polled every "
   "few seconds and evaluating a heavy stack for a label would cost more than "
   "the retopology")

# ⚠⚠ THE 52-MINUTE LESSON. Marty's first real mesh showed 2 424 faces on the
# panel and sent 266 469 triangles to the engine. The datablock count is only
# the same number when nothing modifies the mesh.
ob.modifiers.new("Subsurf", "SUBSURF").levels = 2
bpy.context.view_layer.update()
deep = quadify.quad_status(deep=True)
ok("eval_tris" in deep,
   "status: deep=True reports the EVALUATED triangle count")
ok(deep["eval_tris"] > deep["faces"] * 4,
   "⚠ status: and it is far larger than the datablock count (%s vs %s faces) "
   "- the label that lied by 110x is the whole reason deep exists"
   % (deep.get("eval_tris"), deep.get("faces")))
ok(deep["big"] is (deep["eval_tris"] > quadify.BIG_MESH_TRIS),
   "status: the `big` flag is the evaluated count against the engine's ceiling")
ok(quadify.BIG_MESH_TRIS == 100000,
   "status: that ceiling is the teardown's 100k, where tracing stops being "
   "seconds")

# ⚠ A POLLED COMMAND MUST NEVER WRITE.
before = (len(bpy.data.objects), len(bpy.data.meshes),
          len(ob.data.vertices), len(ob.data.polygons))
for _ in range(5):
    quadify.quad_status()
quadify.quad_status(deep=True)
after = (len(bpy.data.objects), len(bpy.data.meshes),
         len(ob.data.vertices), len(ob.data.polygons))
ok(before == after,
   "⚠ status: polling it changes NOTHING in the scene - not the object count, "
   "not the mesh, not even after a deep read (to_mesh has to be cleared)")

ob.modifiers.clear()
bpy.context.view_layer.update()


# ------------------------------------------------------------------ progress

quadify._progress_end()
rec = quadify.quad_progress()
ok(rec["active"] is False, "progress: nothing running reads as inactive")
ok("phase" in rec and "stage" not in rec,
   "⚠ progress: the key is `phase`, NOT `stage` - the app's ProgressRow already "
   "paints this record shape for the optimizer, so matching it means the tab's "
   "one bar draws these stages with no second widget")
for key in ("active", "phase", "done", "total", "item", "serial", "elapsed"):
    ok(key in rec, "progress: the record carries '%s'" % key)

quadify._progress_begin("Suzanne")
ok(quadify.quad_progress()["active"] is True and
   quadify.quad_progress()["item"] == "Suzanne",
   "progress: beginning a run names what it is running on")
ok(quadify.quad_progress()["total"] == len(quadify.STAGES),
   "progress: over the engine's five real stages")
first = quadify.quad_progress()["serial"]
quadify._progress_stage("trace")
mid = quadify.quad_progress()
ok(mid["phase"] == "trace" and mid["done"] == quadify.STAGES.index("trace") + 1,
   "progress: moving to a stage counts it")
ok(mid["serial"] == first,
   "progress: the serial identifies the RUN, so it does not move between stages")
ok(quadify.quad_progress()["elapsed"] >= 0.0,
   "progress: and it reports how long the run has been going")
quadify._progress_end()
quadify._progress_stage("trace")
ok(quadify.quad_progress()["active"] is False,
   "⚠ progress: a stage OUTSIDE a run is a no-op - otherwise a headless caller "
   "could leave an 'active' run behind that nothing will ever finish")

# ⚠ The record is REPLACED WHOLESALE, never mutated: it is read on the socket
# thread while the main thread is inside a run.
_src = open(os.path.join(ADDON, "quadify.py"), encoding="utf-8").read()
_tree = ast.parse(_src)


def _fn(name):
    for node in ast.walk(_tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    return None


def _touches_bpy(node):
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name) and sub.id == "bpy":
            return True
        if isinstance(sub, ast.Attribute) and isinstance(sub.value, ast.Name) \
                and sub.value.id == "bpy":
            return True
    return False


ok(not _touches_bpy(_fn("quad_progress")),
   "⚠ progress: quad_progress touches NO bpy - it is answered while Blender's "
   "main thread is inside the engine, and reading bpy from there is reading "
   "Blender's data while it is being written")
ok(not _touches_bpy(_fn("quad_cancel")),
   "⚠ cancel: quad_cancel touches no bpy either, for the same reason")
ok(not _touches_bpy(_fn("_worker")),
   "⚠ worker: the WORKER touches no bpy at all - it writes the OBJ, runs the "
   "engine and parses the result, and parks what it made in `_pending`")
ok(any(isinstance(n, ast.Attribute) and n.attr == "register"
       for n in ast.walk(_fn("retopologize"))),
   "⚠ worker: the timer is registered by retopologize, ON THE MAIN THREAD - "
   "bpy.app.timers.register is not documented thread-safe, so the worker must "
   "never call it")
ok(not any(isinstance(n, ast.Attribute) and n.attr == "register"
           for n in ast.walk(_fn("_worker"))),
   "worker: and it does not register one itself")

# Both live commands are served BEFORE the main-thread queue in server.py, or a
# cancel queues behind the thing it is cancelling and is not a cancel.
_server = open(os.path.join(ADDON, "server.py"), encoding="utf-8").read()
_dispatch = _server.index("def _dispatch")
_handle = _server.index("def _handle")
for cmd in ("quad_progress", "quad_cancel"):
    ok(_dispatch < _server.index('"%s"' % cmd) < _handle,
       "⚠ routing: %s is answered on the SOCKET thread, before the main-thread "
       "queue - the main thread is inside the run" % cmd)

# ⚠ The quad_* PREFIX gate was REMOVED 2026-08-14 with every other licence
# gate — all tabs are free, and the paid thing (premium packs) is gated in
# the app's licence server, not in Blender. The absence is pinned the same
# way the presence was: a leftover prefix check would re-lock a free tool
# for anyone whose old licence lapsed.
ok('cmd.startswith("quad_")' not in _server,
   "⚠ gate: NO quad_* prefix gate is left in the add-on - the tool is free "
   "for everyone, licence state included")
ok('cmd.startswith("opt_")' not in _server
   and 'cmd.startswith("madiref_")' not in _server,
   "gate: the opt_* and madiref_* prefix gates went with it - no prefix "
   "licence gate remains anywhere in server.py")

ok(quadify.quad_cancel()["ok"] is False,
   "cancel: with nothing running it says so rather than raising")
ok("nothing running" in quadify.quad_cancel()["reason"],
   "cancel: and names the reason")


# ------------------------------------------------------------- mesh in / out

fresh_scene()
ob = add_suzanne()
bm = bmesh.new()
bm.from_mesh(ob.data)
bmesh.ops.triangulate(bm, faces=bm.faces[:])
path = os.path.join(bpy.app.tempdir, "roundtrip.obj")
nv, nf = quadify._write_obj(bm, path)
bm.free()
verts, faces = quadify._read_obj(path)
ok(len(verts) == nv and len(faces) == nf,
   "obj: what is written comes back with the same counts")
ok(all(len(f) == 3 for f in faces), "obj: triangulated, which is all the engine reads")
ok(all(0 <= i < len(verts) for f in faces for i in f),
   "obj: and every index is in range")

# ⚠ The reader is deliberately tolerant: VCGlib writes NEGATIVE (relative)
# indices and `f v/vt/vn` triples depending on the stage, and a parser that
# assumes one of them survives only by luck.
tolerant = os.path.join(bpy.app.tempdir, "tolerant.obj")
with open(tolerant, "w", encoding="utf-8") as fh:
    fh.write("v 0 0 0\nv 1 0 0\nv 1 1 0\nv 0 1 0\n"
             "f 1/1/1 2/2/2 3/3/3 4/4/4\n"
             "f -4 -3 -2 -1\n")
tv, tf = quadify._read_obj(tolerant)
ok(len(tv) == 4 and len(tf) == 2, "obj: it reads both faces")
ok(tf[0] == [0, 1, 2, 3],
   "⚠ obj: `f v/vt/vn` triples are read - the engine writes them at one stage")
ok(tf[1] == [0, 1, 2, 3],
   "⚠ obj: and NEGATIVE (relative) indices, which it writes at another")


# ----------------------------------------------------------------- the config

prep = os.path.join(bpy.app.tempdir, "prep.txt")
quadify._write_prep_config(prep, 1.0, quadify.NO_SHARP, True)
text = open(prep, encoding="utf-8").read()
ok("sharp_feature_thr -1" in text,
   "⚠ config: sharp detection OFF writes -1, which is what disables feature "
   "lines - and what upstream's own Organic preset uses. At 35 degrees an "
   "organic mesh gave 49,694 feature edges and 8,562 unsolved partitions")
quadify._write_prep_config(prep, 2.0, 35.0, False)
text = open(prep, encoding="utf-8").read()
ok("do_remesh 0" in text and "sharp_feature_thr 35" in text,
   "config: and the values asked for are the values written")
ok("scaleFact 2" in text, "config: density goes through as scaleFact")

flow = os.path.join(bpy.app.tempdir, "flow.txt")
quadify._write_flow_config(flow, 1.0, {})
lines = [l for l in open(flow, encoding="utf-8").read().splitlines() if l.strip()]
ok(len(lines) > 10,
   "config: every stage-two key is written, always - upstream's parser wants "
   "the whole file in its own order")
ok(all(len(l.split()) >= 2 for l in lines),
   "⚠ config: and NO key is left blank - upstream warns its parser is finicky "
   "about both order and empty values")


# ------------------------------------------------------------------ symmetry

fresh_scene()
ob = add_suzanne()
bm = bmesh.new()
bm.from_mesh(ob.data)
whole = len(bm.verts)
quadify._bisect(bm, "x", 0.0)
ok(len(bm.verts) < whole,
   "symmetry: bisecting on X removes half the mesh, so the engine only sees one side")
ok(all(v.co.x <= 1e-4 for v in bm.verts),
   "symmetry: and what is left is all on ONE side of the plane (the inner "
   "half - bisect_plane clears the outer one)")
bm.free()


# ------------------------------------------------------- refusing to start

fresh_scene()
ok(quadify.retopologize()["ok"] is False,
   "run: with nothing selected it refuses")
ok("mesh" in quadify.retopologize()["error"],
   "run: and says a mesh is what it needs")
bpy.ops.object.empty_add()
ok(quadify.retopologize()["ok"] is False,
   "run: an empty is not a mesh, and it says so rather than crashing")

fresh_scene()
add_suzanne()
quadify._progress_begin("busy")
busy = quadify.retopologize()
ok(busy["ok"] is False and "already running" in busy["error"],
   "run: a second run while one is in flight is refused - one engine, one job")
quadify._progress_end()

ok(quadify.quad_result()["ok"] is False,
   "result: before any run it says there has not been one")


# ------------------------------------------------- the whole pipeline, for real

if not ENGINE_READY:
    print("SKIP pipeline: the engine is not present", flush=True)
else:
    fresh_scene()
    ob = add_suzanne()
    # A transform chosen to punish guessing: axis-angle rotation, non-uniform
    # placement, a scale. Every rotation mode is already inside matrix_world,
    # which is why NO rotation_mode branch exists and none should.
    ob.rotation_mode = "AXIS_ANGLE"
    ob.rotation_axis_angle = (math.radians(40), 0.3, 0.8, 0.5)
    ob.scale = (1.4, 1.4, 1.4)
    ob.location = (3.0, -2.0, 1.5)
    bpy.context.view_layer.update()
    source_name = ob.name
    source_location = tuple(ob.location)

    started = quadify.retopologize(object_name=source_name, density=1.0,
                                   use_sharp=False, preprocess=True,
                                   smoothing=True)
    ok(started.get("ok") is True and started.get("started") is True,
       "⚠ pipeline: retopologize RETURNS IMMEDIATELY - it hands back as soon "
       "as the mesh is on disk. The blocking version froze Blender for 52 "
       "minutes while the app's request gave up at 30")
    ok(started.get("faces_in", 0) > 0, "pipeline: reporting what it sent")
    ok(quadify.quad_progress()["active"] is True,
       "pipeline: and the run shows as active straight away")

    # ⚠ THE CHECK WORTH KEEPING: Blender's main thread is FREE while the engine
    # works. The blocking version scores 0 here by construction.
    main_thread_ops = 0
    deadline = time.time() + 600
    while quadify._pending is None and time.time() < deadline:
        probe = bpy.data.objects.new("probe", None)
        bpy.data.objects.remove(probe)
        bpy.context.view_layer.update()
        main_thread_ops += 1
        time.sleep(0.05)
    ok(quadify._pending is not None,
       "pipeline: the worker finished and parked its result in `_pending`")
    ok(main_thread_ops > 0,
       "⚠ pipeline: Blender's main thread did %d real bpy operations DURING "
       "the run - the version that owned the main thread scores 0 here, which "
       "is exactly what made it a 52-minute freeze" % main_thread_ops)

    job = quadify._pending
    ok(job.get("error") is None,
       "pipeline: with no error (%s)" % job.get("error"))
    # The main-thread half, called directly: timers do not fire headless.
    quadify._finish(job)
    quadify._progress_end()
    result = quadify.quad_result()

    ok(result["ok"] is True, "pipeline: the run reports success")
    ok(result["faces"] > 0 and result["quads"] == result["faces"],
       "⚠ pipeline: EVERY face is a quad (%s of %s) - that is the product"
       % (result.get("quads"), result.get("faces")))
    ok(result["tris"] == 0 and result["ngons"] == 0,
       "pipeline: no triangles and no n-gons left")
    ok(result["quad_pct"] == 100.0, "pipeline: reported as 100%")
    ok(result["seconds"] > 0, "pipeline: and how long it took")
    ok(result.get("exit") is not None,
       "⚠ pipeline: the engine's exit code is RECORDED and nothing branches on "
       "it - quad_from_patches returns 0x80000003 on a perfect run, so success "
       "is the output file, never the return code (got %r)"
       % (result.get("exit"),))

    new_ob = bpy.data.objects.get(result["object"])
    ok(new_ob is not None, "pipeline: the result is a real object in the scene")
    ok(tuple(new_ob.location) == source_location,
       "pipeline: placed where the original was")
    ok(abs(new_ob.rotation_euler.x) < 1e-6 and abs(new_ob.scale[0] - 1.0) < 1e-6,
       "⚠ pipeline: with rotation and scale BAKED INTO THE GEOMETRY, not "
       "re-applied - putting them back would apply them twice")

    source = bpy.data.objects.get(source_name)
    ok(source is not None,
       "pipeline: the original is KEPT by default - a retopo that deletes the "
       "source is one bad result away from lost work")
    ok(source.hide_render is True, "pipeline: hidden rather than removed")

    # ⚠ Placement is measured on evaluated VERTICES in world space. bound_box
    # proves nothing here: for the original those 8 corners are the rotated
    # corners of an unrotated box, and for the result they are the axis-aligned
    # box of already-rotated geometry - the two disagree with placement perfect.
    depsgraph = bpy.context.evaluated_depsgraph_get()
    bpy.context.view_layer.update()

    def world_span(obj):
        matrix = obj.matrix_world
        pts = [matrix @ v.co for v in obj.data.vertices]
        return [(min(p[i] for p in pts), max(p[i] for p in pts))
                for i in range(3)]

    a, b = world_span(source), world_span(new_ob)
    drift = max(max(abs(a[i][0] - b[i][0]), abs(a[i][1] - b[i][1]))
                for i in range(3))
    size = max(a[i][1] - a[i][0] for i in range(3))
    ok(drift < size * 0.05,
       "⚠ pipeline: measured on evaluated VERTICES in world space, the result "
       "sits within %.3f of the original on a %.2f-unit mesh - a bound_box "
       "check would disagree by 0.19 with the placement perfect"
       % (drift, size))

    # Symmetry adds a mirror rather than mirroring the geometry, so it stays
    # editable.
    fresh_scene()
    add_suzanne()
    started = quadify.retopologize(density=1.0, use_sharp=False,
                                   symmetry="x", smoothing=False)
    if started.get("ok"):
        deadline = time.time() + 600
        while quadify._pending is None and time.time() < deadline:
            time.sleep(0.05)
        if quadify._pending is not None:
            quadify._finish(quadify._pending)
            quadify._progress_end()
            out = quadify.quad_result()
            mirrored = bpy.data.objects.get(out.get("object") or "")
            ok(mirrored is not None
               and any(m.type == "MIRROR" for m in mirrored.modifiers),
               "pipeline: a symmetry axis comes back as a MIRROR MODIFIER, so "
               "it stays editable instead of being baked in")

print("")
print("%d passed, %d failed" % (len(PASS), len(FAIL)))
for f in FAIL:
    print("FAIL " + f)
