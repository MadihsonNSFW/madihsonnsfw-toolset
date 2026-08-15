# Blend file size, offscreen: the .blend block reader and the tree window.
#
#   python tests\app_blendsize_test.py
#
# Reads two REAL .blend files written by Blender 5.2 (tests\assets\tiny_scene*)
# — the same scene saved uncompressed and zstd-compressed. That pairing is the
# point of the assets: the two must produce byte-identical trees, and the
# uncompressed one must add up to EXACTLY its size on disk.
#
# ⚠ That exact-total check is the load-bearing test in this file. The 5.x block
# header was decoded by measurement, and the first (wrong) reading of it walked
# pointer values as block codes, invented millions of nonsense datablocks — and
# still finished on the final byte with a total that looked right. Only "the
# parts add up AND the codes are legible AND it reached ENDB" catches that.
import os
import struct
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

sys.path.insert(0, os.path.join(_ROOT, "app"))

from PySide6.QtCore import QEventLoop, QTimer, Qt  # noqa: E402
from PySide6.QtGui import QPixmap  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import blendsize  # noqa: E402
import config as configmod  # noqa: E402
import optimizer as optmod  # noqa: E402

PASS = []
FAIL = []


def ok(cond, label):
    (PASS if cond else FAIL).append(label)
    print(("ok   " if cond else "FAIL ") + label, flush=True)


ASSETS = os.path.join(_ROOT, "tests", "assets")
PLAIN = os.path.join(ASSETS, "tiny_scene.blend")
ZSTD = os.path.join(ASSETS, "tiny_scene_zstd.blend")

for path in (PLAIN, ZSTD):
    if not os.path.isfile(path):
        print("FAIL missing test asset %s" % path, flush=True)
        print("\n0 passed, 1 failed", flush=True)
        sys.exit(1)

app = QApplication.instance() or QApplication([])
configmod.save = lambda cfg: None       # never touch the real config.json

print("\n--- 1. formatting ---", flush=True)
ok(blendsize.human_bytes(0) == "0 B", "human_bytes: zero")
ok(blendsize.human_bytes(1023) == "1023 B", "human_bytes: bytes stay whole")
ok(blendsize.human_bytes(1536) == "1.5 KB", "human_bytes: KB")
ok(blendsize.human_bytes(2 * 1024 ** 3) == "2.0 GB", "human_bytes: GB")
ok(blendsize.format_version("0501") == "5.1", "version: 5.x four digits")
ok(blendsize.format_version("0502") == "5.2", "version: 5.2")
ok(blendsize.format_version("305") == "3.05", "version: legacy three digits")

print("\n--- 2. the uncompressed file adds up EXACTLY ---", flush=True)
plain = blendsize.scan(PLAIN)
parts = (sum(g["bytes"] for g in plain["types"])
         + sum(o["bytes"] for o in plain["overhead"]))
ok(plain["total_bytes"] == plain["disk_bytes"],
   "every byte of the file is accounted for (%d == %d)"
   % (plain["total_bytes"], plain["disk_bytes"]))
ok(parts == plain["total_bytes"],
   "the tree's parts sum to the whole (%d)" % parts)
ok(plain["complete"] is True, "the walk reached the closing ENDB block")
ok(plain["compression"] is None, "uncompressed file reported as uncompressed")
ok(plain["blender"] == "5.2", "Blender version read from the header")

print("\n--- 3. codes are legible, not pointer garbage ---", flush=True)
# The wrong-field-order failure produces block codes made of pointer bytes, so
# every kind comes back as an unknown four-character code.
known = set(blendsize.ID_NAMES.values())
kinds = [g["kind"] for g in plain["types"]]
ok(kinds and all(k in known for k in kinds),
   "every datablock type is a known one: %s" % ", ".join(kinds[:6]))
ok(plain["datablocks"] > 10,
   "found %d datablocks" % plain["datablocks"])

print("\n--- 4. compressed and uncompressed agree ---", flush=True)
zstd = blendsize.scan(ZSTD)
ok(zstd["compression"] == "zstd", "zstd container detected")
ok(zstd["total_bytes"] == plain["total_bytes"],
   "same data total through the decompressor")
ok(zstd["disk_bytes"] < plain["disk_bytes"],
   "the compressed file really is smaller on disk")
ok(zstd["ratio"] > 1.0, "ratio reported above 1")
plain_tree = [(g["kind"], g["bytes"], [i["name"] for i in g["items"]])
              for g in plain["types"]]
zstd_tree = [(g["kind"], g["bytes"], [i["name"] for i in g["items"]])
             for g in zstd["types"]]
ok(plain_tree == zstd_tree, "the two files produce an identical tree")

print("\n--- 5. datablocks are named and broken down ---", flush=True)
by_name = {}
for group in plain["types"]:
    for item in group["items"]:
        by_name[item["name"]] = (group["kind"], item)
for wanted in ("TinyBallMesh", "TinyMat", "TinyTex", "TinyBall"):
    ok(wanted in by_name, "found the datablock named %s" % wanted)
mesh = by_name.get("TinyBallMesh", (None, {}))[1]
labels = [part["label"] for part in mesh.get("parts") or []]
ok("Vertex groups" in labels,
   "the mesh's vertex groups are named in plain English")
image = by_name.get("TinyTex", (None, {}))[1]
ok(any("Packed file" == part["label"] for part in image.get("parts") or []),
   "the packed image's payload is identified")
ok(by_name.get("TinyTex", ("",))[0] == "Images",
   "the image is filed under Images")
ok(all(abs(sum(p["share"] for p in item.get("parts") or []) - 1.0) < 0.001
       for _kind, item in by_name.values() if item.get("parts")),
   "each datablock's parts sum to 100% of it")

print("\n--- 6. progress and cancelling ---", flush=True)
seen = []
blendsize.scan(PLAIN, progress=lambda done, total: seen.append((done, total)))
ok(all(0 <= done <= total for done, total in seen),
   "progress never reports past the end of the file")
ok(all(seen[i][0] <= seen[i + 1][0] for i in range(len(seen) - 1)),
   "progress only moves forwards")


class _Cancel:
    def __init__(self):
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return True


cancel = _Cancel()
try:
    blendsize.scan(PLAIN, should_cancel=cancel)
    cancelled = False
except blendsize.BlendSizeError as exc:
    cancelled = "ancel" in str(exc)
ok(cancelled, "should_cancel abandons the scan")

print("\n--- 7. files it must refuse ---", flush=True)
tmp = tempfile.mkdtemp(prefix="madi_blendsize_")
junk = os.path.join(tmp, "notablend.blend")
with open(junk, "wb") as handle:
    handle.write(b"this is not a blend file at all, not even close")
try:
    blendsize.scan(junk)
    refused = False
except blendsize.BlendSizeError:
    refused = True
ok(refused, "a file that is not a .blend is refused with a clear error")

missing = os.path.join(tmp, "gone.blend")
try:
    blendsize.scan(missing)
    handled = False
except (blendsize.BlendSizeError, OSError):
    handled = True
ok(handled, "a missing file does not crash the reader")

truncated = os.path.join(tmp, "cut.blend")
with open(PLAIN, "rb") as src:
    body = src.read()
with open(truncated, "wb") as handle:
    handle.write(body[:len(body) // 2])
cut = blendsize.scan(truncated)
ok(cut["complete"] is False,
   "a truncated file is reported as incomplete rather than as whole")
ok(cut["types"], "a truncated file still reports what it did read")

print("\n--- 8. the header forms ---", flush=True)


class _Bytes:
    def __init__(self, data):
        self.data = data
        self.at = 0

    def read(self, count):
        chunk = self.data[self.at:self.at + count]
        self.at += len(chunk)
        return chunk


pointer, endian, version, form, size = blendsize.read_header(
    _Bytes(b"BLENDER-v305" + b"\0" * 8))
ok((form, size, pointer, endian) == ("legacy", 12, 8, "<"),
   "the legacy 12-byte header still reads")
pointer, endian, version, form, size = blendsize.read_header(
    _Bytes(b"BLENDER17-01v0501" + b"\0" * 8))
ok((form, size, pointer, endian, version) == ("5.x", 17, 8, "<", "0501"),
   "the 5.x 17-byte header reads, and its length comes from the header itself")
refused = False
try:
    blendsize.read_header(_Bytes(b"NOTBLEND1234"))
except blendsize.BlendSizeError:
    refused = True
ok(refused, "a bad magic is refused by the header reader")

# ⚠ The measured field order, pinned. `len` at +16 AFTER the pointer; reading
# it at +8 is the mistake that still produced a plausible-looking total.
head = blendsize._bhead_for("5.x", "<", 8)[0]
ok(head.size == 32, "the 5.x block header is 32 bytes")
packed = head.pack(b"DATA", 7, 0xDEADBEEF, 1234, 1)
_code, _sdna, _old, length, _nr = head.unpack(packed)
ok(length == 1234, "length is read from the field that really holds it")
ok(struct.unpack_from("<q", packed, 8)[0] != 1234,
   "and NOT from +8, which holds the old pointer")

class StubWindow:
    capturing = False
    _previewing = False

    def __init__(self, blend=""):
        self.cfg = {"optimizer": dict(configmod.DEFAULTS["optimizer"])}
        self._connected_file = blend
        self.captures = 0

    def bridge_free_for_tools(self):
        return True

    def begin_capture(self, label, verb="capturing"):
        self.captures += 1

    def end_capture(self):
        self.captures = max(0, self.captures - 1)


class StubBridge:
    def feature_reason(self, _feature):
        return "the add-on is too old"       # must NOT gate this tool


def settle(tool, timeout=60.0):
    loop = QEventLoop()
    end = time.time() + timeout
    timer = QTimer()
    timer.timeout.connect(
        lambda: (not tool._workers or time.time() > end) and loop.quit())
    timer.start(30)
    loop.exec()
    timer.stop()


print("\n--- 9. the tree fills, IN THE TOOL ---", flush=True)
# ⚠ It was a QDialog for one afternoon. Marty asked for it in the same window,
# so there is no separate window class any more - these assertions are on the
# tool itself, and a `BlendSizeWindow` coming back would fail the check below.
ok(not hasattr(optmod, "BlendSizeWindow"),
   "there is no separate window class - the tree lives in the tool")

tool = optmod.FileSizeTool(StubBridge(), StubWindow(PLAIN))
ok(tool.isEnabled(),
   "the tool is not gated by the add-on version - it never uses the bridge")
tool.measure(ZSTD)
settle(tool)
ok(tool.tree.topLevelItemCount() > 3,
   "the tree has %d top-level rows" % tool.tree.topLevelItemCount())
ok(tool.bar.isHidden(), "the progress bar goes away when the scan ends")
ok("on disk" in tool.head.text(), "the header line names both sizes")
ok("compress" in tool.note.text(),
   "the note explains why the totals differ from Explorer")
top = tool.tree.topLevelItem(0)
ok(top.data(2, Qt.UserRole) is not None,
   "a share is attached for the bar delegate to paint")
ok(tool.tree.topLevelItem(
    tool.tree.topLevelItemCount() - 1).text(0).startswith("The file's own"),
   "the file's own bookkeeping is shown, and shown last")
ok("(" in top.text(0) and ")" in top.text(0),
   "a type row carries its datablock count in its name, not in the % column")
ok(top.text(3).endswith("%"),
   "...and the last column is a percentage on every row")

print("\n--- 10. the third level is lazy ---", flush=True)
child = None
for index in range(tool.tree.topLevelItemCount()):
    node = tool.tree.topLevelItem(index)
    for kid in range(node.childCount()):
        candidate = node.child(kid)
        if candidate.childCount() == 1 and \
                candidate.child(0).text(0) == optmod.FileSizeTool.LAZY:
            child = candidate
            break
    if child is not None:
        break
ok(child is not None, "datablocks start with a placeholder child")
if child is not None:
    child.setExpanded(True)
    ok(child.childCount() >= 1, "expanding builds the real contents")
    ok(all(child.child(i).text(0) != optmod.FileSizeTool.LAZY
           for i in range(child.childCount())),
       "the placeholder is gone once expanded")
    ok(all(child.child(i).text(1) for i in range(child.childCount())),
       "every part row carries a size")

print("\n--- 11. the share bar paints ---", flush=True)
tool.resize(900, 600)
tool.show()
pixmap = QPixmap(900, 600)
painted = True
try:
    tool.tree.render(pixmap)
except Exception as exc:                # noqa: BLE001
    painted = False
    print("   render raised: %s" % exc, flush=True)
ok(painted, "the tree renders with the share-bar delegate attached")

print("\n--- 12. the buttons ---", flush=True)
tool.apply_status({})
ok(tool.btn_open.isEnabled(), "the open-file button is live when a file is set")
ok(tool.blend_path() == PLAIN, "the path comes from the health poll's file")

empty = optmod.FileSizeTool(StubBridge(), StubWindow(""))
empty.apply_status({})
ok(not empty.btn_open.isEnabled(),
   "with no open .blend the button is disabled rather than failing on a press")
ok("Choose a file" in empty.status.text(), "and it says what to do instead")

busy = StubWindow(PLAIN)
tool2 = optmod.FileSizeTool(StubBridge(), busy)
tool2.measure(PLAIN)
settle(tool2)
ok(busy.captures == 0,
   "a scan does NOT grey the app out - nothing here touches Blender's thread")

missing_tool = optmod.FileSizeTool(StubBridge(),
                                   StubWindow(os.path.join(tmp, "nope.blend")))
missing_tool.apply_status({})
missing_tool.measure_open()
ok("not readable" in missing_tool.status.text(),
   "a path the app cannot reach is reported, not opened")

print("\n--- 13. leaks and leftovers ---", flush=True)
# ⚠ Every check here is a COUNTERFACTUAL: it is only worth anything if it
# would fail on the code before it. The MadiRef leak hunt taught that the hard
# way - the first "after" measurement was wrong because Qt6 dropped the
# processEvents DeferredDeletion flag, so objects already scheduled to die
# still counted as alive.
import gc  # noqa: E402
import shutil  # noqa: E402
import threading  # noqa: E402

from PySide6.QtCore import QCoreApplication, QEvent  # noqa: E402


def flush_deletes():
    """Qt6 has no processEvents(DeferredDeletion) - this is the replacement."""
    app.processEvents()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()
    gc.collect()


def wait_idle(tool, timeout=60.0):
    loop = QEventLoop()
    end = time.time() + timeout
    timer = QTimer()
    timer.timeout.connect(
        lambda: (not tool._workers or time.time() > end) and loop.quit())
    timer.start(20)
    loop.exec()
    timer.stop()
    flush_deletes()


leak_tool = optmod.FileSizeTool(StubBridge(), StubWindow(PLAIN))
threads_before = threading.active_count()

for _round in range(4):
    leak_tool.measure(PLAIN)
    wait_idle(leak_tool)

ok(leak_tool.tree.topLevelItemCount() > 3, "four scans in a row all landed")
ok(not leak_tool._workers,
   "no worker is still held after the scans finished (%d)"
   % len(leak_tool._workers))
live_workers = [o for o in gc.get_objects()
                if type(o).__name__ == "_ScanWorker"]
ok(len(live_workers) <= 1,
   "workers are not accumulating: %d alive after 4 scans" % len(live_workers))
ok(threading.active_count() <= threads_before,
   "no reader threads left running (%d -> %d)"
   % (threads_before, threading.active_count()))
ok(leak_tool._summary and "path" not in str(type(leak_tool._summary.get(
    "types", ""))), "the full scan result is not retained after filling")
ok("types" not in leak_tool._summary,
   "only a small summary is kept, not every datablock")

# The conclusive one on Windows: a file with an open handle CANNOT be deleted.
# If the reader leaked its file object or its decompressor, this fails.
handle_dir = tempfile.mkdtemp(prefix="madi_blendsize_handle_")
copy = os.path.join(handle_dir, "held.blend")
shutil.copyfile(ZSTD, copy)
leak_tool.measure(copy)
wait_idle(leak_tool)
try:
    os.remove(copy)
    released = True
    why = ""
except OSError as exc:
    released = False
    why = str(exc)
ok(released, "the scanned file can be deleted afterwards - no handle left open"
   + (" (%s)" % why if why else ""))

# ...and the same after a CANCELLED scan, which takes a different path out of
# the reader and so releases the handle in a different place.
shutil.copyfile(ZSTD, copy)
leak_tool.measure(copy)
leak_tool.stop()
wait_idle(leak_tool)
try:
    os.remove(copy)
    released = True
    why = ""
except OSError as exc:
    released = False
    why = str(exc)
ok(released, "a CANCELLED scan releases the file too" +
   (" (%s)" % why if why else ""))
ok(not leak_tool._workers, "a cancelled worker is retired, not stranded")

# Switching away from the tool (another tool, another tab) must end a scan in
# flight. ⚠ It has to be SHOWN first or `hide()` is a no-op and there is no
# hide event at all - which is how this check first passed for the wrong
# reason.
leak_tool.show()
app.processEvents()
leak_tool.measure(PLAIN)
leak_tool.hide()
ok(all(worker._cancel for worker in leak_tool._workers) or
   not leak_tool._workers,
   "switching away from the tool cancels a scan in flight")
wait_idle(leak_tool)
ok(threading.active_count() <= threads_before,
   "and its thread is gone (%d)" % threading.active_count())
# ⚠ ...but a MINIMISE must not, and that branch CANNOT be driven from Python:
# `spontaneous()` is set by the event system and there is no way to forge it,
# so a behavioural check here would pass whatever the code did. Asserted
# against the BYTECODE instead - `co_names`, not the source text, because a
# grep would match the comment that explains the guard (the same trap that
# broke three absence checks on 2026-08-12).
ok("spontaneous" in optmod.FileSizeTool.hideEvent.__code__.co_names,
   "hideEvent consults spontaneous(), so a minimise cannot cancel a scan")

# Rebuilding the tool must not strand anything either.
count_before = len([o for o in gc.get_objects()
                    if type(o).__name__ == "_ScanWorker"])
throwaway = optmod.FileSizeTool(StubBridge(), StubWindow(PLAIN))
throwaway.measure(PLAIN)
throwaway.stop()
wait_idle(throwaway)
throwaway.deleteLater()
flush_deletes()
count_after = len([o for o in gc.get_objects()
                   if type(o).__name__ == "_ScanWorker"])
ok(count_after <= count_before,
   "a discarded tool leaves no worker behind (%d -> %d)"
   % (count_before, count_after))
shutil.rmtree(handle_dir, ignore_errors=True)

print("\n%d passed, %d failed" % (len(PASS), len(FAIL)), flush=True)
sys.exit(1 if FAIL else 0)
