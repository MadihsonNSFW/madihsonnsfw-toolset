"""The app's own window chrome — title bar, window buttons, and the Win32
hooks that let us draw them without losing what Windows does for free.

Marty, 2026-08-15: he wanted the window's edges — title bar included — styled
like a modern desktop app rather than a default Windows one, and picked concept
**A, "seamless rail"** out of five renders:
the rail runs to the very top carrying the app mark, and the strip beside it
is only a drag zone with the open section's name and the window buttons.

⚠ **THE NATIVE FRAME IS NOT REMOVED — ITS NON-CLIENT *AREA* IS.** The obvious
route is `Qt.FramelessWindowHint`, and it is the wrong one: it strips
`WS_CAPTION`/`WS_THICKFRAME`, and with them go the **drop shadow**, the
**Windows 11 rounded corners**, the **native resize borders** and the
**maximise animation**. A window with square corners and no shadow looks
*more* like a cheap application than the stock title bar does, which is the
opposite of the point. So the window keeps its real frame and we answer
`WM_NCCALCSIZE` with "the client area is the whole window" — Windows still
owns the frame, still rounds it, still shadows it; there is simply nothing of
it left to paint over our content.

⚠ **`WM_NCHITTEST` HAS TO BE ANSWERED TOO, AND IT IS WHERE THE BUTTONS LIVE
OR DIE.** With the client area covering the caption, `DefWindowProc` still
reports `HTCAPTION` for the top strip — so Qt would never see a click there
and the three window buttons would be dead. `_Chrome.hit_test` returns
`HTCAPTION` only over bare title-bar background and `HTCLIENT` over anything
interactive. Getting that backwards costs either the buttons or the dragging.

⚠ **DRAGGING IS WINDOWS', NOT OURS.** Returning `HTCAPTION` buys the native
move loop, which means Aero Snap, drag-to-top-to-maximise and
double-click-to-maximise all work with no code. It also means **the window
can be maximised without our button being pressed**, so `WindowControls`
watches the window's state rather than tracking its own clicks.

⚠ **A WINDOW FLAG CHANGE RE-CREATES THE HWND.** `MainWindow._set_on_top`
flips `WindowStaysOnTopHint`, and Qt destroys and re-creates the native
window to do it — the old HWND is gone and the new one has a stock title bar
until `reinstall()` runs. That was invisible in every test that never touched
the 📌 button.

**Not wired up:** the Windows 11 Snap Layouts flyout (hovering the maximise
button). It needs `HTMAXBUTTON`, which hands the button to the non-client
area and so takes its click and its hover away from Qt — both would have to
be re-implemented by hand, and a maximise button that misses clicks is a
worse bug than a missing flyout. Everything else about snapping still works.

If the native install fails for any reason, `install()` returns False and the
window keeps its ordinary Windows title bar: on a machine where this does not
work the app must still be usable, not chromeless and undraggable.
"""

import ctypes
import sys

from PySide6.QtCore import QAbstractNativeEventFilter, QEvent, QPoint, Qt
from PySide6.QtWidgets import (QAbstractButton, QAbstractItemView,
                               QAbstractSlider, QApplication, QComboBox,
                               QHBoxLayout, QLabel, QLineEdit, QPushButton,
                               QWidget)

import icons
import theme
import widgets

# The title bar's height, shared by both columns so the app mark lines up
# with the window buttons across the seam.
BAR_H = 40

# Anything of these types swallows its own clicks, so the hit test must call
# it client area. Everything else in the bar is background you can drag by.
INTERACTIVE = (QAbstractButton, QLineEdit, QComboBox, QAbstractSlider,
               QAbstractItemView)

WINDOWS = sys.platform == "win32"


# ----------------------------------------------------------------- widgets

class _ControlButton(QPushButton):
    """One window button: transparent at rest, and the close one turns red."""

    def __init__(self, glyph, danger=False, parent=None):
        super().__init__(parent)
        self._glyph = glyph
        self._danger = danger
        self.setObjectName("winclose" if danger else "winbtn")
        self.setFixedSize(38, 26)
        self.setFocusPolicy(Qt.NoFocus)
        self.setCursor(Qt.ArrowCursor)
        self._tint(theme.TEXT)

    def set_glyph(self, glyph):
        self._glyph = glyph
        self._tint(theme.TEXT)

    def _tint(self, color):
        self.setIcon(icons.icon(self._glyph, 15, color))

    def enterEvent(self, event):
        # ⚠ White, and only on the close button: its hover fill is a strong
        # red and TEXT grey on it reads as a disabled control.
        self._tint("#ffffff" if self._danger else theme.TEXT)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._tint(theme.TEXT)
        super().leaveEvent(event)


class WindowControls(QWidget):
    """Minimise, maximise/restore, close."""

    def __init__(self, window, parent=None):
        super().__init__(parent)
        self._window = window
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(2)
        self.minimise = _ControlButton("win_min")
        self.minimise.setToolTip("Minimise")
        self.maximise = _ControlButton("win_max")
        self.close_button = _ControlButton("win_close", danger=True)
        self.close_button.setToolTip("Close")
        for button in (self.minimise, self.maximise, self.close_button):
            row.addWidget(button)
        self.minimise.clicked.connect(window.showMinimized)
        self.maximise.clicked.connect(self.toggle_maximised)
        self.close_button.clicked.connect(window.close)
        window.installEventFilter(self)
        self.sync()

    def toggle_maximised(self):
        if self._window.isMaximized():
            self._window.showNormal()
        else:
            self._window.showMaximized()

    def eventFilter(self, obj, event):
        if obj is self._window and event.type() == QEvent.WindowStateChange:
            self.sync()
        return False

    def sync(self):
        """Match the button to the window's actual state.

        ⚠ NOT toggled from our own click. Windows maximises the window itself
        on a double-click or a drag to the top edge (see the module note on
        `HTCAPTION`), and a button that only tracked its own presses showed
        "maximise" on an already-maximised window.
        """
        maxed = self._window.isMaximized()
        self.maximise.set_glyph("win_restore" if maxed else "win_max")
        self.maximise.setToolTip("Restore down" if maxed else "Maximise")

    def retheme(self):
        for button in (self.minimise, self.maximise, self.close_button):
            button.set_glyph(button._glyph)


class RailHeader(QWidget):
    """The app mark, on the rail's surface, above the rail."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("railheader")
        self.setFixedHeight(BAR_H)
        self.setMinimumWidth(0)
        self.setMaximumWidth(widgets.SectionRail.WIDTH)
        row = QHBoxLayout(self)
        row.setContentsMargins(11, 0, 8, 0)
        row.setSpacing(9)
        self.mark = QLabel()
        self.mark.setPixmap(icons.pixmap("appmark", 17, theme.ACCENT))
        self.name = QLabel("MADI Toolset")
        self.name.setObjectName("railwordmark")
        row.addWidget(self.mark)
        row.addWidget(self.name)
        row.addStretch(1)

    def minimumSizeHint(self):
        """Let the column squeeze to whatever the rail can squeeze to.

        ⚠ **`setMinimumWidth(0)` IS NOT ENOUGH, AND THAT IS NOT OBVIOUS.**
        A parent layout sizes a child by `qSmartMinSize`, which for the default
        (shrinkable) size policy uses the child's **minimumSizeHint**, and only
        *then* lets an explicit `minimumSize` raise it — it can never lower it.
        This widget's layout hints 189 px because of the wordmark, so the rail
        column's floor became 172 (the header's own maximum) instead of the
        rail's 56, and the window's minimum width jumped 549 → 682 px. Measured
        by walking the columns, because nothing about it looks wrong on screen.
        """
        hint = super().minimumSizeHint()
        hint.setWidth(0)
        return hint

    def resizeEvent(self, event):
        """Drop the wordmark exactly where the rail drops its labels, so the
        column compacts as one thing rather than in two steps."""
        super().resizeEvent(event)
        self.name.setVisible(self.width() >= widgets.SectionRail.TEXT_CUTOFF)

    def retheme(self):
        self.mark.setPixmap(icons.pixmap("appmark", 17, theme.ACCENT))


class TitleStrip(QWidget):
    """The drag zone beside the rail: where you are, and the window buttons."""

    def __init__(self, window, parent=None):
        super().__init__(parent)
        self.setObjectName("titlestrip")
        self.setFixedHeight(BAR_H)
        row = QHBoxLayout(self)
        row.setContentsMargins(15, 0, 7, 0)
        row.setSpacing(8)
        # ⚠ Elided, and with a tiny minimum: a section can be renamed to
        # anything in Developer mode: edit, and a plain QLabel's minimum is its
        # whole text — one long name would hold the window open (widgets.py).
        self.section = widgets.ElidedLabel("", minimum=36)
        self.section.setObjectName("titlesection")
        self.controls = WindowControls(window)
        row.addWidget(self.section, 1)
        row.addWidget(self.controls, 0, Qt.AlignVCenter)

    def set_section(self, text):
        self.section.setText(text or "")

    def retheme(self):
        self.controls.retheme()


# ------------------------------------------------------------------ win32

WM_NCCALCSIZE = 0x0083
WM_NCHITTEST = 0x0084

HTCLIENT, HTCAPTION = 1, 2
HTLEFT, HTRIGHT, HTTOP, HTTOPLEFT = 10, 11, 12, 13
HTTOPRIGHT, HTBOTTOM, HTBOTTOMLEFT, HTBOTTOMRIGHT = 14, 15, 16, 17

SM_CXFRAME, SM_CYFRAME, SM_CXPADDEDBORDER = 32, 33, 92
SWP_NOSIZE, SWP_NOMOVE, SWP_NOZORDER = 0x0001, 0x0002, 0x0004
SWP_FRAMECHANGED, SWP_NOACTIVATE = 0x0020, 0x0010
DWMWA_WINDOW_CORNER_PREFERENCE, DWMWCP_ROUND = 33, 2

_filter = None          # the one app-wide filter; kept alive deliberately


# ⚠ Defined only on Windows, and that is not defensiveness for its own sake:
# `import ctypes.wintypes` RAISES on Linux and macOS, and these structures
# reference its types while the class body runs. Importing this module has to
# stay harmless everywhere — the test suites import it on whatever they are
# run on, and the widgets above are pure Qt.
if WINDOWS:
    import ctypes.wintypes

    RECT = ctypes.wintypes.RECT
    MSG = ctypes.wintypes.MSG

    class _NCCalcSizeParams(ctypes.Structure):
        _fields_ = [("rgrc", RECT * 3),
                    ("lppos", ctypes.c_void_p)]
else:
    RECT = MSG = _NCCalcSizeParams = None


def _unsigned(value, bits=64):
    """A ctypes LPARAM arrives signed; an address must not."""
    return value if value >= 0 else value + (1 << bits)


def _frame_metrics(hwnd):
    """Frame thickness in PHYSICAL pixels, for the monitor this window is on.

    ⚠ **`GetSystemMetrics` alone is wrong on a mixed-DPI desktop.** Qt makes
    the process per-monitor DPI aware, but that API still answers for the
    PRIMARY monitor — so maximising onto a 150 % screen would inset by the
    100 % screen's border and leave the window a few pixels off. Marty runs
    Windows display scaling, so this is his normal case, not an edge one.
    The `…ForDpi` pair is Windows 10 1607+; older builds fall through to the
    plain metrics, which are then correct anyway because they cannot be mixed.
    """
    user32 = ctypes.windll.user32
    try:
        dpi = user32.GetDpiForWindow(hwnd)
        if dpi:
            get = user32.GetSystemMetricsForDpi
            pad = get(SM_CXPADDEDBORDER, dpi)
            return (get(SM_CXFRAME, dpi) + pad, get(SM_CYFRAME, dpi) + pad)
    except Exception:
        pass
    get = user32.GetSystemMetrics
    pad = get(SM_CXPADDEDBORDER)
    return (get(SM_CXFRAME) + pad, get(SM_CYFRAME) + pad)


class _Chrome(object):
    """One window's native state: which HWND, and what can be dragged."""

    def __init__(self, window, hwnd, drag):
        self.window = window
        self.hwnd = hwnd
        self.drag = list(drag)

    # -- WM_NCCALCSIZE -----------------------------------------------------
    def calc_size(self, lparam):
        """Client area = the whole window, so nothing of the frame is drawn.

        ⚠ EXCEPT MAXIMISED. Windows sizes a maximised window to the work area
        *plus* the frame thickness, expecting the frame to eat the difference.
        With no frame left to eat it the window overhangs the screen on all
        four sides and covers the taskbar, so the borders are subtracted back
        off here.
        """
        if not ctypes.windll.user32.IsZoomed(self.hwnd):
            return
        params = _NCCalcSizeParams.from_address(_unsigned(lparam))
        cx, cy = _frame_metrics(self.hwnd)
        params.rgrc[0].left += cx
        params.rgrc[0].right -= cx
        params.rgrc[0].top += cy
        params.rgrc[0].bottom -= cy

    # -- WM_NCHITTEST ------------------------------------------------------
    def hit_test(self, lparam):
        """Ask Windows where the window is, then hand off to `classify`."""
        packed = _unsigned(lparam) & 0xFFFFFFFF
        x = ctypes.c_short(packed & 0xFFFF).value
        y = ctypes.c_short((packed >> 16) & 0xFFFF).value
        rect = RECT()
        ctypes.windll.user32.GetWindowRect(self.hwnd, ctypes.byref(rect))
        return self.classify(x - rect.left, y - rect.top,
                             rect.right - rect.left, rect.bottom - rect.top,
                             bool(ctypes.windll.user32.IsZoomed(self.hwnd)))

    def classify(self, px, py, width, height, zoomed):
        """Which part of the window is at (px, py), in PHYSICAL pixels?

        ⚠ Split out from `hit_test` so it can be tested at all. The half above
        needs a live HWND — there isn't one under the offscreen platform the
        suites run on — while this half is where every decision that matters
        is made, including the one that decides whether the window buttons
        receive their clicks.
        """
        ratio = float(self.window.devicePixelRatioF() or 1.0)

        # resize edges first — they win over the title bar in the corners,
        # which is what makes the window resizable from its top edge at all
        if not zoomed:
            edge = max(4, int(round(6 * ratio)))
            left, right = px < edge, px >= width - edge
            top, bottom = py < edge, py >= height - edge
            if top and left:
                return HTTOPLEFT
            if top and right:
                return HTTOPRIGHT
            if bottom and left:
                return HTBOTTOMLEFT
            if bottom and right:
                return HTBOTTOMRIGHT
            if left:
                return HTLEFT
            if right:
                return HTRIGHT
            if top:
                return HTTOP
            if bottom:
                return HTBOTTOM

        point = QPoint(int(px / ratio), int(py / ratio))
        for widget in self.drag:
            if widget is None or not widget.isVisible():
                continue
            local = widget.mapFrom(self.window, point)
            if not widget.rect().contains(local):
                continue
            child = widget.childAt(local)
            if child is None or not isinstance(child, INTERACTIVE):
                return HTCAPTION
            break
        return HTCLIENT


class _NativeFilter(QAbstractNativeEventFilter):
    """One filter for the whole app; windows register themselves by HWND."""

    def __init__(self):
        super().__init__()
        self.windows = {}

    def nativeEventFilter(self, event_type, message):
        if event_type != b"windows_generic_MSG":
            return False, 0
        try:
            msg = MSG.from_address(int(message))
            entry = self.windows.get(int(msg.hWnd))
            if entry is None:
                return False, 0
            if msg.message == WM_NCCALCSIZE and msg.wParam:
                entry.calc_size(msg.lParam)
                return True, 0
            if msg.message == WM_NCHITTEST:
                return True, entry.hit_test(msg.lParam)
        except Exception:
            # ⚠ SWALLOWED ON PURPOSE. This runs for every message the window
            # receives; an exception escaping here would be raised thousands
            # of times a second, and a chrome detail is never worth taking the
            # app down for. A failure just falls through to Windows' default.
            return False, 0
        return False, 0


def available():
    """Can this platform have the custom chrome at all?"""
    return WINDOWS


def install(window, drag=()):
    """Hand `window` its own title bar. False = keep the Windows one.

    `drag` is the widgets whose background moves the window.
    """
    global _filter
    if not WINDOWS:
        return False
    try:
        app = QApplication.instance()
        if app is None:
            return False
        if _filter is None:
            _filter = _NativeFilter()
            app.installNativeEventFilter(_filter)
        hwnd = int(window.winId())
        # ⚠ Drop any earlier HWND for this window first. Qt re-creates the
        # native window when a flag changes, and a stale entry would keep
        # answering for a handle Windows has already recycled — possibly to
        # somebody else's window.
        for old in [h for h, e in _filter.windows.items() if e.window is window]:
            del _filter.windows[old]
        window._madi_chrome_drag = list(drag)
        _filter.windows[hwnd] = _Chrome(window, hwnd, drag)
        _round_corners(hwnd)
        ctypes.windll.user32.SetWindowPos(
            hwnd, 0, 0, 0, 0, 0,
            SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE
            | SWP_FRAMECHANGED)
        return True
    except Exception:
        return False


def reinstall(window):
    """Re-apply after Qt re-created the native window (the 📌 pin button)."""
    drag = getattr(window, "_madi_chrome_drag", None)
    if not drag:
        return False
    return install(window, drag)


def _round_corners(hwnd):
    """Ask DWM for the Windows 11 corner radius.

    Windows 11 rounds a normal window anyway; this states it explicitly so the
    intent survives someone later changing the window styles. Silently does
    nothing on Windows 10, where the attribute is unknown.
    """
    try:
        pref = ctypes.c_int(DWMWCP_ROUND)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd, DWMWA_WINDOW_CORNER_PREFERENCE,
            ctypes.byref(pref), ctypes.sizeof(pref))
    except Exception:
        pass
