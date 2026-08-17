"""Install the Blender add-on the app carries inside itself.

⚠ **THIS IS A LOCAL OPERATION AND ALWAYS HAS BEEN.** App → Blender, over the
bridge, using the copy packed into this build (`addon_bundle.py`). No server, no
download, no licence. It was carved out of the old self-updater on 2026-08-15
when that whole subsystem was removed — the two had been living in one file only
because a release could also carry an add-on, and that route is gone.

Everything hard about this module is in `_hand_to_blender`, and all of it was
learned the expensive way. Read that docstring before changing anything here.

Runs off the GUI thread. Failure is always a sentence a user can act on.
"""

import os
import re
import sys
import tempfile
import time
import zipfile

from PySide6.QtCore import QObject, QThread, Signal

# Not for talking to Blender — the Bridge instance is injected. This is for the
# two file helpers that read the add-on's own record of an install, which is the
# only channel that survives the reload.
import bridge as bridgemod

# States. Three, where the updater had eight: this either is or is not
# installing, and the third is why it stopped.
IDLE = "idle"
INSTALLING = "installing"
FAILED = "failed"

# After handing Blender the add-on: it installs ~1.5 s later and then reloads,
# which drops the bridge for a few seconds. A big scene makes re-registering
# slower, so the wait is generous — the alternative is telling someone their
# update failed while it is still working.
ADDON_POLL_SECONDS = 1.0
ADDON_WAIT = 90

# Where Blender keeps user-installed extensions, per Blender version. Used ONLY
# to answer "did the add-on actually land somewhere?" when the bridge poll
# could not — see `installed_addon_versions`.
_EXT_TAIL = ("extensions", "user_default")
_VERSION_LINE = re.compile(r'^\s*version\s*=\s*["\']([^"\']+)["\']', re.M)

MANIFEST_NAME = "blender_manifest.toml"


def blender_config_roots():
    """Every `…/Blender/<x.y>/` folder on this machine, or [] if there are none.

    Windows is the only one that matters in practice, but the other two are two
    lines and stop this from being silently useless off it.
    """
    home = os.path.expanduser("~")
    if sys.platform.startswith("win"):
        base = os.path.join(os.environ.get("APPDATA")
                            or os.path.join(home, "AppData", "Roaming"),
                            "Blender Foundation", "Blender")
    elif sys.platform == "darwin":
        base = os.path.join(home, "Library", "Application Support", "Blender")
    else:
        base = os.path.join(home, ".config", "blender")
    try:
        names = sorted(os.listdir(base), reverse=True)
    except OSError:
        return []
    return [os.path.join(base, n) for n in names
            if os.path.isdir(os.path.join(base, n))]


def installed_addon_versions(package_id, roots=None):
    """{Blender version -> installed add-on version}, read straight off disk.

    ⚠⚠ **THE ONLY HONEST ANSWER WHEN THE BRIDGE POLL CANNOT GIVE ONE.** An
    install is per Blender VERSION — separate extension folders — while the
    bridge is a single port that exactly one Blender holds at a time. Those two
    facts can disagree, and on 2026-08-15 they did: the add-on was pushed to
    the Blender holding the bridge (5.1), that Blender installed it and
    reloaded — which FREES THE PORT — and a second Blender (5.2, still on the
    old add-on) took it. The app went on polling for its target version on what
    had silently become a different instance, timed out, and reported
    *"Blender stopped answering"* about a Blender that had answered every
    single poll. The install had succeeded the whole time.

    ⚠ Parsed with a regex, not `tomllib` — this app runs on Python 3.10, where
    there is no `tomllib`. `utf-8-sig` because a BOM on `blender_manifest.toml`
    is a real thing that has bitten this project before (2026-08-14); here it
    would merely hide a version, but reading it wrong is how that started.
    """
    found = {}
    for root in (roots if roots is not None else blender_config_roots()):
        manifest = os.path.join(root, *(_EXT_TAIL + (package_id,
                                                     "blender_manifest.toml")))
        try:
            with open(manifest, "r", encoding="utf-8-sig") as handle:
                match = _VERSION_LINE.search(handle.read())
        except OSError:
            continue
        if match:
            found[os.path.basename(root.rstrip("\\/"))] = match.group(1)
    return found


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


class _Task(QThread):
    """One background job. The pusher owns it; results come back as a signal on
    the GUI thread."""

    done = Signal(object)
    note = Signal(str)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self._fn = fn
        self.cancelled = False

    def run(self):
        try:
            result = self._fn(self)
        except Exception as err:                            # noqa: BLE001
            result = {"ok": False, "reason": "client_error", "detail": str(err)}
        if not self.cancelled:
            self.done.emit(result)


class AddonPusher(QObject):
    """Installs the carried add-on into the connected Blender."""

    stateChanged = Signal(str)
    messageChanged = Signal(str)

    def __init__(self, bridge=None, parent=None):
        super().__init__(parent)
        self._bridge = bridge
        self._state = IDLE
        self._message = ""
        self._task = None

    # ------------------------------------------------------------- state

    @property
    def state(self):
        return self._state

    @property
    def message(self):
        return self._message

    @property
    def busy(self):
        return self._task is not None

    def _set(self, state, message=""):
        self._state = state
        self._message = message
        self.stateChanged.emit(state)
        if message:
            self.messageChanged.emit(message)

    @staticmethod
    def _log(text):
        try:
            import dev_console
            dev_console.log("addon: %s" % text)
        except Exception:                                   # noqa: BLE001
            pass

    def _run(self, work, on_done):
        task = _Task(work, self)
        self._task = task

        def finished(result):
            self._task = None
            task.deleteLater()
            on_done(result)

        task.done.connect(finished)
        task.note.connect(lambda text: self._set(self._state, text))
        task.start()

    def shutdown(self, timeout_ms=4000):
        task = self._task
        if task is None:
            return
        task.cancelled = True
        task.wait(timeout_ms)

    # ------------------------------------------------------- what is there

    def connected_addon_version(self):
        """The add-on version the bridge is actually reporting, or None."""
        try:
            return getattr(self._bridge, "addon_version", None) or None
        except Exception:                                   # noqa: BLE001
            return None

    def block_reason(self):
        """Why the add-on cannot be installed from here (None = it can).

        The chicken-and-egg case is the interesting one: an add-on older than
        0.7.0 has no `addon_update` command, so it cannot be updated by this
        route at all. It degrades like every other feature gap — this ONE
        button explains itself, nothing else changes. `save_bundled_addon` is
        the way out of it.
        """
        if self._bridge is None:
            return "Blender is not connected."
        try:
            if not getattr(self._bridge, "addon_version", None):
                return "Blender is not connected, so there is no add-on to update."
            return self._bridge.feature_reason("addon_self_update")
        except Exception:                                   # noqa: BLE001
            return None

    # ------------------------------------------------------------ install

    def install_bundled_addon(self):
        """Install the add-on this build carries.

        ⚠ Deliberately works from source as well as from a frozen build:
        whether OUR app is packaged has nothing to do with installing a Blender
        extension, and running from source is exactly when this is most useful —
        a source run does NOT otherwise carry an add-on-side fix.
        """
        import addon_bundle

        if self._task is not None:
            return
        blocked = self.block_reason()
        if blocked:
            self._set(FAILED, blocked)
            return
        target = addon_bundle.VERSION

        def work(task):
            task.note.emit("Unpacking Blender add-on %s..." % target)
            try:
                data = addon_bundle.zip_bytes()   # verifies its own hash
            except Exception as err:                        # noqa: BLE001
                return {"ok": False, "reason": "bundle_bad", "detail": str(err)}
            folder = tempfile.mkdtemp(prefix="madi_addon_")
            zip_path = os.path.join(folder, addon_bundle.file_name())
            try:
                with open(zip_path, "wb") as fh:
                    fh.write(data)
            except OSError as err:
                return {"ok": False, "reason": "write_failed", "detail": str(err)}
            try:
                return self._hand_to_blender(task, zip_path, target,
                                             addon_bundle.SHA256)
            finally:
                # Blender copied it to its own temp on the way in
                # (selfupdate.py stage()), so ours is free to go either way.
                try:
                    os.remove(zip_path)
                    os.rmdir(folder)
                except OSError:
                    pass

        self._set(INSTALLING, "Installing Blender add-on %s..." % target)
        self._run(work, self._on_addon)

    @staticmethod
    def save_bundled_addon(path):
        """Write the carried add-on to disk, for installing by hand.

        The fallback that always works: an add-on older than 0.7.0 has no
        `addon_update` command, so it cannot be told to update itself, and a
        first-ever install has no bridge at all.
        """
        import addon_bundle

        with open(path, "wb") as fh:
            fh.write(addon_bundle.zip_bytes())
        return path

    def _hand_to_blender(self, task, zip_path, target, sha256=None):
        """Give Blender a verified zip and find out what actually happened.

        Blender installs about a second AFTER replying and then reloads, which
        DROPS the bridge — so the outcome cannot come back down the connection
        that asked for it.

        ⚠⚠ **INFERRING THE OUTCOME FROM A VERSION POLL IS NOT ENOUGH, AND THAT
        IS THE LESSON OF 2026-08-14.** Blender refused a package (a BOM on its
        manifest), installed nothing, reloaded the OLD add-on over itself and
        stayed up — so the poll saw a healthy bridge on the old version until
        it timed out, and the user was told "Blender did not come back", which
        was not true and pointed at the wrong thing entirely. There are three
        checks instead of one:

            1. the package is inspected HERE before Blender is asked, so a
               broken one fails in a second with a reason;
            2. the add-on WRITES DOWN how the install went, in a file that
               survives the reload;
            3. this reads that record back and reports what it says, whether
               the bridge returns or not.
        """
        bridge = self._bridge
        # 1. Never hand Blender something we can already see it will refuse.
        #    ⚠ Its `id` is kept: that is the extension FOLDER NAME, which is
        #    how `_addon_gave_up` can look on disk and find an install the
        #    bridge poll could not see. Taken from the package rather than
        #    hardcoded, so it cannot drift from what is actually being sent.
        package_id = None
        try:
            info = inspect_addon_package(zip_path, expect_version=target)
            package_id = (info or {}).get("id")
        except ValueError as err:
            return {"ok": False, "reason": "package_bad", "detail": str(err)}
        except Exception as err:                            # noqa: BLE001
            return {"ok": False, "reason": "package_bad", "detail": str(err)}

        # A record left by a PREVIOUS attempt must never be read as this one's
        # answer, so it goes before anything is sent.
        try:
            bridgemod.clear_addon_update_result()
        except Exception:                                   # noqa: BLE001
            pass

        task.note.emit("Handing it to Blender...")
        try:
            bridge.addon_update(zip_path, version=target, sha256=sha256)
        except Exception as err:                            # noqa: BLE001
            # The add-on checks the package again on arrival, so its refusal
            # lands here — as a real sentence, on the socket, immediately.
            return {"ok": False, "reason": "bridge_failed", "detail": str(err)}

        # The bridge going away here is success, not failure.
        task.note.emit("Blender is installing it...")
        deadline = time.time() + ADDON_WAIT
        seen = None
        while time.time() < deadline and not task.cancelled:
            time.sleep(ADDON_POLL_SECONDS)

            # ⚠⚠ **THE RECORD FIRST, AND WITHOUT ASKING THE BRIDGE ANYTHING.**
            # This used to be read only in the branch where the bridge ANSWERED
            # on the wrong version — so on the ordinary successful path, where
            # installing reloads the extension and the bridge is DOWN, the loop
            # hit `continue` and never looked at the answer already sitting on
            # disk. The add-on writes it within a second or two; the app then
            # ignored it for the full 90 s and reported failure over an install
            # that had completely succeeded.
            #
            # Marty hit exactly that on 2026-08-17 ("i can't install... it
            # doesn't work") while the extension was in fact installed, and it
            # is the THIRD time a successful push has been reported as a
            # failure. **The record is the add-on's own account, written after
            # the work; a socket is a guess about whether Blender is up.**
            # Prefer the account.
            record = self._addon_record(target)
            if record:
                state = record.get("state")
                if state == "installed" and record.get("ok"):
                    # ⚠ `reloaded` is carried, not collapsed. Installed-and-
                    # reloaded is finished; installed-but-not-reloaded means
                    # Blender is still RUNNING THE OLD CODE until it restarts.
                    # Both are successful installs and neither is a failure —
                    # but only one of them is done, and the message has to say
                    # which.
                    return {"ok": True, "addon": target, "from_record": True,
                            "reloaded": bool(record.get("reloaded"))}
                if state in ("refused", "failed"):
                    return {"ok": False, "reason": "addon_refused",
                            "detail": record.get("error") or "", "seen": seen}

            try:
                # poll=False on purpose: the fail-fast gate silences REPEATING
                # background polls, and this is a user's action waiting on a
                # bridge we know is coming back.
                pong = bridge.request("ping", timeout=3.0)
            except Exception:                               # noqa: BLE001
                # ⚠ Not a failure, and not free either: a dead localhost port
                # DROPS the SYN on Windows rather than refusing, so each of
                # these burns the full 3 s. That is survivable only because the
                # record above now ends the wait long before the deadline.
                continue
            if isinstance(pong, dict):
                seen = pong.get("version") or seen
            if seen == target:
                return {"ok": True, "addon": target}
        return self._addon_gave_up(seen, target, package_id)

    @staticmethod
    def _addon_record(target=None):
        """The add-on's own account of the last install, off disk.

        ⚠ Filtered by the version we are actually waiting on. The record is one
        file in a shared folder, so a leftover from an earlier push — or from a
        test run against a throwaway package — must never be read as the answer
        to THIS one. Cleared before every attempt as well; this is the second
        lock, because "cleared it first" is an assumption and the version in
        the file is a fact.
        """
        try:
            found = bridgemod.addon_update_result()
        except Exception:                                   # noqa: BLE001
            return None
        if not found:
            return None
        if target and found.get("version") != target:
            return None
        return found

    def _addon_gave_up(self, seen, target, package_id=None):
        """Nothing matched inside the deadline. Say what actually happened
        rather than guessing — the record on disk usually knows."""
        record = self._addon_record(target)
        state = (record or {}).get("state")
        if state in ("refused", "failed"):
            return {"ok": False, "reason": "addon_refused",
                    "detail": (record or {}).get("error") or "", "seen": seen}
        if state == "installed":
            # It IS installed; only the live reload did not finish. Restarting
            # Blender completes it, and saying so is the difference between a
            # two-second fix and a re-install that was never needed.
            return {"ok": False, "reason": "addon_restart",
                    "detail": (record or {}).get("error") or "",
                    "addon": target}
        if state == "staged":
            return {"ok": False, "reason": "addon_stalled", "detail": ""}

        # ⚠⚠ "THE BRIDGE ANSWERED ON THE WRONG VERSION" AND "BLENDER WENT AWAY"
        # ARE DIFFERENT FAILURES AND USED TO SHARE ONE MESSAGE. `seen` is set
        # only by a reply, so a version here PROVES Blender was answering the
        # whole time — telling that user to restart Blender because it "stopped
        # answering" is the same class of lie as the one the comment in
        # `_on_addon` was written about, and it cost an evening on 2026-08-15.
        if seen:
            # Did it install somewhere the poll could not see? The bridge is
            # ONE port and an install is PER BLENDER VERSION, so the instance
            # that took the package can stop being the instance we are talking
            # to — that is not a corner case, it is what a reload CAUSES.
            elsewhere = {blender: found for blender, found
                         in installed_addon_versions(package_id).items()
                         if found == target} if package_id else {}
            if elsewhere:
                where = ", ".join("Blender %s" % b for b in sorted(elsewhere))
                return {"ok": False, "reason": "addon_other_blender",
                        "detail": "%s now has %s; the Blender this app is "
                                  "connected to still reports %s."
                                  % (where, target, seen),
                        "addon": target, "seen": seen}
            return {"ok": False, "reason": "addon_not_installed",
                    "detail": "It is still reporting %s." % seen, "seen": seen}
        return {"ok": False, "reason": "addon_timeout", "detail": "?"}

    def _on_addon(self, result):
        if result.get("ok"):
            self._log("add-on installed: %s" % result.get("addon"))
            # ⚠ Two true sentences, not one. When the answer came from the
            # RECORD the bridge has not necessarily come back yet, and saying
            # "installed" while the status bar still reads disconnected is the
            # kind of half-truth that sent us looking for a broken installer.
            # Name the reload so the disconnected second is expected.
            if result.get("from_record") and not result.get("reloaded"):
                # Installed, but Blender is still running the old code.
                self._set(IDLE, "Blender add-on %s installed — restart Blender "
                                "to finish loading it."
                          % result.get("addon"))
            elif result.get("from_record"):
                self._set(IDLE, "Blender add-on %s installed — Blender is "
                                "reloading it, so the connection returns in a "
                                "moment." % result.get("addon"))
            else:
                self._set(IDLE,
                          "Blender add-on %s installed." % result.get("addon"))
            return
        reason = result.get("reason")
        detail = result.get("detail") or ""
        self._log("add-on install failed: %s %s" % (reason, detail))
        # ⚠ EVERY MESSAGE HERE NAMES WHAT WENT WRONG AND WHAT TO DO NEXT. The
        # one this replaced said "Blender did not come back — restart Blender"
        # for a failure in which Blender never went anywhere, and that sentence
        # is what a whole evening of looking in the wrong place was built on.
        # A message that guesses is worse than no message.
        base = {
            "bundle_bad": "The add-on packed into this build could not be "
                          "unpacked, so nothing was sent to Blender.",
            "write_failed": "The add-on could not be written to a temporary "
                            "file, so it was not sent.",
            "package_bad": "That add-on package is not one Blender can install, "
                           "so it was not sent.",
            "bridge_failed": "Blender would not accept the add-on.",
            "addon_refused": "Blender refused the add-on package and installed "
                             "nothing.",
            # ⚠ THE COMMON CASE, AND IT IS A SUCCESS. The add-on is on disk and
            # loaded; only its bridge did not come back on its own after the
            # reload. Pressing Start reconnects in a second — telling someone to
            # re-install (or leaving them watching a spinner) is what this
            # message exists to stop.
            "addon_restart": "The add-on installed. Blender's bridge did not "
                             "come back on its own — press Start in Blender's "
                             "MadihsonNSFW sidebar, or restart Blender, to "
                             "reconnect.",
            "addon_stalled": "Blender accepted the add-on but never installed "
                             "it. Restart Blender and try again.",
            # ⚠ ANOTHER SUCCESS WEARING A FAILURE'S CLOTHES, and the reason
            # this pair exists (2026-08-15). With two Blenders open, the add-on
            # goes to whichever one HOLDS THE BRIDGE — and installing makes
            # that one reload, which frees the port for the other. So the
            # install lands in Blender A while the app ends up polling Blender
            # B, which will never report the new version however long it waits.
            "addon_other_blender": "The add-on installed — but into the Blender "
                                   "that was holding the bridge, which is not "
                                   "the one this app is connected to now. Start "
                                   "the bridge in the Blender you want updated "
                                   "(MadihsonNSFW sidebar ▸ Start), then press "
                                   "Update add-on again.",
            "addon_not_installed": "Blender stayed connected but never picked "
                                   "up the new add-on. Restart Blender and try "
                                   "again.",
            "addon_timeout": "Blender stopped answering while installing the "
                             "add-on — restart Blender, then check its version "
                             "in Settings.",
        }.get(reason, "The add-on was not installed.")
        # The detail is the add-on's or Blender's own words. Carrying it
        # through is what turns "it failed" into something fixable.
        if detail and reason in ("bundle_bad", "write_failed", "package_bad",
                                 "bridge_failed", "addon_refused",
                                 "addon_restart", "addon_other_blender",
                                 "addon_not_installed"):
            base = "%s %s" % (base, detail)
        self._set(FAILED, base)
