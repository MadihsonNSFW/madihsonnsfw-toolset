"""Super focus — focus follows the mouse, between this app and Blender only.

Marty asked for it (2026-08-05): *"A tickbox called Super focus — when it's on,
whenever I hover over blender after doing something in our app windows focuses
on blender, same thing happenes when i hover over the app itself, this is done
so i won't have to click on our app (if our app is not focused) to set a
keyframe or vice versa."*

The point is the SECOND click. Windows gives the click that activates a
background window to the window, not to the button under it, so pressing Set
Keyframe in an unfocused app costs two clicks — one to arrive, one to press.
Focus the window the cursor is already over and the first click does the work.

⚠ IT ONLY EVER FOCUSES THE WINDOW UNDER THE CURSOR, and only when that window
is one of OURS or BLENDER'S. Hovering anything else does nothing at all. A
general focus-follows-mouse would fight the desktop and could raise a window the
user is deliberately keeping behind something; this cannot, because there is no
path in `pick_target` that returns a window it did not recognise.

⚠ Windows-only, and it says so rather than pretending. `available()` is false
everywhere else and the checkbox is hidden — never silently ticked-but-dead.
"""

import ctypes
import os
import sys

from PySide6.QtCore import QObject, Qt, QTimer
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import QApplication

# Blender's top-level window class on Windows (GHOST is its windowing layer).
# Verified against the live 5.2 session: class GHOST_WindowClass, process
# blender.exe. Both are checked — see is_blender().
BLENDER_WINDOW_CLASS = "GHOST_WindowClass"
BLENDER_PROCESS = "blender.exe"

GA_ROOT = 2
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

_user32 = None
_kernel32 = None


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


def _load():
    """Bind the handful of win32 calls this needs, once.

    ⚠ Every restype/argtype is declared. An HWND is a POINTER on 64-bit and
    ctypes defaults a return value to C int, so an undeclared
    GetForegroundWindow hands back a TRUNCATED handle — which compares unequal
    to the real one and makes "am I already focused?" answer no, forever.
    """
    global _user32, _kernel32
    if _user32 is not None or sys.platform != "win32":
        return
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except (OSError, AttributeError):       # pragma: no cover - not Windows
        return
    hwnd = ctypes.c_void_p
    user32.WindowFromPoint.argtypes = [_POINT]
    user32.WindowFromPoint.restype = hwnd
    user32.GetAncestor.argtypes = [hwnd, ctypes.c_uint]
    user32.GetAncestor.restype = hwnd
    user32.GetForegroundWindow.argtypes = []
    user32.GetForegroundWindow.restype = hwnd
    user32.SetForegroundWindow.argtypes = [hwnd]
    user32.SetForegroundWindow.restype = ctypes.c_bool
    user32.BringWindowToTop.argtypes = [hwnd]
    user32.BringWindowToTop.restype = ctypes.c_bool
    user32.GetClassNameW.argtypes = [hwnd, ctypes.c_wchar_p, ctypes.c_int]
    user32.GetClassNameW.restype = ctypes.c_int
    user32.GetWindowThreadProcessId.argtypes = [hwnd,
                                                ctypes.POINTER(ctypes.c_ulong)]
    user32.GetWindowThreadProcessId.restype = ctypes.c_ulong
    user32.AttachThreadInput.argtypes = [ctypes.c_ulong, ctypes.c_ulong,
                                         ctypes.c_bool]
    user32.AttachThreadInput.restype = ctypes.c_bool
    kernel32.GetCurrentThreadId.argtypes = []
    kernel32.GetCurrentThreadId.restype = ctypes.c_ulong
    kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_bool,
                                     ctypes.c_ulong]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_bool
    kernel32.QueryFullProcessImageNameW.argtypes = [
        ctypes.c_void_p, ctypes.c_ulong, ctypes.c_wchar_p,
        ctypes.POINTER(ctypes.c_ulong)]
    kernel32.QueryFullProcessImageNameW.restype = ctypes.c_bool
    _user32, _kernel32 = user32, kernel32


def available():
    """Whether Super focus can work at all on this machine."""
    _load()
    return _user32 is not None


def root_window_at(x, y):
    """The TOP-LEVEL window under a screen point, or 0.

    GetAncestor(GA_ROOT) matters: WindowFromPoint hands back whatever native
    child is under the cursor, and comparing that with a Qt window handle would
    never match.
    """
    if not available():
        return 0
    handle = _user32.WindowFromPoint(_POINT(int(x), int(y)))
    if not handle:
        return 0
    root = _user32.GetAncestor(handle, GA_ROOT)
    return int(root or handle)


def foreground_window():
    if not available():
        return 0
    return int(_user32.GetForegroundWindow() or 0)


def class_name(hwnd):
    if not available() or not hwnd:
        return ""
    buf = ctypes.create_unicode_buffer(256)
    _user32.GetClassNameW(ctypes.c_void_p(hwnd), buf, 256)
    return buf.value


def process_name(hwnd):
    """Base name of the exe owning `hwnd` ('' if it cannot be read).

    Deliberately tolerant: a window belonging to an elevated process refuses to
    open, and that must read as "not Blender", never as an error.
    """
    if not available() or not hwnd:
        return ""
    pid = ctypes.c_ulong(0)
    _user32.GetWindowThreadProcessId(ctypes.c_void_p(hwnd), ctypes.byref(pid))
    if not pid.value:
        return ""
    handle = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False,
                                   pid.value)
    if not handle:
        return ""
    try:
        size = ctypes.c_ulong(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        if not _kernel32.QueryFullProcessImageNameW(handle, 0, buf,
                                                    ctypes.byref(size)):
            return ""
        return os.path.basename(buf.value)
    finally:
        _kernel32.CloseHandle(handle)


def is_blender(hwnd):
    """Class first (a cheap string), process name only if that misses — so a
    Blender launched under any wrapper still counts."""
    if class_name(hwnd) == BLENDER_WINDOW_CLASS:
        return True
    return process_name(hwnd).lower() == BLENDER_PROCESS


def focus_window(hwnd):
    """Raise and focus `hwnd`, even from a process that is not the foreground.

    ⚠ Windows REFUSES SetForegroundWindow to a background process — which is
    exactly the case that matters here: Blender has focus and the cursor arrives
    over us. Attaching our input queue to the foreground window's thread for the
    length of the call is the documented way round it; without it the call just
    returns false and the taskbar button flashes instead.
    """
    if not available() or not hwnd:
        return False
    current = foreground_window()
    if current == hwnd:
        return True
    ours = _kernel32.GetCurrentThreadId()
    other = 0
    if current:
        other = _user32.GetWindowThreadProcessId(ctypes.c_void_p(current), None)
    attached = bool(other and other != ours
                    and _user32.AttachThreadInput(ours, other, True))
    try:
        _user32.BringWindowToTop(ctypes.c_void_p(hwnd))
        return bool(_user32.SetForegroundWindow(ctypes.c_void_p(hwnd)))
    finally:
        if attached:
            _user32.AttachThreadInput(ours, other, False)


def pick_target(hwnd, own, foreground):
    """Which window Super focus should raise, or 0 for "leave everything alone".

    Kept as a pure function of three plain values because it is the whole
    safety argument: anything not recognised as ours or Blender's returns 0, and
    a window that already HAS focus returns 0 too — never fight for what we have,
    or the timer re-raises the same window five times a second.
    """
    if not hwnd:
        return 0
    if hwnd == foreground:
        return 0
    if hwnd in own:
        return hwnd
    if is_blender(hwnd):
        return hwnd
    return 0


class SuperFocus(QObject):
    """The poll. Off unless the tickbox is on; ~7 win32 calls a tick."""

    INTERVAL_MS = 150

    def __init__(self, window, parent=None):
        super().__init__(parent if parent is not None else window)
        self._window = window
        self._enabled = False
        self._timer = QTimer(self)
        self._timer.setInterval(self.INTERVAL_MS)
        self._timer.timeout.connect(self.tick)

    @property
    def enabled(self):
        return self._enabled

    def set_enabled(self, on):
        """On is only ever really on where it can work — see available()."""
        self._enabled = bool(on) and available()
        if self._enabled:
            self._timer.start()
        else:
            self._timer.stop()
        return self._enabled

    def own_windows(self):
        """Native handles of our own real windows.

        ⚠ Popups, menus and tooltips are excluded on purpose. They are
        top-level widgets too, but they already hold the interaction, and
        activating one would close it under the user's cursor.
        """
        handles = set()
        app = QApplication.instance()
        if app is None:
            return handles
        for widget in app.topLevelWidgets():
            if not widget.isVisible():
                continue
            if widget.windowType() not in (Qt.Window, Qt.Dialog):
                continue
            try:
                handles.add(int(widget.winId()))
            except (RuntimeError, ValueError):   # deleted C++ side
                continue
        return handles

    def tick(self):
        """One poll. Returns the handle it focused (0 = did nothing), which is
        what the tests read."""
        if not self._enabled:
            return 0
        # ⚠ Never switch focus mid-drag. A drag that crosses the other window —
        # dragging a slider past the edge, a marquee in the viewport — would
        # otherwise have the mouse taken out from under it halfway.
        if QApplication.mouseButtons() != Qt.NoButton:
            return 0
        point = QCursor.pos()
        target = pick_target(root_window_at(point.x(), point.y()),
                             self.own_windows(), foreground_window())
        if target:
            focus_window(target)
        return target
