"""Tools for the Anim Layers tab.

A standard animation-layers workflow over Blender's NLA, driven through the
bridge: every layer is one NLA track holding one strip, the selected layer is
edited in tweak mode (keys/auto-key land in it) while the whole stack keeps
evaluating. The stack list below polls Blender while the tab is visible, so
changes made either side stay in sync.
"""

import json

from PySide6.QtCore import QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDoubleSpinBox, QFrame,
                               QHBoxLayout, QInputDialog, QLabel, QListWidget,
                               QListWidgetItem, QMessageBox, QPushButton,
                               QSpinBox, QToolButton, QVBoxLayout, QWidget)

import bridge as bridgemod
import theme

BLEND_TYPES = ("REPLACE", "COMBINE", "ADD", "SUBTRACT", "MULTIPLY")
POLL_MS = 1500


class DragSlider(QWidget):
    """Blender-style value slider: click and drag left/right to change the
    value (shown as a filled bar with a percentage), double-click to type.
    `preview` fires throttled while dragging, `committed` on release."""

    preview = Signal(float)
    committed = Signal(float)

    DRAG_RANGE_PX = 150.0        # pixels of mouse travel for the full 0..1

    def __init__(self, parent=None):
        super().__init__(parent)
        self._value = 1.0
        self._start_x = 0.0
        self._start_value = 0.0
        self._moved = False
        self._pending = None
        self.dragging = False
        self._timer = QTimer(self)
        self._timer.setInterval(120)
        self._timer.timeout.connect(self._flush_preview)
        self.setFixedSize(86, 22)
        self.setCursor(Qt.SizeHorCursor)
        self.setToolTip("Influence — click-drag left/right (0–100%), "
                        "double-click to type a value")

    def value(self):
        return self._value

    def set_value(self, v):
        v = max(0.0, min(1.0, float(v)))
        if abs(v - self._value) > 1e-4:
            self._value = v
            self.update()

    # ------------------------------------------------------------- painting

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        if not self.isEnabled():
            p.setOpacity(0.35)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setPen(QPen(QColor(theme.BORDER)))
        p.setBrush(QColor(theme.BG))
        p.drawRoundedRect(r, 3, 3)
        if self._value > 0.0:
            fill = QRectF(r.x() + 1, r.y() + 1,
                          (r.width() - 2) * self._value, r.height() - 2)
            p.setPen(Qt.NoPen)
            c = QColor(theme.ACCENT)
            c.setAlpha(110)
            p.setBrush(c)
            p.drawRoundedRect(fill, 2, 2)
        p.setPen(QPen(QColor(theme.TEXT)))
        p.drawText(self.rect(), Qt.AlignCenter,
                   "%d%%" % round(self._value * 100))

    # ------------------------------------------------------------- mouse

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._start_x = event.position().x()
            self._start_value = self._value
            self._moved = False
            self.dragging = True
            self._timer.start()
            event.accept()

    def mouseMoveEvent(self, event):
        if self.dragging:
            dx = event.position().x() - self._start_x
            if abs(dx) > 2:
                self._moved = True
            self.set_value(self._start_value + dx / self.DRAG_RANGE_PX)
            self._pending = self._value
            event.accept()

    def mouseReleaseEvent(self, event):
        if self.dragging:
            self.dragging = False
            self._timer.stop()
            self._pending = None
            if self._moved:
                self.committed.emit(self._value)
            event.accept()

    def mouseDoubleClickEvent(self, event):
        self._type_value()
        event.accept()               # don't bubble into the row's rename

    def _type_value(self):
        val, ok = QInputDialog.getInt(self, "Influence", "Influence %:",
                                      round(self._value * 100), 0, 100)
        if ok:
            self.set_value(val / 100.0)
            self.committed.emit(self._value)

    def _flush_preview(self):
        if self._pending is not None:
            v, self._pending = self._pending, None
            self.preview.emit(v)


class LayerRow(QWidget):
    """One stack row: solo / name / blend / influence + key / mute / lock.
    Displayed top layer first; `index` stays the REAL (bottom-first) index."""

    solo_clicked = Signal(int)
    mute_toggled = Signal(int, bool)
    lock_toggled = Signal(int, bool)
    blend_changed = Signal(int, str)
    influence_changed = Signal(int, float)
    influence_preview = Signal(int, float)
    influence_key_toggled = Signal(int, bool)
    rename_requested = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.index = -1
        self._syncing = False
        lay = QHBoxLayout(self)
        lay.setContentsMargins(4, 1, 4, 1)
        lay.setSpacing(4)

        self.btn_solo = QToolButton()
        self.btn_solo.setText("★")
        self.btn_solo.setCheckable(True)
        self.btn_solo.setToolTip("Solo — hear only this layer (mutes the "
                                 "rest, un-solo restores their mutes)")
        self.btn_solo.clicked.connect(
            lambda: self.solo_clicked.emit(self.index))
        lay.addWidget(self.btn_solo)

        self.name = QLabel("—")
        self.name.setMinimumWidth(90)
        self.name.setToolTip("Double-click to rename")
        lay.addWidget(self.name, 1)

        self.reason = QLabel("")
        self.reason.setObjectName("dim")
        lay.addWidget(self.reason)

        self.blend = QComboBox()
        for b in BLEND_TYPES:
            self.blend.addItem(b.title(), b)
        self.blend.setToolTip("Blend type — how this layer combines with the "
                              "layers below it")
        self.blend.currentIndexChanged.connect(self._on_blend)
        lay.addWidget(self.blend)

        self.influence = DragSlider()
        self.influence.committed.connect(self._on_influence)
        self.influence.preview.connect(self._on_influence_preview)
        lay.addWidget(self.influence)

        self.btn_key = QToolButton()
        self.btn_key.setText("+")
        self.btn_key.setCheckable(True)
        self.btn_key.setToolTip("Animate influence — on: dragging the slider "
                                "keys influence at the current frame; off: "
                                "influence collapses back to one static "
                                "value (the current one)")
        self.btn_key.clicked.connect(self._on_key_toggle)
        lay.addWidget(self.btn_key)

        self.btn_mute = QToolButton()
        self.btn_mute.setText("👁")
        self.btn_mute.setCheckable(True)
        self.btn_mute.setToolTip("Mute layer (also excludes it from bakes)")
        self.btn_mute.clicked.connect(self._on_mute)
        lay.addWidget(self.btn_mute)

        self.btn_lock = QToolButton()
        self.btn_lock.setText("🔒")
        self.btn_lock.setCheckable(True)
        self.btn_lock.setToolTip("Lock layer — its settings freeze and "
                                 "selecting it stops editing (no tweak mode)")
        self.btn_lock.clicked.connect(self._on_lock)
        lay.addWidget(self.btn_lock)

    # ------------------------------------------------------------------

    def mouseDoubleClickEvent(self, event):
        self.rename_requested.emit(self.index)
        event.accept()

    def _on_blend(self, _i):
        if not self._syncing:
            self.blend_changed.emit(self.index, self.blend.currentData())

    def _on_influence(self, value):
        if not self._syncing:
            self.influence_changed.emit(self.index, value)

    def _on_influence_preview(self, value):
        if not self._syncing:
            self.influence_preview.emit(self.index, value)

    def _on_key_toggle(self):
        if not self._syncing:
            self.influence_key_toggled.emit(self.index,
                                            self.btn_key.isChecked())

    def _on_mute(self):
        if not self._syncing:
            self.mute_toggled.emit(self.index, self.btn_mute.isChecked())

    def _on_lock(self):
        if not self._syncing:
            self.lock_toggled.emit(self.index, self.btn_lock.isChecked())

    def apply(self, row, solo_name):
        """Mirror one status row into the widgets without echoing signals."""
        self._syncing = True
        try:
            self.index = row["index"]
            label = row["name"]
            if row.get("keys") == 0 and "blend_type" in row:
                label += "   (empty — pose and key to fill it)"
            self.name.setText(label)
            self.btn_solo.setChecked(row.get("solo", False))
            self.btn_mute.setChecked(row.get("mute", False))
            self.btn_lock.setChecked(row.get("lock", False))
            healthy = "blend_type" in row
            reason = row.get("locked_reason")
            # a track-locked layer still has its strip fields; only structural
            # problems (multi-strip / empty foreign tracks) hide the controls
            structural = reason is not None and reason != "track locked"
            self.reason.setText("(%s)" % reason if structural else "")
            for w in (self.blend, self.influence, self.btn_key):
                w.setVisible(healthy)
                w.setEnabled(healthy and reason is None)
            if healthy:
                i = self.blend.findData(row["blend_type"])
                if i >= 0:
                    self.blend.setCurrentIndex(i)
                # never stomp a drag in progress with a poll update
                if not self.influence.dragging:
                    self.influence.set_value(row.get("influence", 1.0))
                self.btn_key.setChecked(row.get("animated_influence", False))
        finally:
            self._syncing = False


class LayerStackTool(QWidget):
    """The layer stack + management buttons (the heart of the tab)."""

    status_refreshed = Signal(dict)

    def __init__(self, bridge, window, parent=None):
        super().__init__(parent)
        self.bridge = bridge
        self.window = window
        self._status = None
        self._busy = False
        self._structure_key = None
        self._syncing_selection = False
        # OBJECT or SHAPEKEY — the whole page follows this, and every tool
        # reads it back out of the broadcast status (core echoes it)
        self.data_type = "OBJECT"
        # tab preferences (LayerOptionsTool owns the UI, config.json the value)
        self.sync_names = True
        self.default_blend = "COMBINE"
        self._sync_tried = set()

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)

        blurb = QLabel(
            "Animation layers over Blender's NLA. Click a layer to edit it — "
            "keys and auto-key land in that layer while the full result keeps "
            "playing. New Layer adopts the current action as the base layer "
            "the first time.")
        blurb.setWordWrap(True)
        blurb.setObjectName("dim")
        lay.addWidget(blurb)

        top = QHBoxLayout()
        self.obj_label = QLabel("—")
        self.obj_label.setObjectName("dim")
        top.addWidget(self.obj_label, 1)
        top.addWidget(self._dim_label("Layers for"))
        self.type_combo = QComboBox()
        self.type_combo.addItem("Object", "OBJECT")
        self.type_combo.addItem("Shape Keys", "SHAPEKEY")
        self.type_combo.setToolTip(
            "Which animation to layer: the object's own channels (bones, "
            "transforms, custom properties) or its shape-key values.\n"
            "Each has its own separate stack.")
        self.type_combo.currentIndexChanged.connect(self._on_data_type)
        top.addWidget(self.type_combo)
        lay.addLayout(top)

        # shown only when the object already has NLA tracks we didn't make:
        # until one of these is answered, no op touches those strips
        self.nla_banner = QFrame()
        self.nla_banner.setStyleSheet(
            "QFrame { border: 1px solid %s; border-radius: 3px; }"
            % theme.BORDER)
        bl = QHBoxLayout(self.nla_banner)
        bl.setContentsMargins(6, 4, 6, 4)
        warn = QLabel("This object already has NLA tracks.")
        warn.setWordWrap(True)
        warn.setStyleSheet("border: none;")
        bl.addWidget(warn, 1)
        self.btn_adopt = QPushButton("Use as Layers")
        self.btn_adopt.setObjectName("accent")
        self.btn_adopt.setToolTip(
            "Treat the existing tracks as the layer stack. Every strip keeps "
            "its exact range — nothing is restretched.")
        self.btn_adopt.clicked.connect(self.adopt_nla)
        bl.addWidget(self.btn_adopt)
        self.btn_clear_nla = QPushButton("Start Fresh…")
        self.btn_clear_nla.setObjectName("danger")
        self.btn_clear_nla.setToolTip(
            "Remove the existing NLA tracks and start a clean stack. The "
            "actions stay in the file.")
        self.btn_clear_nla.clicked.connect(self.clear_nla)
        bl.addWidget(self.btn_clear_nla)
        self.nla_banner.setVisible(False)
        lay.addWidget(self.nla_banner)

        self.list = QListWidget()
        self.list.setSelectionMode(QListWidget.SingleSelection)
        self.list.itemSelectionChanged.connect(self._on_row_selected)
        self.list.setMinimumHeight(160)
        lay.addWidget(self.list, 1)

        btns = QHBoxLayout()
        self.btn_new = QPushButton("New Layer")
        self.btn_new.setObjectName("accent")
        self.btn_new.clicked.connect(self.add_layer)
        btns.addWidget(self.btn_new)
        self.btn_dup = QPushButton("Duplicate")
        self.btn_dup.clicked.connect(self.duplicate_layer)
        btns.addWidget(self.btn_dup)
        self.chk_linked = QCheckBox("linked")
        self.chk_linked.setToolTip("Duplicate shares the action instead of "
                                   "copying it")
        btns.addWidget(self.chk_linked)
        self.btn_up = QPushButton("▲")
        self.btn_up.setFixedWidth(34)
        self.btn_up.setToolTip("Move layer up")
        self.btn_up.clicked.connect(lambda: self.move_layer("UP"))
        btns.addWidget(self.btn_up)
        self.btn_down = QPushButton("▼")
        self.btn_down.setFixedWidth(34)
        self.btn_down.setToolTip("Move layer down")
        self.btn_down.clicked.connect(lambda: self.move_layer("DOWN"))
        btns.addWidget(self.btn_down)
        self.btn_del = QPushButton("Delete")
        self.btn_del.setObjectName("danger")
        self.btn_del.clicked.connect(self.delete_layer)
        btns.addWidget(self.btn_del)
        btns.addStretch(1)
        lay.addLayout(btns)

        act_row = QHBoxLayout()
        act_lab = QLabel("Layer action")
        act_lab.setObjectName("dim")
        act_row.addWidget(act_lab)
        self.action_combo = QComboBox()
        self.action_combo.setToolTip(
            "Load an existing action into the selected layer")
        self.action_combo.setSizeAdjustPolicy(
            QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.action_combo.setMinimumContentsLength(12)
        act_row.addWidget(self.action_combo, 1)
        self.chk_autoblend = QCheckBox("Auto blend")
        self.chk_autoblend.setToolTip(
            "Pick Add or Replace automatically from the action's scale / "
            "rotation values, so a wrong blend type can't explode the rig")
        self.chk_autoblend.setChecked(True)
        act_row.addWidget(self.chk_autoblend)
        self.btn_load = QPushButton("Load")
        self.btn_load.clicked.connect(self.load_action)
        act_row.addWidget(self.btn_load)
        lay.addLayout(act_row)

        # Keying from here (Marty, 2026-08-05). Deliberately NOT a channel
        # picker: what gets keyed is Blender's own decision — the active keying
        # set, or the user's Default Key Channels — so this cannot drift from
        # what pressing I in the viewport does. The key lands in the selected
        # layer because selecting one puts its strip in NLA tweak mode.
        key_row = QHBoxLayout()
        self.btn_key = QPushButton("Set Keyframe")
        self.btn_key.setObjectName("accent")
        self.btn_key.clicked.connect(lambda: self.key_selection(False))
        key_row.addWidget(self.btn_key)
        self.btn_unkey = QPushButton("Remove Keyframe")
        self.btn_unkey.clicked.connect(lambda: self.key_selection(True))
        key_row.addWidget(self.btn_unkey)
        self.key_hint = self._dim_label("uses your own Blender key settings")
        key_row.addWidget(self.key_hint, 1)
        lay.addLayout(key_row)
        self._sync_key_buttons()

        self.status = QLabel("—")
        self.status.setWordWrap(True)
        self.status.setObjectName("dim")
        self.status.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(self.status)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self.poll)
        self._timer.start(POLL_MS)

    # ------------------------------------------------------------- helpers

    @staticmethod
    def _dim_label(text):
        lab = QLabel(text)
        lab.setObjectName("dim")
        return lab

    def _fail(self, exc):
        self.status.setStyleSheet("color: #e06c60;")
        self.status.setText(str(exc))

    def _ok(self, text):
        self.status.setStyleSheet("")
        self.status.setText(text)

    def apply_settings(self, settings):
        """Take the tab preferences (from config.json / the Options page)."""
        self.sync_names = bool(settings.get("sync_names", True))
        self.default_blend = settings.get("default_blend", "COMBINE")
        if "auto_blend" in settings:
            self.chk_autoblend.setChecked(bool(settings["auto_blend"]))
        self._sync_tried.clear()

    def _on_data_type(self, _i=None):
        """Switch stacks. The two are completely separate, so drop the cached
        structure and ask Blender about the new one straight away."""
        self.data_type = self.type_combo.currentData()
        self._structure_key = None
        self._syncing_selection = True
        try:
            self.list.clear()
        finally:
            self._syncing_selection = False
        self.refresh()

    def _guarded(self):
        """Whether it's safe to hit the bridge right now."""
        if self._busy:
            return False
        if self.window is not None and self.window.capturing:
            return False
        return True

    def _call(self, fn, *args, **kwargs):
        """Run a bridge command, apply the status it returns, report errors.
        Returns the status dict or None. Every command is aimed at the stack
        currently on show (object vs shape keys)."""
        if not self._guarded():
            return None
        if self.window is not None and not self.window.bridge_free_for_tools():
            return None
        kwargs.setdefault("data_type", self.data_type)
        self._busy = True
        try:
            status = fn(*args, **kwargs)
        except bridgemod.BridgeError as exc:
            self._fail(exc)
            return None
        finally:
            self._busy = False
        self.apply_status(status)
        return status

    def _selected_index(self):
        """Real (bottom-first) index of the selected row, or None."""
        items = self.list.selectedItems()
        if not items:
            return None
        return items[0].data(Qt.UserRole)

    def _require_selection(self):
        idx = self._selected_index()
        if idx is None:
            self._ok("Select a layer first")
        return idx

    # -------------------------------------------------------------- keying

    KEY_TIP = ("Set a keyframe at the current frame on what you are working on "
               "in Blender — the same channels Blender itself would use (the "
               "active keying set, or your Preferences ▸ Animation ▸ Default "
               "Key Channels).\nOn the Shape Keys stack it keys the active "
               "shape key's value.")
    UNKEY_TIP = ("Remove the keyframes at the current frame from the selected "
                 "objects and bones — Blender's own Alt+I.\nOn the Shape Keys "
                 "stack it removes the active shape key's key.")

    def _key_reason(self):
        """Why keying from here is unavailable on the installed add-on, or None.
        Fails OPEN: a bridge we cannot ask is unknown, not missing."""
        try:
            return self.bridge.feature_reason("anim_layers_keying")
        except Exception:               # noqa: BLE001 — a dead bridge is routine
            return None

    def _sync_key_buttons(self):
        reason = self._key_reason()
        self.btn_key.setEnabled(not reason)
        self.btn_unkey.setEnabled(not reason)
        self.btn_key.setToolTip(reason or self.KEY_TIP)
        self.btn_unkey.setToolTip(reason or self.UNKEY_TIP)
        self.key_hint.setText(
            "needs a newer add-on" if reason
            else "uses your own Blender key settings")

    @staticmethod
    def _key_message(info):
        """Report what BLENDER did, not what we asked for. The channels are its
        decision, so naming them is the only way the button is checkable."""
        frame = info.get("frame")
        where = " at frame %s" % frame if frame is not None else ""
        if info.get("shape_key"):
            target = "shape key '%s'" % info["shape_key"]
        else:
            bits = []
            for count, word in ((info.get("objects"), "object"),
                                (info.get("bones"), "bone")):
                if count:
                    bits.append("%d %s%s" % (count, word,
                                             "" if count == 1 else "s"))
            target = " / ".join(bits) or "the selection"
        if info.get("deleted"):
            # Alt+I clears every channel at the frame, not just the keying
            # set's — so naming channels here would be a lie.
            return "Removed keys from %s%s." % (target, where)
        if info.get("keying_set"):
            how = " using keying set '%s'" % info["keying_set"]
        elif info.get("channels"):
            how = " (%s)" % ", ".join(c.replace("_", " ").title()
                                      for c in info["channels"])
        else:
            how = ""
        return "Keyed %s%s%s." % (target, where, how)

    def key_selection(self, delete=False):
        """Blender's own I / Alt+I, from here. No layer has to be selected —
        the key lands wherever Blender is already keying."""
        reason = self._key_reason()
        if reason:
            self._fail(reason)
            return
        status = self._call(self.bridge.anim_layers_key_selection, delete=delete)
        if status is None:
            return
        self._ok(self._key_message(status.get("keyed") or {}))

    # ------------------------------------------------------------- polling

    def poll(self):
        """The timer path — only while the tab is actually on screen."""
        if not self.isVisible():
            return
        self.refresh(polling=True)

    def refresh(self, polling=False):
        """Ask Blender about the stack now. Anything that changes WHICH stack
        we're showing goes through here, not poll(): a deliberate switch has
        to take effect immediately, visible tab or not.

        `polling` marks the timer path so it fails instantly while the bridge
        is known to be down — this poll runs every 1.5 s and used to block the
        GUI thread for the socket timeout on every tick."""
        if not self._guarded():
            return
        try:
            status = self.bridge.anim_layers_status(data_type=self.data_type,
                                                    poll=polling)
        except bridgemod.BridgeError:
            self.obj_label.setText("Blender not connected")
            return
        self.apply_status(status)

    def apply_status(self, status):
        if not isinstance(status, dict):
            return
        self._status = status
        # capabilities are only known once something has answered, so the key
        # buttons settle on the first reply rather than at construction
        self._sync_key_buttons()
        self.status_refreshed.emit(status)
        # a mesh with no shape keys has no second stack to offer. Do this
        # BEFORE the error branch: "no shape keys" arrives AS an error.
        if "has_shapekeys" in status:
            has_shapes = bool(status["has_shapekeys"])
            item = self.type_combo.model().item(
                self.type_combo.findData("SHAPEKEY"))
            if item is not None:
                item.setEnabled(has_shapes)
            if self.data_type == "SHAPEKEY" and not has_shapes:
                # the user picked an object without shape keys — fall back
                # instead of sitting on an error row
                self.type_combo.setCurrentIndex(
                    self.type_combo.findData("OBJECT"))
                return
        if status.get("error"):
            self.obj_label.setText(status["error"])
            self.list.clear()
            self.nla_banner.setVisible(False)
            self._structure_key = None
            return
        self.nla_banner.setVisible(bool(status.get("foreign_nla")))
        self.obj_label.setText("%s  (%s)  —  %d %slayer%s" % (
            status["object"], status.get("mode", "?").replace("_", " ").lower(),
            len(status["layers"]),
            "shape-key " if self.data_type == "SHAPEKEY" else "",
            "" if len(status["layers"]) == 1 else "s"))

        rows = status["layers"]
        # rebuild the list only when the structure changed; otherwise update
        # the row widgets in place (no flicker, keeps drags/edits alive)
        key = json.dumps([(r["name"], r.get("locked_reason"),
                           "blend_type" in r) for r in rows])
        if key != self._structure_key:
            self._structure_key = key
            # clear()/addItem move the selection, which emits
            # itemSelectionChanged — that is OUR bookkeeping, not a click, and
            # echoing it back as anim_layers_select would select a stale layer
            # in Blender (and re-enter this method mid-rebuild)
            self._syncing_selection = True
            try:
                self.list.clear()
                for row in reversed(rows):   # top layer first, like every
                    item = QListWidgetItem() # layer UI
                    widget = LayerRow()
                    widget.solo_clicked.connect(self.toggle_solo)
                    widget.mute_toggled.connect(
                        lambda i, v: self.set_state(i, mute=v))
                    widget.lock_toggled.connect(
                        lambda i, v: self.set_state(i, lock=v))
                    widget.blend_changed.connect(
                        lambda i, b: self.set_state(i, blend_type=b))
                    widget.influence_changed.connect(self.set_influence)
                    widget.influence_preview.connect(self.preview_influence)
                    widget.influence_key_toggled.connect(self.set_animated)
                    widget.rename_requested.connect(self.rename_layer)
                    item.setSizeHint(widget.sizeHint())
                    item.setData(Qt.UserRole, row["index"])
                    self.list.addItem(item)
                    self.list.setItemWidget(item, widget)
            finally:
                self._syncing_selection = False
            self._sync_tried.clear()   # a changed stack deserves a fresh try
        solo = status.get("solo")
        for i in range(self.list.count()):
            item = self.list.item(i)
            widget = self.list.itemWidget(item)
            idx = item.data(Qt.UserRole)
            if widget is None or not 0 <= idx < len(rows):
                continue      # a refresh raced this one; the poll will settle
            widget.apply(rows[idx], solo)

        # mirror Blender's active layer into the list selection
        active = status.get("active_index")
        self._syncing_selection = True
        try:
            for i in range(self.list.count()):
                item = self.list.item(i)
                item.setSelected(item.data(Qt.UserRole) == active)
        finally:
            self._syncing_selection = False

        self._refresh_actions()
        self._maybe_sync_names(rows)

    def _refresh_actions(self):
        """Keep the action dropdown current (cheap: names only)."""
        try:
            actions = self.bridge.anim_layers_actions()
        except bridgemod.BridgeError:
            return
        names = [a["name"] for a in actions]
        current = [self.action_combo.itemText(i)
                   for i in range(self.action_combo.count())]
        if names != current:
            sel = self.action_combo.currentText()
            self.action_combo.clear()
            self.action_combo.addItems(names)
            i = self.action_combo.findText(sel)
            if i >= 0:
                self.action_combo.setCurrentIndex(i)

    # ------------------------------------------------------------- actions

    def _on_row_selected(self):
        if self._syncing_selection:
            return
        idx = self._selected_index()
        if idx is None:
            return
        status = self._call(self.bridge.anim_layers_select, idx)
        if status is not None:
            name = status.get("selected", "?")
            if status.get("in_tweak"):
                self._ok("Editing '%s' — keys land in this layer" % name)
            else:
                self._ok("Selected '%s' (locked — not editing)" % name)

    def _maybe_sync_names(self, rows):
        """With Sync Names on, a layer whose action was renamed elsewhere
        takes the new name back. Each mismatch is attempted ONCE — Blender
        uniquifies names, and a rename that doesn't converge must not be
        retried on every poll."""
        if not self.sync_names or not self._guarded():
            return
        pending = [r for r in rows
                   if r.get("action") and r.get("action") != r["name"]
                   and r.get("action_users", 1) <= 1 and not r.get("lock")
                   and (r["name"], r["action"]) not in self._sync_tried]
        if not pending:
            return
        for r in pending:
            self._sync_tried.add((r["name"], r["action"]))
        self._busy = True
        try:
            self.bridge.anim_layers_sync_names(data_type=self.data_type)
        except bridgemod.BridgeError:
            return
        finally:
            self._busy = False
        self.refresh()

    def add_layer(self):
        status = self._call(self.bridge.anim_layers_add,
                            blend_type=self.default_blend)
        if status is not None:
            msg = "Added '%s'" % status.get("added", "layer")
            if status.get("adopted_base"):
                msg += " — current action became base layer '%s'" \
                       % status["adopted_base"]
            self._ok(msg)

    def adopt_nla(self):
        status = self._call(self.bridge.anim_layers_adopt_nla)
        if status is None:
            return
        info = status.get("adopted", {})
        msg = "Using %d existing track%s as layers" % (
            len(info.get("layers", [])),
            "" if len(info.get("layers", [])) == 1 else "s")
        if info.get("locked"):
            msg += " — %d can't be layers and stay locked (%s)" % (
                len(info["locked"]),
                ", ".join(r["name"] for r in info["locked"]))
        self._ok(msg)

    def clear_nla(self):
        count = len(self._status.get("layers", [])) if self._status else 0
        if QMessageBox.question(
                self, "Start Fresh",
                "Remove all %d NLA track%s from this object?\nThe actions "
                "stay in the file, and the object's current action is "
                "untouched." % (count, "" if count == 1 else "s")) \
                != QMessageBox.StandardButton.Yes:
            return
        status = self._call(self.bridge.anim_layers_clear_nla, confirm=True)
        if status is not None:
            self._ok("Removed %d track%s — New Layer starts a fresh stack"
                     % (len(status.get("cleared", {}).get("layers", [])),
                        "" if count == 1 else "s"))

    def delete_layer(self):
        idx = self._require_selection()
        if idx is None:
            return
        row = self._status["layers"][idx] if self._status else {}
        name = row.get("name", "layer")
        if QMessageBox.question(
                self, "Delete Layer",
                "Delete layer '%s'?\nIts action stays in the file until "
                "the next save purges unused data." % name) \
                != QMessageBox.StandardButton.Yes:
            return
        status = self._call(self.bridge.anim_layers_delete, idx)
        if status is not None:
            self._ok("Deleted '%s'" % status.get("deleted", name))

    def duplicate_layer(self):
        idx = self._require_selection()
        if idx is None:
            return
        status = self._call(self.bridge.anim_layers_duplicate, idx,
                            self.chk_linked.isChecked())
        if status is not None:
            self._ok("Duplicated to '%s'%s" % (
                status.get("duplicated", "?"),
                " (linked action)" if self.chk_linked.isChecked() else ""))

    def move_layer(self, direction):
        idx = self._require_selection()
        if idx is None:
            return
        status = self._call(self.bridge.anim_layers_move, idx, direction)
        if status is not None:
            moved = status.get("moved", {})
            self._ok("Moved '%s' %s" % (moved.get("layer", "layer"),
                                        direction.lower()))

    def rename_layer(self, idx):
        if self._status is None or idx >= len(self._status["layers"]):
            return
        old = self._status["layers"][idx]["name"]
        name, ok = QInputDialog.getText(self, "Rename Layer",
                                        "Layer name:", text=old)
        if not ok or not name.strip() or name.strip() == old:
            return
        status = self._call(self.bridge.anim_layers_rename, idx, name.strip(),
                            sync_action=self.sync_names)
        if status is not None:
            r = status.get("renamed", {})
            self._ok("Renamed '%s' → '%s'" % (r.get("from", old),
                                              r.get("to", name)))

    def toggle_solo(self, idx):
        status = self._call(self.bridge.anim_layers_solo, idx)
        if status is not None:
            solo = status.get("solo")
            self._ok("Solo: %s" % solo if solo else "Solo off")

    def set_state(self, idx, **kwargs):
        self._call(self.bridge.anim_layers_set_state, idx, **kwargs)

    def set_influence(self, idx, value):
        # whether the write keys or stays static is the add-on's call now —
        # it tracks the per-layer animated-influence flag itself
        self._call(self.bridge.anim_layers_set_state, idx, influence=value)

    def preview_influence(self, idx, value):
        """Throttled live update while the influence slider is dragged —
        best-effort: skipped whenever the bridge is busy."""
        if not self._guarded():
            return
        if self.window is not None and not self.window.bridge_free_for_tools():
            return
        self._busy = True
        try:
            self.bridge.anim_layers_set_state(idx, influence=value,
                                              data_type=self.data_type)
        except bridgemod.BridgeError:
            pass
        finally:
            self._busy = False

    def set_animated(self, idx, animated):
        status = self._call(self.bridge.anim_layers_influence_animated, idx,
                            animated)
        if status is not None:
            self._ok("Influence %s for '%s'" % (
                "animated (keyed at current frame)" if animated else "static",
                status["layers"][idx]["name"] if idx < len(status["layers"])
                else "?"))

    def load_action(self):
        idx = self._require_selection()
        if idx is None:
            return
        action = self.action_combo.currentText()
        if not action:
            self._ok("No action selected")
            return
        status = self._call(self.bridge.anim_layers_set_action, idx, action,
                            self.chk_autoblend.isChecked())
        if status is not None:
            info = status.get("action_set", {})
            msg = "Loaded '%s' into '%s'" % (info.get("action", action),
                                             info.get("layer", "?"))
            if info.get("auto_blend"):
                msg += " — blend set to %s" % info["auto_blend"].title()
            self._ok(msg)


class MergeBakeTool(QWidget):
    """Merge / Bake: flatten layers into one (Merge) or bake their combined
    result into a new layer on top, over the timeline range. Down/Up work
    from the layer currently selected in the Layers tool; muted layers stay
    out of the bake (and survive a merge untouched)."""

    def __init__(self, bridge, window, parent=None):
        super().__init__(parent)
        self.bridge = bridge
        self.window = window
        self._busy = False
        self._data_type = "OBJECT"

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)

        blurb = QLabel(
            "Bake the layer stack over the timeline range. 'New layer' keeps "
            "the sources and adds the baked result above them; 'Merge' "
            "replaces them with it. Down and Up start from the layer "
            "selected in the Layers tool. Muted layers are left out — a "
            "merge keeps them as they are.")
        blurb.setWordWrap(True)
        blurb.setObjectName("dim")
        lay.addWidget(blurb)

        row1 = QHBoxLayout()
        row1.addWidget(self._dim_label("Bake type"))
        self.type_combo = QComboBox()
        self.type_combo.addItem("Anim Layers (channels)", "AL")
        self.type_combo.addItem("Blender NLA (visual)", "NLA")
        self.type_combo.setToolTip(
            "Anim Layers: exact channel bake — smart keys, upward bakes, "
            "custom properties; constraints stay live.\n"
            "Blender NLA: visual keying — constraint/sim motion is baked "
            "into the keys (and constraints can be cleared).")
        self.type_combo.currentIndexChanged.connect(self._update_enabled)
        row1.addWidget(self.type_combo, 1)
        row1.addWidget(self._dim_label("Operator"))
        self.op_combo = QComboBox()
        self.op_combo.addItem("Bake to new layer", "NEW")
        self.op_combo.addItem("Merge layers", "MERGE")
        self.op_combo.setToolTip("New layer keeps the sources; Merge deletes "
                                 "them and splices the result in")
        self.op_combo.currentIndexChanged.connect(self._update_enabled)
        row1.addWidget(self.op_combo, 1)
        lay.addLayout(row1)

        row2 = QHBoxLayout()
        row2.addWidget(self._dim_label("Direction"))
        self.dir_combo = QComboBox()
        self.dir_combo.addItem("All layers", "ALL")
        self.dir_combo.addItem("Down from selected", "DOWN")
        self.dir_combo.addItem("Up from selected (additive)", "UP")
        self.dir_combo.setToolTip(
            "All: the whole stack. Down: the selected layer and everything "
            "below it. Up: the selected ADD/SUBTRACT layer and the additive "
            "layers above it, stopping at the next Replace layer — the "
            "result stays an additive layer.")
        self.dir_combo.currentIndexChanged.connect(self._update_enabled)
        row2.addWidget(self.dir_combo, 1)
        row2.addWidget(self._dim_label("Steps"))
        self.steps = QSpinBox()
        self.steps.setRange(1, 100)
        self.steps.setValue(1)
        self.steps.setToolTip("Key every N frames (when Smart Bake is off)")
        self.steps.setMinimumWidth(52)
        row2.addWidget(self.steps)
        lay.addLayout(row2)

        self.chk_smart = QCheckBox("Smart bake — keep the original keyframes")
        self.chk_smart.setChecked(True)
        self.chk_smart.setToolTip(
            "Key only where the source layers have keys (mapped through "
            "each strip's repeat/speed/offset) and restore interpolation "
            "and handle types — instead of a key every frame")
        self.chk_smart.toggled.connect(self._update_enabled)
        lay.addWidget(self.chk_smart)

        self.chk_selected = QCheckBox("Only selected bones")
        self.chk_selected.setToolTip(
            "Bake only channels of the bones selected in Blender")
        lay.addWidget(self.chk_selected)

        self.chk_modifiers = QCheckBox("Merge f-curve modifiers")
        self.chk_modifiers.setChecked(True)
        self.chk_modifiers.setToolTip(
            "On: Noise/Cycles etc. get baked into the keys and dropped.\n"
            "Off: keys stay clean and the modifiers are copied onto the "
            "result, still live.")
        lay.addWidget(self.chk_modifiers)

        self.chk_constraints = QCheckBox("Clear constraints after bake")
        self.chk_constraints.setToolTip(
            "Blender NLA bake only: remove pose-bone constraints once their "
            "motion is baked into keys")
        lay.addWidget(self.chk_constraints)

        self.chk_backup = QCheckBox("Copy original actions before merging")
        self.chk_backup.setChecked(True)
        self.chk_backup.setToolTip(
            "Merge only: keep a fake-user backup copy of every merged "
            "layer's action (name + '.orig'), so a merge is recoverable")
        lay.addWidget(self.chk_backup)

        act_row = QHBoxLayout()
        self.btn_bake = QPushButton("Bake")
        self.btn_bake.setObjectName("accent")
        self.btn_bake.clicked.connect(self.bake)
        act_row.addWidget(self.btn_bake)
        act_row.addStretch(1)
        lay.addLayout(act_row)

        self.status = QLabel("—")
        self.status.setWordWrap(True)
        self.status.setObjectName("dim")
        self.status.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(self.status)
        lay.addStretch(1)

        self._update_enabled()

    # ------------------------------------------------------------------

    @staticmethod
    def _dim_label(text):
        lab = QLabel(text)
        lab.setObjectName("dim")
        return lab

    def _update_enabled(self):
        al = self.type_combo.currentData() == "AL"
        merge = self.op_combo.currentData() == "MERGE"
        self.chk_smart.setEnabled(al)
        self.chk_modifiers.setEnabled(al)
        self.chk_constraints.setEnabled(not al)
        self.chk_backup.setEnabled(merge)
        self.steps.setEnabled(not (al and self.chk_smart.isChecked()))
        self.btn_bake.setText("Merge" if merge else "Bake")

    def set_data_type(self, data_type):
        """Shape-key stacks: Blender's own NLA bake can't key shape-key
        channels, so that engine isn't offered at all."""
        if data_type == self._data_type:
            return
        self._data_type = data_type
        shapes = data_type == "SHAPEKEY"
        if shapes:
            self.type_combo.setCurrentIndex(self.type_combo.findData("AL"))
        self.type_combo.setEnabled(not shapes)
        self.type_combo.setToolTip(
            "Blender's NLA bake can't key shape-key channels, so shape-key "
            "layers always use the Anim Layers engine" if shapes
            else "Anim Layers: exact channel bake — smart keys, upward "
                 "bakes, custom properties; constraints stay live.\n"
                 "Blender NLA: visual keying — constraint/sim motion is "
                 "baked into the keys (and constraints can be cleared).")
        self.chk_selected.setText("Only the active shape key" if shapes
                                  else "Only selected bones")
        self._update_enabled()

    def on_layers_changed(self, status):
        if isinstance(status, dict) and status.get("data_type"):
            self.set_data_type(status["data_type"])

    def _fail(self, exc):
        self.status.setStyleSheet("color: #e06c60;")
        self.status.setText(str(exc))

    def _ok(self, text):
        self.status.setStyleSheet("")
        self.status.setText(text)

    def _guarded(self):
        if self._busy:
            return False
        if self.window is not None and self.window.capturing:
            return False
        if self.window is not None and not self.window.bridge_free_for_tools():
            return False
        return True

    # ------------------------------------------------------------------

    def bake(self):
        if not self._guarded():
            return
        bake_type = self.type_combo.currentData()
        mode = self.op_combo.currentData()
        al = bake_type == "AL"
        if mode == "MERGE":
            if QMessageBox.question(
                    self, "Merge Layers",
                    "Merge the baked layers into one? The source layers are "
                    "deleted%s." % (" (their actions are kept as .orig "
                                    "backups)" if self.chk_backup.isChecked()
                                    else "")) \
                    != QMessageBox.StandardButton.Yes:
                return
        self._busy = True
        try:
            status = self.bridge.anim_layers_bake(
                mode=mode,
                direction=self.dir_combo.currentData(),
                bake_type=bake_type,
                smart=al and self.chk_smart.isChecked(),
                steps=self.steps.value(),
                selected_only=self.chk_selected.isChecked(),
                merge_modifiers=(not al) or self.chk_modifiers.isChecked(),
                clear_constraints=(not al)
                and self.chk_constraints.isChecked(),
                copy_original=mode == "MERGE"
                and self.chk_backup.isChecked(),
                data_type=self._data_type)
        except bridgemod.BridgeError as exc:
            self._fail(exc)
            return
        finally:
            self._busy = False
        baked = (status or {}).get("baked", {})
        if not baked:
            self._fail("Bake returned no result")
            return
        what = "Merged" if mode == "MERGE" else "Baked"
        msg = "%s %d layer%s → '%s'  (%d channels, %d keys, frames %g–%g)" % (
            what, len(baked.get("merged", [])),
            "" if len(baked.get("merged", [])) == 1 else "s",
            baked.get("result", "?"), baked.get("channels", 0),
            baked.get("keys", 0), baked.get("frame_start", 0),
            baked.get("frame_end", 0))
        if baked.get("result_blend") == "ADD":
            msg += " — result stays additive"
        if baked.get("backups"):
            msg += ".  Backups: %s" % ", ".join(baked["backups"])
        self._ok(msg)


class LayerToolsTool(QWidget):
    """Per-layer working tools: select the layer's bones, reset it to rest,
    make curves cyclic, and pull parts of a layer out into new layers.
    Everything acts on the layer selected in the Layers tool and is scoped by
    the filter row + 'only selected bones'."""

    def __init__(self, bridge, window, parent=None):
        super().__init__(parent)
        self.bridge = bridge
        self.window = window
        self._busy = False
        self._layers = []
        self._data_type = "OBJECT"

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)

        blurb = QLabel(
            "These act on the layer selected above. The filter scopes every "
            "one of them — untick channels or axes to leave them alone.")
        blurb.setWordWrap(True)
        blurb.setObjectName("dim")
        lay.addWidget(blurb)

        # ---------------------------------------------------------- filter
        filt = QHBoxLayout()
        filt.addWidget(self._dim_label("Filter"))
        self.chan_boxes = {}
        for key, label in (("LOCATION", "Loc"), ("ROTATION", "Rot"),
                           ("SCALE", "Scale")):
            box = QCheckBox(label)
            box.setChecked(True)
            box.setToolTip("Include %s channels" % label.lower())
            filt.addWidget(box)
            self.chan_boxes[key] = box
        filt.addSpacing(10)
        self.axis_boxes = {}
        for axis in ("W", "X", "Y", "Z"):
            box = QCheckBox(axis)
            box.setChecked(True)
            box.setToolTip("Include the %s component (W is the quaternion "
                           "scalar)" % axis)
            filt.addWidget(box)
            self.axis_boxes[axis] = box
        filt.addStretch(1)
        lay.addLayout(filt)

        self.chk_selected = QCheckBox("Only selected bones")
        self.chk_selected.setChecked(True)
        self.chk_selected.setToolTip(
            "On: act only on the bones selected in Blender.\n"
            "Off: act on every bone the layer already animates.")
        lay.addWidget(self.chk_selected)

        # ---------------------------------------------------------- layer ops
        lay.addWidget(self._heading("This layer"))
        row = QHBoxLayout()
        self.btn_select_bones = QPushButton("Select Bones")
        self.btn_select_bones.setToolTip(
            "Select the bones this layer animates (in Blender)")
        self.btn_select_bones.clicked.connect(self.select_bones)
        row.addWidget(self.btn_select_bones)
        self.btn_reset = QPushButton("Reset Layer")
        self.btn_reset.setToolTip(
            "Key rest values into THIS layer at the current frame, so it "
            "stops affecting the scoped bones. Other layers are untouched.")
        self.btn_reset.clicked.connect(self.reset_layer)
        row.addWidget(self.btn_reset)
        self.btn_cyclic = QPushButton("Make Cyclic")
        self.btn_cyclic.setToolTip(
            "Add a Cycles modifier so the layer's keyed motion repeats "
            "forever (added below any existing modifiers)")
        self.btn_cyclic.clicked.connect(lambda: self.set_cyclic(True))
        row.addWidget(self.btn_cyclic)
        self.btn_uncyclic = QPushButton("Remove Cyclic")
        self.btn_uncyclic.clicked.connect(lambda: self.set_cyclic(False))
        row.addWidget(self.btn_uncyclic)
        row.addStretch(1)
        lay.addLayout(row)

        # ---------------------------------------------------------- extract
        lay.addWidget(self._heading("Extract to a new layer"))
        ex = QHBoxLayout()
        self.btn_extract = QPushButton("Extract Bones")
        self.btn_extract.setToolTip(
            "Move the scoped curves out of this layer into a new layer "
            "above it — the animation looks exactly the same")
        self.btn_extract.clicked.connect(self.extract_bones)
        ex.addWidget(self.btn_extract)
        self.btn_markers = QPushButton("Extract at Markers")
        self.btn_markers.setToolTip(
            "Mocap cleanup: sample this layer only at the timeline markers "
            "into a new layer of clean, editable keys")
        self.btn_markers.clicked.connect(self.extract_markers)
        ex.addWidget(self.btn_markers)
        self.chk_mute_source = QCheckBox("mute source")
        self.chk_mute_source.setChecked(True)
        self.chk_mute_source.setToolTip(
            "Mute the layer the marker keys came from (it is never deleted)")
        ex.addWidget(self.chk_mute_source)
        ex.addStretch(1)
        lay.addLayout(ex)

        # ---------------------------------------------------------- share
        lay.addWidget(self._heading("Share key timing"))
        sh = QHBoxLayout()
        sh.addWidget(self._dim_label("Take timing from"))
        self.source_combo = QComboBox()
        self.source_combo.setToolTip(
            "The layer whose key positions get copied into this one")
        self.source_combo.setSizeAdjustPolicy(
            QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.source_combo.setMinimumContentsLength(10)
        sh.addWidget(self.source_combo, 1)
        self.btn_share = QPushButton("Share Keys")
        self.btn_share.setToolTip(
            "Add keys to this layer at the other layer's key frames, "
            "holding its own values — the animation does not change")
        self.btn_share.clicked.connect(self.share_keys)
        sh.addWidget(self.btn_share)
        lay.addLayout(sh)

        # ---------------------------------------------------------- multikey
        lay.addWidget(self._heading("Multikey"))
        mk = QHBoxLayout()
        self.mk_op = QComboBox()
        self.mk_op.addItem("Offset by", "OFFSET")
        self.mk_op.addItem("Set to", "REPLACE")
        self.mk_op.addItem("Scale by", "SCALE")
        self.mk_op.addItem("Randomize ±", "RANDOMIZE")
        self.mk_op.setToolTip(
            "What to do to the keys: shift them, set them all to one value, "
            "scale them around a pivot, or jitter them")
        self.mk_op.currentIndexChanged.connect(self._on_multikey_op)
        mk.addWidget(self.mk_op)
        self.mk_value = QDoubleSpinBox()
        self.mk_value.setRange(-10000.0, 10000.0)
        self.mk_value.setDecimals(3)
        self.mk_value.setSingleStep(0.1)
        self.mk_value.setMinimumWidth(80)
        mk.addWidget(self.mk_value)
        self.mk_pivot = QComboBox()
        self.mk_pivot.addItem("around average", "AVERAGE")
        self.mk_pivot.addItem("around zero", "ZERO")
        self.mk_pivot.setToolTip(
            "Average: each curve scales around the mean of its own keys.\n"
            "Zero: raw values are multiplied — on an additive layer zero is "
            "the neutral value, so this scales the layer's effect.")
        mk.addWidget(self.mk_pivot)
        self.mk_seed_label = self._dim_label("seed")
        mk.addWidget(self.mk_seed_label)
        self.mk_seed = QSpinBox()
        self.mk_seed.setRange(0, 99999)
        self.mk_seed.setMinimumWidth(58)
        self.mk_seed.setToolTip("Same seed = same jitter every time")
        mk.addWidget(self.mk_seed)
        self.btn_multikey = QPushButton("Apply")
        self.btn_multikey.setObjectName("accent")
        self.btn_multikey.clicked.connect(self.apply_multikey)
        mk.addWidget(self.btn_multikey)
        mk.addStretch(1)
        lay.addLayout(mk)

        self.chk_selected_keys = QCheckBox("Only selected keyframes")
        self.chk_selected_keys.setChecked(True)
        self.chk_selected_keys.setToolTip(
            "On: only the keys selected in Blender's Dope Sheet / Graph "
            "Editor change.\nOff: every key of the scoped curves changes.")
        lay.addWidget(self.chk_selected_keys)

        self.status = QLabel("—")
        self.status.setWordWrap(True)
        self.status.setObjectName("dim")
        self.status.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(self.status)
        # no trailing stretch: this sits UNDER the layer stack on the Layers
        # page, and the stack is what should absorb spare height

        self._on_multikey_op()

    # ------------------------------------------------------------------

    @staticmethod
    def _dim_label(text):
        lab = QLabel(text)
        lab.setObjectName("dim")
        return lab

    @staticmethod
    def _heading(text):
        lab = QLabel(text)
        lab.setStyleSheet("font-weight: bold; margin-top: 4px;")
        return lab

    def _fail(self, exc):
        self.status.setStyleSheet("color: #e06c60;")
        self.status.setText(str(exc))

    def _ok(self, text):
        self.status.setStyleSheet("")
        self.status.setText(text)

    def _guarded(self):
        if self._busy:
            return False
        if self.window is not None and self.window.capturing:
            return False
        if self.window is not None and not self.window.bridge_free_for_tools():
            return False
        return True

    def _channels(self):
        """None when everything is ticked — the add-on treats None as 'no
        filter at all', which also lets custom properties through."""
        picked = [k for k, box in self.chan_boxes.items() if box.isChecked()]
        return None if len(picked) == len(self.chan_boxes) else picked

    def _axes(self):
        picked = [k for k, box in self.axis_boxes.items() if box.isChecked()]
        return None if len(picked) == len(self.axis_boxes) else picked

    def _scope(self):
        return {"selected_only": self.chk_selected.isChecked(),
                "channels": self._channels(), "axes": self._axes()}

    def _call(self, fn, *args, **kwargs):
        if not self._guarded():
            return None
        kwargs.setdefault("data_type", self._data_type)
        self._busy = True
        try:
            return fn(*args, **kwargs)
        except bridgemod.BridgeError as exc:
            self._fail(exc)
            return None
        finally:
            self._busy = False

    def set_data_type(self, data_type):
        """Object vs shape-key stack. Shape keys have no bone selection and
        no Loc/Rot/Scale channels, so the scoping controls change meaning —
        say so rather than letting them silently scope everything out."""
        if data_type == self._data_type:
            return
        self._data_type = data_type
        shapes = data_type == "SHAPEKEY"
        self.chk_selected.setText("Only the active shape key" if shapes
                                  else "Only selected bones")
        self.chk_selected.setToolTip(
            "On: act only on the shape key that is active in the mesh's "
            "Shape Keys list.\nOff: act on every shape key the layer "
            "animates." if shapes else
            "On: act only on the bones selected in Blender.\n"
            "Off: act on every bone the layer already animates.")
        self.btn_select_bones.setEnabled(not shapes)
        for box in list(self.chan_boxes.values()) + list(
                self.axis_boxes.values()):
            box.setEnabled(not shapes)
            if shapes:
                box.setToolTip("Shape-key values aren't location, rotation "
                               "or scale — this filter doesn't apply to them")

    def on_layers_changed(self, status):
        """Fed by the Layers tool's poll so the share-timing dropdown always
        matches the real stack (and never offers the active layer itself)."""
        if not isinstance(status, dict):
            return
        if status.get("data_type"):
            self.set_data_type(status["data_type"])
        if status.get("error"):
            return
        rows = status.get("layers", [])
        active = status.get("active_index")
        entries = [(r["index"], r["name"]) for r in rows
                   if r["index"] != active]
        if entries == self._layers:
            return
        self._layers = entries
        keep = self.source_combo.currentData()
        self.source_combo.clear()
        for idx, name in reversed(entries):   # top layer first, like the list
            self.source_combo.addItem(name, idx)
        if keep is not None:
            i = self.source_combo.findData(keep)
            if i >= 0:
                self.source_combo.setCurrentIndex(i)

    # ------------------------------------------------------------- actions

    def select_bones(self):
        scope = self._scope()
        status = self._call(self.bridge.anim_layers_select_bones,
                            channels=scope["channels"], axes=scope["axes"])
        if status is None:
            return
        info = status.get("selected_bones", {})
        bones = info.get("bones", [])
        if bones:
            self._ok("Selected %d bone%s from '%s'" % (
                len(bones), "" if len(bones) == 1 else "s",
                info.get("layer", "?")))
        else:
            self._ok("'%s' animates no bones matching the filter"
                     % info.get("layer", "?"))

    def reset_layer(self):
        status = self._call(self.bridge.anim_layers_reset, **self._scope())
        if status is None:
            return
        info = status.get("reset", {})
        self._ok("Reset '%s' at frame %s — %d channel%s keyed to rest" % (
            info.get("layer", "?"), info.get("frame", "?"),
            info.get("channels", 0),
            "" if info.get("channels") == 1 else "s"))

    def set_cyclic(self, enable):
        status = self._call(self.bridge.anim_layers_cyclic, enable=enable,
                            **self._scope())
        if status is None:
            return
        info = status.get("cyclic", {})
        n = info.get("curves", 0)
        if not n:
            self._ok("Nothing to change on '%s' — %s"
                     % (info.get("layer", "?"),
                        "curves need at least 2 keys to cycle" if enable
                        else "no cyclic curves in scope"))
        else:
            self._ok("%s cyclic on %d curve%s in '%s'" % (
                "Made" if enable else "Removed", n, "" if n == 1 else "s",
                info.get("layer", "?")))

    def extract_bones(self):
        status = self._call(self.bridge.anim_layers_extract_bones,
                            **self._scope())
        if status is None:
            return
        info = status.get("extracted", {})
        self._ok("Moved %d curve%s from '%s' into new layer '%s'" % (
            info.get("curves", 0), "" if info.get("curves") == 1 else "s",
            info.get("from", "?"), info.get("layer", "?")))

    def extract_markers(self):
        status = self._call(self.bridge.anim_layers_extract_markers,
                            mute_source=self.chk_mute_source.isChecked(),
                            **self._scope())
        if status is None:
            return
        info = status.get("markers", {})
        msg = "Extracted %d marker%s from '%s' → '%s' (%d keys)" % (
            info.get("markers", 0), "" if info.get("markers") == 1 else "s",
            info.get("from", "?"), info.get("layer", "?"),
            info.get("keys", 0))
        if info.get("source_muted"):
            msg += " — source muted"
        self._ok(msg)

    def _on_multikey_op(self, _i=None):
        """Only show the controls the chosen op actually uses — and start
        each one from its own neutral value (1.0 scales nothing, 0.0 shifts
        nothing)."""
        op = self.mk_op.currentData()
        self.mk_pivot.setVisible(op == "SCALE")
        for w in (self.mk_seed, self.mk_seed_label):
            w.setVisible(op == "RANDOMIZE")
        self.mk_value.setValue(1.0 if op == "SCALE" else 0.0)
        self.mk_value.setToolTip({
            "OFFSET": "Added to every key in scope",
            "REPLACE": "Every key in scope is set to this value",
            "SCALE": "Multiplier — 1.0 changes nothing, 0.0 flattens",
            "RANDOMIZE": "Each key moves by a random amount up to ± this",
        }[op])

    def apply_multikey(self):
        op = self.mk_op.currentData()
        status = self._call(self.bridge.anim_layers_multikey, op,
                            self.mk_value.value(),
                            selected_keys=self.chk_selected_keys.isChecked(),
                            pivot=self.mk_pivot.currentData(),
                            seed=self.mk_seed.value(), **self._scope())
        if status is None:
            return
        info = status.get("multikey", {})
        self._ok("%s: %d key%s across %d curve%s in '%s'" % (
            self.mk_op.currentText(), info.get("keys", 0),
            "" if info.get("keys") == 1 else "s", info.get("curves", 0),
            "" if info.get("curves") == 1 else "s", info.get("layer", "?")))

    def share_keys(self):
        source = self.source_combo.currentData()
        if source is None:
            self._ok("Pick a layer to take the key timing from")
            return
        status = self._call(self.bridge.anim_layers_share_keys, source,
                            **self._scope())
        if status is None:
            return
        info = status.get("shared", {})
        self._ok("Added %d key%s to '%s' at '%s' key times" % (
            info.get("keys", 0), "" if info.get("keys") == 1 else "s",
            info.get("layer", "?"), info.get("from", "?")))


class LayerSettingsTool(QWidget):
    """Per-layer settings that aren't filter-scoped: the layer's own frame
    range (how its action plays on the timeline) and what its influence
    keyframes do in the graph editor. Both follow the layer selected in the
    Layers list and are fed by that tool's poll."""

    EXTRAP = (("Hold", "HOLD"), ("Hold Forward", "HOLD_FORWARD"),
              ("Nothing", "NOTHING"))

    def __init__(self, bridge, window, parent=None):
        super().__init__(parent)
        self.bridge = bridge
        self.window = window
        self._busy = False
        self._row = None            # last status row for the active layer
        self._active = None
        self._data_type = "OBJECT"

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)

        # ------------------------------------------------------ frame range
        lay.addWidget(self._heading("Frame range"))
        self.chk_custom = QCheckBox("Custom frame range for this layer")
        self.chk_custom.setToolTip(
            "Off: the layer spans the scene range and is kept there.\n"
            "On: you own the span — start/end, speed and repeat below.")
        self.chk_custom.clicked.connect(self._on_custom_toggled)
        lay.addWidget(self.chk_custom)

        r1 = QHBoxLayout()
        r1.addWidget(self._dim_label("Start"))
        self.spin_start = QDoubleSpinBox()
        self.spin_start.setRange(-100000.0, 100000.0)
        self.spin_start.setDecimals(1)
        self.spin_start.setMinimumWidth(70)
        self.spin_start.setToolTip("Moves the whole strip — the length and "
                                   "the timing inside it stay as they are")
        r1.addWidget(self.spin_start)
        r1.addWidget(self._dim_label("End"))
        self.spin_end = QDoubleSpinBox()
        self.spin_end.setRange(-100000.0, 100000.0)
        self.spin_end.setDecimals(1)
        self.spin_end.setMinimumWidth(70)
        self.spin_end.setToolTip(
            "The strip's last frame. Blender ties end, speed and repeat "
            "together, so setting the end re-derives the repeat count on a "
            "repeating strip (exactly like dragging the strip edge).")
        r1.addWidget(self.spin_end)
        r1.addStretch(1)
        lay.addLayout(r1)

        r2 = QHBoxLayout()
        r2.addWidget(self._dim_label("Speed"))
        self.spin_speed = QDoubleSpinBox()
        self.spin_speed.setRange(0.001, 1000.0)
        self.spin_speed.setDecimals(3)
        self.spin_speed.setSingleStep(0.1)
        self.spin_speed.setMinimumWidth(70)
        self.spin_speed.setToolTip("Playback scale — 2.0 makes the action "
                                   "take twice as long")
        r2.addWidget(self.spin_speed)
        r2.addWidget(self._dim_label("Repeat"))
        self.spin_repeat = QDoubleSpinBox()
        self.spin_repeat.setRange(0.001, 1000.0)
        self.spin_repeat.setDecimals(3)
        self.spin_repeat.setSingleStep(0.5)
        self.spin_repeat.setMinimumWidth(70)
        self.spin_repeat.setToolTip("How many times the action plays inside "
                                    "the strip")
        r2.addWidget(self.spin_repeat)
        r2.addStretch(1)
        lay.addLayout(r2)

        r3 = QHBoxLayout()
        r3.addWidget(self._dim_label("Extrapolation"))
        self.extrap = QComboBox()
        for label, data in self.EXTRAP:
            self.extrap.addItem(label, data)
        self.extrap.setToolTip(
            "What the layer does outside its own range — hold the last pose, "
            "hold it forward only, or contribute nothing")
        r3.addWidget(self.extrap)
        self.chk_reverse = QCheckBox("Reversed")
        self.chk_reverse.setToolTip("Play the action backwards")
        r3.addWidget(self.chk_reverse)
        r3.addStretch(1)
        lay.addLayout(r3)

        r4 = QHBoxLayout()
        self.btn_apply_range = QPushButton("Apply Range")
        self.btn_apply_range.setObjectName("accent")
        self.btn_apply_range.clicked.connect(self.apply_range)
        r4.addWidget(self.btn_apply_range)
        self.btn_sync = QPushButton("Sync to Action")
        self.btn_sync.setToolTip(
            "Fit the strip to its action: length x speed x repeat, starting "
            "where it starts now")
        self.btn_sync.clicked.connect(self.sync_to_action)
        r4.addWidget(self.btn_sync)
        self.chk_always_sync = QCheckBox("always")
        self.chk_always_sync.setToolTip(
            "Re-fit the strip every time the layer is touched, so it follows "
            "the action as you key past its end")
        self.chk_always_sync.clicked.connect(self._on_always_sync)
        r4.addWidget(self.chk_always_sync)
        r4.addStretch(1)
        lay.addLayout(r4)

        # -------------------------------------------------- influence keys
        lay.addWidget(self._heading("Influence keys"))
        blurb = QLabel(
            "How this layer's influence curve behaves in the graph editor. "
            "Only layers whose influence has been set have one.")
        blurb.setWordWrap(True)
        blurb.setObjectName("dim")
        lay.addWidget(blurb)

        i1 = QHBoxLayout()
        i1.addWidget(self._dim_label("Apply to"))
        self.infl_scope = QComboBox()
        self.infl_scope.addItem("This layer", "LOCAL")
        self.infl_scope.addItem("All layers", "GLOBAL")
        self.infl_scope.setToolTip("Local: the selected layer. "
                                   "Global: every layer in the stack.")
        i1.addWidget(self.infl_scope)
        self.infl_boxes = {}
        for key, label, tip in (
                ("select", "Select", "Select the influence keyframes"),
                ("hide", "Hide", "Hide the influence channel"),
                ("mute", "Mute", "Mute it — influence stops being animated"),
                ("lock", "Lock", "Lock it against edits")):
            box = QCheckBox(label)
            box.setToolTip(tip)
            box.clicked.connect(
                lambda checked, k=key: self._on_influence_flag(k, checked))
            i1.addWidget(box)
            self.infl_boxes[key] = box
        i1.addStretch(1)
        lay.addLayout(i1)

        self.status = QLabel("—")
        self.status.setWordWrap(True)
        self.status.setObjectName("dim")
        self.status.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(self.status)

        self._set_enabled_state()

    # ------------------------------------------------------------------

    @staticmethod
    def _dim_label(text):
        lab = QLabel(text)
        lab.setObjectName("dim")
        return lab

    @staticmethod
    def _heading(text):
        lab = QLabel(text)
        lab.setStyleSheet("font-weight: bold; margin-top: 4px;")
        return lab

    def _fail(self, exc):
        self.status.setStyleSheet("color: #e06c60;")
        self.status.setText(str(exc))

    def _ok(self, text):
        self.status.setStyleSheet("")
        self.status.setText(text)

    def _guarded(self):
        if self._busy:
            return False
        if self.window is not None and self.window.capturing:
            return False
        if self.window is not None and not self.window.bridge_free_for_tools():
            return False
        return True

    def _call(self, fn, *args, **kwargs):
        if not self._guarded():
            return None
        kwargs.setdefault("data_type", self._data_type)
        self._busy = True
        try:
            return fn(*args, **kwargs)
        except bridgemod.BridgeError as exc:
            self._fail(exc)
            return None
        finally:
            self._busy = False

    # ------------------------------------------------------------- status

    def on_layers_changed(self, status):
        """Mirror the active layer's settings. Fields the user is typing in
        are left alone — the poll must never fight an edit in progress."""
        if not isinstance(status, dict):
            return
        if status.get("data_type"):
            self._data_type = status["data_type"]
        if status.get("error"):
            self._row, self._active = None, None
            self._set_enabled_state()
            return
        rows = status.get("layers", [])
        active = status.get("active_index")
        row = None
        if active is not None and 0 <= active < len(rows):
            row = rows[active]
        if row is None or "blend_type" not in row:
            self._row, self._active = None, None
            self._set_enabled_state()
            return
        self._row, self._active = row, active

        def put(widget, value):
            if widget.hasFocus():
                return
            widget.blockSignals(True)
            try:
                widget.setValue(value)
            finally:
                widget.blockSignals(False)

        put(self.spin_start, row.get("frame_start", 0.0))
        put(self.spin_end, row.get("frame_end", 0.0))
        put(self.spin_speed, max(0.001, row.get("scale", 1.0)))
        put(self.spin_repeat, max(0.001, row.get("repeat", 1.0)))
        for widget, checked in ((self.chk_custom, row.get("custom_range")),
                                (self.chk_reverse, row.get("reversed")),
                                (self.chk_always_sync,
                                 row.get("always_sync"))):
            widget.blockSignals(True)
            widget.setChecked(bool(checked))
            widget.blockSignals(False)
        i = self.extrap.findData(row.get("extrapolation"))
        if i >= 0 and not self.extrap.hasFocus():
            self.extrap.blockSignals(True)
            self.extrap.setCurrentIndex(i)
            self.extrap.blockSignals(False)
        for key, box in self.infl_boxes.items():
            box.blockSignals(True)
            box.setChecked(bool(row.get("influence_%s"
                                        % ("selected" if key == "select"
                                           else key))))
            box.blockSignals(False)
        self._set_enabled_state()

    def _set_enabled_state(self):
        have = self._row is not None
        locked = bool(self._row and self._row.get("lock"))
        usable = have and not locked
        for w in (self.chk_custom, self.btn_sync, self.chk_always_sync,
                  self.btn_apply_range, self.spin_start, self.spin_end,
                  self.spin_speed, self.spin_repeat, self.extrap,
                  self.chk_reverse, self.infl_scope):
            w.setEnabled(usable)
        for box in self.infl_boxes.values():
            box.setEnabled(usable)
        if not have:
            self._ok("Select a layer to see its settings")
        elif locked:
            self._ok("'%s' is locked" % self._row.get("name", "?"))

    # ------------------------------------------------------------ actions

    def _on_custom_toggled(self, checked):
        status = self._call(self.bridge.anim_layers_frame_range,
                            custom=bool(checked))
        if status is None:
            return
        info = status.get("frame_range", {})
        self._ok("Custom range %s for '%s' (%g–%g)" % (
            "on" if checked else "off — back on the scene range",
            info.get("layer", "?"), info.get("frame_start", 0),
            info.get("frame_end", 0)))

    def apply_range(self):
        status = self._call(self.bridge.anim_layers_frame_range,
                            custom=True,
                            frame_start=self.spin_start.value(),
                            frame_end=self.spin_end.value(),
                            extrapolation=self.extrap.currentData(),
                            reverse=self.chk_reverse.isChecked(),
                            repeat=self.spin_repeat.value(),
                            scale=self.spin_speed.value())
        if status is None:
            return
        info = status.get("frame_range", {})
        self._ok("'%s': frames %g–%g, speed %g, repeat %g%s" % (
            info.get("layer", "?"), info.get("frame_start", 0),
            info.get("frame_end", 0), info.get("scale", 1),
            info.get("repeat", 1),
            " (reversed)" if info.get("reversed") else ""))

    def sync_to_action(self):
        status = self._call(self.bridge.anim_layers_frame_range, sync=True)
        if status is None:
            return
        info = status.get("frame_range", {})
        self._ok("Synced '%s' to its action — frames %g–%g" % (
            info.get("layer", "?"), info.get("frame_start", 0),
            info.get("frame_end", 0)))

    def _on_always_sync(self, checked):
        status = self._call(self.bridge.anim_layers_frame_range,
                            always_sync=bool(checked))
        if status is None:
            return
        self._ok("Always-sync %s for '%s'" % (
            "on" if checked else "off",
            status.get("frame_range", {}).get("layer", "?")))

    def _on_influence_flag(self, key, checked):
        status = self._call(self.bridge.anim_layers_influence_keys,
                            scope=self.infl_scope.currentData(),
                            **{key: bool(checked)})
        if status is None:
            # the write failed — put the box back where the scene says it is
            if self._row is not None:
                box = self.infl_boxes[key]
                box.blockSignals(True)
                box.setChecked(bool(self._row.get(
                    "influence_%s" % ("selected" if key == "select"
                                      else key))))
                box.blockSignals(False)
            return
        info = status.get("influence_settings", {})
        msg = "Influence keys %s: %s on %s" % (
            "set" if checked else "cleared", key,
            ", ".join(info.get("layers", [])) or "nothing")
        if info.get("skipped"):
            msg += "  (no influence curve: %s)" % ", ".join(info["skipped"])
        self._ok(msg)


class LayerOptionsTool(QWidget):
    """Preferences for the whole tab, persisted in config.json. They change
    how the other tools behave rather than doing anything themselves."""

    def __init__(self, bridge, window, parent=None):
        super().__init__(parent)
        self.bridge = bridge
        self.window = window
        self.stack = None          # set by LayersPage so changes take effect

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)

        blurb = QLabel("These are remembered between sessions.")
        blurb.setWordWrap(True)
        blurb.setObjectName("dim")
        lay.addWidget(blurb)

        self.chk_sync_names = QCheckBox("Sync layer and action names")
        self.chk_sync_names.setToolTip(
            "Renaming a layer renames its action too — and a layer whose "
            "action was renamed elsewhere takes the new name back.\n"
            "Layers sharing one action are left alone (they would fight over "
            "the name).")
        self.chk_sync_names.clicked.connect(self._save)
        lay.addWidget(self.chk_sync_names)

        row = QHBoxLayout()
        row.addWidget(self._dim_label("Blend type for new layers"))
        self.blend = QComboBox()
        for b in BLEND_TYPES:
            self.blend.addItem(b.title(), b)
        self.blend.setToolTip(
            "What a new layer starts as. Combine is the usual choice for "
            "adding on top of a base animation; the very first (base) layer "
            "is always Replace.")
        self.blend.currentIndexChanged.connect(self._save)
        row.addWidget(self.blend)
        row.addStretch(1)
        lay.addLayout(row)

        note = QLabel(
            "Auto blend on action load is the checkbox next to Load in the "
            "Layers list — it's remembered too.")
        note.setWordWrap(True)
        note.setObjectName("dim")
        lay.addWidget(note)
        lay.addStretch(1)

    @staticmethod
    def _dim_label(text):
        lab = QLabel(text)
        lab.setObjectName("dim")
        return lab

    def load_settings(self, settings):
        self.chk_sync_names.blockSignals(True)
        self.blend.blockSignals(True)
        try:
            self.chk_sync_names.setChecked(bool(settings.get("sync_names",
                                                             True)))
            i = self.blend.findData(settings.get("default_blend", "COMBINE"))
            if i >= 0:
                self.blend.setCurrentIndex(i)
        finally:
            self.chk_sync_names.blockSignals(False)
            self.blend.blockSignals(False)

    def settings(self):
        return {"sync_names": self.chk_sync_names.isChecked(),
                "default_blend": self.blend.currentData()}

    def _save(self, *_):
        if self.stack is not None:
            self.stack.apply_settings(self.settings())
        if self.window is not None and hasattr(self.window, "save_settings"):
            self.window.save_settings()

    def set_capture_busy(self, busy):
        self.setEnabled(not busy)


class LayersPage(QWidget):
    """The Layers rail entry: the stack on top, its tools directly below.
    They belong together — every tool acts on whichever layer is selected in
    the list, so splitting them across rail entries hid the thing you need
    to see while using them."""

    def __init__(self, bridge, window, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)

        self.stack = LayerStackTool(bridge, window)
        lay.addWidget(self.stack, 1)          # the list absorbs spare height

        # ⚠ MERGE / BAKE LIVES HERE, not in its own rail entry (Marty,
        # 2026-08-05: "Merge/bake options from Anim layers tab should be under
        # 'Layers' right inbetween layers and Layer tools"). It acts on the
        # layer stack directly above it, so putting it on a page of its own
        # meant flattening a stack you could no longer see.
        rule0 = QFrame()
        rule0.setFrameShape(QFrame.HLine)
        rule0.setStyleSheet("color: %s;" % theme.BORDER)
        lay.addWidget(rule0)

        heading0 = QLabel("Merge / Bake")
        heading0.setStyleSheet("font-weight: bold;")
        lay.addWidget(heading0)

        self.merge_bake = MergeBakeTool(bridge, window)
        lay.addWidget(self.merge_bake)

        rule = QFrame()
        rule.setFrameShape(QFrame.HLine)
        rule.setStyleSheet("color: %s;" % theme.BORDER)   # same rule ToolPage uses
        lay.addWidget(rule)

        heading = QLabel("Layer Tools")
        heading.setStyleSheet("font-weight: bold;")
        lay.addWidget(heading)

        self.tools = LayerToolsTool(bridge, window)
        lay.addWidget(self.tools)

        rule2 = QFrame()
        rule2.setFrameShape(QFrame.HLine)
        rule2.setStyleSheet("color: %s;" % theme.BORDER)
        lay.addWidget(rule2)

        heading2 = QLabel("Layer Settings")
        heading2.setStyleSheet("font-weight: bold;")
        lay.addWidget(heading2)

        self.settings = LayerSettingsTool(bridge, window)
        lay.addWidget(self.settings)

        # one poll feeds all of them: the stack already asks Blender every 1.5 s
        self.stack.status_refreshed.connect(self.merge_bake.on_layers_changed)
        self.stack.status_refreshed.connect(self.tools.on_layers_changed)
        self.stack.status_refreshed.connect(self.settings.on_layers_changed)

    def load_settings(self, settings):
        self.stack.apply_settings(settings)

    def settings_now(self):
        """What the page wants persisted — auto blend lives on the stack's
        Load row, so it is read back from there."""
        return {"auto_blend": self.stack.chk_autoblend.isChecked()}

    def set_capture_busy(self, busy):
        for tool in (self.stack, self.merge_bake, self.tools, self.settings):
            tool.setEnabled(not busy)
