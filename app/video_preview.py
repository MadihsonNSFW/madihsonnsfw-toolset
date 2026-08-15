"""Hover previews for playblast (.mp4) items.

Playblasts are loose FILES, so unlike .anim items they have no sequence/ folder
to play back. This module samples a handful of frames straight out of the mp4
with QtMultimedia and caches them as jpgs, after which the grid treats them
exactly like an anim's preview sequence.

Optimisations that matter here:
- SEEK to N evenly spaced positions instead of decoding the whole file, so cost
  is ~constant regardless of how long the playblast is
- cache keyed by (path, mtime, size) and written OUTSIDE the library, so shared
  library folders stay clean and a re-render invalidates the cache by itself
- one video at a time on a serial queue: playblasts are decoded by the OS codec
  and several at once just thrash
- items already cached never touch the decoder again (disk check only)
"""

import hashlib
import os
import shutil

from PySide6.QtCore import QObject, Qt, QTimer, QUrl, Signal
from PySide6.QtMultimedia import QMediaPlayer, QVideoSink

import config

FRAMES = 12           # frames sampled per playblast (matches anim previews' feel)
FRAME_SIZE = 256      # px, same as capture_preview's thumbnails
_JPEG_QUALITY = 85
_SEEK_TIMEOUT_MS = 4000   # per frame; a wedged decoder must not stall the queue

CACHE_ROOT = os.path.join(config.APP_DIR, "_preview_cache")


def _key(path):
    """Cache identity: path + mtime + size, so a re-rendered playblast of the
    same name misses the cache automatically."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    raw = "%s|%d|%d" % (os.path.normcase(os.path.abspath(path)),
                        int(st.st_mtime), st.st_size)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def cache_dir(path):
    k = _key(path)
    return os.path.join(CACHE_ROOT, k) if k else None


def cached_frames(path):
    """Sorted frame jpgs for a video, or [] when it isn't cached yet."""
    d = cache_dir(path)
    if not d or not os.path.isdir(d):
        return []
    try:
        return [os.path.join(d, f) for f in sorted(os.listdir(d))
                if f.endswith(".jpg")]
    except OSError:
        return []


def is_cached(path):
    return bool(cached_frames(path))


def purge_stale(max_dirs=400):
    """Drop the oldest cache dirs once the cache grows past max_dirs."""
    try:
        dirs = [os.path.join(CACHE_ROOT, d) for d in os.listdir(CACHE_ROOT)]
    except OSError:
        return 0
    dirs = [d for d in dirs if os.path.isdir(d)]
    if len(dirs) <= max_dirs:
        return 0
    dirs.sort(key=lambda d: os.path.getmtime(d))
    removed = 0
    for d in dirs[:len(dirs) - max_dirs]:
        shutil.rmtree(d, ignore_errors=True)
        removed += 1
    return removed


class VideoPreviewQueue(QObject):
    """Serial frame-extractor for playblasts.

    QMediaPlayer is event-loop driven and not thread-safe, so this runs on the
    GUI thread — it never blocks it, because every step is a signal callback.
    """

    ready = Signal(str)      # video path -> frames are on disk
    failed = Signal(str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._queue = []
        self._busy = False
        self._path = None
        self._out = None
        self._targets = []
        self._index = 0
        self._saved = 0

        self._sink = QVideoSink(self)
        self._player = QMediaPlayer(self)
        self._player.setVideoSink(self._sink)
        self._player.setAudioOutput(None)      # playblasts carry no audio
        self._sink.videoFrameChanged.connect(self._on_frame)
        self._player.mediaStatusChanged.connect(self._on_status)
        self._player.errorOccurred.connect(self._on_error)

        self._timeout = QTimer(self)
        self._timeout.setSingleShot(True)
        self._timeout.setInterval(_SEEK_TIMEOUT_MS)
        self._timeout.timeout.connect(self._on_timeout)

    # ------------------------------------------------------------------

    def enqueue(self, paths):
        """Queue videos that aren't cached yet (already-cached ones cost a stat)."""
        added = 0
        for p in paths:
            if p in self._queue or p == self._path:
                continue
            if is_cached(p):
                continue
            self._queue.append(p)
            added += 1
        self._pump()
        return added

    def clear_pending(self):
        """Drop everything not started (tab switch / rescan)."""
        self._queue = []

    def _pump(self):
        if self._busy or not self._queue:
            return
        self._busy = True
        self._path = self._queue.pop(0)
        self._index = 0
        self._saved = 0
        self._targets = []
        self._out = cache_dir(self._path)
        if not self._out:
            self._finish(False, "file vanished")
            return
        self._player.setSource(QUrl.fromLocalFile(self._path))
        self._timeout.start()

    def _on_status(self, status):
        if not self._busy:
            return  # tear-down emits NoMedia; that is not a failure
        if status == QMediaPlayer.LoadedMedia and not self._targets:
            dur = self._player.duration()
            if dur <= 0:
                self._finish(False, "zero-length video")
                return
            # sample inside the clip, skipping the very first/last frame
            self._targets = [int(dur * (i + 0.5) / FRAMES) for i in range(FRAMES)]
            self._player.pause()
            self._seek_next()
        elif status in (QMediaPlayer.InvalidMedia, QMediaPlayer.NoMedia):
            self._finish(False, "could not open the video")

    def _seek_next(self):
        if self._index >= len(self._targets):
            self._finish(self._saved > 0,
                         "" if self._saved else "no frames decoded")
            return
        self._timeout.start()
        self._player.setPosition(self._targets[self._index])

    def _on_frame(self, frame):
        if not self._busy or not self._targets or not frame.isValid():
            return
        if self._index >= len(self._targets):
            return
        image = frame.toImage()
        if image.isNull():
            return
        self._timeout.stop()
        image = image.scaled(FRAME_SIZE, FRAME_SIZE,
                             Qt.KeepAspectRatioByExpanding,
                             Qt.SmoothTransformation)
        try:
            os.makedirs(self._out, exist_ok=True)
            image.save(os.path.join(self._out, "frame_%02d.jpg" % self._index),
                       "JPG", _JPEG_QUALITY)
            self._saved += 1
        except OSError as exc:
            self._finish(False, str(exc))
            return
        self._index += 1
        # give the event loop a beat before the next seek
        QTimer.singleShot(0, self._seek_next)

    def _on_timeout(self):
        if not self._busy:
            return
        # this position refused to decode — skip it rather than stall the queue
        self._index += 1
        if self._index >= len(self._targets):
            self._finish(self._saved > 0, "" if self._saved else "decode timed out")
        else:
            self._seek_next()

    def _on_error(self, *_args):
        if self._busy:
            self._finish(False, self._player.errorString() or "playback error")

    def _finish(self, ok, error=""):
        if not self._busy:
            return
        # clear state BEFORE touching the player: setSource() can emit
        # mediaStatusChanged synchronously and re-enter this method
        path, out = self._path, self._out
        self._busy = False
        self._path = None
        self._out = None
        self._targets = []
        self._timeout.stop()
        self._player.stop()
        self._player.setSource(QUrl())
        if ok:
            self.ready.emit(path)
        else:
            if out and os.path.isdir(out) and not os.listdir(out):
                try:
                    os.rmdir(out)
                except OSError:
                    pass
            if path:
                self.failed.emit(path, error)
        QTimer.singleShot(0, self._pump)
