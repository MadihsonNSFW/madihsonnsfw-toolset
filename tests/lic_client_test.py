# Licensing client: crypto, fingerprint, protected storage, the state machine,
# and the gated tabs in the real MainWindow. Offscreen; touches nothing real.
#
#   app\.venv\Scripts\python.exe tests\lic_client_test.py
#
# Nothing here reaches the network: signed blobs are minted locally with a
# throwaway key, and the manager's public key is pointed at it.
import base64
import hashlib
import json
import os
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# The gate is normally frozen-exe only; force it on so the locked UI is testable.
os.environ["MADI_FORCE_LICENSE"] = "1"
# Never touch the real licence blob or the real config.json.
_SANDBOX = tempfile.mkdtemp(prefix="madi_lic_")
os.environ["LOCALAPPDATA"] = _SANDBOX

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

sys.path.insert(0, os.path.join(_ROOT, "app"))

import config  # noqa: E402

config.CONFIG_PATH = os.path.join(_SANDBOX, "config.json")

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

from PySide6.QtWidgets import QApplication  # noqa: E402

import licensing  # noqa: E402
from licensing import ed25519, machine, store  # noqa: E402
from licensing import manager as mgr  # noqa: E402

PASS = []
FAIL = []


def ok(cond, what):
    (PASS if cond else FAIL).append(what)


# --------------------------------------------------- a throwaway signing key
# Ed25519 SIGNING, test-only. The shipped client verifies and never signs; this
# exists so the suite can mint blobs without the server, and it double-checks
# the verifier against an independent implementation of the same RFC.

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


KEY = keypair(b"\x11" * 32)
PUBLIC = KEY[1]
mgr.PUBLIC_KEY = base64.b64encode(PUBLIC).decode()

# NOTHING HERE MAY TOUCH THE NETWORK. LicenseManager.start() calls recheck()
# whenever the stored blob is due one, which posted to the REAL licence server
# on every regression run - writing junk `verify_denied` rows into the live
# ledger and making the suite depend on the internet. `.invalid` is a reserved
# TLD, so DNS fails immediately instead of burning a timeout.
mgr.SERVER_URL = "http://madi-tests.invalid"


def blob(**over):
    """A server-shaped signed answer."""
    now = int(time.time())
    payload = {
        "v": 1, "sub": "ent_test", "nonce": "", "iat": now,
        "not_after": now + 30 * 86400, "active": True, "revoked": False,
        "seat_ok": True, "reason": "ok", "seats_used": 1, "seats_max": 1,
    }
    payload.update(over)
    # Canonical exactly like the server: sorted keys, no spaces, ints only.
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return text, base64.b64encode(sign(KEY, text.encode())).decode()


# ---------------------------------------------------------------- ed25519

for name, pk, msg, sig in [
    ("empty message",
     "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a", "",
     "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a"
     "33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"),
    ("one byte",
     "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c", "72",
     "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e1"
     "5996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00"),
    ("two bytes",
     "fc51cd8e6218a1a38da47ed00230f0580816ed13ba3303ac5deb911548908025", "af82",
     "6291d657deec24024827e69c3abe01a30ce548a284743a445e3680d7db5ac3ac18ff9b538d1"
     "6f290ae67f760984dc6594a7c15e9716ed28dc027beceea1ec40a"),
]:
    ok(ed25519.verify(bytes.fromhex(pk), bytes.fromhex(msg), bytes.fromhex(sig)),
       "ed25519: RFC 8032 vector verifies (%s)" % name)
    bad = bytearray(bytes.fromhex(sig))
    bad[0] ^= 1
    ok(not ed25519.verify(bytes.fromhex(pk), bytes.fromhex(msg), bytes(bad)),
       "ed25519: a flipped signature bit fails (%s)" % name)

ok(ed25519.verify(PUBLIC, b"hello", sign(KEY, b"hello")),
   "ed25519: verifies a signature made by an independent implementation")
ok(not ed25519.verify(PUBLIC, b"hell0", sign(KEY, b"hello")),
   "ed25519: a one-character message change fails")
ok(not ed25519.verify(b"", b"x", b""), "ed25519: empty key/signature is simply invalid")
ok(not ed25519.verify(b"\x00" * 32, b"x", b"\x00" * 64),
   "ed25519: zeroed key and signature is invalid, not a crash")

# S >= L is a malleability trick, not a signature.
_good = sign(KEY, b"m")
_bad_s = _good[:32] + (int.from_bytes(_good[32:], "little") + ed25519.L).to_bytes(32, "little")
ok(not ed25519.verify(PUBLIC, b"m", _bad_s),
   "ed25519: a scalar at or above the group order is rejected (RFC 8032 check)")

# ---------------------------------------------------------- licence keys
# Manually issued keys, for sales that never touch Patreon. The format check
# must agree with the server's EXACTLY (license-server\lib\ids.js) or a real
# key gets rejected in the app before it is ever sent.

def _make_key(body15):
    """Build a well-formed key the way the server does, so the checksum is real."""
    total = 0
    for ch in body15:
        total = (total * 3 + mgr.KEY_ALPHABET.find(ch)) % len(mgr.KEY_ALPHABET)
    full = body15 + mgr.KEY_ALPHABET[total]
    return "MADI-" + "-".join(full[i:i + 4] for i in range(0, 16, 4))


_good = _make_key("23456789ABCDEFG")
ok(mgr.normalise_key(_good) == _good, "key: a well-formed key is accepted")
ok(mgr.normalise_key(_good.lower()) == _good, "key: case does not matter")
ok(mgr.normalise_key(_good.replace("-", "")) == _good, "key: dashes are optional")
ok(mgr.normalise_key(" " + _good.replace("MADI-", "") + " ") == _good,
   "key: spaces and a missing MADI- prefix are fine")
ok(mgr.normalise_key(_good.replace("-", " ")) == _good, "key: spaces instead of dashes")
ok(mgr.normalise_key(_good[:-1] + ("2" if _good[-1] != "2" else "3")) is None,
   "key: one wrong character is caught by the check digit, before any round trip")
_body = _good.replace("-", "")[4:]
ok(mgr.normalise_key("MADI-" + _body[1] + _body[0] + _body[2:]) is None,
   "key: two transposed characters are caught")
ok(mgr.normalise_key("MADI-1234-5678-9ABC-DEFG") is None,
   "key: characters outside the alphabet are refused, never silently 'corrected'")
ok(mgr.normalise_key("") is None and mgr.normalise_key(None) is None,
   "key: nothing is not a key")
ok(mgr.normalise_key(_good + "XY") is None, "key: too long is rejected")
ok(all(c not in mgr.KEY_ALPHABET for c in "01OIL"),
   "key: 0/1/O/I/L are excluded - these get read off a screen and typed")

# ------------------------------------------------------------ TLS trust
# The licence check must work on a Windows install that has not updated in
# years or has automatic root update switched off. Found 2026-08-03: a fully
# updated Windows 11 box carried an EXPIRED ISRG Root X2 (Sep 2025), which
# OpenSSL 1.1.1 picked as the anchor and then refused the connection over.

import calendar  # noqa: E402
import ssl as _ssl  # noqa: E402

from licensing import roots  # noqa: E402

ok(roots.verified_pem(), "tls: the embedded root passes its published fingerprint check")
ok("ISRG Root X1" in roots.EMBEDDED, "tls: the Let's Encrypt root is the one we carry")

_bad = dict(roots.EMBEDDED)
_bad["ISRG Root X1"] = (roots.ISRG_ROOT_X1, "0" * 64)
_real = roots.EMBEDDED
roots.EMBEDDED = _bad
ok(roots.verified_pem() == "",
   "tls: a root whose fingerprint does not match is DROPPED, never trusted")
roots.EMBEDDED = _real

_ctx = mgr.ssl_context()
ok(_ctx.verify_mode == _ssl.CERT_REQUIRED, "tls: certificates are actually verified")
ok(_ctx.check_hostname, "tls: the hostname is checked too")
_names = [dict(x[0] for x in c["subject"]).get("commonName") for c in _ctx.get_ca_certs()]
ok("ISRG Root X1" in _names,
   "tls: our own root is in the trust set - the OS store is a bonus, not a requirement")
_now = time.time()
_expired = []
for _c in _ctx.get_ca_certs():
    try:
        if calendar.timegm(time.strptime(_c["notAfter"], "%b %d %H:%M:%S %Y %Z")) < _now:
            _expired.append(dict(x[0] for x in _c["subject"]).get("commonName"))
    except Exception:
        pass
ok(not _expired,
   "tls: no EXPIRED anchor survives into the trust set (that is the bug that broke HTTPS)")
ok(len(_ctx.get_ca_certs()) > 1, "tls: healthy OS anchors are still used as well")
ok(mgr.ssl_context() is _ctx, "tls: the context is built once and cached")

# ------------------------------------------------------------- fingerprint

comp = machine.components()
ok(isinstance(comp, dict) and comp, "fingerprint: this machine reports components")
ok(all(len(v) == 32 and all(c in "0123456789abcdef" for c in v) for v in comp.values()),
   "fingerprint: every component leaves as a hash, never a serial number")
ok("mac" not in comp and "nic" not in comp,
   "fingerprint: no MAC address (virtual NICs are the top cause of false lockouts)")
ok(machine.components() == comp, "fingerprint: stable across calls")
ok(machine.components(refresh=True) == comp, "fingerprint: stable when recomputed")
ok(len(machine.label()) > 0, "fingerprint: the PC reports a name for support")
_raw_leak = any(os.environ.get("COMPUTERNAME", "zzz").lower() in v.lower() for v in comp.values())
ok(not _raw_leak, "fingerprint: the machine name is not recoverable from a component")

# ----------------------------------------------------------------- storage

ok(_SANDBOX in store.storage_path(), "store: writes under LOCALAPPDATA")
ok("dist" not in store.storage_path() and "app" not in store.storage_path().split(os.sep)[-2:],
   "store: NOT next to the exe - copying the app folder must not copy the licence")
store.clear()
ok(store.load() == {}, "store: no licence reads as empty, not as an error")
ok(store.save({"token": "MADI-x", "n": 1}), "store: saves")
ok(store.load()["token"] == "MADI-x", "store: round-trips")
_raw = open(store.storage_path(), "rb").read()
ok(b"MADI-x" not in _raw, "store: the token is not readable in the file (DPAPI)")
_rec = store.touch_clock({})
ok(_rec["clock_high_water"] >= int(time.time()) - 2, "store: clock high-water is recorded")
_rec["clock_high_water"] = 99999999999
store.touch_clock(_rec)
ok(_rec["clock_high_water"] == 99999999999,
   "store: the high-water mark only ever goes up (that is the rollback defence)")
store.clear()
ok(store.load() == {}, "store: clear() removes it")

# ----------------------------------------------------- signed-blob checking

_p, _s = blob(nonce="my-nonce-1234")
ok(mgr.check_blob(_p, _s, "my-nonce-1234") is not None, "blob: a genuine answer is accepted")
ok(mgr.check_blob(_p, _s, "different-nonce") is None,
   "blob: an answer carrying somebody else's nonce is rejected (replay defence)")
ok(mgr.check_blob(_p.replace('"active":true', '"active":false'), _s, "my-nonce-1234") is None,
   "blob: editing the payload breaks it")
ok(mgr.check_blob(_p, base64.b64encode(b"\x00" * 64).decode(), "my-nonce-1234") is None,
   "blob: a forged signature is rejected")
ok(mgr.check_blob("", "", None) is None, "blob: nothing is not an answer")
_wrong = keypair(b"\x22" * 32)
_txt = json.dumps({"v": 1, "not_after": 1}, sort_keys=True, separators=(",", ":"))
ok(mgr.check_blob(_txt, base64.b64encode(sign(_wrong, _txt.encode())).decode(), None) is None,
   "blob: signed by the WRONG key is rejected - a fake server cannot answer for us")

# ------------------------------------------------------------ state machine

app = QApplication.instance() or QApplication([])


# Every manager this suite makes is kept alive. start() can spawn a _Task
# QThread, and a QThread whose owner has been garbage-collected takes the
# process down (0xC0000409) the moment it finishes and emits into a dead
# QObject. Nothing noticed for as long as no test ever ran the event loop; the
# preview check below does, and found it immediately.
_KEEP_ALIVE = []


def manager_with(record):
    store.clear()
    if record:
        store.save(record)
    m = mgr.LicenseManager()
    _KEEP_ALIVE.append(m)
    m.start()
    return m


ok(mgr.is_gated(), "gate: MADI_FORCE_LICENSE turns the gate on from source")

# ⚠ THE ONE-ENVIRONMENT-VARIABLE BYPASS, PINNED SHUT (2026-08-06).
# `is_gated()` used to read the environment BEFORE checking `sys.frozen`, so
# `MADI_FORCE_LICENSE=0` unlocked every paid tab on the SHIPPED EXE: gate off ->
# state DEV -> DEV is an unlocked state -> the real tabs get built. No patching,
# no decompiling, and the file explaining it ships inside the exe.
_saved_env = os.environ.get("MADI_FORCE_LICENSE")
_saved_frozen = getattr(sys, "frozen", None)
try:
    sys.frozen = True
    for _value in ("0", "1", "", "anything"):
        os.environ["MADI_FORCE_LICENSE"] = _value
        ok(mgr.is_gated(),
           "gate: a FROZEN build stays gated with MADI_FORCE_LICENSE=%r - the "
           "override is for the dev loop and must not ship a way out" % _value)
    del os.environ["MADI_FORCE_LICENSE"]
    ok(mgr.is_gated(), "gate: and with the variable unset")
finally:
    if _saved_frozen is None:
        del sys.frozen
    else:
        sys.frozen = _saved_frozen
    if _saved_env is None:
        os.environ.pop("MADI_FORCE_LICENSE", None)
    else:
        os.environ["MADI_FORCE_LICENSE"] = _saved_env

os.environ["MADI_FORCE_LICENSE"] = "1"
ok(mgr.is_gated(),
   "gate: ...and unfreezing puts the source-run override back, so the rest of "
   "this suite still exercises the locked UI")
ok(manager_with({}).state == mgr.UNLICENSED, "state: no licence -> unlicensed")

_p, _s = blob()
ok(manager_with({"token": "t", "payload": _p, "sig": _s}).state == mgr.ACTIVE,
   "state: a fresh signed licence -> active")

_p, _s = blob(revoked=True, active=False, reason="revoked")
_m = manager_with({"token": "t", "payload": _p, "sig": _s})
ok(_m.state == mgr.REVOKED, "state: a signed revoke locks the tabs")
ok(not _m.unlocked, "state: and revoked really is locked")

_p, _s = blob(active=False, reason="seat_in_use", seat_ok=False)
ok(manager_with({"token": "t", "payload": _p, "sig": _s}).state == mgr.SEAT_CONFLICT,
   "state: seat held elsewhere -> seat conflict, with its own message")

# ⚠ THIS PAIR ASSERTED THE OPPOSITE UNTIL 2026-08-06, and the old wording is
# worth keeping in view: "STALE STILL WORKS - our outage must not brick a paying
# customer" and "the licence is perpetual, they paid for it once". Both were
# right for a perpetual licence and both are wrong for an annual one, because if
# being unreachable kept everything unlocked then staying offline would simply
# be how you never renew. Marty's rule: "if the app doesn't see an internet for
# 30 days revoke the premium capabilities until a successful license check."
#
# The 30 days is not counted here - it is the server's `not_after`, so a clock
# change cannot buy extra time and we are never the ones deciding.
_now = int(time.time())
_p, _s = blob(not_after=_now - 5 * 86400)
_m = manager_with({"token": "t", "payload": _p, "sig": _s, "last_check": _now})
ok(_m.state == mgr.STALE, "state: past not_after -> stale")
ok(not _m.unlocked,
   "state: STALE LOCKS the paid tabs - 30 days with no successful check is the "
   "condition Marty asked to have revoke premium until one succeeds")
ok("locked" in _m.message.lower() and "internet" in _m.message.lower(),
   "state: and says so, with the fix - a lock with no explanation is a support ticket")

_p, _s = blob(not_after=_now - 400 * 86400)
_m = manager_with({"token": "t", "payload": _p, "sig": _s, "last_check": _now})
ok(_m.state == mgr.GRACE_EXPIRED, "state: long past not_after -> grace expired")
ok(not _m.unlocked, "state: and that locks too")

# ------------------------------------------------------- the licence's own year
# entitled_until is a DIFFERENT clock from not_after: one is "how stale is this
# answer", the other is "when was this paid up to". It lives inside the
# signature so it holds with no network at all - otherwise going offline would
# be the way to never expire.

_p, _s = blob(entitled_until=_now + 200 * 86400)
_m = manager_with({"token": "t", "payload": _p, "sig": _s, "last_check": _now})
ok(_m.state == mgr.ACTIVE and _m.unlocked, "expiry: inside the year -> active")
ok(_m.days_left is not None and 199 <= _m.days_left <= 200,
   "expiry: days_left counts down from the signed date")
ok(not _m.expiring_soon, "expiry: 200 days out is not 'expiring soon'")

_p, _s = blob(entitled_until=_now + 3 * 86400)
_m = manager_with({"token": "t", "payload": _p, "sig": _s, "last_check": _now})
ok(_m.state == mgr.ACTIVE and _m.unlocked,
   "expiry: three days left still WORKS - warning is not locking")
ok(_m.expiring_soon, "expiry: ...but it is flagged as expiring soon, so the app can warn")

_p, _s = blob(entitled_until=_now - 86400)
_m = manager_with({"token": "t", "payload": _p, "sig": _s, "last_check": _now})
ok(_m.state == mgr.EXPIRED, "expiry: past the date -> EXPIRED, computed locally")
ok(not _m.unlocked, "expiry: and locked")
ok("patreon" in _m.message.lower(),
   "expiry: the message points at Patreon, which is where renewing happens")
ok("contact support" not in _m.message.lower() and "withdrawn" not in _m.message.lower(),
   "expiry: and never says 'contact support' or 'withdrawn' - that is the "
   "REVOKED wording, and telling someone whose year lapsed that they were "
   "withdrawn is an accusation plus a support ticket")

# Two shapes mean "never expires" and BOTH have to be safe, because they arrive
# from different places: `null` is what the live server sends for a forever
# licence, and the field being ABSENT is what a server too old to know about
# expiry sends. Either one locking somebody out would be a self-inflicted outage
# during a partial rollout.
_p, _s = blob(entitled_until=None)
_m = manager_with({"token": "t", "payload": _p, "sig": _s, "last_check": _now})
ok(_m.state == mgr.ACTIVE and _m.days_left is None,
   "expiry: entitled_until null = a forever licence, not an expired one")

_p, _s = blob()
_m = manager_with({"token": "t", "payload": _p, "sig": _s, "last_check": _now})
ok(_m.state == mgr.ACTIVE and _m.days_left is None,
   "expiry: and the field missing entirely (an older server) is also perpetual")

# ⚠ isinstance(True, int) IS TRUE in Python, and `now >= True` is true for every
# real timestamp — so a bool here would read as "expired in 1970" and lock out a
# paying customer. The same trap is already documented on the update channel's
# `size` field; this is the licensing half of it.
for _junk in (True, False, "soon", 1.5, [], {}):
    _p, _s = blob(entitled_until=_junk)
    _m = manager_with({"token": "t", "payload": _p, "sig": _s, "last_check": _now})
    ok(_m.state == mgr.ACTIVE and _m.days_left is None,
       "expiry: %r is not a date and must not expire anybody" % (_junk,))

# --------------------------------------------------- a check at every launch
# Marty: "check license every time user open the app". It used to be weekly, so
# a revoke or an expiry could sit unnoticed for up to seven days.
# ⚠ AND THE THREAD MUST BE WAITED FOR, OR THE PROCESS ABORTS.
# Moving the check to every launch made `--smoke` crash with 0xC0000409: the
# run builds the window and returns immediately, destroying a QThread that is
# still mid-request, which Qt treats as fatal. It is not a test-only problem -
# `updater\swap.smoke()` runs exactly that to decide whether to KEEP an update,
# so a build that dies there would make every update roll itself back.
_m = manager_with({"token": "t", "payload": blob()[0], "sig": blob()[1],
                   "last_check": _now})
ok(hasattr(_m, "shutdown"), "teardown: the licence manager can be shut down")
_m.recheck(quiet=True)
_m.shutdown()
ok(_m._task is None,
   "teardown: shutdown() waits for the in-flight check and clears it, so "
   "nothing is left running to be destroyed underneath Qt")
try:
    _m.shutdown()
    _twice_ok = True
except Exception:
    _twice_ok = False
ok(_twice_ok,
   "teardown: shutting down twice raises nothing - closeEvent can follow the "
   "smoke path, and a crash while closing is the worst place for one")

_asked = []
_real_recheck = mgr.LicenseManager.recheck
mgr.LicenseManager.recheck = lambda self, quiet=False: _asked.append(self.state)
try:
    for _made, _label in [
        (blob(), "active"),
        (blob(not_after=_now - 5 * 86400), "stale"),
        (blob(entitled_until=_now - 86400), "expired"),
    ]:
        _asked.clear()
        _p, _s = _made
        # last_check is NOW, which under the old weekly rule meant "asked
        # recently, do not ask again" - so this is exactly the case that used
        # to stay silent for up to seven days.
        manager_with({"token": "t", "payload": _p, "sig": _s, "last_check": _now})
        ok(bool(_asked),
           "startup: a %s licence is re-checked the moment the app opens, even "
           "though it was checked seconds ago" % _label)

    _asked.clear()
    manager_with({})
    ok(not _asked,
       "startup: ...but with no licence stored there is nothing to ask about, "
       "so no request goes out at all")
finally:
    mgr.LicenseManager.recheck = _real_recheck

_p, _s = blob()
_m = manager_with({"token": "t", "payload": _p, "sig": _s,
                   "clock_high_water": _now + 30 * 86400, "last_check": _now})
ok(_m.state == mgr.CLOCK_TAMPER,
   "state: a clock rolled back a month is caught by the high-water mark")

_p, _s = blob()
_m = manager_with({"token": "t", "payload": _p, "sig": _s,
                   "clock_high_water": _now + 3600, "last_check": _now})
ok(_m.state == mgr.ACTIVE,
   "state: an hour of clock slack (DST, NTP) is NOT treated as tampering")

_p, _s = blob()
ok(manager_with({"token": "t", "payload": _p + " ", "sig": _s}).state == mgr.UNLICENSED,
   "state: a tampered stored blob reads as no licence, not as a licence")

# ------------------------------------- a SOURCE run still has a blob to push
# ⚠ REGRESSION GUARD, and it cost Marty a confused first click on the Scene
# Optimizer. There are TWO gates: the app's tabs (ungated from source, so the
# dev loop is untouched) and the ADD-ON's paid panels inside Blender, which can
# only be opened by the real server-signed blob. `start()` used to return before
# loading the stored record when not gated, so `_record` stayed empty,
# `main._push_license()` found nothing to send, and every paid Blender panel
# stayed locked in every source run - Anim Layers' N-panel, the Bone picker and
# the optimizer alike. It hid for so long because all three were only ever
# proven against the gated exe, which does load it.
_p, _s = blob()
store.save({"token": "t", "payload": _p, "sig": _s})
os.environ["MADI_FORCE_LICENSE"] = "0"          # i.e. a run from source
_dev = mgr.LicenseManager()
_dev.start()
ok(_dev.state == mgr.DEV,
   "source: the app's own tabs are still ungated from source")
ok(_dev._record.get("payload") == _p and _dev._record.get("sig") == _s,
   "source: and the stored licence is STILL LOADED, so there is something to "
   "hand Blender - without this every paid Blender panel is locked in every "
   "source run")
store.clear()
_none = mgr.LicenseManager()
_none.start()
ok(not _none._record.get("payload"),
   "source: with no licence stored there is nothing to send - loading the "
   "record is not a bypass, and the add-on stays locked")
os.environ["MADI_FORCE_LICENSE"] = "1"

# -------------------------------------------------------- the gated tabs

import main as mainmod  # noqa: E402

store.clear()
win = mainmod.MainWindow()
titles = [win.main_tabs.tabText(i) for i in range(win.main_tabs.count())]
# ⚠ EVERY TAB IS FREE SINCE 2026-08-14 (Marty: "make all tabs free" — the
# pivot: the tools are free for everyone and premium pose/animation PACKS
# become the paid thing, gated SERVER-SIDE by refusing the download without a
# key; ..\PACKS_PLAN.md). The strip order is UNCHANGED because the four
# ex-paid tabs were appended to FREE_TOOLS in the exact order GATED held
# them. Both the order and the emptiness are pinned, because the lock
# machinery is still in the codebase, dormant — nothing may quietly re-gate
# a tab without this suite saying so.
ok(titles == ["Studio Library", "Rendering", "Bone picker", "Anim Layers",
              "Node Setup", "Node Editor", "MadiRef", "Optimization",
              "NSFW Tools", "Physics", "What's New"],
   "tabs: the strip is in Marty's order, unchanged by the freeing")
ok(mainmod.MainWindow.GATED == (),
   "tabs: GATED is EMPTY - every tab is free (2026-08-14)")
ok(mainmod.MainWindow.GATED_ATTRS == (),
   "tabs: GATED_ATTRS is empty with it - no free tab's attribute may ever "
   "be on a restore list")
locked = [i for i in range(win.main_tabs.count())
          if isinstance(win.main_tabs.widget(i), licensing.LockedPage)]
ok(locked == [],
   "tabs: NO LockedPage anywhere, with no licence stored at all (got %r)"
   % (locked,))
ok(win.rendering is not None and win.render_queue is not None
   and win.picker is not None
   and win.node_setup is not None and win.nodeeditor is not None,
   "tabs: the always-free tools are built exactly as before")
# ⚠ Anim Layers is LAZY since PERF_PLAN option C — still free, but built on
# FIRST OPEN instead of at startup. Free-ness is proved by opening it through
# the real path (the tab switch) and getting the page, no licence involved.
ok(win.anim_layers is None,
   "tabs: the lazy Anim Layers tab is NOT built at startup")
_al_index = next(i for i in range(win.main_tabs.count())
                 if win.main_tabs.tabText(i) == "Anim Layers")
win.main_tabs.setCurrentIndex(_al_index)
ok(win.anim_layers is not None and win.layers_page is not None
   and win.markers_tool is not None,
   "tabs: opening Anim Layers builds it on demand, free, no licence asked")
ok(win.madiref is not None and win.optimizer is not None
   and win.nsfw is not None and win.physics is not None,
   "tabs: the four EX-PAID tools are now BUILT at startup, licence or none")
ok(win.bone_jiggle_tool is not None
   and win.affector_torus_tool is not None
   and win.optimizer_adaptive_tool is not None,
   "tabs: and their sub-tools with them")
ok(len(win._pages()) == win.tabs.count() + len(win.FREE_TOOLS)
   + len(win.GATED),
   "tabs: _pages() carries every tool tab (%d free + %d gated)"
   % (len(win.FREE_TOOLS), len(win.GATED)))
rail = win.physics.rail
ok(rail.topLevelItem(0).text(0) == "BONES",
   "tabs: the Physics rail lists Bones above Cage - a live page, not a stub")
win.save_settings()
ok(True, "tabs: save_settings() survives with every tab built")
ok(win.license_chip.isVisible() or win.license_chip.text() == "Not licensed",
   "tabs: the status bar still shows the licence state - the key pays for "
   "PACKS now, not tabs")

# the dead-bridge stub the lock previews used: dormant machinery, kept live
_dead = mainmod._DeadBridge()
ok(_dead.feature_reason("anything") is None,
   "preview: the dead bridge reports features as available, so tools look normal")
ok(_dead.capabilities == [] and _dead.addon_version is None,
   "preview: capabilities is a LIST, not a failure dict (this crashed Physics once)")
ok(_dead.request("cage_build")["ok"] is False, "preview: every command fails instantly")
ok(_dead.anything_written_later()["ok"] is False,
   "preview: including helpers that do not exist yet")

# a licence ARRIVING changes nothing in the strip any more - there is nothing
# locked for it to swap. The manager itself still works: the key's meaning is
# premium packs (server-side), and the chip/renewal flow all still run on it.
_p, _s = blob()
win.license._record = {"token": "t", "payload": _p, "sig": _s}
win.license._evaluate()
ok(win.license.state == mgr.ACTIVE, "unlock: the manager still goes active")
ok([win.main_tabs.tabText(i) for i in range(win.main_tabs.count())] == titles,
   "unlock: ...and the strip does not move or change by one tab")
ok(not any(isinstance(win.main_tabs.widget(i), licensing.LockedPage)
           for i in range(win.main_tabs.count())),
   "unlock: still no lock panels - there was nothing to unlock")

from PySide6.QtGui import QCloseEvent  # noqa: E402

win.closeEvent(QCloseEvent())
ok(True, "tabs: closeEvent shuts the render queue down cleanly with every "
         "tab live")

# ---- the DORMANT lock screen, constructed directly --------------------------
# No tab shows it while GATED is empty, but the class stays shippable for a
# future re-gating, so its behaviour is pinned here on a hand-built instance.
# "Check again" must never be a dead button: recheck() returns immediately
# when there is no stored token, so offering it then visibly ignores the user.
win.license._record = {}
win.license._evaluate()
_lock = licensing.LockedPage(win.license, "Physics", "Cages and jiggle.")
ok(not win.license.has_token, "recheck: no licence stored in this state")
ok(not _lock.recheck_button.isVisibleTo(_lock),
   "recheck: the button is HIDDEN with no licence - it could do nothing there")
ok(_lock.key_button.isVisibleTo(_lock) and _lock.unlock_button.isVisibleTo(_lock),
   "recheck: the two buttons that DO something are still offered")
win.license._record = {"token": "t"}
_lock._on_state(mgr.SEAT_CONFLICT)
ok(_lock.recheck_button.isVisibleTo(_lock),
   "recheck: it appears on a seat conflict, where the other machine may have freed it")
win.license._record = {}
_lock._on_state(mgr.UNLICENSED)
ok(not _lock.recheck_button.isVisibleTo(_lock), "recheck: and goes away again")
_lock.deleteLater()

# ---- what the lock screen SAYS (Marty, 2026-08-09) -------------------------
# "Members only" left the one question that decides whether somebody pays —
# WHICH tier — unanswered on the only screen where they are asking it.
from PySide6.QtWidgets import QLabel as _QLabel  # noqa: E402

_page = licensing.LockedPage(win.license, "Physics", "Cages and jiggle.")
_texts = " ".join(lbl.text() for lbl in _page.findChildren(_QLabel))
ok("Tier 3" in _texts,
   "lock: the panel names TIER 3 — 'members only' never said which tier")
ok("for a year" in _texts,
   "lock: ...and that it lasts a year, not forever (licences went annual "
   "2026-08-06 and this screen is read by people about to pay)")

# ⚠ THE COUNT IS COUNTED. This sentence named three free tabs for three days
# after there were seven, because it was a hand-typed list beside a list that
# kept changing.
_free = ["Studio Library"] + [t for _k, t in mainmod.MainWindow.FREE_TOOLS] \
        + ["What's New"]
ok(("other %d tabs are free" % len(_free)) in _texts,
   "⚠ lock: the free-tab count is derived from FREE_TOOLS, so freeing a tab "
   "updates this screen by itself (%d)" % len(_free))
for _title in _free:
    ok(_title in _texts, "lock: ...and %s is named among them" % _title)
ok("unlocks permanently" not in _texts,
   "lock: the old perpetual-licence promise is gone from every label")
_page.deleteLater()

win.render_queue.shutdown()

print("")
print("%d passed, %d failed" % (len(PASS), len(FAIL)))
for f in FAIL:
    print("FAIL " + f)
sys.exit(1 if FAIL else 0)
