# The Export Abc options dialog (Marty, 2026-08-05).
#
#   python tests\app_abc_test.py
#
# The Blender half - what the add-on accepts, and that the app's tables have not
# drifted from it - is `abc_export_test.py`. This one is about the dialog: that
# every option is reachable, that what it hands back is what was ticked, and
# that the choices are remembered.
import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

sys.path.insert(0, os.path.join(_ROOT, "app"))

from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox,  # noqa: E402
                               QDoubleSpinBox, QSpinBox, QWidget)

import config  # noqa: E402

config.CONFIG_PATH = os.path.join(tempfile.mkdtemp(prefix="madi_abc_"),
                                  "config.json")

import main as mainmod  # noqa: E402

PASS, FAIL = [], []


def ok(cond, label):
    (PASS if cond else FAIL).append(label)
    print(("ok   " if cond else "FAIL ") + label, flush=True)


app = QApplication.instance() or QApplication([])
parent = QWidget()
defaults = mainmod.abc_defaults()

dialog = mainmod.AbcExportDialog(parent, defaults, 1, 24)
ok(set(dialog._widgets) == set(defaults),
   "dialog: every option has a control (%d)" % len(dialog._widgets))
ok(dialog.values() == defaults,
   "dialog: opened at the defaults, it hands the defaults back unchanged")

# --------------------------------------------------------------- frame range
# Marty, 2026-08-05: "Scale, frame start frame end, ... and basically the rest".
ok(dialog.frames() == (1, 24),
   "frames: the sidebar's Start/End seed the dialog and come back out (%s)"
   % (dialog.frames(),))
ok(not dialog.chk_scene_range.isChecked(),
   "frames: with a range given, 'use the scene range' is off")
ok(set(dialog._widgets).isdisjoint({"frame_start", "frame_end"}),
   "frames: they are NOT options - they are arguments to save_abc, and putting "
   "them in _widgets would persist them into abc_export and hand them to "
   "core.abc_options, which drops unknown keys anyway")
dialog.frame_start.setValue(40)
dialog.frame_end.setValue(10)
ok(dialog.frames() == (10, 40),
   "frames: a backwards range is put the right way round (%s)"
   % (dialog.frames(),))
dialog.chk_scene_range.setChecked(True)
ok(dialog.frames() == (None, None),
   "frames: ticking the box hands back None - the ADD-ON reads the scene range "
   "at export time, so the range is whatever Blender says when it runs")
ok(not dialog.frame_start.isEnabled(),
   "frames: and the boxes grey out, rather than showing numbers that are not "
   "being used")

# ⚠ THE CRASH THIS REPLACED: the sidebar's frame boxes are blank by DEFAULT, so
# they arrive as None, and the old header formatted them with %d - the dialog
# could not open at all unless a range had been typed in first.
blank = mainmod.AbcExportDialog(parent, defaults, None, None)
ok(blank.frames() == (None, None),
   "frames: no range given (the default state) opens on the scene range "
   "instead of raising")
ok(blank.chk_scene_range.isChecked(),
   "frames: with the box ticked to say so")
ranged = mainmod.AbcExportDialog(parent, defaults, None, None,
                                 scene_range=(7, 96))
ok(ranged.frame_start.value() == 7 and ranged.frame_end.value() == 96,
   "frames: and when the bridge could say what the scene range is, the boxes "
   "start there - so unticking gives a sane range, not 1-250")

# ⚠ The methods only mean anything while Triangulate is on. A live-looking
# control that does nothing is worse than a greyed one - and Qt does NOT grey a
# QFormLayout's LABEL with its field, so both halves are checked.
ok(not dialog._widgets["quad_method"].isEnabled(),
   "dialog: the quad method is greyed while Triangulate is off")
dialog._widgets["triangulate"].setChecked(True)
ok(dialog._widgets["quad_method"].isEnabled()
   and dialog._widgets["ngon_method"].isEnabled(),
   "dialog: ticking Triangulate wakes both method pickers")
dialog._widgets["triangulate"].setChecked(False)
ok(not dialog._widgets["ngon_method"].isEnabled(),
   "dialog: and unticking greys them again")

# what it hands back is what was set - each widget kind exercised
dialog._widgets["vcolors"].setChecked(True)
dialog._widgets["selected"].setChecked(False)
dialog._widgets["evaluation_mode"].setCurrentIndex(
    dialog._widgets["evaluation_mode"].findData("VIEWPORT"))
dialog._widgets["global_scale"].setValue(0.01)
dialog._widgets["xsamples"].setValue(8)
got = dialog.values()
ok(got["vcolors"] is True and got["selected"] is False,
   "values: checkboxes come back as bools")
ok(got["evaluation_mode"] == "VIEWPORT",
   "values: a combo comes back as the ENUM Blender wants, not its label")
ok(abs(got["global_scale"] - 0.01) < 1e-9 and got["xsamples"] == 8,
   "values: numbers come back as numbers (%s / %s)"
   % (got["global_scale"], got["xsamples"]))
ok(isinstance(got["xsamples"], int) and isinstance(got["vcolors"], bool),
   "values: and with the right TYPES - the add-on coerces, but sending the "
   "right thing is what keeps the two in step")

dialog.reset_defaults()
ok(dialog.values() == defaults,
   "values: 'Blender defaults' really puts every one back")

# a remembered set is applied on open
stored = dict(defaults)
stored.update({"selected": False, "triangulate": True, "gsamples": 3,
               "quad_method": "BEAUTY", "sh_close": 0.5})
again = mainmod.AbcExportDialog(parent, stored, 5, 9)
ok(again.values() == stored,
   "memory: a stored set is what the dialog opens on - these are settings, not "
   "a question to answer again every export")
ok(again._widgets["quad_method"].isEnabled(),
   "memory: including the greying, which follows the stored Triangulate")

# unknown/partial stored values must not break it - config.json is editable and
# an older build's stored set will be missing whatever was added since
partial = mainmod.AbcExportDialog(parent, {"vcolors": True, "gone": 1}, 1, 2)
got = partial.values()
ok(got["vcolors"] is True and got["normals"] is True,
   "memory: a PARTIAL stored set fills the rest in from the defaults")
ok("gone" not in got,
   "memory: and a stale key that no longer exists is simply not offered")

ok(config.DEFAULTS.get("abc_export") == {},
   "config: there is a place to remember them, empty until the first export")

# ⚠ The reply is the capability check for this feature, because `save_abc`
# exists in every add-on version - a command-name gate cannot see that it grew
# a parameter. No `options` echoed back means they were ignored.
import inspect  # noqa: E402

src = inspect.getsource(mainmod.LibraryView.save_abc_flow)
ok('"options" not in r' in src,
   "gate: the flow checks the REPLY for the options it sent")
ok("IGNORED" in src,
   "gate: and says so plainly, because a silently-ignored option is a wrong "
   "cache rather than a failed export")
ok("options=chosen" in src, "gate: the chosen options really are sent")
ok(src.index("AbcExportDialog") > src.index("already exists"),
   "flow: the dialog is asked AFTER the overwrite prompt - cancelling it must "
   "not leave an item already versioned aside")
ok("frame_start, frame_end = dialog.frames()" in src
   and "frame_start=frame_start" in src,
   "flow: the range the DIALOG hands back is the one exported - not the "
   "sidebar's, which only seeds it")

print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
for label in FAIL:
    print("  FAILED: " + label)
sys.exit(1 if FAIL else 0)
