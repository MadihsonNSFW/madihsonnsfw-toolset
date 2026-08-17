"""The Optimization tab — make a heavy scene fit in memory before you render it.

Five tools on the shared LAYOUT A shell:

  Optimize     Adaptive        size every texture by how big it lands in camera
               Fixed size      one size for a whole set of images
               Meshes          a managed Decimate on distant geometry
  Maintenance  Restore         put everything back; re-make missing stand-ins
               Memory report   what is actually eating the RAM

WHERE THE SETTINGS LIVE
In the app's own config.json, and they are passed to Blender with every command.
There is deliberately no second store in the add-on: the engine takes each dial
as an argument so it can be driven headless, and one store cannot drift from
another. (The Bone picker made the opposite choice for the same good reason —
there the add-on owned the settings first. `docs\\bone-picker.md`.)

⚠ EVERY REPEATING POLL PASSES `poll=True`. A connect to a dead localhost port is
not refused on Marty's machine — the SYN is dropped — so an un-flagged poll burns
the full timeout on the GUI thread every tick (`docs\\app-shell.md`).

HOW A RUN IS DRIVEN, AND WHY IT IS SHAPED THIS WAY
A run can take minutes, and every command here is one blocking bridge call. That
leaves two separate freezes, which are NOT the same problem:

  * **Blender** freezes, and nothing can change that — the work is all bpy data
    access, so it has to own Blender's main thread. It is said plainly in the UI.
  * **This app** used to freeze with it, purely because the call was made on the
    GUI thread. That was a bug and it is fixed: the call goes to a `_Runner`
    daemon thread and the reply comes back as a queued signal.

⚠ AND THAT IS ALSO WHAT MAKES A REAL PROGRESS BAR POSSIBLE. While the main
thread is busy, the add-on answers `opt_progress` on its SOCKET thread instead of
queueing it, so asking "how far along?" gets a straight answer mid-resize. The
bar counts actual items — it is not a decoration. On an add-on older than 0.12.0
there is nothing to ask, so it falls back to a busy animation rather than the
whole tab switching off (`bridge.FEATURE_REQUIREMENTS`).

⚠ THE BAR LIVES ON THE PAGE, NOT IN THE TOOL, and it has to. A run greys every
tool out through `set_capture_busy`, and Qt draws every child of a disabled
widget disabled too — `setEnabled(True)` on a child cannot escape it. A bar
inside the tool would spend the whole run greyed out.
"""
import os
import threading
import traceback

import blendsize
import bridge as bridgemod
import config
import dev_console
import theme
from rendering import RenderingPage
from widgets import NoScrollComboBox, ValueSlider

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox,
                               QFileDialog, QFormLayout, QHBoxLayout,
                               QHeaderView, QInputDialog, QLabel, QLineEdit,
                               QMessageBox, QProgressBar, QPushButton,
                               QStyledItemDelegate, QTableWidget,
                               QTableWidgetItem, QTreeWidget, QTreeWidgetItem,
                               QVBoxLayout, QWidget)

POLL_MS = 2500
# Slower than the picker's 1.5 s on purpose: nothing here changes unless the
# user changes it, and the reply walks every image in the file.

PROGRESS_MS = 300
# How often the bar asks how far the run has got. Cheap enough to be smooth: the
# add-on answers it off a plain dict without touching bpy, so it costs one
# localhost round trip and no Blender work at all.

# How many polls in a row may fail before the bar gives up and just spins. Covers
# every reason the answer might not come — an add-on too old to have the command,
# a Blender that went away mid-run — without matching on error text.
PROGRESS_GIVE_UP = 3

FEATURE = "scene_optimizer"

# What each target set means, in the user's words rather than ours.
TARGET_LABELS = (
    ("SELECTED", "Selected objects"),
    ("SCENE", "Everything in this scene"),
    ("ALL_OBJECTS", "Every object in the file"),
    ("IMAGES_NO_HDR", "All images except HDR/EXR"),
    ("IMAGES_HDR", "HDR/EXR images only"),
    ("ALL_IMAGES", "Every image in the file"),
)
OBJECT_TARGETS = {"SELECTED", "SCENE", "ALL_OBJECTS"}

FIXED_SIZES = (128, 256, 512, 1024, 2048, 4096, 8192)


class _Runner(QObject):
    """One blocking bridge call on a daemon thread.

    Same shape as the Physics tab's worker: the signals are emitted from the
    thread and Qt queues them back onto the GUI thread, so nothing here touches
    a widget off-thread.
    """

    done = Signal(object)
    failed = Signal(str)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self.fn = fn

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        try:
            result = self.fn()
        except bridgemod.BridgeError as exc:
            dev_console.BUFFER.add("ERROR",
                                   "Optimizer bridge call failed: %s" % exc)
            self.failed.emit(str(exc))
        except Exception as exc:      # noqa: BLE001
            # A worker thread dying silently is exactly what the console is for;
            # sys.excepthook does not cover threads.
            dev_console.BUFFER.add(
                "CRIT", "Unexpected error in an optimizer worker:\n%s"
                % traceback.format_exc())
            self.failed.emit(str(exc))
        else:
            self.done.emit(result)


class ProgressRow(QWidget):
    """The tab's one progress bar. Owned by the PAGE — see the module note.

    Starts as a busy animation and becomes a real count as soon as Blender
    reports a stage it can count. Both states are honest: "something is
    happening" is all we know until the add-on has worked out how many textures
    there are.
    """

    def __init__(self, bridge, parent=None):
        super().__init__(parent)
        self.bridge = bridge
        self._misses = 0
        self._counting = False
        self._command = "opt_progress"

        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 2, 8, 4)
        lay.setSpacing(10)
        self.bar = QProgressBar()
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(10)
        self.bar.setRange(0, 0)
        lay.addWidget(self.bar, 1)
        self.label = QLabel("")
        self.label.setObjectName("dim")
        lay.addWidget(self.label)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll)
        self.hide()

    # ------------------------------------------------------------------

    def start(self, label, command="opt_progress"):
        """Show the bar for a run. `command` is which progress command to poll.

        ⚠ Parameterised because the tab once hosted a second kind of run
        (Quadify, removed 2026-08-17) polling its own progress command. Keep it
        that way: one proven widget beats a second bar with its own miss
        counter, its own capability check and its own bugs. Both records
        have the same shape (`active/phase/done/total/item`), which is a
        deliberate constraint on the add-on side, not a coincidence.
        """
        self._command = command
        self._misses = 0
        self._counting = False
        self.bar.setRange(0, 0)          # busy until there is something to count
        self.label.setText("%s…" % label)
        self.show()
        # An add-on that cannot be asked simply spins. Checked up front so the
        # common case never fires a doomed request, and backed by the miss
        # counter below for everything a capability list cannot predict.
        if self.bridge is not None and self.bridge.supports(command):
            self.timer.start(PROGRESS_MS)

    def stop(self):
        self.timer.stop()
        self.hide()
        self.label.setText("")
        self.bar.setRange(0, 0)
        self._counting = False

    def poll(self):
        try:
            report = getattr(self.bridge, self._command)(poll=True)
        except bridgemod.BridgeError:
            self._misses += 1
            if self._misses >= PROGRESS_GIVE_UP:
                self.timer.stop()        # keep spinning; the run is still going
            return
        self._misses = 0
        if not isinstance(report, dict) or not report.get("active"):
            # Between stages, or the reply beat the run's own. Leave whatever is
            # on screen rather than flicking back to busy for one tick.
            return
        self.apply(report)

    def apply(self, report):
        """Paint one progress reading. Split out so it can be driven in tests."""
        total = int(report.get("total") or 0)
        done = min(int(report.get("done") or 0), total) if total else 0
        phase = str(report.get("phase") or "Working")
        if total > 0:
            self.bar.setRange(0, total)
            self.bar.setValue(done)
            self._counting = True
            text = "%s — %d of %d" % (phase, done, total)
            item = str(report.get("item") or "")
            if item:
                if len(item) > 34:
                    item = item[:31] + "…"
                text += "  ·  %s" % item
        elif self._counting:
            # A stage with nothing in it, after one that had items: keep the bar
            # determinate and full rather than dropping back to the busy sweep,
            # which reads as "it started over".
            text = phase
        else:
            self.bar.setRange(0, 0)
            text = phase
        self.label.setText(text + "…")


class OptimizerPage(RenderingPage):
    """Top-level 'Optimization' tab."""

    EMPTY_TEXT = (
        "No optimization tools yet.\n\n"
        "This tab shrinks textures and distant meshes to what your\n"
        "render actually needs — and puts them all back in one click.")

    def __init__(self, bridge, window, parent=None, empty_text=None):
        super().__init__(bridge, window, parent, empty_text)
        # Below the rail/stack splitter, spanning the tab. Deliberately NOT
        # inside a tool: a run disables every tool, and Qt greys a disabled
        # widget's children with it whatever their own enabled state says.
        self.progress = ProgressRow(bridge, self)
        self.layout().addWidget(self.progress)

    def set_capture_busy(self, busy):
        for _title, _group, widget in self._tools:
            if hasattr(widget, "set_capture_busy"):
                widget.set_capture_busy(busy)


class _OptimizerTool(QWidget):
    """Shared plumbing: settings, the bridge guard, the capability gate.

    Only ONE tool owns the poll (AdaptiveTool); the rest are fed by its
    `status_refreshed` signal, so the tab makes one round trip per tick however
    many tools are open.

    Two class attributes exist so a tool in this tab that is NOT the optimizer
    can share this plumbing without inheriting the optimizer's assumptions:

    - `CONFIG_KEY` — which config.json group its dials live in. A tool that is
      not the optimizer keeps its own group rather than landing in this dict.
    - ⚠ `BROADCASTS` — whether this tool's replies are an optimizer STATUS.
      `_finished` fans every reply out to all six tools, which is right when
      every command answers with the whole status and **actively wrong** for a
      tool whose reply is a retopo result: the other tools would call
      `apply_status` on it and repaint from a dict that has none of their keys.
    """

    CONFIG_KEY = "optimizer"
    BROADCASTS = True

    def __init__(self, bridge, window, parent=None):
        super().__init__(parent)
        self.bridge = bridge
        self.window = window
        self._busy = False
        self._status = {}
        self._syncing = False
        self._runner = None     # holds the in-flight worker alive

    # ------------------------------------------------------------- settings

    def settings(self):
        """The saved dials, as the engine's parameters."""
        if self.window is None:
            return {}
        return dict(self.window.cfg.get(self.CONFIG_KEY, {}))

    def save_settings(self, **changes):
        """Write a dial back to config.json.

        ⚠ Never while the lock preview is being built. That constructs a real
        page with a dead bridge and throws it away — every control's
        initialisation would otherwise write its default over whatever the user
        actually chose, from a tab they have not even unlocked.
        """
        if self.window is None or getattr(self.window, "_previewing", False):
            return
        group = dict(self.window.cfg.get(self.CONFIG_KEY, {}))
        group.update(changes)
        self.window.cfg[self.CONFIG_KEY] = group
        config.save(self.window.cfg)

    def params(self, **extra):
        """Settings + this call's extras, with the cache folder resolved.

        An empty `cache_dir` means "wherever the add-on puts them by default" —
        resolved from the status rather than guessed at here, because the
        default is the machine Blender is on, not the machine the app is on.
        """
        out = self.settings()
        if not out.get("cache_dir"):
            out["cache_dir"] = self._status.get("default_cache") or ""
        out.update(extra)
        return out

    # ------------------------------------------------------------- helpers

    @staticmethod
    def _dim(text):
        label = QLabel(text)
        label.setObjectName("dim")
        label.setWordWrap(True)
        return label

    def _fail(self, exc):
        self.status.setStyleSheet("color: #e06c60;")
        self.status.setText(str(exc))

    def _ok(self, text):
        self.status.setStyleSheet("")
        self.status.setText(text)

    def feature_reason(self):
        """Why the optimizer is unavailable on the installed add-on, or None."""
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

    def page(self):
        """The Optimization page, which owns the tab's one progress bar."""
        return getattr(self.window, "optimizer", None)

    def _call(self, label, fn, *args, then=None,
              progress_command="opt_progress"):
        """Run a bridge command OFF THE GUI THREAD, with the bar up.

        ⚠ This used to call `fn` inline and return its status. That is what
        froze the whole app for the length of a run — reported on a fixed-size
        resize, and it would have happened on every button in this tab. Nothing
        here may block the GUI thread again, which is why this hands back
        nothing: the reply arrives at `_finished`, and a caller that needs it
        passes `then`.

        Every optimizer command answers with the WHOLE status, so the tab
        repaints from the reply rather than firing a second round trip.
        """
        if not self._guarded():
            return
        if self.window is not None and not self.window.bridge_free_for_tools():
            return
        self._busy = True
        page = self.page()
        if page is not None:
            page.progress.start(label, progress_command)
        if self.window is not None:
            # Greys every tab out and parks the health poll: Blender's main
            # thread is ours until this comes back, so any other command would
            # simply queue behind it. The progress bar lives outside everything
            # this disables, on purpose.
            self.window.begin_capture(label, verb="running")
        runner = _Runner(lambda: fn(*args), parent=self)
        runner.done.connect(lambda status: self._finished(status, then))
        runner.failed.connect(self._failed)
        self._runner = runner
        runner.start()

    def _end_run(self):
        """Take the bar down and give the window back. Idempotent on purpose —
        `end_capture` is a counter, and unbalancing it would leave the app
        greyed out with nothing running."""
        if not self._busy:
            return
        self._busy = False
        self._runner = None
        page = self.page()
        if page is not None:
            page.progress.stop()
        if self.window is not None:
            self.window.end_capture()

    def _finished(self, status, then=None):
        self._end_run()
        if self.BROADCASTS:
            self.broadcast(status)
        if then is not None:
            then(status)

    def _failed(self, message):
        self._end_run()
        self._fail(message)

    def broadcast(self, status):
        """Push a fresh status to every tool. Overridden by the poll owner."""
        if self.window is not None:
            owner = getattr(self.window, "optimizer_adaptive_tool", None)
            if owner is not None and owner is not self:
                owner.apply_status(status)
                owner.status_refreshed.emit(status)
                return
        self.apply_status(status)

    def apply_status(self, status):
        raise NotImplementedError

    def set_capture_busy(self, busy):
        self.setEnabled(not busy)

    # -------------------------------------------------------- shared widgets

    def _target_combo(self, objects_only=False):
        combo = NoScrollComboBox()
        for key, label in TARGET_LABELS:
            if objects_only and key not in OBJECT_TARGETS:
                continue
            combo.addItem(label, key)
        current = self.settings().get("target", "SCENE")
        index = combo.findData(current)
        combo.setCurrentIndex(index if index >= 0 else 0)
        combo.currentIndexChanged.connect(
            lambda _i, c=combo: self.save_settings(target=c.currentData()))
        return combo

    def _report(self, status, key="result"):
        """Turn a command's tally into one readable line."""
        result = (status or {}).get(key) or {}
        summary = result.get("summary")
        if not summary:
            return "Done."
        failures = result.get("failed") or []
        if failures:
            first = failures[0]
            summary += "  First problem: %s — %s." % (first.get("name"),
                                                      first.get("reason"))
        return summary


class AdaptiveTool(_OptimizerTool):
    """Size every texture by how big its object lands in the camera.

    Owns the ONLY poll — the other tools are fed from `status_refreshed`.
    """

    status_refreshed = Signal(object)

    def __init__(self, bridge, window, parent=None):
        super().__init__(bridge, window, parent)
        settings = self.settings()

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        lay.addWidget(self._dim(
            "Each texture is shrunk to the size it actually needs, measured "
            "from how big its object looks through the render camera. Your "
            "original files are never touched — smaller copies are written to "
            "a cache folder and put back with one click."))

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight)

        self.combo_target = self._target_combo(objects_only=True)
        form.addRow("Optimize", self.combo_target)

        self.slider_quality = ValueSlider(
            0.1, 2.0, float(settings.get("quality", 1.0)), decimals=2,
            tooltip="1.00 asks for exactly the on-screen size. Lower it to "
                    "trade sharpness for memory — 0.25 is often enough.")
        self.slider_quality.valueChanged.connect(
            lambda v: self.save_settings(quality=float(v)))
        form.addRow("Quality", self.slider_quality)

        self.slider_min = ValueSlider(
            32, 4096, int(settings.get("min_size", 256)), decimals=0,
            suffix=" px",
            tooltip="Nothing is shrunk below this, including anything out of "
                    "frame — it can still show up in a reflection.")
        self.slider_min.valueChanged.connect(
            lambda v: self.save_settings(min_size=int(v)))
        form.addRow("Smallest", self.slider_min)

        self.slider_max = ValueSlider(
            256, 8192, int(settings.get("max_size", 4096)), decimals=0,
            suffix=" px", tooltip="No texture is asked for above this.")
        self.slider_max.valueChanged.connect(
            lambda v: self.save_settings(max_size=int(v)))
        form.addRow("Largest", self.slider_max)

        self.chk_animation = QCheckBox(
            "Size for the whole animation, not just this frame")
        self.chk_animation.setChecked(bool(settings.get("animation", False)))
        self.chk_animation.setToolTip(
            "Steps the frame range and keeps each texture's biggest "
            "appearance, so something that walks up to the camera stays sharp "
            "when it gets there. Slower — it evaluates every stepped frame.")
        self.chk_animation.toggled.connect(self._on_animation)
        form.addRow("", self.chk_animation)

        self.slider_step = ValueSlider(
            1, 50, int(settings.get("frame_step", 1)), decimals=0,
            tooltip="Check every Nth frame. 1 is exact; higher is faster and "
                    "can miss a single-frame close-up.")
        self.slider_step.valueChanged.connect(
            lambda v: self.save_settings(frame_step=int(v)))
        form.addRow("Frame step", self.slider_step)
        # Kept because Qt does NOT disable a QFormLayout's label with its field,
        # and walking back up to find it later is a layout guess.
        self._step_label = form.labelForField(self.slider_step)

        self.chk_meshes = QCheckBox("Also decimate distant meshes")
        self.chk_meshes.setChecked(bool(settings.get("meshes", False)))
        self.chk_meshes.setToolTip(
            "Adds a managed Decimate to far-away geometry, using the settings "
            "on the Meshes page. Off by default — decimation can hurt a "
            "character; it is for environment clutter.")
        self.chk_meshes.toggled.connect(
            lambda v: self.save_settings(meshes=bool(v)))
        form.addRow("", self.chk_meshes)
        lay.addLayout(form)

        row = QHBoxLayout()
        self.btn_preview = QPushButton("What would change?")
        self.btn_preview.setToolTip(
            "Work out every new size and show them here, changing nothing.")
        self.btn_preview.clicked.connect(self.preview)
        row.addWidget(self.btn_preview)
        self.btn_overlay = QPushButton("Show in viewport")
        self.btn_overlay.setToolTip(
            "Draw the same answers over the 3D Viewport in Blender, next to "
            "each object. Press Esc in Blender to close it.")
        self.btn_overlay.clicked.connect(self.overlay)
        row.addWidget(self.btn_overlay)
        row.addStretch(1)
        self.btn_run = QPushButton("Optimize")
        self.btn_run.setDefault(True)
        self.btn_run.clicked.connect(self.run)
        row.addWidget(self.btn_run)
        lay.addLayout(row)

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Texture", "Now", "Would be"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(0,
                                                           QHeaderView.Stretch)
        self.table.setMinimumHeight(150)
        lay.addWidget(self.table, 1)

        self.status = QLabel("—")
        self.status.setObjectName("dim")
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(self.status)

        # ⚠ the SYNC, not the handler — the handler saves, and a constructor
        # must not write the user's config (see `_sync_animation`)
        self._sync_animation(self.chk_animation.isChecked())

        self.timer = None            # stays None when the feature is gated
        reason = self.feature_reason()
        if reason:
            self.setEnabled(False)
            self._fail(reason)
            return

        # ⚠ THE POLL ONLY RUNS WHILE THIS TOOL IS ON SCREEN — the same rule,
        # and the same show/hide signals, as the render queue's sysmon (Marty,
        # 2026-08-05: polling belongs to the tab you are looking at). Started
        # here with `.start(POLL_MS)`, it asked Blender for `opt_status` every
        # 2.5 s for the LIFE OF THE APP, from a tab that may never be opened —
        # a GUI-thread socket round trip nobody was reading.
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)

    def showEvent(self, event):
        super().showEvent(event)
        if self.timer is not None and not self.timer.isActive():
            self.timer.start(POLL_MS)
            # After the switch paints, not during it: the first discovery that
            # Blender died pays the connect cap, and that stall must not ride
            # on the tab switch itself (markers.showEvent documents the same
            # dropped-SYN behaviour of a dead localhost port).
            QTimer.singleShot(0, self.refresh)

    def hideEvent(self, event):
        super().hideEvent(event)
        if self.timer is not None:
            self.timer.stop()

    # ------------------------------------------------------------------

    def _sync_animation(self, on):
        """Match the step controls to the tickbox — and write NOTHING.

        ⚠ Split out of `_on_animation` because the constructor calls it to get
        the initial enabled state right, and going through the handler meant
        **building the window SAVED the config**. The value written was the one
        just restored on the line that makes the tickbox, so nothing was ever
        lost — but it is the very pattern `save_settings` warns about, and it
        is invisible on a machine that already has a `config.json`: only a
        fresh checkout shows it, as a file appearing out of nowhere.
        """
        self.slider_step.setEnabled(bool(on))
        if self._step_label is not None:
            self._step_label.setEnabled(bool(on))

    def _on_animation(self, on):
        self._sync_animation(on)
        self.save_settings(animation=bool(on))

    def refresh(self):
        if not self._guarded():
            return
        if self.window is not None and not self.window.bridge_free_for_tools():
            return
        try:
            status = self.bridge.opt_status(poll=True)
        except bridgemod.BridgeError:
            return                      # the status bar already says so
        self.apply_status(status)
        self.status_refreshed.emit(status)

    def broadcast(self, status):
        self.apply_status(status)
        self.status_refreshed.emit(status)

    def apply_status(self, status):
        if not isinstance(status, dict):
            return
        self._status = status
        camera = status.get("camera")
        managed = status.get("managed") or []
        self.btn_run.setEnabled(bool(camera))
        self.btn_preview.setEnabled(bool(camera))
        self.btn_overlay.setEnabled(bool(camera))
        if not status.get("addon_can_resize", True):
            self._fail("This Blender build has no OpenImageIO, so textures "
                       "cannot be resized.")
            return
        if not camera:
            self._fail("This scene has no active camera — the optimizer "
                       "measures how big things look through it.")
            return
        bits = ["%d object(s), %d image(s)" % (status.get("objects", 0),
                                               status.get("images", 0))]
        if managed:
            bits.append("%d texture(s) currently optimized" % len(managed))
            broken = [m for m in managed if m.get("missing")]
            if broken:
                bits.append("%d missing from the cache — use Restore → "
                            "Re-make missing copies" % len(broken))
        self._ok("Camera: %s. %s." % (camera, ". ".join(bits)))

    # ------------------------------------------------------------------

    def _fill_plan(self, rows):
        self._syncing = True
        try:
            self.table.setRowCount(len(rows))
            for index, row in enumerate(rows):
                self.table.setItem(index, 0,
                                   QTableWidgetItem(str(row.get("name", ""))))
                now = row.get("from") or 0
                self.table.setItem(index, 1, QTableWidgetItem(
                    "%d px" % now if now else "?"))
                if not row.get("ok", True):
                    text = row.get("reason") or "skipped"
                elif not now or row.get("to", 0) >= now:
                    text = "unchanged"
                else:
                    text = "%d px" % row.get("to", 0)
                self.table.setItem(index, 2, QTableWidgetItem(text))
        finally:
            self._syncing = False

    def preview(self):
        self._ok("Working out the sizes…")
        self._call("Measuring the scene", self.bridge.opt_plan, self.params(),
                   then=self.show_plan)

    def show_plan(self, status):
        plan = status.get("plan") or {}
        rows = plan.get("images") or []
        self._fill_plan(rows)
        shrinking = [r for r in rows
                     if r.get("ok") and r.get("from") and
                     r.get("to", 0) < r["from"]]
        meshes = plan.get("meshes") or {}
        note = "%d of %d texture(s) would shrink" % (len(shrinking), len(rows))
        if plan.get("bytes_saved"):
            note += ", saving about %s" % plan.get("human_saved")
        if meshes:
            note += "; %d mesh(es) would be decimated" % len(meshes)
        self._ok(note + ". Nothing has been changed.")

    def overlay(self):
        self._call("Measuring the scene", self.bridge.opt_preview_start,
                   self.params(), then=lambda _status: self._ok(
                       "Showing the preview in Blender's 3D Viewport — press "
                       "Esc there to close it."))

    def run(self):
        question = ("Optimize now?\n\nBlender will be busy while this runs — "
                    "with a lot of 4K textures that can take minutes. This "
                    "window stays usable and shows how far along it is.\n\n"
                    "Your original files are not modified, and Restore puts "
                    "everything back.")
        if QMessageBox.question(self, "Optimize", question,
                                QMessageBox.Yes | QMessageBox.No,
                                QMessageBox.Yes) != QMessageBox.Yes:
            return
        self._ok("Optimizing…")
        self._call("Optimizing the scene", self.bridge.opt_adaptive,
                   self.params(), then=self.show_run)

    def show_run(self, status):
        note = self._report(status)
        if "mesh_result" in status:
            note += "  Meshes: " + self._report(status, "mesh_result")
        self._ok(note)
        # The plan on screen described the file as it was before this run.
        self.table.setRowCount(0)


class FixedSizeTool(_OptimizerTool):
    """One size for a whole set of images.

    This is the only way to reach the world's environment texture: an HDRI
    belongs to no object, so no camera-based pass can ever see it. "HDR/EXR
    images only" plus a size is the one-click way to cap it.
    """

    def __init__(self, bridge, window, parent=None):
        super().__init__(bridge, window, parent)
        settings = self.settings()

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        lay.addWidget(self._dim(
            "Give a whole set of images one size, without measuring anything. "
            "This is also the only way to reach your world's HDRI — it belongs "
            "to no object, so the camera-based pass never sees it."))

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight)
        self.combo_target = self._target_combo()
        form.addRow("Resize", self.combo_target)

        self.combo_size = NoScrollComboBox()
        for size in FIXED_SIZES:
            self.combo_size.addItem("%d px" % size, size)
        index = self.combo_size.findData(int(settings.get("fixed_size", 1024)))
        self.combo_size.setCurrentIndex(index if index >= 0 else 3)
        self.combo_size.currentIndexChanged.connect(
            lambda _i: self.save_settings(
                fixed_size=int(self.combo_size.currentData())))
        form.addRow("To", self.combo_size)
        lay.addLayout(form)

        lay.addWidget(self._dim(
            "Anything already at or below that size is left alone — nothing is "
            "ever enlarged."))

        row = QHBoxLayout()
        self.btn_queue = QPushButton("Add to queue")
        self.btn_queue.setToolTip(
            "Line this target and size up instead of running it now, then add "
            "more. Each queued job becomes its own named texture set, so a "
            "queue of three gives you three sets to switch between. "
            "Double-click a name in the list to call it something useful.")
        self.btn_queue.clicked.connect(self.enqueue)
        row.addWidget(self.btn_queue)
        row.addStretch(1)
        self.btn_run = QPushButton("Resize images")
        self.btn_run.clicked.connect(self.run)
        row.addWidget(self.btn_run)
        lay.addLayout(row)

        # ---- the queue -------------------------------------------------
        self.queue = QTableWidget(0, 3)
        self.queue.setHorizontalHeaderLabels(["Name", "Resize", "To"])
        self.queue.setSelectionBehavior(QAbstractItemView.SelectRows)
        # Double-click edits the NAME, and only the name — the other two cells
        # are what the job actually is and are set when it is queued.
        self.queue.setEditTriggers(QAbstractItemView.DoubleClicked
                                   | QAbstractItemView.EditKeyPressed)
        self.queue.itemChanged.connect(self._queue_renamed)
        self.queue.verticalHeader().setVisible(False)
        self.queue.horizontalHeader().setSectionResizeMode(0,
                                                           QHeaderView.Stretch)
        self.queue.setMaximumHeight(140)
        self.queue.hide()
        lay.addWidget(self.queue)
        qrow = QHBoxLayout()
        self.btn_unqueue = QPushButton("Remove")
        self.btn_unqueue.clicked.connect(self.dequeue)
        self.btn_clear_queue = QPushButton("Clear queue")
        self.btn_clear_queue.clicked.connect(self.clear_queue)
        for b in (self.btn_unqueue, self.btn_clear_queue):
            b.hide()
            qrow.addWidget(b)
        qrow.addStretch(1)
        lay.addLayout(qrow)
        self._jobs = []

        # ---- the named sets --------------------------------------------
        lay.addWidget(self._dim(
            "<b>Texture sets.</b> Every resize is remembered as a named set — "
            "one per queued job — so you can keep one resolution for one scene "
            "and another for the next, and switch between them. A set is only "
            "a note about which textures should be which size — your originals "
            "are never part of it and Restore always works."))
        self.sets = QTableWidget(0, 3)
        self.sets.setHorizontalHeaderLabels(["Texture set", "Textures", "Cache"])
        self.sets.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.sets.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.sets.verticalHeader().setVisible(False)
        self.sets.horizontalHeader().setSectionResizeMode(0,
                                                          QHeaderView.Stretch)
        self.sets.setMinimumHeight(110)
        self.sets.itemSelectionChanged.connect(self._sync_set_buttons)
        lay.addWidget(self.sets, 1)

        srow = QHBoxLayout()
        self.btn_use = QPushButton("Switch to this set")
        self.btn_use.clicked.connect(self.use_set)
        srow.addWidget(self.btn_use)
        self.btn_rename = QPushButton("Rename…")
        self.btn_rename.clicked.connect(self.rename_set)
        srow.addWidget(self.btn_rename)
        self.btn_forget = QPushButton("Forget")
        self.btn_forget.setToolTip(
            "Remove the set from this list. Your textures and your originals "
            "are untouched — this deletes a note, not a state.")
        self.btn_forget.clicked.connect(self.forget_set)
        srow.addWidget(self.btn_forget)
        srow.addStretch(1)
        lay.addLayout(srow)

        self.status = QLabel("—")
        self.status.setObjectName("dim")
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(self.status)

        self._sync_set_buttons()
        # ⚠ THE QUEUE AND THE SETS NEED add-on 0.12.0, AND THEY HIDE RATHER
        # THAN FAIL. On an older add-on `jobs` is simply ignored and the resize
        # runs off `target`/`size` — so a user who queued three of them would
        # get one, silently, against whatever the combos happened to show. A
        # control that does something other than what it says is worse than one
        # that is not there. Resizing itself is untouched: degrade the feature,
        # not the tab (same rule as the progress bar).
        self._sets_available = True
        try:
            self._sets_available = bool(self.bridge.supports("opt_group_apply"))
        except Exception:               # noqa: BLE001 - a dead bridge is routine
            pass                        # fail OPEN: unknown is not "missing"
        if not self._sets_available:
            for widget in (self.btn_queue, self.sets, self.btn_use,
                           self.btn_rename, self.btn_forget):
                widget.hide()

        reason = self.feature_reason()
        if reason:
            self.setEnabled(False)
            self._fail(reason)

    # ------------------------------------------------------------- queue

    def _job_label(self, job):
        """What the two read-only columns say: WHAT it will resize, and to what.

        A job queued from a selection reports the objects it captured rather
        than the words "Selected objects" — that count is the visible proof
        that this row is fixed to those objects and will not quietly follow
        the selection somewhere else before the queue runs.
        """
        objects = job.get("objects")
        if objects is None:
            label = next((t for k, t in TARGET_LABELS if k == job["target"]),
                         job["target"])
        elif len(objects) == 1:
            label = objects[0]
        else:
            label = "%d selected objects" % len(objects)
        return label, "%d px" % job["size"]

    def _auto_name(self, target, size, objects):
        """A name worth reading before the user has typed one."""
        label = self._job_label({"target": target, "size": size,
                                 "objects": objects})[0]
        return "%s %d px" % (label, size)

    def _refresh_queue(self):
        # ⚠ Inside `_syncing`: setItem fires itemChanged, which is where a
        # rename is picked up. Without this, rebuilding the table would read
        # its own cells back as if the user had typed them.
        self._syncing = True
        try:
            self.queue.setRowCount(len(self._jobs))
            for index, job in enumerate(self._jobs):
                label, size = self._job_label(job)
                name = QTableWidgetItem(job.get("name", ""))
                name.setToolTip("Double-click to rename. This is what the "
                                "texture set will be called.")
                self.queue.setItem(index, 0, name)
                for column, text in ((1, label), (2, size)):
                    cell = QTableWidgetItem(text)
                    cell.setFlags(cell.flags() & ~Qt.ItemIsEditable)
                    self.queue.setItem(index, column, cell)
        finally:
            self._syncing = False
        showing = bool(self._jobs)
        self.queue.setVisible(showing)
        self.btn_unqueue.setVisible(showing)
        self.btn_clear_queue.setVisible(showing)
        self.btn_run.setText("Run queue (%d)" % len(self._jobs) if showing
                             else "Resize images")

    def _queue_renamed(self, item):
        if self._syncing or item.column() != 0:
            return
        row = item.row()
        if not (0 <= row < len(self._jobs)):
            return
        job = self._jobs[row]
        typed = item.text().strip()
        # An empty name is not a name. Rather than leaving a blank row and
        # letting the add-on invent something at run time, put the automatic
        # one back so what is on screen is what will be created.
        job["name"] = typed or self._auto_name(job["target"], job["size"],
                                               job.get("objects"))
        if job["name"] != item.text():
            self._syncing = True
            try:
                item.setText(job["name"])
            finally:
                self._syncing = False

    def enqueue(self):
        target = self.combo_target.currentData()
        size = int(self.combo_size.currentData())
        if target != "SELECTED":
            # Every other target means the same thing whenever it is resolved
            # — "everything in this scene" is not a snapshot and should not be.
            self._add_job(target, size, None)
            return
        # ⚠ A FRESH read, not the last poll's. The poll is 2.5 s apart and the
        # gesture here is "select these, add them" — a job built from a
        # selection that old is a job for the wrong objects.
        self._call("Reading the selection", self.bridge.opt_status,
                   then=lambda status: self._enqueue_selection(status, size))

    def _enqueue_selection(self, status, size):
        names = (status or {}).get("selected_objects")
        if names is None:
            # Older add-on: it can resize perfectly well, it just cannot say
            # WHICH objects are selected, and a queue that cannot capture that
            # is the bug this replaced. Refuse the one thing, keep the rest.
            self._fail(
                "This Blender add-on cannot tell the queue which objects are "
                "selected. Update the extension from ⚙ Library Settings to "
                "queue selections — everything else here still works.")
            return
        if not names:
            self._fail("Nothing is selected in Blender, so there is nothing "
                       "to queue.")
            return
        self._add_job("SELECTED", size, names)

    def _add_job(self, target, size, objects):
        job = {"target": target, "size": size,
               "name": self._auto_name(target, size, objects)}
        if objects is not None:
            job["objects"] = list(objects)
        self._jobs.append(job)
        self._refresh_queue()
        self._ok("%d job(s) queued. Each one becomes its own texture set you "
                 "can switch between — double-click a name to change it."
                 % len(self._jobs))

    def dequeue(self):
        rows = sorted({i.row() for i in self.queue.selectedIndexes()},
                      reverse=True)
        for row in rows:
            if 0 <= row < len(self._jobs):
                del self._jobs[row]
        self._refresh_queue()

    def clear_queue(self):
        self._jobs = []
        self._refresh_queue()

    # ------------------------------------------------------------- sets

    def _selected_set(self):
        row = self.sets.currentRow()
        groups = self._status.get("groups") or []
        return groups[row] if 0 <= row < len(groups) else None

    def _sync_set_buttons(self):
        chosen = self._selected_set()
        for button in (self.btn_use, self.btn_rename, self.btn_forget):
            button.setEnabled(chosen is not None)

    def use_set(self):
        chosen = self._selected_set()
        if chosen is None:
            return
        if chosen.get("missing"):
            # ⚠ Told BEFORE the run, not after. A set whose cached files have
            # been deleted looks perfectly healthy in a list of names, and
            # switching to it would otherwise just sit there rebuilding with no
            # explanation of why it is slow.
            if QMessageBox.question(
                    self, "Cached files are missing",
                    "%d of this set's %d cached textures are no longer on "
                    "disk — the cache folder has been cleared or moved.\n\n"
                    "They can be re-made from your originals, but that takes "
                    "as long as the first resize did. Go ahead?"
                    % (chosen["missing"], chosen["count"]),
                    QMessageBox.Yes | QMessageBox.No,
                    QMessageBox.Yes) != QMessageBox.Yes:
                return
        self._call("Switching texture set", self.bridge.opt_group_apply,
                   {"name": chosen["name"]},
                   then=lambda status: self._ok(
                       "Now on “%s”. %s" % (chosen["name"],
                                            self._report(status))))

    def rename_set(self):
        chosen = self._selected_set()
        if chosen is None:
            return
        name, accepted = QInputDialog.getText(
            self, "Rename texture set", "Name", text=chosen["name"])
        if accepted and name.strip():
            self._call("Renaming", self.bridge.opt_group_rename,
                       {"name": chosen["name"], "new_name": name.strip()})

    def forget_set(self):
        chosen = self._selected_set()
        if chosen is None:
            return
        if QMessageBox.question(
                self, "Forget this set?",
                "Forget “%s”?\n\nYour textures stay exactly as they are and "
                "your originals are untouched — this only removes the note "
                "that these textures belong together."  % chosen["name"],
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No) != QMessageBox.Yes:
            return
        self._call("Forgetting", self.bridge.opt_group_delete,
                   {"name": chosen["name"]})

    # ------------------------------------------------------------------

    def apply_status(self, status):
        if not isinstance(status, dict):
            return
        self._status = status
        if not status.get("addon_can_resize", True):
            self.btn_run.setEnabled(False)
            self._fail("This Blender build has no OpenImageIO, so textures "
                       "cannot be resized.")
            return
        self.btn_run.setEnabled(True)

        groups = status.get("groups") or []
        self._syncing = True
        try:
            self.sets.setRowCount(len(groups))
            for index, group in enumerate(groups):
                name = group.get("name", "")
                item = QTableWidgetItem(
                    ("● " + name) if group.get("active") else name)
                if group.get("active"):
                    item.setToolTip("This is the set the scene is on now")
                self.sets.setItem(index, 0, item)
                self.sets.setItem(index, 1, QTableWidgetItem(
                    "%d at %s" % (group.get("count", 0),
                                  ", ".join("%d px" % s
                                            for s in group.get("sizes") or []))))
                missing = group.get("missing", 0)
                cache = QTableWidgetItem(
                    "%d missing" % missing if missing else "ready")
                if missing:
                    cache.setForeground(QColor("#e0a33d"))
                    cache.setToolTip(
                        "Cached files for this set have been deleted or the "
                        "cache folder was cleared. Switching to it will re-make "
                        "them from your originals, which takes time.")
                self.sets.setItem(index, 2, cache)
        finally:
            self._syncing = False
        self._sync_set_buttons()

        managed = len(status.get("managed") or [])
        note = "%d image(s) in the file, %d currently optimized." % (
            status.get("images", 0), managed)
        stale = [g for g in groups if g.get("missing")]
        if stale:
            self._fail(
                "%s  ⚠ %d texture set(s) have cached files missing — the cache "
                "was cleared or moved. Switching to one will re-make them."
                % (note, len(stale)))
            return
        if status.get("active_group"):
            note += "  On texture set “%s”." % status["active_group"]
        self._ok(note)

    def run(self):
        params = self.params(target=self.combo_target.currentData(),
                             size=int(self.combo_size.currentData()))
        if self._jobs:
            params["jobs"] = list(self._jobs)
        self._ok("Resizing…")
        self._call("Resizing textures", self.bridge.opt_resize, params,
                   then=self.show_resize)

    def show_resize(self, status):
        self.clear_queue()
        result = status.get("result") or {}
        made = result.get("groups")
        if made is None:
            # An add-on from before one-set-per-job. It still made a set, it
            # just made one for the whole run.
            made = [result["group"]] if result.get("group") else []
        note = self._report(status)
        if len(made) == 1:
            note += "  Saved as texture set “%s”." % made[0]
        elif made:
            note += "  Saved as %d texture sets — %s. Pick one below and " \
                    "press Switch to this set to move between them." % (
                        len(made), ", ".join("“%s”" % name for name in made))
        self._ok(note)


class MeshesTool(_OptimizerTool):
    """A managed Decimate on distant geometry.

    ⚠ Off the main path on purpose. Decimation can destroy UV seams and shape
    keys, so it is right for environment clutter and wrong for a hero character.
    The UI says so rather than leaving it to be discovered.
    """

    def __init__(self, bridge, window, parent=None):
        super().__init__(bridge, window, parent)
        settings = self.settings()

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        lay.addWidget(self._dim(
            "Far-away meshes get a Decimate modifier called "
            "<b>MADI_Opt_Decimate</b>, with the ratio set by distance. It is "
            "added at the end of the stack and removed again by Restore — any "
            "Decimate you added yourself is matched by name and never touched."))
        warning = self._dim(
            "⚠ Decimation can wreck UV seams and shape keys. Use it on "
            "background clutter, not on a character you are animating.")
        warning.setStyleSheet("color: #e0a33d;")
        lay.addWidget(warning)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight)
        self.combo_target = self._target_combo(objects_only=True)
        form.addRow("Decimate", self.combo_target)

        self.slider_floor = ValueSlider(
            100, 200000, int(settings.get("face_floor", 5000)), decimals=0,
            suffix=" faces",
            tooltip="Meshes with fewer faces than this are left alone — "
                    "decimating a simple mesh saves nothing and can only "
                    "make it worse.")
        self.slider_floor.valueChanged.connect(
            lambda v: self.save_settings(face_floor=int(v)))
        form.addRow("Only above", self.slider_floor)

        self.slider_full = ValueSlider(
            0.0, 500.0, float(settings.get("full_distance", 20.0)), decimals=1,
            suffix=" m",
            tooltip="Anything closer than this keeps every face.")
        self.slider_full.valueChanged.connect(
            lambda v: self.save_settings(full_distance=float(v)))
        form.addRow("Full detail to", self.slider_full)

        self.slider_low = ValueSlider(
            1.0, 5000.0, float(settings.get("low_distance", 200.0)),
            decimals=1, suffix=" m",
            tooltip="Past this distance the lowest ratio is used. Between the "
                    "two distances the ratio slides evenly.")
        self.slider_low.valueChanged.connect(
            lambda v: self.save_settings(low_distance=float(v)))
        form.addRow("Lowest detail at", self.slider_low)

        self.slider_ratio = ValueSlider(
            0.01, 1.0, float(settings.get("low_ratio", 0.2)), decimals=2,
            tooltip="How many faces the furthest meshes keep. 0.20 keeps a "
                    "fifth of them.")
        self.slider_ratio.valueChanged.connect(
            lambda v: self.save_settings(low_ratio=float(v)))
        form.addRow("Lowest ratio", self.slider_ratio)
        lay.addLayout(form)

        row = QHBoxLayout()
        row.addStretch(1)
        self.btn_revert = QPushButton("Remove them")
        self.btn_revert.clicked.connect(self.revert)
        row.addWidget(self.btn_revert)
        self.btn_run = QPushButton("Decimate meshes")
        self.btn_run.clicked.connect(self.run)
        row.addWidget(self.btn_run)
        lay.addLayout(row)

        self.status = QLabel("—")
        self.status.setObjectName("dim")
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(self.status)
        lay.addStretch(1)

        reason = self.feature_reason()
        if reason:
            self.setEnabled(False)
            self._fail(reason)

    def apply_status(self, status):
        if not isinstance(status, dict):
            return
        self._status = status
        decimated = status.get("decimated") or []
        self.btn_run.setEnabled(bool(status.get("camera")))
        self.btn_revert.setEnabled(bool(decimated))
        if not status.get("camera"):
            self._fail("This scene has no active camera — distance is measured "
                       "from it.")
            return
        if decimated:
            self._ok("%d object(s) currently carry MADI_Opt_Decimate."
                     % len(decimated))
        else:
            self._ok("No meshes are decimated right now.")

    def run(self):
        self._ok("Working out distances…")
        self._call("Decimating meshes", self.bridge.opt_decimate,
                   self.params(target=self.combo_target.currentData(),
                               meshes=True),
                   then=lambda status: self._ok(
                       self._report(status, "mesh_result")))

    def revert(self):
        self._call("Removing decimation", self.bridge.opt_revert_meshes,
                   self.params(target=self.combo_target.currentData()),
                   then=lambda status: self._ok(
                       self._report(status, "mesh_result")))


class RestoreTool(_OptimizerTool):
    """Put everything back, and look after the cache folder."""

    def __init__(self, bridge, window, parent=None):
        super().__init__(bridge, window, parent)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        lay.addWidget(self._dim(
            "Everything this tab does is reversible. Restoring points each "
            "image back at your own file and removes the modifiers we added — "
            "the smaller copies stay in the cache, so optimizing again later "
            "costs nothing."))

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight)
        row = QHBoxLayout()
        self.edit_cache = QLineEdit()
        self.edit_cache.setPlaceholderText("the add-on's default folder")
        self.edit_cache.setText(self.settings().get("cache_dir", ""))
        self.edit_cache.editingFinished.connect(
            lambda: self.save_settings(cache_dir=self.edit_cache.text().strip()))
        row.addWidget(self.edit_cache, 1)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self.pick_cache)
        row.addWidget(browse)
        holder = QWidget()
        holder.setLayout(row)
        row.setContentsMargins(0, 0, 0, 0)
        form.addRow("Cache folder", holder)
        lay.addLayout(form)
        lay.addWidget(self._dim(
            "The smaller copies live here. It is a cache — deleting it is safe, "
            "and anything still in use is re-made the next time the file is "
            "opened."))

        buttons = QVBoxLayout()
        self.btn_images = QPushButton("Put all textures back")
        self.btn_images.clicked.connect(self.revert_images)
        buttons.addWidget(self.btn_images)
        self.btn_meshes = QPushButton("Remove every added Decimate")
        self.btn_meshes.clicked.connect(self.revert_meshes)
        buttons.addWidget(self.btn_meshes)
        self.btn_regen = QPushButton("Re-make missing copies")
        self.btn_regen.setToolTip(
            "Rebuilds any smaller copy that has gone missing or is older than "
            "the file it came from — and moves them all into the cache folder "
            "above, which is how you re-home them after changing it.")
        self.btn_regen.clicked.connect(self.regenerate)
        buttons.addWidget(self.btn_regen)
        self.btn_clear = QPushButton("Clear cache folder")
        self.btn_clear.setToolTip(
            "Frees the disk. Every optimized texture goes back on your own "
            "file first, and only files this tool wrote are deleted — "
            "anything else in that folder is counted and left alone.")
        self.btn_clear.clicked.connect(self.clear_cache)
        buttons.addWidget(self.btn_clear)
        lay.addLayout(buttons)
        # Hidden rather than broken on an add-on too old to have the command.
        # Same rule as the texture sets: degrade the feature, not the tab.
        try:
            if not self.bridge.supports("opt_clear_cache"):
                self.btn_clear.hide()
        except Exception:               # noqa: BLE001 - a dead bridge is routine
            pass                        # fail OPEN: unknown is not "missing"

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Texture", "Size", "State"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(0,
                                                           QHeaderView.Stretch)
        self.table.setMinimumHeight(140)
        lay.addWidget(self.table, 1)

        self.status = QLabel("—")
        self.status.setObjectName("dim")
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(self.status)

        reason = self.feature_reason()
        if reason:
            self.setEnabled(False)
            self._fail(reason)

    def pick_cache(self):
        start = self.edit_cache.text().strip() or \
            self._status.get("default_cache", "")
        chosen = QFileDialog.getExistingDirectory(self, "Cache folder", start)
        if chosen:
            self.edit_cache.setText(chosen)
            self.save_settings(cache_dir=chosen)

    def apply_status(self, status):
        if not isinstance(status, dict):
            return
        self._status = status
        managed = status.get("managed") or []
        decimated = status.get("decimated") or []
        self.btn_images.setEnabled(bool(managed))
        self.btn_meshes.setEnabled(bool(decimated))
        self.btn_regen.setEnabled(bool(managed))
        if not self.edit_cache.text().strip():
            self.edit_cache.setPlaceholderText(
                status.get("default_cache") or "the add-on's default folder")

        # ⚠ The rebuild stays inside `_syncing`: setRowCount/setItem emit
        # selection signals, and a poll landing mid-edit must not re-enter.
        self._syncing = True
        try:
            self.table.setRowCount(len(managed))
            for index, entry in enumerate(managed):
                self.table.setItem(index, 0,
                                   QTableWidgetItem(entry.get("name", "")))
                self.table.setItem(index, 1, QTableWidgetItem(
                    "%d px" % entry.get("size", 0)))
                # ⚠ "original missing" outranks "copy missing". A missing copy
                # is a one-click rebuild; a missing ORIGINAL means Restore
                # cannot give this texture back, which is the one failure this
                # tab must never let someone walk into unwarned.
                if entry.get("original_missing"):
                    item = QTableWidgetItem("ORIGINAL MISSING")
                    item.setForeground(QColor("#e06c60"))
                    item.setToolTip(
                        "The file this was made from is no longer where it was "
                        "(%s). Restore cannot put it back until it is there "
                        "again." % (entry.get("resolved") or "unknown path"))
                elif entry.get("missing"):
                    item = QTableWidgetItem("copy missing")
                    item.setForeground(QColor("#e0a33d"))
                else:
                    item = QTableWidgetItem("optimized")
                self.table.setItem(index, 2, item)
        finally:
            self._syncing = False

        stranded = [m for m in managed if m.get("original_missing")]
        if stranded:
            self._fail(
                "%d texture(s) cannot be restored — the original file is no "
                "longer where it was. Put it back, or use Re-make missing "
                "copies after moving it, and this will clear."
                % len(stranded))
            return

        parts = []
        if managed:
            parts.append("%d texture(s) optimized" % len(managed))
        if decimated:
            parts.append("%d mesh(es) decimated" % len(decimated))
        self._ok(", ".join(parts) + "." if parts
                 else "Nothing is optimized right now.")

    def revert_images(self):
        self._call("Restoring textures", self.bridge.opt_revert_images,
                   self.params(target="ALL_IMAGES"),
                   then=lambda status: self._ok(self._report(status)))

    def revert_meshes(self):
        self._call("Removing decimation", self.bridge.opt_revert_meshes,
                   self.params(target="ALL_OBJECTS"),
                   then=lambda status: self._ok(
                       self._report(status, "mesh_result")))

    def regenerate(self):
        self._ok("Re-making copies…")
        self._call("Re-making copies", self.bridge.opt_regenerate,
                   self.params(),
                   then=lambda status: self._ok(self._report(status)))

    def clear_cache(self):
        """Delete the stand-ins. Every consequence is spelled out FIRST.

        This is the one control in the tab that deletes files, so the dialog
        says what goes, what survives, and what happens to the scene — before
        it happens, not in the report afterwards.
        """
        folder = (self.edit_cache.text().strip()
                  or self._status.get("default_cache")
                  or "the add-on's default folder")
        managed = len(self._status.get("managed") or [])
        sets = len(self._status.get("groups") or [])
        lines = ["Delete the smaller copies in:\n%s" % folder]
        if managed:
            lines.append(
                "Your %d optimized texture(s) go back to full size first. "
                "They point AT the files being deleted, so that part is not "
                "optional." % managed)
        if sets:
            lines.append(
                "Your %d texture set(s) are kept. They will report their "
                "files as missing until you switch to one, which re-makes "
                "them from your originals." % sets)
        lines.append("Your own texture files are never touched, and anything "
                     "in that folder this tool did not write is left alone.")
        if QMessageBox.question(
                self, "Clear the cache folder?", "\n\n".join(lines),
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No) != QMessageBox.Yes:
            return
        self._ok("Clearing…")
        self._call("Clearing the cache", self.bridge.opt_clear_cache,
                   self.params(), then=self.show_clear)

    def show_clear(self, status):
        cache = status.get("cache") or {}
        note = ""
        if cache.get("restored"):
            note = "%d texture(s) put back. " % cache["restored"]
        note += "Removed %d file(s), %s freed." % (
            cache.get("removed", 0), cache.get("bytes_human", "0 B"))
        if cache.get("kept"):
            note += "  %d file(s) in that folder were not ours and were left " \
                    "where they are." % cache["kept"]
        failed = cache.get("failed") or []
        if failed:
            self._fail("%s  %d could not be deleted — first: %s (%s)." % (
                note, len(failed), failed[0].get("name"),
                failed[0].get("reason")))
            return
        self._ok(note)


class MemoryTool(_OptimizerTool):
    """What is actually eating the memory in this scene."""

    def __init__(self, bridge, window, parent=None):
        super().__init__(bridge, window, parent)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        lay.addWidget(self._dim(
            "A rough measure of what each mesh and texture costs in memory, "
            "biggest first — so you can see what is worth optimizing instead "
            "of guessing."))
        caveat = self._dim(
            "⚠ These are estimates. They ignore mip maps, GPU compression and "
            "render buffers, so compare rows with each other rather than "
            "reading the total as a VRAM figure.")
        caveat.setStyleSheet("color: %s;" % theme.TEXT_DIM)
        lay.addWidget(caveat)

        row = QHBoxLayout()
        row.addStretch(1)
        self.btn_run = QPushButton("Measure this scene")
        self.btn_run.clicked.connect(self.run)
        row.addWidget(self.btn_run)
        lay.addLayout(row)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["", "Name", "Size", "Share"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(1,
                                                           QHeaderView.Stretch)
        self.table.setMinimumHeight(200)
        lay.addWidget(self.table, 1)

        # What a RENDER of this scene needs on the GPU — separate from the
        # table above, which is what the scene DATA costs. Hidden until there
        # is a figure, so an add-on too old to send one leaves no empty box.
        self.vram = QLabel("")
        self.vram.setWordWrap(True)
        self.vram.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.vram.hide()
        lay.addWidget(self.vram)

        self.status = QLabel("—")
        self.status.setObjectName("dim")
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(self.status)

        reason = self.feature_reason()
        if reason:
            self.setEnabled(False)
            self._fail(reason)

    def apply_status(self, status):
        if not isinstance(status, dict):
            return
        self._status = status
        estimate = status.get("estimate")
        if estimate:
            self._fill(estimate)

    def _fill(self, estimate):
        rows = estimate.get("rows") or []
        self._syncing = True
        try:
            self.table.setRowCount(len(rows))
            for index, row in enumerate(rows):
                self.table.setItem(index, 0,
                                   QTableWidgetItem(row.get("kind", "")))
                self.table.setItem(index, 1,
                                   QTableWidgetItem(row.get("name", "")))
                self.table.setItem(index, 2,
                                   QTableWidgetItem(row.get("human", "")))
                self.table.setItem(index, 3, QTableWidgetItem(
                    "%.1f%%" % (100.0 * row.get("share", 0.0))))
        finally:
            self._syncing = False
        note = "About %s across %d datablock(s)" % (
            estimate.get("total_human", "?"), estimate.get("counted", 0))
        if estimate.get("shown", 0) < estimate.get("counted", 0):
            note += " — showing the largest %d" % estimate["shown"]
        self._ok(note + ". Estimates, not measurements.")
        self._fill_vram(estimate.get("vram") or {})

    def _fill_vram(self, vram):
        """The two render figures. Blank when the add-on is too old to send
        them — the table above is still worth having on its own."""
        if not vram:
            self.vram.setText("")
            self.vram.hide()
            return
        self.vram.show()
        self.vram.setText(
            "<b>To render this scene</b> at %d×%d (%s):<br>"
            "• from the command line, or the Render Queue: <b>%s</b><br>"
            "• from inside Blender: <b>%s</b> — the viewport and interface "
            "keep about %s on the card that a background render never "
            "allocates.<br>"
            "<span style='color:%s'>Includes ~%s of render buffers and ~%s of "
            "mesh acceleration data. Rough guides, not measurements: what "
            "actually fits depends on your driver, your engine build and "
            "whatever else is using the GPU.</span>"
            % (vram.get("resolution", [0, 0])[0],
               vram.get("resolution", [0, 0])[1],
               vram.get("engine", "?"),
               vram.get("headless_human", "?"),
               vram.get("interactive_human", "?"),
               vram.get("ui_human", "?"),
               theme.TEXT_DIM,
               vram.get("buffer_human", "?"),
               vram.get("bvh_human", "?")))

    def run(self):
        self._ok("Measuring…")
        self._call("Measuring memory", self.bridge.opt_estimate, {},
                   then=lambda status: self._fill(status.get("estimate") or {}))


# ---------------------------------------------------------------------------
# Blend file size — what is taking up room in the FILE, as opposed to in RAM
# ---------------------------------------------------------------------------

class _ScanWorker(QObject):
    """One `blendsize.scan` on a daemon thread.

    Same shape as `_Runner`, with a progress signal — a 2 GB file takes several
    seconds and there is a real byte count to show, so there is no reason for a
    busy sweep here.

    ⚠ It does NOT go through `_call`. That wraps a run in `begin_capture`,
    which greys the whole app and parks the health poll because Blender's main
    thread is busy. Nothing here touches Blender: the file is read off disk,
    and every other tab stays usable while it happens.

    ⚠ **IT IS DELIBERATELY PARENTLESS, AND `retired` IS THE WHOLE REASON.** A
    worker parented to the tool would be kept alive by that parent for the life
    of the app, so every scan would leave one behind. But it also cannot simply
    be dropped when a new scan starts: its thread may still be inside
    `blendsize.scan` and about to `emit`, and emitting from a QObject Python
    has already collected is a crash. So the owner holds it until `retired`
    fires, which is the LAST thing `_run` does — by the time the slot runs on
    the GUI thread, the thread has returned and the object is safe to drop.
    """

    done = Signal(object)
    failed = Signal(str)
    progress = Signal(int, int)
    retired = Signal(object)        # carries self, so the owner can let go

    def __init__(self, path):
        super().__init__()          # ⚠ no parent — see the class docstring
        self.path = path
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        try:
            result = blendsize.scan(
                self.path,
                progress=lambda done, total: self.progress.emit(done, total),
                should_cancel=lambda: self._cancel)
        except blendsize.BlendSizeError as exc:
            if not self._cancel:
                self.failed.emit(str(exc))
        except Exception as exc:        # noqa: BLE001
            dev_console.BUFFER.add(
                "CRIT", "Unexpected error reading a .blend:\n%s"
                % traceback.format_exc())
            self.failed.emit("Could not read that .blend: %s" % exc)
        else:
            if not self._cancel:
                self.done.emit(result)
        finally:
            # LAST statement in the thread, always reached — a cancelled or
            # failed scan has to release its worker exactly like a good one, or
            # cancelling repeatedly is its own slow leak.
            self.retired.emit(self)


class _ShareBar(QStyledItemDelegate):
    """A share-of-file bar painted into the Share column.

    Reads `theme.ACCENT` at paint time rather than caching a QColor, so a theme
    swap repaints correctly — `theme.apply_theme` rebinds module globals and a
    colour captured at construction would be frozen forever.

    One colour for every row on purpose. The palette's green/amber/red/gold
    carry MEANING elsewhere in the app, and a size chart that borrows them
    starts implying that a big mesh is a bad one.
    """

    def paint(self, painter, option, index):
        super().paint(painter, option, index)
        share = index.data(Qt.UserRole)
        if share is None:
            return
        rect = option.rect.adjusted(4, 0, -8, 0)
        if rect.width() <= 2:
            return
        height = 7
        top = rect.y() + (rect.height() - height) // 2
        painter.save()
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(theme.BORDER))
        painter.drawRoundedRect(rect.x(), top, rect.width(), height, 2, 2)
        filled = int(rect.width() * max(0.0, min(1.0, float(share))))
        if share > 0:
            painter.setBrush(QColor(theme.ACCENT))
            painter.drawRoundedRect(rect.x(), top, max(2, filled), height, 2, 2)
        painter.restore()


class FileSizeTool(_OptimizerTool):
    """What is making the .blend BIG — read from the file, not from Blender.

    Deliberately separate from Memory report, which answers a different
    question: that one estimates RAM for the render-visible scene and sees
    meshes and images only. This one reads the saved file and sees everything
    in it — shape keys, bind data, vertex groups, orphans, packed images — to
    the byte. On Marty's own Softbody file the two disagree by an order of
    magnitude, because 90% of that file is things a RAM estimate never walks.

    ⚠ It needs NO add-on and NO bridge: it opens the .blend itself. So there is
    no `feature_reason` gate here and nothing to degrade when Blender is shut.

    ⚠ **The tree lives IN THE TOOL, not in a dialog** (Marty, 2026-08-12: *"can
    you make it work in the same one"*). It was a `BlendSizeWindow(QDialog)`
    for one afternoon. Mounted with `scroll=False`, because the tree scrolls
    itself and a wrapping scroll area would nest two scrollbars.
    """

    LAZY = "__lazy__"

    def __init__(self, bridge, window, parent=None):
        super().__init__(bridge, window, parent)
        self._workers = set()       # alive until each one's `retired` arrives
        self._summary = {}

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        lay.addWidget(self._dim(
            "Opens the .blend and adds up what is actually inside it, biggest "
            "first — every mesh, image and shape key by name, and what each "
            "one is made of. Exact, not estimated. Reads the file as it was "
            "last SAVED; Blender does not need to be running."))

        row = QHBoxLayout()
        self.head = QLabel("—")
        self.head.setWordWrap(True)
        self.head.setTextInteractionFlags(Qt.TextSelectableByMouse)
        row.addWidget(self.head, 1)
        self.btn_open = QPushButton("Measure the open .blend")
        self.btn_open.setObjectName("accent")
        self.btn_open.clicked.connect(self.measure_open)
        row.addWidget(self.btn_open)
        self.btn_pick = QPushButton("Choose a file…")
        self.btn_pick.clicked.connect(self.measure_chosen)
        row.addWidget(self.btn_pick)
        lay.addLayout(row)

        # ⚠ Every column means ONE thing. The last one showed a datablock
        # COUNT on type rows and a PERCENTAGE on the rows under them, with no
        # header — two units in one unlabelled column. The count moved into
        # the type's name, where it reads as what it is.
        self.tree = QTreeWidget()
        self.tree.setColumnCount(4)
        self.tree.setHeaderLabels(["Name", "Size", "Share of file", "%"])
        self.tree.setUniformRowHeights(True)
        self.tree.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tree.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tree.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self.tree.setColumnWidth(1, 92)
        self.tree.setColumnWidth(2, 170)
        self.tree.setColumnWidth(3, 70)
        self.tree.setItemDelegateForColumn(2, _ShareBar(self.tree))
        self.tree.itemExpanded.connect(self._expanded)
        self.tree.setMinimumHeight(240)
        lay.addWidget(self.tree, 1)

        self.bar = QProgressBar()
        self.bar.setTextVisible(True)
        self.bar.hide()
        lay.addWidget(self.bar)

        self.note = QLabel("")
        self.note.setObjectName("dim")
        self.note.setWordWrap(True)
        lay.addWidget(self.note)

        self.status = QLabel("—")
        self.status.setObjectName("dim")
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(self.status)

        if not blendsize.zstd_available():
            # ⚠ Nearly every .blend saved today is zstd, so this is the
            # normal-case failure and it has to be said before the button is
            # pressed rather than reported as an unreadable file.
            self._fail("This build cannot read compressed .blend files, which "
                       "is most of them. Uncompressed files still work.")

    # ------------------------------------------------------------- the file

    def blend_path(self):
        """The .blend the connected Blender has open, or ''.

        Comes from the health poll's `file`, which every add-on version has
        sent — no capability check and no new bridge command for something the
        app is already told.
        """
        return getattr(self.window, "_connected_file", "") or ""

    def apply_status(self, status):
        if isinstance(status, dict):
            self._status = status
        path = self.blend_path()
        self.btn_open.setEnabled(bool(path) and not self._scanning())
        if not blendsize.zstd_available() or self._scanning():
            return
        if self._summary:
            return                      # a result is on screen; leave it named
        if path:
            self._ok("Open in Blender: %s" % os.path.basename(path))
        else:
            self._ok("No saved .blend is open in Blender — use Choose a file "
                     "to measure one on disk.")

    # -------------------------------------------------------------- actions

    def measure_open(self):
        path = self.blend_path()
        if not path:
            self._fail("Blender has no saved file open.")
            return
        if not os.path.isfile(path):
            # The bridge may be driving a Blender on another machine, or the
            # file may have moved since it was opened.
            self._fail("That .blend is not readable from here: %s" % path)
            return
        self.measure(path)

    def measure_chosen(self):
        start = os.path.dirname(self.blend_path()) or ""
        path, _filter = QFileDialog.getOpenFileName(
            self, "Choose a .blend to measure", start,
            "Blender files (*.blend *.blend1);;All files (*)")
        if path:
            self.measure(path)

    # ------------------------------------------------------------- scanning

    def _scanning(self):
        return any(not worker._cancel for worker in self._workers)

    def measure(self, path):
        """Start over on *path*. Safe to call while a scan is running."""
        self.stop()
        self.tree.clear()
        self._summary = {}
        self.head.setText("Reading %s…" % os.path.basename(path))
        self.head.setStyleSheet("")
        self.note.setText("")
        self.bar.setRange(0, 0)
        self.bar.setFormat("Reading…")
        self.bar.show()
        self.btn_open.setEnabled(False)
        self._ok("Measuring %s…" % os.path.basename(path))

        worker = _ScanWorker(path)
        worker.done.connect(self._loaded)
        worker.failed.connect(self._failed)
        worker.progress.connect(self._progress)
        worker.retired.connect(self._retire)
        self._workers.add(worker)
        worker.start()

    def stop(self):
        """Cancel anything in flight. The worker itself is dropped by
        `_retire` once its thread has actually returned — never here."""
        for worker in self._workers:
            worker.cancel()

    def _retire(self, worker):
        self._workers.discard(worker)
        worker.deleteLater()

    def hideEvent(self, event):
        # Switching tools or tabs must not leave a thread reading a 2 GB file.
        # Same signal the render queue uses to park its timers.
        #
        # ⚠ **BUT ONLY A NON-SPONTANEOUS HIDE.** Windows sends a hide to the
        # whole window when the app is MINIMISED, and cancelling somebody's
        # scan because they got it out of the way for a moment would be a far
        # worse bug than a background read finishing unseen. `spontaneous()`
        # is exactly the difference: false for a programmatic hide (the stack
        # switching pages), true for one the window system caused.
        if not event.spontaneous():
            self.stop()
        super().hideEvent(event)

    def _progress(self, done, total):
        if total > 0:
            self.bar.setRange(0, 1000)
            self.bar.setValue(int(1000.0 * done / total))
            self.bar.setFormat("Reading… %s of %s" % (
                blendsize.human_bytes(done), blendsize.human_bytes(total)))

    def _failed(self, message):
        self.bar.hide()
        self.head.setText("—")
        self.btn_open.setEnabled(bool(self.blend_path()))
        self._fail(message)

    def _loaded(self, result):
        self.bar.hide()
        self.btn_open.setEnabled(bool(self.blend_path()))
        self._fill(result)

    # -------------------------------------------------------------- filling

    def _fill(self, result):
        """Build the tree. ⚠ `result` is NOT kept: the tree owns the parts it
        needs for level three, and holding the whole scan as well would keep a
        second copy of every datablock alive for the life of the app."""
        self.tree.clear()
        compression = result.get("compression")
        if compression:
            summary = ("<b>%s</b> — <b>%s</b> on disk, <b>%s</b> of data "
                       "inside it (%s-compressed, %.1f×)."
                       % (result["name"], result["disk_human"],
                          result["total_human"], compression,
                          result.get("ratio") or 0.0))
        else:
            summary = ("<b>%s</b> — <b>%s</b>, stored uncompressed."
                       % (result["name"], result["disk_human"]))
        summary += ("  %d datablocks, saved by Blender %s."
                    % (result["datablocks"], result["blender"]))
        self.head.setText(summary)

        for group in result.get("types") or []:
            node = QTreeWidgetItem([
                "%s  (%d)" % (group["kind"], group["count"]),
                group["human"], "",
                "%.1f%%" % (100.0 * group["share"])])
            node.setData(2, Qt.UserRole, group["share"])
            node.setTextAlignment(1, Qt.AlignRight | Qt.AlignVCenter)
            node.setTextAlignment(3, Qt.AlignRight | Qt.AlignVCenter)
            self.tree.addTopLevelItem(node)
            for item in group["items"]:
                child = QTreeWidgetItem([
                    item["name"], item["human"], "",
                    "%.1f%%" % (100.0 * item["share"])])
                child.setData(2, Qt.UserRole, item["share"])
                child.setTextAlignment(1, Qt.AlignRight | Qt.AlignVCenter)
                child.setTextAlignment(3, Qt.AlignRight | Qt.AlignVCenter)
                child.setData(0, Qt.UserRole + 1, item.get("parts") or [])
                if item.get("parts"):
                    child.addChild(QTreeWidgetItem([self.LAZY]))
                node.addChild(child)

        overhead = result.get("overhead") or []
        if overhead:
            total = sum(entry["bytes"] for entry in overhead)
            share = sum(entry["share"] for entry in overhead)
            node = QTreeWidgetItem([
                "The file's own bookkeeping  (%d)" % len(overhead),
                blendsize.human_bytes(total), "",
                "%.1f%%" % (100.0 * share)])
            node.setData(2, Qt.UserRole, share)
            node.setTextAlignment(1, Qt.AlignRight | Qt.AlignVCenter)
            node.setTextAlignment(3, Qt.AlignRight | Qt.AlignVCenter)
            for entry in overhead:
                child = QTreeWidgetItem([
                    entry["label"], entry["human"], "",
                    "%.1f%%" % (100.0 * entry["share"])])
                child.setData(2, Qt.UserRole, entry["share"])
                child.setTextAlignment(1, Qt.AlignRight | Qt.AlignVCenter)
                child.setTextAlignment(3, Qt.AlignRight | Qt.AlignVCenter)
                node.addChild(child)
            self.tree.addTopLevelItem(node)

        for index in range(min(3, self.tree.topLevelItemCount())):
            self.tree.topLevelItem(index).setExpanded(True)

        note = "Sizes are what each thing takes up INSIDE the file. "
        if compression:
            note += ("The file on disk is %s because Blender compresses it, so "
                     "these add up to more than its size in Explorer. "
                     % result["disk_human"])
        note += ("The last column is share of the file; inside a datablock it "
                 "is share of that datablock.")
        if not result.get("complete", True):
            note = ("⚠ This file ends without its closing block — it may be "
                    "truncated, so treat these figures as a partial read.  "
                    + note)
        self.note.setText(note)

        # Only what a later status line needs — never the scan itself.
        self._summary = {"name": result["name"], "path": result["path"],
                         "datablocks": result["datablocks"]}
        self._ok("%s — %s of data across %d datablocks. Exact, read from the "
                 "file." % (result["name"], result["total_human"],
                            result["datablocks"]))

    def _expanded(self, node):
        """Build a datablock's contents the first time it is opened.

        Levels one and two are a few thousand rows, which Qt builds instantly;
        expanding every datablock's contents up front would be tens of
        thousands, and there is no reason to pay for the ones nobody opens.
        """
        if node.childCount() != 1:
            return
        if node.child(0).text(0) != self.LAZY:
            return
        node.takeChild(0)
        for part in node.data(0, Qt.UserRole + 1) or []:
            child = QTreeWidgetItem([
                part["label"], part["human"], "",
                "%.1f%%" % (100.0 * part["share"])])
            child.setTextAlignment(1, Qt.AlignRight | Qt.AlignVCenter)
            child.setTextAlignment(3, Qt.AlignRight | Qt.AlignVCenter)
            structs = part.get("structs") or []
            if structs and structs != [part["label"]]:
                child.setToolTip(0, "Blender structs: %s" % ", ".join(structs))
            node.addChild(child)
