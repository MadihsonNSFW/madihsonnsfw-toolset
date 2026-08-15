# Add-on self-update (add-on 0.7.0): verifying a package before installing it,
# and the real extension install.
#
#   blender.exe -b --factory-startup --python tests\addon_update_test.py
#
# ⚠ THE INSTALL HALF IS SKIPPED UNLESS IT IS ISOLATED. It needs
# MADI_ADDON_INSTALL_TEST=1 *and* BLENDER_USER_RESOURCES pointing somewhere
# temporary — `run_all.ps1` sets both. Without that guard, running this file
# directly would install a dummy extension into Marty's real Blender, which is
# not a thing a test suite gets to do.
#
# ⚠ bpy.app.timers NEVER FIRE in background Blender: there is no event loop, the
# script ends and Blender quits. So the timer path cannot be exercised here.
# `install_now()` is a plain function precisely so this suite can call it
# directly; `_tick` is only a thin scheduler around it.
import hashlib
import importlib
import os
import sys
import types
import zipfile

import bpy

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

ADDON = os.path.join(_ROOT, "blender_addon", "madi_anim_library")

# Loaded as a real PACKAGE, not a lone file: selfupdate.py does `from . import
# core`, which needs a parent package to resolve against.
pkg = types.ModuleType("madi_pkg")
pkg.__path__ = [ADDON]
sys.modules["madi_pkg"] = pkg
core = importlib.import_module("madi_pkg.core")
selfupdate = importlib.import_module("madi_pkg.selfupdate")

PASS = []
FAIL = []


def ok(cond, label):
    (PASS if cond else FAIL).append(label)
    print(("ok   " if cond else "FAIL ") + label, flush=True)


TMP = bpy.app.tempdir
DUMMY = os.path.join(TMP, "madi_probe_ext.zip")

MANIFEST = """schema_version = "1.0.0"
id = "madi_probe_ext"
version = "0.1.0"
name = "Madi Probe"
tagline = "Throwaway extension used by the add-on update suite"
maintainer = "MadihsonNSFW <martymason4704@gmail.com>"
type = "add-on"
tags = ["Animation"]
blender_version_min = "4.2.0"
license = ["SPDX:GPL-3.0-or-later"]
"""

with zipfile.ZipFile(DUMMY, "w") as z:
    z.writestr("blender_manifest.toml", MANIFEST)
    z.writestr("__init__.py", "def register():\n    pass\n\n\ndef unregister():\n    pass\n")

GOOD_HASH = hashlib.sha256(open(DUMMY, "rb").read()).hexdigest()

# ⚠ The outcome record goes somewhere THROWAWAY for this run. Without this the
# suite would write "madi_probe_ext 0.1.0 installed" into the folder the real
# app reads, and the app would report a dummy package as the last thing that
# was installed.
selfupdate._RESULT_DIR = os.path.join(TMP, "madi_probe_result")
os.makedirs(selfupdate._RESULT_DIR, exist_ok=True)

# ------------------------------------------------------------------- status

st = selfupdate.status()
ok(st["version"] == core.ADDON_VERSION, "status reports the installed add-on version")
ok(st["pending"] is None, "status: nothing is queued to begin with")
# A RANGE, not an exact match: self-update landed in 0.7.0 and the add-on keeps
# moving. Pinning the exact version made an unrelated version bump fail here.
ok(tuple(int(x) for x in core.ADDON_VERSION.split(".")) >= (0, 7, 0),
   "the add-on carrying self-update is 0.7.0 or newer (is %s)" % core.ADDON_VERSION)

# --------------------------------------------------------- what it refuses
# The hash is re-checked HERE even though the app already verified it against a
# signed manifest: this process is being asked to install code, and "something
# on localhost said so" is not a reason to.

for args, why in [
    ((os.path.join(TMP, "does-not-exist.zip"),), "a file that is not there"),
    ((__file__,), "something that is not a .zip"),
    ((DUMMY, "0.1.0", "f" * 64), "a package whose hash does not match"),
]:
    raised = False
    try:
        selfupdate.stage(*args)
    except ValueError:
        raised = True
    ok(raised, "stage refuses %s" % why)
    ok(selfupdate.status()["pending"] is None,
       "stage: and queues nothing after refusing %s" % why)

# ------------------------------------------- the manifest check (2026-08-14)
# ⚠⚠ THE FAILURE THIS WHOLE SECTION EXISTS FOR. Add-on 0.45.0 shipped with a
# UTF-8 BOM on `blender_manifest.toml`. Blender parses that file with tomllib,
# which refuses a BOM ("Invalid statement at line 1, column 1") - and
# `package_install_files` reported it through Blender's REPORT system while
# STILL RETURNING {'FINISHED'}. So every install silently did nothing, the
# add-on reloaded the old version over itself, and the app waited 90 s for a
# version that was never coming. Three bytes. No error anywhere.
#
# The old suite could not have caught it: it writes its own clean manifest.
# These checks are about packages that are WRONG.


def _repack(name, mutate):
    """A copy of the dummy package with one entry rewritten."""
    path = os.path.join(TMP, name)
    source = zipfile.ZipFile(DUMMY)
    with zipfile.ZipFile(path, "w") as out:
        for item in source.infolist():
            data = source.read(item.filename)
            data = mutate(item.filename, data)
            if data is not None:
                out.writestr(item.filename, data)
    source.close()
    return path


BOM_ZIP = _repack("madi_probe_bom.zip",
                  lambda n, d: (b"\xef\xbb\xbf" + d
                                if n == "blender_manifest.toml" else d))
NO_MANIFEST = _repack("madi_probe_bare.zip",
                      lambda n, d: None if n == "blender_manifest.toml" else d)
BROKEN_TOML = _repack("madi_probe_broken.zip",
                      lambda n, d: (b"id = \nversion =\n"
                                    if n == "blender_manifest.toml" else d))

good = selfupdate.inspect_package(DUMMY)
ok(good["id"] == "madi_probe_ext" and good["version"] == "0.1.0",
   "inspect_package: reads the id and version out of a good package")

for path, needle, why in [
    (BOM_ZIP, "BOM", "⚠ a UTF-8 BOM on the manifest - the 0.45.0 failure, and "
                     "the message NAMES it rather than saying 'could not parse'"),
    (NO_MANIFEST, "blender_manifest", "a package with no manifest at all"),
    (BROKEN_TOML, "TOML", "a manifest that is not valid TOML"),
]:
    detail = ""
    try:
        selfupdate.inspect_package(path)
    except ValueError as err:
        detail = str(err)
    ok(needle in detail, "inspect_package rejects %s" % why)

# And the refusal has to happen at STAGE time, on the socket the app is still
# holding - not at install time, when there is nobody left to tell.
for path, why in [(BOM_ZIP, "a BOM'd package"),
                  (NO_MANIFEST, "a package with no manifest")]:
    raised = False
    try:
        selfupdate.stage(path, version="0.1.0")
    except ValueError:
        raised = True
    ok(raised, "⚠ stage REFUSES %s while the app is still listening" % why)
    ok(selfupdate.status()["pending"] is None,
       "stage: and schedules nothing for %s" % why)

# A package that is fine but is not the one that was asked for.
mismatched = False
try:
    selfupdate.stage(DUMMY, version="9.9.9", sha256=GOOD_HASH)
except ValueError as err:
    mismatched = "9.9.9" in str(err)
ok(mismatched, "stage refuses a package whose version is not the one requested")
ok(selfupdate.status()["pending"] is None,
   "stage: and queues nothing after a version mismatch")

# ⚠ THE REAL SHIPPING MANIFEST, not a dummy. This is the check that would have
# stopped 0.45.0 leaving the building.
_real_manifest = os.path.join(ADDON, "blender_manifest.toml")
with open(_real_manifest, "rb") as _fh:
    _real_raw = _fh.read()
ok(_real_raw[:3] != b"\xef\xbb\xbf",
   "⚠ THE SHIPPING blender_manifest.toml HAS NO BOM - a BOM makes every "
   "install a silent no-op")
_parsed = None
try:
    _parsed = selfupdate.parse_manifest(_real_raw)
except ValueError as err:
    print("   manifest error: %s" % err)
ok(_parsed is not None and _parsed.get("id") == "madi_anim_library",
   "the shipping manifest parses the way Blender parses it")
ok(_parsed is not None and _parsed.get("version") == core.ADDON_VERSION,
   "the shipping manifest's version matches core.ADDON_VERSION (%s)"
   % core.ADDON_VERSION)

# ------------------------------------------------------------- what it does

res = selfupdate.stage(DUMMY, version="0.1.0", sha256=GOOD_HASH)
ok(res["scheduled"] is True, "stage: a package with the right hash is scheduled")
ok(res["sha256"] == GOOD_HASH, "stage: and it reports the hash it actually read")
ok(res["from"] == core.ADDON_VERSION, "stage: it says which version it is replacing")
ok(res["delay"] >= 1.0,
   "stage: the install is delayed - the reply has to get out before the reload cuts the socket")
ok(selfupdate.status()["pending"] == "0.1.0", "stage: status now shows it queued")

staged_path = selfupdate._PENDING["path"]
ok(os.path.isfile(staged_path), "stage: the package is copied somewhere of our own")
ok(os.path.abspath(staged_path) != os.path.abspath(DUMMY),
   "stage: to OUR OWN copy - the app must be free to clean up the moment it has the reply")

# Staging again replaces the queued package rather than stacking timers.
res2 = selfupdate.stage(DUMMY, version="0.1.0", sha256=GOOD_HASH)
ok(res2["scheduled"] is True and selfupdate.status()["pending"] == "0.1.0",
   "stage: asking twice is harmless")
ok(not os.path.exists(staged_path),
   "stage: and the SUPERSEDED copy is deleted - otherwise asking twice leaks a zip nobody sees")
staged_path = selfupdate._PENDING["path"]

# --------------------------------------------------------------- installing

if not (os.environ.get("MADI_ADDON_INSTALL_TEST") == "1"
        and os.environ.get("BLENDER_USER_RESOURCES")):
    print("SKIP install: not isolated (needs MADI_ADDON_INSTALL_TEST=1 + "
          "BLENDER_USER_RESOURCES)", flush=True)
else:
    resources = os.environ["BLENDER_USER_RESOURCES"]
    # reload=False: reloading madi_pkg is meaningless here (it is not a real
    # installed extension in this process) and reload_addon() is the one part
    # that can only be exercised in a GUI session.
    out = selfupdate.install_now(reload=False)
    ok(out["installed"] is True, "install: the extension really installs")
    ok(out.get("error") is None, "install: with no error")
    ok(out["reloaded"] is False, "install: and the reload was not attempted")
    landed = os.path.join(resources, "extensions", "user_default", "madi_probe_ext")
    ok(os.path.isdir(landed), "install: it lands in the ISOLATED extensions folder")
    ok(not os.path.exists(staged_path),
       "install: the temporary package is cleaned up afterwards")
    ok(selfupdate.status()["pending"] is None, "install: and nothing is left queued")
    # This is the finding that shaped the whole design: the operator works from
    # a context with no window at all, which a timer callback also has.
    ok(True, "install: package_install_files works from a restricted, windowless context")

    # --- rule 2: the outcome is READ BACK OFF DISK, never inferred ---------
    ok(out.get("on_disk") == "0.1.0",
       "⚠ install: the version is read back from Blender's OWN files - the "
       "operator's {'FINISHED'} is not a verdict and never was")
    ok(selfupdate.installed_version("madi_probe_ext") == "0.1.0",
       "installed_version: reads the manifest Blender actually wrote")
    ok(selfupdate.installed_version("madi_nothing_here") is None,
       "installed_version: None for a package that is not installed")

    # --- rule 3: and WRITTEN DOWN, where it survives the reload -----------
    record = selfupdate.last_result()
    ok(isinstance(record, dict) and record.get("ok") is True
       and record.get("state") == "installed" and record.get("version") == "0.1.0",
       "⚠ install: the outcome is recorded in a FILE - the reload purges every "
       "module and the socket is already closed, so nothing else survives")
    ok(selfupdate.status()["last"] == record,
       "addon_status carries that record, so the app can ask how it went")

    # --- a package BLENDER refuses: {'FINISHED'}, and nothing installed ----
    # ⚠ THE 0.45.0 FAILURE, REPRODUCED. stage() would refuse this now, so it is
    # queued by hand - the point is that install_now CANNOT be fooled either,
    # which is what keeps a bad package from reading as a silent success.
    refused_zip = os.path.join(TMP, "madi_probe_refused.zip")
    with zipfile.ZipFile(refused_zip, "w") as z:
        z.writestr("blender_manifest.toml",
                   b"\xef\xbb\xbf" + MANIFEST.replace(
                       "madi_probe_ext", "madi_probe_refused").replace(
                       '"0.1.0"', '"0.2.0"').encode("utf-8"))
        z.writestr("__init__.py", "def register():\n    pass\n\n\n"
                                  "def unregister():\n    pass\n")
    selfupdate._PENDING = {"path": refused_zip, "version": "0.2.0",
                           "id": "madi_probe_refused", "sha256": ""}
    refused = selfupdate.install_now(reload=False)
    ok(refused["installed"] is False,
       "⚠ install: a package Blender REFUSES is reported as not installed - "
       "package_install_files returns {'FINISHED'} either way")
    ok("refused" in (refused.get("error") or "").lower(),
       "install: and the error says Blender refused it")
    ok(refused.get("on_disk") is None,
       "install: because nothing was written to the extensions folder")
    ok(not os.path.isdir(os.path.join(resources, "extensions", "user_default",
                                      "madi_probe_refused")),
       "install: confirmed - no folder for the refused package")
    bad_record = selfupdate.last_result()
    ok(bad_record.get("ok") is False and bad_record.get("state") == "refused",
       "⚠ install: the REFUSAL is recorded too - that record is the only way "
       "the app can ever learn about it")

nothing = selfupdate.install_now(reload=False)
ok(nothing["installed"] is False and "nothing" in nothing["reason"],
   "install: with nothing staged it does nothing, rather than failing")

ok(selfupdate._tick() is None,
   "tick: the timer callback returns None so it runs exactly once")

# ---------------------------------------------- resuming the bridge (0.39.0)
# ⚠ THE ADD-ON NO LONGER STARTS THE BRIDGE ON ITS OWN (Marty, 2026-08-12), and
# a reload goes through `register()` — which is where the autostart used to be.
# So an update would install perfectly and leave the bridge down, and the app,
# which finds out by re-polling `ping`, would sit there looking hung. This is
# the one path that puts it back, and it only fires for a bridge that WAS
# serving: restoring what the user started, not starting it for them.
#
# ⚠ AGAINST A STUB, NEVER THE REAL SERVER. `_resume_bridge` binds a real socket
# on the real port, and the suite runs while Marty's own Blender may be holding
# 9877 — a test that either steals that port or fails because it cannot is a
# test about his desktop, not about this code. Only the WIRING can be wrong
# here (the sys.modules key the reload has to look the new module up by), and a
# stub proves that exactly.
_real_server_mod = sys.modules.get("madi_pkg.server")


class _StubServer:
    def __init__(self):
        self.port = 0
        self.starts = 0

    def start(self):
        self.starts += 1
        return True


_stub = types.ModuleType("madi_pkg.server")
_stub.server = _StubServer()
sys.modules["madi_pkg.server"] = _stub
try:
    ok(selfupdate._resume_bridge() is True and _stub.server.starts == 1,
       "⚠ resume: finds the RELOADED server module by its sys.modules key and "
       "starts it — the key is the whole thing that can be wrong here")
    del sys.modules["madi_pkg.server"]
    ok(selfupdate._resume_bridge() is False,
       "resume: with no server module it answers False instead of raising — a "
       "failed resume must not take the finished update down with it")
finally:
    if _real_server_mod is not None:
        sys.modules["madi_pkg.server"] = _real_server_mod
    else:
        sys.modules.pop("madi_pkg.server", None)

_reload_src = open(os.path.join(os.path.dirname(selfupdate.__file__),
                                "selfupdate.py"), encoding="utf-8").read()
ok(_reload_src.index("was_serving = was_serving or")
   < _reload_src.index("obj.stop()"),
   "⚠ resume: `running` is SAMPLED BEFORE the stop — read afterwards it could "
   "only ever answer 'nothing was running', and the bridge would never return")

# ⚠⚠ AND BEFORE THE INSTALL, WHICH IS THE ONE THAT ACTUALLY BIT (2026-08-14).
# `package_install_files` re-enables the extension, `unregister()` stops the
# bridge, and every BridgeServer reads running=False from then on. MEASURED in
# Marty's live Blender: True before the operator, False after, with the sweep
# then answering `was_serving = False`. So `_resume_bridge` was never called on
# ANY push - the update installed perfectly every time and left the bridge
# down, which is the whole of "a failed add-on push may be a lie". Three
# sessions wrote that up as an oddity of the reload rather than a bug.
# ⚠ ON THE AST, NOT ON THE TEXT. The comment above the sample explains itself by
# NAMING `package_install_files`, so a `.index()` comparison finds the comment
# first and fails on correct code - the fourth time this project has been bitten
# by an absence/order check that greps source.
import ast  # noqa: E402

_install_fn = next(n for n in ast.walk(ast.parse(_reload_src))
                   if isinstance(n, ast.FunctionDef) and n.name == "install_now")


def _line_of(fn, needle):
    """The line a call to `needle` appears on inside `fn`, or None."""
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            func = node.func
            name = getattr(func, "id", None) or getattr(func, "attr", None)
            if name == needle:
                return node.lineno
    return None


_sample_at = _line_of(_install_fn, "_bridge_is_serving")
_install_at = _line_of(_install_fn, "package_install_files")
_reload_at = _line_of(_install_fn, "reload_addon")
ok(_sample_at is not None, "resume: install_now samples whether a bridge was serving")
ok(_sample_at is not None and _install_at is not None
   and _sample_at < _install_at,
   "⚠⚠ resume: and it samples it BEFORE package_install_files — the operator "
   "re-enables the extension, unregister() stops the bridge, and every reading "
   "taken after that says 'nothing was running' (sample line %s, install line "
   "%s)" % (_sample_at, _install_at))
ok(_reload_at is not None and any(
    kw.arg == "was_serving"
    for node in ast.walk(_install_fn) if isinstance(node, ast.Call)
    and getattr(node.func, "id", None) == "reload_addon"
    for kw in node.keywords),
   "resume: and hands that answer to the reload, which cannot work it out itself")
ok(selfupdate.reload_addon.__defaults__ == (None,),
   "resume: reload_addon still answers for itself when nobody tells it (the "
   "dev reload path, where nothing has stopped anything yet)")


class _Serving:
    """Stands in for a BridgeServer the way the gc sweep finds them: BY TYPE
    NAME, because there is no tracked handle to ask."""

    def __init__(self, running):
        self.running = running


_Serving.__name__ = "BridgeServer"
_live = _Serving(True)
ok(selfupdate._bridge_is_serving() is True,
   "resume: a listening bridge is found through the gc sweep")
_live.running = False
ok(selfupdate._bridge_is_serving() is False,
   "resume: and a stopped one is not - which is exactly what the install "
   "leaves behind, and why the sample has to come first")
del _live

print("")
print("%d passed, %d failed" % (len(PASS), len(FAIL)))
for f in FAIL:
    print("FAIL " + f)
sys.exit(1 if FAIL else 0)
