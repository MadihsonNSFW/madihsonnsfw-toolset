"""MadiRef proxy container (.mrfx) — the thing that makes playback fast.

A reference video is played by SCRUBBING far more than by watching, and that is
exactly what a normal delivery codec is worst at: long-GOP H.264 has to decode
from the previous keyframe every time you land on a frame, which is why
Blender feels slow on the same file. The proxy fixes it structurally rather
than by being clever — every frame is stored independently, so seeking to frame
N is a file offset lookup and one JPEG decode, no matter where N is.

Layout:

    [ header 256 B ][ payload: JPEG blobs ][ index: frame_count * 12 B ]

The index is written LAST because the writer streams frames as they arrive and
does not know their sizes up front; `index_offset` in the header is patched on
close. A file whose header still says index_offset == 0 was interrupted
mid-ingest and must be treated as absent — that is the crash-safety story, and
it is why `open_proxy` refuses one rather than trying to salvage it.

Reads go through mmap so random access costs a memcpy of one JPEG and the OS
page cache does the rest.
"""

import mmap
import os
import struct

MAGIC = 0x4D524658          # 'MRFX'
VERSION = 1
HEADER_SIZE = 256
INDEX_ENTRY = struct.Struct("<QI")      # offset, size
_HEADER = struct.Struct("<IIIIIIIIIIQQQII")

FLAG_HAS_AUDIO = 1 << 0


class ProxyError(Exception):
    pass


def _pack_header(h):
    blob = _HEADER.pack(
        MAGIC, VERSION, h["frame_count"], h["src_w"], h["src_h"],
        h["proxy_w"], h["proxy_h"], h["fps_num"], h["fps_den"], h["flags"],
        h["index_offset"], h["payload_offset"], h["duration_ms"],
        h["jpeg_quality"], 0)
    return blob + b"\0" * (HEADER_SIZE - len(blob))


def _unpack_header(blob):
    # ⚠ Check the LENGTH before unpacking. A file too short to hold a header
    # (any random small file that happens to be named .mrfx) made
    # `struct.unpack_from` raise `struct.error`, which is NOT a ValueError and
    # so escaped open_proxy's handler — a junk cache entry crashed the tab
    # instead of being re-ingested.
    if len(blob) < _HEADER.size:
        raise ProxyError("file is too small to be a proxy (%d bytes)"
                         % len(blob))
    (magic, version, frame_count, src_w, src_h, proxy_w, proxy_h,
     fps_num, fps_den, flags, index_offset, payload_offset, duration_ms,
     jpeg_quality, _res) = _HEADER.unpack_from(blob, 0)
    if magic != MAGIC:
        raise ProxyError("not a MadiRef proxy (bad magic)")
    if version != VERSION:
        raise ProxyError("proxy version %d, this build reads %d"
                         % (version, VERSION))
    return {
        "frame_count": frame_count, "src_w": src_w, "src_h": src_h,
        "proxy_w": proxy_w, "proxy_h": proxy_h,
        "fps_num": fps_num, "fps_den": fps_den, "flags": flags,
        "index_offset": index_offset, "payload_offset": payload_offset,
        "duration_ms": duration_ms, "jpeg_quality": jpeg_quality,
    }


class ProxyWriter:
    """Streams frames into a .mrfx. Frames may arrive out of order.

    Out-of-order tolerance matters because encoding is done on a worker thread
    pool: the index is addressed by frame number, so a late frame lands in the
    right slot regardless of when its bytes were appended.
    """

    def __init__(self, path, frame_count, src_w, src_h, proxy_w, proxy_h,
                 fps_num, fps_den, duration_ms=0, jpeg_quality=85):
        self.path = path
        self.header = {
            "frame_count": frame_count, "src_w": src_w, "src_h": src_h,
            "proxy_w": proxy_w, "proxy_h": proxy_h,
            "fps_num": fps_num, "fps_den": fps_den, "flags": 0,
            "index_offset": 0, "payload_offset": HEADER_SIZE,
            "duration_ms": duration_ms, "jpeg_quality": jpeg_quality,
        }
        self._index = [(0, 0)] * frame_count
        self._written = 0
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._fh = open(path, "wb")
        # index_offset stays 0 until close(), which is what marks a partial
        # file as unusable if we are killed mid-ingest
        self._fh.write(_pack_header(self.header))

    @property
    def written(self):
        return self._written

    def add_frame(self, index, jpeg_bytes):
        if self._fh is None:
            raise ProxyError("writer is closed")
        if index < 0:
            raise ProxyError("frame %d out of range" % index)
        if index >= len(self._index):
            # The frame count passed in is only a HINT. Container metadata lies
            # (variable frame rate, wrong duration, missing frame rate), so the
            # real count is whatever actually came out of the decoder — grow to
            # fit rather than dropping the tail of someone's reference.
            self._index.extend([(0, 0)] * (index + 1 - len(self._index)))
        offset = self._fh.tell()
        self._fh.write(jpeg_bytes)
        self._index[index] = (offset, len(jpeg_bytes))
        self._written += 1

    def set_flag(self, flag):
        self.header["flags"] |= flag

    def close(self):
        """Write the index, patch the header, and only then call it a proxy."""
        if self._fh is None:
            return
        # Frames that never arrived keep (0, 0). Point them at the nearest
        # earlier real frame so a gap shows the previous picture instead of
        # failing — a reference video with one undecodable frame is still
        # usable, and a hard error here would throw away the whole ingest.
        last = None
        for i, (off, size) in enumerate(self._index):
            if size:
                last = (off, size)
            elif last is not None:
                self._index[i] = last
        # Trim trailing frames that never arrived (an over-long hint) so the
        # count is the truth, then the index describes exactly what is here.
        while self._index and not self._index[-1][1]:
            self._index.pop()
        index_offset = self._fh.tell()
        for off, size in self._index:
            self._fh.write(INDEX_ENTRY.pack(off, size))
        self.header["index_offset"] = index_offset
        self.header["frame_count"] = len(self._index)
        self._fh.seek(0)
        self._fh.write(_pack_header(self.header))
        self._fh.flush()
        os.fsync(self._fh.fileno())
        self._fh.close()
        self._fh = None

    def abort(self):
        """Give up and leave nothing behind — a half file would be read as a
        cache hit next time and serve garbage."""
        if self._fh is not None:
            self._fh.close()
            self._fh = None
        try:
            os.remove(self.path)
        except OSError:
            pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.close()
        else:
            self.abort()
        return False


class ProxyReader:
    """Random access to proxy frames. One mmap, no per-frame file I/O."""

    def __init__(self, path):
        self.path = path
        self._fh = open(path, "rb")
        try:
            self._mm = mmap.mmap(self._fh.fileno(), 0, access=mmap.ACCESS_READ)
        except Exception:
            self._fh.close()
            raise
        try:
            self.header = _unpack_header(self._mm[:HEADER_SIZE])
        except Exception:
            self.close()
            raise
        if not self.header["index_offset"]:
            self.close()
            raise ProxyError("proxy is incomplete (ingest was interrupted)")
        self._index_off = self.header["index_offset"]

    @property
    def frame_count(self):
        return self.header["frame_count"]

    @property
    def size(self):
        return (self.header["proxy_w"], self.header["proxy_h"])

    @property
    def fps(self):
        den = self.header["fps_den"] or 1
        return self.header["fps_num"] / float(den)

    @property
    def has_audio(self):
        return bool(self.header["flags"] & FLAG_HAS_AUDIO)

    def frame_bytes(self, index):
        """The JPEG blob for a frame, clamped to the valid range.

        Clamping rather than raising is deliberate: the caller is a timeline
        mapping that can legitimately ask for a frame before the start or past
        the end, and holding the first/last picture is the useful answer.
        """
        n = self.frame_count
        if n <= 0:
            return b""
        index = 0 if index < 0 else (n - 1 if index >= n else index)
        off, size = INDEX_ENTRY.unpack_from(
            self._mm, self._index_off + index * INDEX_ENTRY.size)
        if not size:
            return b""
        return self._mm[off:off + size]

    def close(self):
        mm = getattr(self, "_mm", None)
        if mm is not None:
            try:
                mm.close()
            except Exception:
                pass
            self._mm = None
        if getattr(self, "_fh", None) is not None:
            try:
                self._fh.close()
            except Exception:
                pass
            self._fh = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()
        return False


def open_proxy(path):
    """A ProxyReader, or None when the file is missing/partial/foreign.

    Never raises for an unusable cache entry — a bad proxy means "re-ingest",
    not "the app is broken".
    """
    if not path or not os.path.isfile(path):
        return None
    try:
        return ProxyReader(path)
    except (ProxyError, OSError, ValueError, struct.error):
        return None
