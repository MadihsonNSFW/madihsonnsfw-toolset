"""MadiRef — video reference in Blender's viewport and in the app, in sync.

The problem this solves is not "play a video": it is that scrubbing a normal
delivery codec is slow, and a reference clip is scrubbed far more than it is
watched. Long-GOP H.264 has to decode from the previous keyframe every time you
land somewhere, which is why the same file feels sluggish inside Blender.

The shape of the answer, and why each piece exists:

  ingest.py   once per clip, transcode to an all-intra proxy. Every frame
              independent, so a seek is an offset lookup. This is the single
              biggest win — bigger than GPU decode.
  proxy.py    the .mrfx container: header + JPEG payload + index.
  shm.py      a named shared-memory ring. Blender maps it READ-ONLY and
              uploads straight from it; measured zero-copy.
  decoder.py  proxy -> float32 -> ring, on a worker thread. The float32
              conversion lives here on purpose: it is the most expensive step,
              and Blender's main thread is already evaluating the rig.
  audio.py    audio from the ORIGINAL file, app-side only.
  tab.py      the MadiRef tab.

Timing is mapped by TIME, never by frame index, so a 60 fps clip on a 24 fps
scene shows the right moment instead of being stretched. See docs\\madiref.md.
"""

from .ingest import (CACHE_ROOT, IngestJob, cached_proxy, find_ffmpeg,
                     is_ingested, proxy_path, purge_stale, source_key)
from .proxy import ProxyError, ProxyReader, ProxyWriter, open_proxy

__all__ = [
    "CACHE_ROOT", "IngestJob", "ProxyError", "ProxyReader", "ProxyWriter",
    "cached_proxy", "find_ffmpeg", "is_ingested", "open_proxy", "proxy_path",
    "purge_stale", "source_key",
]
