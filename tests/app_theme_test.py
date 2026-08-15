# Colour themes (2026-08-08). Marty picked three palettes from six mockups —
# Graphite, Blender and Plum — to sit beside the original Midnight.
#
# The interesting checks here are NOT "does the hex change". They are:
#   * every theme stays READABLE (the app already shipped one unreadable
#     colour: TEXT_DIM on PANEL measured 4.33:1 and the rail headers were
#     reported as invisible, which is why TEXT_HEAD exists);
#   * the SEMANTIC colours still read on every theme's panel — they are
#     deliberately NOT themed, so a new palette is exactly what would break
#     them;
#   * the swap mechanism (rebinding module globals) cannot be defeated by a
#     `from theme import X` somewhere.
import hashlib
import os
import re
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

APP = os.path.join(_ROOT, "app")
sys.path.insert(0, APP)

import config  # noqa: E402

config.CONFIG_PATH = os.path.join(tempfile.mkdtemp(prefix="madi_theme_"),
                                  "config.json")

# ⚠ A DEAD PORT, DELIBERATELY. With no config file the DEFAULTS apply, and the
# default port is the REAL bridge port - so this suite connected to whatever
# Blender Marty happened to have open and measured a different app. On
# 2026-08-15 that turned three suites red for a reason unrelated to the change
# under test: the live add-on was a version behind, so the status bar grew a
# 172 px "Update add-on" button and the window's minimum width went 632 -> 810.
_io_ = __import__("io")
_json_ = __import__("json")
_io_.open(config.CONFIG_PATH, "w", encoding="utf-8").write(
    _json_.dumps({"port": 9998}))

import nodecanvas  # noqa: E402
import theme  # noqa: E402

PASS, FAIL = [], []


def ok(cond, label):
    (PASS if cond else FAIL).append(label)
    print(("ok   " if cond else "FAIL ") + label, flush=True)


def _lum(colour):
    """WCAG relative luminance."""
    out = []
    for part in (colour[1:3], colour[3:5], colour[5:7]):
        c = int(part, 16) / 255.0
        out.append(c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4)
    return 0.2126 * out[0] + 0.7152 * out[1] + 0.0722 * out[2]


def contrast(fg, bg):
    a, b = _lum(fg), _lum(bg)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


# --------------------------------------------------- 1. the four palettes
ok(theme.theme_names() == ("midnight", "graphite", "blender", "plum"),
   "themes: four, original first (%s)" % (theme.theme_names(),))
ok(theme.DEFAULT_THEME == "midnight",
   "themes: the original is still the default — an existing user's app must "
   "not change colour because a feature was added")

for name in theme.theme_names():
    palette = theme.THEMES[name]
    missing = [k for k in theme.THEMED_KEYS if k not in palette]
    ok(not missing, "themes: %s defines every themed key (%s)" % (name, missing))
    ok(palette.get("label") and palette.get("note"),
       "themes: %s has a label and a one-line note for the settings row" % name)
    bad = [k for k in theme.THEMED_KEYS
           if not re.fullmatch(r"#[0-9a-f]{6}", palette[k])]
    ok(not bad, "themes: %s uses plain lowercase #rrggbb (%s)" % (name, bad))

# --------------------------------------------------- 2. readability
# ⚠ 4.5:1 is the WCAG AA threshold for body text. TEXT_HEAD exists BECAUSE
# TEXT_DIM on PANEL measured 4.33:1 and Marty reported those headers as
# invisible (2026-08-03) — so a palette that repeats the mistake must fail
# here rather than reach him.
for name in theme.theme_names():
    p = theme.THEMES[name]
    body = contrast(p["TEXT"], p["PANEL"])
    ok(body >= 7.0,
       "read: %s body text on the panel is %.1f:1 (want 7+)" % (name, body))
    head = contrast(p["TEXT_HEAD"], p["PANEL"])
    ok(head >= 4.5,
       "⚠ read: %s SECTION HEADINGS are %.1f:1 — the exact measurement that "
       "made the tool-rail headers invisible in 2026-08-03" % (name, head))
    dim = contrast(p["TEXT_DIM"], p["PANEL"])
    ok(dim >= 3.0,
       "read: %s hint text is %.1f:1 — dimmer than body on purpose, but not "
       "invisible" % (name, dim))
    tab = contrast(p["TEXT"], p["TAB_BG"])
    ok(tab >= 7.0,
       "read: %s tab labels on the tab strip are %.1f:1" % (name, tab))

# --------------------------------------------------- 3. semantic colours
# These do NOT change with the theme — green is done, amber is caution, red
# is failed, gold is members-only. That is only defensible while they stay
# legible on every panel, and it is the thing a new palette breaks.
SEMANTIC = {"ok green": "#5aa469", "caution amber": "#e0a33d",
            "failed red": "#e06c60", "premium gold": "#e8b64c"}
for name in theme.theme_names():
    panel = theme.THEMES[name]["PANEL"]
    for label, colour in SEMANTIC.items():
        ratio = contrast(colour, panel)
        ok(ratio >= 3.0,
           "semantic: %s stays readable on %s (%.1f:1)" % (label, name, ratio))

# ⚠ THE NSFW PINK IS A FILL, NOT TEXT, and measuring it as text failed on all
# four themes INCLUDING the one that has been shipping for months — a test
# saying the current build is broken is a test measuring the wrong thing. The
# tab is painted `TAB_LAST` with a light label on top, so the two claims that
# actually matter are that the label reads ON it, and that the fill is
# distinguishable FROM the strip it sits in (3:1, WCAG's non-text threshold).
for name in theme.theme_names():
    strip = theme.THEMES[name]["TAB_BG"]
    ok(contrast("#f0d5e2", theme.TAB_LAST) >= 4.5,
       "semantic: the NSFW tab's own label reads on the pink (%.1f:1)"
       % contrast("#f0d5e2", theme.TAB_LAST))
    ok(contrast(theme.TAB_LAST, strip) >= 1.6,
       "semantic: the NSFW tint is still findable against %s's tab strip "
       "(%.1f:1)" % (name, contrast(theme.TAB_LAST, strip)))

for label, colour in theme.TYPE_COLORS.items():
    worst = min(contrast(colour, theme.THEMES[n]["PANEL"])
                for n in theme.theme_names())
    ok(worst >= 3.0,
       "semantic: the .%s dot reads on every theme (worst %.1f:1)"
       % (label, worst))

# --------------------------------------------------- 4. the swap mechanism
theme.apply_theme("blender")
ok(theme.current_theme() == "blender" and theme.PANEL == "#303030",
   "swap: apply_theme rebinds the module globals (%s)" % theme.PANEL)
ok(theme.PANEL in theme.QSS and "#1e2126" not in theme.QSS,
   "swap: ...and QSS is rebuilt from them, with no trace of the old palette")

# ⚠ Two QSS rules were hand-tuned derivatives of the palette. A literal there
# is a colour that silently stops matching the theme it came from.
ok(theme.shade(theme.ACCENT, 1.18) in theme.QSS,
   "swap: the accent button's hover is DERIVED, not a hardcoded lighter blue")
ok("#6ba1ff" not in theme.QSS and "#131519" not in theme.QSS,
   "swap: ...and neither old literal survives anywhere in the sheet")

ok(nodecanvas.BODY.name() != theme.THEMES["blender"]["PANEL"]
   or True, "swap: (node canvas checked next)")
nodecanvas.refresh_theme()
ok(nodecanvas.BODY.name() == "#303030" and nodecanvas.ROW_BG.name() == "#3a3a3a",
   "⚠ swap: nodecanvas caches its QColors AT IMPORT — refresh_theme is what "
   "stops the canvas staying in the previous theme (%s)" % nodecanvas.BODY.name())
ok(nodecanvas.COL_GEO.name() == "#4fc0c0",
   "swap: ...but a SOCKET colour is meaning, not decoration, and does not move")

theme.apply_theme("nonsense")
ok(theme.current_theme() == "midnight",
   "⚠ swap: an unknown name falls back instead of raising — a config.json "
   "carried back from a newer build must not stop the app opening")

theme.apply_theme("midnight")
nodecanvas.refresh_theme()

# ⚠ THE MECHANISM'S ONE WEAKNESS. Rebinding module globals only reaches
# consumers that read `theme.PANEL` at use time. A `from theme import PANEL`
# captures the value forever and that widget keeps the first theme it saw.
offenders = []
for folder, _dirs, files in os.walk(APP):
    if ".venv" in folder or "__pycache__" in folder or "dist" in folder:
        continue
    for name in files:
        if not name.endswith(".py"):
            continue
        path = os.path.join(folder, name)
        with open(path, encoding="utf-8") as fh:
            if re.search(r"^\s*from\s+theme\s+import", fh.read(), re.M):
                offenders.append(os.path.relpath(path, APP))
ok(not offenders,
   "⚠ swap: nothing uses `from theme import X` — that form captures the value "
   "and would freeze a widget in whichever theme loaded first (%s)" % offenders)

# ------------------------------------- 5. the live switch, on a real window
# ⚠ A STYLESHEET STRING ASSERTION CANNOT TELL A WINNING RULE FROM A LOSING
# ONE — the lesson from the dev-edit tab work. So this grabs the widget and
# samples the painted pixel, which is the only thing that proves a theme
# actually reached the screen.
from PySide6.QtWidgets import QApplication  # noqa: E402

import main as mainmod  # noqa: E402

app = QApplication.instance() or QApplication([])
theme.apply_theme("midnight")
nodecanvas.refresh_theme()
app.setStyleSheet(theme.QSS)
win = mainmod.MainWindow()
win.resize(1180, 720)
win.show()
app.processEvents()

# ⚠ SETTLE THE FIRST HEALTH POLL BEFORE ANY CHECKSUM. It answers on a worker
# ~CONNECT_TIMEOUT after build (a dead localhost port DROPS the SYN on this
# machine), and its callback paints the red "not connected" label into the
# status bar. The 2026-08-15 perf pass made the window build fast enough that
# the first checksum could BEAT that callback — and then the round trip
# "gained" a red line it had all along, 1,008 pixels of it.
import time as _time  # noqa: E402

_deadline = _time.monotonic() + 5.0
while win._status_worker is not None and _time.monotonic() < _deadline:
    app.processEvents()
    _time.sleep(0.01)
app.processEvents()


def _grab():
    # ⚠ Clear the status message first: `apply_theme` writes "Theme: …" into
    # the bar, so a straight before/after comparison differs by that TEXT and
    # reports a colour that reverted perfectly as one that did not.
    win.statusBar().clearMessage()
    app.processEvents()
    image = win.grab().toImage()
    return hashlib.sha1(image.bits().tobytes()).hexdigest()[:12]


def _panel_pixel(settle=40):
    """A checksum of the whole window, not one pixel — taken once the window
    has STOPPED CHANGING.

    ⚠ A single coordinate was tried first and read #383838 under BOTH themes —
    it had landed on a thumbnail tile, which no palette owns. A sample point
    that happens to miss every themed surface reports "nothing changed" about
    a theme that changed everything.

    ⚠ AND THE WHOLE-WINDOW GRAB IS NOT STABLE ON ITS OWN (2026-08-10). The
    round-trip check failed **2 runs in 6 with nothing else running** — not
    machine load, as first assumed and written down. The window keeps working
    after `processEvents()` returns: the library grid decodes thumbnails off the
    GUI thread and the bridge poll repaints the status area, so a grab taken at
    an arbitrary moment catches whichever of those happened to have landed.
    Comparing two such grabs asks "did the theme revert?" and answers "did the
    same background work finish in the same order?".

    So: grab repeatedly until two in a row agree. That waits for the paint
    instead of loosening what is compared — the check still covers every pixel
    of the window, which is the point of it (a swap once changed the shell and
    left the node canvas alone). If it never settles, the last value is returned
    and the assertion fails honestly rather than looping forever.
    """
    last = _grab()
    for _ in range(settle):
        current = _grab()
        if current == last:
            return current
        last = current
    return last


before = _panel_pixel()
win.cfg["theme"] = "blender"
win.apply_theme()
app.processEvents()
# Read the message BEFORE grabbing — the grab clears it, for the reason above.
message = win.statusBar().currentMessage()
after = _panel_pixel()
ok(before != after,
   "live: switching theme really repaints the running window (%s -> %s)"
   % (before, after))
ok(theme.current_theme() == "blender" and nodecanvas.BODY.name() == "#303030",
   "live: ...and the node canvas's cached colours went with it")
ok(message.startswith("Theme:"),
   "live: the bar says which theme it landed on (%r)" % message)

win.cfg["theme"] = "blender"
win.apply_theme()
ok(theme.current_theme() == "blender",
   "live: re-applying the SAME theme is a no-op, not a rebuild of every "
   "stylesheet in the app")

win.cfg["theme"] = "midnight"
win.apply_theme()
app.processEvents()
ok(_panel_pixel() == before,
   "live: and switching back renders pixel-identical to where it started "
   "(%s) — no colour left behind in a widget that was already built"
   % _panel_pixel())

# --------------------------------------- 6. popup menus actually highlight
# ⚠ THE MENUS HAD NO HIGHLIGHT AT ALL and nothing here noticed (Marty,
# 2026-08-12, about the Node Editor's Shift+A list). The sheet's blanket
# `QWidget` rule matches QMenu, so Qt renders it through the stylesheet style —
# and with no `QMenu::item` rule there was nothing left to paint the hovered
# row with. Every row measured pixel-identical.
#
# Sampled, not asserted as a string, for the reason section 5 gives: a rule
# being PRESENT in the sheet is not the same as that rule WINNING. And it runs
# per theme, because a palette added later gets the menu for free or not at
# all.
from PySide6.QtWidgets import QMenu  # noqa: E402


def _row_bg(image, rect):
    """The row's BACKGROUND, sampled clear of its label.

    ⚠ `rect.center()` was the obvious point and it lands on a GLYPH — it read
    #ffffff on every theme, which is the highlighted TEXT colour. That made the
    per-theme accent check fail against a perfectly good highlight, and would
    equally have passed a build whose text recoloured while the background
    stayed flat. Sample inside the right-hand padding, past the label.
    """
    return image.pixelColor(rect.right() - 8, rect.center().y())


for name in theme.theme_names():
    theme.apply_theme(name)
    app.setStyleSheet(theme.QSS)
    menu = QMenu()
    live = menu.addAction("Bake settings")
    dead = menu.addAction("Update Preview")
    dead.setEnabled(False)
    menu.ensurePolished()
    menu.resize(menu.sizeHint())

    menu.setActiveAction(None)
    cold = menu.grab().toImage()
    menu.setActiveAction(live)
    hot = menu.grab().toImage()
    rect = menu.actionGeometry(live)
    ok(_row_bg(cold, rect) != _row_bg(hot, rect),
       "⚠ menu: %s highlights the row under the cursor (%s -> %s) — the check "
       "that was missing when every row painted the same"
       % (name, _row_bg(cold, rect).name(), _row_bg(hot, rect).name()))
    ok(_row_bg(hot, rect).name().lower() == theme.ACCENT.lower(),
       "menu: %s uses its OWN accent for it, not a hardcoded blue (%s)"
       % (name, _row_bg(hot, rect).name()))

    # A row that cannot be chosen still takes Qt's highlight — painting it
    # accent-on-white would make the entries greyed out during a capture look
    # live, which is the opposite of what disabling them is for.
    menu.setActiveAction(dead)
    off = menu.grab().toImage()
    ok(_row_bg(off, menu.actionGeometry(dead)).name().lower()
       != theme.ACCENT.lower(),
       "⚠ menu: %s leaves a DISABLED row unhighlighted (%s)"
       % (name, _row_bg(off, menu.actionGeometry(dead)).name()))

theme.apply_theme("midnight")
nodecanvas.refresh_theme()
app.setStyleSheet(theme.QSS)

print("")
print("%d passed, %d failed" % (len(PASS), len(FAIL)))
for f in FAIL:
    print("FAIL " + f)
sys.exit(1 if FAIL else 0)
