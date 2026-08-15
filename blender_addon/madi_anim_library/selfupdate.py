"""Updating this add-on, in place, on a request from the app.

THE HAZARD THIS FILE IS BUILT AROUND: the bridge server lives INSIDE this
add-on. The reload procedure stops every BridgeServer, purges `sys.modules` and
re-enables the extension - so an add-on that reinstalls itself while handling a
command destroys the socket that is still holding that command's reply. The
caller sees a dropped connection and cannot tell success from a crash.

So the work is split in two:

    stage()       verify the zip, remember it, ASK for the install, RETURN.
                  The reply travels back to the app on the client thread while
                  the main thread is idle.
    _tick()       a bpy.app.timers callback, a second and a half later, once
                  that reply is long gone. It installs, then reloads.

After that the app cannot be told anything - there is no connection any more.
It finds out the way it would after any reinstall: it re-polls `ping` until the
bridge answers again and compares the version. If the bridge never comes back,
the files on disk are still correctly installed, so restarting Blender finishes
the job. That is the fallback, and it is why the install happens BEFORE the
reload rather than as part of it.

⚠ `bpy.app.timers` DO NOT FIRE in background (-b) Blender - there is no event
loop, the script ends and Blender quits. That is why `install_now()` is a plain
function the tests call directly, and `_tick` is only a thin scheduler around
it. Do not fold them back together.

⚠⚠ THE SECOND HAZARD, AND IT COST A WHOLE EVENING (2026-08-14):
`bpy.ops.extensions.package_install_files()` RETURNS {'FINISHED'} WHEN IT HAS
INSTALLED NOTHING. A package Blender refuses - a manifest it cannot parse, most
of all - is reported through Blender's REPORT system, which a caller in a timer
has no access to, and the operator raises nothing. So "it returned FINISHED" and
"it installed" are different facts, and this file must never confuse them again.
0.45.0 shipped with a UTF-8 BOM on its manifest, every install silently did
nothing, the add-on reloaded the OLD version over itself, and the app sat
waiting 90 s for a version that was never coming. Three bytes, no error message
anywhere. Hence the three rules this file now follows:

    1. CHECK THE PACKAGE BEFORE SCHEDULING (`inspect_package`). The socket the
       app is holding is still open at `stage()` time, so a bad package is a
       one-second error message instead of a 90-second silence.
    2. VERIFY ON DISK AFTER INSTALLING (`installed_version`). Blender's own
       files are the only honest witness to whether an install happened.
    3. WRITE THE OUTCOME DOWN WHERE IT SURVIVES THE RELOAD (`_record`). The
       reload purges every module here and the socket is long closed, so a file
       is the only channel left that can reach the person who pressed Update.
"""

import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import time
import traceback
import zipfile

import bpy

from . import bridgeauth
from . import core

# The module name addon_utils knows this extension by, e.g.
# "bl_ext.user_default.madi_anim_library".
MODULE = __package__ or "madi_anim_library"

# Long enough for the client thread to have written the reply to a localhost
# socket and closed it (the reply is a few hundred bytes; the write happens as
# soon as _process_queue sets the done event). Deliberately not shaved down -
# there is no prize for reloading a second earlier, and the cost of being wrong
# is a customer who cannot tell whether the update worked.
INSTALL_DELAY = 1.5

READ_CHUNK = 1024 * 1024

# The extension repository we install into, and the two file names that decide
# everything below.
REPO = "user_default"
MANIFEST_NAME = "blender_manifest.toml"
RESULT_NAME = "addon_update_result.json"

_PENDING = None


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(READ_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# Rule 1: check the package before scheduling anything
# ---------------------------------------------------------------------------

def parse_manifest(raw):
    """Read an extension manifest the way BLENDER reads it, and say plainly
    what is wrong when it will not.

    ⚠ **A UTF-8 BOM IS FATAL, AND IT IS NAMED HERE ON PURPOSE.** Blender parses
    this file with `tomllib`, which does NOT skip one: it fails on *"Invalid
    statement (at line 1, column 1)"*. That is what happened to 0.45.0 - an
    editor re-saved the manifest with a BOM and every install became a silent
    no-op. Three bytes, no exception, no error the app could see. A generic
    "could not parse" would have sent the next person looking at the TOML
    itself, which is perfectly valid TOML.
    """
    if raw[:3] == b"\xef\xbb\xbf":
        raise ValueError(
            "%s starts with a UTF-8 BOM. Blender's TOML reader refuses it "
            "(\"Invalid statement (at line 1, column 1)\") and installs "
            "nothing. Save the file as UTF-8 WITHOUT a byte-order mark."
            % MANIFEST_NAME)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("%s is not valid UTF-8 (%s)" % (MANIFEST_NAME, exc))

    data = None
    try:
        import tomllib
    except ImportError:
        tomllib = None
    if tomllib is not None:
        try:
            data = tomllib.loads(text)
        except Exception as exc:                            # noqa: BLE001
            raise ValueError("%s is not valid TOML: %s" % (MANIFEST_NAME, exc))
    if data is None:
        # No tomllib (Python below 3.11). Blender itself always has it; this
        # path exists so the same function can be reused off a Blender build.
        data = {}
        for key in ("id", "version", "schema_version"):
            found = re.search(r'^\s*%s\s*=\s*"([^"]+)"' % key, text, re.M)
            if found:
                data[key] = found.group(1)
    missing = [key for key in ("id", "version") if not data.get(key)]
    if missing:
        raise ValueError("%s declares no %s"
                         % (MANIFEST_NAME, " and no ".join(missing)))
    return {"id": data["id"], "version": data["version"]}


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


def inspect_package(path):
    """What is in an extension zip, and will Blender accept it.

    ⚠ **THIS IS THE CHECK THAT WAS MISSING.** It runs while the app is still
    holding the socket, so a package Blender would refuse is refused HERE, with
    a reason, in a second - instead of being scheduled, installed into nothing,
    and inferred from a version poll that times out 90 s later blaming Blender
    for "not coming back".
    """
    try:
        with zipfile.ZipFile(path) as bundle:
            entry = _manifest_entry(bundle.namelist())
            if entry is None:
                raise ValueError(
                    "there is no %s in this package, so it is not a Blender "
                    "extension" % MANIFEST_NAME)
            raw = bundle.read(entry)
    except ValueError:
        raise
    except zipfile.BadZipFile as exc:
        raise ValueError("this package is not a readable zip (%s)" % exc)
    except OSError as exc:
        raise ValueError("this package could not be read (%s)" % exc)
    found = parse_manifest(raw)
    found["entry"] = entry
    return found


# ---------------------------------------------------------------------------
# Rule 2: verify on disk. Rule 3: write the outcome down.
# ---------------------------------------------------------------------------

def extensions_root():
    """Blender's user extensions folder, or "" if it cannot be asked."""
    try:
        return bpy.utils.user_resource("EXTENSIONS")
    except Exception:                                       # noqa: BLE001
        return ""


def installed_version(package_id):
    """The version Blender REALLY has on disk for `package_id`, or None.

    None means the extension folder is not there at all, which is exactly what
    a refused package looks like - the operator returns {'FINISHED'} and writes
    nothing.
    """
    root = extensions_root()
    if not root or not package_id:
        return None
    path = os.path.join(root, REPO, package_id, MANIFEST_NAME)
    try:
        with open(path, "rb") as handle:
            return parse_manifest(handle.read())["version"]
    except (OSError, ValueError):
        return None


# ⚠ TESTS ONLY. `addon_update_test.py` runs the real install against a throwaway
# extensions folder, and without this it would write its dummy package's outcome
# into the folder the REAL app reads - so a test run would leave the app
# reporting that "madi_probe_ext 0.1.0" was the last thing installed. Never set
# from shipped code.
_RESULT_DIR = None


def result_path():
    """Where an update's outcome is written: the folder the app already reads
    the bridge token from, so it needs no new agreement between the two."""
    return os.path.join(_RESULT_DIR or bridgeauth.token_dir(), RESULT_NAME)


def _record(**fields):
    """Write down how an update went, WHERE IT SURVIVES THE RELOAD.

    ⚠ It has to be a FILE. Installing reloads the extension, which purges every
    module in this package, and the socket the request arrived on closed a
    second before that - so neither a global nor the reply can carry the
    answer. The app reads this back through `addon_status`, or straight off
    disk when the bridge never returns, and that is the only route by which a
    failed install can reach the person who pressed the button.
    """
    payload = dict(fields)
    payload["when"] = time.time()
    try:
        os.makedirs(bridgeauth.token_dir(), exist_ok=True)
        with open(result_path(), "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
    except OSError:
        traceback.print_exc()                # never take the update down with it
    return payload


def last_result():
    """The outcome of the last update, or None if there has never been one."""
    try:
        with open(result_path(), "r", encoding="utf-8") as handle:
            found = json.load(handle)
    except (OSError, ValueError):
        return None
    return found if isinstance(found, dict) else None


def status():
    """What is installed, whether an update is queued, and how the last one
    went. `last` is the part the app cannot work out for itself."""
    return {
        "version": core.ADDON_VERSION,
        "module": MODULE,
        "pending": None if _PENDING is None else _PENDING["version"],
        "last": last_result(),
    }


def stage(path, version=None, sha256=None):
    """Check a downloaded add-on zip and schedule its install.

    Returns immediately, ON PURPOSE - see the module docstring. The hash is
    re-checked HERE even though the app already verified it against a signed
    manifest: this process is being asked to install code, and "something on
    localhost said it was fine" is not a reason to. If it does not match, the
    file is not installed and nothing is scheduled.
    """
    global _PENDING
    if not isinstance(path, str) or not os.path.isfile(path):
        raise ValueError("no such file: %r" % path)
    if not path.lower().endswith(".zip"):
        raise ValueError("an extension is installed from a .zip")
    actual = _sha256(path)
    if sha256 and actual.lower() != str(sha256).lower():
        raise ValueError("the add-on package does not match its expected hash - "
                         "it was not installed")

    # ⚠ AND IT HAS TO BE SOMETHING BLENDER WILL ACTUALLY TAKE. A correct hash
    # only proves the bytes arrived intact; it says nothing about whether the
    # package can be installed. This is the one moment there is still a socket
    # open to answer on - see the module docstring.
    found = inspect_package(path)
    if version and found["version"] != version:
        raise ValueError("this package is %s, not the %s that was asked for - "
                         "it was not installed" % (found["version"], version))
    target = version or found["version"]

    # Copied out of the app's staging area into our own temp file, so the app is
    # free to clean up (or fail, or be closed) the moment it has our reply.
    handle, dest = tempfile.mkstemp(prefix="madi_addon_update_", suffix=".zip")
    os.close(handle)
    shutil.copyfile(path, dest)

    # Replacing an earlier request takes its temp file with it. Without this,
    # asking twice leaves the first copy on disk forever - nobody would ever
    # notice, which is exactly why it would keep happening.
    if _PENDING is not None:
        _cleanup(_PENDING["path"])
    _PENDING = {"path": dest, "version": target, "id": found["id"],
                "sha256": actual}
    _record(ok=None, state="staged", version=target, id=found["id"],
            was=core.ADDON_VERSION, error=None)
    if not bpy.app.timers.is_registered(_tick):
        bpy.app.timers.register(_tick, first_interval=INSTALL_DELAY)
    return {"scheduled": True, "version": target, "id": found["id"],
            "sha256": actual, "delay": INSTALL_DELAY,
            "from": core.ADDON_VERSION}


def _tick():
    """Timer callback. Returns None so it runs exactly once.

    ⚠ Its failure has to be WRITTEN DOWN, not just printed. Nothing is
    listening at this point - the reply went out a second and a half ago - so a
    traceback in Blender's console is a message to nobody.
    """
    try:
        install_now()
    except Exception as err:                                # noqa: BLE001
        traceback.print_exc()
        _record(ok=False, state="failed",
                error="the install did not run: %s: %s"
                      % (type(err).__name__, err))
    return None


def install_now(reload=True):
    """Install the staged package and reload the add-on.

    Split out of the timer so it is directly testable: background Blender never
    runs timers (see the module docstring).
    """
    global _PENDING
    job = _PENDING
    _PENDING = None
    if not job:
        return {"installed": False, "reason": "nothing staged"}

    package_id = job.get("id") or ""
    result = {"installed": False, "reloaded": False, "version": job["version"],
              "id": package_id}

    # ⚠⚠ SAMPLED BEFORE THE INSTALL, AND THAT IS THE WHOLE POINT.
    # `package_install_files` re-enables the extension, which runs `unregister()`,
    # which STOPS THE BRIDGE. Measured 2026-08-14: `running` goes True -> False
    # during the operator call. `reload_addon`'s own sweep runs after that, so it
    # could only ever answer "nothing was running" - and so `_resume_bridge` was
    # never called, on any push, ever. That is the whole of "A FAILED ADD-ON PUSH
    # MAY BE A LIE": the update installed perfectly every time and left the
    # bridge down, and the app, which finds out by re-polling `ping`, sat there
    # looking hung. Three sessions wrote that up as an oddity of the reload.
    was_serving = _bridge_is_serving()
    result["was_serving"] = was_serving

    try:
        bpy.ops.extensions.package_install_files(
            filepath=job["path"], repo=REPO, enable_on_install=True)
    except Exception as err:
        result["error"] = "%s: %s" % (type(err).__name__, err)
        traceback.print_exc()
        _cleanup(job["path"])
        _record(ok=False, state="failed", version=job["version"],
                id=package_id, error=result["error"])
        return result

    # ⚠⚠ THE OPERATOR'S RETURN VALUE IS NOT A VERDICT AND NEVER WAS. It answers
    # {'FINISHED'} whether it installed the package or refused it - the refusal
    # goes to Blender's report system, which nothing here can read. Blender's
    # own files are the only honest witness, so ask them. This single check is
    # what turns "silently reloads the old version for ever" into an error
    # message. See the module docstring for the evening it cost.
    on_disk = installed_version(package_id)
    result["on_disk"] = on_disk
    if on_disk != job["version"]:
        result["error"] = (
            "Blender refused the package: %s. Its manifest is the usual "
            "cause - Blender's console will carry a \"Failed to load "
            "manifest\" line."
            % ("the extension folder still reports %s" % on_disk if on_disk
               else "it wrote no extension folder at all"))
        print("[MadihsonNSFW] add-on update FAILED: %s" % result["error"])
        _cleanup(job["path"])
        _record(ok=False, state="refused", version=job["version"],
                id=package_id, on_disk=on_disk, error=result["error"])
        # ⚠ NO RELOAD. Nothing changed on disk, so reloading would only bounce
        # the bridge and hand the app a version poll that can never succeed -
        # which is exactly what it looked like from outside for a whole
        # evening: a healthy port, the old version, and no error anywhere.
        return result

    result["installed"] = True
    # Recorded BEFORE the reload, deliberately: the install has already
    # succeeded, and the reload is the one step that could take this process
    # down with it. Written now, "installed" survives even a crash.
    _record(ok=True, state="installed", version=job["version"], id=package_id,
            reloaded=False, error=None)

    if reload:
        try:
            reload_addon(was_serving=was_serving)
            result["reloaded"] = True
        except Exception as err:
            # The files ARE installed correctly at this point, so this is
            # recoverable by restarting Blender - say so rather than implying
            # the update failed.
            result["error"] = "installed, but the live reload failed (%s). " \
                              "Restart Blender to finish." % err
            traceback.print_exc()
    _cleanup(job["path"])
    _record(ok=True, state="installed", version=job["version"], id=package_id,
            reloaded=result["reloaded"], error=result.get("error"))
    print("[MadihsonNSFW] add-on update: %s" % result)
    return result


def _cleanup(path):
    try:
        os.remove(path)
    except OSError:
        pass


def _resume_bridge():
    """Put a bridge that WAS running back on its port after a reload.

    ⚠ **THIS IS NOT AN AUTOSTART AND THE DISTINCTION IS THE WHOLE POINT.** The
    add-on never starts the bridge on its own any more (0.39.0, Marty: *"Don't
    automatically Start blender bridge no matter what"*) — `register()` used to,
    and a reload goes through `register()`. So without this, updating the
    extension would install perfectly and leave the bridge down: the app re-polls
    `ping` for a socket nobody is ever going to open, which reads as a hung
    update rather than a finished one.

    What it restores is a bridge the user had already started, on the port they
    had it on, in the instance that was serving. It runs ONLY when the reload
    found a running server to stop.

    ⚠ **One attempt, no retry.** If the rebind is refused the user presses
    Start, exactly as they would in any other session — a retry loop here is
    the same silent port-stealing that was just deleted from `server.py`.
    """
    mod = sys.modules.get(MODULE + ".server")
    if mod is None:
        return False
    srv = mod.server
    try:
        prefs = bpy.context.preferences.addons[MODULE].preferences
        srv.port = prefs.port
    except (KeyError, AttributeError):
        pass                                  # the default port is fine
    try:
        return bool(srv.start())
    except Exception:                         # noqa: BLE001
        traceback.print_exc()
        return False


def _bridge_is_serving():
    """Is a bridge listening right now?

    Found by TYPE NAME through the garbage collector, for the same reason
    `reload_addon` does it: earlier reloads leave stranded instances behind and
    there is no single tracked handle to ask.
    """
    import gc

    return any(bool(getattr(obj, "running", False)) for obj in gc.get_objects()
               if type(obj).__name__ == "BridgeServer")


def reload_addon(was_serving=None):
    """The full reload: stop the servers, purge the modules, re-enable.

    A plain reinstall does NOT refresh already-imported submodules, which is why
    sys.modules is purged by hand. Servers are found by TYPE NAME through the
    garbage collector rather than through a tracked handle, because earlier
    reloads leave old instances behind and any one of them still holding the
    port would stop the new bridge binding (six were found stranded once).

    ⚠⚠ `was_serving` IS AN ARGUMENT BECAUSE THE ANSWER CANNOT BE FOUND HERE
    AFTER AN INSTALL. `package_install_files` re-enables the extension, and
    `unregister()` stops the bridge - so by the time this function runs, every
    BridgeServer reads `running == False` and the sweep below concludes
    "nothing was running" no matter what was true a second earlier. `install_now`
    samples it BEFORE the install and passes it in. Left as None (the dev reload
    path, where nothing has stopped anything yet) the sweep still answers.

    This function survives its own module being purged: the timer holds a
    reference to it, and a function keeps its __globals__ alive regardless of
    what sys.modules contains.
    """
    import gc

    import addon_utils

    stopped = 0
    for obj in gc.get_objects():
        if type(obj).__name__ == "BridgeServer":
            try:
                # ⚠ Sampled BEFORE the stop — `running` is False afterwards, so
                # asking later could only ever answer "nothing was running" and
                # the bridge would never come back. Only the instance that
                # actually held the port counts; the stranded ones this sweep
                # exists to collect were never listening.
                was_serving = was_serving or bool(getattr(obj, "running", False))
                obj.stop()
                stopped += 1
            except Exception:
                pass

    try:
        addon_utils.disable(MODULE, default_set=False)
    except Exception:
        traceback.print_exc()

    for name in [n for n in list(sys.modules)
                 if n == MODULE or n.startswith(MODULE + ".")]:
        sys.modules.pop(name, None)

    addon_utils.enable(MODULE, default_set=False, persistent=True)
    resumed = _resume_bridge() if was_serving else False
    print("[MadihsonNSFW] reloaded %s (stopped %d server(s), bridge %s)"
          % (MODULE, stopped,
             "resumed" if resumed else
             "left down - press Start in the sidebar" if was_serving else
             "was not running"))
    return stopped
