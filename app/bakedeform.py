"""Bake deformation to shape keys — a tool in the Optimization tab.

Marty, 2026-08-21: *"build this tool 'bake deformation to shape keys', make it
so it bakes on current frame range"*.

⚠ **It sits under Quadify because it is the second half of one workflow.**
Retopologise a character on the current frame, put a Surface Deform on the cage
so it follows the original exactly, then bake that motion into shape keys and
the modifier stack is free for Cloth — which is the whole reason the cage
exists. Measured on Marty's own character: 0.0 error on every frame.

⚠ **THE FRAME RANGE IS THE SCENE'S, and it is shown, not assumed.** He asked for
"current frame range", so the panel reads it back off Blender rather than
offering fields that could disagree with it.

⚠ **The cost is stated before the button is pressed.** One key holds a whole
copy of the mesh, so a long range on a dense cage is real memory — frames ×
vertices × 12 bytes. "This might be large" tells nobody anything; "410 MB"
does.

⚠ **The report says what happened, never what was asked for** — the same rule
the Quadify panel beside it lives by.

⚠⚠ **"Delete all shape keys" is the way back out**, added the same day: a bake
with the wrong frame range is otherwise stuck, and re-baking on top of 250 dead
keys stacks them. It confirms first, names the mesh and the count in the
dialog, and it is the ONE control here that cannot be taken back — which is why
it lives in its own box rather than under the bake button, where it would be a
misclick.
"""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QGroupBox, QHBoxLayout, QLabel,
                               QMessageBox, QPushButton, QSpinBox, QVBoxLayout)

import optimizer as optimizermod

FEATURE = "quadify"          # same add-on feature gate as the tool above it
TITLE = "Bake to shape keys"


class BakeDeformTool(optimizermod._OptimizerTool):
    """Freeze a modifier's motion into one shape key per frame."""

    CONFIG_KEY = "bakedeform"
    BROADCASTS = False

    def __init__(self, bridge, window, parent=None):
        super().__init__(bridge, window, parent)
        saved = self.settings()
        self._bake = {}

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(8)

        why = QLabel("Shape keys are evaluated before modifiers, so baking a "
                     "Surface Deform into keys leaves the modifier stack free "
                     "for Cloth.")
        why.setObjectName("dim")
        why.setWordWrap(True)
        outer.addWidget(why)

        pick = QHBoxLayout()
        self.target = QLabel("—")
        self.target.setObjectName("dim")
        self.target.setWordWrap(True)
        self.pick_button = QPushButton("Select mesh")
        self.pick_button.setToolTip("Take whatever is selected in Blender "
                                    "right now.")
        self.pick_button.clicked.connect(self.pick_selected)
        pick.addWidget(self.target, 1)
        pick.addWidget(self.pick_button, 0, Qt.AlignTop)
        outer.addLayout(pick)

        self.what = QLabel("")
        self.what.setObjectName("dim")
        self.what.setWordWrap(True)
        outer.addWidget(self.what)

        box = QGroupBox("Bake")
        lay = QVBoxLayout(box)

        self.range_label = QLabel("Frame range: —")
        self.range_label.setObjectName("dim")
        lay.addWidget(self.range_label)

        row = QHBoxLayout()
        row.addWidget(QLabel("Every"))
        self.step = QSpinBox()
        self.step.setRange(1, 50)
        self.step.setSuffix(" frame(s)")
        self.step.setValue(int(saved.get("step", 1)))
        self.step.setToolTip("Bake every Nth frame. Fewer keys and less "
                             "memory; in-between frames are a linear blend.")
        row.addWidget(self.step)
        row.addStretch(1)
        lay.addLayout(row)

        # ⚠ DISABLE, not delete, and that is the default. A bake that removes
        # what it baked cannot be checked afterwards, and a wrong frame range
        # would be unrecoverable.
        self.remove = QCheckBox("Delete the modifiers afterwards")
        self.remove.setChecked(bool(saved.get("remove", False)))
        self.remove.setToolTip("Off: they are switched off and left in place, "
                               "so you can turn them back on and bake again.")
        lay.addWidget(self.remove)

        self.cost = QLabel("")
        self.cost.setObjectName("dim")
        self.cost.setWordWrap(True)
        lay.addWidget(self.cost)

        self.run_button = QPushButton("Bake to shape keys")
        self.run_button.setEnabled(False)
        self.run_button.clicked.connect(self.run)
        lay.addWidget(self.run_button)
        outer.addWidget(box)

        # ⚠ Its OWN box, not another button under the bake. The two do
        # opposite things to the same mesh and one of them cannot be taken
        # back; sitting flush together they are a misclick.
        keys_box = QGroupBox("Shape keys on this mesh")
        keys_lay = QVBoxLayout(keys_box)
        self.keys_label = QLabel("—")
        self.keys_label.setObjectName("dim")
        self.keys_label.setWordWrap(True)
        keys_lay.addWidget(self.keys_label)

        self.clear_button = QPushButton("Delete all shape keys")
        self.clear_button.setEnabled(False)
        self.clear_button.setToolTip("Removes every key including Basis, and "
                                     "the keyframes driving them. The mesh "
                                     "goes back to its base shape.")
        self.clear_button.clicked.connect(self.clear_keys)
        keys_lay.addWidget(self.clear_button)
        outer.addWidget(keys_box)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        outer.addWidget(self.status)
        outer.addStretch(1)

        self.step.valueChanged.connect(self._save)
        self.remove.toggled.connect(self._save)

    # ------------------------------------------------------------------

    def _save(self):
        self.save_settings(step=self.step.value(),
                           remove=self.remove.isChecked())

    def refresh(self):
        """Read what there is to bake. Fired when the tool is shown."""
        reason = self.feature_reason()
        if reason:
            self.target.setText(reason)
            self.run_button.setEnabled(False)
            self.clear_button.setEnabled(False)
            return
        try:
            self.apply_bake_status(self.bridge.bake_status(poll=True))
        except Exception:                       # noqa: BLE001 - dead bridge
            self.target.setText("Blender is not connected.")
            self.run_button.setEnabled(False)
            self.clear_button.setEnabled(False)

    def pick_selected(self):
        """Take Blender's selection now. ⚠ NOT a poll — the same reasoning as
        Quadify's button: `poll=True` fails instantly while the connection is
        marked down, which is exactly when someone presses this."""
        try:
            status = self.bridge.bake_status()
        except Exception as exc:                # noqa: BLE001
            self.target.setText("Blender is not connected.")
            self.run_button.setEnabled(False)
            self.clear_button.setEnabled(False)
            self._fail(exc)
            return
        self.apply_bake_status(status)
        name = (status or {}).get("object")
        self._ok("Target is %s." % name if name else "Nothing is selected.")

    def apply_bake_status(self, status):
        """Paint one reading. Split out so tests can drive it."""
        if not isinstance(status, dict):
            return
        self._bake = dict(status)
        first = status.get("frame_start")
        last = status.get("frame_end")
        if first is not None and last is not None:
            self.range_label.setText("Frame range: %s to %s (Blender's own)"
                                     % (first, last))
        name = status.get("object") or ""
        # ⚠⚠ THE KEY COUNT IS PAINTED BEFORE THE MODIFIER CHECKS BELOW, and
        # that ordering is the whole point. A mesh that has just been baked has
        # NO enabled modifiers left — the bake switched them off — so it takes
        # the "nothing to bake" early return. Deciding the delete button down
        # there would leave it dead in the one state where it is most wanted.
        self._apply_keys(status)
        if not name:
            self.target.setText("Select a mesh object in Blender.")
            self.what.setText("")
            self.cost.setText("")
            self.run_button.setEnabled(False)
            return
        self.target.setText("%s — %s vertices"
                            % (name, "{:,}".format(status.get("verts", 0))))
        modifiers = status.get("modifiers") or []
        if not modifiers:
            self.what.setText("Nothing to bake — this mesh has no enabled "
                              "modifiers.")
            self.cost.setText("")
            self.run_button.setEnabled(False)
            return
        self.what.setText("Will bake: %s."
                          % ", ".join("%s (%s)" % (m.get("name"),
                                                   (m.get("type") or "").lower()
                                                   .replace("_", " "))
                                      for m in modifiers))
        # ⚠ Recomputed for the CHOSEN step, not taken from the reply — the
        # reply assumes every frame, and the number on screen has to match the
        # button that is about to be pressed.
        frames = int(status.get("frames") or 0)
        step = max(1, self.step.value())
        keys = max(1, (frames + step - 1) // step)
        megabytes = keys * int(status.get("verts") or 0) * 12 / (1024.0 * 1024.0)
        self.cost.setText("%d shape keys, about %.1f MB of mesh data."
                          % (keys, megabytes))
        self.run_button.setEnabled(True)

    def _apply_keys(self, status):
        """Paint the shape-key count and decide whether delete is available."""
        name = status.get("object") or ""
        count = int(status.get("shape_keys") or 0)
        shared = int(status.get("shared") or 0)
        if not name:
            self.keys_label.setText("—")
            self.clear_button.setEnabled(False)
            return
        # ⚠ An add-on older than the route gets a disabled button with the
        # reason on it, not a doomed request. The capability list is derived
        # from the add-on's own dispatcher, so this is a fact, not a guess.
        try:
            available = self.bridge.supports("bake_clear_keys")
        except Exception:                       # noqa: BLE001 - dead bridge
            available = True                    # unknown is not "missing"
        if not count:
            self.keys_label.setText("No shape keys.")
            self.clear_button.setEnabled(False)
            return
        text = "%d shape key%s." % (count, "" if count == 1 else "s")
        if shared > 1:
            # Shape keys live on the mesh datablock, so this is not a detail.
            text += (" ⚠ %d objects share this mesh and would all lose them."
                     % shared)
        if not available:
            text += " Deleting them needs a newer add-on."
        self.keys_label.setText(text)
        self.clear_button.setEnabled(available)

    def clear_keys(self):
        """Delete every shape key on the target mesh, after confirming.

        ⚠ **The dialog names the mesh, the count, and what it costs.** "Are you
        sure?" tells nobody anything; "251 keys off Cage, and the mesh goes
        back to its base shape" is a decision someone can actually take.
        """
        status = self._bake or {}
        name = status.get("object") or ""
        count = int(status.get("shape_keys") or 0)
        if not name or not count:
            return
        shared = int(status.get("shared") or 0)
        extra = ""
        if shared > 1:
            extra = ("\n\nShape keys belong to the mesh, and %d objects share "
                     "this one — every one of them loses them." % shared)
        if QMessageBox.question(
                self, "Delete all shape keys",
                "Delete all %d shape keys from %s?\n\n"
                "Basis goes too, so the mesh returns to its base shape, and "
                "the keyframes driving the keys are removed with them. This "
                "cannot be undone from here.%s" % (count, name, extra),
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No) != QMessageBox.Yes:
            return
        self._ok("Deleting shape keys…")
        self._set_busy(True)
        runner = optimizermod._Runner(
            lambda: self.bridge.clear_shape_keys(name), parent=self)
        runner.done.connect(self.show_clear_result)
        runner.failed.connect(self._failed_clear)
        self._runner = runner
        runner.start()

    def _failed_clear(self, message):
        self._set_busy(False)
        self._fail(message)

    def show_clear_result(self, reply):
        """Paint what happened, from the reply and only from the reply."""
        if not isinstance(reply, dict):
            self._set_busy(False)
            return
        if not reply.get("ok"):
            self._set_busy(False)
            self._fail(reply.get("error") or "the shape keys were not removed")
            return
        removed = reply.get("removed", 0)
        # ⚠ Zero is a real answer, not a failure: the add-on refuses nothing
        # when there was nothing there, and saying so is more use than an
        # error that reads like the button is broken.
        if removed:
            self._ok("Removed %d shape key%s from %s."
                     % (removed, "" if removed == 1 else "s",
                        reply.get("object", "the mesh")))
        else:
            self._ok("%s had no shape keys." % reply.get("object", "The mesh"))
        self._set_busy(False)

    def _set_busy(self, busy):
        """⚠ Both buttons share `self._runner`, and that attribute is what
        holds the worker thread alive. Starting a second run while the first is
        in flight drops the reference to it, so neither button is available
        while the other is working."""
        if busy:
            self.run_button.setEnabled(False)
            self.clear_button.setEnabled(False)
        else:
            # ⚠ Re-READ rather than re-enable. What is available afterwards
            # depends on what the mesh looks like now — a bake leaves nothing
            # to bake, a delete leaves nothing to delete — so guessing here
            # would light up a button that has no work behind it.
            self.refresh()

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh()

    # ------------------------------------------------------------------

    def run(self):
        params = {
            "object": (self._bake or {}).get("object") or "",
            "step": self.step.value(),
            "remove_modifiers": self.remove.isChecked(),
            "disable_modifiers": not self.remove.isChecked(),
        }
        self._ok("Baking…")
        self._set_busy(True)
        runner = optimizermod._Runner(
            lambda: self.bridge.bake_to_shape_keys(params), parent=self)
        runner.done.connect(self.show_result)
        runner.failed.connect(self._failed_bake)
        self._runner = runner
        runner.start()

    def _failed_bake(self, message):
        self._set_busy(False)
        self._fail(message)

    def show_result(self, reply):
        """Paint what happened, from the reply and only from the reply."""
        if not isinstance(reply, dict):
            self._set_busy(False)
            return
        if not reply.get("ok"):
            self._set_busy(False)
            self._fail(reply.get("error") or "the bake failed")
            return
        verb = "removed" if reply.get("removed") else "switched off"
        self._ok("Baked %d shape keys on %s, frames %s to %s. %s %s."
                 % (reply.get("keys", 0), reply.get("object", "the mesh"),
                    reply.get("frame_start"), reply.get("frame_end"),
                    ", ".join(reply.get("modifiers") or ["No modifiers"]),
                    verb))
        self._set_busy(False)

    def apply_status(self, status):
        """The tab's fan-out hook. Nothing here needs an optimizer status, but
        every tool in the tab is wired to it and a missing method breaks the
        fan-out for the others."""
        self._status = status or {}
