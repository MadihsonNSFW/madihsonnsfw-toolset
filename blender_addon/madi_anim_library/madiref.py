"""MadiRef — the viewport half. Draws the reference the app is decoding.

This module does as little as possible on purpose. It does NOT decode video, it
does NOT know what a proxy is, and it owns none of the timing arithmetic: it
maps a shared-memory segment, uploads whatever frame is in it, draws a quad,
and publishes the scene's frame and fps back into the same segment so the app
can follow along. Everything else lives app-side, so changing it never needs an
add-on push.

Why not an Image datablock and a textured plane: `Image.pixels` pushes float32
through Blender's whole image system on the main thread, and there is no
supported way to hand a GPUTexture to an Image. Drawing directly with the `gpu`
module measured ~1.2 ms per 540p frame end to end, against a main thread that is
already evaluating a 461-bone rig.

⚠ `GPUTexture` has NO update method (only `clear`/`read`) and its `data=`
argument accepts a **FLOAT buffer only** — a UBYTE buffer raises. So each new
frame builds a fresh texture from a float32 copy. The texture is cached against
the frame index, which is what stops an orbit (many redraws, same frame) from
re-uploading anything.

⚠ The ring's binary layout is duplicated from `app\\madiref\\shm.py` because the
add-on and the app ship separately and cannot import each other.
`tests\\madiref_test.py` asserts the two struct formats stay identical — if you
change one, that test is the thing that catches the other.
"""

import math
import struct

import bpy
import gpu
from gpu_extras.batch import batch_for_shader
from mathutils import Vector

# ------------------------------------------------------------ ring layout
# ⚠ MUST MATCH app\madiref\shm.py. See the module docstring.
_MAGIC = 0x4D525242          # 'MRRB'
_VERSION = 1
_HEADER_SIZE = 256
_SLOT_HEADER = 64
_H = struct.Struct("<IIIIIIIIII")
_PRODUCER = struct.Struct("<IIQiI")
_PRODUCER_OFF = 40
_CONSUMER = struct.Struct("<ifQI")
_CONSUMER_OFF = 64
_VIEW = struct.Struct("<fffffII")
_VIEW_OFF = 88
_SLOT = struct.Struct("<QII")

# The three ways the reference can be placed (Marty, 2026-08-11):
#   1 follows the viewport   2 pinned where you posed it   3 pinned to the camera
# ⚠ MODE_VIEWPORT is screen space and drawn in POST_PIXEL; the other two are
# real quads in world space, drawn in POST_VIEW, which is what gives them true
# 3D depth for free.
MODE_VIEWPORT = 0
MODE_PINNED = 1
MODE_CAMERA = 2
# Kept as an alias: the ring's `mode` field has carried 0 since the first build
# and app copies in the wild still write it.
MODE_OVERLAY = MODE_VIEWPORT

_TIMER_S = 1.0 / 60.0        # ring poll; only tags a redraw when the frame moves

# Direct manipulation in the viewport (Marty, 2026-08-11: "scalable and movable
# and rotatable"). Sliders in the app were the only way to place the overlay,
# which means alt-tabbing out of Blender to nudge your own reference.
_HANDLE_PX = 9.0             # corner grab radius
_ROTATE_GAP_PX = 26.0        # how far the rotate knob sits above the top edge
_MIN_SCALE = 0.04
_MAX_SCALE = 3.0
_SNAP_DEG = 15.0             # ctrl while rotating


class _State:
    def __init__(self):
        self.shm = None
        self.name = None
        self.width = 0
        self.height = 0
        self.slots = 0
        self.slot_bytes = 0
        self.frame_count = 0
        self.handle_px = None
        self.handle_view = None
        self.texture = None
        self.tex_frame = -1
        self.timer_on = False
        self.plane_object = ""
        self.last_drawn = -1
        self.sync_restore = None
        self.errors = 0
        # Depth: the ONLY masking mechanism (the collection-mask path was
        # removed 2026-08-11 on Marty's word — "the depth works! This is the
        # only way we going to mask the video").
        self.occlude = False
        # 0 = sit at the far plane, i.e. EVERYTHING in the scene covers it
        self.occlude_distance = 0.0
        # Where the reference lives. MODE_PINNED/MODE_CAMERA keep a world
        # matrix; MODE_VIEWPORT ignores it entirely.
        self.pinned_matrix = None
        self.locked = False
        # direct manipulation
        self.modal_running = False
        self.hover_zone = None       # 'body' | 'corner' | 'rotate' | None
        self.hover_index = -1
        # ⚠ region.as_pointer(), NEVER id(region). Blender hands back a FRESH
        # Python wrapper on every access, so `id()` differs each time and the
        # comparison in the draw handler almost never matched — which is why
        # the handles were "only sometimes visible". Same family as the
        # `==`-not-`is` trap already recorded for the picker's active_index.
        self.hover_region = 0


_S = _State()


# --------------------------------------------------------------- ring I/O

def _slot_off(i):
    return _HEADER_SIZE + i * _S.slot_bytes


def _newest(retries=3):
    """(frame_index, memoryview) of the freshest complete slot, or None.

    Seqlock: an odd sequence means the writer is mid-slot, and a sequence that
    moved while we were reading means we may have torn — both retry. A torn
    read costs one frame, which is why this is not worth a real lock.
    """
    buf = _S.shm.buf
    for _ in range(retries):
        i = _PRODUCER.unpack_from(buf, _PRODUCER_OFF)[0]
        if not 0 <= i < _S.slots:
            return None
        base = _slot_off(i)
        seq0, frame_index, _ = _SLOT.unpack_from(buf, base)
        if seq0 % 2:
            continue
        off = base + _SLOT_HEADER
        mv = buf[off:off + _S.width * _S.height * 4]
        seq1 = _SLOT.unpack_from(buf, base)[0]
        if seq1 == seq0:
            return frame_index, mv
    return None


def _view_state():
    (opacity, x, y, scale, rotation, mode,
     visible) = _VIEW.unpack_from(_S.shm.buf, _VIEW_OFF)
    return opacity, x, y, scale, rotation, mode, bool(visible)


def _write_view(x=None, y=None, scale=None, rotation=None):
    """Push a viewport-side edit back into the ring so the app mirrors it.

    Only the four things the mouse can change are writable from here — opacity,
    mode and visibility stay the app's to own, so a drag can never accidentally
    hide the reference or switch it to plane mode.
    """
    if _S.shm is None:
        return
    o, cx, cy, sc, rot, mode, vis = _view_state()
    _VIEW.pack_into(_S.shm.buf, _VIEW_OFF, o,
                    cx if x is None else float(x),
                    cy if y is None else float(y),
                    sc if scale is None else float(scale),
                    rot if rotation is None else float(rotation),
                    mode, int(vis))


def _overlay_geometry(region_w, region_h):
    """Corners of the overlay quad in region pixels, and its centre.

    ⚠ x/y are the CENTRE as a fraction of the region — scaling and rotating
    both happen about it, and a corner anchor would make the picture crawl
    across the screen as you resized it.
    """
    o, fx, fy, scale, rot, _mode, _vis = _view_state()
    w = max(region_w * scale, 8.0)
    h = w * (_S.height / float(_S.width or 1))
    cx, cy = fx * region_w, fy * region_h
    cos_r, sin_r = math.cos(rot), math.sin(rot)
    hw, hh = w * 0.5, h * 0.5
    local = ((-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh))
    corners = []
    for lx, ly in local:
        corners.append((cx + lx * cos_r - ly * sin_r,
                        cy + lx * sin_r + ly * cos_r))
    return corners, (cx, cy), (w, h), rot, o


def _to_local(px, py, centre, rot):
    """A region point in the overlay's own unrotated frame — which is what
    makes hit-testing a rotated rectangle the same problem as an upright one."""
    dx, dy = px - centre[0], py - centre[1]
    cos_r, sin_r = math.cos(-rot), math.sin(-rot)
    return dx * cos_r - dy * sin_r, dx * sin_r + dy * cos_r


def _hit_test(px, py, region_w, region_h):
    """What is under the cursor: 'rotate', 'corner', 'body' or None."""
    if _S.shm is None:
        return None, -1
    corners, centre, (w, h), rot, _o = _overlay_geometry(region_w, region_h)
    for i, (hx, hy) in enumerate(corners):
        if abs(px - hx) <= _HANDLE_PX and abs(py - hy) <= _HANDLE_PX:
            return "corner", i
    rx, ry = _rotate_handle_pos(centre, h, rot)
    if abs(px - rx) <= _HANDLE_PX and abs(py - ry) <= _HANDLE_PX:
        return "rotate", -1
    lx, ly = _to_local(px, py, centre, rot)
    if abs(lx) <= w * 0.5 and abs(ly) <= h * 0.5:
        return "body", -1
    return None, -1


def _rotate_handle_pos(centre, h, rot):
    """The rotate knob, floating above the top edge like every 2D editor."""
    d = h * 0.5 + _ROTATE_GAP_PX
    return (centre[0] - d * math.sin(rot), centre[1] + d * math.cos(rot))


def _publish_scene(scene):
    """Tell the app where the timeline is. This is what makes the app's view
    and the viewport show the same frame with no polling and no IPC."""
    fps = scene.render.fps / float(scene.render.fps_base or 1.0)
    cur = _CONSUMER.unpack_from(_S.shm.buf, _CONSUMER_OFF)
    _CONSUMER.pack_into(_S.shm.buf, _CONSUMER_OFF, int(scene.frame_current),
                        float(fps), cur[2] + 1, (cur[3] + 1) & 0xFFFFFFFF)


# ---------------------------------------------------------------- texture

def _texture_for(frame_index, mv):
    """Build (or reuse) the GPUTexture for a frame.

    Reuse is the whole point of caching on frame index: orbiting the view
    redraws many times per second on the SAME reference frame, and without this
    every one of those would re-upload the picture.
    """
    if _S.texture is not None and _S.tex_frame == frame_index:
        return _S.texture
    import numpy as np
    arr = np.frombuffer(mv, dtype=np.uint8)
    # ⚠ float32 is not a choice: GPUTexture's data= takes FLOAT buffers only.
    f = arr.astype(np.float32)         # a COPY -- nothing below views the ring
    f *= (1.0 / 255.0)
    # ⚠ `arr` is a numpy VIEW onto shared memory and counts as an exported
    # pointer. While one exists, `SharedMemory.close()` raises
    # "BufferError: cannot close exported pointers exist" and the segment stays
    # mapped — so closing the reference silently fails and the app cannot
    # unlink. Drop the view here, and the caller releases the memoryview.
    del arr
    buf = gpu.types.Buffer('FLOAT', f.size, f)
    _S.texture = gpu.types.GPUTexture((_S.width, _S.height), format='RGBA8',
                                      data=buf)
    _S.tex_frame = frame_index
    return _S.texture


# ------------------------------------- putting the scene in front of it
# ⚠ DEPTH IS THE ONLY MASKING MECHANISM. A collection-mask path existed here
# (offscreen render + a two-sampler shader punching the overlay through it) and
# was REMOVED on 2026-08-11: it never worked, and depth does the same job
# per-pixel, for free, in a state change. Do not bring it back — `docs\madiref.md`
# records what it cost.

def _depth_z_for(target_ndc):
    """The `pos.z` that lands a vertex at a chosen NDC depth.

    Measured in a live POST_PIXEL handler: the scene's depth buffer is fully
    intact (0.5005..1.0 on Marty's scene, 74k distinct values, 9% of pixels
    carrying geometry). So the reference does not need a mask at all — give the
    quad a depth just short of the far plane and Blender's own depth test puts
    every real object in front of it, perfectly shaded, for free.

    The 2D projection is `z_ndc = a*z + b` with w left at 1, so this inverts it
    from the LIVE matrix rather than assuming a fixed near/far.
    """
    proj = gpu.matrix.get_projection_matrix()
    a, b = proj[2][2], proj[2][3]
    if not a:
        return 0.0
    return (target_ndc - b) / a


# Just inside the far plane: nearer than the empty background (which sits at
# 1.0) so the reference still draws over empty space, but behind every real
# object in the scene.
_OCCLUDE_NDC = 0.9998


def _depth_z_for_distance(rv3d, metres):
    """`pos.z` that puts the reference `metres` in front of the viewer.

    Anything nearer than that covers it; anything further is covered by it.

    ⚠ The depth convention here is MEASURED, not assumed: writing a quad at a
    known NDC and reading the depth buffer back gave -0.5→0.25, 0.0→0.5,
    0.5→0.75, 0.9998→0.9999, i.e. plain `depth = (ndc + 1) / 2`. Deriving it
    from the matrices instead gave an answer that did not match the buffer at
    all, so do not "simplify" this back into matrix algebra.
    """
    from mathutils import Vector
    if rv3d is None or metres <= 0.0:
        return _depth_z_for(_OCCLUDE_NDC)
    # where the 3D pass would have put a point straight ahead at this distance
    clip = rv3d.window_matrix @ Vector((0.0, 0.0, -float(metres), 1.0))
    if not clip.w:
        return _depth_z_for(_OCCLUDE_NDC)
    depth = (clip.z / clip.w + 1.0) * 0.5
    depth = max(0.0, min(0.99999, depth))
    # ...then back through the 2D projection the overlay is drawn with
    return _depth_z_for(depth * 2.0 - 1.0)


def _quad(shader, coords, opacity, depth=False, occlude=False):
    """Draw the reference quad. `coords` are already in the target space.

    ⚠ The image arrives top-row-first and OpenGL samples bottom-up, so the V
    coordinates are flipped here. Getting this wrong shows the reference upside
    down, which looks like a decoder bug and is not one.
    """
    if occlude:
        # 2D coords + a Z chosen so the scene's own depth occludes us
        z = _depth_z_for_distance(bpy.context.region_data,
                                  _S.occlude_distance)
        coords = tuple((c[0], c[1], z) for c in coords)
    batch = batch_for_shader(
        shader, 'TRI_FAN',
        {"pos": coords,
         "texCoord": ((0, 1), (1, 1), (1, 0), (0, 0))})
    gpu.state.blend_set('ALPHA')
    if occlude:
        # ⚠ TEST depth, never WRITE it. Writing would leave the reference in
        # the depth buffer for everything drawn after us (gizmos, other
        # overlays) and they would vanish behind it.
        gpu.state.depth_test_set('LESS_EQUAL')
        gpu.state.depth_mask_set(False)
    if depth:
        gpu.state.depth_test_set('LESS_EQUAL')
        gpu.state.depth_mask_set(True)
    shader.bind()
    shader.uniform_sampler("image", _S.texture)
    shader.uniform_float("color", (1.0, 1.0, 1.0, opacity))
    batch.draw(shader)
    if depth or occlude:
        gpu.state.depth_mask_set(False)
        gpu.state.depth_test_set('NONE')
    gpu.state.blend_set('NONE')


def _prepare(for_mode):
    """Common per-draw work; returns (opacity, x, y, scale) or None to skip.

    ⚠ The mode is checked FIRST, before any ring work. Both handlers
    (POST_PIXEL and POST_VIEW) are registered at all times so switching modes
    needs no re-registration — but without this early-out, every redraw would
    read the ring and release a memoryview twice to draw once.
    """
    if _S.shm is None:
        return None
    try:
        opacity, x, y, scale, rot, mode, visible = _view_state()
        if mode != for_mode:
            return None
        if not visible or opacity <= 0.0:
            return None
        got = _newest()
        if got is None:
            return None
        frame_index, mv = got
        try:
            _texture_for(frame_index, mv)
        finally:
            # ⚠ Release the export EVERY draw, on the failure path too. A single
            # leaked memoryview is enough to make `SharedMemory.close()` raise
            # for the rest of the session.
            try:
                mv.release()
            except Exception:                        # noqa: BLE001
                pass
        _S.last_drawn = frame_index
        _publish_scene(bpy.context.scene)
        return opacity, x, y, scale, rot
    except Exception:                                # noqa: BLE001
        # A draw handler that raises spams the console every redraw and can
        # make Blender feel broken; count it and stay quiet.
        _S.errors += 1
        return None


def _draw_overlay():
    """Mode 1 — screen space, follows the viewport."""
    st = _prepare(MODE_VIEWPORT)
    if st is None:
        return
    opacity = st[0]
    region = bpy.context.region
    if region is None:
        return
    rw, rh = region.width, region.height
    corners, centre, (w, h), rot, _o = _overlay_geometry(rw, rh)
    _quad(gpu.shader.from_builtin('IMAGE_COLOR'), corners, opacity,
          occlude=_S.occlude)
    # Handles only while the cursor is on it, and never when locked.
    if (not _S.locked and _S.hover_zone
            and _S.hover_region == region.as_pointer()):
        _draw_handles(corners, centre, h, rot)


def _draw_handles(corners, centre, h, rot):
    """Outline, corner grips and the rotate knob."""
    shader = gpu.shader.from_builtin('UNIFORM_COLOR')
    gpu.state.blend_set('ALPHA')
    shader.bind()
    shader.uniform_float("color", (1.0, 1.0, 1.0, 0.55))
    batch_for_shader(shader, 'LINE_LOOP', {"pos": corners}).draw(shader)
    rx, ry = _rotate_handle_pos(centre, h, rot)
    top_mid = ((corners[3][0] + corners[2][0]) * 0.5,
               (corners[3][1] + corners[2][1]) * 0.5)
    batch_for_shader(shader, 'LINES',
                     {"pos": (top_mid, (rx, ry))}).draw(shader)
    for i, (hx, hy) in enumerate(corners):
        active = (_S.hover_zone == "corner" and _S.hover_index == i)
        shader.uniform_float("color", (1.0, 0.8, 0.2, 0.95) if active
                             else (1.0, 1.0, 1.0, 0.8))
        _filled_square(shader, hx, hy, _HANDLE_PX * 0.66)
    shader.uniform_float("color", (1.0, 0.8, 0.2, 0.95)
                         if _S.hover_zone == "rotate" else (1.0, 1.0, 1.0, 0.8))
    _filled_square(shader, rx, ry, _HANDLE_PX * 0.66)
    gpu.state.blend_set('NONE')


def _filled_square(shader, cx, cy, r):
    batch_for_shader(shader, 'TRI_FAN',
                     {"pos": ((cx - r, cy - r), (cx + r, cy - r),
                              (cx + r, cy + r), (cx - r, cy + r))}).draw(shader)


def _quad_matrix_for(mode, region, rv3d):
    """The world matrix of the reference quad for the two 3D modes.

    Mode 2 (PINNED) uses the matrix captured when you pinned it, so the picture
    stays exactly where you posed it in the scene and viewport navigation does
    nothing to it.

    Mode 3 (CAMERA) is rebuilt from the scene camera every draw, so it rides
    the camera and NOTHING else moves it — which is what "only the camera
    affects the movement and depth" means. Its distance is the depth slider,
    and its size follows the camera's own field of view, so `scale` reads as a
    fraction of the camera frame at any focal length.
    """
    from mathutils import Matrix
    if mode == MODE_PINNED:
        return _S.pinned_matrix
    scene = bpy.context.scene
    cam = scene.camera
    if cam is None:
        return None
    _o, _fx, _fy, scale, rot, _m, _v = _view_state()
    dist = _S.occlude_distance or max(scene.camera.data.clip_start * 4.0, 2.0)
    cm = cam.matrix_world
    # Half-width of the camera frustum at `dist`, from the real sensor/lens.
    try:
        sensor = cam.data.sensor_width or 36.0
        half_w = dist * (sensor * 0.5) / max(cam.data.lens, 1e-4)
    except Exception:                                # noqa: BLE001
        half_w = dist * 0.5
    if getattr(cam.data, "type", 'PERSP') == 'ORTHO':
        half_w = cam.data.ortho_scale * 0.5
    w = 2.0 * half_w * max(scale, 0.01)
    h = w * (_S.height / float(_S.width or 1))
    # centred on the camera axis, `dist` along its forward (-Z), spun by `rot`
    local = (Matrix.Translation((0.0, 0.0, -dist))
             @ Matrix.Rotation(rot, 4, 'Z')
             @ Matrix.Diagonal((w, h, 1.0, 1.0)))
    return cm @ local


def _draw_world():
    """Modes 2 and 3 — a real quad in the scene, so depth is simply true."""
    mode = MODE_PINNED if _S.pinned_matrix is not None else None
    try:
        cur_mode = int(_view_state()[5]) if _S.shm is not None else 0
    except Exception:                                # noqa: BLE001
        return
    if cur_mode not in (MODE_PINNED, MODE_CAMERA):
        return
    st = _prepare(cur_mode)
    if st is None:
        return
    opacity = st[0]
    m = _quad_matrix_for(cur_mode, bpy.context.region,
                         bpy.context.region_data)
    if m is None:
        return
    corners = (Vector((-0.5, -0.5, 0.0)), Vector((0.5, -0.5, 0.0)),
               Vector((0.5, 0.5, 0.0)), Vector((-0.5, 0.5, 0.0)))
    coords = [(m @ c)[:] for c in corners]
    _quad(gpu.shader.from_builtin('IMAGE_COLOR'), coords, opacity, depth=True)
    _ = mode


# ------------------------------------------------------------------ timer

def _tick():
    """Tag a redraw only when the served frame actually changed.

    ⚠ Deliberately tiny. Blender's main thread is shared with everything else,
    and a timer that does real work is exactly the shape of the third-party
    stalls this project has chased before.
    """
    if _S.shm is None:
        _S.timer_on = False
        return None
    try:
        newest_frame = _PRODUCER.unpack_from(_S.shm.buf, _PRODUCER_OFF)[1]
        if newest_frame != _S.last_drawn:
            _tag_redraw()
    except Exception:                                # noqa: BLE001
        return None
    return _TIMER_S


def _tag_redraw():
    wm = bpy.context.window_manager
    for window in wm.windows:
        for area in window.screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()


# ------------------------------------------------- direct manipulation

class MADILIB_OT_madiref_adjust(bpy.types.Operator):
    """Move, scale and rotate the reference overlay in the viewport.

    ⚠ This runs for as long as the reference is shown, so it is written to
    **get out of the way**: every event that is not on the overlay returns
    PASS_THROUGH, and the only events it ever swallows are a press or a wheel
    with the cursor actually on the picture. Blender's navigation, selection
    and every other modal operator are untouched anywhere else.

    ⚠ It cannot be verified headless — `blender -b` has no window-manager modal
    loop, the same limitation `picker_start` documents. The pure geometry it
    relies on (hit-testing, the scale/rotate maths) is unit-tested instead, and
    that is deliberately where the logic lives.
    """

    bl_idname = "madilib.madiref_adjust"
    bl_label = "Adjust reference overlay"
    # ⚠ No 'UNDO': this edits shared memory, not scene data. Pushing an undo
    # step would put a viewport nudge into the user's animation undo stack.
    bl_options = {'REGISTER'}

    def invoke(self, context, event):
        if _S.shm is None:
            return {'CANCELLED'}
        if _S.modal_running:
            return {'CANCELLED'}
        _S.modal_running = True
        self._mode = 'IDLE'
        self._start = None
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    # ------------------------------------------------------------------

    def _finish(self):
        _S.modal_running = False
        _S.hover_zone = None
        _S.hover_index = -1
        _S.hover_region = 0
        _tag_redraw()
        return {'FINISHED'}

    def modal(self, context, event):
        # The reference went away (closed, or the app quit) -- so do we.
        if _S.shm is None:
            return self._finish()
        region = context.region
        area = context.area
        if area is None or area.type != 'VIEW_3D' or region is None:
            return {'PASS_THROUGH'}
        try:
            _o, _x, _y, _sc, _rot, mode, visible = _view_state()
        except Exception:                            # noqa: BLE001
            return {'PASS_THROUGH'}
        # Plane mode is a real object: Blender's own transform tools own it,
        # and intercepting anything here would fight them.
        # Only the screen-space mode is mouse-editable here: pinned and camera
        # placements are real 3D quads and belong to the depth/pin controls.
        # ⚠ And a LOCKED reference must consume nothing at all.
        if mode != MODE_VIEWPORT or not visible or _S.locked:
            if _S.hover_zone:
                _S.hover_zone, _S.hover_index, _S.hover_region = None, -1, 0
                region.tag_redraw()
            return {'PASS_THROUGH'}

        mx, my = event.mouse_region_x, event.mouse_region_y
        rw, rh = region.width, region.height

        if self._mode != 'IDLE':
            return self._drag(context, event, mx, my, rw, rh)

        zone, index = _hit_test(mx, my, rw, rh)
        # ⚠ as_pointer(), NOT id(). Blender returns a fresh Python wrapper on
        # every `context.region` access, so `id()` changed constantly and the
        # draw handler's comparison almost never matched — the handles were
        # "only sometimes visible" for exactly this reason.
        ptr = region.as_pointer()
        if (zone, index, ptr) != (_S.hover_zone, _S.hover_index,
                                  _S.hover_region):
            _S.hover_zone, _S.hover_index = zone, index
            _S.hover_region = ptr if zone else 0
            region.tag_redraw()
        if zone is None:
            return {'PASS_THROUGH'}

        if event.type == 'WHEELUPMOUSE' and event.value == 'PRESS':
            self._nudge_scale(rw, 1.1 if not event.shift else 1.025)
            return {'RUNNING_MODAL'}
        if event.type == 'WHEELDOWNMOUSE' and event.value == 'PRESS':
            self._nudge_scale(rw, 1 / 1.1 if not event.shift else 1 / 1.025)
            return {'RUNNING_MODAL'}
        if event.type == 'LEFTMOUSE' and event.value == 'PRESS':
            corners, centre, (w, h), rot, _op = _overlay_geometry(rw, rh)
            _o2, fx, fy, scale, rot2, _m, _v = _view_state()
            self._start = {
                "mouse": (mx, my), "centre": centre, "fx": fx, "fy": fy,
                "scale": scale, "rot": rot2, "w": w, "h": h,
                "grab_angle": math.atan2(my - centre[1], mx - centre[0]),
                "grab_dist": math.hypot(mx - centre[0], my - centre[1]) or 1.0,
            }
            self._mode = {"body": 'MOVE', "corner": 'SCALE',
                          "rotate": 'ROTATE'}[zone]
            return {'RUNNING_MODAL'}
        return {'PASS_THROUGH'}

    # ------------------------------------------------------------------

    def _nudge_scale(self, region_w, factor):
        _o, _fx, _fy, scale, _rot, _m, _v = _view_state()
        _write_view(scale=max(_MIN_SCALE, min(_MAX_SCALE, scale * factor)))
        _tag_redraw()

    def _drag(self, context, event, mx, my, rw, rh):
        s = self._start
        if event.type in {'RIGHTMOUSE', 'ESC'} and event.value == 'PRESS':
            # put everything back exactly as it was
            _write_view(x=s["fx"], y=s["fy"], scale=s["scale"],
                        rotation=s["rot"])
            self._mode = 'IDLE'
            _tag_redraw()
            return {'RUNNING_MODAL'}
        if event.type == 'LEFTMOUSE' and event.value == 'RELEASE':
            self._mode = 'IDLE'
            return {'RUNNING_MODAL'}
        if event.type != 'MOUSEMOVE':
            return {'RUNNING_MODAL'}

        fine = 0.25 if event.shift else 1.0
        dx = (mx - s["mouse"][0]) * fine
        dy = (my - s["mouse"][1]) * fine

        if self._mode == 'MOVE':
            _write_view(x=s["fx"] + dx / float(rw or 1),
                        y=s["fy"] + dy / float(rh or 1))
        elif self._mode == 'SCALE':
            # distance from the centre drives it, so grabbing any corner
            # behaves the same and the aspect ratio can never be broken
            dist = math.hypot(mx - s["centre"][0], my - s["centre"][1])
            factor = dist / s["grab_dist"]
            if event.shift:
                factor = 1.0 + (factor - 1.0) * 0.25
            _write_view(scale=max(_MIN_SCALE,
                                  min(_MAX_SCALE, s["scale"] * factor)))
        elif self._mode == 'ROTATE':
            ang = math.atan2(my - s["centre"][1], mx - s["centre"][0])
            rot = s["rot"] + (ang - s["grab_angle"])
            if event.ctrl:
                step = math.radians(_SNAP_DEG)
                rot = round(rot / step) * step
            _write_view(rotation=rot)
        _tag_redraw()
        return {'RUNNING_MODAL'}


def _start_adjust():
    """Kick the modal off in a real VIEW_3D.

    ⚠ Bridge commands run on a timer with no usable area context, so this has
    to find a 3D viewport and `temp_override` into it — the same dance
    `picker_start` documents.
    """
    if _S.modal_running:
        return True
    wm = bpy.context.window_manager
    for window in wm.windows:
        for area in window.screen.areas:
            if area.type != 'VIEW_3D':
                continue
            for region in area.regions:
                if region.type != 'WINDOW':
                    continue
                try:
                    with bpy.context.temp_override(window=window, area=area,
                                                   region=region):
                        bpy.ops.madilib.madiref_adjust('INVOKE_DEFAULT')
                    return _S.modal_running
                except Exception:                    # noqa: BLE001
                    return False
    return False


# --------------------------------------------------------------- commands

def madiref_open(name, plane_object="", sync_framedrop=None):
    """Attach to the app's ring and start drawing."""
    from multiprocessing import shared_memory
    madiref_close()
    try:
        shm = shared_memory.SharedMemory(name=name)
    except FileNotFoundError:
        return {"ok": False, "error": "reference segment '%s' is not open" % name}
    try:
        (magic, version, w, h, slots, slot_bytes, _pix_off, frame_count,
         _fn, _fd) = _H.unpack_from(shm.buf, 0)
    except Exception as exc:                         # noqa: BLE001
        shm.close()
        return {"ok": False, "error": "unreadable segment: %s" % exc}
    if magic != _MAGIC or version != _VERSION:
        shm.close()
        return {"ok": False, "error": "segment is not a MadiRef ring "
                                      "(or is a different version)"}
    _S.shm = shm
    _S.name = name
    _S.width, _S.height = w, h
    _S.slots, _S.slot_bytes = slots, slot_bytes
    _S.frame_count = frame_count
    _S.plane_object = plane_object or ""
    _S.texture = None
    _S.tex_frame = -1
    _S.last_drawn = -1
    _S.errors = 0

    _S.handle_px = bpy.types.SpaceView3D.draw_handler_add(
        _draw_overlay, (), 'WINDOW', 'POST_PIXEL')
    _S.handle_view = bpy.types.SpaceView3D.draw_handler_add(
        _draw_world, (), 'WINDOW', 'POST_VIEW')
    if not _S.timer_on:
        bpy.app.timers.register(_tick, first_interval=_TIMER_S)
        _S.timer_on = True
    if sync_framedrop is not None:
        _set_sync(bool(sync_framedrop))
    # Direct manipulation starts with the reference — the point is that you
    # never have to go and turn it on.
    adjust = _start_adjust()
    _tag_redraw()
    return {"ok": True, "width": w, "height": h, "frames": frame_count,
            "adjustable": adjust}


def _set_sync(on):
    """FRAME_DROP is what makes 'do not slow down' true.

    A timeline-locked reference plays at half speed if the viewport plays at
    half speed. Rather than give the video its own clock (which would drift
    from the animation and make pose-matching useless), we ask Blender to hold
    real time by dropping frames — then frame-matched and real-time agree.

    The previous value is remembered and restored on close: silently leaving a
    scene setting changed is not ours to do.
    """
    scene = bpy.context.scene
    if on:
        if _S.sync_restore is None:
            _S.sync_restore = scene.sync_mode
        scene.sync_mode = 'FRAME_DROP'
    elif _S.sync_restore is not None:
        scene.sync_mode = _S.sync_restore
        _S.sync_restore = None


def madiref_close():
    """Detach and put everything back."""
    if _S.handle_px is not None:
        try:
            bpy.types.SpaceView3D.draw_handler_remove(_S.handle_px, 'WINDOW')
        except Exception:                            # noqa: BLE001
            pass
        _S.handle_px = None
    if _S.handle_view is not None:
        try:
            bpy.types.SpaceView3D.draw_handler_remove(_S.handle_view, 'WINDOW')
        except Exception:                            # noqa: BLE001
            pass
        _S.handle_view = None
    if _S.timer_on:
        try:
            if bpy.app.timers.is_registered(_tick):
                bpy.app.timers.unregister(_tick)
        except Exception:                            # noqa: BLE001
            pass
        _S.timer_on = False
    try:
        _set_sync(False)
    except Exception:                                # noqa: BLE001
        pass
    _S.texture = None
    _S.tex_frame = -1
    _S.hover_zone = None
    _S.hover_index = -1
    _S.hover_region = 0
    # The modal ends itself on the next event once the segment is gone; this
    # just makes sure it is not left believing it is still running.
    if _S.shm is not None:
        # ⚠ Collect first. Any surviving memoryview or numpy view onto the
        # mapping makes close() raise BufferError and leaves the segment
        # mapped — the app then cannot unlink it, and the next open collides.
        try:
            import gc
            gc.collect()
        except Exception:                            # noqa: BLE001
            pass
        try:
            _S.shm.close()
        except BufferError:
            # Something still holds a view. Say so rather than pretending the
            # reference closed cleanly; the segment frees when Blender exits.
            _S.errors += 1
        except Exception:                            # noqa: BLE001
            pass
        _S.shm = None
    _S.name = None
    _tag_redraw()
    return {"ok": True}


def madiref_config(plane_object=None, sync_framedrop=None,
                   occlude=None, occlude_distance=None, locked=None):
    """Settings that are NOT in the ring (the ring carries the live view
    state, which the app writes directly and this side only reads)."""
    if plane_object is not None:
        _S.plane_object = plane_object or ""
    if sync_framedrop is not None:
        _set_sync(bool(sync_framedrop))
    if occlude is not None:
        _S.occlude = bool(occlude)
    if occlude_distance is not None:
        _S.occlude_distance = max(0.0, float(occlude_distance))
    if locked is not None:
        _S.locked = bool(locked)
        if _S.locked:
            _S.hover_zone = None
            _S.hover_index = -1
            _S.hover_region = 0
    _tag_redraw()
    return {"ok": True, "plane_object": _S.plane_object,
            "locked": _S.locked,
            "occlude": _S.occlude,
            "occlude_distance": _S.occlude_distance,
            "sync_framedrop": _S.sync_restore is not None}


def madiref_make_plane(name="MADI_Reference", distance=5.0, height=2.0):
    """A plane sized to the reference's aspect, for MODE_PLANE.

    Auto-sizing is from the VIDEO's aspect ratio, which is what Marty asked
    for: the object is created with the right shape so scaling it uniformly
    never distorts the picture.
    """
    if _S.shm is None:
        return {"ok": False, "error": "no reference is open"}
    aspect = (_S.width / float(_S.height)) if _S.height else 1.777
    obj = bpy.data.objects.get(name)
    if obj is None:
        mesh = bpy.data.meshes.new(name)
        mesh.from_pydata(
            [(-0.5, -0.5, 0.0), (0.5, -0.5, 0.0),
             (0.5, 0.5, 0.0), (-0.5, 0.5, 0.0)], [], [(0, 1, 2, 3)])
        mesh.update()
        obj = bpy.data.objects.new(name, mesh)
        bpy.context.scene.collection.objects.link(obj)
        obj.location = (0.0, distance, 0.0)
        obj.rotation_euler = (1.5707963, 0.0, 0.0)
    obj.scale = (height * aspect, height, 1.0)
    # the mesh is only a transform handle -- the picture is drawn over it, so
    # the geometry itself should stay out of the way
    obj.display_type = 'WIRE'
    obj.hide_render = True
    _S.plane_object = obj.name
    _tag_redraw()
    return {"ok": True, "object": obj.name, "aspect": round(aspect, 4)}


def madiref_status():
    st = {"ok": True, "attached": _S.shm is not None, "name": _S.name,
          "width": _S.width, "height": _S.height,
          "frames": _S.frame_count, "last_drawn": _S.last_drawn,
          "plane_object": _S.plane_object, "draw_errors": _S.errors,
          "drawing": _S.handle_px is not None,
          "adjustable": _S.modal_running,
          "locked": _S.locked,
          "pinned": _S.pinned_matrix is not None,
          "occlude": _S.occlude,
          "occlude_distance": _S.occlude_distance}
    if _S.shm is not None:
        try:
            # ⚠ SEVEN names for seven values. This zip had six, so everything
            # after `scale` was reported shifted by one — `mode` showed the
            # rotation and `visible` showed the mode, which reads as "the
            # overlay is hidden" when it is not. A zip silently truncates.
            st["view"] = dict(zip(
                ("opacity", "x", "y", "scale", "rotation", "mode", "visible"),
                _view_state()))
        except Exception:                            # noqa: BLE001
            pass
    return st


def madiref_pin(mode="viewport"):
    """Choose one of the three placements.

    `pinned` captures WHERE IT IS RIGHT NOW — the screen-space rectangle is
    projected into the scene at the depth slider's distance, facing the current
    view — so "pin it where I posed it" means exactly that, with no separate
    positioning step.
    """
    from mathutils import Matrix
    if _S.shm is None:
        return {"ok": False, "error": "no reference is open"}
    want = {"viewport": MODE_VIEWPORT, "pinned": MODE_PINNED,
            "camera": MODE_CAMERA}.get(str(mode).lower())
    if want is None:
        return {"ok": False, "error": "unknown mode %r" % (mode,)}

    if want == MODE_PINNED:
        # ⚠ A bridge command runs on a timer with NO area context, so
        # `bpy.context.region_data` is None here — the same reason
        # `_start_adjust` has to go hunting. Find a real 3D viewport instead of
        # telling the user to hover one.
        rv3d = bpy.context.region_data
        if rv3d is None:
            for window in bpy.context.window_manager.windows:
                for area in window.screen.areas:
                    if area.type == 'VIEW_3D':
                        rv3d = area.spaces.active.region_3d
                        break
                if rv3d is not None:
                    break
        if rv3d is None:
            return {"ok": False,
                    "error": "open a 3D viewport in Blender first"}
        _o, _fx, _fy, scale, rot, _m, _v = _view_state()
        dist = _S.occlude_distance or max(rv3d.view_distance, 1.0)
        # the view's own axes, so the pinned quad faces the way you were looking
        inv = rv3d.view_matrix.inverted()
        eye = inv.translation
        centre = eye + (-inv.col[2].to_3d().normalized()) * dist
        # width of the view frustum at `dist`, so `scale` keeps its meaning
        try:
            half = dist * abs(1.0 / rv3d.window_matrix[0][0])
        except Exception:                            # noqa: BLE001
            half = dist * 0.5
        w = 2.0 * half * max(scale, 0.01)
        h = w * (_S.height / float(_S.width or 1))
        basis = inv.to_3x3().to_4x4()
        _S.pinned_matrix = (Matrix.Translation(centre) @ basis
                            @ Matrix.Rotation(rot, 4, 'Z')
                            @ Matrix.Diagonal((w, h, 1.0, 1.0)))
    elif want == MODE_VIEWPORT:
        _S.pinned_matrix = None

    o, fx, fy, scale, rotation, _mode, vis = _view_state()
    _VIEW.pack_into(_S.shm.buf, _VIEW_OFF, o, fx, fy, scale, rotation,
                    want, int(vis))
    _tag_redraw()
    return {"ok": True, "mode": mode, "pinned": _S.pinned_matrix is not None}


def madiref_reset_view():
    """Put the overlay back to a sane place — the escape hatch for an overlay
    dragged off-screen or rotated to somewhere confusing."""
    _write_view(x=0.18, y=0.20, scale=0.32, rotation=0.0)
    _tag_redraw()
    return {"ok": True}


_classes = (MADILIB_OT_madiref_adjust,)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)


def unregister():
    madiref_close()
    for cls in reversed(_classes):
        try:
            bpy.utils.unregister_class(cls)
        except Exception:                            # noqa: BLE001
            pass
