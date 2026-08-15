# SPDX-License-Identifier: GPL-2.0-or-later
"""
MADI Picker
===========
A 2D bone picker that lives INSIDE the Image Editor, in the spirit of AnimSchool
Picker. It draws buttons on the editor canvas; each button maps to a bone (or,
for round GROUP buttons, to a set of bones). Click a button to select its
bone(s); box-select to grab several at once.

⚠ THIS IS A FREE TOOL as of 2026-08-06 (Marty: "Bone pickers, Anim Layers and
Node setup Tabs should be free and not pay gated"). It was gated until then —
every operator carried an `entitlement.py` check — and that gate has been
removed rather than left switched off. Nothing here requires the licensed app;
the app's Bone picker tab is simply a nicer way to build the same layouts.
See `docs\\bone-picker.md`.

This does NOT create a new editor/space type (Blender doesn't allow that from
Python) - it draws into the existing Image Editor via a GPU draw handler and
reads mouse input through a persistent modal operator.

Buttons are anchored in CANVAS space (the same space the image occupies), so
they pan and zoom together with the background image: zoom into the face and
the face buttons grow with it, staying over the same spot on the picture.

Tested against Blender 5.2.0 LTS.

Usage
-----
1. Enable the MadihsonNSFW extension and sign in with the Toolset app.
2. Open an Image Editor, N-panel -> "MADI Picker" tab.
3. Make a TAB, point it at your rig, give it a background image, then press
   "Start Picker".
   - Click a button          -> select that bone (round button = whole group).
                                In Object Mode the rig is taken into POSE MODE
                                first, so the selection is one you can see
   - Drag a SLIDER           -> scrub its shape key 0-100%
   - Alt+click a SLIDER      -> keyframe that shape key on the current frame
                                (Alt+Shift+click removes this frame's key). The
                                pip at its right end is amber when THIS frame
                                is keyed, green when the key is animated
                                elsewhere
   - Ctrl+hover a button     -> it outlines cyan: a Ctrl+drag will move it
   - Shift+click             -> add to selection
   - Drag empty space        -> box select
   - Click empty space       -> deselect all
   - Ctrl+click empty space  -> add a button for the active bone. With SEVERAL
                                bones selected, the first lands there and the
                                rest wait for a click each (right-click, Esc or
                                leaving the canvas cancels the rest)
   - Ctrl+drag a button      -> move it (moves the whole selection if it's
                                part of the selection). Buttons are SOLID: a
                                move stops against its neighbours and slides
                                along them, so nothing can end up inside
                                anything else
   - Ctrl+drag a button EDGE -> resize that side only (it stops against a
                                neighbour too)
   - Ctrl+G                  -> make a round GROUP button AT THE MOUSE
   - Right-click a SLIDER    -> "Clear N Keyframes": takes the shape key's
                                whole animation off (the value stays, Ctrl+Z
                                brings it back). Absent when there's nothing
                                to clear
   - Right-click             -> context menu at the mouse. It only lists what's
                                possible there: align the selected buttons
                                horizontally / vertically, fit their height to
                                their label, reset them to the size they were
                                placed at, add a button for every selected bone
                                (empty space only), create a group button - you
                                then PLACE it with a click, like a multi-add
                                (empty space only) - and delete
   - Del / X                 -> delete selected buttons (or the one under the
                                cursor)
   - Esc / "Stop Picker"     -> end the session. The overlay disappears and
                                every Image Editor gets back exactly what it
                                was showing before the session - stopped, the
                                editors belong to the user
"Button Scale" is a BRUSH, exactly like the color swatch: it applies live to the
SELECTED buttons, and with nothing selected it's simply what the next button you
add will get. It's stored per button and FOLLOWS the button you click, so the
field always reads that button's real value. Dragging it scales the WHOLE
selection by the same ratio, so buttons you deliberately made bigger than their
neighbours stay bigger. Positions are never touched.
There is NO text size setting: a button is never inflated by its own label, and
EVERY LABEL IS FITTED TO ITS OWN BUTTON (v0.13.0 put this back - v0.12.0's one
shared size per tab meant the smallest button set the size for everyone, and a
deliberately enlarged button drew tiny writing). Button Scale scales the writing
with the box, and on a labelled button it also pulls the HEIGHT down to hug the
text as it goes, so scaling up never grows dead space above and below the
writing. Right-click -> "Fit Height to Text" does the same on demand.
"Align Gap" is live in the same spirit: once you've aligned a selection, dragging
it re-runs that align at the new gap, so you can dial the spacing in by eye.
"New buttons: no label" makes the next buttons you add plain squares - the bone is
still mapped, and any single button's text can be switched back on in the list.
New buttons copy the SELECTED button's size and text; with nothing selected, a
no-label button comes out as a minimal square at the Button Scale you set.

TABS let one scene hold several pickers - one per character, or several per
character (face / body / props). A tab owns an armature and a background image;
buttons are still stored on the armature object (so they save with the .blend and
travel with the rig), tagged with the tab they belong to.

Layouts can be exported to a .json preset to share or reuse on another rig.
"""

import json
import math
import os

import bpy
import blf
import gpu
from gpu_extras.batch import batch_for_shader
from bpy_extras.io_utils import ImportHelper, ExportHelper
from bpy.props import (
    StringProperty, FloatProperty, IntProperty, BoolProperty, EnumProperty,
    CollectionProperty, PointerProperty, FloatVectorProperty,
)
from bpy.types import PropertyGroup, Operator, Panel, UIList, Menu
from bpy.app.handlers import persistent

# ⚠ `entitlement` is deliberately NOT imported here any more, and neither is a
# LOCKED_HINT defined - the picker is free (2026-08-06). Leaving an unused
# import of the gate module behind is how a tool ends up looking gated to the
# next person reading it.
from . import core

# ---------------------------------------------------------------------------
# Module state (draw handler + running session)
# ---------------------------------------------------------------------------
_state = {
    "handle": None,           # draw_handler_add handle
    "running": False,         # modal session active
    "box": None,              # (x0,y0,x1,y1) region px while box-selecting
    "suppress_apply": False,  # guard so reading values back doesn't re-apply
    "hover_idx": -1,          # index of Ctrl-hovered button (-1 = none)
    "hover_ex": 0,            # which side is under the cursor (-1/0/+1)
    "hover_ey": 0,
    # The draw handler runs once per Image Editor, but a rubber band and a hover
    # grip belong to exactly ONE of them - they're in that region's pixel coords
    # and they answer "what is the mouse doing HERE". Without these they ghost
    # into every other Image Editor on the screen. as_pointer() of the region
    # they belong to, 0 = none. (PLACE mode's ghost has always done this.)
    "box_region": 0,
    "hover_region": 0,
    # Which button the last Del/X or right-click was over (-1 = empty space). The
    # context menu has no cursor of its own, so the modal resolves it once here.
    "cursor_idx": -1,
    # Click-to-place queue: bone names still waiting for a click of their own.
    # Non-empty == the modal is in PLACE mode, which is a mode precisely so that
    # a pile of buttons never lands on one spot to be dragged apart afterwards.
    "place": [],
    # A GROUP button waiting for its placement click (job 20): a plain dict
    # {"members": [names], "h": float, "scale": float} snapshotted when the
    # menu/panel armed it - plain values, never element refs (gotcha 18).
    # Non-None == PLACE mode too, sharing the queue's ghost/cancel rules.
    "place_group": None,
    # SLIDER buttons waiting for a click each (v0.19.0): plain (object_name,
    # key_name) tuples. Non-empty == PLACE mode, same ghost/cancel rules.
    "place_sliders": [],
    # What each Image Editor was showing when the session STARTED
    # (space.as_pointer() -> image name or None), so stopping the picker hands
    # the editors back exactly as they were (job 26). Emptied by the restore,
    # so whichever of Stop/_finish runs first wins and the other is a no-op.
    "saved_images": {},
    "place_mouse": None,      # (lx, ly) of the ghost, region px
    "place_region": 0,        # as_pointer() of the region it belongs to
    # Last value Button Scale was seen at. It applies the RATIO of its own
    # movement to every selected button, so a mixed selection keeps its relative
    # sizes - which means it needs the previous value to compare against.
    # None = no baseline yet: record the next write instead of acting on it.
    "scale_prev": None,
    # Align Gap is live too, but it is an ABSOLUTE value, not a ratio: every
    # drag re-derives the whole align from the run below, so this is only here
    # to skip a redundant re-run when the callback fires on an unchanged value.
    "gap_prev": None,
    # The last Align run, so the Align Gap slider can re-derive it while it's
    # dragged: which axis it used, and where its buttons sat BEFORE it moved
    # them. Buttons are held by INDEX + key, never as element references
    # (gotcha 18). None = nothing aligned yet this session, so the slider is
    # inert - same "needs a selection" rule the brushes use.
    "align_run": None,
}

# Builtin shaders, built on FIRST USE - never at import.
#
# 'UNIFORM_COLOR' / 'SMOOTH_COLOR' (per-vertex color, batched) draw the fills;
# plain LINES are hard-edged, so outlines go through the POLYLINE_* pair, which
# anti-aliases in the shader (measured offscreen on one arc: 29 intensities vs
# exactly 1) and takes its width as a UNIFORM - gpu.state.line_width_set() does
# nothing for them.
#
# Why lazy: gpu.shader.from_builtin() at module level raises
#   SystemError: GPU functions for drawing requires the gpu module to be
#   initialized
# the moment the module is imported under `blender -b`, so addon_utils.enable()
# fails and NOTHING headless can reach the picker's data - not batch preset
# conversion, not CI, not reading a layout out of a .blend. Nothing below is
# reachable without a real draw pass anyway.
_shaders = {}


def _shader(name):
    sh = _shaders.get(name)
    if sh is None:
        sh = _shaders[name] = gpu.shader.from_builtin(name)
    return sh

DEFAULT_COLOR = (0.20, 0.22, 0.26)
DEFAULT_SCALE = 0.04          # canvas units (1.0 == full image width)
DEFAULT_BTN_SCALE = 1.0       # global multiplier on every button's stored w/h
# The label is SIZED TO ITS BUTTON (v0.11.0) - there is no text size setting any
# more, and no label floor: a button is exactly as big as you made it and the
# text is fitted inside it, so Button Scale scales the writing with the box for
# free. (v0.12.0 shared ONE size across the tab; v0.13.0 reverted that on
# Marty's call - the size is per button again, so a big button gets big text.)
#
# ONE padding number, not two fractions (v0.12.0). It's a fraction of the
# button's SMALLER drawn dimension, applied on all four sides, so "the gap above
# the writing matches the gap beside it" is true BY CONSTRUCTION instead of by
# two constants tuned against each other - which is exactly how top/bottom ended
# up visibly bigger than left/right. 0.05 reproduced v0.11.0's left/right gap;
# v0.18.0 doubled it to 0.10 on Marty's call ("the text gets too close to the
# edges") - _fit_height reads this same constant, so hugged buttons stay
# consistent with the draw.
TEXT_PAD = 0.10
# a circle's inscribed square is 1/sqrt(2) of its diameter - that's the usable
# text box inside a round GROUP button
GROUP_INSCRIBE = 0.707
# Corner rounding on BONE and SLIDER buttons (v0.20.0), as a fraction of the
# button's smaller DRAWN side - so it holds at every zoom and every aspect, and
# 0.5 would be a full stadium. Purely cosmetic: hit-testing, box-select and the
# walls all still use the rect, and the corners they lose are a few px.
# Since v0.20.1 this is the DEFAULT and the mid-(un)register fallback - the live
# value is the Corner Roundness preference (Marty: 0.25 -> 0.125 -> 0.07).
BTN_ROUND = 0.07
# A new SLIDER button's drawn width : height ON SCREEN (the stored w corrects
# for the canvas' per-axis normalisation, gotcha 7), and how dark its empty
# track is relative to the button colour (the filled part IS the colour).
SLIDER_ASPECT = 4.0
SLIDER_TRACK = 0.30
# Below this the label is mush. A PER-BUTTON skip again since v0.13.0 (sizes
# are per button, so each label makes its own call) - it was all-or-nothing in
# v0.12.0 only because the size was shared and the minimum over the tab.
MIN_LABEL_PX = 5
# ⚠ THE CEILING EXISTS BECAUSE ITS ABSENCE CRASHED BLENDER (2026-08-06, v0.23.0).
# Labels are fitted in SCREEN px, and the fit used to go straight into
# blf.size() - a size that tracks zoom with no bound, in an editor whose zoom
# is effectively unbounded. A fresh glyph size costs about its AREA (measured
# live: 0.22 ms at 107 px, 1.05 at 427, 3.13 at 854 - 4x per doubling), so a
# deep zoom was paying hundreds of ms per wheel tick re-rasterising one label
# ("especially bad when I zoom in too close"), and far enough in the glyph
# bitmap allocation reaches hundreds of MB: Blender died with no crash.txt, no
# WER entry, no TDR - an abort deep inside an allocation. blf therefore never
# rasterises above this ceiling; a label on a bigger button is drawn by
# SCALING the capped rasterisation on the GPU (gpu.matrix), so it still grows
# with its button - softly, like the zoomed picture under it - at bounded cost.
MAX_LABEL_RASTER_PX = 160.0
# Fitted sizes are also quantised DOWN onto a geometric ladder (~6% rungs)
# before reaching blf. Every distinct float size mints a glyph cache blf keeps
# for the whole session: warm sizes are free (160 re-draws: 0.3 ms, measured)
# but each FRESH size pays rasterisation once, on the main thread - and a zoom
# drag re-fits every visible label on every tick, all at sizes nobody has used
# before. Rungs make consecutive ticks land on sizes already cached, so a zoom
# is warm after its first tick. A rung is never more than 6% under the fitted
# size - under a pixel at UI sizes, and always inside the padding.
_LABEL_LADDER = math.log(1.06)


def _label_draw_size(label_px):
    """(draw_px, scale) for a label fitted at `label_px` screen px: the size
    blf actually rasterises, and the GPU upscale that makes up the rest.

    scale is 1.0 for every button under the rasterisation ceiling; past it,
    draw_px pins to the ceiling's rung and scale carries the remainder exactly
    (draw_px * scale == label_px), so the text keeps tracking its button at
    any zoom. Pure maths on purpose - the tests sweep it to 1,000,000 px,
    a size the old code really did reach (and die at) on a deep zoom."""
    want = label_px if label_px < MAX_LABEL_RASTER_PX else MAX_LABEL_RASTER_PX
    rung = math.exp(math.floor(math.log(want) / _LABEL_LADDER) * _LABEL_LADDER)
    if rung < MIN_LABEL_PX:
        # only reachable when label_px itself is at/near the mush floor, so
        # the clamp can never lift draw_px above the fitted size
        rung = float(MIN_LABEL_PX)
    if label_px > MAX_LABEL_RASTER_PX:
        return rung, label_px / rung
    return rung, 1.0
# Gap left between buttons that align (or a multi-add) has to place next to each
# other, as a fraction of their mean DRAWN extent along that axis - so it scales
# with the buttons instead of being a fixed canvas distance, and it's measured on
# the axis being spaced, which sidesteps the per-axis canvas normalisation.
ALIGN_GAP = 0.25
MISSING_COLOR = (1.0, 0.15, 0.10, 0.95)   # outline for unmatched bones
# Keyframe pip on a SLIDER (v0.22.0), in Blender's own language: amber when
# THIS frame is keyed, green when the shape key is animated but this frame
# isn't. Drawn after the labels, so text can never hide the one thing that
# tells you whether you just keyed something.
KEY_ON_FRAME_COLOR = (1.00, 0.78, 0.15, 0.95)
KEY_ANIMATED_COLOR = (0.30, 0.80, 0.35, 0.85)
# The old sizing constants, used ONLY by the one-time reflow below: it has to
# reproduce the floor exactly as v0.10.0 drew it, so it can't read the live
# TEXT_*_FRAC (which moved when the padding came down) or the deleted scene-wide
# text size. Do not "tidy" these into the constants above.
_LEGACY_FONT = 0.022
_LEGACY_TEXT_W_FRAC = 0.82
_LEGACY_TEXT_H_FRAC = 0.55

# v2 added "scale" + the reference image; v3 moved font/scale onto each button;
# v4 carries each button's AS-PLACED size (w0/h0/scale0, what Reset Size goes
# back to) and its `blank` flag; v5 DROPS "font" - labels are sized from the
# button they sit in now, so there is no text size to store; v6 adds SLIDER
# buttons (sk_object/sk_key).
# v1-v5 still load: any "font" they carry is read and discarded, and a preset
# with no origin recorded treats the layout as loaded as the origin.
PRESET_VERSION = 6

# Round GROUP buttons are drawn as a fan. The segment count has to follow the
# DRAWN radius: a fixed 28 was fine on a small button and visibly faceted on a
# big/zoomed one, which is half of why the edges looked rough.
_CIRCLE_MAX_ERR = 0.3         # px a chord may deviate from the true circle
_circle_cache = {}            # segments -> unit-circle points
_quarter_cache = {}           # segments -> unit quarter-turn points (corners)


def _circle_pts(segs):
    pts = _circle_cache.get(segs)
    if pts is None:
        pts = [(math.cos(2.0 * math.pi * i / segs),
                math.sin(2.0 * math.pi * i / segs)) for i in range(segs)]
        if len(_circle_cache) > 64:
            _circle_cache.clear()
        _circle_cache[segs] = pts
    return pts


def _circle_segs(r):
    """Segments needed to keep a radius-r circle's facets under _CIRCLE_MAX_ERR.

    A chord spanning 2*pi/n deviates from the arc by r*(1-cos(pi/n)), so
    n >= pi / acos(1 - err/r). ~41 segments at r=100px, ~91 at r=500."""
    r = max(r, 1.0)
    c = 1.0 - min(0.999, _CIRCLE_MAX_ERR / r)
    return max(16, min(256, int(math.pi / math.acos(c)) + 1))


def _quarter_pts(segs):
    """(cos, sin) over a quarter turn, segs+1 samples, cached.

    All four corners of a rounded rect are this same quarter rotated by a
    multiple of 90 degrees, and those rotations are just swaps/negations of the
    pair - so one small cache serves every corner of every button, and the draw
    loop does no trig at all."""
    pts = _quarter_cache.get(segs)
    if pts is None:
        pts = [(math.cos(math.pi * 0.5 * k / segs),
                math.sin(math.pi * 0.5 * k / segs)) for k in range(segs + 1)]
        if len(_quarter_cache) > 64:
            _quarter_cache.clear()
        _quarter_cache[segs] = pts
    return pts


def _corner_segs(r):
    """Segments for ONE corner arc of a rounded rect (v0.24.1, perf).

    `_circle_segs` floors at 16 segments, which is right for a whole circle but
    absurd for a 2px corner: it was spending 4 segments (5 points) on an arc
    smaller than the line width, and every one of those points goes through
    Python lists into a fresh GPU buffer, EVERY redraw. Measured at 154
    buttons: 21 verts each with the old floor, 9 with this. The step-down is
    keyed on the DRAWN radius, so a big button still gets a smooth corner - at
    r >= 10px this matches what the old rule gave."""
    if r < 1.5:
        return 1
    if r < 4.0:
        return 2
    if r < 10.0:
        return 3
    return max(3, _circle_segs(r) // 4)


def _round_radius(w, h, frac=None):
    """Corner radius in px for a w x h button (0 when it's too small to show).

    A fraction of the SMALLER side, so a wide button and a square one get the
    same visual roundness instead of the wide one turning into a stadium.

    `frac` is the Corner Roundness preference. The button loop reads it ONCE
    per frame and passes it in for every button - _prefs() is an RNA lookup by
    name, and 155 of those per redraw is real money. The one-per-frame overlays
    (ghost, hover) can leave it None and let it look itself up."""
    if frac is None:
        p = _prefs()
        frac = (p.pk_btn_round / 100.0) if p is not None else BTN_ROUND
    r = min(abs(w), abs(h)) * frac
    # Sub-pixel corners are geometry nobody can see, so they fall back to a
    # plain rect. The floor is HALF a pixel, not one (v0.20.1): at 12.5% a
    # button under 8px tall was dropping to square while its neighbours stayed
    # round - a visible inconsistency at low zoom, for a corner that does
    # render once the fill is anti-aliased.
    return r if r >= 0.5 else 0.0


def _round_rect_pts(x0, y0, x1, y1, r):
    """Perimeter of a rounded rect, CCW from the bottom edge, as a closed ring.

    Corner arcs are faceted by the same rule circles use (_circle_segs on the
    corner radius), so roundness never looks chunkier than a group button at
    the same zoom. r <= 0 gives the four plain corners back, which is what
    keeps every caller working when a button is too small to round (or the
    Corner Roundness preference is at 0%)."""
    if r <= 0.0:
        return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    segs = _corner_segs(r)
    q = _quarter_pts(segs)
    ax0, ay0, ax1, ay1 = x0 + r, y0 + r, x1 - r, y1 - r     # the arc centres
    pts = []
    for c, s in q:                       # bottom-right: bottom edge -> right
        pts.append((ax1 + r * s, ay0 - r * c))
    for c, s in q:                       # top-right
        pts.append((ax1 + r * c, ay1 + r * s))
    for c, s in q:                       # top-left
        pts.append((ax0 - r * s, ay1 + r * c))
    for c, s in q:                       # bottom-left, closing to the start
        pts.append((ax0 - r * c, ay0 - r * s))
    return pts


def _clip_ring_x(ring, xv):
    """The part of a convex ring with x <= xv (Sutherland-Hodgman, one edge).

    Exact on the polygon - crossing edges are cut at their true intersection,
    not snapped to a vertex - so a slider's value fill ends dead flat while
    still wearing the track's rounded left end, at any fill fraction."""
    out = []
    n = len(ring)
    for i in range(n):
        ax, ay = ring[i]
        bx, by = ring[(i + 1) % n]
        a_in, b_in = ax <= xv, bx <= xv
        if a_in:
            out.append((ax, ay))
        if a_in != b_in:
            t = (xv - ax) / (bx - ax)
            out.append((xv, ay + (by - ay) * t))
    return out


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------
_REF_SIZE = 40                # measure metrics here, then scale (see below)
# blf.dimensions() is not linear in font size, so a ratio measured at _REF_SIZE
# under-predicts the width at other sizes - measured up to 1.8% low across
# 31..310px. Pad the requirement so "the label fits inside the padding" is
# actually true at every zoom instead of quietly eating into the padding.
_RATIO_PAD = 1.04
_ratio_cache = {}             # text -> width/nominal-size, blf is deterministic
_cap_cache = {}               # "cap": cap-height/nominal-size, measured once


def _cap_ratio():
    """Cap height ("M") divided by the NOMINAL font size.

    The height budget has to be spent on INK, not on nominal size. A nominal
    size carries ascender AND descender room, so v0.11.0's "the label may be 70%
    of the button's height" put only 0.72 x 0.70 = ~50% of the button under
    actual glyphs, and the leftover read as huge padding above and below. The
    width budget never had this problem - `blf.dimensions()[0]` IS the ink width
    - which is precisely why left/right looked right and top/bottom didn't.

    Measured on 5.2's default font: 0.725 at 40px, 0.75 at 12-20px, 0.7375 at
    160px. Like _label_width_ratio this is measured once at _REF_SIZE and reused;
    the +/-3% that costs errs slightly TIGHT (less padding), which is the
    friendly direction for the thing this was written to fix.

    NOTE it sets blf's global size as a side effect - it has to, to measure - so
    it is only ever reached from the fit maths, never after the final
    blf.size()."""
    r = _cap_cache.get("cap")
    if r is None:
        blf.size(0, _REF_SIZE)
        r = _cap_cache["cap"] = max(0.1, blf.dimensions(0, "M")[1] / _REF_SIZE)
    return r


def _label_width_ratio(text):
    """Text width divided by its NOMINAL font size, measured once at a reference
    size. Unitless, so a canvas-unit font size converts straight into a
    canvas-unit text width.

    Deliberately not tw/th: `blf.dimensions` height depends on which glyphs the
    string happens to contain ("spine" has no ascender/descender, "Bpg" does), so
    sizing off it would make every label a slightly different size. The whole
    point of a global text size is that all writing matches."""
    if not text:
        return 0.0
    r = _ratio_cache.get(text)
    if r is None:
        blf.size(0, _REF_SIZE)
        tw, _th = blf.dimensions(0, text)
        r = tw * _RATIO_PAD / _REF_SIZE
        if len(_ratio_cache) > 4096:
            _ratio_cache.clear()
        _ratio_cache[text] = r
    return r


def _text_box(btn, bw, bh):
    """The usable ink box inside a button, after padding. Same unit in as out.

    ONE padding number on all four sides, taken from the button's SMALLER drawn
    side so it can't be wider than the button is (v0.12.0 - see TEXT_PAD). A
    GROUP's usable box is its inscribed square, since the corners of a circle
    aren't there to write in."""
    if btn.kind == 'GROUP':
        bw, bh = bw * GROUP_INSCRIBE, bh * GROUP_INSCRIBE
    pad = TEXT_PAD * min(bw, bh)
    return bw - 2.0 * pad, bh - 2.0 * pad


def _btn_text(btn):
    """The label this button actually draws - "" while it's blank.

    Blank is a real per-button FLAG, not an empty `label`: every text site reads
    `label or bone`, so clearing the label just falls back to the bone name (a
    0.01 x 0.01 button with label="" and bone="master" measured out at
    0.190 x 0.0909, drawing "master"). Both the label pass in _draw_callback and
    the fit below go through here."""
    if btn.blank:
        return ""
    if btn.kind == 'SLIDER':
        # live readout: percent of the key's OWN slider range, so a -1..1 or
        # 0..2 key still reads 0-100. The blank check above runs first, which
        # also keeps duck-typed ghosts (no sk_* attrs) out of here.
        kb = _slider_key(btn)
        pct = int(round(_slider_frac(kb) * 100.0)) if kb else 0
        return "%s %d%%" % (btn.label or btn.sk_key, pct)
    return btn.label or btn.bone


def _label_fit(btn, bw, bh):
    """The biggest NOMINAL font size this button could hold, in whatever unit
    `bw`/`bh` are given in. 0.0 when there's nothing to draw.

    THE BUTTON IS THE SIZE YOU MADE IT AND THE TEXT FITS INSIDE IT (v0.11.0).
    That is the reverse of every version before it, where the button grew to fit
    the label and a long name could floor a button to 63% of the image width.
    There is no text size setting: the label is a function of the box, so Button
    Scale - which scales the box - scales the writing with it for free.

    Both budgets are spent on INK: the width one always was (`blf.dimensions`
    measures the glyphs), and since v0.12.0 the height one is too, via
    _cap_ratio. Unit-agnostic on purpose - the draw path feeds it px, and a font
    size in, say, canvas-Y units is exactly what a px size is after dividing by
    sy."""
    text = _btn_text(btn)
    if not text or bw <= 0.0 or bh <= 0.0:
        return 0.0
    aw, ah = _text_box(btn, bw, bh)
    if aw <= 0.0 or ah <= 0.0:
        return 0.0
    if btn.kind == 'SLIDER':
        # fit against the WIDEST string this slider can show ("... 100%"), not
        # the live one - otherwise the size would pump while the value drags
        text = "%s 100%%" % (btn.label or btn.sk_key)
    ratio = _label_width_ratio(text)
    by_h = ah / _cap_ratio()
    by_w = (aw / ratio) if ratio > 0.0 else by_h
    return min(by_w, by_h)


def _btn_wh(btn):
    """Drawn size in canvas units: what the user set, times this button's scale.

    No label floor any more - see _label_fit. Hit-testing, box-select, align and
    drawing all read this, so they can't disagree about how big a button is."""
    return btn.w * btn.scale, btn.h * btn.scale


def _gap(context=None):
    """Align's gap, as a fraction of the buttons' own drawn size."""
    scene = getattr(context or bpy.context, "scene", None)
    return getattr(scene, "madi_picker_gap", ALIGN_GAP) if scene else ALIGN_GAP


def _on_label_update(self, context):
    _tag_redraw(context)


def _on_scale_update(self, context):
    """Button Scale is a brush, live to the selection only - and RELATIVE, so a
    selection of differently-sized buttons keeps its differences. It scales the
    label too, because the label is derived from the boxes (see _label_fit)."""
    new = context.scene.madi_picker_scale
    # a programmatic write (sync, preset load, migration) is a new baseline to
    # measure the next drag against, never something to act on
    if _state["suppress_apply"] or _state["scale_prev"] is None:
        _state["scale_prev"] = new
        return
    _scale_selected(context, new, _state["scale_prev"])
    _state["scale_prev"] = new


def _on_gap_update(self, context):
    """Align Gap is LIVE on the last align: dragging it re-runs that align at the
    new gap, instead of changing nothing until Align is pressed again.

    Unlike the size brushes this needs no `prev` baseline to measure against -
    the gap is an absolute fraction and every write re-derives the whole run from
    its cached PRE-align positions (see _apply_gap_live), so a drag can be
    reversed exactly. `gap_prev` only skips a redundant re-run."""
    new = context.scene.madi_picker_gap
    if _state["suppress_apply"] or new == _state["gap_prev"]:
        _state["gap_prev"] = new
        return
    _state["gap_prev"] = new
    _apply_gap_live(context)


def _on_bone_update(self, context):
    """Retargeting a bone name only needs a repaint (red outline clears)."""
    _tag_redraw(context)


class MADI_PickerBoneRef(PropertyGroup):
    """One bone inside a GROUP button. Its own property so each member can be
    retargeted individually in the UI."""
    bone: StringProperty(name="Bone", update=_on_bone_update)


class MADI_PickerButton(PropertyGroup):
    kind: EnumProperty(
        name="Kind",
        items=[('BONE', "Bone", "Square button that selects one bone"),
               ('GROUP', "Group", "Round button that selects several bones"),
               ('SLIDER', "Slider",
                "Horizontal slider that drives a shape key (v0.19.0)")],
        default='BONE')
    bone: StringProperty(name="Bone", update=_on_bone_update)   # kind == BONE
    members: CollectionProperty(type=MADI_PickerBoneRef)        # kind == GROUP
    # kind == SLIDER: the mesh OBJECT (by name - a PointerProperty would keep
    # the mesh alive in the .blend even after the user deletes it) and the
    # shape key block it drives. Resolved live by _slider_key on every read,
    # so a renamed/deleted target degrades to the red "missing" outline
    # instead of a stale pointer.
    sk_object: StringProperty(name="Object", update=_on_bone_update)
    sk_key: StringProperty(name="Shape Key", update=_on_bone_update)
    label: StringProperty(name="Label", update=_on_label_update)
    # CANVAS-space position: same coords the image occupies, so buttons track
    # the picture through pan/zoom. (0,0)..(1,1) spans the image.
    x: FloatProperty(name="X", default=0.5)
    y: FloatProperty(name="Y", default=0.5)
    # independent width/height in canvas units, so buttons scale with zoom like
    # the image AND each side can be resized on its own
    w: FloatProperty(name="W", default=DEFAULT_SCALE,
                     min=0.002, max=5.0, precision=3, step=0.1)
    h: FloatProperty(name="H", default=DEFAULT_SCALE,
                     min=0.002, max=5.0, precision=3, step=0.1)
    color: FloatVectorProperty(name="Color", subtype='COLOR', size=3,
                               default=DEFAULT_COLOR, min=0.0, max=1.0)
    # NOTE: there is no per-button `font` any more (v0.11.0). The label is sized
    # from the button it sits in - see _label_fit. Presets from v1-v4 carry one;
    # it is read and discarded.
    # Per-button multiplier on w/h - and therefore on the label, which is derived
    # from them. Never written back into w/h, so returning it to 1.0 restores
    # exactly what was drawn.
    scale: FloatProperty(name="Scale", default=1.0, min=0.05, max=20.0,
                         precision=2, step=5.0, update=_on_label_update)
    # THIS button is the one the user picked, as opposed to "some button for this
    # bone is picked". Only ever consulted between buttons that map to the same
    # thing (see _btn_is_selected): with no duplicates it changes nothing, and a
    # bone selected out in the viewport - where there's no button to flag - still
    # lights every button it has.
    select: BoolProperty(name="Picked", default=False)
    # Draw no text at all. Per button, not a live read of the scene toggle, or
    # switching that toggle on would retro-blank every button already made.
    # Note the label is KEPT while blank, so un-ticking this brings it back.
    blank: BoolProperty(
        name="Blank", default=False,
        description="Draw this button with no text at all (the label is kept, "
                    "just not shown)",
        update=_on_label_update)
    # The size this button was PLACED at, for Reset Size. 0.0 == no origin on
    # record (a button from before the feature that has never been through a file
    # load); Reset Size skips those rather than inventing a size they never had.
    w0: FloatProperty(name="Placed W", default=0.0, min=0.0, max=5.0)
    h0: FloatProperty(name="Placed H", default=0.0, min=0.0, max=5.0)
    scale0: FloatProperty(name="Placed Scale", default=0.0, min=0.0, max=20.0)
    # which TAB this button belongs to. Buttons still live on the armature (so
    # they save with the .blend and follow the rig), but one rig can back several
    # tabs - face / body / props - and two tabs can even share a rig.
    # uid 0 is the default tab, which is also what pre-tabs layouts load as.
    tab_uid: IntProperty(name="Tab", default=0)


class MADI_PickerData(PropertyGroup):
    buttons: CollectionProperty(type=MADI_PickerButton)
    active_index: IntProperty(name="Active Button", default=0)


def _on_tab_image_update(self, context):
    """Picking a reference image shows it in the Image Editor - but only for the
    tab you're actually looking at, so a script touching a background tab can't
    yank the picture out from under the one on screen. Only while the picker
    RUNS (job 26): stopped, the editors belong to the user."""
    if _state["running"] and self == _active_tab(context):
        _show_image(context, self.image)
    _tag_redraw(context)


def _on_tab_target_update(self, context):
    _state["hover_idx"] = -1
    _tag_redraw(context)


class MADI_PickerTab(PropertyGroup):
    """One picker page: a rig + a reference image + a slice of that rig's
    buttons. Lives on the Scene, so a scene can hold a picker per character."""
    name: StringProperty(name="Name", default="Picker",
                         update=_on_tab_target_update)
    uid: IntProperty(name="UID", default=0)
    armature: PointerProperty(
        type=bpy.types.Object, name="Armature",
        description="The rig this tab's buttons drive",
        poll=lambda self, obj: obj.type == 'ARMATURE',
        update=_on_tab_target_update)
    image: PointerProperty(
        type=bpy.types.Image, name="Background",
        description="Reference picture shown in the Image Editor for this tab "
                    "- trace the layout over it",
        update=_on_tab_image_update)


# ---------------------------------------------------------------------------
# Tabs
# ---------------------------------------------------------------------------
def _tabs(context):
    return context.scene.madi_picker_tabs


def _new_tab_uid(context):
    """A uid no tab has ever had, from a monotonic high-water mark on the Scene.

    `max(live tab uids) + 1` was NOT enough: remove the last tab and its uid comes
    straight back, so anything still carrying it lands on the next tab created.
    The counter only ever goes up, so a uid is handed out exactly once.

    A file made before the counter existed starts it at whatever is already in
    use - including uids found on BUTTONS, so an orphan stranded by the old
    remove-tab bug can't be inherited either."""
    scene = context.scene
    nxt = scene.madi_picker_uid_next
    for t in _tabs(context):
        nxt = max(nxt, t.uid + 1)
    for ob in bpy.data.objects:
        if ob.type == 'ARMATURE':
            for b in ob.madi_picker.buttons:
                nxt = max(nxt, b.tab_uid + 1)
    scene.madi_picker_uid_next = nxt + 1
    return nxt


def _purge_tab_buttons(uid):
    """Delete every button carrying `uid`, on EVERY armature. -> how many.

    Not just `tab.armature`: that pointer is empty whenever the rig was deleted
    (Blender nulls it) or the user re-pointed the field before removing the tab,
    and the buttons would then stay behind at a uid no tab has - invisible to
    _iter_buttons and to the UIList, undeletable from the UI, and still written
    into the .blend. Two tabs sharing one rig have different uids, so this still
    only ever takes the tab's own buttons."""
    n = 0
    for ob in bpy.data.objects:
        if ob.type != 'ARMATURE':
            continue
        coll = ob.madi_picker.buttons
        doomed = [j for j, b in enumerate(coll) if b.tab_uid == uid]
        if not doomed:
            continue
        for j in reversed(doomed):        # by INDEX, high first: a remove()
            coll.remove(j)                # compacts everything above it
            n += 1
        ob.madi_picker.active_index = max(
            0, min(ob.madi_picker.active_index, len(coll) - 1))
    return n


def _ensure_tabs(context):
    """Guarantee one tab exists, migrating a pre-tabs layout into it.

    Old files stored the rig in Scene.madi_picker_target and every button has
    tab_uid 0 by default, so the first tab (uid 0) inherits them untouched."""
    scene = context.scene
    tabs = scene.madi_picker_tabs
    if len(tabs):
        return _active_tab(context)
    tab = tabs.add()
    tab.uid = 0
    legacy = getattr(scene, "madi_picker_target", None)
    tab.armature = legacy
    tab.name = legacy.name if legacy else "Picker 1"
    scene.madi_picker_tab_index = 0
    return tab


def _active_tab(context):
    tabs = context.scene.madi_picker_tabs
    i = context.scene.madi_picker_tab_index
    if 0 <= i < len(tabs):
        return tabs[i]
    return tabs[0] if len(tabs) else None


def _active_uid(context):
    tab = _active_tab(context)
    return tab.uid if tab else 0


def _show_image(context, image):
    """Put `image` in every Image Editor (in practice: all of them). None
    CLEARS the editor since v0.14.0 (job 23): every tab owns its background,
    so a tab with no reference must not sit on the previous tab's picture."""
    for area in context.screen.areas:
        if area.type != 'IMAGE_EDITOR':
            continue
        space = area.spaces.active
        if space and space.image != image:    # == compares the real pointer;
            space.image = image               # `is` never matches bpy wrappers
        area.tag_redraw()


def _snapshot_session_images(context):
    """Record what every Image Editor is showing, keyed by space pointer, so
    stopping the picker can hand the editors back untouched (job 26)."""
    saved = {}
    for area in context.screen.areas:
        if area.type != 'IMAGE_EDITOR':
            continue
        space = area.spaces.active
        if space:
            saved[space.as_pointer()] = space.image.name if space.image else None
    _state["saved_images"] = saved


def _restore_session_images(context):
    """Put every Image Editor back to its pre-session image (job 26).

    Empties the snapshot first, so the Stop button and `_finish` can both call
    it and only the first does anything. An editor opened MID-session has no
    snapshot; it only gets cleared if it's showing the picker's own background
    - an image the user opened there deliberately is none of our business."""
    saved = _state["saved_images"]
    _state["saved_images"] = {}
    if not saved:
        return
    tab = _active_tab(context)
    bg = tab.image if tab else None
    for area in context.screen.areas:
        if area.type != 'IMAGE_EDITOR':
            continue
        space = area.spaces.active
        if not space:
            continue
        key = space.as_pointer()
        if key in saved:
            name = saved[key]
            img = bpy.data.images.get(name) if name else None
        elif bg is not None and space.image == bg:
            img = None
        else:
            continue
        if space.image != img:
            space.image = img
        area.tag_redraw()


def _on_tab_index_update(self, context):
    tab = _active_tab(context)
    # image swap only while running (job 26) - and None CLEARS (job 23), so a
    # tab with no reference doesn't sit on another tab's picture
    if tab and _state["running"]:
        _show_image(context, tab.image)
    _state["hover_idx"] = -1
    # a queue armed for THIS tab's rig must not follow you to another one
    _clear_place()
    # ...and neither must a cached align run: the gap slider would otherwise be
    # live on buttons that are no longer on screen
    _state["align_run"] = None
    _tag_redraw(context)


# ---------------------------------------------------------------------------
# Add-on preferences (Edit > Preferences > Add-ons > MADI Picker)
# ---------------------------------------------------------------------------
# ⚠ THE PICKER HAS NO AddonPreferences CLASS OF ITS OWN ANY MORE.
#
# As a single-file add-on it carried one, keyed `bl_idname = __name__`. Inside a
# package that expression is `bl_ext.user_default.madi_anim_library.picker`,
# which is NOT an add-on key - `preferences.addons.get(...)` returns None, and
# every reader below already tolerates None by falling back to a constant
# (`p.btn_round if p is not None else BTN_ROUND`). So NOTHING would raise and
# NOTHING would be logged: Button Opacity, Corner Roundness and Darken
# Background would simply stop working, and read as settings that reset
# themselves. Proven headless before the move - `_prefs()` returned None and the
# probe ran happily to the end.
#
# The three properties now live on `MADILIB_Prefs` as `pk_*`, which is the
# extension's real preferences class, and this looks them up by __package__.
# `picker_core_test.py` asserts `_prefs()` is not None after register, because
# that assertion is the only thing standing between this and a silent
# regression.
def _prefs():
    """The extension's preferences, or None mid-(un)register."""
    ad = bpy.context.preferences.addons.get(__package__)
    return getattr(ad, "preferences", None) if ad else None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _target(context):
    """The armature object the picker drives (the active tab's rig)."""
    tab = _active_tab(context)
    if tab:
        return tab.armature
    return context.scene.madi_picker_target


def _iter_buttons(context, arm=None):
    """(index, button) for the buttons on the ACTIVE TAB only.

    The index is the real index into arm.madi_picker.buttons - hover, delete and
    the UIList all address the full collection, so filtering must not renumber."""
    arm = arm if arm is not None else _target(context)
    if not arm:
        return []
    uid = _active_uid(context)
    return [(i, b) for i, b in enumerate(arm.madi_picker.buttons)
            if b.tab_uid == uid]


def _active_bone_and_arm(context):
    """Resolve the bone the user means to map, and its armature object.
    Prefers the actual active pose bone (whatever rig they're posing), then
    falls back to the picker target's active data bone."""
    pb = context.active_pose_bone
    if pb:
        return pb.id_data, pb.name        # id_data == the armature object
    arm = _target(context)
    if arm and arm.data.bones.active:
        return arm, arm.data.bones.active.name
    return None, None


def _bone_shown(pb):
    """Is this pose bone actually on screen?

    `bone.hide` is only half the answer: a bone on a hidden bone COLLECTION has
    hide == False and keeps its selection flag, so a multi-add would make buttons
    for a whole rig's worth of bones the user can't see. Live on Lily - 30
    collections, 20 of them hidden (Tweak, Widgets, FK Arm Left, Fingers Left...).

    `is_visible_effectively`, not `is_visible`: collections nest, so a visible
    child of a hidden parent is still off screen. A bone in NO collection has
    nothing hiding it, so it counts as visible."""
    bone = pb.bone
    if bone.hide:
        return False
    colls = bone.collections
    return not colls or any(c.is_visible_effectively for c in colls)


def _btn_bones(btn):
    """Every bone a button maps to (1 for BONE, N for GROUP, none for SLIDER)."""
    if btn.kind == 'GROUP':
        return [m.bone for m in btn.members if m.bone]
    if btn.kind == 'SLIDER':
        return []
    return [btn.bone] if btn.bone else []


def _slider_key(btn):
    """The live shape key block a SLIDER drives, or None. Resolved by NAME on
    every read - never cached, never a pointer - so a deleted or renamed mesh
    degrades to the red missing outline, exactly like a missing bone."""
    ob = bpy.data.objects.get(btn.sk_object)
    keys = getattr(getattr(ob, "data", None), "shape_keys", None) if ob else None
    return keys.key_blocks.get(btn.sk_key) if keys else None


def _slider_frac(kb):
    """The key's value as 0..1 of its OWN slider range (slider_min..slider_max),
    so the fill and the percent read 0-100 whatever the range really is."""
    span = kb.slider_max - kb.slider_min
    if span <= 0.0:
        return 0.0
    return max(0.0, min(1.0, (kb.value - kb.slider_min) / span))


def _slider_set_frac(kb, f):
    kb.value = kb.slider_min + (kb.slider_max - kb.slider_min) * max(
        0.0, min(1.0, f))


def _slider_fcurve_owner(kb):
    """(container, F-Curve) for this shape key's value, or (None, None).

    The CONTAINER comes back too because removing a channel needs its owner,
    not just the curve (Clear Keyframes, v0.24.0). 5.2 actions are SLOTTED:
    the curves live in the slot's channelbag and `Action.fcurves` doesn't exist
    any more (probed live - the attribute is gone, not empty), so this walks
    layers -> strips -> channelbag and keeps a guarded fallback for a pre-slot
    action, where the action itself is the container."""
    ad = getattr(kb.id_data, "animation_data", None)
    act = ad.action if ad is not None else None
    if act is None:
        return None, None
    path = 'key_blocks["%s"].value' % bpy.utils.escape_identifier(kb.name)
    slot = getattr(ad, "action_slot", None)
    if slot is not None:
        for lay in act.layers:
            for st in lay.strips:
                try:
                    cb = st.channelbag(slot)
                except Exception:
                    cb = None
                fc = cb.fcurves.find(path) if cb is not None else None
                if fc is not None:
                    return cb, fc
    fcs = getattr(act, "fcurves", None)          # pre-slot files
    fc = fcs.find(path) if fcs is not None else None
    return (act, fc) if fc is not None else (None, None)


def _slider_fcurve(kb):
    """The F-Curve driving this shape key's value, or None."""
    return _slider_fcurve_owner(kb)[1]


def _fcurve_has_key_at(fc, frame):
    """Is there a keyframe on this frame? Binary search - keyframe_points are
    sorted by time, and a face curve can carry hundreds of them per redraw."""
    kps = fc.keyframe_points
    lo, hi = 0, len(kps) - 1
    while lo <= hi:
        mid = (lo + hi) // 2
        x = kps[mid].co[0]
        if abs(x - frame) < 0.5:
            return True
        if x < frame:
            lo = mid + 1
        else:
            hi = mid - 1
    return False


def _slider_key_state(kb, frame):
    """0 = not animated · 1 = animated · 2 = keyed ON this frame (v0.22.0)."""
    if kb is None:
        return 0
    fc = _slider_fcurve(kb)
    if fc is None:
        return 0
    return 2 if _fcurve_has_key_at(fc, frame) else 1


def _tab_slider_keys(context, arm=None):
    """(mesh, key) pairs that already wear a SLIDER on the ACTIVE TAB.

    Per TAB, not per file (v0.22.0, Marty's rule): a tab is one page, and the
    same key on a face page and a body page is legitimate - but two sliders for
    one key on the SAME page are two things fighting over one value."""
    arm = arm if arm is not None else _target(context)
    if not arm:
        return set()
    return {(b.sk_object, b.sk_key) for _i, b in _iter_buttons(context, arm)
            if b.kind == 'SLIDER'}


def _btn_missing(arm, btn):
    """Names this button maps to that don't resolve (bones - or, for a SLIDER,
    its mesh/shape key)."""
    if not arm or not arm.pose:
        return []
    if btn.kind == 'SLIDER':
        if _slider_key(btn) is None:
            return [btn.sk_key or btn.sk_object or ""]
        return []
    bones = arm.pose.bones
    if btn.kind == 'BONE':
        # fast path (v0.24.1, perf): the same answer without building two
        # throwaway lists per button per redraw - this runs 155 times a frame
        n = btn.bone
        if not n:
            return [""]
        return [] if n in bones else [n]
    missing = [n for n in _btn_bones(btn) if n not in bones]
    if btn.kind == 'BONE' and not btn.bone:
        missing.append("")            # unmapped button counts as needing a bone
    return missing


def _btn_key(btn):
    """What a DUPLICATE shares: two buttons with the same key map to exactly the
    same thing, and are the only case where per-button picking matters."""
    if btn.kind == 'GROUP':
        return tuple(m.bone for m in btn.members)
    if btn.kind == 'SLIDER':
        return (btn.sk_object, btn.sk_key)
    return btn.bone


def _claimed_bones(context, arm):
    """Every bone covered by a PICKED button (`btn.select`) on the active tab.

    One pass per redraw. BONES, not keys, since v0.14.0 (job 24): clicking a
    GROUP is a specific pick of that handle, so a lone "chest" button across
    the page must not light up just because the group contains chest - exactly
    the same reason a clicked duplicate's twin stays dark."""
    if not arm:
        return frozenset()
    out = set()
    for _i, b in _iter_buttons(context, arm):
        if b.select:
            out.update(_btn_bones(b))
    return out


def _btn_sel_state(btn, sel, claimed=None):
    """0 = unselected · 1 = SELECTED · 2 = COVERED.

    A BONE button follows its bone; a GROUP button needs ALL of its members
    selected. When the user has PICKED specific buttons (clicked / box-touched
    them), a button they didn't pick whose bone is already claimed by a pick is
    NOT selected - Del, brushes and Align must act on the pick alone (job 24) -
    but it IS "covered", and draws with a secondary highlight so you can see
    exactly which buttons a clicked group (or a clicked twin) is driving
    (job 25). With no pick on record (a bone selected in the VIEWPORT, where
    there is no button to click) every button for it is fully selected, which
    is the thing that makes a picker a picker."""
    if btn.kind == 'SLIDER':
        # no bones to follow: the flag itself is the selection, set by
        # box-select (clicking a slider SCRUBS it, it doesn't select it), so
        # layout edits - Del, align, Ctrl+multi-drag, colour - reach sliders
        return 1 if btn.select else 0
    if btn.kind == 'BONE':
        # fast path (v0.24.1, perf): identical rule, no list built. A BONE
        # button has exactly one name, so "all members selected" and "any name
        # claimed" both collapse to a single membership test.
        n = btn.bone
        if not n or n not in sel:
            return 0
        if claimed and not btn.select and n in claimed:
            return 2
        return 1
    names = _btn_bones(btn)
    if not names:
        return 0
    if btn.kind == 'GROUP':
        if not all(n in sel for n in names):
            return 0
    elif names[0] not in sel:
        return 0
    if claimed:
        if btn.select:
            return 1
        if any(n in claimed for n in names):
            return 2
    return 1


def _btn_is_selected(btn, sel, claimed=None):
    """True only for state 1 - what edits, deletes and brushes act on."""
    return _btn_sel_state(btn, sel, claimed) == 1


def _clear_place():
    """Disarm click-to-place. Anything left in the queue is simply dropped."""
    n = (len(_state["place"]) + len(_state["place_sliders"])
         + (1 if _state["place_group"] else 0))
    _state["place"] = []
    _state["place_group"] = None
    _state["place_sliders"] = []
    _state["place_mouse"] = None
    _state["place_region"] = 0
    return n


def _place_armed():
    """PLACE mode? Three queues share it: bones, an armed group, sliders."""
    return bool(_state["place"] or _state["place_group"]
                or _state["place_sliders"])


def _bone_label(bone_name):
    """The short label a new button gets - the button grows to fit it."""
    return bone_name.split("-")[-1][:6]


def _slider_label(key_name):
    """A slider's default label: the key's name, kept readable - sliders are
    wide, so they can afford more than a bone button's 6 characters."""
    return key_name[:14]


def _clear_btn_flags(arm):
    """Forget every per-button pick. Called wherever the selection is being
    replaced wholesale, so a stale flag can't keep a twin dark."""
    if not arm:
        return
    for btn in arm.madi_picker.buttons:
        if btn.select:
            btn.select = False


def _drop_dead_picks(context, arm, dead_bones):
    """Let the bones of a pick that just got DELETED go with it.

    The pick lives on the button (`btn.select`), so deleting the button you had
    clicked deletes the pick - while its BONE stays selected. With no pick left on
    record `_btn_is_selected` falls back to "light every button for this bone"
    (the rule that makes viewport selection work), and the twins you never touched
    come up selected out of nowhere.

    Your selection *was* that button and it's gone, so the bones go too - unless
    a surviving PICKED button still covers them (shift+click can flag several),
    which means a pick is still on record and the selection still means
    something."""
    if not arm or not arm.pose:
        return
    names = set(dead_bones) - _claimed_bones(context, arm)
    hit = False
    for name in names:
        pb = arm.pose.bones.get(name)
        if pb and pb.select:
            pb.select = False
            hit = True
    act = arm.data.bones.active
    if act and act.name in names:
        arm.data.bones.active = None      # or it keeps its outline
        hit = True
    if hit:
        _tag_redraw(context)


def _remove_buttons(context, arm, idxs, drop_picks=True):
    """Delete buttons by index, taking any pick that dies with them. -> count.

    Every path that destroys a button goes through here - Del/X, the panel's
    Remove, a Replace-mode preset load - so the twin-relight rule lives in ONE
    place, the same way the delete rule itself does."""
    coll = arm.madi_picker.buttons
    idxs = sorted({i for i in idxs if 0 <= i < len(coll)}, reverse=True)
    if not idxs:
        return 0
    # read the doomed picks' BONES before the removes: remove(i) compacts the
    # array in place, so every index above i means a different button afterwards
    dead = set()
    if drop_picks:
        for i in idxs:
            if coll[i].select:
                dead.update(_btn_bones(coll[i]))
    for i in idxs:                        # high index first, same reason
        coll.remove(i)
    arm.madi_picker.active_index = max(
        0, min(arm.madi_picker.active_index, len(coll) - 1))
    if dead:
        _drop_dead_picks(context, arm, dead)
    return len(idxs)


def _selected_bone_names(arm):
    if not arm or not arm.pose:
        return set()
    return {pb.name for pb in arm.pose.bones if pb.select}


def _deselect_all(arm):
    """Clear selection AND the active bone, so nothing keeps its outline."""
    if not arm or not arm.pose:
        return
    for pb in arm.pose.bones:
        pb.select = False
    arm.data.bones.active = None
    _clear_btn_flags(arm)


def _select_bones(arm, bone_names, extend):
    """Select bones on the armature (5.2: flag lives on the pose bone)."""
    if not arm or not arm.pose:
        return
    if not extend:
        for pb in arm.pose.bones:
            pb.select = False
        # a fresh selection replaces the per-button picks too; the caller flags
        # whichever button it was that did the selecting
        _clear_btn_flags(arm)
    first = None
    for name in bone_names:
        pb = arm.pose.bones.get(name)
        if pb:
            pb.select = True
            first = first or name
    if first:
        b = arm.data.bones.get(first)
        if b:
            arm.data.bones.active = b


def _select_bone(arm, bone_name, extend):
    _select_bones(arm, [bone_name], extend)


def _ensure_pose_mode(context, arm):
    """Take the tab's rig into POSE mode, so a picked bone is really picked.

    Clicking a button in Object Mode used to set the pose-bone flags and do
    nothing you could see - the button lit up in the picker, the viewport sat
    there (v0.21.0, Marty). A click on a bone button is an unambiguous "I want
    this bone", so the picker switches for you; whatever the active object was
    in the middle of (another rig's pose, a mesh's edit/sculpt/paint mode) is
    left cleanly through OBJECT first, which is what Blender itself does.

    Object SELECTION is deliberately left alone: probed live, both Pose Mode
    and context.selected_pose_bones work on an unselected-but-active rig, so
    there is no reason to disturb what the user has selected. No 3D-Viewport
    override either - object.mode_set polls True from the Image Editor
    (probed); only the KEYFRAME operators need one.

    Returns an error string for report(), never raises: an exception in the
    modal takes the whole session down (gotcha 3)."""
    if arm is None or arm.mode == 'POSE':
        return None
    vl = context.view_layer
    if arm.name not in vl.objects:
        return "%s is not in this view layer - can't enter Pose Mode" % arm.name
    try:
        act = vl.objects.active
        if act is not None and act != arm and act.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')   # `!=`, never `is`, gotcha 24
        vl.objects.active = arm
        bpy.ops.object.mode_set(mode='POSE')
    except Exception as e:
        return str(e)
    return None


# --- keyframing over the canvas (v0.18.0) -----------------------------------
# The keyframe key works while the mouse is over the picker. There is no
# hardcoded key: the binding is read from the USER'S Pose keymap at press time,
# so both presets work (Blender: I; Industry Compatible: S / Shift+S /
# Shift+W/E/R) and the F6 keymap-toggle add-on keeps working with no cache to
# go stale. A key press is rare - scanning ~50 keymap items then is nothing
# (don't "optimize" a cache in).
_KF_PREFIX = "anim.keyframe_insert"   # insert / insert_by_name / insert_menu


def _kf_match(context, event):
    """The user's Pose-keymap item this key press means, or None.

    Mouse buttons never reach this (the caller filters them) - the picker's
    pointer gestures always win over an exotic mouse binding."""
    kc = context.window_manager.keyconfigs.user
    km = kc.keymaps.get("Pose")
    if km is None:
        return None
    for kmi in km.keymap_items:
        if (not kmi.active or not kmi.idname.startswith(_KF_PREFIX)
                or kmi.type != event.type
                or kmi.value not in {'PRESS', 'ANY'}
                # a held-key modifier can't be verified from a modal event
                or kmi.key_modifier != 'NONE'):
            continue
        if event.is_repeat and not kmi.repeat:
            continue
        # kmi modifiers are ints: -1 = any, 0/1 must match the event exactly
        if any(want != -1 and bool(want) != have for want, have in
               ((kmi.ctrl, event.ctrl), (kmi.shift, event.shift),
                (kmi.alt, event.alt), (kmi.oskey, event.oskey))):
            continue
        return kmi
    return None


def _kf_insert(context, kmi):
    """Run the matched keyframe operator as if pressed in the 3D viewport.

    The override is MANDATORY: keyframe ops poll False outside the viewport
    (probed live - no override, no keys). Errors are reported, never raised -
    an exception here would take the whole modal session down with it."""
    cat, _, opname = kmi.idname.partition(".")
    op = getattr(getattr(bpy.ops, cat, None), opname, None)
    if op is None:
        return "unknown operator %r" % kmi.idname
    kwargs = {}
    props = kmi.properties
    if props is not None:
        for p in props.bl_rna.properties:
            if p.identifier != "rna_type" and props.is_property_set(p.identifier):
                kwargs[p.identifier] = getattr(props, p.identifier)
    area = next((a for a in context.window.screen.areas
                 if a.type == 'VIEW_3D'), None)
    if area is None:
        return "no 3D Viewport on this screen to key in"
    region = next((r for r in area.regions if r.type == 'WINDOW'), None)
    try:
        with context.temp_override(area=area, region=region):
            if not op.poll():
                return "can't insert keyframes here"
            op('INVOKE_DEFAULT', **kwargs)
    except Exception as e:
        return str(e)
    return None


def _kf_insert_slider(btn):
    """Key a SLIDER's shape key value at the current frame. -> error or None.

    Plain data-API keying on the key block itself - no viewport override
    needed, and Blender routes it into the Key datablock's (slotted) action."""
    kb = _slider_key(btn)
    if kb is None:
        return "slider target missing: %s / %s" % (btn.sk_object or "?",
                                                   btn.sk_key or "?")
    try:
        kb.keyframe_insert("value")
    except Exception as e:
        return str(e)
    return None


def _kf_remove_slider(btn):
    """Take THIS frame's key back off a SLIDER's shape key. -> error or None.

    `keyframe_delete` raises when there is nothing on the frame, which is the
    common case (you Alt+Shift+clicked an unkeyed frame) - so it comes back as
    a plain message, not a traceback that would end the modal (gotcha 3)."""
    kb = _slider_key(btn)
    if kb is None:
        return "slider target missing: %s / %s" % (btn.sk_object or "?",
                                                   btn.sk_key or "?")
    try:
        if not kb.keyframe_delete("value"):
            return "no keyframe on this frame"
    except Exception:
        return "no keyframe on this frame"
    return None


# --- canvas <-> region coordinate conversion --------------------------------
def _view_scale(region):
    """Pixels per canvas unit, as (sx, sy). Drives zoom-scaled button size."""
    v = region.view2d
    x0, y0 = v.view_to_region(0.0, 0.0, clip=False)
    x1, y1 = v.view_to_region(1.0, 1.0, clip=False)
    sx = float(x1 - x0) or 1.0
    sy = float(y1 - y0) or 1.0
    return sx, sy


def _view_aspect(region):
    """sy/sx - equals the image's height/width in px, so it's zoom-independent.

    Canvas coords are normalised PER AXIS ((0,0)-(1,1) spans the image whatever
    its shape), so on a 512x1536 reference this is 3.0: one canvas unit of Y is
    three times as many pixels as one canvas unit of X. Anything that has to come
    out with a real-world shape - text, circles - must go through this."""
    sx, sy = _view_scale(region)
    return sy / sx if sx else 1.0


def _picker_region(context):
    """Any Image Editor WINDOW region, for operators that need the view aspect
    but weren't launched from the canvas."""
    for area in context.screen.areas:
        if area.type == 'IMAGE_EDITOR':
            for region in area.regions:
                if region.type == 'WINDOW' and region.width > 1:
                    return region
    return None


def _canvas_to_px(region, cx, cy):
    return region.view2d.view_to_region(cx, cy, clip=False)


def _px_to_canvas(region, lx, ly):
    return region.view2d.region_to_view(lx, ly)


def _btn_rect(region, btn, sx=None, sy=None):
    """Button bounds in region pixels: (x0, y0, x1, y1).

    Uses the per-button-scaled size, so hit-testing, box-select and drawing all
    agree on how big a button really is."""
    if sx is None:
        sx, sy = _view_scale(region)
    w, h = _btn_wh(btn)
    cx, cy = _canvas_to_px(region, btn.x, btn.y)
    hw = w * sx * 0.5
    hh = h * sy * 0.5
    return cx - hw, cy - hh, cx + hw, cy + hh


def _edge_margin(hw, hh):
    """How deep the resize band reaches in from a button's border, in px."""
    return max(3.0, min(9.0, min(hw, hh) * 0.35))


def _btn_in_box(btn, bx0, by0, bx1, by1, xmin, ymin, xmax, ymax):
    """Does this button TOUCH the rubber band? (px, band already sorted)

    Box-select is an intersection test, like everywhere else in Blender: catching
    a button by its edge has to select it. It used to require the button's CENTRE
    to be inside the band, which meant a band drawn across a row of buttons could
    visibly cover half of each one and select none of them.

    A GROUP is an ELLIPSE, not its bounding box - a band clipping the empty
    corner of a circle isn't touching the button. Scaling the band by the radii
    turns the ellipse into a unit circle and makes the closest-point test exact."""
    if bx1 < xmin or bx0 > xmax or by1 < ymin or by0 > ymax:
        return False
    if btn.kind != 'GROUP':
        return True
    cx, cy = (bx0 + bx1) * 0.5, (by0 + by1) * 0.5
    rx, ry = (bx1 - bx0) * 0.5, (by1 - by0) * 0.5
    if rx <= 0.0 or ry <= 0.0:
        return True
    # band corners relative to the centre, in units of each radius
    nx = min(max(0.0, (xmin - cx) / rx), (xmax - cx) / rx)
    ny = min(max(0.0, (ymin - cy) / ry), (ymax - cy) / ry)
    return nx * nx + ny * ny <= 1.0


# ---------------------------------------------------------------------------
# ⚠ THE SOLID WALLS ARE GONE (2026-08-10) - buttons may overlap freely
# ---------------------------------------------------------------------------
# They existed from job 16 (2026-07-30, "buttons must never end up inside one
# another") until Marty asked for the opposite: "remove collision in Pickers
# buttons, they should not collide with each other". Roughly 150 lines went with
# them - _collide_statics/_collide_gap/_clamp_axis/_clamp_step/_grow_range/
# _clamp_edge, and the modal's _statics/_movers/_wall_gap/_wall_delta.
#
# ⚠ Do NOT reintroduce a "never overlap" rule if this is ever revisited. The
# hard-won part was never the clamping, it was that layouts predating the walls
# are full of legal overlaps, so the rule had to be "never INCREASE penetration"
# rather than "never overlap" - a flat test welds those pairs in place. That
# reasoning is in docs\bone-picker.md if it is ever needed again.
#
# ⚠ ALIGN IS A DIFFERENT THING AND IT STAYED. `_align_spread` still pushes an
# aligned run apart, because aligning is what CREATES the overlap and that is an
# explicit action the user asked for - not a wall appearing under their cursor.


def _hit_button(context, region, lx, ly):
    """-> (button, index, ex, ey). (None, -1, 0, 0) when nothing is hit.

    ex/ey say WHICH side the cursor is on (-1 low, +1 high, 0 = not near an
    edge), so scaling can anchor the opposite side."""
    arm = _target(context)
    if not arm:
        return None, -1, 0, 0
    sx, sy = _view_scale(region)
    # walk backwards so the topmost (last drawn) button wins
    for i, btn in reversed(_iter_buttons(context, arm)):
        x0, y0, x1, y1 = _btn_rect(region, btn, sx, sy)
        if not (x0 <= lx <= x1 and y0 <= ly <= y1):
            continue
        hw, hh = (x1 - x0) * 0.5, (y1 - y0) * 0.5
        m = _edge_margin(hw, hh)
        if btn.kind == 'GROUP':
            # round button: the bbox corners are NOT part of it, and its grips
            # belong on the ring, not on the bounding box - otherwise you'd grab
            # (and shadow buttons at) a corner you can't even see.
            nx = (lx - (x0 + x1) * 0.5) / hw if hw else 0.0
            ny = (ly - (y0 + y1) * 0.5) / hh if hh else 0.0
            nd = math.hypot(nx, ny)
            if nd > 1.0:
                continue
            ex = ey = 0
            band = m / max(1e-6, min(hw, hh))
            if nd > 1.0 - band and nd > 1e-6:
                # near the ring: the drag direction picks the side(s), so a
                # diagonal grab scales both axes like a rect corner does
                if abs(nx) > 0.38 * nd:
                    ex = 1 if nx > 0 else -1
                if abs(ny) > 0.38 * nd:
                    ey = 1 if ny > 0 else -1
            return btn, i, ex, ey
        ex = -1 if (lx - x0) < m else (1 if (x1 - lx) < m else 0)
        ey = -1 if (ly - y0) < m else (1 if (y1 - ly) < m else 0)
        return btn, i, ex, ey
    return None, -1, 0, 0


def _selected_buttons(context, arm=None):
    """Buttons on the active tab whose bone(s) are selected - what edits and
    multi-drag operate on. A hidden tab's buttons are never touched."""
    arm = arm if arm is not None else _target(context)
    if not arm:
        return []
    sel = _selected_bone_names(arm)
    claimed = _claimed_bones(context, arm)
    return [b for _i, b in _iter_buttons(context, arm)
            if _btn_is_selected(b, sel, claimed)]


def _apply_to_selected(context, prop, value):
    """Live-apply one appearance value to the SELECTED buttons, flat.

    Only Color works this way - a colour has no magnitude, so every selected
    button simply takes it. With no Apply button to gate it, a stray drag must
    never hit the whole layout, so with nothing selected this is just what the
    NEXT button added will get."""
    n = 0
    for btn in _selected_buttons(context):
        setattr(btn, prop, value)
        n += 1
    if n:
        _tag_redraw(context)
    return n


def _scale_selected(context, new, prev, lo=0.05, hi=20.0):
    """Multiply every selected button's `scale` by how far the slider just moved
    (`new / prev`).

    Setting them all to `new` instead would flatten a mixed selection to one
    size the instant you touched the slider - the differences you built are the
    thing being edited, not noise. So the slider is a relative dial: the button
    you clicked reads true (the sync put its value in the field, so its ratio is
    1:1 with the slider) and everything else keeps its proportion to it.

    The factor is clamped to what EVERY selected button can take, because a
    single button hitting its limit while the others keep going would silently
    destroy the ratios - permanently, since dragging back can't recover them.

    Job 18 (v0.13.0): each step also pulls a LABELLED button's height down to
    hug its text (_fit_height), so scaling up re-shapes the button instead of
    scaling its dead space up with it - "it's not important for them to remain
    square". Blank buttons and GROUP handles just multiply, exactly as before.
    w0/h0/scale0 are never touched, so Reset Size stays the undo for this. Per
    Marty's call the brush does NOT go through the wall clamp - a brush-scaled
    button may overlap a neighbour (only moves and edge-drags collide)."""
    btns = _selected_buttons(context)
    if not btns or prev is None or prev <= 0.0 or new <= 0.0:
        return 0
    f = new / prev
    if f <= 0.0 or f == 1.0:
        return 0

    for btn in btns:
        v = btn.scale
        if v <= 0.0:
            continue
        f = min(f, hi / v) if f > 1.0 else max(f, lo / v)
    if f <= 0.0 or f == 1.0:
        return 0
    region = _picker_region(context)
    aspect = _view_aspect(region) if region else 1.0
    n = 0
    for btn in btns:
        v = btn.scale
        if v <= 0.0:
            continue
        btn.scale = min(hi, max(lo, v * f))
        h = _fit_height(btn, aspect)      # None for blank / GROUP
        if h is not None and h < btn.h - 1e-9:
            btn.h = h
        n += 1
    if n:
        _tag_redraw(context)
    return n


def _on_color_update(self, context):
    if _state["suppress_apply"]:
        return
    _apply_to_selected(context, "color", context.scene.madi_picker_color)


def _sync_brushes(context, btn=None, color=False):
    """Load a button's REAL scale into the Button Scale slider.

    Clicking a button has to update the field, or it keeps showing whatever was
    dragged onto the *previous* button and reads as that one's value. Color is
    deliberately left alone unless asked for: a picked colour is something you
    carry between buttons, a size is a property of the button in front of you.
    """
    if btn is None:
        sel = _selected_buttons(context)
        if not sel:
            return          # nothing selected: the brushes are the new-button
        btn = sel[0]        # defaults now, so leave them where the user put them
    scene = context.scene
    # writing a brush normally applies it to the whole selection - not here
    _state["suppress_apply"] = True
    try:
        scene.madi_picker_scale = btn.scale
        if color:
            scene.madi_picker_color = btn.color
    finally:
        _state["suppress_apply"] = False
    # the slider now reads THIS button, so that's what the next drag is measured
    # from. (Set explicitly rather than trusting the update callback to fire -
    # assigning an unchanged value may not call it.)
    _remember_brushes(scene)


def _remember_brushes(scene):
    """Baseline the size dial at the slider's current value."""
    _state["scale_prev"] = scene.madi_picker_scale
    _state["gap_prev"] = scene.madi_picker_gap


def _new_button_height(context, arm):
    """The last button's height *on this tab* - the fallback shape for a new one
    when there's no selection to copy and no reason to go minimal."""
    btns = _iter_buttons(context, arm)
    return btns[-1][1].h if btns else DEFAULT_SCALE


def _new_button_shape(context, arm):
    """-> (w, h, scale, blank) the next button will be created at.

    Three rules, in order (Marty, 2026-07-30):
    1. **Something selected -> copy that button.** You're extending a layout, and
       the button in front of you is what the next one should match: its size,
       its scale, and whether it carries text at all - EXCEPT that a ticked
       "no label" always wins (job 21): selecting bones whose buttons carry
       text must not silently override the toggle you just set.
    2. **Nothing selected, "no label" on -> the smallest square, at the Button
       Scale you set.** A button with no writing in it has nothing to be wide
       for, and since v0.11.0 nothing inflates it either.
    3. **Nothing selected -> the last button on this tab's height** (the old
       rule), so a labelled layout stays consistent.

    Read BEFORE the collection grows, or rule 3 measures the button being
    created; the click-to-place ghost calls this too, so the preview and the
    button agree."""
    scene = context.scene
    sel = _selected_buttons(context, arm)
    if sel:
        b = sel[0]
        return b.w, b.h, b.scale, b.blank or scene.madi_picker_blank
    if scene.madi_picker_blank:
        return DEFAULT_SCALE, DEFAULT_SCALE, scene.madi_picker_scale, True
    return (DEFAULT_SCALE, _new_button_height(context, arm),
            scene.madi_picker_scale, False)


def _new_slider_shape(context, region=None):
    """(w, h) a new SLIDER is created at: SLIDER_ASPECT wide-to-tall ON SCREEN.

    The stored w corrects for the canvas' per-axis normalisation (gotcha 7 -
    `w == h` is not screen-square), same as a round GROUP does, so a slider is
    a slider-shaped bar on any reference image."""
    region = region or _picker_region(context)
    aspect = _view_aspect(region) if region else 1.0
    h = DEFAULT_SCALE * 0.75
    return h * SLIDER_ASPECT * aspect, h


def _add_slider_button(context, arm, ob_name, key_name):
    """Create a SLIDER button on the active tab, minus its position. The ONE
    slider creator - the placement click is the only caller."""
    w, h = _new_slider_shape(context)
    btn = arm.madi_picker.buttons.add()
    btn.kind = 'SLIDER'
    btn.sk_object = ob_name
    btn.sk_key = key_name
    btn.tab_uid = _active_uid(context)
    btn.w, btn.h = w, h
    btn.scale = context.scene.madi_picker_scale
    btn.color = context.scene.madi_picker_color
    btn.label = _slider_label(key_name)
    btn.w0, btn.h0, btn.scale0 = btn.w, btn.h, btn.scale
    arm.madi_picker.active_index = len(arm.madi_picker.buttons) - 1
    return btn


def _add_tab_button(context, arm, bone_name):
    """Create a BONE button on the active tab, minus its position."""
    # measured BEFORE the add - see _new_button_shape
    w, h, scale, blank = _new_button_shape(context, arm)
    btn = arm.madi_picker.buttons.add()
    btn.kind = 'BONE'
    btn.bone = bone_name
    btn.tab_uid = _active_uid(context)
    btn.w, btn.h, btn.scale = w, h, scale
    btn.color = context.scene.madi_picker_color   # colour is still the brush's
    btn.label = _bone_label(bone_name)
    # ...but a blank button doesn't draw it (the label is still stored, so the
    # per-button toggle in the list can bring it back)
    btn.blank = blank
    # what Reset Size goes back to - recorded here because nothing else can
    # recover it once an edge-drag or the Button Scale dial has been near it
    btn.w0, btn.h0, btn.scale0 = btn.w, btn.h, btn.scale
    arm.madi_picker.active_index = len(arm.madi_picker.buttons) - 1
    return btn


# ---------------------------------------------------------------------------
# Draw
# ---------------------------------------------------------------------------
def _draw_poly_lines(verts, color, width, vp):
    """Anti-aliased line segments in one uniform color."""
    if not verts:
        return
    sh = _shader('POLYLINE_UNIFORM_COLOR')
    batch = batch_for_shader(sh, 'LINES', {"pos": verts})
    sh.bind()
    sh.uniform_float("viewportSize", vp)
    sh.uniform_float("lineWidth", width)
    sh.uniform_float("color", color)
    batch.draw(sh)


def _draw_poly_lines_vc(verts, cols, width, vp):
    """Same, per-vertex color, so every button's outline goes out in one batch."""
    if not verts:
        return
    sh = _shader('POLYLINE_SMOOTH_COLOR')
    batch = batch_for_shader(sh, 'LINES', {"pos": verts, "color": cols})
    sh.bind()
    sh.uniform_float("viewportSize", vp)
    sh.uniform_float("lineWidth", width)
    batch.draw(sh)


def _fan_into(verts, cols, idx, ring, color):
    """Append a convex ring to the SHARED fill batch as a centre fan.

    One code path for every filled shape the button loop draws - round groups,
    rounded buttons, slider tracks and the clipped value fill - so they all
    land in the same TRIS batch instead of one batch per rect (see the loop)."""
    n = len(ring)
    if n < 3:
        return
    base = len(verts)
    if n == 4:
        # a plain rect (roundness off, or a button too small to round): two
        # triangles beat a centre fan by a vertex and two triangles, and this
        # is the COMMON case at low zoom - the ring is convex, so the diagonal
        # split is safe (v0.24.1, perf)
        verts += ring
        cols += [color] * 4
        idx.append((base, base + 1, base + 2))
        idx.append((base + 2, base + 3, base))
        return
    verts.append((sum(p[0] for p in ring) / n, sum(p[1] for p in ring) / n))
    cols.append(color)
    verts += ring
    cols += [color] * n
    for k in range(n):
        idx.append((base, base + 1 + k, base + 1 + ((k + 1) % n)))


def _ring_into(verts, cols, ring, color):
    """Append a closed ring to the shared outline batch, as LINE PAIRS.

    Pairs, never a strip: 'LINE_LOOP' is gone from the modern GPU module and a
    strip silently drops the closing edge (gotcha 8)."""
    n = len(ring)
    for k in range(n):
        verts += [ring[k], ring[(k + 1) % n]]
        cols += [color] * 2


def _draw_rect(x0, y0, x1, y1, color, filled=True, width=1.5, vp=(1.0, 1.0)):
    if filled:
        verts = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
        sh = _shader('UNIFORM_COLOR')
        batch = batch_for_shader(sh, 'TRIS', {"pos": verts},
                                 indices=[(0, 1, 2), (2, 3, 0)])
        sh.bind()
        sh.uniform_float("color", color)
        batch.draw(sh)
        return
    # NOT 'LINE_LOOP': that primitive is gone from the modern GPU module and
    # degrades to an open strip, which silently drops the closing (left) edge -
    # the old "left grip / box-select is missing a line" bug.
    _draw_poly_lines([(x0, y0), (x1, y0), (x1, y0), (x1, y1),
                      (x1, y1), (x0, y1), (x0, y1), (x0, y0)], color, width, vp)


def _draw_round_rect(x0, y0, x1, y1, r, color, filled=True, width=1.5,
                     vp=(1.0, 1.0)):
    """One rounded rect on its own batch - for the overlays there is exactly
    ONE of per frame (the place ghost, the hover outline). The button loop
    batches instead: never call this per button."""
    ring = _round_rect_pts(x0, y0, x1, y1, r)
    n = len(ring)
    if filled:
        cx = sum(p[0] for p in ring) / n
        cy = sum(p[1] for p in ring) / n
        sh = _shader('UNIFORM_COLOR')
        batch = batch_for_shader(
            sh, 'TRIS', {"pos": [(cx, cy)] + ring},
            indices=[(0, 1 + k, 1 + ((k + 1) % n)) for k in range(n)])
        sh.bind()
        sh.uniform_float("color", color)
        batch.draw(sh)
        return
    verts = []
    for k in range(n):
        verts += [ring[k], ring[(k + 1) % n]]
    _draw_poly_lines(verts, color, width, vp)


def _draw_arc(cx, cy, rx, ry, a0, a1, color, width, vp):
    """Grip hint for a round button: a slice of its ring, so the highlight sits
    where the button actually is (a rect band would float off the circle)."""
    span = abs(a1 - a0)
    segs = max(4, int(_circle_segs(max(rx, ry)) * span / (2.0 * math.pi)) + 1)
    pts = [(cx + math.cos(a0 + (a1 - a0) * k / segs) * rx,
            cy + math.sin(a0 + (a1 - a0) * k / segs) * ry)
           for k in range(segs + 1)]
    verts = []
    for k in range(segs):
        verts += [pts[k], pts[k + 1]]
    _draw_poly_lines(verts, color, width, vp)


class _GhostButton:
    """Stand-in for the button a queued click is about to create, so the preview
    is sized by the REAL `_new_button_shape` / `_btn_wh` path instead of a guess
    that would drift from it. Duck-typed on purpose - those helpers only ever
    read attributes."""

    def __init__(self, bone, w, h, scale, blank=False, kind='BONE'):
        self.kind = kind
        self.bone = bone
        self.label = _bone_label(bone)
        self.w = w
        self.h = h
        self.scale = scale
        # carried, or the preview would show a name the real button won't have
        self.blank = blank


def _draw_place_ghost(context, region, arm, sx, sy, vp, font_id):
    """Preview of the next queued button at the cursor + a count, so an armed
    queue is impossible to mistake for an idle picker. A queued GROUP draws as
    an amber ring instead of a rect."""
    queue = _state["place"]
    spec = _state["place_group"]
    squeue = _state["place_sliders"]
    mouse = _state["place_mouse"]
    if ((not queue and not spec and not squeue) or not mouse
            or _state["place_region"] != region.as_pointer()):
        return
    mx, my = mouse
    if squeue:
        # a slider ghost: the REAL _new_slider_shape / _btn_wh path, blank so
        # the duck-typed ghost never reaches the sk_* lookups in _btn_text
        w, h = _new_slider_shape(context, region)
        ghost = _GhostButton("", w, h, context.scene.madi_picker_scale,
                             blank=True, kind='SLIDER')
        gw, gh = _btn_wh(ghost)
        hw, hh = gw * sx * 0.5, gh * sy * 0.5
        gr = _round_radius(hw * 2.0, hh * 2.0)
        _draw_round_rect(mx - hw, my - hh, mx + hw, my + hh, gr,
                         (1.0, 0.72, 0.15, 0.25), True)
        _draw_round_rect(mx - hw, my - hh, mx + hw, my + hh, gr,
                         (1.0, 0.78, 0.2, 0.95), False, 2.0, vp)
        text = squeue[0][1] if len(squeue) == 1 else "%s  (+%d more)" % (
            squeue[0][1], len(squeue) - 1)
    elif spec:
        # round means round in px: w = h * (sy/sx), same as _create_group
        aspect = (sy / sx) if sx else 1.0
        ghost = _GhostButton("", spec["h"] * aspect, spec["h"], spec["scale"],
                             blank=True, kind='GROUP')
        gw, gh = _btn_wh(ghost)
        hw, hh = gw * sx * 0.5, gh * sy * 0.5
        _draw_arc(mx, my, hw, hh, 0.0, 2.0 * math.pi,
                  (1.0, 0.78, 0.2, 0.95), 2.0, vp)
        text = "Group of %d bones" % len(spec["members"])
    else:
        # the SAME rule the real button will be created by, so the preview can't
        # drift from what actually lands
        ghost = _GhostButton(queue[0], *_new_button_shape(context, arm))
        gw, gh = _btn_wh(ghost)
        hw, hh = gw * sx * 0.5, gh * sy * 0.5
        gr = _round_radius(hw * 2.0, hh * 2.0)
        _draw_round_rect(mx - hw, my - hh, mx + hw, my + hh, gr,
                         (1.0, 0.72, 0.15, 0.25), True)
        _draw_round_rect(mx - hw, my - hh, mx + hw, my + hh, gr,
                         (1.0, 0.78, 0.2, 0.95), False, 2.0, vp)
        text = queue[0] if len(queue) == 1 else "%s  (+%d more)" % (
            queue[0], len(queue) - 1)
    # what this click will drop, above the ghost - inside it the text would
    # have to fight for room at small sizes
    blf.size(font_id, 12)
    blf.color(font_id, 1.0, 0.85, 0.35, 1.0)
    tw, _th = blf.dimensions(font_id, text)
    blf.position(font_id, mx - tw * 0.5, my + hh + 6, 0)
    blf.draw(font_id, text)


def _draw_callback():
    # The overlay draws ONLY while the picker runs (job 26): stopping hands the
    # Image Editor back clean. The handler itself stays registered for the whole
    # add-on lifetime purely so a modal crash can't orphan it.
    if not _state["running"]:
        return
    context = bpy.context
    area = context.area
    region = context.region
    if area is None or area.type != 'IMAGE_EDITOR' or region is None:
        return

    arm = _target(context)
    if not arm:
        return

    selected = _selected_bone_names(arm)
    claimed = _claimed_bones(context, arm)
    sx, sy = _view_scale(region)
    # This callback runs once per Image Editor. The buttons belong in all of
    # them; the mouse-driven overlays (hover grip, rubber band) belong only in
    # the one they were started in - see _state["hover_region"].
    rptr = region.as_pointer()
    hover_idx = _state["hover_idx"] if _state["hover_region"] == rptr else -1
    hov_ex, hov_ey = _state["hover_ex"], _state["hover_ey"]

    gpu.state.blend_set('ALPHA')
    vp = (region.width, region.height)   # POLYLINE_* shaders need the viewport

    # dim the editor behind the picker (preferences slider, job 31): one black
    # quad UNDER everything, so a bright reference stops fighting the buttons.
    # Pure overlay - the image datablock is never touched, and since the
    # callback only draws while running, stopping un-dims by itself.
    p = _prefs()
    if p is not None and p.pk_bg_darken > 0.0:
        _draw_rect(0.0, 0.0, region.width, region.height,
                   (0.0, 0.0, 0.0, p.pk_bg_darken / 100.0), True)
    # How solid the buttons are drawn (preferences, v0.20.0). SOLID by default -
    # the old hard-coded 0.85/0.9/0.95 made every button see-through whether you
    # wanted it or not. One number for every state, so a selected button doesn't
    # quietly get more opaque than a resting one: the selection reads through
    # the brighten and the white outline, not through alpha.
    alpha = (p.pk_btn_alpha / 100.0) if p is not None else 1.0
    # ...and the corner roundness, read ONCE and handed to every _round_radius
    # in the loop below (v0.20.1)
    rfrac = (p.pk_btn_round / 100.0) if p is not None else BTN_ROUND

    # only the ACTIVE TAB's buttons are drawn (and hit-tested, and box-selected)
    buttons = _iter_buttons(context, arm)

    # Collect every button into ONE fill batch + ONE outline batch. Allocating a
    # GPU batch per rect per frame is what makes overlays crawl once a real
    # layout has 100+ buttons. (Unmatched bones get a second, thicker LINES
    # batch so their red outline reads clearly - still just one extra batch.)
    fill_verts, fill_cols, fill_idx = [], [], []
    line_verts, line_cols = [], []
    miss_verts, miss_cols = [], []
    # the slider keyframe pips get their OWN batch, drawn after the labels
    # (v0.22.0) - one more batch, and text can never sit on top of them
    pip_verts, pip_cols, pip_idx = [], [], []
    frame = context.scene.frame_current
    visible = []
    for i, btn in buttons:
        x0, y0, x1, y1 = _btn_rect(region, btn, sx, sy)
        # cull anything scrolled off-screen
        if x1 < 0 or y1 < 0 or x0 > region.width or y0 > region.height:
            continue
        state = _btn_sel_state(btn, selected, claimed)
        # ⚠ A SLIDER's key block is resolved ONCE per redraw, here. `_btn_missing`
        # and the SLIDER draw branch below both need it, and each used to look it
        # up for itself - two `bpy.data.objects.get` + shape-key walks per slider
        # per frame to answer the same question twice.
        kb = _slider_key(btn) if btn.kind == 'SLIDER' else None
        missing = (kb is None) if btn.kind == 'SLIDER' else bool(_btn_missing(arm, btn))
        visible.append((i, btn, x0, y0, x1, y1, state))
        r, g, b = btn.color
        if state == 1:
            # brighten the button's own color so custom colors stay readable
            fill = (r + (1.0 - r) * 0.55, g + (1.0 - g) * 0.55,
                    b + (1.0 - b) * 0.55, alpha)
        elif state == 2:
            # COVERED (job 25): a clicked group/twin drives this bone - a
            # half-strength brighten + soft outline says "this is what the
            # pick selects" without reading as the pick itself
            fill = (r + (1.0 - r) * 0.25, g + (1.0 - g) * 0.25,
                    b + (1.0 - b) * 0.25, alpha)
        else:
            fill = (r, g, b, alpha)

        if missing:
            outline = MISSING_COLOR
            ov, oc = miss_verts, miss_cols
        elif state:
            outline = ((1.0, 1.0, 1.0, 0.95) if state == 1 else
                       (1.0, 1.0, 1.0, 0.55))
            ov, oc = line_verts, line_cols
        else:
            # NO BLACK OUTLINES (Marty, v0.19.4): a resting button is its fill,
            # nothing else. Outlines are now reserved for meaning - white =
            # picked/covered, red = unmatched - so an edge on screen always says
            # something. `ov is None` is the "draw no outline" flag below.
            outline = None
            ov = oc = None

        if btn.kind == 'GROUP':
            cx, cy = (x0 + x1) * 0.5, (y0 + y1) * 0.5
            rx, ry = (x1 - x0) * 0.5, (y1 - y0) * 0.5
            segs = _circle_segs(max(rx, ry))     # follows the DRAWN size
            ring = [(cx + ux * rx, cy + uy * ry) for ux, uy in _circle_pts(segs)]
            _fan_into(fill_verts, fill_cols, fill_idx, ring, fill)
            if ov is not None:
                _ring_into(ov, oc, ring, outline)
        elif btn.kind == 'SLIDER':
            # a track in the button's colour dimmed down, the VALUE as a fill
            # in the colour itself (state-brightened like any button, so a
            # box-selected slider lights up) - both in the same batch, the
            # value second so it lands on top
            frac = _slider_frac(kb) if kb else 0.0   # `kb` resolved once, above
            track = (r * SLIDER_TRACK, g * SLIDER_TRACK, b * SLIDER_TRACK,
                     alpha)
            rad = _round_radius(x1 - x0, y1 - y0, rfrac)
            ring = _round_rect_pts(x0, y0, x1, y1, rad)
            _fan_into(fill_verts, fill_cols, fill_idx, ring, track)
            xv = x0 + (x1 - x0) * frac
            if xv > x0 + 0.5:                 # sub-pixel fill: draw nothing
                # the value is the TRACK's own shape cut at xv, so the fill
                # wears the rounded left end and can never poke out of it
                _fan_into(fill_verts, fill_cols, fill_idx,
                          _clip_ring_x(ring, xv), fill)
            # keyframe pip at the right end: amber = keyed on THIS frame, green
            # = animated but not here. Scrub and it flips, so the slider itself
            # tells you which frames carry a key (v0.22.0, Marty's ask).
            kstate = _slider_key_state(kb, frame)
            if kstate:
                s = max(2.0, min(x1 - x0, y1 - y0) * 0.28)
                px = x1 - s - max(1.5, rad * 0.6)
                py = (y0 + y1) * 0.5
                _fan_into(pip_verts, pip_cols, pip_idx,
                          [(px + s, py), (px, py + s),
                           (px - s, py), (px, py - s)],
                          KEY_ON_FRAME_COLOR if kstate == 2
                          else KEY_ANIMATED_COLOR)
            if ov is not None:
                _ring_into(ov, oc, ring, outline)
        else:
            ring = _round_rect_pts(x0, y0, x1, y1,
                                   _round_radius(x1 - x0, y1 - y0, rfrac))
            _fan_into(fill_verts, fill_cols, fill_idx, ring, fill)
            if ov is not None:
                _ring_into(ov, oc, ring, outline)

    if fill_verts:
        sh = _shader('SMOOTH_COLOR')
        batch = batch_for_shader(sh, 'TRIS',
                                 {"pos": fill_verts, "color": fill_cols},
                                 indices=fill_idx)
        sh.bind()
        batch.draw(sh)
    _draw_poly_lines_vc(line_verts, line_cols, 1.5, vp)
    _draw_poly_lines_vc(miss_verts, miss_cols, 2.5, vp)

    # Ctrl+hover feedback, for the ONE hovered button. On an edge/corner: the
    # grip for the side the drag will move. Anywhere else inside: highlight the
    # whole button, so you can see what a Ctrl+drag is about to move (a round
    # button has no nearby edge band to hint at it, so without this it looked
    # inert). Nothing hovered = no scan at all (v0.24.1, perf).
    for i, btn, x0, y0, x1, y1, _st in (
            () if hover_idx < 0 else
            [v for v in visible if v[0] == hover_idx][:1]):
        m = _edge_margin((x1 - x0) * 0.5, (y1 - y0) * 0.5)
        grip = (0.25, 0.85, 1.0, 1.0)
        cx, cy = (x0 + x1) * 0.5, (y0 + y1) * 0.5
        rx, ry = (x1 - x0) * 0.5, (y1 - y0) * 0.5
        rr = _round_radius(x1 - x0, y1 - y0, rfrac)
        if hov_ex or hov_ey:
            if btn.kind == 'GROUP':
                a = math.atan2(hov_ey, hov_ex)
                span = math.radians(40.0)
                _draw_arc(cx, cy, rx, ry, a - span, a + span, grip, 2.5, vp)
            else:
                # the band stops short of the corner arcs, or a rounded button
                # would wear a grip hint with its ends hanging in mid-air
                if hov_ex < 0:
                    _draw_rect(x0, y0 + rr, x0 + m, y1 - rr, grip, False,
                               2.5, vp)
                elif hov_ex > 0:
                    _draw_rect(x1 - m, y0 + rr, x1, y1 - rr, grip, False,
                               2.5, vp)
                if hov_ey < 0:
                    _draw_rect(x0 + rr, y0, x1 - rr, y0 + m, grip, False,
                               2.5, vp)
                elif hov_ey > 0:
                    _draw_rect(x0 + rr, y1 - m, x1 - rr, y1, grip, False,
                               2.5, vp)
        else:
            # move-ready: outline the button itself
            move = (0.25, 0.85, 1.0, 0.9)
            if btn.kind == 'GROUP':
                _draw_arc(cx, cy, rx, ry, 0.0, 2.0 * math.pi, move, 2.0, vp)
            else:
                _draw_round_rect(x0, y0, x1, y1, rr, move, False, 2.0, vp)

    # box-select rubber band, in the region it's being dragged in only
    box = _state["box"] if _state["box_region"] == rptr else None
    if box is not None:
        bx0, by0, bx1, by1 = box
        _draw_rect(bx0, by0, bx1, by1, (0.3, 0.6, 1.0, 0.15), filled=True)
        _draw_rect(bx0, by0, bx1, by1, (0.4, 0.7, 1.0, 0.9), False, 1.5, vp)

    # Labels (drawn last, on top). EVERY LABEL IS FITTED TO ITS OWN BUTTON
    # (v0.13.0, reverting v0.12.0's one-size-per-tab on Marty's call): scale a
    # button up and its writing scales with it. The fit is computed on the px
    # rect that was just drawn, so no aspect maths is needed here - px are
    # physical on both axes.
    font_id = 0
    # measure FIRST, draw after: _label_fit reaches _label_width_ratio and
    # _cap_ratio, and both set blf's global size to _REF_SIZE on a cache miss -
    # interleaving a miss between blf.size() and blf.draw() would draw one
    # label at the wrong size.
    labels = []
    for i, btn, x0, y0, x1, y1, state in visible:
        text = _btn_text(btn)           # "" while the button is blank
        if not text:
            continue
        label_px = _label_fit(btn, x1 - x0, y1 - y0)
        # per-button skip (see MIN_LABEL_PX): only the labels that would be
        # mush vanish, the rest of the tab keeps its text
        if label_px >= MIN_LABEL_PX:
            labels.append((btn, x0, y0, x1, y1, state, text, label_px))
    cap_r = _cap_ratio()
    for btn, x0, y0, x1, y1, state, text, label_px in labels:
        # ⚠ blf never sees the fitted size raw: capped and laddered first, or
        # a deep zoom rasterises glyphs the size of the screen - this exact
        # line is what crashed Blender (see MAX_LABEL_RASTER_PX).
        draw_px, tscale = _label_draw_size(label_px)
        blf.size(font_id, draw_px)
        tw, _th = blf.dimensions(font_id, text)
        r, g, b = btn.color
        # pick black/white text from the brightness actually drawn
        lum = 0.299 * r + 0.587 * g + 0.114 * b
        if state:
            lum = lum + (1.0 - lum) * (0.55 if state == 1 else 0.25)
        if lum > 0.5:
            blf.color(font_id, 0.05, 0.05, 0.05, 1.0)
        else:
            blf.color(font_id, 0.95, 0.95, 0.95, 1.0)
        # centre on CAP height, not on each string's own measured height, or
        # "hip" and "Bpqjg" would sit on different baselines
        if tscale > 1.0:
            # over the ceiling: grow the capped rasterisation on the GPU. The
            # matrix scales blf's quads AND its positioning, so the centring
            # is the ordinary maths, done around the button's centre.
            gpu.matrix.push()
            try:
                gpu.matrix.translate(((x0 + x1) * 0.5, (y0 + y1) * 0.5))
                gpu.matrix.scale((tscale, tscale))
                blf.position(font_id, -tw * 0.5, -cap_r * draw_px * 0.5, 0)
                blf.draw(font_id, text)
            finally:
                gpu.matrix.pop()
        else:
            blf.position(font_id, (x0 + x1) * 0.5 - tw * 0.5,
                         (y0 + y1) * 0.5 - cap_r * draw_px * 0.5, 0)
            blf.draw(font_id, text)

    # keyframe pips LAST of the button geometry: a long label must never cover
    # the one mark that says "this frame is keyed"
    if pip_verts:
        sh = _shader('SMOOTH_COLOR')
        batch = batch_for_shader(sh, 'TRIS',
                                 {"pos": pip_verts, "color": pip_cols},
                                 indices=pip_idx)
        sh.bind()
        batch.draw(sh)

    _draw_place_ghost(context, region, arm, sx, sy, vp, font_id)

    gpu.state.blend_set('NONE')

    # HUD hint
    if _state["running"]:
        blf.size(font_id, 12)
        if _place_armed():
            # the armed state owns the HUD line: it changes what a click DOES, so
            # the normal cheat-sheet would be actively misleading here
            blf.color(font_id, 1.0, 0.85, 0.35, 0.95)
            blf.position(font_id, 8, 8, 0)
            if _state["place_group"]:
                blf.draw(font_id, "PLACING a group button - click to drop it | "
                                  "right-click / Esc / leave the canvas to "
                                  "cancel")
            elif _state["place_sliders"]:
                blf.draw(font_id, "PLACING %d slider(s) - click to drop \"%s\" |"
                                  " right-click / Esc / leave the canvas to "
                                  "cancel"
                                  % (len(_state["place_sliders"]),
                                     _state["place_sliders"][0][1]))
            else:
                blf.draw(font_id, "PLACING %d button(s) - click to drop \"%s\" |"
                                  " right-click / Esc / leave the canvas to "
                                  "cancel"
                                  % (len(_state["place"]), _state["place"][0]))
        else:
            blf.color(font_id, 1.0, 1.0, 1.0, 0.6)
            blf.position(font_id, 8, 8, 0)
            blf.draw(font_id, "MADI Picker - click=select | drag=box | "
                              "Ctrl+click=add | Ctrl+drag=move | "
                              "Ctrl+drag edge=resize | Ctrl+G=group here | "
                              "Del=delete | Esc=stop")


# ---------------------------------------------------------------------------
# Draw handler lifecycle
# ---------------------------------------------------------------------------
# ⚠ WHERE THE DRAW HANDLE LIVES, AND WHY IT IS NOT IN `_state`.
#
# `_state` is a module-level dict, so a dev reload (which purges sys.modules)
# replaces it with a fresh one and the previous handle is simply LOST. Unlike
# frame handlers there is no list to sweep - Blender exposes no way to enumerate
# registered draw handlers - so a lost handle can never be removed, and the old
# callback keeps painting over every Image Editor for the rest of the session,
# once per frame, from a dead module.
#
# `bpy.app.driver_namespace` is owned by Blender rather than by this module, so
# it is the one place a handle survives a reload and can be found afterwards.
_DRAW_KEY = "_madi_picker_draw_handle"


def _enable_draw():
    ns = bpy.app.driver_namespace
    # A handle already parked here belongs to a PREVIOUS load of this module.
    # Remove it before adding ours, or the editors carry two callbacks.
    stale = ns.get(_DRAW_KEY)
    if stale is not None:
        try:
            bpy.types.SpaceImageEditor.draw_handler_remove(stale, 'WINDOW')
        except (ValueError, TypeError):
            pass                    # already gone; nothing to do
        ns.pop(_DRAW_KEY, None)
        _state["handle"] = None
    if _state["handle"] is None:
        _state["handle"] = bpy.types.SpaceImageEditor.draw_handler_add(
            _draw_callback, (), 'WINDOW', 'POST_PIXEL')
        ns[_DRAW_KEY] = _state["handle"]


def _disable_draw():
    ns = bpy.app.driver_namespace
    handle = _state["handle"] or ns.get(_DRAW_KEY)
    if handle is not None:
        try:
            bpy.types.SpaceImageEditor.draw_handler_remove(handle, 'WINDOW')
        except (ValueError, TypeError):
            pass
    _state["handle"] = None
    ns.pop(_DRAW_KEY, None)


def _tag_redraw(context):
    # reached from property update callbacks, which can fire while a file loads
    # or under a context override - neither is guaranteed to have a screen
    screen = getattr(context, "screen", None)
    if screen is None:
        return
    for area in screen.areas:
        if area.type == 'IMAGE_EDITOR':
            area.tag_redraw()


# The one thing outside this module needs: the extension's preferences class
# owns the picker's three appearance settings (see _prefs), and their update
# callbacks live in __init__.py, which has no business reaching for a private.
tag_redraw = _tag_redraw


# ---------------------------------------------------------------------------
# Picker session (persistent modal)
# ---------------------------------------------------------------------------
class MADI_OT_picker_session(Operator):
    bl_idname = "madi_picker.session"
    bl_label = "Start Picker"
    bl_description = ("Run the picker: click buttons to select bones, drag to "
                      "box-select, Ctrl to edit the layout. Esc to stop")

    _mode = 'IDLE'            # IDLE / BOX / DRAG / SCALE
    # [(button, orig_x, orig_y, orig_w, orig_h)] - the w/h here are the DRAWN
    # sizes (global scale + label floor applied), because that's what the user
    # is dragging the edge of.
    # These ARE live collection-element references (gotcha 18), and they're only
    # safe because DRAG and SCALE consume every event they don't handle: nothing
    # can add or remove a button mid-drag. Return PASS_THROUGH from either mode
    # and this becomes a use-after-free.
    _items = []
    _start_canvas = (0.0, 0.0)
    _edge = (0, 0)            # which side was grabbed (SCALE)
    _anchor = (0.0, 0.0)      # canvas point held fixed while scaling
    _start_dist = (0.0, 0.0)  # anchor->cursor distance per axis at grab
    _box_start = (0, 0)       # region-local px
    _box_region = None        # region the box-select started in
    # SLIDE (v0.19.0): the slider being scrubbed, by REAL collection index, and
    # its track frozen in the grab region's px. Ints and floats only - the key
    # block is re-resolved by name every event, and the release can land
    # outside the editor (gotcha 4), so the grab region's x is kept too.
    _slide_idx = -1
    _slide_span = (0.0, 1.0)
    _slide_rgn_x = 0

    def invoke(self, context, event):
        if _state["running"]:
            self.report({'WARNING'}, "Picker already running")
            return {'CANCELLED'}
        tab = _ensure_tabs(context)
        # what the editors show now comes back when the picker stops (job 26)
        _snapshot_session_images(context)
        if tab:
            _show_image(context, tab.image)
        _enable_draw()
        _state["running"] = True
        _state["hover_idx"] = -1
        self._mode = 'IDLE'
        self._drop_grab()
        context.window_manager.modal_handler_add(self)
        _tag_redraw(context)
        return {'RUNNING_MODAL'}

    def _drop_grab(self):
        """Let go of everything a DRAG/SCALE was holding.

        `_items` carries live collection-element references, which are only safe
        while the grab consumes every event (gotcha 18); dropping them the
        moment the mouse comes up keeps that window as small as it can be."""
        self._items = []

    def _finish(self, context):
        self._drop_grab()
        _state["running"] = False
        _state["box"] = None
        _state["box_region"] = 0
        _state["hover_idx"] = -1
        _state["hover_region"] = 0
        _clear_place()            # a queue can't outlive the session that armed it
        # hand the editors back (no-op if the Stop button already did - job 26).
        # The draw handler stays registered (crash-safety); the callback itself
        # early-outs while not running, so the overlay is gone either way.
        _restore_session_images(context)
        _tag_redraw(context)

    def modal(self, context, event):
        # never let an exception kill the modal without cleaning up, or the
        # picker gets stuck "already running" and input dies.
        try:
            return self._modal_impl(context, event)
        except Exception as e:
            import traceback
            traceback.print_exc()
            self.report({'ERROR'}, "Picker error: %s" % e)
            self._finish(context)
            return {'CANCELLED'}

    def _modal_impl(self, context, event):
        if not _state["running"]:
            self._finish(context)
            return {'CANCELLED'}

        if event.type == 'ESC' and event.value == 'PRESS':
            # Esc backs out of click-to-place FIRST: ending the whole session
            # because you changed your mind about one button would be brutal
            if _place_armed():
                self._place_cancel(context, "Esc")
                return {'RUNNING_MODAL'}
            self._finish(context)
            return {'CANCELLED'}

        area, region = _area_region_under_mouse(context, event.mouse_x, event.mouse_y)
        over_picker = region is not None
        lx = event.mouse_x - region.x if over_picker else 0
        ly = event.mouse_y - region.y if over_picker else 0

        # ---- PLACE: one click drops one queued button (or the armed group) ----
        if _place_armed():
            if event.type == 'MOUSEMOVE':
                if not over_picker:
                    # leaving the canvas cancels: an armed queue that survived
                    # would hijack the user's next click somewhere else entirely
                    self._place_cancel(context, "left the canvas")
                    return {'PASS_THROUGH'}
                _state["place_mouse"] = (lx, ly)
                _state["place_region"] = region.as_pointer()
                _tag_redraw(context)              # the ghost follows the cursor
                return {'PASS_THROUGH'}
            if event.type == 'RIGHTMOUSE' and event.value == 'PRESS':
                self._place_cancel(context, "right-click")
                return {'RUNNING_MODAL'}
            if (event.type == 'LEFTMOUSE' and event.value == 'PRESS'
                    and over_picker):
                if _state["place_group"]:
                    self._place_group(context, region, lx, ly)
                elif _state["place_sliders"]:
                    self._place_next_slider(context, region, lx, ly)
                else:
                    self._place_next(context, region, lx, ly)
                return {'RUNNING_MODAL'}
            # anything else (pan, zoom, tab switch...) still works while armed
            return {'PASS_THROUGH'}

        # ---- SLIDE: a click-drag on a SLIDER scrubs its shape key ----------
        # The rect is frozen at the grab (the button can't move and the view
        # can't pan - every event is consumed), and the key is re-resolved by
        # NAME each event: nothing held across events but ints and strings.
        if self._mode == 'SLIDE':
            if event.type == 'MOUSEMOVE':
                self._slide_apply(context, event)
                return {'RUNNING_MODAL'}
            if event.type == 'LEFTMOUSE' and event.value == 'RELEASE':
                self._slide_apply(context, event)
                self._slide_finish(context)
                return {'RUNNING_MODAL'}
            return {'RUNNING_MODAL'}

        # ---- DRAG: move one or many buttons ----
        if self._mode == 'DRAG':
            if event.type == 'MOUSEMOVE' and over_picker and self._items:
                cx, cy = _px_to_canvas(region, lx, ly)
                # ⚠ NO WALLS since 2026-08-10. Buttons used to be SOLID (job 16,
                # 2026-07-30: "buttons must never end up inside one another") and
                # Marty reversed it - "they should not collide with each other".
                # So a drag is now the plain delta from the grab point. The whole
                # per-event STEP dance existed only to let a clamped button slide
                # along a wall; with nothing to slide against, the absolute delta
                # is both simpler and exact - it can never drift from the cursor.
                dx = cx - self._start_canvas[0]
                dy = cy - self._start_canvas[1]
                for btn, ox, oy, _ow, _oh in self._items:
                    btn.x = ox + dx
                    btn.y = oy + dy
                _tag_redraw(context)
                return {'RUNNING_MODAL'}
            if event.type == 'LEFTMOUSE' and event.value == 'RELEASE':
                self._mode = 'IDLE'
                self._drop_grab()
                return {'RUNNING_MODAL'}
            return {'RUNNING_MODAL'}

        # ---- SCALE: resize ONLY the grabbed side(s) ----
        if self._mode == 'SCALE':
            if event.type == 'MOUSEMOVE' and over_picker and self._items:
                cx, cy = _px_to_canvas(region, lx, ly)
                ex, ey = self._edge
                ax, ay = self._anchor
                # each axis gets its OWN factor, so grabbing one side changes
                # only that dimension (a corner changes both, independently)
                fx = fy = None
                if ex and self._start_dist[0] > 1e-6:
                    fx = max(0.05, min(20.0, abs(cx - ax) / self._start_dist[0]))
                if ey and self._start_dist[1] > 1e-6:
                    fy = max(0.05, min(20.0, abs(cy - ay) / self._start_dist[1]))
                # ⚠ No wall clamp here either (2026-08-10) - an edge-drag used
                # to stop dead against a neighbour. A resize was never a rigid
                # translation, so what got clamped was the FACTOR by the worst
                # case across the selection; with the walls gone the factor is
                # the mouse's, and only the property's own min/max survives.
                for btn, ox, oy, ow, oh in self._items:
                    scale = max(1e-4, btn.scale)
                    if fx is not None:
                        # ow is the DRAWN width, so divide this button's own
                        # scale back out before storing, then re-derive what will
                        # actually be drawn - otherwise the pinned edge drifts
                        # whenever a clamp bites. (Since v0.11.0 the only clamp
                        # left is the property's own min/max: the label no longer
                        # sets a floor, it just gets smaller with the button.)
                        btn.w = max(0.002, min(5.0, ow * fx / scale))
                        nw = btn.w * scale
                        # pin this button's own opposite side
                        btn.x = (ox - ex * ow * 0.5) + ex * nw * 0.5
                    if fy is not None:
                        btn.h = max(0.002, min(5.0, oh * fy / scale))
                        nh = btn.h * scale
                        btn.y = (oy - ey * oh * 0.5) + ey * nh * 0.5
                _tag_redraw(context)
                return {'RUNNING_MODAL'}
            if event.type == 'LEFTMOUSE' and event.value == 'RELEASE':
                self._mode = 'IDLE'
                self._drop_grab()
                return {'RUNNING_MODAL'}
            return {'RUNNING_MODAL'}

        # ---- BOX: rubber-band select ----
        if self._mode == 'BOX':
            if event.type == 'MOUSEMOVE' and over_picker:
                _state["box"] = (self._box_start[0], self._box_start[1], lx, ly)
                _tag_redraw(context)
                return {'RUNNING_MODAL'}
            if event.type == 'LEFTMOUSE' and event.value == 'RELEASE':
                # release can land outside the editor -> region is None; fall
                # back to the region the box started in.
                self._apply_box(context, self._box_region or region, event.shift)
                _sync_brushes(context)      # show the first button in the band
                _state["box"] = None
                _state["box_region"] = 0
                self._box_region = None
                self._mode = 'IDLE'
                _tag_redraw(context)
                return {'RUNNING_MODAL'}
            return {'RUNNING_MODAL'}

        # ---- IDLE ----
        # track the Ctrl+hover resize grip (Ctrl alone, tapped without moving
        # the mouse, must refresh it too)
        if event.type in {'MOUSEMOVE', 'LEFT_CTRL', 'RIGHT_CTRL'}:
            idx, ex, ey = -1, 0, 0
            if over_picker and event.ctrl:
                _btn, idx, ex, ey = _hit_button(context, region, lx, ly)
            # the grip belongs to the editor the mouse is in: the same button is
            # drawn in every Image Editor, so without this the cyan outline lights
            # up in all of them at once
            rptr = region.as_pointer() if idx >= 0 else 0
            if (idx != _state["hover_idx"] or ex != _state["hover_ex"]
                    or ey != _state["hover_ey"]
                    or rptr != _state["hover_region"]):
                _state["hover_idx"] = idx
                _state["hover_ex"] = ex
                _state["hover_ey"] = ey
                _state["hover_region"] = rptr
                _tag_redraw(context)
            return {'PASS_THROUGH'}

        # delete: selected buttons, else the one under the cursor. Goes through
        # the operator so the rule lives in ONE place (the menu's Delete is the
        # other caller) and so it lands on the undo stack.
        if event.type in {'DEL', 'X'} and event.value == 'PRESS' and over_picker:
            _btn, idx, _ex, _ey = _hit_button(context, region, lx, ly)
            _state["cursor_idx"] = idx
            bpy.ops.madi_picker.delete()
            return {'RUNNING_MODAL'}

        # group up the current selection, dropping the handle AT THE MOUSE
        if (event.type == 'G' and event.value == 'PRESS' and event.ctrl
                and over_picker):
            gx, gy = _px_to_canvas(region, lx, ly)
            bpy.ops.madi_picker.make_group(use_cursor=True, cx=gx, cy=gy)
            return {'RUNNING_MODAL'}

        # keyframe the current bone selection: whatever key the user's own Pose
        # keymap binds to insert-keyframe works over the canvas too (v0.18.0).
        # Keyboard only - the picker's pointer gestures outrank a mouse binding.
        # Comes AFTER Del/X and Ctrl+G so the picker's own keys keep winning.
        # Over a SLIDER (v0.19.0) the key keys THAT shape key instead of the
        # bones - point at the thing you mean, like everywhere in Blender.
        if (over_picker and event.value == 'PRESS'
                and 'MOUSE' not in event.type
                and not event.type.startswith(('TIMER', 'NDOF'))):
            kmi = _kf_match(context, event)
            if kmi is not None:
                hbtn, _hi, _hx, _hy = _hit_button(context, region, lx, ly)
                if hbtn is not None and hbtn.kind == 'SLIDER':
                    err = _kf_insert_slider(hbtn)
                    if not err:
                        self.report({'INFO'}, "Keyed %s"
                                    % (hbtn.label or hbtn.sk_key))
                else:
                    err = _kf_insert(context, kmi)
                if err:
                    self.report({'WARNING'}, "Keyframe: %s" % err)
                # consume it either way, or the Image Editor acts on the key
                # (Alt+S there is image.save)
                return {'RUNNING_MODAL'}

        # right-click: context menu AT THE MOUSE, acting on the current selection.
        # It does not select anything itself - Blender's own right-click never
        # does either, and the whole point is to act on what you just selected.
        # The popup pushes its handler in FRONT of this modal, so it gets the
        # clicks even though the mouse is still geometrically over the canvas.
        if event.type == 'RIGHTMOUSE' and event.value == 'PRESS' and over_picker:
            # the menu has no cursor of its own: resolve what's under this one
            # ONCE, here, and let the items read it out of _state
            _btn, idx, _ex, _ey = _hit_button(context, region, lx, ly)
            _state["cursor_idx"] = idx
            bpy.ops.wm.call_menu(name=MADI_MT_picker_context.bl_idname)
            return {'RUNNING_MODAL'}

        if event.type == 'LEFTMOUSE' and event.value == 'PRESS' and over_picker:
            btn, _idx, ex, ey = _hit_button(context, region, lx, ly)
            # Alt+click a SLIDER keys its shape key at the current frame;
            # Alt+Shift+click takes that key back off (v0.22.0, Marty's pick -
            # keying with no keyboard at all). Ctrl stays the layout modifier,
            # so Ctrl+Alt is left alone; on anything but a slider Alt falls
            # through to the normal gestures.
            if (event.alt and not event.ctrl and btn is not None
                    and btn.kind == 'SLIDER'):
                what = btn.label or btn.sk_key or "slider"
                err = (_kf_remove_slider(btn) if event.shift
                       else _kf_insert_slider(btn))
                if err:
                    self.report({'WARNING'}, "%s: %s" % (what, err))
                else:
                    self.report({'INFO'}, "%s %s at frame %d"
                                % (what, "un-keyed" if event.shift else "keyed",
                                   context.scene.frame_current))
                _tag_redraw(context)
                return {'RUNNING_MODAL'}
            if event.ctrl:
                if btn:
                    if btn.kind == 'GROUP':
                        # a group handle is grabbed ALONE, never as part of the
                        # selection: Ctrl+dragging the circle must not drag every
                        # member button along with it
                        group = [btn]
                    else:
                        group = _selected_buttons(context)
                        # grabbing a selected button affects the whole selection,
                        # otherwise just the one under the cursor
                        if btn not in group:
                            group = [btn]
                    # cache the DRAWN sizes: the edge being dragged is the drawn
                    # one, so scaling and the pinned edge must both work in it
                    eff = {b.as_pointer(): _btn_wh(b) for b in group}
                    self._items = [(b, b.x, b.y, *eff[b.as_pointer()])
                                   for b in group]
                    self._start_canvas = _px_to_canvas(region, lx, ly)
                    if ex or ey:
                        # scale from the grabbed side: anchor the OPPOSITE side
                        self._mode = 'SCALE'
                        self._edge = (ex, ey)
                        bw, bh = eff[btn.as_pointer()]
                        ax = btn.x - ex * bw * 0.5 if ex else btn.x
                        ay = btn.y - ey * bh * 0.5 if ey else btn.y
                        self._anchor = (ax, ay)
                        mx, my = self._start_canvas
                        self._start_dist = (abs(mx - ax), abs(my - ay))
                    else:
                        self._mode = 'DRAG'
                else:
                    self._add_button(context, region, lx, ly)
                    _tag_redraw(context)
                return {'RUNNING_MODAL'}
            # normal picking
            if btn:
                if btn.kind == 'SLIDER':
                    # a slider is SCRUBBED, not selected: the click grabs the
                    # value (and jumps it to the cursor), box-select is how a
                    # slider joins a layout selection
                    if _slider_key(btn) is None:
                        self.report({'WARNING'},
                                    "Slider target missing: %s / %s"
                                    % (btn.sk_object or "?", btn.sk_key or "?"))
                        return {'RUNNING_MODAL'}
                    x0, _y0, x1, _y1 = _btn_rect(region, btn)
                    self._mode = 'SLIDE'
                    self._slide_idx = _idx
                    self._slide_span = (float(x0), float(x1))
                    self._slide_rgn_x = region.x
                    self._slide_apply(context, event)
                    return {'RUNNING_MODAL'}
                # Object Mode click: get the rig into Pose Mode FIRST, or the
                # selection lands on data nothing is looking at (v0.21.0).
                arm = _target(context)
                err = _ensure_pose_mode(context, arm)
                if err:
                    self.report({'WARNING'}, err)
                # the mode switch runs a nested operator (undo push), so the
                # button comes back by INDEX rather than being held across it -
                # the same index-and-re-fetch habit gotcha 18 buys us
                if arm is not None and 0 <= _idx < len(arm.madi_picker.buttons):
                    btn = arm.madi_picker.buttons[_idx]
                _select_bones(arm, _btn_bones(btn), event.shift)
                # THIS button is the pick, so a twin mapped to the same bone stays
                # dark. (_select_bones already dropped the old flags unless this
                # is a shift+click, which adds to them.)
                btn.select = True
                # the sliders must show THIS button's size, not the last one
                # something was dragged onto
                _sync_brushes(context, btn)
                _tag_redraw(context)
                return {'RUNNING_MODAL'}
            # empty: start box select (a click with no drag -> deselect)
            self._mode = 'BOX'
            self._box_start = (lx, ly)
            self._box_region = region
            _state["box"] = (lx, ly, lx, ly)
            # the band is in THIS region's pixels; it must not be drawn in any
            # other Image Editor, where those pixels mean somewhere else entirely
            _state["box_region"] = region.as_pointer()
            return {'RUNNING_MODAL'}

        # everything else flows through so Blender stays usable
        return {'PASS_THROUGH'}

    def _apply_box(self, context, region, extend):
        arm = _target(context)
        if not arm or region is None:
            return
        x0, y0, x1, y1 = _state["box"]
        xmin, xmax = sorted((x0, x1))
        ymin, ymax = sorted((y0, y1))
        if not extend:
            _deselect_all(arm)
        first = None
        sx, sy = _view_scale(region)
        for _i, btn in _iter_buttons(context, arm):
            bx0, by0, bx1, by1 = _btn_rect(region, btn, sx, sy)
            if _btn_in_box(btn, bx0, by0, bx1, by1, xmin, ymin, xmax, ymax):
                # the buttons the band TOUCHES are the picks - a twin it misses
                # stays dark even though the band selected its bone
                btn.select = True
                for name in _btn_bones(btn):
                    pb = arm.pose.bones.get(name)
                    if pb:
                        pb.select = True
                        first = first or name
        if first:
            # a band that caught bones is a selection like any other: same
            # Object -> Pose switch a click gets (v0.21.0). Done AFTER the
            # loop, so no button reference is held across the nested operator.
            err = _ensure_pose_mode(context, arm)
            if err:
                self.report({'WARNING'}, err)
            b = arm.data.bones.get(first)
            if b:
                arm.data.bones.active = b

    def _add_button(self, context, region, lx, ly):
        """Ctrl+click on empty canvas.

        One bone: it lands here, exactly as it always did. SEVERAL selected: the
        first lands here and the rest queue up for a click each, because dropping
        five buttons on one point just means dragging four of them off it."""
        arm, bone_name = _active_bone_and_arm(context)
        if not arm:
            self.report({'WARNING'}, "No active bone - select a bone first")
            return
        tab = _ensure_tabs(context)
        # keep this tab pointed at the rig we're actually mapping
        if tab and tab.armature != arm:
            tab.armature = arm
        names = MADI_OT_picker_add_selected._bones(context, arm) or [bone_name]
        _state["place"] = list(names)
        _state["place_mouse"] = (lx, ly)
        _state["place_region"] = region.as_pointer()
        self._place_next(context, region, lx, ly)

    def _place_next(self, context, region, lx, ly):
        """Drop the button at the head of the queue at this point."""
        arm = _target(context)
        if not arm or not _state["place"]:
            self._place_cancel(context, "no armature")
            return
        btn = _add_tab_button(context, arm, _state["place"].pop(0))
        btn.x, btn.y = _px_to_canvas(region, lx, ly)
        if not _state["place"]:
            _clear_place()
        _tag_redraw(context)

    def _place_next_slider(self, context, region, lx, ly):
        """Drop the slider at the head of the queue at this point (v0.19.0)."""
        arm = _target(context)
        if not arm or not _state["place_sliders"]:
            self._place_cancel(context, "no armature")
            return
        ob_name, key_name = _state["place_sliders"].pop(0)
        btn = _add_slider_button(context, arm, ob_name, key_name)
        btn.x, btn.y = _px_to_canvas(region, lx, ly)
        if not _state["place_sliders"]:
            _clear_place()
        _tag_redraw(context)

    def _place_group(self, context, region, lx, ly):
        """Drop the armed GROUP button at this point (job 20)."""
        arm = _target(context)
        spec = _state["place_group"]
        if not arm or not spec:
            self._place_cancel(context, "no armature")
            return
        x, y = _px_to_canvas(region, lx, ly)
        _create_group(context, arm, spec, x, y, _view_aspect(region))
        _clear_place()
        _tag_redraw(context)

    def _place_cancel(self, context, why):
        n = _clear_place()
        if n:
            self.report({'INFO'}, "%d button(s) not placed (%s)" % (n, why))
        _tag_redraw(context)

    def _slide_btn(self, context):
        """The slider a SLIDE grab is holding - re-fetched by index every event
        (gotcha 18), None the moment anything about it stops adding up."""
        arm = _target(context)
        coll = arm.madi_picker.buttons if arm else None
        i = self._slide_idx
        if coll is None or not (0 <= i < len(coll)):
            return None
        btn = coll[i]
        return btn if btn.kind == 'SLIDER' else None

    def _slide_apply(self, context, event):
        """Value from the cursor's x inside the frozen track. Absolute, not
        relative: the handle jumps to the click like Blender's own sliders
        under click-drag (and AnimSchool's)."""
        btn = self._slide_btn(context)
        kb = _slider_key(btn) if btn else None
        if kb is None:
            self._slide_finish(context, key=False)
            return
        x0, x1 = self._slide_span
        if x1 - x0 < 1e-6:
            return
        f = (event.mouse_x - self._slide_rgn_x - x0) / (x1 - x0)
        _slider_set_frac(kb, f)
        _tag_redraw(context)

    def _slide_finish(self, context, key=True):
        """Drop the grab; auto-key the result if Blender's autokey is on.
        Keying failure is reported, never raised - an exception here would end
        the whole session (see modal())."""
        self._mode = 'IDLE'
        btn, self._slide_idx = (self._slide_btn(context), -1)
        if not key or btn is None:
            return
        try:
            if context.scene.tool_settings.use_keyframe_insert_auto:
                kb = _slider_key(btn)
                if kb is not None:
                    kb.keyframe_insert("value")
        except Exception as e:
            self.report({'WARNING'}, "Auto-key failed: %s" % e)


def _in_region(region, mx, my):
    return (region.x <= mx < region.x + region.width and
            region.y <= my < region.y + region.height)


# A COLLAPSED side region (sidebar / toolbar) is 1x1, so it can't block the
# mouse the way an open one does - but Blender still draws a little arrow tab
# inside the canvas to re-open it. That tab sits in the WINDOW region, so the
# modal used to swallow the click and the sidebar became impossible to reopen
# without stopping the picker. Azone rects aren't exposed to Python, so reserve
# the corner Blender draws them in.
_AZONE_PAD = 12               # px at UI scale 1.0, along the collapsed edge
_AZONE_RUN = 3                # ...and this many pads down from the top


def _azone_pad(context):
    try:
        scale = context.preferences.system.ui_scale
    except AttributeError:
        scale = 1.0
    return max(6, int(round(_AZONE_PAD * scale)))


def _in_hidden_azone(area, window, mx, my, pad):
    """True when (mx, my) is on a collapsed side region's re-open arrow.

    Only LEFT/RIGHT are handled: they're the toolbar and the sidebar, the two
    with a visible tab in the Image Editor. Keeping the reserved rect to one
    corner (pad x 3*pad) rather than the whole edge means a button parked at the
    canvas edge stays clickable."""
    if window is None:
        return False
    top = window.y + window.height
    for region in area.regions:
        if region.width > 1 and region.height > 1:
            continue                      # not collapsed: it blocks normally
        al = region.alignment
        if al == 'LEFT':
            x0, x1 = window.x, window.x + pad
        elif al == 'RIGHT':
            x0, x1 = window.x + window.width - pad, window.x + window.width
        else:
            continue
        if x0 <= mx < x1 and top - _AZONE_RUN * pad <= my < top:
            return True
    return False


def _area_region_under_mouse(context, mx, my):
    """Return (area, region) of the Image Editor WINDOW region under the
    absolute mouse (mx, my), or (None, None).

    NOTE: with region overlap (Blender's default) the WINDOW region spans the
    WHOLE area - the N-panel/toolbar rects sit *inside* it. So a click on the
    sidebar is geometrically "inside WINDOW" too. We must reject those, or the
    modal swallows every panel click and the sidebar goes dead while running.

    The same applies to a COLLAPSED sidebar/toolbar: it's 1x1 and blocks nothing,
    but its re-open arrow is drawn inside the canvas - see _in_hidden_azone.
    """
    pad = _azone_pad(context)
    for area in context.screen.areas:
        if area.type != 'IMAGE_EDITOR':
            continue
        window = None
        blocked = False
        for region in area.regions:
            if region.type == 'WINDOW':
                window = region
            elif region.width > 1 and region.height > 1:
                # any real UI region (UI / TOOLS / HEADER / ...) wins the mouse
                if _in_region(region, mx, my):
                    blocked = True
        if blocked or window is None:
            continue
        if _in_hidden_azone(area, window, mx, my, pad):
            continue          # let the click re-open the panel
        if _in_region(window, mx, my):
            return area, window
    return None, None


# ---------------------------------------------------------------------------
# Operators for the panel
# ---------------------------------------------------------------------------
class MADI_OT_picker_stop(Operator):
    bl_idname = "madi_picker.stop"
    bl_label = "Stop Picker"
    bl_description = "End the running picker session"

    def execute(self, context):
        _state["running"] = False
        # restore the editors HERE, not just in _finish: the modal only reaches
        # _finish on its next event, and the whole point of stopping is getting
        # the Image Editor back right now (job 26)
        _restore_session_images(context)
        _tag_redraw(context)
        return {'FINISHED'}


class MADI_OT_picker_add_active(Operator):
    bl_idname = "madi_picker.add_active"
    bl_label = "Add Button for Active Bone"
    bl_description = "Add a button (near the canvas center) for the active bone"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        arm, bone_name = _active_bone_and_arm(context)
        if not arm:
            self.report({'ERROR'}, "No active bone - select a bone in Pose mode first")
            return {'CANCELLED'}
        tab = _ensure_tabs(context)
        if tab and tab.armature != arm:
            tab.armature = arm
        btn = _add_tab_button(context, arm, bone_name)
        # stagger so multiple adds don't stack exactly
        n = len(_iter_buttons(context, arm))
        btn.x = 0.5 + ((n % 5) - 2) * 0.08
        btn.y = 0.5 + ((n // 5) % 5 - 2) * 0.08
        _tag_redraw(context)
        return {'FINISHED'}


class MADI_OT_picker_remove_button(Operator):
    bl_idname = "madi_picker.remove_button"
    bl_label = "Remove Button"
    bl_description = "Delete the highlighted button in the list"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        arm = _target(context)
        if not arm:
            return {'CANCELLED'}
        data = arm.madi_picker
        i = data.active_index
        if not (0 <= i < len(data.buttons)):
            self.report({'WARNING'}, "No button highlighted")
            return {'CANCELLED'}
        # the list filters by tab but indices stay global, so a stale
        # active_index can point at a button the user can't even see
        if data.buttons[i].tab_uid != _active_uid(context):
            self.report({'WARNING'}, "Highlighted button is on another tab")
            return {'CANCELLED'}
        # shared with Del/X: if this row was the pick, its bones go too, or the
        # twins it was hiding all light up
        _remove_buttons(context, arm, [i])
        data.active_index = max(0, min(i, len(data.buttons) - 1))
        _state["hover_idx"] = -1
        _tag_redraw(context)
        return {'FINISHED'}


def _create_group(context, arm, spec, x, y, aspect):
    """Add the GROUP button described by `spec` ({"members", "h", "scale"} -
    plain values only, gotcha 18) at canvas (x, y). Shared by make_group and
    the PLACE drop, so an armed group can't drift from an instant one."""
    if aspect <= 0.0:
        aspect = 1.0
    data = arm.madi_picker
    # counted before the add, so the new handle isn't counting itself
    n_groups = sum(1 for _i, b in _iter_buttons(context, arm)
                   if b.kind == 'GROUP') + 1
    grp = data.buttons.add()
    grp.kind = 'GROUP'
    grp.tab_uid = _active_uid(context)
    for name in spec["members"]:
        grp.members.add().bone = name
    grp.h = spec["h"]
    # ROUND means equal in PIXELS: w*sx == h*sy, so w = h * (sy/sx). On a
    # square image that's just w = h; on a portrait reference it's what stops
    # the "round" button being a tall ellipse.
    grp.w = spec["h"] * aspect
    grp.x, grp.y = x, y
    grp.color = context.scene.madi_picker_color
    grp.scale = spec["scale"]
    grp.label = "G%d" % n_groups
    grp.blank = context.scene.madi_picker_blank
    # as-placed size for Reset Size. w is re-derived from h0 on reset (a
    # circle is round in PIXELS), so only h0/scale0 really matter here.
    grp.w0, grp.h0, grp.scale0 = grp.w, grp.h, grp.scale
    data.active_index = len(data.buttons) - 1
    return grp


class MADI_OT_picker_make_group(Operator):
    bl_idname = "madi_picker.make_group"
    bl_label = "Group Selected"
    bl_description = ("Make a round GROUP button from the selected buttons - "
                      "clicking it selects every bone in the group. Ctrl+G "
                      "drops it at the mouse; the menu and panel let you place "
                      "it with a click")
    bl_options = {'REGISTER', 'UNDO'}

    # Ctrl+G in the modal passes the cursor through. With `place` the group is
    # ARMED instead (job 20): the next canvas click places it, like a multi-add.
    # SKIP_SAVE so a later press can't reuse a stale position or mode.
    use_cursor: BoolProperty(default=False, options={'SKIP_SAVE', 'HIDDEN'})
    cx: FloatProperty(default=0.5, options={'SKIP_SAVE', 'HIDDEN'})
    cy: FloatProperty(default=0.5, options={'SKIP_SAVE', 'HIDDEN'})
    place: BoolProperty(default=False, options={'SKIP_SAVE', 'HIDDEN'})

    def invoke(self, context, event):
        """Reached when the operator is called 'INVOKE_DEFAULT' (e.g. from a
        keymap) rather than by the modal, which passes cx/cy itself.
        event.mouse_x/y are WINDOW-absolute, so the region has to be resolved
        before they mean anything in canvas space."""
        if not self.use_cursor and not self.place:
            _area, region = _area_region_under_mouse(context, event.mouse_x,
                                                     event.mouse_y)
            if region is not None:
                self.cx, self.cy = _px_to_canvas(region,
                                                 event.mouse_x - region.x,
                                                 event.mouse_y - region.y)
                self.use_cursor = True
        return self.execute(context)

    def execute(self, context):
        arm = _target(context)
        if not arm:
            return {'CANCELLED'}
        members, srcs = [], []
        for btn in _selected_buttons(context, arm):
            if btn.kind != 'BONE':
                continue                  # don't nest groups
            srcs.append(btn)
            if btn.bone and btn.bone not in members:
                members.append(btn.bone)
        if len(members) < 2:
            self.report({'WARNING'},
                        "Select at least 2 buttons (bones) to group")
            return {'CANCELLED'}

        # EVERY number this handle inherits is read off the cluster HERE, before
        # the collection grows. `buttons.add()` reallocates the IDP_IDPARRAY
        # behind it at growth boundaries (n = 5, 9, 17, 26, 36... measured), and
        # an element reference taken before that points into freed memory
        # afterwards: reads come back as garbage, writes segfault (gotcha 18).
        # Plain floats survive the move - `srcs` does not, so it is dropped.
        n = len(srcs)
        spec = {
            "members": members,
            "h": sum(b.h for b in srcs) / n,
            # scale comes from the CLUSTER, not the brush: `h` already does, and
            # a handle drawn at half its members' size looks broken
            "scale": sum(b.scale for b in srcs) / n,
        }
        # no cursor (panel press, not running): sit the handle just above the
        # cluster it controls, so it doesn't bury the buttons it was made from
        mid_x = sum(b.x for b in srcs) / n
        top_y = max(b.y + b.h * 0.5 for b in srcs)
        del srcs                  # stale the moment the collection grows

        if self.place and _state["running"]:
            # job 20: arm the placement - the modal's PLACE branch drops it at
            # the next canvas click, with the same ghost/cancel rules as a
            # multi-add. The spec is plain values, so nothing here can dangle.
            _state["place"] = []
            _state["place_group"] = spec
            _state["place_mouse"] = None
            _state["place_region"] = 0
            _tag_redraw(context)
            self.report({'INFO'}, "Click to place the group button")
            return {'FINISHED'}

        region = _picker_region(context)
        aspect = _view_aspect(region) if region else 1.0
        if self.use_cursor:
            x, y = self.cx, self.cy
        else:
            x, y = mid_x, top_y + spec["h"] * 0.75
        _create_group(context, arm, spec, x, y, aspect)
        _tag_redraw(context)
        self.report({'INFO'}, "Group of %d bones" % len(members))
        return {'FINISHED'}


def _remember_align_run(context, arm, btns, axis):
    """Snapshot an align run so the Align Gap slider can re-derive it live.

    The gap can NOT be applied to the result of an align: `_spread` only ever
    pushes buttons apart and is not idempotent on its own output, so raising the
    gap twice would push twice and lowering it again would pull nothing back -
    the row creeps. Every live re-derive therefore restarts from where the
    buttons were BEFORE the align, which is only knowable here.

    Held by INDEX + key, never as element references: a later add()/remove()
    leaves those pointing into freed or shifted memory (gotcha 18). `n` is the
    whole collection's length - if anything at all was added or removed the
    indices are suspect, so the run is dropped rather than half-trusted."""
    want = {b.as_pointer() for b in btns}
    _state["align_run"] = {
        "arm": arm.name,
        "axis": axis,
        "n": len(arm.madi_picker.buttons),
        "items": [(i, _btn_key(b), b.x, b.y)
                  for i, b in _iter_buttons(context, arm)
                  if b.as_pointer() in want],
    }


def _apply_gap_live(context):
    """Re-run the last align at the slider's current gap. -> buttons moved.

    Does nothing until something has been aligned this session (there is no gap
    without an axis), and nothing unless the CURRENT selection is still exactly
    that run - dragging the gap must not reach into buttons the user has since
    moved on from, and the run's buttons are the only ones whose pre-align
    positions are on record."""
    run = _state["align_run"]
    if not run:
        return 0
    arm = _target(context)
    if arm is None or arm.name != run["arm"]:
        return 0
    coll = arm.madi_picker.buttons
    if len(coll) != run["n"]:
        _state["align_run"] = None        # the layout changed under it
        return 0
    btns = []
    for i, key, _x, _y in run["items"]:
        if not (0 <= i < len(coll)) or _btn_key(coll[i]) != key:
            _state["align_run"] = None
            return 0
        btns.append(coll[i])
    if len(btns) < 2:
        return 0
    cur = {b.as_pointer() for b in MADI_OT_picker_align._targets(context)}
    if cur != {b.as_pointer() for b in btns}:
        return 0
    # back to the pre-align layout, then align it again at the new gap: the
    # result is identical to having pressed Align with the slider already there
    for btn, (_i, _k, x, y) in zip(btns, run["items"]):
        btn.x, btn.y = x, y
    MADI_OT_picker_align._run(context, btns, run["axis"], _gap(context))
    _tag_redraw(context)
    return len(btns)


class MADI_OT_picker_align(Operator):
    bl_idname = "madi_picker.align"
    bl_label = "Align Buttons"
    bl_description = "Line the selected buttons up on one axis"
    bl_options = {'REGISTER', 'UNDO'}

    axis: EnumProperty(
        name="Axis",
        items=(
            ('ROW', "Align Horizontally",
             "Line the selected buttons up in a horizontal row: every one takes "
             "the average Y, X is untouched"),
            ('COL', "Align Vertically",
             "Stack the selected buttons in a vertical column: every one takes "
             "the average X, Y is untouched"),
        ),
        default='ROW', options={'SKIP_SAVE'})

    @staticmethod
    def _targets(context):
        """What align actually moves: the selected BONE buttons.

        A GROUP handle is left where it is, for the same reason Ctrl+drag grabs a
        circle alone: it's parked above its cluster on purpose, and box-selecting
        that cluster selects the handle too - so including it would fling the
        handle into the row nobody asked it to join. A selection of nothing BUT
        groups is unambiguous, though, so then the handles are the targets."""
        sel = _selected_buttons(context)
        bones = [b for b in sel if b.kind != 'GROUP']
        return bones if bones else sel

    @staticmethod
    def _spread(btns, attr, dim, sizes, gap_frac):
        """Push a run apart along one axis until nothing overlaps, leaving a gap.

        Aligning is what CREATES the overlap: two buttons a hair apart in X end up
        inside each other the moment they share a Y. So the other axis gets
        de-overlapped in the same operation.

        Only where it actually overlaps, though - a button already clear of its
        neighbour keeps its position, so deliberate spacing (a gap between two
        finger clusters) survives. The run is then re-centred on its own original
        midpoint, so a row doesn't crawl to the right every time it's aligned."""
        items = sorted(((getattr(b, attr), sizes[b.as_pointer()][dim], b)
                        for b in btns), key=lambda t: t[0])
        gap = gap_frac * (sum(t[1] for t in items) / len(items))
        pos, prev_edge = [], None
        for p, size, _b in items:
            if prev_edge is not None:
                p = max(p, prev_edge + gap + size * 0.5)
            pos.append(p)
            prev_edge = p + size * 0.5
        shift = ((items[0][0] + items[-1][0]) - (pos[0] + pos[-1])) * 0.5
        for (_p0, _size, b), p in zip(items, pos):
            setattr(b, attr, p + shift)

    @staticmethod
    def _run(context, btns, axis, gap_frac):
        """The align itself, with no operator around it - so the Align Gap
        slider can re-run exactly this while it's being dragged."""
        # de-overlapping needs the DRAWN sizes (own scale applied) - the same
        # thing an edge-drag resizes - not the stored w/h
        sizes = {b.as_pointer(): _btn_wh(b) for b in btns}
        # the average, not the active button: box-select picks its "first" bone by
        # collection order, so an active-button anchor would be a coin toss.
        # Positions only - w/h/scale are never touched.
        if axis == 'ROW':
            y = sum(b.y for b in btns) / len(btns)
            for b in btns:
                b.y = y
            MADI_OT_picker_align._spread(btns, "x", 0, sizes, gap_frac)
        else:
            x = sum(b.x for b in btns) / len(btns)
            for b in btns:
                b.x = x
            MADI_OT_picker_align._spread(btns, "y", 1, sizes, gap_frac)

    def execute(self, context):
        arm = _target(context)
        btns = self._targets(context)
        if not arm or len(btns) < 2:
            self.report({'WARNING'}, "Select at least 2 buttons to align")
            return {'CANCELLED'}
        # BEFORE anything moves: the gap slider re-derives from these
        _remember_align_run(context, arm, btns, self.axis)
        self._run(context, btns, self.axis, _gap(context))
        _tag_redraw(context)
        self.report({'INFO'}, "Aligned %d button(s)" % len(btns))
        return {'FINISHED'}


class MADI_OT_picker_delete(Operator):
    bl_idname = "madi_picker.delete"
    bl_label = "Delete Buttons"
    bl_description = ("Delete the selected buttons - or, with nothing selected, "
                      "the button under the cursor. Never a blanket clear")
    bl_options = {'REGISTER', 'UNDO'}

    # Single implementation of the delete rule, shared by Del/X in the modal and
    # by the right-click menu. Both set _state["cursor_idx"] first, because the
    # "nothing selected -> the one under the cursor" half needs a cursor and a
    # menu item has none of its own.
    @staticmethod
    def _victims(context, arm):
        """-> (indices, came_from_the_selection). The flag matters because only
        the SELECTION path should take the bones down with the buttons: deleting
        the button under the cursor must not reach into a selection it was never
        part of."""
        sel = _selected_bone_names(arm)
        claimed = _claimed_bones(context, arm)
        idxs = [i for i, b in _iter_buttons(context, arm)
                if _btn_is_selected(b, sel, claimed)]
        if idxs:
            return idxs, True
        i = _state["cursor_idx"]
        return ([i] if 0 <= i < len(arm.madi_picker.buttons) else []), False

    def execute(self, context):
        arm = _target(context)
        if not arm:
            return {'CANCELLED'}
        idxs, from_sel = self._victims(context, arm)
        if not idxs:
            self.report({'WARNING'}, "Nothing to delete")
            return {'CANCELLED'}
        n = _remove_buttons(context, arm, idxs, drop_picks=from_sel)
        # both indices point into a collection that just moved under them
        _state["hover_idx"] = -1
        _state["cursor_idx"] = -1
        _tag_redraw(context)
        self.report({'INFO'}, "Deleted %d button(s)" % n)
        return {'FINISHED'}


def _btn_origin(btn, aspect=1.0):
    """-> (w, h, scale) this button was PLACED at, or None if it has no origin
    on record.

    No origin means a button made before the feature existed that has never been
    through a file load (`_on_load_post` seeds those from wherever they are, so a
    layout built at 0.1 can't offer to "reset" itself to a 0.04 it never had).
    Reset Size skips those rather than inventing a size for them.

    A GROUP's width is RE-DERIVED from its origin height instead of restored
    from `w0`: round means equal in PIXELS, so a w0 recorded under a different
    reference image's aspect would come back as an ellipse."""
    if btn.w0 <= 0.0 or btn.h0 <= 0.0 or btn.scale0 <= 0.0:
        return None
    w = btn.h0 * aspect if btn.kind == 'GROUP' else btn.w0
    return w, btn.h0, btn.scale0


class MADI_OT_picker_clear_slider_keys(Operator):
    bl_idname = "madi_picker.clear_slider_keys"
    bl_label = "Clear Keyframes"
    bl_description = ("Remove EVERY keyframe from this slider's shape key, on "
                      "every frame. The value it currently sits at is left "
                      "alone, and Ctrl+Z brings the animation back")
    bl_options = {'REGISTER', 'UNDO'}

    @staticmethod
    def _targets(context, arm):
        """(index, key count) for the sliders this would actually clear.

        ⚠ This does NOT follow the delete `_victims` rule, on purpose. Victims
        are "the selection, else the cursor", so a single selected bone button
        anywhere on the tab would swallow the whole list and right-clicking a
        slider would offer nothing (caught by probe, v0.24.0). Here **the
        CURSOR wins whenever it's on a slider** - "right-click THIS slider and
        clear it" can't depend on what else happens to be selected - and only
        a right-click somewhere else falls back to the selected sliders.

        Filtered to sliders that really have keys, which is what keeps the item
        out of the menu when it could do nothing. Twins are collapsed by
        (Key datablock, key name): two sliders on one shape key drive ONE
        curve, and clearing it twice would report double."""
        if not arm:
            return []
        coll = arm.madi_picker.buttons
        i = _state["cursor_idx"]
        if 0 <= i < len(coll) and coll[i].kind == 'SLIDER':
            cand = [i]
        else:
            sel = _selected_bone_names(arm)
            claimed = _claimed_bones(context, arm)
            cand = [j for j, b in _iter_buttons(context, arm)
                    if b.kind == 'SLIDER' and _btn_is_selected(b, sel, claimed)]
        out, seen = [], set()
        for i in cand:
            btn = coll[i]
            if btn.kind != 'SLIDER':
                continue
            kb = _slider_key(btn)
            if kb is None:
                continue
            ident = (kb.id_data.name, kb.name)
            if ident in seen:
                continue
            fc = _slider_fcurve(kb)
            if fc is None or not len(fc.keyframe_points):
                continue
            seen.add(ident)
            out.append((i, len(fc.keyframe_points)))
        return out

    def execute(self, context):
        arm = _target(context)
        targets = self._targets(context, arm)
        if not targets:
            self.report({'WARNING'}, "That slider has no keyframes")
            return {'CANCELLED'}
        coll = arm.madi_picker.buttons
        n_keys, names = 0, []
        for i, _n in targets:
            btn = coll[i]
            kb = _slider_key(btn)
            if kb is None:
                continue
            owner, fc = _slider_fcurve_owner(kb)
            if owner is None or fc is None:
                continue
            n_keys += len(fc.keyframe_points)
            names.append(btn.label or btn.sk_key or "slider")
            try:
                # the whole CHANNEL, not just its points: an emptied F-Curve
                # would leave the key "animated with no keyframes", and the
                # slider's pip would stay lit for a curve that does nothing
                owner.fcurves.remove(fc)
            except Exception as e:
                self.report({'WARNING'}, str(e))
                return {'CANCELLED'}
        _tag_redraw(context)
        self.report({'INFO'}, "Cleared %d keyframe(s) from %s"
                    % (n_keys, names[0] if len(names) == 1
                       else "%d sliders" % len(names)))
        return {'FINISHED'}


class MADI_OT_picker_reset_size(Operator):
    bl_idname = "madi_picker.reset_size"
    bl_label = "Reset Size"
    bl_description = ("Put the button back to the size it was placed at, "
                      "undoing edge-drags and Button Scale on it. Its position "
                      "is not touched")
    bl_options = {'REGISTER', 'UNDO'}

    @staticmethod
    def _targets(context, arm):
        """Indices of the buttons this would actually change.

        Targets follow the DELETE precedent (`_victims`): the selected buttons,
        else the one under the cursor. Anything already at its origin, or with no
        origin on record, is dropped here - which is also what keeps the menu
        item out of the menu when it couldn't do anything."""
        if not arm:
            return []
        region = _picker_region(context)
        aspect = _view_aspect(region) if region else 1.0
        coll = arm.madi_picker.buttons
        out = []
        for i in MADI_OT_picker_delete._victims(context, arm)[0]:
            btn = coll[i]
            o = _btn_origin(btn, aspect)
            if o is None:
                continue
            if (abs(btn.w - o[0]) > 1e-6 or abs(btn.h - o[1]) > 1e-6
                    or abs(btn.scale - o[2]) > 1e-6):
                out.append(i)
        return out

    def execute(self, context):
        arm = _target(context)
        idxs = self._targets(context, arm)
        if not idxs:
            self.report({'WARNING'}, "Nothing to reset")
            return {'CANCELLED'}
        region = _picker_region(context)
        aspect = _view_aspect(region) if region else 1.0
        coll = arm.madi_picker.buttons
        for i in idxs:
            btn = coll[i]
            # w/h AND scale: what's on screen is w * scale, so resetting the box
            # alone would leave a scaled-up button scaled up. Text Size is left
            # where it is - it's the label's size, not the button's.
            btn.w, btn.h, btn.scale = _btn_origin(btn, aspect)
        # the size dials follow the button you touch; leaving Button Scale
        # reading the pre-reset value would measure the next drag from a size
        # that no longer exists
        _sync_brushes(context, coll[idxs[0]])
        _tag_redraw(context)
        # a long label can hold a button above its origin (the floor is not
        # written back into w/h) - correct, but it looks like a failed reset
        self.report({'INFO'}, "Reset %d button(s) to placed size" % len(idxs))
        return {'FINISHED'}


def _fit_height(btn, aspect):
    """The `h` that would make this button hug the label it DRAWS. None = skip.

    Marty's answer to job 15 (2026-07-30): the vertical slack on a tall button is
    the button's SHAPE, not padding - the label is already as big as its width
    allows and no font change can fill the rest - so the only honest fix is to
    bring the height down to the writing. Since job 18 (v0.13.0) Button Scale
    calls this live on every step, so a scaled-up button re-shapes to its text
    instead of scaling its dead space up with it; the right-click item stays for
    fitting without scaling.

    Since v0.13.0 labels are per-button again, so the size this button DRAWS at
    is its own fit - at the hug point that fit is exactly WIDTH-bound, which
    closes the equation. In canvas-Y units, with W the drawn width, T = TEXT_PAD,
    k = cap_ratio / width_ratio: the ink height is k * (usable width), the
    height is ink + padding both sides, and padding reads the SMALLER side:
      k <= 1 (label wider than tall - the normal case): pad off the height,
              H = k*W / (1 - 2T*(1 - k))
      k >  1 (a very short label): pad off the width,
              H = W * (k*(1 - 2T) + 2T)
    The two cases meet exactly at k == 1 (both give H == W)."""
    if btn.kind in {'GROUP', 'SLIDER'}:
        return None               # round means round; a slider's shape is a
    text = _btn_text(btn)         # track, not a text box - never hug either
    if not text:
        return None
    ratio = _label_width_ratio(text)
    if ratio <= 0.0:
        return None
    if aspect <= 0.0:
        aspect = 1.0
    scale = btn.scale if btn.scale > 0.0 else 1.0
    bw_y = (btn.w * scale) / aspect            # drawn width, in canvas-Y units
    k = _cap_ratio() / ratio
    if k <= 1.0:                               # height is the smaller side
        bh = k * bw_y / (1.0 - 2.0 * TEXT_PAD * (1.0 - k))
    else:                                      # width is: pad off the width
        bh = bw_y * (k * (1.0 - 2.0 * TEXT_PAD) + 2.0 * TEXT_PAD)
    h = max(0.002, min(5.0, bh / scale))
    # It can only ever SHRINK: a height-bound label (h already under the hug
    # point) has no vertical slack to remove, so it is left alone.
    return min(h, btn.h)


class MADI_OT_picker_fit_height(Operator):
    bl_idname = "madi_picker.fit_height"
    bl_label = "Fit Height to Text"
    bl_description = ("Bring the button's height down until it hugs its label, "
                      "so the gap above and below the writing matches the gap "
                      "beside it. Width and position are not touched")
    bl_options = {'REGISTER', 'UNDO'}

    @staticmethod
    def _targets(context, arm):
        """Indices this would actually change - same DELETE precedent as Reset
        Size (`_victims`: the selection, else the button under the cursor), then
        drop anything already hugging its label, blank, or round."""
        if not arm:
            return []
        region = _picker_region(context)
        aspect = _view_aspect(region) if region else 1.0
        coll = arm.madi_picker.buttons
        out = []
        for i in MADI_OT_picker_delete._victims(context, arm)[0]:
            h = _fit_height(coll[i], aspect)
            if h is not None and abs(h - coll[i].h) > 1e-6:
                out.append(i)
        return out

    def execute(self, context):
        arm = _target(context)
        idxs = self._targets(context, arm)
        if not idxs:
            self.report({'WARNING'}, "Nothing to fit")
            return {'CANCELLED'}
        region = _picker_region(context)
        aspect = _view_aspect(region) if region else 1.0
        coll = arm.madi_picker.buttons
        n = 0
        for i in idxs:
            h = _fit_height(coll[i], aspect)
            if h is None:
                continue
            coll[i].h = h            # w0/h0 are NOT touched, so Reset Size undoes this
            n += 1
        _tag_redraw(context)
        self.report({'INFO'}, "Fitted %d button(s) to their label" % n)
        return {'FINISHED'}


class MADI_OT_picker_add_selected(Operator):
    bl_idname = "madi_picker.add_selected"
    bl_label = "Add Buttons for Selected Bones"
    bl_description = ("Arm one button per selected bone, then CLICK to place them "
                      "one at a time. Right-click, Esc or leaving the canvas "
                      "cancels whatever is left")
    bl_options = {'REGISTER'}          # places nothing itself: nothing to undo

    @staticmethod
    def _bones(context, arm=None):
        """The selected bones, in the ARMATURE's order - not selection order,
        which Blender doesn't record anyway. A chain (spine1..4, or a finger)
        therefore comes out in the order it runs along the rig."""
        arm = arm if arm is not None else _target(context)
        if not arm or not arm.pose:
            return []
        # hidden bones keep their selection flag, and a multi-add would happily
        # make buttons for bones the user can't even see
        return [pb.name for pb in arm.pose.bones if pb.select and _bone_shown(pb)]

    def execute(self, context):
        if not _state["running"]:
            self.report({'ERROR'}, "Start the picker first - placing needs clicks")
            return {'CANCELLED'}
        arm = _target(context)
        if not arm:
            self.report({'ERROR'}, "This tab has no armature")
            return {'CANCELLED'}
        names = self._bones(context, arm)
        if not names:
            self.report({'WARNING'}, "No bones selected")
            return {'CANCELLED'}
        # nothing is created here: the queue is armed and each button appears
        # where its own click lands (see MADI_OT_picker_session PLACE mode)
        _state["place"] = list(names)
        _state["place_mouse"] = None
        _state["place_region"] = 0
        _tag_redraw(context)
        self.report({'INFO'}, "Click to place %d button(s)" % len(names))
        return {'FINISHED'}


def _slider_sources(context):
    """Mesh objects with real shape keys, the picker target's own meshes (its
    children / anything it deforms via an Armature modifier) first."""
    arm = _target(context)
    out = []
    for ob in bpy.data.objects:
        if ob.type != 'MESH':
            continue
        keys = getattr(ob.data, "shape_keys", None)
        if not keys or len(keys.key_blocks) < 2:    # Basis alone isn't a slider
            continue
        near = arm is not None and (
            ob.parent == arm                        # == , never `is` (gotcha 24)
            or any(m.type == 'ARMATURE' and m.object == arm
                   for m in ob.modifiers))
        out.append((0 if near else 1, ob.name))
    out.sort()
    return [n for _rank, n in out]


class MADI_SKPick(PropertyGroup):
    """One tickable shape key in the Add Sliders dialog. NOT a flag enum: those
    are backed by a 32-bit int, and a face mesh can carry hundreds of keys."""
    use: BoolProperty(name="Use", default=False)
    # already has a slider on this tab: shown, but greyed and un-tickable
    # (v0.22.0) - one key, one slider per page
    taken: BoolProperty(default=False)
    # `name` is PropertyGroup's own built-in - the key's name goes there


def _sk_rebuild_picks(self, context):
    """(Re)fill the dialog's key list for the chosen mesh, key-block order.

    Keys that already have a slider on this tab come through FLAGGED rather
    than missing: a key you can see greyed out with "already on this tab"
    answers the question, a key that silently isn't in the list looks broken."""
    self.picks.clear()
    self.pick_index = 0
    ob = bpy.data.objects.get(self.object_name)
    keys = getattr(getattr(ob, "data", None), "shape_keys", None) if ob else None
    if not keys:
        return
    taken = _tab_slider_keys(context or bpy.context)
    ref = keys.reference_key
    for kb in keys.key_blocks:
        if kb != ref:                     # the Basis isn't a slider
            p = self.picks.add()
            p.name = kb.name
            p.taken = (self.object_name, kb.name) in taken


def _sk_tick_all(self, _context):
    for p in self.picks:
        p.use = self.tick_all and not p.taken


class MADI_UL_sk_picks(UIList):
    bl_idname = "MADI_UL_sk_picks"

    def draw_item(self, context, layout, data, item, icon,
                  active_data, active_prop, index):
        # ONE full-width checkbox with the key's name as its label, so clicking
        # ANYWHERE on the row ticks it. v0.19.0 drew a tiny box + a separate
        # name label - clicking the name only highlighted the row, and Marty
        # (reasonably) clicked names, hit OK and got "tick at least one".
        if item.taken:
            row = layout.row()
            row.enabled = False           # greyed: this page already has it
            row.label(text="%s   ·   already on this tab" % item.name,
                      icon='CHECKMARK')
            return
        layout.prop(item, "use", text=item.name)


class MADI_OT_picker_add_sliders(Operator):
    bl_idname = "madi_picker.add_sliders"
    bl_label = "Add Shape Key Sliders"
    bl_description = ("Pick a mesh and tick its shape keys, then CLICK to place "
                      "one slider per key. Dragging a slider drives the key "
                      "0-100%; your keyframe key over it keys it")
    bl_options = {'REGISTER'}          # places nothing itself: nothing to undo

    # a PLAIN string + prop_search, NOT a dynamic EnumProperty: dynamic enums
    # inside operator dialogs are a known-flaky RNA construct, and Marty's
    # dialog came up with no Mesh selector at all (v0.19.1). prop_search is
    # the same widget Blender uses everywhere and just works.
    object_name: StringProperty(name="Mesh", update=_sk_rebuild_picks)
    picks: CollectionProperty(type=MADI_SKPick)
    pick_index: IntProperty(default=0, options={'SKIP_SAVE'})
    tick_all: BoolProperty(name="All", default=False, update=_sk_tick_all,
                           options={'SKIP_SAVE'})

    def invoke(self, context, event):
        if not _state["running"]:
            self.report({'ERROR'}, "Start the picker first - placing needs clicks")
            return {'CANCELLED'}
        srcs = _slider_sources(context)
        if not srcs:
            self.report({'WARNING'}, "No mesh in this scene has shape keys")
            return {'CANCELLED'}
        # writing the property fires _sk_rebuild_picks, so the key list is
        # already filled with the rig's first keyed mesh when the dialog opens
        self.object_name = srcs[0]
        return context.window_manager.invoke_props_dialog(self, width=280)

    def draw(self, context):
        lay = self.layout
        lay.prop_search(self, "object_name", context.scene, "objects",
                        text="Mesh")
        if not self.picks:
            lay.label(text="Pick a mesh that has shape keys", icon='INFO')
            return
        n = sum(1 for p in self.picks if p.use)
        r = lay.row(align=True)
        r.label(text="Click keys to tick them (%d ticked):" % n)
        r.prop(self, "tick_all", text="All", toggle=True)
        lay.template_list(MADI_UL_sk_picks.bl_idname, "", self, "picks",
                          self, "pick_index", rows=8)

    def execute(self, context):
        if not _state["running"]:
            self.report({'ERROR'}, "Start the picker first - placing needs clicks")
            return {'CANCELLED'}
        arm = _target(context)
        if not arm:
            self.report({'ERROR'}, "This tab has no armature")
            return {'CANCELLED'}
        ticked = [p.name for p in self.picks if p.use]
        if not ticked:
            self.report({'WARNING'}, "Tick at least one shape key - click the "
                                     "key names in the list (or All), then OK")
            return {'CANCELLED'}
        # re-check against the LIVE tab, not the flags the dialog opened with:
        # one key gets one slider per page (v0.22.0)
        taken = _tab_slider_keys(context, arm)
        names = [n for n in ticked if (self.object_name, n) not in taken]
        if not names:
            self.report({'WARNING'},
                        "Already on this tab: %s" % ", ".join(ticked[:3]))
            return {'CANCELLED'}
        _state["place_sliders"] = [(self.object_name, n) for n in names]
        _state["place_mouse"] = None
        _state["place_region"] = 0
        _tag_redraw(context)
        skipped = len(ticked) - len(names)
        self.report({'INFO'}, "Click to place %d slider(s)%s"
                    % (len(names),
                       " (%d already on this tab)" % skipped if skipped else ""))
        return {'FINISHED'}


class MADI_MT_picker_context(Menu):
    """Right-click menu inside the picker canvas.

    Right-click deliberately does NOT change the selection (same as everywhere
    else in Blender), so the items act on exactly what was already selected. The
    menu is built from what's actually possible at that spot: an item that can't
    do anything is left OUT rather than greyed, so the menu answers "what can I
    do here" instead of listing everything the picker can ever do."""
    bl_idname = "MADI_MT_picker_context"
    bl_label = "Picker"

    def draw(self, context):
        layout = self.layout
        arm = _target(context)
        n_align = len(MADI_OT_picker_align._targets(context))
        on_button = _state["cursor_idx"] >= 0
        n_del = len(MADI_OT_picker_delete._victims(context, arm)[0]) if arm else 0
        n_reset = len(MADI_OT_picker_reset_size._targets(context, arm))
        n_fit = len(MADI_OT_picker_fit_height._targets(context, arm)) if arm else 0
        n_bones = len(MADI_OT_picker_add_selected._bones(context))
        empty = True

        if n_align >= 2:
            op = layout.operator(MADI_OT_picker_align.bl_idname,
                                 text="Align Horizontally", icon='ALIGN_MIDDLE')
            op.axis = 'ROW'
            op = layout.operator(MADI_OT_picker_align.bl_idname,
                                 text="Align Vertically", icon='ALIGN_CENTER')
            op.axis = 'COL'
            empty = False

        # both absent unless they would change something - no origin on record
        # / already at it, or already hugging the label
        if n_fit or n_reset:
            if not empty:
                layout.separator()
            empty = False
        if n_fit:
            layout.operator(
                MADI_OT_picker_fit_height.bl_idname,
                text="Fit Height to Text" if n_fit == 1
                else "Fit %d Heights to Text" % n_fit,
                icon='ARROW_LEFTRIGHT')
        if n_reset:
            layout.operator(
                MADI_OT_picker_reset_size.bl_idname,
                text="Reset Size" if n_reset == 1
                else "Reset %d Button Sizes" % n_reset,
                icon='FULLSCREEN_EXIT')

        # only over empty canvas: on a button, Ctrl+click is the add gesture and
        # what you almost certainly want there is Delete
        if not on_button and n_bones:
            if not empty:
                layout.separator()
            # the ellipsis is Blender's "this needs more input from you": the
            # buttons appear where you click, one click each
            layout.operator(
                MADI_OT_picker_add_selected.bl_idname,
                text="Add Button…" if n_bones == 1
                else "Add %d Buttons…" % n_bones,
                icon='ADD')
            empty = False

        # Create Group Button (jobs 19+20): the same op Ctrl+G runs, but ARMED -
        # you place the handle with a click, like a multi-add. Only over empty
        # space, and only when make_group could act: >= 2 distinct bones among
        # the selected BONE buttons (groups can't nest, so selected GROUP
        # handles don't count). Same "leave impossible items out" rule as
        # everything else here.
        if not on_button and arm:
            n_grp = len({b.bone for b in _selected_buttons(context, arm)
                         if b.kind == 'BONE' and b.bone})
            if n_grp >= 2:
                if not empty:
                    layout.separator()
                # the ellipsis is Blender's "this needs more input from you"
                op = layout.operator(MADI_OT_picker_make_group.bl_idname,
                                     text="Create Group Button…",
                                     icon='MESH_CIRCLE')
                op.place = True           # SKIP_SAVE (gotcha 13): set explicitly
                empty = False

        # shape key sliders (v0.19.0): empty space only, and only when some
        # mesh actually has keys to drive - same leave-it-out rule
        if not on_button and _slider_sources(context):
            if not empty:
                layout.separator()
            # ⚠ menus run operators with EXEC_REGION_WIN by default - invoke()
            # never runs, so the props dialog never opened and execute() fell
            # straight through to "tick at least one shape key" (the v0.19.0-2
            # ghost). Scope INVOKE_DEFAULT to a sub-layout so the other items
            # keep their plain-execute behaviour.
            sub = layout.column()
            sub.operator_context = 'INVOKE_DEFAULT'
            sub.operator(MADI_OT_picker_add_sliders.bl_idname,
                         text="Add Shape Key Slider(s)…",
                         icon='SHAPEKEY_DATA')
            empty = False

        # Clear Keyframes (v0.24.0): only over a slider that HAS keys, so the
        # item can't appear where it would do nothing - same rule as the rest
        n_clear = (MADI_OT_picker_clear_slider_keys._targets(context, arm)
                   if arm else [])
        if n_clear:
            if not empty:
                layout.separator()
            keys = sum(n for _i, n in n_clear)
            layout.operator(
                MADI_OT_picker_clear_slider_keys.bl_idname,
                text=("Clear %d Keyframe%s" % (keys, "" if keys == 1 else "s")
                      if len(n_clear) == 1
                      else "Clear %d Keyframes on %d Sliders" % (keys,
                                                                 len(n_clear))),
                icon='KEYFRAME')
            empty = False

        if n_del:
            if not empty:
                layout.separator()
            layout.operator(
                MADI_OT_picker_delete.bl_idname,
                text="Delete Button" if n_del == 1 else "Delete %d Buttons" % n_del,
                icon='TRASH')
            empty = False

        if empty:
            # an empty popup reads as broken - say why there's nothing here
            layout.label(text="Nothing selected", icon='INFO')


class MADI_OT_picker_add_tab(Operator):
    bl_idname = "madi_picker.add_tab"
    bl_label = "Add Tab"
    bl_description = ("Add another picker page - its own rig, its own reference "
                      "image and its own buttons")
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        scene = context.scene
        tabs = scene.madi_picker_tabs
        if not len(tabs):
            # first press on a fresh (or pre-tabs) scene: the migrated tab IS
            # the tab they asked for - don't hand them an empty second one
            _ensure_tabs(context)
            _tag_redraw(context)
            return {'FINISHED'}
        tab = tabs.add()
        tab.uid = _new_tab_uid(context)
        # a new tab starts on whatever rig you're posing, which is almost always
        # what you're about to map
        arm, _bone = _active_bone_and_arm(context)
        tab.armature = arm
        tab.name = arm.name if arm else "Picker %d" % len(tabs)
        scene.madi_picker_tab_index = len(tabs) - 1
        _tag_redraw(context)
        return {'FINISHED'}


class MADI_OT_picker_remove_tab(Operator):
    bl_idname = "madi_picker.remove_tab"
    bl_label = "Remove Tab"
    bl_description = ("Delete the active tab AND every button on it (buttons on "
                      "other tabs are untouched)")
    bl_options = {'REGISTER', 'UNDO'}

    def invoke(self, context, event):
        # deletes layout work: always confirm
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        scene = context.scene
        tabs = scene.madi_picker_tabs
        i = scene.madi_picker_tab_index
        if not (0 <= i < len(tabs)):
            return {'CANCELLED'}
        if len(tabs) == 1:
            self.report({'WARNING'}, "Keep at least one tab")
            return {'CANCELLED'}
        uid = tabs[i].uid              # a plain int: `tabs[i]` dies below
        # by uid across every armature, NOT via tab.armature - an empty pointer
        # (rig deleted, or the field re-pointed first) used to strand the buttons
        n = _purge_tab_buttons(uid)
        tabs.remove(i)
        scene.madi_picker_tab_index = min(i, len(tabs) - 1)
        _state["hover_idx"] = -1
        _tag_redraw(context)
        self.report({'INFO'}, "Tab removed (%d button(s))" % n)
        return {'FINISHED'}


class MADI_OT_picker_set_tab(Operator):
    bl_idname = "madi_picker.set_tab"
    bl_label = "Switch Tab"
    bl_description = "Show this picker page"

    index: IntProperty(default=0, options={'SKIP_SAVE'})

    def execute(self, context):
        tabs = context.scene.madi_picker_tabs
        if not (0 <= self.index < len(tabs)):
            return {'CANCELLED'}
        # the index property's update callback swaps the background image in
        context.scene.madi_picker_tab_index = self.index
        return {'FINISHED'}


class MADI_OT_picker_open_image(Operator, ImportHelper):
    bl_idname = "madi_picker.open_image"
    bl_label = "Open Reference"
    bl_description = ("Load a picture from disk as this tab's background and "
                      "show it in the Image Editor")
    bl_options = {'REGISTER', 'UNDO'}

    filter_glob: StringProperty(
        default="*.png;*.jpg;*.jpeg;*.tga;*.tif;*.tiff;*.bmp;*.exr;*.webp",
        options={'HIDDEN'})

    def execute(self, context):
        tab = _ensure_tabs(context)
        if not tab:
            return {'CANCELLED'}
        try:
            img = bpy.data.images.load(self.filepath, check_existing=True)
        except RuntimeError as e:
            self.report({'ERROR'}, "Could not load image: %s" % e)
            return {'CANCELLED'}
        tab.image = img          # its update callback shows it while running;
        _tag_redraw(context)     # stopped, the editors belong to the user
        return {'FINISHED'}


class MADI_OT_picker_use_editor_image(Operator):
    bl_idname = "madi_picker.use_editor_image"
    bl_label = "Use Editor Image"
    bl_description = ("Adopt whatever image this Image Editor is already showing "
                      "as the tab's background")

    def execute(self, context):
        tab = _ensure_tabs(context)
        space = context.space_data
        img = getattr(space, "image", None) if space else None
        if not tab or img is None:
            self.report({'WARNING'}, "This editor has no image loaded")
            return {'CANCELLED'}
        tab.image = img
        _tag_redraw(context)
        return {'FINISHED'}


class MADI_OT_picker_pick_appearance(Operator):
    bl_idname = "madi_picker.pick_appearance"
    bl_label = "Copy From Selected"
    bl_description = ("Load the color of the first selected button into the "
                      "brushes above (text size and scale already follow "
                      "whichever button you click)")

    def execute(self, context):
        sel = _selected_buttons(context)
        if not sel:
            self.report({'WARNING'}, "No button selected")
            return {'CANCELLED'}
        # size + scale already follow the button you click; this adds the colour
        _sync_brushes(context, sel[0], color=True)
        return {'FINISHED'}


def _list_active_button(context, arm):
    """The button the N-panel's list has highlighted, or None.

    ⚠ `active_index` addresses the WHOLE collection. Every tab's buttons live in
    one list on the armature and `MADI_UL_buttons.filter_items` merely HIDES the
    other tabs' rows - the real indices never move - so after a tab switch the
    active index can still point at a row that is not on screen. Editing that
    would silently resize a button on a tab the user isn't looking at, so the
    tab_uid test below is the whole reason this helper exists."""
    if arm is None:
        return None
    data = arm.madi_picker
    i = data.active_index
    if not 0 <= i < len(data.buttons):
        return None
    btn = data.buttons[i]
    return btn if btn.tab_uid == _active_uid(context) else None


def _view3d_spaces(context):
    """Every 3D Viewport open in every window."""
    out = []
    for window in context.window_manager.windows:
        for area in window.screen.areas:
            if area.type != 'VIEW_3D':
                continue
            for space in area.spaces:
                if space.type == 'VIEW_3D':
                    out.append(space)
    return out


class MADI_OT_picker_viewport_overlays(Operator):
    bl_idname = "madi_picker.viewport_overlays"
    bl_label = "Bones & Extras"
    bl_description = ("Show or hide BONES and EXTRAS (empties, cameras, other "
                      "visual guides) in every 3D Viewport - so the rig can be "
                      "picked from here with the character clear of controls. "
                      "Nothing is moved or hidden in the scene: these are "
                      "Blender's own two overlay switches")
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        spaces = _view3d_spaces(context)
        if not spaces:
            self.report({'WARNING'}, "No 3D Viewport open")
            return {'CANCELLED'}
        # ⚠ OFF unless EVERY viewport already has both off. One press must have
        # one meaning: with a mixed set (bones on in one view, off in another)
        # a per-space flip would leave it just as mixed, and the button would
        # look broken. Turning everything off first, then everything on, is the
        # behaviour that reads as a single switch.
        show = not _overlays_on(context)
        for space in spaces:
            space.overlay.show_bones = show
            space.overlay.show_extras = show
        self.report({'INFO'}, "Bones & extras %s in %d viewport(s)"
                    % ("shown" if show else "hidden", len(spaces)))
        return {'FINISHED'}


def _overlays_on(context):
    """True when EVERY 3D Viewport is showing both bones and extras.

    Drives the panel button's `depress`, so the control reads as a switch that
    is currently on rather than a button you press hopefully. With no viewport
    open at all this is False - there is nothing on show."""
    spaces = _view3d_spaces(context)
    return bool(spaces) and all(s.overlay.show_bones and s.overlay.show_extras
                                for s in spaces)


# ---------------------------------------------------------------------------
# Presets (shareable .json layouts)
# ---------------------------------------------------------------------------
def _preset_from_arm(context, arm):
    tab = _active_tab(context)
    out = []
    for _i, btn in _iter_buttons(context, arm):
        item = {
            "kind": btn.kind,
            "label": btn.label,
            "x": round(btn.x, 6), "y": round(btn.y, 6),
            "w": round(btn.w, 6), "h": round(btn.h, 6),
            "color": [round(c, 5) for c in btn.color],
            "scale": round(btn.scale, 6),
            # v4: what Reset Size goes back to, and whether the button draws
            # text at all. Without the origin a loaded layout would have no
            # "original" to return to.
            "blank": bool(btn.blank),
            "w0": round(btn.w0, 6), "h0": round(btn.h0, 6),
            "scale0": round(btn.scale0, 6),
        }
        if btn.kind == 'GROUP':
            item["members"] = [m.bone for m in btn.members]
        elif btn.kind == 'SLIDER':
            item["sk_object"] = btn.sk_object
            item["sk_key"] = btn.sk_key
        else:
            item["bone"] = btn.bone
        out.append(item)
    img = tab.image if tab else None
    preset = {"format": "madi_picker_preset",
              "version": PRESET_VERSION,
              "source_rig": arm.name,
              "tab": tab.name if tab else "",
              # the BRUSH values (what a new button would get). Older readers
              # treated these as global, which is exactly the right fallback.
              "scale": round(context.scene.madi_picker_scale, 6),
              "buttons": out}
    if img is not None:
        # name first, path as the fallback - a layout traced over a reference is
        # meaningless without it, but the file may not exist on another machine
        preset["image"] = img.name
        try:
            preset["image_path"] = bpy.path.abspath(img.filepath)
        except (AttributeError, ValueError):
            pass
    return preset


def _preset_image(context, data):
    """Restore a preset's reference picture: an image already in the file wins,
    otherwise try the recorded path. Silently gives up - a missing reference must
    never block the layout from loading."""
    name = data.get("image")
    if name and name in bpy.data.images:
        return bpy.data.images[name]
    path = data.get("image_path")
    if path:
        try:
            return bpy.data.images.load(path, check_existing=True)
        except RuntimeError:
            return None
    return None


def _preset_to_arm(context, arm, data, replace):
    """-> (added, missing_names). Never touches the rig, only the layout.

    Loads into the ACTIVE TAB: `replace` clears that tab's buttons, not the
    other tabs' buttons that happen to live on the same armature."""
    coll = arm.madi_picker.buttons
    uid = _active_uid(context)
    if replace:
        # every pick on this tab dies with it, so the bones go too - otherwise
        # the buttons loaded in below light up for a selection nobody made
        _remove_buttons(context, arm,
                        [j for j, b in enumerate(coll) if b.tab_uid == uid])
    added = 0
    # the preset's top-level scale is the brush value. Any "font" a v1-v4 preset
    # carries is read and dropped: since v0.11.0 a label is sized from the button
    # it sits in, so there is nothing to restore it into.
    d_scale = 1.0
    _state["suppress_apply"] = True       # brushes write to the selection
    try:
        if "scale" in data:
            try:
                context.scene.madi_picker_scale = float(data["scale"])
            except (TypeError, ValueError):
                pass
        d_scale = context.scene.madi_picker_scale
    finally:
        _state["suppress_apply"] = False
    _remember_brushes(context.scene)
    tab = _active_tab(context)
    if tab:
        img = _preset_image(context, data)
        if img is not None:
            tab.image = img               # its update shows it if running
            if _state["running"]:
                _show_image(context, img)
    for item in data.get("buttons", []):
        btn = coll.add()
        btn.tab_uid = uid
        btn.kind = item.get("kind", 'BONE')
        btn.label = item.get("label", "")
        if btn.kind == 'GROUP':
            for name in item.get("members", []):
                btn.members.add().bone = name
        elif btn.kind == 'SLIDER':
            btn.sk_object = item.get("sk_object", "")
            btn.sk_key = item.get("sk_key", "")
        else:
            btn.bone = item.get("bone", "")
        btn.x = float(item.get("x", 0.5))
        btn.y = float(item.get("y", 0.5))
        btn.w = float(item.get("w", DEFAULT_SCALE))
        btn.h = float(item.get("h", DEFAULT_SCALE))
        col = item.get("color", DEFAULT_COLOR)
        btn.color = (float(col[0]), float(col[1]), float(col[2]))
        btn.scale = float(item.get("scale", d_scale))
        btn.blank = bool(item.get("blank", False))
        # v3 and older carry no origin, so the layout AS LOADED is the origin -
        # the same rule _on_load_post applies to buttons that predate the feature
        btn.w0 = float(item.get("w0", 0.0)) or btn.w
        btn.h0 = float(item.get("h0", 0.0)) or btn.h
        btn.scale0 = float(item.get("scale0", 0.0)) or btn.scale
        added += 1
    arm.madi_picker.active_index = max(0, len(coll) - 1)
    missing = set()
    for _i, btn in _iter_buttons(context, arm):
        missing.update(n for n in _btn_missing(arm, btn) if n)
    return added, sorted(missing)


class MADI_OT_picker_save_preset(Operator, ExportHelper):
    bl_idname = "madi_picker.save_preset"
    bl_label = "Save Preset"
    bl_description = ("Write the ACTIVE TAB's picker layout to a .json file you "
                      "can keep or share")

    filename_ext = ".json"
    filter_glob: StringProperty(default="*.json", options={'HIDDEN'})

    def execute(self, context):
        arm = _target(context)
        if not arm:
            return {'CANCELLED'}
        n = len(_iter_buttons(context, arm))
        if not n:
            self.report({'WARNING'}, "No buttons on this tab to save")
            return {'CANCELLED'}
        try:
            with open(self.filepath, 'w', encoding='utf-8') as f:
                json.dump(_preset_from_arm(context, arm), f, indent=2)
        except OSError as e:
            self.report({'ERROR'}, "Could not write preset: %s" % e)
            return {'CANCELLED'}
        self.report({'INFO'}, "Saved %d button(s)" % n)
        return {'FINISHED'}


class MADI_OT_picker_load_preset(Operator, ImportHelper):
    bl_idname = "madi_picker.load_preset"
    bl_label = "Load Preset"
    bl_description = ("Load a .json picker layout onto this tab's rig. Bones the "
                      "rig doesn't have are outlined red and can be retargeted")
    bl_options = {'REGISTER', 'UNDO'}

    filename_ext = ".json"
    filter_glob: StringProperty(default="*.json", options={'HIDDEN'})
    replace: BoolProperty(
        name="Replace Existing",
        description="Clear this TAB's current buttons first (other tabs on the "
                    "same rig are left alone)",
        default=True)

    def execute(self, context):
        _ensure_tabs(context)
        arm = _target(context)
        if not arm:
            self.report({'ERROR'}, "Pick an armature first")
            return {'CANCELLED'}
        try:
            with open(self.filepath, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except (OSError, ValueError) as e:
            self.report({'ERROR'}, "Could not read preset: %s" % e)
            return {'CANCELLED'}
        if not isinstance(data, dict) or "buttons" not in data:
            self.report({'ERROR'}, "Not a MADI Picker preset")
            return {'CANCELLED'}
        try:
            added, missing = _preset_to_arm(context, arm, data, self.replace)
        except (TypeError, ValueError, IndexError) as e:
            self.report({'ERROR'}, "Malformed preset: %s" % e)
            return {'CANCELLED'}
        _tag_redraw(context)
        if missing:
            self.report({'WARNING'},
                        "Loaded %d button(s) - %d bone name(s) not on this rig, "
                        "retarget them in the panel" % (added, len(missing)))
        else:
            self.report({'INFO'}, "Loaded %d button(s)" % added)
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------
def _tab_group_map(data, uid):
    """bone name -> index of the FIRST same-tab GROUP handle carrying it.

    Drives the UIList's visual grouping (job 30): a BONE button whose bone is
    in here is pulled to sit under that handle and draws indented. DERIVED on
    every draw, nothing stored - so deleting the handle reverts the list by
    itself and the map can never go stale. A bone carried by two groups shows
    under the first handle only (a display permutation can't duplicate rows)."""
    out = {}
    for i, b in enumerate(data.buttons):
        if b.tab_uid == uid and b.kind == 'GROUP':
            for m in b.members:
                if m.bone and m.bone not in out:
                    out[m.bone] = i
    return out


class MADI_UL_buttons(UIList):
    """Per-button list: label only, and a red row when the bone is missing.
    Color lives in the Appearance box above - no second swatch down here.
    Grouped bones sit indented under their GROUP handle (job 30)."""
    bl_idname = "MADI_UL_buttons"

    def draw_item(self, context, layout, data, item, icon,
                  active_data, active_prop, index):
        arm = _target(context)
        sel = _selected_bone_names(arm) if arm else set()
        claimed = _claimed_bones(context, arm) if arm else None
        missing = bool(_btn_missing(arm, item)) if arm else False
        row = layout.row(align=True)
        # a grouped bone draws indented under its handle (job 30); membership
        # is derived per draw, so it needs no per-button state
        if (item.kind == 'BONE' and item.bone
                and item.bone in _tab_group_map(data, _active_uid(context))):
            row.label(text="", icon='BLANK1')
        if missing:
            row.alert = True
            row.label(text="", icon='ERROR')
        else:
            row.label(text="", icon='RESTRICT_SELECT_OFF'
                      if _btn_is_selected(item, sel, claimed) else 'BLANK1')
        # a blank button keeps its label, it just doesn't draw it - grey the
        # field so the list doesn't promise text the canvas isn't showing
        sub = row.row(align=True)
        sub.active = not item.blank
        sub.prop(item, "label", text="", emboss=False)
        if item.kind == 'GROUP':
            row.label(text="%d bones" % len(item.members))
        elif item.kind == 'SLIDER':
            row.label(text="", icon='SHAPEKEY_DATA')
        # the only way BACK from blank: the scene toggle only decides what new
        # buttons get, so without this a blank button would be one-way
        row.prop(item, "blank", text="",
                 icon='BLANK1' if item.blank else 'FONT_DATA', emboss=False)

    def filter_items(self, context, data, propname):
        """Hide buttons that belong to another tab, and pull each GROUP's
        member buttons to sit directly under their handle (job 30).

        Filtering here (rather than keeping a per-tab collection) is what lets
        buttons stay in ONE collection on the armature: active_index and every
        operator keep addressing the real indices - the neworder below is a
        pure DISPLAY permutation, the real indices never move."""
        buttons = getattr(data, propname)
        uid = _active_uid(context)
        flt = [self.bitflag_filter_item if b.tab_uid == uid else 0
               for b in buttons]
        gmap = _tab_group_map(data, uid)
        seq, placed = [], set()
        for i, b in enumerate(buttons):
            if i in placed or b.tab_uid != uid:
                continue
            if b.kind == 'BONE' and b.bone in gmap:
                continue          # deferred: lands under its handle below
            seq.append(i)
            placed.add(i)
            if b.kind == 'GROUP':
                # every same-tab bone button this handle claims FIRST, in
                # collection order (twins both land here)
                for j, mb in enumerate(buttons):
                    if (j not in placed and mb.tab_uid == uid
                            and mb.kind == 'BONE'
                            and gmap.get(mb.bone) == i):
                        seq.append(j)
                        placed.add(j)
        for i in range(len(buttons)):     # other tabs: hidden, but the
            if i not in placed:           # permutation must still be complete
                seq.append(i)
        order = [0] * len(buttons)
        for rank, i in enumerate(seq):
            order[i] = rank
        return flt, order


class MADI_PT_picker(Panel):
    bl_label = "MADI Picker"
    bl_idname = "MADI_PT_picker"
    bl_space_type = 'IMAGE_EDITOR'
    bl_region_type = 'UI'
    bl_category = "MADI Picker"

    def draw(self, context):
        layout = self.layout

        # ⚠ NO LOCKED CARD HERE ANY MORE — the picker is free (2026-08-06).
        # This used to open with `entitlement.state()` and, when locked, draw a
        # "Locked / open the app and sign in" card instead of the whole panel.
        scene = context.scene
        tabs = scene.madi_picker_tabs

        # ---- tab strip -------------------------------------------------------
        # NOTE: a Panel may not write to ID data, so the default tab can't be
        # created here - offer the button and let the operator do it.
        if not len(tabs):
            layout.operator("madi_picker.add_tab", text="Create a Picker Tab",
                            icon='ADD')
            legacy = scene.madi_picker_target
            if legacy:
                layout.label(text="Existing layout on %s will move into it"
                                  % legacy.name, icon='INFO')
            return

        tab = _active_tab(context)
        strip = layout.grid_flow(row_major=True, columns=2, align=True)
        for i, t in enumerate(tabs):
            op = strip.operator("madi_picker.set_tab",
                                text=t.name or "Tab %d" % (i + 1),
                                depress=(t == tab),
                                icon='ARMATURE_DATA' if t.armature else 'BLANK1')
            op.index = i
        r = layout.row(align=True)
        r.operator("madi_picker.add_tab", text="", icon='ADD')
        r.operator("madi_picker.remove_tab", text="", icon='REMOVE')
        if tab:
            r.prop(tab, "name", text="")

        arm = _target(context)
        col = layout.column(align=True)
        if _state["running"]:
            col.operator("madi_picker.stop", icon='PAUSE', depress=True)
        else:
            col.operator("madi_picker.session", icon='PLAY')
        # Marty, 2026-08-10: "a button that toggles bones and extras from 3d
        # viewport". It sits with Start/Stop because that is when you want it -
        # picking from the picture, with the viewport clear of octahedrons and
        # empties - and it depresses to show which way it currently is.
        on = _overlays_on(context)
        col.operator("madi_picker.viewport_overlays",
                     text="Bones & Extras: %s" % ("shown" if on else "hidden"),
                     icon='HIDE_OFF' if on else 'HIDE_ON', depress=on)

        if tab:
            tb = layout.box()
            tb.prop(tab, "armature")
            tb.label(text="Background", icon='IMAGE_REFERENCE')
            tb.prop(tab, "image", text="")
            rr = tb.row(align=True)
            rr.operator("madi_picker.open_image", text="Open...", icon='FILEBROWSER')
            rr.operator("madi_picker.use_editor_image", text="Use Editor's",
                        icon='IMAGE_DATA')

        if not arm:
            layout.label(text="Pick an armature to begin", icon='INFO')
            return

        data = arm.madi_picker
        tab_btns = _iter_buttons(context, arm)
        n_sel = len(_selected_buttons(context, arm))

        # ---- appearance: three live brushes, no Apply ------------------------
        # all three write to the SELECTION; with nothing selected they're just
        # what the next button added will get. One header says which it is.
        app = layout.box()
        app.label(text="Applies to: %s" % ("%d selected" % n_sel if n_sel
                                           else "new buttons"), icon='BRUSH_DATA')
        app.prop(scene, "madi_picker_color", text="")
        # no Text Size: labels are sized from the buttons, so Button Scale scales
        # the writing too (v0.11.0; per-button again since v0.13.0) - and on a
        # labelled button it also pulls the height down to hug the text (job 18)
        app.prop(scene, "madi_picker_scale", text="Button Scale", slider=True)
        app.operator("madi_picker.pick_appearance", icon='EYEDROPPER')
        # Align Gap is live since v0.10.0 - it re-runs the last align as it's
        # dragged - so it belongs with the things that act on the selection, not
        # in the Buttons box where it sat as a plain setting. It needs an axis
        # before it means anything, hence the second line.
        app.separator()
        app.label(text="Align Gap: %s" % ("live on the last align"
                                          if _state["align_run"]
                                          else "align something first"),
                  icon='ALIGN_MIDDLE')
        app.prop(scene, "madi_picker_gap", text="", slider=True)

        # ---- buttons (this tab only) ----------------------------------------
        box = layout.box()
        box.label(text="Buttons (%d)" % len(tab_btns), icon='MESH_PLANE')
        box.template_list(MADI_UL_buttons.bl_idname, "", data, "buttons",
                          data, "active_index", rows=5)
        # ⚠ THE ONLY WAY TO RESIZE ONE BUTTON ON ITS OWN (2026-08-10).
        # Marty: "i just want a way to be able to scale group buttons
        # individually because i can't do it now" - and Button Scale above
        # cannot do it, because it is a BRUSH on the SELECTION:
        #   · select a GROUP's member bones in the viewport and the handle AND
        #     every member button are all state 1, so the brush grows the lot
        #     together (measured, 2026-08-10);
        #   · highlight the handle in this list instead and it is state 0 -
        #     a list row is not a selection - so the brush does nothing at all.
        # This row writes `btn.scale` on ONE button, so it is individual by
        # construction, for every kind. `scale`'s own update fires the redraw.
        act_btn = _list_active_button(context, arm)
        if act_btn is not None:
            one = box.column(align=True)
            one.label(text="Just this one: %s"
                           % (act_btn.label or act_btn.kind.title()),
                      icon='MESH_CIRCLE' if act_btn.kind == 'GROUP'
                      else 'MESH_PLANE')
            one.prop(act_btn, "scale", text="Scale", slider=True)
        r = box.row(align=True)
        r.operator("madi_picker.add_active", text="Add", icon='ADD')
        r.operator("madi_picker.remove_button", text="Remove", icon='REMOVE')
        # armed placement while the picker runs (job 20); with no session to
        # catch the click, execute falls back to "above the cluster"
        op = box.operator("madi_picker.make_group", text="Group Selected",
                          icon='MESH_CIRCLE')
        op.place = True
        # a setting, not a brush: it only affects buttons added AFTER it, which
        # is why it can't touch the selection and doesn't live in Appearance
        box.prop(scene, "madi_picker_blank", text="New buttons: no label")
        act = arm.data.bones.active
        box.label(text="Active bone: %s" % (act.name if act else "-"))

        # ---- presets --------------------------------------------------------
        pre = layout.box()
        pre.label(text="Presets", icon='FILE_BLEND')
        r = pre.row(align=True)
        r.operator("madi_picker.save_preset", text="Save", icon='EXPORT')
        r.operator("madi_picker.load_preset", text="Load", icon='IMPORT')

        # ---- retarget (only when something doesn't match this rig) ----------
        rows = []
        for _i, btn in tab_btns:
            if btn.kind == 'GROUP':
                rows += [(btn, m) for m in btn.members
                         if m.bone not in arm.pose.bones]
            elif btn.kind == 'SLIDER':
                # a slider is unmatched when its mesh/key doesn't resolve -
                # its `bone` is empty by design, so the bone test would flag
                # every slider ever made
                if _slider_key(btn) is None:
                    rows.append((btn, None))
            elif btn.bone not in arm.pose.bones:
                rows.append((btn, None))
        if rows:
            rt = layout.box()
            rt.label(text="Retarget (%d unmatched)" % len(rows), icon='ERROR')
            rt.label(text="Pick the bone on this rig:")
            for btn, member in rows:
                r = rt.row(align=True)
                r.alert = True
                if btn.kind == 'SLIDER':
                    # sliders retarget to a mesh + key, not to a bone
                    r.label(text="", icon='SHAPEKEY_DATA')
                    r.prop_search(btn, "sk_object", bpy.data, "objects",
                                  text="")
                    s_ob = bpy.data.objects.get(btn.sk_object)
                    s_keys = getattr(getattr(s_ob, "data", None),
                                     "shape_keys", None) if s_ob else None
                    if s_keys is not None:
                        r.prop_search(btn, "sk_key", s_keys, "key_blocks",
                                      text="")
                    continue
                r.label(text="", icon='MESH_CIRCLE' if btn.kind == 'GROUP'
                        else 'MESH_PLANE')
                if member is None:
                    r.prop_search(btn, "bone", arm.pose, "bones", text="",
                                  icon='BONE_DATA')
                else:
                    r.prop_search(member, "bone", arm.pose, "bones", text="",
                                  icon='BONE_DATA')

        # ---- cheat sheet ----------------------------------------------------
        col = layout.column(align=True)
        col.label(text="While running:")
        col.label(text="Click=select | Drag=box | Empty=deselect",
                  icon='RESTRICT_SELECT_OFF')
        col.label(text="Ctrl+Click=add | Ctrl+Drag=move (they may overlap)",
                  icon='GREASEPENCIL')
        col.label(text="Ctrl+Drag a button EDGE=resize", icon='FULLSCREEN_ENTER')
        col.label(text="Ctrl+G=group AT THE MOUSE | Del=delete", icon='TRASH')
        col.label(text="Right-click=menu: align/fit/reset/add/group/"
                       "sliders/delete", icon='ALIGN_MIDDLE')
        col.label(text="Adding many bones=one click each", icon='RESTRICT_SELECT_OFF')
        col.label(text="Slider: drag=value | keyframe key over it=key it",
                  icon='SHAPEKEY_DATA')


# ---------------------------------------------------------------------------
# Register
# ---------------------------------------------------------------------------
_classes = (
    MADI_PickerBoneRef,
    MADI_SKPick,
    MADI_PickerButton,
    MADI_PickerData,
    MADI_PickerTab,
    MADI_OT_picker_session,
    MADI_OT_picker_stop,
    MADI_OT_picker_add_active,
    MADI_OT_picker_remove_button,
    MADI_OT_picker_make_group,
    MADI_OT_picker_align,
    MADI_OT_picker_delete,
    MADI_OT_picker_clear_slider_keys,
    MADI_OT_picker_reset_size,
    MADI_OT_picker_fit_height,
    MADI_OT_picker_add_selected,
    MADI_MT_picker_context,
    MADI_OT_picker_add_tab,
    MADI_OT_picker_remove_tab,
    MADI_OT_picker_set_tab,
    MADI_OT_picker_open_image,
    MADI_OT_picker_use_editor_image,
    MADI_OT_picker_pick_appearance,
    MADI_OT_picker_viewport_overlays,
    MADI_OT_picker_save_preset,
    MADI_OT_picker_load_preset,
    MADI_OT_picker_add_sliders,
    MADI_UL_sk_picks,
    MADI_UL_buttons,
    MADI_PT_picker,
)


# ---------------------------------------------------------------------------
# NO LICENCE GATE - the Bone picker is FREE (Marty, 2026-08-06)
# ---------------------------------------------------------------------------
# Every operator here used to be wrapped with an `entitlement.unlocked()`
# check installed by walking `_classes` at import. That is gone: the app's
# Bone picker tab is free, and a free tab whose Blender half still refused
# would be worse than either choice made cleanly.
#
# ⚠ IF A GATE IS EVER PUT BACK HERE, read `docsone-picker.md` first -
# it keeps the two things that were expensive to learn. `register_class`
# INSPECTS an operator's method signatures and rejects the class outright
# (`expected Operator, "invoke" function to have 3 args, found 1`), so one
# `*args` wrapper for execute and invoke cannot be registered at all; and
# the check must never go on `_draw_callback`, which runs once per frame
# per Image Editor.



# ---------------------------------------------------------------------------
# Bridge API — what the Toolset app's "Bone picker" tab reads and writes
# ---------------------------------------------------------------------------
# THE TWO UIs CANNOT DRIFT, and not because anything synchronises them: there is
# no second copy of the layout. The buttons live on the armature
# (`obj.madi_picker`) and the tabs on the Scene; this panel and the app both
# read and write THOSE. The app polls `picker_status` and Blender redraws its
# panel constantly, so a change on either side turns up on the other without
# either one being told.
#
# ⚠ Not even the appearance settings are a mirror. Anim Layers has a real
# two-store mirror (app config.json <-> add-on preferences) with a first-contact
# rule and an echo guard, and it only exists because the app owned those
# settings first. Here the add-on preferences are the ONLY store and the app is
# a remote control - strictly simpler, and there is nothing to get out of step.
# Don't "improve" this into a mirror.


def _picker_arm(context):
    """The active tab's rig, without creating anything."""
    tabs = context.scene.madi_picker_tabs
    idx = context.scene.madi_picker_tab_index
    if 0 <= idx < len(tabs):
        return tabs[idx].armature
    return None


def _button_row(index, btn, arm):
    row = {
        "index": index,
        "kind": btn.kind,
        "label": btn.label,
        "x": round(btn.x, 6), "y": round(btn.y, 6),
        "w": round(btn.w, 6), "h": round(btn.h, 6),
        "color": [round(c, 5) for c in btn.color],
        "scale": round(btn.scale, 6),
        "blank": bool(btn.blank),
        "picked": bool(btn.select),
        "tab_uid": btn.tab_uid,
        "bone": btn.bone,
        "members": [m.bone for m in btn.members],
        "sk_object": btn.sk_object,
        "sk_key": btn.sk_key,
    }
    row["missing"] = [n for n in _btn_missing(arm, btn) if n] if arm else []
    return row


def picker_status():
    """Everything the app's tab shows. PURE READ.

    ⚠ IT MUST STAY PURE. This is POLLED, so anything it wrote would land on a
    timer for as long as the app is open - undo steps, a dirtied file, a scene
    that changes while nobody is touching it. In particular it must NEVER call
    `_ensure_tabs()`, which CREATES a default tab: the app asking "what have you
    got?" must not be what gives a scene its first picker tab. `picker_add_tab`
    is how a tab comes into being, and it is a deliberate act.
    """
    context = bpy.context
    scene = context.scene
    tabs = scene.madi_picker_tabs
    idx = scene.madi_picker_tab_index
    arm = _picker_arm(context)
    out = {
        "running": bool(_state["running"]),
        "active_index": idx,
        "active_uid": _active_uid(context) if len(tabs) else 0,
        "armature": arm.name if arm else None,
        "tabs": [{"index": i,
                  "name": t.name,
                  "uid": t.uid,
                  "armature": t.armature.name if t.armature else None,
                  "image": t.image.name if t.image else None}
                 for i, t in enumerate(tabs)],
        "buttons": [],
        "active_button": arm.madi_picker.active_index if arm else -1,
        # Everything the app needs to offer a choice, so it never has to guess
        # a name or keep its own list. `bones` and `meshes` are what make
        # RETARGETING possible in the app at all - without them the tab could
        # show that a button is unmatched but not offer anything to fix it with.
        "armatures": sorted(o.name for o in bpy.data.objects
                            if o.type == 'ARMATURE'),
        "images": sorted(i.name for i in bpy.data.images),
        "bones": sorted(pb.name for pb in arm.pose.bones) if arm else [],
        "meshes": {o.name: [kb.name for kb in o.data.shape_keys.key_blocks]
                   for o in bpy.data.objects
                   if o.type == 'MESH' and getattr(o.data, "shape_keys", None)},
        "brushes": {
            "color": [round(c, 5) for c in scene.madi_picker_color],
            "scale": round(scene.madi_picker_scale, 6),
            "gap": round(scene.madi_picker_gap, 6),
            "blank": bool(scene.madi_picker_blank),
        },
        "prefs": picker_prefs(),
    }
    if arm is not None:
        out["buttons"] = [_button_row(i, b, arm)
                          for i, b in _iter_buttons(context, arm)]
    out["unmatched"] = sum(1 for b in out["buttons"] if b["missing"])
    return out


def picker_prefs():
    """The three appearance settings. The add-on preferences are the ONLY
    store - see the note at the top of this section."""
    p = _prefs()
    if p is None:
        return {}
    return {"btn_alpha": round(p.pk_btn_alpha, 4),
            "btn_round": round(p.pk_btn_round, 4),
            "bg_darken": round(p.pk_bg_darken, 4)}


def picker_set_prefs(values):
    p = _prefs()
    if p is not None and isinstance(values, dict):
        if "btn_alpha" in values:
            p.pk_btn_alpha = float(values["btn_alpha"])
        if "btn_round" in values:
            p.pk_btn_round = float(values["btn_round"])
        if "bg_darken" in values:
            p.pk_bg_darken = float(values["bg_darken"])
    return picker_status()


def _require_tab(index=None):
    """The tab at `index`, or the active one. Raises with a plain reason."""
    context = bpy.context
    tabs = context.scene.madi_picker_tabs
    if not len(tabs):
        raise RuntimeError("No picker tabs yet - add one first.")
    if index is None:
        index = context.scene.madi_picker_tab_index
    if not 0 <= index < len(tabs):
        raise IndexError("No picker tab at index %d" % index)
    return tabs[index]


def picker_set_tab(index):
    context = bpy.context
    tabs = context.scene.madi_picker_tabs
    if not 0 <= index < len(tabs):
        raise IndexError("No picker tab at index %d" % index)
    # Assigning the index fires _on_tab_index_update, which swaps the
    # background in - the same path the Blender panel's buttons take.
    context.scene.madi_picker_tab_index = index
    _tag_redraw(context)
    status = picker_status()
    status["selected"] = index
    return status


def picker_add_tab(name=None):
    """Add a picker page. Deliberately the same two-case shape as
    `MADI_OT_picker_add_tab`, so the app and the Blender button cannot behave
    differently: on a fresh or pre-tabs scene the MIGRATED tab is the one the
    user asked for, and handing them an empty second one would be wrong."""
    context = bpy.context
    scene = context.scene
    tabs = scene.madi_picker_tabs
    if not len(tabs):
        tab = _ensure_tabs(context)      # deliberate: this IS the create
    else:
        tab = tabs.add()
        tab.uid = _new_tab_uid(context)
        arm, _bone = _active_bone_and_arm(context)
        tab.armature = arm
        tab.name = arm.name if arm else "Picker %d" % len(tabs)
        scene.madi_picker_tab_index = len(tabs) - 1
    if name:
        tab.name = name
    _tag_redraw(context)
    status = picker_status()
    status["added"] = tab.name
    return status


def picker_remove_tab(index=None):
    context = bpy.context
    scene = context.scene
    tabs = scene.madi_picker_tabs
    _require_tab(index)                  # bounds + "no tabs yet", with a reason
    real = scene.madi_picker_tab_index if index is None else index
    if len(tabs) <= 1:
        raise RuntimeError("Keep at least one picker tab.")
    name = tabs[real].name
    # ⚠ Read the uid as a plain int FIRST - `tabs[real]` is freed by remove(),
    # and reading .uid off it afterwards is a use-after-free. Purge by uid
    # across every armature, not via tab.armature: an empty pointer (rig
    # deleted, or the field re-pointed first) used to strand the buttons.
    uid = tabs[real].uid
    removed = _purge_tab_buttons(uid)
    tabs.remove(real)
    scene.madi_picker_tab_index = min(real, len(tabs) - 1)
    _state["hover_idx"] = -1
    _tag_redraw(context)
    status = picker_status()
    status["deleted"] = name
    status["buttons_removed"] = removed
    return status


def picker_rename_tab(name, index=None):
    tab = _require_tab(index)
    old = tab.name
    tab.name = name
    _tag_redraw(bpy.context)
    status = picker_status()
    status["renamed"] = [old, name]
    return status


def picker_set_tab_rig(object_name, index=None):
    tab = _require_tab(index)
    obj = bpy.data.objects.get(object_name) if object_name else None
    if object_name and obj is None:
        raise KeyError("No object called %r" % object_name)
    if obj is not None and obj.type != 'ARMATURE':
        raise RuntimeError("%r is not an armature." % object_name)
    tab.armature = obj
    _tag_redraw(bpy.context)
    status = picker_status()
    status["rig"] = obj.name if obj else None
    return status


def picker_set_tab_image(image_name, index=None):
    tab = _require_tab(index)
    img = bpy.data.images.get(image_name) if image_name else None
    if image_name and img is None:
        raise KeyError("No image called %r" % image_name)
    tab.image = img            # its update pushes it to the editors if running
    _tag_redraw(bpy.context)
    status = picker_status()
    status["image"] = img.name if img else None
    return status


def picker_set_button(index, **fields):
    """Edit one button. Only the fields actually passed are touched, so the app
    can send just what the user changed (the same rule the sliders follow)."""
    context = bpy.context
    arm = _picker_arm(context)
    if arm is None:
        raise RuntimeError("This picker tab has no rig.")
    coll = arm.madi_picker.buttons
    if not 0 <= index < len(coll):
        raise IndexError("No button at index %d" % index)
    btn = coll[index]
    if "label" in fields:
        btn.label = str(fields["label"])
    if "blank" in fields:
        btn.blank = bool(fields["blank"])
    if "scale" in fields and fields["scale"] is not None:
        btn.scale = float(fields["scale"])
    if fields.get("color") is not None:
        col = fields["color"]
        btn.color = (float(col[0]), float(col[1]), float(col[2]))
    if "bone" in fields and fields["bone"] is not None:
        btn.bone = str(fields["bone"])
    if "sk_object" in fields and fields["sk_object"] is not None:
        btn.sk_object = str(fields["sk_object"])
    if "sk_key" in fields and fields["sk_key"] is not None:
        btn.sk_key = str(fields["sk_key"])
    # Retargeting one member of a GROUP, by position in its member list.
    member = fields.get("member_index")
    if member is not None and fields.get("member_bone") is not None:
        if not 0 <= member < len(btn.members):
            raise IndexError("No group member at index %d" % member)
        btn.members[member].bone = str(fields["member_bone"])
    _tag_redraw(context)
    status = picker_status()
    status["edited"] = index
    return status


def picker_remove_buttons(indices):
    context = bpy.context
    arm = _picker_arm(context)
    if arm is None:
        raise RuntimeError("This picker tab has no rig.")
    coll = arm.madi_picker.buttons
    wanted = sorted({int(i) for i in (indices or [])
                     if 0 <= int(i) < len(coll)}, reverse=True)
    if not wanted:
        raise IndexError("Nothing to remove.")
    _remove_buttons(context, arm, wanted)
    _tag_redraw(context)
    status = picker_status()
    status["deleted"] = len(wanted)
    return status


def picker_set_brushes(**fields):
    """The live brushes (colour / Button Scale / Align Gap / no-label).

    ⚠ Writing these re-enters their own `update=` callbacks, which apply to the
    SELECTION - that is what they are for, and it is why the picker guards its
    own programmatic writes with `suppress_apply` (gotcha 14). Colour and scale
    are MEANT to reach the selection when a user drags them, so they are written
    plainly here; only `blank` is a plain setting."""
    scene = bpy.context.scene
    if fields.get("color") is not None:
        col = fields["color"]
        scene.madi_picker_color = (float(col[0]), float(col[1]), float(col[2]))
    if fields.get("scale") is not None:
        scene.madi_picker_scale = float(fields["scale"])
    if fields.get("gap") is not None:
        scene.madi_picker_gap = float(fields["gap"])
    if "blank" in fields and fields["blank"] is not None:
        scene.madi_picker_blank = bool(fields["blank"])
    _tag_redraw(bpy.context)
    return picker_status()


def _image_editor_override():
    """(window, area, region) of the first Image Editor on screen, or None.

    The picker's session is a MODAL operator living in the Image Editor, so
    starting it from the bridge - which runs on a timer with no area of its own
    - needs an explicit override."""
    wm = bpy.context.window_manager
    for window in wm.windows:
        for area in window.screen.areas:
            if area.type != 'IMAGE_EDITOR':
                continue
            for region in area.regions:
                if region.type == 'WINDOW':
                    return window, area, region
    return None


def picker_start():
    """Start a session from the app.

    ⚠ THE ONE COMMAND HERE THAT CANNOT BE PROVEN HEADLESS. `blender -b` has no
    window manager modal loop, so a test can only reach the refusal path. It is
    verified by hand in a real session; if it ever misbehaves, the Blender-side
    Start Picker button is the unchanged path that always works.
    """
    if _state["running"]:
        return picker_status()
    if not len(bpy.context.scene.madi_picker_tabs):
        raise RuntimeError("No picker tabs yet - add one first.")
    override = _image_editor_override()
    if override is None:
        raise RuntimeError(
            "Open an Image Editor in Blender first - the picker draws in it.")
    window, area, region = override
    with bpy.context.temp_override(window=window, area=area, region=region):
        bpy.ops.madi_picker.session('INVOKE_DEFAULT')
    return picker_status()


def picker_stop():
    if _state["running"]:
        override = _image_editor_override()
        if override is None:
            # No editor to override with, but the session still has to end.
            _state["running"] = False
            _clear_place()
            _restore_session_images(bpy.context)
            _tag_redraw(bpy.context)
        else:
            window, area, region = override
            with bpy.context.temp_override(window=window, area=area,
                                           region=region):
                bpy.ops.madi_picker.stop()
    return picker_status()


# ---------------------------------------------------------------------------
# Studio Library items (.picker)
# ---------------------------------------------------------------------------
# Marty asked for the picker's presets to become library items rather than
# loose .json files, so a layout browses with a picture like a pose does.
#
# The payload is the EXISTING v6 preset dict, unchanged - so a .json he already
# has converts by being dropped in as `<name>.picker\picker.json`, and a library
# item can still be read by anything that understood the old presets.
#
# ⚠ THE THUMBNAIL IS THE REFERENCE IMAGE, not a viewport render. `capture_preview`
# renders the 3D viewport, which for a picker layout would be a picture of the
# character rather than of the thing being saved - and a layout traced over a
# reference is meaningless without it.
#
# ⚠ Studio Library is FREE, so these items are visible to someone with no
# licence. That is deliberate: they are the user's own files, and hiding them
# would make the library look different from one machine to the next. APPLYING
# one is gated (server.py refuses every picker_* write), so the item shows, and
# says why it will not load.


def _picker_thumbnail(image, out_path, size=256):
    """Write the tab's reference picture as the item's thumbnail. Best effort -
    a missing preview is cosmetic, and must never stop the layout being saved."""
    if image is None:
        return False
    try:
        scaled = image.copy()
        try:
            scaled.scale(size, size)
            scaled.filepath_raw = out_path
            scaled.file_format = 'JPEG'
            scaled.save()
            return True
        finally:
            bpy.data.images.remove(scaled)
    except (RuntimeError, ValueError, OSError, AttributeError):
        return False


def picker_save_item(library_root, folder, name, overwrite=False):
    """Save the ACTIVE tab's layout as a `.picker` library item."""
    context = bpy.context
    arm = _picker_arm(context)
    if arm is None:
        raise RuntimeError("This picker tab has no rig to save a layout from.")
    tab = _require_tab()
    preset = _preset_from_arm(context, arm)
    if not preset.get("buttons"):
        raise RuntimeError("This picker tab has no buttons yet.")

    item_dir = os.path.join(library_root, folder or "",
                            core.safe_name(name) + core.PICKER_EXT)
    if os.path.isdir(item_dir):
        if not overwrite:
            raise RuntimeError("Item already exists: %s (use overwrite)"
                               % item_dir)
        core.version_item(item_dir)
    os.makedirs(item_dir, exist_ok=True)
    data = dict(preset)
    data["type"] = "picker"
    data["metadata"] = core._metadata(arm, {"tab": tab.name,
                                            "buttons": len(preset["buttons"])})
    with open(os.path.join(item_dir, "picker.json"), "w",
              encoding="utf-8") as fh:
        json.dump(data, fh, indent=1)
    thumb = _picker_thumbnail(tab.image if tab else None,
                              os.path.join(item_dir, "thumbnail.jpg"))
    # ⚠ Answers with the WHOLE STATUS, like every other picker command, with the
    # save result under keys of its own. The app broadcasts each reply to every
    # tool, so a command that answered with a bare {path, buttons} dict would
    # look like a status with no tabs and blank the tab list.
    status = picker_status()
    status["saved_path"] = item_dir
    status["saved_buttons"] = len(preset["buttons"])
    status["saved_thumbnail"] = thumb
    return status


def picker_apply_item(item_path, replace=True):
    """Load a `.picker` item into the ACTIVE tab. -> the whole status."""
    context = bpy.context
    arm = _picker_arm(context)
    if arm is None:
        raise RuntimeError("Pick a rig for this picker tab first.")
    path = item_path
    if os.path.isdir(path):
        path = os.path.join(path, "picker.json")
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if data.get("format") != "madi_picker_preset":
        raise RuntimeError("Not a picker layout: %s" % item_path)
    added, missing = _preset_to_arm(context, arm, data, replace)
    _tag_redraw(context)
    status = picker_status()
    status["added"] = added
    status["missing"] = missing
    return status


def _reflow_v11(aspect=1.0):
    """One-time: bake the pre-v0.11.0 label floor into `w`/`h`. -> n reflowed.

    Up to v0.10.0 a label could FLOOR its button bigger than its stored size
    ("the button grows, the text never shrinks"). v0.11.0 dropped that - a button
    is exactly the size you made it and the label is fitted inside it - so every
    existing layout would otherwise collapse to sizes it was never drawn at.
    Measured on Marty's file: a "chest" button drawn 0.0211 wide would have come
    back 0.008. So bake what each button was ACTUALLY DRAWN at into w/h, and the
    layout survives the update looking identical.

    Appearance-preserving, not value-preserving - the same principle the v0.7.0
    scale migration used.

    The old per-button text size is still in the button's IDProperties even
    though the RNA property is gone (PropertyGroup elements are IDProperty-backed
    - `btn.get("font")` reads it; note this does NOT work for props registered on
    an ID, see gotcha 19). Deleting the key is what makes this run exactly once,
    and re-running it anyway is harmless: the floor is a max(), so a second pass
    computes the same number and changes nothing."""
    n = 0
    for obj in bpy.data.objects:
        if obj.type != 'ARMATURE':
            continue
        for btn in obj.madi_picker.buttons:
            raw = btn.get("font")
            if raw is None:
                continue                  # created under v0.11.0+, or done already
            try:
                del btn["font"]
            except (KeyError, TypeError):
                pass
            text = _btn_text(btn)         # a blank button never had a floor
            font = (raw if raw > 0.0 else _LEGACY_FONT) * btn.scale
            if not text or font <= 0.0:
                continue
            # the old _min_wh, kept here verbatim (with the OLD padding
            # fractions) because nothing else needs it
            wf, hf = _LEGACY_TEXT_W_FRAC, _LEGACY_TEXT_H_FRAC
            if btn.kind == 'GROUP':
                wf, hf = wf * GROUP_INSCRIBE, hf * GROUP_INSCRIBE
            need_w = font * _label_width_ratio(text) * aspect / wf
            need_h = font / hf
            if btn.kind == 'GROUP':
                px = max(need_w, need_h * aspect)
                need_w, need_h = px, px / aspect
            scale = btn.scale or 1.0
            w = min(5.0, max(btn.w, min(5.0, need_w) / scale))
            h = min(5.0, max(btn.h, min(5.0, need_h) / scale))
            if w != btn.w or h != btn.h:
                btn.w, btn.h = w, h
                # the size it was "placed at" is the size it has always LOOKED,
                # or Reset Size would offer to shrink it to a size no one saw
                btn.w0, btn.h0, btn.scale0 = w, h, btn.scale
                n += 1
    return n


def _frame_dependent(context):
    """Does anything currently ON SCREEN change with the frame?

    Only SLIDERs do: their fill follows the shape key's value and their pip
    follows whether this frame is keyed. A tab of BONE buttons draws the same
    picture on every frame of the timeline.
    """
    arm = _target(context)
    if not arm:
        return False
    for _i, b in _iter_buttons(context, arm):
        if b.kind == 'SLIDER':
            return True
    return False


@persistent
def _on_frame_change(_scene):
    """Keep SLIDER fills honest while the animation plays or the timeline is
    scrubbed (v0.19.0): a frame change moves shape key values, but nothing else
    tags the Image Editor for redraw. Early-outs to a dict read when the picker
    isn't running.

    ⚠ AND IT ASKS WHETHER THERE IS ANYTHING TO REDRAW FOR (v0.24.2, perf). This
    used to tag unconditionally, so PLAYING BACK an animation rebuilt the whole
    picker once per frame - every button's ring re-tessellated, both vertex
    buffers re-uploaded and one text draw per label - to produce an identical
    picture. On a bone-only tab (no sliders, which is most of them) that is the
    entire cost for no change at all, paid at playback frame rate on Blender's
    main thread.

    Measured on the python half alone: ~1 ms per redraw at 155 buttons, ~3.5 ms
    at 400, plus the GPU upload and the per-label text draws on top - so at 24
    fps this was reclaiming a real slice of playback. The scan that decides is
    one pass over the active tab and only runs while the picker is up.
    """
    if not _state["running"]:
        return
    context = bpy.context
    if not _frame_dependent(context):
        return
    _tag_redraw(context)


@persistent
def _on_load_post(_dummy):
    """Migrate a freshly loaded file: give every scene its default tab, fold the
    old scene-wide button scale into the buttons themselves, and bake the old
    label floor into their sizes (_reflow_v11).

    This can't live in the Panel (drawing may not write to ID data) and mustn't
    wait for the first operator, or a pre-tabs layout would sit there looking
    tab-less."""
    try:
        for scene in bpy.data.scenes:
            if not len(scene.madi_picker_tabs):
                tab = scene.madi_picker_tabs.add()
                tab.uid = 0
                legacy = scene.madi_picker_target
                tab.armature = legacy
                tab.name = legacy.name if legacy else "Picker 1"
                scene.madi_picker_tab_index = 0
            # Button Scale used to be scene-wide. Write it onto every button that
            # predates the change, so the slider stops being global the moment it
            # goes per button. (The old per-button `font` is simply gone as of
            # v0.11.0 - labels are sized from their button now - so there is no
            # text migration left to do.)
            old_scale = scene.madi_picker_scale
            for obj in bpy.data.objects:
                if obj.type != 'ARMATURE':
                    continue
                for btn in obj.madi_picker.buttons:
                    if old_scale != 1.0 and btn.scale == 1.0:
                        btn.scale = old_scale
                    # Buttons that predate Reset Size have no as-placed size.
                    # Seed it from where they are NOW, not from DEFAULT_SCALE: a
                    # layout built at 0.1 must not offer to "reset" itself to a
                    # 0.04 it was never at. Idempotent - it only ever fills a
                    # zero, so unlike the scale fold above (gotcha 20) re-running
                    # this consumes nothing.
                    if btn.w0 <= 0.0:
                        btn.w0, btn.h0, btn.scale0 = btn.w, btn.h, btn.scale
            if old_scale != 1.0:
                # it's a brush now, not a global - and writing a brush applies it
                # to the selection, which is the last thing a migration wants
                _state["suppress_apply"] = True
                try:
                    scene.madi_picker_scale = 1.0
                finally:
                    _state["suppress_apply"] = False
        # AFTER the scale fold: the old label floor was computed against the
        # button's final scale, so reflowing first would bake the wrong number
        region = _picker_region(bpy.context)
        _reflow_v11(_view_aspect(region) if region else 1.0)
        # a freshly loaded file's sliders are the baseline for the next drag
        _remember_brushes(bpy.context.scene)
    except Exception:
        import traceback
        traceback.print_exc()


_HANDLERS = (
    ("load_post", _on_load_post),
    ("frame_change_post", _on_frame_change),
)


def _strip_stale_handlers():
    """Drop handlers left behind by a PREVIOUS load of this module.

    The dev reload purges `sys.modules`, so the reloaded module's functions are
    DIFFERENT OBJECTS from the ones still sitting in `bpy.app.handlers` - an
    identity check cannot find them, and the old ones keep firing against a dead
    module, `_on_frame_change` once per frame. Match on the qualified name
    instead. Same fix `jiggle.py` needed (docs\\addon-bridge.md); the picker is
    the second module to need it, which is why this is a copy of a known-good
    shape rather than a fresh idea.
    """
    ours = {fn.__name__ for _name, fn in _HANDLERS}
    for name, _fn in _HANDLERS:
        handlers = getattr(bpy.app.handlers, name)
        for existing in list(handlers):
            if (getattr(existing, "__name__", None) in ours
                    and getattr(existing, "__module__", "").endswith("picker")):
                handlers.remove(existing)


def register():
    for c in _classes:
        bpy.utils.register_class(c)
    bpy.types.Object.madi_picker = PointerProperty(type=MADI_PickerData)
    # kept registered purely so pre-tabs files can hand their rig to the first
    # tab; the UI doesn't show it any more
    bpy.types.Scene.madi_picker_target = PointerProperty(
        type=bpy.types.Object, name="Armature",
        poll=lambda self, obj: obj.type == 'ARMATURE')
    bpy.types.Scene.madi_picker_tabs = CollectionProperty(type=MADI_PickerTab)
    bpy.types.Scene.madi_picker_tab_index = IntProperty(
        name="Active Tab", default=0, update=_on_tab_index_update)
    # monotonic high-water mark for tab uids - see _new_tab_uid. Not shown in
    # the UI; it exists so a removed tab's uid can never be handed out again.
    bpy.types.Scene.madi_picker_uid_next = IntProperty(
        name="Next Tab UID", default=0, options={'HIDDEN'})
    bpy.types.Scene.madi_picker_color = FloatVectorProperty(
        name="Button Color", subtype='COLOR', size=3, min=0.0, max=1.0,
        default=DEFAULT_COLOR,
        description="Applies live to the selected buttons; with nothing "
                    "selected it's the color new buttons get",
        update=_on_color_update)
    bpy.types.Scene.madi_picker_gap = FloatProperty(
        name="Align Gap", default=ALIGN_GAP, min=0.0, max=3.0, soft_max=1.0,
        precision=2, step=5.0,
        description="Space Align leaves between buttons, as a fraction of their "
                    "own drawn size - 0.25 is a quarter of a button, 0 lets them "
                    "touch. LIVE: once you've aligned something, dragging this "
                    "re-aligns it at the new gap (as long as it's still the "
                    "selection)",
        update=_on_gap_update)
    bpy.types.Scene.madi_picker_blank = BoolProperty(
        name="Blank Buttons", default=False,
        description="New buttons are added with no text on them - the bone is "
                    "still mapped and the label is still stored, it just isn't "
                    "drawn. Only affects buttons added from now on; un-tick a "
                    "single button's text icon in the Buttons list to bring its "
                    "label back")
    # NOTE: there is no madi_picker_font any more (v0.11.0) - labels are sized
    # from the button they sit in, so Button Scale is the only size dial.
    bpy.types.Scene.madi_picker_scale = FloatProperty(
        name="Button Scale", default=DEFAULT_BTN_SCALE, min=0.05, max=20.0,
        soft_min=0.2, soft_max=4.0, precision=2, step=5.0,
        description="Resize the SELECTED buttons (with nothing selected, what "
                    "new buttons get). Labels are sized from their button, so "
                    "this scales the writing with the box - and a labelled "
                    "button's height follows its text, so scaling up doesn't "
                    "grow empty space above and below it. Reads the button you "
                    "last clicked; dragging it scales the whole selection BY THE "
                    "SAME RATIO, so a mixed selection keeps its relative sizes. "
                    "Positions are untouched",
        update=_on_scale_update)
    # ⚠ Strip FIRST. The `not in` tests these lines used to do could never see a
    # handler left by a previous load - it is a different function object with
    # the same name (see _strip_stale_handlers).
    _strip_stale_handlers()
    for _name, _fn in _HANDLERS:
        _handlers = getattr(bpy.app.handlers, _name)
        if _fn not in _handlers:
            _handlers.append(_fn)
    # Try to baseline the relative size dials against what the sliders already
    # show. During register() BOTH bpy.context and bpy.data are restricted
    # (`_RestrictContext` / `_RestrictData` - the latter has no `.scenes` at all
    # and raises AttributeError), so this usually finds nothing. That's fine:
    # the dials self-baseline on their first write instead, costing one drag
    # increment and never a wrong-sized jump. Don't "improve" this with
    # bpy.data - it throws and takes the whole add-on's registration with it.
    scene = getattr(bpy.context, "scene", None)
    if scene is not None:
        _remember_brushes(scene)
    # the handler lives for the whole add-on lifetime (a modal crash can't
    # orphan it) but the callback only DRAWS while the picker runs (job 26)
    _enable_draw()


def unregister():
    _state["running"] = False
    # HAND THE EDITORS BACK before dropping the snapshot. This used to just
    # clear `saved_images`, which was fine when disabling the add-on was a rare,
    # deliberate act - but inside the Toolset extension a reload is ROUTINE (the
    # dev reload procedure, and `addon_update` reloading after a self-update), so
    # discarding the snapshot would leave the user's Image Editors showing a
    # picker background they never opened, with no way back. Best effort: during
    # unregister the context can be restricted or screenless.
    try:
        _restore_session_images(bpy.context)
    except (AttributeError, ReferenceError, TypeError):
        _state["saved_images"] = {}
    _clear_place()          # _state survives a re-register; a ghost must not
    _state["align_run"] = None      # ...and neither must a stale align run
    _disable_draw()
    _ratio_cache.clear()
    _cap_cache.clear()
    _shaders.clear()
    _strip_stale_handlers()
    for _name, _fn in _HANDLERS:
        _handlers = getattr(bpy.app.handlers, _name)
        while _fn in _handlers:
            _handlers.remove(_fn)
    del bpy.types.Scene.madi_picker_scale
    del bpy.types.Scene.madi_picker_blank
    del bpy.types.Scene.madi_picker_gap
    del bpy.types.Scene.madi_picker_color
    del bpy.types.Scene.madi_picker_uid_next
    del bpy.types.Scene.madi_picker_tab_index
    del bpy.types.Scene.madi_picker_tabs
    del bpy.types.Scene.madi_picker_target
    del bpy.types.Object.madi_picker
    for c in reversed(_classes):
        try:
            bpy.utils.unregister_class(c)
        except RuntimeError:
            pass        # already gone (a half-finished register, or a reload)
