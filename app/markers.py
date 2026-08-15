"""Timeline markers — the Anim Layers tab's marker manager.

Marty's "option D" (2026-08-12): the list on the left, an editor on the right.
Picked over a plain table for one concrete reason — notes are the whole point of
this tool, and a table cell cannot hold a sentence.

WHERE THE DATA LIVES, AND WHY THERE IS NO SYNC PROTOCOL
Nowhere here. Notes and tags are properties ON THE MARKER inside the .blend
(add-on `markers.py`), so this tool and the Blender panel are reading the same
bytes and cannot disagree. All this side needs is to know WHEN to re-read, which
is what `revision` is for: one cheap value that changes whenever anything a list
would show changes, compared on a 1.5 s poll.

⚠ A REFRESH MUST NEVER OVERWRITE A FIELD THE USER IS TYPING IN. That is the
classic bug in any two-window editor, and here it would eat a sentence rather
than a checkbox. Every fill skips a widget that has focus (`_fill_detail`), and
the poll skips the detail pane entirely while an edit is pending.

⚠ ONLY THE FIELD THAT CHANGED IS SENT. `marker_set` writes exactly what it
receives, so a tool that posted the whole marker on every keystroke would
overwrite a note typed in Blender with its own copy from one poll ago.

⚠ MARKER NAMES ARE NOT UNIQUE, and two markers can share a name AND a frame
(measured — BLENDER_NOTES.md). Rows are addressed by the `ref` the add-on
handed back, never by name.
"""

import json
import os

from PySide6.QtCore import QRect, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QDialog,
                               QDialogButtonBox, QFileDialog, QFormLayout,
                               QHBoxLayout, QHeaderView, QInputDialog, QLabel,
                               QLineEdit, QMenu, QMessageBox, QPlainTextEdit,
                               QPushButton, QSpinBox, QSplitter,
                               QStyledItemDelegate, QToolButton, QTreeWidget,
                               QTreeWidgetItem, QVBoxLayout, QWidget)

import bridge as bridgemod
import theme
import widgets

FEATURE = "markers"

# Same cadence as the Anim Layers stack. It only runs while the tool is on
# screen, and a marker list is a handful of strings — this is nothing.
POLL_MS = 1500

# Long enough that typing a sentence is one write, short enough that clicking
# away feels immediate. The same 250-400 ms band as every other debounce here.
WRITE_DELAY_MS = 400

EXPORT_EXT = ".markers"


ROW_HEIGHT = 42
ROW_PAD = 7


class MarkerRowDelegate(QStyledItemDelegate):
    """Two lines per row — name and frame above, the note below.

    Marty's "A2" (2026-08-12), and the reason is the tool's whole purpose: the
    notes ARE the feature, and a single-line row makes you click every marker to
    read them one at a time.

    ⚠ Painted rather than built from widgets. A second label per row would be
    two more QObjects per marker and a layout pass on every rebuild; a delegate
    draws the same pixels with none of that, which is what keeps a poll cheap.
    """

    def paint(self, painter, option, index):
        row = index.data(Qt.ItemDataRole.UserRole) or {}
        painter.save()
        if option.state & option.state.__class__.State_Selected:
            painter.fillRect(option.rect, QColor(theme.ACCENT).darker(180))
        rect = option.rect.adjusted(ROW_PAD, 4, -ROW_PAD, -4)

        frame = str(row.get("frame", ""))
        metrics = painter.fontMetrics()
        frame_w = metrics.horizontalAdvance(frame) + 6
        painter.setPen(QColor(theme.ACCENT))
        painter.drawText(QRect(rect.right() - frame_w, rect.top(), frame_w,
                               metrics.height()),
                         int(Qt.AlignmentFlag.AlignRight), frame)

        layer = row.get("layer") or ""
        layer_w = metrics.horizontalAdvance(layer) + 10 if layer else 0
        if layer:
            painter.setPen(QColor(theme.TEXT_DIM))
            painter.drawText(QRect(rect.right() - frame_w - layer_w, rect.top(),
                                   layer_w, metrics.height()),
                             int(Qt.AlignmentFlag.AlignRight), layer)

        name = row.get("name", "")
        if row.get("tags"):
            name = "%s   %s" % (name, ", ".join(row["tags"]))
        name_w = max(10, rect.width() - frame_w - layer_w - 8)
        bold = QFont(option.font)
        bold.setBold(True)
        painter.setFont(bold)
        painter.setPen(QColor(theme.TEXT))
        painter.drawText(QRect(rect.left(), rect.top(), name_w,
                               metrics.height()),
                         int(Qt.AlignmentFlag.AlignLeft),
                         metrics.elidedText(name, Qt.TextElideMode.ElideRight,
                                            name_w))

        note = (row.get("note") or "").replace("\n", " ").strip()
        painter.setFont(option.font)
        painter.setPen(QColor(theme.TEXT_DIM))
        painter.drawText(QRect(rect.left(), rect.top() + metrics.height() + 2,
                               rect.width(), metrics.height()),
                         int(Qt.AlignmentFlag.AlignLeft),
                         metrics.elidedText(note or "—",
                                            Qt.TextElideMode.ElideRight,
                                            rect.width()))
        painter.restore()

    def sizeHint(self, option, index):
        return QSize(120, ROW_HEIGHT)


def _item_for(row):
    """One list row. Everything visible is painted by MarkerRowDelegate; the
    item exists to carry the data and the accessible text."""
    item = QTreeWidgetItem([row["name"]])
    item.setData(0, Qt.ItemDataRole.UserRole, row)
    item.setToolTip(0, row.get("note") or "")
    return item


def _fill_combo(combo, all_label, values, keep_missing=False):
    """Repopulate a filter combo, keeping the current choice where possible.

    `keep_missing` re-adds the current choice even when it has dropped out of
    `values` — see the layer filter's note in `_rebuild`.
    """
    chosen = combo.currentText()
    combo.blockSignals(True)
    combo.clear()
    combo.addItem(all_label)
    for value in values:
        combo.addItem(value)
    if keep_missing and chosen and chosen != all_label and chosen not in values:
        combo.addItem(chosen)
    idx = combo.findText(chosen)
    combo.setCurrentIndex(idx if idx >= 0 else 0)
    combo.blockSignals(False)


class BatchRenameDialog(widgets.GuardedDialog):
    """Prefix / suffix / find-and-replace, the way the addon this was modelled
    on does it — one pass over every marker."""

    def __init__(self, parent, count):
        super().__init__(parent)
        self.setWindowTitle("Batch rename markers")
        self.setMinimumWidth(360)
        form = QFormLayout(self)
        self.prefix = QLineEdit()
        self.suffix = QLineEdit()
        self.find = QLineEdit()
        self.replace = QLineEdit()
        form.addRow("Prefix", self.prefix)
        form.addRow("Suffix", self.suffix)
        form.addRow("Find", self.find)
        form.addRow("Replace with", self.replace)
        hint = QLabel("Applies to all %d markers." % count)
        hint.setStyleSheet("color: %s;" % theme.TEXT_DIM)
        form.addRow(hint)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok |
                                   QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def values(self):
        return {"prefix": self.prefix.text(), "suffix": self.suffix.text(),
                "find": self.find.text(), "replace": self.replace.text()}


class MarkersTool(QWidget):
    """The markers in the open .blend, with a note and tags on each."""

    def __init__(self, bridge, window, parent=None):
        super().__init__(parent)
        self.bridge = bridge
        self.window = window
        self._markers = []
        self._revision = None
        self._filling = False
        self._pending = {}
        self._pending_ref = None
        self._queue_tool = None
        self._layers_ok = True

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)

        top = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search name, tag or note")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._apply_filter)
        top.addWidget(self.search, 1)
        self.tag_filter = QComboBox()
        self.tag_filter.addItem("All tags")
        self.tag_filter.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.tag_filter.setMinimumContentsLength(8)
        self.tag_filter.currentIndexChanged.connect(self._apply_filter)
        top.addWidget(self.tag_filter)
        self.layer_filter = QComboBox()
        self.layer_filter.addItem("All layers")
        self.layer_filter.setToolTip(self.LAYER_TIP)
        self.layer_filter.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.layer_filter.setMinimumContentsLength(9)
        # ⚠ NOT `_apply_filter` any more. Picking a layer now takes the other
        # layers' markers OUT of the scene so Blender's timeline strip clears
        # too (Marty, 2026-08-12) — it is a bridge call with a real effect, not
        # a view filter.
        self.layer_filter.currentIndexChanged.connect(self._on_layer_pick)
        top.addWidget(self.layer_filter)

        sets = QHBoxLayout()
        sets.setContentsMargins(0, 0, 0, 0)
        self.set_combo = QComboBox()
        self.set_combo.addItem("Marker sets")
        self.set_combo.setToolTip(
            "Named sets of markers saved inside this .blend")
        self.set_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.set_combo.setMinimumContentsLength(10)
        sets.addWidget(self.set_combo)
        self.set_save = QPushButton("Save set")
        self.set_save.setToolTip(
            "Save every marker in this file under a name, inside the .blend")
        self.set_save.clicked.connect(self._save_set)
        sets.addWidget(self.set_save)
        self.set_load = QPushButton("Load")
        self.set_load.clicked.connect(self._load_set)
        sets.addWidget(self.set_load)
        self.set_delete = QPushButton("Delete")
        self.set_delete.clicked.connect(self._delete_set)
        sets.addWidget(self.set_delete)
        top.addLayout(sets)
        self.tools_button = QToolButton()
        self.tools_button.setText("Tools")
        self.tools_button.setPopupMode(
            QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(self.tools_button)
        menu.addAction("Bind cameras by name", self._bind_by_name)
        menu.addAction("Batch rename…", self._batch_rename)
        menu.addSeparator()
        menu.addAction("Import markers…", self._import)
        menu.addAction("Export markers…", self._export)
        self.tools_button.setMenu(menu)
        top.addWidget(self.tools_button)
        root.addLayout(top)

        split = QSplitter(Qt.Orientation.Horizontal)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(1)
        self.tree.setHeaderHidden(True)
        self.tree.setRootIsDecorated(False)
        self.tree.setAllColumnsShowFocus(True)
        self.tree.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection)
        self.tree.header().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch)
        self.tree.setItemDelegate(MarkerRowDelegate(self.tree))
        self.tree.setUniformRowHeights(True)
        self.tree.currentItemChanged.connect(self._on_row_changed)
        self.tree.itemDoubleClicked.connect(lambda *_: self._jump())
        self.tree.setMinimumWidth(150)
        split.addWidget(self.tree)

        self.detail = QWidget()
        form = QVBoxLayout(self.detail)
        form.setContentsMargins(10, 0, 0, 0)
        self.title = QLabel("—")
        self.title.setStyleSheet("color: %s; font-size: 13px;" % theme.TEXT_HEAD)
        form.addWidget(self.title)

        grid = QFormLayout()
        grid.setContentsMargins(0, 6, 0, 0)
        self.name_edit = QLineEdit()
        self.name_edit.textEdited.connect(
            lambda t: self._queue_write("name", t))
        grid.addRow("Name", self.name_edit)
        self.frame_spin = QSpinBox()
        self.frame_spin.setRange(-1000000, 1000000)
        self.frame_spin.valueChanged.connect(
            lambda v: self._queue_write("frame", int(v)))
        grid.addRow("Frame", self.frame_spin)
        cam_row = QHBoxLayout()
        self.camera_label = QLabel("—")
        cam_row.addWidget(self.camera_label, 1)
        self.camera_button = QPushButton("Clear")
        self.camera_button.clicked.connect(self._clear_camera)
        cam_row.addWidget(self.camera_button)
        cam_box = QWidget()
        cam_box.setLayout(cam_row)
        cam_row.setContentsMargins(0, 0, 0, 0)
        grid.addRow("Camera", cam_box)
        # ⚠ EDITABLE combo: picking an existing layer and typing a new one are
        # the same control, because that IS how a layer is created — layers are
        # derived from what the markers say, so there is nothing else to add
        # one to (see `layers_in_use` in the add-on).
        self.layer_edit = QComboBox()
        self.layer_edit.setEditable(True)
        self.layer_edit.lineEdit().setPlaceholderText("no layer")
        self.layer_edit.currentTextChanged.connect(self._layer_changed)
        grid.addRow("Layer", self.layer_edit)
        self.tags_edit = QLineEdit()
        self.tags_edit.setPlaceholderText("hero, wip")
        self.tags_edit.textEdited.connect(
            lambda t: self._queue_write("tags", t))
        grid.addRow("Tags", self.tags_edit)
        form.addLayout(grid)

        form.addWidget(QLabel("Note"))
        self.note_edit = QPlainTextEdit()
        self.note_edit.setPlaceholderText(
            "What you want to remember about this frame")
        self.note_edit.setMinimumHeight(90)
        self.note_edit.textChanged.connect(self._note_changed)
        form.addWidget(self.note_edit, 1)

        actions = QHBoxLayout()
        self.jump_button = QPushButton("Jump to frame")
        self.jump_button.clicked.connect(self._jump)
        actions.addWidget(self.jump_button)
        self.render_button = QPushButton("Render at marker")
        self.render_button.setToolTip(
            "Save the open .blend and add this single frame to the Render "
            "Queue. It does not start rendering.")
        self.render_button.clicked.connect(self._render)
        actions.addWidget(self.render_button)
        actions.addStretch(1)
        form.addLayout(actions)
        split.addWidget(self.detail)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 2)
        root.addWidget(split, 1)

        # ⚠ Markers missing from the timeline with nothing saying why is
        # indistinguishable from having lost them. Both UIs say it out loud.
        self.hidden_label = QLabel("")
        self.hidden_label.setStyleSheet("color: %s;" % theme.TEXT_DIM)
        self.hidden_label.setVisible(False)
        root.addWidget(self.hidden_label)

        bottom = QHBoxLayout()
        self.add_button = QPushButton("Add at playhead")
        self.add_button.clicked.connect(self._add)
        bottom.addWidget(self.add_button)
        self.remove_button = QPushButton("Remove")
        self.remove_button.clicked.connect(self._remove)
        bottom.addWidget(self.remove_button)
        bottom.addStretch(1)
        self.status = QLabel("")
        self.status.setStyleSheet("color: %s;" % theme.TEXT_DIM)
        bottom.addWidget(self.status)
        root.addLayout(bottom)

        self._timer = QTimer(self)
        self._timer.setInterval(POLL_MS)
        self._timer.timeout.connect(self._poll)

        self._write_timer = QTimer(self)
        self._write_timer.setSingleShot(True)
        self._write_timer.setInterval(WRITE_DELAY_MS)
        self._write_timer.timeout.connect(self._flush)

        self._show_detail(False)

    # ------------------------------------------------------------ lifecycle

    def showEvent(self, event):
        super().showEvent(event)
        self._timer.start()
        # ⚠ `poll=True` even though this is not the repeating timer. Switching
        # to the tab is automatic, not a button press, and a connect to a dead
        # localhost port is DROPPED rather than refused on Marty's machine — so
        # `poll=False` would stall the tab switch for the connect timeout every
        # time Blender is closed. The timer behind it retries 1.5 s later, and
        # the health poll reopens the gate the moment the bridge is really back
        # (docs\app-shell.md).
        self.refresh(poll=True)

    def hideEvent(self, event):
        # ⚠ Flush BEFORE stopping: switching tabs mid-sentence must not throw
        # the sentence away. The write is a few milliseconds.
        self._flush()
        self._timer.stop()
        super().hideEvent(event)

    # -------------------------------------------------------------- helpers

    def feature_reason(self):
        """Why markers are unavailable on the installed add-on, or None."""
        try:
            return self.bridge.feature_reason(FEATURE)
        except Exception:               # noqa: BLE001 — a dead bridge is routine
            return None                 # fail OPEN: unknown is not "missing"

    def set_queue_tool(self, tool):
        """The Render Queue, for the Render button. A callable-free handoff
        would be nicer, but the queue is a widget with its own state — and the
        button hides itself when there is no queue, so the standalone render
        manager can host this tool unchanged."""
        self._queue_tool = tool
        self.render_button.setVisible(tool is not None)

    def _ref(self, row=None):
        row = self.current() if row is None else row
        if not row:
            return None
        return {"uid": row.get("uid") or "", "index": row.get("index"),
                "name": row.get("name"), "frame": row.get("frame")}

    def current(self):
        item = self.tree.currentItem()
        return item.data(0, Qt.ItemDataRole.UserRole) if item else None

    def _show_detail(self, on):
        # A column of empty fields says nothing — the Studio Library learned
        # this one the hard way (docs\app-shell.md).
        self.detail.setVisible(bool(on))

    def _fail(self, exc):
        self.status.setText(str(exc))

    # ---------------------------------------------------------------- reads

    def refresh(self, poll=False):
        reason = self.feature_reason()
        if reason:
            self._set_enabled(False)
            self.status.setText(reason)
            return
        self._set_enabled(True)
        try:
            data = self.bridge.marker_list(poll=poll)
        except bridgemod.BridgeError as exc:
            if not poll:
                self._fail(exc)
            return
        # ⚠ THE ECHO IS THE ONLY WAY TO SEE THIS. Layers grew `marker_set` a
        # PARAMETER rather than adding a command, and a command that exists in
        # every version answers "yes" to any capability probe — so the reply
        # carrying a `layers` key is the whole test (docs\addon-bridge.md,
        # "Choosing the SHAPE of a change"). On an add-on without it the layer
        # controls grey out instead of silently doing nothing.
        self._set_layers_supported("layers" in (data or {}))
        self._revision = data.get("revision")
        self._markers = data.get("markers") or []
        self._rebuild(data)

    LAYER_TIP = ("Show only one layer's markers. With none picked they all "
                 "show.")
    LAYER_GATE = ("Marker layers need Blender add-on 0.41.0 or newer — update "
                  "the extension from ⚙ Library Settings.")

    def _set_layers_supported(self, on):
        """⚠ RUNS IN FULL EVERY TIME — no early return on 'nothing changed'.

        `_set_enabled(True)` at the top of every `refresh` re-enables these two
        controls along with everything else, so a memoised version left them
        LIVE from the second refresh onward on an add-on that cannot store a
        layer — usable, and silently doing nothing, which is the exact failure
        the gate exists to prevent. It also has to hand them BACK when a newer
        add-on arrives mid-session (the same rule as the Render presets gate,
        docs\\app-shell.md). Pinned by app_markers_test.
        """
        self._layers_ok = bool(on)
        for w in (self.layer_filter, self.layer_edit):
            w.setEnabled(self._layers_ok)
            w.setToolTip(self.LAYER_TIP if self._layers_ok else self.LAYER_GATE)

    def _poll(self):
        if self._pending:
            # An edit is in flight — re-reading now would race our own write
            # back over the field being typed in.
            return
        reason = self.feature_reason()
        if reason:
            return
        try:
            data = self.bridge.marker_list(poll=True)
        except bridgemod.BridgeError:
            return                       # a dead bridge is not an error here
        if data.get("revision") == self._revision:
            return                       # nothing changed in Blender
        self._revision = data.get("revision")
        self._markers = data.get("markers") or []
        self._rebuild(data)

    def _set_enabled(self, on):
        for w in (self.tree, self.search, self.tag_filter, self.layer_filter,
                  self.tools_button, self.add_button, self.remove_button,
                  self.detail, self.set_combo, self.set_save, self.set_load,
                  self.set_delete):
            w.setEnabled(bool(on))

    def _rebuild(self, data):
        tags = data.get("tags") or []
        layers = data.get("layers") or []
        keep = self.current()
        keep_uid = (keep or {}).get("uid")
        keep_key = ((keep or {}).get("name"), (keep or {}).get("frame"))
        self._filling = True
        self.tree.clear()
        for row in self._markers:
            self.tree.addTopLevelItem(_item_for(row))
        _fill_combo(self.tag_filter, "All tags", tags)
        # ⚠ The layer combo is driven by what Blender says it is SHOWING, not by
        # what the user last clicked — the add-on is the one that actually parks
        # the markers, so it owns that state. `keep_missing` covers the layer
        # whose last marker was just renamed away while you were still in it.
        _fill_combo(self.layer_filter, "All layers", layers, keep_missing=True)
        shown = data.get("showing_layer") or ""
        self.layer_filter.blockSignals(True)
        idx = self.layer_filter.findText(shown) if shown else 0
        self.layer_filter.setCurrentIndex(idx if idx >= 0 else 0)
        self.layer_filter.blockSignals(False)
        _fill_combo(self.set_combo, "Marker sets", data.get("sets") or [])
        hidden = int(data.get("hidden") or 0)
        self.hidden_label.setText(
            "%d marker%s hidden by this layer" % (hidden, "" if hidden == 1
                                                  else "s") if hidden else "")
        self.hidden_label.setVisible(bool(hidden))
        # The editor's own combo is the LIST of layers to pick from; its text
        # is the selected marker's layer and is set by `_fill_detail`.
        self.layer_edit.blockSignals(True)
        text = self.layer_edit.currentText()
        self.layer_edit.clear()
        self.layer_edit.addItem("")
        for name in layers:
            self.layer_edit.addItem(name)
        self.layer_edit.setCurrentText(text)
        self.layer_edit.blockSignals(False)

        # ⚠ `_filling` STAYS TRUE ACROSS THE SELECTION RESTORE BELOW, and that
        # is load-bearing. `setCurrentItem` emits `currentItemChanged`, whose
        # handler fills the editor with force=True — so a poll that rebuilt the
        # list would re-select the same marker and overwrite the note being
        # typed, right past the focus rule. Caught by app_markers_test.
        restored = None
        for i in range(self.tree.topLevelItemCount()):
            row = self.tree.topLevelItem(i).data(0, Qt.ItemDataRole.UserRole)
            if keep_uid and row.get("uid") == keep_uid:
                restored = i
                break
            if not keep_uid and (row.get("name"), row.get("frame")) == keep_key:
                restored = i
                break
        if restored is not None:
            self.tree.setCurrentItem(self.tree.topLevelItem(restored))
        elif self.tree.topLevelItemCount() and keep is None:
            self.tree.setCurrentItem(self.tree.topLevelItem(0))
        self._filling = False
        self._apply_filter()
        # NOT forced — this is the path that must respect the focus rule.
        self._fill_detail()
        self.status.setText("%d marker%s" % (len(self._markers),
                                             "" if len(self._markers) == 1
                                             else "s"))

    def _apply_filter(self):
        needle = self.search.text().lower().strip()
        tag = self.tag_filter.currentText()
        tag = "" if self.tag_filter.currentIndex() <= 0 else tag.lower()
        # ⚠ NO LAYER FILTERING HERE ANY MORE. Showing a layer removes the other
        # markers from the scene, so `marker_list` never returns them — filtering
        # again would be a second, silent copy of the rule, and the two would
        # drift. Search and tags only.
        for i in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(i)
            row = item.data(0, Qt.ItemDataRole.UserRole) or {}
            hay = "%s %s %s %s" % (row.get("name", ""),
                                   " ".join(row.get("tags") or []),
                                   row.get("note", ""), row.get("layer") or "")
            ok = needle in hay.lower() if needle else True
            if ok and tag:
                ok = tag in [t.lower() for t in (row.get("tags") or [])]
            item.setHidden(not ok)

    def active_layer(self):
        """The layer being shown, or "" for all of them."""
        if self.layer_filter.currentIndex() <= 0:
            return ""
        return self.layer_filter.currentText()

    def _on_layer_pick(self):
        """Ask Blender to show one layer and put the rest away.

        ⚠ This is a WRITE, not a filter. The add-on takes the other layers'
        markers out of the scene so Blender's own timeline strip clears — which
        is the only way, because Blender always draws every marker it has.
        """
        if self._filling:
            return
        self._flush()
        try:
            self.bridge.marker_show_layer(self.active_layer())
        except bridgemod.BridgeError as exc:
            self._fail(exc)
            return
        self.refresh()

    def _chosen_set(self):
        if self.set_combo.currentIndex() <= 0:
            return ""
        return self.set_combo.currentText()

    def _save_set(self):
        name, ok_pressed = QInputDialog.getText(
            self, "Save marker set", "Name this set of markers:",
            text=self._chosen_set() or "Markers")
        if not ok_pressed or not name.strip():
            return
        try:
            reply = self.bridge.marker_set_save(name.strip())
        except bridgemod.BridgeError as exc:
            self._fail(exc)
            return
        self.refresh()
        self.status.setText("Saved '%s' (%d markers) into the .blend"
                            % (reply.get("saved"), reply.get("count", 0)))

    def _load_set(self):
        name = self._chosen_set()
        if not name:
            self.status.setText("Pick a set to load first.")
            return
        # ⚠ It REPLACES every marker in the file. Ask.
        if QMessageBox.question(
                self, "Load marker set",
                "Replace every marker in this scene with '%s'?\n\n"
                "Anything not saved in a set is lost." % name) != \
                QMessageBox.StandardButton.Yes:
            return
        try:
            reply = self.bridge.marker_set_load(name)
        except bridgemod.BridgeError as exc:
            self._fail(exc)
            return
        self.refresh()
        self.status.setText("Loaded '%s' (%d markers)"
                            % (name, reply.get("count", 0)))

    def _delete_set(self):
        name = self._chosen_set()
        if not name:
            return
        if QMessageBox.question(
                self, "Delete marker set",
                "Forget the saved set '%s'?\n\nThe markers in your scene stay "
                "as they are." % name) != QMessageBox.StandardButton.Yes:
            return
        try:
            self.bridge.marker_set_delete(name)
        except bridgemod.BridgeError as exc:
            self._fail(exc)
            return
        self.refresh()
        self.status.setText("Deleted the set '%s'" % name)

    def _fill_detail(self, force=False):
        """Put the selected marker into the editor.

        ⚠ SKIPS ANY WIDGET THAT HAS FOCUS unless forced. A poll landing while
        Marty is halfway through a note must not replace what he has typed with
        what Blender had a second ago.
        """
        row = self.current()
        self._show_detail(row is not None)
        if row is None:
            return
        self._filling = True
        self.title.setText(row["name"])
        if force or not self.name_edit.hasFocus():
            self.name_edit.setText(row["name"])
        if force or not self.frame_spin.hasFocus():
            self.frame_spin.setValue(int(row["frame"]))
        if force or not self.tags_edit.hasFocus():
            self.tags_edit.setText(", ".join(row.get("tags") or []))
        if force or not self.layer_edit.hasFocus():
            self.layer_edit.setCurrentText(row.get("layer") or "")
        if force or not self.note_edit.hasFocus():
            self.note_edit.setPlainText(row.get("note") or "")
        cam = row.get("camera")
        self.camera_label.setText(cam or "not bound")
        self.camera_button.setEnabled(bool(cam))
        self._filling = False

    def _on_row_changed(self, current, previous):
        if self._filling:
            return
        # ⚠ Flush against the row we were ON, not the one just clicked.
        self._flush()
        self._fill_detail(force=True)

    # --------------------------------------------------------------- writes

    def _note_changed(self):
        if self._filling:
            return
        self._queue_write("note", self.note_edit.toPlainText())

    def _layer_changed(self, text):
        if self._filling:
            return
        self._queue_write("layer", text.strip())

    def _queue_write(self, field, value):
        if self._filling:
            return
        ref = self._ref()
        if ref is None:
            return
        if self._pending and self._pending_ref != ref:
            self._flush()
            ref = self._ref()
        self._pending_ref = ref
        if field == "tags":
            value = [t.strip() for t in str(value).split(",") if t.strip()]
        self._pending[field] = value
        self._write_timer.start()

    def _flush(self):
        """Send whatever is pending, now."""
        self._write_timer.stop()
        if not self._pending or self._pending_ref is None:
            return
        fields = dict(self._pending)
        ref = dict(self._pending_ref)
        self._pending = {}
        self._pending_ref = None
        try:
            reply = self.bridge.marker_set(ref, **fields)
        except bridgemod.BridgeError as exc:
            self._fail(exc)
            return
        self._revision = reply.get("revision")
        updated = reply.get("marker")
        if updated:
            self._apply_row(updated)

    def _apply_row(self, updated):
        """Fold one changed marker back into the list without a full rebuild —
        a rebuild would move the cursor out of the field being typed in."""
        # ⚠ MATCH ON UID *OR* SLOT, both here and in the tree below. The first
        # write to a marker is the one that MINTS its uid, so the copy in hand
        # still has `uid: ""` while the reply carries a real one — a uid-only
        # match silently misses exactly that write, and `_revision` has already
        # been advanced past it, so no poll comes along to repair it. Export
        # reads this list, so the note would be missing from the file.
        for i, row in enumerate(self._markers):
            if (row.get("uid") and row["uid"] == updated.get("uid")) or \
                    row.get("index") == updated.get("index"):
                self._markers[i] = updated
                break
        for i in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(i)
            row = item.data(0, Qt.ItemDataRole.UserRole) or {}
            same_uid = row.get("uid") and row["uid"] == updated.get("uid")
            same_slot = row.get("index") == updated.get("index")
            if same_uid or same_slot:
                fresh = _item_for(updated)
                item.setData(0, Qt.ItemDataRole.UserRole, updated)
                for col in range(self.tree.columnCount()):
                    item.setText(col, fresh.text(col))
                item.setToolTip(0, fresh.toolTip(0))
                if item is self.tree.currentItem():
                    self.title.setText(updated["name"])
                break
        # A layer may have just been created or emptied, and the row may no
        # longer belong in a filtered view.
        self._refresh_layer_lists()
        self._apply_filter()

    def _refresh_layer_lists(self):
        """Re-derive the layer choices from the rows in hand.

        ⚠ Needed because `_flush` advances `_revision` to the value the reply
        carried, so the poll sees nothing changed and never rebuilds — without
        this, a layer you just typed would not appear in the filter until
        something else forced a refresh.

        ⚠ The editor's combo is left alone while it HAS FOCUS: repopulating a
        combo resets its edit text, which is the same class of bug as a poll
        overwriting a note.
        """
        layers = sorted({(r.get("layer") or "") for r in self._markers} - {""})
        _fill_combo(self.layer_filter, "All layers", layers, keep_missing=True)
        if self.layer_edit.hasFocus():
            return
        self._filling = True
        text = self.layer_edit.currentText()
        self.layer_edit.blockSignals(True)
        self.layer_edit.clear()
        self.layer_edit.addItem("")
        for name in layers:
            self.layer_edit.addItem(name)
        self.layer_edit.setCurrentText(text)
        self.layer_edit.blockSignals(False)
        self._filling = False

    def _add(self):
        # ⚠ A new marker joins the layer you are LOOKING AT. Adding one while a
        # layer is filtered and having it land outside that layer would make it
        # vanish the moment it was created, which reads as the button being
        # broken.
        try:
            self.bridge.marker_add("Marker", layer=self.active_layer())
        except bridgemod.BridgeError as exc:
            self._fail(exc)
            return
        self.refresh()

    def _remove(self):
        ref = self._ref()
        if ref is None:
            return
        row = self.current()
        if QMessageBox.question(
                self, "Remove marker",
                "Remove '%s' at frame %s?\n\nIts note and tags go with it."
                % (row.get("name"), row.get("frame"))) != \
                QMessageBox.StandardButton.Yes:
            return
        self._pending = {}
        self._pending_ref = None
        try:
            self.bridge.marker_remove(ref)
        except bridgemod.BridgeError as exc:
            self._fail(exc)
            return
        self.refresh()

    def _clear_camera(self):
        ref = self._ref()
        if ref is None:
            return
        try:
            reply = self.bridge.marker_set(ref, camera=None)
        except bridgemod.BridgeError as exc:
            self._fail(exc)
            return
        self._revision = reply.get("revision")
        if reply.get("marker"):
            self._apply_row(reply["marker"])
        self._fill_detail(force=True)

    def _jump(self):
        ref = self._ref()
        if ref is None:
            return
        self._flush()
        try:
            reply = self.bridge.marker_goto(self._ref())
        except bridgemod.BridgeError as exc:
            self._fail(exc)
            return
        self.status.setText("Jumped to frame %s" % reply.get("frame"))

    def _render(self):
        """Save the open file and put this one frame in the Render Queue.

        ⚠ IT DOES NOT RENDER OVER THE BRIDGE. A foreground render is one call
        that takes minutes, and every other tab's poll queues behind it
        (docs\\app-shell.md). The queue renders in its own headless Blender,
        which is what that tool is for.
        """
        row = self.current()
        if row is None or self._queue_tool is None:
            return
        self._flush()
        try:
            saved = self.bridge.save_blend()
        except bridgemod.BridgeError as exc:
            self._fail(exc)
            return
        path = (saved or {}).get("path")
        if not path:
            self.status.setText("Save the .blend in Blender first.")
            return
        queued, message = self._queue_tool.queue_at_frame(
            path, row["frame"], label=row.get("name") or "")
        self.status.setText(message)
        if not queued:
            QMessageBox.information(self, "Render at marker", message)

    def _bind_by_name(self):
        try:
            reply = self.bridge.marker_bind_by_name()
        except bridgemod.BridgeError as exc:
            self._fail(exc)
            return
        self.refresh()
        count = reply.get("count", 0)
        self.status.setText(
            "Bound %d marker%s to a camera of the same name"
            % (count, "" if count == 1 else "s") if count
            else "No marker shares a name with a camera")

    def _batch_rename(self):
        dlg = BatchRenameDialog(self, len(self._markers))
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        values = dlg.values()
        if not any(values.values()):
            return
        try:
            reply = self.bridge.marker_rename(**values)
        except bridgemod.BridgeError as exc:
            self._fail(exc)
            return
        self.refresh()
        self.status.setText("Renamed %d marker(s)" % reply.get("count", 0))

    # ------------------------------------------------------ import / export

    def _export(self):
        if not self._markers:
            self.status.setText("There are no markers to export.")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Export markers", "markers" + EXPORT_EXT,
            "Marker files (*%s);;JSON (*.json)" % EXPORT_EXT)
        if not path:
            return
        payload = {
            "format": "madi-markers",
            "version": 1,
            # Only what a marker IS — never the uid, which belongs to the file
            # it came from and would collide on import.
            "markers": [{"name": m["name"], "frame": m["frame"],
                         "note": m.get("note", ""),
                         "tags": m.get("tags") or [],
                         "layer": m.get("layer") or "",
                         "camera": m.get("camera")}
                        for m in self._markers],
        }
        try:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=1)
        except OSError as exc:
            self._fail(exc)
            return
        self.status.setText("Exported %d markers to %s"
                            % (len(self._markers), os.path.basename(path)))

    def _import(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Import markers", "",
            "Marker files (*%s *.marker *.json)" % EXPORT_EXT)
        if not path:
            return
        rows = read_marker_file(path)
        if rows is None:
            QMessageBox.warning(self, "Import markers",
                                "That file does not look like a marker export.")
            return
        added = 0
        for row in rows:
            try:
                self.bridge.marker_add(row.get("name") or "Marker",
                                       row.get("frame"),
                                       note=row.get("note", ""),
                                       tags=row.get("tags") or None,
                                       layer=row.get("layer") or "")
            except bridgemod.BridgeError as exc:
                self._fail(exc)
                break
            added += 1
        self.refresh()
        self.status.setText("Imported %d marker%s"
                            % (added, "" if added == 1 else "s"))


def read_marker_file(path):
    """Markers out of an export file, or None if it is not one.

    Reads our own `.markers` and the plain `{"markers": [...]}` / bare-list
    shapes a `.marker` file from elsewhere can take — the extension is JSON
    either way, and refusing a file over its wrapper would be pointless
    strictness. Anything without a usable name and frame is skipped rather
    than failing the whole import.
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if isinstance(data, dict):
        rows = data.get("markers")
    elif isinstance(data, list):
        rows = data
    else:
        return None
    if not isinstance(rows, list):
        return None
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        frame = row.get("frame")
        if isinstance(frame, str) and frame.lstrip("-").isdigit():
            frame = int(frame)
        if not isinstance(frame, int):
            continue
        tags = row.get("tags")
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(",") if t.strip()]
        out.append({"name": str(row.get("name") or "Marker"),
                    "frame": frame,
                    "note": str(row.get("note") or ""),
                    "layer": str(row.get("layer") or ""),
                    "tags": tags if isinstance(tags, list) else []})
    return out
