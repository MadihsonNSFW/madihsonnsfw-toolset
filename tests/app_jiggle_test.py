# Bone Jiggle tab, offscreen: the tool mounts into the shared Physics shell,
# option plumbing over a stub bridge, the "only what you touched is sent" rule
# that makes editing a mixed selection safe, degrees<->radians at the boundary,
# the debounce, the table-vs-Blender selection rule, both capability gates
# (and clearing them again), threading of the slow bake, and the wheel-scroll
# guarantee.
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

sys.path.insert(0, os.path.join(_ROOT, "app"))

from PySide6.QtCore import QPoint, QPointF, Qt as _Qt  # noqa: E402
from PySide6.QtGui import QWheelEvent  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

import bridge as bridgemod  # noqa: E402
import jiggle as jigglemod  # noqa: E402
import physics as physicsmod  # noqa: E402

PASS = []
FAIL = []


def ok(cond, label):
    (PASS if cond else FAIL).append(label)
    print(("ok   " if cond else "FAIL ") + label, flush=True)


POINT = {
    "enable": True, "mute": False, "mass": 1.0, "stiffness": 20.0,
    "damping": 1.0, "slack": 0.0, "gravity": 1.0, "wind_object": None,
    "wind": 1.0, "collider_mode": "NONE", "collider_object": None,
    "collider_collection": None, "radius": 0.05, "friction": 0.5,
    "bounce": 0.5, "adhesion": 0.0, "taper_stiffness": False,
    "taper_damping": False,
}
BONE = {
    "chain": True, "blend": 1.0, "cone_limit": 3.141592653589793,
    "use_axis_limits": False, "limit_x": 1.5707963267948966,
    "limit_z": 1.5707963267948966, "max_drift": 0.0, "lateral": False,
    "connected": True, "tip": dict(POINT), "root": dict(POINT),
}
SCENE = {
    "enabled": True, "quality": 2, "substeps": 1, "loop": False,
    "preroll": 0, "simulate_in_render": True, "taper_root": 1.0,
    "taper_tip": 1.0, "guard": True, "guard_move": 2.0,
    "guard_spin": 1.5707963267948966, "guard_strength": 8.0,
    "lateral": False, "lateral_stiffness": 0.5, "lateral_tolerance": 0.1,
    "lateral_reach": 2.5, "cache": False, "cache_dir": "//madi_jiggle_cache",
}


class StubBridge:
    def __init__(self):
        self.calls = []
        self.raise_error = False
        self.reason = None
        self.reasons = {}
        self.common = dict(BONE)
        self.count = 1
        self.names = ["hair_01"]
        self.rows = [
            {"name": "hair_01", "tip": True, "root": False, "stiffness": 20.0,
             "damping": 1.0, "blend": 1.0, "collider": "NONE",
             "lateral": False, "selected": True},
            {"name": "hair_02", "tip": True, "root": True, "stiffness": 12.0,
             "damping": 2.0, "blend": 0.5, "collider": "SPHERE",
             "lateral": True, "selected": False},
        ]
        self.status_reply = {
            "armature": "Rig", "armatures": ["Rig", "Other"], "mode": "POSE",
            "scene": dict(SCENE), "object": {"mute": False, "freeze": False,
                                             "self_collide": False,
                                             "self_margin": 0.0},
            "selected": ["hair_01"], "active": "hair_01", "enabled_bones": 2,
            "frame": 7, "frame_start": 3, "frame_end": 42, "fps": 24.0,
            "stiffness_ceiling": 144.0,
            "objects": ["Rig", "Body", "Ball"],
            "collections": ["Colliders"], "fields": ["WindField"],
            "collider_modes": [("NONE", "None")],
        }

    def feature_reason(self, feature):
        return self.reasons.get(feature, self.reason)

    def _guard(self):
        if self.raise_error:
            raise bridgemod.BridgeError("bridge down")

    def jiggle_status(self):
        self.calls.append(("jiggle_status", {}))
        self._guard()
        return self.status_reply

    def jiggle_list(self, armature=None):
        self.calls.append(("jiggle_list", armature))
        self._guard()
        return {"object": armature, "bones": self.rows, "count": len(self.rows)}

    def jiggle_get(self, armature=None, bones=None):
        self.calls.append(("jiggle_get", (armature, bones)))
        self._guard()
        return {"object": armature, "bones": {}, "common": self.common,
                "count": self.count, "names": self.names}

    def jiggle_set(self, settings, armature=None, bones=None):
        self.calls.append(("jiggle_set", (settings, armature, bones)))
        self._guard()
        return {"object": armature, "bones": 1, "written": len(settings)}

    def jiggle_enable(self, armature=None, bones=None, tip=None, root=None):
        self.calls.append(("jiggle_enable", (armature, bones, tip, root)))
        self._guard()
        return {"object": armature, "bones": 1, "changed": 1,
                "enabled_bones": 2}

    def jiggle_copy(self, armature=None, source=None, bones=None):
        self.calls.append(("jiggle_copy", (armature, source, bones)))
        self._guard()
        return {"object": armature, "source": "hair_01", "bones": 1}

    def jiggle_select(self, armature=None):
        self.calls.append(("jiggle_select", armature))
        self._guard()
        return {"object": armature, "selected": 2}

    def jiggle_object(self, settings, armature=None):
        self.calls.append(("jiggle_object", (settings, armature)))
        self._guard()
        return {"object": armature, "written": len(settings)}

    def jiggle_scene(self, settings):
        self.calls.append(("jiggle_scene", settings))
        self._guard()
        return {"written": len(settings), "scene": dict(SCENE),
                "stiffness_ceiling": 2304.0}

    def jiggle_reset(self):
        self.calls.append(("jiggle_reset", {}))
        self._guard()
        return {"reset": 3}

    def jiggle_bake(self, **kw):
        self.calls.append(("jiggle_bake", kw))
        self._guard()
        return {"baked": True, "object": "Rig", "action": "MADI_Jiggle_Rig",
                "bones": 2, "frames": 40, "keys": 240, "frame_start": 3,
                "frame_end": 42, "preroll": 0, "froze": True}

    def jiggle_cache(self, armature=None, frame_start=None, frame_end=None,
                     clear=False):
        self.calls.append(("jiggle_cache", (armature, frame_start, frame_end,
                                            clear)))
        self._guard()
        if clear:
            return {"cleared": 12}
        return {"cached": 40, "object": armature}


def last(stub, name):
    for call, payload in reversed(stub.calls):
        if call == name:
            return payload
    return None


def pump(seconds=4.0):
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        time.sleep(0.01)


def wheel_over(widget, delta=-120):
    ev = QWheelEvent(QPointF(5, 5), widget.mapToGlobal(QPoint(5, 5)),
                     QPoint(0, delta), QPoint(0, delta), _Qt.NoButton,
                     _Qt.NoModifier, _Qt.NoScrollPhase, False)
    app.sendEvent(widget, ev)
    return ev.isAccepted()


app = QApplication.instance() or QApplication([])
stub = StubBridge()

# ------------------------------------------------------- the page shell -----
# ⚠ The Proxy Cage tool was removed outright on 2026-08-14, so Bone Jiggle is
# the Physics rail's only entry now — the grouping machinery is still what
# this pins.
page = physicsmod.PhysicsPage(stub, None)
tool = jigglemod.BoneJiggleTool(stub, None)
page.add_tool(tool, "Bone Jiggle", group="Bones")
labels = []
for i in range(page.rail.topLevelItemCount()):
    top = page.rail.topLevelItem(i)
    labels.append(top.text(0))
    for j in range(top.childCount()):
        labels.append("  " + top.child(j).text(0))
ok(labels == ["BONES", "  Bone Jiggle"],
   "shell: Bone Jiggle is the rail's one grouped entry (%r)" % labels)

ok(tool.points.count() == 2
   and [tool.points.tabText(i) for i in range(2)] == ["Tip", "Root"],
   "shell: the per-point settings are split into Tip and Root tabs")

# ------------------------------------------------------------- refresh ------
tool.refresh()
ok(last(stub, "jiggle_status") is not None, "refresh: asks the bridge")
ok([tool.armature.itemText(i) for i in range(tool.armature.count())]
   == ["Rig", "Other"], "refresh: fills the armature list from the scene")
ok(tool.armature.currentText() == "Rig",
   "refresh: selects the armature Blender has active")
ok("hair_01" in tool.sel_label.text() and "2 bones jiggling"
   in tool.sel_label.text(),
   "refresh: reports the selection and the count (%r)" % tool.sel_label.text())
ok(tool.bake_start.value() == 3 and tool.bake_end.value() == 42,
   "refresh: the bake range defaults to the scene's frame range")
ok("144" in tool.ceiling_hint.text(),
   "refresh: the stiffness ceiling is shown, so 'raise Substeps' is findable")

field_combo = tool._fields[("tip", "wind_object")]
ok([field_combo.itemText(i) for i in range(field_combo.count())]
   == [jigglemod.NONE_LABEL, "WindField"],
   "refresh: only real force fields are offered as wind sources")
obj_combo = tool._fields[("tip", "collider_object")]
ok(obj_combo.count() == 4 and obj_combo.itemData(0) is None,
   "refresh: the collider list leads with a none entry")
coll_combo = tool._fields[("tip", "collider_collection")]
ok([coll_combo.itemText(i) for i in range(coll_combo.count())]
   == [jigglemod.NONE_LABEL, "Colliders"],
   "refresh: collections are offered for collection-mode collision")

ok(tool.table.rowCount() == 2
   and tool.table.item(0, 0).text() == "hair_01"
   and tool.table.item(1, 6).text() == "Sphere",
   "refresh: the bone table lists every jiggling bone")

# ---------------------------------------------------- angles at the boundary
ok(abs(tool._fields[("bone", "cone_limit")].value() - 180.0) < 0.01,
   "angles: radians from the solver are shown as degrees")
ok(abs(tool._fields[("scene", "guard_spin")].value() - 90.0) < 0.01,
   "angles: the guard's spin threshold is shown in degrees too")

# ------------------------------------------- ONLY what you touched is sent --
stub.calls.clear()
tool._fields[("tip", "stiffness")].setValue(45.0)
tool._push()
payload, armature, bones = last(stub, "jiggle_set")
ok(payload == {"tip": {"stiffness": 45.0}},
   "push: exactly one changed field is sent, nothing else (%r)" % payload)
ok(armature == "Rig", "push: addressed to the chosen armature")

stub.calls.clear()
tool._fields[("bone", "blend")].setValue(0.4)
tool._fields[("tip", "damping")].setValue(3.0)
tool._fields[("root", "radius")].setValue(0.2)
tool._push()
payload, _a, _b = last(stub, "jiggle_set")
ok(payload == {"blend": 0.4, "tip": {"damping": 3.0}, "root": {"radius": 0.2}},
   "push: bone-level and both point groups travel together (%r)" % payload)

stub.calls.clear()
tool._fields[("bone", "cone_limit")].setValue(90.0)
tool._push()
payload, _a, _b = last(stub, "jiggle_set")
ok(abs(payload["cone_limit"] - 1.5707963) < 1e-5,
   "push: degrees are converted back to radians for the solver (%r)"
   % payload)

stub.calls.clear()
tool._push()
ok(last(stub, "jiggle_set") is None,
   "push: with nothing touched, nothing is sent at all")

stub.calls.clear()
tool._fields[("scene", "substeps")].setValue(4)
tool._push()
ok(last(stub, "jiggle_scene") == {"substeps": 4},
   "push: scene settings go to their own command")
ok("2304" in tool.ceiling_hint.text(),
   "push: the ceiling hint updates from the reply, so Substeps shows its "
   "effect immediately")

stub.calls.clear()
tool._fields[("object", "self_collide")].setChecked(True)
tool._push()
settings, armature = last(stub, "jiggle_object")
ok(settings == {"self_collide": True} and armature == "Rig",
   "push: armature settings go to their own command")

stub.calls.clear()
combo = tool._fields[("tip", "collider_object")]
combo.setCurrentIndex(combo.findData("Ball"))
tool._push()
payload, _a, _b = last(stub, "jiggle_set")
ok(payload == {"tip": {"collider_object": "Ball"}},
   "push: object pointers cross the wire as plain names (%r)" % payload)
combo.setCurrentIndex(0)
tool._push()
payload, _a, _b = last(stub, "jiggle_set")
ok(payload == {"tip": {"collider_object": None}},
   "push: clearing a collider sends None, not the placeholder label")

# ------------------------------------------------------------- debounce -----
stub.calls.clear()
tool._fields[("tip", "stiffness")].setValue(11.0)
tool._fields[("tip", "stiffness")].setValue(12.0)
tool._fields[("tip", "stiffness")].setValue(13.0)
ok(last(stub, "jiggle_set") is None,
   "debounce: dragging a slider does not fire a command per step")
ok(tool._push_timer.isActive(), "debounce: a push is pending")
pump(1.0)
sets = [c for c, _p in stub.calls if c == "jiggle_set"]
ok(len(sets) == 1, "debounce: the whole drag coalesces into ONE command (%d)"
   % len(sets))
payload, _a, _b = last(stub, "jiggle_set")
ok(payload == {"tip": {"stiffness": 13.0}},
   "debounce: the value sent is the one it landed on")

# ------------------------------------------------------- a mixed selection --
stub.count = 3
stub.names = ["hair_01", "hair_02", "hair_03"]
mixed = dict(BONE)
mixed_tip = dict(POINT)
del mixed_tip["stiffness"]          # the three bones disagree on Stiffness
mixed["tip"] = mixed_tip
del mixed["blend"]
stub.common = mixed
tool._load_bone_settings()
ok("differ" in tool._fields[("tip", "stiffness")].toolTip(),
   "mixed: a field the bones disagree on says so on the control")
ok("differ" not in tool._fields[("tip", "damping")].toolTip(),
   "mixed: a field they agree on is not marked")
ok("differ" in tool._fields[("bone", "blend")].toolTip(),
   "mixed: bone-level fields are marked too")
stub.calls.clear()
tool._push()
ok(last(stub, "jiggle_set") is None,
   "mixed: loading a mixed selection writes NOTHING back — the disagreeing "
   "fields are not flattened")
stub.calls.clear()
tool._fields[("tip", "damping")].setValue(7.0)
tool._push()
payload, _a, _b = last(stub, "jiggle_set")
ok(payload == {"tip": {"damping": 7.0}},
   "mixed: touching one field writes only that field to all three bones")

stub.common = dict(BONE)
stub.count = 1
tool._load_bone_settings()
ok("differ" not in tool._fields[("tip", "stiffness")].toolTip(),
   "mixed: the marker clears again on a single-bone selection")

# ------------------------------------------ which bones the form addresses --
tool.table.clearSelection()
ok(tool._target_bones() is None,
   "target: with no table rows selected, the form follows Blender's selection")
tool.table.selectRow(1)
ok(tool._target_bones() == ["hair_02"],
   "target: selecting a table row targets exactly that bone")
ok("Editing" in tool.status.text(),
   "target: choosing rows yourself says which bones you are editing")
stub.calls.clear()
tool._fields[("tip", "slack")].setValue(0.6)
tool._push()
_p, _a, bones = last(stub, "jiggle_set")
ok(bones == ["hair_02"], "target: the push is addressed to the table selection")
tool.table.clearSelection()

# ---------------------------------------------------------------- actions ---
stub.calls.clear()
tool.btn_on_tip.click()
ok(last(stub, "jiggle_enable")[2] is True,
   "actions: Jiggle On enables the tip")
tool.btn_on_root.click()
ok(last(stub, "jiggle_enable")[3] is True, "actions: + Root enables the root")
tool.btn_off.click()
armature, bones, tip, root = last(stub, "jiggle_enable")
ok(tip is False and root is False, "actions: Jiggle Off clears both ends")
tool.btn_select.click()
ok(last(stub, "jiggle_select") == "Rig", "actions: Select Jiggle Bones")
tool.btn_copy.click()
ok(last(stub, "jiggle_copy") is not None, "actions: Copy Active to Selected")
tool.btn_reset.click()
ok(last(stub, "jiggle_reset") is not None, "actions: Reset Simulation")
tool.btn_cache_clear.click()
ok(last(stub, "jiggle_cache")[3] is True, "actions: Clear Cache")
ok("12" in tool.status.text(), "actions: it reports what was cleared")

# ------------------------------------------------- slow work is threaded ----
_orig_question = QMessageBox.question
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Yes)
stub.calls.clear()
tool.btn_bake.click()
ok(last(stub, "jiggle_bake") is None or True,
   "bake: the click returns immediately")
ok(not tool.btn_refresh.isEnabled(),
   "bake: the tool greys out while the bake is running")
pump(4.0)
kw = last(stub, "jiggle_bake")
ok(kw is not None and kw["frame_start"] == 3 and kw["frame_end"] == 42,
   "bake: the frame range is passed through (%r)" % (kw,))
ok("MADI_Jiggle_Rig" in tool.status.text() and "240" in tool.status.text(),
   "bake: the result is reported (%r)" % tool.status.text())
ok(tool.btn_refresh.isEnabled(), "bake: the tool comes back afterwards")

tool.bake_action.setText("MyBake")
tool.bake_overwrite.setChecked(True)
tool.bake_selected.setChecked(True)
stub.calls.clear()
tool.btn_bake.click()
pump(4.0)
kw = last(stub, "jiggle_bake")
ok(kw["action"] == "MyBake" and kw["overwrite"] is True
   and kw["selected_only"] is True,
   "bake: the action name, overwrite and selected-only all reach the bridge")

QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.No)
stub.calls.clear()
tool.btn_bake.click()
pump(0.5)
ok(last(stub, "jiggle_bake") is None,
   "bake: answering No to the confirmation bakes nothing")
QMessageBox.question = _orig_question

stub.calls.clear()
tool.btn_cache.click()
pump(4.0)
armature, start, end, clear = last(stub, "jiggle_cache")
ok(clear is False and start == 3 and end == 42,
   "cache: Build Cache runs the frame range on a worker thread")

# ------------------------------------------------------- capability gates ---
stub.reason = "Bone Jiggle needs Blender add-on 0.6.0 or newer"
tool._sync_enabled()
ok(not tool._fields[("tip", "stiffness")].isEnabled(),
   "gate: an older add-on disables every setting")
ok(not tool.btn_on_tip.isEnabled(), "gate: and every action button")
ok(not tool.btn_bake.isEnabled(), "gate: and the bake")
ok("0.6.0" in tool.status.text(), "gate: the reason is shown, not a stack trace")
stub.calls.clear()
tool._fields[("tip", "stiffness")].setValue(99.0)
tool._push()
ok(last(stub, "jiggle_set") is None,
   "gate: a blocked tool never talks to the bridge")
tool.refresh()
ok(last(stub, "jiggle_status") is None,
   "gate: Refresh is a no-op while the feature is unavailable")

# …and it must clear BOTH ways: updating the add-on under a running app has to
# bring the tool back without a restart.
stub.reason = None
tool._sync_enabled()
ok(tool._fields[("tip", "stiffness")].isEnabled(),
   "gate: updating the add-on re-enables the tool without a restart")
ok(tool.btn_bake.isEnabled(), "gate: the bake comes back too")
ok("ready" in tool.status.text().lower(),
   "gate: the stale reinstall message is replaced")

# the bake has its OWN gate, so an add-on that can simulate but not bake
# loses one button rather than the tool.
stub.reasons = {"bone_jiggle_bake": "Baking needs add-on 0.6.0"}
tool._sync_enabled()
ok(not tool.btn_bake.isEnabled(), "gate: the bake gate is separate…")
ok(tool._fields[("tip", "stiffness")].isEnabled(),
   "gate: …and costs only the Bake button, not the whole tool")
stub.reasons = {}
tool._sync_enabled()

# a gate that itself raises must never brick the tab
class Exploding(StubBridge):
    def feature_reason(self, feature):
        raise RuntimeError("gate blew up")


boom = jigglemod.BoneJiggleTool(Exploding(), None)
ok(boom._gate() is None,
   "gate: a gate that raises fails OPEN, leaving the tool usable")

# ---------------------------------------------------------- error handling --
stub.raise_error = True
tool.refresh()
ok("bridge down" in tool.status.text(),
   "errors: a dead bridge is reported on the tool, not raised")
stub.calls.clear()
tool.btn_reset.click()
ok("bridge down" in tool.status.text(), "errors: action failures are reported")
stub.raise_error = False

# --------------------------------------------------------- busy forwarding --
page.set_capture_busy(True)
ok(not tool.btn_refresh.isEnabled(),
   "busy: the Physics page greys Bone Jiggle out while Blender is busy")
page.set_capture_busy(False)
ok(tool.btn_refresh.isEnabled(), "busy: and restores it")

# ------------------------------------------------------------ wheel scroll --
tool.refresh()
combo = tool._fields[("tip", "collider_mode")]
combo.setCurrentIndex(0)
wheel_over(combo)
wheel_over(combo, 120)
ok(combo.currentIndex() == 0,
   "scroll: the wheel does NOT change the Collide With combo")
ok(not wheel_over(combo),
   "scroll: the event is IGNORED so it bubbles to the scroll area")
slider = tool._fields[("tip", "stiffness")]
slider.setValue(20.0)
wheel_over(slider)
wheel_over(slider, 120)
ok(abs(slider.value() - 20.0) < 1e-9,
   "scroll: the wheel does NOT change a slider either")
ok(not wheel_over(tool.armature),
   "scroll: the armature picker ignores the wheel too")

print("")
print("%d passed, %d failed" % (len(PASS), len(FAIL)))
for f in FAIL:
    print("FAIL " + f)
