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
        Blender is busy. Tools that hit the bridge should disable here; the
        shell itself has nothing to disable yet."""
