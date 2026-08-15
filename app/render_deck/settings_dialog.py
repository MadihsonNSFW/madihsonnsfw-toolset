"""Settings dialog."""
from __future__ import annotations

from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QFileDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QPushButton, QVBoxLayout, QWidget,
)

import widgets

from .settings import ENGINES, Settings


class SettingsDialog(widgets.GuardedDialog):
    def __init__(self, settings: Settings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumWidth(560)
        self._s = settings

        form = QFormLayout()

        # Blender versions — the user can register several installs and pick
        # which one to render with from the main window's dropdown.
        blender_box = QWidget()
        bv = QVBoxLayout(blender_box)
        bv.setContentsMargins(0, 0, 0, 0)
        self.blender_list = QListWidget()
        self.blender_list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.blender_list.setMaximumHeight(110)
        for p in settings.blender_paths:
            self.blender_list.addItem(p)
        bv.addWidget(self.blender_list)
        btn_row = QHBoxLayout()
        add_b = QPushButton("Add…")
        add_b.clicked.connect(self._add_blender)
        rem_b = QPushButton("Remove selected")
        rem_b.clicked.connect(self._remove_blender)
        btn_row.addWidget(add_b)
        btn_row.addWidget(rem_b)
        btn_row.addStretch(1)
        bv.addLayout(btn_row)
        form.addRow("Blender versions:", blender_box)

        # Default output dir
        self.output_edit = QLineEdit(settings.output_dir)
        self.output_edit.setPlaceholderText("(leave empty to use each .blend's own output path)")
        out_row = QHBoxLayout()
        out_row.addWidget(self.output_edit)
        browse_o = QPushButton("Browse…")
        browse_o.clicked.connect(self._pick_output)
        out_row.addWidget(browse_o)
        form.addRow("Default output folder:", out_row)

        # Default engine
        self.engine_combo = QComboBox()
        for label, value in ENGINES.items():
            self.engine_combo.addItem(label, value)
        idx = max(0, list(ENGINES.values()).index(settings.default_engine)
                  if settings.default_engine in ENGINES.values() else 0)
        self.engine_combo.setCurrentIndex(idx)
        form.addRow("Default engine:", self.engine_combo)

        # Toolbar scale
        self.scale_combo = QComboBox()
        for label, value in [("50%", 0.5), ("75%", 0.75), ("100%", 1.0),
                             ("125%", 1.25), ("150%", 1.5), ("200%", 2.0)]:
            self.scale_combo.addItem(label, value)
        cur = min(range(self.scale_combo.count()),
                  key=lambda i: abs(self.scale_combo.itemData(i) - settings.toolbar_scale))
        self.scale_combo.setCurrentIndex(cur)
        form.addRow("Toolbar button size:", self.scale_combo)

        # Extra args
        self.extra_edit = QLineEdit(settings.extra_args)
        self.extra_edit.setPlaceholderText("e.g. --factory-startup   -t 16")
        form.addRow("Extra Blender args:", self.extra_edit)

        # Crash auto-resume
        self.resume_check = QCheckBox(
            "Auto-resume: if a render crashes, restart from the frame it died on")
        self.resume_check.setChecked(settings.auto_resume)
        form.addRow("", self.resume_check)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)

    def _add_blender(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select blender.exe", "", "Blender (blender.exe);;All files (*.*)")
        if not path:
            return
        existing = {self.blender_list.item(i).text()
                    for i in range(self.blender_list.count())}
        if path not in existing:
            self.blender_list.addItem(path)

    def _remove_blender(self):
        for item in self.blender_list.selectedItems():
            self.blender_list.takeItem(self.blender_list.row(item))

    def _pick_output(self):
        path = QFileDialog.getExistingDirectory(self, "Select output folder")
        if path:
            self.output_edit.setText(path)

    def apply_to(self) -> Settings:
        paths = [self.blender_list.item(i).text()
                 for i in range(self.blender_list.count())]
        self._s.blender_paths = paths
        # Keep the active selection valid after edits.
        if self._s.blender_path not in paths:
            self._s.blender_path = paths[0] if paths else ""
        self._s.output_dir = self.output_edit.text().strip()
        self._s.default_engine = self.engine_combo.currentData()
        self._s.toolbar_scale = self.scale_combo.currentData()
        self._s.extra_args = self.extra_edit.text().strip()
        self._s.auto_resume = self.resume_check.isChecked()
        self._s.save()
        return self._s
