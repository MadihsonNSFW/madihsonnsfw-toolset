# Node Editor tab, offscreen: the test graph (nodes, reroute dots, the
# multi-input socket), wires tracking moves, the curving amount end to end
# through devedit's "curving" field (incl. the 0-vs-None rule), selection
# (S1 accent + Blender-style brighter active), zoom clamping — and the
# checkbox half of the same batch: theme.QSS really ships a checkmark.
import os
import re
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

sys.path.insert(0, os.path.join(_ROOT, "app"))
sys.path.insert(0, os.path.join(_ROOT, "tests"))

import _branding  # noqa: E402

_FORBIDDEN, _STUDIED = _branding.words(_ROOT)

from PySide6.QtWidgets import QApplication, QLabel, QPushButton  # noqa: E402

PASS = []
FAIL = []


def ok(cond, label):
    (PASS if cond else FAIL).append(label)
    print(("ok   " if cond else "FAIL ") + label, flush=True)


app = QApplication.instance() or QApplication([])

# ------------------------------------------------- 1. the tick is real
import theme  # noqa: E402

blocks = re.findall(r"([^{}]+)\{([^}]*)\}", theme.QSS)
image_rules = [(sel, body) for sel, body in blocks if "image:" in body]
ok(any("QCheckBox::indicator:checked" in sel for sel, _b in image_rules),
   "theme: the checked checkbox indicator carries an image (the tick)")
ok(all("QRadioButton" not in sel for sel, _b in image_rules),
   "theme: radio buttons do NOT get the tick (they stay dots)")
ok(any("QTreeWidget::indicator" in sel for sel, _b in blocks),
   "theme: item-view tickboxes (trees/lists) are styled too")

ok(os.path.isfile(theme.CHECK_SVG), "theme: the tick svg exists on disk")
ok(os.path.isfile(theme.CHECK_DIM_SVG), "theme: the dimmed tick exists too")
with open(theme.CHECK_SVG, encoding="utf-8") as fh:
    ok('stroke="#ffffff"' in fh.read(),
       "theme: the tick really is white (style A)")
with open(theme.CHECK_DIM_SVG, encoding="utf-8") as fh:
    ok(('stroke="%s"' % theme.TEXT_DIM) in fh.read(),
       "theme: the disabled tick uses the dim text colour")
ok(theme.CHECK_SVG.replace("\\", "/") in theme.QSS,
   "theme: the QSS points at the tick with forward slashes (Qt url rule)")

# ------------------------------------------------- 2. the canvas graph
import bakenodes  # noqa: E402
import nodecanvas  # noqa: E402

# The TAB carries the BAKE graph now (2026-08-07, section 13). The canvas
# MECHANICS — wires, reroutes, multi-input, curving, selection — keep their
# original fixture on a standalone scene, because that graph exercises
# every mechanism (the bake graph has no multi-input, for instance).
tab = nodecanvas.NodeEditorTab()
scene = nodecanvas.NodeScene()
view = nodecanvas.NodeView(scene)
nodes = nodecanvas.build_test_graph(scene)

ok(view.property("_madi_wire_canvas") is True,
   "canvas: the view is marked for devedit's Edge smoothness menu")
ok(sorted(nodes) == ["apply", "blend", "in", "r1", "r2", "solver"],
   "graph: the mechanics fixture has its four nodes and two reroute dots")
ok(len(scene.wires) == 8, "graph: eight wires (got %d)" % len(scene.wires))
ok(nodes["blend"].inputs[0][2] is True,
   "graph: the Blend input is marked multi-input")
ok(len(scene.wires_into(nodes["blend"], 0)) == 2,
   "graph: the multi-input socket really carries TWO wires")

# wires follow whatever moves — a node...
w_solver_apply = scene.wires_into(nodes["apply"], 0)[0]
nodes["solver"].moveBy(37, -21)
start = w_solver_apply.path().pointAtPercent(0)
want = nodes["solver"].socket_pos("out", 0)
ok((start - want).manhattanLength() < 0.5,
   "wires: a wire's start tracks the node it leaves")

# ...and a reroute dot, on BOTH sides of it
w_in_r1 = [w for w in scene.wires if w.dst[0] is nodes["r1"]][0]
w_r1_out = [w for w in scene.wires
            if w.src[0] is nodes["r1"] and w.dst[0] is nodes["solver"]][0]
nodes["r1"].moveBy(-15, 33)
centre = nodes["r1"].socket_pos("in", 0)
ok((w_in_r1.path().pointAtPercent(1) - centre).manhattanLength() < 0.5
   and (w_r1_out.path().pointAtPercent(0) - centre).manhattanLength() < 0.5,
   "wires: both sides of a reroute dot follow it when it moves")

# ------------------------------------------------- 3. curving amounts
wire = scene.wires[0]


def chord():
    p1, p2 = wire.endpoints()
    return ((p2.x() - p1.x()) ** 2 + (p2.y() - p1.y()) ** 2) ** 0.5


scene.set_curving(0)
ok(wire.path().length() <= chord() * 1.01,
   "curving: 0 draws a dead-straight wire")
scene.set_curving(10)
ok(wire.path().length() > chord() * 1.03,
   "curving: 10 visibly bows the same wire")
scene.set_curving(99)
ok(scene.curving == 10, "curving: clamped to 10 at the top")
scene.set_curving(-3)
ok(scene.curving == 0, "curving: clamped to 0 at the bottom")

view.set_wire_curving(None)
ok(scene.curving == nodecanvas.DEFAULT_CURVING,
   "curving: None (no saved edit) means the DEFAULT, not zero")
view.set_wire_curving(0)
ok(scene.curving == 0 and view.wire_curving() == 0,
   "curving: a saved 0 stays 0 — the radius is-None rule holds here too")

# ------------------------------------------------- 4. selection styles
nodes["solver"].setSelected(True)
nodes["blend"].setSelected(True)   # blend becomes ACTIVE, solver stays selected
ok(scene.active_node is nodes["blend"],
   "select: the last-clicked node is the ACTIVE one")
ok(nodes["blend"]._border_pen().color() == nodecanvas.ACTIVE_OUTLINE,
   "select: the active node gets the brighter outline (Blender-style)")
ok(nodes["solver"]._border_pen().color() == nodecanvas.SELECT_OUTLINE,
   "select: other selected nodes get the accent outline (S1)")
scene.clearSelection()
scene.set_active_node(None)
ok(nodes["solver"]._border_pen().color() == nodecanvas.BODY_BORDER,
   "select: deselecting puts the quiet border back")

# ------------------------------------------------- 5. zoom + sockets
pcts = []
view.zoomChanged.connect(pcts.append)
ok(abs(view.apply_zoom(0.01) - view.ZOOM_MIN) < 1e-6,
   "zoom: clamped at the floor")
ok(abs(view.apply_zoom(99) - view.ZOOM_MAX) < 1e-6,
   "zoom: clamped at the ceiling")
ok(pcts and all(isinstance(p, int) for p in pcts),
   "zoom: the toolbar readout gets whole percentages")
view.apply_zoom(1.0)

hit = scene.socket_under(nodes["solver"].socket_pos("out", 0))
ok(hit is not None and hit[0] is nodes["solver"] and hit[1] == "out",
   "sockets: hit-testing finds the solver's output where it is drawn")
ok(scene.socket_under(nodes["solver"].socket_pos("out", 0), kind="in") is None,
   "sockets: the kind filter refuses the wrong side")

before = len(tab.scene.items())
tab.add_bake_node("Reroute")
tab.add_bake_node("Bake")
ok(len(tab.scene.items()) == before + 2,
   "toolbar: + Add node drops new bake nodes on the canvas")

# ------------------------------------------------- 6. devedit wiring
import devedit  # noqa: E402

tmp = tempfile.mkdtemp(prefix="madi_nodeed_")
devedit.STORE = devedit.EditStore(os.path.join(tmp, "dev_edits.json"))

ok("curving" in devedit.FIELDS, "devedit: 'curving' is a stored field")
target = devedit._widget_target(view)
ok("curving" in target.extras
   and "Edge smoothness" in target.extras["curving"][0],
   "devedit: the canvas offers Edge smoothness…")
ok("curving" not in devedit._widget_target(QPushButton("x")).extras,
   "devedit: a plain button does NOT offer it")

devedit.STORE.put(target.key, target.original, target.kind, target.where,
                  curving=0)
ok(devedit.STORE.get(target.key).get("curving") == 0,
   "devedit: a curving of 0 survives the store (is-None, not falsiness)")
view.set_wire_curving(5)
target.push()
ok(view.wire_curving() == 0,
   "devedit: push() applies the saved 0 to the canvas")
devedit.STORE.drop(target.key)
target.push()
ok(view.wire_curving() == nodecanvas.DEFAULT_CURVING,
   "devedit: dropping the record puts the default curve back")
ok(callable(getattr(devedit, "pick_curving", None)),
   "devedit: the Edge smoothness dialog exists")
src = open(os.path.join(os.path.dirname(devedit.__file__),
                        "devedit.py"), encoding="utf-8").read()
ok("act_curving" in src and "pick_curving(target, window)" in src,
   "devedit: the menu really routes to pick_curving")

# ------------------------------------------------- 7. the FREE tab
# ⚠ FREED 2026-08-08 (Marty: "Node editor should be free (But in the future
# we will have premium nodes)"). Freeing a tab here is THREE edits and each
# one can be forgotten alone, so all three are pinned: out of GATED, out of
# GATED_ATTRS — where a leftover name would blank a LIVE tab whenever a lock
# preview is built — and into FREE_TOOLS so it is constructed at startup.
import main as mainmod  # noqa: E402

gated = {key: title for key, title, _b in mainmod.MainWindow.GATED}
free = {key: title for key, title in mainmod.MainWindow.FREE_TOOLS}
ok("nodeeditor" not in gated,
   "main: the Node Editor is NOT gated any more (got %r)" % (sorted(gated),))
ok(free.get("nodeeditor") == "Node Editor",
   "main: it is a FREE_TOOLS tab, built unconditionally at startup")
# ⚠ Was "last of the free block" until MadiRef joined FREE_TOOLS on
# 2026-08-11. What actually matters is that it sits BESIDE Node Setup — being
# last was only ever how that was true at the time, and asserting the position
# again would just break on the next free tab.
_free_keys = [k for k, _t in mainmod.MainWindow.FREE_TOOLS]
ok(_free_keys.index("nodeeditor") == _free_keys.index("node_setup") + 1,
   "main: it sits directly beside Node Setup in the free block")
ok("nodeeditor" not in mainmod.MainWindow.GATED_ATTRS,
   "main: and OUT of GATED_ATTRS — a lock preview must never blank a live tab")
# ⚠ This list has been rewritten in BOTH directions — tabs freed out of it
# (2026-08-06, 2026-08-08), MadiRef gated back in (2026-08-11), and then
# EVERYTHING freed on 2026-08-14 (the pivot: premium packs are the paid
# thing). Still pinned whole: a tab quietly re-entering it would re-lock a
# tool under Marty's hand.
ok([k for k, _t, _b in mainmod.MainWindow.GATED] == [],
   "main: GATED is empty — every tab is free since 2026-08-14")
ok(callable(getattr(mainmod.MainWindow, "_build_nodeeditor", None)),
   "main: _build_nodeeditor exists")
ok(all(not any(w in blurb.lower() for w in _FORBIDDEN)
       for _k, _t, blurb in mainmod.MainWindow.GATED),
   "branding: no gated blurb mentions what must never ship")

# ------------------------------------------------- 8. grid re-level + ctrl zoom
from PySide6.QtCore import QEvent, QRectF, Qt  # noqa: E402
from PySide6.QtGui import QImage, QKeyEvent, QPainter  # noqa: E402

ok(nodecanvas.grid_spacing(1.0) == (22, 110),
   "grid: at 100% a cell is 22 with a major line every 110")
ok(nodecanvas.grid_spacing(0.15) == (110, 550),
   "grid: at the zoom floor it promotes instead of vanishing")
minor, major = nodecanvas.grid_spacing(3.0)
ok((minor, major) == (22, 110), "grid: zoomed in it stays at base spacing")
minor, major = nodecanvas.grid_spacing(0.01)
ok(minor * 0.01 >= nodecanvas.GRID_MIN_PX,
   "grid: cells stay readable at absurd zoom-out")
ok(major == minor * nodecanvas.GRID_MAJOR,
   "grid: the major line is always GRID_MAJOR cells")
ok(nodecanvas.grid_spacing(0) == (22, 110),
   "grid: zoom 0 answers base spacing rather than looping")

img = QImage(220, 160, QImage.Format_ARGB32)
img.fill(0)
painter = QPainter(img)
scene.render(painter, QRectF(0, 0, 220, 160), QRectF(-10, -10, 220, 160))
painter.end()
ok(img.pixelColor(2, 2).alpha() == 255,
   "grid: drawBackground really paints (no blank canvas)")

view.apply_zoom(1.0)
view.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_Plus,
                             Qt.KeyboardModifier.ControlModifier))
ok(abs(view.zoom_factor() - view.ZOOM_STEP) < 1e-6,
   "zoom: ctrl-plus steps in")
minus = QKeyEvent(QEvent.KeyPress, Qt.Key_Minus,
                  Qt.KeyboardModifier.ControlModifier)
view.keyPressEvent(minus)
view.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_Minus,
                             Qt.KeyboardModifier.ControlModifier))
ok(view.zoom_factor() < 1.0, "zoom: ctrl-minus steps back out")
view.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_Equal,
                             Qt.KeyboardModifier.ControlModifier))
ok(view.zoom_factor() > 1 / view.ZOOM_STEP,
   "zoom: ctrl-equals works for keyboards without a numpad plus")
before_plain = view.zoom_factor()
view.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_Plus,
                             Qt.KeyboardModifier.NoModifier))
ok(abs(view.zoom_factor() - before_plain) < 1e-9,
   "zoom: a plain keypress without ctrl does not zoom")

# ------------------------------------------------- 9. the tab takes focus
# ⚠ Drive a REAL show, not showEvent() by hand: the bug was that the canvas
# never held focus, and only a real show proves the handler is reached.
tab = nodecanvas.NodeEditorTab()
tab.resize(600, 400)
tab.show()
app.processEvents()
ok(tab.canvas.hasFocus(),
   "focus: the canvas takes focus when the tab is shown (ctrl +/- works cold)")
ok(tab.canvas.focusPolicy() == Qt.StrongFocus,
   "focus: the view accepts focus at all")
before_zoom = tab.canvas.zoom_factor()
tab.canvas.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_Plus,
                                   Qt.KeyboardModifier.ControlModifier))
ok(tab.canvas.zoom_factor() > before_zoom,
   "focus: ctrl-plus zooms a freshly shown tab without a click first")
# ⚠ Read the STORED text, not `text()`. The hint is a widgets.ElidedLabel
# now (it used to hold the whole window open at 2194 px), and `text()` is
# whatever fits the current width — which offscreen is usually nothing.
hint_texts = [w.full_text() if hasattr(w, "full_text") else w.text()
              for w in tab.findChildren(QLabel)]
ok(any("Ctrl" in t for t in hint_texts),
   "hint: the toolbar tells you Ctrl +/- zooms")
tab.close()

# --------------------------------------------- 10. the REAL paint + wheel paths
# ⚠ Section 8 proved drawBackground works when CALLED — via scene.render,
# which bypasses the view. The live app paints through QGraphicsView, which
# SKIPS the scene's drawBackground entirely if the view has its own
# backgroundBrush — the grid was invisible in the app for a full day while
# every offscreen proof showed it. Same story for the wheel: wheelEvent()
# called directly works; the running app routes wheels through the
# application-wide SmoothScroller first, which consumed the gesture to
# glide the canvas scrollbars. Both checks below go through the real paths.
import math  # noqa: E402

from PySide6.QtCore import QPoint, QPointF  # noqa: E402
from PySide6.QtGui import QWheelEvent  # noqa: E402
from PySide6.QtWidgets import QGraphicsScene, QGraphicsView  # noqa: E402

import widgets  # noqa: E402

tab2 = nodecanvas.NodeEditorTab()
tab2.resize(640, 420)
tab2.show()
app.processEvents()
canvas = tab2.canvas

ok(canvas.backgroundBrush().style() == Qt.NoBrush,
   "grid: the VIEW has no background brush (a brush would mute the scene's "
   "drawBackground — the bug that hid the grid in the live app)")

canvas.centerOn(1500, 1500)
app.processEvents()
img = canvas.viewport().grab().toImage()
tl = canvas.mapToScene(QPoint(0, 0))
mx = math.ceil((tl.x() + 20) / 110.0) * 110
line_vp = canvas.mapFromScene(QPointF(mx, tl.y() + 60))
bg = theme.BG.lower()
found_grid = any(
    img.pixelColor(line_vp.x() + dx, line_vp.y() + dy).name().lower() != bg
    for dx in (-2, -1, 0, 1, 2) for dy in range(0, 40, 7)
    if 0 <= line_vp.x() + dx < img.width()
    and 0 <= line_vp.y() + dy < img.height())
ok(found_grid,
   "grid: a major line actually reaches the SCREEN through the view")
sy = math.ceil((tl.y() + 40) / 22.0) * 22
mid_vp = canvas.mapFromScene(QPointF(mx + 11, sy + 11))
ok(img.pixelColor(mid_vp.x(), mid_vp.y()).name().lower() == bg,
   "grid: mid-cell is still clean background (not a smeared fill)")

widgets.install_no_wheel(app)
widgets.install_smooth_scroll(app)


def send_wheel(widget, dy, mods):
    pos = QPointF(widget.width() / 2.0, widget.height() / 2.0)
    gpos = QPointF(widget.mapToGlobal(QPoint(int(pos.x()), int(pos.y()))))
    QApplication.sendEvent(widget, QWheelEvent(
        pos, gpos, QPoint(0, 0), QPoint(0, dy), Qt.NoButton, mods,
        Qt.ScrollPhase.NoScrollPhase, False))


from PySide6.QtWidgets import QApplication  # noqa: E402

canvas.apply_zoom(1.0)
send_wheel(canvas.viewport(), 120, Qt.KeyboardModifier.NoModifier)
ok(canvas.zoom_factor() > 1.0,
   "wheel: zooms with the app's REAL filters installed (SmoothScroller "
   "exempts the canvas)")
z = canvas.zoom_factor()
send_wheel(canvas.viewport(), 120, Qt.KeyboardModifier.ControlModifier)
ok(canvas.zoom_factor() > z, "wheel: ctrl+wheel zooms too")

plain_scene = QGraphicsScene()
plain_scene.setSceneRect(0, 0, 3000, 3000)
plain = QGraphicsView(plain_scene)
plain.resize(300, 200)
plain.show()
app.processEvents()
pbar = plain.verticalScrollBar()
pbar.setValue((pbar.minimum() + pbar.maximum()) // 2)
pv = pbar.value()
send_wheel(plain.viewport(), 120, Qt.KeyboardModifier.NoModifier)
ok(pbar.value() != pv,
   "wheel: an UNMARKED graphics view still belongs to the scroller "
   "(the exemption is the marker, not the widget class)")
plain.close()
tab2.close()

# ------------------------------------------------- 11. the cut tool
# Blender's Cut Links (Marty, 2026-08-07): Ctrl+LMB drag draws a dashed
# line, every wire it crosses dies on release. The drags go to the REAL
# viewport (the section-10 lesson) and hit an isolated pair placed far from
# the test graph, so a cut can only sever what it aims at. Curving is 0 for
# the section so every noodle is a predictable straight segment.
from PySide6.QtGui import QMouseEvent  # noqa: E402

tab3 = nodecanvas.NodeEditorTab()
tab3.resize(640, 420)
tab3.show()
app.processEvents()
cscene, cview = tab3.scene, tab3.canvas
cscene.set_curving(0)

pair_a = nodecanvas.NodeItem("Cut src", theme.TYPE_COLORS["pose"],
                             outputs=[("Out", nodecanvas.COL_GEO)])
pair_a.setPos(600, 500)
pair_b = nodecanvas.NodeItem("Cut dst", theme.TYPE_COLORS["set"],
                             inputs=[("In", nodecanvas.COL_GEO, True)])
pair_b.setPos(950, 500)
pair_c = nodecanvas.NodeItem("Cut src 2", theme.TYPE_COLORS["anim"],
                             outputs=[("Out", nodecanvas.COL_GEO)])
pair_c.setPos(600, 620)
for it in (pair_a, pair_b, pair_c):
    cscene.addItem(it)
# wire1 runs dead-horizontal a->b; wire2 slopes up c->b into the SAME
# multi-input socket, sitting ~60 scene px below wire1 at the cut column.
wire1 = cscene.add_wire((pair_a, "out", 0), (pair_b, "in", 0),
                        nodecanvas.COL_GEO)
wire2 = cscene.add_wire((pair_c, "out", 0), (pair_b, "in", 0),
                        nodecanvas.COL_GEO)
wire_y = pair_a.socket_pos("out", 0).y()

# unit level first: the stroked-fill crossing test itself
from PySide6.QtGui import QPainterPath  # noqa: E402

probe = QPainterPath(QPointF(860, wire_y - 30))
probe.lineTo(QPointF(860, wire_y + 30))
got = cscene.wires_crossing(probe)
ok(got == [wire1],
   "cut: a vertical probe through wire1 finds it — and ONLY it (the X-cross "
   "case raw QPainterPath.intersects gets wrong without stroking)")
far = QPainterPath(QPointF(860, wire_y - 120))
far.lineTo(QPointF(860, wire_y - 60))
ok(cscene.wires_crossing(far) == [],
   "cut: a probe that crosses nothing reports nothing")

baseline = len(cscene.wires)


def send_mouse(etype, scene_pt, button, buttons, mods):
    vp = cview.mapFromScene(scene_pt)
    vpos = QPointF(vp)
    gpos = QPointF(cview.viewport().mapToGlobal(vp))
    QApplication.sendEvent(cview.viewport(), QMouseEvent(
        etype, vpos, gpos, button, buttons, mods))


cview.centerOn(860, wire_y)
app.processEvents()

# the real drag: press above wire1, drag through it, release below —
# stopping well short of wire2's height
send_mouse(QEvent.MouseButtonPress, QPointF(860, wire_y - 35),
           Qt.LeftButton, Qt.LeftButton, Qt.KeyboardModifier.ControlModifier)
send_mouse(QEvent.MouseMove, QPointF(860, wire_y + 5),
           Qt.NoButton, Qt.LeftButton, Qt.KeyboardModifier.ControlModifier)
ok(any(isinstance(i, nodecanvas._CutLine) for i in cscene.items()),
   "cut: the dashed trail is on the canvas while the drag is in flight")
send_mouse(QEvent.MouseMove, QPointF(860, wire_y + 35),
           Qt.NoButton, Qt.LeftButton, Qt.KeyboardModifier.ControlModifier)
send_mouse(QEvent.MouseButtonRelease, QPointF(860, wire_y + 35),
           Qt.LeftButton, Qt.NoButton, Qt.KeyboardModifier.ControlModifier)

ok(wire1 not in cscene.wires and len(cscene.wires) == baseline - 1,
   "cut: a real Ctrl+drag through wire1 severs it")
ok(wire2 in cscene.wires
   and cscene.wires_into(pair_b, 0) == [wire2],
   "cut: its multi-input partner survives — one wire cut, not the socket")
ok(not any(isinstance(i, nodecanvas._CutLine) for i in cscene.items()),
   "cut: the dashed trail is gone after release")
bake_wires = [w for w in cscene.wires
              if w.src[0] is tab3.nodes["bake"]
              or w.dst[0] is tab3.nodes["bake"]]
ok(len(bake_wires) == 2,
   "cut: the bake graph across the canvas never felt it")

# a Ctrl+CLICK is not a cut, even dead on the wire
wire1b = cscene.add_wire((pair_a, "out", 0), (pair_b, "in", 0),
                         nodecanvas.COL_GEO)
send_mouse(QEvent.MouseButtonPress, QPointF(860, wire_y),
           Qt.LeftButton, Qt.LeftButton, Qt.KeyboardModifier.ControlModifier)
send_mouse(QEvent.MouseButtonRelease, QPointF(860, wire_y),
           Qt.LeftButton, Qt.NoButton, Qt.KeyboardModifier.ControlModifier)
ok(wire1b in cscene.wires,
   "cut: a Ctrl+click without a drag cuts nothing (Blender needs a drag too)")

# and without Ctrl the same drag is the rubber band, not a cut
send_mouse(QEvent.MouseButtonPress, QPointF(860, wire_y - 35),
           Qt.LeftButton, Qt.LeftButton, Qt.KeyboardModifier.NoModifier)
send_mouse(QEvent.MouseMove, QPointF(860, wire_y + 35),
           Qt.NoButton, Qt.LeftButton, Qt.KeyboardModifier.NoModifier)
send_mouse(QEvent.MouseButtonRelease, QPointF(860, wire_y + 35),
           Qt.LeftButton, Qt.NoButton, Qt.KeyboardModifier.NoModifier)
ok(wire1b in cscene.wires,
   "cut: the same drag WITHOUT Ctrl leaves every wire alone")

ok(any("cuts" in (lbl.full_text() if hasattr(lbl, "full_text")
                  else lbl.text())
       for lbl in tab3.findChildren(QLabel)),
   "hint: the toolbar mentions the cut gesture")

# ------------------------------------------------- 12. smooth panning
# Marty, 2026-08-07: "when panning in node area it doesn't feel smooth".
# The cause: scrollbars take ints and every per-event delta was truncated —
# a slow drag at a high mouse poll rate hands out sub-pixel deltas, so
# int() threw away up to a pixel PER EVENT and the canvas crawled behind
# the hand. The fix carries the fraction. Sub-pixel positions can't survive
# mapFromScene (it returns QPoint), so this section speaks raw view coords.


def send_pan(etype, vpos, button, buttons):
    gpos = QPointF(cview.viewport().mapToGlobal(vpos.toPoint()))
    QApplication.sendEvent(cview.viewport(), QMouseEvent(
        etype, vpos, gpos, button, buttons, Qt.KeyboardModifier.NoModifier))


hbar = cview.horizontalScrollBar()
hbar.setValue((hbar.minimum() + hbar.maximum()) // 2)
start_val = hbar.value()

# forty 0.6-px steps = a 24-px drag; the old int-per-event code applied ZERO
x = 320.0
send_pan(QEvent.MouseButtonPress, QPointF(x, 210), Qt.MiddleButton,
         Qt.MiddleButton)
for _ in range(40):
    x += 0.6
    send_pan(QEvent.MouseMove, QPointF(x, 210), Qt.NoButton, Qt.MiddleButton)
send_pan(QEvent.MouseButtonRelease, QPointF(x, 210), Qt.MiddleButton,
         Qt.NoButton)
moved = start_val - hbar.value()
ok(abs(moved - 24) <= 1,
   "pan: forty sub-pixel deltas add up to the full 24-px drag "
   "(truncation applied 0 of it — got %d)" % moved)

# integer drags stay exact: no drift added by the carry
start_val = hbar.value()
send_pan(QEvent.MouseButtonPress, QPointF(300, 210), Qt.MiddleButton,
         Qt.MiddleButton)
for step in range(1, 11):
    send_pan(QEvent.MouseMove, QPointF(300 + 3 * step, 210), Qt.NoButton,
             Qt.MiddleButton)
send_pan(QEvent.MouseButtonRelease, QPointF(330, 210), Qt.MiddleButton,
         Qt.NoButton)
ok(start_val - hbar.value() == 30,
   "pan: ten exact 3-px steps move exactly 30 (the carry never drifts)")

# a fresh pan starts clean — no leftover fraction from the last one
cview._pan_carry = QPointF(0.7, 0.3)
send_pan(QEvent.MouseButtonPress, QPointF(200, 200), Qt.MiddleButton,
         Qt.MiddleButton)
ok(cview._pan_carry == QPointF(0, 0),
   "pan: pressing middle-mouse clears any carried fraction")
send_pan(QEvent.MouseButtonRelease, QPointF(200, 200), Qt.MiddleButton,
         Qt.NoButton)
tab3.close()

# ------------------------------------------------- 13. the bake node set
# The first REAL nodes (2026-08-07): Bake (target) -> Bake settings
# (12-type dropdown + resolution + button) -> Output image, resolved by
# WALKING THE WIRES from the pressed button. A fake bridge drives the whole
# click-to-result flow offscreen; the real engine is texbake_test.py's job.
import time as _time  # noqa: E402

from PySide6.QtGui import QImage as _QImage  # noqa: E402

fake_png = os.path.join(tempfile.mkdtemp(prefix="madi_bakeui_"), "done.png")
pic = _QImage(8, 8, _QImage.Format_ARGB32)
pic.fill(0xFFAA3311)
pic.save(fake_png)

# An unset Output path resolves to default_bake_dir()\<material>_baked since
# 2026-08-07 — point the default at a sandbox so the suite never writes the
# real app\baked folder.
BAKE_TMP = tempfile.mkdtemp(prefix="madi_bakedir_")
bakenodes.default_bake_dir = lambda: BAKE_TMP


class FakeBridge:
    def __init__(self, reason=None, echo_options=True):
        self.reason = reason
        self.calls = []
        self.options = {}
        # An add-on older than 0.25.0 takes the option keys and drops them
        # on the floor; only the missing echo gives it away.
        self.echo_options = echo_options

    def feature_reason(self, feature):
        return self.reason

    def supports(self, cmd):
        return True

    def list_materials(self):
        return {"materials": [
            {"name": "MatA", "users": 1, "objects": ["Cube"],
             "has_nodes": True},
            {"name": "MatB", "users": 0, "objects": [], "has_nodes": True}]}

    def bake_texture(self, material, bake_type, width, height,
                     out_path=None, **kw):
        self.calls.append((material, bake_type, width, height, out_path))
        self.options = dict(kw)
        to_image = kw.get("target", "IMAGE_TEXTURES") == "IMAGE_TEXTURES"
        reply = {"material": material, "bake_type": bake_type,
                 "width": width, "height": height, "samples": 4,
                 "device": "GPU", "path": fake_png if to_image else None,
                 "target": kw.get("target", "IMAGE_TEXTURES"),
                 "color_attribute": None if to_image else "Col",
                 "seconds": 0.12}
        if self.echo_options:
            reply["options"] = {
                "samples_auto": kw.get("samples") is None,
                "pass_filter": kw.get("pass_filter"),
                "view_from": kw.get("view_from"),
                "normal_space": kw.get("normal_space"),
                "normal_swizzle": kw.get("normal_swizzle"),
                "margin": kw.get("margin"),
                "margin_type": kw.get("margin_type"),
                # the 0.29.0 native echoes — `target` doubles as the
                # "this add-on bakes natively" marker the app reads
                "use_clear": kw.get("use_clear"),
                "target": kw.get("target"),
                "selected_to_active": {
                    "on": kw.get("use_selected_to_active"),
                    "cage": kw.get("use_cage"),
                    "cage_object": kw.get("cage_object"),
                    "cage_extrusion": kw.get("cage_extrusion"),
                    "max_ray_distance": kw.get("max_ray_distance")}}
        return reply


class FakeWindow:
    def __init__(self):
        self.begun = []
        self.ended = 0

    def begin_capture(self, label, verb="capturing"):
        self.begun.append((label, verb))

    def end_capture(self):
        self.ended += 1


def wait_bake(tab, seconds=5.0):
    t0 = _time.time()
    while tab.bake_running() and _time.time() - t0 < seconds:
        app.processEvents()
    app.processEvents()


fake = FakeBridge()
fwin = FakeWindow()
tab4 = nodecanvas.NodeEditorTab(fake, fwin)
g = tab4.nodes
ok(sorted(g) == ["bake", "out", "shader"],
   "bake: the tab carries the three-node bake graph (the Image texture "
   "node was retired 2026-08-07 — its resolution lives on Bake settings)")
ok(len(tab4.scene.wires) == 2, "bake: pre-wired Bake -> settings -> out")
ok([bakenodes.BAKE_ENUM[t] for t in bakenodes.BAKE_TYPES] == [
    "COMBINED", "AO", "SHADOW", "POSITION", "NORMAL", "UV", "ROUGHNESS",
    "EMIT", "ENVIRONMENT", "DIFFUSE", "GLOSSY", "TRANSMISSION"],
   "bake: the dropdown carries all twelve types, mapped to cycles' enum")

# the wire walk, including through a reroute dot
ok(bakenodes.upstream_node(tab4.scene, g["bake"], 0,
                           bakenodes.BakeTargetNode) is g["shader"],
   "walk: the settings node finds its Bake (target) feed")
w_shader = tab4.scene.wires_into(g["bake"], 0)[0]
tab4.scene.remove_wire(w_shader)
rr = nodecanvas.RerouteItem(bakenodes.COL_MATERIAL)
rr.setPos(-250, -100)
tab4.scene.addItem(rr)
tab4.scene.add_wire((g["shader"], "out", 0), (rr, "in", 0),
                    bakenodes.COL_MATERIAL)
tab4.scene.add_wire((rr, "out", 0), (g["bake"], "in", 0),
                    bakenodes.COL_MATERIAL)
ok(bakenodes.upstream_node(tab4.scene, g["bake"], 0,
                           bakenodes.BakeTargetNode) is g["shader"],
   "walk: the target is still found THROUGH a reroute dot")
ok(bakenodes.downstream_node(tab4.scene, g["bake"],
                             bakenodes.OutputImageNode) is g["out"],
   "walk: the output node is found downstream")

# clickable rows: the pill zone answers, the label zone does not
pill = QPointF(g["bake"].w * 0.75, nodecanvas.HEADER_H
               + nodecanvas.ROW_GAP + nodecanvas.ROW_H * 0.5)
label_zone = QPointF(g["bake"].w * 0.2, pill.y())
ok(g["bake"]._row_index_at(pill) == 0,
   "rows: a click on the value pill hits row 0")
ok(g["bake"]._row_index_at(label_zone) is None,
   "rows: the label half is NOT a field (it drags the node)")

g["bake"].set_size(2048, 2048)
ok(dict(g["bake"].rows)["Resolution"] == "2048 × 2048"
   and g["bake"].width_px == 2048,
   "rows: the resolution row (moved off the Image texture node) shows the "
   "size it will bake")
g["bake"].set_size(2048, 1024)
ok(dict(g["bake"].rows)["Resolution"] == "2048 × 1024",
   "rows: a non-square custom size is still reachable")

# refusals, in order: no bridge -> gated -> unpicked material -> cut wire
tab_none = nodecanvas.NodeEditorTab(None, None)
tab_none.run_bake(tab_none.nodes["bake"])
ok("bridge" in tab_none.status_label.text().lower(),
   "gate: no bridge says so instead of crashing")
tab_gated = nodecanvas.NodeEditorTab(FakeBridge(reason="needs add-on 0.24.0"),
                                     None)
tab_gated.nodes["shader"].set_material("MatA")
tab_gated.run_bake(tab_gated.nodes["bake"])
ok("0.24.0" in tab_gated.status_label.text(),
   "gate: an old add-on puts the FEATURE_REQUIREMENTS reason on the status")
ok(not tab_gated.bridge.calls, "gate: and no request was fired")

tab4.run_bake(g["bake"])
ok("pick a material" in tab4.status_label.text().lower(),
   "gate: an unpicked material is named before any request")

ok(tab4.material_names() == ["MatA", "MatB"],
   "list: the shader pill reads the live material list")

# the real run: bind, thread, capture bracket, preview
g["shader"].set_material("MatA")
g["bake"].set_type("Roughness")
tab4.run_bake(g["bake"])
ok(tab4.bake_running(), "run: the bake goes to a worker, not the GUI thread")
wait_bake(tab4)
ok(not tab4.bake_running(), "run: and it comes back")
auto_path = os.path.join(BAKE_TMP, "MatA_baked")
ok(fake.calls == [("MatA", "ROUGHNESS", 2048, 1024, auto_path)],
   "run: the request carries the bound graph values, with the unset path "
   "resolved to the toolset's baked folder (got %r)" % fake.calls)
ok(fwin.begun == [("MatA", "baking")] and fwin.ended == 1,
   "run: begin_capture/end_capture bracket the bake")
ok(g["out"].preview is not None,
   "run: the output node shows the baked image")
ok("done.png" in g["out"].rows[0][1],
   "run: the output node names the file it holds")
ok("baked" in tab4.status_label.text().lower(),
   "run: the status line reports the result")

# an add-on warning (empty map, no lights, wrong output…) reaches the
# status line and marks the Output node — never a silent "Baked ✓"
fake.warning = ("the map baked EMPTY: the scene has no enabled lights and "
                "the world is nearly black — a COMBINED bake renders the "
                "surface LIT")
orig_bake = fake.bake_texture

def _warn_bake(*a, **kw):
    r = orig_bake(*a, **kw)
    r["warning"] = fake.warning
    return r

fake.bake_texture = _warn_bake
tab4.run_bake(g["bake"])
wait_bake(tab4)
ok("no enabled lights" in tab4.status_label.text(),
   "warn: the add-on's empty-bake reason lands on the status line")
ok(g["out"].rows[g["out"].status_row][1].startswith("⚠"),
   "warn: the Output node's note carries the ⚠")
fake.bake_texture = orig_bake

# severed wires refuse with a direction, not a traceback — and neither end
# of the chain is optional any more (Marty, 2026-08-07)
out_wire = [w for w in tab4.scene.wires if w.dst[0] is g["out"]][0]
tab4.scene.remove_wire(out_wire)
tab4.run_bake(g["bake"])
ok("output image" in tab4.status_label.text().lower()
   and "connect" in tab4.status_label.text().lower(),
   "gate: with no Output image node downstream the bake refuses (got %r)"
   % tab4.status_label.text())
tab4.scene.add_wire((g["bake"], "out", 0), (g["out"], "in", 0),
                    bakenodes.COL_BAKED)
for w in list(tab4.scene.wires_into(g["bake"], 0)):
    tab4.scene.remove_wire(w)
tab4.run_bake(g["bake"])
ok("bulk bake" in tab4.status_label.text().lower(),
   "gate: a cut source wire names both nodes that could feed it (got %r)"
   % tab4.status_label.text())
tab4.scene.add_wire((g["shader"], "out", 0), (g["bake"], "in", 0),
                    bakenodes.COL_MATERIAL)

# Del removes a selected node and takes its wires with it
extra = tab4.add_bake_node("Bake")
tab4.scene.add_wire((extra, "out", 0), (g["bake"], "in", 0),
                    bakenodes.COL_MATERIAL)
wires_before = len(tab4.scene.wires)
items_before = len(tab4.scene.items())
tab4.scene.clearSelection()
extra.setSelected(True)
tab4.canvas.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_Delete,
                                    Qt.KeyboardModifier.NoModifier))
ok(len(tab4.scene.items()) == items_before - 2
   and len(tab4.scene.wires) == wires_before - 1,
   "delete: Del removes the selected node AND its wires (a wire is a "
   "scene item too, so two items go)")

# ------------------------------------------- 14. the bake OPTIONS (0.25.0)
# Marty, 2026-08-07: "add all the bake options we have in blender, and
# depending on what is chosen -- for example if Diffuse is chosen you have
# these three". The rows are therefore rebuilt per type, and which options
# a type offers is BLENDER's rule (cycles/ui.py), mirrored on both sides.
import ast  # noqa: E402

TEXBAKE = os.path.join(_ROOT,
                       "blender_addon", "madi_anim_library", "texbake.py")
with open(TEXBAKE, encoding="utf-8") as fh:
    addon_tree = ast.parse(fh.read())
addon = {}
for node in addon_tree.body:
    if isinstance(node, ast.Assign) and len(node.targets) == 1 \
            and isinstance(node.targets[0], ast.Name):
        try:
            addon[node.targets[0].id] = ast.literal_eval(node.value)
        except ValueError:
            pass

ok(addon["INFLUENCE"] == bakenodes.INFLUENCE,
   "mirror: the app's INFLUENCE map matches the add-on's, type for type")
ok(addon["NO_VIEW_FROM"] == bakenodes.NO_VIEW_FROM,
   "mirror: the six types with no View From match the add-on's")
ok(addon["DATA_TYPES"] == bakenodes.DATA_TYPES,
   "mirror: the data-map set (Non-Color float files) matches the add-on's")
ok(addon["MAX_SAMPLES"] == bakenodes.MAX_SAMPLES,
   "mirror: the sample cap matches the add-on's")
ok(addon["TARGETS"] == tuple(e for _l, e in bakenodes.TARGET_LABELS),
   "mirror: the two bake targets match the add-on's, in order")
ok(set(bakenodes.PASS_LABELS) == set(addon["PASS_FLAGS"]),
   "mirror: every contribution flag the add-on knows has a UI label")

tab5 = nodecanvas.NodeEditorTab(FakeBridge(), FakeWindow())
bake = tab5.nodes["bake"]


def row_map(node):
    return {label: value for label, value in node.rows}


def labels(node):
    return [label for label, _v in node.rows]


ok(row_map(bake)["Type"] == "Combined" and "Samples" in row_map(bake),
   "rows: Type and Samples are always there")
combined = labels(bake)
ok(combined.index("Lighting") < combined.index("Direct")
   < combined.index("Contributions") < combined.index("Diffuse"),
   "combined: Lighting (direct/indirect) then Contributions, Blender's order")
ok([f for f in ("Diffuse", "Glossy", "Transmission", "Emit")
    if f in combined] == ["Diffuse", "Glossy", "Transmission", "Emit"],
   "combined: all four contributions are offered")

bake.set_type("Diffuse")
diffuse = labels(bake)
ok([f for f in ("Direct", "Indirect", "Color") if f in diffuse]
   == ["Direct", "Indirect", "Color"],
   "diffuse: exactly the three from Marty's screenshot")
ok("Diffuse" not in diffuse and "Emit" not in diffuse,
   "diffuse: and NOT Combined's four contributions")
ok("View From" in diffuse, "diffuse: View From is offered (Blender does)")

bake.set_type("Normal")
normal = row_map(bake)
ok(normal["Space"] == "Tangent" and normal["Swizzle R"] == "+X"
   and normal["G"] == "+Y" and normal["B"] == "+Z",
   "normal: space + the R/G/B swizzle, at Blender's own defaults")
ok("View From" not in normal,
   "normal: View From is NOT offered (Blender greys it for this type)")
ok("Direct" not in normal, "normal: and no contributions")

bake.set_type("Roughness")
rough = labels(bake)
ok(rough == ["Type", "Selected to Active", "Output", "Target",
             "Clear Image", "Margin type", "Margin", "Resolution",
             "Samples"],
   "roughness: a type with no Influence panel shows the panel's constant "
   "blocks, in the panel's order (got %r)" % rough)
ok(bake.pass_filter() is None,
   "roughness: and sends no pass filter at all (got %r)" % bake.pass_filter())

for label in ("Ambient Occlusion", "Position", "UV", "Environment"):
    bake.set_type(label)
    if "View From" in labels(bake):
        ok(False, "view: %s must not offer View From" % label)
        break
else:
    ok(True, "view: none of the six Blender exempts offers View From")
for label in ("Combined", "Shadow", "Emission", "Diffuse", "Glossy",
              "Transmission"):
    bake.set_type(label)
    if "View From" not in labels(bake):
        ok(False, "view: %s must offer View From" % label)
        break
else:
    ok(True, "view: the other six all offer it")

# heading rows, tickbox rows, and what is clickable
bake.set_type("Combined")
heads = [i for i, (_l, v) in enumerate(bake.rows) if v is None]
checks = [i for i, (_l, v) in enumerate(bake.rows) if v is True or v is False]
ok(len(heads) == 3 and all(i not in bake.fields for i in heads),
   "rows: the three section headings carry no field (they drag the node)")
ok(len(checks) == 8 and all(i in bake.fields for i in checks),
   "rows: all eight tickboxes are clickable (six contributions + "
   "Selected to Active + Clear Image)")
ok(all(isinstance(bake.rows[i][1], bool) for i in checks),
   "rows: a tickbox row's value is a real bool, which is what paints a tick")

# sockets must not move when the rows do — wires would jump otherwise
before = bake.socket_pos("in", 0), bake.socket_pos("out", 0)
tall = bake.h
bake.set_type("Roughness")
ok(bake.h < tall, "rows: a type with fewer options makes a shorter node")
ok(bake.socket_pos("in", 0) == before[0]
   and bake.socket_pos("out", 0) == before[1],
   "rows: sockets are indexed by SLOT, so rebuilding rows never moves a wire")
bake.set_type("Combined")

# toggling
bake.toggle_pass("GLOSSY")
ok(row_map(bake)["Glossy"] is False and "GLOSSY" not in bake.pass_filter(),
   "toggle: unticking a contribution drops it from the pass filter")
ok(0 not in bake.dim_rows and not bake.dim_rows,
   "toggle: nothing is dimmed while Direct or Indirect is on")
bake.toggle_pass("DIRECT")
bake.toggle_pass("INDIRECT")
ok(bake.dim_rows and all(bake.rows[i][0] in
                         ("Diffuse", "Glossy", "Transmission", "Emit")
                         for i in bake.dim_rows),
   "toggle: with both lighting passes off the contributions dim (Blender's "
   "layout.active = False — dim but still clickable)")
ok(all(i in bake.fields for i in bake.dim_rows),
   "toggle: and a dimmed row is still a live field")
for flag in bakenodes.PASS_LABELS:      # back to Blender's all-on default
    bake.passes[flag] = True
bake.rebuild()

# samples
ok(bake.samples is None and bake.samples_text() == "Scene",
   "samples: the default is the SCENE's own render samples — native, the "
   "fast engine's 1/16 is gone")
bake.set_type("Normal")
ok(bake.samples_text() == "Scene",
   "samples: a data map reads the same — one rule for every type now")
bake.set_type("Combined")
bake.set_samples(64)
ok(bake.samples == 64 and row_map(bake)["Samples"] == "64",
   "samples: an explicit count shows as itself")
bake.set_samples(0)
ok(bake.samples == 1, "samples: zero samples is clamped to one")
bake.set_samples(None)
ok(bake.samples is None, "samples: and Auto comes back")

# margin
bake.margin = 4
bake.set_margin_type("EXTEND")
ok(row_map(bake)["Margin"] == "4 px"
   and row_map(bake)["Margin type"] == "Extend",
   "output: margin size and margin type both show their value")
bake.set_margin_type("ADJACENT_FACES")

# ⚠ the margin TYPE hides for tangent-space Normal and UV — Blender's own
# rule (CYCLES_RENDER_PT_bake_output_margin), size stays
bake.set_type("Normal")
ok("Margin type" not in row_map(bake) and "Margin" in row_map(bake),
   "margin: a TANGENT normal bake hides the type dropdown, keeps the size")
bake.set_space("OBJECT")
ok("Margin type" in row_map(bake),
   "margin: an OBJECT-space normal bake shows it again")
bake.set_space("TANGENT")
bake.set_type("UV")
ok("Margin type" not in row_map(bake),
   "margin: a UV bake hides it too")
bake.set_type("Combined")

# Selected to Active: the tick is the header, the family appears under it
ok(row_map(bake)["Selected to Active"] is False,
   "s2a: off by default, like a fresh panel")
bake.toggle_s2a()
s2a_rows = labels(bake)
ok(row_map(bake)["Cage"] is False
   and "Extrusion" in s2a_rows and "Max Ray Distance" in s2a_rows
   and "Cage Object" not in s2a_rows,
   "s2a: ticking it reveals Cage, Extrusion and Max Ray Distance — no "
   "cage object row until Cage is on (got %r)" % s2a_rows)
bake.toggle_cage()
ok("Cage Object" in labels(bake) and "Cage Extrusion" in labels(bake),
   "s2a: Cage on reveals the cage object row and renames the extrusion — "
   "Blender's own layout")
cage_idx = [i for i, (l, _v) in enumerate(bake.rows)
            if l == "Cage Extrusion"][0]
ok(cage_idx not in bake.dim_rows,
   "s2a: the extrusion is live while no cage object is named")
bake.cage_object = "CageMesh"
bake.rebuild()
cage_idx = [i for i, (l, _v) in enumerate(bake.rows)
            if l == "Cage Extrusion"][0]
ok(cage_idx in bake.dim_rows,
   "s2a: ⚠ naming a cage object DIMS the extrusion — Blender's rule (the "
   "object replaces it): dim, not gone")
ok(bake.settings_dict()["cage_object"] == "CageMesh"
   and bake.settings_dict()["selected_to_active"] is True,
   "s2a: the family is remembered with the node")
bake.cage_object = ""
bake.toggle_cage()
bake.toggle_s2a()

# Target: a color-attribute bake strips the image-only rows
bake.set_target("VERTEX_COLORS")
vrows = labels(bake)
ok("Clear Image" not in vrows and "Margin" not in vrows
   and "Resolution" not in vrows and "Samples" in vrows,
   "target: Active Color Attribute hides Clear/Margin/Resolution — there "
   "is no image (got %r)" % vrows)
ok(bake.settings_dict()["target"] == "VERTEX_COLORS",
   "target: remembered with the node")
bake.set_target("IMAGE_TEXTURES")

# every contribution off is refused BEFORE the bridge call
tab5.nodes["shader"].set_material("MatA")
bake.set_type("Diffuse")
for flag in ("DIRECT", "INDIRECT", "COLOR"):
    bake.toggle_pass(flag)
ok(bake.pass_filter() == [], "off: the pass filter really is empty")
tab5.run_bake(bake)
ok("contribution" in tab5.status_label.text().lower()
   and not tab5.bridge.calls,
   "off: an all-off bake is refused in words, without spending a request")
for flag in ("DIRECT", "INDIRECT", "COLOR"):
    bake.toggle_pass(flag)

# the full run carries every option
bake.set_samples(32)
bake.set_view_from("ACTIVE_CAMERA")
bake.set_margin_type("EXTEND")
bake.margin = 8
bake.toggle_pass("INDIRECT")
tab5.run_bake(bake)
wait_bake(tab5)
sent = tab5.bridge.options
ok(sent.get("samples") == 32 and sent.get("margin") == 8
   and sent.get("margin_type") == "EXTEND"
   and sent.get("view_from") == "ACTIVE_CAMERA",
   "send: samples, margin, margin type and view from all reach the bridge")
ok(sent.get("pass_filter") == ["DIRECT", "COLOR"],
   "send: only the ticked contributions go (got %r)"
   % (sent.get("pass_filter"),))
ok(sent.get("normal_space") == "TANGENT"
   and sent.get("normal_swizzle") == ["POS_X", "POS_Y", "POS_Z"],
   "send: the normal options travel too, for the add-on to use or ignore")
ok(sent.get("target") == "IMAGE_TEXTURES" and sent.get("use_clear") is True
   and sent.get("use_selected_to_active") is False
   and sent.get("use_cage") is False and sent.get("cage_object") is None,
   "send: the 0.29.0 panel family travels — target, clear and the "
   "selected-to-active block")
ok("view_transform" not in sent and "denoise" not in sent,
   "send: and the retired 0.28.x keys are NOT in the payload")
ok("baked" in tab5.status_label.text().lower(),
   "send: and a current add-on reports a clean result")

# a color-attribute run: no file, and the summary says where the map went
bake.set_target("VERTEX_COLORS")
tab5.run_bake(bake)
wait_bake(tab5)
ok(tab5.bridge.options.get("target") == "VERTEX_COLORS",
   "vcol: the target reaches the bridge")
ok("color attribute" in tab5.status_label.text(),
   "vcol: the summary names the attribute, not a file (got %r)"
   % tab5.status_label.text())
bake.set_target("IMAGE_TEXTURES")

# ⚠ an add-on that ignores the options must SAY so — grown parameters
# cannot be capability-checked, the echo is the only proof
old = FakeBridge(echo_options=False)
tab6 = nodecanvas.NodeEditorTab(old, FakeWindow())
tab6.nodes["shader"].set_material("MatA")
tab6.run_bake(tab6.nodes["bake"])
wait_bake(tab6)
ok("ignored the bake options" in tab6.status_label.text(),
   "echo: a missing options block warns that the add-on ignored them")
ok(tab6.nodes["out"].rows[tab6.nodes["out"].status_row][1].startswith("⚠"),
   "echo: and the Output node's note carries the ⚠")

old.warning = "the map baked EMPTY: the surface rendered fully transparent"
_plain = old.bake_texture


def _both(*a, **kw):
    r = _plain(*a, **kw)
    r["warning"] = old.warning
    return r


old.bake_texture = _both
tab6.run_bake(tab6.nodes["bake"])
wait_bake(tab6)
ok("ignored the bake options" in tab6.status_label.text()
   and "fully transparent" in tab6.status_label.text(),
   "echo: a stale add-on's own warning is kept, not swallowed by ours")

# ------------------------------- 15. the bake batch of 2026-08-07 (0.26.0)
# Marty's list: the target node is TITLED Bake (with Bake all slots), the
# options node Bake settings, a Bulk bake node (selected / folder modes),
# auto names + the designated baked folder, progress strips, Shift+A, and
# "remember node settings".
import config  # noqa: E402
from PySide6.QtGui import QPainter as _QPainter15  # noqa: E402
from PySide6.QtWidgets import QToolButton  # noqa: E402

# --- titles and the add menus
ok(g["shader"].title == "Bake" and g["bake"].title == "Bake settings",
   "rename: the target node is titled Bake, the options node Bake settings")
ok(bakenodes.NODE_KINDS == ("Bake", "Bulk bake", "Collection", "Map set",
                            "Bake settings", "Output image"),
   "add: NODE_KINDS carries the six real nodes in chain order (Collection "
   "and Map set joined 2026-08-08; Image texture stays retired) — got %r"
   % (bakenodes.NODE_KINDS,))
ok(not hasattr(bakenodes, "ImageTextureNode"),
   "add: the Image texture node is really gone, not just unlisted")
ok(all(k in bakenodes.NODE_MAKERS for k in bakenodes.NODE_KINDS),
   "add: every menu entry has a maker")
toolbar_menu = tab4.findChildren(QToolButton)[0].menu()
ok([a.text() for a in toolbar_menu.actions()]
   == list(bakenodes.NODE_KINDS) + ["Reroute dot"],
   "add: the toolbar menu is NODE_KINDS + the reroute dot, in order")

# --- Shift+A opens the same add menu at the cursor
opened = []
tab4.open_add_menu = lambda gp, sp: opened.append(sp)
tab4.canvas.keyPressEvent(QKeyEvent(
    QEvent.KeyPress, Qt.Key_A, Qt.KeyboardModifier.ShiftModifier))
ok(len(opened) == 1,
   "shift+a: the canvas key handler reaches the tab's add menu")
tab4.canvas.keyPressEvent(QKeyEvent(
    QEvent.KeyPress, Qt.Key_A, Qt.KeyboardModifier.NoModifier))
ok(len(opened) == 1, "shift+a: a plain A does nothing")
placed = tab4.add_bake_node("Bulk bake", QPointF(123, 45))
ok(placed.pos() == QPointF(123, 45),
   "shift+a: an added node lands at the given (cursor) position")
ok("Shift+A" in [lbl.text() for lbl in
                 tab4.findChildren(type(tab4.zoom_label))][0]
   or any("Shift+A" in lbl.text()
          for lbl in tab4.findChildren(type(tab4.zoom_label))),
   "shift+a: the toolbar hint mentions it")
tab4.scene.remove_node(placed)

# --- the all-slots tickbox on the target node
tgt = g["shader"]
ok(tgt.rows[1] == ("Bake all slots", False) and 1 in tgt.fields,
   "slots: the Bake node carries a clickable all-slots tickbox, off by "
   "default")
tgt.toggle_all_slots()
ok(tgt.all_slots is True and tgt.rows[1][1] is True,
   "slots: toggling ticks it")
tgt.toggle_all_slots()

# --- the default output folder is pinned in the release builder
MAKE_RELEASE = os.path.join(os.path.dirname(_ROOT), "license-server", "tools", "make_release.js")
with open(MAKE_RELEASE, encoding="utf-8") as fh:
    ok('"baked"' in fh.read(),
       "folder: make_release.js NEVER_SHIP_DIRS knows the baked folder — "
       "a release must not ship (or an update sweep) the user's maps")
ok(bakenodes.sanitize_name("We ird/na:me") == "We ird_na_me",
   "folder: auto names sanitize the same way the add-on does")
ok(bakenodes.auto_out_path("MatA").endswith("MatA_baked")
   and os.path.splitext(bakenodes.auto_out_path("MatA"))[1] == "",
   "folder: auto paths are extensionless — the add-on owns .png vs .exr")


# --- a bridge that answers bake_targets / list_collections
class BulkFakeBridge(FakeBridge):
    def __init__(self, targets=None, skipped=0, collections=None,
                 caps=True, **kw):
        super().__init__(**kw)
        self.targets = list(targets or [])
        self.skipped = skipped
        self.collections = list(collections or [])
        self.caps = caps
        self.target_calls = []

    def supports(self, cmd):
        if cmd in ("bake_targets", "list_collections"):
            return self.caps
        return True

    def feature_reason(self, feature):
        if feature in ("bake_all_slots", "bulk_bake") and not self.caps:
            return ("needs Blender add-on 0.26.0 or newer — update the "
                    "extension")
        return self.reason

    def bake_targets(self, mode, material=None, collection=None):
        self.target_calls.append((mode, material, collection))
        return {"mode": mode, "targets": self.targets,
                "skipped": self.skipped}

    def list_collections(self):
        return {"collections": self.collections}


# --- Bake all slots: one press, one bake per slot, auto names
slots_bridge = BulkFakeBridge(
    targets=[{"object": "Body", "materials": ["Face", "Torso", "Legs"]}])
swin = FakeWindow()
tab7 = nodecanvas.NodeEditorTab(slots_bridge, swin)
tab7.nodes["shader"].set_material("Torso")
tab7.nodes["shader"].toggle_all_slots()
tab7.run_bake(tab7.nodes["bake"])
wait_bake(tab7)
ok(slots_bridge.target_calls == [("material", "Torso", None)],
   "slots: the slot list is resolved by the ADD-ON at press time")
ok([c[0] for c in slots_bridge.calls] == ["Face", "Torso", "Legs"],
   "slots: every slot material bakes, in slot order (got %r)"
   % [c[0] for c in slots_bridge.calls])
ok([c[4] for c in slots_bridge.calls]
   == [os.path.join(BAKE_TMP, "%s_baked" % m)
       for m in ("Face", "Torso", "Legs")],
   "slots: each map auto-names <material>_baked in the baked folder")
ok(all(kw.get("object_name") == "Body" for kw in
       [slots_bridge.options]),
   "slots: the resolved object is pinned on the request, so bake_texture "
   "cannot pick a different mesh")
ok(swin.begun == [("3 slots of Body", "baking")] and swin.ended == 1,
   "slots: ONE capture bracket wraps the whole run (got %r)" % swin.begun)
ok("Baked 3/3 maps" in tab7.status_label.text(),
   "slots: the summary counts the maps (got %r)"
   % tab7.status_label.text())
ok(tab7.nodes["bake"].progress is None,
   "slots: the progress strip clears when the run ends")

# an explicit Output path lends its FOLDER, names stay automatic
slots_bridge.calls = []
tab7.nodes["out"].set_path(os.path.join(BAKE_TMP, "sub", "picked.png"))
tab7.run_bake(tab7.nodes["bake"])
wait_bake(tab7)
ok([c[4] for c in slots_bridge.calls]
   == [os.path.join(BAKE_TMP, "sub", "%s_baked" % m)
       for m in ("Face", "Torso", "Legs")],
   "slots: an explicit path lends its folder — N maps cannot share one "
   "filename (got %r)" % [c[4] for c in slots_bridge.calls])
tab7.nodes["out"].set_path(None)

# all slots on an add-on without bake_targets: that one feature refuses
old_caps = BulkFakeBridge(caps=False)
tab8 = nodecanvas.NodeEditorTab(old_caps, FakeWindow())
tab8.nodes["shader"].set_material("MatA")
tab8.nodes["shader"].toggle_all_slots()
tab8.run_bake(tab8.nodes["bake"])
ok("0.26.0" in tab8.status_label.text() and not old_caps.calls,
   "slots: an old add-on refuses all-slots with the update reason, "
   "single bakes untouched")

# --- the Bulk bake node
bulk_bridge = BulkFakeBridge(
    targets=[{"object": "Chair", "materials": ["Wood", "Metal"]},
             {"object": "Table", "materials": ["Wood"]}],
    skipped=2)
bwin = FakeWindow()
tab9 = nodecanvas.NodeEditorTab(bulk_bridge, bwin)
bulk = tab9.add_bake_node("Bulk bake")
ok(isinstance(bulk, bakenodes.BulkBakeNode) and bulk.title == "Bulk bake",
   "bulk: the node exists and is titled Bulk bake")
ok(bulk.mode == "SELECTED"
   and bulk.button_text() == "Selected to bake queue",
   "bulk: bulk mode is the default and the button says so")
rows = {label: value for label, value in bulk.rows}
ok("Collection" in rows and 1 in bulk.dim_rows and 1 not in bulk.fields,
   "bulk: in bulk mode the collection row is grayed and not clickable")
bulk.set_mode("COLLECTION")
ok(1 in bulk.fields and 1 not in bulk.dim_rows
   and bulk.button_text() == "Bake collection",
   "bulk: folder mode wakes the collection picker and relabels the button")

# ⚠ UNWIRED IT CANNOT BAKE (Marty, 2026-08-07): the chain is the permission
tab9.run_bulk_bake(bulk)
ok("bake settings" in tab9.status_label.text().lower()
   and not bulk_bridge.target_calls,
   "bulk: an unwired Bulk bake node refuses and names what it needs "
   "(got %r)" % tab9.status_label.text())
tab9.scene.add_wire((bulk, "out", 0), (tab9.nodes["bake"], "in", 0),
                    bakenodes.COL_MATERIAL)
# ⚠ THIS RULE FLIPPED ON 2026-08-08. The Bake-settings input became a
# MULTI-input (Marty: "both Bake and 'Collection' nodes can be wired in bake
# settings in the same time"), so a second wire no longer replaces the
# first — they ACCUMULATE and every source is baked. One-wire-per-input
# still holds for every plain input, which the reroute tests cover.
_into9 = tab9.scene.wires_into(tab9.nodes["bake"], 0)
ok(any(w.src[0] is bulk for w in _into9) and len(_into9) == 2,
   "bulk: wiring it in ADDS to the Bake node's wire — the settings input is "
   "multi now, so a Bake and a Bulk bake node can drive one run together "
   "(got %d wires)" % len(_into9))

tab9.run_bulk_bake(bulk)
ok("collection" in tab9.status_label.text().lower()
   and not bulk_bridge.target_calls,
   "bulk: folder mode without a collection refuses before any request")
bulk.set_collection("Props")
tab9.run_bulk_bake(bulk)
wait_bake(tab9)
ok(bulk_bridge.target_calls[-1] == ("collection", None, "Props"),
   "bulk: folder mode asks the add-on for the collection's meshes")
sent = [(c[0], c[4]) for c in bulk_bridge.calls]
ok([m for m, _p in sent] == ["Wood", "Metal", "Wood"],
   "bulk: every slot of every mesh bakes (got %r)" % [m for m, _p in sent])
ok(sent[0][1] == os.path.join(BAKE_TMP, "Wood_Chair_baked")
   and sent[2][1] == os.path.join(BAKE_TMP, "Wood_Table_baked"),
   "bulk: a material shared by two objects folds the object into the "
   "name — the second bake must not overwrite the first")
ok(sent[1][1] == os.path.join(BAKE_TMP, "Metal_baked"),
   "bulk: an unshared material keeps the plain auto name")
ok("Baked 3/3 maps" in tab9.status_label.text()
   and "2 selected item(s) skipped" in tab9.status_label.text(),
   "bulk: the summary counts maps AND names the skipped selection (got %r)"
   % tab9.status_label.text())
ok(bwin.begun == [("3 maps", "baking")] and bwin.ended == 1,
   "bulk: one capture bracket around the whole run")

# selected mode goes through the same enumeration
bulk.set_mode("SELECTED")
bulk_bridge.calls = []
tab9.run_bulk_bake(bulk)
wait_bake(tab9)
ok(bulk_bridge.target_calls[-1] == ("selected", None, None),
   "bulk: selected mode asks for the viewport selection")

# nothing bakeable = words, not a run
empty_bridge = BulkFakeBridge(targets=[], skipped=3)
tab10 = nodecanvas.NodeEditorTab(empty_bridge, FakeWindow())
bulk10 = tab10.add_bake_node("Bulk bake")
tab10.scene.add_wire((bulk10, "out", 0), (tab10.nodes["bake"], "in", 0),
                     bakenodes.COL_MATERIAL)
tab10.run_bulk_bake(bulk10)
ok("Nothing bakeable" in tab10.status_label.text()
   and "3 item(s)" in tab10.status_label.text()
   and not empty_bridge.calls,
   "bulk: an all-skipped selection explains itself without baking (got %r)"
   % tab10.status_label.text())

# one failed map does not sink the rest
flaky = BulkFakeBridge(
    targets=[{"object": "A", "materials": ["Ok1", "Bad", "Ok2"]}])
_good = flaky.bake_texture


def _flaky_bake(material, *a, **kw):
    if material == "Bad":
        raise RuntimeError("no UV map")
    return _good(material, *a, **kw)


flaky.bake_texture = _flaky_bake
tab11 = nodecanvas.NodeEditorTab(flaky, FakeWindow())
tab11.nodes["shader"].set_material("Ok1")
tab11.nodes["shader"].toggle_all_slots()
tab11.run_bake(tab11.nodes["bake"])
wait_bake(tab11)
ok("Baked 2/3 maps" in tab11.status_label.text()
   and "failed: Bad (no UV map)" in tab11.status_label.text(),
   "queue: a failed map is reported and the rest still bake (got %r)"
   % tab11.status_label.text())

# --- the progress strip itself
node = tab9.nodes["bake"]
ok(node.progress is None, "strip: hidden while idle")
node.set_progress(0.5)
ok(node.progress == 0.5, "strip: a queue sets a real fraction")
ok(node.boundingRect().height() > node.h + 8,
   "strip: the boundingRect reserves the space under the body")
body = node.shape().boundingRect()
ok(abs(body.height() - node.h) < 1 and abs(body.width() - node.w) < 1,
   "strip: the SHAPE stays the body — the strip zone is not clickable")
shot15 = _QImage(int(node.w) + 20, int(node.h) + 20,
                 _QImage.Format_ARGB32)
shot15.fill(0)
p15 = _QPainter15(shot15)
p15.translate(4, 4)
node.paint(p15, None)
p15.end()
strip_y = int(node.h + 5) + 4
lit = sum(1 for x in range(shot15.width())
          if shot15.pixelColor(x, strip_y).alpha() > 0)
ok(lit > node.w * 0.3,
   "strip: painting at progress 0.5 really draws the bar (%d px lit)" % lit)
node.set_progress(-1.0)
phase_before = node._marquee_phase
tab9._bake_driver = node
tab9._tick_marquee()
ok(node._marquee_phase != phase_before,
   "strip: the marquee phase advances on the tab's timer tick")
tab9._bake_driver = None
node.set_progress(None)

# --- remember node settings (off by default, per node type)
class CfgWindow(FakeWindow):
    def __init__(self, remember):
        super().__init__()
        self.cfg = {"nodeeditor": {"remember": remember, "last": {}}}
        self.saved = 0

    def save_config(self):
        self.saved += 1


ok(config.DEFAULTS["nodeeditor"] == {"remember": False, "last": {}},
   "remember: config defaults carry the group, OFF by default")

win_off = CfgWindow(remember=False)
tab12 = nodecanvas.NodeEditorTab(FakeBridge(), win_off)
tab12.nodes["shader"].set_material("MatA")
tab12.nodes["bake"].set_size(2048, 2048)
tab12.run_bake(tab12.nodes["bake"])
wait_bake(tab12)
ok(win_off.cfg["nodeeditor"]["last"] == {} and win_off.saved == 0,
   "remember: while the tickbox is off, nothing is stored")

win_on = CfgWindow(remember=True)
tab13 = nodecanvas.NodeEditorTab(FakeBridge(), win_on)
tab13.nodes["shader"].set_material("MatB")
tab13.nodes["bake"].set_size(4096, 1024)
tab13.nodes["bake"].set_type("Normal")
tab13.run_bake(tab13.nodes["bake"])
wait_bake(tab13)
last = win_on.cfg["nodeeditor"]["last"]
ok(last.get("BakeTargetNode", {}).get("material") == "MatB"
   and last.get("BakeSettingsNode", {}).get("width") == 4096
   and last.get("BakeSettingsNode", {}).get("bake_type") == "Normal"
   and win_on.saved >= 1,
   "remember: a bake snapshots every node type's settings and saves")
tab14 = nodecanvas.NodeEditorTab(FakeBridge(), win_on)
ok(tab14.nodes["bake"].width_px == 4096
   and tab14.nodes["bake"].bake_type == "Normal"
   and tab14.nodes["shader"].material == "MatB",
   "remember: a fresh tab's starting graph pre-fills from the stored "
   "values")
added = tab14.add_bake_node("Bake settings")
ok(added.width_px == 4096 and added.height_px == 1024,
   "remember: a newly added node pre-fills too")
tab15 = nodecanvas.NodeEditorTab(FakeBridge(), CfgWindow(remember=False))
ok(tab15.nodes["bake"].width_px == 1024,
   "remember: with the tickbox off, nodes start at the defaults")

# the settings dialog carries the tickbox and keeps the stored values
import inspect as _inspect  # noqa: E402

import main as main_mod  # noqa: E402

dlg_src = _inspect.getsource(main_mod.LibrarySettingsDialog)
ok("Remember node settings" in dlg_src,
   "remember: ⚙ Library Settings offers the tickbox")
ok("node_group" in dlg_src and '"nodeeditor": node_group' in dlg_src,
   "remember: values() hands back the WHOLE group — cfg.update() must not "
   "drop the stored last-used values")

# ---------------------------- 16. the wiring rules (Marty, 2026-08-07 pm)
# "Both Bake and Bulk Bake shouldn't be able to bake by their own" — the
# chain is the permission; sockets connect only to their own colour; the
# Image texture node is gone; and a live value must not read as disabled.

tab16 = nodecanvas.NodeEditorTab(BulkFakeBridge(
    targets=[{"object": "Body", "materials": ["Skin"]}]), FakeWindow())
s16 = tab16.nodes["bake"]

# --- both sources are GREEN, and only the settings input takes green
ok(nodecanvas.socket_colour(tab16.nodes["shader"], "out", 0)
   == bakenodes.COL_MATERIAL,
   "type: the Bake node sends green")
bulk16 = tab16.add_bake_node("Bulk bake")
ok(nodecanvas.socket_colour(bulk16, "out", 0) == bakenodes.COL_MATERIAL,
   "type: the Bulk bake node sends the SAME green — both are bake sources")
ok(nodecanvas.socket_colour(s16, "in", 0) == bakenodes.COL_MATERIAL,
   "type: the Bake settings input is green")
ok(len(s16.inputs) == 1,
   "type: Bake settings has ONE input — the old second socket sat beside "
   "the Samples row and read like a Samples input (got %d)"
   % len(s16.inputs))

# --- a mismatched drag is refused, a matching one connects
green_out = (bulk16, "out", 0)
green_in = (s16, "in", 0)
orange_in = (tab16.nodes["out"], "in", 0)
ok(nodecanvas.sockets_compatible(green_out, green_in),
   "type: green to green connects")
ok(not nodecanvas.sockets_compatible(green_out, orange_in),
   "type: green into the ORANGE Baked input is refused")
ok(nodecanvas.sockets_compatible((s16, "out", 0), orange_in),
   "type: the orange Baked output still reaches the Output image node")

before16 = len(tab16.scene.wires)
sw16 = tab16.canvas
sw16.resize(600, 400)
sw16.show()
app.processEvents()


def drag_socket(view, frm, to):
    """Press on one socket, release on another — the real gesture."""
    scene_from = frm[0].socket_pos(frm[1], frm[2])
    scene_to = to[0].socket_pos(to[1], to[2])
    view.centerOn((scene_from + scene_to) / 2.0)
    app.processEvents()
    for etype, pt, btn, btns in (
            (QEvent.MouseButtonPress, scene_from, Qt.LeftButton, Qt.LeftButton),
            (QEvent.MouseMove, scene_to, Qt.NoButton, Qt.LeftButton),
            (QEvent.MouseButtonRelease, scene_to, Qt.LeftButton, Qt.NoButton)):
        vp = view.mapFromScene(pt)
        QApplication.sendEvent(view.viewport(), QMouseEvent(
            etype, QPointF(vp), QPointF(view.viewport().mapToGlobal(vp)),
            btn, btns, Qt.KeyboardModifier.NoModifier))


drag_socket(sw16, (bulk16, "out", 0), (tab16.nodes["out"], "in", 0))
ok(len(tab16.scene.wires) == before16,
   "type: dragging green onto an orange socket draws NO wire")
ok("colour" in tab16.status_label.text().lower(),
   "type: and the status line says why (got %r)" % tab16.status_label.text())
drag_socket(sw16, (bulk16, "out", 0), (s16, "in", 0))
ok(any(w.src[0] is bulk16 for w in tab16.scene.wires_into(s16, 0)),
   "type: the same drag onto the GREEN input connects (it JOINS the wire "
   "already there — the input is multi since 2026-08-08)")

# an untyped reroute adopts the first colour that reaches it
rr16 = tab16.add_bake_node("Reroute")
ok(rr16.typed is False, "reroute: a fresh dot has no type yet")
ok(nodecanvas.sockets_compatible((s16, "out", 0), (rr16, "in", 0)),
   "reroute: an untyped dot accepts any colour")
drag_socket(sw16, (s16, "out", 0), (rr16, "in", 0))
ok(rr16.typed and rr16.colour == bakenodes.COL_BAKED,
   "reroute: it adopts the colour of the wire that reached it")
ok(not nodecanvas.sockets_compatible((bulk16, "out", 0), (rr16, "in", 0)),
   "reroute: once typed it refuses a different colour, like any socket")
tab16.canvas.hide()

# --- the Bake node cannot bake without the settings node either
tab17 = nodecanvas.NodeEditorTab(BulkFakeBridge(
    targets=[{"object": "Body", "materials": ["Skin"]}]), FakeWindow())
lone = tab17.add_bake_node("Bake")
lone.set_material("Skin")
ok(not hasattr(lone, "run_bake") and not hasattr(lone, "_button_rect"),
   "chain: the Bake node has no button of its own — it is a source, and "
   "only the Bake settings node runs anything")
for w in list(tab17.scene.wires_into(tab17.nodes["bake"], 0)):
    tab17.scene.remove_wire(w)
tab17.run_bake(tab17.nodes["bake"])
ok(not tab17.bridge.calls
   and "connect" in tab17.status_label.text().lower(),
   "chain: with nothing feeding it, Bake settings refuses (got %r)"
   % tab17.status_label.text())

# --- a live value pill is brighter than a dimmed one
bulk17 = tab17.add_bake_node("Bulk bake")
bulk17.set_mode("SELECTED")          # collection row dim, mode row live


def pill_pixels(node, row, colour_test):
    shot = _QImage(int(node.w) + 20, int(node.h) + 30,
                   _QImage.Format_ARGB32)
    shot.fill(0)
    p = _QPainter15(shot)
    p.translate(4, 4)
    node.paint(p, None)
    p.end()
    y = int(nodecanvas.HEADER_H + nodecanvas.ROW_GAP
            + row * (nodecanvas.ROW_H + nodecanvas.ROW_GAP)
            + nodecanvas.ROW_H / 2) + 4
    band = range(max(0, y - 5), min(shot.height(), y + 6))
    return sum(1 for yy in band for x in range(int(node.value_x()) + 4,
                                               int(node.w))
               if colour_test(shot.pixelColor(x, yy)))


bright = pill_pixels(bulk17, 0, lambda c: c.lightness() > 200)
dimmed = pill_pixels(bulk17, 1, lambda c: c.lightness() > 200)
ok(bright > 0,
   "bright: a LIVE value pill paints bright text (%d px)" % bright)
ok(bright > dimmed * 2,
   "bright: and a grayed row stays visibly dimmer (%d vs %d px) — a live "
   "field must not read as disabled" % (bright, dimmed))
ok(nodecanvas.VALUE_TEXT.lightness() > nodecanvas.TEXT_DIM.lightness(),
   "bright: the value colour really is lighter than the dim one")

# ------------------------- 17. Replace shader (Marty, 2026-08-07, 0.27.0)
# "in output image node add a tickbox that replaces the shader (and slots if
# there are any) of where the shaders were baked on their respective uvs
# (default is unchecked)" — and his correction: *"just PLACE the node in the
# material > respective slots and attach it to material output"*, which is
# the add-on's half (texbake_test section 10). The app's half is the same
# either way: it is the only part of this pipeline that writes to the scene,
# so it is off by default, gated on its own command, and spent as ONE call
# AFTER the whole queue — never per map.


class ReplaceFakeBridge(BulkFakeBridge):
    def __init__(self, replace_caps=True, fail=None, skipped_rows=None,
                 echo_all_slots=True, **kw):
        super().__init__(**kw)
        self.replace_caps = replace_caps
        self.fail = fail
        self.skipped_rows = list(skipped_rows or [])
        self.replace_calls = []
        self.replace_all = None
        self.echo_all_slots = echo_all_slots
        self.order = []

    def supports(self, cmd):
        if cmd == "apply_baked_material":
            return self.replace_caps
        return super().supports(cmd)

    def feature_reason(self, feature):
        if feature == "bake_replace" and not self.replace_caps:
            return ("Replace shader needs Blender add-on 0.27.0 or newer — "
                    "update the extension from ⚙ Library Settings")
        return super().feature_reason(feature)

    def bake_texture(self, material, *a, **kw):
        self.order.append("bake:%s" % material)
        reply = super().bake_texture(material, *a, **kw)
        # the real add-on echoes the object it actually baked
        reply["object"] = kw.get("object_name") or "Body"
        return reply

    def apply_baked_material(self, items, all_slots=False):
        self.order.append("apply:%d" % len(items))
        self.replace_calls.append([dict(row) for row in items])
        self.replace_all = all_slots
        if self.fail:
            raise RuntimeError(self.fail)
        reply = {"applied": [{"object": r.get("object"),
                              "material": r.get("material"),
                              "slots": [0], "node": "Image Texture",
                              "output": "Material Output",
                              "was_fed_by": "Principled BSDF",
                              "uv_layer": "UVMap", "uv_tile": [0, 0]}
                             for r in items],
                 "skipped": self.skipped_rows, "count": len(items)}
        # 0.30.0 echoes the grown parameter back; `echo_all_slots=False`
        # plays the older add-on that silently ignored it.
        if self.echo_all_slots:
            reply["all_slots"] = bool(all_slots)
        return reply


out18 = nodecanvas.NodeEditorTab(ReplaceFakeBridge(), FakeWindow()).nodes["out"]
ok(out18.rows[1] == ("Replace shader", False) and 1 in out18.fields,
   "replace: the Output node carries the tickbox, UNTICKED (got %r)"
   % (out18.rows[1],))
ok(out18.replace_shader is False,
   "replace: default is off — this is the one command that writes to the "
   "user's scene")
ok(out18.rows[-1][0] == "Status" and out18.status_row == len(out18.rows) - 1,
   "replace: Status stays the LAST row — the All slots tickbox landed above "
   "it, and show_result writes through status_row rather than a literal 2")
out18.toggle_replace()
ok(out18.replace_shader is True and out18.rows[1][1] is True,
   "replace: clicking the row toggles it")
ok(out18.settings_dict().get("replace_shader") is True,
   "replace: it is part of what 'remember node settings' stores")
out18.apply_settings({"out_path": None, "replace_shader": False})
ok(out18.replace_shader is False and out18.rows[1][1] is False,
   "replace: and of what it restores")

# --- off: nothing is sent, and the existing wording is untouched
plain18 = ReplaceFakeBridge(targets=[{"object": "Body",
                                      "materials": ["Skin"]}])
tab18 = nodecanvas.NodeEditorTab(plain18, FakeWindow())
tab18.nodes["shader"].set_material("Skin")
tab18.run_bake(tab18.nodes["bake"])
wait_bake(tab18)
ok(not plain18.replace_calls,
   "replace: with the tickbox off the run never mentions it")
ok(tab18.status_label.text().startswith("Baked Skin → "),
   "replace: and the single-map summary is exactly what it always was "
   "(got %r)" % tab18.status_label.text())

# --- on: one call, AFTER the bake, built from the REPLY
rep19 = ReplaceFakeBridge(targets=[{"object": "Body", "materials": ["Skin"]}])
win19 = FakeWindow()
tab19 = nodecanvas.NodeEditorTab(rep19, win19)
tab19.nodes["shader"].set_material("Skin")
tab19.nodes["bake"].set_type("Normal")
tab19.nodes["out"].toggle_replace()
tab19.run_bake(tab19.nodes["bake"])
wait_bake(tab19)
ok(rep19.order == ["bake:Skin", "apply:1"],
   "replace: the swap happens AFTER the map is on disk (got %r)"
   % rep19.order)
row19 = rep19.replace_calls[0][0]
ok(row19["object"] == "Body" and row19["material"] == "Skin"
   and row19["path"] == fake_png and row19["bake_type"] == "NORMAL",
   "replace: the row is built from the REPLY — the object the add-on really "
   "baked and the path it really wrote (got %r)" % row19)
ok(sorted(row19) == ["bake_type", "material", "object", "path"],
   "replace: and carries nothing else — the add-on resolves the material, "
   "the output node and the UV tile itself (got %r)" % sorted(row19))
ok(win19.begun == [("Skin", "baking")] and win19.ended == 1,
   "replace: the swap runs INSIDE the run's single capture bracket "
   "(got %r / %d)" % (win19.begun, win19.ended))
ok("1 shader replaced" in tab19.status_label.text(),
   "replace: the summary says what was swapped (got %r)"
   % tab19.status_label.text())
ok(not tab19.bake_running() and tab19.nodes["bake"].progress is None,
   "replace: and the run really is over — strip cleared, buttons live")

# --- many maps: still ONE call, after the LAST bake
rep20 = ReplaceFakeBridge(
    targets=[{"object": "Body", "materials": ["Face", "Torso", "Legs"]}])
win20 = FakeWindow()
tab20 = nodecanvas.NodeEditorTab(rep20, win20)
tab20.nodes["shader"].set_material("Torso")
tab20.nodes["shader"].toggle_all_slots()
tab20.nodes["out"].toggle_replace()
tab20.run_bake(tab20.nodes["bake"])
wait_bake(tab20)
ok(rep20.order == ["bake:Face", "bake:Torso", "bake:Legs", "apply:3"],
   "replace: an all-slots run swaps ONCE at the end — doing it per map "
   "would hand the later bakes a changed scene (got %r)" % rep20.order)
ok(len(rep20.replace_calls) == 1
   and [r["material"] for r in rep20.replace_calls[0]]
   == ["Face", "Torso", "Legs"],
   "replace: every baked slot is in that one call")
ok(win20.ended == 1,
   "replace: still one capture bracket for bakes AND the swap")
ok("Baked 3/3 maps" in tab20.status_label.text()
   and "3 shaders replaced" in tab20.status_label.text(),
   "replace: the queue summary keeps its own wording and adds the swap "
   "(got %r)" % tab20.status_label.text())

# --- a failed map is not replaced, and does not stop the others
rep21 = ReplaceFakeBridge(
    targets=[{"object": "A", "materials": ["Ok1", "Bad", "Ok2"]}])
_ok_bake = rep21.bake_texture


def _rep_flaky(material, *a, **kw):
    if material == "Bad":
        raise RuntimeError("no UV map")
    return _ok_bake(material, *a, **kw)


rep21.bake_texture = _rep_flaky
tab21 = nodecanvas.NodeEditorTab(rep21, FakeWindow())
tab21.nodes["shader"].set_material("Ok1")
tab21.nodes["shader"].toggle_all_slots()
tab21.nodes["out"].toggle_replace()
tab21.run_bake(tab21.nodes["bake"])
wait_bake(tab21)
ok([r["material"] for r in rep21.replace_calls[0]] == ["Ok1", "Ok2"],
   "replace: a map that never landed is not swapped in (got %r)"
   % [r["material"] for r in rep21.replace_calls[0]])
ok("Baked 2/3 maps" in tab21.status_label.text()
   and "2 shaders replaced" in tab21.status_label.text(),
   "replace: the summary carries both halves (got %r)"
   % tab21.status_label.text())

# --- the add-on's own skips are reported
rep22 = ReplaceFakeBridge(
    targets=[{"object": "Body", "materials": ["Skin"]}],
    skipped_rows=[{"object": "Body", "material": "Skin",
                   "reason": "the baked file is missing"}])
tab22 = nodecanvas.NodeEditorTab(rep22, FakeWindow())
tab22.nodes["shader"].set_material("Skin")
tab22.nodes["out"].toggle_replace()
tab22.run_bake(tab22.nodes["bake"])
wait_bake(tab22)
ok("not replaced" in tab22.status_label.text()
   and "baked file is missing" in tab22.status_label.text(),
   "replace: a slot the add-on could not swap is named, not silently "
   "dropped (got %r)" % tab22.status_label.text())

# --- a failing swap does not turn a good bake into a failed one
rep23 = ReplaceFakeBridge(targets=[{"object": "Body", "materials": ["Skin"]}],
                          fail="material 'Skin' is linked from a library")
tab23 = nodecanvas.NodeEditorTab(rep23, FakeWindow())
tab23.nodes["shader"].set_material("Skin")
tab23.nodes["out"].toggle_replace()
tab23.run_bake(tab23.nodes["bake"])
wait_bake(tab23)
ok(tab23.status_label.text().startswith("Baked Skin → ")
   and "replace failed: material 'Skin' is linked" in tab23.status_label.text(),
   "replace: the maps are on disk either way — the summary says both "
   "(got %r)" % tab23.status_label.text())
ok(not tab23.bake_running(),
   "replace: a failed swap still ends the run")

# --- an old add-on refuses BEFORE the bake is spent
rep24 = ReplaceFakeBridge(targets=[{"object": "Body", "materials": ["Skin"]}],
                          replace_caps=False)
tab24 = nodecanvas.NodeEditorTab(rep24, FakeWindow())
tab24.nodes["shader"].set_material("Skin")
tab24.nodes["out"].toggle_replace()
tab24.run_bake(tab24.nodes["bake"])
ok("0.27.0" in tab24.status_label.text() and not rep24.calls,
   "replace: an add-on without apply_baked_material refuses before any "
   "bake runs (got %r)" % tab24.status_label.text())
tab24.nodes["out"].toggle_replace()
tab24.run_bake(tab24.nodes["bake"])
wait_bake(tab24)
ok(rep24.calls and not rep24.replace_calls,
   "replace: unticking it bakes normally on that same old add-on")

# --- the bulk button goes through the same gate (it presses Bake settings)
rep25 = ReplaceFakeBridge(targets=[{"object": "Body", "materials": ["Skin"]}],
                          replace_caps=False)
tab25 = nodecanvas.NodeEditorTab(rep25, FakeWindow())
bulk25 = tab25.add_bake_node("Bulk bake")
tab25.scene.add_wire((bulk25, "out", 0), (tab25.nodes["bake"], "in", 0),
                     bakenodes.COL_MATERIAL)
tab25.nodes["out"].toggle_replace()
tab25.run_bulk_bake(bulk25)
ok("0.27.0" in tab25.status_label.text() and not rep25.calls,
   "replace: a bulk press is refused by the same gate — it is a shortcut "
   "for pressing Bake settings, not a second path (got %r)"
   % tab25.status_label.text())

# --- 18. the retired rows (0.29.0): View transform and Denoise are gone
# Neither is a Bake panel option, and the node IS the panel now ("do it
# exactly the way it is done in blender"). The rows, the attributes, the
# payload keys and the settings keys all left together — and an OLD saved
# settings dict that still carries them must apply without a murmur.
rep26 = ReplaceFakeBridge(targets=[{"object": "Body", "materials": ["Skin"]}])
tab26 = nodecanvas.NodeEditorTab(rep26, FakeWindow())
s26 = tab26.nodes["bake"]
for t in ("Combined", "Roughness"):
    s26.set_type(t)
    ok("View transform" not in dict(s26.rows)
       and "Denoise" not in dict(s26.rows),
       "retired: %s offers neither View transform nor Denoise" % t)
s26.set_type("Combined")
ok("view_transform" not in s26.settings_dict()
   and "denoise" not in s26.settings_dict(),
   "retired: the settings dict no longer stores them")
s26.apply_settings({"bake_type": "Diffuse", "view_transform": True,
                    "denoise": False})
ok(s26.bake_type == "Diffuse" and not hasattr(s26, "view_transform"),
   "retired: ⚠ a 0.28.x settings dict still APPLIES — the dead keys are "
   "ignored, the live ones land")
tab26.nodes["shader"].set_material("Skin")
tab26.run_bake(s26)
wait_bake(tab26)
ok(rep26.calls and "view_transform" not in rep26.options
   and "denoise" not in rep26.options,
   "retired: the payload carries neither key — a 0.29.0 add-on never "
   "reads them")

# --- EXR: Qt cannot decode it, and "no bake yet" would read as a FAILED
# bake after a perfectly good one (0.28.2)
out27 = nodecanvas.NodeEditorTab(FakeBridge(), None).nodes["out"]
ok(out27.no_preview_text == "no bake yet",
   "exr: a fresh node starts on the plain wording")
fake_exr = os.path.join(BAKE_TMP, "done.exr")
with open(fake_exr, "wb") as _fh:
    _fh.write(b"\x76\x2f\x31\x01")           # EXR magic; QImage still can't
out27.show_result(fake_exr, "COMBINED · 1s · x")
ok(out27.preview is None and out27.no_preview_text == "EXR saved — no preview",
   "exr: ⚠ an EXR result says SAVED, not 'no bake yet' (got %r)"
   % out27.no_preview_text)
out27.show_result(fake_png, "COMBINED · 1s · x")
ok(out27.preview is not None and out27.no_preview_text == "no bake yet",
   "exr: and a PNG result restores the preview and the plain wording")

# --- 19. a pre-0.29 add-on is NAMED (grown parameters, tier two)
# An add-on that echoes an options block WITHOUT `target` is 0.25–0.28: it
# accepted the payload but still bakes margin-0 + hand padding, seams and
# all. The app cannot capability-check that away — it can only read the
# echo and say so.


class Pre29Bridge(ReplaceFakeBridge):
    def bake_texture(self, *a, **kw):
        reply = super().bake_texture(*a, **kw)
        if "options" in reply:
            reply["options"].pop("target", None)
        return reply


old29 = Pre29Bridge(targets=[{"object": "Body", "materials": ["Skin"]}])
tab29 = nodecanvas.NodeEditorTab(old29, FakeWindow())
tab29.nodes["shader"].set_material("Skin")
tab29.run_bake(tab29.nodes["bake"])
wait_bake(tab29)
ok("OLD way" in tab29.status_label.text()
   and "update the extension" in tab29.status_label.text(),
   "pre29: an options echo without `target` warns the add-on still bakes "
   "the margin-0 way (got %r)" % tab29.status_label.text())
ok(tab29.nodes["out"].rows[tab29.nodes["out"].status_row][1].startswith("⚠"),
   "pre29: and the Output node keeps the ⚠")

import bridge as bridge_mod  # noqa: E402

ok("apply_baked_material" in bridge_mod.GATED_COMMANDS
   and bridge_mod.GATED_COMMANDS["apply_baked_material"] == "0.27.0",
   "replace: the command is capability-checkable — a NEW command, not a "
   "grown parameter nobody can see")

# --- 20. searching for a shader (Marty, 2026-08-08)
# "ability to search for a shader from this node (shaders should show even
# if partial match)". The ranking matters as much as the filtering: Enter
# takes the best match, not whichever one happens to be first in the scene.
names20 = ["Lily Bodysuit", "Bodysuit Trim", "Skin", "Eyes", "body_hair"]
ok(bakenodes.filter_names(names20, "") == names20,
   "search: an empty query is not a filter — everything, in scene order")
ok(bakenodes.filter_names(names20, "body")
   == ["Bodysuit Trim", "body_hair", "Lily Bodysuit"],
   "search: a PARTIAL match counts, and every name the query STARTS ranks "
   "above one that merely contains it (got %r)"
   % bakenodes.filter_names(names20, "body"))
ok(bakenodes.filter_names(names20, "SKIN") == ["Skin"],
   "search: case-insensitive")
ok(bakenodes.filter_names(names20, "lily body") == ["Lily Bodysuit"],
   "search: every token has to appear, so two words narrow instead of widen")
ok(bakenodes.filter_names(names20, "zzz") == [],
   "search: nothing matches nothing")
ok(bakenodes.filter_names(names20, "skin")[0] == "Skin",
   "search: an exact match outranks a mere containment")

menu20 = bakenodes.SearchMenu(names20, lambda n: None)
ok([n for n, a in menu20.entries] == names20,
   "search: the menu carries one action per material")
menu20.edit.setText("body")          # the REAL path: textChanged -> filter
visible = [n for n, a in menu20.entries if a.isVisible()]
ok(visible == ["Lily Bodysuit", "Bodysuit Trim", "body_hair"],
   "search: typing HIDES the non-matches (the actions are never rebuilt) "
   "(got %r)" % visible)
ok(menu20.best_match() == "Bodysuit Trim",
   "search: Enter would take the best-ranked match, not the first visible "
   "row (got %r)" % menu20.best_match())
menu20.edit.setText("zzz")
ok(not any(a.isVisible() for _n, a in menu20.entries)
   and menu20.empty_action.isVisible(),
   "search: an empty result says so rather than showing a blank menu")
menu20.edit.setText("")
ok(all(a.isVisible() for _n, a in menu20.entries),
   "search: clearing the box brings everything back")

picked20 = []
menu20b = bakenodes.SearchMenu(names20, picked20.append)
menu20b.edit.setText("eye")
menu20b.accept_best()
ok(picked20 == ["Eyes"], "search: Enter picks — the material really is set")

tab33 = nodecanvas.NodeEditorTab(FakeBridge(), FakeWindow())
menu20c = tab33.nodes["shader"].build_material_menu()
ok(isinstance(menu20c, bakenodes.SearchMenu),
   "search: the Bake node's material pill pops the searchable menu")
ok([n for n, _a in menu20c.entries] == ["MatA", "MatB"],
   "search: fed by the live material list from Blender")
ok(any(a.text() == "Type a name…" for a in menu20c.actions()),
   "search: and 'Type a name…' survives underneath it")
dead20 = nodecanvas.NodeEditorTab(None, FakeWindow())
menu20d = dead20.nodes["shader"].build_material_menu()
ok(not isinstance(menu20d, bakenodes.SearchMenu)
   and any("unreachable" in a.text() for a in menu20d.actions()),
   "search: with no bridge it is a plain refusal menu, not an empty search")

# --- 21. the Output node: a default path you can SEE, and All slots
# "Output image should have a default path in our addon folder if none is
# set" — it always had one; what it did not have was a way to see it.
out34 = nodecanvas.NodeEditorTab(FakeBridge(), FakeWindow()).nodes["out"]
ok(out34.rows[0][0] == "Path"
   and os.path.basename(BAKE_TMP) in out34.rows[0][1]
   and "auto" in out34.rows[0][1],
   "path: an unset path NAMES the folder it will use (got %r)"
   % (out34.rows[0][1],))
ok(bakenodes.default_bake_dir() in out34.toolTip(),
   "path: and the tooltip carries it in full")
out34.set_path(os.path.join(BAKE_TMP, "mine.png"))
ok(out34.rows[0][1] == "mine.png",
   "path: an explicit file shows its own name (got %r)" % (out34.rows[0][1],))
out34.set_path(None)
ok("auto" in out34.rows[0][1], "path: and clearing it goes back to automatic")

ok(out34.rows[2] == ("All slots", False) and 2 in out34.fields,
   "allslots: the Output node carries the tickbox, UNTICKED (got %r)"
   % (out34.rows[2],))
ok(2 in out34.dim_rows,
   "allslots: dimmed while Replace shader is off — it has nothing to act on")
out34.toggle_replace()
ok(2 not in out34.dim_rows,
   "allslots: ticking Replace shader lights it up")
out34.toggle_all_slots()
ok(out34.replace_all_slots is True
   and out34.settings_dict().get("replace_all_slots") is True,
   "allslots: it toggles and 'remember node settings' stores it")
out34.apply_settings({"out_path": None, "replace_shader": True,
                      "replace_all_slots": False})
ok(out34.replace_all_slots is False and out34.rows[2][1] is False,
   "allslots: and restores it")
out34.apply_settings({"out_path": None, "replace_shader": False})
ok(out34.replace_all_slots is False,
   "allslots: ⚠ a settings dict from before 0.30.0 has no such key and "
   "applies cleanly, defaulting to off")

# --- 22. All slots reaches the add-on, and a stale one is NAMED
rep35 = ReplaceFakeBridge(targets=[{"object": "Body", "materials": ["Skin"]}])
tab35 = nodecanvas.NodeEditorTab(rep35, FakeWindow())
tab35.nodes["shader"].set_material("Skin")
tab35.nodes["out"].toggle_replace()
tab35.run_bake(tab35.nodes["bake"])
wait_bake(tab35)
ok(rep35.replace_all is False,
   "allslots: off, the flag still goes out as False — not omitted")

rep36 = ReplaceFakeBridge(targets=[{"object": "Body", "materials": ["Skin"]}])
tab36 = nodecanvas.NodeEditorTab(rep36, FakeWindow())
tab36.nodes["shader"].set_material("Skin")
tab36.nodes["out"].toggle_replace()
tab36.nodes["out"].toggle_all_slots()
tab36.run_bake(tab36.nodes["bake"])
wait_bake(tab36)
ok(rep36.replace_all is True,
   "allslots: ticked, the add-on is asked for every slot")
ok("⚠" not in tab36.status_label.text(),
   "allslots: and an add-on that echoes it back says nothing extra (got %r)"
   % tab36.status_label.text())

old37 = ReplaceFakeBridge(targets=[{"object": "Body", "materials": ["Skin"]}],
                          echo_all_slots=False)
tab37 = nodecanvas.NodeEditorTab(old37, FakeWindow())
tab37.nodes["shader"].set_material("Skin")
tab37.nodes["out"].toggle_replace()
tab37.nodes["out"].toggle_all_slots()
tab37.run_bake(tab37.nodes["bake"])
wait_bake(tab37)
ok("ignored All slots" in tab37.status_label.text()
   and "0.30.0" in tab37.status_label.text(),
   "allslots: ⚠ a GROWN parameter — an add-on that drops it is NAMED, "
   "because supports() cannot see it (got %r)" % tab37.status_label.text())

# --- 23. the Map set node (Marty's "add another node", 2026-08-08)
ok("Map set" in bakenodes.NODE_KINDS
   and bakenodes.NODE_MAKERS["Map set"] is bakenodes.MapSetNode,
   "mapset: it is in the one add-menu table, so the toolbar and Shift+A "
   "both offer it")
ms = bakenodes.MapSetNode(None)
ok(ms.rows[0] == ("Maps", None),
   "mapset: a dim heading, then one tickbox per type")
ok(len(ms.rows) == len(bakenodes.BAKE_TYPES) + 1,
   "mapset: all twelve types are offered")
ok(ms.types() == ["NORMAL", "ROUGHNESS", "DIFFUSE"],
   "mapset: a fresh node ticks a PBR set, and reports it in BLENDER's type "
   "order (Normal, Roughness, Diffuse) rather than the order MAP_SET_DEFAULT "
   "happens to list — got %r" % ms.types())
ms.toggle("Combined")
ok(ms.types()[0] == "COMBINED",
   "mapset: ticking another one keeps the type order, not the click order")
ms.toggle("Combined")
ok(ms.settings_dict()["picked"]["Normal"] is True,
   "mapset: what it remembers is the tick state")
ms2 = bakenodes.MapSetNode(None)
ms2.apply_settings({"picked": {"Normal": False, "Diffuse": False}})
ok(ms2.types() == ["ROUGHNESS"], "mapset: and it restores it")

# wired in: source -> Map set -> Bake settings -> Output
map_bridge = ReplaceFakeBridge(
    targets=[{"object": "Body", "materials": ["Skin"]}])
tab30 = nodecanvas.NodeEditorTab(map_bridge, FakeWindow())
g30 = tab30.nodes
mset = tab30.add_bake_node("Map set")
tab30.scene.remove_wire(tab30.scene.wires_into(g30["bake"], 0)[0])
tab30.scene.add_wire((g30["shader"], "out", 0), (mset, "in", 0),
                     bakenodes.COL_MATERIAL)
tab30.scene.add_wire((mset, "out", 0), (g30["bake"], "in", 0),
                     bakenodes.COL_MATERIAL)
src30, found30 = bakenodes.upstream_source(tab30.scene, g30["bake"])
ok(src30 is g30["shader"] and found30 is mset,
   "mapset: the chain walk passes THROUGH it and still finds the source")
ok(bakenodes.upstream_source(tab30.scene, g30["bake"])[0] is not mset,
   "mapset: it is a pass-through, never the source itself")

g30["shader"].set_material("Skin")
tab30.run_bake(g30["bake"])
wait_bake(tab30)
ok([c[1] for c in map_bridge.calls] == ["NORMAL", "ROUGHNESS", "DIFFUSE"],
   "mapset: one press bakes every ticked type (got %r)"
   % [c[1] for c in map_bridge.calls])
names30 = [os.path.basename(c[4]) for c in map_bridge.calls]
ok(names30 == ["Skin_normal_baked", "Skin_roughness_baked",
               "Skin_diffuse_baked"],
   "mapset: ⚠ the type is folded into the auto name, or the maps would "
   "overwrite each other (got %r)" % names30)
ok("Baked 3/3 maps" in tab30.status_label.text(),
   "mapset: the summary counts them (got %r)" % tab30.status_label.text())

# ⚠ the influence filter is computed PER TYPE, not from the settings node's
# own Type row — Combined's EMIT is not a Diffuse contribution
tab30b = nodecanvas.NodeEditorTab(
    ReplaceFakeBridge(targets=[{"object": "Body", "materials": ["Skin"]}]),
    FakeWindow())
s30 = tab30b.nodes["bake"]
ok(s30.pass_filter_for("DIFFUSE") == ["DIRECT", "INDIRECT", "COLOR"],
   "mapset: a Diffuse bake is asked for Diffuse's own three contributions")
ok(s30.pass_filter_for("NORMAL") is None,
   "mapset: and a data type is sent no filter at all")
ok(s30.pass_filter() == s30.pass_filter_for(s30.enum()),
   "mapset: the node's own filter is the same call, keyed on its Type row")

# nothing ticked is refused BEFORE the run is spent
empty30 = ReplaceFakeBridge(targets=[{"object": "Body",
                                      "materials": ["Skin"]}])
tab31 = nodecanvas.NodeEditorTab(empty30, FakeWindow())
g31 = tab31.nodes
mset31 = tab31.add_bake_node("Map set")
for label in bakenodes.MAP_SET_DEFAULT:
    mset31.toggle(label)
tab31.scene.remove_wire(tab31.scene.wires_into(g31["bake"], 0)[0])
tab31.scene.add_wire((g31["shader"], "out", 0), (mset31, "in", 0),
                     bakenodes.COL_MATERIAL)
tab31.scene.add_wire((mset31, "out", 0), (g31["bake"], "in", 0),
                     bakenodes.COL_MATERIAL)
g31["shader"].set_material("Skin")
tab31.run_bake(g31["bake"])
ok(not empty30.calls and "Tick at least one map" in tab31.status_label.text(),
   "mapset: nothing ticked refuses in words, and spends no bake (got %r)"
   % tab31.status_label.text())

# ⚠ several maps into ONE material still means ONE replace row
rep32 = ReplaceFakeBridge(targets=[{"object": "Body", "materials": ["Skin"]}])
tab32 = nodecanvas.NodeEditorTab(rep32, FakeWindow())
g32 = tab32.nodes
mset32 = tab32.add_bake_node("Map set")
tab32.scene.remove_wire(tab32.scene.wires_into(g32["bake"], 0)[0])
tab32.scene.add_wire((g32["shader"], "out", 0), (mset32, "in", 0),
                     bakenodes.COL_MATERIAL)
tab32.scene.add_wire((mset32, "out", 0), (g32["bake"], "in", 0),
                     bakenodes.COL_MATERIAL)
g32["shader"].set_material("Skin")
g32["out"].toggle_replace()
tab32.run_bake(g32["bake"])
wait_bake(tab32)
ok(len(rep32.replace_calls) == 1 and len(rep32.replace_calls[0]) == 1,
   "mapset: ⚠ three maps of one material make ONE replace row — the add-on "
   "reuses its image node, so without the dedupe the last type baked would "
   "decide what the shader shows (got %r)" % (rep32.replace_calls,))
ok(rep32.replace_calls[0][0]["bake_type"] == "NORMAL",
   "mapset: and it is the FIRST type in Blender's order, which is "
   "predictable — not whichever finished last")

# --- 24. the colour rule, checked across the WHOLE node set (2026-08-08)
# Marty asked for it again after the rebuild — his exe until that morning
# was five batches old and had no socket types at all, so the rule was real
# in source and absent from what he was clicking. This pins it per PAIR
# rather than on one example drag, so a new node cannot arrive with a
# socket that quietly connects to anything.
from PySide6.QtGui import QColor  # noqa: E402

tab40 = nodecanvas.NodeEditorTab(FakeBridge(), FakeWindow())
made = {kind: tab40.add_bake_node(kind) for kind in bakenodes.NODE_KINDS}
green = [(n, "out", 0) for k, n in made.items() if n.outputs
         and QColor(n.outputs[0][1]).name() == bakenodes.COL_MATERIAL.name()]
ok(len(green) == 4,
   "type: four nodes emit the green target colour — Bake, Bulk bake, "
   "Collection and Map set (got %d)" % len(green))
settings_in = (made["Bake settings"], "in", 0)
out_in = (made["Output image"], "in", 0)
ok(all(nodecanvas.sockets_compatible(g, settings_in) for g in green),
   "type: every green source may enter the Bake settings input")
ok(not any(nodecanvas.sockets_compatible(g, out_in) for g in green),
   "type: and NONE of them may enter the orange Output input")
baked_out = (made["Bake settings"], "out", 0)
ok(nodecanvas.sockets_compatible(baked_out, out_in)
   and not nodecanvas.sockets_compatible(baked_out, settings_in),
   "type: the orange baked result goes to the Output node and nowhere else")
ok(nodecanvas.socket_is_multi(made["Bake settings"], 0),
   "type: the settings input is the multi one (hollow, takes several)")
ok(not nodecanvas.socket_is_multi(made["Output image"], 0),
   "type: the Output input is NOT — one baked result, one output")

# --- 25. the Collection node (Marty, 2026-08-08)
col_bridge = ReplaceFakeBridge(
    targets=[{"object": "Rock", "materials": ["Moss", "Stone"]}],
    collections=[{"name": "Props", "depth": 0, "meshes": 2}])
tab41 = nodecanvas.NodeEditorTab(col_bridge, FakeWindow())
g41 = tab41.nodes
col41 = tab41.add_bake_node("Collection")
ok(isinstance(col41, bakenodes.CollectionNode)
   and col41.rows[0][0] == "Collection",
   "collection: the node carries a collection picker")
ok(col41.outputs[0][1].name() == bakenodes.COL_MATERIAL.name(),
   "collection: and emits the same green as the other sources")
col41.set_collection("Props")
tab41.scene.add_wire((col41, "out", 0), (g41["bake"], "in", 0),
                     bakenodes.COL_MATERIAL)
srcs41, _ms41 = bakenodes.upstream_sources(tab41.scene, g41["bake"])
ok(len(srcs41) == 2 and col41 in srcs41 and g41["shader"] in srcs41,
   "collection: BOTH it and the Bake node feed the settings node at once "
   "(got %d sources)" % len(srcs41))

# with the Bake node unpicked, the collection still runs
tab41.run_bake(g41["bake"])
wait_bake(tab41)
ok(sorted(c[0] for c in col_bridge.calls) == ["Moss", "Stone"],
   "collection: every material of every mesh in it bakes (got %r)"
   % [c[0] for c in col_bridge.calls])
ok(("collection", None, "Props") in col_bridge.target_calls,
   "collection: resolved through bake_targets at PRESS time (got %r)"
   % (col_bridge.target_calls,))
ok("⚠" not in tab41.status_label.text()
   and "Pick a material" not in tab41.status_label.text(),
   "collection: ⚠ the starting graph's UNPICKED Bake node does not sink the "
   "run — one source's refusal is not the whole press (got %r)"
   % tab41.status_label.text())

# a material named by BOTH sources is baked once
dup_bridge = ReplaceFakeBridge(
    targets=[{"object": "Rock", "materials": ["Moss"]}])
tab42 = nodecanvas.NodeEditorTab(dup_bridge, FakeWindow())
g42 = tab42.nodes
col42 = tab42.add_bake_node("Collection")
col42.set_collection("Props")
tab42.scene.add_wire((col42, "out", 0), (g42["bake"], "in", 0),
                     bakenodes.COL_MATERIAL)
g42["shader"].set_material("Moss")
tab42.run_bake(g42["bake"])
wait_bake(tab42)
ok(len([c for c in dup_bridge.calls if c[0] == "Moss"]) == 1,
   "collection: ⚠ a material both sources name is baked ONCE, not twice "
   "(got %r)" % [c[0] for c in dup_bridge.calls])

# an unpicked Collection node refuses in ITS OWN words when it is the one
# that was pressed
bare43 = ReplaceFakeBridge(targets=[])
tab43 = nodecanvas.NodeEditorTab(bare43, FakeWindow())
col43 = tab43.add_bake_node("Collection")
tab43.scene.remove_wire(tab43.scene.wires_into(tab43.nodes["bake"], 0)[0])
tab43.scene.add_wire((col43, "out", 0), (tab43.nodes["bake"], "in", 0),
                     bakenodes.COL_MATERIAL)
tab43.run_bake(tab43.nodes["bake"])
ok("Pick a collection on the Collection node" in tab43.status_label.text()
   and not bare43.calls,
   "collection: unpicked refuses before a request is spent (got %r)"
   % tab43.status_label.text())

# --- 26. shift + right-drag adds reroutes (Blender's Add Reroute)
tab44 = nodecanvas.NodeEditorTab(FakeBridge(), FakeWindow())
scene44 = tab44.scene
wire44 = scene44.wires_into(tab44.nodes["bake"], 0)[0]
before44 = len(scene44.wires)
colour44 = QColor(wire44.colour)
mid = wire44.path().pointAtPercent(0.5)
cut = QPainterPath(QPointF(mid.x() - 40, mid.y() - 40))
cut.lineTo(QPointF(mid.x() + 40, mid.y() + 40))
dot44 = scene44.split_with_reroute(wire44, cut)
ok(isinstance(dot44, nodecanvas.RerouteItem),
   "reroute: the gesture drops a reroute dot into the wire")
ok(wire44 not in scene44.wires and len(scene44.wires) == before44 + 1,
   "reroute: one wire became two (got %d, was %d)"
   % (len(scene44.wires), before44))
ok(dot44.typed and dot44.colour.name() == colour44.name(),
   "reroute: ⚠ the dot is born TYPED with the wire's colour — an untyped "
   "one would be a hole in the colour rule the moment it appeared")
ok(bakenodes.upstream_source(scene44, tab44.nodes["bake"])[0]
   is tab44.nodes["shader"],
   "reroute: and the graph walk still finds the source THROUGH it")
gap = ((dot44.pos().x() - mid.x()) ** 2
       + (dot44.pos().y() - mid.y()) ** 2) ** 0.5
ok(gap < 30,
   "reroute: the dot lands on the WIRE near the crossing, not on the "
   "gesture line (%.1f px away from the wire's midpoint)" % gap)

# the real event path: shift + RIGHT drag, and a shiftless one doing nothing
view44 = tab44.canvas
view44.resize(700, 500)
wire44b = scene44.wires_into(tab44.nodes["out"], 0)[0]
mid_b = wire44b.path().pointAtPercent(0.5)


def send_view(view, etype, scene_pt, button, buttons, mods):
    vp = view.mapFromScene(scene_pt)
    view.viewport().setFocus()
    handler = {QEvent.MouseButtonPress: view.mousePressEvent,
               QEvent.MouseMove: view.mouseMoveEvent,
               QEvent.MouseButtonRelease: view.mouseReleaseEvent}[etype]
    handler(QMouseEvent(etype, QPointF(vp),
                        QPointF(view.viewport().mapToGlobal(vp)),
                        button, buttons, mods))


count44 = len(scene44.wires)
for mods, label, expect in (
        (Qt.KeyboardModifier.NoModifier, "without shift", 0),
        (Qt.KeyboardModifier.ShiftModifier, "with shift", 1)):
    start = QPointF(mid_b.x() - 30, mid_b.y() - 30)
    send_view(view44, QEvent.MouseButtonPress, start,
              Qt.RightButton, Qt.RightButton, mods)
    send_view(view44, QEvent.MouseMove, QPointF(mid_b.x() + 30,
                                                mid_b.y() + 30),
              Qt.NoButton, Qt.RightButton, mods)
    send_view(view44, QEvent.MouseButtonRelease,
              QPointF(mid_b.x() + 30, mid_b.y() + 30),
              Qt.RightButton, Qt.NoButton, mods)
    grew = len(scene44.wires) - count44
    count44 = len(scene44.wires)
    ok(grew == expect,
       "reroute: a right-drag %s adds %d wire(s) (got %d)"
       % (label, expect, grew))

# --- 27. the ? and its help bubble
tab45 = nodecanvas.NodeEditorTab(FakeBridge(), FakeWindow())
for kind in bakenodes.NODE_KINDS:
    node = tab45.add_bake_node(kind)
    ok(bool(node.help_text) and node.help_rect() is not None,
       "help: the %s node has help text and a ? button" % kind)
plain45 = nodecanvas.NodeItem("Plain", "#4f8cff", rows=[("a", "b")])
ok(plain45.help_text is None and plain45.help_rect() is None,
   "help: ⚠ a node with nothing to say grows NO ? — the mechanics fixture "
   "stays free of a button it never uses")

bake45 = tab45.nodes["bake"]
bubble45 = tab45.scene.toggle_help(bake45)
ok(bubble45 is not None and tab45.scene.help_bubble is bubble45
   and bubble45.scene() is tab45.scene,
   "help: clicking the ? opens a bubble in the scene")
ok(bubble45.y() + bubble45.h <= bake45.y(),
   "help: it sits ABOVE the node (bubble ends %.0f, node starts %.0f)"
   % (bubble45.y() + bubble45.h, bake45.y()))
other45 = tab45.scene.toggle_help(tab45.nodes["out"])
ok(other45 is not None and bubble45.scene() is None,
   "help: ⚠ opening another node's help CLOSES the first — one at a time, "
   "or two panels overlap with no way to shut either")
ok(tab45.scene.toggle_help(tab45.nodes["out"]) is None
   and tab45.scene.help_bubble is None,
   "help: and clicking the same ? again closes it")

moved45 = tab45.scene.toggle_help(bake45)
before_y = moved45.y()
bake45.moveBy(0, 60)
ok(abs(moved45.y() - (before_y + 60)) < 0.5,
   "help: the bubble FOLLOWS its node (a panel left behind reads as a bug)")
tab45.scene.remove_node(bake45)
ok(tab45.scene.help_bubble is None,
   "help: deleting the node takes its bubble with it")

for style in ("A", "B", "C", "D"):
    probe = nodecanvas.HelpBubble(tab45.nodes["out"], "Some text.",
                                  style=style)
    ok(probe.h > 20 and probe.boundingRect().width() > nodecanvas.HELP_W,
       "help: style %s lays out with a real height (%.0f px)"
       % (style, probe.h))

# the tick really is painted on the canvas, not just in the stylesheet
from PySide6.QtCore import QRectF as _QRectF  # noqa: E402
from PySide6.QtGui import QPainter as _QPainter  # noqa: E402

shot = _QImage(40, 40, _QImage.Format_ARGB32)
painted = []
for state in (False, True):
    shot.fill(0)
    p = _QPainter(shot)
    nodecanvas.draw_check(p, _QRectF(10, 10, 20, 20), state)
    p.end()
    painted.append(sum(1 for y in range(40) for x in range(40)
                       if shot.pixelColor(x, y).lightness() > 200))
ok(painted[1] > painted[0],
   "tick: a ticked box paints bright pixels an unticked one does not")

print("%d passed, %d failed" % (len(PASS), len(FAIL)), flush=True)
sys.exit(1 if FAIL else 0)
