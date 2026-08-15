# Super focus (Marty, 2026-08-05): focus follows the mouse, between this app and
# Blender only.
#
#   python tests\app_superfocus_test.py
#
# ⚠ WHAT A TEST CAN ACTUALLY PROVE HERE. Nothing offscreen can steal focus, and
# a suite that raised real windows would be testing Windows rather than us. So
# the DECISION is what is pinned - `pick_target` is a pure function of three
# plain values precisely so it can be - and the win32 half was proven live
# against the running Blender instead:
#     class GHOST_WindowClass | process blender.exe
#     root_window_at(centre of Blender's rect) -> Blender's handle
#     focus_window(blender) from a BACKGROUND process -> True, and it really
#         took the foreground (the AttachThreadInput path), then put the
#         previous foreground window back
# The safety argument is the first block: anything unrecognised returns 0.
import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

sys.path.insert(0, os.path.join(_ROOT, "app"))

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog, QMenu, QWidget  # noqa: E402

import config  # noqa: E402

# ⚠ The tickbox persists the moment it is toggled, exactly like the pin - so
# this has to be redirected before a MainWindow exists or the suite rewrites
# Marty's real config.json.
config.CONFIG_PATH = os.path.join(tempfile.mkdtemp(prefix="madi_sf_"),
                                  "config.json")

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

import superfocus as sf  # noqa: E402

PASS, FAIL = [], []


def ok(cond, label):
    (PASS if cond else FAIL).append(label)
    print(("ok   " if cond else "FAIL ") + label, flush=True)


app = QApplication.instance() or QApplication([])

# ------------------------------------------------- the decision, and its limits
OURS, BLENDER, STRANGER = 101, 202, 303
_real_is_blender = sf.is_blender
sf.is_blender = lambda h: h == BLENDER
try:
    ok(sf.pick_target(STRANGER, {OURS}, 0) == 0,
       "safety: a window that is neither ours nor Blender's is LEFT ALONE - "
       "this is the whole reason it is not a general focus-follows-mouse")
    ok(sf.pick_target(0, {OURS}, 0) == 0,
       "safety: nothing under the cursor means nothing happens")
    ok(sf.pick_target(BLENDER, {OURS}, BLENDER) == 0,
       "safety: the window that ALREADY has focus is never re-raised - the "
       "timer would otherwise fight for it five times a second")
    ok(sf.pick_target(OURS, {OURS}, OURS) == 0,
       "safety: same for our own window")
    ok(sf.pick_target(BLENDER, {OURS}, OURS) == BLENDER,
       "focus: hovering Blender while WE have focus hands it over")
    ok(sf.pick_target(OURS, {OURS}, BLENDER) == OURS,
       "focus: and hovering us while BLENDER has focus takes it back - which is "
       "the half that saves the extra click")
    ok(sf.pick_target(999, {OURS, 999}, BLENDER) == 999,
       "focus: any of our own windows counts, not just the main one (a dialog "
       "must be reachable too)")
finally:
    sf.is_blender = _real_is_blender

# ------------------------------------------------------------ the poll object
window = QWidget()
poll = sf.SuperFocus(window)
ok(poll.enabled is False, "poll: off until it is asked for")
ok(poll._timer.isActive() is False,
   "poll: and the timer is NOT running - an unticked feature costs nothing")
ok(poll.tick() == 0, "poll: a tick while off does nothing at all")

poll.set_enabled(True)
ok(poll.enabled is sf.available(),
   "poll: on only where it can actually work (available=%s)" % sf.available())
ok(poll._timer.isActive() is sf.available(),
   "poll: and the timer follows that")
poll.set_enabled(False)
ok(not poll._timer.isActive(), "poll: switching it off stops the timer")

# ⚠ Popups and tooltips are excluded on purpose: activating one would close it
# under the cursor. Menus are top-level widgets, so this needs saying in code.
dialog = QDialog()
dialog.show()
menu = QMenu()
menu.addAction("x")
menu.show()
app.processEvents()
own = poll.own_windows()
ok(int(dialog.winId()) in own,
   "windows: a dialog of ours counts as ours")
ok(int(menu.winId()) not in own,
   "windows: an open menu does NOT - it already holds the interaction")
menu.hide()
dialog.hide()
app.processEvents()
ok(int(dialog.winId()) not in poll.own_windows(),
   "windows: and a hidden window is not offered focus either")

# ------------------------------------------------------- the whole tick, faked
_real_root = sf.root_window_at
_real_fg = sf.foreground_window
_real_focus = sf.focus_window
_real_avail = sf.available
focused = []
sf.available = lambda: True
sf.root_window_at = lambda x, y: BLENDER
sf.foreground_window = lambda: OURS
sf.is_blender = lambda h: h == BLENDER
sf.focus_window = lambda h: focused.append(h) or True
try:
    poll.set_enabled(True)
    ok(poll.tick() == BLENDER and focused == [BLENDER],
       "tick: cursor over Blender, focus was ours -> Blender is raised")
    sf.root_window_at = lambda x, y: STRANGER
    focused.clear()
    ok(poll.tick() == 0 and not focused,
       "tick: cursor over anything else -> nothing is touched")
finally:
    sf.available = _real_avail
    sf.root_window_at = _real_root
    sf.foreground_window = _real_fg
    sf.focus_window = _real_focus
    sf.is_blender = _real_is_blender
    poll.set_enabled(False)

# ------------------------------------------------------ the tickbox in the shell
os.environ["MADI_FORCE_LICENSE"] = "0"
import main as mainmod  # noqa: E402

win = mainmod.MainWindow()
ok(win.superfocus_box.text() == "Super focus",
   "shell: the tickbox is called what Marty called it")
ok(win.superfocus_box.isChecked() is False,
   "shell: OFF by default - it changes what a click does across two apps")
ok(win.superfocus_box.parent() is win.statusBar(),
   "shell: it lives in the status bar, so it is reachable from every tab")
ok("Blender" in win.superfocus_box.toolTip()
   and "Nothing else" in win.superfocus_box.toolTip(),
   "shell: and says out loud that nothing else on the desktop is touched")

win.superfocus_box.setChecked(True)
ok(win.cfg.get("super_focus") is True,
   "shell: ticking it is remembered")
ok(win.superfocus.enabled is sf.available(),
   "shell: and really starts the poll")
win.superfocus_box.setChecked(False)
ok(win.cfg.get("super_focus") is False and not win.superfocus.enabled,
   "shell: unticking stops it again")
ok(config.DEFAULTS.get("super_focus") is False,
   "shell: the setting has a default, so a config written before it existed "
   "still gets one")
ok(win.superfocus_box.isVisibleTo(win.statusBar()) is sf.available(),
   "shell: hidden outright where it cannot work - never ticked-but-dead")
win.close()

print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
for label in FAIL:
    print("  FAILED: " + label)
sys.exit(1 if FAIL else 0)
