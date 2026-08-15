"""Node Editor tab — a Blender-style node canvas (Marty picked design A,
2026-08-06: colored headers, dot grid, bezier noodles; later the same day
he picked background style C from four rendered swatches — dots + a faint
major line every 5 cells, re-leveling with zoom instead of going blank).

The tab carries the BAKE node set since 2026-08-07 (`bakenodes.py` — the
first real nodes; `docs\\nodeeditor.md`). `build_test_graph()` stays as the
SUITE's mechanics fixture only: it exercises reroutes and the multi-input
socket, which the bake graph does not have.

Architecture notes, for whoever touches this next:

- Sockets are NOT QGraphicsItems. A node paints its own socket dots and
  answers `socket_pos(kind, index)` in scene coordinates; wires and the
  view's hit-testing go through that. One item per node instead of one per
  socket keeps the item count (and the click-routing surprises) down.

- A wire endpoint is `(item, kind, index)` where item is a NodeItem or a
  RerouteItem. Reroutes answer the same `socket_pos` protocol with their
  centre, which is what lets a wire not care what it is plugged into.

- ⚠ Wire curving is the devedit "curving" field (0 = straight, 10 = full
  noodle — same scale as Blender's own Noodle Curving preference). The view
  carries `_madi_wire_canvas` so devedit offers "Edge smoothness…" on it,
  and `set_wire_curving(None)` must fall back to DEFAULT_CURVING because a
  cleared record hands every applier None — while a saved 0 is a real value
  (dead-straight wires) and must stay 0. Same is-None rule as radius.

- Selection is Marty's S1 (2026-08-06): accent outline on selected nodes,
  with the ACTIVE (last-clicked) node brighter, the way Blender separates
  the two. The colours are module constants so switching to the white /
  orange / lifted-body options he was shown is a two-line change.
"""

import math
import os

from PySide6.QtCore import QLineF, QPointF, QRectF, Qt, QThread, QTimer, Signal
from PySide6.QtGui import (QBrush, QColor, QCursor, QFont, QFontMetrics,
                           QPainter, QPainterPath, QPainterPathStroker, QPen,
                           QPolygonF)
from PySide6.QtWidgets import (QGraphicsItem, QGraphicsPathItem,
                               QGraphicsScene, QGraphicsView, QHBoxLayout,
                               QLabel, QMenu, QToolButton, QVBoxLayout,
                               QWidget)

import config
import theme
import widgets

# ---------------------------------------------------------------- constants

GRID = 22
GRID_DOT = QColor("#262a31")
GRID_LINE = QColor("#2f3742")  # style C (Marty, 2026-08-06): a faint line
                               # — bumped from #232830 on his read of the
                               # first render, still under the dot colour
GRID_MAJOR = 5                 # every GRID_MAJOR cells, dots in between
GRID_MIN_PX = 16.0             # promote the grid when a cell would land
                               # under this many SCREEN px (Blender-style
                               # re-level — the canvas never goes blank)


def grid_spacing(zoom):
    """(minor, major) grid spacing in scene units at `zoom`.

    Promotes by GRID_MAJOR whenever a cell would paint tighter than
    GRID_MIN_PX on screen, so a zoomed-out canvas shows a coarser grid
    rather than none. The cap is paranoia for zoom -> 0."""
    spacing = GRID
    if zoom > 0:
        while spacing * zoom < GRID_MIN_PX and spacing < GRID * GRID_MAJOR ** 6:
            spacing *= GRID_MAJOR
    return spacing, spacing * GRID_MAJOR

BODY = QColor(theme.PANEL)
BODY_BORDER = QColor(theme.BORDER)
ROW_BG = QColor(theme.PANEL2)
TEXT = QColor(theme.TEXT)
TEXT_DIM = QColor(theme.TEXT_DIM)
# A live value pill's text. Brighter than the row LABEL on purpose: the
# value is the part you change, and it has to read as clearly enabled next
# to a dimmed row (Marty, 2026-08-07).
VALUE_TEXT = QColor("#eef1f5")

# S1 with Blender's selected/active split. Swap these two to restyle:
# S2 white outline -> SELECT "#e6e9ee"; S3 Blender orange -> "#e0a34f";
# S4 lifted body   -> also lighten BODY in NodeItem.paint when selected.
SELECT_OUTLINE = QColor(theme.ACCENT)
ACTIVE_OUTLINE = QColor("#e6e9ee")

DEFAULT_CURVING = 5          # Blender's own Noodle Curving default
SOCKET_R = 4.5
SOCKET_HIT = 12.0            # how close a click must land, scene px

HEADER_H = 22
ROW_H = 18
ROW_GAP = 4
NODE_W = 170
NODE_RADIUS = 7

# Socket colours by what would flow through them, Blender-style.
COL_BONES = QColor("#c0b04f")
COL_GEO = QColor("#4fc0c0")
COL_MOTION = QColor(theme.ACCENT)

CHECK_BOX = 13.0             # tickbox side, in a row


def refresh_theme():
    """Rebuild the cached QColors after `theme.apply_theme` (2026-08-08).

    ⚠ **THESE ARE BUILT AT IMPORT, NOT PER PAINT** — deliberately, because a
    canvas repaint touches them thousands of times and constructing a QColor
    each time is waste. The cost is that they are a SNAPSHOT of the palette:
    without this, switching theme restyled the whole app and left the node
    canvas painted in the previous one. Any new module-level `QColor(theme.X)`
    belongs in here the same day.

    The socket colours (`COL_BONES`, `COL_GEO`) are NOT here: those say what
    flows through a socket, which is meaning, not decoration.
    """
    global BODY, BODY_BORDER, ROW_BG, TEXT, TEXT_DIM, SELECT_OUTLINE, COL_MOTION
    BODY = QColor(theme.PANEL)
    BODY_BORDER = QColor(theme.BORDER)
    ROW_BG = QColor(theme.PANEL2)
    TEXT = QColor(theme.TEXT)
    TEXT_DIM = QColor(theme.TEXT_DIM)
    SELECT_OUTLINE = QColor(theme.ACCENT)
    COL_MOTION = QColor(theme.ACCENT)


def socket_colour(item, kind, index):
    """The QColor of one socket. ⚠ A SOCKET'S COLOUR IS ITS TYPE — the
    canvas will not connect two that differ (Marty, 2026-08-07: "can only
    enter in sockets that are green")."""
    seq = item.outputs if kind == "out" else item.inputs
    return QColor(seq[index][1])


def socket_is_multi(item, index):
    """Whether an INPUT accepts several wires (drawn hollow and bigger)."""
    seq = getattr(item, "inputs", ())
    if index >= len(seq):
        return False
    spec = seq[index]
    return len(spec) > 2 and bool(spec[2])


def sockets_compatible(src, dst):
    """Can this (item, kind, index) pair be wired together?

    Same colour, or one end is an UNTYPED reroute dot — which then adopts
    the other end's colour. Reroutes have to be exempt or they could never
    be wired into anything: a dot is born with no type at all."""
    src_item, dst_item = src[0], dst[0]
    if isinstance(src_item, RerouteItem) and not src_item.typed:
        return True
    if isinstance(dst_item, RerouteItem) and not dst_item.typed:
        return True
    return (socket_colour(*src).name().lower()
            == socket_colour(*dst).name().lower())


def draw_check(painter, rect, on, dim=False):
    """The same style-A tick the rest of the app wears, as a path.

    theme.py has to ship the tick as a generated SVG because a Qt
    STYLESHEET cannot draw one; a QGraphicsItem paints itself, so the node
    canvas draws the identical stroke directly. Same 14-unit geometry as
    the SVG, so the two never drift apart by eye."""
    painter.setPen(QPen(QColor(theme.BORDER), 1))
    fill = QColor(theme.ACCENT) if on else ROW_BG
    if on and dim:
        fill = fill.darker(160)
    painter.setBrush(QBrush(fill))
    painter.drawRoundedRect(rect, 3, 3)
    if not on:
        return
    unit = rect.width() / 14.0
    tick = QPainterPath()
    tick.moveTo(rect.x() + 3.0 * unit, rect.y() + 7.4 * unit)
    tick.lineTo(rect.x() + 6.0 * unit, rect.y() + 10.2 * unit)
    tick.lineTo(rect.x() + 11.0 * unit, rect.y() + 4.6 * unit)
    pen = QPen(QColor(theme.TEXT_DIM if dim else "#ffffff"), 2.2 * unit)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.NoBrush)
    painter.drawPath(tick)


# -------------------------------------------------------------------- items


class NodeItem(QGraphicsItem):
    """One node: colored header, label/value rows, painted socket dots.

    A row is `(label, value)` and the VALUE decides how it draws:

        "1024"        a value pill (the normal case)
        True / False  a tickbox — the same style A check the rest of the
                      app got, painted rather than a widget
        None          a dim section heading, no pill (Blender's sub-panel
                      titles: "Influence", "Contributions", "Output")

    Rows can be replaced at runtime (`set_rows`) — the Bake node rebuilds
    its whole row list every time the bake type changes, the way Blender's
    panel swaps its Influence controls. Sockets are indexed by SLOT, not by
    row, so growing the rows never moves a wire.
    """

    def __init__(self, title, header_color, rows=(), inputs=(), outputs=(),
                 width=NODE_W, label_frac=0.5):
        super().__init__()
        self.title = title
        self.header_color = QColor(header_color)
        self.rows = list(rows)            # [(label, value)]
        self.inputs = list(inputs)        # [(name, QColor, multi)]
        self.outputs = list(outputs)      # [(name, QColor)]
        self.w = width
        self.label_frac = label_frac      # where the value column starts
        self.extra_h = 0.0                # painted area below the rows
        self.dim_rows = set()             # drawn greyed (Blender's inactive)
        # The ComfyUI-style strip under the node while IT is the one working:
        # None = hidden, 0..1 = filled fraction, negative = indeterminate
        # marquee (the tab's timer advances _marquee_phase while a bake runs).
        self.progress = None
        self._marquee_phase = 0.0
        # ⚠ The ? only appears when a node HAS something to say. The test
        # graph's nodes carry no help_text and therefore no button, which
        # is what keeps the mechanics fixture free of a UI it never uses.
        self.help_text = None
        self.h = self._natural_height()
        self.setFlags(QGraphicsItem.ItemIsMovable
                      | QGraphicsItem.ItemIsSelectable
                      | QGraphicsItem.ItemSendsScenePositionChanges)

    # --- geometry ---------------------------------------------------------

    def _natural_height(self):
        slots = max(len(self.inputs), len(self.outputs), len(self.rows))
        return (HEADER_H + ROW_GAP + slots * (ROW_H + ROW_GAP) + ROW_GAP
                + self.extra_h)

    def set_rows(self, rows):
        """Swap the whole row list and resize to match."""
        self.prepareGeometryChange()
        self.rows = list(rows)
        self.h = self._natural_height()
        self.update()

    def set_extra_height(self, extra):
        """Reserve painted space below the rows (a button, a preview)."""
        self.prepareGeometryChange()
        self.extra_h = float(extra)
        self.h = self._natural_height()
        self.update()

    def value_x(self):
        """Left edge of the value column — pills, tickboxes and pill
        hit-testing all start here."""
        return self.w * self.label_frac

    def help_rect(self):
        """The ? button in the header, or None when the node has no help.
        Sits at the RIGHT end of the header, clear of the title text."""
        if not self.help_text:
            return None
        size = 15.0
        return QRectF(self.w - size - 7.0, (HEADER_H - size) * 0.5,
                      size, size)


    def boundingRect(self):
        # The extra 7 below the body is the progress strip's home (it draws
        # at h+3..h+7). Constant on purpose: a boundingRect that changes
        # with `progress` would need prepareGeometryChange on every tick.
        pad = SOCKET_R + 3
        return QRectF(-pad, -3, self.w + 2 * pad, self.h + 13)

    def shape(self):
        """The BODY only. The default shape is boundingRect, and growing
        that for the progress strip must not grow the click/selection zone
        with it — socket hits go through the view's socket_under, never
        through this."""
        path = QPainterPath()
        path.addRoundedRect(QRectF(0, 0, self.w, self.h),
                            NODE_RADIUS, NODE_RADIUS)
        return path

    def set_progress(self, value):
        """None hides the strip; 0..1 fills it; negative = marquee."""
        self.progress = value
        self.update()

    def _slot_y(self, index):
        return HEADER_H + ROW_GAP + index * (ROW_H + ROW_GAP) + ROW_H * 0.5

    def socket_pos(self, kind, index):
        """Scene position of a socket dot. kind is "in" or "out"."""
        x = 0.0 if kind == "in" else float(self.w)
        return self.mapToScene(QPointF(x, self._slot_y(index)))

    def socket_at(self, scene_pos):
        """(kind, index) of the socket near scene_pos, or None."""
        for kind, seq in (("out", self.outputs), ("in", self.inputs)):
            for i in range(len(seq)):
                if (self.socket_pos(kind, i) - scene_pos).manhattanLength() \
                        <= SOCKET_HIT:
                    return kind, i
        return None

    # --- painting ---------------------------------------------------------

    def _border_pen(self):
        """Separated from paint() so the suite can check the selection colour
        without rendering pixels."""
        if self.scene() and getattr(self.scene(), "active_node", None) is self:
            return QPen(ACTIVE_OUTLINE, 1.6)
        if self.isSelected():
            return QPen(SELECT_OUTLINE, 2.0)
        return QPen(BODY_BORDER, 1.0)

    def paint(self, painter, option, widget=None):
        painter.setRenderHint(QPainter.Antialiasing, True)
        body = QRectF(0, 0, self.w, self.h)

        painter.setPen(self._border_pen())
        painter.setBrush(QBrush(BODY))
        painter.drawRoundedRect(body, NODE_RADIUS, NODE_RADIUS)

        # header: rounded top corners only (clip a rounded rect to the strip)
        head = QPainterPath()
        head.addRoundedRect(QRectF(0, 0, self.w, HEADER_H + NODE_RADIUS),
                            NODE_RADIUS, NODE_RADIUS)
        clip = QPainterPath()
        clip.addRect(QRectF(0, 0, self.w, HEADER_H))
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(self.header_color))
        painter.drawPath(head.intersected(clip))

        font = painter.font()
        font.setPixelSize(12)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QPen(self.header_color.darker(300)))
        help_rect = self.help_rect()
        title_w = self.w - 20 - (help_rect.width() + 6 if help_rect else 0)
        painter.drawText(QRectF(10, 0, title_w, HEADER_H),
                         Qt.AlignVCenter | Qt.AlignLeft, self.title)

        if help_rect is not None:
            # a ringed ? in the header's own darker ink, so it reads as part
            # of the header rather than as another value pill
            ink = self.header_color.darker(300)
            painter.setPen(QPen(ink, 1.2))
            painter.setBrush(Qt.NoBrush)
            painter.drawEllipse(help_rect)
            mark = painter.font()
            mark.setPixelSize(11)
            mark.setBold(True)
            painter.setFont(mark)
            painter.drawText(help_rect, Qt.AlignCenter, "?")
            painter.setFont(font)

        font.setBold(False)
        font.setPixelSize(11)
        painter.setFont(font)
        vx = self.value_x()
        for i, (label, value) in enumerate(self.rows):
            y = HEADER_H + ROW_GAP + i * (ROW_H + ROW_GAP)
            dim = i in self.dim_rows
            if value is None:                       # a section heading
                painter.setPen(QPen(TEXT_DIM))
                font.setBold(True)
                painter.setFont(font)
                painter.drawText(QRectF(10, y, self.w - 20, ROW_H),
                                 Qt.AlignVCenter | Qt.AlignLeft, label)
                font.setBold(False)
                painter.setFont(font)
                continue
            painter.setPen(QPen(TEXT_DIM if dim else TEXT))
            painter.drawText(QRectF(10, y, vx - 12, ROW_H),
                             Qt.AlignVCenter | Qt.AlignLeft, label)
            if value is True or value is False:     # a tickbox
                box = QRectF(vx, y + (ROW_H - CHECK_BOX) * 0.5,
                             CHECK_BOX, CHECK_BOX)
                draw_check(painter, box, value, dim)
                continue
            pill = QRectF(vx, y, self.w - vx - 10, ROW_H)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QBrush(ROW_BG))
            painter.drawRoundedRect(pill, 4, 4)
            # ⚠ A LIVE value is bright, a dimmed one is not (Marty,
            # 2026-08-07: "when the text is not grayed out make it
            # brighter, so the user knows it's not grayed out"). Every pill
            # used to paint in TEXT_DIM, which made an editable field look
            # exactly like a disabled one — the whole point of dim_rows.
            painter.setPen(QPen(TEXT_DIM if dim else VALUE_TEXT))
            painter.drawText(pill.adjusted(8, 0, -8, 0),
                             Qt.AlignVCenter | Qt.AlignRight, str(value))

        # socket dots. A multi-input is drawn hollow and a little bigger, the
        # way Blender marks "several wires may land here".
        for kind, seq in (("in", self.inputs), ("out", self.outputs)):
            x = 0.0 if kind == "in" else float(self.w)
            for i, spec in enumerate(seq):
                colour = spec[1]
                multi = kind == "in" and len(spec) > 2 and spec[2]
                c = QPointF(x, self._slot_y(i))
                if multi:
                    painter.setPen(QPen(QColor(colour), 2))
                    painter.setBrush(QBrush(BODY))
                    painter.drawEllipse(c, SOCKET_R + 1.5, SOCKET_R + 1.5)
                else:
                    painter.setPen(QPen(QColor("#101215"), 1))
                    painter.setBrush(QBrush(QColor(colour)))
                    painter.drawEllipse(c, SOCKET_R, SOCKET_R)

        # The progress strip UNDER the node — ComfyUI's "this one is
        # working" bar (Marty, 2026-08-07). Determinate while a queue
        # counts maps, a travelling marquee segment while one bake simply
        # takes as long as it takes.
        if self.progress is not None:
            track = QRectF(0, self.h + 3, self.w, 4)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QBrush(ROW_BG))
            painter.drawRoundedRect(track, 2, 2)
            painter.setBrush(QBrush(QColor(theme.ACCENT)))
            if self.progress < 0:
                span = self.w * 0.3
                x = (self.w + span) * self._marquee_phase - span
                left, right = max(0.0, x), min(float(self.w), x + span)
                if right > left:
                    painter.drawRoundedRect(
                        QRectF(left, track.y(), right - left, 4), 2, 2)
            else:
                fill = self.w * max(0.0, min(1.0, self.progress))
                if fill > 0.5:
                    painter.drawRoundedRect(
                        QRectF(0, track.y(), fill, 4), 2, 2)

    # --- the ? button -----------------------------------------------------

    def mousePressEvent(self, event):
        """⚠ The ? is checked FIRST, before FieldNode's row hit-testing and
        before the drag — it sits in the header, where a press would
        otherwise just start moving the node."""
        rect = self.help_rect()
        if (event.button() == Qt.LeftButton and rect is not None
                and rect.contains(event.pos())):
            event.accept()
            scene = self.scene()
            if scene is not None and hasattr(scene, "toggle_help"):
                scene.toggle_help(self)
            return
        super().mousePressEvent(event)

    # --- change tracking --------------------------------------------------

    def itemChange(self, change, value):
        if change == QGraphicsItem.ItemScenePositionHasChanged and self.scene():
            self.scene().update_wires_for(self)
            bubble = getattr(self.scene(), "help_bubble", None)
            if bubble is not None and bubble.node is self:
                bubble.reposition()
        elif (change == QGraphicsItem.ItemSelectedHasChanged and value
                and self.scene()):
            self.scene().set_active_node(self)
        return super().itemChange(change, value)


# Which look the node help bubble wears. ⚠ ONE CONSTANT IS THE SWAP POINT —
# Marty picks from rendered options (2026-08-08) exactly as he did for the
# node design, the tickbox and the grid, and the four are painted by the one
# method below so they can never drift apart in behaviour, only in looks.
#   A = Blender tooltip: dark panel, hairline border, pointer at the node
#   B = accent card: the node's OWN header colour as a top bar
#   C = ghost: translucent, borderless, text only
#   D = mini node: a header strip carrying the node's title, like a node
HELP_STYLE = "A"
HELP_W = 268.0
HELP_PAD = 10.0


class HelpBubble(QGraphicsItem):
    """The little screen a node's ? opens above it (Marty, 2026-08-08:
    *"open a neat little screen above with a text description of exactly
    what the node does"*).

    ⚠ ONE bubble exists at a time, owned by the scene — two open panels
    would overlap and there would be no obvious way to shut either. Clicking
    the same ? closes it, clicking another node's moves it.

    ⚠ It is NOT a child of the node. A child would inherit the node's
    selection outline and be dragged with it while the user is reading;
    keeping it a sibling at a high Z lets it sit over whatever it needs to."""

    def __init__(self, node, text, style=None):
        super().__init__()
        self.node = node
        self.text = text
        self.style = style or HELP_STYLE
        self.setZValue(5)
        self._layout()
        self.reposition()

    def _layout(self):
        font = QFont()
        font.setPixelSize(11)
        metrics = QFontMetrics(font)
        inner = HELP_W - 2 * HELP_PAD
        rect = metrics.boundingRect(QRectF(0, 0, inner, 1e5).toRect(),
                                    int(Qt.TextWordWrap), self.text)
        self.head_h = 0.0 if self.style in ("A", "C") else 22.0
        if self.style == "A":
            self.head_h = 18.0            # a title line, no coloured strip
        self.text_h = float(rect.height())
        self.h = self.head_h + self.text_h + 2 * HELP_PAD

    def reposition(self):
        """Above the node, left edges aligned — and it FOLLOWS the node,
        because a panel that stayed put while the node moved would look
        like a bug rather than a tooltip."""
        self.setPos(self.node.x(),
                    self.node.y() - self.h - (12.0 if self.style == "A"
                                              else 8.0))

    def boundingRect(self):
        return QRectF(-2, -2, HELP_W + 4, self.h + 12)

    def paint(self, painter, option, widget=None):
        painter.setRenderHint(QPainter.Antialiasing, True)
        body = QRectF(0, 0, HELP_W, self.h)
        accent = QColor(getattr(self.node, "header_color", theme.ACCENT))
        title = getattr(self.node, "title", "Node")
        font = painter.font()
        font.setPixelSize(11)

        if self.style == "C":
            # ghost: translucent slab, no border, nothing but words
            back = QColor("#0d0f13")
            back.setAlpha(232)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QBrush(back))
            painter.drawRoundedRect(body, 6, 6)
        else:
            painter.setPen(QPen(QColor(theme.BORDER), 1))
            painter.setBrush(QBrush(QColor("#1b1f26")))
            painter.drawRoundedRect(body, 6, 6)

        if self.style == "B":
            # accent card: a bar in the node's own colour, so the panel is
            # visibly ABOUT this node
            bar = QPainterPath()
            bar.addRoundedRect(QRectF(0, 0, HELP_W, self.head_h + 6), 6, 6)
            clip = QPainterPath()
            clip.addRect(QRectF(0, 0, HELP_W, self.head_h))
            painter.setPen(Qt.NoPen)
            painter.setBrush(QBrush(accent))
            painter.drawPath(bar.intersected(clip))
            font.setBold(True)
            painter.setFont(font)
            painter.setPen(QPen(accent.darker(300)))
            painter.drawText(QRectF(HELP_PAD, 0, HELP_W - 2 * HELP_PAD,
                                    self.head_h),
                             Qt.AlignVCenter | Qt.AlignLeft, title)
        elif self.style == "D":
            # mini node: the same header geometry the nodes themselves use
            head = QPainterPath()
            head.addRoundedRect(QRectF(0, 0, HELP_W, self.head_h + 6), 6, 6)
            clip = QPainterPath()
            clip.addRect(QRectF(0, 0, HELP_W, self.head_h))
            painter.setPen(Qt.NoPen)
            painter.setBrush(QBrush(accent.darker(115)))
            painter.drawPath(head.intersected(clip))
            font.setBold(True)
            painter.setFont(font)
            painter.setPen(QPen(QColor("#12151a")))
            painter.drawText(QRectF(HELP_PAD, 0, HELP_W - 2 * HELP_PAD,
                                    self.head_h),
                             Qt.AlignVCenter | Qt.AlignLeft, title)
            painter.setPen(QPen(QColor(theme.BORDER), 1))
            painter.drawLine(QPointF(0, self.head_h),
                             QPointF(HELP_W, self.head_h))
        elif self.style == "A":
            # Blender tooltip: the node's name as a plain bright title
            font.setBold(True)
            painter.setFont(font)
            painter.setPen(QPen(QColor(theme.TEXT_HEAD)))
            painter.drawText(QRectF(HELP_PAD, 2, HELP_W - 2 * HELP_PAD,
                                    self.head_h),
                             Qt.AlignVCenter | Qt.AlignLeft, title)

        font.setBold(False)
        painter.setFont(font)
        painter.setPen(QPen(TEXT_DIM if self.style == "C" else TEXT))
        painter.drawText(QRectF(HELP_PAD, self.head_h + HELP_PAD * 0.5,
                                HELP_W - 2 * HELP_PAD, self.text_h + 4),
                         int(Qt.TextWordWrap) | int(Qt.AlignLeft), self.text)

        if self.style == "A":
            # a little pointer down at the node it belongs to
            tip = QPolygonF([QPointF(18, self.h), QPointF(30, self.h),
                             QPointF(24, self.h + 7)])
            painter.setPen(Qt.NoPen)
            painter.setBrush(QBrush(QColor("#1b1f26")))
            painter.drawPolygon(tip)
            painter.setPen(QPen(QColor(theme.BORDER), 1))
            painter.drawLine(tip.at(0), tip.at(2))
            painter.drawLine(tip.at(2), tip.at(1))


class RerouteItem(QGraphicsItem):
    """A Blender-style reroute dot: wires pass through it, so two noodles
    heading into the same node can be gathered and steered together. One
    input side, any number of outputs — same rule as Blender's.

    ⚠ A FRESH one is UNTYPED and takes the colour of the first wire that
    reaches it (`adopt`), the way Blender's reroute takes the type of its
    input. Without that, colour-matched connecting (2026-08-07) would make
    a reroute useless: it would be born some fixed colour and refuse every
    socket that is not it."""

    R = 6.0

    def __init__(self, colour=None):
        super().__init__()
        self.typed = colour is not None
        self.colour = QColor(colour if colour is not None else COL_BONES)
        self.inputs = [("in", self.colour)]
        self.outputs = [("out", self.colour)]
        self.setFlags(QGraphicsItem.ItemIsMovable
                      | QGraphicsItem.ItemIsSelectable
                      | QGraphicsItem.ItemSendsScenePositionChanges)
        self.setToolTip("Reroute")

    def boundingRect(self):
        pad = 4
        return QRectF(-self.R - pad, -self.R - pad,
                      2 * (self.R + pad), 2 * (self.R + pad))

    def socket_pos(self, kind, index):
        return self.mapToScene(QPointF(0, 0))

    def socket_at(self, scene_pos):
        if (self.socket_pos("in", 0) - scene_pos).manhattanLength() \
                <= SOCKET_HIT:
            return "in", 0
        return None

    def adopt(self, colour):
        """Take a wire's colour as this dot's type, and recolour whatever
        is already plugged into it so the run of noodles reads as one."""
        self.typed = True
        self.colour = QColor(colour)
        self.inputs = [("in", self.colour)]
        self.outputs = [("out", self.colour)]
        scene = self.scene()
        if scene is not None:
            for wire in scene.wires:
                if wire.touches(self):
                    wire.recolour(self.colour)
        self.update()

    def paint(self, painter, option, widget=None):
        painter.setRenderHint(QPainter.Antialiasing, True)
        pen = (QPen(SELECT_OUTLINE, 2) if self.isSelected()
               else QPen(QColor("#101215"), 1))
        painter.setPen(pen)
        # An untyped dot is drawn hollow: it has no colour yet, and a solid
        # one would be claiming a type it does not have.
        painter.setBrush(QBrush(self.colour if self.typed else BODY))
        painter.drawEllipse(QPointF(0, 0), self.R, self.R)

    def itemChange(self, change, value):
        if change == QGraphicsItem.ItemScenePositionHasChanged and self.scene():
            self.scene().update_wires_for(self)
        return super().itemChange(change, value)


class WireItem(QGraphicsPathItem):
    """A noodle. Endpoints are (item, kind, index); the path is rebuilt from
    them whenever either end moves or the curving amount changes."""

    def __init__(self, src, dst, colour):
        super().__init__()
        self.src = src
        self.dst = dst
        self.colour = QColor(colour)
        self.setZValue(-1)
        self.setPen(QPen(self.colour, 1.7))

    def endpoints(self):
        s_item, s_kind, s_idx = self.src
        d_item, d_kind, d_idx = self.dst
        return (s_item.socket_pos(s_kind, s_idx),
                d_item.socket_pos(d_kind, d_idx))

    def recolour(self, colour):
        self.colour = QColor(colour)
        self.setPen(QPen(self.colour, 1.7))

    def update_path(self, curving):
        p1, p2 = self.endpoints()
        path = QPainterPath(p1)
        if curving <= 0:
            path.lineTo(p2)
        else:
            # Handles reach horizontally toward each other, further the
            # curvier — proportional to distance, floored so short wires
            # still visibly bow. Same feel as Blender's noodles.
            off = (curving / 10.0) * max(30.0, abs(p2.x() - p1.x()) * 0.55)
            path.cubicTo(p1 + QPointF(off, 0), p2 - QPointF(off, 0), p2)
        self.setPath(path)

    def touches(self, item):
        return self.src[0] is item or self.dst[0] is item


# -------------------------------------------------------------------- scene


class NodeScene(QGraphicsScene):
    """Holds the graph plus the two bits of drawing state the items read:
    the wire-curving amount and which node is ACTIVE (last clicked)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSceneRect(-1200, -800, 2400, 1600)
        self.curving = DEFAULT_CURVING
        self.active_node = None
        self.wires = []
        self.help_bubble = None       # at most one, see toggle_help

    # --- graph ------------------------------------------------------------

    def add_wire(self, src, dst, colour):
        # ⚠ ONE WIRE PER INPUT, Blender's rule: a second wire into a plain
        # input REPLACES the first. Without it two sources could feed one
        # socket and the graph walk would silently follow whichever landed
        # first — an ambiguity that only shows up as a bake of the wrong
        # thing. Multi-inputs (drawn hollow) are exempt, which is what they
        # are for.
        d_item, d_kind, d_idx = dst
        if d_kind == "in" and not socket_is_multi(d_item, d_idx):
            for wire in self.wires_into(d_item, d_idx):
                self.remove_wire(wire)
        wire = WireItem(src, dst, colour)
        self.addItem(wire)
        self.wires.append(wire)
        wire.update_path(self.curving)
        return wire

    def remove_wire(self, wire):
        if wire in self.wires:
            self.wires.remove(wire)
        self.removeItem(wire)

    def toggle_help(self, node):
        """Open this node's help bubble, or close it if it is already the
        one showing. Returns the bubble, or None when it just closed."""
        current = self.help_bubble
        self.close_help()
        if current is not None and current.node is node:
            return None
        if not getattr(node, "help_text", None):
            return None
        self.help_bubble = HelpBubble(node, node.help_text)
        self.addItem(self.help_bubble)
        return self.help_bubble

    def close_help(self):
        if self.help_bubble is not None:
            self.removeItem(self.help_bubble)
            self.help_bubble = None

    def split_with_reroute(self, wire, gesture):
        """Drop a reroute dot into `wire` where `gesture` crosses it, and
        rejoin the two halves through it — Blender's Add Reroute
        (shift + right-drag), Marty 2026-08-08.

        ⚠ The dot lands on the WIRE, not on the gesture line. The gesture
        is a slash across several noodles at once, so its own path is only
        a way of SAYING WHICH wires; the point the user meant on each one
        is the point on that noodle. Sampling both and taking the closest
        pair is exact enough at any zoom and needs no curve maths.

        ⚠ The dot is born TYPED, with the wire's colour — an untyped one
        would be a hole in the colour rule the moment it appeared."""
        if wire not in self.wires:
            return None
        src, dst, colour = wire.src, wire.dst, QColor(wire.colour)
        point = self._closest_point(wire.path(), gesture)
        self.remove_wire(wire)
        dot = RerouteItem(colour)
        dot.setPos(point)
        self.addItem(dot)
        self.add_wire(src, (dot, "in", 0), colour)
        self.add_wire((dot, "out", 0), dst, colour)
        return dot

    @staticmethod
    def _closest_point(wire_path, gesture, samples=64):
        """The point on `wire_path` nearest `gesture`."""
        marks = [gesture.pointAtPercent(i / 16.0) for i in range(17)]
        best, best_d = wire_path.pointAtPercent(0.5), None
        for i in range(samples + 1):
            point = wire_path.pointAtPercent(i / float(samples))
            for mark in marks:
                dist = ((point.x() - mark.x()) ** 2
                        + (point.y() - mark.y()) ** 2)
                if best_d is None or dist < best_d:
                    best, best_d = point, dist
        return best

    def update_wires_for(self, item):
        for wire in self.wires:
            if wire.touches(item):
                wire.update_path(self.curving)

    def remove_node(self, item):
        """Drop a node or reroute dot and every wire plugged into it."""
        if self.help_bubble is not None and self.help_bubble.node is item:
            self.close_help()          # never leave a panel about a gone node
        for wire in [w for w in self.wires if w.touches(item)]:
            self.remove_wire(wire)
        if self.active_node is item:
            self.active_node = None
        self.removeItem(item)

    def wires_crossing(self, cut_path):
        """The wires whose noodle the cut line crosses. ⚠ Both paths are
        STROKED into thin filled outlines first — QPainterPath.intersects
        compares FILL areas, and an open polyline's implicit fill is a
        degenerate sliver, so raw noodle-vs-cut-line tests miss (or
        false-hit) real crossings."""
        stroker = QPainterPathStroker()
        stroker.setWidth(3.0)
        cut = stroker.createStroke(cut_path)
        return [w for w in self.wires
                if cut.intersects(stroker.createStroke(w.path()))]

    def wires_into(self, item, index=None):
        """The wires ending at item (optionally at one input socket) —
        how the suite checks the multi-input really carries two."""
        got = []
        for wire in self.wires:
            d_item, d_kind, d_idx = wire.dst
            if d_item is item and d_kind == "in" \
                    and (index is None or d_idx == index):
                got.append(wire)
        return got

    # --- state ------------------------------------------------------------

    def set_curving(self, amount):
        self.curving = max(0, min(10, int(amount)))
        for wire in self.wires:
            wire.update_path(self.curving)

    def set_active_node(self, node):
        previous = self.active_node
        self.active_node = node
        for item in (previous, node):
            if item is not None:
                item.update()

    def socket_under(self, scene_pos, kind=None):
        """(item, kind, index) near scene_pos, or None. `kind` filters to
        "in"/"out" so a connect-drag can insist on ending at an input."""
        for item in self.items():
            finder = getattr(item, "socket_at", None)
            if finder is None:
                continue
            hit = finder(scene_pos)
            if hit and (kind is None or hit[0] == kind):
                return item, hit[0], hit[1]
        return None

    # --- background -------------------------------------------------------

    def drawBackground(self, painter, rect):
        painter.fillRect(rect, QColor(theme.BG))
        # Style C: dots every cell, a faint line every GRID_MAJOR cells.
        # The grid RE-LEVELS instead of switching off (it used to go blank
        # under ~30% zoom): when a cell would land under GRID_MIN_PX on
        # screen, spacing promotes by GRID_MAJOR, so the dot count is
        # bounded by the viewport (~viewport_px / GRID_MIN_PX per axis)
        # at any zoom. Pens are cosmetic — 1px lines / 2px dots on screen
        # regardless of scale.
        zoom = painter.transform().m11()
        if zoom <= 0:
            return
        minor, major = grid_spacing(zoom)

        lines = []
        x = math.floor(rect.left() / major) * major
        while x < rect.right():
            lines.append(QLineF(x, rect.top(), x, rect.bottom()))
            x += major
        y = math.floor(rect.top() / major) * major
        while y < rect.bottom():
            lines.append(QLineF(rect.left(), y, rect.right(), y))
            y += major
        pen = QPen(GRID_LINE)
        pen.setCosmetic(True)
        painter.setPen(pen)
        if lines:
            painter.drawLines(lines)

        dots = []
        x = math.floor(rect.left() / minor) * minor
        while x < rect.right():
            y = math.floor(rect.top() / minor) * minor
            while y < rect.bottom():
                dots.append(QPointF(x, y))
                y += minor
            x += minor
        pen = QPen(GRID_DOT)
        pen.setWidthF(2.0)
        pen.setCosmetic(True)
        pen.setCapStyle(Qt.RoundCap)
        painter.setPen(pen)
        if dots:
            painter.drawPoints(QPolygonF(dots))


# --------------------------------------------------------------------- view


class NodeView(QGraphicsView):
    """Wheel zooms (to the cursor), middle-mouse pans, left-drag on empty
    space rubber-band selects, left-drag from a socket pulls a new wire,
    Ctrl+left-drag severs every wire it crosses (Blender's Cut Links)."""

    ZOOM_MIN = 0.15
    ZOOM_MAX = 3.0
    ZOOM_STEP = 1.15            # one wheel notch / one Ctrl +/- press

    zoomChanged = Signal(int)   # percent

    def __init__(self, scene, parent=None):
        super().__init__(scene, parent)
        self.setRenderHint(QPainter.Antialiasing, True)
        self.setDragMode(QGraphicsView.RubberBandDrag)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        # ⚠ NO setBackgroundBrush here — EVER. A QGraphicsView with its own
        # background brush fills the viewport itself and NEVER CALLS the
        # scene's drawBackground, which is where the grid lives. That one
        # line kept the grid invisible in the live app from the day the tab
        # shipped, while every offscreen proof (scene.render bypasses the
        # view) showed it fine (found 2026-08-07). The scene's own
        # drawBackground fills theme.BG first, so nothing is lost.
        # What devedit keys "Edge smoothness…" off (see module docstring).
        self.setProperty("_madi_wire_canvas", True)
        self._pan_from = None
        self._pan_carry = QPointF(0, 0)
        self._drag_wire = None
        self._drag_src = None
        self._cut_line = None
        self._reroute_line = None      # shift + right-drag: Add Reroute

    # --- devedit hook -----------------------------------------------------

    def set_wire_curving(self, amount):
        """⚠ None means "no saved edit" (fall back to the default), while 0
        is a real saved value meaning straight wires — the radius rule."""
        scene = self.scene()
        if scene is None:
            return
        scene.set_curving(DEFAULT_CURVING if amount is None else amount)

    def wire_curving(self):
        return self.scene().curving if self.scene() else DEFAULT_CURVING

    # --- zoom -------------------------------------------------------------

    def zoom_factor(self):
        return self.transform().m11()

    def apply_zoom(self, factor):
        clamped = max(self.ZOOM_MIN, min(self.ZOOM_MAX, factor))
        current = self.zoom_factor()
        if current:
            self.scale(clamped / current, clamped / current)
        self.zoomChanged.emit(int(round(self.zoom_factor() * 100)))
        return self.zoom_factor()

    def wheelEvent(self, event):
        # Plain wheel and Ctrl+wheel both zoom (Marty asked for Ctrl+scroll
        # explicitly, 2026-08-06 — no modifier filter means both work).
        step = self.ZOOM_STEP if event.angleDelta().y() > 0 else 1 / self.ZOOM_STEP
        self.apply_zoom(self.zoom_factor() * step)
        event.accept()

    def keyPressEvent(self, event):
        # Ctrl +/- zoom steps (Marty, 2026-08-06). Key_Equal/Key_Underscore
        # cover the unshifted/shifted keys so it works without the numpad.
        if event.modifiers() & Qt.ControlModifier:
            key = event.key()
            if key in (Qt.Key_Plus, Qt.Key_Equal):
                self.apply_zoom(self.zoom_factor() * self.ZOOM_STEP)
                event.accept()
                return
            if key in (Qt.Key_Minus, Qt.Key_Underscore):
                self.apply_zoom(self.zoom_factor() / self.ZOOM_STEP)
                event.accept()
                return
        if (event.key() == Qt.Key_A
                and event.modifiers() == Qt.ShiftModifier):
            # Blender's Shift+A (Marty, 2026-08-07): the Add-node menu at
            # the cursor, and the node lands where the cursor is. The menu
            # itself lives on the tab so the toolbar "+ Add node" and this
            # can never offer different lists.
            owner = self.parent()
            if hasattr(owner, "open_add_menu"):
                vp = self.viewport().mapFromGlobal(QCursor.pos())
                if not self.viewport().rect().contains(vp):
                    vp = self.viewport().rect().center()
                owner.open_add_menu(QCursor.pos(), self.mapToScene(vp))
                event.accept()
                return
        if (event.key() in (Qt.Key_Delete, Qt.Key_X)
                and not event.modifiers()):
            # Blender's X/Del: selected nodes go, wires plugged into them
            # go with them. Real nodes are re-addable from + Add node.
            scene = self.scene()
            doomed = [item for item in scene.selectedItems()
                      if hasattr(item, "socket_pos")]
            for item in doomed:
                scene.remove_node(item)
            if doomed:
                event.accept()
                return
        super().keyPressEvent(event)

    # --- mouse ------------------------------------------------------------

    def mousePressEvent(self, event):
        if event.button() == Qt.MiddleButton:
            self._pan_from = event.position()
            self._pan_carry = QPointF(0, 0)
            self.setCursor(Qt.ClosedHandCursor)
            event.accept()
            return
        if (event.button() == Qt.RightButton
                and event.modifiers() & Qt.ShiftModifier):
            # Blender's Add Reroute (Marty, 2026-08-08): shift + right-drag
            # across wires drops a reroute dot on each one. Same gesture,
            # same button, same "armed at press" rule as the cut — so the
            # two read as a pair, and letting go of Shift mid-drag keeps
            # going. ⚠ It must outrank the context menu: a right-press that
            # is accepted here never reaches contextMenuEvent.
            self._reroute_line = _CutLine(
                self.mapToScene(event.position().toPoint()),
                QColor(theme.ACCENT))
            self.scene().addItem(self._reroute_line)
            event.accept()
            return
        if (event.button() == Qt.LeftButton
                and event.modifiers() & Qt.ControlModifier):
            # Blender's Cut Links (Marty, 2026-08-07): Ctrl+drag draws a
            # dashed line and every noodle it crosses dies on release. It
            # outranks socket grabs and node moves — same as Blender, where
            # the cut starts wherever you press while Ctrl is held. (The
            # price, also same as Blender: Ctrl+click no longer toggles
            # selection — the rubber band is the multi-select.)
            self._cut_line = _CutLine(
                self.mapToScene(event.position().toPoint()))
            self.scene().addItem(self._cut_line)
            event.accept()
            return
        if event.button() == Qt.LeftButton:
            hit = self.scene().socket_under(
                self.mapToScene(event.position().toPoint()))
            if hit is not None:
                item, kind, index = hit
                # Dragging FROM an input is turned around into "from
                # whatever feeds it" in Blender; here, simply start at the
                # socket either way and let release decide the direction.
                self._drag_src = (item, kind, index)
                colour = (item.outputs[index][1] if kind == "out"
                          else item.inputs[index][1])
                self._drag_wire = _DragWire(
                    item.socket_pos(kind, index), QColor(colour),
                    self.scene().curving)
                self.scene().addItem(self._drag_wire)
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._pan_from is not None:
            # ⚠ Scrollbars only take ints, and a mouse at a high poll rate
            # hands out sub-pixel deltas — truncating each one threw away up
            # to a pixel PER EVENT, which is why panning felt steppy (Marty,
            # 2026-08-07). Carry the fraction to the next event instead: the
            # canvas then tracks the hand exactly.
            delta = event.position() - self._pan_from + self._pan_carry
            self._pan_from = event.position()
            ix, iy = int(delta.x()), int(delta.y())
            self._pan_carry = QPointF(delta.x() - ix, delta.y() - iy)
            self.horizontalScrollBar().setValue(
                self.horizontalScrollBar().value() - ix)
            self.verticalScrollBar().setValue(
                self.verticalScrollBar().value() - iy)
            event.accept()
            return
        if self._cut_line is not None:
            # No modifier re-check: the tool armed at press, releasing Ctrl
            # mid-drag keeps cutting — Blender behaves the same way.
            self._cut_line.extend(
                self.mapToScene(event.position().toPoint()))
            event.accept()
            return
        if self._reroute_line is not None:
            self._reroute_line.extend(
                self.mapToScene(event.position().toPoint()))
            event.accept()
            return
        if self._drag_wire is not None:
            self._drag_wire.follow(
                self.mapToScene(event.position().toPoint()))
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MiddleButton and self._pan_from is not None:
            self._pan_from = None
            self.unsetCursor()
            event.accept()
            return
        if event.button() == Qt.LeftButton and self._cut_line is not None:
            cut = self._cut_line
            self._cut_line = None
            scene = self.scene()
            scene.removeItem(cut)
            # A Ctrl+CLICK is not a cut (Blender needs a real drag too).
            # The path is in scene units, so the ~4-view-px threshold is
            # divided by the zoom.
            if cut.path().length() > 4.0 / max(self.zoom_factor(), 1e-6):
                for wire in scene.wires_crossing(cut.path()):
                    scene.remove_wire(wire)
            event.accept()
            return
        if event.button() == Qt.RightButton and self._reroute_line is not None:
            line = self._reroute_line
            self._reroute_line = None
            scene = self.scene()
            scene.removeItem(line)
            if line.path().length() > 4.0 / max(self.zoom_factor(), 1e-6):
                for wire in scene.wires_crossing(line.path()):
                    scene.split_with_reroute(wire, line.path())
            event.accept()
            return
        if self._drag_wire is not None:
            scene = self.scene()
            scene.removeItem(self._drag_wire)
            self._drag_wire = None
            src_item, src_kind, src_idx = self._drag_src
            self._drag_src = None
            want = "in" if src_kind == "out" else "out"
            hit = scene.socket_under(
                self.mapToScene(event.position().toPoint()), kind=want)
            if hit is not None and hit[0] is not src_item:
                if src_kind == "out":
                    src, dst = (src_item, "out", src_idx), hit
                else:
                    src, dst = hit, (src_item, "in", src_idx)
                # ⚠ COLOUR IS TYPE: a green output only enters a green
                # input (Marty, 2026-08-07). Refusing at the release is
                # better than an error at bake time — a wire you cannot
                # draw is a mistake you cannot make.
                if sockets_compatible(src, dst):
                    # The TYPED end decides the colour. Dragging out of an
                    # untyped reroute means the dot takes the colour of
                    # what it lands on, not its own placeholder.
                    src_bare = (isinstance(src[0], RerouteItem)
                                and not src[0].typed)
                    dst_bare = (isinstance(dst[0], RerouteItem)
                                and not dst[0].typed)
                    colour = (socket_colour(*dst)
                              if src_bare and not dst_bare
                              else socket_colour(*src))
                    for end in (src[0], dst[0]):
                        if isinstance(end, RerouteItem) and not end.typed:
                            end.adopt(colour)
                    scene.add_wire(src, dst, colour)
                else:
                    self._refuse_wire(src, dst)
            event.accept()
            return
        super().mouseReleaseEvent(event)


    def _refuse_wire(self, src, dst):
        """Say why a drag did not connect. Silence would read as a missed
        socket, and the user would try the same wrong wire again."""
        owner = self.parent()
        if not hasattr(owner, "set_status"):
            return

        def name(end):
            item = end[0]
            seq = item.outputs if end[1] == "out" else item.inputs
            label = seq[end[2]][0] if end[2] < len(seq) else "?"
            return "%s's %s" % (getattr(item, "title", "Reroute"), label)

        owner.set_status("%s and %s are different socket types — a wire "
                         "only joins sockets of the same colour"
                         % (name(src), name(dst)))


class _CutLine(QGraphicsPathItem):
    """The dashed trail while a Ctrl+drag link-cut is in flight. Rides above
    everything (nodes are z0, wires z-1) so the cut is visible over a node.

    Shift+right-drag (Add Reroute, 2026-08-08) borrows the same trail in
    the ACCENT colour — one gesture shape, two meanings, told apart by
    colour rather than by a second widget."""

    def __init__(self, start, colour=None):
        super().__init__()
        self.start = start
        pen = QPen(QColor(colour or "#e6e9ee"), 1.4, Qt.DashLine)
        pen.setCosmetic(True)
        self.setPen(pen)
        self.setZValue(3)

    def extend(self, pos):
        path = self.path()
        if path.elementCount() == 0:
            path = QPainterPath(self.start)
        path.lineTo(pos)
        self.setPath(path)


class _DragWire(QGraphicsPathItem):
    """The provisional noodle while a connect-drag is in flight."""

    def __init__(self, anchor, colour, curving):
        super().__init__()
        self.anchor = anchor
        self.curving = curving
        pen = QPen(colour, 1.7, Qt.DashLine)
        self.setPen(pen)
        self.setZValue(-1)

    def follow(self, pos):
        path = QPainterPath(self.anchor)
        if self.curving <= 0:
            path.lineTo(pos)
        else:
            off = (self.curving / 10.0) * max(
                30.0, abs(pos.x() - self.anchor.x()) * 0.55)
            path.cubicTo(self.anchor + QPointF(off, 0),
                         pos - QPointF(off, 0), pos)
        self.setPath(path)


# ---------------------------------------------------------------- the graph


def build_test_graph(scene):
    """The placeholder graph: enough of every mechanism to judge the canvas.
    Two reroute dots gather the pair of noodles heading into the solver
    (Marty's "connectors that join two wires that go into a node"), and the
    Blend node's input is a MULTI-INPUT socket carrying two wires."""
    n_in = NodeItem("Armature in", theme.TYPE_COLORS["pose"],
                    rows=[("Rig", "Eve_Rig")],
                    outputs=[("Bones", COL_BONES), ("Geometry", COL_GEO)])
    n_in.setPos(-420, -140)

    n_solver = NodeItem("Spring solver", theme.TYPE_COLORS["anim"],
                        rows=[("Stiffness", "0.35"), ("Damping", "0.18"),
                              ("Gravity", "1.0")],
                        inputs=[("Bones", COL_BONES), ("Geometry", COL_GEO)],
                        outputs=[("Motion", COL_MOTION)])
    n_solver.setPos(-60, -170)

    n_blend = NodeItem("Blend", theme.TYPE_COLORS["shapes"],
                       rows=[("Mode", "Mix")],
                       inputs=[("Motion", COL_MOTION, True)],   # multi-input
                       outputs=[("Result", COL_MOTION)])
    n_blend.setPos(-60, 40)

    n_apply = NodeItem("Apply", theme.TYPE_COLORS["set"],
                       rows=[("Target", "viewport"), ("Extra", "blend")],
                       inputs=[("Motion", COL_MOTION),
                               ("Extra", COL_MOTION)])
    n_apply.setPos(300, -80)

    r1 = RerouteItem(COL_BONES)
    r1.setPos(-210, -110)
    r2 = RerouteItem(COL_GEO)
    r2.setPos(-210, -80)

    for item in (n_in, n_solver, n_blend, n_apply, r1, r2):
        scene.addItem(item)

    scene.add_wire((n_in, "out", 0), (r1, "in", 0), COL_BONES)
    scene.add_wire((r1, "out", 0), (n_solver, "in", 0), COL_BONES)
    scene.add_wire((n_in, "out", 1), (r2, "in", 0), COL_GEO)
    scene.add_wire((r2, "out", 0), (n_solver, "in", 1), COL_GEO)
    scene.add_wire((n_solver, "out", 0), (n_apply, "in", 0), COL_MOTION)
    # the multi-input: two wires landing on ONE socket
    scene.add_wire((n_solver, "out", 0), (n_blend, "in", 0), COL_MOTION)
    scene.add_wire((r1, "out", 0), (n_blend, "in", 0), COL_BONES)
    scene.add_wire((n_blend, "out", 0), (n_apply, "in", 1), COL_MOTION)

    return {"in": n_in, "solver": n_solver, "blend": n_blend,
            "apply": n_apply, "r1": r1, "r2": r2}


# ----------------------------------------------------------------- the tab


class _BakeTask(QThread):
    """One bridge call off the GUI thread — the nsfw._Task shape. A bake
    legitimately takes what it takes; inline it would freeze the window
    (docs\\app-shell.md, the long-command rule)."""

    done = Signal(object)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self._fn = fn

    def run(self):
        try:
            self.done.emit({"ok": True, "result": self._fn()})
        except Exception as err:
            self.done.emit({"ok": False, "error": str(err)})


class NodeEditorTab(QWidget):
    """The whole tab: toolbar (+ Add node, status, zoom readout) over the
    view, carrying the BAKE GRAPH — the first real node set (2026-08-07).
    `bridge`/`window` may be None (suites, previews): the canvas works
    fully offline, only pressing Bake needs Blender."""

    def __init__(self, bridge=None, window=None, parent=None):
        super().__init__(parent)
        import bakenodes   # function-level: bakenodes imports this module
        self._bakenodes = bakenodes
        self.bridge = bridge
        self._window = window
        # One bake at a time, but a RUN may be many bakes (all slots, bulk):
        # the queue holds the still-pending items, results collect per item,
        # and `driver` is the node wearing the progress strip.
        self._bake_task = None
        self._bake_queue = []
        self._bake_total = 0
        self._bake_results = []
        self._bake_current = None
        self._bake_driver = None
        self._bake_out_node = None
        self._bake_skipped = 0
        # "Replace shader" on the Output node: bound when the queue starts,
        # spent as ONE more task after the queue drains (see _finish_queue).
        self._bake_replace = False
        self._bake_replace_all = False
        self._replace_note = None
        self._marquee = QTimer(self)
        self._marquee.setInterval(60)
        self._marquee.timeout.connect(self._tick_marquee)
        self.scene = NodeScene(self)
        self.canvas = NodeView(self.scene, self)
        self.nodes = bakenodes.build_bake_graph(self.scene, self)
        for node in self.nodes.values():
            self._apply_remembered(node)

        bar = QHBoxLayout()
        bar.setContentsMargins(8, 6, 8, 2)
        add = QToolButton()
        add.setText("+ Add node")
        add.setPopupMode(QToolButton.InstantPopup)
        menu = QMenu(add)
        for kind in bakenodes.NODE_KINDS:
            menu.addAction(kind, lambda k=kind: self.add_bake_node(k))
        menu.addAction("Reroute dot", lambda: self.add_bake_node("Reroute"))
        add.setMenu(menu)
        bar.addWidget(add)

        # ⚠ ELIDED ON PURPOSE. As a plain QLabel this one line reported a
        # 1944 px minimum width, and because a QStackedWidget's minimum is
        # its widest page it set the floor for the WHOLE app window at
        # 2194 px — Marty could not narrow the app at all (2026-08-08).
        # widgets.ElidedLabel keeps the text in a tooltip and lets the
        # window shrink.
        hint = widgets.ElidedLabel(
            "Shift+A adds a node at the cursor, click a value to set it, "
            "drag a socket to rewire, Ctrl-drag cuts wires, Shift+right-drag "
            "adds reroutes, Del removes, wheel or Ctrl +/− zooms, "
            "middle-mouse pans")
        hint.setStyleSheet("color: %s;" % theme.TEXT_DIM)
        bar.addWidget(hint, 1)
        bar.addStretch(1)

        self.status_label = QLabel("")
        self.status_label.setStyleSheet("color: %s;" % theme.TEXT_HEAD)
        bar.addWidget(self.status_label)

        self.zoom_label = QLabel("100%")
        self.zoom_label.setStyleSheet("color: %s;" % theme.TEXT_DIM)
        self.canvas.zoomChanged.connect(
            lambda pct: self.zoom_label.setText("%d%%" % pct))
        bar.addWidget(self.zoom_label)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addLayout(bar)
        lay.addWidget(self.canvas, 1)

    def showEvent(self, event):
        """⚠ Ctrl +/− is a VIEW key handler, so it does nothing until the
        canvas holds focus — and nobody clicks an empty canvas before
        reaching for a shortcut. Take focus when the tab appears (Marty hit
        exactly this, 2026-08-06). Qt only delivers this once the parent
        window is up, which is why it is not done in __init__."""
        super().showEvent(event)
        self.canvas.setFocus(Qt.OtherFocusReason)

    def set_capture_busy(self, busy):
        """MainWindow greys every tool page through this while Blender is
        busy. The canvas itself stays live — dragging nodes mid-capture is
        harmless and the Bake button already refuses while a bake (or any
        capture) runs — but the hook must exist: _pages() lists this tab
        like every other tool tab, and the caller does not special-case
        anyone."""

    def hideEvent(self, event):
        """Leaving the tab is the natural "last time" moment — snapshot the
        node settings then (a no-op unless the tickbox is on)."""
        self._snapshot_settings()
        super().hideEvent(event)

    def set_status(self, text):
        self.status_label.setText(text or "")

    def add_bake_node(self, kind, pos=None):
        """Drop another node — at `pos` (Shift+A puts the cursor position
        here) or the middle of the current view."""
        bn = self._bakenodes
        if pos is None:
            pos = self.canvas.mapToScene(
                self.canvas.viewport().rect().center())
        maker = bn.NODE_MAKERS.get(kind)
        if maker is not None:
            item = maker(self)
            self._apply_remembered(item)
        else:
            item = RerouteItem()
        item.setPos(pos)
        self.scene.addItem(item)
        return item

    def open_add_menu(self, global_pos, scene_pos):
        """The Shift+A menu — same list as the toolbar's "+ Add node"
        (both read bakenodes.NODE_KINDS), the node lands at the cursor."""
        menu = QMenu(self)
        for kind in self._bakenodes.NODE_KINDS:
            menu.addAction(kind,
                           lambda k=kind, p=scene_pos: self.add_bake_node(k, p))
        menu.addAction("Reroute dot",
                       lambda p=scene_pos: self.add_bake_node("Reroute", p))
        menu.exec(global_pos)

    # -------------------------------------------- remember node settings

    def _node_cfg(self):
        """The config's nodeeditor group, or None when there is no config
        to read (standalone canvases in the suites)."""
        cfg = getattr(self._window, "cfg", None)
        if not isinstance(cfg, dict):
            return None
        return cfg.setdefault("nodeeditor", {})

    def _remember_enabled(self):
        group = self._node_cfg()
        return bool(group and group.get("remember"))

    def _apply_remembered(self, node):
        """Pre-fill a node with the last-used values of its type — only
        when the settings tickbox is on, and never fatally (a stale dict
        from an older build must degrade to defaults, not crash the tab)."""
        if not self._remember_enabled():
            return
        stored = (self._node_cfg().get("last") or {}).get(type(node).__name__)
        apply = getattr(node, "apply_settings", None)
        if stored and apply:
            try:
                apply(dict(stored))
            except Exception:
                pass

    def _snapshot_settings(self):
        """Store every settings-carrying node's current values, per node
        type (the last of a type wins). Called when a bake starts and when
        the tab hides — the two moments "last time" can mean."""
        if not self._remember_enabled():
            return
        group = self._node_cfg()
        last = group.setdefault("last", {})
        for item in self.scene.items():
            getter = getattr(item, "settings_dict", None)
            if getter:
                try:
                    last[type(item).__name__] = getter()
                except Exception:
                    pass
        saver = getattr(self._window, "save_config", None)
        if callable(saver):
            saver()
        else:
            config.save(getattr(self._window, "cfg"))

    # ------------------------------------------------------------- baking

    def bake_running(self):
        return self._bake_task is not None or bool(self._bake_queue)

    def material_names(self):
        """Live material list for the Bake node's pill, or None when
        Blender can't answer. A click-driven read, sub-second — inline is
        the right call here (the pose-apply precedent, docs\\app-shell.md)."""
        if self.bridge is None:
            return None
        try:
            if not self.bridge.supports("list_materials"):
                return None
            reply = self.bridge.list_materials()
            return [m["name"] for m in reply.get("materials", [])]
        except Exception:
            return None

    def collection_names(self):
        """Collections for the Bulk bake node's folder picker (name, depth,
        bakeable-mesh count), or None when Blender can't answer — the
        material_names shape."""
        if self.bridge is None:
            return None
        try:
            if not self.bridge.supports("list_collections"):
                return None
            reply = self.bridge.list_collections()
            return list(reply.get("collections") or [])
        except Exception:
            return None

    def run_bake(self, settings_node, driver=None):
        """Resolve the chain from THIS Bake-settings node through the wires
        and run it. Every parameter is bound before any thread starts
        (app-shell: a worker must never read live UI state when it happens
        to run) — with "Bake all slots" the slot list is resolved HERE too,
        by the add-on, so the queue cannot drift from what bake_texture
        would pick.

        ⚠ THE CHAIN IS THE PERMISSION (Marty, 2026-08-07): a source node —
        Bake or Bulk bake — cannot bake by itself, and this node cannot
        bake without an Output image node downstream. Both refusals name
        the missing wire."""
        bn = self._bakenodes
        if self.bake_running():
            return
        if self.bridge is None:
            self.set_status("No bridge — start Blender's add-on first")
            return
        reason = self.bridge.feature_reason("texture_bake")
        if reason:
            self.set_status(reason)
            return
        sources, map_set = bn.upstream_sources(self.scene, settings_node)
        out = bn.downstream_node(self.scene, settings_node, bn.OutputImageNode)
        if not sources:
            self.set_status("Connect a Bake, Bulk bake or Collection node to "
                            "the Bake settings input")
            return
        if out is None:
            self.set_status("Connect an Output image node to the Bake "
                            "settings output")
            return
        if getattr(out, "replace_shader", False):
            # Refuse BEFORE the run is spent, like the all-off contributions
            # rule — finding out the shaders cannot be swapped AFTER a bulk
            # bake is the worst moment to hear it.
            reason = self.bridge.feature_reason("bake_replace")
            if reason:
                self.set_status(reason)
                return
        bake_type = bn.BAKE_ENUM[settings_node.bake_type]
        # A Map set node in the chain OVERRIDES the Type row: every ticked
        # type is baked for every target the source found (2026-08-08). It
        # overrides nothing else — resolution, margin and the rest still
        # come from this node, the way a bulk run takes them from here too.
        types = [bake_type]
        if map_set is not None:
            types = map_set.types()
            if not types:
                self.set_status("Tick at least one map on the Map set node")
                return
        width, height = settings_node.width_px, settings_node.height_px
        # The whole Bake panel, bound as one block (0.29.0 — the native
        # rebuild). No per-type filtering here: the add-on applies
        # Blender's own visibility rules (View From, margin, influence)
        # to whatever arrives, exactly as the panel would.
        options = dict(samples=settings_node.samples,
                       margin=settings_node.margin,
                       margin_type=settings_node.margin_type,
                       use_clear=settings_node.use_clear,
                       target=settings_node.target,
                       view_from=settings_node.view_from,
                       pass_filter=settings_node.pass_filter(),
                       normal_space=settings_node.normal_space,
                       normal_swizzle=list(settings_node.swizzle),
                       use_selected_to_active=(
                           settings_node.selected_to_active),
                       use_cage=settings_node.use_cage,
                       cage_object=settings_node.cage_object or None,
                       cage_extrusion=settings_node.cage_extrusion,
                       max_ray_distance=settings_node.max_ray_distance)
        for etype in types:
            # Blender lets you untick everything and bakes black; say so
            # here rather than spend a minute proving it. With a Map set the
            # check runs per type, since each one offers its own set.
            filter_for = settings_node.pass_filter_for(etype)
            if filter_for is not None and not filter_for:
                self.set_status(
                    "Every contribution is off — switch one on or the map "
                    "bakes black" if len(types) == 1 else
                    "Every contribution is off for %s — switch one on or "
                    "that map bakes black" % etype.title())
                return
        # Every source's targets, merged. ⚠ A (object, material) pair that
        # two sources both name is baked ONCE — a Bake node and a Collection
        # that contains it would otherwise queue the same map twice and the
        # second would overwrite the first for no gain.
        pairs, skipped, refusals = [], 0, []

        def merge(row):
            """Add one (object, material) row, unless another source has
            already covered it.

            ⚠ The Bake node names a MATERIAL with no object (the add-on
            resolves one at bake time); a Collection names the same
            material WITH the object it found. Those are the same bake, so
            keying on the pair alone queued it twice — caught by the suite
            (Moss, Moss). The object-qualified row is the more specific
            one and WINS, whichever order they arrive in."""
            for index, have in enumerate(pairs):
                if have["material"] != row["material"]:
                    continue
                if have.get("object") == row.get("object"):
                    return
                if row.get("object") and not have.get("object"):
                    pairs[index] = row       # specific beats unqualified
                    return
                if have.get("object") and not row.get("object"):
                    return
            pairs.append(row)
        for source in sources:
            rows, extra, refusal = self._targets_from(source)
            if refusal:
                # ⚠ ONE source's refusal must not sink the others now that
                # the socket is MULTI. The starting graph ships an UNPICKED
                # Bake node wired in, so wiring a Collection or Bulk bake
                # node beside it would otherwise answer "Pick a material on
                # the Bake node" forever. A lone source still refuses in its
                # own words — that wording is pinned.
                # ⚠ And when nothing at all can run, the refusal shown is
                # the one belonging to the node whose BUTTON was pressed.
                # Without that, pressing Bulk bake with an empty selection
                # answered "Pick a material on the Bake node" — a complaint
                # about a node the user did not touch.
                refusals.insert(0 if source is driver else len(refusals),
                                refusal)
                continue
            skipped += extra
            for row in rows:
                merge(row)
        if not pairs:
            self.set_status(refusals[0] if refusals else
                            "Nothing bakeable is wired into this Bake "
                            "settings node")
            return
        explicit = out.out_path
        folder = None
        if len(pairs) * len(types) > 1 and explicit:
            # N maps cannot share one filename — an explicit path lends its
            # FOLDER and the names go automatic, which the status line says.
            folder = os.path.dirname(explicit) or None
            explicit = None
        # Two objects sharing a material would auto-name the SAME file, so
        # those get the object folded in. Counted across the WHOLE merged
        # list, which is what makes the rule hold when a Bake node and a
        # Collection contribute the same material from different objects.
        mat_count = {}
        for row in pairs:
            mat_count[row["material"]] = mat_count.get(row["material"], 0) + 1
        items = []
        for row in pairs:
            mat = row["material"]
            stem = (mat if mat_count[mat] == 1 or not row.get("object")
                    else "%s_%s" % (mat, row["object"]))
            for etype in types:
                # one type = the name it always had; a Map set folds the
                # type in, or its maps would overwrite each other
                name = stem if len(types) == 1 else "%s_%s" % (stem,
                                                               etype.lower())
                path = explicit or bn.auto_out_path(name, folder)
                opts = dict(options)
                opts["pass_filter"] = settings_node.pass_filter_for(etype)
                items.append(dict(material=mat, bake_type=etype,
                                  width=width, height=height, out_path=path,
                                  object_name=row.get("object"), options=opts))
        self._bake_skipped = skipped
        self._snapshot_settings()
        self._start_queue(items, driver=driver or settings_node, out_node=out,
                          label=self._run_label(items, sources, map_set,
                                                pairs))

    def _run_label(self, items, sources, map_set, pairs):
        """What the busy indicator calls this run. ⚠ The single-map and
        all-slots wordings PREDATE the merge and are pinned by the suite —
        one source of one kind must still read exactly as it always did."""
        bn = self._bakenodes
        if len(items) == 1:
            return items[0]["material"]
        if (len(sources) == 1 and map_set is None
                and isinstance(sources[0], bn.BakeTargetNode)):
            return "%d slots of %s" % (len(items), pairs[0].get("object"))
        if len(sources) == 1 and isinstance(sources[0], bn.BakeTargetNode):
            return "%d maps of %s" % (len(items),
                                      pairs[0].get("object")
                                      or pairs[0]["material"])
        return "%d maps" % len(items)

    def _targets_from(self, source):
        """One source's (object, material) rows — `(rows, skipped, refusal)`.

        Every source resolves its targets HERE, at press time, never in the
        worker: the optimizer's queued-"SELECTED" lesson. A refusal string
        means stop and say so; it is never a raise, because one unpicked
        node should read as a sentence, not a traceback."""
        bn = self._bakenodes
        if isinstance(source, bn.BakeTargetNode):
            if not source.material:
                return [], 0, "Pick a material on the Bake node"
            if not source.all_slots:
                return [{"object": None, "material": source.material}], 0, None
            reason = self.bridge.feature_reason("bake_all_slots")
            if reason:
                return [], 0, reason
            try:
                reply = self.bridge.bake_targets("material",
                                                 material=source.material)
            except Exception as err:
                return [], 0, "Could not list the slots: %s" % err
            rows = reply.get("targets") or []
            if not rows or not rows[0].get("materials"):
                return [], 0, ("No bakeable object uses '%s' — it needs a "
                               "mesh with UVs" % source.material)
            return ([{"object": rows[0]["object"], "material": m}
                     for m in rows[0]["materials"]], 0, None)

        if isinstance(source, bn.CollectionNode):
            if not source.collection:
                return [], 0, "Pick a collection on the Collection node"
            reason = self.bridge.feature_reason("bulk_bake")
            if reason:
                return [], 0, reason
            try:
                reply = self.bridge.bake_targets(
                    "collection", collection=source.collection)
            except Exception as err:
                return [], 0, "Could not list bake targets: %s" % err
            return self._rows_of(reply, "collection '%s'" % source.collection)

        # BulkBakeNode: the viewport selection, or its own named collection
        reason = self.bridge.feature_reason("bulk_bake")
        if reason:
            return [], 0, reason
        if source.mode == "COLLECTION" and not source.collection:
            return [], 0, "Pick a collection on the Bulk bake node"
        try:
            if source.mode == "COLLECTION":
                reply = self.bridge.bake_targets(
                    "collection", collection=source.collection)
            else:
                reply = self.bridge.bake_targets("selected")
        except Exception as err:
            return [], 0, "Could not list bake targets: %s" % err
        where = ("collection '%s'" % source.collection
                 if source.mode == "COLLECTION" else "the selection")
        return self._rows_of(reply, where)

    @staticmethod
    def _rows_of(reply, where):
        """A bake_targets reply flattened to (object, material) rows."""
        targets = list(reply.get("targets") or [])
        skipped = int(reply.get("skipped") or 0)
        if not targets:
            extra = (" (%d item(s) had no materials or UVs)" % skipped
                     if skipped else "")
            return [], skipped, "Nothing bakeable in %s%s" % (where, extra)
        rows = [{"object": t["object"], "material": mat}
                for t in targets for mat in (t.get("materials") or [])]
        return rows, skipped, None

    def run_bulk_bake(self, bulk_node):
        """The Bulk bake node's button. It cannot bake on its own (Marty,
        2026-08-07): it finds the Bake-settings node its green wire feeds
        and presses THAT — so a bulk run gets the same type, resolution and
        options as a single one, from one place."""
        bn = self._bakenodes
        if self.bake_running():
            return
        settings = bn.downstream_node(self.scene, bulk_node,
                                      bn.BakeSettingsNode)
        if settings is None:
            self.set_status("Connect this Bulk bake node to a Bake settings "
                            "node — that is where the type, resolution and "
                            "options live")
            return
        sources, _map_set = bn.upstream_sources(self.scene, settings)
        if bulk_node not in sources:
            # Its wire reaches the settings node, but something else holds
            # that node's input — press the button on the one that wins.
            # ⚠ Since the input went MULTI this can only mean the wire does
            # not actually land there (a stale reroute, say); being one of
            # SEVERAL sources is now legal and bakes them all.
            self.set_status("That Bake settings node is fed by another "
                            "node — rewire it to this Bulk bake first")
            return
        # the strip belongs on the button that was pressed, not on the
        # settings node it delegates to
        self.run_bake(settings, driver=bulk_node)

    # ---------------------------------------------------- the bake queue

    def _start_queue(self, items, driver, out_node, label):
        for item in items:
            parent = os.path.dirname(item.get("out_path") or "")
            if parent:
                try:
                    os.makedirs(parent, exist_ok=True)
                except OSError:
                    pass    # the add-on makedirs too; its error names the path
        self._bake_queue = list(items)
        self._bake_total = len(items)
        self._bake_results = []
        self._bake_driver = driver
        self._bake_out_node = out_node
        # bound HERE, before any thread starts, like every other parameter:
        # the tickbox may be clicked while the queue runs.
        self._bake_replace = bool(getattr(out_node, "replace_shader", False))
        self._bake_replace_all = bool(
            getattr(out_node, "replace_all_slots", False))
        self._replace_note = None
        if self._window is not None:
            self._window.begin_capture(label, verb="baking")
        self._marquee.start()
        self._run_next()

    def _run_next(self):
        done = self._bake_total - len(self._bake_queue)
        if self._bake_driver is not None:
            # one bake = marquee (it takes what it takes); a queue = a real
            # fraction, nudged off zero so the strip is visible from item 1
            self._bake_driver.set_progress(
                -1.0 if self._bake_total == 1
                else max(0.02, done / float(self._bake_total)))
        if not self._bake_queue:
            self._finish_queue()
            return
        item = self._bake_queue.pop(0)
        self._bake_current = item
        if self._bake_total > 1:
            self.set_status("Baking %s (%d of %d)…"
                            % (item["material"], done + 1, self._bake_total))
        else:
            self.set_status("Baking %s…" % item["material"])
        task = _BakeTask(lambda it=item: self.bridge.bake_texture(
            it["material"], it["bake_type"], it["width"], it["height"],
            out_path=it["out_path"], object_name=it.get("object_name"),
            **it["options"]),
            parent=self)
        task.done.connect(self._item_done)
        self._bake_task = task
        task.start()
        self.scene.update()

    def _item_done(self, payload):
        self._bake_task = None
        item = self._bake_current
        self._bake_current = None
        if payload.get("ok"):
            self._bake_results.append((item, payload["result"] or {}, None))
        else:
            # One failed map must not sink the rest of a bulk run — record
            # it and keep going; the summary names the first failure.
            self._bake_results.append((item, None,
                                       str(payload.get("error"))))
        self._run_next()

    @staticmethod
    def _stale_options_warning(reply):
        """⚠ The bake options are GROWN PARAMETERS — an older add-on
        accepts them, ignores the ones it doesn't know and bakes its old
        way. The reply's echo is the only way to know (the save_abc rule),
        so a missing echo must not be silent — and must not swallow a real
        warning either. Two tiers: no `options` block at all = pre-0.25;
        an `options` block without `target` = pre-0.29, which still bakes
        with margin 0 + hand padding instead of Blender's own margin."""
        options = reply.get("options")
        warning = reply.get("warning")
        if options is not None and "target" in options:
            return warning
        if options is None:
            stale = ("this add-on ignored the bake options (samples, "
                     "contributions, margin) — update the extension "
                     "from ⚙ Library Settings")
        else:
            stale = ("this add-on bakes the OLD way (pre-0.29: margin 0 + "
                     "hand padding, not Blender's own margin) — update the "
                     "extension from ⚙ Library Settings")
        return stale if not warning else stale + "; " + warning

    def _replace_items(self):
        """One row per map that really landed, built from the REPLY rather
        than from what was asked for — the add-on echoes the object it
        actually baked and the path it actually wrote (extension included),
        which is exactly what the replacement has to point at."""
        rows, seen = [], set()
        for _item, reply, err in self._bake_results:
            if err is not None or not reply or not reply.get("path"):
                continue
            # ⚠ ONE row per (object, material). A Map set run bakes several
            # types into the same material, and the add-on reuses its own
            # image node — so without this the material would be rewired
            # once per type and whichever finished LAST would decide what
            # the shader shows. The first ticked type wins instead, which
            # is Blender's own type order and therefore predictable.
            key = (reply.get("object"), reply.get("material"))
            if key in seen:
                continue
            seen.add(key)
            rows.append({"object": reply.get("object"),
                         "material": reply.get("material"),
                         "path": reply.get("path"),
                         "bake_type": reply.get("bake_type")})
        return rows

    def _replace_done(self, payload):
        """The replace pass came back — keep its note and close the run. The
        maps are on disk either way, so a failure here narrows the summary,
        it does not turn a good bake into a failed one."""
        self._bake_task = None
        if payload.get("ok"):
            reply = payload.get("result") or {}
            count = int(reply.get("count") or 0)
            note = "%d shader%s replaced" % (count, "" if count == 1 else "s")
            skipped = reply.get("skipped") or []
            if skipped:
                note += " · %d not replaced (%s)" % (
                    len(skipped), skipped[0].get("reason", "?"))
            if self._bake_replace_all and "all_slots" not in reply:
                # ⚠ `all_slots` is a GROWN parameter — an add-on older than
                # 0.30.0 accepts it, ignores it and replaces only the baked
                # materials. The echo is the only way to know (the save_abc
                # rule), so a missing one says so instead of quietly doing
                # less than the tickbox promised.
                note += (" · ⚠ this add-on ignored All slots (needs 0.30.0) "
                         "— update the extension from ⚙ Library Settings")
            self._replace_note = note
        else:
            self._replace_note = "replace failed: %s" % payload.get("error")
        self._finish_queue()

    def _finish_queue(self):
        # ⚠ The replace pass belongs INSIDE the run: one more worker task,
        # the same capture bracket, before any summary is written — and only
        # once the whole queue has drained, because swapping a slot while
        # later maps of the same object are still baking would change the
        # scene under them.
        if self._bake_replace:
            self._bake_replace = False      # once per run, whatever happens
            items = self._replace_items()
            if items:
                self.set_status("Replacing %d shader%s with the baked map…"
                                % (len(items), "" if len(items) == 1 else "s"))
                all_slots = self._bake_replace_all
                task = _BakeTask(
                    lambda rows=items, a=all_slots:
                        self.bridge.apply_baked_material(rows, all_slots=a),
                    parent=self)
                task.done.connect(self._replace_done)
                self._bake_task = task
                task.start()
                return
        self._marquee.stop()
        driver = self._bake_driver
        self._bake_driver = None
        if driver is not None:
            driver.set_progress(None)
        if self._window is not None:
            self._window.end_capture()
        out = self._bake_out_node
        self._bake_out_node = None
        results = self._bake_results
        self._bake_results = []
        skipped = self._bake_skipped
        self._bake_skipped = 0
        replaced = self._replace_note
        self._replace_note = None

        def say(text):
            """The summary, with what the replace pass did folded in — the
            existing wording is left exactly as it was when the tickbox is
            off (the suite pins it)."""
            self.set_status(text + (" · " + replaced if replaced else ""))

        oks = [(item, r) for item, r, err in results if err is None]
        errs = [(item, err) for item, r, err in results if err is not None]
        if oks:
            last_item, last = oks[-1]
            note = "%s · %ss · %s" % (last.get("bake_type", "?"),
                                      last.get("seconds", "?"),
                                      last.get("device", "?"))
            if self._stale_options_warning(last):
                # the node keeps the ⚠ on screen after the status line has
                # moved on — a warning you have to have been looking at is
                # not a warning
                note = "⚠ " + note
            if out is not None and last.get("path"):
                out.show_result(last["path"], note)
        if self._bake_total <= 1:
            # the single-map wording predates the queue and stays exact
            if errs:
                say("Bake failed: %s" % errs[0][1])
            elif oks:
                warning = self._stale_options_warning(oks[0][1])
                if warning:
                    # An empty map that says "Baked ✓" cost a real evening —
                    # the add-on measures its result and names the cause
                    # (no lights / transparent surface / wrong output).
                    say("⚠ " + warning)
                else:
                    r = oks[0][1]
                    if r.get("path"):
                        say("Baked %s → %s" % (
                            r.get("material", "?"),
                            os.path.basename(str(r.get("path", "?")))))
                    else:
                        # a color-attribute bake: no image, no file — the
                        # map landed on the mesh's vertices
                        say("Baked %s → color attribute '%s'" % (
                            r.get("material", "?"),
                            r.get("color_attribute", "?")))
        else:
            warn_count = sum(1 for _i, r in oks
                             if self._stale_options_warning(r))
            if oks and not any(r.get("path") for _i, r in oks):
                where = "color attributes"     # vertex bakes write no files
            else:
                where = (os.path.dirname(str(oks[-1][1].get("path")))
                         if oks and oks[-1][1].get("path")
                         else self._bakenodes.default_bake_dir())
            bits = ["Baked %d/%d maps → %s"
                    % (len(oks), self._bake_total, where)]
            if warn_count:
                bits.append("⚠ %d came back with warnings" % warn_count)
            if errs:
                bits.append("failed: %s (%s)"
                            % (errs[0][0]["material"], errs[0][1]))
            if skipped:
                bits.append("%d selected item(s) skipped — no mesh, "
                            "materials or UVs" % skipped)
            say(" · ".join(bits))
        self.scene.update()

    def _tick_marquee(self):
        driver = self._bake_driver
        if driver is not None:
            driver._marquee_phase = (driver._marquee_phase + 0.05) % 1.0
            driver.update()
