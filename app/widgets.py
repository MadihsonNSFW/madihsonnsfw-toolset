"""Shared custom controls.

`ValueSlider` is the Blender-style drag slider used across the tool tabs:
click-drag left/right to change the value, double-click to type an exact one.

It is a GENERALISED sibling of `anim_layers.DragSlider`, not a replacement.
DragSlider is hardwired to 0..1 and renders a percentage, and Anim Layers is
finished and confirmed live — so it is left exactly as it is rather than
refactored underneath a working feature. This one takes an arbitrary range,
int or float, and an optional unit suffix.
"""

from PySide6.QtCore import (QEvent, QObject, QPointF, QRectF, QSize, Qt,
                            QTimer, Signal)
from PySide6.QtGui import QColor, QFontMetrics, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QAbstractScrollArea,
                               QAbstractSpinBox, QApplication, QCheckBox,
                               QComboBox, QDialog, QInputDialog, QLabel,
                               QSizePolicy,
                               QFrame, QHBoxLayout, QProgressBar, QPushButton,
                               QSlider, QStatusBar, QStyle, QTabBar,
                               QTreeWidget, QTreeWidgetItem, QVBoxLayout,
                               QWidget)

import icons
import theme


class Popover(QFrame):
    """A small panel that points at a status-bar button.

    Marty picked the non-blocking update flow (2026-08-08): *"B"*. So the
    confirm, the result and the finish all appear here instead of in a modal
    — the app stays usable while an update downloads, which is the behaviour
    the updater was written for in the first place.

    ⚠ **`Qt.Popup`, not `Qt.Tool`.** A Popup takes the mouse grab, so a click
    anywhere else closes it — which is what makes "ignore it" a real answer
    and means the thing can never be left stranded on screen. The cost is that
    it is modal-ish to the MOUSE while open; keep it to a couple of buttons.
    """

    def __init__(self, parent=None):
        super().__init__(parent, Qt.Popup)
        self.setObjectName("popover")
        self.setFrameShape(QFrame.StyledPanel)
        self.setStyleSheet(
            "QFrame#popover { background: %s; border: 1px solid %s;"
            " border-radius: 6px; }" % (theme.PANEL, theme.BORDER))
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(6)

        self.title = QLabel()
        self.title.setStyleSheet("color: %s; font-size: 13px;" % theme.TEXT)
        self.body = QLabel()
        self.body.setWordWrap(True)
        self.body.setStyleSheet("color: %s;" % theme.TEXT_DIM)
        lay.addWidget(self.title)
        lay.addWidget(self.body)

        self._buttons = QHBoxLayout()
        self._buttons.setSpacing(6)
        self._buttons.addStretch(1)
        lay.addLayout(self._buttons)
        self.setMaximumWidth(320)

    def set_content(self, title, body, accent=None):
        self.title.setText(title)
        self.title.setStyleSheet(
            "color: %s; font-size: 13px;" % (accent or theme.TEXT))
        self.body.setText(body)
        self.body.setVisible(bool(body))

    def clear_buttons(self):
        """⚠ `setParent(None)` FIRST, then deleteLater.

        Taking a widget out of a layout does NOT hide it — it only stops the
        layout managing its geometry — and `deleteLater` does not run until
        the event loop next turns. Between those two facts the previous
        popover's buttons stayed visible on top of the new ones for a frame,
        which is how "Install" and "Restart now" ended up on the same panel.
        Unparenting takes them off screen immediately.
        """
        while self._buttons.count() > 1:
            item = self._buttons.takeAt(1)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()

    def add_button(self, text, on_click, primary=False):
        button = QPushButton(text)
        if primary:
            button.setStyleSheet("color: #dff0e4; background: #2f5d3d;"
                                 " border: 1px solid #3d7a50;")

        def clicked():
            self.close()
            on_click()

        button.clicked.connect(clicked)
        self._buttons.addWidget(button)
        return button

    def popup_above(self, anchor):
        """Open with the panel's bottom-right corner over `anchor`'s top-right,
        so it reads as belonging to the button that raised it."""
        self.adjustSize()
        if anchor is None or anchor.window() is None:
            self.show()
            return
        corner = anchor.mapToGlobal(anchor.rect().topRight())
        x = corner.x() - self.width() + 2
        y = corner.y() - self.height() - 6
        screen = anchor.window().screen()
        if screen is not None:
            available = screen.availableGeometry()
            x = max(available.left() + 4, min(x, available.right() - self.width() - 4))
            y = max(available.top() + 4, y)
        self.move(x, y)
        self.show()


class StatusBar(QStatusBar):
    """The app's status bar, with a message area we own rather than Qt's.

    Marty, 2026-08-08: *"make sure we see the version of the app in bottom
    left part at all times (small writing)"* — and **at all times** is the
    whole reason this class exists, because between two Qt rules there is no
    way to do it with the stock API:

    ⚠ **A temporary message HIDES every non-permanent widget**
    (`QStatusBar::hideOrShow`), so a version label added with `addWidget()`
    disappears the moment anything calls `showMessage` — which this app does
    from 74 places.
    ⚠ **Permanent widgets are laid out on the RIGHT**, and there is no
    "permanent and leftmost": `insertPermanentWidget(0, …)` still lands to the
    right of the message area. Inserting into the layout directly does not
    help either — the temporary message is *painted* across that area, over
    the top of whatever sits there.

    So the temporary-message mechanism is not used at all. The message is an
    ordinary `QLabel` in the layout, and `showMessage` / `currentMessage` /
    `clearMessage` are re-implemented on it with the same signatures and the
    same `messageChanged` signal. **Every existing call site keeps working
    untouched**, which is the point — 74 of them is far too many to convert,
    and one missed conversion would be a message that silently never appears.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSizeGripEnabled(False)

        # Far left, and never touched again: what build this is. Deliberately
        # a size step down and TEXT_DIM — it is there to be quoted in a bug
        # report, not to compete with the message beside it.
        self.version_label = QLabel()
        self.version_label.setObjectName("versionlabel")
        font = self.version_label.font()
        font.setPointSizeF(max(7.0, font.pointSizeF() - 1.5))
        self.version_label.setFont(font)
        self.version_label.setStyleSheet("color: %s;" % theme.TEXT_DIM)
        self.addWidget(self.version_label)

        self.message_label = QLabel()
        self.message_label.setObjectName("statusmessage")
        # Long messages (a render summary, a traceback's first line) must not
        # be able to push the permanent buttons off the right-hand end.
        self.message_label.setSizePolicy(QSizePolicy.Ignored,
                                         QSizePolicy.Preferred)
        self.addWidget(self.message_label, 1)

        # The update's progress lives in the bar too (Marty picked this over a
        # dialog, 2026-08-08): "nothing blocks" is the whole point, so an
        # update running has to be visible without taking the app away. Sits
        # after the message and before the permanent buttons.
        self.progress_label = QLabel()
        self.progress_label.setStyleSheet("color: %s;" % theme.TEXT_DIM)
        self.progress_bar = QProgressBar()
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setFixedWidth(120)
        self.progress_bar.setFixedHeight(8)
        self.addWidget(self.progress_label)
        self.addWidget(self.progress_bar)
        self.progress_label.hide()
        self.progress_bar.hide()

        self._message_timer = QTimer(self)
        self._message_timer.setSingleShot(True)
        self._message_timer.timeout.connect(self.clearMessage)

    def set_version(self, text):
        self.version_label.setText(text)

    # ------------------------------------------------------------- progress

    def show_progress(self, text, done=0, total=0):
        """Show the strip. `total` of 0 means indeterminate — which is a real
        state here, not a placeholder: the verify/swap/smoke phase after the
        download has no byte count to report, and a bar frozen at 100% would
        read as a hang."""
        self.progress_label.setText(text)
        if total > 0:
            self.progress_bar.setRange(0, total)
            self.progress_bar.setValue(done)
        else:
            self.progress_bar.setRange(0, 0)
        self.progress_label.show()
        self.progress_bar.show()

    def hide_progress(self):
        self.progress_label.hide()
        self.progress_bar.hide()
        self.progress_label.setText("")

    def progress_visible(self):
        return self.progress_bar.isVisible()

    # ------------------------------------------------- the message, our way

    def showMessage(self, text, timeout=0):
        text = "" if text is None else str(text)
        self.message_label.setText(text)
        self.message_label.setToolTip(text)
        self._message_timer.stop()
        if timeout > 0:
            self._message_timer.start(int(timeout))
        self.messageChanged.emit(text)

    def currentMessage(self):
        return self.message_label.text()

    def clearMessage(self):
        self._message_timer.stop()
        if self.message_label.text():
            self.message_label.setText("")
            self.message_label.setToolTip("")
            self.messageChanged.emit("")


class DragCheckBox(QCheckBox):
    """A checkbox you can PAINT across: press one, drag over its neighbours,
    and they all take the state the first one just went to.

    Marty, 2026-08-08: *"if i click and hold and go over all the checkboxes
    (filters) i can deselect or select multiple"* — ticking ten type filters
    one at a time is ten round trips through a refilter.

    Two things make this work, and both are why it lives on the widget rather
    than on the panel:

    ⚠ **Qt grabs the mouse for the widget that was PRESSED.** Every move event
    of the gesture arrives here; the neighbours never see a press at all. So
    this walks the parent's children under the cursor rather than each box
    minding itself — a per-box implementation simply never fires.

    ⚠ **It toggles on PRESS, not on release.** A normal checkbox waits for the
    release and cancels if you slide off it, which is the exact opposite of a
    paint gesture. The cost is that press-and-drag-away no longer cancels; the
    gain is the feature.
    """

    def __init__(self, text="", parent=None):
        super().__init__(text, parent)
        self._paint_to = None      # the state this gesture is applying
        self._painted = ()         # boxes already visited, so a wobble is safe

    def mousePressEvent(self, event):
        if event.button() != Qt.LeftButton:
            super().mousePressEvent(event)
            return
        self._paint_to = not self.isChecked()
        self._painted = {self}
        self.setChecked(self._paint_to)
        event.accept()

    def mouseMoveEvent(self, event):
        if self._paint_to is None:
            super().mouseMoveEvent(event)
            return
        parent = self.parentWidget()
        if parent is not None:
            local = parent.mapFromGlobal(event.globalPosition().toPoint())
            under = parent.childAt(local)
            if (isinstance(under, DragCheckBox) and under not in self._painted
                    and under.isEnabled()):
                self._painted.add(under)
                under.setChecked(self._paint_to)
        event.accept()

    def mouseReleaseEvent(self, event):
        if self._paint_to is None:
            super().mouseReleaseEvent(event)
            return
        self._paint_to = None
        self._painted = ()
        event.accept()


class ElidedLabel(QLabel):
    """A long label that does NOT hold the whole window open.

    ⚠ **A single-line QLabel's `minimumSizeHint` is its FULL text width**,
    and a QStackedWidget's minimum is the widest page — so ONE long hint
    label, in ONE tab, sets the floor for the entire main window. That is
    exactly what happened: Marty could not narrow the app (2026-08-08), and
    the measured floor was **2194 px**, of which the Node Editor's toolbar
    hint alone was **1944**. `main.py` had already met the same bug with
    the library path and elided it by hand; this is that fix made reusable.

    ⚠ **`sizeHint` is computed from the STORED text, never from the label's
    current contents.** Eliding by calling `setText` would otherwise feed
    back on itself — a narrower hint asks the layout for less room, which
    elides further, which shrinks the hint again.
    """

    def __init__(self, text="", parent=None, minimum=90):
        super().__init__(text, parent)
        self._full = text
        self._minimum = minimum
        self.setToolTip(text)          # nothing is lost when it is clipped

    def full_text(self):
        return self._full

    def setText(self, text):
        self._full = text
        self.setToolTip(text)
        super().setText(text)
        self._elide()

    def sizeHint(self):
        size = super().sizeHint()
        size.setWidth(QFontMetrics(self.font()).horizontalAdvance(self._full)
                      + 4)
        return size

    def minimumSizeHint(self):
        size = super().minimumSizeHint()
        size.setWidth(min(self._minimum, self.sizeHint().width()))
        return size

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._elide()

    def _elide(self):
        metrics = QFontMetrics(self.font())
        QLabel.setText(self, metrics.elidedText(
            self._full, Qt.ElideRight, max(0, self.width() - 2)))


class NoWheelFilter(QObject):
    """Application-wide: the wheel SCROLLS, it never edits.

    Qt's default is that a wheel event over a combo, spin box or slider changes
    its value, and over a tab bar switches tab. So scrolling down a settings
    panel silently rewrites whatever happened to sit under the cursor — Marty
    hit it on the Physics tab (2026-08-02) and asked for it everywhere
    (2026-08-03).

    `NoScrollComboBox` and `ValueSlider` each fix themselves, but that only ever
    covers the widgets someone remembered to swap. This covers every tab —
    including tabs not written yet — which is the whole point of doing it here
    rather than as another find-and-replace.

    The event is NOT simply swallowed: it is forwarded to the nearest scrolling
    ancestor, so the panel scrolls exactly as the user expects. Swallowing it
    would trade a wrong value for a dead scroll wheel.

    Deliberately NOT in the target list:
      * QScrollBar / QAbstractScrollArea — they are the scrolling.
      * QAbstractItemView (tables, trees, the open combo popup) — a list that
        cannot be scrolled is worse than useless. An open combo's popup is a
        separate view widget, so picking with the wheel while the list is down
        still works.
    """

    TARGETS = (QComboBox, QAbstractSpinBox, QSlider, QTabBar)

    def eventFilter(self, obj, event):
        if event.type() != QEvent.Wheel or not isinstance(obj, self.TARGETS):
            return False
        area = self._scroll_ancestor(obj)
        if area is not None:
            QApplication.sendEvent(area.viewport(), event)
        return True          # never reaches the widget's own wheelEvent

    @staticmethod
    def _scroll_ancestor(widget):
        node = widget.parentWidget()
        while node is not None:
            if isinstance(node, QAbstractScrollArea):
                return node
            node = node.parentWidget()
        return None


_WHEEL_GUARD = None
_GUARD_FLAG = "_madi_wheel_guarded"


def wheel_guard():
    """The one shared `NoWheelFilter`, created on first use.

    One instance for the whole process on purpose: an event filter holds no
    per-widget state, and 75 copies of it would be 75 objects to keep alive.
    """
    global _WHEEL_GUARD
    if _WHEEL_GUARD is None:
        _WHEEL_GUARD = NoWheelFilter()
    return _WHEEL_GUARD


def guard_wheel(root):
    """Put the wheel guard on every target widget inside `root`. Returns how
    many were newly guarded.

    ⚠⚠ **THIS USED TO BE ONE FILTER ON THE QApplication, AND THAT COST 380 ms
    OF EVERY WINDOW BUILD.** A filter installed on the application object sees
    *every event in the process* — each `ChildAdded`, `Polish`, `Paint` and
    `Timer`, for all 1,778 widgets — and each one is a C++ -> Python call that
    then builds a Python enum just to ask `event.type()`. Measured on
    2026-08-15: **96,515 invocations to build one window**, and the window took
    1,099 ms with the filter against 662 ms without. Installed on the widgets
    that actually need guarding — **75 of those 1,778** — the same build takes
    716 ms and the walk itself costs 7.5 ms. `PERF_PLAN.md` has the table.

    ⚠ **THE GUARANTEE IS NOW EXPLICIT, WHICH IS THE PRICE.** The app-wide
    version covered widgets nobody had written yet; this one covers what it is
    pointed at. Two things keep that honest and both are load-bearing:
    `GuardedDialog` (every dialog guards itself when it is first shown) and
    `app_ui_test`, which sends a REAL wheel event to every target widget in the
    whole window and fails if any of them changed value.

    Idempotent — the flag is a Qt dynamic property rather than a Python set, so
    it lives and dies with the widget and cannot keep a deleted one alive.
    """
    guard = wheel_guard()
    fresh = 0
    for kind in NoWheelFilter.TARGETS:
        for widget in root.findChildren(kind):
            if widget.property(_GUARD_FLAG):
                continue
            widget.installEventFilter(guard)
            widget.setProperty(_GUARD_FLAG, True)
            fresh += 1
    if isinstance(root, NoWheelFilter.TARGETS) and not root.property(_GUARD_FLAG):
        root.installEventFilter(guard)
        root.setProperty(_GUARD_FLAG, True)
        fresh += 1
    return fresh


_SCROLL_FLAG = "_madi_scroll_filtered"


def guard_scroll(root):
    """Put the smooth scroller on every scroll area inside `root`.

    ⚠ **EXACTLY EQUIVALENT TO THE OLD APPLICATION-WIDE INSTALL, BECAUSE
    `SmoothScroller._area_for` ONLY EVER ACCEPTS A VIEWPORT.** It answers
    `None` for anything that is not a scroll area's own viewport, so a filter
    on the QApplication was seeing every event in the process in order to act
    on a handful of widgets — measured 2026-08-15 at **408 ms of one window
    build**, on top of the wheel guard's 380 and devedit's 397.

    Both halves are needed: the **viewport** carries the wheel gesture, and the
    **scroll area itself** is what receives the `Show` that triggers
    `tune_scroll_widget` (per-pixel scrolling — without it the icon grid jumps
    a whole tile row per notch, which Marty asked to have fixed).
    """
    scroller = smooth_scroller()
    fresh = 0
    areas = list(root.findChildren(QAbstractScrollArea))
    if isinstance(root, QAbstractScrollArea):
        areas.append(root)
    for area in areas:
        if area.property(_SCROLL_FLAG):
            continue
        area.installEventFilter(scroller)
        viewport = area.viewport()
        if viewport is not None:
            viewport.installEventFilter(scroller)
        area.setProperty(_SCROLL_FLAG, True)
        # The Show that would have tuned it may already have happened.
        tune_scroll_widget(area)
        fresh += 1
    return fresh


def attach_input_filters(root):
    """Both input filters, for a widget tree that has just been built.

    One call, because forgetting the second one is silent: the wheel would edit
    a combo again, or a grid would go back to jumping a row per notch. Returns
    (wheel targets, scroll areas) newly filtered.
    """
    return guard_wheel(root), guard_scroll(root)


class GuardedDialog(QDialog):
    """A QDialog that guards its own combos, spin boxes and sliders.

    ⚠ **GUARDED ON FIRST SHOW, NOT IN `__init__`** — at construction time the
    subclass has not built its widgets yet, so a walk there would find nothing
    and silently guard an empty tree. A dialog is always shown before anybody
    can put a wheel over it, so `showEvent` is both the earliest correct moment
    and one that cannot be forgotten by a subclass.
    """

    def showEvent(self, event):
        super().showEvent(event)
        attach_input_filters(self)


def install_no_wheel(app):
    """Deprecated: the guard is no longer application-wide (see `guard_wheel`).

    Kept because five test suites and `main()` call it. It now only makes sure
    the shared filter exists — installing it on the application is exactly the
    380 ms mistake this replaced, so it deliberately does NOT do that any more.
    """
    return wheel_guard()


def tune_scroll_widget(area):
    """Make one scroll area scroll by PIXELS, at a sane speed. Idempotent.

    ⚠ THE ITEM-VIEW HALF IS NOT COSMETIC — it is what makes pixel maths mean
    anything at all. A QAbstractItemView defaults to `ScrollPerItem`, and in that
    mode **the scrollbar's range is measured in ITEMS, not pixels**, so anything
    that sets the bar by a pixel count would jump that many ROWS. The icon grid
    is the worst of it: in IconMode a notch moves a whole tile row, which is
    exactly the chunky feel Marty asked about.
    """
    if getattr(area, "_madi_scroll_tuned", False):
        return
    area._madi_scroll_tuned = True
    if isinstance(area, QAbstractItemView):
        area.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        area.setHorizontalScrollMode(QAbstractItemView.ScrollPerPixel)
    for bar in (area.verticalScrollBar(), area.horizontalScrollBar()):
        # Per-pixel mode leaves singleStep at 1, so the keyboard and the
        # scrollbar arrows crawl a pixel at a time.
        if bar is not None and bar.singleStep() < SmoothScroller.LINE_PX:
            bar.setSingleStep(SmoothScroller.LINE_PX)


class SmoothScroller(QObject):
    """Application-wide: a wheel notch GLIDES to its destination.

    Qt scrolls the instant it gets a wheel event, so a notch is a hard jump —
    over an icon grid on `ScrollPerItem` that jump is a whole row. This keeps a
    target per scrollbar and eases toward it, so a flick reads as movement
    rather than as teleporting.

    Installed the same way as the wheel guard, and for the same reason: one
    application filter covers every tab **including tabs not written yet**,
    instead of a find-and-replace that only covers what someone remembered.

    ⚠ **THE FIRST STEP IS APPLIED SYNCHRONOUSLY**, before the event returns.
    Two reasons, and both matter. The wheel has to feel connected — an entirely
    deferred scroll reads as input lag, which is the opposite of the ask. And
    the existing guarantee that "the wheel scrolls the panel instead of editing
    the widget" is asserted synchronously by `tests\app_ui_test.py`; a purely
    animated scroller would make a green test go red while the app still worked,
    which teaches nobody anything.

    ⚠ **An exhausted bar does NOT consume the event.** At the top of a list,
    scrolling up must fall through to whatever scroll area contains it, or a
    nested view becomes a trap that swallows the wheel. This is why `_bar_for`
    checks there is room to move BEFORE claiming the event.
    """

    # ⚠ HOW FAR A NOTCH TRAVELS — the one number to change if it feels wrong.
    # 25 x 4 = 100 px, which is roughly what Chrome and Explorer do. It is
    # deliberately NOT Qt's old behaviour: in `ScrollPerItem` a notch moved
    # `wheelScrollLines` ITEMS, so over the icon grid it jumped up to three
    # TILE ROWS (~800 px). Per-pixel scrolling at that speed would be a blur, and
    # at three text lines (72 px) a big library would crawl. 100 px is the
    # middle: fine-grained enough to read, fast enough to get somewhere.
    LINE_PX = 25            # one "line" — also the scrollbars' singleStep floor
    LINES_PER_NOTCH = 4
    INTERVAL_MS = 16        # ~60 Hz
    # Fraction of the remaining distance eaten per tick. 0.28 lands a notch in
    # ~7 frames (~110 ms): quick enough to feel immediate, slow enough to read.
    EASE = 0.28
    SETTLE_PX = 1           # closer than this and we just land on it

    def __init__(self, parent=None):
        super().__init__(parent)
        self._targets = {}          # QScrollBar -> target value
        self._timer = QTimer(self)
        self._timer.setInterval(self.INTERVAL_MS)
        self._timer.timeout.connect(self._tick)

    # ------------------------------------------------------------- filtering

    def eventFilter(self, obj, event):
        kind = event.type()
        if kind == QEvent.Show:
            # Tune on show rather than on the first wheel: switching scroll mode
            # re-computes the scrollbar range, and doing that mid-gesture would
            # make the first notch jump.
            if isinstance(obj, QAbstractScrollArea):
                tune_scroll_widget(obj)
            return False
        if kind != QEvent.Wheel:
            return False
        area = self._area_for(obj)
        if area is None:
            return False
        # ⚠ The node canvas OWNS its wheel — it zooms (Blender-style). This
        # filter was consuming the gesture to glide the canvas scrollbars
        # instead, so "wheel zooms" worked in every suite (which calls
        # wheelEvent directly) and never once in the running app
        # (2026-08-07). Keyed on the marker property the view already
        # carries for devedit, so this module needs no nodecanvas import.
        if area.property("_madi_wire_canvas"):
            return False
        tune_scroll_widget(area)
        bar = self._bar_for(area, event)
        if bar is None:
            return False            # nothing to scroll here — let it bubble
        self._push(bar, event)
        return True

    @staticmethod
    def _area_for(obj):
        """The scroll area a wheel event over `obj` belongs to.

        Only the VIEWPORT counts. Catching the scroll area itself as well would
        handle the same gesture twice, and catching a scrollbar would fight the
        drag the user is already making with it.
        """
        if not isinstance(obj, QWidget):
            return None
        parent = obj.parentWidget()
        if isinstance(parent, QAbstractScrollArea) and parent.viewport() is obj:
            return parent
        return None

    def _bar_for(self, area, event):
        """Which bar this gesture drives, or None if it cannot move that way."""
        angle = event.angleDelta()
        horizontal = (event.modifiers() & Qt.AltModifier) or \
            (abs(angle.x()) > abs(angle.y()))
        bar = area.horizontalScrollBar() if horizontal \
            else area.verticalScrollBar()
        if bar is None or bar.minimum() == bar.maximum():
            return None
        delta = angle.x() if horizontal else angle.y()
        if not delta:
            return None
        current = self._targets.get(bar, bar.value())
        # ⚠ Already pinned at the end we are heading for? Don't claim the event.
        if delta < 0 and current >= bar.maximum():
            return None
        if delta > 0 and current <= bar.minimum():
            return None
        return bar

    def _push(self, bar, event):
        angle = event.angleDelta()
        horizontal = (event.modifiers() & Qt.AltModifier) or \
            (abs(angle.x()) > abs(angle.y()))
        delta = angle.x() if horizontal else angle.y()
        # A notch is 120 units. Qt inverts sign for scrolling down.
        pixels = (delta / 120.0) * self.LINE_PX * self.LINES_PER_NOTCH
        start = self._targets.get(bar, bar.value())
        target = start - pixels
        target = max(bar.minimum(), min(bar.maximum(), target))
        self._targets[bar] = target
        # Land the first frame NOW — see the class docstring.
        self._advance(bar)
        if self._targets:
            self._timer.start()

    # -------------------------------------------------------------- stepping

    def _advance(self, bar):
        """Move one bar one frame toward its target. -> True while it is live."""
        target = self._targets.get(bar)
        if target is None:
            return False
        try:
            value = bar.value()
        except RuntimeError:
            # The widget was destroyed under us mid-glide.
            self._targets.pop(bar, None)
            return False
        remaining = target - value
        if abs(remaining) <= self.SETTLE_PX:
            bar.setValue(int(round(target)))
            self._targets.pop(bar, None)
            return False
        step = remaining * self.EASE
        # Always move at least a pixel, or the tail of the ease stalls.
        if abs(step) < 1.0:
            step = 1.0 if step > 0 else -1.0
        bar.setValue(int(round(value + step)))
        return True

    def _tick(self):
        for bar in list(self._targets):
            self._advance(bar)
        if not self._targets:
            self._timer.stop()

    def flush(self):
        """Land every in-flight glide immediately. For tests, and for anything
        that needs the scroll position to be final right now."""
        for bar, target in list(self._targets.items()):
            try:
                bar.setValue(int(round(target)))
            except RuntimeError:
                pass
        self._targets.clear()
        self._timer.stop()


_SMOOTH_SCROLLER = None


def smooth_scroller():
    """The one shared `SmoothScroller`, created on first use."""
    global _SMOOTH_SCROLLER
    if _SMOOTH_SCROLLER is None:
        _SMOOTH_SCROLLER = SmoothScroller()
    return _SMOOTH_SCROLLER


def install_smooth_scroll(app):
    """Deprecated: smooth scrolling is attached per scroll area (`guard_scroll`).

    Kept because `main()` and several suites call it. Like `install_no_wheel`,
    it now only makes sure the shared filter exists — putting it on the
    application is the 408 ms mistake this replaced.
    """
    return smooth_scroller()


class NoScrollComboBox(QComboBox):
    """A combo that does NOT change its value when you scroll over it.

    Qt's default is to treat a wheel event over a combo as "next/previous
    item", which means scrolling a settings panel silently rewrites whatever
    happens to be under the cursor. Marty hit exactly that (2026-08-02):
    "scrolling is annoying because it changes options if I keep my mouse in the
    middle". Ignoring the event lets it bubble to the scroll area instead, so
    the wheel only ever scrolls.

    `WheelFocus` is dropped too, so the combo can't grab focus from a scroll
    and start swallowing wheel events that way either.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFocusPolicy(Qt.StrongFocus)

    def wheelEvent(self, event):
        event.ignore()


class ValueSlider(QWidget):
    """Drag-to-set numeric control over an arbitrary range."""

    valueChanged = Signal(float)

    DRAG_RANGE_PX = 180.0     # mouse travel for the full min..max sweep
    FINE = 0.15               # Shift multiplier for precise dragging

    def __init__(self, minimum, maximum, value, decimals=0, suffix="",
                 tooltip="", parent=None):
        super().__init__(parent)
        self._min = float(minimum)
        self._max = float(maximum)
        self._decimals = int(decimals)
        self._suffix = suffix
        self._value = 0.0
        self._start_x = 0.0
        self._start_value = 0.0
        self._dragging = False
        self._moved = False

        self.setFixedHeight(22)
        # A modest explicit minimum, NOT the text width: an explicit
        # setMinimumWidth overrides the text-based minimumSizeHint, which is
        # what keeps the window shrinkable (see docs\app-shell.md).
        self.setMinimumWidth(90)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setCursor(Qt.SizeHorCursor)
        base = tooltip or "Click-drag to change, double-click to type"
        self.setToolTip("%s\n(range %s to %s; hold Shift while dragging for "
                        "fine control)"
                        % (base, self._fmt(self._min), self._fmt(self._max)))
        self.setValue(value)

    # ------------------------------------------------------------- value

    def _fmt(self, v):
        if self._decimals <= 0:
            return "%d" % round(v)
        return "%.*f" % (self._decimals, v)

    def value(self):
        """int for whole-number sliders, float otherwise — so callers and
        tests get the type the setting actually is."""
        if self._decimals <= 0:
            return int(round(self._value))
        return self._value

    def setValue(self, v):
        try:
            v = float(v)
        except (TypeError, ValueError):
            return
        v = max(self._min, min(self._max, v))
        if self._decimals <= 0:
            v = float(round(v))
        else:
            v = round(v, self._decimals)
        if v != self._value:
            self._value = v
            self.update()
            self.valueChanged.emit(v)

    # ----------------------------------------------------------- painting

    def _fraction(self):
        span = self._max - self._min
        if span <= 0:
            return 0.0
        return max(0.0, min(1.0, (self._value - self._min) / span))

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        if not self.isEnabled():
            p.setOpacity(0.35)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setPen(QPen(QColor(theme.BORDER)))
        p.setBrush(QColor(theme.BG))
        p.drawRoundedRect(r, 3, 3)
        frac = self._fraction()
        if frac > 0.0:
            fill = QRectF(r.x() + 1, r.y() + 1,
                          max(0.0, (r.width() - 2) * frac), r.height() - 2)
            p.setPen(Qt.NoPen)
            c = QColor(theme.ACCENT)
            c.setAlpha(110)
            p.setBrush(c)
            p.drawRoundedRect(fill, 2, 2)
        p.setPen(QPen(QColor(theme.TEXT)))
        text = self._fmt(self._value)
        if self._suffix:
            text += self._suffix
        p.drawText(self.rect(), Qt.AlignCenter, text)

    # -------------------------------------------------------------- mouse

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and self.isEnabled():
            self._start_x = event.position().x()
            self._start_value = self._value
            self._dragging = True
            self._moved = False
            event.accept()

    def mouseMoveEvent(self, event):
        if not self._dragging:
            return
        dx = event.position().x() - self._start_x
        if abs(dx) > 2:
            self._moved = True
        scale = self.FINE if (event.modifiers() & Qt.ShiftModifier) else 1.0
        span = self._max - self._min
        self.setValue(self._start_value
                      + (dx / self.DRAG_RANGE_PX) * span * scale)
        event.accept()

    def mouseReleaseEvent(self, event):
        if self._dragging:
            self._dragging = False
            event.accept()

    def wheelEvent(self, event):
        """Never change value on scroll — the wheel belongs to the panel.

        QWidget already ignores wheel events, but this is stated explicitly so
        nobody 'helpfully' adds scroll-to-adjust later: a settings form that
        rewrites itself as you scroll past is the bug, not the feature.
        """
        event.ignore()

    def mouseDoubleClickEvent(self, event):
        if not self.isEnabled():
            return
        if self._decimals <= 0:
            v, ok = QInputDialog.getInt(self, "Set value", "Value:",
                                        int(round(self._value)),
                                        int(self._min), int(self._max))
        else:
            v, ok = QInputDialog.getDouble(self, "Set value", "Value:",
                                           self._value, self._min, self._max,
                                           self._decimals)
        if ok:
            self.setValue(v)
        event.accept()


class SectionRail(QTreeWidget):
    """The app's top-level navigation: one grouped, icon-led entry per section.

    Replaces the eleven-tab strip that used to sit across the top of the window
    (Marty, 2026-08-14: the strip "looks too cheap"). Eleven identical words in
    one cramped row had no hierarchy and no room left to grow; a rail has both.

    ⚠ **THE TAB BAR IS STILL THERE, JUST HIDDEN, AND THAT IS DELIBERATE.**
    `MainWindow.main_tabs` stays a real QTabWidget, so `addTab`, `tabText`,
    `count`, `widget` and `setCurrentIndex` keep meaning what they meant — 23
    call sites in main.py and four test suites read them. This rail is a
    CONTROLLER for that widget, not a replacement.

    That split gives the shell a property it did not have before: **the tab
    text is now purely an internal key, and the rail label is the human name.**
    Tab text is already a lookup key in four places — `theme.TAB_TINTS`,
    `main.TAB_TEXT_COLORS`, devedit's saved renames and the suites' exact title
    lists — and the old comment in theme.py warned that decorating it would
    quietly break all of them. Now a rename lands on the rail entry and the key
    underneath it cannot move.

    ⚠ **A QTreeWidget ON PURPOSE, NOT A CUSTOM-PAINTED WIDGET.** `devedit`
    already knows how to right-click-rename and recolour a *tree item* — it
    calls them "rail entries", because the tool rails inside Rendering and
    Physics are trees too. Any hand-rolled nav would have silently taken
    Marty's rename-anything mode away from the section names, which is exactly
    where he used it (2026-08-04, and `TAB_TEXT_COLORS` is what came out).

    ⚠ **TINTS AND THE PREMIUM MARK FOLLOW THE CANONICAL TITLE**, stored on the
    item, never the text being displayed — otherwise renaming "NSFW Tools"
    would drop Marty's pink. This is the same bug the tab bar had in reverse
    when the tint was `QTabBar::tab:last` and followed a POSITION.
    """

    sectionChanged = Signal(int)

    ICON = 18            # glyph size; the row is sized off this
    WIDTH = 172          # wide enough for "Studio Library" plus icon and pad
    COMPACT = 56         # squeezed: the glyph, its pill and the scrollbar
    ROW_H = 30
    GROUP_H = 27

    # Item data slots. UserRole carries the tab index the entry drives.
    KEY_ROLE = Qt.UserRole + 1        # icon name (also the FREE_TOOLS key)
    TITLE_ROLE = Qt.UserRole + 2      # canonical title: tints/premium/tests
    LABEL_ROLE = Qt.UserRole + 3      # the shown label, parked while squeezed
    # Below this the labels are word fragments rather than words, so they go
    # entirely and the rail reads as the icon strip it has become.
    # ⚠ MEASURED, not guessed: the longest label ("Studio Library") needs ~88 px
    # of text on top of the glyph, its padding and the row margins. At 122 px —
    # where the rail actually lands when the window is at its minimum — it read
    # "Studio Li…", which is the exact thing this is here to prevent.
    TEXT_CUTOFF = 150

    def __init__(self, tints=None, premium=(), parent=None):
        super().__init__(parent)
        self._tints = dict(tints or {})
        self._premium = set(premium)
        self._entries = []            # QTreeWidgetItem, in tab order
        self._groups = {}
        self._labels_shown = True
        self.setObjectName("sectionrail")
        self.setHeaderHidden(True)
        self.setRootIsDecorated(False)
        self.setIndentation(0)
        self.setIconSize(QSize(self.ICON, self.ICON))
        # ⚠ NOT `setFixedWidth`. Fixed means min == max, so the rail's full
        # width was 172 px of the WINDOW's minimum on every tab — a third of
        # what Marty could drag it down to (2026-08-15: "we need to be able to
        # scale the window a lot"). It still WANTS 172 (see `sizeHint`), so
        # nothing moves at normal sizes; drag the window narrow and Qt squeezes
        # it towards the icons, eliding the labels itself. No mode, no
        # threshold, nothing to keep in step.
        self.setMinimumWidth(self.COMPACT)
        self.setMaximumWidth(self.WIDTH)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.currentItemChanged.connect(self._on_current)
        # ⚠ ONE CLICK OPENS A GROUP, not two (Marty, 2026-08-15). Qt's default
        # is double-click, and with `setRootIsDecorated(False)` there was no
        # branch arrow to click either — so the headings were collapsible and
        # nothing on screen or in the interaction said so. The chevron in
        # `_group_item` is the other half of the same fix.
        self.setExpandsOnDoubleClick(False)
        self.itemClicked.connect(self._on_clicked)
        self.itemExpanded.connect(self._paint_chevron)
        self.itemCollapsed.connect(self._paint_chevron)

    def sizeHint(self):
        """Ask for the full width, so the rail only ever shrinks under real
        pressure. A QTreeWidget's own hint is derived from its contents and is
        both narrower and unstable as entries are renamed."""
        hint = super().sizeHint()
        hint.setWidth(self.WIDTH)
        return hint

    def resizeEvent(self, event):
        """Drop the labels once they would only show fragments.

        Squeezed to icon width, Qt clips "Studio Library" to "Stu" — it does
        not even add an ellipsis — and eleven such fragments read as a broken
        widget rather than a deliberately compact one (rendered at the new
        minimum size and looked at, 2026-08-15). Below the cutoff the text
        goes entirely and the glyphs carry it, with the full name on hover.
        """
        super().resizeEvent(event)
        self._show_labels(self.width() >= self.TEXT_CUTOFF)

    def _show_labels(self, show):
        """⚠ Parks the CURRENT text, not the canonical title — Marty renames
        these through Developer mode: edit, and a rename must survive being
        squeezed and expanded again."""
        if show == self._labels_shown:
            return
        self._labels_shown = show
        for item in self._entries + list(self._groups.values()):
            if show:
                parked = item.data(0, self.LABEL_ROLE)
                if parked is not None:
                    item.setText(0, parked)
            else:
                item.setData(0, self.LABEL_ROLE, item.text(0))
                item.setText(0, "")
        for item in self._entries:
            item.setToolTip(0, "" if show else item.data(0, self.TITLE_ROLE))

    # ---------------------------------------------------------------- build

    def _group_item(self, group):
        """Group header, created on first use. Never selectable — Qt then also
        skips it for arrow-key navigation, so the keyboard walks tools only."""
        node = self._groups.get(group)
        if node is None:
            node = QTreeWidgetItem([group.upper()])
            node.setFlags(node.flags() & ~Qt.ItemIsSelectable)
            # TEXT_HEAD, not TEXT_DIM: the tool rails learned this on
            # 2026-08-03 when their headers measured 4.33:1 and Marty reported
            # them invisible. Same palette, same reason, same fix.
            node.setForeground(0, QColor(theme.TEXT_HEAD))
            font = node.font(0)
            font.setBold(True)
            font.setPointSizeF(max(7.0, font.pointSizeF() - 1.0))
            node.setFont(0, font)
            node.setSizeHint(0, QSize(0, self.GROUP_H))
            self.addTopLevelItem(node)
            node.setExpanded(True)
            self._groups[group] = node
            self._paint_chevron(node)
        return node

    def _paint_chevron(self, item):
        """The open/closed mark on a group heading.

        ⚠ A DRAWN ICON, not Qt's branch indicator. `setRootIsDecorated(False)`
        and zero indentation are what keep the entries aligned with the icons
        beside them, and turning decoration back on would indent every tool row
        to make space for an arrow. Putting the chevron in the heading's own
        icon slot lands it exactly where the tools' glyphs sit, so the column
        reads straight down.
        """
        if item is None or item.childCount() == 0:
            return
        name = "chevron_down" if item.isExpanded() else "chevron_right"
        item.setIcon(0, icons.icon(name, self.ICON, theme.TEXT_HEAD))

    def _on_clicked(self, item, _column):
        """Single click on a heading opens or closes it.

        Tool entries fall through untouched — they are handled by the
        selection, and swallowing their clicks here would fight it.
        """
        if item is not None and item.childCount():
            item.setExpanded(not item.isExpanded())

    def add_section(self, index, title, key="", group=""):
        """Add one entry driving tab *index*. `group` "" puts it at the root."""
        item = QTreeWidgetItem([title])
        item.setData(0, Qt.UserRole, index)
        item.setData(0, self.KEY_ROLE, key)
        item.setData(0, self.TITLE_ROLE, title)
        item.setSizeHint(0, QSize(0, self.ROW_H))
        if group:
            node = self._group_item(group)
            node.addChild(item)
            # ⚠ AFTER the child is in. `_paint_chevron` ignores a childless
            # item — a heading with nothing under it has nothing to open — and
            # at `_group_item` time the group is always still empty, so
            # painting it there drew nothing at all.
            self._paint_chevron(node)
        else:
            self.addTopLevelItem(item)
        self._entries.append(item)
        self._paint_icon(item)
        return item

    # --------------------------------------------------------------- state

    def _paint_icon(self, item):
        """Recolour one entry's glyph for its current state.

        Accent when selected, dim otherwise — the same language the rest of
        the app uses for "you are here"."""
        key = item.data(0, self.KEY_ROLE)
        if not key:
            return
        selected = item is self.currentItem()
        glyph = icons.pixmap(key, self.ICON,
                             theme.ACCENT if selected else theme.TEXT_DIM)
        if item.data(0, self.TITLE_ROLE) in self._premium:
            glyph = self._with_star(glyph)
        item.setIcon(0, QIcon(glyph))

    def _with_star(self, glyph):
        """Badge a glyph members-only.

        ⚠ DORMANT: `MainWindow.GATED` has been empty since 2026-08-14, so
        nothing is premium and this never runs. Kept working rather than
        deleted, because the rail took the responsibility over from
        `SectionTabBar`, which still accepts the same `premium` set."""
        ratio = glyph.devicePixelRatio()
        out = QPixmap(int(round(self.ICON * ratio)),
                      int(round(self.ICON * ratio)))
        out.setDevicePixelRatio(ratio)
        out.fill(Qt.transparent)
        painter = QPainter(out)
        painter.drawPixmap(QPointF(0, 0), glyph)
        painter.drawPixmap(QPointF(self.ICON - 8, 0),
                           icons.pixmap("star", 8, theme.PREMIUM_MARK))
        painter.end()
        return out

    def drawRow(self, painter, option, index):
        """Paint an entry's tint behind it, then let Qt draw the row normally.

        ⚠ **`item.setBackground()` DOES NOT WORK HERE AND LOOKED LIKE IT DID.**
        The moment a view carries any `::item` stylesheet rule, Qt draws items
        through QStyleSheetStyle, which paints its own background and drops the
        model's brush on the floor — so the NSFW pink simply was not there, on
        a rail that otherwise looked finished (spotted in a rendered shot,
        2026-08-14; nothing raised and no test could have caught the colour
        without looking at pixels).

        This is the same division of labour `SectionTabBar` used on the strip
        this rail replaced: hand-paint ONLY the tinted row, and only its
        background. A SELECTED row is left alone — the selection is what tells
        you where you are, and losing it on one entry would be the worse bug.
        """
        item = self.itemFromIndex(index)
        fill = None
        hover = bool(option.state & QStyle.State_MouseOver)
        if item is not None:
            if item.childCount():
                # A GROUP HEADING sits on its own filled bar (Marty picked
                # this one — "B, filled bar" — from six rendered variants,
                # 2026-08-15). It is the strongest separation of the six: a
                # heading row and a tool row cannot be confused at a glance,
                # which was the complaint the chevron only half answered.
                fill = theme.shade(theme.PANEL2, 1.3) if hover else theme.PANEL2
            elif not (option.state & QStyle.State_Selected):
                fill = self._tints.get(item.data(0, self.TITLE_ROLE))
                if fill is None and hover:
                    fill = theme.PANEL
        if fill:
            painter.save()
            painter.setRenderHint(QPainter.Antialiasing, True)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(fill))
            # Inset to the same pill the QSS gives ::item (margin 1px 7px,
            # radius 6px), so a bar and a tint read as the same shape as the
            # selection rather than as full-bleed stripes.
            painter.drawRoundedRect(
                QRectF(option.rect).adjusted(7, 1, -7, -1), 6, 6)
            painter.restore()
        super().drawRow(painter, option, index)

    def _on_current(self, current, previous):
        for item in (previous, current):
            if item is not None:
                self._paint_icon(item)
        if current is None:
            return
        index = current.data(0, Qt.UserRole)
        if index is not None:
            self.sectionChanged.emit(index)

    def set_current_index(self, index):
        """Select the entry driving tab *index*, without re-emitting."""
        for item in self._entries:
            if item.data(0, Qt.UserRole) == index:
                if item is self.currentItem():
                    return
                blocked = self.blockSignals(True)
                self.setCurrentItem(item)
                self.blockSignals(blocked)
                for entry in self._entries:
                    self._paint_icon(entry)
                return

    def entry_for(self, title):
        """The entry whose CANONICAL title is *title* — the tests' way in, and
        proof that a rename never moves the key."""
        for item in self._entries:
            if item.data(0, self.TITLE_ROLE) == title:
                return item
        return None

    def label_for(self, index):
        """What the rail CALLS tab *index* — for the title bar to echo.

        ⚠ NOT `QTabWidget.tabText(index)`. The tab strip is hidden and its text
        stayed the internal KEY on purpose, while the label a person reads (and
        renames, in Developer mode: edit) lives on the rail entry. Reading the
        tab would put the key in the title bar and make every rename look lost.

        ⚠ Reads the PARKED text while the rail is squeezed to icons, where the
        entry's own text is deliberately empty — the title bar has its own room
        and should still say where you are.
        """
        for item in self._entries:
            if item.data(0, Qt.UserRole) != index:
                continue
            if self._labels_shown:
                shown = item.text(0)
            else:
                shown = item.data(0, self.LABEL_ROLE)
            return shown or item.data(0, self.TITLE_ROLE) or ""
        return ""

    def retheme(self):
        """Re-tint after `theme.apply_theme` rebinds the palette. Every glyph
        is drawn in a palette colour, so they all have to be redrawn — and the
        icon cache has to be dropped first or it serves the old theme back."""
        icons.clear_cache()
        for group in self._groups.values():
            group.setForeground(0, QColor(theme.TEXT_HEAD))
            self._paint_chevron(group)   # drawn in TEXT_HEAD, so it moves too
        for item in self._entries:
            self._paint_icon(item)
