# Developer mode: edit - renaming UI text and shipping it as the default.
#
#   app\.venv\Scripts\python.exe tests\app_devedit_test.py
#
# Offscreen Qt, and config.APP_DIR is redirected to a temp dir so the real
# dev_edits.json next to config.json is never touched (same rule as the settings
# and Render Queue suites).
#
# This suite DRIVES THE FILTER, rather than calling the rename helpers directly:
# the thing that breaks in practice is an event never reaching the filter, and a
# test that calls rename() by hand cannot see that.
import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))

from PySide6.QtCore import QEvent, QPoint, Qt              # noqa: E402
from PySide6.QtGui import QContextMenuEvent, QTextCursor   # noqa: E402
from PySide6.QtWidgets import (QApplication, QCheckBox, QGroupBox,  # noqa: E402
                               QLabel, QPushButton, QTabWidget,
                               QTreeWidget, QTreeWidgetItem, QVBoxLayout,
                               QWidget)

import config                                              # noqa: E402

TMP = tempfile.mkdtemp(prefix="madi_devedit_")
config.APP_DIR = TMP
# DATA_DIR is the WRITABLE root (macOS splits it off APP_DIR); the
# caches, queues and presets read it, so redirecting only APP_DIR
# would build them in the real dist folder.
config.DATA_DIR = TMP
config.CONFIG_PATH = os.path.join(TMP, "config.json")

import devedit                                             # noqa: E402

devedit.STORE = devedit.EditStore()

PASS = []
FAIL = []


def ok(cond, label):
    (PASS if cond else FAIL).append(label)
    print(("ok   " if cond else "FAIL ") + label, flush=True)


app = QApplication.instance() or QApplication([])
KEEP = []


def make_window():
    """A miniature of the real shell: outer tabs, a rail, buttons, labels."""
    win = QWidget()
    win.setObjectName("TestWindow")
    lay = QVBoxLayout(win)

    tabs = QTabWidget()
    tabs.setObjectName("maintabs")
    for name in ("Studio Library", "Rendering", "NSFW Tools"):
        page = QWidget()
        pl = QVBoxLayout(page)
        pl.addWidget(QLabel("page " + name))
        tabs.addTab(page, name)
    lay.addWidget(tabs)

    rail = QTreeWidget()
    rail.setObjectName("toolrail")
    group = QTreeWidgetItem(["RIGS"])
    rail.addTopLevelItem(group)
    group.addChild(QTreeWidgetItem(["Affector Torus"]))
    group.addChild(QTreeWidgetItem(["Bone Jiggle"]))
    # Expanded, or the children have no visual rect and itemAt() finds nothing -
    # the real rail expands its groups on creation for the same reason.
    group.setExpanded(True)
    lay.addWidget(rail)

    btn = QPushButton("Add Affector Torus")
    btn.setObjectName("accent")
    lay.addWidget(btn)
    chk = QCheckBox("Only selected bones")
    lay.addWidget(chk)
    box = QGroupBox("Dynamics")
    lay.addWidget(box)
    plain = QLabel("Thickness")
    lay.addWidget(plain)
    icon_only = QPushButton("")
    lay.addWidget(icon_only)

    win.tabs, win.rail, win.btn = tabs, rail, btn
    win.chk, win.box, win.plain, win.icon_only = chk, box, plain, icon_only
    KEEP.append(win)
    return win


win = make_window()
win.show()

# ------------------------------------------------------------------- keys

btn_target = devedit.target_at(win.btn, QPoint(2, 2))
ok(btn_target is not None and btn_target.original == "Add Affector Torus",
   "target: a button reports its own text as the original")
ok(btn_target.key.endswith("|Add Affector Torus"),
   "key: ends with the ORIGINAL text, which is what makes it searchable in source")
ok("TestWindow" in btn_target.key and "accent" in btn_target.key,
   "key: carries the path from the window down (got %s)" % btn_target.key)

again = make_window()
ok(devedit.target_at(again.btn, QPoint(2, 2)).key == btn_target.key,
   "key: the same widget in a rebuilt window gets the SAME key")

icon_target = devedit.target_at(win.icon_only, QPoint(2, 2))
ok(icon_target is not None and icon_target.renameable is False,
   "target: a button with no text has nothing to RENAME, but can still be recoloured")
ok(devedit.target_at(win.plain, QPoint(1, 1)).renameable is True,
   "target: a label with text is renameable")

bar = win.tabs.tabBar()
tab_target = devedit.target_at(bar, bar.tabRect(2).center())
ok(tab_target is not None and tab_target.original == "NSFW Tools",
   "target: a tab is found by the point clicked, not by index alone")
ok(tab_target.kind == "tab" and "|tab|" in tab_target.key,
   "key: a tab is namespaced so it cannot collide with a button of the same text")

item_target = devedit.target_at(win.rail, win.rail.visualItemRect(
    win.rail.topLevelItem(0).child(0)).center())
ok(item_target is not None and item_target.original == "Affector Torus",
   "target: a rail entry is renameable too")
ok(item_target.kind == "item", "target: and is recorded as a rail entry")

# Two identical labels in different places must not share a key.
a, b = QLabel("Amount"), QLabel("Amount")
holder1, holder2 = QWidget(), QWidget()
holder1.setObjectName("boxA")
holder2.setObjectName("boxB")
QVBoxLayout(holder1).addWidget(a)
QVBoxLayout(holder2).addWidget(b)
KEEP += [holder1, holder2]
ok(devedit.target_at(a, QPoint(1, 1)).key != devedit.target_at(b, QPoint(1, 1)).key,
   "key: same text in two places = two different keys")

# ------------------------------------------------------------- rename/reset

devedit.rename.__doc__  # (kept explicit: the menu path is exercised below)
t = devedit.target_at(win.btn, QPoint(2, 2))
devedit.STORE.put(t.key, t.original, t.kind, t.where, text="Add Torus")
t.set_text("Add Torus")
ok(win.btn.text() == "Add Torus", "rename: the widget shows the new text")
ok(devedit.STORE.count() == 1, "rename: one edit stored")
rec = devedit.STORE.get(t.key)
ok(rec["original"] == "Add Affector Torus" and rec["text"] == "Add Torus",
   "rename: the record keeps BOTH texts, so the source edit is findable")
ok(os.path.isfile(devedit.STORE.path()), "rename: written to dev_edits.json")

fresh = devedit.EditStore()
fresh._path = devedit.STORE.path()
ok(fresh.get(t.key) is not None, "store: survives a reload from disk")

# ---------------------------------------------------------------- applying

win2 = make_window()
ok(win2.btn.text() == "Add Affector Torus", "apply: a new window starts unrenamed")
devedit.apply_all(win2)
ok(win2.btn.text() == "Add Torus", "apply: the stored rename lands on a new window")

# Idempotent: a second pass must not look up "Add Torus" and lose the link.
devedit.apply_all(win2)
ok(win2.btn.text() == "Add Torus", "apply: running twice is a no-op, not a break")
t2 = devedit.target_at(win2.btn, QPoint(2, 2))
ok(t2.original == "Add Affector Torus",
   "apply: the widget still remembers its ORIGINAL after being renamed")
ok(t2.key == t.key, "apply: so it still computes the same key")

# Overrides must apply with the mode OFF - the checkbox gates editing only.
devedit.set_enabled(False)
win3 = make_window()
devedit.apply_all(win3)
ok(win3.btn.text() == "Add Torus",
   "apply: renames stay applied while Developer edit is OFF")

# Tabs and rail entries.
tb = win3.tabs.tabBar()
tt = devedit.target_at(tb, tb.tabRect(2).center())
devedit.STORE.put(tt.key, tt.original, tt.kind, tt.where, text="Toys")
win4 = make_window()
devedit.apply_all(win4)
ok(win4.tabs.tabText(2) == "Toys", "apply: a tab renames")
ok(win4.tabs.tabText(0) == "Studio Library", "apply: and its neighbours do not")

it = devedit.target_at(win3.rail, win3.rail.visualItemRect(
    win3.rail.topLevelItem(0).child(1)).center())
devedit.STORE.put(it.key, it.original, it.kind, it.where, text="Jiggle")
win5 = make_window()
devedit.apply_all(win5)
ok(win5.rail.topLevelItem(0).child(1).text(0) == "Jiggle",
   "apply: a rail entry renames")
ok(win5.rail.topLevelItem(0).child(0).text(0) == "Affector Torus",
   "apply: its sibling does not")

# A tab that moved index must still rename - the key is its text, not its slot.
win6 = make_window()
page = win6.tabs.widget(2)
win6.tabs.removeTab(2)
win6.tabs.insertTab(0, page, "NSFW Tools")
devedit.apply_all(win6)
ok(win6.tabs.tabText(0) == "Toys",
   "apply: a tab renames after being moved to another index")

# ------------------------------------------------------------ the filter

filt = devedit.DevEditFilter()
menus = []
devedit.show_menu = lambda target, widget, pos: menus.append(target)

devedit.set_enabled(False)
ev = QContextMenuEvent(QContextMenuEvent.Mouse, QPoint(2, 2),
                       win.btn.mapToGlobal(QPoint(2, 2)))
ok(filt.eventFilter(win.btn, ev) is False and not menus,
   "filter: with the mode OFF a right-click is left entirely alone")

devedit.set_enabled(True)
ok(filt.eventFilter(win.btn, ev) is True and len(menus) == 1,
   "filter: with the mode ON a right-click on a button is consumed and offers a menu")
ok(menus[0].original == "Add Affector Torus",
   "filter: and the menu targets the right control")

menus.clear()
plain_widget = QWidget()
plain_widget.show()
KEEP.append(plain_widget)
ok(filt.eventFilter(plain_widget, ev) is True and len(menus) == 1,
   "filter: a plain panel is editable too - a background needs to be clickable")
ok(menus[0].renameable is False,
   "filter: and its menu offers colours only, with no rename")

menus.clear()
ok(filt.eventFilter(win.icon_only, ev) is True and menus
   and menus[0].renameable is False,
   "filter: same for a button with no text")

# The Show hook is what keeps a lazily-built page renamed.
late = make_window()
ok(late.btn.text() == "Add Affector Torus", "filter: a fresh page starts unrenamed")
filt.eventFilter(late, QEvent(QEvent.Show))
ok(late.btn.text() == "Add Torus",
   "filter: showing a page built later applies the renames to it")

# ------------------------------------------------------------- reset/clear

devedit.set_enabled(True)
back = devedit.target_at(win2.btn, QPoint(2, 2))
devedit.reset(back)
ok(win2.btn.text() == "Add Affector Torus", "reset: puts the shipped text back")
ok(devedit.STORE.get(back.key) is None, "reset: and drops the record")

before = devedit.STORE.count()
ok(before >= 2, "clear: there are edits to clear (%d)" % before)
n = devedit.clear_all(win4)
ok(n == before and devedit.STORE.count() == 0, "clear: every record is dropped")
ok(win4.tabs.tabText(2) == "NSFW Tools",
   "clear: and the UI goes back to what the source says")

# The rename() path itself, driven through a stubbed prompt - this is what the
# menu calls, so the branches in it are worth exercising for real.
devedit.STORE.clear()
answers = []
devedit.QInputDialog.getText = staticmethod(
    lambda *a, **k: (answers.pop(0), True) if answers else ("", False))

t3 = devedit.target_at(win.btn, QPoint(2, 2))
first_key = t3.key
answers.append("Whatever")
devedit.rename(t3)
ok(win.btn.text() == "Whatever" and devedit.STORE.count() == 1,
   "rename: the prompt's answer is applied and stored")

# Renaming a SECOND time must update the same record, not create a new one.
t4 = devedit.target_at(win.btn, QPoint(2, 2))
ok(t4.key == first_key,
   "rename: a re-rename resolves back to the ORIGINAL, so the key is unchanged")
answers.append("Second")
devedit.rename(t4)
ok(devedit.STORE.count() == 1, "rename: twice leaves ONE record, not two orphans")
ok(devedit.STORE.get(first_key)["text"] == "Second"
   and devedit.STORE.get(first_key)["original"] == "Add Affector Torus",
   "rename: that record still names the original source string")

# Typing the shipped text back is not an override at all.
t5 = devedit.target_at(win.btn, QPoint(2, 2))
answers.append("Add Affector Torus")
devedit.rename(t5)
ok(devedit.STORE.count() == 0,
   "rename: typing the original back removes the record instead of storing a no-op")

# Cancelling changes nothing.
t6 = devedit.target_at(win.btn, QPoint(2, 2))
before_text = win.btn.text()
ok(devedit.rename(t6) is None and win.btn.text() == before_text
   and devedit.STORE.count() == 0, "rename: cancelling the prompt changes nothing")

# ------------------------------------------------------------- robustness

bad = os.path.join(TMP, "broken.json")
with open(bad, "w", encoding="utf-8") as fh:
    fh.write("{not json at all")
broken = devedit.EditStore(bad)
ok(broken.count() == 0, "store: a corrupt file reads as empty rather than crashing")
ok(broken.get("anything") is None, "store: and answers lookups safely")

missing = devedit.EditStore(os.path.join(TMP, "nope", "none.json"))
ok(missing.count() == 0, "store: a missing file is not an error")
ok(missing.save() is False, "store: an unwritable path reports failure, not a crash")

ok(devedit.apply_all(None) == 0, "apply: a null root is a no-op")
devedit.STORE.clear()
ok(devedit.apply_all(win) == 0, "apply: with no edits stored it does nothing at all")

ok("dev_edit" in config.DEFAULTS and config.DEFAULTS["dev_edit"] is False,
   "config: the setting exists and ships OFF")

# ------------------------------------------------------------------ colours

devedit.STORE.clear()
cw = make_window()
ct = devedit.target_at(cw.btn, QPoint(2, 2))
ct.set_fg("#ff0000")
devedit.STORE.put(ct.key, ct.original, ct.kind, ct.where, fg="#ff0000")
ok("color: #ff0000" in cw.btn.styleSheet(), "colour: text colour lands on the widget")

ct2 = devedit.target_at(cw.btn, QPoint(2, 2))
ct2.set_bg("#0000ff")
devedit.STORE.put(ct2.key, ct2.original, ct2.kind, ct2.where, bg="#0000ff")
sheet = cw.btn.styleSheet()
ok("color: #ff0000" in sheet and "background-color: #0000ff" in sheet,
   "colour: setting a background KEEPS the text colour (got %r)" % sheet)
ok(sheet.count("color: #ff0000") == 1,
   "colour: repeated edits rebuild from the base sheet instead of stacking rules")

ok(devedit.STORE.count() == 1,
   "colour: name and both colours live in ONE record per control")
crec = devedit.STORE.get(ct.key)
ok(crec["fg"] == "#ff0000" and crec["bg"] == "#0000ff" and "text" not in crec,
   "colour: a colour-only edit stores no text")

cw2 = make_window()
devedit.apply_all(cw2)
ok("color: #ff0000" in cw2.btn.styleSheet()
   and "background-color: #0000ff" in cw2.btn.styleSheet(),
   "colour: both land on a freshly built window")

# A widget that already had a stylesheet must keep it.
styled = make_window()
styled.plain.setStyleSheet("font-weight: bold;")
st = devedit.target_at(styled.plain, QPoint(1, 1))
st.set_fg("#00ff00")
ok("font-weight: bold" in styled.plain.styleSheet()
   and "color: #00ff00" in styled.plain.styleSheet(),
   "colour: the widget's own stylesheet survives (got %r)" % styled.plain.styleSheet())

# Tabs: text colour yes, background no (Qt has no per-tab background).
tabt = devedit.target_at(cw.tabs.tabBar(), cw.tabs.tabBar().tabRect(1).center())
ok(tabt.can_background is False, "colour: a tab offers no background - Qt has none")
tabt.set_fg("#123456")
devedit.STORE.put(tabt.key, tabt.original, tabt.kind, tabt.where, fg="#123456")
cw3 = make_window()
devedit.apply_all(cw3)
ok(cw3.tabs.tabBar().tabTextColor(1).name() == "#123456",
   "colour: a tab's text colour applies to a new window")

# Rail entries take both.
railt = devedit.target_at(cw.rail, cw.rail.visualItemRect(
    cw.rail.topLevelItem(0).child(0)).center())
railt.set_fg("#abcdef")
railt.set_bg("#fedcba")
devedit.STORE.put(railt.key, railt.original, railt.kind, railt.where,
                  fg="#abcdef", bg="#fedcba")
cw4 = make_window()
devedit.apply_all(cw4)
child = cw4.rail.topLevelItem(0).child(0)
ok(child.foreground(0).color().name() == "#abcdef"
   and child.background(0).color().name() == "#fedcba",
   "colour: a rail entry takes both colours")

# Clearing colours must restore the VIEW default, not paint it black - the real
# rail's group headers are deliberately theme.TEXT_HEAD.
group = cw4.rail.topLevelItem(0)
gt = devedit.target_at(cw4.rail, cw4.rail.visualItemRect(group).center())
gt.set_fg("#111111")
devedit.clear_colours(gt)
ok(group.foreground(0).style() == Qt.NoBrush,
   "colour: clearing a rail entry restores the view default rather than black")

# Clearing a widget's colours puts its own stylesheet back untouched.
devedit.clear_colours(devedit.target_at(styled.plain, QPoint(1, 1)))
ok(styled.plain.styleSheet().strip() == "font-weight: bold;",
   "colour: clearing leaves the widget's original stylesheet exactly as it was")

# A colour-only record disappears when its colours go, rather than lingering.
ck = devedit.target_at(cw.btn, QPoint(2, 2)).key
devedit.clear_colours(devedit.target_at(cw.btn, QPoint(2, 2)))
ok(devedit.STORE.get(ck) is None,
   "colour: a record with nothing left in it is dropped, not kept empty")

# But a rename on the same control must survive clearing its colours.
mixed = make_window()
mt = devedit.target_at(mixed.btn, QPoint(2, 2))
devedit.STORE.put(mt.key, mt.original, mt.kind, mt.where,
                  text="Renamed", fg="#ff0000")
devedit.clear_colours(devedit.target_at(mixed.btn, QPoint(2, 2)))
kept = devedit.STORE.get(mt.key)
ok(kept is not None and kept["text"] == "Renamed" and "fg" not in kept,
   "colour: clearing colours leaves the rename alone")
devedit.STORE.clear()

# ------------------------------------------------------- rich text + links

devedit.STORE.clear()
rw = make_window()
rt = devedit.target_at(rw.plain, QPoint(1, 1))
ok(rt.rich is True, "rich: a QLabel is marked as taking markup")
ok(devedit.target_at(rw.btn, QPoint(2, 2)).rich is False,
   "rich: a QPushButton is not - it cannot render markup")

markup = 'Thickness — see <b>the guide</b> at <a href="https://example.com">docs</a>'
rt.set_text(markup)
devedit.STORE.put(rt.key, rt.original, rt.kind, rt.where, text=markup)
ok(rw.plain.text() == markup, "rich: the markup is stored on the label verbatim")
ok(rw.plain.textFormat() == Qt.RichText, "rich: the label is switched to rich text")
ok(rw.plain.openExternalLinks() is True,
   "rich: links are made clickable - markup alone leaves them dead")
ok(bool(rw.plain.textInteractionFlags() & Qt.LinksAccessibleByMouse),
   "rich: and the mouse can reach them")

rw2 = make_window()
devedit.apply_all(rw2)
ok(rw2.plain.text() == markup and rw2.plain.openExternalLinks() is True,
   "rich: a new window gets the markup AND the link handling")

ok(devedit._plain(markup).startswith("Thickness")
   and "<b>" not in devedit._plain(markup)
   and "the guide" in devedit._plain(markup),
   "rich: tags are stripped for menu labels, the words are kept")

# Bold on its own must not switch link handling on.
bw = make_window()
bt = devedit.target_at(bw.plain, QPoint(1, 1))
bt.set_text("Plain <b>bold</b> only")
ok(bw.plain.textFormat() == Qt.RichText and bw.plain.openExternalLinks() is False,
   "rich: bold alone does not turn on external links")

# The editor dialog: markup in, markup out, and the buttons wrap a selection.
dlg = devedit.RichTextDialog(None, "hello world", "hello world")
KEEP.append(dlg)
cur = dlg.editor.textCursor()
cur.setPosition(0)
cur.setPosition(5, QTextCursor.KeepAnchor)
dlg.editor.setTextCursor(cur)
dlg._wrap("<b>", "</b>")
ok(dlg.markup() == "<b>hello</b> world",
   "rich: the Bold button wraps the selection (got %r)" % dlg.markup())
ok(dlg.preview.text() == dlg.markup(), "rich: the preview renders what is typed")

devedit.STORE.clear()

# ------------------------------------------------------- tab backgrounds

tw = make_window()
tw.show()
tbar = tw.tabs.tabBar()

# The theme styles the outer tabs as `QTabWidget#maintabs > QTabBar::tab`, which
# is MORE specific than a plain `QTabBar::tab` - a plain rule would silently
# lose. The selector has to mirror the ID.
ok(devedit._tab_selector(tbar) == "QTabWidget#maintabs > QTabBar",
   "tabs: the selector mirrors the parent's id so it can beat the theme (got %s)"
   % devedit._tab_selector(tbar))

bt = devedit.target_at(tbar, tbar.tabRect(2).center())
ok(bt.kind == "tab", "tabs: clicking a tab targets that tab")
bar_t = devedit._tabbar_target(tbar, 2)
ok(bar_t.kind == "tabbar" and bar_t.key.endswith("|tabbar|"),
   "tabs: the backgrounds are a separate, bar-level record")
ok(bar_t.one_pos == "last",
   "tabs: the last tab can be singled out - Qt offers :first and :last only")
ok(devedit._tabbar_target(tbar, 1).one_pos is None,
   "tabs: a middle tab cannot, and does not pretend to")
ok(devedit._tabbar_target(tbar, 0).one_pos == "first", "tabs: the first tab can")

devedit.STORE.put(bar_t.key, bar_t.original, bar_t.kind, bar_t.where,
                  bg="#202020", bg_sel="#3a3a3a", bg_one="#c000c0",
                  one_pos="last")
bar_t.push()
sheet = tbar.styleSheet()
ok("QTabWidget#maintabs > QTabBar::tab { background-color: #202020; }" in sheet,
   "tabs: all-tab background rule (got %r)" % sheet)
ok("::tab:selected { background-color: #3a3a3a; }" in sheet,
   "tabs: selected-tab rule")
ok("::tab:last { background-color: #c000c0; }" in sheet,
   "tabs: and the single-tab rule uses the recorded position")

tw2 = make_window()
tw2.show()
devedit.apply_all(tw2)
ok("#c000c0" in tw2.tabs.tabBar().styleSheet(),
   "tabs: the backgrounds apply to a freshly built window")

# Does Qt actually PAINT it? A specificity mistake produces a perfectly valid
# stylesheet that changes nothing, so the string check above is not enough.
tw3 = make_window()
tw3.resize(600, 300)
tw3.show()
b3 = tw3.tabs.tabBar()
devedit.apply_all(tw3)
app.processEvents()
shot = b3.grab().toImage()
rect = b3.tabRect(b3.count() - 1)
painted = shot.pixelColor(rect.center())
ok(painted.name() == "#c000c0",
   "tabs: Qt really paints the last tab that colour (got %s) - proves the "
   "selector beats the theme" % painted.name())

devedit.clear_colours(devedit._tabbar_target(b3, 2))
app.processEvents()
after = b3.grab().toImage().pixelColor(rect.center())
ok(after.name() != "#c000c0", "tabs: and clearing really removes it")

devedit.STORE.clear()

# ------------------------------------------------- the Show hook stays cheap
# It runs on EVERY show event in the application, so a widget already checked
# against the current edits must not cause another subtree walk.
devedit.STORE.clear()
perf = make_window()
pt = devedit.target_at(perf.btn, QPoint(2, 2))
devedit.STORE.put(pt.key, pt.original, pt.kind, pt.where, text="Renamed")
devedit.apply_all(perf)
ok(perf.btn.text() == "Renamed", "revision: the edit applied")

walks = []
real_apply = devedit.apply_all
devedit.apply_all = lambda root: (walks.append(root), real_apply(root))[1]
show = QEvent(QEvent.Show)
for _ in range(5):
    filt.eventFilter(perf, show)
ok(not walks, "revision: re-showing a checked widget does NOT re-walk it")

devedit.STORE.put(pt.key, pt.original, pt.kind, pt.where, text="Renamed twice")
filt.eventFilter(perf, show)
ok(len(walks) == 1, "revision: but a NEW edit invalidates the stamp and it re-walks")
ok(perf.btn.text() == "Renamed twice", "revision: and the newer text lands")
devedit.apply_all = real_apply
devedit.STORE.clear()

print("")

# ------------------------------------------------------------- roundness
# Marty, 2026-08-04: "make me ability to edit the roundness of buttons".
from PySide6.QtWidgets import QGroupBox as _QGroupBox  # noqa: E402

_btn = QPushButton("Rounded")
_tgt = devedit._widget_target(_btn)
ok("radius" in _tgt.extras,
   "radius: a button offers corner roundness")
ok("radius" not in devedit._widget_target(QLabel("plain")).extras,
   "radius: a plain QLabel does not - it paints no background, so a radius "
   "would appear to do nothing, and a menu entry that seems broken is worse "
   "than a missing one")
ok("radius" in devedit._widget_target(_QGroupBox("Box")).extras,
   "radius: a group box does, because it paints its own frame")

devedit.STORE.put(_tgt.key, _tgt.original, _tgt.kind, _tgt.where, radius=9)
_tgt.push()
ok("border-radius: 9px" in _btn.styleSheet(),
   "radius: it reaches the widget's stylesheet (%r)" % _btn.styleSheet())

# ⚠ 0 IS A REAL SETTING - square corners - and the store used to test fields
# for truthiness, which threw the whole record away the moment it was saved.
_btn0 = QPushButton("Square")
_t0 = devedit._widget_target(_btn0)
devedit.STORE.put(_t0.key, _t0.original, _t0.kind, _t0.where, radius=0)
ok(devedit.STORE.get(_t0.key) is not None
   and devedit.STORE.get(_t0.key).get("radius") == 0,
   "radius: a roundness of ZERO survives being saved - it means square, not "
   "'nothing set'")
_t0.push()
ok("border-radius: 0px" in _btn0.styleSheet(),
   "radius: and it is applied (%r)" % _btn0.styleSheet())

# Roundness and colour share one stylesheet, so setting one must not drop the
# other.
devedit.STORE.put(_tgt.key, _tgt.original, _tgt.kind, _tgt.where, fg="#ff0000")
_tgt.push()
sheet = _btn.styleSheet()
ok("border-radius: 9px" in sheet and "color: #ff0000" in sheet,
   "radius: a colour edit keeps the roundness and vice versa - they live in "
   "the same stylesheet (%r)" % sheet)

devedit.STORE.drop(_tgt.key)
devedit.STORE.drop(_t0.key)

print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
for label in FAIL:
    print("  FAILED: " + label)
sys.exit(1 if FAIL else 0)
