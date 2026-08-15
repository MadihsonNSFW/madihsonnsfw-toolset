# Render Queue offscreen: pending() semantics, session round-trip, resume
# render-args math (-s from the banked frame), hold_for_resume / shutdown ->
# resumable PAUSED job on disk, Start<->Resume relabel, responsive cards.
# All state is redirected to a temp dir — never touches the real
# render_queue\ data (settings.json / session.json) or spawns Blender.
import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

sys.path.insert(0, os.path.join(_ROOT, "app"))

from PySide6.QtWidgets import QApplication  # noqa: E402

PASS = []
FAIL = []


def ok(cond, label):
    (PASS if cond else FAIL).append(label)
    print(("ok   " if cond else "FAIL ") + label, flush=True)


# --- redirect ALL queue data to a temp dir BEFORE anything loads it --------
tmp = tempfile.mkdtemp(prefix="madi_rq_")


def _tmp_dir():
    return tmp


from render_deck import util  # noqa: E402
util.data_dir = _tmp_dir
from render_deck import joblog, session, settings  # noqa: E402
for mod in (joblog, session, settings):
    if hasattr(mod, "data_dir"):
        mod.data_dir = _tmp_dir

from render_deck.job import JobStatus, RenderJob  # noqa: E402
from render_deck.render_controller import RenderController  # noqa: E402
from render_deck import queue_tool  # noqa: E402

app = QApplication.instance() or QApplication([])

# a real file on disk so probe_fresh() can hold
blend = os.path.join(tmp, "scene.blend")
open(blend, "wb").close()
MT = os.path.getmtime(blend)

# ---------------------------------------------------------------- pending --
done = RenderJob(filepath="a.blend", status=JobStatus.DONE)
queued = RenderJob(filepath="b.blend", status=JobStatus.QUEUED)
failed = RenderJob(filepath="c.blend", status=JobStatus.FAILED)
paused = RenderJob(filepath="d.blend", status=JobStatus.PAUSED, resume_frame=7)
jobs = [done, queued, failed, paused]

todo = RenderController.pending(jobs)
ok(todo == [queued, failed, paused],
   "pending(): DONE skipped, FAILED retried, order kept")
ok(RenderController.pending([done]) == [done],
   "pending(): everything Done -> run the lot again")
ok(RenderController.pending(jobs, first=paused) == [paused, queued, failed],
   "pending(first=paused): resumed job moves to the front")
ok(RenderController.pending([done, queued], first=done)[0] is done,
   "pending(first=Done): an explicitly resumed Done job still runs")

# ------------------------------------------------------- session round-trip
j = RenderJob(filepath=blend, start_frame=None, end_frame=None,
              engine="CYCLES", camera="Cam")
j.status = JobStatus.PAUSED
j.resume_frame = 42
j.frames_total = 100
j.frames_done = 41
j.export_dir = r"X:\out"
j.time_limit = 30.0
j.samples = 128
j.probed_start = 1          # probe range: deliberately NOT persisted
j.probed_end = 100
j.probe_mtime = MT
session.save_jobs([j, RenderJob(filepath="", status=JobStatus.QUEUED)])
loaded = session.load_jobs()
ok(len(loaded) == 1, "empty-path row dropped on load")
lj = loaded[0]
ok(lj.status == JobStatus.PAUSED and lj.resume_frame == 42
   and lj.frames_done == 41,
   "paused job comes back with its resume frame")
ok(lj.engine == "CYCLES" and lj.camera == "Cam"
   and lj.export_dir == r"X:\out" and lj.time_limit == 30.0
   and lj.samples == 128,
   "per-job overrides + probe cache fields survive")
ok(lj.probed_start is None and lj.probed_end is None,
   "probed range NOT persisted (re-read from the file each run)")
ok(lj.start_frame is None,
   "user's 'use the .blend's range' choice survives as None")

j2 = RenderJob(filepath=blend, status=JobStatus.RENDERING)
j2.probe_mtime = MT + 999            # stale cache
j2.export_dir = r"X:\stale"
session.save_jobs([j2])
lj2 = session.load_jobs()[0]
ok(lj2.status == JobStatus.QUEUED, "mid-flight RENDERING restarts as QUEUED")
ok(lj2.export_dir is None and lj2.probe_mtime is None,
   "stale probe cache (mtime mismatch) dropped on load")

# ------------------------------------------------------- resume args math --
st = settings.Settings()
ctl = RenderController(st)
captured = []
ctl._spawn = lambda args: captured.append(list(args))

rj = RenderJob(filepath=blend, start_frame=1, end_frame=20)
rj.probe_mtime = MT                 # fresh cache -> no probe launch
rj.status = JobStatus.PAUSED
rj.resume_frame = 13
ctl.start([rj], first=rj)
ok(len(captured) == 1, "render spawned once (no probe needed)")
args = captured[0] if captured else []
ok("-b" in args and blend in args, "args: -b <file>")
si = args.index("-s") if "-s" in args else -1
ok(si >= 0 and args[si + 1] == "13",
   "resume math: -s is the BANKED frame 13, not the range start")
ok("-e" in args and args[args.index("-e") + 1] == "20", "-e is the range end")
ok(args[-1] == "-a", "always animation mode (-a) last")
ok(rj.resume_frame is None, "resume point consumed on start")
ok(rj.frames_total == 20, "frames_total from the effective range")
ok(rj.status == JobStatus.RENDERING, "job marked RENDERING")

# ------------------------------------------------------- hold_for_resume ---
ctl2 = RenderController(st)
hj = RenderJob(filepath=blend, start_frame=1, end_frame=20)
hj.frames_done = 5
ctl2._current = hj
ctl2._phase = "render"
ctl2.hold_for_resume()
ok(hj.status == JobStatus.PAUSED and hj.resume_frame == 5,
   "hold_for_resume banks the current frame (first + done - 1)")
before = hj.resume_frame
ctl2._paused = True
ctl2.hold_for_resume()
ok(hj.resume_frame == before, "no-op when already paused")
ctl3 = RenderController(st)
ctl3.hold_for_resume()
ok(True, "idle hold_for_resume is a clean no-op")

# ------------------------------------------- the embedded tool + shutdown --
tool = queue_tool.RenderQueueTool(None)
ok(tool.minimumSizeHint().width() <= 760,
   "tool's own minimum width stays small (got %d)"
   % tool.minimumSizeHint().width())

# Start <-> Resume relabel follows a paused job in the list
tool.jobs = [RenderJob(filepath=blend, status=JobStatus.PAUSED,
                       resume_frame=9)]
tool._sync_start_action()
ok(tool.act_start.text() == "Resume", "a paused job relabels Start -> Resume")
tool.jobs = [RenderJob(filepath=blend)]
tool._sync_start_action()
ok(tool.act_start.text() == "Start", "no paused job -> back to Start")

# responsive: cards shed as the pane narrows (thresholds 940 / 760).
# resizeEvent only fires on a SHOWN widget — offscreen show is harmless.
ft = tool.frame_timers
tool.show()
tool.resize(1200, 600)
app.processEvents()
ok(ft._ram.isVisibleTo(ft) and ft._last.isVisibleTo(ft),
   "wide: RAM/VRAM and LAST/LIMIT cards shown")
tool.resize(700, 600)
app.processEvents()
ok(not ft._ram.isVisibleTo(ft) and not ft._vram.isVisibleTo(ft),
   "narrow (<760): RAM/VRAM cards hidden")
ok(not ft._last.isVisibleTo(ft) and not ft._limit.isVisibleTo(ft),
   "narrow (<760): LAST/LIMIT cards hidden too")
ok(ft._current.isVisibleTo(ft), "CURRENT FRAME always stays")
tool.resize(1200, 600)
app.processEvents()
ok(ft._ram.isVisibleTo(ft) and ft._last.isVisibleTo(ft),
   "wide again: cards reshow")
tool.hide()

# closing the app mid-render freezes the job to a resumable PAUSED on disk
sj = RenderJob(filepath=blend, start_frame=1, end_frame=20)
sj.frames_done = 3
tool.jobs = [sj]
tool.controller._current = sj
tool.controller._phase = "render"
tool.shutdown()
back = session.load_jobs()
ok(len(back) == 1 and back[0].status == JobStatus.PAUSED
   and back[0].resume_frame == 3,
   "shutdown(): mid-render job lands on disk as PAUSED at its frame")

# ------------------------------------------------ background playblast -----
# The Studio Library 🎬 "run in background" path: a snapshot .blend rendered
# headlessly as Workbench H.264, then renamed to the name the user typed.
pb_dir = os.path.join(tmp, "_playblasts")
os.makedirs(pb_dir, exist_ok=True)
snap = os.path.join(tmp, "madi_playblast_snapshot.blend")
open(snap, "wb").close()
target = os.path.join(pb_dir, "hero take.mp4")

plain = RenderJob(filepath=blend)
ok(not plain.is_playblast, "a normal job is not a playblast")
pj = RenderJob(filepath=snap, start_frame=1, end_frame=5)
pj.playblast_out = target
pj.playblast_pct = 75
pj.playblast_label = "hero take"
pj.temp_blend = True
ok(pj.is_playblast, "playblast_out is what makes a job a playblast")
ok(pj.name == "hero take",
   "File column shows the user's name, not the temp snapshot's")

st2 = settings.Settings()
st2.output_dir = os.path.join(tmp, "elsewhere")   # must NOT capture a playblast
st2.default_engine = "CYCLES"                     # must NOT override Workbench
st2.extra_args = "--cycles-device OPTIX"          # must NOT leak in
ctl3 = RenderController(st2)
pcap = []
ctl3._spawn = lambda args: pcap.append(list(args))
ctl3.start([pj, plain], first=pj, only=True)
ok(len(pcap) == 1, "playblast: one spawn, no probe (range already known)")
pargs = pcap[0] if pcap else []
ok("-E" in pargs and pargs[pargs.index("-E") + 1] == "BLENDER_WORKBENCH",
   "playblast renders Workbench (solid shading), not the queue's engine")
oi = pargs.index("-o") if "-o" in pargs else -1
ok(oi >= 0 and pargs[oi + 1] == os.path.join(pb_dir, "hero take_"),
   "-o is the dialog's own folder/name, not settings.output_dir")
ok("--cycles-device" not in pargs,
   "queue-wide extra_args are not inherited by a playblast")
expr = pargs[pargs.index("--python-expr") + 1] if "--python-expr" in pargs else ""
ok("'FFMPEG'" in expr and "'H264'" in expr, "playblast expr sets H.264 FFMPEG")
ok("resolution_percentage',75" in expr.replace(" ", ""),
   "playblast expr carries the dialog's resolution %")
ok("-s" in pargs and pargs[pargs.index("-s") + 1] == "1"
   and "-e" in pargs and pargs[pargs.index("-e") + 1] == "5",
   "playblast renders exactly the requested range")
ok(pargs[-1] == "-a", "playblast is an animation render")
ok(ctl3._queue == [], "only=True: the rest of the queue is left alone")

pj.frames_total, pj.frames_done = 5, 2
st2.auto_resume = True
ok(not ctl3._should_resume(pj),
   "a crashed playblast never auto-resumes (would write a 2nd partial mp4)")
ok(RenderController(st2)._should_resume(
    RenderJob(filepath=blend, frames_total=5, frames_done=2)),
   "…while a normal job still does")

# session persistence must skip playblasts (their .blend is deleted)
tool2 = queue_tool.RenderQueueTool()
tool2.jobs = [RenderJob(filepath=blend), pj]
tool2._save_session()
ok([j.filepath for j in session.load_jobs()] == [blend],
   "session.json keeps real jobs and drops the playblast")

# terminal handling: rename the frame-decorated file, bin the snapshot, drop row
tool3 = queue_tool.RenderQueueTool()
tool3.jobs.clear()            # __init__ restores the saved session; start clean
tool3.table.setRowCount(0)    # rows and jobs are index-locked throughout
got = []
tool3.playblastFinished.connect(got.append)
done_job = RenderJob(filepath=snap, start_frame=1, end_frame=5)
done_job.playblast_out = target
done_job.playblast_label = "hero take"
done_job.temp_blend = True
done_job.status = JobStatus.DONE
tool3.jobs = [done_job]
tool3._append_row(done_job)
decorated = os.path.join(pb_dir, "hero take_0001-0005.mp4")
with open(decorated, "wb") as fh:
    fh.write(b"fake mp4")
# Blender's "Save Versions" pref leaves a .blend1 beside the snapshot; the
# cleanup has to take it too, or %TEMP% collects half-megabyte orphans.
with open(snap + "1", "wb") as fh:
    fh.write(b"stale backup")
tool3._refresh_job(done_job)
ok(os.path.isfile(target) and not os.path.isfile(decorated),
   "Blender's '<name>_0001-0005.mp4' is renamed to exactly '<name>.mp4'")
ok(not os.path.isfile(snap), "the temp snapshot .blend is deleted")
ok(not os.path.isfile(snap + "1"),
   "…and the .blend1 backup Blender's Save Versions leaves beside it "
   "(found orphaned in %TEMP% 2026-08-02)")
ok(tool3.jobs == [] and tool3.table.rowCount() == 0,
   "the playblast row is removed once it finishes")
ok(len(got) == 1 and got[0]["ok"] and got[0]["path"] == target,
   "playblastFinished carries ok + the final path")
tool3._on_queue_finished()    # the controller fires this right after
ok("hero take" in tool3.status_label.text()
   and "done" in tool3.status_label.text(),
   "run summary reports the playblast, not '0/0 rendered'")

# failure path: no mp4 produced
open(snap, "wb").close()
tool4 = queue_tool.RenderQueueTool()
tool4.jobs.clear()
tool4.table.setRowCount(0)
bad = []
tool4.playblastFinished.connect(bad.append)
fail_job = RenderJob(filepath=snap)
fail_job.playblast_out = os.path.join(pb_dir, "never made.mp4")
fail_job.playblast_label = "never made"
fail_job.temp_blend = True
fail_job.status = JobStatus.FAILED
tool4.jobs = [fail_job]
tool4._append_row(fail_job)
tool4._refresh_job(fail_job)
ok(len(bad) == 1 and not bad[0]["ok"] and bad[0]["error"],
   "a failed playblast reports ok=False with a reason")
ok(not os.path.isfile(snap) and tool4.jobs == [],
   "a failed playblast still cleans up its snapshot and row")

# closing the app mid-playblast must not leak the snapshot into %TEMP%
open(snap, "wb").close()
tool5 = queue_tool.RenderQueueTool()
live = RenderJob(filepath=snap)
live.playblast_out = target
live.temp_blend = True
tool5.jobs = [live]
tool5.shutdown()
ok(not os.path.isfile(snap),
   "shutdown() bins the snapshot of a playblast that never finished")

# guards before anything is spawned
tool6 = queue_tool.RenderQueueTool()
tool6.settings.blender_path = ""
started, msg = tool6.queue_playblast(blend, target, 1, 5)
ok(not started and "blender" in msg.lower(),
   "queue_playblast refuses with no blender.exe configured")
tool6.settings.blender_path = blend          # any existing file passes the check
tool6.controller._proc = object()            # pretend a render is in progress
started2, msg2 = tool6.queue_playblast(blend, target, 1, 5)
ok(not started2 and "rendering" in msg2.lower(),
   "queue_playblast refuses while the queue is already rendering")
tool6.controller._proc = None

# ------------------------------------------------- collections to leave out
# Marty, 2026-08-04: "add the ability to chose collections that should be
# disabled for each scene".
from render_deck import util as rd_util  # noqa: E402
from render_deck import session as rd_session  # noqa: E402
from render_deck.job import RenderJob as _RJ  # noqa: E402

ok(rd_util.disable_collections_expr([]) is None,
   "collections: nothing chosen means no extra expression at all")
expr = rd_util.disable_collections_expr(["Props", "Debug"])
ok("hide_render" in expr, "collections: hidden with hide_render")
# NOT `exclude`: unlinking a collection from the view layer drops its objects
# out of the depsgraph, so a boolean operand or shrinkwrap target inside it
# BREAKS the geometry that uses it instead of merely not being drawn.
ok("exclude" not in expr,
   "collections: and NOT with LayerCollection.exclude, which would break "
   "anything depending on those objects")
ok("Props" in expr and "Debug" in expr, "collections: both names travel")
ok("bpy.data.collections" in expr,
   "collections: applied to the collection itself, so it holds for every view "
   "layer rather than just the active one")

_job = _RJ(filepath="x.blend")
ok(_job.disabled_collections == [],
   "collections: a new job leaves nothing out by default")
_job.disabled_collections = ["Props", "Gone"]
_job.apply_probe({"collections": ["Props", "Hero"]})
ok(_job.probed_collections == ["Props", "Hero"],
   "collections: the probe reports what the file actually has")
ok(_job.disabled_collections == ["Props"],
   "collections: a name the file no longer has is DROPPED - otherwise the "
   "render would quietly not hide it while the queue still claimed it had "
   "(%s)" % _job.disabled_collections)
ok("disabled_collections" in rd_session._FIELDS,
   "collections: persisted, so a restart does not send the render off with "
   "everything switched back on")

# ------------------------------------------- Save & Queue (app 1.7.0, job 2)
# Marty, 2026-08-10: "Save and Add active blend file to render queue". The
# queue itself never learns what a bridge is — the Toolset hands it a callable.
_sq_blend = os.path.join(tmp, "shot_open.blend")
open(_sq_blend, "wb").close()
_calls = []


def _fake_save():
    _calls.append(1)
    return _sq_blend


_no_hook = queue_tool.RenderQueueTool()
_no_hook.jobs.clear()
_texts = [a.text().replace("\n", " ") for a in _no_hook.tb.actions()]
ok(not any("Save &" in t for t in _texts),
   "save&queue: ⚠ NO button without the hook — that is what keeps the "
   "STANDALONE render manager (no bridge at all) building from this same file")

_sq = queue_tool.RenderQueueTool(save_open_blend=_fake_save)
_sq.jobs.clear()
_sq.table.setRowCount(0)
_texts = [a.text().replace("\n", " ") for a in _sq.tb.actions()]
ok(any("Save &" in t for t in _texts),
   "save&queue: the button appears once a host provides the hook")

_sq._save_and_queue()
ok(len(_calls) == 1 and len(_sq.jobs) == 1
   and _sq.jobs[0].filepath == _sq_blend,
   "save&queue: saves through the hook, then queues what it handed back")

# ⚠ THE ITERATION LOOP: pressing it again on the same file must not grow the
# queue. Saving the shot you are working on and re-queueing is the NORMAL use,
# and a job holds a path, so the existing row already renders the new bytes.
_sq._save_and_queue()
ok(len(_sq.jobs) == 1,
   "save&queue: a second press does NOT queue the same file twice (%d rows)"
   % len(_sq.jobs))
ok("already queued" in _sq.status_label.text(),
   "save&queue: ...and says so, rather than looking like it did nothing")

# a refusal (unsaved file, bridge down, old add-on) must queue nothing at all
_sq2 = queue_tool.RenderQueueTool(save_open_blend=lambda: None)
_sq2.jobs.clear()
_sq2.table.setRowCount(0)
_sq2._save_and_queue()
ok(len(_sq2.jobs) == 0,
   "save&queue: the host answering None queues nothing — the app has already "
   "said why, and a job with no path would be worse than no job")

# ---- a row is always selected (Marty, 2026-08-15) --------------------------
# "make it so it always selects first from list if no job is selected".
# Start/Resume, the frame-range fields and Remove all read `_selected_job()`,
# so an empty selection quietly disables half the toolbar — and an empty
# selection looks exactly like a full one, so nothing on screen explains it.
_sel = queue_tool.RenderQueueTool()
_sel.jobs.clear()
_sel.table.setRowCount(0)
ok(_sel._selected_job() is None,
   "queue: an EMPTY queue selects nothing - there is nothing to select")

_a = RenderJob(filepath=os.path.join(tmp, "a.blend"), start_frame=1, end_frame=5)
_b = RenderJob(filepath=os.path.join(tmp, "b.blend"), start_frame=1, end_frame=5)
_sel.jobs = [_a]
_sel._append_row(_a)
ok(_sel._selected_job() is _a,
   "queue: the FIRST job added is selected straight away")

_sel.jobs.append(_b)
_sel._append_row(_b)
ok(_sel._selected_job() is _a,
   "queue: adding a second job does NOT steal the selection - it is a "
   "fallback, not a policy")

# ⚠ The case that matters: deleting the selected row must not leave the
# toolbar pointing at nothing.
_sel.table.clearSelection()
_sel._ensure_selection()
ok(_sel._selected_job() is _a,
   "queue: losing the selection falls back to the first row")

print("\n%d passed, %d failed" % (len(PASS), len(FAIL)), flush=True)
sys.exit(1 if FAIL else 0)
