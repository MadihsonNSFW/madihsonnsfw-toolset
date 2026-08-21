"""MADI Quadify — quad retopology, driven from the add-on. See docs\\quadify.md.

Select a mesh, get clean all-quad topology back. We do **not** write the
remeshing maths: the engine is QuadWild + Bi-MDF (SIGGRAPH 2021 / 2023),
shipped as two upstream command-line programs inside this extension.

THE ARCHITECTURE, AND WHY IT IS THE WAY IT IS

⚠ **The engine runs as a SUBPROCESS. It is never loaded into Blender.** Two
independent reasons, either one sufficient:

1. **Licensing.** The engine is GPL-3.0. Loading it into a process is a linking
   argument; running a GPL *program* from ours is not, the way everyone uses
   ffmpeg. The paid Toolset app never touches it at all — it only asks this
   add-on over the bridge.
2. **It aborts.** Upstream calls `exit()` on a malformed feature file or an
   unloadable mesh, and stage C aborts even on SUCCESS (see below). Inside a
   loaded library that is Blender gone with no warning and no save. Out here it
   is an exit code we read and ignore.

⚠⚠ **THE EXIT CODE LIES — SUCCESS IS THE OUTPUT FILE, NEVER THE RETURN CODE.**
Measured 2026-08-13: `quad_from_patches` writes its result, flushes it, prints
its profiler tree and *then* returns `0x80000003` (STATUS_BREAKPOINT) on a run
that produced a perfect 100 %-quad mesh. Stage one is clean, stage two never is.
So `_run_stage` treats the code as a log hint and `retopologize` decides success
by asking whether the expected file exists, is newer than the input and parses.
Anything that starts trusting `returncode` here will report every good retopo as
a failure.

WHAT LANDS ON DISK, AND WHERE

⚠ **The engine writes beside the INPUT MESH and names everything after it** —
there is no output-directory argument. So every run gets its own uuid temp
directory holding one file called `mesh.obj`, which means the output names are
fixed and known (`mesh_rem_p0_1_quadrangulation_smooth.obj`) and two objects
retopologised at once cannot collide. It also means we never sanitise a user's
object name into a filename, because the object's name never becomes one.

TRANSFORMS

⚠ **Rotation and scale are baked, location is not** — the engine works in the
mesh's own space and we put the result back at the object's location. We do it
by stripping the translation out of `matrix_world`, which is why there is no
`rotation_mode` branch anywhere in this file: quaternion, euler, axis-angle and
delta transforms are all already inside that matrix. QRemeshify special-cases
`'QUATERNION'` and silently gets axis-angle wrong; not branching is the fix.

SHARP EDGES

The engine finds its own feature lines from a dihedral threshold
(`sharp_feature_thr`), and `-1` disables them — upstream's own "Organic" preset
does exactly that. ⚠ **So this build does NOT yet honour edges the user marked
sharp by hand**, or seams, or material borders; that needs us to write the
`.sharp` format and switch stage one to feature mode 1, and it is not proven.
Say so in the UI rather than implying marked edges are respected.
"""

import bmesh
import bpy
import mathutils
import os
import re
import subprocess
import sys
import threading
import time
import uuid

from . import quadpreserve

# Stage one's second argument: 2 = detect feature lines from the angle
# threshold. Stage two's is the quantisation method upstream's README uses.
_STAGE1_MODE = "2"
_STAGE2_MODE = "1"

# `sharp_feature_thr -1` means "no sharp features" — upstream's basic_setup_
# Organic.txt is the source for that, not a guess.
NO_SHARP = -1.0

# Windows: a subprocess must not flash a console window on top of Blender.
_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def engine_dir():
    """Where the two upstream programs live: inside this extension."""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "engine")


def engine_info():
    """What we have, as plain data — the app greys its button on this.

    Never raises: a missing engine is a normal state to report, not an error to
    handle, and the caller is a socket reply.
    """
    root = engine_dir()
    exe = ".exe" if sys.platform == "win32" else ""
    stage1 = os.path.join(root, "quadwild" + exe)
    stage2 = os.path.join(root, "quad_from_patches" + exe)
    config = os.path.join(root, "config")
    missing = [name for name, path in (("quadwild", stage1),
                                       ("quad_from_patches", stage2),
                                       ("config", config))
               if not os.path.exists(path)]
    return {"root": root, "stage1": stage1, "stage2": stage2,
            "ready": not missing, "missing": missing}


# The teardown's practical ceiling: past this the tracing stage stops being
# seconds and starts being tens of minutes. Marty's first real mesh was 266 469.
BIG_MESH_TRIS = 100000


def quad_status(deep=False):
    """What the tool needs to draw itself: the engine, and what is selected.

    ⚠ **The plain reading is CHEAP and must never write** — it reports the
    mesh datablock's counts, because evaluating a heavy modifier stack every
    2.5 s to put a number on a label would cost more than the retopo.

    ⚠⚠ **`deep=True` IS THE HONEST NUMBER AND THE UI MUST USE IT.** The
    datablock count is what the engine gets ONLY when nothing modifies the
    mesh. On Marty's first real run the panel said **2 424 faces** and what
    actually went to the engine was **266 469 triangles** — 110× more. He
    pressed a button expecting seconds and got 52 minutes. A label that wrong
    is not an approximation, it is a control that lies. `deep` costs one
    evaluated `to_mesh()`, so it is called when the tool is SHOWN, never on
    the poll.
    """
    info = engine_info()
    ob = bpy.context.active_object
    meshes = [o.name for o in bpy.context.selected_objects if o.type == "MESH"]
    out = {"engine_ready": info["ready"], "engine_missing": info["missing"],
           "selected": meshes, "object": "", "verts": 0, "faces": 0,
           "modifiers": 0, "running": _progress["active"]}
    if ob is not None and ob.type == "MESH":
        out.update({"object": ob.name, "verts": len(ob.data.vertices),
                    "faces": len(ob.data.polygons),
                    "modifiers": len(ob.modifiers)})
        # What "Preserve rig data" would actually have to carry. All three are
        # plain len() on data already in memory, so the cheap poll stays cheap.
        keys = ob.data.shape_keys
        out.update({
            "shape_keys": len(keys.key_blocks) if keys else 0,
            "vertex_groups": len(ob.vertex_groups),
            "deform_modifiers": [m.name for m in ob.modifiers
                                 if m.type in quadpreserve.DEFORM_TYPES],
        })
        if deep:
            try:
                depsgraph = bpy.context.evaluated_depsgraph_get()
                ob_eval = ob.evaluated_get(depsgraph)
                mesh = ob_eval.to_mesh()
                # What the engine really receives: triangulated.
                tris = sum(len(p.vertices) - 2 for p in mesh.polygons)
                verts = len(mesh.vertices)
                ob_eval.to_mesh_clear()
                out["eval_tris"] = tris
                out["eval_verts"] = verts
                out["big"] = tris > BIG_MESH_TRIS
            except Exception as exc:            # noqa: BLE001
                out["eval_error"] = str(exc)
    return out


def quad_select(object_name=""):
    """Select the named object and make it active — the report's 'Select
    result' button, so the thing that was just made is the thing under the
    cursor when the user turns back to Blender."""
    ob = bpy.data.objects.get(object_name)
    if ob is None:
        return {"ok": False, "error": "no object named '%s'" % object_name}
    for other in bpy.context.selected_objects:
        other.select_set(False)
    ob.select_set(True)
    bpy.context.view_layer.objects.active = ob
    return {"ok": True, "object": ob.name}


# ---------------------------------------------------------------------------
# Progress
# ---------------------------------------------------------------------------
# Same contract as optimizer.py, for the same reason: this record is read on the
# SOCKET thread while Blender's main thread is inside a run, so it is REPLACED
# WHOLESALE and never mutated in place. `_progress["done"] += 1` would be two
# bytecodes with a visible gap between them.
#
# ⚠ NO bpy ACCESS in quad_progress(), ever.
#
# The stages are real, not a timer: the engine narrates itself on stdout and we
# read it line by line as it runs. `_STAGE_MARKERS` is the whole parser.
STAGES = ("preprocess", "field", "trace", "quantize", "smooth")

_STAGE_MARKERS = (
    ("Before Remeshing", "preprocess"),
    ("fieldComputation", "field"),
    ("TRACING STEP", "trace"),
    ("Bi-MDF setup", "quantize"),
    ("SMOOTHING", "smooth"),
)

# "**** SUBPATCH TRACING - THERE ARE 29 Unsolved Partitions ****" — the one
# genuine countdown the engine gives us, and it counts DOWN to zero.
_UNSOLVED = re.compile(r"THERE ARE\s+(\d+)\s+Unsolved", re.I)

# ⚠ The key is `phase`, not `stage`, and that is not cosmetic: the app's
# ProgressRow already paints exactly this record shape for the optimizer, so
# matching it means the tab's one bar draws our stages with no second widget and
# no second set of bugs.
_progress = {"active": False, "phase": "", "done": 0, "total": 0,
             "item": "", "serial": 0, "started": 0.0}


def _progress_set(**changes):
    """Publish a new record. Never mutates the old one — see the note above."""
    global _progress
    record = dict(_progress)
    record.update(changes)
    _progress = record


def _progress_begin(item=""):
    _progress_set(active=True, phase=STAGES[0], done=0, total=len(STAGES),
                  item=str(item), serial=_progress["serial"] + 1,
                  started=time.time())


def _progress_end():
    _progress_set(active=False, phase="", done=0, total=0, item="")


def _progress_stage(stage):
    """Move to a named stage. No-op outside a run, so a headless caller that
    invokes the pipeline directly cannot leave an 'active' run behind that
    nothing will ever finish."""
    if _progress["active"] and stage in STAGES:
        _progress_set(phase=stage, done=STAGES.index(stage) + 1,
                      total=len(STAGES))


def quad_progress():
    """How far the run in flight has got. **Answered off the main thread.**

    ⚠ NO bpy ACCESS HERE, EVER. server.py routes this before the main-thread
    queue precisely because the main thread is busy running the engine; that is
    the only way an answer comes back at all. Touching bpy from here would be
    reading Blender's data while it is being written.
    """
    snapshot = _progress                    # one atomic read of the global
    elapsed = (time.time() - snapshot["started"]) if snapshot["active"] else 0.0
    out = dict(snapshot)
    out["elapsed"] = round(max(0.0, elapsed), 2)
    return out


# ---------------------------------------------------------------------------
# Mesh in, mesh out
# ---------------------------------------------------------------------------

def _evaluated_bmesh(ob, depsgraph):
    """The object as it renders — modifiers and shape keys applied — in its own
    space, with rotation and scale baked in and location left alone.

    ⚠⚠ **`new_from_object`, NOT `to_mesh()`, AND THAT CHOICE IS A CRASH FIX
    (2026-08-21).** Marty's Blender died on Retopologize with
    EXCEPTION_ACCESS_VIOLATION inside `BM_mesh_bm_from_me`. The cause is a real
    inconsistency in what `to_mesh()` hands back: on a mesh with shape keys and
    a topology-changing modifier, **the evaluated mesh keeps the ORIGINAL Key**.
    Measured on his character: `to_mesh()` returned **296,474 vertices carrying
    436 key blocks of 18,675 elements each**. `bm.from_mesh` copies shape-key
    data per vertex, so it walks 296,474 vertices through arrays of 18,675 and
    reads **277,799 vertices past the end of all 436 of them**.

    ⚠⚠ **THE OVERRUN HAPPENS ON EVERY RUN; ONLY THE CRASH IS OCCASIONAL.** An
    out-of-bounds READ faults only when nothing is mapped after the allocation,
    so the same file, same object and same code survived eight reads in one
    process and died on the first in another. "It worked when I tried it" means
    nothing here — which is why the regression check asserts the SHAPE OF THE
    DATA (no shape-key layers) instead of that a call returned. A survival
    check would have passed while the bug shipped.

    `bpy.data.meshes.new_from_object(..., preserve_all_data_layers=False)`
    hands back the same geometry (296,474 verts / 295,848 faces, identical)
    with **no shape keys at all** — which is right for this module twice over:
    Quadify triangulates and writes an OBJ, so it never wanted them. Measured
    on that mesh it is also **4× faster** — 0.33 s against 1.32 s — because
    copying 436 layers was most of the work.

    ⚠ It is a REAL datablock with no users, not a borrowed temporary, so it
    must be removed — in a `finally`, because a raise here would otherwise leak
    a 296,474-vertex mesh into the .blend.
    """
    ob_eval = ob.evaluated_get(depsgraph)
    mesh = bpy.data.meshes.new_from_object(
        ob_eval, preserve_all_data_layers=False, depsgraph=depsgraph)
    if mesh is None:
        raise RuntimeError("'%s' evaluates to nothing that can be meshed"
                           % ob.name)
    bm = bmesh.new()
    try:
        bm.from_mesh(mesh)
    finally:
        bpy.data.meshes.remove(mesh)

    # Strip the translation and apply what is left. Every rotation mode is
    # already baked into this matrix — see the module docstring.
    matrix = ob.matrix_world.copy()
    matrix.translation = mathutils.Vector((0.0, 0.0, 0.0))
    bm.transform(matrix)

    # A negative scale flips winding; the engine reads orientation.
    if matrix.determinant() < 0.0:
        bmesh.ops.reverse_faces(bm, faces=bm.faces[:])
    return bm


def _bisect(bm, axes, offset=0.0):
    """Cut away half the mesh per symmetry axis, so the engine only ever sees
    the half we keep and a Mirror modifier rebuilds the rest.

    ⚠ The plane sits at the object's ORIGIN plus `offset`. An off-origin
    character mirrors in the wrong place otherwise — QRemeshify has no way to
    say so at all, which is why the offset is a parameter here.
    """
    for index, axis in enumerate("xyz"):
        if axis not in axes:
            continue
        normal = [0.0, 0.0, 0.0]
        normal[index] = 1.0
        centre = [0.0, 0.0, 0.0]
        centre[index] = offset
        geom = bm.verts[:] + bm.edges[:] + bm.faces[:]
        bmesh.ops.bisect_plane(bm, geom=geom, dist=1e-6,
                               plane_co=mathutils.Vector(centre),
                               plane_no=mathutils.Vector(normal),
                               clear_outer=True, clear_inner=False)
    return bm


def _write_obj(bm, path):
    """Hand-written OBJ: vertices and triangles, nothing else.

    The engine wants triangles and reads no other channel, so writing UVs,
    normals or materials here would only be data for it to discard.
    """
    bm.verts.ensure_lookup_table()
    lines = []
    for vert in bm.verts:
        co = vert.co
        lines.append("v %.6f %.6f %.6f" % (co.x, co.y, co.z))
    for face in bm.faces:
        lines.append("f " + " ".join(str(v.index + 1) for v in face.verts))
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines))
        handle.write("\n")
    return len(bm.verts), len(bm.faces)


def _read_obj(path):
    """Read the engine's result back: vertices and faces only.

    Deliberately tolerant of the writer it is fed — negative (relative) indices
    and `f v/vt/vn` triples both appear in VCGlib's output depending on the
    stage, and a 20-line parser that assumes one of them is how QRemeshify's
    importer survives only by luck.
    """
    verts = []
    faces = []
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if line.startswith("v "):
                parts = line.split()
                verts.append((float(parts[1]), float(parts[2]),
                              float(parts[3])))
            elif line.startswith("f "):
                face = []
                for token in line.split()[1:]:
                    index = int(token.split("/")[0])
                    face.append(index - 1 if index > 0 else len(verts) + index)
                if len(face) >= 3:
                    faces.append(face)
    return verts, faces


# ---------------------------------------------------------------------------
# The engine
# ---------------------------------------------------------------------------

def _write_prep_config(path, density, sharp_angle, preprocess, alpha=0.01):
    """Stage one's config. `sharp_feature_thr -1` disables feature lines."""
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("do_remesh %d\n" % (1 if preprocess else 0))
        handle.write("sharp_feature_thr %g\n" % sharp_angle)
        handle.write("alpha %g\n" % alpha)
        handle.write("scaleFact %g\n" % density)


def _write_flow_config(path, density, settings):
    """Stage two's config.

    ⚠ **Order matters and blank values are not allowed** — upstream warns its
    parser is finicky about both, so this writes every key, always, in upstream's
    own order. ⚠ The two filenames at the end stay RELATIVE: the engine resolves
    them against its working directory, which is why `_run_stage` runs with
    `cwd` set to the engine folder.

    ⚠ Eleven of these do nothing on this build — no Gurobi means only the
    Bi-MDF branch runs — and they are written anyway because the parser expects
    the full file. The UI must not offer them; see docs\\quadify.md.
    """
    values = {
        "alpha": settings.get("isometry_bias", 0.005),
        "regularityNonQuadrilateralsWeight":
            settings.get("ngon_regularity_weight", 0.9),
        "alignSingularities": int(bool(settings.get("align_singularities",
                                                    True))),
        "alignSingularitiesWeight":
            settings.get("singularity_align_weight", 0.1),
        "repeatLosingConstraintsQuads":
            int(bool(settings.get("repeat_quads", False))),
        "repeatLosingConstraintsNonQuads":
            int(bool(settings.get("repeat_ngons", False))),
        "repeatLosingConstraintsAlign":
            int(bool(settings.get("repeat_align", True))),
        "fixedChartClusters": int(settings.get("chart_cluster_size", 0)),
    }
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("alpha %g\n" % values["alpha"])
        handle.write("ilpMethod 1\n")
        handle.write("timeLimit 200\n")
        handle.write("gapLimit 0.0\n")
        handle.write("callbackTimeLimit 8 3.00 5.000 10.0 20.0 30.0 60.0 "
                     "90.0 120.0\n")
        handle.write("callbackGapLimit 8 0.005 0.02 0.05 0.10 0.15 0.20 "
                     "0.25 0.3\n")
        handle.write("minimumGap 0.4\n")
        handle.write("isometry 1\n")
        handle.write("regularityQuadrilaterals 1\n")
        handle.write("regularityNonQuadrilaterals 1\n")
        handle.write("regularityNonQuadrilateralsWeight %g\n"
                     % values["regularityNonQuadrilateralsWeight"])
        handle.write("alignSingularities %d\n" % values["alignSingularities"])
        handle.write("alignSingularitiesWeight %g\n"
                     % values["alignSingularitiesWeight"])
        handle.write("repeatLosingConstraintsIterations 1\n")
        handle.write("repeatLosingConstraintsQuads %d\n"
                     % values["repeatLosingConstraintsQuads"])
        handle.write("repeatLosingConstraintsNonQuads %d\n"
                     % values["repeatLosingConstraintsNonQuads"])
        handle.write("repeatLosingConstraintsAlign %d\n"
                     % values["repeatLosingConstraintsAlign"])
        handle.write("hardParityConstraint 1\n")
        handle.write("scaleFact %g\n" % density)
        handle.write("fixedChartClusters %d\n" % values["fixedChartClusters"])
        handle.write("useFlowSolver 1\n")
        handle.write('flow_config_filename '
                     '"config/main_config/flow_virtual_simple.json"\n')
        handle.write('satsuma_config_filename "config/satsuma/default.json"\n')


# ⚠ THE RUNNING ENGINE PROCESS, so it can be KILLED FROM THE SOCKET THREAD.
# Marty's first real run took **52 minutes** on a 266k-triangle bread and there
# was no way to stop it: the main thread is inside `_run_stage`, so a queued
# `quad_cancel` could not be served until the thing it was cancelling had
# finished. It lives here, beside the progress record, for the same reason and
# under the same rules — rebinding a module global is atomic under the GIL, and
# NOTHING that touches this may touch bpy.
_process = None
_cancelled = False
# The worker parks a finished job here; `_tick` picks it up on the main thread.
_pending = None
_last_result = None


def quad_cancel():
    """Stop the run in flight. **Answered off the main thread.**

    ⚠ NO bpy ACCESS HERE, EVER — server.py serves this before the main-thread
    queue, exactly like `quad_progress`, because the main thread is inside the
    engine and a cancel that waits for it is not a cancel.

    Killing the child is safe by construction: the engine only ever writes to
    its own temp directory, and the scene is not touched until a result has
    been read back. So a cancel can never leave a half-built object behind.
    """
    global _cancelled
    process = _process                          # one atomic read
    if process is None or process.poll() is not None:
        return {"ok": False, "cancelled": False, "reason": "nothing running"}
    _cancelled = True
    try:
        process.kill()
    except Exception as exc:                    # noqa: BLE001
        return {"ok": False, "cancelled": False, "reason": str(exc)}
    return {"ok": True, "cancelled": True}


def _run_stage(argv, cwd, log):
    """Run one engine program, reading its narration as it goes.

    ⚠ The return code is NOT a verdict — see the module docstring. It is
    recorded for the log and nothing branches on it.
    """
    global _process
    process = subprocess.Popen(argv, cwd=cwd, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, text=True,
                               encoding="utf-8", errors="replace",
                               creationflags=_NO_WINDOW)
    _process = process
    for line in process.stdout:
        log.append(line.rstrip("\n"))
        for marker, stage in _STAGE_MARKERS:
            if marker in line:
                _progress_stage(stage)
                break
        found = _UNSOLVED.search(line)
        if found:
            _progress_set(item="%s partitions left" % found.group(1))
    process.stdout.close()
    return process.wait()


def retopologize(object_name=None, density=1.0, sharp_angle=35.0,
                 use_sharp=True, preprocess=True, smoothing=True,
                 symmetry="", symmetry_offset=0.0, replace=False,
                 settings=None, scene=None, preserve=False,
                 fix_concave=True):
    """START a retopology. Returns as soon as the mesh is on disk.

    ⚠⚠ **THIS DOES NOT BLOCK, AND THAT IS THE WHOLE POINT.** It used to run the
    engine inline on Blender's main thread, which froze Blender for the length
    of the job — **52 minutes** on Marty's first real mesh, with the app's own
    request timing out at 30 and the work carrying on invisibly behind it.

    Only two steps of the pipeline need the main thread, and both are seconds:
    reading the evaluated mesh (here) and building the result object
    (`_tick`). Writing the OBJ, running the engine and parsing the result touch
    **no bpy at all**, so they belong on a worker thread — the same rule that
    lets `quad_progress` answer while Blender is busy.

    ⚠ The worker NEVER touches bpy and never builds anything. It parks its
    result in `_pending` and a **timer registered here, on the main thread**,
    picks it up. `bpy.app.timers.register` is not documented thread-safe, so
    the worker must not call it — that is why the timer starts now and polls,
    rather than being registered when the work finishes.
    """
    settings = settings or {}
    scene = scene or bpy.context.scene
    info = engine_info()
    if not info["ready"]:
        return {"ok": False, "error": "engine missing: %s"
                % ", ".join(info["missing"])}
    if _progress["active"]:
        return {"ok": False, "error": "a retopology is already running"}

    ob = bpy.data.objects.get(object_name) if object_name \
        else bpy.context.active_object
    if ob is None or ob.type != "MESH":
        return {"ok": False, "error": "select a mesh object"}

    _progress_begin(ob.name)
    # ⚠⚠ **WITH `preserve` ON, THE READ HAPPENS WITH THE DEFORM STACK OFF.**
    # `_evaluated_bmesh` reads the object as it RENDERS, so retopologising a
    # posed character bakes the pose into the geometry — and then copying the
    # Armature modifier back applies it a second time. Measured: as wrong as
    # having no armature at all, to four decimals. `rest_state` neutralises
    # exactly the modifiers that will be copied back, and records which, so
    # `_finish` copies what was really turned off rather than guessing again
    # minutes later. See `quadpreserve`.
    disabled, skipped = [], []
    matrix = ob.matrix_world.copy()
    matrix.translation = mathutils.Vector((0.0, 0.0, 0.0))
    try:
        with quadpreserve.rest_state(ob, enabled=bool(preserve)) as rest:
            # ⚠ The depsgraph is re-got INSIDE the rest state. Getting it
            # first hands back an evaluation of the stack we just switched off.
            bpy.context.view_layer.update()
            depsgraph = bpy.context.evaluated_depsgraph_get()
            bm = _evaluated_bmesh(ob, depsgraph)
            disabled = list(rest.disabled)
            skipped = list(rest.skipped)
        try:
            if symmetry:
                _bisect(bm, symmetry.lower(), symmetry_offset)
            bmesh.ops.triangulate(bm, faces=bm.faces[:])
            if not bm.faces:
                _progress_end()
                return {"ok": False, "error": "no faces to remesh"}
            work = os.path.join(bpy.app.tempdir,
                                "madi_quadify_%s" % uuid.uuid4().hex[:12])
            os.makedirs(work, exist_ok=True)
            source = os.path.join(work, "mesh.obj")
            verts_in, faces_in = _write_obj(bm, source)
        finally:
            bm.free()
    except Exception as exc:                    # noqa: BLE001
        _progress_end()
        return {"ok": False, "error": "could not read the mesh: %s" % exc}

    _write_prep_config(os.path.join(work, "prep.txt"), density,
                       sharp_angle if use_sharp else NO_SHARP, preprocess)
    _write_flow_config(os.path.join(work, "flow.txt"), density, settings)

    global _cancelled, _pending
    _cancelled = False
    _pending = None
    job = {"work": work, "info": info, "smoothing": smoothing,
           "source": ob.name, "symmetry": symmetry,
           "symmetry_offset": symmetry_offset, "replace": replace,
           "verts_in": verts_in, "faces_in": faces_in,
           "preserve": bool(preserve), "fix_concave": bool(fix_concave),
           "disabled": disabled,
           "skipped": skipped, "matrix": matrix,
           "started": time.time()}
    thread = threading.Thread(target=_worker, args=(job,), daemon=True)
    thread.start()
    bpy.app.timers.register(_tick, first_interval=0.5)
    return {"ok": True, "started": True, "object": ob.name,
            "verts_in": verts_in, "faces_in": faces_in, "work": work}


def _worker(job):
    """The long part, OFF Blender's main thread. ⚠ TOUCHES NO bpy, EVER."""
    log = []
    work, info = job["work"], job["info"]
    try:
        code1 = _run_stage([info["stage1"], os.path.join(work, "mesh.obj"),
                            _STAGE1_MODE, os.path.join(work, "prep.txt")],
                           info["root"], log)
        traced = os.path.join(work, "mesh_rem_p0.obj")
        if _cancelled:
            job["error"] = "cancelled"
        elif not os.path.exists(traced):
            job["error"] = "the engine stopped during tracing"
            job["exit"] = code1
        else:
            code2 = _run_stage([info["stage2"], traced, _STAGE2_MODE,
                                os.path.join(work, "flow.txt")],
                               info["root"], log)
            job["exit"] = code2
            if _cancelled:
                job["error"] = "cancelled"
            else:
                # ⚠ SUCCESS IS THIS FILE EXISTING, NOT the exit code — stage
                # two aborts on the way out of a perfectly good run. Fall back
                # to the unsmoothed mesh rather than fail: it is a real result
                # and refusing it would throw the whole run away.
                names = ["mesh_rem_p0_1_quadrangulation_smooth.obj",
                         "mesh_rem_p0_1_quadrangulation.obj"]
                if not job["smoothing"]:
                    names.reverse()
                found = next((os.path.join(work, n) for n in names
                              if os.path.exists(os.path.join(work, n))), None)
                if found is None:
                    job["error"] = "the engine produced no quad mesh"
                else:
                    verts, faces = _read_obj(found)
                    if not faces:
                        job["error"] = "the engine's result was empty"
                    else:
                        job["verts"] = verts
                        job["faces"] = faces
                        job["smoothed"] = found.endswith("_smooth.obj")
    except Exception as exc:                    # noqa: BLE001
        job["error"] = str(exc)
    job["log"] = log[-25:]
    job["seconds"] = round(time.time() - job["started"], 2)
    global _pending
    _pending = job                              # atomic rebind; _tick reads it


def _tick():
    """Main-thread pickup. Registered when the job starts, polls until done.

    Returning None unregisters the timer, so it costs nothing between runs.
    """
    job = _pending                              # one atomic read
    if job is None:
        return 0.5 if _progress["active"] else None
    try:
        _finish(job)
    finally:
        _progress_end()
    return None


def _finish(job):
    """Build the result in the scene. Main thread — this is the only part of
    the whole run that needs it."""
    global _last_result, _pending
    _pending = None
    if job.get("error"):
        _last_result = {"ok": False, "error": job["error"],
                        "log": job.get("log", []), "exit": job.get("exit"),
                        "seconds": job.get("seconds")}
        return
    # ⚠ Re-resolved BY NAME, because the job ran for as long as it liked and
    # the user was free to work the whole time — the object may be renamed or
    # gone. A stale reference here would be a crash, not a message.
    source = bpy.data.objects.get(job["source"])
    if source is None:
        _last_result = {"ok": False, "seconds": job.get("seconds"),
                        "error": "'%s' is gone — nothing to replace"
                                 % job["source"]}
        return
    faces = job["faces"]
    try:
        new_ob, preserved, split = _build_object(
            source, job["verts"], faces, job["symmetry"],
            job["symmetry_offset"], bpy.context.scene, job["replace"], job)
    except Exception as exc:                    # noqa: BLE001
        _last_result = {"ok": False, "error": "could not build the result: %s"
                        % exc, "seconds": job.get("seconds")}
        return
    # ⚠⚠ COUNTED OFF THE MESH THAT EXISTS, not off the engine's face list.
    # Splitting concave faces turns a quad into two triangles, so the two
    # disagree the moment that clean-up does anything - and this panel's whole
    # rule is that it reports what was MEASURED.
    built = new_ob.data.polygons
    sides = [len(p.vertices) for p in built]
    quads = sum(1 for n in sides if n == 4)
    total = len(sides) or 1
    _last_result = {
        "ok": True, "object": new_ob.name, "source": job["source"],
        "verts_in": job["verts_in"], "faces_in": job["faces_in"],
        "verts": len(new_ob.data.vertices), "faces": len(sides),
        "quads": quads,
        "tris": sum(1 for n in sides if n == 3),
        "ngons": sum(1 for n in sides if n > 4),
        "quad_pct": round(100.0 * quads / total, 1),
        "seconds": job.get("seconds"), "smoothed": job.get("smoothed"),
        "exit": job.get("exit"), "work": job["work"],
        "concave_split": split,
        "preserve": bool(job.get("preserve")), "preserved": preserved}


def quad_result():
    """The last finished run's report. The app polls `quad_progress` and asks
    for this once `active` goes false."""
    return dict(_last_result) if _last_result else {"ok": False,
                                                    "error": "no run yet"}


def _split_concave(mesh):
    """Split any concave face on the result, in place. Returns how many.

    ⚠⚠ **THIS IS WHAT MAKES THE RESULT USABLE AS A SURFACE DEFORM TARGET.**
    Blender's Surface Deform refuses a target that contains a concave face —
    Marty hit it as *"target contains concave polygons"* — and a quad remesh
    produces a handful around its singularities as a matter of course. A cage
    that cannot be bound to is not a cage.

    ⚠ **IT COSTS THE ALL-QUAD PROMISE, so the count is REPORTED, not hidden.**
    Splitting a concave quad leaves two triangles. On a real run it was 9 faces
    in 820; the report says how many so the number in the panel and the number
    in the mesh never disagree.

    ⚠ The test is the SIGN of the cross product against the face normal at each
    corner, which is what `connect_verts_concave` itself uses — not an angle
    threshold, because a face is either concave or it is not.
    """
    bm = bmesh.new()
    try:
        bm.from_mesh(mesh)
        concave = []
        for face in bm.faces:
            if len(face.verts) < 4:
                continue
            normal = face.normal
            corners = [v.co for v in face.verts]
            count = len(corners)
            for index in range(count):
                before = corners[index - 1]
                here = corners[index]
                after = corners[(index + 1) % count]
                if ((here - before).cross(after - here)).dot(normal) < -1e-9:
                    concave.append(face)
                    break
        if not concave:
            return 0
        bmesh.ops.connect_verts_concave(bm, faces=concave)
        bm.to_mesh(mesh)
        mesh.update()
        return len(concave)
    except Exception:                           # noqa: BLE001
        # A failed clean-up must not cost the retopology itself.
        return 0
    finally:
        bm.free()


def _morphed_base(ob):
    """The source's BASE topology in the shape its morphs currently give it.

    ⚠⚠ **THE WEIGHTS ARE INDEXED BY BASE VERTICES, BUT THE RETOPO FOLLOWS THE
    MORPHED SHAPE.** Since the shape keys bake in rather than transfer, those
    two stopped being the same mesh the moment a morph is dialled in — and
    binding one to the other samples every weight at the wrong place. That is
    silently wrong, not visibly broken, which is the worst kind.

    ⚠ Every modifier is switched off, not just the deform ones: shape keys are
    evaluated BEFORE modifiers, so with the stack off `to_mesh()` hands back
    base topology carrying the morphed shape. Blender's own evaluation, so
    relative keys, slider ranges and vertex-group masks are all honoured
    instead of being re-implemented here and drifting.

    Returns `None` when the object has no shape keys — nothing to correct for.
    """
    if ob.data.shape_keys is None:
        return None
    state = [(modifier, modifier.show_viewport) for modifier in ob.modifiers]
    try:
        for modifier, _ in state:
            modifier.show_viewport = False
        bpy.context.view_layer.update()
        depsgraph = bpy.context.evaluated_depsgraph_get()
        ob_eval = ob.evaluated_get(depsgraph)
        mesh = ob_eval.to_mesh()
        if len(mesh.vertices) != len(ob.data.vertices):
            # A modifier we could not switch off changed the topology; the
            # indexing no longer lines up, so the base mesh is the safer read.
            ob_eval.to_mesh_clear()
            return None
        coords = [tuple(vertex.co) for vertex in mesh.vertices]
        ob_eval.to_mesh_clear()
        return coords
    except Exception:                           # noqa: BLE001
        return None
    finally:
        for modifier, shown in state:
            try:
                modifier.show_viewport = shown
            except ReferenceError:
                pass
        bpy.context.view_layer.update()


def _build_object(source, verts, faces, symmetry, symmetry_offset, scene,
                  replace, job=None):
    """Turn the engine's vertices and faces into a real object beside the
    original: same collections, same transform, same parent.

    Returns `(new_ob, preserve_report)`. ⚠ **The preserve pass has to run
    before the `replace` branch**, because that branch deletes the very object
    everything is being sampled from.
    """
    mesh = bpy.data.meshes.new("%s_quads" % source.data.name)
    mesh.from_pydata(verts, [], faces)
    mesh.validate()
    mesh.update()

    split = 0
    if job is None or job.get("fix_concave", True):
        split = _split_concave(mesh)

    new_ob = bpy.data.objects.new("%s_quads" % source.name, mesh)
    for collection in source.users_collection:
        collection.objects.link(new_ob)
    if not source.users_collection:
        scene.collection.objects.link(new_ob)

    # Only the location goes back on: rotation and scale are baked into the
    # geometry, so re-applying them would apply them twice.
    new_ob.location = source.location
    new_ob.parent = source.parent
    new_ob.matrix_parent_inverse = source.matrix_parent_inverse.copy()

    if symmetry:
        mirror = new_ob.modifiers.new("MADI Mirror", "MIRROR")
        mirror.use_axis[0] = "x" in symmetry.lower()
        mirror.use_axis[1] = "y" in symmetry.lower()
        mirror.use_axis[2] = "z" in symmetry.lower()

    preserved = None
    if job is not None and job.get("preserve"):
        try:
            preserved = quadpreserve.preserve(
                source, new_ob, job.get("matrix") or source.matrix_world,
                disabled=job.get("disabled") or (),
                skipped=job.get("skipped") or (),
                source_coords=_morphed_base(source))
        except Exception as exc:            # noqa: BLE001
            # ⚠ A failed transfer must not cost the retopology. The mesh is
            # the thing that took minutes; the rig data can be redone.
            preserved = {"ok": False, "error": str(exc)}

    if replace:
        bpy.data.objects.remove(source, do_unlink=True)
    else:
        source.hide_set(True)
        source.hide_render = True
    return new_ob, preserved, split
