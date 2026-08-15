# The vertex group half of Marty's 2026-08-05 batch: the type filter that was
# hiding the items, the Save Vertex Groups picker, and the weight-paint preview
# wiring.
#
#   python tests\app_vgroups_test.py
#
# The Blender half - the mode switch, the render and putting the scene back - is
# `vgroup_preview_test.py`. This one is the app.
import inspect
import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

sys.path.insert(0, os.path.join(_ROOT, "app"))

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import config  # noqa: E402

config.CONFIG_PATH = os.path.join(tempfile.mkdtemp(prefix="madi_vg_"),
                                  "config.json")

PASS, FAIL = [], []


def ok(cond, label):
    (PASS if cond else FAIL).append(label)
    print(("ok   " if cond else "FAIL ") + label, flush=True)


app = QApplication.instance() or QApplication([])

import bridge as bridgemod  # noqa: E402
import library as librarymod  # noqa: E402
import main as mainmod  # noqa: E402
import panels as panelsmod  # noqa: E402

# ---------------------------------------------------------------- type filter
# ⚠ THE BUG THIS PINS. `LibraryView.refilter` drops any item whose type is not
# in the sidebar's enabled set, so a type missing from that list is not merely
# unfilterable - it is INVISIBLE. `.vgroups` and `.picker` were both missing,
# which is why Marty's vertex groups saved fine and never appeared (2026-08-05).
sidebar = panelsmod.Sidebar()
offered = set(sidebar.type_checks)
from_disk = {ext.lstrip(".") for ext in librarymod.ITEM_EXTS}
missing = from_disk - offered
ok(not missing,
   "filter: every type the scanner can produce has a filter checkbox - a "
   "missing one makes those items invisible, not unfilterable (missing: %s)"
   % sorted(missing))
ok("vgroups" in offered and "picker" in offered,
   "filter: including the two that were missing (%s)" % sorted(offered))
# ⚠ "playblast" USED TO BE ASSERTED HERE as the bare-file type with no
# extension in ITEM_EXTS. It is gone from both lists as of 2026-08-06 (Marty:
# "remove 'playblasts' from showing in anim library, remove the filter too"), so
# the assertion is now the OPPOSITE one: a filter for a type the scanner can no
# longer produce would be a checkbox that filters nothing.
ok("playblast" not in offered,
   "filter: no checkbox for playblasts - library.scan does not make them any "
   "more (%s)" % sorted(offered))
ok(offered == from_disk,
   "filter: the two lists agree EXACTLY, in both directions (extra: %s)"
   % sorted(offered - from_disk))
ok(sidebar.enabled_types() == offered,
   "filter: they all start ticked, so nothing is hidden until asked")
sidebar.type_checks["vgroups"].setChecked(False)
ok("vgroups" not in sidebar.enabled_types(),
   "filter: and unticking one really drops it")

import grid as gridmod  # noqa: E402

for typ in sorted(offered):
    icon = gridmod.type_icon(typ, 14)
    ok(not icon.isNull() and not icon.pixmap(14, 14).toImage().isGrayscale(),
       "filter: '%s' has a drawn icon - a blank one next to a live checkbox "
       "reads as a broken row" % typ)

# ------------------------------------------------------------------ the picker
LISTING = [
    {"object": "Body", "verts": 8000,
     "groups": ["Head", "Neck", "Chest", "Head_end"]},
    {"object": "Cloth", "verts": 900, "groups": ["Hem"]},
]
dlg = mainmod.SaveVGroupsDialog(None, LISTING)
body = dlg.tree.topLevelItem(0)
ok(dlg.tree.topLevelItemCount() == 2 and body.childCount() == 4,
   "picker: one node per mesh, one row per group")
# ⚠ Everything ticked: pressing Save without reading anything has to write what
# the button wrote before the dialog existed.
ok(dlg.selection() == {"Body": ["Head", "Neck", "Chest", "Head_end"],
                       "Cloth": ["Hem"]},
   "picker: everything starts ticked, so the old behaviour is still the "
   "default one (%s)" % dlg.selection())

body.child(1).setCheckState(0, Qt.Unchecked)
body.child(2).setCheckState(0, Qt.Unchecked)
ok(dlg.selection() == {"Body": ["Head", "Head_end"], "Cloth": ["Hem"]},
   "picker: unticking picks a subset - which is the whole request, since a "
   "rigged character has a hundred bone groups (%s)" % dlg.selection())

dlg._set_all(False)
ok(dlg.selection() == {},
   "picker: 'select none' clears every mesh, not just the first")
ok(not dlg.btn_save.isEnabled(),
   "picker: and Save goes dead rather than writing an item with nothing in it")
dlg._set_all(True)
ok(len(dlg.selection().get("Body", [])) == 4 and dlg.btn_save.isEnabled(),
   "picker: 'select all' brings them back")

# The search HIDES, exactly as in the shape key dialog.
dlg.search.setText("head")
hidden = {body.child(i).data(0, Qt.UserRole): body.child(i).isHidden()
          for i in range(body.childCount())}
ok(hidden["Head"] is False and hidden["Head_end"] is False
   and hidden["Neck"] is True,
   "search: it filters by name (%s)" % hidden)
ok(dlg.tree.topLevelItem(1).isHidden(),
   "search: a mesh with no matches goes too")
ok("Neck" in dlg.selection().get("Body", []),
   "search: but a HIDDEN group is still saved - a search finds groups, it does "
   "not choose them (%s)" % dlg.selection().get("Body"))
# ⚠ ...and 'select all' means all, not "all the ones you can see".
dlg._set_all(False)
dlg._set_all(True)
ok(len(dlg.selection().get("Cloth", [])) == 1,
   "search: 'select all' reaches the meshes the search hid, or a group goes "
   "missing from an item nobody thinks to check")
dlg.search.setText("")

# --------------------------------------------------------- individual export
# Marty, 2026-08-05: "make sure to have an option to export individually too
# (and not all in one file)".
ok(hasattr(dlg, "chk_separate") and not dlg.chk_separate.isChecked(),
   "batch: 'one item per group' exists and is OFF by default - one item holding "
   "the set is what this button has always written")
ok(hasattr(mainmod.LibraryView, "save_vgroups_separately"),
   "batch: the individual path exists on the view")

bsrc = inspect.getsource(mainmod.LibraryView.save_vgroups_separately)
ok("setRange(0, len(jobs))" in bsrc,
   "batch: the progress bar is DETERMINATE, counting real groups - the app "
   "makes one call per group, so it already knows the count")
ok('state["failed"]' in bsrc and "QTimer.singleShot(0, step)" in bsrc,
   "batch: one group failing does not abort the run - forty groups is minutes "
   "of Blender being busy to throw away over one bad name")
ok('groups={obj: [group]}' in bsrc,
   "batch: each item is saved with exactly ONE group")
ok(bsrc.count('feature_reason("vgroup_preview")') == 1
   and "preview_block" in bsrc,
   "batch: the preview gate is asked ONCE up front, not per item - forty items "
   "should still be written, without forty identical refusals")

# ---------------------------------------------------------------- bulk badge
# "we need another icon near the thumbnail indicating it's a bulk export and
# not just one."
ok(librarymod._bulk_count({"type": "vgroups",
                           "meshes": [{"groups": [1, 2, 3]},
                                      {"groups": [4]}]}) == 4,
   "badge: a vgroups item counts its groups across every mesh")
ok(librarymod._bulk_count({"type": "shapes",
                           "meshes": [{"keys": [1, 2]}]}) == 2,
   "badge: a shapes item counts its keys - it has the same one-or-many choice")
# ⚠ Not every type that holds a list. A .pose holds forty bones and that is
# simply what the type IS; badging it would make the badge mean nothing.
ok(librarymod._bulk_count({"type": "pose", "bones": {"a": 1, "b": 2}}) == 0,
   "badge: a pose is not 'bulk' just because it has many bones")
ok(librarymod._bulk_count({"type": "vgroups"}) == 0
   and librarymod._bulk_count({}) == 0,
   "badge: and a malformed or empty item counts zero rather than raising")

# ⚠ PERFORMANCE, and it is not a detail. bulk_count runs on every tile PAINT,
# where meta() only ran when a filter was set - so without a type check BEFORE
# the parse, scrolling the grid would parse every .anim json in view, and those
# are the ones read_data warns are big.
import tempfile as _tf  # noqa: E402


class _ExplodingItem(librarymod.Item):
    def read_data(self):
        raise AssertionError("bulk_count parsed an item it had no business "
                             "parsing - see the guard in library.bulk_count")


_dir = _tf.mkdtemp(prefix="madi_bulkperf_")
for _typ in ("anim", "pose", "abc", "picker", "mirror", "remap", "set"):
    _it = _ExplodingItem(_dir, "x", "", "x", _typ, 0.0)
    ok(_it.bulk_count() == 0,
       "badge: a '%s' item answers 0 WITHOUT reading its json" % _typ)


class _FakeItem:
    def __init__(self, typ, count, name="thing"):
        self.type, self._count, self.name = typ, count, name
        self.color, self.path = None, ""

    def bulk_count(self):
        return self._count

    def anim_flags(self):
        # ⚠ Part of the tile pixmap's contract since 2026-08-06 (the anim
        # badges). A stub that lags the real Item raises inside the PAINT path,
        # which is how this one announced itself.
        return ()


one = gridmod.placeholder_pixmap(_FakeItem("vgroups", 1), 128)
many = gridmod.placeholder_pixmap(_FakeItem("vgroups", 12), 128)
ok(one.toImage() != many.toImage(),
   "badge: a 12-group item does NOT look like a 1-group one")
ok(gridmod.placeholder_pixmap(_FakeItem("vgroups", 1), 128).toImage()
   == gridmod.placeholder_pixmap(_FakeItem("vgroups", 0), 128).toImage(),
   "badge: one group and none look the same - 'bulk' starts at two")
# ⚠ The count is in the cache key, or re-saving with a different number of
# groups keeps the old badge until the app restarts.
ok(gridmod.placeholder_pixmap(_FakeItem("vgroups", 12), 128).toImage()
   != gridmod.placeholder_pixmap(_FakeItem("vgroups", 3), 128).toImage(),
   "badge: and the count is part of the pixmap cache key")

# -------------------------------------------------------------------- the flow
src = inspect.getsource(mainmod.LibraryView.save_vgroups_flow)
ok("SaveVGroupsDialog" in src and "groups=chosen" in src,
   "flow: the dialog's choice is what gets saved, not every group on the mesh")
ok("objects=list(chosen)" in src,
   "flow: and only the meshes that actually have a tick")
ok("_start_vgroup_capture" in src,
   "flow: a saved item goes on to the weight-paint preview")

rec = inspect.getsource(mainmod.LibraryView.on_recapture)
ok('item.type == "vgroups"' in rec and "_start_vgroup_capture" in rec,
   "flow: and so does the 📷 button - re-capturing must not quietly swap the "
   "weight paint for whatever the viewport happens to look like")

cap = inspect.getsource(mainmod.LibraryView._start_vgroup_capture)
ok('feature_reason("vgroup_preview")' in cap,
   "gate: an add-on too old for it is asked BEFORE the capture is started")
ok("self._start_capture(" not in cap and "bridge.capture_preview(" not in cap,
   "gate: and there is no fallback to the ordinary capture - a grey viewport "
   "where weight colours are expected reads as 'the weights did not save'")

# ---------------------------------------------------------------------- gating
req = bridgemod.FEATURE_REQUIREMENTS["vgroup_preview"]
ok(req[0] == "capture_vgroup_preview",
   "gate: it is gated on its own command name (%s)" % (req,))
ok(bridgemod.version_tuple(req[1]) >= (0, 16, 0),
   "gate: introduced in 0.16.0 or later (%s)" % req[1])
ok(bridgemod.version_tuple(bridgemod.EXPECTED_ADDON_VERSION) >= (0, 16, 0),
   "gate: and the app expects an add-on that has it (got %s)"
   % bridgemod.EXPECTED_ADDON_VERSION)
old = bridgemod.feature_block_reason(["save_vgroups", "capture_preview"],
                                     "vgroup_preview", "0.15.0")
ok(old and "0.16.0" in old,
   "gate: an older add-on gets a reason naming the version, not a stack trace "
   "(%s)" % old)
new = bridgemod.feature_block_reason(
    ["save_vgroups", "capture_vgroup_preview"], "vgroup_preview", "0.16.0")
ok(new is None, "gate: and a current one is simply not blocked (%s)" % new)
ok(bridgemod.feature_block_reason(None, "vgroup_preview", None) is None,
   "gate: offline, nothing is claimed either way - crying wolf about a bridge "
   "that has not answered yet is worse than staying quiet")

ok(hasattr(bridgemod.Bridge, "capture_vgroup_preview"),
   "bridge: the client method exists")
worker = inspect.getsource(mainmod.CaptureWorker._run)
ok("capture_vgroup_preview" in worker,
   "bridge: and the capture worker can call it off the GUI thread, like every "
   "other capture - Blender blocks either way, the app must not")

print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
for label in FAIL:
    print("  FAILED: " + label)
sys.exit(1 if FAIL else 0)
