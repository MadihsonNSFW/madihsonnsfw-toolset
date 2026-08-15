"""Proxy -> ring. The worker that keeps whatever Blender is looking at ready.

This module owns **all** of the timing arithmetic. Blender publishes only its
raw scene frame and scene fps; everything about offsets, speed and the
video/scene frame-rate mismatch is resolved here, so changing any of it is an
app change and never an add-on push.

⚠ **Map by TIME, never by frame index.** A 60 fps reference on a 24 fps scene
must show the moment 1.0 s in when the timeline is at frame 24 — that is video
frame 60, not video frame 24. Mapping by index is the bug that makes a
reference play at the wrong speed, and it is the specific thing Marty asked
for: "no matter how much the fps difference is, the video reference timing
should be the same and not slow down".

The other sense of "slows down" is a viewport playing under its scene fps. That
is NOT fixed here — a second clock would drift from the animation and you could
no longer match a pose against it. It is fixed by putting Blender into
`FRAME_DROP` sync so the timeline itself holds real time; then frame-matched
and real-time are the same thing. See `docs\\madiref.md`.
"""

import secrets
import threading
import time

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QImage, QPainter

from . import notes as _notes
from . import proxy as _proxy
from . import shm as _shm

POLL_S = 0.004              # 250 Hz; scrub latency is this + ~1.6 ms decode
IDLE_POLL_S = 0.020         # nothing moving, stop spinning a core


def target_frame(scene_frame, scene_fps, video_fps, offset_frames=0.0,
                 speed=1.0, frame_count=None):
    """Which proxy frame belongs at this scene frame.

    Clamped, not wrapped: before the start you hold frame 0 and past the end
    you hold the last frame, which is what a reference should do rather than
    looping unexpectedly under someone's animation.
    """
    if scene_fps <= 0:
        scene_fps = 24.0
    seconds = (scene_frame - offset_frames) / float(scene_fps)
    idx = int(round(seconds * float(video_fps) * float(speed)))
    if idx < 0:
        idx = 0
    if frame_count is not None and frame_count > 0 and idx > frame_count - 1:
        idx = frame_count - 1
    return idx


def _packed_bits(img):
    """A tightly packed RGBA byte buffer for a QImage.

    ⚠⚠ **THE FAST PATH RETURNS A VIEW INTO `img`, NOT A COPY**, so the CALLER
    MUST KEEP `img` ALIVE until it has finished with the result. Passing a
    freshly built image straight in — `_packed_bits(make_image())` — frees it
    the moment this returns and whatever reads the buffer next gets an access
    violation. That crashed the app on every frame carrying a drawn note
    (2026-08-12); the fix is a named local at the call site.

    ⚠ Qt aligns each scanline, so `bytesPerLine()` is not always width*4. Handing
    a padded buffer to the ring shifts every row by a few bytes and the viewport
    shows a diagonally sheared picture — which reads as a corrupt decoder rather
    than as a stride bug. Fast path when there is no padding, row copy when
    there is.
    """
    w, h = img.width(), img.height()
    row = w * 4
    bits = img.constBits()
    if img.bytesPerLine() == row:
        return bits
    mv = memoryview(bits).cast("B")
    out = bytearray(row * h)
    stride = img.bytesPerLine()
    for y in range(h):
        src = y * stride
        out[y * row:(y + 1) * row] = mv[src:src + row]
    return out


def new_ring_name():
    """A fresh, unguessable segment name per session.

    ⚠ A named segment is openable by any local process that knows the name, so
    it must not be predictable and must not be reused across runs. The name is
    handed to Blender over the authenticated bridge and nowhere else
    (`docs\\security.md`).
    """
    return "madiref_%s" % secrets.token_hex(8)


class ReferencePlayer(QObject):
    """Serves the frame Blender is asking for, and shows the app the same one.

    The app view is fed from the SAME decoded QImage that goes into the ring —
    not a second read of the same source — so the two views cannot disagree
    even for a frame.
    """

    frame_ready = Signal(int, object)        # proxy frame index, QImage
    stopped = Signal()

    def __init__(self, proxy_path, parent=None, slots=_shm.DEFAULT_SLOTS):
        super().__init__(parent)
        self.proxy_path = proxy_path
        self._slots = slots
        self._reader = None
        self._ring = None
        self._thread = None
        self._run = False
        self._lock = threading.Lock()

        self.offset_frames = 0.0
        self.speed = 1.0
        self.follow_blender = True
        self._manual_frame = 0
        self._served = -1
        self._last_consumer_stamp = -1
        # Drawn notes (2026-08-12). None until the tab attaches the clip's
        # book; `_served_notes` is the revision the frame on screen was painted
        # from, which is what makes an EDIT re-serve a frame that has not moved.
        self._notes = None
        self._notes_to_blender = True
        self._served_notes = -1

    # ------------------------------------------------------------------

    @property
    def ring_name(self):
        return self._ring.name if self._ring else None

    @property
    def frame_count(self):
        return self._reader.frame_count if self._reader else 0

    @property
    def video_fps(self):
        return self._reader.fps if self._reader else 0.0

    @property
    def size(self):
        return self._reader.size if self._reader else (0, 0)

    def start(self):
        if self._run:
            return True
        self._reader = _proxy.open_proxy(self.proxy_path)
        if self._reader is None:
            return False
        w, h = self._reader.size
        if not w or not h:
            self._reader.close()
            self._reader = None
            return False
        fps = self._reader.header["fps_num"], self._reader.header["fps_den"]
        self._ring = _shm.RingWriter(new_ring_name(), w, h,
                                     self._reader.frame_count, fps[0], fps[1],
                                     slots=self._slots)
        self._run = True
        self._served = -1
        # ⚠ Publish the first frame BEFORE the worker exists. Doing it after
        # `start()` let this thread and the worker write a slot at the same
        # time — two writers, one seqlock, and the reader can legitimately see
        # a torn frame. It also means a viewport that draws before the first
        # poll finds a real picture instead of an empty slot.
        self._serve(self._manual_frame if not self.follow_blender else 0)
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="madiref-decoder")
        self._thread.start()
        return True

    def stop(self):
        self._run = False
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=2.0)
        self._thread = None
        if self._ring is not None:
            self._ring.unlink()
            self._ring = None
        if self._reader is not None:
            self._reader.close()
            self._reader = None
        self.stopped.emit()

    # ---------------------------------------------------------- settings

    def set_offset(self, frames):
        with self._lock:
            self.offset_frames = float(frames)
            self._served = -1

    def set_speed(self, speed):
        with self._lock:
            self.speed = max(0.01, float(speed))
            self._served = -1

    def set_follow_blender(self, follow):
        with self._lock:
            self.follow_blender = bool(follow)
            self._served = -1

    def set_manual_frame(self, index):
        with self._lock:
            self._manual_frame = int(index)
            self._served = -1

    def set_view(self, **kw):
        if self._ring is not None:
            self._ring.set_view_state(**kw)

    def view_state(self):
        """The live view block — which BLENDER also writes, when the overlay is
        dragged, scaled or rotated in the viewport."""
        return self._ring.view_state() if self._ring is not None else None

    def blender_state(self):
        return self._ring.consumer_state() if self._ring else None

    def blender_attached(self):
        """Has the viewport drawn recently? `alive` only moves when the add-on
        draws, so this is liveness rather than a claim made at connect time."""
        st = self.blender_state()
        return bool(st and st["stamp"] != self._last_consumer_stamp)

    # -------------------------------------------------------------- loop

    def _current_target(self):
        with self._lock:
            follow = self.follow_blender
            offset, speed = self.offset_frames, self.speed
            manual = self._manual_frame
        if not follow:
            return max(0, min(manual, max(self.frame_count - 1, 0)))
        st = self._ring.consumer_state()
        if not st["stamp"]:
            return 0                    # Blender has never drawn yet
        return target_frame(st["scene_frame"], st["scene_fps"], self.video_fps,
                            offset, speed, self.frame_count)

    def _serve(self, index):
        blob = self._reader.frame_bytes(index)
        if not blob:
            return False
        img = QImage()
        if not img.loadFromData(blob, "JPG"):
            return False
        # ⚠ RGBA8888 is the ring's contract. loadFromData gives RGB32 (BGRA),
        # and handing that over unconverted swaps red and blue in the viewport
        # — a bug that looks like a colour-management problem and is not one.
        if img.format() != QImage.Format_RGBA8888:
            img = img.convertToFormat(QImage.Format_RGBA8888)
        # ⚠ `out` MUST BE A NAMED LOCAL. `_packed_bits` hands back
        # `constBits()` — a pointer INTO the QImage, not a copy — so writing
        # `_packed_bits(self._for_blender(...))` in one expression let CPython
        # free the composited image the instant `_packed_bits` returned, and
        # `write_frame` then copied from freed memory. An ACCESS VIOLATION that
        # killed the app, and only ever on a frame carrying a note: with no
        # strokes `_for_blender` returns `img` itself, which this function
        # already holds. Keep the reference alive across the write.
        out = self._for_blender(img, index)
        try:
            self._ring.write_frame(index, _packed_bits(out))
        except (ValueError, TypeError):
            return False
        self._served = index
        self._served_notes = self._notes_revision()
        # ⚠ The RAW frame goes to the app, never the composited one. The tab
        # draws notes over its own preview so it can show the stroke being
        # dragged; handing it a copy that already had them painted in would
        # double every line, softly, along its antialiased edges.
        self.frame_ready.emit(index, img)
        return True

    # ------------------------------------------------- notes (2026-08-12)

    def set_notes(self, book, show_in_blender=True):
        """Attach the clip's NoteBook. Painting them into the frame is the
        whole viewport half of the feature: Blender uploads what it is given,
        so the markings arrive with rotation, scale, depth and all three
        placements already applied and NO add-on change at all."""
        with self._lock:
            self._notes = book
            self._notes_to_blender = bool(show_in_blender)
            self._served = -1           # repaint the frame already on screen

    def set_notes_in_blender(self, show):
        with self._lock:
            self._notes_to_blender = bool(show)
            self._served = -1

    def _notes_revision(self):
        book = self._notes
        return book.revision if book is not None else -1

    def notes_changed(self):
        """Has the book been edited since the frame on screen was written?

        ⚠ THIS IS WHY THE LAST STROKE APPEARS AT ALL. `_served` suppresses
        rewriting a frame that is already out, and an edit changes the PICTURE
        without changing the FRAME INDEX — so the loop would sit idle and the
        line you just drew would not reach Blender until you scrubbed away and
        back."""
        return self._notes_revision() != self._served_notes

    def _for_blender(self, img, index):
        """`img` with the notes in force at `index` painted on, or `img`.

        Costs nothing on a clip with no notes and nothing on the stretches
        between them — the copy only happens when there is something to draw.
        """
        with self._lock:
            book, show = self._notes, self._notes_to_blender
        if book is None or not show:
            return img
        strokes = book.strokes_at(index)
        if not strokes:
            return img
        # ⚠ A REAL COPY, not `QImage(img)`. QImage is implicitly shared and
        # `detach()` is not exposed in PySide6, so painting on a shallow copy
        # would scribble on the very frame `frame_ready` hands to the tab —
        # which draws its own notes on top, and would then show every line
        # twice.
        stamped = img.copy()
        painter = QPainter(stamped)
        try:
            _notes.paint_strokes(painter, strokes,
                                 stamped.width(), stamped.height())
        finally:
            painter.end()
        return stamped

    def _loop(self):
        while self._run:
            try:
                want = self._current_target()
                # ⚠ `notes_changed()` is the second reason to re-serve. Without
                # it an edit to the frame already on screen never reaches
                # Blender, because the index has not moved and the loop idles.
                if want != self._served or self.notes_changed():
                    self._serve(want)
                    time.sleep(POLL_S)
                else:
                    time.sleep(IDLE_POLL_S)
            except Exception:                        # noqa: BLE001
                # A decoder thread that dies takes the reference down silently;
                # keep going and let the next poll retry.
                time.sleep(IDLE_POLL_S)
