"""Rig properties — a rig's custom properties as CHANNELS, driven from here.

**Option C, "Channels"** (Marty picked it from four rendered mockups,
2026-08-19): one row per property — its value, WHERE its keys sit on the
timeline, and a keyframe diamond. Select several rows and key or un-key them
together; click a diamond in a strip to jump the playhead there.

Where the rest lives:
  * `blender_addon\\madi_anim_library\\rigprops.py` — the reads, the writes,
    and the measurements behind them.
  * `docs\\rigprops.md` — the module doc.

===========================================================================
⚠⚠ 775 ROWS IS THE WHOLE DESIGN PROBLEM. NOTHING HERE IS A WIDGET.
===========================================================================
A Daz rig carries **775 custom properties**. The obvious build — a slider, a
strip and a button per row via `setItemWidget` — is 2,325 live widgets. That
costs seconds to build, megabytes to hold, and it re-lays-out the whole tree
every time one value moves.

So the table holds **one `QTreeWidgetItem` per row and no child widgets at
all**: a single `QStyledItemDelegate` paints the value bar, the key strip and
the diamond, and Qt only ever asks it to paint the rows you can SEE (about
20). A value change repaints; it does not rebuild. `ChannelTable` turns clicks
into actions by mapping the mouse to (row, column) itself — which is also what
lets a drag on the value column change the value without touching the row
selection, so "select two rows, then key them" survives dragging a third.

⚠ **THE POLL SENDS WHAT IT ALREADY HAS.** `rig_props_list(rig, shape,
revision)` — the add-on answers `{"unchanged": True}` in ~60 bytes when
nothing moved, instead of the 97 KB the full answer costs. That is the
difference between this tab being free to leave open and being the most
expensive thing in the app. See the add-on module for the three tiers.

⚠ **`shape` IS AN ALIGNMENT CONTRACT, NOT A CACHE HINT.** `values` arrives as
a bare array lined up with the rows. `_apply` refuses a reply whose `count`
disagrees with the rows it holds and asks for a full read instead — drawing
775 values against the wrong 775 names would look plausible and be entirely
wrong.

⚠ **A DRAG WRITES AT MOST EVERY `WRITE_MS`.** The value is applied locally at
once (so the bar tracks the mouse) and sent on a timer, with the final value
always sent on release. Without that, one drag is a hundred blocking socket
round-trips.
"""

import time

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import (QAbstractItemView, QHBoxLayout, QHeaderView,
                               QInputDialog, QLabel, QLineEdit, QMessageBox,
                               QPushButton, QStyle, QStyledItemDelegate,
                               QToolButton, QTreeWidget, QTreeWidgetItem,
                               QVBoxLayout, QWidget)

import bridge as bridgemod
import icons
import theme
import widgets

TITLE = "Rig properties"
FEATURE = "rig_props"

# Same cadence as Organize and Markers: one poll per 1.5 s while the tab is on
# screen, none at all while it is not.
POLL_MS = 1500
# A dragged value is sent at most this often. 60 ms is under a frame at 15 fps
# of mouse movement and well over the round-trip, so the write never queues.
WRITE_MS = 60
# Typing in the filter re-sorts and re-lists 775 rows; a keystroke should not.
FILTER_MS = 150

# Blender's own field colours, and they are the point: someone who animates in
# Blender already reads green as "animated" and yellow as "keyed here".
ANIM = "#5bb96f"
KEYED = "#e3c64b"

STATE_NONE, STATE_ANIM, STATE_KEYED = 0, 1, 2
KEY_ICON = {STATE_NONE: "key_off", STATE_ANIM: "key_anim",
            STATE_KEYED: "key_on"}
STATE_COLOUR = {STATE_NONE: None, STATE_ANIM: ANIM, STATE_KEYED: KEYED}

COL_NAME, COL_VALUE, COL_KEYS, COL_KEY = 0, 1, 2, 3

# How near a diamond a click has to land to count as hitting it, in pixels.
# Generous on purpose: the diamonds are 9 px wide and the strip is the only
# place in the app where you aim at one.
HIT_PX = 6.0


def _icon(name, size=15, colour=None):
    return icons.icon(name, size, colour or theme.TEXT)


def _tint(colour, alpha):
    c = QColor(colour)
    c.setAlpha(alpha)
    return c


class ChannelDelegate(QStyledItemDelegate):
    """Paints the three drawn columns. ⚠ It reads the PAGE's arrays, never the
    item's data: 775 rows × 4 columns of `setData` per poll is exactly the
    per-row work this design exists to avoid."""

    def __init__(self, page, parent=None):
        super().__init__(parent)
        self.page = page

    def paint(self, painter, option, index):
        column = index.column()
        if column == COL_NAME:
            super().paint(painter, option, index)
            return
        row = index.data(Qt.UserRole)
        if row is None:
            super().paint(painter, option, index)
            return
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        # ⚠ THE DELEGATE HAS TO PAINT THE SELECTION ITSELF. Qt only draws it
        # for cells it renders, and this one renders three of the four — so a
        # selected row highlighted under its NAME and nowhere else, which on a
        # tab whose whole point is selecting several rows read as nothing
        # being selected at all. Seen by rendering the finished tab.
        if option.state & QStyle.State_Selected:
            painter.fillRect(option.rect, _tint(theme.ACCENT, 46))
        rect = QRectF(option.rect).adjusted(2, 2.5, -2, -2.5)
        if column == COL_VALUE:
            self._paint_value(painter, rect, row)
        elif column == COL_KEYS:
            self._paint_strip(painter, QRectF(option.rect), row)
        else:
            self._paint_diamond(painter, QRectF(option.rect), row)
        painter.restore()

    # ------------------------------------------------------------- value
    def _paint_value(self, painter, rect, row):
        page = self.page
        state = page.state_of(row)
        tint = STATE_COLOUR[state]
        painter.setPen(QPen(QColor(theme.BG)))
        painter.setBrush(QColor(theme.BG))
        painter.drawRoundedRect(rect, 3, 3)
        edge = QColor(theme.BORDER)
        base = QColor(theme.BG)
        if tint:
            base = _tint(tint, 40 if state == STATE_ANIM else 62)
            edge = _tint(tint, 210)
        painter.setPen(QPen(edge))
        painter.setBrush(base)
        painter.drawRoundedRect(rect, 3, 3)

        kind = page.kinds[row]
        value = page.values[row]
        if kind == "bool":
            box = QRectF(rect.x() + 5, rect.center().y() - 6, 12, 12)
            painter.setPen(QPen(QColor(theme.BORDER)))
            painter.setBrush(QColor(theme.PANEL2))
            painter.drawRoundedRect(box, 2, 2)
            if value:
                painter.setPen(QPen(QColor(theme.ACCENT), 2))
                painter.drawPolyline(QPolygonF([
                    QPointF(box.x() + 2.5, box.center().y()),
                    QPointF(box.center().x() - 0.5, box.bottom() - 3),
                    QPointF(box.right() - 2.5, box.y() + 3)]))
            painter.setPen(QPen(QColor(theme.TEXT)))
            painter.drawText(rect.adjusted(22, 0, -4, 0),
                             Qt.AlignVCenter | Qt.AlignLeft,
                             "On" if value else "Off")
            return

        low, high = page.bounds(row)
        if high > low:
            # ⚠ THE FILL STARTS AT ZERO when the range crosses it. A −1..1
            # morph sitting at 0 is OFF; a bar filled to the middle says it is
            # half on, which is what Blender's own slider is careful not to
            # say.
            zero = 0.0 if low < 0.0 < high else low
            span = high - low
            a = (min(zero, value) - low) / span
            b = (max(zero, value) - low) / span
            a = min(max(a, 0.0), 1.0)
            b = min(max(b, 0.0), 1.0)
            inner = rect.adjusted(1, 1, -1, -1)
            fill = QRectF(inner.x() + inner.width() * a, inner.y(),
                          inner.width() * (b - a), inner.height())
            if fill.width() > 0.5:
                painter.setPen(Qt.NoPen)
                painter.setBrush(_tint(theme.ACCENT, 120))
                painter.drawRoundedRect(fill, 2, 2)
        painter.setPen(QPen(QColor(theme.TEXT)))
        painter.drawText(rect, Qt.AlignCenter, page.text_of(row))

    # ------------------------------------------------------------- strip
    def _paint_strip(self, painter, rect, row):
        page = self.page
        frames = page.keys.get(page.names[row])
        mid = rect.center().y()
        painter.setPen(QPen(QColor(theme.BORDER), 1))
        painter.drawLine(QPointF(rect.x() + page.STRIP_PAD, mid),
                         QPointF(rect.right() - page.STRIP_PAD, mid))
        x = page.frame_x(rect, page.frame)
        painter.setPen(QPen(QColor(theme.ACCENT), 1.2))
        painter.drawLine(QPointF(x, rect.y() + 2),
                         QPointF(x, rect.bottom() - 2))
        if not frames:
            return
        for frame in frames:
            fx = page.frame_x(rect, frame)
            here = (frame == page.frame)
            colour = QColor(KEYED if here else ANIM)
            painter.setPen(QPen(colour.darker(140), 1))
            painter.setBrush(colour)
            size = 4.6 if here else 3.8
            painter.drawPolygon(QPolygonF([
                QPointF(fx, mid - size), QPointF(fx + size, mid),
                QPointF(fx, mid + size), QPointF(fx - size, mid)]))

    # ----------------------------------------------------------- diamond
    def _paint_diamond(self, painter, rect, row):
        state = self.page.state_of(row)
        colour = STATE_COLOUR[state] or theme.TEXT_DIM
        pixmap = icons.pixmap(KEY_ICON[state], 15, colour)
        size = pixmap.size() / pixmap.devicePixelRatio()
        painter.drawPixmap(
            int(rect.center().x() - size.width() / 2.0),
            int(rect.center().y() - size.height() / 2.0), pixmap)


class ChannelTable(QTreeWidget):
    """The table, and the mouse. Clicking the drawn columns is handled here
    rather than by widgets — see the module docstring."""

    value_pressed = Signal(int, float)      # row, x fraction of the cell
    value_dragged = Signal(int, float)      # row, dx in pixels
    value_released = Signal()
    value_typed = Signal(int)               # row — double-clicked, type it
    toggle_key = Signal(int)                # row
    seek_frame = Signal(int)                # frame

    def __init__(self, page, parent=None):
        super().__init__(parent)
        self.page = page
        self._drag_row = None
        self._drag_x = 0.0
        self.setRootIsDecorated(False)
        self.setUniformRowHeights(True)
        self.setAlternatingRowColors(True)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.setHeaderLabels(["Property", "Value", "Keys", ""])
        header = self.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(COL_NAME, QHeaderView.Interactive)
        header.setSectionResizeMode(COL_VALUE, QHeaderView.Interactive)
        header.setSectionResizeMode(COL_KEYS, QHeaderView.Stretch)
        header.setSectionResizeMode(COL_KEY, QHeaderView.Fixed)
        self.setColumnWidth(COL_NAME, 240)
        self.setColumnWidth(COL_VALUE, 150)
        self.setColumnWidth(COL_KEY, 30)

    def _row_at(self, pos):
        item = self.itemAt(pos)
        if item is None:
            return None, None
        column = self.columnAt(int(pos.x()))
        return item, column

    def mousePressEvent(self, event):
        item, column = self._row_at(event.position().toPoint())
        if item is None or column in (None, COL_NAME):
            super().mousePressEvent(event)
            return
        row = item.data(0, Qt.UserRole)
        if row is None:
            super().mousePressEvent(event)
            return
        rect = self.visualItemRect(item)
        if column == COL_VALUE:
            # ⚠ NO `super()` CALL — pressing a value must not change the row
            # selection, or every drag would throw away the multi-selection
            # the bulk buttons act on.
            self._drag_row = row
            self._drag_x = event.position().x()
            left = self.columnViewportPosition(COL_VALUE)
            width = max(1, self.columnWidth(COL_VALUE))
            self.value_pressed.emit(row, (event.position().x() - left) / width)
            event.accept()
            return
        if column == COL_KEY:
            self.toggle_key.emit(row)
            event.accept()
            return
        if column == COL_KEYS:
            frame = self.page.frame_hit(
                QRectF(self.columnViewportPosition(COL_KEYS), rect.y(),
                       self.columnWidth(COL_KEYS), rect.height()),
                event.position().x(), self.page.keys.get(self.page.names[row]))
            if frame is not None:
                self.seek_frame.emit(frame)
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        """Double-click a value to TYPE it.

        ⚠ The drag is bounded by the SOFT range; typing is how you get past it,
        which is the same division Blender's own fields make. Marty asked for
        it directly (2026-08-19: "so i dont just rely on the slider").
        """
        item, column = self._row_at(event.position().toPoint())
        if item is not None and column == COL_VALUE:
            row = item.data(0, Qt.UserRole)
            if row is not None:
                # ⚠ Cancel the drag the first press of this double-click
                # started, or releasing over the dialog writes a stray value.
                self._drag_row = None
                self.value_typed.emit(row)
                event.accept()
                return
        super().mouseDoubleClickEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag_row is None:
            super().mouseMoveEvent(event)
            return
        self.value_dragged.emit(self._drag_row,
                                event.position().x() - self._drag_x)
        self._drag_x = event.position().x()
        event.accept()

    def mouseReleaseEvent(self, event):
        if self._drag_row is not None:
            self._drag_row = None
            self.value_released.emit()
            event.accept()
            return
        super().mouseReleaseEvent(event)


class RigPropsPage(QWidget):
    """Organize ▸ Rig properties."""

    STRIP_PAD = 8.0          # px of margin either end of a key strip

    def __init__(self, bridge, window, parent=None):
        super().__init__(parent)
        self.bridge = bridge
        self.window = window
        self._busy = False

        # ---- the model. Parallel arrays, because every hot path walks them
        # by index and a list of dicts would allocate 775 lookups per repaint.
        self.names = []
        self.kinds = []
        self.meta = []           # per row: label/smin/smax/default/bounded
        self.values = []
        self.keys = {}           # name -> [frames]; animated rows only
        self.frame = 0
        self.start, self.end = 1, 250
        self.rig = None
        self.rigs = []
        self.skipped = 0
        self._shape = None
        self._revision = None
        self._view = []          # model indices, filtered and sorted
        self._follow = True
        self._pending = {}       # row -> value waiting to be sent
        self._last_write = 0.0
        self._reason = ""

        self._build()

        self._timer = QTimer(self)
        self._timer.setInterval(POLL_MS)
        self._timer.timeout.connect(self._poll)
        self._write_timer = QTimer(self)
        self._write_timer.setInterval(WRITE_MS)
        self._write_timer.setSingleShot(True)
        self._write_timer.timeout.connect(self._flush_writes)
        self._filter_timer = QTimer(self)
        self._filter_timer.setInterval(FILTER_MS)
        self._filter_timer.setSingleShot(True)
        self._filter_timer.timeout.connect(self._relist)

    # ------------------------------------------------------------- layout
    def _build(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(8)

        self.blurb = QLabel(
            "Every property as a channel: its value, and where its keys are "
            "on the timeline. Select several rows and key or un-key them "
            "together; click a diamond to jump to that frame.")
        self.blurb.setObjectName("dim")
        self.blurb.setWordWrap(True)
        outer.addWidget(self.blurb)

        outer.addWidget(self._build_rig_row())
        outer.addWidget(self._build_filter_row())
        outer.addWidget(self._build_action_row())

        self.table = ChannelTable(self)
        self.delegate = ChannelDelegate(self, self.table)
        self.table.setItemDelegate(self.delegate)
        self.table.itemSelectionChanged.connect(self._on_selection)
        self.table.value_pressed.connect(self._on_value_pressed)
        self.table.value_dragged.connect(self._on_value_dragged)
        self.table.value_released.connect(self._on_value_released)
        self.table.value_typed.connect(self._on_value_typed)
        self.table.toggle_key.connect(self._on_toggle_key)
        self.table.seek_frame.connect(self.seek)
        outer.addWidget(self.table, 1)

        self.status = widgets.ElidedLabel("", minimum=120)
        self.status.setObjectName("dim")
        outer.addWidget(self.status)

    def _build_rig_row(self):
        host = QWidget()
        row = QHBoxLayout(host)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        row.addWidget(QLabel("Rig"))
        self.rig_combo = widgets.NoScrollComboBox()
        self.rig_combo.setMinimumWidth(140)
        self.rig_combo.currentIndexChanged.connect(self._on_rig_picked)
        row.addWidget(self.rig_combo, 1)
        self.follow_button = QToolButton()
        self.follow_button.setText("Follow active")
        self.follow_button.setIcon(_icon("follow"))
        self.follow_button.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.follow_button.setCheckable(True)
        self.follow_button.setChecked(True)
        self.follow_button.setToolTip(
            "Show whichever ARMATURE is active in Blender.\n"
            "Clicking a mesh does not change the rig.")
        self.follow_button.toggled.connect(self._on_follow)
        row.addWidget(self.follow_button)
        self.count_label = QLabel("")
        self.count_label.setObjectName("dim")
        row.addWidget(self.count_label)
        return host

    def _build_filter_row(self):
        host = QWidget()
        row = widgets.FlowLayout(h_spacing=6, v_spacing=4)
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("Filter properties…")
        self.filter_edit.setClearButtonEnabled(True)
        self.filter_edit.addAction(_icon("search", 14, theme.TEXT_DIM),
                                   QLineEdit.LeadingPosition)
        self.filter_edit.setMinimumWidth(140)
        self.filter_edit.textChanged.connect(
            lambda _t: self._filter_timer.start())
        row.addWidget(self.filter_edit)
        self.chips = {}
        for key, label, tip in (
                ("animated", "Animated", "Only properties that have keys"),
                ("keyed", "Keyed here",
                 "Only properties keyed on the current frame"),
                ("nonzero", "Non-zero",
                 "Only properties that are not at their default")):
            chip = QToolButton()
            chip.setText(label)
            chip.setCheckable(True)
            chip.setToolTip(tip)
            chip.toggled.connect(lambda _v: self._relist())
            self.chips[key] = chip
            row.addWidget(chip)
        self.sort_combo = widgets.NoScrollComboBox()
        self.sort_combo.addItem("Sort: animated first", "animated")
        self.sort_combo.addItem("Sort: name", "name")
        self.sort_combo.addItem("Sort: value", "value")
        self.sort_combo.currentIndexChanged.connect(lambda _i: self._relist())
        row.addWidget(self.sort_combo)
        host.setLayout(row)
        return host

    def _build_action_row(self):
        host = QWidget()
        row = widgets.FlowLayout(h_spacing=6, v_spacing=4)
        self.sel_label = QLabel("Nothing selected")
        self.sel_label.setStyleSheet("font-weight: bold;")
        row.addWidget(self.sel_label)
        self.key_button = QPushButton("Key")
        self.key_button.setIcon(_icon("key_on", 15, KEYED))
        self.key_button.setProperty("_madi_keep_text", True)
        self.key_button.setToolTip("Insert a key on every selected property")
        self.key_button.clicked.connect(self.key_selected)
        row.addWidget(self.key_button)
        self.unkey_button = QPushButton("Delete key")
        self.unkey_button.setIcon(_icon("key_off"))
        self.unkey_button.setProperty("_madi_keep_text", True)
        self.unkey_button.setToolTip(
            "Delete the key on the current frame, if there is one")
        self.unkey_button.clicked.connect(self.unkey_selected)
        row.addWidget(self.unkey_button)
        self.clear_button = QPushButton("Delete all keys")
        self.clear_button.setIcon(_icon("cross"))
        self.clear_button.setProperty("_madi_keep_text", True)
        self.clear_button.setToolTip(
            "Remove every key. The value stays where it is")
        self.clear_button.clicked.connect(self.clear_selected)
        row.addWidget(self.clear_button)
        self.reset_button = QPushButton("Reset")
        self.reset_button.setIcon(_icon("reset"))
        self.reset_button.setProperty("_madi_keep_text", True)
        self.reset_button.setToolTip("Back to the property's default value")
        self.reset_button.clicked.connect(self.reset_selected)
        row.addWidget(self.reset_button)
        self.prev_button = QToolButton()
        self.prev_button.setIcon(_icon("key_prev"))
        self.prev_button.setToolTip("Previous key")
        self.prev_button.clicked.connect(lambda: self.step_key(-1))
        row.addWidget(self.prev_button)
        self.frame_label = QLabel("Frame —")
        self.frame_label.setStyleSheet("font-weight: bold;")
        row.addWidget(self.frame_label)
        self.next_button = QToolButton()
        self.next_button.setIcon(_icon("key_next"))
        self.next_button.setToolTip("Next key")
        self.next_button.clicked.connect(lambda: self.step_key(1))
        row.addWidget(self.next_button)
        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.setIcon(_icon("refresh"))
        self.refresh_button.setProperty("_madi_keep_text", True)
        self.refresh_button.setToolTip(
            "Re-read everything, including each property's soft range —\n"
            "editing a range in Blender is the one change the poll cannot see")
        self.refresh_button.clicked.connect(lambda: self.refresh(full=True))
        row.addWidget(self.refresh_button)
        host.setLayout(row)
        return host

    # ------------------------------------------------------- model access
    def bounds(self, row):
        """The slider's range. An UNBOUNDED property gets a window around its
        own value rather than an invented range — see `text_of`."""
        info = self.meta[row]
        low, high = info.get("smin"), info.get("smax")
        if low is None or high is None:
            value = self.values[row] or 0.0
            return min(0.0, value), max(1.0, value)
        return low, high

    def text_of(self, row):
        value = self.values[row]
        if self.kinds[row] == "int":
            return "%d" % int(value)
        return "%.3f" % float(value)

    def state_of(self, row):
        frames = self.keys.get(self.names[row])
        if not frames:
            return STATE_NONE
        return STATE_KEYED if self.frame in frames else STATE_ANIM

    def frame_x(self, rect, frame):
        span = max(1, self.end - self.start)
        pos = (frame - self.start) / float(span)
        pos = min(max(pos, 0.0), 1.0)
        return rect.x() + self.STRIP_PAD + \
            (rect.width() - 2 * self.STRIP_PAD) * pos

    def frame_hit(self, rect, x, frames):
        """Which key a click in a strip landed on, or None."""
        if not frames:
            return None
        best, best_dx = None, HIT_PX
        for frame in frames:
            dx = abs(self.frame_x(rect, frame) - x)
            if dx <= best_dx:
                best, best_dx = frame, dx
        return best

    def selected_rows(self):
        return [item.data(0, Qt.UserRole)
                for item in self.table.selectedItems()
                if item.columnCount() and item.data(0, Qt.UserRole) is not None]

    def selected_names(self):
        return [self.names[row] for row in self.selected_rows()]

    # ------------------------------------------------------------ visibility
    def showEvent(self, event):
        super().showEvent(event)
        self._timer.start()
        # `quiet=True` for the reason Organize documents: switching tabs is
        # automatic, and a connect to a dead port is DROPPED on this machine.
        self.refresh(quiet=True)

    def hideEvent(self, event):
        self._timer.stop()
        self._write_timer.stop()
        super().hideEvent(event)

    def set_capture_busy(self, busy):
        self._busy = bool(busy)
        self._set_enabled(not busy)

    def _set_enabled(self, on):
        on = bool(on) and not self._busy
        for widget in (self.table, self.rig_combo, self.follow_button,
                       self.filter_edit, self.sort_combo, self.key_button,
                       self.unkey_button, self.clear_button,
                       self.reset_button, self.prev_button, self.next_button,
                       self.refresh_button):
            widget.setEnabled(on)
        for chip in self.chips.values():
            chip.setEnabled(on)
        if on:
            self._on_selection()

    def feature_reason(self):
        try:
            return self.bridge.feature_reason(FEATURE)
        except Exception:                # noqa: BLE001 — a dead bridge is routine
            return None

    # ------------------------------------------------------------- reading
    def refresh(self, quiet=False, full=False):
        reason = self.feature_reason()
        if reason:
            self._set_enabled(False)
            self.status.setText(reason)
            return
        try:
            data = self._read(full=full)
        except bridgemod.BridgeError as exc:
            self._set_enabled(False)
            self.status.setText("Open Blender to use rig properties."
                                if quiet else
                                "Blender is not answering: %s" % exc)
            return
        self._set_enabled(True)
        self._apply(data)

    def _read(self, full=False):
        rig = None if self._follow else self.rig
        return self.bridge.rig_props_list(
            rig,
            shape=None if full else self._shape,
            revision=None if full else self._revision,
            full=full)

    def _poll(self):
        if self._busy or self._pending or self.feature_reason():
            # ⚠ Never poll mid-drag: the reply carries the value Blender had
            # before the write landed, and applying it would snap the bar back
            # under the mouse.
            return
        try:
            data = self._read()
        except bridgemod.BridgeError:
            return                       # a dead bridge is not an error here
        self._apply(data)

    # ------------------------------------------------------------ applying
    def _apply(self, data):
        """Take a reply and touch only what moved.

        Three tiers, and the first one is the common case: `unchanged` means
        the add-on found the same revision it was told about and sent nothing
        else. No painting, no listing, no allocation.
        """
        self._revision = data.get("revision")
        if data.get("unchanged"):
            return
        reason = data.get("reason")
        if reason:
            self._reset_model()
            self._reason = reason
            self.status.setText(reason)
            self._relist()
            return
        self._reason = ""
        self.rig = data.get("rig")
        self.rigs = data.get("rigs") or []
        self.count_label.setText("%d properties" % data.get("count", 0))
        self.frame = int(data.get("frame", 0))
        self.start = int(data.get("start", 1))
        self.end = int(data.get("end", 250))
        self.keys = data.get("keys") or {}
        self.skipped = int(data.get("skipped", 0))

        rows = data.get("rows")
        values = data.get("values") or []
        if rows is not None:
            self._shape = data.get("shape")
            self.names = [r.get("name") for r in rows]
            self.kinds = [r.get("kind") or "float" for r in rows]
            self.meta = rows
        elif len(values) != len(self.names):
            # ⚠ THE ALIGNMENT GUARD. The add-on said the shape was unchanged
            # and the array does not fit — rather than draw 775 values against
            # the wrong names, throw the cache away and ask for everything.
            self._shape = None
            self.refresh(full=True)
            return
        self.values = values
        self._sync_rig_combo()
        self._relist()
        self._update_frame_label()
        self._update_status()

    def _reset_model(self):
        self.names, self.kinds, self.meta, self.values = [], [], [], []
        self.keys, self.rig = {}, None
        self._shape = None

    def _sync_rig_combo(self):
        names = list(self.rigs)
        current = [self.rig_combo.itemText(i)
                   for i in range(self.rig_combo.count())]
        block = self.rig_combo.blockSignals(True)
        if names != current:
            self.rig_combo.clear()
            self.rig_combo.addItems(names)
        if self.rig in names:
            self.rig_combo.setCurrentIndex(names.index(self.rig))
        self.rig_combo.blockSignals(block)
        # ⚠ **ENABLED WHENEVER THERE ARE RIGS, NOT ONLY WHEN FOLLOW IS OFF.**
        # It used to grey out while "Follow active" was on — which is the
        # DEFAULT — so the first thing anyone tries, opening the rig dropdown,
        # did nothing at all and looked broken (Marty, 2026-08-19: "it doesnt
        # look like i can select rig"). Picking a rig now turns Follow active
        # off by itself; a control you can see is a control you can use.
        self.rig_combo.setEnabled(bool(names))

    # ------------------------------------------------------------- listing
    def _relist(self):
        """Rebuild the visible rows: filter, sort, then one item each.

        ⚠ This is the only O(n) widget work in the tab, and it runs on a
        filter or a sort — NOT on a poll. A value change repaints; it does not
        come through here.
        """
        text = self.filter_edit.text().strip().lower()
        want_anim = self.chips["animated"].isChecked()
        want_keyed = self.chips["keyed"].isChecked()
        want_nonzero = self.chips["nonzero"].isChecked()
        view = []
        for row, name in enumerate(self.names):
            if text and text not in name.lower():
                label = self.meta[row].get("label")
                if not label or text not in label.lower():
                    continue
            frames = self.keys.get(name)
            if want_anim and not frames:
                continue
            if want_keyed and not (frames and self.frame in frames):
                continue
            if want_nonzero and not self._is_changed(row):
                continue
            view.append(row)

        order = self.sort_combo.currentData()
        if order == "name":
            view.sort(key=lambda r: self.names[r].lower())
        elif order == "value":
            view.sort(key=lambda r: -abs(float(self.values[r] or 0.0)))
        else:
            # animated first, then keyed-here first within that, then name
            view.sort(key=lambda r: (0 if self.keys.get(self.names[r]) else 1,
                                     self.names[r].lower()))
        self._view = view
        self._rebuild_items()

    def _is_changed(self, row):
        default = self.meta[row].get("default")
        value = self.values[row]
        if default is None:
            return bool(value)
        try:
            return abs(float(value) - float(default)) > 1e-6
        except (TypeError, ValueError):
            return value != default

    def _rebuild_items(self):
        keep = set(self.selected_rows())
        self.table.setUpdatesEnabled(False)
        self.table.clear()
        items = []
        for row in self._view:
            item = QTreeWidgetItem([self.names[row], "", "", ""])
            item.setData(0, Qt.UserRole, row)
            # ⚠ EVERY column carries the row index: the delegate is handed a
            # per-column index and has nothing else to look the row up by.
            for column in (COL_VALUE, COL_KEYS, COL_KEY):
                item.setData(column, Qt.UserRole, row)
            label = self.meta[row].get("label")
            item.setToolTip(COL_NAME, "%s\n%s" % (label, self.names[row])
                            if label else self.names[row])
            items.append(item)
        self.table.addTopLevelItems(items)
        for item in items:
            if item.data(0, Qt.UserRole) in keep:
                item.setSelected(True)
        self.table.setUpdatesEnabled(True)
        self._update_headers()
        self._on_selection()

    def _update_headers(self):
        self.table.headerItem().setText(
            COL_KEYS, "Keys  (%d – %d)" % (self.start, self.end))

    def _update_frame_label(self):
        self.frame_label.setText("Frame %d" % self.frame)

    def _update_status(self):
        if self._reason:
            return
        animated = sum(1 for name in self.names if self.keys.get(name))
        keyed = sum(1 for name in self.names
                    if self.frame in (self.keys.get(name) or ()))
        bits = ["showing %d of %d" % (len(self._view), len(self.names)),
                "%d animated" % animated,
                "%d keyed on this frame" % keyed]
        if self.skipped:
            bits.append("%d not shown (not a number or a switch)"
                        % self.skipped)
        self.status.setText("  ·  ".join(bits))

    def _on_selection(self):
        rows = self.selected_rows()
        self.sel_label.setText("%d selected" % len(rows) if rows
                               else "Nothing selected")
        self.sel_label.setStyleSheet(
            "font-weight: bold; color: %s;" % (theme.ACCENT if rows
                                               else theme.TEXT_DIM))
        on = bool(rows) and not self._busy
        for button in (self.key_button, self.unkey_button, self.clear_button,
                       self.reset_button):
            button.setEnabled(on)

    def retheme(self):
        self.key_button.setIcon(_icon("key_on", 15, KEYED))
        self.unkey_button.setIcon(_icon("key_off"))
        self.clear_button.setIcon(_icon("cross"))
        self.reset_button.setIcon(_icon("reset"))
        self.prev_button.setIcon(_icon("key_prev"))
        self.next_button.setIcon(_icon("key_next"))
        self.follow_button.setIcon(_icon("follow"))
        self.refresh_button.setIcon(_icon("refresh"))
        self.table.viewport().update()

    # -------------------------------------------------------------- writes
    def _write(self, call, *args, **kwargs):
        try:
            call(*args, **kwargs)
        except bridgemod.BridgeError as exc:
            QMessageBox.warning(self, TITLE, str(exc))
            return False
        self.refresh()
        return True

    def _on_rig_picked(self, index):
        if index < 0 or index >= len(self.rigs):
            return
        self.rig = self.rigs[index]
        self._shape = None               # a different rig is a different shape
        self._revision = None
        # ⚠ Choosing a rig BY HAND means you want that rig — so Follow active
        # gets out of the way rather than overwriting the choice on the next
        # poll. Set silently: letting `toggled` fire would refresh twice and
        # the first one would still be following.
        if self._follow:
            self._follow = False
            block = self.follow_button.blockSignals(True)
            self.follow_button.setChecked(False)
            self.follow_button.blockSignals(block)
        self.refresh(full=True)

    def _on_follow(self, on):
        self._follow = bool(on)
        self._revision = None
        self.refresh()

    # ---- the value drag
    def _on_value_pressed(self, row, fraction):
        if self.kinds[row] == "bool":
            self.values[row] = not self.values[row]
            self._pending[row] = self.values[row]
            self._flush_writes()
            self.table.viewport().update()
            return
        # ⚠ NOTHING IS PENDING UNTIL THE MOUSE ACTUALLY MOVES. Seeding here
        # made a plain CLICK send a write of the value the property already
        # had — a wasted round trip, a wasted viewport redraw, and an undo
        # step for doing nothing. `_on_value_dragged` records the value it
        # sets, which is the only moment there is something to send.

    def _on_value_typed(self, row):
        """Double-click: type an exact number.

        ⚠ **THE TYPED RANGE IS WIDER THAN THE SLIDER'S.** Dragging is bounded
        by the property's SOFT range; typing is how you go past it, exactly as
        Blender's own fields work. The add-on still clamps to the HARD range
        and returns what actually landed, so the tab shows the truth rather
        than what was asked for.
        """
        kind = self.kinds[row]
        if kind == "bool":
            return                       # a switch has nothing to type
        name = self.meta[row].get("label") or self.names[row]
        low, high = self.bounds(row)
        hint = "%s\n\nSlider range %s to %s — you can type outside it." % (
            self.names[row], self._fmt(row, low), self._fmt(row, high))
        if kind == "int":
            value, ok = QInputDialog.getInt(self, name, hint,
                                            int(self.values[row]),
                                            -1000000000, 1000000000)
        else:
            value, ok = QInputDialog.getDouble(self, name, hint,
                                               float(self.values[row]),
                                               -1e9, 1e9, 4)
        if not ok:
            return
        self.values[row] = value
        self._pending[row] = value
        self._flush_writes()             # at once — there is no drag to batch
        self.table.viewport().update()

    def _fmt(self, row, value):
        return ("%d" % int(value)) if self.kinds[row] == "int" \
            else ("%.3f" % float(value))

    def _on_value_dragged(self, row, dx):
        if self.kinds[row] == "bool":
            return
        low, high = self.bounds(row)
        span = high - low
        if span <= 0:
            return
        # The same feel as widgets.ValueSlider: a full sweep is 180 px.
        value = float(self.values[row]) + (dx / 180.0) * span
        value = min(max(value, low), high)
        if self.kinds[row] == "int":
            value = int(round(value))
        self.values[row] = value
        self._pending[row] = value
        self.table.viewport().update()
        if not self._write_timer.isActive():
            self._write_timer.start()

    def _on_value_released(self):
        self._write_timer.stop()
        self._flush_writes()

    def _flush_writes(self):
        """Send whatever the drag has accumulated. ⚠ One request per property,
        never per mouse move — `_pending` holds only the LATEST value for each
        row, so a drag that crossed a hundred pixels sends one number."""
        if not self._pending or not self.rig:
            self._pending.clear()
            return
        pending, self._pending = self._pending, {}
        self._last_write = time.monotonic()
        for row, value in pending.items():
            try:
                reply = self.bridge.rig_props_set(self.rig, self.names[row],
                                                  value)
            except bridgemod.BridgeError as exc:
                self.status.setText("Could not set %s: %s"
                                    % (self.names[row], exc))
                return
            if isinstance(reply, dict) and "revision" in reply:
                self._revision = reply["revision"]
                if "value" in reply:
                    # Blender may have clamped it to the property's hard range.
                    self.values[row] = reply["value"]
        self.table.viewport().update()

    def _on_toggle_key(self, row):
        """The diamond: insert a key here, or delete the one that is here."""
        name = self.names[row]
        frames = self.keys.get(name) or ()
        if self.frame in frames:
            self._write(self.bridge.rig_props_unkey, self.rig, [name],
                        self.frame)
        else:
            self._write(self.bridge.rig_props_key, self.rig, [name],
                        self.frame)

    # ---- the bulk buttons
    def key_selected(self):
        names = self.selected_names()
        if names:
            self._write(self.bridge.rig_props_key, self.rig, names, self.frame)

    def unkey_selected(self):
        names = self.selected_names()
        if names:
            self._write(self.bridge.rig_props_unkey, self.rig, names,
                        self.frame)

    def clear_selected(self):
        names = self.selected_names()
        if not names:
            return
        if QMessageBox.question(
                self, TITLE,
                "Delete every keyframe on %d propert%s?\n\nThe values stay "
                "where they are." % (len(names),
                                     "y" if len(names) == 1 else "ies"),
                QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
            return
        self._write(self.bridge.rig_props_unkey, self.rig, names, None, True)

    def reset_selected(self):
        names = self.selected_names()
        if names:
            self._write(self.bridge.rig_props_reset, self.rig, names)

    # ---- the playhead
    def seek(self, frame):
        if self._write(self.bridge.rig_props_frame, int(frame)):
            self._update_frame_label()

    def step_key(self, direction):
        """Previous/next key of the SELECTION, or of everything animated when
        nothing is selected — the same rule Blender's up/down arrows use."""
        rows = self.selected_rows()
        names = [self.names[r] for r in rows] if rows else list(self.keys)
        candidates = set()
        for name in names:
            for frame in self.keys.get(name) or ():
                candidates.add(frame)
        if not candidates:
            self.status.setText("No keys to step to.")
            return
        if direction < 0:
            earlier = [f for f in candidates if f < self.frame]
            target = max(earlier) if earlier else min(candidates)
        else:
            later = [f for f in candidates if f > self.frame]
            target = min(later) if later else max(candidates)
        self.seek(target)
