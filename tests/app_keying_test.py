# The Set Keyframe / Remove Keyframe buttons in the Anim Layers tab (Marty,
# 2026-08-05).
#
#   python tests\app_keying_test.py
#
# ⚠ The thing worth guarding here is not that the buttons exist. It is that they
# never DECIDE anything: the channels are Blender's choice, the app only relays
# them, and the status line has to report what came back rather than what was
# asked for. A message written from the request would read "Keyed 1 object
# (Location, Rotation, Scale)" even on a rig where the user's keying set only
# touches rotation.
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

sys.path.insert(0, os.path.join(_ROOT, "app"))

from PySide6.QtWidgets import QApplication  # noqa: E402

import anim_layers  # noqa: E402
import bridge as bridgemod  # noqa: E402

PASS, FAIL = [], []


def ok(cond, label):
    (PASS if cond else FAIL).append(label)
    print(("ok   " if cond else "FAIL ") + label, flush=True)


EMPTY = {"error": None, "layers": [], "object": "Rig", "mode": "POSE",
         "solo": None, "active_index": None, "data_type": "OBJECT"}


class StubBridge:
    """A second implementation of the bridge interface - keep it in step when
    bridge.py grows a parameter (docs\\testing.md)."""

    def __init__(self, reason=None):
        self.reason = reason
        self.calls = []
        self.reply = dict(EMPTY)

    def anim_layers_status(self, **kw):
        return dict(self.reply)

    def anim_layers_actions(self, **kw):
        return []

    def anim_layers_key_selection(self, **kw):
        self.calls.append(kw)
        out = dict(self.reply)
        out["keyed"] = self.keyed
        return out

    def feature_reason(self, feature):
        return self.reason if feature == "anim_layers_keying" else None

    keyed = {"deleted": False, "keying_set": None,
             "channels": ["LOCATION", "ROTATION"], "objects": 1, "bones": 3,
             "frame": 12}


app = QApplication.instance() or QApplication([])

# ---------------------------------------------------------- the two buttons
bridge = StubBridge()
stack = anim_layers.LayerStackTool(bridge, None)
ok(stack.btn_key.text() == "Set Keyframe"
   and stack.btn_unkey.text() == "Remove Keyframe",
   "keys: both buttons are there, and the second one is the un-key Marty asked "
   "for")
ok(stack.btn_key.isEnabled() and stack.btn_unkey.isEnabled(),
   "keys: enabled against an add-on that answers")
ok("Default Key Channels" in stack.btn_key.toolTip(),
   "keys: the tooltip says whose settings decide - that is the ask, in words")

stack.key_selection(False)
ok(bridge.calls and bridge.calls[-1].get("delete") is False,
   "keys: Set sends delete=False")
ok(bridge.calls[-1].get("data_type") == "OBJECT",
   "keys: aimed at the stack on show, like every other command in this tool")
stack.key_selection(True)
ok(bridge.calls[-1].get("delete") is True, "keys: Remove sends delete=True")
ok(len(bridge.calls) == 2,
   "keys: one press, one command - nothing is sent twice")

# ⚠ No layer has to be selected. Keys land wherever Blender is already keying
# (the layer in NLA tweak mode), so demanding a selection would refuse a
# perfectly valid press.
ok(stack._selected_index() is None,
   "keys: nothing is selected in the list for this check")
ok(len(bridge.calls) == 2 and "Select a layer" not in stack.status.text(),
   "keys: and it went through anyway - the layer list is not a gate on this")

# ------------------------------------------------------------ the message
msg = stack._key_message(dict(StubBridge.keyed))
ok("1 object" in msg and "3 bones" in msg,
   "message: says what was keyed (%r)" % msg)
ok("frame 12" in msg, "message: and at which frame")
ok("Location" in msg and "Rotation" in msg and "Scale" not in msg,
   "message: naming the channels BLENDER reported, not a fixed list we made up")
named = dict(StubBridge.keyed)
named.update({"keying_set": "Whole Character", "channels": []})
ok("Whole Character" in stack._key_message(named),
   "message: an active keying set is named instead")
removed = dict(StubBridge.keyed)
removed["deleted"] = True
text = stack._key_message(removed)
ok(text.startswith("Removed") and "Location" not in text,
   "message: a REMOVAL never lists channels - Alt+I clears every channel at the "
   "frame, so naming the keying set's would be a lie (%r)" % text)
shape = {"deleted": False, "shape_key": "Smile", "channels": ["Value"],
         "objects": 1, "bones": 0, "frame": 3}
ok("Smile" in stack._key_message(shape),
   "message: the shape-key stack names the key it touched")

# ------------------------------------------------------- the capability gate
blocked = StubBridge(reason="needs 0.14.0")
old = anim_layers.LayerStackTool(blocked, None)
ok(not old.btn_key.isEnabled() and not old.btn_unkey.isEnabled(),
   "gate: an older add-on switches the two buttons off")
ok(old.btn_key.toolTip() == "needs 0.14.0",
   "gate: with the reason ON the control, not buried in a log")
ok("newer add-on" in old.key_hint.text(),
   "gate: and the hint next to them changes too")
old.key_selection(False)
ok(not blocked.calls,
   "gate: pressing it anyway sends nothing - a doomed request is worse than a "
   "refusal")
ok("0.14.0" in old.status.text(), "gate: and it says why")
# ⚠ EVERYTHING ELSE IN THE TAB STILL WORKS. The compatibility contract is that
# an old add-on costs the FEATURE, never the tab.
ok(old.btn_new.isEnabled() and old.btn_del.isEnabled() and old.list.isEnabled(),
   "gate: the rest of the layer stack is untouched")


class NoFeatureReason:
    """An older stub bridge, from before feature_reason existed."""

    def anim_layers_status(self, **kw):
        return dict(EMPTY)

    def anim_layers_actions(self, **kw):
        return []


tolerant = anim_layers.LayerStackTool(NoFeatureReason(), None)
ok(tolerant.btn_key.isEnabled(),
   "gate: a bridge that cannot even be ASKED fails OPEN - unknown is not "
   "'missing', and this is what keeps the older test stubs working")

# --------------------------------------------------------- the declaration
req = bridgemod.FEATURE_REQUIREMENTS["anim_layers_keying"]
ok(req[0] == "anim_layers_key_selection" and req[1] == "0.14.0",
   "contract: the feature is declared against the command and the version that "
   "introduced it")
ok(hasattr(bridgemod.Bridge, "anim_layers_key_selection"),
   "contract: and the client method exists")
# ⚠ Compared as a TUPLE. Pinning the string here failed the moment the next
# feature bumped the add-on, for no real reason - the same trap docs\testing.md
# already records, walked straight into.
ok(bridgemod.version_tuple(bridgemod.EXPECTED_ADDON_VERSION) >= (0, 14, 0),
   "contract: the app expects an add-on new enough to have it (got %s)"
   % bridgemod.EXPECTED_ADDON_VERSION)

print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
for label in FAIL:
    print("  FAILED: " + label)
sys.exit(1 if FAIL else 0)
