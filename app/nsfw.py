"""NSFW Tools tab — MADI rigs built into the scene from an embedded spec.

First tool: the **Affector Torus**. A torus whose geometry-node rig is dented
and bulged by any mesh in an Affectors collection. This tool's whole job is to
put it in the scene; everything after that happens in Blender.

WHERE THE RIG COMES FROM: `nsfw_spec.py` in this build (packed data), sent over
the bridge to a GENERIC builder in the add-on (`assets.py`). The add-on knows
nothing about tori or deformers, so it can be read without giving anything away.
⚠ This does not make a built rig secret — a node group in someone's file can
always be opened. It keeps the recipe from ever SHIPPING readable.

IT ARRIVES AS A NORMAL GEOMETRY NODES MODIFIER, exactly as it was in the file it
was captured from: the real group name, the modifier's own panel, every input
where Blender puts it. That is deliberate — this tool does not mirror the rig's
settings, so the modifier panel is the only place to tune it and hiding any part
of it would leave nothing to drive.
"""

import nsfw_spec
from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QMessageBox, QPushButton,
                               QVBoxLayout, QWidget)

# Marty's pick (2026-08-04, through Developer mode: edit). Deliberately NOT
# theme.ACCENT - this one button is meant to stand out from every other accent
# button in the app.
ACCENT_BUTTON = "#ff2962"


class _Task(QThread):
    """One bridge call, off the GUI thread. Building the rig creates a 9k-vert
    mesh and a ~100-node group in a single main-thread call, so it must never
    run inline."""

    done = Signal(object)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self._fn = fn

    def run(self):
        try:
            self.done.emit({"ok": True, "result": self._fn()})
        except Exception as err:
            self.done.emit({"ok": False, "error": str(err)})


class AffectorTorusTool(QWidget):
    """Add the Affector Torus to the scene."""

    OBJECT = "Madi Torus"
    MODIFIER = "Affector Deform"

    def __init__(self, bridge, parent=None):
        super().__init__(parent)
        self.bridge = bridge
        self._task = None
        # The rig this tool reports on. Blender suffixes a duplicate name, so a
        # scene that ALREADY has a "Madi Torus" gets "Madi Torus.001" - without
        # tracking what we actually created, the status line would describe
        # somebody else's object.
        self._object = self.OBJECT

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)

        # Rich text: the <b> runs and the paragraph break are deliberate, so the
        # label renders as HTML. Blank lines do NOT survive that - use <br>.
        self.blurb = QLabel(
            "A torus rig that dents and bulges where an affector passes through "
            "it. <b>Add it, then tune it on its Geometry Nodes modifier in "
            "Blender</b>, <b>put your affector meshes in a collection and pick "
            "that collection as its Affectors input.</b> Bind it to your own "
            "mesh with a Surface Deform modifier."
            "<br><br>"
            "Affectors should have a scale of 1, so make sure to apply to scale.")
        self.blurb.setObjectName("dim")
        self.blurb.setWordWrap(True)
        lay.addWidget(self.blurb)

        row = QHBoxLayout()
        self.btn_add = QPushButton("Add Stretching torus")
        self.btn_add.setObjectName("accent")
        self.btn_add.setStyleSheet("background-color: %s;" % ACCENT_BUTTON)
        self.btn_add.clicked.connect(self.add_torus)
        row.addWidget(self.btn_add)
        row.addStretch(1)
        lay.addLayout(row)

        self.status = QLabel("")
        self.status.setObjectName("dim")
        self.status.setWordWrap(True)
        lay.addWidget(self.status)
        lay.addStretch(1)

        self.set_enabled_state(True)
        self._loaded = False

    def showEvent(self, event):
        """First load happens when the tab is actually OPENED, never in the
        constructor.

        ⚠ This is not tidiness, it is a crash fix. The locked-tab preview builds
        this page for real, `grab()`s a pixmap and `deleteLater()`s it - so a
        `QTimer.singleShot(0, self.refresh)` in __init__ fired on the next loop
        turn against an already-deleted C++ object and took the whole app down
        (0xC0000409). `grab()` does NOT show a widget, so a preview reaches none
        of this. Any future tool that wants to load something on open must use
        showEvent too, or a bare timer OWNED BY self - never QTimer.singleShot
        with a bound method.
        """
        super().showEvent(event)
        if not self._loaded:
            self._loaded = True
            self.refresh()

    # ------------------------------------------------------------- helpers

    def _reason(self):
        """Why the tool cannot act, or None. An add-on too old to build assets
        loses this ONE tool with a plain reason, like every other gate."""
        try:
            return self.bridge.feature_reason("nsfw_assets")
        except Exception:
            return None

    def set_capture_busy(self, busy):
        """Called by the page while Blender is busy elsewhere."""
        self.setEnabled(not busy)

    def set_enabled_state(self, enabled):
        blocked = self._reason()
        self.btn_add.setEnabled(enabled and not blocked and self._task is None)
        if blocked:
            self.status.setText(blocked)

    def _run(self, fn, on_done):
        if self._task is not None:
            return
        task = _Task(fn, self)
        self._task = task

        def finished(result):
            self._task = None
            self.set_enabled_state(True)
            on_done(result)

        task.done.connect(finished)
        task.finished.connect(task.deleteLater)
        self.set_enabled_state(False)
        task.start()

    # -------------------------------------------------------------- status

    def refresh(self):
        if self._reason():
            self.set_enabled_state(True)
            return
        obj, mod = self._object, self.MODIFIER

        def work():
            return self.bridge.asset_status(obj, mod)

        self._run(work, self._on_status)

    def _on_status(self, result):
        if not result.get("ok"):
            self.status.setText("Blender is not reachable.")
            return
        info = result.get("result") or {}
        if not info.get("present"):
            # Names the button by whatever it actually says, so a rename can
            # never leave this pointing at a control that is not there.
            self.status.setText("Not in the scene yet — press %s."
                                % self.btn_add.text())
            return
        self.status.setText(
            "“%s” is in the scene. Its settings live on the “%s” modifier."
            % (info.get("object"), self.MODIFIER))

    # ------------------------------------------------------------- actions

    def add_torus(self):
        blocked = self._reason()
        if blocked:
            QMessageBox.information(self, "Affector Torus", blocked)
            return
        packed = nsfw_spec.packed()

        def work():
            return self.bridge.asset_build(packed)

        self._run(work, self._on_built)

    def _on_built(self, result):
        if not result.get("ok"):
            self.status.setText("Could not add it: %s" % result.get("error"))
            return
        info = result.get("result") or {}
        # Report on whatever was ACTUALLY created, not the name we asked for.
        # In a scene that already had a "Madi Torus" this is "Madi Torus.001".
        if info.get("object"):
            self._object = info["object"]
        if info.get("problems"):
            # A rig that built with complaints is not one to trust silently.
            self.status.setText(
                "Added “%s”, but %d part(s) did not apply — the rig may not "
                "behave correctly." % (info.get("object"), len(info["problems"])))
            return
        self.status.setText(
            "Added “%s” — %d nodes, %d verts. Its settings are on the “%s” "
            "modifier in Blender."
            % (info.get("object"), info.get("nodes", 0), info.get("verts", 0),
               self.MODIFIER))
