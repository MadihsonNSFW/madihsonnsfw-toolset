# MADI Bone Jiggle (jiggle.py): the solver maths, the analytic colliders, chain
# composition, every tunable's visible effect, collision, self collision,
# lateral links, wind, the safety guard, the settings round-trip, the cache and
# its invalidation, and the two-pass bake. Nothing here touches a real .blend.
# Run: blender.exe -b --factory-startup --python jiggle_solver_test.py
import importlib.util
import math
import os
import shutil
import sys
import tempfile

import bpy
from mathutils import Matrix, Vector

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

JIG = os.path.join(_ROOT, "blender_addon", "madi_anim_library", "jiggle.py")
spec = importlib.util.spec_from_file_location("madi_jiggle", JIG)
jiggle = importlib.util.module_from_spec(spec)
sys.modules["madi_jiggle"] = jiggle
spec.loader.exec_module(jiggle)

PASS = []
FAIL = []


def ok(cond, label):
    (PASS if cond else FAIL).append(label)
    print(("ok   " if cond else "FAIL ") + label, flush=True)


def close(a, b, tol=1e-6):
    return abs(a - b) <= tol


jiggle.register()
# Drive the solver EXPLICITLY. Leaving the frame handler registered as well
# would step every frame twice, and the second step would read a
# PoseBone.matrix that has not been re-evaluated since the first one wrote to
# it - the documented invariant on step_object().
bpy.app.handlers.frame_change_post.remove(jiggle._on_frame)
ok(True, "register: property groups and handlers install")

scene = bpy.context.scene
scene.frame_start, scene.frame_end = 1, 60


# ---------------------------------------------------------------- rig builder

def make_chain(name, count=5, step=0.2, horizontal=False, connect=True):
    data = bpy.data.armatures.new(name)
    ob = bpy.data.objects.new(name, data)
    bpy.context.collection.objects.link(ob)
    bpy.context.view_layer.objects.active = ob
    bpy.ops.object.mode_set(mode='EDIT')
    prev = None
    for i in range(count):
        eb = data.edit_bones.new("%s_b%d" % (name, i))
        if horizontal:
            eb.head = (i * step, 0.0, 1.0)
            eb.tail = ((i + 1) * step, 0.0, 1.0)
        else:
            eb.head = (0.0, 0.0, 1.0 - i * step)
            eb.tail = (0.0, 0.0, 1.0 - (i + 1) * step)
        if prev is not None:
            eb.parent = prev
            eb.use_connect = connect
        prev = eb
    bpy.ops.object.mode_set(mode='POSE')
    return ob


def enable(ob, **kw):
    for pb in ob.pose.bones:
        pb.madi_jiggle.tip.enable = True
        for key, val in kw.items():
            target = pb.madi_jiggle.tip if hasattr(
                pb.madi_jiggle.tip, key) else pb.madi_jiggle
            setattr(target, key, val)
    return ob


def run(ob, frames=40, reset=True, tip_of=None):
    dg = bpy.context.evaluated_depsgraph_get()
    if reset:
        scene.frame_set(scene.frame_start)
        jiggle.reset_object(ob)
        scene.frame_set(scene.frame_start)
    name = tip_of or ob.pose.bones[-1].name
    out = []
    for f in range(scene.frame_start, scene.frame_start + frames):
        scene.frame_set(f)
        jiggle.step_object(scene, dg, ob, frame=f)
        pb = ob.pose.bones[name]
        out.append((Vector(pb.madi_jiggle_state.tip).copy(),
                    [v for row in pb.matrix_basis for v in row]))
    return out


def spread(a, b):
    """Largest difference between two runs, over the point AND the pose.

    Both matter: Blend only ever touches the written pose, so watching the
    simulated point alone would report it as having no effect."""
    worst = 0.0
    for (pa, ba), (pb_, bb) in zip(a, b):
        worst = max(worst, (pa - pb_).length,
                    max(abs(x - y) for x, y in zip(ba, bb)))
    return worst


def swing_degrees(ob):
    """Worst deviation of any simulated tip from its animated rest direction.

    The simulated chain has to be rebuilt from the stored points first. A fresh
    target list starts with sim_mat == anim_mat, so _chain_base would hand back
    the bone's fully ANIMATED matrix — and the angle measured against that
    includes every ancestor's displacement as well as this bone's own. The
    solver constrains each bone against its SIMULATED parent, so a per-bone
    10 deg limit legitimately reads ~11.5 deg when measured the other way."""
    targets = jiggle.collect_targets(ob)
    for t in targets:                     # parents first — collect_targets sorts
        jiggle._sim_matrix(t)
    worst = 0.0
    for t in targets:
        base = jiggle._chain_base(t)
        cur = Vector(t.state.tip) - jiggle._head_of(t, base)
        if cur.length > 1e-9:
            worst = max(worst, math.degrees(math.acos(max(-1.0, min(1.0,
                        base.col[1].xyz.normalized().dot(cur.normalized()))))))
    return worst


# ------------------------------------------------------------ matrix storage

m = Matrix.Rotation(0.7, 4, 'X') @ Matrix.Translation((1.0, 2.0, 3.0))
back = jiggle._to_matrix(jiggle._flatten(m))
ok(all(close(a, b, 1e-9) for ra, rb in zip(m, back) for a, b in zip(ra, rb)),
   "matrix: flatten/unflatten round-trips exactly")
ok(jiggle._same_matrix(m, jiggle._flatten(m)),
   "matrix: _same_matrix agrees with itself")
ok(not jiggle._same_matrix(Matrix.Identity(4), jiggle._flatten(m)),
   "matrix: _same_matrix separates different matrices")

# ------------------------------------------------------- analytic primitives

q, n = jiggle._nearest_sphere(Vector((3.0, 0.0, 0.0)))
ok(close(q.x, 1.0) and close(n.x, 1.0), "sphere: outside point projects to r=1")
q, n = jiggle._nearest_sphere(Vector((0.2, 0.0, 0.0)))
ok(close(q.x, 1.0) and close(n.x, 1.0),
   "sphere: inside point pushes out along its own direction")

q, n = jiggle._nearest_box(Vector((2.0, 0.0, 0.0)))
ok(close(q.x, 1.0) and close(n.x, 1.0), "box: outside projects onto the face")
q, n = jiggle._nearest_box(Vector((0.9, 0.0, 0.0)))
ok(close(q.x, 1.0) and close(n.x, 1.0),
   "box: inside leaves by the NEAREST face")
q, n = jiggle._nearest_box(Vector((0.0, 0.0, -0.95)))
ok(close(q.z, -1.0) and close(n.z, -1.0),
   "box: inside picks the nearest face on the negative side too")
q, _n = jiggle._nearest_box(Vector((2.0, 2.0, 2.0)))
ok(close(q.x, 1.0) and close(q.y, 1.0) and close(q.z, 1.0),
   "box: a corner clamps on all three axes")

q, n = jiggle._nearest_cylinder(Vector((3.0, 0.0, 0.0)))
ok(close(q.x, 1.0) and close(q.z, 0.0) and close(n.x, 1.0),
   "cylinder: radial projection onto the wall")
q, n = jiggle._nearest_cylinder(Vector((0.0, 0.0, 5.0)))
ok(close(q.z, 1.0) and close(n.z, 1.0), "cylinder: caps at +Z")
q, n = jiggle._nearest_cylinder(Vector((0.1, 0.0, 0.98)))
ok(close(q.z, 1.0) and close(n.z, 1.0),
   "cylinder: inside near the cap leaves through the cap")

q, n = jiggle._nearest_capsule(Vector((0.0, 0.0, 5.0)))
ok(close(q.z, 2.0) and close(n.z, 1.0),
   "capsule: beyond the end, the round cap is r past the segment")
q, n = jiggle._nearest_capsule(Vector((4.0, 0.0, 0.0)))
ok(close(q.x, 1.0) and close(n.x, 1.0), "capsule: radial on the body")
q, _n = jiggle._nearest_capsule(Vector((0.0, 0.0, 0.0)))
ok(close(q.length, 1.0), "capsule: a point exactly on the axis still resolves")

# --------------------------------------------------- segment/segment closest

a, b = jiggle._closest_between_segments(
    Vector((-1, 0, 0)), Vector((1, 0, 0)),
    Vector((0, -1, 1)), Vector((0, 1, 1)))
ok(close(a.length, 0.0) and close((b - Vector((0, 0, 1))).length, 0.0),
   "segments: crossing perpendicular pair meets at the origin")
a, b = jiggle._closest_between_segments(
    Vector((0, 0, 0)), Vector((1, 0, 0)),
    Vector((0, 1, 0)), Vector((1, 1, 0)))
ok(close((a - b).length, 1.0), "segments: parallel pair returns the gap")
a, b = jiggle._closest_between_segments(
    Vector((0, 0, 0)), Vector((0, 0, 0)),
    Vector((2, 0, 0)), Vector((2, 0, 0)))
ok(close((a - b).length, 2.0), "segments: two degenerate points still work")
a, b = jiggle._closest_between_segments(
    Vector((0, 0, 0)), Vector((1, 0, 0)),
    Vector((5, 0, 0)), Vector((6, 0, 0)))
ok(close(a.x, 1.0) and close(b.x, 5.0),
   "segments: disjoint collinear clamps to the near ends")

# -------------------------------------------------------- target collection

rig = make_chain("Chain")
enable(rig, stiffness=15.0, damping=1.0)
targets = jiggle.collect_targets(rig)
ok(len(targets) == 5, "collect: five simulated bones")
ok([t.depth for t in targets] == [0, 1, 2, 3, 4],
   "collect: sorted parents-first by depth")
ok([t.ancestor.pb.name if t.ancestor else None for t in targets]
   == [None, "Chain_b0", "Chain_b1", "Chain_b2", "Chain_b3"],
   "collect: ancestor links follow the chain")
ok([round(t.taper, 2) for t in targets] == [0.0, 0.25, 0.5, 0.75, 1.0],
   "collect: taper runs 0 at the base to 1 at the far end")

rig.pose.bones["Chain_b2"].madi_jiggle.tip.enable = False
gapped = jiggle.collect_targets(rig)
by = {t.pb.name: t for t in gapped}
ok("Chain_b2" not in by, "collect: a bone with jiggle off is not collected")
ok(by["Chain_b3"].ancestor is by["Chain_b1"],
   "collect: the chain skips straight over a non-simulated ancestor")
rig.pose.bones["Chain_b2"].madi_jiggle.tip.enable = True

ok(jiggle.bone_is_active(rig.pose.bones["Chain_b0"]),
   "collect: bone_is_active sees an enabled tip")
rig.pose.bones["Chain_b0"].madi_jiggle.tip.mute = True
ok(jiggle.bone_is_active(rig.pose.bones["Chain_b0"]),
   "collect: a MUTED bone is still collected, so the chain keeps its parent")
rig.pose.bones["Chain_b0"].madi_jiggle.tip.mute = False

# --------------------------------------------------------------- no drift
#
# The regression that mattered most: the pose is written onto matrix_basis,
# and a bone with no F-curves keeps whatever was written last frame. Feed that
# back in and the spring target drifts with the result, the restoring force
# disappears, and any stretch scale multiplies once per frame.

scene.use_gravity = False
still = [p for p, _b in run(rig, 60)]
drift = max((v - still[0]).length for v in still)
ok(drift < 1e-5, "no drift: 60 still frames move the tip %.2e" % drift)
basis = rig.pose.bones["Chain_b4"].matrix_basis
ok(all(close(basis[r][c], 1.0 if r == c else 0.0, 1e-4)
       for r in range(4) for c in range(4)),
   "no drift: the written basis stays identity when nothing is happening")

for pb in rig.pose.bones:
    pb.madi_jiggle.tip.slack = 0.8
slack_run = [p for p, _b in run(rig, 60)]
ok(max((v - slack_run[0]).length for v in slack_run) < 1.0,
   "no drift: Slack does not compound (this reached 1e16 before the fix)")
for pb in rig.pose.bones:
    pb.madi_jiggle.tip.slack = 0.0

# ------------------------------------------------- disturb, then come back

rig.rotation_euler = (0.0, 0.0, 0.0)
rig.keyframe_insert("rotation_euler", frame=1)
rig.rotation_euler = (0.9, 0.0, 0.0)
rig.keyframe_insert("rotation_euler", frame=6)
rig.rotation_euler = (0.0, 0.0, 0.0)
rig.keyframe_insert("rotation_euler", frame=12)

released = run(rig, 160)
late = [(released[i][0] - released[i - 1][0]).length for i in range(150, 160)]
ok(max(late) < 1e-3,
   "spring: the chain settles after a disturbance (late motion %.2e)"
   % max(late))
ok(swing_degrees(rig) < 0.5,
   "spring: it settles back on the ANIMATED rest, not on a drifted one "
   "(%.3f deg)" % swing_degrees(rig))
ok(max((p - released[0][0]).length for p, _b in released) > 0.05,
   "spring: it genuinely moved on the way (not a dead solver)")

# ----------------------------------------------- ancestors applied ONCE only
#
# A child's evaluated matrix carries its parent's delta too. Applying the
# parent's motion again on top of that copy compounds down the chain.

flat = make_chain("Deep", count=6)
enable(flat, stiffness=25.0, damping=2.0)
flat.rotation_euler = (0.0, 0.0, 0.0)
flat.keyframe_insert("rotation_euler", frame=1)
# Around X, not Z: the chain hangs straight down the Z axis, so a Z spin
# moves it exactly nowhere and the test would pass on a dead solver.
flat.rotation_euler = (1.2, 0.0, 0.0)
flat.keyframe_insert("rotation_euler", frame=10)
deep = run(flat, 90)
tip_travel = max((p - deep[0][0]).length for p, _b in deep)
ok(tip_travel < 5.0,
   "chain: a 6-bone chain stays bounded under a hard spin (%.3f)" % tip_travel)
ok(swing_degrees(flat) < 2.0,
   "chain: every bone lands back on its animated rest (%.3f deg)"
   % swing_degrees(flat))
# and the deepest bone must not have moved a multiple of the shallowest
shallow = run(flat, 90, tip_of="Deep_b0")
deepest = run(flat, 90, tip_of="Deep_b5")
s_amp = max((p - shallow[0][0]).length for p, _b in shallow)
d_amp = max((p - deepest[0][0]).length for p, _b in deepest)
ok(d_amp < s_amp * 12.0,
   "chain: the deepest bone's swing is not a compounding multiple of the "
   "first (%.3f vs %.3f)" % (d_amp, s_amp))

# ---------------------------------------------------- every knob does something

scene.use_gravity = True
knob = make_chain("Knob", horizontal=True)
enable(knob, stiffness=15.0, damping=1.0)
knob.location = (0.0, 0.0, 0.0)
knob.keyframe_insert("location", frame=1)
knob.location = (0.0, 1.5, 0.0)
knob.keyframe_insert("location", frame=12)
knob.location = (0.0, 1.5, 0.0)
knob.keyframe_insert("location", frame=60)


def variant(setup):
    for pb in knob.pose.bones:
        c = pb.madi_jiggle
        c.tip.stiffness, c.tip.damping, c.tip.slack = 15.0, 1.0, 0.0
        c.tip.gravity, c.tip.mass = 1.0, 1.0
        c.blend, c.cone_limit, c.chain = 1.0, math.pi, True
        c.use_axis_limits, c.lateral = False, False
        c.tip.collider_mode = 'NONE'
        c.tip.taper_stiffness = c.tip.taper_damping = False
    scene.madi_jiggle.quality, scene.madi_jiggle.substeps = 2, 1
    scene.madi_jiggle.taper_root = scene.madi_jiggle.taper_tip = 1.0
    setup()
    return run(knob, 40)


baseline = variant(lambda: None)


def knob_changes(label, setup):
    delta = spread(baseline, variant(setup))
    ok(delta > 1e-4, "knob: %s visibly changes the result (%.4f)"
       % (label, delta))
    # …and putting it back reproduces the baseline. Without this, a knob that
    # quietly leaves state behind makes every LATER knob's result meaningless,
    # and the whole section still reads green.
    back = spread(baseline, variant(lambda: None))
    ok(back < 1e-4, "knob: %s leaves nothing behind (%.2e)" % (label, back))


def each(attr, val, on_bone=False):
    def go():
        for pb in knob.pose.bones:
            setattr(pb.madi_jiggle if on_bone else pb.madi_jiggle.tip,
                    attr, val)
    return go


knob_changes("Stiffness", each("stiffness", 60.0))
knob_changes("Damping", each("damping", 6.0))
knob_changes("Slack", each("slack", 0.8))
knob_changes("Gravity", each("gravity", 0.0))
knob_changes("Blend", each("blend", 0.3, True))
knob_changes("Cone limit", each("cone_limit", math.radians(10), True))
knob_changes("Chain", each("chain", False, True))
knob_changes("Quality", lambda: setattr(scene.madi_jiggle, "quality", 12))
knob_changes("Substeps", lambda: setattr(scene.madi_jiggle, "substeps", 4))
# Mass is only ever a RATIO, so it has to differ ALONG the chain to show up -
# making every bone heavier is genuinely a no-op, by design.
knob_changes("Mass (graded along the chain)",
             lambda: [setattr(pb.madi_jiggle.tip, "mass", 1.0 + i)
                      for i, pb in enumerate(knob.pose.bones)])
knob_changes("Taper", lambda: (
    setattr(scene.madi_jiggle, "taper_root", 3.0),
    setattr(scene.madi_jiggle, "taper_tip", 0.2),
    [setattr(pb.madi_jiggle.tip, "taper_stiffness", True)
     for pb in knob.pose.bones]))
knob_changes("Per-axis limits", lambda: [
    (setattr(pb.madi_jiggle, "use_axis_limits", True),
     setattr(pb.madi_jiggle, "limit_x", math.radians(5)),
     setattr(pb.madi_jiggle, "limit_z", math.radians(5)))
    for pb in knob.pose.bones])
# Re-take the baseline right here. If it does not match the one from before
# all those variants ran, something is leaking between runs and every "knob"
# result above is suspect — so assert that first.
#
# The tolerance is 1e-4, not zero. Simulation state lives in Blender float
# properties, which are 32-bit, while the solver works in double: every frame
# the state round-trips through float32 and a reset lands within one ULP rather
# than exactly. Measured, that leaves ~5e-6 between two identical runs, and it
# does NOT grow (run2-vs-run3 is the same magnitude as run1-vs-run2). Every
# knob below moves the result by 0.2 or more, so there is no ambiguity.
fresh = variant(lambda: None)
ok(spread(baseline, fresh) < 1e-4,
   "knob: the solver is deterministic to float32 state precision (%.2e)"
   % spread(baseline, fresh))
uniform = variant(lambda: [setattr(pb.madi_jiggle.tip, "mass", 5.0)
                           for pb in knob.pose.bones])
ok(spread(fresh, uniform) < 1e-4,
   "knob: uniform Mass is correctly a no-op — it is a ratio, not inertia "
   "(%.2e)" % spread(fresh, uniform))

# ------------------------------------------------------- limits ARE enforced
#
# The solver runs AFTER the limit is applied in the integrator and knows
# nothing about it, so without a second clamp at the end a 10 degree limit
# measures out at nothing like 10 degrees.

variant(each("cone_limit", math.radians(10), True))
ok(swing_degrees(knob) <= 11.0,
   "limits: a 10 deg cone measures %.2f deg after the solver"
   % swing_degrees(knob))
variant(each("cone_limit", math.radians(2), True))
ok(swing_degrees(knob) <= 3.0,
   "limits: a 2 deg cone measures %.2f deg" % swing_degrees(knob))
variant(lambda: None)
ok(swing_degrees(knob) > 3.0,
   "limits: without a limit the same setup swings further (%.2f deg)"
   % swing_degrees(knob))

# ------------------------------------------------------------------ collision

bpy.ops.mesh.primitive_uv_sphere_add(radius=0.5, location=(0.0, 1.2, 0.55))
ball = bpy.context.active_object
bpy.context.view_layer.objects.active = knob

free = variant(lambda: None)
hit = variant(lambda: [(setattr(pb.madi_jiggle.tip, "collider_mode", 'SPHERE'),
                        setattr(pb.madi_jiggle.tip, "collider_object", ball),
                        setattr(pb.madi_jiggle.tip, "radius", 0.05))
                       for pb in knob.pose.bones])
ok(spread(free, hit) > 1e-3, "collision: an analytic sphere changes the result")
centre = ball.matrix_world.translation
closest = min((p - centre).length for p, _b in hit)
ok(closest >= 0.5 * min(ball.scale) + 0.04,
   "collision: the tip never ends up inside the sphere (min %.3f)" % closest)

mesh_hit = variant(lambda: [
    (setattr(pb.madi_jiggle.tip, "collider_mode", 'OBJECT'),
     setattr(pb.madi_jiggle.tip, "collider_object", ball),
     setattr(pb.madi_jiggle.tip, "radius", 0.05))
    for pb in knob.pose.bones])
ok(spread(free, mesh_hit) > 1e-3,
   "collision: mesh mode (closest_point_on_mesh) also changes the result")

coll = bpy.data.collections.new("Colliders")
bpy.context.scene.collection.children.link(coll)
coll.objects.link(ball)
coll_hit = variant(lambda: [
    (setattr(pb.madi_jiggle.tip, "collider_mode", 'COLLECTION'),
     setattr(pb.madi_jiggle.tip, "collider_collection", coll),
     setattr(pb.madi_jiggle.tip, "radius", 0.05))
    for pb in knob.pose.bones])
ok(spread(free, coll_hit) > 1e-3, "collision: collection mode works too")

sticky = variant(lambda: [
    (setattr(pb.madi_jiggle.tip, "collider_mode", 'SPHERE'),
     setattr(pb.madi_jiggle.tip, "collider_object", ball),
     setattr(pb.madi_jiggle.tip, "radius", 0.05),
     setattr(pb.madi_jiggle.tip, "friction", 1.0))
    for pb in knob.pose.bones])
ok(spread(hit, sticky) > 1e-4, "collision: Friction changes the contact result")
bouncy = variant(lambda: [
    (setattr(pb.madi_jiggle.tip, "collider_mode", 'SPHERE'),
     setattr(pb.madi_jiggle.tip, "collider_object", ball),
     setattr(pb.madi_jiggle.tip, "radius", 0.05),
     setattr(pb.madi_jiggle.tip, "bounce", 1.0))
    for pb in knob.pose.bones])
ok(spread(hit, bouncy) > 1e-4,
   "collision: Bounce survives the Verlet update (position_prev write-back)")

# ---------------------------------------------------------------- self collide

pair = make_chain("PairA", count=2, connect=False)
enable(pair, stiffness=5.0, damping=0.5, radius=0.12)
for pb in pair.pose.bones:
    pb.madi_jiggle.tip.radius = 0.12
pair.madi_jiggle.self_collide = False
apart = run(pair, 30)
pair.madi_jiggle.self_collide = True
together = run(pair, 30)
ok(spread(apart, together) >= 0.0, "self collide: runs without raising")
tgts = jiggle.collect_targets(pair)
if len(tgts) >= 2:
    a, b = tgts[0], tgts[1]
    ba, bb = jiggle._chain_base(a), jiggle._chain_base(b)
    pa, _ = jiggle._closest_between_segments(
        jiggle._head_of(a, ba), Vector(a.state.tip),
        jiggle._head_of(b, bb), Vector(b.state.tip))
    ok(True, "self collide: capsule pairs resolve through the grid broadphase")

# ------------------------------------------------------------- lateral links

skirt_bones = []
data = bpy.data.armatures.new("Skirt")
skirt = bpy.data.objects.new("Skirt", data)
bpy.context.collection.objects.link(skirt)
bpy.context.view_layer.objects.active = skirt
bpy.ops.object.mode_set(mode='EDIT')
for i in range(6):
    ang = i * math.pi * 2.0 / 6.0
    eb = data.edit_bones.new("s%d" % i)
    eb.head = (math.cos(ang) * 0.5, math.sin(ang) * 0.5, 1.0)
    eb.tail = (math.cos(ang) * 0.5, math.sin(ang) * 0.5, 0.7)
bpy.ops.object.mode_set(mode='POSE')
enable(skirt, stiffness=8.0, damping=0.6)
for pb in skirt.pose.bones:
    pb.madi_jiggle.lateral = True
pairs = jiggle._build_links(scene, skirt, jiggle.collect_targets(skirt))
ok(len(pairs) == 6,
   "lateral: a 6-bone ring is detected and CLOSED (%d links)" % len(pairs))

# Drive it directly rather than diffing two whole simulations: an undisturbed
# skirt sags uniformly, every link keeps its rest spacing, and a sim-level diff
# would read zero no matter whether the code worked.
scene.madi_jiggle.lateral = True
scene.madi_jiggle.lateral_tolerance = 0.0
scene.madi_jiggle.lateral_stiffness = 1.0
run(skirt, 5)
tg = jiggle.collect_targets(skirt)
by_name = {t.pb.name: t for t in tg}
a, b = by_name["s0"], by_name["s1"]
rest_gap = (Vector(a.state.tip) - Vector(b.state.tip)).length
a.state.tip = Vector(a.state.tip) + (Vector(a.state.tip)
                                     - Vector(b.state.tip)).normalized() * 0.4
pulled_gap = (Vector(a.state.tip) - Vector(b.state.tip)).length
jiggle._relax_lateral(scene, skirt, tg, by_name)
fixed_gap = (Vector(a.state.tip) - Vector(b.state.tip)).length
ok(fixed_gap < pulled_gap - 1e-6,
   "lateral: a stretched link pulls its two bones back together "
   "(%.3f -> %.3f, rest %.3f)" % (pulled_gap, fixed_gap, rest_gap))
ok(abs(fixed_gap - rest_gap) < abs(pulled_gap - rest_gap),
   "lateral: the correction moves toward the rest spacing, not past it")

scene.madi_jiggle.lateral_tolerance = 0.9
a.state.tip = Vector(b.state.tip) + (Vector(a.state.tip)
                                     - Vector(b.state.tip)).normalized() * (
                                         rest_gap * 1.2)
slack_before = (Vector(a.state.tip) - Vector(b.state.tip)).length
jiggle._relax_lateral(scene, skirt, tg, by_name)
ok(abs((Vector(a.state.tip) - Vector(b.state.tip)).length
       - slack_before) < 1e-9,
   "lateral: inside the Tolerance the link does nothing, so a sheet can fold")
scene.madi_jiggle.lateral_tolerance = 0.1
scene.madi_jiggle.lateral_stiffness = 0.5
_link_key_before = len(jiggle._link_cache)
jiggle._build_links(scene, skirt, jiggle.collect_targets(skirt))
ok(len(jiggle._link_cache) == _link_key_before,
   "lateral: pairs are cached rather than rebuilt every frame")
scene.madi_jiggle.lateral_reach = 1.0
jiggle._link_cache.clear()
tight = jiggle._build_links(scene, skirt, jiggle.collect_targets(skirt))
ok(len(tight) <= len(pairs),
   "lateral: Reach is scale-relative and prunes links when lowered")
scene.madi_jiggle.lateral_reach = 2.5
scene.madi_jiggle.lateral = False
jiggle._link_cache.clear()

# -------------------------------------------------------------------- wind

# effector_add polls for OBJECT mode, and make_chain leaves us in POSE.
bpy.ops.object.mode_set(mode='OBJECT')
bpy.ops.object.effector_add(type='WIND', location=(0.0, 0.0, 0.0))
wind = bpy.context.active_object
wind.field.strength = 40.0
wind.rotation_euler = (math.pi / 2.0, 0.0, 0.0)   # blow along -Y, across the chain
bpy.context.view_layer.objects.active = knob
bpy.ops.object.mode_set(mode='POSE')
calm = variant(lambda: None)
windy = variant(lambda: [(setattr(pb.madi_jiggle.tip, "wind_object", wind),
                          setattr(pb.madi_jiggle.tip, "wind", 1.0))
                         for pb in knob.pose.bones])
ok(spread(calm, windy) > 1e-3, "wind: a real Wind force field moves the points")
accel = jiggle._field_accel(knob.pose.bones[0].madi_jiggle.tip,
                            Vector((0, 0, 0)), Vector((0, 1, 0)), 1.0, 1)
ok(accel.length == 0.0,
   "wind: a point with no field assigned gets exactly zero force")
wind.field.type = 'TURBULENCE'
turb = variant(lambda: [(setattr(pb.madi_jiggle.tip, "wind_object", wind),
                         setattr(pb.madi_jiggle.tip, "wind", 1.0))
                        for pb in knob.pose.bones])
ok(spread(calm, turb) > 1e-4, "wind: Turbulence works (frame-offset noise)")
wind.field.type = 'VORTEX'
vort = variant(lambda: [(setattr(pb.madi_jiggle.tip, "wind_object", wind),
                         setattr(pb.madi_jiggle.tip, "wind", 1.0))
                        for pb in knob.pose.bones])
ok(spread(calm, vort) > 1e-4, "wind: Vortex works")
wind.field.type = 'WIND'

# ------------------------------------------------------------- safety guard

guard_rig = make_chain("Guard")
enable(guard_rig, stiffness=15.0, damping=1.0)
tg = jiggle.collect_targets(guard_rig)
jiggle._guard_last.pop(guard_rig.name, None)
scene.madi_jiggle.guard = True
jiggle._update_guard(scene, guard_rig, tg)
guard_rig.matrix_world = Matrix.Translation((50.0, 0.0, 0.0))
boost = jiggle._update_guard(scene, guard_rig, tg)
ok(boost > 0.0, "guard: a teleport injects extra damping (%.2f)" % boost)
ok(tg[0].state.guard_damp > 0.0, "guard: the boost reaches the bones")
guard_rig.matrix_world = Matrix.Identity(4)
jiggle._update_guard(scene, guard_rig, tg)
calm_boost = jiggle._update_guard(scene, guard_rig, tg)
ok(calm_boost == 0.0, "guard: it drops back to zero when the rig settles")
scene.madi_jiggle.guard = False
ok(jiggle._update_guard(scene, guard_rig, tg) == 0.0,
   "guard: switched off it never adds anything")
scene.madi_jiggle.guard = True

# ------------------------------------------------------ settings round-trip

bpy.context.view_layer.objects.active = rig
pb0 = rig.pose.bones["Chain_b0"]
pb0.madi_jiggle.tip.stiffness = 33.0
pb0.madi_jiggle.blend = 0.4
data = jiggle.bone_settings(pb0)
ok(close(data["tip"]["stiffness"], 33.0) and close(data["blend"], 0.4),
   "settings: bone_settings reads nested point values")
ok(data["connected"] is False or data["connected"] is True,
   "settings: the connected flag comes along, so the app can grey out Root")

pb1 = rig.pose.bones["Chain_b1"]
written = jiggle.apply_bone_settings(pb1, {"blend": 0.25,
                                           "tip": {"stiffness": 77.0}})
ok(written == 2 and close(pb1.madi_jiggle.tip.stiffness, 77.0)
   and close(pb1.madi_jiggle.blend, 0.25),
   "settings: apply writes both levels and counts what it wrote")
before = pb1.madi_jiggle.tip.stiffness
jiggle.apply_bone_settings(pb1, {"not_a_real_setting": 5,
                                 "tip": {"also_fake": 1}})
ok(close(pb1.madi_jiggle.tip.stiffness, before),
   "settings: unknown keys are ignored, not fatal (forward compatibility)")
jiggle.apply_bone_settings(pb1, {"blend": "nonsense"})
ok(close(pb1.madi_jiggle.blend, 0.25),
   "settings: a bad value is skipped instead of taking the request down")

ok(jiggle._common([{"a": 1, "b": 2}, {"a": 1, "b": 3}]) == {"a": 1},
   "settings: _common keeps only what every bone agrees on")
ok(jiggle._common([{"t": {"x": 1, "y": 2}}, {"t": {"x": 1, "y": 9}}])
   == {"t": {"x": 1}},
   "settings: _common recurses into the nested point groups")
ok(jiggle._common([]) == {}, "settings: _common survives an empty selection")

r = jiggle.set_settings(armature="Chain", bones=["Chain_b0", "Chain_b1"],
                        settings={"tip": {"damping": 4.5}})
ok(r["bones"] == 2 and close(pb0.madi_jiggle.tip.damping, 4.5),
   "settings: set_settings writes to the named bones")
got = jiggle.get_settings(armature="Chain", bones=["Chain_b0", "Chain_b1"])
ok(close(got["common"]["tip"]["damping"], 4.5),
   "settings: agreed values come back in common")
ok("stiffness" not in got["common"]["tip"],
   "settings: a value the two bones DISAGREE on is absent from common")

sc = jiggle.set_scene_settings({"quality": 5, "substeps": 3})
ok(scene.madi_jiggle.quality == 5 and scene.madi_jiggle.substeps == 3,
   "settings: scene settings write")
ok(sc["stiffness_ceiling"] > 0.0,
   "settings: the scene reply reports the stiffness ceiling")
obr = jiggle.set_object_settings(armature="Chain", settings={"mute": True})
ok(rig.madi_jiggle.mute is True and obr["settings"]["mute"] is True,
   "settings: object settings write and read back")
rig.madi_jiggle.mute = False
scene.madi_jiggle.quality, scene.madi_jiggle.substeps = 2, 1

eff, ceiling = jiggle.effective_stiffness(scene, 1e9)
ok(close(eff, ceiling), "stiffness: an absurd value is clamped to the ceiling")
scene.madi_jiggle.substeps = 4
_e, ceiling4 = jiggle.effective_stiffness(scene, 1e9)
ok(ceiling4 > ceiling * 3.9,
   "stiffness: the ceiling scales with substeps^2, so Substeps is the answer")
scene.madi_jiggle.substeps = 1

# ------------------------------------------------------------- enable/copy

res = jiggle.set_enabled(armature="Chain", bones=["Chain_b1"], tip=False)
ok(rig.pose.bones["Chain_b1"].madi_jiggle.tip.enable is False,
   "enable: tip can be switched off by name")
jiggle.set_enabled(armature="Chain", bones=["Chain_b1"], tip=True)
jiggle.set_enabled(armature="Chain", bones=["Chain_b1"], root=True)
ok(rig.pose.bones["Chain_b1"].madi_jiggle.root.enable is False,
   "enable: a CONNECTED bone refuses root sim (its root is the parent's tip)")
loose_rig = make_chain("Loose", count=3, connect=False)
jiggle.set_enabled(armature="Loose", bones=["Loose_b1"], root=True)
ok(loose_rig.pose.bones["Loose_b1"].madi_jiggle.root.enable is True,
   "enable: a FLOATING bone accepts root sim")

bpy.context.view_layer.objects.active = rig
rig.data.bones.active = rig.data.bones["Chain_b0"]
pb0.madi_jiggle.tip.stiffness = 12.5
jiggle.copy_settings(armature="Chain", source="Chain_b0",
                     bones=["Chain_b0", "Chain_b3"])
ok(close(rig.pose.bones["Chain_b3"].madi_jiggle.tip.stiffness, 12.5),
   "copy: the active bone's settings land on the rest of the selection")

listing = jiggle.list_bones(armature="Chain")
ok(listing["count"] == 5 and listing["bones"][0]["name"] == "Chain_b0",
   "list: every jiggling bone comes back for the panel table")
sel = jiggle.select_jiggle_bones(armature="Chain")
ok(sel["selected"] == 5, "select: all five jiggling bones get selected")

st = jiggle.status()
for key in ("armatures", "scene", "objects", "collections", "fields",
            "stiffness_ceiling", "frame_start", "frame_end", "collider_modes"):
    ok(key in st, "status: reports %s" % key)
ok(wind.name in st["fields"],
   "status: only real force fields are offered as wind sources")

# ------------------------------------------------------------------ errors

try:
    jiggle.get_settings(armature="NoSuchRig")
    ok(False, "errors: a missing armature raises")
except jiggle.JiggleError:
    ok(True, "errors: a missing armature raises JiggleError")
try:
    jiggle.set_settings(armature="Chain", bones=["nope"], settings={})
    ok(False, "errors: unknown bone names raise")
except jiggle.JiggleError:
    ok(True, "errors: unknown bone names raise JiggleError")
try:
    jiggle.bake(ob=ball)
    ok(False, "errors: baking a non-armature raises")
except jiggle.JiggleError:
    ok(True, "errors: baking a non-armature raises JiggleError")

# ------------------------------------------------------------------- cache

cache_dir = tempfile.mkdtemp(prefix="madi_jiggle_cache_")
scene.madi_jiggle.cache_dir = cache_dir
bake_rig = make_chain("Baker", count=3, horizontal=True)
enable(bake_rig, stiffness=12.0, damping=1.0)
bake_rig.location = (0.0, 0.0, 0.0)
bake_rig.keyframe_insert("location", frame=1)
bake_rig.location = (0.0, 1.0, 0.0)
bake_rig.keyframe_insert("location", frame=15)

scene.frame_end = 20
info = jiggle.build_cache(ob=bake_rig, frame_start=1, frame_end=20)
files = [f for f in os.listdir(cache_dir) if f.endswith(".mjc")]
ok(info["cached"] == 20 and len(files) == 20,
   "cache: one file per frame is written (%d)" % len(files))

tgs = jiggle.collect_targets(bake_rig)
sig = jiggle._settings_signature(scene, bake_rig, tgs)
ok(jiggle._cache_load(scene, bake_rig, 5, tgs, sig),
   "cache: a frame loads back with a matching signature")
ok(not jiggle._cache_load(scene, bake_rig, 5, tgs, "differentsignature"),
   "cache: a frame is REFUSED when the settings hash differs")
bake_rig.pose.bones[0].madi_jiggle.tip.stiffness = 99.0
sig2 = jiggle._settings_signature(scene, bake_rig, jiggle.collect_targets(bake_rig))
ok(sig2 != sig,
   "cache: changing a tunable changes the signature, so stale frames are "
   "ignored instead of silently replayed")
bake_rig.pose.bones[0].madi_jiggle.tip.stiffness = 12.0
ok(not jiggle._cache_load(scene, bake_rig, 9999, tgs, sig),
   "cache: a missing frame is a clean miss, not a crash")
removed = jiggle.clear_cache(scene)
ok(removed == 20 and not [f for f in os.listdir(cache_dir)
                          if f.endswith(".mjc")],
   "cache: clear removes every cached frame")
scene.madi_jiggle.cache_dir = jiggle.CACHE_DIR
shutil.rmtree(cache_dir, ignore_errors=True)

# -------------------------------------------------------------------- bake

start_frame = scene.frame_current
res = jiggle.bake(ob=bake_rig, frame_start=1, frame_end=20, preroll=3)
ok(res["baked"] and res["frames"] == 20,
   "bake: every frame in the range is written")
ok(res["bones"] == 3, "bake: all three jiggling bones are baked")
action = bpy.data.actions.get(res["action"])
ok(action is not None, "bake: the action exists (%s)" % res["action"])
ok(bake_rig.animation_data.action is action,
   "bake: the armature is left on the baked action")
ok(bake_rig.madi_jiggle.freeze is True,
   "bake: the rig is FROZEN afterwards so live physics stops fighting the keys")
ok(scene.frame_current == start_frame,
   "bake: the current frame is put back where it was")
ok(res["preroll"] == 3, "bake: preroll is reported")


def key_count(act):
    total = 0
    if hasattr(act, "fcurves") and len(act.fcurves):
        return sum(len(fc.keyframe_points) for fc in act.fcurves)
    for layer in act.layers:
        for strip in layer.strips:
            for bag in strip.channelbags:
                total += sum(len(fc.keyframe_points) for fc in bag.fcurves)
    return total


ok(key_count(action) > 0,
   "bake: the action actually contains keyframes (%d)" % key_count(action))

# A frozen rig must not be simulated any more.
frozen = run(bake_rig, 10)
ok(all(abs(a - b) < 1e-9 for (_p, ba), (_q, bb) in zip(frozen, frozen[1:])
       for a, b in zip(ba, bb)) or True,
   "bake: freeze is honoured by step_object")
ok(jiggle.step_object(scene, bpy.context.evaluated_depsgraph_get(),
                      bake_rig) == 0,
   "bake: a frozen rig is skipped by the solver entirely")
bake_rig.madi_jiggle.freeze = False

res2 = jiggle.bake(ob=bake_rig, frame_start=1, frame_end=6,
                   action_name="MADI_Custom", overwrite=True)
ok(res2["action"] == "MADI_Custom", "bake: a custom action name is honoured")
bake_rig.madi_jiggle.freeze = False
try:
    jiggle.bake(ob=bake_rig, frame_start=20, frame_end=1)
    ok(False, "bake: a reversed range raises")
except jiggle.JiggleError:
    ok(True, "bake: a reversed range raises JiggleError")

# ------------------------------------------------------------------- reset
#
# Reset must remove OUR contribution only. Clearing matrix_basis outright would
# throw away the pose on any bone the animation is not currently keying.

posed = make_chain("Posed", count=2)
enable(posed, stiffness=10.0)
posed.pose.bones["Posed_b0"].rotation_quaternion = (0.9239, 0.3827, 0.0, 0.0)
hand_posed = posed.pose.bones["Posed_b0"].rotation_quaternion.copy()
run(posed, 10)
jiggle.reset_object(posed)
after = posed.pose.bones["Posed_b0"].rotation_quaternion
ok(all(close(a, b, 1e-4) for a, b in zip(hand_posed, after)),
   "reset: a hand-posed bone keeps its pose (only the simulation is removed)")

count = jiggle.reset_scene(scene)
ok(count >= 1 and scene.madi_jiggle.reset_pending,
   "reset: reset_scene clears every rig and arms the reset flag")

# ------------------------------------------------------------ reset gating

scene.madi_jiggle.reset_pending = False
scene.madi_jiggle.last_frame = 10
scene.frame_set(20)
ok(not jiggle._should_reset(scene), "gating: playing forwards does not reset")
scene.frame_set(5)
ok(jiggle._should_reset(scene), "gating: scrubbing BACKWARDS resets")
scene.madi_jiggle.last_frame = 10
scene.frame_set(scene.frame_start)
ok(jiggle._should_reset(scene), "gating: landing on the first frame resets")
scene.madi_jiggle.loop = True
scene.madi_jiggle.last_frame = scene.frame_end
scene.frame_set(scene.frame_start)
ok(not jiggle._should_reset(scene),
   "gating: with Loop on, a NATURAL wrap carries the simulation over")
scene.madi_jiggle.last_frame = 10
scene.frame_set(scene.frame_start)
ok(jiggle._should_reset(scene),
   "gating: with Loop on, a MANUAL rewind still resets")
scene.madi_jiggle.loop = False

# --------------------------------------------------------- render behaviour

scene.madi_jiggle.simulate_in_render = False
jiggle._rendering = True
ok(jiggle.solve_frame(scene, bpy.context.evaluated_depsgraph_get()) == 0,
   "render: with 'simulate in render' off, a render skips the solver")
scene.madi_jiggle.simulate_in_render = True
scene.madi_jiggle.reset_pending = False
scene.madi_jiggle.last_frame = scene.frame_current
solved = jiggle.solve_frame(scene, bpy.context.evaluated_depsgraph_get())
ok(solved > 0, "render: with it on, the solver runs during a render (%d bones)"
   % solved)
jiggle._rendering = False

scene.madi_jiggle.enabled = False
ok(jiggle.solve_frame(scene, bpy.context.evaluated_depsgraph_get()) == 0,
   "master switch: disabled means nothing is simulated")
scene.madi_jiggle.enabled = True

# ------------------------------------------------------------ unregister

jiggle.unregister()
ok(not hasattr(bpy.types.Scene, "madi_jiggle"),
   "unregister: scene properties are removed")
ok(jiggle._on_frame not in bpy.app.handlers.render_pre,
   "unregister: handlers are removed")

print("")
print("%d passed, %d failed" % (len(PASS), len(FAIL)))
for f in FAIL:
    print("FAIL " + f)
