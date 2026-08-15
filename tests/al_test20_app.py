# Task 20 verification (offscreen): Multikey UI, Frame Range + Influence-key
# settings, and the foreign-NLA banner — against a stub bridge.
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

sys.path.insert(0, os.path.join(_ROOT, "app"))

from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

import anim_layers  # noqa: E402

PASS = []
FAIL = []


def ok(cond, label):
    (PASS if cond else FAIL).append(label)
    print(("ok   " if cond else "FAIL ") + label, flush=True)


def layer_row(index, name, **over):
    row = {"index": index, "name": name, "mute": False, "lock": False,
           "solo": False, "nla_solo": False, "locked_reason": None,
           "action": name, "action_users": 1, "keys": 4,
           "blend_type": "COMBINE", "influence": 1.0,
           "animated_influence": False, "influence_keys": 1,
           "influence_keyed": False, "frame_start": 1.0, "frame_end": 60.0,
           "repeat": 1.0, "scale": 1.0, "reversed": False,
           "extrapolation": "HOLD", "strip_mute": False,
           "influence_hide": False, "influence_mute": False,
           "influence_lock": False, "influence_selected": False,
           "custom_range": False, "always_sync": False, "action_length": 20.0}
    row.update(over)
    return row


def status(layers, active=1, foreign=False):
    return {"error": None, "object": "Rig", "object_type": "ARMATURE",
            "data_type": "OBJECT", "has_shapekeys": False, "mode": "POSE",
            "frame": 12, "frame_start": 1, "frame_end": 60,
            "has_animdata": True, "nla_evaluation": True, "in_tweak": True,
            "active_action": None, "managed": not foreign,
            "foreign_nla": foreign, "solo": None, "tweak_debug": {},
            "layers": layers, "active_index": active}


class StubBridge:
    def __init__(self):
        self.calls = []

    def _record(self, name, kwargs, result):
        self.calls.append((name, kwargs))
        return result

    # ---- multikey / tools
    def anim_layers_multikey(self, op, value, **kw):
        kw.update({"op": op, "value": value})
        return self._record("multikey", kw, {
            "error": None, "layers": [],
            "multikey": {"layer": "Layer 2", "op": op, "value": value,
                         "curves": 3, "keys": 9, "selected_keys": True}})

    # ---- frame range
    def anim_layers_frame_range(self, **kw):
        return self._record("frame_range", kw, {
            "error": None, "layers": [],
            "frame_range": {"layer": "Layer 2", "custom": kw.get("custom"),
                            "always_sync": bool(kw.get("always_sync")),
                            "frame_start": 10.0, "frame_end": 50.0,
                            "repeat": 2.0, "scale": 1.5, "reversed": True,
                            "extrapolation": "HOLD"}})

    # ---- influence keys
    def anim_layers_influence_keys(self, **kw):
        return self._record("influence_keys", kw, {
            "error": None, "layers": [],
            "influence_settings": {"scope": kw.get("scope"),
                                   "layers": ["Layer 2"],
                                   "skipped": ["Layer 3"], "keys": 3,
                                   "select": kw.get("select"),
                                   "hide": kw.get("hide"),
                                   "mute": kw.get("mute"),
                                   "lock": kw.get("lock")}})

    # ---- NLA adoption
    def anim_layers_adopt_nla(self, **kw):
        out = status([], active=None)
        out["adopted"] = {"layers": ["Walk"],
                          "locked": [{"name": "Two Strips",
                                      "reason": "2 strips (one per layer)"}]}
        return self._record("adopt_nla", {}, out)

    def anim_layers_clear_nla(self, confirm=False, **kw):
        out = status([], active=None)
        out["cleared"] = {"layers": ["Walk", "Two Strips"]}
        return self._record("clear_nla", {"confirm": confirm}, out)

    # ---- polled by the stack tool (unused here but must exist)
    def anim_layers_status(self, **kw):
        return self._record("status", kw, status([]))

    def anim_layers_actions(self):
        return self._record("actions", {}, [])

    def anim_layers_select(self, index, **kw):
        return self._record("select", {"index": index},
                            status([], active=index))

    def anim_layers_solo(self, index=None, **kw):
        return self._record("solo", {"index": index}, status([], active=0))


app = QApplication.instance() or QApplication([])
stub = StubBridge()


def last(name):
    assert stub.calls[-1][0] == name, stub.calls[-1]
    return stub.calls[-1][1]


def sent(name, since=0):
    """Did we send this command? (a status refresh often follows it)"""
    return [kw for n, kw in stub.calls[since:] if n == name]


# ==================================================== Multikey (LayerTools)
tools = anim_layers.LayerToolsTool(stub, None)

ok(tools.mk_op.currentData() == "OFFSET", "multikey defaults to Offset")
ok(tools.chk_selected_keys.isChecked(),
   "'only selected keyframes' defaults on")
ok(not tools.mk_pivot.isVisible() and not tools.mk_seed.isVisible(),
   "pivot + seed are hidden for Offset")

tools.mk_value.setValue(0.25)
tools.apply_multikey()
c = last("multikey")
ok(c["op"] == "OFFSET" and abs(c["value"] - 0.25) < 1e-9,
   "Offset sends op + value (%s)" % c)
ok(c["selected_keys"] is True and c["selected_only"] is True,
   "the key scope and bone scope both travel (%s)" % c)
ok(c["channels"] is None and c["axes"] is None,
   "the shared filter travels with multikey")

tools.mk_op.setCurrentIndex(tools.mk_op.findData("SCALE"))
ok(abs(tools.mk_value.value() - 1.0) < 1e-9,
   "switching to Scale resets the value to the neutral 1.0")
tools.mk_pivot.setCurrentIndex(tools.mk_pivot.findData("ZERO"))
tools.mk_value.setValue(2.0)
tools.apply_multikey()
c = last("multikey")
ok(c["op"] == "SCALE" and c["pivot"] == "ZERO",
   "Scale sends the pivot (%s)" % c)

tools.mk_op.setCurrentIndex(tools.mk_op.findData("RANDOMIZE"))
ok(abs(tools.mk_value.value()) < 1e-9,
   "switching to Randomize resets the value to 0")
tools.mk_seed.setValue(42)
tools.mk_value.setValue(0.1)
tools.apply_multikey()
c = last("multikey")
ok(c["op"] == "RANDOMIZE" and c["seed"] == 42, "Randomize sends the seed (%s)"
   % c)

tools.chk_selected_keys.setChecked(False)
tools.chan_boxes["SCALE"].setChecked(False)
tools.apply_multikey()
c = last("multikey")
ok(c["selected_keys"] is False and sorted(c["channels"]) ==
   ["LOCATION", "ROTATION"],
   "unticking flows through to multikey too (%s)" % c)
tools.chan_boxes["SCALE"].setChecked(True)

# ==================================================== Layer settings
st = anim_layers.LayerSettingsTool(stub, None)
ok(not st.spin_start.isEnabled(),
   "settings start disabled until a layer is selected")

rows = [layer_row(0, "Base Layer"),
        layer_row(1, "Layer 2", custom_range=True, frame_start=10.0,
                  frame_end=50.0, repeat=2.0, scale=1.5, reversed=True,
                  extrapolation="HOLD_FORWARD", influence_mute=True),
        layer_row(2, "Layer 3")]
st.on_layers_changed(status(rows, active=1))

ok(st.spin_start.isEnabled() and st.chk_custom.isChecked(),
   "the active layer's settings load in")
ok(abs(st.spin_start.value() - 10.0) < 1e-6
   and abs(st.spin_end.value() - 50.0) < 1e-6,
   "start/end mirror the status row (%.1f..%.1f)"
   % (st.spin_start.value(), st.spin_end.value()))
ok(abs(st.spin_repeat.value() - 2.0) < 1e-6
   and abs(st.spin_speed.value() - 1.5) < 1e-6,
   "repeat/speed mirror the status row")
ok(st.chk_reverse.isChecked() and st.extrap.currentData() == "HOLD_FORWARD",
   "reversed + extrapolation mirror the status row")
ok(st.infl_boxes["mute"].isChecked()
   and not st.infl_boxes["hide"].isChecked(),
   "the influence-key toggles mirror the curve flags")

before = len(stub.calls)
st.on_layers_changed(status(rows, active=1))
ok(len(stub.calls) == before,
   "loading status never sends anything back to Blender")

# ---- switching to a layer with no custom range
st.on_layers_changed(status(rows, active=0))
ok(not st.chk_custom.isChecked(), "switching layers reloads the settings")

# ---- a locked layer greys the block out
st.on_layers_changed(status([layer_row(0, "Locked", lock=True)], active=0))
ok(not st.spin_start.isEnabled() and not st.btn_apply_range.isEnabled(),
   "a locked layer disables the settings")
st.on_layers_changed(status(rows, active=1))

# ---- apply
st.spin_start.setValue(12.0)
st.spin_end.setValue(48.0)
st.spin_speed.setValue(2.0)
st.spin_repeat.setValue(3.0)
st.chk_reverse.setChecked(True)
st.extrap.setCurrentIndex(st.extrap.findData("NOTHING"))
st.apply_range()
c = last("frame_range")
ok(c["custom"] is True and c["frame_start"] == 12.0 and c["frame_end"] == 48.0,
   "Apply Range sends the span and turns custom on (%s)" % c)
ok(c["scale"] == 2.0 and c["repeat"] == 3.0 and c["reverse"] is True
   and c["extrapolation"] == "NOTHING",
   "Apply Range sends speed/repeat/reverse/extrapolation (%s)" % c)

st.chk_custom.setChecked(False)
st._on_custom_toggled(False)
c = last("frame_range")
ok(c["custom"] is False and c.get("frame_start") is None,
   "turning custom OFF sends only the toggle (%s)" % c)

st.sync_to_action()
ok(last("frame_range")["sync"] is True, "Sync to Action sends sync=True")

st.chk_always_sync.setChecked(True)
st._on_always_sync(True)
c = last("frame_range")
ok(c["always_sync"] is True and not c.get("sync"),
   "'always' sends always_sync without re-syncing now (%s)" % c)

# ---- influence flags
st.infl_boxes["hide"].setChecked(True)
st._on_influence_flag("hide", True)
c = last("influence_keys")
ok(c["hide"] is True and c["scope"] == "LOCAL"
   and c.get("select") is None and c.get("mute") is None,
   "one toggle sends ONLY that flag (%s)" % c)

st.infl_scope.setCurrentIndex(st.infl_scope.findData("GLOBAL"))
st._on_influence_flag("select", True)
ok(last("influence_keys")["scope"] == "GLOBAL",
   "the scope combo travels with the toggle")
ok("Layer 3" in st.status.text(),
   "layers with no influence curve are named in the status line (%s)"
   % st.status.text())

# ==================================================== NLA banner
stack = anim_layers.LayerStackTool(stub, None)
stack.apply_status(status(rows, active=1, foreign=False))
ok(not stack.nla_banner.isVisibleTo(stack),
   "no banner on a stack we manage")
stack.apply_status(status(rows, active=1, foreign=True))
ok(stack.nla_banner.isVisibleTo(stack),
   "foreign NLA shows the adopt / start-fresh banner")

mark = len(stub.calls)
stack.adopt_nla()
ok(len(sent("adopt_nla", mark)) == 1, "Use as Layers calls adopt (%s)"
   % [n for n, _ in stub.calls[mark:]])
ok("locked" in stack.status.text(),
   "adoption reports the tracks that can't be layers (%s)"
   % stack.status.text())

# clearing must be behind a confirmation
_orig = QMessageBox.question
QMessageBox.question = staticmethod(
    lambda *a, **k: QMessageBox.StandardButton.No)
n = len(stub.calls)
stack.clear_nla()
ok(not sent("clear_nla", n), "declining the confirm sends nothing")
QMessageBox.question = staticmethod(
    lambda *a, **k: QMessageBox.StandardButton.Yes)
mark = len(stub.calls)
stack.clear_nla()
ok(sent("clear_nla", mark) == [{"confirm": True}],
   "confirming sends clear_nla(confirm=True) (%s)"
   % sent("clear_nla", mark))
ok(not sent("select", mark),
   "rebuilding the list never fires a layer select at Blender (%s)"
   % [n for n, _ in stub.calls[mark:]])
QMessageBox.question = _orig

# ==================================================== the page ties together
page = anim_layers.LayersPage(stub, None)
ok(hasattr(page, "stack") and hasattr(page, "tools")
   and hasattr(page, "settings"),
   "the Layers page holds stack + tools + settings")
page.stack.status_refreshed.emit(status(rows, active=1))
ok(abs(page.settings.spin_start.value() - 10.0) < 1e-6,
   "the stack's poll feeds the settings tool (no second poll)")
ok(page.tools.source_combo.count() == 2,
   "and still feeds the share-source dropdown (%d entries)"
   % page.tools.source_combo.count())
page.set_capture_busy(True)
ok(not page.settings.isEnabled() and not page.tools.isEnabled(),
   "a capture greys out every tool on the page")
page.set_capture_busy(False)

print("\n%d passed, %d failed" % (len(PASS), len(FAIL)), flush=True)
for f in FAIL:
    print("  FAILED: " + f, flush=True)
sys.exit(1 if FAIL else 0)
