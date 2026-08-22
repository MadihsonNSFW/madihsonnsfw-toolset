"""Item grid: icon-mode list with thumbnails / generated placeholders.

Perf notes (rebuilt 2026-08-15, PERF_PLAN.md F2 — measured at 800 items):
- ⚠ THE DISK IS TOUCHED IN EXACTLY ONE PLACE: `_decode_file`. Every decoded
  file lands in `_source_cache` at ≤256 px (the zoom slider's own maximum and
  the size capture writes anyway), and every tile of every size is DERIVED
  from that in RAM. Before this, zooming re-decoded every thumbnail from disk
  at each size — 452 ms cold and 440 ms "warm", because…
- ⚠ …EVERY CACHE HERE IS CAPPED IN BYTES, NOT ENTRIES. The old 512-entry cap
  thrashed the moment a library passed 512 items (each zoom evicted every
  other size), and 512 entries of 220 px tiles was quietly 95 MB.
- ⚠ THE GRID DECODES LAZILY, VISIBLE-FIRST: set_items/set_icon_size put up a
  cached tile or a placeholder, decode what the viewport shows synchronously
  (or the first 64 rows when the widget has no size yet — which is what keeps
  every offscreen test fully synchronous), and drain the rest through a
  0 ms timer in ~8 ms slices. `thumbnail_pixmap` itself stays synchronous for
  its direct callers.
- hover sequences lazy-load per frame into a small LRU (_seq_cache) so playback
  after the first loop costs no disk I/O at all
"""

import math
import os
import time
from collections import OrderedDict

from PySide6.QtCore import Qt, QMimeData, QSize, QTimer, Signal
from PySide6.QtGui import (QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap,
                           QFont, QPolygonF)
from PySide6.QtCore import QPointF, QRectF
from PySide6.QtWidgets import QListWidget, QListWidgetItem, QListView

import theme
import video_preview

# drag-drop payload: newline-joined absolute item folder paths
ITEM_MIME = "application/x-madi-library-items"

_placeholder_cache = OrderedDict()  # (type, name, size, color, bulk, flags) -> QPixmap
_thumb_cache = OrderedDict()   # (path, mtime, size, type, color, bulk, flags) -> stamped tile
_type_icon_cache = {}
_seq_cache = OrderedDict()     # item_path -> {"sig", "files", "scaled": {size: [pm|None]}}
_bytes_cache = OrderedDict()   # (path, mtime) -> the thumbnail FILE's bytes
_source_cache = OrderedDict()  # (path, mtime) -> decoded ≤256 px QPixmap (hot set)

_SOURCE_PX = 256           # the zoom slider tops out at 256 and capture writes 256
# ⚠ TWO LAYERS, because decoded pixels do not fit: 800 sources at 256 px are
# ~200 MB decoded, and a 64 MB decoded-only cache SCAN-THRASHED — a zoom walk
# missed on every single row and re-read all 800 files from disk (measured:
# 1,600 decodes for one zoom cycle). The jpg's own bytes are ~20 KB, so the
# BYTES layer holds a whole big library (~16 MB / 800 items) and "warm" means
# no disk ever; the small DECODED layer just keeps the hot set from paying the
# ~0.6 ms re-decode.
_BYTES_CACHE_MB = 64.0     # compressed files — the layer that saves the disk
_SOURCE_CACHE_MB = 24.0    # decoded originals — the layer that saves the CPU
_TILE_CACHE_MB = 32.0      # stamped per-size tiles — derived, cheap to rebuild
_PLACEHOLDER_CACHE_MB = 8.0
_SEQ_CACHE_MAX = 12

_bytes_bytes = 0
_source_bytes = 0
_tile_bytes = 0
_placeholder_bytes = 0


def _pm_bytes(pm):
    return pm.width() * pm.height() * 4


def _decode_file(path):
    """⚠ The ONLY road from the disk in this module — returns the file's raw
    bytes (None when unreadable). The F2 bench ceilings count calls here,
    which is what stops an eager read quietly coming back."""
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return None


def _source_pixmap(thumb, mtime):
    """The decoded original (≤256 px): decoded layer, bytes layer, then disk."""
    global _bytes_bytes, _source_bytes
    key = (thumb, mtime)
    pm = _source_cache.get(key)
    if pm is not None:
        _source_cache.move_to_end(key)
        return pm
    raw = _bytes_cache.get(key)
    if raw is not None:
        _bytes_cache.move_to_end(key)
    else:
        raw = _decode_file(thumb)
        if raw is None:
            return None
        _bytes_cache[key] = raw
        _bytes_bytes += len(raw)
        # ⚠ len() > 1 so one oversized entry can never evict itself on arrival.
        while _bytes_bytes > _BYTES_CACHE_MB * 1048576 and len(_bytes_cache) > 1:
            _k, _old = _bytes_cache.popitem(last=False)
            _bytes_bytes -= len(_old)
    pm = QPixmap()
    if not pm.loadFromData(raw):
        return None
    if pm.width() > _SOURCE_PX or pm.height() > _SOURCE_PX:
        pm = pm.scaled(_SOURCE_PX, _SOURCE_PX, Qt.KeepAspectRatioByExpanding,
                       Qt.SmoothTransformation)
    _source_cache[key] = pm
    _source_bytes += _pm_bytes(pm)
    while _source_bytes > _SOURCE_CACHE_MB * 1048576 and len(_source_cache) > 1:
        _k, _old = _source_cache.popitem(last=False)
        _source_bytes -= _pm_bytes(_old)
    return pm


# ---------------------------------------------------------------- type icons

def _draw_type_glyph(p, typ, rect, color):
    """Small flat glyph: pose = still photo, anim = play, set = cursor."""
    c = QColor(color)
    p.setRenderHint(QPainter.Antialiasing)
    x, y, s = rect.x(), rect.y(), rect.height()
    if typ == "anim":
        p.setPen(Qt.NoPen)
        p.setBrush(c)
        tri = QPolygonF([QPointF(x + s * 0.30, y + s * 0.20),
                         QPointF(x + s * 0.30, y + s * 0.80),
                         QPointF(x + s * 0.85, y + s * 0.50)])
        p.drawPolygon(tri)
    elif typ == "pose":
        pen = QPen(c, max(1.2, s * 0.12))
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(QRectF(x + s * 0.14, y + s * 0.18, s * 0.72, s * 0.64),
                          s * 0.1, s * 0.1)
        p.setPen(Qt.NoPen)
        p.setBrush(c)
        p.drawEllipse(QPointF(x + s * 0.38, y + s * 0.42), s * 0.09, s * 0.09)
        mount = QPolygonF([QPointF(x + s * 0.22, y + s * 0.74),
                           QPointF(x + s * 0.48, y + s * 0.48),
                           QPointF(x + s * 0.64, y + s * 0.64),
                           QPointF(x + s * 0.76, y + s * 0.52),
                           QPointF(x + s * 0.78, y + s * 0.74)])
        p.drawPolygon(mount)
    elif typ == "set":
        p.setPen(Qt.NoPen)
        p.setBrush(c)
        cur = QPolygonF([QPointF(x + s * 0.30, y + s * 0.15),
                         QPointF(x + s * 0.30, y + s * 0.75),
                         QPointF(x + s * 0.45, y + s * 0.60),
                         QPointF(x + s * 0.55, y + s * 0.85),
                         QPointF(x + s * 0.64, y + s * 0.80),
                         QPointF(x + s * 0.54, y + s * 0.56),
                         QPointF(x + s * 0.72, y + s * 0.55)])
        p.drawPolygon(cur)
    elif typ == "vgroups":
        # a weight ramp: three bars of rising height = painted influence
        p.setPen(Qt.NoPen)
        p.setBrush(c)
        for n, h in enumerate((0.28, 0.48, 0.68)):
            p.drawRect(QRectF(x + s * (0.20 + n * 0.22), y + s * (0.82 - h),
                              s * 0.14, s * h))
    elif typ == "picker":
        # a picker board: outline with three buttons on it. Drawn because the
        # type reached the sidebar filters and a blank icon reads as a bug.
        pen = QPen(c, max(1.0, s * 0.08))
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(QRectF(x + s * 0.14, y + s * 0.14, s * 0.72, s * 0.72),
                          s * 0.1, s * 0.1)
        p.setPen(Qt.NoPen)
        p.setBrush(c)
        for fx, fy in ((0.36, 0.34), (0.64, 0.34), (0.50, 0.64)):
            p.drawEllipse(QPointF(x + s * fx, y + s * fy), s * 0.09, s * 0.09)
    elif typ == "renderpreset":
        # a camera aperture: ring plus three blades = render settings
        pen = QPen(c, max(1.0, s * 0.09))
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        cx, cy, r = x + s * 0.5, y + s * 0.5, s * 0.34
        p.drawEllipse(QPointF(cx, cy), r, r)
        for ang in (90.0, 210.0, 330.0):
            rad = math.radians(ang)
            p.drawLine(QPointF(cx + math.cos(rad) * r * 0.96,
                               cy - math.sin(rad) * r * 0.96),
                       QPointF(cx + math.cos(rad - 1.15) * r * 0.30,
                               cy - math.sin(rad - 1.15) * r * 0.30))
    elif typ == "shapes":
        # three slider rows = shape key panel
        pen = QPen(c, max(1.1, s * 0.09))
        p.setPen(pen)
        knobs = ((0.25, 0.62), (0.50, 0.32), (0.75, 0.72))
        for fy, _fx in knobs:
            p.drawLine(QPointF(x + s * 0.14, y + s * fy),
                       QPointF(x + s * 0.86, y + s * fy))
        p.setPen(Qt.NoPen)
        p.setBrush(c)
        for fy, fx in knobs:
            p.drawEllipse(QPointF(x + s * fx, y + s * fy), s * 0.10, s * 0.10)
    elif typ == "remap":
        # two bone columns with an arrow across = rig-to-rig transfer
        p.setPen(Qt.NoPen)
        p.setBrush(c)
        for fy in (0.22, 0.50, 0.78):
            p.drawEllipse(QPointF(x + s * 0.16, y + s * fy), s * 0.09, s * 0.09)
            p.drawEllipse(QPointF(x + s * 0.84, y + s * fy), s * 0.09, s * 0.09)
        pen = QPen(c, max(1.1, s * 0.09))
        p.setPen(pen)
        p.drawLine(QPointF(x + s * 0.32, y + s * 0.50),
                   QPointF(x + s * 0.66, y + s * 0.50))
        p.setPen(Qt.NoPen)
        p.drawPolygon(QPolygonF([QPointF(x + s * 0.58, y + s * 0.36),
                                 QPointF(x + s * 0.58, y + s * 0.64),
                                 QPointF(x + s * 0.72, y + s * 0.50)]))
    elif typ == "abc":
        # cache cylinder = alembic point/mesh cache
        pen = QPen(c, max(1.1, s * 0.09))
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        rx, ry = s * 0.30, s * 0.11
        cx = x + s * 0.5
        p.drawEllipse(QPointF(cx, y + s * 0.24), rx, ry)
        p.drawLine(QPointF(cx - rx, y + s * 0.24), QPointF(cx - rx, y + s * 0.74))
        p.drawLine(QPointF(cx + rx, y + s * 0.24), QPointF(cx + rx, y + s * 0.74))
        p.drawArc(QRectF(cx - rx, y + s * 0.74 - ry, rx * 2, ry * 2),
                  180 * 16, 180 * 16)
        p.drawArc(QRectF(cx - rx, y + s * 0.49 - ry, rx * 2, ry * 2),
                  180 * 16, 180 * 16)
    elif typ == "playblast":
        # clapperboard: angled slate over the board
        p.setPen(Qt.NoPen)
        p.setBrush(c)
        p.drawRoundedRect(QRectF(x + s * 0.12, y + s * 0.42, s * 0.76, s * 0.40),
                          s * 0.06, s * 0.06)
        p.drawPolygon(QPolygonF([QPointF(x + s * 0.14, y + s * 0.36),
                                 QPointF(x + s * 0.82, y + s * 0.14),
                                 QPointF(x + s * 0.88, y + s * 0.26),
                                 QPointF(x + s * 0.20, y + s * 0.48)]))
    elif typ == "mirror":
        # butterfly: two triangles facing a center line
        p.setPen(Qt.NoPen)
        p.setBrush(c)
        p.drawPolygon(QPolygonF([QPointF(x + s * 0.10, y + s * 0.25),
                                 QPointF(x + s * 0.42, y + s * 0.50),
                                 QPointF(x + s * 0.10, y + s * 0.75)]))
        p.drawPolygon(QPolygonF([QPointF(x + s * 0.90, y + s * 0.25),
                                 QPointF(x + s * 0.58, y + s * 0.50),
                                 QPointF(x + s * 0.90, y + s * 0.75)]))
        pen = QPen(c, max(1.0, s * 0.08))
        pen.setStyle(Qt.DotLine)
        p.setPen(pen)
        p.drawLine(QPointF(x + s * 0.5, y + s * 0.12), QPointF(x + s * 0.5, y + s * 0.88))
    # ---- Blender assets (2026-08-22). ⚠ A type with no branch here draws
    # NOTHING and the tile gets a blank badge — the glyph is not optional
    # decoration, it is the only thing on a tile that says what it is.
    elif typ == "object":
        # a cube seen slightly from above = a thing with geometry
        pen = QPen(c, max(1.1, s * 0.10))
        pen.setJoinStyle(Qt.RoundJoin)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        p.drawPolygon(QPolygonF([QPointF(x + s * 0.50, y + s * 0.14),
                                 QPointF(x + s * 0.85, y + s * 0.32),
                                 QPointF(x + s * 0.50, y + s * 0.50),
                                 QPointF(x + s * 0.15, y + s * 0.32)]))
        for fx in (0.15, 0.85):
            p.drawLine(QPointF(x + s * fx, y + s * 0.32),
                       QPointF(x + s * fx, y + s * 0.68))
        p.drawLine(QPointF(x + s * 0.50, y + s * 0.50),
                   QPointF(x + s * 0.50, y + s * 0.86))
        p.drawLine(QPointF(x + s * 0.15, y + s * 0.68),
                   QPointF(x + s * 0.50, y + s * 0.86))
        p.drawLine(QPointF(x + s * 0.85, y + s * 0.68),
                   QPointF(x + s * 0.50, y + s * 0.86))
    elif typ == "collection":
        # Blender's own idea of a collection: a box holding boxes
        pen = QPen(c, max(1.1, s * 0.10))
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        p.drawRect(QRectF(x + s * 0.12, y + s * 0.20, s * 0.76, s * 0.62))
        p.setPen(Qt.NoPen)
        p.setBrush(c)
        for fx in (0.30, 0.50, 0.70):
            p.drawRect(QRectF(x + s * (fx - 0.07), y + s * 0.44,
                              s * 0.14, s * 0.22))
    elif typ == "material":
        # a shaded sphere with a specular dot = a surface
        p.setPen(Qt.NoPen)
        p.setBrush(c)
        p.drawEllipse(QRectF(x + s * 0.14, y + s * 0.14, s * 0.72, s * 0.72))
        p.setBrush(QColor(255, 255, 255, 190))
        p.drawEllipse(QPointF(x + s * 0.36, y + s * 0.34), s * 0.10, s * 0.10)
    elif typ == "nodegroup":
        # two nodes and a noodle
        pen = QPen(c, max(1.1, s * 0.10))
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(QRectF(x + s * 0.10, y + s * 0.16, s * 0.30, s * 0.30),
                          s * 0.06, s * 0.06)
        p.drawRoundedRect(QRectF(x + s * 0.58, y + s * 0.54, s * 0.30, s * 0.30),
                          s * 0.06, s * 0.06)
        p.drawLine(QPointF(x + s * 0.40, y + s * 0.31),
                   QPointF(x + s * 0.58, y + s * 0.69))


# Filter labels that are not just the type key. The keys stay short because
# they are the folder EXTENSION (`name.renderpreset`); only what the user reads
# gets spelled out (Marty, 2026-08-05: 'in studio library add "Render Presets"
# filter').
TYPE_LABELS = {"renderpreset": "render presets",
               # Blender assets — plural, to read as a filter rather than as
               # the name of one thing, the same as every other row.
               "object": "objects", "collection": "collections",
               "material": "materials", "nodegroup": "node groups"}


def type_label(typ):
    return TYPE_LABELS.get(typ, typ)


def type_icon(typ, size=16):
    """Standalone icon (sidebar filters etc.) in the item-type color."""
    key = (typ, size)
    if key in _type_icon_cache:
        return _type_icon_cache[key]
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    _draw_type_glyph(p, typ, QRectF(0, 0, size, size),
                     theme.TYPE_COLORS.get(typ, theme.ACCENT))
    p.end()
    icon = QIcon(pm)
    _type_icon_cache[key] = icon
    return icon


def _stamp_label(pm, color):
    """Color-label strip along the tile's bottom edge."""
    if not color:
        return pm
    h = max(4, int(pm.height() * 0.05))
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(color))
    p.drawRoundedRect(QRectF(2, pm.height() - h - 2, pm.width() - 4, h), 2, 2)
    p.end()
    return pm


def _stamp_badge(pm, typ):
    """Corner type badge on a tile pixmap (dark backing for readability)."""
    s = max(14, int(pm.height() * 0.16))
    pad = max(3, s // 4)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(Qt.NoPen)
    backing = QColor("#000000")
    backing.setAlpha(140)
    p.setBrush(backing)
    p.drawRoundedRect(QRectF(pad, pad, s + 4, s + 4), 3, 3)
    _draw_type_glyph(p, typ, QRectF(pad + 2, pad + 2, s, s),
                     theme.TYPE_COLORS.get(typ, theme.ACCENT))
    p.end()
    return pm


def _stamp_bulk(pm, count):
    """Top-RIGHT badge: a stack of sheets + how many are in it.

    Marty, 2026-08-05: "for bulk exports we need another icon near the thumbnail
    indicating it's a bulk export and not just one." Opposite corner from the
    type badge so the two never collide, and it carries the NUMBER as well as
    the glyph — "this holds several" and "this holds forty" are different
    enough to matter when you are deciding what to load.
    """
    if count < 2:
        return pm
    s = max(14, int(pm.height() * 0.16))
    pad = max(3, s // 4)
    text = str(count) if count < 100 else "99+"
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    font = QFont("Segoe UI", max(6, int(s * 0.52)), QFont.Bold)
    p.setFont(font)
    tw = p.fontMetrics().horizontalAdvance(text)
    gap = max(2, s // 6)
    w = s + gap + tw + 6
    x = pm.width() - pad - w
    p.setPen(Qt.NoPen)
    backing = QColor("#000000")
    backing.setAlpha(140)
    p.setBrush(backing)
    p.drawRoundedRect(QRectF(x, pad, w, s + 4), 3, 3)
    # three offset sheets = a stack
    c = QColor(theme.TEXT_HEAD)
    p.setBrush(c)
    gx, gy, gs = x + 3, pad + 2, s
    for n, alpha in ((2, 110), (1, 175), (0, 255)):
        c.setAlpha(alpha)
        p.setBrush(c)
        p.drawRoundedRect(QRectF(gx + gs * 0.10 * n, gy + gs * 0.62 - gs * 0.22 * n,
                                 gs * 0.62, gs * 0.20),
                          gs * 0.06, gs * 0.06)
    c.setAlpha(255)
    p.setPen(c)
    p.drawText(QRectF(gx + gs + gap, pad, tw + 2, s + 4),
               Qt.AlignVCenter | Qt.AlignLeft, text)
    p.end()
    return pm


def _draw_flag_glyph(p, flag, r, color):
    """One anim badge, drawn inside `r`. Deliberately shapes, not letters — at
    9-14 px a letter is a smudge, and these sit three in a row."""
    p.setPen(Qt.NoPen)
    p.setBrush(color)
    if flag == "baked":
        # a keyframe diamond — Blender's own symbol for "there is a key here",
        # and a baked anim is a key on every frame
        s = r.width()
        p.drawPolygon(QPolygonF([
            QPointF(r.x() + s * 0.5, r.y() + s * 0.12),
            QPointF(r.x() + s * 0.88, r.y() + s * 0.5),
            QPointF(r.x() + s * 0.5, r.y() + s * 0.88),
            QPointF(r.x() + s * 0.12, r.y() + s * 0.5)]))
    elif flag == "modifiers":
        # a sine wave — an F-modifier is a curve shaping a curve
        pen = QPen(color, max(1.0, r.width() * 0.13))
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        path = QPainterPath()
        s = r.width()
        path.moveTo(r.x() + s * 0.10, r.y() + s * 0.50)
        path.cubicTo(r.x() + s * 0.30, r.y() + s * 0.02,
                     r.x() + s * 0.42, r.y() + s * 0.98,
                     r.x() + s * 0.58, r.y() + s * 0.50)
        path.cubicTo(r.x() + s * 0.70, r.y() + s * 0.14,
                     r.x() + s * 0.80, r.y() + s * 0.34,
                     r.x() + s * 0.90, r.y() + s * 0.28)
        p.drawPath(path)
    else:   # "props" — a slider: the shape every rig property is driven with
        s = r.width()
        pen = QPen(color, max(1.0, s * 0.12))
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        p.drawLine(QPointF(r.x() + s * 0.10, r.y() + s * 0.5),
                   QPointF(r.x() + s * 0.90, r.y() + s * 0.5))
        p.setPen(Qt.NoPen)
        p.setBrush(color)
        p.drawEllipse(QPointF(r.x() + s * 0.66, r.y() + s * 0.5),
                      s * 0.20, s * 0.20)


def _stamp_flags(pm, flags):
    """Bottom-LEFT row of small badges: what a saved anim actually contains
    (Marty, 2026-08-05 — modifiers kept, baked, bone properties).

    ⚠ Bottom-left is the last free corner. Top-left is the type badge,
    top-right the bulk count, and the bottom EDGE is the colour label strip —
    which is why this sits above it rather than on it.
    """
    if not flags:
        return pm
    s = max(9, int(pm.height() * 0.115))
    pad = max(3, s // 3)
    gap = max(2, s // 4)
    strip = max(4, int(pm.height() * 0.05)) + 3   # clear the colour label
    w = len(flags) * s + (len(flags) - 1) * gap + 6
    y = pm.height() - strip - s - 4
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(Qt.NoPen)
    backing = QColor("#000000")
    backing.setAlpha(140)
    p.setBrush(backing)
    p.drawRoundedRect(QRectF(pad, y, w, s + 4), 3, 3)
    color = QColor(theme.TEXT_HEAD)
    for i, flag in enumerate(flags):
        _draw_flag_glyph(p, flag,
                         QRectF(pad + 3 + i * (s + gap), y + 2, s, s), color)
    p.end()
    return pm


# ---------------------------------------------------------------- pixmaps

def placeholder_pixmap(item, size):
    # ⚠ The count is part of the cache key, or an item re-saved with a different
    # number of groups keeps the old badge until the app restarts. Same for the
    # anim flags: re-saving with "keep modifiers" off has to change the tile.
    bulk = item.bulk_count()
    flags = item.anim_flags()
    key = (item.type, item.name, size, item.color, bulk, flags)
    cached = _placeholder_cache.get(key)
    if cached is not None:
        _placeholder_cache.move_to_end(key)
        return cached
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    color = QColor(theme.TYPE_COLORS.get(item.type, theme.ACCENT))
    bg = QColor(color)
    bg.setAlpha(38)
    p.setBrush(bg)
    p.setPen(QPen(QColor(theme.BORDER), 1))
    p.drawRoundedRect(1, 1, size - 2, size - 2, 8, 8)
    p.setPen(QColor(color))
    f = QFont("Segoe UI", int(size * 0.34), QFont.Bold)
    p.setFont(f)
    p.drawText(pm.rect().adjusted(0, -int(size * 0.06), 0, -int(size * 0.06)),
               Qt.AlignCenter, (item.name[:1] or "?").upper())
    p.end()
    _stamp_badge(pm, item.type)
    _stamp_bulk(pm, bulk)
    _stamp_flags(pm, flags)
    _stamp_label(pm, item.color)
    # ⚠ Byte-capped since F2: the lazy grid puts a placeholder on EVERY row
    # while decodes drain, so at 800 items this cache is no longer "a few
    # thumbless outliers" — uncapped it was silently 38 MB per tile size.
    global _placeholder_bytes
    _placeholder_cache[key] = pm
    _placeholder_bytes += _pm_bytes(pm)
    while (_placeholder_bytes > _PLACEHOLDER_CACHE_MB * 1048576
           and len(_placeholder_cache) > 1):
        _k, _old = _placeholder_cache.popitem(last=False)
        _placeholder_bytes -= _pm_bytes(_old)
    return pm


def _video_thumb_file(item):
    """Middle cached frame of a playblast — the most representative still."""
    frames = video_preview.cached_frames(item.path)
    return frames[len(frames) // 2] if frames else None


def _thumb_file(item):
    thumb = item.thumbnail
    if not thumb and item.type == "playblast":
        thumb = _video_thumb_file(item)   # extracted from the mp4, cached
    return thumb


def thumbnail_pixmap(item, size):
    """The stamped tile, synchronously — decoding the file if it must.

    Direct callers (refresh_icons, on_video_preview_ready, the anim-flag
    tests) rely on "synchronously"; the LAZINESS lives in ItemGrid, which asks
    `tile_if_cached` first and only lands here from its decode pump."""
    thumb = _thumb_file(item)
    if not thumb:
        return placeholder_pixmap(item, size)
    try:
        mtime = os.path.getmtime(thumb)
    except OSError:
        return placeholder_pixmap(item, size)
    bulk = item.bulk_count()
    flags = item.anim_flags()
    key = (thumb, mtime, size, item.type, item.color, bulk, flags)
    cached = _thumb_cache.get(key)
    if cached is not None:
        _thumb_cache.move_to_end(key)
        return cached
    src = _source_pixmap(thumb, mtime)
    if src is None:
        return placeholder_pixmap(item, size)
    # ⚠ Always .scaled(), even at the source's own size: it returns a COPY,
    # and the stamps below draw ON the pixmap — stamping the cached source
    # would brand every future size with this size's badges.
    pm = src.scaled(size, size, Qt.KeepAspectRatioByExpanding,
                    Qt.SmoothTransformation)
    _stamp_badge(pm, item.type)
    _stamp_bulk(pm, bulk)
    _stamp_flags(pm, flags)
    _stamp_label(pm, item.color)
    global _tile_bytes
    _thumb_cache[key] = pm
    _tile_bytes += _pm_bytes(pm)
    while _tile_bytes > _TILE_CACHE_MB * 1048576 and len(_thumb_cache) > 1:
        _k, _old = _thumb_cache.popitem(last=False)
        _tile_bytes -= _pm_bytes(_old)
    return pm


def tile_if_cached(item, size):
    """A finished pixmap for the row IF no disk read could improve it now:
    the stamped tile from RAM, or the placeholder when there is no thumbnail
    file at all. None means "decode later and I'll be better".

    ⚠ This must NEVER call `_decode_file` — it is the grid's build path, and
    the whole point of F2 is that building 800 rows costs 800 dict lookups,
    not 800 JPEG decodes."""
    thumb = _thumb_file(item)
    if not thumb:
        return placeholder_pixmap(item, size)   # final: nothing to decode
    try:
        mtime = os.path.getmtime(thumb)
    except OSError:
        return placeholder_pixmap(item, size)
    key = (thumb, mtime, size, item.type, item.color,
           item.bulk_count(), item.anim_flags())
    cached = _thumb_cache.get(key)
    if cached is not None:
        _thumb_cache.move_to_end(key)
        return cached
    return None


def _seq_entry(item):
    if item.type == "playblast":
        # loose mp4: frames live in the extraction cache, not in the item
        seq_dir = video_preview.cache_dir(item.path)
        files = video_preview.cached_frames(item.path)
        if not files or not seq_dir:
            return None
    else:
        seq_dir = os.path.join(item.path, "sequence")
        try:
            files = sorted(os.path.join(seq_dir, f) for f in os.listdir(seq_dir)
                           if f.lower().endswith((".jpg", ".png")))
        except OSError:
            return None
    if not files:
        return None
    sig = (len(files), os.path.getmtime(seq_dir))
    entry = _seq_cache.get(item.path)
    if entry is None or entry["sig"] != sig:
        entry = {"sig": sig, "files": files, "scaled": {}}
        _seq_cache[item.path] = entry
        while len(_seq_cache) > _SEQ_CACHE_MAX:
            _seq_cache.popitem(last=False)
    _seq_cache.move_to_end(item.path)
    return entry


def sequence_frame(entry, index, size):
    """Lazy-load + cache one scaled frame of a sequence."""
    frames = entry["scaled"].setdefault(size, [None] * len(entry["files"]))
    i = index % len(frames)
    if frames[i] is None:
        pm = QPixmap(entry["files"][i])
        if pm.isNull():
            return None
        frames[i] = pm.scaled(size, size, Qt.KeepAspectRatioByExpanding,
                              Qt.SmoothTransformation)
    return frames[i]


class ItemGrid(QListWidget):
    itemSelected = Signal(object)        # Item or None
    itemActivated2 = Signal(object)      # double-click -> apply
    deleteRequested = Signal()           # Del key pressed with a selection
    contextMenuRequested = Signal(object, object)  # item, global QPoint

    def __init__(self, icon_size=110, parent=None):
        super().__init__(parent)
        self._icon_size = icon_size
        self.setViewMode(QListView.IconMode)
        self.setResizeMode(QListView.Adjust)
        self.setMovement(QListView.Static)
        self.setUniformItemSizes(True)
        self.setSpacing(8)
        self.setIconSize(QSize(icon_size, icon_size))
        self.setGridSize(QSize(icon_size + 18, icon_size + 34))
        self.setWordWrap(False)
        self.setSelectionMode(QListWidget.ExtendedSelection)  # box/ctrl/shift select
        self.setSelectionRectVisible(True)                    # rubber band
        self.setMouseTracking(True)
        self.itemSelectionChanged.connect(self._on_selection)
        self.itemDoubleClicked.connect(
            lambda it: self.itemActivated2.emit(it.data(Qt.UserRole)))
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._on_context_menu)
        self.setDragEnabled(True)  # tiles drag onto sidebar folders to move
        self.setDragDropMode(QListWidget.DragOnly)

        # hover playback of sequence/ frames (Studio-Library style)
        self._hover_li = None
        self._hover_entry = None
        self._hover_index = 0
        self._hover_timer = QTimer(self)
        self._hover_timer.setInterval(42)  # ~24 fps
        self._hover_timer.timeout.connect(self._advance_hover)

        # lazy thumbnail decoding (F2): rows waiting for a real tile, drained
        # visible-first in ~8 ms slices between events
        self._pending = []
        self._decode_timer = QTimer(self)
        self._decode_timer.setInterval(0)
        self._decode_timer.timeout.connect(self._decode_tick)

    def set_icon_size(self, size):
        """Live zoom. Tiles come from RAM (`_source_cache`) — the disk is only
        read for rows whose original was never decoded or has been evicted,
        and then lazily. Zooming used to re-decode EVERY file at each size:
        452 ms cold and 440 ms "warm" at 800 items, because the entry-capped
        cache had already evicted the previous size."""
        if size == self._icon_size:
            return
        self._icon_size = size
        self._stop_hover()
        self._decode_timer.stop()
        self._pending = []
        self.setIconSize(QSize(size, size))
        self.setGridSize(QSize(size + 18, size + 34))
        for i in range(self.count()):
            li = self.item(i)
            item = li.data(Qt.UserRole)
            if item is None:
                continue
            pm = tile_if_cached(item, size)
            if pm is None:
                li.setIcon(QIcon(placeholder_pixmap(item, size)))
                self._pending.append(i)
            else:
                li.setIcon(QIcon(pm))
        # uniformItemSizes caches tile geometry and icon swaps don't invalidate
        # it — at big sizes tiles can clip/overlap until the next window resize,
        # so relayout + repaint right now
        self.doItemsLayout()
        self.viewport().update()
        self._prime_decodes()

    def set_items(self, items):
        self._stop_hover()
        self._decode_timer.stop()
        self._pending = []
        self.clear()
        self.setUpdatesEnabled(False)   # one relayout, not one per row
        for item in items:
            pm = tile_if_cached(item, self._icon_size)
            li = QListWidgetItem(
                QIcon(pm if pm is not None
                      else placeholder_pixmap(item, self._icon_size)),
                item.name)
            li.setData(Qt.UserRole, item)
            li.setToolTip("%s  [%s]\n%s" % (item.name, item.type, item.relpath))
            li.setTextAlignment(Qt.AlignHCenter | Qt.AlignTop)
            self.addItem(li)
            if pm is None:
                self._pending.append(self.count() - 1)
        self.setUpdatesEnabled(True)
        self._prime_decodes()

    # ------------------------------------------------- lazy decode pump

    _SYNC_DECODE_ROWS = 64

    def _prime_decodes(self):
        """Decode what the first paint will show NOW; the rest on the timer.

        ⚠ The synchronous batch is the visible rows PLUS the first 64
        unconditionally — an unshown widget still reports a default-sized
        viewport, so "visible" alone is a guess there (it measured 4 rows in
        an offscreen build). 64 is bigger than any real first screen, and it
        is what keeps libraries up to that size — and therefore every offscreen
        test fixture — fully synchronous."""
        if not self._pending:
            return
        rows = set(self._visible_pending())
        rows.update(self._pending[:self._SYNC_DECODE_ROWS])
        for row in sorted(rows):
            self._decode_row(row)
        if self._pending:
            self._decode_timer.start()

    def _visible_pending(self):
        vp = self.viewport().rect()
        if vp.height() <= 0:
            return []
        return [r for r in self._pending
                if vp.intersects(self.visualItemRect(self.item(r)))]

    def _decode_row(self, row):
        try:
            self._pending.remove(row)
        except ValueError:
            return
        li = self.item(row)
        if li is None:
            return
        item = li.data(Qt.UserRole)
        if item is not None:
            li.setIcon(QIcon(thumbnail_pixmap(item, self._icon_size)))

    def _decode_tick(self):
        """~8 ms of decoding per pass, visible rows first — scrolling into
        placeholders promotes them to the front on the next tick."""
        deadline = time.perf_counter() + 0.008
        while self._pending and time.perf_counter() < deadline:
            vp = self.viewport().rect()
            row = next((r for r in self._pending
                        if vp.intersects(self.visualItemRect(self.item(r)))),
                       self._pending[0])
            self._decode_row(row)
        if not self._pending:
            self._decode_timer.stop()

    def flush_decodes(self):
        """Drain the queue synchronously (tests, and anyone about to measure)."""
        while self._pending:
            self._decode_row(self._pending[0])
        self._decode_timer.stop()

    def refresh_icons(self, items):
        """Re-derive the tiles for `items` in place — a colour label or badge
        changed WITHOUT an mtime bump, so no rescan is coming to redraw them.
        refilter() only hides rows now; this is the repaint half it lost."""
        wanted = {id(it) for it in items}
        for i in range(self.count()):
            li = self.item(i)
            it = li.data(Qt.UserRole)
            if it is not None and id(it) in wanted:
                li.setIcon(QIcon(thumbnail_pixmap(it, self._icon_size)))

    def _on_selection(self):
        sel = self.selectedItems()
        cur = self.currentItem()
        show = cur if (cur is not None and cur.isSelected()) else (sel[0] if sel else None)
        self.itemSelected.emit(show.data(Qt.UserRole) if show else None)

    def selected_library_items(self):
        return [li.data(Qt.UserRole) for li in self.selectedItems()]

    def mimeData(self, items):
        md = QMimeData()
        paths = [li.data(Qt.UserRole).path for li in items]
        md.setData(ITEM_MIME, "\n".join(paths).encode("utf-8"))
        return md

    def _on_context_menu(self, pos):
        li = self.itemAt(pos)
        if li is None:
            return
        self._stop_hover()  # menu blocks; don't leave playback repainting under it
        if not li.isSelected():
            self.setCurrentItem(li)  # right-click selects, Explorer-style
        self.contextMenuRequested.emit(li.data(Qt.UserRole),
                                       self.viewport().mapToGlobal(pos))

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Delete and self.selectedItems():
            self.deleteRequested.emit()
            return
        super().keyPressEvent(event)

    # ---------------------------------------------------- hover playback

    def mouseMoveEvent(self, event):
        li = self.itemAt(event.position().toPoint())
        if li is not self._hover_li:
            self._stop_hover()
            if li is not None:
                item = li.data(Qt.UserRole)
                entry = _seq_entry(item)
                if entry is not None:
                    self._hover_li = li
                    self._hover_entry = entry
                    self._hover_index = 0
                    self._hover_timer.start()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):
        self._stop_hover()
        super().leaveEvent(event)

    def _stop_hover(self):
        self._hover_timer.stop()
        if self._hover_li is not None:
            item = self._hover_li.data(Qt.UserRole)
            if item is not None:
                self._hover_li.setIcon(QIcon(thumbnail_pixmap(item, self._icon_size)))
        self._hover_li = None
        self._hover_entry = None

    def _advance_hover(self):
        if self._hover_li is None or self._hover_entry is None:
            return
        pm = sequence_frame(self._hover_entry, self._hover_index, self._icon_size)
        if pm is not None:
            self._hover_li.setIcon(QIcon(pm))
        self._hover_index += 1
