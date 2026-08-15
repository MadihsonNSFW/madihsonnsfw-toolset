"""Self-update for the built app.

Public surface — everything else is an implementation detail:

    updater.UpdateManager(license_manager)   the state machine
    updater.is_supported()                   frozen build only
    updater.<STATE>                          the states below

Read `..\\..\\UPDATER_PLAN.md` before changing any of it; the two rules that
are load-bearing are that an update installs only on a FRESH `ACTIVE` licence,
and that nothing reaches the app folder until its bytes match a hash inside a
signature we verified.
"""

from . import swap  # noqa: F401  (main.py asks it what a download would cost)
from .manager import (AVAILABLE, CHECKING, DOWNLOADING, FAILED, IDLE,
                      INSTALLING, READY, UNSUPPORTED, UpdateManager,
                      is_supported)
from .offer import Offer

__all__ = [
    "UpdateManager", "Offer", "is_supported",
    "IDLE", "CHECKING", "AVAILABLE", "DOWNLOADING", "INSTALLING", "READY",
    "FAILED", "UNSUPPORTED",
]
