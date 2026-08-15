# Library auto-refresh (gear Library Settings, global, default OFF): the config
# default, the settings dialog round-trip, what the QFileSystemWatcher does and
# does NOT watch, teardown, and the debounced rescan actually picking up a new
# item while keeping the user's selection.
# Offscreen Qt against a throwaway library — never touches the real one.
import os
import shutil
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

sys.path.insert(0, os.path.join(_ROOT, "app"))

from PySide6.QtCore import Qt, QEventLoop, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

PASS = []
FAIL = []


def ok(cond, label):
    (PASS if cond else FAIL).append(label)
    print(("ok   " if cond else "FAIL ") + label, flush=True)


def pump(ms):
    """Run the event loop for ms — the watcher and its debounce are async."""
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


import config  # noqa: E402
import main as app  # noqa: E402

qapp = QApplication.instance() or QApplication([])

# ------------------------------------------------------------- the default --
ok(config.DEFAULTS.get("auto_refresh") is False,
   "auto_refresh ships OFF by default")
ok("auto_refresh" in config.load(),
   "a config.json written before the key existed still gets it")

# ------------------------------------------------------- a scratch library --
root = tempfile.mkdtemp(prefix="madi_ar_")
os.makedirs(os.path.join(root, "Poses", "Face"))
os.makedirs(os.path.join(root, "walk.anim"))
os.makedirs(os.path.join(root, "Poses", "smile.pose"))

win = app.MainWindow()
win.cfg["auto_refresh"] = False
# fake mp4s below would make the video-preview decoder log noise
win.video_previews = None
lib = {"name": "Scratch", "path": root}
view = app.LibraryView(lib, win.bridge, win)
win.tabs.addTab(view, "Scratch")

ok(len(view.items) == 2, "scratch library scans its 2 items")
ok(view._watcher is None, "no watcher exists while auto-refresh is off")

# --------------------------------------------------- the settings dialog --
dlg = app.LibrarySettingsDialog(win, win.cfg)
ok(dlg.chk_auto.isChecked() is False, "dialog opens reflecting the saved value")
dlg.chk_auto.setChecked(True)
# The dialog carries every library-wide setting (dev_console joined it), so
# assert on the key rather than the whole dict — adding a setting must not
# break this test.
ok(dlg.values().get("auto_refresh") is True, "dialog returns the new value")
ok("dev_console" in dlg.values(),
   "dialog also carries the other library-wide settings")

# --------------------------------------------------------------- switch on --
win.cfg["auto_refresh"] = True
win.apply_auto_refresh()
ok(view._watcher is not None, "turning it on creates the watcher")

watched = {os.path.normcase(os.path.normpath(p))
           for p in view._watcher.directories()}
ok(os.path.normcase(os.path.normpath(root)) in watched,
   "the library root is watched")
ok(os.path.normcase(os.path.join(root, "Poses")) in watched
   and os.path.normcase(os.path.join(root, "Poses", "Face")) in watched,
   "navigation folders are watched, nested ones too")
ok(os.path.normcase(os.path.join(root, "walk.anim")) not in watched,
   "item folders are NOT watched (their insides churn on every save)")

# ------------------------------------------ a change on disk gets picked up --
os.makedirs(os.path.join(root, "Poses", "hero.pose"))
pump(2500)
ok(len(view.items) == 3, "an item added outside the app triggers a rescan")
ok(any(i.name == "hero" for i in view.items), "…and the new item is in the list")

names = {os.path.normcase(os.path.normpath(p))
         for p in view._watcher.directories()}
ok(os.path.normcase(os.path.join(root, "Poses", "hero.pose")) not in names,
   "the rescan re-arms the watch list without adding the new item folder")

# ⚠ A LOOSE mp4 IS NO LONGER AN ITEM (Marty, 2026-08-05: "remove 'playblasts'
# from showing in anim library"). The watcher still fires for it — it lands in a
# watched folder — the SCAN is what drops it. Both halves are asserted, because
# "no new item" would also be true if the watcher had quietly stopped working.
os.makedirs(os.path.join(root, "_playblasts"))
pump(2000)
before_mp4 = len(view.items)
with open(os.path.join(root, "_playblasts", "take01.mp4"), "wb") as fh:
    fh.write(b"x")
pump(2500)
ok(not any(i.type == "playblast" for i in view.items),
   "a playblast mp4 in the library is NOT shown as an item any more")
ok(len(view.items) == before_mp4,
   "…and it adds nothing else either (the scan drops it, nothing else)")
ok(not any(i.path.lower().endswith(".mp4") for i in view.items),
   "no mp4 reaches the grid under any type")

# --------------------------------------------------- selection is preserved --
view.refilter()
first = view.grid.item(0)
first.setSelected(True)
kept = first.data(Qt.UserRole).path
os.makedirs(os.path.join(root, "another.pose"))
pump(2500)
still = [i.path for i in view.grid.selected_library_items()]
ok(kept in still, "an auto-rescan restores the selection by path")

# ------------------------------------------------- busy Blender defers work --
win._captures = 1              # pretend a capture is rendering
before = len(view.items)
os.makedirs(os.path.join(root, "later.pose"))
view.auto_rescan()
ok(len(view.items) == before,
   "auto_rescan defers while Blender is mid-capture (thumbnail still landing)")
win._captures = 0
view.auto_rescan()
ok(len(view.items) == before + 1, "…and picks it up once the capture is done")

# -------------------------------------------------------------- switch off --
win.cfg["auto_refresh"] = False
win.apply_auto_refresh()
ok(view._watcher is None, "turning it off tears the watcher down")
count = len(view.items)
os.makedirs(os.path.join(root, "ignored.pose"))
pump(1500)
ok(len(view.items) == count, "with it off, a disk change is ignored until a manual rescan")
view.rescan()
ok(len(view.items) == count + 1, "manual rescan still works exactly as before")

win.close()
shutil.rmtree(root, ignore_errors=True)

print("\n%d passed, %d failed" % (len(PASS), len(FAIL)), flush=True)
sys.exit(1 if FAIL else 0)
