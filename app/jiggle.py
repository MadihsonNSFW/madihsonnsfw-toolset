"""Physics tab — Bone Jiggle.

Drives `jiggle.py` in the Blender add-on over the bridge: spring-driven
secondary motion on bones, with every tunable the solver has reachable from
here.

Three shape decisions worth knowing before editing this file:

1. **Only the fields you actually touched are sent.** The engine writes just
   the keys present in a request, and this panel tracks which widgets the user
   moved since the last load. That is what makes editing a MIXED selection
   safe: drag Stiffness with twelve bones selected and only Stiffness changes
   on those twelve — everything else keeps whatever each bone had, instead of
   being flattened to whatever the form happened to be showing.

2. **The per-point settings live in Tip/Root sub-tabs.** Every dynamics,
   collision and wind setting exists twice in the solver, once per simulated
   end. Laying both out side by side is a wall of forty controls; the sub-tab
   shows one end at a time and reaches all of them.

3. **No repeating poll.** Refresh is a button, exactly like the Proxy Cage
   next door. A timer that calls the bridge from the GUI thread is the freeze
   this app has already had to fix once (docs\\app-shell.md).
"""

import math
import threading
import traceback

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QFormLayout,
                               QGroupBox, QHBoxLayout, QHeaderView, QLabel,
                               QLineEdit, QMessageBox, QPushButton,
                               QTableWidget, QTableWidgetItem, QTabWidget,
                               QVBoxLayout, QWidget)

import bridge as bridgemod
import dev_console
from widgets import NoScrollComboBox, ValueSlider

FEATURE = "bone_jiggle"
BAKE_FEATURE = "bone_jiggle_bake"

# How long the panel waits after the last edit before sending it. A drag over
# a ValueSlider emits on every step, so an un-debounced push would fire a
# bridge command per pixel.
PUSH_DELAY_MS = 250

NONE_LABEL = "— none —"


class _Worker(QObject):
    """One blocking bridge call on a daemon thread.

    A local copy of main.BridgeWorker for the same reason physics.py has one:
    importing it from main.py would be circular. A bake steps the whole frame
    range twice, so it cannot run on the GUI thread."""

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
            dev_console.BUFFER.add("ERROR", "Jiggle bridge call failed: %s" % exc)
            self.failed.emit(str(exc))
        except Exception as exc:      # noqa: BLE001
            dev_console.BUFFER.add(
                "CRIT", "Unexpected error in a jiggle worker:\n%s"
                % traceback.format_exc())
            self.failed.emit(str(exc))
        else:
            self.done.emit(r)


class BoneJiggleTool(QWidget):
    """Spring-driven secondary motion on bones, tuned from the app."""

    def __init__(self, bridge, window, parent=None):
        super().__init__(parent)
        self.bridge = bridge
        self.window = window
        self._worker = None
        self._loading = False          # suppress pushes while filling the form
        self._dirty = set()            # (group, key) the user actually moved
        self._fields = {}              # (group, key) -> widget
        self._labels = {}              # (group, key) -> its form label
        self._rows = []                # the bone table's backing data
        self._gate_reason_shown = False

        self._push_timer = QTimer(self)
        self._push_timer.setSingleShot(True)
        self._push_timer.setInterval(PUSH_DELAY_MS)
        self._push_timer.timeout.connect(self._push)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(9)

        blurb = QLabel(
            "Select bones in Blender's Pose Mode and switch jiggle on. Each "
            "bone gets a simulated point that is pulled toward the pose the "
            "animation asks for, so it lags, overshoots and settles on its own "
            "— hair, tails, ears, chains, cloth trim. The bone's rotation is "
            "derived from where the point ends up, which is what keeps long "
            "chains stable. Press Refresh after changing the selection.")
        blurb.setWordWrap(True)
        blurb.setObjectName("dim")
        lay.addWidget(blurb)

        lay.addWidget(self._build_target())
        lay.addWidget(self._build_table())
        lay.addWidget(self._build_points())
        lay.addWidget(self._build_bone())
        lay.addWidget(self._build_armature())
        lay.addWidget(self._build_solver())
        lay.addWidget(self._build_bake())

        self.status = QLabel("Press Refresh to read the rig.")
        self.status.setWordWrap(True)
        self.status.setObjectName("dim")
        lay.addWidget(self.status)
        lay.addStretch(1)

        self._sync_enabled()

    # ------------------------------------------------------------- builders

    def _slider(self, group, key, lo, hi, val, decimals=0, suffix="", tip=""):
        w = ValueSlider(lo, hi, val, decimals=decimals, suffix=suffix,
                        tooltip=tip)
        w.valueChanged.connect(lambda _v, g=group, k=key: self._touch(g, k))
        self._fields[(group, key)] = w
        return w

    def _check(self, group, key, text, val=False, tip=""):
        w = QCheckBox(text)
        w.setChecked(bool(val))
        if tip:
            w.setToolTip(tip)
        w.toggled.connect(lambda _v, g=group, k=key: self._touch(g, k))
        self._fields[(group, key)] = w
        return w

    def _combo(self, group, key, items, tip=""):
        w = NoScrollComboBox()
        for entry in items:
            if isinstance(entry, tuple):
                w.addItem(entry[1], entry[0])
            else:
                w.addItem(entry, entry)
        if tip:
            w.setToolTip(tip)
        w.currentIndexChanged.connect(lambda _v, g=group, k=key:
                                      self._touch(g, k))
        self._fields[(group, key)] = w
        return w

    def _line(self, group, key, placeholder="", tip=""):
        w = QLineEdit()
        w.setPlaceholderText(placeholder)
        if tip:
            w.setToolTip(tip)
        w.textEdited.connect(lambda _v, g=group, k=key: self._touch(g, k))
        self._fields[(group, key)] = w
        return w

    def _row(self, form, group, key, label, widget):
        form.addRow(label, widget)
        self._labels[(group, key)] = form.labelForField(widget)
        return widget

    # ---------------------------------------------------------------- target

    def _build_target(self):
        box = QGroupBox("Target")
        form = QFormLayout(box)

        self.armature = NoScrollComboBox()
        self.armature.setToolTip("The rig to work on")
        self.armature.currentIndexChanged.connect(self._on_armature_changed)
        form.addRow("Armature", self.armature)

        row = QHBoxLayout()
        self.sel_label = QLabel("—")
        self.sel_label.setWordWrap(True)
        self.sel_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        row.addWidget(self.sel_label, 1)
        self.btn_refresh = QPushButton("Refresh")
        self.btn_refresh.setToolTip(
            "Re-read the rig, the bone selection and every setting from Blender")
        self.btn_refresh.clicked.connect(self.refresh)
        row.addWidget(self.btn_refresh)
        host = QWidget()
        host.setLayout(row)
        form.addRow("Selected", host)

        actions = QHBoxLayout()
        self.btn_on_tip = QPushButton("Jiggle On")
        self.btn_on_tip.setToolTip(
            "Switch the simulation on for the selected bones' tips — the usual "
            "case, and the only one a connected bone can use")
        self.btn_on_tip.clicked.connect(lambda: self._set_enabled(tip=True))
        self.btn_on_root = QPushButton("+ Root")
        self.btn_on_root.setToolTip(
            "Also simulate the ROOT of the selected bones. Only possible on a "
            "bone that is not connected to its parent — a connected bone's "
            "root is its parent's tip and cannot move independently")
        self.btn_on_root.clicked.connect(lambda: self._set_enabled(root=True))
        self.btn_off = QPushButton("Jiggle Off")
        self.btn_off.setToolTip("Switch the simulation off for the selection")
        self.btn_off.clicked.connect(
            lambda: self._set_enabled(tip=False, root=False))
        self.btn_select = QPushButton("Select Jiggle Bones")
        self.btn_select.setToolTip(
            "Select every bone on this rig that has jiggle switched on")
        self.btn_select.clicked.connect(self._select_bones)
        self.btn_copy = QPushButton("Copy Active to Selected")
        self.btn_copy.setToolTip(
            "Copy every setting from the ACTIVE bone onto the rest of the "
            "selection")
        self.btn_copy.clicked.connect(self._copy_settings)
        for b in (self.btn_on_tip, self.btn_on_root, self.btn_off):
            actions.addWidget(b)
        actions.addStretch(1)
        form.addRow("", self._wrap(actions))

        more = QHBoxLayout()
        more.addWidget(self.btn_select)
        more.addWidget(self.btn_copy)
        more.addStretch(1)
        form.addRow("", self._wrap(more))
        return box

    def _wrap(self, layout):
        host = QWidget()
        host.setLayout(layout)
        layout.setContentsMargins(0, 0, 0, 0)
        return host

    # ----------------------------------------------------------------- table

    def _build_table(self):
        box = QGroupBox("Jiggle bones")
        outer = QVBoxLayout(box)
        hint = QLabel(
            "Every bone on this rig with jiggle switched on. Select rows here "
            "to edit exactly those bones; with nothing selected the settings "
            "below apply to whatever is selected in Blender.")
        hint.setWordWrap(True)
        hint.setObjectName("dim")
        outer.addWidget(hint)

        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(
            ["Bone", "Tip", "Root", "Stiff", "Damp", "Blend", "Collide"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.setMinimumHeight(120)
        head = self.table.horizontalHeader()
        head.setSectionResizeMode(0, QHeaderView.Stretch)
        for i in range(1, 7):
            head.setSectionResizeMode(i, QHeaderView.ResizeToContents)
        self.table.itemSelectionChanged.connect(self._on_table_selection)
        outer.addWidget(self.table)
        return box

    # ---------------------------------------------------------------- points

    def _build_points(self):
        box = QGroupBox("Point settings")
        outer = QVBoxLayout(box)
        hint = QLabel(
            "Every dynamics, collision and wind setting exists once for the "
            "bone's TIP and once for its ROOT. The Root tab only does anything "
            "on a bone whose root is simulated.")
        hint.setWordWrap(True)
        hint.setObjectName("dim")
        outer.addWidget(hint)

        self.points = QTabWidget()
        self.points.addTab(self._point_page("tip"), "Tip")
        self.points.addTab(self._point_page("root"), "Root")
        outer.addWidget(self.points)
        return box

    def _point_page(self, group):
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(6, 6, 6, 6)

        dyn = QGroupBox("Dynamics")
        f = QFormLayout(dyn)
        self._row(f, group, "mute", "Mute", self._check(
            group, "mute", "Pause this end",
            tip="Stop simulating this end without losing its settings — the "
                "bone snaps back to the animated pose"))
        self._row(f, group, "stiffness", "Stiffness", self._slider(
            group, "stiffness", 0.0, 200.0, 20.0, 1,
            tip="How hard the point is pulled back to the pose the animation "
                "asks for. Low is floppy, high follows tightly"))
        # One hint label, owned by the Tip tab. Building it in both tabs would
        # leave `self.ceiling_hint` pointing at the Root copy while only the
        # Tip one is in a layout, so the text would go somewhere invisible.
        if group == "tip":
            self.ceiling_hint = QLabel("")
            self.ceiling_hint.setObjectName("dim")
            self.ceiling_hint.setWordWrap(True)
            f.addRow("", self.ceiling_hint)
        self._row(f, group, "damping", "Damping", self._slider(
            group, "damping", 0.0, 20.0, 1.0, 2,
            tip="How quickly the motion bleeds away. Low values ring for a "
                "long time; high values look heavy and dead"))
        self._row(f, group, "slack", "Slack", self._slider(
            group, "slack", 0.0, 1.0, 0.0, 2,
            tip="How freely the bone may change length. 0 holds its length "
                "exactly; raise it for something rubbery"))
        self._row(f, group, "mass", "Mass", self._slider(
            group, "mass", 0.01, 10.0, 1.0, 2,
            tip="Relative weight. It sets how much wind moves this point and "
                "how a correction is shared with the parent — it is NOT "
                "inertia, and making every bone heavier changes nothing"))
        self._row(f, group, "gravity", "Gravity", self._slider(
            group, "gravity", -2.0, 2.0, 1.0, 2,
            tip="Multiplier on the scene's own gravity. 0 ignores gravity, "
                "negative floats upward"))
        self._row(f, group, "taper_stiffness", "Taper stiffness", self._check(
            group, "taper_stiffness", "Use the Root/Tip taper",
            tip="Scale Stiffness along the chain using the solver's Root/Tip "
                "taper instead of one flat value — stiff at the base, loose at "
                "the end"))
        self._row(f, group, "taper_damping", "Taper damping", self._check(
            group, "taper_damping", "Use the Root/Tip taper",
            tip="The same, for Damping"))
        lay.addWidget(dyn)

        col = QGroupBox("Collision")
        f = QFormLayout(col)
        self._row(f, group, "collider_mode", "Collide with", self._combo(
            group, "collider_mode",
            [("NONE", "None"), ("OBJECT", "Object  (mesh surface)"),
             ("COLLECTION", "Collection  (every mesh in it)"),
             ("SPHERE", "Sphere  (analytic)"), ("BOX", "Box  (analytic)"),
             ("CYLINDER", "Cylinder  (analytic)"),
             ("CAPSULE", "Capsule  (analytic)")],
            tip="What this point collides against.\n\n"
                "Object/Collection test the real evaluated mesh, so a deforming "
                "body works — but they cost the most.\n\n"
                "The analytic shapes use only an object's transform and scale, "
                "so an Empty is a perfectly good collider and they are far "
                "cheaper. Reach for those first."))
        self._row(f, group, "collider_object", "Collider", self._combo(
            group, "collider_object", [NONE_LABEL],
            tip="The collision target. For the analytic shapes only its "
                "transform and scale matter"))
        self._row(f, group, "collider_collection", "Collection", self._combo(
            group, "collider_collection", [NONE_LABEL],
            tip="Every mesh in this collection is collided against"))
        self._row(f, group, "radius", "Radius", self._slider(
            group, "radius", 0.0, 1.0, 0.05, 3, " m",
            tip="Collision radius of the simulated point itself. This is also "
                "the capsule radius used by self collision"))
        self._row(f, group, "friction", "Friction", self._slider(
            group, "friction", 0.0, 1.0, 0.5, 2,
            tip="1 holds the point exactly where it last touched, 0 lets it "
                "slide freely. The contact is remembered in the collider's own "
                "space, so a moving collider drags the point along with it"))
        self._row(f, group, "bounce", "Bounce", self._slider(
            group, "bounce", 0.0, 1.0, 0.5, 2,
            tip="How much speed survives an impact, along the surface normal"))
        self._row(f, group, "adhesion", "Adhesion", self._slider(
            group, "adhesion", 0.0, 1.0, 0.0, 3, " m",
            tip="Extra distance over which an EXISTING contact is kept alive, "
                "so the point clings and peels away instead of releasing the "
                "instant it clears the surface"))
        lay.addWidget(col)

        wind = QGroupBox("Wind")
        f = QFormLayout(wind)
        self._row(f, group, "wind_object", "Force field", self._combo(
            group, "wind_object", [NONE_LABEL],
            tip="A Blender force field — Wind, Turbulence or Vortex. Its "
                "strength, size and falloff are read live from the field's own "
                "settings, so you tune it in Blender's physics panel rather "
                "than duplicating the controls here"))
        self._row(f, group, "wind", "Strength", self._slider(
            group, "wind", 0.0, 10.0, 1.0, 2,
            tip="Multiplier on the force field's own strength"))
        lay.addWidget(wind)
        return page

    # ------------------------------------------------------------------ bone

    def _build_bone(self):
        box = QGroupBox("Bone")
        f = QFormLayout(box)
        self._row(f, "bone", "blend", "Blend", self._slider(
            "bone", "blend", 0.0, 1.0, 1.0, 2,
            tip="0 is the animation untouched, 1 is full simulation. Key this "
                "to fade the jiggle in and out of a shot"))
        self._row(f, "bone", "chain", "Chain", self._check(
            "bone", "chain", "Push corrections into the parent", True,
            tip="Let this bone's corrections travel back up the chain, so the "
                "whole chain reacts instead of only the last bone"))
        self._row(f, "bone", "cone_limit", "Cone limit", self._slider(
            "bone", "cone_limit", 0.0, 180.0, 180.0, 1, "°",
            tip="How far the bone may swing away from the direction the "
                "animation gives it, in any direction. 180 is unlimited"))
        self._row(f, "bone", "use_axis_limits", "Per-axis limits", self._check(
            "bone", "use_axis_limits", "Limit the two swing planes separately",
            tip="Use separate X and Z limits instead of one cone — for things "
                "that should only move one way, like a fin or an eyelid"))
        self._row(f, "bone", "limit_x", "Limit X", self._slider(
            "bone", "limit_x", 0.0, 180.0, 90.0, 1, "°",
            tip="Swing limit around the bone's local X"))
        self._row(f, "bone", "limit_z", "Limit Z", self._slider(
            "bone", "limit_z", 0.0, 180.0, 90.0, 1, "°",
            tip="Swing limit around the bone's local Z"))
        self._row(f, "bone", "max_drift", "Max drift", self._slider(
            "bone", "max_drift", 0.0, 1.0, 0.0, 3, " m",
            tip="How far a floating bone's ROOT may leave its animated "
                "position. 0 is unlimited. Only applies when the root is "
                "simulated"))
        self._row(f, "bone", "lateral", "Lateral links", self._check(
            "bone", "lateral", "Link sideways to neighbouring chains",
            tip="Let this bone hold its spacing to bones at the same depth in "
                "neighbouring chains, so a skirt or a hair bunch moves as one "
                "sheet instead of passing through itself. Switch the feature "
                "on in Solver as well"))
        return box

    # -------------------------------------------------------------- armature

    def _build_armature(self):
        box = QGroupBox("Armature")
        f = QFormLayout(box)
        self._row(f, "object", "mute", "Mute", self._check(
            "object", "mute", "Stop simulating this rig",
            tip="Switch the whole rig's simulation off without touching any "
                "bone's settings"))
        self._row(f, "object", "freeze", "Freeze", self._check(
            "object", "freeze", "Hold the current keys",
            tip="Set automatically after a bake so the live solver stops "
                "overwriting the keyframes it just wrote. Clear it to go back "
                "to live simulation"))
        self._row(f, "object", "self_collide", "Self collision", self._check(
            "object", "self_collide", "Stop simulated bones passing through "
                                      "each other",
            tip="Treats each simulated bone as a capsule of its point Radius. "
                "Costs real time on a dense rig — switch it on only where you "
                "can see the interpenetration"))
        self._row(f, "object", "self_margin", "Self margin", self._slider(
            "object", "self_margin", 0.0, 0.1, 0.0, 3, " m",
            tip="Extra clearance held between two bones on top of their radii"))
        return box

    # ---------------------------------------------------------------- solver

    def _build_solver(self):
        box = QGroupBox("Solver  (whole scene)")
        f = QFormLayout(box)
        self._row(f, "scene", "enabled", "Enable jiggle", self._check(
            "scene", "enabled", "Simulate on playback", True,
            tip="The master switch for the whole solver"))
        self._row(f, "scene", "quality", "Quality", self._slider(
            "scene", "quality", 1, 32, 2,
            tip="Relaxation passes per step. Higher holds chains and lengths "
                "together better and costs linearly more"))
        self._row(f, "scene", "substeps", "Substeps", self._slider(
            "scene", "substeps", 1, 16, 1,
            tip="Simulation steps per frame. Raise this for stiff or fast "
                "setups — it is also what lets Stiffness go above the ceiling, "
                "and it makes playback match a bake"))
        self._row(f, "scene", "preroll", "Preroll", self._slider(
            "scene", "preroll", 0, 200, 0,
            tip="Settle the simulation for this many steps before the first "
                "frame, so a shot does not open with the rig springing out of "
                "a dead stiff pose"))
        self._row(f, "scene", "loop", "Loop physics", self._check(
            "scene", "loop", "Carry the sim across a timeline wrap",
            tip="Keep the motion running when playback wraps from the last "
                "frame to the first, instead of resetting. A manual rewind "
                "still resets"))
        self._row(f, "scene", "simulate_in_render", "Simulate in render",
                  self._check(
                      "scene", "simulate_in_render", "Keep simulating while "
                                                     "rendering", True,
                      tip="Leave this on and a render simulates like playback "
                          "does. Turn it off and a render shows only what has "
                          "been baked"))
        self._row(f, "scene", "taper_root", "Taper root", self._slider(
            "scene", "taper_root", 0.0, 4.0, 1.0, 2,
            tip="Stiffness/Damping multiplier at the BASE of a chain, for "
                "points with Taper switched on"))
        self._row(f, "scene", "taper_tip", "Taper tip", self._slider(
            "scene", "taper_tip", 0.0, 4.0, 1.0, 2,
            tip="Stiffness/Damping multiplier at the END of a chain, for "
                "points with Taper switched on"))

        self._row(f, "scene", "guard", "Safety guard", self._check(
            "scene", "guard", "Damp automatically on a teleport or hard spin",
            True,
            tip="Adds damping when the rig moves or turns violently between "
                "frames, so a cut or a teleport does not blow the jiggle up"))
        self._row(f, "scene", "guard_move", "Guard move", self._slider(
            "scene", "guard_move", 0.0, 50.0, 2.0, 2,
            tip="World units the rig may move per frame before the guard "
                "starts adding damping"))
        self._row(f, "scene", "guard_spin", "Guard spin", self._slider(
            "scene", "guard_spin", 0.0, 180.0, 85.0, 1, "°",
            tip="How far the rig may turn per frame before the guard starts "
                "adding damping"))
        self._row(f, "scene", "guard_strength", "Guard strength", self._slider(
            "scene", "guard_strength", 0.0, 50.0, 8.0, 2,
            tip="How much damping the guard adds once it triggers"))

        self._row(f, "scene", "lateral", "Lateral links", self._check(
            "scene", "lateral", "Solve side-to-side links",
            tip="Solve the links between neighbouring chains. Bones opt in "
                "individually with the Lateral links checkbox above"))
        self._row(f, "scene", "lateral_stiffness", "Link stiffness",
                  self._slider(
                      "scene", "lateral_stiffness", 0.0, 1.0, 0.5, 2,
                      tip="How hard linked bones hold their spacing"))
        self._row(f, "scene", "lateral_tolerance", "Link tolerance",
                  self._slider(
                      "scene", "lateral_tolerance", 0.0, 1.0, 0.1, 2,
                      tip="Fraction of the rest spacing a link may change "
                          "before it pulls back. Slack, so a sheet can still "
                          "fold instead of going rigid"))
        self._row(f, "scene", "lateral_reach", "Link reach", self._slider(
            "scene", "lateral_reach", 1.0, 8.0, 2.5, 2,
            tip="How far a link may reach, as a MULTIPLE of the average "
                "spacing at that depth — relative, so the same setup behaves "
                "the same at any scale"))

        self._row(f, "scene", "cache", "Use cache", self._check(
            "scene", "cache", "Write simulated frames to disk",
            tip="Scrubbing backwards replays from disk instead of restarting "
                "the simulation. Cached frames record the settings that made "
                "them and are ignored once those change, so a stale cache can "
                "never quietly serve you the wrong motion"))
        self._row(f, "scene", "cache_dir", "Cache folder", self._line(
            "scene", "cache_dir", "//madi_jiggle_cache",
            tip="Where cached frames are written. // means beside the .blend"))

        row = QHBoxLayout()
        self.btn_reset = QPushButton("Reset Simulation")
        self.btn_reset.setToolTip(
            "Snap every rig back onto its animation and start the simulation "
            "again from scratch")
        self.btn_reset.clicked.connect(self._reset)
        self.btn_cache = QPushButton("Build Cache")
        self.btn_cache.setToolTip(
            "Step the whole frame range once and write every frame to disk")
        self.btn_cache.clicked.connect(self._build_cache)
        self.btn_cache_clear = QPushButton("Clear Cache")
        self.btn_cache_clear.clicked.connect(self._clear_cache)
        for b in (self.btn_reset, self.btn_cache, self.btn_cache_clear):
            row.addWidget(b)
        row.addStretch(1)
        f.addRow("", self._wrap(row))
        return box

    # ------------------------------------------------------------------ bake

    def _build_bake(self):
        box = QGroupBox("Bake")
        f = QFormLayout(box)
        hint = QLabel(
            "Bakes the simulation into keyframes on the armature and then "
            "freezes it, so the live solver stops fighting the keys it just "
            "wrote. Clear Freeze above to go back to simulating.")
        hint.setWordWrap(True)
        hint.setObjectName("dim")
        f.addRow(hint)

        row = QHBoxLayout()
        self.bake_start = ValueSlider(0, 100000, 1, decimals=0,
                                      tooltip="First frame to bake")
        self.bake_end = ValueSlider(0, 100000, 250, decimals=0,
                                    tooltip="Last frame to bake")
        row.addWidget(self.bake_start)
        row.addWidget(self.bake_end)
        f.addRow("Frame range", self._wrap(row))

        self.bake_preroll = ValueSlider(
            0, 200, 0, decimals=0,
            tooltip="Steps to settle before the first frame is recorded")
        f.addRow("Preroll", self.bake_preroll)

        self.bake_selected = QCheckBox("Selected bones only")
        self.bake_selected.setToolTip(
            "Bake only the selected bones instead of every jiggling bone")
        f.addRow("", self.bake_selected)

        self.bake_action = QLineEdit()
        self.bake_action.setPlaceholderText("(MADI_Jiggle_<armature>)")
        self.bake_action.setToolTip("Name for the action the keys go into")
        f.addRow("Action", self.bake_action)

        self.bake_overwrite = QCheckBox("Overwrite an existing action")
        self.bake_overwrite.setToolTip(
            "Replace an action of that name instead of adding keys to it")
        f.addRow("", self.bake_overwrite)

        self.btn_bake = QPushButton("Bake to Keyframes")
        self.btn_bake.clicked.connect(self._bake)
        f.addRow("", self.btn_bake)
        return box

    # ------------------------------------------------------------- gate/busy

    def _gate(self, feature=FEATURE):
        """None if the add-on supports the feature, else why it doesn't."""
        try:
            return self.bridge.feature_reason(feature)
        except Exception:      # noqa: BLE001 - never let the gate break the UI
            return None

    def set_capture_busy(self, busy):
        self._busy(busy)

    def _busy(self, busy):
        self.btn_refresh.setEnabled(not busy)
        self._sync_enabled(busy=busy)

    def _sync_enabled(self, *_args, busy=False):
        reason = self._gate()
        blocked = busy or reason is not None
        for w in self._fields.values():
            w.setEnabled(not blocked)
        for lab in self._labels.values():
            if lab is not None:
                lab.setEnabled(not blocked)
        for b in (self.btn_on_tip, self.btn_on_root, self.btn_off,
                  self.btn_select, self.btn_copy, self.btn_reset,
                  self.btn_cache, self.btn_cache_clear):
            b.setEnabled(not blocked)
        self.table.setEnabled(not blocked)
        self.points.setEnabled(not blocked)

        # Baking has its own gate, so an add-on that can simulate but not bake
        # loses the Bake button only.
        bake_reason = self._gate(BAKE_FEATURE) or reason
        self.btn_bake.setEnabled(not busy and bake_reason is None)
        self.btn_bake.setToolTip(bake_reason or "")

        if reason is not None:
            self.status.setStyleSheet("")
            self.status.setText(reason)
        elif self._gate_reason_shown:
            self.status.setStyleSheet("")
            self.status.setText("Add-on updated — Bone Jiggle is ready.")
        self._gate_reason_shown = reason is not None

    def _fail(self, message):
        self._busy(False)
        self.status.setStyleSheet("color: #e06c60;")
        self.status.setText(str(message))
        if self.window is not None:
            self.window.update_bridge_status()

    def _ok(self, text):
        self.status.setStyleSheet("")
        self.status.setText(text)

    def _note(self, text):
        self.status.setStyleSheet("color: #d6a04a;")
        self.status.setText(text)

    # --------------------------------------------------------------- refresh

    def refresh(self):
        if self._gate() is not None:
            self._sync_enabled()
            return
        try:
            st = self.bridge.jiggle_status() or {}
        except bridgemod.BridgeError as exc:
            self._fail(exc)
            return
        self._apply_status(st)
        try:
            listing = self.bridge.jiggle_list(
                armature=self._armature_name()) or {}
        except bridgemod.BridgeError:
            listing = {"bones": []}
        self._fill_table(listing.get("bones") or [])
        self._load_bone_settings()

    def _apply_status(self, st):
        self._loading = True
        try:
            names = st.get("armatures") or []
            want = st.get("armature") or self._armature_name()
            self.armature.blockSignals(True)
            self.armature.clear()
            self.armature.addItems(names)
            if want and want in names:
                self.armature.setCurrentIndex(names.index(want))
            self.armature.blockSignals(False)

            objects = [NONE_LABEL] + list(st.get("objects") or [])
            collections = [NONE_LABEL] + list(st.get("collections") or [])
            fields = [NONE_LABEL] + list(st.get("fields") or [])
            for group in ("tip", "root"):
                self._refill(group, "collider_object", objects)
                self._refill(group, "collider_collection", collections)
                self._refill(group, "wind_object", fields)

            self._set_group("scene", st.get("scene") or {})
            self._set_group("object", st.get("object") or {})

            sel = st.get("selected") or []
            active = st.get("active")
            total = st.get("enabled_bones") or 0
            if sel:
                shown = ", ".join(sel[:6]) + (" …" if len(sel) > 6 else "")
                text = "%d selected (%s)" % (len(sel), shown)
            else:
                text = "nothing selected in Blender"
            if active:
                text += "  ·  active: %s" % active
            text += "  ·  %d bone%s jiggling" % (total, "" if total == 1 else "s")
            self.sel_label.setText(text)

            start = st.get("frame_start")
            end = st.get("frame_end")
            if start is not None:
                self.bake_start.setValue(start)
            if end is not None:
                self.bake_end.setValue(end)
            self._show_ceiling(st.get("stiffness_ceiling"))
        finally:
            self._loading = False

    def _show_ceiling(self, ceiling):
        if not ceiling:
            self.ceiling_hint.setText("")
            return
        self.ceiling_hint.setText(
            "Stiffness above %.0f is clamped at this frame rate and substep "
            "count — raise Substeps to use more." % ceiling)

    def _refill(self, group, key, items):
        w = self._fields[(group, key)]
        want = w.currentData()
        w.blockSignals(True)
        w.clear()
        for name in items:
            w.addItem(name, None if name == NONE_LABEL else name)
        if want:
            i = w.findData(want)
            if i >= 0:
                w.setCurrentIndex(i)
        w.blockSignals(False)

    def _fill_table(self, rows):
        self._rows = rows
        self.table.blockSignals(True)
        self.table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            def cell(col, text):
                item = QTableWidgetItem(str(text))
                self.table.setItem(r, col, item)
            cell(0, row.get("name", ""))
            cell(1, "on" if row.get("tip") else "")
            cell(2, "on" if row.get("root") else "")
            cell(3, "%.1f" % float(row.get("stiffness") or 0.0))
            cell(4, "%.2f" % float(row.get("damping") or 0.0))
            cell(5, "%.2f" % float(row.get("blend") or 0.0))
            mode = row.get("collider") or "NONE"
            cell(6, "" if mode == "NONE" else mode.title())
        self.table.blockSignals(False)

    def _load_bone_settings(self, announce=False):
        """Fill the per-bone form from whatever bones we are editing.

        `announce` is off by default because this runs at the end of every
        refresh, and refresh runs at the end of every action — so announcing
        here would wipe out "Baked 40 frames…" a fraction of a second after it
        appeared. Only a selection change the user made is worth saying."""
        bones = self._target_bones()
        try:
            r = self.bridge.jiggle_get(armature=self._armature_name(),
                                       bones=bones) or {}
        except bridgemod.BridgeError:
            # No selection is a normal state, not an error worth shouting about.
            return
        common = r.get("common") or {}
        multiple = int(r.get("count") or 0) > 1
        self._loading = True
        try:
            self._set_group("bone", common)
            self._set_group("tip", common.get("tip") or {})
            self._set_group("root", common.get("root") or {})
            self._mark_mixed("bone", common, multiple)
            self._mark_mixed("tip", common.get("tip") or {}, multiple)
            self._mark_mixed("root", common.get("root") or {}, multiple)
        finally:
            self._loading = False
        self._dirty.clear()
        if announce:
            self._ok("Editing %d bone(s): %s"
                     % (r.get("count", 0),
                        ", ".join((r.get("names") or [])[:6])))

    _MIXED_NOTE = ("\n\nThese bones differ on this setting — it is only "
                   "written if you change it.")

    def _mark_mixed(self, group, values, multiple):
        """A field the selection does not agree on comes back ABSENT from
        `common`. Say so on the control rather than showing one bone's value as
        if it were everyone's — nothing is written until you move it anyway."""
        for (g, key), w in self._fields.items():
            if g != group:
                continue
            base = w.toolTip().split(self._MIXED_NOTE)[0]
            mixed = multiple and key not in values
            w.setToolTip(base + self._MIXED_NOTE if mixed else base)

    # ------------------------------------------------------------- edit flow

    def _touch(self, group, key):
        if self._loading:
            return
        self._dirty.add((group, key))
        self._push_timer.start()

    def _value(self, group, key):
        w = self._fields[(group, key)]
        if isinstance(w, QCheckBox):
            return w.isChecked()
        if isinstance(w, NoScrollComboBox):
            return w.currentData()
        if isinstance(w, QLineEdit):
            return w.text()
        return w.value()

    # Degrees in the panel, radians in the solver. Blender stores every angle
    # in radians; showing radians to an animator would be absurd.
    _ANGLE_KEYS = {("bone", "cone_limit"), ("bone", "limit_x"),
                   ("bone", "limit_z"), ("scene", "guard_spin")}

    def _push(self):
        """Send ONLY what changed. See the note at the top of the file."""
        if not self._dirty or self._gate() is not None:
            return
        dirty, self._dirty = self._dirty, set()

        bone, tip, root, obj, scene = {}, {}, {}, {}, {}
        buckets = {"bone": bone, "tip": tip, "root": root,
                   "object": obj, "scene": scene}
        for group, key in dirty:
            val = self._value(group, key)
            if (group, key) in self._ANGLE_KEYS:
                val = math.radians(float(val))
            buckets[group][key] = val

        try:
            if bone or tip or root:
                payload = dict(bone)
                if tip:
                    payload["tip"] = tip
                if root:
                    payload["root"] = root
                r = self.bridge.jiggle_set(
                    payload, armature=self._armature_name(),
                    bones=self._target_bones()) or {}
                self._ok("Updated %d setting(s) on %d bone(s)."
                         % (r.get("written", 0), r.get("bones", 0)))
            if obj:
                self.bridge.jiggle_object(obj, armature=self._armature_name())
            if scene:
                r = self.bridge.jiggle_scene(scene) or {}
                self._show_ceiling(r.get("stiffness_ceiling"))
        except bridgemod.BridgeError as exc:
            self._fail(exc)

    def _set_group(self, group, values):
        for key, val in (values or {}).items():
            w = self._fields.get((group, key))
            if w is None:
                continue
            if (group, key) in self._ANGLE_KEYS:
                val = math.degrees(float(val))
            w.blockSignals(True)
            try:
                if isinstance(w, QCheckBox):
                    w.setChecked(bool(val))
                elif isinstance(w, NoScrollComboBox):
                    if val in (None, ""):
                        i = 0          # the "— none —" entry
                    else:
                        i = w.findData(val)
                        if i < 0:
                            # The scene has something this list has not seen
                            # (a collider assigned before a Refresh). Show it
                            # rather than silently resetting the bone to none.
                            w.addItem(str(val), val)
                            i = w.findData(val)
                    w.setCurrentIndex(max(0, i))
                elif isinstance(w, QLineEdit):
                    w.setText("" if val is None else str(val))
                else:
                    w.setValue(val)
            finally:
                w.blockSignals(False)

    # --------------------------------------------------------------- helpers

    def _armature_name(self):
        return self.armature.currentText().strip() or None

    def _target_bones(self):
        """Rows selected in the table win; otherwise Blender's own selection.

        Returning None means "whatever is selected in Blender", which is what
        the engine falls back to."""
        model = self.table.selectionModel()
        if model is None:
            return None
        names = [self._rows[i.row()].get("name")
                 for i in model.selectedRows()
                 if 0 <= i.row() < len(self._rows)]
        return names or None

    def _on_armature_changed(self, *_args):
        if self._loading:
            return
        self.refresh()

    def _on_table_selection(self):
        if self._loading:
            return
        self._load_bone_settings(announce=True)

    # --------------------------------------------------------------- actions

    def _run(self, fn, on_done):
        """Anything that can take real time goes to a worker thread."""
        self._busy(True)
        self._worker = _Worker(fn, self)
        self._worker.done.connect(on_done)
        self._worker.done.connect(lambda *_a: self._busy(False))
        self._worker.failed.connect(self._fail)
        self._worker.start()

    def _set_enabled(self, tip=None, root=None):
        name = self._armature_name()
        bones = self._target_bones()
        try:
            r = self.bridge.jiggle_enable(armature=name, bones=bones,
                                          tip=tip, root=root) or {}
        except bridgemod.BridgeError as exc:
            self._fail(exc)
            return
        self._ok("%d bone(s) updated — %d jiggling on this rig."
                 % (r.get("bones", 0), r.get("enabled_bones", 0)))
        self.refresh()

    def _select_bones(self):
        try:
            r = self.bridge.jiggle_select(armature=self._armature_name()) or {}
        except bridgemod.BridgeError as exc:
            self._fail(exc)
            return
        self._ok("Selected %d jiggling bone(s) in Blender."
                 % r.get("selected", 0))
        self.refresh()

    def _copy_settings(self):
        try:
            r = self.bridge.jiggle_copy(armature=self._armature_name(),
                                        bones=self._target_bones()) or {}
        except bridgemod.BridgeError as exc:
            self._fail(exc)
            return
        self._ok("Copied %s onto %d bone(s)."
                 % (r.get("source", "?"), r.get("bones", 0)))
        self.refresh()

    def _reset(self):
        try:
            r = self.bridge.jiggle_reset() or {}
        except bridgemod.BridgeError as exc:
            self._fail(exc)
            return
        self._ok("Simulation reset on %d rig(s)." % r.get("reset", 0))

    def _build_cache(self):
        name = self._armature_name()
        self._ok("Building the cache…")
        self._run(lambda: self.bridge.jiggle_cache(
            armature=name, frame_start=self.bake_start.value(),
            frame_end=self.bake_end.value()), self._cache_done)

    def _cache_done(self, r):
        r = r or {}
        self._ok("Cached %d frame(s) for %s."
                 % (r.get("cached", 0), r.get("object", "?")))

    def _clear_cache(self):
        try:
            r = self.bridge.jiggle_cache(clear=True) or {}
        except bridgemod.BridgeError as exc:
            self._fail(exc)
            return
        self._ok("Cleared %d cached frame(s)." % r.get("cleared", 0))

    def _bake(self):
        name = self._armature_name()
        if not name:
            self._note("Pick an armature first.")
            return
        start, end = self.bake_start.value(), self.bake_end.value()
        if end < start:
            self._note("The end frame is before the start frame.")
            return
        confirm = QMessageBox.question(
            self, "Bake jiggle",
            "Bake frames %d-%d of %s into keyframes?\n\n"
            "The rig is then frozen so the live simulation stops overwriting "
            "them — clear Freeze in the Armature section to go back to "
            "simulating." % (start, end, name),
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
        if confirm != QMessageBox.Yes:
            return
        action = self.bake_action.text().strip() or None
        self._ok("Baking %d frames…" % (end - start + 1))
        self._run(lambda: self.bridge.jiggle_bake(
            armature=name, frame_start=start, frame_end=end,
            preroll=self.bake_preroll.value(),
            selected_only=self.bake_selected.isChecked(),
            action=action, overwrite=self.bake_overwrite.isChecked()),
            self._bake_done)

    def _bake_done(self, r):
        r = r or {}
        self._ok("Baked %d frame(s) on %d bone(s) into '%s' (%d keys). The rig "
                 "is now frozen."
                 % (r.get("frames", 0), r.get("bones", 0),
                    r.get("action", "?"), r.get("keys", 0)))
        self.refresh()
