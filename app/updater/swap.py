"""Putting a downloaded update in place, on Windows, while the app is running.

THE CONSTRAINT EVERYTHING HERE IS SHAPED BY: a running .exe cannot be deleted
or overwritten on Windows - but it CAN be renamed. So the swap is

    rename the old file out of the way  ->  write the new one at the same path

and the app keeps running from the renamed file until it is restarted. The path
never changes, which matters because the Blender add-on launches the app by an
absolute path stored in its preferences (blender_addon\\__init__.py) - an
updater that moved the folder would quietly break that button.

Nothing here is committed until it is proven: after the files are in place the
NEW build is run with --smoke, and if it does not exit 0 every file is put back.
An update channel that can leave a paying customer with an app that will not
start is worse than having no update channel.

WHAT IS NEVER TOUCHED: only files the manifest lists. config.json and
render_queue\\ live next to the exe and hold the user's own settings and local
paths; they are not in a release, so they cannot be overwritten by one.
"""

import hashlib
import os
import shutil
import subprocess
import sys

# Left beside the file it replaced, and deleted at the next startup - by which
# time the process that was holding it has gone.
OLD_SUFFIX = ".madi_old"
STAGE_DIR = ".madi_update"
READ_CHUNK = 1024 * 1024


def app_root():
    """The folder to update: the one the exe lives in.

    Only meaningful when frozen. From source there is nothing to swap - those
    files are the developer's working copy, and replacing them would be
    vandalism, not an update.
    """
    if not getattr(sys, "frozen", False):
        return None
    return os.path.dirname(os.path.abspath(sys.executable))


def exe_path():
    if not getattr(sys, "frozen", False):
        return None
    return os.path.abspath(sys.executable)


def file_hash(path):
    """sha256 of a file, or None if it cannot be read."""
    try:
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            while True:
                chunk = handle.read(READ_CHUNK)
                if not chunk:
                    break
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def plan(root, offer):
    """Which of the offer's files this install actually needs.

    A file already present with the right hash is skipped - that is the whole
    delta: a release lists all ~273 files, and normally exactly one of them
    differs.
    """
    needed = []
    for item in offer.files:
        target = os.path.join(root, item["path"].replace("/", os.sep))
        if file_hash(target) != item["sha256"]:
            needed.append(item)
    return needed


def stage_dir(root):
    return os.path.join(root, STAGE_DIR)


def clear_stage(root):
    """Remove the staging area. Safe to call at any time."""
    path = stage_dir(root)
    if not os.path.isdir(path):
        return
    for base, dirs, files in os.walk(path, topdown=False):
        for name in files:
            try:
                os.remove(os.path.join(base, name))
            except OSError:
                pass
        for name in dirs:
            try:
                os.rmdir(os.path.join(base, name))
            except OSError:
                pass
    try:
        os.rmdir(path)
    except OSError:
        pass


def staged_path(root, item):
    """Where a downloaded file waits. Named by HASH, not by its destination.

    Two files in a release can be byte-identical (PyInstaller folders have a
    few), and naming the staging copy after the content means it is downloaded
    once and used twice, for free.
    """
    return os.path.join(stage_dir(root), item["sha256"])


def write_staged(root, item, data):
    """Store a downloaded file, refusing it if the bytes are not what was
    promised. This is the second half of the signature check: the manifest is
    signed, so a hash from it is trustworthy, and anything that does not match
    one never reaches the app folder."""
    if hashlib.sha256(data).hexdigest() != item["sha256"]:
        return False
    os.makedirs(stage_dir(root), exist_ok=True)
    path = staged_path(root, item)
    tmp = path + ".part"
    with open(tmp, "wb") as handle:
        handle.write(data)
    os.replace(tmp, path)
    return True


def verify_staged(root, items):
    """Every file is present in staging and hashes correctly."""
    for item in items:
        if file_hash(staged_path(root, item)) != item["sha256"]:
            return False
    return True


def apply(root, items):
    """Move staged files into place. Returns (ok, error).

    All or nothing: the first failure puts everything back. A half-applied
    update is a mixed set of files from two builds, which is the one outcome
    worse than not updating.

    ⚠⚠ **A STAGED BLOB CAN SERVE MORE THAN ONE DESTINATION, SO IT CANNOT
    ALWAYS BE MOVED.** `staged_path` names the staging copy after its HASH
    precisely so byte-identical files are downloaded once, and the download
    loop in `manager.install` dedupes on exactly that. Moving the blob into
    place CONSUMES it, so the second destination wanting those bytes found
    nothing and the whole install failed with `[WinError 2]` — then rolled
    itself back and retried, forever.

    **This fired on every single release**, because `CHANGELOG.md` ships at
    two paths (`CHANGELOG.md` and `_internal\\CHANGELOG.md`) and changes with
    every build, so it is always in the delta; four Qt `.qm` translations are
    identical too. Reported 2026-08-14 from a user's log on 1.9.0, and the sha
    in it is 1.16.0's CHANGELOG.

    So: copy while another destination still needs the bytes, and move only on
    the LAST use. The two halves of the channel have to agree about dedup —
    the downloader knowing and the installer not knowing is what broke it.
    """
    moved = []   # (target, renamed_old) - to put back
    placed = []  # targets written - to remove on the way back
    remaining = {}
    for item in items:
        remaining[item["sha256"]] = remaining.get(item["sha256"], 0) + 1
    try:
        for item in items:
            target = os.path.join(root, item["path"].replace("/", os.sep))
            os.makedirs(os.path.dirname(target), exist_ok=True)
            if os.path.exists(target):
                old = target + OLD_SUFFIX
                if os.path.exists(old):
                    # A leftover from a previous update whose cleanup did not
                    # run. It is not in use now, so it goes.
                    os.remove(old)
                # The one Windows rule that makes this possible at all: this
                # succeeds even when `target` is the RUNNING executable.
                os.rename(target, old)
                moved.append((target, old))
            source = staged_path(root, item)
            remaining[item["sha256"]] -= 1
            if remaining[item["sha256"]]:
                shutil.copyfile(source, target)
            else:
                os.replace(source, target)
            placed.append(target)
        return True, None
    except Exception as err:
        undo(placed, moved)
        return False, str(err)


def undo(placed, moved):
    """Put back what apply() moved. Reverse order, and never raises."""
    for target in reversed(placed):
        try:
            if os.path.exists(target):
                os.remove(target)
        except OSError:
            pass
    for target, old in reversed(moved):
        try:
            if os.path.exists(old):
                os.replace(old, target)
        except OSError:
            pass


def rollback(root, items):
    """Undo an applied update: every .madi_old goes back to its own name."""
    restored = 0
    for item in items:
        target = os.path.join(root, item["path"].replace("/", os.sep))
        old = target + OLD_SUFFIX
        if not os.path.exists(old):
            continue
        try:
            if os.path.exists(target):
                os.remove(target)
            os.replace(old, target)
            restored += 1
        except OSError:
            pass
    return restored


def smoke(exe, timeout=120):
    """Does the newly installed build actually start? Returns (ok, detail).

    `main.py --smoke` builds the whole window offscreen and exits 0. The
    windowed build prints nothing, so THE EXIT CODE IS THE ANSWER - which is
    also why this cannot be replaced by looking at the output.
    """
    try:
        done = subprocess.run(
            [exe, "--smoke"],
            timeout=timeout,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired:
        return False, "the new build did not finish starting"
    except Exception as err:
        return False, str(err)
    if done.returncode != 0:
        return False, "the new build exited with code %d" % done.returncode
    return True, None


def cleanup(root):
    """Delete what a finished update left behind. Called once at startup.

    By now the process that was holding the old exe has exited, so the files
    that could not be deleted during the swap can be.
    """
    if not root or not os.path.isdir(root):
        return 0
    removed = 0
    for base, _dirs, files in os.walk(root):
        if os.path.basename(base) == STAGE_DIR:
            continue
        for name in files:
            if name.endswith(OLD_SUFFIX):
                try:
                    os.remove(os.path.join(base, name))
                    removed += 1
                except OSError:
                    # Still locked, somehow. It costs disk, not correctness,
                    # and the next startup will try again.
                    pass
    clear_stage(root)
    return removed
