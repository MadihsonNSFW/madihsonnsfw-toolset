"""The "What's New" tab — release notes, shipped with the build.

Marty, 2026-08-04: "A tab that can be used to post update logs later when
pushing updates."

WHERE THE NOTES COME FROM
`CHANGELOG.md` next to the app, written by hand and packed into the exe like
any other data file. Deliberately NOT fetched from the licence server:

⚠ **The notes have to be readable with no network and no licence.** Someone
whose key has lapsed, or who is offline, still needs to see what changed in the
build they are running — that is the one moment release notes are worth
anything. A server-fetched page would be blank in exactly that case.

⚠ **AND THIS TAB IS NOT GATED.** It is the only tab besides Studio Library that
is not, and that is the point: what a paid build contains is precisely what
someone deciding whether to pay should be able to read.
"""
import os
import sys

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QPushButton, QTextBrowser,
                               QVBoxLayout, QWidget)

import theme
import version

CHANGELOG = "CHANGELOG.md"

EMPTY = """# No release notes yet

Notes for each release go in **CHANGELOG.md**, next to the app. Whatever is in
that file appears here.
"""


def changelog_path():
    """Where the notes live.

    ⚠ NEXT TO THE EXE FIRST, then next to this module. In a frozen build
    `__file__` points inside the bundle (`_internal`), which is not somewhere
    anyone can reasonably edit — and the whole point of this file is that notes
    can be written for a release. A copy sitting beside the exe therefore wins,
    and the bundled one is the fallback that guarantees the tab is never empty.
    """
    candidates = []
    if getattr(sys, "frozen", False):
        candidates.append(os.path.join(os.path.dirname(sys.executable),
                                       CHANGELOG))
    candidates.append(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   CHANGELOG))
    for path in candidates:
        if os.path.isfile(path):
            return path
    return candidates[0]


def read_notes(path=None):
    """The notes as Markdown text. Never raises: an unreadable changelog is a
    cosmetic problem, and a tab that crashes the app over one would not be."""
    path = path or changelog_path()
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read().strip()
    except OSError:
        return EMPTY
    return text or EMPTY


class UpdatesPage(QWidget):
    """Release notes for this build, plus the version it is."""

    def __init__(self, window=None, parent=None):
        super().__init__(parent)
        self.window = window

        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 12)
        lay.setSpacing(8)

        head = QHBoxLayout()
        title = QLabel("What's New")
        title.setObjectName("h1")
        head.addWidget(title)
        self.version_label = QLabel("You are on %s" % version.APP_VERSION)
        self.version_label.setObjectName("dim")
        head.addWidget(self.version_label)
        head.addStretch(1)
        self.btn_reload = QPushButton("Reload")
        self.btn_reload.setObjectName("flat")
        self.btn_reload.setToolTip("Re-read CHANGELOG.md from disk")
        self.btn_reload.clicked.connect(self.reload)
        head.addWidget(self.btn_reload)
        lay.addLayout(head)

        self.view = QTextBrowser()
        # Links go to the browser rather than trying to load inside the tab —
        # a QTextBrowser that navigates away from the notes has no way back.
        self.view.setOpenExternalLinks(True)
        self.view.setStyleSheet(
            "QTextBrowser { background: %s; border: 1px solid %s;"
            " border-radius: 6px; padding: 10px; }" % (theme.PANEL,
                                                       theme.BORDER))
        # ⚠ A QTextBrowser folds its DOCUMENT's ideal width into its minimum,
        # so the release notes were setting this tab's 756 px floor — and the
        # widest tab sets the whole window's. It scrolls both ways, so an
        # explicit minimum costs nothing but lets the window get small.
        self.view.setMinimumWidth(220)
        self.view.setMinimumHeight(120)
        lay.addWidget(self.view, 1)

        self.status = QLabel("")
        # Wraps rather than pushing the window wider: this line carries file
        # paths and error text, which is exactly the unbounded kind.
        self.status.setWordWrap(True)
        self.status.setObjectName("dim")
        self.status.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(self.status)

        self.reload()

    def reload(self):
        path = changelog_path()
        # setMarkdown, not hand-rolled HTML: Qt renders headings, lists, bold
        # and links itself, so the notes stay a plain file anyone can edit
        # without knowing what the app will do to the markup.
        self.view.setMarkdown(read_notes(path))
        self.status.setText(
            "From %s" % path if os.path.isfile(path)
            else "No %s found next to the app yet." % CHANGELOG)

    def set_capture_busy(self, busy):
        """Nothing here touches Blender, so a busy bridge changes nothing —
        but the window greys every page and expects this to exist."""
