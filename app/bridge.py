"""TCP client for the Blender bridge add-on (newline-delimited JSON)."""

import json
import os
import socket
import threading
import time

# Where the add-on leaves the secret that proves a caller can read this user's
# files — the guard on `addon_update`, the one command that installs code.
# Mirrors blender_addon\…\bridgeauth.py; the app's licence store already owns
# this folder (app\licensing\store.APP_FOLDER).
BRIDGE_TOKEN_FILE = "bridge.token"


# Where the add-on writes down how the last `addon_update` went. It has to be a
# file: installing reloads the extension, which purges its modules and drops
# the socket, so there is no connection left to report on by the time the
# outcome is known. Mirrors `selfupdate.RESULT_NAME`.
ADDON_RESULT_FILE = "addon_update_result.json"


def _app_folder():
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "MadihsonNSFW Toolset")


def bridge_token():
    """The current bridge token, or "" if there is none to read.

    Read fresh on every use rather than cached: the add-on mints a new one each
    time the bridge starts, so a cached value goes stale the first time Marty
    restarts Blender — and the symptom would be "Update add-on stopped working"
    with nothing to point at.
    """
    path = os.path.join(_app_folder(), BRIDGE_TOKEN_FILE)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError:
        return ""


def addon_result_path():
    return os.path.join(_app_folder(), ADDON_RESULT_FILE)


def addon_update_result():
    """How the add-on's last self-install went, read straight off disk.

    ⚠ **THE LAST-RESORT CHANNEL, AND THE ONLY ONE THAT WORKS WHEN THE BRIDGE
    DOES NOT COME BACK.** `addon_status` carries the same record, but a reload
    that leaves the bridge down (or a Blender that was closed mid-update) means
    there is nobody to ask — and that is exactly the case where the user most
    needs to be told what happened. A file survives all of it.
    """
    try:
        with open(addon_result_path(), "r", encoding="utf-8") as handle:
            found = json.load(handle)
    except (OSError, ValueError):
        return None
    return found if isinstance(found, dict) else None


def clear_addon_update_result():
    """Drop the previous outcome before starting a new install, so a stale
    record can never be read as this attempt's answer."""
    try:
        os.remove(addon_result_path())
    except OSError:
        pass

# ---------------------------------------------------------------------------
# Not-reachable handling. Measured 2026-08-02 with the bridge stopped: a
# connect to 127.0.0.1:9877 does NOT come back refused — the SYN is dropped, so
# every attempt burns the FULL timeout. With pollers calling on the GUI thread
# (status every 5 s, Anim Layers every 1.5 s at a 10 s timeout) that is exactly
# the "app hangs every 10 seconds" Marty hit whenever Blender's server is off.
#
# Two rules fix it at the source, for every caller at once:
#   1. Connecting is capped separately from the command timeout. A localhost
#      connect to a listening socket is sub-millisecond; a long timeout only
#      ever buys waiting for something that isn't there. The generous
#      per-command timeout still applies to the REPLY, so bakes and renders are
#      unaffected.
#   2. Once a connect fails, everything else fails INSTANTLY for a while.
#      Only a designated prober (`probe=True`, the app's off-thread status
#      poll) keeps knocking, so no UI code ever waits on a dead port.
# Measured on this machine: connecting to a LISTENING localhost socket takes
# ~15 ms (max 16 ms over 200 samples), so 250 ms is a 15x margin on the only
# case that matters — and it caps what a doomed attempt can cost the caller.
CONNECT_TIMEOUT = 0.25
# Longer than the app's slow health-poll interval (main.SLOW_STATUS_MS), so a
# probe that keeps failing keeps the gate shut and NO other caller ever
# attempts a connect. It's still an expiry rather than a latch, so the gate
# can't wedge shut if polling ever stops.
UNREACHABLE_BACKOFF = 30.0


# The add-on version this build of the app was written against. The bridge
# reports its own (core.ADDON_VERSION) in ping/status; MainWindow.
# update_bridge_status warns when they differ, which catches the "rebuilt the
# exe but forgot to reinstall the extension" case (and the reverse). Bump it
# together with blender_manifest.toml whenever new commands land.
EXPECTED_ADDON_VERSION = "0.46.0"

# ---------------------------------------------------------------------------
# Update safety: a version gap must DEGRADE, never break.
#
# The rule for this project (see docs\addon-bridge.md "Compatibility contract"):
# the app never assumes a command exists. The add-on advertises the commands it
# actually answers (`capabilities` in ping/status, derived from its dispatcher's
# source so it can't go stale), and every feature that needs a command added
# after the app's baseline declares it here. If the installed add-on lacks it,
# that ONE feature switches itself off with a plain-English reason — everything
# else keeps working exactly as before.
#
# So: a NEW app on an OLD add-on loses only the new features. An OLD app on a
# NEW add-on is unaffected (unknown capability names it never asks about, and
# extra reply keys are ignored — verified). Nothing throws either way.
#
# feature key -> (bridge command, add-on version that introduced it, message)
# The version is the FALLBACK: an add-on old enough not to advertise
# capabilities at all still reports a version, so we can still answer
# "can it do this?" without guessing.
FEATURE_REQUIREMENTS = {
    "background_playblast": (
        "snapshot_blend", "0.4.2",
        "Background playblast needs Blender add-on 0.4.2 or newer — reinstall "
        "the extension to enable it."),
    "bone_jiggle": (
        "jiggle_status", "0.6.0",
        "Bone Jiggle needs Blender add-on 0.6.0 or newer — reinstall the "
        "extension to enable it."),
    "bone_jiggle_bake": (
        "jiggle_bake", "0.6.0",
        "Baking the jiggle to keyframes needs Blender add-on 0.6.0 or newer — "
        "reinstall the extension to enable it."),
    # The chicken-and-egg one: an add-on too old to update itself cannot be
    # updated by this route, so the app offers the zip and the manual install
    # instead. Exactly the same shape as every other gate — one control turns
    # itself off with a reason, nothing else changes.
    "addon_self_update": (
        "addon_update", "0.7.0",
        "Installing the Blender add-on from here needs add-on 0.7.0 or newer — "
        "save the package and install it in Blender this once, and it can "
        "update itself from then on."),
    "nsfw_assets": (
        "asset_build", "0.8.0",
        "Adding the MADI rigs needs Blender add-on 0.8.0 or newer — update the "
        "extension to enable it."),
    # Sharing the Anim Layers settings with Blender's own panel. An older
    # add-on simply has no panel to share them with, so the app keeps its own
    # copy and nothing else changes — the tab works exactly as before.
    "anim_layers_shared_prefs": (
        "anim_layers_set_prefs", "0.9.0",
        "Sharing the Anim Layers settings with Blender's N-panel needs add-on "
        "0.9.0 or newer. Until then the app keeps its own copy."),
    # Unlocking the add-on's own paid panels. An older add-on has no paid panels
    # to unlock, so there is nothing to report and nothing breaks.
    "license_unlock": (
        "license_unlock", "0.9.0",
        "Unlocking the Blender panels needs add-on 0.9.0 or newer."),
    # The Bone picker tab. On an older add-on there is no picker in Blender at
    # all, so this ONE tab reports why and does nothing — every other tab is
    # untouched. That is the whole compatibility contract in one line.
    "bone_picker": (
        "picker_status", "0.10.0",
        "The Bone picker needs Blender add-on 0.10.0 or newer — update the "
        "extension from ⚙ Library Settings to enable it."),
    # The Optimization tab. Same shape again: an older add-on costs this one
    # tab, with the reason on the control, and nothing else notices.
    "scene_optimizer": (
        "opt_status", "0.11.0",
        "The Scene Optimizer needs Blender add-on 0.11.0 or newer — update the "
        "extension from ⚙ Library Settings to enable it."),
    # Key / un-key from the Anim Layers tab. An older add-on costs those TWO
    # BUTTONS, with the reason on them, and every other thing in the tab keeps
    # working — the tab is not switched off over it.
    "anim_layers_keying": (
        "anim_layers_key_selection", "0.14.0",
        "Setting a keyframe from here needs Blender add-on 0.14.0 or newer — "
        "update the extension from ⚙ Library Settings to enable it."),
    # Weight-paint previews for .vgroups items. An older add-on costs the
    # PICTURE, never the item: the groups are saved either way, the tile just
    # keeps the plain type glyph. Degrading to a normal viewport shot would be
    # worse than none — a grey mesh where weights are expected looks like the
    # weights failed to save.
    "vgroup_preview": (
        "capture_vgroup_preview", "0.16.0",
        "Weight-paint previews need Blender add-on 0.16.0 or newer — update "
        "the extension from ⚙ Library Settings to enable them."),
    # Render presets. The whole tool is one feature: without the add-on there
    # is nothing to read settings out of and nothing to write them back into,
    # so the page shows the reason instead of a list of presets you could save
    # but never apply. Saved presets on disk are untouched either way.
    "render_presets": (
        "render_preset_capture", "0.17.0",
        "Render presets need Blender add-on 0.17.0 or newer — update the "
        "extension from ⚙ Library Settings to enable them."),
    # Telling Blender about a render the APP made, so the N-panel's Watch
    # button knows about background playblasts too. On an older add-on the
    # app's own Watch button still works — it reads the same shared file
    # directly — and only Blender's copy of the button stays empty.
    "note_render": (
        "note_render", "0.20.0",
        "Telling Blender about a background render needs add-on 0.20.0 or "
        "newer — the app's own Watch button works either way."),
    # ⚠ THE SAVE-ANIM OPTIONS (keep_modifiers / include_props, add-on 0.20.0)
    # DELIBERATELY HAVE NO ENTRY HERE, and neither does `status.output_dir`.
    # Both are things an OLD ADD-ON CANNOT BE ASKED ABOUT BY NAME: one is a
    # parameter on a command that has always existed, the other a field on a
    # reply. A capability gate reads command names, so it would answer "yes"
    # for both and prove nothing. `save_anim` ECHOES its options back (the
    # `save_abc` rule) and the app says so when the echo is missing;
    # `output_dir` simply arrives as None and the dialog keeps its own default.
    # ⚠ `opt_progress` (add-on 0.12.0) DELIBERATELY HAS NO ENTRY HERE. An entry
    # would switch the whole Optimization tab off on 0.11.0, and 0.11.0
    # optimizes perfectly well — it just cannot say how far along it is. So the
    # tab asks `bridge.supports("opt_progress")` and shows a busy bar instead of
    # a counting one. Degrade the FEATURE, not the tab. (docs\optimizer.md)
    # The Node Editor's bake pipeline. An older add-on costs the BAKE — the
    # canvas still draws and wires, the Bake button carries the reason.
    "texture_bake": (
        "bake_texture", "0.24.0",
        "Baking needs Blender add-on 0.24.0 or newer — update the extension "
        "from ⚙ Library Settings to enable it."),
    "bake_all_slots": (
        "bake_targets", "0.26.0",
        "Bake all slots needs Blender add-on 0.26.0 or newer — update the "
        "extension from ⚙ Library Settings, or untick it to bake this one "
        "material."),
    "bulk_bake": (
        "bake_targets", "0.26.0",
        "Bulk bake needs Blender add-on 0.26.0 or newer — update the "
        "extension from ⚙ Library Settings to enable it."),
    "bake_replace": (
        "apply_baked_material", "0.27.0",
        "Replace shader needs Blender add-on 0.27.0 or newer — update the "
        "extension from ⚙ Library Settings, or untick it to just save the "
        "maps."),
    # Save & Queue in the Render Queue. Degrades to the ONE button: Add Blends
    # and the rest of the queue are disk-only and never needed Blender at all.
    "save_open_blend": (
        "save_blend", "0.34.0",
        "Save & Queue needs Blender add-on 0.34.0 or newer — update the "
        "extension from ⚙ Library Settings, or save in Blender and use Add "
        "Blends."),
    # MadiRef's viewport half. The tab still WORKS without it — ingest, the
    # app's own player and audio are all app-side — so this gates the "show it
    # in Blender" button only.
    # ⚠ TWO DIFFERENT GATES, don't confuse them. This one is about the add-on
    # VERSION and asks "can the installed extension do it?"; since 2026-08-11
    # the tab also sits behind the LICENCE (MainWindow.GATED), which asks "has
    # this person paid?". A licensed user on an old add-on still gets the tab
    # and loses the button — which is what this text is for.
    "madiref_viewport": (
        "madiref_open", "0.35.0",
        "Showing the reference in Blender needs add-on 0.35.0 or newer — "
        "update the extension from ⚙ Library Settings. The clip still plays "
        "here in the app."),
    # The three placements (viewport / pinned / camera). Degrades to the
    # screen-space overlay, which is what it always did, so this greys the
    # placement dropdown only.
    "madiref_placement": (
        "madiref_pin", "0.37.0",
        "Pinning the reference into the scene or to the camera needs add-on "
        "0.37.0 or newer — update the extension from ⚙ Library Settings. It "
        "still follows the viewport meanwhile."),
    # Markers. The whole tool needs the bridge, so this greys the tool rather
    # than one control — there is nothing useful it could show without a
    # Blender to read the markers from.
    "markers": (
        "marker_list", "0.40.0",
        "Timeline markers need Blender add-on 0.40.0 or newer — update the "
        "extension from ⚙ Library Settings to use this tool."),
    # ⚠ Gated on `quad_status`, NOT on `quad_retopologize`: the status command
    # is what tells the tool whether the ENGINE is present, and an add-on new
    # enough to answer it can always say "no engine" for itself. Gating on the
    # run command would leave the tool looking available right up to the button.
    "quadify": (
        "quad_status", "0.44.0",
        "Quadify needs Blender add-on 0.44.0 or newer — update the extension "
        "from ⚙ Library Settings to use this tool."),
}

# Commands introduced after the app's baseline: the only ones a capability
# check can legitimately answer "no" to. Everything else is assumed present,
# because an add-on that can't advertise is OLD, not broken.
GATED_COMMANDS = {cmd: since for cmd, since, _why in FEATURE_REQUIREMENTS.values()}


def version_tuple(text):
    """'0.4.1' -> (0, 4, 1); unparsable parts sort as 0, so comparisons are
    safe on anything an older add-on might report."""
    parts = []
    for chunk in str(text or "").split("."):
        digits = "".join(c for c in chunk if c.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts) or (0,)


def version_note(reported, expected=EXPECTED_ADDON_VERSION, capabilities=None):
    """A short status-bar note about the installed add-on, or None.

    A version DIFFERENCE on its own is no longer treated as breakage — that
    was alarming for something that usually still works fine. What matters is
    whether anything the app needs is actually missing:
      - missing capabilities  -> name the affected features (real problem)
      - older, nothing missing-> quiet informational note
      - newer add-on          -> None; extra commands never hurt the app
    """
    if reported is None:
        return ("add-on predates version reporting — reinstall the extension "
                "(expected %s)" % expected)
    missing = missing_features(capabilities, reported)
    if missing:
        names = ", ".join(sorted(missing))
        return ("add-on %s: %s unavailable — reinstall the extension (app "
                "expects %s)" % (reported, names, expected))
    if str(reported) == str(expected):
        return None
    if version_tuple(reported) > version_tuple(expected):
        return None          # newer bridge, nothing the app asks for is gone
    return "add-on %s (app built for %s) — all features available" % (
        reported, expected)


def supports(capabilities, command, reported_version=None):
    """Does this bridge answer `command`?

    Three cases, in order of how much we know:
      1. it advertised a capability list  -> straight membership test
      2. no list, but it reported a version -> compare against the version the
         command was introduced in (older add-ons still answer honestly)
      3. nothing known at all -> assume YES for everything except gated
         commands, because an add-on that can't advertise is OLD, not broken,
         and refusing to talk to it would be the breakage we're avoiding.
    """
    if capabilities:
        return command in set(capabilities)
    since = GATED_COMMANDS.get(command)
    if since is None:
        return True                      # never gated — always been there
    if reported_version:
        return version_tuple(reported_version) >= version_tuple(since)
    return False                         # gated + nothing to judge by


def missing_features(capabilities, reported_version=None):
    """Which FEATURE_REQUIREMENTS keys this bridge can't support."""
    if capabilities is None and not reported_version:
        return set()          # nothing known yet (offline) — don't cry wolf
    return {feature
            for feature, (cmd, _since, _why) in FEATURE_REQUIREMENTS.items()
            if not supports(capabilities, cmd, reported_version)}


def feature_block_reason(capabilities, feature, reported_version=None):
    """Plain-English reason `feature` is unavailable, or None if it's fine."""
    req = FEATURE_REQUIREMENTS.get(feature)
    if req is None or (capabilities is None and not reported_version):
        return None
    cmd, _since, why = req
    return None if supports(capabilities, cmd, reported_version) else why


class BridgeError(Exception):
    pass


class Bridge:
    """Stateless client: one connection per request keeps failure modes simple."""

    def __init__(self, port=9877, host="127.0.0.1"):
        self.port = port
        self.host = host
        # What the connected add-on says it can do, refreshed by ping/status.
        # None = not asked yet (or Blender unreachable) — deliberately distinct
        # from [] so "unknown" never reads as "supports nothing".
        self.capabilities = None
        self.addon_version = None
        # monotonic deadline until which we KNOW Blender isn't answering.
        # Shared across threads (worker threads + the GUI thread use one
        # Bridge), hence the lock — it's only ever two float reads/writes.
        self._down_until = 0.0
        self._gate = threading.Lock()

    # ---- reachability gate -------------------------------------------
    def _fail_fast_for(self):
        """Seconds left in the current fail-fast window (0 = try normally)."""
        with self._gate:
            return max(0.0, self._down_until - time.monotonic())

    def _mark_down(self):
        with self._gate:
            self._down_until = time.monotonic() + UNREACHABLE_BACKOFF

    def _mark_up(self):
        with self._gate:
            self._down_until = 0.0

    @property
    def reachable(self):
        """False while we're inside a fail-fast window."""
        return self._fail_fast_for() <= 0.0

    def supports(self, command):
        """Whether the connected add-on answers `command`. Callers use this to
        disable a feature politely rather than firing a doomed request."""
        return supports(self.capabilities, command, self.addon_version)

    def feature_reason(self, feature):
        """Why `feature` is unavailable on the connected add-on (None = it's
        available, or we don't know yet)."""
        return feature_block_reason(self.capabilities, feature,
                                    self.addon_version)

    def _remember(self, result):
        """Cache the handshake fields off any ping/status reply."""
        if isinstance(result, dict):
            if "capabilities" in result:
                self.capabilities = result.get("capabilities") or []
            if "version" in result:
                self.addon_version = result.get("version")
        return result

    def request(self, cmd, params=None, timeout=15.0, probe=False, poll=False):
        """Send one command.

        `poll=True` marks a REPEATING background call (the tab pollers). Those
        are the ones that must never wait on a dead port, so they fail
        instantly while we know nothing is listening.

        Anything the USER just triggered leaves `poll` False and always tries:
        it's a single attempt capped at CONNECT_TIMEOUT, and it means clicking
        a button the moment Blender's server comes back simply works instead
        of being told to wait for the next health check.

        `probe=True` is the app's health poll — it always tries (that's its
        job) and its outcome is what opens the gate for everyone else.
        """
        payload = json.dumps({"cmd": cmd, "params": params or {}}) + "\n"
        if poll and not probe:
            wait = self._fail_fast_for()
            if wait > 0:
                raise BridgeError(
                    "Blender not reachable on port %d (rechecking in %.0f s)"
                    % (self.port, wait))
        # Connecting is capped on its own: a live localhost socket answers
        # immediately, so a longer wait here only ever delays a failure. The
        # caller's timeout still governs the REPLY, which is the part that can
        # legitimately take minutes (bake, capture, alembic).
        try:
            sock = socket.create_connection(
                (self.host, self.port), timeout=min(CONNECT_TIMEOUT, timeout))
        except (OSError, socket.timeout) as exc:
            self._mark_down()
            raise BridgeError("Blender not reachable on port %d (%s)"
                              % (self.port, exc))
        self._mark_up()
        try:
            with sock as s:
                s.settimeout(timeout)
                s.sendall(payload.encode("utf-8"))
                buf = b""
                while not buf.endswith(b"\n"):
                    chunk = s.recv(65536)
                    if not chunk:
                        raise BridgeError("connection closed by Blender")
                    buf += chunk
        except BridgeError:
            raise
        except (OSError, socket.timeout) as exc:
            # A failure AFTER connecting means Blender is there but busy or
            # wedged — not "unreachable", so the gate stays open.
            raise BridgeError("Blender stopped responding on port %d (%s)"
                              % (self.port, exc))
        try:
            response = json.loads(buf.decode("utf-8"))
        except ValueError as exc:
            raise BridgeError("bad response: %s" % exc)
        if not response.get("ok"):
            raise BridgeError(self._explain(cmd, response.get(
                "error", "unknown bridge error")))
        return response.get("result")

    @staticmethod
    def _explain(cmd, error):
        """Turn the add-on's raw refusal into something actionable.

        The catch-all safety net for update skew: if a capability check was
        ever missed, the user still gets "reinstall the extension" instead of
        a bare `unknown command: 'x'`."""
        if "unknown command" in str(error).lower():
            return ("this Blender add-on is older than the app and doesn't "
                    "support '%s' — reinstall the extension (app expects %s)"
                    % (cmd, EXPECTED_ADDON_VERSION))
        return error

    # convenience wrappers -------------------------------------------------

    def ping(self, timeout=2.0, probe=False):
        return self._remember(self.request("ping", timeout=timeout, probe=probe))

    def status(self, timeout=3.0, probe=False):
        """`probe=True` is for the app's background health poll — the one
        caller that should keep retrying while the bridge is down."""
        return self._remember(self.request("status", timeout=timeout,
                                           probe=probe))

    def apply_pose(self, path, selected_only=False, blend=1.0, key=False,
                   mirror=False, mirror_table=None, remap_table=None):
        return self.request("apply_pose", {"path": path, "selected_only": selected_only,
                                           "blend": blend, "key": key, "mirror": mirror,
                                           "mirror_table": mirror_table,
                                           "remap_table": remap_table})

    def save_mirror(self, library_root, folder, name, overwrite=False, description=""):
        return self.request("save_mirror", {"library_root": library_root,
                                            "folder": folder, "name": name,
                                            "overwrite": overwrite,
                                            "description": description})

    def apply_set(self, path, extend=False):
        return self.request("apply_set", {"path": path, "extend": extend})

    def save_pose(self, library_root, folder, name, overwrite=False, description=""):
        return self.request("save_pose", {"library_root": library_root, "folder": folder,
                                          "name": name, "overwrite": overwrite,
                                          "description": description})

    def save_set(self, library_root, folder, name, overwrite=False, description=""):
        return self.request("save_set", {"library_root": library_root, "folder": folder,
                                         "name": name, "overwrite": overwrite,
                                         "description": description})

    def save_anim(self, library_root, folder, name, frame_start=None, frame_end=None,
                  overwrite=False, description="", bake=False,
                  keep_modifiers=True, include_props=False):
        """⚠ `keep_modifiers` / `include_props` reach an add-on older than
        0.20.0 as ignored keyword arguments — `save_anim` has existed since the
        first build, so no capability check can see that it grew options. The
        reply's `options` echo is the check; `main.on_save` reports when it is
        missing rather than letting the item disagree with the dialog."""
        return self.request("save_anim", {"library_root": library_root, "folder": folder,
                                          "name": name, "frame_start": frame_start,
                                          "frame_end": frame_end, "overwrite": overwrite,
                                          "description": description, "bake": bake,
                                          "keep_modifiers": keep_modifiers,
                                          "include_props": include_props},
                            timeout=300.0)

    def note_render(self, path, timeout=10.0):
        """Tell Blender about a render the APP produced (the background
        playblast), so its N-panel Watch button finds it too."""
        return self.request("note_render", {"path": path}, timeout=timeout)

    def apply_anim(self, path, mode="replace", start_at="current", selected_only=False,
                   mirror=False, mirror_table=None, remap_table=None, blend=1.0):
        return self.request("apply_anim", {"path": path, "mode": mode,
                                           "start_at": start_at,
                                           "selected_only": selected_only,
                                           "mirror": mirror, "blend": blend,
                                           "mirror_table": mirror_table,
                                           "remap_table": remap_table})

    def list_armatures(self, timeout=10.0):
        return self.request("list_armatures", timeout=timeout)

    def build_remap(self, source_names=None, source_object=None, rules=None,
                    timeout=30.0):
        return self.request("build_remap", {"source_names": source_names,
                                            "source_object": source_object,
                                            "rules": rules}, timeout=timeout)

    def save_remap(self, library_root, folder, name, rules=None, mapping=None,
                   unmatched=None, source="", overwrite=False, description=""):
        return self.request("save_remap",
                            {"library_root": library_root, "folder": folder,
                             "name": name, "rules": rules, "map": mapping,
                             "unmatched": unmatched, "source": source,
                             "overwrite": overwrite,
                             "description": description}, timeout=30.0)

    def list_shape_keys(self, objects=None):
        return self.request("list_shape_keys", {"objects": objects}, timeout=15.0)

    def save_shapes(self, library_root, folder, name, objects=None, keys=None,
                    delete_after=False, overwrite=False, description=""):
        return self.request("save_shapes",
                            {"library_root": library_root, "folder": folder,
                             "name": name, "objects": objects, "keys": keys,
                             "delete_after": delete_after, "overwrite": overwrite,
                             "description": description}, timeout=120.0)

    def list_view_layers(self):
        return self.request("list_view_layers", {}, timeout=10.0)

    def setup_denoise(self, view_layers=None, disable_render_denoise=True,
                      combine="ALPHA_OVER", split="PASSES"):
        return self.request("setup_denoise",
                            {"view_layers": view_layers,
                             "disable_render_denoise": disable_render_denoise,
                             "combine": combine, "split": split}, timeout=60.0)

    def clear_denoise(self, restore_passes=True):
        return self.request("clear_denoise",
                            {"restore_passes": restore_passes}, timeout=60.0)

    def apply_shapes(self, path, mode="replace", force=False, to_active=False,
                     blend=1.0):
        return self.request("apply_shapes", {"path": path, "mode": mode,
                                             "force": force, "blend": blend,
                                             "to_active": to_active}, timeout=120.0)

    def delete_shape_keys(self, object_name, keys):
        return self.request("delete_shape_keys",
                            {"object": object_name, "keys": keys}, timeout=30.0)

    def save_abc(self, library_root, folder, name, frame_start=None,
                 frame_end=None, overwrite=False, description="",
                 options=None):
        """`options` = any subset of the Alembic exporter's settings. An add-on
        too old to know about them ignores the key and exports exactly as it
        always did, so this needs no capability gate."""
        # alembic export samples every frame of heavy meshes — be generous
        return self.request("save_abc",
                            {"library_root": library_root, "folder": folder,
                             "name": name, "frame_start": frame_start,
                             "frame_end": frame_end, "overwrite": overwrite,
                             "description": description,
                             "options": options}, timeout=600.0)

    def apply_abc(self, path):
        return self.request("apply_abc", {"path": path}, timeout=600.0)

    def playblast(self, output, frame_start=None, frame_end=None,
                  use_camera=False, resolution_percent=50, overlays=False):
        return self.request("playblast",
                            {"output": output, "frame_start": frame_start,
                             "frame_end": frame_end, "use_camera": use_camera,
                             "resolution_percent": resolution_percent,
                             "overlays": overlays}, timeout=600.0)

    def snapshot_blend(self, path=None):
        """Save a throwaway copy of the live scene for headless rendering.
        Big rigs take a while to write, hence the long timeout — the app runs
        this on a worker thread behind the busy grey-out."""
        return self.request("snapshot_blend", {"path": path}, timeout=600.0)

    # ------------------------------------------------------- jiggle cage ---
    def cage_status(self):
        """Selected bones, candidate bodies and the shipped presets."""
        return self.request("cage_status", timeout=10.0, poll=True)

    def cage_build(self, body, bones, **opts):
        """Build a cage. Remesh + weight transfer + bind on a dense body is
        genuinely slow, so this gets a long timeout and the caller runs it on
        a worker thread rather than the GUI thread."""
        params = {"body": body, "bones": list(bones)}
        params.update({k: v for k, v in opts.items() if v is not None})
        return self.request("cage_build", params, timeout=600.0)

    def cage_list(self):
        return self.request("cage_list", timeout=10.0, poll=True)

    def cage_remove(self, cage, body=None, label=None):
        """body/label are the caller's record of the cage — they let a cage
        deleted by hand in the viewport still be cleaned off the body."""
        return self.request("cage_remove",
                            {"cage": cage, "body": body, "label": label},
                            timeout=30.0)

    def cage_groups(self, body):
        """Vertex groups on a body, for the 'Vertex group' source mode."""
        return self.request("cage_groups", {"body": body}, timeout=10.0,
                            poll=True)

    def cage_cleanup(self):
        """Strip body deforms whose cage was deleted in the viewport."""
        return self.request("cage_cleanup", timeout=30.0)

    def cage_enable(self, cage, physics=None, hidden=None):
        return self.request("cage_enable",
                            {"cage": cage, "physics": physics,
                             "hidden": hidden}, timeout=15.0)

    # ------------------------------------------------------- bone jiggle ---
    def jiggle_status(self):
        """Armature, selection, scene settings and the stiffness ceiling."""
        return self.request("jiggle_status", timeout=10.0, poll=True)

    def jiggle_get(self, armature=None, bones=None):
        """Per-bone settings plus the values the whole selection agrees on."""
        return self.request("jiggle_get",
                            {"armature": armature, "bones": bones},
                            timeout=15.0, poll=True)

    def jiggle_set(self, settings, armature=None, bones=None):
        """Write settings to the chosen bones. Only the keys present in
        `settings` are written — a mixed selection keeps whatever it had."""
        return self.request("jiggle_set",
                            {"armature": armature, "bones": bones,
                             "settings": settings}, timeout=30.0)

    def jiggle_enable(self, armature=None, bones=None, tip=None, root=None):
        return self.request("jiggle_enable",
                            {"armature": armature, "bones": bones,
                             "tip": tip, "root": root}, timeout=30.0)

    def jiggle_copy(self, armature=None, source=None, bones=None):
        return self.request("jiggle_copy",
                            {"armature": armature, "source": source,
                             "bones": bones}, timeout=30.0)

    def jiggle_list(self, armature=None):
        return self.request("jiggle_list", {"armature": armature},
                            timeout=15.0, poll=True)

    def jiggle_select(self, armature=None):
        return self.request("jiggle_select", {"armature": armature},
                            timeout=15.0)

    def jiggle_object(self, settings, armature=None):
        return self.request("jiggle_object",
                            {"armature": armature, "settings": settings},
                            timeout=15.0)

    def jiggle_scene(self, settings):
        return self.request("jiggle_scene", {"settings": settings},
                            timeout=15.0)

    def jiggle_reset(self):
        return self.request("jiggle_reset", timeout=30.0)

    # ------------------------------------------------------------- assets ---
    def asset_build(self, spec, collection=None):
        """Build a rig from a spec. Long timeout: it creates a 9k-vert mesh and
        a ~100-node group in one main-thread call."""
        return self.request("asset_build",
                            {"spec": spec, "collection": collection},
                            timeout=180.0)

    def asset_status(self, obj, modifier):
        return self.request("asset_status", {"object": obj, "modifier": modifier},
                            timeout=10.0, poll=True)

    # --------------------------------------------------- add-on self-update ---
    def addon_status(self):
        """What add-on version is installed, and is an update already queued."""
        return self.request("addon_status", timeout=10.0, poll=True)

    def addon_update(self, path, version=None, sha256=None):
        """Hand Blender a verified add-on zip to install.

        Returns as soon as it is SCHEDULED, not when it is installed — the
        add-on owns the socket this reply comes back on, so it installs about a
        second later, after the reply is safely gone. The bridge then drops
        while it reloads; the caller re-polls `ping` to find out how it went.

        Carries the bridge token, because installing an extension is installing
        code and the add-on refuses to do it for a caller that cannot read the
        token file (`blender_addon\\…\\bridgeauth.py`). An add-on older than
        0.22.0 ignores the extra field, so this stays backwards compatible.
        """
        return self.request("addon_update",
                            {"path": path, "version": version, "sha256": sha256,
                             "auth": bridge_token()},
                            timeout=60.0)

    def jiggle_bake(self, armature=None, frame_start=None, frame_end=None,
                    preroll=None, selected_only=False, action=None,
                    overwrite=False):
        """Bake to keyframes. Long timeout and a worker thread on the caller's
        side: this steps the whole frame range twice over on a real rig."""
        return self.request(
            "jiggle_bake",
            {"armature": armature, "frame_start": frame_start,
             "frame_end": frame_end, "preroll": preroll,
             "selected_only": selected_only, "action": action,
             "overwrite": overwrite}, timeout=600.0)

    def list_materials(self):
        return self.request("list_materials", {})

    def bake_targets(self, mode, material=None, collection=None):
        """What a bake run would cover, without baking (add-on 0.26.0).
        mode "material" answers "Bake all slots" (the object bake_texture
        would pick + all its slot materials); "selected" / "collection"
        feed the Bulk bake node. A NEW command, not a grown parameter, so
        `supports("bake_targets")` is a real capability check — see
        FEATURE_REQUIREMENTS."""
        return self.request("bake_targets",
                            {"mode": mode, "material": material,
                             "collection": collection}, timeout=30.0)

    def list_collections(self):
        """Scene collections with depth + bakeable-mesh counts, for the Bulk
        bake node's folder picker (add-on 0.26.0)."""
        return self.request("list_collections", {}, timeout=10.0)

    def apply_baked_material(self, items, all_slots=False):
        """Place each baked map into the material it came from, wired to
        that material's active Material Output (add-on 0.27.0 — the Output
        image node's "Replace shader" tickbox).

        `items` is one row per map the run produced: object, material, path,
        bake_type. A NEW command rather than a parameter on bake_texture, so
        `supports("apply_baked_material")` is a real capability check — and
        so the bake itself stays a pure read. ⚠ Sent ONCE, after the whole
        queue: rewiring a material while later maps still have to bake would
        hand those bakes a different shader than the one asked for.

        ⚠ `all_slots` (add-on 0.30.0, the "All slots" tickbox) is a GROWN
        parameter and therefore invisible to `supports()` — an older add-on
        accepts it and silently replaces only the baked materials. The
        reply echoes `all_slots`, and the caller warns when that echo is
        missing (the save_abc rule)."""
        return self.request("apply_baked_material",
                            {"items": list(items),
                             "all_slots": bool(all_slots)},
                            timeout=120.0)

    def bake_texture(self, material, bake_type, width, height, out_path=None,
                     object_name=None, samples=None, margin=16,
                     margin_type="ADJACENT_FACES", use_clear=True,
                     target="IMAGE_TEXTURES", pass_filter=None,
                     view_from="ABOVE_SURFACE", normal_space="TANGENT",
                     normal_swizzle=None, use_selected_to_active=False,
                     use_cage=False, cage_object=None, cage_extrusion=0.0,
                     max_ray_distance=0.0):
        """Bake one texture map — NATIVELY since add-on 0.29.0: the whole
        Bake panel's option set, passed straight through to Blender's own
        operator (Marty tested the panel, saw no seams, and had the
        pipeline rebuilt around it). Long timeout and a worker thread on
        the caller's side: a real scene at a real resolution takes what it
        takes, and the reply echoes every input (the save_abc rule).

        ⚠ Everything from `margin_type` on is GROWN onto a command that
        already existed (0.25.0 first, 0.29.0 again), so
        `supports("bake_texture")` cannot see any of it — an older add-on
        takes the payload, ignores the keys it doesn't know and bakes its
        old way. The reply's `options` block is the proof they landed —
        `options["target"]` specifically means "this add-on bakes
        natively" — and NodeEditorTab says so when it is missing. The
        0.28.x `device`/`denoise`/`view_transform` keys are gone from the
        payload; a 0.29.0 add-on never reads them."""
        return self.request(
            "bake_texture",
            {"material": material, "bake_type": bake_type,
             "width": width, "height": height, "out_path": out_path,
             "object": object_name, "samples": samples, "margin": margin,
             "margin_type": margin_type, "use_clear": use_clear,
             "target": target, "pass_filter": pass_filter,
             "view_from": view_from, "normal_space": normal_space,
             "normal_swizzle": normal_swizzle,
             "use_selected_to_active": use_selected_to_active,
             "use_cage": use_cage, "cage_object": cage_object,
             "cage_extrusion": cage_extrusion,
             "max_ray_distance": max_ray_distance}, timeout=600.0)

    def jiggle_cache(self, armature=None, frame_start=None, frame_end=None,
                     clear=False):
        return self.request(
            "jiggle_cache",
            {"armature": armature, "frame_start": frame_start,
             "frame_end": frame_end, "clear": clear}, timeout=600.0)

    def node_tools_status(self, output_folder="exr_composited",
                          output_suffix="_exr_composited_"):
        return self.request("node_tools_status",
                            {"output_folder": output_folder,
                             "output_suffix": output_suffix}, timeout=10.0)

    def relink_nodes(self, match_mode="NAME", index_fallback=False,
                     copy_inputs=False):
        return self.request("relink_nodes",
                            {"match_mode": match_mode,
                             "index_fallback": index_fallback,
                             "copy_inputs": copy_inputs}, timeout=30.0)

    def setup_image_sequence(self, set_scene_range=True, start_at_one=True,
                             set_output=True, output_folder="exr_composited",
                             output_suffix="_exr_composited_"):
        # Counts frames on disk — renders can live on a network drive.
        return self.request("setup_image_sequence",
                            {"set_scene_range": set_scene_range,
                             "start_at_one": start_at_one,
                             "set_output": set_output,
                             "output_folder": output_folder,
                             "output_suffix": output_suffix}, timeout=120.0)

    def anim_layers_status(self, data_type="OBJECT", object_name=None,
                           poll=False):
        # Polled while the Anim Layers tab is visible — keep the timeout tight
        # so a busy Blender degrades to a "not reachable" row, not a hang.
        # `poll=True` (the timer path) additionally fails instantly while the
        # bridge is known to be down; a user-driven refresh still tries.
        return self.request("anim_layers_status",
                            {"data_type": data_type, "object": object_name},
                            timeout=5.0, poll=poll)

    def anim_layers_add(self, data_type="OBJECT", object_name=None, name=None,
                        blend_type="COMBINE"):
        return self.request("anim_layers_add",
                            {"data_type": data_type, "object": object_name,
                             "name": name, "blend_type": blend_type},
                            timeout=15.0)

    def anim_layers_delete(self, index, data_type="OBJECT", object_name=None):
        return self.request("anim_layers_delete",
                            {"index": index, "data_type": data_type,
                             "object": object_name}, timeout=15.0)

    def anim_layers_duplicate(self, index, linked=False, data_type="OBJECT",
                              object_name=None):
        return self.request("anim_layers_duplicate",
                            {"index": index, "linked": linked,
                             "data_type": data_type, "object": object_name},
                            timeout=30.0)

    def anim_layers_rename(self, index, name, sync_action=True,
                           data_type="OBJECT", object_name=None):
        return self.request("anim_layers_rename",
                            {"index": index, "name": name,
                             "sync_action": sync_action,
                             "data_type": data_type, "object": object_name},
                            timeout=15.0)

    def anim_layers_set_state(self, index, mute=None, lock=None,
                              blend_type=None, influence=None,
                              key_influence=False, data_type="OBJECT",
                              object_name=None):
        return self.request("anim_layers_set_state",
                            {"index": index, "mute": mute, "lock": lock,
                             "blend_type": blend_type,
                             "influence": influence,
                             "key_influence": key_influence,
                             "data_type": data_type,
                             "object": object_name}, timeout=15.0)

    def anim_layers_influence_animated(self, index, animated,
                                       data_type="OBJECT", object_name=None):
        return self.request("anim_layers_influence_animated",
                            {"index": index, "animated": animated,
                             "data_type": data_type,
                             "object": object_name}, timeout=15.0)

    def anim_layers_key_influence(self, index, delete=False,
                                  data_type="OBJECT", object_name=None):
        return self.request("anim_layers_key_influence",
                            {"index": index, "delete": delete,
                             "data_type": data_type,
                             "object": object_name}, timeout=15.0)

    def anim_layers_key_selection(self, delete=False, data_type="OBJECT",
                                  object_name=None):
        """Key (or un-key) at the current frame, the way Blender's own I and
        Alt+I do. The CHANNELS are Blender's decision, not ours — the active
        keying set, or the user's Default Key Channels."""
        return self.request("anim_layers_key_selection",
                            {"delete": delete, "data_type": data_type,
                             "object": object_name}, timeout=30.0)

    def anim_layers_solo(self, index=None, data_type="OBJECT",
                         object_name=None):
        return self.request("anim_layers_solo",
                            {"index": index, "data_type": data_type,
                             "object": object_name}, timeout=15.0)

    def anim_layers_select(self, index, data_type="OBJECT", object_name=None):
        return self.request("anim_layers_select",
                            {"index": index, "data_type": data_type,
                             "object": object_name}, timeout=15.0)

    def anim_layers_set_action(self, index, action, auto_blend=False,
                               sync_name=False, data_type="OBJECT",
                               object_name=None):
        return self.request("anim_layers_set_action",
                            {"index": index, "action": action,
                             "auto_blend": auto_blend,
                             "sync_name": sync_name,
                             "data_type": data_type,
                             "object": object_name}, timeout=15.0)

    def anim_layers_actions(self):
        return self.request("anim_layers_actions", {}, timeout=10.0)

    def anim_layers_sync_names(self, data_type="OBJECT", object_name=None):
        return self.request("anim_layers_sync_names",
                            {"data_type": data_type, "object": object_name},
                            timeout=15.0)

    def anim_layers_move(self, index, direction, data_type="OBJECT",
                         object_name=None):
        return self.request("anim_layers_move",
                            {"index": index, "direction": direction,
                             "data_type": data_type, "object": object_name},
                            timeout=15.0)

    def anim_layers_select_bones(self, index=None, extend=False,
                                 channels=None, axes=None,
                                 data_type="OBJECT", object_name=None):
        return self.request("anim_layers_select_bones",
                            {"index": index, "extend": extend,
                             "channels": channels, "axes": axes,
                             "data_type": data_type, "object": object_name},
                            timeout=20.0)

    def anim_layers_reset(self, index=None, selected_only=True, channels=None,
                          axes=None, data_type="OBJECT", object_name=None):
        return self.request("anim_layers_reset",
                            {"index": index, "selected_only": selected_only,
                             "channels": channels, "axes": axes,
                             "data_type": data_type, "object": object_name},
                            timeout=60.0)

    def anim_layers_cyclic(self, index=None, enable=True, selected_only=True,
                           channels=None, axes=None, data_type="OBJECT",
                           object_name=None):
        return self.request("anim_layers_cyclic",
                            {"index": index, "enable": enable,
                             "selected_only": selected_only,
                             "channels": channels, "axes": axes,
                             "data_type": data_type, "object": object_name},
                            timeout=60.0)

    # ------------------------------------------------------- entitlement ---
    def license_unlock(self, payload, sig):
        """Hand Blender the server-signed licence blob so its paid panels work.

        The EXACT bytes we stored, never a re-serialised copy — the add-on
        verifies the signature over them, and re-encoding the JSON would change
        the bytes and fail the check.
        """
        return self.request("license_unlock", {"payload": payload, "sig": sig},
                            timeout=15.0)

    def license_lock(self):
        return self.request("license_lock", timeout=10.0)

    def anim_layers_set_prefs(self, prefs):
        """Hand Blender the Anim Layers settings the two UIs share.

        There is no matching getter on purpose: the add-on's copy rides along in
        every `anim_layers_status` reply, which is already polled, so a change
        made in Blender reaches the app without anyone asking for it.
        """
        return self.request("anim_layers_set_prefs", {"prefs": prefs},
                            timeout=15.0)

    # ------------------------------------------------------------ bone picker
    # Every mutating call returns the WHOLE status, the same convention the
    # anim-layer commands follow, so the tab repaints from the reply instead of
    # firing a second round trip to find out what happened.

    def picker_status(self, poll=False):
        """PURE READ. `poll=True` for the repeating timer, so a dead bridge
        fails instantly instead of burning the connect timeout on the GUI
        thread (docs\\app-shell.md)."""
        return self.request("picker_status", timeout=10.0, poll=poll)

    def picker_set_tab(self, index):
        return self.request("picker_set_tab", {"index": index}, timeout=15.0)

    def picker_add_tab(self, name=None):
        return self.request("picker_add_tab", {"name": name}, timeout=15.0)

    def picker_remove_tab(self, index=None):
        return self.request("picker_remove_tab", {"index": index}, timeout=15.0)

    def picker_rename_tab(self, name, index=None):
        return self.request("picker_rename_tab", {"name": name, "index": index},
                            timeout=15.0)

    def picker_set_tab_rig(self, object_name, index=None):
        return self.request("picker_set_tab_rig",
                            {"object": object_name, "index": index},
                            timeout=15.0)

    def picker_set_tab_image(self, image_name, index=None):
        return self.request("picker_set_tab_image",
                            {"image": image_name, "index": index}, timeout=15.0)

    def picker_set_button(self, index, **fields):
        """Only the fields actually passed are sent, so a drag can push one
        number instead of the whole button."""
        params = {"index": index}
        params.update(fields)
        return self.request("picker_set_button", params, timeout=15.0)

    def picker_remove_buttons(self, indices):
        return self.request("picker_remove_buttons", {"indices": list(indices)},
                            timeout=15.0)

    def picker_set_brushes(self, **fields):
        return self.request("picker_set_brushes", fields, timeout=15.0)

    def picker_set_prefs(self, prefs):
        return self.request("picker_set_prefs", {"prefs": prefs}, timeout=15.0)

    def save_blend(self):
        """Save the .blend Blender currently has open. -> {path, size, was_dirty}

        ⚠ A generous timeout: Marty's shot files run to hundreds of MB and a
        save is disk-bound, so the default would time out on a save that is
        working perfectly well — and a timed-out save still SAVES, leaving the
        app to report a failure that never happened.
        """
        return self.request("save_blend", timeout=300.0)

    def picker_start(self):
        return self.request("picker_start", timeout=15.0)

    def picker_stop(self):
        return self.request("picker_stop", timeout=15.0)

    def picker_save_item(self, library_root, folder, name, overwrite=False):
        """Save the active picker tab as a `.picker` library item."""
        return self.request("picker_save_item",
                            {"library_root": library_root, "folder": folder,
                             "name": name, "overwrite": overwrite},
                            timeout=60.0)

    def picker_apply_item(self, path, replace=True):
        return self.request("picker_apply_item",
                            {"path": path, "replace": replace}, timeout=30.0)

    # ---------------------------------------------------- scene optimizer ---
    # Every one of these answers with the WHOLE status, results under their own
    # keys (`result`, `mesh_result`, `plan`, `estimate`), so the tab repaints
    # from the reply instead of firing a second round trip.
    #
    # ⚠ THE TIMEOUTS HERE ARE THE LONGEST IN THIS FILE ON PURPOSE. Resizing a
    # scene's worth of 4K textures is minutes of real work, not milliseconds,
    # and an animation-mode pass steps every frame in the range. A generous
    # timeout costs nothing when the command is fast; a tight one turns a
    # working run into a "Blender stopped responding" halfway through.

    def list_vertex_groups(self, objects=None):
        return self.request("list_vertex_groups", {"objects": objects})

    def save_vgroups(self, library_root, folder, name, objects=None,
                     groups=None, description="", overwrite=False):
        return self.request("save_vgroups", {
            "library_root": library_root, "folder": folder, "name": name,
            "objects": objects, "groups": groups, "description": description,
            "overwrite": overwrite}, timeout=300.0)

    def apply_vgroups(self, path, mode="EXACT", to_active=False, replace=True,
                      source_object=None):
        """⚠ `mode` is the whole safety story: EXACT is an index-based restore
        and refuses on a vertex-count mismatch; TRANSFER is a spatial estimate.
        They are separate actions in the UI for that reason."""
        return self.request("apply_vgroups", {
            "path": path, "mode": mode, "to_active": to_active,
            "replace": replace, "source_object": source_object},
            timeout=300.0)

    def opt_status(self, poll=False):
        """PURE READ. `poll=True` for the repeating timer, so a dead bridge
        fails instantly instead of burning the connect timeout on the GUI
        thread (`docs\\app-shell.md`)."""
        return self.request("opt_status", timeout=10.0, poll=poll)

    def opt_progress(self, poll=False):
        """How far the run in flight has got.

        ⚠ ANSWERED WHILE BLENDER IS BUSY, which no other command here is. The
        add-on serves it on the socket thread instead of queueing it for the
        main thread, so it comes back immediately even mid-resize — that is the
        only reason a progress bar can move at all. Hence the short timeout: if
        this one blocks, something is wrong, and waiting is pointless.
        """
        return self.request("opt_progress", timeout=5.0, poll=poll)

    def quad_status(self, poll=False, deep=False):
        """PURE READ: is the engine there, and what is selected.

        ⚠ `deep=True` evaluates the mesh to report the triangle count the
        engine will REALLY get. Use it when the tool is shown, never on a
        timer — and never show the shallow count as if it were the job size."""
        return self.request("quad_status", {"deep": bool(deep)},
                            timeout=30.0, poll=poll)

    def quad_progress(self, poll=False):
        """Which stage the retopo is on. Answered while Blender is busy, the
        same way `opt_progress` is and for the same reason — see there."""
        return self.request("quad_progress", timeout=5.0, poll=poll)

    def quad_select(self, name):
        """Select the retopologised object in Blender and make it active."""
        return self.request("quad_select", {"object": name}, timeout=10.0)

    def quad_retopologize(self, params):
        """START a retopology. ⚠ **RETURNS IMMEDIATELY** — it hands back as
        soon as the mesh is on disk, then the add-on works on its own thread
        and the caller polls `quad_progress`.

        ⚠ The short timeout is deliberate and is the fix for a real failure: a
        blocking version with a 30-minute timeout gave up on a 52-minute job
        while Blender carried on working invisibly behind it. Nothing here may
        go back to waiting for the run."""
        return self.request("quad_retopologize", params, timeout=60.0)

    def quad_cancel(self):
        """Stop the run in flight. Answered off the main thread, like
        `quad_progress` — a cancel that queues behind the work it is
        cancelling is not a cancel."""
        return self.request("quad_cancel", timeout=10.0)

    def quad_result(self, poll=False):
        """The finished run's report, once `quad_progress` goes inactive."""
        return self.request("quad_result", timeout=15.0, poll=poll)

    def opt_plan(self, params):
        """What a run WOULD do. Reads every texture header, so not instant."""
        return self.request("opt_plan", params, timeout=300.0)

    def opt_resize(self, params):
        return self.request("opt_resize", params, timeout=1800.0)

    def opt_group_apply(self, params):
        """Switch to a named texture set. Regenerates anything whose cached
        file has gone, so it is a resize in the worst case — same timeout."""
        return self.request("opt_group_apply", params, timeout=1800.0)

    def opt_group_rename(self, params):
        return self.request("opt_group_rename", params, timeout=30.0)

    def opt_group_delete(self, params):
        return self.request("opt_group_delete", params, timeout=30.0)

    def opt_adaptive(self, params):
        return self.request("opt_adaptive", params, timeout=1800.0)

    def opt_decimate(self, params):
        return self.request("opt_decimate", params, timeout=600.0)

    def opt_revert_images(self, params):
        return self.request("opt_revert_images", params, timeout=300.0)

    def opt_revert_meshes(self, params):
        return self.request("opt_revert_meshes", params, timeout=120.0)

    def opt_regenerate(self, params):
        return self.request("opt_regenerate", params, timeout=1800.0)

    def opt_clear_cache(self, params):
        """Restore every texture, then delete the stand-ins. The restore is why
        this is not a quick call — it reloads every managed image."""
        return self.request("opt_clear_cache", params, timeout=900.0)

    def opt_estimate(self, params=None):
        return self.request("opt_estimate", params or {}, timeout=300.0)

    def opt_preview_start(self, params):
        return self.request("opt_preview_start", params, timeout=300.0)

    def opt_preview_stop(self):
        return self.request("opt_preview_stop", {}, timeout=15.0)

    def anim_layers_extract_bones(self, index=None, name=None,
                                  selected_only=True, channels=None,
                                  axes=None, data_type="OBJECT",
                                  object_name=None):
        return self.request("anim_layers_extract_bones",
                            {"index": index, "name": name,
                             "selected_only": selected_only,
                             "channels": channels, "axes": axes,
                             "data_type": data_type, "object": object_name},
                            timeout=60.0)

    def anim_layers_share_keys(self, source_index, index=None,
                               selected_only=True, channels=None, axes=None,
                               data_type="OBJECT", object_name=None):
        return self.request("anim_layers_share_keys",
                            {"source_index": source_index, "index": index,
                             "selected_only": selected_only,
                             "channels": channels, "axes": axes,
                             "data_type": data_type, "object": object_name},
                            timeout=60.0)

    def anim_layers_extract_markers(self, index=None, name=None,
                                    selected_only=True, channels=None,
                                    axes=None, mute_source=True,
                                    data_type="OBJECT", object_name=None):
        return self.request("anim_layers_extract_markers",
                            {"index": index, "name": name,
                             "selected_only": selected_only,
                             "channels": channels, "axes": axes,
                             "mute_source": mute_source,
                             "data_type": data_type, "object": object_name},
                            timeout=300.0)

    def anim_layers_influence_keys(self, index=None, scope="LOCAL",
                                   select=None, hide=None, mute=None,
                                   lock=None, data_type="OBJECT",
                                   object_name=None):
        return self.request("anim_layers_influence_keys",
                            {"index": index, "scope": scope,
                             "select": select, "hide": hide, "mute": mute,
                             "lock": lock, "data_type": data_type,
                             "object": object_name}, timeout=20.0)

    def anim_layers_adopt_nla(self, data_type="OBJECT", object_name=None):
        return self.request("anim_layers_adopt_nla",
                            {"data_type": data_type, "object": object_name},
                            timeout=30.0)

    def anim_layers_clear_nla(self, confirm=False, data_type="OBJECT",
                              object_name=None):
        return self.request("anim_layers_clear_nla",
                            {"confirm": confirm, "data_type": data_type,
                             "object": object_name}, timeout=30.0)

    def anim_layers_frame_range(self, index=None, custom=None,
                                frame_start=None, frame_end=None,
                                extrapolation=None, reverse=None, repeat=None,
                                scale=None, sync=False, always_sync=None,
                                data_type="OBJECT", object_name=None):
        return self.request("anim_layers_frame_range",
                            {"index": index, "custom": custom,
                             "frame_start": frame_start,
                             "frame_end": frame_end,
                             "extrapolation": extrapolation,
                             "reverse": reverse, "repeat": repeat,
                             "scale": scale, "sync": sync,
                             "always_sync": always_sync,
                             "data_type": data_type, "object": object_name},
                            timeout=15.0)

    def anim_layers_multikey(self, op="OFFSET", value=0.0, index=None,
                             selected_only=True, selected_keys=True,
                             channels=None, axes=None, pivot="AVERAGE",
                             seed=0, data_type="OBJECT", object_name=None):
        return self.request("anim_layers_multikey",
                            {"op": op, "value": value, "index": index,
                             "selected_only": selected_only,
                             "selected_keys": selected_keys,
                             "channels": channels, "axes": axes,
                             "pivot": pivot, "seed": seed,
                             "data_type": data_type, "object": object_name},
                            timeout=60.0)

    def anim_layers_bake(self, mode="NEW", direction="ALL", index=None,
                         bake_type="AL", smart=False, steps=1,
                         selected_only=False, merge_modifiers=True,
                         clear_constraints=False, copy_original=False,
                         data_type="OBJECT", object_name=None):
        # a long timeline on a heavy rig samples every frame — generous timeout
        return self.request("anim_layers_bake",
                            {"mode": mode, "direction": direction,
                             "index": index, "bake_type": bake_type,
                             "smart": smart, "steps": steps,
                             "selected_only": selected_only,
                             "merge_modifiers": merge_modifiers,
                             "clear_constraints": clear_constraints,
                             "copy_original": copy_original,
                             "data_type": data_type, "object": object_name},
                            timeout=300.0)

    def capture_preview(self, path, width=256, height=256, frames=None,
                        shape_steps=None, timeout=300.0):
        return self.request("capture_preview",
                            {"path": path, "width": width, "height": height,
                             "frames": list(frames) if frames else None,
                             "shape_steps": shape_steps},
                            timeout=timeout)

    def capture_vgroup_preview(self, path, width=256, height=256,
                               max_groups=24, timeout=300.0):
        """Weight-paint stills of a .vgroups item's groups. A separate command
        from `capture_preview` on purpose — see FEATURE_REQUIREMENTS."""
        return self.request("capture_vgroup_preview",
                            {"path": path, "width": width, "height": height,
                             "max_groups": max_groups},
                            timeout=timeout)

    # ------------------------------------------------------ render presets

    def render_preset_schema(self):
        """The catalogue the add-on can read — group keys, labels and counts."""
        return self.request("render_preset_schema")

    def render_preset_capture(self, groups=None):
        """Read the scene's render settings; `groups` = catalogue keys."""
        return self.request("render_preset_capture",
                            {"groups": list(groups) if groups else None})

    def render_preset_apply(self, data, groups=None):
        """Write a saved preset back onto the scene."""
        return self.request("render_preset_apply",
                            {"data": data,
                             "groups": list(groups) if groups else None})

    # ----------------------------------------------------- timeline markers

    def marker_list(self, poll=False):
        """Every marker with its note and tags, plus a `revision`.

        ⚠ Polled while the Markers tool is visible, so it keeps the same tight
        timeout and `poll=True` fast-fail as the Anim Layers status: a busy
        Blender must degrade to a stale list, never to a frozen tab.
        """
        return self.request("marker_list", timeout=5.0, poll=poll)

    def marker_add(self, name="Marker", frame=None, note="", tags=None,
                   layer=""):
        return self.request("marker_add",
                            {"name": name, "frame": frame, "note": note,
                             "tags": list(tags) if tags else None,
                             "layer": layer})

    def marker_set(self, ref, **fields):
        """Edit ONE field of one marker.

        ⚠ Only what the caller passes is sent, and the add-on only writes what
        it receives. Sending the whole marker on every keystroke is how an app
        that is one poll behind overwrites a note typed in Blender.

        ⚠ `camera` is absent-vs-None: leaving it out keeps the binding, passing
        None clears it. Do not "helpfully" default it.
        """
        payload = {"ref": dict(ref)}
        payload.update(fields)
        return self.request("marker_set", payload)

    def marker_remove(self, ref):
        return self.request("marker_remove", {"ref": dict(ref)})

    def marker_goto(self, ref):
        return self.request("marker_goto", {"ref": dict(ref)})

    def marker_show_layer(self, layer=""):
        """Show one layer's markers and park the rest; "" shows them all.

        ⚠ THIS WRITES. It removes the other layers' markers from the scene so
        Blender's timeline strip clears — the only way, since Blender always
        draws every marker it has. They are stored on the scene and restored
        intact, cameras included (docs\\markers.md).
        """
        return self.request("marker_show_layer", {"layer": layer}, timeout=30.0)

    def marker_set_save(self, name):
        """Save every marker — parked ones too — under a name, in the .blend."""
        return self.request("marker_set_save", {"name": name}, timeout=30.0)

    def marker_set_load(self, name):
        """⚠ REPLACES every marker in the scene with the named set."""
        return self.request("marker_set_load", {"name": name}, timeout=30.0)

    def marker_set_delete(self, name):
        return self.request("marker_set_delete", {"name": name})

    def marker_bind_by_name(self, exact=True):
        return self.request("marker_bind_by_name", {"exact": exact})

    def marker_rename(self, find="", replace="", prefix="", suffix="",
                      only=None):
        return self.request("marker_rename",
                            {"find": find, "replace": replace,
                             "prefix": prefix, "suffix": suffix,
                             "only": list(only) if only else None})


class BlendStreamer:
    """Streams slider values to Blender on a worker thread (send-latest-only:
    a fast drag never queues up stale values)."""

    def __init__(self, bridge):
        self.bridge = bridge
        self._thread = None
        self._evt = threading.Event()
        self._active = False
        self._value = 1.0

    @property
    def active(self):
        return self._active

    def start(self, path, selected_only=False):
        r = self.bridge.request("begin_blend",
                                {"path": path, "selected_only": selected_only},
                                timeout=10.0)
        self._active = True
        self._evt.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return r

    def set_value(self, value):
        self._value = value
        self._evt.set()

    def stop(self, keep=True, key=False):
        if not self._active:
            return None
        self._active = False
        self._evt.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None
        return self.bridge.request("end_blend", {"keep": keep, "key": key},
                                   timeout=10.0)

    def _loop(self):
        last = None
        while self._active:
            self._evt.wait(0.5)
            if not self._active:
                break
            self._evt.clear()
            v = self._value
            if v == last:
                continue
            try:
                self.bridge.request("set_blend", {"blend": v}, timeout=5.0)
                last = v
            except BridgeError:
                break  # Blender went away; end_blend in stop() will report it
