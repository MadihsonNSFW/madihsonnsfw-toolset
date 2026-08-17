"""Dark theme QSS for the MadihsonNSFW Toolset, styled after Studio Library.

**FOUR PALETTES** since 2026-08-08 (Marty picked three to add to the original
from six mockups). `THEMES` below holds them; `apply_theme(name)` swaps one in
and rebuilds `QSS`.

⚠ **ONLY THE STRUCTURAL COLOURS ARE THEMED** — surfaces, borders, body text,
the accent, the tab strip. The semantic ones are deliberately NOT: green means
done, amber means caution, red means failed, gold means members-only, pink
means NSFW Tools, and `TYPE_COLORS` tells ten library item types apart. A
colour that carries meaning must not change with a decoration setting, or the
meaning is a decoration too.

⚠ **THAT ONLY HOLDS BECAUSE ALL FOUR PALETTES ARE DARK.** Marty's light
candidate ("Ash") was not chosen, and it is the one that would have broken
this: the premium gold measured 1.6:1 on a light panel and would have needed
its own per-theme variant, and so would half of TYPE_COLORS. **If a light
theme is ever added, the semantic colours stop being constants** — see
`tests\app_theme_test.py`, which measures every semantic colour against every
theme's panel and is what would catch it.

⚠ **REBINDING MODULE GLOBALS IS THE MECHANISM**, and it works only because
every consumer reads `theme.PANEL` rather than `from theme import PANEL` — a
`from` import would capture the old value and that widget would keep the old
colour forever. A test pins that no module uses the `from` form.
"""

import os
import tempfile

import config

# The structural palette. Every key here is themeable; anything not in here is
# fixed across themes on purpose (see the module docstring).
THEMES = {
    "midnight": {
        "label": "Midnight",
        "note": "The original. Cool grey-blue.",
        "ACCENT": "#4f8cff", "BG": "#17191d", "PANEL": "#1e2126",
        "PANEL2": "#23262c", "BORDER": "#2c3038", "TEXT": "#c9ced6",
        "TEXT_DIM": "#7d8590", "TEXT_HEAD": "#a3acb9",
        "TAB_BG": "#161414", "TAB_BG_SELECTED": "#2b2b2b",
    },
    "graphite": {
        "label": "Graphite",
        "note": "The same darkness with the blue cast taken out.",
        "ACCENT": "#c98f4a", "BG": "#1a1a19", "PANEL": "#232322",
        "PANEL2": "#2c2b29", "BORDER": "#33322f", "TEXT": "#d6d3cd",
        "TEXT_DIM": "#8a8781", "TEXT_HEAD": "#b0ada6",
        "TAB_BG": "#141413", "TAB_BG_SELECTED": "#2e2d2b",
    },
    "blender": {
        "label": "Blender",
        "note": "Matches Blender's own greys, so the two read as one workspace.",
        "ACCENT": "#4772b3", "BG": "#1d1d1d", "PANEL": "#303030",
        "PANEL2": "#3a3a3a", "BORDER": "#4a4a4a", "TEXT": "#e5e5e5",
        "TEXT_DIM": "#9a9a9a", "TEXT_HEAD": "#bfbfbf",
        "TAB_BG": "#161616", "TAB_BG_SELECTED": "#303030",
    },
    "plum": {
        "label": "Plum",
        "note": "Purple-cast dark; the one the NSFW pink was always going to suit.",
        "ACCENT": "#c060a8", "BG": "#191320", "PANEL": "#211a2b",
        "PANEL2": "#2c2338", "BORDER": "#342a41", "TEXT": "#d3c9df",
        "TEXT_DIM": "#8d829b", "TEXT_HEAD": "#b0a4bf",
        "TAB_BG": "#130f18", "TAB_BG_SELECTED": "#2c2338",
    },
}
DEFAULT_THEME = "midnight"
# What `apply_theme` is allowed to rebind. Deliberately explicit: a typo in a
# palette must fail loudly rather than quietly inventing a module global.
THEMED_KEYS = ("ACCENT", "BG", "PANEL", "PANEL2", "BORDER", "TEXT",
               "TEXT_DIM", "TEXT_HEAD", "TAB_BG", "TAB_BG_SELECTED")

ACCENT = "#4f8cff"
BG = "#17191d"
PANEL = "#1e2126"
PANEL2 = "#23262c"
BORDER = "#2c3038"
TEXT = "#c9ced6"
TEXT_DIM = "#7d8590"
# Section headings that must stay READABLE while still reading as secondary.
# TEXT_DIM on PANEL measures 4.33:1, which is under the 4.5:1 readability
# threshold — fine for a hint label you glance at, not fine for the tool-rail
# group headers, which Marty reported as invisible (2026-08-03). This is 7.0:1.
TEXT_HEAD = "#a3acb9"

# Outer (toolset section) tab bar — Marty's colours, picked 2026-08-04 through
# Developer mode: edit. The strip is darker than the panels so the tabs read as
# a header rather than as part of the page, the selected tab lifts out of it,
# and NSFW Tools is tinted so it is findable at a glance.
TAB_BG = "#161414"
TAB_BG_SELECTED = "#2b2b2b"
TAB_LAST = "#9c4071"

# ⚠ TINTED BY NAME, NOT BY POSITION. This used to be `QTabBar::tab:last` in the
# QSS below, because Qt stylesheets have no per-index tab selector — which meant
# the colour followed whichever tab was last rather than following NSFW Tools.
# The tab order changed on 2026-08-04 and the pink would have moved to Rendering
# by itself. `main.SectionTabBar` paints these, so the order is free to change.
TAB_TINTS = {"NSFW Tools": TAB_LAST}

# The little star on every members-only tab (Marty, 2026-08-04) — so it is
# obvious which tabs come with supporting, before anyone clicks one and finds a
# lock panel. Gold reads as "premium" without competing with the pink tint or
# the blue accent underline.
# ⚠ PAINTED, NOT APPENDED TO THE TAB TEXT. The tab's text is a LOOKUP KEY in
# four places — TAB_TINTS, main.TAB_TEXT_COLORS, devedit's saved renames and the
# test suites' exact title lists — so "Physics ★" would quietly stop matching
# "Physics" in all of them. main.SectionTabBar draws it instead.
PREMIUM_MARK = "#e8b64c"

# Amber for "read this before you press the button" — a caution inside a dialog,
# not an error. Deliberately not red: nothing has gone wrong, and colouring a
# heads-up like a failure trains people to ignore both.
WARN = "#e0a34f"

TYPE_COLORS = {
    "pose": "#4f8cff",
    "anim": "#b06cf0",
    "set": "#4fc07a",
    "mirror": "#e0a34f",
    "shapes": "#e06ca8",
    "vgroups": "#5ac8b0",
    "remap": "#4fc0c0",
    "abc": "#c0b04f",
    "playblast": "#a8b0bc",
    "picker": "#8f7ae0",
    "renderpreset": "#e0704f",
}

# color labels for tiles (Studio Library style) — name -> hex, order = menu order
LABEL_COLORS = (
    ("Red", "#e06c60"),
    ("Orange", "#e0a34f"),
    ("Yellow", "#d8c74f"),
    ("Green", "#4fc07a"),
    ("Blue", "#4f8cff"),
    ("Purple", "#b06cf0"),
)

# ---- the checkbox tick ------------------------------------------------
# ⚠ Qt cannot draw a checkmark from a stylesheet: the moment ::indicator is
# styled at all, the native tick is discarded and only the background rules
# remain — which is why every checked box in the app was a plain blue
# square until 2026-08-06 (Marty picked style A: white tick on the accent).
# The tick therefore has to be an image file the QSS points at. It is
# REGENERATED on every start rather than shipped: a PyInstaller rebuild
# wipes the dist folder, and an asset that self-heals cannot be lost with
# it. Falls back to %TEMP% if the app folder is not writable.


def _write_indicator_svgs():
    tick = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 14 14">'
            '<path d="M3 7.4 6 10.2 11 4.6" fill="none" stroke="%s" '
            'stroke-width="2.2" stroke-linecap="round" '
            'stroke-linejoin="round"/></svg>')
    wanted = (("check.svg", "#ffffff"), ("check_dim.svg", TEXT_DIM))
    # ⚠ DATA_DIR, NOT APP_DIR. These SVGs are GENERATED, so this is a
    # write - and on macOS APP_DIR is inside the .app bundle, where
    # writing breaks the signature. On Windows and Linux the two are the
    # same folder, so nothing moves.
    for folder in (os.path.join(config.DATA_DIR, "assets"),
                   os.path.join(tempfile.gettempdir(),
                                "MadihsonNSFW Toolset")):
        try:
            os.makedirs(folder, exist_ok=True)
            paths = []
            for name, colour in wanted:
                path = os.path.join(folder, name)
                data = tick % colour
                try:
                    with open(path, encoding="utf-8") as fh:
                        stale = fh.read() != data
                except OSError:
                    stale = True
                if stale:
                    with open(path, "w", encoding="utf-8") as fh:
                        fh.write(data)
                paths.append(path)
            return paths
        except OSError:
            continue
    # Nowhere writable: the url() will point at a missing file and Qt just
    # paints the plain square — the pre-2026-08-06 look, never a crash.
    return [os.path.join(config.DATA_DIR, "assets", n)
            for n, _c in wanted]


CHECK_SVG, CHECK_DIM_SVG = _write_indicator_svgs()
# Qt stylesheets want url() paths with FORWARD slashes, even on Windows.
_CHECK_URL = CHECK_SVG.replace("\\", "/")
_CHECK_DIM_URL = CHECK_DIM_SVG.replace("\\", "/")


def shade(colour, factor):
    """Lighten (factor > 1) or darken (factor < 1) a #rrggbb string.

    Exists because two QSS rules were hand-tuned derivatives of the palette —
    the accent button's hover and the console's background — and a hard-coded
    derivative is a colour that silently stops matching the theme it was
    derived from.
    """
    value = colour.lstrip("#")
    parts = [int(value[i:i + 2], 16) for i in (0, 2, 4)]
    return "#%02x%02x%02x" % tuple(max(0, min(255, round(p * factor)))
                                   for p in parts)


def current_theme():
    return _CURRENT


def theme_names():
    """Menu order: the original first, then the three added on 2026-08-08."""
    return ("midnight", "graphite", "blender", "plum")


def apply_theme(name):
    """Swap the structural palette and rebuild QSS. Returns the new QSS.

    The caller is responsible for handing it to `QApplication.setStyleSheet`
    and for repainting anything that draws itself — see `main.apply_theme`.
    An unknown name falls back to the default rather than raising: a
    config.json edited by hand, or carried back from a newer build, must not
    stop the app opening.
    """
    global QSS, _CURRENT, CHECK_SVG, CHECK_DIM_SVG, _CHECK_URL, _CHECK_DIM_URL
    palette = THEMES.get(name) or THEMES[DEFAULT_THEME]
    _CURRENT = name if name in THEMES else DEFAULT_THEME
    for key in THEMED_KEYS:
        globals()[key] = palette[key]
    # ⚠ The dim checkmark is drawn in TEXT_DIM, so it is part of the palette —
    # regenerate it or an unticked box keeps the previous theme's grey.
    CHECK_SVG, CHECK_DIM_SVG = _write_indicator_svgs()
    _CHECK_URL = CHECK_SVG.replace("\\", "/")
    _CHECK_DIM_URL = CHECK_DIM_SVG.replace("\\", "/")
    QSS = build_qss()
    return QSS


def build_palette():
    """The app-wide defaults the universal rule used to set, as a QPalette.

    ⚠ DISABLED IS DELIBERATELY THE SAME COLOUR AS ACTIVE for the text roles:
    the QSS `color:` used to apply to disabled widgets too, so a stock grey
    here would CHANGE how every unstyled disabled widget reads. Widgets that
    want a dim disabled look say so in QSS (`#dim`, `:disabled`), as before.
    """
    from PySide6.QtGui import QColor, QPalette
    pal = QPalette()
    bg, text = QColor(BG), QColor(TEXT)
    for group in (QPalette.Active, QPalette.Inactive, QPalette.Disabled):
        for role in (QPalette.Window, QPalette.Base, QPalette.AlternateBase,
                     QPalette.Button, QPalette.ToolTipBase):
            pal.setColor(group, role, bg)
        for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText,
                     QPalette.ToolTipText):
            pal.setColor(group, role, text)
    return pal


def app_font():
    from PySide6.QtGui import QFont
    font = QFont("Segoe UI")
    font.setPixelSize(12)   # the QSS said 12px — PIXELS, not points; a point
    return font             # size here would move every metric with DPI


def apply_app_defaults(app):
    """Font + palette in place of the universal QWidget rule (PERF_PLAN B).

    ⚠ Call BEFORE the first window is built and AGAIN on every theme swap —
    `build_palette` reads the rebound globals. Font first: widgets measure
    themselves against it during construction (ElidedLabel, the rail's
    cutoff, the window minimum)."""
    app.setFont(app_font())
    app.setPalette(build_palette())


def build_qss():
    # ⚠ NO UNIVERSAL `QWidget {{ }}` RULE — that was the classic Qt stylesheet
    # trap: 98 characters matching every widget in the process, 167 ms of
    # every window build (PERF_PLAN B, measured 2026-08-15). Its three jobs —
    # default background, default text colour, "Segoe UI" 12px — live in
    # `apply_app_defaults` (QPalette + app font), which is what Qt wants used
    # for defaults anyway. main() applies BOTH; a stylesheet alone no longer
    # paints an unstyled widget's ground.
    return f"""
QMainWindow, QDialog {{ background: {BG}; }}
QSplitter::handle {{ background: {BG}; width: 3px; }}

QTabWidget::pane {{ border: none; }}
QTabBar::tab {{
    background: {BG}; color: {TEXT_DIM};
    padding: 6px 14px; border: none;
    border-bottom: 2px solid transparent;
}}
QTabBar::tab:selected {{ color: {TEXT}; border-bottom: 2px solid {ACCENT}; }}
QTabBar::tab:hover {{ color: {TEXT}; }}
QTabBar::close-button {{ image: none; }}

/* outer (toolset section) tabs — the strip is HIDDEN since 2026-08-14; the
   left rail below navigates instead. These rules are kept because the QTabBar
   itself is kept (it is still the widget that holds the pages, and its tab
   TEXT is still the app's internal key), and because `devedit` builds
   `QTabWidget#maintabs > QTabBar` selectors when Marty recolours a tab. */
QTabWidget#maintabs > QTabBar {{ background: {TAB_BG}; }}
QTabWidget#maintabs > QTabBar::tab {{
    background: {TAB_BG}; padding: 8px 22px; font-size: 13px;
    border-bottom: 2px solid transparent;
}}
QTabWidget#maintabs > QTabBar::tab:selected {{
    background: {TAB_BG_SELECTED}; color: {TEXT};
    border-bottom: 2px solid {ACCENT};
}}
/* The NSFW Tools tint is NOT here: a stylesheet cannot pick a tab by index, so
   `:last` meant "whichever tab is last" and moved when the order changed.
   `widgets.SectionRail` paints TAB_TINTS by name instead. */

/* ---- the section rail: the app's navigation (widgets.SectionRail) ----
   Darker than the pages it sits beside, so it reads as chrome rather than as
   part of whichever tool is open — the job TAB_BG used to do for the strip,
   which is why it is still the same token.
   ⚠ EVERY entry carries `border-left: 2px solid transparent`, not just the
   selected one. Qt lays an item out inside its border box, so giving only the
   selected row a 2 px edge shifts its icon and label sideways on click, and
   the whole rail twitches as you move down it. */
QTreeWidget#sectionrail {{
    background: {TAB_BG}; border: none; border-right: 1px solid {BORDER};
    outline: none; padding: 7px 0;
}}
QTreeWidget#sectionrail::item {{
    color: {TEXT}; padding: 5px 7px; margin: 1px 7px;
    border: none; border-left: 2px solid transparent; border-radius: 6px;
}}
/* ⚠ NO `::item:hover` RULE HERE ON PURPOSE. The stylesheet paints an item's
   background AFTER `SectionRail.drawRow` has run, so a hover rule painted over
   the group headings' filled bar and made a heading look UNFILLED exactly
   while the pointer was on it. Hover is drawn in `drawRow` with everything
   else, where the order is ours to choose. */
QTreeWidget#sectionrail::item:selected {{
    background: {TAB_BG_SELECTED}; color: {TEXT};
    border-left: 2px solid {ACCENT};
}}

/* ---- the window chrome: our title bar, in place of Windows' (chrome.py) --
   The rail header carries the SAME surface and the SAME right border as the
   rail below it, which is the whole trick of the "seamless rail" concept —
   the column has to read as one piece from the app mark down to the last
   tool, with no seam where the title bar used to stop. */
QWidget#railheader {{
    background: {TAB_BG}; border-right: 1px solid {BORDER};
}}
QLabel#railwordmark {{ background: transparent; color: {TEXT}; font-weight: 600; }}
QWidget#titlestrip {{ background: {BG}; }}
QLabel#titlesection {{ background: transparent; color: {TEXT_DIM}; }}
/* ⚠ The window buttons are sized in code (chrome._ControlButton), so no
   padding or min-width here — the generic QPushButton rule would otherwise
   pad them back out and push the close button off the corner. */
QPushButton#winbtn, QPushButton#winclose {{
    background: transparent; border: none; border-radius: 5px; padding: 0;
}}
QPushButton#winbtn:hover {{ background: {PANEL2}; }}
QPushButton#winbtn:pressed {{ background: {BORDER}; }}
/* Windows' own close-button red, kept literal in both themes: it is a safety
   colour people recognise, not part of our palette. */
QPushButton#winclose:hover {{ background: #c42b1c; }}
QPushButton#winclose:pressed {{ background: #b02416; }}

QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QTextEdit {{
    background: {PANEL2}; border: 1px solid {BORDER}; border-radius: 4px;
    padding: 4px 8px; color: {TEXT}; selection-background-color: {ACCENT};
}}
QLineEdit:focus {{ border-color: {ACCENT}; }}
/* Disabled inputs used to look IDENTICAL to live ones: only QPushButton and
   QToolButton had a :disabled rule, so any greyed-out combo or field read
   as clickable. */
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled,
QDoubleSpinBox:disabled, QTextEdit:disabled {{
    background: {PANEL}; color: {TEXT_DIM}; border-color: {PANEL2};
}}
QComboBox:disabled::drop-down {{ color: {TEXT_DIM}; }}
QCheckBox:disabled, QRadioButton:disabled, QLabel:disabled {{
    color: {TEXT_DIM};
}}
QCheckBox:disabled::indicator, QRadioButton:disabled::indicator {{
    border-color: {PANEL2}; background: {PANEL};
}}

QPushButton {{
    background: {PANEL2}; border: 1px solid {BORDER}; border-radius: 4px;
    padding: 5px 14px; color: {TEXT};
}}
QPushButton:hover {{ background: {BORDER}; }}
QPushButton:pressed {{ background: {PANEL}; }}
QPushButton:disabled {{ color: {TEXT_DIM}; }}
QPushButton#accent {{
    background: {ACCENT}; border: none; color: white; font-weight: bold;
}}
QPushButton#accent:hover {{ background: {shade(ACCENT, 1.18)}; }}
QPushButton#flat {{ background: transparent; border: none; padding: 4px 8px; }}
QPushButton#flat:hover {{ background: {PANEL2}; }}
QPushButton#danger {{
    background: transparent; border: 1px solid #6b3230; color: #e06c60;
}}
QPushButton#danger:hover {{ background: #4a2523; }}
/* The "Buy me a coffee" chip in the status bar. Marty picked this one from
   six rendered variants (2026-08-17) - it sits on the panel colour like a tag
   rather than competing with the file name beside it. Themed here rather than
   with a hardcoded stylesheet so it follows a theme swap; only the heart is a
   fixed red, and that lives in `icons.py`. */
QPushButton#support {{
    background: {PANEL2}; border: 1px solid {BORDER}; border-radius: 4px;
    color: {TEXT_DIM}; padding: 3px 10px;
}}
QPushButton#support:hover {{ background: {PANEL}; color: {TEXT}; }}

QTreeWidget, QListWidget {{
    background: {PANEL}; border: none; outline: none;
}}
QTreeWidget::item {{ padding: 3px 2px; border-radius: 4px; }}
QTreeWidget::item:selected, QListWidget::item:selected {{
    background: {PANEL2}; border: 1px solid {ACCENT};
}}
QTreeWidget::item:hover {{ background: {PANEL2}; }}

/* The tool rail inside a RenderingPage (Rendering, Node Setup, Anim Layers,
   Physics). A single-column tree stretches its last section, so a row's rect
   is the FULL viewport width — the selected row's 1 px accent outline landed
   on the very last pixel column of the rail and read as a cut-off box (Marty,
   2026-08-16). This insets the rows so the outline closes inside the panel.

   ⚠ PAD THE TREE, NEVER MARGIN THE ROWS. A margin on `::item` insets the row
   the same way, but it also stops the row's background covering the full
   width — and the indent Qt draws to the left of each tool is the BRANCH
   column, which the native Windows style fills on the current row IN THE
   USER'S OWN SYSTEM ACCENT COLOUR. Margin the row and a stray coloured tab
   appears beside the selected tool (measured: a coral 2x6 bar, from Marty's
   Windows accent). `::branch` QSS does not suppress it — background,
   border-image, image, every :has-children state, all ignored — and neither
   does overriding `drawBranches`; the fill comes from the row. Padding the
   viewport moves the whole row instead, so it still covers its own branch.
   ⚠ Not on the shared `QTreeWidget` rule either: the data trees (markers,
   presets, the optimizer report) are multi-column and this is a rail. */
QTreeWidget#toolrail {{ padding: 2px 4px; }}
QListWidget::item {{ border-radius: 6px; border: 1px solid transparent; padding: 2px; }}
QListWidget::item:hover {{ background: {PANEL2}; }}

QCheckBox, QRadioButton {{ spacing: 6px; }}
QCheckBox::indicator, QRadioButton::indicator {{
    width: 14px; height: 14px; border: 1px solid {BORDER};
    border-radius: 3px; background: {PANEL2};
}}
QRadioButton::indicator {{ border-radius: 7px; }}
/* The tick itself is an image (see _write_indicator_svgs above). Radios are
   deliberately image-free: a radio is a dot, not a tick. */
QCheckBox::indicator:checked {{
    background: {ACCENT}; border-color: {ACCENT};
    image: url("{_CHECK_URL}");
}}
QRadioButton::indicator:checked {{
    background: {ACCENT}; border-color: {ACCENT};
}}
QCheckBox::indicator:checked:disabled {{
    background: {PANEL}; border-color: {PANEL2};
    image: url("{_CHECK_DIM_URL}");
}}
/* The same tick on tickboxes living inside item views — the Render Queue's
   collections tree, the shape-key and vertex-group pickers. "Every tickbox
   in the app" includes these, and unstyled they drew the platform's own
   indicator next to our styled ones. */
QTreeWidget::indicator, QTreeView::indicator,
QListWidget::indicator, QListView::indicator {{
    width: 14px; height: 14px; border: 1px solid {BORDER};
    border-radius: 3px; background: {PANEL2};
}}
QTreeWidget::indicator:checked, QTreeView::indicator:checked,
QListWidget::indicator:checked, QListView::indicator:checked {{
    background: {ACCENT}; border-color: {ACCENT};
    image: url("{_CHECK_URL}");
}}

QSlider::groove:horizontal {{
    height: 4px; background: {PANEL2}; border-radius: 2px;
}}
QSlider::handle:horizontal {{
    width: 14px; height: 14px; margin: -5px 0;
    background: {TEXT}; border-radius: 7px;
}}
QSlider::sub-page:horizontal {{ background: {ACCENT}; border-radius: 2px; }}

QScrollBar:vertical {{
    background: transparent; width: 10px; margin: 0;
}}
QScrollBar::handle:vertical {{
    background: {BORDER}; border-radius: 5px; min-height: 30px;
}}
QScrollBar::handle:vertical:hover {{ background: {TEXT_DIM}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; }}
QScrollBar::handle:horizontal {{ background: {BORDER}; border-radius: 5px; min-width: 30px; }}

/* icon toolbars (Render Queue tool) */
QToolBar {{
    background: {PANEL}; border: none; border-radius: 4px;
    spacing: 1px; padding: 3px;
}}
QToolBar::separator {{ background: {BORDER}; width: 1px; margin: 5px 4px; }}
QToolButton {{
    background: transparent; border: 1px solid transparent; border-radius: 4px;
    padding: 3px 5px; color: {TEXT}; min-width: 34px;
}}
QToolButton:hover {{ background: {PANEL2}; border: 1px solid {BORDER}; }}
QToolButton:pressed {{ background: {BG}; }}
QToolButton:checked {{ background: {ACCENT}; border: 1px solid {ACCENT}; color: white; }}
QToolButton:disabled {{ color: {TEXT_DIM}; }}

/* Popup menus — every right-click menu, the Node Editor's Shift+A Add list and
   the Bake node's material picker.
   ⚠ THESE RULES ARE NOT OPTIONAL AND THE MENUS HAD NONE (Marty, 2026-08-12:
   the Shift+A list did not highlight under the cursor). The blanket `QWidget`
   rule at the top of this sheet matches QMenu as well, so once it applies Qt
   renders the menu through the stylesheet style — and with no `::item` rule
   there is nothing left to paint a highlight with. Measured before the fix:
   the ACTIVE row and the inactive rows were pixel-identical at (23, 25, 29).
   ⚠ Style the WHOLE menu or none of it. `::item` padding moves the icon, the
   submenu arrow and the separator with it, so `::icon`, `::right-arrow` and
   `::separator` below are part of the same fix, not extras — the Color Label
   submenu carries swatch icons and is what shows it. */
QMenu {{
    background: {PANEL}; border: 1px solid {BORDER}; border-radius: 6px;
    padding: 4px;
}}
QMenu::item {{
    background: transparent; color: {TEXT};
    padding: 5px 28px 5px 26px; margin: 1px 2px; border-radius: 4px;
}}
QMenu::item:selected {{ background: {ACCENT}; color: #ffffff; }}
/* ⚠ Disabled must beat :selected — a menu row you cannot choose still takes
   the keyboard/mouse highlight in Qt, and painting it accent-on-white made a
   dead entry (the ones greyed out while a capture runs) look live. */
QMenu::item:disabled {{ color: {TEXT_DIM}; }}
QMenu::item:selected:disabled {{ background: transparent; color: {TEXT_DIM}; }}
QMenu::separator {{ height: 1px; background: {BORDER}; margin: 4px 10px; }}
QMenu::icon {{ left: 8px; }}
/* ⚠ The submenu arrow is left to the platform style ON PURPOSE. A QSS-drawn
   one has to be two borders (stylesheets cannot rotate), which paints an
   L-shaped corner rather than a chevron — rendered and looked at, 2026-08-12.
   An `image:` would work but would mean a third generated SVG for one arrow.
   Nothing else here overrides it, so Qt keeps drawing the real one. */

QTableWidget {{
    background: {PANEL}; alternate-background-color: {PANEL2};
    gridline-color: {BORDER}; border: 1px solid {BORDER}; border-radius: 4px;
}}
QTableWidget::item {{ padding: 3px 4px; }}
QTableWidget::item:selected {{ background: {ACCENT}; color: white; }}
QHeaderView::section {{
    background: {PANEL2}; color: {TEXT_DIM}; padding: 5px 8px;
    border: none; border-right: 1px solid {BG};
}}

QPlainTextEdit {{
    background: {shade(BG, 0.82)}; border: 1px solid {BORDER}; border-radius: 4px;
    color: {TEXT}; selection-background-color: {ACCENT};
}}

QStatusBar {{ background: {PANEL}; color: {TEXT_DIM}; }}
QToolTip {{
    background: {PANEL2}; color: {TEXT}; border: 1px solid {BORDER}; padding: 4px;
}}
QGroupBox {{
    border: 1px solid {BORDER}; border-radius: 6px; margin-top: 10px; padding-top: 6px;
}}
QGroupBox::title {{ subcontrol-origin: margin; left: 8px; color: {TEXT_DIM}; }}
QLabel#dim {{ color: {TEXT_DIM}; }}
QLabel#h1 {{ font-size: 14px; font-weight: bold; }}
QFrame#panel {{ background: {PANEL}; border-radius: 6px; }}
"""


# ⚠ `QSS` stays a module ATTRIBUTE, not just a function, because that is what
# every existing caller and four test suites read. `apply_theme` rebinds it.
_CURRENT = DEFAULT_THEME
QSS = build_qss()
