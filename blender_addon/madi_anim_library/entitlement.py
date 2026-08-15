"""Whether this Blender session may use the paid tools.

The Toolset app hands over the licence blob its server signed, and this verifies
it against the same Ed25519 public key the app carries. Until that happens the
paid panels say so and do nothing.

⚠ READ THIS BEFORE RELYING ON IT. The add-on ships as READABLE PYTHON, so anyone
willing to open this file can delete the check. That is not a flaw to be fixed -
it is what shipping source means, the same ceiling as the geometry nodes
(`docs\\nsfw.md`). What the gate actually buys:

  * a normal user cannot use the paid panels without running the licensed app,
  * a FAKE licence is not possible - the blob is signed by our server's key and
    verified here, so nothing can be hand-written into it,
  * an expired or revoked licence stops working on its own (`not_after` is
    absolute and comes from the server, never computed here).

⚠ ANNUAL LICENCES NEEDED NO CHANGE IN HERE, and that is by construction rather
than by luck. Since 2026-08-06 a licence also carries `entitled_until` (the paid
year), and the server CAPS `not_after` at that date - so a blob issued the day
before someone's year ends is only good until their year ends. This file keeps
checking the one field it always checked and expires the panels on exactly the
right day. Do not "fix" this by reading `entitled_until` here as well: the cap
is what makes an older add-on behave correctly too, and two places enforcing the
same deadline is two places to get it wrong.

What it does NOT buy: protection against someone editing this file, and it
cannot detect a blob captured from a licensed machine and replayed on another
within its lifetime - the server's blob carries no machine fingerprint (the seat
binding happens server-side, at the point the blob is issued). Both are strictly
easier than editing the source, so neither is the binding constraint.

SESSION-SCOPED ON PURPOSE. The unlock lives in memory and dies with Blender or
with an add-on reload. Persisting it would mean writing "unlocked" somewhere a
text editor could reach, which is a worse lie than asking the app once.
"""

import base64
import json
import time

from . import ed25519

# The same key the app verifies with (app\licensing\manager.PUBLIC_KEY). Public
# by definition - it can only CHECK a signature, never make one.
PUBLIC_KEY = "MbYPKzKddZcjdchpdZXPddVCSkJi2LLVbPyBdh65n3s="

_STATE = {"unlocked": False, "sub": None, "not_after": 0, "reason": "not asked"}


def state():
    """What we know, and when it stops being true."""
    out = dict(_STATE)
    if out["unlocked"] and out["not_after"] and time.time() > out["not_after"]:
        # Expiry is checked on READ as well as on unlock: a session left open
        # for days must not keep a licence alive past its own deadline.
        out = {"unlocked": False, "sub": out["sub"], "not_after": out["not_after"],
               "reason": "expired"}
        _STATE.update(out)
    return out


def unlocked():
    return bool(state()["unlocked"])


def lock(reason="locked"):
    _STATE.update(unlocked=False, sub=None, not_after=0, reason=reason)
    return state()


def unlock(payload, sig):
    """Verify a server-signed licence blob and open the paid panels.

    Everything here is a reason to REFUSE; there is no path that trusts the
    caller. The app sends the exact bytes it stored, so what is verified is what
    the server signed - re-serialising the JSON first would change the bytes and
    break the signature.
    """
    if not isinstance(payload, str) or not isinstance(sig, str) or not payload:
        return lock("malformed")
    try:
        key = base64.b64decode(PUBLIC_KEY)
        signature = base64.b64decode(sig)
    except Exception:                       # noqa: BLE001 - any bad base64
        return lock("malformed")
    try:
        if not ed25519.verify(key, payload.encode("utf-8"), signature):
            return lock("bad signature")
        blob = json.loads(payload)
    except Exception:                       # noqa: BLE001
        return lock("bad signature")
    if not isinstance(blob, dict):
        return lock("malformed")
    if blob.get("revoked"):
        return lock("revoked")
    if not blob.get("active"):
        return lock(str(blob.get("reason") or "not active"))
    not_after = blob.get("not_after")
    if not isinstance(not_after, int):
        return lock("no expiry")            # never guess one
    if time.time() > not_after:
        return lock("expired")
    _STATE.update(unlocked=True, sub=blob.get("sub") or None,
                  not_after=not_after, reason="ok")
    return state()
