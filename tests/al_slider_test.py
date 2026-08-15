# Offscreen DragSlider + row integration test.
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

sys.path.insert(0, os.path.join(_ROOT, "app"))

from PySide6.QtCore import QPoint, Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QInputDialog  # noqa: E402

import anim_layers  # noqa: E402

PASS = []
FAIL = []


def ok(cond, label):
    (PASS if cond else FAIL).append(label)
    print(("ok   " if cond else "FAIL ") + label, flush=True)


app = QApplication.instance() or QApplication([])

s = anim_layers.DragSlider()
s.show()
committed = []
previews = []
s.committed.connect(committed.append)
s.preview.connect(previews.append)

# value/clamp
s.set_value(0.5)
ok(abs(s.value() - 0.5) < 1e-6, "set_value works")
s.set_value(3.0)
ok(s.value() == 1.0, "clamps above 1")
s.set_value(-1.0)
ok(s.value() == 0.0, "clamps below 0")

# click without movement commits nothing (no accidental writes)
s.set_value(0.5)
QTest.mousePress(s, Qt.LeftButton, pos=QPoint(40, 11))
QTest.mouseRelease(s, Qt.LeftButton, pos=QPoint(40, 11))
ok(not committed, "plain click commits nothing")

# drag right increases and commits once on release
QTest.mousePress(s, Qt.LeftButton, pos=QPoint(40, 11))
QTest.mouseMove(s, QPoint(40 + 45, 11))     # +45px = +0.30
ok(s.dragging, "dragging state active")
QTest.mouseRelease(s, Qt.LeftButton, pos=QPoint(40 + 45, 11))
ok(len(committed) == 1, "drag commits exactly once")
ok(abs(committed[0] - 0.8) < 0.02,
   "45px right on 150px range = +0.30 (got %.3f)" % committed[0])
ok(not s.dragging, "dragging cleared on release")

# drag left decreases, clamped at 0
committed.clear()
QTest.mousePress(s, Qt.LeftButton, pos=QPoint(60, 11))
QTest.mouseMove(s, QPoint(60 - 200, 11))
QTest.mouseRelease(s, Qt.LeftButton, pos=QPoint(60 - 200, 11))
ok(committed and committed[0] == 0.0, "big left drag clamps to 0%%")

# typed value path (dialog monkeypatched)
QInputDialog.getInt = staticmethod(lambda *a, **k: (65, True))
committed.clear()
s._type_value()
ok(committed and abs(committed[0] - 0.65) < 1e-6,
   "typed 65%% commits 0.65")

# row: poll apply must not stomp an active drag
row = anim_layers.LayerRow()
row.show()
status_row = {"index": 0, "name": "L", "mute": False, "lock": False,
              "solo": False, "locked_reason": None, "blend_type": "COMBINE",
              "influence": 0.25, "animated_influence": False, "keys": 5}
row.apply(status_row, None)
ok(abs(row.influence.value() - 0.25) < 1e-6, "row apply sets slider value")
QTest.mousePress(row.influence, Qt.LeftButton, pos=QPoint(40, 11))
QTest.mouseMove(row.influence, QPoint(70, 11))
row.apply(status_row, None)                  # poll arrives mid-drag
ok(abs(row.influence.value() - 0.25) > 1e-3,
   "poll does not stomp the value mid-drag")
QTest.mouseRelease(row.influence, Qt.LeftButton, pos=QPoint(70, 11))
row.apply(status_row, None)
ok(abs(row.influence.value() - 0.25) < 1e-6,
   "poll updates again after the drag ends")

print("\n%d passed, %d failed" % (len(PASS), len(FAIL)), flush=True)
sys.exit(1 if FAIL else 0)
