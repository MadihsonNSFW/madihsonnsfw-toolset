"""The panel a locked tab shows instead of its tools.

Deliberately NOT hidden or greyed-out tools: the tab stays in the tab bar and
explains what it does. Someone who has not paid should be able to see exactly
what they would get, and someone who HAS paid should never have to guess why a
tab is empty.

THE PREVIEW BEHIND THE LOCK is a PICTURE, not the real tab. The page is built
once with a dead bridge, painted into a pixmap, and thrown away immediately.
It looks identical to running the real thing - because it is rendered from the
real thing, so it can never go stale - but nothing is alive back there:

  * no bridge traffic (the Anim Layers page starts a 1.5 s poll in its
    constructor; a live preview would sit there talking to Blender forever),
  * no timers, no worker threads, no Render Queue touching job data,
  * and nothing an overlay could be removed to get at, because after the grab
    there are no widgets left - only pixels.
"""

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QPainter
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QInputDialog, QLabel,
                               QLineEdit, QPushButton, QSizePolicy, QVBoxLayout,
                               QWidget)

import theme
import version

from . import manager as mgr


def free_tabs_line():
    """"Seven tabs are free…" — COUNTED, never typed.

    ⚠ The sentence this replaces named three free tabs for three days after
    there were seven, because it was a hand-written list sitting next to a
    list that kept changing. Reading `MainWindow`'s own tuples means freeing a
    tab updates the lock screen by itself.

    The import is deliberately LAZY: `main` imports this module, so a
    top-level import here would be circular. By the time a locked page is
    built, `main` is long since loaded.
    """
    try:
        import main
        free = (["Studio Library"]
                + [title for _key, title in main.MainWindow.FREE_TOOLS]
                + ["What's New"])
    except Exception:
        # A missing sentence is better than a wrong one, and better still than
        # a lock screen that will not open.
        return "The rest of the Toolset is free."
    return ("The other %d tabs are free and always will be: %s."
            % (len(free), ", ".join(free)))


class LockedPage(QWidget):
    """One locked tab. *blurb* is what this tab does, in the user's words.

    *preview_factory* is called at most once, the first time this page is
    actually shown, and should return a QPixmap of the real tab (or None).
    Doing it lazily keeps startup fast: a locked user only pays for the tabs
    they actually look at.
    """

    # Enough to read the shape of the tab through, not enough to use it.
    DIM = 202

    def __init__(self, manager, title, blurb, parent=None, preview_factory=None):
        super().__init__(parent)
        self.manager = manager
        self._link = ""
        self._preview = None
        self._preview_factory = preview_factory
        self._preview_tried = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 24, 24, 24)
        outer.addStretch(1)

        card = QFrame()
        card.setObjectName("lockcard")
        card.setStyleSheet(
            "#lockcard { background: %s; border: 1px solid %s; border-radius: 10px; }"
            % (theme.PANEL, theme.BORDER)
        )
        card.setMaximumWidth(560)
        lay = QVBoxLayout(card)
        lay.setContentsMargins(28, 24, 28, 24)
        lay.setSpacing(10)

        head = QLabel(title)
        head.setObjectName("h1")
        lay.addWidget(head)

        # ⚠ NAME THE TIER. "Members only" left the one question that actually
        # decides whether somebody pays — WHICH tier — unanswered on the only
        # screen where they are asking it (Marty, 2026-08-09).
        sub = QLabel("Tier 3 supporters on Patreon")
        sub.setStyleSheet("color: %s; font-weight: 600;" % theme.TEXT_HEAD)
        lay.addWidget(sub)

        body = QLabel(blurb)
        body.setWordWrap(True)
        body.setStyleSheet("color: %s;" % theme.TEXT_HEAD)
        lay.addWidget(body)

        rule = QFrame()
        rule.setFrameShape(QFrame.HLine)
        rule.setStyleSheet("color: %s;" % theme.BORDER)
        lay.addWidget(rule)

        # --- the pairing code, only while linking
        self.code_label = QLabel("")
        self.code_label.setAlignment(Qt.AlignCenter)
        self.code_label.setStyleSheet(
            "font-family: Consolas, monospace; font-size: 22px; font-weight: 600;"
            " letter-spacing: 3px; color: %s; background: %s; border: 1px solid %s;"
            " border-radius: 6px; padding: 12px;" % (theme.TEXT, theme.BG, theme.BORDER)
        )
        self.code_label.hide()
        lay.addWidget(self.code_label)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.status.setStyleSheet("color: %s;" % theme.TEXT_HEAD)
        # Long messages must not be able to widen the card.
        self.status.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        lay.addWidget(self.status)

        row = QHBoxLayout()
        self.unlock_button = QPushButton("Unlock with Patreon")
        self.unlock_button.clicked.connect(self.manager.unlock)
        row.addWidget(self.unlock_button)

        self.open_button = QPushButton("Open the page again")
        self.open_button.setObjectName("flat")
        self.open_button.clicked.connect(self._open_link)
        self.open_button.hide()
        row.addWidget(self.open_button)

        self.move_button = QPushButton("Move my licence here")
        self.move_button.clicked.connect(self.manager.move_seat)
        self.move_button.hide()
        row.addWidget(self.move_button)

        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setObjectName("flat")
        self.cancel_button.clicked.connect(self.manager.cancel_unlock)
        self.cancel_button.hide()
        row.addWidget(self.cancel_button)

        self.key_button = QPushButton("Use a licence key")
        self.key_button.setObjectName("flat")
        self.key_button.setToolTip(
            "Bought the Toolset outside Patreon? Enter the key you were given.")
        self.key_button.clicked.connect(self._enter_key)
        row.addWidget(self.key_button)

        # Only ever shown when there IS a licence to re-check (see _on_state).
        # Named for what it does: "I already have a licence" read like the way
        # to enter one, which is the button beside it.
        self.recheck_button = QPushButton("Check again")
        self.recheck_button.setObjectName("flat")
        self.recheck_button.setToolTip(
            "Ask the licence server again - if your licence was freed up or "
            "restored, this picks it up straight away")
        self.recheck_button.clicked.connect(lambda: self.manager.recheck(quiet=False))
        row.addWidget(self.recheck_button)
        row.addStretch(1)
        lay.addLayout(row)

        # ⚠ THIS USED TO PROMISE "unlocks this permanently - it stays yours
        # whether or not you keep pledging". That stopped being true on
        # 2026-08-06 when licences became annual, and a lock screen making a
        # promise the server will not keep is the worst possible place for a
        # stale sentence — it is read by exactly the people about to pay.
        #
        # ⚠ AND IT WENT STALE A SECOND WAY. It still named three free tabs long
        # after there were SEVEN (Bone picker, Anim Layers and Node Setup were
        # freed 2026-08-06, the Node Editor on 2026-08-08). Under-selling the
        # free half on the screen that asks for money is the opposite of what
        # this panel is for. Both counts now come from `main.MainWindow`'s own
        # lists rather than being typed here again — see `free_tabs_line()`.
        foot = QLabel(
            '<b>Tier 3</b> on Patreon unlocks these three tabs for a year. '
            'Renewing needs a Tier 3 pledge that is still active at the time.'
            '<br>%s<br>'
            'Bugs and questions: <a href="%s">Discord</a>&nbsp;&nbsp;·&nbsp;&nbsp;'
            '<a href="%s">Patreon</a>'
            % (free_tabs_line(), version.DISCORD_URL, version.PATREON_URL)
        )
        foot.setWordWrap(True)
        foot.setOpenExternalLinks(True)
        foot.setStyleSheet("color: %s; font-size: 12px;" % theme.TEXT_DIM)
        lay.addWidget(foot)

        middle = QHBoxLayout()
        middle.addStretch(1)
        middle.addWidget(card)
        middle.addStretch(1)
        outer.addLayout(middle)
        outer.addStretch(1)

        manager.stateChanged.connect(self._on_state)
        manager.messageChanged.connect(self._on_message)
        manager.pairingStarted.connect(self._on_pairing)
        manager.busyChanged.connect(self._on_busy)
        self._on_state(manager.state)

    # ---------------------------------------------------------- preview

    def showEvent(self, event):
        """Render the preview the first time this tab is opened, never at
        startup - a locked user should not pay for four tabs they may not
        even click on."""
        super().showEvent(event)
        if self._preview_tried or self._preview_factory is None:
            return
        self._preview_tried = True
        try:
            self._preview = self._preview_factory()
        except Exception:
            self._preview = None  # no preview is a cosmetic loss, never a bug
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        if self._preview is not None and not self._preview.isNull():
            scaled = self._preview.scaled(
                self.size(), Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
            painter.drawPixmap((self.width() - scaled.width()) // 2, 0, scaled)
            # Darken hard. The point is to show what is back there, not to
            # make it usable - and the card on top has to stay readable.
            colour = QColor(theme.BG)
            colour.setAlpha(self.DIM)
            painter.fillRect(self.rect(), colour)
        super().paintEvent(event)

    # ------------------------------------------------------------------

    def _enter_key(self):
        """Ask for a manually issued licence key.

        Whatever they paste is tidied up before being sent - case, spaces and
        dashes are all optional - and a mistyped key is caught here by its
        check character rather than costing a round trip.
        """
        key, accepted = QInputDialog.getText(
            self, "Licence key", "Enter your licence key:", QLineEdit.Normal, "")
        if accepted and key.strip():
            self.manager.redeem(key)

    def _open_link(self):
        if self._link:
            QDesktopServices.openUrl(QUrl(self._link))

    def _on_pairing(self, code, link):
        self._link = link
        self.code_label.setText(code)
        self.code_label.show()
        self.open_button.show()
        self.status.setText(
            "Finish signing in with Patreon in your browser, then come back here.\n"
            "The page should show this same code."
        )
        QDesktopServices.openUrl(QUrl(link))

    def _on_message(self, text):
        self.status.setText(text)

    def _on_busy(self, busy):
        self.unlock_button.setEnabled(not busy)
        self.move_button.setEnabled(not busy)
        self.recheck_button.setEnabled(not busy)
        self.key_button.setEnabled(not busy)

    def _on_state(self, state):
        linking = state == mgr.LINKING
        conflict = state == mgr.SEAT_CONFLICT
        expired = state == mgr.EXPIRED
        # Renewing IS the Patreon flow, but "Unlock with Patreon" reads like a
        # first purchase to someone who has already bought once and is asking
        # why their tabs stopped working.
        self.unlock_button.setText("Renew with Patreon" if expired
                                   else "Unlock with Patreon")
        self.unlock_button.setVisible(not linking and not conflict)
        self.cancel_button.setVisible(linking)
        # `recheck()` does nothing without a stored token, so offering it then
        # is a button that visibly ignores you. It earns its place only where
        # something could actually have changed server-side: a seat freed up
        # elsewhere, or a revoke undone.
        self.recheck_button.setVisible(not linking and self.manager.has_token)
        self.key_button.setVisible(not linking and not conflict)
        self.move_button.setVisible(conflict)
        if not linking:
            self.code_label.hide()
            self.open_button.hide()
        if conflict:
            self.status.setText(
                "Your licence is active on another computer. One licence covers one "
                "machine at a time - moving it here will sign the other one out."
            )
        elif state == mgr.REVOKED:
            self.status.setText("This licence was withdrawn. Please contact support.")
        elif expired:
            self.status.setText(self.manager.expiry_message())
        elif state in (mgr.STALE, mgr.GRACE_EXPIRED):
            # ⚠ These lock the tabs now, so they land HERE and must say
            # something. Before 2026-08-06 they never reached this panel at all
            # (both were unlocked states), which would have left the card blank
            # under a lock icon with no explanation of what to do.
            self.status.setText(self.manager.message or
                                "Your licence could not be confirmed recently. "
                                "Connect to the internet and it unlocks again.")
        elif state == mgr.UNLICENSED and not self.status.text():
            self.status.setText("")
