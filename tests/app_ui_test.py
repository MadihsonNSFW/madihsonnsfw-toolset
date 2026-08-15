# App shell polish, offscreen (2026-08-03, all four Marty-reported):
#   1. tool-rail group headers must be READABLE (they were painted with the
#      palette's near-black `mid()`),
#   2. the Physics rail must list Bones/Bone Jiggle above Cage/Proxy Cage,
#   3. the mouse wheel must NEVER change a setting in ANY tab — and must still
#      scroll the panel,
#   4. the always-on-top pin button.
import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

sys.path.insert(0, os.path.join(_ROOT, "app"))
sys.path.insert(0, os.path.join(_ROOT, "tests"))

import _branding  # noqa: E402

_FORBIDDEN, _STUDIED = _branding.words(_ROOT)

from PySide6.QtCore import QPoint, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QColor, QWheelEvent  # noqa: E402
from PySide6.QtWidgets import (QAbstractScrollArea,  # noqa: E402
                               QAbstractSpinBox, QApplication,
                               QCheckBox, QGridLayout, QLabel,
                               QComboBox, QDoubleSpinBox, QListWidget,
                               QPushButton, QScrollArea, QSlider, QSpinBox,
                               QTabWidget, QVBoxLayout, QWidget)

import config  # noqa: E402

# Never write Marty's real config.json — the pin button persists on toggle.
config.CONFIG_PATH = os.path.join(tempfile.mkdtemp(prefix="madi_ui_"),
                                  "config.json")

# ⚠⚠ **A DEAD PORT, DELIBERATELY.** With no config file the defaults apply,
# and the default port is the REAL bridge port — so whenever Marty happened to
# have Blender open, this suite connected to it and measured a different app.
# On 2026-08-15 that turned the window-minimum check red for a reason that had
# nothing to do with the change under test: the live add-on was a version
# behind, so the status bar grew a 172 px **"Update add-on"** button and the
# minimum went 632 -> 810. A ceiling test whose answer depends on what is
# running elsewhere on the machine is worse than no ceiling test.
#
# (That 172 px is real, not an artefact: on a user's machine with an outdated
# add-on the window genuinely cannot be narrowed as far. Noted in PERF_PLAN.md
# rather than fixed here.)
import json as _json  # noqa: E402

_io = __import__("io")
_io.open(config.CONFIG_PATH, "w", encoding="utf-8").write(
    _json.dumps({"port": 9998}))

import theme  # noqa: E402
import widgets  # noqa: E402
import rendering as renderingmod  # noqa: E402

PASS, FAIL = [], []


def ok(cond, label):
    (PASS if cond else FAIL).append(label)
    print(("ok   " if cond else "FAIL ") + label, flush=True)


app = QApplication.instance() or QApplication([])
app.setStyleSheet(theme.QSS)
widgets.install_no_wheel(app)


def luminance(rgb):
    def chan(c):
        c /= 255.0
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (chan(v) for v in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(fg_hex, bg_hex):
    def rgb(h):
        h = h.lstrip("#")
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
    l1, l2 = sorted((luminance(rgb(fg_hex)), luminance(rgb(bg_hex))),
                    reverse=True)
    return (l1 + 0.05) / (l2 + 0.05)


# ------------------------------------------------- 1. readable rail headers
page = renderingmod.RenderingPage(None, None)
page.add_tool(QWidget(), "Tool A", group="Bones")
head = page.rail.topLevelItem(0)
col = head.foreground(0).color()
ratio = contrast(col.name(), theme.PANEL)
ok(col.name().lower() == theme.TEXT_HEAD.lower(),
   "header: painted with theme.TEXT_HEAD, not the palette's near-black mid()")
ok(ratio >= 4.5,
   "header: %s on %s is %.2f:1, at or above the 4.5:1 readability threshold"
   % (col.name(), theme.PANEL, ratio))
ok(head.font(0).bold(), "header: bold, so it reads as a heading")
ok(contrast(theme.TEXT_HEAD, theme.PANEL) > contrast(theme.TEXT_DIM,
                                                     theme.PANEL),
   "header: TEXT_HEAD is genuinely brighter than the old TEXT_DIM")
ok(not (head.flags() & Qt.ItemIsSelectable),
   "header: still not selectable — it is a heading, not a tool")

# -------------------------------------------------------- 2. Physics order
import main as mainmod  # noqa: E402

win = mainmod.MainWindow()
labels = []
rail = win.physics.rail
for i in range(rail.topLevelItemCount()):
    top = rail.topLevelItem(i)
    labels.append(top.text(0))
    for j in range(top.childCount()):
        labels.append("  " + top.child(j).text(0))
ok(labels == ["BONES", "  Bone Jiggle"],
   "order: the Physics rail is Bone Jiggle under BONES and nothing else - "
   "the Proxy Cage was removed outright 2026-08-14 (%r)" % labels)

# ------------------------------------------- 3. the wheel scrolls, never edits
def wheel(widget, delta=-120):
    ev = QWheelEvent(QPointF(5, 5), widget.mapToGlobal(QPoint(5, 5)),
                     QPoint(0, delta), QPoint(0, delta), Qt.NoButton,
                     Qt.NoModifier, Qt.NoScrollPhase, False)
    app.sendEvent(widget, ev)


host = QScrollArea()
inner = QWidget()
lay = QVBoxLayout(inner)
combo = QComboBox()
combo.addItems(["one", "two", "three"])
spin = QSpinBox()
spin.setRange(0, 100)
spin.setValue(5)
dspin = QDoubleSpinBox()
dspin.setRange(0.0, 10.0)
dspin.setValue(2.5)
slider = QSlider(Qt.Horizontal)
slider.setRange(0, 100)
slider.setValue(50)
tabs = QTabWidget()
tabs.addTab(QWidget(), "A")
tabs.addTab(QWidget(), "B")
for w in (combo, spin, dspin, slider, tabs):
    lay.addWidget(w)
lay.addSpacing(4000)                      # make it genuinely scrollable
host.setWidget(inner)
host.setWidgetResizable(True)
host.resize(300, 200)
host.show()
app.processEvents()

# ⚠ **GUARDED EXPLICITLY SINCE 2026-08-15.** The filter used to sit on the
# QApplication, which covered this ad-hoc panel for free — and cost 380 ms of
# every window build, because it saw every event in the process (PERF_PLAN.md).
# It now goes on the widgets, so anything built outside MainWindow has to ask.
widgets.guard_wheel(host)

before = host.verticalScrollBar().value()
# NOTE: these are PLAIN Qt widgets, not NoScrollComboBox/ValueSlider. That is
# the point — the guarantee is the guard, so it holds for widgets nobody has
# converted.
for w, name in ((combo, "QComboBox"), (spin, "QSpinBox"),
                (dspin, "QDoubleSpinBox"), (slider, "QSlider")):
    start = w.currentIndex() if isinstance(w, QComboBox) else w.value()
    # ⚠ **ONE EVENT, NOT A DOWN-THEN-UP PAIR.** This check used to wheel down
    # and then straight back up, so a *working* wheel returned the widget to
    # its starting value and the check passed either way — it went on passing
    # with the guard removed entirely, which is how it was found (2026-08-15).
    wheel(w)
    now = w.currentIndex() if isinstance(w, QComboBox) else w.value()
    ok(now == start, "wheel: a plain %s is not changed by ONE wheel notch "
                     "(%r -> %r)" % (name, start, now))

start_tab = tabs.currentIndex()
wheel(tabs.tabBar())
ok(tabs.currentIndex() == start_tab,
   "wheel: a QTabBar does not switch tab (Bone Jiggle's Tip/Root tabs sit "
   "mid-panel)")
ok(host.verticalScrollBar().value() != before,
   "wheel: the panel SCROLLED instead (%d -> %d) — forwarded, not swallowed"
   % (before, host.verticalScrollBar().value()))

# ⚠⚠ **THIS IS THE CHECK THAT REPLACES APP-WIDE COVERAGE.** The old filter sat
# on the QApplication and therefore covered every widget ever created,
# including ones nobody had written yet; the new one covers what it is pointed
# at. So instead of trusting the pointing, walk the REAL window and put a real
# wheel event over every single widget a wheel could edit. If a tool is ever
# built without being guarded, this fails with its class name.
_unguarded = []
for _kind in (QComboBox, QAbstractSpinBox, QSlider):
    for _w in win.findChildren(_kind):
        if isinstance(_w, QAbstractSpinBox):
            _before = _w.text()
        elif isinstance(_w, QComboBox):
            _before = _w.currentIndex()
        else:
            _before = _w.value()
        wheel(_w)
        if isinstance(_w, QAbstractSpinBox):
            _after = _w.text()
        elif isinstance(_w, QComboBox):
            _after = _w.currentIndex()
        else:
            _after = _w.value()
        if _after != _before:
            _unguarded.append("%s in %s" % (
                type(_w).__name__,
                type(_w.parentWidget()).__name__ if _w.parentWidget() else "?"))
ok(not _unguarded,
   "wheel: EVERY editable widget in the whole window ignores the wheel — "
   "%d checked, unguarded: %s"
   % (len(win.findChildren(QComboBox)) + len(win.findChildren(QAbstractSpinBox))
      + len(win.findChildren(QSlider)), _unguarded[:6] or "none"))

# ⚠ A widget built AFTER startup is the hole the app-wide filter used to cover
# for free. Switching to a section re-walks it, so a tool that rebuilds a page
# (a point page, a table made when data arrives) cannot quietly lose the guard.
_late_page = win.main_tabs.widget(3)
_late = QComboBox(_late_page)
_late.addItems(["a", "b", "c"])
ok(not _late.property("_madi_wheel_guarded"),
   "wheel: a widget built after startup starts out unguarded (that is the "
   "cost of not filtering the whole application)")
win.main_tabs.setCurrentIndex(3)
ok(_late.property("_madi_wheel_guarded"),
   "wheel: …and opening its section guards it — the re-walk is idempotent and "
   "measured at ~1.8 ms per tab switch")
_late.setParent(None)

# ⚠ The scroller has the same guarantee problem and needs the same answer: it
# is attached per scroll area now, so anything built without the walk scrolls
# in ScrollPerItem mode and jumps a whole tile row per notch — the exact
# complaint the smooth scrolling was written for.
_untuned = [type(_a).__name__ for _a in win.findChildren(QAbstractScrollArea)
            if not getattr(_a, "_madi_scroll_tuned", False)]
ok(not _untuned,
   "scroll: every scroll area in the window is tuned to per-pixel (%d "
   "checked, untuned: %s)"
   % (len(win.findChildren(QAbstractScrollArea)), _untuned[:6] or "none"))

# ⚠ And a dialog cannot be forgotten either: `widgets.GuardedDialog` guards its
# own tree on first show, so the rule is simply that no app dialog subclasses
# QDialog directly. Checked STATICALLY — instantiating sixteen dialogs would
# need sixteen sets of constructor arguments, and the check would rot.
import importlib  # noqa: E402
import inspect  # noqa: E402
from PySide6.QtWidgets import QDialog  # noqa: E402

_bare = []
for _name in ("main", "markers", "render_presets", "dev_console", "devedit",
              "render_deck.settings_dialog"):
    _mod = importlib.import_module(_name)
    for _attr, _obj in vars(_mod).items():
        if (inspect.isclass(_obj) and issubclass(_obj, QDialog)
                and _obj.__module__ == _mod.__name__
                and not issubclass(_obj, widgets.GuardedDialog)):
            _bare.append("%s.%s" % (_name, _attr))
ok(not _bare,
   "wheel: every app dialog is a widgets.GuardedDialog, so it guards itself "
   "when shown (bare QDialog subclasses: %s)" % (_bare or "none"))

lst = QListWidget()
lst.addItems(["r%d" % i for i in range(200)])
lst.resize(120, 80)
lst.show()
app.processEvents()
lst_before = lst.verticalScrollBar().value()
wheel(lst.viewport())
ok(lst.verticalScrollBar().value() != lst_before,
   "wheel: lists/tables still scroll — item views are deliberately unfiltered")

ok(widgets.install_no_wheel(app) is widgets.install_no_wheel(app),
   "wheel: installing the guard twice reuses one filter")

# ---------------------------------------------------------------- 4. the pin
ok(hasattr(win, "pin_button") and win.pin_button.isCheckable(),
   "pin: a checkable button lives in the status bar")
ok(not (win.windowFlags() & Qt.WindowStaysOnTopHint), "pin: off by default")
win.pin_button.setChecked(True)
ok(bool(win.windowFlags() & Qt.WindowStaysOnTopHint),
   "pin: checking it sets WindowStaysOnTopHint")
ok(win.cfg.get("always_on_top") is True, "pin: the choice is persisted")
ok("Pinned" in win.pin_button.text(), "pin: the button reports the state")
win.pin_button.setChecked(False)
ok(not (win.windowFlags() & Qt.WindowStaysOnTopHint), "pin: unpins again")
ok(win.cfg.get("always_on_top") is False, "pin: persists the off state too")
ok("always_on_top" in config.DEFAULTS,
   "pin: it has a default, so a config.json written before it still loads")
ok(config.load().get("always_on_top") is False,
   "pin: load() fills the default in for an old config")

# --------------------------------------------- Marty's baked-in look (08-04)
# These came out of Developer mode: edit and were made permanent, so they are
# defaults now and a regression in one is a real regression.
#
# ⚠ The tab colours are checked by SAMPLING A PAINTED PIXEL, not by reading the
# stylesheet. theme.QSS addresses the outer tabs with an ID selector, so a rule
# written with a weaker selector produces a perfectly valid stylesheet that
# changes nothing - a string assertion cannot tell the two apart.
ok(theme.TAB_BG == "#161414" and theme.TAB_BG_SELECTED == "#2b2b2b"
   and theme.TAB_LAST == "#9c4071",
   "look: the outer tab-bar colours are the ones Marty picked")

# ⚠ THE STRIP IS GONE (2026-08-14) — `widgets.SectionRail` navigates now and
# the QTabBar is HIDDEN, so these checks moved onto the rail. They had to move
# rather than be deleted: the RULE they pin is the one that has already been
# got wrong once, and it is now enforced by different code on a different
# widget, which is exactly when a test earns its keep. A hidden bar also grabs
# to a ZERO-WIDTH image, so the old version failed by sampling nothing —
# "the colour is wrong" when the truth was "there is no picture".
win.resize(1400, 700)
win.show()
app.processEvents()
rail = win.section_rail
# The bar is hidden, not gone: it still carries the tooltips and the per-tab
# text colours, and the checks further down still read them off it.
bar = win.main_tabs.tabBar()
ok(not win.main_tabs.tabBar().isVisible(),
   "shell: the section tab strip is hidden - the rail is the navigation")
shot = rail.grab().toImage()
nsfw_item = rail.entry_for("NSFW Tools")
last_item = rail.entry_for(win.main_tabs.tabText(win.main_tabs.count() - 1))
nsfw_rect = rail.visualItemRect(nsfw_item) if nsfw_item else None
last_rect = rail.visualItemRect(last_item) if last_item else None
ok(nsfw_rect is not None and not nsfw_rect.isEmpty()
   and nsfw_rect.bottom() < shot.height(),
   "look: the NSFW Tools entry is really inside the grab (%s of %d tall) - "
   "otherwise the colour checks below are sampling nothing"
   % (nsfw_rect, shot.height()))

# ⚠ THE TINT FOLLOWS THE NAME, NOT THE POSITION. It used to be
# `QTabBar::tab:last` in the QSS, which did not mean "NSFW Tools" — it meant
# "whichever tab is last" — so Marty's 2026-08-04 reorder would have handed his
# pink to Rendering without anything failing. The rail paints `theme.TAB_TINTS`
# by the entry's CANONICAL title, and these two checks are the pair that pins
# it: the named entry HAS the colour, and the last entry does NOT.
nsfw = next((i for i in range(win.main_tabs.count())
             if win.main_tabs.tabText(i) == "NSFW Tools"), -1)
ok(nsfw >= 0 and nsfw != win.main_tabs.count() - 1,
   "look: NSFW Tools is not the last section (index %d of %d) - which is "
   "exactly the case the old :last rule got wrong"
   % (nsfw, win.main_tabs.count()))
ok(nsfw_rect is not None
   and shot.pixelColor(nsfw_rect.center()).name() == theme.TAB_LAST,
   "look: NSFW Tools really paints %s wherever it sits (got %s)"
   % (theme.TAB_LAST,
      shot.pixelColor(nsfw_rect.center()).name() if nsfw_rect else "no rect"))
ok(last_rect is not None
   and shot.pixelColor(last_rect.center()).name() != theme.TAB_LAST,
   "look: and the LAST entry does NOT get it - the colour belongs to a "
   "section, not to a position")
ok(theme.TAB_TINTS.get("NSFW Tools") == theme.TAB_LAST,
   "look: the tint is declared by name in theme.TAB_TINTS")

# ⚠ AND IT SURVIVES A RENAME. Marty renames sections through Developer mode:
# edit, and the tint is looked up by the entry's stored canonical title rather
# than by the text on screen — so this is the check that the pink cannot be
# lost by typing. The old strip had the mirror-image bug (position, not name).
_renamed = rail.entry_for("NSFW Tools")
_was = _renamed.text(0)
_renamed.setText(0, "Toys")
app.processEvents()
_after = rail.grab().toImage()
ok(_after.pixelColor(rail.visualItemRect(_renamed).center()).name()
   == theme.TAB_LAST,
   "look: renaming the entry to %r keeps the tint - it follows the stored "
   "title, not the label" % "Toys")
_renamed.setText(0, _was)
app.processEvents()

# ⚠ THE WINDOW MUST STILL NARROW (Marty, 2026-08-08: "i can't scale the app
# window horizontally that much"). A single-line QLabel's minimumSizeHint is
# its FULL text width, and a QStackedWidget's minimum is its WIDEST PAGE — so
# one long hint label in one tab held the whole window open at **2194 px**,
# 1944 of it the Node Editor's toolbar hint. Nothing failed, because no test
# had ever asked how narrow the window could go. This is that test: it is a
# CEILING on the minimum, so any new long label is caught where it is added
# rather than by eye months later. Fix = widgets.ElidedLabel.
floor = win.minimumSizeHint().width()
# ⚠ The cap is a CEILING, not a target. It exists so the next long label is
# caught where it is added; it is not an assertion that 1200 is good. If it
# ever fails, the message names who is holding the window open — do not just
# raise the number.
holders = []
for _w in win.findChildren(QWidget):
    try:
        _m = max(_w.minimumSizeHint().width(), _w.minimumWidth())
    except Exception:
        continue
    if _m >= floor - 40:
        holders.append((_m, type(_w).__name__,
                        (_w.text()[:34] if hasattr(_w, "text") else "")))
holders.sort(reverse=True)
# ⚠ THE CEILING DROPPED FROM 1200 TO 700 ON 2026-08-15 (Marty: "we need to be
# able to scale the window a lot" — 900 px of minimum HEIGHT did not fit on a
# 1080p screen once the taskbar and title bar were counted). Getting there took
# five separate floors: the MadiRef side panel and the library sidebar into
# scroll areas, an EXPLICIT minimum on ToolPage's scroll area (a QScrollArea
# folds its child's minimum into its own, so scrolling never got a chance), the
# rail made squeezable instead of fixed, and the status bar's longest button
# label shortened. Measured 549 x 586 after.
ok(floor <= 700,
   "layout: the window can still be narrowed — minimum width %d px (was "
   "2194 before ElidedLabel, 980 before the 2026-08-15 pass). Widest "
   "holders: %r" % (floor, holders[:4]))
_tall = win.minimumSizeHint().height()
# The one that actually hurt on 1080p: usable height is ~1010 px there.
ok(_tall <= 700,
   "layout: and it can be made SHORT - minimum height %d px (was 900, which "
   "nearly filled a 1080p screen on its own)" % _tall)
wide = [w for w in win.findChildren(QLabel)
        if w.minimumSizeHint().width() > 1000]
ok(not wide,
   "layout: no single label forces a >1000 px minimum — a label is never "
   "where a window's width should come from (offenders: %r)"
   % [w.text()[:40] for w in wide])

# ⚠ THE PREMIUM STAR IS PAINTED, NOT APPENDED TO THE TAB TEXT (Marty, 2026-08-04
# - "put a small premium sign so people know it's premium"). The tab's text is a
# lookup key in four places - theme.TAB_TINTS, TAB_TEXT_COLORS, devedit's saved
# renames and these suites' exact title lists - so "Physics *" would quietly
# stop matching "Physics" in all of them. These checks pin both halves: the
# titles stay clean, and the star really lands on the gated tabs only.
gated_titles = set()   # 1.19.0: GATED is gone, nothing is gated
free_titles = {t for _k, t in mainmod.MainWindow.FREE_TOOLS}
ok(len(gated_titles) == 0 and len(free_titles) == 9,
   "premium: NO tab is members-only since 2026-08-14 - every tool is free "
   "and premium PACKS are the paid thing, gated server-side (%d gated)"
   % len(gated_titles))
# ⚠ Marty, 2026-08-05: "Rendering tab should be after Studio library in order
# and it should be not premium (free)". Position and gate arrived as one ask, so
# both are pinned - and the star check below is what proves the FREE half
# visually rather than just structurally.
ok(win.main_tabs.tabText(1) == "Rendering",
   "premium: Rendering sits right after Studio Library (got %r)"
   % win.main_tabs.tabText(1))
ok(all("*" not in t and "★" not in t for t in
       (win.main_tabs.tabText(i) for i in range(win.main_tabs.count()))),
   "premium: and NO tab title was decorated - the text is a lookup key")


def _star_pixels(item):
    """Count gold pixels in a rail entry, where the badge would be."""
    rect = rail.visualItemRect(item)
    mark = QColor(theme.PREMIUM_MARK)
    hits = 0
    for x in range(max(0, rect.left()), min(shot.width(), rect.right())):
        for y in range(max(0, rect.top()), min(shot.height(), rect.bottom())):
            pixel = shot.pixelColor(x, y)
            if (abs(pixel.red() - mark.red()) < 40
                    and abs(pixel.green() - mark.green()) < 40
                    and abs(pixel.blue() - mark.blue()) < 40):
                hits += 1
    return hits


# ⚠ Flipped 2026-08-14: with GATED empty, NO section may wear the star. The
# badge painter is dormant machinery (SectionRail still takes a premium set,
# exactly as the tab bar did), so the visual check stays - it is what catches
# a tab quietly re-entering GATED, or the badge growing a second trigger.
_entries = [rail.entry_for(win.main_tabs.tabText(i))
            for i in range(win.main_tabs.count())]
ok(all(e is not None for e in _entries),
   "shell: every section has a rail entry (%d of %d)"
   % (sum(e is not None for e in _entries), win.main_tabs.count()))
ok(all(_star_pixels(e) == 0 for e in _entries if e is not None),
   "premium: no section paints the gold star - nothing is members-only")
ok(all("Premium" not in bar.tabToolTip(i) for i in range(bar.count())),
   "premium: and no tooltip claims otherwise in words")

# ⚠ THE INFO PANEL MUST FIT IN A NORMAL WINDOW (Marty's screenshot, 2026-08-04:
# "when scaling this right side of the menu gets split in half"). It needed
# 420 px, so in a 1160-wide window the right-hand side was half off the edge.
# Three unwrappable things were setting that floor on their own: a row of three
# save buttons, and two labels a QCheckBox/QLabel will not wrap.
import panels as panelsmod  # noqa: E402

_info = panelsmod.InfoPanel()
app.processEvents()
_need = _info.minimumSizeHint().width()
ok(_need <= 350,
   "panel: the Studio Library info panel fits in %d px (was 420 - bump this "
   "only if the panel really has to grow)" % _need)
_wanted = {"Save Pose", "Save Set", "Save Mirror", "Save Anim"}
_saves = [b for b in _info.findChildren(QPushButton) if b.text() in _wanted]
ok({b.text() for b in _saves} == _wanted,
   "panel: all four Save buttons survived the relayout (%s)"
   % sorted(b.text() for b in _saves))
# ⚠ Checked STRUCTURALLY, off the layout, not from widget positions: nothing
# here is ever shown, so every pos() is (0, 0) and a geometry test passes or
# fails for reasons that have nothing to do with the layout.
_grids = [g for g in _info.findChildren(QGridLayout)
          if any(g.itemAt(i).widget() in _saves
                 for i in range(g.count()))]
ok(_grids and _grids[0].rowCount() >= 2 and _grids[0].columnCount() >= 2,
   "panel: on a 2x2 grid rather than one row - a single row of them cost "
   "390 px on its own")
ok(_info.anim_start.buttonSymbols() == QAbstractSpinBox.NoButtons
   and _info.anim_end.buttonSymbols() == QAbstractSpinBox.NoButtons,
   "panel: the frame fields have no spinner arrows")
_info.anim_start.setValue(_info.anim_start.minimum())
ok(_info.anim_start.text() == "start",
   "panel: and show their placeholder when left alone (%r)"
   % _info.anim_start.text())

# ---------------------------------------------------------------------------
# The author filter fills itself when OPENED (2026-08-15). It used to be built
# on every library scan, which meant reading every item's full data file —
# 3.47 s of a 4.54 s LibraryView build over 800 poses (PERF_PLAN.md). These
# checks exist because the filter had NO test at all before it was changed.
_lib_root = os.path.join(tempfile.mkdtemp(prefix="madi_authors_"), "lib")
os.makedirs(_lib_root)
for _i, _who in enumerate(("ana", "bo", "ana", "cy")):
    _item = os.path.join(_lib_root, "p%d.pose" % _i)
    os.makedirs(_item)
    open(os.path.join(_item, "pose.json"), "w", encoding="utf-8").write(
        '{"name": "p%d", "type": "pose", "bones": {}, '
        '"metadata": {"author": "%s"}}' % (_i, _who))

_view = mainmod.LibraryView({"name": "Authors", "path": _lib_root},
                            win.bridge, win)
_combo = _view.sidebar.author_combo
ok(_combo.count() == 1 and _combo.itemText(0) == "Any author",
   "authors: the dropdown is EMPTY until opened — %d entries after a scan, "
   "which is what stops the scan reading every item's json" % _combo.count())
ok(_view.grid.count() == 4,
   "authors: all four items are shown while no author is chosen (%d)"
   % _view.grid.count())

_combo.showPopup()
_combo.hidePopup()
_listed = [_combo.itemText(i) for i in range(_combo.count())]
ok(_listed == ["Any author", "ana", "bo", "cy"],
   "authors: opening it lists every author, sorted and de-duplicated (%r)"
   % _listed)

_combo.setCurrentText("ana")
_view.refilter()


def _shown(view):
    """Visible rows. refilter() HIDES rows now instead of rebuilding them
    (391 ms per search keystroke at 800 items before — PERF_PLAN.md F1), so
    grid.count() stays the library size and visibility is the filter."""
    return sum(not view.grid.item(i).isHidden()
               for i in range(view.grid.count()))


ok(_shown(_view) == 2 and _view.grid.count() == 4,
   "authors: choosing one really filters the grid (%d of 4 visible) — and "
   "the rows are HIDDEN, not rebuilt (%d still exist)"
   % (_shown(_view), _view.grid.count()))

# ⚠ A rescan must NOT drop the choice. The library rescans on import, delete
# and the folder watcher, and clearing the combo there would silently reset a
# filter the user is in the middle of using.
_view.rescan()
ok(_view.sidebar.filters()["author"] == "ana",
   "authors: a rescan keeps the chosen author (%r)"
   % _view.sidebar.filters()["author"])
ok(_shown(_view) == 2,
   "authors: and the grid is still filtered after it (%d)" % _shown(_view))
_view.deleteLater()

# An ordinary entry — not selected, not tinted — takes the plain rail colour.
middle = next((i for i in range(win.main_tabs.count())
               if i != win.main_tabs.currentIndex() and i != 0
               and win.main_tabs.tabText(i) not in theme.TAB_TINTS), None)
_middle_item = (rail.entry_for(win.main_tabs.tabText(middle))
                if middle is not None else None)
ok(_middle_item is not None
   and shot.pixelColor(rail.visualItemRect(_middle_item).center()).name()
   == theme.TAB_BG,
   "look: an ordinary entry paints the rail colour")

ok(mainmod.TAB_TEXT_COLORS.get("Physics") == "#ffffff",
   "look: Physics keeps its own text colour")
phys = next((i for i in range(win.main_tabs.count())
             if win.main_tabs.tabText(i) == "Physics"), -1)
ok(phys >= 0 and bar.tabTextColor(phys).name() == "#ffffff",
   "look: and it is applied in code, because QSS cannot address a middle tab")
# ⚠ And it reaches the RAIL, which is the strip people can actually see now.
# Marty picked this white himself; a colour that only lands on a hidden widget
# has been silently lost.
_phys_item = rail.entry_for("Physics")
ok(_phys_item is not None
   and _phys_item.foreground(0).color().name() == "#ffffff",
   "look: and the rail entry wears it too - the visible one")

# The rail label and the page heading are now different strings, which is the
# whole reason add_tool grew a `heading` argument.
from PySide6.QtWidgets import QLabel  # noqa: E402

page = renderingmod.RenderingPage(None, None)
tool_page = page.add_tool(QWidget(), "Short", group="G",
                          heading="A much longer heading")
ok(page.rail.topLevelItem(0).child(0).text(0) == "Short",
   "look: add_tool's rail entry uses the short label")
heads = [w.text() for w in tool_page.findChildren(QLabel)
         if w.objectName() == "h1"]
ok(heads == ["A much longer heading"],
   "look: while the page header uses the heading (got %s)" % heads)

plain = page.add_tool(QWidget(), "Both", group="G")
ok([w.text() for w in plain.findChildren(QLabel) if w.objectName() == "h1"]
   == ["Both"],
   "look: and with no heading given, the label does both jobs as before")

import nsfw as nsfwmod  # noqa: E402

ok(nsfwmod.ACCENT_BUTTON == "#ff2962", "look: the torus button keeps its colour")
tool = win.affector_torus_tool
if tool is not None:                       # None while the tab is locked
    ok(tool.btn_add.text() == "Add Stretching torus", "look: and its name")
    ok(nsfwmod.ACCENT_BUTTON in tool.btn_add.styleSheet(),
       "look: the colour is actually on the button")
    ok("<b>" in tool.blurb.text() and "<br><br>" in tool.blurb.text(),
       "look: the blurb keeps its bold runs and its paragraph break")
    ok("\n" not in tool.blurb.text(),
       "look: as <br>, not newlines - a rich-text label collapses those")

import bridge as bridgemod  # noqa: E402

# ------------------------- WHICH Blender is the bridge? (Marty, 2026-08-05)
# With two instances open the port is held by exactly one of them, and the app
# used to say only "Blender". Every button then acts on a file you may not be
# looking at.
CONNECTED = {"version": bridgemod.EXPECTED_ADDON_VERSION,
             "capabilities": [], "licensed": True,
             "file": r"T:\Blender Work 2026\Project\sq02_sc01.031_.blend",
             "active_object": "Lily", "is_armature": True, "mode": "OBJECT",
             "selected_bones": 0, "frame": 12, "frame_start": 1,
             "frame_end": 250}
win._connected_file = None
win._on_status_ok(dict(CONNECTED))
# ⚠ `full_text()`, NEVER `.text()`. bridge_label is an ElidedLabel, so `.text()`
# is the DISPLAY string — truncated to whatever width the layout happened to
# give it, which in an offscreen run nothing controls. This assertion read
# `.text()` and failed on a 436 px label that elided four characters into
# "frame 12", reporting a status line that was in fact perfectly correct. The
# filename check above survived only because the filename sits at the FRONT of
# the string, i.e. it was one rename away from the same false alarm.
# `app_nodeeditor_test.py` already learned this for the toolbar hints.
ok("sq02_sc01.031_.blend" in win.bridge_label.full_text(),
   "bridge status names the .blend it is connected to (%r)"
   % win.bridge_label.full_text())
ok("Lily" in win.bridge_label.full_text()
   and "frame 12" in win.bridge_label.full_text(),
   "...without losing the object and the frame")
ok(CONNECTED["file"] in win.bridge_label.toolTip(),
   "the full path is in the tooltip")
ok(win._connected_file == CONNECTED["file"], "and it is remembered")

# ⚠ The move. Stopping the bridge in one Blender lets the other take the port
# within 5 s, and every button then acts on a DIFFERENT file with no warning.
win.statusBar().clearMessage()
other = dict(CONNECTED, file=r"T:\Blender Work 2026\test\picker_01_.blend")
win._on_status_ok(other)
ok("picker_01_.blend" in win.statusBar().currentMessage(),
   "⚠ a bridge that moved to another Blender SAYS SO (%r)"
   % win.statusBar().currentMessage())

win.statusBar().clearMessage()
win._on_status_ok(dict(other))
ok(not win.statusBar().currentMessage(),
   "...once, not on every poll of the same file")

win._connected_file = None
win.statusBar().clearMessage()
win._on_status_ok(dict(CONNECTED))
ok(not win.statusBar().currentMessage(),
   "the FIRST connect is not a move - there was nothing to move from")

unsaved = dict(CONNECTED, file="")
win._connected_file = None
win._on_status_ok(unsaved)
ok("unsaved" in win.bridge_label.full_text(),
   "an unsaved .blend still reads as something (%r)"
   % win.bridge_label.full_text())

win._on_status_failed("gone")
ok(win._connected_file is None,
   "a dropped bridge forgets the file, so reconnecting to a DIFFERENT Blender "
   "is not announced as a move before it has been seen once")
ok("two Blenders" in win.bridge_label.toolTip(),
   "and the offline tooltip points at the two-instance case")

# ⚠ THE LICENCE-PUSH CHECKS WERE DELETED IN 1.19.0. The add-on's entitlement
# gate and the app's `_push_license` are both gone, so there is nothing to push
# and nothing a version note could swallow. The bug they guarded (a version
# difference silently switching off every paid Blender feature) cannot recur
# because there are no paid features left to switch off.
# Discord to report bugs and Patreon for support". The two links do two jobs and
# the wording has to keep them apart.

import version as versionmod  # noqa: E402
from main import AboutDialog  # noqa: E402


def _about_text(dlg):
    from PySide6.QtWidgets import QLabel as _QLabel
    return "\n".join(w.text() for w in dlg.findChildren(_QLabel))


def _about_buttons(dlg):
    return [b.text() for b in dlg.findChildren(QPushButton)]


dlg = AboutDialog(win, None, "0.20.0")
text = _about_text(dlg)
buttons = _about_buttons(dlg)

ok(versionmod.APP_VERSION in text, "about: shows the app version")
ok("0.20.0" in text, "about: and the add-on version that is actually connected")
ok(versionmod.AUTHOR in text, "about: credits MadihsonNSFW")
ok(any("Discord" in b for b in buttons), "about: has a Discord button (%r)" % buttons)
ok(any("Patreon" in b for b in buttons), "about: has a Patreon button")
ok(any("Discord" in b and "bug" in b.lower() for b in buttons),
   "about: the Discord button says it is for BUGS, which is the job Marty gave "
   "it - a bare link makes the user guess which of the two to use")
ok(versionmod.DISCORD_URL.startswith("https://discord.gg/"),
   "about: the Discord invite is https (%s)" % versionmod.DISCORD_URL)
ok("patreon.com" in versionmod.PATREON_URL, "about: the Patreon link points at Patreon")

# ⚠ THE ATTRIBUTION RULE, PINNED. An About box is literally the "who made this"
# screen, so it is the single most likely place for an attribution we do not
# ship to slip in. The word list is local-only (see `_branding`), which means
# this check guards a real build and passes trivially in a clone rather than
# failing one that could never satisfy it.
blob = (text + " " + " ".join(buttons) + " " + dlg.windowTitle()).lower()
ok(not any(w in blob for w in _FORBIDDEN),
   "about: carries none of the withheld attributions (branding rule)")
ok(" ai " not in blob and "ai-written" not in blob,
   "about: and nothing about how it was written")
dlg.deleteLater()

# ⚠ THE LICENCE LINE IS GONE (1.19.0). The About box no longer discusses
# entitlement because there is none — every tool is free and the app contacts
# nothing. What it still has to get right is checked above: the versions, the
# author, the two links, and the attribution rule.
# ===================================== 5. one instance of the app, not two ==
# Two copies would fight over the same config.json, the same render queue and
# the same bridge — and only one Blender can hold the bridge, so the second
# window would look connected and act on nothing.
import os as _os  # noqa: E402

key = "madi-ui-test-%d" % _os.getpid()          # never the real one: Marty's
first = mainmod.claim_single_instance(key)      # own copy may be open
ok(first is not None, "single: the first caller owns the app")
second = mainmod.claim_single_instance(key)
ok(second is None,
   "single: a second caller is refused — that is the whole feature")
first.close()
third = mainmod.claim_single_instance(key)
ok(third is not None,
   "single: and the name frees up when the owner goes, so a crash cannot "
   "leave the app permanently unlaunchable")
third.close()

# ⚠ THE ONE THAT MATTERS MOST, and it is not about windows at all.
# `updater\swap.smoke()` runs `main.py --smoke` to decide whether to KEEP an
# update, and it can run while the app that started the update is still open.
# A smoke run refused for "another copy is running" would exit non-zero and
# make EVERY update roll itself back. So the claim must sit inside the
# not-smoke branch, and this reads the source to prove it does.
src = open(_os.path.join(os.path.join(_ROOT, "app"),
                         "main.py"), encoding="utf-8").read()
guard = src[src.index("def main("):]
claim_at = guard.index("claim_single_instance()")
smoke_at = guard.rindex("if not smoke:", 0, claim_at)
between = guard[smoke_at:claim_at]
ok(between.count("\n") < 12 and "single = " in between,
   "single: the claim is inside `if not smoke:` — a smoke run must never be "
   "refused, or every update would roll itself back")

# ============================ 6. drag across the type filters to set many ===
from PySide6.QtCore import QEvent  # noqa: E402
from PySide6.QtGui import QMouseEvent  # noqa: E402

import panels as panelsmod  # noqa: E402

side = panelsmod.Sidebar()
side.resize(220, 700)
side.show()
app.processEvents()
boxes = list(side.type_checks.values())
ok(len(boxes) >= 3 and all(isinstance(b, widgets.DragCheckBox) for b in boxes),
   "drag: every type filter is a DragCheckBox (%d)" % len(boxes))


def _press(w):
    gp = QPointF(w.mapToGlobal(QPoint(6, 6)))
    app.sendEvent(w, QMouseEvent(QEvent.MouseButtonPress, QPointF(6, 6), gp,
                                 Qt.LeftButton, Qt.LeftButton, Qt.NoModifier))


def _drag_over(pressed, target):
    gp = QPointF(target.mapToGlobal(QPoint(6, 6)))
    local = QPointF(pressed.mapFromGlobal(gp.toPoint()))
    app.sendEvent(pressed, QMouseEvent(QEvent.MouseMove, local, gp,
                                       Qt.NoButton, Qt.LeftButton,
                                       Qt.NoModifier))


def _release(w):
    gp = QPointF(w.mapToGlobal(QPoint(6, 6)))
    app.sendEvent(w, QMouseEvent(QEvent.MouseButtonRelease, QPointF(6, 6), gp,
                                 Qt.LeftButton, Qt.NoButton, Qt.NoModifier))


a, b, c = boxes[0], boxes[1], boxes[2]
for cb in boxes:
    cb.setChecked(True)
_press(a)
_drag_over(a, b)
_drag_over(a, c)
_release(a)
ok(not a.isChecked() and not b.isChecked() and not c.isChecked(),
   "drag: press one and drag over its neighbours and all three UNTICK "
   "(%r)" % [x.isChecked() for x in (a, b, c)])
ok(boxes[3].isChecked(),
   "drag: ...and one the gesture never reached is untouched")

# ⚠ the whole gesture is delivered to the PRESSED widget — Qt grabs the mouse
# on press, so the neighbours never see an event of their own. A per-box
# implementation would simply never fire; this proves the parent walk works.
_press(a)               # a is unticked -> this paints TICKED
_drag_over(a, b)
_release(a)
ok(a.isChecked() and b.isChecked(),
   "drag: the direction comes from the FIRST box, so the same gesture ticks "
   "as well as unticks")

# a plain click still has to behave like a checkbox
before = c.isChecked()
_press(c)
_release(c)
ok(c.isChecked() != before, "drag: a single click still just toggles")

# ================= 7. the item panel collapses when nothing is selected =====
# Marty, 2026-08-08, with a screenshot: a full-height column of "—" was taking
# the right-hand third of the window to say nothing at all.
info = panelsmod.InfoPanel()
ok(info.detail.isHidden(),
   "panel: the detail block starts hidden — nothing is selected yet")
ok(info.save_name is not None and not info.save_name.isHidden(),
   "panel: the New item box is NOT hidden with it — you save with nothing "
   "selected, which is the whole point of it being outside the block")
info.show_item(None)
ok(info.detail.isHidden(), "panel: and stays hidden when the selection clears")

# ============== 8. the version sits bottom-left AT ALL TIMES (2026-08-08) ====
# Marty: "make sure we see the version of the app in bottom left part at all
# times (small writing)". The interesting half is "at all times" — a stock
# QStatusBar HIDES non-permanent widgets while a temporary message shows, and
# this app shows one from 74 places, so the obvious implementation would be
# invisible exactly when somebody is looking at the bar.
import version as versionmod  # noqa: E402

bar = win.statusBar()
ok(isinstance(bar, widgets.StatusBar),
   "version: the window's status bar is our StatusBar, set before anything "
   "called statusBar() and made a plain one")
ok(bar.version_label.text() == "v" + versionmod.APP_VERSION,
   "version: it reads the running version, not a hand-typed copy (%r)"
   % bar.version_label.text())
ok(versionmod.APP_VERSION in bar.version_label.toolTip(),
   "version: and the tooltip spells it out for a bug report")

# LEFTMOST: ahead of the message, and ahead of every permanent widget.
others = [w for w in (bar.message_label, win.bridge_label, win.about_button)
          if w is not None]
ok(all(bar.version_label.x() < w.x() for w in others),
   "version: it is the leftmost thing in the bar (%d px, next is %d)"
   % (bar.version_label.x(), min(w.x() for w in others)))

# ⚠ THE ONE THAT WOULD HAVE BEEN WRONG. A temporary message must not take it
# away — which is exactly what QStatusBar's own mechanism does.
bar.showMessage("something long and important is happening")
ok(not bar.version_label.isHidden(),
   "⚠ version: a status message does NOT hide it — the whole reason the "
   "message is our own label instead of Qt's temporary one")
ok(bar.currentMessage() == "something long and important is happening",
   "version: and the message still round-trips through currentMessage()")
bar.clearMessage()
ok(not bar.currentMessage() and not bar.version_label.isHidden(),
   "version: clearing the message leaves the version where it was")

# The message must never be able to widen the window — a render summary or a
# traceback's first line is long, and this bar carries ten permanent widgets.
before = win.minimumSizeHint().width()
bar.showMessage("x" * 4000)
ok(win.minimumSizeHint().width() <= before,
   "version: a 4000-character message does not grow the window's minimum "
   "width (%d -> %d)" % (before, win.minimumSizeHint().width()))
bar.clearMessage()

# ================ 9. Developer mode: edit is not in a shipped build =========
# Marty, 2026-08-08: "whenever we push updates for this app or build .exe we
# should disable the developer mode: edit option, this should only be a thing
# when we are working on the app or locally testing it."
import devedit  # noqa: E402

ok(devedit.available(),
   "devedit: offered when running from source — that IS working on the app")

frozen_before = getattr(sys, "frozen", None)
sys.frozen = True                      # pretend to be the built exe
os.environ.pop(devedit.DEV_ENV, None)
try:
    ok(not devedit.available(),
       "devedit: NOT offered in a frozen build")
    devedit.set_enabled(True)
    ok(not devedit.enabled(),
       "⚠ devedit: and set_enabled REFUSES there — a stray `dev_edit: true` in "
       "a config.json must not put right-click editing in front of a tester")

    dlg = mainmod.LibrarySettingsDialog(win, dict(win.cfg))
    ok(dlg.chk_devedit is None,
       "devedit: the Settings checkbox is not built at all (absent, not greyed "
       "out — a disabled 'Developer mode' is still an invitation to ask)")
    boxes = [b.text() for b in dlg.findChildren(QCheckBox)]
    ok(not any("Developer mode" in t for t in boxes),
       "devedit: and nothing else in the dialog says it either (%d boxes)"
       % len(boxes))
    ok(all("Clear edits" not in (b.text() or "")
           for b in dlg.findChildren(QPushButton)),
       "devedit: Clear edits goes with it")
    # A value that IS in the config must survive a trip through Settings.
    dlg2 = mainmod.LibrarySettingsDialog(win, dict(win.cfg, dev_edit=True))
    ok(dlg2.values()["dev_edit"] is True,
       "devedit: a stored value passes straight through rather than being "
       "silently rewritten because its widget is missing")

    os.environ[devedit.DEV_ENV] = "1"
    ok(devedit.available(),
       "devedit: MADI_DEV_EDIT=1 is the escape hatch, so Marty keeps it in his "
       "own exe without it existing for anyone else")
finally:
    os.environ.pop(devedit.DEV_ENV, None)
    if frozen_before is None:
        del sys.frozen
    else:
        sys.frozen = frozen_before
    devedit.set_enabled(False)

# ⚠ SECTION 10 (the status-bar update flow) WAS DELETED IN 1.19.0 with the
# updater itself. The add-on push that survived it has its own suite,
# `app_addon_push_test.py`.

# ========== 10. THE APP MAKES NO NETWORK CALLS. AT ALL. ====================
# ⚠⚠ THE HEADLINE PROMISE OF 1.19.0, AND THE EASIEST ONE TO LOSE BY ACCIDENT.
# Marty: "FULLY remove the ... mention of the server". A single `urllib` import
# added later — a version check, a crash report, a "what's new" fetch — would
# quietly undo it, and nothing else in this fleet would notice, because a
# network call that works looks exactly like no network call at all.
#
# Scanned as SOURCE rather than by importing: an import-time check would only
# see modules that happen to be loaded, and the whole point is to catch a
# module nobody thought about.
import glob as _glob  # noqa: E402
import re as _re  # noqa: E402

_NET = _re.compile(r"^\s*(?:import|from)\s+"
                   r"(urllib|http|requests|ftplib|smtplib|telnetlib|"
                   r"xmlrpc|aiohttp|httpx|websocket)\b", _re.M)
_offenders = []
for _py in sorted(_glob.glob(os.path.join(_ROOT, "app", "**", "*.py"),
                             recursive=True)):
    if ".venv" in _py or "__pycache__" in _py or os.sep + "dist" + os.sep in _py:
        continue
    with open(_py, encoding="utf-8") as _fh:
        _hit = _NET.search(_fh.read())
    if _hit:
        _offenders.append("%s -> %s" % (os.path.basename(_py),
                                        _hit.group(1)))
ok(not _offenders,
   "⚠⚠ network: NO module under app\\ imports a network library (%r)"
   % _offenders)

# The one socket that is allowed, and the reason it is allowed: it is the
# Blender bridge, on loopback, to a port on this machine.
with open(os.path.join(_ROOT, "app", "bridge.py"), encoding="utf-8") as _fh:
    _bridge_src = _fh.read()
ok("socket.create_connection" in _bridge_src,
   "network: the ONE socket in the app is the Blender bridge's, in bridge.py")
_socket_users = []
for _py in sorted(_glob.glob(os.path.join(_ROOT, "app", "**", "*.py"),
                             recursive=True)):
    if ".venv" in _py or "__pycache__" in _py or os.sep + "dist" + os.sep in _py:
        continue
    if os.path.basename(_py) in ("bridge.py", "main.py"):
        continue          # main.py's single-instance lock is a local mutex
    with open(_py, encoding="utf-8") as _fh:
        if _re.search(r"^\s*import socket\b", _fh.read(), _re.M):
            _socket_users.append(os.path.basename(_py))
ok(not _socket_users,
   "network: and nothing else opens one (%r)" % _socket_users)

# ========== 11. EVERY TAB BUILT, AND THE TEARDOWN ==========================
# ⚠⚠ RESCUED FROM `lic_client_test.py`, WHICH WAS DELETED WITH LICENSING ON
# 2026-08-15. That suite's NAME said licences; roughly eighty of its lines were
# a whole-shell integration check that had nothing to do with them, and three
# separate docs credited it with catching bugs no other suite could see. Losing
# it silently is exactly the kind of hole a rename or a delete leaves behind —
# **check what a suite actually asserts before deleting it, not what it is
# called.**
#
# ⚠ `_pages()` IS THE ONE THAT MATTERS. A tool page missed from that list was
# shipped TWICE (the Optimization tab, then MadiRef) and this count is the only
# thing that has ever caught it.
# ⚠ A FRESH WINDOW. The `win` above has had tabs opened by ten earlier
# sections, so its lazy-tab state is no longer pristine — asserting "not
# built at startup" on it failed for a reason that had nothing to do with
# the code. The suite this came from built its own window for that reason.
#
# ⚠⚠ AND IT HAS TO START ON TAB 0. An earlier section toggles the always-on-top
# pin, which PERSISTS the config — including `main_tab`, the tab `win` happened
# to be on. A fresh window then restores that tab and builds it, which is
# CORRECT behaviour (a lazy tab you land on must exist) and would make this
# check fail for a reason that is not a bug. Pin the starting tab so the
# assertion is about laziness and nothing else.
_cfg_now = _json.loads(_io.open(config.CONFIG_PATH, encoding="utf-8").read())
_cfg_now["main_tab"] = 0
_io.open(config.CONFIG_PATH, "w", encoding="utf-8").write(_json.dumps(_cfg_now))
_w = mainmod.MainWindow()
ok(_w.main_tabs.currentIndex() == 0,
   "tabs: the fresh window starts on Studio Library, so the lazy check below "
   "is about laziness and not about a restored tab")
_tab_titles = [_w.main_tabs.tabText(i) for i in range(_w.main_tabs.count())]
ok(_tab_titles == ["Studio Library", "Rendering", "Bone picker", "Anim Layers",
                   "Node Setup", "Node Editor", "MadiRef", "Optimization",
                   "NSFW Tools", "Physics", "What's New"],
   "tabs: the strip is in Marty's order (%r)" % (_tab_titles,))
ok(_w.rendering is not None and _w.render_queue is not None
   and _w.picker is not None
   and _w.node_setup is not None and _w.nodeeditor is not None,
   "tabs: the eagerly-built tools are constructed at startup")
ok(_w.madiref is not None and _w.optimizer is not None
   and _w.nsfw is not None and _w.physics is not None,
   "tabs: so are the four that used to be paid")
ok(_w.bone_jiggle_tool is not None
   and _w.affector_torus_tool is not None
   and _w.optimizer_adaptive_tool is not None,
   "tabs: and their sub-tools with them")

# ⚠ Anim Layers is LAZY (PERF_PLAN option C): built on FIRST OPEN, and proved
# through the real path — a tab switch — not by calling the builder.
ok(_w.anim_layers is None,
   "tabs: the lazy Anim Layers tab is NOT built at startup")
_al_index = _tab_titles.index("Anim Layers")
_w.main_tabs.setCurrentIndex(_al_index)
ok(_w.anim_layers is not None and _w.layers_page is not None
   and _w.markers_tool is not None,
   "tabs: opening Anim Layers builds it on demand")

# ⚠⚠ THIS MUST STAY BELOW THE LAZY-TAB OPEN ABOVE. `_pages()` counts what has
# been BUILT, so on a window where Anim Layers has never been opened the answer
# is legitimately one short — measured: 9 vs 10. Moved above the open, this
# check goes red on perfectly good code. Injection-proved 2026-08-15: with
# every lazy tab built it is exactly equal, and dropping a single page from
# `_pages()` takes it to 9 and fails.
ok(len(_w._pages()) == _w.tabs.count() + len(_w.FREE_TOOLS),
   "⚠ tabs: _pages() carries every tool tab, with every lazy tab built "
   "(%d library + %d tools, got %d)"
   % (_w.tabs.count(), len(_w.FREE_TOOLS), len(_w._pages())))
ok(_w.physics.rail.topLevelItem(0).text(0) == "BONES",
   "tabs: the Physics rail lists BONES — a live page, not a stub")
_w.save_settings()
ok(True, "tabs: save_settings() survives with every tab built")

# The dead bridge the app hands a tool when Blender is absent. ⚠ `capabilities`
# must be a LIST, not a failure dict — returning a dict crashed Physics once.
_dead = mainmod._DeadBridge()
ok(_dead.feature_reason("anything") is None,
   "dead bridge: reports features as available, so tools look normal")
ok(_dead.capabilities == [] and _dead.addon_version is None,
   "dead bridge: capabilities is a LIST, not a failure dict")
ok(_dead.request("quad_status")["ok"] is False,
   "dead bridge: every command fails instantly")
ok(_dead.anything_written_later()["ok"] is False,
   "dead bridge: including helpers that do not exist yet")

# ⚠ THE TEARDOWN, WITH EVERY TAB LIVE. The render queue owns a worker; closing
# with all tabs built is the path that has to shut it down cleanly.
from PySide6.QtGui import QCloseEvent  # noqa: E402

_w.closeEvent(QCloseEvent())
ok(True, "tabs: closeEvent shuts the render queue down cleanly with every tab live")
print("")
print("%d passed, %d failed" % (len(PASS), len(FAIL)))
for f in FAIL:
    print("FAIL " + f)
