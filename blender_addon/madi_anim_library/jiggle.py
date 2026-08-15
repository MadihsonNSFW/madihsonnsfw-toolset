# MADI Bone Jiggle — spring-driven secondary motion on bones (no UI).
#
# Add jiggle to a rig without a cloth or softbody sim: each enabled bone gets
# one or two simulated POINTS in world space, the points are pulled toward the
# pose the animation asks for, and the bone's rotation is DERIVED from where
# the points ended up. Nothing about the bone itself is integrated, which is
# what keeps long chains stable and cheap.
#
# Kept OUT of core.py like the other tool modules: it is a self-contained
# solver that shares nothing with the library logic, and core.py is already
# 230 KB.
#
# The per-frame pipeline (each step is a function below):
#
#   collect_targets()   bones with a simulated end, sorted parents-first
#   snapshot            the ANIMATED world matrices, taken once (see below)
#   for each substep:
#     _integrate()      damping -> gravity/wind -> spring to the animated
#                       rest position -> pos += vel + a*dt^2 -> limits ->
#                       anchors -> contact
#     _relax() x N      Gauss-Seidel: spring to the chained rest position +
#                       length constraint, corrections split by mass and
#                       propagated back into the parent
#     _relax_lateral()  optional side-to-side links between neighbouring
#                       chains (skirts, hair bunches)
#     _resolve_mutual() optional capsule/capsule self-collision, grid broadphase
#     _reclamp()        angle limits AGAIN, because the solver does not know
#                       about them (this is not optional — see below)
#     _sim_matrix()     points -> the bone's simulated world matrix
#     velocity          vel = pos - pos_prev
#   _write_pose()       the accumulated local delta onto matrix_basis
#
# ---------------------------------------------------------------------------
# Three decisions here that are deliberate, and each one is load-bearing:
#
# 1. THE ANIMATED MATRICES ARE SNAPSHOT ONCE PER FRAME, before anything is
#    written. Every "where should this bone be" question is then answered from
#    the snapshot, never from live `PoseBone.matrix`. Reading it live makes the
#    result depend on how far through the bone list you are AND on whether the
#    depsgraph happens to have re-evaluated, which is the kind of bug that
#    reproduces once in ten scrubs.
#
# 2. THE POSE IS WRITTEN AS A LOCAL DELTA ONTO `matrix_basis`, not by assigning
#    `PoseBone.matrix`. `matrix = A @ basis` where A depends only on ancestors,
#    so `basis @= delta` means exactly "add this on top of the animation" no
#    matter what the ancestors are doing and with no depsgraph round-trip.
#    It also makes the animation/simulation blend a matrix interpolation toward
#    identity, so it lands correctly whatever rotation mode the bone is in
#    instead of needing a branch per mode.
#
# 3. THE MATRIX CHILDREN CHAIN OFF CARRIES NO SCALE. Stretch is applied only in
#    the final pose write. A scaled matrix in the chain multiplies down the
#    chain and shears everything below it.
# ---------------------------------------------------------------------------

import hashlib
import json
import math
import os

import bpy
from bpy.props import (BoolProperty, EnumProperty, FloatProperty,
                       FloatVectorProperty, IntProperty, PointerProperty,
                       StringProperty)
from bpy.types import PropertyGroup
from mathutils import Matrix, Quaternion, Vector, noise

# ---------------------------------------------------------------------------
# Constants

CACHE_DIR = "//madi_jiggle_cache"
BAKE_ACTION_FMT = "MADI_Jiggle_%s"

# A spring integrated with an explicit step goes unstable once k*dt^2 gets near
# 1, so the effective stiffness is capped here rather than trusted. The studied
# add-on capped at 0.1/dt^2 with dt hard-wired to 1/fps, which quietly makes
# maximum stiffness a function of the scene frame rate: a rig tuned at 24 fps
# behaves differently at 60. Ours has the same ceiling shape but dt is the
# SUBSTEP, so raising Substeps genuinely raises the usable stiffness instead of
# the frame rate deciding it. `effective_stiffness()` reports the ceiling so
# the app can tell the user which knob to turn.
MAX_K_DT2 = 0.25

EPS = 1e-9

_PRIMITIVES = ('SPHERE', 'BOX', 'CYLINDER', 'CAPSULE')

COLLIDER_MODES = (
    ('NONE', "None", "No collision for this point"),
    ('OBJECT', "Object", "Collide against one mesh object's evaluated surface"),
    ('COLLECTION', "Collection", "Collide against every mesh in a collection"),
    ('SPHERE', "Sphere", "Analytic sphere — the object's transform and scale, no mesh needed"),
    ('BOX', "Box", "Analytic box — the object's transform and scale, no mesh needed"),
    ('CYLINDER', "Cylinder", "Analytic cylinder along the object's local Z"),
    ('CAPSULE', "Capsule", "Analytic capsule along the object's local Z"),
)

# Runtime-only, never saved: the previous frame's world matrix per armature,
# used by the safety guard, and the lateral link pairs per armature.
_guard_last = {}
_link_cache = {}
_baking = False
_rendering = False


class JiggleError(Exception):
    """A failure worth showing the user verbatim."""


# ---------------------------------------------------------------------------
# Properties
#
# The studied add-on carries roughly sixty flat `wiggle_*` properties on
# PoseBone, every dynamics tunable existing twice with a `_head` suffix. Ours
# nests instead: ONE point-settings group, used twice. Same reach, a third of
# the surface, and adding a tunable to both ends is one line rather than two
# that can drift apart.

class MadiJigglePoint(PropertyGroup):
    """Everything that describes ONE simulated point (a bone's tip or root)."""

    enable: BoolProperty(
        name="Enable", default=False,
        description="Simulate this end of the bone")
    mute: BoolProperty(
        name="Mute", default=False,
        description="Pause the simulation for this end — the bone snaps back "
                    "to the animated pose without losing its settings")

    mass: FloatProperty(
        name="Mass", default=1.0, min=0.001, soft_max=10.0,
        description="Relative weight. Sets how wind moves this point and how a "
                    "correction is shared with the parent — it is NOT inertia")
    stiffness: FloatProperty(
        name="Stiffness", default=20.0, min=0.0, soft_max=200.0,
        description="How hard the point is pulled back to the pose the "
                    "animation asks for. Above the ceiling shown in the app it "
                    "is clamped for stability — raise Substeps to go higher")
    damping: FloatProperty(
        name="Damping", default=1.0, min=0.0, soft_max=20.0,
        description="How quickly motion bleeds away. Low values ring for a "
                    "long time")
    slack: FloatProperty(
        name="Slack", default=0.0, min=0.0, max=1.0, subtype='FACTOR',
        description="How freely the bone may change length. 0 holds the bone's "
                    "length exactly, 1 lets it stretch and squash freely")
    gravity: FloatProperty(
        name="Gravity", default=1.0, soft_min=-2.0, soft_max=2.0,
        description="Multiplier on the scene's own gravity")

    wind_object: PointerProperty(
        type=bpy.types.Object, name="Wind Field",
        description="A Blender force field (Wind, Turbulence or Vortex). Its "
                    "strength and falloff are read live from the field's own "
                    "settings, so it is tuned in Blender's physics panel")
    wind: FloatProperty(
        name="Wind Strength", default=1.0, soft_min=0.0, soft_max=10.0,
        description="Multiplier on the force field's own strength")

    collider_mode: EnumProperty(
        name="Collide With", items=COLLIDER_MODES, default='NONE',
        description="What this point collides against")
    collider_object: PointerProperty(
        type=bpy.types.Object, name="Collider",
        description="The collision target. For the analytic shapes only the "
                    "object's transform and scale are used, so an Empty works")
    collider_collection: PointerProperty(
        type=bpy.types.Collection, name="Collider Collection",
        description="Every mesh in this collection is collided against")

    radius: FloatProperty(
        name="Radius", default=0.05, min=0.0, soft_max=1.0, unit='LENGTH',
        description="Collision radius of the simulated point itself")
    friction: FloatProperty(
        name="Friction", default=0.5, min=0.0, max=1.0, subtype='FACTOR',
        description="1 holds the point exactly where it last touched, 0 lets "
                    "it slide freely. The contact is remembered in the "
                    "collider's own space, so a moving collider drags it along")
    bounce: FloatProperty(
        name="Bounce", default=0.5, min=0.0, soft_max=1.0, subtype='FACTOR',
        description="How much speed survives an impact along the surface normal")
    adhesion: FloatProperty(
        name="Adhesion", default=0.0, min=0.0, soft_max=1.0, unit='LENGTH',
        description="Extra distance over which an EXISTING contact is kept "
                    "alive, so the point clings and peels away instead of "
                    "releasing the moment it clears the surface")

    taper_stiffness: BoolProperty(
        name="Taper Stiffness", default=False,
        description="Scale Stiffness along the chain using the scene's Root/Tip "
                    "taper instead of using one flat value")
    taper_damping: BoolProperty(
        name="Taper Damping", default=False,
        description="Scale Damping along the chain using the scene's Root/Tip "
                    "taper instead of using one flat value")


class MadiJiggleBone(PropertyGroup):
    """Per-bone settings: the two points, plus what belongs to the bone."""

    tip: PointerProperty(type=MadiJigglePoint)
    root: PointerProperty(type=MadiJigglePoint)

    chain: BoolProperty(
        name="Chain", default=True,
        description="Let this bone's corrections push back into its parent, so "
                    "a whole chain reacts instead of only the last bone")
    blend: FloatProperty(
        name="Blend", default=1.0, min=0.0, max=1.0, subtype='FACTOR',
        description="0 is the animation untouched, 1 is full simulation")

    cone_limit: FloatProperty(
        name="Cone Limit", default=math.pi, min=0.0, max=math.pi,
        subtype='ANGLE',
        description="How far the bone may swing away from the direction the "
                    "animation gives it, in any direction")
    use_axis_limits: BoolProperty(
        name="Per-Axis Limits", default=False,
        description="Limit the two swing planes separately instead of using one "
                    "cone — for things that should only move one way, like a "
                    "fin or an eyelid")
    limit_x: FloatProperty(
        name="Limit X", default=math.pi / 2, min=0.0, max=math.pi,
        subtype='ANGLE', description="Swing limit around the bone's local X")
    limit_z: FloatProperty(
        name="Limit Z", default=math.pi / 2, min=0.0, max=math.pi,
        subtype='ANGLE', description="Swing limit around the bone's local Z")

    max_drift: FloatProperty(
        name="Max Drift", default=0.0, min=0.0, soft_max=1.0, unit='LENGTH',
        description="How far a floating bone's root may leave the animated "
                    "position. 0 is unlimited")

    lateral: BoolProperty(
        name="Lateral Links", default=False,
        description="Let this bone be linked sideways to bones at the same "
                    "depth in neighbouring chains, so a skirt or a hair bunch "
                    "moves as one sheet instead of passing through itself")


class MadiJiggleState(PropertyGroup):
    """Live simulation state. Saved in the .blend so a scrub-free playback
    survives a save, and so the disk cache has something to serialise."""

    # Stored as PLAIN 16-float vectors, row-major, never subtype='MATRIX':
    # that subtype fills a flat assignment column-major, so a matrix written
    # the obvious way comes back transposed. `_store_matrix`/`_to_matrix` are
    # the only things that touch these.
    pose_matrix: FloatVectorProperty(size=16)
    valid: BoolProperty(default=False)

    tip: FloatVectorProperty(size=3, subtype='XYZ')
    tip_prev: FloatVectorProperty(size=3, subtype='XYZ')
    tip_vel: FloatVectorProperty(size=3, subtype='XYZ')

    root: FloatVectorProperty(size=3, subtype='XYZ')
    root_prev: FloatVectorProperty(size=3, subtype='XYZ')
    root_vel: FloatVectorProperty(size=3, subtype='XYZ')

    # Contacts are remembered in the COLLIDER's local space, which is what
    # makes friction work against an animated collider for free.
    # What the bone's basis was before we touched it, and what we left it as.
    # Together they are how the solver tells "the animation moved this bone"
    # apart from "this is my own result from last frame" — see _Target.
    pre_basis: FloatVectorProperty(size=16,
                                   default=[1, 0, 0, 0, 0, 1, 0, 0,
                                            0, 0, 1, 0, 0, 0, 0, 1])
    post_basis: FloatVectorProperty(size=16,
                                    default=[1, 0, 0, 0, 0, 1, 0, 0,
                                             0, 0, 1, 0, 0, 0, 0, 1])

    tip_hit_object: StringProperty(default="")
    tip_hit_local: FloatVectorProperty(size=3, subtype='XYZ')
    tip_hit_normal: FloatVectorProperty(size=3, subtype='XYZ')
    root_hit_object: StringProperty(default="")
    root_hit_local: FloatVectorProperty(size=3, subtype='XYZ')
    root_hit_normal: FloatVectorProperty(size=3, subtype='XYZ')

    guard_damp: FloatProperty(default=0.0)


class MadiJiggleObject(PropertyGroup):
    """Per-armature switches."""

    mute: BoolProperty(
        name="Mute", default=False,
        description="Stop simulating this armature entirely")
    freeze: BoolProperty(
        name="Freeze", default=False,
        description="Set automatically after a bake so the live solver stops "
                    "overwriting the keys it just wrote. Clear it to go back to "
                    "live simulation")
    self_collide: BoolProperty(
        name="Self Collision", default=False,
        description="Stop this armature's simulated bones passing through each "
                    "other. Each bone is a capsule of its point Radius")
    self_margin: FloatProperty(
        name="Self Margin", default=0.0, min=0.0, soft_max=0.1, unit='LENGTH',
        description="Extra clearance held between two bones on top of their radii")


class MadiJiggleScene(PropertyGroup):
    """Scene-wide solver settings."""

    enabled: BoolProperty(
        name="Enable Jiggle", default=True,
        description="Master switch for the whole solver")
    quality: IntProperty(
        name="Quality", default=2, min=1, max=64,
        description="Relaxation passes per substep. Higher holds chains and "
                    "length constraints together better and costs linearly more")
    substeps: IntProperty(
        name="Substeps", default=1, min=1, max=16,
        description="Simulation steps per frame. Raise this for stiff or fast "
                    "setups — it also makes playback match a bake, because the "
                    "step size stops depending on the frame rate alone")
    loop: BoolProperty(
        name="Loop Physics", default=False,
        description="Carry the simulation across the end of the timeline when "
                    "playback wraps, instead of resetting. A manual rewind "
                    "still resets")
    preroll: IntProperty(
        name="Preroll", default=0, min=0, max=500,
        description="Settle the simulation for this many steps before the first "
                    "frame, so it does not start from a dead stiff pose")
    simulate_in_render: BoolProperty(
        name="Simulate While Rendering", default=True,
        description="Keep simulating during a render. Turn this off and a "
                    "render shows only what has been baked")

    taper_root: FloatProperty(
        name="Taper Root", default=1.0, min=0.0, soft_max=4.0,
        description="Stiffness/Damping multiplier at the base of a chain, for "
                    "points with Taper switched on")
    taper_tip: FloatProperty(
        name="Taper Tip", default=1.0, min=0.0, soft_max=4.0,
        description="Stiffness/Damping multiplier at the end of a chain, for "
                    "points with Taper switched on")

    guard: BoolProperty(
        name="Safety Guard", default=True,
        description="Add damping automatically when the character is teleported "
                    "or spun hard, so the rig does not explode on a cut")
    guard_move: FloatProperty(
        name="Guard Move Speed", default=2.0, min=0.0, soft_max=50.0,
        description="World units per frame the armature may move before extra "
                    "damping kicks in")
    guard_spin: FloatProperty(
        name="Guard Spin Speed", default=1.5, min=0.0, soft_max=20.0,
        subtype='ANGLE',
        description="Rotation per frame the armature may turn before extra "
                    "damping kicks in")
    guard_strength: FloatProperty(
        name="Guard Strength", default=8.0, min=0.0, soft_max=50.0,
        description="How much damping the guard adds once it triggers")

    lateral: BoolProperty(
        name="Lateral Links", default=False,
        description="Solve side-to-side links between neighbouring chains. "
                    "Unlike a viewport-only stabiliser this runs inside the "
                    "solver, so it bakes and is frame-rate independent")
    lateral_stiffness: FloatProperty(
        name="Link Stiffness", default=0.5, min=0.0, max=1.0, subtype='FACTOR',
        description="How hard linked bones hold their spacing")
    lateral_tolerance: FloatProperty(
        name="Link Tolerance", default=0.1, min=0.0, max=1.0, subtype='FACTOR',
        description="Fraction of the rest spacing a link may change before it "
                    "pulls back — slack, so a sheet can still fold")
    lateral_reach: FloatProperty(
        name="Link Reach", default=2.5, min=1.0, soft_max=8.0,
        description="How far a link may reach, as a multiple of the average "
                    "spacing at that depth. Relative on purpose, so it behaves "
                    "the same on a doll and on a giant")

    cache: BoolProperty(
        name="Use Cache", default=False,
        description="Write each simulated frame to disk so scrubbing backwards "
                    "replays instead of restarting. The cache records the "
                    "settings it was made with and ignores itself when they "
                    "change")
    cache_dir: StringProperty(
        name="Cache Folder", default=CACHE_DIR, subtype='DIR_PATH',
        description="Where cached frames are written")

    # runtime bookkeeping — not user settings
    last_frame: IntProperty(default=-999999)
    reset_pending: BoolProperty(default=True)


# ---------------------------------------------------------------------------
# Serialisation
#
# The command layer walks these tables instead of naming fields one at a time,
# so a tunable added above reaches the bridge with no second edit. `object` and
# `collection` entries cross the wire as plain names.

POINT_FIELDS = (
    ("enable", "value"), ("mute", "value"),
    ("mass", "value"), ("stiffness", "value"), ("damping", "value"),
    ("slack", "value"), ("gravity", "value"),
    ("wind_object", "object"), ("wind", "value"),
    ("collider_mode", "value"), ("collider_object", "object"),
    ("collider_collection", "collection"),
    ("radius", "value"), ("friction", "value"), ("bounce", "value"),
    ("adhesion", "value"),
    ("taper_stiffness", "value"), ("taper_damping", "value"),
)

BONE_FIELDS = (
    ("chain", "value"), ("blend", "value"),
    ("cone_limit", "value"), ("use_axis_limits", "value"),
    ("limit_x", "value"), ("limit_z", "value"),
    ("max_drift", "value"), ("lateral", "value"),
)

OBJECT_FIELDS = (
    ("mute", "value"), ("freeze", "value"),
    ("self_collide", "value"), ("self_margin", "value"),
)

SCENE_FIELDS = (
    ("enabled", "value"), ("quality", "value"), ("substeps", "value"),
    ("loop", "value"), ("preroll", "value"), ("simulate_in_render", "value"),
    ("taper_root", "value"), ("taper_tip", "value"),
    ("guard", "value"), ("guard_move", "value"), ("guard_spin", "value"),
    ("guard_strength", "value"),
    ("lateral", "value"), ("lateral_stiffness", "value"),
    ("lateral_tolerance", "value"), ("lateral_reach", "value"),
    ("cache", "value"), ("cache_dir", "value"),
)


def _read_fields(group, fields):
    out = {}
    for name, kind in fields:
        val = getattr(group, name)
        if kind in ("object", "collection"):
            out[name] = val.name if val else None
        elif isinstance(val, (bool, int, str)):
            # str covers the enums (collider_mode) and the paths (cache_dir);
            # bool must be tested before int, since bool IS an int.
            out[name] = val
        else:
            out[name] = float(val)
    return out


def _write_fields(group, fields, data):
    """Only keys actually present are written, so an app build that predates a
    setting still sends a valid request."""
    written = 0
    kinds = dict(fields)
    for name, val in (data or {}).items():
        kind = kinds.get(name)
        if kind is None:
            continue
        try:
            if kind == "object":
                setattr(group, name, bpy.data.objects.get(val) if val else None)
            elif kind == "collection":
                setattr(group, name, bpy.data.collections.get(val) if val else None)
            else:
                setattr(group, name, val)
        except (TypeError, ValueError, AttributeError):
            continue          # a bad value must never take the whole request down
        written += 1
    return written


def bone_settings(pb):
    d = _read_fields(pb.madi_jiggle, BONE_FIELDS)
    d["tip"] = _read_fields(pb.madi_jiggle.tip, POINT_FIELDS)
    d["root"] = _read_fields(pb.madi_jiggle.root, POINT_FIELDS)
    d["connected"] = bool(pb.bone.use_connect)
    return d


def apply_bone_settings(pb, data):
    n = _write_fields(pb.madi_jiggle, BONE_FIELDS, data)
    n += _write_fields(pb.madi_jiggle.tip, POINT_FIELDS, data.get("tip"))
    n += _write_fields(pb.madi_jiggle.root, POINT_FIELDS, data.get("root"))
    return n


def effective_stiffness(scene, stiffness):
    """What the solver will actually use, and the ceiling it is capped at.

    Surfaced so the app can say 'raise Substeps' instead of leaving the user
    dragging a Stiffness slider that stopped doing anything two thirds ago."""
    dt = _timestep(scene)
    ceiling = MAX_K_DT2 / (dt * dt) if dt > EPS else float("inf")
    return min(stiffness, ceiling), ceiling


def _timestep(scene):
    fps = scene.render.fps / max(1e-6, scene.render.fps_base)
    sub = max(1, scene.madi_jiggle.substeps)
    return 1.0 / max(1e-6, fps * sub)


# ---------------------------------------------------------------------------
# Target collection

def _flatten(m):
    """A Matrix as 16 floats, row-major."""
    return [v for row in m for v in row]


def _to_matrix(flat):
    """16 stored floats back into a Matrix. Exact inverse of _flatten."""
    f = list(flat)
    return Matrix((f[0:4], f[4:8], f[8:12], f[12:16]))


def _store_matrix(state, name, m):
    setattr(state, name, _flatten(m))


def _same_matrix(m, flat, tol=1e-6):
    return all(abs(x - y) <= tol for x, y in zip(_flatten(m), list(flat)))


class _Target:
    """One bone's solver context for one frame. Everything the solver reads
    about the ANIMATED pose is captured here, once, before anything is written."""

    __slots__ = ("pb", "ob", "cfg", "state", "depth", "ancestor",
                 "anim_mat", "anim_inv", "anim_head", "anim_tail", "rest_len",
                 "do_tip", "do_root", "sim_mat", "taper", "children",
                 "basis_anim", "raw", "raw_inv", "delta_inv")

    def __init__(self, pb, ob):
        self.pb = pb
        self.ob = ob
        self.cfg = pb.madi_jiggle
        self.state = pb.madi_jiggle_state
        self.ancestor = None
        self.children = []
        self.depth = 0
        self.taper = 0.0
        mw = ob.matrix_world

        # THE ANIMATED POSE, WITH OUR OWN LAST RESULT TAKEN BACK OUT.
        #
        # A bone with no F-curves keeps whatever basis was last written to it,
        # so simply reading the pose would feed the solver its own previous
        # output: the spring target drifts along with the result and the
        # restoring force quietly disappears, while any stretch scale
        # multiplies itself once per frame until the rig leaves the solar
        # system. (Measured: slack 0.8 reached 1e16 units in 40 frames.)
        #
        # `matrix = A @ basis` where A depends only on ancestors, so A can be
        # recovered exactly and re-composed with the ANIMATED basis. Which
        # basis that is comes from a comparison, not a guess: if the basis is
        # still what we left last frame, the animation did not touch this bone
        # and the pre-simulation value is the animated one; if it changed, the
        # animation re-evaluated and the current value already IS the animated
        # one. No depsgraph round-trip either way.
        basis_now = pb.matrix_basis.copy()
        state = self.state
        if _same_matrix(basis_now, state.post_basis):
            self.basis_anim = _to_matrix(state.pre_basis)
        else:
            self.basis_anim = basis_now
        # `raw` has THIS bone's own delta removed. Its ancestors' deltas are
        # still in the prefix — _finalise_targets strips those, once the
        # ancestor links exist.
        self.raw = mw @ pb.matrix @ basis_now.inverted_safe() @ self.basis_anim
        self.raw_inv = self.raw.inverted_safe()
        self.delta_inv = (self.basis_anim.inverted_safe()
                          @ basis_now).inverted_safe()
        self.anim_mat = self.raw
        self.anim_inv = self.raw_inv
        self.anim_head = self.raw.translation.copy()
        self.anim_tail = self.raw @ Vector((0.0, pb.bone.length, 0.0))
        self.rest_len = (self.anim_tail - self.anim_head).length
        cfg = self.cfg
        self.do_tip = cfg.tip.enable and not cfg.tip.mute
        self.do_root = (cfg.root.enable and not cfg.root.mute
                        and not pb.bone.use_connect)
        self.sim_mat = self.anim_mat.copy()


def bone_is_active(pb):
    """Does this bone take part in the simulation at all?

    Mute is deliberately NOT part of this: a muted bone still has to be
    collected so it snaps back to the animated pose and so the chain below it
    keeps a parent to hang from."""
    cfg = pb.madi_jiggle
    return bool(cfg.tip.enable or (cfg.root.enable and not pb.bone.use_connect))


def collect_targets(ob):
    """Every simulated bone on one armature, parents first.

    Hierarchy order matters twice over — a child reads its ancestor's simulated
    matrix, and a correction is pushed back into the parent — and
    `pose.bones` is in creation order, which is not it."""
    targets = []
    by_name = {}
    for pb in ob.pose.bones:
        if bone_is_active(pb):
            t = _Target(pb, ob)
            targets.append(t)
            by_name[pb.name] = t

    for t in targets:
        parent = t.pb.parent
        while parent is not None:
            anc = by_name.get(parent.name)
            # Skip straight over ancestors that are not simulated (or are
            # muted): the chain may legitimately have gaps, and a muted bone
            # is by definition still sitting on the animation.
            if anc is not None and (anc.do_tip or anc.do_root):
                t.ancestor = anc
                anc.children.append(t)
                break
            parent = parent.parent

    for t in targets:
        d, anc = 0, t.ancestor
        while anc is not None:
            d += 1
            anc = anc.ancestor
        t.depth = d
    targets.sort(key=lambda x: x.depth)
    _finalise_targets(targets)

    # Taper runs 0 at the base of a chain to 1 at its far end, so a tapered
    # value means the same thing on a 3-bone chain and a 30-bone one.
    for t in targets:
        length = _chain_length(t)
        t.taper = (t.depth / length) if length > 0 else 0.0
    return targets


def _finalise_targets(targets):
    """Strip each bone's ANCESTORS' deltas out of its snapshot. Parents first.

    Removing a bone's own delta is not enough. Its evaluated matrix is built
    from the chain above it, so it also carries whatever the parent was moved
    by last frame — and `_chain_base` then applies the parent's motion a SECOND
    time, on top of the copy already baked into the child. The result compounds
    down the chain, one extra multiplication per bone per frame; with stretch
    in the delta it reached 1e15 units inside forty frames.

    Ancestors further up cancel on their own: both matrices in
    `anc.raw_inv @ raw` share them. Only the immediate simulated ancestor's own
    delta survives that cancellation, because its own matrix had it removed —
    so exactly one delta has to be divided back out, and it is known exactly.
    """
    for t in targets:
        anc = t.ancestor
        if anc is None:
            continue
        t.anim_mat = anc.anim_mat @ anc.delta_inv @ (anc.raw_inv @ t.raw)
        t.anim_inv = t.anim_mat.inverted_safe()
        t.anim_head = t.anim_mat.translation.copy()
        t.anim_tail = t.anim_mat @ Vector((0.0, t.pb.bone.length, 0.0))
        t.rest_len = (t.anim_tail - t.anim_head).length
    for t in targets:
        t.sim_mat = t.anim_mat.copy()


def _chain_length(t):
    """Depth of the deepest descendant, measured from this chain's own base."""
    base = t
    while base.ancestor is not None:
        base = base.ancestor
    deepest = [0]

    def walk(node, d):
        deepest[0] = max(deepest[0], d)
        for c in node.children:
            walk(c, d + 1)
    walk(base, 0)
    return deepest[0]


def _taper(scene, t, use):
    if not use:
        return 1.0
    s = scene.madi_jiggle
    return s.taper_root + (s.taper_tip - s.taper_root) * t.taper


# ---------------------------------------------------------------------------
# Chain composition

def _chain_base(t):
    """Where this bone sits once its ancestors have moved, with its OWN
    animated local transform still in place.

    This is the target the springs pull toward, so animation drives the
    simulation for free: pose the rig and the rest position follows."""
    anc = t.ancestor
    if anc is None:
        return t.anim_mat
    return anc.sim_mat @ (anc.anim_inv @ t.anim_mat)


def _head_of(t, base):
    return Vector(t.state.root) if t.do_root else base.translation.copy()


def _rest_tip(t, base):
    """The animated rest position of the tip, chained off the simulated parent."""
    head = _head_of(t, base)
    direction = base.col[1].xyz
    if direction.length < EPS:
        direction = Vector((0.0, 1.0, 0.0))
    return head + direction.normalized() * t.rest_len


def _sim_matrix(t):
    """Points -> the bone's simulated world matrix, for children to chain off.

    Carries NO scale by construction: stretch belongs in the pose write, and a
    scaled matrix in the chain shears everything below it."""
    base = _chain_base(t)
    local = Matrix.Translation(base.inverted_safe() @ _head_of(t, base))
    m = base @ local
    if t.do_tip:
        v = m.inverted_safe() @ Vector(t.state.tip)
        if v.length > EPS:
            m = m @ v.to_track_quat('Y', 'Z').to_matrix().to_4x4()
    t.sim_mat = m
    return m


def _local_delta(t):
    """The local transform to add on top of the bone's animated basis.

    Aiming happens in the bone's OWN space, which is what preserves roll: the
    target vector sits near local +Y, so the track quaternion is near identity
    and the animated twist survives. Doing the same aim in world space would
    throw the roll away, and no amount of tuning gets it back."""
    base = _chain_base(t)
    local = Matrix.Translation(base.inverted_safe() @ _head_of(t, base))
    if not t.do_tip:
        return local
    m = base @ local
    v = m.inverted_safe() @ Vector(t.state.tip)
    if v.length < EPS:
        return local
    rot = v.to_track_quat('Y', 'Z').to_matrix().to_4x4()
    scale = Matrix.Identity(4)
    if t.cfg.tip.slack >= 0.01 and t.rest_len > EPS:
        world_len = (Vector(t.state.tip) - _head_of(t, base)).length
        sy = max(0.1, min(10.0, world_len / t.rest_len))
        scale = Matrix.Diagonal((1.0, sy, 1.0, 1.0))
    return local @ rot @ scale


# ---------------------------------------------------------------------------
# Force fields
#
# Read straight off a real Blender force field so wind is tuned in Blender's
# own physics panel rather than in a second set of sliders that means something
# subtly different.

def _field_accel(point_cfg, pos, ref_dir, mass, frame):
    """Acceleration from the assigned force field (already divided by mass)."""
    ob = point_cfg.wind_object
    if ob is None or ob.field is None:
        return Vector((0.0, 0.0, 0.0))
    field = ob.field
    mult = point_cfg.wind
    mw = ob.matrix_world

    if field.type == 'WIND':
        direction = mw.col[2].xyz.normalized()
        # A bone broadside to the wind catches more of it than one edge-on.
        # `wind_factor` is the field's own Wind Factor, NOT apply_to_rotation —
        # that one is a bool and reads as 1.0, which silently makes every rig
        # fully shielded end-on.
        shielding = 1.0 - getattr(field, "wind_factor", 0.0) * abs(
            direction.dot(ref_dir))
        return direction * (field.strength * mult * shielding / mass)

    if field.type == 'TURBULENCE':
        size = max(0.01, field.size)
        # mathutils has no 4D noise, so time is faked by walking the Z sample
        # with the frame number. It correlates the time axis with world Z,
        # which is visible only if you go looking for it.
        sample = pos / size + Vector((0.0, 0.0, frame * 0.1))
        return noise.noise_vector(sample) * (field.strength * mult / mass)

    if field.type == 'VORTEX':
        local = mw.inverted_safe() @ pos
        radial = Vector((local.x, local.y, 0.0))
        dist = radial.length
        if dist < EPS:
            return Vector((0.0, 0.0, 0.0))
        radial /= dist
        tangent = Vector((-radial.y, radial.x, 0.0))
        vec = tangent - radial * field.inflow
        world = (mw.to_3x3() @ vec).normalized()
        return world * (field.strength * mult / max(dist, 0.1) / mass)

    return Vector((0.0, 0.0, 0.0))


# ---------------------------------------------------------------------------
# Analytic primitives
#
# Each is a unit shape scaled by the collider object's own scale, matching
# Blender's default primitive conventions, and each returns (closest point,
# outward normal) in the object's local space. Roughly an order of magnitude
# cheaper than closest_point_on_mesh and they need no mesh at all — an Empty
# is a perfectly good collider.

def _nearest_sphere(p):
    length = p.length
    n = (p / length) if length > EPS else Vector((0.0, 0.0, 1.0))
    return n.copy(), n


def _nearest_box(p):
    if abs(p.x) <= 1.0 and abs(p.y) <= 1.0 and abs(p.z) <= 1.0:
        gaps = (1.0 - abs(p.x), 1.0 - abs(p.y), 1.0 - abs(p.z))
        axis = gaps.index(min(gaps))
        n = Vector((0.0, 0.0, 0.0))
        n[axis] = 1.0 if p[axis] >= 0.0 else -1.0
        q = p.copy()
        q[axis] = n[axis]
        return q, n
    q = Vector((max(-1.0, min(1.0, p.x)),
                max(-1.0, min(1.0, p.y)),
                max(-1.0, min(1.0, p.z))))
    n = p - q
    return q, (n.normalized() if n.length > EPS else Vector((0.0, 0.0, 1.0)))


def _nearest_cylinder(p):
    radial = Vector((p.x, p.y, 0.0))
    dist = radial.length
    rdir = (radial / dist) if dist > EPS else Vector((1.0, 0.0, 0.0))
    if dist <= 1.0 and abs(p.z) <= 1.0:
        if (1.0 - dist) <= (1.0 - abs(p.z)):
            return (Vector((rdir.x, rdir.y, p.z)),
                    Vector((rdir.x, rdir.y, 0.0)))
        zs = 1.0 if p.z >= 0.0 else -1.0
        return Vector((p.x, p.y, zs)), Vector((0.0, 0.0, zs))
    q = Vector((rdir.x * min(dist, 1.0), rdir.y * min(dist, 1.0),
                max(-1.0, min(1.0, p.z))))
    n = p - q
    return q, (n.normalized() if n.length > EPS else Vector((0.0, 0.0, 1.0)))


def _nearest_capsule(p):
    axis_pt = Vector((0.0, 0.0, max(-1.0, min(1.0, p.z))))
    d = p - axis_pt
    length = d.length
    n = (d / length) if length > EPS else Vector((1.0, 0.0, 0.0))
    return axis_pt + n, n


_PRIM_FUNCS = {
    'SPHERE': _nearest_sphere,
    'BOX': _nearest_box,
    'CYLINDER': _nearest_cylinder,
    'CAPSULE': _nearest_capsule,
}


def _closest_on_collider(ob, mode, pos, dg):
    """(world point, world normal) on `ob`, or None if it has no surface."""
    mw = ob.matrix_world
    local = mw.inverted_safe() @ pos
    if mode in _PRIMITIVES:
        q, n = _PRIM_FUNCS[mode](local)
    else:
        if ob.type != 'MESH':
            return None
        ev = ob.evaluated_get(dg)
        try:
            ok, q, n, _idx = ev.closest_point_on_mesh(local)
        except (RuntimeError, ValueError):
            return None
        if not ok:
            return None
    normal_mat = mw.to_3x3().inverted_safe().transposed()
    world_n = normal_mat @ n
    if world_n.length < EPS:
        return None
    return mw @ q, world_n.normalized()


def _collider_objects(point_cfg):
    mode = point_cfg.collider_mode
    if mode == 'NONE':
        return []
    if mode == 'COLLECTION':
        coll = point_cfg.collider_collection
        if coll is None:
            return []
        return [o for o in coll.all_objects if o.type == 'MESH']
    ob = point_cfg.collider_object
    return [ob] if ob is not None else []


def _resolve_contact(point_cfg, state, prefix, pos, vel, dg, bounce_on):
    """Push the point out of whatever it is touching, and remember the contact.

    Returns (position, velocity). Friction is POSITIONAL — the point is pulled
    back toward where it last touched — rather than a force on the velocity.
    That cannot model kinetic friction, but it is stable at any step size and
    it is what makes a value of 1.0 mean exactly 'stays put'."""
    mode = point_cfg.collider_mode
    if mode == 'NONE':
        return pos, vel
    colliders = _collider_objects(point_cfg)
    if not colliders:
        return pos, vel

    radius = point_cfg.radius
    adhesion = point_cfg.adhesion
    held = getattr(state, prefix + "_hit_object")

    for ob in colliders:
        found = _closest_on_collider(ob, mode, pos, dg)
        if found is None:
            continue
        hit, normal = found
        away = pos - hit
        dist = away.length
        behind = away.dot(normal) < 0.0
        clinging = bool(held == ob.name and dist < radius + adhesion)
        if not (behind or dist < radius or clinging):
            continue

        # Behind the surface, the only trustworthy way out is the surface
        # normal; outside it, the direction from the surface to the point is
        # better, because it is right in a concave corner too.
        out = normal if behind or dist < EPS else away.normalized()
        pos = hit + out * radius

        if bounce_on:
            along = vel.dot(out)
            if along < 0.0:
                vel = vel - out * (along * (1.0 + point_cfg.bounce))

        if held == ob.name and point_cfg.friction > 0.0:
            last = ob.matrix_world @ Vector(getattr(state, prefix + "_hit_local"))
            pos = pos.lerp(last, point_cfg.friction)

        setattr(state, prefix + "_hit_object", ob.name)
        setattr(state, prefix + "_hit_local", ob.matrix_world.inverted_safe() @ pos)
        setattr(state, prefix + "_hit_normal", out)
        return pos, vel

    setattr(state, prefix + "_hit_object", "")
    return pos, vel


# ---------------------------------------------------------------------------
# Angle limits
#
# ⚠ These are applied in _integrate AND AGAIN after the relaxation passes. The
# solver has no idea they exist and will happily drag the point straight back
# out of the cone, so a limit applied only once measures out at nothing like
# the number the user typed.

def _clamp_swing(cfg, base, head, pos, rest_len):
    if cfg.cone_limit >= math.pi - 1e-4 and not cfg.use_axis_limits:
        return pos

    rest_dir = base.col[1].xyz
    if rest_dir.length < EPS:
        return pos
    rest_dir = rest_dir.normalized()
    current = pos - head
    if current.length < EPS:
        return pos
    length = current.length
    cur_dir = current / length

    if cfg.cone_limit < math.pi - 1e-4:
        dot = max(-1.0, min(1.0, rest_dir.dot(cur_dir)))
        angle = math.acos(dot)
        if angle > cfg.cone_limit:
            axis = rest_dir.cross(cur_dir)
            if axis.length < EPS:
                # Exactly 180 degrees away: any perpendicular axis will do.
                axis = rest_dir.orthogonal()
            cur_dir = (Quaternion(axis.normalized(), cfg.cone_limit)
                       @ rest_dir).normalized()
            length = rest_len
            current = cur_dir * length

    if cfg.use_axis_limits:
        basis = base.to_3x3().normalized()
        local = basis.inverted_safe() @ (cur_dir * length)
        ang_z = math.atan2(local.x, local.y)
        ang_x = -math.atan2(local.z, local.y)
        ang_z = max(-cfg.limit_z, min(cfg.limit_z, ang_z))
        ang_x = max(-cfg.limit_x, min(cfg.limit_x, ang_x))
        rebuilt = Vector((math.sin(ang_z) * math.cos(ang_x),
                          math.cos(ang_z) * math.cos(ang_x),
                          -math.sin(ang_x)))
        current = basis @ (rebuilt * rest_len)

    return head + current


# ---------------------------------------------------------------------------
# Anchors
#
# A Damped Track / Track To / Locked Track constraint on the bone doubles as a
# pin: the point is drawn toward the constraint's own target by the
# constraint's own influence. Real feature, zero extra settings to explain.

_ANCHOR_TYPES = ('DAMPED_TRACK', 'TRACK_TO', 'LOCKED_TRACK')


def _anchor(pb, pos):
    for con in pb.constraints:
        if con.mute or con.type not in _ANCHOR_TYPES:
            continue
        target = getattr(con, "target", None)
        if target is None:
            continue
        sub = getattr(con, "subtarget", "")
        if sub and target.type == 'ARMATURE' and sub in target.pose.bones:
            goal = target.matrix_world @ target.pose.bones[sub].head
        else:
            goal = target.matrix_world.translation.copy()
        pos = pos.lerp(goal, max(0.0, min(1.0, con.influence)))
    return pos


def has_anchor(pb):
    return any(c.type in _ANCHOR_TYPES and not c.mute
               and getattr(c, "target", None) is not None
               for c in pb.constraints)


# ---------------------------------------------------------------------------
# Integration

def _integrate(scene, t, dt, dg, frame):
    """Damping, forces, one explicit step, then limits, anchors and contact."""
    state = t.state
    cfg = t.cfg
    gravity = scene.gravity if scene.use_gravity else Vector((0.0, 0.0, 0.0))
    base = _chain_base(t)

    if t.do_root:
        point = cfg.root
        pos = Vector(state.root)
        vel = Vector(state.root_vel)
        damping = point.damping * _taper(scene, t, point.taper_damping)
        vel *= max(0.0, min(1.0, 1.0 - (damping + state.guard_damp) * dt))

        stiffness = point.stiffness * _taper(scene, t, point.taper_stiffness)
        stiffness = min(stiffness, MAX_K_DT2 / (dt * dt))
        target = base.translation
        accel = gravity * point.gravity
        accel += _field_accel(point, pos, base.col[1].xyz.normalized(),
                              point.mass, frame)
        accel += (target - pos) * stiffness

        pos = pos + vel + accel * (dt * dt)
        if cfg.max_drift > 0.0:
            drift = pos - target
            if drift.length > cfg.max_drift:
                pos = target + drift.normalized() * cfg.max_drift
        pos, vel = _resolve_contact(point, state, "root", pos, vel, dg, True)
        state.root = pos
        state.root_vel = vel
    else:
        # Not simulated: the point IS the animation, every frame.
        state.root = base.translation
        state.root_prev = base.translation
        state.root_vel = (0.0, 0.0, 0.0)

    if t.do_tip:
        point = cfg.tip
        pos = Vector(state.tip)
        vel = Vector(state.tip_vel)
        damping = point.damping * _taper(scene, t, point.taper_damping)
        vel *= max(0.0, min(1.0, 1.0 - (damping + state.guard_damp) * dt))

        stiffness = point.stiffness * _taper(scene, t, point.taper_stiffness)
        stiffness = min(stiffness, MAX_K_DT2 / (dt * dt))
        target = _rest_tip(t, base)
        head = _head_of(t, base)
        accel = gravity * point.gravity
        accel += _field_accel(point, pos, (target - head).normalized(),
                              point.mass, frame)
        accel += (target - pos) * stiffness

        pos = pos + vel + accel * (dt * dt)
        pos = _clamp_swing(cfg, base, head, pos, t.rest_len)
        pos = _anchor(t.pb, pos)
        pos, vel = _resolve_contact(point, state, "tip", pos, vel, dg, True)
        state.tip = pos
        state.tip_vel = vel
    else:
        tip = _rest_tip(t, base)
        state.tip = tip
        state.tip_prev = tip
        state.tip_vel = (0.0, 0.0, 0.0)


# ---------------------------------------------------------------------------
# Relaxation
#
# Gauss-Seidel, not Jacobi: each bone reads its ancestor's ALREADY corrected
# matrix, so a correction travels the length of a chain inside one pass instead
# of taking one pass per bone.

def _relax(scene, t, dg, dt, last_pass):
    if not t.do_tip:
        return
    quality = max(1, scene.madi_jiggle.quality)
    cfg = t.cfg
    point = cfg.tip
    base = _chain_base(t)
    head = _head_of(t, base)
    pos = Vector(t.state.tip)

    # a) spring toward the rest position, rebuilt from the parent's CURRENT
    #    simulated matrix.
    stiffness = point.stiffness * _taper(scene, t, point.taper_stiffness)
    error = _rest_tip(t, base) - pos
    # The fraction of the error taken per pass, clamped so a stiff setting can
    # never overshoot past the target and oscillate.
    share = min(1.0, stiffness * dt / quality)
    correction = error * share
    _apply_correction(scene, t, correction, base, head, last_pass)

    # b) length constraint. Slack is a LOOSENESS slider: 0 holds the bone's
    #    length exactly, 1 leaves it alone.
    pos = Vector(t.state.tip)
    span = pos - head
    if span.length > EPS and t.rest_len > EPS:
        want = head + span.normalized() * t.rest_len
        _apply_correction(scene, t, (want - pos) * (1.0 - point.slack),
                          base, head, last_pass)

    pos, _vel = _resolve_contact(point, t.state, "tip", Vector(t.state.tip),
                                 Vector(t.state.tip_vel), dg, False)
    t.state.tip = pos
    _sim_matrix(t)


def _apply_correction(scene, t, correction, base, head, last_pass):
    """Move the tip, and push the parent's share of the move back up the chain.

    The parent's share is its mass fraction, with two overrides taken from the
    studied solver because they visibly help: a bone pinned by a constraint
    gives its parent everything except its own slack, and on the final pass the
    split is the parent's slack, which settles a chain instead of leaving the
    last correction ringing between two bones."""
    anc = t.ancestor
    give = (t.cfg.chain and anc is not None and anc.do_tip)
    if not give:
        t.state.tip = Vector(t.state.tip) + correction
        return

    total = t.cfg.tip.mass + anc.cfg.tip.mass
    share = (t.cfg.tip.mass / total) if total > EPS else 0.5
    if has_anchor(t.pb):
        share = 1.0 - t.cfg.tip.slack
    elif last_pass:
        share = anc.cfg.tip.slack
    share = max(0.0, min(1.0, share))

    t.state.tip = Vector(t.state.tip) + correction * (1.0 - share)
    parent_move = correction * share
    if parent_move.length < EPS:
        return

    if t.pb.bone.use_connect:
        # Connected: the child's head IS the parent's tail, so moving the
        # parent's tail is exactly right.
        anc.state.tip = Vector(anc.state.tip) - parent_move
    else:
        # Not connected: the child hangs off the parent at an offset, so
        # dragging the parent's tail by the same vector tears them apart.
        # ROTATE the parent about its own head instead, by the angle the
        # child's midpoint swept, and hold its length to within a percent so
        # repeated corrections cannot accumulate into a squash.
        anc_base = _chain_base(anc)
        pivot = _head_of(anc, anc_base)
        mid_before = (head + Vector(t.state.tip)) * 0.5
        mid_after = mid_before - parent_move
        v1 = mid_before - pivot
        v2 = mid_after - pivot
        if v1.length < EPS or v2.length < EPS:
            return
        arm = Vector(anc.state.tip) - pivot
        ratio = max(0.99, min(1.01, v2.length / v1.length))
        anc.state.tip = pivot + (v1.rotation_difference(v2) @ arm) * ratio
    _sim_matrix(anc)


def _reclamp(t):
    """Angle limits, after the solver has had its way with the point."""
    if not t.do_tip:
        return
    cfg = t.cfg
    if cfg.cone_limit >= math.pi - 1e-4 and not cfg.use_axis_limits:
        return
    base = _chain_base(t)
    head = _head_of(t, base)
    t.state.tip = _clamp_swing(cfg, base, head, Vector(t.state.tip), t.rest_len)


# ---------------------------------------------------------------------------
# Lateral links
#
# The idea is borrowed and the implementation is not: bones at the same depth
# in neighbouring chains hold their spacing, so a skirt moves as a sheet rather
# than each panel swinging through its neighbours. The studied add-on does this
# on a 100 Hz modal timer that fights its own solver, is excluded from bakes and
# only works while the armature sits at the origin. Ours is a distance
# constraint inside the relaxation loop, so it bakes, it is deterministic, and
# it does not care where the armature is.

def _link_key(ob, targets):
    return (ob.name, tuple(sorted(t.pb.name for t in targets if t.cfg.lateral)))


def _build_links(scene, ob, targets):
    """Pair up same-depth bones. Rings are detected and closed; everything else
    is chained by proximity."""
    key = _link_key(ob, targets)
    cached = _link_cache.get(ob.name)
    if cached is not None and cached[0] == key:
        return cached[1]

    by_depth = {}
    for t in targets:
        if t.cfg.lateral and t.do_tip:
            by_depth.setdefault(t.depth, []).append(t)

    pairs = []
    reach = scene.madi_jiggle.lateral_reach
    for group in by_depth.values():
        if len(group) < 3:
            continue
        centre = Vector((0.0, 0.0, 0.0))
        for t in group:
            centre += t.anim_tail
        centre /= len(group)

        # Sort around the centroid so neighbours end up adjacent whatever
        # order the bones were created in.
        axis_x, axis_y = _ring_basis(group, centre)
        group.sort(key=lambda t: math.atan2(
            (t.anim_tail - centre).dot(axis_y),
            (t.anim_tail - centre).dot(axis_x)))

        spacing = [(group[i].anim_tail - group[i + 1].anim_tail).length
                   for i in range(len(group) - 1)]
        if not spacing:
            continue
        average = sum(spacing) / len(spacing)
        if average < EPS:
            continue
        # A closed ring shows up as a first-to-last gap no bigger than the
        # ordinary spacing. Threshold is a MULTIPLE of that spacing, never a
        # hard-coded distance — the same setup at a different scale must behave
        # the same way.
        closed = ((group[0].anim_tail - group[-1].anim_tail).length
                  <= average * reach)
        ordered = list(group)
        if closed:
            ordered.append(group[0])
        for i in range(len(ordered) - 1):
            a, b = ordered[i], ordered[i + 1]
            rest = (a.anim_tail - b.anim_tail).length
            if rest <= average * reach and rest > EPS:
                pairs.append((a.pb.name, b.pb.name, rest))

    _link_cache[ob.name] = (key, pairs)
    return pairs


def _ring_basis(group, centre):
    """Two axes spanning the plane the group roughly lies in."""
    normal = Vector((0.0, 0.0, 0.0))
    for i in range(len(group)):
        a = group[i].anim_tail - centre
        b = group[(i + 1) % len(group)].anim_tail - centre
        normal += a.cross(b)
    if normal.length < EPS:
        normal = Vector((0.0, 0.0, 1.0))
    normal.normalize()
    axis_x = normal.orthogonal().normalized()
    return axis_x, normal.cross(axis_x).normalized()


def _relax_lateral(scene, ob, targets, by_name):
    settings = scene.madi_jiggle
    pairs = _build_links(scene, ob, targets)
    if not pairs:
        return
    tolerance = settings.lateral_tolerance
    stiffness = settings.lateral_stiffness
    for name_a, name_b, rest in pairs:
        a = by_name.get(name_a)
        b = by_name.get(name_b)
        if a is None or b is None or not (a.do_tip and b.do_tip):
            continue
        pa = Vector(a.state.tip)
        pb_ = Vector(b.state.tip)
        span = pb_ - pa
        dist = span.length
        if dist < EPS:
            continue
        # Slack first: a link that pulls at every tiny deviation makes a sheet
        # rigid. Only the part of the error beyond the tolerance is corrected.
        error = dist - rest
        allowed = rest * tolerance
        if abs(error) <= allowed:
            continue
        error -= math.copysign(allowed, error)
        shift = span.normalized() * (error * 0.5 * stiffness)
        a.state.tip = pa + shift
        b.state.tip = pb_ - shift
        _sim_matrix(a)
        _sim_matrix(b)


# ---------------------------------------------------------------------------
# Self collision
#
# Capsule against capsule, head to tail. The studied version is O(n^2) with no
# broadphase at all, which is fine on a few hundred hair bones and unusable at
# the scale its marketing quotes. A uniform grid costs about twenty lines and
# removes the cliff.

def _closest_between_segments(p1, q1, p2, q2):
    """Closest points on two segments (Ericson, Real-Time Collision Detection),
    degenerate cases included."""
    d1 = q1 - p1
    d2 = q2 - p2
    r = p1 - p2
    a = d1.dot(d1)
    e = d2.dot(d2)
    f = d2.dot(r)

    if a <= EPS and e <= EPS:
        return p1, p2
    if a <= EPS:
        s = 0.0
        t = max(0.0, min(1.0, f / e))
    else:
        c = d1.dot(r)
        if e <= EPS:
            t = 0.0
            s = max(0.0, min(1.0, -c / a))
        else:
            b = d1.dot(d2)
            denom = a * e - b * b
            s = max(0.0, min(1.0, (b * f - c * e) / denom)) if denom > EPS else 0.0
            t = (b * s + f) / e
            if t < 0.0:
                t = 0.0
                s = max(0.0, min(1.0, -c / a))
            elif t > 1.0:
                t = 1.0
                s = max(0.0, min(1.0, (b - c) / a))
    return p1 + d1 * s, p2 + d2 * t


def _resolve_mutual(ob, targets):
    """Push overlapping bone capsules apart, once per frame after the solver.

    Once per frame is deliberate: run it inside the relaxation loop and it
    fights the springs and buzzes."""
    settings = ob.madi_jiggle
    active = [t for t in targets if t.do_tip and t.cfg.tip.radius > 0.0]
    if len(active) < 2:
        return
    margin = settings.self_margin

    segments = []
    for t in active:
        base = _chain_base(t)
        segments.append((t, _head_of(t, base), Vector(t.state.tip),
                         t.cfg.tip.radius))

    cell = max(EPS, 2.0 * max(s[3] for s in segments) + margin
               + sum((s[2] - s[1]).length for s in segments) / len(segments))
    grid = {}
    for idx, (_t, head, tip, radius) in enumerate(segments):
        lo = Vector((min(head.x, tip.x), min(head.y, tip.y), min(head.z, tip.z)))
        hi = Vector((max(head.x, tip.x), max(head.y, tip.y), max(head.z, tip.z)))
        pad = radius + margin
        for cx in range(int((lo.x - pad) // cell), int((hi.x + pad) // cell) + 1):
            for cy in range(int((lo.y - pad) // cell), int((hi.y + pad) // cell) + 1):
                for cz in range(int((lo.z - pad) // cell), int((hi.z + pad) // cell) + 1):
                    grid.setdefault((cx, cy, cz), []).append(idx)

    seen = set()
    for bucket in grid.values():
        for i in range(len(bucket)):
            for j in range(i + 1, len(bucket)):
                a, b = bucket[i], bucket[j]
                if (a, b) in seen:
                    continue
                seen.add((a, b))
                ta, head_a, tip_a, ra = segments[a]
                tb, head_b, tip_b, rb = segments[b]
                # Directly connected bones are MEANT to touch; including them
                # just fights the length constraints and buzzes.
                if ta.pb.parent is tb.pb or tb.pb.parent is ta.pb:
                    continue
                ca, cb = _closest_between_segments(head_a, tip_a, head_b, tip_b)
                span = cb - ca
                dist = span.length
                reach = ra + rb + margin
                if dist >= reach:
                    continue
                push = (span.normalized() if dist > EPS
                        else Vector((0.0, 0.0, 1.0))) * ((reach - dist) * 0.5)
                # A bone whose head is locked to the animation can only give at
                # the tip, so it takes the whole correction there.
                ta.state.tip = Vector(ta.state.tip) - push
                tb.state.tip = Vector(tb.state.tip) + push
                _sim_matrix(ta)
                _sim_matrix(tb)


# ---------------------------------------------------------------------------
# Writing the pose

def _write_pose(t):
    """Add the simulation on top of the animation, as a local delta.

    Blending happens HERE, on the delta, by interpolating it toward identity.
    That is why there is no rotation-mode branch anywhere in this file: a
    matrix assigned to `matrix_basis` lands in whichever channels the bone
    actually uses."""
    delta = _local_delta(t)
    blend = max(0.0, min(1.0, t.cfg.blend))
    if blend < 1.0:
        delta = Matrix.LocRotScale(
            delta.to_translation() * blend,
            Quaternion().slerp(delta.to_quaternion(), blend),
            Vector((1.0, 1.0, 1.0)).lerp(delta.to_scale(), blend))
    # Composed onto the ANIMATED basis, not onto whatever is sitting there —
    # `basis_anim` already has last frame's result taken back out, so this
    # replaces the previous delta instead of stacking on top of it.
    basis = t.basis_anim @ delta
    t.pb.matrix_basis = basis
    _store_matrix(t.state, "pre_basis", t.basis_anim)
    _store_matrix(t.state, "post_basis", basis)
    _store_matrix(t.state, "pose_matrix", t.sim_mat)
    t.state.valid = True


# ---------------------------------------------------------------------------
# Reset

def reset_target(t):
    """Snap one bone's simulation back onto the animation."""
    state = t.state
    state.tip = t.anim_tail
    state.tip_prev = t.anim_tail
    state.tip_vel = (0.0, 0.0, 0.0)
    state.root = t.anim_head
    state.root_prev = t.anim_head
    state.root_vel = (0.0, 0.0, 0.0)
    state.tip_hit_object = ""
    state.root_hit_object = ""
    state.guard_damp = 0.0
    state.valid = False
    # Both sides equal to the animated basis, so next frame's "did the
    # animation move this bone?" test starts from a clean, truthful state.
    _store_matrix(state, "pre_basis", t.basis_anim)
    _store_matrix(state, "post_basis", t.basis_anim)
    t.sim_mat = t.anim_mat.copy()


def reset_object(ob):
    """Take the simulation back off this armature.

    Only OUR contribution is removed — the bone is put back to its animated
    basis, not to identity. Clearing the basis outright would throw away the
    pose on any bone the animation is not currently keying, which looks
    exactly like the rig snapping to rest every time you scrub to frame 1."""
    for t in collect_targets(ob):
        t.pb.matrix_basis = t.basis_anim
        reset_target(t)
        _sim_matrix(t)
    _guard_last.pop(ob.name, None)


def reset_scene(scene=None):
    scene = scene or bpy.context.scene
    count = 0
    for ob in bpy.data.objects:
        if ob.type == 'ARMATURE':
            reset_object(ob)
            count += 1
    scene.madi_jiggle.last_frame = -999999
    scene.madi_jiggle.reset_pending = True
    _link_cache.clear()
    return count


# ---------------------------------------------------------------------------
# Safety guard
#
# A cut or a teleport hands the solver an enormous apparent velocity and the
# rig explodes. Measure how far the armature moved and turned since the last
# frame and add damping in proportion.

def _update_guard(scene, ob, targets):
    settings = scene.madi_jiggle
    mw = ob.matrix_world
    previous = _guard_last.get(ob.name)
    _guard_last[ob.name] = mw.copy()
    if not settings.guard or previous is None:
        for t in targets:
            t.state.guard_damp = 0.0
        return 0.0

    moved = (mw.translation - previous.translation).length
    turned = previous.to_quaternion().rotation_difference(
        mw.to_quaternion()).angle
    over = 0.0
    if settings.guard_move > EPS:
        over = max(over, moved / settings.guard_move - 1.0)
    if settings.guard_spin > EPS:
        over = max(over, turned / settings.guard_spin - 1.0)
    boost = max(0.0, over) * settings.guard_strength
    for t in targets:
        t.state.guard_damp = boost
    return boost


# ---------------------------------------------------------------------------
# Disk cache
#
# One pickle per object per frame. The studied add-on's cache has no
# invalidation of any kind — change the stiffness and it keeps serving the old
# motion with no indication anything is stale. Ours stamps every frame with a
# hash of the settings that produced it and treats a mismatch as a miss, so a
# stale cache is invisible rather than misleading.

def _settings_signature(scene, ob, targets):
    parts = [
        scene.render.fps, scene.render.fps_base,
        scene.madi_jiggle.quality, scene.madi_jiggle.substeps,
        tuple(scene.gravity) if scene.use_gravity else (0, 0, 0),
        ob.madi_jiggle.self_collide, round(ob.madi_jiggle.self_margin, 6),
        scene.madi_jiggle.lateral, scene.madi_jiggle.lateral_stiffness,
        scene.madi_jiggle.lateral_tolerance, scene.madi_jiggle.lateral_reach,
        scene.madi_jiggle.taper_root, scene.madi_jiggle.taper_tip,
    ]
    for t in targets:
        parts.append(t.pb.name)
        parts.append(json.dumps(bone_settings(t.pb), sort_keys=True,
                                default=str))
    blob = json.dumps(parts, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha1(blob).hexdigest()[:16]


def _cache_path(scene, ob, frame):
    root = bpy.path.abspath(scene.madi_jiggle.cache_dir or CACHE_DIR)
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in ob.name)
    return os.path.join(root, "%s_%06d.mjc" % (safe, frame))


# ⚠ THIS CACHE IS JSON, AND IT USED TO BE PICKLE. DO NOT PUT IT BACK.
#
# `pickle.load` runs constructors chosen by the FILE, so it is arbitrary code
# execution on anything you did not write yourself. That would be academic for a
# cache in a temp folder — but CACHE_DIR is "//madi_jiggle_cache", and `//` in
# Blender means BESIDE THE .BLEND. `cache_dir` is a scene property too, so it is
# saved inside the .blend and a hostile file can aim it wherever it ships its own
# payload. So the whole attack was: download a rig, open it, scrub the timeline.
# Marty both downloads and sells .blend files.
#
# The stored data is six 3-float vectors per bone, which JSON holds exactly. The
# signature check that follows is NOT a defence against any of this: it runs
# after the file has already been parsed, which is precisely why the parser has
# to be the safe one. Old .mjc pickles simply fail to parse and read as a cache
# miss, so the frame is recomputed and nothing breaks.

_CACHE_FORMAT = 1
_VEC_KEYS = ("tip", "tip_prev", "tip_vel", "root", "root_prev", "root_vel")


def _cache_store(scene, ob, frame, targets, signature):
    path = _cache_path(scene, ob, frame)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        payload = {"v": _CACHE_FORMAT, "sig": signature, "bones": {
            t.pb.name: [list(t.state.tip), list(t.state.tip_prev),
                        list(t.state.tip_vel), list(t.state.root),
                        list(t.state.root_prev), list(t.state.root_vel)]
            for t in targets}}
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
    except (OSError, TypeError, ValueError):
        return False
    return True


def _vec3(value):
    """Three real floats, or None. A cache file is untrusted input now."""
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        return None
    out = []
    for n in value:
        if isinstance(n, bool) or not isinstance(n, (int, float)):
            return None
        n = float(n)
        if n != n or n in (float("inf"), float("-inf")):
            return None     # a NaN in the solver poisons every later frame
        out.append(n)
    return out


def _cache_load(scene, ob, frame, targets, signature):
    path = _cache_path(scene, ob, frame)
    if not os.path.isfile(path):
        return False
    try:
        with open(path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, ValueError, UnicodeDecodeError):
        return False        # unreadable, or a pre-JSON pickle: recompute
    if not isinstance(payload, dict) or payload.get("v") != _CACHE_FORMAT:
        return False
    if payload.get("sig") != signature:
        return False          # settings moved on — a stale frame is worse than none
    bones = payload.get("bones")
    if not isinstance(bones, dict):
        return False
    # Validated in full BEFORE anything is written back, so a half-good file
    # cannot leave half the rig on cached state and half on live state.
    restored = []
    for t in targets:
        entry = bones.get(t.pb.name)
        if not isinstance(entry, (list, tuple)) or len(entry) != 6:
            return False
        vectors = [_vec3(v) for v in entry]
        if any(v is None for v in vectors):
            return False
        restored.append((t, vectors))
    for t, vectors in restored:
        for name, value in zip(_VEC_KEYS, vectors):
            setattr(t.state, name, value)
    return True


def clear_cache(scene=None):
    scene = scene or bpy.context.scene
    root = bpy.path.abspath(scene.madi_jiggle.cache_dir or CACHE_DIR)
    removed = 0
    if os.path.isdir(root):
        for name in os.listdir(root):
            if name.endswith(".mjc"):
                try:
                    os.remove(os.path.join(root, name))
                    removed += 1
                except OSError:
                    pass
    return removed


# ---------------------------------------------------------------------------
# The solve

def _advance(scene, dg, ob, targets, dt, frame):
    settings = scene.madi_jiggle
    by_name = {t.pb.name: t for t in targets}
    substeps = max(1, settings.substeps)
    quality = max(1, settings.quality)

    for step in range(substeps):
        for t in targets:
            _integrate(scene, t, dt, dg, frame)
        for t in targets:
            _sim_matrix(t)

        for pass_index in range(quality):
            last = (pass_index == quality - 1)
            for t in targets:
                _relax(scene, t, dg, dt, last)

        if settings.lateral:
            _relax_lateral(scene, ob, targets, by_name)
        if ob.madi_jiggle.self_collide and step == substeps - 1:
            _resolve_mutual(ob, targets)

        for t in targets:
            _reclamp(t)
            _sim_matrix(t)
            state = t.state
            state.tip_vel = Vector(state.tip) - Vector(state.tip_prev)
            state.tip_prev = state.tip
            state.root_vel = Vector(state.root) - Vector(state.root_prev)
            state.root_prev = state.root

    for t in targets:
        _write_pose(t)


def step_object(scene, dg, ob, frame=None):
    """One simulated frame for one armature. The entry point every caller
    uses — the frame handler, the bake and the tests all go through here, so
    playback and a bake cannot drift apart.

    ⚠ INVARIANT: call this at most once per depsgraph evaluation. The snapshot
    recovers the animated pose by dividing the current basis out of
    `PoseBone.matrix`, which is only valid while the two agree — and
    `PoseBone.matrix` does not refresh when a basis is written. Calling it
    twice without a `frame_set` (or a `view_layer.update()`) in between feeds
    the solver a matrix built from the previous basis and the result is
    garbage. The frame handler and the bake both respect this; preroll gets it
    for free by reusing one target list instead of re-snapshotting."""
    if ob.type != 'ARMATURE' or ob.madi_jiggle.mute or ob.madi_jiggle.freeze:
        return 0
    targets = collect_targets(ob)
    if not targets:
        return 0
    frame = scene.frame_current if frame is None else frame
    dt = _timestep(scene)
    _update_guard(scene, ob, targets)

    if scene.madi_jiggle.cache:
        signature = _settings_signature(scene, ob, targets)
        if _cache_load(scene, ob, frame, targets, signature):
            for t in targets:
                _sim_matrix(t)
                _write_pose(t)
            return len(targets)
        _advance(scene, dg, ob, targets, dt, frame)
        _cache_store(scene, ob, frame, targets, signature)
        return len(targets)

    _advance(scene, dg, ob, targets, dt, frame)
    return len(targets)


def _armatures(scene):
    return [ob for ob in scene.objects if ob.type == 'ARMATURE']


def _should_reset(scene):
    settings = scene.madi_jiggle
    frame = scene.frame_current
    if settings.reset_pending:
        return True
    if frame == settings.last_frame:
        return False
    backwards = frame < settings.last_frame
    at_start = frame <= scene.frame_start
    if not (backwards or at_start):
        return False
    # Loop Physics carries the state across a NATURAL wrap only. A manual
    # rewind still resets, because "I scrubbed back to look at something"
    # and "the timeline wrapped" want opposite things.
    if settings.loop and at_start and settings.last_frame >= scene.frame_end:
        return False
    return True


def solve_frame(scene, dg):
    """One frame for every armature in the scene."""
    settings = scene.madi_jiggle
    if not settings.enabled:
        return 0
    if _rendering and not settings.simulate_in_render:
        return 0

    if _should_reset(scene):
        for ob in _armatures(scene):
            reset_object(ob)
        settings.reset_pending = False
        settings.last_frame = scene.frame_current
        if settings.preroll:
            _settle(scene, dg, settings.preroll)
        return 0

    total = 0
    for ob in _armatures(scene):
        total += step_object(scene, dg, ob)
    settings.last_frame = scene.frame_current
    return total


def _settle(scene, dg, steps):
    """Run the solver in place so it starts settled instead of dead stiff."""
    for ob in _armatures(scene):
        if ob.madi_jiggle.mute or ob.madi_jiggle.freeze:
            continue
        targets = collect_targets(ob)
        if not targets:
            continue
        dt = _timestep(scene)
        for _ in range(max(0, steps)):
            _advance(scene, dg, ob, targets, dt, scene.frame_current)


# ---------------------------------------------------------------------------
# Bake
#
# Two passes, READ then WRITE, and they must stay separate.
#
# Assign the target action first and keyframe as you go, and from the second
# frame onward Blender re-evaluates that same action on top of everything else,
# so what gets captured is the previous frame's baked value rather than the
# real result. The visible symptom is the base animation quietly disappearing
# out of the bake. Reading everything first and writing afterwards — with no
# frame change during the write — cannot do that, because nothing is evaluated
# while the keys go in.

_BAKE_CHANNELS = (("location", 3), ("scale", 3))


def bake(ob=None, frame_start=None, frame_end=None, preroll=None,
         selected_only=False, action_name=None, overwrite=False):
    """Bake the simulation into keyframes on the armature."""
    global _baking
    scene = bpy.context.scene
    ob = ob or bpy.context.active_object
    if ob is None or ob.type != 'ARMATURE':
        raise JiggleError("Select an armature to bake")
    if ob.madi_jiggle.mute:
        raise JiggleError("%s is muted — nothing would be baked" % ob.name)

    start = scene.frame_start if frame_start is None else int(frame_start)
    end = scene.frame_end if frame_end is None else int(frame_end)
    if end < start:
        raise JiggleError("The end frame is before the start frame")
    preroll = scene.madi_jiggle.preroll if preroll is None else int(preroll)

    targets = collect_targets(ob)
    if not targets:
        raise JiggleError("No bones on %s have jiggle switched on" % ob.name)
    if selected_only:
        targets = [t for t in targets if _selected(t.pb)]
        if not targets:
            raise JiggleError("None of the selected bones have jiggle switched on")
    names = [t.pb.name for t in targets]

    frozen_before = ob.madi_jiggle.freeze
    original_frame = scene.frame_current
    original_action = (ob.animation_data.action
                       if ob.animation_data else None)
    _baking = True
    try:
        dg = bpy.context.evaluated_depsgraph_get()
        # ---- PASS 1: READ. The action is never touched in here.
        scene.frame_set(start)
        ob.madi_jiggle.freeze = False
        reset_object(ob)
        # Re-evaluate after the reset. The solver's snapshot assumes
        # `PoseBone.matrix` was evaluated with the basis currently on the bone,
        # and the reset just rewrote those bases without anything re-running.
        scene.frame_set(start)
        if preroll:
            live = collect_targets(ob)
            dt = _timestep(scene)
            for _ in range(preroll):
                _advance(scene, dg, ob, live, dt, start)

        captured = []
        for frame in range(start, end + 1):
            if frame != start:
                scene.frame_set(frame)
            step_object(scene, dg, ob, frame=frame)
            captured.append((frame, {
                name: (tuple(ob.pose.bones[name].location),
                       tuple(ob.pose.bones[name].rotation_quaternion),
                       tuple(ob.pose.bones[name].rotation_euler),
                       tuple(ob.pose.bones[name].rotation_axis_angle),
                       tuple(ob.pose.bones[name].scale))
                for name in names}))

        # ---- PASS 2: WRITE. No frame changes, so nothing re-evaluates.
        action = _bake_action(ob, action_name, overwrite)
        keys = 0
        for frame, snapshot in captured:
            for name, values in snapshot.items():
                pb = ob.pose.bones[name]
                loc, quat, euler, axis_angle, scale = values
                pb.location = loc
                pb.rotation_quaternion = quat
                pb.rotation_euler = euler
                pb.rotation_axis_angle = axis_angle
                pb.scale = scale
                for path, _size in _BAKE_CHANNELS:
                    pb.keyframe_insert(path, frame=frame, group=name)
                    keys += 1
                pb.keyframe_insert(_rotation_path(pb), frame=frame, group=name)
                keys += 1
        # Live simulation would now fight the keys it just wrote.
        ob.madi_jiggle.freeze = True
        return {"baked": True, "object": ob.name, "action": action.name,
                "bones": len(names), "frames": len(captured), "keys": keys,
                "frame_start": start, "frame_end": end, "preroll": preroll,
                "froze": True}
    except Exception:
        ob.madi_jiggle.freeze = frozen_before
        if ob.animation_data and original_action is not None:
            ob.animation_data.action = original_action
        raise
    finally:
        _baking = False
        scene.frame_set(original_frame)


def _rotation_path(pb):
    if pb.rotation_mode == 'QUATERNION':
        return "rotation_quaternion"
    if pb.rotation_mode == 'AXIS_ANGLE':
        return "rotation_axis_angle"
    return "rotation_euler"


def _bake_action(ob, action_name, overwrite):
    name = action_name or (BAKE_ACTION_FMT % ob.name)
    if ob.animation_data is None:
        ob.animation_data_create()
    existing = bpy.data.actions.get(name)
    if existing is not None and overwrite:
        # Cheaper and far safer than reaching into the layered-action channel
        # bags to strip curves one at a time.
        bpy.data.actions.remove(existing)
        existing = None
    if existing is None:
        existing = bpy.data.actions.new(name)
        existing.use_fake_user = True
    ob.animation_data.action = existing
    return existing


def _selected(pb):
    """Bone selection is `PoseBone.select` on 5.x and `Bone.select` on older
    builds — and on 5.2 `Bone` has no `select` at all, so this must ask, not
    assume (BLENDER_NOTES)."""
    for owner in (pb, pb.bone):
        value = getattr(owner, "select", None)
        if value is not None:
            return bool(value)
    return False


def _set_selected(pb, on):
    for owner in (pb, pb.bone):
        if hasattr(owner, "select"):
            owner.select = bool(on)
            return True
    return False


def build_cache(ob=None, frame_start=None, frame_end=None):
    """Step the whole range with caching forced on, then restore."""
    scene = bpy.context.scene
    settings = scene.madi_jiggle
    ob = ob or bpy.context.active_object
    if ob is None or ob.type != 'ARMATURE':
        raise JiggleError("Select an armature to cache")
    start = scene.frame_start if frame_start is None else int(frame_start)
    end = scene.frame_end if frame_end is None else int(frame_end)
    was = settings.cache
    original_frame = scene.frame_current
    settings.cache = True
    written = 0
    try:
        dg = bpy.context.evaluated_depsgraph_get()
        scene.frame_set(start)
        reset_object(ob)
        scene.frame_set(start)      # see the same note in bake()
        for frame in range(start, end + 1):
            scene.frame_set(frame)
            step_object(scene, dg, ob, frame=frame)
            written += 1
    finally:
        settings.cache = was
        scene.frame_set(original_frame)
    return {"cached": written, "object": ob.name,
            "frame_start": start, "frame_end": end}


# ---------------------------------------------------------------------------
# Bridge-facing operations

def _active_armature():
    ob = bpy.context.active_object
    if ob is not None and ob.type == 'ARMATURE':
        return ob
    for other in bpy.context.selected_objects:
        if other.type == 'ARMATURE':
            return other
    return None


def _resolve(armature):
    ob = bpy.data.objects.get(armature) if armature else _active_armature()
    if ob is None:
        raise JiggleError("No armature — select one in Blender")
    if ob.type != 'ARMATURE':
        raise JiggleError("%s is not an armature" % ob.name)
    return ob


def _pick_bones(ob, bones=None, selected_only=True):
    if bones:
        picked = [ob.pose.bones[n] for n in bones if n in ob.pose.bones]
        if not picked:
            raise JiggleError("None of those bones exist on %s" % ob.name)
        return picked
    if selected_only:
        picked = [pb for pb in ob.pose.bones if _selected(pb)]
        if picked:
            return picked
        raise JiggleError("No bones selected — select the bones in Pose Mode")
    return list(ob.pose.bones)


def status():
    """Everything the panel needs to draw itself once."""
    scene = bpy.context.scene
    ob = _active_armature()
    settings = _read_fields(scene.madi_jiggle, SCENE_FIELDS)
    dt = _timestep(scene)
    out = {
        "armature": ob.name if ob else None,
        "armatures": sorted(o.name for o in scene.objects
                            if o.type == 'ARMATURE'),
        "mode": bpy.context.mode,
        "scene": settings,
        "frame": scene.frame_current,
        "frame_start": scene.frame_start,
        "frame_end": scene.frame_end,
        "fps": scene.render.fps / max(1e-6, scene.render.fps_base),
        "stiffness_ceiling": (MAX_K_DT2 / (dt * dt)) if dt > EPS else 0.0,
        "collider_modes": [(i, label) for i, label, _d in COLLIDER_MODES],
        "objects": sorted(o.name for o in bpy.data.objects),
        "collections": sorted(c.name for c in bpy.data.collections),
        "fields": sorted(o.name for o in bpy.data.objects
                         if o.field is not None
                         and o.field.type in ('WIND', 'TURBULENCE', 'VORTEX')),
        "selected": [], "active": None, "object": None, "enabled_bones": 0,
    }
    if ob is None:
        return out
    out["object"] = _read_fields(ob.madi_jiggle, OBJECT_FIELDS)
    out["selected"] = [pb.name for pb in ob.pose.bones if _selected(pb)]
    out["enabled_bones"] = sum(1 for pb in ob.pose.bones if bone_is_active(pb))
    active_bone = ob.data.bones.active
    if active_bone is not None and active_bone.name in ob.pose.bones:
        out["active"] = active_bone.name
    return out


def get_settings(armature=None, bones=None):
    """Settings for the chosen bones, plus which values they all agree on.

    `common` is what the panel shows: with a mixed selection a field that
    differs comes back absent rather than showing whichever bone happened to be
    first, so pressing nothing changes nothing."""
    ob = _resolve(armature)
    picked = _pick_bones(ob, bones)
    per_bone = {pb.name: bone_settings(pb) for pb in picked}
    common = _common(list(per_bone.values()))
    return {"object": ob.name, "bones": per_bone, "common": common,
            "count": len(picked),
            "names": [pb.name for pb in picked]}


def _common(entries):
    if not entries:
        return {}
    out = {}
    for key, value in entries[0].items():
        if isinstance(value, dict):
            nested = _common([e[key] for e in entries if key in e])
            if nested:
                out[key] = nested
        elif all(key in e and e[key] == value for e in entries):
            out[key] = value
    return out


def set_settings(armature=None, bones=None, settings=None):
    ob = _resolve(armature)
    picked = _pick_bones(ob, bones)
    written = 0
    for pb in picked:
        written += apply_bone_settings(pb, settings or {})
    _link_cache.pop(ob.name, None)
    return {"object": ob.name, "bones": len(picked), "written": written}


def set_enabled(armature=None, bones=None, tip=None, root=None):
    """Switch the simulation on or off for the chosen bones' ends."""
    ob = _resolve(armature)
    picked = _pick_bones(ob, bones)
    changed = 0
    for pb in picked:
        if tip is not None:
            pb.madi_jiggle.tip.enable = bool(tip)
            changed += 1
        if root is not None:
            if pb.bone.use_connect and root:
                continue      # a connected bone's root is its parent's tip
            pb.madi_jiggle.root.enable = bool(root)
            changed += 1
        if tip or root:
            reset_bone(pb)
    _link_cache.pop(ob.name, None)
    return {"object": ob.name, "bones": len(picked), "changed": changed,
            "enabled_bones": sum(1 for p in ob.pose.bones if bone_is_active(p))}


def reset_bone(pb):
    state = pb.madi_jiggle_state
    state.valid = False
    state.tip_hit_object = ""
    state.root_hit_object = ""
    state.guard_damp = 0.0


def copy_settings(armature=None, source=None, bones=None):
    """Copy one bone's settings onto the rest of the selection."""
    ob = _resolve(armature)
    if source is None:
        active = ob.data.bones.active
        source = active.name if active is not None else None
    if not source or source not in ob.pose.bones:
        raise JiggleError("No source bone — make one bone active in Pose Mode")
    payload = bone_settings(ob.pose.bones[source])
    payload.pop("connected", None)
    targets = [pb for pb in _pick_bones(ob, bones) if pb.name != source]
    for pb in targets:
        apply_bone_settings(pb, payload)
    _link_cache.pop(ob.name, None)
    return {"object": ob.name, "source": source, "bones": len(targets)}


def select_jiggle_bones(armature=None):
    ob = _resolve(armature)
    count = 0
    for pb in ob.pose.bones:
        on = bone_is_active(pb)
        _set_selected(pb, on)
        count += int(on)
    return {"object": ob.name, "selected": count}


def list_bones(armature=None):
    """Every simulated bone on the rig, for the panel's list."""
    ob = _resolve(armature)
    rows = []
    for pb in ob.pose.bones:
        if not bone_is_active(pb):
            continue
        cfg = pb.madi_jiggle
        rows.append({
            "name": pb.name,
            "tip": cfg.tip.enable, "tip_mute": cfg.tip.mute,
            "root": cfg.root.enable and not pb.bone.use_connect,
            "root_mute": cfg.root.mute,
            "stiffness": cfg.tip.stiffness, "damping": cfg.tip.damping,
            "blend": cfg.blend, "collider": cfg.tip.collider_mode,
            "lateral": cfg.lateral,
            "selected": _selected(pb),
        })
    return {"object": ob.name, "bones": rows, "count": len(rows)}


def set_object_settings(armature=None, settings=None):
    ob = _resolve(armature)
    written = _write_fields(ob.madi_jiggle, OBJECT_FIELDS, settings or {})
    return {"object": ob.name, "written": written,
            "settings": _read_fields(ob.madi_jiggle, OBJECT_FIELDS)}


def set_scene_settings(settings=None):
    scene = bpy.context.scene
    written = _write_fields(scene.madi_jiggle, SCENE_FIELDS, settings or {})
    dt = _timestep(scene)
    return {"written": written,
            "scene": _read_fields(scene.madi_jiggle, SCENE_FIELDS),
            "stiffness_ceiling": (MAX_K_DT2 / (dt * dt)) if dt > EPS else 0.0}


# ---------------------------------------------------------------------------
# Handlers

@bpy.app.handlers.persistent
def _on_frame(scene, dg=None):
    if _baking:
        return
    try:
        solve_frame(scene, dg or bpy.context.evaluated_depsgraph_get())
    except Exception as exc:              # noqa: BLE001
        # A raising frame handler is removed by Blender and takes the whole
        # feature down silently. Report and keep the handler alive.
        print("[MadihsonNSFW] jiggle solver error: %s" % exc)


@bpy.app.handlers.persistent
def _on_render_start(*_args):
    global _rendering
    _rendering = True


@bpy.app.handlers.persistent
def _on_render_end(*_args):
    global _rendering
    _rendering = False


@bpy.app.handlers.persistent
def _on_load(*_args):
    _guard_last.clear()
    _link_cache.clear()
    for scene in bpy.data.scenes:
        scene.madi_jiggle.reset_pending = True
        scene.madi_jiggle.last_frame = -999999


_classes = (
    MadiJigglePoint,
    MadiJiggleBone,
    MadiJiggleState,
    MadiJiggleObject,
    MadiJiggleScene,
)

_HANDLERS = (
    ("frame_change_post", _on_frame),
    ("render_pre", _on_render_start),
    ("render_post", _on_render_end),
    ("render_cancel", _on_render_end),
    ("load_post", _on_load),
)


def _strip_stale_handlers():
    """Drop handlers left behind by a PREVIOUS load of this module.

    The dev reload purges `sys.modules`, so the reloaded module's functions are
    different objects from the ones still sitting in `bpy.app.handlers` — an
    identity check would not find them, and the old ones would keep firing
    against a dead module, once per frame. Match on the qualified name
    instead."""
    ours = {fn.__name__ for _name, fn in _HANDLERS}
    for name, _fn in _HANDLERS:
        handlers = getattr(bpy.app.handlers, name)
        for existing in list(handlers):
            if (getattr(existing, "__name__", None) in ours
                    and getattr(existing, "__module__", "").endswith("jiggle")):
                handlers.remove(existing)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)
    bpy.types.PoseBone.madi_jiggle = PointerProperty(type=MadiJiggleBone)
    bpy.types.PoseBone.madi_jiggle_state = PointerProperty(type=MadiJiggleState)
    bpy.types.Object.madi_jiggle = PointerProperty(type=MadiJiggleObject)
    bpy.types.Scene.madi_jiggle = PointerProperty(type=MadiJiggleScene)
    _strip_stale_handlers()
    for name, fn in _HANDLERS:
        handlers = getattr(bpy.app.handlers, name)
        if fn not in handlers:
            handlers.append(fn)


def unregister():
    _strip_stale_handlers()
    for name, fn in _HANDLERS:
        handlers = getattr(bpy.app.handlers, name)
        while fn in handlers:
            handlers.remove(fn)
    _guard_last.clear()
    _link_cache.clear()
    for attr, owner in (("madi_jiggle", bpy.types.PoseBone),
                        ("madi_jiggle_state", bpy.types.PoseBone),
                        ("madi_jiggle", bpy.types.Object),
                        ("madi_jiggle", bpy.types.Scene)):
        if hasattr(owner, attr):
            delattr(owner, attr)
    for cls in reversed(_classes):
        try:
            bpy.utils.unregister_class(cls)
        except RuntimeError:
            pass
