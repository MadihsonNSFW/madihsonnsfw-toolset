"""Licensing: Patreon-backed unlock for everything past Studio Library.

Studio Library, Rendering and What's New are free forever. The other six tabs
need a licence, which is earned by supporting on Patreon or by redeeming a key,
and which lasts A YEAR — renewed by proving you still qualify.

⚠ IT USED TO BE PERPETUAL, and a lot of the reasoning in here was shaped by
that. Marty changed it on 2026-08-06, along with the offline rule that went with
it: thirty days without a successful check now locks the paid tabs, where it
used to leave them open indefinitely. See `manager.UNLOCKED_STATES`.

Standard library only (plus PySide6, which the app already has). See
`..\\..\\LICENSING_PLAN.md` for the policy and `manager.py` for the two rules
this package exists to keep: nothing runs on the GUI thread, and the client is
never the judge.
"""

from .lock import LockedPage
from .manager import (ACTIVE, CLOCK_TAMPER, DEV, EXPIRED, EXPIRY_WARNING_DAYS,
                      GRACE_EXPIRED, LINKING, REVOKED, SEAT_CONFLICT, STALE,
                      UNLICENSED, UNLOCKED_STATES, LicenseManager, is_gated)

__all__ = [
    "LicenseManager", "LockedPage", "is_gated",
    "ACTIVE", "CLOCK_TAMPER", "DEV", "EXPIRED", "GRACE_EXPIRED", "LINKING",
    "REVOKED", "SEAT_CONFLICT", "STALE", "UNLICENSED",
    # Exported so callers ask "is this state unlocked?" instead of listing the
    # states themselves. Every hand-written copy of that list is a place that
    # will not be updated the next time the set changes - which it just did.
    "UNLOCKED_STATES", "EXPIRY_WARNING_DAYS",
]
