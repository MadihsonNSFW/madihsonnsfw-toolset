"""Tools that live in the Rendering tab.

Each tool is a plain QWidget; rendering.RenderingPage wraps it in a rail entry
and a titled settings page.
"""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QComboBox, QHBoxLayout, QLabel,
                               QPushButton, QVBoxLayout, QWidget)

import bridge as bridgemod
import theme

# The Render Queue tool lives in its own package (render_deck/) — it's a whole
# offline render manager (job table, probe, controller); re-exported here so
# every Rendering-tab tool is reachable from render_tools.
from render_deck.queue_tool import RenderQueueTool  # noqa: F401


class DenoiseSetupTool(QWidget):
    """One button: turn on the per-view-layer denoising data passes and build
    the compositor tree that denoises each layer on its own passes."""

    def __init__(self, bridge, window, parent=None):
        super().__init__(parent)
        self.bridge = bridge
        self.window = window

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(9)

        blurb = QLabel(
            "Builds the whole denoising setup in the compositor: enables the "
            "passes, adds the Denoise nodes with Denoising Normal / Albedo "
            "wired in as guides, and recombines everything into the final "
            "image.")
        blurb.setWordWrap(True)
        blurb.setObjectName("dim")
        lay.addWidget(blurb)

        split_lab = QLabel("Denoise separately per")
        split_lab.setObjectName("dim")
        lay.addWidget(split_lab)
        self.split = QComboBox()
        self.split.addItem("Light pass  (diffuse / glossy / transmission / volume)",
                           "PASSES")
        self.split.addItem("View layer  (one denoise on the combined image)",
                           "LAYERS")
        self.split.setToolTip(
            "Light pass: every pass is denoised on its own — Direct and "
            "Indirect separately for each component — then the beauty is "
            "rebuilt as (Direct + Indirect) × Colour, plus Volume, Emission "
            "and Environment, with the alpha restored. Colour passes are left "
            "alone because they are already clean.\n\n"
            "View layer: one Denoise node per view layer on the combined "
            "image. Fewer nodes, less control.")
        lay.addWidget(self.split)

        self.chk_disable_render = QCheckBox(
            "Disable render-time denoising (denoise in the compositor instead)")
        self.chk_disable_render.setChecked(True)
        self.chk_disable_render.setToolTip(
            "Recommended. Cycles would otherwise denoise once during the render "
            "and again in the compositor. With this on, the beauty pass stays "
            "noisy and the Denoise node does the work.")
        lay.addWidget(self.chk_disable_render)

        self.chk_combine = QCheckBox("Combine multiple view layers with Alpha Over")
        self.chk_combine.setChecked(True)
        self.chk_combine.setToolTip(
            "Off: the per-layer Denoise nodes are left unconnected so you can "
            "wire the combine yourself.")
        lay.addWidget(self.chk_combine)

        row = QHBoxLayout()
        self.btn = QPushButton("Set Up Denoising")
        self.btn.setObjectName("accent")
        self.btn.clicked.connect(self.run)
        row.addWidget(self.btn)
        self.btn_clear = QPushButton("Remove Setup")
        self.btn_clear.setObjectName("danger")
        self.btn_clear.setToolTip(
            "Undo: delete the denoise tree this tool built and put the render "
            "settings back exactly as they were before you set it up. A "
            "compositor tree you built yourself is never touched.")
        self.btn_clear.clicked.connect(self.clear)
        row.addWidget(self.btn_clear)
        self.btn_refresh = QPushButton("Check scene")
        self.btn_refresh.setToolTip("Show the scene's engine and view layers")
        self.btn_refresh.clicked.connect(self.refresh)
        row.addWidget(self.btn_refresh)
        row.addStretch(1)
        lay.addLayout(row)

        self.chk_keep_passes = QCheckBox(
            "Removing: keep the passes enabled (only delete the node tree)")
        self.chk_keep_passes.setToolTip(
            "Leave the render passes switched on so you can wire your own "
            "compositor tree with them.")
        lay.addWidget(self.chk_keep_passes)

        self.status = QLabel("—")
        self.status.setWordWrap(True)
        self.status.setObjectName("dim")
        self.status.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(self.status)
        lay.addStretch(1)

    # ------------------------------------------------------------------

    def _busy(self, busy):
        self.btn.setEnabled(not busy)
        self.btn_clear.setEnabled(not busy)
        self.btn_refresh.setEnabled(not busy)

    def refresh(self):
        try:
            info = self.bridge.list_view_layers()
        except bridgemod.BridgeError as exc:
            self._fail(exc)
            return
        bits = []
        for vl in info["layers"]:
            marks = []
            if not vl["use"]:
                marks.append("disabled")
            if vl["denoise_passes"]:
                marks.append("passes on")
            bits.append("%s%s" % (vl["name"],
                                  "  (%s)" % ", ".join(marks) if marks else ""))
        engine_ok = info["engine"] == "CYCLES"
        self.status.setText(
            "Engine: %s%s\nView layers: %s"
            % (info["engine"],
               "" if engine_ok else "   ⚠ denoising data passes need Cycles",
               "; ".join(bits)))
        self.status.setStyleSheet(
            "" if engine_ok else "color: %s;" % theme.TYPE_COLORS["mirror"])

    def run(self):
        if self.window is not None and not self.window.bridge_free_for_tools():
            return
        self._busy(True)
        try:
            r = self.bridge.setup_denoise(
                disable_render_denoise=self.chk_disable_render.isChecked(),
                combine="ALPHA_OVER" if self.chk_combine.isChecked() else "NONE",
                split=self.split.currentData())
        except bridgemod.BridgeError as exc:
            self._fail(exc)
            return
        finally:
            self._busy(False)

        self.status.setStyleSheet("")
        lines = ["Built '%s' — %d Denoise node(s) across %d view layer(s): %s."
                 % (r["tree"], r["denoise_nodes"], len(r["layers"]),
                    ", ".join(r["layers"]))]
        if r.get("split") == "PASSES":
            info = next(iter(r.get("passes", {}).values()), {})
            if info.get("denoised_passes"):
                lines.append("Denoised separately: %s."
                             % ", ".join(info["denoised_passes"]))
            if info.get("flat_passes"):
                lines.append("Added back without denoising (already clean): "
                             "%s, plus the colour passes."
                             % ", ".join(info["flat_passes"]))
            if info.get("skipped_components"):
                lines.append("No passes found for: %s."
                             % ", ".join(info["skipped_components"]))
            lines.append("Beauty rebuilt as (Direct + Indirect) × Colour, "
                         "alpha restored at the end.")
        else:
            srcs = set(r.get("image_sources", {}).values())
            if srcs == {"Image"}:
                lines.append("Denoising the beauty pass directly — render-time "
                             "denoising is off, so it is the noisy image.")
            elif srcs == {"Noisy Image"}:
                lines.append("Denoising the Noisy Image pass (render-time "
                             "denoising left on).")
        if r["render_denoise_disabled"]:
            lines.append("Render-time denoising switched off.")
        if len(r["layers"]) > 1:
            lines.append("View layers combined with %s." % r["combined"])
        self.status.setText("\n".join(lines))
        if self.window is not None:
            self.window.statusBar().showMessage(
                "Denoise setup: %s (%d layer(s))"
                % (r["tree"], r["denoise_nodes"]), 6000)

    def clear(self):
        if self.window is not None and not self.window.bridge_free_for_tools():
            return
        self._busy(True)
        try:
            r = self.bridge.clear_denoise(
                restore_passes=not self.chk_keep_passes.isChecked())
        except bridgemod.BridgeError as exc:
            self._fail(exc)
            return
        finally:
            self._busy(False)

        lines = []
        if r["removed_tree"]:
            lines.append("Removed the '%s' node tree." % r["removed_tree"])
        else:
            lines.append("No node tree to remove.")
        if r["passes_restored"]:
            lines.append("Render settings restored on: %s."
                         % ", ".join(r["restored_layers"]))
        elif r["had_snapshot"]:
            lines.append("Passes left enabled, as asked.")
        else:
            lines.append("No saved snapshot for this scene, so the pass "
                         "toggles were left as they are.")
        self.status.setStyleSheet("")
        self.status.setText("\n".join(lines))
        if self.window is not None:
            self.window.statusBar().showMessage("Denoise setup removed", 6000)

    def _fail(self, exc):
        self._busy(False)
        self.status.setStyleSheet("color: #e06c60;")
        self.status.setText(str(exc))
        if self.window is not None:
            self.window.update_bridge_status()
