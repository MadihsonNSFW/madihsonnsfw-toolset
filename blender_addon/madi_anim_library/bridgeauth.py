"""A shared secret for the bridge's dangerous commands.

WHY THIS EXISTS. The bridge listens on 127.0.0.1:9877 and, until 2026-08-06,
authenticated nothing at all: any process that could open a socket could drive
all 128 commands. That was a deliberate, documented trade - a local port for a
local app - and it was survivable while "local process" meant "something already
running as the user, which can do worse anyway".

It stopped being survivable when it turned out A WEB PAGE COUNTS AS A LOCAL
PROCESS. See `server._client_loop` for the cross-protocol hole that made an
ordinary `fetch()` able to reach this port; that hole is closed there, and this
file is the second lock on the one command whose abuse is unrecoverable:
`addon_update` installs a Blender extension, which is arbitrary code.

WHAT A TOKEN DOES AND DOES NOT BUY:
  * it does NOT stop malware already running as Marty - that can read the file
    like anyone else, and could install an extension by writing to Blender's
    directory without going near this port;
  * it DOES stop anything that can reach a socket but not the filesystem, which
    is exactly the browser case, and any sandboxed process.
So this is defence in depth for a specific reachable attacker, not a claim that
the bridge is now an authenticated service.

The token is written ONLY by the instance that actually won the port (see
`server.start`). With two Blenders open, the loser must not overwrite the
winner's token, or the app would hold a secret for a bridge nobody is listening
on - which is the "two instances" confusion this project has already been bitten
by twice.
"""

import os
import secrets
import stat

# The same per-user folder the app keeps its licence in
# (app\licensing\store.APP_FOLDER). Chosen over Blender's own config directory
# because the app has no idea which Blender version - or which of several
# installs - is holding the bridge, and a path it has to guess is a path that
# breaks silently.
APP_FOLDER = "MadihsonNSFW Toolset"
FILE_NAME = "bridge.token"

_TOKEN = None


def token_dir():
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, APP_FOLDER)


def token_path():
    return os.path.join(token_dir(), FILE_NAME)


def current():
    """The token this session is using, or None if it was never written."""
    return _TOKEN


def issue():
    """Mint a token for this session and write it where the app can read it.

    Called from `server.start()` AFTER the bind succeeds. Returns the token, or
    None if it could not be written - in which case the guarded commands refuse,
    which is the safe direction: a bridge that cannot prove who it is talking to
    should not be installing code.
    """
    global _TOKEN
    value = secrets.token_urlsafe(32)
    path = token_path()
    try:
        os.makedirs(token_dir(), exist_ok=True)
        # Written fresh every start, so a token cannot outlive the process that
        # issued it and be replayed at the next one.
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(value)
        try:
            # Owner-only where the OS means it. On Windows this is close to
            # cosmetic (the folder ACL is what matters), but it costs one line
            # and the add-on is not Windows-only by construction.
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
    except OSError as exc:
        print("[MadihsonNSFW] could not write the bridge token (%s) - "
              "add-on updates from the app will be refused" % exc)
        _TOKEN = None
        return None
    _TOKEN = value
    return value


def clear(mine=None):
    """Forget a token, and remove the file ONLY if that token is the live one.

    `mine` is the value the CALLER got back from `issue()`. Pass it. A caller
    that never issued one (or issued an older one) does nothing at all here.

    ⚠ IT USED TO DELETE UNCONDITIONALLY, AND THAT BROKE THE NEXT UPDATE.
    `selfupdate.reload_addon` stops every BridgeServer it can find through the
    garbage collector, because earlier reloads leave instances stranded (three
    were live on the first real 0.22.0 reload; six have been seen). Each `stop()`
    calls in here. So the order that actually happens is:

        new add-on starts  -> writes token A
        stranded server stops -> clear() -> DELETED token A

    ...leaving the running bridge holding a token in memory that the app could
    no longer read, and `addon_update` refusing the app by its own rule. Caught
    live: bridge up on 0.22.0, `_TOKEN` set, file gone, and no way to push the
    next version over the bridge at all.

    ⚠ COMPARING THE FILE WAS NOT ENOUGH, and 0.25.0 is the second fix. `_TOKEN`
    is a MODULE global, so every BridgeServer inside one module instance shares
    it — a stranded server stopping reads the LIVE server's token out of the
    global, matches it against the file, and deletes the file it does not own.
    Caught live again on 0.24.3: bridge listening, `_TOKEN` 43 chars in memory,
    `bridge.token` gone from disk, `addon_update` refusing the real app with
    "must come from the Toolset app". Identity has to travel with the INSTANCE,
    which is what `mine` is for; the file comparison stays as the second lock
    for a genuinely stale module.
    """
    global _TOKEN
    if mine is None:
        mine = _TOKEN               # legacy caller: old behaviour, one owner
    if not mine or not _TOKEN or not secrets.compare_digest(str(mine), _TOKEN):
        return                      # not ours — do not touch memory OR disk
    _TOKEN = None
    try:
        with open(token_path(), "r", encoding="utf-8") as handle:
            on_disk = handle.read().strip()
    except OSError:
        return                      # already gone
    if not secrets.compare_digest(on_disk, mine):
        return                      # a newer session owns it - leave it alone
    try:
        os.remove(token_path())
    except OSError:
        pass


def check(supplied):
    """Constant-time compare. False whenever there is nothing to compare to."""
    if not _TOKEN or not isinstance(supplied, str) or not supplied:
        return False
    return secrets.compare_digest(supplied, _TOKEN)
