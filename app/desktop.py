"""The OS-integration surface: open a file, show it in the file manager.

One module so the port has ONE auditable place where the app asks the desktop
to do something, instead of four `os.startfile` calls scattered through
`main.py`. ⚠ `os.startfile` does not EXIST off Windows — it raises
`AttributeError`, which is not an `OSError`, so every caller that carefully
caught `OSError` would have caught nothing at all.

⚠ **The command is chosen by a pure function, and executed by another.**
`open_command` / `reveal_command` return argv and touch nothing, so the suite
can pin what each platform would run without a Mac, a Linux box, or a spawned
process. Executing is the easy half; picking correctly is the half that breaks.

⚠ **These RAISE `OSError` on failure, deliberately.** Its vendored twin in
`render_deck\\util.py` swallows everything — right for a queue that must not
die mid-render, wrong here, where every call site already reports the failure
to the user. The two are not shared on purpose: `render_deck\\` is a vendored
copy of the standalone render manager and the copies have been allowed to
diverge since 2026-07-31.
"""

import os
import subprocess
import sys

#: Windows opens through a shell API rather than a command line, so there is
#: no argv to return. Callers must treat None as "use os.startfile".
SHELL_OPEN = None


def _windows(platform=None):
    return (platform or sys.platform).startswith("win")


def _macos(platform=None):
    return (platform or sys.platform) == "darwin"


def open_command(path, platform=None):
    """argv that opens `path` with the user's default application.

    Returns `SHELL_OPEN` (None) on Windows, where it is not a command.
    `platform` is for the suite; live callers leave it alone.
    """
    if _windows(platform):
        return SHELL_OPEN
    if _macos(platform):
        return ["open", path]
    return ["xdg-open", path]


def reveal_command(path, platform=None):
    """argv that shows `path` in the file manager.

    ⚠ Only Windows and macOS can HIGHLIGHT the file itself. Linux has no
    portable way to do that — the freedesktop D-Bus `ShowItems` call works on
    the big file managers but not reliably enough to depend on — so there we
    open the containing folder and say so rather than doing nothing.
    """
    if _windows(platform):
        # ⚠ The comma is part of the flag, and the path must be a separate
        # argument: `explorer "/select," "C:\\x\\y.blend"`.
        return ["explorer", "/select,", os.path.normpath(path)]
    if _macos(platform):
        return ["open", "-R", path]
    return ["xdg-open", os.path.dirname(os.path.abspath(path))]


def open_path(path):
    """Open `path` in the default application. Raises OSError if it fails."""
    argv = open_command(path)
    if argv is SHELL_OPEN:
        os.startfile(path)          # noqa: S606 - Windows only, see above
        return
    subprocess.Popen(argv)


def reveal_in_folder(path):
    """Show `path` in the file manager. Raises OSError if it fails."""
    subprocess.Popen(reveal_command(path))


def can_highlight_file(platform=None):
    """True where `reveal_in_folder` selects the file rather than just opening
    its folder — so a caller can word its own message honestly."""
    return _windows(platform) or _macos(platform)
