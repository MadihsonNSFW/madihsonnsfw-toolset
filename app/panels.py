"""Sidebar (folders + type filters) and the right-hand Info/Options panel."""

import time

from PySide6.QtCore import Qt, QMimeData, QSize, QTimer, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (QAbstractSpinBox, QCheckBox, QComboBox,
                               QDoubleSpinBox, QFormLayout, QFrame, QGridLayout,
                               QHBoxLayout,
                               QLabel, QLineEdit, QListWidget, QListWidgetItem,
                               QMenu, QPushButton, QSizePolicy, QSlider,
                               QSpinBox, QTabWidget, QTreeWidget,
                               QTreeWidgetItem, QVBoxLayout, QWidget)

# Room for a five-digit frame number and no more. See the note where these are
# built: without a cap they take width from the button beside them.
FRAME_FIELD_MAX_W = 72

import grid as gridmod
import theme
import widgets

_icon_cache = {}

# drag-drop payload: the library-relative path of a sidebar folder
FOLDER_MIME = "application/x-madi-library-folder"


def folder_icon(color=None, size=14):
    """Small flat folder icon drawn in theme colors."""
    key = (color, size)
    if key in _icon_cache:
        return _icon_cache[key]
    c = QColor(color or "#d8b45a")
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(Qt.NoPen)
    p.setBrush(c)
    w, h = size, size
    path = QPainterPath()
    # back tab
    path.addRoundedRect(w * 0.06, h * 0.18, w * 0.42, h * 0.2, 1.5, 1.5)
    # body
    path.addRoundedRect(w * 0.06, h * 0.28, w * 0.88, h * 0.54, 2, 2)
    p.drawPath(path.simplified())
    p.end()
    icon = QIcon(pm)
    _icon_cache[key] = icon
    return icon


def library_icon(size=14):
    key = ("lib", size)
    if key in _icon_cache:
        return _icon_cache[key]
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(theme.ACCENT))
    s = size
    # 2x2 grid = "all items"
    for x, y in ((0.1, 0.1), (0.55, 0.1), (0.1, 0.55), (0.55, 0.55)):
        p.drawRoundedRect(s * x, s * y, s * 0.35, s * 0.35, 1.5, 1.5)
    p.end()
    icon = QIcon(pm)
    _icon_cache[key] = icon
    return icon


class FolderTree(QTreeWidget):
    """Folder tree: accepts tile drags from the grid (move items) and drags of
    its own folder nodes (move a folder into another one; 'All' = library root)."""

    itemsDropped = Signal(list, object)  # abs item paths, target rel folder (None = All/root)
    folderDropped = Signal(str, object)  # dragged rel folder, target rel folder (None = root)
    deleteRequested = Signal(str)        # Del key on a folder node (never the All root)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDragEnabled(True)
        self.setDragDropMode(QTreeWidget.DragDrop)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Delete:
            sel = self.selectedItems()
            rel = sel[0].data(0, Qt.UserRole) if sel else None
            if rel:  # the "All" root (rel None) is not deletable
                self.deleteRequested.emit(rel)
                return
        super().keyPressEvent(event)

    def mimeData(self, items):
        rel = items[0].data(0, Qt.UserRole) if items else None
        if rel is None:
            return None  # the "All" root isn't draggable
        md = QMimeData()
        md.setData(FOLDER_MIME, rel.encode("utf-8"))
        return md

    def _drag_rel(self, event):
        return bytes(event.mimeData().data(FOLDER_MIME)).decode("utf-8")

    def _droppable(self, event):
        node = self.itemAt(event.position().toPoint())
        if node is None:
            return False
        if event.mimeData().hasFormat(gridmod.ITEM_MIME):
            return True
        if event.mimeData().hasFormat(FOLDER_MIME):
            rel = self._drag_rel(event)
            target = node.data(0, Qt.UserRole)
            parent = rel.rsplit("/", 1)[0] if "/" in rel else None
            if target == parent:
                return False  # already lives there
            if target is not None and (target == rel
                                       or target.startswith(rel + "/")):
                return False  # can't move a folder into itself / its subtree
            return True
        return False

    def dragEnterEvent(self, event):
        if (event.mimeData().hasFormat(gridmod.ITEM_MIME)
                or event.mimeData().hasFormat(FOLDER_MIME)):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        if self._droppable(event):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        if not self._droppable(event):
            event.ignore()
            return
        node = self.itemAt(event.position().toPoint())
        target = node.data(0, Qt.UserRole)
        if event.mimeData().hasFormat(gridmod.ITEM_MIME):
            raw = bytes(event.mimeData().data(gridmod.ITEM_MIME)).decode("utf-8")
            self.itemsDropped.emit([p for p in raw.split("\n") if p], target)
            event.acceptProposedAction()
            return
        # folder drag: report the drop as a COPY so Qt doesn't also delete the
        # dragged node from the tree — the view moves the folder on disk and
        # rebuilds the whole tree via rescan()
        self.folderDropped.emit(self._drag_rel(event), target)
        event.setDropAction(Qt.CopyAction)
        event.accept()


class AuthorCombo(QComboBox):
    """The author filter, filled when it is OPENED rather than when the
    library is scanned.

    ⚠⚠ **FILLING THIS EAGERLY MEANT PARSING THE ENTIRE LIBRARY.** The author
    of an item lives inside its data file, so building the list called
    `Item.meta()` — and therefore `read_data()`, whose own docstring says
    *"only call on selection, files can be big"* — once per item, every time
    the library was scanned. Profiled on 2026-08-15 over 800 poses:
    **1,604 `io.open` calls, 3.47 s of a 4.54 s `LibraryView` build**, and
    those were synthetic poses holding no bone data. Marty's carry 461 bones
    each, so the real files are far bigger. A dropdown nobody had opened was
    the most expensive thing in the tab.

    ⚠ **A RESCAN MARKS IT STALE, IT DOES NOT CLEAR IT.** Clearing would drop a
    selected author on every rescan — and the library rescans on import, on
    delete and on the folder watcher — so the filter would silently reset
    itself while the user was using it. The current text is preserved and
    re-selected when the list is next built.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._source = None
        self._stale = True

    def set_source(self, source):
        """`source()` returns the set of author names, called on demand."""
        self._source = source
        self._stale = True

    def mark_stale(self):
        self._stale = True

    def showPopup(self):
        self._fill()
        super().showPopup()

    def _fill(self):
        if not self._stale or self._source is None:
            return
        self._stale = False
        current = self.currentText()
        self.blockSignals(True)
        self.clear()
        self.addItem("Any author")
        for name in sorted(self._source(), key=str.lower):
            self.addItem(name)
        found = self.findText(current)
        self.setCurrentIndex(found if found >= 0 else 0)
        self.blockSignals(False)


class Sidebar(QWidget):
    selectionChanged = Signal()  # folder or type-filter change
    itemsDropped = Signal(list, object)  # re-emitted from the folder tree
    folderDropped = Signal(str, object)  # re-emitted from the folder tree
    deleteFolderRequested = Signal(str)  # re-emitted from the folder tree
    renameFolderRequested = Signal(str, str)  # rel path, new name (inline edit)
    newFolderRequested = Signal(str)     # right-click empty tree space ("" = root)

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 4, 8)

        lab = QLabel("Type")
        lab.setObjectName("dim")
        lay.addWidget(lab)
        # ⚠ EVERY TYPE HAS TO BE HERE. `LibraryView.refilter` drops any item
        # whose type is not in this set, so a type missing from this list is not
        # "unfilterable" — it is INVISIBLE, no matter what is on disk. `.picker`
        # and `.vgroups` were exactly that until Marty asked why his vertex
        # groups never showed up (2026-08-05); `app_vgroups_test.py` now pins
        # the list against `library.ITEM_EXTS` so the next type cannot repeat it.
        # ⚠ "playblast" is NOT in this list any more, and that is the ONE type
        # whose absence here is correct: `library.scan` no longer produces them
        # at all (Marty, 2026-08-05), so a checkbox would filter nothing.
        # `app_vgroups_test.py` compares this list against `library.ITEM_EXTS`,
        # which playblasts were never in — they were loose files, not items.
        # ⚠ A DragCheckBox, not a QCheckBox: press one and drag down the list to
        # set them all the same way (Marty, 2026-08-08). It only works because
        # they share this one parent — the gesture is resolved by asking the
        # parent what is under the cursor. Keep them siblings.
        self.type_checks = {}
        for typ in ("pose", "anim", "set", "mirror", "shapes", "remap", "abc",
                    "vgroups", "picker", "renderpreset"):
            cb = widgets.DragCheckBox(gridmod.type_label(typ))
            cb.setIcon(gridmod.type_icon(typ, 14))
            cb.setChecked(True)
            cb.toggled.connect(lambda *_: self.selectionChanged.emit())
            self.type_checks[typ] = cb
            lay.addWidget(cb)

        lab2 = QLabel("Folders")
        lab2.setObjectName("dim")
        lay.addWidget(lab2)
        self.tree = FolderTree()
        self.tree.setHeaderHidden(True)
        self.tree.setIconSize(QSize(14, 14))
        self.tree.itemSelectionChanged.connect(lambda: self.selectionChanged.emit())
        self.tree.itemsDropped.connect(self.itemsDropped.emit)
        self.tree.folderDropped.connect(self.folderDropped.emit)
        self.tree.deleteRequested.connect(self.deleteFolderRequested.emit)
        self.tree.setExpandsOnDoubleClick(False)  # double-click = rename, not toggle
        self.tree.itemDoubleClicked.connect(self._on_double_click)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._on_tree_menu)
        self.tree.itemChanged.connect(self._on_folder_edited)
        self.tree.itemDelegate().closeEditor.connect(self._on_editor_closed)
        self._editing = None  # (node, rel, original label) during inline rename
        lay.addWidget(self.tree, 1)

        lab3 = QLabel("Tags")
        lab3.setObjectName("dim")
        lay.addWidget(lab3)
        self.tag_list = QListWidget()
        self.tag_list.setMaximumHeight(110)
        self.tag_list.itemChanged.connect(lambda *_: self.selectionChanged.emit())
        lay.addWidget(self.tag_list)

        lab4 = QLabel("Filters")
        lab4.setObjectName("dim")
        lay.addWidget(lab4)
        self.author_combo = AuthorCombo()
        self.author_combo.addItem("Any author")
        self.date_combo = QComboBox()
        self.date_combo.addItems(["Any time", "Today", "Last 7 days", "Last 30 days"])
        self.len_combo = QComboBox()
        self.len_combo.addItems(["Any length", "≤ 24 frames", "25-100 frames",
                                 "> 100 frames"])
        for combo in (self.author_combo, self.date_combo, self.len_combo):
            combo.currentIndexChanged.connect(lambda *_: self.selectionChanged.emit())
            lay.addWidget(combo)

    def set_folders(self, folders, counts):
        """folders: list of 'a/b' relpaths; counts: {relfolder: n_items_recursive}"""
        self.tree.blockSignals(True)
        prev = self.current_folder()
        self.tree.clear()
        root = QTreeWidgetItem(["All  (%d)" % counts.get(None, 0)])
        root.setData(0, Qt.UserRole, None)
        root.setIcon(0, library_icon())
        self.tree.addTopLevelItem(root)
        nodes = {}
        for rel in folders:
            parts = rel.split("/")
            parent = root if len(parts) == 1 else nodes.get("/".join(parts[:-1]), root)
            n = counts.get(rel, 0)
            label = parts[-1] + ("  (%d)" % n if n else "")
            node = QTreeWidgetItem([label])
            node.setData(0, Qt.UserRole, rel)
            node.setIcon(0, folder_icon())
            parent.addChild(node)
            nodes[rel] = node
        self.tree.expandAll()
        target = nodes.get(prev, root)
        self.tree.setCurrentItem(target)
        self.tree.blockSignals(False)

    def _on_tree_menu(self, pos):
        """Right-click in the EMPTY space below the folders → New Folder (root).
        Right-clicking a node keeps its normal behavior (no menu)."""
        if self.tree.itemAt(pos) is not None:
            return
        menu = QMenu(self.tree)
        act = menu.addAction("New Folder…")
        if menu.exec(self.tree.viewport().mapToGlobal(pos)) is act:
            self.newFolderRequested.emit("")

    def _on_double_click(self, node, _col):
        rel = node.data(0, Qt.UserRole)
        if not rel:  # the "All" root has no rel path — nothing to rename
            return
        old_label = node.text(0)
        self.tree.blockSignals(True)
        node.setText(0, rel.rsplit("/", 1)[-1])  # edit the bare name, no count
        node.setFlags(node.flags() | Qt.ItemIsEditable)
        self.tree.blockSignals(False)
        self._editing = (node, rel, old_label)
        self.tree.editItem(node, 0)

    def _on_folder_edited(self, node, _col):
        """Inline edit committed (Enter / focus-out)."""
        if self._editing is None or node is not self._editing[0]:
            return
        _node, rel, _old = self._editing
        self._editing = None
        self.tree.blockSignals(True)
        node.setFlags(node.flags() & ~Qt.ItemIsEditable)
        self.tree.blockSignals(False)
        self.renameFolderRequested.emit(rel, node.text(0).strip())

    def _on_editor_closed(self, *_):
        """Editor closed without a commit (Esc) — put the old label back."""
        if self._editing is None:
            return
        node, _rel, old_label = self._editing
        self._editing = None
        self.tree.blockSignals(True)
        node.setText(0, old_label)
        node.setFlags(node.flags() & ~Qt.ItemIsEditable)
        self.tree.blockSignals(False)

    def select_folder(self, rel):
        """Select a folder node by its rel path (after a rename/rescan)."""
        def walk(node):
            for i in range(node.childCount()):
                ch = node.child(i)
                if ch.data(0, Qt.UserRole) == rel:
                    self.tree.setCurrentItem(ch)
                    return True
                if walk(ch):
                    return True
            return False
        root = self.tree.topLevelItem(0)
        if root is not None:
            walk(root)

    def current_folder(self):
        sel = self.tree.selectedItems()
        return sel[0].data(0, Qt.UserRole) if sel else None

    def enabled_types(self):
        return {t for t, cb in self.type_checks.items() if cb.isChecked()}

    def set_tags(self, counts):
        """counts: {tag: n_items}. Checked state survives the rebuild."""
        checked = self.checked_tags()
        self.tag_list.blockSignals(True)
        self.tag_list.clear()
        for tag in sorted(counts, key=str.lower):
            li = QListWidgetItem("%s  (%d)" % (tag, counts[tag]))
            li.setData(Qt.UserRole, tag)
            li.setFlags(li.flags() | Qt.ItemIsUserCheckable)
            li.setCheckState(Qt.Checked if tag in checked else Qt.Unchecked)
            self.tag_list.addItem(li)
        self.tag_list.blockSignals(False)

    def checked_tags(self):
        return {self.tag_list.item(i).data(Qt.UserRole)
                for i in range(self.tag_list.count())
                if self.tag_list.item(i).checkState() == Qt.Checked}

    def set_authors(self, authors):
        """Fill the author list NOW from a ready-made set.

        Kept for callers that already hold the names. The library view uses
        `set_author_source` instead — see `AuthorCombo` for why paying for this
        on every scan was the single most expensive thing in the tab.
        """
        self.author_combo.set_source(lambda: authors)
        self.author_combo.mark_stale()
        self.author_combo._fill()

    def set_author_source(self, source):
        """Hand the author list a way to compute itself when it is opened."""
        self.author_combo.set_source(source)

    def filters(self):
        return {
            "tags": self.checked_tags(),
            "author": (None if self.author_combo.currentIndex() == 0
                       else self.author_combo.currentText()),
            "days": {1: 1, 2: 7, 3: 30}.get(self.date_combo.currentIndex()),
            "length": self.len_combo.currentIndex(),  # 0 any, 1 ≤24, 2 25-100, 3 >100
        }


class SelectAllSpinBox(QSpinBox):
    """A frame field that behaves like a text box you can just type into.

    Two things, both Marty's (2026-08-04): "the Start and end fields should lose
    the small buttons they have and when clicked the text 'start' and 'end'
    should be gone so users can type the frame range".

    ⚠ CLEARED, NOT JUST SELECTED. Selecting the placeholder and letting the
    next keystroke replace it is the usual trick and it is what this did — but
    the word stays on screen until something is typed, so the field still reads
    as "occupied" at the moment you click it. Emptying it is the difference
    between "type over this" and "type here".

    Leaving it empty puts the placeholder back on the way out, so the meaning
    of the field ("use the scene's own range") is never lost by clicking it.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        # No up/down arrows: these are frame numbers people type, and the
        # steppers were only ever in the way at this size.
        self.setButtonSymbols(QAbstractSpinBox.NoButtons)

    def _showing_placeholder(self):
        return bool(self.specialValueText()) and self.value() == self.minimum()

    def focusInEvent(self, event):
        super().focusInEvent(event)
        # Deferred: the click that gave us focus also places a cursor, so both
        # the clear and the select have to happen AFTER it to stick.
        if self._showing_placeholder():
            QTimer.singleShot(0, self.lineEdit().clear)
        else:
            QTimer.singleShot(0, self.lineEdit().selectAll)

    def focusOutEvent(self, event):
        # Nothing typed -> back to the placeholder. Without this the field
        # would sit visibly blank and there would be no way to say "just use
        # the scene range" again short of retyping the number it had.
        if not self.lineEdit().text().strip():
            self.setValue(self.minimum())
        super().focusOutEvent(event)


class InfoPanel(QWidget):
    applyRequested = Signal(object, dict)   # item, options
    saveRequested = Signal(str, str)        # kind ("pose"|"set"|"anim"), name
    blendStarted = Signal()                 # slider grabbed -> live blend session
    blendChanged = Signal(float)
    blendEnded = Signal()
    recaptureRequested = Signal(object)     # item -> re-render its thumbnail/sequence
    deleteRequested = Signal()              # delete the grid's selected items

    def __init__(self, parent=None):
        super().__init__(parent)
        self._item = None
        self._capture_busy = False  # a preview capture is rendering in Blender
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 8, 8, 8)

        # ⚠ EVERYTHING ABOUT THE SELECTED ITEM LIVES IN ONE CONTAINER, so it
        # can be hidden in one move when there is no selection (Marty,
        # 2026-08-08, with a screenshot: a full-height panel of "—" was taking
        # the right-hand third of the window to say nothing). The New item box
        # below is NOT in here — you save with nothing selected, so it has to
        # stay.
        self.detail = QWidget()
        dlay = QVBoxLayout(self.detail)
        dlay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.detail, 1)

        self.preview = QLabel()
        self.preview.setFixedHeight(190)
        self.preview.setAlignment(Qt.AlignCenter)
        self.preview.setStyleSheet("background: %s; border-radius: 6px;" % theme.PANEL)
        dlay.addWidget(self.preview)

        title_row = QHBoxLayout()
        self.title = QLabel("—")
        self.title.setObjectName("h1")
        self.title.setAlignment(Qt.AlignCenter)
        self.btn_recapture = QPushButton("📷")
        self.btn_recapture.setObjectName("flat")
        self.btn_recapture.setToolTip("Re-render this item's thumbnail (and preview "
                                      "sequence for anims) from the current viewport")
        self.btn_recapture.setEnabled(False)
        self.btn_recapture.clicked.connect(
            lambda: self._item is not None and self.recaptureRequested.emit(self._item))
        title_row.addStretch(1)
        title_row.addWidget(self.title)
        title_row.addStretch(1)
        title_row.addWidget(self.btn_recapture)
        dlay.addLayout(title_row)

        tabs = QTabWidget()
        dlay.addWidget(tabs, 1)

        # ---- Info tab
        info = QWidget()
        self.info_form = QFormLayout(info)
        self.info_labels = {}
        for key in ("Type", "Folder", "Author", "Created", "Blender", "Armature",
                    "Bones", "Frames", "Tags", "Description"):
            val = QLabel("—")
            val.setWordWrap(True)
            val.setTextInteractionFlags(Qt.TextSelectableByMouse)
            self.info_labels[key] = val
            k = QLabel(key)
            k.setObjectName("dim")
            self.info_form.addRow(k, val)
        tabs.addTab(info, "Info")

        # ---- Options tab
        opts = QWidget()
        ov = QVBoxLayout(opts)
        form = QFormLayout()
        self.blend_slider = QSlider(Qt.Horizontal)
        self.blend_slider.setRange(0, 100)
        self.blend_slider.setValue(100)
        self.blend_spin = QDoubleSpinBox()
        self.blend_spin.setRange(0.0, 1.0)
        self.blend_spin.setSingleStep(0.05)
        self.blend_spin.setValue(1.0)
        self.blend_slider.valueChanged.connect(
            lambda v: self.blend_spin.setValue(v / 100.0))
        self.blend_spin.valueChanged.connect(
            lambda v: self.blend_slider.setValue(int(v * 100)))
        # live blend streaming while the slider is dragged
        self.blend_slider.sliderPressed.connect(self.blendStarted.emit)
        self.blend_slider.valueChanged.connect(
            lambda v: self.blendChanged.emit(v / 100.0)
            if self.blend_slider.isSliderDown() else None)
        self.blend_slider.sliderReleased.connect(self.blendEnded.emit)
        self.blend_slider.setToolTip(
            "Influence: how much of the item is applied on Load — poses and "
            "anims blend from the rig's current state, shape keys land at "
            "that fraction of the saved deltas (100% = exact). Dragging live-"
            "previews poses only.")
        row = QHBoxLayout()
        row.addWidget(self.blend_slider, 1)
        row.addWidget(self.blend_spin)
        form.addRow("Blend", row)
        ov.addLayout(form)
        self.chk_key = QCheckBox("Key (insert keyframes)")
        self.chk_selected = QCheckBox("Selected bones only")
        self.chk_extend = QCheckBox("Extend selection (sets)")
        self.chk_mirror = QCheckBox("Mirror (flip L/R)")
        ov.addWidget(self.chk_key)
        ov.addWidget(self.chk_selected)
        ov.addWidget(self.chk_extend)
        ov.addWidget(self.chk_mirror)
        self.mirror_label = QLabel("Mirror table: auto-detect")
        self.mirror_label.setObjectName("dim")
        self.mirror_label.setWordWrap(True)
        ov.addWidget(self.mirror_label)
        self.chk_remap = QCheckBox("Use Remap (rig-to-rig)")
        self.chk_remap.setToolTip("Resolve item bone names through the active "
                                  ".remap table before applying (remap runs "
                                  "first, then Mirror on the target rig)")
        ov.addWidget(self.chk_remap)
        self.remap_label = QLabel("Remap table: none")
        self.remap_label.setObjectName("dim")
        self.remap_label.setWordWrap(True)
        ov.addWidget(self.remap_label)
        # ⚠ Shorter than it reads, on purpose. A QCheckBox cannot wrap its own
        # label, so a long one sets the whole panel's minimum width and the
        # right-hand side ends up clipped or scrolling sideways. The full
        # sentence is in the tooltip immediately below, which is where the
        # detail belongs anyway.
        self.chk_shapes_active = QCheckBox("Shapes → active object")
        self.chk_shapes_active.setToolTip("Add a .shapes item's keys to the "
                                          "ACTIVE mesh even if the object name "
                                          "differs (vertex count must still "
                                          "match; single-mesh items only)")
        ov.addWidget(self.chk_shapes_active)

        anim_lab = QLabel("Animation")
        anim_lab.setObjectName("dim")
        ov.addWidget(anim_lab)
        self.anim_mode = QComboBox()
        self.anim_mode.addItems(["Replace", "Merge", "Insert"])
        ov.addWidget(self.anim_mode)
        self.chk_at_current = QCheckBox("Paste at current frame")
        self.chk_at_current.setChecked(True)
        ov.addWidget(self.chk_at_current)
        ov.addStretch(1)
        tabs.addTab(opts, "Options")

        self.btn_apply = QPushButton("Load")
        self.btn_apply.setObjectName("accent")
        self.btn_apply.setEnabled(False)
        self.btn_apply.clicked.connect(self._emit_apply)
        dlay.addWidget(self.btn_apply)

        self.btn_delete = QPushButton("Delete")
        self.btn_delete.setObjectName("danger")
        self.btn_delete.setToolTip("Delete the selected item(s) from the library "
                                   "(Del key works too)")
        self.btn_delete.setEnabled(False)
        self.btn_delete.clicked.connect(self.deleteRequested.emit)
        dlay.addWidget(self.btn_delete)
        self.detail.hide()      # nothing is selected when the panel is built

        # ---- Save box
        box = QFrame()
        box.setObjectName("panel")
        bv = QVBoxLayout(box)
        lab = QLabel("New item  (into current folder)")
        lab.setObjectName("dim")
        # Wraps rather than holding the whole panel open at its own width. A
        # QLabel does not wrap by default, so this one line was setting the
        # panel's minimum on its own.
        lab.setWordWrap(True)
        bv.addWidget(lab)
        self.save_name = QLineEdit()
        self.save_name.setPlaceholderText("name…  (blank = unnamed_#)")
        bv.addWidget(self.save_name)
        # ⚠ A 2x2 GRID, NOT A ROW OF THREE. Three buttons side by side needed
        # 390 px before they started clipping, which on its own set the whole
        # panel's minimum width — the "right side gets split in half" Marty
        # screenshotted on 2026-08-04. Two columns halve that, and the buttons
        # keep their full words instead of being squeezed.
        saves = QGridLayout()
        saves.setContentsMargins(0, 0, 0, 0)
        b1 = QPushButton("Save Pose")
        b1.clicked.connect(lambda: self._emit_save("pose"))
        b2 = QPushButton("Save Set")
        b2.clicked.connect(lambda: self._emit_save("set"))
        b4 = QPushButton("Save Mirror")
        b4.setToolTip("Auto-detect L/R bone pairs on the active armature and "
                      "save them as a mirror table")
        b4.clicked.connect(lambda: self._emit_save("mirror"))
        b3 = QPushButton("Save Anim")
        b3.clicked.connect(lambda: self._emit_save("anim"))
        saves.addWidget(b1, 0, 0)
        saves.addWidget(b2, 0, 1)
        saves.addWidget(b4, 1, 0)
        saves.addWidget(b3, 1, 1)
        bv.addLayout(saves)
        rowa = QHBoxLayout()
        # ⚠ The frame fields are capped so the row degrades in the right order.
        # In a QHBoxLayout everything shrinks together, so a narrow panel took
        # the width out of the BUTTON — which has a word in it — while two
        # numeric fields kept space they never needed, and "Save Anim" clipped.
        # Reported by Marty with a screenshot, 2026-08-04.
        self.anim_start = SelectAllSpinBox()
        self.anim_start.setRange(-100000, 100000)
        self.anim_start.setSpecialValueText("start")  # at minimum -> scene start
        self.anim_start.setValue(self.anim_start.minimum())
        self.anim_end = SelectAllSpinBox()
        self.anim_end.setRange(-100000, 100000)
        self.anim_end.setSpecialValueText("end")
        self.anim_end.setValue(self.anim_end.minimum())
        self.anim_start.setToolTip(
            "First frame — click and type. Leave it empty for the scene's own "
            "start frame.")
        self.anim_end.setToolTip(
            "Last frame — click and type. Leave it empty for the scene's own "
            "end frame.")
        for field in (self.anim_start, self.anim_end):
            field.setMaximumWidth(FRAME_FIELD_MAX_W)
            field.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        frames_lab = QLabel("Frames")
        frames_lab.setObjectName("dim")
        rowa.addWidget(frames_lab)
        rowa.addWidget(self.anim_start)
        rowa.addWidget(self.anim_end)
        rowa.addStretch(1)
        bv.addLayout(rowa)
        # ⚠ "Bake every frame" USED TO BE A CHECKBOX HERE. It moved into the
        # Save Anim dialog on 2026-08-05 ("The menu for animation export
        # settings should be opened after clicking 'Save anim'") along with the
        # two new options, because a save-time decision belongs with the save,
        # not parked in a panel where it silently applies to the next one. The
        # frame boxes above stayed: they still SEED the dialog, same as they do
        # for Export Abc.
        b7 = QPushButton("Export Abc")
        b7.setToolTip("Export the SELECTED objects to an Alembic (.abc) cache "
                      "item — uses the anim start/end boxes for the frame "
                      "range (leave at 'start'/'end' for the scene range)")
        b7.clicked.connect(lambda: self._emit_save("abc"))
        bv.addWidget(b7)
        b5 = QPushButton("Save Shape Keys…")
        b5.setToolTip("Vault the shape keys of the selected MESH objects into the "
                      "library (per-key checklist; optionally delete them from the "
                      "mesh to slim the .blend)")
        b5.clicked.connect(lambda: self._emit_save("shapes"))
        bv.addWidget(b5)
        b7 = QPushButton("Save Vertex Groups")
        b7.setToolTip("Store the vertex groups (weight paint) of the selected "
                      "MESH objects. Loading puts them back exactly; there is "
                      "a separate Transfer for putting them on a different "
                      "mesh, which is an approximation.")
        b7.clicked.connect(lambda: self._emit_save("vgroups"))
        bv.addWidget(b7)
        b6 = QPushButton("Save Remap…")
        b6.setToolTip("Build a rig-to-rig remap table: auto-match the SOURCE "
                      "item/armature's bone names onto the TARGET (active) "
                      "armature, hand-fix the leftovers, save as a .remap item")
        b6.clicked.connect(lambda: self._emit_save("remap"))
        bv.addWidget(b6)
        # Marty, 2026-08-10: "add a save picker tab button in Studio Library,
        # that will save picker tab directly in library". The Bone picker tab
        # could already do this from its own Presets box; this is the same
        # command reached from where the rest of the saving happens.
        b8 = QPushButton("Save Picker Tab")
        b8.setToolTip("Save the ACTIVE Bone picker tab's layout as a .picker "
                      "item — its buttons drawn onto the tab's reference "
                      "picture for the preview")
        b8.clicked.connect(lambda: self._emit_save("picker"))
        bv.addWidget(b8)
        # Fixed height, and a spacer after it: with the detail block hidden
        # there is nothing left that wants to grow, and without this the save
        # box would stretch to fill the window instead of sitting at the top.
        box.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        lay.addWidget(box)
        # Stretch 0, so it only absorbs space when the detail block (stretch 1)
        # is hidden.
        lay.addStretch(0)
        self._save_buttons = (b1, b2, b4, b3, b5, b6, b7, b8)

    # ------------------------------------------------------------------

    def set_capture_busy(self, busy):
        """Grey out everything that would hit the bridge while Blender is busy
        rendering a preview (Delete stays enabled — it's disk-only)."""
        self._capture_busy = busy
        for b in self._save_buttons:
            b.setEnabled(not busy)
        has = self._item is not None
        self.btn_apply.setEnabled(has and not busy)
        self.btn_recapture.setEnabled(has and not busy and not self._item.bare
                                      and self._item.type not in ("mirror", "remap"))
        self.blend_slider.setEnabled(not busy)

    def options(self):
        start = self.anim_start.value()
        end = self.anim_end.value()
        return {
            "blend": self.blend_spin.value(),
            "key": self.chk_key.isChecked(),
            "selected_only": self.chk_selected.isChecked(),
            "extend": self.chk_extend.isChecked(),
            "mirror": self.chk_mirror.isChecked(),
            "remap": self.chk_remap.isChecked(),
            "shapes_to_active": self.chk_shapes_active.isChecked(),
            "anim_mode": self.anim_mode.currentText().lower(),
            "start_at": "current" if self.chk_at_current.isChecked() else "original",
            "frame_start": None if start == self.anim_start.minimum() else start,
            "frame_end": None if end == self.anim_end.minimum() else end,
        }

    def _emit_apply(self):
        if self._item is not None:
            self.applyRequested.emit(self._item, self.options())

    def _emit_save(self, kind):
        # blank name is fine — the view auto-names it unnamed_1, unnamed_2, …
        self.saveRequested.emit(kind, self.save_name.text().strip())

    def show_item(self, item):
        self._item = item
        if item is None:
            # Hide the whole block rather than showing a column of "—".
            # The preview, the Info/Options tabs, Load and Delete all describe
            # a selected item; with none there is nothing for them to say, and
            # they were taking a third of the window to say it.
            self.detail.hide()
            self.title.setText("—")
            self.preview.clear()
            self.btn_apply.setEnabled(False)
            self.btn_delete.setEnabled(False)
            self.btn_recapture.setEnabled(False)
            for lab in self.info_labels.values():
                lab.setText("—")
            return
        self.detail.show()
        self.title.setText(item.name)
        self.btn_apply.setEnabled(not self._capture_busy)
        self.btn_delete.setEnabled(True)
        self.btn_recapture.setEnabled(item.type not in ("mirror", "remap")
                                      and not item.bare
                                      and not self._capture_busy)
        self.btn_apply.setText({"set": "Select", "mirror": "Use Table",
                                "shapes": "Add Keys", "abc": "Import",
                                "playblast": "Play",
                                "remap": "Use Remap"}.get(item.type, "Load"))
        pm = gridmod.thumbnail_pixmap(item, 180)
        self.preview.setPixmap(pm.scaledToHeight(180, Qt.SmoothTransformation)
                               if isinstance(pm, QPixmap) else pm)

        data = item.read_data()
        meta = data.get("metadata", {})
        bones = data.get("bones", {})
        self.info_labels["Type"].setText(
            item.type + ("  (loose file)" if item.bare else ""))
        self.info_labels["Folder"].setText(item.folder or "(root)")
        self.info_labels["Author"].setText(meta.get("author") or "—")
        self.info_labels["Created"].setText(meta.get("created") or
                                            time.strftime("%Y-%m-%d", time.localtime(item.mtime)))
        self.info_labels["Blender"].setText(meta.get("blender_version") or "—")
        self.info_labels["Armature"].setText(meta.get("source_armature") or "—")
        if item.type == "mirror":
            self.info_labels["Bones"].setText("%d pairs, %d center" % (
                len(data.get("map", {})) // 2, len(data.get("center", []))))
        elif item.type == "remap":
            self.info_labels["Bones"].setText("%d mapped, %d unmatched" % (
                len(data.get("map", {})), len(data.get("unmatched", []))))
        elif item.type == "shapes":
            ms = data.get("meshes", [])
            self.info_labels["Bones"].setText("%d key(s) on %d mesh(es)" % (
                sum(len(m.get("keys", [])) for m in ms), len(ms)))
        elif item.type == "abc":
            objs = meta.get("objects") or []
            self.info_labels["Bones"].setText(
                "%d object(s)" % len(objs) if objs else "—")
        else:
            self.info_labels["Bones"].setText(str(len(bones)) if bones else "—")
        if meta.get("frame_start") is not None:
            fps = meta.get("fps")  # older items predate the fps stamp
            self.info_labels["Frames"].setText("%d – %d  (%d frames%s)" % (
                meta["frame_start"], meta["frame_end"],
                meta["frame_end"] - meta["frame_start"] + 1,
                "  @ %g fps" % fps if fps else ""))
        else:
            self.info_labels["Frames"].setText("—")
        self.info_labels["Tags"].setText(", ".join(item.tags) or "—")
        self.info_labels["Description"].setText(meta.get("description") or "—")
