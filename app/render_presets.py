"""Render presets — the Rendering tab's preset manager.

Blender's own preset system in the Toolset's UI: read the scene's render
settings, keep them under a name, put them back later. The catalogue of WHICH
settings lives in the add-on (`renderpresets.py`) so there is one list, not two
that drift; this side owns the store on disk and the UI.

WHERE PRESETS LIVE, AND WHY IT MATTERS
`render_presets\` next to config.json — so, for the installed build, next to
the exe. ⚠ **A PyInstaller rebuild WIPES the dist folder**, which already costs
config.json / render_queue\ / _preview_cache\ if they are not carried across;
this folder joins that list (`docs\app-shell.md`).

One JSON file per preset, named after the preset. The display name lives
INSIDE the file as well, so a file someone renames by hand still shows the name
they gave it, and a name with a slash or a colon in it is still saveable.
"""

import datetime
import json
import os
import re

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QDialog,
                               QDialogButtonBox, QHBoxLayout, QInputDialog,
                               QLabel, QLineEdit, QListWidget, QListWidgetItem,
                               QMessageBox, QPushButton, QSplitter, QTreeWidget,
                               QTreeWidgetItem, QVBoxLayout, QWidget)

import bridge as bridgemod
import config
import library as librarymod
import theme
import widgets

FEATURE = "render_presets"
DIRNAME = "render_presets"
EXT = ".json"

# Studio Library item type. ⚠ The extension is also in THREE other lists —
# `core.ITEM_EXTS`, `library.ITEM_EXTS` and `panels.Sidebar.type_checks` — and
# the last one fails silently (core.py's comment has the whole story).
ITEM_TYPE = "renderpreset"
ITEM_EXT = ".renderpreset"
ITEM_DATA = "renderpreset.json"

_SAFE = re.compile(r"[^A-Za-z0-9 _.-]+")


def presets_dir():
    return os.path.join(config.DATA_DIR, DIRNAME)


def _slug(name):
    """A filename for a preset name. Never empty, never a path."""
    text = _SAFE.sub("_", (name or "").strip()).strip(" .")
    text = re.sub(r"\s+", " ", text)[:60].strip()
    return text or "preset"


def free_path(name, folder=None):
    """An unused file path for *name* — `Final 4K.json`, then `Final 4K 2.json`."""
    folder = folder or presets_dir()
    base = _slug(name)
    path = os.path.join(folder, base + EXT)
    n = 2
    while os.path.exists(path):
        path = os.path.join(folder, "%s %d%s" % (base, n, EXT))
        n += 1
    return path


def load_preset(path):
    """One preset off disk, or None if it is not one of ours.

    A folder the user can open is a folder the user can drop anything into, so
    anything unreadable is skipped rather than raised — a stray .json must not
    take the whole list down.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("groups"), dict):
        return None
    data.setdefault("name", os.path.splitext(os.path.basename(path))[0])
    data["path"] = path
    return data


def list_presets(folder=None):
    """Every readable preset, newest name-sorted (case-insensitive)."""
    folder = folder or presets_dir()
    out = []
    try:
        names = sorted(os.listdir(folder), key=str.lower)
    except OSError:
        return out
    for entry in names:
        if not entry.lower().endswith(EXT):
            continue
        data = load_preset(os.path.join(folder, entry))
        if data is not None:
            out.append(data)
    out.sort(key=lambda d: (d.get("name") or "").lower())
    return out


def write_preset(data, path=None, folder=None):
    """Save a preset; returns the path written. Creates the folder on demand."""
    folder = folder or presets_dir()
    try:
        os.makedirs(folder, exist_ok=True)
    except OSError:
        pass
    if path is None:
        path = free_path(data.get("name", "preset"), folder)
    payload = dict(data)
    payload.pop("path", None)
    payload["saved"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=1)
    return path


def delete_preset(path):
    try:
        os.remove(path)
        return True
    except OSError:
        return False


def count_settings(data):
    return sum(len((g or {}).get("values") or {})
               for g in (data.get("groups") or {}).values())


# ---------------------------------------------------------- Studio Library

def library_item_path(root, folder, name):
    return os.path.join(root, folder or "", _slug(name) + ITEM_EXT)


def write_library_item(root, folder, name, preset, overwrite=False):
    """Write a preset into a library as a `<name>.renderpreset` item.

    ⚠ **Written app-side, with no bridge call**, unlike every other item type.
    A render preset is already JSON in the app's hands — sending it to Blender
    so Blender can write a file would add nothing except a way for it to fail
    with Blender closed. The FORMAT is the shared thing, not the writer, which
    is why `type` and `metadata` match what `core._metadata` produces.

    Overwriting versions the old payload first, exactly as the add-on's saves
    do, so a replaced preset is recoverable from Versions… like anything else.
    """
    path = library_item_path(root, folder, name)
    if os.path.isdir(path):
        if not overwrite:
            raise FileExistsError(path)
        librarymod.snapshot_item(path)
    os.makedirs(path, exist_ok=True)
    payload = {
        "type": ITEM_TYPE,
        "metadata": {
            "format_version": 1,
            "created": datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            "author": os.environ.get("USERNAME") or os.environ.get("USER") or "",
            "blender_version": preset.get("blender") or "",
            "source_file": "",
            "engine": preset.get("engine") or "",
            "settings": count_settings(preset),
        },
        "name": name,
        "blender": preset.get("blender"),
        "scene": preset.get("scene"),
        "engine": preset.get("engine"),
        "groups": preset.get("groups") or {},
    }
    with open(os.path.join(path, ITEM_DATA), "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=1)
    return path


class SaveToLibraryDialog(widgets.GuardedDialog):
    """Which library, which folder, what name."""

    def __init__(self, parent, libraries, name=""):
        super().__init__(parent)
        self.setWindowTitle("Save to Studio Library")
        self.setMinimumWidth(420)
        self._libraries = list(libraries)

        lay = QVBoxLayout(self)
        lay.setSpacing(8)

        row = QHBoxLayout()
        row.addWidget(QLabel("Library"))
        self.lib = QComboBox()
        for cfg in self._libraries:
            self.lib.addItem(cfg.get("name") or cfg.get("path", "?"),
                             cfg.get("path"))
        self.lib.currentIndexChanged.connect(self._reload_folders)
        row.addWidget(self.lib, 1)
        lay.addLayout(row)

        row2 = QHBoxLayout()
        row2.addWidget(QLabel("Folder"))
        self.folder = QComboBox()
        row2.addWidget(self.folder, 1)
        lay.addLayout(row2)

        row3 = QHBoxLayout()
        row3.addWidget(QLabel("Name"))
        self.name = QLineEdit(name)
        self.name.textChanged.connect(self._update_ok)
        row3.addWidget(self.name, 1)
        lay.addLayout(row3)

        self.note = QLabel("")
        self.note.setWordWrap(True)
        self.note.setObjectName("dim")
        lay.addWidget(self.note)

        self.buttons = QDialogButtonBox(QDialogButtonBox.Save
                                        | QDialogButtonBox.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        lay.addWidget(self.buttons)

        self._reload_folders()

    def _reload_folders(self):
        self.folder.clear()
        self.folder.addItem("(library root)", "")
        root = self.lib.currentData()
        if root:
            try:
                folders, _items = librarymod.scan(root)
            except Exception:               # noqa: BLE001 — a missing root is
                folders = []                # a note, not a crash
            for rel in folders:
                self.folder.addItem(rel, rel)
        self._update_ok()

    def target(self):
        return self.lib.currentData(), self.folder.currentData() or ""

    def preset_name(self):
        return self.name.text().strip()

    def exists(self):
        root, folder = self.target()
        if not root or not self.preset_name():
            return False
        return os.path.isdir(library_item_path(root, folder,
                                               self.preset_name()))

    def _update_ok(self):
        ok = self.buttons.button(QDialogButtonBox.Save)
        has = bool(self.preset_name()) and bool(self.lib.currentData())
        if ok is not None:
            ok.setEnabled(has)
        self.note.setText(
            "An item called '%s' is already there — saving keeps the old one "
            "under Versions…" % self.preset_name() if self.exists() else
            "Saved as a library item, so it can be tagged, versioned and "
            "zipped for sharing like anything else in the library.")


class SavePresetDialog(widgets.GuardedDialog):
    """Name it, and tick which groups of settings it keeps.

    The values are already captured when this opens, so the tree shows what is
    actually about to be saved rather than a promise. Groups the add-on marks
    `default: False` — the output path and the frame range — start UNTICKED:
    they are per-shot settings inside a per-look feature, and a preset that
    silently retimed the scene would be a bug report, not a feature.
    """

    def __init__(self, parent, captured, name="", groups=None):
        super().__init__(parent)
        self.setWindowTitle("Save render preset")
        self.resize(560, 560)
        self._captured = captured

        lay = QVBoxLayout(self)
        lay.setSpacing(8)

        row = QHBoxLayout()
        row.addWidget(QLabel("Name"))
        self.name = QLineEdit(name)
        self.name.setPlaceholderText("Final 4K — Cycles 512")
        self.name.textChanged.connect(self._update_ok)
        row.addWidget(self.name, 1)
        lay.addLayout(row)

        blurb = QLabel("Everything ticked is stored in the preset. Applying it "
                       "later writes exactly these settings and nothing else.")
        blurb.setWordWrap(True)
        blurb.setObjectName("dim")
        lay.addWidget(blurb)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Setting", "Value"])
        self.tree.setColumnWidth(0, 320)
        self.tree.setSelectionMode(QAbstractItemView.NoSelection)
        lay.addWidget(self.tree, 1)

        picked = None if groups is None else set(groups)
        for key, block in (captured.get("groups") or {}).items():
            values = block.get("values") or {}
            node = QTreeWidgetItem(
                self.tree, ["%s  (%d)" % (block.get("label", key), len(values)),
                            ""])
            node.setData(0, Qt.UserRole, key)
            node.setFlags(node.flags() | Qt.ItemIsUserCheckable)
            on = block.get("default", True) if picked is None else key in picked
            # A group this Blender has nothing for cannot be ticked into
            # existence — an empty group in a preset is a row that promises
            # settings it does not carry.
            if not values:
                node.setCheckState(0, Qt.Unchecked)
                node.setFlags(node.flags() & ~Qt.ItemIsEnabled)
            else:
                node.setCheckState(0, Qt.Checked if on else Qt.Unchecked)
            for path, value in values.items():
                QTreeWidgetItem(node, [path, _fmt(value)])
        self.tree.itemChanged.connect(lambda *_a: self._update_ok())

        btn_row = QHBoxLayout()
        self.btn_all = QPushButton("Select all")
        self.btn_all.clicked.connect(lambda: self._set_all(True))
        btn_row.addWidget(self.btn_all)
        self.btn_none = QPushButton("Select none")
        self.btn_none.clicked.connect(lambda: self._set_all(False))
        btn_row.addWidget(self.btn_none)
        btn_row.addStretch(1)
        self.count = QLabel("")
        self.count.setObjectName("dim")
        btn_row.addWidget(self.count)
        lay.addLayout(btn_row)

        self.buttons = QDialogButtonBox(QDialogButtonBox.Save
                                        | QDialogButtonBox.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        lay.addWidget(self.buttons)
        self._update_ok()

    def _set_all(self, on):
        for i in range(self.tree.topLevelItemCount()):
            node = self.tree.topLevelItem(i)
            if node.flags() & Qt.ItemIsEnabled:
                node.setCheckState(0, Qt.Checked if on else Qt.Unchecked)

    def selection(self):
        """The ticked group keys, in catalogue order."""
        out = []
        for i in range(self.tree.topLevelItemCount()):
            node = self.tree.topLevelItem(i)
            if node.checkState(0) == Qt.Checked:
                out.append(node.data(0, Qt.UserRole))
        return out

    def preset_name(self):
        return self.name.text().strip()

    def groups_payload(self):
        """The captured blocks for the ticked groups only."""
        picked = set(self.selection())
        return {k: v for k, v in (self._captured.get("groups") or {}).items()
                if k in picked}

    def _update_ok(self):
        picked = self.selection()
        settings = sum(len((self._captured["groups"][k].get("values") or {}))
                       for k in picked)
        self.count.setText("%d setting(s) in %d group(s)"
                           % (settings, len(picked)))
        ok = self.buttons.button(QDialogButtonBox.Save)
        if ok is not None:
            ok.setEnabled(bool(self.preset_name()) and settings > 0)


def _fmt(value):
    if isinstance(value, bool):
        return "on" if value else "off"
    if isinstance(value, float):
        return ("%.4f" % value).rstrip("0").rstrip(".")
    return str(value)


class RenderPresetsTool(QWidget):
    """Save the scene's render settings under a name and put them back later."""

    def __init__(self, bridge, window, parent=None):
        super().__init__(parent)
        self.bridge = bridge
        self.window = window
        self._presets = []

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(9)

        blurb = QLabel(
            "Stores the scene's render settings — engine, sampling, denoising, "
            "light paths, output format, colour management — under a name, and "
            "writes them back whenever you pick one. Applying a preset is not "
            "an undo step in Blender, so save what you have now before trying "
            "someone else's.")
        blurb.setWordWrap(True)
        blurb.setObjectName("dim")
        lay.addWidget(blurb)

        split = QSplitter(Qt.Horizontal)

        self.list = QListWidget()
        self.list.setMinimumWidth(170)
        self.list.currentItemChanged.connect(lambda *_a: self._show_current())
        self.list.itemDoubleClicked.connect(lambda *_a: self.apply_preset())
        split.addWidget(self.list)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Setting", "Value"])
        self.tree.setColumnWidth(0, 300)
        self.tree.setSelectionMode(QAbstractItemView.NoSelection)
        split.addWidget(self.tree)
        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)
        split.setSizes([200, 520])
        lay.addWidget(split, 1)

        row = QHBoxLayout()
        self.btn_save = QPushButton("Save current settings…")
        self.btn_save.setObjectName("accent")
        self.btn_save.setToolTip(
            "Read the scene's render settings and keep them under a name.")
        self.btn_save.clicked.connect(self.save_current)
        row.addWidget(self.btn_save)
        self.btn_apply = QPushButton("Apply to scene")
        self.btn_apply.setToolTip("Write the selected preset onto the scene.")
        self.btn_apply.clicked.connect(self.apply_preset)
        row.addWidget(self.btn_apply)
        self.btn_update = QPushButton("Update from scene")
        self.btn_update.setToolTip(
            "Re-read the scene into the selected preset, keeping the same "
            "groups it already stores.")
        self.btn_update.clicked.connect(self.update_preset)
        row.addWidget(self.btn_update)
        row.addStretch(1)
        lay.addLayout(row)

        row2 = QHBoxLayout()
        self.btn_library = QPushButton("Save to Studio Library")
        self.btn_library.setToolTip(
            "Copy the selected preset into a library as a .renderpreset item, "
            "so it can be tagged, versioned and zipped for sharing.")
        self.btn_library.clicked.connect(self.save_to_library)
        row2.addWidget(self.btn_library)
        self.btn_rename = QPushButton("Rename…")
        self.btn_rename.clicked.connect(self.rename_preset)
        row2.addWidget(self.btn_rename)
        self.btn_delete = QPushButton("Delete")
        self.btn_delete.setObjectName("danger")
        self.btn_delete.clicked.connect(self.delete_selected)
        row2.addWidget(self.btn_delete)
        self.btn_refresh = QPushButton("Refresh")
        self.btn_refresh.setToolTip("Re-read the presets folder from disk.")
        self.btn_refresh.clicked.connect(self.refresh)
        row2.addWidget(self.btn_refresh)
        row2.addStretch(1)
        lay.addLayout(row2)

        self.status = QLabel("—")
        self.status.setWordWrap(True)
        self.status.setObjectName("dim")
        self.status.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(self.status)

        # The tooltips a feature gate temporarily replaces, so it can put them
        # back (see _sync_buttons).
        self._tips = {btn: btn.toolTip()
                      for btn in (self.btn_save, self.btn_apply,
                                  self.btn_update)}
        self.refresh()

    # ------------------------------------------------------------- helpers

    def feature_reason(self):
        """Why presets are unavailable on the installed add-on, or None."""
        try:
            return self.bridge.feature_reason(FEATURE)
        except Exception:               # noqa: BLE001 — a dead bridge is routine
            return None                 # fail OPEN: unknown is not "missing"

    def _fail(self, exc):
        self.status.setStyleSheet("color: #e06c60;")
        self.status.setText(str(exc))
        if self.window is not None:
            self.window.update_bridge_status()

    def _ok(self, text):
        self.status.setStyleSheet("")
        self.status.setText(text)

    def current(self):
        item = self.list.currentItem()
        return None if item is None else item.data(Qt.UserRole)

    def _sync_buttons(self):
        """Selection gates the four that need one; the add-on gates the two
        that need Blender. A missing add-on never hides the LIST — the presets
        on disk are still yours, and still readable."""
        has = self.current() is not None
        self.btn_rename.setEnabled(has)
        self.btn_delete.setEnabled(has)
        # ⚠ NOT in the gated loop below. Writing a library item is pure disk
        # work on data already in hand, so it keeps working with Blender closed
        # or on an add-on too old to capture anything new.
        self.btn_library.setEnabled(has)
        reason = self.feature_reason()
        # ⚠ Both halves of this run every time. The add-on can be updated from
        # ⚙ Library Settings without restarting the app, so a cleared gate has
        # to hand the buttons BACK — enabled, with their own tooltips. Setting
        # only the disabled case left Save switched off for the rest of the
        # session, looking like the update had not worked.
        for btn, needs_pick in ((self.btn_save, False),
                                (self.btn_apply, True),
                                (self.btn_update, True)):
            if reason:
                btn.setEnabled(False)
                btn.setToolTip(reason)
            else:
                btn.setEnabled(has or not needs_pick)
                btn.setToolTip(self._tips[btn])
        if reason:
            self.status.setStyleSheet("color: %s;" % theme.TYPE_COLORS["mirror"])
            self.status.setText(reason)

    def _guarded(self):
        if self.window is not None and not self.window.bridge_free_for_tools():
            return False
        return True

    # -------------------------------------------------------------- the list

    def refresh(self, select=None):
        """Re-read the folder. `select` = a path to leave selected."""
        keep = select or self.current()
        self.list.clear()
        self._presets = list_presets()
        for data in self._presets:
            item = QListWidgetItem(data.get("name", "?"))
            item.setData(Qt.UserRole, data)
            item.setToolTip("%s\n%d setting(s), saved %s"
                            % (data.get("path", ""), count_settings(data),
                               data.get("saved", "—")))
            self.list.addItem(item)
            if keep is not None and data.get("path") == (
                    keep.get("path") if isinstance(keep, dict) else keep):
                self.list.setCurrentItem(item)
        if self.list.currentItem() is None and self.list.count():
            self.list.setCurrentRow(0)
        self._show_current()

    def _show_current(self):
        self.tree.clear()
        data = self.current()
        if data is None:
            self._sync_buttons()
            return
        for key, block in (data.get("groups") or {}).items():
            values = block.get("values") or {}
            node = QTreeWidgetItem(
                self.tree, ["%s  (%d)" % (block.get("label", key), len(values)),
                            ""])
            for path, value in values.items():
                QTreeWidgetItem(node, [path, _fmt(value)])
            node.setExpanded(True)
        head = "%s — %d setting(s)" % (data.get("name", "?"),
                                       count_settings(data))
        if data.get("engine"):
            head += ", saved on %s" % data["engine"]
        if data.get("blender"):
            head += " / Blender %s" % data["blender"]
        self._ok(head)
        self._sync_buttons()

    # ------------------------------------------------------------- the verbs

    def _capture(self):
        """Every group, so the save dialog can show real values before ticking."""
        if not self._guarded():
            return None
        try:
            return self.bridge.render_preset_capture()
        except bridgemod.BridgeError as exc:
            self._fail(exc)
            return None

    def save_current(self):
        captured = self._capture()
        if captured is None:
            return
        dialog = SavePresetDialog(self, captured)
        if dialog.exec() != QDialog.Accepted:
            return
        name = dialog.preset_name()
        existing = next((d for d in self._presets
                         if (d.get("name") or "").lower() == name.lower()), None)
        path = None
        if existing is not None:
            if QMessageBox.question(
                    self, "Save render preset",
                    "A preset called '%s' already exists. Replace it?" % name,
                    QMessageBox.Yes | QMessageBox.No,
                    QMessageBox.No) != QMessageBox.Yes:
                return
            path = existing.get("path")
        data = {"name": name,
                "blender": captured.get("blender"),
                "scene": captured.get("scene"),
                "engine": captured.get("engine"),
                "groups": dialog.groups_payload()}
        try:
            written = write_preset(data, path=path)
        except OSError as exc:
            self._fail("Could not write the preset: %s" % exc)
            return
        self.refresh(select=written)
        self._ok("Saved '%s' — %d setting(s)." % (name, count_settings(data)))

    def apply_preset(self):
        data = self.current()
        if data is None or not self._guarded():
            return
        if self.feature_reason():
            return
        try:
            report = self.bridge.render_preset_apply(data)
        except bridgemod.BridgeError as exc:
            self._fail(exc)
            return
        lines = ["'%s' applied — %s" % (data.get("name", "?"),
                                        report.get("summary", "done."))]
        for entry in (report.get("failed") or [])[:4]:
            lines.append("Refused: %s — %s" % (entry.get("path"),
                                               entry.get("reason")))
        if len(report.get("failed") or []) > 4:
            lines.append("…and %d more refused."
                         % (len(report["failed"]) - 4))
        if report.get("rejected"):
            lines.append("Ignored (not a render setting we know): %s."
                         % ", ".join(report["rejected"][:4]))
        self._ok("\n".join(lines))
        if self.window is not None:
            self.window.statusBar().showMessage(
                "Render preset '%s': %s" % (data.get("name", "?"),
                                            report.get("summary", "")), 6000)

    def update_preset(self):
        """Re-read the scene into this preset, keeping the groups it stores."""
        data = self.current()
        if data is None:
            return
        groups = list((data.get("groups") or {}).keys())
        if not self._guarded():
            return
        try:
            captured = self.bridge.render_preset_capture(groups=groups)
        except bridgemod.BridgeError as exc:
            self._fail(exc)
            return
        if QMessageBox.question(
                self, "Update render preset",
                "Replace the %d setting(s) in '%s' with the scene's current "
                "values?" % (count_settings(data), data.get("name", "?")),
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No) != QMessageBox.Yes:
            return
        fresh = dict(data)
        fresh.update({"blender": captured.get("blender"),
                      "scene": captured.get("scene"),
                      "engine": captured.get("engine"),
                      "groups": captured.get("groups") or {}})
        try:
            written = write_preset(fresh, path=data.get("path"))
        except OSError as exc:
            self._fail("Could not write the preset: %s" % exc)
            return
        self.refresh(select=written)
        self._ok("Updated '%s' from the scene — %d setting(s)."
                 % (fresh.get("name", "?"), count_settings(fresh)))

    def libraries(self):
        """The library roots to offer. The window's live config first, because
        a library added this session is not in the file on disk yet."""
        cfg = getattr(self.window, "cfg", None)
        if not isinstance(cfg, dict):
            cfg = config.load()
        return [lib for lib in (cfg.get("libraries") or []) if lib.get("path")]

    def save_to_library(self):
        """Copy the selected preset into a Studio Library as an item.

        ⚠ No bridge call: the preset is already JSON here, and a library write
        that needed Blender open would be a worse feature. The gate above does
        not apply to this button for the same reason.
        """
        data = self.current()
        if data is None:
            return
        libs = self.libraries()
        if not libs:
            self._fail("No library is configured — add one in the Studio "
                       "Library tab first.")
            return
        dialog = SaveToLibraryDialog(self, libs, name=data.get("name", ""))
        if dialog.exec() != QDialog.Accepted:
            return
        root, folder = dialog.target()
        name = dialog.preset_name()
        if dialog.exists() and QMessageBox.question(
                self, "Save to Studio Library",
                "'%s' already exists there. Replace it? The old one is kept "
                "under Versions…" % name,
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No) != QMessageBox.Yes:
            return
        try:
            path = write_library_item(root, folder, name, data, overwrite=True)
        except OSError as exc:
            self._fail("Could not write the library item: %s" % exc)
            return
        self._refresh_library(root)
        self._ok("Saved '%s' to the library — %s"
                 % (name, os.path.basename(path)))
        if self.window is not None:
            self.window.statusBar().showMessage(
                "Render preset '%s' saved to the Studio Library" % name, 6000)

    def _refresh_library(self, root):
        """Rescan the library tab this item landed in, so it appears at once."""
        tabs = getattr(self.window, "tabs", None)
        if tabs is None:
            return
        for i in range(tabs.count()):
            view = tabs.widget(i)
            cfg = getattr(view, "lib_cfg", None) or {}
            if cfg.get("path") == root and hasattr(view, "rescan"):
                view.rescan()

    def rename_preset(self):
        data = self.current()
        if data is None:
            return
        name, accepted = QInputDialog.getText(
            self, "Rename preset", "New name:", QLineEdit.Normal,
            data.get("name", ""))
        name = (name or "").strip()
        if not accepted or not name or name == data.get("name"):
            return
        fresh = dict(data)
        fresh["name"] = name
        try:
            written = write_preset(fresh, path=data.get("path"))
        except OSError as exc:
            self._fail("Could not write the preset: %s" % exc)
            return
        # The file keeps its old filename on purpose: renaming the file would
        # break nothing here, but it would surprise anyone who had the folder
        # open. The name that counts is the one inside.
        self.refresh(select=written)
        self._ok("Renamed to '%s'." % name)

    def delete_selected(self):
        data = self.current()
        if data is None:
            return
        if QMessageBox.question(
                self, "Delete preset",
                "Delete '%s'? The file is removed from disk." % data.get("name"),
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No) != QMessageBox.Yes:
            return
        if not delete_preset(data.get("path", "")):
            self._fail("Could not delete %s" % data.get("path"))
            return
        self.refresh()
        self._ok("Deleted '%s'." % data.get("name"))

    def set_capture_busy(self, busy):
        self.setEnabled(not busy)
