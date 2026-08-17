"""Build `app\\assets\\app_icon.icns` from `app_icon.ico`.

    python tools\\make_icns.py

macOS will not take a `.ico`, and PyInstaller's `--icon` wants a real `.icns`
on that platform. Rather than add a build dependency (`iconutil` is macOS-only,
`pillow` is not in this project), the container is written here: an `.icns` is
just a header plus typed chunks, and each chunk may hold a PNG.

⚠ **ONLY SIZES THE SOURCE ACTUALLY HAS.** The `.ico` tops out at 256x256, so
512 and 1024 slots are left out rather than filled with an upscale. macOS falls
back to the largest size present and scales it itself — the same pixels either
way, without this file claiming a resolution it does not have. If a crisper
Finder preview is ever wanted, the fix is a bigger SOURCE icon, not a bigger
number here.
"""
import os
import struct
import sys

from PySide6.QtCore import QBuffer, QByteArray, QSize, Qt
from PySide6.QtGui import QGuiApplication, QImageReader

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "app", "assets", "app_icon.ico")
OUT = os.path.join(ROOT, "app", "assets", "app_icon.icns")

# (chunk type, pixel size). The @2x types carry the same pixels at the same
# resolution — that is what "retina" means here, not a bigger image.
CHUNKS = [
    (b"ic11", 32),    # 16pt @2x
    (b"ic12", 64),    # 32pt @2x
    (b"ic07", 128),
    (b"ic13", 256),   # 128pt @2x
    (b"ic08", 256),
]


def best_image(path, size):
    """The .ico frame at `size`, or the largest one scaled down to it."""
    reader = QImageReader(path)
    best = None
    while True:
        img = reader.read()
        if not img.isNull():
            if img.width() == size:
                return img
            if best is None or img.width() > best.width():
                best = img
        if not reader.jumpToNextImage():
            break
    if best is None:
        raise SystemExit("no images in %s" % path)
    return best.scaled(QSize(size, size), Qt.IgnoreAspectRatio,
                       Qt.SmoothTransformation)


def png_bytes(image):
    buf = QBuffer(QByteArray())
    buf.open(QBuffer.WriteOnly)
    if not image.save(buf, "PNG"):
        raise SystemExit("PNG encode failed")
    return bytes(buf.data())


def main():
    if not os.path.isfile(SRC):
        raise SystemExit("missing source icon: %s" % SRC)
    # ⚠ BIND IT TO A NAME. `QGuiApplication.instance() or QGuiApplication([])`
    # leaves the new object unreferenced, Python collects it immediately, and
    # the next Qt call segfaults — measured, exit code 139.
    _app = QGuiApplication.instance() or QGuiApplication(sys.argv[:1])
    assert _app is not None

    body = b""
    for kind, size in CHUNKS:
        data = png_bytes(best_image(SRC, size))
        body += kind + struct.pack(">I", len(data) + 8) + data
        print("  %s  %4dpx  %6d bytes" % (kind.decode(), size, len(data)))

    blob = b"icns" + struct.pack(">I", len(body) + 8) + body
    with open(OUT, "wb") as fh:
        fh.write(blob)
    print("wrote %s (%d bytes, %d sizes)" % (OUT, len(blob), len(CHUNKS)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
