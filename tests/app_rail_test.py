# The section rail: the app's navigation since 2026-08-14, when the eleven-tab
# strip across the top was replaced (Marty: it "looks too cheap"). The strip's
# QTabBar is kept and HIDDEN, so what this suite really guards is the seam —
# the rail and the tab widget must never disagree about which section is open,
# and the tab TEXT must stay the internal key while the rail carries the label.
import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

sys.path.insert(0, os.path.join(_ROOT, "app"))

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QColor  # noqa: E402
from PySide6.QtWidgets import QApplication, QPushButton  # noqa: E402

import config  # noqa: E402

# Never Marty's real config.json - the section choice is persisted on change.
config.CONFIG_PATH = os.path.join(tempfile.mkdtemp(prefix="madi_rail_"),
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

win = mainmod.MainWindow()
win.resize(1300, 760)
win.show()
app.processEvents()
rail = win.section_rail
tabs = win.main_tabs

# ---------------------------------------------------------------- structure

titles = [tabs.tabText(i) for i in range(tabs.count())]
ok(all(rail.entry_for(t) is not None for t in titles),
   "every section has a rail entry (%d sections)" % len(titles))
ok([rail.entry_for(t).data(0, Qt.UserRole) for t in titles]
   == list(range(len(titles))),
   "and each entry drives its own tab index, in order")

# ⚠ A section missing from SECTION_META still gets an entry (unglyphed,
# ungrouped) so a new tab can never leave the app unnavigable - but shipping
# one un-iconed is an oversight, not a design, so it is pinned here.
missing = [t for t in titles if t not in mainmod.SECTION_META]
ok(not missing, "every shipped section is described in SECTION_META (%s)"
   % (missing or "none missing"))
stale = [t for t in mainmod.SECTION_META if t not in titles]
ok(not stale,
   "and SECTION_META has no entries for sections that do not exist (%s)"
   % (stale or "none stale"))

# ⚠ GROUPS MUST BE CONTIGUOUS IN TAB ORDER. The rail files each section under
# its heading as it walks the tabs, so a group whose members are not
# neighbours would silently appear TWICE under the same name.
seen, runs = [], []
for title in titles:
    group = mainmod.SECTION_META.get(title, ("", ""))[1]
    if not runs or runs[-1] != group:
        runs.append(group)
runs = [g for g in runs if g]
ok(len(runs) == len(set(runs)),
   "group headings are contiguous in tab order - none appears twice (%s)"
   % runs)
headers = [rail.topLevelItem(i).text(0) for i in range(rail.topLevelItemCount())
           if rail.topLevelItem(i).childCount()]
ok(all(not (h.flags() & Qt.ItemIsSelectable)
       for h in (rail.topLevelItem(i) for i in range(rail.topLevelItemCount()))
       if h.childCount()),
   "group headers are not selectable, so the keyboard walks tools only (%s)"
   % headers)

# ------------------------------------------------------------------- wiring

target = titles.index("Optimization")
rail.setCurrentItem(rail.entry_for("Optimization"))
app.processEvents()
ok(tabs.currentIndex() == target,
   "picking a rail entry opens that section (%d)" % tabs.currentIndex())

other = titles.index("Rendering")
tabs.setCurrentIndex(other)
app.processEvents()
ok(rail.currentItem() is rail.entry_for("Rendering"),
   "and changing the section moves the rail selection back the other way")

# ⚠ THE LOOP MUST NOT RING. Both directions are connected, so a naive
# implementation re-emits forever; `set_current_index` blocks signals. If this
# ever recurses the suite dies rather than fails, which is its own signal.
for name in ("Physics", "Studio Library", "MadiRef", "Studio Library"):
    tabs.setCurrentIndex(titles.index(name))
    app.processEvents()
ok(tabs.currentIndex() == titles.index("Studio Library")
   and rail.currentItem() is rail.entry_for("Studio Library"),
   "rapid section changes settle with both halves agreeing")

# ---------------------------------------------------------------- the label

# ⚠ THE TAB TEXT IS THE KEY, THE RAIL LABEL IS THE NAME. Marty renames
# sections through Developer mode: edit; the tint, TAB_TEXT_COLORS, devedit's
# own store and four suites all look tabs up BY TEXT, so a rename must not
# reach it.
entry = rail.entry_for("NSFW Tools")
entry.setText(0, "Toys")
app.processEvents()
ok(tabs.tabText(titles.index("NSFW Tools")) == "NSFW Tools",
   "renaming a rail entry does NOT change the tab text it is keyed by")
ok(rail.entry_for("NSFW Tools") is entry,
   "and the entry is still findable by its canonical title")
entry.setText(0, "NSFW Tools")
app.processEvents()

# ------------------------------------------------------------------- glyphs


def glyph_hits(pixmap, hexcolor, tol=30):
    """Pixels in *pixmap* close to *hexcolor* - the icon really is that ink."""
    want = QColor(hexcolor)
    image = pixmap.toImage()
    hits = 0
    for x in range(image.width()):
        for y in range(image.height()):
            pixel = image.pixelColor(x, y)
            if pixel.alpha() > 120 and (
                    abs(pixel.red() - want.red()) < tol
                    and abs(pixel.green() - want.green()) < tol
                    and abs(pixel.blue() - want.blue()) < tol):
                hits += 1
    return hits


ok(all(not rail.entry_for(t).icon(0).isNull() for t in titles),
   "every section carries a drawn glyph")
ok(glyph_hits(icons.pixmap("library", 18, theme.ACCENT), theme.ACCENT) > 8,
   "a glyph really is painted in the colour it is asked for")
ok(glyph_hits(icons.pixmap("library", 18, theme.ACCENT), theme.TEXT_DIM) == 0,
   "and only in that colour - the accent glyph has no dim ink in it")

# ⚠ Unknown names must not raise. An icon is decoration; a typo is not worth
# taking a tab down for, and the rail asks for a glyph named after a tab key.
ok(not icons.pixmap("no_such_glyph", 18).isNull(),
   "an unknown glyph name yields a blank pixmap rather than an exception")

# ⚠ THE CACHE IS KEYED BY COLOUR. `theme.apply_theme` REBINDS the palette, so a
# cache keyed by (name, size) alone would serve the previous theme's glyphs
# forever - the same class of bug as nodecanvas's cached QColors.
before = icons.pixmap("physics", 18, "#ff0000")
after = icons.pixmap("physics", 18, "#00ff00")
ok(glyph_hits(before, "#ff0000") > 5 and glyph_hits(after, "#00ff00") > 5,
   "two colours of the same glyph do not collide in the cache")

# ------------------------------------------------------- group headings open

# ⚠ THE HEADINGS WERE COLLAPSIBLE AND NOTHING SAID SO (Marty, 2026-08-15, with
# ANIMATION / NODES / SCENE circled in a screenshot). `setRootIsDecorated(False)`
# means Qt draws no branch arrow, and Qt's default is DOUBLE click — so the one
# affordance and the one gesture were both missing. The chevron is a drawn icon
# in the heading's own icon slot, which is where the tools' glyphs sit, so
# turning decoration back on (and indenting every tool row) was not needed.
_groups = [rail.topLevelItem(i) for i in range(rail.topLevelItemCount())
           if rail.topLevelItem(i).childCount()]
ok(len(_groups) == 3,
   "the rail has the three group headings (%d)" % len(_groups))
ok(all(not g.icon(0).isNull() for g in _groups),
   "every heading carries a chevron, so it reads as something that opens")


def chevron_shape(item):
    """The ink's bounding box. ⚠ Counting PIXELS cannot tell these apart — a
    chevron turned 90 degrees has exactly as much ink (measured: 14 vs 14).
    Its SHAPE is the thing that differs: open points down and is wider than it
    is tall, shut points right and is taller than it is wide."""
    image = item.icon(0).pixmap(rail.ICON, rail.ICON).toImage()
    xs, ys = [], []
    for x in range(image.width()):
        for y in range(image.height()):
            if image.pixelColor(x, y).alpha() > 120:
                xs.append(x)
                ys.append(y)
    if not xs:
        return 0, 0
    return max(xs) - min(xs), max(ys) - min(ys)


# ⚠ THE HEADING SITS ON A FILLED BAR (Marty picked "B - filled bar" from six
# rendered variants, 2026-08-15). Sampled away from the text and the chevron,
# so this measures the BAR rather than the ink on it.
_bar_shot = rail.grab().toImage()
_bar_rect = rail.visualItemRect(_groups[0])
_bar_px = _bar_shot.pixelColor(_bar_rect.right() - 20,
                               _bar_rect.center().y()).name()
ok(_bar_px == theme.PANEL2,
   "a group heading paints the filled bar %s (got %s)"
   % (theme.PANEL2, _bar_px))
_tool_rect = rail.visualItemRect(rail.entry_for("Anim Layers"))
_tool_px = _bar_shot.pixelColor(_tool_rect.right() - 20,
                                _tool_rect.center().y()).name()
ok(_tool_px != theme.PANEL2,
   "and a TOOL row does not - that contrast is the whole point (%s)"
   % _tool_px)

_g = _groups[0]
ok(_g.isExpanded(), "headings start open")
_ow, _oh = chevron_shape(_g)
_g.setExpanded(False)
app.processEvents()
_sw, _sh = chevron_shape(_g)
ok(_ow > _oh, "open, the chevron points DOWN - wider than tall (%dx%d)"
   % (_ow, _oh))
ok(_sh > _sw, "shut, it points RIGHT - taller than wide (%dx%d)" % (_sw, _sh))
_g.setExpanded(True)
app.processEvents()

# ⚠ Driven by a REAL mouse click on the viewport, not by calling the slot:
# the whole complaint was about what a click does, and `setExpandsOnDoubleClick`
# plus the signal wiring is exactly the part a direct call would skip.
from PySide6.QtCore import QPoint  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402

_rect = rail.visualItemRect(_g)
QTest.mouseClick(rail.viewport(), Qt.LeftButton, Qt.NoModifier, _rect.center())
app.processEvents()
ok(not _g.isExpanded(),
   "ONE click on a heading closes it")
QTest.mouseClick(rail.viewport(), Qt.LeftButton, Qt.NoModifier, _rect.center())
app.processEvents()
ok(_g.isExpanded(), "and one more opens it again")

# ⚠ A tool entry must be untouched by all this - it selects, it does not toggle.
_tool = rail.entry_for("Bone picker")
_trect = rail.visualItemRect(_tool)
QTest.mouseClick(rail.viewport(), Qt.LeftButton, Qt.NoModifier, _trect.center())
app.processEvents()
ok(tabs.currentIndex() == titles.index("Bone picker"),
   "clicking a TOOL still just opens it")
ok(all(g.isExpanded() for g in _groups),
   "and collapses nothing")

# ------------------------------------------------------------ squeezed rail

# ⚠ THE RAIL IS NOT FIXED-WIDTH, and that is what lets the window get small:
# `setFixedWidth` makes min == max, so 172 px of rail was 172 px of the
# WINDOW's minimum on every tab. It still ASKS for 172 (sizeHint), so nothing
# moves at normal sizes.
ok(rail.minimumWidth() <= rail.COMPACT and rail.maximumWidth() == rail.WIDTH,
   "the rail can be squeezed to %d and still wants %d (min %d, max %d)"
   % (rail.COMPACT, rail.WIDTH, rail.minimumWidth(), rail.maximumWidth()))
ok(rail.sizeHint().width() == rail.WIDTH,
   "its sizeHint is the full width, not whatever the items happen to measure")

# ⚠ AND THE LABELS GO RATHER THAN CLIP. Qt truncates "Studio Library" to "Stu"
# with no ellipsis, and eleven fragments read as a broken widget - looked at in
# a rendered shot, which is the only way that shows.
# ⚠ Driven by resizing the WINDOW, not the rail: the rail lives in a layout
# that re-imposes its geometry, so poking its width directly proves nothing
# about what a person dragging the window edge would see.
_before = rail.entry_for("Studio Library").text(0)
win.resize(win.minimumSizeHint().width(), win.minimumSizeHint().height())
app.processEvents()
ok(rail.width() < rail.WIDTH,
   "at the window's minimum the rail really is squeezed (%d px)" % rail.width())
ok(rail.entry_for("Studio Library").text(0) == "",
   "squeezed to icon width the labels drop out entirely")
ok(rail.entry_for("Studio Library").toolTip(0) == "Studio Library",
   "and the name moves to the tooltip, so nothing is lost")
win.resize(1300, 760)
app.processEvents()
ok(rail.entry_for("Studio Library").text(0) == _before,
   "widened again, the label comes back exactly as it was (%r)" % _before)

# ⚠ A RENAME MUST SURVIVE THE ROUND TRIP - the parked label is the CURRENT
# text, not the canonical title, because Marty renames these.
_e = rail.entry_for("Physics")
_e.setText(0, "Wobble")
win.resize(win.minimumSizeHint().width(), win.minimumSizeHint().height())
app.processEvents()
win.resize(1300, 760)
app.processEvents()
ok(_e.text(0) == "Wobble",
   "a renamed entry keeps its new name across a squeeze (%r)" % _e.text(0))
_e.setText(0, "Physics")

# --------------------------------------------------------------- the emoji

# ⚠ THE POINT OF THE RESKIN. Every one of these was a BUTTON LABEL: they are
# font, so they ignored the palette and restyled themselves with a Windows
# update. If one comes back, it comes back looking like a prototype.
EMOJI = ("\u2699", "\u27f3", "\U0001f3ac", "\u25b6", "\U0001f50d", "\u2b07")
offenders = []
for button in win.findChildren(QPushButton):
    text = button.text() or ""
    for glyph in EMOJI:
        if glyph in text:
            offenders.append((glyph, text))
ok(not offenders,
   "no button wears an emoji as its label any more (%s)"
   % (offenders or "clean"))

print("%d passed, %d failed" % (len(PASS), len(FAIL)))
sys.exit(1 if FAIL else 0)
