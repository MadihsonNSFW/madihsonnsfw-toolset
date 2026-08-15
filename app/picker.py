"""The Bone picker tab — a manager for the picker that runs inside Blender.

Marty's call (2026-08-04): **this tab is a manager, not a second canvas.** The
picker's buttons are drawn by a GPU handler in Blender's Image Editor and that
is where you click them; here you look after everything AROUND that — the tabs,
which rig and reference image each one uses, the button list, retargeting a
layout onto a different rig, the appearance settings, and saving a layout to the
library.

WHY THE TWO UIs CANNOT DRIFT
There is no second copy of the layout. The buttons live on the armature
(`obj.madi_picker`) and the tabs on the Scene; this tab and the Blender panel
both read and write THOSE, over `picker_*` bridge commands. So "sync" is not a
mechanism — it is the absence of a second source of truth. Same reasoning as the
Anim Layers panel (`docs\\anim-layers.md`), and it is why there is deliberately
no mirror here: even the three appearance settings live only in the add-on
preferences, with this tab acting as a remote control.

⚠ EVERY REPEATING POLL PASSES `poll=True`. A connect to a dead localhost port is
not refused on Marty's machine — the SYN is dropped — so an un-flagged poll
burns the full timeout on the GUI thread, every tick (`docs\\app-shell.md`).
"""
import json
import os
import shutil

from PySide6.QtCore import QRectF
from PySide6.QtGui import QColor, QImage, QPainter, QPen

import bridge as bridgemod
import theme
from rendering import RenderingPage

# --------------------------------------------------------------------------
# The item thumbnail: the reference picture WITH the buttons drawn on it
# --------------------------------------------------------------------------
# Marty, 2026-08-05: "When saving bone picker we need to make sure the buttons
# are visible in the preview thumbnail."
#
# The add-on writes the tab's reference image and stops there, because the
# buttons are a GPU overlay in Blender's Image Editor and there is no way to
# screenshot that from a script (`render.opengl` renders a VIEW_3D, and refuses
# outright in background — BLENDER_NOTES). But the layout is already in
# `picker.json`, so the app can simply DRAW it: same shapes, same colours, same
# coordinates, no Blender involved. It also means an existing item can be
# re-composed at any time.
#
# ⚠ THE CLEAN REFERENCE IS KEPT AS `reference.jpg`, and every compose starts
# from THAT. Composing thumbnail.jpg onto itself would paint the buttons on top
# of the buttons, a little more opaque each time — and 📷 Update Preview would
# be the thing that ruined the picture.
#
# Canvas space is (0,0)..(1,1) over the image with **y UP** (Blender), and the
# add-on squashes the reference to a square, so the mapping is direct apart
# from the y flip. Drawn size is `w * scale` in canvas units (`_btn_wh`).
REFERENCE_FILE = "reference.jpg"
BTN_ROUND = 0.07        # matches picker.BTN_ROUND
SLIDER_TRACK = 0.30     # matches picker.SLIDER_TRACK
# The live picker draws at ~60% alpha with no outline. A tile is ~110 px, so a
# faint blob over a photograph is not "visible"; the thumbnail is drawn more
# opaque, with a soft dark edge for separation. A legibility choice for the
# preview only — it does not change how the picker looks in Blender.
FILL_ALPHA = 0.90
EDGE_ALPHA = 0.35


def compose_thumbnail(item_path, size=256):
    """Draw a `.picker` item's buttons onto its reference picture.

    Returns True if a thumbnail was written. Best effort throughout: a preview
    is cosmetic and must never be what fails a save.
    """
    try:
        with open(os.path.join(item_path, "picker.json"), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return False
    buttons = data.get("buttons") or []
    if not buttons:
        return False

    thumb = os.path.join(item_path, "thumbnail.jpg")
    reference = os.path.join(item_path, REFERENCE_FILE)
    try:
        if not os.path.isfile(reference):
            if not os.path.isfile(thumb):
                return False       # no reference image on the tab: nothing to draw on
            shutil.copy2(thumb, reference)
        image = QImage(reference)
        if image.isNull():
            return False
        if image.width() != size or image.height() != size:
            image = image.scaled(size, size)
        image = image.convertToFormat(QImage.Format_RGB32)
        painter = QPainter(image)
        try:
            _paint_buttons(painter, buttons, image.width(), image.height())
        finally:
            painter.end()
        return bool(image.save(thumb, "JPEG", 92))
    except (OSError, ValueError):
        return False


def _paint_buttons(painter, buttons, width, height):
    painter.setRenderHint(QPainter.Antialiasing)
    for btn in buttons:
        try:
            cx = float(btn.get("x", 0.5)) * width
            # ⚠ canvas y is UP, Qt's is DOWN
            cy = (1.0 - float(btn.get("y", 0.5))) * height
            scale = float(btn.get("scale", 1.0))
            bw = abs(float(btn.get("w", 0.05))) * scale * width
            bh = abs(float(btn.get("h", 0.05))) * scale * height
            rgb = list(btn.get("color") or (0.8, 0.8, 0.8))[:3]
        except (TypeError, ValueError):
            continue
        if bw <= 0.0 or bh <= 0.0:
            continue
        while len(rgb) < 3:
            rgb.append(0.8)
        kind = btn.get("kind", "BONE")
        if kind == "SLIDER":
            rgb = [c * SLIDER_TRACK for c in rgb]
        fill = QColor.fromRgbF(min(max(rgb[0], 0.0), 1.0),
                               min(max(rgb[1], 0.0), 1.0),
                               min(max(rgb[2], 0.0), 1.0), FILL_ALPHA)
        rect = QRectF(cx - bw * 0.5, cy - bh * 0.5, bw, bh)
        painter.setBrush(fill)
        painter.setPen(QPen(QColor(0, 0, 0, int(255 * EDGE_ALPHA)),
                            max(0.6, min(bw, bh) * 0.06)))
        if kind == "GROUP":
            painter.drawEllipse(rect)
        else:
            radius = min(bw, bh) * BTN_ROUND
            painter.drawRoundedRect(rect, radius, radius)
from widgets import NoScrollComboBox, ValueSlider

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QColorDialog,
                               QFormLayout, QGroupBox, QHBoxLayout,
                               QHeaderView, QInputDialog, QLabel, QLineEdit,
                               QMessageBox, QPushButton, QTableWidget,
                               QTableWidgetItem, QVBoxLayout, QWidget)

POLL_MS = 1500
# A drag emits valueChanged per step; without this the app would send a bridge
# command per pixel. Same 250 ms Bone Jiggle settled on (docs\app-shell.md).
PUSH_DEBOUNCE_MS = 250

FEATURE = "bone_picker"


class PickerPage(RenderingPage):
    """Top-level 'Bone picker' tab — the shared LAYOUT A shell."""

    EMPTY_TEXT = (
        "No picker tools yet.\n\n"
        "The picker itself draws in Blender's Image Editor —\n"
        "this tab looks after its tabs, buttons and presets.")

    def set_capture_busy(self, busy):
        """Forward to the tools: every one of them talks to the bridge, so they
        must actually grey out while Blender is busy."""
        for _title, _group, widget in self._tools:
            if hasattr(widget, "set_capture_busy"):
                widget.set_capture_busy(busy)


class _PickerTool(QWidget):
    """Shared plumbing: the bridge guard, error reporting, the capability gate.

    Only ONE tool owns the poll (PickerTabsTool); the rest are fed by its
    `status_refreshed` signal, so the tab makes one round trip per tick however
    many tools are open.
    """

    def __init__(self, bridge, window, parent=None):
        super().__init__(parent)
        self.bridge = bridge
        self.window = window
        self._busy = False
        self._status = {}
        self._syncing = False

    # ------------------------------------------------------------- helpers

    @staticmethod
    def _dim(text):
        lab = QLabel(text)
        lab.setObjectName("dim")
        lab.setWordWrap(True)
        return lab

    def _fail(self, exc):
        self.status.setStyleSheet("color: #e06c60;")
        self.status.setText(str(exc))

    def _ok(self, text):
        self.status.setStyleSheet("")
        self.status.setText(text)

    def feature_reason(self):
        """Why the picker is unavailable on the installed add-on, or None.

        The compatibility contract: an older add-on costs this ONE tab, with the
        reason shown, and nothing else changes (`docs\\addon-bridge.md`).
        """
        try:
            return self.bridge.feature_reason(FEATURE)
        except Exception:               # noqa: BLE001 - a dead bridge is routine
            return None                 # fail OPEN: unknown is not "missing"

    def _guarded(self):
        if self._busy:
            return False
        if self.window is not None and self.window.capturing:
            return False
        return True

    def _call(self, fn, *args, **kwargs):
        """Run a bridge command and apply the status it returns.

        Every mutating picker command answers with the WHOLE status, so the tab
        repaints from the reply rather than firing a second round trip.
        """
        if not self._guarded():
            return None
        if self.window is not None and not self.window.bridge_free_for_tools():
            return None
        self._busy = True
        try:
            status = fn(*args, **kwargs)
        except bridgemod.BridgeError as exc:
            self._fail(exc)
            return None
        finally:
            self._busy = False
        self.broadcast(status)
        return status

    def broadcast(self, status):
        """Push a fresh status to every tool. Overridden by the poll owner."""
        if self.window is not None:
            owner = getattr(self.window, "picker_tabs_tool", None)
            if owner is not None and owner is not self:
                owner.apply_status(status)
                owner.status_refreshed.emit(status)
                return
        self.apply_status(status)

    def apply_status(self, status):
        raise NotImplementedError

    def set_capture_busy(self, busy):
        self.setEnabled(not busy)


class PickerTabsTool(_PickerTool):
    """Tabs, the rig and reference image each one uses, and the session.

    Owns the ONLY poll — the other tools are fed from `status_refreshed`.
    """

    status_refreshed = Signal(object)

    def __init__(self, bridge, window, parent=None):
        super().__init__(bridge, window, parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)

        lay.addWidget(self._dim(
            "A picker tab is one page: its own rig, its own reference picture "
            "and its own buttons. The picker itself runs in Blender's Image "
            "Editor."))

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Tab", "Rig", "Background"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.Stretch)
        self.table.setMinimumHeight(130)
        self.table.itemSelectionChanged.connect(self._on_row_selected)
        lay.addWidget(self.table)

        row = QHBoxLayout()
        self.btn_add = QPushButton("Add tab")
        self.btn_add.clicked.connect(self.add_tab)
        self.btn_rename = QPushButton("Rename…")
        self.btn_rename.clicked.connect(self.rename_tab)
        self.btn_remove = QPushButton("Remove")
        self.btn_remove.clicked.connect(self.remove_tab)
        for b in (self.btn_add, self.btn_rename, self.btn_remove):
            row.addWidget(b)
        row.addStretch(1)
        lay.addLayout(row)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight)
        self.combo_rig = NoScrollComboBox()
        self.combo_rig.setMinimumWidth(120)
        self.combo_rig.activated.connect(self._on_rig)
        form.addRow("Rig", self.combo_rig)
        self.combo_image = NoScrollComboBox()
        self.combo_image.setMinimumWidth(120)
        self.combo_image.activated.connect(self._on_image)
        form.addRow("Background", self.combo_image)
        lay.addLayout(form)

        run = QHBoxLayout()
        self.btn_start = QPushButton("Start picker")
        self.btn_start.clicked.connect(self.start)
        self.btn_stop = QPushButton("Stop picker")
        self.btn_stop.clicked.connect(self.stop)
        run.addWidget(self.btn_start)
        run.addWidget(self.btn_stop)
        run.addStretch(1)
        lay.addLayout(run)
        lay.addWidget(self._dim(
            "Starting needs an Image Editor open in Blender — that is where "
            "the buttons are drawn."))

        self.status = QLabel("—")
        self.status.setObjectName("dim")
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(self.status)
        lay.addStretch(1)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self.poll)
        self._timer.start(POLL_MS)

    # ------------------------------------------------------------- polling

    def poll(self):
        """The timer path — only while the tab is actually on screen."""
        if not self.isVisible():
            return
        self.refresh(polling=True)

    def refresh(self, polling=False):
        if not self._guarded():
            return
        reason = self.feature_reason()
        if reason:
            self._gate(reason)
            return
        try:
            status = self.bridge.picker_status(poll=polling)
        except bridgemod.BridgeError:
            self._ok("Blender not connected")
            return
        self.broadcast(status)

    def broadcast(self, status):
        self.apply_status(status)
        self.status_refreshed.emit(status)

    def _gate(self, reason):
        """An add-on too old to have a picker: say so, and turn the tab off."""
        for w in (self.table, self.btn_add, self.btn_rename, self.btn_remove,
                  self.combo_rig, self.combo_image, self.btn_start,
                  self.btn_stop):
            w.setEnabled(False)
        self._fail(reason)

    # -------------------------------------------------------------- status

    def apply_status(self, status):
        if not isinstance(status, dict):
            return
        self._status = status
        tabs = status.get("tabs") or []
        active = status.get("active_index", 0)

        # ⚠ THE REBUILD MUST STAY INSIDE THE GUARD. setRowCount/setItem emit
        # itemSelectionChanged, which would echo straight back to Blender as a
        # tab switch with a stale index — and re-enter apply_status while it is
        # still running. The Anim Layers list learned this the hard way.
        self._syncing = True
        try:
            self.table.setRowCount(len(tabs))
            for i, t in enumerate(tabs):
                self._set_cell(i, 0, t.get("name") or "Picker %d" % (i + 1))
                self._set_cell(i, 1, t.get("armature") or "—")
                self._set_cell(i, 2, t.get("image") or "—")
            if 0 <= active < len(tabs):
                self.table.selectRow(active)
        finally:
            self._syncing = False

        # Never overwrite a combo the user is interacting with.
        self._fill_combo(self.combo_rig, status.get("armatures") or [],
                         status.get("armature"), "— no rig —")
        images = status.get("images") or []
        current_image = None
        if 0 <= active < len(tabs):
            current_image = tabs[active].get("image")
        self._fill_combo(self.combo_image, images, current_image,
                         "— no background —")

        running = bool(status.get("running"))
        self.btn_start.setEnabled(not running and bool(tabs))
        self.btn_stop.setEnabled(running)
        self.btn_remove.setEnabled(len(tabs) > 1)
        self.btn_rename.setEnabled(bool(tabs))
        if not tabs:
            self._ok("No picker tabs yet — add one to begin.")
        else:
            unmatched = status.get("unmatched") or 0
            bits = ["%d tab%s" % (len(tabs), "" if len(tabs) == 1 else "s"),
                    "%d button%s" % (len(status.get("buttons") or []),
                                     "" if len(status.get("buttons") or []) == 1
                                     else "s")]
            if unmatched:
                bits.append("%d unmatched" % unmatched)
            bits.append("running" if running else "stopped")
            self._ok(" · ".join(bits))

    def _set_cell(self, row, col, text):
        item = QTableWidgetItem(text)
        item.setFlags(item.flags() & ~Qt.ItemIsEditable)
        self.table.setItem(row, col, item)

    def _fill_combo(self, combo, names, current, empty_label):
        if combo.hasFocus():
            return                      # the user is choosing; leave it alone
        wanted = [empty_label] + list(names)
        existing = [combo.itemText(i) for i in range(combo.count())]
        self._syncing = True
        try:
            if existing != wanted:
                combo.clear()
                combo.addItems(wanted)
            index = wanted.index(current) if current in wanted else 0
            combo.setCurrentIndex(index)
        finally:
            self._syncing = False

    # ------------------------------------------------------------- actions

    def _on_row_selected(self):
        if self._syncing:
            return
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return
        index = rows[0].row()
        if index == self._status.get("active_index"):
            return
        self._call(self.bridge.picker_set_tab, index)

    def add_tab(self):
        name, ok = QInputDialog.getText(self, "Add picker tab", "Name:")
        if not ok:
            return
        self._call(self.bridge.picker_add_tab, name.strip() or None)

    def rename_tab(self):
        tabs = self._status.get("tabs") or []
        index = self._status.get("active_index", 0)
        if not 0 <= index < len(tabs):
            return
        name, ok = QInputDialog.getText(self, "Rename picker tab", "Name:",
                                        text=tabs[index].get("name") or "")
        if not ok or not name.strip():
            return
        self._call(self.bridge.picker_rename_tab, name.strip())

    def remove_tab(self):
        tabs = self._status.get("tabs") or []
        index = self._status.get("active_index", 0)
        if not 0 <= index < len(tabs):
            return
        name = tabs[index].get("name") or "this tab"
        n = len([b for b in (self._status.get("buttons") or [])])
        if QMessageBox.question(
                self, "Remove picker tab",
                "Remove %r and its %d button(s)?\n\nButtons on other tabs are "
                "untouched." % (name, n)) != QMessageBox.Yes:
            return
        self._call(self.bridge.picker_remove_tab)

    def _on_rig(self, _index):
        if self._syncing:
            return
        name = self.combo_rig.currentText()
        self._call(self.bridge.picker_set_tab_rig,
                   None if name.startswith("—") else name)

    def _on_image(self, _index):
        if self._syncing:
            return
        name = self.combo_image.currentText()
        self._call(self.bridge.picker_set_tab_image,
                   None if name.startswith("—") else name)

    def start(self):
        self._call(self.bridge.picker_start)

    def stop(self):
        self._call(self.bridge.picker_stop)


class PickerButtonsTool(_PickerTool):
    """The active tab's buttons: what they are, what they point at, and the
    live appearance brushes that act on the Blender-side selection."""

    def __init__(self, bridge, window, parent=None):
        super().__init__(bridge, window, parent)
        self._push = QTimer(self)
        self._push.setSingleShot(True)
        self._push.timeout.connect(self._flush)
        self._pending = {}
        self._pending_index = None

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Kind", "Label", "Target", ""])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.Stretch)
        self.table.setMinimumHeight(180)
        self.table.itemSelectionChanged.connect(self._on_row_selected)
        lay.addWidget(self.table)

        row = QHBoxLayout()
        self.btn_remove = QPushButton("Remove selected")
        self.btn_remove.clicked.connect(self.remove_selected)
        row.addWidget(self.btn_remove)
        row.addStretch(1)
        lay.addLayout(row)

        # ---- the selected button -------------------------------------------
        lay.addWidget(self._dim("Selected button"))
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight)
        self.edit_label = QLineEdit()
        self.edit_label.editingFinished.connect(self._on_label)
        form.addRow("Label", self.edit_label)
        self.combo_target = NoScrollComboBox()
        self.combo_target.setMinimumWidth(120)
        self.combo_target.activated.connect(self._on_target)
        form.addRow("Bone", self.combo_target)
        self.slider_scale = ValueSlider(
            0.05, 20.0, 1.0, decimals=2,
            tooltip="Size multiplier on this button")
        self.slider_scale.valueChanged.connect(self._on_scale)
        form.addRow("Scale", self.slider_scale)
        self.chk_blank = QCheckBox("Draw no text on it")
        self.chk_blank.toggled.connect(self._on_blank)
        form.addRow("", self.chk_blank)
        self.btn_colour = QPushButton("Colour…")
        self.btn_colour.clicked.connect(self._on_colour)
        form.addRow("", self.btn_colour)
        lay.addLayout(form)

        self.status = QLabel("—")
        self.status.setObjectName("dim")
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(self.status)
        lay.addStretch(1)
        self._set_editors_enabled(False)

    # -------------------------------------------------------------- status

    def apply_status(self, status):
        if not isinstance(status, dict):
            return
        self._status = status
        buttons = status.get("buttons") or []
        selected = self._selected_index()

        self._syncing = True
        try:
            self.table.setRowCount(len(buttons))
            for i, b in enumerate(buttons):
                self._set_cell(i, 0, b.get("kind", "BONE").title())
                self._set_cell(i, 1, b.get("label") or "—")
                self._set_cell(i, 2, self._target_text(b))
                missing = b.get("missing") or []
                cell = QTableWidgetItem("!" if missing else "")
                cell.setFlags(cell.flags() & ~Qt.ItemIsEditable)
                if missing:
                    cell.setForeground(QColor("#e06c60"))
                    cell.setToolTip("Not on this rig: " + ", ".join(missing))
                self.table.setItem(i, 3, cell)
            if selected is not None and selected < len(buttons):
                self.table.selectRow(selected)
        finally:
            self._syncing = False

        self._sync_editors()
        unmatched = status.get("unmatched") or 0
        if not buttons:
            self._ok("No buttons on this tab yet — Ctrl+click in Blender's "
                     "Image Editor to add one.")
        elif unmatched:
            self._ok("%d button(s), %d pointing at something this rig does not "
                     "have — pick a bone below to retarget." % (len(buttons),
                                                                unmatched))
        else:
            self._ok("%d button(s), all matched." % len(buttons))

    @staticmethod
    def _target_text(b):
        kind = b.get("kind")
        if kind == 'GROUP':
            members = b.get("members") or []
            return "%d bones: %s" % (len(members), ", ".join(members[:3])) + \
                   ("…" if len(members) > 3 else "")
        if kind == 'SLIDER':
            return "%s / %s" % (b.get("sk_object") or "?",
                                b.get("sk_key") or "?")
        return b.get("bone") or "—"

    def _set_cell(self, row, col, text):
        item = QTableWidgetItem(text)
        item.setFlags(item.flags() & ~Qt.ItemIsEditable)
        self.table.setItem(row, col, item)

    def _selected_index(self):
        rows = self.table.selectionModel().selectedRows()
        return rows[0].row() if rows else None

    def _selected_button(self):
        index = self._selected_index()
        buttons = self._status.get("buttons") or []
        if index is None or not 0 <= index < len(buttons):
            return None
        return buttons[index]

    def _set_editors_enabled(self, on):
        for w in (self.edit_label, self.combo_target, self.slider_scale,
                  self.chk_blank, self.btn_colour, self.btn_remove):
            w.setEnabled(on)

    def _sync_editors(self):
        btn = self._selected_button()
        if btn is None:
            self._set_editors_enabled(False)
            return
        self._set_editors_enabled(True)
        self._syncing = True
        try:
            if not self.edit_label.hasFocus():
                self.edit_label.setText(btn.get("label") or "")
            if not self.slider_scale.hasFocus():
                self.slider_scale.setValue(float(btn.get("scale", 1.0)))
            self.chk_blank.setChecked(bool(btn.get("blank")))
            self._fill_targets(btn)
        finally:
            self._syncing = False

    def _fill_targets(self, btn):
        """What this button could point at. A GROUP retargets one member at a
        time (whichever is missing first), a SLIDER its shape key."""
        kind = btn.get("kind")
        if kind == 'SLIDER':
            meshes = self._status.get("meshes") or {}
            keys = meshes.get(btn.get("sk_object") or "", [])
            names, current = list(keys), btn.get("sk_key")
            label = "Shape key"
        elif kind == 'GROUP':
            names = list(self._status.get("bones") or [])
            missing = btn.get("missing") or []
            members = btn.get("members") or []
            current = missing[0] if missing else (members[0] if members else None)
            label = "Member"
        else:
            names = list(self._status.get("bones") or [])
            current = btn.get("bone")
            label = "Bone"
        form = self.combo_target.parentWidget().layout()
        if isinstance(form, QFormLayout):
            lab = form.labelForField(self.combo_target)
            if lab is not None:
                lab.setText(label)
        wanted = list(names)
        if current and current not in wanted:
            wanted = [current] + wanted     # keep an unmatched name visible
        existing = [self.combo_target.itemText(i)
                    for i in range(self.combo_target.count())]
        if existing != wanted:
            self.combo_target.clear()
            self.combo_target.addItems(wanted)
        if current in wanted:
            self.combo_target.setCurrentIndex(wanted.index(current))
        self.combo_target.setEnabled(bool(wanted))

    # ------------------------------------------------------------- editing

    def _on_row_selected(self):
        if self._syncing:
            return
        self._sync_editors()

    def _queue(self, **fields):
        """Coalesce edits to the selected button. A ValueSlider drag emits per
        step, so an un-debounced push would be a bridge command per pixel."""
        index = self._selected_index()
        if index is None:
            return
        if self._pending_index is not None and self._pending_index != index:
            self._flush()               # never land one button's edit on another
        self._pending_index = index
        self._pending.update(fields)
        self._push.start(PUSH_DEBOUNCE_MS)

    def _flush(self):
        if self._pending_index is None or not self._pending:
            return
        index, fields = self._pending_index, dict(self._pending)
        self._pending.clear()
        self._pending_index = None
        self._call(self.bridge.picker_set_button, index, **fields)

    def _on_label(self):
        if self._syncing:
            return
        self._queue(label=self.edit_label.text())
        self._flush()                   # typing is already deliberate

    def _on_scale(self, value):
        if self._syncing:
            return
        self._queue(scale=float(value))

    def _on_blank(self, on):
        if self._syncing:
            return
        self._queue(blank=bool(on))
        self._flush()

    def _on_target(self, _index):
        if self._syncing:
            return
        btn = self._selected_button()
        if btn is None:
            return
        name = self.combo_target.currentText()
        if not name:
            return
        kind = btn.get("kind")
        if kind == 'SLIDER':
            self._queue(sk_key=name)
        elif kind == 'GROUP':
            members = btn.get("members") or []
            missing = btn.get("missing") or []
            target = missing[0] if missing else (members[0] if members else None)
            if target is None or target not in members:
                return
            self._queue(member_index=members.index(target), member_bone=name)
        else:
            self._queue(bone=name)
        self._flush()

    def _on_colour(self):
        btn = self._selected_button()
        if btn is None:
            return
        col = btn.get("color") or [1.0, 1.0, 1.0]
        start = QColor.fromRgbF(*[max(0.0, min(1.0, float(c))) for c in col[:3]])
        chosen = QColorDialog.getColor(start, self, "Button colour")
        if not chosen.isValid():
            return
        self._queue(color=[chosen.redF(), chosen.greenF(), chosen.blueF()])
        self._flush()

    def remove_selected(self):
        index = self._selected_index()
        if index is None:
            return
        buttons = self._status.get("buttons") or []
        name = (buttons[index].get("label") or "this button") \
            if index < len(buttons) else "this button"
        if QMessageBox.question(self, "Remove button",
                                "Remove %r?" % name) != QMessageBox.Yes:
            return
        self._call(self.bridge.picker_remove_buttons, [index])


class PickerPresetsTool(_PickerTool):
    """Save the active tab's layout into the Studio Library as a `.picker` item.

    Loading one is done from the library grid, where the items live and where
    they have their picture — a second Load button here would be a worse copy of
    a browser that already exists.

    ⚠ The thumbnail is the tab's REFERENCE IMAGE, not a viewport render: a
    layout traced over a picture is meaningless without it.
    """

    def __init__(self, bridge, window, parent=None):
        super().__init__(bridge, window, parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        lay.addWidget(self._dim(
            "A saved layout becomes a normal library item — it browses with a "
            "picture, versions on overwrite, and double-clicking it in Studio "
            "Library loads it onto the rig this tab is pointing at."))

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight)
        self.edit_name = QLineEdit()
        self.edit_name.setPlaceholderText("layout name")
        form.addRow("Name", self.edit_name)
        self.edit_folder = QLineEdit()
        self.edit_folder.setPlaceholderText("optional sub-folder, e.g. Lily")
        form.addRow("Folder", self.edit_folder)
        self.chk_overwrite = QCheckBox("Overwrite if it already exists")
        form.addRow("", self.chk_overwrite)
        lay.addLayout(form)

        row = QHBoxLayout()
        self.btn_save = QPushButton("Save to library")
        self.btn_save.clicked.connect(self.save)
        row.addWidget(self.btn_save)
        row.addStretch(1)
        lay.addLayout(row)

        self.status = QLabel("—")
        self.status.setObjectName("dim")
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(self.status)
        lay.addStretch(1)

    def apply_status(self, status):
        if not isinstance(status, dict):
            return
        self._status = status
        buttons = status.get("buttons") or []
        tabs = status.get("tabs") or []
        active = status.get("active_index", 0)
        self.btn_save.setEnabled(bool(buttons))
        if not self.edit_name.text() and 0 <= active < len(tabs):
            self.edit_name.setPlaceholderText(tabs[active].get("name") or
                                              "layout name")
        if not buttons:
            self._ok("Nothing to save — this tab has no buttons yet.")
        else:
            self._ok("%d button(s) ready to save." % len(buttons))

    def save(self):
        root = None
        if self.window is not None and hasattr(self.window,
                                               "current_library_root"):
            root = self.window.current_library_root()
        if not root:
            self._fail("No library to save into — add one in Studio Library "
                       "first.")
            return
        name = self.edit_name.text().strip()
        if not name:
            tabs = self._status.get("tabs") or []
            active = self._status.get("active_index", 0)
            if 0 <= active < len(tabs):
                name = tabs[active].get("name") or ""
        if not name:
            self._fail("Give the layout a name first.")
            return
        result = self._call(self.bridge.picker_save_item, root,
                            self.edit_folder.text().strip(), name,
                            overwrite=self.chk_overwrite.isChecked())
        if result is None:
            return
        # The reply is the whole status with the save result under `saved_*`
        # keys — every picker command answers that way, so that broadcasting a
        # reply to the other tools is always safe.
        note = "Saved %r (%d buttons)" % (name, result.get("saved_buttons", 0))
        # The add-on wrote the bare reference picture; draw the layout onto it
        # so the tile shows the buttons rather than an empty photo.
        if result.get("saved_thumbnail") and result.get("saved_path"):
            if compose_thumbnail(result["saved_path"]):
                note += ", buttons drawn on the preview"
        if not result.get("saved_thumbnail"):
            note += " — no thumbnail: this tab has no reference image."
        self._ok(note)
        if self.window is not None:
            for i in range(self.window.tabs.count()):
                view = self.window.tabs.widget(i)
                if hasattr(view, "rescan"):
                    view.rescan()


class PickerOptionsTool(_PickerTool):
    """How the picker LOOKS while it runs.

    ⚠ These live only in the add-on preferences — this tab is a remote control,
    not a mirror. Anim Layers has a genuine two-store mirror with a
    first-contact rule and an echo guard, and it exists only because the app
    owned those settings first. Don't add a second store here.
    """

    def __init__(self, bridge, window, parent=None):
        super().__init__(bridge, window, parent)
        self._push = QTimer(self)
        self._push.setSingleShot(True)
        self._push.timeout.connect(self._flush)
        self._pending = {}

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        lay.addWidget(self._dim(
            "These are the picker's own appearance settings, stored in "
            "Blender's add-on preferences — the same values the Image Editor "
            "panel shows."))

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight)
        self.slider_alpha = ValueSlider(
            1.0, 100.0, 100.0, decimals=0, suffix="%",
            tooltip="How solid the buttons are drawn")
        self.slider_alpha.valueChanged.connect(
            lambda v: self._queue(btn_alpha=float(v)))
        form.addRow("Button opacity", self.slider_alpha)
        self.slider_round = ValueSlider(
            0.0, 50.0, 7.0, decimals=0, suffix="%",
            tooltip="Corner roundness, as a share of the smaller side")
        self.slider_round.valueChanged.connect(
            lambda v: self._queue(btn_round=float(v)))
        form.addRow("Corner roundness", self.slider_round)
        self.slider_darken = ValueSlider(
            1.0, 100.0, 60.0, decimals=0, suffix="%",
            tooltip="Dim the reference image behind the buttons")
        self.slider_darken.valueChanged.connect(
            lambda v: self._queue(bg_darken=float(v)))
        form.addRow("Darken background", self.slider_darken)
        lay.addLayout(form)

        self.status = QLabel("—")
        self.status.setObjectName("dim")
        self.status.setWordWrap(True)
        lay.addWidget(self.status)
        lay.addStretch(1)

    def apply_status(self, status):
        if not isinstance(status, dict):
            return
        self._status = status
        prefs = status.get("prefs") or {}
        if not prefs:
            self._ok("Blender not connected")
            return
        self._syncing = True
        try:
            for key, widget in (("btn_alpha", self.slider_alpha),
                                ("btn_round", self.slider_round),
                                ("bg_darken", self.slider_darken)):
                if key in prefs and not widget.hasFocus():
                    widget.setValue(float(prefs[key]))
        finally:
            self._syncing = False
        self._ok("")

    def _queue(self, **fields):
        if self._syncing:
            return
        self._pending.update(fields)
        self._push.start(PUSH_DEBOUNCE_MS)

    def _flush(self):
        if not self._pending:
            return
        prefs = dict(self._pending)
        self._pending.clear()
        self._call(self.bridge.picker_set_prefs, prefs)


class PickerSetupPage(QWidget):
    """Tabs & Rig with Presets and Appearance folded into it — Marty, 2026-08-04.

    ⚠ COMPOSED, NOT MERGED, and for a specific reason beyond taste: the three
    tools are separate objects because `PickerTabsTool` owns the tab's ONLY
    poll and fans its status out to the other two over `status_refreshed`.
    Merging them into one class would mean rebuilding that wiring; composing
    them keeps every tool exactly as it was and changes only where it is shown.

    ⚠ `set_capture_busy` has to be forwarded by hand. `PickerPage` only walks
    the widgets it was given through `add_tool`, so without this the two folded
    tools would stay live while Blender is busy — the one thing the greying is
    there to prevent.
    """

    def __init__(self, tabs_tool, presets_tool, options_tool, parent=None):
        super().__init__(parent)
        self.tabs_tool = tabs_tool
        self.presets_tool = presets_tool
        self.options_tool = options_tool

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(14)
        for title, tool in (("Tabs & Rig", tabs_tool),
                            ("Presets", presets_tool),
                            ("Appearance", options_tool)):
            box = QGroupBox(title)
            inner = QVBoxLayout(box)
            inner.setContentsMargins(10, 8, 10, 10)
            inner.addWidget(tool)
            lay.addWidget(box)

    def set_capture_busy(self, busy):
        for tool in (self.tabs_tool, self.presets_tool, self.options_tool):
            if hasattr(tool, "set_capture_busy"):
                tool.set_capture_busy(busy)
