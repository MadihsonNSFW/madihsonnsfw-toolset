"""The self-updater: check, download, verify, swap, prove, restart.

⚠ UPDATING IS OPEN TO EVERYONE (Marty, 2026-08-06: "anybody can check for
update AND update the app"). There is no licence gate here and none on the
server either. This REVERSED the original design, which required a freshly
re-checked ACTIVE licence on the client and re-resolved the token on both
server routes, so if you are reading old comments elsewhere that describe "the
two gates", they are gone.

WHY IT COSTS NOTHING TO GIVE AWAY. The paid modules ship inside the exe and are
unlocked at runtime by the licence - the "withhold the paid code from the build"
phase was never built. So everyone holding the app already holds every byte of
it, and refusing them a newer copy of the same program protected nothing while
guaranteeing that the users on the most broken builds were exactly the ones who
could not fix them. What a lapsed or revoked licence loses is the paid TABS, and
that is `licensing/`'s job, not this module's.

The licence manager is still injected, for one much smaller reason: a token, if
there is one, is sent so the server can attribute the download in its ledger.
It can no longer refuse anything.

Everything runs off the GUI thread (the same _Task pattern as the bridge and
the licence manager), and a failed check is silent unless the user asked for it.
An update check must never be the reason the app stalls or the reason someone
is locked out.
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

from PySide6.QtCore import QObject, QThread, Signal, QTimer

import version
# Not for talking to Blender - the Bridge instance is injected. This is for the
# two file helpers that read the add-on's own record of an install, which is
# the only channel that survives the reload (`bridge.addon_update_result`).
import bridge as bridgemod
# `lic` is still needed for SERVER_URL and the TLS context - the update routes
# live on the licence server. The `licensing` package itself is no longer
# imported here: nothing in this module judges a licence any more.
from licensing import manager as lic

from . import offer as offer_mod
from . import swap

# States
IDLE = "idle"
CHECKING = "checking"
AVAILABLE = "available"
DOWNLOADING = "downloading"
INSTALLING = "installing"
READY = "ready"            # installed; a restart finishes the job
FAILED = "failed"
UNSUPPORTED = "unsupported"

CHECK_TIMEOUT = 20
# A file, not a JSON answer. The licence client's 8 s is right for a small
# reply and far too short for 1.5 MB on a bad connection.
DOWNLOAD_TIMEOUT = 180
# Long enough that startup, the licence check and the bridge handshake are all
# finished and the user is looking at a working app before anything is asked.
STARTUP_DELAY_MS = 12000

# After handing Blender the add-on: it installs ~1.5 s later and then reloads,
# which drops the bridge for a few seconds. A big scene makes re-registering
# slower, so the wait is generous - the alternative is telling someone their
# update failed while it is still working.
ADDON_POLL_SECONDS = 1.0
ADDON_WAIT = 90

# Where Blender keeps user-installed extensions, per Blender version. Used ONLY
# to answer "did the add-on actually land somewhere?" when the bridge poll
# could not — see `installed_addon_versions`.
_EXT_TAIL = ("extensions", "user_default")
_VERSION_LINE = re.compile(r'^\s*version\s*=\s*["\']([^"\']+)["\']', re.M)


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
    had silently become a different instance, timed out, and told Marty
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


def is_supported():
    """Can this build update itself at all?

    Frozen only - the same boundary as the licence gate. From source there is
    nothing to swap: those files are the developer's working copy, and an
    "update" would overwrite them.
    """
    return bool(getattr(sys, "frozen", False))


# ----------------------------------------------------------------- network


def _post_json(path, body, timeout=CHECK_TIMEOUT):
    data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        lic.SERVER_URL + path, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout,
                                context=lic.ssl_context()) as response:
        return json.loads(response.read().decode("utf-8"))


def _post_bytes(path, body, expected_size, timeout=DOWNLOAD_TIMEOUT):
    """Fetch one file. Refuses anything bigger than the manifest promised.

    The cap is not paranoia about our own server - it is what stops a wrong
    answer, from anywhere, being read into memory until the machine gives up.
    """
    data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        lic.SERVER_URL + path, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout,
                                context=lic.ssl_context()) as response:
        payload = response.read(expected_size + 1)
    if len(payload) != expected_size:
        return None
    return payload


def _call(fn, *args, **kwargs):
    """Every failure becomes a dict, never an exception - same contract as the
    licence client, and for the same reason: an HTTP error still carries a
    signed body that has to be read."""
    try:
        return fn(*args, **kwargs)
    except urllib.error.HTTPError as err:
        try:
            return json.loads(err.read().decode("utf-8"))
        except Exception:
            return {"ok": False, "reason": "http_%d" % err.code}
    except Exception:
        return {"ok": False, "reason": "unreachable"}


class _Task(QThread):
    """One background job. The manager owns it; results come back as a signal
    on the GUI thread."""

    done = Signal(object)
    note = Signal(str)
    tick = Signal(int, int)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self._fn = fn
        self.cancelled = False

    def run(self):
        try:
            result = self._fn(self)
        except Exception as err:
            result = {"ok": False, "reason": "client_error", "detail": str(err)}
        if not self.cancelled:
            self.done.emit(result)


class UpdateManager(QObject):
    stateChanged = Signal(str)
    messageChanged = Signal(str)
    progress = Signal(int, int)   # bytes done, bytes total

    def __init__(self, license_manager, bridge=None, parent=None):
        super().__init__(parent)
        self._license = license_manager
        self._bridge = bridge
        self._state = IDLE if is_supported() else UNSUPPORTED
        self._message = ""
        self._task = None
        self._offer = None
        self._installed = []      # what apply() put in place, for a rollback
        self._manual = False

    def _token(self):
        """The licence token if we hold one, else "".

        Sent so the server can attribute a download in its ledger, and for
        nothing else - an update is served with or without it. Never assume
        `_license` has a record: an unlicensed install updates too, and that is
        the normal case for anyone on the free tabs.
        """
        try:
            return self._license._record.get("token") or ""
        except Exception:
            return ""

    # ------------------------------------------------------------- state

    @property
    def state(self):
        return self._state

    @property
    def message(self):
        return self._message

    @property
    def offer(self):
        return self._offer

    @property
    def busy(self):
        return self._state in (CHECKING, DOWNLOADING, INSTALLING)

    def _set(self, state, message=""):
        changed = state != self._state
        self._state = state
        self._message = message
        if message:
            self.messageChanged.emit(message)
        if changed:
            self.stateChanged.emit(state)

    def _fail(self, reason, detail=""):
        """Give up. Quiet unless the user asked, because a background check
        that failed is not news - it will run again."""
        self._log("update check failed: %s %s" % (reason, detail))
        if self._manual:
            self._set(FAILED, reason)
        else:
            self._set(IDLE)

    @staticmethod
    def _log(text):
        try:
            import dev_console

            dev_console.BUFFER.add("INFO", text)
        except Exception:
            pass

    # ------------------------------------------------------------ startup

    def start(self, auto):
        """Clean up after the last update, then optionally look for the next.

        Cleanup runs even when auto-checking is off: those .madi_old files are
        the previous build sitting on disk, and the process that was holding
        them has now gone.
        """
        if not is_supported():
            return
        root = swap.app_root()
        removed = swap.cleanup(root)
        if removed:
            self._log("update: cleaned up %d file(s) from the previous version" % removed)
        if auto:
            QTimer.singleShot(STARTUP_DELAY_MS, lambda: self.check(manual=False))

    # -------------------------------------------------------------- check

    def check(self, manual=False):
        """Ask whether there is a newer build. No licence involved.

        This used to re-check the licence first and refuse on anything but a
        fresh ACTIVE. See the module docstring for why that went.
        """
        self._manual = manual
        if not is_supported():
            self._set(UNSUPPORTED,
                      "Updates apply to the installed app - this is running from source.")
            return
        if self._task is not None:
            return
        self._set(CHECKING, "Checking for updates...")
        self._start_check()

    def _start_check(self):
        import secrets

        token = self._token()
        nonce = secrets.token_urlsafe(24)
        current = version.APP_VERSION
        current_addon = self.connected_addon_version()

        def work(_task):
            return _call(_post_json, "/update/check",
                         {"token": token, "nonce": nonce, "version": current,
                          "addon_version": current_addon or ""})

        self._run(work,
                  lambda result: self._on_check(result, nonce, current, current_addon))

    def connected_addon_version(self):
        """What Blender actually has loaded right now, or None.

        Never asked over the network and never guessed: if Blender is not
        running there is no add-on to update, and the release's add-on half is
        simply ignored until there is.
        """
        bridge = self._bridge
        if bridge is None:
            return None
        try:
            return getattr(bridge, "addon_version", None)
        except Exception:
            return None

    def _on_check(self, result, nonce, current, current_addon=None):
        if not result.get("ok"):
            # "We could not reach it" and "it answered, just not with an update"
            # are different facts and must not share a message - the same
            # distinction licensing rule 1 exists for. A 404 (a server that
            # predates the update routes) is NOT an outage, and saying it is
            # sends someone off checking their internet connection.
            reason = str(result.get("reason") or result.get("error") or "")
            if reason in ("not found", "http_404"):
                text = "This server is not offering updates yet."
            elif reason in ("revoked", "suspended"):
                text = "Your licence was withdrawn, so updates are not available."
            elif reason in ("unknown_token",):
                text = "This licence is not recognised by the update server."
            elif reason in ("unreachable", "client_error", ""):
                text = "Could not reach the update server."
            else:
                text = "The update server could not be asked just now."
            return self._fail(text, reason)
        if not result.get("update"):
            self._offer = None
            self._set(IDLE, "You are on the latest version." if self._manual else "")
            return
        # ⚠ There used to be an `anonymous` branch here that turned a tokenless
        # reply into "a newer version exists, sign in to install it". The server
        # no longer sends that flag - an unlicensed caller gets the same full
        # signed offer as anybody else - so the branch is gone rather than left
        # sitting unreachable.
        parsed, reason = offer_mod.parse(result.get("payload"), result.get("sig"),
                                         nonce, current, current_addon)
        if parsed is None:
            # Never shown verbatim: why a signature failed is only interesting
            # to someone attacking it.
            return self._fail("The update could not be verified, so it was not "
                              "installed.", reason)
        self._offer = parsed
        if parsed.app_newer:
            self._set(AVAILABLE, "Version %s is available." % parsed.version)
        elif parsed.addon_newer:
            self._set(AVAILABLE, "A newer Blender add-on (%s) is available."
                      % parsed.addon["version"])
        else:
            self._offer = None
            self._set(IDLE, "You are on the latest version." if self._manual else "")

    # ------------------------------------------------------------ install

    def install(self):
        """Download, verify, swap, and prove the new build starts."""
        if self._state != AVAILABLE or self._offer is None or self._task is not None:
            return
        if not self._offer.app_newer:
            # The release was worth fetching for its ADD-ON half only.
            return
        root = swap.app_root()
        if not root:
            return self._fail("There is nothing to update here.")
        token = self._token()
        current = self._offer

        def work(task):
            needed = swap.plan(root, current)
            if not needed:
                return {"ok": True, "installed": [], "nothing": True}
            total = sum(item["size"] for item in needed)
            got = 0
            task.note.emit("Downloading %s (%.1f MB)..."
                           % (current.version, total / 1048576.0))
            # Identical files share a hash, so a release that ships the same
            # bytes under two names is fetched once.
            fetched = set()
            for item in needed:
                if task.cancelled:
                    return {"ok": False, "reason": "cancelled"}
                if item["sha256"] not in fetched:
                    data = _post_bytes("/update/download",
                                       {"token": token, "sha256": item["sha256"]},
                                       item["size"])
                    if data is None:
                        return {"ok": False, "reason": "download_failed",
                                "detail": item["path"]}
                    if not swap.write_staged(root, item, data):
                        # The bytes did not match the SIGNED hash. That is the
                        # boundary this whole channel rests on.
                        return {"ok": False, "reason": "hash_mismatch",
                                "detail": item["path"]}
                    fetched.add(item["sha256"])
                got += item["size"]
                task.tick.emit(got, total)

            if not swap.verify_staged(root, needed):
                return {"ok": False, "reason": "hash_mismatch", "detail": "staging"}

            task.note.emit("Installing...")
            ok, err = swap.apply(root, needed)
            if not ok:
                return {"ok": False, "reason": "install_failed", "detail": err or ""}

            # PROVE IT before committing. If the new build cannot start, the old
            # one goes straight back and the user never meets a dead app.
            task.note.emit("Checking the new version starts...")
            started, why = swap.smoke(swap.exe_path())
            if not started:
                swap.rollback(root, needed)
                return {"ok": False, "reason": "rolled_back", "detail": why or ""}
            return {"ok": True, "installed": needed}

        self._set(DOWNLOADING, "Downloading update...")
        self._run(work, self._on_install)

    def _on_install(self, result):
        if not result.get("ok"):
            reason = result.get("reason")
            detail = str(result.get("detail") or "")
            self._log("update install failed: %s %s" % (reason, detail))
            text = {
                "hash_mismatch": "The download did not match what the server signed, "
                                 "so nothing was changed.",
                "download_failed": "The download did not finish, so nothing was changed.",
                "install_failed": "The update could not be written. Close anything "
                                  "using the app folder and try again.",
                "rolled_back": "The new version did not start, so the previous one "
                               "was put back.",
                "cancelled": "Update cancelled.",
            }.get(reason, "The update did not finish, so nothing was changed.")
            self._set(FAILED, text)
            return
        if result.get("nothing"):
            self._set(IDLE, "Already up to date.")
            return
        self._installed = result.get("installed") or []
        self._set(READY, "Version %s is installed - restart to use it."
                  % (self._offer.version if self._offer else ""))

    # ----------------------------------------------------------- the add-on

    def addon_available(self):
        """Is there a newer Blender add-on in the current offer, for the add-on
        that is actually connected?"""
        item = self._offer
        return bool(item and item.addon and item.addon_newer)

    def addon_block_reason(self):
        """Why the add-on cannot be installed from here (None = it can).

        The chicken-and-egg case is the interesting one: an add-on older than
        0.7.0 has no `addon_update` command, so it cannot be updated by this
        route at all. It degrades like every other feature gap - this ONE
        button explains itself, nothing else changes.
        """
        if self._bridge is None:
            return "Blender is not connected."
        try:
            if not getattr(self._bridge, "addon_version", None):
                return "Blender is not connected, so there is no add-on to update."
            return self._bridge.feature_reason("addon_self_update")
        except Exception:
            return None

    def _hand_to_blender(self, task, zip_path, target, sha256=None):
        """Give Blender a verified zip and find out what actually happened.

        Shared by both routes into an add-on install (a release offer, and the
        copy carried inside the app). Blender installs about a second AFTER
        replying and then reloads, which DROPS the bridge - so the outcome
        cannot come back down the connection that asked for it.

        ⚠⚠ **INFERRING THE OUTCOME FROM A VERSION POLL IS NOT ENOUGH, AND THAT
        IS THE LESSON OF 2026-08-14.** Blender refused a package (a BOM on its
        manifest), installed nothing, reloaded the OLD add-on over itself and
        stayed up - so the poll saw a healthy bridge on the old version until
        it timed out, and the user was told "Blender did not come back", which
        was not true and pointed at the wrong thing entirely. There are now
        three checks instead of one:

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
            info = offer_mod.inspect_addon_package(zip_path,
                                                   expect_version=target)
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
        except Exception as err:
            # The add-on checks the package again on arrival, so its refusal
            # lands here - as a real sentence, on the socket, immediately.
            return {"ok": False, "reason": "bridge_failed", "detail": str(err)}

        # The bridge going away here is success, not failure.
        task.note.emit("Blender is installing it...")
        deadline = time.time() + ADDON_WAIT
        seen = None
        while time.time() < deadline and not task.cancelled:
            time.sleep(ADDON_POLL_SECONDS)
            try:
                # poll=False on purpose: the fail-fast gate silences REPEATING
                # background polls, and this is a user's action waiting on a
                # bridge we know is coming back.
                pong = bridge.request("ping", timeout=3.0)
            except Exception:
                continue
            if isinstance(pong, dict):
                seen = pong.get("version") or seen
            if seen == target:
                return {"ok": True, "addon": target}
            # It answered, on the wrong version. Either it has not installed
            # yet, or it never will - and the record on disk knows which.
            record = self._addon_record(target)
            if record and record.get("state") in ("refused", "failed"):
                return {"ok": False, "reason": "addon_refused",
                        "detail": record.get("error") or "", "seen": seen}
        return self._addon_gave_up(seen, target, package_id)

    @staticmethod
    def _addon_record(target=None):
        """The add-on's own account of the last install, off disk.

        ⚠ Filtered by the version we are actually waiting on. The record is one
        file in a shared folder, so a leftover from an earlier push - or from a
        test run against a throwaway package - must never be read as the answer
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
        rather than guessing - the record on disk usually knows."""
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
        # `_on_addon` was written about, and it cost Marty an evening on
        # 2026-08-15.
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

    def install_bundled_addon(self):
        """Install the add-on the app carries, with no server involved.

        Deliberately NOT gated on is_supported(): whether OUR app is a frozen
        build has nothing to do with installing a Blender extension, and running
        from source is exactly when this is most useful.
        """
        import addon_bundle

        if self._task is not None:
            return
        blocked = self.addon_block_reason()
        if blocked:
            self._set(FAILED, blocked)
            return
        target = addon_bundle.VERSION

        def work(task):
            task.note.emit("Unpacking Blender add-on %s..." % target)
            try:
                data = addon_bundle.zip_bytes()   # verifies its own hash
            except Exception as err:
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
                # Blender copied it to its own temp on the way in (selfupdate.py
                # stage()), so ours is free to go either way.
                try:
                    os.remove(zip_path)
                    os.rmdir(folder)
                except OSError:
                    pass

        self._set(INSTALLING, "Installing Blender add-on %s..." % target)
        self._run(work, self._on_addon)

    def save_bundled_addon(self, path):
        """Write the carried add-on to disk, for installing by hand.

        The fallback that always works: an add-on older than 0.7.0 has no
        `addon_update` command, so it cannot be told to update itself, and a
        first-ever install has no bridge at all.
        """
        import addon_bundle

        with open(path, "wb") as fh:
            fh.write(addon_bundle.zip_bytes())
        return path

    def install_addon(self):
        """Download the add-on offered by a release, verify it, install it."""
        if self._task is not None or not self.addon_available():
            return
        blocked = self.addon_block_reason()
        if blocked:
            self._set(FAILED, blocked)
            return
        root = swap.app_root()
        if not root:
            return self._fail("There is nowhere to download the add-on to.")
        token = self._token()
        item = dict(self._offer.addon)
        target = item["version"]

        def work(task):
            task.note.emit("Downloading Blender add-on %s..." % target)
            data = _post_bytes("/update/download",
                               {"token": token, "sha256": item["sha256"]},
                               item["size"])
            if data is None:
                return {"ok": False, "reason": "download_failed"}
            if not swap.write_staged(root, item, data):
                return {"ok": False, "reason": "hash_mismatch"}
            # Hand Blender a file with its real .zip name; the staging copy is
            # named by hash, and an extension is installed from a .zip.
            staged = swap.staged_path(root, item)
            zip_path = os.path.join(os.path.dirname(staged), item["path"])
            try:
                os.replace(staged, zip_path)
            except OSError:
                zip_path = staged
            return self._hand_to_blender(task, zip_path, target, item["sha256"])

        self._set(INSTALLING, "Installing the Blender add-on...")
        self._run(work, self._on_addon)

    def _on_addon(self, result):
        if result.get("ok"):
            self._log("add-on updated to %s" % result.get("addon"))
            self._offer = None
            self._set(IDLE, "Blender add-on %s installed." % result.get("addon"))
            return
        reason = result.get("reason")
        detail = result.get("detail") or ""
        self._log("add-on update failed: %s %s" % (reason, detail))
        # ⚠ EVERY MESSAGE HERE NAMES WHAT WENT WRONG AND WHAT TO DO NEXT. The
        # one this replaced said "Blender did not come back — restart Blender"
        # for a failure in which Blender never went anywhere, and that sentence
        # is what a whole evening of looking in the wrong place was built on.
        # A message that guesses is worse than no message.
        base = {
            "hash_mismatch": "The add-on download did not match what the server "
                             "signed, so it was not installed.",
            "download_failed": "The add-on download did not finish.",
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
        if detail and reason in ("package_bad", "bridge_failed",
                                 "addon_refused", "addon_restart",
                                 "addon_other_blender", "addon_not_installed"):
            base = "%s %s" % (base, detail)
        self._set(FAILED, base)

    # ------------------------------------------------------------ restart

    def restart(self):
        """Relaunch and quit. Detached, so the new process does not die with us.

        The path is unchanged by design, so this is also the same path the
        add-on's "Open Toolset App" button uses.
        """
        exe = swap.exe_path()
        if not exe:
            return False
        try:
            flags = (getattr(subprocess, "DETACHED_PROCESS", 0)
                     | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
            subprocess.Popen([exe], cwd=os.path.dirname(exe), close_fds=True,
                             creationflags=flags)
        except Exception as err:
            self._log("update: could not relaunch: %s" % err)
            return False
        return True

    # --------------------------------------------------------------- plumbing

    def shutdown(self, timeout_ms=4000):
        """Same rule as the licence manager: never let a running QThread be
        destroyed, because Qt aborts the process when that happens.

        Latent here rather than proven - the update check is on a 12 s timer, so
        it has never been mid-flight at teardown. A DOWNLOAD easily could be,
        and it is the same crash. Bounded wait: a stalled socket must not turn
        closing the app into a hang.
        """
        task = self._task
        if task is None:
            return
        task.cancelled = True
        try:
            task.wait(timeout_ms)
        except Exception:
            pass
        self._task = None

    def _run(self, work, on_done):
        task = _Task(work, self)
        self._task = task

        def finished(result):
            self._task = None
            on_done(result)

        task.done.connect(finished)
        task.note.connect(lambda text: self.messageChanged.emit(text))
        task.tick.connect(lambda done, total: self.progress.emit(done, total))
        task.finished.connect(task.deleteLater)
        task.start()
