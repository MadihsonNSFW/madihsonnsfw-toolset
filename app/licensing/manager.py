"""Licence state machine and all of its network traffic.

TWO RULES THIS FILE EXISTS TO KEEP:

1. **Nothing here ever runs on the GUI thread.** A connect to an unreachable
   host is not refused on this machine — the SYN is dropped — so every attempt
   burns the full timeout. That is the bug that froze the whole app every few
   seconds when Blender's bridge was down (docs\\app-shell.md). Every request
   below runs in a worker thread.

2. **The client is never the judge.** It holds a server-signed blob or it does
   not. It checks the signature, checks that its own nonce came back inside
   that signature, and believes nothing else.

The licence is PERPETUAL: paid once, entitled forever. So the weekly check is
not an entitlement check — it exists so that a revoke can bite and so one seat
means one machine. That is why an unreachable server never locks anything:
these people paid, and our outage is not their problem.
"""

import calendar
import json
import os
import secrets
import ssl
import sys
import time
import urllib.error
import urllib.request

from PySide6.QtCore import QObject, QThread, Signal

from . import ed25519, machine, roots, store

# The licence server's public key. Its private half lives only on the server.
PUBLIC_KEY = "MbYPKzKddZcjdchpdZXPddVCSkJi2LLVbPyBdh65n3s="

# ⚠ THE SERVER URL IS NOT IN THIS FILE, AND NOT IN THE PUBLIC REPOSITORY.
# `endpoint.py` is local-only (see .gitignore). A build packaged WITHOUT it
# ships an app that cannot sign in, and nothing says so until a user tries —
# which is why `tools\verify_exe.py` asserts the URL is present in the frozen
# build.
#
# The environment wins over the file, so a test, a staging run or a
# self-hosted server can redirect it without editing anything.
try:
    from .endpoint import SERVER_URL as _DEFAULT_SERVER
except ImportError:                          # a clone, or a deliberately stripped build
    _DEFAULT_SERVER = ""

SERVER_URL = os.environ.get("MADI_LICENSE_SERVER", _DEFAULT_SERVER).rstrip("/")

# ⚠ THE OFFLINE POLICY REVERSED ON 2026-08-06, at Marty's instruction:
# "check license every time user open the app, and if the app doesn't see an
# internet for 30 days revoke the premium capabilities until a successful
# license check."
#
# It used to be the opposite - an outage NEVER locked anyone out, because a
# perpetual licence had to survive our downtime. That reasoning does not carry
# over to a licence that expires: if being unreachable kept everything unlocked,
# staying offline would simply be how you never renew.
#
# So the check now runs at every startup, and the unlocked window is the 30 days
# the SERVER stamps into `not_after` - not a number this file invents, because a
# duration the client counts down is a duration a clock change defeats.
CHECK_INTERVAL = 0
# Kept only so a blob signed by an older server (which had no expiry concept)
# still resolves to something sensible; nothing extends past not_after now.
OFFLINE_GRACE = 0
# Clocks legitimately move by an hour (DST) or a few minutes (NTP). Six hours
# back is not weather.
CLOCK_SLACK = 6 * 3600

REQUEST_TIMEOUT = 8
PAIR_POLL_SECONDS = 2
PAIR_TIMEOUT = 10 * 60

# How close to expiry the app starts saying so. Nobody should meet a locked tab
# without having been warned - the whole point is that renewing is easy.
EXPIRY_WARNING_DAYS = 14

# States
UNLICENSED = "unlicensed"
LINKING = "linking"
ACTIVE = "active"
STALE = "stale"
GRACE_EXPIRED = "grace_expired"
EXPIRED = "expired"
REVOKED = "revoked"
SEAT_CONFLICT = "seat_conflict"
CLOCK_TAMPER = "clock_tamper"
DEV = "dev"

# ⚠ The states in which the paid tabs work. STALE and GRACE_EXPIRED USED TO BE
# IN HERE and are deliberately not any more - that pair IS the "we have not
# reached the server in 30 days" condition, and Marty's rule is that it locks.
# EXPIRED is not in here either, obviously; it is the whole point.
#
# What has NOT changed: the free tabs (Studio Library, Rendering, What's New)
# never consult this at all, so none of these states can take away work someone
# has already saved.
UNLOCKED_STATES = (ACTIVE, DEV)


def is_gated():
    """Only the built exe is gated.

    Running from source stays unlocked so the dev loop and the regression are
    untouched by any of this. MADI_FORCE_LICENSE=1 turns the gate on from
    source, which is how the locked UI gets tested.

    ⚠ A FROZEN BUILD IGNORES THE OVERRIDE ENTIRELY, and that ordering is the
    whole point of this function (fixed 2026-08-06).

    It used to read the environment FIRST, so `MADI_FORCE_LICENSE=0` turned the
    gate off on the SHIPPED EXE: `is_gated()` false -> state DEV -> DEV is in
    UNLOCKED_STATES -> `_add_gated_tabs()` builds all six paid tabs for real. A
    single environment variable, no patching and no decompiling, unlocked every
    paid tab for anybody who read this file - and the file ships inside the exe.

    The switch exists for the DEV LOOP, so it is now only honoured where the dev
    loop lives. `=0` from source still means "off", which is the default there
    anyway, so nothing in the suite changes.

    ⚠ This does NOT make the app hard to unlock - see `docs\licensing.md`. The
    Python is recoverable from the exe by anyone who wants it, and the honest
    boundary is the ADD-ON's gate, which verifies a real server signature. This
    fix removes an open door; it does not build a wall.
    """
    if getattr(sys, "frozen", False):
        return True
    return os.environ.get("MADI_FORCE_LICENSE") == "1"


# ---------------------------------------------------------- licence keys
# Manually issued keys, for sales that never touch Patreon. The format and its
# check character mirror the server exactly (license-server\lib\ids.js), so an
# ordinary typo is caught HERE — before a round trip, and without the user
# being told something scary about their key being invalid.

KEY_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
KEY_LENGTH = 16  # 15 random characters plus one check character


def _key_check(body):
    total = 0
    for char in body:
        index = KEY_ALPHABET.find(char)
        if index < 0:
            return None
        total = (total * 3 + index) % len(KEY_ALPHABET)
    return KEY_ALPHABET[total]


def normalise_key(text):
    """Tidy whatever was typed into the canonical key, or None.

    Accepts any case, missing or extra dashes, spaces, and a pasted MADI-
    prefix or none. Deliberately does NOT "correct" characters outside the
    alphabet: O/0 and I/L/1 are excluded precisely because they are ambiguous,
    and guessing could turn one valid key into a different valid key.
    """
    if not isinstance(text, str):
        return None
    raw = "".join(ch for ch in text.upper() if ch.isalnum())
    if raw.startswith("MADI"):
        raw = raw[4:]
    if len(raw) != KEY_LENGTH:
        return None
    if any(ch not in KEY_ALPHABET for ch in raw):
        return None
    if _key_check(raw[:-1]) != raw[-1]:
        return None
    return "MADI-" + "-".join(raw[i:i + 4] for i in range(0, KEY_LENGTH, 4))


_SSL_CONTEXT = None


def ssl_context():
    """TLS trust that does not depend on the machine being up to date.

    Built from two sources, in this order of importance:

    1. **The roots we ship** (`roots.py`). A Windows install with automatic
       root update disabled, or one that simply has not updated in years, can
       be missing the root entirely - and then nothing else works. Carrying it
       means a customer's activation never depends on the state of their
       certificate store.

    2. **The OS store, minus anything expired.** Keeps us working if the host
       ever changes CA. The filter is not cosmetic: a fully updated Windows 11
       machine was found carrying an expired ISRG Root X2 (Sep 2025), which
       OpenSSL 1.1.1 chose as the anchor and then rejected the connection over,
       instead of using the valid cross-signed one the server sends. An expired
       certificate is not a valid trust anchor, so dropping it is strictly more
       correct, not a loosening.

    Falls back to the plain default context if anything here goes wrong -
    better a working default than an exception on the way to a network call.
    """
    global _SSL_CONTEXT
    if _SSL_CONTEXT is not None:
        return _SSL_CONTEXT
    try:
        default = ssl.create_default_context()
        pem = [roots.verified_pem()]
        try:
            parsed = default.get_ca_certs()
            ders = default.get_ca_certs(binary_form=True)
            now = time.time()
            for info, der in zip(parsed, ders):
                try:
                    expires = calendar.timegm(
                        time.strptime(info["notAfter"], "%b %d %H:%M:%S %Y %Z"))
                except Exception:
                    # Unparseable date: keep it. We only drop PROVEN expiries.
                    expires = now + 1
                if expires > now:
                    pem.append(ssl.DER_cert_to_PEM_cert(der))
        except Exception:
            pass
        blob = "".join(p for p in pem if p)
        _SSL_CONTEXT = ssl.create_default_context(cadata=blob) if blob else default
    except Exception:
        _SSL_CONTEXT = ssl.create_default_context()
    return _SSL_CONTEXT


def _post(path, body):
    data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        SERVER_URL + path, data=data, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT,
                                context=ssl_context()) as response:
        return json.loads(response.read().decode("utf-8"))


def _get(path):
    request = urllib.request.Request(SERVER_URL + path)
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT,
                                context=ssl_context()) as response:
        return json.loads(response.read().decode("utf-8"))


def _call(fn, *args):
    """Run a request, turning every failure into a dict instead of an exception.

    An HTTP error still carries a signed body (the server signs refusals), so it
    must be read rather than thrown away — that is how a revoke reaches us.
    """
    try:
        return fn(*args)
    except urllib.error.HTTPError as err:
        try:
            return json.loads(err.read().decode("utf-8"))
        except Exception:
            return {"ok": False, "reason": "http_%d" % err.code}
    except Exception:
        return {"ok": False, "reason": "unreachable"}


def _expiry_of(blob):
    """`entitled_until` as an int, or None for "never expires".

    ⚠ `isinstance(True, int)` IS TRUE IN PYTHON, and `now >= True` is true for
    any real timestamp — so a blob carrying `entitled_until: true` would read as
    long expired and lock out a paying customer. The same bool trap is already
    documented on the update channel's size field, where both sides reject bools
    explicitly; this is the licensing half of that rule.
    """
    value = blob.get("entitled_until")
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def check_blob(payload, sig_b64, nonce):
    """Verify a signed answer. Returns the parsed blob, or None.

    Three things must hold, and all three are load-bearing:
      * the signature is genuine   -> a fake server cannot answer for us
      * OUR nonce is inside it     -> a captured "yes" cannot be replayed
      * not_after is absolute      -> the client never computes an expiry
    """
    try:
        import base64

        if not payload or not sig_b64:
            return None
        key = base64.b64decode(PUBLIC_KEY)
        sig = base64.b64decode(sig_b64)
        if not ed25519.verify(key, payload.encode("utf-8"), sig):
            return None
        blob = json.loads(payload)
        if not isinstance(blob, dict):
            return None
        if nonce is not None and blob.get("nonce") != nonce:
            return None
        if not isinstance(blob.get("not_after"), int):
            return None
        return blob
    except Exception:
        return None


class _Task(QThread):
    """One background request. Kept trivially simple: the manager owns the
    reference, the result comes back as a signal on the GUI thread."""

    done = Signal(object)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self._fn = fn
        self.cancelled = False

    def run(self):
        try:
            result = self._fn(self)
        except Exception as err:  # a worker thread that dies silently is a bug
            result = {"ok": False, "reason": "client_error", "detail": str(err)}
        if not self.cancelled:
            self.done.emit(result)


class LicenseManager(QObject):
    stateChanged = Signal(str)
    busyChanged = Signal(bool)
    messageChanged = Signal(str)
    pairingStarted = Signal(str, str)  # code, link url
    # Fired when an online re-check FINISHES, carrying the state it settled on.
    # stateChanged is not enough for a caller that needs a fresh answer: it only
    # fires when the state actually changes, so "still active" is silent — and
    # "still active, confirmed just now" is exactly what the updater must wait
    # for before it will install anything.
    checkFinished = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._state = DEV if not is_gated() else UNLICENSED
        self._message = ""
        self._record = {}
        self._task = None
        self._pair_task = None

    # ------------------------------------------------------------- state

    @property
    def state(self):
        return self._state

    @property
    def message(self):
        return self._message

    @property
    def unlocked(self):
        return self._state in UNLOCKED_STATES

    @property
    def entitlement(self):
        return self._record.get("ent")

    @property
    def entitled_until(self):
        """When the paid year runs out, as unix seconds - or None for never.

        Read from inside the SIGNED blob rather than from the record around it,
        so a date cannot be edited on disk to buy more time.
        """
        blob = check_blob(self._record.get("payload"), self._record.get("sig"), None)
        return _expiry_of(blob) if blob else None

    @property
    def days_left(self):
        """Whole days until expiry; None if it never expires, negative if past."""
        until = self.entitled_until
        if until is None:
            return None
        return int((until - time.time()) // 86400)

    @property
    def expiring_soon(self):
        """Worth telling the user about, and not yet expired."""
        left = self.days_left
        return left is not None and 0 <= left <= EXPIRY_WARNING_DAYS

    def expiry_message(self):
        """What to say when the year has run out.

        Deliberately NOT the revoked wording. Someone whose licence lapsed has
        done nothing wrong, and "contact support" is the wrong instruction when
        the fix is a Patreon pledge and a sign-in.
        """
        # ⚠ NAMES THE TIER, because renewal is the one case where the answer
        # is narrower than "support on Patreon": cumulative spend earns a
        # FIRST licence but cannot renew one (`config.js renewRequiresTier`),
        # so telling a lapsed patron to "support on Patreon" could send
        # somebody who has already paid $19 in $5 pledges round a loop that
        # will not work.
        return ("Your licence has run out. A current Tier 3 pledge on Patreon "
                "renews it - support and sign in again to unlock the paid tabs "
                "for another year. Everything you have saved is untouched.")

    @property
    def has_token(self):
        """Is there a licence here at all to re-check?

        Without one, `recheck()` returns immediately and does nothing — so any
        control that offers it must hide itself, or it is a dead button in the
        exact state most people see."""
        return bool(self._record.get("token"))

    def _set(self, state, message=""):
        changed = state != self._state
        self._state = state
        self._message = message
        if message:
            self.messageChanged.emit(message)
        if changed:
            self.stateChanged.emit(state)

    def start(self):
        """Evaluate what we already hold. No network, so it is instant and the
        window is never held up by a licence check."""
        # ⚠ THE STORED LICENCE IS LOADED EVEN FROM SOURCE, and that does not
        # contradict the DEV line below. There are TWO gates, and only one of
        # them is ours to switch off:
        #   * the APP's tabs, ungated from source so the dev loop is untouched;
        #   * the ADD-ON's paid panels, which are a separate gate inside Blender
        #     and can only be opened by the real server-signed blob.
        # Returning before this load left `_record` empty, so `_push_license`
        # found nothing to send and EVERY paid Blender panel stayed locked in
        # EVERY source run - the Anim Layers N-panel, the Bone picker and the
        # Scene Optimizer alike. It went unnoticed because all three were only
        # ever proven against the gated exe, which does load it.
        # Loading it here is not a bypass: with no licence stored, `_record`
        # stays empty and the add-on stays locked, exactly as it should.
        self._record = store.load()
        if not is_gated():
            self._set(DEV, "Running from source - licensing is off")
            return
        self._evaluate()
        # ⚠ EVERY LAUNCH ASKS (Marty, 2026-08-06: "check license every time user
        # open the app"). It used to ask at most weekly. This costs one small
        # request off the GUI thread and buys two things a weekly check could
        # not: a revoke or an expiry lands the next time the app opens rather
        # than up to seven days later, and a licence that lapsed while the
        # machine was offline recovers the moment it is online again.
        #
        # EXPIRED is in this list on purpose - re-asking is exactly how a
        # renewal gets noticed, and without it someone who renewed on Patreon
        # would sit looking at a locked tab wondering why.
        if self._state in (ACTIVE, STALE, GRACE_EXPIRED, EXPIRED, CLOCK_TAMPER):
            self.recheck(quiet=True)

    def _evaluate(self):
        record = self._record
        blob = check_blob(record.get("payload"), record.get("sig"), None)
        if not blob:
            self._set(UNLICENSED)
            return
        if blob.get("revoked"):
            self._set(REVOKED, "This licence was withdrawn. Contact support.")
            return
        if not blob.get("active"):
            reason = blob.get("reason")
            if reason == "seat_in_use":
                self._set(SEAT_CONFLICT, "Your licence is in use on another computer.")
            elif reason == "expired":
                # The SERVER said the year is up. Same state the local
                # `entitled_until` check reaches on its own further down - both
                # paths exist because either one can be first: the server
                # answers when we are online, the stored date when we are not.
                self._set(EXPIRED, self.expiry_message())
            else:
                self._set(UNLICENSED)
            return

        now = time.time()
        high_water = int(record.get("clock_high_water") or 0)
        if high_water and now < high_water - CLOCK_SLACK:
            # Not an accusation - just a reason to go and ask the server.
            self._set(CLOCK_TAMPER, "The system clock moved backwards - rechecking.")
            return

        # ⚠ EXPIRY IS CHECKED BEFORE not_after, AND WITH NO NETWORK.
        # `entitled_until` is inside the signature, so this holds on a machine
        # that never goes online again. Checking it second would mean an expired
        # licence read as merely "not confirmed recently", which is a different
        # message and a different fix.
        #
        # A blob from a server too old to send the field has None here, which
        # means "no expiry" - the same decay rule as the server's, so a partial
        # rollout cannot lock anybody out.
        entitled_until = _expiry_of(blob)
        if entitled_until is not None and now >= entitled_until:
            self._set(EXPIRED, self.expiry_message())
            return

        not_after = int(blob["not_after"])
        if now <= not_after:
            self._set(ACTIVE)
            return

        # Past not_after: we have not had a straight answer in 30 days. Marty's
        # rule - this locks the paid tabs until a check succeeds. STALE and
        # GRACE_EXPIRED both still exist so the MESSAGE can distinguish "we will
        # retry in a moment" from "this has been going on a while", but neither
        # unlocks anything now.
        if now <= not_after + 7 * 86400:
            self._set(STALE,
                      "Your licence has not been confirmed for 30 days, so the paid "
                      "tabs are locked until it is. Connect to the internet and they "
                      "come straight back.")
        else:
            self._set(GRACE_EXPIRED,
                      "Your licence has not been confirmed in over a month. Connect "
                      "to the internet to unlock the paid tabs again - nothing you "
                      "have saved is affected.")

    # ------------------------------------------------------- online check

    def recheck(self, quiet=False):
        """Ask the server. Never blocks: the answer arrives as a signal.

        Returns True if a request was actually started. A caller that WAITS for
        checkFinished needs to know when it will never come, or it sits in a
        "checking..." state forever.
        """
        if not is_gated() or self._task is not None:
            return False
        token = self._record.get("token")
        if not token:
            self._set(UNLICENSED)
            return False
        nonce = secrets.token_urlsafe(24)
        components = machine.components()

        def work(_task):
            return _call(_post, "/verify",
                         {"token": token, "nonce": nonce, "components": components})

        self._run(work, lambda result: self._on_verify(result, nonce, quiet))
        return True

    def _on_verify(self, result, nonce, quiet):
        try:
            self._verify_result(result, nonce, quiet)
        finally:
            # Always, including on an unreachable server: a caller waiting on
            # this must be told the attempt is over, whatever the answer was.
            self.checkFinished.emit(self._state)

    def _verify_result(self, result, nonce, quiet):
        blob = check_blob(result.get("payload"), result.get("sig"), nonce)
        if blob is None:
            # Unreachable, or an answer we cannot trust. Either way we keep
            # whatever we already had - an outage must not lock anyone out.
            if not quiet:
                self.messageChanged.emit("Could not reach the licence server.")
            self._evaluate()
            return
        self._record["payload"] = result["payload"]
        self._record["sig"] = result["sig"]
        self._record["last_check"] = int(time.time())
        self._record["ent"] = blob.get("sub") or self._record.get("ent")
        store.touch_clock(self._record)
        store.save(self._record)
        self._evaluate()
        if self._state == ACTIVE and not quiet:
            self.messageChanged.emit("Licence confirmed.")

    # ------------------------------------------------------------ pairing

    def unlock(self):
        """Start the Patreon flow: get a code, open the browser, then poll."""
        if self._pair_task is not None:
            return
        self._set(LINKING, "Opening Patreon in your browser...")

        def work(task):
            made = _call(_post, "/pair/new", {})
            if not made.get("ok"):
                return {"ok": False, "reason": made.get("reason") or made.get("error")
                        or "unreachable"}
            code, link = made["code"], made["link_url"]
            self.pairingStarted.emit(code, link)
            deadline = time.time() + PAIR_TIMEOUT
            while time.time() < deadline and not task.cancelled:
                time.sleep(PAIR_POLL_SECONDS)
                if task.cancelled:
                    return {"ok": False, "reason": "cancelled"}
                status = _call(_get, "/pair/status?c=" + code)
                state = status.get("state")
                if state == "ok":
                    return self._activate(status["token"])
                if state in ("denied", "expired", "lost", "unknown"):
                    return {"ok": False, "reason": status.get("reason") or state}
            return {"ok": False, "reason": "timeout"}

        self._run(work, self._on_pair, pairing=True)

    def _activate(self, token):
        """Bind this machine. Runs inside the pairing worker."""
        nonce = secrets.token_urlsafe(24)
        result = _call(_post, "/activate", {
            "token": token,
            "nonce": nonce,
            "components": machine.components(),
            "label": machine.label(),
        })
        result["_token"] = token
        result["_nonce"] = nonce
        return result

    def _on_pair(self, result):
        self._pair_task = None
        if result.get("ok"):
            blob = check_blob(result.get("payload"), result.get("sig"), result.get("_nonce"))
            if blob is None:
                self._set(UNLICENSED, "The server's answer could not be verified.")
                return
            self._record = {
                "token": result["_token"],
                "payload": result["payload"],
                "sig": result["sig"],
                "ent": blob.get("sub"),
                "last_check": int(time.time()),
                "label": machine.label(),
            }
            store.touch_clock(self._record)
            store.save(self._record)
            self._evaluate()
            self.messageChanged.emit("Thank you for your support - everything is unlocked.")
            return

        reason = result.get("reason")
        if reason == "seat_in_use":
            # Not a failure: they own this, it is just somewhere else.
            self._record.update({
                "token": result.get("_token") or self._record.get("token"),
                "payload": result.get("payload"),
                "sig": result.get("sig"),
            })
            store.touch_clock(self._record)
            store.save(self._record)
            seat = result.get("seat") or {}
            self._set(SEAT_CONFLICT,
                      "Your licence is already active on %s."
                      % (seat.get("label") or "another computer"))
            return
        self._set(UNLICENSED, self._explain(reason))

    def redeem(self, key):
        """Unlock with a manually issued licence key - no browser, no Patreon.

        The same key works again after a reinstall: the server returns the SAME
        entitlement rather than treating it as a second sale.
        """
        if self._task is not None:
            return
        normalised = normalise_key(key)
        if not normalised:
            self.messageChanged.emit(
                "That key doesn't look right. Check it and try again - it looks "
                "like MADI-XXXX-XXXX-XXXX-XXXX.")
            return
        nonce = secrets.token_urlsafe(24)
        self.messageChanged.emit("Checking your key...")

        def work(_task):
            result = _call(_post, "/redeem", {
                "key": normalised,
                "nonce": nonce,
                "components": machine.components(),
                "label": machine.label(),
            })
            result["_token"] = result.get("token")
            result["_nonce"] = nonce
            return result

        # The landing is identical to the Patreon flow - same token, same
        # signed blob, same seat rules - so it deliberately shares the handler.
        self._run(work, self._on_pair)

    def cancel_unlock(self):
        if self._pair_task is not None:
            self._pair_task.cancelled = True
            self._pair_task = None
        self._set(UNLICENSED, "")

    # --------------------------------------------------------- seat moves

    def move_seat(self):
        """Self-serve 'move my licence to this computer'."""
        token = self._record.get("token")
        if not token or self._task is not None:
            return

        def work(_task):
            return _call(_post, "/seat/move", {
                "token": token,
                "nonce": secrets.token_urlsafe(24),
                "components": machine.components(),
                "label": machine.label(),
            })

        self._run(work, self._on_move)

    def _on_move(self, result):
        if result.get("ok"):
            blob = check_blob(result.get("payload"), result.get("sig"), None)
            if blob is not None:
                self._record["payload"] = result["payload"]
                self._record["sig"] = result["sig"]
                self._record["last_check"] = int(time.time())
                store.touch_clock(self._record)
                store.save(self._record)
            self._evaluate()
            self.messageChanged.emit("Your licence now lives on this computer.")
            return
        reason = result.get("reason")
        if reason in ("too_soon", "yearly_limit"):
            when = result.get("next_at")
            self.messageChanged.emit(
                "You can move your licence again %s."
                % (time.strftime("on %d %b %Y", time.localtime(when)) if when else "later")
            )
        else:
            self.messageChanged.emit(self._explain(reason))

    def unlink(self):
        """Forget the licence on this machine. The seat stays claimed on the
        server — this is not a way to hand the seat back, and saying otherwise
        would make 'unlink' an unlimited seat-reset button."""
        store.clear()
        self._record = {}
        self._set(UNLICENSED, "This computer no longer holds your licence details.")

    # ------------------------------------------------------------ plumbing

    def shutdown(self, timeout_ms=4000):
        """Stop any in-flight request and WAIT for its thread to finish.

        ⚠ QT ABORTS THE WHOLE PROCESS IF A RUNNING QThread IS DESTROYED
        (`0xC0000409` on Windows, with no traceback because a windowed build has
        nowhere to print one). That is not theoretical: the moment the licence
        check moved to EVERY LAUNCH (2026-08-06), `--smoke` began crashing —
        it builds the window and returns immediately, while the check is still
        somewhere in DNS or a TLS handshake.

        ⚠ AND `--smoke` IS NOT JUST A TEST. `updater\\swap.smoke()` runs exactly
        that to decide whether to KEEP an update or roll it back, so a build
        that cannot survive it would make every update undo itself. The old
        weekly check hid this: with a fresh `last_check` no thread was started,
        so nothing was ever destroyed mid-flight.

        Waiting is the fix rather than skipping the check in smoke mode,
        because the same race exists for any user who opens the app and closes
        it a second later - a window that is now open on every single launch.
        """
        for attr in ("_task", "_pair_task"):
            task = getattr(self, attr, None)
            if task is None:
                continue
            # Tells the worker its result is unwanted, so no signal fires into
            # a half-destroyed window. It does NOT interrupt the socket - the
            # request has its own timeout and wait() is bounded anyway.
            task.cancelled = True
            try:
                task.wait(timeout_ms)
            except Exception:
                pass
            setattr(self, attr, None)

    def _run(self, work, on_done, pairing=False):
        task = _Task(work, self)
        if pairing:
            self._pair_task = task
        else:
            self._task = task

        def finished(result):
            if pairing:
                self._pair_task = None
            else:
                self._task = None
            self.busyChanged.emit(False)
            on_done(result)

        task.done.connect(finished)
        task.finished.connect(task.deleteLater)
        self.busyChanged.emit(True)
        task.start()

    @staticmethod
    def _explain(reason):
        return {
            "no_membership": "That Patreon account isn't supporting MadihsonNSFW "
                             "yet - the paid tabs come with Tier 3.",
            # ⚠ "unlocks permanently" was left over from perpetual licences and
            # stopped being true on 2026-08-06. Said to somebody deciding
            # whether to upgrade, it is the worst sentence in the app to have
            # wrong.
            "below_tier": "The paid tabs come with Tier 3 - upgrade your pledge "
                          "on Patreon and they unlock for a year.",
            "no_completed_charge": "Patreon hasn't taken the first payment yet - "
                                   "try again once it goes through.",
            "revoked": "This licence was withdrawn. Contact support.",
            "suspended": "This licence is suspended. Contact support.",
            "blocked": "This computer can't be activated. Contact support.",
            "unknown_token": "This licence wasn't recognised - please link again.",
            "bad_key": "That key wasn't recognised. Check it and try again.",
            "key_revoked": "That key is no longer valid. Contact support.",
            "too_many_attempts": "Too many tries. Wait a little while, then "
                                 "try again.",
            "patreon_slow": "Patreon didn't answer in time. Nothing is wrong with "
                            "your pledge - please try again.",
            "timeout": "That took too long. Please try again.",
            "cancelled": "",
            "unreachable": "Could not reach the licence server. Check your connection.",
            "not_configured": "The licence server isn't ready yet.",
        }.get(reason, "Something went wrong. Please try again.")
