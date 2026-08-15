"""The shared-memory ring — how pixels reach Blender, and how Blender answers.

One segment serves both viewers, which is what makes "the same frame in the app
and in the viewport" true by construction rather than by two things being
told to agree:

    decoder --writes pixels--> [ ring ] <--reads-- Blender's draw handler
                                  ^                        |
                                  +---- writes its scene frame/time ----+

Because Blender publishes its own frame and time into the header on every draw,
the app can follow Blender live by reading memory. It never polls the bridge,
and there is no per-frame IPC at all.

⚠ **Pixels are uint8 RGBA, not float32.** The first design converted in the
decoder because the conversion measured 3.62 ms at 1080p — but the proxy is
540p, where it is 0.75 ms. Shipping uint8 keeps numpy out of the app venv
(which keeps the exe small), makes the ring 4x smaller, and still leaves
Blender at ~1.21 ms per frame including the upload. Blender has numpy; the app
does not, and this is the reason it does not need it.

Tearing: each slot carries a seqlock. The writer bumps `seq` to odd, fills the
slot, bumps it to even; a reader that sees an odd or changed `seq` retries.
CPython stores into a shared mapping are plain memcpy with no explicit barrier,
but x86 store ordering plus several slots of rotation make a torn read
vanishingly unlikely — and the cost if it ever happened is one dropped frame,
which is why this is not worth a real lock.
"""

import struct
from multiprocessing import shared_memory

MAGIC = 0x4D525242          # 'MRRB'
VERSION = 1
HEADER_SIZE = 256
SLOT_HEADER = 64
DEFAULT_SLOTS = 4

# The three placements. ⚠ Must match the add-on's constants.
MODE_VIEWPORT = 0        # screen space, follows the view
MODE_PINNED = 1          # a real quad, left where the user pinned it
MODE_CAMERA = 2          # a real quad riding the scene camera
MODE_OVERLAY = MODE_VIEWPORT     # the name the ring has carried since 1.7.0

_H = struct.Struct("<IIIIIIIIII")           # magic..fps_den  (offset 0, 40 B)
_PRODUCER = struct.Struct("<IIQiI")         # newest_slot, newest_frame,
#                                             write_stamp, request_frame, flags
_PRODUCER_OFF = 40
_CONSUMER = struct.Struct("<ifQI")          # scene_frame, scene_fps, stamp, alive
_CONSUMER_OFF = 64
# ⚠ Blender publishes its RAW scene frame and scene fps, not a computed video
# time. All the offset/speed/fps-mismatch arithmetic then lives in exactly one
# place (decoder.target_frame) instead of being half in the add-on, where a
# change would need an add-on push to take effect.
# opacity, x, y, scale, rotation, mode, visible
# ⚠ x/y are the overlay's CENTRE as a fraction of the region, not its corner.
# Centre-anchored because scaling and rotating both happen about it — with a
# corner anchor the picture crawls across the screen as you resize it.
# ⚠ `rotation` is RADIANS and was added 2026-08-11; the add-on duplicates this
# format and `app_madiref_test.py` fails if the two drift.
_VIEW = struct.Struct("<fffffII")
_VIEW_OFF = 88

_SLOT = struct.Struct("<QII")               # seq, frame_index, reserved


def _slot_bytes(width, height):
    return SLOT_HEADER + width * height * 4


def segment_size(width, height, slots):
    return HEADER_SIZE + _slot_bytes(width, height) * slots


class _Base:
    """Shared header accessors. Both ends read most fields; each end owns
    the ones it writes, which is the whole concurrency story."""

    def __init__(self, shm):
        self._shm = shm
        (magic, version, self.width, self.height, self.slots, self.slot_bytes,
         self.pixel_offset, self.frame_count, self.fps_num,
         self.fps_den) = _H.unpack_from(shm.buf, 0)
        if magic != MAGIC:
            raise ValueError("not a MadiRef ring (bad magic)")
        if version != VERSION:
            raise ValueError("ring version %d, this build speaks %d"
                             % (version, VERSION))

    # ---------------------------------------------------------- geometry

    @property
    def fps(self):
        return self.fps_num / float(self.fps_den or 1)

    def _slot_off(self, i):
        return HEADER_SIZE + i * self.slot_bytes

    def _pixels_off(self, i):
        return self._slot_off(i) + SLOT_HEADER

    @property
    def pixel_bytes(self):
        return self.width * self.height * 4

    # ------------------------------------------------------ shared fields

    def producer_state(self):
        newest_slot, newest_frame, stamp, request, flags = _PRODUCER.unpack_from(
            self._shm.buf, _PRODUCER_OFF)
        return {"newest_slot": newest_slot, "newest_frame": newest_frame,
                "stamp": stamp, "request_frame": request, "flags": flags}

    def consumer_state(self):
        frame, fps, stamp, alive = _CONSUMER.unpack_from(self._shm.buf,
                                                         _CONSUMER_OFF)
        return {"scene_frame": frame, "scene_fps": fps,
                "stamp": stamp, "alive": alive}

    def view_state(self):
        opacity, x, y, scale, rotation, mode, visible = _VIEW.unpack_from(
            self._shm.buf, _VIEW_OFF)
        return {"opacity": opacity, "x": x, "y": y, "scale": scale,
                "rotation": rotation, "mode": mode, "visible": bool(visible)}

    def set_view_state(self, opacity=None, x=None, y=None, scale=None,
                       rotation=None, mode=None, visible=None):
        """⚠ Written by BOTH ends. The app writes when a control moves; Blender
        writes when the overlay is dragged in the viewport. Last writer wins,
        which is safe only because neither writes continuously — the app polls
        and mirrors instead of pushing."""
        cur = self.view_state()
        _VIEW.pack_into(
            self._shm.buf, _VIEW_OFF,
            cur["opacity"] if opacity is None else float(opacity),
            cur["x"] if x is None else float(x),
            cur["y"] if y is None else float(y),
            cur["scale"] if scale is None else float(scale),
            cur["rotation"] if rotation is None else float(rotation),
            cur["mode"] if mode is None else int(mode),
            int(cur["visible"] if visible is None else bool(visible)))

    def close(self):
        """⚠ Collect before closing.

        `newest()` hands out a memoryview ONTO the mapping, and numpy views
        built from it count too. While any of them is alive,
        `SharedMemory.close()` raises `BufferError: cannot close exported
        pointers exist`, the segment stays mapped, and a later `unlink()`
        cannot free it — so the next session's open collides with a ghost.
        Callers should release their views; this is the backstop.
        """
        if self._shm is not None:
            try:
                import gc
                gc.collect()
            except Exception:                        # noqa: BLE001
                pass
            try:
                self._shm.close()
            except BufferError:
                pass
            except Exception:                        # noqa: BLE001
                pass
            self._shm = None


class RingWriter(_Base):
    """App side. Creates the segment, publishes frames, reads Blender back."""

    def __init__(self, name, width, height, frame_count, fps_num, fps_den,
                 slots=DEFAULT_SLOTS):
        size = segment_size(width, height, slots)
        try:
            stale = shared_memory.SharedMemory(name=name)
            stale.close()
            stale.unlink()
        except FileNotFoundError:
            pass
        shm = shared_memory.SharedMemory(name=name, create=True, size=size)
        _H.pack_into(shm.buf, 0, MAGIC, VERSION, width, height, slots,
                     _slot_bytes(width, height), SLOT_HEADER, frame_count,
                     fps_num, fps_den or 1)
        _PRODUCER.pack_into(shm.buf, _PRODUCER_OFF, 0, 0, 0, -1, 0)
        _CONSUMER.pack_into(shm.buf, _CONSUMER_OFF, -1, 0.0, 0, 0)
        # centre near the lower left, a third of the region wide, unrotated
        _VIEW.pack_into(shm.buf, _VIEW_OFF, 1.0, 0.18, 0.20, 0.32, 0.0,
                        MODE_OVERLAY, 1)
        for i in range(slots):
            _SLOT.pack_into(shm.buf, HEADER_SIZE + i * _slot_bytes(width, height),
                            0, 0xFFFFFFFF, 0)
        super().__init__(shm)
        self.name = name
        self._next = 0
        self._stamp = 0

    def write_frame(self, frame_index, pixels):
        """Publish one frame. `pixels` is any buffer of width*height*4 bytes
        in RGBA order — a QImage's bits() goes straight in with no conversion.
        """
        need = self.pixel_bytes
        mv = memoryview(pixels).cast("B")
        if len(mv) < need:
            raise ValueError("frame is %d bytes, ring slot needs %d"
                             % (len(mv), need))
        i = self._next % self.slots
        self._next += 1
        self._stamp += 1
        base = self._slot_off(i)
        seq = self._stamp * 2
        _SLOT.pack_into(self._shm.buf, base, seq - 1, frame_index, 0)  # odd
        off = self._pixels_off(i)
        self._shm.buf[off:off + need] = mv[:need]
        _SLOT.pack_into(self._shm.buf, base, seq, frame_index, 0)      # even
        cur = self.producer_state()
        _PRODUCER.pack_into(self._shm.buf, _PRODUCER_OFF, i, frame_index,
                            self._stamp, cur["request_frame"], cur["flags"])

    def set_request(self, frame_index):
        cur = self.producer_state()
        _PRODUCER.pack_into(self._shm.buf, _PRODUCER_OFF, cur["newest_slot"],
                            cur["newest_frame"], cur["stamp"], int(frame_index),
                            cur["flags"])

    def unlink(self):
        shm = self._shm
        self.close()
        if shm is not None:
            try:
                shm.unlink()
            except (FileNotFoundError, Exception):   # noqa: BLE001
                pass


class RingReader(_Base):
    """Blender side (and the app's own preview). Read-only for pixels."""

    def __init__(self, name):
        super().__init__(shared_memory.SharedMemory(name=name))
        self.name = name

    def newest(self, retries=3):
        """(frame_index, memoryview) for the freshest complete slot, or None.

        The memoryview is a live window onto shared memory — the caller must
        consume it before the writer laps the ring. At four slots and normal
        frame rates that is many milliseconds; the upload takes ~1 ms.
        """
        for _ in range(retries):
            i = self.producer_state()["newest_slot"]
            if not 0 <= i < self.slots:
                return None
            base = self._slot_off(i)
            seq0, frame_index, _ = _SLOT.unpack_from(self._shm.buf, base)
            if seq0 % 2:                     # writer is mid-slot
                continue
            off = self._pixels_off(i)
            mv = self._shm.buf[off:off + self.pixel_bytes]
            seq1, _, _ = _SLOT.unpack_from(self._shm.buf, base)
            if seq1 == seq0:
                return frame_index, mv
        return None

    def publish_consumer_state(self, scene_frame, scene_fps):
        """Blender tells the app where the timeline is. This is what the app
        follows, and what the decoder maps into a proxy frame."""
        cur = self.consumer_state()
        _CONSUMER.pack_into(self._shm.buf, _CONSUMER_OFF, int(scene_frame),
                            float(scene_fps), cur["stamp"] + 1,
                            (cur["alive"] + 1) & 0xFFFFFFFF)


def open_ring(name):
    """A RingReader, or None if the segment is gone. Never raises for an
    absent segment — the app closing is a normal event, not an error."""
    try:
        return RingReader(name)
    except (FileNotFoundError, ValueError):
        return None
