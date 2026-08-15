"""Source video -> MadiRef proxy. Runs once per clip, with a progress bar.

Two backends, same output:

* **ffmpeg** when one can be found. Faster, and it reads things Windows will
  not (ProRes, DNxHD, awkward MKV). Preferred whenever available.
* **QtMultimedia** otherwise. Ships with PySide6 and is backed by Media
  Foundation, so it is present on every Windows 10/11 machine — which is the
  whole reason the Toolset does not carry an ~80 MB ffmpeg and keeps its
  one-file-exe update story.

⚠ The Qt backend PLAYS THROUGH SEQUENTIALLY at an accelerated rate; it does not
seek. `video_preview.py` seeks (and carries a 4 s timeout because "a wedged
decoder must not stall the queue") — that is the right shape for sampling 12
thumbnails and the wrong shape here. Measured on a 1080p30 long-GOP clip,
sequential play-through delivered 300/300 frames in order at every rate up to
16x. **Do not "optimise" this into a seek loop.**

⚠ Rate 8 is the setting, not a starting point: 16x and 32x measured *slower*
(the encode becomes the bottleneck and the extra rate only adds contention).

Threading: QMediaPlayer stays on the GUI thread — Qt multimedia is event-loop
driven and fragile off it — and JPEG encoding plus file writing go to a worker.
The queue between them is BOUNDED and the player is paused when it backs up,
because an unbounded queue of 1080p frames is ~8 MB each and would happily eat
several GB on a long clip.
"""

import hashlib
import os
import queue
import shutil
import struct
import subprocess
import threading

from PySide6.QtCore import (QBuffer, QByteArray, QObject, QTimer, QUrl, Qt,
                            Signal)
from PySide6.QtMultimedia import QMediaPlayer, QVideoSink

import config

from . import proxy

CACHE_ROOT = os.path.join(config.APP_DIR, "_madiref_cache")

PROXY_HEIGHT = 540          # reference does not need more; 20.1 KB/frame here
JPEG_QUALITY = 85
PLAYBACK_RATE = 8.0
QUEUE_HIGH = 48             # pause the decoder above this many pending frames
QUEUE_LOW = 16              # ...and let it run again below this
_STALL_MS = 15000           # no frames for this long = a wedged decoder

_FFMPEG_HINTS = (
    r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
    r"D:\Program Files\ShareX\ffmpeg.exe",
    r"D:\Program Files\Shutter Encoder\Library\ffmpeg.exe",
)


# --------------------------------------------------------------- cache keys

def source_key(path):
    """Identity of a source video: path + mtime + size.

    Same idiom as video_preview.py — re-export a reference clip under the same
    name and the cache misses by itself, with no explicit invalidation.
    """
    try:
        st = os.stat(path)
    except OSError:
        return None
    raw = "%s|%d|%d" % (os.path.normcase(os.path.abspath(path)),
                        int(st.st_mtime), st.st_size)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def proxy_path(source):
    k = source_key(source)
    return os.path.join(CACHE_ROOT, k + ".mrfx") if k else None


def cached_proxy(source):
    """An open ProxyReader when this clip is already ingested, else None."""
    p = proxy_path(source)
    return proxy.open_proxy(p) if p else None


def is_ingested(source):
    r = cached_proxy(source)
    if r is None:
        return False
    r.close()
    return True


DEFAULT_CACHE_GB = 6.0


def cache_entries():
    """(path, bytes, mtime) for every cached proxy, newest last."""
    try:
        names = [os.path.join(CACHE_ROOT, f) for f in os.listdir(CACHE_ROOT)
                 if f.endswith(".mrfx")]
    except OSError:
        return []
    out = []
    for f in names:
        try:
            st = os.stat(f)
        except OSError:
            continue
        out.append((f, st.st_size, st.st_mtime))
    out.sort(key=lambda e: e[2])
    return out


def cache_size():
    return sum(e[1] for e in cache_entries())


def clear_cache():
    """Delete every prepared clip. Costs only the time to prepare them again."""
    removed = freed = 0
    for path, size, _m in cache_entries():
        try:
            os.remove(path)
            removed += 1
            freed += size
        except OSError:
            pass
    return removed, freed


def purge_stale(max_files=60, max_bytes=None):
    """Trim the cache to a SIZE budget, oldest first.

    ⚠ This used to cap the FILE COUNT only, which is close to useless here: a
    proxy is ~36 MB per minute of footage, so four of Marty's clips reached
    3.9 GB while sitting far under a 60-file limit. Long references are exactly
    the ones worth caching AND the ones that bloat, so the budget has to be in
    bytes.
    """
    if max_bytes is None:
        try:
            import config
            gb = float(config.load().get("madiref_cache_gb", DEFAULT_CACHE_GB))
        except Exception:                            # noqa: BLE001
            gb = DEFAULT_CACHE_GB
        max_bytes = max(gb, 0.5) * (1024 ** 3)
    entries = cache_entries()
    total = sum(e[1] for e in entries)
    removed = 0
    # oldest first, until both budgets are satisfied
    for path, size, _m in entries:
        if total <= max_bytes and len(entries) - removed <= max_files:
            break
        try:
            os.remove(path)
            total -= size
            removed += 1
        except OSError:
            pass
    return removed


def find_ffmpeg(explicit=None):
    """A usable ffmpeg.exe, or None.

    ⚠ Do NOT also look for ffprobe and require it — plenty of builds ship
    without one (ShareX's is configured --disable-ffprobe), and needing it
    would reject a perfectly good ffmpeg.
    """
    for cand in (explicit,) + _FFMPEG_HINTS:
        if cand and os.path.isfile(cand):
            return cand
    return shutil.which("ffmpeg")


# ------------------------------------------------------------- JPEG worker

class _EncodeWorker(threading.Thread):
    """Scales, JPEG-encodes and writes frames handed over by the decoder.

    Kept off the GUI thread so ingest does not freeze the app; a single worker
    is enough because the decoder side is the slower half.
    """

    def __init__(self, writer, q, height, quality):
        super().__init__(daemon=True)
        self.writer = writer
        self.q = q
        self.height = height
        self.quality = quality
        self.error = None
        self.done = 0

    def run(self):
        while True:
            item = self.q.get()
            try:
                if item is None:
                    return
                index, image = item
                try:
                    if image.height() != self.height:
                        image = image.scaledToHeight(
                            self.height, Qt.SmoothTransformation)
                    ba = QByteArray()
                    buf = QBuffer(ba)
                    buf.open(QBuffer.WriteOnly)
                    image.save(buf, "JPG", self.quality)
                    buf.close()
                    self.writer.add_frame(index, bytes(ba))
                    self.done += 1
                except Exception as exc:            # noqa: BLE001
                    if self.error is None:
                        self.error = str(exc)
            finally:
                self.q.task_done()


# ------------------------------------------------------------------ ingest

class IngestJob(QObject):
    """One clip -> one proxy. Lives on the GUI thread."""

    progress = Signal(int, int, str)     # done, total (0 = unknown), note
    finished = Signal(str)               # proxy path
    failed = Signal(str)

    def __init__(self, source, parent=None, ffmpeg=None,
                 height=PROXY_HEIGHT, quality=JPEG_QUALITY, use_ffmpeg=True):
        super().__init__(parent)
        self.source = source
        self.height = height
        self.quality = quality
        self.ffmpeg = ffmpeg
        # use_ffmpeg=False forces the Qt backend. That path is the one that has
        # to work on a machine with no ffmpeg at all, so the tests exercise it
        # explicitly rather than trusting it to be reached by absence.
        self.use_ffmpeg = use_ffmpeg
        self._out = proxy_path(source)
        self._writer = None
        self._worker = None
        self._q = None
        self._player = None
        self._sink = None
        self._count = 0
        self._expected = 0
        self._cancelled = False
        self._ended = False
        self._paused_for_queue = False
        self._src_size = (0, 0)
        self._fps = (0, 1)

    # ------------------------------------------------------------------

    def start(self):
        if not self._out:
            self.failed.emit("source file vanished")
            return
        existing = proxy.open_proxy(self._out)
        if existing is not None:
            existing.close()
            self.finished.emit(self._out)
            return
        os.makedirs(CACHE_ROOT, exist_ok=True)
        ff = find_ffmpeg(self.ffmpeg) if self.use_ffmpeg else None
        if ff:
            QTimer.singleShot(0, lambda: self._run_ffmpeg(ff))
        else:
            self._run_qt()

    def cancel(self):
        self._cancelled = True

    # --------------------------------------------------------- ffmpeg path

    def _run_ffmpeg(self, exe):
        """Pull an MJPEG stream out of ffmpeg and split it on SOI/EOI.

        image2pipe + mjpeg gives us exactly the bytes the proxy stores, so
        there is no decode/re-encode round trip on this path at all.
        """
        try:
            w, h, fps_n, fps_d, nframes = self._ffmpeg_probe(exe)
            self._src_size = (w, h)
            self._fps = (fps_n, fps_d)
            self._expected = nframes
            self._writer = proxy.ProxyWriter(
                self._out, nframes, w, h,
                int(round(self.height * (w / float(h)))) if h else 0,
                self.height, fps_n, fps_d, jpeg_quality=self.quality)
            cmd = [exe, "-hide_banner", "-loglevel", "error", "-nostdin",
                   "-i", self.source,
                   "-vf", "scale=-2:%d" % self.height,
                   "-q:v", "3", "-f", "image2pipe", "-vcodec", "mjpeg", "-"]
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE,
                                    creationflags=getattr(subprocess,
                                                          "CREATE_NO_WINDOW", 0))
            buf = b""
            while True:
                if self._cancelled:
                    proc.kill()
                    self._writer.abort()
                    self.failed.emit("cancelled")
                    return
                chunk = proc.stdout.read(1 << 20)
                if not chunk:
                    break
                buf += chunk
                buf = self._drain_jpegs(buf)
            proc.stdout.close()
            err = proc.stderr.read().decode("utf-8", "replace").strip()
            proc.wait()
            self._drain_jpegs(buf, final=True)
            if not self._count:
                self._writer.abort()
                self.failed.emit(err or "ffmpeg produced no frames")
                return
            # ⚠ The probe parses ffmpeg's stderr, which can legitimately fail to
            # yield a size (odd containers, unusual builds). A proxy written with
            # proxy_w = 0 opens fine and then the DECODER quietly refuses to
            # start, which reads as "the clip is broken". Recover the real size
            # from a frame we just wrote instead of trusting the probe.
            if not self._writer.header["proxy_w"]:
                self._fix_size_from_first_frame()
            self._writer.close()
            self.progress.emit(self._count, self._count, "done")
            self.finished.emit(self._out)
        except Exception as exc:                     # noqa: BLE001
            if self._writer is not None:
                self._writer.abort()
            # ffmpeg failing is not fatal — Qt may still read this file
            self._writer = None
            self._count = 0
            self._run_qt()
            _ = exc

    def _drain_jpegs(self, buf, final=False):
        """Cut complete JPEGs (FFD8..FFD9) out of the stream buffer."""
        while True:
            start = buf.find(b"\xff\xd8")
            if start < 0:
                return b"" if final else buf
            end = buf.find(b"\xff\xd9", start + 2)
            if end < 0:
                return buf[start:]
            blob = buf[start:end + 2]
            buf = buf[end + 2:]
            self._writer.add_frame(self._count, blob)
            self._count += 1
            if self._count % 25 == 0:
                self.progress.emit(self._count, self._expected, "ingesting")

    def _fix_size_from_first_frame(self):
        """Read frame 0 back off the writer's own file and take its size."""
        try:
            off, size = self._writer._index[0]
            if not size:
                return
            with open(self._out, "rb") as fh:
                fh.seek(off)
                blob = fh.read(size)
            from PySide6.QtGui import QImage
            img = QImage()
            if img.loadFromData(blob, "JPG") and img.height():
                self._writer.header["proxy_w"] = img.width()
                self._writer.header["proxy_h"] = img.height()
                if not self._writer.header["src_w"]:
                    self._writer.header["src_w"] = img.width()
                    self._writer.header["src_h"] = img.height()
        except Exception:                            # noqa: BLE001
            pass

    def _ffmpeg_probe(self, exe):
        """Size / fps / frame count without ffprobe (many builds lack it).

        ffmpeg writes stream details to stderr on a null-output pass; that is
        cheap and always available.
        """
        cmd = [exe, "-hide_banner", "-nostdin", "-i", self.source,
               "-map", "0:v:0", "-c", "copy", "-f", "null", "-"]
        p = subprocess.run(cmd, capture_output=True,
                           creationflags=getattr(subprocess,
                                                 "CREATE_NO_WINDOW", 0))
        text = p.stderr.decode("utf-8", "replace")
        w = h = 0
        fps_n, fps_d = 0, 1
        nframes = 0
        for line in text.splitlines():
            if " Video: " in line and not w:
                for tok in line.split(","):
                    tok = tok.strip()
                    if "x" in tok and tok.split(" ")[0].replace("x", "").isdigit():
                        try:
                            a, b = tok.split(" ")[0].split("x")
                            w, h = int(a), int(b)
                        except ValueError:
                            pass
                    if tok.endswith(" fps"):
                        try:
                            f = float(tok[:-4])
                            fps_n, fps_d = int(round(f * 1000)), 1000
                        except ValueError:
                            pass
            if line.startswith("frame=") or " frame=" in line:
                try:
                    nframes = int(line.split("frame=")[1].split()[0])
                except (IndexError, ValueError):
                    pass
        return w, h, (fps_n or 30000), (fps_d or 1000), nframes

    # ------------------------------------------------------------- Qt path

    def _run_qt(self):
        self._q = queue.Queue()
        self._sink = QVideoSink(self)
        self._player = QMediaPlayer(self)
        self._player.setVideoSink(self._sink)
        self._player.setAudioOutput(None)
        self._sink.videoFrameChanged.connect(self._on_frame)
        self._player.mediaStatusChanged.connect(self._on_status)
        self._player.errorOccurred.connect(self._on_error)

        self._stall = QTimer(self)
        self._stall.setSingleShot(True)
        self._stall.setInterval(_STALL_MS)
        self._stall.timeout.connect(lambda: self._fail("the decoder stalled"))

        self._pump = QTimer(self)
        self._pump.setInterval(50)
        self._pump.timeout.connect(self._check_queue)

        self._player.setSource(QUrl.fromLocalFile(self.source))
        self._player.setPlaybackRate(PLAYBACK_RATE)
        self._stall.start()

    def _on_status(self, status):
        if self._ended:
            return
        if status == QMediaPlayer.LoadedMedia and self._writer is None:
            md = self._player.metaData()
            fps = 0.0
            try:
                from PySide6.QtMultimedia import QMediaMetaData
                fps = float(md.value(QMediaMetaData.VideoFrameRate) or 0)
            except Exception:                        # noqa: BLE001
                fps = 0.0
            if fps <= 0:
                fps = 30.0
            self._fps = (int(round(fps * 1000)), 1000)
            dur = max(self._player.duration(), 0)
            self._expected = int(dur * fps / 1000.0) if dur else 0
            self._writer = proxy.ProxyWriter(
                self._out, self._expected, 0, 0, 0, self.height,
                self._fps[0], self._fps[1], duration_ms=dur,
                jpeg_quality=self.quality)
            self._worker = _EncodeWorker(self._writer, self._q,
                                         self.height, self.quality)
            self._worker.start()
            self._pump.start()
            self._player.play()
        elif status == QMediaPlayer.EndOfMedia:
            self._finish_qt()
        elif status == QMediaPlayer.InvalidMedia:
            self._fail("Windows cannot decode this file — install ffmpeg or "
                       "convert the clip")

    def _on_frame(self, frame):
        if self._ended or self._writer is None or not frame.isValid():
            return
        if self._cancelled:
            self._fail("cancelled")
            return
        image = frame.toImage()
        if image.isNull():
            return
        self._stall.start()
        if not self._src_size[0]:
            self._src_size = (image.width(), image.height())
            self._writer.header["src_w"] = image.width()
            self._writer.header["src_h"] = image.height()
            if image.height():
                self._writer.header["proxy_w"] = int(round(
                    self.height * image.width() / float(image.height())))
        self._q.put((self._count, image))
        self._count += 1
        if self._count % 25 == 0:
            self.progress.emit(self._worker.done if self._worker else 0,
                               self._expected, "ingesting")

    def _check_queue(self):
        """Backpressure. Pausing the decoder is the only thing standing
        between a long clip and several GB of queued frames."""
        if self._ended or self._player is None:
            return
        depth = self._q.qsize()
        if not self._paused_for_queue and depth >= QUEUE_HIGH:
            self._player.pause()
            self._paused_for_queue = True
        elif self._paused_for_queue and depth <= QUEUE_LOW:
            self._player.play()
            self._paused_for_queue = False

    def _finish_qt(self):
        if self._ended:
            return
        self._ended = True
        self._pump.stop()
        self._stall.stop()
        self._player.stop()
        self._q.put(None)
        self._worker.join(timeout=60)
        if self._worker.error and not self._worker.done:
            self._writer.abort()
            self.failed.emit(self._worker.error)
            return
        if not self._worker.done:
            self._writer.abort()
            self.failed.emit("no frames could be decoded")
            return
        self._writer.close()
        self.progress.emit(self._worker.done, self._worker.done, "done")
        self.finished.emit(self._out)

    def _on_error(self, *_a):
        self._fail(self._player.errorString() or "playback error")

    def _fail(self, msg):
        if self._ended:
            return
        self._ended = True
        for t in ("_pump", "_stall"):
            timer = getattr(self, t, None)
            if timer is not None:
                timer.stop()
        if self._player is not None:
            self._player.stop()
        if self._worker is not None:
            self._q.put(None)
            self._worker.join(timeout=10)
        if self._writer is not None:
            self._writer.abort()
        self.failed.emit(msg)
