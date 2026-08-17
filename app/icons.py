"""Line icons drawn with QPainter — the app's nav and toolbar glyphs.

⚠ **DRAWN, NOT SHIPPED AS FILES, AND NOT SVG.** Three earlier lessons in this
codebase all point the same way and this module is where they land together:

* `SectionTabBar._star` draws a star with a `QPainterPath` rather than writing
  "★", because glyph coverage varies by font and a missing one renders as a
  tofu box — which looks like a bug, not like a badge. The library toolbar was
  still spelling its buttons with emoji (⟳ ⚙ 🎬 ▶ 🔍) and they had exactly that
  problem: they are FONT, so they restyle themselves with a Windows update,
  ignore the palette, and are the single loudest "unfinished" tell in the UI.
* `grid.py` already draws its item-type glyphs this way (`_draw_type_glyph`),
  so a painter icon set is the house idiom rather than a new one.
* An SVG icon would need Qt's `qsvg` image plugin present in the FROZEN build.
  That is one more thing a PyInstaller rebuild can quietly drop, and the
  failure mode is silent (blank buttons) — see `theme._write_indicator_svgs`,
  where the checkmark had to become a self-healing regenerated file for the
  same class of reason. Painter code cannot be left out of a build: it is the
  build.

Every glyph is drawn on a **24×24 grid** and scaled, so sizes stay consistent
and a new icon only has to be authored once.

⚠ **THE COLOUR IS PART OF THE CACHE KEY.** These are tinted from the palette
(`theme.TEXT_DIM` at rest, `theme.ACCENT` when selected), and the palette is
REBOUND by `theme.apply_theme` — so caching by (name, size) alone would keep
serving the previous theme's icons forever. `clear_cache()` exists for the same
reason and `theme.apply_theme`'s caller uses it.

⚠ **HiDPI IS NOT OPTIONAL HERE.** Marty runs Windows display scaling; a pixmap
drawn at logical size on a 150 % screen is visibly soft next to crisp text. The
pixmap is rendered at `size * ratio` device pixels with `setDevicePixelRatio`,
which is what makes the icon sharp and still lay out as `size` logical pixels.
"""

import math

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (QColor, QIcon, QPainter, QPainterPath, QPen,
                           QPixmap, QPolygonF)
from PySide6.QtWidgets import QApplication

import theme

GRID = 24.0          # the authoring grid every _draw_* below works in
STROKE = 1.75        # line weight on that grid
_CACHE = {}
_CACHE_MAX = 384     # names × sizes × themes; a cap so a long session cannot grow


# --------------------------------------------------------------- the glyphs
# Each takes the painter (already scaled to the 24-grid, pen set, antialiased)
# and the colour, for the few glyphs that fill as well as stroke.

def _library(p, _c):
    for x, y in ((3.5, 3.5), (13.5, 3.5), (3.5, 13.5), (13.5, 13.5)):
        p.drawRoundedRect(QRectF(x, y, 7, 7), 1.6, 1.6)


def _render(p, _c):
    """A clapperboard: the tab is where renders are set up and queued."""
    p.drawRoundedRect(QRectF(3.5, 9.4, 17, 10.6), 2, 2)
    p.drawRoundedRect(QRectF(3.5, 4.4, 17, 5), 1.2, 1.2)
    for x in (8.6, 13.4, 18.2):
        p.drawLine(QPointF(x, 4.4), QPointF(x - 1.7, 9.4))


def _picker(p, _c):
    p.drawEllipse(QPointF(6.3, 17.7), 2.6, 2.6)
    p.drawEllipse(QPointF(17.7, 6.3), 2.6, 2.6)
    p.drawLine(QPointF(8.4, 15.6), QPointF(15.6, 8.4))


def _layers(p, _c):
    top = QPainterPath()
    top.moveTo(12, 3.5)
    top.lineTo(20.5, 8)
    top.lineTo(12, 12.5)
    top.lineTo(3.5, 8)
    top.closeSubpath()
    p.drawPath(top)
    for dy in (4.5, 8.5):
        under = QPainterPath()
        under.moveTo(4.5, 8 + dy)
        under.lineTo(12, 12 + dy)
        under.lineTo(19.5, 8 + dy)
        p.drawPath(under)


def _nodesetup(p, _c):
    p.drawRoundedRect(QRectF(3.5, 8, 6.5, 8), 1.6, 1.6)
    p.drawLine(QPointF(10, 10.6), QPointF(14.6, 10.6))
    p.drawLine(QPointF(10, 13.4), QPointF(14.6, 13.4))
    p.drawEllipse(QPointF(17.8, 12), 2.7, 2.7)


def _nodeeditor(p, _c):
    p.drawEllipse(QPointF(5.8, 6.5), 2.4, 2.4)
    p.drawEllipse(QPointF(5.8, 17.5), 2.4, 2.4)
    p.drawEllipse(QPointF(18.2, 12), 2.7, 2.7)
    for y0, y1 in ((6.9, 11.2), (17.1, 12.8)):
        link = QPainterPath()
        link.moveTo(8.2, y0)
        link.quadTo(12.6, y0, 15.5, y1)
        p.drawPath(link)


def _madiref(p, c):
    p.drawRoundedRect(QRectF(3.5, 5.5, 17, 13), 2, 2)
    p.setPen(Qt.NoPen)
    p.setBrush(c)
    p.drawPolygon(QPolygonF([QPointF(10.4, 9.4), QPointF(15.2, 12),
                             QPointF(10.4, 14.6)]))


def _optimize(p, c):
    """A gauge — the tab measures a scene and brings it back under budget."""
    dial = QRectF(4.4, 8.4, 15.2, 15.2)
    arc = QPainterPath()
    arc.arcMoveTo(dial, 180)
    arc.arcTo(dial, 180, -180)
    p.drawPath(arc)
    p.drawLine(QPointF(12, 16), QPointF(15.9, 11.4))
    p.setPen(Qt.NoPen)
    p.setBrush(c)
    p.drawEllipse(QPointF(12, 16), 1.35, 1.35)


def _texmaps(p, c):
    """A material ball over a run of tiles — a photo becoming a surface.

    ⚠ Drawn at the size it SHIPS at before being judged (the 2026-08-17
    lesson): at 18 px the sphere reads, the highlight reads, and the tiles
    read as a strip rather than as three dots.
    """
    p.drawEllipse(QRectF(5.2, 3.2, 13.6, 13.6))
    p.setPen(Qt.NoPen)
    p.setBrush(c)
    p.drawEllipse(QPointF(9.6, 7.6), 1.5, 1.5)
    for x in (4.2, 9.5, 14.8):
        p.drawRoundedRect(QRectF(x, 18.6, 4.6, 2.6), 0.8, 0.8)


def _nsfw(p, _c):
    heart = QPainterPath()
    heart.moveTo(12, 19.6)
    heart.cubicTo(5.9, 15.4, 4.2, 11.3, 6.3, 8.7)
    heart.cubicTo(8.2, 6.4, 10.8, 6.7, 12, 8.4)
    heart.cubicTo(13.2, 6.7, 15.8, 6.4, 17.7, 8.7)
    heart.cubicTo(19.8, 11.3, 18.1, 15.4, 12, 19.6)
    p.drawPath(heart)


def _support(p, c):
    """A FILLED heart — the "Buy me a coffee" button.

    ⚠ Filled on purpose, where `_nsfw` above is a heart STROKED: the two sit in
    the same window, and an outline heart beside an outline heart reads as the
    same control. Filled, and red rather than the rail colour, is what tells
    them apart.

    ⚠ **The deep cleft and the sharp point ARE the design.** Judge this at
    15 px, the size it actually ships at — at 60 px any lobed blob passes for a
    heart, which is how a first attempt got through looking like a rounded
    square.
    """
    heart = QPainterPath()
    heart.moveTo(12, 21.3)
    heart.cubicTo(10.6, 19.9, 2.4, 13.4, 2.4, 8.3)
    heart.cubicTo(2.4, 4.9, 5.1, 2.7, 7.9, 2.7)
    heart.cubicTo(10.0, 2.7, 11.3, 4.2, 12, 6.4)
    heart.cubicTo(12.7, 4.2, 14.0, 2.7, 16.1, 2.7)
    heart.cubicTo(18.9, 2.7, 21.6, 4.9, 21.6, 8.3)
    heart.cubicTo(21.6, 13.4, 13.4, 19.9, 12, 21.3)
    heart.closeSubpath()
    p.setPen(Qt.NoPen)
    p.fillPath(heart, c)


def _physics(p, _c):
    """A damped wave: what every tool on that tab ends up producing."""
    wave = QPainterPath()
    wave.moveTo(3, 12)
    wave.cubicTo(5, 6.4, 7, 6.4, 9, 12)
    wave.cubicTo(11, 17.6, 13, 17.6, 15, 12)
    wave.cubicTo(17, 6.4, 19, 6.4, 21, 12)
    p.drawPath(wave)


def _news(p, _c):
    bell = QPainterPath()
    bell.moveTo(6.4, 16.2)
    bell.lineTo(6.4, 10.9)
    bell.cubicTo(6.4, 7.6, 8.9, 5.2, 12, 5.2)
    bell.cubicTo(15.1, 5.2, 17.6, 7.6, 17.6, 10.9)
    bell.lineTo(17.6, 16.2)
    bell.lineTo(19.2, 18.5)
    bell.lineTo(4.8, 18.5)
    bell.closeSubpath()
    p.drawPath(bell)
    clap = QPainterPath()
    clap.moveTo(10.2, 20.3)
    clap.quadTo(12, 21.7, 13.8, 20.3)
    p.drawPath(clap)


def _search(p, _c):
    p.drawEllipse(QPointF(10.8, 10.8), 5.5, 5.5)
    p.drawLine(QPointF(14.9, 14.9), QPointF(19.6, 19.6))


def _gear(p, _c):
    """⚠ The teeth need the RING. Drawn as bare spokes radiating from a hub
    this read as a sunburst, not a gear — the closed outer circle is what makes
    the short segments outside it parse as teeth (contact sheet, 2026-08-14)."""
    p.drawEllipse(QPointF(12, 12), 2.9, 2.9)
    p.drawEllipse(QPointF(12, 12), 6.2, 6.2)
    for step in range(8):
        angle = step * math.pi / 4 + math.pi / 8
        cos, sin = math.cos(angle), math.sin(angle)
        p.drawLine(QPointF(12 + 6.0 * cos, 12 + 6.0 * sin),
                   QPointF(12 + 7.9 * cos, 12 + 7.9 * sin))


def _folder(p, _c):
    folder = QPainterPath()
    folder.moveTo(3.6, 18.6)
    folder.lineTo(3.6, 6.6)
    folder.lineTo(9.4, 6.6)
    folder.lineTo(11.5, 9.2)
    folder.lineTo(20.4, 9.2)
    folder.lineTo(20.4, 18.6)
    folder.closeSubpath()
    p.drawPath(folder)


def _refresh(p, c):
    """Rescan. The arrowhead is FILLED, not two strokes: an open chevron on a
    290° arc reads as a break in the circle at icon sizes."""
    dial = QRectF(4.7, 4.7, 14.6, 14.6)
    arc = QPainterPath()
    arc.arcMoveTo(dial, 72)
    arc.arcTo(dial, 72, -300)
    p.drawPath(arc)
    tip = QPointF(12 + 7.3 * math.cos(math.radians(72)),
                  12 - 7.3 * math.sin(math.radians(72)))
    p.setPen(Qt.NoPen)
    p.setBrush(c)
    p.drawPolygon(QPolygonF([QPointF(tip.x() - 2.6, tip.y() - 1.1),
                             QPointF(tip.x() + 2.4, tip.y() - 2.1),
                             QPointF(tip.x() + 0.6, tip.y() + 2.5)]))


def _import(p, _c):
    p.drawLine(QPointF(12, 3.8), QPointF(12, 13.4))
    arrow = QPainterPath()
    arrow.moveTo(8.4, 9.8)
    arrow.lineTo(12, 13.4)
    arrow.lineTo(15.6, 9.8)
    p.drawPath(arrow)
    tray = QPainterPath()
    tray.moveTo(4.6, 15.2)
    tray.lineTo(4.6, 18.6)
    tray.lineTo(19.4, 18.6)
    tray.lineTo(19.4, 15.2)
    p.drawPath(tray)


def _play(p, c):
    p.setPen(Qt.NoPen)
    p.setBrush(c)
    p.drawPolygon(QPolygonF([QPointF(8.8, 6.8), QPointF(17.4, 12),
                             QPointF(8.8, 17.2)]))


def _camera(p, _c):
    p.drawRoundedRect(QRectF(3.5, 7, 12.6, 10.4), 2, 2)
    lens = QPainterPath()
    lens.moveTo(16.6, 11)
    lens.lineTo(20.5, 8.4)
    lens.lineTo(20.5, 16)
    lens.lineTo(16.6, 13.4)
    lens.closeSubpath()
    p.drawPath(lens)


def _plus(p, _c):
    p.drawLine(QPointF(12, 6.2), QPointF(12, 17.8))
    p.drawLine(QPointF(6.2, 12), QPointF(17.8, 12))


def _chevron_down(p, _c):
    """Open group. Points at what it controls — the rows below it."""
    path = QPainterPath()
    path.moveTo(7.5, 10)
    path.lineTo(12, 14.5)
    path.lineTo(16.5, 10)
    p.drawPath(path)


def _chevron_right(p, _c):
    """Closed group: the rows are tucked away to the side."""
    path = QPainterPath()
    path.moveTo(10, 7.5)
    path.lineTo(14.5, 12)
    path.lineTo(10, 16.5)
    p.drawPath(path)


def _appmark(p, c):
    """The app's own mark, for the top of the rail.

    ⚠ The only FILLED glyph here besides the star, and the notch is punched
    with `CompositionMode_Clear` rather than painted in the background colour.
    The mark sits on the rail's surface in one place and could sit on any
    other later; a notch drawn in `theme.TAB_BG` would be a solid block of the
    wrong colour the moment it moved, and invisible until someone looked.
    """
    p.setPen(Qt.NoPen)
    p.setBrush(c)
    p.drawRoundedRect(QRectF(2.5, 2.5, 19, 19), 5.6, 5.6)
    p.setCompositionMode(QPainter.CompositionMode_Clear)
    p.drawRoundedRect(QRectF(7.4, 9.4, 9.2, 5.2), 1.8, 1.8)
    p.setCompositionMode(QPainter.CompositionMode_SourceOver)


# ---- window controls. Deliberately plain: these three are the most
# over-learned shapes in Windows and anything clever here reads as a bug.

def _win_min(p, _c):
    p.drawLine(QPointF(7, 12), QPointF(17, 12))


def _win_max(p, _c):
    p.drawRect(QRectF(7.5, 7.5, 9, 9))


def _win_restore(p, _c):
    """Restore down: the front pane, and the one it came from behind it."""
    p.drawRect(QRectF(6.5, 9.5, 8, 8))
    path = QPainterPath()
    path.moveTo(9.2, 9.3)
    path.lineTo(9.2, 6.5)
    path.lineTo(17.5, 6.5)
    path.lineTo(17.5, 14.6)
    path.lineTo(14.7, 14.6)
    p.drawPath(path)


def _win_close(p, _c):
    p.drawLine(QPointF(7.5, 7.5), QPointF(16.5, 16.5))
    p.drawLine(QPointF(16.5, 7.5), QPointF(7.5, 16.5))


def _star(p, c):
    """The members-only mark. Dormant while every tab is free (2026-08-14) —
    kept because `SectionRail` still takes a `premium` set, exactly as the tab
    bar it replaced did."""
    path = QPainterPath()
    for step in range(10):
        angle = -math.pi / 2 + step * math.pi / 5
        reach = 8.0 if step % 2 == 0 else 3.6
        point = QPointF(12 + reach * math.cos(angle),
                        12 + reach * math.sin(angle))
        path.lineTo(point) if step else path.moveTo(point)
    path.closeSubpath()
    p.setPen(Qt.NoPen)
    p.setBrush(c)
    p.drawPath(path)


# Name -> painter. The nav names deliberately match `MainWindow.FREE_TOOLS`
# keys so the rail can look an icon up by the key it already has.
DRAW = {
    "library": _library,
    "rendering": _render,
    "picker": _picker,
    "anim_layers": _layers,
    "node_setup": _nodesetup,
    "nodeeditor": _nodeeditor,
    "texmaps": _texmaps,
    "madiref": _madiref,
    "optimizer": _optimize,
    "nsfw": _nsfw,
    "support": _support,
    "physics": _physics,
    "news": _news,
    "search": _search,
    "gear": _gear,
    "folder": _folder,
    "refresh": _refresh,
    "import": _import,
    "play": _play,
    "camera": _camera,
    "plus": _plus,
    "chevron_down": _chevron_down,
    "chevron_right": _chevron_right,
    "star": _star,
    "appmark": _appmark,
    "win_min": _win_min,
    "win_max": _win_max,
    "win_restore": _win_restore,
    "win_close": _win_close,
}


# ------------------------------------------------------------------- public

def _ratio():
    """Device pixel ratio of the running app, or 1 with no QApplication (the
    test suites import this module headless)."""
    app = QApplication.instance()
    if app is None:
        return 1.0
    screen = app.primaryScreen()
    return float(screen.devicePixelRatio()) if screen else 1.0


def clear_cache():
    """Drop every cached pixmap. Called when the palette is rebound — see the
    module docstring."""
    _CACHE.clear()


def pixmap(name, size=18, color=None, ratio=None):
    """One glyph, antialiased and HiDPI-correct. Unknown name -> transparent.

    A missing glyph must never raise: an icon is decoration, and a typo in a
    name is not worth taking a tab down for.
    """
    color = color or theme.TEXT_DIM
    if ratio is None:
        ratio = _ratio()
    key = (name, size, str(color), round(ratio, 2))
    hit = _CACHE.get(key)
    if hit is not None:
        return hit

    device = max(1, int(round(size * ratio)))
    out = QPixmap(device, device)
    out.fill(Qt.transparent)
    out.setDevicePixelRatio(ratio)
    draw = DRAW.get(name)
    if draw is not None:
        painter = QPainter(out)
        painter.setRenderHint(QPainter.Antialiasing, True)
        # ⚠ SCALE BY THE LOGICAL SIZE, NOT BY `device`. A QPixmap carrying a
        # devicePixelRatio hands the painter a coordinate system already in
        # LOGICAL units, so scaling by the device size applies the ratio a
        # second time — every glyph drew at ratio× and clipped to a corner
        # (looked at on a contact sheet, which is the only way that shows).
        painter.scale(size / GRID, size / GRID)
        pen = QPen(QColor(color), STROKE)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        draw(painter, QColor(color))
        painter.end()

    if len(_CACHE) >= _CACHE_MAX:
        _CACHE.clear()
    _CACHE[key] = out
    return out


def icon(name, size=18, color=None):
    """QIcon wrapper for buttons and rail entries."""
    return QIcon(pixmap(name, size, color))


def button_icon(button, name, size=17, color=None):
    """Give a button a drawn glyph, and clear any placeholder text it had.

    ⚠ Clearing the text is the point of this helper. The buttons it replaces
    carried their glyph AS text ("⟳", "🎬"), so setting an icon without
    emptying the label would leave both showing.

    ⚠ Defaults to `theme.TEXT`, NOT the module default of `TEXT_DIM`: a button
    is something you press, and at 17 px the dim grey read as greyed-out next
    to full-strength labels (rendered and looked at, 2026-08-14). Dim is for
    glyphs that annotate — the magnifier beside the zoom slider — not for
    controls.
    """
    button.setIcon(icon(name, size, color or theme.TEXT))
    if button.text() and not button.property("_madi_keep_text"):
        button.setText("")
    return button
