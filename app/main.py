"""MadihsonNSFW Toolset — external companion app for Blender.

Top-level tabs: "Studio Library" (the pose/anim/shape library, one inner tab
per library root) and "Rendering" (render/compositing tools — see rendering.py).

Run:  .venv\\Scripts\\pythonw.exe main.py        (or run_app.bat)
Smoke test (no window shown):  python main.py --smoke
"""

import json
import linecache
import math
import os
import re
import subprocess
import sys
import zipfile
import threading
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PySide6.QtCore import (QFileSystemWatcher, QObject, QSize, Qt, QTimer,
                            QUrl, Signal)
from PySide6.QtCore import QPointF
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtGui import (QAction, QColor, QDesktopServices, QFontMetrics,
                           QIcon, QPainter, QPainterPath, QPixmap)
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox,
                               QComboBox, QDialog, QDialogButtonBox,
                               QDoubleSpinBox, QFileDialog, QFormLayout,
                               QFrame, QGroupBox, QHBoxLayout, QInputDialog,
                               QLabel, QLineEdit, QListWidget, QListWidgetItem,
                               QMainWindow, QMenu, QMessageBox, QProgressBar,
                               QPushButton, QScrollArea, QSlider, QSpinBox,
                               QSplitter,
                               QStyle, QStyleOptionTab, QStylePainter, QTabBar,
                               QTabWidget, QTreeWidget, QTreeWidgetItem,
                               QVBoxLayout, QWidget)

import addon_push
import bridge as bridgemod
import chrome as chrome_mod
import config
import desktop
import dev_console
import devedit
import icons
import importer
import jiggle as jigglemod
import lastrender
import library
import madiref.tab as madiref_tab
import node_tools
import nodecanvas
import nsfw as nsfwmod
import optimizer as optimizermod
import physics as physicsmod
import picker as pickermod
import quadify as quadifymod
import render_presets
import render_tools
import rendering as renderingmod
import superfocus
import theme
import updates as updates_mod
import version
import widgets
from render_deck import util as render_deck_util
import grid as gridmod
import video_preview
from grid import ItemGrid
from panels import InfoPanel, SelectAllSpinBox, Sidebar

APP_NAME = "MadihsonNSFW Toolset"

# Outer tabs that get their own text colour, by section title. Marty's pick
# (2026-08-04, through Developer mode: edit).
# ⚠ This is CODE rather than QSS on purpose: Qt style sheets can address
# `::tab:first`, `:last` and `:selected` and nothing else, so a tab in the
# middle can only be coloured through setTabTextColor(). The tab BACKGROUNDS
# are in theme.py, where a selector does reach them.
TAB_TEXT_COLORS = {"Physics": "#ffffff"}

# What the left rail shows for each section: (icon name, group heading).
# ⚠ KEYED BY TAB TEXT, which is the app's stable internal key — see
# `widgets.SectionRail`. A section missing from here still gets a rail entry,
# just an unglyphed ungrouped one, so adding a tab can never leave the app
# unnavigable; the rail suite pins that every shipped tab IS listed.
# ⚠ THE GROUPS MUST STAY CONTIGUOUS IN TAB ORDER. The rail walks the tabs in
# index order and files each under its heading, so a group whose members are
# not neighbours would appear twice under the same name.
SECTION_META = {
    "Studio Library": ("library", ""),
    "Rendering": ("rendering", ""),
    "Bone picker": ("picker", "Animation"),
    "Anim Layers": ("anim_layers", "Animation"),
    "Node Setup": ("node_setup", "Nodes"),
    "Node Editor": ("nodeeditor", "Nodes"),
    "Texture Maps": ("texmaps", "Nodes"),
    "MadiRef": ("madiref", "Scene"),
    "Optimization": ("optimizer", "Scene"),
    "NSFW Tools": ("nsfw", "Scene"),
    "Physics": ("physics", "Scene"),
    "What's New": ("news", ""),
}

# The Anim Layers settings the app and Blender's N-panel both own. Kept in
# config.json here and in add-on preferences there, mirrored both ways.
# ⚠ Adding one means adding it to the add-on's MADILIB_Prefs AND to
# anim_layers_ui.shared_prefs()/apply_prefs(), or the same switch will quietly
# mean different things in the two UIs.
SHARED_LAYER_PREFS = ("sync_names", "auto_blend", "default_blend")
# How long after pushing our own copy we ignore what Blender reports back. A
# push and the 1.5 s status poll can cross, and the poll's older answer would
# undo the change the user just made.
PREFS_ECHO_GUARD_S = 3.0

# Bridge health poll. Slow right down once we know it's not answering: with
# the server off, a connect isn't refused, it's dropped (bridge.py), so each
# attempt costs real time — there's no point spending it every 5 seconds.
FAST_STATUS_MS = 5000
SLOW_STATUS_MS = 15000
SEARCH_DEBOUNCE_MS = 150   # one refilter per pause in typing, not per key
# How long "Check for updates" stays dead after a press. Marty's number, "so
# user can't spam the button" — and the server's update route is rate-limited
# per IP, so hammering it would earn a 429 rather than an answer.
UPDATE_CHECK_COOLDOWN_MS = 10000


def _swatch(color, size=12):
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(color))
    p.drawRoundedRect(0, 0, size, size, 3, 3)
    p.end()
    return QIcon(pm)


class CaptureWorker(QObject):
    """Runs capture_preview on a daemon thread so the app stays responsive.
    Blender itself is still busy playing the range through the viewport —
    only the app side is unblocked. Signals are emitted from the worker
    thread and auto-queued onto the Qt main thread."""

    done = Signal(object)   # bridge result dict
    failed = Signal(str)

    def __init__(self, bridge, path, frames, shape_steps=None, parent=None,
                 vgroups=False):
        super().__init__(parent)
        self.bridge = bridge
        self.path = path
        self.frames = frames
        self.shape_steps = shape_steps
        self.vgroups = vgroups

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        try:
            if self.vgroups:
                r = self.bridge.capture_vgroup_preview(self.path)
            else:
                r = self.bridge.capture_preview(self.path, frames=self.frames,
                                                shape_steps=self.shape_steps)
        except bridgemod.BridgeError as exc:
            self.failed.emit(str(exc))
        else:
            self.done.emit(r)


class BridgeWorker(QObject):
    """Runs ONE blocking bridge call on a daemon thread (same deal as
    CaptureWorker — Blender is busy either way, only the app stays alive).
    Used for alembic export/import, which can take a while on heavy caches."""

    done = Signal(object)
    failed = Signal(str)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self.fn = fn

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        try:
            r = self.fn()
        except bridgemod.BridgeError as exc:
            dev_console.BUFFER.add("ERROR", "Bridge call failed: %s" % exc)
            self.failed.emit(str(exc))
        except Exception as exc:      # noqa: BLE001 - a worker thread dying
            # silently is exactly the kind of "it just didn't work" the console
            # exists for; sys.excepthook doesn't cover threads.
            dev_console.BUFFER.add(
                "CRIT", "Unexpected error in a bridge worker:\n%s"
                % traceback.format_exc())
            self.failed.emit(str(exc))
        else:
            self.done.emit(r)


class VersionsDialog(widgets.GuardedDialog):
    """Version history of one item: every overwrite-save keeps the previous
    payload in versions/vNNN; Restore rolls back (current state is versioned
    first, the chosen version stays in the history)."""

    def __init__(self, view, item):
        super().__init__(view)
        self.view = view
        self.item = item
        self.setWindowTitle("Versions — %s  [%s]" % (item.name, item.type))
        self.resize(430, 400)
        lay = QVBoxLayout(self)
        self.listw = QListWidget()
        self.listw.setIconSize(QSize(64, 64))
        self.listw.itemDoubleClicked.connect(lambda _li: self.restore())
        lay.addWidget(self.listw, 1)
        row = QHBoxLayout()
        self.btn_restore = QPushButton("Restore")
        self.btn_restore.setObjectName("accent")
        self.btn_restore.clicked.connect(self.restore)
        self.btn_del = QPushButton("Delete Version")
        self.btn_del.setObjectName("danger")
        self.btn_del.clicked.connect(self.delete_version)
        btn_close = QPushButton("Close")
        btn_close.clicked.connect(self.reject)
        row.addWidget(self.btn_restore)
        row.addWidget(self.btn_del)
        row.addStretch(1)
        row.addWidget(btn_close)
        lay.addLayout(row)
        self.reload()

    def reload(self):
        self.listw.clear()
        versions = library.list_versions(self.item)
        for v in versions:
            icon = QIcon(v["thumbnail"]) if v["thumbnail"] else QIcon()
            li = QListWidgetItem(icon, "%s      %s" % (v["label"], v["created"]))
            li.setData(Qt.UserRole, v)
            self.listw.addItem(li)
        if versions:
            self.listw.setCurrentRow(0)
        else:
            self.listw.addItem("No previous versions — overwrite-saves create them")
        self.btn_restore.setEnabled(bool(versions))
        self.btn_del.setEnabled(bool(versions))

    def _selected(self):
        li = self.listw.currentItem()
        return li.data(Qt.UserRole) if li else None

    def restore(self):
        v = self._selected()
        if v is None:
            return
        if self.view.window.capturing:  # a capture may be writing this item's thumbnail
            QMessageBox.information(self, "Restore",
                                    "Blender is capturing a preview — try again "
                                    "when it finishes.")
            return
        answer = QMessageBox.question(
            self, "Restore version",
            "Roll '%s' back to %s (%s)?\nThe current state is kept as a new "
            "version first." % (self.item.name, v["label"], v["created"]),
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer != QMessageBox.Yes:
            return
        try:
            library.restore_version(self.item, v["dir"])
        except OSError as exc:
            QMessageBox.warning(self, "Restore", str(exc))
            return
        self.view.rescan()
        self.view.window.statusBar().showMessage(
            "Restored '%s' to %s" % (self.item.name, v["label"]), 6000)
        self.reload()

    def delete_version(self):
        v = self._selected()
        if v is None:
            return
        answer = QMessageBox.question(
            self, "Delete version",
            "Permanently delete %s (%s) of '%s'?" % (v["label"], v["created"],
                                                     self.item.name),
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer != QMessageBox.Yes:
            return
        import shutil
        try:
            shutil.rmtree(v["dir"])
        except OSError as exc:
            QMessageBox.warning(self, "Delete version", str(exc))
            return
        self.reload()


# The Alembic export options, in the order and grouping they are shown.
# (key, label, default, tooltip) — the default MUST match the add-on's
# `core.ABC_OPTIONS`, and `abc_export_test.py` reads both files to prove it,
# because a silent disagreement here means the dialog shows one thing and
# Blender does another.
#
# ⚠ These are BLENDER'S own defaults, with `selected` the one deliberate
# exception (this item type has always exported the selection).
ABC_OPTION_GROUPS = (
    ("Scope", (
        ("selected", "Selected objects only", True,
         "Off exports the whole scene. This has always been on for library "
         "items - turning it off is how you cache a whole set."),
        ("flatten", "Flatten hierarchy", False,
         "Drop parenting and write every object at the top level."),
        ("use_instancing", "Use instancing", True,
         "Write linked duplicates once and reference them. Smaller files; "
         "some applications read them badly."),
    )),
    ("Geometry", (
        ("uvs", "UV coordinates", True, ""),
        ("packuv", "Merge UVs", True,
         "Write shared UVs once instead of per face corner."),
        ("normals", "Normals", True, ""),
        ("vcolors", "Colour attributes", False, ""),
        ("orcos", "Generated coordinates", True,
         "The 'Generated' texture coordinate space."),
        ("face_sets", "Face sets", False,
         "Material assignments as Alembic face sets."),
        ("curves_as_mesh", "Curves as mesh", False,
         "Convert curve objects to meshes instead of writing curves."),
        ("export_custom_properties", "Custom properties", True, ""),
    )),
    ("Subdivision", (
        ("apply_subdiv", "Apply subdivision surface", False,
         "Write the SUBDIVIDED result. Much heavier, and it cannot be undone "
         "on the far side."),
        ("subdiv_schema", "Use subdivision schema", False,
         "Write the base cage plus a subdivision tag, so the reader "
         "subdivides. The opposite trade to the option above."),
    )),
    ("Particles", (
        ("export_hair", "Hair", True, ""),
        ("export_particles", "Particles", True, ""),
    )),
)
# The non-checkbox settings, built by hand below.
ABC_NUMBERS = {"global_scale": 1.0, "xsamples": 1, "gsamples": 1,
               "sh_open": 0.0, "sh_close": 1.0}
ABC_CHOICES = {"evaluation_mode": ("RENDER", "VIEWPORT"),
               "quad_method": ("BEAUTY", "FIXED", "FIXED_ALTERNATE",
                               "SHORTEST_DIAGONAL", "LONGEST_DIAGONAL"),
               "ngon_method": ("BEAUTY", "CLIP")}
ABC_CHOICE_DEFAULTS = {"evaluation_mode": "RENDER",
                       "quad_method": "SHORTEST_DIAGONAL",
                       "ngon_method": "BEAUTY"}
# Checkboxes that sit inside another group's box rather than in the list above.
ABC_EXTRA_DEFAULTS = {"triangulate": False}


def abc_defaults():
    """Every option at its default — the app's copy of `core.ABC_OPTIONS`.

    ⚠ Built from the four tables above and nothing else, because
    `abc_export_test.py` reads those four out of this file with `ast` and
    compares them against the add-on's. A default written straight into a
    widget would escape that check and the dialog would show one thing while
    Blender did another."""
    out = {}
    for _title, rows in ABC_OPTION_GROUPS:
        for key, _label, default, _tip in rows:
            out[key] = default
    out.update(ABC_NUMBERS)
    out.update(ABC_EXTRA_DEFAULTS)
    out.update(ABC_CHOICE_DEFAULTS)
    return out


class AbcExportDialog(widgets.GuardedDialog):
    """Options for Export Abc (Marty, 2026-08-05: "add some options for Export
    ABC in studio library").

    Mirrors Blender's own Alembic exporter panel rather than inventing a
    shorter list: every one of these is a real decision about the cache, and a
    missing one means dropping back to Blender's File menu, which is the thing
    this tab exists to avoid.

    ⚠ The choices are REMEMBERED (config.json, `abc_export`). An export dialog
    that resets every time is one you have to re-read every time; these are
    settings, not a question.

    ⚠ THE FRAME RANGE IS NOT ONE OF THEM. It lives here (Marty, 2026-08-05:
    "Scale, frame start frame end, ... and basically the rest") but it is
    deliberately kept out of `_widgets`, so it is neither saved into
    `abc_export` nor handed to `core.abc_options` — it is an argument to
    `save_abc`, not an operator keyword, and "which frames" is a decision about
    THIS export rather than a setting that should persist into the next one.
    """

    def __init__(self, parent, values, frame_start, frame_end,
                 scene_range=None):
        super().__init__(parent)
        self.setWindowTitle("Export Abc")
        self.resize(430, 660)
        self._widgets = {}
        outer = QVBoxLayout(self)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        lay = QVBoxLayout(body)
        lay.setContentsMargins(0, 0, 6, 0)

        # ⚠ `frame_start`/`frame_end` arrive as None when the sidebar boxes are
        # blank, which is their DEFAULT state — the old header formatted them
        # with %d and blew up before the dialog could open.
        box = QGroupBox("Frames")
        form = QFormLayout(box)
        self.chk_scene_range = QCheckBox("Use the scene's frame range")
        self.chk_scene_range.setToolTip(
            "Export whatever Blender's own Start/End are set to at the moment "
            "the export runs, instead of pinning a range here.")
        form.addRow(self.chk_scene_range)
        lo, hi = scene_range if scene_range else (1, 250)
        self.frame_start = QSpinBox()
        self.frame_start.setRange(-100000, 100000)
        self.frame_end = QSpinBox()
        self.frame_end.setRange(-100000, 100000)
        self.frame_start.setValue(int(frame_start if frame_start is not None
                                      else lo))
        self.frame_end.setValue(int(frame_end if frame_end is not None else hi))
        form.addRow("Start", self.frame_start)
        form.addRow("End", self.frame_end)

        def _sync_range(on):
            for widget in (self.frame_start, self.frame_end):
                widget.setEnabled(not on)
                label = form.labelForField(widget)
                if label is not None:
                    label.setEnabled(not on)
        self.chk_scene_range.toggled.connect(_sync_range)
        self.chk_scene_range.setChecked(frame_start is None
                                        and frame_end is None)
        _sync_range(self.chk_scene_range.isChecked())
        if scene_range:
            hint = QLabel("The scene is %d - %d." % (lo, hi))
            hint.setObjectName("dim")
            form.addRow(hint)
        lay.addWidget(box)

        for title, rows in ABC_OPTION_GROUPS:
            box = QGroupBox(title)
            inner = QVBoxLayout(box)
            for key, label, default, tip in rows:
                chk = QCheckBox(label)
                chk.setChecked(bool(values.get(key, default)))
                if tip:
                    chk.setToolTip(tip)
                inner.addWidget(chk)
                self._widgets[key] = chk
            lay.addWidget(box)

        box = QGroupBox("Evaluate as")
        inner = QVBoxLayout(box)
        combo = widgets.NoScrollComboBox()
        combo.addItem("Render settings", "RENDER")
        combo.addItem("Viewport settings", "VIEWPORT")
        combo.setToolTip(
            "Which modifier visibility the cache is made from. Render is "
            "Blender's default and usually what you want; Viewport matches "
            "what is on screen, subdivision levels included.")
        combo.setCurrentIndex(max(0, combo.findData(
            values.get("evaluation_mode", "RENDER"))))
        inner.addWidget(combo)
        self._widgets["evaluation_mode"] = combo
        lay.addWidget(box)

        box = QGroupBox("Transform")
        form = QFormLayout(box)
        scale = QDoubleSpinBox()
        scale.setRange(0.0001, 1000.0)
        scale.setDecimals(4)
        scale.setValue(float(values.get("global_scale", 1.0)))
        form.addRow("Scale", scale)
        self._widgets["global_scale"] = scale
        tri = QCheckBox("Triangulate")
        tri.setChecked(bool(values.get("triangulate", False)))
        form.addRow(tri)
        self._widgets["triangulate"] = tri
        quad = widgets.NoScrollComboBox()
        for name in ABC_CHOICES["quad_method"]:
            quad.addItem(name.replace("_", " ").title(), name)
        quad.setCurrentIndex(max(0, quad.findData(
            values.get("quad_method", "SHORTEST_DIAGONAL"))))
        form.addRow("Quad method", quad)
        self._widgets["quad_method"] = quad
        ngon = widgets.NoScrollComboBox()
        for name in ABC_CHOICES["ngon_method"]:
            ngon.addItem(name.title(), name)
        ngon.setCurrentIndex(max(0, ngon.findData(
            values.get("ngon_method", "BEAUTY"))))
        form.addRow("N-gon method", ngon)
        self._widgets["ngon_method"] = ngon
        # ⚠ The two method pickers only mean anything while Triangulate is on,
        # and a live-looking control that does nothing is worse than a greyed
        # one (docs\app-shell.md - and Qt will not grey a form's LABEL with its
        # field, so both are done by hand).
        def _sync_tri(on):
            for widget in (quad, ngon):
                widget.setEnabled(on)
                label = form.labelForField(widget)
                if label is not None:
                    label.setEnabled(on)
        tri.toggled.connect(_sync_tri)
        _sync_tri(tri.isChecked())
        lay.addWidget(box)

        box = QGroupBox("Sampling")
        form = QFormLayout(box)
        for key, label, tip in (
                ("xsamples", "Transform samples",
                 "Sub-frame samples of object transforms. Above 1 is for "
                 "motion blur on the far side."),
                ("gsamples", "Geometry samples",
                 "Sub-frame samples of the deforming mesh. Multiplies the "
                 "file size.")):
            spin = QSpinBox()
            spin.setRange(1, 128)
            spin.setValue(int(values.get(key, 1)))
            spin.setToolTip(tip)
            form.addRow(label, spin)
            self._widgets[key] = spin
        for key, label, default in (("sh_open", "Shutter open", 0.0),
                                    ("sh_close", "Shutter close", 1.0)):
            spin = QDoubleSpinBox()
            spin.setRange(-1.0, 1.0)
            spin.setSingleStep(0.1)
            spin.setValue(float(values.get(key, default)))
            form.addRow(label, spin)
            self._widgets[key] = spin
        lay.addWidget(box)

        lay.addStretch(1)
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)

        row = QHBoxLayout()
        btn_export = QPushButton("Export")
        btn_export.setObjectName("accent")
        btn_export.clicked.connect(self.accept)
        row.addWidget(btn_export)
        btn_reset = QPushButton("Blender defaults")
        btn_reset.setToolTip("Put every option back to what Blender's own "
                             "Alembic exporter uses.")
        btn_reset.clicked.connect(self.reset_defaults)
        row.addWidget(btn_reset)
        row.addStretch(1)
        btn_cancel = QPushButton("Cancel")
        btn_cancel.clicked.connect(self.reject)
        row.addWidget(btn_cancel)
        outer.addLayout(row)

    def reset_defaults(self):
        self.apply_values(abc_defaults())

    def apply_values(self, values):
        for key, widget in self._widgets.items():
            if key not in values:
                continue
            value = values[key]
            if isinstance(widget, QCheckBox):
                widget.setChecked(bool(value))
            elif isinstance(widget, QComboBox):
                widget.setCurrentIndex(max(0, widget.findData(value)))
            elif isinstance(widget, QSpinBox):
                widget.setValue(int(value))
            else:
                widget.setValue(float(value))

    def values(self):
        out = {}
        for key, widget in self._widgets.items():
            if isinstance(widget, QCheckBox):
                out[key] = widget.isChecked()
            elif isinstance(widget, QComboBox):
                out[key] = widget.currentData()
            elif isinstance(widget, QSpinBox):
                out[key] = widget.value()
            else:
                out[key] = round(widget.value(), 4)
        return out

    def frames(self):
        """(start, end), or (None, None) for "whatever the scene is set to".

        None is passed straight through to `save_abc`, which reads the scene
        range at export time — so ticking the box means the range is decided in
        Blender when the export runs, not captured here.
        """
        if self.chk_scene_range.isChecked():
            return None, None
        start, end = self.frame_start.value(), self.frame_end.value()
        return (end, start) if end < start else (start, end)


class SaveAnimDialog(widgets.GuardedDialog):
    """Options for Save Anim (Marty, 2026-08-05: "The menu for animation export
    settings should be opened after clicking 'Save anim', including the frame
    range").

    Same shape as the Export Abc dialog on purpose: the button opens the
    settings, the settings are remembered (config.json, `anim_export`), and the
    FRAME RANGE lives here but is deliberately NOT remembered — which frames
    you exported is a fact about one export, not a preference.
    """

    def __init__(self, parent, values, frame_start, frame_end,
                 scene_range=None, layer_warning=None):
        super().__init__(parent)
        self.setWindowTitle("Save Anim")
        self.resize(430, 0)
        outer = QVBoxLayout(self)

        # ⚠ THE WARNING GOES FIRST, above the settings. It is the reason to
        # close this dialog and go and do something else, so it must be read
        # before the tickboxes, not found under them after pressing Save.
        if layer_warning:
            warn = QLabel("⚠ " + layer_warning)
            warn.setWordWrap(True)
            warn.setStyleSheet("color: %s;" % theme.WARN)
            outer.addWidget(warn)

        box = QGroupBox("Frames")
        form = QFormLayout(box)
        lo, hi = scene_range if scene_range else (1, 250)
        self.frame_start = QSpinBox()
        self.frame_start.setRange(-100000, 100000)
        self.frame_end = QSpinBox()
        self.frame_end.setRange(-100000, 100000)
        # ⚠ "If the frame range is not set by user it should be default
        # timeline" (Marty). So the boxes arrive holding the scene's REAL
        # numbers, not a blank that silently means the same thing — the
        # difference is whether you can see what you are about to export.
        self.frame_start.setValue(int(frame_start if frame_start is not None
                                      else lo))
        self.frame_end.setValue(int(frame_end if frame_end is not None else hi))
        form.addRow("Start", self.frame_start)
        form.addRow("End", self.frame_end)
        hint = QLabel("The scene's timeline is %d - %d." % (lo, hi))
        hint.setObjectName("dim")
        form.addRow(hint)
        outer.addWidget(box)

        box = QGroupBox("What to store")
        inner = QVBoxLayout(box)
        self.chk_bake = QCheckBox("Bake every frame")
        self.chk_bake.setToolTip(
            "Sample the evaluated pose on every frame of the range — captures "
            "IK/constraint motion as plain keys.\nGraph-editor handles and "
            "F-modifiers are not kept: a baked curve has a key on every frame, "
            "so there is nothing left for them to shape.")
        self.chk_bake.setChecked(bool(values.get("bake", False)))
        inner.addWidget(self.chk_bake)

        self.chk_modifiers = QCheckBox("Keep F-curve modifiers and graph-editor data")
        self.chk_modifiers.setToolTip(
            "Store each curve's F-modifiers (Noise, Cycles, Generator…) along "
            "with the handles, easing and key types, so the animation looks "
            "the same in the graph editor when it comes back.")
        self.chk_modifiers.setChecked(bool(values.get("keep_modifiers", True)))
        inner.addWidget(self.chk_modifiers)

        self.chk_props = QCheckBox("Inherit every bone property")
        self.chk_props.setToolTip(
            "Also store each saved bone's custom properties — IK/FK switches, "
            "space switches, anything the rig exposes on a bone.\n"
            "Animated properties were always saved as curves; this is for the "
            "ones nobody keyframed, which the animation still depends on.")
        self.chk_props.setChecked(bool(values.get("include_props", False)))
        inner.addWidget(self.chk_props)
        outer.addWidget(box)

        # ⚠ Baking DESTROYS what "keep modifiers" would keep, so the tickbox
        # greys out rather than sitting there claiming otherwise. Its value is
        # left alone underneath, so unticking Bake restores the choice.
        def _sync_bake(on):
            self.chk_modifiers.setEnabled(not on)
            self.chk_modifiers.setText(
                "Keep F-curve modifiers and graph-editor data"
                + ("  — baking replaces them with keys" if on else ""))
        self.chk_bake.toggled.connect(_sync_bake)
        _sync_bake(self.chk_bake.isChecked())

        row = QHBoxLayout()
        btn_save = QPushButton("Save Anim")
        btn_save.setObjectName("accent")
        btn_save.clicked.connect(self.accept)
        row.addWidget(btn_save)
        row.addStretch(1)
        btn_cancel = QPushButton("Cancel")
        btn_cancel.clicked.connect(self.reject)
        row.addWidget(btn_cancel)
        outer.addLayout(row)

    def frames(self):
        start, end = self.frame_start.value(), self.frame_end.value()
        return (end, start) if end < start else (start, end)

    def values(self):
        """⚠ `keep_modifiers` reports FALSE while baking, whatever the greyed
        tickbox holds. The add-on decides the same thing independently, but the
        two must not disagree about what the item contains — the badge on the
        tile is drawn from this."""
        bake = self.chk_bake.isChecked()
        return {"bake": bake,
                "keep_modifiers": self.chk_modifiers.isChecked() and not bake,
                "include_props": self.chk_props.isChecked()}


class SaveShapesDialog(widgets.GuardedDialog):
    """Shape Key Vault save checklist: pick which keys of the selected meshes
    to store; optionally delete them from the mesh afterwards (vault move).

    Marty's 2026-08-04 additions: a search box, two exclusion filters, and the
    option to save every checked key as its OWN library item rather than one
    item holding all of them.

    ⚠ A FILTER UNTICKS, DISABLES *AND* HIDES — in that order. If excluding
    driven keys only unticked them, nothing would stop them being ticked
    straight back on while the filter still claimed to be excluding them; if it
    only hid them, a ticked row would be saved from off screen. Turning a
    filter off hands the keys back.
    """

    def __init__(self, view, listing):
        super().__init__(view)
        self.setWindowTitle("Save Shape Keys")
        self.resize(430, 560)
        lay = QVBoxLayout(self)

        self.search = QLineEdit()
        self.search.setPlaceholderText("Search shape keys…")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._refilter)
        lay.addWidget(self.search)

        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        for mesh in listing:
            node = QTreeWidgetItem(["%s  (%d verts)" % (mesh["object"],
                                                        mesh["verts"])])
            node.setFlags(node.flags() | Qt.ItemIsAutoTristate
                          | Qt.ItemIsUserCheckable)
            node.setData(0, Qt.UserRole, mesh["object"])
            self.tree.addTopLevelItem(node)
            for k in mesh["keys"]:
                if k["is_basis"]:
                    continue
                label = k["name"]
                if k["has_driver"]:
                    label += "   ⚠ driver"
                if k.get("has_animation"):
                    label += "   ● animated"
                child = QTreeWidgetItem([label])
                child.setFlags(child.flags() | Qt.ItemIsUserCheckable)
                child.setCheckState(0, Qt.Checked)
                child.setData(0, Qt.UserRole, k["name"])
                # Kept on the row so the filters can answer without going back
                # to the listing.
                child.setData(0, Qt.UserRole + 1, bool(k["has_driver"]))
                child.setData(0, Qt.UserRole + 2, bool(k.get("has_animation")))
                node.addChild(child)
        self.tree.expandAll()
        lay.addWidget(self.tree, 1)

        # ⚠ ON by default, and that is Marty's call: a driven key's value is
        # decided by something else, and the driver is NOT rebuilt when the
        # keys go back — so vaulting one is usually a mistake.
        self.chk_skip_drivers = QCheckBox("Exclude keys that have drivers")
        self.chk_skip_drivers.setChecked(True)
        self.chk_skip_drivers.toggled.connect(self._refilter)
        lay.addWidget(self.chk_skip_drivers)
        # OFF by default: an animated key is still a perfectly good shape, and
        # its animation is a reason to be careful rather than a reason to skip.
        self.chk_skip_animated = QCheckBox("Exclude keys that are already "
                                           "animated")
        self.chk_skip_animated.toggled.connect(self._refilter)
        lay.addWidget(self.chk_skip_animated)

        note = QLabel("⚠ driver = the key's value has a driver. The driver is "
                      "stored as text for reference but NOT rebuilt when the "
                      "keys are added back.")
        note.setObjectName("dim")
        note.setWordWrap(True)
        lay.addWidget(note)

        self.chk_separate = QCheckBox(
            "Save each key as its own library item")
        self.chk_separate.setToolTip(
            "Instead of one item holding every checked key, write one item per "
            "key — each with its own preview. Slower, because each one is "
            "captured separately, and it shows a progress bar while it runs.")
        lay.addWidget(self.chk_separate)
        self.chk_delete = QCheckBox("Delete saved keys from mesh (slims the .blend)")
        lay.addWidget(self.chk_delete)
        self.count = QLabel("")
        self.count.setObjectName("dim")
        lay.addWidget(self.count)
        self._refilter()
        row = QHBoxLayout()
        btn_save = QPushButton("Save")
        btn_save.setObjectName("accent")
        btn_save.clicked.connect(self.accept)
        btn_cancel = QPushButton("Cancel")
        btn_cancel.clicked.connect(self.reject)
        row.addWidget(btn_save)
        row.addStretch(1)
        row.addWidget(btn_cancel)
        lay.addLayout(row)

    def _excluded(self, child):
        """Why this key is out of bounds right now, or None."""
        if self.chk_skip_drivers.isChecked() and child.data(0, Qt.UserRole + 1):
            return "has a driver"
        if (self.chk_skip_animated.isChecked()
                and child.data(0, Qt.UserRole + 2)):
            return "is already animated"
        return None

    def _refilter(self, *_):
        """Apply the search and the two exclusions to every row.

        ⚠ Search HIDES, exclusions REMOVE, and they are not the same thing. A
        row hidden by a search is still going to be saved — it is just not on
        screen, and silently dropping it because of what is typed in a search
        box would be a trap. A row an exclusion has taken away is unticked AND
        off the list, which is what "exclude" means.

        ⚠ An excluded row is unticked and disabled BEFORE it is hidden, not
        merely hidden (Marty, 2026-08-05: "make the items in list dissapear if
        they are excluded"). Hiding alone would leave a ticked row off screen
        that still went into `selection()` — the exact trap the search comment
        above describes, arrived at from the other direction.
        """
        needle = self.search.text().strip().lower()
        kept = 0
        excluded = 0
        for i in range(self.tree.topLevelItemCount()):
            node = self.tree.topLevelItem(i)
            shown_children = 0
            for j in range(node.childCount()):
                child = node.child(j)
                name = (child.data(0, Qt.UserRole) or "").lower()
                child.setHidden(bool(needle) and needle not in name)
                reason = self._excluded(child)
                if reason:
                    # ⚠ Remember whether the user had it ticked BEFORE the
                    # filter took it away, so turning the filter off can put it
                    # back. Without this, flicking a filter on and off silently
                    # emptied the checklist and the next Save wrote nothing.
                    if child.data(0, Qt.UserRole + 3) is None:
                        child.setData(0, Qt.UserRole + 3,
                                      child.checkState(0) == Qt.Checked)
                    child.setCheckState(0, Qt.Unchecked)
                    child.setFlags(child.flags() & ~Qt.ItemIsEnabled)
                    child.setToolTip(0, "Excluded: this key %s." % reason)
                    child.setHidden(True)
                    excluded += 1
                else:
                    was = child.data(0, Qt.UserRole + 3)
                    if was is not None:
                        # Restore only what the FILTER changed. A key the user
                        # unticked by hand stays unticked — this must not
                        # override a decision they made themselves.
                        child.setCheckState(0,
                                            Qt.Checked if was else Qt.Unchecked)
                        child.setData(0, Qt.UserRole + 3, None)
                    child.setFlags(child.flags() | Qt.ItemIsEnabled
                                   | Qt.ItemIsUserCheckable)
                    child.setToolTip(0, "")
                    if child.checkState(0) == Qt.Checked:
                        kept += 1
                if not child.isHidden():
                    shown_children += 1
            # A mesh with nothing left on screen is noise — whether the search
            # or an exclusion emptied it.
            node.setHidden(shown_children == 0)
        # ⚠ The excluded count is not decoration. On a DAZ figure nearly every
        # key is driven, so "exclude driven" (on by default) empties the list —
        # without a number saying where they went, a working dialog reads as a
        # broken one.
        self.count.setText("%d key(s) will be saved" % kept
                           + (" — %d hidden by the filters below" % excluded
                              if excluded else ""))
        return kept

    def selection(self):
        """{object_name: [checked key names]} — objects with no checks omitted.

        Reads the CHECK STATE, never the hidden flag, and that is what makes
        both filters safe: a key the search scrolled away is still ticked and
        still saved (a search FINDS keys, it does not choose them), while a key
        an exclusion took away was unticked before it was hidden.
        """
        out = {}
        for i in range(self.tree.topLevelItemCount()):
            node = self.tree.topLevelItem(i)
            names = [node.child(j).data(0, Qt.UserRole)
                     for j in range(node.childCount())
                     if node.child(j).checkState(0) == Qt.Checked]
            if names:
                out[node.data(0, Qt.UserRole)] = names
        return out


class ImportDialog(widgets.GuardedDialog):
    """Bring items INTO the library: files, folders, or a zip of many.

    Marty, 2026-08-05: *"this should be main way users can import things to
    studio library"*. So it takes whatever it is handed — a share zip, a folder
    of items, a pile of loose .abc files — and shows exactly what it found
    before it copies anything. `importer.py` does the work and knows nothing
    about item TYPES, which is why a type added later needs no work here.
    """

    FILTER = ("Library items and archives (*.zip *.abc *.mp4);;"
              "Zip archives (*.zip);;All files (*)")

    def __init__(self, parent, root, folders, dest_folder=""):
        super().__init__(parent)
        self.setWindowTitle("Import into the library")
        self.resize(620, 480)
        self.root = root
        self._sources = []
        self.candidates = []
        self.ignored = []

        lay = QVBoxLayout(self)
        lay.setSpacing(8)

        blurb = QLabel(
            "Add a zip somebody shared with you, a folder of items, or the "
            "items themselves. Everything they carry — thumbnails, previews, "
            "tags, versions — comes with them.")
        blurb.setWordWrap(True)
        blurb.setObjectName("dim")
        lay.addWidget(blurb)

        row = QHBoxLayout()
        btn_files = QPushButton("Add files…")
        btn_files.clicked.connect(self.add_files)
        row.addWidget(btn_files)
        btn_folder = QPushButton("Add folder…")
        btn_folder.clicked.connect(self.add_folder)
        row.addWidget(btn_folder)
        btn_clear = QPushButton("Clear")
        btn_clear.clicked.connect(self.clear)
        row.addWidget(btn_clear)
        row.addStretch(1)
        row.addWidget(QLabel("Into"))
        self.dest = QComboBox()
        self.dest.addItem("(library root)", "")
        for rel in folders:
            self.dest.addItem(rel, rel)
        index = self.dest.findData(dest_folder or "")
        self.dest.setCurrentIndex(index if index >= 0 else 0)
        self.dest.currentIndexChanged.connect(self._fill)
        row.addWidget(self.dest)
        lay.addLayout(row)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Name", "Type", "Goes to"])
        self.tree.setColumnWidth(0, 260)
        self.tree.setColumnWidth(1, 90)
        self.tree.setSelectionMode(QAbstractItemView.NoSelection)
        lay.addWidget(self.tree, 1)

        self.note = QLabel("Nothing added yet.")
        self.note.setWordWrap(True)
        self.note.setObjectName("dim")
        lay.addWidget(self.note)

        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok
                                        | QDialogButtonBox.Cancel)
        self.buttons.button(QDialogButtonBox.Ok).setText("Import")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        lay.addWidget(self.buttons)
        self._fill()

    # ------------------------------------------------------------------

    def add_files(self):
        paths, _f = QFileDialog.getOpenFileNames(
            self, "Add files to import", "", self.FILTER)
        self.add(paths)

    def add_folder(self):
        path = QFileDialog.getExistingDirectory(self, "Add a folder to import")
        if path:
            self.add([path])

    def add(self, paths):
        for p in paths:
            if p and p not in self._sources:
                self._sources.append(p)
        self._fill()

    def clear(self):
        self._sources = []
        self._fill()

    def dest_folder(self):
        return self.dest.currentData() or ""

    def _fill(self):
        self.candidates, self.ignored = importer.scan(self._sources)
        self.tree.clear()
        dest = self.dest_folder()
        for cand in self.candidates:
            where = "/".join(x for x in (dest, cand.relfolder) if x) or "(root)"
            node = QTreeWidgetItem(self.tree,
                                   [cand.name, cand.type or "file", where])
            node.setIcon(0, gridmod.type_icon(cand.type, 14)
                         if cand.type else QIcon())
        if not self._sources:
            self.note.setText("Nothing added yet.")
        elif not self.candidates:
            self.note.setText("Nothing importable found. "
                              + "; ".join("%s — %s" % (os.path.basename(p), w)
                                          for p, w in self.ignored[:3]))
        else:
            text = "%d item(s) ready." % len(self.candidates)
            if self.ignored:
                text += ("  Ignoring %d thing(s): %s"
                         % (len(self.ignored),
                            "; ".join("%s — %s" % (os.path.basename(p), w)
                                      for p, w in self.ignored[:2])))
            self.note.setText(text)
        ok = self.buttons.button(QDialogButtonBox.Ok)
        if ok is not None:
            ok.setEnabled(bool(self.candidates))


class SaveVGroupsDialog(widgets.GuardedDialog):
    """Which vertex groups of the selected meshes go into a .vgroups item.

    Marty, 2026-08-05: "an option menu to select witch vertex paint to export
    but also an ability to export multiple vertex paints". Saving every group
    on the mesh was the only option before, which on a rigged character means
    a hundred-odd bone groups when what was wanted was the three that were
    just painted.

    ⚠ Everything starts TICKED, so pressing Save without reading anything
    writes exactly what the old button wrote. A dialog that quietly changes
    what an existing button does is worse than no dialog.
    """

    def __init__(self, view, listing):
        super().__init__(view)
        self.setWindowTitle("Save Vertex Groups")
        self.resize(400, 520)
        lay = QVBoxLayout(self)

        self.search = QLineEdit()
        self.search.setPlaceholderText("Search vertex groups…")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._refilter)
        lay.addWidget(self.search)

        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        for mesh in listing:
            node = QTreeWidgetItem(["%s  (%d verts)" % (mesh["object"],
                                                        mesh["verts"])])
            node.setFlags(node.flags() | Qt.ItemIsAutoTristate
                          | Qt.ItemIsUserCheckable)
            node.setData(0, Qt.UserRole, mesh["object"])
            self.tree.addTopLevelItem(node)
            for name in mesh.get("groups") or []:
                child = QTreeWidgetItem([name])
                child.setFlags(child.flags() | Qt.ItemIsUserCheckable)
                child.setCheckState(0, Qt.Checked)
                child.setData(0, Qt.UserRole, name)
                node.addChild(child)
        self.tree.expandAll()
        lay.addWidget(self.tree, 1)

        row = QHBoxLayout()
        btn_all = QPushButton("Select all")
        btn_all.clicked.connect(lambda: self._set_all(True))
        btn_none = QPushButton("Select none")
        btn_none.clicked.connect(lambda: self._set_all(False))
        row.addWidget(btn_all)
        row.addWidget(btn_none)
        row.addStretch(1)
        lay.addLayout(row)

        note = QLabel("The preview is a weight-paint shot of each group, so "
                      "several groups make a thumbnail you can hover to play "
                      "through them.")
        note.setObjectName("dim")
        note.setWordWrap(True)
        lay.addWidget(note)

        # Marty, 2026-08-05: "make sure to have an option to export individually
        # too (and not all in one file)". Same shape as the shape key dialog's
        # batch option, and OFF for the same reason — one item holding the set
        # is what this button has always written.
        self.chk_separate = QCheckBox("Save each group as its own library item")
        self.chk_separate.setToolTip(
            "Instead of one item holding every ticked group, write one item "
            "per group — each with its own weight-paint preview. Slower, "
            "because each one is captured separately, and it shows a progress "
            "bar while it runs.")
        lay.addWidget(self.chk_separate)

        self.count = QLabel("")
        self.count.setObjectName("dim")
        lay.addWidget(self.count)

        row = QHBoxLayout()
        self.btn_save = QPushButton("Save")
        self.btn_save.setObjectName("accent")
        self.btn_save.clicked.connect(self.accept)
        btn_cancel = QPushButton("Cancel")
        btn_cancel.clicked.connect(self.reject)
        row.addWidget(self.btn_save)
        row.addStretch(1)
        row.addWidget(btn_cancel)
        lay.addLayout(row)

        # After the button exists — _update_count enables/disables it.
        self.tree.itemChanged.connect(self._update_count)
        self._update_count()

    def _set_all(self, on):
        """⚠ Ignores the search: these say ALL, and a hidden row is still a
        row. Buttons that silently mean "all the ones you can see" are how a
        group goes missing from an item nobody thinks to check."""
        state = Qt.Checked if on else Qt.Unchecked
        for i in range(self.tree.topLevelItemCount()):
            node = self.tree.topLevelItem(i)
            for j in range(node.childCount()):
                node.child(j).setCheckState(0, state)

    def _refilter(self, *_):
        """The search HIDES, exactly as in the shape key dialog — a hidden
        group is still ticked and still saved."""
        needle = self.search.text().strip().lower()
        for i in range(self.tree.topLevelItemCount()):
            node = self.tree.topLevelItem(i)
            shown = 0
            for j in range(node.childCount()):
                child = node.child(j)
                name = (child.data(0, Qt.UserRole) or "").lower()
                child.setHidden(bool(needle) and needle not in name)
                if not child.isHidden():
                    shown += 1
            node.setHidden(bool(needle) and shown == 0)

    def _update_count(self, *_):
        n = sum(len(v) for v in self.selection().values())
        self.count.setText("%d group(s) will be saved" % n)
        self.btn_save.setEnabled(bool(n))

    def selection(self):
        """{object_name: [checked group names]} — objects with no checks
        omitted, which is what `core.save_vgroups` expects for `groups`."""
        out = {}
        for i in range(self.tree.topLevelItemCount()):
            node = self.tree.topLevelItem(i)
            names = [node.child(j).data(0, Qt.UserRole)
                     for j in range(node.childCount())
                     if node.child(j).checkState(0) == Qt.Checked]
            if names:
                out[node.data(0, Qt.UserRole)] = names
        return out


class SaveRemapDialog(widgets.GuardedDialog):
    """Rig-to-rig remap builder: pick a SOURCE (library item or scene armature),
    auto-match its bone names onto the TARGET (active) armature, hand-assign
    the leftovers, save as a .remap item. Rules run in order BEFORE matching;
    at apply time the saved map wins over the rules."""

    OPS = (("Strip prefix", "prefix_strip"), ("Add prefix", "prefix_add"),
           ("Find → replace", "replace"))
    UNMATCHED = "— unmatched —"

    def __init__(self, view, sources, target_label):
        super().__init__(view)
        self.view = view
        self.bridge = view.bridge
        self._match = None      # last build_remap result
        self._rule_rows = []    # (row_widget, op_combo, edit1, edit2)
        self.setWindowTitle("Save Remap — target: %s" % target_label)
        self.resize(560, 620)
        lay = QVBoxLayout(self)

        srow = QHBoxLayout()
        lab = QLabel("Source")
        lab.setObjectName("dim")
        srow.addWidget(lab)
        self.source_combo = QComboBox()
        for label, payload in sources:
            self.source_combo.addItem(label, payload)
        srow.addWidget(self.source_combo, 1)
        lay.addLayout(srow)

        rules_lab = QLabel("Rules (run in order before matching — e.g. strip a "
                           "'DEF-' prefix):")
        rules_lab.setObjectName("dim")
        lay.addWidget(rules_lab)
        self.rules_lay = QVBoxLayout()
        lay.addLayout(self.rules_lay)
        rrow = QHBoxLayout()
        btn_rule = QPushButton("+ Rule")
        btn_rule.setObjectName("flat")
        btn_rule.clicked.connect(self.add_rule)
        btn_match = QPushButton("Re-Match")
        btn_match.setToolTip("Re-run the auto-match with the current source "
                             "and rules")
        btn_match.clicked.connect(self.rematch)
        rrow.addWidget(btn_rule)
        rrow.addWidget(btn_match)
        rrow.addStretch(1)
        lay.addLayout(rrow)

        self.match_label = QLabel("—")
        lay.addWidget(self.match_label)
        unm_lab = QLabel("Unmatched — pick a target bone by hand (or leave "
                         "unmatched):")
        unm_lab.setObjectName("dim")
        lay.addWidget(unm_lab)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Source bone", "Target bone"])
        self.tree.setRootIsDecorated(False)
        self.tree.setColumnWidth(0, 250)
        lay.addWidget(self.tree, 1)

        brow = QHBoxLayout()
        self.btn_save = QPushButton("Save")
        self.btn_save.setObjectName("accent")
        self.btn_save.setEnabled(False)
        self.btn_save.clicked.connect(self.accept)
        btn_cancel = QPushButton("Cancel")
        btn_cancel.clicked.connect(self.reject)
        brow.addWidget(self.btn_save)
        brow.addStretch(1)
        brow.addWidget(btn_cancel)
        lay.addLayout(brow)

        self.source_combo.currentIndexChanged.connect(lambda *_: self.rematch())
        self.rematch()

    # ---- rules

    def add_rule(self):
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        op = QComboBox()
        for label, _key in self.OPS:
            op.addItem(label)
        e1 = QLineEdit()
        e1.setPlaceholderText("prefix")
        e2 = QLineEdit()
        e2.setPlaceholderText("replace with")
        e2.hide()
        op.currentIndexChanged.connect(
            lambda i: (e2.setVisible(i == 2),
                       e1.setPlaceholderText("find" if i == 2 else "prefix")))
        btn_x = QPushButton("✕")
        btn_x.setObjectName("flat")
        entry = (row, op, e1, e2)
        btn_x.clicked.connect(lambda: self.remove_rule(entry))
        h.addWidget(op)
        h.addWidget(e1, 1)
        h.addWidget(e2, 1)
        h.addWidget(btn_x)
        self.rules_lay.addWidget(row)
        self._rule_rows.append(entry)

    def remove_rule(self, entry):
        self._rule_rows.remove(entry)
        entry[0].setParent(None)
        entry[0].deleteLater()

    def rules(self):
        out = []
        for _row, op, e1, e2 in self._rule_rows:
            key = self.OPS[op.currentIndex()][1]
            if key == "replace":
                if e1.text():
                    out.append({"op": key, "find": e1.text(),
                                "replace": e2.text()})
            elif e1.text():
                out.append({"op": key, "value": e1.text()})
        return out

    # ---- matching

    def rematch(self):
        payload = self.source_combo.currentData()
        if payload is None:
            return
        try:
            if payload[0] == "item":
                r = self.bridge.build_remap(source_names=payload[2],
                                            rules=self.rules())
            else:
                r = self.bridge.build_remap(source_object=payload[1],
                                            rules=self.rules())
        except bridgemod.BridgeError as exc:
            QMessageBox.warning(self, "Auto-match", str(exc))
            return
        self._match = r
        total = len(r["map"]) + len(r["unmatched"])
        self.match_label.setText("%d of %d bones matched automatically  →  %s"
                                 % (len(r["map"]), total, r["target_armature"]))
        targets = sorted(r["target_bones"], key=str.lower)
        self.tree.clear()
        for s in r["unmatched"]:
            node = QTreeWidgetItem([s, ""])
            self.tree.addTopLevelItem(node)
            combo = QComboBox()
            combo.setEditable(True)  # type-to-search
            combo.setInsertPolicy(QComboBox.NoInsert)
            combo.addItem(self.UNMATCHED)
            combo.addItems(targets)
            self.tree.setItemWidget(node, 1, combo)
        self.btn_save.setEnabled(True)

    def result_data(self):
        """(rules, map, unmatched, source_desc) — call after exec() accepted.
        Hand-picked targets are folded into the map (they win over rules)."""
        r = self._match
        mapping = dict(r["map"])
        unmatched = []
        tset = set(r["target_bones"])
        for i in range(self.tree.topLevelItemCount()):
            node = self.tree.topLevelItem(i)
            combo = self.tree.itemWidget(node, 1)
            choice = combo.currentText().strip() if combo else ""
            if choice in tset:
                mapping[node.text(0)] = choice
            else:
                unmatched.append(node.text(0))
        payload = self.source_combo.currentData()
        return self.rules(), mapping, unmatched, payload[1]


class AboutDialog(widgets.GuardedDialog):
    """Who made this, what version it is, and where to go for help.

    The two links do two different jobs and the wording keeps them apart:
    Discord is where bugs go, Patreon is where support goes. Somebody arriving
    here because something crashed should not be met with a pledge button.

    It also shows the licence state in plain words — including, when there is
    one, the date it runs out. That is deliberately here and not only on a
    locked tab: the person who most needs to know their year is nearly up is
    the one whose tabs still all work.
    """

    def __init__(self, parent, license_manager=None, addon_version=None):
        super().__init__(parent)
        self.setWindowTitle("About " + version.APP_NAME)
        self.setMinimumWidth(420)
        lay = QVBoxLayout(self)
        lay.setSpacing(10)

        title = QLabel(version.APP_NAME)
        font = title.font()
        font.setPointSize(font.pointSize() + 4)
        font.setBold(True)
        title.setFont(font)
        lay.addWidget(title)

        versions = "Version %s" % version.APP_VERSION
        if addon_version:
            versions += "   ·   Blender add-on %s" % addon_version
        sub = QLabel(versions + "\nby " + version.AUTHOR)
        sub.setObjectName("dim")
        lay.addWidget(sub)

        # ⚠ NO LICENCE LINE SINCE 1.19.0. There are no licences: every tool is
        # free, the app contacts nothing, and an About box that discussed
        # entitlement would be describing machinery that no longer exists.

        lay.addSpacing(4)
        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setObjectName("dim")
        lay.addWidget(line)

        help_row = QLabel(
            'Found a bug? Report it on <b>Discord</b> — that is the fastest way '
            'to get it looked at.<br>Support the Toolset on <b>Patreon</b>.')
        help_row.setWordWrap(True)
        lay.addWidget(help_row)

        buttons = QHBoxLayout()
        discord = QPushButton("Discord  (report a bug)")
        discord.clicked.connect(lambda: QDesktopServices.openUrl(
            QUrl(version.DISCORD_URL)))
        buttons.addWidget(discord)
        patreon = QPushButton("Patreon  (support)")
        patreon.clicked.connect(lambda: QDesktopServices.openUrl(
            QUrl(version.PATREON_URL)))
        buttons.addWidget(patreon)
        lay.addLayout(buttons)

        close = QDialogButtonBox(QDialogButtonBox.Close)
        close.rejected.connect(self.reject)
        close.accepted.connect(self.accept)
        lay.addWidget(close)


class LibrarySettingsDialog(widgets.GuardedDialog):
    """Library-wide options (⚙ in the library toolbar).

    Settings here are GLOBAL — they apply to every library tab, not just the
    one the button was pressed in — so the wording says so."""

    def __init__(self, parent, cfg):
        super().__init__(parent)
        self._cfg = cfg    # values() merges nested groups against this
        self.setWindowTitle("Library Settings")
        self.resize(420, 0)
        lay = QVBoxLayout(self)

        self.chk_auto = QCheckBox("Auto-refresh when files change")
        self.chk_auto.setChecked(bool(cfg.get("auto_refresh", False)))
        lay.addWidget(self.chk_auto)
        note = QLabel("Watches every library folder and rescans on its own when "
                      "items are added, renamed or removed outside the app "
                      "(playblasts landing from the render queue, files dropped "
                      "in Explorer). Off by default — ⟳ always rescans by hand. "
                      "Applies to all library tabs.")
        note.setObjectName("dim")
        note.setWordWrap(True)
        lay.addWidget(note)

        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setStyleSheet("color: %s;" % theme.BORDER)
        lay.addWidget(line)

        # Colour theme (Marty picked three from six mockups, 2026-08-08).
        trow = QHBoxLayout()
        trow.addWidget(QLabel("Colour theme"))
        self.cmb_theme = widgets.NoScrollComboBox()
        for name in theme.theme_names():
            self.cmb_theme.addItem(theme.THEMES[name]["label"], name)
        current = cfg.get("theme", theme.DEFAULT_THEME)
        index = self.cmb_theme.findData(current)
        self.cmb_theme.setCurrentIndex(index if index >= 0 else 0)
        trow.addWidget(self.cmb_theme, 1)
        lay.addLayout(trow)
        self.theme_note = QLabel(theme.THEMES[current]["note"]
                                 if current in theme.THEMES else "")
        self.theme_note.setObjectName("dim")
        self.theme_note.setWordWrap(True)
        lay.addWidget(self.theme_note)
        self.cmb_theme.currentIndexChanged.connect(self._theme_note)

        line2 = QFrame()
        line2.setFrameShape(QFrame.HLine)
        line2.setStyleSheet("color: %s;" % theme.BORDER)
        lay.addWidget(line2)

        self.chk_console = QCheckBox("Developer console")
        self.chk_console.setChecked(bool(cfg.get("dev_console", False)))
        lay.addWidget(self.chk_console)
        cnote = QLabel("Adds a Console button to the status bar showing this "
                       "session's errors, warnings and tracebacks — useful when "
                       "something misbehaves and there's no terminal to look at "
                       "(the built exe has none). Off by default. Logging runs "
                       "either way, so switching this on still shows what "
                       "already happened.")
        cnote.setObjectName("dim")
        cnote.setWordWrap(True)
        lay.addWidget(cnote)

        # ⚠ DEVELOPMENT TOOL — NOT BUILT AT ALL IN A SHIPPED BUILD (2026-08-08).
        # `devedit.available()` owns that decision so the checkbox and the mode
        # itself cannot disagree; the whole block is absent rather than
        # disabled, because a greyed-out "Developer mode" is still an invitation
        # to ask what it does. `_settings_values()` then reads the config value
        # straight through, so a value that IS in a config.json survives a trip
        # through Settings instead of being silently rewritten to False.
        self.devedit_available = devedit.available()
        self._cfg_dev_edit = bool(cfg.get("dev_edit", False))
        self.chk_devedit = None
        self.devedit_label = None
        self.btn_clear_edits = None
        if self.devedit_available:
            self.chk_devedit = QCheckBox("Developer mode: edit")
            self.chk_devedit.setChecked(self._cfg_dev_edit)
            lay.addWidget(self.chk_devedit)
            enote = QLabel("Right-click anything — a tab, button, label, field, "
                           "panel or tool-rail entry — to change its text or its "
                           "colours. Paragraphs can take <b>bold</b> and clickable "
                           "links; tabs can take backgrounds. Edits are saved and "
                           "stay applied whether this is on or off; switching it "
                           "off only puts the right-click menus away. ⚠ While it's "
                           "on, right-click is the edit menu everywhere, so the "
                           "library's own item menu is unavailable until you turn "
                           "it back off.")
            enote.setObjectName("dim")
            enote.setWordWrap(True)
            lay.addWidget(enote)

            erow = QHBoxLayout()
            self.devedit_label = QLabel(devedit.summary())
            self.devedit_label.setObjectName("dim")
            self.devedit_label.setWordWrap(True)
            erow.addWidget(self.devedit_label, 1)
            self.btn_clear_edits = QPushButton("Clear edits")
            self.btn_clear_edits.setToolTip(
                "Drop every rename and colour, and put the app back to how it ships")
            self.btn_clear_edits.clicked.connect(self._clear_edits)
            erow.addWidget(self.btn_clear_edits)
            lay.addLayout(erow)

        vline = QFrame()
        vline.setFrameShape(QFrame.HLine)
        vline.setStyleSheet("color: %s;" % theme.BORDER)
        lay.addWidget(vline)

        self.chk_node_remember = QCheckBox("Remember node settings")
        self.chk_node_remember.setChecked(
            bool((cfg.get("nodeeditor") or {}).get("remember", False)))
        lay.addWidget(self.chk_node_remember)
        nnote = QLabel("Node Editor: new nodes start with the values you last "
                       "used for that node type (bake type, resolution, "
                       "paths…) instead of the defaults, and the starting "
                       "graph does too. Off by default — nothing is stored "
                       "while it is off.")
        nnote.setObjectName("dim")
        nnote.setWordWrap(True)
        lay.addWidget(nnote)

        nline = QFrame()
        nline.setFrameShape(QFrame.HLine)
        nline.setStyleSheet("color: %s;" % theme.BORDER)
        lay.addWidget(nline)

        # ⚠ NO AUTO-UPDATE SETTING SINCE 1.19.0 — there is no updater to
        # configure. New versions come from the GitHub releases page.
        vrow = QHBoxLayout()
        # The version has to be READABLE somewhere: "you're on 1.0.0, 1.0.1 is
        # out" is only useful to someone who can find the first number.
        self.version_label = QLabel("%s %s" % (APP_NAME, version.APP_VERSION))
        self.version_label.setObjectName("dim")
        vrow.addWidget(self.version_label)
        vrow.addStretch(1)
        lay.addLayout(vrow)
        unote = QLabel("This app does not update itself and makes no network "
                       "connections. New releases are published on GitHub — "
                       "download one and unzip it over this folder. Your "
                       "library, render queue, presets and baked maps live "
                       "outside it and are never touched.")
        unote.setObjectName("dim")
        unote.setWordWrap(True)
        lay.addWidget(unote)
        # The Blender extension, installable straight from here. Deliberately
        # NOT behind the licence: the bridge serves Studio Library too, which is
        # free, so gating the add-on would break the free tab.
        arow = QHBoxLayout()
        self.addon_label = QLabel(self._addon_summary())
        self.addon_label.setObjectName("dim")
        arow.addWidget(self.addon_label)
        arow.addStretch(1)
        self.btn_install_addon = QPushButton("Install in Blender")
        self.btn_install_addon.setToolTip(
            "Install the bundled Blender add-on into the running Blender.\n"
            "Blender reloads the extension to finish, so the connection drops "
            "for a few seconds — save your work first.")
        self.btn_install_addon.clicked.connect(self._install_addon)
        arow.addWidget(self.btn_install_addon)
        self.btn_save_addon = QPushButton("Save zip…")
        self.btn_save_addon.setToolTip(
            "Write the add-on out so you can install it in Blender by hand.\n"
            "This is the way in when Blender is closed, or when the installed "
            "add-on is too old to update itself.")
        self.btn_save_addon.clicked.connect(self._save_addon)
        arow.addWidget(self.btn_save_addon)
        lay.addLayout(arow)

        brow = QHBoxLayout()
        btn_ok = QPushButton("Save")
        btn_ok.setObjectName("accent")
        btn_ok.clicked.connect(self.accept)
        btn_cancel = QPushButton("Cancel")
        btn_cancel.clicked.connect(self.reject)
        brow.addWidget(btn_ok)
        brow.addStretch(1)
        brow.addWidget(btn_cancel)
        lay.addLayout(brow)

    def _owner(self):
        """The MainWindow. The ⚙ button lives in the library toolbar, so the
        parent here is a LibraryView, which carries it as `.window`. Checked
        both ways rather than assumed, so re-parenting this dialog later cannot
        turn these buttons into silent no-ops."""
        owner = self.parent()
        if owner is not None and not hasattr(owner, "updater"):
            owner = getattr(owner, "window", None)
        return owner if (owner is not None and hasattr(owner, "updater")) else None

    def _addon_summary(self):
        """What is installed in Blender versus what the app is carrying.

        ⚠ Reads `bridge.EXPECTED_ADDON_VERSION`, NOT `addon_bundle.VERSION`:
        importing the bundle keeps 4.1 MB resident for one string (measured
        2026-08-15, PERF_PLAN.md M1). The two cannot drift — al_panel_test
        pins the constant to the add-on source and app_updater_test pins the
        bundle to the constant — so only the two paths that ship the bundle's
        BYTES import it."""
        owner = self._owner()
        bundled = bridgemod.EXPECTED_ADDON_VERSION
        live = getattr(getattr(owner, "bridge", None), "addon_version", None)
        if live is None:
            return ("Blender add-on %s bundled  ·  Blender not connected"
                    % bundled)
        if live == bundled:
            return "Blender add-on %s  ·  up to date" % live
        return ("Blender add-on %s installed  ·  %s bundled"
                % (live, bundled))

    def _install_addon(self):
        """Push the bundled add-on into the running Blender."""
        owner = self._owner()
        if owner is None:
            return
        blocked = owner.addon_pusher.block_reason()
        if blocked:
            # The chicken-and-egg case lands here: an add-on older than 0.7.0
            # has no addon_update command, so it cannot be told to update
            # itself. Point at the fallback rather than just refusing.
            QMessageBox.information(
                self, "Blender add-on",
                "%s\n\nUse “Save zip…” and install it in Blender once "
                "(Preferences → Add-ons → Install from Disk); after that it can "
                "be updated from here." % blocked)
            return
        answer = QMessageBox.question(
            self, "Install Blender add-on",
            "Install add-on %s into the running Blender?\n\n"
            "Blender reloads the extension to finish, so the connection drops "
            "for a few seconds.\n\nSave your work in Blender first."
            % bridgemod.EXPECTED_ADDON_VERSION,
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer == QMessageBox.Yes:
            owner.addon_pusher.install_bundled_addon()

    def _save_addon(self):
        """Write the bundled add-on out for a manual install."""
        import addon_bundle

        owner = self._owner()
        if owner is None:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Save the Blender add-on",
            os.path.join(os.path.expanduser("~"), addon_bundle.file_name()),
            "Blender extension (*.zip)")
        if not path:
            return
        try:
            owner.updater.save_bundled_addon(path)
        except Exception as err:
            QMessageBox.warning(self, "Save the Blender add-on",
                                "Could not write it:\n%s" % err)
            return
        QMessageBox.information(
            self, "Blender add-on",
            "Saved to:\n%s\n\nIn Blender: Edit → Preferences → Add-ons → the "
            "dropdown → Install from Disk." % path)

    def _theme_note(self):
        name = self.cmb_theme.currentData()
        self.theme_note.setText(theme.THEMES.get(name, {}).get("note", ""))

    def _clear_edits(self):
        n = devedit.STORE.count()
        if not n:
            return
        if QMessageBox.question(
                self, "Clear edits",
                "Drop %d edit(s) — names and colours — and put the app back to "
                "how it ships?" % n) != QMessageBox.Yes:
            return
        owner = self._owner()
        devedit.clear_all(owner)
        if self.devedit_label is not None:
            self.devedit_label.setText(devedit.summary())

    def values(self):
        # ⚠ "nodeeditor" is a NESTED group and the caller cfg.update()s this
        # dict wholesale — hand back the existing group with only "remember"
        # changed, or the stored last-used node values would be dropped.
        node_group = dict(self._cfg.get("nodeeditor") or {})
        node_group["remember"] = self.chk_node_remember.isChecked()
        return {"auto_refresh": self.chk_auto.isChecked(),
                "theme": self.cmb_theme.currentData(),
                "dev_console": self.chk_console.isChecked(),
                # Pass the stored value straight through when the control was
                # never built (a shipped build): Settings is not the place a
                # setting silently changes because its widget is missing.
                "dev_edit": (self.chk_devedit.isChecked()
                             if self.chk_devedit is not None
                             else self._cfg_dev_edit),
                "nodeeditor": node_group}


class PlayblastDialog(widgets.GuardedDialog):
    """Playblast options. Same-instance viewport render — Blender is busy for
    the duration (the app greys out like a preview capture). No audio."""

    def __init__(self, view, default_dir, background_block=None,
                 dir_source=None):
        super().__init__(view)
        # Where `default_dir` came from, shown under the folder box. Worth a
        # line of UI: "why is it suddenly pointing at my renders folder?" is
        # otherwise a puzzle, and the answer is that Blender says so.
        self._dir_source = dir_source
        # Why background rendering is unavailable on the connected add-on
        # (None = it's fine). Set by the caller from the bridge capabilities.
        self._background_block = background_block
        self.setWindowTitle("Playblast")
        self.resize(420, 0)
        lay = QVBoxLayout(self)

        from PySide6.QtWidgets import QFormLayout, QSpinBox
        form = QFormLayout()
        self.name_edit = QLineEdit(time.strftime("playblast_%Y%m%d_%H%M%S"))
        form.addRow("Name", self.name_edit)
        self.source = QComboBox()
        self.source.addItems(["Viewport (as shown)", "Active camera"])
        # Marty, 2026-08-05: "Make 'Active camera' in playblast the default
        # option". It is also what makes the background default below possible
        # at all — a headless Blender has no viewport to shoot from.
        self.source.setCurrentIndex(1)
        form.addRow("From", self.source)
        rowf = QHBoxLayout()
        self.f_start = SelectAllSpinBox()
        self.f_start.setRange(-100000, 100000)
        self.f_start.setSpecialValueText("start")  # minimum -> scene start
        self.f_start.setValue(self.f_start.minimum())
        self.f_end = SelectAllSpinBox()
        self.f_end.setRange(-100000, 100000)
        self.f_end.setSpecialValueText("end")
        self.f_end.setValue(self.f_end.minimum())
        rowf.addWidget(self.f_start)
        rowf.addWidget(self.f_end)
        form.addRow("Frames", rowf)
        self.res_pct = QSpinBox()
        self.res_pct.setRange(10, 200)
        self.res_pct.setValue(50)
        self.res_pct.setSuffix(" %")
        form.addRow("Resolution", self.res_pct)
        lay.addLayout(form)

        self.chk_overlays = QCheckBox("Show overlays (bones, wires, gizmos…)")
        lay.addWidget(self.chk_overlays)
        self.chk_background = QCheckBox("Run in background (Render Queue)")
        self.chk_background.setToolTip(
            "Snapshot the scene and render it with a separate headless "
            "Blender, so this session stays free while it runs.\n"
            "Active camera only — a headless Blender has no viewport to "
            "render 'as shown' from.")
        # ⚠ ON BY DEFAULT (Marty, 2026-08-05). `_on_source_changed` runs after
        # every widget exists and will untick it again if the add-on is too old
        # or the source is Viewport — so this is the WISH, and that method has
        # the final say. Ticking it here rather than in `_sync_note` keeps the
        # one place that can turn it off in charge.
        self.chk_background.setChecked(True)
        lay.addWidget(self.chk_background)
        self.source.currentIndexChanged.connect(self._on_source_changed)
        self.chk_background.toggled.connect(lambda _v: self._sync_note())

        drow = QHBoxLayout()
        self.dir_edit = QLineEdit(default_dir)
        btn_browse = QPushButton("…")
        btn_browse.setObjectName("flat")
        btn_browse.clicked.connect(self._browse)
        drow.addWidget(self.dir_edit, 1)
        drow.addWidget(btn_browse)
        dlab = QLabel("Output folder")
        dlab.setObjectName("dim")
        lay.addWidget(dlab)
        lay.addLayout(drow)
        if self._dir_source:
            src = QLabel(self._dir_source)
            src.setObjectName("dim")
            src.setWordWrap(True)
            lay.addWidget(src)

        self.note = QLabel()
        self.note.setObjectName("dim")
        self.note.setWordWrap(True)
        lay.addWidget(self.note)
        self._on_source_changed(self.source.currentIndex())

        brow = QHBoxLayout()
        btn_ok = QPushButton("Playblast")
        btn_ok.setObjectName("accent")
        btn_ok.clicked.connect(self.accept)
        btn_cancel = QPushButton("Cancel")
        btn_cancel.clicked.connect(self.reject)
        brow.addWidget(btn_ok)
        brow.addStretch(1)
        brow.addWidget(btn_cancel)
        lay.addLayout(brow)

    def _on_source_changed(self, index):
        """Viewport mode is a same-instance render only: a headless Blender has
        no 3D viewport to render 'as shown' from, so background is camera-only.
        Overlays are the mirror case — camera renders never have them."""
        camera = index == 1
        self.chk_overlays.setEnabled(not camera)
        # An add-on too old for snapshot_blend can't do this at all — say so on
        # the checkbox instead of letting the render fail later.
        blocked = self._background_block
        self.chk_background.setEnabled(camera and not blocked)
        if blocked:
            self.chk_background.setToolTip(blocked)
            self.chk_background.setText(
                "Run in background (Render Queue) — needs a newer add-on")
        if not camera or blocked:
            self.chk_background.setChecked(False)
        self._sync_note()

    def _sync_note(self):
        if self.chk_background.isChecked():
            self.note.setText(
                "Renders in the Render Queue on a separate headless Blender "
                "(solid shading, active camera) — this session stays free. "
                "H.264 mp4, no audio.")
        else:
            self.note.setText(
                "Blender is busy while the playblast renders (same instance). "
                "H.264 mp4, no audio.")

    def _browse(self):
        path = QFileDialog.getExistingDirectory(self, "Playblast output folder",
                                                self.dir_edit.text())
        if path:
            self.dir_edit.setText(os.path.normpath(path))

    def values(self):
        name = re.sub(r'[<>:"/\\|?*]', "_", self.name_edit.text()).strip().rstrip(".")
        start = self.f_start.value()
        end = self.f_end.value()
        return {
            "name": name or time.strftime("playblast_%Y%m%d_%H%M%S"),
            "dir": self.dir_edit.text().strip(),
            "use_camera": self.source.currentIndex() == 1,
            "frame_start": None if start == self.f_start.minimum() else start,
            "frame_end": None if end == self.f_end.minimum() else end,
            "percent": self.res_pct.value(),
            "overlays": self.chk_overlays.isChecked(),
            "background": (self.chk_background.isChecked()
                           and self.chk_background.isEnabled()),
        }


class LibraryView(QWidget):
    """One tab: toolbar + sidebar | grid | info panel for a single library root."""

    def __init__(self, lib_cfg, bridge, window, parent=None):
        super().__init__(parent)
        self.lib_cfg = lib_cfg
        self.bridge = bridge
        self.window = window
        self.folders = []
        self.items = []
        self.active_mirror = None  # path of the .mirror item chosen via "Use Table"
        self.active_remap = None   # path of the .remap item chosen via "Use Remap"
        self._capture = None       # in-flight CaptureWorker (one at a time)
        self._abc_worker = None    # in-flight alembic export/import worker
        # Auto-refresh (opt-in, ⚙ Library Settings). Created before the first
        # rescan below, because rescan() re-arms the watch list.
        self._watcher = None
        self._watch_timer = QTimer(self)
        self._watch_timer.setSingleShot(True)
        self._watch_timer.setInterval(700)   # debounce: a copy fires many events
        self._watch_timer.timeout.connect(self.auto_rescan)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)

        bar = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search…")
        self.search.setClearButtonEnabled(True)
        # ⚠ Debounced: refilter hides rows instead of rebuilding them now, but
        # it still walks every row — a fast typist deserves one walk, not five
        # (same idiom as _watch_timer / _zoom_timer). Everything else that
        # refilters — sidebar clicks — is a single action and stays direct.
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(SEARCH_DEBOUNCE_MS)
        self._search_timer.timeout.connect(self.refilter)
        self.search.textChanged.connect(
            lambda _text: self._search_timer.start())
        # ⚠ THESE USED TO BE EMOJI ("⟳", "⚙", "🎬", "▶", "🔍", "⬇"). They are
        # FONT, so they ignored the palette, changed shape with a Windows font
        # update, and were the loudest unfinished-looking thing in the app
        # (Marty, 2026-08-14: "looks way too cheap"). `icons` draws them —
        # same reasoning as `SectionTabBar._star` never spelling out "★".
        btn_refresh = QPushButton()
        btn_refresh.setObjectName("flat")
        btn_refresh.setToolTip("Rescan library folder")
        btn_refresh.clicked.connect(self.rescan)
        btn_settings = QPushButton()
        btn_settings.setObjectName("flat")
        btn_settings.setToolTip("Library Settings (applies to all library tabs)")
        btn_settings.clicked.connect(self.library_settings)
        # Icon-only, like the other secondary actions in this row: its label
        # was 108 px of the toolbar's minimum, and the toolbar is what sets how
        # narrow the whole window can be dragged. Import keeps its word — it is
        # the one coloured button and the way things come in.
        btn_newfolder = QPushButton()
        btn_newfolder.setObjectName("flat")
        btn_newfolder.setToolTip("New folder in this library")
        btn_newfolder.clicked.connect(lambda: self.new_folder())
        # THE way things come into a library (Marty, 2026-08-05) — so it is the
        # one coloured button up here, and it takes anything: a share zip, a
        # folder of items, loose .abc/.mp4 files, several at once.
        btn_import = QPushButton("Import")
        btn_import.setProperty("_madi_keep_text", True)
        btn_import.setObjectName("accent")
        btn_import.setToolTip(
            "Import items into this library — a zip somebody shared, a folder "
            "of items, or the items themselves. Thumbnails, previews, tags and "
            "versions come with them.")
        btn_import.clicked.connect(self.import_flow)
        btn_playblast = QPushButton()
        btn_playblast.setObjectName("flat")
        btn_playblast.setToolTip("Playblast: viewport/camera render of the "
                                 "frame range to an mp4 (Blender is busy "
                                 "while it renders)")
        btn_playblast.clicked.connect(self.playblast_flow)
        # Marty, 2026-08-05: "add a button like this too in the app itself."
        # Next to 🎬 because it is the other half of the same job — make one,
        # watch the last one. Its enabled state is refreshed on every rescan
        # (`sync_watch_button`), not polled: nothing else can create a render
        # behind the app's back.
        self.btn_watch = QPushButton()
        self.btn_watch.setObjectName("flat")
        self.btn_watch.clicked.connect(self.watch_last_render)
        # ⚠ `widgets.ElidedLabel`, not a hand-elided QLabel. Eliding the text
        # once at 220 px still left a 204 px MINIMUM — a plain QLabel reports
        # its (elided) text width as its minimum, so the library path was the
        # single widest thing in this toolbar and the toolbar was what set the
        # window's 800 px floor. ElidedLabel re-elides to whatever room it is
        # given and reports `minimum` instead (2026-08-15).
        self.path_label = widgets.ElidedLabel(lib_cfg["path"], minimum=44)
        self.path_label.setObjectName("dim")
        self.zoom = QSlider(Qt.Horizontal)
        self.zoom.setRange(64, 256)
        self.zoom.setValue(config.load().get("icon_size", 110))
        # Wants 110, settles for 60 when the window is narrow — a fixed width
        # here was 110 px of the toolbar's minimum that nothing needed.
        self.zoom.setMinimumWidth(44)
        self.zoom.setMaximumWidth(110)
        self.zoom.setToolTip("Preview size (drag to zoom tiles)")
        self._zoom_timer = QTimer(self)  # debounce: rescale after the drag settles
        self._zoom_timer.setSingleShot(True)
        self._zoom_timer.setInterval(60)
        self._zoom_timer.timeout.connect(
            lambda: self.grid.set_icon_size(self.zoom.value()))
        self.zoom.valueChanged.connect(lambda *_: self._zoom_timer.start())
        self.zoom.sliderReleased.connect(self._persist_zoom)
        zoom_label = QLabel()
        zoom_label.setPixmap(icons.pixmap("search", 14, theme.TEXT_DIM))
        zoom_label.setObjectName("dim")
        for button, glyph in ((btn_refresh, "refresh"), (btn_settings, "gear"),
                              (btn_newfolder, "folder"), (btn_import, "import"),
                              (btn_playblast, "camera"),
                              (self.btn_watch, "play")):
            icons.button_icon(button, glyph)
        # The accent button paints white text, so its glyph has to be white too
        # — the default TEXT_DIM would read as a disabled icon on a live button.
        icons.button_icon(btn_import, "import", color="#ffffff")
        bar.addWidget(btn_refresh)
        bar.addWidget(btn_settings)
        bar.addWidget(btn_newfolder)
        bar.addWidget(btn_import)
        bar.addWidget(btn_playblast)
        bar.addWidget(self.btn_watch)
        self.sync_watch_button()
        bar.addWidget(self.search, 1)
        bar.addWidget(zoom_label)
        bar.addWidget(self.zoom)
        bar.addWidget(self.path_label)
        lay.addLayout(bar)

        split = QSplitter(Qt.Horizontal)
        self.sidebar = Sidebar()
        self.sidebar.selectionChanged.connect(self.refilter)
        self.sidebar.itemsDropped.connect(self.on_items_dropped)
        self.sidebar.folderDropped.connect(self.move_folder)
        self.sidebar.deleteFolderRequested.connect(self.delete_folder)
        self.sidebar.renameFolderRequested.connect(self.rename_folder)
        self.sidebar.newFolderRequested.connect(self.new_folder)
        self.grid = ItemGrid(icon_size=config.load().get("icon_size", 110))
        self.grid.itemSelected.connect(self.on_select)
        self.grid.itemActivated2.connect(self.on_apply_default)
        self.info = InfoPanel()
        self.info.applyRequested.connect(self.on_apply)
        self.info.saveRequested.connect(self.on_save)
        self.info.blendStarted.connect(self.on_blend_start)
        self.info.blendChanged.connect(self.on_blend_change)
        self.info.blendEnded.connect(self.on_blend_end)
        self.info.recaptureRequested.connect(self.on_recapture)
        self.info.deleteRequested.connect(self.on_delete)
        self.grid.deleteRequested.connect(self.on_delete)
        self.grid.contextMenuRequested.connect(self.on_context_menu)
        self.streamer = bridgemod.BlendStreamer(self.bridge)
        # ⚠ THE SIDEBAR SCROLLS TOO. It stacks ten type checkboxes, the folder
        # tree, tags and three filter combos, which gave it a 222 x 597
        # minimum — the tallest thing in the tab and a third of its width.
        # Same treatment the info panel already had, for the same reason.
        side_scroll = QScrollArea()
        side_scroll.setWidgetResizable(True)
        side_scroll.setFrameShape(QFrame.NoFrame)
        side_scroll.setWidget(self.sidebar)
        side_scroll.setMinimumWidth(120)
        split.addWidget(side_scroll)
        # The tiles are the one thing that can always be scrolled to, so this
        # is a floor for "still looks like a grid", not for the content.
        self.grid.setMinimumWidth(120)
        split.addWidget(self.grid)
        # The info panel scrolls vertically instead of dictating the window's
        # minimum height (its stack of preview + options + save form is tall).
        info_scroll = QScrollArea()
        info_scroll.setWidgetResizable(True)
        info_scroll.setFrameShape(QFrame.NoFrame)
        info_scroll.setWidget(self.info)
        info_scroll.setMinimumWidth(190)
        # ⚠ The real fix for "the right side gets split in half" (Marty's
        # screenshot, 2026-08-04) is in panels.py: the save buttons became a
        # 2x2 grid and two unwrappable labels were shortened, which took the
        # panel's own minimum from 420 px down to 334. The scroll area already
        # produced a horizontal bar below that — the content was never actually
        # unreachable — but needing one at all in a normally-sized window is
        # what looked broken. Nothing is pinned to a hand-picked width here;
        # the panel reports what it needs and the splitter respects it.
        split.addWidget(info_scroll)
        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)
        split.setStretchFactor(2, 0)
        # And it may not be dragged shut entirely: a collapsed panel looks like
        # the app lost its right-hand side, and there is no handle left to say
        # otherwise.
        split.setCollapsible(2, False)
        split.setSizes([180, 640, 300])
        lay.addWidget(split, 1)

        self.rescan()

        # restore this library's remap table from config (checkbox stays off —
        # opting into remap is a per-session choice)
        saved_remap = lib_cfg.get("active_remap")
        if saved_remap and os.path.isdir(saved_remap):
            self.active_remap = saved_remap
            self.info.remap_label.setText(
                "Remap table: %s" % self._remap_display_name(saved_remap))
        elif saved_remap:  # table was deleted/moved outside the app
            lib_cfg.pop("active_remap", None)
            config.save(self.window.cfg)

    @staticmethod
    def _remap_display_name(path):
        name = os.path.basename(path.rstrip("\\/"))
        return name[:-len(".remap")] if name.endswith(".remap") else name

    def _set_active_remap(self, path):
        """Set/clear the tab's remap table; persisted per library in config."""
        self.active_remap = path
        if path:
            self.info.remap_label.setText(
                "Remap table: %s" % self._remap_display_name(path))
            self.lib_cfg["active_remap"] = path
        else:
            self.info.remap_label.setText("Remap table: none")
            self.lib_cfg.pop("active_remap", None)
        config.save(self.window.cfg)

    # ------------------------------------------------------------- data

    def rescan(self):
        self.sync_watch_button()
        self.folders, self.items = library.scan(self.lib_cfg["path"])
        # Display order, decided ONCE: newest first. refilter() depends on
        # this being fixed — it toggles row visibility and never reorders.
        self.items.sort(key=lambda it: -it.mtime)
        counts = {None: len(self.items)}
        for it in self.items:
            rel = it.folder
            while True:
                if rel:
                    counts[rel] = counts.get(rel, 0) + 1
                    if "/" not in rel:
                        break
                    rel = rel.rsplit("/", 1)[0]
                else:
                    break
        self.sidebar.set_folders(self.folders, counts)
        tag_counts = {}
        for it in self.items:
            for t in it.tags:
                tag_counts[t] = tag_counts.get(t, 0) + 1
        self.sidebar.set_tags(tag_counts)
        # ⚠ A SOURCE, NOT A SET. Computing the set here read every item's full
        # data file on every scan — 3.47 s of a 4.54 s build over 800 items,
        # for a dropdown that is usually never opened (`panels.AuthorCombo`).
        # `self.items` is read when the list is actually built, so this stays
        # correct across rescans without costing anything at scan time.
        self.sidebar.set_author_source(
            lambda: {a for a in (it.meta().get("author") for it in self.items)
                     if a})
        self.sidebar.author_combo.mark_stale()
        self.grid.set_items(self.items)   # rows built ONCE; refilter() hides
        self.refilter()
        self._rewatch()   # folders may have appeared/vanished since last scan

    # ------------------------------------------------------- auto-refresh

    def library_settings(self):
        """⚙ toolbar button. The dialog itself belongs to the WINDOW — the same
        settings are reachable from the status bar on every tab, and two copies
        of this would be two places to fix a bug in."""
        self.window.show_library_settings(self)

    def _watch_dirs(self):
        """Folders worth watching: the library root and its navigation folders.

        Deliberately NOT the item folders themselves — their insides churn
        (thumbnail.jpg, sequence/, versions/) every time an item is saved, and
        a rescan triggered by our own write would just chase its tail. Watching
        the parents still catches every item added, renamed, moved or deleted,
        plus loose files like a playblast mp4 landing in _playblasts."""
        root = self.lib_cfg["path"]
        dirs = [root] + [os.path.join(root, f.replace("/", os.sep))
                         for f in self.folders]
        return [d for d in dirs if os.path.isdir(d)]

    def set_auto_refresh(self, on):
        """Turn the file watcher on or off (global setting, applied per tab)."""
        if not on:
            self._watch_timer.stop()
            if self._watcher is not None:
                # deleteLater() only destroys it on the NEXT event loop pass,
                # and until then it happily keeps emitting — which would fire a
                # rescan seconds after the user switched the feature off. Cut
                # the signal and drop the paths first, so "off" means off now.
                try:
                    self._watcher.directoryChanged.disconnect()
                except (TypeError, RuntimeError):
                    pass
                paths = self._watcher.directories()
                if paths:
                    self._watcher.removePaths(paths)
                self._watcher.deleteLater()
                self._watcher = None
            return
        if self._watcher is None:
            self._watcher = QFileSystemWatcher(self)
            self._watcher.directoryChanged.connect(
                lambda _p: self._watch_timer.start())
        self._rewatch()

    def _rewatch(self):
        """Point the watcher at the current folder list (no-op when off)."""
        if self._watcher is None:
            return
        old = self._watcher.directories()
        if old:
            self._watcher.removePaths(old)
        new = self._watch_dirs()
        if new:
            self._watcher.addPaths(new)

    def auto_rescan(self):
        """Debounced rescan from the watcher. Keeps the user's place: the
        selection is restored by path, so an item appearing elsewhere in the
        library can't yank the grid out from under a click."""
        if self.window.capturing:
            # Blender is mid-capture and about to write a thumbnail — let that
            # finish and pick the change up on the next event.
            self._watch_timer.start()
            return
        selected = {it.path for it in self.grid.selected_library_items()}
        before = len(self.items)
        self.rescan()
        if selected:
            for i in range(self.grid.count()):
                li = self.grid.item(i)
                it = li.data(Qt.UserRole)
                if it is not None and it.path in selected:
                    li.setSelected(True)
        if len(self.items) != before:
            self.window.statusBar().showMessage(
                "Library changed on disk — refreshed (%d items)"
                % len(self.items), 4000)

    def refilter(self):
        """Apply the filters by HIDING rows, never by rebuilding them.

        ⚠ The rows are created once per rescan, already in display order
        (newest first — the sort lives in rescan and must stay there), so a
        filter change is a visibility pass. It used to rebuild every
        QListWidgetItem + QIcon: **391 ms per search KEYSTROKE at 800 items**
        (measured 2026-08-15, PERF_PLAN.md F1); the hide walk is single-digit
        milliseconds. Anything that changes WHAT a tile looks like must
        refresh icons itself — see set_color_label."""
        folder = self.sidebar.current_folder()
        types = self.sidebar.enabled_types()
        text = self.search.text().strip().lower()
        f = self.sidebar.filters()
        now = time.time()
        shown = []
        for i in range(self.grid.count()):
            li = self.grid.item(i)
            it = li.data(Qt.UserRole)
            visible = it is not None and self._passes(it, folder, types,
                                                      text, f, now)
            if visible:
                shown.append(it)
            elif li.isSelected():
                # A hidden row must not stay selected — Delete and Apply act
                # on the selection, and acting on something invisible is how
                # items vanish "for no reason".
                li.setSelected(False)
            if li.isHidden() == visible:   # touch only rows that flip
                li.setHidden(not visible)
        self.window.statusBar().showMessage(
            "%d / %d items" % (len(shown), len(self.items)))
        self.queue_video_previews(shown)

    def _passes(self, it, folder, types, text, f, now):
        """One item against the current filter set (split out so the
        refilter walk stays readable)."""
        if it.type not in types:
            return False
        if folder and not (it.folder == folder or
                           it.folder.startswith(folder + "/")):
            return False
        if text and text not in it.name.lower():
            return False
        if f["tags"] and not (f["tags"] & set(it.tags)):
            return False
        if f["author"] and it.meta().get("author") != f["author"]:
            return False
        if f["days"] and it.mtime < now - f["days"] * 86400:
            return False
        if f["length"]:
            fs = it.meta().get("frame_start")
            fe = it.meta().get("frame_end")
            if fs is None or fe is None:
                return False  # length filter only matches items with a frame range
            n = fe - fs + 1
            if ((f["length"] == 1 and n > 24)
                    or (f["length"] == 2 and not 25 <= n <= 100)
                    or (f["length"] == 3 and n <= 100)):
                return False
        return True

    def queue_video_previews(self, items):
        """Playblasts are loose mp4s with no sequence/ folder — sample frames
        out of the video (once, cached on disk) so they hover-play like anims.
        Only the ones actually on screen, and only if not already cached."""
        q = getattr(self.window, "video_previews", None)
        if q is None:
            return
        q.clear_pending()   # a new filter/folder supersedes the old backlog
        q.enqueue([it.path for it in items if it.type == "playblast"])

    def on_video_preview_ready(self, path):
        """Frames landed for a playblast — refresh just that tile (and the
        info panel if it happens to be the selected item)."""
        for i in range(self.grid.count()):
            li = self.grid.item(i)
            item = li.data(Qt.UserRole)
            if item is not None and item.path == path:
                li.setIcon(QIcon(gridmod.thumbnail_pixmap(
                    item, self.grid._icon_size)))
                if self.info._item is not None and self.info._item.path == path:
                    self.info.show_item(item)
                break

    # ------------------------------------------------------------- actions

    def on_select(self, item):
        self.info.show_item(item)

    def _bridge_free(self):
        """Bridge commands queue on Blender's main thread — while a capture is
        rendering they would only pile up and time out, so refuse politely."""
        if self.window.capturing:
            self.window.statusBar().showMessage(
                "Blender is busy capturing a preview — try again when it finishes", 4000)
            return False
        return True

    def set_capture_busy(self, busy):
        self.info.set_capture_busy(busy)

    def on_apply_default(self, item):
        self.on_apply(item, self.info.options())

    def on_apply(self, item, opts):
        if item.type == "playblast":  # disk-only: open in the default player
            try:
                desktop.open_path(item.path)
            except OSError as exc:
                self.window.statusBar().showMessage("Could not open: %s" % exc, 5000)
            return
        if not self._bridge_free():
            return
        if item.type == "abc":  # slow for big caches -> background worker
            self.import_abc_flow(item)
            return
        remap_table = None
        if opts.get("remap") and item.type in ("pose", "anim"):
            if not self.active_remap:
                self.window.statusBar().showMessage(
                    "No remap table set — apply a .remap item first (Use Remap)", 5000)
                return
            remap_table = self.active_remap
        # remap runs FIRST, then mirror on the target rig's names
        flags = ("%s%s" % (" remapped" if remap_table else "",
                           " mirrored" if opts["mirror"] else ""))
        if opts["blend"] < 1.0 and item.type in ("pose", "anim"):
            flags += " at %d%%" % round(opts["blend"] * 100)
        try:
            if item.type == "pose":
                r = self.bridge.apply_pose(item.path, selected_only=opts["selected_only"],
                                           blend=opts["blend"], key=opts["key"],
                                           mirror=opts["mirror"],
                                           mirror_table=self.active_mirror,
                                           remap_table=remap_table)
                msg = "Applied '%s'%s: %d bones (%d missing)" % (
                    item.name, flags, r["applied"], r["missing"])
            elif item.type == "mirror":
                self.active_mirror = item.path
                self.info.mirror_label.setText("Mirror table: %s" % item.name)
                msg = "Mirror table set to '%s'" % item.name
            elif item.type == "remap":
                self._set_active_remap(item.path)
                self.info.chk_remap.setChecked(True)
                msg = "Remap table set to '%s' (Use Remap is on)" % item.name
            elif item.type == "set":
                r = self.bridge.apply_set(item.path, extend=opts["extend"])
                msg = "Selected %d bones (%d missing)" % (r["selected"], r["missing"])
            elif item.type == "picker":
                # ⚠ Studio Library is FREE and this item type is not: the tile
                # is visible to everyone, and the add-on refuses the load with
                # its own reason if nothing has unlocked the picker. Deliberate
                # — these are the user's own files, and hiding them would make
                # the library look different from one machine to the next.
                r = self.bridge.picker_apply_item(item.path, replace=True)
                missing = r.get("missing") or []
                msg = "Loaded picker layout '%s': %d button(s)" % (
                    item.name, r.get("added", 0))
                if missing:
                    msg += "  ⚠ not on this rig: " + ", ".join(missing[:4])
                    if len(missing) > 4:
                        msg += " (+%d more)" % (len(missing) - 4)
            elif item.type == "vgroups":
                # EXACT only. The approximate route is its own menu entry with
                # its own warning — see transfer_vgroups().
                r = self.bridge.apply_vgroups(
                    item.path, mode="EXACT",
                    to_active=opts.get("shapes_to_active", False))
                msg = "Restored %d vertex group(s) onto %d mesh(es)" % (
                    r.get("applied", 0), r.get("objects", 0))
                for entry in r.get("skipped") or []:
                    msg += "  ⚠ %s: %s" % (entry["object"], entry["reason"])
            elif item.type == "renderpreset":
                # The item IS the captured preset, so the same command the
                # Rendering tab uses takes it unchanged — no second format and
                # no conversion step that could drift from the tab's.
                r = self.bridge.render_preset_apply(item.read_data())
                msg = "Render preset '%s': %s" % (item.name,
                                                  r.get("summary", "applied"))
                for entry in (r.get("failed") or [])[:2]:
                    msg += "  ⚠ %s: %s" % (entry.get("path"),
                                           entry.get("reason"))
            elif item.type == "shapes":
                to_active = opts.get("shapes_to_active", False)
                try:
                    r = self.bridge.apply_shapes(item.path, to_active=to_active,
                                                 blend=opts["blend"])
                except bridgemod.BridgeError as exc:
                    if "checksum mismatch" not in str(exc):
                        raise
                    answer = QMessageBox.question(
                        self, "Mesh changed since saving",
                        "%s\n\nApply anyway?" % exc,
                        QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
                    if answer != QMessageBox.Yes:
                        return
                    r = self.bridge.apply_shapes(item.path, force=True,
                                                 to_active=to_active,
                                                 blend=opts["blend"])
                msg = "Added %d shape key(s) from '%s'%s%s" % (
                    r["applied"], item.name,
                    " to active object" if to_active else "",
                    " at %d%%" % round(opts["blend"] * 100)
                    if opts["blend"] < 1.0 else "")
                if r["skipped"]:
                    msg += "  ⚠ " + "; ".join(r["skipped"][:3])
            elif item.type == "anim":
                # ⚠ THE ONE APPLY THAT RUNS OFF-THREAD (Marty, 2026-08-06:
                # "applying complex or baked animation also causes a short
                # freeze"). A baked .anim for the 461-bone rig is thousands of
                # F-curves, and every other branch here is a sub-second call
                # that would cost more in flicker than it saves. Same treatment
                # as the alembic paths: the busy grey-out plus the status-bar
                # bar, so the window keeps painting while Blender works.
                self.apply_anim_flow(item, opts, flags, remap_table)
                return
            else:
                msg = "Type '%s' not supported yet" % item.type
        except bridgemod.BridgeError as exc:
            self.window.bridge_error(exc)
            return
        self.window.statusBar().showMessage(msg, 6000)
        self.window.update_bridge_status()

    def _next_unnamed(self, folder):
        """Blank save-name → unnamed_1, unnamed_2, … (the next free number in
        the target folder, counted across all item types)."""
        pat = re.compile(r"unnamed_(\d+)\.[a-z]+$", re.IGNORECASE)
        try:
            entries = os.listdir(os.path.join(self.lib_cfg["path"], folder))
        except OSError:
            entries = []
        taken = {int(m.group(1)) for m in map(pat.match, entries) if m}
        n = 1
        while n in taken:
            n += 1
        return "unnamed_%d" % n

    def on_save(self, kind, name):
        if not self._bridge_free():
            return
        folder = self.sidebar.current_folder() or ""
        if not name:
            name = self._next_unnamed(folder)
        if kind == "shapes":
            self.save_shapes_flow(name, folder)
            return
        if kind == "vgroups":
            self.save_vgroups_flow(name, folder)
            return
        if kind == "remap":
            self.save_remap_flow(name, folder)
            return
        if kind == "abc":
            self.save_abc_flow(name, folder)
            return
        if kind == "picker":
            self.save_picker_flow(name, folder)
            return
        opts = self.info.options()
        anim_opts = {}
        if kind == "anim":
            anim_opts = self.ask_anim_options(opts)
            if anim_opts is None:      # cancelled in the dialog
                return
            opts = {**opts, **anim_opts}

        def do_save(overwrite):
            if kind == "pose":
                r = self.bridge.save_pose(self.lib_cfg["path"], folder, name,
                                          overwrite=overwrite)
                return r, "Saved pose '%s' (%d bones)" % (name, r["bones"])
            if kind == "anim":
                r = self.bridge.save_anim(
                    self.lib_cfg["path"], folder, name,
                    frame_start=opts["frame_start"],
                    frame_end=opts["frame_end"],
                    overwrite=overwrite, bake=opts["bake"],
                    keep_modifiers=opts["keep_modifiers"],
                    include_props=opts["include_props"])
                # ⚠ THE ECHO IS THE CAPABILITY CHECK (the `save_abc` rule).
                # `save_anim` has existed since the first add-on, so nothing in
                # `capabilities()` can tell us whether this one understood the
                # new options — an add-on older than 0.20.0 accepts them,
                # ignores them and writes an item that disagrees with the
                # dialog. A missing echo is the only evidence, so say so.
                ignored = ""
                if "options" not in r and (not opts["keep_modifiers"]
                                           or opts["include_props"]):
                    ignored = ("  ⚠ this add-on ignored the modifier/property "
                               "options — update the extension")
                return r, "Saved anim '%s'%s (%d curves, frames %d-%d)%s" % (
                    name, " baked" if r.get("baked") else "",
                    r["curves"], r["frame_start"], r["frame_end"], ignored)
            if kind == "mirror":
                r = self.bridge.save_mirror(self.lib_cfg["path"], folder, name,
                                            overwrite=overwrite)
                return r, "Saved mirror table '%s' (%d pairs, %d center, %d unmatched)" % (
                    name, r["pairs"], r["center"], r["unmatched"])
            r = self.bridge.save_set(self.lib_cfg["path"], folder, name,
                                     overwrite=overwrite)
            return r, "Saved set '%s' (%d bones)" % (name, r["bones"])

        try:
            try:
                r, msg = do_save(False)
            except bridgemod.BridgeError as exc:
                if "already exists" not in str(exc):
                    raise
                answer = QMessageBox.question(
                    self, "Item exists",
                    "'%s' already exists in this folder.\nOverwrite it?" % name,
                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
                if answer != QMessageBox.Yes:
                    self.window.statusBar().showMessage("Save cancelled", 4000)
                    return
                r, msg = do_save(True)
        except bridgemod.BridgeError as exc:
            self.window.bridge_error(exc)
            return
        self.rescan()  # item shows up right away; thumbnail follows when capture lands
        if kind != "mirror":  # a viewport shot means nothing for a table
            frames = (r["frame_start"], r["frame_end"]) if kind == "anim" else None
            self._start_capture(r["path"], frames, name, base_msg=msg)
        else:
            self.window.statusBar().showMessage(msg, 6000)

    def save_shapes_flow(self, name, folder):
        try:
            listing = self.bridge.list_shape_keys()
        except bridgemod.BridgeError as exc:
            self.window.bridge_error(exc)
            return
        if not any(not k["is_basis"] for mesh in listing for k in mesh["keys"]):
            QMessageBox.information(
                self, "Save Shape Keys",
                "The selected mesh object(s) have no shape keys.\n"
                "Select the mesh(es) to vault in Blender first.")
            return
        dlg = SaveShapesDialog(self, listing)
        if not dlg.exec():
            return
        sel = dlg.selection()
        if not sel:
            self.window.statusBar().showMessage("No keys checked — nothing saved", 4000)
            return
        delete_after = dlg.chk_delete.isChecked()
        if dlg.chk_separate.isChecked():
            return self.save_shapes_separately(folder, name, sel, delete_after)

        # delete_after is deferred to AFTER the preview capture — the ramp
        # preview needs the keys still live on the mesh
        def do_save(overwrite):
            return self.bridge.save_shapes(self.lib_cfg["path"], folder, name,
                                           objects=list(sel.keys()), keys=sel,
                                           delete_after=False,
                                           overwrite=overwrite)

        try:
            try:
                r = do_save(False)
            except bridgemod.BridgeError as exc:
                if "already exists" not in str(exc):
                    raise
                answer = QMessageBox.question(
                    self, "Item exists",
                    "'%s' already exists in this folder.\nOverwrite it?" % name,
                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
                if answer != QMessageBox.Yes:
                    self.window.statusBar().showMessage("Save cancelled", 4000)
                    return
                r = do_save(True)
        except bridgemod.BridgeError as exc:
            self.window.bridge_error(exc)
            return
        msg = "Saved %d shape key(s) from %d mesh(es) as '%s'" % (
            r["keys"], r["meshes"], name)
        self.rescan()

        def after_capture(_ok):
            if not delete_after:
                return
            deleted, errs = 0, []
            for ob_name, key_names in sel.items():
                try:
                    deleted += self.bridge.delete_shape_keys(
                        ob_name, key_names)["deleted"]
                except bridgemod.BridgeError as exc:
                    errs.append("%s: %s" % (ob_name, exc))
            m = "Vault move: %d key(s) removed from mesh" % deleted
            if errs:
                m += "  ⚠ " + "; ".join(errs)
            self.window.statusBar().showMessage(m, 8000)

        self._start_capture(r["path"], None, name, base_msg=msg,
                            shape_steps=16, after=after_capture)

    def save_remap_flow(self, name, folder):
        """Save Remap…: SOURCE = selected pose/anim/set item (bone names read
        from its json) or another scene armature; TARGET = active armature."""
        try:
            st = self.bridge.status()
            arms = self.bridge.list_armatures()
        except bridgemod.BridgeError as exc:
            self.window.bridge_error(exc)
            return
        if not st["is_armature"]:
            QMessageBox.information(
                self, "Save Remap",
                "Make the TARGET armature the active object in Blender first.")
            return
        sources = []
        it = self._selected_item()
        if it is not None and it.type in ("pose", "anim", "set"):
            data = it.read_data()
            names = (list(data.get("bones", []))
                     if it.type == "set" else sorted(data.get("bones", {})))
            if names:
                sources.append((
                    "Item: %s  [%s]  (%d bones)" % (it.name, it.type, len(names)),
                    ("item", "%s [%s]" % (it.name, it.type), names)))
        for a in arms:
            if a["name"] == st["active_object"]:
                continue  # the active armature is the target
            sources.append(("Armature: %s  (%d bones)" % (a["name"], a["bones"]),
                            ("armature", a["name"])))
        if not sources:
            QMessageBox.information(
                self, "Save Remap",
                "No source found — select a pose/anim/set item in the grid, or "
                "have the source armature in the scene too (the TARGET armature "
                "stays active).")
            return
        dlg = SaveRemapDialog(self, sources, st["active_object"])
        if not dlg.exec() or dlg._match is None:
            return
        rules, mapping, unmatched, source_desc = dlg.result_data()

        def do_save(overwrite):
            return self.bridge.save_remap(self.lib_cfg["path"], folder, name,
                                          rules=rules, mapping=mapping,
                                          unmatched=unmatched,
                                          source=source_desc,
                                          overwrite=overwrite)

        try:
            try:
                r = do_save(False)
            except bridgemod.BridgeError as exc:
                if "already exists" not in str(exc):
                    raise
                answer = QMessageBox.question(
                    self, "Item exists",
                    "'%s' already exists in this folder.\nOverwrite it?" % name,
                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
                if answer != QMessageBox.Yes:
                    self.window.statusBar().showMessage("Save cancelled", 4000)
                    return
                r = do_save(True)
        except bridgemod.BridgeError as exc:
            self.window.bridge_error(exc)
            return
        self.rescan()
        self.window.statusBar().showMessage(
            "Saved remap '%s' (%d mapped, %d unmatched)" % (
                name, r["mapped"], r["unmatched"]), 6000)

    def anim_layer_warning(self):
        """"Bake this first" warning for the Save Anim dialog, or None.

        Marty, 2026-08-05: "If an animation has multiple layers or NLA layers,
        put up a warning so they bake the animation in bake tab or else it won't
        show propperly."

        ⚠ A WARNING, NOT A REFUSAL. Saving a layered rig is legitimate — the
        ACTIVE action is what gets stored and that may be exactly what was
        wanted. What is not obvious is that the layers underneath contribute to
        what is on screen and none of it is in the file, so the item plays back
        looking wrong for a reason nothing else would explain.

        ⚠ Asked once, when the dialog opens, and never polled: this is one
        `anim_layers_status` call on a click, not something to put on a timer.
        Any failure at all (old add-on, no armature, bridge down) means no
        warning — a false alarm here would teach him to ignore the real one.
        """
        try:
            st = self.bridge.anim_layers_status() or {}
        except bridgemod.BridgeError:
            return None
        if st.get("error"):
            return None
        layers = st.get("layers") or []
        foreign = st.get("foreign_nla")
        if len(layers) > 1:
            return ("This rig has %d animation layers. Only the ACTIVE one is "
                    "saved — bake or merge them in Anim Layers ▸ Merge / Bake "
                    "first, or the item will play back missing the rest."
                    % len(layers))
        if foreign:
            return ("This rig has NLA tracks that were not made here. Only the "
                    "active action is saved — bake the stack down in Anim "
                    "Layers ▸ Merge / Bake first, or the item will play back "
                    "missing what the tracks contribute.")
        return None

    def ask_anim_options(self, opts):
        """Show the Save Anim dialog. Returns the chosen options, or None if it
        was cancelled.

        The panel's Start/End boxes still SEED the range (same deal as Export
        Abc) — they are a starting point now, not the only place to say it —
        and when they are blank the scene's own timeline fills them in.
        """
        try:
            st = self.bridge.status()
            scene_range = (st["frame_start"], st["frame_end"])
        except (bridgemod.BridgeError, KeyError, TypeError):
            scene_range = None
        stored = {"bake": False, "keep_modifiers": True, "include_props": False}
        stored.update(self.window.cfg.get("anim_export") or {})
        dialog = SaveAnimDialog(self, stored,
                                opts["frame_start"], opts["frame_end"],
                                scene_range=scene_range,
                                layer_warning=self.anim_layer_warning())
        if dialog.exec() != QDialog.Accepted:
            self.window.statusBar().showMessage("Save cancelled", 4000)
            return None
        chosen = dialog.values()
        self.window.cfg["anim_export"] = chosen
        config.save(self.window.cfg)
        start, end = dialog.frames()
        return {**chosen, "frame_start": start, "frame_end": end}

    def save_abc_flow(self, name, folder):
        """Export Abc: the options dialog owns the frame range and every
        exporter setting (Marty, 2026-08-05). The sidebar's Start/End boxes
        still seed the dialog, so the old way of setting a range keeps working
        — it is now a starting point rather than the only place to say it. The
        export runs on a worker thread behind the busy grey-out (heavy caches
        take a while), then an anim-style playback preview."""
        opts = self.info.options()
        name = re.sub(r'[<>:"/\\|?*]', "_", name).strip().rstrip(".") or name
        item_dir = os.path.join(self.lib_cfg["path"], folder, name + ".abc")
        if os.path.isfile(item_dir):
            QMessageBox.warning(
                self, "Export Abc",
                "A loose .abc file named '%s' is already in this folder — "
                "rename it or convert it to a library item first." % name)
            return
        overwrite = False
        if os.path.isdir(item_dir):
            answer = QMessageBox.question(
                self, "Item exists",
                "'%s' already exists in this folder.\nOverwrite it?" % name,
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if answer != QMessageBox.Yes:
                self.window.statusBar().showMessage("Save cancelled", 4000)
                return
            overwrite = True
        # ⚠ ASKED BEFORE THE OVERWRITE PROMPT WOULD BE WRONG — an export you
        # then cancel should not have already versioned the old item. The
        # dialog is last, so Cancel here still leaves everything untouched.
        stored = dict(abc_defaults())
        stored.update(self.window.cfg.get("abc_export") or {})
        # Only so the dialog can SAY what the scene range is next to the
        # "use it" box; the export itself still passes None and lets the add-on
        # read the range at run time. Offline, the hint is simply left off.
        try:
            st = self.bridge.status()
            scene_range = (st["frame_start"], st["frame_end"])
        except (bridgemod.BridgeError, KeyError, TypeError):
            scene_range = None
        dialog = AbcExportDialog(self, stored,
                                 opts["frame_start"], opts["frame_end"],
                                 scene_range=scene_range)
        if dialog.exec() != QDialog.Accepted:
            self.window.statusBar().showMessage("Export cancelled", 4000)
            return
        chosen = dialog.values()
        frame_start, frame_end = dialog.frames()
        self.window.cfg["abc_export"] = chosen
        config.save(self.window.cfg)
        self.window.begin_capture(name, verb="exporting")
        worker = BridgeWorker(
            lambda ow=overwrite: self.bridge.save_abc(
                self.lib_cfg["path"], folder, name,
                frame_start=frame_start, frame_end=frame_end,
                overwrite=ow, options=chosen), parent=self)
        self._abc_worker = worker

        def _done(r):
            self._abc_worker = None
            self.window.end_capture()
            self.rescan()
            msg = "Exported '%s' (%d object(s), frames %d-%d, %.1f MB)" % (
                name, r["objects"], r["frame_start"], r["frame_end"],
                r["size_bytes"] / 1048576.0)
            # ⚠ THE REPLY IS THE CAPABILITY CHECK HERE, and it has to be —
            # `save_abc` exists in every add-on version, so the usual
            # command-name gate cannot tell that it grew an `options`
            # parameter. An add-on too old to read them ignores them SILENTLY
            # and exports with the defaults, which is a wrong cache rather than
            # a failed one. It echoes back what it used; nothing echoed means
            # nothing was read.
            if "options" not in r:
                msg += "  —  your export options were IGNORED (the Blender " \
                       "add-on is too old to read them; update it from " \
                       "⚙ Settings)"
            self._start_capture(r["path"], (r["frame_start"], r["frame_end"]),
                                name, base_msg=msg)

        def _failed(err):
            self._abc_worker = None
            self.window.end_capture()
            self.rescan()  # a failed export may leave an empty item folder
            QMessageBox.warning(self, "Export Abc", err)

        worker.done.connect(_done)
        worker.failed.connect(_failed)
        worker.start()

    def apply_anim_flow(self, item, opts, flags, remap_table):
        """Paste an animation on a worker thread, behind the busy grey-out.

        The freeze this removes was never Blender being slow — it was the app
        waiting on the socket ON ITS GUI THREAD, so the window stopped painting
        for as long as the paste took. Blender is busy either way; this is only
        about the app staying alive and SAYING it is working (`begin_capture`
        greys the pages, names the job in the bridge label and shows the
        indeterminate bar — the same thing the alembic export/import do).

        ⚠ The options are read and bound HERE, not inside the worker. `opts`
        and `self.active_mirror` are live UI state, and a thread that read them
        when it happened to get scheduled could paste with settings the user had
        already changed.
        """
        path, mode = item.path, opts["anim_mode"]
        start_at, sel_only = opts["start_at"], opts["selected_only"]
        mirror, mirror_table = opts["mirror"], self.active_mirror
        blend = opts["blend"]

        self.window.begin_capture(item.name, verb="applying")
        worker = BridgeWorker(
            lambda: self.bridge.apply_anim(
                path, mode=mode, start_at=start_at, selected_only=sel_only,
                mirror=mirror, mirror_table=mirror_table,
                remap_table=remap_table, blend=blend), parent=self)
        # Held on self for the same reason the abc worker is: a local would be
        # collected mid-flight and take the QObject's signals with it.
        self._anim_worker = worker

        def _done(r):
            self._anim_worker = None
            self.window.end_capture()
            self.window.statusBar().showMessage(
                "Pasted '%s'%s: %d curves at frames %d-%d (%d bones missing)"
                % (item.name, flags, r["curves"], r["pasted_range"][0],
                   r["pasted_range"][1], r["missing"]), 6000)
            self.window.update_bridge_status()

        def _failed(err):
            self._anim_worker = None
            self.window.end_capture()
            self.window.bridge_error(bridgemod.BridgeError(err))

        worker.done.connect(_done)
        worker.failed.connect(_failed)
        worker.start()

    def import_abc_flow(self, item):
        """Import a .abc item (or loose .abc file) into the scene as new
        objects — worker thread, big caches take a while."""
        self.window.begin_capture(item.name, verb="importing")
        worker = BridgeWorker(lambda: self.bridge.apply_abc(item.path),
                              parent=self)
        self._abc_worker = worker

        def _done(r):
            self._abc_worker = None
            self.window.end_capture()
            names = ", ".join(r["objects"][:4])
            if r["imported"] > 4:
                names += ", …"
            self.window.statusBar().showMessage(
                "Imported %d object(s) from '%s'  (%s)" % (
                    r["imported"], item.name, names), 8000)

        def _failed(err):
            self._abc_worker = None
            self.window.end_capture()
            QMessageBox.warning(self, "Import Abc", err)

        worker.done.connect(_done)
        worker.failed.connect(_failed)
        worker.start()

    def sync_watch_button(self):
        """Enable/label ▶ from the shared record on disk.

        ⚠ Reads the record rather than remembering what this session rendered:
        the newest render may have come from the OTHER side (Blender's own
        N-panel playblast) or from a previous run of the app, and a button that
        only knew about this session's would sit dead after a restart with a
        perfectly good mp4 on disk."""
        path = lastrender.last()
        self.btn_watch.setEnabled(path is not None)
        self.btn_watch.setToolTip(
            "Watch the newest viewport render:\n%s" % path if path else
            "Watch the newest viewport render — there isn't one yet. "
            "Playblast something with 🎬.")

    def watch_last_render(self):
        path = lastrender.last()
        if path is None:
            self.sync_watch_button()   # it went away since the last rescan
            self.window.statusBar().showMessage(
                "No viewport render to watch yet", 4000)
            return
        try:
            desktop.open_path(path)
        except OSError as exc:
            QMessageBox.warning(self, "Watch render", str(exc))

    def _playblast_dir(self, status):
        """(folder, why) for the playblast dialog's Output folder box.

        Marty, 2026-08-05: "when doing playblast the output folder should be
        the same as the output folder that is set in blender."

        ⚠ BLENDER WINS OVER THE REMEMBERED FOLDER, every time the dialog opens.
        Persisting the last-used folder and preferring THAT would mean the
        setting worked exactly once and then never again — the first playblast
        would save its own folder and shadow the scene's from then on. The
        remembered one stays as the fallback for when Blender cannot say
        (an older add-on, an unsaved file with a `//` output path, the bridge
        down), which is the only time it is the best answer available.
        """
        out = status.get("output_dir")
        if out:
            return out, "From Blender's own output folder (Output ▸ Output Path)."
        remembered = self.lib_cfg.get("playblast_dir")
        if remembered:
            return remembered, ("Blender didn't say where its renders go — "
                                "using the folder you last chose.")
        return (os.path.join(self.lib_cfg["path"], "_playblasts"),
                "Blender didn't say where its renders go — using the library's "
                "own _playblasts folder.")

    def playblast_flow(self):
        """🎬 toolbar button: options dialog, then the playblast renders on a
        worker thread behind the busy grey-out; auto-opens the mp4 when done."""
        if not self._bridge_free():
            return
        # Ask the bridge what it can do BEFORE offering the option (a stale
        # capability cache would only re-enable it for one doomed click). The
        # same reply carries the scene's own output folder.
        status = {}
        try:
            status = self.bridge.status(timeout=1.5) or {}
        except bridgemod.BridgeError:
            pass
        default_dir, dir_source = self._playblast_dir(status)
        dlg = PlayblastDialog(
            self, default_dir, dir_source=dir_source,
            background_block=self.bridge.feature_reason("background_playblast"))
        if not dlg.exec():
            return
        o = dlg.values()
        if not o["dir"]:
            return
        if o["dir"] != default_dir:  # remember the chosen folder per library
            self.lib_cfg["playblast_dir"] = o["dir"]
            config.save(self.window.cfg)
        output = os.path.join(o["dir"], o["name"] + ".mp4")
        if os.path.exists(output):
            answer = QMessageBox.question(
                self, "Playblast", "'%s.mp4' already exists.\nOverwrite it?"
                % o["name"], QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if answer != QMessageBox.Yes:
                return
        if o["background"]:
            self.background_playblast(o, output)
            return
        self.window.begin_capture(o["name"], verb="playblasting")
        worker = BridgeWorker(
            lambda: self.bridge.playblast(
                output, frame_start=o["frame_start"], frame_end=o["frame_end"],
                use_camera=o["use_camera"], resolution_percent=o["percent"],
                overlays=o["overlays"]), parent=self)
        self._abc_worker = worker

        def _done(r):
            self._abc_worker = None
            self.window.end_capture()
            fps = r.get("fps")
            self.window.statusBar().showMessage(
                "Playblast done: frames %d-%d%s, %.1f MB — %s" % (
                    r["frame_start"], r["frame_end"],
                    " @ %g fps" % fps if fps else "",
                    r["size_bytes"] / 1048576.0, r["path"]), 10000)
            # The ADD-ON already recorded this one (it did the render), but
            # writing it here too keeps one rule: whoever ends up with the path
            # records it. Costs a small file write.
            self.window.note_render(r["path"], tell_blender=False)
            try:
                desktop.open_path(r["path"])  # default video player
            except OSError:
                pass

        def _failed(err):
            self._abc_worker = None
            self.window.end_capture()
            QMessageBox.warning(self, "Playblast", err)

        worker.done.connect(_done)
        worker.failed.connect(_failed)
        worker.start()

    def background_playblast(self, o, output):
        """Playblast without tying up Blender: ask the bridge for a throwaway
        snapshot of the scene (fast — one file write), then hand that to the
        Render Queue, which drives its own headless Blender.

        The snapshot is the only part Blender is busy for; the render itself
        happens in another process, so the live session stays usable."""
        queue = getattr(self.window, "render_queue", None)
        if queue is None:
            QMessageBox.warning(self, "Playblast",
                                "The Render Queue isn't available.")
            return
        self.window.begin_capture(o["name"], verb="snapshotting for")
        worker = BridgeWorker(lambda: self.bridge.snapshot_blend(), parent=self)
        self._abc_worker = worker

        def _done(r):
            self._abc_worker = None
            self.window.end_capture()
            # Blank frame boxes mean "the scene's range" — resolve them here
            # from the snapshot, so the queue never has to launch Blender again
            # just to re-read a range we were already told.
            start = o["frame_start"]
            end = o["frame_end"]
            start = r["frame_start"] if start is None else start
            end = r["frame_end"] if end is None else end
            if end < start:
                start, end = end, start
            ok, msg = queue.queue_playblast(
                r["path"], output, frame_start=start, frame_end=end,
                percent=o["percent"], label=o["name"], temp_blend=True)
            if not ok:
                # The snapshot is no use to anyone now — bin it and any
                # .blend1 backup Blender made beside it.
                render_deck_util.remove_snapshot(r["path"])
                QMessageBox.warning(self, "Background playblast", msg)
                return
            self.window.statusBar().showMessage(
                "Background playblast '%s' started: frames %d-%d at %d%% — "
                "Blender is free, watch it in Rendering ▸ Render Queue"
                % (o["name"], start, end, o["percent"]), 10000)

        def _failed(err):
            self._abc_worker = None
            self.window.end_capture()
            QMessageBox.warning(self, "Background playblast", err)

        worker.done.connect(_done)
        worker.failed.connect(_failed)
        worker.start()

    def wrap_abc(self, item):
        """Convert a loose .abc file into a proper <name>.abc item folder (the
        file becomes cache.abc inside) so it gains tags/versions/previews."""
        if not item.bare:
            return
        src = item.path
        tmp = src + ".wrapping"
        import shutil
        try:
            os.rename(src, tmp)          # free the name for the folder
            os.makedirs(src)
            shutil.move(tmp, os.path.join(src, "cache.abc"))
        except OSError as exc:
            if os.path.isfile(tmp) and not os.path.exists(src):
                os.rename(tmp, src)      # roll back to the loose file
            QMessageBox.warning(self, "Convert to Library Item", str(exc))
            self.rescan()
            return
        data = {"type": "abc",
                "metadata": {"created": time.strftime("%Y-%m-%dT%H:%M:%S"),
                             "author": os.environ.get("USERNAME") or "",
                             "description": "",
                             "source_file": os.path.basename(src),
                             "size_bytes": os.path.getsize(
                                 os.path.join(src, "cache.abc"))}}
        try:
            with open(os.path.join(src, "abc.json"), "w", encoding="utf-8") as f:
                json.dump(data, f, indent=1)
        except OSError as exc:
            QMessageBox.warning(self, "Convert to Library Item", str(exc))
        self.rescan()
        self.window.statusBar().showMessage(
            "Converted '%s' to a library item — tags/versions/previews now "
            "work (use 📷 after importing it)" % item.name, 8000)

    def on_delete(self):
        items = self.grid.selected_library_items()
        if not items:
            return
        listing = "\n".join("•  %s  [%s]" % (i.name, i.type) for i in items[:12])
        if len(items) > 12:
            listing += "\n… and %d more" % (len(items) - 12)
        answer = QMessageBox.question(
            self, "Delete %d item%s" % (len(items), "s" if len(items) > 1 else ""),
            "Permanently delete from the library?\n\n%s" % listing,
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer != QMessageBox.Yes:
            return
        import shutil
        errors = []
        for it in items:
            try:
                if os.path.isdir(it.path):
                    shutil.rmtree(it.path)
                else:
                    os.remove(it.path)  # bare .abc = a plain file
            except OSError as exc:
                errors.append("%s: %s" % (it.name, exc))
            if self.active_mirror == it.path:
                self.active_mirror = None
                self.info.mirror_label.setText("Mirror table: auto-detect")
            if self.active_remap == it.path:
                self._set_active_remap(None)
                self.info.chk_remap.setChecked(False)
        self.rescan()
        self.info.show_item(None)
        if errors:
            QMessageBox.warning(self, "Delete", "Some items failed:\n" + "\n".join(errors))
        else:
            self.window.statusBar().showMessage("Deleted %d item(s)" % len(items), 5000)

    def on_recapture(self, item):
        if not self._bridge_free():
            return
        # A vertex-group item's preview is its weight paint, not the viewport as
        # it happens to look — so 📷 goes down the same road the save did.
        if item.type == "vgroups":
            self._start_vgroup_capture(item.path, item.name)
            return
        # A picker layout's preview is its reference picture with the buttons
        # drawn on it — no Blender involved, and no viewport shot could produce
        # it. ⚠ Without this branch 📷 would replace the layout with whatever
        # the 3D view happens to show, which is the one thing that preview must
        # never become.
        if item.type == "picker":
            if pickermod.compose_thumbnail(item.path):
                # the thumbnail cache is keyed by (path, mtime, size), so
                # rewriting the file is enough to miss it — a rescan repaints
                self.rescan()
                self.window.statusBar().showMessage(
                    "Redrew the picker preview for '%s'" % item.name, 5000)
            else:
                self.window.statusBar().showMessage(
                    "No reference picture stored with '%s'" % item.name, 5000)
            return
        frames = None
        shape_steps = 16 if item.type == "shapes" else None
        if item.type in ("anim", "abc"):  # playback preview over the item range
            meta = item.read_data().get("metadata", {})
            if meta.get("frame_start") is not None:
                frames = (meta["frame_start"], meta["frame_end"])
        self._start_capture(item.path, frames, item.name, shape_steps=shape_steps)

    def save_picker_flow(self, name, folder):
        """Save the ACTIVE Bone picker tab as a `.picker` item.

        ⚠ NO VIEWPORT CAPTURE, unlike every other type here. A picker layout's
        preview is its own reference picture with the buttons drawn onto it —
        a 3D shot would show the character rather than the thing being saved,
        and a layout traced over a reference means nothing without it. The
        add-on writes the clean picture; `picker.compose_thumbnail` draws the
        buttons on. Same path 📷 takes for a `.picker` tile (on_recapture).
        """
        def do_save(overwrite):
            return self.bridge.picker_save_item(self.lib_cfg["path"], folder,
                                                name, overwrite=overwrite)

        try:
            try:
                r = do_save(False)
            except bridgemod.BridgeError as exc:
                if "already exists" not in str(exc):
                    raise
                answer = QMessageBox.question(
                    self, "Item exists",
                    "'%s' already exists in this folder.\nOverwrite it?" % name,
                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
                if answer != QMessageBox.Yes:
                    self.window.statusBar().showMessage("Save cancelled", 4000)
                    return
                r = do_save(True)
        except bridgemod.BridgeError as exc:
            self.window.bridge_error(exc)
            return
        # ⚠ The reply is the WHOLE picker status with the save result under
        # `saved_*` keys — every picker command answers that way. Reading it as
        # a bare {path, buttons} dict is the mistake this comment exists for.
        msg = "Saved picker tab '%s' (%d buttons)" % (
            name, r.get("saved_buttons", 0))
        if r.get("saved_thumbnail") and r.get("saved_path"):
            if pickermod.compose_thumbnail(r["saved_path"]):
                msg += ", buttons drawn on the preview"
        elif not r.get("saved_thumbnail"):
            msg += " — no preview: that tab has no reference image"
        self.rescan()
        self.window.statusBar().showMessage(msg, 6000)

    def save_vgroups_flow(self, name, folder):
        """Store chosen vertex groups of the selected meshes as a .vgroups
        item, then capture a weight-paint preview of them."""
        try:
            listing = self.bridge.list_vertex_groups()
        except bridgemod.BridgeError as exc:
            self.window.bridge_error(exc)
            return
        total = sum(len(m.get("groups") or []) for m in listing)
        if not total:
            QMessageBox.information(
                self, "Save Vertex Groups",
                "The selected mesh object(s) have no vertex groups.\n"
                "Select the mesh(es) in Blender first.")
            return
        dialog = SaveVGroupsDialog(self, listing)
        if dialog.exec() != QDialog.Accepted:
            self.window.statusBar().showMessage("Save cancelled", 4000)
            return
        chosen = dialog.selection()
        if not chosen:
            return
        if dialog.chk_separate.isChecked():
            self.save_vgroups_separately(folder, name, chosen)
            return

        def do_save(overwrite):
            return self.bridge.save_vgroups(
                self.lib_cfg["path"], folder, name,
                objects=list(chosen), groups=chosen, overwrite=overwrite)

        try:
            try:
                result = do_save(False)
            except bridgemod.BridgeError as exc:
                if "already exists" not in str(exc):
                    raise
                if QMessageBox.question(
                        self, "Item exists",
                        "'%s' already exists in this folder.\nOverwrite it?"
                        % name, QMessageBox.Yes | QMessageBox.No,
                        QMessageBox.No) != QMessageBox.Yes:
                    return
                result = do_save(True)
        except bridgemod.BridgeError as exc:
            self.window.bridge_error(exc)
            return
        self.rescan()
        msg = ("Saved %d vertex group(s) from %d mesh(es) as '%s'"
               % (result["groups"], result["meshes"], name))
        self._start_vgroup_capture(result["path"], name, base_msg=msg)

    def save_vgroups_separately(self, folder, name, chosen):
        """One library item per vertex group, each with its own weight-paint
        preview (Marty, 2026-08-05: "an option to export individually too (and
        not all in one file)").

        ⚠ Same shape as `save_shapes_separately`, and for the same reasons: the
        bar is driven from HERE because the app makes one call per group and so
        already knows the count, and ONE GROUP FAILING DOES NOT STOP THE REST —
        a run over forty groups is minutes of Blender being busy, and throwing
        it away because the fourth had a name clash would waste all of it.
        """
        jobs = [(obj, group) for obj, groups in chosen.items()
                for group in groups]
        if not jobs:
            return
        # Asked ONCE, up front. The per-item captures cannot be interrupted, so
        # the count is the only thing worth knowing before it starts.
        if QMessageBox.question(
                self, "Save each group separately",
                "Write %d library item(s), one per vertex group?\n\n"
                "Each is captured separately in Weight Paint, so this takes a "
                "while — Blender is busy for the duration." % len(jobs),
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.Yes) != QMessageBox.Yes:
            return
        # Asked once here too, rather than per item: if the add-on cannot make
        # weight-paint previews, forty items should still be written — without
        # forty identical refusals in the status bar.
        preview_block = self.bridge.feature_reason("vgroup_preview")

        bar = self.window.capture_progress
        bar.setRange(0, len(jobs))
        bar.setValue(0)
        bar.setFormat("%v / %m")
        bar.setTextVisible(True)
        self.window.begin_capture("%d vertex groups" % len(jobs), verb="saving")

        state = {"index": 0, "saved": 0, "failed": []}

        def finish():
            bar.setTextVisible(False)
            bar.setFormat("")
            bar.setRange(0, 0)          # back to the indeterminate default
            self.window.end_capture()
            self.rescan()
            note = "Saved %d of %d vertex group(s) as separate items" % (
                state["saved"], len(jobs))
            if state["failed"]:
                note += "  —  failed: " + ", ".join(state["failed"][:4])
                if len(state["failed"]) > 4:
                    note += " and %d more" % (len(state["failed"]) - 4)
            elif preview_block:
                note += "  —  no weight-paint previews: " + preview_block
            self.window.statusBar().showMessage(note, 12000)

        def step():
            if state["index"] >= len(jobs):
                return finish()
            obj, group = jobs[state["index"]]
            state["index"] += 1
            bar.setValue(state["index"])
            item_name = "%s_%s" % (name, group) if name else group
            self.window.statusBar().showMessage(
                "Saving '%s'  (%d of %d)…" % (item_name, state["index"],
                                              len(jobs)))
            try:
                result = self.bridge.save_vgroups(
                    self.lib_cfg["path"], folder, item_name,
                    objects=[obj], groups={obj: [group]}, overwrite=True)
            except bridgemod.BridgeError as exc:
                state["failed"].append("%s (%s)" % (group, exc))
                return QTimer.singleShot(0, step)
            state["saved"] += 1
            if preview_block:
                return QTimer.singleShot(0, step)
            self._capture_for_batch(result.get("path"), item_name, step,
                                    vgroups=True)

        QTimer.singleShot(0, step)

    def transfer_vgroups(self, item):
        """Put a stored item's weights onto the ACTIVE mesh, spatially.

        ⚠ A SEPARATE ACTION FROM LOADING, on purpose. Loading is an index-based
        restore and is exact. This is an approximation — Blender's nearest-face
        interpolation — and on a character it always needs cleanup. Marty asked
        for both; putting them behind one button is how somebody ships a rig
        with quietly wrong weights.
        """
        if QMessageBox.question(
                self, "Transfer weights",
                "Transfer these weights onto the ACTIVE mesh?\n\n"
                "This is an approximation, not a restore: the weights are "
                "sampled onto whatever topology the target has. Expect to "
                "clean it up — especially around seams and small details.\n\n"
                "Loading the item normally is exact, but needs a mesh with the "
                "same vertex count.",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No) != QMessageBox.Yes:
            return
        try:
            result = self.bridge.apply_vgroups(item.path, mode="TRANSFER")
        except bridgemod.BridgeError as exc:
            self.window.bridge_error(exc)
            return
        self.window.statusBar().showMessage(
            "Transferred %d group(s) from '%s' — approximate, check the result"
            % (result.get("applied", 0), result.get("transferred_from", "?")),
            10000)

    def save_shapes_separately(self, folder, name, sel, delete_after):
        """One library item per shape key, each with its own preview.

        Marty, 2026-08-04: "ability to export many shapekeys, this should export
        all shapekeys one after another with previews, we need the same loading
        bar we did before."

        ⚠ THE BAR IS DRIVEN FROM HERE, not from the add-on. The Optimization
        tab needed `opt_progress` because ONE bridge call did all the work and
        only Blender knew how far along it was. This is the opposite shape: the
        app makes one call per key, so it already knows the count and the
        position, and asking Blender would tell it nothing it does not have.

        ⚠ ONE KEY FAILING DOES NOT STOP THE REST. Twenty keys is minutes of
        work; abandoning it because the fourth had a name clash would waste all
        of it. Failures are collected and named at the end.
        """
        jobs = [(obj, key) for obj, keys in sel.items() for key in keys]
        if not jobs:
            return
        if QMessageBox.question(
                self, "Save each key separately",
                "Write %d library item(s), one per shape key?\n\n"
                "Each is captured separately, so this takes a while — Blender "
                "is busy for the duration." % len(jobs),
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.Yes) != QMessageBox.Yes:
            return

        bar = self.window.capture_progress
        bar.setRange(0, len(jobs))
        bar.setValue(0)
        bar.setFormat("%v / %m")
        bar.setTextVisible(True)
        self.window.begin_capture("%d shape keys" % len(jobs), verb="saving")

        state = {"index": 0, "saved": 0, "failed": []}

        def finish():
            bar.setTextVisible(False)
            bar.setFormat("")
            bar.setRange(0, 0)          # back to the indeterminate default
            self.window.end_capture()
            self.rescan()
            note = "Saved %d of %d shape key(s) as separate items" % (
                state["saved"], len(jobs))
            if state["failed"]:
                note += "  —  failed: " + ", ".join(state["failed"][:4])
                if len(state["failed"]) > 4:
                    note += " and %d more" % (len(state["failed"]) - 4)
            self.window.statusBar().showMessage(note, 12000)

        def step():
            if state["index"] >= len(jobs):
                return finish()
            obj, key = jobs[state["index"]]
            state["index"] += 1
            bar.setValue(state["index"])
            item_name = "%s_%s" % (name, key) if name else key
            self.window.statusBar().showMessage(
                "Saving '%s'  (%d of %d)…" % (item_name, state["index"],
                                              len(jobs)))
            try:
                result = self.bridge.save_shapes(
                    self.lib_cfg["path"], folder, item_name,
                    objects=[obj], keys={obj: [key]},
                    delete_after=bool(delete_after), overwrite=True)
            except bridgemod.BridgeError as exc:
                state["failed"].append("%s (%s)" % (key, exc))
                # Straight on to the next one rather than unwinding: see the
                # docstring — a long batch must survive one bad key.
                return QTimer.singleShot(0, step)
            state["saved"] += 1
            self._capture_for_batch(result.get("path"), item_name, step)

        QTimer.singleShot(0, step)

    def _capture_for_batch(self, path, label, then, vgroups=False):
        """Capture one item's preview, then continue the batch either way.

        ⚠ `then` runs on success AND on failure. A preview that could not be
        rendered is a missing thumbnail, not a reason to abandon the nineteen
        keys queued behind it.
        """
        if not path:
            return QTimer.singleShot(0, then)
        worker = CaptureWorker(self.bridge, path, None, parent=self,
                               vgroups=vgroups)
        self._batch_capture = worker
        worker.done.connect(lambda _r: QTimer.singleShot(0, then))
        worker.failed.connect(lambda _e: QTimer.singleShot(0, then))
        worker.start()

    def _start_vgroup_capture(self, path, label, base_msg=""):
        """Weight-paint preview for a .vgroups item.

        ⚠ Blocked cleanly on an add-on too old for it, rather than falling back
        to `capture_preview`: a plain grey viewport shot where weight colours
        are expected reads as "the weights did not save". The item is already
        on disk and correct — only the picture is missing, and the message says
        which.
        """
        reason = self.bridge.feature_reason("vgroup_preview")
        if reason:
            self.window.statusBar().showMessage(
                ((base_msg + "  —  ") if base_msg else "")
                + "no weight-paint preview: " + reason, 12000)
            return
        prefix = (base_msg + "  —  ") if base_msg else ""
        self.window.begin_capture(label)
        self.window.statusBar().showMessage(
            prefix + "Capturing weight-paint preview for '%s'…" % label)
        worker = CaptureWorker(self.bridge, path, None, parent=self,
                               vgroups=True)
        self._capture = worker

        def _done(result):
            self._capture = None
            self.window.end_capture()
            self.rescan()
            msg = prefix + "Preview captured for '%s'" % label
            if result.get("capped"):
                msg += " (first %d groups)" % result.get("groups", 0)
            self.window.statusBar().showMessage(msg, 6000)

        def _failed(err):
            self._capture = None
            self.window.end_capture()
            self.rescan()  # the item itself is saved either way
            self.window.statusBar().showMessage(
                prefix + "⚠ preview capture failed (%s) — use the 📷 button"
                % err, 10000)

        worker.done.connect(_done)
        worker.failed.connect(_failed)
        worker.start()

    def _start_capture(self, path, frames, label, base_msg="", shape_steps=None,
                       after=None):
        """Kick off a background preview capture; UI that talks to the bridge
        is greyed out until it finishes (window.begin/end_capture). `after` is
        called with True/False once the capture succeeds/fails — used for work
        that must wait for the capture (e.g. vault-move key deletion)."""
        prefix = (base_msg + "  —  ") if base_msg else ""
        self.window.begin_capture(label)
        self.window.statusBar().showMessage(
            prefix + "Capturing preview for '%s'…" % label)
        worker = CaptureWorker(self.bridge, path, frames,
                               shape_steps=shape_steps, parent=self)
        self._capture = worker

        def _done(_result):
            self._capture = None
            self.window.end_capture()
            self.rescan()
            self.window.statusBar().showMessage(
                prefix + "Preview captured for '%s'" % label, 6000)
            if after is not None:
                after(True)

        def _failed(err):
            self._capture = None
            self.window.end_capture()
            self.rescan()  # the item itself is saved either way
            self.window.statusBar().showMessage(
                prefix + "⚠ preview capture failed (%s) — use the 📷 button" % err, 10000)
            if after is not None:
                after(False)

        worker.done.connect(_done)
        worker.failed.connect(_failed)
        worker.start()

    # --------------------------------------------------- context menu

    def on_context_menu(self, item, global_pos):
        menu = QMenu(self)
        apply_label = {"set": "Select Bones", "mirror": "Use Table",
                       "shapes": "Add Keys to Mesh", "abc": "Import",
                       "playblast": "Play",
                       "vgroups": "Restore Groups (exact)",
                       "renderpreset": "Apply Render Settings",
                       "remap": "Use Remap"}.get(item.type, "Apply")
        act_apply = menu.addAction(apply_label)
        # ⚠ Transfer is its own entry, never folded into Apply. Restoring is
        # exact and index-based; transferring is a spatial ESTIMATE that needs
        # cleanup. One button for both is how wrong weights ship quietly.
        act_transfer = None
        if item.type == "vgroups":
            act_transfer = menu.addAction("Transfer to Active Mesh "
                                          "(approximate)…")
        act_mirror = act_sel = None
        if item.type in ("pose", "anim"):
            act_mirror = menu.addAction("Apply Mirrored")
            act_sel = menu.addAction("Apply to Selected Bones")
        menu.addSeparator()
        act_prev = None
        # ⚠ `renderpreset` joins the no-preview list: a viewport shot of
        # whatever happens to be on screen says nothing about a set of render
        # settings, and an item that CAN be given a meaningless picture is an
        # item someone will give one to.
        if item.type not in ("mirror", "remap", "renderpreset") and not item.bare:
            act_prev = menu.addAction("Update Preview  📷")
        act_wrap = None
        if item.bare and item.type == "abc":  # only .abc files can be wrapped
            act_wrap = menu.addAction("Convert to Library Item")
        act_flips = None
        if item.type == "mirror":
            act_flips = menu.addAction("Prop Flips…")
        act_ren = menu.addAction("Rename…")
        act_ver = act_tags = None
        color_acts = {}
        if not item.bare:  # tags/versions/labels live inside an item folder
            act_ver = menu.addAction("Versions…")
            act_tags = menu.addAction("Edit Tags…")
            color_menu = menu.addMenu("Color Label")
            for cname, chex in theme.LABEL_COLORS:
                a = color_menu.addAction(_swatch(chex), cname)
                color_acts[a] = chex
            a = color_menu.addAction("None")
            color_acts[a] = None
        act_open = menu.addAction("Open in Explorer")
        n = len(self.grid.selected_library_items())
        # Marty, 2026-08-04: select several, right-click, zip them for sharing.
        # The count is in the label so it is obvious the whole SELECTION is
        # going in, not just the item that was right-clicked.
        act_zip = menu.addAction("Zip %d Items for Sharing…" % n if n > 1
                                 else "Zip for Sharing…")
        menu.addSeparator()
        act_del = menu.addAction("Delete %d Items" % n if n > 1 else "Delete")
        if self.window.capturing:  # bridge is busy rendering a preview
            for a in (act_apply, act_mirror, act_sel, act_prev):
                if a is not None:
                    a.setEnabled(False)
        chosen = menu.exec(global_pos)
        if chosen is None:
            return
        if chosen is act_apply:
            self.on_apply(item, self.info.options())
        elif chosen is act_transfer:
            self.transfer_vgroups(item)
        elif chosen is act_mirror:
            opts = dict(self.info.options())
            opts["mirror"] = True
            self.on_apply(item, opts)
        elif chosen is act_sel:
            opts = dict(self.info.options())
            opts["selected_only"] = True
            self.on_apply(item, opts)
        elif chosen is act_prev:
            self.on_recapture(item)
        elif chosen is act_wrap:
            self.wrap_abc(item)
        elif chosen is act_flips:
            self.edit_prop_flips(item)
        elif chosen is act_ren:
            self.rename_item(item)
        elif chosen is act_ver:
            VersionsDialog(self, item).exec()
        elif chosen is act_tags:
            self.edit_tags(item)
        elif chosen in color_acts:
            self.set_color_label(color_acts[chosen])
        elif chosen is act_open:
            self.open_in_explorer(item)
        elif chosen is act_zip:
            self.zip_items(item)
        elif chosen is act_del:
            self.on_delete()

    def rename_item(self, item):
        name, ok = QInputDialog.getText(self, "Rename", "New name:", text=item.name)
        if not ok:
            return
        # same sanitization as the add-on's safe_name()
        name = re.sub(r'[<>:"/\\|?*]', "_", name).strip().rstrip(".")
        if not name or name == item.name:
            return
        # bare items keep their real file extension (.mp4 playblasts etc.)
        ext = os.path.splitext(item.path)[1] if item.bare else "." + item.type
        new_path = os.path.join(os.path.dirname(item.path), name + ext)
        if os.path.exists(new_path):
            QMessageBox.warning(self, "Rename",
                                "'%s' already exists in this folder." % name)
            return
        try:
            os.rename(item.path, new_path)
        except OSError as exc:
            QMessageBox.warning(self, "Rename", str(exc))
            return
        if self.active_mirror == item.path:
            self.active_mirror = new_path
            self.info.mirror_label.setText("Mirror table: %s" % name)
        if self.active_remap == item.path:
            self._set_active_remap(new_path)
        self.rescan()
        self.window.statusBar().showMessage(
            "Renamed '%s' to '%s'" % (item.name, name), 5000)

    def edit_prop_flips(self, item):
        """Mirror tables only: numeric custom props whose name matches one of
        these patterns get NEGATED when a pose/anim is mirrored through the
        table (twist-style props). Written straight into mirror.json."""
        data = item.read_data()
        cur = ", ".join(data.get("prop_flips", []))
        text, ok = QInputDialog.getText(
            self, "Prop Flips — %s" % item.name,
            "Custom-prop name patterns to negate on mirror\n"
            "(comma-separated, * wildcards, case-insensitive — e.g. *twist*):",
            text=cur)
        if not ok:
            return
        pats = []
        for t in text.split(","):
            t = t.strip()
            if t and t not in pats:
                pats.append(t)
        data["prop_flips"] = pats
        try:
            with open(os.path.join(item.path, "mirror.json"), "w",
                      encoding="utf-8") as f:
                json.dump(data, f, indent=1)
        except OSError as exc:
            QMessageBox.warning(self, "Prop Flips", str(exc))
            return
        self.window.statusBar().showMessage(
            "Prop flips for '%s': %s" % (item.name, ", ".join(pats) or "none"),
            6000)

    def open_in_explorer(self, item):
        desktop.reveal_in_folder(item.path)

    def import_flow(self):
        """The ⬇ Import button. No bridge, on purpose: bringing items into a
        library is disk work, and it must work with Blender closed."""
        dialog = ImportDialog(self, self.lib_cfg["path"], self.folders,
                              self.sidebar.current_folder() or "")
        if dialog.exec() != QDialog.Accepted or not dialog.candidates:
            return
        report = importer.run(dialog.candidates, self.lib_cfg["path"],
                              dialog.dest_folder())
        self.rescan()
        if report["failed"]:
            QMessageBox.warning(
                self, "Import",
                report["summary"] + "\n\n"
                + "\n".join("%s — %s" % (f["name"], f["reason"])
                            for f in report["failed"][:8]))
        self.window.statusBar().showMessage(report["summary"], 8000)

    def zip_items(self, clicked=None):
        """Zip the selected items into one archive for sharing.

        ⚠ READ-ONLY on the library. Nothing is moved, renamed or deleted — this
        copies bytes out to a file the user names, and a failure part-way leaves
        the library exactly as it was.

        Entry names are `<item name>/...` relative to each item's own folder, so
        an archive can be unzipped straight into a library folder and the items
        land as items. A bare file (a loose .abc, a playblast) goes in at the
        top level, because it has no folder to preserve.
        """
        items = self.grid.selected_library_items()
        if not items and clicked is not None:
            items = [clicked]
        if not items:
            return
        default = (items[0].name if len(items) == 1
                   else "%s_%d_items" % (self.lib_cfg.get("name", "library"),
                                         len(items)))
        target, _filter = QFileDialog.getSaveFileName(
            self, "Zip %d item(s) for sharing" % len(items),
            os.path.join(os.path.expanduser("~"), default + ".zip"),
            "Zip archive (*.zip)")
        if not target:
            return
        if not target.lower().endswith(".zip"):
            target += ".zip"

        written = 0
        skipped = []
        try:
            with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
                for entry in items:
                    path = entry.path
                    if os.path.isdir(path):
                        for folder, _dirs, files in os.walk(path):
                            for name in files:
                                full = os.path.join(folder, name)
                                rel = os.path.relpath(full, os.path.dirname(path))
                                archive.write(full,
                                              rel.replace(os.sep, "/"))
                        written += 1
                    elif os.path.isfile(path):
                        archive.write(path, os.path.basename(path))
                        written += 1
                    else:
                        skipped.append(entry.name)
        except OSError as exc:
            QMessageBox.warning(self, "Could not write the zip", str(exc))
            return

        note = "Zipped %d item(s) to %s" % (written, os.path.basename(target))
        if skipped:
            # Named, not counted: "3 of 4" with no names leaves someone to work
            # out which one is missing from a zip they have already sent.
            note += "  —  skipped (not on disk): " + ", ".join(skipped)
        self.window.statusBar().showMessage(note, 8000)

    def edit_tags(self, item):
        text, ok = QInputDialog.getText(self, "Edit Tags",
                                        "Tags for '%s' (comma-separated):" % item.name,
                                        text=", ".join(item.tags))
        if not ok:
            return
        tags = []
        for t in text.split(","):
            t = t.strip()
            if t and t not in tags:
                tags.append(t)
        item.tags = tags
        try:
            item.save_tags()
        except OSError as exc:
            QMessageBox.warning(self, "Edit Tags", str(exc))
            return
        self.rescan()  # sidebar tag counts + info panel refresh

    def set_color_label(self, color):
        items = self.grid.selected_library_items()
        errors = []
        for it in items:
            it.color = color
            try:
                it.save_tags()
            except OSError as exc:
                errors.append("%s: %s" % (it.name, exc))
        # ⚠ refilter() no longer re-renders tiles (it only hides rows), so
        # the new label strip must be stamped explicitly — same move as
        # on_video_preview_ready. The colour is part of the pixmap cache key,
        # so this derives a fresh tile rather than serving the old one.
        self.grid.refresh_icons(items)
        if errors:
            QMessageBox.warning(self, "Color Label",
                                "Some items failed:\n" + "\n".join(errors))

    # --------------------------------------------------- drag-drop move

    def on_items_dropped(self, paths, rel_folder):
        if self.window.capturing:  # a capture may be writing into one of these
            self.window.statusBar().showMessage(
                "Blender is capturing a preview — move items when it finishes", 4000)
            return
        dest = os.path.join(self.lib_cfg["path"], rel_folder or "")
        import shutil
        moved, skipped = 0, []
        for src in paths:
            base = os.path.basename(src.rstrip("\\/"))
            target = os.path.join(dest, base)
            if os.path.normcase(os.path.abspath(target)) == \
               os.path.normcase(os.path.abspath(src)):
                continue  # dropped into its own folder
            if os.path.exists(target):
                skipped.append(base)
                continue
            try:
                shutil.move(src, target)
            except OSError as exc:
                skipped.append("%s (%s)" % (base, exc))
                continue
            if self.active_mirror == src:
                self.active_mirror = target
            if self.active_remap == src:
                self._set_active_remap(target)
            moved += 1
        self.rescan()
        msg = "Moved %d item(s) to %s" % (moved, rel_folder or "root")
        if skipped:
            msg += "  ⚠ skipped: %s" % ", ".join(skipped)
        self.window.statusBar().showMessage(msg, 8000)

    def move_folder(self, rel, target_rel):
        """Sidebar folder drag: move a folder (items + subfolders) into another
        folder, or onto 'All' = the library root."""
        if self.window.capturing:  # a capture may be writing inside this folder
            self.window.statusBar().showMessage(
                "Blender is capturing a preview — move folders when it finishes", 4000)
            return
        if target_rel and (target_rel == rel or target_rel.startswith(rel + "/")):
            return  # into itself/its subtree (the tree blocks this already)
        name = rel.rsplit("/", 1)[-1]
        # normpath: rel uses '/', item paths use '\' — startswith needs one form
        src = os.path.normpath(os.path.join(self.lib_cfg["path"], rel))
        dst = os.path.normpath(os.path.join(self.lib_cfg["path"],
                                            target_rel or "", name))
        if not os.path.isdir(src):
            self.rescan()  # folder vanished outside the app
            return
        if os.path.exists(dst):
            QMessageBox.warning(self, "Move Folder",
                                "'%s' already exists in %s." % (name, target_rel or "root"))
            return
        import shutil
        try:
            shutil.move(src, dst)
        except OSError as exc:
            QMessageBox.warning(self, "Move Folder", str(exc))
            self.rescan()
            return
        # active tables that lived inside the moved folder follow it
        if self.active_mirror and self.active_mirror.startswith(src + os.sep):
            self.active_mirror = dst + self.active_mirror[len(src):]
        if self.active_remap and self.active_remap.startswith(src + os.sep):
            self._set_active_remap(dst + self.active_remap[len(src):])
        new_rel = (target_rel + "/" + name) if target_rel else name
        self.rescan()
        self.sidebar.select_folder(new_rel)
        self.window.statusBar().showMessage(
            "Moved folder '%s' to %s" % (name, target_rel or "root"), 5000)

    # --------------------------------------------------- live blending

    def _selected_item(self):
        sel = self.grid.selectedItems()
        from PySide6.QtCore import Qt as _Qt
        return sel[0].data(_Qt.UserRole) if sel else None

    def on_blend_start(self):
        item = self._selected_item()
        if item is None or item.type != "pose" or not self._bridge_free():
            return
        try:
            r = self.streamer.start(item.path,
                                    selected_only=self.info.options()["selected_only"])
            self.window.statusBar().showMessage(
                "Live blending '%s' (%d bones)…" % (item.name, r["bones"]))
        except bridgemod.BridgeError as exc:
            self.window.statusBar().showMessage("Live blend unavailable: %s" % exc, 5000)

    def on_blend_change(self, value):
        if self.streamer.active:
            self.streamer.set_value(value)

    def on_blend_end(self):
        if not self.streamer.active:
            return
        try:
            r = self.streamer.stop(keep=True, key=self.info.options()["key"])
            msg = "Blend applied"
            if r and r.get("keyed"):
                msg += " (keyed %d bones)" % r["keyed"]
            self.window.statusBar().showMessage(msg, 5000)
        except bridgemod.BridgeError as exc:
            self.window.bridge_error(exc)

    def delete_folder(self, rel):
        """Del key on a sidebar folder: confirm, then remove the folder and
        everything inside it."""
        if self.window.capturing:  # a capture may be writing inside this folder
            self.window.statusBar().showMessage(
                "Blender is capturing a preview — delete folders when it finishes", 4000)
            return
        name = rel.rsplit("/", 1)[-1]
        n_items = sum(1 for it in self.items
                      if it.folder == rel or it.folder.startswith(rel + "/"))
        n_subs = sum(1 for f in self.folders if f.startswith(rel + "/"))
        detail = []
        if n_items:
            detail.append("%d item(s)" % n_items)
        if n_subs:
            detail.append("%d subfolder(s)" % n_subs)
        what = (" and its " + " and ".join(detail)) if detail else " (empty)"
        answer = QMessageBox.question(
            self, "Delete Folder",
            "Permanently delete folder '%s'%s?" % (name, what),
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer != QMessageBox.Yes:
            return
        # normpath: rel uses '/', item paths use '\' — startswith needs one form
        full = os.path.normpath(os.path.join(self.lib_cfg["path"], rel))
        import shutil
        try:
            shutil.rmtree(full)
        except OSError as exc:
            QMessageBox.warning(self, "Delete Folder", str(exc))
            self.rescan()
            return
        # active tables that lived inside the folder are gone with it
        if self.active_mirror and self.active_mirror.startswith(full + os.sep):
            self.active_mirror = None
            self.info.mirror_label.setText("Mirror table: auto-detect")
        if self.active_remap and self.active_remap.startswith(full + os.sep):
            self._set_active_remap(None)
            self.info.chk_remap.setChecked(False)
        parent = rel.rsplit("/", 1)[0] if "/" in rel else None
        self.rescan()
        if parent:
            self.sidebar.select_folder(parent)
        self.info.show_item(None)
        self.window.statusBar().showMessage("Deleted folder '%s'" % name, 5000)

    def rename_folder(self, rel, new_name):
        """Commit of the sidebar's inline folder edit. Always ends in rescan()
        on the early-outs too — the node label was stripped to the bare name
        for editing and rescan restores the '(count)' suffix."""
        old_name = rel.rsplit("/", 1)[-1]
        parent = rel.rsplit("/", 1)[0] if "/" in rel else ""
        name = re.sub(r'[<>:"/\\|?*]', "_", new_name).strip().rstrip(".")
        if self.window.capturing:  # a capture may be writing inside this folder
            self.window.statusBar().showMessage(
                "Blender is capturing a preview — rename folders when it finishes", 4000)
            self.rescan()
            return
        if not name or name == old_name:
            self.rescan()
            return
        # normpath: rel uses '/', item paths use '\' — startswith needs one form
        src = os.path.normpath(os.path.join(self.lib_cfg["path"], rel))
        dst = os.path.normpath(os.path.join(self.lib_cfg["path"], parent, name))
        if os.path.exists(dst):
            QMessageBox.warning(self, "Rename Folder",
                                "'%s' already exists here." % name)
            self.rescan()
            return
        try:
            os.rename(src, dst)
        except OSError as exc:
            QMessageBox.warning(self, "Rename Folder", str(exc))
            self.rescan()
            return
        if self.active_mirror and self.active_mirror.startswith(
                src + os.sep):  # active table lived inside the renamed folder
            self.active_mirror = dst + self.active_mirror[len(src):]
        if self.active_remap and self.active_remap.startswith(src + os.sep):
            self._set_active_remap(dst + self.active_remap[len(src):])
        new_rel = (parent + "/" + name) if parent else name
        self.rescan()
        self.sidebar.select_folder(new_rel)
        self.window.statusBar().showMessage(
            "Renamed folder '%s' to '%s'" % (old_name, name), 5000)

    def _persist_zoom(self):
        cfg = self.window.cfg
        cfg["icon_size"] = self.zoom.value()
        config.save(cfg)

    def new_folder(self, base=None):
        """+ Folder button (base=None → current folder) or empty-space
        right-click in the sidebar (base='' → library root)."""
        if base is None:
            base = self.sidebar.current_folder() or ""
        name, ok = QInputDialog.getText(self, "New Folder",
                                        "Folder name (inside %s):" % (base or "root"))
        if not ok or not name.strip():
            return
        path = os.path.join(self.lib_cfg["path"], base, name.strip())
        try:
            os.makedirs(path, exist_ok=True)
        except OSError as exc:
            QMessageBox.warning(self, "New Folder", str(exc))
            return
        self.rescan()


class _DeadBridge:
    """A bridge that answers nothing, for building the lock-screen preview.

    Any call returns a failure straight away, so a tool constructed for the
    preview cannot reach Blender even once - no probing the live scene, no
    commands, no waiting on a socket. The catch-all is generic on purpose: the
    tools call a dozen different helpers between them and this must cover the
    ones written later too.

    The named members exist because they are NOT commands and have their own
    contracts — `capabilities` is a list, and `feature_reason` returns a string
    or None. Handing those a failure dict is how the first version of this
    crashed the Physics preview."""

    def __init__(self):
        self.capabilities = []
        self.addon_version = None
        self.port = 0

    def feature_reason(self, _feature):
        # None means "available, or we don't know yet" — so the preview shows
        # each tool the way it normally looks, not greyed out.
        return None

    def request(self, *args, **kwargs):
        return {"ok": False, "error": "locked"}

    def __getattr__(self, _name):
        return lambda *args, **kwargs: {"ok": False, "error": "locked"}


class SectionTabBar(QTabBar):
    """The outer tab strip, able to tint a tab by NAME rather than by position.

    ⚠ THIS EXISTS BECAUSE QT STYLESHEETS CANNOT SELECT A TAB BY INDEX — only
    `:first`, `:last` and `:selected`. The NSFW Tools tint used to be
    `QTabBar::tab:last`, which did not mean "NSFW Tools", it meant "whichever
    tab happens to be last"; the old comment in theme.py said as much and warned
    that adding a tab would steal the colour. Marty reordered the tabs on
    2026-08-04 and NSFW Tools stopped being last, so the pink would have moved
    to Rendering on its own. Painting by name makes the order free to change.

    Only the tinted tab is painted by hand, and only its BACKGROUND: everything
    else still goes through `CE_TabBarTab`, so the stylesheet keeps owning the
    strip's whole look. A selected tab is left alone — the selected background
    and the accent underline are what tell you where you are, and losing them
    on one tab would be a worse bug than a missing tint.
    """

    def __init__(self, tints=None, premium=(), parent=None):
        super().__init__(parent)
        self._tints = dict(tints or {})
        self._premium = set(premium)

    @staticmethod
    def _star(center, radius):
        """A five-pointed star as a path.

        Drawn rather than written as "★": glyph coverage varies by font and a
        missing one renders as a tofu box, which would look like a bug rather
        than like a badge.
        """
        path = QPainterPath()
        for step in range(10):
            angle = -math.pi / 2 + step * math.pi / 5
            reach = radius if step % 2 == 0 else radius * 0.45
            point = QPointF(center.x() + reach * math.cos(angle),
                            center.y() + reach * math.sin(angle))
            if step:
                path.lineTo(point)
            else:
                path.moveTo(point)
        path.closeSubpath()
        return path

    def paintEvent(self, _event):
        painter = QStylePainter(self)
        option = QStyleOptionTab()
        for index in range(self.count()):
            self.initStyleOption(option, index)
            color = self._tints.get(self.tabText(index))
            if color and not (option.state & QStyle.State_Selected):
                painter.fillRect(option.rect, QColor(color))
                painter.drawControl(QStyle.CE_TabBarTabLabel, option)
            else:
                painter.drawControl(QStyle.CE_TabBarTab, option)
            if self.tabText(index) in self._premium:
                # Top-right corner, inside the 22 px of horizontal padding the
                # stylesheet gives every tab, so it never crowds the label.
                rect = option.rect
                painter.save()
                painter.setRenderHint(QPainter.Antialiasing, True)
                painter.fillPath(
                    self._star(QPointF(rect.right() - 8.5, rect.top() + 9.5),
                               4.2),
                    QColor(theme.PREMIUM_MARK))
                painter.restore()


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        # ⚠ The QSS has no universal QWidget rule since PERF_PLAN B — an
        # unstyled widget's ground and font come from the APP defaults, so a
        # window built without them (every test that only set the stylesheet)
        # would paint on Qt's stock light palette. Idempotent; main() and the
        # theme switch apply them too.
        _app = QApplication.instance()
        if _app is not None:
            theme.apply_app_defaults(_app)
        self._previewing = False
        self.cfg = config.load()
        self.bridge = bridgemod.Bridge(port=self.cfg.get("port", 9877))
        self.setWindowTitle(APP_NAME)
        self.resize(1180, 720)

        # ⚠ BEFORE the first `self.statusBar()` anywhere, or QMainWindow makes a
        # plain QStatusBar and keeps it — setStatusBar afterwards would replace a
        # bar that other code already holds references into. Ours carries the
        # version in the bottom-left corner permanently; `widgets.StatusBar`
        # explains why a stock QStatusBar cannot (2026-08-08).
        self.setStatusBar(widgets.StatusBar(self))
        self.statusBar().set_version("v" + version.APP_VERSION)
        self.statusBar().version_label.setToolTip(
            "%s %s\nQuote this version in any bug report."
            % (APP_NAME, version.APP_VERSION))

        # one shared extractor: playblast frames are decoded by the OS codec and
        # running several at once just thrashes. Created BEFORE the tabs, since
        # each LibraryView rescans (and queues previews) while being built.
        self.video_previews = video_preview.VideoPreviewQueue(self)
        self.video_previews.ready.connect(self._on_video_preview_ready)
        video_preview.purge_stale()

        # inner tabs = one per library root (self.tabs stays the LIBRARY tabs:
        # captures, persistence and the smoke test all iterate it)
        self.tabs = QTabWidget()
        self.tabs.setTabsClosable(True)
        self.tabs.tabCloseRequested.connect(self.close_tab)
        corner = QPushButton()
        icons.button_icon(corner, "plus", 15)
        corner.setObjectName("flat")
        corner.setToolTip("Add a library tab")
        corner.clicked.connect(self.add_library_dialog)
        self.tabs.setCornerWidget(corner, Qt.TopRightCorner)

        for lib in self.cfg["libraries"]:
            self.tabs.addTab(LibraryView(lib, self.bridge, self), lib["name"])
        self.tabs.setCurrentIndex(min(self.cfg.get("current_tab", 0),
                                      self.tabs.count() - 1))
        self.tabs.currentChanged.connect(self._persist)

        # outer tabs = the toolset's sections
        self.main_tabs = QTabWidget()
        self.main_tabs.setObjectName("maintabs")
        self.main_tabs.setTabBar(SectionTabBar(
            theme.TAB_TINTS,
            premium=()))
        library_page = QWidget()
        lp = QVBoxLayout(library_page)
        lp.setContentsMargins(0, 0, 0, 0)
        lp.addWidget(self.tabs)
        self.main_tabs.addTab(library_page, "Studio Library")
        # What's New. Ships inside the build, so the release notes are readable
        # with no internet — which is the one moment release notes are worth
        # anything. Added at the END, which is free to do now the NSFW tint
        # follows a NAME rather than the last position (see SectionTabBar).
        self.updates = updates_mod.UpdatesPage(self)
        # Anim Layers settings mirror. False means "we have not agreed with
        # Blender yet", which is what makes the app's copy win on first contact.
        self._prefs_synced = False
        self._prefs_pushed_at = 0.0
        # The add-on's licence gate is session-scoped, so this is re-pushed on
        # every connect rather than once at startup.
        self.rendering = None
        self.node_setup = None
        self.anim_layers = None
        self.physics = None
        self.nsfw = None
        self.nodeeditor = None
        self.madiref = None
        self.texmaps = None
        self.picker = None
        self.picker_tabs_tool = None
        self.picker_buttons_tool = None
        self.picker_presets_tool = None
        self.picker_options_tool = None
        self.optimizer = None
        self.optimizer_adaptive_tool = None
        self.optimizer_fixed_tool = None
        self.optimizer_meshes_tool = None
        self.optimizer_restore_tool = None
        self.optimizer_memory_tool = None
        self.affector_torus_tool = None
        self.render_queue = None
        self.layers_page = None
        self.layer_options = None
        self.markers_tool = None
        self.bone_jiggle_tool = None
        # The tool tabs, in order. Every one is built unconditionally: since
        # 1.19.0 there is no other kind.
        self._lazy_hosts = {}
        for key, title in self.FREE_TOOLS:
            if key in self.LAZY_TOOLS:
                # ⚠ LAZY (PERF_PLAN C): an empty host holds the tab's place;
                # the real page is built by _ensure_tab_built on first open.
                # 79 % of startup widgets belonged to tabs nobody had opened.
                host = QWidget()
                host_lay = QVBoxLayout(host)
                host_lay.setContentsMargins(0, 0, 0, 0)
                self._lazy_hosts[host] = key
                self.main_tabs.addTab(host, title)
            else:
                self.main_tabs.addTab(getattr(self, "_build_" + key)(), title)
        self.main_tabs.addTab(self.updates, "What's New")
        self._apply_tab_colors()

        # ---- the left rail takes over navigation -----------------------
        # ⚠ The tab bar is HIDDEN, not removed. Everything that reads the tab
        # widget keeps working (see widgets.SectionRail), and the tab TEXT stays
        # the internal key while the rail carries the label people read.
        self.main_tabs.tabBar().hide()
        self.section_rail = widgets.SectionRail(
            theme.TAB_TINTS,
            premium=())
        for index in range(self.main_tabs.count()):
            title = self.main_tabs.tabText(index)
            key, group = SECTION_META.get(title, ("", ""))
            self.section_rail.add_section(index, title, key, group)
        self.section_rail.sectionChanged.connect(self.main_tabs.setCurrentIndex)
        self.main_tabs.currentChanged.connect(
            self.section_rail.set_current_index)
        # ⚠ AGAIN, now the rail exists. The first call happened while the tabs
        # were being built, so it could only reach the (hidden) tab bar — and
        # `TAB_TEXT_COLORS` is Marty's own pick, which would have been applied
        # exclusively to a widget nobody can see.
        self._apply_tab_colors()

        # ---- our own title bar, in place of Windows' ---------------------
        # Two columns, each with its own top piece, so the rail's surface runs
        # unbroken from the app mark to the last tool and only the strip beside
        # it is chrome (chrome.py explains why this is not FramelessWindowHint).
        self.rail_header = chrome_mod.RailHeader()
        self.title_strip = chrome_mod.TitleStrip(self)

        rail_column = QWidget()
        rail_box = QVBoxLayout(rail_column)
        rail_box.setContentsMargins(0, 0, 0, 0)
        rail_box.setSpacing(0)
        rail_box.addWidget(self.rail_header)
        rail_box.addWidget(self.section_rail, 1)

        page_column = QWidget()
        page_box = QVBoxLayout(page_column)
        page_box.setContentsMargins(0, 0, 0, 0)
        page_box.setSpacing(0)
        page_box.addWidget(self.title_strip)
        page_box.addWidget(self.main_tabs, 1)

        shell = QWidget()
        shell_layout = QHBoxLayout(shell)
        shell_layout.setContentsMargins(0, 0, 0, 0)
        shell_layout.setSpacing(0)
        shell_layout.addWidget(rail_column)
        shell_layout.addWidget(page_column, 1)

        # ⚠ ORDER MATTERS on one signal: the lazy build must run before the
        # section sync and the input-filter re-walk look at the page.
        self.main_tabs.currentChanged.connect(self._ensure_tab_built)
        self.main_tabs.currentChanged.connect(self._sync_title_section)
        self.main_tabs.currentChanged.connect(self._refilter_page)

        self.main_tabs.setCurrentIndex(min(self.cfg.get("main_tab", 0),
                                           self.main_tabs.count() - 1))
        # ⚠ setCurrentIndex(0 → 0) emits nothing, and the restored tab might BE
        # a lazy one — make sure whatever is on screen at startup is real.
        self._ensure_tab_built(self.main_tabs.currentIndex())
        # ⚠ AFTER setCurrentIndex, and unconditionally: index 0 restored from
        # config fires no currentChanged, so the rail would open with the
        # Studio Library page showing and nothing selected beside it.
        self.section_rail.set_current_index(self.main_tabs.currentIndex())
        self.main_tabs.currentChanged.connect(self._persist)
        self.setCentralWidget(shell)
        self._sync_title_section(self.main_tabs.currentIndex())

        # ⚠ Both bar halves are drag handles, and the header is listed first:
        # dragging the window by the app mark is the thing everybody tries.
        # ⚠ ONE WALK, HERE, INSTEAD OF A FILTER ON THE QApplication. The wheel
        # guard used to be app-wide, which taxed every event in the process —
        # 96,515 Python callbacks to build this window, 380 ms of it. This
        # guards the 75 widgets that can actually be edited by a wheel. Dialogs
        # guard themselves (`widgets.GuardedDialog`); a tab built later must
        # call this again for its own page (`widgets.guard_wheel`).
        widgets.attach_input_filters(self)

        self.custom_chrome = chrome_mod.install(
            self, (self.title_strip, self.rail_header))
        if not self.custom_chrome:
            # The native title bar is still there, so a second set of window
            # buttons would be two ways to close one window. The strip stays —
            # it is where you are, which is worth its own row either way.
            self.title_strip.controls.hide()

        self._captures = 0
        self.capture_progress = QProgressBar()
        self.capture_progress.setRange(0, 0)  # indeterminate: the bridge call is one blocking request
        self.capture_progress.setFixedWidth(110)
        self.capture_progress.hide()
        self.statusBar().addPermanentWidget(self.capture_progress)
        # ⚠ ELIDED. This label carries the connected .blend AND the active
        # object, so a long filename made the whole WINDOW unnarrowable —
        # the same disease as the Node Editor's toolbar hint (2026-08-08),
        # but dynamic, so it only appeared once a real scene was connected.
        # A status label is exactly where a minimum width must never come
        # from; the full text stays in the tooltip that is already set.
        self.bridge_label = widgets.ElidedLabel("●", minimum=110)
        self.statusBar().addPermanentWidget(self.bridge_label)
        self._status_worker = None      # in-flight health check (off-thread)
        # The .blend the bridge last answered from. None = never connected, so
        # the first connect is not reported as a move (see _note_connected_file).
        self._connected_file = None
        self._status_timer = QTimer(self)
        self._status_timer.timeout.connect(self.update_bridge_status)
        self._status_timer.start(FAST_STATUS_MS)
        self.update_bridge_status()
        self.apply_auto_refresh()

        # Developer console: the button lives in the status bar and only exists
        # when the setting is on. The recorder itself started at import.
        # ⚙ Settings, reachable from EVERY tab (Marty, 2026-08-04). The library
        # toolbar has its own copy, but that one only exists on the Studio
        # Library tab — so from anywhere else the settings, and with them the
        # add-on installer, could not be opened at all without switching tabs.
        self.settings_button = QPushButton()
        icons.button_icon(self.settings_button, "gear", 15)
        self.settings_button.setObjectName("flat")
        self.settings_button.setToolTip("Settings (applies to the whole app)")
        self.settings_button.clicked.connect(self.show_library_settings)
        self.statusBar().addPermanentWidget(self.settings_button)

        # ⚠ The add-on update offered right where the warning is. The note that
        # the extension is out of date has always been in this status bar, but
        # acting on it meant knowing that ⚙ Library Settings has an installer
        # in it. Marty: "we need a button in the bottom where we get the
        # warning to click so it updates from there." Hidden until there is
        # actually something to update, so it is never just decoration.
        self.addon_update_button = QPushButton("Update add-on")
        self.addon_update_button.setObjectName("flat")
        self.addon_update_button.setStyleSheet("color: #d8c74f;")
        self.addon_update_button.setToolTip(
            "Install the Blender extension this app was built for")
        # ⚠⚠ THIS WENT TO `show_library_settings` UNTIL 2026-08-17 AND THAT WAS
        # THE BUG MARTY REPORTED as "it doesn't do anything at all". The button
        # says "Update add-on"; it opened a settings dialog and updated nothing,
        # leaving you to find "Install in Blender" inside it — the exact hunt
        # the comment above says this button exists to remove. `install_addon_now`
        # was written for this and **was never connected to anything** — an
        # orphaned handler, which no suite noticed because nothing tested the
        # button. `app_ui_test.py` now pins the connection.
        self.addon_update_button.clicked.connect(self.install_addon_now)
        self.addon_update_button.hide()
        self.statusBar().addPermanentWidget(self.addon_update_button)

        # ⓘ About: who made this, and where to go for help or to support it.
        # Deliberately next to the update controls rather than buried in
        # settings — the Discord link is the bug-report route, and someone with
        # a bug is looking at the bottom of the window already.
        self.about_button = QPushButton("ⓘ")
        self.about_button.setObjectName("flat")
        self.about_button.setToolTip("About, Discord and Patreon")
        self.about_button.clicked.connect(self.show_about)
        self.statusBar().addPermanentWidget(self.about_button)

        # "Check for updates" — free, and since 2026-08-06 so is INSTALLING one.
        # ⚠ NO "UPDATES" BUTTON SINCE 1.19.0 — nothing to check. Releases are
        # published on GitHub and unzipped over the folder by hand.
        self._console_dialog = None
        self.console_button = QPushButton("Console")
        self.console_button.setObjectName("flat")
        self.console_button.setToolTip(
            "Open the developer console (this session's errors and log)")
        self.console_button.clicked.connect(self.show_dev_console)
        self.console_button.hide()
        self.statusBar().addPermanentWidget(self.console_button)
        dev_console.BUFFER.appended.connect(self._on_log_line)
        self.apply_dev_console()

        # Buy me a coffee. In the status bar so it is present without being in
        # anyone's way — the Patreon link already existed but was buried in
        # the About dialog, where nobody looking to support the thing would
        # find it. Marty picked this look ("panel chip") from six variants
        # rendered as real buttons, 2026-08-17.
        # ⚠ Styled by `QPushButton#support` in theme.py, NOT by a stylesheet
        # set here: a hardcoded one would survive a theme swap and sit there in
        # the old palette. The heart is the one fixed colour, and it is red on
        # every theme by design.
        self.support_button = QPushButton("Buy me a coffee")
        self.support_button.setObjectName("support")
        self.support_button.setIcon(icons.icon("support", 15, "#e0574f"))
        self.support_button.setIconSize(QSize(15, 15))
        self.support_button.setCursor(Qt.PointingHandCursor)
        self.support_button.setToolTip(
            "Support the Toolset on Patreon — opens in your browser")
        self.support_button.clicked.connect(self.open_support_page)
        self.statusBar().addPermanentWidget(self.support_button)

        # Keep-on-top pin. Sits in the status bar next to the console button so
        # it is reachable from every tab without spending toolbar room.
        self.pin_button = QPushButton("📌 Pin")
        self.pin_button.setObjectName("flat")
        self.pin_button.setCheckable(True)
        self.pin_button.setToolTip(
            "Keep this window on top of every other application — so it stays "
            "visible over Blender while you work")
        self.pin_button.toggled.connect(self.set_always_on_top)
        self.statusBar().addPermanentWidget(self.pin_button)
        # Set the flag BEFORE the first show(): applied here it costs nothing,
        # whereas toggling it on a visible window makes Windows re-create it.
        if bool(self.cfg.get("always_on_top", False)):
            self.pin_button.setChecked(True)

        # Super focus (Marty, 2026-08-05) — the window under the mouse takes
        # focus, between this app and Blender ONLY. In the status bar next to
        # the pin because it is app-wide and gets flipped while working, not a
        # settings-dialog decision. Hidden outright where it cannot work.
        self.superfocus = superfocus.SuperFocus(self)
        self.superfocus_box = QCheckBox("Super focus")
        self.superfocus_box.setToolTip(
            "Focus follows the mouse, between this app and Blender only.\n"
            "Hover Blender and Blender takes focus; hover this window and it "
            "does — so a button press here never costs an extra click just to "
            "arrive.\nNothing else on the desktop is touched.")
        self.superfocus_box.toggled.connect(self.set_super_focus)
        self.statusBar().addPermanentWidget(self.superfocus_box)
        if not superfocus.available():
            self.superfocus_box.hide()
        elif bool(self.cfg.get("super_focus", False)):
            self.superfocus_box.setChecked(True)

        # ⚠ NO LICENCE CHIP AND NO UPDATE BUTTON SINCE 1.19.0. Both subsystems
        # were removed outright (Marty, 2026-08-15: "FULLY remove the updating
        # mechanics / mention of the server ... from now on people will have to
        # update themselves from the github"). The app makes NO network calls of
        # any kind now; new versions come from the GitHub releases page.
        #
        # What survived is `addon_push`, which is purely local — app to Blender,
        # using the copy of the extension packed into this build. It never had
        # anything to do with the server; it only lived in `updater\` because a
        # release could also carry an add-on.
        self.addon_pusher = addon_push.AddonPusher(self.bridge, self)
        self.addon_pusher.messageChanged.connect(self._on_addon_message)
        self.addon_pusher.stateChanged.connect(self._on_addon_state)

        # Developer edit: apply any saved renames LAST, once every widget above
        # exists. Gated tabs built later are covered by the filter's Show hook,
        # and the unlock path re-applies explicitly.
        devedit.set_enabled(bool(self.cfg.get("dev_edit", False)))
        devedit.apply_all(self)

    # ------------------------------------------------- the Blender add-on
    # ⚠ WHAT WAS HERE UNTIL 1.19.0: the whole self-update flow (popover
    # confirm, status-bar progress, restart-to-finish) and the licence chip.
    # Both are gone — the app no longer talks to any server, and new versions
    # come from the GitHub releases page. What remains is the one genuinely
    # local operation: pushing the add-on this build carries into Blender.

    def open_support_page(self):
        """Open the Patreon membership page in the user's default browser.

        ⚠ `QDesktopServices.openUrl` and nothing else — it hands the URL to the
        OS, which is what "default browser" means. Spawning a browser by name
        would pick the wrong one and fail on a machine that has not got it.
        """
        QDesktopServices.openUrl(QUrl(version.PATREON_MEMBERSHIP_URL))

    def _on_addon_message(self, text):
        if text:
            self.statusBar().showMessage(text, 8000)

    def _on_addon_state(self, state):
        # ⚠⚠ THIS LINE CALLED `clear_progress()` UNTIL 2026-08-17 AND NO SUCH
        # METHOD HAS EVER EXISTED — the status bar's is `hide_progress()`. The
        # slot raised AttributeError on the terminal state, so a **completely
        # successful** install left "Installing Blender add-on" spinning
        # forever (Marty: "it seems like it installed it but i still see
        # loading going forever" — it had installed, 0.49.0, reloaded).
        # ⚠ And the FAILED branch is BELOW it, so a genuine failure showed no
        # warning either: every outcome looked like a hang. Qt swallows an
        # exception in a slot to stderr, which a windowed build has nowhere to
        # print — **a GUI slot is the one place a typo'd method name is
        # invisible.** `app_ui_test.py` now drives the real states through it.
        if state == addon_push.INSTALLING:
            self.statusBar().show_progress("Installing Blender add-on")
            return
        self.statusBar().hide_progress()
        if state == addon_push.FAILED:
            QMessageBox.warning(self, "Blender add-on",
                                self.addon_pusher.message
                                or "The add-on was not installed.")

    def install_addon_now(self):
        """⚙ Settings ▸ Update add-on. Never silent: installing reloads the
        extension, which drops the bridge for a few seconds — not something to
        do to someone mid-save."""
        blocked = self.addon_pusher.block_reason()
        if blocked:
            QMessageBox.information(self, "Blender add-on", blocked)
            return
        import addon_bundle

        answer = QMessageBox.question(
            self, "Blender add-on",
            "Install Blender add-on %s? You have %s.\n\n"
            "Blender reloads the add-on to finish, so the connection drops "
            "for a few seconds.\n\nSave your work in Blender first."
            % (addon_bundle.VERSION,
               self.bridge.addon_version or "an older version"),
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer == QMessageBox.Yes:
            self.addon_pusher.install_bundled_addon()
    # ---------------------------------------------------------- gated tabs

    # Everything past Studio Library. A GATED entry's blurb is what a locked
    # tab tells someone who has not paid: they should be able to see exactly
    # what they would get, in their own words, not ours.
    # ⚠ GATED IS EMPTY SINCE 2026-08-14 — every tab is free. FREE_TOOLS
    # carries the whole strip order now, with What's New added after it.
    #
    # ⚠ RENDERING IS NOT IN HERE ANY MORE (Marty, 2026-08-05): it is the SECOND
    # tab and it is FREE. Adding it back would do two things at once — lock it
    # and move it — so if it ever becomes paid again, put it back in this tuple
    # AND decide where it goes, rather than assuming the old position.
    #
    # ⚠ NSFW TOOLS IS NO LONGER LAST, AND THE PINK NO LONGER DEPENDS ON THAT.
    # theme.QSS used to tint the strip with `QTabBar::tab:last` — Qt has no
    # per-index tab selector — so the colour meant "whichever tab is last" and
    # this reorder would have quietly moved Marty's pink onto Rendering.
    # `SectionTabBar` paints it by NAME instead, so the tab can now sit
    # anywhere and reordering is free.
    # ⚠ THE FREE TOOL TABS, IN STRIP ORDER (Marty, 2026-08-06: "Bone pickers,
    # Anim Layers and Node setup Tabs should be free and not pay gated" and
    # "Node setup tab should be after Anim Layers tab in order").
    #
    # These are built UNCONDITIONALLY at startup, which is what makes them free.
    # Rendering was already free and moves into this list rather than keeping
    # its own hand-written addTab call, so the order is one readable thing.
    FREE_TOOLS = (
        ("rendering", "Rendering"),
        ("picker", "Bone picker"),
        ("anim_layers", "Anim Layers"),
        ("node_setup", "Node Setup"),
        # Freed 2026-08-08 (Marty: "Node editor should be free (But in the
        # future we will have premium nodes)"). ⚠ Freeing it MOVED it — the
        # free tabs are all built before the gated ones, so it sits beside
        # Node Setup now instead of after Physics. That is the same thing
        # that happened to Bone picker / Anim Layers / Node Setup on
        # 2026-08-06, and the strip reads free-then-paid because of it.
        # ⚠ "Premium nodes later" is a NODE-level gate, not a tab-level one:
        # putting this tab back in GATED would re-lock the whole canvas.
        ("nodeeditor", "Node Editor"),
        # Texture Maps (2026-08-17). A LAZY tab: it owns an OpenGL context and
        # a set of shaders, and nobody should pay for either at startup.
        ("texmaps", "Texture Maps"),
        # ⚠ THE FOUR PAID TABS WERE ALL FREED 2026-08-14 (Marty: "make all
        # tabs free" — the pivot: every tool free for everyone, and premium
        # pose/animation PACKS become the paid thing, gated SERVER-SIDE by
        # refusing the download without a key; see ..\PACKS_PLAN.md).
        # Appended in the exact order GATED held them, so the strip does not
        # move by a pixel. MadiRef's own arc: argued free on ship day because
        # the tab works with no Blender at all, paid the day after
        # (2026-08-11, "Make MadiRef paywalled"), free again with the rest.
        ("madiref", "MadiRef"),
        ("optimizer", "Optimization"),
        ("nsfw", "NSFW Tools"),
        ("physics", "Physics"),
    )

    # ⚠ EMPTY SINCE 2026-08-14 — every tab is free (Marty: "make all tabs
    # free"). The paid thing is now premium pose/animation PACKS, and their
    # ⚠⚠ THE WHOLE TAB-GATING MACHINERY WAS DELETED IN 1.19.0, ALONG WITH
    # `licensing\` ITSELF (Marty, 2026-08-15: "FULLY remove the updating
    # mechanics / mention of the server"). `GATED`, `GATED_ATTRS`,
    # `_add_gated_tabs`, `LockedPage` and `_build_preview` are gone. It had
    # been dormant and empty since 2026-08-14, when every tab was freed.
    #
    # ⚠ If a tab ever needs gating again, this is a REBUILD, not four edits —
    # the old machinery lives in the 2026-08-15_5 backup and in
    # `docs\licensing.md`. Do not assume any of it is still here.
    # ⚠ PERF_PLAN option C, one tab per session (Marty, 2026-08-15: "C — lazy
    # tabs, one per session"): a key in here gets an empty host at startup and
    # its `_build_*` runs on FIRST OPEN. The builder's module imports move
    # INSIDE the builder in the same session (option D), and
    # `tools\verify_exe.py` asserts the modules still land in the frozen
    # build's PYZ — PyInstaller collects function-level imports, but that is
    # proved there, never assumed. NOT a licensing mechanism: LockedPage/GATED
    # is that; this is purely "don't build what nobody is looking at".
    LAZY_TOOLS = {"anim_layers", "texmaps"}

    def _ensure_tab_built(self, index):
        """Build a lazy tab the first time it lands on screen.

        Runs FIRST on currentChanged (connection order), so _sync_title_section
        and _refilter_page see the real page. The input filters are attached
        HERE rather than trusted to the re-walk behind us — connection order is
        an easy thing to break silently, and an unguarded combo box is the bug
        Marty reported twice."""
        host = self.main_tabs.widget(index)
        key = self._lazy_hosts.pop(host, None) if hasattr(self, "_lazy_hosts") \
            else None
        if key is None:
            return
        page = getattr(self, "_build_" + key)()
        host.layout().addWidget(page)
        widgets.attach_input_filters(host)


    def _refilter_page(self, index):
        """Re-attach the input filters to the section being opened.

        ⚠ **THIS IS WHAT COVERS WIDGETS BUILT AFTER STARTUP.** The wheel guard
        and the smooth scroller used to sit on the QApplication, so anything
        created at any time was covered for free — at a cost of 64 % of the
        window build (PERF_PLAN.md). Attached per widget, a combo box a tool
        creates later (a rebuilt point page, a table made when data arrives) is
        unguarded, and the symptom is the old bug returning: the wheel edits a
        setting instead of scrolling.
        ⚠ Idempotent and cheap — both walks skip anything already carrying the
        marker property, so this is a `findChildren` over one page, not work.
        """
        page = self.main_tabs.widget(index)
        if page is not None:
            widgets.attach_input_filters(page)

    def _sync_title_section(self, index):
        """Echo the open section in the title bar.

        Reads the RAIL's label, not the tab text — see `SectionRail.label_for`.
        Cheap enough to call on every tab change, and it has to be, because a
        Developer-mode rename has no signal of its own.
        """
        strip = getattr(self, "title_strip", None)
        if strip is not None:
            strip.set_section(self.section_rail.label_for(index))

    def _apply_tab_colors(self):
        """Per-tab text colours. Re-run after anything that rebuilds tabs.

        ⚠ There is no premium set any more (1.19.0): every tab is free and the
        gating machinery is gone. `premium` stays as an empty local so the loop
        below reads the same as the rail's own painter."""
        bar = self.main_tabs.tabBar()
        premium = set()
        # ⚠ The rail is built LATE in __init__ — after the first call to this
        # method — and the tab bar it mirrors is hidden, so the colours have to
        # reach both. `getattr` rather than an attribute: during that first
        # call the name does not exist yet at all.
        rail = getattr(self, "section_rail", None)
        for i in range(self.main_tabs.count()):
            title = self.main_tabs.tabText(i)
            color = TAB_TEXT_COLORS.get(title)
            if color:
                bar.setTabTextColor(i, QColor(color))
                # Marty picked this white himself (2026-08-04, Developer mode:
                # edit). It lived on a strip nobody can see any more, so it
                # moves to the entry that replaced the tab.
                if rail is not None:
                    entry = rail.entry_for(title)
                    if entry is not None:
                        entry.setForeground(0, QColor(color))
            # The star says "this one is members-only"; the tooltip says it in
            # words, because a gold star on its own is a guess.
            if title in premium:
                bar.setTabToolTip(i, "★  Premium — included with supporting")

    # ⚠ `_build_preview`, `_on_license_state` and `_unlock_tabs` were removed
    # in 1.19.0 with the rest of the gating machinery. `_build_preview` built a
    # real page, photographed it and threw it away so a lock panel could show a
    # blurred picture of what was behind it; nothing needs that now.
    def _build_rendering(self):
        page = renderingmod.RenderingPage(self.bridge, self)
        # Render Queue first (Marty, 2026-08-04) — it is the tool that gets
        # opened, and the rail's first entry is also the one selected on build.
        # Offline render queue — drives its own headless blender.exe, no bridge
        # (works with Blender closed). scroll=False: it scrolls itself.
        # …with ONE exception: Save & Queue, which does need a live Blender.
        # Handed in as a callable so the queue itself keeps no bridge import
        # (docs\render-queue.md — the vendored copy has to stay portable).
        self.render_queue = render_tools.RenderQueueTool(
            self, save_open_blend=self.save_open_blend_for_queue)
        self.render_queue.playblastFinished.connect(self._on_playblast_finished)
        page.add_tool(self.render_queue, "Render Queue",
                      group="Render", scroll=False)
        page.add_tool(render_tools.DenoiseSetupTool(self.bridge, self),
                      "Denoising setup", group="Render")
        self.render_presets_tool = render_presets.RenderPresetsTool(
            self.bridge, self)
        page.add_tool(self.render_presets_tool, "Render presets",
                      group="Render")
        self.rendering = page
        return page

    def _build_node_setup(self):
        # Bridge-driven ports of the Image Node Tools add-on (same rail +
        # settings-pane shell as the Rendering tab).
        page = renderingmod.RenderingPage(self.bridge, self)
        # ONE rail entry (Marty, 2026-08-04): Image sequence setup moved inside
        # Relink rather than sitting beside it. RelinkPage composes the two
        # existing tools, so neither one's behaviour changed.
        page.add_tool(node_tools.RelinkPage(self.bridge, self),
                      "Relink", group="Nodes")
        self.node_setup = page
        return page

    def _build_anim_layers(self):
        # Layers workflow over the NLA, all via the bridge.
        # ⚠ Imported HERE, not at module top (PERF_PLAN option D): this tab is
        # lazy, and importing its modules at startup would pay their cost for
        # sessions that never open it. verify_exe proves the frozen build
        # still collects them.
        import anim_layers as anim_layers_mod
        import markers as markersmod

        page = renderingmod.RenderingPage(self.bridge, self)
        # stack + its tools live on ONE page — the tools act on the selected
        # layer, so the list has to stay visible while you use them
        self.layers_page = anim_layers_mod.LayersPage(self.bridge, self)
        page.add_tool(self.layers_page, "Layers", group="Layers")
        # ⚠ Merge / Bake NO LONGER HAS A RAIL ENTRY (Marty, 2026-08-05) — it is
        # a section inside the Layers page now, between the stack and Layer
        # Tools, and `LayersPage` owns both the widget and its wiring to the
        # one poll. Nothing here to connect any more.
        layers_page = self.layers_page
        self.layer_options = anim_layers_mod.LayerOptionsTool(self.bridge, self)
        self.layer_options.stack = layers_page.stack
        page.add_tool(self.layer_options, "Options", group="Layers")
        # tab preferences from config.json, applied before the first poll
        al_cfg = self.cfg.get("anim_layers", {})
        self.layer_options.load_settings(al_cfg)
        layers_page.load_settings(al_cfg)
        layers_page.stack.chk_autoblend.toggled.connect(
            lambda _v: self.save_settings())
        # The settings mirror rides on the stack's existing 1.5 s poll — no
        # second poll, and nothing to notice a Blender-side change for it.
        layers_page.stack.status_refreshed.connect(self._on_layer_prefs)
        # Timeline markers (2026-08-12). Its own group, after Layers, because
        # the rail lists groups in FIRST-USE order and markers are a different
        # job from the layer stack — you reach for one or the other.
        self.markers_tool = markersmod.MarkersTool(self.bridge, self)
        # ⚠ The Render Queue is built by `_build_rendering`, which runs FIRST
        # (Rendering heads FREE_TOOLS). If that ever stops being true this
        # hands over None and the Render button hides itself rather than
        # crashing — which is also what makes the tool reusable somewhere with
        # no queue at all.
        self.markers_tool.set_queue_tool(self.render_queue)
        page.add_tool(self.markers_tool, "Markers", group="Markers")
        self.anim_layers = page
        return page

    def _build_nodeeditor(self):
        """The Node Editor tab — the canvas plus the BAKE node set
        (nodecanvas.py + bakenodes.py, 2026-08-07). FREE since 2026-08-08,
        so it is built at startup like the other FREE_TOOLS rather than
        behind a lock panel.

        Constructing it makes no bridge request and starts no timer — the
        bridge is only touched when a pill or the Bake button is clicked,
        and every miss degrades to a status-line reason (texture_bake is
        capability-gated in bridge.FEATURE_REQUIREMENTS)."""
        self.nodeeditor = nodecanvas.NodeEditorTab(self.bridge, self)
        return self.nodeeditor

    def _build_madiref(self):
        """MadiRef — video reference in this window and in the viewport, in
        sync (2026-08-11, `docs\\madiref.md`).

        Free like every other tab; it was members-only for three days in
        August 2026 and the machinery that made it so no longer exists.

        ⚠ Constructing it reaches Blender not at all and the disk only to
        total up the clip cache for its label. **No shared-memory segment is
        created and the 100 ms tick is built but never started until a clip is
        opened** — which matters more now than it did while the tab was free,
        because `_build_preview` constructs a REAL page to photograph the lock
        panel and throws it away. A constructor that reserved the ring would
        leak a named segment on every preview; `shutdown()` stays the only
        owner, and it is reached through `closeEvent` whether or not the tab
        was ever unlocked."""
        self.madiref = madiref_tab.MadiRefTab(self.bridge, self)
        self.madiref.status_message.connect(
            lambda msg: self.statusBar().showMessage(msg, 6000))
        return self.madiref

    def _build_texmaps(self):
        """Texture Maps - a photo (or a texture out of the open .blend)
        becomes a PBR set (2026-08-17, `docs\texmaps.md`).

        ⚠ **LAZY, and it is the tab that most needs to be.** Opening it
        creates an OpenGL context and compiles a dozen fragment shaders; at
        startup that would be paid by everyone who never opens it. The
        imports are INSIDE the function for the same reason (PERF_PLAN
        option D) - and `tools\verify_exe.py` pins the modules in the frozen
        build's PYZ, because PyInstaller collecting a function-level import
        is something to prove rather than assume.

        ⚠ It reaches Blender ONLY when asked (the scene picker and Use active
        object). With no Blender at all the tab still works end to end from a
        file on disk, which is the same contract MadiRef has.
        """
        import texmaps as texmapsmod

        self.texmaps = texmapsmod.TexMapsPage(self.bridge, self)
        return self.texmaps

    def _build_physics(self):
        # Bone Jiggle — spring-driven secondary motion on bones. The tool
        # disables itself with a reason on an add-on older than it needs.
        # ⚠ The Proxy Cage tool was REMOVED outright on 2026-08-14 (Marty's
        # instruction, deliberately with no What's New entry); the physics.py
        # docstring and the physics-cage doc carry the record.
        page = physicsmod.PhysicsPage(self.bridge, self)
        self.bone_jiggle_tool = jigglemod.BoneJiggleTool(self.bridge, self)
        page.add_tool(self.bone_jiggle_tool, "Bone Jiggle", group="Bones")
        self.physics = page
        return page

    def _build_picker(self):
        # A MANAGER, not a second canvas (Marty's call): the picker's buttons
        # are drawn by a GPU handler in Blender's Image Editor and clicked
        # there. Nothing here holds a copy of a layout - the buttons live on the
        # armature and these tools read and write THAT over the bridge, so the
        # two UIs cannot drift. See docs\bone-picker.md.
        page = pickermod.PickerPage(self.bridge, self)
        # Presets and Appearance are folded INTO Tabs & Rig (Marty, 2026-08-04).
        # All four tools are still built and still assigned to the window, and
        # PickerTabsTool's poll still fans out to the other three - only the
        # rail is shorter.
        self.picker_tabs_tool = pickermod.PickerTabsTool(self.bridge, self)
        self.picker_presets_tool = pickermod.PickerPresetsTool(self.bridge, self)
        self.picker_options_tool = pickermod.PickerOptionsTool(self.bridge, self)
        page.add_tool(pickermod.PickerSetupPage(self.picker_tabs_tool,
                                                self.picker_presets_tool,
                                                self.picker_options_tool),
                      "Tabs & Rig", group="Picker")
        self.picker_buttons_tool = pickermod.PickerButtonsTool(self.bridge, self)
        page.add_tool(self.picker_buttons_tool, "Buttons", group="Picker")
        # ONE poll for the whole tab: the Tabs tool owns it and re-broadcasts,
        # so opening four rail entries still costs one round trip per tick.
        for tool in (self.picker_buttons_tool, self.picker_presets_tool,
                     self.picker_options_tool):
            self.picker_tabs_tool.status_refreshed.connect(tool.apply_status)
        self.picker = page
        return page

    def _build_optimizer(self):
        # Shrink textures and distant meshes to what the render actually needs.
        # The dials live in config.json and travel with every command - the
        # add-on keeps no second copy, so nothing can drift. See
        # docs\optimizer.md.
        page = optimizermod.OptimizerPage(self.bridge, self)
        # ⚠ Fixed size is FIRST (Marty, 2026-08-04) and so is the one selected
        # when the tab opens — but Adaptive still owns the ONLY poll, and it is
        # constructed here before it is added so the fan-out is wired whichever
        # tool the rail happens to show.
        self.optimizer_adaptive_tool = optimizermod.AdaptiveTool(self.bridge,
                                                                 self)
        self.optimizer_fixed_tool = optimizermod.FixedSizeTool(self.bridge, self)
        page.add_tool(self.optimizer_fixed_tool, "Fixed size", group="Optimize")
        page.add_tool(self.optimizer_adaptive_tool, "Adaptive",
                      group="Optimize")
        self.optimizer_meshes_tool = optimizermod.MeshesTool(self.bridge, self)
        page.add_tool(self.optimizer_meshes_tool, "Meshes", group="Optimize")
        # Quad retopology. In the OPTIMIZE group beside the others (Marty,
        # 2026-08-13 - a separate RETOPOLOGY group was offered and declined).
        # See docs\quadify.md.
        self.quadify_tool = quadifymod.QuadifyTool(self.bridge, self)
        page.add_tool(self.quadify_tool, "Quadify", group="Optimize")
        self.optimizer_restore_tool = optimizermod.RestoreTool(self.bridge, self)
        page.add_tool(self.optimizer_restore_tool, "Restore",
                      group="Maintenance")
        self.optimizer_memory_tool = optimizermod.MemoryTool(self.bridge, self)
        page.add_tool(self.optimizer_memory_tool, "Memory report",
                      group="Maintenance")
        # Reads the .blend off disk. The only tool in this tab that needs
        # neither Blender nor the add-on - see docs\optimizer.md.
        self.optimizer_filesize_tool = optimizermod.FileSizeTool(self.bridge,
                                                                 self)
        # scroll=False: the tree scrolls itself, and a wrapping scroll area
        # would nest two scrollbars.
        page.add_tool(self.optimizer_filesize_tool, "File size",
                      group="Maintenance", scroll=False)
        # ONE poll for the whole tab: the Adaptive tool owns it and
        # re-broadcasts, so six open tools still cost one round trip per tick.
        for tool in (self.optimizer_fixed_tool, self.optimizer_meshes_tool,
                     self.optimizer_restore_tool, self.optimizer_memory_tool,
                     self.optimizer_filesize_tool, self.quadify_tool):
            self.optimizer_adaptive_tool.status_refreshed.connect(
                tool.apply_status)
        self.optimizer = page
        return page

    def _build_nsfw(self):
        # Ready-made MADI rigs. The recipe is packed data in this build
        # (nsfw_spec.py) and the add-on carries only a generic builder, so the
        # extension can be read without giving the rig away. See docs\nsfw.md.
        page = renderingmod.RenderingPage(
            self.bridge, self,
            empty_text="Pick a rig on the left to add it to your scene.")
        self.affector_torus_tool = nsfwmod.AffectorTorusTool(self.bridge, self)
        page.add_tool(self.affector_torus_tool, "Penetration Tech", group="Rigs",
                      heading="Penetration torus that you will use on your mesh "
                              "with Surface deform")
        self.nsfw = page
        return page

    # ------------------------------------------------------- always on top

    def set_super_focus(self, on):
        """Turn focus-follows-mouse on or off, and remember it.

        ⚠ Says so out loud the first time. It changes what a click does across
        two applications, so leaving someone to work out why Blender keeps
        coming forward would be a support ticket, not a feature."""
        on = bool(on)
        self.cfg["super_focus"] = on
        config.save(self.cfg)
        really = self.superfocus.set_enabled(on)
        if on and really:
            self.statusBar().showMessage(
                "Super focus is on — whichever of this app or Blender your "
                "mouse is over takes focus. Nothing else is touched.", 8000)
        elif on:
            self.statusBar().showMessage(
                "Super focus needs Windows — it is doing nothing here.", 8000)

    def set_always_on_top(self, on):
        """Pin/unpin the window above every other application.

        Changing a window flag on a VISIBLE window makes Qt (and Windows)
        destroy and re-create the native window, which drops it behind
        everything and loses the frame position. Saving the geometry across the
        flip and re-showing puts it back exactly where it was; while hidden
        (startup) none of that is needed and we just set the flag."""
        on = bool(on)
        self.cfg["always_on_top"] = on
        config.save(self.cfg)
        self.pin_button.setText("📌 Pinned" if on else "📌 Pin")
        visible = self.isVisible()
        geo = self.geometry()
        self.setWindowFlag(Qt.WindowStaysOnTopHint, on)
        # ⚠ The native window Qt just re-created is a DIFFERENT HWND, and our
        # chrome was registered against the old one — so without this the
        # window comes back wearing a stock Windows title bar under ours, and
        # only pinning it ever showed that (chrome.reinstall).
        if self.custom_chrome:
            chrome_mod.reinstall(self)
        if visible:
            self.setGeometry(geo)
            self.show()
            self.raise_()
        self.statusBar().showMessage(
            "Window pinned on top" if on else "Window unpinned", 3000)

    # --------------------------------------------------------- dev console

    # ------------------------------------------------------ developer edit

    def apply_theme(self):
        """Swap the colour theme without a restart (2026-08-08).

        Three things have to happen and only the first is obvious:

        1. Rebuild the QSS and hand it to the APPLICATION, so dialogs and the
           windows that do not exist yet get it too.
        2. ⚠ **Rebuild `nodecanvas`'s cached QColors.** They are built once at
           import, on purpose (a canvas repaint touches them thousands of
           times), which makes them a snapshot of whichever palette was
           loaded first. Without this the whole app changes and the node
           canvas stays in the old theme.
        3. ⚠ **Repaint the widgets that paint THEMSELVES.** A stylesheet
           change restyles anything Qt draws, but `SectionTabBar`, the picker
           and the node canvas read `theme.*` inside their own `paintEvent` —
           they are only correct once something asks them to repaint.
        """
        name = self.cfg.get("theme", theme.DEFAULT_THEME)
        if name == theme.current_theme():
            return
        qss = theme.apply_theme(name)
        nodecanvas.refresh_theme()
        # ⚠ The rail's glyphs are DRAWN in palette colours and cached by
        # colour, so they are the same class of problem as nodecanvas's cached
        # QColors above: a repaint alone would faithfully redraw the old theme.
        # `retheme` drops the icon cache first.
        self.section_rail.retheme()
        # Same problem, same fix: the app mark and the three window buttons are
        # drawn glyphs too, and the rail's retheme is what cleared the cache
        # they share — so these must be re-tinted AFTER it, never before.
        self.rail_header.retheme()
        self.title_strip.retheme()
        app = QApplication.instance()
        if app is not None:
            # ⚠ Defaults FIRST: build_palette reads the globals apply_theme
            # just rebound, and the stylesheet no longer carries a universal
            # QWidget rule to paper over a stale palette (PERF_PLAN B).
            theme.apply_app_defaults(app)
            app.setStyleSheet(qss)
        for widget in self.findChildren(QWidget):
            widget.update()
        self.update()
        # ⚠ Marty's own renames and colours sit ON TOP of the theme, and
        # setStyleSheet has just replaced every stylesheet they were written
        # into. Re-apply or a theme change silently clears them.
        devedit.apply_all(self)
        self.statusBar().showMessage(
            "Theme: %s" % theme.THEMES[theme.current_theme()]["label"], 4000)

    def apply_dev_edit(self):
        """Turn the right-click rename menus on or off.

        Only the MENUS. Renames already stored stay applied — see devedit.py.
        Silent in a build that does not offer the mode: there is no control
        that could have just changed, so there is nothing to report, and
        `set_enabled` refuses regardless of what the config says.
        """
        devedit.set_enabled(bool(self.cfg.get("dev_edit", False)))
        if not devedit.available():
            return
        self.statusBar().showMessage(
            "Developer edit on — right-click anything to rename or recolour it"
            if devedit.enabled() else "Developer edit off", 6000)

    def apply_dev_console(self):
        """Show/hide the console button; hide the window when switched off."""
        on = bool(self.cfg.get("dev_console", False))
        self.console_button.setVisible(on)
        self._refresh_console_button()
        if not on and self._console_dialog is not None:
            self._console_dialog.hide()

    def show_dev_console(self):
        if self._console_dialog is None:
            self._console_dialog = dev_console.DevConsoleDialog(self)
        self._console_dialog.show()
        self._console_dialog.raise_()
        self._console_dialog.activateWindow()

    def _on_log_line(self, level, _line):
        if level in ("ERROR", "CRIT"):
            self._refresh_console_button()

    def _refresh_console_button(self):
        """Errors are worth noticing without opening the window."""
        n = dev_console.BUFFER.error_count
        self.console_button.setText("Console (%d)" % n if n else "Console")
        self.console_button.setStyleSheet("color: #e06c60;" if n else "")

    def note_render(self, path, tell_blender=True):
        """Record `path` as the newest viewport render, for both Watch buttons.

        Writes the shared file itself and, when it can, tells the add-on so its
        N-panel button knows too.

        ⚠ `tell_blender` is about WHO ELSE NEEDS TELLING, not about being
        polite. A BACKGROUND playblast is rendered by a headless blender.exe,
        so the Blender Marty is working in never sees that file exist — the
        bridge call is the only way its button ever learns about it. A blocking
        playblast is the opposite: the add-on ran it and recorded it before
        replying, so a second call would be pure noise.
        """
        lastrender.note(path)
        for i in range(self.tabs.count()):
            self.tabs.widget(i).sync_watch_button()
        if not tell_blender:
            return
        try:
            self.bridge.note_render(path)
        except bridgemod.BridgeError:
            pass    # Blender closed, or an add-on older than 0.20.0 — the
                    # app's own Watch button reads the file regardless

    def _on_playblast_finished(self, result):
        """A background playblast landed. Rescan so the new mp4 shows up as a
        library item even with auto-refresh off, then open it — same ending as
        the blocking playblast, minus the wait."""
        if not result.get("ok"):
            QMessageBox.warning(self, "Background playblast",
                                result.get("error") or "The playblast failed.")
            return
        path = result["path"]
        for i in range(self.tabs.count()):
            self.tabs.widget(i).rescan()
        size = ""
        try:
            size = " — %.1f MB" % (os.path.getsize(path) / 1048576.0)
        except OSError:
            pass
        self.statusBar().showMessage(
            "Background playblast done: %s%s" % (path, size), 10000)
        # A headless Blender rendered this — the live session has no idea it
        # happened, so telling it is the only way its Watch button finds it.
        self.note_render(path)
        try:
            desktop.open_path(path)   # default video player, like the blocking one
        except OSError:
            pass

    def apply_auto_refresh(self):
        """Push the global auto_refresh setting out to every library tab."""
        on = bool(self.cfg.get("auto_refresh", False))
        for i in range(self.tabs.count()):
            self.tabs.widget(i).set_auto_refresh(on)

    # ------------------------------------------------------------- captures

    def current_library_root(self):
        """The library the user is looking at, for tools outside the library
        tabs (the Bone picker saves `.picker` items into it). None if there is
        no library configured."""
        view = self.tabs.currentWidget()
        cfg = getattr(view, "lib_cfg", None)
        if isinstance(cfg, dict):
            return cfg.get("path")
        return None

    def _pages(self):
        """Every page that reacts to Blender being busy (library tabs + the
        tool tabs).

        The members-only pages are None until the licence unlocks them, so this
        filters — a lock panel has nothing to grey out. The FREE_TOOLS pages are
        built at startup, so they are always in the list.

        ⚠ **A new tool tab must be added here too.** Nothing else notices: the
        tab simply never greys out while Blender is busy, which looks like it
        is still usable when it is not."""
        pages = [self.rendering, self.node_setup, self.anim_layers, self.physics,
                 self.picker, self.optimizer, self.nsfw, self.nodeeditor,
                 self.madiref, self.texmaps]
        return [self.tabs.widget(i) for i in range(self.tabs.count())] + \
               [page for page in pages if page is not None]

    def _on_video_preview_ready(self, path):
        """Fan the finished playblast preview out to every library tab (the
        same mp4 can be visible in more than one)."""
        for i in range(self.tabs.count()):
            self.tabs.widget(i).on_video_preview_ready(path)

    # ⚠ `_manual_update_check` / `_end_check_cooldown` were removed in 1.19.0
    # along with the updater they drove.
    def show_about(self, parent=None):
        """About, with the Discord and Patreon links.

        The add-on version is read from the live bridge rather than from what we
        expect, so it says what is actually loaded in Blender — which is the
        number that matters when somebody is quoting it in a bug report.
        """
        addon = None
        try:
            addon = getattr(self.bridge, "addon_version", None)
        except Exception:
            pass
        AboutDialog(parent or self, getattr(self, "license", None), addon).exec()

    def show_library_settings(self, parent=None):
        """The app's settings, from anywhere.

        ⚠ Reachable from the STATUS BAR as well as the library toolbar (Marty,
        2026-08-04: "should be visible in all tabs"). The toolbar copy only
        exists on the Studio Library tab, so from any other tab the settings —
        including the add-on installer — were simply unreachable without
        switching tabs first.
        """
        dlg = LibrarySettingsDialog(parent or self, self.cfg)
        if not dlg.exec():
            return
        self.cfg.update(dlg.values())
        config.save(self.cfg)
        self.apply_theme()
        self.apply_auto_refresh()
        self.apply_dev_console()
        self.apply_dev_edit()

    def bridge_free_for_tools(self):
        """Same guard LibraryView uses: bridge commands queue on Blender's main
        thread, so refuse while a capture is rendering."""
        if self.capturing:
            self.statusBar().showMessage(
                "Blender is busy capturing a preview — try again when it "
                "finishes", 4000)
            return False
        return True

    @property
    def capturing(self):
        return self._captures > 0

    def begin_capture(self, label, verb="capturing"):
        self._captures += 1
        self._status_timer.stop()  # status polls would just time out mid-render
        self.bridge_label.setText("●  Blender: %s '%s'…" % (verb, label))
        self.bridge_label.setStyleSheet("color: #d8b45a;")
        self.capture_progress.show()
        for page in self._pages():
            page.set_capture_busy(True)

    def end_capture(self):
        self._captures = max(0, self._captures - 1)
        if self._captures:
            return
        self.capture_progress.hide()
        for page in self._pages():
            page.set_capture_busy(False)
        self.update_bridge_status()
        self._status_timer.start(FAST_STATUS_MS)

    # ------------------------------------------------------------- bridge

    def update_bridge_status(self):
        """Kick off a health check WITHOUT blocking the GUI.

        This used to call the bridge inline, which froze the whole app for the
        socket timeout every time Blender's server was off (the connect isn't
        refused, it's dropped — see bridge.CONNECT_TIMEOUT). It's a background
        poll; it has no business running on the GUI thread."""
        if self._status_worker is not None:
            return                      # one in flight is enough
        worker = BridgeWorker(
            lambda: self.bridge.status(timeout=1.5, probe=True), parent=self)
        self._status_worker = worker
        worker.done.connect(self._on_status_ok)
        worker.failed.connect(self._on_status_failed)
        worker.start()

    def _clear_status_worker(self):
        worker, self._status_worker = self._status_worker, None
        if worker is not None:
            worker.deleteLater()

    def _on_status_failed(self, _err):
        self._clear_status_worker()
        self.bridge_label.setText("●  Blender not connected (port %d)"
                                  % self.bridge.port)
        self.bridge_label.setStyleSheet("color: #e06c60;")
        # Nothing is known about the installed add-on while Blender is away, so
        # the update offer goes with it — offering to fix a problem we cannot
        # currently see would be a guess.
        self.addon_update_button.hide()
        # Blender went away. Whatever it holds now, the app's copy of the Anim
        # Layers settings wins again when it comes back — the user may have
        # changed them here in the meantime, and a reconnect must not undo that.
        self._prefs_synced = False
        # Forget which file we were in: the next connect may well be a DIFFERENT
        # Blender that grabbed the freed port, and announcing that as a move is
        # right — but only once we have actually seen it.
        self._connected_file = None
        self.bridge_label.setToolTip(
            "The bridge isn't answering. Start it from Blender's N-panel "
            "(MadihsonNSFW ▸ Studio Library) — if you have two Blenders open, "
            "check the one that says 'Bridge: off'. The app keeps checking; "
            "nothing else waits on it.")
        # Nothing is there — checking every 5 s just burns connect attempts.
        self._set_status_interval(SLOW_STATUS_MS)

    def _on_status_ok(self, st):
        self._clear_status_worker()
        self._set_status_interval(FAST_STATUS_MS)
        try:
            target = st["active_object"] or "no object"
            if not st["is_armature"]:
                target += " (not an armature!)"
            # An add-on older than the app is the classic "rebuilt the exe but
            # forgot to reinstall the extension" trap — say so instead of
            # failing later on a command the old bridge doesn't route.
            # A version GAP is not automatically a problem — what matters is
            # whether a feature the app offers is actually missing from the
            # installed add-on (bridge.version_note works that out from the
            # reported capabilities). Amber only when something is really lost.
            note = bridgemod.version_note(st.get("version"),
                                          capabilities=st.get("capabilities"))
            missing = bridgemod.missing_features(st.get("capabilities"),
                                                 st.get("version"))
            # The button appears exactly when the note does, and only when the
            # gap is costing something — a version difference with nothing
            # missing needs no action, so offering one would be noise.
            self.addon_update_button.setVisible(bool(missing))
            blend = self._note_connected_file(st)
            if note:
                mark = "⚠ " if missing else ""
                self.bridge_label.setText("●  %s: %s — %s%s"
                                          % (blend, target, mark, note))
                self.bridge_label.setStyleSheet(
                    "color: #d8c74f;" if missing else "color: #8a8f98;")
                self.bridge_label.setToolTip(
                    "The Blender extension is older than this app.\n"
                    "Everything else keeps working; only the features listed "
                    "are switched off.\nReinstall blender_addon\\"
                    "madi_anim_library-<version>.zip to get them back."
                    if missing else
                    "Different versions, but nothing this app needs is "
                    "missing — all features are available.")
                return
            self.bridge_label.setText("●  %s: %s — frame %d"
                                      % (blend, target, st["frame"]))
            self.bridge_label.setStyleSheet("color: #4fc07a;")
            self.bridge_label.setToolTip(
                "Connected to %s" % (st.get("file") or "an unsaved file"))
            # ⚠ The licence push is ABOVE, before the version note's `return` —
            # see the block there for why it cannot live down here. It is
            # driven by what the ADD-ON REPORTS rather than by our own "have I
            # sent it" flag: an add-on RELOAD wipes the unlock and is faster
            # than this poll, so the app never sees the bridge go down and a
            # flag-based push would never fire again.
        except (KeyError, TypeError) as exc:
            # A reply we can't read is a bug worth seeing, not a dead bridge.
            dev_console.BUFFER.add("ERROR", "Unreadable status reply: %s" % exc)
            self.bridge_label.setText("●  Blender: unreadable status")
            self.bridge_label.setStyleSheet("color: #d8c74f;")
            self.bridge_label.setToolTip("")

    def _note_connected_file(self, st):
        """The .blend the bridge is actually in, and a shout when it changes.

        ⚠ WITH TWO BLENDER INSTANCES OPEN THIS IS THE ONLY WAY TO TELL WHICH
        ONE THE APP IS DRIVING. The bridge binds the port exclusively, so only
        one instance can hold it, and every button in this app acts on whichever
        that is. Marty hit exactly that (2026-08-05); before this the status bar
        only ever said "Blender".

        ⚠ The bridge can still MOVE between instances — it just cannot move on
        its own any more (add-on 0.39.0 deleted the 5 s retry that used to hand
        the port to a second Blender the moment the holder let go). It changes
        when somebody presses Start somewhere, which is exactly when a shout in
        the status bar is worth reading.
        """
        path = st.get("file") or ""
        name = os.path.basename(path) or "unsaved .blend"
        if self._connected_file is not None and self._connected_file != path:
            self.statusBar().showMessage(
                "The bridge is now a different Blender: %s" % name, 10000)
            dev_console.BUFFER.add(
                "INFO", "Bridge moved: %s -> %s" % (self._connected_file, path))
        self._connected_file = path
        return name

    def _set_status_interval(self, ms):
        """Poll fast while connected, slowly while it's known to be down."""
        if self._status_timer.interval() != ms:
            self._status_timer.setInterval(ms)

    def bridge_error(self, exc):
        # Record before showing: the dialog is dismissed and gone, the console
        # entry is what's still there when the question is "what happened?".
        dev_console.BUFFER.add("ERROR", "Bridge: %s" % exc)
        self.update_bridge_status()
        QMessageBox.warning(self, "Blender bridge", str(exc))

    def save_open_blend_for_queue(self):
        """Save whatever Blender has open. -> its path, or None (and say why).

        The Render Queue's Save & Queue button calls this. It lives HERE rather
        than in the queue because the queue is a vendored copy of the standalone
        render manager, which has no bridge at all — this is the whole seam
        between them (`RenderQueueTool(save_open_blend=…)`).

        ⚠ Everything that can go wrong is reported and answered with None; the
        caller queues nothing rather than queueing a path it did not get.
        """
        if not self.bridge_free_for_tools():
            return None
        reason = self.bridge.feature_reason("save_open_blend")
        if reason:
            QMessageBox.information(self, "Save & Queue", reason)
            return None
        try:
            result = self.bridge.save_blend()
        except bridgemod.BridgeError as exc:
            self.bridge_error(exc)
            return None
        path = (result or {}).get("path")
        if not path:
            QMessageBox.warning(self, "Save & Queue",
                                "Blender did not report a saved file.")
            return None
        self.statusBar().showMessage(
            "Saved %s" % os.path.basename(path) if result.get("was_dirty")
            else "%s was already saved" % os.path.basename(path), 5000)
        return path

    # ------------------------------------------------------------- tabs

    def add_library_dialog(self):
        path = QFileDialog.getExistingDirectory(self, "Choose a library folder")
        if not path:
            return
        name, ok = QInputDialog.getText(self, "Library name", "Tab name:",
                                        text=os.path.basename(path) or "Library")
        if not ok:
            return
        lib = {"name": name.strip() or "Library", "path": os.path.normpath(path)}
        self.cfg["libraries"].append(lib)
        self.tabs.addTab(LibraryView(lib, self.bridge, self), lib["name"])
        self.tabs.setCurrentIndex(self.tabs.count() - 1)
        self.apply_auto_refresh()   # a new tab honours the global setting too
        self._persist()

    def close_tab(self, index):
        if self.tabs.count() <= 1:
            return
        self.tabs.removeTab(index)
        del self.cfg["libraries"][index]
        self._persist()

    def _persist(self, *_):
        self.cfg["current_tab"] = self.tabs.currentIndex()
        self.cfg["main_tab"] = self.main_tabs.currentIndex()
        config.save(self.cfg)

    def save_settings(self, *_):
        """Anim Layers tab preferences — the Options page owns two of them,
        the Layers page's Load row owns auto-blend."""
        if self.layer_options is None or self.layers_page is None:
            return  # tab still locked — there is nothing to save
        if getattr(self, "_previewing", False):
            return  # building a throwaway page for the lock preview
        settings = dict(self.cfg.get("anim_layers", {}))
        settings.update(self.layer_options.settings())
        settings.update(self.layers_page.settings_now())
        self.cfg["anim_layers"] = settings
        config.save(self.cfg)
        self._push_layer_prefs(settings)

    # ⚠ `_push_license` WAS HERE UNTIL 1.19.0. The add-on had a session-scoped
    # entitlement gate that the app re-armed over the bridge on every reconnect.
    # Both halves of that are gone: the extension has no gate to arm and this
    # app has no licence to send.
    # ------------------------------------- Anim Layers settings <-> Blender

    def _push_layer_prefs(self, settings):
        """Send our copy to the add-on, so its N-panel says the same thing.

        Silent on failure: Blender being closed is not an error worth a dialog,
        and the push happens again the next time anything is saved or the
        bridge comes back.
        """
        try:
            if self.bridge.feature_reason("anim_layers_shared_prefs"):
                return          # add-on older than 0.9.0 — it has no panel
            self.bridge.anim_layers_set_prefs(
                {k: settings[k] for k in SHARED_LAYER_PREFS if k in settings})
        except Exception:       # noqa: BLE001 - a dropped bridge is routine
            return
        self._prefs_pushed_at = time.monotonic()
        self._prefs_synced = True

    def _on_layer_prefs(self, status):
        """Blender's copy, arriving on the poll everything else already uses.

        ⚠ ON FIRST CONTACT THE APP WINS. The two stores can disagree while
        Blender is closed — the user changes a setting in the app, and the
        add-on still holds whatever it had. Adopting Blender's copy then would
        silently undo them. So the first status after a connect PUSHES, and only
        after that does a difference count as "changed in Blender".
        """
        if not isinstance(status, dict) or getattr(self, "_previewing", False):
            return
        theirs = status.get("prefs")
        if not isinstance(theirs, dict) or not theirs:
            return              # add-on older than 0.9.0 — it has no copy
        mine = dict(self.cfg.get("anim_layers", {}))
        if not self._prefs_synced:
            self._push_layer_prefs(mine)
            return
        # A push and the 1.5 s poll can cross, and the poll's older answer would
        # undo the change that was just made.
        if time.monotonic() - self._prefs_pushed_at < PREFS_ECHO_GUARD_S:
            return
        changed = {k: v for k, v in theirs.items()
                   if k in SHARED_LAYER_PREFS and k in mine and mine[k] != v}
        if not changed:
            return
        mine.update(changed)
        self.cfg["anim_layers"] = mine
        config.save(self.cfg)
        if self.layer_options is not None:
            self.layer_options.load_settings(mine)
        if self.layers_page is not None:
            self.layers_page.load_settings(mine)
        self.statusBar().showMessage(
            "Anim Layers settings changed in Blender: %s"
            % ", ".join(sorted(changed)), 5000)

    def shutdown_workers(self):
        """Wait for every background QThread we own before the window dies.

        ⚠ Qt ABORTS the process (0xC0000409, no traceback in a windowed build)
        if a running QThread is destroyed. Cheap when nothing is in flight, and
        the alternative is a crash on exit that looks like anything but this.

        Called from BOTH teardown paths - closeEvent and the `--smoke` return -
        because the smoke run never raises a close event at all.
        """
        for owner in (getattr(self, "license", None), getattr(self, "updater", None)):
            if owner is not None:
                try:
                    owner.shutdown()
                except Exception:
                    pass

    def closeEvent(self, e):
        # A render in progress is frozen into a resumable paused job; the job
        # list, queue settings and sleep lock are settled before we go down.
        # Still guarded: the queue is None in a --smoke run that never built it.
        if self.render_queue is not None:
            self.render_queue.shutdown()
        # ⚠ MadiRef owns a named shared-memory segment. Without this it
        # outlives the process and the next run collides with a ghost.
        madiref = getattr(self, "madiref", None)
        if madiref is not None:
            madiref.shutdown()
        self.shutdown_workers()
        super().closeEvent(e)


ICON_NAME = "app_icon.ico"


def icon_path():
    """Where the window icon lives, frozen or from source.

    Same shape as `updates.changelog_path`, and for the same reason: in a frozen
    build `__file__` points inside `_internal`, so the bundled copy is the
    fallback rather than the first guess. A missing icon is cosmetic — the app
    starts with Qt's default rather than refusing to open — so this returns a
    path that may not exist and the caller checks.
    """
    names = []
    if getattr(sys, "frozen", False):
        base = os.path.dirname(sys.executable)
        names.append(os.path.join(base, ICON_NAME))
        names.append(os.path.join(base, "_internal", ICON_NAME))
    here = os.path.dirname(os.path.abspath(__file__))
    names.append(os.path.join(here, "assets", ICON_NAME))
    for path in names:
        if os.path.isfile(path):
            return path
    return names[0] if names else ""


def app_icon():
    """The window icon, or an empty QIcon when it cannot be found."""
    path = icon_path()
    return QIcon(path) if path and os.path.isfile(path) else QIcon()


# One name, one running Toolset. Two copies would fight over the same
# config.json, the same render queue and the same bridge — only one Blender can
# hold the bridge at a time, so the second window would look connected and act
# on nothing.
SINGLE_INSTANCE_KEY = "MadihsonNSFW-Toolset-single-instance"


def claim_single_instance(key=SINGLE_INSTANCE_KEY):
    """Own the app, or find out who does.

    Returns the listening QLocalServer if this process is the one, or None if
    another copy already holds the name (which has then been asked to come
    forward).

    ⚠ A NAMED SOCKET, NOT A LOCK FILE. The OS drops the name when the process
    dies, so a crash cannot leave the app unlaunchable — a stale lock file
    absolutely can, and "it won't start any more" is a worse bug than the one
    this prevents.

    ⚠ On Windows this is a named pipe, so connecting to a name nobody owns
    fails immediately rather than burning the timeout — unlike a dead TCP port
    on this machine, which drops the SYN (docs\\app-shell.md). The short wait
    below is a belt-and-braces cap, not the thing that makes it quick.

    `key` is a parameter so the suite can claim a name of its own instead of
    fighting the copy Marty has open.
    """
    probe = QLocalSocket()
    probe.connectToServer(key)
    if probe.waitForConnected(300):
        # Someone is home. Ask them to surface, then get out of the way.
        probe.write(b"raise\n")
        probe.waitForBytesWritten(300)
        probe.disconnectFromServer()
        return None
    server = QLocalServer()
    # A hard kill can leave the name owned by nothing. No-op on Windows;
    # on Unix it clears the stale socket file so we are not locked out.
    QLocalServer.removeServer(key)
    if not server.listen(key):
        return None
    return server


def _on_second_instance(server, win):
    """A second copy was started: raise this window instead of ignoring it.

    Doing nothing would read as "the app is broken" — the user double-clicked
    and no window appeared, because the one they wanted is behind Blender.
    """
    sock = server.nextPendingConnection()
    if sock is not None:
        sock.disconnectFromServer()
    win.setWindowState((win.windowState() & ~Qt.WindowMinimized)
                       | Qt.WindowActive)
    win.show()
    win.raise_()
    win.activateWindow()


def main():
    smoke = "--smoke" in sys.argv
    if smoke:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    # Start recording BEFORE anything else can fail — the console is only
    # worth having if it caught the thing that went wrong. The --smoke run
    # keeps the real streams untouched so its one line still reaches stdout.
    if not smoke:
        dev_console.BUFFER.install()
        dev_console.BUFFER.add("INFO", "%s %s starting (add-on expected: %s)"
                               % (APP_NAME, version.APP_VERSION,
                                  bridgemod.EXPECTED_ADDON_VERSION))
    app = QApplication(sys.argv)
    # Set on the APPLICATION, not just the main window: every dialog, the task
    # bar entry and the Alt-Tab card all inherit it, so the icon is not a thing
    # that has to be remembered for each new window.
    icon = app_icon()
    if not icon.isNull():
        app.setWindowIcon(icon)
    # ⚠ The theme is applied BEFORE the window is built, so every widget is
    # constructed against the right palette. The live switch afterwards is the
    # harder path (`MainWindow.apply_theme`); this one just has to be first.
    theme.apply_theme(config.load().get("theme", theme.DEFAULT_THEME))
    nodecanvas.refresh_theme()
    theme.apply_app_defaults(app)   # the defaults the QWidget{} rule carried
    app.setStyleSheet(theme.QSS)
    # ⚠ BOTH OF THESE ARE DEPRECATED NO-OP SHIMS since the 2026-08-15 perf
    # pass — they only make sure the shared filter objects exist. Neither
    # installs anything on the QApplication any more; that was 788 ms of every
    # window build. The real work is `widgets.attach_input_filters(root)`,
    # called by MainWindow on itself, on every tab switch, on a lazy tab as it
    # is built, and by GuardedDialog. Kept because five suites call them.
    widgets.install_no_wheel(app)
    widgets.install_smooth_scroll(app)
    # Right-click renaming, when Developer mode: edit is on.
    #
    # ⚠⚠ **ONLY WHEN THE FEATURE EXISTS IN THIS RUN.** This is an APPLICATION
    # event filter, so it is asked about every event in the process — measured
    # 2026-08-15 at **397 ms of a single window build**, the same tax the wheel
    # guard used to charge (PERF_PLAN.md). Developer mode: edit is absent from
    # every frozen build by design (`devedit.available()`, docs\devedit.md), and
    # `dev_edits.json` is excluded from releases — so a shipped app was paying
    # 397 ms for a feature it could not offer and a store that was always empty.
    # The filter stays exactly as it was for source runs, where the feature is
    # real and its Show hook is what keeps late-built pages renamed.
    if devedit.available():
        devedit.install(app)

    # ⚠ NOT IN SMOKE MODE. `--smoke` starts the app, reports what it built and
    # exits; it is how a build is verified, and it may well run while a real
    # copy is open. A smoke run that refused to start because "another copy is
    # running" would make every build check fail for a reason that is not
    # back — the same class of disaster as the 0xC0000409 crash, arrived at
    # from the opposite direction. Smoke shows no window and touches no
    # settings, so there is nothing for it to collide with anyway.
    single = None
    if not smoke:
        single = claim_single_instance()
        if single is None:
            dev_console.BUFFER.add(
                "INFO", "Another copy of the Toolset is already running — "
                        "raised its window instead of opening a second one.")
            return 0

    win = MainWindow()
    if smoke:
        view = win.tabs.currentWidget()
        sections = [win.main_tabs.tabText(i)
                    for i in range(win.main_tabs.count())]
        # ⚠ `zstd` IS REPORTED HERE BECAUSE ITS ABSENCE IS OTHERWISE SILENT.
        # `blendsize` imports zstandard inside a try, so a build that dropped
        # the package (or shipped the C backend without the Python half) starts
        # perfectly, passes every marker, and then refuses nearly every real
        # .blend in the File size tool — Blender compresses by default. Nothing
        # else in the build pipeline can see that, and `--smoke` is the only
        # thing that runs the FROZEN app. Print it, and the build can be
        # checked rather than hoped about.
        import blendsize
        print("SMOKE v%s sections=%s libtabs=%d items=%d folders=%d grid=%d "
              "zstd=%s" % (
                  version.APP_VERSION, "/".join(sections), win.tabs.count(),
                  len(view.items), len(view.folders), view.grid.count(),
                  "yes" if blendsize.zstd_available() else "NO"))
        # ⚠ MUST come before the return, or the process ABORTS (0xC0000409).
        # Any worker still mid-request when this returns is destroyed by Qt,
        # which treats that as fatal. The licence check that used to guarantee
        # one is gone, but the health poll and the add-on push both still run
        # off-thread — and `--smoke` is how a build is verified, so a crash
        # here would fail every build check.
        win.shutdown_workers()
        return 0
    # Held on the window so the server outlives this function — a garbage
    # collected QLocalServer stops listening, and the guard would quietly
    # stop guarding after the first few seconds.
    win._single_instance = single
    single.newConnection.connect(
        lambda: _on_second_instance(single, win))
    win.show()
    # ⚠ 3.6 MB of SOURCE TEXT, held for nothing. Importing the app leaves
    # `linecache` holding whole files (pynvml 286 KB, nsfw_spec 279 KB,
    # inspect 121 KB…) because something formatted a traceback or used
    # `inspect` on the way in. It is only ever needed while a traceback is
    # being rendered, and it refills itself if one ever is — so dropping it
    # here is free memory with nothing traded for it (measured 2026-08-15).
    linecache.clearcache()
    # ⚠ AFTER show(), and never in --smoke. Reopening the last reference clip
    # (Marty, 2026-08-12) only ever loads one that is ALREADY PREPARED, so it
    # is near-instant — but it belongs behind the first paint regardless, and a
    # smoke run must open nothing at all. `restore_last_clip` is a no-op when
    # the tab is locked, when the clip has gone, or when its proxy has been
    # cleared, which is exactly what pressing "Close clip" does.
    madiref = getattr(win, "madiref", None)
    if madiref is not None:
        QTimer.singleShot(0, madiref.restore_last_clip)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
