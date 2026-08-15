# Update safety + developer console.
#
# Compatibility contract: an add-on and an app of different ages must DEGRADE,
# never break. Only the features whose command is genuinely missing switch off;
# everything else keeps working, and nothing throws either way.
#
# Dev console: recorder always on, UI opt-in, and it must never SWALLOW output.
# Offscreen Qt, no Blender, no sockets.
import io
import logging
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


import bridge as b  # noqa: E402
import config  # noqa: E402
import dev_console  # noqa: E402

qapp = QApplication.instance() or QApplication([])

# ===================================================== compatibility ========
CUR = b.EXPECTED_ADDON_VERSION
# FULL = a bridge that can do everything the app gates on. DERIVED from
# FEATURE_REQUIREMENTS, not listed: a hardcoded list silently turns these cases
# into failures the moment another feature is version-gated (it has, once).
FULL = (["ping", "status", "save_pose", "apply_anim"]
        + sorted(b.GATED_COMMANDS))
# LEGACY = an add-on missing EXACTLY ONE gated command, so "only the affected
# feature goes" is a real assertion rather than a tautology.
LEGACY = [c for c in FULL if c != "snapshot_blend"]

# --- the declared contract itself
ok(bool(b.FEATURE_REQUIREMENTS), "features declare what they need")
for feat, (cmd, since, why) in b.FEATURE_REQUIREMENTS.items():
    ok(bool(cmd) and bool(since) and len(why) > 20,
       "'%s' declares a command, a version and a human reason" % feat)
    ok(b.version_tuple(since) <= b.version_tuple(CUR),
       "'%s' requires a version the app itself expects or older" % feat)

# --- 1. NEW app + OLD add-on: only the new feature goes
ok(b.supports(LEGACY, "save_pose"),
   "old add-on: long-standing commands stay available")
ok(not b.supports(LEGACY, "snapshot_blend"),
   "old add-on: the newer command reads as unavailable")
ok(b.missing_features(LEGACY, "0.4.1") == {"background_playblast"},
   "old add-on: exactly the affected feature is reported missing")
reason = b.feature_block_reason(LEGACY, "background_playblast", "0.4.1")
ok(reason and "reinstall" in reason.lower(),
   "old add-on: the reason tells the user what to do")

# --- 2. OLD add-on that can't advertise at all -> judged by version
ok(b.supports(None, "save_pose", "0.4.1"),
   "no capability list: old commands still assumed present")
ok(not b.supports(None, "snapshot_blend", "0.4.1"),
   "no capability list: version fallback knows 0.4.1 predates snapshot_blend")
ok(b.supports(None, "snapshot_blend", "0.4.2"),
   "no capability list: version fallback allows 0.4.2+")
ok(b.supports(None, "some_command_invented_later", "0.1.0"),
   "an ungated command is never blocked by the version fallback")

# --- 3. NEWER add-on than the app: unaffected
newer = FULL + ["a_future_command"]
ok(b.supports(newer, "snapshot_blend") and not b.missing_features(newer, "9.9.9"),
   "newer add-on: nothing missing")
ok(b.version_note("9.9.9", capabilities=newer) is None,
   "newer add-on: no scary status note")

# --- 4. nothing known yet (Blender not reachable): no false alarms
ok(b.missing_features(None) == set(),
   "offline: no features declared missing")
ok(b.feature_block_reason(None, "background_playblast") is None,
   "offline: nothing is blocked on a guess")

# --- 5. status-bar wording: warn only when something is really lost
ok(b.version_note(CUR, capabilities=FULL) is None, "match -> silent")
note = b.version_note("0.4.2", capabilities=FULL)
ok(note and "all features available" in note,
   "older but complete -> informational, not alarming")
note = b.version_note("0.4.1", capabilities=LEGACY)
ok(note and "background_playblast" in note,
   "older and incomplete -> names the lost feature")

# --- 6. the Bridge instance caches the handshake
cl = b.Bridge(port=1)          # never connected; we feed it replies by hand
ok(cl.capabilities is None and cl.addon_version is None,
   "a fresh Bridge knows nothing (distinct from 'supports nothing')")
cl._remember({"version": "0.4.1", "capabilities": LEGACY})
ok(cl.addon_version == "0.4.1" and not cl.supports("snapshot_blend"),
   "handshake is remembered off a status reply")
ok(cl.feature_reason("background_playblast"),
   "the instance reports a blocked feature")
cl._remember({"version": CUR, "capabilities": FULL})
ok(cl.supports("snapshot_blend") and not cl.feature_reason("background_playblast"),
   "a later reply updates it (reinstalling mid-session recovers)")
cl._remember({"version": "0.4.9"})         # reply without the key
ok(cl.capabilities == FULL,
   "a reply lacking 'capabilities' does not wipe what we knew")

# --- 7. the catch-all: a missed gate still explains itself
msg = b.Bridge._explain("snapshot_blend", "unknown command: 'snapshot_blend'")
ok("reinstall the extension" in msg,
   "a raw 'unknown command' is translated into an actionable message")
ok(b.Bridge._explain("save_pose", "no armature selected") == "no armature selected",
   "ordinary errors pass through untouched")

# ===================================================== dev console =========
ok(config.DEFAULTS.get("dev_console") is False,
   "developer console ships OFF by default")
ok("dev_console" in config.load(), "older config.json still gets the key")

buf = dev_console.LogBuffer()
buf.add("INFO", "hello")
ok("hello" in buf.text(), "buffer records a line")
ok(buf.error_count == 0, "INFO is not counted as an error")
buf.add("ERROR", "went wrong")
ok(buf.error_count == 1, "errors are counted for the button badge")
buf.add("INFO", "one\ntwo\nthree")
ok(buf.text().count("\n") >= 4, "multi-line entries are split into lines")
buf.add("INFO", "   \n  ")
before = len(buf.lines)
buf.add("INFO", "")
ok(len(buf.lines) == before, "blank output is not recorded")

# bounded: memory can't grow forever in a long session
small = dev_console.LogBuffer()
for i in range(dev_console.MAX_LINES + 500):
    small.add("INFO", "line %d" % i)
ok(len(small.lines) == dev_console.MAX_LINES,
   "the buffer is bounded at MAX_LINES (%d)" % dev_console.MAX_LINES)
ok("line %d" % (dev_console.MAX_LINES + 499) in small.text(),
   "…keeping the NEWEST lines, which are the ones you need")

# the tee must not silence the real stream
sink = io.StringIO()
tee = dev_console._Tee(sink, buf, "INFO")
tee.write("through to the real stream\n")
ok("through to the real stream" in sink.getvalue(),
   "stdout capture TEEs — the real stream still gets everything")
ok("through to the real stream" in buf.text(), "…and the buffer sees it too")
dead = dev_console._Tee(None, buf, "INFO")
dead.write("no stream at all\n")
ok("no stream at all" in buf.text(),
   "a frozen exe with no stdout still records (must not crash)")

# logging handler routing
lg = logging.getLogger("madi.compat.test")
lg.propagate = False
lg.setLevel(logging.INFO)
lg.addHandler(dev_console._BufferHandler(buf))
lg.warning("careful")
lg.error("broken")
ok("careful" in buf.text() and "broken" in buf.text(),
   "logging output reaches the console buffer")
ok("WARN" in buf.text(), "WARNING is shortened to WARN for the fixed column")

buf.clear()
ok(buf.text() == "" and buf.error_count == 0, "clear resets lines and count")

# ============================================== unreachable bridge ========
# 2026-08-02 bug: with Blender's server stopped the app froze every few
# seconds. A connect to a dead localhost port is NOT refused here — the SYN is
# dropped, so every attempt burned the whole timeout, on the GUI thread, from
# pollers running every 1.5-5 s. These lock in the fix.
import socket  # noqa: E402
import time  # noqa: E402

ok(b.CONNECT_TIMEOUT <= 0.5,
   "connecting is capped tightly (%.2fs) — a live localhost connect is ~15 ms, "
   "so a long wait only ever delays a failure" % b.CONNECT_TIMEOUT)

# a port nothing listens on, to exercise the failure path deterministically
_probe = socket.socket()
_probe.bind(("127.0.0.1", 0))
DEAD_PORT = _probe.getsockname()[1]
_probe.close()

dead = b.Bridge(port=DEAD_PORT)
ok(dead.reachable, "a fresh Bridge starts optimistic (nothing known yet)")

t0 = time.time()
try:
    dead.status(timeout=5.0, probe=True)
    ok(False, "a dead port must not answer")
except b.BridgeError:
    elapsed = time.time() - t0
ok(elapsed < 1.0,
   "a failed connect costs the connect cap, NOT the command timeout "
   "(%.3fs with timeout=5.0)" % elapsed)
ok(not dead.reachable, "a failed connect marks the bridge unreachable")

# pollers must fail INSTANTLY while it's known down — this is the freeze fix
t0 = time.time()
for _ in range(50):
    try:
        dead.anim_layers_status(poll=True)
    except b.BridgeError:
        pass
poll_cost = time.time() - t0
ok(poll_cost < 0.05,
   "50 polls while down cost %.4fs total (they fail fast, never wait)"
   % poll_cost)

# …but a USER action still tries, so clicking right after Blender comes back
# works instead of being told to wait for the next health check
t0 = time.time()
try:
    dead.anim_layers_status(poll=False)
except b.BridgeError as exc:
    user_cost = time.time() - t0
    ok("rechecking" not in str(exc),
       "a user-driven call is NOT refused by the gate — it really tries")
ok(0.05 < user_cost < 1.0,
   "…and that attempt is still capped by the connect timeout (%.3fs)"
   % user_cost)

# the probe is the designated knocker: always tries, and its success is what
# reopens the gate for everyone
t0 = time.time()
try:
    dead.status(timeout=5.0, probe=True)
except b.BridgeError:
    pass
ok(time.time() - t0 > 0.05, "the probe keeps knocking even while gated")
dead._mark_up()
ok(dead.reachable, "a successful connect reopens the gate for all callers")
t0 = time.time()
try:
    dead.anim_layers_status(poll=True)
except b.BridgeError:
    pass
ok(time.time() - t0 > 0.05,
   "…so pollers resume trying immediately once it's back")

# a failure AFTER connecting is 'busy', not 'unreachable' — the gate must not
# trip and start refusing pollers just because one command timed out
srv = socket.socket()
srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind(("127.0.0.1", 0))
srv.listen(4)
mute_port = srv.getsockname()[1]
mute = b.Bridge(port=mute_port)
try:
    mute.request("status", timeout=0.4)
    ok(False, "a silent server must not answer")
except b.BridgeError as exc:
    ok("stopped responding" in str(exc),
       "connected-but-silent reads as 'stopped responding', not 'unreachable'")
ok(mute.reachable,
   "a read timeout does NOT trip the unreachable gate (Blender is just busy)")
srv.close()

# the app polls health OFF the GUI thread and backs off while down
import main as appmod  # noqa: E402
ok(appmod.SLOW_STATUS_MS > appmod.FAST_STATUS_MS,
   "the health poll backs off while the bridge is down (%d -> %d ms)"
   % (appmod.FAST_STATUS_MS, appmod.SLOW_STATUS_MS))
ok(b.UNREACHABLE_BACKOFF > appmod.SLOW_STATUS_MS / 1000.0,
   "the fail-fast window outlasts the slow poll, so a still-failing probe "
   "keeps the gate shut and no other caller ever attempts a connect")

win = appmod.MainWindow()
win.bridge = b.Bridge(port=DEAD_PORT)
t0 = time.time()
win.update_bridge_status()
ok(time.time() - t0 < 0.05,
   "update_bridge_status returns immediately — it must never block the GUI "
   "thread on a socket")
ok(win._status_worker is not None, "…because the check runs on a worker")
before = win._status_worker
win.update_bridge_status()
ok(win._status_worker is before, "a second tick doesn't pile up more workers")
win.close()

print("\n%d passed, %d failed" % (len(PASS), len(FAIL)), flush=True)
sys.exit(1 if FAIL else 0)
