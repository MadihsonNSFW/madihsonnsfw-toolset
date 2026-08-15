"""The MadiRef tab — open a clip, watch it here, put it in the viewport.

Layout follows the rest of the Toolset: a settings column on the right of the
thing being worked on, no modal dialogs, and every Blender-dependent control
degrades to a reason in the status line rather than disappearing.

⚠ The tab WORKS WITHOUT BLENDER. Ingest, this window's player and the audio are
all app-side, so a missing or old add-on costs you the viewport overlay and
nothing else — that is why `madiref_viewport` gates one button here instead of
the tab.
"""

import math
import os

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog,
                               QFrame, QHBoxLayout, QLabel, QProgressBar,
                               QPushButton, QScrollArea, QSizePolicy, QSlider,
                               QSpinBox, QVBoxLayout, QWidget)

import config
import theme

from . import audio as _audio
from . import decoder as _decoder
from . import ingest as _ingest
from . import notes as _notes
from . import shm as _shm

VIDEO_FILTER = ("Video (*.mp4 *.mov *.mkv *.avi *.m4v *.webm *.wmv);;"
                "All files (*)")


class VideoView(QWidget):
    """Shows the frame the decoder just served. Aspect-correct, never upscaled
    past the widget, and black where the picture is not.

    It is also the drawing surface (2026-08-12). ⚠ **The notes are drawn HERE
    as an overlay, not taken from the served frame**, for two reasons: the
    stroke under the cursor has to appear before it exists as a note, and the
    "show in Blender" tickbox must be able to be OFF while you can still see
    what you are drawing.
    """

    # A finished notes.Stroke, and the frame that was on screen when it STARTED.
    # ⚠ The frame is captured at press, not read at release: with Follow
    # Blender on the timeline can move mid-stroke, and a drawing belongs to the
    # frame you were looking at when you began it.
    stroke_drawn = Signal(object, int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._image = None
        self._book = None
        self._frame = 0
        self._pen_on = False
        self._pen_color = _notes.DEFAULT_COLOR
        self._pen_width = _notes.DEFAULT_WIDTH
        self._live = None                # the stroke being dragged
        self._live_frame = 0             # the frame it was started on
        self.setMinimumHeight(220)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setAutoFillBackground(False)

    def set_image(self, image):
        self._image = image
        self.update()

    def clear(self):
        self._image = None
        self._live = None
        self.update()

    # ------------------------------------------------------------- notes

    def set_book(self, book):
        self._book = book
        self.update()

    def set_frame(self, index):
        if index != self._frame:
            self._frame = int(index)
            self.update()

    def set_pen(self, on=None, color=None, width=None):
        if on is not None:
            self._pen_on = bool(on)
            self.setCursor(Qt.CrossCursor if self._pen_on else Qt.ArrowCursor)
        if color is not None:
            self._pen_color = color
        if width is not None:
            self._pen_width = float(width)

    def _image_rect(self):
        """Where the picture actually sits inside the widget.

        ⚠ ONE definition, used by both the paint and the mouse mapping. Two
        copies of this arithmetic is how a drawing lands offset from the
        cursor — the aspect letterboxing makes it easy to get subtly wrong.
        """
        img = self._image
        if img is None or img.isNull():
            return None
        area = self.rect()
        scaled = img.size().scaled(area.size(), Qt.KeepAspectRatio)
        x = area.x() + (area.width() - scaled.width()) // 2
        y = area.y() + (area.height() - scaled.height()) // 2
        return x, y, max(scaled.width(), 1), max(scaled.height(), 1)

    def _to_frame(self, pos):
        """Widget point -> 0..1 of the frame, or None when outside it."""
        box = self._image_rect()
        if box is None:
            return None
        x, y, w, h = box
        fx = (pos.x() - x) / float(w)
        fy = (pos.y() - y) / float(h)
        if not (0.0 <= fx <= 1.0 and 0.0 <= fy <= 1.0):
            return None
        return fx, fy

    def mousePressEvent(self, event):
        if not self._pen_on or event.button() != Qt.LeftButton:
            return super().mousePressEvent(event)
        point = self._to_frame(event.position())
        if point is None:
            return
        self._live = _notes.Stroke([point], self._pen_color, self._pen_width)
        self._live_frame = self._frame
        self.update()

    def mouseMoveEvent(self, event):
        if self._live is None:
            return super().mouseMoveEvent(event)
        point = self._to_frame(event.position())
        # Points outside the picture are DROPPED, not clamped: clamping draws a
        # line along the edge that nobody asked for when the cursor wanders off.
        if point is not None:
            self._live.points.append(point)
            self.update()

    def mouseReleaseEvent(self, event):
        if self._live is None or event.button() != Qt.LeftButton:
            return super().mouseReleaseEvent(event)
        # ⚠ The release POSITION joins the stroke. Qt does not guarantee a move
        # event at the point you let go, so on a quick flick the line stopped
        # short of the cursor — visible as an arrow that does not reach what it
        # is pointing at.
        point = self._to_frame(event.position())
        if point is not None and point != (self._live.points[-1]
                                           if self._live.points else None):
            self._live.points.append(point)
        stroke, self._live = self._live, None
        if stroke.points:
            self.stroke_drawn.emit(stroke, self._live_frame)
        self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.fillRect(self.rect(), Qt.black)
        img = self._image
        if img is None or img.isNull():
            p.setPen(Qt.gray)
            p.drawText(self.rect(), Qt.AlignCenter,
                       "Open a reference clip to begin")
            return
        x, y, w, h = self._image_rect()
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)
        p.drawImage(x, y, img.scaled(w, h, Qt.IgnoreAspectRatio,
                                     Qt.SmoothTransformation))

        strokes = list(self._book.strokes_at(self._frame)) if self._book else []
        if self._live is not None:
            strokes.append(self._live)
        if strokes:
            p.save()
            p.translate(x, y)
            _notes.paint_strokes(p, strokes, w, h)
            p.restore()


class NoteScrubber(QSlider):
    """The clip scrubber, with a tick over every frame that carries a note.

    ⚠ Without these a note is unfindable, and that matters MORE now that a
    drawing shows on one frame only: a 3,800-frame clip is one drag end to end,
    and a note you cannot scrub exactly onto is a note you cannot see at all.
    The ticks and Prev/Next are how you land on it.
    """

    def __init__(self, parent=None):
        super().__init__(Qt.Horizontal, parent)
        self._book = None

    def set_book(self, book):
        self._book = book
        self.update()

    def paintEvent(self, event):
        super().paintEvent(event)
        book = self._book
        span = self.maximum() - self.minimum()
        if book is None or span <= 0 or not book.count():
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, False)
        usable = max(self.width() - 2, 1)
        # Every note carries a drawing now, so every tick means the same thing
        # — the hollow "notes stop here" variant went with the End here button.
        colour = QColor(_notes.DEFAULT_COLOR)
        for frame in book.frames():
            fraction = (frame - self.minimum()) / float(span)
            p.fillRect(1 + int(round(fraction * usable)), 0, 2, 6, colour)
        p.end()


def _section(title):
    lbl = QLabel(title)
    lbl.setStyleSheet("color:%s; font-weight:600; margin-top:8px;"
                      % theme.TEXT_HEAD)
    return lbl


class MadiRefTab(QWidget):
    """Everything the reference needs, in one page."""

    status_message = Signal(str)

    def __init__(self, bridge, parent=None):
        super().__init__(parent)
        self.bridge = bridge
        # The MainWindow, for its config dict — the last-opened clip is
        # remembered there so it comes back on the next launch.
        self._window = parent
        self.player = None
        self.audio = _audio.ReferenceAudio(self)
        self.audio.unavailable.connect(self._on_audio_unavailable)
        self._job = None
        self._source = None
        self._shown_in_blender = False
        self._capture_busy = False
        self._mirroring = False
        # Drawn notes (2026-08-12). `book` is None with no clip open; `_frame`
        # is the proxy frame on screen, which is what a note is keyed by.
        self.book = None
        self._frame = 0

        root = QHBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(10)

        left = QVBoxLayout()
        left.setSpacing(6)
        self.view = VideoView(self)
        self.view.stroke_drawn.connect(self._on_stroke_drawn)
        left.addWidget(self.view, 1)

        self.scrub = NoteScrubber(self)
        self.scrub.setEnabled(False)
        self.scrub.valueChanged.connect(self._on_scrub)
        left.addWidget(self.scrub)

        bar = QHBoxLayout()
        self.lbl_frame = QLabel("—")
        self.lbl_frame.setStyleSheet("color:%s;" % theme.TEXT_DIM)
        bar.addWidget(self.lbl_frame)
        bar.addStretch(1)
        self.lbl_clip = QLabel("No clip")
        self.lbl_clip.setStyleSheet("color:%s;" % theme.TEXT_DIM)
        bar.addWidget(self.lbl_clip)
        left.addLayout(bar)

        self.progress = QProgressBar()
        self.progress.setVisible(False)
        self.progress.setTextVisible(True)
        left.addWidget(self.progress)
        root.addLayout(left, 1)

        panel = QFrame()
        panel.setFixedWidth(268)
        panel.setStyleSheet("QFrame{background:%s;border:1px solid %s;"
                            "border-radius:6px;}" % (theme.PANEL, theme.BORDER))
        # ⚠ THE CONTROLS LIVE IN A SCROLL AREA, not straight in the frame.
        # Thirty-odd stacked controls gave this panel an 816 px minimum HEIGHT,
        # and a QStackedWidget takes its tallest page — so MadiRef alone was
        # holding the whole window's minimum at 900 px tall, which does not fit
        # on a 1080p screen once the taskbar and title bar are counted
        # (Marty, 2026-08-15: "we need to be able to scale the window a lot").
        panel_body = QWidget()
        panel_body.setStyleSheet("QWidget{border:none;}")
        col = QVBoxLayout(panel_body)
        col.setContentsMargins(10, 10, 10, 10)
        col.setSpacing(6)

        # ⚠ ONE button, two jobs — it says which one it is doing. With a clip
        # loaded there was no way to put it down at all (Marty, 2026-08-12).
        self.btn_open = QPushButton("Open clip…")
        self.btn_open.clicked.connect(self._on_open_or_close)
        col.addWidget(self.btn_open)

        # ⚠ Directly under the button that fills it and the button that empties
        # it (Marty, 2026-08-12). It used to sit in a "Prepared clips" section
        # of its own near the bottom, beside a Clear button — and **Close clip
        # took that button's job**, so the section became a label with nothing
        # to do next to it.
        self.lbl_cache = QLabel("—")
        self.lbl_cache.setStyleSheet("color:%s;" % theme.TEXT_DIM)
        self.lbl_cache.setToolTip(
            "Prepared clips are kept on disk so reopening one is instant. "
            "Closing a clip clears them all; they are also trimmed "
            "oldest-first to stay under the budget in ⚙ Library Settings.")
        col.addWidget(self.lbl_cache)

        col.addWidget(_section("Timing"))
        self.chk_follow = QCheckBox("Follow Blender's timeline")
        self.chk_follow.setChecked(True)
        self.chk_follow.setToolTip(
            "The reference frame is chosen by TIME, so a clip at a different "
            "frame rate to the scene still plays at the right speed.")
        self.chk_follow.toggled.connect(self._on_follow)
        col.addWidget(self.chk_follow)

        self.chk_framedrop = QCheckBox("Hold real time (frame drop)")
        self.chk_framedrop.setChecked(True)
        self.chk_framedrop.setToolTip(
            "Puts Blender in FRAME_DROP sync so a slow viewport drops frames "
            "instead of playing everything slowly. This is what keeps the "
            "reference at true speed while staying frame-matched.\n"
            "Your previous sync setting is restored when the reference closes.")
        self.chk_framedrop.toggled.connect(self._push_config)
        col.addWidget(self.chk_framedrop)

        col.addLayout(self._spin_row(
            "Offset (scene frames)", "offset",
            QSpinBox(), -100000, 100000, 0, self._on_offset))
        speed = QDoubleSpinBox()
        speed.setDecimals(2)
        speed.setSingleStep(0.05)
        col.addLayout(self._spin_row("Speed", "speed", speed,
                                     0.05, 20.0, 1.0, self._on_speed))

        col.addWidget(_section("In the viewport"))
        # The three placements Marty asked for, in his order.
        self.cmb_mode = QComboBox()
        self.cmb_mode.addItem("1 · Follows the viewport", "viewport")
        self.cmb_mode.addItem("2 · Pinned where you put it", "pinned")
        self.cmb_mode.addItem("3 · Pinned to the camera", "camera")
        self.cmb_mode.setToolTip(
            "1 — a screen-space overlay that stays put as you navigate.\n"
            "2 — pins it into the scene exactly where it is now, so it stays "
            "there and you can orbit around it.\n"
            "3 — rides the scene camera: only the camera moves it, and the "
            "Depth slider sets how far in front of the camera it sits.")
        self.cmb_mode.currentIndexChanged.connect(self._on_mode)
        col.addWidget(self.cmb_mode)

        self.chk_locked = QCheckBox("Lock (no dragging in the viewport)")
        self.chk_locked.setToolTip(
            "Stops the reference reacting to the mouse in the viewport, and "
            "hides its handles. Everything on this panel still works.")
        self.chk_locked.toggled.connect(self._on_locked)
        col.addWidget(self.chk_locked)

        self.btn_show = QPushButton("Show in Blender")
        self.btn_show.setCheckable(True)
        self.btn_show.clicked.connect(self._on_show)
        col.addWidget(self.btn_show)

        col.addLayout(self._slider_row("Size", "scale", 4, 300, 32,
                                       self._on_view_changed))
        col.addLayout(self._slider_row("Rotate", "rot", -180, 180, 0,
                                       self._on_view_changed))
        col.addLayout(self._slider_row("Opacity", "opacity", 5, 100, 100,
                                       self._on_view_changed))
        col.addLayout(self._slider_row("X", "posx", 0, 100, 18,
                                       self._on_view_changed))
        col.addLayout(self._slider_row("Y", "posy", 0, 100, 20,
                                       self._on_view_changed))
        self.btn_reset_view = QPushButton("Reset placement")
        self.btn_reset_view.clicked.connect(self._on_reset_view)
        col.addWidget(self.btn_reset_view)

        # ------------------------------------------------ Notes (2026-08-12)
        col.addWidget(_section("Notes"))
        nhint = QLabel("Draw on the picture. What you draw belongs to that "
                       "one frame and shows only there.")
        nhint.setWordWrap(True)
        nhint.setStyleSheet("color:%s;" % theme.TEXT_DIM)
        col.addWidget(nhint)

        trow = QHBoxLayout()
        trow.setSpacing(4)
        self.btn_pen = QPushButton("Draw")
        self.btn_pen.setCheckable(True)
        self.btn_pen.setToolTip("Draw on the frame you are parked on. "
                                "Turn it off to scrub without leaving marks.")
        self.btn_pen.toggled.connect(self._on_pen_toggled)
        trow.addWidget(self.btn_pen, 1)
        self.btn_undo = QPushButton("Undo")
        self.btn_undo.setToolTip("Remove the last stroke drawn on this frame.")
        self.btn_undo.clicked.connect(self._on_note_undo)
        trow.addWidget(self.btn_undo)
        self.btn_clear = QPushButton("Clear")
        self.btn_clear.setToolTip("Remove everything drawn on this frame.")
        self.btn_clear.clicked.connect(self._on_note_clear)
        trow.addWidget(self.btn_clear)
        col.addLayout(trow)

        colrow = QHBoxLayout()
        colrow.setSpacing(4)
        clbl = QLabel("Colour")
        clbl.setStyleSheet("color:%s;" % theme.TEXT)
        colrow.addWidget(clbl)
        colrow.addStretch(1)
        self._color_buttons = []
        for hexcol in _notes.COLORS:
            swatch = QPushButton()
            swatch.setCheckable(True)
            swatch.setFixedSize(20, 20)
            swatch.setToolTip(hexcol)
            swatch.setProperty("madi_color", hexcol)
            # ⚠ Styled per button, not through the sheet: the app's QSS paints
            # every QPushButton the panel colour, so an unstyled swatch is six
            # identical grey squares.
            swatch.setStyleSheet(
                "QPushButton{background:%s;border:1px solid %s;"
                "border-radius:10px;}"
                "QPushButton:checked{border:2px solid %s;}"
                % (hexcol, theme.BORDER, theme.TEXT))
            swatch.clicked.connect(
                lambda _c=False, h=hexcol: self._on_pen_color(h))
            colrow.addWidget(swatch)
            self._color_buttons.append(swatch)
        col.addLayout(colrow)

        col.addLayout(self._slider_row(
            "Width", "notewidth",
            int(_notes.MIN_WIDTH * 1000), int(_notes.MAX_WIDTH * 1000),
            int(_notes.DEFAULT_WIDTH * 1000), self._on_pen_width))

        self.chk_notes_blender = QCheckBox("Show markings in Blender")
        self.chk_notes_blender.setChecked(True)
        self.chk_notes_blender.setToolTip(
            "Paints the notes into the reference the viewport is showing. "
            "Turn it off for a clean reference while you animate — you can "
            "still see and edit them here.")
        self.chk_notes_blender.toggled.connect(self._on_notes_in_blender)
        col.addWidget(self.chk_notes_blender)

        jrow = QHBoxLayout()
        jrow.setSpacing(4)
        self.btn_prev_note = QPushButton("‹ Prev")
        self.btn_prev_note.clicked.connect(lambda: self._jump_note(-1))
        jrow.addWidget(self.btn_prev_note)
        self.btn_next_note = QPushButton("Next ›")
        self.btn_next_note.clicked.connect(lambda: self._jump_note(1))
        jrow.addWidget(self.btn_next_note)
        # ⚠ NO "End here" BUTTON. It existed only to terminate a note that ran
        # "until the next one"; a note that shows on its own frame has nothing
        # to close (Marty, 2026-08-12).
        col.addLayout(jrow)

        self.lbl_notes = QLabel("")
        self.lbl_notes.setWordWrap(True)
        self.lbl_notes.setStyleSheet("color:%s;" % theme.TEXT_DIM)
        col.addWidget(self.lbl_notes)

        col.addWidget(_section("Keep in front"))
        ohint = QLabel("Sets how far away the reference sits. Anything nearer "
                       "than that covers it — fully shaded, using Blender's "
                       "own depth, at no cost.")
        ohint.setWordWrap(True)
        ohint.setStyleSheet("color:%s;" % theme.TEXT_DIM)
        col.addWidget(ohint)

        # ⚠ ONE control, not a checkbox plus a slider. The slider alone did
        # nothing until a separate tickbox was on, which is not what "give me a
        # slider to set the depth" means — the slider IS the switch now.
        drow = QHBoxLayout()
        dlbl = QLabel("Depth")
        dlbl.setStyleSheet("color:%s;" % theme.TEXT)
        dlbl.setFixedWidth(56)
        drow.addWidget(dlbl)
        self.sld_depth = QSlider(Qt.Horizontal)
        self.sld_depth.setRange(0, 1000)          # tenths of a metre
        self.sld_depth.setValue(0)
        self.sld_depth.setToolTip(
            "How far in front of you the reference sits, in metres.\n"
            "Anything NEARER than this covers it; anything further is behind "
            "it.\n\nAt 0 the reference is always on top, as if the scene were "
            "not there.")
        self.sld_depth.valueChanged.connect(self._on_occlude_depth)
        drow.addWidget(self.sld_depth, 1)
        self.lbl_depth = QLabel("off")
        self.lbl_depth.setFixedWidth(46)
        self.lbl_depth.setStyleSheet("color:%s;" % theme.TEXT_DIM)
        drow.addWidget(self.lbl_depth)
        col.addLayout(drow)


        col.addWidget(_section("Audio"))
        self.chk_audio = QCheckBox("Play reference audio")
        self.chk_audio.setToolTip(
            "Plays while the timeline is running. Scrubbing stays silent — "
            "a seek per frame stutters and tells you nothing.")
        self.chk_audio.toggled.connect(self._on_audio_toggle)
        col.addWidget(self.chk_audio)
        col.addLayout(self._slider_row("Volume", "volume", 0, 100, 80,
                                       self._on_volume))

        # ⚠ NO "Prepared clips" SECTION AND NO Clear BUTTON. Close clip does
        # exactly what Clear did, so keeping it would have been two controls
        # for one action; the size moved up beside the Open/Close button that
        # governs it (Marty, 2026-08-12).
        col.addStretch(1)
        self.lbl_note = QLabel("")
        self.lbl_note.setWordWrap(True)
        self.lbl_note.setStyleSheet("color:%s;" % theme.TEXT_DIM)
        col.addWidget(self.lbl_note)
        panel_scroll = QScrollArea()
        panel_scroll.setWidgetResizable(True)
        panel_scroll.setFrameShape(QFrame.NoFrame)
        # The panel is a fixed 268 px, so a horizontal bar would only ever be
        # noise; the vertical one appears when the window is too short.
        panel_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        panel_scroll.setWidget(panel_body)
        panel_outer = QVBoxLayout(panel)
        panel_outer.setContentsMargins(0, 0, 0, 0)
        panel_outer.addWidget(panel_scroll)
        root.addWidget(panel)

        # Drives the app's own frame counter and the audio clock. It does NOT
        # drive the picture -- that arrives on the decoder's frame_ready signal.
        self._tick = QTimer(self)
        self._tick.setInterval(100)
        self._tick.timeout.connect(self._on_tick)

        self._on_pen_color(_notes.DEFAULT_COLOR)
        self._refresh_enabled()
        self._refresh_cache_label()
        self._refresh_notes_label()

    # ------------------------------------------------------------ helpers

    def _spin_row(self, label, name, spin, lo, hi, value, slot):
        row = QHBoxLayout()
        lbl = QLabel(label)
        lbl.setStyleSheet("color:%s;" % theme.TEXT)
        row.addWidget(lbl, 1)
        spin.setRange(lo, hi)
        spin.setValue(value)
        spin.valueChanged.connect(slot)
        setattr(self, "spn_" + name, spin)
        row.addWidget(spin)
        return row

    def _slider_row(self, label, name, lo, hi, value, slot):
        row = QHBoxLayout()
        lbl = QLabel(label)
        lbl.setStyleSheet("color:%s;" % theme.TEXT)
        lbl.setFixedWidth(56)
        row.addWidget(lbl)
        s = QSlider(Qt.Horizontal)
        s.setRange(lo, hi)
        s.setValue(value)
        s.valueChanged.connect(slot)
        setattr(self, "sld_" + name, s)
        row.addWidget(s, 1)
        return row

    def _refresh_cache_label(self):
        """How much disk the prepared clips are using, under the button that
        makes them and the button that clears them.

        ⚠ There is no Clear button any more — **Close clip does exactly what it
        did**, so the two were one action with two controls. The wording says
        so, because otherwise a number with no visible way to act on it reads
        as a dead end.
        """
        n = len(_ingest.cache_entries())
        gb = _ingest.cache_size() / (1024.0 ** 3)
        if not n:
            self.lbl_cache.setText("No prepared clips on disk")
            return
        self.lbl_cache.setText(
            "%d prepared clip%s · %.2f GB%s"
            % (n, "" if n == 1 else "s", gb,
               " · cleared when you close the clip" if n else ""))

    def _note(self, text):
        self.lbl_note.setText(text or "")
        if text:
            self.status_message.emit(text)

    def _refresh_enabled(self):
        loaded = self.player is not None
        # The one button says which of its two jobs it will do.
        busy = self._job is not None
        self.btn_open.setText("Close clip" if (loaded or busy)
                              else "Open clip…")
        self.btn_open.setToolTip(
            "Close the clip and clear every prepared clip from disk. Your "
            "video files are never touched." if (loaded or busy) else "")
        for w in (self.chk_follow, self.chk_audio, self.cmb_mode,
                  self.spn_offset, self.spn_speed):
            w.setEnabled(loaded)
        self.sld_depth.setEnabled(self._shown_in_blender
                                  and not self._capture_busy)
        for w in (self.cmb_mode, self.chk_locked):
            w.setEnabled(self._shown_in_blender and not self._capture_busy)
        reason = self._viewport_reason()
        self.btn_show.setEnabled(loaded and not reason and not self._capture_busy)
        self.btn_show.setToolTip(reason or "")
        self.scrub.setEnabled(loaded and not self.chk_follow.isChecked())

    def _viewport_reason(self):
        """Why 'Show in Blender' cannot work right now, or ''.

        Uses the same capability contract as every other bridge-dependent
        control, so an older add-on greys ONE button with an explanation
        instead of the tab misbehaving.
        """
        try:
            return self.bridge.feature_reason("madiref_viewport") or ""
        except Exception:                            # noqa: BLE001
            return ""

    # -------------------------------------------------------------- clips

    # ------------------------------------------- remembering the last clip

    def _cfg(self):
        """The app's config dict, or None (a standalone tab in the suites)."""
        cfg = getattr(self._window, "cfg", None)
        return cfg if isinstance(cfg, dict) else None

    def _remember_clip(self, path):
        """⚠ Store the SOURCE path, never the proxy. The proxy is a cache
        entry keyed by (path, mtime, size) and may legitimately be gone or
        rebuilt; the source is what the user chose, and it is also what the
        notes are keyed by."""
        cfg = self._cfg()
        if cfg is None:
            return
        if path:
            cfg["madiref_last_clip"] = path
        else:
            cfg.pop("madiref_last_clip", None)
        try:
            config.save(cfg)
        except Exception:                            # noqa: BLE001
            pass

    def restore_last_clip(self):
        """Reopen whatever was loaded when the app last closed.

        Marty, 2026-08-12: *"when closing the app and re-opening we need to
        have our old video loaded along with markings"*. The notes come with
        it for free — they are keyed by the source path, so loading the clip
        finds them.

        ⚠ **ONLY FROM THE CACHE.** If the prepared clip is gone this does
        NOTHING rather than re-ingesting: ingest is minutes of work on a long
        reference, and a startup that silently begins one is a startup that
        looks hung. ⚠ It is also why pressing **Close clip** forgets the path —
        that press clears the cache, so restoring it would mean re-ingesting
        exactly what was just thrown away.
        """
        cfg = self._cfg()
        path = (cfg or {}).get("madiref_last_clip")
        if not path or self.player is not None:
            return False
        if not os.path.isfile(path) or not _ingest.is_ingested(path):
            return False
        self.open_clip(path)
        return True

    # -------------------------------------------------------------- clips

    def _on_open_or_close(self):
        if self.player is not None or self._job is not None:
            self.close_clip(clear_cache=True)
        else:
            self.open_clip()

    def open_clip(self, path=None):
        if not path:
            path, _ = QFileDialog.getOpenFileName(
                self, "Open reference clip", "", VIDEO_FILTER)
        if not path:
            return
        self.close_clip()
        self._source = path
        self._remember_clip(path)
        self.lbl_clip.setText(os.path.basename(path))
        cached = _ingest.proxy_path(path)
        if _ingest.is_ingested(path):
            self._start_player(cached)
            return
        self.progress.setVisible(True)
        self.progress.setRange(0, 0)
        self.progress.setFormat("Preparing %p%")
        self._note("Preparing the clip — this happens once per file.")
        # ⚠ The button stays LIVE and becomes "Close clip": disabling it here
        # meant a long ingest could not be called off at all.
        self._refresh_enabled()
        self._job = _ingest.IngestJob(path, self)
        self._job.progress.connect(self._on_ingest_progress)
        self._job.finished.connect(self._on_ingest_done)
        self._job.failed.connect(self._on_ingest_failed)
        QTimer.singleShot(0, self._job.start)

    def _on_ingest_progress(self, done, total, _note):
        if total > 0:
            self.progress.setRange(0, total)
            self.progress.setValue(done)
        else:
            self.progress.setRange(0, 0)

    def _on_ingest_done(self, proxy_path):
        self.progress.setVisible(False)
        self.btn_open.setEnabled(True)
        self._job = None
        _ingest.purge_stale()
        self._refresh_cache_label()
        # ⚠ THE USER MAY HAVE PRESSED "Close clip" WHILE THIS RAN. The worker
        # cannot be called back mid-flight, so it still lands here — and
        # starting the player now would reopen the very clip that was just
        # closed, after its cache had been cleared. `_source` is the flag:
        # close_clip() sets it None.
        if self._source is None:
            self._refresh_enabled()
            return
        self._start_player(proxy_path)

    def _on_ingest_failed(self, msg):
        self.progress.setVisible(False)
        self.btn_open.setEnabled(True)
        self._job = None
        self._refresh_enabled()
        self._note("Could not read this clip: %s" % msg)

    def _start_player(self, proxy_path):
        player = _decoder.ReferencePlayer(proxy_path, self)
        if not player.start():
            self._note("The prepared clip could not be opened — try again to "
                       "rebuild it.")
            return
        self.player = player
        player.frame_ready.connect(self._on_frame)
        self.scrub.setRange(0, max(player.frame_count - 1, 0))
        self.audio.set_source(self._source)
        # ⚠ The book is keyed on the SOURCE clip, not on the proxy: re-encode a
        # clip and you want a fresh proxy but the same notes.
        self.book = _notes.NoteBook(self._source)
        self.view.set_book(self.book)
        self.scrub.set_book(self.book)
        player.set_notes(self.book, self.chk_notes_blender.isChecked())
        self._push_view()
        self._apply_settings_to_player()
        self._tick.start()
        self._refresh_enabled()
        self._refresh_notes_label()
        self._note("%d frames at %.2f fps." % (player.frame_count,
                                               player.video_fps))

    def close_clip(self, clear_cache=False):
        """Put the clip down.

        `clear_cache` is the **Close clip** button (Marty, 2026-08-12: closing
        a clip "will clear cache automatically"). ⚠ It clears **every** prepared
        clip, the same as the Clear button under *Prepared clips* — that is what
        "the cache" means in this tab. Originals are never touched and the only
        cost is preparing them again. It is NOT done on the app's own teardown,
        which must be free.
        """
        self._tick.stop()
        self.audio.stop()
        if self._shown_in_blender:
            self._hide_in_blender()
        if self.player is not None:
            self.player.stop()
            # ⚠ `self.player = None` IS NOT ENOUGH — measured. The player is a
            # QObject PARENTED TO THIS TAB, so Qt keeps it alive whatever
            # Python does with the name, and every open/close cycle left
            # another dead one on the tab's child list for the life of the
            # session. `stop()` frees the real resources (thread, ring, mmap);
            # this frees the husk. `setParent(None)` first so it leaves the
            # child list now rather than at the next event-loop turn.
            self.player.setParent(None)
            self.player.deleteLater()
            self.player = None
        if self._job is not None:
            self._job = None
            self.progress.setVisible(False)
        # Notes are saved on every edit, so there is nothing to flush here —
        # just stop pointing at a book whose clip has gone.
        self.book = None
        self.view.set_book(None)
        self.scrub.set_book(None)
        self.btn_pen.setChecked(False)
        self.view.clear()
        self._source = None
        self.lbl_clip.setText("No clip")
        self.lbl_frame.setText("—")
        if clear_cache:
            # ⚠ close_clip() FIRST (above): an open proxy is mmapped and
            # Windows refuses to delete it, which is the same trap the Clear
            # button documents.
            removed, freed = _ingest.clear_cache()
            self._remember_clip(None)     # nothing left to restore next launch
            self._note("Clip closed · cleared %d prepared clip%s (%.2f GB). "
                       "Your video files are untouched."
                       % (removed, "" if removed == 1 else "s",
                          freed / (1024.0 ** 3)))
            self._refresh_cache_label()
        self._refresh_enabled()
        self._refresh_notes_label()

    # ------------------------------------------------------------ signals

    def _on_frame(self, index, image):
        self.view.set_image(image)
        self.view.set_frame(index)
        self._frame = index
        fps = self.player.video_fps if self.player else 0.0
        self.lbl_frame.setText("frame %d / %d" % (index, self.frame_count()))
        if fps > 0:
            self.audio.sync(index / fps)
        if self.chk_follow.isChecked():
            self.scrub.blockSignals(True)
            self.scrub.setValue(index)
            self.scrub.blockSignals(False)

    # -------------------------------------------------- notes (2026-08-12)

    def _on_pen_toggled(self, on):
        self.view.set_pen(on=on)
        # ⚠ The button SAYS what pressing it will do. It is a toggle with no
        # other indicator, so "Draw" while already drawing leaves you looking
        # for the way out (Marty, 2026-08-12).
        self.btn_pen.setText("Stop drawing" if on else "Draw")
        # ⚠ FOLLOW BLENDER IS NOT TOUCHED. Turning it off here "so the frame
        # holds still while you draw" broke timeline sync for the rest of the
        # session — nothing turned it back on, and nobody associates a Draw
        # button with the reference silently unfollowing the timeline. The
        # frame a stroke belongs to is captured at PRESS instead, which solves
        # the same problem without taking a setting away.

    def _on_pen_color(self, hexcol):
        self.view.set_pen(color=hexcol)
        for btn in self._color_buttons:
            btn.setChecked(btn.property("madi_color") == hexcol)

    def _on_pen_width(self, thousandths):
        self.view.set_pen(width=thousandths / 1000.0)

    def _on_notes_in_blender(self, on):
        if self.player is not None:
            self.player.set_notes_in_blender(on)

    def _on_stroke_drawn(self, stroke, frame):
        if self.book is None:
            return
        # ⚠ `frame` is where the stroke STARTED, not where we are now. With
        # Follow Blender on the timeline can move while you draw, and the
        # drawing belongs to the frame you were looking at when you began.
        self.book.add_stroke(frame, stroke)
        self._after_note_edit()

    def _on_note_undo(self):
        if self.book is not None and not self.book.undo(self._frame):
            self._note("Nothing drawn on this frame.")
        self._after_note_edit()

    def _on_note_clear(self):
        if self.book is not None and not self.book.clear(self._frame):
            self._note("Nothing drawn on this frame.")
        self._after_note_edit()

    def _after_note_edit(self):
        self.view.update()
        self.scrub.update()
        self._refresh_notes_label()

    def _jump_note(self, direction):
        if self.book is None:
            return
        target = (self.book.next_note(self._frame) if direction > 0
                  else self.book.previous_note(self._frame))
        if target is None:
            self._note("No %s note." % ("later" if direction > 0 else "earlier"))
            return
        # ⚠ Jumping means choosing the frame ourselves, which Follow Blender
        # cannot allow — but it SAYS so rather than going quiet, because a
        # setting that switches itself off unannounced reads as a broken sync.
        if self.chk_follow.isChecked():
            self.chk_follow.setChecked(False)
            self._note("Frame %d. Follow Blender's timeline turned off so the "
                       "jump could hold — tick it again to re-sync." % target)
        else:
            self._note("Frame %d." % target)
        self.scrub.setValue(target)

    def _refresh_notes_label(self):
        book = self.book
        for widget in (self.btn_pen, self.btn_undo, self.btn_clear,
                       self.btn_prev_note, self.btn_next_note,
                       self.chk_notes_blender):
            widget.setEnabled(book is not None)
        if book is None:
            self.lbl_notes.setText("")
            return
        n = book.count()
        here = len(book.strokes_at(self._frame))
        state = ("%d stroke%s on this frame" % (here, "" if here == 1 else "s")
                 if here else "nothing on this frame")
        self.lbl_notes.setText("%d note%s · %s"
                               % (n, "" if n == 1 else "s", state))

    def frame_count(self):
        return self.player.frame_count - 1 if self.player else 0

    def _on_tick(self):
        # ⚠ Must run even with no clip loaded: this is what stops the audio
        # when the timeline pauses, and a pause delivers no frames at all.
        self.audio.check_idle()
        if self.player is None:
            return
        st = self.player.blender_state()
        if st and self.chk_follow.isChecked() and st["stamp"]:
            self.lbl_clip.setToolTip("Blender is at scene frame %d (%.3g fps)"
                                     % (st["scene_frame"], st["scene_fps"]))
        # The overlay can be dragged/scaled/rotated in the viewport, so these
        # controls follow it rather than being the only way to set it.
        if self._shown_in_blender:
            self._mirror_view_from_ring()

    def _on_follow(self, on):
        if self.player:
            self.player.set_follow_blender(on)
        self.scrub.setEnabled(self.player is not None and not on)

    def _on_scrub(self, value):
        if self.player and not self.chk_follow.isChecked():
            self.player.set_manual_frame(value)

    def _on_offset(self, value):
        if self.player:
            self.player.set_offset(value)

    def _on_speed(self, value):
        if self.player:
            self.player.set_speed(value)

    def _on_mode(self, _index):
        """Switching placement is a COMMAND, not a ring write.

        ⚠ `pinned` has to capture where the reference is at that moment, which
        only Blender can work out (it needs the live view matrix). So the mode
        goes through `madiref_pin` and the add-on writes the ring itself —
        pushing the mode from here would set the number without the pin.
        """
        mode = self.cmb_mode.currentData() or "viewport"
        if not self._shown_in_blender:
            return
        try:
            res = self.bridge.request("madiref_pin", {"mode": mode})
        except Exception as exc:                     # noqa: BLE001
            self._note("Blender did not answer: %s" % exc)
            return
        if not (res or {}).get("ok"):
            self._note((res or {}).get("error", "Could not change placement."))
            return
        self._note({
            "viewport": "Following the viewport.",
            "pinned": "Pinned into the scene where it is now.",
            "camera": "Pinned to the camera — only the camera moves it.",
        }[mode])

    def _on_locked(self, on):
        if not self._shown_in_blender:
            return
        try:
            res = self.bridge.request("madiref_config", {"locked": bool(on)})
        except Exception as exc:                     # noqa: BLE001
            self._note("Blender did not answer: %s" % exc)
            return
        if (res or {}).get("locked") is not bool(on):
            self._note("Blender ignored the lock — update the add-on from "
                       "⚙ Library Settings.")

    def _on_view_changed(self, *_a):
        self._push_view()

    def _push_view(self):
        if self.player is None or self._mirroring:
            return
        self.player.set_view(
            opacity=self.sld_opacity.value() / 100.0,
            x=self.sld_posx.value() / 100.0,
            y=self.sld_posy.value() / 100.0,
            scale=max(self.sld_scale.value() / 100.0, 0.04),
            rotation=math.radians(self.sld_rot.value()),
            visible=True)

    def _mirror_view_from_ring(self):
        """Follow what the viewport is doing.

        The overlay is dragged, scaled and rotated in Blender, and those edits
        land in the ring — so these controls have to MIRROR it, not fight it.
        `_mirroring` stops the resulting setValue() calls being pushed straight
        back and stomping on a drag in progress.
        """
        if self.player is None:
            return
        ring = self.player.view_state()
        if not ring:
            return
        want = {
            "scale": int(round(ring["scale"] * 100)),
            "rot": int(round(math.degrees(ring["rotation"]))),
            "opacity": int(round(ring["opacity"] * 100)),
            "posx": int(round(ring["x"] * 100)),
            "posy": int(round(ring["y"] * 100)),
        }
        self._mirroring = True
        try:
            for name, value in want.items():
                s = getattr(self, "sld_" + name)
                # ⚠ Only touch a slider that actually disagrees. Writing the
                # same value still emits valueChanged on some styles, and the
                # round trip would jitter a live drag.
                if s.value() != value and not s.isSliderDown():
                    s.blockSignals(True)
                    s.setValue(value)
                    s.blockSignals(False)
        finally:
            self._mirroring = False

    def _on_occlude_depth(self, value):
        """Depth-based occlusion: the cheap, whole-scene answer.

        Costs nothing per frame — no geometry extraction, no offscreen, no
        mask. The scene's depth buffer is already there and already correct.
        The slider is the whole control: 0 turns it off.
        """
        metres = value / 10.0
        self.lbl_depth.setText("off" if metres <= 0 else "%.1f m" % metres)
        if not self._shown_in_blender:
            return
        try:
            res = self.bridge.request("madiref_config", {
                "occlude": metres > 0,
                "occlude_distance": metres})
        except Exception as exc:                     # noqa: BLE001
            self._note("Blender did not answer: %s" % exc)
            return
        # ⚠ Trust the ECHO, not the request. This command grows parameters, and
        # a dispatcher that forgets to forward one fails completely silently —
        # which is exactly how the depth slider shipped doing nothing.
        got = (res or {}).get("occlude")
        if metres > 0 and got is not True:
            self._note("Blender ignored the depth setting — the add-on is "
                       "older than this feature. Update it from ⚙ Library "
                       "Settings.")

    def _on_reset_view(self):
        if self.player is None:
            return
        self.player.set_view(x=0.18, y=0.20, scale=0.32, rotation=0.0)
        self._mirror_view_from_ring()

    def _apply_settings_to_player(self):
        self.player.set_offset(self.spn_offset.value())
        self.player.set_speed(self.spn_speed.value())
        self.player.set_follow_blender(self.chk_follow.isChecked())

    # ------------------------------------------------------------ Blender

    def _on_show(self, checked):
        if checked:
            self._show_in_blender()
        else:
            self._hide_in_blender()

    def _show_in_blender(self):
        if self.player is None:
            self.btn_show.setChecked(False)
            return
        reason = self._viewport_reason()
        if reason:
            self.btn_show.setChecked(False)
            self._note(reason)
            return
        try:
            res = self.bridge.request("madiref_open", {
                "name": self.player.ring_name,
                "sync_framedrop": self.chk_framedrop.isChecked(),
            })
        except Exception as exc:                     # noqa: BLE001
            self.btn_show.setChecked(False)
            self._note("Blender did not answer: %s" % exc)
            return
        if not (res or {}).get("ok"):
            self.btn_show.setChecked(False)
            self._note((res or {}).get("error", "Blender refused the reference."))
            return
        self._shown_in_blender = True
        self.btn_show.setText("Hide from Blender")
        self._push_view()
        self._refresh_enabled()
        self._note("Showing in the 3D viewport.")

    def _hide_in_blender(self):
        self._shown_in_blender = False
        self.btn_show.setText("Show in Blender")
        self.btn_show.setChecked(False)
        self._refresh_enabled()
        try:
            self.bridge.request("madiref_close", {})
        except Exception:                            # noqa: BLE001
            pass

    def _push_config(self, *_a):
        if not self._shown_in_blender:
            return
        try:
            self.bridge.request("madiref_config", {
                "sync_framedrop": self.chk_framedrop.isChecked()})
        except Exception:                            # noqa: BLE001
            pass

    # -------------------------------------------------------------- audio

    def _on_audio_toggle(self, on):
        self.audio.set_enabled(on)
        if on and self._source:
            self._note("Audio follows playback; scrubbing stays silent.")

    def _on_volume(self, value):
        self.audio.set_volume(value / 100.0)

    def _on_audio_unavailable(self, why):
        self.chk_audio.blockSignals(True)
        self.chk_audio.setChecked(False)
        self.chk_audio.blockSignals(False)
        self._note("No audio for this clip (%s)." % why)

    # ------------------------------------------------------------ busy state

    def set_capture_busy(self, busy):
        """Required of every page in `MainWindow._pages()`.

        Only the controls that TALK TO BLENDER are disabled: a capture is
        Blender-side, and the reference itself keeps playing here because
        nothing about it needs the bridge.
        """
        self._capture_busy = bool(busy)
        self.btn_show.setEnabled(
            not busy and self.player is not None and not self._viewport_reason())
        self.chk_framedrop.setEnabled(not busy)

    # ------------------------------------------------------------- teardown

    def shutdown(self):
        """Called when the app closes. The ring must be unlinked or the
        segment outlives the process.

        ⚠ **AND BLENDER IS TOLD TO CLOSE THE REFERENCE UNCONDITIONALLY**, not
        only when this tab thinks it is showing one (Marty, 2026-08-12: *"when
        closing the app make sure to disable showing the video in viewport"*).
        `close_clip` alone checks `_shown_in_blender`, and that flag is a
        BELIEF held by ONE app process: if an instance dies, or a second one is
        started, the new app has the flag False while Blender is still drawing
        — and then nothing ever asks it to stop. The result is a reference
        painted over the viewport, reading from a segment that has gone, which
        no slider and no drag can move because the app driving it no longer
        exists.

        ⚠ `madiref_close` is idempotent, and it is EXEMPT from the licence gate
        for exactly this reason — it also restores the scene's `sync_mode`.
        ⚠ SHORT TIMEOUT: this runs on the GUI thread during teardown, and a
        dead localhost port DROPS the SYN on Windows rather than refusing it,
        so an unbounded call would hang the app on exit for every user who
        closed Blender first.
        """
        self.close_clip()
        try:
            self.bridge.request("madiref_close", {}, timeout=2.0)
        except Exception:                            # noqa: BLE001
            pass
