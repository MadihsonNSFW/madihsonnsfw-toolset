# Task 15 verification: LayerToolsTool offscreen — filter plumbing, scope
# passing, share dropdown sync, status messages.
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

sys.path.insert(0, os.path.join(_ROOT, "app"))

from PySide6.QtWidgets import QApplication  # noqa: E402

import anim_layers  # noqa: E402

PASS = []
FAIL = []


def ok(cond, label):
    (PASS if cond else FAIL).append(label)
    print(("ok   " if cond else "FAIL ") + label, flush=True)


class StubBridge:
    def __init__(self):
        self.calls = []

    def _record(self, name, kwargs, result):
        self.calls.append((name, kwargs))
        return result

    def anim_layers_select_bones(self, **kw):
        return self._record("select_bones", kw, {
            "error": None, "layers": [],
            "selected_bones": {"layer": "Layer 2", "bones": ["A", "B"],
                               "missing": []}})

    def anim_layers_reset(self, **kw):
        return self._record("reset", kw, {
            "error": None, "layers": [],
            "reset": {"layer": "Layer 2", "channels": 10, "bones": 1,
                      "frame": 12}})

    def anim_layers_cyclic(self, **kw):
        return self._record("cyclic", kw, {
            "error": None, "layers": [],
            "cyclic": {"layer": "Layer 2", "enabled": kw.get("enable"),
                       "curves": 3, "scoped": 3}})

    def anim_layers_extract_bones(self, **kw):
        return self._record("extract_bones", kw, {
            "error": None, "layers": [],
            "extracted": {"from": "Layer 2", "layer": "Layer 2 Extracted",
                          "curves": 7}})

    def anim_layers_extract_markers(self, **kw):
        return self._record("extract_markers", kw, {
            "error": None, "layers": [],
            "markers": {"from": "Layer 2", "layer": "Layer 2 Markers",
                        "curves": 3, "keys": 12, "markers": 4,
                        "source_muted": kw.get("mute_source")}})

    def anim_layers_share_keys(self, source_index, **kw):
        kw["source_index"] = source_index
        return self._record("share_keys", kw, {
            "error": None, "layers": [],
            "shared": {"from": "Base Layer", "layer": "Layer 2", "keys": 5,
                       "channels": 3}})


app = QApplication.instance() or QApplication([])
stub = StubBridge()
tool = anim_layers.LayerToolsTool(stub, None)


def last(name):
    assert stub.calls[-1][0] == name, stub.calls[-1]
    return stub.calls[-1][1]


# ---------------------------------------------------------------- defaults
ok(all(b.isChecked() for b in tool.chan_boxes.values()),
   "all channel boxes start ticked")
ok(all(b.isChecked() for b in tool.axis_boxes.values()),
   "all axis boxes start ticked")
ok(tool.chk_selected.isChecked(), "only-selected-bones defaults on")
ok(tool._channels() is None and tool._axes() is None,
   "fully ticked filter sends None (= no filtering, custom props included)")

# ---------------------------------------------------------------- filter math
tool.chan_boxes["SCALE"].setChecked(False)
ok(sorted(tool._channels()) == ["LOCATION", "ROTATION"],
   "unticking Scale sends the remaining two (got %s)" % tool._channels())
tool.axis_boxes["W"].setChecked(False)
ok(sorted(tool._axes()) == ["X", "Y", "Z"],
   "unticking W sends XYZ (got %s)" % tool._axes())
tool.chan_boxes["SCALE"].setChecked(True)
tool.axis_boxes["W"].setChecked(True)
ok(tool._channels() is None and tool._axes() is None, "re-ticking resets to None")

# ---------------------------------------------------------------- each action
tool.select_bones()
c = last("select_bones")
ok(c["channels"] is None and c["axes"] is None,
   "select bones passes the filter (got %s)" % c)
ok("Selected 2 bones from 'Layer 2'" in tool.status.text(),
   "select bones status (got: %s)" % tool.status.text())

tool.chan_boxes["ROTATION"].setChecked(False)
tool.chk_selected.setChecked(False)
tool.reset_layer()
c = last("reset")
ok(c["selected_only"] is False
   and sorted(c["channels"]) == ["LOCATION", "SCALE"],
   "reset passes scope + filter (got %s)" % c)
ok("10 channels keyed to rest" in tool.status.text(),
   "reset status (got: %s)" % tool.status.text())
tool.chan_boxes["ROTATION"].setChecked(True)
tool.chk_selected.setChecked(True)

tool.set_cyclic(True)
ok(last("cyclic")["enable"] is True, "make cyclic passes enable=True")
ok("Made cyclic on 3 curves" in tool.status.text(),
   "cyclic status (got: %s)" % tool.status.text())
tool.set_cyclic(False)
ok(last("cyclic")["enable"] is False, "remove cyclic passes enable=False")
ok("Removed cyclic on 3 curves" in tool.status.text(),
   "uncyclic status (got: %s)" % tool.status.text())

# The Inbetween control was removed on 2026-08-04 — Blender's own Breakdowner
# already does it, so the tab stopped duplicating it.
ok(not hasattr(tool, "inbetween") and not hasattr(tool, "apply_inbetween"),
   "no Inbetween control on the tools panel")

tool.extract_bones()
ok(last("extract_bones")["selected_only"] is True, "extract passes scope")
ok("into new layer 'Layer 2 Extracted'" in tool.status.text(),
   "extract status (got: %s)" % tool.status.text())

tool.chk_mute_source.setChecked(False)
tool.extract_markers()
c = last("extract_markers")
ok(c["mute_source"] is False, "markers passes mute_source")
ok("Extracted 4 markers" in tool.status.text()
   and "source muted" not in tool.status.text(),
   "markers status without mute (got: %s)" % tool.status.text())
tool.chk_mute_source.setChecked(True)

# ---------------------------------------------------------------- share sync
before = len(stub.calls)
tool.share_keys()
ok(len(stub.calls) == before and "Pick a layer" in tool.status.text(),
   "share with an empty dropdown asks instead of calling")

status = {"error": None, "active_index": 1,
          "layers": [{"index": 0, "name": "Base Layer"},
                     {"index": 1, "name": "Layer 2"},
                     {"index": 2, "name": "Layer 3"}]}
tool.on_layers_changed(status)
items = [tool.source_combo.itemText(i)
         for i in range(tool.source_combo.count())]
ok(items == ["Layer 3", "Base Layer"],
   "dropdown lists other layers top-first, excludes the active one (got %s)"
   % items)

tool.source_combo.setCurrentIndex(1)          # Base Layer -> index 0
tool.share_keys()
c = last("share_keys")
ok(c["source_index"] == 0,
   "share passes the REAL layer index, not the combo row (got %s)"
   % c["source_index"])
ok("Added 5 keys to 'Layer 2'" in tool.status.text(),
   "share status (got: %s)" % tool.status.text())

# selection survives a poll that doesn't change the stack
tool.on_layers_changed(status)
ok(tool.source_combo.currentData() == 0,
   "an unchanged poll keeps the chosen source layer")

status2 = dict(status, active_index=0)
tool.on_layers_changed(status2)
items = [tool.source_combo.itemText(i)
         for i in range(tool.source_combo.count())]
ok(items == ["Layer 3", "Layer 2"],
   "changing the active layer rebuilds the dropdown (got %s)" % items)

tool.on_layers_changed({"error": "No active object"})
ok(tool.source_combo.count() == 2, "an error status leaves the dropdown alone")

print("\n%d passed, %d failed" % (len(PASS), len(FAIL)), flush=True)
sys.exit(1 if FAIL else 0)
