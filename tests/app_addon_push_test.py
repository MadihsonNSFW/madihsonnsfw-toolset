# The Blender add-on push: `app\addon_push.py`.
#
# ⚠ THIS SUITE IS THE SURVIVING HALF OF `app_updater_test.py`, which went with
# the self-updater in 1.19.0. Everything here is about the ONE local operation
# that remained — handing Blender the extension this build carries — and every
# check below exists because the thing it guards actually went wrong once.
#
# The three that matter most, in the order they were learned:
#   * a package Blender would refuse is refused HERE, before Blender (a UTF-8
#     BOM on the manifest made `package_install_files` return FINISHED having
#     installed nothing, so every push silently reloaded the old version);
#   * the outcome is read from the add-on's own record on DISK, never inferred
#     from a version poll (the poll cannot tell "not yet" from "never will");
#   * "the bridge answered on the wrong version" and "Blender went away" are
#     DIFFERENT failures — an install can land in the other Blender, because
#     installing makes the one holding the bridge reload and free the port.
#
# Run with the app venv python (QT_QPA_PLATFORM=offscreen).
import base64
import json
import os
import shutil
import sys
import tempfile
import zipfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

sys.path.insert(0, os.path.join(_ROOT, "app"))

import addon_push  # noqa: E402
import bridge as bridgemod  # noqa: E402

PASS = []
FAIL = []


def ok(cond, label):
    (PASS if cond else FAIL).append(label)
    print(("ok   " if cond else "FAIL ") + label, flush=True)


_SANDBOX = tempfile.mkdtemp(prefix="madi_push_")


class _Task:
    """Stands in for the QThread the real push runs on."""

    def __init__(self):
        self.notes = []
        self.cancelled = False

        class _Sig:
            def __init__(self, sink):
                self._sink = sink

            def emit(self, text):
                self._sink.append(text)

        self.note = _Sig(self.notes)


# =========================================================== the bundle =====
# ⚠ Imported LAST and on purpose: `addon_bundle` is ~38,000 lines and +4 MB
# resident, and the app itself only imports it when a push actually happens.
import hashlib  # noqa: E402
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

# ⚠ NOTHING LICENCE-SHAPED IS LEFT IN THE CARRIED ADD-ON (1.19.0). The
# entitlement module and its three bridge commands were deleted; if a rebuild
# ever put them back, this catches it in the packed copy rather than in Blender.
ok("entitlement.py" not in _names and "ed25519.py" not in _names,
   "⚠ bundle: the packed add-on carries NO entitlement module")
ok(b"license_unlock" not in _zf.read("server.py"),
   "⚠ bundle: and its dispatcher routes no license_* command")


# ========================================================== the package =====

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

_found = addon_push.inspect_addon_package(_ok_zip)
ok(_found == {"id": "madi_anim_library", "version": "9.9.9"},
   "package: a good add-on zip reports its id and version")
ok(addon_push.inspect_addon_package(_ok_zip, expect_version="9.9.9"),
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
        addon_push.inspect_addon_package(_path)
    except ValueError as err:
        _detail = str(err)
    ok(_needle in _detail, "package: refuses %s" % _why)

_detail = ""
try:
    addon_push.inspect_addon_package(_ok_zip, expect_version="1.2.3")
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
ok(addon_push.inspect_addon_package(_bundle_zip,
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


_real_wait = addon_push.ADDON_WAIT
_real_poll = addon_push.ADDON_POLL_SECONDS
addon_push.ADDON_WAIT = 1
addon_push.ADDON_POLL_SECONDS = 0.02


def _push(bridge, zip_path, target="9.9.9"):
    bridgemod.clear_addon_update_result()
    pusher = addon_push.AddonPusher(bridge)
    return pusher, pusher._hand_to_blender(_Task(), zip_path, target)


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
# ⚠⚠ CHANGED 2026-08-17: this is a SUCCESS now, not a failure with a reason.
# It used to return `addon_restart` — an `ok: False` — for an install that had
# worked, which is the same class of lie as "Blender did not come back". The
# extension is on disk; only the live reload did not finish. The verdict is
# success and the MESSAGE carries the one thing left to do.
ok(_out.get("ok") is True and _out.get("reloaded") is False,
   "⚠ push: an install whose reload failed is a SUCCESS that still needs a "
   "restart — not a failed install (%r)" % (_out,))
_man._on_addon(_out)
ok("installed" in _man.message.lower() and "restart" in _man.message.lower(),
   "⚠ push: and it says installed AND names the restart, because Blender is "
   "still running the old code until then (%r)" % _man.message)

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
_seen_versions = addon_push.installed_addon_versions("madi_anim_library",
                                                          roots=_roots)
ok(_seen_versions == {"5.1": "9.9.9", "5.2": "0.44.0", "4.9": "9.9.9"},
   "roots: every Blender's installed add-on version is read off disk, BOM and "
   "all (got %r)" % (_seen_versions,))
ok(addon_push.installed_addon_versions("no_such_package",
                                            roots=_roots) == {},
   "roots: a package that is installed nowhere reads as nothing, not a crash")
ok(isinstance(addon_push.blender_config_roots(), list),
   "roots: probing the real machine never raises, whatever is installed")

_real_roots = addon_push.blender_config_roots
addon_push.blender_config_roots = lambda: _roots
try:
    _man, _out = _push(_PushBridge([{"version": "0.44.0"}] * 60), _ok_zip)
finally:
    addon_push.blender_config_roots = _real_roots
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


# ⚠⚠ THE ORDINARY SUCCESSFUL PUSH LEAVES THE BRIDGE DOWN, AND THAT USED TO BE
# REPORTED AS FAILURE. Installing reloads the extension, which drops the
# bridge — so every `ping` in the wait loop raises. The loop read the add-on's
# record ONLY in the branch where the bridge answered, so on the one path that
# matters it never looked, waited the full `ADDON_WAIT` (90 s in the shipping
# build) and then failed over an install that had completely succeeded.
#
# Marty hit it on 2026-08-17 — *"i can't install ... it doesn't work"* — while
# the extension was in fact installed and the record on disk said so. Measured
# after the fix against his live Blender: **1.1 s** instead of 90.
class _DownBridge(_PushBridge):
    """Takes the package, then never answers again — a real reload."""

    def __init__(self, record):
        _PushBridge.__init__(self, [], record=record)
        self.pings = 0

    def request(self, cmd, **kwargs):
        self.pings += 1
        raise RuntimeError("bridge is down (installing)")


_down = _DownBridge({"ok": True, "state": "installed", "version": "9.9.9",
                     "id": "madi_anim_library", "reloaded": True})
_man, _out = _push(_down, _ok_zip)
ok(_out.get("ok") is True,
   "⚠⚠ push: a bridge that never comes back is SUCCESS when the record says "
   "installed — the add-on's own account outranks a socket (%r)" % (_out,))
ok(_out.get("from_record") is True,
   "push: and it is flagged as coming from the record, so the message can say "
   "the connection returns in a moment rather than claiming it is already back")
# ⚠ The point is not only the verdict, it is the WAIT. One poll interval is
# enough to see the record; running to the deadline is the bug.
ok(_down.pings <= 2,
   "⚠ push: it stops as soon as the record appears rather than pinging until "
   "the deadline (pings=%d)" % _down.pings)
_man._on_addon(_out)
_msg = _man.message
ok("installed" in _msg.lower() and "reload" in _msg.lower(),
   "push: the message says installed AND names the reload (%r)" % _msg)

# ...and a record that says it FAILED still fails, with the add-on's reason.
_down_bad = _DownBridge({"ok": False, "state": "failed", "version": "9.9.9",
                         "id": "madi_anim_library", "error": "manifest bad"})
_man, _out = _push(_down_bad, _ok_zip)
ok(_out.get("ok") is False and "manifest bad" in (_out.get("detail") or ""),
   "push: a record that says FAILED is still a failure, with its own reason "
   "(%r)" % (_out,))

addon_push.ADDON_WAIT = _real_wait
addon_push.ADDON_POLL_SECONDS = _real_poll

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

shutil.rmtree(_SANDBOX, ignore_errors=True)

print("")
print("%d passed, %d failed" % (len(PASS), len(FAIL)))
for label in FAIL:
    print("  FAILED: " + label)
sys.exit(1 if FAIL else 0)
