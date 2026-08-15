# Task 10 verification: MergeBakeTool offscreen — enable/disable logic and
# option passing through a stub bridge.
# Run with the app venv python (QT_QPA_PLATFORM=offscreen).
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

sys.path.insert(0, os.path.join(_ROOT, "app"))

from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

import anim_layers  # noqa: E402

PASS = []
FAIL = []


def ok(cond, label):
    (PASS if cond else FAIL).append(label)
    print(("ok   " if cond else "FAIL ") + label, flush=True)


class StubBridge:
    def __init__(self):
        self.calls = []
        self.result = {"error": None, "layers": [],
                       "baked": {"mode": "NEW", "direction": "ALL",
                                 "bake_type": "AL", "result": "Baked Layer",
                                 "result_blend": "REPLACE",
                                 "merged": ["Base Layer", "Layer 2"],
                                 "channels": 12, "keys": 240, "smart": True,
                                 "steps": 1, "backups": [], "frames": 30,
                                 "frame_start": 1.0, "frame_end": 30.0}}

    def anim_layers_bake(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


app = QApplication.instance() or QApplication([])
stub = StubBridge()
tool = anim_layers.MergeBakeTool(stub, None)

# ---------------------------------------------------------------- defaults
ok(tool.type_combo.currentData() == "AL", "default bake type = AL")
ok(tool.op_combo.currentData() == "NEW", "default operator = new layer")
ok(tool.dir_combo.currentData() == "ALL", "default direction = all")
ok(tool.chk_smart.isChecked(), "smart bake defaults on")
ok(not tool.steps.isEnabled(), "steps disabled while smart is on")
ok(tool.chk_modifiers.isChecked(), "merge modifiers defaults on")
ok(not tool.chk_constraints.isEnabled(), "clear constraints disabled for AL")
ok(not tool.chk_backup.isEnabled(), "copy-original disabled for new-layer op")
ok(tool.btn_bake.text() == "Bake", "button reads Bake for new-layer op")

# ---------------------------------------------------------------- NLA state
tool.type_combo.setCurrentIndex(1)          # NLA
ok(not tool.chk_smart.isEnabled(), "NLA: smart disabled")
ok(tool.steps.isEnabled(), "NLA: steps enabled")
ok(not tool.chk_modifiers.isEnabled(), "NLA: merge-modifiers disabled")
ok(tool.chk_constraints.isEnabled(), "NLA: clear constraints enabled")
tool.type_combo.setCurrentIndex(0)          # back to AL
ok(tool.chk_smart.isEnabled() and not tool.chk_constraints.isEnabled(),
   "AL restored after switching back")

# ---------------------------------------------------------------- MERGE state
tool.op_combo.setCurrentIndex(1)            # MERGE
ok(tool.chk_backup.isEnabled(), "MERGE: copy-original enabled")
ok(tool.btn_bake.text() == "Merge", "button reads Merge for merge op")
tool.op_combo.setCurrentIndex(0)

# ---------------------------------------------------------------- bake call
tool.bake()
ok(len(stub.calls) == 1, "bake sends one bridge call")
c = stub.calls[-1]
ok(c["mode"] == "NEW" and c["direction"] == "ALL" and c["bake_type"] == "AL"
   and c["smart"] is True and c["selected_only"] is False
   and c["merge_modifiers"] is True and c["clear_constraints"] is False
   and c["copy_original"] is False,
   "default params passed correctly (got %s)" % c)
ok("Baked 2 layers" in tool.status.text()
   and "'Baked Layer'" in tool.status.text(),
   "status summarises the result (got: %s)" % tool.status.text())

# options routed through
tool.dir_combo.setCurrentIndex(1)           # DOWN
tool.chk_smart.setChecked(False)
tool.steps.setValue(4)
tool.chk_selected.setChecked(True)
tool.chk_modifiers.setChecked(False)
tool.bake()
c = stub.calls[-1]
ok(c["direction"] == "DOWN" and c["smart"] is False and c["steps"] == 4
   and c["selected_only"] is True and c["merge_modifiers"] is False,
   "changed options passed correctly (got %s)" % c)

# NLA forces smart off + modifiers merged, clear-constraints honoured
tool.type_combo.setCurrentIndex(1)
tool.chk_constraints.setChecked(True)
tool.bake()
c = stub.calls[-1]
ok(c["bake_type"] == "NLA" and c["smart"] is False
   and c["merge_modifiers"] is True and c["clear_constraints"] is True,
   "NLA call forces AL-only options off (got %s)" % c)
tool.type_combo.setCurrentIndex(0)

# MERGE asks first; a Yes passes copy_original
asked = []
QMessageBox.question = staticmethod(
    lambda *a, **k: (asked.append(1), QMessageBox.StandardButton.Yes)[1])
tool.op_combo.setCurrentIndex(1)
stub.result["baked"]["mode"] = "MERGE"
stub.result["baked"]["backups"] = ["Base Layer.orig", "Layer 2.orig"]
tool.bake()
c = stub.calls[-1]
ok(bool(asked), "merge asks for confirmation")
ok(c["mode"] == "MERGE" and c["copy_original"] is True,
   "merge passes copy_original (got %s)" % c)
ok("Backups:" in tool.status.text(),
   "status mentions the backups (got: %s)" % tool.status.text())

# UP direction reaches the bridge as UP
tool.op_combo.setCurrentIndex(0)
tool.dir_combo.setCurrentIndex(2)
tool.bake()
ok(stub.calls[-1]["direction"] == "UP", "UP direction passed")

print("\n%d passed, %d failed" % (len(PASS), len(FAIL)), flush=True)
sys.exit(1 if FAIL else 0)
