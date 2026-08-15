# Zipping library items for sharing (Marty, 2026-08-04: "Ability to mass select
# multiple items in Studio Library and right clikc > zip them for sharing").
#
#   python tests\app_zip_test.py
#
# Real files in a temp folder, a real archive on disk. The point of the feature
# is the bytes that come out, so nothing here is mocked except the file dialog.
import os
import sys
import tempfile
import zipfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

sys.path.insert(0, os.path.join(_ROOT, "app"))

from PySide6.QtWidgets import QApplication, QFileDialog  # noqa: E402

PASS, FAIL = [], []


def ok(cond, label):
    (PASS if cond else FAIL).append(label)
    print(("ok   " if cond else "FAIL ") + label, flush=True)


app = QApplication.instance() or QApplication([])

import main as mainmod  # noqa: E402

TMP = tempfile.mkdtemp(prefix="madi_zip_")
LIB = os.path.join(TMP, "library")
os.makedirs(LIB)


def write(path, text="x"):
    folder = os.path.dirname(path)
    if folder and not os.path.isdir(folder):
        os.makedirs(folder)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


# Two folder items (the normal shape) and one bare file (a loose .abc).
pose = os.path.join(LIB, "wave")
write(os.path.join(pose, "pose.json"), "{}")
write(os.path.join(pose, "thumbnail.jpg"), "jpg")
write(os.path.join(pose, "versions", "v1.json"), "{}")

anim = os.path.join(LIB, "walk")
write(os.path.join(anim, "anim.json"), "{}")

loose = write(os.path.join(LIB, "cache.abc"), "abc")


class Item:
    def __init__(self, name, path, bare=False):
        self.name, self.path, self.bare = name, path, bare


class StubGrid:
    def __init__(self, items):
        self._items = items

    def selected_library_items(self):
        return list(self._items)


class StubWindow:
    def __init__(self):
        self.messages = []

    def statusBar(self):
        return self

    def showMessage(self, text, _ms=0):
        self.messages.append(text)


class Harness(mainmod.LibraryView):
    """Only zip_items is exercised - the rest of LibraryView needs a bridge, a
    real library scan and a live window, none of which this feature touches."""

    def __init__(self, items):
        self.grid = StubGrid(items)
        self.window = StubWindow()
        self.lib_cfg = {"name": "testlib"}


ITEMS = [Item("wave", pose), Item("walk", anim),
         Item("cache.abc", loose, bare=True)]
view = Harness(ITEMS)

OUT = os.path.join(TMP, "share.zip")
QFileDialog.getSaveFileName = staticmethod(lambda *a, **k: (OUT, "Zip (*.zip)"))

view.zip_items()
ok(os.path.isfile(OUT), "zip: an archive is written where the user asked")

with zipfile.ZipFile(OUT) as z:
    names = sorted(z.namelist())
    ok("wave/pose.json" in names,
       "zip: a folder item keeps its own folder, so unzipping into a library "
       "puts the item back as an item (%s)" % names)
    ok("wave/versions/v1.json" in names,
       "zip: including everything inside it - versions travel with the item")
    ok("walk/anim.json" in names, "zip: every SELECTED item is in, not just "
       "the one that was right-clicked")
    ok("cache.abc" in names,
       "zip: a bare file goes in at the top level - it has no folder to keep")
    ok(not any(n.startswith("/") or ".." in n for n in names),
       "zip: no absolute or climbing paths, so it cannot unzip outside its "
       "target folder")
    ok(z.read("wave/pose.json") == b"{}",
       "zip: and the bytes are the real ones")

ok(os.path.isdir(pose) and os.path.isfile(loose),
   "zip: READ-ONLY - the library is untouched, nothing is moved or consumed")
ok(any("3 item" in m for m in view.window.messages),
   "zip: the count is reported (%s)" % view.window.messages)

# An item whose folder has gone is NAMED, not silently dropped: "3 of 4" with
# no names leaves someone guessing what is missing from a zip already sent.
view2 = Harness([Item("wave", pose), Item("ghost", os.path.join(LIB, "gone"))])
OUT2 = os.path.join(TMP, "partial.zip")
QFileDialog.getSaveFileName = staticmethod(lambda *a, **k: (OUT2, "Zip (*.zip)"))
view2.zip_items()
ok(os.path.isfile(OUT2), "zip: a missing item does not abort the whole archive")
ok(any("ghost" in m for m in view2.window.messages),
   "zip: and the one that was skipped is named (%s)" % view2.window.messages)

# Cancelling the dialog must write nothing at all.
QFileDialog.getSaveFileName = staticmethod(lambda *a, **k: ("", ""))
before = set(os.listdir(TMP))
Harness(ITEMS).zip_items()
ok(set(os.listdir(TMP)) == before, "zip: cancelling writes no file")

# The extension is added when the user leaves it off.
OUT3 = os.path.join(TMP, "noext")
QFileDialog.getSaveFileName = staticmethod(lambda *a, **k: (OUT3, "Zip (*.zip)"))
Harness(ITEMS).zip_items()
ok(os.path.isfile(OUT3 + ".zip"),
   "zip: a name typed without .zip still gets one")

import shutil  # noqa: E402
shutil.rmtree(TMP, ignore_errors=True)

print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
for label in FAIL:
    print("  FAILED: " + label)
sys.exit(1 if FAIL else 0)
