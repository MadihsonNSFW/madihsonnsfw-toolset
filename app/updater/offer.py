"""Reading and checking an update offer.

AN UPDATE CHANNEL IS A REMOTE-CODE-EXECUTION CHANNEL. Everything in this file
exists because of that one sentence: by the time an offer is accepted, the app
has agreed to replace its own executable with bytes from the network. So the
offer is checked here, completely, before anything is downloaded - and every
downloaded byte is checked again against the hashes this file returns.

Four things must all hold, and each one closes a different door:

  the signature is genuine   a fake server, a hosts-file redirect or a proxy
                             cannot offer us anything at all
  OUR nonce is inside it     a captured offer cannot be replayed at us later
  not_after has not passed   an offer is a live answer, not a file to keep
  it is strictly NEWER       anti-rollback. An OLD release carries a perfectly
                             valid signature forever, so signature checking
                             cannot catch this and version comparison must.

The paths are then re-validated here even though the server validates them on
import, because this is the process that would actually write them to disk.
A server that has been compromised, or simply changed by someone in a hurry,
must not be able to talk this side into writing outside the app folder.
"""

import re
import time
import zipfile

import version
from licensing import manager as lic

# ---------------------------------------------------------------------------
# The Blender add-on package
# ---------------------------------------------------------------------------
# ⚠ A VERIFIED HASH ONLY PROVES THE BYTES ARRIVED. It says nothing about
# whether Blender can install them, and on 2026-08-14 that difference cost an
# evening: 0.45.0's `blender_manifest.toml` had picked up a UTF-8 BOM, Blender's
# TOML reader refused it at line 1 column 1, and
# `bpy.ops.extensions.package_install_files()` reported that through Blender's
# own report system while STILL RETURNING {'FINISHED'}. So the install silently
# did nothing, the add-on reloaded the old version over itself, and the app
# waited 90 s for a version that was never coming and then blamed Blender for
# "not coming back".
#
# This is the app's half of the fix: refuse a package we can already see is
# broken, before Blender is ever asked. The add-on checks the same things again
# on arrival (`selfupdate.inspect_package`) - neither side trusts the other,
# which is the same rule the hash follows.
MANIFEST_NAME = "blender_manifest.toml"


def _manifest_entry(names):
    """The manifest inside a package: at the root, or one folder down."""
    nested = None
    for name in names:
        parts = name.split("/")
        if parts[-1] != MANIFEST_NAME:
            continue
        if len(parts) == 1:
            return name
        if len(parts) == 2 and nested is None:
            nested = name
    return nested


def inspect_addon_package(path, expect_version=None):
    """`{"id", "version"}` for an extension zip, or ValueError saying why not.

    Deliberately NOT a TOML parser: the app runs on a Python without `tomllib`,
    and the point here is to catch what would make Blender refuse the package,
    not to re-implement Blender. The BOM is checked BY NAME because a generic
    "could not read the manifest" is what sent the last investigation looking
    at a file that was perfectly valid TOML.
    """
    try:
        with zipfile.ZipFile(path) as bundle:
            entry = _manifest_entry(bundle.namelist())
            if entry is None:
                raise ValueError("there is no %s in it, so it is not a Blender "
                                 "extension" % MANIFEST_NAME)
            raw = bundle.read(entry)
    except ValueError:
        raise
    except zipfile.BadZipFile as exc:
        raise ValueError("it is not a readable zip (%s)" % exc)
    except OSError as exc:
        raise ValueError("it could not be read (%s)" % exc)

    if raw[:3] == b"\xef\xbb\xbf":
        raise ValueError(
            "its %s starts with a UTF-8 BOM, which Blender's TOML reader "
            "refuses - the extension would install nothing at all"
            % MANIFEST_NAME)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("its %s is not valid UTF-8 (%s)" % (MANIFEST_NAME, exc))

    found = {}
    for key in ("id", "version"):
        match = re.search(r'^\s*%s\s*=\s*"([^"]+)"' % key, text, re.M)
        if match:
            found[key] = match.group(1)
    missing = [key for key in ("id", "version") if key not in found]
    if missing:
        raise ValueError("its %s declares no %s"
                         % (MANIFEST_NAME, " and no ".join(missing)))
    if expect_version and found["version"] != expect_version:
        raise ValueError("it contains add-on %s, not the %s it was offered as"
                         % (found["version"], expect_version))
    return found

# Sanity bounds. A signed manifest can only come from us, so these are not the
# security boundary - they are the difference between a mistake being a failed
# update and a mistake filling somebody's disk.
MAX_FILES = 4000
MAX_TOTAL_BYTES = 2 * 1024 * 1024 * 1024
MAX_PATH_LEN = 200

_HEX = frozenset("0123456789abcdef")


def is_hash(value):
    return (isinstance(value, str) and len(value) == 64
            and all(ch in _HEX for ch in value))


def safe_relpath(value):
    """The path as a clean relative POSIX path, or None if it is not safe.

    Deliberately a WHITELIST of shapes rather than a sanitising pass: stripping
    the bad parts out of a hostile path is how "..\\..\\windows\\system32" turns
    into something that still escapes. Anything not obviously fine is refused.
    """
    if not isinstance(value, str) or not value or len(value) > MAX_PATH_LEN:
        return None
    # One separator, decided by the server, converted by us. A backslash here
    # would be a literal character in a filename on Linux and a separator on
    # Windows - the exact ambiguity that made a zip extract flat once already.
    if "\\" in value:
        return None
    if value.startswith("/"):
        return None
    if len(value) > 1 and value[1] == ":":  # C:\... or C:/...
        return None
    parts = value.split("/")
    for part in parts:
        if part in ("", ".", ".."):
            return None
        # Windows silently strips a trailing dot or space, so "evil." and
        # "evil" are the same file to the OS but different strings to us.
        if part != part.strip() or part.endswith("."):
            return None
        if any(ord(ch) < 32 for ch in part):
            return None
    return "/".join(parts)


class Offer:
    """A verified, self-consistent description of what to install."""

    def __init__(self, version_, min_version, released, files, addon,
                 app_newer=True, addon_newer=False):
        self.version = version_
        self.min_version = min_version
        self.released = released
        self.files = files          # list of {"path", "sha256", "size"}
        self.addon = addon          # {"version", "path", "sha256", "size"} or None
        # Which HALF of this release is actually worth installing here. A
        # release carries the app and the add-on together, but they move on
        # their own schedules - so an app that is already current can still be
        # sitting on an out-of-date add-on, and vice versa.
        self.app_newer = app_newer
        self.addon_newer = addon_newer

    @property
    def total_bytes(self):
        return sum(f["size"] for f in self.files)

    def __repr__(self):
        return "<Offer %s, %d files, addon %s>" % (
            self.version, len(self.files), self.addon["version"] if self.addon else "-")


def _entry(raw, seen):
    path = safe_relpath(raw.get("path")) if isinstance(raw, dict) else None
    if path is None or path in seen:
        return None
    if not is_hash(raw.get("sha256")):
        return None
    size = raw.get("size")
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        return None
    seen.add(path)
    return {"path": path, "sha256": raw["sha256"], "size": size}


def parse(payload, sig, nonce, current_version, current_addon=None, now=None):
    """Verify a signed offer. Returns (Offer, None) or (None, reason).

    `current_addon` is the version of the add-on Blender currently has, when it
    is known. Passing it lets a release be worth taking because the ADD-ON is
    behind even though the app is current - without it, someone whose app is up
    to date could never be offered a newer add-on.

    `reason` is for the log and the developer console, never for a dialog: the
    user gets "no update" or "something went wrong", because the detail of WHY
    a signature failed is only useful to someone attacking it.
    """
    blob = lic.check_blob(payload, sig, nonce)
    if blob is None:
        return None, "unverified"
    if blob.get("kind") != "update":
        # Domain separation. A licence blob is also correctly signed by this
        # key; without this line it would be accepted here as an "offer" and
        # every field would then read as missing.
        return None, "not_an_update"
    if blob.get("v") != 1:
        return None, "unknown_format"

    at = time.time() if now is None else now
    if at > blob["not_after"]:
        return None, "expired"

    offered = blob.get("version")
    app_newer = version.is_newer(offered, current_version)
    raw_addon_version = (blob.get("addon") or {}).get("version") \
        if isinstance(blob.get("addon"), dict) else None
    addon_newer = bool(current_addon) and version.is_newer(raw_addon_version, current_addon)
    if not (app_newer or addon_newer):
        # ANTI-ROLLBACK, and it covers both halves: an older release carries a
        # perfectly valid signature forever, so this comparison is the only
        # thing that can refuse it.
        return None, "not_newer"
    min_version = blob.get("min_version") or "0.0.0"
    if version.is_newer(min_version, current_version):
        # This build is too old to take this update directly - it would need
        # an intermediate one. Better to say so than to install half of it.
        return None, "too_old"

    entries = blob.get("app")
    if not isinstance(entries, list) or not entries:
        return None, "no_files"
    if len(entries) > MAX_FILES:
        return None, "too_many_files"
    files = []
    seen = set()
    for raw in entries:
        item = _entry(raw, seen)
        if item is None:
            return None, "bad_file_entry"
        files.append(item)
    if sum(f["size"] for f in files) > MAX_TOTAL_BYTES:
        return None, "too_large"

    addon = None
    raw_addon = blob.get("addon")
    if raw_addon:
        if not isinstance(raw_addon, dict):
            return None, "bad_addon"
        name = raw_addon.get("path")
        # A bare file name: the add-on is handed to Blender as one zip, so a
        # path here would mean nothing except an attempt to write elsewhere.
        if safe_relpath(name) != name or "/" in (name or ""):
            return None, "bad_addon"
        if not is_hash(raw_addon.get("sha256")):
            return None, "bad_addon"
        size = raw_addon.get("size")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            return None, "bad_addon"
        if not version.parse(raw_addon.get("version") or ""):
            return None, "bad_addon"
        addon = {"version": raw_addon["version"], "path": name,
                 "sha256": raw_addon["sha256"], "size": size}

    return Offer(offered, min_version, blob.get("released"), files, addon,
                 app_newer=app_newer, addon_newer=addon_newer), None
