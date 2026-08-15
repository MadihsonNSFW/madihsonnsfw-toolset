# Marty's 2026-08-05 anim/playblast batch, app side.
#
#   python tests\app_anim_options_test.py
#
# The Blender half is `anim_options_test.py`. This one covers the app: the Save
# Anim options dialog, the tile badges that report what an item holds, the
# playblast dialog's new defaults and its output folder, the two Watch buttons,
# the layers warning, and the system monitor that now only ticks while it is on
# screen.
import json
import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

sys.path.insert(0, os.path.join(_ROOT, "app"))

from PySide6.QtWidgets import QApplication, QLabel  # noqa: E402

import config  # noqa: E402

# ⚠ Same isolation as the other app suites: a temp APP_DIR and a temp LIBRARY,
# both set BEFORE main is imported. Building a MainWindow scans whatever
# `libraries` says, and the default is Marty's REAL library.
TMP = tempfile.mkdtemp(prefix="madi_animopt_")
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
config.DEFAULT_LIBRARY = os.path.join(TMP, "library")
config.DEFAULTS["libraries"] = [{"name": "Test", "path": config.DEFAULT_LIBRARY}]
os.makedirs(config.DEFAULT_LIBRARY, exist_ok=True)

import bridge as bridgemod   # noqa: E402
import grid as gridmod       # noqa: E402
import lastrender            # noqa: E402
import library as librarymod  # noqa: E402

PASS, FAIL = [], []


def ok(cond, label):
    (PASS if cond else FAIL).append(label)
    print(("ok   " if cond else "FAIL ") + label, flush=True)


app = QApplication.instance() or QApplication([])

import main as mainmod       # noqa: E402
import panels as panelsmod   # noqa: E402

# ===================================================== the Save Anim dialog ===
dlg = mainmod.SaveAnimDialog(None, {}, None, None, scene_range=(7, 96))
ok((dlg.frame_start.value(), dlg.frame_end.value()) == (7, 96),
   "⚠ blank frame boxes fill in with the SCENE TIMELINE, as real numbers you "
   "can see - Marty: 'If the frame range is not set by user it should be "
   "default timeline' (%r)" % (dlg.frames(),))
ok(dlg.values() == {"bake": False, "keep_modifiers": True,
                    "include_props": False},
   "the defaults are what Save Anim already did: modifiers kept, no properties "
   "(%r)" % dlg.values())

seeded = mainmod.SaveAnimDialog(None, {}, 20, 40, scene_range=(1, 250))
ok(seeded.frames() == (20, 40),
   "the panel's Start/End boxes still SEED the dialog (same as Export Abc)")

backwards = mainmod.SaveAnimDialog(None, {}, 90, 30, scene_range=(1, 250))
ok(backwards.frames() == (30, 90), "a backwards range is put the right way up")

remembered = mainmod.SaveAnimDialog(
    None, {"bake": True, "keep_modifiers": True, "include_props": True},
    1, 10)
ok(remembered.chk_bake.isChecked() and remembered.chk_props.isChecked(),
   "the stored choices come back (config.json `anim_export`)")
ok(not remembered.chk_modifiers.isEnabled(),
   "⚠ 'keep modifiers' greys out while Bake is on - baking replaces them with "
   "keys, and a live control that does nothing is worse than a greyed one")
ok(remembered.values()["keep_modifiers"] is False,
   "⚠ …and it REPORTS False while baking, whatever the greyed tick holds: the "
   "add-on decides the same thing independently and the badge is drawn from "
   "this, so the two must not disagree about what the item contains")
remembered.chk_bake.setChecked(False)
ok(remembered.chk_modifiers.isEnabled()
   and remembered.values()["keep_modifiers"] is True,
   "unticking Bake restores the choice underneath rather than losing it")

warned = mainmod.SaveAnimDialog(None, {}, 1, 10, layer_warning="Three layers.")
labels = [w.text() for w in warned.findChildren(QLabel)]
ok(any("Three layers." in t for t in labels),
   "the layers warning is shown in the dialog (%r)" % labels[:4])
ok(warned.layout().itemAt(0).widget().text().startswith("⚠"),
   "⚠ …at the TOP, above the settings: it is the reason to close this dialog "
   "and go and do something else, so it has to be read before the tickboxes "
   "rather than found under them after pressing Save")
plain_dlg = mainmod.SaveAnimDialog(None, {}, 1, 10)
plain_labels = [w.text() for w in plain_dlg.findChildren(QLabel)]
ok(not any("⚠" in t for t in plain_labels),
   "…and no warning at all when there is nothing to warn about")

# ============================================ the warning's own decision ======
win = mainmod.MainWindow()
view = win.tabs.currentWidget()


class LayersBridge:
    def __init__(self, status):
        self._status = status

    def anim_layers_status(self, **kw):
        if isinstance(self._status, Exception):
            raise self._status
        return self._status


real_bridge = view.bridge
view.bridge = LayersBridge({"error": None, "layers": [{}, {}, {}],
                            "foreign_nla": False})
msg = view.anim_layer_warning()
ok(msg and "3 animation layers" in msg,
   "three layers -> a warning naming the count (%r)" % msg)
ok(msg and "Merge / Bake" in msg,
   "…and it points at the tab that fixes it, which is what was asked for")

view.bridge = LayersBridge({"error": None, "layers": [{}], "foreign_nla": True})
ok((view.anim_layer_warning() or "").find("NLA") != -1,
   "foreign NLA tracks warn too, even with one layer")

view.bridge = LayersBridge({"error": None, "layers": [{}],
                            "foreign_nla": False})
ok(view.anim_layer_warning() is None, "one plain layer -> no warning")

view.bridge = LayersBridge({"error": "no armature"})
ok(view.anim_layer_warning() is None,
   "⚠ an ERROR means no warning, not a warning about the error - a false "
   "alarm here teaches him to ignore the real one")

view.bridge = LayersBridge(bridgemod.BridgeError("offline"))
ok(view.anim_layer_warning() is None,
   "…and an old add-on or a closed Blender is silent too")
view.bridge = real_bridge

# ================================================= the anim tile badges =======
LIB = config.DEFAULT_LIBRARY


def write_anim(name, meta):
    d = os.path.join(LIB, name + ".anim")
    os.makedirs(d, exist_ok=True)
    data = {"type": "anim", "metadata": dict(meta), "bones": {},
            "curves": [{"bone": "b", "data_path": "x", "array_index": 0,
                        "keys": [[1, 0.0]], "modifiers": []}]}
    with open(os.path.join(d, "anim.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
    return d


write_anim("everything", {"frame_start": 1, "frame_end": 10, "baked": True,
                          "fcurve_modifiers": True, "bone_props": True})
write_anim("nothing", {"frame_start": 1, "frame_end": 10})
write_anim("mods_only", {"frame_start": 1, "frame_end": 10,
                         "fcurve_modifiers": True})
os.makedirs(os.path.join(LIB, "a_pose.pose"), exist_ok=True)
with open(os.path.join(LIB, "a_pose.pose", "pose.json"), "w",
          encoding="utf-8") as f:
    json.dump({"type": "pose", "metadata": {}, "bones": {}}, f)

_folders, items = librarymod.scan(LIB)
by_name = {i.name: i for i in items}

ok(by_name["everything"].anim_flags() == ("baked", "modifiers", "props"),
   "an item with all three flags reports them in draw order (%r)"
   % (by_name["everything"].anim_flags(),))
ok(by_name["nothing"].anim_flags() == (),
   "⚠ an item saved BEFORE these flags existed gets none - nobody knows what "
   "those files kept, and guessing would badge them wrongly")
ok(by_name["mods_only"].anim_flags() == ("modifiers",),
   "and only the flags it actually has")
ok(by_name["a_pose"].anim_flags() == (),
   "⚠ THE TYPE CHECK COMES FIRST - a .pose is never parsed for anim flags, "
   "same performance rule as bulk_count")

# ⚠ metadata is read WITHOUT parsing the whole file - the badges are drawn on
# every tile paint, and an .anim for a 461-bone rig is megabytes of curves.
big = os.path.join(LIB, "big.anim")
os.makedirs(big, exist_ok=True)
with open(os.path.join(big, "anim.json"), "w", encoding="utf-8") as f:
    f.write('{"type": "anim", "metadata": {"baked": true}, "curves": [')
    f.write(",".join('{"bone": "b%d"}' % i for i in range(20000)))
    f.write("]}")
peeked = librarymod._peek_metadata(os.path.join(big, "anim.json"))
ok(peeked == {"baked": True},
   "_peek_metadata reads the metadata block out of a huge file (%r)" % peeked)

# and it is never WRONG, only slower, when the fast path does not fit
odd = os.path.join(LIB, "odd.anim")
os.makedirs(odd, exist_ok=True)
with open(os.path.join(odd, "anim.json"), "w", encoding="utf-8") as f:
    json.dump({"type": "anim", "curves": [{"bone": "x" * 20000}],
               "metadata": {"bone_props": True}}, f)
ok(librarymod._peek_metadata(os.path.join(odd, "anim.json"))
   == {"bone_props": True},
   "⚠ metadata written LAST, past the read window, falls back to a full parse "
   "- the fast path is slower in the worst case, never wrong")
with open(os.path.join(odd, "anim.json"), "w", encoding="utf-8") as f:
    f.write("{ this is not json")
ok(librarymod._peek_metadata(os.path.join(odd, "anim.json")) == {},
   "a corrupt item json is {} rather than an exception on a paint path")

# the badge has to actually be drawn, and change the pixmap
flagged = gridmod.thumbnail_pixmap(by_name["everything"], 128)
unflagged = gridmod.thumbnail_pixmap(by_name["nothing"], 128)
ok(flagged.toImage() != unflagged.toImage(),
   "a flagged tile does not look like an unflagged one")
ok(gridmod._stamp_flags(gridmod.QPixmap(64, 64), ()) is not None,
   "no flags draws nothing and returns the pixmap unharmed")

# ⚠ the flags are part of the cache key, or an item re-saved with different
# options keeps its old badges until the app restarts
key_probe = []
real_stamp = gridmod._stamp_flags


def spy(pm, flags):
    key_probe.append(flags)
    return real_stamp(pm, flags)


gridmod._stamp_flags = spy
gridmod._placeholder_cache.clear()
resaved = by_name["everything"]
gridmod.placeholder_pixmap(resaved, 100)
before = len(key_probe)
gridmod.placeholder_pixmap(resaved, 100)
cached_hit = len(key_probe) == before
# re-save it with the modifiers switched off, exactly as the dialog would
write_anim("everything", {"frame_start": 1, "frame_end": 10, "baked": True,
                          "fcurve_modifiers": False, "bone_props": True})
resaved.mtime += 1                       # a re-save moves the folder's mtime
gridmod.placeholder_pixmap(resaved, 100)
gridmod._stamp_flags = real_stamp
ok(cached_hit, "the placeholder pixmap is cached")
ok(resaved.anim_flags() == ("baked", "props"),
   "a re-save with different options changes the flags (%r)"
   % (resaved.anim_flags(),))
ok(key_probe[-1] == ("baked", "props") and len(key_probe) > before,
   "⚠ …and the tile is REDRAWN, because the flags are in the cache key - "
   "otherwise it keeps a badge that is no longer true until the app restarts")
write_anim("everything", {"frame_start": 1, "frame_end": 10, "baked": True,
                          "fcurve_modifiers": True, "bone_props": True})
resaved.mtime += 1

# ============================================ playblasts are gone from here ===
with open(os.path.join(LIB, "take01.mp4"), "wb") as f:
    f.write(b"x")
_f2, items2 = librarymod.scan(LIB)
ok(not any(i.type == "playblast" for i in items2),
   "a loose mp4 is no longer scanned as an item (Marty: 'remove playblasts "
   "from showing in anim library')")
ok("playblast" not in view.sidebar.type_checks,
   "…and the type filter went with it")
ok(set(view.sidebar.type_checks) >= {e[1:] for e in librarymod.ITEM_EXTS},
   "⚠ every REAL item type still has its filter - the list whose absence is "
   "silent (%r)" % sorted(view.sidebar.type_checks))

# ================================================= the playblast dialog =======
pdlg = mainmod.PlayblastDialog(view, TMP)
vals = pdlg.values()
ok(pdlg.source.currentIndex() == 1 and vals["use_camera"] is True,
   "'Active camera' is the default source (Marty, 2026-08-05)")
ok(vals["background"] is True,
   "'Run in background (Render Queue)' is on by default")

pdlg.source.setCurrentIndex(0)           # Viewport (as shown)
ok(pdlg.values()["background"] is False,
   "⚠ switching to Viewport still unticks it - a headless Blender has no "
   "viewport to render 'as shown' from, and the default must not defeat that")

blocked = mainmod.PlayblastDialog(view, TMP, background_block="too old")
ok(blocked.values()["background"] is False,
   "⚠ and an add-on too old for snapshot_blend beats the default as well")

sourced = mainmod.PlayblastDialog(view, TMP, dir_source="From Blender.")
texts = [w.text() for w in sourced.findChildren(QLabel)]
ok(any("From Blender." in t for t in texts),
   "the dialog says WHERE the folder came from")

# ------------------------------------------------- which folder it defaults to
view.lib_cfg["playblast_dir"] = os.path.join(TMP, "remembered")
chosen, why = view._playblast_dir({"output_dir": r"D:\renders\shot"})
ok(chosen == r"D:\renders\shot",
   "⚠ BLENDER'S OWN OUTPUT FOLDER WINS, every time the dialog opens. "
   "Preferring the remembered one would make the setting work exactly once - "
   "the first playblast would save its folder and shadow the scene's forever")
ok("Blender" in why, "…and the dialog can say so")

chosen, why = view._playblast_dir({})
ok(chosen == os.path.join(TMP, "remembered"),
   "when Blender cannot say, the folder he last chose is the best answer left")
ok("didn't say" in why, "…and it explains why it is not Blender's")

del view.lib_cfg["playblast_dir"]
chosen, _why = view._playblast_dir({"output_dir": None})
ok(chosen.endswith("_playblasts"),
   "and with neither, the library's own _playblasts folder")

# ==================================================== the two Watch buttons ===
record = lastrender.state_path()
saved_record = None
if os.path.isfile(record):               # never clobber Marty's real one
    with open(record, "rb") as f:
        saved_record = f.read()
try:
    mp4 = os.path.join(TMP, "watch_me.mp4")
    with open(mp4, "wb") as f:
        f.write(b"x")
    ok(lastrender.note(mp4) is True, "the record is written")
    ok(lastrender.last() == mp4, "…and read back")

    view.sync_watch_button()
    ok(view.btn_watch.isEnabled(), "▶ enables once there is a render")
    ok(mp4 in view.btn_watch.toolTip(), "…and its tooltip names the file")

    os.remove(mp4)
    view.sync_watch_button()
    ok(not view.btn_watch.isEnabled(),
       "⚠ a render that has been tidied away disables the button - opening a "
       "missing file is worse than a dead button")

    sent = []

    class NoteBridge:
        def note_render(self, path):
            sent.append(path)

    win.bridge = NoteBridge()
    with open(mp4, "wb") as f:
        f.write(b"x")
    win.note_render(mp4)
    ok(sent == [mp4],
       "⚠ a BACKGROUND playblast is pushed to Blender - a headless process "
       "rendered it, so the live session has no other way to hear about it")
    ok(view.btn_watch.isEnabled(), "…and every tab's button wakes up")

    sent.clear()
    win.note_render(mp4, tell_blender=False)
    ok(sent == [],
       "a BLOCKING playblast is not pushed - the add-on ran it and already "
       "recorded it before replying")

    class DeadBridge:
        def note_render(self, path):
            raise bridgemod.BridgeError("Blender is closed")

    win.bridge = DeadBridge()
    win.note_render(mp4)
    ok(lastrender.last() == mp4,
       "a closed Blender does not stop the APP's own button working")
finally:
    if saved_record is None:
        try:
            os.remove(record)
        except OSError:
            pass
    else:
        with open(record, "wb") as f:
            f.write(saved_record)

# ============================================ the bake tick moved out of here =
info = view.info
ok(not hasattr(info, "chk_bake"),
   "'Bake every frame' is no longer parked in the info panel - it moved into "
   "the dialog with the rest of the save-time options")
ok("bake" not in info.options(),
   "…and options() no longer reports it, so nothing can read a stale one")
ok("frame_start" in info.options(),
   "the frame boxes stayed: they seed the dialog")

# ============================================== the system monitor pauses =====
# ⚠ Driven by REALLY SWITCHING TABS, not by calling showEvent by hand. The whole
# claim is that Qt's own visibility is a good enough signal — poking the handler
# directly would prove only that the handler starts a timer.
queue = win.render_queue
ok(not queue._sys_tick.isActive(),
   "the monitor does not tick before the window is even shown")
win.show()
app.processEvents()


def section(title):
    for i in range(win.main_tabs.count()):
        if win.main_tabs.tabText(i) == title:
            return i
    raise AssertionError("no %r tab" % title)


win.main_tabs.setCurrentIndex(section("Rendering"))
app.processEvents()
ok(queue._sys_tick.isActive(),
   "RAM/VRAM sampling runs while the Rendering tab is the visible one")
win.main_tabs.setCurrentIndex(section("Studio Library"))
app.processEvents()
ok(not queue._sys_tick.isActive(),
   "⚠ …and STOPS on any other tab (Marty, 2026-08-05) - it used to tick once a "
   "second for the whole life of the app, for cards nobody was looking at")
win.main_tabs.setCurrentIndex(section("Rendering"))
app.processEvents()
ok(queue._sys_tick.isActive(), "…and starts again on the way back")
win.hide()
app.processEvents()
ok(not queue._sys_tick.isActive(),
   "a hidden window stops it too - one pair of handlers covers every way this "
   "widget can leave the screen")

# ================================================== bridge: the new plumbing ==
sent_cmds = {}


class RecordingClient(bridgemod.Bridge):
    def request(self, cmd, params=None, timeout=None):
        sent_cmds[cmd] = params
        return {}


client = RecordingClient()
client.save_anim("root", "f", "n", frame_start=1, frame_end=2,
                 keep_modifiers=False, include_props=True)
ok(sent_cmds["save_anim"]["keep_modifiers"] is False
   and sent_cmds["save_anim"]["include_props"] is True,
   "the client sends both new options")
client.note_render(r"C:\x.mp4")
ok(sent_cmds["note_render"] == {"path": r"C:\x.mp4"},
   "…and note_render carries the path")

# ⚠ A FLOOR, NEVER `==`. This was pinned exactly to "0.20.0" and broke the
# moment the add-on moved to 0.21.0 — a test failing because the project moved
# forward normally, which is the whole reason the floor rule exists.
_want = (0, 20, 0)
_have = tuple(int(n) for n in bridgemod.EXPECTED_ADDON_VERSION.split("."))
ok(_have >= _want,
   "the app expects add-on %s or newer (%s)"
   % (".".join(str(n) for n in _want), bridgemod.EXPECTED_ADDON_VERSION))
ok("note_render" in bridgemod.GATED_COMMANDS,
   "note_render is gated, so an older add-on costs Blender's button and "
   "nothing else")
ok("save_anim" not in bridgemod.GATED_COMMANDS,
   "⚠ save_anim is NOT gated: it has existed since the first build, so a "
   "capability check would answer 'yes' and prove nothing. The reply's "
   "`options` echo is the check instead")

# ================================ applying an anim must not freeze the app ====
# Marty, 2026-08-06: "applying complex or baked animation also causes a short
# freeze". A baked .anim for the 461-bone rig is thousands of curves, and
# `on_apply` used to make that bridge call INLINE, on the GUI thread - so the
# window stopped painting for the whole paste. It now goes through BridgeWorker
# behind the same busy grey-out the alembic paths use.
import threading  # noqa: E402
import time       # noqa: E402


class SlowAnimBridge:
    """Records which thread the paste ran on, and blocks until released."""

    def __init__(self):
        self.thread = None
        self.kwargs = None
        self.release = threading.Event()

    def status(self, *a, **k):
        return {}

    def apply_anim(self, path, **kw):
        self.thread = threading.current_thread()
        self.kwargs = kw
        self.release.wait(5.0)
        return {"curves": 1200, "pasted_range": [1, 48], "missing": 0}


# The view was built before this suite wrote its .anim items, so it has to be
# told to look again.
view.rescan()
anim_item = next((i for i in view.items if i.type == "anim"), None)
ok(anim_item is not None, "apply: there is an .anim item to drive this with")

slow = SlowAnimBridge()
view.bridge = slow
opts = {"blend": 1.0, "extend": False, "remap": False, "mirror": False,
        "shapes_to_active": False, "selected_only": False, "key": False,
        "anim_mode": "REPLACE", "start_at": None,
        "frame_start": None, "frame_end": None}
view.on_apply(anim_item, opts)

# on_apply must have RETURNED while the bridge call is still in flight - that
# return is the whole fix.
for _ in range(200):                      # give the worker a moment to enter
    if slow.thread is not None:
        break
    QApplication.processEvents()
    time.sleep(0.01)
ok(slow.thread is not None, "apply: the paste actually started")
ok(slow.thread is not threading.main_thread(),
   "⚠ apply: an .anim pastes on a WORKER thread, not the GUI thread - this is "
   "the freeze Marty reported")
ok(win.capture_progress.isVisible() or win._captures > 0,
   "apply: ...and the busy indicator is up while it runs")
slow.release.set()
for _ in range(200):
    if getattr(view, "_anim_worker", None) is None:
        break
    QApplication.processEvents()
    time.sleep(0.01)
ok(getattr(view, "_anim_worker", None) is None,
   "apply: the worker is released when it finishes (no leak)")
ok(win._captures == 0, "apply: and the grey-out lifts")

# The options are bound at CALL time, not read inside the thread - a worker
# that read live UI state could paste with settings the user had since changed.
ok(slow.kwargs and slow.kwargs.get("mode") == "REPLACE"
   and slow.kwargs.get("blend") == 1.0,
   "apply: the options are captured when the paste is requested")

view.bridge = real_bridge

print("\n%d passed, %d failed" % (len(PASS), len(FAIL)), flush=True)
sys.exit(1 if FAIL else 0)
