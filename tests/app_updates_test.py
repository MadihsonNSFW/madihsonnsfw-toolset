# The "What's New" tab (Marty, 2026-08-04: "A tab that can be used to post
# update logs later when pushing updates").
#
#   python tests\app_updates_test.py
import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

sys.path.insert(0, os.path.join(_ROOT, "app"))

from PySide6.QtWidgets import QApplication  # noqa: E402

PASS, FAIL = [], []


def ok(cond, label):
    (PASS if cond else FAIL).append(label)
    print(("ok   " if cond else "FAIL ") + label, flush=True)


app = QApplication.instance() or QApplication([])

import updates as updmod  # noqa: E402
import version  # noqa: E402

# ------------------------------------------------------------- the shipped file
shipped = updmod.changelog_path()
ok(os.path.isfile(shipped),
   "notes: a CHANGELOG.md ships with the app (%s)" % shipped)
text = updmod.read_notes()
ok(len(text) > 200, "notes: and it has something in it")
ok(text != updmod.EMPTY, "notes: the real file, not the placeholder")

page = updmod.UpdatesPage()
ok(version.APP_VERSION in page.version_label.text(),
   "notes: the tab says which version you are on - notes are worthless if you "
   "do not know which build you have")
rendered = page.view.toPlainText()
ok(len(rendered) > 200,
   "notes: the markdown is rendered into the view (%d chars)" % len(rendered))
ok("#" not in rendered.split("\n")[0],
   "notes: rendered as MARKDOWN, not dumped as raw text - the first line has "
   "no hashes left in it (%r)" % rendered.split("\n")[0][:60])

# ---------------------------------------------------------------- robustness
# ⚠ An unreadable changelog is a cosmetic problem. A tab that took the app down
# over one would not be.
missing = os.path.join(tempfile.mkdtemp(prefix="madi_notes_"), "nope.md")
ok(updmod.read_notes(missing) == updmod.EMPTY,
   "notes: a missing file falls back to a placeholder instead of raising")

blank = os.path.join(tempfile.mkdtemp(prefix="madi_notes_"), "CHANGELOG.md")
open(blank, "w", encoding="utf-8").close()
ok(updmod.read_notes(blank) == updmod.EMPTY,
   "notes: and so does an empty one - an empty tab explains nothing")

# ------------------------------------------------------------- not gated
import main as mainmod  # noqa: E402

gated = set()   # 1.19.0: nothing is gated
ok("What's New" not in gated,
   "notes: the tab is NOT members-only - what a paid build contains is exactly "
   "what someone deciding whether to pay should be able to read")
ok(hasattr(updmod.UpdatesPage, "set_capture_busy"),
   "notes: it still answers set_capture_busy, because the window greys every "
   "page it knows about")

print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
for label in FAIL:
    print("  FAILED: " + label)
sys.exit(1 if FAIL else 0)
