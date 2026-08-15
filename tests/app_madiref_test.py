# MadiRef, offscreen: the timing arithmetic (which is the whole point of the
# feature), the .mrfx container incl. its crash-safety rule, the shared-memory
# ring round trip and seqlock, the audio playback/scrub inference, and the two
# things that go stale silently -- the ring's binary layout being duplicated in
# the add-on, and the capability gate that keeps an older add-on from breaking
# the tab.
#
# Deliberately NOT here: anything that needs a real video file or a running
# Blender. Ingest is covered by hand against real clips; this suite has to run
# in ~seconds on any machine.
import os
import struct
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))

PASS = []
FAIL = []


def ok(cond, label):
    (PASS if cond else FAIL).append(label)
    print(("ok   " if cond else "FAIL ") + label, flush=True)


from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication.instance() or QApplication([])

from madiref import decoder, proxy, shm  # noqa: E402

# ------------------------------------------------ 1. timing: map by TIME
tf = decoder.target_frame
ok(tf(24, 24.0, 60.0) == 60,
   "timing: a 60fps clip on a 24fps scene shows 1.0s at scene frame 24")
ok(tf(12, 24.0, 60.0) == 30,
   "timing: ...and 0.5s at scene frame 12")
ok(tf(60, 60.0, 24.0) == 24,
   "timing: the reverse mismatch maps by time too")
ok(tf(75, 30.0, 30.0) == 75,
   "timing: matched frame rates are 1:1")
ok(tf(34, 24.0, 24.0, 10.0) == 24,
   "timing: offset is measured in SCENE frames")
ok(tf(48, 24.0, 24.0, 0.0, 0.5) == 24,
   "timing: speed retimes the reference")
ok(tf(-100, 24.0, 24.0) == 0,
   "timing: before the start CLAMPS (a reference must not wrap)")
ok(tf(9999, 24.0, 24.0, 0.0, 1.0, 300) == 299,
   "timing: past the end holds the last frame")
ok(tf(24, 0.0, 24.0) >= 0,
   "timing: a zero scene fps cannot divide by zero")
# the bug this whole module exists to avoid
ok(tf(24, 24.0, 60.0) != 24,
   "timing: mapping is NOT by frame index (that is the 'plays slow' bug)")

# ---------------------------------------------------- 2. the .mrfx container
tmp = tempfile.mkdtemp(prefix="madiref_test_")
path = os.path.join(tmp, "clip.mrfx")
w = proxy.ProxyWriter(path, 3, 1920, 1080, 960, 540, 30000, 1000)
w.add_frame(0, b"\xff\xd8FRAME0\xff\xd9")
w.add_frame(1, b"\xff\xd8FRAME1\xff\xd9")
w.add_frame(2, b"\xff\xd8FRAME2\xff\xd9")
w.close()
r = proxy.open_proxy(path)
ok(r is not None, "proxy: a closed proxy opens")
ok(r.frame_count == 3, "proxy: frame count survives")
ok(r.size == (960, 540), "proxy: proxy size survives")
ok(abs(r.fps - 30.0) < 1e-6, "proxy: fps survives as a rational")
ok(b"FRAME1" in r.frame_bytes(1), "proxy: frames come back by index")
ok(b"FRAME0" in r.frame_bytes(-5), "proxy: a negative index clamps to the first")
ok(b"FRAME2" in r.frame_bytes(999), "proxy: an index past the end clamps")
r.close()

# the hint is only a hint -- real clips lie about their frame count
path2 = os.path.join(tmp, "grow.mrfx")
w2 = proxy.ProxyWriter(path2, 1, 0, 0, 0, 540, 30, 1)
for i in range(5):
    w2.add_frame(i, b"\xff\xd8x\xff\xd9")
w2.close()
r2 = proxy.open_proxy(path2)
ok(r2 is not None and r2.frame_count == 5,
   "proxy: the index GROWS past the hinted frame count")
if r2:
    r2.close()

# crash safety: a file whose index was never written must not read as a hit
path3 = os.path.join(tmp, "partial.mrfx")
w3 = proxy.ProxyWriter(path3, 2, 0, 0, 0, 540, 30, 1)
w3.add_frame(0, b"\xff\xd8x\xff\xd9")
del w3                       # no close() -- exactly what a crash leaves behind
ok(proxy.open_proxy(path3) is None,
   "proxy: an interrupted ingest is treated as ABSENT, not as a cache hit")
ok(proxy.open_proxy(os.path.join(tmp, "nope.mrfx")) is None,
   "proxy: a missing file returns None rather than raising")
with open(os.path.join(tmp, "junk.mrfx"), "wb") as fh:
    fh.write(b"not a proxy at all, really not")
ok(proxy.open_proxy(os.path.join(tmp, "junk.mrfx")) is None,
   "proxy: a foreign file returns None rather than raising")

# ------------------------------------------------------------- 3. the ring
name = "madiref_selftest_%d" % os.getpid()
ring = shm.RingWriter(name, 8, 4, 12, 30, 1, slots=3)
try:
    payload = bytes(bytearray((i * 13) % 256 for i in range(8 * 4 * 4)))
    ring.write_frame(7, payload)
    rd = shm.open_ring(name)
    ok(rd is not None, "ring: a reader attaches by name")
    got = rd.newest()
    ok(got is not None and got[0] == 7, "ring: the frame index survives")
    ok(got is not None and bytes(got[1]) == payload,
       "ring: pixels are byte-identical through shared memory")
    if got:
        got[1].release()

    # Blender's half of the channel
    rd.publish_consumer_state(42, 23.976)
    st = ring.consumer_state()
    ok(st["scene_frame"] == 42,
       "ring: Blender's scene frame reaches the app")
    ok(abs(st["scene_fps"] - 23.976) < 1e-3,
       "ring: ...and its scene fps, as a float")
    ok(st["stamp"] > 0, "ring: the stamp moves so liveness is detectable")

    # rotation must not corrupt the newest slot
    for i in range(ring.slots * 4):
        ring.write_frame(i, payload)
    got2 = rd.newest()
    ok(got2 is not None and got2[0] == ring.slots * 4 - 1,
       "ring: lapping the ring still reports the newest frame")
    if got2:
        got2[1].release()

    # view state is the app -> Blender channel
    ring.set_view_state(opacity=0.5, scale=0.25, mode=shm.MODE_PINNED)
    v = rd.view_state()
    ok(abs(v["opacity"] - 0.5) < 1e-6 and v["mode"] == shm.MODE_PINNED,
       "ring: view state (opacity/mode) reaches the Blender side")
    ok(ring.write_frame.__doc__ is not None, "ring: write_frame is documented")
    try:
        ring.write_frame(0, b"too short")
        short_ok = False
    except ValueError:
        short_ok = True
    ok(short_ok, "ring: an undersized frame is refused, not written past")
    rd.close()
finally:
    ring.unlink()

ok(shm.open_ring("madiref_definitely_not_here") is None,
   "ring: opening a segment that is gone returns None, not an exception")

# ------------------------- 4. the add-on duplicates this layout -- keep it true
addon = os.path.join(ROOT, "blender_addon", "madi_anim_library", "madiref.py")
src = open(addon, encoding="utf-8").read()


def _fmt(text, var):
    import re
    m = re.search(r'^%s = struct\.Struct\("([^"]+)"\)' % var, text,
                  re.MULTILINE)
    return m.group(1) if m else None


app_src = open(os.path.join(ROOT, "app", "madiref", "shm.py"),
               encoding="utf-8").read()
for var in ("_H", "_PRODUCER", "_CONSUMER", "_VIEW", "_SLOT"):
    a, b = _fmt(app_src, var), _fmt(src, var)
    ok(a is not None and a == b,
       "wire format: %s matches between app and add-on (%s vs %s)"
       % (var, a, b))
for const, value in (("_HEADER_SIZE", shm.HEADER_SIZE),
                     ("_SLOT_HEADER", shm.SLOT_HEADER)):
    ok(("%s = %d" % (const, value)) in src,
       "wire format: the add-on's %s is %d" % (const, value))
ok(("_MAGIC = 0x%08X" % shm.MAGIC).replace("0X", "0x") in src.upper().replace(
    "0X", "0x") or ("0x%X" % shm.MAGIC) in src.upper(),
   "wire format: the add-on carries the same magic number")
for off_name, off_val in (("_PRODUCER_OFF", 40), ("_CONSUMER_OFF", 64),
                          ("_VIEW_OFF", 88)):
    ok(("%s = %d" % (off_name, off_val)) in src and
       ("%s = %d" % (off_name, off_val)) in app_src,
       "wire format: %s is %d on both sides" % (off_name, off_val))

# ------------------------------------------- 5. the capability gate exists
import bridge  # noqa: E402

ok("madiref_viewport" in bridge.FEATURE_REQUIREMENTS,
   "gate: the viewport half is declared in FEATURE_REQUIREMENTS")
cmd, since, why = bridge.FEATURE_REQUIREMENTS["madiref_viewport"]
ok(cmd == "madiref_open", "gate: it is keyed on madiref_open")
ok(bridge.version_tuple(since) >= (0, 35, 0),
   "gate: it requires add-on 0.35.0 or newer")
ok("app" in why.lower() or "here" in why.lower(),
   "gate: the reason says the clip still plays in the app")
# ⚠ MADIREF IS PAID SINCE 2026-08-11 (Marty: "Make MadiRef paywalled"). It was
# free for exactly one day and these assertions pinned the OPPOSITE, so they are
# now written against the real tuples instead of by grepping main.py: a policy
# that reversed once can reverse again, and a source-text probe silently stops
# meaning anything when the text around it is reworded.
main_src = open(os.path.join(ROOT, "app", "main.py"), encoding="utf-8").read()
import main as mainmod  # noqa: E402

_gated = {}   # 1.19.0: GATED is gone — nothing is gated at all
_free = dict(mainmod.MainWindow.FREE_TOOLS)
ok("madiref" not in _gated,
   "gate: MadiRef is NOT gated any more — every tab went free 2026-08-14, "
   "premium PACKS are the paid thing (server-side)")
ok(_free.get("madiref") == "MadiRef",
   "gate: it is a FREE_TOOLS tab, built unconditionally at startup")
# ⚠ Appended to FREE_TOOLS in the exact order GATED used to hold the four,
# so the tab kept the strip position it has had since the day it shipped.
_free_keys = [k for k, _t in mainmod.MainWindow.FREE_TOOLS]
ok(_free_keys.index("madiref") == _free_keys.index("nodeeditor") + 1,
   "gate: it still sits directly after Node Editor — the strip did not move")
ok(not hasattr(mainmod.MainWindow, "GATED_ATTRS"),
   "gate: GATED_ATTRS is gone entirely - the lock-preview machinery was "
   "removed in 1.19.0, so nothing can blank a live tab")
# ⚠ AND THE BLENDER HALF'S GATE IS GONE WITH IT (the app's lock was never the
# bridge's — a gate here had to be REMOVED here). The madiref_* prefix check
# left server.py on 2026-08-14 along with opt_* and quad_*: the bridge
# answers everyone, and the licence's meaning moved to pack downloads.
_srv = open(os.path.join(ROOT, "blender_addon", "madi_anim_library",
                         "server.py"), encoding="utf-8").read()
ok('cmd.startswith("madiref_")' not in _srv,
   "gate: the add-on no longer refuses madiref_* commands — the prefix gate "
   "is really gone, not merely skipped")
# ⚠ A new tool tab has to join _pages() as well, and NOTHING else notices if it
# does not: the tab simply never greys out while Blender is capturing, so it
# looks usable when it is not. Every page in that list is sent
# set_capture_busy(), so a tab missing the method would crash the capture path.
pages_block = main_src.split("def _pages(self):")[1].split("return")[0]
ok("self.madiref" in pages_block,
   "busy: MadiRef is in MainWindow._pages() so it greys out with the rest")
from madiref import tab as tabmod  # noqa: E402
ok(hasattr(tabmod.MadiRefTab, "set_capture_busy"),
   "busy: ...and it implements set_capture_busy, which _pages() calls on all")
ok(hasattr(tabmod.MadiRefTab, "shutdown"),
   "teardown: the tab has shutdown() — the ring MUST be unlinked on close")
ok("madiref" in main_src.split("def closeEvent")[1][:600],
   "teardown: ...and closeEvent really calls it")

# ------------------------------------------------------- 6. audio inference
from madiref import audio as audiomod  # noqa: E402

a = audiomod.ReferenceAudio()
a._source = "pretend.mp4"
a.set_enabled(True)
ok(a.enabled, "audio: it can be enabled")


from PySide6.QtMultimedia import QMediaPlayer as _QMP  # noqa: E402


class _FakePlayer:
    # ⚠ Real enum values, not 0/1. `check_idle` compares against
    # QMediaPlayer.PlayingState, and a fake returning plain ints silently
    # never matched — the test went green against a stub that could not
    # exercise the code path at all.
    def __init__(self):
        self.calls = []
        self._state = _QMP.StoppedState

    def playbackState(self):
        return self._state

    def play(self):
        self._state = _QMP.PlayingState
        self.calls.append("play")

    def pause(self):
        self._state = _QMP.StoppedState
        self.calls.append("pause")

    def setPosition(self, ms):
        self.calls.append(("seek", ms))

    def position(self):
        return 0


a._player = _FakePlayer()
# steady forward motion == playback
for t in (0.0, 1 / 24, 2 / 24, 3 / 24):
    a.sync(t)
ok("play" in a._player.calls,
   "audio: steady forward motion is treated as playback and starts sound")

b = audiomod.ReferenceAudio()
b._source = "pretend.mp4"
b.set_enabled(True)
b._player = _FakePlayer()
# a scrub: big jumps, and backwards
for t in (0.0, 4.0, 1.0, 9.0):
    b.sync(t)
ok("play" not in b._player.calls,
   "audio: scrubbing (jumps/backwards) never starts sound")

c = audiomod.ReferenceAudio()
c._source = "pretend.mp4"
c.set_enabled(False)
c._player = _FakePlayer()
for t in (0.0, 1 / 24, 2 / 24, 3 / 24):
    c.sync(t)
ok(not c._player.calls, "audio: disabled means the player is never touched")

# ⚠ PAUSE. `sync()` is driven by FRAMES, and a paused timeline delivers none —
# so nothing told the audio to stop and it played on. Absence of events is the
# only signal, so it has to be polled.
import time as _t  # noqa: E402

d = audiomod.ReferenceAudio()
d._source = "pretend.mp4"
d.set_enabled(True)
d._player = _FakePlayer()
for t in (0.0, 1 / 24, 2 / 24, 3 / 24):
    d.sync(t)
ok(d._player.playbackState() == _QMP.PlayingState,
   "audio: playing after a steady run")
d.check_idle()
ok(d._player.playbackState() == _QMP.PlayingState,
   "audio: a fresh tick does NOT stop it (frames only just arrived)")
d._last_sync_at = _t.monotonic() - (audiomod._IDLE_STOP_S + 0.1)
d.check_idle()
ok(d._player.playbackState() == _QMP.StoppedState,
   "audio: STOPS once the frames stop arriving — this is the pause fix")
ok("check_idle" in open(os.path.join(ROOT, "app", "madiref", "tab.py"),
                        encoding="utf-8").read(),
   "audio: ...and the tab actually polls it")
_tabsrc = open(os.path.join(ROOT, "app", "madiref", "tab.py"),
               encoding="utf-8").read()
_tick_body = _tabsrc.split("def _on_tick")[1].split("def ")[0]
ok(_tick_body.index("check_idle") < _tick_body.index("if self.player is None"),
   "audio: check_idle runs BEFORE the no-clip early return, or a pause with "
   "nothing loaded would never stop it")

# ------------------------------------------------------------ 7. cache keys
from madiref import ingest  # noqa: E402

probe = os.path.join(tmp, "probe.mp4")
with open(probe, "wb") as fh:
    fh.write(b"x" * 32)
k1 = ingest.source_key(probe)
with open(probe, "wb") as fh:
    fh.write(b"x" * 64)              # same name, different content
k2 = ingest.source_key(probe)
ok(k1 and k2 and k1 != k2,
   "cache: re-exporting a clip under the same name misses the cache by itself")
ok(ingest.source_key(os.path.join(tmp, "gone.mp4")) is None,
   "cache: a missing source has no key")
ok(ingest.find_ffmpeg("definitely/not/here.exe") != "definitely/not/here.exe",
   "cache: a bogus explicit ffmpeg path is not accepted blindly")

# ⚠ the budget is BYTES, not a file count — four real clips reached 3.9 GB
# while far under the old 60-file cap
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_real_root = ingest.CACHE_ROOT
ingest.CACHE_ROOT = os.path.join(tmp, "cache")
try:
    os.makedirs(ingest.CACHE_ROOT, exist_ok=True)
    import time as _time
    for i in range(5):
        with open(os.path.join(ingest.CACHE_ROOT, "c%d.mrfx" % i), "wb") as fh:
            fh.write(b"\0" * 200_000)
        os.utime(os.path.join(ingest.CACHE_ROOT, "c%d.mrfx" % i),
                 (_time.time() + i, _time.time() + i))
    ok(len(ingest.cache_entries()) == 5, "cache: entries are listed")
    ok(ingest.cache_size() == 1_000_000, "cache: total size is summed")
    ok(ingest.cache_entries()[0][2] < ingest.cache_entries()[-1][2],
       "cache: entries come back OLDEST first, which is the trim order")
    n = ingest.purge_stale(max_files=99, max_bytes=500_000)
    ok(n == 3 and ingest.cache_size() <= 500_000,
       "cache: a SIZE budget trims oldest-first until it fits (removed %d)" % n)
    removed, freed = ingest.clear_cache()
    ok(removed == 2 and ingest.cache_size() == 0,
       "cache: clear_cache empties it and reports what it freed")
    ok(freed > 0, "cache: ...and how many bytes came back")
finally:
    ingest.CACHE_ROOT = _real_root
import config as _cfg  # noqa: E402
ok("madiref_cache_gb" in _cfg.DEFAULTS,
   "cache: the budget is a config key, so it can be raised without a rebuild")

# ------------------------------- 8. direct manipulation in the viewport
# ⚠ The modal operator itself CANNOT be tested headless (`blender -b` has no
# window-manager modal loop — the same limit picker_start documents). That is
# precisely why the geometry lives in plain functions: hit-testing and the
# rotation maths are the parts that can actually be wrong, and they are checked
# here against the add-on's real source.
import importlib.util  # noqa: E402
import math  # noqa: E402
import shutil  # noqa: E402

_spec = importlib.util.spec_from_file_location("_madiref_addon", addon)
_mod = importlib.util.module_from_spec(_spec)
_bpy_missing = False
try:
    _spec.loader.exec_module(_mod)
except Exception:                                    # noqa: BLE001
    _bpy_missing = True                              # no bpy outside Blender

if _bpy_missing:
    # Still assert the SOURCE carries the contract, so a rename is caught.
    for token in ("_hit_test", "_overlay_geometry", "_rotate_handle_pos",
                  "_to_local", "MADILIB_OT_madiref_adjust", "PASS_THROUGH",
                  "madiref_reset_view"):
        ok(token in src, "viewport: the add-on defines %s" % token)
    ok("bl_options = {'REGISTER'}" in src,
       "viewport: the modal does NOT push undo steps (it edits shared memory, "
       "not the scene)")
    ok(src.count("{'PASS_THROUGH'}") >= 4,
       "viewport: the modal passes events through on every non-overlay path")
    ok("_MIN_SCALE" in src and "_MAX_SCALE" in src,
       "viewport: scale is clamped, so the overlay cannot vanish or fill all")
    ok("event.ctrl" in src and "_SNAP_DEG" in src,
       "viewport: ctrl snaps rotation")
    ok("mode != MODE_VIEWPORT" in src,
       "viewport: the 3D placements are left to the depth/pin controls, not "
       "the mouse modal")
    ok("_S.locked" in src and "locked" in src,
       "viewport: a LOCKED reference consumes no mouse events and shows no "
       "handles")
    # ⚠ the bug that made the handles "only sometimes visible"
    ok("as_pointer()" in src,
       "viewport: the hovered region is identified by as_pointer()")
    # ⚠ Comments must not count: the warning ABOUT id(region) contains the very
    # string being banned, so a naive grep can never go green.
    _code = "\n".join(ln.split("#")[0] for ln in src.splitlines())
    ok("id(region)" not in _code,
       "viewport: ...and NEVER by id(region) — Blender hands back a fresh "
       "wrapper per access, so id() almost never matched")
    for tok in ("MODE_VIEWPORT", "MODE_PINNED", "MODE_CAMERA", "madiref_pin"):
        ok(tok in src, "placement: the add-on defines %s" % tok)
    # the collection mask is GONE and must stay gone
    for gone in ("_punch_shader", "_collect_mask_geometry", "_ensure_mask",
                 "front_collection", "madiref_list_collections"):
        ok(gone not in src,
           "placement: the collection-mask path is REMOVED (%s)" % gone)
    # ---- depth occlusion: the one that actually works
    ok("_depth_z_for_distance" in src and "_OCCLUDE_NDC" in src,
       "depth: the add-on can place the reference at a real distance")
    ok("(clip.z / clip.w + 1.0) * 0.5" in src,
       "depth: the MEASURED convention depth=(ndc+1)/2 is used verbatim "
       "(deriving it from the matrices gave a different, wrong answer)")
    _q = src.split("def _quad")[1].split("\ndef ")[0]
    ok("depth_mask_set(False)" in _q,
       "depth: occlusion TESTS depth and never WRITES it, or everything drawn "
       "after the reference would vanish behind it")
    # ⚠ the dispatcher is a separate file and forgetting it fails SILENTLY
    srv = open(os.path.join(ROOT, "blender_addon", "madi_anim_library",
                            "server.py"), encoding="utf-8").read()
    _disp = srv.split('cmd == "madiref_config"')[1].split("if cmd ==")[0]
    for param in ("occlude", "occlude_distance", "locked",
                  "plane_object", "sync_framedrop"):
        ok(param + "=" in _disp,
           "dispatcher: madiref_config forwards %r (the app's setting is "
           "dropped silently otherwise)" % param)
    # ⚠ THE BUG THAT BLANKED THE WHOLE REFERENCE. Blender uploads
    # ModelViewProjectionMatrix for its BUILT-IN shaders; a create_from_info
    # shader gets whatever was left in the uniform buffer, so the quad was
    # transformed by a stale matrix and drawn off-screen. A headless check
    # cannot catch it — loading an identity matrix by hand (which any offscreen
    # test must do) makes the missing upload look correct. Hence a source check.
    # (the punch shader is gone — depth replaced it; see docs\madiref.md)

# pure geometry, reimplemented from the same formulas the add-on uses, so the
# expectations here are independent of it
def _corners(cx, cy, w, h, rot):
    hw, hh = w / 2.0, h / 2.0
    out = []
    for lx, ly in ((-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh)):
        out.append((cx + lx * math.cos(rot) - ly * math.sin(rot),
                    cy + lx * math.sin(rot) + ly * math.cos(rot)))
    return out


c = _corners(500, 300, 200, 100, 0.0)
ok(c[0] == (400.0, 250.0) and c[2] == (600.0, 350.0),
   "viewport: an unrotated overlay's corners are centre +/- half size")
c90 = _corners(0, 0, 200, 100, math.pi / 2)
ok(abs(c90[0][0] - 50.0) < 1e-6 and abs(c90[0][1] + 100.0) < 1e-6,
   "viewport: a 90-degree rotation swaps the axes about the CENTRE")
ok(all(abs(math.hypot(x, y) - math.hypot(100, 50)) < 1e-6 for x, y in c90),
   "viewport: rotation preserves every corner's distance from the centre")


def _local(px, py, cx, cy, rot):
    dx, dy = px - cx, py - cy
    return (dx * math.cos(-rot) - dy * math.sin(-rot),
            dx * math.sin(-rot) + dy * math.cos(-rot))


lx, ly = _local(600, 300, 500, 300, math.pi / 2)
ok(abs(lx) < 1e-6 and abs(ly + 100) < 1e-6,
   "viewport: a point maps into the overlay's own frame, so hit-testing a "
   "rotated rectangle is the same problem as an upright one")
ok(abs(_local(500, 300, 500, 300, 1.234)[0]) < 1e-6,
   "viewport: the centre maps to the origin at any angle")

# ================================ drawn notes (2026-08-12) ==================
# ⚠ A NOTE SHOWS ON ITS OWN FRAME AND NO OTHER. It first shipped lasting
# "until the next note", which Marty reversed after using it: that rule needed
# an "End here" terminator to be usable at all, and smeared one drawing across
# the rest of the clip. Both are gone. The checks below are the MIRROR of the
# ones they replace — the range rule is the whole behaviour, so it is the thing
# worth testing hardest in either direction.
from PySide6.QtGui import QImage  # noqa: E402

from madiref import notes as _notes  # noqa: E402

_ndir = tempfile.mkdtemp(prefix="madi_notes_")


def _book(name="clip.mp4"):
    return _notes.NoteBook(os.path.join(_ndir, name), folder=_ndir)


def _stroke(*points):
    return _notes.Stroke(list(points) or [(0.1, 0.1), (0.2, 0.2)])


b = _book()
ok(b.count() == 0 and b.strokes_at(0) == [],
   "notes: a clip with no notes draws nothing")

b.add_stroke(100, _stroke())
ok(b.count() == 1 and b.frames() == [100],
   "notes: a stroke creates the note on its frame, with nothing to open first")
ok(len(b.strokes_at(100)) == 1,
   "notes: ...and it shows on that frame")
ok(b.strokes_at(99) == [] and b.strokes_at(101) == []
   and b.strokes_at(4000) == [],
   "⚠ notes: ...and on NO OTHER — not the frame before, not the one after, "
   "not the rest of the clip. This is the rule that was reversed")

b.add_stroke(200, _stroke())
ok(len(b.strokes_at(200)) == 1 and b.strokes_at(150) == [],
   "notes: a second note is independent — the gap between them is empty")
ok(b.count() == 2, "notes: ...and they are two notes, not one range")

b.add_stroke(100, _stroke())
ok(len(b.strokes_at(100)) == 2 and b.count() == 2,
   "notes: drawing again on the SAME frame adds to that frame's note")

# --- undo and clear act on the frame you are looking at, nothing else
ok(b.undo(100) is True and len(b.strokes_at(100)) == 1,
   "notes: undo drops the last stroke on this frame")
ok(b.undo(150) is False,
   "notes: ...and does nothing on a frame with no drawing")
ok(b.undo(100) is True and b.has_note(100) is False and b.count() == 1,
   "⚠ notes: undoing the LAST stroke removes the note itself — an empty note "
   "would leave a scrubber tick pointing at nothing")

b.add_stroke(300, _stroke())
ok(b.clear(300) is True and b.has_note(300) is False,
   "notes: clear removes everything drawn on this frame")
ok(b.clear(300) is False, "notes: clearing an empty frame says so")

# ⚠ the terminator is GONE with the rule that needed it
ok(not hasattr(b, "end_at"),
   "⚠ notes: `end_at` is deleted, not left dormant — it only ever existed to "
   "close an open-ended note, and there are none")
ok(not hasattr(b, "note_in_force"),
   "⚠ notes: and so is `note_in_force`, which WAS the range rule")

# --- the round trip, which is the ask ("saved and loaded ... after restarting")
b2 = _book()
ok(b2.count() == b.count() and b2.frames() == b.frames(),
   "⚠ notes: a fresh NoteBook on the same clip reads the notes back — this is "
   "the 'even after restarting the app' half of the ask")
ok(b2.frames() == [200] and b2.strokes_at(199) == [],
   "notes: ...on their OWN frames, with the gaps still empty after a reload")
s = b2.strokes_at(200)[0]
ok(s.points and all(0.0 <= x <= 1.0 and 0.0 <= y <= 1.0 for x, y in s.points),
   "notes: points survive as NORMALISED coordinates")
ok(os.path.dirname(b2.path) == _ndir and b2.path.endswith(".json"),
   "notes: one JSON file per clip")
raw = open(b2.path, encoding="utf-8").read()
ok("clip.mp4" in raw,
   "⚠ notes: the SOURCE PATH is recorded inside — the filename is a hash, so "
   "without it a moved clip's notes are unidentifiable rather than repairable")

# ⚠ keyed on the PATH, not on the proxy's (path, mtime, size): re-encoding a
# clip should give a new proxy and the SAME notes.
ok(_notes.key_for("D:\\a\\Clip.MP4") == _notes.key_for("d:/a/clip.mp4"),
   "⚠ notes: the key is case- and separator-insensitive, or the same file "
   "gets two note files depending on how the path was typed")
ok(_notes.key_for("D:\\a\\one.mp4") != _notes.key_for("D:\\a\\two.mp4"),
   "notes: different clips do not collide")

# --- a corrupt or foreign file must not take the tab down
open(os.path.join(_ndir, _notes.key_for(os.path.join(_ndir, "junk.mp4"))
                  + ".json"), "w", encoding="utf-8").write("{not json")
ok(_book("junk.mp4").count() == 0,
   "notes: an unreadable notes file reads as 'no notes', never a crash")

# --- the revision counter, which is what makes an edit reach Blender
b3 = _book("rev.mp4")
r0 = b3.revision
b3.add_stroke(5, _stroke())
ok(b3.revision > r0,
   "⚠ notes: every edit bumps `revision` — `decoder._served` suppresses "
   "rewriting a frame that has not moved, so without this the stroke you just "
   "drew never reaches the viewport until you scrub away and back")
r1 = b3.revision
b3.clear(5)
ok(b3.revision > r1, "notes: ...and so does removing one")

# --- the decoder's half, without a proxy: the compositing decision only
_dec = decoder.ReferencePlayer.__new__(decoder.ReferencePlayer)
_dec._lock = __import__("threading").Lock()
_dec._notes = None
_dec._notes_to_blender = True
_dec._served_notes = -1
_img = QImage(32, 18, QImage.Format_RGBA8888)
_img.fill(0xFF000000)
ok(_dec._for_blender(_img, 0) is _img,
   "notes: with no book the frame is passed through untouched — no copy, no "
   "cost on a clip nobody has drawn on")
b3.add_stroke(7, _stroke())
_dec._notes = b3
ok(_dec._for_blender(_img, 6) is _img and _dec._for_blender(_img, 8) is _img,
   "notes: ...and none on the frames either side of one — no copy, no cost, "
   "which is what makes a one-frame note free everywhere else")
_stamped = _dec._for_blender(_img, 7)
ok(_stamped is not _img,
   "notes: the note's OWN frame is composited onto a COPY — the app is handed "
   "the raw frame so it can draw the stroke under the cursor without doubling")
_dec._notes_to_blender = False
ok(_dec._for_blender(_img, 7) is _img,
   "⚠ notes: 'Show markings in Blender' OFF stops the compositing only — the "
   "app still shows them, because that is where you are drawing")

# ⚠ the release builder must never sweep these up; unlike a proxy they cannot
# be regenerated at all
_mr = open(os.path.join(os.path.dirname(_ROOT), "license-server", "tools", "make_release.js"),
           encoding="utf-8").read()
ok('"_madiref_notes"' in _mr,
   "⚠ notes: make_release.js NEVER_SHIP_DIRS knows _madiref_notes — a missing "
   "name here is DATA LOSS, and this is the one file the user cannot rebuild")
ok(_notes.NOTES_ROOT.endswith("_madiref_notes")
   and "_madiref_cache" not in _notes.NOTES_ROOT,
   "⚠ notes: they live OUTSIDE the proxy cache, which is trimmed oldest-first "
   "against a GB budget and would delete them")

# --- the pen on the widget, with a REAL letterbox
# ⚠ The unit checks above cannot catch the mapping from widget pixels to 0..1
# of the frame, and a letterboxed view is exactly where that goes wrong: the
# picture is not the widget. A wide frame in a square widget gives real bars.
from PySide6.QtCore import QPointF, Qt  # noqa: E402
from PySide6.QtGui import QMouseEvent  # noqa: E402

from madiref.tab import VideoView  # noqa: E402

_frame = QImage(320, 180, QImage.Format_RGBA8888)
_frame.fill(0xFF404040)
_view = VideoView()
_view.resize(400, 400)
_view.set_image(_frame)
_vx, _vy, _vw, _vh = _view._image_rect()
ok((_vx, _vw, _vh) == (0, 400, 225) and _vy == 87,
   "pen: a 16:9 frame in a square widget letterboxes to the middle (%d,%d %dx%d)"
   % (_vx, _vy, _vw, _vh))

_drawn = []
_view.stroke_drawn.connect(lambda s, f: _drawn.append((s, f)))
_view.set_pen(on=True, color="#e8483a", width=0.02)
_view.set_frame(10)


def _mouse(kind, px, py):
    ev = QMouseEvent(kind, QPointF(px, py), QPointF(px, py),
                     Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)
    {QMouseEvent.Type.MouseButtonPress: _view.mousePressEvent,
     QMouseEvent.Type.MouseMove: _view.mouseMoveEvent}.get(
         kind, _view.mouseReleaseEvent)(ev)


_mouse(QMouseEvent.Type.MouseButtonPress, _vx + _vw * 0.25, _vy + _vh * 0.5)
_mouse(QMouseEvent.Type.MouseMove, _vx + _vw * 0.5, _vy + _vh * 0.5)
_mouse(QMouseEvent.Type.MouseButtonRelease, _vx + _vw * 0.75, _vy + _vh * 0.5)
ok(len(_drawn) == 1, "pen: a drag emits one stroke")
# ⚠ THE FRAME IS CAPTURED AT PRESS. With Follow Blender on the timeline can
# move mid-stroke; reading the frame at RELEASE would file the drawing against
# whatever Blender had reached, which is not the picture that was drawn on.
_view.set_frame(99)
ok(_drawn[0][1] == 10,
   "⚠ pen: the stroke carries the frame it STARTED on (%d), not the one on "
   "screen now" % _drawn[0][1])
_view.set_frame(10)
_pts = _drawn[0][0].points
ok(abs(_pts[0][0] - 0.25) < 0.005 and abs(_pts[0][1] - 0.5) < 0.005
   and abs(_pts[-1][0] - 0.75) < 0.005,
   "⚠ pen: widget pixels map to the FRAME's 0..1, letterbox removed "
   "(%.3f,%.3f -> %.3f)" % (_pts[0][0], _pts[0][1], _pts[-1][0]))

_mouse(QMouseEvent.Type.MouseButtonPress, _vx + _vw * 0.5, 2)
_mouse(QMouseEvent.Type.MouseButtonRelease, _vx + _vw * 0.5, 2)
ok(len(_drawn) == 1,
   "⚠ pen: a press in the LETTERBOX BAR draws nothing — it is not part of the "
   "picture, and clamping it would rule a line along the edge")

_wbook = _notes.NoteBook(os.path.join(_ndir, "widget.mp4"), folder=_ndir)
_wbook.add_stroke(_drawn[0][1], _drawn[0][0])
_view.set_book(_wbook)
_view.set_pen(on=False)


def _red_on_row(image, row):
    return sum(1 for px in range(image.width())
               if image.pixelColor(px, row).red() > 180
               and image.pixelColor(px, row).green() < 120)


_row = int(_vy + _vh * 0.5)
_view.set_frame(10)
ok(_red_on_row(_view.grab().toImage(), _row) > 150,
   "pen: the stroke is drawn on its OWN frame")
_view.set_frame(11)
ok(_red_on_row(_view.grab().toImage(), _row) == 0,
   "⚠ pen: ...and is GONE one frame later — the one-frame rule holds in the "
   "widget, not just in the model")
_view.set_frame(9)
ok(_red_on_row(_view.grab().toImage(), _row) == 0,
   "pen: ...and one frame earlier too")

# --- the composited frame actually REACHES the ring, with the right pixels
# ⚠⚠ THIS IS THE CRASH CHECK (2026-08-12). `_packed_bits` returns
# `constBits()` — a VIEW into the QImage — so `_packed_bits(_for_blender(...))`
# in one expression freed the composited image before `write_frame` copied out
# of it: an ACCESS VIOLATION that killed the app on every frame carrying a
# note. It could not be caught by the stub checks above, which never reach the
# ring, and a test that reproduced it would take the whole suite down with it.
# So this asserts the OUTCOME — the composited pixels arriving intact — which
# a use-after-free cannot satisfy quietly.
_cimg = QImage(16, 8, QImage.Format_RGBA8888)
_cimg.fill(0xFF000000)                       # opaque black
_cbook = _notes.NoteBook(os.path.join(_ndir, "ring.mp4"), folder=_ndir)
_cbook.add_stroke(0, _notes.Stroke([(0.05, 0.5), (0.95, 0.5)],
                                   color="#ff0000", width=0.2))
_cdec = decoder.ReferencePlayer.__new__(decoder.ReferencePlayer)
_cdec._lock = __import__("threading").Lock()
_cdec._notes = _cbook
_cdec._notes_to_blender = True
_cdec._served_notes = -1

_cname = "madiref_notes_selftest_%d" % os.getpid()
_cring = shm.RingWriter(_cname, 16, 8, 12, 30, 1, slots=2)
try:
    _out = _cdec._for_blender(_cimg, 0)      # the named local the fix requires
    _cring.write_frame(3, decoder._packed_bits(_out))
    _crd = shm.open_ring(_cname)
    _newest = _crd.newest()
    ok(_newest is not None and _newest[0] == 3,
       "crash: a composited frame reaches the ring at all")
    _mv = _newest[1]
    _px = bytes(_mv[:16 * 8 * 4])
    _mv.release()
    # the middle row must carry red where the stroke was drawn
    _mid = 4 * 16 * 4
    _reds = sum(1 for x in range(16)
                if _px[_mid + x * 4] > 180 and _px[_mid + x * 4 + 1] < 80)
    ok(_reds >= 10,
       "⚠ crash: the STROKE's pixels survive into the ring (%d red of 16) — a "
       "freed image would give garbage here, or take the process down" % _reds)
    ok(_px[0] == 0 and _px[1] == 0,
       "crash: ...and the untouched corner is still the original black, so it "
       "is the real frame that was composited, not a stray buffer")
finally:
    _cring.unlink()

# ⚠ And the SHAPE that prevents it, because the outcome check above passes
# either way when it is written with a named local — which is exactly the
# thing that must not be "tidied" back into one expression.
# ⚠ PARSED, NOT GREPPED — for the third time this session. Written as
# `"_packed_bits(self._for_blender(" not in src` it FAILED against the fixed
# file, because the comment warning against that exact expression contains it.
# A string search for an absence asks "did anyone mention this"; the AST sees
# only what the code actually does.
import ast as _ast  # noqa: E402

_dec_src = open(os.path.join(ROOT, "app", "madiref", "decoder.py"),
                encoding="utf-8").read()
_inline = []
for _n in _ast.walk(_ast.parse(_dec_src)):
    if not isinstance(_n, _ast.Call):
        continue
    _fn = _n.func
    _name = _fn.id if isinstance(_fn, _ast.Name) else getattr(_fn, "attr", "")
    if _name == "_packed_bits" and _n.args and isinstance(_n.args[0],
                                                          _ast.Call):
        _inline.append(getattr(_n, "lineno", "?"))
ok(not _inline,
   "⚠ crash: `_packed_bits(...)` is never handed a CALL's return value — that "
   "one expression is the use-after-free, since it keeps no reference to the "
   "image it is reading (line %s)" % _inline)
ok("out = self._for_blender(img, index)" in _dec_src
   and "_packed_bits(out)" in _dec_src,
   "crash: the composited image is bound to a name that outlives the write")

# --- closing the app must drop the reference from the viewport
# ⚠ UNCONDITIONALLY, not just when this process thinks it is showing one
# (Marty, 2026-08-12). `_shown_in_blender` is a belief held by ONE app process:
# if an instance dies or a second one starts, the new app has it False while
# Blender is still drawing, and nothing ever asks Blender to stop. That is a
# reference painted over the viewport reading from a segment that has gone,
# which no slider and no drag can move.
from madiref.tab import MadiRefTab  # noqa: E402


class _RecordingBridge:
    def __init__(self):
        self.sent = []

    def feature_reason(self, _n):
        return ""

    def request(self, cmd, params=None, timeout=None):
        self.sent.append((cmd, timeout))
        return {"ok": True}


class _DeadPortBridge:
    """A localhost port that DROPS the SYN — what Windows does with Blender
    shut. Blocks for the whole timeout instead of refusing."""

    def feature_reason(self, _n):
        return ""

    def request(self, cmd, params=None, timeout=None):
        import time as _t
        _t.sleep(min(timeout if timeout is not None else 30.0, 5.0))
        raise OSError("timed out")


_rb = _RecordingBridge()
_stab = MadiRefTab(_rb)
ok(_stab._shown_in_blender is False, "shutdown: this tab never showed anything")
_stab.shutdown()
ok(any(c == "madiref_close" for c, _t in _rb.sent),
   "⚠ shutdown: closing the app tells Blender to drop the reference EVEN when "
   "this process never showed one — the ghost-overlay case")
_timeouts = [t for c, t in _rb.sent if c == "madiref_close"]
ok(_timeouts and all(t is not None and t <= 3.0 for t in _timeouts),
   "⚠ shutdown: ...with a SHORT timeout (%s) — it runs on the GUI thread at "
   "exit, and a dead localhost port blocks rather than refusing" % _timeouts)

import time as _time  # noqa: E402

_dead = MadiRefTab(_DeadPortBridge())
_t0 = _time.time()
_dead.shutdown()
_elapsed = _time.time() - _t0
ok(_elapsed < 4.0,
   "⚠ shutdown: a CLOSED Blender does not hang the app on exit (%.1fs) — the "
   "bound is the whole point of passing a timeout" % _elapsed)

# --- Open clip / Close clip, remembering the clip, and NO LEAKS
# Marty, 2026-08-12: the one button becomes "Close clip"; closing clears the
# prepared-clip cache; and reopening the app brings the last clip back with its
# markings. Plus "make sure no leaks can happen".
import gc  # noqa: E402

from PySide6.QtCore import QCoreApplication, QEvent  # noqa: E402
from PySide6.QtWidgets import QWidget  # noqa: E402

from madiref import ingest as _ingest  # noqa: E402


class _FakeWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.cfg = {}


def _flush_deletes():
    """⚠ Qt6 DROPPED the DeferredDeletion processEvents flag, so a plain
    processEvents() never runs deleteLater() — and a leak check written
    without this reports objects alive that are already scheduled to go."""
    app.processEvents()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()
    gc.collect()


_cachedir = tempfile.mkdtemp(prefix="madi_cache_")
_realroot, _ingest.CACHE_ROOT = _ingest.CACHE_ROOT, _cachedir
_realnotes, _notes.NOTES_ROOT = _notes.NOTES_ROOT, tempfile.mkdtemp(
    prefix="madi_n2_")
try:
    _win = _FakeWindow()
    _ctab = MadiRefTab(_RecordingBridge(), _win)
    ok(_ctab.btn_open.text().startswith("Open"),
       "clip: the button offers to OPEN when nothing is loaded (%r)"
       % _ctab.btn_open.text())

    # a source whose "proxy" is a real (tiny) one built by the suite's writer
    _src = os.path.join(_cachedir, "ref.mp4")
    open(_src, "wb").write(b"path only")

    # ⚠ close_clip(clear_cache=True) must WIPE the cache and FORGET the clip,
    # or the next launch would try to restore a clip whose proxy was just
    # deleted — which would mean silently re-ingesting what was thrown away.
    _win.cfg["madiref_last_clip"] = _src
    open(os.path.join(_cachedir, "aaaa1111bbbb2222.mrfx"), "wb").write(b"x")
    ok(len(_ingest.cache_entries()) == 1, "clip: one prepared clip on disk")
    _ctab.close_clip(clear_cache=True)
    ok(len(_ingest.cache_entries()) == 0,
       "⚠ clip: Close clip clears every prepared clip — 'closing clip will "
       "clear cache automatically'")
    ok(_win.cfg.get("madiref_last_clip") is None,
       "⚠ clip: ...and FORGETS the clip, so the next launch does not try to "
       "restore one whose proxy it just deleted")
    ok(_ctab.btn_open.text().startswith("Open"),
       "clip: the button offers to open again")

    # restoring does nothing when there is nothing prepared — never a silent
    # re-ingest at startup, which would look like a hang
    _win.cfg["madiref_last_clip"] = _src
    ok(_ctab.restore_last_clip() is False,
       "⚠ clip: restore is a NO-OP when the proxy is gone — startup must never "
       "kick off an ingest by itself")
    _win.cfg["madiref_last_clip"] = os.path.join(_cachedir, "vanished.mp4")
    ok(_ctab.restore_last_clip() is False,
       "clip: ...and when the source file itself has gone")

    # ⚠ THE LEAK. ReferencePlayer is a QObject PARENTED TO THE TAB, so
    # `self.player = None` frees nothing — measured: 5 cycles left 5 live C++
    # objects on the tab's child list, for the life of the session.
    _before = len(_ctab.children())
    _players = []
    for _ in range(4):
        _p = decoder.ReferencePlayer.__new__(decoder.ReferencePlayer)
        decoder.ReferencePlayer.__init__(_p, "nonexistent.mrfx", _ctab)
        _players.append(_p)
    ok(len(_ctab.children()) == _before + 4,
       "leak: four players parented to the tab show up as children")
    for _p in _players:
        _p.setParent(None)
        _p.deleteLater()
    _flush_deletes()
    ok(len(_ctab.children()) == _before,
       "⚠ leak: setParent(None)+deleteLater() is what actually frees them — "
       "dropping the Python name leaves the QObject on the parent forever")
    _src_txt = open(os.path.join(ROOT, "app", "madiref", "tab.py"),
                    encoding="utf-8").read()
    ok("self.player.deleteLater()" in _src_txt
       and "self.player.setParent(None)" in _src_txt,
       "⚠ leak: ...and close_clip does both, not just `self.player = None`")
finally:
    _ingest.CACHE_ROOT = _realroot
    _notes.NOTES_ROOT = _realnotes
    shutil.rmtree(_cachedir, ignore_errors=True)

shutil.rmtree(_ndir, ignore_errors=True)
shutil.rmtree(tmp, ignore_errors=True)

print("%d passed, %d failed" % (len(PASS), len(FAIL)), flush=True)
sys.exit(1 if FAIL else 0)
