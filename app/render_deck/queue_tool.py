"""Render Queue tool: Blender picker, job list, render controls, live log.

Renders headlessly by driving blender.exe as child processes of the toolset
app — completely independent of the Blender instance the bridge talks to, so
the queue keeps rendering with Blender closed."""
from __future__ import annotations

import os
import time
from typing import List

from PySide6.QtCore import QEvent, QProcess, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QFont, QTextCursor
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QFileDialog,
    QHBoxLayout, QHeaderView, QLabel, QMenu, QMessageBox, QPlainTextEdit,
    QDialogButtonBox, QListWidget, QListWidgetItem,
    QPushButton, QSizePolicy, QSpinBox, QSplitter, QTableWidget,
    QTableWidgetItem, QToolBar, QVBoxLayout, QWidget,
)

import theme

from . import icons, joblog, probe, session, sysmon, util
from .job import JobStatus, RenderJob
from .render_controller import RenderController
from .settings import Settings
from .settings_dialog import SettingsDialog
from .widgets import AnimatedProgressBar, DropOverlay, FrameTimers

(COL_FILE, COL_START, COL_END, COL_ENGINE, COL_CAMERA, COL_STATUS,
 COL_PROGRESS, COL_VERSION, COL_SIZE, COL_PACKED, COL_SAMPLES,
 COL_NOISE, COL_LIMIT) = range(13)
HEADERS = ["File", "Start", "End", "Engine", "Camera", "Status", "Progress",
           "Blender", "Size", "Assets", "Samples", "Noise", "Time limit"]
EDITABLE_COLS = (COL_START, COL_END, COL_CAMERA)
DEFAULT_WIDTHS = {COL_FILE: 240, COL_START: 56, COL_END: 56, COL_ENGINE: 90,
                  COL_CAMERA: 110, COL_STATUS: 96, COL_PROGRESS: 78,
                  COL_VERSION: 64, COL_SIZE: 74, COL_PACKED: 62,
                  COL_SAMPLES: 68, COL_NOISE: 60, COL_LIMIT: 74}
# Read out of the .blend by the probe — shown greyed, since they describe the
# file rather than anything the user set here.
INFO_COLS = (COL_VERSION, COL_SIZE, COL_PACKED, COL_SAMPLES, COL_NOISE, COL_LIMIT)

# Toolbar sizing baseline (multiplied by settings.toolbar_scale).
BASE_ICON = 14
BASE_FONT_PT = 8


class JobTable(QTableWidget):
    """Editable job list. Rows are box/ctrl/shift multi-selectable and the
    Delete key removes the selection. Drops are handled by the window so the
    animated overlay can cover the whole thing."""

    def __init__(self, on_delete, parent=None):
        super().__init__(0, len(HEADERS), parent)
        self._on_delete = on_delete
        self.setHorizontalHeaderLabels(HEADERS)
        self.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)  # box + ctrl + shift
        self.setAlternatingRowColors(True)
        self.setEditTriggers(QAbstractItemView.DoubleClicked
                             | QAbstractItemView.EditKeyPressed)
        self.verticalHeader().setVisible(False)
        hdr = self.horizontalHeader()
        hdr.setStretchLastSection(False)
        for c in range(len(HEADERS)):
            hdr.setSectionResizeMode(c, QHeaderView.Interactive)
            self.setColumnWidth(c, DEFAULT_WIDTHS[c])

    def keyPressEvent(self, e):
        if (e.key() in (Qt.Key_Delete, Qt.Key_Backspace)
                and self.state() != QAbstractItemView.EditingState):
            self._on_delete()
            e.accept()
            return
        super().keyPressEvent(e)


class RenderQueueTool(QWidget):
    """The embeddable Render Queue widget (a Rendering-tab tool)."""

    # A background playblast finished (or failed). Payload:
    # {"ok": bool, "path": str, "label": str, "error": str}
    playblastFinished = Signal(object)

    def __init__(self, window=None, parent=None, save_open_blend=None):
        super().__init__(parent)
        self.window = window          # the toolset MainWindow (status bar host)
        # ⚠ THE ONLY SEAM WHERE THIS TOOL TOUCHES A LIVE BLENDER. Everything
        # else in here is disk-only — it renders FILES, and never needs Blender
        # to be running. The Toolset passes a callable that saves whatever
        # Blender currently has open (over the bridge) and returns its path, or
        # None if it could not; the STANDALONE render manager passes nothing and
        # the Save & Queue button is never built. That is what keeps this
        # vendored copy free of any bridge import, so the two copies stay
        # portable to each other (HANDOFF: they already diverge — don't widen
        # the gap with a dependency).
        self.save_open_blend = save_open_blend
        self.setAcceptDrops(True)

        self.settings = Settings.load()
        self.joblog = joblog.JobLogStore()  # per-job console logs on disk (tail on switch)
        self.jobs: List[RenderJob] = []
        self._updating = False
        self._viewed: RenderJob | None = None  # whose log/bars are on screen
        self._view_pinned = False   # user clicked a row → don't auto-follow away
        # True when the last run ended because the user stopped or paused it —
        # either way, not a finished queue, so no auto-shutdown.
        self._ended_by_user = False
        # Result line for a background playblast, consumed by the run summary
        # (its row is removed before the queue reports finished).
        self._last_playblast = ""
        self._export_proc = None    # async "read output folder" Blender probe
        self._export_proc_job = None
        self._export_proc_mtime = None

        # Fills the descriptive columns (version/size/packed/samples/…) in the
        # background, one file at a time, and stands down while rendering.
        self.info_probe = probe.InfoProbe(self.settings, self)
        self.info_probe.updated.connect(self._on_info_probed)

        self.controller = RenderController(self.settings, self)
        self.controller.log.connect(self._append_log)
        self.controller.job_log.connect(self._on_job_log)
        self.controller.job_status.connect(self._refresh_job)
        # A finished probe fills the export-folder / time-limit cache — persist
        # it right away so a restart doesn't have to launch Blender again.
        self.controller.probe_done.connect(lambda _job: self._save_session())
        self.controller.queue_finished.connect(self._on_queue_finished)
        self.controller.paused_changed.connect(self._on_paused_changed)

        # Ticks the live "current frame" timer while a render is running.
        self._frame_tick = QTimer(self)
        self._frame_tick.setInterval(200)
        self._frame_tick.timeout.connect(self._tick_frame_timers)

        # Polls system RAM + GPU VRAM once a second — but ONLY while this tool
        # is actually visible (showEvent/hideEvent above).
        self._sys_tick = QTimer(self)
        self._sys_tick.setInterval(1000)
        self._sys_tick.timeout.connect(self._poll_sysmon)

        self._build_ui()
        self.overlay = DropOverlay(self)
        self._apply_toolbar_scale()
        self._load_session()
        # Re-take the sleep lock if it was on last session (the button was set
        # checked during toolbar build; this makes it true).
        if self.settings.keep_awake:
            self._on_keep_awake_toggled(True)
        self._poll_sysmon()      # show RAM/VRAM immediately
        # ⚠ NOT started here any more — `showEvent` owns it. Starting it in
        # __init__ ran the timer from launch, including for the whole time the
        # app sits on the Studio Library tab, which is most of the time.
        # No modal on first use — just point at the Add… button until a
        # blender.exe is registered.
        if not self.settings.blender_paths:
            self.status_label.setText(
                "First: register a blender.exe with Add… (top right), then "
                "drag .blend files in.")

    # ---- window-level drag & drop (with animated overlay) ------------
    def _has_blend(self, e) -> bool:
        return e.mimeData().hasUrls() and any(
            u.toLocalFile().lower().endswith(".blend") for u in e.mimeData().urls())

    def dragEnterEvent(self, e):
        if self._has_blend(e):
            e.acceptProposedAction()
            self.overlay.begin()

    def dragMoveEvent(self, e):
        if self._has_blend(e):
            e.acceptProposedAction()

    def dragLeaveEvent(self, e):
        self.overlay.end()

    def dropEvent(self, e):
        paths = [u.toLocalFile() for u in e.mimeData().urls()
                 if u.toLocalFile().lower().endswith(".blend")]
        if paths:
            self._add_files(paths)
            e.acceptProposedAction()
        self.overlay.flash()

    # ⚠ THE SYSTEM MONITOR ONLY RUNS WHILE THIS TOOL IS ON SCREEN. Marty,
    # 2026-08-05: "rendering tab should not be updating ram/vram usage while not
    # switched to that tab". It sampled once a second for the whole life of the
    # app — cheap per call, but it is a wake-up every second forever for cards
    # nobody is looking at, and on a laptop that is the difference between idle
    # and not.
    #
    # ⚠ Qt's own show/hide is the signal, NOT a tab-index listener. This widget
    # is hidden by the outer tab bar AND by the Rendering tab's own tool rail
    # (it is one page of a QStackedWidget), and a hidden parent hides it too —
    # one pair of handlers covers all of that, and cannot go stale when a tab is
    # reordered, which the app has already done twice.
    def showEvent(self, e):
        super().showEvent(e)
        self._poll_sysmon()          # never show a stale number on arrival
        self._sys_tick.start()

    def hideEvent(self, e):
        super().hideEvent(e)
        self._sys_tick.stop()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if hasattr(self, "overlay"):
            self.overlay.setGeometry(self.rect())
        # Responsive: shed stat cards as the pane narrows, so the tool imposes
        # only a small minimum width on the window. (The toolbar collapses
        # overflowing buttons into a » popup on its own.) Thresholds sit above
        # each state's own minimum width so a shown state always fits.
        if hasattr(self, "frame_timers"):
            w = self.width()
            self.frame_timers.show_cards(sys_cards=w >= 940, extras=w >= 760)

    # ---- UI ----------------------------------------------------------
    def _build_ui(self):
        root = QVBoxLayout(self)
        root.addWidget(self._build_toolbar())

        # Blender version picker row — a dropdown of the configured installs.
        top = QHBoxLayout()
        top.addWidget(QLabel("Blender:"))
        self.blender_combo = QComboBox()
        self.blender_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        # Don't let a long path/placeholder text dictate the pane's minimum
        # width — the combo elides, the tooltip carries the full path.
        self.blender_combo.setSizeAdjustPolicy(
            QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.blender_combo.setMinimumContentsLength(8)
        self.blender_combo.currentIndexChanged.connect(self._on_blender_selected)
        top.addWidget(self.blender_combo, 1)
        add_b = QPushButton("Add…")
        add_b.setToolTip("Register another blender.exe version")
        add_b.clicked.connect(self._add_blender)
        top.addWidget(add_b)
        root.addLayout(top)
        self._refresh_blender_combo()

        # Reorder row (drag hints live in tooltips — labels here would force a
        # wide minimum window)
        list_bar = QHBoxLayout()
        up = QPushButton("↑ Move up")
        up.clicked.connect(lambda: self._move_selected(-1))
        down = QPushButton("↓ Move down")
        down.clicked.connect(lambda: self._move_selected(1))
        # Explicit minimums let the layout squeeze the buttons on a narrow
        # window instead of them pinning the window's minimum width.
        up.setMinimumWidth(44)
        down.setMinimumWidth(44)
        list_bar.addWidget(up)
        list_bar.addWidget(down)
        export_btn = QPushButton("📂 Open export folder")
        export_btn.setToolTip("Open the render output folder for the selected .blend")
        export_btn.setMinimumWidth(84)
        export_btn.clicked.connect(self._open_export_folder)
        list_bar.addWidget(export_btn)
        list_bar.addStretch(1)
        root.addLayout(list_bar)

        # Live per-frame render timers + RAM/VRAM, on their own row so the
        # cards can collapse independently of the buttons (see resizeEvent).
        timers_row = QHBoxLayout()
        self.frame_timers = FrameTimers()
        # One card's width, not the whole strip's: the strip must never pin
        # the window's minimum — resizeEvent hides cards before they'd clip.
        self.frame_timers.setMinimumWidth(96)
        timers_row.addWidget(self.frame_timers)
        timers_row.addStretch(1)
        root.addLayout(timers_row)

        # Quick frame-range control (applies to selected rows, or all if none)
        range_row = QHBoxLayout()
        range_row.addWidget(QLabel("Frame range:"))
        self.range_start_spin = QSpinBox()
        self.range_start_spin.setRange(0, 1_048_574)
        self.range_start_spin.setValue(1)
        self.range_end_spin = QSpinBox()
        self.range_end_spin.setRange(0, 1_048_574)
        self.range_end_spin.setValue(250)
        # The 7-digit max would otherwise size the boxes for 1048574 and pin
        # the pane's minimum width; let the layout squeeze them instead.
        self.range_start_spin.setMinimumWidth(64)
        self.range_end_spin.setMinimumWidth(64)
        # Un-dim (see _set_range_boxes_dim) as soon as the user focuses a box,
        # so a "blacked out" default-range row is still editable on click.
        self.range_start_spin.installEventFilter(self)
        self.range_end_spin.installEventFilter(self)
        range_row.addWidget(self.range_start_spin)
        range_row.addWidget(QLabel("to"))
        range_row.addWidget(self.range_end_spin)
        target_tip = "Applies to the selected files (or all if none selected)."
        apply_btn = QPushButton("Set custom range")
        apply_btn.setToolTip(target_tip)
        apply_btn.setMinimumWidth(72)
        apply_btn.clicked.connect(self._apply_range_to_selected)
        default_btn = QPushButton("Use .blend default")
        default_btn.setToolTip("Render each file's own saved frame range.\n"
                               + target_tip)
        default_btn.setMinimumWidth(72)
        default_btn.clicked.connect(self._use_file_range_selected)
        range_row.addWidget(apply_btn)
        range_row.addWidget(default_btn)
        range_row.addStretch(1)
        root.addLayout(range_row)

        # Overall progress across the whole frame range (big bar), with a live
        # percentage read-out to its right.
        range_wrap = QHBoxLayout()
        range_wrap.setSpacing(8)
        self.range_bar = AnimatedProgressBar()
        range_wrap.addWidget(self.range_bar, 1)
        self.range_pct = QLabel("0%")
        self.range_pct.setMinimumWidth(52)
        self.range_pct.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.range_pct.setStyleSheet("color:#4fc0c0; font-weight:bold;")
        range_wrap.addWidget(self.range_pct)
        root.addLayout(range_wrap)
        # …and a thin bar for the single current frame's progress (1/3 height),
        # with its own percentage read-out (— on EEVEE, which has no per-frame %).
        frame_wrap = QHBoxLayout()
        frame_wrap.setSpacing(8)
        self.frame_bar = AnimatedProgressBar(compact=True, colors=("#e0a34f", "#d8c74f"))
        frame_wrap.addWidget(self.frame_bar, 1)
        self.frame_pct = QLabel("—")
        # Room for "0:41/1:30 ⏱ 45%" when a Cycles time limit is running.
        self.frame_pct.setMinimumWidth(96)
        self.frame_pct.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.frame_pct.setStyleSheet("color:#e0a34f; font-weight:bold;")
        frame_wrap.addWidget(self.frame_pct)
        root.addLayout(frame_wrap)

        splitter = QSplitter(Qt.Vertical)
        self.table = JobTable(self._remove_selected)
        self.table.itemChanged.connect(self._on_cell_edited)
        self.table.currentCellChanged.connect(self._on_current_cell_changed)
        self.table.itemSelectionChanged.connect(self._on_selection_changed)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._show_table_menu)
        splitter.addWidget(self.table)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setFont(QFont("Consolas", 9))
        self.log.setMaximumBlockCount(5000)
        splitter.addWidget(self.log)
        splitter.setSizes([420, 200])
        root.addWidget(splitter, 1)

        bottom = QHBoxLayout()
        self.status_label = QLabel("Idle")
        # Long status texts must clip, not dictate the window's minimum width
        # (the stretch factor still gives the label all the row's space).
        self.status_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        bottom.addWidget(self.status_label, 1)
        root.addLayout(bottom)

    def _build_toolbar(self) -> QToolBar:
        tb = QToolBar()
        self.tb = tb
        tb.setMovable(False)
        tb.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)

        def action(name, text, slot, color=icons.DEFAULT, tip="", checkable=False):
            act = QAction(icons.icon(name, color), text, self)
            act.setCheckable(checkable)
            if checkable:
                act.toggled.connect(slot)
            else:
                act.triggered.connect(slot)
            if tip:
                act.setToolTip(tip)
            tb.addAction(act)
            return act

        action("add", "Add\nBlends", self._add_files, tip="Add .blend files")
        if callable(self.save_open_blend):
            action("add", "Save &\nQueue", self._save_and_queue,
                   color=theme.ACCENT,
                   tip="Save the .blend Blender currently has open, then add it\n"
                       "to the queue. The queue renders files on disk, so an\n"
                       "unsaved scene would otherwise render as it was last "
                       "written.")
        action("remove", "Remove\nSel.", self._remove_selected)
        action("clear", "Remove\nAll", self._clear_jobs)
        tb.addSeparator()
        action("open", "Open\nBlend", self._open_blend,
               tip="Open the selected .blend in Blender")
        action("output", "Output\npath", self._open_output,
               tip="Open the render output folder")
        self.act_log = action("log", "Live\nLog", self._toggle_log_panel,
                              tip="Show / hide the log panel at the bottom",
                              checkable=True)
        self.act_log.setChecked(True)
        tb.addSeparator()

        # Start doubles as Resume: when the selected row is a paused job it
        # picks that job back up (see _sync_start_action).
        self.act_start = action("start", "Start", self._start, color="#4fc07a",
                                tip="Render the queue")
        # Pause is one-way — you come back via Start/Resume, which also lets you
        # go and render a different scene in between.
        self.act_pause = action("pause", "Pause", self._pause, color="#e0a34f",
                                tip="Pause the running render at the current frame")
        self.act_pause.setEnabled(False)
        self.act_stop = action("stop", "Stop", self._stop, color="#e06c60",
                               tip="Kill the running render")
        self.act_stop.setEnabled(False)
        tb.addSeparator()
        self.act_preview = action(
            "preview", "Preview\nVideo", self._render_preview, color=theme.ACCENT,
            tip="Render the selected .blend to an H.264 MP4 in solid shading\n"
                "(Workbench engine, active camera) — a fast preview of the motion.")
        self.act_play = action(
            "play", "View\nAnim.", self._view_animation, color=theme.ACCENT,
            tip="Open the selected job's rendered output in Blender's animation\n"
                "player (also on the right-click menu).")
        tb.addSeparator()
        wf = action("watch", "Watch\nFolders", lambda: None, tip="Planned")
        sch = action("scheduler", "Sched.", lambda: None, tip="Planned")
        wf.setEnabled(False)
        sch.setEnabled(False)
        tb.addSeparator()
        self.act_awake = action(
            "awake", "Keep\nawake", self._on_keep_awake_toggled, checkable=True,
            tip="Stop Windows putting the PC to sleep while this app is open,\n"
                "so a long queue can't be suspended mid-render.\n"
                "The monitor can still turn off. Released when you close the app.")
        # Only the checked state here — the saved lock is re-armed from
        # __init__, once status_label exists for the handler to write to.
        self._set_awake_checked(self.settings.keep_awake)
        self.act_shutdown = action(
            "power", "Shutdown\nwhen done", self._on_shutdown_toggled,
            color="#e06c60", checkable=True,
            tip="Force-shut down the PC when the render queue finishes.\n"
                "Toggle any time; shows a 60s cancel countdown first.")
        self._set_shutdown_checked(self.settings.shutdown_when_done)
        tb.addSeparator()
        action("settings", "Settings", self._open_settings, tip="Preferences")
        return tb

    def _apply_toolbar_scale(self):
        s = self.settings.toolbar_scale
        self.tb.setIconSize(QSize(round(BASE_ICON * s), round(BASE_ICON * s)))
        f = self.tb.font()
        f.setPointSizeF(BASE_FONT_PT * s)
        self.tb.setFont(f)

    # ---- Blender version picker -------------------------------------
    def _blender_label(self, path: str) -> str:
        """Friendly label for a blender.exe — its parent folder name, which
        usually carries the version (e.g. 'Blender 5.2'); falls back to the
        exe name."""
        return os.path.basename(os.path.dirname(path)) or os.path.basename(path) or path

    def _refresh_blender_combo(self):
        self.blender_combo.blockSignals(True)
        self.blender_combo.clear()
        paths = self.settings.blender_paths
        if not paths:
            self.blender_combo.addItem("No Blender selected — click Add…", "")
        else:
            for p in paths:
                self.blender_combo.addItem(self._blender_label(p), p)
                self.blender_combo.setItemData(
                    self.blender_combo.count() - 1, p, Qt.ToolTipRole)
            active = self.settings.blender_path
            idx = paths.index(active) if active in paths else 0
            self.blender_combo.setCurrentIndex(idx)
            self.settings.blender_path = paths[idx]
        self.blender_combo.blockSignals(False)

    def _on_blender_selected(self, _idx):
        path = self.blender_combo.currentData()
        if path:
            self.settings.blender_path = path
            self.settings.save()

    def _add_blender(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select blender.exe", "", "Blender (blender.exe);;All files (*.*)")
        if not path:
            return
        if path not in self.settings.blender_paths:
            self.settings.blender_paths.append(path)
        self.settings.blender_path = path      # a freshly added version becomes active
        self.settings.save()
        self._refresh_blender_combo()

    def _open_settings(self):
        dlg = SettingsDialog(self.settings, self)
        if dlg.exec():
            self.settings = dlg.apply_to()
            self.controller.settings = self.settings
            self._refresh_blender_combo()
            self._apply_toolbar_scale()

    # ---- job list ----------------------------------------------------
    def _add_files(self, paths=None):
        if not paths:
            paths, _ = QFileDialog.getOpenFileNames(
                self, "Add .blend files", "", "Blender files (*.blend)")
        added = []
        for p in paths or []:
            job = RenderJob(filepath=p, engine=self.settings.default_engine)
            self.jobs.append(job)
            self._append_row(job)
            added.append(job)
        self._sync_range_controls()
        self._sync_start_action()
        self._save_session()
        # Read what's inside each new .blend so the info columns fill in.
        self.info_probe.enqueue(added)

    def _save_and_queue(self):
        """Save Blender's open file and queue it (Marty, 2026-08-10).

        ⚠ ALREADY-QUEUED FILES ARE NOT ADDED TWICE. Saving the shot you are
        working on and pressing this again is the normal way to use it — an
        iteration loop, not a mistake — and a queue slowly filling with the same
        path would be a worse answer than selecting the row it is already on.
        The freshly saved file is what that row will render either way, because
        a job holds a PATH, not a copy.
        """
        path = self.save_open_blend()
        if not path:
            return          # the host has already said why
        for row, job in enumerate(self.jobs):
            if os.path.normcase(os.path.abspath(job.filepath)) == \
                    os.path.normcase(os.path.abspath(path)):
                self.table.selectRow(row)
                self.status_label.setText(
                    "Saved — already queued as row %d" % (row + 1))
                # Re-probe: the file on disk just changed under the row, so its
                # frame range / engine / size columns are now stale.
                self.info_probe.enqueue([job])
                return
        self._add_files([path])
        self.status_label.setText("Saved and queued %s"
                                  % os.path.basename(path))

    def _append_row(self, job: RenderJob):
        r = self.table.rowCount()
        self.table.insertRow(r)
        self._write_row(r, job)
        self._ensure_selection()

    def _ensure_selection(self):
        """Keep a row selected whenever there is one to select.

        Marty, 2026-08-15: the queue should always fall back to the first job
        when nothing is selected. Start/Resume, the frame-range fields and
        Remove all read `_selected_job()`, so an empty selection quietly makes
        half the toolbar do nothing — and an empty selection looks exactly
        like a full one, so there is nothing on screen to explain why.

        ⚠ Deliberately does NOT steal a selection that already exists, and
        does nothing on an empty queue. It is a fallback, not a policy.
        """
        if self.table.rowCount() and not self._selected_rows():
            self.table.selectRow(0)

    def _write_row(self, r: int, job: RenderJob):
        self._updating = True
        # An empty Start/End means "use the .blend's own range". Once the probe
        # has read what that range actually is, show the numbers — greyed, so
        # they still read as the file's defaults rather than an override.
        on_default = (job.start_frame is None, job.end_frame is None)
        start_txt = (str(job.start_frame) if job.start_frame is not None
                     else ("" if job.probed_start is None else str(job.probed_start)))
        end_txt = (str(job.end_frame) if job.end_frame is not None
                   else ("" if job.probed_end is None else str(job.probed_end)))
        cells = [job.name, start_txt, end_txt, job.engine or job.file_engine or "file",
                 job.camera or "scene cam", job.status.value, job.progress_label,
                 job.version_label, job.size_label, job.packed_label,
                 job.samples_label, job.noise_label,
                 util.format_seconds(job.time_limit)]
        for c, text in enumerate(cells):
            item = QTableWidgetItem(text)
            if c == COL_FILE:
                item.setToolTip(job.filepath)
            elif c == COL_VERSION:
                item.setToolTip(job.version_tooltip)
            elif c == COL_PACKED:
                item.setToolTip(
                    "Whether the .blend has packed images/sounds/fonts inside it "
                    "('packed') or references them on disk ('linked').")
            elif c == COL_ENGINE and not job.engine and job.file_engine:
                item.setToolTip(f"Engine saved in the .blend: {job.file_engine}")
            if c in EDITABLE_COLS:
                item.setFlags(item.flags() | Qt.ItemIsEditable)
                # show placeholders greyed so defaults read as hints
                if (c == COL_CAMERA and not job.camera) or \
                   (c == COL_START and on_default[0]) or \
                   (c == COL_END and on_default[1]):
                    item.setForeground(Qt.gray)
            else:
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                if c in INFO_COLS or (c == COL_ENGINE and not job.engine):
                    item.setForeground(Qt.gray)
            self.table.setItem(r, c, item)
        self._updating = False

    def _on_cell_edited(self, item: QTableWidgetItem):
        if self._updating:
            return
        col = item.column()
        r = item.row()
        if r >= len(self.jobs) or col not in EDITABLE_COLS:
            return
        job = self.jobs[r]
        text = item.text().strip()
        if col == COL_CAMERA:
            job.camera = "" if text.lower() in ("", "scene cam") else text
        else:
            value = None
            if text:
                try:
                    value = int(text)
                except ValueError:
                    value = None
            if col == COL_START:
                job.start_frame = value
            else:
                job.end_frame = value
        self._write_row(r, job)
        self._sync_range_controls()
        self._save_session()

    def _selected_rows(self) -> List[int]:
        return sorted({i.row() for i in self.table.selectedIndexes()})

    def _active_job(self):
        """The job that can't be removed right now — the one actually rendering.
        None when idle. A PAUSED job is NOT active: pausing releases the
        controller, which is what lets another scene render in the meantime."""
        return self.controller.current if self.controller.running else None

    def _remove_selected(self):
        rows = self._selected_rows()
        if not rows:
            return
        active = self._active_job()
        skipped_active = False
        # Block the table's signals so removeRow() shuffling doesn't spuriously
        # re-pin the view onto a shifted row mid-render.
        self.table.blockSignals(True)
        for r in reversed(rows):
            job = self.jobs[r]
            if job is active:          # can't pull the job that's rendering
                skipped_active = True
                continue
            self.controller.remove_from_queue(job)  # also drop it from the pending queue
            self.info_probe.drop(job)               # …and from the info backlog
            self.joblog.remove(job)
            self.table.removeRow(r)
            del self.jobs[r]
        self.table.blockSignals(False)
        self._after_queue_edit(active)
        if skipped_active:
            self.status_label.setText(
                "Removed queued job(s) — the job that's rendering can't be removed.")

    def _clear_jobs(self):
        # While rendering/paused this clears the QUEUE but keeps the job that's
        # in progress; idle, it clears everything.
        active = self._active_job()
        self.table.blockSignals(True)
        for r in range(len(self.jobs) - 1, -1, -1):
            job = self.jobs[r]
            if job is active:
                continue
            self.controller.remove_from_queue(job)
            self.info_probe.drop(job)
            self.joblog.remove(job)
            self.table.removeRow(r)
            del self.jobs[r]
        self.table.blockSignals(False)
        self._after_queue_edit(active)

    def _after_queue_edit(self, active):
        """Shared cleanup after removing rows: keep the on-screen log/bars
        pointing at a valid job and refresh them, then persist."""
        if self._viewed not in self.jobs:
            if active is not None and active in self.jobs:
                self._view_job(active)     # follow the still-rendering job again
            else:
                self._clear_view()
        elif self._viewed is not None:
            self._update_bars(self._viewed)  # refresh bars/percentages after the change
        # ⚠ BEFORE the two syncs below, not after: both read `_selected_job()`,
        # so re-selecting afterwards would leave Start/Resume and the range
        # fields describing the selection that was just deleted.
        self._ensure_selection()
        self._sync_range_controls()
        self._sync_start_action()
        self._save_session()

    def _clear_view(self):
        self._viewed = None
        self.log.clear()
        self.range_bar.reset("Idle")
        self.frame_bar.reset("")
        self._set_pcts("0%", "—")
        self.frame_timers.reset()

    def _range_target_rows(self) -> List[int]:
        return self._selected_rows() or list(range(len(self.jobs)))

    def _apply_range_to_selected(self):
        if self.controller.running or self.controller.paused:
            return
        s, e = self.range_start_spin.value(), self.range_end_spin.value()
        if e < s:
            s, e = e, s
        for r in self._range_target_rows():
            self.jobs[r].start_frame = s
            self.jobs[r].end_frame = e
            self._write_row(r, self.jobs[r])
        self._sync_range_controls()
        self._save_session()

    def _use_file_range_selected(self):
        if self.controller.running or self.controller.paused:
            return
        for r in self._range_target_rows():
            self.jobs[r].start_frame = None
            self.jobs[r].end_frame = None
            self._write_row(r, self.jobs[r])
        self._sync_range_controls()
        self._save_session()

    # ---- frame-range control dimming --------------------------------
    def _set_range_boxes_dim(self, dim: bool):
        """'Black out' the custom-range spin boxes when the target rows are on
        the .blend's own range. Kept editable — focusing a box clears the dim
        (see eventFilter) so a custom range can still be typed."""
        style = ("QSpinBox { background:#101216; color:#4a4f58; "
                 "border:1px solid #22252b; }") if dim else ""
        self.range_start_spin.setStyleSheet(style)
        self.range_end_spin.setStyleSheet(style)

    def _on_selection_changed(self):
        # Start/Resume depends on which row is selected, so keep them in step.
        self._sync_range_controls()
        self._sync_start_action()

    def _sync_range_controls(self):
        """Dim the spin boxes when every target row uses its file default."""
        rows = self._range_target_rows()
        on_default = bool(rows) and all(
            self.jobs[r].start_frame is None for r in rows)
        self._set_range_boxes_dim(on_default)

    def eventFilter(self, obj, event):
        if (event.type() == QEvent.FocusIn
                and obj in (self.range_start_spin, self.range_end_spin)):
            self._set_range_boxes_dim(False)
        return super().eventFilter(obj, event)

    def _move_selected(self, delta: int):
        if self.controller.running or self.controller.paused:
            return
        rows = self._selected_rows()
        if len(rows) != 1:
            return
        r = rows[0]
        nr = r + delta
        if 0 <= nr < len(self.jobs):
            self.jobs[r], self.jobs[nr] = self.jobs[nr], self.jobs[r]
            self._write_row(r, self.jobs[r])
            self._write_row(nr, self.jobs[nr])
            self.table.selectRow(nr)

    # ---- toolbar actions --------------------------------------------
    def _open_blend(self):
        rows = self._selected_rows()
        if not rows:
            QMessageBox.information(self, "Open Blend", "Select a job first.")
            return
        path = self.jobs[rows[0]].filepath
        if self.settings.blender_path and os.path.exists(self.settings.blender_path):
            QProcess.startDetached(self.settings.blender_path, [path])
        else:
            util.open_path(os.path.dirname(path))

    def _open_output(self):
        target = self.settings.output_dir
        if not target and self._selected_rows():
            target = os.path.dirname(self.jobs[self._selected_rows()[0]].filepath)
        if target and os.path.isdir(target):
            util.open_path(target)
        else:
            QMessageBox.information(
                self, "Open output path",
                "No output folder set (Settings) and no job selected.")

    def _export_dir_for(self, job: RenderJob) -> str:
        """Where this job's frames are actually written, best-known:
        1. the app's shared output folder (`-o` override) if one is set;
        2. the .blend's own configured output dir (scene.render.filepath),
           read by a probe and cached on the job;
        3. the .blend's folder as a last-resort fallback (Blender's `//`)."""
        if self.settings.output_dir:
            return self.settings.output_dir
        if job.export_dir:
            return job.export_dir
        return os.path.dirname(job.filepath)

    def _open_export_folder(self):
        rows = self._selected_rows()
        if not rows:
            QMessageBox.information(self, "Open export folder", "Select a job first.")
            return
        job = self.jobs[rows[0]]
        # Fast path: we already know where frames land (global -o override, or a
        # path we probed earlier and that still matches the .blend on disk) —
        # open it immediately, no Blender launch. The cache is saved with the
        # session, so this stays instant across restarts until the file changes
        # or the job is removed.
        if self.settings.output_dir or (job.export_dir and job.probe_fresh()):
            self._reveal_export_dir(job)
            return
        if job.export_dir and not job.probe_fresh():
            job.clear_probe_cache()   # .blend re-saved — re-read it
        # Otherwise read the .blend's own output path. Do it ASYNC via QProcess
        # so the UI never freezes, streaming progress to this job's console so
        # the user can see it's working (not hung).
        if not (self.settings.blender_path and os.path.exists(self.settings.blender_path)):
            self._reveal_export_dir(job)   # no Blender to probe with → .blend folder
            return
        if self._export_proc is not None:
            self.status_label.setText("Still reading the previous output path…")
            return
        if not self.act_log.isChecked():   # make sure the console panel is visible
            self.act_log.setChecked(True)
        self._view_pinned = True
        self._view_job(job)
        self._on_job_log(job, f"\n>>> Reading output folder from {job.name} — "
                              f"launching Blender in the background, one moment…\n")
        self.status_label.setText(f"Reading output folder from {job.name}…")
        proc = QProcess(self)
        proc.setProcessChannelMode(QProcess.MergedChannels)
        self._export_proc = proc
        self._export_proc_job = job
        self._export_proc_mtime = job.file_mtime()
        proc.finished.connect(self._on_export_probe_finished)
        proc.start(self.settings.blender_path, util.probe_args(job.filepath))

    def _on_export_probe_finished(self, _code, _status):
        proc = self._export_proc
        job = self._export_proc_job
        mtime = self._export_proc_mtime
        self._export_proc = None
        self._export_proc_job = None
        self._export_proc_mtime = None
        out = bytes(proc.readAllStandardOutput()) if proc is not None else b""
        if proc is not None:
            proc.deleteLater()
        # It's the same launch either way, so keep everything the probe reports —
        # this doubles as the info-column read for that file.
        info = util.parse_probe(out)
        if info:
            job.apply_probe(info, mtime)
        if job.export_dir:
            self._on_job_log(job, f">>> Output folder: {job.export_dir}\n")
        else:
            self._on_job_log(job, ">>> Couldn't read the output path; "
                                  "opening the .blend's folder instead.\n")
        if job.has_time_limit:
            self._on_job_log(job, f">>> Cycles Time Limit: "
                                  f"{util.format_seconds(job.time_limit)} per frame.\n")
        self._save_session()             # keep the cache across restarts
        self._refresh_job(job)           # info columns + default range
        if job is self._viewed:
            self._update_bars(job)       # refresh the TIME LIMIT card
        if not self.controller.running:
            self.status_label.setText("Idle")
        self._reveal_export_dir(job)

    def _reveal_export_dir(self, job: RenderJob):
        target = self._export_dir_for(job)
        if target and os.path.isdir(target):
            util.open_path(target)
        else:
            QMessageBox.information(
                self, "Open export folder",
                f"Export folder doesn't exist yet:\n{target}\n\n"
                "It's created when the render first writes to it.")

    # ---- view the rendered animation --------------------------------
    def _view_animation(self):
        """Open the selected job's output in Blender's built-in player.

        `blender -a <file>` is the animation player — but only when NOT combined
        with -b, so this is a detached GUI process, not one of our headless
        renders."""
        job = self._selected_job()
        if job is None:
            QMessageBox.information(self, "View animation", "Select a job first.")
            return
        blender = self.settings.blender_path
        if not (blender and os.path.exists(blender)):
            QMessageBox.warning(self, "No Blender", "Pick a valid blender.exe first.")
            return
        folder = self._export_dir_for(job)
        target = util.newest_render(folder, os.path.splitext(job.name)[0]) \
            if folder and os.path.isdir(folder) else None
        if not target:
            QMessageBox.information(
                self, "View animation",
                f"Nothing rendered to play yet in:\n{folder}\n\n"
                "Render some frames first (or use Open export folder to check "
                "where this .blend actually writes).")
            return
        QProcess.startDetached(blender, util.player_args(target))
        self.status_label.setText(f"Playing {os.path.basename(target)}")

    def _show_table_menu(self, pos):
        """Right-click menu on a job row."""
        row = self.table.rowAt(pos.y())
        if 0 <= row < len(self.jobs) and row not in self._selected_rows():
            self.table.selectRow(row)
        if not self._selected_rows():
            return
        menu = self._build_table_menu()
        menu.exec(self.table.viewport().mapToGlobal(pos))

    def _build_table_menu(self) -> QMenu:
        menu = QMenu(self)
        menu.addAction(icons.icon("play", icons.DEFAULT), "View animation",
                       self._view_animation)
        menu.addAction(icons.icon("preview", icons.DEFAULT),
                       "Render preview video (solid, H.264)", self._render_preview)
        menu.addSeparator()
        menu.addAction(icons.icon("folder", icons.DEFAULT), "Open export folder",
                       self._open_export_folder)
        menu.addAction(icons.icon("open", icons.DEFAULT), "Open .blend in Blender",
                       self._open_blend)
        menu.addSeparator()
        act_hide = menu.addAction("Collections to leave out…",
                                  self._pick_collections)
        act_hide.setToolTip("Choose which collections this job renders without. "
                            "Per job, so the same .blend can be queued twice "
                            "with different things switched off.")
        menu.addSeparator()
        act_reread = menu.addAction("Re-read file info", self._reread_info)
        act_reread.setToolTip("Open the .blend again to refresh the range, samples, "
                              "time limit and output folder columns.")
        menu.addSeparator()
        menu.addAction(icons.icon("remove", icons.DEFAULT), "Remove from queue",
                       self._remove_selected)
        return menu

    def _pick_collections(self):
        """Choose which collections the selected job renders without.

        ⚠ Needs the job to have been PROBED — the collection names come out of
        the .blend, and offering a free-text box instead would let someone type
        a name that silently matches nothing. If the probe has not run yet, say
        so rather than showing an empty list that looks like "no collections".
        """
        rows = self._selected_rows()
        if not rows:
            return
        job = self.jobs[rows[0]]
        names = list(job.probed_collections or [])
        if not names:
            QMessageBox.information(
                self, "Collections",
                "This file has not been read yet, so its collections are not "
                "known.\n\nUse “Re-read file info” first — that opens the "
                ".blend once and lists them.")
            return

        dlg = QDialog(self)
        dlg.setWindowTitle("Collections to leave out")
        dlg.resize(340, 420)
        lay = QVBoxLayout(dlg)
        blurb = QLabel(
            "Ticked collections are <b>not rendered</b> in this job. Your "
            ".blend is not modified — they are hidden in the copy Blender "
            "loads to render, and only for this job.")
        blurb.setWordWrap(True)
        lay.addWidget(blurb)
        listing = QListWidget()
        chosen = set(job.disabled_collections or [])
        for name in names:
            row = QListWidgetItem(name)
            row.setFlags(row.flags() | Qt.ItemIsUserCheckable)
            row.setCheckState(Qt.Checked if name in chosen else Qt.Unchecked)
            listing.addItem(row)
        lay.addWidget(listing, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        lay.addWidget(buttons)
        if not dlg.exec():
            return
        job.disabled_collections = [
            listing.item(i).text() for i in range(listing.count())
            if listing.item(i).checkState() == Qt.Checked]
        self._write_row(rows[0], job)
        self._save_session()

    def _reread_info(self):
        """Force a fresh probe of the selected jobs (e.g. after re-saving the
        .blend while the app was open)."""
        jobs = [self.jobs[r] for r in self._selected_rows()]
        for job in jobs:
            job.clear_probe_cache()
            self._refresh_job(job)
        self.info_probe.enqueue(jobs)
        self.status_label.setText(f"Re-reading {len(jobs)} file(s)…")

    def _on_info_probed(self, job: RenderJob):
        """A background read finished — refresh that row and remember it."""
        self._refresh_job(job)
        if job is self._viewed:
            self._update_bars(job)
        self._sync_range_controls()
        self._save_session()

    def _toggle_log_panel(self, checked: bool):
        # Live Log off removes the whole bottom log panel.
        if hasattr(self, "log"):  # ignore the initial setChecked during toolbar build
            self.log.setVisible(checked)

    # ---- render ------------------------------------------------------
    def _selected_job(self):
        """The job whose row is selected, or None."""
        rows = self._selected_rows()
        return self.jobs[rows[0]] if rows else None

    def _resumable(self):
        """The paused job Start would pick up: the selected one if it's paused,
        otherwise the first paused job in the list."""
        sel = self._selected_job()
        if sel is not None and sel.status == JobStatus.PAUSED:
            return sel
        return next((j for j in self.jobs if j.status == JobStatus.PAUSED), None)

    def _sync_start_action(self):
        """Relabel Start as Resume when pressing it would continue a paused job,
        so pausing and un-pausing aren't the same button."""
        if self.controller.running:
            return                      # label is irrelevant while disabled
        job = self._resumable()
        if job is not None:
            self.act_start.setText("Resume")
            self.act_start.setIcon(icons.icon("resume", "#4fc07a"))
            self.act_start.setToolTip(
                f"Resume {job.name} from frame {job.resume_frame}, then continue "
                f"the rest of the queue")
        else:
            self.act_start.setText("Start")
            self.act_start.setIcon(icons.icon("start", "#4fc07a"))
            self.act_start.setToolTip("Render the queue")

    def _prepare_run(self, jobs):
        """Shared setup for any run (queue, resume or preview)."""
        for j in jobs:
            self.joblog.reset(j)   # clear each job's log file for a fresh run
            j.sample_done = j.sample_total = 0
            j.frame_started_at = None
            j.last_frame_seconds = None
        self._ended_by_user = False
        self.frame_timers.reset()
        self._frame_tick.start()
        self.log.clear()
        self.act_start.setEnabled(False)
        self.act_pause.setEnabled(True)
        self.act_stop.setEnabled(True)
        self.act_preview.setEnabled(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.info_probe.hold(True)   # don't compete with the render for CPU
        self._viewed = None       # let auto-follow latch onto the first render
        self._view_pinned = False  # …until the user clicks a row again

    def _check_ready(self) -> bool:
        if not self.settings.blender_path or not os.path.exists(self.settings.blender_path):
            QMessageBox.warning(self, "No Blender", "Pick a valid blender.exe first.")
            return False
        if not self.jobs:
            QMessageBox.information(self, "Empty queue", "Add at least one .blend file.")
            return False
        return True

    def _start(self):
        if not self._check_ready():
            return
        # Resume mode: start at the paused job, then carry on down the queue.
        first = self._resumable()
        # Jobs that already finished are left alone (see RenderController.pending)
        # so reopening the app and pressing Start continues the queue rather than
        # re-rendering hours of completed frames.
        pending = self.controller.pending(self.jobs, first)
        skipped = len(self.jobs) - len(pending)
        self._prepare_run(pending)
        if first is not None:
            self._on_job_log(first, f">>> Resuming {first.name} from frame "
                                    f"{first.resume_frame}.\n\n")
        elif skipped:
            self._on_job_log(
                pending[0],
                f">>> Skipping {skipped} job(s) already marked Done — starting with "
                f"{pending[0].name}.\n>>> (To render everything again, remove the "
                f"finished jobs and add them back.)\n\n")
        self.controller.start(self.jobs, first)

    def _render_preview(self):
        """Render the selected .blend to a solid-shaded H.264 MP4."""
        if not self._check_ready():
            return
        job = self._selected_job()
        if job is None:
            QMessageBox.information(self, "Preview video", "Select a job first.")
            return
        self._prepare_run([job])
        self._on_job_log(job, f">>> Preview render of {job.name} — solid shading "
                              f"(Workbench), active camera, H.264 MP4.\n\n")
        self.controller.start(self.jobs, job, preview=True)

    # ---- background playblast (driven by the Studio Library tab) ------
    def queue_playblast(self, blend_path, out_mp4, frame_start=None,
                        frame_end=None, percent=50, label="",
                        temp_blend=True):
        """Render a playblast headlessly, off the live Blender session.

        `blend_path` is normally a throwaway snapshot the bridge just wrote of
        the user's scene (`temp_blend=True` → deleted when the run ends).
        Returns (started, message); a False comes with a reason to show.
        """
        if not self.settings.blender_path or \
                not os.path.exists(self.settings.blender_path):
            return False, ("No Blender selected in the Render Queue. Open the "
                           "Rendering tab → Render Queue and pick a blender.exe "
                           "first.")
        if self.controller.running:
            return False, ("The Render Queue is rendering right now. Wait for it "
                           "to finish (or pause it), then playblast again.")
        job = RenderJob(filepath=blend_path, start_frame=frame_start,
                        end_frame=frame_end)
        job.playblast_out = out_mp4
        job.playblast_pct = int(percent)
        job.playblast_label = label or os.path.basename(out_mp4)
        job.temp_blend = bool(temp_blend)
        if job.render_start is not None and job.render_end is not None:
            job.frames_total = max(0, job.render_end - job.render_start + 1)
        self.jobs.append(job)
        self._append_row(job)
        self._prepare_run([job])
        self._on_job_log(job, f">>> Background playblast '{job.name}' — "
                              f"rendering a snapshot of the live scene, so "
                              f"Blender stays free.\n\n")
        # `only=True`: a playblast must never drag the user's real queue along.
        self.controller.start(self.jobs, job, only=True)
        return True, ""

    # ---- render at a marker (driven by the Markers tool) ---------------
    def queue_at_frame(self, blend_path, frame, label=""):
        """Queue ONE frame of `blend_path` — the Markers tool's Render button.

        Returns (queued, message); a False comes with a reason to show.

        ⚠ It APPENDS rather than reusing the row for that path, which is the
        opposite of Save & Queue's rule, and deliberately: a still at frame 48
        is a different job from the full-range render of the same file, and
        reusing the row would silently rewrite the range on a job the user
        queued for the whole shot. Two stills at two markers are two rows.

        ⚠ It does NOT start the queue. Rendering is what the Render Queue's own
        buttons are for, and a marker click that quietly began a render on
        Marty's machine would be a surprise, not a feature.
        """
        if self.controller.running:
            return False, ("The Render Queue is rendering right now — the "
                           "frame was not added. Wait for it to finish, or "
                           "pause it, then try again.")
        frame = int(frame)
        job = RenderJob(filepath=blend_path, start_frame=frame, end_frame=frame,
                        engine=self.settings.default_engine)
        job.frames_total = 1
        self.jobs.append(job)
        self._append_row(job)
        self.table.selectRow(len(self.jobs) - 1)
        return True, ("Queued frame %d%s — press Render in the Render Queue "
                      "when you are ready." % (frame, " (%s)" % label if label
                                               else ""))

    def _playblast_output(self, job: RenderJob):
        """What Blender actually wrote for a playblast job. Video output gets
        the frame range appended to the name, so the exact path is only known
        after the fact."""
        out_dir = os.path.dirname(job.playblast_out)
        stem = os.path.splitext(os.path.basename(job.playblast_out))[0]
        try:
            cands = [os.path.join(out_dir, f) for f in os.listdir(out_dir)
                     if f.startswith(stem) and f.lower().endswith(".mp4")]
        except OSError:
            return None
        return max(cands, key=os.path.getmtime) if cands else None

    def _finish_playblast(self, job: RenderJob):
        """A background playblast reached a terminal status: give the file the
        name the user asked for, bin the snapshot, and drop the row (its
        .blend is gone, so the job could never be re-run anyway)."""
        ok = job.status == JobStatus.DONE
        path, err = job.playblast_out, ""
        if ok:
            written = self._playblast_output(job)
            if written is None:
                ok, err = False, ("The render finished but produced no mp4 — "
                                  "check the Render Queue log.")
            else:
                try:
                    if os.path.normcase(written) != os.path.normcase(path):
                        os.replace(written, path)
                except OSError:
                    path = written   # keep whatever Blender wrote; still a win
        else:
            err = ("Playblast %s — check the Render Queue log."
                   % job.status.value.lower())
        if job.temp_blend:
            util.remove_snapshot(job.filepath)   # incl. any .blend1 backup
        try:
            r = self.jobs.index(job)
            self.jobs.pop(r)
            self.table.removeRow(r)
        except ValueError:
            pass
        if self._viewed is job:
            self._viewed = None
        # The row is gone before queue_finished fires, so leave a note for the
        # summary line — otherwise it reports "0/0 rendered" for a run that did
        # in fact produce something.
        self._last_playblast = ("Playblast '%s' done — %s" % (job.name, path)
                                if ok else "Playblast '%s' failed" % job.name)
        self.playblastFinished.emit(
            {"ok": ok, "path": path, "label": job.name, "error": err})

    def _pause(self):
        self.controller.pause()

    def _stop(self):
        self._ended_by_user = True  # a manual stop must NOT trigger auto-shutdown
        self.controller.stop()
        self.act_stop.setEnabled(False)

    def _on_paused_changed(self, paused: bool):
        if paused:
            # Pausing ends the run, so it must not fire auto-shutdown either.
            self._ended_by_user = True
            self._save_session()   # persist the frame we stopped on

    def _refresh_job(self, job: RenderJob):
        try:
            r = self.jobs.index(job)
        except ValueError:
            return
        finished = (JobStatus.DONE, JobStatus.FAILED, JobStatus.CANCELLED)
        if job.is_playblast and job.status in finished:
            # Handles its own tidy-up and removes the row — nothing below this
            # point may run against a job that's no longer in the list.
            self._finish_playblast(job)
            return
        self._write_row(r, job)
        self._sync_start_action()   # a job going paused/done changes Start↔Resume
        # Auto-follow the active render onto screen, but ONLY until the user
        # manually clicks a row (then the view is pinned to their choice — a
        # finished job stays showing its own console even while another renders).
        if not self._view_pinned and \
                job.status in (JobStatus.RENDERING, JobStatus.PROBING) and \
                (self._viewed is None or self._viewed.status in finished):
            self._view_job(job)
        elif job is self._viewed:
            self._update_bars(job)

    # ---- per-job view (each file shows its own log + bars) -----------
    def _on_current_cell_changed(self, cur_row, _cc, _pr, _pc):
        if 0 <= cur_row < len(self.jobs):
            self._view_pinned = True   # explicit selection sticks
            self._view_job(self.jobs[cur_row])

    def _view_job(self, job: RenderJob):
        self._viewed = job
        # Load only the TAIL of the job's log file — fast regardless of size.
        self.log.setPlainText(self.joblog.tail(job))
        self.log.moveCursor(QTextCursor.End)
        self._update_bars(job)

    def _set_pcts(self, range_txt: str, frame_txt: str):
        self.range_pct.setText(range_txt)
        self.frame_pct.setText(frame_txt)

    def _update_frame_timers(self, job):
        """Point the two timer cards at `job`: live current-frame elapsed +
        the last finished frame's duration."""
        if job and job.status == JobStatus.RENDERING and job.frame_started_at is not None:
            self.frame_timers.set_current(time.monotonic() - job.frame_started_at, True)
        else:
            self.frame_timers.set_current(None, False)
        self.frame_timers.set_last(job.last_frame_seconds if job else None)

    def _tick_frame_timers(self):
        job = self._viewed
        # With a time limit the frame bar advances on the clock, not on Blender's
        # output, so it has to be repainted on the tick rather than on each
        # "Sample x/y" line.
        if job is not None and job.status == JobStatus.RENDERING and job.has_time_limit:
            self._update_bars(job)
        else:
            self._update_frame_timers(job)

    def _poll_sysmon(self):
        self.frame_timers.set_ram(sysmon.ram())
        self.frame_timers.set_vram(sysmon.vram())

    def _frame_elapsed(self, job: RenderJob):
        """Seconds spent on the frame in progress, or None."""
        if job.status == JobStatus.RENDERING and job.frame_started_at is not None:
            return time.monotonic() - job.frame_started_at
        return None

    def _update_bars(self, job: RenderJob):
        """Point both progress bars — and the two percentage read-outs — at
        `job`'s current state."""
        self._update_frame_timers(job)
        self.frame_timers.set_limit(job.time_limit)
        if job.status == JobStatus.RENDERING and job.frames_total:
            gpct = int(100 * job.frames_done / job.frames_total)
            self.range_bar.setActive(True)
            self.range_bar.setProgress(
                job.frames_done, job.frames_total,
                f"{job.name} — frame {job.current_frame} of {job.render_end}  "
                f"({job.frames_done}/{job.frames_total}) · {gpct}%")
            # Current-frame bar: sample progress, or — when a Cycles Time Limit
            # is set — whichever of samples/clock is further along, since the
            # frame ends on the first of the two. Without either, keep it
            # flowing (EEVEE has no per-frame %).
            self.frame_bar.setActive(True)
            elapsed = self._frame_elapsed(job)
            frac, capped = job.frame_progress(elapsed)
            if frac is None:
                self.frame_bar.setBusy("")
                self._set_pcts(f"{gpct}%", "—")
            else:
                self.frame_bar.setProgress(int(round(frac * 1000)), 1000)
                fpct = int(frac * 100)
                if capped:
                    # Show how far into the limit this frame is, so the number
                    # to the left of the clock says what the clock is counting
                    # towards: "0:41/1:30 ⏱ 45%".
                    txt = (f"{util.format_seconds(elapsed)}/"
                           f"{util.format_seconds(job.time_limit)} ⏱ {fpct}%")
                else:
                    txt = (f"{util.format_seconds(job.time_limit)} ⏱ {fpct}%"
                           if job.has_time_limit else f"{fpct}%")
                self._set_pcts(f"{gpct}%", txt)
            self.status_label.setText(
                ("Preview render — " if job.preview else "Rendering ") + job.name
                + (f"  ·  time limit {util.format_seconds(job.time_limit)}/frame"
                   if job.has_time_limit else ""))
        elif job.status == JobStatus.PROBING:
            self.range_bar.setBusy(f"{job.name} — reading frame range…")
            self.frame_bar.setBusy("")
            self._set_pcts("…", "—")
            self.status_label.setText(f"{job.name}  (reading range…)")
        elif job.status == JobStatus.PAUSED:
            # `resume_frame` is the exact frame we'll restart on (it survives a
            # restart); fall back to the running count when it isn't set yet.
            at = (job.resume_frame if job.resume_frame is not None
                  else job.current_frame)
            gpct = (int(100 * job.frames_done / job.frames_total)
                    if job.frames_total else 0)
            # Show the progress it stopped at instead of an empty bar — a paused
            # job restored from the last session must not look like frame zero.
            if job.frames_total:
                self.range_bar.setProgress(
                    job.frames_done, job.frames_total,
                    f"{job.name} — paused at frame {at}  "
                    f"({job.frames_done}/{job.frames_total}) · {gpct}%")
            else:
                self.range_bar.reset(f"{job.name} — paused at frame {at}")
            self.range_bar.setActive(False)
            self.frame_bar.setActive(False)
            self._set_pcts(f"{gpct}%", "—")
            self.status_label.setText(
                f"{job.name}  (paused at frame {at} — select it and press Resume)")
        elif job.status == JobStatus.DONE:
            self.range_bar.setProgress(1, 1, f"{job.name} — done · 100%")
            self.range_bar.setActive(False)
            self.frame_bar.setProgress(1, 1)
            self.frame_bar.setActive(False)
            self._set_pcts("100%", "100%")
        else:  # QUEUED / FAILED / CANCELLED
            self.range_bar.reset(f"{job.name} — {job.status.value}")
            self.frame_bar.reset("")
            self._set_pcts("0%", "—")

    def _on_queue_finished(self):
        self._frame_tick.stop()
        self._tick_frame_timers()  # settle the display (current inactive, last kept)
        self.act_start.setEnabled(True)
        self.act_pause.setEnabled(False)
        self.act_stop.setEnabled(False)
        self.act_preview.setEnabled(True)
        self.table.setEditTriggers(QAbstractItemView.DoubleClicked
                                   | QAbstractItemView.EditKeyPressed)
        for j in self.jobs:
            j.preview = False
        self._sync_start_action()
        self.info_probe.hold(False)     # the machine is ours again
        self.info_probe.enqueue(self.jobs)
        pb, self._last_playblast = self._last_playblast, ""
        paused = next((j for j in self.jobs if j.status == JobStatus.PAUSED), None)
        if pb:
            # The run was a background playblast; its row has already been
            # removed, so the normal "n/m rendered" tally would be nonsense.
            self.range_bar.reset(pb)
            self.frame_bar.reset("")
            self._set_pcts("100%" if "done" in pb else "0%", "—")
            self.status_label.setText(pb)
        elif paused is not None:
            # A pause isn't a finished queue — say so instead of "Finished 0/2".
            msg = (f"Paused {paused.name} at frame {paused.resume_frame} — "
                   f"select it and press Resume to continue")
            self.range_bar.setProgress(paused.frames_done, paused.frames_total or 1,
                                       msg) if paused.frames_total else \
                self.range_bar.reset(msg)
            self.range_bar.setActive(False)
            self.frame_bar.reset("")
            self.status_label.setText(msg)
        else:
            done = sum(1 for j in self.jobs if j.status == JobStatus.DONE)
            self.range_bar.reset(f"Finished — {done}/{len(self.jobs)} rendered")
            self.frame_bar.reset("")
            self._set_pcts("100%" if done == len(self.jobs) and done else "0%", "—")
            self.status_label.setText(f"Finished — {done}/{len(self.jobs)} rendered")
        self._save_session()
        self._maybe_shutdown()

    # ---- log routing -------------------------------------------------
    def _on_job_log(self, job: RenderJob, text: str):
        self.joblog.append(job, text)   # full history to the job's own file
        if job is self._viewed:
            self._append_log(text)      # live-append only to the viewed job

    def _append_log(self, text: str):
        self.log.moveCursor(QTextCursor.End)
        self.log.insertPlainText(text)
        self.log.moveCursor(QTextCursor.End)

    # ---- keep the PC awake -------------------------------------------
    def _set_awake_checked(self, checked: bool):
        self.act_awake.blockSignals(True)
        self.act_awake.setChecked(checked)
        self.act_awake.blockSignals(False)

    def _on_keep_awake_toggled(self, checked: bool):
        if not util.set_keep_awake(checked) and checked:
            # The OS refused the lock — don't leave a checked button implying
            # the PC is being held awake when it isn't.
            self._set_awake_checked(False)
            self.settings.keep_awake = False
            self.status_label.setText("Couldn't prevent sleep on this system")
            return
        self.settings.keep_awake = checked
        self.settings.save()
        self.status_label.setText(
            "Keeping the PC awake (monitor can still sleep)" if checked
            else "Sleep back to the normal Windows setting")

    # ---- auto-shutdown when the queue finishes -----------------------
    def _set_shutdown_checked(self, checked: bool):
        self.act_shutdown.blockSignals(True)
        self.act_shutdown.setChecked(checked)
        self.act_shutdown.blockSignals(False)

    def _on_shutdown_toggled(self, checked: bool):
        # A live toggle — can be flipped mid-render; only acted on at finish.
        self.settings.shutdown_when_done = checked
        self.settings.save()

    def _maybe_shutdown(self):
        if self.settings.shutdown_when_done and not self._ended_by_user:
            self._begin_shutdown_countdown()

    def _run_shutdown(self):
        # Cross-platform force shutdown (see util.shutdown_now).
        util.shutdown_now()

    def _begin_shutdown_countdown(self, secs: int = 60):
        """Give a cancel window before pulling the plug, so an accidental toggle
        (or a late change of mind) can't lose unsaved work."""
        dlg = QDialog(self)
        dlg.setWindowTitle("Shutdown scheduled")
        dlg.setMinimumWidth(380)
        v = QVBoxLayout(dlg)
        lbl = QLabel()
        lbl.setWordWrap(True)
        v.addWidget(lbl)
        btns = QHBoxLayout()
        cancel_btn = QPushButton("Cancel shutdown")
        now_btn = QPushButton("Shut down now")
        btns.addStretch(1)
        btns.addWidget(cancel_btn)
        btns.addWidget(now_btn)
        v.addLayout(btns)

        state = {"n": secs, "go": False}

        def render_text():
            lbl.setText("Render queue finished.\n\n"
                        f"The PC will shut down in {state['n']} seconds.\n"
                        "Running programs will be force-closed.")

        def tick():
            state["n"] -= 1
            if state["n"] <= 0:
                state["go"] = True
                dlg.accept()
            else:
                render_text()

        timer = QTimer(dlg)
        timer.setInterval(1000)
        timer.timeout.connect(tick)

        def go_now():
            state["go"] = True
            dlg.accept()

        def cancel():
            state["go"] = False
            dlg.reject()

        now_btn.clicked.connect(go_now)
        cancel_btn.clicked.connect(cancel)
        render_text()
        timer.start()
        dlg.exec()
        timer.stop()
        if state["go"]:
            self._run_shutdown()

    # ---- session persistence (job list survives restarts) -----------
    def _load_session(self):
        for job in session.load_jobs():
            self.jobs.append(job)
            self._append_row(job)
        self._sync_range_controls()
        self._sync_start_action()
        # Anything whose cached info went stale (file re-saved since) is re-read.
        self.info_probe.enqueue(self.jobs)
        # Materialise the portable data files on first run so they live next to
        # the executable straight away (visible + writable-location check).
        self.settings.save()
        self._save_session()

    def _save_session(self):
        # Background playblasts are deliberately not persisted: their .blend is
        # a snapshot that gets deleted the moment the render ends, so restoring
        # one would only put a job pointing at a missing file back in the list.
        session.save_jobs([j for j in self.jobs if not j.is_playblast])

    def shutdown(self):
        """Called by the toolset window's closeEvent (an embedded widget never
        gets its own). If a render is in progress, freeze it into a resumable
        paused job BEFORE saving, so reopening continues from the current frame
        instead of restarting (kill() is async and wouldn't commit otherwise)."""
        self.controller.hold_for_resume()
        self._save_session()
        # A playblast can't be resumed (its snapshot isn't coming back), so
        # closing mid-playblast bins the temp .blend rather than leaking it
        # into %TEMP% forever.
        for j in self.jobs:
            if j.is_playblast and j.temp_blend:
                util.remove_snapshot(j.filepath)
        self.settings.save()
        # Release the sleep lock via util, NOT the toggle handler — the handler
        # would clear settings.keep_awake and lose the user's choice for the
        # next launch. (Process exit would release it too; this is just tidy.)
        util.set_keep_awake(False)
        self.joblog.cleanup()

    def set_capture_busy(self, busy):
        """RenderingPage grey-out hook while Blender is busy on the bridge —
        the queue drives its own headless Blender, so nothing to disable."""
