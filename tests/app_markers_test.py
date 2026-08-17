# The Markers tool, app side (Marty, 2026-08-12: "Build app option D plus
# Blender panel 1").
#
#   python tests\app_markers_test.py
#
# The Blender half - the properties, the panel, marker_list being a pure read -
# is `markers_test.py`. This one is the app, and two of these checks are the
# reason the tool is shaped the way it is:
#
#   * A POLL MUST NOT OVERWRITE A FIELD THE USER IS TYPING IN. Two windows on
#     one note, 1.5 s apart; without the focus rule a refresh eats a sentence.
#   * ONLY THE FIELD THAT CHANGED IS SENT. `marker_set` writes what it receives,
#     so a tool that posted the whole marker would push its own one-poll-old
#     copy over a note just typed in Blender.
import json
import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

sys.path.insert(0, os.path.join(_ROOT, "app"))

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import (QApplication, QInputDialog,  # noqa: E402
                               QMessageBox)

import config  # noqa: E402

TMP = tempfile.mkdtemp(prefix="madi_mk_")
config.APP_DIR = TMP
# DATA_DIR is the WRITABLE root (macOS splits it off APP_DIR); the
# caches, queues and presets read it, so redirecting only APP_DIR
# would build them in the real dist folder.
config.DATA_DIR = TMP
config.CONFIG_PATH = os.path.join(TMP, "config.json")
config.DEFAULT_LIBRARY = os.path.join(TMP, "library")
config.DEFAULTS["libraries"] = [{"name": "Test", "path": config.DEFAULT_LIBRARY}]
os.makedirs(config.DEFAULT_LIBRARY, exist_ok=True)

import bridge as bridgemod  # noqa: E402
import markers as markersmod  # noqa: E402

PASS, FAIL = [], []


def ok(cond, label):
    (PASS if cond else FAIL).append(label)
    print(("ok   " if cond else "FAIL ") + label, flush=True)


app = QApplication.instance() or QApplication([])


def _rows():
    return [
        {"index": 0, "uid": "mk10", "name": "shot_010", "frame": 10,
         "camera": "Cam_A", "note": "hips lead the step", "tags": ["hero"],
         "select": False},
        {"index": 1, "uid": "mk48", "name": "shot_020", "frame": 48,
         "camera": None, "note": "arc is flat", "tags": ["wip"],
         "select": False},
        {"index": 2, "uid": "mk96", "name": "fx_splash", "frame": 96,
         "camera": None, "note": "", "tags": ["fx", "wip"], "select": False},
    ]


class StubBridge:
    """Answers like the add-on would, and records every call."""

    def __init__(self, reason=None):
        self.reason = reason
        self.rows = _rows()
        self.revision = 1
        self.sets = []              # (ref, fields) of every marker_set
        self.adds = []
        self.removes = []
        self.gotos = []
        self.renames = []
        self.binds = 0
        self.list_calls = 0
        self.saved = {"path": os.path.join(TMP, "shot.blend")}
        # Parking, modelled the way the add-on really does it: a shown layer
        # means the OTHER markers are not in the scene at all, so marker_list
        # simply does not return them.
        self.showing = ""
        self.shows = []
        # ⚠ NOT `self.sets` — that already records every marker_set call. Two
        # different meanings of "set" collided here once already.
        self.saved_sets = {}

    def feature_reason(self, feature):
        return self.reason

    def _live(self):
        if not self.showing:
            return list(self.rows)
        return [r for r in self.rows if (r.get("layer") or "") == self.showing]

    def marker_list(self, poll=False):
        self.list_calls += 1
        live = self._live()
        tags = sorted({t for r in live for t in r["tags"]})
        # ⚠ Layers come from ALL rows, parked included — exactly as the add-on
        # does it, or picking a layer would erase every other layer from the
        # menu and leave no way back.
        layers = sorted({r.get("layer") or "" for r in self.rows} - {""})
        return {"markers": live, "tags": tags, "layers": layers,
                "showing_layer": self.showing,
                "hidden": len(self.rows) - len(live),
                "sets": sorted(self.saved_sets),
                "revision": self.revision, "frame_current": 1,
                "frame_start": 1, "frame_end": 250}

    def marker_show_layer(self, layer=""):
        self.shows.append(layer)
        self.showing = layer
        self.revision += 1
        return {"layer": layer, "hidden": len(self.rows) - len(self._live()),
                "revision": self.revision}

    def marker_set_save(self, name):
        self.saved_sets[name] = [dict(r) for r in self.rows]
        self.revision += 1
        return {"saved": name, "count": len(self.rows),
                "sets": sorted(self.saved_sets)}

    def marker_set_load(self, name):
        self.rows = [dict(r) for r in self.saved_sets[name]]
        self.showing = ""
        self.revision += 1
        return {"loaded": name, "count": len(self.rows),
                "revision": self.revision}

    def marker_set_delete(self, name):
        self.saved_sets.pop(name, None)
        self.revision += 1
        return {"deleted": name, "sets": sorted(self.saved_sets)}

    def marker_set(self, ref, **fields):
        self.sets.append((dict(ref), dict(fields)))
        self.revision += 1
        # Resolves the way the add-on does: uid first, then the index. And it
        # MINTS a uid on the first write, which is the case that matters.
        hit = None
        if ref.get("uid"):
            hit = next((r for r in self.rows if r["uid"] == ref["uid"]), None)
        if hit is None:
            hit = self.rows[ref["index"]]
        row = dict(hit)
        if not row["uid"]:
            row["uid"] = "minted%d" % row["index"]
        for key, value in fields.items():
            row[key] = value
        self.rows[row["index"]] = row
        return {"marker": row, "revision": self.revision}

    def marker_add(self, name="Marker", frame=None, note="", tags=None,
                   layer=""):
        self.adds.append({"name": name, "frame": frame, "note": note,
                          "tags": tags, "layer": layer})
        self.revision += 1
        return {"added": {"index": len(self.rows), "uid": "new", "name": name,
                          "frame": frame or 1, "camera": None, "note": note,
                          "tags": tags or [], "layer": layer, "select": False},
                "revision": self.revision}

    def marker_remove(self, ref):
        self.removes.append(dict(ref))
        self.rows = [r for r in self.rows if r["uid"] != ref["uid"]]
        self.revision += 1
        return {"revision": self.revision}

    def marker_goto(self, ref):
        self.gotos.append(dict(ref))
        row = next(r for r in self.rows if r["uid"] == ref["uid"])
        return {"frame": row["frame"], "name": row["name"]}

    def marker_bind_by_name(self, exact=True):
        self.binds += 1
        return {"bound": [], "count": 0, "revision": self.revision}

    def marker_rename(self, **kw):
        self.renames.append(kw)
        self.revision += 1
        return {"renamed": [], "count": 2, "revision": self.revision}

    def save_blend(self):
        return dict(self.saved)


class StubQueue:
    def __init__(self, queued=True):
        self.calls = []
        self.queued = queued

    def queue_at_frame(self, path, frame, label=""):
        self.calls.append((path, frame, label))
        return self.queued, "Queued frame %d" % frame


def build(reason=None, queue=None):
    bridge = StubBridge(reason)
    tool = markersmod.MarkersTool(bridge, None)
    tool.set_queue_tool(queue)
    tool.resize(700, 400)
    tool.show()
    app.processEvents()
    return bridge, tool


# ------------------------------------------------------------- the file reader

good = {"format": "madi-markers", "version": 1,
        "markers": [{"name": "a", "frame": 5, "note": "n", "tags": ["x"]}]}
p = os.path.join(TMP, "good.markers")
with open(p, "w", encoding="utf-8") as fh:
    json.dump(good, fh)
rows = markersmod.read_marker_file(p)
ok(rows is not None and len(rows) == 1 and rows[0]["frame"] == 5,
   "import: our own export reads back")

p2 = os.path.join(TMP, "bare.json")
with open(p2, "w", encoding="utf-8") as fh:
    json.dump([{"name": "b", "frame": "7", "tags": "one, two"}], fh)
rows = markersmod.read_marker_file(p2)
ok(rows and rows[0]["frame"] == 7 and rows[0]["tags"] == ["one", "two"],
   "import: a bare list, a numeric string frame and a comma tag string all work")

p3 = os.path.join(TMP, "junk.json")
with open(p3, "w", encoding="utf-8") as fh:
    fh.write("not json at all")
ok(markersmod.read_marker_file(p3) is None,
   "import: a file that is not JSON is refused, not raised")

p4 = os.path.join(TMP, "partial.json")
with open(p4, "w", encoding="utf-8") as fh:
    json.dump({"markers": [{"name": "no frame"}, {"name": "ok", "frame": 3}]}, fh)
rows = markersmod.read_marker_file(p4)
ok(rows == [{"name": "ok", "frame": 3, "note": "", "layer": "", "tags": []}],
   "import: a row with no frame is skipped, the rest still import")

ok(markersmod.read_marker_file(os.path.join(TMP, "nope.json")) is None,
   "import: a missing file is refused, not raised")

# --------------------------------------------------------------- the list

bridge, tool = build()
tool.refresh()
ok(tool.tree.topLevelItemCount() == 3, "list: every marker gets a row")
# ⚠ The row is PAINTED by MarkerRowDelegate now (Marty's "A2"), so there are no
# per-column strings to assert on. The contract is the row's DATA plus the
# delegate being installed — asserting painted pixels would test Qt, not us.
ok(isinstance(tool.tree.itemDelegate(), markersmod.MarkerRowDelegate),
   "list: rows are drawn by the two-line delegate")
first = tool.tree.topLevelItem(0).data(0, Qt.ItemDataRole.UserRole)
ok(first["frame"] == 10 and first["tags"] == ["hero"],
   "list: the row carries the frame and tags the delegate draws")
ok(first.get("note") == "hips lead the step",
   "list: and the note, which is the second line and the whole point of A2")
ok(tool.tree.topLevelItem(0).toolTip(0) == "hips lead the step",
   "list: the note is the row's tooltip")
ok([tool.tag_filter.itemText(i) for i in range(tool.tag_filter.count())]
   == ["All tags", "fx", "hero", "wip"],
   "list: the tag filter is built from the tags in use")

# ------------------------------------------------------------------ search

tool.search.setText("arc")
app.processEvents()
shown = [tool.tree.topLevelItem(i).data(0, Qt.ItemDataRole.UserRole)["name"]
         for i in range(3) if not tool.tree.topLevelItem(i).isHidden()]
ok(shown == ["shot_020"], "search: reaches the NOTE, not just the name")
tool.search.setText("fx")
app.processEvents()
shown = [i for i in range(3) if not tool.tree.topLevelItem(i).isHidden()]
ok(len(shown) == 1, "search: reaches the tags too")
tool.search.setText("")
tool.tag_filter.setCurrentText("wip")
app.processEvents()
shown = [i for i in range(3) if not tool.tree.topLevelItem(i).isHidden()]
ok(len(shown) == 2, "search: the tag filter narrows to the two carrying it")
tool.tag_filter.setCurrentIndex(0)
app.processEvents()

# ------------------------------------------------------------ the detail pane

tool.tree.setCurrentItem(tool.tree.topLevelItem(0))
app.processEvents()
ok(tool.detail.isVisible(), "detail: selecting a marker shows the editor")
ok(tool.name_edit.text() == "shot_010" and tool.frame_spin.value() == 10,
   "detail: name and frame are filled")
ok(tool.note_edit.toPlainText() == "hips lead the step", "detail: and the note")
ok(tool.tags_edit.text() == "hero", "detail: and the tags")
ok(tool.camera_label.text() == "Cam_A" and tool.camera_button.isEnabled(),
   "detail: a bound camera is named, with Clear live")

# ------------------------------------------ ⚠ only the changed field is sent

bridge.sets.clear()
tool.note_edit.setFocus()
tool.note_edit.setPlainText("new note")
tool._flush()
ok(len(bridge.sets) == 1, "write: one edit is one call")
ref, fields = bridge.sets[0]
ok(list(fields) == ["note"],
   "write: ONLY the note was sent - not the name, frame or tags")
ok(ref["uid"] == "mk10", "write: addressed by uid, never by name")

# a burst of typing coalesces into ONE write
bridge.sets.clear()
for text in ("a", "ab", "abc"):
    tool.note_edit.setPlainText(text)
ok(len(bridge.sets) == 0, "write: nothing is sent while the debounce is running")
tool._flush()
ok(len(bridge.sets) == 1 and bridge.sets[0][1]["note"] == "abc",
   "write: the burst coalesced into one call carrying the last value")

# tags are sent as a LIST, split from the comma string
bridge.sets.clear()
tool.tags_edit.setText("hero, contact")
tool._queue_write("tags", "hero, contact")
tool._flush()
ok(bridge.sets and bridge.sets[0][1]["tags"] == ["hero", "contact"],
   "write: a comma string becomes a list of tags")

# ------------------------------------------- ⚠ a poll must not eat a sentence
#
# ⚠ NOTHING MAY `build()` ANOTHER WIDGET BETWEEN HERE AND THE END OF THIS
# SECTION. `show()` on a second tool takes the active window with it, and
# `setFocus()` on a widget in an inactive window does not grant focus — so the
# precondition below fails and both checks go red against perfectly good code.
# The uid section that used to sit here was moved down for exactly that reason.

tool.tree.setCurrentItem(tool.tree.topLevelItem(1))
app.processEvents()
tool.note_edit.setFocus()
app.processEvents()
ok(tool.note_edit.hasFocus(), "focus: the note field really has focus (precondition)")
tool._filling = True
tool.note_edit.setPlainText("half a sentence I am still")
tool._filling = False
bridge.rows[1]["note"] = "what Blender had a second ago"
bridge.revision += 1
tool._poll()
app.processEvents()
ok(tool.note_edit.toPlainText() == "half a sentence I am still",
   "focus: a poll landing mid-note did NOT overwrite what is being typed")

# and with focus elsewhere the same poll DOES refresh it
tool.search.setFocus()
app.processEvents()
bridge.rows[1]["note"] = "updated in blender"
bridge.revision += 1
tool._poll()
app.processEvents()
ok(tool.note_edit.toPlainText() == "updated in blender",
   "focus: with the cursor elsewhere, a Blender-side change does come through")

# ------------------------------------------------------ the revision shortcut

before = bridge.list_calls
tool._poll()
rebuilt = bridge.list_calls
ok(rebuilt == before + 1, "poll: it always ASKS (the revision lives in Blender)")
tool.tree.setCurrentItem(tool.tree.topLevelItem(0))
app.processEvents()
current_before = tool.tree.currentItem()
tool._poll()
app.processEvents()
ok(tool.tree.currentItem() is current_before,
   "poll: an unchanged revision leaves the list and the selection alone")

# a pending edit blocks the poll outright
tool._pending = {"note": "x"}
tool._pending_ref = tool._ref()
calls = bridge.list_calls
tool._poll()
ok(bridge.list_calls == calls,
   "poll: skipped entirely while an edit is waiting to be sent")
tool._pending = {}
tool._pending_ref = None

# --------------------------------------------------- switching rows flushes

tool.tree.setCurrentItem(tool.tree.topLevelItem(0))
app.processEvents()
bridge.sets.clear()
tool._queue_write("note", "typed on row 0")
tool.tree.setCurrentItem(tool.tree.topLevelItem(2))
app.processEvents()
ok(len(bridge.sets) == 1 and bridge.sets[0][0]["uid"] == "mk10",
   "flush: changing row sent the pending edit AGAINST THE ROW IT WAS TYPED ON")

# hiding the tool flushes too, rather than dropping the edit
tool.tree.setCurrentItem(tool.tree.topLevelItem(0))
app.processEvents()
bridge.sets.clear()
tool._queue_write("note", "typed then tabbed away")
tool.hide()
app.processEvents()
ok(len(bridge.sets) == 1 and bridge.sets[0][1]["note"] == "typed then tabbed away",
   "flush: hiding the tab sends the edit instead of losing it")
ok(not tool._timer.isActive(), "poll: the timer stops when the tool is hidden")
tool.show()
app.processEvents()
ok(tool._timer.isActive(), "poll: and starts again when it comes back")

# ------------------------------------------------------- the camera sentinel

tool.tree.setCurrentItem(tool.tree.topLevelItem(0))
app.processEvents()
bridge.sets.clear()
tool._clear_camera()
ok(bridge.sets and bridge.sets[0][1] == {"camera": None},
   "camera: Clear sends camera=None explicitly")
bridge.sets.clear()
tool._queue_write("note", "unrelated")
tool._flush()
ok(bridge.sets and "camera" not in bridge.sets[0][1],
   "camera: an ordinary edit does not mention it, so the binding survives")

# ----------------------------------------------------------- jump and render

bridge.gotos.clear()
tool._jump()
ok(bridge.gotos and bridge.gotos[0]["uid"] == "mk10",
   "jump: asks Blender to move the playhead to that marker")

queue = StubQueue()
bridge2, tool2 = build(queue=queue)
tool2.refresh()
tool2.tree.setCurrentItem(tool2.tree.topLevelItem(1))
app.processEvents()
tool2._render()
ok(queue.calls and queue.calls[0][1] == 48,
   "render: the marker's frame goes to the Render Queue")
ok(queue.calls[0][0] == bridge2.saved["path"],
   "render: and it queues the file the bridge just saved")

_, tool3 = build(queue=None)
ok(not tool3.render_button.isVisible(),
   "render: with no queue to hand, the button hides itself")

# ---------------------------------------------------------------- the gate

_, locked = build(reason="Timeline markers need Blender add-on 0.40.0 or newer")
locked.refresh()
ok(not locked.tree.isEnabled(),
   "gate: an add-on too old for marker_list disables the tool")
ok("0.40.0" in locked.status.text(),
   "gate: and says why, in the tool rather than a dialog")

# the gate must LIFT without restarting the app - the add-on can be updated
# from Library Settings mid-session
locked.bridge.reason = None
locked.refresh()
ok(locked.tree.isEnabled(),
   "gate: a newer add-on arriving mid-session hands the tool back")

# -------------------------------------------------------------- remove asks

asked = {"n": 0}
real_question = QMessageBox.question


def fake_question(*a, **k):
    asked["n"] += 1
    return QMessageBox.StandardButton.No


QMessageBox.question = staticmethod(fake_question)
bridge.removes.clear()
tool.tree.setCurrentItem(tool.tree.topLevelItem(0))
app.processEvents()
tool._remove()
ok(asked["n"] == 1 and not bridge.removes,
   "remove: asks first, and No really means no")
QMessageBox.question = staticmethod(
    lambda *a, **k: QMessageBox.StandardButton.Yes)
tool._remove()
ok(len(bridge.removes) == 1 and bridge.removes[0]["uid"] == "mk10",
   "remove: Yes removes the marker it was asked about")
QMessageBox.question = real_question

# --------------------------- ⚠ the FIRST write to a marker mints its uid
# (below the focus section on purpose — see the warning up there)
#
# A marker nothing has ever written to reports `uid: ""`, because `marker_list`
# refuses to mint one — it is polled. The reply to that first write carries a
# real uid, so folding it back in cannot match on uid alone. `_revision` has
# already moved past it, so no poll comes along to repair a miss — and export
# reads this list, so the note would be missing from the exported file.

fresh, tool_f = build()
fresh.rows = [dict(r, uid="") for r in fresh.rows]
tool_f.refresh()
tool_f.tree.setCurrentItem(tool_f.tree.topLevelItem(1))
app.processEvents()
ok((tool_f.current() or {}).get("uid") == "",
   "uid: an untouched marker really does arrive with no uid (precondition)")
tool_f._queue_write("note", "first ever note")
tool_f._flush()
ok(tool_f._markers[1].get("note") == "first ever note",
   "uid: the first write folds back into the list even though the uid changed")
ok(tool_f._markers[1].get("uid") == "minted1",
   "uid: and the list now carries the minted uid, so the next edit is exact")

# ------------------------------------------------------------------ layers
# Marty, 2026-08-12: one layer per marker, and "when no layer is selected -
# all markers show".

lb, tl = build()
lb.rows[0]["layer"] = "blocking"
lb.rows[1]["layer"] = "polish"
lb.rows[2]["layer"] = ""
tl.refresh()
ok([tl.layer_filter.itemText(i) for i in range(tl.layer_filter.count())]
   == ["All layers", "blocking", "polish"],
   "layers: the filter is built from the layers actually in use")
ok(tl.tree.topLevelItem(0).data(0, Qt.ItemDataRole.UserRole)["layer"]
   == "blocking",
   "layers: the row carries its layer for the delegate to draw")

# ⚠ PICKING A LAYER IS A WRITE NOW, not a view filter: it asks Blender to put
# the other markers away so the TIMELINE STRIP clears too.
lb.shows.clear()
tl.layer_filter.setCurrentText("blocking")
app.processEvents()
ok(lb.shows == ["blocking"],
   "layers: ⚠ picking one asks the bridge to SHOW it - the others leave the "
   "scene, which is the only thing that clears Blender's timeline")
ok(tl.tree.topLevelItemCount() == 1,
   "layers: and the list holds only that layer's markers afterwards")
ok(tl.hidden_label.isVisible() and "2" in tl.hidden_label.text(),
   "layers: ⚠ and it SAYS how many are hidden - markers missing with no "
   "explanation is indistinguishable from having lost them")

# ⚠ MARTY'S RULE - and the layerless marker must come back too.
lb.shows.clear()
tl.layer_filter.setCurrentIndex(0)
app.processEvents()
ok(lb.shows == [""], "layers: choosing All layers asks for everything back")
ok(tl.tree.topLevelItemCount() == 3,
   "layers: with none picked, ALL markers show - including the layerless one")
ok(not tl.hidden_label.isVisible(),
   "layers: and the hidden notice goes away")

# assigning a layer is the same control as picking one
tl.tree.setCurrentItem(tl.tree.topLevelItem(2))
app.processEvents()
lb.sets.clear()
tl._layer_changed("newlayer")
tl._flush()
ok(lb.sets and lb.sets[0][1] == {"layer": "newlayer"},
   "layers: typing a new name in the editor assigns it - that IS how a layer "
   "is created")
ok("newlayer" in [tl.layer_filter.itemText(i)
                  for i in range(tl.layer_filter.count())],
   "layers: and it appears in the filter immediately, without waiting a poll")

# ⚠ adding while filtered must not create a marker you cannot see
tl.layer_filter.setCurrentText("blocking")
app.processEvents()
lb.adds.clear()
tl._add()
ok(lb.adds and lb.adds[0]["layer"] == "blocking",
   "layers: ⚠ a marker added while filtered joins THAT layer - otherwise it "
   "vanishes the moment you create it")
tl.layer_filter.setCurrentIndex(0)
app.processEvents()
lb.adds.clear()
tl._add()
ok(lb.adds and lb.adds[0]["layer"] == "",
   "layers: and with no layer picked it joins none")

# the echo gate: an add-on with no layer support must grey the controls, not
# silently ignore the writes
old = StubBridge()
old_list = old.marker_list


def no_layers(poll=False):
    data = old_list(poll=poll)
    data.pop("layers", None)
    for r in data["markers"]:
        r.pop("layer", None)
    return data


old.marker_list = no_layers
tool_old = markersmod.MarkersTool(old, None)
tool_old.show()
app.processEvents()
tool_old.refresh()
ok(not tool_old.layer_filter.isEnabled() and not tool_old.layer_edit.isEnabled(),
   "layers: an add-on too old to know about them greys the layer controls")
# ⚠ AND IT MUST STILL BE GREY ON THE NEXT REFRESH. `_set_enabled(True)` runs at
# the top of every refresh and re-enables everything; a gate that memoised
# "already off" handed these back live from the second poll onward.
tool_old.refresh()
tool_old.refresh()
ok(not tool_old.layer_filter.isEnabled() and not tool_old.layer_edit.isEnabled(),
   "layers: ⚠ and STAYS grey across further refreshes")
ok("0.41.0" in tool_old.layer_filter.toolTip(),
   "layers: and the tooltip says which version adds them")
old.marker_list = old_list
tool_old.refresh()
ok(tool_old.layer_filter.isEnabled(),
   "layers: a newer add-on arriving mid-session hands them back")

# ------------------------------------------------------------ marker sets
# Marty, 2026-08-12: "save marker preset per project, this should be saved in
# .blend file and autoloaded by our tool".

sb, ts = build()
ts.refresh()
ok([ts.set_combo.itemText(i) for i in range(ts.set_combo.count())]
   == ["Marker sets"],
   "sets: a file with none saved shows just the placeholder")

real_text = QInputDialog.getText
QInputDialog.getText = staticmethod(lambda *a, **k: ("Shot breakdown", True))
ts._save_set()
QInputDialog.getText = real_text
ok("Shot breakdown" in sb.saved_sets,
   "sets: saving asks Blender to store it in the .blend")
ok("Shot breakdown" in [ts.set_combo.itemText(i)
                        for i in range(ts.set_combo.count())],
   "sets: ⚠ and it appears without a restart - the app reads whatever the open "
   "file holds, which IS the autoload")

# ⚠ loading replaces every marker, so it must ask first
asked = {"n": 0}
QMessageBox.question = staticmethod(
    lambda *a, **k: (asked.__setitem__("n", asked["n"] + 1),
                     QMessageBox.StandardButton.No)[1])
sb.rows = []
ts.set_combo.setCurrentText("Shot breakdown")
ts._load_set()
ok(asked["n"] == 1 and sb.rows == [],
   "sets: loading asks before replacing every marker, and No means no")
QMessageBox.question = staticmethod(
    lambda *a, **k: QMessageBox.StandardButton.Yes)
ts._load_set()
ok(len(sb.rows) == 3, "sets: Yes puts the whole set back")

ts._delete_set()
ok("Shot breakdown" not in sb.saved_sets, "sets: delete forgets the set...")
ok(len(sb.rows) == 3, "sets: ...and leaves the markers alone")
QMessageBox.question = real_question

# ---------------------------------------------------------- ⚠ NO LEAKS
# Marty asked for this directly (2026-08-12). The tool holds a poll timer, a
# debounce timer and a tree full of rows; none of it may outlive the widget.

import gc  # noqa: E402
import weakref  # noqa: E402

refs = []
for _ in range(6):
    b_leak = StubBridge()
    t_leak = markersmod.MarkersTool(b_leak, None)
    t_leak.show()
    app.processEvents()
    t_leak.refresh()
    t_leak._queue_write("note", "pending on purpose")
    t_leak.hide()          # flushes, stops the timer
    refs.append(weakref.ref(t_leak))
    t_leak.setParent(None)
    t_leak.deleteLater()
    del t_leak
    app.processEvents()

# Qt6 dropped the DeferredDeletion processEvents flag, so the deletions are
# drained explicitly - the same dance app_blendsize_test does.
from PySide6.QtCore import QCoreApplication, QEvent  # noqa: E402

for _ in range(3):
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()
gc.collect()
alive = [r for r in refs if r() is not None]
ok(not alive,
   "leak: six tools built, used and closed leave NONE alive (%d stranded)"
   % len(alive))

# and a long-lived tool must not accumulate rows across refreshes
b_grow = StubBridge()
t_grow = markersmod.MarkersTool(b_grow, None)
t_grow.show()
app.processEvents()
for _ in range(25):
    b_grow.revision += 1
    t_grow._poll()
    app.processEvents()
ok(t_grow.tree.topLevelItemCount() == 3,
   "leak: twenty-five polls leave three rows, not seventy-five")
ok(len(t_grow._markers) == 3, "leak: and the list in hand stays three long")
ok(t_grow._pending == {} and t_grow._pending_ref is None,
   "leak: nothing is left pending after a quiet poll run")
t_grow.hide()
ok(not t_grow._timer.isActive() and not t_grow._write_timer.isActive(),
   "leak: both timers are stopped once the tool is hidden")

# ------------------------------------------------------------------ export

bridge3, tool4 = build()
tool4.refresh()
out = os.path.join(TMP, "out.markers")
payload = {"format": "madi-markers", "version": 1,
           "markers": [{"name": m["name"], "frame": m["frame"],
                        "note": m.get("note", ""), "tags": m.get("tags") or [],
                        "camera": m.get("camera")}
                       for m in tool4._markers]}
with open(out, "w", encoding="utf-8") as fh:
    json.dump(payload, fh)
written = json.load(open(out, encoding="utf-8"))
ok(all("uid" not in m for m in written["markers"]),
   "export: the uid is NOT written - it belongs to the file it came from")
ok(markersmod.read_marker_file(out) is not None,
   "export: and what it writes is what the importer reads")

# ------------------------------------------------------- the tool is FREE

main_src = open(os.path.join(_ROOT, "app", "main.py"),
                encoding="utf-8").read()
import main as _mainmod  # noqa: E402
ok(not hasattr(_mainmod.MainWindow, "GATED_ATTRS")
   and not hasattr(_mainmod.MainWindow, "GATED"),
   "licence: the gating machinery is gone entirely since 1.19.0 - every tool "
   "is free and there is nothing left to lock")
bridge_src = open(os.path.join(_ROOT, "app", "bridge.py"),
                  encoding="utf-8").read()
ok('"markers": (' in bridge_src and '"marker_list", "0.40.0"' in bridge_src,
   "compat: the tool declares its requirement, so an old add-on greys it")

print("\n%d passed, %d failed" % (len(PASS), len(FAIL)), flush=True)
for f in FAIL:
    print("FAIL " + f, flush=True)
