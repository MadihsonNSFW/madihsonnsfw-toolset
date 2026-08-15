# Optimization tab, offscreen: the poll, the settings store, the confirmation
# before a long run, the capability gate and the busy greying.
#
#   python tests\app_optimizer_test.py
#
# The tab is a remote control for the engine inside Blender, so everything here
# is about reading a status and sending the right parameters - never about
# resizing anything (optimizer_test.py does the real files).
import os
import sys
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

sys.path.insert(0, os.path.join(_ROOT, "app"))

from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

import bridge as bridgemod  # noqa: E402
import config as configmod  # noqa: E402
import optimizer as optmod  # noqa: E402

PASS = []
FAIL = []


def ok(cond, label):
    (PASS if cond else FAIL).append(label)
    print(("ok   " if cond else "FAIL ") + label, flush=True)


def status(**over):
    """A plausible opt_status reply."""
    base = {
        "scene": "Scene",
        "camera": "Camera",
        "resolution": [1920, 1080, 100],
        "frame_range": [1, 250],
        "objects": 51,
        "selected": 2,
        "selected_objects": ["Hero", "Prop"],
        "images": 94,
        "managed": [
            {"name": "skin.png", "size": 1024, "original": "//tex/skin.png",
             "current": "C:/cache/abc_1024.png", "missing": False},
            {"name": "wall.png", "size": 512, "original": "//tex/wall.png",
             "current": "C:/cache/def_512.png", "missing": True},
        ],
        "decimated": ["Rock.001"],
        "preview_running": False,
        "default_cache": r"C:\Users\Someone\madi_optimizer_cache",
        "targets": ["SELECTED", "SCENE", "ALL_OBJECTS", "IMAGES_NO_HDR",
                    "IMAGES_HDR", "ALL_IMAGES"],
        "addon_can_resize": True,
    }
    base.update(over)
    return base


def progress(**over):
    """A plausible opt_progress reply."""
    base = {"active": True, "phase": "Resizing textures", "done": 7,
            "total": 40, "item": "skin_diffuse.png", "serial": 3,
            "started": 100.0, "elapsed": 4.5}
    base.update(over)
    return base


class StubBridge:
    def __init__(self):
        self.calls = []
        self.reply = status()
        self.raise_error = False
        self.reason = None
        self.progress_reply = progress()
        self.progress_error = False
        self.can_progress = True
        self.can_groups = True
        self.can_clear = True

    def feature_reason(self, feature):
        return self.reason

    def supports(self, command):
        if command == "opt_progress":
            return self.can_progress
        if command == "opt_group_apply":
            return self.can_groups
        if command == "opt_clear_cache":
            return self.can_clear
        return True

    def opt_progress(self, poll=False):
        # Explicit rather than caught by __getattr__: it is the one opt_* call
        # that does NOT answer with a status.
        self.calls.append(("opt_progress", (), {"poll": poll}))
        if self.progress_error:
            raise bridgemod.BridgeError("bridge down")
        return self.progress_reply

    def _record(self, name, *a, **kw):
        self.calls.append((name, a, kw))
        if self.raise_error:
            raise bridgemod.BridgeError("bridge down")
        return self.reply

    def __getattr__(self, name):
        if not name.startswith("opt_"):
            raise AttributeError(name)
        return lambda *a, **kw: self._record(name, *a, **kw)

    def named(self, name):
        return [c for c in self.calls if c[0] == name]


class StubWindow:
    capturing = False
    _previewing = False

    def __init__(self):
        self.cfg = {"optimizer": dict(configmod.DEFAULTS["optimizer"])}
        self.saved = 0
        self.optimizer = None        # the page, once it has been built
        self.captures = 0
        self.capture_calls = []

    def bridge_free_for_tools(self):
        return True

    def begin_capture(self, label, verb="capturing"):
        self.captures += 1
        self.capture_calls.append((label, verb))

    def end_capture(self):
        self.captures = max(0, self.captures - 1)


# config.save writes next to main.py; the stub window is not the real one, so
# nothing here may reach the user's file.
_writes = []
configmod.save = lambda cfg: _writes.append(cfg)

app = QApplication.instance() or QApplication([])
stub = StubBridge()
win = StubWindow()


def settle(tool, timeout=5.0):
    """Pump the event loop until the tool's run has come back.

    ⚠ EVERY RUN IS OFF THE GUI THREAD NOW, so a test that clicked a button and
    read the panel on the next line would be reading it before the reply
    exists. That is the point of the change - the app stays responsive during a
    run - and it is why the checks below have to wait here.
    """
    deadline = time.time() + timeout
    while tool._busy and time.time() < deadline:
        app.processEvents()
        time.sleep(0.001)
    app.processEvents()
    return not tool._busy

# ------------------------------------------------------------------- defaults
ok("optimizer" in configmod.DEFAULTS,
   "config: the tab's settings have a defaults group, so a config written "
   "before this tab existed still gets them")
defaults = configmod.DEFAULTS["optimizer"]
for key in ("target", "quality", "min_size", "max_size", "fixed_size",
            "animation", "frame_step", "meshes", "face_floor",
            "full_distance", "low_distance", "low_ratio", "cache_dir"):
    ok(key in defaults, "config: '%s' has a default" % key)
ok(defaults["meshes"] is False,
   "config: decimation is OFF by default - it can wreck a character, so it is "
   "opt-in rather than something that happens to someone")
ok(defaults["target"] == "SCENE",
   "config: the default target is this scene, not the whole file")

# --------------------------------------------------------------------- shell
page = optmod.OptimizerPage(stub, win)
adaptive = optmod.AdaptiveTool(stub, win)
fixed = optmod.FixedSizeTool(stub, win)
meshes = optmod.MeshesTool(stub, win)
restore = optmod.RestoreTool(stub, win)
memory = optmod.MemoryTool(stub, win)
page.add_tool(adaptive, "Adaptive", group="Optimize")
page.add_tool(fixed, "Fixed size", group="Optimize")
page.add_tool(meshes, "Meshes", group="Optimize")
page.add_tool(restore, "Restore", group="Maintenance")
page.add_tool(memory, "Memory report", group="Maintenance")
for tool in (fixed, meshes, restore, memory):
    adaptive.status_refreshed.connect(tool.apply_status)
# The real MainWindow does this at the end of _build_optimizer; the tools find
# the tab's one progress bar through it.
win.optimizer = page

titles = [t for t, _g, _w in page._tools]
ok(titles == ["Adaptive", "Fixed size", "Meshes", "Restore", "Memory report"],
   "shell: the rail lists five tools in add order (got %s)" % titles)
groups = [g for _t, g, _w in page._tools]
ok(groups[:3] == ["Optimize"] * 3 and groups[3:] == ["Maintenance"] * 2,
   "shell: doing and undoing are separate groups")
ok(page.EMPTY_TEXT != optmod.RenderingPage.EMPTY_TEXT,
   "shell: the tab has its own empty-state text")

# ---------------------------------------------------------------------- poll
stub.calls = []
adaptive.refresh()
calls = stub.named("opt_status")
ok(len(calls) == 1, "poll: one status call")
ok(calls[0][2].get("poll") is True,
   "poll: it passes poll=True - a dead localhost port drops the SYN here, so "
   "an un-flagged poll burns the full timeout on the GUI thread every tick")
ok(adaptive.timer is not None and not adaptive.timer.isActive(),
   "poll: parked after construction — it used to start in __init__ and ask "
   "Blender for opt_status every 2.5 s for the life of the app, from a tab "
   "nobody had opened (PERF_PLAN O1)")
# ⚠ Shown via the PAGE, not the bare widget: add_tool re-parented adaptive
# into the page's stack, and a child of a hidden parent gets no showEvent —
# which is also exactly how the app shows it.
page.show()
app.processEvents()
ok(adaptive.timer.isActive()
   and adaptive.timer.interval() == optmod.POLL_MS,
   "poll: showing the tab starts it, on the tab's own interval (%d ms)"
   % optmod.POLL_MS)
page.hide()
app.processEvents()
ok(not adaptive.timer.isActive(),
   "poll: hiding the tab parks it again — the same show/hide rule as the "
   "render queue's sysmon")
stub.calls = []   # the showEvent refresh above is not the next section's
ok(optmod.POLL_MS >= 1500,
   "poll: no faster than the picker's - the reply walks every image in the "
   "file, and nothing here changes unless the user changes it")

stub.raise_error = True
before = adaptive.status.text()
adaptive.refresh()
ok(adaptive.status.text() == before,
   "poll: a dead bridge does not overwrite the panel - the status bar already "
   "says so")
stub.raise_error = False

# ------------------------------------------------------- status -> the panels
adaptive.refresh()
ok("Camera" in adaptive.status.text(),
   "status: the camera is named, because everything is measured through it")
ok(restore.table.rowCount() == 2,
   "status: the Restore list shows both managed textures")
ok(restore.table.item(0, 0).text() == "skin.png",
   "status: by name")
ok(restore.table.item(1, 2).text() == "copy missing",
   "status: a stand-in that has gone missing is called out, not shown as fine")
ok(restore.btn_images.isEnabled() and restore.btn_meshes.isEnabled(),
   "status: both restore buttons are live when there is something to restore")
ok(meshes.btn_revert.isEnabled(),
   "status: the Meshes page offers to remove what it added")

# ⚠ `broadcast`, not `apply_status`: apply_status is the RECEIVER on each tool,
# and only the poll owner's broadcast fans a status out to the other four. A
# test that pushed apply_status would leave the other tools on stale data and
# then blame them for it.
ok(hasattr(adaptive, "status_refreshed"),
   "shell: the Adaptive tool owns the fan-out signal")

# A control that cannot act in the state it is shown in is worse than a missing
# one - the same rule the licensing panel's 'Check again' button follows.
adaptive.broadcast(status(managed=[], decimated=[]))
ok(not restore.btn_images.isEnabled() and not restore.btn_meshes.isEnabled(),
   "status: with nothing optimized, the restore buttons are DISABLED rather "
   "than sitting there doing nothing")

# No camera: the adaptive pass cannot run at all, and says why.
adaptive.broadcast(status(camera=None))
ok(not adaptive.btn_run.isEnabled(),
   "status: no camera disables the run button")
ok("camera" in adaptive.status.text().lower(),
   "status: and explains that it measures through the camera")
ok(not meshes.btn_run.isEnabled(),
   "status: decimation needs the camera too, and is disabled with it")

# A Blender without OpenImageIO can never resize.
adaptive.broadcast(status(addon_can_resize=False))
ok("OpenImageIO" in adaptive.status.text(),
   "status: a Blender that cannot resize says so instead of failing per image")
ok(not fixed.btn_run.isEnabled(),
   "status: and the fixed-size tool is disabled by the same reply")
adaptive.broadcast(status())

# ------------------------------------------------------------------ settings
adaptive.slider_quality.setValue(0.25)
ok(abs(win.cfg["optimizer"]["quality"] - 0.25) < 1e-6,
   "settings: moving a slider saves it")
ok(_writes, "settings: and writes config.json")
adaptive.slider_min.setValue(512)
ok(win.cfg["optimizer"]["min_size"] == 512, "settings: whole-number dials too")

params = adaptive.params()
ok(abs(params["quality"] - 0.25) < 1e-6 and params["min_size"] == 512,
   "settings: the saved dials are what gets sent")
ok(params["cache_dir"] == status()["default_cache"],
   "settings: an empty cache folder resolves to the ADD-ON's default - the "
   "default belongs to the machine Blender is on, not the one the app is on")
win.cfg["optimizer"]["cache_dir"] = r"D:\my cache"
ok(adaptive.params()["cache_dir"] == r"D:\my cache",
   "settings: a chosen folder wins over the default")
win.cfg["optimizer"]["cache_dir"] = ""

# ⚠ Building the locked-tab preview constructs a real page with a dead bridge
# and throws it away. Every control's initialisation must not write its default
# over what the user chose, from a tab they have not even unlocked.
win._previewing = True
adaptive.save_settings(quality=99.0)
ok(abs(win.cfg["optimizer"]["quality"] - 0.25) < 1e-6,
   "settings: nothing is saved while the lock preview is being built")
win._previewing = False

# ----------------------------------------------------------- the frame step
adaptive.chk_animation.setChecked(False)
ok(not adaptive.slider_step.isEnabled(),
   "animation: the frame step is dead unless animation mode is on")
ok(adaptive._step_label is not None
   and not adaptive._step_label.isEnabled(),
   "animation: and so is its LABEL - Qt does not disable a QFormLayout's label "
   "with its field")
adaptive.chk_animation.setChecked(True)
ok(adaptive.slider_step.isEnabled() and adaptive._step_label.isEnabled(),
   "animation: both come back")
ok(win.cfg["optimizer"]["animation"] is True, "animation: and it is saved")
adaptive.chk_animation.setChecked(False)

# ------------------------------------------------------------------ targets
keys = [adaptive.combo_target.itemData(i)
        for i in range(adaptive.combo_target.count())]
ok(set(keys) == optmod.OBJECT_TARGETS,
   "targets: the adaptive tool offers ONLY object targets - an image set has "
   "nothing to measure against a camera (got %s)" % keys)
all_keys = [fixed.combo_target.itemData(i)
            for i in range(fixed.combo_target.count())]
ok(len(all_keys) == 6 and "IMAGES_HDR" in all_keys,
   "targets: the fixed-size tool offers all six, including HDR-only - the one "
   "way to reach a world HDRI, which belongs to no object")
ok(all(isinstance(adaptive.combo_target.itemText(i), str)
       and adaptive.combo_target.itemText(i) != adaptive.combo_target.itemData(i)
       for i in range(adaptive.combo_target.count())),
   "targets: shown in the user's words, not as enum names")

# ------------------------------------------------------------------ the runs
stub.calls = []
adaptive.preview()
ok(settle(adaptive), "preview: the run comes back")
calls = stub.named("opt_plan")
ok(len(calls) == 1, "preview: asks for a plan")
ok("size" not in calls[0][1][0],
   "preview: and nothing that would change anything")

stub.reply = status(plan={
    "images": [
        {"name": "skin.png", "from": 4096, "to": 1024, "ok": True,
         "reason": None},
        {"name": "packed.png", "from": 2048, "to": 512, "ok": False,
         "reason": "packed into the .blend"},
        {"name": "tiny.png", "from": 128, "to": 256, "ok": True,
         "reason": None},
    ],
    "meshes": {"Rock": 0.2},
    "camera": "Camera", "bytes_saved": 50 * 1024 * 1024,
    "human_saved": "50.0 MB"})
adaptive.preview()
settle(adaptive)
ok(adaptive.table.rowCount() == 3, "preview: every planned texture is listed")
ok(adaptive.table.item(0, 2).text() == "1024 px",
   "preview: a shrinking texture shows its new size")
ok(adaptive.table.item(1, 2).text() == "packed into the .blend",
   "preview: one that cannot be touched shows WHY, in that column")
ok(adaptive.table.item(2, 2).text() == "unchanged",
   "preview: one already smaller than the target says unchanged, never a "
   "bigger number - nothing is ever enlarged")
ok("50.0 MB" in adaptive.status.text(),
   "preview: the saving is reported before anything is done")
ok("Nothing has been changed" in adaptive.status.text(),
   "preview: and it is explicit that this changed nothing")
stub.reply = status()

# ⚠ A run can take minutes with Blender frozen, so it asks first.
asked = {"n": 0}
original_question = QMessageBox.question
QMessageBox.question = lambda *a, **kw: (asked.__setitem__("n", asked["n"] + 1),
                                         QMessageBox.No)[1]
stub.calls = []
adaptive.run()
settle(adaptive)
ok(asked["n"] == 1, "run: it asks before starting")
ok(not stub.named("opt_adaptive"),
   "run: and answering No really does nothing")
QMessageBox.question = lambda *a, **kw: (asked.__setitem__("n", asked["n"] + 1),
                                         QMessageBox.Yes)[1]
stub.reply = status(result={"counts": {"changed": 12, "unchanged": 3,
                                       "failed": 1, "skipped": 2},
                            "failed": [{"name": "odd.psd",
                                        "reason": "not a format we can rewrite"}],
                            "summary": "12 changed, 1 failed."},
                    mesh_result={"counts": {"changed": 4}, "failed": [],
                                 "summary": "4 changed."})
adaptive.run()
settle(adaptive)
ok(len(stub.named("opt_adaptive")) == 1, "run: Yes sends the command")
sent = stub.named("opt_adaptive")[0][1][0]
ok(sent["target"] in optmod.OBJECT_TARGETS and "quality" in sent,
   "run: the whole settings block travels with the command - the add-on keeps "
   "no copy of these, so nothing can drift")
ok("odd.psd" in adaptive.status.text(),
   "run: the first failure is NAMED, so a partial run is not silently 'done'")
ok("Meshes" in adaptive.status.text(),
   "run: mesh work is reported separately from image work")
QMessageBox.question = original_question
stub.reply = status()

stub.calls = []
fixed.run()
settle(fixed)
sent = stub.named("opt_resize")[0][1][0]
ok(sent["size"] in optmod.FIXED_SIZES,
   "fixed: a size from the list is sent (%s)" % sent.get("size"))
ok(sent["target"] == fixed.combo_target.currentData(),
   "fixed: with the target the user picked, not the adaptive tool's")

stub.calls = []
meshes.run()
settle(meshes)
ok(stub.named("opt_decimate")[0][1][0]["meshes"] is True,
   "meshes: the standalone run asks for mesh work explicitly")
meshes.revert()
settle(meshes)
ok(len(stub.named("opt_revert_meshes")) == 1, "meshes: and can undo itself")

stub.calls = []
restore.revert_images()
settle(restore)
ok(stub.named("opt_revert_images")[0][1][0]["target"] == "ALL_IMAGES",
   "restore: 'put all textures back' really means ALL of them, not the "
   "current target - a restore that left some behind would be worse than none")
restore.revert_meshes()
settle(restore)
ok(stub.named("opt_revert_meshes")[0][1][0]["target"] == "ALL_OBJECTS",
   "restore: and the same for meshes")
restore.regenerate()
settle(restore)
ok(len(stub.named("opt_regenerate")) == 1,
   "restore: re-making missing copies is its own command")
ok(stub.named("opt_regenerate")[0][1][0].get("cache_dir"),
   "restore: and it carries the cache folder, which is what re-homes them")

stub.calls = []
adaptive.overlay()
settle(adaptive)
ok(len(stub.named("opt_preview_start")) == 1,
   "overlay: the in-viewport preview is started over the bridge")
ok("Esc" in adaptive.status.text(),
   "overlay: and the user is told how to close it, since it lives in Blender")

# ----------------------------------------------------------- memory report
stub.reply = status(estimate={
    "rows": [{"kind": "Image", "name": "skin.png", "bytes": 64 * 1024 * 1024,
              "share": 0.8, "human": "64.0 MB"},
             {"kind": "Mesh", "name": "Body", "bytes": 16 * 1024 * 1024,
              "share": 0.2, "human": "16.0 MB"}],
    "total_bytes": 80 * 1024 * 1024, "total_human": "80.0 MB",
    "counted": 2, "shown": 2})
stub.calls = []
memory.run()
settle(memory)
ok(len(stub.named("opt_estimate")) == 1, "memory: one command")
ok(memory.table.rowCount() == 2, "memory: both rows land")
ok(memory.table.item(0, 1).text() == "skin.png"
   and memory.table.item(0, 2).text() == "64.0 MB",
   "memory: name and size as the engine reported them")
ok(memory.table.item(0, 3).text() == "80.0%", "memory: with its share")
ok("Estimates, not measurements" in memory.status.text(),
   "memory: labelled as an estimate every time it is shown, not once in a "
   "docstring")

# ⚠ TWO figures, and the difference is the whole point (Marty, 2026-08-04).
stub.reply = status(estimate={
    "rows": [], "total_bytes": 0, "total_human": "0 B", "counted": 0,
    "shown": 0,
    "vram": {"headless_bytes": 2 * 1024 ** 3, "headless_human": "2.0 GB",
             "interactive_bytes": 3 * 1024 ** 3, "interactive_human": "3.0 GB",
             "buffer_bytes": 1, "buffer_human": "99.0 MB",
             "bvh_bytes": 1, "bvh_human": "40.0 MB",
             "ui_bytes": 1, "ui_human": "700.0 MB",
             "resolution": [1920, 1080], "engine": "CYCLES"}})
stub.calls = []
memory.run()
settle(memory)
text = memory.vram.text()
ok(not memory.vram.isHidden(), "vram: the render figures are shown")
ok("2.0 GB" in text and "3.0 GB" in text,
   "vram: both figures appear (%r)" % text[:120])
ok("command line" in text and "inside Blender" in text,
   "vram: labelled by HOW you render, since that is what makes them differ")
ok("700.0 MB" in text,
   "vram: and the interface overhead is named, so the gap is explained rather "
   "than just asserted")
ok("not measurements" in text.lower() or "rough guides" in text.lower(),
   "vram: still labelled an estimate - it depends on driver and engine build")

# An add-on too old to send them leaves no empty box behind.
stub.reply = status(estimate={"rows": [], "total_bytes": 0,
                              "total_human": "0 B", "counted": 0, "shown": 0})
memory.run()
settle(memory)
ok(memory.vram.isHidden(),
   "vram: an add-on that cannot report them hides the box rather than showing "
   "an empty one")
stub.reply = status()
stub.reply = status()

# ------------------------------------------------------------ texture sets
# Marty, 2026-08-04: a resize should leave a named set behind, so one scene can
# be at one resolution and another at another, and you can cycle between them.
stub.calls = []
fixed._jobs = []
fixed.combo_target.setCurrentIndex(fixed.combo_target.findData("SCENE"))
fixed.combo_size.setCurrentIndex(fixed.combo_size.findData(512))
fixed.enqueue()
fixed.combo_size.setCurrentIndex(fixed.combo_size.findData(2048))
fixed.enqueue()
ok(fixed.queue.rowCount() == 2, "queue: two resizes line up")
ok(not fixed.queue.isHidden(),
   "queue: the list only appears once there is something in it")
ok("Run queue (2)" in fixed.btn_run.text(),
   "queue: and the button says what it will do (%r)" % fixed.btn_run.text())

# Every row arrives already named, and the name is the one thing you can edit.
ok(all(fixed._jobs[row].get("name") for row in (0, 1)),
   "queue: a queued job is named before anyone types anything (%s)"
   % [j.get("name") for j in fixed._jobs])
ok("512 px" in fixed.queue.item(0, 0).text(),
   "queue: and the automatic name says what the job does (%r)"
   % fixed.queue.item(0, 0).text())
from PySide6.QtCore import Qt as _Qt  # noqa: E402
ok(bool(fixed.queue.item(0, 0).flags() & _Qt.ItemIsEditable),
   "queue: the NAME cell is editable - Marty: 'i should be able to rename "
   "them from there too by double clicking on the names'")
ok(not (fixed.queue.item(0, 1).flags() & _Qt.ItemIsEditable),
   "queue: the other cells are not - they are what the job IS, fixed when it "
   "was queued")
fixed.queue.item(0, 0).setText("  Preview pass  ")
ok(fixed._jobs[0]["name"] == "Preview pass",
   "queue: typing a name lands on the job, trimmed (%r)"
   % fixed._jobs[0]["name"])
fixed.queue.item(0, 0).setText("   ")
ok(fixed._jobs[0]["name"] and fixed._jobs[0]["name"] != "Preview pass"
   and fixed.queue.item(0, 0).text() == fixed._jobs[0]["name"],
   "queue: clearing it puts the automatic name back rather than leaving a "
   "blank row that means something else at run time (%r)"
   % fixed._jobs[0]["name"])

fixed.run()
settle(fixed)
sent = stub.named("opt_resize")[-1][1][0]
ok(len(sent.get("jobs") or []) == 2,
   "queue: the whole queue travels in ONE command, so it is one progress bar "
   "and one pass over the textures")
ok(all(job.get("name") for job in sent["jobs"]),
   "queue: every job carries its name, because that is what its set gets "
   "called (%s)" % [j.get("name") for j in sent["jobs"]])
ok(sent.get("size"),
   "queue: a plain size rides along too, so an add-on that ignores 'jobs' does "
   "something sane rather than nothing")
ok(fixed.queue.rowCount() == 0,
   "queue: and it empties once it has run, so it cannot fire twice")

# ⚠ THE BUG THIS ALL EXISTS FOR. "Selected objects" is resolved by the add-on
# when the run STARTS, so two jobs queued from two different selections both
# saw the last one. A job queued from a selection captures the objects NOW.
stub.calls = []
fixed._jobs = []
fixed.combo_target.setCurrentIndex(fixed.combo_target.findData("SELECTED"))
fixed.enqueue()
settle(fixed)
ok(stub.named("opt_status"),
   "queue: queuing a selection asks Blender who is selected RIGHT NOW - the "
   "poll is seconds old and the gesture is 'select these, add them'")
ok(fixed._jobs and fixed._jobs[0].get("objects") == ["Hero", "Prop"],
   "queue: and the job carries those objects, so changing the selection "
   "afterwards cannot move it (%s)"
   % (fixed._jobs[0].get("objects") if fixed._jobs else None))
ok("2 selected objects" in fixed.queue.item(0, 1).text(),
   "queue: the row shows what it captured, which is the visible proof it is "
   "pinned (%r)" % fixed.queue.item(0, 1).text())

stub.reply = status(selected=0, selected_objects=[])
fixed.enqueue()
settle(fixed)
ok(len(fixed._jobs) == 1 and "Nothing is selected" in fixed.status.text(),
   "queue: queuing an empty selection is refused with a reason, not queued as "
   "a job that will do nothing (%r)" % fixed.status.text())

# An add-on old enough not to report the names cannot support this, and says so
# rather than queuing a job that would follow the selection.
old_reply = status()
old_reply.pop("selected_objects")
stub.reply = old_reply
fixed.enqueue()
settle(fixed)
ok(len(fixed._jobs) == 1 and "Update the extension" in fixed.status.text(),
   "queue: an add-on that cannot say which objects are selected refuses the "
   "queue with the fix, and leaves everything else working (%r)"
   % fixed.status.text())
stub.reply = status()
fixed._jobs = []
fixed._refresh_queue()

# What the user is told after a queue runs: the sets it made, by name.
fixed.show_resize(status(result={"summary": "12 changed.",
                                 "groups": ["Preview pass", "Hero 2K"]}))
ok("Preview pass" in fixed.status.text() and "Hero 2K" in fixed.status.text(),
   "queue: the report names every set the run created (%r)"
   % fixed.status.text())
fixed.show_resize(status(result={"summary": "3 changed.", "group": "512 px"}))
ok("512 px" in fixed.status.text(),
   "queue: and an add-on from before one-set-per-job still reports its one "
   "set (%r)" % fixed.status.text())

adaptive.broadcast(status(groups=[
    {"name": "Draft 512", "count": 40, "missing": 0, "sizes": [512],
     "active": False, "cache_dir": "C:/cache", "created": 1},
    {"name": "Hero 2K", "count": 12, "missing": 5, "sizes": [2048],
     "active": True, "cache_dir": "C:/cache", "created": 2},
], active_group="Hero 2K"))
ok(fixed.sets.rowCount() == 2, "sets: both sets are listed")
ok(fixed.sets.item(1, 0).text().startswith("●"),
   "sets: the one the scene is on is marked, the other is not (marked=%s)"
   % fixed.sets.item(0, 0).text().startswith("●"))
ok(fixed.sets.item(1, 2).text() == "5 missing",
   "sets: a set whose cached files were cleared says so IN THE LIST, not only "
   "when you try to use it")
ok("cache was cleared" in fixed.status.text(),
   "sets: and the status line warns about it, because a set of names looks "
   "perfectly healthy either way")

adaptive.broadcast(status(groups=[
    {"name": "Draft 512", "count": 40, "missing": 0, "sizes": [512],
     "active": True, "cache_dir": "C:/cache", "created": 1}],
    active_group="Draft 512"))
ok("Draft 512" in fixed.status.text(),
   "sets: with nothing missing it just says which set you are on")
fixed.sets.setCurrentCell(0, 0)
ok(fixed.btn_use.isEnabled() and fixed.btn_rename.isEnabled(),
   "sets: selecting one enables switching and renaming")
stub.calls = []
fixed.use_set()
settle(fixed)
ok(stub.named("opt_group_apply")[0][1][0]["name"] == "Draft 512",
   "sets: switching asks for that set by name")

old_groups = StubBridge()
old_groups.can_groups = False
old_tool = optmod.FixedSizeTool(old_groups, StubWindow())
ok(old_tool.btn_queue.isHidden() and old_tool.sets.isHidden(),
   "sets: on an add-on too old for them the queue and the list are HIDDEN - "
   "'jobs' would be ignored and the user would silently get one resize instead "
   "of the several they lined up")
ok(old_tool.btn_run.isEnabled(),
   "sets: but plain resizing still works - degrade the feature, not the tab")

# -------------------------------------------------------- a stranded original
# ⚠ The one failure this tab must never let someone walk into. A missing COPY
# is a one-click rebuild; a missing ORIGINAL means Restore cannot give the
# texture back at all, so it outranks it in the table and takes over the status
# line rather than being one amber word in a list.
adaptive.broadcast(status(managed=[
    {"name": "skin.png", "size": 1024, "original": "//tex/skin.png",
     "resolved": "C:/gone/skin.png", "current": "C:/cache/abc_1024.png",
     "missing": False, "original_missing": True},
    {"name": "wall.png", "size": 512, "original": "//tex/wall.png",
     "resolved": "C:/tex/wall.png", "current": "C:/cache/def_512.png",
     "missing": True, "original_missing": False},
]))
ok(restore.table.item(0, 2).text() == "ORIGINAL MISSING",
   "stranded: a texture whose ORIGINAL has gone is called out in red, not "
   "shown as merely optimized")
ok("C:/gone/skin.png" in (restore.table.item(0, 2).toolTip() or ""),
   "stranded: and the tooltip names the path it looked in")
ok(restore.table.item(1, 2).text() == "copy missing",
   "stranded: a missing COPY still reads as the lesser problem it is")
ok("cannot be restored" in restore.status.text(),
   "stranded: the status line leads with it, because pressing Restore would "
   "not give this texture back")
adaptive.broadcast(status())
ok("cannot be restored" not in restore.status.text(),
   "stranded: and it clears once the file is back")

# ------------------------------------------------------- clear cache folder
# Marty: "in Restore make sure to add a button to 'clear cache folder'". It is
# the only control in the tab that deletes anything, so what it says before it
# runs matters as much as what it does.
asked = []


def _yes(parent, title, text, *a, **kw):
    asked.append((title, text))
    return QMessageBox.Yes


def _no(parent, title, text, *a, **kw):
    asked.append((title, text))
    return QMessageBox.No


_real_question = optmod.QMessageBox.question
adaptive.broadcast(status(groups=[
    {"name": "Draft 512", "count": 40, "missing": 0, "sizes": [512],
     "active": True, "cache_dir": "C:/cache", "created": 1}],
    active_group="Draft 512"))
try:
    optmod.QMessageBox.question = _no
    stub.calls = []
    restore.clear_cache()
    settle(restore)
    ok(asked and not stub.named("opt_clear_cache"),
       "clear: it asks first, and No really does nothing")
    _text = asked[-1][1]
    ok("go back to full size" in _text,
       "clear: the dialog says the textures are restored first - they point AT "
       "the files being deleted, and that surprises people (%r)" % _text[:90])
    ok("texture set" in _text,
       "clear: and what happens to the texture sets, which survive")
    ok("never touched" in _text or "left alone" in _text,
       "clear: and that the user's own files are not at risk")

    optmod.QMessageBox.question = _yes
    stub.calls = []
    restore.clear_cache()
    settle(restore)
    sent = stub.named("opt_clear_cache")
    ok(len(sent) == 1, "clear: Yes sends the command once")
    ok(sent[0][1][0].get("cache_dir"),
       "clear: with the cache folder resolved, so the add-on clears the one "
       "the user is looking at")
finally:
    optmod.QMessageBox.question = _real_question

restore.show_clear(status(cache={"folder": "C:/cache", "removed": 42,
                                 "bytes": 1, "bytes_human": "1.2 GB",
                                 "kept": 3, "restored": 7, "failed": []}))
_said = restore.status.text()
ok("7 texture(s) put back" in _said and "42" in _said and "1.2 GB" in _said,
   "clear: the report says what it put back, what it removed and what that "
   "freed (%r)" % _said)
ok("3 file(s)" in _said and "not ours" in _said,
   "clear: and that files it did not recognise were left where they are - "
   "silence there would look like a bug")

old_clear = StubBridge()
old_clear.can_clear = False
ok(optmod.RestoreTool(old_clear, StubWindow()).btn_clear.isHidden(),
   "clear: on an add-on too old for the command the button is hidden rather "
   "than there and broken")

# ------------------------------------------------------------------ progress
# ⚠ THE WHOLE POINT OF THIS SECTION: a run must not block the GUI thread, and it
# must be able to say how far along it is. Both were reported broken - the app
# froze on a fixed-size resize, with nothing to look at while it did.
row = page.progress
ok(isinstance(row, optmod.ProgressRow),
   "progress: the PAGE owns the bar, not a tool - a run disables every tool, "
   "and Qt greys a disabled widget's children with it whatever their own "
   "enabled state says")
ok(row.isHidden(), "progress: hidden until something is running")
ok(row.bar.minimum() == 0 and row.bar.maximum() == 0,
   "progress: it starts as a busy sweep, because until Blender has counted the "
   "textures there is genuinely nothing to count")

row.apply(progress(phase="Resizing textures", done=7, total=40,
                   item="skin_diffuse.png"))
ok(row.bar.maximum() == 40 and row.bar.value() == 7,
   "progress: a countable stage makes it a real bar")
ok("7 of 40" in row.label.text(),
   "progress: with the count in words too (got %r)" % row.label.text())
ok("skin_diffuse.png" in row.label.text(),
   "progress: and the item it is on, so a long run is visibly moving even "
   "between whole percentage points")
row.apply(progress(done=99, total=40))
ok(row.bar.value() <= row.bar.maximum(),
   "progress: a count past the total is clamped rather than drawn off the end")
row.apply(progress(item="a" * 90, done=1, total=4))
ok(len(row.label.text()) < 90,
   "progress: a very long texture name is elided, not allowed to stretch the "
   "tab")

fresh = optmod.ProgressRow(stub)
fresh.apply(progress(phase="Measuring the scene", total=0, done=0))
ok(fresh.bar.maximum() == 0,
   "progress: a stage with nothing to count stays a busy sweep")
fresh.apply(progress(total=8, done=8))
fresh.apply(progress(phase="Tidying up", total=0, done=0))
ok(fresh.bar.maximum() == 8,
   "progress: but AFTER a counted stage it holds the bar rather than dropping "
   "back to the sweep, which reads as 'it started over'")

stub.calls = []
fresh.start("Resizing textures")
fresh.poll()
calls = stub.named("opt_progress")
ok(len(calls) == 1, "progress: the bar asks Blender directly")
ok(calls[0][2].get("poll") is True,
   "progress: passing poll=True like every other repeating call here")
ok(fresh.timer.interval() == optmod.PROGRESS_MS,
   "progress: on its own faster interval (%d ms) - it is answered off a plain "
   "dict, so it costs Blender nothing" % optmod.PROGRESS_MS)
ok(optmod.PROGRESS_MS < optmod.POLL_MS,
   "progress: and faster than the status poll, or the bar would crawl")
fresh.stop()
ok(fresh.isHidden() and not fresh.timer.isActive(),
   "progress: stopping takes it down and stops asking")

old_stub = StubBridge()
old_stub.can_progress = False
old_row = optmod.ProgressRow(old_stub)
old_row.start("Resizing textures")
ok(not old_row.timer.isActive(),
   "progress: an add-on too old to be asked is never asked")
ok(not old_row.isHidden() and old_row.bar.maximum() == 0,
   "progress: it just spins instead - losing the COUNT must not cost the tab, "
   "which is why opt_progress has no FEATURE_REQUIREMENTS entry")
old_row.stop()

sick = StubBridge()
sick.progress_error = True
sick_row = optmod.ProgressRow(sick)
sick_row.start("Resizing textures")
for _ in range(optmod.PROGRESS_GIVE_UP):
    sick_row.poll()
ok(not sick_row.timer.isActive(),
   "progress: after a few failed asks it gives up asking")
ok(not sick_row.isHidden(),
   "progress: but keeps spinning - the RUN is still going, and taking the bar "
   "down would say it had finished")
sick_row.stop()

# ------------------------------------------------- the run is off the GUI thread
gate = threading.Event()
slow = StubBridge()
slow_win = StubWindow()
slow_page = optmod.OptimizerPage(slow, slow_win)
slow_tool = optmod.FixedSizeTool(slow, slow_win)
slow_page.add_tool(slow_tool, "Fixed size", group="Optimize")
slow_win.optimizer = slow_page


def _slow_resize(_params):
    gate.wait(10.0)
    return status()


slow.opt_resize = _slow_resize          # instance attribute beats __getattr__
slow_tool.run()
app.processEvents()
ok(slow_tool._busy, "run: the call is still in flight")
ok(not slow_page.progress.isHidden(),
   "run: with the bar up for the whole of it")
ok(slow_win.captures == 1,
   "run: and the window parked - every other tab greys out, because Blender's "
   "main thread is ours until this comes back")
spins = 0
for _ in range(25):
    app.processEvents()
    spins += 1
ok(spins == 25 and slow_tool._busy,
   "run: THE GUI THREAD KEEPS RUNNING while the bridge call blocks - this is "
   "the freeze that was reported, and the reason the call moved off it")
gate.set()
ok(settle(slow_tool), "run: and it comes back when Blender answers")
ok(slow_win.captures == 0, "run: the window is handed back")
ok(slow_page.progress.isHidden(), "run: and the bar comes down")

# A failure must release everything too. A run that died leaving the app greyed
# out with a bar on it would be worse than the freeze it replaced.
broken = StubBridge()
broken.raise_error = True
broken_win = StubWindow()
broken_page = optmod.OptimizerPage(broken, broken_win)
broken_tool = optmod.FixedSizeTool(broken, broken_win)
broken_page.add_tool(broken_tool, "Fixed size", group="Optimize")
broken_win.optimizer = broken_page
broken_tool.run()
ok(settle(broken_tool), "fail: a failing run still finishes")
ok(broken_win.captures == 0,
   "fail: and hands the window back - a run that died greyed out would be "
   "worse than the freeze")
ok(broken_page.progress.isHidden(), "fail: the bar comes down")
ok("bridge down" in broken_tool.status.text(),
   "fail: with the reason on the tool that ran it")

broken_tool._end_run()
broken_tool._end_run()
ok(broken_win.captures == 0,
   "fail: ending twice cannot unbalance the capture counter - it is a counter, "
   "and over-releasing it would grey the app out with nothing running")

# --------------------------------------------------------- capability gate
gated_stub = StubBridge()
gated_stub.reason = ("The Scene Optimizer needs Blender add-on 0.11.0 or "
                     "newer.")
gated = optmod.AdaptiveTool(gated_stub, StubWindow())
ok(not gated.isEnabled(),
   "gate: on an older add-on the tool turns ITSELF off")
ok("0.11.0" in gated.status.text(),
   "gate: with the reason on the control, not a silent failure")
ok(not gated_stub.named("opt_status"),
   "gate: and it never starts a poll it knows will fail")
ok(gated.timer is None,
   "gate: the poll timer stays None — showEvent has nothing to start")

open_stub = StubBridge()


def _raise(_f):
    raise RuntimeError("bridge exploded")


open_stub.feature_reason = _raise
open_tool = optmod.FixedSizeTool(open_stub, StubWindow())
ok(open_tool.isEnabled(),
   "gate: a bridge that cannot answer fails OPEN - unknown is not 'missing', "
   "or a dead bridge would look like an out-of-date add-on")

# --------------------------------------------------------------- busy greying
page.set_capture_busy(True)
ok(not adaptive.isEnabled() and not restore.isEnabled(),
   "busy: every tool greys out while Blender is capturing - bridge commands "
   "queue on Blender's main thread")
page.set_capture_busy(False)
ok(adaptive.isEnabled(), "busy: and comes back")


class BusyWindow(StubWindow):
    capturing = True


busy_tool = optmod.FixedSizeTool(stub, BusyWindow())
stub.calls = []
busy_tool.run()
ok(not stub.named("opt_resize"),
   "busy: a click while Blender is busy sends nothing at all")

print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
for label in FAIL:
    print("  FAILED: " + label)
sys.exit(1 if FAIL else 0)
