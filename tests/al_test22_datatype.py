# Task 22 verification (offscreen): the Object / Shape Keys data-type toggle
# — that it reaches EVERY bridge call, relabels the scoping controls, and
# falls back when the object has no shape keys.
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


def status(layers, active=0, data_type="OBJECT", has_shapekeys=True,
           error=None):
    out = {"error": error, "object": "Head", "object_type": "MESH",
           "data_type": data_type, "has_shapekeys": has_shapekeys,
           "mode": "OBJECT", "frame": 12, "frame_start": 1, "frame_end": 60,
           "has_animdata": True, "nla_evaluation": True, "in_tweak": True,
           "active_action": None, "managed": True, "foreign_nla": False,
           "solo": None, "tweak_debug": {}, "layers": layers,
           "active_index": active}
    return out


class StubBridge:
    """Records (name, kwargs) for every call, with the real wrappers'
    defaults filled in so 'was data_type passed?' is answerable."""

    def __init__(self):
        self.calls = []
        self.status_type = "OBJECT"
        self.has_shapekeys = True

    def _rec(self, name, kw, result=None):
        self.calls.append((name, kw))
        return result if result is not None else status(
            [layer_row(0, "Base")], data_type=kw.get("data_type", "OBJECT"))

    def anim_layers_status(self, data_type="OBJECT", object_name=None,
                           poll=False):
        # `poll` mirrors the real Bridge (2026-08-02 unreachable-bridge fix):
        # the timer path passes it so it can fail fast while Blender is down.
        self.status_type = data_type
        err = None
        if data_type == "SHAPEKEY" and not self.has_shapekeys:
            err = "Head has no shape keys"
        return self._rec("status", {"data_type": data_type},
                         status([layer_row(0, "Base")], data_type=data_type,
                                has_shapekeys=self.has_shapekeys, error=err))

    def anim_layers_actions(self):
        return self._rec("actions", {}, [])

    def __getattr__(self, name):
        if not name.startswith("anim_layers_"):
            raise AttributeError(name)

        def call(*args, **kw):
            kw["_args"] = args
            return self._rec(name[len("anim_layers_"):], kw)
        return call


app = QApplication.instance() or QApplication([])
stub = StubBridge()


def types_used(since=0, skip=("status", "actions")):
    return {n: kw.get("data_type") for n, kw in stub.calls[since:]
            if n not in skip}


# ==================================================== the page as a whole
page = anim_layers.LayersPage(stub, None)
stack, tools, settings = page.stack, page.tools, page.settings

ok(stack.type_combo.currentData() == "OBJECT", "the toggle starts on Object")
ok(stack.data_type == "OBJECT", "and the stack asks for the object stack")

stack.apply_status(status([layer_row(0, "Base"), layer_row(1, "Layer 2")],
                          active=1))
ok(tools._data_type == "OBJECT" and settings._data_type == "OBJECT",
   "the tools pick the type up from the broadcast status")
ok(tools.chk_selected.text() == "Only selected bones",
   "bone wording on an object stack (%s)" % tools.chk_selected.text())
ok(tools.btn_select_bones.isEnabled(), "Select Bones is available")
ok(all(b.isEnabled() for b in tools.chan_boxes.values()),
   "the transform filter is available")

# ==================================================== switch to shape keys
mark = len(stub.calls)
stack.type_combo.setCurrentIndex(stack.type_combo.findData("SHAPEKEY"))
ok(stack.data_type == "SHAPEKEY", "the toggle switches the stack")
ok(stub.status_type == "SHAPEKEY",
   "switching polls Blender for the shape-key stack straight away")
ok(tools._data_type == "SHAPEKEY" and settings._data_type == "SHAPEKEY",
   "the tools follow")
ok(tools.chk_selected.text() == "Only the active shape key",
   "the scoping checkbox is relabelled (%s)" % tools.chk_selected.text())
ok(not tools.btn_select_bones.isEnabled(),
   "Select Bones is disabled — there are no bones on a shape-key stack")
ok(not any(b.isEnabled() for b in tools.chan_boxes.values())
   and not any(b.isEnabled() for b in tools.axis_boxes.values()),
   "the Loc/Rot/Scale filter is disabled (it scopes shape keys to nothing)")
ok("shape-key" in stack.obj_label.text(),
   "the header says which stack you're looking at (%s)" % stack.obj_label.text())

# ==================================================== every call carries it
mark = len(stub.calls)
stack.add_layer()
stack.list.setCurrentRow(0)
stack.delete_layer_confirmed = True
stack.toggle_solo(0)
stack.set_state(0, mute=True)
stack.set_influence(0, 0.5)
stack.preview_influence(0, 0.4)
stack.set_animated(0, True)
stack.move_layer("UP")
stack.duplicate_layer()
stack.adopt_nla()
used = types_used(mark)
ok(used and all(v == "SHAPEKEY" for v in used.values()),
   "every stack-tool command carries data_type=SHAPEKEY (%s)" % used)

mark = len(stub.calls)
tools.reset_layer()
tools.set_cyclic(True)
tools.extract_bones()
tools.extract_markers()
tools.apply_multikey()
tools.source_combo.addItem("Base", 0)
tools.share_keys()
used = types_used(mark)
ok(used and all(v == "SHAPEKEY" for v in used.values()),
   "every layer-tool command carries it too (%s)" % used)

mark = len(stub.calls)
settings.apply_range()
settings.sync_to_action()
settings._on_custom_toggled(False)
settings._on_always_sync(True)
settings._on_influence_flag("mute", True)
used = types_used(mark)
ok(used and all(v == "SHAPEKEY" for v in used.values()),
   "every layer-settings command carries it too (%s)" % used)

# ==================================================== Merge / Bake follows
mb = anim_layers.MergeBakeTool(stub, None)
stack.status_refreshed.connect(mb.on_layers_changed)
stack.apply_status(status([layer_row(0, "Base")], data_type="SHAPEKEY"))
ok(mb._data_type == "SHAPEKEY", "Merge/Bake follows the page's toggle")
ok(mb.type_combo.currentData() == "AL" and not mb.type_combo.isEnabled(),
   "the NLA engine is not offered for shape keys (it can't key them)")
ok(mb.chk_selected.text() == "Only the active shape key",
   "and its scope checkbox is relabelled (%s)" % mb.chk_selected.text())
mark = len(stub.calls)
mb.bake()
ok(types_used(mark) == {"bake": "SHAPEKEY"},
   "the bake command carries the type (%s)" % types_used(mark))

stack.apply_status(status([layer_row(0, "Base")], data_type="OBJECT"))
ok(mb._data_type == "OBJECT" and mb.type_combo.isEnabled(),
   "switching back re-enables the NLA engine")

# ==================================================== fallback
# the real path: you're on the shape-key stack, then click an object in
# Blender that has none — the next poll must not leave you on an error row
stack.type_combo.setCurrentIndex(stack.type_combo.findData("OBJECT"))
stack.type_combo.setCurrentIndex(stack.type_combo.findData("SHAPEKEY"))
ok(stack.data_type == "SHAPEKEY", "back on the shape-key stack")
stub.has_shapekeys = False
stack.refresh()
ok(stack.data_type == "OBJECT",
   "an object with no shape keys falls back to the object stack (%s)"
   % stack.data_type)
item = stack.type_combo.model().item(stack.type_combo.findData("SHAPEKEY"))
ok(item is not None and not item.isEnabled(),
   "and the Shape Keys entry is greyed out")

stub.has_shapekeys = True
stack.refresh()          # poll() is visibility-gated; offscreen it no-ops
item = stack.type_combo.model().item(stack.type_combo.findData("SHAPEKEY"))
ok(item is not None and item.isEnabled(),
   "selecting a mesh WITH shape keys re-enables the entry")

print("\n%d passed, %d failed" % (len(PASS), len(FAIL)), flush=True)
for f in FAIL:
    print("  FAILED: " + f, flush=True)
sys.exit(1 if FAIL else 0)
