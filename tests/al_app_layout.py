# Layout check: the Layers rail entry holds BOTH the stack and the tools,
# and there is no separate "Layer Tools" entry any more.
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
    def anim_layers_status(self, **kw):
        return {"error": None, "layers": []}

    def anim_layers_actions(self, **kw):
        return []


app = QApplication.instance() or QApplication([])
page = anim_layers.LayersPage(StubBridge(), None)

ok(isinstance(page.stack, anim_layers.LayerStackTool),
   "page exposes the layer stack")
ok(isinstance(page.tools, anim_layers.LayerToolsTool),
   "page exposes the layer tools")
ok(isinstance(page.merge_bake, anim_layers.MergeBakeTool),
   "page exposes Merge / Bake (moved off the rail, 2026-08-05)")
ok(page.stack.parent() is page and page.tools.parent() is page
   and page.merge_bake.parent() is page,
   "all three are children of the ONE page")

# ⚠ Marty asked for it "right inbetween layers and Layer tools" — an ORDER, not
# just a location, so the order is what gets asserted.
lay0 = page.layout()
ok(lay0.indexOf(page.stack) < lay0.indexOf(page.merge_bake)
   < lay0.indexOf(page.tools),
   "Merge / Bake sits between the layer stack and Layer Tools")

# the stack's poll must still drive the tools' source dropdown
page.stack.apply_status({"error": None, "active_index": 1,
                         "layers": [{"index": 0, "name": "Base Layer",
                                     "mute": False, "lock": False,
                                     "solo": False, "locked_reason": None,
                                     "blend_type": "REPLACE",
                                     "influence": 1.0,
                                     "animated_influence": False,
                                     "keys": 4},
                                    {"index": 1, "name": "Layer 2",
                                     "mute": False, "lock": False,
                                     "solo": False, "locked_reason": None,
                                     "blend_type": "COMBINE",
                                     "influence": 1.0,
                                     "animated_influence": False,
                                     "keys": 2}],
                         "object": "Rig", "mode": "POSE", "solo": None})
items = [page.tools.source_combo.itemText(i)
         for i in range(page.tools.source_combo.count())]
ok(items == ["Base Layer"],
   "one poll feeds the tools' dropdown, active layer excluded (got %s)"
   % items)

# stack takes the stretch so the list grows, tools keep natural height
lay = page.layout()
ok(lay.stretch(lay.indexOf(page.stack)) == 1,
   "the layer list absorbs spare height")
ok(lay.stretch(lay.indexOf(page.tools)) == 0,
   "the tools block keeps its natural height")

# capture-busy greys every section, Merge / Bake included — it is the one that
# starts a long bake, so leaving it live while Blender renders would be the
# worst of the three to miss
page.set_capture_busy(True)
ok(not page.stack.isEnabled() and not page.tools.isEnabled()
   and not page.merge_bake.isEnabled(),
   "capture-busy disables every section")
page.set_capture_busy(False)
ok(page.stack.isEnabled() and page.tools.isEnabled()
   and page.merge_bake.isEnabled(),
   "and re-enables them")

print("\n%d passed, %d failed" % (len(PASS), len(FAIL)), flush=True)
sys.exit(1 if FAIL else 0)
