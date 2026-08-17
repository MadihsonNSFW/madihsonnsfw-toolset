"""Texture Maps — where a source image comes from, and the scene picker.

**The rule this module exists to enforce: pixels never cross the bridge.**
Blender tells the app which FILES its images are, and the app reads them
itself. Sending a 4096x4096 image over a JSON socket would be ~50 MB of
base64 per texture, on Blender's main thread, for something the app can read
off the same disk in a few milliseconds.

The one exception is an image with no file to read — packed into the .blend,
generated, or painted and unsaved. For those the app asks the add-on to write
ONE PNG to a path the app names (`tex_export`), and reads that. It is the same
shape as `capture_preview(path)` and `bake_texture(out_path)`, both of which
already take an app-named path.

⚠ **THE OPTIMIZER TRAP.** An image the Scene Optimizer has shrunk has a
STAND-IN as its `filepath` — a 512-px proxy of the user's real texture. Making
maps from that would silently produce maps at a sixteenth of the detail, and
look like nothing was wrong. `tex_list` reports `original` for exactly this,
and `Source.for_scene_image` prefers it over `filepath` every time.
"""
import os

from PySide6.QtCore import QObject, QSize, Qt, Signal
from PySide6.QtGui import QImage, QImageReader, QPixmap

import config

# Formats Qt reads and we are happy to generate from. A superset of the three
# the original site takes, because a desktop app has no reason to refuse a TIFF.
READABLE = (".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".bmp", ".tga")

# What a scene image is FOR, worked out from the node it feeds. Ordered: the
# picker shows base colours first, because those are what you generate FROM.
ROLE_ORDER = ("Base Color", "Emission", "Alpha", "Roughness", "Metallic",
              "Normal", "Displacement", "other")

THUMB = 46


def cache_dir():
    """Where scene images with no file of their own are written.

    Under DATA_DIR so a frozen build keeps it beside the exe and a source run
    keeps it in `app\\` — the same rule every other cache in the app follows.
    """
    path = os.path.join(config.DATA_DIR, "_texmaps_cache")
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        pass
    return path


class Source(object):
    """One image the tab can generate from, whatever it came from.

    Deliberately ONE type for both origins: the pipeline, the export naming and
    the tests all take a Source and never ask where it came from. That is what
    keeps "from a file" and "from the scene" equal citizens instead of one
    being a special case bolted onto the other.
    """

    def __init__(self, path="", name="", origin="file", image=None,
                 material="", role="", object_name="", width=0, height=0,
                 note=""):
        self.path = path
        self.name = name or os.path.basename(path)
        self.origin = origin                 # "file" | "scene" | "seamless"
        self.image = image                   # QImage, loaded lazily
        self.material = material
        self.role = role
        self.object_name = object_name
        self.width = width
        self.height = height
        self.note = note                     # anything the user should know

    # -------------------------------------------------------------- loading

    def load(self):
        """Read the pixels. Raises RuntimeError with a sentence on failure."""
        if self.image is not None and not self.image.isNull():
            return self.image
        if not self.path:
            raise RuntimeError("this source has no file to read")
        reader = QImageReader(self.path)
        reader.setAutoTransform(True)        # honour EXIF rotation
        image = reader.read()
        if image.isNull():
            raise RuntimeError("could not read %s: %s"
                               % (os.path.basename(self.path),
                                  reader.errorString()))
        self.image = image
        self.width, self.height = image.width(), image.height()
        return image

    @property
    def stem(self):
        """The base for every exported file name."""
        base = os.path.splitext(os.path.basename(self.path or self.name))[0]
        return base or "texture"

    def describe(self):
        """The line under the thumbnail."""
        bits = []
        if self.width and self.height:
            bits.append("%d×%d" % (self.width, self.height))
        if self.origin == "scene":
            if self.material:
                bits.append(self.material)
            if self.role and self.role != "other":
                bits.append(self.role)
        if self.note:
            bits.append(self.note)
        return "  ·  ".join(bits)

    # -------------------------------------------------------------- factory

    @classmethod
    def for_file(cls, path):
        return cls(path=path, origin="file")

    @classmethod
    def for_scene_image(cls, entry):
        """From one `tex_list` row.

        ⚠ `original` beats `filepath`: see the module docstring — a managed
        (shrunk) image's filepath is a proxy, and generating from it is a
        mistake nothing downstream could detect.
        """
        path = entry.get("original") or entry.get("filepath") or ""
        note = ""
        if entry.get("original") and entry.get("original") != entry.get("filepath"):
            note = "using your original, not the Optimizer's stand-in"
        elif entry.get("packed"):
            note = "packed into the .blend"
        elif entry.get("dirty"):
            note = "painted and unsaved"
        users = entry.get("users") or []
        first = users[0] if users else {}
        return cls(path=path, name=entry.get("name") or "",
                   origin="scene",
                   material=first.get("material", ""),
                   role=first.get("role", ""),
                   object_name=(first.get("objects") or [""])[0],
                   width=int((entry.get("size") or [0, 0])[0]),
                   height=int((entry.get("size") or [0, 0])[1]),
                   note=note)


def needs_export(entry):
    """True if the add-on must write this image out before we can read it."""
    if entry.get("packed") or entry.get("dirty"):
        return True
    path = entry.get("original") or entry.get("filepath") or ""
    if not path:
        return True
    if os.path.splitext(path)[1].lower() not in READABLE:
        return True
    return not os.path.isfile(path)


def sort_entries(entries):
    """Base colours first, then by role order, then by name.

    The picker is a list of textures someone is about to make maps FROM, and
    a base colour is nearly always the right answer — so it is not sorted
    alphabetically, which would bury it under a roughness map.
    """
    def key(entry):
        users = entry.get("users") or []
        role = (users[0].get("role") if users else "") or "other"
        try:
            index = ROLE_ORDER.index(role)
        except ValueError:
            index = len(ROLE_ORDER)
        return (index, (entry.get("name") or "").lower())
    return sorted(entries, key=key)


# ===========================================================================
# Thumbnails
# ===========================================================================

class ThumbCache(QObject):
    """Scaled thumbnails, read off the GUI thread, remembered by (path, mtime).

    ⚠ **`QImageReader.setScaledSize` is the whole point** — it lets the JPEG /
    PNG decoder produce the small image directly instead of decoding a
    4096x4096 into 64 MB of RAM and throwing 99.9% of it away. On a scene with
    thirty 4K textures that is the difference between a picker that opens and
    one that hangs the app for several seconds.
    """

    ready = Signal(str, QPixmap)             # path, thumbnail

    def __init__(self, parent=None):
        super().__init__(parent)
        self._cache = {}
        self._pending = set()
        self._pool = None

    def _key(self, path):
        try:
            return (path, os.path.getmtime(path))
        except OSError:
            return (path, 0)

    def get(self, path):
        """A thumbnail if we have one, else None and a background read."""
        if not path:
            return None
        key = self._key(path)
        hit = self._cache.get(key)
        if hit is not None:
            return hit
        if path not in self._pending:
            self._pending.add(path)
            self._start(path)
        return None

    def _start(self, path):
        from PySide6.QtCore import QThreadPool, QRunnable

        cache = self

        class _Job(QRunnable):
            def run(self):
                pixmap = None
                try:
                    reader = QImageReader(path)
                    reader.setAutoTransform(True)
                    size = reader.size()
                    if size.isValid() and size.width() > 0:
                        scale = float(THUMB * 2) / max(size.width(),
                                                       size.height())
                        if scale < 1.0:
                            reader.setScaledSize(QSize(
                                max(1, int(size.width() * scale)),
                                max(1, int(size.height() * scale))))
                    image = reader.read()
                    if not image.isNull():
                        pixmap = QPixmap.fromImage(image)
                except Exception:                             # noqa: BLE001
                    pixmap = None
                cache._finish(path, pixmap)

        if self._pool is None:
            self._pool = QThreadPool(self)
            # ⚠ Two threads, not `idealThreadCount`: this is disk-bound work
            # and a dozen readers on one drive is slower than two, while also
            # competing with whatever else the app is doing.
            self._pool.setMaxThreadCount(2)
        self._pool.start(_Job())

    def _finish(self, path, pixmap):
        self._pending.discard(path)
        if pixmap is None or pixmap.isNull():
            return
        self._cache[self._key(path)] = pixmap
        # ⚠ Emitted from the worker; Qt queues it onto the GUI thread because
        # the receiver lives there. Nothing here touches a widget.
        self.ready.emit(path, pixmap)

    def clear(self):
        self._cache.clear()
