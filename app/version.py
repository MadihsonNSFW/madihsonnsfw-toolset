"""The app's own version, and the one place versions are compared.

WHY THIS EXISTS: until now nothing in `app\\` carried a version at all. The
self-updater cannot compare what it cannot read, and "is the offered build
newer than the running one" is the question the whole update channel turns on -
including its anti-rollback rule, which is the only replay defence a static
signed manifest can have (there is no nonce to echo in a file served from
disk). So the comparison lives HERE, once, and both the client and the tests
use it rather than each rolling their own string compare.

WHAT VERSIONS THERE ARE, and how they relate:
  * APP_VERSION      - this app. It is ALSO the release version: a release is
                       named after the app build it ships, because the app is
                       what updates itself and what the user sees.
  * ADDON_VERSION    - the Blender extension's own line, owned by
                       `core.ADDON_VERSION` / `blender_manifest.toml` /
                       `bridge.EXPECTED_ADDON_VERSION` (all three bumped
                       together). A release manifest carries it separately
                       because the add-on ships inside the same release but
                       moves on its own schedule.

Semantic-ish: MAJOR.MINOR.PATCH, integers only. Deliberately no pre-release or
build metadata - PEP 440 and semver disagree about how to order them, and a
sorting rule two implementations can read differently is exactly the kind of
thing that makes one customer's updater refuse a perfectly good build.
"""

# First versioned release. The app was shipping long before it had a number;
# 1.0.0 is where the counting starts rather than a claim about maturity.
APP_VERSION = "1.24.0"

# ---------------------------------------------------------------- identity
# Where the app says it came from and where to go for help. Kept HERE, in the
# one module that has no Qt import, because three unrelated places need them —
# the About box, the locked-tab panel, and the expiry notice — and three
# hand-typed copies of a URL is three chances to ship a dead link.
APP_NAME = "MadihsonNSFW Toolset"
AUTHOR = "MadihsonNSFW"

# Support goes to Patreon; bugs go to Discord. Two links doing two jobs, and the
# wording everywhere should keep them apart — someone with a crash to report
# does not want to be pointed at a pledge page.
PATREON_URL = "https://www.patreon.com/c/MadihsonNSFW"
# The membership page specifically — where the "Buy me a coffee" chip in the
# status bar goes (Marty, 2026-08-17). Deliberately its own constant rather
# than reusing PATREON_URL above: that one is the creator page and this one
# lands on the tiers.
# ⚠ Both `patreon.com/madihsonnsfw/membership` and `patreon.com/c/…` 308 to the
# same canonical page — checked, not assumed, because a donate button that goes
# nowhere is worse than no button.
PATREON_MEMBERSHIP_URL = "https://www.patreon.com/madihsonnsfw/membership"
# https, though the invite is usually written http: discord.gg redirects to TLS
# anyway, and shipping an http:// link in a desktop app is a needless downgrade.
DISCORD_URL = "https://discord.gg/EPcgrRkdhe"


def parse(text):
    """"1.2.3" -> (1, 2, 3). Returns None for anything that is not that shape.

    Strict on purpose. A version we cannot parse must not compare as "older"
    (that would offer a downgrade) nor as "newer" (that would install
    anything); callers treat None as "no answer" and do nothing.
    """
    if not isinstance(text, str):
        return None
    parts = text.strip().split(".")
    if len(parts) != 3:
        return None
    out = []
    for part in parts:
        # isdigit() is true for superscripts and other unicode digits, which
        # int() then happily accepts - so check the characters are ASCII.
        if not part or not all("0" <= ch <= "9" for ch in part):
            return None
        out.append(int(part))
    return tuple(out)


def is_newer(candidate, current):
    """Is `candidate` strictly newer than `current`?

    False when either side is unparseable, and False when they are equal. The
    updater installs only on True, so every uncertain case means "leave it
    alone" - which is also the anti-rollback rule: a signed manifest for an
    OLDER build is a valid signature and must still be refused, or an old build
    with a known hole can be re-served forever.
    """
    a = parse(candidate)
    b = parse(current)
    if a is None or b is None:
        return False
    return a > b
