# Self-update: version comparison (phase 0), and later the signed manifest,
# the licence gate and the swap. Offscreen; touches nothing real.
#
#   app\.venv\Scripts\python.exe tests\app_updater_test.py
#
# Version comparison is not busywork here: it IS the anti-rollback rule. A
# manifest served from disk carries no nonce, so "never install something that
# is not strictly newer" is the only replay defence the channel has, and it is
# only as good as this function.
import base64
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_SANDBOX = tempfile.mkdtemp(prefix="madi_upd_")
os.environ["LOCALAPPDATA"] = _SANDBOX

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

sys.path.insert(0, os.path.join(_ROOT, "app"))

import version  # noqa: E402

PASS = []
FAIL = []


def ok(cond, what):
    (PASS if cond else FAIL).append(what)


# ------------------------------------------------------------------ parsing

ok(version.parse("1.0.0") == (1, 0, 0), "parse: the ordinary case")
ok(version.parse("12.34.56") == (12, 34, 56), "parse: multi-digit parts")
ok(version.parse(" 1.2.3 ") == (1, 2, 3), "parse: surrounding whitespace is tolerated")
ok(version.parse("1.02.0") == (1, 2, 0), "parse: a leading zero is just a number")

for bad, why in [
    ("1.2", "two parts is not a version"),
    ("1.2.3.4", "four parts is not a version"),
    ("", "an empty string"),
    ("...", "separators with nothing between them"),
    ("1.2.x", "a non-numeric part"),
    ("v1.2.3", "a leading v - common, and still not our format"),
    ("1.2.3-beta", "a pre-release suffix (deliberately unsupported)"),
    ("-1.2.3", "a negative part"),
    ("1.2.3 4", "an embedded space"),
    ("1.\uff12.3", "a FULLWIDTH digit - isdigit() says yes and int() accepts it"),
    ("\u0661.2.3", "an Arabic-Indic digit, same trap"),
    (None, "None"),
    (123, "a number instead of a string"),
    (b"1.2.3", "bytes"),
    (["1", "2", "3"], "a list"),
]:
    ok(version.parse(bad) is None, "parse: rejects %s" % why)


# --------------------------------------------------------------- comparison

ok(version.is_newer("1.0.1", "1.0.0"), "newer: a patch bump is newer")
ok(version.is_newer("1.1.0", "1.0.9"), "newer: minor outranks patch")
ok(version.is_newer("2.0.0", "1.99.99"), "newer: major outranks everything")
ok(version.is_newer("1.10.0", "1.9.0"),
   "newer: 1.10.0 beats 1.9.0 - compared as NUMBERS, not as text")
ok(version.is_newer("1.0.10", "1.0.9"), "newer: and the same in the patch field")

ok(not version.is_newer("1.0.0", "1.0.0"), "newer: equal is NOT newer (no pointless reinstall)")
ok(not version.is_newer("1.0.0", "1.0.1"), "newer: older is not newer")
ok(not version.is_newer("1.0.0", "2.0.0"), "newer: a major downgrade is refused")
ok(not version.is_newer("1.02.0", "1.2.0"),
   "newer: a cosmetically different spelling of the same version is not an update")

# The anti-rollback property, stated as its own check because it is the whole
# point: a manifest for an OLDER build carries a perfectly valid signature.
# Signature checking cannot catch that; only this can.
ok(not version.is_newer("0.9.0", version.APP_VERSION),
   "rollback: a validly signed OLDER release is still refused")

# Unparseable on either side must mean "do nothing" - never "older" (which
# would offer a downgrade) and never "newer" (which would install anything).
for cand, cur, why in [
    ("garbage", "1.0.0", "an unreadable candidate"),
    ("1.0.1", "garbage", "an unreadable current version"),
    ("garbage", "garbage", "both unreadable"),
    (None, "1.0.0", "no candidate at all"),
    ("1.0.1", None, "no current version"),
    ("999", "1.0.0", "a bare number that is not our format"),
]:
    ok(not version.is_newer(cand, cur), "newer: %s means do nothing" % why)


# ------------------------------------------------------- the shipped version

ok(version.parse(version.APP_VERSION) is not None,
   "app: APP_VERSION is itself parseable - the updater compares against it")
ok(isinstance(version.APP_VERSION, str) and version.APP_VERSION,
   "app: APP_VERSION is a non-empty string")

# The app must be able to state its own version somewhere a user can find it,
# or "you are on 1.0.0" is a sentence nobody can act on.
import main as mainmod  # noqa: E402

ok(getattr(mainmod, "version", None) is version,
   "app: main.py uses the shared version module rather than a second constant")

import config  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

config.CONFIG_PATH = os.path.join(_SANDBOX, "config.json")
_app = QApplication.instance() or QApplication([])
_dlg = mainmod.LibrarySettingsDialog(None, {})
ok(version.APP_VERSION in _dlg.version_label.text(),
   "app: Library Settings shows the running version")
ok("add-on" in _dlg.addon_label.text().lower(),
   "app: and a row for the Blender add-on, with its own install controls")
ok(_dlg.btn_install_addon is not None and _dlg.btn_save_addon is not None,
   "app: both routes in - install into a running Blender, or save the zip")
# Membership, NOT set equality: pinning the exact key set makes every new
# setting fail this suite for no real reason (adding "dev_edit" did exactly
# that on 2026-08-04 — same lesson as never pinning a version number).
_vals = _dlg.values()
ok("auto_update" in _vals,
   "app: the settings dialog now carries the auto-update switch too")
ok(set(_vals) <= set(config.DEFAULTS),
   "app: and every switch it saves is a real config key (got %s)" % sorted(_vals))
ok(_dlg.chk_update.isChecked(),
   "app: checking for updates is ON by default - an unpatched paying customer is the worse failure")

import config as configmod  # noqa: E402

ok(configmod.DEFAULTS["auto_update"] is True, "app: and the default says so")


# ------------------------------------------------------------- signed offers
# A throwaway Ed25519 key, so the suite can mint offers without a server. The
# shipped client only ever VERIFIES.

import licensing  # noqa: E402
from licensing import ed25519  # noqa: E402
from licensing import manager as licmgr  # noqa: E402


def _clamp(h):
    a = bytearray(h[:32])
    a[0] &= 248
    a[31] &= 127
    a[31] |= 64
    return int.from_bytes(a, "little")


def _compress(point):
    zinv = pow(point[2], ed25519.P - 2, ed25519.P)
    x = point[0] * zinv % ed25519.P
    y = point[1] * zinv % ed25519.P
    return (y | ((x & 1) << 255)).to_bytes(32, "little")


def keypair(seed):
    h = hashlib.sha512(seed).digest()
    a = _clamp(h)
    return seed, _compress(ed25519._mul(ed25519.BASE, a)), h[32:], a


def sign(secret, message):
    _seed, public, prefix, a = secret
    r = int.from_bytes(hashlib.sha512(prefix + message).digest(), "little") % ed25519.L
    R = _compress(ed25519._mul(ed25519.BASE, r))
    k = int.from_bytes(hashlib.sha512(R + public + message).digest(), "little") % ed25519.L
    return R + ((r + k * a) % ed25519.L).to_bytes(32, "little")


KEY = keypair(b"\x33" * 32)
licmgr.PUBLIC_KEY = base64.b64encode(KEY[1]).decode()

import updater  # noqa: E402
from updater import offer as offer_mod  # noqa: E402
from updater import swap  # noqa: E402

NONCE = "n-update-12345678"
FILE_A = {"path": "MadihsonNSFW Toolset.exe", "sha256": "a" * 64, "size": 10}


def make_offer(key=KEY, **over):
    now = int(time.time())
    payload = {
        "v": 1, "kind": "update", "sub": "ent_test", "nonce": NONCE,
        "iat": now, "not_after": now + 6 * 3600,
        "version": "1.0.1", "min_version": "0.0.0", "released": now,
        "app": [dict(FILE_A)], "addon": None,
    }
    payload.update(over)
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return text, base64.b64encode(sign(key, text.encode())).decode()


def parse(**over):
    p, s = make_offer(**over)
    return offer_mod.parse(p, s, NONCE, "1.0.0")


got, why = parse()
ok(got is not None and got.version == "1.0.1" and why is None,
   "offer: a genuine offer is accepted")
ok(got.files[0]["sha256"] == "a" * 64 and got.total_bytes == 10,
   "offer: it carries the hashes every downloaded byte is then checked against")

_p, _s = make_offer()
ok(offer_mod.parse(_p, _s, "a-different-nonce", "1.0.0")[1] == "unverified",
   "offer: an offer carrying somebody else's nonce is refused (replay defence)")
ok(offer_mod.parse(_p.replace("a" * 64, "b" * 64), _s, NONCE, "1.0.0")[1] == "unverified",
   "offer: swapping a HASH in the payload breaks the signature - that is the RCE boundary")
ok(offer_mod.parse(_p, base64.b64encode(b"\x00" * 64).decode(), NONCE, "1.0.0")[1] == "unverified",
   "offer: a forged signature is refused")
_wrong = keypair(b"\x44" * 32)
_p2, _s2 = make_offer(key=_wrong)
ok(offer_mod.parse(_p2, _s2, NONCE, "1.0.0")[1] == "unverified",
   "offer: signed by the WRONG key - a fake server cannot offer us anything")

ok(parse(kind="licence")[1] == "not_an_update",
   "offer: a LICENCE blob is correctly signed too, and must not pass as an offer")
ok(parse(kind=None)[1] == "not_an_update", "offer: and neither does one with no kind")
ok(parse(v=2)[1] == "unknown_format", "offer: an unknown payload version")
ok(parse(not_after=int(time.time()) - 60)[1] == "expired",
   "offer: an expired offer is not acted on - it is a live answer, not a file")

ok(parse(version="1.0.0")[1] == "not_newer", "offer: the version we already run")
ok(parse(version="0.9.9")[1] == "not_newer",
   "offer: ANTI-ROLLBACK - an older release is signed just as validly, and refused anyway")
ok(parse(version="nonsense")[1] == "not_newer", "offer: an unparseable version")
ok(parse(min_version="1.5.0")[1] == "too_old",
   "offer: a release this build is too old to take directly says so")
ok(parse(min_version="1.0.0")[0] is not None,
   "offer: min_version equal to the running version is fine")

for bad, why_ in [
    ("../../Windows/System32/evil.dll", "climbing out of the app folder"),
    ("a/../../b", "a .. buried in the middle"),
    ("/etc/passwd", "an absolute POSIX path"),
    ("C:/Windows/evil.dll", "an absolute Windows path"),
    ("_internal\\qt.dll", "a backslash separator"),
    ("evil.dll.", "a trailing dot, which Windows silently strips"),
    ("evil.dll ", "a trailing space, same trick"),
    ("bad\x00name", "an embedded control character"),
    ("", "an empty path"),
    (None, "no path at all"),
]:
    ok(parse(app=[{**FILE_A, "path": bad}])[1] == "bad_file_entry",
       "offer: refuses %s" % why_)

ok(parse(app=[dict(FILE_A), dict(FILE_A)])[1] == "bad_file_entry",
   "offer: the same path listed twice")
ok(parse(app=[{**FILE_A, "sha256": "nope"}])[1] == "bad_file_entry",
   "offer: a hash that is not 64 hex characters")
ok(parse(app=[{**FILE_A, "size": -1}])[1] == "bad_file_entry", "offer: a negative size")
ok(parse(app=[{**FILE_A, "size": True}])[1] == "bad_file_entry",
   "offer: a bool is not a size (isinstance(True, int) is True in Python)")
ok(parse(app=[])[1] == "no_files", "offer: an empty file list")
ok(parse(app=[dict(FILE_A)] * 5000)[1] in ("too_many_files", "bad_file_entry"),
   "offer: an absurd number of files")
ok(parse(app=[{**FILE_A, "size": 3 * 1024 ** 3}])[1] == "too_large",
   "offer: more bytes than any real release")

_addon = {"version": "0.6.1", "path": "madi_anim_library-0.6.1.zip",
          "sha256": "c" * 64, "size": 100}
got, why = parse(addon=dict(_addon))
ok(got is not None and got.addon["version"] == "0.6.1",
   "offer: an add-on rides along in the same release")
ok(parse(addon={**_addon, "path": "sub/dir.zip"})[1] == "bad_addon",
   "offer: the add-on must be a bare file name, never a path")
ok(parse(addon={**_addon, "version": "x"})[1] == "bad_addon", "offer: with a real version")
ok(parse(addon={**_addon, "sha256": "z" * 64})[1] == "bad_addon", "offer: and a real hash")

ok(offer_mod.safe_relpath("_internal/PySide6/Qt6Core.dll") == "_internal/PySide6/Qt6Core.dll",
   "offer: an ordinary nested path is fine")

# ⚠ ONLY THE POSITIVE CASE WAS PINNED HERE UNTIL 2026-08-06 - the one direction
# that cannot hurt anybody. These are the ones that matter: this process writes
# whatever the list says to disk, and os.path.join(root, "C:\\x") silently
# DISCARDS root, so an absolute path is not a traversal attempt that fails, it
# is a traversal attempt that works. The server validates these on import too;
# this side re-checks because a signature proves origin, not intent, and a
# server that has been compromised must not be able to talk us into it.
for hostile in [
    "../evil.dll", "../../windows/system32/evil.dll", "a/../../evil.dll",
    "/etc/passwd", "C:/Windows/evil.dll", "C:\\Windows\\evil.dll",
    "_internal\\evil.dll", "a//b", "./evil.dll", "a/./b",
    "evil.dll.", "evil.dll ", " evil.dll", "a/ b/c",
    "nul\x00.dll", "x" * 400, "", None, 42, [],
]:
    ok(offer_mod.safe_relpath(hostile) is None,
       "offer: refuses a path that would escape the app folder: %r" % (hostile,))

# ...and an entry carrying one is refused as a whole, not silently skipped -
# installing "most of" a release is the outcome the swap exists to prevent.
ok(parse(app=[{"path": "../evil.dll", "sha256": "a" * 64, "size": 1}])[1]
   == "bad_file_entry", "offer: one bad path refuses the entire offer")
ok(parse(app=[dict(FILE_A), {"path": "../evil.dll", "sha256": "b" * 64, "size": 1}])[1]
   == "bad_file_entry", "offer: ...even when every OTHER file in it is fine")
ok(parse(addon={**_addon, "path": "../evil.zip"})[1] == "bad_addon",
   "offer: and the same for the add-on half")


# ------------------------------------------------------------------- the swap

class FakeOffer:
    def __init__(self, files):
        self.files = files
        self.addon = None
        self.version = "1.0.1"


ROOT = tempfile.mkdtemp(prefix="madi_swap_")


def write(rel, text):
    full = os.path.join(ROOT, rel.replace("/", os.sep))
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "wb") as handle:
        handle.write(text if isinstance(text, bytes) else text.encode())
    return full


def read(rel):
    try:
        with open(os.path.join(ROOT, rel.replace("/", os.sep)), "rb") as handle:
            return handle.read()
    except OSError:
        return None


def entry(rel, data):
    return {"path": rel, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}


OLD_EXE = b"the old exe"
NEW_EXE = b"the new exe, version one point one"
SAME_DLL = b"a dll that never changes"

write("app.exe", OLD_EXE)
write("_internal/qt.dll", SAME_DLL)
# NOT in any manifest: the user's own settings, which live next to the exe.
write("config.json", '{"mine": true}')
write("render_queue/jobs.json", "[]")

new_exe = entry("app.exe", NEW_EXE)
same_dll = entry("_internal/qt.dll", SAME_DLL)
plan = swap.plan(ROOT, FakeOffer([new_exe, same_dll]))
ok([p["path"] for p in plan] == ["app.exe"],
   "swap: only the file that actually differs is downloaded - that IS the delta")

ok(swap.file_hash(os.path.join(ROOT, "app.exe")) == entry("app.exe", OLD_EXE)["sha256"],
   "swap: a local file hashes to what is on disk")
ok(swap.file_hash(os.path.join(ROOT, "no-such-file")) is None,
   "swap: a missing file hashes to None rather than raising")

ok(not swap.write_staged(ROOT, new_exe, b"not the promised bytes"),
   "swap: staged bytes that do not match the SIGNED hash are refused")
ok(swap.write_staged(ROOT, new_exe, NEW_EXE), "swap: the right bytes are staged")
ok(swap.verify_staged(ROOT, [new_exe]), "swap: staging verifies")

applied, err = swap.apply(ROOT, [new_exe])
ok(applied and err is None, "swap: apply() puts the new file in place")
ok(read("app.exe") == NEW_EXE, "swap: and the new bytes are there")
ok(read("app.exe" + swap.OLD_SUFFIX) == OLD_EXE,
   "swap: the old one is RENAMED beside it, not deleted - a running exe cannot be deleted")
ok(read("config.json") == b'{"mine": true}',
   "swap: config.json is untouched - it is in no manifest, so nothing can overwrite it")
ok(read("render_queue/jobs.json") == b"[]", "swap: and neither is the render queue's data")

ok(swap.rollback(ROOT, [new_exe]) == 1, "swap: rollback restores the previous file")
ok(read("app.exe") == OLD_EXE, "swap: the old bytes are back")
ok(read("app.exe" + swap.OLD_SUFFIX) is None, "swap: and the .madi_old is consumed")

# All or nothing: a failure part way through must leave NOTHING half-swapped.
write("second.dll", b"original second")
swap.write_staged(ROOT, new_exe, NEW_EXE)
missing = entry("second.dll", b"never staged")   # deliberately not downloaded
applied, err = swap.apply(ROOT, [new_exe, missing])
ok(not applied, "swap: apply() fails when a staged file is missing")
ok(read("app.exe") == OLD_EXE,
   "swap: and it PUT BACK the file it had already swapped - half an update is the worst outcome")
ok(read("second.dll") == b"original second", "swap: the untouched file is untouched")
ok(read("app.exe" + swap.OLD_SUFFIX) is None, "swap: no stray .madi_old is left behind")

# ⚠⚠ THE SAME BYTES AT TWO PATHS — the bug that broke EVERY update until
# 2026-08-14. `staged_path` names a staged file after its HASH so identical
# files are downloaded once, and `manager.install`'s download loop dedupes on
# exactly that. `apply()` then MOVED the blob into place, which consumed it,
# so the second destination hit `[WinError 2] cannot find the specified file`,
# the install rolled back, and it retried forever.
#
# This was never exotic: **every release ships `CHANGELOG.md` at two paths**
# (root and `_internal\`) and it changes with every build, so it was always in
# the delta. Four Qt `.qm` translations are byte-identical too. Found in a
# user's log on 1.9.0, whose sha was 1.16.0's CHANGELOG.
#
# ⚠ Downloading ONCE is what the suite has to imitate, or it proves nothing:
# staging the blob per-item would hide the whole bug.
TWIN = b"identical bytes shipped under two names"
write("notes.md", b"old notes")
write("_internal/notes.md", b"old notes too")
twin_a = entry("notes.md", TWIN)
twin_b = entry("_internal/notes.md", TWIN)
ok(twin_a["sha256"] == twin_b["sha256"],
   "swap: the two paths really do share one hash - the precondition")
ok(swap.write_staged(ROOT, twin_a, TWIN), "swap: staged ONCE, as the downloader does")
ok(swap.verify_staged(ROOT, [twin_a, twin_b]),
   "swap: and one blob verifies for BOTH items")
applied, err = swap.apply(ROOT, [twin_a, twin_b])
ok(applied and err is None,
   "swap: apply() installs both copies from one staged blob (was: %s)" % err)
ok(read("notes.md") == TWIN and read("_internal/notes.md") == TWIN,
   "swap: and BOTH paths really got the new bytes")
ok(read("notes.md" + swap.OLD_SUFFIX) == b"old notes"
   and read("_internal/notes.md" + swap.OLD_SUFFIX) == b"old notes too",
   "swap: each keeps its own .madi_old, so a rollback still has both")
swap.rollback(ROOT, [twin_a, twin_b])

# Cleanup, which runs at the next startup once nothing is holding the old file.
swap.write_staged(ROOT, new_exe, NEW_EXE)
swap.apply(ROOT, [new_exe])
ok(read("app.exe" + swap.OLD_SUFFIX) == OLD_EXE, "swap: (re-applied for the cleanup check)")
removed = swap.cleanup(ROOT)
ok(removed == 1, "cleanup: the previous version's file is removed")
ok(read("app.exe" + swap.OLD_SUFFIX) is None, "cleanup: and it is really gone")
ok(read("app.exe") == NEW_EXE, "cleanup: the installed version is left alone")
ok(not os.path.isdir(swap.stage_dir(ROOT)), "cleanup: the staging area goes too")
ok(swap.cleanup(ROOT) == 0, "cleanup: running it again finds nothing to do")
ok(swap.cleanup(None) == 0, "cleanup: and it tolerates having no app folder at all")

ok(swap.app_root() is None and swap.exe_path() is None,
   "swap: from source there is no app folder to update - those files are the working copy")


# --------------------------------------------- updating is open to everyone
#
# ⚠ THIS SECTION USED TO BE "the licence gate", and asserted the opposite of
# everything below: the licence was re-checked before every check, and anything
# but a fresh ACTIVE refused. Marty reversed it on 2026-08-06 ("anybody can
# check for update AND update the app"), because the paid modules ship inside
# the exe anyway - so refusing an update protected nothing while stranding the
# people on the most broken builds. What a dead licence loses is the paid TABS,
# and that is lic_client_test.py's subject, not this one's.

from PySide6.QtCore import QObject, Signal  # noqa: E402


class FakeLicense(QObject):
    checkFinished = Signal(str)

    def __init__(self, state, token=True, starts=True):
        super().__init__()
        self.state = state
        self._record = {"token": "tok"} if token else {}
        self.rechecked = 0
        self._starts = starts

    @property
    def has_token(self):
        return bool(self._record.get("token"))

    def recheck(self, quiet=False):
        self.rechecked += 1
        if not self._starts:
            return False
        self.checkFinished.emit(self.state)
        return True


_real_supported = updater.manager.is_supported
updater.manager.is_supported = lambda: True
_started = []
updater.manager.UpdateManager._start_check = lambda self: _started.append(self.state)

# EVERY licence state updates, including the ones that lock every paid tab.
for state in [
    licensing.ACTIVE,
    licensing.STALE,
    licensing.GRACE_EXPIRED,
    licensing.EXPIRED,
    licensing.SEAT_CONFLICT,
    licensing.REVOKED,
    licensing.UNLICENSED,
]:
    for manual in (True, False):
        _started.clear()
        lic = FakeLicense(state)
        man = updater.UpdateManager(lic)
        man.check(manual=manual)
        ok(bool(_started),
           "open: %s %s -> the update is still requested" % (
               state, "(user asked)" if manual else "(background)"))
    ok(lic.rechecked == 0,
       "open: %s - and the licence is never re-checked first, because nothing "
       "here depends on the answer" % state)

# No licence at all is the ordinary case for a free-tabs user, and it is the
# one that most needs to work: they are the least likely to have a current build.
for manual in (True, False):
    _started.clear()
    lic = FakeLicense(licensing.UNLICENSED, token=False)
    man = updater.UpdateManager(lic)
    man.check(manual=manual)
    ok(bool(_started),
       "open: no licence at all still checks (%s)" % ("asked" if manual else "background"))

# ⚠ And the token must be OPTIONAL all the way down. `_token()` reads through a
# licence manager that may hold no record at all; if it raised, or returned
# None, the request body would carry a null token and the whole open channel
# would fall over for exactly the users it was opened for.
ok(updater.UpdateManager(FakeLicense(licensing.UNLICENSED, token=False))._token() == "",
   "open: no record -> an empty token string, not None and not an exception")
ok(updater.UpdateManager(FakeLicense(licensing.ACTIVE))._token() == "tok",
   "open: a stored token is still sent, so downloads stay attributable")


class _NoRecord(QObject):
    """A licence manager with no `_record` at all - the shape `_token` must
    survive, since it is reached on every check."""
    checkFinished = Signal(str)
    state = licensing.UNLICENSED


ok(updater.UpdateManager(_NoRecord())._token() == "",
   "open: even a licence manager with no _record yields '' rather than raising")

# A reply carrying a real signed offer is AVAILABLE regardless of licence.
man = updater.UpdateManager(FakeLicense(licensing.EXPIRED, token=False))
man._manual = True
man._on_check({"ok": True, "update": True, "version": "9.9.9"}, "nonce", "1.0.0")
ok(man.offer is None and man.state == updater.FAILED,
   "open: a reply with no signed payload is a FAILURE, not a silent offer - "
   "the signature is still the boundary, it is just no longer a licence check")

updater.manager.is_supported = _real_supported
lic = FakeLicense(licensing.ACTIVE)
man = updater.UpdateManager(lic)
man.check(manual=True)
ok(man.state == updater.UNSUPPORTED,
   "open: running from source is still not updatable - those files are the "
   "working copy, and that boundary has nothing to do with licensing")


# ------------------------------------------------------- the add-on half
# A release carries the app and the add-on together, but they move on their own
# schedules — so "my app is current" must not mean "I can never be told my
# add-on is old".

import bridge as bridgemod  # noqa: E402

ADDON_ENTRY = {"version": "0.7.1", "path": "madi_anim_library-0.7.1.zip",
               "sha256": "d" * 64, "size": 4096}


def parse_with_addon(current_app, current_addon, **over):
    p, s = make_offer(**over)
    return offer_mod.parse(p, s, NONCE, current_app, current_addon)


got, why = parse_with_addon("1.0.1", "0.7.0", version="1.0.1", addon=dict(ADDON_ENTRY))
ok(got is not None and got.addon_newer and not got.app_newer,
   "addon: an up-to-date app is still offered a newer ADD-ON")
ok(got.addon["version"] == "0.7.1", "addon: and the offer names it")

got, why = parse_with_addon("1.0.0", "0.7.0", version="1.0.1", addon=dict(ADDON_ENTRY))
ok(got is not None and got.app_newer and got.addon_newer,
   "addon: both halves can be newer at once")

ok(parse_with_addon("1.0.1", "0.7.1", version="1.0.1", addon=dict(ADDON_ENTRY))[1]
   == "not_newer",
   "addon: when BOTH are current there is nothing to offer")
ok(parse_with_addon("1.0.1", "0.7.5", version="1.0.1", addon=dict(ADDON_ENTRY))[1]
   == "not_newer",
   "addon: ANTI-ROLLBACK covers the add-on too - an older one is refused")
ok(parse_with_addon("1.0.1", None, version="1.0.1", addon=dict(ADDON_ENTRY))[1]
   == "not_newer",
   "addon: with Blender not connected there is no add-on to update")

got, _ = parse_with_addon("1.0.0", None, version="1.0.1", addon=dict(ADDON_ENTRY))
ok(got is not None and got.app_newer and not got.addon_newer,
   "addon: the app half still updates while Blender is closed")


class FakeBridge:
    def __init__(self, addon_version, block=None):
        self.addon_version = addon_version
        self._block = block

    def feature_reason(self, feature):
        return self._block


updater.manager.is_supported = lambda: True

man = updater.UpdateManager(FakeLicense(licensing.ACTIVE), FakeBridge("0.7.0"))
ok(man.connected_addon_version() == "0.7.0", "addon: the connected version is read from the bridge")
man._offer = got
ok(not man.addon_available(),
   "addon: nothing is offered when the parsed offer says the add-on is not newer")
man._offer, _ = parse_with_addon("1.0.1", "0.7.0", version="1.0.1", addon=dict(ADDON_ENTRY))
ok(man.addon_available(), "addon: and it is offered when it is")
ok(man.addon_block_reason() is None, "addon: a current add-on can install it")

_blocked = "needs Blender add-on 0.7.0 or newer"
man2 = updater.UpdateManager(FakeLicense(licensing.ACTIVE), FakeBridge("0.6.0", _blocked))
man2._offer = man._offer
ok(man2.addon_block_reason() == _blocked,
   "addon: the CHICKEN-AND-EGG case - an add-on too old to update itself says so plainly")

man3 = updater.UpdateManager(FakeLicense(licensing.ACTIVE), FakeBridge(None))
ok("not connected" in (man3.addon_block_reason() or "").lower(),
   "addon: with Blender closed it says that, rather than failing obscurely")
man4 = updater.UpdateManager(FakeLicense(licensing.ACTIVE), None)
ok(man4.addon_block_reason() is not None,
   "addon: and with no bridge at all it refuses rather than crashing")
ok(man4.connected_addon_version() is None, "addon: no bridge means no version")

# --------------------------------------------- telling apart the failures
# Found live 2026-08-03: the server was up and healthy but 404'd the update
# routes (they had not been deployed yet), and the app said "could not reach the
# update server" — sending you off to check your internet. Reached-and-refused
# is not the same fact as unreachable, which is the whole point of licensing
# rule 1.

man = updater.UpdateManager(FakeLicense(licensing.ACTIVE), FakeBridge("0.7.0"))
man._manual = True
CASES = [
    ({"ok": False, "error": "not found"}, "not offering updates",
     "a server that predates the update routes is NOT an outage"),
    ({"ok": False, "reason": "http_404"}, "not offering updates",
     "and neither is a bare 404"),
    ({"ok": False, "reason": "unreachable"}, "could not reach",
     "a genuine outage still says so"),
    ({"ok": False, "reason": "revoked"}, "withdrawn",
     "a revoked licence says that, not 'unreachable'"),
    ({"ok": False, "reason": "unknown_token"}, "not recognised",
     "an unknown licence says that"),
]
for payload, expect, why in CASES:
    man._on_check(payload, "n", "1.0.0")
    ok(expect in man.message.lower(), "failure: %s" % why)
    ok(man.state == updater.FAILED, "failure: and a manual check reports it (%s)" % expect)

updater.manager.is_supported = _real_supported

# The gate must be declared the same way every other one is, or an older add-on
# would meet a hard failure instead of one disabled button.
ok("addon_self_update" in bridgemod.FEATURE_REQUIREMENTS,
   "addon: the self-update feature declares its requirement like every other")
_cmd, _since, _msg = bridgemod.FEATURE_REQUIREMENTS["addon_self_update"]
ok(_cmd == "addon_update" and _since == "0.7.0",
   "addon: gated on addon_update, since 0.7.0")
ok(bridgemod.feature_block_reason(["ping", "status"], "addon_self_update", "0.6.0"),
   "addon: an add-on without the command is blocked, with a reason")
ok(bridgemod.feature_block_reason(["ping", "addon_update"], "addon_self_update",
                                  "0.7.0") is None,
   "addon: one with it is not")
ok(tuple(int(x) for x in bridgemod.EXPECTED_ADDON_VERSION.split(".")) >= (0, 7, 0),
   "addon: the app expects an add-on that can update itself (%s)"
   % bridgemod.EXPECTED_ADDON_VERSION)


# ------------------------------------------------- the add-on carried in-app
# So the extension can be installed WITHOUT a published release - and because
# the very first install can never come over the bridge anyway: an add-on too
# old to have `addon_update` cannot be told to update itself.

import zipfile  # noqa: E402
from io import BytesIO  # noqa: E402

import addon_bundle  # noqa: E402

ok(addon_bundle.VERSION == bridgemod.EXPECTED_ADDON_VERSION,
   "bundle: the carried add-on is EXACTLY the version the app expects (%s)"
   % addon_bundle.VERSION)

_data = addon_bundle.zip_bytes()
ok(hashlib.sha256(_data).hexdigest() == addon_bundle.SHA256,
   "bundle: it verifies against its own hash on the way out")

_zf = zipfile.ZipFile(BytesIO(_data))
_names = _zf.namelist()
ok("blender_manifest.toml" in _names,
   "bundle: the manifest is at the ZIP ROOT, which is what Blender requires")
ok(not any("\\" in n for n in _names),
   "bundle: forward-slash entry names only (Compress-Archive's backslashes "
   "extract flat off Windows)")
ok(not any("__pycache__" in n for n in _names), "bundle: no __pycache__")
ok("core.py" in _names and "server.py" in _names and "assets.py" in _names,
   "bundle: it carries the actual add-on modules")
_manifest = _zf.read("blender_manifest.toml").decode("utf-8")
ok('version = "%s"' % addon_bundle.VERSION in _manifest,
   "bundle: the manifest inside agrees with the declared version")

# The zip is built from source at build time, so it must match what is on disk
# right now - a stale bundle would install an add-on older than this app.
_src = os.path.join(_ROOT,
                    "blender_addon", "madi_anim_library")
_live = open(os.path.join(_src, "core.py"), "rb").read()
ok(_zf.read("core.py") == _live,
   "bundle: core.py matches the add-on SOURCE - the pack is not stale")

_saved = os.path.join(_SANDBOX, "out.zip")
man = updater.UpdateManager(FakeLicense(licensing.ACTIVE), FakeBridge("0.8.0"))
man.save_bundled_addon(_saved)
ok(open(_saved, "rb").read() == _data, "bundle: it can be written out for a manual install")
ok(addon_bundle.file_name().endswith(".zip") and addon_bundle.VERSION
   in addon_bundle.file_name(),
   "bundle: saved under a name that says which version it is")

# Installing needs a bridge that can actually take it.
man_old = updater.UpdateManager(FakeLicense(licensing.ACTIVE),
                                FakeBridge("0.6.0", "needs 0.7.0 or newer"))
ok(man_old.addon_block_reason(),
   "bundle: an add-on too old to update itself is refused, with a reason")
man_none = updater.UpdateManager(FakeLicense(licensing.ACTIVE), FakeBridge(None))
ok(man_none.addon_block_reason(),
   "bundle: and so is a Blender that is not running - Save zip is the way in")


# ------------------------------- checking a package before Blender sees it
# ⚠⚠ THE 0.45.0 FAILURE. The add-on's manifest picked up a UTF-8 BOM; Blender's
# TOML reader refuses one, `package_install_files` reported that through
# Blender's REPORT system while STILL RETURNING {'FINISHED'}, and so the install
# silently did nothing while the app waited 90 s for a version that was never
# coming - and then blamed Blender for "not coming back". Every check below is
# one of the three locks that stops that being possible again.

_offermod = updater.manager.offer_mod


def _pkg(path, manifest, extra=b"x"):
    with zipfile.ZipFile(path, "w") as z:
        if manifest is not None:
            z.writestr("blender_manifest.toml", manifest)
        z.writestr("__init__.py", extra)
    return path


_GOOD_MAN = ('schema_version = "1.0.0"\nid = "madi_anim_library"\n'
             'version = "9.9.9"\nname = "x"\ntype = "add-on"\n')
_ok_zip = _pkg(os.path.join(_SANDBOX, "good.zip"), _GOOD_MAN.encode("utf-8"))
_bom_zip = _pkg(os.path.join(_SANDBOX, "bom.zip"),
                b"\xef\xbb\xbf" + _GOOD_MAN.encode("utf-8"))
_bare_zip = _pkg(os.path.join(_SANDBOX, "bare.zip"), None)
_notzip = os.path.join(_SANDBOX, "notzip.zip")
open(_notzip, "wb").write(b"this is not a zip")

_found = _offermod.inspect_addon_package(_ok_zip)
ok(_found == {"id": "madi_anim_library", "version": "9.9.9"},
   "package: a good add-on zip reports its id and version")
ok(_offermod.inspect_addon_package(_ok_zip, expect_version="9.9.9"),
   "package: and passes when it is the version that was expected")

for _path, _needle, _why in [
    (_bom_zip, "BOM", "⚠ a UTF-8 BOM on the manifest - the message NAMES it, "
                      "because 'could not parse' sent the last investigation "
                      "to a file that was valid TOML"),
    (_bare_zip, "blender_manifest", "a zip with no manifest in it"),
    (_notzip, "zip", "something that is not a zip at all"),
]:
    _detail = ""
    try:
        _offermod.inspect_addon_package(_path)
    except ValueError as err:
        _detail = str(err)
    ok(_needle in _detail, "package: refuses %s" % _why)

_detail = ""
try:
    _offermod.inspect_addon_package(_ok_zip, expect_version="1.2.3")
except ValueError as err:
    _detail = str(err)
ok("9.9.9" in _detail and "1.2.3" in _detail,
   "package: refuses one that is not the version it was offered as, naming both")

# ⚠ THE REAL CARRIED BUNDLE. This is the check that would have stopped 0.45.0.
ok(_zf.read("blender_manifest.toml")[:3] != b"\xef\xbb\xbf",
   "⚠ bundle: the carried add-on's manifest has NO BOM - with one, every "
   "install is a silent no-op and nothing anywhere reports it")
_bundle_zip = os.path.join(_SANDBOX, "carried.zip")
open(_bundle_zip, "wb").write(_data)
ok(_offermod.inspect_addon_package(_bundle_zip,
                                   expect_version=addon_bundle.VERSION),
   "bundle: the carried package passes the same check Blender applies")


# --------------------------------------------- what the app does about it
class _Note:
    def __init__(self):
        self.sent = []

    def emit(self, text):
        self.sent.append(text)


class _Task:
    def __init__(self):
        self.note = _Note()
        self.cancelled = False


class _PushBridge:
    """A bridge that takes the package and then answers `ping` however the
    test wants - which is the whole space of outcomes the app has to survive."""

    def __init__(self, pings, raise_on_update=None, record=None):
        self.addon_version = "0.44.0"
        self.pings = list(pings)
        self.sent = []
        self._raise = raise_on_update
        self._record = record

    def feature_reason(self, feature):
        return None

    def addon_update(self, path, version=None, sha256=None):
        self.sent.append((path, version))
        if self._raise:
            raise RuntimeError(self._raise)
        # The real add-on writes its outcome to this file. Writing it HERE, and
        # not before the push, is what makes the test honest: the app clears
        # any earlier record first, so a seeded one would simply be deleted.
        if self._record is not None:
            os.makedirs(os.path.dirname(bridgemod.addon_result_path()),
                        exist_ok=True)
            with open(bridgemod.addon_result_path(), "w", encoding="utf-8") as fh:
                json.dump(self._record, fh)
        return {"scheduled": True}

    def request(self, cmd, **kwargs):
        return self.pings.pop(0) if self.pings else {"version": "0.44.0"}


_real_wait = updater.manager.ADDON_WAIT
_real_poll = updater.manager.ADDON_POLL_SECONDS
updater.manager.ADDON_WAIT = 1
updater.manager.ADDON_POLL_SECONDS = 0.02


def _push(bridge, zip_path, target="9.9.9"):
    bridgemod.clear_addon_update_result()
    manager = updater.UpdateManager(FakeLicense(licensing.ACTIVE), bridge)
    return manager, manager._hand_to_blender(_Task(), zip_path, target)


# 1. A package the app can already see is broken never reaches Blender.
_bridge = _PushBridge([])
_man, _out = _push(_bridge, _bom_zip)
ok(_out["ok"] is False and _out["reason"] == "package_bad",
   "⚠ push: a package Blender would refuse is refused HERE, before Blender")
ok(_bridge.sent == [],
   "⚠ push: and the command is never sent - nothing is installed, nothing "
   "reloads, there is no 90-second wait to sit through")
ok("BOM" in _out["detail"], "push: with the real reason attached")

# 2. The add-on refusing it: the app reports THAT, not a timeout guess.
_man, _out = _push(_PushBridge(
    [{"version": "0.44.0"}] * 60,
    record={"ok": False, "state": "refused", "version": "9.9.9",
            "error": "Blender refused the package: it wrote no extension "
                     "folder at all."}), _ok_zip)
ok(_out["reason"] == "addon_refused",
   "⚠ push: a refusal recorded by the add-on is read back and reported as one")
_man._on_addon(_out)
ok("refused" in _man.message.lower() and "wrote no extension folder" in _man.message,
   "⚠ push: and the user is told Blender refused it, IN BLENDER'S OWN TERMS - "
   "the old message said 'Blender did not come back', which was never true")
ok("did not come back" not in _man.message.lower(),
   "push: the message that sent the last investigation the wrong way is gone")

# 3. Installed, but the live reload did not finish: a restart, not a re-install.
_man, _out = _push(_PushBridge(
    [{"version": "0.44.0"}] * 60,
    record={"ok": True, "state": "installed", "version": "9.9.9",
            "reloaded": False,
            "error": "installed, but the live reload failed"}), _ok_zip)
ok(_out["reason"] == "addon_restart",
   "push: an install whose reload failed is not reported as a failed install")
_man._on_addon(_out)
ok("restart" in _man.message.lower(),
   "push: and the fix offered is restarting Blender, which is the actual fix")

# 4. Nothing recorded at all, but THE BRIDGE KEPT ANSWERING. ⚠ This is NOT a
# timeout: `seen` is set only by a reply, so a version here proves Blender was
# there the whole time. It reported "Blender stopped answering" until
# 2026-08-15 — the same lie, about the same button, that the 08-14 pass was
# written to kill.
_man, _out = _push(_PushBridge([{"version": "0.44.0"}] * 60), _ok_zip)
ok(_out["reason"] == "addon_not_installed",
   "push: a bridge that answered on the old version is NOT reported as one "
   "that stopped answering (got %r)" % _out["reason"])
_man._on_addon(_out)
ok("stopped answering" not in _man.message.lower()
   and "0.44.0" in _man.message,
   "push: and the message says what it is still reporting (%r)" % _man.message)

# 5. A record from a DIFFERENT version is not this attempt's answer.
_man, _out = _push(_PushBridge(
    [{"version": "0.44.0"}] * 60,
    record={"ok": False, "state": "refused", "version": "0.1.0",
            "error": "some other push, or a test run"}), _ok_zip)
ok(_out["reason"] == "addon_not_installed",
   "⚠ push: a record left by a DIFFERENT push is ignored - one file in a "
   "shared folder must never answer for the wrong question")

# 6. The bridge refusing the command outright - the add-on's own check.
_man, _out = _push(_PushBridge([], raise_on_update="not a Blender extension"),
                   _ok_zip)
ok(_out["reason"] == "bridge_failed" and "extension" in _out["detail"],
   "push: the add-on's own refusal comes back on the socket, immediately")
_man._on_addon(_out)
ok("extension" in _man.message,
   "push: and its words are carried through to the user, not swallowed")

# 7. The happy path still works.
_man, _out = _push(_PushBridge([{"version": "9.9.9"}]), _ok_zip)
ok(_out == {"ok": True, "addon": "9.9.9"}, "push: a real install still reports success")

# 8. ⚠⚠ THE TWO-BLENDER CASE (Marty, 2026-08-15). An install is per Blender
# VERSION — separate extension folders — but the bridge is ONE port that
# exactly one Blender holds. Installing makes the holder reload, which FREES
# THE PORT, so a second Blender can take it: the package lands in Blender A
# while the app ends up polling Blender B, which will never report the new
# version. It timed out and blamed Blender for "not answering" — about a
# Blender that answered every poll, after an install that had SUCCEEDED.
_ext_root = tempfile.mkdtemp(prefix="madi_blenderroots_")


def _fake_blender(version, addon_version, package="madi_anim_library", bom=False):
    """A Blender config folder with our extension installed in it."""
    folder = os.path.join(_ext_root, version, "extensions", "user_default", package)
    os.makedirs(folder, exist_ok=True)
    text = ('schema_version = "1.0.0"\nid = "%s"\nversion = "%s"\n'
            % (package, addon_version))
    with open(os.path.join(folder, "blender_manifest.toml"), "w",
              encoding="utf-8-sig" if bom else "utf-8") as fh:
        fh.write(text)
    return os.path.join(_ext_root, version)


_roots = [_fake_blender("5.1", "9.9.9"), _fake_blender("5.2", "0.44.0"),
          _fake_blender("4.9", "9.9.9", bom=True)]
_seen_versions = updater.manager.installed_addon_versions("madi_anim_library",
                                                          roots=_roots)
ok(_seen_versions == {"5.1": "9.9.9", "5.2": "0.44.0", "4.9": "9.9.9"},
   "roots: every Blender's installed add-on version is read off disk, BOM and "
   "all (got %r)" % (_seen_versions,))
ok(updater.manager.installed_addon_versions("no_such_package",
                                            roots=_roots) == {},
   "roots: a package that is installed nowhere reads as nothing, not a crash")
ok(isinstance(updater.manager.blender_config_roots(), list),
   "roots: probing the real machine never raises, whatever is installed")

_real_roots = updater.manager.blender_config_roots
updater.manager.blender_config_roots = lambda: _roots
try:
    _man, _out = _push(_PushBridge([{"version": "0.44.0"}] * 60), _ok_zip)
finally:
    updater.manager.blender_config_roots = _real_roots
ok(_out["reason"] == "addon_other_blender",
   "⚠ push: when the target IS on disk but the connected bridge never reports "
   "it, the verdict is 'installed into another Blender' (got %r)"
   % _out["reason"])
ok("5.1" in _out["detail"] and "0.44.0" in _out["detail"],
   "push: and it names WHICH Blender got it and what this one still has (%r)"
   % _out["detail"])
_man._on_addon(_out)
_msg = _man.message.lower()
ok("stopped answering" not in _msg and "restart" not in _msg
   and "start" in _msg,
   "⚠ push: the user is told to start the bridge in the Blender they want "
   "updated — NOT to restart Blender, which fixes nothing here (%r)"
   % _man.message)

shutil.rmtree(_ext_root, ignore_errors=True)

updater.manager.ADDON_WAIT = _real_wait
updater.manager.ADDON_POLL_SECONDS = _real_poll

# The record file itself: written by the add-on, read by us, cleared on demand.
bridgemod.clear_addon_update_result()
ok(bridgemod.addon_update_result() is None,
   "record: nothing there reads as nothing, not as a crash")
with open(bridgemod.addon_result_path(), "w", encoding="utf-8") as _fh:
    _fh.write("{not json")
ok(bridgemod.addon_update_result() is None,
   "record: a corrupt record reads as nothing - it must never take the app down")
with open(bridgemod.addon_result_path(), "w", encoding="utf-8") as _fh:
    json.dump({"ok": True, "state": "installed", "version": "9.9.9"}, _fh)
ok((bridgemod.addon_update_result() or {}).get("state") == "installed",
   "record: and a real one is read back")
bridgemod.clear_addon_update_result()
ok(bridgemod.addon_update_result() is None, "record: clearing it works")

# ⚠ The settings dialog shows bridge.EXPECTED_ADDON_VERSION as "bundled"
# WITHOUT importing addon_bundle (38,559 lines, +4.1 MB resident — PERF_PLAN
# M1). That is only honest while the two constants cannot drift, so the pin
# lives here, in the suite that imports the bundle anyway.
import addon_bundle  # noqa: E402

ok(addon_bundle.VERSION == bridgemod.EXPECTED_ADDON_VERSION,
   "the bundled add-on (%s) IS the version the app claims to carry (%s) — "
   "the settings dialog reads the constant, not the bundle"
   % (addon_bundle.VERSION, bridgemod.EXPECTED_ADDON_VERSION))

print("")
print("%d passed, %d failed" % (len(PASS), len(FAIL)))
for f in FAIL:
    print("FAIL " + f)
sys.exit(1 if FAIL else 0)
