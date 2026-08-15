# The Save Shape Keys dialog: search, the two exclusion filters, and the
# one-item-per-key batch option (Marty, 2026-08-04).
#
#   python tests\app_shapes_test.py
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

sys.path.insert(0, os.path.join(_ROOT, "app"))

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

PASS, FAIL = [], []


def ok(cond, label):
    (PASS if cond else FAIL).append(label)
    print(("ok   " if cond else "FAIL ") + label, flush=True)


app = QApplication.instance() or QApplication([])

import main as mainmod  # noqa: E402

LISTING = [{
    "object": "Face", "verts": 5000,
    "keys": [
        {"name": "Basis", "is_basis": True, "has_driver": False,
         "has_animation": False, "value": 0.0, "muted": False},
        {"name": "smile", "is_basis": False, "has_driver": False,
         "has_animation": False, "value": 0.0, "muted": False},
        {"name": "blink_driven", "is_basis": False, "has_driver": True,
         "has_animation": False, "value": 0.0, "muted": False},
        {"name": "jaw_anim", "is_basis": False, "has_driver": False,
         "has_animation": True, "value": 0.0, "muted": False},
        {"name": "smile_wide", "is_basis": False, "has_driver": False,
         "has_animation": False, "value": 0.0, "muted": False},
    ]}]

dlg = mainmod.SaveShapesDialog(None, LISTING)
node = dlg.tree.topLevelItem(0)
names = [node.child(i).data(0, Qt.UserRole) for i in range(node.childCount())]
ok(names == ["smile", "blink_driven", "jaw_anim", "smile_wide"],
   "shapes: the Basis is never offered (%s)" % names)

# ------------------------------------------------------------------ filters
ok(dlg.chk_skip_drivers.isChecked(),
   "shapes: driven keys are excluded BY DEFAULT - the driver is not rebuilt "
   "when the keys go back, so vaulting one is usually a mistake")
ok(not dlg.chk_skip_animated.isChecked(),
   "shapes: animated keys are NOT excluded by default - an animated key is "
   "still a perfectly good shape")

sel = dlg.selection()
ok("blink_driven" not in sel.get("Face", []),
   "shapes: so the driven key is not in the selection (%s)" % sel)
ok("jaw_anim" in sel.get("Face", []),
   "shapes: but the animated one is")

driven = node.child(1)
ok(not (driven.flags() & Qt.ItemIsEnabled),
   "shapes: an excluded key is DISABLED, not merely unticked - otherwise it "
   "could be ticked straight back on while the filter still claimed to "
   "exclude it")
# Marty, 2026-08-05: "make the items in list dissapear if they are excluded".
ok(driven.isHidden(),
   "shapes: and it is GONE from the list, not sat there greyed out")
# ⚠ Both, in that order. Hiding a row that is still ticked would save it from
# off screen - the same trap the search section below guards from the other side.
ok(driven.checkState(0) == Qt.Unchecked,
   "shapes: it was unticked BEFORE it was hidden - a hidden-but-ticked row "
   "would still be saved")
ok("driver" in (driven.toolTip(0) or "").lower(),
   "shapes: and it says why (%r)" % driven.toolTip(0))

dlg.chk_skip_drivers.setChecked(False)
ok(driven.flags() & Qt.ItemIsEnabled and not driven.isHidden(),
   "shapes: turning the filter off hands the key back - visible and enabled")

dlg.chk_skip_animated.setChecked(True)
ok("jaw_anim" not in dlg.selection().get("Face", []),
   "shapes: the animation filter works the same way")
# ⚠ Turning a filter off must put back what it took. The first version left
# everything unticked, so flicking a filter on and off silently emptied the
# checklist and the next Save wrote nothing.
dlg.chk_skip_animated.setChecked(False)
ok("jaw_anim" in dlg.selection().get("Face", []),
   "shapes: and turning it off RE-TICKS what it unticked (%s)"
   % dlg.selection().get("Face", []))

# ...but never overrides a decision the user made by hand.
jaw = node.child(2)
jaw.setCheckState(0, Qt.Unchecked)
dlg.chk_skip_animated.setChecked(True)
dlg.chk_skip_animated.setChecked(False)
ok("jaw_anim" not in dlg.selection().get("Face", []),
   "shapes: a key the USER unticked stays unticked through a filter round "
   "trip - restoring must not undo their choice")
jaw.setCheckState(0, Qt.Checked)
dlg.chk_skip_drivers.setChecked(True)

# ------------------------------------------------------------------- search
dlg.search.setText("smile")
hidden = {node.child(i).data(0, Qt.UserRole): node.child(i).isHidden()
          for i in range(node.childCount())}
ok(hidden["smile"] is False and hidden["smile_wide"] is False,
   "search: matching keys stay visible")
ok(hidden["jaw_anim"] is True, "search: the others are hidden")

# ⚠ THE TRAP: a hidden row is still saved. Dropping keys because of what is
# typed in a search box would lose work silently.
after = dlg.selection().get("Face", [])
ok("jaw_anim" in after,
   "search: a HIDDEN key is still saved - search finds keys, it does not "
   "choose them (%s)" % after)
dlg.search.setText("")

# ------------------------------------------------------------------ counting
count_text = dlg.count.text()
ok("key(s) will be saved" in count_text,
   "shapes: the dialog says how many will be saved (%r)" % count_text)
# ⚠ Now that excluded rows VANISH, the count has to say where they went. On a
# DAZ figure nearly every key is driven and "exclude driven" is on by default,
# so the list arrives all but empty - a working dialog that reads as broken.
ok("1 hidden by the filters" in count_text,
   "shapes: and how many the filters took away (%r)" % count_text)
dlg.chk_skip_drivers.setChecked(False)
ok("hidden by the filters" not in dlg.count.text(),
   "shapes: with nothing excluded it says nothing about hiding (%r)"
   % dlg.count.text())

# A mesh with nothing left on screen goes too - an empty expander is noise.
dlg.chk_skip_drivers.setChecked(True)
dlg.chk_skip_animated.setChecked(True)
dlg.search.setText("zzz_no_such_key")
ok(node.isHidden(), "shapes: a mesh with no visible keys left hides too")
dlg.search.setText("")
dlg.chk_skip_animated.setChecked(False)
ok(not node.isHidden(), "shapes: and comes back")

# ------------------------------------------------------------------ batch
ok(hasattr(dlg, "chk_separate") and not dlg.chk_separate.isChecked(),
   "batch: 'one item per key' exists and is OFF by default - the old "
   "behaviour is still the default one")
ok(hasattr(mainmod.LibraryView, "save_shapes_separately"),
   "batch: the batch path exists on the view")

# The bar is driven by the APP here, not by an add-on progress command: the app
# makes one call per key, so it already knows the count.
import inspect  # noqa: E402
src = inspect.getsource(mainmod.LibraryView.save_shapes_separately)
ok("setRange(0, len(jobs))" in src,
   "batch: the progress bar is DETERMINATE, counting real keys")
ok("failed" in src and "state[\"index\"]" in src,
   "batch: failures are collected rather than aborting the run - twenty keys "
   "is minutes of work to throw away over one bad name")

print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
for label in FAIL:
    print("  FAILED: " + label)
sys.exit(1 if FAIL else 0)
