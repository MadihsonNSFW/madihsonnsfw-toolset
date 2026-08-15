# The Render presets tool (Marty, 2026-08-05: "In rendering tab add a 'Render
# presets' window, in here user can save rendering settings").
#
#   python tests\app_render_presets_test.py
#
# The Blender half - the catalogue, the ordering and the whitelist - is
# `render_presets_test.py`. This one is the app: the store on disk, the save
# dialog's ticks, and the tool's verbs against a stub bridge.
#
# ⚠ Everything here runs against a TEMP presets folder. `presets_dir()` reads
# `config.APP_DIR` live, so pointing that at a temp dir is what keeps the suite
# away from a real installed build's presets.
import json
import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

sys.path.insert(0, os.path.join(_ROOT, "app"))

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import (QApplication, QDialog, QMessageBox,  # noqa: E402
                               QWidget)

import config  # noqa: E402

TMP = tempfile.mkdtemp(prefix="madi_rpre_")
config.APP_DIR = TMP
config.CONFIG_PATH = os.path.join(TMP, "config.json")

# ⚠ A DEAD PORT, DELIBERATELY. With no config file the DEFAULTS apply, and the
# default port is the REAL bridge port - so this suite connected to whatever
# Blender Marty happened to have open and measured a different app. On
# 2026-08-15 that turned three suites red for a reason unrelated to the change
# under test: the live add-on was a version behind, so the status bar grew a
# 172 px "Update add-on" button and the window's minimum width went 632 -> 810.
_io_ = __import__("io")
_json_ = __import__("json")
_io_.open(config.CONFIG_PATH, "w", encoding="utf-8").write(
    _json_.dumps({"port": 9998}))
# ⚠ And the library too, before main is imported: building a MainWindow scans
# whatever `libraries` says, and the default is Marty's REAL library — which is
# read-only here, but it also probes every playblast mp4 with ffprobe and fills
# the log with it. The suite gets its own empty one.
config.DEFAULT_LIBRARY = os.path.join(TMP, "library")
config.DEFAULTS["libraries"] = [{"name": "Test", "path": config.DEFAULT_LIBRARY}]
os.makedirs(config.DEFAULT_LIBRARY, exist_ok=True)

import bridge as bridgemod  # noqa: E402
import render_presets as rp  # noqa: E402
import theme  # noqa: E402

PASS, FAIL = [], []


def ok(cond, label):
    (PASS if cond else FAIL).append(label)
    print(("ok   " if cond else "FAIL ") + label, flush=True)


app = QApplication.instance() or QApplication([])

# What the add-on hands back, trimmed to three groups. `empty` is the case that
# matters: a group this Blender has nothing for.
CAPTURED = {
    "blender": "5.2.0 LTS",
    "scene": "Scene",
    "engine": "CYCLES",
    "groups": {
        "format": {"label": "Resolution & frame rate", "default": True,
                   "values": {"render.resolution_x": 1920,
                              "render.resolution_y": 1080,
                              "render.fps": 24}},
        "empty": {"label": "EEVEE", "default": True, "values": {}},
        "frames": {"label": "Frame range", "default": False,
                   "values": {"frame_start": 1, "frame_end": 250}},
    },
    "skipped": [],
}


class StubBridge:
    """Records what the tool asked for; answers like the add-on would."""

    def __init__(self, reason=None):
        self.reason = reason
        self.captures = []          # the `groups` argument of each capture
        self.applied = []           # (data, groups)
        self.report = {"summary": "3 settings changed.", "applied": [],
                       "unchanged": [], "skipped": [], "failed": [],
                       "rejected": []}

    def feature_reason(self, feature):
        return self.reason

    def render_preset_capture(self, groups=None):
        self.captures.append(groups)
        if groups is None:
            return json.loads(json.dumps(CAPTURED))
        picked = {k: v for k, v in CAPTURED["groups"].items() if k in groups}
        out = json.loads(json.dumps(CAPTURED))
        out["groups"] = picked
        return out

    def render_preset_apply(self, data, groups=None):
        self.applied.append((data, groups))
        return self.report


# =============================================================== the store ===
ok(rp._slug("Final 4K") == "Final 4K", "slug: an ordinary name is left alone")
ok("/" not in rp._slug("a/b:c*d?") and "\\" not in rp._slug("a/b:c*d?"),
   "slug: a name with path separators cannot become a path (%r)"
   % rp._slug("a/b:c*d?"))
ok(rp._slug("") == "preset" and rp._slug("   ") == "preset",
   "slug: an empty name still gets a filename")
ok(rp._slug("...") == "preset",
   "slug: a name of nothing but dots does not become '.' or '..'")
ok(len(rp._slug("x" * 200)) <= 60, "slug: capped at 60 characters")

p1 = rp.free_path("Draft", TMP)
ok(os.path.basename(p1) == "Draft.json", "free_path: first one takes the name")
open(p1, "w").close()
p2 = rp.free_path("Draft", TMP)
ok(os.path.basename(p2) == "Draft 2.json",
   "free_path: a taken name gets a number rather than overwriting (%s)"
   % os.path.basename(p2))
os.remove(p1)

data = {"name": "Final 4K", "engine": "CYCLES", "blender": "5.2.0 LTS",
        "groups": {"format": {"label": "Resolution & frame rate",
                              "values": {"render.resolution_x": 3840}}}}
written = rp.write_preset(data)
ok(os.path.isfile(written), "write_preset: the file lands in the folder")
loaded = rp.load_preset(written)
ok(loaded["name"] == "Final 4K"
   and loaded["groups"]["format"]["values"]["render.resolution_x"] == 3840,
   "write -> load round trip keeps the name and the values")
ok(loaded.get("saved"), "write_preset stamps when it was saved")
ok(loaded["path"] == written, "load_preset carries the path it came from")
with open(written, "r", encoding="utf-8") as f:
    on_disk = json.load(f)
ok("path" not in on_disk,
   "the path is NOT written into the file - it is where the file is, and a "
   "copied preset folder would carry the old one")

rp.write_preset({"name": "apple", "groups": {}})
rp.write_preset({"name": "Banana", "groups": {}})
with open(os.path.join(TMP, rp.DIRNAME, "junk.json"), "w") as f:
    f.write("{not json at all")
with open(os.path.join(TMP, rp.DIRNAME, "nope.json"), "w") as f:
    json.dump({"hello": "world"}, f)
with open(os.path.join(TMP, rp.DIRNAME, "notes.txt"), "w") as f:
    f.write("hi")
names = [d["name"] for d in rp.list_presets()]
ok(names == ["apple", "Banana", "Final 4K"],
   "list_presets sorts case-insensitively (%r)" % names)
ok("junk" not in " ".join(names) and "nope" not in " ".join(names),
   "unreadable or foreign .json files are skipped, not raised - a preset "
   "folder is a folder a user can drop anything into")
ok(rp.load_preset(os.path.join(TMP, rp.DIRNAME, "notes.txt")) is None,
   "load_preset refuses something that is not JSON")
ok(rp.count_settings(loaded) == 1, "count_settings counts across the groups")
ok(rp.count_settings({"groups": {}}) == 0, "count_settings copes with nothing")

ok(rp.delete_preset(os.path.join(TMP, rp.DIRNAME, "apple.json")),
   "delete_preset removes the file")
ok(not rp.delete_preset(os.path.join(TMP, rp.DIRNAME, "gone.json")),
   "delete_preset says so rather than raising when there is nothing there")

ok(rp._fmt(True) == "on" and rp._fmt(False) == "off",
   "_fmt: a bool reads as on/off, not True/False")
ok(rp._fmt(1.50000) == "1.5" and rp._fmt(2.0) == "2",
   "_fmt: a float loses its trailing zeros (%r)" % rp._fmt(1.5))

# ============================================================== the dialog ===
parent = QWidget()
dialog = rp.SavePresetDialog(parent, CAPTURED)
tops = [dialog.tree.topLevelItem(i)
        for i in range(dialog.tree.topLevelItemCount())]
ok(len(tops) == 3, "dialog: one row per captured group")
by_key = {t.data(0, Qt.UserRole): t for t in tops}
ok(by_key["format"].checkState(0) == Qt.Checked,
   "dialog: a default group starts ticked")
ok(by_key["frames"].checkState(0) == Qt.Unchecked,
   "dialog: the frame range starts UNTICKED - a preset that silently retimed "
   "the scene would be a bug report, not a feature")
ok(by_key["empty"].checkState(0) == Qt.Unchecked
   and not (by_key["empty"].flags() & Qt.ItemIsEnabled),
   "dialog: a group this Blender has nothing for cannot be ticked into "
   "existence")
ok(by_key["format"].childCount() == 3,
   "dialog: the values are shown, so what is being saved is visible")
ok(dialog.selection() == ["format"], "dialog: selection() is what is ticked")
ok(list(dialog.groups_payload()) == ["format"],
   "dialog: the payload carries only the ticked groups")

ok(not dialog.buttons.button(rp.QDialogButtonBox.Save).isEnabled(),
   "dialog: Save is off until it has a name")
dialog.name.setText("Final 4K")
ok(dialog.buttons.button(rp.QDialogButtonBox.Save).isEnabled(),
   "dialog: a name plus one ticked group is enough")
dialog._set_all(False)
ok(dialog.selection() == [] and
   not dialog.buttons.button(rp.QDialogButtonBox.Save).isEnabled(),
   "dialog: Save is off again with nothing ticked - an empty preset applies "
   "nothing and would look broken")
dialog._set_all(True)
ok(dialog.selection() == ["format", "frames"],
   "dialog: Select all ticks every ENABLED group (%r)" % dialog.selection())
ok("5 setting(s) in 2 group(s)" in dialog.count.text(),
   "dialog: the count follows the ticks (%r)" % dialog.count.text())

seeded = rp.SavePresetDialog(parent, CAPTURED, name="X", groups=["frames"])
ok(seeded.selection() == ["frames"],
   "dialog: re-opened for an existing preset, it ticks what that preset holds")

# ================================================================ the tool ===
# Everything the verbs would pop up is answered Yes; the save dialog is
# replaced with one that accepts immediately.
rp.QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Yes)
rp.QInputDialog.getText = staticmethod(lambda *a, **k: ("Renamed", True))


class InstantDialog(rp.SavePresetDialog):
    NAME = "From the tool"
    GROUPS = ["format"]

    def exec(self):
        self.name.setText(self.NAME)
        self._set_all(False)
        for i in range(self.tree.topLevelItemCount()):
            node = self.tree.topLevelItem(i)
            if node.data(0, Qt.UserRole) in self.GROUPS:
                node.setCheckState(0, Qt.Checked)
        return QDialog.Accepted


rp.SavePresetDialog = InstantDialog

bridge = StubBridge()
tool = rp.RenderPresetsTool(bridge, None)
ok(tool.list.count() == 2,
   "tool: the folder's presets are listed on build (%d)" % tool.list.count())
tool.list.setCurrentRow(0)
ok(tool.current()["name"] == "Banana", "tool: selecting a row selects its data")
tool.list.setCurrentRow(next(i for i in range(tool.list.count())
                             if tool.list.item(i).text() == "Final 4K"))
ok(tool.tree.topLevelItemCount() == 1
   and tool.tree.topLevelItem(0).childCount() == 1,
   "tool: the details tree shows the selected preset's groups and settings")
ok("Resolution & frame rate" in tool.tree.topLevelItem(0).text(0),
   "tool: the group's label is what is shown, not its key")

tool.save_current()
ok(bridge.captures[-1] is None,
   "save: the capture asks for EVERY group, so the dialog can show real values "
   "before anything is ticked")
saved = [d for d in rp.list_presets() if d["name"] == "From the tool"]
ok(len(saved) == 1, "save: the preset is written to the folder")
ok(list(saved[0]["groups"]) == ["format"],
   "save: only the ticked groups are stored (%r)" % list(saved[0]["groups"]))
ok(saved[0]["engine"] == "CYCLES" and saved[0]["blender"] == "5.2.0 LTS",
   "save: what it was captured on is recorded with it")

before = len(rp.list_presets())
tool.save_current()
ok(len(rp.list_presets()) == before,
   "save: saving the same name again replaces that preset rather than piling "
   "up 'Name 2' files")

# --- apply -----------------------------------------------------------------
row = next(i for i in range(tool.list.count())
           if tool.list.item(i).text() == "From the tool")
tool.list.setCurrentRow(row)
tool.apply_preset()
sent, groups = bridge.applied[-1]
ok(sent["name"] == "From the tool" and groups is None,
   "apply: the whole stored preset goes over, and nothing narrows it - a "
   "preset applies what it holds")
ok("3 settings changed." in tool.status.text(),
   "apply: the add-on's summary is what the user reads (%r)"
   % tool.status.text())

bridge.report = dict(bridge.report,
                     summary="1 setting changed, 1 refused.",
                     failed=[{"path": "render.image_settings.file_format",
                              "reason": "enum item not found"}],
                     rejected=["name"])
tool.apply_preset()
ok("file_format" in tool.status.text() and "enum item" in tool.status.text(),
   "apply: a refused setting is named, with the reason")
ok("name" in tool.status.text().split("Ignored")[-1],
   "apply: a path the add-on rejected is reported, not swallowed")

# --- update from scene -----------------------------------------------------
bridge.captures = []
tool.update_preset()
ok(bridge.captures[-1] == ["format"],
   "update: re-reads EXACTLY the groups this preset already stores, so an "
   "update cannot quietly widen it (%r)" % bridge.captures[-1])
fresh = next(d for d in rp.list_presets() if d["name"] == "From the tool")
ok(list(fresh["groups"]) == ["format"], "update: the shape is unchanged")

# --- rename / delete -------------------------------------------------------
path_before = tool.current()["path"]
tool.rename_preset()
renamed = [d for d in rp.list_presets() if d["name"] == "Renamed"]
ok(len(renamed) == 1, "rename: the name changes")
ok(renamed[0]["path"] == path_before,
   "rename: the FILE keeps its name - renaming it would surprise anyone with "
   "the folder open, and the name that counts is the one inside")

count_before = tool.list.count()
tool.delete_selected()
ok(tool.list.count() == count_before - 1, "delete: the row goes")
ok(not os.path.exists(path_before), "delete: and so does the file")

# --- the feature gate ------------------------------------------------------
REASON = ("Render presets need Blender add-on 0.17.0 or newer — update the "
          "extension from ⚙ Library Settings to enable them.")
gated = rp.RenderPresetsTool(StubBridge(reason=REASON), None)
ok(not gated.btn_save.isEnabled() and not gated.btn_apply.isEnabled()
   and not gated.btn_update.isEnabled(),
   "gate: the three buttons that need Blender are off")
ok(gated.btn_save.toolTip() == REASON, "gate: the reason is on the control")
ok(REASON in gated.status.text(), "gate: and spelled out in the status line")
ok(gated.list.count() >= 1,
   "gate: the LIST still shows every preset - they are files on disk and stay "
   "readable whatever the add-on is")
ok(gated.btn_rename.isEnabled() and gated.btn_delete.isEnabled(),
   "gate: renaming and deleting are pure disk work and stay available")

# ⚠ the stale-tooltip guard: the add-on can be updated from Library Settings
# without restarting, so a cleared gate has to put the tooltips back.
gated.bridge.reason = None
gated._sync_buttons()
ok(gated.btn_save.isEnabled() and gated.btn_save.toolTip() != REASON,
   "gate: cleared, the button comes back with its OWN tooltip - a stale 'you "
   "need 0.17.0' would look like the update had not worked")


class DeadBridge(StubBridge):
    def feature_reason(self, feature):
        raise RuntimeError("no bridge")


dead = rp.RenderPresetsTool(DeadBridge(), None)
ok(dead.btn_save.isEnabled(),
   "a dead bridge fails OPEN - 'we do not know' is not 'the add-on is too old'")

# ============================================================= the wiring ===
cmd, since, why = bridgemod.FEATURE_REQUIREMENTS["render_presets"]
ok(cmd == "render_preset_capture" and since == "0.17.0",
   "bridge: the feature is gated on the capture command at 0.17.0")
ok("0.17.0" in why and "Library Settings" in why,
   "bridge: the reason tells the user what to do about it")
ok(bridgemod.GATED_COMMANDS.get("render_preset_capture") == "0.17.0",
   "bridge: the command is in the gated set derived from that table")
# ⚠ A FLOOR, not an equality — pinning the exact version here breaks the suite
# the next time the add-on is bumped for something unrelated (it did, within
# the hour, when the library item type took it to 0.18.0). The three-way
# manifest/core/app agreement is `bridge_version_test.py`'s job.
ok(tuple(int(p) if p.isdigit() else 0
         for p in bridgemod.EXPECTED_ADDON_VERSION.split(".")) >= (0, 17, 0),
   "bridge: the app expects an add-on at or past the one that answers it (%s)"
   % bridgemod.EXPECTED_ADDON_VERSION)

sent = {}


class FakeRequest(bridgemod.Bridge):
    def request(self, cmd, params=None, timeout=15.0, probe=False, poll=False):
        sent[cmd] = params
        return {}


fake = FakeRequest()
fake.render_preset_capture(groups=["format"])
fake.render_preset_apply({"groups": {}}, groups=["format"])
fake.render_preset_schema()
ok(sent["render_preset_capture"] == {"groups": ["format"]},
   "client: capture sends the group list")
ok(sent["render_preset_apply"] == {"data": {"groups": {}},
                                   "groups": ["format"]},
   "client: apply sends the preset and the group filter")
ok("render_preset_schema" in sent, "client: the schema command is reachable")

# ================================================== the rail entry, for real ==
import main as mainmod  # noqa: E402

win = mainmod.MainWindow()
rail = win.rendering.rail
labels = []
for i in range(rail.topLevelItemCount()):
    top = rail.topLevelItem(i)
    for j in range(top.childCount()):
        labels.append(top.child(j).text(0))
ok("Render presets" in labels,
   "the Rendering tab really has the rail entry (%r)" % labels)
ok(isinstance(win.render_presets_tool, rp.RenderPresetsTool),
   "and the window holds the tool itself")

# ======================================= the Studio Library item type ========
# Marty, 2026-08-05: 'make sure there is a button "Save to Studio Library", and
# in studio library add "Render PResets" filter, these filters are also meant
# to be shared within users of this tool if needed.'
import grid as gridmod       # noqa: E402
import library as librarymod  # noqa: E402
import panels as panelsmod   # noqa: E402

ok(rp.ITEM_EXT in librarymod.ITEM_EXTS,
   "the extension is in library.ITEM_EXTS (list 2 of 3)")
ok(librarymod.DATA_FILES.get(rp.ITEM_TYPE) == rp.ITEM_DATA,
   "and its payload file is registered, or read_data returns {}")
ok(rp.ITEM_DATA in librarymod._PAYLOAD_FILES,
   "and it is a versioned payload, or Versions... would silently keep nothing")

# ⚠ THE LIST WHOSE ABSENCE IS SILENT. `.vgroups` and `.picker` both saved,
# scanned and never drew, for exactly this reason.
sidebar_types = set(win.tabs.currentWidget().sidebar.type_checks)
ok(rp.ITEM_TYPE in sidebar_types,
   "the sidebar has a filter for it (list 3 of 3 - the SILENT one)")
ok(gridmod.type_label(rp.ITEM_TYPE) == "render presets",
   "the filter reads 'render presets', not the folder extension (%r)"
   % gridmod.type_label(rp.ITEM_TYPE))
ok(rp.ITEM_TYPE in theme.TYPE_COLORS,
   "it has its own colour, so the tile and the filter icon are not the accent")


def _icon_is_drawn(typ):
    """A filter icon that draws nothing reads as a bug (picker had none)."""
    pm = gridmod.type_icon(typ, 16).pixmap(16, 16).toImage()
    return any(pm.pixelColor(x, y).alpha() > 0
               for x in range(16) for y in range(16))


ok(_icon_is_drawn(rp.ITEM_TYPE), "and a glyph that actually paints pixels")

# --- write / scan / see it -------------------------------------------------
LIB = config.DEFAULT_LIBRARY
preset = {"name": "Final 4K", "engine": "CYCLES", "blender": "5.2.0 LTS",
          "scene": "Scene",
          "groups": {"format": {"label": "Resolution & frame rate",
                                "values": {"render.resolution_x": 3840,
                                           "render.resolution_y": 2160}}}}
item = rp.write_library_item(LIB, "", "Final 4K", preset)
ok(os.path.basename(item) == "Final 4K" + rp.ITEM_EXT,
   "write_library_item: the folder carries the extension (%s)"
   % os.path.basename(item))
with open(os.path.join(item, rp.ITEM_DATA), encoding="utf-8") as f:
    stored = json.load(f)
ok(stored["type"] == rp.ITEM_TYPE and stored["groups"] == preset["groups"],
   "the payload keeps the type tag and the settings unchanged")
ok(stored["metadata"]["settings"] == 2
   and stored["metadata"]["engine"] == "CYCLES"
   and stored["metadata"]["created"],
   "and metadata shaped like every other item type's")

try:
    rp.write_library_item(LIB, "", "Final 4K", preset)
    ok(False, "writing over an existing item without overwrite refuses")
except FileExistsError:
    ok(True, "writing over an existing item without overwrite refuses")

rp.write_library_item(LIB, "", "Final 4K", preset, overwrite=True)
ok(os.path.isdir(os.path.join(item, librarymod.VERSIONS_DIR)),
   "overwriting VERSIONS the old payload, same as an add-on-side save - a "
   "replaced preset is recoverable from Versions...")

folders, items = librarymod.scan(LIB)
found = [i for i in items if i.type == rp.ITEM_TYPE]
ok(len(found) == 1 and found[0].name == "Final 4K",
   "library.scan finds it as a renderpreset item")
ok(found[0].read_data()["groups"] == preset["groups"],
   "and read_data gives back the preset the tab can apply, unchanged")
ok(found[0].bulk_count() == 0,
   "it is not a BULK type - a preset holding 40 settings is just what the type "
   "is, so no stack badge")

view = win.tabs.currentWidget()
view.rescan()
ok(any(i.type == rp.ITEM_TYPE for i in view.items),
   "the library tab scans it")
ok(view.grid.count() == len(view.items),
   "⚠ AND IT SURVIVES refilter() INTO THE GRID (%d of %d) - the check "
   "`.vgroups` and `.picker` never had" % (view.grid.count(), len(view.items)))
view.sidebar.type_checks[rp.ITEM_TYPE].setChecked(False)
view.refilter()
_visible = sum(not view.grid.item(i).isHidden()
               for i in range(view.grid.count()))
ok(_visible == len(view.items) - 1,
   "and unticking the filter takes it back out again (refilter HIDES rows "
   "now — visible rows are the filter, grid.count() stays the library size)")
view.sidebar.type_checks[rp.ITEM_TYPE].setChecked(True)

# --- the button ------------------------------------------------------------
tool2 = rp.RenderPresetsTool(StubBridge(), win)
ok(tool2.btn_library.isEnabled(), "Save to Studio Library is live with a "
   "preset selected")
gated2 = rp.RenderPresetsTool(StubBridge(reason=REASON), win)
ok(gated2.btn_library.isEnabled(),
   "⚠ and it stays live even when the add-on is too old - writing a library "
   "item is pure disk work on data already in hand, and needing Blender open "
   "to file something away would be a worse feature")

libs = tool2.libraries()
ok(libs and libs[0]["path"] == LIB,
   "the tool offers the WINDOW's live libraries, not just the ones on disk")
dlg = rp.SaveToLibraryDialog(parent, libs, name="Final 4K")
ok(dlg.folder.count() >= 1 and dlg.folder.itemData(0) == "",
   "the save dialog offers the library root first")
ok(dlg.exists(), "it spots a name that is already taken")
ok("Versions" in dlg.note.text(), "...and says what replacing would do")
dlg.name.setText("")
ok(not dlg.buttons.button(rp.QDialogButtonBox.Save).isEnabled(),
   "Save is off without a name")

# --- applying one, from the library ----------------------------------------
# The real routing, driven through LibraryView.on_apply with a recording
# bridge: the item's own payload must reach `render_preset_apply` unchanged,
# because the item IS the preset and a conversion step here could drift from
# the Rendering tab's.
class RecordingBridge(StubBridge):
    def status(self, *a, **k):
        return {}


rec = RecordingBridge()
rec.report = dict(rec.report, summary="7 settings changed.")
real_bridge = view.bridge
view.bridge = rec
target = next(i for i in view.items if i.type == rp.ITEM_TYPE)
view.on_apply(target, {"blend": 1.0, "extend": False, "remap": False,
                       "mirror": False, "shapes_to_active": False,
                       "frame_start": None, "frame_end": None})
view.bridge = real_bridge
ok(rec.applied, "on_apply routed a .renderpreset item to render_preset_apply")
sent_item, sent_groups = rec.applied[-1]
ok(sent_item["groups"] == preset["groups"] and sent_groups is None,
   "and sent the item's own payload, whole and unnarrowed")

print("\n%d passed, %d failed" % (len(PASS), len(FAIL)), flush=True)
sys.exit(1 if FAIL else 0)
