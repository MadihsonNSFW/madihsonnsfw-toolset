"""Texture Maps tab — one photo (or one scene texture) becomes a PBR set.

**Layout B, "one workbench"** (Marty picked it from four rendered mockups,
2026-08-17): no inner tool rail. One source at the top, a CHIP PER MAP where
the tick means "export this one" and clicking the chip shows its dials, one
preview, one export bar. The Full PBR set is not a separate mode — it is
every chip ticked.

Why that shape rather than the eight-tool rail the original site uses: with a
rail, "one photo into five maps" is a different page from "one photo into a
normal map", so the controls exist twice and the two pages disagree about
defaults. Here there is one source, one set of dials per map, and one export.

⚠ **THE TICK AND THE SELECTION ARE DIFFERENT THINGS.** A chip carries a
checkbox (goes in the export) and a pressed state (whose dials you are
looking at). They were one control in the mockup and that was wrong: you
routinely want to *look* at a map you are not exporting, and to export maps
you are not looking at.

Where the rest lives:
  * `texmaps_gl.py`  — the shaders, the meshes, the map spec (every default).
  * `texmaps_ref.py` — the same maths in pure Python, the shaders' oracle.
  * `texmaps_source.py` — where an image came from, and the scene picker.
  * `docs\texmaps.md` — the module doc.
"""
import os
import threading
import time
import traceback
import zipfile

from PySide6.QtCore import (QPoint, QSize, Qt, QTimer, Signal)
from PySide6.QtGui import QColor, QImage, QPainter, QPixmap
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QFileDialog,
                               QFormLayout, QFrame, QGroupBox, QHBoxLayout,
                               QLabel, QMessageBox, QPushButton, QScrollArea,
                               QSizePolicy, QSplitter, QToolButton,
                               QVBoxLayout, QWidget)

import bridge as bridgemod
import config
import dev_console
import icons
import texmaps_gl as tgl
import texmaps_source as tsrc
import theme
import widgets
from widgets import NoScrollComboBox, ValueSlider

TITLE = "Texture Maps"
CONFIG_KEY = "texmaps"

# One re-render per pause in dragging, not one per pixel of mouse travel. A
# ValueSlider emits on every move; at 60 Hz with a 6 ms render that is fine,
# but the preview also rebuilds the lit mesh and its five map textures, and
# coalescing is free.
DEBOUNCE_MS = 30

VIEWS = ("2D flat", "Sphere", "Cube", "All maps")

# How many scene thumbnails to BUILD. A strip shows a couple of dozen; the
# rest are data, not widgets.
SCENE_STRIP_MAX = 48


# ===========================================================================
# Small widgets
# ===========================================================================

class MapChip(QFrame):
    """One map: a tick (export it) and a label (click to open its dials)."""

    selected = Signal(str)
    toggled = Signal(str, bool)

    def __init__(self, key, label, parent=None):
        super().__init__(parent)
        self.key = key
        self._active = False
        self.setCursor(Qt.PointingHandCursor)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(7, 3, 9, 3)
        lay.setSpacing(6)
        self.box = QCheckBox()
        self.box.setToolTip("Include %s in the export" % label)
        self.box.stateChanged.connect(
            lambda _s: self.toggled.emit(self.key, self.box.isChecked()))
        lay.addWidget(self.box)
        self.label = QLabel(label)
        lay.addWidget(self.label)
        self._restyle()

    def set_active(self, active):
        if active == self._active:
            return
        self._active = active
        self._restyle()

    def is_ticked(self):
        return self.box.isChecked()

    def set_ticked(self, on):
        self.box.setChecked(bool(on))

    def _restyle(self):
        # ⚠ Theme constants, never palette() — the app rebinds these on a
        # theme change and re-runs `retheme()` below.
        if self._active:
            self.setStyleSheet(
                "QFrame { background: %s; border: 1px solid %s; "
                "border-radius: 4px; } QLabel { color: white; }"
                % (theme.ACCENT, theme.ACCENT))
        else:
            self.setStyleSheet(
                "QFrame { background: %s; border: 1px solid %s; "
                "border-radius: 4px; } QLabel { color: %s; }"
                % (theme.PANEL2, theme.BORDER, theme.TEXT))

    def retheme(self):
        self._restyle()

    def mousePressEvent(self, event):
        # A click anywhere but the tick selects. The tick keeps its own click.
        if event.button() == Qt.LeftButton:
            self.selected.emit(self.key)
        super().mousePressEvent(event)


class PreviewView(QWidget):
    """Paints whatever the runner last produced; drags orbit the 3D views.

    Not a QOpenGLWidget on purpose — see the note at the top of
    `texmaps_gl.py`. This widget knows nothing about GL; it is handed a
    QPixmap and asked to draw it.
    """

    orbited = Signal(float, float)
    resized = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pixmap = None
        self._message = "Choose a source image to begin."
        self.setMinimumSize(180, 160)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        # ⚠ The canvas exemption: the app's SmoothScroller must not treat a
        # drag surface as something to glide-scroll (`widgets.guard_scroll`).
        self.setProperty("_madi_wire_canvas", True)
        self._drag = None
        self._orbit = [0.6, 0.22]
        self._can_orbit = False

    def set_pixmap(self, pixmap):
        self._pixmap = pixmap
        self.update()

    def set_message(self, text):
        self._message = text
        self._pixmap = None
        self.update()

    def set_orbitable(self, can):
        self._can_orbit = bool(can)
        self.setCursor(Qt.OpenHandCursor if can else Qt.ArrowCursor)

    @property
    def orbit(self):
        return tuple(self._orbit)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.resized.emit()

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)
        rect = self.rect()
        p.fillRect(rect, QColor(theme.BG))
        p.setPen(QColor(theme.BORDER))
        p.drawRoundedRect(rect.adjusted(0, 0, -1, -1), 6, 6)
        if self._pixmap is not None and not self._pixmap.isNull():
            size = self._pixmap.size()
            size.scale(rect.size() - QSize(16, 16), Qt.KeepAspectRatio)
            target = self._pixmap.scaled(size, Qt.KeepAspectRatio,
                                         Qt.SmoothTransformation)
            x = rect.x() + (rect.width() - target.width()) // 2
            y = rect.y() + (rect.height() - target.height()) // 2
            p.drawPixmap(x, y, target)
        else:
            p.setPen(QColor(theme.TEXT_DIM))
            p.drawText(rect, Qt.AlignCenter | Qt.TextWordWrap, self._message)

    def mousePressEvent(self, event):
        if self._can_orbit and event.button() == Qt.LeftButton:
            self._drag = event.position()
            self.setCursor(Qt.ClosedHandCursor)
            event.accept()

    def mouseMoveEvent(self, event):
        if self._drag is None:
            return
        delta = event.position() - self._drag
        self._drag = event.position()
        self._orbit[0] -= delta.x() * 0.01
        self._orbit[1] = max(-1.4, min(1.4, self._orbit[1] + delta.y() * 0.01))
        self.orbited.emit(self._orbit[0], self._orbit[1])
        event.accept()

    def mouseReleaseEvent(self, event):
        if self._drag is not None:
            self._drag = None
            self.setCursor(Qt.OpenHandCursor)
            event.accept()


class _Worker(object):
    """One blocking job on a daemon thread, reported back through a signal.

    Same shape as `optimizer._Runner`: the callable runs off the GUI thread
    and touches no widget; the caller connects to the signals.
    """

    def __init__(self, fn, on_done, on_fail):
        self.fn = fn
        self.on_done = on_done
        self.on_fail = on_fail

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        try:
            result = self.fn()
        except Exception as exc:                              # noqa: BLE001
            dev_console.BUFFER.add(
                "ERROR", "Texture Maps worker failed:\n%s"
                % traceback.format_exc())
            self.on_fail(str(exc))
        else:
            self.on_done(result)


# ===========================================================================
# The tab
# ===========================================================================

class TexMapsPage(QWidget):

    # Signals so worker threads can hand results back onto the GUI thread.
    _scene_ready = Signal(object)
    _scene_failed = Signal(str)
    _export_done = Signal(object)
    _export_failed = Signal(str)
    _exported_one = Signal(object)

    SCENE_STRIP_MAX = SCENE_STRIP_MAX

    def __init__(self, bridge, window, parent=None):
        super().__init__(parent)
        self.bridge = bridge
        self.window = window
        self.source = None
        self._entries = []
        self._active = "normal"
        self._view = 0
        self._busy = False
        self._gl_error = None
        self._last_scene_file = None
        self._preview_cache = {}
        self._cache_order = []
        self._thumbs = tsrc.ThumbCache(self)
        self._thumbs.ready.connect(self._on_thumb)
        self._thumb_targets = {}

        cfg = dict((window.cfg.get(CONFIG_KEY) if window else None) or {})
        self.params = tgl.all_defaults()
        for key, saved in (cfg.get("params") or {}).items():
            if key in self.params and isinstance(saved, dict):
                self.params[key].update(saved)
        self.enabled = dict(tgl.DEFAULT_ENABLED)
        self.enabled.update(cfg.get("enabled") or {})
        self.preview = dict(self.params["preview"])

        self._build()

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(DEBOUNCE_MS)
        self._debounce.timeout.connect(self._render_preview)

        self._scene_ready.connect(self._apply_scene)
        self._scene_failed.connect(self._scene_error)
        self._export_done.connect(self._finish_export)
        self._export_failed.connect(self._fail_export)

        self._select("normal")
        self._update_export_line()

    # ------------------------------------------------------------ building

    def _build(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 10, 12, 10)
        outer.setSpacing(8)

        head = QLabel(TITLE)
        head.setObjectName("h1")
        outer.addWidget(head)
        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setStyleSheet("color: %s;" % theme.BORDER)
        outer.addWidget(line)

        outer.addWidget(self._build_source())

        # ⚠⚠ **THE CHIP ROW MUST BE ABLE TO SHRINK.** Seven chips plus four
        # view buttons in a plain QHBoxLayout report their whole width as the
        # row's minimum, and a QMainWindow takes the widest child — measured
        # 2026-08-18: opening this tab pushed the WINDOW minimum from 638 px
        # to **1476 px**, against Marty's "we need to be able to scale the
        # window a lot" and the 549 px floor the rest of the app holds to.
        # It cost nothing at startup (the tab is lazy) and everything the
        # moment anyone opened it, which is exactly the kind of regression a
        # startup measurement cannot see.
        # A scroll area with an EXPLICIT minimum is the fix the app already
        # uses (`rendering.ToolPage`): the explicit minimum overrides the
        # child's own size hint, which is the only thing Qt honours here.
        chips = QWidget()
        chips.setLayout(self._build_chips())
        chip_area = QScrollArea()
        chip_area.setWidgetResizable(True)
        chip_area.setFrameShape(QFrame.NoFrame)
        chip_area.setWidget(chips)
        chip_area.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        chip_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        chip_area.setFixedHeight(chips.sizeHint().height() + 14)
        chip_area.setMinimumWidth(180)
        outer.addWidget(chip_area)

        split = QSplitter(Qt.Horizontal)
        split.addWidget(self._build_dials())
        split.addWidget(self._build_preview())
        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)
        split.setSizes([330, 560])
        outer.addWidget(split, 1)

        outer.addLayout(self._build_export_bar())

    def _build_source(self):
        frame = QFrame()
        frame.setObjectName("panel")
        lay = QVBoxLayout(frame)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(6)

        top = QHBoxLayout()
        top.setSpacing(10)
        self.source_thumb = QLabel()
        self.source_thumb.setFixedSize(58, 58)
        self.source_thumb.setAlignment(Qt.AlignCenter)
        self.source_thumb.setStyleSheet(
            "border: 1px solid %s; border-radius: 4px; background: %s;"
            % (theme.BORDER, theme.BG))
        top.addWidget(self.source_thumb)

        names = QVBoxLayout()
        names.setSpacing(2)
        self.source_name = QLabel("No source image")
        self.source_name.setStyleSheet("font-weight: 600;")
        names.addWidget(self.source_name)
        self.source_meta = widgets.ElidedLabel(
            "Drop an image here, browse for one, or take one from Blender.")
        self.source_meta.setObjectName("dim")
        names.addWidget(self.source_meta)
        names.addStretch(1)
        top.addLayout(names, 1)

        self.browse_button = QPushButton("Browse…")
        self.browse_button.setIcon(icons.icon("folder", 15, theme.TEXT))
        self.browse_button.setProperty("_madi_keep_text", True)
        self.browse_button.clicked.connect(self.browse)
        top.addWidget(self.browse_button)

        self.scene_button = QPushButton("Blender scene")
        self.scene_button.setIcon(icons.icon("import", 15, theme.TEXT))
        self.scene_button.setProperty("_madi_keep_text", True)
        self.scene_button.setToolTip(
            "List the image textures in the open .blend")
        self.scene_button.clicked.connect(self.refresh_scene)
        top.addWidget(self.scene_button)

        self.paste_button = QPushButton("Paste")
        self.paste_button.setToolTip("Use the image on the clipboard")
        self.paste_button.clicked.connect(self.paste)
        top.addWidget(self.paste_button)
        lay.addLayout(top)

        self.scene_label = QLabel("SCENE TEXTURES  ·  click one to use it")
        self.scene_label.setObjectName("dim")
        lay.addWidget(self.scene_label)

        self.scene_area = QScrollArea()
        self.scene_area.setWidgetResizable(True)
        self.scene_area.setFrameShape(QFrame.NoFrame)
        self.scene_area.setFixedHeight(84)
        self.scene_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.scene_area.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scene_host = QWidget()
        self.scene_row = QHBoxLayout(self.scene_host)
        self.scene_row.setContentsMargins(0, 0, 0, 0)
        self.scene_row.setSpacing(10)
        self.scene_area.setWidget(self.scene_host)
        lay.addWidget(self.scene_area)

        row = QHBoxLayout()
        self.active_button = QPushButton("Use active object")
        self.active_button.setToolTip(
            "Take the base colour of the active object's material")
        self.active_button.clicked.connect(self.use_active)
        row.addWidget(self.active_button)
        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.setIcon(icons.icon("refresh", 15, theme.TEXT))
        self.refresh_button.setProperty("_madi_keep_text", True)
        self.refresh_button.clicked.connect(self.refresh_scene)
        row.addWidget(self.refresh_button)
        row.addStretch(1)
        self.scene_note = QLabel("")
        self.scene_note.setObjectName("dim")
        row.addWidget(self.scene_note)
        lay.addLayout(row)

        self.setAcceptDrops(True)
        self._show_scene_placeholder("Press Blender scene to list the "
                                     "textures in your open file.")
        return frame

    def _build_chips(self):
        row = QHBoxLayout()
        row.setSpacing(4)
        tag = QLabel("MAPS")
        tag.setObjectName("dim")
        row.addWidget(tag)
        self.chips = {}
        for key in tgl.MAP_ORDER:
            chip = MapChip(key, tgl.MAPS[key]["label"])
            chip.set_ticked(self.enabled.get(key, False))
            chip.selected.connect(self._select)
            chip.toggled.connect(self._on_tick)
            row.addWidget(chip)
            self.chips[key] = chip
        row.addStretch(1)

        self.view_buttons = []
        for index, name in enumerate(VIEWS):
            button = QToolButton()
            button.setText(name)
            button.setCheckable(True)
            button.setAutoExclusive(True)
            button.setChecked(index == 0)
            button.setCursor(Qt.PointingHandCursor)
            button.clicked.connect(lambda _c, i=index: self._set_view(i))
            row.addWidget(button)
            self.view_buttons.append(button)
        return row

    def _build_dials(self):
        self.dial_host = QWidget()
        self.dial_layout = QVBoxLayout(self.dial_host)
        self.dial_layout.setContentsMargins(0, 0, 8, 0)
        self.dial_layout.setSpacing(8)
        self.dial_group = QGroupBox("Normal")
        self.dial_group.setLayout(QFormLayout())
        self.dial_layout.addWidget(self.dial_group)
        self.dial_layout.addWidget(self._build_preview_group())
        self.dial_layout.addStretch(1)

        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(QFrame.NoFrame)
        area.setWidget(self.dial_host)
        # ⚠ An explicit minimum, or the scroll area folds the form's own
        # minimum into itself and the window stops shrinking — the
        # `ToolPage` lesson, `docs\app-shell.md`.
        area.setMinimumWidth(230)
        area.setMinimumHeight(140)
        self.dial_area = area
        return area

    def _build_preview_group(self):
        group = QGroupBox("Preview material  (not exported)")
        form = QFormLayout()
        form.setContentsMargins(6, 4, 6, 6)
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(6)
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.preview_widgets = {}
        for spec in tgl.PREVIEW_PARAMS:
            widget = self._control(spec, self.preview,
                                   self._on_preview_changed)
            form.addRow(spec["label"], widget)
            self.preview_widgets[spec["key"]] = widget
        group.setLayout(form)
        group.setToolTip("These change the preview only — they never touch "
                         "the exported maps.")
        return group

    def _build_preview(self):
        host = QWidget()
        lay = QVBoxLayout(host)
        lay.setContentsMargins(4, 0, 0, 0)
        lay.setSpacing(6)
        self.view = PreviewView()
        self.view.orbited.connect(lambda _y, _p: self._render_preview())
        self.view.resized.connect(self._on_view_resized)
        lay.addWidget(self.view, 1)
        self.status_line = widgets.ElidedLabel("")
        self.status_line.setObjectName("dim")
        lay.addWidget(self.status_line)
        return host

    def _build_export_bar(self):
        row = QHBoxLayout()
        # ⚠ Elided, not a plain QLabel: a single-line QLabel reports its FULL
        # text width as its minimum, and this one carries a sentence. Same
        # disease as the Node Editor hint that made ElidedLabel necessary.
        self.export_line = widgets.ElidedLabel("", minimum=90)
        self.export_line.setObjectName("dim")
        row.addWidget(self.export_line, 1)
        row.addStretch(0)
        self.bit_combo = NoScrollComboBox()
        self.bit_combo.addItems(["Height 16-bit", "Height 8-bit"])
        self.bit_combo.setToolTip(
            "16-bit avoids the stair-stepping an 8-bit height map shows on "
            "smooth slopes")
        self.bit_combo.currentIndexChanged.connect(
            lambda _i: self._update_export_line())
        row.addWidget(self.bit_combo)
        self.folder_button = QPushButton("Export to folder…")
        self.folder_button.setIcon(icons.icon("folder", 15, theme.TEXT))
        self.folder_button.setProperty("_madi_keep_text", True)
        self.folder_button.clicked.connect(self.export_to_folder)
        row.addWidget(self.folder_button)
        self.zip_button = QPushButton("Save ZIP…")
        self.zip_button.clicked.connect(self.export_zip)
        row.addWidget(self.zip_button)
        return row

    # ------------------------------------------------------------- controls

    def _control(self, spec, store, on_change):
        """One dial, built from the spec table. ⚠ Every number is a
        ValueSlider (Marty's rule) — no spin boxes."""
        key = spec["key"]
        kind = spec["kind"]
        if kind == "bool":
            widget = widgets.DragCheckBox(spec["label"])
            widget.setChecked(bool(store.get(key, spec["default"])))
            widget.toggled.connect(lambda v, k=key: on_change(k, bool(v)))
        elif kind == "choice":
            widget = NoScrollComboBox()
            widget.addItems(spec["choices"])
            widget.setCurrentIndex(int(store.get(key, spec["default"])))
            widget.currentIndexChanged.connect(
                lambda v, k=key: on_change(k, int(v)))
        else:
            widget = ValueSlider(spec["lo"], spec["hi"],
                                 store.get(key, spec["default"]),
                                 decimals=spec["decimals"],
                                 suffix=spec.get("suffix", ""),
                                 tooltip=spec.get("tip", ""))
            widget.valueChanged.connect(lambda v, k=key: on_change(k, v))
        if spec.get("tip") and kind != "float":
            widget.setToolTip(spec["tip"])
        return widget

    def _rebuild_dials(self):
        group = self.dial_group
        spec = tgl.MAPS[self._active]
        group.setTitle(spec["label"])
        old = group.layout()
        QWidget().setLayout(old)                 # detach and drop the old form
        form = QFormLayout()
        form.setContentsMargins(6, 4, 6, 6)
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(6)
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        hint = QLabel(spec["hint"])
        hint.setObjectName("dim")
        hint.setWordWrap(True)
        form.addRow(hint)
        store = self.params[self._active]
        self.dial_widgets = {}
        for param in spec["params"]:
            widget = self._control(param, store, self._on_param)
            label = "" if param["kind"] == "bool" else param["label"]
            form.addRow(label, widget)
            self.dial_widgets[param["key"]] = widget
        reset = QPushButton("Reset to defaults")
        reset.setObjectName("flat")
        reset.clicked.connect(self._reset_active)
        form.addRow("", reset)
        group.setLayout(form)
        # A lazily built tab attaches its own filters; widgets made later
        # (this form is rebuilt on every chip click) need them too, or the
        # wheel starts editing values again.
        widgets.attach_input_filters(group)

    # --------------------------------------------------------------- events

    def _select(self, key):
        self._active = key
        for chip_key, chip in self.chips.items():
            chip.set_active(chip_key == key)
        self._rebuild_dials()
        self._queue_render()

    def _on_tick(self, key, on):
        self.enabled[key] = bool(on)
        self._save()
        self._update_export_line()
        # Ticking Seamless changes what every other map is made FROM, so the
        # whole preview is stale, not just the seamless one.
        if key == "seamless":
            self._preview_cache.clear()
            self._cache_order = []
        self._queue_render()

    def _on_param(self, key, value):
        params = self.params[self._active]
        params[key] = value
        if self._active == "roughness" and key == "preset":
            # A preset writes the dials it owns; rebuild so they show it.
            self.params["roughness"] = tgl.apply_rough_preset(params, value)
            self._rebuild_dials()
        self._save()
        self._queue_render()

    def _on_preview_changed(self, key, value):
        self.preview[key] = value
        self.params["preview"] = dict(self.preview)
        self._save()
        self._queue_render()

    def _reset_active(self):
        self.params[self._active] = tgl.defaults_for(self._active)
        self._rebuild_dials()
        self._save()
        self._queue_render()

    def _set_view(self, index):
        self._view = index
        self.view.set_orbitable(index in (1, 2))
        self._queue_render()

    def _on_view_resized(self):
        # A resize changes the render size, so the cached pixmap is the wrong
        # resolution — but only re-render after the drag settles.
        self._queue_render()

    def _queue_render(self):
        self._debounce.start()

    def _save(self):
        if self.window is None:
            return
        self.window.cfg[CONFIG_KEY] = {
            "params": {k: v for k, v in self.params.items() if k != "enabled"},
            "enabled": dict(self.enabled),
        }
        config.save(self.window.cfg)

    def retheme(self):
        for chip in self.chips.values():
            chip.retheme()
        self.view.update()

    def set_capture_busy(self, busy):
        """The window greys every page while Blender is busy. Only the
        Blender-facing buttons care: making maps needs no Blender at all."""
        for button in (self.scene_button, self.active_button,
                       self.refresh_button):
            button.setEnabled(not busy and not self._busy)

    # ---------------------------------------------------------- the source

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls() or event.mimeData().hasImage():
            event.acceptProposedAction()

    def dropEvent(self, event):
        data = event.mimeData()
        if data.hasUrls():
            for url in data.urls():
                path = url.toLocalFile()
                if os.path.splitext(path)[1].lower() in tsrc.READABLE:
                    self.set_source(tsrc.Source.for_file(path))
                    event.acceptProposedAction()
                    return
        if data.hasImage():
            image = QImage(data.imageData())
            if not image.isNull():
                self.set_source(tsrc.Source(name="pasted image",
                                            origin="file", image=image,
                                            width=image.width(),
                                            height=image.height()))
                event.acceptProposedAction()

    def browse(self):
        patterns = " ".join("*" + e for e in tsrc.READABLE)
        path, _filter = QFileDialog.getOpenFileName(
            self, "Choose a source image", "",
            "Images (%s);;All files (*)" % patterns)
        if path:
            self.set_source(tsrc.Source.for_file(path))

    def paste(self):
        image = QApplication.clipboard().image()
        if image.isNull():
            self._say("There is no image on the clipboard.")
            return
        self.set_source(tsrc.Source(name="pasted image", origin="file",
                                    image=image, width=image.width(),
                                    height=image.height()))

    def set_source(self, source):
        """Load a source and re-render. The one way in, whatever the origin."""
        try:
            image = source.load()
        except RuntimeError as exc:
            self._say(str(exc))
            return
        if not tgl.available():
            self._gl_fail()
            return
        try:
            tgl.runner().set_source(image)
        except tgl.GLUnavailable as exc:
            self._say(str(exc))
            return
        self.source = source
        self._preview_cache.clear()
        self._cache_order = []
        self.source_name.setText(source.name)
        self.source_meta.setText(source.describe() or "%d×%d"
                                 % (image.width(), image.height()))
        self.source_thumb.setPixmap(
            QPixmap.fromImage(image).scaled(56, 56, Qt.KeepAspectRatio,
                                            Qt.SmoothTransformation))
        self._update_export_line()
        self._render_preview()

    # ------------------------------------------------------ Blender scene

    def refresh_scene(self):
        reason = self.bridge.feature_reason("texmaps_scene") \
            if hasattr(self.bridge, "feature_reason") else None
        if reason:
            self._show_scene_placeholder(reason)
            return
        self._show_scene_placeholder("Reading the scene…")
        worker = _Worker(self.bridge.tex_list,
                         self._scene_ready.emit, self._scene_failed.emit)
        worker.start()

    def _scene_error(self, message):
        self._show_scene_placeholder(
            "Could not read the scene: %s" % message)

    def _apply_scene(self, reply):
        entries = tsrc.sort_entries((reply or {}).get("images") or [])
        self._entries = entries
        self._last_scene_file = (reply or {}).get("file")
        self._clear_scene_row()
        if not entries:
            self._show_scene_placeholder(
                "No image textures in the open .blend.")
            self.scene_note.setText("")
            return
        self._thumb_targets = {}
        # ⚠ CAPPED. Each cell is three widgets, so a production scene with 300
        # textures would build 900 of them — and a horizontal strip can only
        # ever show a couple of dozen. The cap is on the WIDGETS, not on the
        # data: `self._entries` keeps everything, sorted base-colours-first,
        # so the ones most likely to be wanted are the ones built.
        shown = entries[:self.SCENE_STRIP_MAX]
        for entry in shown:
            self.scene_row.addWidget(self._scene_cell(entry))
        self.scene_row.addStretch(1)
        extra = len(entries) - len(shown)
        self.scene_label.setText(
            "SCENE TEXTURES  ·  click one to use it"
            + ("" if not extra
               else "  ·  showing the first %d of %d — base colours first"
               % (len(shown), len(entries))))
        self.scene_area.show()
        self.scene_note.setText("%d texture%s in %d material%s"
                                % (len(entries), "" if len(entries) == 1 else "s",
                                   (reply or {}).get("materials", 0),
                                   "" if (reply or {}).get("materials", 0) == 1
                                   else "s"))

    def _scene_cell(self, entry):
        cell = QWidget()
        lay = QVBoxLayout(cell)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        button = QToolButton()
        button.setFixedSize(tsrc.THUMB + 8, tsrc.THUMB + 8)
        button.setIconSize(QSize(tsrc.THUMB, tsrc.THUMB))
        button.setCursor(Qt.PointingHandCursor)
        users = entry.get("users") or []
        material = users[0].get("material", "") if users else ""
        role = users[0].get("role", "") if users else ""
        button.setToolTip("%s\n%s%s\n%d×%d"
                          % (entry.get("name", ""),
                             material + (" · " if material and role else ""),
                             role,
                             (entry.get("size") or [0, 0])[0],
                             (entry.get("size") or [0, 0])[1]))
        button.clicked.connect(lambda _c, e=entry: self.use_scene_image(e))
        path = entry.get("original") or entry.get("filepath") or ""
        pixmap = self._thumbs.get(path) if path else None
        if pixmap is not None:
            button.setIcon(pixmap)
        else:
            button.setIcon(icons.icon("texmaps", tsrc.THUMB, theme.TEXT_DIM))
            if path:
                self._thumb_targets.setdefault(path, []).append(button)
        lay.addWidget(button, 0, Qt.AlignHCenter)
        label = QLabel(material or entry.get("name", ""))
        label.setObjectName("dim")
        label.setAlignment(Qt.AlignHCenter)
        label.setMaximumWidth(tsrc.THUMB + 26)
        lay.addWidget(label)
        return cell

    def _on_thumb(self, path, pixmap):
        for button in self._thumb_targets.pop(path, []):
            try:
                button.setIcon(pixmap)
            except RuntimeError:
                pass                 # the row was rebuilt while we were reading

    def use_scene_image(self, entry):
        if tsrc.needs_export(entry):
            self._export_scene_image(entry)
            return
        self.set_source(tsrc.Source.for_scene_image(entry))

    def _export_scene_image(self, entry):
        """Ask Blender to write the pixels out, then read that file."""
        if not self.bridge.supports("tex_export"):
            self._say("This image has no file on disk, and the installed "
                      "add-on cannot write one out. Update the add-on, or "
                      "save the image from Blender first.")
            return
        name = entry.get("name") or "image"
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in name)
        target = os.path.join(tsrc.cache_dir(), "%s.png" % safe)
        self._say("Asking Blender for %s…" % name)

        def run():
            return self.bridge.tex_export(name, target)

        def done(reply):
            path = (reply or {}).get("path") or target
            source = tsrc.Source.for_scene_image(entry)
            source.path = path
            source.note = "written out of the .blend"
            self.set_source(source)

        _Worker(run, done, self._scene_failed.emit).start()

    def use_active(self):
        reason = self.bridge.feature_reason("texmaps_scene") \
            if hasattr(self.bridge, "feature_reason") else None
        if reason:
            self._say(reason)
            return
        self._say("Asking Blender for the active object's texture…")

        def done(reply):
            entries = (reply or {}).get("images") or []
            active = (reply or {}).get("active")
            self._scene_ready.emit(reply)
            match = None
            for entry in entries:
                if entry.get("name") == active:
                    match = entry
                    break
            if match is None:
                self._scene_failed.emit(
                    "The active object has no image texture in its material.")
                return
            self.use_scene_image(match)

        _Worker(self.bridge.tex_list, done, self._scene_failed.emit).start()

    def _clear_scene_row(self):
        while self.scene_row.count():
            item = self.scene_row.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def _show_scene_placeholder(self, text):
        """No thumbnails yet — say why, in the label, and give the strip's
        space back. An empty 84 px box with one sentence in it is the tab's
        largest single waste of vertical room, and this tab is used at small
        window sizes."""
        self._clear_scene_row()
        self.scene_area.hide()
        self.scene_label.setText(text)

    # -------------------------------------------------------------- render

    def _render_size(self):
        """Preview render size: the widget, capped, and never above source."""
        rect = self.view.size()
        side = max(160, min(tgl.PREVIEW_MAX,
                            max(rect.width(), rect.height())))
        if self.source is not None and self.source.image is not None:
            longest = max(self.source.image.width(), self.source.image.height())
            side = min(side, max(160, longest))
        return int(side)

    def _map_source(self):
        """(texture id, size) every map is generated FROM.

        With Seamless ticked that is the tiling version, not the original —
        which is the entire point of the switch.
        """
        runner = tgl.runner()
        if self.enabled.get("seamless"):
            return runner.render_seamless_texture(self.params["seamless"])
        return runner._src_tex.textureId(), runner.source_size

    def _cache_key(self, *parts):
        return tuple(parts)

    def _cached(self, key, build):
        hit = self._preview_cache.get(key)
        if hit is not None:
            return hit
        value = build()
        self._preview_cache[key] = value
        self._cache_order.append(key)
        # A bounded cache: a long session of slider dragging would otherwise
        # keep every intermediate image alive.
        while len(self._cache_order) > 12:
            self._preview_cache.pop(self._cache_order.pop(0), None)
        return value

    def _params_key(self, key):
        return tuple(sorted(self.params[key].items()))

    def _render_preview(self):
        if self.source is None:
            return
        if self._gl_error:
            return
        started = time.perf_counter()
        try:
            pixmap, note = self._build_preview_pixmap()
        except tgl.GLUnavailable as exc:
            self._gl_fail(str(exc))
            return
        except Exception as exc:                              # noqa: BLE001
            dev_console.BUFFER.add("ERROR", "Texture Maps preview failed:\n%s"
                                   % traceback.format_exc())
            self.view.set_message("The preview could not be drawn: %s" % exc)
            return
        self.view.set_pixmap(pixmap)
        elapsed = (time.perf_counter() - started) * 1000.0
        source_note = ""
        if self.source is not None:
            source_note = "%s · %d×%d · " % (self.source.name,
                                             self.source.image.width(),
                                             self.source.image.height())
        self.status_line.setText("%s%s · %.0f ms · OpenGL"
                                 % (source_note, note, elapsed))

    def _build_preview_pixmap(self):
        runner = tgl.runner()
        side = self._render_size()
        src_id, src_size = self._map_source()
        seam = "seam" if self.enabled.get("seamless") else "raw"
        seam_key = self._params_key("seamless") if seam == "seam" else ()

        if self._view == 0:                                    # 2D flat
            key = self._cache_key("flat", self._active, side, seam, seam_key,
                                  self._params_key(self._active),
                                  self.preview.get("tiling", 1))
            image = self._cached(key, lambda: self._flat_image(
                runner, src_id, src_size, side))
            return QPixmap.fromImage(image), "%s · 2D" % tgl.MAPS[
                self._active]["label"]

        if self._view == 3:                                    # All maps
            key = self._cache_key("all", side, seam, seam_key,
                                  tuple(self._params_key(k)
                                        for k in tgl.MAP_ORDER))
            image = self._cached(key, lambda: self._contact_sheet(
                runner, src_id, src_size, side))
            return QPixmap.fromImage(image), "every map"

        kind = "sphere" if self._view == 1 else "cube"
        image = self._mesh_image(runner, src_id, src_size, side, kind)
        return QPixmap.fromImage(image), "%s preview" % kind

    def _flat_image(self, runner, src_id, src_size, side):
        if self._active == "seamless":
            # ⚠ REUSE what `_map_source` already produced. With Seamless both
            # TICKED and selected, this used to run the whole eight-pass
            # pipeline a second time for the same result — the duplicate is
            # invisible because both renders are correct, and it doubled the
            # cost of the tab's heaviest live path.
            if self.enabled.get("seamless"):
                texture = src_id
            else:
                texture, _size = runner.render_seamless_texture(
                    self.params["seamless"])
        else:
            texture = runner.map_texture(self._active,
                                         self.params[self._active],
                                         (side, side), src_id, src_size)
        tiling = float(int(self.preview.get("tiling", 1)) + 1)
        return runner.render_flat(texture, (side, side), tiling)

    def _mesh_image(self, runner, src_id, src_size, side, kind):
        """The lit material. Each map is rendered to a texture and left on the
        GPU — reading five maps back only to upload them again is the obvious
        waste in a pipeline like this."""
        textures = {"base": src_id}
        for key in ("normal", "roughness", "ao", "metallic", "height"):
            if not self.enabled.get(key) and key != self._active:
                continue
            textures[key] = runner.map_texture(key, self.params[key],
                                               (side, side), src_id, src_size)
        return runner.render_mesh(kind, textures, (side, side), self.preview,
                                  self.view.orbit)

    def _contact_sheet(self, runner, src_id, src_size, side):
        """Every map at once, drawn with QPainter from six small renders."""
        keys = [k for k in tgl.MAP_ORDER if k != "seamless"]
        columns = 3
        rows = (len(keys) + columns - 1) // columns
        cell = max(120, side // columns)
        sheet = QImage(columns * cell, rows * (cell + 18),
                       QImage.Format_RGB32)
        sheet.fill(QColor(theme.BG))
        painter = QPainter(sheet)
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
        for index, key in enumerate(keys):
            image = runner.render_map(key, self.params[key], (cell, cell),
                                      source_id=src_id, source_size=src_size)
            x = (index % columns) * cell
            y = (index // columns) * (cell + 18)
            painter.drawImage(x, y + 18, image)
            painter.setPen(QColor(theme.TEXT if self.enabled.get(key)
                                  else theme.TEXT_DIM))
            painter.drawText(x + 4, y + 13, "%s%s"
                             % ("✓ " if self.enabled.get(key) else "",
                                tgl.MAPS[key]["label"]))
        painter.end()
        return sheet

    def _gl_fail(self, detail=""):
        self._gl_error = detail or "OpenGL is not available on this machine"
        self.view.set_message(
            "This tab needs OpenGL 3.3, and this machine would not provide "
            "it.\n\n%s\n\nEverything else in the Toolset is unaffected."
            % self._gl_error)
        for button in (self.folder_button, self.zip_button,
                       self.browse_button, self.paste_button):
            button.setEnabled(False)

    def _say(self, text):
        self.status_line.setText(text)

    # -------------------------------------------------------------- export

    def _ticked(self):
        return [k for k in tgl.MAP_ORDER if self.enabled.get(k)]

    def _update_export_line(self):
        ticked = self._ticked()
        if self.source is None:
            self.export_line.setText("No source image yet.")
            return
        width = self.source.image.width() if self.source.image else 0
        height = self.source.image.height() if self.source.image else 0
        if not ticked:
            self.export_line.setText(
                "Nothing ticked — tick a map to export it.")
            return
        bits = "16-bit" if self.bit_combo.currentIndex() == 0 else "8-bit"
        self.export_line.setText(
            "%d map%s · %d×%d · height %s"
            % (len(ticked), "" if len(ticked) == 1 else "s", width, height,
               bits))

    def _file_name(self, key):
        stem = self.source.stem
        if key == "height":
            suffix = "height16" if self.bit_combo.currentIndex() == 0 \
                else "height"
        else:
            suffix = {"normal": "normal", "roughness": "roughness",
                      "ao": "ao", "metallic": "metallic", "bump": "bump",
                      "seamless": "seamless"}[key]
        return "%s_%s.png" % (stem, suffix)

    def _render_for_export(self):
        """Every ticked map at FULL source size, read back on the GUI thread.

        ⚠ GL is GUI-thread only, so the rendering happens here; only the PNG
        encoding (368 ms for a 4096² image) goes to a worker. Splitting it
        this way is what keeps a five-map 4K export from freezing the window
        for two seconds.
        """
        runner = tgl.runner()
        src_id, src_size = self._map_source()
        out = []
        for key in self._ticked():
            if key == "seamless":
                image = runner.render_seamless(self.params["seamless"])
            elif key == "height" and self.bit_combo.currentIndex() == 0:
                image = runner.render_map(key, self.params[key], src_size,
                                          deep=True, source_id=src_id,
                                          source_size=src_size)
            else:
                image = runner.render_map(key, self.params[key], src_size,
                                          source_id=src_id,
                                          source_size=src_size)
            out.append((self._file_name(key), image))
        return out

    def _readme(self):
        names = ", ".join(self._file_name(k) for k in self._ticked())
        return README_TEMPLATE % {
            "source": self.source.name,
            "files": names,
            "date": time.strftime("%Y-%m-%d"),
        }

    def export_to_folder(self):
        if not self._can_export():
            return
        folder = QFileDialog.getExistingDirectory(
            self, "Where should the maps go?", "")
        if not folder:
            return
        self._start_export(folder, None)

    def export_zip(self):
        if not self._can_export():
            return
        path, _f = QFileDialog.getSaveFileName(
            self, "Save the map set", "%s_maps.zip" % self.source.stem,
            "Zip archive (*.zip)")
        if not path:
            return
        self._start_export(None, path)

    def _can_export(self):
        if self.source is None:
            self._say("Choose a source image first.")
            return False
        if not self._ticked():
            self._say("Nothing is ticked, so there is nothing to export.")
            return False
        return True

    def _start_export(self, folder, zip_path):
        try:
            payload = self._render_for_export()
        except Exception as exc:                              # noqa: BLE001
            dev_console.BUFFER.add("ERROR", "Texture Maps export render "
                                   "failed:\n%s" % traceback.format_exc())
            self._say("Could not render the maps: %s" % exc)
            return
        readme = self._readme()
        self._busy = True
        self.folder_button.setEnabled(False)
        self.zip_button.setEnabled(False)
        self._progress("Writing %d files…" % (len(payload) + 1), 0,
                       len(payload) + 1)

        def run():
            written = []
            if zip_path:
                with zipfile.ZipFile(zip_path, "w",
                                     zipfile.ZIP_DEFLATED) as archive:
                    for name, image in payload:
                        temp = os.path.join(tsrc.cache_dir(), name)
                        image.save(temp, "PNG")
                        archive.write(temp, name)
                        written.append(name)
                        try:
                            os.remove(temp)
                        except OSError:
                            pass
                    archive.writestr("README.txt", readme)
                return {"zip": zip_path, "files": written + ["README.txt"]}
            for name, image in payload:
                image.save(os.path.join(folder, name), "PNG")
                written.append(name)
            with open(os.path.join(folder, "README.txt"), "w",
                      encoding="utf-8") as handle:
                handle.write(readme)
            return {"folder": folder, "files": written + ["README.txt"]}

        _Worker(run, self._export_done.emit, self._export_failed.emit).start()

    def _finish_export(self, result):
        self._busy = False
        self.folder_button.setEnabled(True)
        self.zip_button.setEnabled(True)
        self._progress(None)
        where = result.get("zip") or result.get("folder")
        self._say("Wrote %d files to %s" % (len(result.get("files") or []),
                                            where))

    def _fail_export(self, message):
        self._busy = False
        self.folder_button.setEnabled(True)
        self.zip_button.setEnabled(True)
        self._progress(None)
        self._say("Export failed: %s" % message)
        QMessageBox.warning(self, "Export failed", message)

    def _progress(self, text, done=0, total=0):
        bar = None
        if self.window is not None and hasattr(self.window, "statusBar"):
            try:
                bar = self.window.statusBar()
            except Exception:                                 # noqa: BLE001
                bar = None
        if bar is None or not hasattr(bar, "show_progress"):
            return
        if text is None:
            bar.hide_progress()
        else:
            bar.show_progress(text, done, total)


README_TEMPLATE = """MadihsonNSFW Toolset — texture maps
Generated %(date)s from %(source)s

FILES
  %(files)s

COLOUR SPACE — the one thing that is usually got wrong
  Every map here except the source colour is DATA, not a picture.
    Blender  : set the Image Texture node's colour space to Non-Color.
    Unreal   : untick sRGB (and use the Normalmap compression setting).
    Unity    : untick sRGB. Unity wants SMOOTHNESS, which is 1 - roughness.

WIRING IT UP IN BLENDER
  Roughness  -> Principled BSDF > Roughness
  Metallic   -> Principled BSDF > Metallic
  Normal     -> Normal Map node -> Principled BSDF > Normal
  Height     -> Displacement node -> Material Output > Displacement
                (and set the material's displacement method)
  Ambient Occlusion
             -> NOT a Principled input. Multiply it into your base colour
                with a Mix (Multiply) node, or leave it for a compositor.
                Never wire AO into a light.

NORMAL MAPS
  These are OpenGL (+Y up), which is what Blender wants. Tick Invert Y in
  the app before exporting if you need DirectX (Unreal).

HEIGHT
  16-bit unless you chose otherwise. An 8-bit height map stair-steps on
  smooth slopes, and displacement is exactly where that shows.

A NOTE ON WHAT THESE ARE
  Every map here is INFERRED FROM A PHOTOGRAPH, not measured from a
  surface. They are a fast, good-looking starting point — check them
  against reference and adjust rather than trusting them outright.
"""
