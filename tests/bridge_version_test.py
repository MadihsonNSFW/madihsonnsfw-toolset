# Bridge plumbing (2026-08-02 optimization): the adaptive tick interval and
# the add-on/app version handshake. Pure logic — no sockets, no Blender UI —
# so it runs in the Blender-side pass next to the other core.py suites.
# Run: blender.exe -b --factory-startup --python bridge_version_test.py
import importlib.util
import os
import re
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

ADDON = os.path.join(_ROOT, "blender_addon", "madi_anim_library")
APP = os.path.join(_ROOT, "app")

spec = importlib.util.spec_from_file_location(
    "madi_core", os.path.join(ADDON, "core.py"))
core = importlib.util.module_from_spec(spec)
spec.loader.exec_module(core)

PASS = []
FAIL = []


def ok(cond, label):
    (PASS if cond else FAIL).append(label)
    print(("ok   " if cond else "FAIL ") + label, flush=True)


# ---------------------------------------------------- version sync (3 files)
with open(os.path.join(ADDON, "blender_manifest.toml"), encoding="utf-8") as fh:
    manifest = fh.read()
m = re.search(r'^version\s*=\s*"([^"]+)"', manifest, re.M)
ok(m is not None, "manifest has a version")
manifest_version = m.group(1) if m else None
ok(core.ADDON_VERSION == manifest_version,
   "core.ADDON_VERSION (%s) matches the manifest (%s)"
   % (core.ADDON_VERSION, manifest_version))

with open(os.path.join(APP, "bridge.py"), encoding="utf-8") as fh:
    app_src = fh.read()
m2 = re.search(r'^EXPECTED_ADDON_VERSION\s*=\s*"([^"]+)"', app_src, re.M)
ok(m2 is not None, "app declares EXPECTED_ADDON_VERSION")
ok(m2 and m2.group(1) == manifest_version,
   "app expects the shipped add-on version (%s vs %s)"
   % (m2.group(1) if m2 else None, manifest_version))

# ------------------------------------------------------- version_note logic
# bridge.py is pure stdlib at module level, so it imports fine under Blender.
sys.path.insert(0, APP)
import bridge as bridgemod  # noqa: E402

# NOTE (2026-08-02): a version DIFFERENCE is no longer treated as breakage.
# What earns a warning is a MISSING CAPABILITY the app actually needs, so
# these cases are stated in terms of what the bridge can do, not its number.
CUR = bridgemod.EXPECTED_ADDON_VERSION
# FULL = a bridge that can do everything the app gates on. DERIVED from
# FEATURE_REQUIREMENTS rather than listed, so adding a gated feature can't
# quietly turn these cases into failures the way a hardcoded list does.
FULL = ["ping", "status", "save_pose"] + sorted(bridgemod.GATED_COMMANDS)
LEGACY = ["ping", "status", "save_pose"]          # pre-snapshot_blend

ok(bridgemod.version_note(CUR, capabilities=FULL) is None,
   "matching versions, nothing missing -> no warning")
note = bridgemod.version_note("0.4.1", capabilities=LEGACY)
ok(note is not None and "reinstall the extension" in note
   and "background_playblast" in note,
   "older add-on missing a feature -> names the feature (got %r)" % note)
note = bridgemod.version_note("0.4.2", capabilities=FULL)
ok(note is not None and "all features available" in note,
   "older add-on that still has everything -> informational, not a warning "
   "(got %r)" % note)
note = bridgemod.version_note("9.9.9", capabilities=FULL + ["future_cmd"])
ok(note is None,
   "newer add-on -> no complaint; extra commands can't hurt (got %r)" % note)
ok(bridgemod.version_note("0.4.1", capabilities=None) is not None,
   "old add-on that advertises nothing is still judged by its version")
note = bridgemod.version_note(None, expected="0.4.1")
ok(note is not None and "predates version reporting" in note,
   "add-on too old to report -> clear message")
ok(bridgemod.version_tuple("0.4.1") == (0, 4, 1), "version_tuple parses")
ok(bridgemod.version_tuple("0.10.0") > bridgemod.version_tuple("0.9.9"),
   "version compare is numeric, not lexical")
ok(bridgemod.version_tuple("1.2.3-beta") == (1, 2, 3),
   "non-numeric suffixes tolerated")
ok(bridgemod.version_tuple("") == (0,), "empty string is safe")

# ------------------------------------------------------------ adaptive tick
# Import server.py WITHOUT the package machinery: stub the `from . import core`
# relative import so the module body runs standalone.
import types  # noqa: E402

pkg = types.ModuleType("madi_pkg")
pkg.__path__ = [ADDON]
pkg.core = core
sys.modules["madi_pkg"] = pkg
sys.modules["madi_pkg.core"] = core
sspec = importlib.util.spec_from_file_location(
    "madi_pkg.server", os.path.join(ADDON, "server.py"))
server = importlib.util.module_from_spec(sspec)
sspec.loader.exec_module(server)

ok(server._TICK_HOT < server._TICK_IDLE,
   "hot interval is faster than idle (%.3f < %.3f)"
   % (server._TICK_HOT, server._TICK_IDLE))
ok(server._TICK_HOT <= 0.01,
   "hot tick <= 10 ms so a drag isn't bridge-bound (%.3f)" % server._TICK_HOT)
ok(server._TICK_IDLE <= 0.05,
   "idle tick no slower than the old flat 0.05 (%.3f)" % server._TICK_IDLE)
ok(server._HOT_WINDOW >= 1.0,
   "hot window covers a pause between clicks (%.1fs)" % server._HOT_WINDOW)

srv = server.BridgeServer()
ok(srv._tick_interval() == server._TICK_IDLE,
   "a fresh server idles at the slow tick")
srv._last_activity = time.monotonic()
ok(srv._tick_interval() == server._TICK_HOT,
   "activity switches it to the hot tick")
srv._last_activity = time.monotonic() - (server._HOT_WINDOW + 0.5)
ok(srv._tick_interval() == server._TICK_IDLE,
   "it relaxes again once the hot window expires")

# _process_queue must drain everything queued and re-stamp activity
handled = []
srv._handle = lambda req: handled.append(req["cmd"]) or {"echo": req["cmd"]}
import queue as _queue  # noqa: E402
import threading as _threading  # noqa: E402

holders = []
for name in ("a", "b", "c"):
    done = _threading.Event()
    holder = {}
    holders.append((done, holder))
    srv._queue.put(({"cmd": name, "params": {}}, done, holder))
srv._last_activity = 0.0
interval = srv._process_queue()
ok(handled == ["a", "b", "c"], "every queued request drained in one tick")
ok(all(d.is_set() for d, _ in holders), "each waiter released")
ok(all(h["response"]["ok"] for _, h in holders), "each got an ok response")
ok(interval == server._TICK_HOT,
   "a tick that did work returns the HOT interval")

# a failing handler still answers (and doesn't wedge the queue)
def boom(_req):
    raise RuntimeError("nope")


srv._handle = boom
done = _threading.Event()
holder = {}
srv._queue.put(({"cmd": "x", "params": {}}, done, holder))
srv._process_queue()
ok(done.is_set() and holder["response"]["ok"] is False
   and "nope" in holder["response"]["error"],
   "a raising handler returns an error response, queue keeps running")
ok(srv._queue.empty(), "queue drained")

# --------------------------------------------- capabilities advertising ----
# The add-on tells the app which commands it answers, so a version gap
# degrades one feature instead of breaking. Derived from the dispatcher's
# SOURCE precisely so it can never go stale.
caps = server.BridgeServer.capabilities()
ok(isinstance(caps, list) and len(caps) > 40,
   "capabilities() advertises the whole command set (%d)" % len(caps))
ok(caps == sorted(set(caps)), "capabilities are sorted and unique")
for must in ("ping", "status", "save_pose", "apply_anim", "playblast",
             "snapshot_blend", "anim_layers_bake", "jiggle_status"):
    ok(must in caps, "capabilities include '%s'" % must)
# ⚠ The cage_* commands were removed outright 2026-08-14 (Proxy Cage is gone);
# an advertised ghost would mean a route the dispatcher cannot serve.
ok(not any(c.startswith("cage_") for c in caps),
   "capabilities carry NO cage_* ghosts after the removal")

# every advertised name must really be handled, and vice versa: the guard that
# keeps this honest as commands come and go
import inspect as _inspect  # noqa: E402
handle_src = _inspect.getsource(server.BridgeServer._handle)
ok(all(('"%s"' % c) in handle_src or ("'%s'" % c) in handle_src for c in caps),
   "every advertised capability appears in the dispatcher")
declared = set(re.findall(r'cmd\s*==\s*["\']([a-z0-9_]+)["\']', handle_src))
ok(declared == set(caps),
   "advertised set == dispatcher set (no drift possible)")

# ping and status must both carry it — the app reads whichever it has
ping_src = handle_src[handle_src.find('cmd == "ping"'):]
ok("capabilities" in ping_src[:400], "ping reports capabilities")
ok(handle_src.count("self.capabilities()") >= 2,
   "status reports capabilities too")

# -------- three bridge states, and NOTHING takes the port on its own (0.39.0) -
# "off" and "another Blender already has the port" look identical through a
# bare running flag, and they need opposite reactions: press Start, versus
# "go and stop it in the OTHER Blender" (Marty ran two Blenders, 2026-08-05).
#
# ⚠ THE THIRD STATE USED TO BE `waiting` AND USED TO MEAN IT: a losing instance
# retried every 5 s and took the port the moment the holder let go. Marty,
# 2026-08-12: "when one is active do not let them start it on another blender
# instance unless they stop first". So the retry is deleted, the state is
# `blocked`, and the checks below are the mirror image of the ones they replace.
import bpy  # noqa: E402

a = server.BridgeServer(port=9979)
ok(a.state == "stopped", "a fresh server reports 'stopped'")
ok(a.start() is True and a._running and a.state == "listening",
   "started, it reports 'listening' and start() returns True (%s)" % a.state)

b = server.BridgeServer(port=9979)     # same port: the second Blender
ok(b.start() is False,
   "⚠ start() RETURNS FALSE on a taken port — the operator judges this, and "
   "an exception cannot reach it because start() catches its own bind error")
ok(not b._running and b.state == "blocked",
   "⚠ a second server on a taken port reports 'blocked', NOT 'stopped' (%s)"
   % b.state)
ok(not hasattr(b, "_retry_start"),
   "⚠ `_retry_start` IS GONE, not merely unregistered — a dormant retry is one "
   "someone wires back in by accident, and it was the whole mechanism by which "
   "the bridge moved between instances with nobody pressing anything")

a.stop()
ok(a.state == "stopped", "the holder reports 'stopped' once stopped")
ok(not b._running and b.state == "blocked",
   "⚠ AND THE BLOCKED INSTANCE STAYS BLOCKED once the port frees — this is the "
   "behaviour Marty asked for: the bridge only ever moves when somebody "
   "presses Start (%s)" % b.state)
ok(b.start() is True and b._running and b.state == "listening",
   "...and pressing Start is what moves it — the same instance takes the now "
   "free port on request")
b.stop()

panel_src = open(os.path.join(ADDON, "__init__.py"), encoding="utf-8").read()
ok("blocked" in panel_src and "Another Blender has the bridge" in panel_src
   and "Stop it there" in panel_src,
   "the N-panel says which of the three states it is in, and what to do")

# ⚠ THE BRIDGE MUST NEVER START ITSELF (Marty, 2026-08-12: "no matter what").
# ⚠ PARSED, NOT GREPPED. Both of these were first written as `"_autostart" not
# in panel_src` and both FAILED against correct code: the comments explaining
# the removal name the very things they removed. Same trap as the licence
# work's "unlocks permanently" marker — an absence check that reads raw source
# is really asking "did anyone mention this", which is the opposite question.
# The AST sees declarations only, so prose about them is free.
import ast  # noqa: E402

_tree = ast.parse(panel_src)
_funcs = {n.name for n in ast.walk(_tree)
          if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
ok("_autostart" not in _funcs,
   "⚠ no autostart function survives in the add-on's __init__ — register() "
   "must not be able to bring the bridge up on its own")
_prefs_cls = next((n for n in _tree.body if isinstance(n, ast.ClassDef)
                   and n.name == "MADILIB_Prefs"), None)
_props = {n.target.id for n in (_prefs_cls.body if _prefs_cls else [])
          if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name)}
ok(_prefs_cls is not None and "auto_start" not in _props,
   "⚠ ...and the preference went with it — a switch that cannot do what it "
   "says is worse than no switch (%s)" % sorted(_props))
ok("port" in _props,
   "...while the port preference, which the Start button still reads, stays")

# ...but a RELOAD must put back a bridge that WAS running, or a self-update
# installs correctly and reads as a hang while the app re-polls a socket that
# is never reopened.
su_src = open(os.path.join(ADDON, "selfupdate.py"), encoding="utf-8").read()
ok("_resume_bridge" in su_src and "was_serving" in su_src,
   "⚠ selfupdate RESUMES a bridge that was serving before the reload — "
   "restoring what the user started, which is not the same as autostarting")


# =============== nothing in the shipped add-on may name ONE MACHINE'S DISK ==
# ⚠ Marty, 2026-08-08, on the eve of handing a build to someone else: "in
# bridge app make sure that we can select the path to our app because now it's
# hardcoded to our path". Both `DEFAULT_APP` and `DEFAULT_LIBRARY` were
# absolute paths on the machine this was written on — correct for exactly one
# install, pointing at a folder that does not exist on every other one.
#
# This scans EVERY shipped module, not just the two that were wrong, because
# the next one will be somewhere else.
drive = re.compile(r"""["'](?:[A-Za-z]:[\\/]|\\\\[A-Za-z0-9_-]+[\\/])""")
offenders = []
for name in sorted(os.listdir(ADDON)):
    if not name.endswith(".py"):
        continue
    src = open(os.path.join(ADDON, name), encoding="utf-8").read()
    for num, line in enumerate(src.splitlines(), 1):
        bare = line.split("#", 1)[0]          # a path in a COMMENT is fine
        if drive.search(bare):
            offenders.append("%s:%d %s" % (name, num, line.strip()[:70]))
ok(not offenders,
   "⚠ no absolute path is baked into the shipped add-on — it would be right "
   "on one machine and wrong on every other (%s)"
   % ("; ".join(offenders) if offenders else "clean"))

ok('DEFAULT_APP = ""' in panel_src,
   "...and the one path the add-on still keeps defaults to EMPTY rather than "
   "to a guess")
ok("DEFAULT_LIBRARY" not in panel_src,
   "...while the other went entirely: with no Save/Apply in the panel the "
   "add-on has no library of its own, and every bridge command carries its "
   "own library_root from the app")
ok("fileselect_add" in panel_src,
   "Open Toolset App ASKS for the exe when none is set, instead of failing "
   "with 'go and edit a preference'")
ok("use_preferences_save" in panel_src,
   "...and warns when Blender's preference auto-save is off, because the path "
   "it just remembered would be forgotten on restart")
ok('bpy.path.abspath(raw) if raw else ""' in panel_src,
   "⚠ an EMPTY app path never goes through bpy.path.abspath — that returns "
   "the .blend's own folder, so an unset path would quietly become a real "
   "and wrong one")


# ========== the N-panel is the BRIDGE, not a second library UI (2026-08-08) ==
# Marty: "We don't need Save pose features in the blender bridge, only app like
# we have now is fine, same with apply poses". The app has the grid, folders,
# tags, versions and the save dialogs; a poorer copy inside Blender was only
# ever somewhere for the two to disagree.
for gone in ("madilib.save_pose", "madilib.save_set", "madilib.apply_item",
             "madilib.refresh_items", "MADILIB_Props",
             "WindowManager.madilib ", "library_path"):
    ok(gone not in panel_src,
       "panel: %r is gone from the add-on — saving and applying live in the "
       "app" % gone)
# ...and what the panel IS for is still there.
for kept in ("madilib.server_toggle", "madilib.open_app",
             "madilib.watch_last_render"):
    ok(kept in panel_src, "panel: %r stays — that is what the panel is" % kept)

# ⚠ THE ENGINE IS UNTOUCHED. The bridge commands still call these, and that is
# how the app does the work; deleting them because "the panel does not use
# them any more" would take the feature out of both halves.
for fn in ("save_pose", "save_set", "apply_pose", "apply_set", "apply_anim"):
    ok(callable(getattr(core, fn, None)),
       "panel: core.%s still exists — the app reaches it over the bridge" % fn)

# Anim Layers has its OWN property group; removing ours must not have taken it.
al_src = open(os.path.join(ADDON, "anim_layers_ui.py"), encoding="utf-8").read()
ok("madilib_al" in al_src,
   "panel: Anim Layers keeps `wm.madilib_al` — a different property that only "
   "looks like the one that went")

# ------------------------------------------------ save_blend (0.34.0, job 2)
# Backs the Render Queue's Save & Queue: the queue renders FILES, so queueing
# the open scene without saving would render whatever was last written.
import tempfile  # noqa: E402  (Blender-side suite; imported where it is used)

ok(callable(getattr(core, "save_blend", None)),
   "save_blend: the command exists in core")

# ⚠ THE REFUSAL IS THE IMPORTANT HALF. `blender -b --factory-startup` has never
# saved, so bpy.data.filepath is "" — exactly the state a user hits on a fresh
# scene, and the one where inventing a path would be worst.
ok(bpy.data.filepath == "", "save_blend: the test file has no path (setup)")
_err = None
try:
    core.save_blend()
except RuntimeError as exc:
    _err = str(exc)
ok(_err is not None, "save_blend: an unsaved file is REFUSED, not guessed at")
ok(_err and "Save As" in _err,
   "save_blend: ...and the message says what to do about it (%r)" % _err)

_tmp_blend = os.path.join(tempfile.mkdtemp(prefix="madi_sb_"), "queued.blend")
bpy.ops.wm.save_as_mainfile(filepath=_tmp_blend)
_was = bool(bpy.data.is_dirty)
_r = core.save_blend()
ok(os.path.normcase(_r["path"]) == os.path.normcase(_tmp_blend),
   "save_blend: reports the absolute path it wrote (%s)" % _r["path"])
ok(_r["size"] > 0, "save_blend: and its size on disk (%d bytes)" % _r["size"])
ok(_r["was_dirty"] == _was,
   "save_blend: was_dirty reports the state the file was in on arrival")
ok(bpy.data.is_dirty is False, "save_blend: and the file is clean afterwards")
# ⚠ THE ORDER IS THE POINT, and behaviour alone cannot show it: after any save
# `is_dirty` is False, so a version that sampled it AFTERWARDS would answer
# "nothing to do" every single time and the app would say so to a user whose
# work had in fact just been written. Headless never sets the flag (no undo
# stack), so the guarantee is pinned at the source.
_sb_src = _inspect.getsource(core.save_blend)
ok(_sb_src.index("was_dirty = bool(bpy.data.is_dirty)")
   < _sb_src.index("bpy.ops.wm.save_mainfile()"),
   "save_blend: ⚠ was_dirty is sampled BEFORE the save, not after")

# the route + the app's gate, so a new command can't reach the app un-gated
srv_src = open(os.path.join(ADDON, "server.py"), encoding="utf-8").read()
ok('if cmd == "save_blend"' in srv_src,
   "save_blend: routed in the dispatcher, so capabilities() advertises it")
_req = bridgemod.FEATURE_REQUIREMENTS.get("save_open_blend")
ok(_req is not None and _req[0] == "save_blend",
   "save_blend: the app declares a FEATURE_REQUIREMENTS entry, so an older "
   "add-on costs the ONE button and not the Render Queue")
# ⚠ A FLOOR, never `==` — two suites broke inside an hour on an unrelated bump,
# and the three-way version agreement is this file's other job, not this line's.
ok(_req is not None and bridgemod.version_tuple(_req[1])
   <= bridgemod.version_tuple(core.ADDON_VERSION),
   "save_blend: its floor (%s) is at or below the add-on shipping it (%s)"
   % (_req[1] if _req else "?", core.ADDON_VERSION))

print("\n%d passed, %d failed" % (len(PASS), len(FAIL)), flush=True)
sys.exit(1 if FAIL else 0)
