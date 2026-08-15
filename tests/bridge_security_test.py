# Bridge and client-side abuse defences (add-on 0.22.0, 2026-08-06).
#
#   blender.exe -b --factory-startup --python tests\bridge_security_test.py
#
# WHAT THIS SUITE IS ABOUT. The bridge listens on 127.0.0.1 and authenticates
# nothing, which was a deliberate trade: a local port for a local app, and any
# process that could reach it was already running as the user. That reasoning
# has one hole in it, and it is the reason this file exists:
#
#   A WEB PAGE IS ALSO A LOCAL PROCESS.
#
# The read loop answered "bad json" to a line it could not parse and then KEPT
# READING, so a browser fetch() POST - a few header lines it rejected, then a
# body - had its body dispatched as a command. Proven against Marty's live
# bridge on 2026-08-06: an HTTP POST from a page ran `ping` on his Blender. The
# same shape reaches `addon_update`, which installs a Blender extension, which
# is arbitrary code.
#
# The second half is the jiggle cache, which used `pickle.load` on files that
# live BESIDE THE .BLEND - so opening a downloaded rig and scrubbing the
# timeline was arbitrary code execution too.
#
# ⚠ Uses a THROWAWAY PORT. Marty's real Blender holds 9877 and this suite must
# never fight it for the port.
import importlib
import json
import os
import socket
import sys
import tempfile
import threading
import types

import bpy

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

ADDON = os.path.join(_ROOT, "blender_addon", "madi_anim_library")
TEST_PORT = 39877  # never 9877 - see the header

# ⚠ AND A THROWAWAY TOKEN DIRECTORY, for exactly the same reason. This suite
# calls the REAL bridgeauth.issue() / clear(), and `token_dir()` reads
# LOCALAPPDATA at call time — so without this the suite mints and then
# DELETES the token file belonging to Marty's live Blender, three rooms
# away. That is not hypothetical: it is what actually happened, three
# times, and got written up twice as "cause not established" (a refused
# add-on push, `docs\security.md` §5) before the suite was caught doing it.
# A test that shares a path with the live session is not isolated, however
# careful it is about the port.
os.environ["LOCALAPPDATA"] = tempfile.mkdtemp(prefix="madi_sec_appdata_")

pkg = types.ModuleType("madi_sec_pkg")
pkg.__path__ = [ADDON]
sys.modules["madi_sec_pkg"] = pkg
bridgeauth = importlib.import_module("madi_sec_pkg.bridgeauth")
assert tempfile.gettempdir().lower() in bridgeauth.token_dir().lower(), (
    "the token sandbox did not take — refusing to run against the real path")

PASS = []
FAIL = []


def ok(cond, label):
    (PASS if cond else FAIL).append(label)


# --------------------------------------------------------------- the socket
#
# A real BridgeServer on a throwaway port. Only the CLIENT-THREAD half of the
# protocol is exercised, which is the half the fixes live in - a dispatched
# command would need `_process_queue`, and bpy timers never fire in background
# Blender.

server = importlib.import_module("madi_sec_pkg.server")


class _Loop:
    """Just enough BridgeServer to run _client_loop against a real socket."""

    def __init__(self):
        self._running = True
        self.dispatched = []

    _client_loop = server.BridgeServer._client_loop

    def _dispatch(self, raw):
        # Mirrors the real _dispatch's FIRST step - parse, and report a parse
        # failure as an ok:false reply. The stub used to return ok:true for
        # everything, which quietly turned the "a syntax error is reported"
        # check into a test of the stub. Everything past the parse needs the
        # main-thread queue, which background Blender has no timers to run.
        self.dispatched.append(raw)
        try:
            json.loads(raw.decode("utf-8"))
        except Exception as exc:
            return {"ok": False, "error": "bad json: %s" % exc}
        return {"ok": True, "result": {"pong": True}}


def talk(payload, read_for=1.5):
    """Send bytes to a one-shot loop; return (replies, commands_dispatched)."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    loop = _Loop()

    def serve():
        conn, _ = listener.accept()
        try:
            loop._client_loop(conn)
        finally:
            listener.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()

    client = socket.create_connection(("127.0.0.1", port), timeout=5)
    client.sendall(payload)
    client.settimeout(read_for)
    buf = b""
    try:
        while True:
            chunk = client.recv(65536)
            if not chunk:
                break
            buf += chunk
    except socket.timeout:
        pass
    closed_by_server = not buf.endswith(b"KEEPALIVE")
    client.close()
    loop._running = False
    thread.join(timeout=3)
    replies = [json.loads(l) for l in buf.decode("utf-8", "replace").split("\n") if l.strip()]
    return replies, loop.dispatched, closed_by_server


# 1. THE HEADLINE: an HTTP request from a web page must not reach the dispatcher.
HTTP_POST = (
    b"POST / HTTP/1.1\r\n"
    b"Host: 127.0.0.1:9877\r\n"
    b"Content-Type: text/plain;charset=UTF-8\r\n"
    b"Origin: https://evil.example\r\n"
    b"\r\n"
    b'{"cmd":"ping"}\n'
)
replies, dispatched, _ = talk(HTTP_POST)
ok(len(dispatched) == 0,
   "an HTTP POST from a web page dispatches NOTHING (its body used to run)")
ok(len(replies) == 1 and not replies[0]["ok"],
   "...it gets exactly one refusal, then the connection goes")
ok("newline-delimited JSON" in replies[0].get("error", ""),
   "...and the refusal says what this port actually speaks")

# 2. The same, with the command first: still refused, because the FIRST line
#    decides. Belt and braces against a cleverer request line.
replies, dispatched, _ = talk(b"GET /?x=1 HTTP/1.1\r\n" + b'{"cmd":"ping"}\n')
ok(len(dispatched) == 0, "a GET request line is refused before anything is run")

# 3. A real client is completely unaffected.
replies, dispatched, _ = talk(b'{"cmd":"ping"}\n{"cmd":"status"}\n')
ok(len(dispatched) == 2, "two genuine commands on one connection both run")
ok(all(r["ok"] for r in replies), "...and both are answered")

# 4. Malformed JSON that IS an object still gets an error without a hang-up:
#    a real client with a bug deserves a message, not a dropped socket.
replies, dispatched, _ = talk(b'{"cmd": oops}\n{"cmd":"ping"}\n')
ok(len(dispatched) == 2, "a JSON syntax error is reported, and the client may retry")
ok(not replies[0]["ok"] and replies[1]["ok"], "...the retry on the same connection works")

# 5. A stream with no newline at all must not grow for ever.
ok(server._MAX_LINE <= 128 * 1024 * 1024, "the receive buffer has a hard ceiling")
ok(server._MAX_LINE >= 8 * 1024 * 1024, "...generous enough for a picker tab image")

# ------------------------------------------------------- the addon_update lock

os.environ["LOCALAPPDATA"] = tempfile.mkdtemp(prefix="madi_sec_")
importlib.reload(bridgeauth)

ok(bridgeauth.current() is None, "no token exists before the bridge starts")
ok(not bridgeauth.check("anything"), "...and nothing verifies against a token that does not exist")

issued = bridgeauth.issue()
ok(isinstance(issued, str) and len(issued) >= 32, "a token is minted, and it is long")
ok(os.path.isfile(bridgeauth.token_path()), "...written where the app can read it")
with open(bridgeauth.token_path(), encoding="utf-8") as fh:
    ok(fh.read().strip() == issued, "...and the file holds exactly that token")

ok(bridgeauth.check(issued), "the real token verifies")
for bad in ["", None, 123, issued + "x", issued[:-1], issued.upper()]:
    ok(not bridgeauth.check(bad), "a wrong token is refused: %r" % (bad,))

second = bridgeauth.issue()
ok(second != issued, "restarting the bridge mints a NEW token")
ok(not bridgeauth.check(issued), "...and the previous one stops working, so it cannot be replayed")

bridgeauth.clear()
ok(not os.path.isfile(bridgeauth.token_path()), "stopping the bridge removes the file")
ok(not bridgeauth.check(second), "...and refuses everything again")

# ⚠ A STRANDED SERVER MUST NOT DELETE A LIVE SESSION'S TOKEN. `reload_addon`
# stops every BridgeServer it can find through the GC because old instances are
# left behind by earlier reloads - three were live after the first real 0.22.0
# reload. `clear()` deleted the file unconditionally, so the order that actually
# happened was "new add-on writes token, stranded server deletes it", leaving a
# running bridge whose token the app could not read and `addon_update` refusing
# the app by its own rule. Found on Marty's live Blender, not in a test.
live = bridgeauth.issue()
stale_token = "a-token-a-previous-session-issued"
bridgeauth._TOKEN = stale_token          # what a stranded instance still holds
bridgeauth.clear()
ok(os.path.isfile(bridgeauth.token_path()),
   "a stale instance's stop() does NOT delete the live token file")
with open(bridgeauth.token_path(), encoding="utf-8") as fh:
    ok(fh.read().strip() == live, "...the file still holds the LIVE token")
bridgeauth._TOKEN = live
bridgeauth.clear()
ok(not os.path.isfile(bridgeauth.token_path()),
   "...and the session that owns it can still clean up after itself")

# ⚠ AND THE SECOND FIX (0.25.0). Comparing the FILE was not enough: `_TOKEN` is
# a MODULE global, so every BridgeServer inside one module instance shares it. A
# stranded server stopping read the LIVE server's token out of that global,
# matched it against the file and deleted a file it did not own. Caught on
# Marty's live 0.24.3 - bridge listening, 43-char token in memory, `bridge.token`
# gone from disk, and `addon_update` refusing the real app with "must come from
# the Toolset app". Identity now travels with the INSTANCE.
live2 = bridgeauth.issue()
bridgeauth.clear("a-token-from-an-instance-that-never-won-the-port")
ok(os.path.isfile(bridgeauth.token_path()),
   "a stranded instance sharing the module cannot delete the live token FILE")
ok(bridgeauth.check(live2),
   "...and cannot wipe the live token out of MEMORY either (the shared-global "
   "hole: the old clear() would have matched the global and unlinked)")
bridgeauth.clear(live2)
ok(not os.path.isfile(bridgeauth.token_path()) and not bridgeauth.check(live2),
   "...while the instance that DID issue it still cleans up, given its own")

with open(os.path.join(ADDON, "server.py"), encoding="utf-8") as fh:
    server_src = fh.read()
ok("self._token = bridgeauth.issue()" in server_src,
   "wiring: start() keeps the token it minted when it won the port")
ok("bridgeauth.clear(self._token)" in server_src,
   "wiring: stop() may only clear the token THAT instance issued")

# The app must look in the SAME place. Read from the app's own module rather
# than repeating the path here, or the two halves drift and the only symptom is
# "Update add-on stopped working".
sys.path.insert(0, os.path.join(_ROOT, "app"))
app_bridge = importlib.import_module("bridge")
ok(os.path.normcase(os.path.dirname(
       os.path.join(os.environ["LOCALAPPDATA"], "MadihsonNSFW Toolset",
                    app_bridge.BRIDGE_TOKEN_FILE)))
   == os.path.normcase(bridgeauth.token_dir()),
   "the app and the add-on agree on where the token lives")
bridgeauth.issue()
ok(app_bridge.bridge_token() == bridgeauth.current(),
   "the app reads back exactly the token the add-on wrote")
bridgeauth.clear()
ok(app_bridge.bridge_token() == "", "with no file the app sends no token rather than crashing")

# The gate itself, on the real dispatcher. `addon_update` installs a Blender
# extension - arbitrary code - so it is the one command that asks who is
# calling. Constructed, never started: this must not touch a real port.
srv = server.BridgeServer(port=TEST_PORT)


def refused(params):
    try:
        srv._handle({"cmd": "addon_update", "params": params})
        return None
    except Exception as exc:                       # noqa: BLE001
        return str(exc)


bridgeauth.clear()
for params in [{"path": "x.zip"}, {"path": "x.zip", "auth": ""},
               {"path": "x.zip", "auth": "guessed"}]:
    why = refused(params)
    ok(why is not None and "Toolset app" in why,
       "addon_update is refused without the token: %r" % (params.get("auth"),))

token = bridgeauth.issue()
why = refused({"path": "/definitely/not/here.zip", "auth": token})
ok(why is not None and "Toolset app" not in why,
   "with the right token it gets PAST the gate (and then fails on the missing file)")
ok("no such file" in why.lower(),
   "...failing for the honest reason, which proves the gate was the only thing stopping it")
bridgeauth.clear()

ok("addon_update" in server.BridgeServer.capabilities(),
   "addon_update is still advertised - the gate is not a feature removal")

# ------------------------------------------------------- the jiggle cache
#
# It stores six 3-float vectors per bone. It used to store them with pickle,
# in "//madi_jiggle_cache" - i.e. NEXT TO THE .BLEND - so a downloaded rig
# could carry its own payload and opening it was enough.

jiggle = importlib.import_module("madi_sec_pkg.jiggle")

ok("pickle" not in sys.modules.get("madi_sec_pkg.jiggle").__dict__,
   "jiggle.py no longer imports pickle at all")
with open(os.path.join(ADDON, "jiggle.py"), encoding="utf-8") as fh:
    source = fh.read()
ok("pickle.load" not in source.replace("# `pickle.load`", ""),
   "...and calls pickle.load nowhere")

for bad in [None, [], [1, 2], [1, 2, 3, 4], ["a", "b", "c"], [1, 2, None],
            [True, 1.0, 2.0], [float("nan"), 0.0, 0.0], [float("inf"), 0.0, 0.0]]:
    ok(jiggle._vec3(bad) is None, "a cache vector is refused: %r" % (bad,))
ok(jiggle._vec3([1, 2.5, -3]) == [1.0, 2.5, -3.0], "a real vector is accepted as floats")

# A crafted cache file must be inert. The classic pickle payload is a reduce
# that runs a command; here it is enough to prove the bytes are never unpickled.
cache_root = tempfile.mkdtemp(prefix="madi_jcache_")
scene = types.SimpleNamespace(madi_jiggle=types.SimpleNamespace(cache_dir=cache_root))
obj = types.SimpleNamespace(name="Cube")
state = types.SimpleNamespace(tip=[0, 0, 0], tip_prev=[0, 0, 0], tip_vel=[0, 0, 0],
                              root=[0, 0, 0], root_prev=[0, 0, 0], root_vel=[0, 0, 0])
target = types.SimpleNamespace(pb=types.SimpleNamespace(name="Bone"), state=state)

import pickle  # the test may use it; the add-on may not


class _Boom:
    def __reduce__(self):
        # If this is ever unpickled, `marker` appears on disk - which is the
        # same primitive that would run anything else.
        return (os.makedirs, (os.path.join(cache_root, "PWNED"),))


path = jiggle._cache_path(scene, obj, 1)
os.makedirs(os.path.dirname(path), exist_ok=True)
with open(path, "wb") as fh:
    pickle.dump({"sig": "x", "bones": {"Bone": _Boom()}}, fh)

loaded = jiggle._cache_load(scene, obj, 1, [target], "x")
ok(loaded is False, "a pickle left in the cache folder is NOT loaded")
ok(not os.path.exists(os.path.join(cache_root, "PWNED")),
   "...and its payload never ran - this is the whole point of the change")

# The real round trip still works.
ok(jiggle._cache_store(scene, obj, 2, [target], "sig1"), "a frame is still cached")
state.tip = [9, 9, 9]
ok(jiggle._cache_load(scene, obj, 2, [target], "sig1"), "...and read back")
ok(list(state.tip) == [0.0, 0.0, 0.0], "...restoring the values that were stored")
ok(not jiggle._cache_load(scene, obj, 2, [target], "other-sig"),
   "a settings change is still a cache miss")

# Corrupt / hostile JSON must be a miss, never a crash and never a half-restore.
for junk in ['{"v":1,"sig":"s","bones":{"Bone":"not-a-list"}}',
             '{"v":1,"sig":"s","bones":{"Bone":[[0,0,0]]}}',
             '{"v":99,"sig":"s","bones":{}}',
             '{"v":1,"sig":"s","bones":[]}',
             'not json at all', '']:
    p = jiggle._cache_path(scene, obj, 3)
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(junk)
    state.tip = [7, 7, 7]
    got = jiggle._cache_load(scene, obj, 3, [target], "s")
    ok(got is False and list(state.tip) == [7, 7, 7],
       "hostile cache JSON is a clean miss with nothing written back: %s" % junk[:28])

for f in FAIL:
    print("FAIL " + f)
# ⚠ The runner matches "^\d+ passed" at the START of a line (run_all.ps1), so a
# prefix here reads as "suite crashed" however green it is.
print("\n%d passed, %d failed" % (len(PASS), len(FAIL)), flush=True)
sys.exit(1 if FAIL else 0)
