"""Carry a rig's data onto a retopologised mesh. See docs\\quadify.md.

Marty, 2026-08-21: *"preserves everything that is preserved when separating by
loose parts ... goal is to make it move and behave like the original mesh"*,
then, after seeing resampled morphs tear a real character: *"shapekeys should be
baked how they are in the frame we select ... instead of transfering shapekeys
... but do transfer the rest of the data"* — which is how Quad Remesher works.

⚠⚠ **SEPARATE PRESERVES EVERYTHING BECAUSE THE VERTICES ARE THE SAME ONES.**
Measured on 5.2: `mesh.separate` carries vertex groups **with their weights**,
shape keys, **shape-key drivers**, UVs, materials, the Armature modifier and its
target, constraints, the parent, custom properties, the object's action and its
collections — nothing is lost, because the new object keeps the very vertices
the data was indexed by. A retopology throws every vertex away. So the job
splits in two and the halves are not alike:

* **Copy** — anything that does not name a vertex: the modifier stack,
  constraints, materials, custom properties, animation. Literal, lossless.
* **Resample** — anything that does: weights, and material slots per face.
  There is no lossless version of this and pretending otherwise is how a rig
  ships with quietly wrong weights.
* **Bake** — shape keys. They are NOT resampled: the read leaves them at their
  current values, so the geometry that comes back already is the shape on
  screen and the result carries no keys. See the tombstone further down for the
  measurements that killed the resampling version.

THE MAPPING, AND WHY IT IS SOLVED ONCE

Every resampled channel uses the same bind: for each new vertex, the nearest
point on the source's triangulated surface facing the same way, held as
**clamped barycentric weights on that triangle plus a signed depth along its
normal**. It is solved once and reused, and it is BOUNDED by construction —
`_coefficients` explains why that matters and what the unbounded version did.

⚠⚠ **VALIDATED AGAINST A CLOSED FORM, NOT AGAINST ANOTHER IMPLEMENTATION.**
A uniform 1.25x scale has a known right answer. Comparing two implementations
only ever tells you they differ — the first draft was checked against a Surface
Deform bake and **the bake was the one that was wrong**. ⚠ Do not reach for
`object.surfacedeform_bind` as an oracle here: it did not follow the source's
shape keys under `blender -b`, and the disagreement looked exactly like our bug.

⚠⚠ **A SELF-CHECK OF THE BIND PROVES NOTHING.** Rebuilding the target from its
own basis is exact even when the triangle list is garbage, because any
non-degenerate basis can express any residual. The first draft scored a perfect
0.0 on that check with the mapping pointing at the wrong vertices.

WHAT IS READ, AND IN WHICH STATE

⚠⚠ **THE READ MUST NEUTRALISE THE DEFORM STACK OR EVERYTHING BELOW IS WRONG.**
`quadify._evaluated_bmesh` reads the object **as it renders** — modifiers and
shape keys applied. Retopologise a posed character, copy its Armature modifier
onto the result, and the pose is applied twice. Measured: the double-applied
mesh is off by **3.5115** units — the *same* error, to four decimals, as having
no armature at all, which is not a coincidence but an identity (for a rigid
bone rotation |P(v) - P(P(v))| = |v - P(v)|). So `rest_state` turns the deform
modifiers off for the duration of the read. ⚠ It does NOT touch the shape
keys — those are meant to bake in.

⚠ **It disables the MODIFIER, never `armature.data.pose_position`.** The pose
position lives on armature *data*, which other meshes and other scenes share;
`show_viewport` is per-modifier and touches nothing but this object.

⚠ **Only the neutralised modifiers are copied back.** Everything else in the
stack — Subsurf, Solidify, a geometry-nodes deformer — really did run and is
baked into the geometry that came back, so copying it would apply it a second
time. The report NAMES what was left behind rather than dropping it silently.
"""

import numpy as np

# ⚠ No `import bpy` here, and it is worth keeping that way: nothing in this
# module reaches for the context or the scene. It is handed the two objects and
# a matrix, which is what makes the whole transfer testable against a stand-in
# mesh without an engine run behind it.

# Modifiers that only move vertices. These are switched OFF for the read (so
# their effect is not baked into the retopology) and copied onto the result
# afterwards, which is what makes it deform like the original.
#
# ⚠ NODES is deliberately absent. A geometry-nodes modifier may generate as
# easily as it deforms — Marty's own Softbody Pro is one — so it is treated as
# generative: left running for the read, not copied, and named in the report.
DEFORM_TYPES = frozenset((
    "ARMATURE", "CAST", "CURVE", "DISPLACE", "HOOK", "LAPLACIANDEFORM",
    "LAPLACIANSMOOTH", "LATTICE", "MESH_DEFORM", "SHRINKWRAP", "SIMPLE_DEFORM",
    "SMOOTH", "CORRECTIVE_SMOOTH", "SURFACE_DEFORM", "WARP", "WAVE",
))

# Below this a transferred weight is noise, and writing it would turn a sparse
# group into a dense one on every vertex the mesh has.
WEIGHT_EPSILON = 1e-5

# How much further than the nearest triangle the bind may look for one that
# actually FACES the same way. 4x is enough to cross the gap in a fold on a
# retopo that follows the surface, and small enough that it never wanders onto
# an unrelated part of the body. See `bind`.
SHEET_SLACK = 4.0


# ---------------------------------------------------------------------------
# The read state
# ---------------------------------------------------------------------------

class rest_state(object):
    """Switch the deform stack off for the read, then put it back.

    ⚠ **Shape keys are deliberately NOT touched** — they are meant to bake
    into the result at their current values. Only what gets COPIED onto the
    result is neutralised, because only that can be applied twice.

    ⚠ **The restore is in `__exit__`, so it survives an exception in the read.**
    Leaving somebody's character with its armature switched off because a mesh
    failed to triangulate would look exactly like data loss.
    """

    def __init__(self, ob, enabled=True):
        self.ob = ob
        self.enabled = enabled
        self.disabled = []              # modifier names, in stack order
        self.skipped = []               # (name, type) left running
        self._modifiers = []

    def __enter__(self):
        if not self.enabled:
            return self
        for modifier in self.ob.modifiers:
            if modifier.type in DEFORM_TYPES:
                self._modifiers.append((modifier, modifier.show_viewport))
                self.disabled.append(modifier.name)
                modifier.show_viewport = False
            elif modifier.show_viewport:
                self.skipped.append((modifier.name, modifier.type))
        # ⚠⚠ **THE SHAPE KEYS ARE LEFT ALONE, AND THAT IS THE WHOLE DESIGN.**
        # Marty, 2026-08-21: *"shapekeys should be baked how they are in the
        # frame we select ... instead of transfering shapekeys ... but do
        # transfer the rest of the data"* — which is how Quad Remesher works.
        # So the morphs stay at whatever the current frame gives, get evaluated
        # into the mesh the engine sees, and the result simply IS that shape.
        # Only the deform stack above is neutralised, because THAT is what gets
        # copied onto the result and would otherwise apply twice.
        return self

    def __exit__(self, exc_type, exc, tb):
        for modifier, shown in self._modifiers:
            try:
                modifier.show_viewport = shown
            except ReferenceError:      # the user deleted it mid-read
                pass
        self._modifiers = []
        return False


# ---------------------------------------------------------------------------
# The bind
# ---------------------------------------------------------------------------

def _source_arrays(source, matrix, coords_override=None):
    """The source's BASE mesh as (coords, tris, tri_polygon), in the retopo's
    space.

    ⚠ **The BASE topology, because that is what VERTEX WEIGHTS are indexed
    by.** `vertex.groups` has one entry per base vertex; sampling a subdivided
    surface would align with nothing. When the source has a Subsurf the retopo
    therefore follows the smooth surface while the weights are sampled off the
    cage under it — an approximation, and named as one in the report.

    ⚠⚠ **BUT ITS SHAPE MUST BE THE MORPHED ONE, WHICH IS WHY
    `coords_override` EXISTS.** Since 2026-08-21 the shape keys are BAKED into
    the retopology rather than transferred, so the mesh the engine saw is the
    *morphed* shape while `source.data.vertices` is still the unmorphed base.
    Binding one to the other samples every weight at the wrong place, and on a
    live morph that is silently wrong rather than visibly broken. The caller
    hands in base-topology coordinates with the morphs applied — Blender's own
    evaluation with every modifier switched off, which keeps the vertex
    indexing while giving the shape actually on screen.

    ⚠ `matrix` is `matrix_world` with the translation stripped, exactly what
    `_evaluated_bmesh` bakes into the result, so both sides land in one space.
    """
    mesh = source.data
    count = len(mesh.vertices)
    if coords_override is not None and len(coords_override) == count:
        coords = np.asarray(coords_override, dtype=np.float64)
    else:
        coords = np.empty(count * 3, dtype=np.float64)
        mesh.vertices.foreach_get("co", coords)
        coords = coords.reshape(count, 3)

    rotation = np.array(matrix.to_3x3(), dtype=np.float64)
    coords = coords @ rotation.T

    mesh.calc_loop_triangles()
    tris = np.empty(len(mesh.loop_triangles) * 3, dtype=np.int32)
    mesh.loop_triangles.foreach_get("vertices", tris)
    tris = tris.reshape(len(mesh.loop_triangles), 3)
    polygon = np.empty(len(mesh.loop_triangles), dtype=np.int32)
    mesh.loop_triangles.foreach_get("polygon_index", polygon)
    return coords, tris, polygon, rotation


def _vertex_normals(mesh, count):
    """Per-vertex normals as an (N, 3) array.

    ⚠ Read off `mesh.vertex_normals`, which Blender computes and caches, rather
    than summed by hand — a hand-rolled average has to decide about area
    weighting and sharp edges, and getting that subtly wrong here would put
    vertices on the wrong sheet of a fold, which is the exact bug this feeds.
    """
    normals = np.zeros((count, 3), dtype=np.float64)
    try:
        flat = np.empty(count * 3, dtype=np.float64)
        mesh.vertex_normals.foreach_get("vector", flat)
        normals = flat.reshape(count, 3)
    except (AttributeError, TypeError, ValueError, RuntimeError):
        return None
    lengths = np.linalg.norm(normals, axis=1, keepdims=True)
    lengths[lengths == 0.0] = 1.0
    return normals / lengths


def _frames(coords, tris):
    """Per-triangle origin, two edge vectors and a UNIT normal.

    ⚠⚠ **UNIT, deliberately, and it was `n/sqrt(|n|)` for one wrong hour.**
    Scaling the normal with the triangle makes the off-surface residual scale
    with the triangle's area too — and in a fold that opens, triangle areas
    change by **1.4x to 4.3x** (measured), so the residual gets multiplied by
    up to two. The residual is a retopology's distance from the surface it
    approximates; it should be carried across unchanged, not amplified by
    whatever the morph does to the local area.

    ⚠ The bind only ever tests the SIGN of a dot product against this, so its
    magnitude never decides which sheet is chosen.
    """
    p0 = coords[tris[:, 0]]
    e1 = coords[tris[:, 1]] - p0
    e2 = coords[tris[:, 2]] - p0
    normal = np.cross(e1, e2)
    length = np.linalg.norm(normal, axis=1, keepdims=True)
    length[length == 0.0] = 1.0         # a degenerate triangle must not divide
    return p0, e1, e2, normal / length


def bind(target_co, coords, tris, target_no=None, slack=SHEET_SLACK):
    """Map every target vertex onto the source surface. Solved ONCE.

    Returns `(hit, coeffs, unsure)` — the triangle each vertex landed on, an
    (N, 4) array of three clamped barycentric weights plus a signed depth along
    the unit normal, and how many vertices could not be placed on a
    correctly-facing sheet. The weights are what the vertex-group and material
    transfers read; the depth is what gives the shape keys their thickness back.
    See `_coefficients` for why it is not a plain frame solve.

    ⚠⚠ **`target_no` IS NOT AN OPTIMISATION, IT IS THE FIX FOR FOLDED SURFACES.**
    The nearest triangle in 3D is not necessarily on the same SHEET of the
    surface. Wherever a body folds back on itself — an anus, a mouth, an
    armpit, between fingers — a vertex just outside the fold has its nearest
    neighbour on the sheet facing it, not the one it belongs to. Measured on
    Marty's Hinako mesh, 2026-08-21: **7 062 vertex pairs** sit within 3 mm of
    each other and move **42 mm apart** under `Anus_Open2`, a 14x divergence.
    Bind to the wrong sheet there and the morph drags the vertex the wrong way
    across the gap — which is exactly what he saw ("shapekeys break the
    remeshed part").

    ⚠ **This is why WEIGHTS looked fine while shape keys tore.** A weight is a
    smooth scalar and both sheets of a fold carry nearly the same one, so
    picking the wrong sheet costs nothing. A shape-key offset is a VECTOR that
    points in opposite directions on the two sheets, so the same wrong pick is
    the whole failure. **A transfer that passes on scalars has not been tested.**

    So candidates are gathered in a radius and the nearest one whose normal
    AGREES with the target's own is taken. Distance alone only decides between
    triangles already on the right side.
    """
    from mathutils import Vector
    from mathutils.bvhtree import BVHTree

    tree = BVHTree.FromPolygons([Vector(c) for c in coords],
                                [tuple(int(i) for i in t) for t in tris])
    p0, e1, e2, normal = _frames(coords, tris)

    # A floor for the search radius, so a vertex sitting exactly ON the surface
    # still gets to look at its neighbours rather than searching a sphere of
    # radius zero.
    span = float(np.linalg.norm(coords.max(axis=0) - coords.min(axis=0)))
    floor = max(span * 1e-4, 1e-9)

    hit = np.zeros(len(target_co), dtype=np.int64)
    spot = np.array(target_co, dtype=np.float64)
    unsure = 0
    for index, co in enumerate(target_co):
        point = Vector(co)
        found = tree.find_nearest(point)
        if found is None or found[2] is None:
            continue
        best, place = found[2], found[0]
        if target_no is not None:
            want = target_no[index]
            radius = max(float(found[3]) * slack, floor)
            closest = None
            for candidate in tree.find_nearest_range(point, radius):
                where = candidate[2]
                if where is None:
                    continue
                if float(np.dot(normal[where], want)) <= 0.0:
                    continue
                # ⚠⚠ **THE NEAREST AGREEING ONE, NOT THE FIRST.**
                # `find_nearest_range` does NOT come back nearest-first — it
                # returns BVH traversal order. Measured on Marty's mesh: only
                # **2 of 123** samples happened to be sorted, an example being
                # [0.0055, 0.0026, 0.0027, 0.0027, 0.0095, ...]. Taking the
                # first agreeing hit therefore picked a near-random triangle
                # within the radius, changed 813 of 867 binds, and made the
                # transfer WORSE than plain distance (237 badly-placed
                # vertices against 107). The docstring said "nearest-first";
                # nothing had checked it.
                if closest is None or candidate[3] < closest[0]:
                    closest = (candidate[3], where, candidate[0])
            if closest is not None:
                best, place = closest[1], closest[2]
            else:
                # Nothing faces the right way within the radius. Keep the plain
                # nearest rather than dropping the vertex, and SAY SO.
                unsure += 1
        hit[index] = best
        spot[index] = place

    return hit, _coefficients(target_co, spot, coords, tris, hit), unsure


def _coefficients(target_co, spot, coords, tris, hit):
    """Turn each vertex into weights on its triangle plus a depth off it.

    ⚠⚠ **BOUNDED BY CONSTRUCTION, AND THAT IS THE POINT.** The first version
    solved `v = p0 + a*e1 + b*e2 + c*n` for (a, b, c). On a SLIVER triangle —
    and a triangulated Daz mesh is full of them — `e1` and `e2` are nearly
    parallel, the basis is ill-conditioned, and the solve hands back enormous
    numbers: measured on Marty's Hinako mesh, **a = -17.9 and b = 8.9 for a
    point lying ON its own triangle**. Those coefficients then multiply the
    DEFORMED edges, and a vertex that should have moved 0.02 moved **0.0756,
    2.8x further than the entire morph's travel**. That is what "shapekeys
    break the remeshed part" looks like from the inside.

    Barycentric weights of the nearest point cannot do that. They are clamped
    into the simplex, so the surface part of the reconstruction is always
    inside its triangle no matter how badly shaped that triangle is, and the
    depth is a plain signed distance along the unit normal. An ill-conditioned
    triangle now costs a little accuracy instead of an explosion.

    ⚠ It is the CLAMP that provides the guarantee, not the formula — the
    barycentric formula divides by the same near-zero determinant.
    """
    p0, e1, e2, normal = _frames(coords, tris)
    origin = p0[hit]
    edge1, edge2 = e1[hit], e2[hit]
    unit = normal[hit]

    relative = spot - origin
    d00 = np.einsum("ij,ij->i", edge1, edge1)
    d01 = np.einsum("ij,ij->i", edge1, edge2)
    d11 = np.einsum("ij,ij->i", edge2, edge2)
    d20 = np.einsum("ij,ij->i", relative, edge1)
    d21 = np.einsum("ij,ij->i", relative, edge2)
    denominator = d00 * d11 - d01 * d01
    safe = np.where(np.abs(denominator) < 1e-24, 1.0, denominator)
    beta = (d11 * d20 - d01 * d21) / safe
    gamma = (d00 * d21 - d01 * d20) / safe
    beta = np.where(np.abs(denominator) < 1e-24, 0.0, beta)
    gamma = np.where(np.abs(denominator) < 1e-24, 0.0, gamma)

    weights = np.stack((1.0 - beta - gamma, beta, gamma), axis=1)
    np.clip(weights, 0.0, 1.0, out=weights)
    total = weights.sum(axis=1, keepdims=True)
    total[total == 0.0] = 1.0
    weights /= total

    # Everything the weights could not express — the distance off the surface,
    # and whatever tangential slop the clamp removed — as one signed depth.
    surface = (weights[:, 0:1] * origin
               + weights[:, 1:2] * (origin + edge1)
               + weights[:, 2:3] * (origin + edge2))
    depth = np.einsum("ij,ij->i", target_co - surface, unit)
    return np.concatenate((weights, depth[:, None]), axis=1)


def evaluate(coords, tris, hit, coeffs):
    """Rebuild the target positions from one state of the source.

    `coeffs` is (N, 4): three barycentric weights and a signed depth. The
    surface term is a convex combination of the triangle's own corners, so it
    can never leave the triangle however the source deforms; only the depth
    term reaches off the surface, and it is a distance the retopology actually
    had.
    """
    p0, e1, e2, normal = _frames(coords, tris)
    origin = p0[hit]
    surface = (coeffs[:, 0:1] * origin
               + coeffs[:, 1:2] * (origin + e1[hit])
               + coeffs[:, 2:3] * (origin + e2[hit]))
    return surface + coeffs[:, 3:4] * normal[hit]


def barycentric(coeffs):
    """The three surface weights out of a bind, already clamped and summing to
    one. Kept as a function because the weight and material transfers speak in
    those terms, and because it used to do real work (see `_coefficients`)."""
    return coeffs[:, :3]


# ---------------------------------------------------------------------------
# Copying what does not name a vertex
# ---------------------------------------------------------------------------

# Never carried across: identity, the stack position, and the bind data of a
# Surface Deform (it names vertices of a mesh that no longer exists).
_SKIP_PROPS = frozenset(("rna_type", "name", "type", "is_active", "is_bound",
                         "is_override_data", "persistent_uid"))


def _copy_settings(source, target):
    """Every writable RNA property, then the IDProperties underneath.

    Used for both modifiers and constraints — they are the same shape of
    problem, and a generic walk beats a per-type table that goes stale the
    first time Blender adds a field.
    """
    for prop in source.bl_rna.properties:
        name = prop.identifier
        if name in _SKIP_PROPS or prop.is_readonly:
            continue
        try:
            setattr(target, name, getattr(source, name))
        except (AttributeError, TypeError, ValueError):
            # A property that refuses is one this pairing does not have;
            # losing it beats losing the whole modifier.
            continue
    try:
        items = list(source.keys())
    except TypeError:
        # ⚠ Not every bpy_struct carries IDProperties - a plain Modifier
        # raises "this type doesn't support IDProperties" rather than
        # returning an empty list. Only NODES modifiers really need this.
        items = []
    for key in items:
        try:
            target[key] = source[key]
        except (TypeError, ValueError):
            continue


def copy_modifiers(source, new_ob, names):
    """Put the neutralised deform modifiers back, in their original order.

    ⚠ **Only the ones `rest_state` switched off.** Everything else in the
    source's stack really ran and is already baked into the geometry that came
    back from the engine — copying it would apply it twice.

    ⚠ A Mirror added for symmetry is already on the result, and it must stay
    FIRST: mirroring after an Armature deform mirrors the deformed shape.
    """
    wanted = [m for m in source.modifiers if m.name in set(names)]
    copied = []
    for modifier in wanted:
        made = new_ob.modifiers.new(modifier.name, modifier.type)
        if made is None:
            continue
        _copy_settings(modifier, made)
        copied.append(made.name)
    return copied


def copy_constraints(source, new_ob):
    copied = []
    for constraint in source.constraints:
        made = new_ob.constraints.new(constraint.type)
        made.name = constraint.name
        _copy_settings(constraint, made)
        copied.append(made.name)
    return copied


def copy_custom_props(source, new_ob):
    """Object-level custom properties, and the UI data that goes with them.

    ⚠ `_RNA_UI` and the modern `id_properties_ui` both matter — a Daz morph
    driver reads min/max off them, and a property that arrives without its
    range silently clamps somewhere else.
    """
    copied = []
    for key in source.keys():
        if key in ("_RNA_UI", "cycles"):
            continue
        try:
            new_ob[key] = source[key]
        except (TypeError, ValueError):
            continue
        copied.append(key)
        try:
            spec = source.id_properties_ui(key).as_dict()
            new_ob.id_properties_ui(key).update(**spec)
        except (TypeError, AttributeError, KeyError):
            pass
    return copied


def copy_animation(source, new_ob):
    """Share the object's action and copy its object-level drivers.

    The action is SHARED rather than duplicated: `separate` copies it, but a
    copy is one more thing to keep in sync by hand, and nothing about a retopo
    wants a second identical action in the file.
    """
    animation = source.animation_data
    if animation is None:
        return {"action": None, "drivers": 0}
    made = new_ob.animation_data_create()
    made.action = animation.action
    count = _copy_drivers(animation, made, lambda path: True)
    return {"action": animation.action.name if animation.action else None,
            "drivers": count}


def _copy_drivers(from_anim, to_anim, keep):
    """Copy driver F-curves whose data path `keep` accepts.

    ⚠ **The driver, its variables AND its targets** — a driver copied without
    its variables evaluates to zero and looks like a rig that half works.
    """
    count = 0
    for fcurve in from_anim.drivers:
        if not keep(fcurve.data_path):
            continue
        try:
            made = to_anim.drivers.new(fcurve.data_path, index=fcurve.array_index)
        except (RuntimeError, TypeError):
            continue
        made.extrapolation = fcurve.extrapolation
        made.hide = fcurve.hide
        made.mute = fcurve.mute
        driver, original = made.driver, fcurve.driver
        driver.type = original.type
        driver.expression = original.expression
        driver.use_self = original.use_self
        for variable in list(driver.variables):
            driver.variables.remove(variable)
        for source_var in original.variables:
            variable = driver.variables.new()
            variable.name = source_var.name
            variable.type = source_var.type
            for index, source_target in enumerate(source_var.targets):
                target = variable.targets[index]
                if source_var.type == "SINGLE_PROP":
                    target.id_type = source_target.id_type
                target.id = source_target.id
                target.bone_target = source_target.bone_target
                target.data_path = source_target.data_path
                target.transform_type = source_target.transform_type
                target.transform_space = source_target.transform_space
                target.rotation_mode = source_target.rotation_mode
        # A driver curve keeps its modifiers and points; without them a
        # "generator" driver reads flat.
        for point in fcurve.keyframe_points:
            made.keyframe_points.insert(point.co[0], point.co[1])
        count += 1
    return count


# ---------------------------------------------------------------------------
# Resampling what does name a vertex
# ---------------------------------------------------------------------------

def _weight_arrays(source):
    """Every vertex group as a dense per-vertex array, in ONE pass.

    ⚠ **One pass over the vertices, not one pass per group.** A Daz figure
    carries a few hundred groups over tens of thousands of vertices; walking
    the mesh once per group is that product, and it is the difference between
    a second and several minutes.
    """
    count = len(source.data.vertices)
    arrays = {}
    for group in source.vertex_groups:
        arrays[group.index] = np.zeros(count, dtype=np.float64)
    for vertex in source.data.vertices:
        for entry in vertex.groups:
            array = arrays.get(entry.group)
            if array is not None:
                array[vertex.index] = entry.weight
    return arrays


def transfer_weights(source, new_ob, tris, hit, coeffs):
    """Barycentric-interpolate every vertex group onto the result.

    ⚠ **This is an ESTIMATE and the UI has to say so.** `core.apply_vgroups`
    has carried the same warning since 2026-08-04 for the same reason: weights
    that look plausible and are wrong are worse than weights that are missing,
    because nobody goes looking for them. On a character this always wants a
    cleanup pass.
    """
    arrays = _weight_arrays(source)
    if not arrays:
        return {"groups": 0, "weights": 0}
    weights = barycentric(coeffs)
    corners = tris[hit]
    written = 0
    for group in source.vertex_groups:
        array = arrays.get(group.index)
        if array is None:
            continue
        sampled = (weights[:, 0] * array[corners[:, 0]]
                   + weights[:, 1] * array[corners[:, 1]]
                   + weights[:, 2] * array[corners[:, 2]])
        made = new_ob.vertex_groups.get(group.name)
        if made is None:
            made = new_ob.vertex_groups.new(name=group.name)
        made.lock_weight = group.lock_weight
        # Only what is actually IN the group. Writing the zeros would turn a
        # sparse group into a dense one on every vertex the mesh has.
        for index in np.nonzero(sampled > WEIGHT_EPSILON)[0]:
            made.add([int(index)], float(sampled[index]), "REPLACE")
            written += 1
    return {"groups": len(arrays), "weights": written}


# ⚠⚠ `transfer_shape_keys` WAS HERE, AND IT WAS REMOVED ON PURPOSE
# (2026-08-21). It resampled every morph onto the new topology through the same
# bind the weights use. It worked on smooth geometry and tore on Marty's real
# character - "shapekeys 'Anus_Open2' mainly, breaks the remeshed part".
#
# The measurements, kept because they are the argument against rebuilding it:
#   * The mesh FOLDS back on itself. 7 062 vertex pairs sit within 3 mm of each
#     other and move 42 mm APART under that one morph - a 14x divergence. A
#     bind picked by proximity cannot know which sheet it is on.
#   * Weights survived that and morphs did not, because a weight is a smooth
#     scalar both sheets share, while a morph offset is a VECTOR pointing
#     opposite ways on the two sides. A transfer proved on scalars is not
#     proved.
#   * Two rounds of fixes (normal-aware sheet choice, then a bounded
#     barycentric map replacing an unbounded frame solve) took the worst vertex
#     from 2.8x the morph's own travel down to under it - real progress, and
#     still only "close".
#
# Marty's call was to stop resampling morphs at all and BAKE them, the way Quad
# Remesher does: the read leaves the keys at their current values, so the
# result already is the shape on screen. Everything that does NOT name a vertex
# is still copied. The bind survives for weights and materials, where being
# close is genuinely good enough.

def transfer_materials(source, new_ob, coords, tris, polygon, matrix):
    """Copy the material slots, and put each new face on the right one.

    ⚠ **Slots alone are not enough on a character.** Copying the list and
    leaving every face on slot 0 puts the whole body on the eyelash material
    if that happens to be first. So the faces get their own nearest-surface
    lookup — a second, cheap BVH pass over face CENTRES, because a face's
    material is a face question and the vertex bind cannot answer it.
    """
    slots = [slot.material for slot in source.material_slots]
    for material in slots:
        new_ob.data.materials.append(material)
    if len(slots) < 2:
        return {"slots": len(slots), "faces": 0}

    mesh = new_ob.data
    faces = len(mesh.polygons)
    centres = np.empty(faces * 3, dtype=np.float64)
    mesh.polygons.foreach_get("center", centres)
    # ⚠ Face NORMALS here for the same reason vertex normals are used for the
    # shape keys: in a fold the nearest face is often the one facing back at
    # you, and on a character that is a different material.
    face_no = np.empty(faces * 3, dtype=np.float64)
    mesh.polygons.foreach_get("normal", face_no)
    hit_face, _, _ = bind(centres.reshape(faces, 3), coords, tris,
                          target_no=face_no.reshape(faces, 3))

    source_index = np.empty(len(source.data.polygons), dtype=np.int32)
    source.data.polygons.foreach_get("material_index", source_index)
    mapped = source_index[polygon[hit_face]].astype(np.int32)
    mesh.polygons.foreach_set("material_index", mapped)
    mesh.update()
    return {"slots": len(slots), "faces": int(faces)}


# ---------------------------------------------------------------------------
# The whole job
# ---------------------------------------------------------------------------

def preserve(source, new_ob, matrix, disabled=(), skipped=(),
             source_coords=None):
    """Carry everything carryable from `source` onto `new_ob`.

    `matrix` is `matrix_world` with the translation stripped — the same one
    `quadify._evaluated_bmesh` baked into the result, so the source's own
    coordinates have to go through it before either side can be compared.

    `disabled` is the list of deform modifiers `rest_state` switched off for
    the read, recorded at read time rather than recomputed here: the run may
    have taken minutes and the user was free to edit the stack the whole time.

    Returns a report. ⚠ **It says what was done, never what was attempted** —
    the same rule the retopology report itself lives by.
    """
    report = {"ok": True, "groups": 0, "weights": 0, "keys": 0, "drivers": 0,
              "modifiers": [], "constraints": [], "props": [], "slots": 0,
              "skipped": ["%s (%s)" % (name, kind) for name, kind in skipped],
              "notes": []}

    mesh = new_ob.data
    count = len(mesh.vertices)
    if not count or not len(source.data.vertices):
        report["ok"] = False
        report["notes"].append("nothing to sample from")
        return report

    target_co = np.empty(count * 3, dtype=np.float64)
    mesh.vertices.foreach_get("co", target_co)
    target_co = target_co.reshape(count, 3)
    target_no = _vertex_normals(mesh, count)

    coords, tris, polygon, rotation = _source_arrays(source, matrix,
                                                     source_coords)
    if not len(tris):
        report["ok"] = False
        report["notes"].append("the original has no faces to sample from")
        return report
    hit, coeffs, unsure = bind(target_co, coords, tris,
                               target_no=target_no)
    report["unsure"] = unsure
    if unsure:
        # ⚠ Say it. These are the vertices most likely to fly off under a
        # morph, and the user is the only one who can look at them.
        report["notes"].append(
            "%d of %d vertices had no matching surface facing them and were "
            "placed on the nearest one instead - check those areas under a "
            "large morph" % (unsure, count))

    weights = transfer_weights(source, new_ob, tris, hit, coeffs)
    report.update(weights)

    # ⚠⚠ **SHAPE KEYS ARE NOT TRANSFERRED — THEY ARE BAKED.** Marty's call on
    # 2026-08-21, after resampling them tore a morphed mesh apart: the read
    # leaves the morphs at their current values, so the geometry that comes
    # back already IS the shape you were looking at, and the result carries no
    # keys at all. Quad Remesher does the same. A resampled morph has to be
    # bounded, smooth AND right, and the last of those is not achievable on a
    # surface that folds — `docs\\quadify.md` has the measurements.
    keys = source.data.shape_keys
    report["keys"] = 0
    report["baked_keys"] = 0 if keys is None else max(
        0, len(keys.key_blocks) - 1)
    if report["baked_keys"]:
        report["notes"].append(
            "%d shape keys were BAKED into the result at their current values "
            "- the new mesh has no shape keys of its own"
            % report["baked_keys"])

    materials = transfer_materials(source, new_ob, coords, tris, polygon,
                                   matrix)
    report["slots"] = materials["slots"]

    report["modifiers"] = copy_modifiers(source, new_ob, disabled)
    report["constraints"] = copy_constraints(source, new_ob)
    report["props"] = copy_custom_props(source, new_ob)
    animation = copy_animation(source, new_ob)
    report["drivers"] += animation["drivers"]
    report["action"] = animation["action"]

    # ⚠ A Mirror put on for symmetry has to stay FIRST. Mirroring after an
    # Armature deform mirrors the DEFORMED shape, which on a character looks
    # like the far half lagging a frame behind the near one.
    for index, modifier in enumerate(new_ob.modifiers):
        if modifier.type == "MIRROR" and index:
            for _ in range(index):
                new_ob.modifiers.move(new_ob.modifiers.find(modifier.name),
                                      new_ob.modifiers.find(modifier.name) - 1)
            break

    if report["groups"] and not report["modifiers"]:
        report["notes"].append(
            "weights were transferred but the original had no deform "
            "modifier to copy - nothing will move them")
    if any(kind in ("SUBSURF", "MULTIRES") for _, kind in skipped):
        report["notes"].append(
            "the original was subdivided, so the shape keys were sampled off "
            "the cage under that surface - large morphs will be softer")
    return report
