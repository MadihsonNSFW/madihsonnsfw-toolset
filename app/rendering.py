"""Rendering tab — render / compositing tooling.

Layout A: a grouped tool rail on the left, the selected tool's settings on the
right (same rail -> content -> action shape as the library tab).

Empty on purpose. To add a tool, build a QWidget and call

    page.add_tool(MyToolWidget(), "Turntable", group="Render")

The rail entry, the group header and the page header are made for you, and the
empty state clears itself once the first tool lands.
"""

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QFrame, QLabel, QScrollArea, QSplitter,
                               QStackedWidget, QTreeWidget, QTreeWidgetItem,
                               QVBoxLayout, QWidget)

import theme


class ToolPage(QWidget):
    """A tool's settings pane: title header + the tool's own widget below.

    `scroll=False` mounts the widget directly — for tools that manage their own
    scrolling (tables, log panes), where a wrapping scroll area would nest
    scrollbars."""

    def __init__(self, title, widget, parent=None, scroll=True):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 12)
        lay.setSpacing(8)
        head = QLabel(title)
        head.setObjectName("h1")
        # ⚠ WRAPPED, because a heading is a sentence for some tools. A
        # single-line QLabel reports its FULL text width as its minimum, a
        # QStackedWidget takes the widest page, and the NSFW torus heading
        # ("Penetration torus that you will use…") was holding the WHOLE window
        # open at 952 px on its own. Same disease as the Node Editor hint that
        # made ElidedLabel necessary (2026-08-08) — but a heading you want to
        # read in full, so it wraps rather than elides.
        head.setWordWrap(True)
        lay.addWidget(head)
        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setStyleSheet("color: %s;" % theme.BORDER)
        lay.addWidget(line)
        if scroll:
            area = QScrollArea()
            area.setWidgetResizable(True)
            area.setFrameShape(QFrame.NoFrame)
            self._gutter(widget)
            area.setWidget(widget)
            # ⚠⚠ A SCROLL AREA DOES NOT ABSORB ITS CHILD'S MINIMUM ON ITS OWN.
            # With `widgetResizable`, QScrollArea folds the widget's
            # minimumSizeHint into its own — so a 738 px-wide tool still set a
            # 738 px floor and the scrolling never got a chance to happen. An
            # EXPLICIT minimum overrides the hint (Qt's qSmartMinSize prefers a
            # set minimumSize), which is what finally lets the scrollbars do
            # their job. This one line is most of why the window can now be
            # dragged small (Marty, 2026-08-15: "we need to be able to scale
            # the window a lot").
            # ⚠ Keep it modest but not zero: below this a settings pane is all
            # scrollbar and no content.
            area.setMinimumWidth(220)
            area.setMinimumHeight(140)
            lay.addWidget(area, 1)
        else:
            # ⚠ `scroll=False` means the tool scrolls ITSELF (a table, a tree,
            # a log). Its own chrome does not, though, and a long button label
            # inside one — "Measure the open .blend" — was still setting a
            # 530 px floor for the whole window. Cap it: the scrolling part
            # keeps working and the chrome squeezes.
            widget.setMinimumWidth(min(260, widget.minimumSizeHint().width()))
            lay.addWidget(widget, 1)

    # gap between the last widget on a row and the vertical scrollbar
    GUTTER = 8

    @classmethod
    def _gutter(cls, widget):
        """Keep the scrolled tool clear of its own scrollbar.

        ⚠ `widgetResizable` makes the tool EXACTLY viewport-wide, and nearly
        every tool zeroes its own layout margins — so a widget on the right of
        a row gets drawn with not one pixel between it and the scrollbar.
        Nothing is clipped (measured: right edge == viewport width), but a
        button touching a scrollbar READS as a cut-off button, which is how
        Marty reported it (2026-08-16: "feels like buttons got cut off") for
        Anim Layers' Load and Share Keys.

        Widening the tool's own right margin rather than wrapping it in a
        spacer keeps `area.widget()` the tool itself, which the rest of the
        shell and the suites reach through. Only ever widens: a tool that
        already asked for more right margin than this keeps it.
        """
        lay = widget.layout()
        if lay is None:
            return
        m = lay.contentsMargins()
        if m.right() >= cls.GUTTER:
            return
        lay.setContentsMargins(m.left(), m.top(), cls.GUTTER, m.bottom())


class RenderingPage(QWidget):
    """Top-level 'Rendering' tab. Tools are added with add_tool().

    Also the shared LAYOUT A shell: the Physics tab subclasses this rather than
    copying the rail/stack/splitter wiring, so a fix here reaches both. Only
    the empty-state text differs, hence `empty_text`."""

    EMPTY_TEXT = (
        "No rendering tools yet.\n\n"
        "This tab is the home for render and compositing tooling —\n"
        "each tool gets a rail entry on the left and its settings here.")

    def __init__(self, bridge, window, parent=None, empty_text=None):
        super().__init__(parent)
        self.bridge = bridge
        self.window = window
        self._tools = []      # [(title, group, widget)]
        self._groups = {}     # group name -> QTreeWidgetItem

        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)

        split = QSplitter(Qt.Horizontal)

        self.rail = QTreeWidget()
        # Named so the stylesheet can inset its rows without touching every
        # other QTreeWidget — the data trees (markers, presets, the optimizer
        # report) are multi-column, where the same rule would open a gap
        # between every pair of columns. ⚠ The inset is PADDING ON THE TREE,
        # never a margin on the rows: `#toolrail` in theme.py measured what a
        # row margin costs (Qt fills the branch column of the current row in
        # the user's own Windows accent colour).
        self.rail.setObjectName("toolrail")
        self.rail.setHeaderHidden(True)
        self.rail.setIndentation(10)
        self.rail.setRootIsDecorated(False)
        # A wide tool on the right must never crush the rail to a sliver —
        # but 125 was 125 px of the window's own minimum, on every tab built
        # from this shell. Enough for "Bone Jiggle" and a scrollbar.
        self.rail.setMinimumWidth(96)
        self.rail.currentItemChanged.connect(self._on_rail_change)

        self.stack = QStackedWidget()
        self.empty = QLabel(empty_text or self.EMPTY_TEXT)
        self.empty.setAlignment(Qt.AlignCenter)
        self.empty.setObjectName("dim")
        self.stack.addWidget(self.empty)

        split.addWidget(self.rail)
        split.addWidget(self.stack)
        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)
        split.setSizes([160, 700])
        lay.addWidget(split, 1)

    # ------------------------------------------------------------------

    def _group_node(self, group):
        """Group header in the rail, created on first use (never selectable)."""
        node = self._groups.get(group)
        if node is None:
            node = QTreeWidgetItem([group.upper()])
            node.setFlags(node.flags() & ~Qt.ItemIsSelectable)
            # theme.TEXT_HEAD, not palette().mid(): `mid` on a dark palette is
            # near-black, so the group headers were effectively invisible
            # against the rail (reported 2026-08-03). TEXT_DIM would only reach
            # 4.33:1 here; TEXT_HEAD is 7.0:1 and still reads as secondary.
            node.setForeground(0, QColor(theme.TEXT_HEAD))
            font = node.font(0)
            font.setBold(True)
            node.setFont(0, font)
            self.rail.addTopLevelItem(node)
            node.setExpanded(True)
            self._groups[group] = node
        return node

    def add_tool(self, widget, title, group="Render", scroll=True, heading=None):
        """Add a tool: rail entry under *group*, settings page on the right.

        `heading` splits the page's title from the rail entry, for a tool whose
        rail label wants to stay short while its header says more. Defaults to
        the rail label, which is what every tool did before it existed.
        """
        if self.empty is not None:
            self.stack.removeWidget(self.empty)
            self.empty.deleteLater()
            self.empty = None

        page = ToolPage(heading or title, widget, scroll=scroll)
        index = self.stack.addWidget(page)
        node = QTreeWidgetItem([title])
        node.setData(0, Qt.UserRole, index)
        self._group_node(group).addChild(node)
        self._tools.append((title, group, widget))
        if self.stack.count() == 1:  # first real tool -> select it
            self.rail.setCurrentItem(node)
        return page

    def _on_rail_change(self, current, _previous):
        if current is None:
            return
        index = current.data(0, Qt.UserRole)
        if index is not None:
            self.stack.setCurrentIndex(index)

    def set_capture_busy(self, busy):
        """Mirrors LibraryView's API — the window greys every page out while
        Blender is busy.

        ⚠ **THIS FORWARDS TO THE TOOLS, and it did not until 2026-08-19.**
        It was a docstring and nothing else, so the call stopped at the shell:
        the window held the RenderingPage, the RenderingPage held the tools.
        `LayersPage.set_capture_busy` exists, disables the layer stack, and had
        never once been called.

        ⚠ **`PhysicsPage` was the exception** — it overrode this method and
        forwarded, so the Physics tab really did grey out. That override is
        deleted now that the base does the job, so there is ONE implementation
        rather than two that can drift. Anim Layers, Rendering and Node Setup
        are the rails that were silently not greying.

        Found while adding Rig properties, which polls Blender and so must
        genuinely stop while Blender renders. A tool without the method is
        skipped, which is why nothing broke loudly.
        """
        for _title, _group, widget in self._tools:
            handler = getattr(widget, "set_capture_busy", None)
            if handler is not None:
                handler(busy)

    def retheme(self):
        """Re-tint the tools' drawn glyphs after a theme change.

        ⚠ Same shape as `set_capture_busy` above and found the same day: a
        tool that sets a themed icon ONCE keeps the old palette's colour after
        a theme switch, because `icons.clear_cache()` only helps glyphs that
        are asked for again. The window's repaint does not re-ask.
        """
        for _title, _group, widget in self._tools:
            handler = getattr(widget, "retheme", None)
            if handler is not None:
                handler()
