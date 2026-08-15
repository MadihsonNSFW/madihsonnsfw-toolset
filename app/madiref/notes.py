"""Drawn notes on a reference clip — the model, the ranges and the file.

Marty, 2026-08-12: *"add the ability to draw in specific frame with a marker …
this drawings can be saved and loaded if we open the same clip even after
restarting the app"*, and *"these markings need to be also visible in 3d
viewport too"*.

WHAT A NOTE IS
    A note belongs to ONE proxy frame and holds a list of strokes, and it shows
    on **that frame and no other**. Draw on another frame and you get another,
    independent note.

⚠ **IT USED TO LAST "UNTIL THE NEXT NOTE" and that was wrong in use** (Marty,
2026-08-12, after trying it: *"the drawn frames should automatically get
created whenever i paint on that ONE frame and show on ONE frame only"*). That
rule needed a terminator to be usable at all — an empty note, reached by an
"End here" button — and both are gone with it. **An empty note now carries no
meaning and is deleted rather than kept**, or it would leave a tick on the
scrubber pointing at nothing.

⚠ **STROKES ARE STORED AS VECTORS, NOT PIXELS**, even though they reach Blender
as pixels (`decoder._serve` paints them into the frame it was already writing).
Rasterising is a delivery choice; keeping the geometry is what makes an eraser,
an undo and a future "send the strokes to Blender" route all possible without a
migration.

⚠ **EVERYTHING IS NORMALISED to the frame** — points as 0..1 of width/height,
width as a fraction of HEIGHT. The proxy is 540p today and the source can be
re-ingested at another size; pixel coordinates would silently shift every note
on the clip if it ever were.

⚠ **THE FRAME INDEX IS THE PROXY'S, NEVER THE SCENE'S.** The scene mapping is a
view onto the clip and moves with Offset and Speed (`decoder.target_frame`), so
a note keyed by scene frame would slide the moment either was touched.

⚠ **THESE FILES DO NOT LIVE IN THE PROXY CACHE.** `_madiref_cache\` is trimmed
oldest-first against a gigabyte budget — it reached 3.9 GB on Marty's machine —
and a routine trim would take the drawings with the proxies. A proxy regenerates
in seconds; a drawing does not. They live in `_madiref_notes\`, which nothing
trims, and which is in `make_release.js`'s NEVER_SHIP_DIRS for the same reason
the cache is.
"""

import hashlib
import json
import os

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QPainter, QPen

import config

NOTES_ROOT = os.path.join(config.APP_DIR, "_madiref_notes")

FORMAT_VERSION = 1

# The pens offered in the panel. Red first: it is the one that reads on both a
# bright and a dark reference, which a note has to do without being chosen.
COLORS = ("#e8483a", "#f2c14e", "#4fc06a", "#4f8cff", "#ffffff", "#101010")
DEFAULT_COLOR = COLORS[0]

# As a fraction of frame height, so the slider means the same thing on any clip.
MIN_WIDTH = 0.002
MAX_WIDTH = 0.05
DEFAULT_WIDTH = 0.008


def key_for(source_path):
    """The notes filename for a clip.

    Keyed on the path rather than on the proxy's (path, mtime, size): re-encode
    a clip and you want a new PROXY but the same NOTES. Lower-cased because
    Windows paths are case-insensitive and the same file must not get two
    files depending on how it was typed.
    """
    raw = os.path.abspath(source_path).lower().encode("utf-8", "replace")
    return hashlib.sha1(raw).hexdigest()[:16]


class Stroke:
    """One drag of the pen."""

    __slots__ = ("points", "color", "width")

    def __init__(self, points=None, color=DEFAULT_COLOR, width=DEFAULT_WIDTH):
        self.points = list(points or [])
        self.color = color
        self.width = float(width)

    def to_json(self):
        # Rounded to 4 places: a 540p frame is ~0.0019 per pixel, so this is
        # finer than the picture and keeps the file small on long scribbles.
        return {"c": self.color, "w": round(self.width, 5),
                "p": [[round(x, 4), round(y, 4)] for x, y in self.points]}

    @classmethod
    def from_json(cls, blob):
        pts = []
        for pair in blob.get("p") or []:
            try:
                pts.append((float(pair[0]), float(pair[1])))
            except (TypeError, ValueError, IndexError):
                continue            # one bad point must not lose the stroke
        return cls(pts, str(blob.get("c") or DEFAULT_COLOR),
                   float(blob.get("w") or DEFAULT_WIDTH))


class NoteBook:
    """Every note on one clip.

    `revision` bumps on every change. `decoder` compares it and re-serves the
    current frame when it moves — ⚠ **without that the last stroke does not
    appear until you scrub away and back**, because `_served` suppresses
    rewriting a frame that is already out and an edit changes the picture
    without changing the frame index.

    ⚠ **TWO THREADS READ THIS: the GUI edits it, the decoder worker reads it.**
    There is no lock, and it works only because of how the writes are shaped —
    `_order` is REPLACED (`sorted(...)`), never mutated in place, and
    `strokes_at` hands back a COPY. A reader then gets a whole consistent list
    or the previous one, which is the same trade the shared-memory ring makes.
    ⚠ Turning `self._order = sorted(self._notes)` into an
    `append()` + `sort()` would quietly break that: the decoder can be
    iterating it at the time.
    """

    def __init__(self, source_path, folder=None):
        self.source = os.path.abspath(source_path) if source_path else ""
        self.folder = folder or NOTES_ROOT
        self._notes = {}            # frame -> [Stroke]
        self._order = []            # sorted frame indices, kept in step
        self.revision = 0
        self.load()

    # ------------------------------------------------------------- ranges

    @property
    def path(self):
        return os.path.join(self.folder, key_for(self.source) + ".json")

    def frames(self):
        """Every frame carrying a note, in order. Drives the scrubber ticks."""
        return list(self._order)

    def count(self):
        return len(self._order)

    def has_note(self, frame):
        return int(frame) in self._notes

    def strokes_at(self, frame):
        """What should be drawn on `frame` — its OWN note, or nothing.

        ⚠ A COPY, not the stored list. The decoder reads this from its worker
        thread while the GUI may be appending to it; handing out the live list
        would let a stroke arrive mid-paint.
        """
        return list(self._notes.get(int(frame), ()))

    def next_note(self, frame):
        """The first note strictly after `frame`, for the Next note button."""
        for f in self._order:
            if f > int(frame):
                return f
        return None

    def previous_note(self, frame):
        prev = None
        for f in self._order:
            if f >= int(frame):
                break
            prev = f
        return prev

    # ------------------------------------------------------------- edits

    def _touch(self, frame):
        frame = int(frame)
        if frame not in self._notes:
            self._notes[frame] = []
            self._order = sorted(self._notes)
        return self._notes[frame]

    def add_stroke(self, frame, stroke):
        """Add a stroke to `frame`'s own note, creating it if this is the first.

        The note appears by drawing — there is nothing to create first and
        nothing to close afterwards.
        """
        if not stroke.points:
            return False
        self._touch(frame).append(stroke)
        self._changed()
        return True

    def undo(self, frame):
        """Drop the last stroke drawn on `frame` — and the note with it if that
        was the only one, so no empty note is left holding a scrubber tick."""
        frame = int(frame)
        strokes = self._notes.get(frame)
        if not strokes:
            return False
        strokes.pop()
        if not strokes:
            self._drop(frame)
        self._changed()
        return True

    def clear(self, frame):
        """Remove everything drawn on `frame`."""
        if int(frame) not in self._notes:
            return False
        self._drop(int(frame))
        self._changed()
        return True

    def _drop(self, frame):
        del self._notes[frame]
        self._order = sorted(self._notes)

    def _changed(self):
        self.revision += 1
        self.save()

    # -------------------------------------------------------------- disk

    def load(self):
        self._notes, self._order = {}, []
        try:
            with open(self.path, encoding="utf-8") as fh:
                blob = json.load(fh)
        except (OSError, ValueError):
            return False                    # no notes yet, or an unreadable file
        for key, strokes in (blob.get("notes") or {}).items():
            try:
                frame = int(key)
            except (TypeError, ValueError):
                continue
            self._notes[frame] = [Stroke.from_json(s) for s in strokes
                                  if isinstance(s, dict)]
        self._order = sorted(self._notes)
        self.revision += 1
        return True

    def save(self):
        """Written on every edit. The files are a few KB, and a drawing lost to
        a crash is work lost — there is nothing to batch here."""
        blob = {
            "version": FORMAT_VERSION,
            # ⚠ Recorded so a MOVED clip can be reconnected by hand rather than
            # orphaned: the filename is a hash and says nothing about which
            # clip it belongs to.
            "source": self.source,
            "notes": {str(f): [s.to_json() for s in strokes]
                      for f, strokes in sorted(self._notes.items())},
        }
        try:
            os.makedirs(self.folder, exist_ok=True)
            tmp = self.path + ".part"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(blob, fh, indent=1)
            os.replace(tmp, self.path)
            return True
        except OSError:
            return False


def paint_strokes(painter, strokes, width_px, height_px):
    """Draw `strokes` onto an already-open QPainter covering width x height.

    The one place strokes become pixels — shared by the app's own preview and
    by the frame the decoder hands to Blender, so the two cannot disagree about
    what a note looks like.
    """
    if not strokes:
        return
    painter.setRenderHint(QPainter.Antialiasing, True)
    for stroke in strokes:
        if not stroke.points:
            continue
        pen = QPen(QColor(stroke.color))
        # Width is a fraction of HEIGHT, so a note keeps its weight when the
        # same clip is shown at preview size and at viewport size.
        pen.setWidthF(max(1.0, stroke.width * height_px))
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        painter.setPen(pen)
        pts = [QPointF(x * width_px, y * height_px) for x, y in stroke.points]
        if len(pts) == 1:
            # A tap is a dot, not nothing — otherwise a click that does not
            # drag looks like the pen failed.
            painter.drawPoint(pts[0])
        else:
            painter.drawPolyline(pts)
