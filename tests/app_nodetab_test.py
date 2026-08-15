# Node Setup tab, offscreen: rail order in the RenderingPage shell, both
# tools passing their options over a stub bridge, greying logic, error
# handling, and the bridge-busy gate.
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

sys.path.insert(0, os.path.join(_ROOT, "app"))

from PySide6.QtWidgets import QApplication  # noqa: E402

import bridge as bridgemod  # noqa: E402
import node_tools  # noqa: E402
import rendering as renderingmod  # noqa: E402

PASS = []
FAIL = []


def ok(cond, label):
    (PASS if cond else FAIL).append(label)
    print(("ok   " if cond else "FAIL ") + label, flush=True)


class StubBridge:
    def __init__(self):
        self.calls = []
        self.status_reply = {"editors": [],
                             "relink": {"error": "nothing selected"},
                             "sequence": {"error": "no node"}}
        self.raise_error = False

    def node_tools_status(self, **kw):
        self.calls.append(("node_tools_status", kw))
        if self.raise_error:
            raise bridgemod.BridgeError("bridge down")
        return self.status_reply

    def relink_nodes(self, **kw):
        self.calls.append(("relink_nodes", kw))
        if self.raise_error:
            raise bridgemod.BridgeError("bridge down")
        return {"made": 2, "source": "A", "targets": ["B"], "tree": "T",
                "tree_type": "ShaderNodeTree", "missing": []}

    def setup_image_sequence(self, **kw):
        self.calls.append(("setup_image_sequence", kw))
        return {"node": "Image", "count": 6, "first": 5, "last": 10,
                "scene_start": 1, "scene_end": 6, "range_set": True,
                "output": "//Render/x", "notes": ["frames 5-10 have gaps"]}


app = QApplication.instance() or QApplication([])
stub = StubBridge()

# --- rail order in the shared shell ----------------------------------------
page = renderingmod.RenderingPage(stub, None)
page.add_tool(node_tools.RelinkTool(stub, None), "Relink", group="Nodes")
page.add_tool(node_tools.SequenceSetupTool(stub, None),
              "Image Sequence Setup", group="Nodes")
rail = page.rail if hasattr(page, "rail") else None
labels = []
if rail is not None:
    for i in range(rail.topLevelItemCount()):
        top = rail.topLevelItem(i)
        labels.append(top.text(0))
        for j in range(top.childCount()):
            labels.append("  " + top.child(j).text(0))
ok(labels == ["NODES", "  Relink", "  Image Sequence Setup"],
   "rail: NODES group (shell uppercases headers) with both tools in order "
   "(got %s)" % labels)

# --- Relink option passing --------------------------------------------------
relink = node_tools.RelinkTool(stub, None)
relink.chk_fallback.setChecked(True)
relink.chk_inputs.setChecked(True)
stub.calls.clear()
relink.run()
name, kw = stub.calls[-1]
ok(name == "relink_nodes"
   and kw == {"match_mode": "NAME", "index_fallback": True,
              "copy_inputs": True},
   "Relink passes NAME + fallback + copy_inputs (got %s)" % (kw,))

relink.match.setCurrentIndex(1)      # Position
ok(not relink.chk_fallback.isEnabled(),
   "fallback checkbox greys out in Position mode")
stub.calls.clear()
relink.run()
_, kw = stub.calls[-1]
ok(kw["match_mode"] == "INDEX", "Position mode sends INDEX")
ok("Relinked 2" in relink.status.text(),
   "success lands in the status line")

# --- Relink refresh: error text vs preview ---------------------------------
stub.calls.clear()
relink.refresh()
ok(stub.calls[-1][0] == "node_tools_status", "Check selection is a pure read")
ok(relink.status.text() == "nothing selected",
   "relink preview error shown as-is")
stub.status_reply = {"editors": [], "sequence": {"error": "no node"},
                     "relink": {"error": None, "tree": "T",
                                "tree_type": "ShaderNodeTree", "source": "A",
                                "links": 2, "targets": ["B", "C"]}}
relink.refresh()
ok("From: A (2 links)" in relink.status.text()
   and "To: B, C" in relink.status.text(), "relink preview formatted")

# --- BridgeError -> red status, no crash ------------------------------------
stub.raise_error = True
relink.run()
ok("bridge down" in relink.status.text()
   and "e06c60" in relink.status.styleSheet(),
   "BridgeError shown red")
stub.raise_error = False

# --- bridge-busy gate: run() must not touch the bridge ----------------------
class BusyWindow:
    def bridge_free_for_tools(self):
        return False

    def statusBar(self):
        raise AssertionError("should not be reached")

    def update_bridge_status(self):
        pass


gated = node_tools.RelinkTool(stub, BusyWindow())
stub.calls.clear()
gated.run()
ok(stub.calls == [], "busy window: run() returns before calling the bridge")

# --- Sequence Setup option passing ------------------------------------------
seq = node_tools.SequenceSetupTool(stub, None)
stub.calls.clear()
seq.run()
_, kw = stub.calls[-1]
ok(kw == {"set_scene_range": True, "start_at_one": True, "set_output": True,
          "output_folder": "exr_composited",
          "output_suffix": "_exr_composited_"},
   "sequence defaults pass through (got %s)" % (kw,))
ok("Image: 6 frames (5-10), scene 1-6" in seq.status.text()
   and "⚠ frames 5-10 have gaps" in seq.status.text(),
   "sequence result + notes in the status")

seq.chk_range.setChecked(False)
seq.chk_output.setChecked(False)
seq.folder.setText("   ")            # blank folder falls back
seq.suffix.setText("_x_")
ok(not seq.chk_start1.isEnabled(), "range off greys 'start at 1'")
ok(not seq.folder.isEnabled() and not seq.suffix.isEnabled(),
   "output off greys folder+suffix")
stub.calls.clear()
seq.run()
_, kw = stub.calls[-1]
ok(kw["set_scene_range"] is False and kw["set_output"] is False
   and kw["output_folder"] == "exr_composited"
   and kw["output_suffix"] == "_x_",
   "toggles + blank-folder fallback pass through (got %s)" % (kw,))

print("\n%d passed, %d failed" % (len(PASS), len(FAIL)), flush=True)
sys.exit(1 if FAIL else 0)
