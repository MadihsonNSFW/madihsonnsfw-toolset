"""Organize tab — object SETS saved in the .blend, and a one-press Isolate.

**Layout A, "sets and members"** (Marty picked it from four rendered mockups,
2026-08-18): the list of sets on the left, the members of the picked one on
the right, one big Isolate toggle underneath. The Blender N-panel
(`blender_addon\\madi_anim_library\\organize.py`) draws the same list with the
same star per row, so the two are literally the same picture — which is what
"in sync with blender UI" has to mean if it is to need no explaining.

⚠ **THE .BLEND IS THE ONLY STORE.** Nothing about a set lives in
`config.json`. Blender redraws its panel from `Scene.madi_sets`; this tab
polls `sets_list` and compares `revision`. Neither notifies the other. That is
the Timeline Markers design and it is here for the same reason: two stores
that must agree are two stores that will not.

⚠ **THE STAR AND THE ROW ARE DIFFERENT CONTROLS**, the same way a Texture Maps
chip's tick and its selection are. Clicking a row says "show me this set's
members"; clicking its star says "isolate this set". Merging them would mean
you cannot look at a set without changing what the viewport shows.

⚠ **EVERY REBUILD IS GUARDED BY ITS OWN SIGNATURE.** A 1.5 s poll that
rebuilt two trees each time would be the tab's whole cost, and Marty's scenes
are not small. `_apply` compares three cheap keys — the sets signature, the
members signature and the isolated uid — and touches only what moved. The
common poll (nothing changed) does no widget work at all; the second-commonest
(the star moved) repaints two icons.

Where the rest lives:
  * `blender_addon\\madi_anim_library\\organize.py` — the sets, the operators,
    the N-panel, and the isolate/restore algorithm with its warnings.
  * `docs\\organize.md` — the module doc.
"""

from PySide6.QtCore import QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QIcon
from PySide6.QtWidgets import (QAbstractItemView, QHBoxLayout,
                               QGroupBox, QInputDialog, QLabel, QMessageBox,
                               QPushButton, QSizePolicy, QSplitter,
                               QToolButton, QTreeWidget, QTreeWidgetItem,
                               QVBoxLayout, QWidget)

import bridge as bridgemod
import icons
import theme
import widgets

TITLE = "Organize"
FEATURE = "organize_sets"

# Same cadence as Markers and Anim Layers: one poll per 1.5 s while the tab is
# on screen, and none at all while it is not.
POLL_MS = 1500

# Which object types get which glyph. Anything else falls back to the mesh
# triangle rather than drawing nothing.
KIND_ICON = {
    "ARMATURE": "ob_armature",
    "MESH": "ob_mesh",
    "LIGHT": "ob_light",
    "CAMERA": "ob_camera",
    "EMPTY": "ob_empty",
    "CURVE": "ob_mesh",
    "SURFACE": "ob_mesh",
    "META": "ob_mesh",
    "FONT": "ob_mesh",
    "VOLUME": "ob_mesh",
    "LATTICE": "ob_empty",
    "GPENCIL": "ob_mesh",
    "GREASEPENCIL": "ob_mesh",
    "MISSING": "warn",
}

# ⚠ ICONS ARE CACHED BY (name, size, colour) AND NOT REBUILT PER ROW. A
# QIcon is a painted pixmap; a 40-set scene with 25 members each asked for
# 1000 of them per rebuild before this existed. `icons.pixmap` has its own
# cache, but the QIcon wrapper around it does not, and the wrapper is what a
# tree item stores.
_ICON_CACHE = {}


def _icon(name, size=15, colour=None):
    key = (name, size, colour)
    got = _ICON_CACHE.get(key)
    if got is None:
        got = icons.icon(name, size, colour or theme.TEXT)
        _ICON_CACHE[key] = got
    return got


def kind_icon(kind):
    if kind == "MISSING":
        return _icon("warn", 15, theme.WARN)
    return _icon(KIND_ICON.get(kind, "ob_mesh"))


def set_icon(members):
    """One glyph for a whole set — a rig if it has one, else lights/cameras
    if that is all it is, else a mesh. Mirrors `_set_icon` in the add-on, and
    `organize_test.py` checks the two agree."""
    kinds = [m.get("type") for m in members]
    if "ARMATURE" in kinds:
        return _icon("ob_armature")
    lit = [k for k in kinds if k in ("LIGHT", "CAMERA")]
    if kinds and len(lit) == len(kinds):
        return _icon("ob_light" if kinds.count("LIGHT") >= kinds.count("CAMERA")
                     else "ob_camera")
    return _icon("ob_mesh")


def describe(members):
    """"1 rig · 7 meshes" — the make-up line stolen from mockup B."""
    words = {"ARMATURE": ("rig", "rigs"), "MESH": ("mesh", "meshes"),
             "LIGHT": ("light", "lights"), "CAMERA": ("camera", "cameras"),
             "EMPTY": ("empty", "empties")}
    counts = {}
    for member in members:
        counts[member.get("type")] = counts.get(member.get("type"), 0) + 1
    parts = []
    for kind in ("ARMATURE", "MESH", "LIGHT", "CAMERA", "EMPTY"):
        n = counts.get(kind)
        if n:
            one, many = words[kind]
            parts.append("%d %s" % (n, one if n == 1 else many))
    other = sum(n for kind, n in counts.items()
                if kind not in words and kind != "MISSING")
    if other:
        parts.append("%d other" % other)
    if counts.get("MISSING"):
        parts.append("%d missing" % counts["MISSING"])
    return " · ".join(parts) or "empty"


class SetsTree(QTreeWidget):
    """The list of sets. Column 0 is the isolate star and is a BUTTON, not a
    selection — clicking it must not move the row cursor."""

    isolate_clicked = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setHeaderLabels(["", "Set", "Objects"])
        self.setRootIsDecorated(False)
        self.setIconSize(QSize(15, 15))
        self.setSelectionMode(QAbstractItemView.SingleSelection)
        self.setColumnWidth(0, 30)
        header = self.header()
        header.setStretchLastSection(False)
        from PySide6.QtWidgets import QHeaderView
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        self.setColumnWidth(2, 74)
        # ⚠ Not a fixed minimum: this pane must be able to squeeze with the
        # window (the 549 px floor — `docs\app-shell.md`).
        self.setMinimumWidth(150)

    def mousePressEvent(self, event):
        item = self.itemAt(event.position().toPoint())
        if item is not None and self.columnAt(int(event.position().x())) == 0:
            uid = item.data(0, Qt.UserRole)
            if uid:
                self.isolate_clicked.emit(uid)
                return                    # swallow: the star is not a select
        super().mousePressEvent(event)


class OrganizePage(QWidget):
    """Scene ▸ Organize."""

    def __init__(self, bridge, window, parent=None):
        super().__init__(parent)
        self.bridge = bridge
        self.window = window
        self._busy = False
        # What the last reply said, and the three signatures `_apply` compares.
        self._sets = []
        self._isolated = None
        self._active_uid = None
        self._revision = None
        self._sets_sig = None
        self._members_sig = None
        self._selected = []
        self._build()

        self._timer = QTimer(self)
        self._timer.setInterval(POLL_MS)
        self._timer.timeout.connect(self._poll)

    # ------------------------------------------------------------- layout
    def _build(self):
        outer = QVBoxLayout(self)
        # ⚠ NO MARGINS AND NO TITLE BLOCK SINCE 1.24.0. This page lives inside
        # a `ToolPage` on the Organize rail now (Isolate | Rig properties),
        # and ToolPage draws the heading, the rule and the margins. Keeping
        # its own drew "Organize" under "Isolate" with two rules between them.
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(8)

        self.blurb = widgets.ElidedLabel(
            "Sets of objects — a rig with its meshes, the lights for a shot, "
            "the props on a table. Pick one, select it, or isolate it with "
            "one press.", minimum=120)
        self.blurb.setObjectName("dim")
        outer.addWidget(self.blurb)

        split = QSplitter(Qt.Horizontal)
        split.addWidget(self._build_sets())
        split.addWidget(self._build_members())
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 1)
        split.setSizes([440, 420])
        outer.addWidget(split, 1)

        self.status = widgets.ElidedLabel("", minimum=120)
        self.status.setObjectName("dim")
        outer.addWidget(self.status)

    def _build_sets(self):
        host = QWidget()
        lay = QVBoxLayout(host)
        lay.setContentsMargins(0, 0, 6, 0)
        lay.setSpacing(6)

        # ⚠ A FlowLayout, for the reason the Texture Maps chip row now uses
        # one: four buttons in a plain QHBoxLayout report their total as the
        # row's minimum, and a QMainWindow takes its widest child. This row
        # wraps instead (`docs\app-shell.md`, and `widgets.FlowLayout`).
        bar = widgets.FlowLayout(h_spacing=4, v_spacing=4)
        self.new_button = QPushButton("New set from selection")
        self.new_button.setIcon(_icon("plus"))
        self.new_button.setProperty("_madi_keep_text", True)
        self.new_button.setToolTip(
            "Make a set from what is selected in Blender")
        self.new_button.clicked.connect(self.new_set)
        bar.addWidget(self.new_button)
        self.rename_button = QPushButton("Rename")
        self.rename_button.clicked.connect(self.rename_set)
        bar.addWidget(self.rename_button)
        self.delete_button = QPushButton("Delete")
        self.delete_button.setToolTip("Delete the set. The objects are kept")
        self.delete_button.clicked.connect(self.delete_set)
        bar.addWidget(self.delete_button)
        self.up_button = QToolButton()
        self.up_button.setIcon(_icon("up"))
        self.up_button.setToolTip("Move up")
        self.up_button.clicked.connect(lambda: self.move_set(-1))
        bar.addWidget(self.up_button)
        self.down_button = QToolButton()
        self.down_button.setIcon(_icon("down"))
        self.down_button.setToolTip("Move down")
        self.down_button.clicked.connect(lambda: self.move_set(1))
        bar.addWidget(self.down_button)
        bar_host = QWidget()
        bar_host.setLayout(bar)
        lay.addWidget(bar_host)

        self.tree = SetsTree()
        self.tree.currentItemChanged.connect(self._on_row)
        self.tree.isolate_clicked.connect(self.toggle_isolate)
        lay.addWidget(self.tree, 1)
        return host

    def _build_members(self):
        host = QWidget()
        lay = QVBoxLayout(host)
        lay.setContentsMargins(6, 0, 0, 0)
        lay.setSpacing(6)

        self.members_group = QGroupBox("Members")
        inner = QVBoxLayout(self.members_group)
        inner.setContentsMargins(8, 6, 8, 8)
        inner.setSpacing(6)
        self.members = QTreeWidget()
        self.members.setHeaderHidden(True)
        self.members.setRootIsDecorated(False)
        self.members.setIconSize(QSize(15, 15))
        self.members.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.members.setMinimumWidth(140)
        inner.addWidget(self.members, 1)

        row = widgets.FlowLayout(h_spacing=4, v_spacing=4)
        self.add_button = QPushButton("Add selected")
        self.add_button.setIcon(_icon("plus"))
        self.add_button.setProperty("_madi_keep_text", True)
        self.add_button.setToolTip(
            "Add what is selected in Blender to this set")
        self.add_button.clicked.connect(self.add_selected)
        row.addWidget(self.add_button)
        self.remove_button = QPushButton("Remove")
        self.remove_button.setToolTip(
            "Remove the members picked here from the set. The objects are "
            "not deleted")
        self.remove_button.clicked.connect(self.remove_members)
        row.addWidget(self.remove_button)
        self.select_button = QPushButton("Select in Blender")
        self.select_button.setIcon(_icon("cursor"))
        self.select_button.setProperty("_madi_keep_text", True)
        self.select_button.clicked.connect(self.select_set)
        row.addWidget(self.select_button)
        self.clean_button = QPushButton("Clean missing")
        self.clean_button.setIcon(_icon("warn", 15, theme.WARN))
        self.clean_button.setProperty("_madi_keep_text", True)
        self.clean_button.setToolTip(
            "Drop members whose object has been deleted from the file")
        self.clean_button.clicked.connect(self.clean_set)
        self.clean_button.hide()
        row.addWidget(self.clean_button)
        row_host = QWidget()
        row_host.setLayout(row)
        inner.addWidget(row_host)
        lay.addWidget(self.members_group, 1)

        self.isolate_button = QPushButton("Isolate")
        self.isolate_button.setObjectName("accent")
        self.isolate_button.setIcon(_icon("solo", 15, "white"))
        self.isolate_button.setProperty("_madi_keep_text", True)
        self.isolate_button.setCheckable(True)
        self.isolate_button.setMinimumHeight(32)
        self.isolate_button.clicked.connect(
            lambda: self.toggle_isolate(self._active_uid))
        lay.addWidget(self.isolate_button)

        self.note = QLabel(
            "Isolate is Blender's Local View — the same thing as selecting "
            "the set and pressing /. It shows only that set in the viewport "
            "and leaves everything else exactly as it is; nothing is hidden "
            "and renders are unaffected. Press it again to come back out. "
            "Sets are saved inside the .blend, so they survive renaming the "
            "file and renaming the objects.")
        self.note.setObjectName("dim")
        self.note.setWordWrap(True)
        # ⚠ A word-wrapped QLabel still reports a wide minimumSizeHint on some
        # styles; capping it keeps this paragraph out of the window minimum.
        self.note.setMinimumWidth(120)
        self.note.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        lay.addWidget(self.note)
        return host

    # ---------------------------------------------------------- lifecycle
    def showEvent(self, event):
        super().showEvent(event)
        self._timer.start()
        # ⚠ `poll=True` semantics, for the reason Markers documents: switching
        # to a tab is automatic, not a button press, and a connect to a dead
        # localhost port is DROPPED rather than refused on this machine — a
        # loud refresh would stall the tab switch for the whole timeout every
        # time Blender is closed.
        self.refresh(quiet=True)

    def hideEvent(self, event):
        self._timer.stop()
        super().hideEvent(event)

    def set_capture_busy(self, busy):
        """The window greys every page while Blender is rendering."""
        self._busy = bool(busy)
        self._set_enabled(not busy)

    def _set_enabled(self, on):
        on = bool(on) and not self._busy
        for widget in (self.tree, self.members, self.new_button,
                       self.rename_button, self.delete_button,
                       self.up_button, self.down_button, self.add_button,
                       self.remove_button, self.select_button,
                       self.clean_button, self.isolate_button):
            widget.setEnabled(on)

    def feature_reason(self):
        try:
            return self.bridge.feature_reason(FEATURE)
        except Exception:                # noqa: BLE001 — a dead bridge is routine
            return None

    # ------------------------------------------------------------- reading
    def refresh(self, quiet=False):
        reason = self.feature_reason()
        if reason:
            self._set_enabled(False)
            self.status.setText(reason)
            return
        try:
            data = self.bridge.sets_list()
        except bridgemod.BridgeError as exc:
            self._set_enabled(False)
            if not quiet:
                self.status.setText("Blender is not answering: %s" % exc)
            else:
                self.status.setText("Open Blender to use sets.")
            return
        self._set_enabled(True)
        self._apply(data)

    def _poll(self):
        if self._busy or self.feature_reason():
            return
        try:
            data = self.bridge.sets_list()
        except bridgemod.BridgeError:
            return                       # a dead bridge is not an error here
        if data.get("revision") == self._revision:
            # ⚠ THE COMMON CASE, AND IT DOES NO WIDGET WORK AT ALL. Everything
            # below this line is skipped for as long as nothing in the .blend
            # moves, which is nearly always.
            return
        self._apply(data)

    # ------------------------------------------------------------ applying
    def _apply(self, data):
        """Take a reply and update only the parts of the UI that moved.

        Three signatures, cheapest first — see the module docstring. Built
        from the data, never from the widgets, so a rebuild can never be
        skipped because a widget happens to already look right.
        """
        self._revision = data.get("revision")
        self._sets = data.get("sets") or []
        self._selected = data.get("selected") or []
        isolated = data.get("isolated")
        # ⚠⚠ **ASSIGNED BEFORE THE REBUILD, NOT AFTER.** `_rebuild_sets` ends
        # by painting the stars, and it paints them from `self._isolated` —
        # so with the assignment below the branch, the very first refresh
        # drew every star hollow while the status line and the big button
        # both said a set was isolated. Caught by rendering the finished tab
        # and looking at it; no check in the suite compared an icon.
        was_isolated = self._isolated
        self._isolated = isolated

        sets_sig = tuple((s.get("uid"), s.get("name"), s.get("count"),
                          s.get("missing")) for s in self._sets)
        if sets_sig != self._sets_sig:
            self._sets_sig = sets_sig
            self._rebuild_sets(data)
        elif isolated != was_isolated:
            # The list itself did not change — only which row wears the star.
            self._repaint_stars()

        active = self._current_set()
        members = active.get("members") if active else []
        members_sig = (self._active_uid,
                       tuple((m.get("name"), m.get("type")) for m in members))
        if members_sig != self._members_sig:
            self._members_sig = members_sig
            self._rebuild_members(active)
        self._update_isolate_button()
        self._update_status(data)

    def _rebuild_sets(self, data):
        # ⚠ Remember the cursor by UID, not by row: a set can be moved or
        # deleted between two polls, and a row index would then select some
        # other set — quietly, and the members pane would follow it.
        want = self._active_uid
        self.tree.blockSignals(True)
        self.tree.clear()
        chosen = None
        for entry in self._sets:
            item = QTreeWidgetItem(["", entry.get("name") or "",
                                    str(entry.get("count") or 0)])
            item.setData(0, Qt.UserRole, entry.get("uid"))
            item.setIcon(1, set_icon(entry.get("members") or []))
            item.setTextAlignment(2, Qt.AlignRight | Qt.AlignVCenter)
            item.setToolTip(1, describe(entry.get("members") or []))
            if entry.get("missing"):
                item.setIcon(2, _icon("warn", 14, theme.WARN))
                item.setToolTip(2, "%d object(s) no longer in the file"
                                % entry["missing"])
            self.tree.addTopLevelItem(item)
            if entry.get("uid") == want:
                chosen = item
        if chosen is None and self.tree.topLevelItemCount():
            index = int(data.get("active") or 0)
            index = max(0, min(index, self.tree.topLevelItemCount() - 1))
            chosen = self.tree.topLevelItem(index)
        self.tree.blockSignals(False)
        if chosen is not None:
            self.tree.setCurrentItem(chosen)
            self._active_uid = chosen.data(0, Qt.UserRole)
        else:
            self._active_uid = None
        self._repaint_stars()

    def _repaint_stars(self):
        """Only the star column. This is what a poll costs when somebody
        presses Isolate in Blender: two icon swaps, no rebuild."""
        for index in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(index)
            on = item.data(0, Qt.UserRole) == self._isolated
            item.setIcon(0, _icon("solo" if on else "solo_off", 15,
                                  theme.ACCENT if on else theme.TEXT_DIM))

    def _rebuild_members(self, entry):
        self.members.clear()
        members = (entry or {}).get("members") or []
        missing = 0
        for member in members:
            name = member.get("name")
            kind = member.get("type")
            if not name:
                missing += 1
                item = QTreeWidgetItem(["(deleted)"])
                item.setForeground(0, QColor(theme.WARN))
                item.setIcon(0, kind_icon("MISSING"))
            else:
                item = QTreeWidgetItem([name])
                item.setIcon(0, kind_icon(kind))
                item.setData(0, Qt.UserRole, name)
            self.members.addTopLevelItem(item)
        self.clean_button.setVisible(bool(missing))
        title = "Members"
        if entry:
            title = "Members — %s  ·  %d" % (entry.get("name") or "",
                                             len(members))
        self.members_group.setTitle(title)

    def _update_isolate_button(self):
        active = self._current_set()
        on = bool(active) and active.get("uid") == self._isolated
        self.isolate_button.setChecked(on)
        if active:
            self.isolate_button.setText(
                ("In Local View  %s" if on else "Isolate  %s")
                % (active.get("name") or ""))
        else:
            self.isolate_button.setText("Isolate")
        self.isolate_button.setEnabled(bool(active) and not self._busy)

    def _update_status(self, data):
        if self._isolated:
            entry = self._find(self._isolated)
            if entry:
                self.status.setText(
                    "Local View: %s · %d objects — press Isolate again to "
                    "come back out" % (entry.get("name") or "",
                                       entry.get("count") or 0))
                return
        if not self._sets:
            self.status.setText(
                "No sets yet. Select objects in Blender and press New set "
                "from selection.")
            return
        self.status.setText("%d set%s · %d object%s selected in Blender"
                            % (len(self._sets),
                               "" if len(self._sets) == 1 else "s",
                               len(self._selected),
                               "" if len(self._selected) == 1 else "s"))

    # ------------------------------------------------------------ helpers
    def _find(self, uid):
        for entry in self._sets:
            if entry.get("uid") == uid:
                return entry
        return None

    def _current_set(self):
        return self._find(self._active_uid)

    def _on_row(self, current, _previous):
        self._active_uid = (current.data(0, Qt.UserRole)
                            if current is not None else None)
        entry = self._current_set()
        members = entry.get("members") if entry else []
        self._members_sig = (self._active_uid,
                             tuple((m.get("name"), m.get("type"))
                                   for m in members))
        self._rebuild_members(entry)
        self._update_isolate_button()

    def _write(self, call, *args, **kwargs):
        """Run one write, then re-read. ⚠ The reply carries the new
        `revision`, so the poll that crosses with this write cannot undo it —
        `_apply` is driven from the fresh read, not from the stale poll."""
        try:
            call(*args, **kwargs)
        except bridgemod.BridgeError as exc:
            QMessageBox.warning(self, TITLE, str(exc))
            return False
        self.refresh()
        return True

    # ------------------------------------------------------------- actions
    def new_set(self):
        self._write(self.bridge.sets_new, None, True)
        # Land the cursor on what was just made — it is always the last row.
        if self._sets:
            self._active_uid = self._sets[-1].get("uid")
            self._sets_sig = None            # force the row cursor to move
            self.refresh()

    def rename_set(self):
        entry = self._current_set()
        if not entry:
            return
        name, ok = QInputDialog.getText(self, "Rename set", "Name:",
                                        text=entry.get("name") or "")
        if ok and name.strip():
            self._write(self.bridge.sets_rename, entry["uid"], name.strip())

    def delete_set(self):
        entry = self._current_set()
        if not entry:
            return
        if QMessageBox.question(
                self, "Delete set",
                "Delete “%s”?\n\nThe objects themselves are not touched."
                % (entry.get("name") or ""),
                QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
            return
        self._active_uid = None
        self._write(self.bridge.sets_delete, entry["uid"])

    def move_set(self, delta):
        entry = self._current_set()
        if entry:
            self._write(self.bridge.sets_move, entry["uid"], delta)

    def add_selected(self):
        entry = self._current_set()
        if entry:
            self._write(self.bridge.sets_add, entry["uid"], None)

    def remove_members(self):
        entry = self._current_set()
        if not entry:
            return
        names = [item.data(0, Qt.UserRole)
                 for item in self.members.selectedItems()
                 if item.data(0, Qt.UserRole)]
        if not names:
            # ⚠ NOT a message box. "You didn't pick anything" is a nudge, not
            # an error, and a modal for it stops the app dead — including any
            # driver or test, which is how this was found.
            self.status.setText(
                "Pick the members to remove in the list on the right first.")
            return
        self._write(self.bridge.sets_remove, entry["uid"], names)

    def clean_set(self):
        entry = self._current_set()
        if entry:
            self._write(self.bridge.sets_clean, entry["uid"])

    def select_set(self):
        entry = self._current_set()
        if entry:
            self._write(self.bridge.sets_select, entry["uid"], False)

    def toggle_isolate(self, uid):
        """The toggle. Passing the uid that is already isolated clears it,
        which the add-on decides — the app never sends "off" for a specific
        set, it just sends the uid and lets one place own the rule."""
        if not uid:
            return
        self._write(self.bridge.sets_isolate, uid)

    def retheme(self):
        _ICON_CACHE.clear()
        self.new_button.setIcon(_icon("plus"))
        self.add_button.setIcon(_icon("plus"))
        self.select_button.setIcon(_icon("cursor"))
        self.clean_button.setIcon(_icon("warn", 15, theme.WARN))
        self.up_button.setIcon(_icon("up"))
        self.down_button.setIcon(_icon("down"))
        self.isolate_button.setIcon(_icon("solo", 15, "white"))
        self._sets_sig = None                # icons are baked into the rows
        self._members_sig = None
        if self._sets:
            self._rebuild_sets({"active": 0})
            self._rebuild_members(self._current_set())
