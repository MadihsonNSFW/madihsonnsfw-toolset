# Playblast hover-preview cache, offscreen: cold-start decode of a real mp4
# (tests\assets\tiny.mp4, 6 frames) through VideoPreviewQueue, cache keyed to
# path|mtime|size, cache-hit re-enqueue costs no decode, failure path, and
import os
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# purge_stale. CACHE_ROOT is redirected to a temp dir — the real
# app\_preview_cache is never touched.
import os
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(_ROOT, "app"))

from PySide6.QtCore import QEventLoop, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import video_preview  # noqa: E402

PASS = []
FAIL = []


def ok(cond, label):
    (PASS if cond else FAIL).append(label)
    print(("ok   " if cond else "FAIL ") + label, flush=True)


MP4 = os.path.join(_ROOT, "tests", "assets", "tiny.mp4")
if not os.path.isfile(MP4):
    print("FAIL missing test asset %s" % MP4, flush=True)
    print("\n0 passed, 1 failed", flush=True)
    sys.exit(1)

tmp = tempfile.mkdtemp(prefix="madi_vidcache_")
video_preview.CACHE_ROOT = tmp          # never touch the real cache

app = QApplication.instance() or QApplication([])
q = video_preview.VideoPreviewQueue()

results = {}
q.ready.connect(lambda p: results.setdefault(p, "ready"))
q.failed.connect(lambda p, e: results.setdefault(p, "failed: %s" % e))


def wait_for(path, ms=30000):
    loop = QEventLoop()
    q.ready.connect(lambda p: loop.quit() if p == path else None)
    q.failed.connect(lambda p, e: loop.quit() if p == path else None)
    guard = QTimer()
    guard.setSingleShot(True)
    guard.timeout.connect(loop.quit)
    guard.start(ms)
    if path not in results:
        loop.exec()


# --- cold start -------------------------------------------------------------
ok(video_preview.is_cached(MP4) is False, "cold: not cached yet")
added = q.enqueue([MP4, MP4])
ok(added == 1, "duplicate enqueue collapsed (added=%d)" % added)
wait_for(MP4)
ok(results.get(MP4) == "ready",
   "decode finished with ready (got %s)" % results.get(MP4))
frames = video_preview.cached_frames(MP4)
ok(len(frames) >= 6,
   "sampled frames on disk (%d of %d targets)" % (len(frames),
                                                  video_preview.FRAMES))
ok(all(os.path.getsize(f) > 0 for f in frames), "frames are real jpgs")
cdir = video_preview.cache_dir(MP4)
ok(cdir is not None and cdir.startswith(tmp),
   "cache landed under the (redirected) cache root")

# --- warm: a cached video never touches the decoder again -------------------
ok(video_preview.is_cached(MP4), "warm: is_cached True")
ok(q.enqueue([MP4]) == 0, "warm re-enqueue costs nothing (added=0)")

# --- identity: touching the file invalidates the cache by itself ------------
old_dir = video_preview.cache_dir(MP4)
os.utime(MP4, (time.time() + 5, time.time() + 5))
ok(video_preview.cache_dir(MP4) != old_dir,
   "mtime bump -> different cache key (self-invalidating)")
ok(video_preview.is_cached(MP4) is False,
   "re-rendered playblast reads as uncached")

# --- failure path -----------------------------------------------------------
bogus = os.path.join(tmp, "not_a_video.mp4")
with open(bogus, "wb") as fh:
    fh.write(b"this is not an mp4")
q.enqueue([bogus])
wait_for(bogus)
ok(results.get(bogus, "").startswith("failed"),
   "garbage file fails cleanly (got %s)" % results.get(bogus))
ok(not video_preview.cached_frames(bogus), "no cache dir left behind")

# --- clear_pending drops queued-but-not-started work ------------------------
q._busy = True                        # pretend mid-decode
q.enqueue([MP4])                      # goes to the pending list
q.clear_pending()
ok(q._queue == [], "clear_pending empties the backlog")
q._busy = False

# --- purge_stale ------------------------------------------------------------
for i in range(6):
    d = os.path.join(tmp, "fake_%d" % i)
    os.makedirs(d, exist_ok=True)
    os.utime(d, (i + 1, i + 1))       # distinct ages, oldest first
removed = video_preview.purge_stale(max_dirs=3)
left = [d for d in os.listdir(tmp) if os.path.isdir(os.path.join(tmp, d))]
ok(removed >= 3, "purge removed the oldest dirs (removed=%d)" % removed)
ok(len(left) == 3, "cache capped at max_dirs (left=%d)" % len(left))

print("\n%d passed, %d failed" % (len(PASS), len(FAIL)), flush=True)
sys.exit(1 if FAIL else 0)
