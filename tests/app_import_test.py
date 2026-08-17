# Importing into the library, and the picker thumbnail that shows its buttons
# (Marty, 2026-08-05).
#
#   python tests\app_import_test.py
#
# The importer is deliberately Qt-free and type-agnostic, so most of this is
# plain disk work. The two halves that MATTER are the ones a shared zip can get
# wrong: entry names written with backslashes (PowerShell 5.1's
# Compress-Archive does exactly that) and entries that try to climb out of the
# destination.
import io
import json
import os
import shutil
import sys
import tempfile
import zipfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

sys.path.insert(0, os.path.join(_ROOT, "app"))

from PySide6.QtGui import QColor, QImage, QPainter  # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

import config  # noqa: E402

TMP = tempfile.mkdtemp(prefix="madi_imp_")
config.APP_DIR = TMP
# DATA_DIR is the WRITABLE root (macOS splits it off APP_DIR); the
# caches, queues and presets read it, so redirecting only APP_DIR
# would build them in the real dist folder.
config.DATA_DIR = TMP
config.CONFIG_PATH = os.path.join(TMP, "config.json")

# ⚠ A DEAD PORT, DELIBERATELY. With no config file the DEFAULTS apply, and the
# default port is the REAL bridge port - so this suite connected to whatever
# Blender Marty happened to have open and measured a different app. On
# 2026-08-15 that turned three suites red for a reason unrelated to the change
# under test: the live add-on was a version behind, so the status bar grew a
# 172 px "Update add-on" button and the window's minimum width went 632 -> 810.
_io_ = __import__("io")
_json_ = __import__("json")
_io_.open(config.CONFIG_PATH, "w", encoding="utf-8").write(
    _json_.dumps({"port": 9998}))
config.DEFAULT_LIBRARY = os.path.join(TMP, "library")
config.DEFAULTS["libraries"] = [{"name": "Test", "path": config.DEFAULT_LIBRARY}]
os.makedirs(config.DEFAULT_LIBRARY, exist_ok=True)

import importer  # noqa: E402
import library as librarymod  # noqa: E402
import picker as pickermod  # noqa: E402

PASS, FAIL = [], []


def ok(cond, label):
    (PASS if cond else FAIL).append(label)
    print(("ok   " if cond else "FAIL ") + label, flush=True)


app = QApplication.instance() or QApplication([])


def make_item(root, folder, name, typ, payload=None, thumb=False):
    """A believable library item on disk: payload json (+ a thumbnail)."""
    path = os.path.join(root, folder, "%s.%s" % (name, typ))
    os.makedirs(path, exist_ok=True)
    data = payload if payload is not None else {"type": typ, "metadata": {}}
    fname = librarymod.DATA_FILES.get(typ, "%s.json" % typ)
    with open(os.path.join(path, fname), "w", encoding="utf-8") as f:
        json.dump(data, f)
    if thumb:
        img = QImage(64, 64, QImage.Format_RGB32)
        img.fill(QColor("#334455"))
        img.save(os.path.join(path, "thumbnail.jpg"), "JPEG", 90)
    return path


SRC = os.path.join(TMP, "source")
os.makedirs(SRC, exist_ok=True)

# ============================================================ discovery =====
make_item(SRC, "", "Wave", "anim", thumb=True)
make_item(SRC, "Lily", "Smile", "pose")
make_item(SRC, "Lily/Face", "Blink", "pose")
open(os.path.join(SRC, "loose.abc"), "wb").write(b"not really alembic")
open(os.path.join(SRC, "notes.txt"), "w").write("hello")

found, ignored = importer.scan([SRC])
names = sorted(c.name for c in found)
ok(names == ["Blink", "Smile", "Wave", "loose"],
   "scan: a folder yields every item at any depth, plus loose .abc (%r)" % names)
by_name = {c.name: c for c in found}
ok(by_name["Blink"].relfolder == "Lily/Face",
   "scan: the relative folder is remembered, so structure survives the import")
ok(by_name["loose"].kind == "bare" and by_name["loose"].type == "",
   "scan: a loose .abc is a BARE item, not a folder")
ok(all(c.type != "txt" for c in found), "scan: a stray .txt is not an item")

single = importer.scan([os.path.join(SRC, "Wave.anim")])[0]
ok(len(single) == 1 and single[0].type == "anim",
   "scan: pointing straight at an item folder imports that one item")

_f, ign = importer.scan([os.path.join(SRC, "notes.txt")])
ok(ign and "not a library item" in ign[0][1],
   "scan: something it cannot use is reported with a reason, not dropped")

# ================================================================= copy =====
LIB = os.path.join(TMP, "lib1")
os.makedirs(LIB, exist_ok=True)
report = importer.run(found, LIB)
ok(len(report["imported"]) == 4, "run: everything landed (%d)"
   % len(report["imported"]))
ok(os.path.isdir(os.path.join(LIB, "Wave.anim")),
   "run: an item keeps its extension")
ok(os.path.isfile(os.path.join(LIB, "Wave.anim", "thumbnail.jpg")),
   "⚠ run: THE THUMBNAIL CAME WITH IT - the whole item folder is copied, which "
   "is why 'everything just works including thumbnails' needs no per-type code")
ok(os.path.isdir(os.path.join(LIB, "Lily", "Face", "Blink.pose")),
   "run: the folder structure is recreated")
ok(os.path.isfile(os.path.join(LIB, "loose.abc")), "run: bare files too")
ok(report["types"].get("pose") == 2 and report["types"].get("anim") == 1,
   "run: the report counts by type (%r)" % report["types"])

folders, items = librarymod.scan(LIB)
ok(len(items) == 4 and {i.type for i in items} == {"anim", "pose", "abc"},
   "the library's OWN scanner sees exactly what was imported (%r)"
   % sorted(i.type for i in items))

# --- nothing is ever overwritten -------------------------------------------
report2 = importer.run(found, LIB)
ok(os.path.isdir(os.path.join(LIB, "Wave 2.anim")),
   "⚠ re-importing NUMBERS the clash instead of replacing - an import is "
   "someone else's data arriving, and silently eating your own item is the one "
   "outcome that cannot be undone")
ok(report2["renamed"], "and the report says which ones were renamed")
ok("renamed to avoid replacing" in report2["summary"],
   "...in the summary too (%r)" % report2["summary"])

# ================================================================== zip =====
ZIPS = os.path.join(TMP, "zips")
os.makedirs(ZIPS, exist_ok=True)

# The shape our OWN "Zip for sharing" writes: item folders at the top level.
share = os.path.join(ZIPS, "share.zip")
with zipfile.ZipFile(share, "w") as z:
    for root, _dirs, files in os.walk(os.path.join(SRC, "Wave.anim")):
        for name in files:
            full = os.path.join(root, name)
            rel = os.path.relpath(full, SRC).replace(os.sep, "/")
            z.write(full, rel)
    z.write(os.path.join(SRC, "loose.abc"), "loose.abc")

cands, _ig = importer.scan([share])
ok(sorted(c.name for c in cands) == ["Wave", "loose"],
   "zip: our own share zip is read back item-for-item")
LIB2 = os.path.join(TMP, "lib2")
os.makedirs(LIB2, exist_ok=True)
importer.run(cands, LIB2)
ok(os.path.isfile(os.path.join(LIB2, "Wave.anim", "anim.json"))
   and os.path.isfile(os.path.join(LIB2, "Wave.anim", "thumbnail.jpg")),
   "zip: the item comes out whole, thumbnail and all")
ok(os.path.isfile(os.path.join(LIB2, "loose.abc")), "zip: and the bare file")

# --- ⚠ BACKSLASH ENTRY NAMES ------------------------------------------------
# PowerShell 5.1's Compress-Archive writes `a\b\c.json`, and Python's zipfile
# treats that as ONE flat filename. A zip Marty made that way would have
# imported as garbage names rather than items.
backslash = os.path.join(ZIPS, "ps51.zip")
with zipfile.ZipFile(backslash, "w") as z:
    z.writestr("Lily\\Smile.pose\\pose.json", json.dumps({"type": "pose"}))
    z.writestr("Lily\\Smile.pose\\thumbnail.jpg", b"\xff\xd8\xff")
cands, _ig = importer.scan([backslash])
ok(len(cands) == 1 and cands[0].name == "Smile"
   and cands[0].relfolder == "Lily",
   "⚠ zip: BACKSLASH entry names are read as folders (PowerShell 5.1 writes "
   "them) — %r" % [(c.name, c.relfolder) for c in cands])
LIB3 = os.path.join(TMP, "lib3")
os.makedirs(LIB3, exist_ok=True)
importer.run(cands, LIB3)
ok(os.path.isfile(os.path.join(LIB3, "Lily", "Smile.pose", "pose.json")),
   "...and extracted into real folders")

# --- ⚠ PATH TRAVERSAL -------------------------------------------------------
evil = os.path.join(ZIPS, "evil.zip")
with zipfile.ZipFile(evil, "w") as z:
    z.writestr("../../escaped.pose/pose.json", "{}")
    z.writestr("../evil.txt", "no")
    z.writestr("Good.pose/pose.json", json.dumps({"type": "pose"}))
cands, _ig = importer.scan([evil])
ok([c.name for c in cands] == ["Good"],
   "⚠ zip: an entry climbing out with '..' is refused outright (%r)"
   % [c.name for c in cands])
LIB4 = os.path.join(TMP, "lib4")
os.makedirs(LIB4, exist_ok=True)
importer.run(cands, LIB4)
outside = os.path.abspath(os.path.join(LIB4, "..", "escaped.pose"))
ok(not os.path.exists(outside),
   "...and nothing was written outside the library (%s)" % outside)

# --- a zip with nothing in it ----------------------------------------------
empty = os.path.join(ZIPS, "empty.zip")
with zipfile.ZipFile(empty, "w") as z:
    z.writestr("readme.txt", "hi")
_c, ign = importer.scan([empty])
ok(ign and "no library items in this zip" in ign[0][1],
   "zip: one with nothing usable says so")
broken = os.path.join(ZIPS, "broken.zip")
open(broken, "wb").write(b"PK not really")
_c, ign = importer.scan([broken])
ok(ign and "not a readable zip" in ign[0][1],
   "a corrupt zip is reported, not raised")

# --- EVERY item type round-trips through a zip ------------------------------
ALL = os.path.join(TMP, "alltypes")
os.makedirs(ALL, exist_ok=True)
for ext in librarymod.ITEM_EXTS:
    make_item(ALL, "", "Thing_" + ext[1:], ext[1:])
alltypes = os.path.join(ZIPS, "all.zip")
with zipfile.ZipFile(alltypes, "w") as z:
    for root, _dirs, files in os.walk(ALL):
        for name in files:
            full = os.path.join(root, name)
            z.write(full, os.path.relpath(full, ALL).replace(os.sep, "/"))
cands, _ig = importer.scan([alltypes])
ok(len(cands) == len(librarymod.ITEM_EXTS),
   "⚠ EVERY item type imports, because nothing here knows about types — %d of "
   "%d" % (len(cands), len(librarymod.ITEM_EXTS)))
LIB5 = os.path.join(TMP, "lib5")
os.makedirs(LIB5, exist_ok=True)
rep = importer.run(cands, LIB5)
_f, items = librarymod.scan(LIB5)
ok(len(items) == len(librarymod.ITEM_EXTS),
   "...and the library scanner finds them all afterwards")
ok(not rep["failed"], "...with nothing failing")

# --- destination folder -----------------------------------------------------
LIB6 = os.path.join(TMP, "lib6")
os.makedirs(LIB6, exist_ok=True)
cands, _ig = importer.scan([os.path.join(SRC, "Wave.anim")])
importer.run(cands, LIB6, dest_folder="Shots/A")
ok(os.path.isdir(os.path.join(LIB6, "Shots", "A", "Wave.anim")),
   "run: the destination folder is honoured and created")

# ============================================== the picker thumbnail =========
# Marty: "When saving bone picker we need to make sure the buttons are visible
# in the preview thumbnail."
PICK = make_item(TMP, "", "Body", "picker", payload={
    "type": "picker", "format": "madi_picker_preset", "buttons": [
        {"kind": "BONE", "x": 0.25, "y": 0.75, "w": 0.10, "h": 0.10,
         "scale": 1.0, "color": [1.0, 0.0, 0.0], "label": "a"},
        {"kind": "GROUP", "x": 0.75, "y": 0.25, "w": 0.12, "h": 0.12,
         "scale": 1.0, "color": [0.0, 1.0, 0.0], "label": "b"},
    ]})
img = QImage(256, 256, QImage.Format_RGB32)
img.fill(QColor("#202020"))
img.save(os.path.join(PICK, "thumbnail.jpg"), "JPEG", 95)

ok(pickermod.compose_thumbnail(PICK), "picker: the thumbnail is composed")
ok(os.path.isfile(os.path.join(PICK, pickermod.REFERENCE_FILE)),
   "⚠ picker: the CLEAN picture is kept as reference.jpg - composing onto the "
   "composite would paint buttons on buttons, a little more opaque each time")
out = QImage(os.path.join(PICK, "thumbnail.jpg"))


def sample(image, cx, cy):
    return image.pixelColor(int(cx * image.width()),
                            int((1.0 - cy) * image.height()))


red = sample(out, 0.25, 0.75)
green = sample(out, 0.75, 0.25)
empty = sample(out, 0.50, 0.50)
ok(red.red() > 120 and red.green() < 90,
   "picker: the BONE button is drawn where the layout says (%s)" % red.name())
ok(green.green() > 120 and green.red() < 90,
   "picker: and the GROUP button too (%s)" % green.name())
ok(abs(empty.red() - 0x20) < 24 and abs(empty.green() - 0x20) < 24,
   "picker: empty canvas is left as the reference picture (%s)" % empty.name())
ok(sample(out, 0.25, 0.25).red() < 90,
   "⚠ picker: the y axis is NOT flipped by accident - canvas y is UP, Qt's is "
   "DOWN, and a missed flip would mirror every layout")

# ⚠ idempotence: 📷 Update Preview runs this again on an item that already has
# a composed thumbnail.
before = sample(QImage(os.path.join(PICK, "thumbnail.jpg")), 0.25, 0.75)
pickermod.compose_thumbnail(PICK)
pickermod.compose_thumbnail(PICK)
after = sample(QImage(os.path.join(PICK, "thumbnail.jpg")), 0.25, 0.75)
ok(abs(before.red() - after.red()) < 12
   and abs(before.green() - after.green()) < 12,
   "⚠ picker: composing three times looks like composing once (%s vs %s)"
   % (before.name(), after.name()))

NOBUT = make_item(TMP, "", "Empty", "picker",
                  payload={"type": "picker", "buttons": []})
ok(not pickermod.compose_thumbnail(NOBUT),
   "picker: a layout with no buttons composes nothing rather than raising")
NOPIC = make_item(TMP, "", "NoPic", "picker", payload={
    "type": "picker", "buttons": [{"kind": "BONE", "x": 0.5, "y": 0.5,
                                   "w": 0.1, "h": 0.1, "scale": 1.0,
                                   "color": [1, 1, 1]}]})
ok(not pickermod.compose_thumbnail(NOPIC),
   "picker: a tab with no reference picture is a no-op, not a crash")
ok(pickermod.compose_thumbnail(os.path.join(TMP, "nope.picker")) is False,
   "picker: a missing item is a no-op too")

ok("reference.jpg" in librarymod._PAYLOAD_FILES,
   "⚠ reference.jpg is a VERSIONED payload - a version that kept only the "
   "composite could never be redrawn")

# ================================================= the dialog + button ======
import main as mainmod  # noqa: E402

win = mainmod.MainWindow()
view = win.tabs.currentWidget()
dialog = mainmod.ImportDialog(view, config.DEFAULT_LIBRARY, [], "")
ok(not dialog.buttons.button(dialog.buttons.StandardButton.Ok).isEnabled()
   if hasattr(dialog.buttons, "StandardButton") else True,
   "dialog: Import is off with nothing added")
dialog.add([share])
ok(dialog.tree.topLevelItemCount() == 2,
   "dialog: adding a zip lists what is inside it before anything is copied")
ok("2 item(s) ready" in dialog.note.text(),
   "dialog: and says how many (%r)" % dialog.note.text())
dialog.add([os.path.join(SRC, "notes.txt")])
ok("Ignoring" in dialog.note.text(),
   "dialog: says what it will NOT import, rather than dropping it quietly")
dialog.clear()
ok(dialog.tree.topLevelItemCount() == 0 and not dialog.candidates,
   "dialog: Clear empties it")

bar_buttons = [b for b in view.findChildren(type(win.tabs.cornerWidget()))
               if b.text().endswith("Import")]
ok(bar_buttons and bar_buttons[0].objectName() == "accent",
   "the toolbar's Import button is there and carries the accent (blue) style")

print("\n%d passed, %d failed" % (len(PASS), len(FAIL)), flush=True)
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if FAIL else 0)
