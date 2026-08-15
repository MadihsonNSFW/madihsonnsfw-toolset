# Quadify, app side: the tool in the Optimization tab, offscreen against a stub
# bridge. Nothing here runs the engine - `quadify_test.py` does that inside
# Blender. This suite is about the CONTROL: what it sends, what it draws, and
# the three rules that came out of the 52-minute run Marty reported.
#
#   app\.venv\Scripts\python.exe tests\app_quadify_test.py
#
# ⚠ THE THREE RULES, and each has a check below that fails if it is undone:
#   1. THE REPORT SAYS WHAT WAS MEASURED, never what was asked for. Every
#      number in the right-hand panel comes out of the add-on's reply.
#   2. THE LABEL SHOWS THE EVALUATED TRIANGLE COUNT. The datablock count read
#      2 424 for a mesh that sent 266 469 triangles to the engine - 110x - and
#      that label is what someone decides to press the button on.
#   3. THE RUN MUST NOT GREY THE APP. `_call`/`begin_capture` is right for work
#      that owns Blender's main thread and wrong now that nothing does.
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

sys.path.insert(0, os.path.join(_ROOT, "app"))

from PySide6.QtWidgets import QApplication  # noqa: E402

import bridge as bridgemod  # noqa: E402
import config as configmod  # noqa: E402
import optimizer as optmod  # noqa: E402
import quadify as quadmod  # noqa: E402

PASS = []
FAIL = []


def ok(cond, label):
    (PASS if cond else FAIL).append(label)
    print(("ok   " if cond else "FAIL ") + label, flush=True)


def quad_status(**over):
    """A plausible `quad_status` reply."""
    base = {"engine_ready": True, "engine_missing": [], "selected": ["Suzanne"],
            "object": "Suzanne", "verts": 507, "faces": 500, "modifiers": 1,
            "running": False, "eval_tris": 3936, "eval_verts": 1970,
            "big": False}
    base.update(over)
    return base


def quad_result(**over):
    """A plausible finished run."""
    base = {"ok": True, "object": "Suzanne_quads", "source": "Suzanne",
            "verts_in": 1970, "faces_in": 3936, "verts": 5782, "faces": 5780,
            "quads": 5780, "tris": 0, "ngons": 0, "quad_pct": 100.0,
            "seconds": 6.92, "smoothed": True, "exit": -2147483645,
            "work": r"C:\Temp\madi_quadify_ab12"}
    base.update(over)
    return base


class StubBridge:
    def __init__(self):
        self.calls = []
        self.status_reply = quad_status()
        self.progress_reply = {"active": False, "phase": "", "done": 0,
                               "total": 0, "item": "", "serial": 1,
                               "started": 0.0, "elapsed": 0.0}
        self.result_reply = quad_result()
        self.start_reply = {"ok": True, "started": True, "object": "Suzanne",
                            "verts_in": 1970, "faces_in": 3936}
        self.reason = None
        self.raise_on = set()

    # the shape every _OptimizerTool expects of a bridge
    def feature_reason(self, feature):
        return self.reason

    def supports(self, command):
        return True

    def _record(self, name, *a, **kw):
        self.calls.append((name, a, kw))
        if name in self.raise_on:
            raise bridgemod.BridgeError("bridge down")

    def quad_status(self, poll=False, deep=False):
        self._record("quad_status", poll=poll, deep=deep)
        return self.status_reply

    def quad_progress(self, poll=False):
        self._record("quad_progress", poll=poll)
        return self.progress_reply

    def quad_retopologize(self, params):
        self._record("quad_retopologize", params)
        return self.start_reply

    def quad_result(self, poll=False):
        self._record("quad_result", poll=poll)
        return self.result_reply

    def quad_cancel(self):
        self._record("quad_cancel")
        return {"ok": True, "cancelled": True}

    def quad_select(self, name):
        self._record("quad_select", name)
        return {"ok": True, "object": name}

    def opt_status(self, *a, **kw):
        self._record("opt_status", *a, **kw)
        return {}

    def named(self, name):
        return [c for c in self.calls if c[0] == name]


class StubWindow:
    capturing = False
    _previewing = False

    def __init__(self):
        self.cfg = {}
        self.captures = 0
        self.capture_calls = []
        # ⚠ `_OptimizerTool.page()` finds the tab through `window.optimizer` -
        # that is the handle the one shared progress bar lives behind.
        self.optimizer = None

    def bridge_free_for_tools(self):
        return True

    def begin_capture(self, label, verb="capturing"):
        self.captures += 1
        self.capture_calls.append((label, verb))

    def end_capture(self):
        self.captures = max(0, self.captures - 1)


_writes = []
configmod.save = lambda cfg: _writes.append(cfg)

app = QApplication.instance() or QApplication([])
stub = StubBridge()
win = StubWindow()

page = optmod.OptimizerPage(stub, win)
win.optimizer = page
tool = quadmod.QuadifyTool(stub, win)
page.add_tool(tool, quadmod.TITLE, group="Optimize")


def settle(timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        app.processEvents()
        if not getattr(tool, "_busy", False) and tool._starter is None:
            break
        time.sleep(0.005)
    app.processEvents()


# --------------------------------------------------- inheriting honestly
# ⚠ It reuses `_OptimizerTool` for the off-thread plumbing that a freeze paid
# for. Two attributes are what keep that reuse from being wrong.
ok(isinstance(tool, optmod._OptimizerTool),
   "tool: it is an _OptimizerTool, so it inherits the off-thread run and the "
   "tab's single progress bar rather than growing a second copy")
ok(quadmod.QuadifyTool.CONFIG_KEY == "quadify",
   "⚠ tool: CONFIG_KEY is its own - these dials are NOT optimizer settings")
ok(quadmod.QuadifyTool.BROADCASTS is False,
   "⚠ tool: BROADCASTS is False - a retopo result is not an optimizer status, "
   "and fanning it out would make five other tools repaint from a dict with "
   "none of their keys in it")
tool.save_settings(density=150)
ok(win.cfg.get("quadify", {}).get("density") == 150,
   "config: a dial is saved under the tool's OWN key")
ok("density" not in win.cfg.get("optimizer", {}),
   "⚠ config: and never lands in the optimizer's group - that is the whole "
   "reason CONFIG_KEY exists")
ok(tool.settings().get("density") == 150,
   "config: and it reads back from there")
ok(hasattr(tool, "apply_status"),
   "tool: it still answers the tab's fan-out hook, or the other tools break")

# --------------------------------------------------- what the label says
tool.apply_quad_status(quad_status())
ok("3,936" in tool.target.text(),
   "⚠ label: it shows the EVALUATED triangle count (3,936), not the 500 faces "
   "in the file - the one time those differed it was by 110x")
ok("500" not in tool.target.text(),
   "label: and the datablock count is not shown as if it were the job size")
ok(tool.run_button.isEnabled(), "label: with a mesh selected, Run is live")

tool.apply_quad_status(quad_status(eval_tris=None))
ok("faces in the file" in tool.target.text(),
   "label: with no evaluated count it says so IN WORDS rather than passing the "
   "shallow number off as the real one")

# The size warning is in units of TIME, because 'large' tells nobody whether
# to press the button.
tool.apply_quad_status(quad_status(eval_tris=266469, big=True))
warning = tool.size_warning.text()
ok(tool.size_warning.isVisible() or warning,
   "size: a mesh past the engine's practical ceiling warns")
ok("minutes" in warning,
   "⚠ size: and the warning is in MINUTES - Marty's first real mesh took 52 "
   "of them behind a label that said 2,424 faces")
ok("266,469" in warning, "size: naming the actual count")
ok("Detect" in warning or "sharp" in warning.lower(),
   "size: and it names sharp detection, which is what makes an organic mesh "
   "crawl (49,694 feature edges on that run)")
tool.apply_quad_status(quad_status())
ok(not tool.size_warning.isVisible(), "size: an ordinary mesh gets no warning")

# --------------------------------------------------- refusing to be drawn
tool.apply_quad_status(quad_status(engine_ready=False,
                                   engine_missing=["quadwild"]))
ok(not tool.run_button.isEnabled() and "quadwild" in tool.target.text(),
   "engine: a missing engine disables Run and NAMES what is missing")
tool.apply_quad_status(quad_status(object="", selected=[]))
ok(not tool.run_button.isEnabled() and "elect" in tool.target.text(),
   "engine: nothing selected disables Run and says what to do")

stub.reason = "needs Blender add-on 0.44.0 or newer"
tool.refresh()
ok(not tool.run_button.isEnabled() and stub.reason in tool.target.text(),
   "⚠ gate: an add-on too old greys the tool WITH ITS REASON - the "
   "compatibility contract working, not a bug")
stub.reason = None

# --------------------------------------------------- the deep read is rare
before = len(stub.named("quad_status"))
tool.refresh()
after = stub.named("quad_status")
ok(len(after) == before + 1, "status: refresh asks once")
ok(after[-1][2].get("deep") is True,
   "⚠ status: refresh asks for the DEEP count - it costs one evaluated "
   "to_mesh() and it is the only honest number")
ok(after[-1][2].get("poll") is True,
   "status: as a poll, so a dead bridge fails instantly instead of stalling")

# --------------------------------------------------- what a run sends
tool.apply_quad_status(quad_status())
tool.density.setValue(150)
tool.use_sharp.setChecked(False)
tool.smoothing.setChecked(True)
tool.preprocess.setChecked(True)
tool.replace.setChecked(False)
for axis, box in tool.symmetry.items():
    box.setChecked(axis == "x")
tool.tuning.setChecked(False)
win.captures = 0
tool.run()
settle()
_bar_command = getattr(page.progress, "_command", None)
sent = stub.named("quad_retopologize")
ok(len(sent) == 1, "run: it starts exactly one retopology")
params = sent[-1][1][0]
ok(params["object"] == "Suzanne",
   "run: on the object the status reported, not on whatever is typed anywhere")
ok(abs(params["density"] - 1.5) < 1e-9,
   "run: density is sent as a factor, not as the percent the slider shows")
ok(params["use_sharp"] is False, "run: sharp detection off is sent as off")
ok(params["symmetry"] == "x", "run: symmetry is sent as the axis letters")
ok("settings" not in params,
   "run: with Fine tuning unticked, no advanced block is sent at all - the "
   "engine keeps its own defaults")

# ⚠ RULE 3. This is the check that fails if anyone re-wraps the run in _call.
ok(win.captures == 0,
   "⚠ run: the app is NOT greyed for a retopo. `begin_capture` parks every "
   "other tab's poll, which is right when a run owns Blender's main thread "
   "and wrong now that the engine has its own")
ok(not win.capture_calls,
   "run: and no capture was ever begun, not even one that was ended again")

# Fine tuning sends the EIGHT knobs the engine reads, and no others.
tool.tuning.setChecked(True)
tool.run()
settle()
params = stub.named("quad_retopologize")[-1][1][0]
ok("settings" in params, "tuning: ticking Fine tuning sends the advanced block")
LIVE = {"isometry_bias", "ngon_regularity_weight", "singularity_align_weight",
        "align_singularities", "repeat_quads", "repeat_ngons", "repeat_align",
        "chart_cluster_size"}
ok(set(params["settings"]) == LIVE,
   "⚠ tuning: exactly the EIGHT parameters this build of the engine reads. "
   "Eleven of QRemeshify's nineteen are inert, and a control that does "
   "nothing is the thing this project criticised it for (got %s)"
   % sorted(params["settings"]))
tool.tuning.setChecked(False)

# --------------------------------------------------- the report
tool.show_result(quad_result())
ok(tool.rows["faces"].text() == "5,780",
   "⚠ report: every number comes from the REPLY - counted off the mesh that "
   "actually arrived, never echoed back off a setting")
ok(tool.rows["quad_pct"].text() == "100.0%", "report: the quad percentage")
ok(tool.rows["seconds"].text().startswith("6.9"), "report: and how long it took")
ok("All quads" in tool.report_note.text(),
   "report: a clean result says so rather than staying blank")
ok(tool.select_button.isEnabled(),
   "report: the result can be selected in Blender from here")

tool.show_result(quad_result(quads=5000, tris=40, ngons=12, faces=5052,
                             quad_pct=98.97, smoothed=False))
note = tool.report_note.text()
ok("40 triangles" in note and "12 n-gons" in note,
   "⚠ report: an imperfect result NAMES what is wrong with it - that is the "
   "one thing someone judging a retopo needs told")
ok("smoothing did not run" in note,
   "report: including a smoothing pass that was asked for and did not happen")

tool.show_result({"ok": False, "error": "the engine produced no quad mesh"})
ok("did not finish" in tool.report_note.text(),
   "report: a failed run says so instead of leaving the last run's numbers up")

# --------------------------------------------------- cancel
stub.calls.clear()
tool.cancel()
ok(stub.named("quad_cancel"),
   "⚠ cancel: the button really sends quad_cancel - the plan promised progress "
   "AND cancel, and only progress was built the first time")

# --------------------------------------------------- polling for the end
tool._poll.stop()
stub.progress_reply = {"active": True, "phase": "trace", "done": 3, "total": 5,
                       "item": "14 partitions left", "serial": 2,
                       "started": 1.0, "elapsed": 30.0}
stub.calls.clear()
tool._check()
ok(not stub.named("quad_result"),
   "poll: while the run is active it does not ask for a result yet")
stub.progress_reply = dict(stub.progress_reply, active=False)
tool._check()
ok(stub.named("quad_result"),
   "poll: and asks for the report the moment the run goes inactive")
ok(all(call[2].get("poll") for call in stub.named("quad_progress")),
   "poll: the progress watch is marked as a poll, so a dead bridge is instant")

# The bar is driven by the same command, so its stages are the engine's own.
ok(_bar_command == "quad_progress",
   "⚠ progress: the tab's one bar polls quad_progress - the record's key is "
   "`phase`, matching ProgressRow, which is why there is no second widget "
   "(got %r)" % _bar_command)

# --------------------------------------------------- what it does not claim
_src = open(os.path.join(_ROOT, "app",
                         "quadify.py"), encoding="utf-8").read()
# ⚠ NO CONTROL FOR AN UNBUILT FEATURE. The add-on cannot transfer UVs, vertex
# groups or materials yet, so there must be no widget suggesting it can - the
# exact thing this project criticised QRemeshify for.
_widget_text = " ".join(w.text() for w in tool.findChildren(type(tool.run_button))
                        if hasattr(w, "text"))
_boxes = " ".join(b.text() for b in tool.findChildren(type(tool.use_sharp)))
_controls = (_widget_text + " " + _boxes).lower()
for word in ("uv", "vertex group", "weight", "material"):
    ok(word not in _controls,
       "claims: no control offers %s transfer - that batch is not built, and a "
       "tickbox for it would do nothing" % word)
ok("not transferred" in _src.lower() or "no transfer" in _src.lower()
   or "next batch" in _src.lower(),
   "claims: and the panel says so in words rather than staying silent about it")
ok("_call(" not in _src,
   "⚠ claims: `_call` appears nowhere - it is the wrapper that greys the app, "
   "and re-introducing it would undo the whole off-thread fix")

print("")
print("%d passed, %d failed" % (len(PASS), len(FAIL)))
for f in FAIL:
    print("FAIL " + f)
sys.exit(1 if FAIL else 0)
