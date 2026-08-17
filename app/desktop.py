"""The OS-integration surface: open a file, show it in the file manager.

**Windows only.** The Linux/macOS port was cancelled on 2026-08-17 (Marty:
*"we cancel ALL porting of linux and mac and ONLY focus on windows"*), so the
per-platform command tables that used to live here are gone.

⚠ **The module is KEPT rather than folded back into `main.py`.** It is the one
auditable place where the app asks the desktop to do something, instead of five
`os.startfile` / `explorer` calls scattered through a 5,000-line file, and the
suite can pin what would be run without spawning a process.

⚠ **These RAISE `OSError` on failure, deliberately.** Its vendored twin in
`render_deck\\util.py` swallows everything — right for a queue that must not
die mid-render, wrong here, where every call site already reports the failure
to the user. The two are not shared on purpose: `render_deck\\` is a vendored
copy of the standalone render manager and the copies have been allowed to
diverge since 2026-07-31.
"""

import os
import subprocess

#: Windows opens through a shell API rather than a command line, so there is
#: no argv to return. Callers must treat None as "use os.startfile".
SHELL_OPEN = None


def open_command(path):
    """argv that opens `path` with the user's default application.

    Always `SHELL_OPEN` (None) on Windows, where opening is a shell API call
    and not a command line. Kept as a function so `open_path` has one shape.
    """
    return SHELL_OPEN


def reveal_command(path):
    """argv that shows `path` in the file manager, with the file selected."""
    # ⚠ The comma is part of the flag, and the path must be a separate
    # argument: `explorer "/select," "C:\\x\\y.blend"`.
    return ["explorer", "/select,", os.path.normpath(path)]


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


def can_highlight_file():
    """True where `reveal_in_folder` selects the file rather than just opening
    its folder — always so on Windows. Kept because call sites word their own
    message from it."""
    return True
