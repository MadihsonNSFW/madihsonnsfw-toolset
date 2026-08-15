# The window chrome: our own title bar in place of Windows' (2026-08-15).
#
# Marty asked for edges styled like a modern desktop app rather than a default
# Windows one, and picked concept A, the seamless rail. What
# this suite guards is mostly INVISIBLE, which is exactly why it is here:
#
#   * the hit test decides whether the three window buttons receive their
#     clicks at all — get it backwards and the window either cannot be dragged
#     or cannot be closed, and neither shows up in a screenshot;
#   * the rail header must not become the window's minimum width (it did:
#     549 -> 682 px, invisible on screen and caught only by measuring);
#   * the title bar must echo the RAIL's label, not the tab text, which is
#     still the internal key.
import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

sys.path.insert(0, os.path.join(_ROOT, "app"))

from PySide6.QtCore import QPoint, Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QLabel  # noqa: E402

import config  # noqa: E402

# Never Marty's real config.json — building MainWindow persists the section.
config.CONFIG_PATH = os.path.join(tempfile.mkdtemp(prefix="madi_chrome_"),
                                  "config.json")

# ⚠ A DEAD PORT, DELIBERATELY. With no config file the DEFAULTS apply, and the
# default port is the REAL bridge port - so this suite connected to whatever
# Blender Marty happened to have open and measured a different app. On
# 2026-08-15 that turned three suites red for a reason unrelated to the change
# under test: the live add-on was a version behind, so the status bar grew a
# 172 px "Update add-on" button and the window's minimum width went 632 -> 810.
_io_ = __import__("io")
_json_ = __import__("json")
_io_.open(config.CONFIG_PATH, "w", encoding="utf-8").write(
    _json_.dumps({"port": 9998}))

import chrome  # noqa: E402
import icons  # noqa: E402
import theme  # noqa: E402
import widgets  # noqa: E402

PASS, FAIL = [], []


def ok(cond, label):
    (PASS if cond else FAIL).append(label)
    print(("ok   " if cond else "FAIL ") + label, flush=True)


app = QApplication.instance() or QApplication([])
app.setStyleSheet(theme.QSS)
widgets.install_no_wheel(app)

import main as mainmod  # noqa: E402


# ---------------------------------------------------------------- the mark

def alpha_at(pix, fx, fy):
    """Alpha of one pixel, addressed as a fraction of the glyph."""
    image = pix.toImage()
    x = int(image.width() * fx)
    y = int(image.height() * fy)
    return image.pixelColor(x, y).alpha()


mark = icons.pixmap("appmark", 48, theme.ACCENT, ratio=1.0)
ok(alpha_at(mark, 0.5, 0.5) == 0,
   "appmark: the notch is genuinely PUNCHED OUT — the middle pixel is "
   "transparent, not painted in the background colour, so the mark can sit "
   "on any surface (alpha %d)" % alpha_at(mark, 0.5, 0.5))
ok(alpha_at(mark, 0.5, 0.15) > 200,
   "appmark: and the tile itself is solid above the notch (alpha %d)"
   % alpha_at(mark, 0.5, 0.15))

for name in ("win_min", "win_max", "win_restore", "win_close"):
    pix = icons.pixmap(name, 32, theme.TEXT, ratio=1.0)
    image = pix.toImage()
    ink = sum(1 for y in range(image.height()) for x in range(image.width())
              if image.pixelColor(x, y).alpha() > 40)
    ok(ink > 20, "glyph %s draws something (%d inked pixels)" % (name, ink))

# ⚠ Restore-down must not be the same shape as maximise. They are one button
# swapping between two states, and if the swap were a no-op nothing about the
# window would look wrong — it would just always claim to maximise.
maxed = icons.pixmap("win_max", 32, theme.TEXT, ratio=1.0).toImage()
restore = icons.pixmap("win_restore", 32, theme.TEXT, ratio=1.0).toImage()
differing = sum(1 for y in range(32) for x in range(32)
                if (maxed.pixelColor(x, y).alpha() > 40)
                != (restore.pixelColor(x, y).alpha() > 40))
ok(differing > 20,
   "win_max and win_restore are visibly different shapes (%d pixels differ)"
   % differing)


# ------------------------------------------------------------ the two bars

win = mainmod.MainWindow()
header = win.rail_header
strip = win.title_strip

ok(header.minimumSizeHint().width() == 0,
   "rail header: hints ZERO minimum width. A parent layout sizes a child by "
   "its minimumSizeHint (qSmartMinSize) and setMinimumWidth(0) cannot lower "
   "that — the wordmark's 189 px hint became a 172 px floor on the rail "
   "column and pushed the window's minimum 549 -> 682")
ok(header.maximumWidth() == widgets.SectionRail.WIDTH,
   "rail header: never wider than the rail it sits on")
ok(header.height() == chrome.BAR_H and strip.height() == chrome.BAR_H,
   "both halves of the bar are the same height (%d), so the app mark lines "
   "up with the window buttons across the seam" % chrome.BAR_H)

# (the wordmark's compaction is checked further down, once the window is
# actually on screen — see the note there)

# What the bar costs. Measured against main_tabs rather than against a
# remembered number: the pages' own minimums drift as tools are added.
shell = win.centralWidget()
page_column = shell.layout().itemAt(1).widget()
grew_h = (page_column.minimumSizeHint().height()
          - win.main_tabs.minimumSizeHint().height())
grew_w = (page_column.minimumSizeHint().width()
          - win.main_tabs.minimumSizeHint().width())
ok(grew_h == chrome.BAR_H,
   "the title bar costs exactly its own height (%d px) of the window's "
   "minimum — and it replaces a Windows title bar of about 32, so on screen "
   "it is close to a wash" % grew_h)
ok(grew_w == 0,
   "and NOTHING of the minimum width (%d): the section label is elided and "
   "the buttons are narrower than the pages behind them" % grew_w)

rail_column = shell.layout().itemAt(0).widget()
ok(rail_column.minimumSizeHint().width() == widgets.SectionRail.COMPACT,
   "the rail column still squeezes to icon width (%d px) with the header on "
   "top of it" % rail_column.minimumSizeHint().width())


# ------------------------------------------------------- where you are

ok(strip.section.full_text() == win.section_rail.label_for(
       win.main_tabs.currentIndex()),
   "title bar: says what the rail says (%r)" % strip.section.full_text())

second = 1
win.main_tabs.setCurrentIndex(second)
ok(strip.section.full_text() == win.section_rail.label_for(second),
   "title bar: and follows the section change (%r)" % strip.section.full_text())

# A rename lands on the RAIL entry, and the tab text stays the key.
entry = win.section_rail.entry_for(win.main_tabs.tabText(second))
entry.setText(0, "Renamed Section")
win._sync_title_section(second)
ok(strip.section.full_text() == "Renamed Section",
   "title bar: a Developer-mode rename reaches it — it reads the rail entry, "
   "not tabText (which is still %r)" % win.main_tabs.tabText(second))
ok(win.section_rail.label_for(second) == "Renamed Section",
   "label_for: returns the renamed label")

# Squeezed to icons the entry's own text is deliberately emptied and parked.
win.section_rail._show_labels(False)
ok(win.section_rail.label_for(second) == "Renamed Section",
   "label_for: still answers while the rail is squeezed to icons, where the "
   "entry's visible text is empty — the title bar has its own room")
win.section_rail._show_labels(True)
entry.setText(0, win.main_tabs.tabText(second))
win._sync_title_section(second)

ok(win.section_rail.label_for(999) == "",
   "label_for: an unknown index is empty, not an exception")


# --------------------------------------------------------- window buttons

controls = strip.controls
ok(controls.maximise.toolTip() == "Maximise",
   "window buttons: the middle one offers to maximise a normal window")


class _FakeMaximised(object):
    """Windows can maximise the window itself — by a double-click on the bar
    or a drag to the top edge — so the button must read the WINDOW's state."""

    def isMaximized(self):
        return True


controls._window = _FakeMaximised()
controls.sync()
ok(controls.maximise.toolTip() == "Restore down",
   "window buttons: and flips to restore when the window is maximised by "
   "something other than that button")
controls._window = win
controls.sync()
ok(controls.maximise.toolTip() == "Maximise",
   "window buttons: and back again")


# ------------------------------------------------------------- hit testing

# ⚠ THE ONE THAT DECIDES WHETHER THE BUTTONS WORK. Windows reports the whole
# top strip as caption once the non-client area is gone, so Qt only sees a
# click there if we answer HTCLIENT for it.
win.resize(1180, 720)
win.show()
for _ in range(3):
    app.processEvents()


# --------------------------------------------------- the wordmark, on screen

# ⚠ DRIVEN BY RESIZING THE **WINDOW**, and only now that it is shown. Two
# separate traps, both of which produced a green check for nothing:
#   * `resize()` on a widget that has never been shown does NOT deliver a
#     resize event — Qt defers it to show time — so the header's own handler
#     never ran and the wordmark never hid;
#   * `isVisible()` is False for every child of a hidden window whatever its
#     own state, so asserting `not isVisible()` passed before the code existed.
# Resizing the header directly is no good either: the layout owns its width
# and snaps it straight back.
def settle():
    for _ in range(3):
        app.processEvents()


settle()
ok(not header.name.isHidden(),
   "rail header: the wordmark shows at full width (window %d px)"
   % win.width())
win.resize(win.minimumSizeHint().width(), 700)
settle()
ok(header.name.isHidden(),
   "rail header: and goes away when the window is squeezed to the icons-only "
   "rail, at the same cutoff the rail uses (%d) so the column compacts as one "
   "thing rather than in two steps" % widgets.SectionRail.TEXT_CUTOFF)
win.resize(1180, 720)
settle()
ok(not header.name.isHidden(),
   "rail header: and comes back when there is room again")

probe = chrome._Chrome(win, 0, (strip, header))
ratio = float(win.devicePixelRatioF() or 1.0)


def at(widget, dx, dy):
    """Physical coords of a point inside `widget`, for classify()."""
    top_left = widget.mapTo(win, QPoint(0, 0))
    return (int((top_left.x() + dx) * ratio), int((top_left.y() + dy) * ratio))


W = int(win.width() * ratio)
H = int(win.height() * ratio)

px, py = at(strip, strip.width() // 2, chrome.BAR_H // 2)
ok(probe.classify(px, py, W, H, False) == chrome.HTCAPTION,
   "hit test: bare title-bar background is CAPTION, so Windows itself does "
   "the dragging — which is what buys Aero Snap and double-click-to-maximise")

button = controls.close_button
px, py = at(button, button.width() // 2, button.height() // 2)
ok(probe.classify(px, py, W, H, False) == chrome.HTCLIENT,
   "hit test: the close button is CLIENT, or Qt never sees the click and the "
   "window cannot be closed at all")

px, py = at(header, 30, chrome.BAR_H // 2)
ok(probe.classify(px, py, W, H, False) == chrome.HTCAPTION,
   "hit test: the app mark drags the window too — it is the first thing "
   "anybody grabs")

label = strip.section
px, py = at(label, 4, label.height() // 2)
ok(probe.classify(px, py, W, H, False) == chrome.HTCAPTION,
   "hit test: the section label is not interactive, so it drags rather than "
   "swallowing the press")

px, py = at(win.main_tabs, 60, 80)
ok(probe.classify(px, py, W, H, False) == chrome.HTCLIENT,
   "hit test: the page below the bar is ordinary client area")

edges = {
    (2, 2): chrome.HTTOPLEFT,
    (W - 2, 2): chrome.HTTOPRIGHT,
    (2, H - 2): chrome.HTBOTTOMLEFT,
    (W - 2, H - 2): chrome.HTBOTTOMRIGHT,
    (2, H // 2): chrome.HTLEFT,
    (W - 2, H // 2): chrome.HTRIGHT,
    (W // 2, 1): chrome.HTTOP,
    (W // 2, H - 1): chrome.HTBOTTOM,
}
wrong = [(pt, probe.classify(pt[0], pt[1], W, H, False), want)
         for pt, want in edges.items()
         if probe.classify(pt[0], pt[1], W, H, False) != want]
ok(not wrong,
   "hit test: all eight resize edges answer correctly — with no non-client "
   "area left, these are the only reason the window can be resized (%s)"
   % (wrong or "all eight"))

# ⚠ The top edge overlaps the title bar. Resize has to win there or the
# window can never be made shorter from the top.
ok(probe.classify(W // 2, 1, W, H, False) == chrome.HTTOP,
   "hit test: the resize edge beats the title bar where they overlap")

# Maximised there is nothing to resize, and a 6 px band of dead corner at the
# top of a maximised window is a button people would miss.
maxed_edges = {(2, 2), (W - 2, 2), (2, H - 2), (W // 2, 1)}
ok(all(probe.classify(x, y, W, H, True) in (chrome.HTCLIENT, chrome.HTCAPTION)
       for x, y in maxed_edges),
   "hit test: no resize edges while maximised")


# ----------------------------------------------------------- the fallback

# On a machine where the native hook fails the app must still be usable, so
# `install` says so rather than leaving a chromeless, undraggable window.
_real = chrome.WINDOWS
chrome.WINDOWS = False
ok(chrome.install(win, (strip,)) is False,
   "fallback: install() reports failure instead of raising when the platform "
   "cannot support it — MainWindow then keeps the Windows title bar and hides "
   "its own buttons, rather than showing two sets")
ok(chrome.available() is False, "fallback: available() agrees")
chrome.WINDOWS = _real
ok(chrome.available() is True, "chrome is available on this platform")

ok(chrome.reinstall(QLabel()) is False,
   "reinstall: a window that never had the chrome is a no-op, not a crash")
ok(getattr(win, "_madi_chrome_drag", None),
   "install remembered the drag widgets, so reinstall() can put the chrome "
   "back after the pin button re-creates the native window")

print("%d passed, %d failed" % (len(PASS), len(FAIL)))
sys.exit(1 if FAIL else 0)
