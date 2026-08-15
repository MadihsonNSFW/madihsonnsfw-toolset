# Bone picker tab, offscreen: the poll, the list-rebuild guard, retargeting,
# the debounce, the capability gate and the busy greying.
#
#   python tests\app_picker_test.py
#
# The tab is a MANAGER (Marty's call) - the canvas stays in Blender's Image
# Editor - so everything here is about reading a status and pushing edits back
# without echoing, stomping or flooding.
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

sys.path.insert(0, os.path.join(_ROOT, "app"))

from PySide6.QtWidgets import QApplication  # noqa: E402

import bridge as bridgemod  # noqa: E402
import picker as pickermod  # noqa: E402

PASS = []
FAIL = []


def ok(cond, label):
    (PASS if cond else FAIL).append(label)
    print(("ok   " if cond else "FAIL ") + label, flush=True)


def status(**over):
    """A plausible picker_status reply."""
    base = {
        "running": False,
        "active_index": 0,
        "active_uid": 0,
        "armature": "Lily Rig",
        "tabs": [
            {"index": 0, "name": "Body", "uid": 0, "armature": "Lily Rig",
             "image": "ref.png"},
            {"index": 1, "name": "Face", "uid": 1, "armature": "Lily Rig",
             "image": None},
        ],
        "buttons": [
            {"index": 0, "kind": "BONE", "label": "ROOT", "bone": "root",
             "members": [], "sk_object": "", "sk_key": "", "x": 0.5, "y": 0.5,
             "w": 0.04, "h": 0.04, "color": [1.0, 0.0, 0.0], "scale": 1.0,
             "blank": False, "picked": False, "tab_uid": 0, "missing": []},
            {"index": 1, "kind": "BONE", "label": "GONE", "bone": "nope",
             "members": [], "sk_object": "", "sk_key": "", "x": 0.5, "y": 0.5,
             "w": 0.04, "h": 0.04, "color": [1.0, 1.0, 1.0], "scale": 1.0,
             "blank": False, "picked": False, "tab_uid": 0,
             "missing": ["nope"]},
            {"index": 2, "kind": "GROUP", "label": "arm", "bone": "",
             "members": ["shoulder", "bad_member"], "sk_object": "",
             "sk_key": "", "x": 0.5, "y": 0.5, "w": 0.04, "h": 0.04,
             "color": [0.0, 1.0, 0.0], "scale": 1.0, "blank": False,
             "picked": False, "tab_uid": 0, "missing": ["bad_member"]},
            {"index": 3, "kind": "SLIDER", "label": "smile", "bone": "",
             "members": [], "sk_object": "Body Mesh", "sk_key": "smile",
             "x": 0.5, "y": 0.5, "w": 0.1, "h": 0.02,
             "color": [0.0, 0.0, 1.0], "scale": 1.0, "blank": False,
             "picked": False, "tab_uid": 0, "missing": []},
        ],
        "active_button": 0,
        "armatures": ["Lily Rig", "Other Rig"],
        "images": ["ref.png", "face.png"],
        "bones": ["root", "shoulder", "spine"],
        "meshes": {"Body Mesh": ["Basis", "smile", "frown"]},
        "brushes": {"color": [1.0, 1.0, 1.0], "scale": 1.0, "gap": 0.25,
                    "blank": False},
        "prefs": {"btn_alpha": 100.0, "btn_round": 7.0, "bg_darken": 60.0},
        "unmatched": 2,
    }
    base.update(over)
    return base


class StubBridge:
    def __init__(self):
        self.calls = []
        self.reply = status()
        self.raise_error = False
        self.reason = None

    def feature_reason(self, feature):
        return self.reason

    def _record(self, name, *a, **kw):
        self.calls.append((name, a, kw))
        if self.raise_error:
            raise bridgemod.BridgeError("bridge down")
        return self.reply

    def __getattr__(self, name):
        if not name.startswith("picker_"):
            raise AttributeError(name)
        return lambda *a, **kw: self._record(name, *a, **kw)

    def named(self, name):
        return [c for c in self.calls if c[0] == name]


class StubWindow:
    capturing = False

    def bridge_free_for_tools(self):
        return True


app = QApplication.instance() or QApplication([])
stub = StubBridge()
win = StubWindow()

# ------------------------------------------------------------------- the shell
page = pickermod.PickerPage(stub, win)
tabs_tool = pickermod.PickerTabsTool(stub, win)
buttons_tool = pickermod.PickerButtonsTool(stub, win)
options_tool = pickermod.PickerOptionsTool(stub, win)
page.add_tool(tabs_tool, "Tabs & Rig", group="Picker")
page.add_tool(buttons_tool, "Buttons", group="Picker")
page.add_tool(options_tool, "Appearance", group="Picker")
for t in (buttons_tool, options_tool):
    tabs_tool.status_refreshed.connect(t.apply_status)

titles = [t for t, _g, _w in page._tools]
ok(titles == ["Tabs & Rig", "Buttons", "Appearance"],
   "shell: the rail lists the three tools in add order (got %s)" % titles)
ok(page.EMPTY_TEXT != pickermod.RenderingPage.EMPTY_TEXT,
   "shell: the picker tab has its own empty-state text")

# ------------------------------------------------------------------ the poll
tabs_tool.refresh(polling=True)
calls = stub.named("picker_status")
ok(len(calls) == 1, "poll: one status call")
ok(calls[0][2].get("poll") is True,
   "poll: the TIMER path passes poll=True - a dead localhost port drops the "
   "SYN here, so an un-flagged poll burns the full timeout on the GUI thread")
tabs_tool.refresh()
ok(stub.named("picker_status")[1][2].get("poll") is False,
   "poll: a deliberate refresh does NOT pass poll=True, so a user's click "
   "still tries even while the bridge is marked unreachable")

# ------------------------------------------------------------ the tab list
ok(tabs_tool.table.rowCount() == 2, "tabs: both rows land")
ok(tabs_tool.table.item(0, 0).text() == "Body"
   and tabs_tool.table.item(0, 1).text() == "Lily Rig"
   and tabs_tool.table.item(0, 2).text() == "ref.png",
   "tabs: name, rig and background are shown")
ok(tabs_tool.table.item(1, 2).text() == "—",
   "tabs: a tab with no background says so rather than showing a blank")

# ⚠ THE ECHO GUARD. Rebuilding the table emits itemSelectionChanged; without
# the guard that goes straight back to Blender as a tab switch.
before = len(stub.named("picker_set_tab"))
tabs_tool.apply_status(status())
ok(len(stub.named("picker_set_tab")) == before,
   "tabs: rebuilding the list does NOT echo back a tab switch")

# A real click does switch.
tabs_tool.table.selectRow(1)
sw = stub.named("picker_set_tab")
ok(len(sw) == before + 1 and sw[-1][1][0] == 1,
   "tabs: selecting row 1 switches to tab 1")

# Re-selecting the tab that is already active is a no-op.
stub.reply = status(active_index=1)
tabs_tool.apply_status(stub.reply)
n = len(stub.named("picker_set_tab"))
tabs_tool.table.selectRow(1)
ok(len(stub.named("picker_set_tab")) == n,
   "tabs: re-selecting the ACTIVE tab costs nothing")
stub.reply = status()
tabs_tool.apply_status(stub.reply)

# ---------------------------------------------------------- rig / background
ok([tabs_tool.combo_rig.itemText(i)
    for i in range(tabs_tool.combo_rig.count())][1:] == ["Lily Rig", "Other Rig"],
   "tabs: the rig combo offers every armature in the scene")
ok(tabs_tool.combo_rig.currentText() == "Lily Rig",
   "tabs: and shows the one this tab uses")
tabs_tool.combo_rig.setCurrentIndex(2)
tabs_tool._on_rig(2)
ok(stub.named("picker_set_tab_rig")[-1][1][0] == "Other Rig",
   "tabs: choosing another rig pushes it")
tabs_tool.combo_rig.setCurrentIndex(0)
tabs_tool._on_rig(0)
ok(stub.named("picker_set_tab_rig")[-1][1][0] is None,
   "tabs: choosing the empty row clears the rig (None, not the label text)")

# ------------------------------------------------------------- the session
ok(tabs_tool.btn_start.isEnabled() and not tabs_tool.btn_stop.isEnabled(),
   "session: stopped -> Start live, Stop dead")
tabs_tool.apply_status(status(running=True))
ok(not tabs_tool.btn_start.isEnabled() and tabs_tool.btn_stop.isEnabled(),
   "session: running -> the other way round")
tabs_tool.apply_status(status(tabs=[]))
ok(not tabs_tool.btn_start.isEnabled(),
   "session: nothing to start with no tabs")
tabs_tool.apply_status(status())

# -------------------------------------------------------------- the buttons
buttons_tool.apply_status(status())
ok(buttons_tool.table.rowCount() == 4, "buttons: every button is listed")
ok(buttons_tool.table.item(0, 2).text() == "root",
   "buttons: a BONE button shows its bone")
ok("2 bones" in buttons_tool.table.item(2, 2).text(),
   "buttons: a GROUP shows how many bones it drives")
ok("Body Mesh" in buttons_tool.table.item(3, 2).text(),
   "buttons: a SLIDER shows its mesh and key")
ok(buttons_tool.table.item(1, 3).text() == "!"
   and buttons_tool.table.item(0, 3).text() == "",
   "buttons: only the unmatched ones are flagged")
ok("nope" in (buttons_tool.table.item(1, 3).toolTip() or ""),
   "buttons: and the flag names what is missing")

# ------------------------------------------------------------- retargeting
buttons_tool.table.selectRow(1)          # the unmatched BONE button
names = [buttons_tool.combo_target.itemText(i)
         for i in range(buttons_tool.combo_target.count())]
ok("root" in names and "shoulder" in names,
   "retarget: the bone combo is filled from the rig's real bones")
ok(names[0] == "nope",
   "retarget: the CURRENT (missing) name stays visible, so it is obvious what "
   "is being replaced")
buttons_tool.combo_target.setCurrentIndex(names.index("spine"))
buttons_tool._on_target(0)
call = stub.named("picker_set_button")[-1]
ok(call[1][0] == 1 and call[2].get("bone") == "spine",
   "retarget: picking a bone pushes it for THAT button")

buttons_tool.table.selectRow(2)          # the GROUP with a bad member
buttons_tool.combo_target.setCurrentIndex(
    [buttons_tool.combo_target.itemText(i)
     for i in range(buttons_tool.combo_target.count())].index("root"))
buttons_tool._on_target(0)
call = stub.named("picker_set_button")[-1]
ok(call[2].get("member_index") == 1 and call[2].get("member_bone") == "root",
   "retarget: a GROUP retargets the MISSING member by position, not the first")

buttons_tool.table.selectRow(3)          # the SLIDER
names = [buttons_tool.combo_target.itemText(i)
         for i in range(buttons_tool.combo_target.count())]
ok("frown" in names and "Basis" in names,
   "retarget: a SLIDER offers its mesh's shape keys, not bones")
buttons_tool.combo_target.setCurrentIndex(names.index("frown"))
buttons_tool._on_target(0)
ok(stub.named("picker_set_button")[-1][2].get("sk_key") == "frown",
   "retarget: and pushes the key")

# ---------------------------------------------------------------- debounce
buttons_tool.table.selectRow(0)
n = len(stub.named("picker_set_button"))
for value in (1.1, 1.2, 1.3, 1.4, 1.5):
    buttons_tool._on_scale(value)
ok(len(stub.named("picker_set_button")) == n,
   "debounce: a drag sends NOTHING until it settles - otherwise it is one "
   "bridge command per pixel")
buttons_tool._flush()
sent = stub.named("picker_set_button")
ok(len(sent) == n + 1 and sent[-1][2].get("scale") == 1.5,
   "debounce: the drag lands as ONE command carrying the final value")

# Only what was touched is sent.
ok(set(sent[-1][2]) == {"scale"},
   "debounce: only the field that changed is sent (got %s)" % set(sent[-1][2]))

# Switching button mid-edit must not land the edit on the new one.
buttons_tool._on_scale(2.0)
buttons_tool.table.selectRow(1)
buttons_tool._on_scale(3.0)
buttons_tool._flush()
tail = stub.named("picker_set_button")[-2:]
ok(tail[0][1][0] == 0 and tail[1][1][0] == 1,
   "debounce: an edit queued for one button is flushed BEFORE the next "
   "button's, so it cannot land on the wrong one")

# ---------------------------------------------------------------- appearance
options_tool.apply_status(status())
ok(abs(options_tool.slider_alpha.value() - 100.0) < 1e-6
   and abs(options_tool.slider_round.value() - 7.0) < 1e-6
   and abs(options_tool.slider_darken.value() - 60.0) < 1e-6,
   "appearance: the three settings come from the add-on preferences")
n = len(stub.named("picker_set_prefs"))
options_tool._queue(btn_alpha=50.0)
options_tool._queue(btn_round=20.0)
ok(len(stub.named("picker_set_prefs")) == n, "appearance: debounced too")
options_tool._flush()
sent = stub.named("picker_set_prefs")[-1]
ok(sent[1][0] == {"btn_alpha": 50.0, "btn_round": 20.0},
   "appearance: both edits coalesce into one push")

# A poll must not stomp a slider the user is holding.
options_tool._syncing = False
options_tool.apply_status(status(prefs={"btn_alpha": 12.0, "btn_round": 3.0,
                                        "bg_darken": 4.0}))
ok(abs(options_tool.slider_alpha.value() - 12.0) < 1e-6,
   "appearance: an unfocused slider does follow the poll")

# ------------------------------------------------------------ capability gate
stub.reason = "The Bone picker needs Blender add-on 0.10.0 or newer."
gated = pickermod.PickerTabsTool(stub, win)
gated.refresh()
ok(not gated.btn_add.isEnabled() and not gated.table.isEnabled(),
   "gate: an add-on too old for the picker turns the tab off")
ok("0.10.0" in gated.status.text(),
   "gate: and says WHY, on the control itself")
before = len(stub.named("picker_status"))
gated.refresh()
ok(len(stub.named("picker_status")) == before,
   "gate: a gated tab does not keep polling a bridge that cannot answer")

# ⚠ FAIL OPEN. An unknown capability set is an OLD add-on, not a broken one -
# and a bridge that raises while being asked must not permanently disable a
# feature that is probably fine.
class _Raising(StubBridge):
    def feature_reason(self, feature):
        raise RuntimeError("no idea")


open_tool = pickermod.PickerTabsTool(_Raising(), win)
ok(open_tool.feature_reason() is None,
   "gate: FAILS OPEN - an unanswerable capability question is not a refusal")

# ------------------------------------------------------------ error handling
stub.reason = None
stub.raise_error = True
tabs_tool.refresh()
ok("connect" in tabs_tool.status.text().lower(),
   "errors: a dead bridge says so instead of raising into the UI")
stub.raise_error = False

# ------------------------------------------------------------- busy greying
for tool in (tabs_tool, buttons_tool, options_tool):
    tool.set_capture_busy(True)
ok(not tabs_tool.isEnabled() and not buttons_tool.isEnabled()
   and not options_tool.isEnabled(),
   "busy: every tool greys out while Blender is busy")
page.set_capture_busy(False)
ok(tabs_tool.isEnabled() and buttons_tool.isEnabled()
   and options_tool.isEnabled(),
   "busy: and the PAGE forwards the all-clear to its tools")

# A busy window blocks a command outright.
win.capturing = True
n = len(stub.named("picker_set_tab"))
tabs_tool._call(stub.picker_set_tab, 0)
ok(len(stub.named("picker_set_tab")) == n,
   "busy: no command is sent while a capture is running")
win.capturing = False

# ------------------------------ Save Picker Tab in the library (1.7.0, job 3)
# Marty, 2026-08-10: "add a save picker tab button in Studio Library, that will
# save picker tab directly in library".
from PySide6.QtWidgets import QPushButton  # noqa: E402

import panels as panelsmod  # noqa: E402

_info = panelsmod.InfoPanel()
_kinds = []
_info.saveRequested.connect(lambda kind, _name: _kinds.append(kind))
_picker_btns = [b for b in _info.findChildren(QPushButton)
                if b.text() == "Save Picker Tab"]
ok(len(_picker_btns) == 1,
   "save-picker: exactly one Save Picker Tab button in the save box (%d)"
   % len(_picker_btns))
_picker_btns[0].click()
ok(_kinds == ["picker"],
   "save-picker: it asks for kind 'picker' (%s)" % _kinds)

# ⚠ It must grey out with the rest while Blender is rendering a preview —
# `_save_buttons` is the list `set_capture_busy` walks, and a button left out
# of it stays clickable and fires a command into a busy Blender.
ok(_picker_btns[0] in _info._save_buttons,
   "save-picker: ⚠ it is in _save_buttons, so a capture greys it out too")
_info.set_capture_busy(True)
ok(not _picker_btns[0].isEnabled(),
   "save-picker: ...and it really does go insensitive")
_info.set_capture_busy(False)

# The flow itself: no viewport capture, whatever else happens.
import inspect as _inspect  # noqa: E402
import main as mainmod  # noqa: E402
_flow = _inspect.getsource(mainmod.LibraryView.save_picker_flow)
ok("_start_capture" not in _flow,
   "save-picker: ⚠ NO viewport capture — a picker preview is its reference "
   "picture with the buttons drawn on, and a 3D shot would show the character "
   "instead of the layout being saved")
ok("compose_thumbnail" in _flow,
   "save-picker: it composes the buttons onto the reference instead")
ok("saved_buttons" in _flow and "saved_thumbnail" in _flow,
   "save-picker: reads the reply as a picker STATUS with saved_* keys, not as "
   "a bare result dict")
ok("already exists" in _flow,
   "save-picker: offers to overwrite, like every other save in this panel")

print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
for f in FAIL:
    print("  FAILED: " + f)
sys.exit(1 if FAIL else 0)
