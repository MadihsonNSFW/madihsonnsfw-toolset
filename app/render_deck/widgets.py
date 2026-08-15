"""Custom widgets — an animated gradient progress bar and a drag-drop overlay."""
from __future__ import annotations

import math

from PySide6.QtCore import QRectF, Qt, QTimer
from PySide6.QtGui import (
    QBrush, QColor, QFont, QGradient, QLinearGradient, QPainter, QPainterPath,
    QPen,
)
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget

import theme


def format_duration(seconds) -> str:
    """Seconds -> M:SS or H:MM:SS; em-dash when unknown."""
    if seconds is None:
        return "—"
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"


def format_mem(stat) -> str:
    """(used, total, pct) in bytes -> 'used/totalG'; em-dash when unknown."""
    if not stat:
        return "—"
    used, total, _pct = stat
    g = 1024 ** 3
    return f"{used / g:.1f}/{total / g:.0f}G"


class _TimerCard(QFrame):
    """One rounded, bluish-gradient stat card: a small title over a big time."""

    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self.setObjectName("timerCard")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(11, 3, 11, 4)
        lay.setSpacing(0)
        self._title = QLabel(title)
        self._title.setObjectName("timerTitle")
        self._value = QLabel("—")
        self._value.setObjectName("timerValue")
        lay.addWidget(self._title)
        lay.addWidget(self._value)
        self.setMaximumHeight(44)
        self._active = None
        self.set_active(False)

    def set_text(self, text: str) -> None:
        self._value.setText(text)

    def set_active(self, active: bool) -> None:
        if active == self._active:  # avoid restyling on every tick
            return
        self._active = active
        if active:
            grad, ring = ("qlineargradient(x1:0,y1:0,x2:1,y2:1, stop:0 #2b4a86, "
                          f"stop:1 {theme.ACCENT})", "#6ba1ff")
            val_col = "#eaf2ff"
        else:
            grad, ring = (f"qlineargradient(x1:0,y1:0,x2:1,y2:1, stop:0 "
                          f"{theme.PANEL}, stop:1 {theme.PANEL2})", theme.BORDER)
            val_col = theme.TEXT
        self.setStyleSheet(
            f"#timerCard {{ background:{grad}; border:1px solid {ring};"
            " border-radius:6px; }"
            f"#timerTitle {{ color:{theme.TEXT_DIM}; font-size:7pt;"
            " font-weight:bold; letter-spacing:1px; }"
            f"#timerValue {{ color:{val_col}; font-size:13pt; font-weight:bold;"
            " font-family:'Consolas','Courier New',monospace; }}")


class FrameTimers(QWidget):
    """The stat strip: live CURRENT frame elapsed, LAST finished frame duration,
    the .blend's per-frame TIME LIMIT, and live system RAM + GPU VRAM usage —
    all as matching gradient cards."""

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        self._current = _TimerCard("CURRENT FRAME")
        self._last = _TimerCard("LAST FRAME")
        self._limit = _TimerCard("TIME LIMIT")
        self._limit.setToolTip(
            "Cycles' per-frame Time Limit read from the .blend "
            "(Render Properties ▸ Sampling ▸ Render ▸ Time Limit).\n"
            "When set, a frame stops at whichever comes first — the sample "
            "count finishing or the clock running out — so the frame bar "
            "tracks the clock too and is marked ⏱.")
        self._ram = _TimerCard("RAM")
        self._vram = _TimerCard("VRAM")
        for c in (self._current, self._last, self._limit, self._ram, self._vram):
            lay.addWidget(c)

    def set_current(self, seconds, active: bool) -> None:
        self._current.set_text(format_duration(seconds))
        self._current.set_active(active)

    def set_last(self, seconds) -> None:
        self._last.set_text(format_duration(seconds))

    def set_limit(self, seconds) -> None:
        """Highlight the card only when the .blend actually has a limit set."""
        on = bool(seconds and seconds > 0)
        self._limit.set_text(format_duration(seconds) if on else "off")
        self._limit.set_active(on)

    def set_ram(self, stat) -> None:
        self._ram.set_text(format_mem(stat))

    def set_vram(self, stat) -> None:
        self._vram.set_text(format_mem(stat))

    def show_cards(self, sys_cards=True, extras=True) -> None:
        """Responsive collapse: drop RAM/VRAM first, then LAST FRAME and
        TIME LIMIT, as the tool gets narrower. CURRENT FRAME always stays."""
        self._ram.setVisible(sys_cards)
        self._vram.setVisible(sys_cards)
        self._last.setVisible(extras)
        self._limit.setVisible(extras)

    def reset(self) -> None:
        self.set_current(None, False)
        self.set_last(None)
        self.set_limit(None)


class AnimatedProgressBar(QWidget):
    """A rounded progress bar whose fill is a gradient that flows sideways while
    a render is active. `setProgress` advances it frame-by-frame; the fill also
    eases smoothly toward each new target so frames flow one after another."""

    PATTERN = 140  # px width of one repeat of the moving gradient

    def __init__(self, parent=None, compact=False, colors=(theme.ACCENT, "#4fc0c0")):
        super().__init__(parent)
        self._compact = compact
        self._c1, self._c2 = colors
        self.setMinimumHeight(9 if compact else 26)
        self._target = 0.0     # where the fill should be (0..1)
        self._value = 0.0      # where it is right now (eased)
        self._phase = 0.0      # gradient scroll offset
        self._text = "" if compact else "Idle"
        self._busy = False     # indeterminate (e.g. reading frame range)
        self._active = False

        self._timer = QTimer(self)
        self._timer.setInterval(33)  # ~30 fps
        self._timer.timeout.connect(self._tick)

    # ---- public API ----
    def setProgress(self, done: int, total: int, text: str = "") -> None:
        self._busy = False
        self._target = (done / total) if total else 0.0
        if not self._compact:
            self._text = text or f"{int(self._target * 100)}%"
        self._ensure_running()

    def setBusy(self, text: str = "Working…") -> None:
        self._busy = True
        self._text = text
        self._ensure_running()

    def setActive(self, on: bool) -> None:
        self._active = on
        if on:
            self._ensure_running()
        # When turning off we keep the last fill but stop the flow. Snap to the
        # target first: with the timer stopped there's nothing left to ease the
        # fill toward it, and a bar set-then-deactivated (a paused job restored
        # from the last session, or a finished one) would render empty.
        if not on and not self._busy:
            self._value = self._target
            self._timer.stop()
            self.update()

    def reset(self, text: str = "Idle") -> None:
        self._busy = False
        self._active = False
        self._target = self._value = 0.0
        self._text = text
        self._timer.stop()
        self.update()

    # ---- animation ----
    def _ensure_running(self):
        if not self._timer.isActive():
            self._timer.start()

    def _tick(self):
        self._phase = (self._phase + 6.0) % self.PATTERN
        # ease the visible fill toward the target
        if abs(self._value - self._target) > 0.001:
            self._value += (self._target - self._value) * 0.18
        else:
            self._value = self._target
        # stop the timer once settled and nothing needs animating
        if (not self._busy and not self._active
                and abs(self._value - self._target) <= 0.001):
            self._timer.stop()
        self.update()

    # ---- paint ----
    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        radius = 3 if self._compact else 6

        track = QPainterPath()
        track.addRoundedRect(QRectF(0, 0, w, h), radius, radius)
        p.fillPath(track, QColor("#131519"))

        fill_frac = 1.0 if self._busy else self._value
        fw = max(0.0, min(1.0, fill_frac)) * w
        if fw > 1:
            clip = QPainterPath()
            clip.addRoundedRect(QRectF(0, 0, fw, h), radius, radius)
            p.setClipPath(clip)

            grad = QLinearGradient(self._phase, 0, self._phase + self.PATTERN, 0)
            grad.setSpread(QGradient.RepeatSpread)
            grad.setColorAt(0.0, QColor(self._c1))
            grad.setColorAt(0.5, QColor(self._c2))
            grad.setColorAt(1.0, QColor(self._c1))
            p.fillRect(QRectF(0, 0, fw, h), QBrush(grad))

            # soft top highlight for a glossy look
            gloss = QLinearGradient(0, 0, 0, h)
            gloss.setColorAt(0.0, QColor(255, 255, 255, 45))
            gloss.setColorAt(0.5, QColor(255, 255, 255, 0))
            p.fillRect(QRectF(0, 0, fw, h), QBrush(gloss))
            p.setClipping(False)

        if not self._compact and self._text:
            p.setPen(QColor("#e8eaed"))
            f = QFont()
            f.setPointSizeF(8.5)
            f.setBold(True)
            p.setFont(f)
            p.drawText(QRectF(0, 0, w, h), Qt.AlignCenter, self._text)
        p.end()


class DropOverlay(QWidget):
    """A translucent overlay shown while dragging files over the window: a
    pulsing, marching-ants drop zone during hover, and a bright flash on drop."""

    def __init__(self, parent):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setAcceptDrops(False)
        self.hide()
        self._dash = 0.0
        self._pulse = 0.0
        self._flash = 0.0      # 1 -> 0 fade after a drop
        self._hovering = False
        self._timer = QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self._tick)

    # ---- states ----
    def begin(self):
        self._hovering = True
        self._flash = 0.0
        self.setGeometry(self.parent().rect())
        self.raise_()
        self.show()
        if not self._timer.isActive():
            self._timer.start()

    def end(self):
        self._hovering = False
        if self._flash <= 0:
            self._timer.stop()
            self.hide()

    def flash(self):
        """Play the drop confirmation, then hide."""
        self._hovering = False
        self._flash = 1.0
        self.setGeometry(self.parent().rect())
        self.raise_()
        self.show()
        if not self._timer.isActive():
            self._timer.start()

    def _tick(self):
        self._dash = (self._dash + 1.2) % 28
        self._pulse += 0.14
        if self._flash > 0:
            self._flash = max(0.0, self._flash - 0.10)  # 2x faster drop flash
        if not self._hovering and self._flash <= 0:
            self._timer.stop()
            self.hide()
            return
        self.update()

    # ---- paint ----
    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = self.rect()

        base_alpha = 120 if self._hovering else 0
        flash_alpha = int(90 * self._flash)
        p.fillRect(r, QColor(18, 20, 24, base_alpha + flash_alpha // 2))

        m = 26
        box = QRectF(r.adjusted(m, m, -m, -m))
        accent = QColor("#4fc07a") if self._flash > 0 else QColor(theme.ACCENT)

        # pulsing glow fill inside the drop zone
        glow = 26 + int(18 * (math.sin(self._pulse) * 0.5 + 0.5))
        if self._flash > 0:
            glow = int(120 * self._flash)
        fill = QColor(accent)
        fill.setAlpha(glow)
        path = QPainterPath()
        path.addRoundedRect(box, 16, 16)
        p.fillPath(path, fill)

        # marching-ants dashed border
        pen = QPen(accent, 3, Qt.DashLine)
        pen.setDashPattern([6, 5])
        pen.setDashOffset(self._dash)
        p.setPen(pen)
        p.drawPath(path)

        # a big + and a label, scaled up slightly on drop
        cx, cy = box.center().x(), box.center().y() - 14
        s = 20 + 6 * self._flash
        p.setPen(QPen(QColor("#eef1f4"), 4, Qt.SolidLine, Qt.RoundCap))
        p.drawLine(cx - s, cy, cx + s, cy)
        p.drawLine(cx, cy - s, cx, cy + s)

        p.setPen(QColor("#eef1f4"))
        f = QFont()
        f.setPointSizeF(12)
        f.setBold(True)
        p.setFont(f)
        label = "Dropped!" if self._flash > 0 else "Drop .blend files to add"
        p.drawText(QRectF(box.left(), cy + 28, box.width(), 30),
                   Qt.AlignHCenter | Qt.AlignTop, label)
        p.end()
