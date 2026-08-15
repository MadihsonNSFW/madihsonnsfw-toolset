# Smooth scrolling, offscreen: per-pixel item views, the glide, and the two
# things that must NOT regress — the wheel never editing a setting, and a
# nested view not swallowing the wheel it cannot use.
#
#   python tests\app_scroll_test.py
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

sys.path.insert(0, os.path.join(_ROOT, "app"))

from PySide6.QtCore import QPoint, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QWheelEvent  # noqa: E402
from PySide6.QtWidgets import (QAbstractItemView, QApplication,  # noqa: E402
                               QComboBox, QLabel, QListWidget, QScrollArea,
                               QTableWidget, QVBoxLayout, QWidget)

import widgets  # noqa: E402
import theme  # noqa: E402

PASS, FAIL = [], []


def ok(cond, label):
    (PASS if cond else FAIL).append(label)
    print(("ok   " if cond else "FAIL ") + label, flush=True)


app = QApplication.instance() or QApplication([])
app.setStyleSheet(theme.QSS)
widgets.install_no_wheel(app)
smooth = widgets.install_smooth_scroll(app)


def wheel(widget, delta=-120, modifiers=Qt.NoModifier):
    ev = QWheelEvent(QPointF(5, 5), widget.mapToGlobal(QPoint(5, 5)),
                     QPoint(0, delta), QPoint(0, delta), Qt.NoButton,
                     modifiers, Qt.NoScrollPhase, False)
    app.sendEvent(widget, ev)


# ⚠ **THE SCROLLER IS ATTACHED PER SCROLL AREA SINCE 2026-08-15**, not to the
# QApplication — an application filter saw every event in the process and cost
# 408 ms of a window build (PERF_PLAN.md). In the app the walk is done by
# MainWindow and by `widgets.GuardedDialog`; a fixture built here has to ask
# for it the same way, which is exactly what a real top-level widget does.
def stage(widget):
    """Show a fixture the way the app brings a window up.

    ⚠ BOTH filters, via the one call the app uses. Attaching only the scroller
    left the wheel free to edit a combo again — the two guarantees are
    separate filters and forgetting either is silent.
    """
    widget.show()
    app.processEvents()
    widgets.attach_input_filters(widget)
    app.processEvents()
    return widget


def settle(ms=400):
    """Let the glide finish the way a real session would."""
    end = time.time() + ms / 1000.0
    while time.time() < end:
        app.processEvents()
        time.sleep(0.005)


# ------------------------------------------------------- install is once-only
ok(widgets.install_smooth_scroll(app) is smooth,
   "install: installing twice returns the same filter, not a second one "
   "double-scrolling every notch")

# ------------------------------------------------- per-pixel mode on item views
lst = QListWidget()
for i in range(300):
    lst.addItem("item %d" % i)
lst.resize(200, 150)
stage(lst)

ok(lst.verticalScrollMode() == QAbstractItemView.ScrollPerPixel,
   "pixels: a list view is switched to ScrollPerPixel on show - in ScrollPerItem "
   "the scrollbar range counts ITEMS, so pixel maths would jump whole rows")
ok(lst.horizontalScrollMode() == QAbstractItemView.ScrollPerPixel,
   "pixels: horizontally too")
ok(lst.verticalScrollBar().singleStep() >= widgets.SmoothScroller.LINE_PX,
   "pixels: singleStep is raised off 1px, or the arrow keys and scrollbar "
   "arrows crawl a pixel at a time")

table = QTableWidget(200, 3)
table.resize(300, 120)
stage(table)
ok(table.verticalScrollMode() == QAbstractItemView.ScrollPerPixel,
   "pixels: tables get it too (the picker's button list, the render queue)")

# ------------------------------------------------------------------ the glide
bar = lst.verticalScrollBar()
bar.setValue(0)
smooth.flush()
before = bar.value()
wheel(lst.viewport())
mid = bar.value()

ok(mid > before,
   "glide: the FIRST frame lands synchronously (%d -> %d) - a fully deferred "
   "scroll reads as input lag, and the wheel guarantee is asserted "
   "synchronously elsewhere" % (before, mid))

target = smooth._targets.get(bar)
ok(target is not None and target > mid,
   "glide: and the rest is still in flight (at %d, heading for %s)"
   % (mid, target))

settle()
ok(bar.value() > mid,
   "glide: it keeps going after the event returns (%d -> %d)"
   % (mid, bar.value()))
ok(not smooth._targets,
   "glide: and stops cleanly once it arrives, leaving no timer running")

# a notch moves a sensible distance, not a whole page and not two pixels
bar.setValue(0)
smooth.flush()
wheel(lst.viewport())
settle()
moved = bar.value()
expect = widgets.SmoothScroller.LINE_PX * widgets.SmoothScroller.LINES_PER_NOTCH
ok(abs(moved - expect) <= 2,
   "glide: one notch travels %d px as intended (got %d)" % (expect, moved))

# several notches in a burst accumulate instead of fighting each other
bar.setValue(0)
smooth.flush()
for _ in range(4):
    wheel(lst.viewport())
settle()
ok(abs(bar.value() - expect * 4) <= 4,
   "glide: a fast burst of 4 notches lands 4 notches away (%d, wanted ~%d) - "
   "each one extends the target rather than restarting it"
   % (bar.value(), expect * 4))

# scrolling back up works and is symmetric
wheel(lst.viewport(), 120)
settle()
ok(abs(bar.value() - expect * 3) <= 4,
   "glide: and scrolling back up is symmetric (%d, wanted ~%d)"
   % (bar.value(), expect * 3))

# ------------------------------------------- an exhausted view must not trap
outer = QScrollArea()
inner = QWidget()
lay = QVBoxLayout(inner)
tall = QLabel("\n".join("line %d" % i for i in range(200)))
nested = QListWidget()
for i in range(60):
    nested.addItem("nested %d" % i)
nested.setFixedHeight(80)
lay.addWidget(nested)
lay.addWidget(tall)
outer.setWidget(inner)
outer.setWidgetResizable(True)
outer.resize(300, 200)
stage(outer)
smooth.flush()

nested.verticalScrollBar().setValue(0)
outer.verticalScrollBar().setValue(0)
smooth.flush()
outer_before = outer.verticalScrollBar().value()
# Scrolling UP at the very top of the nested list: it has nowhere to go, so the
# event must fall through to the panel behind it.
wheel(nested.viewport(), 120)
settle()
ok(nested.verticalScrollBar().value() == 0,
   "nested: a list already at the top does not move")
ok(outer.verticalScrollBar().value() == outer_before,
   "nested: and the panel does not move either here, because it is at the top "
   "too - the point is the event was not silently eaten")

# With room to move, the nested list takes it and the panel stays put.
nested.verticalScrollBar().setValue(0)
outer.verticalScrollBar().setValue(30)
smooth.flush()
panel_before = outer.verticalScrollBar().value()
wheel(nested.viewport(), -120)
settle()
ok(nested.verticalScrollBar().value() > 0,
   "nested: with room to scroll, the list under the cursor takes the wheel")
ok(outer.verticalScrollBar().value() == panel_before,
   "nested: and the panel behind it does NOT also scroll (no double handling)")

# ------------------------------- the old guarantee still holds, now smoothly
host = QScrollArea()
host_inner = QWidget()
host_lay = QVBoxLayout(host_inner)
combo = QComboBox()
combo.addItems(["one", "two", "three"])
host_lay.addWidget(combo)
host_lay.addWidget(QLabel("\n".join("row %d" % i for i in range(200))))
host.setWidget(host_inner)
host.setWidgetResizable(True)
host.resize(300, 150)
stage(host)
smooth.flush()
host.verticalScrollBar().setValue(0)
smooth.flush()

start_index = combo.currentIndex()
before = host.verticalScrollBar().value()
wheel(combo)
ok(combo.currentIndex() == start_index,
   "guard: the wheel over a combo still does NOT change it")
settle()
ok(host.verticalScrollBar().value() > before,
   "guard: and the panel scrolled instead (%d -> %d), now gliding - the "
   "NoWheelFilter forwards to the viewport, where the scroller picks it up"
   % (before, host.verticalScrollBar().value()))

# ------------------------------------------------------------------- flush()
lst.verticalScrollBar().setValue(0)
smooth.flush()
wheel(lst.viewport())
ok(bool(smooth._targets), "flush: a glide is in flight")
smooth.flush()
ok(not smooth._targets and lst.verticalScrollBar().value() == expect,
   "flush: and flush() lands it immediately, for anything that needs the "
   "final position right now")

print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
for f in FAIL:
    print("  FAILED: " + f)
sys.exit(1 if FAIL else 0)
