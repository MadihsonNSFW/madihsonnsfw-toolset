# Task 23 verification (offscreen): the Anim Layers preferences — config.json
# round-trip (never touching the real one), the Options page, and how the
# stack tool uses them.
import json
import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

sys.path.insert(0, os.path.join(_ROOT, "app"))

from PySide6.QtWidgets import QApplication  # noqa: E402

import anim_layers  # noqa: E402
import config  # noqa: E402

PASS = []
FAIL = []


def ok(cond, label):
    (PASS if cond else FAIL).append(label)
    print(("ok   " if cond else "FAIL ") + label, flush=True)


# ==================================================== config round-trip
tmp = tempfile.mkdtemp(prefix="madi_cfg_")
real_path = config.CONFIG_PATH
config.CONFIG_PATH = os.path.join(tmp, "config.json")

ok("anim_layers" in config.DEFAULTS,
   "the anim_layers group has defaults")
cfg = config.load()
ok(cfg["anim_layers"] == {"sync_names": True, "auto_blend": True,
                          "default_blend": "COMBINE"},
   "a missing config loads the defaults (%s)" % cfg["anim_layers"])

cfg["anim_layers"] = {"sync_names": False, "auto_blend": False,
                      "default_blend": "ADD"}
config.save(cfg)
again = config.load()
ok(again["anim_layers"] == cfg["anim_layers"],
   "settings survive a save/load round-trip (%s)" % again["anim_layers"])
ok(os.path.isfile(config.CONFIG_PATH) and real_path != config.CONFIG_PATH,
   "the test wrote to a temp config, not the real one")

# an OLDER config (written before a key existed) still gets the new default
with open(config.CONFIG_PATH, "w", encoding="utf-8") as f:
    json.dump({"libraries": [{"name": "Main", "path": tmp}],
               "anim_layers": {"sync_names": False}}, f)
old = config.load()
ok(old["anim_layers"]["sync_names"] is False,
   "a value written by an older build is kept")
ok(old["anim_layers"]["default_blend"] == "COMBINE"
   and old["anim_layers"]["auto_blend"] is True,
   "and the keys it never had are filled in (%s)" % old["anim_layers"])
config.CONFIG_PATH = real_path

# ==================================================== the Options page
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


def status(layers, active=0):
    return {"error": None, "object": "Rig", "object_type": "ARMATURE",
            "data_type": "OBJECT", "has_shapekeys": False, "mode": "POSE",
            "frame": 12, "frame_start": 1, "frame_end": 60,
            "has_animdata": True, "nla_evaluation": True, "in_tweak": True,
            "active_action": None, "managed": True, "foreign_nla": False,
            "solo": None, "tweak_debug": {}, "layers": layers,
            "active_index": active}


class StubBridge:
    def __init__(self, rows=None):
        self.calls = []
        self.rows = rows or [layer_row(0, "Base")]

    def _rec(self, name, kw, result=None):
        self.calls.append((name, kw))
        return result if result is not None else status(self.rows)

    def anim_layers_status(self, **kw):
        return self._rec("status", kw)

    def anim_layers_actions(self):
        return self._rec("actions", {}, [])

    def anim_layers_sync_names(self, **kw):
        return self._rec("sync_names", kw, {"renamed": []})

    def __getattr__(self, name):
        if not name.startswith("anim_layers_"):
            raise AttributeError(name)

        def call(*args, **kw):
            kw["_args"] = args
            return self._rec(name[len("anim_layers_"):], kw)
        return call


class StubWindow:
    capturing = False

    def __init__(self):
        self.saved = 0

    def bridge_free_for_tools(self):
        return True

    def save_settings(self):
        self.saved += 1


app = QApplication.instance() or QApplication([])
stub = StubBridge()
win = StubWindow()

page = anim_layers.LayersPage(stub, win)
opts = anim_layers.LayerOptionsTool(stub, win)
opts.stack = page.stack

opts.load_settings({"sync_names": False, "default_blend": "ADD"})
ok(not opts.chk_sync_names.isChecked() and opts.blend.currentData() == "ADD",
   "the Options page loads saved settings")
ok(win.saved == 0, "loading settings does not trigger a save")
ok(opts.settings() == {"sync_names": False, "default_blend": "ADD"},
   "and reports them back (%s)" % opts.settings())

opts.chk_sync_names.setChecked(True)
opts._save()
ok(win.saved == 1, "changing a setting saves")
ok(page.stack.sync_names is True,
   "and reaches the stack tool straight away")

opts.blend.setCurrentIndex(opts.blend.findData("MULTIPLY"))
ok(win.saved >= 2, "the blend combo saves too")
ok(page.stack.default_blend == "MULTIPLY",
   "and reaches the stack (%s)" % page.stack.default_blend)

# ==================================================== the stack uses them
page.load_settings({"sync_names": True, "auto_blend": False,
                    "default_blend": "SUBTRACT"})
ok(page.stack.chk_autoblend.isChecked() is False,
   "auto blend is restored onto the Load row")
ok(page.settings_now() == {"auto_blend": False},
   "and the page reports it back for saving (%s)" % page.settings_now())

mark = len(stub.calls)
page.stack.add_layer()
sent = [kw for n, kw in stub.calls[mark:] if n == "add"]
ok(sent and sent[0].get("blend_type") == "SUBTRACT",
   "New Layer uses the default blend type (%s)" % sent)

page.stack.apply_status(status([layer_row(0, "Base")]))
from PySide6.QtWidgets import QInputDialog     # noqa: E402
_orig_text = QInputDialog.getText
QInputDialog.getText = staticmethod(lambda *a, **k: ("Renamed", True))
mark = len(stub.calls)
page.stack.rename_layer(0)
sent = [kw for n, kw in stub.calls[mark:] if n == "rename"]
ok(sent and sent[0].get("sync_action") is True,
   "a rename carries the Sync Names setting (%s)" % sent)
page.stack.sync_names = False
mark = len(stub.calls)
page.stack.rename_layer(0)
sent = [kw for n, kw in stub.calls[mark:] if n == "rename"]
ok(sent and sent[0].get("sync_action") is False,
   "and carries it when the setting is off (%s)" % sent)
page.stack.sync_names = True
QInputDialog.getText = _orig_text

# ==================================================== auto name-sync
stub2 = StubBridge([layer_row(0, "Base", action="Base"),
                    layer_row(1, "Layer 2", action="Walk Cycle")])
page2 = anim_layers.LayersPage(stub2, win)
page2.load_settings({"sync_names": False, "auto_blend": True,
                     "default_blend": "COMBINE"})
mark = len(stub2.calls)
page2.stack.apply_status(status(stub2.rows, active=1))
ok(not [n for n, _ in stub2.calls[mark:] if n == "sync_names"],
   "with the setting OFF a name mismatch is left alone")

page2.load_settings({"sync_names": True, "auto_blend": True,
                     "default_blend": "COMBINE"})
mark = len(stub2.calls)
page2.stack.apply_status(status(stub2.rows, active=1))
ok([n for n, _ in stub2.calls[mark:] if n == "sync_names"],
   "with it ON the mismatch triggers a sync (%s)"
   % [n for n, _ in stub2.calls[mark:]])

mark = len(stub2.calls)
page2.stack.apply_status(status(stub2.rows, active=1))
ok(not [n for n, _ in stub2.calls[mark:] if n == "sync_names"],
   "the same mismatch is only attempted ONCE (no poll loop)")

# a shared action must never be chased
stub3 = StubBridge([layer_row(0, "Base", action="Shared", action_users=2),
                    layer_row(1, "Layer 2", action="Shared", action_users=2)])
page3 = anim_layers.LayersPage(stub3, win)
page3.load_settings({"sync_names": True, "auto_blend": True,
                     "default_blend": "COMBINE"})
mark = len(stub3.calls)
page3.stack.apply_status(status(stub3.rows, active=1))
ok(not [n for n, _ in stub3.calls[mark:] if n == "sync_names"],
   "layers sharing one action are never auto-renamed")

# a locked layer is left alone too
stub4 = StubBridge([layer_row(0, "Base", action="Other", lock=True)])
page4 = anim_layers.LayersPage(stub4, win)
page4.load_settings({"sync_names": True, "auto_blend": True,
                     "default_blend": "COMBINE"})
mark = len(stub4.calls)
page4.stack.apply_status(status(stub4.rows, active=0))
ok(not [n for n, _ in stub4.calls[mark:] if n == "sync_names"],
   "a locked layer is never auto-renamed")

print("\n%d passed, %d failed" % (len(PASS), len(FAIL)), flush=True)
for f in FAIL:
    print("  FAILED: " + f, flush=True)
sys.exit(1 if FAIL else 0)
