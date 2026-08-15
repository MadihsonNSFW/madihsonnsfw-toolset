"""Tools that live in the Node Setup tab.

Bridge-driven ports of the Image Node Tools add-on (Relink + Sequence Setup):
the node work happens in the connected Blender, on the tree open in its Node
Editor. Each tool is a plain QWidget; rendering.RenderingPage wraps it in a
rail entry and a titled settings page (same shell as the Rendering tab).
"""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QComboBox, QGroupBox, QHBoxLayout,
                               QLabel, QLineEdit, QPushButton, QVBoxLayout,
                               QWidget)

import bridge as bridgemod


class _NodeTool(QWidget):
    """Shared shape: blurb + options + action row + selectable status."""

    def __init__(self, bridge, window, parent=None):
        super().__init__(parent)
        self.bridge = bridge
        self.window = window

    def _make_status(self, lay):
        self.status = QLabel("—")
        self.status.setWordWrap(True)
        self.status.setObjectName("dim")
        self.status.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(self.status)

    def _fail(self, exc):
        self._busy(False)
        self.status.setStyleSheet("color: #e06c60;")
        self.status.setText(str(exc))
        if self.window is not None:
            self.window.update_bridge_status()

    def _ok(self, text):
        self.status.setStyleSheet("")
        self.status.setText(text)


class RelinkTool(_NodeTool):
    """Move a wired node's outgoing links onto an unconnected one — the
    swap-the-EXR helper, but it works on any node in any tree."""

    def __init__(self, bridge, window, parent=None):
        super().__init__(bridge, window, parent)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(9)

        blurb = QLabel(
            "In Blender's Node Editor, select the connected node and the "
            "unconnected one, then press Relink: the unconnected node takes "
            "over every downstream link (the source is left dangling). Works "
            "in compositor, shader and geometry trees. Single-input sockets "
            "swap; multi-input sockets (Join Geometry) keep both.")
        blurb.setWordWrap(True)
        blurb.setObjectName("dim")
        lay.addWidget(blurb)

        match_lab = QLabel("Match sockets by")
        match_lab.setObjectName("dim")
        lay.addWidget(match_lab)
        self.match = QComboBox()
        self.match.addItem("Name  (passes/layers that line up)", "NAME")
        self.match.addItem("Position  (socket order in the node)", "INDEX")
        self.match.setToolTip(
            "Name: pair sockets by name — right for image nodes whose "
            "passes/layers line up. Same-named sockets are disambiguated by "
            "data type (Mix repeats a name once per type).\n\n"
            "Position: pair sockets by their order in the node.")
        self.match.currentIndexChanged.connect(self._sync_enabled)
        lay.addWidget(self.match)

        self.chk_fallback = QCheckBox("Fall back to position")
        self.chk_fallback.setToolTip(
            "When a socket name has no counterpart, use the socket in the "
            "same position instead of skipping it.")
        lay.addWidget(self.chk_fallback)

        self.chk_inputs = QCheckBox("Also copy inputs")
        self.chk_inputs.setToolTip(
            "Copy the links feeding the source node as well. The source "
            "keeps its own inputs.")
        lay.addWidget(self.chk_inputs)

        row = QHBoxLayout()
        self.btn = QPushButton("Relink")
        self.btn.setObjectName("accent")
        self.btn.clicked.connect(self.run)
        row.addWidget(self.btn)
        self.btn_check = QPushButton("Check selection")
        self.btn_check.setToolTip(
            "Show which node would donate its links and which would receive "
            "them, without changing anything")
        self.btn_check.clicked.connect(self.refresh)
        row.addWidget(self.btn_check)
        row.addStretch(1)
        lay.addLayout(row)

        self._make_status(lay)
        lay.addStretch(1)

    # ------------------------------------------------------------------

    def _sync_enabled(self, *_):
        self.chk_fallback.setEnabled(self.match.currentData() == "NAME")

    def _busy(self, busy):
        self.btn.setEnabled(not busy)
        self.btn_check.setEnabled(not busy)

    def refresh(self):
        try:
            info = self.bridge.node_tools_status()
        except bridgemod.BridgeError as exc:
            self._fail(exc)
            return
        r = info["relink"]
        if r.get("error"):
            self.status.setStyleSheet("")
            self.status.setText(r["error"])
            return
        self._ok("Tree: %s (%s)\nFrom: %s (%d link%s)\nTo: %s"
                 % (r["tree"], r["tree_type"], r["source"], r["links"],
                    "" if r["links"] == 1 else "s", ", ".join(r["targets"])))

    def run(self):
        if self.window is not None and not self.window.bridge_free_for_tools():
            return
        self._busy(True)
        try:
            r = self.bridge.relink_nodes(
                match_mode=self.match.currentData(),
                index_fallback=self.chk_fallback.isChecked(),
                copy_inputs=self.chk_inputs.isChecked())
        except bridgemod.BridgeError as exc:
            self._fail(exc)
            return
        finally:
            self._busy(False)

        lines = ["Relinked %d connection%s: %s → %s  (in '%s')"
                 % (r["made"], "" if r["made"] == 1 else "s", r["source"],
                    ", ".join(r["targets"]), r["tree"])]
        if r["missing"]:
            lines.append("No match for: %s." % ", ".join(r["missing"]))
        self._ok("\n".join(lines))
        if self.window is not None:
            self.window.statusBar().showMessage(
                "Relinked %d connection(s)" % r["made"], 6000)


class SequenceSetupTool(_NodeTool):
    """Point a compositor Image node at one frame of a render, press the
    button: frames are counted on disk and the sequence, scene range and
    render output path are filled in."""

    def __init__(self, bridge, window, parent=None):
        super().__init__(bridge, window, parent)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(9)

        blurb = QLabel(
            "Select an Image node in Blender's compositor that points at any "
            "one frame of a render. Set Up counts the frames on disk, turns "
            "the node into a Sequence (Frames / Start / Offset computed so "
            "the first file lands on the scene's start frame), matches the "
            "scene range, and rebuilds the render output path for the shot.")
        blurb.setWordWrap(True)
        blurb.setObjectName("dim")
        lay.addWidget(blurb)

        self.chk_range = QCheckBox("Set scene range to the sequence")
        self.chk_range.setChecked(True)
        self.chk_range.toggled.connect(self._sync_enabled)
        lay.addWidget(self.chk_range)
        self.chk_start1 = QCheckBox("Start at frame 1")
        self.chk_start1.setChecked(True)
        self.chk_start1.setToolTip(
            "Play the sequence from scene frame 1. Turn off to keep the "
            "scene's current start frame.")
        lay.addWidget(self.chk_start1)

        self.chk_output = QCheckBox("Set render output path")
        self.chk_output.setChecked(True)
        self.chk_output.setToolTip(
            "Rebuild the render output path for the shot this sequence "
            "belongs to (needs the .blend saved next to a Render folder).")
        self.chk_output.toggled.connect(self._sync_enabled)
        lay.addWidget(self.chk_output)

        folder_row = QHBoxLayout()
        self.folder_lab = QLabel("Output folder")
        self.folder_lab.setObjectName("dim")
        folder_row.addWidget(self.folder_lab)
        self.folder = QLineEdit("exr_composited")
        self.folder.setToolTip(
            "Sub-folder of the shot that composited frames are written to")
        folder_row.addWidget(self.folder, 1)
        lay.addLayout(folder_row)

        suffix_row = QHBoxLayout()
        self.suffix_lab = QLabel("Output suffix")
        self.suffix_lab.setObjectName("dim")
        suffix_row.addWidget(self.suffix_lab)
        self.suffix = QLineEdit("_exr_composited_")
        self.suffix.setToolTip(
            "Appended to the shot name; Blender adds the frame number and "
            "extension after it")
        suffix_row.addWidget(self.suffix, 1)
        lay.addLayout(suffix_row)

        row = QHBoxLayout()
        self.btn = QPushButton("Set Up Sequence")
        self.btn.setObjectName("accent")
        self.btn.clicked.connect(self.run)
        row.addWidget(self.btn)
        self.btn_check = QPushButton("Check node")
        self.btn_check.setToolTip(
            "Show the selected Image node, its current frame settings and "
            "the output path preview — nothing is changed and no disk scan")
        self.btn_check.clicked.connect(self.refresh)
        row.addWidget(self.btn_check)
        row.addStretch(1)
        lay.addLayout(row)

        self._make_status(lay)
        lay.addStretch(1)

    # ------------------------------------------------------------------

    def _sync_enabled(self, *_):
        self.chk_start1.setEnabled(self.chk_range.isChecked())
        on = self.chk_output.isChecked()
        for w in (self.folder_lab, self.folder, self.suffix_lab, self.suffix):
            w.setEnabled(on)

    def _busy(self, busy):
        self.btn.setEnabled(not busy)
        self.btn_check.setEnabled(not busy)

    def refresh(self):
        try:
            info = self.bridge.node_tools_status(
                output_folder=self.folder.text().strip() or "exr_composited",
                output_suffix=self.suffix.text())
        except bridgemod.BridgeError as exc:
            self._fail(exc)
            return
        s = info["sequence"]
        if s.get("error"):
            self.status.setStyleSheet("")
            self.status.setText(s["error"])
            return
        lines = ["%s — %s" % (s["node"], s["file"]),
                 "Now: %d frames, offset %d" % (s["frames_now"],
                                                s["offset_now"])]
        if s.get("output_error"):
            lines.append("Output: " + s["output_error"])
        elif s.get("output_preview"):
            lines.append("Out: " + s["output_preview"])
        self._ok("\n".join(lines))

    def run(self):
        if self.window is not None and not self.window.bridge_free_for_tools():
            return
        self._busy(True)
        try:
            r = self.bridge.setup_image_sequence(
                set_scene_range=self.chk_range.isChecked(),
                start_at_one=self.chk_start1.isChecked(),
                set_output=self.chk_output.isChecked(),
                output_folder=self.folder.text().strip() or "exr_composited",
                output_suffix=self.suffix.text())
        except bridgemod.BridgeError as exc:
            self._fail(exc)
            return
        finally:
            self._busy(False)

        lines = ["%s: %d frames (%d-%d), scene %d-%d"
                 % (r["node"], r["count"], r["first"], r["last"],
                    r["scene_start"], r["scene_end"])]
        if r.get("output"):
            lines.append("Output path: %s" % r["output"])
        for note in r.get("notes", []):
            lines.append("⚠ " + note)
        self._ok("\n".join(lines))
        if self.window is not None:
            self.window.statusBar().showMessage(
                "Sequence set up: %d frames" % r["count"], 6000)


class RelinkPage(QWidget):
    """Both node tools on ONE rail page — Marty's call, 2026-08-04.

    ⚠ COMPOSED, NOT MERGED. The two tools keep their own controls, their own
    status line and their own bridge calls; only where they are shown changed.
    Rewriting them into a single class would have meant re-testing behaviour
    that nobody asked to change, and `app_nodetab_test.py` still drives each
    tool directly — which is exactly why they are still separate classes.
    """

    def __init__(self, bridge, window, parent=None):
        super().__init__(parent)
        self.bridge = bridge
        self.window = window

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(14)

        self.relink = RelinkTool(bridge, window)
        self.sequence = SequenceSetupTool(bridge, window)
        for title, tool in (("Relink nodes", self.relink),
                            ("Image sequence setup", self.sequence)):
            box = QGroupBox(title)
            inner = QVBoxLayout(box)
            inner.setContentsMargins(10, 8, 10, 10)
            inner.addWidget(tool)
            lay.addWidget(box)

    def refresh(self):
        """Read both tools' state in one go, so the page's own refresh does
        what a user pressing both Check buttons would."""
        self.relink.refresh()
        self.sequence.refresh()
