"""Developer mode: edit — restyle the UI in place, then ship it as the default.

Marty asked for this (2026-08-04): switch it on in ⚙ Library Settings, right-click
anything, and change its **text** (with bold and clickable links), its **colours**
or, on a tab bar, the **tab backgrounds**. Everything is written to
`dev_edits.json` next to config.json, where it can be read afterwards and baked
into the source as the new defaults.

THREE THINGS THAT LOOK LIKE BUGS AND ARE NOT:

1. **Changes apply whether or not the checkbox is on.** The checkbox gates
   *editing*, not the result. A rename you made yesterday has to still be there
   today, or you cannot live with it long enough to decide whether it was right.
   Switching the mode off just puts the right-click menus away.

2. **While the mode is ON, right-click is the edit menu everywhere.** Not just on
   buttons - on panels, fields, rails and empty space, because "change the
   background of that box" needs the box to be clickable. That means the
   library's own item menu is unavailable while editing. It is off by default,
   nothing else turns it on, and the status bar says when it is on.

3. **A colour set on a container reaches its children.** Qt merges a widget's
   stylesheet with its ancestors', so `color:` on a panel colours the text inside
   it. That is usually what "recolour this box" means; when it is not, click the
   child directly - the menu names whatever it is about to change.

THE KEY IS PATH + ORIGINAL TEXT, and the original half is load-bearing. A path
alone ("the 3rd QPushButton in this box") silently points somewhere else the
moment a button is inserted above it, and the edit would then restyle the WRONG
control. Pinning the original text too means a shifted path simply fails to
match: the edit quietly does not apply, which is the safe direction to fail. It
also makes the file useful on its own - `original` is the string to search for in
the source when making the edit permanent. Widgets with no text (a panel, a
frame) have only the path to go on, so those keys are the fragile ones.

Applying is IDEMPOTENT: the first apply remembers the original on the widget, so
a second pass computes the same key from the original rather than from the new
text. Without that, re-applying would look up "the new name" and find nothing.
"""

import datetime
import json
import os
import sys

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (QCheckBox, QColorDialog, QDialog,
                               QDialogButtonBox, QGroupBox, QHBoxLayout,
                               QInputDialog, QLabel, QMenu, QPushButton,
                               QRadioButton, QTabBar, QTabWidget, QTextEdit,
                               QTreeWidget, QVBoxLayout, QWidget)

import config
import widgets

FILE_NAME = "dev_edits.json"
FORMAT = 3

# Widgets whose visible text can be renamed. QGroupBox is handled beside them
# rather than in this tuple, because its label is title(), not text().
TEXT_WIDGETS = (QPushButton, QCheckBox, QRadioButton, QLabel)

# What "Corner roundness…" is offered on (Marty, 2026-08-04). Buttons and group
# boxes paint their own frame, so a radius is visible on them straight away. A
# plain QLabel does not paint a background unless one has been set, so rounding
# its corners would appear to do nothing — and a menu entry that seems broken is
# worse than a missing one.
ROUNDABLE = (QPushButton, QGroupBox)
# Wider than any sane corner on a control this size; the dialog caps here so a
# typo cannot turn a button into a circle nobody can find the edges of.
MAX_RADIUS = 32

# The fields an edit record can carry. A record with none of them left is
# dropped rather than kept as an empty shell.
#   text                the label / name (may be rich text on a QLabel)
#   fg, bg              this control's own text and background colour
#   bg_sel, bg_one      tab-bar only: the selected tab, and this tab alone
FIELDS = ("text", "fg", "bg", "bg_sel", "bg_one", "radius", "curving")
# ⚠ "radius" and "curving" are NUMBERS, and every other field here is a
# string. `put()` drops a field whose value is None, so a value of 0 has to
# reach it as the int 0 and not as "" - `0` is real and meaningful for both
# (square corners; dead-straight wires) and must not be mistaken for
# "unset". The `is None` test in put() is what keeps them apart; do not turn
# it into a falsiness test.
#
# "curving" is offered on exactly one thing: the Node Editor canvas (the
# view carries a `_madi_wire_canvas` property; nodecanvas.py sets it). It is
# Marty's "edge smoothness" - how much the noodles bow, 0..10 on the same
# scale as Blender's own Noodle Curving preference.

# ⚠ THIS IS A DEVELOPMENT TOOL AND IT DOES NOT SHIP (Marty, 2026-08-08):
# *"whenever we push updates for this app or build .exe we should disable the
# developer mode: edit option, this should only be a thing when we are working
# on the app or locally testing it"*. So the boundary is the same one the
# updater draws — `sys.frozen` — and it is drawn HERE rather than in the
# settings dialog, so that hiding the checkbox and refusing to enable the mode
# cannot drift apart.
#
# The escape hatch exists because Marty runs the built exe day to day and has
# used the mode there. An environment variable keeps it his: nothing a tester
# can find in the UI, and nothing a config.json can turn back on.
DEV_ENV = "MADI_DEV_EDIT"


def available():
    """Is Developer mode: edit offered in this run at all?

    True from source (that IS working on the app). In a frozen build only when
    MADI_DEV_EDIT is set to something meaning yes.
    """
    if not getattr(sys, "frozen", False):
        return True
    return os.environ.get(DEV_ENV, "").strip().lower() not in ("", "0", "false", "no")


_enabled = False


def enabled():
    return _enabled


def set_enabled(on):
    """Turn the right-click editing menus on or off. Does NOT touch edits that
    are already applied - see the module docstring.

    ⚠ Refuses in a build that does not offer the mode, whatever it is asked.
    A `dev_edit: true` left in a config.json - Marty's own, copied by hand, or
    carried across an update - must not put right-click editing in front of
    somebody who has no idea what it is.
    """
    global _enabled
    _enabled = bool(on) and available()
    return _enabled


# --------------------------------------------------------------------- store


class EditStore:
    """The saved edits, keyed by path|original.

    Kept as a flat dict so a hand-edit of the file is possible, and written with
    indent so a diff of it is readable - this file exists to be read by a person.
    """

    def __init__(self, path=None):
        self._path = path
        self._edits = {}
        self._loaded = False
        # Bumped on every change. The Show hook uses it to skip widgets it has
        # already walked, which is what keeps an application-wide filter from
        # re-walking a subtree on every single show event.
        self._rev = 0

    def revision(self):
        self.load()
        return self._rev

    def path(self):
        # Resolved late, and never cached, so a test that redirects
        # config.APP_DIR gets the redirected path (the same trick the Render
        # Queue and settings suites use).
        return self._path or os.path.join(config.APP_DIR, FILE_NAME)

    def load(self, force=False):
        if self._loaded and not force:
            return self._edits
        self._edits = {}
        try:
            with open(self.path(), "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            # No file yet, or one somebody broke by hand. Neither is worth
            # refusing to start over - an empty store just means no edits.
            data = {}
        edits = data.get("edits") if isinstance(data, dict) else None
        if isinstance(edits, dict):
            # Records from any earlier FORMAT load unchanged: every version so
            # far only ADDED optional fields, so an old record is a new record
            # with the new ones missing.
            for key, rec in edits.items():
                if isinstance(rec, dict) and any(rec.get(f) for f in FIELDS):
                    self._edits[key] = rec
        self._loaded = True
        self._rev += 1
        return self._edits

    def save(self):
        data = {"format": FORMAT,
                "note": "Edits made in Developer mode: edit. 'original' is the "
                        "string to search for in the source when making one of "
                        "these permanent. Colours are #rrggbb; 'text' may be "
                        "rich text (<b>, <a href=...>).",
                "edits": self._edits}
        try:
            with open(self.path(), "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=1, ensure_ascii=False)
            return True
        except OSError:
            return False

    def get(self, key):
        return self.load().get(key)

    def put(self, key, original, kind, where="", **fields):
        """Merge fields into one record. A field passed as None is REMOVED, and
        a record left with nothing is dropped - so "put it back the way it was"
        never leaves a no-op entry behind for me to puzzle over later."""
        self.load()
        rec = dict(self._edits.get(key) or {})
        for name, value in fields.items():
            if name not in FIELDS:
                rec[name] = value        # metadata, e.g. which tab bg_one means
            elif value is None:
                rec.pop(name, None)
            else:
                rec[name] = value
        # ⚠ `is None`, NOT falsiness. `radius: 0` means SQUARE CORNERS, which is
        # a deliberate choice and a perfectly good edit — under a truthiness
        # test it read as "nothing set here" and the whole record was deleted
        # the moment it was saved.
        if all(rec.get(f) is None for f in FIELDS):
            return self.drop(key)
        rec.update(original=original, kind=kind, where=where,
                   when=datetime.datetime.now().replace(microsecond=0).isoformat())
        self._edits[key] = rec
        self._rev += 1
        self.save()
        return True

    def drop(self, key):
        self.load()
        gone = self._edits.pop(key, None) is not None
        if gone:
            self._rev += 1
            self.save()
        return gone

    def clear(self):
        self.load()
        n = len(self._edits)
        self._edits = {}
        self._rev += 1
        self.save()
        return n

    def count(self):
        return len(self.load())

    def all(self):
        return dict(self.load())


STORE = EditStore()


# ---------------------------------------------------------------- identity


def _node(widget):
    """One step of a widget path: its object name, or its Python class name."""
    return widget.objectName() or type(widget).__name__


def widget_path(widget):
    """A stable-ish path from the top-level window down to *widget*.

    Deliberately uses the PYTHON class name rather than the Qt one: "ValueSlider"
    says more than "QWidget" when reading the file later. Siblings that would
    collide get an index, so two unnamed buttons in the same box stay distinct.
    """
    steps = []
    node = widget
    while node is not None:
        name = _node(node)
        parent = node.parentWidget()
        if parent is not None:
            same = [c for c in parent.children()
                    if isinstance(c, QWidget) and _node(c) == name]
            if len(same) > 1:
                try:
                    name = "%s[%d]" % (name, same.index(node))
                except ValueError:
                    pass
        steps.append(name)
        node = node.parentWidget()
    return "/".join(reversed(steps))


def _key(widget, kind, original):
    if kind == "widget":
        return "%s|%s" % (widget_path(widget), original)
    return "%s|%s|%s" % (widget_path(widget), kind, original)


def _hex(colour):
    return colour.name() if isinstance(colour, QColor) and colour.isValid() else None


# --------------------------------------------------------------- restyling


def _restyle(widget, fg=None, bg=None, extra=""):
    """Rebuild a widget's stylesheet: whatever it shipped with, plus our bits.

    The widget's own sheet is captured ONCE into `_madi_base_qss`, so repeated
    edits keep rebuilding from the original rather than stacking `color:` rules
    forever, and clearing puts back exactly what was there.
    """
    base = widget.property("_madi_base_qss")
    if base is None:
        base = widget.styleSheet() or ""
        widget.setProperty("_madi_base_qss", base)
    bits = []
    if fg:
        bits.append("color: %s;" % fg)
    if bg:
        bits.append("background-color: %s;" % bg)
    if extra:
        bits.append(extra)
    widget.setStyleSheet((base + " " + " ".join(bits)).strip() if bits else base)


def _tab_selector(bar):
    """A selector for this bar's tabs that can BEAT the app-wide theme.

    theme.QSS styles the outer tabs as `QTabWidget#maintabs > QTabBar::tab`.
    A plain `QTabBar::tab` rule is LESS specific, so it loses however late it is
    set - the tabs would simply not change colour. Mirroring the ID puts the two
    on equal specificity, and ours is set on the widget, so ours wins.
    """
    parent = bar.parentWidget()
    if isinstance(parent, QTabWidget) and parent.objectName():
        return "QTabWidget#%s > QTabBar" % parent.objectName()
    return "QTabBar"


def _tabbar_qss(bar, rec):
    """The tab-background rules for one bar, from its record."""
    sel = _tab_selector(bar)
    bits = []
    if rec.get("bg"):
        bits.append("%s::tab { background-color: %s; }" % (sel, rec["bg"]))
    if rec.get("bg_sel"):
        bits.append("%s::tab:selected { background-color: %s; }"
                    % (sel, rec["bg_sel"]))
    if rec.get("bg_one"):
        # Qt has no per-index tab selector; :first and :last are the only two
        # positional ones it offers, so a single tab can only be picked out when
        # it sits at one end. `one_pos` records which end was meant.
        bits.append("%s::tab:%s { background-color: %s; }"
                    % (sel, rec.get("one_pos", "last"), rec["bg_one"]))
    return " ".join(bits)


# ------------------------------------------------------------------ targets


class Target:
    """One editable thing: a widget, a tab, a tab bar, or a rail entry.

    `set_text` is None when there is nothing to rename (a panel, an icon-only
    button); the menu leaves the rename entries out. `rich` marks a QLabel, the
    only thing here that renders bold and links.
    """

    def __init__(self, widget, kind, original, current, where="",
                 set_text=None, set_fg=None, set_bg=None, label="",
                 rich=False, extras=None):
        self.widget = widget
        self.kind = kind
        self.original = original
        self.current = current
        self.where = where
        self.label = label or where
        self.rich = rich
        # {field: (menu label, applier)} - the extra colour channels a tab bar
        # has and nothing else does.
        self.extras = extras or {}
        self._set_text = set_text
        self._set_fg = set_fg
        self._set_bg = set_bg
        self.key = _key(widget, kind, original)

    @property
    def renameable(self):
        return self._set_text is not None

    @property
    def can_background(self):
        return self._set_bg is not None

    def set_text(self, text):
        self._set_text(text)

    def set_fg(self, colour):
        if self._set_fg:
            self._set_fg(colour)

    def set_bg(self, colour):
        if self._set_bg:
            self._set_bg(colour)

    def record(self):
        return STORE.get(self.key) or {}

    def push(self, rec=None):
        """Apply a record to this target. A missing field CLEARS its channel, so
        this is the single path for both applying and undoing."""
        rec = self.record() if rec is None else rec
        if self.renameable and rec.get("text") and rec["text"] != self.current:
            self.set_text(rec["text"])
        self.set_fg(rec.get("fg"))
        if self.can_background:
            self.set_bg(rec.get("bg"))
        for field, (_label, apply_fn) in self.extras.items():
            apply_fn(rec)


def _orig_map(obj, attr):
    """Per-object memory of what a label used to say.

    Tabs and tree items have no dynamic properties of their own, so the original
    text is remembered on their OWNER, keyed by what is displayed now. Without
    this, applying an edit twice would look up the new name and find nothing.
    """
    got = getattr(obj, attr, None)
    if got is None:
        got = {}
        setattr(obj, attr, got)
    return got


def _widget_text(widget):
    if isinstance(widget, QGroupBox):
        return widget.title()
    if isinstance(widget, TEXT_WIDGETS):
        return widget.text()
    return ""


def _widget_colours(widget):
    rec = STORE.get(_key(widget, "widget",
                         widget.property("_madi_orig") or _widget_text(widget)))
    return (rec or {}).get("fg"), (rec or {}).get("bg")


def _widget_target(widget):
    """A widget: its text if it has any, and always its colours.

    ⚠ The text applier STAMPS the original on the widget before writing, and
    that is not bookkeeping. Without it, a second rename in the same session
    reads the new text as the original, computes a different key, and leaves the
    first record orphaned - so the file grows an edit nobody can trace back to
    any string in the source. Caught by the suite, not by eye.
    """
    setter = None
    rich = False
    if isinstance(widget, QGroupBox):
        current, setter = widget.title(), widget.setTitle
    elif isinstance(widget, TEXT_WIDGETS):
        current, setter = widget.text(), widget.setText
        rich = isinstance(widget, QLabel)   # the only one that renders markup
        if not current.strip():
            setter = None            # an icon-only button has nothing to rename
    else:
        current = ""

    original = widget.property("_madi_orig") or current
    set_text = None
    if setter is not None:
        def set_text(text, _s=setter):
            widget.setProperty("_madi_orig", original)
            _s(text)
            if rich:
                _enable_rich(widget, text)

    # ⚠ Rebuilt from the WHOLE record, not one channel at a time. Roundness and
    # the two colours all live in the same stylesheet, so writing one of them
    # from only its own value would drop the other two. Colour setters keep
    # their old shape (they read the sibling colour back off the widget) because
    # `push()` calls them individually; the radius applier is an `extra`, which
    # is handed the whole record and therefore runs last and puts everything
    # back together.
    def _style(rec):
        radius = rec.get("radius")
        extra = "" if radius is None else "border-radius: %dpx;" % int(radius)
        _restyle(widget, rec.get("fg"), rec.get("bg"), extra)

    def set_fg(colour):
        _restyle(widget, colour, _widget_colours(widget)[1])

    def set_bg(colour):
        _restyle(widget, _widget_colours(widget)[0], colour)

    where = ("group box" if isinstance(widget, QGroupBox)
             else type(widget).__name__)
    label = "“%s”" % _plain(current) if current.strip() else where
    # Roundness is offered on the things it can be SEEN on. On a plain QLabel a
    # corner radius does nothing visible unless it also has a background, and a
    # menu entry that appears to do nothing is worse than no entry.
    extras = {}
    if isinstance(widget, ROUNDABLE):
        extras["radius"] = ("Corner roundness…", _style)
    # The Node Editor canvas (and nothing else): wire curving. Keyed off a
    # dynamic property rather than an isinstance so this module does not have
    # to import nodecanvas just to recognise its view.
    if widget.property("_madi_wire_canvas"):
        def _curving(rec):
            setter = getattr(widget, "set_wire_curving", None)
            if setter is not None:
                setter(rec.get("curving"))
        extras["curving"] = ("Edge smoothness…", _curving)
    return Target(widget, "widget", original, current, where=where,
                  set_text=set_text, set_fg=set_fg, set_bg=set_bg, label=label,
                  rich=rich, extras=extras)


def _enable_rich(label, markup):
    """Make a QLabel render markup, and make its links clickable.

    Qt's AutoText already spots `<b>` on its own, but a link does nothing until
    the label is told to accept mouse interaction and to open external URLs -
    otherwise it looks like a link and is dead when clicked.
    """
    if "<" not in markup:
        return
    label.setTextFormat(Qt.RichText)
    if "<a " in markup.lower():
        label.setOpenExternalLinks(True)
        label.setTextInteractionFlags(
            label.textInteractionFlags() | Qt.LinksAccessibleByMouse)


def _plain(markup):
    """Markup with the tags taken out, for a menu label."""
    out, depth = [], 0
    for ch in markup:
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth = max(0, depth - 1)
        elif not depth:
            out.append(ch)
    return "".join(out).strip() or markup


def _tabbar_target(bar, index=None):
    """The TAB BACKGROUNDS, which belong to the bar rather than to one tab.

    Qt exposes no per-tab background - `setTabTextColor` has no counterpart, and
    a stylesheet cannot address a tab by index. What it does have is `:selected`,
    `:first` and `:last`, so a single tab can be picked out only when it sits at
    one end. That is why this is a bar-level target with an optional "this tab".
    """
    key = _key(bar, "tabbar", "")

    def restyle(rec):
        _restyle(bar, extra=_tabbar_qss(bar, rec))

    def set_bg(colour):
        rec = dict(STORE.get(key) or {})
        rec["bg"] = colour
        restyle(rec)

    extras = {"bg_sel": ("Selected tab background…",
                         lambda rec: restyle(rec))}
    pos = None
    if index is not None and bar.count() > 1:
        if index == 0:
            pos = "first"
        elif index == bar.count() - 1:
            pos = "last"
    target = Target(bar, "tabbar", "", "", where="tab backgrounds",
                    set_bg=set_bg, label="the tab backgrounds",
                    extras=extras)
    target.one_pos = pos
    if pos:
        target.extras["bg_one"] = (
            "This tab only (%s)…" % pos, lambda rec: restyle(rec))
    return target


def _tab_target(bar, pos):
    index = bar.tabAt(pos)
    if index < 0:
        return None
    shown = bar.tabText(index)
    original = _orig_map(bar, "_madi_tab_orig").get(shown, shown)

    def set_text(text, i=index):
        bar.setTabText(i, text)
        _orig_map(bar, "_madi_tab_orig")[text] = original

    def set_fg(colour, i=index):
        # An invalid QColor is Qt's own "use the default", which is exactly what
        # clearing should do - no need to remember the theme's colour.
        bar.setTabTextColor(i, QColor(colour) if colour else QColor())

    target = Target(bar, "tab", original, shown, where="tab",
                    set_text=set_text, set_fg=set_fg,
                    label="tab “%s”" % shown)
    target.tab_index = index
    return target


def _item_target(tree, pos):
    item = tree.itemAt(pos)
    if item is None:
        return None
    shown = item.text(0)
    original = _orig_map(tree, "_madi_item_orig").get(shown, shown)

    def set_text(text):
        item.setText(0, text)
        _orig_map(tree, "_madi_item_orig")[text] = original

    def _role(role, colour):
        if colour:
            item.setData(0, role, QBrush(QColor(colour)))
        else:
            # None restores the view's default - which matters here, because the
            # rail's group headers are deliberately painted theme.TEXT_HEAD and
            # clearing must not turn them black.
            item.setData(0, role, None)

    return Target(tree, "item", original, shown, where="rail entry",
                  set_text=set_text,
                  set_fg=lambda c: _role(Qt.ForegroundRole, c),
                  set_bg=lambda c: _role(Qt.BackgroundRole, c),
                  label="entry “%s”" % shown)


def target_at(widget, pos):
    """What a right-click at *pos* over *widget* would edit, or None."""
    if isinstance(widget, QTabBar):
        # On a tab: that tab. Past the last tab: the bar itself, which is how
        # the strip behind the tabs gets recoloured.
        return _tab_target(widget, pos) or _widget_target(widget)
    if isinstance(widget, QTreeWidget):
        return _item_target(widget, pos) or _widget_target(widget)
    if isinstance(widget, QWidget):
        return _widget_target(widget)
    return None


# ------------------------------------------------------------------- apply


def _apply_widget(widget):
    target = _widget_target(widget)
    rec = STORE.get(target.key)
    if not rec:
        return 0
    target.push(rec)
    return 1


def _apply_tabs(bar):
    done = 0
    memory = _orig_map(bar, "_madi_tab_orig")
    for i in range(bar.count()):
        shown = bar.tabText(i)
        original = memory.get(shown, shown)
        rec = STORE.get(_key(bar, "tab", original))
        if not rec:
            continue
        if rec.get("text") and rec["text"] != shown:
            bar.setTabText(i, rec["text"])
            memory[rec["text"]] = original
            done += 1
        if rec.get("fg"):
            bar.setTabTextColor(i, QColor(rec["fg"]))
            done += 1
    bar_rec = STORE.get(_key(bar, "tabbar", ""))
    if bar_rec:
        _restyle(bar, extra=_tabbar_qss(bar, bar_rec))
        done += 1
    return done


def _apply_items(tree):
    done = 0
    memory = _orig_map(tree, "_madi_item_orig")
    stack = [tree.topLevelItem(i) for i in range(tree.topLevelItemCount())]
    while stack:
        item = stack.pop()
        if item is None:
            continue
        stack.extend(item.child(i) for i in range(item.childCount()))
        shown = item.text(0)
        original = memory.get(shown, shown)
        rec = STORE.get(_key(tree, "item", original))
        if not rec:
            continue
        if rec.get("text") and rec["text"] != shown:
            item.setText(0, rec["text"])
            memory[rec["text"]] = original
            done += 1
        if rec.get("fg"):
            item.setData(0, Qt.ForegroundRole, QBrush(QColor(rec["fg"])))
            done += 1
        if rec.get("bg"):
            item.setData(0, Qt.BackgroundRole, QBrush(QColor(rec["bg"])))
            done += 1
    return done


def apply_all(root):
    """Apply every stored edit to *root* and everything under it.

    Called after the window is built, after the licence unlock rebuilds the
    gated tabs, and on any widget's Show - a tab that did not exist when the app
    started still has to come up edited.
    """
    if not STORE.count() or root is None:
        return 0
    rev = STORE.revision()
    done = 0
    for widget in [root] + root.findChildren(QWidget):
        try:
            if isinstance(widget, QTabBar):
                done += _apply_tabs(widget) + _apply_widget(widget)
            elif isinstance(widget, QTreeWidget):
                done += _apply_items(widget) + _apply_widget(widget)
            else:
                done += _apply_widget(widget)
            # Marked whether or not anything changed: the point is "this widget
            # has been checked against revision N", so the Show hook can skip it.
            widget.setProperty("_madi_rev", rev)
        except RuntimeError:
            # The C++ side went away mid-walk (a preview page being torn down).
            continue
    return done


# ------------------------------------------------------------- text editing


class RichTextDialog(widgets.GuardedDialog):
    """Edit a label's text, with bold and links.

    Deliberately a MARKUP editor with a live preview rather than a WYSIWYG box:
    what gets stored has to be the exact string that will end up in the source,
    and a WYSIWYG editor would hand back Qt's own bloated HTML document instead
    of the four tags anyone would actually write by hand.
    """

    def __init__(self, parent, markup, original):
        super().__init__(parent)
        self.setWindowTitle("Edit text")
        self.resize(560, 340)
        lay = QVBoxLayout(self)

        hint = QLabel("Select some words, then Bold or Link. Or type the tags "
                      "yourself — &lt;b&gt;bold&lt;/b&gt;, &lt;i&gt;italic&lt;/i&gt;, "
                      "&lt;a href=\"https://…\"&gt;link&lt;/a&gt;.")
        hint.setWordWrap(True)
        hint.setObjectName("dim")
        lay.addWidget(hint)

        row = QHBoxLayout()
        for label, slot in (("Bold", lambda: self._wrap("<b>", "</b>")),
                            ("Italic", lambda: self._wrap("<i>", "</i>")),
                            ("Link…", self._link),
                            ("Line break", lambda: self._insert("<br>"))):
            btn = QPushButton(label)
            btn.clicked.connect(slot)
            row.addWidget(btn)
        row.addStretch(1)
        lay.addLayout(row)

        self.editor = QTextEdit()
        self.editor.setAcceptRichText(False)      # markup is edited as TEXT
        self.editor.setPlainText(markup)
        self.editor.textChanged.connect(self._refresh)
        lay.addWidget(self.editor, 1)

        lay.addWidget(QLabel("Preview:"))
        self.preview = QLabel()
        self.preview.setWordWrap(True)
        self.preview.setTextFormat(Qt.RichText)
        self.preview.setMinimumHeight(48)
        lay.addWidget(self.preview)

        self._original = original
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        reset = buttons.addButton("Reset", QDialogButtonBox.ResetRole)
        reset.clicked.connect(lambda: self.editor.setPlainText(original))
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)
        self._refresh()

    def _refresh(self):
        self.preview.setText(self.editor.toPlainText())

    def _insert(self, text):
        self.editor.textCursor().insertText(text)
        self.editor.setFocus()

    def _wrap(self, before, after):
        cursor = self.editor.textCursor()
        if not cursor.hasSelection():
            self._insert(before + after)
            return
        cursor.insertText(before + cursor.selectedText() + after)
        self.editor.setFocus()

    def _link(self):
        url, ok = QInputDialog.getText(self, "Link", "Web address:",
                                       text="https://")
        if not ok or not url.strip():
            return
        cursor = self.editor.textCursor()
        text = cursor.selectedText() or url.strip()
        cursor.insertText('<a href="%s">%s</a>' % (url.strip(), text))
        self.editor.setFocus()

    def markup(self):
        return self.editor.toPlainText().strip()


def edit_text(target, parent=None):
    """Rich text for a label, a plain prompt for everything else."""
    if not target.renameable:
        return None
    if target.rich:
        dlg = RichTextDialog(parent, target.current, target.original)
        if not dlg.exec():
            return None
        text = dlg.markup()
    else:
        text, ok = QInputDialog.getText(
            parent, "Rename", "New text for “%s”:" % _plain(target.original),
            text=target.current)
        if not ok:
            return None
        text = text.strip()
    if not text or text == target.current:
        return None
    target.set_text(text)
    # Set back to what the source says = no override at all.
    STORE.put(target.key, target.original, target.kind, target.where,
              text=None if text == target.original else text)
    return text


# keep the old name working - it is what the menu used to call
rename = edit_text


def pick_colour(target, which, parent=None):
    """Ask for a colour and store it under one of the colour fields."""
    rec = target.record()
    start = QColor(rec.get(which)) if rec.get(which) else QColor("#ffffff")
    titles = {"fg": "Text colour", "bg": "Background colour",
              "bg_sel": "Selected tab background", "bg_one": "This tab's background"}
    colour = QColorDialog.getColor(start, parent, titles.get(which, "Colour"))
    if not colour.isValid():
        return None
    value = _hex(colour)
    extra = {}
    if which == "bg_one" and getattr(target, "one_pos", None):
        extra["one_pos"] = target.one_pos
    STORE.put(target.key, target.original, target.kind, target.where,
              **dict({which: value}, **extra))
    target.push()
    return value


def clear_colours(target):
    """Drop this target's colours and repaint it the way the theme says."""
    fields = {f: None for f in FIELDS if f != "text"}
    STORE.put(target.key, target.original, target.kind, target.where, **fields)
    target.push()


def reset(target):
    """Put a target back to everything the source gives it."""
    STORE.drop(target.key)
    if target.renameable and target.current != target.original:
        target.set_text(target.original)
    target.push({})
    return target.original


def pick_radius(target, parent=None):
    """Ask for a corner radius in pixels and apply it.

    ⚠ A number, not a colour, which is why this is not `pick_colour` with a
    different dialog: 0 is a MEANINGFUL value here (square corners) and the
    record has to keep it. Everything downstream tests `is None` rather than
    truthiness for exactly that reason.
    """
    rec = target.record()
    current = rec.get("radius")
    value, accepted = QInputDialog.getInt(
        parent, "Corner roundness",
        "Corner radius in pixels (0 = square):",
        int(current) if current is not None else 4, 0, MAX_RADIUS, 1)
    if not accepted:
        return None
    STORE.put(target.key, target.original, target.kind, target.where,
              radius=int(value))
    target.push()
    return int(value)


def pick_curving(target, parent=None):
    """Ask how much the Node Editor's wires should bow, and apply it.

    Same is-None contract as pick_radius: 0 is a MEANINGFUL value here
    (dead-straight wires) and the record has to keep it. 0..10 is Blender's
    own Noodle Curving scale, so the number means what a Blender user
    already expects it to mean."""
    rec = target.record()
    current = rec.get("curving")
    value, accepted = QInputDialog.getInt(
        parent, "Edge smoothness",
        "Wire curving, 0 (straight) to 10 (full curve):",
        int(current) if current is not None else 5, 0, 10, 1)
    if not accepted:
        return None
    STORE.put(target.key, target.original, target.kind, target.where,
              curving=int(value))
    target.push()
    return int(value)


def show_menu(target, widget, global_pos):
    rec = target.record()
    menu = QMenu(widget)
    header = menu.addAction("Editing %s" % target.label)
    header.setEnabled(False)
    menu.addSeparator()

    act_text = act_reset_name = None
    if target.renameable:
        act_text = menu.addAction(
            "Edit text (bold, links)…" if target.rich else "Rename…")
        act_reset_name = menu.addAction("Reset text")
        act_reset_name.setEnabled(bool(rec.get("text")))
        menu.addSeparator()

    act_fg = act_bg = act_radius = act_curving = None
    if target.kind != "tabbar":
        act_fg = menu.addAction("Text colour…%s"
                                % (("  (%s)" % rec["fg"]) if rec.get("fg") else ""))
        if target.can_background:
            act_bg = menu.addAction(
                "Background colour…%s"
                % (("  (%s)" % rec["bg"]) if rec.get("bg") else ""))
        if "radius" in target.extras:
            act_radius = menu.addAction(
                "Corner roundness…%s"
                % (("  (%d px)" % rec["radius"])
                   if rec.get("radius") is not None else ""))
        if "curving" in target.extras:
            act_curving = menu.addAction(
                "Edge smoothness…%s"
                % (("  (%d)" % rec["curving"])
                   if rec.get("curving") is not None else ""))

    # A tab's own menu also carries the BAR's background channels, because that
    # is where "make this tab a different colour" actually lives.
    bar_target = None
    bar_actions = {}
    if target.kind == "tab":
        bar_target = _tabbar_target(target.widget,
                                    getattr(target, "tab_index", None))
        bar_rec = bar_target.record()
        menu.addSeparator()
        bar_actions[("bg", bar_target)] = menu.addAction(
            "All tab backgrounds…%s"
            % (("  (%s)" % bar_rec["bg"]) if bar_rec.get("bg") else ""))
        for field, (text, _fn) in bar_target.extras.items():
            act = menu.addAction("%s%s" % (text, ("  (%s)" % bar_rec[field])
                                           if bar_rec.get(field) else ""))
            bar_actions[(field, bar_target)] = act
        act_clear_bar = menu.addAction("Clear tab backgrounds")
        act_clear_bar.setEnabled(bool(bar_rec))
        bar_actions[("__clear__", bar_target)] = act_clear_bar

    menu.addSeparator()
    act_clear = menu.addAction("Clear colours")
    # `is not None`, not truthiness — a saved radius/curving of 0 is a real
    # edit and must light this entry up (the FIELDS comment's own rule).
    act_clear.setEnabled(any(rec.get(f) is not None
                             for f in FIELDS if f != "text"))
    act_reset = menu.addAction("Reset everything here")
    act_reset.setEnabled(bool(rec))
    info = menu.addAction("%d edit(s) saved" % STORE.count())
    info.setEnabled(False)

    chosen = menu.exec(global_pos)
    if chosen is None:
        return
    window = widget.window()
    for (field, tgt), action in bar_actions.items():
        if chosen is action:
            if field == "__clear__":
                clear_colours(tgt)
            else:
                pick_colour(tgt, field, window)
            return
    if chosen is act_text:
        edit_text(target, window)
    elif chosen is act_reset_name:
        target.set_text(target.original)
        STORE.put(target.key, target.original, target.kind, target.where,
                  text=None)
    elif chosen is act_fg:
        pick_colour(target, "fg", window)
    elif chosen is act_bg:
        pick_colour(target, "bg", window)
    elif chosen is act_radius:
        pick_radius(target, window)
    elif chosen is act_curving:
        pick_curving(target, window)
    elif chosen is act_clear:
        clear_colours(target)
    elif chosen is act_reset:
        reset(target)


class DevEditFilter(QObject):
    """Application-wide: right-click edits, while the mode is on.

    An application filter rather than per-widget hooks, for the same reason the
    wheel guard is one (`widgets.NoWheelFilter`): it covers tabs and tools that
    have not been written yet, with no work at the call site.
    """

    def eventFilter(self, obj, event):
        kind = event.type()
        if kind == QEvent.Show and STORE.count() and isinstance(obj, QWidget):
            # A page built late - an unlocked tab, a dialog - still comes up
            # edited. This runs for EVERY show event in the application, so it
            # has to be O(1) once a widget has been seen: the revision stamp
            # left by apply_all says "already checked against these edits", and
            # without it every tab switch would re-walk a whole subtree.
            if obj.property("_madi_rev") != STORE.revision():
                apply_all(obj)
            return False
        if kind != QEvent.ContextMenu or not _enabled:
            return False
        if not isinstance(obj, QWidget) or not obj.isVisible():
            return False
        # Never take over our own editing dialogs - right-clicking inside the
        # markup editor or the colour picker has to keep working normally.
        if isinstance(obj.window(), (QColorDialog, QMenu, RichTextDialog)):
            return False
        try:
            target = target_at(obj, event.pos())
        except RuntimeError:
            return False
        if target is None:
            return False
        show_menu(target, obj, event.globalPos())
        return True


def install(app):
    """Install the editing filter on an application, once. Returns the filter."""
    existing = getattr(app, "_madi_dev_edit", None)
    if existing is not None:
        return existing
    filt = DevEditFilter(app)
    app.installEventFilter(filt)
    app._madi_dev_edit = filt
    return filt


def summary():
    """One line for the settings dialog."""
    n = STORE.count()
    if not n:
        return "No edits yet. They save to %s" % STORE.path()
    return "%d edit(s) saved to %s" % (n, STORE.path())


def clear_all(root=None):
    """Drop every edit and put the UI back to what the source says."""
    n = STORE.count()
    STORE.clear()
    if root is None:
        return n
    # Reversed by hand: apply_all only ever moves forward.
    for widget in [root] + root.findChildren(QWidget):
        try:
            base = widget.property("_madi_base_qss")
            if base is not None:
                widget.setStyleSheet(base)
            if isinstance(widget, QTabBar):
                memory = _orig_map(widget, "_madi_tab_orig")
                for i in range(widget.count()):
                    was = memory.get(widget.tabText(i))
                    if was:
                        widget.setTabText(i, was)
                    widget.setTabTextColor(i, QColor())
            elif isinstance(widget, QTreeWidget):
                memory = _orig_map(widget, "_madi_item_orig")
                stack = [widget.topLevelItem(i)
                         for i in range(widget.topLevelItemCount())]
                while stack:
                    item = stack.pop()
                    if item is None:
                        continue
                    stack.extend(item.child(i) for i in range(item.childCount()))
                    was = memory.get(item.text(0))
                    if was:
                        item.setText(0, was)
                    item.setData(0, Qt.ForegroundRole, None)
                    item.setData(0, Qt.BackgroundRole, None)
            was = widget.property("_madi_orig")
            if was:
                if isinstance(widget, QGroupBox):
                    widget.setTitle(was)
                elif isinstance(widget, TEXT_WIDGETS):
                    widget.setText(was)
        except RuntimeError:
            continue
    return n
