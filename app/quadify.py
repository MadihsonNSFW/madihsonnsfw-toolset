"""Quadify — quad retopology, in the Optimization tab. See docs\\quadify.md.

Marty's layout C, picked from four mockups on 2026-08-13: settings on the left,
a **result report** on the right. His reason for it is the whole design brief —
*a retopo you cannot measure is one you have to eyeball* — so the right panel is
not decoration, it is the point.

⚠ **THE REPORT SAYS WHAT WAS MEASURED, NEVER WHAT WAS ASKED FOR.** Every number
in it comes out of the add-on's reply, counted from the mesh that actually
arrived. Nothing here echoes a setting back as if it were an outcome.

⚠ **This tool does NOT own the tab's poll.** `AdaptiveTool` owns it and
re-broadcasts, so a seventh tool subscribing costs the tab nothing. What it
needs beyond that — is there an engine, what is selected — rides on its own
`quad_status`, fired when the tool becomes visible rather than on a timer.

⚠ **It subclasses `optimizer._OptimizerTool` on purpose.** That is where the
off-thread `_call`, `begin_capture`, the idempotent `_end_run` and the tab's one
progress bar already live, all of it paid for by a freeze Marty reported in
2026-08-04. A second copy of that plumbing would be a second place for the app
to lock up. Two class attributes keep the inheritance honest: `CONFIG_KEY` so
these dials do not land in the optimizer's settings, and `BROADCASTS = False`
because a retopo result is not an optimizer status and fanning it out would
have five other tools repaint from a dict with none of their keys in it.

⚠ **"Preserve rig data" carries a rig onto the result** (Marty, 2026-08-21):
deform modifiers, weights, materials, constraints and custom properties. The
transfer lives in the add-on's `quadpreserve`; this side is a tickbox, a line
saying what would be carried off the selected rig, and a line saying what
really was.

⚠⚠ **SHAPE KEYS ARE NOT CARRIED — THEY BAKE INTO THE GEOMETRY** at their
current values, the way Quad Remesher does it. Resampling them onto new
topology was built, shipped and removed the same day: it tore a real character
where the mesh folds back on itself. The panel must say **BAKED IN**, never
"carried", and must tell the user to set the frame first.

⚠ **UV maps are still NOT transferred, and no control may imply they are.**
That is the one channel left, and a panel that lists what it does while staying
quiet about what it does not is exactly the thing this project criticised
QRemeshify for. The note in the panel says so in as many words.

⚠ **"Fix concave faces" is ON by default** — Surface Deform refuses a target
containing one, and a quad remesh makes a few every time. It costs the all-quad
promise (a split concave quad leaves two triangles), so the report says how
many, counted off the BUILT MESH rather than the engine's face list.

⚠ **The counts come off the SELECTED RIG, not out of prose.** "Preserves
everything" tells nobody whether their 775 morphs are coming, or how big the
result will be when they do.
"""

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QCheckBox, QDoubleSpinBox, QFormLayout,
                               QGridLayout, QGroupBox, QHBoxLayout, QLabel,
                               QPushButton, QRadioButton, QSlider, QSpinBox,
                               QVBoxLayout, QWidget)

import optimizer as optimizermod
import theme

FEATURE = "quadify"

# Marty's rail order puts this with the other mesh work; the tab is already
# paid, so there is no gate to add on this side — the add-on carries it.
TITLE = "Quadify"


class QuadifyTool(optimizermod._OptimizerTool):
    """Retopologise a mesh to all quads, and report what came back."""

    CONFIG_KEY = "quadify"
    BROADCASTS = False

    def __init__(self, bridge, window, parent=None):
        super().__init__(bridge, window, parent)
        saved = self.settings()
        self._quad = {}

        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(12)

        left = QVBoxLayout()
        left.setSpacing(8)
        outer.addLayout(left, 1)

        # ⚠ **THE TARGET IS READ ON `showEvent`, NOT ON A TIMER**, which is the
        # right call for a reading nothing changes on its own — but it leaves
        # one real gap: change the selection in Blender while this tool is
        # already on screen and the label keeps naming the old object. Marty
        # hit it ("sometimes it doesn't update"). A poll would evaluate the
        # mesh every tick to keep a label warm, which is exactly what
        # `quad_status`'s docstring forbids. A button is the honest answer.
        pick = QHBoxLayout()
        self.target = QLabel("—")
        self.target.setObjectName("dim")
        self.target.setWordWrap(True)
        self.pick_button = QPushButton("Select mesh")
        self.pick_button.setToolTip("Take whatever is selected in Blender "
                                    "right now as the target.")
        self.pick_button.clicked.connect(self.pick_selected)
        pick.addWidget(self.target, 1)
        pick.addWidget(self.pick_button, 0, Qt.AlignTop)
        left.addLayout(pick)

        self.size_warning = QLabel("")
        self.size_warning.setWordWrap(True)
        self.size_warning.setStyleSheet("color: #d9a441;")
        self.size_warning.hide()
        left.addWidget(self.size_warning)

        # ---------------------------------------------------------- shape
        shape = QGroupBox("Shape")
        form = QFormLayout(shape)
        row = QHBoxLayout()
        self.density = QSlider(Qt.Horizontal)
        self.density.setRange(10, 400)          # 0.10 .. 4.00, in hundredths
        self.density.setValue(int(float(saved.get("density", 1.0)) * 100))
        self.density_out = QLabel("")
        self.density_out.setObjectName("dim")
        self.density_out.setMinimumWidth(38)
        row.addWidget(self.density, 1)
        row.addWidget(self.density_out)
        form.addRow("Density", row)

        sharp = QHBoxLayout()
        self.use_sharp = QCheckBox("Detect")
        self.use_sharp.setChecked(bool(saved.get("use_sharp", True)))
        self.sharp_angle = QDoubleSpinBox()
        self.sharp_angle.setRange(0.0, 180.0)
        self.sharp_angle.setDecimals(1)
        self.sharp_angle.setSuffix("°")
        self.sharp_angle.setValue(float(saved.get("sharp_angle", 35.0)))
        sharp.addWidget(self.use_sharp)
        sharp.addWidget(self.sharp_angle)
        sharp.addStretch(1)
        form.addRow("Sharp edges", sharp)

        axes = QHBoxLayout()
        self.symmetry = {}
        for axis in "XYZ":
            box = QCheckBox(axis)
            box.setChecked(axis.lower() in str(saved.get("symmetry", "")))
            axes.addWidget(box)
            self.symmetry[axis.lower()] = box
        axes.addStretch(1)
        form.addRow("Symmetry", axes)

        quality = QHBoxLayout()
        self.preprocess = QCheckBox("Preprocess")
        self.preprocess.setChecked(bool(saved.get("preprocess", True)))
        self.smoothing = QCheckBox("Smoothing")
        self.smoothing.setChecked(bool(saved.get("smoothing", True)))
        quality.addWidget(self.preprocess)
        quality.addWidget(self.smoothing)
        quality.addStretch(1)
        form.addRow("Quality", quality)

        # ⚠⚠ ON BY DEFAULT, and the default is the point. Blender's Surface
        # Deform REFUSES a target that contains a concave face - Marty hit it
        # as "target contains concave polygons" - and a quad remesh makes a
        # few around its singularities every time. A cage that cannot be bound
        # to is not a cage. ⚠ It costs the all-quad promise (a split concave
        # quad leaves two triangles), so the report says how many.
        fixes = QHBoxLayout()
        self.fix_concave = QCheckBox("Fix concave faces")
        self.fix_concave.setChecked(bool(saved.get("fix_concave", True)))
        self.fix_concave.setToolTip(
            "Split any concave face on the result. Surface Deform refuses to "
            "bind to a target that has one, so leave this on if the result "
            "will drive another mesh.")
        fixes.addWidget(self.fix_concave)
        fixes.addStretch(1)
        form.addRow("Clean up", fixes)
        left.addWidget(shape)

        # ---------------------------------------------------------- result
        result = QGroupBox("Result")
        rlay = QVBoxLayout(result)
        self.keep_both = QRadioButton("Keep both, hide the original")
        self.replace = QRadioButton("Replace the original")
        self.keep_both.setChecked(not bool(saved.get("replace", False)))
        self.replace.setChecked(bool(saved.get("replace", False)))
        rlay.addWidget(self.keep_both)
        rlay.addWidget(self.replace)

        # Marty, 2026-08-21: make the result move and behave like the original
        # — the way a part separated by loose parts still does.
        self.preserve = QCheckBox("Preserve rig data")
        self.preserve.setChecked(bool(saved.get("preserve", False)))
        self.preserve.setToolTip(
            "Carry the deform modifiers, weights, materials, constraints and "
            "custom properties onto the result. Shape keys are baked into the "
            "geometry at their current values rather than carried.")
        rlay.addWidget(self.preserve)
        # ⚠ What it will carry, counted off the SELECTED RIG, not described in
        # the abstract. "Preserves everything" tells nobody whether their 775
        # morphs are coming; "775 shape keys" does.
        self.preserve_note = QLabel("")
        self.preserve_note.setObjectName("dim")
        self.preserve_note.setWordWrap(True)
        rlay.addWidget(self.preserve_note)
        left.addWidget(result)

        # ----------------------------------------------------- fine tuning
        # ⚠ Only the EIGHT knobs the engine actually reads. Of QRemeshify's 19,
        # eleven are inert on any build without Gurobi — this one included —
        # and shipping them would be shipping controls that lie. Checkable
        # rather than collapsible: unchecked means the engine's own defaults,
        # which is what almost everyone wants and what the tests assume.
        tuning = QGroupBox("Fine tuning")
        tuning.setCheckable(True)
        tuning.setChecked(bool(saved.get("tuning", False)))
        grid = QGridLayout(tuning)
        self.tuning = tuning

        self.isometry_bias = QDoubleSpinBox()
        self.isometry_bias.setRange(0.0, 1.0)
        self.isometry_bias.setDecimals(4)
        self.isometry_bias.setSingleStep(0.001)
        self.isometry_bias.setValue(float(saved.get("isometry_bias", 0.005)))
        self.ngon_weight = QDoubleSpinBox()
        self.ngon_weight.setRange(0.0, 10.0)
        self.ngon_weight.setDecimals(2)
        self.ngon_weight.setValue(float(saved.get("ngon_regularity_weight",
                                                  0.9)))
        self.align_weight = QDoubleSpinBox()
        self.align_weight.setRange(0.0, 10.0)
        self.align_weight.setDecimals(2)
        self.align_weight.setValue(float(saved.get("singularity_align_weight",
                                                   0.1)))
        self.clusters = QSpinBox()
        self.clusters.setRange(0, 100000)
        self.clusters.setSpecialValueText("global")
        self.clusters.setValue(int(saved.get("chart_cluster_size", 0)))

        self.align_sing = QCheckBox("Align singularities")
        self.align_sing.setChecked(bool(saved.get("align_singularities", True)))
        self.repeat_quads = QCheckBox("Retry lost quad constraints")
        self.repeat_quads.setChecked(bool(saved.get("repeat_quads", False)))
        self.repeat_ngons = QCheckBox("Retry lost n-gon constraints")
        self.repeat_ngons.setChecked(bool(saved.get("repeat_ngons", False)))
        self.repeat_align = QCheckBox("Retry lost alignment constraints")
        self.repeat_align.setChecked(bool(saved.get("repeat_align", True)))

        grid.addWidget(QLabel("Isometry bias"), 0, 0)
        grid.addWidget(self.isometry_bias, 0, 1)
        grid.addWidget(QLabel("N-gon regularity"), 1, 0)
        grid.addWidget(self.ngon_weight, 1, 1)
        grid.addWidget(QLabel("Singularity align"), 2, 0)
        grid.addWidget(self.align_weight, 2, 1)
        grid.addWidget(QLabel("Chart clusters"), 3, 0)
        grid.addWidget(self.clusters, 3, 1)
        for offset, box in enumerate((self.align_sing, self.repeat_quads,
                                      self.repeat_ngons, self.repeat_align)):
            grid.addWidget(box, 4 + offset, 0, 1, 2)
        left.addWidget(tuning)

        buttons = QHBoxLayout()
        self.run_button = QPushButton("Retopologize")
        self.run_button.clicked.connect(self.run)
        buttons.addWidget(self.run_button, 1)
        # ⚠ There was NO CANCEL in the first build, and that is what turned a
        # long run into "the app hanged" — 52 minutes with no way out.
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.cancel)
        buttons.addWidget(self.cancel_button)
        left.addLayout(buttons)

        # Polls only while a run is in flight; see `run`.
        self._poll = QTimer(self)
        self._poll.setInterval(1000)
        self._poll.timeout.connect(self._check)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        left.addWidget(self.status)
        left.addStretch(1)

        outer.addWidget(self._build_report())

        for widget in (self.density,):
            widget.valueChanged.connect(self._on_density)
        for widget in (self.sharp_angle, self.isometry_bias, self.ngon_weight,
                       self.align_weight):
            widget.valueChanged.connect(self._save)
        self.clusters.valueChanged.connect(self._save)
        for box in (self.use_sharp, self.preprocess, self.smoothing,
                    self.align_sing, self.repeat_quads, self.repeat_ngons,
                    self.repeat_align, self.replace, self.preserve,
                    self.fix_concave):
            box.toggled.connect(self._save)
        for box in self.symmetry.values():
            box.toggled.connect(self._save)
        tuning.toggled.connect(self._save)
        # ⚠ the SYNC, not the handler — the handler saves, and a constructor
        # must not write the user's config (see `_sync_density`)
        self._sync_density()

    # ------------------------------------------------------------------

    def _build_report(self):
        """The right-hand panel: what the LAST run actually produced."""
        panel = QGroupBox("Last result")
        panel.setFixedWidth(236)
        lay = QVBoxLayout(panel)
        lay.setSpacing(4)

        self.rows = {}
        for key, label in (("faces_in", "Before"), ("faces", "After"),
                           ("quad_pct", "Quads"), ("verts", "Vertices"),
                           ("seconds", "Took")):
            row = QHBoxLayout()
            name = QLabel(label)
            name.setObjectName("dim")
            value = QLabel("—")
            row.addWidget(name)
            row.addStretch(1)
            row.addWidget(value)
            lay.addLayout(row)
            self.rows[key] = value

        self.report_note = QLabel("No run yet.")
        self.report_note.setObjectName("dim")
        self.report_note.setWordWrap(True)
        lay.addWidget(self.report_note)

        self.select_button = QPushButton("Select result")
        self.select_button.setEnabled(False)
        self.select_button.clicked.connect(self._select_result)
        lay.addWidget(self.select_button)

        # ⚠ What the LAST run carried, when it was asked to. Hidden otherwise,
        # so an untouched panel never implies a transfer happened.
        self.preserved_note = QLabel("")
        self.preserved_note.setObjectName("dim")
        self.preserved_note.setWordWrap(True)
        self.preserved_note.hide()
        lay.addWidget(self.preserved_note)

        # Still honest about the one channel that is genuinely not built.
        # Weights, shape keys and materials arrived with "Preserve rig data"
        # on 2026-08-21; UVs did not, and a panel that lists what it does
        # while staying quiet about what it does not is the thing this project
        # criticised QRemeshify for.
        pending = QLabel("UV maps are not carried over — a retopologised mesh "
                         "normally wants unwrapping again anyway.")
        pending.setObjectName("dim")
        pending.setWordWrap(True)
        lay.addWidget(pending)
        lay.addStretch(1)
        self._last = {}
        return panel

    def _sync_density(self):
        """Match the readout to the slider — and write NOTHING.

        ⚠ Split out of `_on_density` because the constructor calls it to get
        the initial readout right, and going through the handler meant
        **building the window SAVED the config**. Caught on 2026-08-17 when
        Quadify was restored: the fleet's config-fingerprint guard is newer
        than this code, so the two had never met — a whole `quadify` defaults
        group appeared in the real `app\\config.json` during a test run.
        Nothing was lost (the values written are the ones just restored), and
        that is exactly why it needed a guard rather than an eye.
        `optimizer.py` has the twin of this fix in `_sync_animation`.
        """
        self.density_out.setText("%.2f" % (self.density.value() / 100.0))

    def _on_density(self):
        self._sync_density()
        self._save()

    def _save(self):
        self.save_settings(
            density=self.density.value() / 100.0,
            sharp_angle=self.sharp_angle.value(),
            use_sharp=self.use_sharp.isChecked(),
            preprocess=self.preprocess.isChecked(),
            smoothing=self.smoothing.isChecked(),
            symmetry=self._symmetry_axes(),
            replace=self.replace.isChecked(),
            preserve=self.preserve.isChecked(),
            fix_concave=self.fix_concave.isChecked(),
            tuning=self.tuning.isChecked(),
            isometry_bias=self.isometry_bias.value(),
            ngon_regularity_weight=self.ngon_weight.value(),
            singularity_align_weight=self.align_weight.value(),
            align_singularities=self.align_sing.isChecked(),
            repeat_quads=self.repeat_quads.isChecked(),
            repeat_ngons=self.repeat_ngons.isChecked(),
            repeat_align=self.repeat_align.isChecked(),
            chart_cluster_size=self.clusters.value())

    def _symmetry_axes(self):
        return "".join(axis for axis, box in self.symmetry.items()
                       if box.isChecked())

    # ------------------------------------------------------------------

    def refresh(self):
        """Ask what is selected and whether the engine is there. Fired when the
        tool is shown, not on a timer — nothing here changes on its own."""
        reason = self.feature_reason()
        if reason:
            self.run_button.setEnabled(False)
            self.target.setText(reason)
            return
        try:
            # deep=True: the honest triangle count, not the datablock's.
            status = self.bridge.quad_status(poll=True, deep=True)
        except Exception:                       # noqa: BLE001 - dead bridge
            self.target.setText("Blender is not connected.")
            self.run_button.setEnabled(False)
            return
        self.apply_quad_status(status)

    def apply_quad_status(self, status):
        """Paint one `quad_status` reading. Split out so tests can drive it."""
        if not isinstance(status, dict):
            return
        self._quad = dict(status)
        # ⚠ BEFORE the early returns below, not after. Both of them leave the
        # panel on screen, and a "would carry 775 shape keys" line left over
        # from the last rig while nothing is selected is a number describing
        # an object that is not the target.
        self._sync_preserve(status)
        if not status.get("engine_ready"):
            missing = ", ".join(status.get("engine_missing") or ["engine"])
            self.target.setText("The retopology engine is missing (%s). "
                                "Reinstall the extension." % missing)
            self.run_button.setEnabled(False)
            return
        name = status.get("object") or ""
        if not name:
            self.target.setText("Select a mesh object in Blender.")
            self.run_button.setEnabled(False)
            return
        # ⚠ SHOW THE EVALUATED TRIANGLE COUNT — what the engine really gets.
        # The datablock count is only the same number when nothing modifies
        # the mesh, and the one time it differed it differed by 110×.
        tris = status.get("eval_tris")
        if tris is None:
            note = "%s — %s faces in the file" % (
                name, "{:,}".format(status.get("faces", 0)))
        else:
            note = "%s — %s triangles to remesh" % (name,
                                                    "{:,}".format(tris))
        self.target.setText(note)
        # A warning that names the consequence in time, because "large" means
        # nothing to someone deciding whether to press a button.
        if status.get("big"):
            self.size_warning.setText(
                "⚠ %s triangles is well past the %s the engine handles "
                "briskly — expect tens of minutes, not seconds. Decimate "
                "first, or turn off Detect under Sharp edges: on organic "
                "meshes it finds a feature line on nearly every edge, which "
                "is what makes a run like this crawl."
                % ("{:,}".format(tris), "{:,}".format(100000)))
            self.size_warning.show()
        else:
            self.size_warning.hide()
        self.run_button.setEnabled(True)

    def _sync_preserve(self, status):
        """Say what "Preserve rig data" would carry off THIS rig.

        ⚠ **Counted, never described.** "Preserves everything" tells nobody
        whether their 775 morphs are coming and how big the result will be;
        "775 shape keys" does. The three numbers are plain `len()` calls in
        `quad_status`, so the poll stays as cheap as it was.

        ⚠ It also names the one thing that is honestly missing. UVs are still
        not transferred, and a panel that lists what it does while staying
        quiet about what it does not is the thing this project criticised
        QRemeshify for.
        """
        if not status.get("object") or not status.get("engine_ready"):
            # Nothing selected, or no engine to run at all — either way this
            # line would be describing something that is not the target.
            self.preserve_note.setText("")
            return
        keys = int(status.get("shape_keys") or 0)
        groups = int(status.get("vertex_groups") or 0)
        deform = status.get("deform_modifiers") or []
        if not (keys or groups or deform):
            self.preserve_note.setText(
                "Nothing to carry — this mesh has no weights, shape keys or "
                "deform modifiers.")
            return
        bits = []
        if deform:
            bits.append("%d deform modifier%s" % (len(deform),
                                                  "" if len(deform) == 1
                                                  else "s"))
        if groups:
            bits.append("%s vertex group%s" % ("{:,}".format(groups),
                                               "" if groups == 1 else "s"))
        text = ("Would carry %s, plus materials, constraints and custom "
                "properties. Weights are resampled onto the new topology, so "
                "they are close but not exact. UVs are not carried."
                % ", ".join(bits))
        if keys:
            # ⚠ The Basis is not a morph; counting it reads as one too many.
            # And this is the sentence that has to be unmistakable: the morphs
            # do not come with the mesh, they come INSIDE it.
            morphs = max(0, keys - 1)
            text += (" ⚠ Its %s shape key%s will be BAKED IN at their current "
                     "values — set the frame you want first. The result will "
                     "have no shape keys of its own."
                     % ("{:,}".format(morphs), "" if morphs == 1 else "s"))
        self.preserve_note.setText(text)

    def pick_selected(self):
        """Take Blender's selection as the target, right now — the button.

        ⚠ **When the active object is not a mesh, this MAKES the selected mesh
        active rather than quietly aiming at it.** `quad_status` reads
        `active_object`, and its deep triangle count is measured on that one
        object, so pointing the run somewhere the count was never taken from
        would put a number on the label belonging to a different mesh. This
        module has been bitten by a lying count once already (2 424 shown,
        266 469 remeshed). `quad_select` already selects and activates, so the
        second reading is honestly about the object that will be remeshed.
        """
        reason = self.feature_reason()
        if reason:
            self.target.setText(reason)
            self.run_button.setEnabled(False)
            self._fail(reason)
            return
        try:
            status = self.bridge.quad_status(deep=True)
            if not (status or {}).get("object"):
                meshes = (status or {}).get("selected") or []
                if meshes:
                    self.bridge.quad_select(meshes[0])
                    status = self.bridge.quad_status(deep=True)
        except Exception as exc:                # noqa: BLE001 - dead bridge
            self.target.setText("Blender is not connected.")
            self.run_button.setEnabled(False)
            self._fail(exc)
            return
        self.apply_quad_status(status)
        name = (status or {}).get("object")
        if name:
            self._ok("Target is %s." % name)
        else:
            self._ok("Nothing is selected in Blender.")

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh()

    # ------------------------------------------------------------------

    def run(self):
        params = {
            "object": (self._quad or {}).get("object") or "",
            "density": self.density.value() / 100.0,
            "sharp_angle": self.sharp_angle.value(),
            "use_sharp": self.use_sharp.isChecked(),
            "preprocess": self.preprocess.isChecked(),
            "smoothing": self.smoothing.isChecked(),
            "symmetry": self._symmetry_axes(),
            "replace": self.replace.isChecked(),
            "preserve": self.preserve.isChecked(),
            "fix_concave": self.fix_concave.isChecked(),
        }
        if self.tuning.isChecked():
            params["settings"] = {
                "isometry_bias": self.isometry_bias.value(),
                "ngon_regularity_weight": self.ngon_weight.value(),
                "singularity_align_weight": self.align_weight.value(),
                "align_singularities": self.align_sing.isChecked(),
                "repeat_quads": self.repeat_quads.isChecked(),
                "repeat_ngons": self.repeat_ngons.isChecked(),
                "repeat_align": self.repeat_align.isChecked(),
                "chart_cluster_size": self.clusters.value(),
            }
        # ⚠ **NOT `_call`, AND THAT IS THE POINT.** `_call` wraps a run in
        # `begin_capture`, which greys the whole app and parks every other
        # tab's poll — correct for an optimizer run that owns Blender's main
        # thread, and WRONG here now that the engine runs on its own thread
        # inside Blender. Nothing is busy: Blender stays interactive, the app
        # stays usable, and this is an ordinary progress bar over a job.
        self._ok("Starting…")
        self.run_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        page = self.page()
        if page is not None:
            page.progress.start("Retopologising", "quad_progress")
        runner = optimizermod._Runner(
            lambda: self.bridge.quad_retopologize(params), parent=self)
        runner.done.connect(self._started)
        runner.failed.connect(self._start_failed)
        self._starter = runner
        runner.start()

    def _started(self, reply):
        """The start call came back — the job is now running in Blender."""
        if not isinstance(reply, dict) or not reply.get("ok"):
            self._start_failed((reply or {}).get("error")
                               or "could not start the retopology")
            return
        faces = reply.get("faces_in") or 0
        self._ok("Running on %s triangles. Blender stays usable — you can "
                 "carry on working." % "{:,}".format(faces))
        self._poll.start()

    def _start_failed(self, message):
        self._poll.stop()
        self.run_button.setEnabled(True)
        self.cancel_button.setEnabled(False)
        page = self.page()
        if page is not None:
            page.progress.stop()
        self._fail(message)

    def _check(self):
        """Watch for the run finishing. The bar polls the same command for its
        own stages; this one only cares whether it is still active."""
        try:
            report = self.bridge.quad_progress(poll=True)
        except Exception:                       # noqa: BLE001 - transient
            return
        if isinstance(report, dict) and report.get("active"):
            return
        self._poll.stop()
        self.run_button.setEnabled(True)
        self.cancel_button.setEnabled(False)
        page = self.page()
        if page is not None:
            page.progress.stop()
        try:
            self.show_result(self.bridge.quad_result())
        except Exception as exc:                # noqa: BLE001
            self._fail(exc)

    def cancel(self):
        try:
            self.bridge.quad_cancel()
            self._ok("Cancelling…")
        except Exception as exc:                # noqa: BLE001
            self._fail(exc)

    def show_result(self, reply):
        """Paint the report from the reply, and only from the reply."""
        if not isinstance(reply, dict):
            return
        if not reply.get("ok"):
            self._fail(reply.get("error") or "the retopology failed")
            self.report_note.setText("The last run did not finish.")
            return
        self._last = dict(reply)
        for key, widget in self.rows.items():
            value = reply.get(key)
            if key == "quad_pct":
                widget.setText("%.1f%%" % float(value or 0.0))
            elif key == "seconds":
                widget.setText("%.1f s" % float(value or 0.0))
            elif value is None:
                widget.setText("—")
            else:
                widget.setText("{:,}".format(int(value)))
        # Say the imperfect parts out loud: a mesh that is not 100 % quads is
        # the single thing someone judging a retopo needs told.
        bits = []
        if reply.get("tris"):
            bits.append("%d triangles" % reply["tris"])
        if reply.get("ngons"):
            bits.append("%d n-gons" % reply["ngons"])
        if not reply.get("smoothed") and self.smoothing.isChecked():
            bits.append("smoothing did not run")
        # ⚠ Say it. The panel promises quads; when the concave clean-up has
        # traded a few for triangles, the two numbers must not disagree in
        # silence.
        if reply.get("concave_split"):
            bits.append("%d concave faces split" % reply["concave_split"])
        self.report_note.setText("; ".join(bits) if bits
                                 else "All quads. Nothing to flag.")
        self._show_preserved(reply)
        self.rows["quad_pct"].setStyleSheet(
            "" if float(reply.get("quad_pct") or 0) >= 100.0
            else "color: %s;" % theme.TEXT)
        self.select_button.setEnabled(bool(reply.get("object")))
        self._ok("Made %s." % reply.get("object", "a new object"))

    def _show_preserved(self, reply):
        """What the run really carried — read off the reply, like every other
        number in this panel.

        ⚠ **A preserve that failed must not read as one that was not asked
        for.** The transfer runs after the retopology and is caught separately
        in the add-on, precisely so a bad transfer does not cost a mesh that
        took minutes — which means "no rig data" and "the rig data broke" are
        different outcomes and have to look different.
        """
        if not reply.get("preserve"):
            self.preserved_note.hide()
            return
        report = reply.get("preserved") or {}
        self.preserved_note.show()
        if not report.get("ok"):
            self.preserved_note.setStyleSheet("color: #e06c60;")
            self.preserved_note.setText(
                "Rig data was NOT carried: %s"
                % (report.get("error") or "; ".join(report.get("notes") or [])
                   or "unknown reason"))
            return
        self.preserved_note.setStyleSheet("")
        bits = []
        if report.get("groups"):
            bits.append("%d groups" % report["groups"])
        if report.get("baked_keys"):
            bits.append("%d shape keys baked in" % report["baked_keys"])
        if report.get("drivers"):
            bits.append("%d drivers" % report["drivers"])
        if report.get("modifiers"):
            bits.append("%d modifiers" % len(report["modifiers"]))
        text = "Carried: %s." % ", ".join(bits) if bits \
            else "Nothing needed carrying."
        for note in (report.get("notes") or []):
            text += " ⚠ %s." % note
        if report.get("skipped"):
            text += " Left behind (already baked into the mesh): %s." \
                % ", ".join(report["skipped"])
        self.preserved_note.setText(text)

    def _select_result(self):
        name = (self._last or {}).get("object")
        if not name:
            return
        try:
            self.bridge.quad_select(name)
        except Exception as exc:                # noqa: BLE001
            self._fail(exc)

    def apply_status(self, status):
        """Fed by the tab's poll owner. Quadify needs nothing from an optimizer
        status, but the hook has to exist — every tool in the tab is wired to
        it, and a missing method would break the fan-out for the others."""
        self._status = status or {}
