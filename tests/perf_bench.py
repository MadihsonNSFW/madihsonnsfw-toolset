# Performance benchmark + regression ceilings (2026-08-15).
#
# Marty: *"we need to start optimizing modules, so they work faster on cpu /
# ram"*. `PERF_PLAN.md` holds the measurements and the menu; this file is what
# stops a win being quietly given back.
#
# ⚠⚠ **THE ASSERTIONS ARE ON WORK DONE, NOT ON WALL-CLOCK TIME.** Counting
# widgets, event-filter invocations, file reads and imported modules is
# deterministic — the same code gives the same number on any machine, in any
# mood, under a profiler. Timings are printed because they are what Marty
# actually feels, but pinning a millisecond figure to a ceiling would fail on a
# busy machine and pass on a fast one, which is worse than not checking.
#
# ⚠ **CEILINGS START AT TODAY'S MEASURED VALUES AND ARE TIGHTENED AS EACH
# OPTIMISATION LANDS.** Each one carries the number it is expected to fall to,
# so a half-finished change is visible: the suite still passes, but the comment
# no longer matches the print-out.
import io
import json
import linecache
import os
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

APP = os.path.join(_ROOT, "app")
sys.path.insert(0, APP)

from PySide6.QtWidgets import (QAbstractSpinBox, QApplication,  # noqa: E402
                               QComboBox, QSlider, QTabBar, QWidget)

import config  # noqa: E402

WORK = tempfile.mkdtemp(prefix="madi_perf_")
_live = os.path.join(APP, "config.json")
_cfg = json.load(io.open(_live, encoding="utf-8")) if os.path.isfile(_live) else {}
_cfg["port"] = 9908                      # never the live bridge port
_cfg["libraries"] = [{"name": "Bench", "path": os.path.join(WORK, "library")}]
os.makedirs(_cfg["libraries"][0]["path"], exist_ok=True)
config.CONFIG_PATH = os.path.join(WORK, "config.json")
io.open(config.CONFIG_PATH, "w", encoding="utf-8").write(json.dumps(_cfg))

PASS, FAIL = [], []


def ok(cond, label):
    (PASS if cond else FAIL).append(label)
    print(("ok   " if cond else "FAIL ") + label, flush=True)


try:
    import psutil
except Exception:
    psutil = None


def rss():
    if psutil is None:
        return 0.0
    return psutil.Process().memory_info().rss / 1048576.0


# =============================================================== imports
t0, m0 = time.perf_counter(), rss()
import theme  # noqa: E402
import widgets  # noqa: E402

app = QApplication.instance() or QApplication([])
app.setStyleSheet(theme.QSS)

t0, m0 = time.perf_counter(), rss()
import main as mainmod  # noqa: E402
import_ms, import_mb = (time.perf_counter() - t0) * 1000.0, rss() - m0
print("import main: %.0f ms, %+.1f MB" % (import_ms, import_mb))

# ⚠ **TIGHTENED TO False ON 2026-08-15.** `sysmon` used to `import pynvml` and
# call `nvmlInit()` at module import, and it is reached from `render_tools` ->
# `queue_tool`, which main.py imports at startup — so every session paid 23 MB
# and 38 ms to initialise the NVIDIA management library, including the ones
# that never open the Render Queue. It is now loaded on the first `vram()`.
NVML_EXPECTED_AT_IMPORT = False
ok(("pynvml" in sys.modules) == NVML_EXPECTED_AT_IMPORT,
   "pynvml imported at startup: %s (expected %s — the GPU library costs 23 MB "
   "and is only needed by the Render Queue's stat cards)"
   % ("pynvml" in sys.modules, NVML_EXPECTED_AT_IMPORT))


# ========================================================== the wheel guard
# ⚠⚠ **THE COUNTER WRAPS THE REAL FILTER; IT DOES NOT INSTALL ONE.** The first
# version of this file did `app.installEventFilter(counter)` — which is exactly
# the app-wide installation the optimisation removes, so the benchmark
# re-created the problem it was measuring and reported no improvement at all
# after the fix had landed. Instrument the thing under test, never a copy of it.
class _Counter(object):
    calls = 0


_real_filter = widgets.NoWheelFilter.eventFilter


def _counted(self, obj, event):
    _Counter.calls += 1
    return _real_filter(self, obj, event)


widgets.NoWheelFilter.eventFilter = _counted

t0, m0 = time.perf_counter(), rss()
win = mainmod.MainWindow()
build_ms, build_mb = (time.perf_counter() - t0) * 1000.0, rss() - m0


class CountingGuard(object):
    calls = property(lambda self: _Counter.calls)


CountingGuard = _Counter          # keep the name the checks below use

widget_count = len(win.findChildren(QWidget))
print("MainWindow: %.0f ms, %+.1f MB, %d widgets, %d guard calls"
      % (build_ms, build_mb, widget_count, CountingGuard.calls))

# ⚠ **TIGHTENED FROM 130,000 TO 200 WHEN OPTION A LANDED** (2026-08-15): the
# guard went from a filter on the QApplication — 91,659 invocations to build
# one window — to a filter on the 75 widgets that can be edited by a wheel, so
# the count is now one per install and nothing at all afterwards. A regression
# past this ceiling means somebody put it back on the application.
GUARD_CALL_CEILING = 200
ok(CountingGuard.calls <= GUARD_CALL_CEILING,
   "wheel guard is asked %d times to build one window (ceiling %d). App-wide "
   "this is every event in the process crossing into Python; on the 75 target "
   "widgets it should be a rounding error"
   % (CountingGuard.calls, GUARD_CALL_CEILING))

# ⚠⚠ **NO NEW FILTER MAY BE INSTALLED ON THE QApplication.** Three were, and
# each one charged the whole process for a handful of widgets: the wheel guard
# (380 ms of a window build), smooth scrolling (408 ms) and Developer mode:
# edit (397 ms — for a feature that is absent from every frozen build). They
# are invisible in a profile of any single tab, because the cost is spread
# across every widget in the app.
#
# Checked in the SOURCE rather than at runtime: Qt does not expose an
# application's installed filters, so there is nothing to ask. `devedit` is the
# one permitted install and it is gated behind `devedit.available()`, which is
# false in a frozen build.
_app_installs = []
for _fname in sorted(os.listdir(APP)):
    if not _fname.endswith(".py"):
        continue
    _src = io.open(os.path.join(APP, _fname), encoding="utf-8").read()
    for _line_no, _line in enumerate(_src.splitlines(), 1):
        if "app.installEventFilter(" in _line and not _line.strip().startswith("#"):
            _app_installs.append("%s:%d" % (_fname, _line_no))
ok(_app_installs == ["devedit.py:1072"],
   "the ONLY application-wide event filter left is devedit's, and main() "
   "installs it only when devedit.available() (found: %s). Anything else here "
   "taxes every event in the process" % (_app_installs,))

targets = []
for kind in (QComboBox, QAbstractSpinBox, QSlider, QTabBar):
    targets.extend(win.findChildren(kind))
print("target widgets needing the guard: %d of %d"
      % (len(targets), widget_count))

# ⚠ TIGHTENED 2000 → 1550 WHEN THE FIRST LAZY TAB LANDED (2026-08-15, option
# C: Anim Layers, −359 widgets). Tighten with every tab that moves; ~250 when
# only the open tab is built.
WIDGET_CEILING = 1550
ok(widget_count <= WIDGET_CEILING,
   "%d widgets exist after startup (ceiling %d). Both the stylesheet and the "
   "wheel guard are PER-WIDGET costs, so this number is the multiplier on "
   "everything else" % (widget_count, WIDGET_CEILING))

# Which tabs are paying for themselves? Printed, not asserted — the split is
# what tells you which tab to make lazy next.
print("widgets per section:")
rows = []
for i in range(win.main_tabs.count()):
    page = win.main_tabs.widget(i)
    rows.append((len(page.findChildren(QWidget)), win.main_tabs.tabText(i)))
for n, name in sorted(rows, reverse=True):
    print("    %5d  %s" % (n, name))
open_index = win.main_tabs.currentIndex()
unopened = sum(n for i, (n, _t) in enumerate(rows) if i != open_index)
print("    %5d  built but not open (%d%%)"
      % (unopened, unopened * 100 // max(widget_count, 1)))


# ====================================================== the library at scale
# ⚠ Counting READS, not seconds. `Item.read_data` opens the item's full json —
# its own docstring says "only call on selection, files can be big" — and the
# author dropdown calls it for every item in the library.
import library as librarymod  # noqa: E402

_real_read = librarymod.Item.read_data
_reads = {"n": 0}


def counted_read(self):
    _reads["n"] += 1
    return _real_read(self)


librarymod.Item.read_data = counted_read


def make_library(root, count):
    from PySide6.QtGui import QColor, QPixmap
    os.makedirs(root, exist_ok=True)
    thumb = QPixmap(256, 256)
    thumb.fill(QColor("#3a4048"))
    for i in range(count):
        item = os.path.join(root, "bench_%04d.pose" % i)
        os.makedirs(item, exist_ok=True)
        io.open(os.path.join(item, "pose.json"), "w", encoding="utf-8").write(
            json.dumps({"name": "b%04d" % i, "type": "pose", "bones": {},
                        "metadata": {"author": "bench_%d" % (i % 5)}}))
        thumb.save(os.path.join(item, "thumbnail.jpg"), "JPG", 80)


ITEMS = 200
lib_root = os.path.join(WORK, "scale")
make_library(lib_root, ITEMS)
folders, items = librarymod.scan(lib_root)
ok(len(items) == ITEMS,
   "the benchmark library really holds %d items (scan found %d) — a fixture "
   "that quietly builds nothing would make every number below meaningless"
   % (ITEMS, len(items)))

_reads["n"] = 0
t0 = time.perf_counter()
view = mainmod.LibraryView({"name": "Bench", "path": lib_root},
                           win.bridge, win)
lib_ms = (time.perf_counter() - t0) * 1000.0
reads = _reads["n"]
print("LibraryView over %d items: %.0f ms, %d full-item reads"
      % (ITEMS, lib_ms, reads))

# ⚠ **TIGHTENED FROM 210 TO 0 WHEN L1 LANDED** (2026-08-15). Opening a library
# must not read a single item's data file: the author dropdown fills itself
# when it is opened (`panels.AuthorCombo`) and every other consumer of `meta()`
# is already behind a filter the user has to switch on. One read per item here
# means somebody has put an eager `meta()` back on the scan path.
READ_CEILING = 0
ok(reads <= READ_CEILING,
   "opening a %d-item library does %d full-item reads (ceiling %d). These are "
   "the big jsons — Marty's poses carry 461 bones each, so the real cost is "
   "worse than this fixture shows" % (ITEMS, reads, READ_CEILING))

librarymod.Item.read_data = _real_read


# ===================================================== batch-1 ceilings (v2)
# ⚠ O1 — TIGHTENED 2026-08-15 (v2): the Optimization tab's poll used to start
# in __init__ and ask Blender for opt_status every 2.5 s FOR THE LIFE OF THE
# APP, from a tab that may never be opened. showEvent owns it now, so after a
# build (nothing shown) the timer must exist and be parked.
import optimizer as optmod  # noqa: E402

_adaptive = win.findChild(optmod.AdaptiveTool)
ok(_adaptive is not None and _adaptive.timer is not None
   and not _adaptive.timer.isActive(),
   "O1: the optimizer's opt_status poll is parked until its tab is shown "
   "(the one standing socket wake-up the first pass left behind)")

# ⚠ F1 — TIGHTENED 2026-08-15 (v2): a refilter flips row VISIBILITY; only a
# rescan may rebuild rows. Counted by wrapping set_items — every refilter
# rebuilt the whole grid before, 391 ms per search keystroke at 800 items.
import grid as gridmod  # noqa: E402

_set_calls = {"n": 0}
_real_set_items = gridmod.ItemGrid.set_items


def _counted_set_items(self, items):
    _set_calls["n"] += 1
    return _real_set_items(self, items)


gridmod.ItemGrid.set_items = _counted_set_items
view.refilter()
ok(_set_calls["n"] == 0 and view.grid.count() == ITEMS,
   "F1: refilter() rebuilds nothing — %d set_items calls, %d rows still in "
   "the grid" % (_set_calls["n"], view.grid.count()))

# The keystroke, driven through the REAL path: setText starts the debounce
# timer, the event loop fires it, refilter runs. Bypassing the timer here
# would leave the wiring untested (the focus-guard lesson: test the rule).
view.search.setText("bench_0001")
_deadline = time.perf_counter() + 3.0
while view._search_timer.isActive() and time.perf_counter() < _deadline:
    app.processEvents()
app.processEvents()
_visible = sum(not view.grid.item(i).isHidden()
               for i in range(view.grid.count()))
ok(_set_calls["n"] == 0 and _visible == 1,
   "F1: a real (debounced) keystroke filters to %d visible of %d rows with "
   "zero rebuilds" % (_visible, view.grid.count()))
view.search.setText("")
_deadline = time.perf_counter() + 3.0
while view._search_timer.isActive() and time.perf_counter() < _deadline:
    app.processEvents()
app.processEvents()
gridmod.ItemGrid.set_items = _real_set_items

# ===================================================== batch-2 ceilings (F2)
# ⚠ Decodes are counted at grid._decode_file — the module's single road to
# the disk. Ceilings, not exact counts: the synchronous batch is "visible
# rows + the first 64", and visible depends on default widget geometry.
_decode_calls = {"n": 0}
_real_decode_file = gridmod._decode_file


def _counted_decode_file(path):
    _decode_calls["n"] += 1
    return _real_decode_file(path)


gridmod._decode_file = _counted_decode_file

view2 = mainmod.LibraryView({"name": "Bench", "path": lib_root},
                            win.bridge, win)
_open_decodes = _decode_calls["n"]
ok(_open_decodes <= 80,
   "F2: opening a %d-item library reads %d thumbnails from disk (ceiling 80 "
   "— the visible+64 sync batch; it decoded ALL of them before, 886 ms at "
   "800 items)" % (ITEMS, _open_decodes))

view2.grid.flush_decodes()
_decode_calls["n"] = 0
view2.grid.set_icon_size(146)
view2.grid.flush_decodes()
view2.grid.set_icon_size(110)
view2.grid.flush_decodes()
ok(_decode_calls["n"] == 0,
   "F2: a warm zoom cycle touches the disk %d times (ceiling 0) — tiles are "
   "derived from the bytes/source layers in RAM; the old entry-capped cache "
   "re-decoded every file at every size once a library passed 512 items"
   % _decode_calls["n"])

ok(gridmod._bytes_bytes <= gridmod._BYTES_CACHE_MB * 1048576
   and gridmod._source_bytes <= gridmod._SOURCE_CACHE_MB * 1048576
   and gridmod._tile_bytes <= gridmod._TILE_CACHE_MB * 1048576
   and gridmod._placeholder_bytes <= gridmod._PLACEHOLDER_CACHE_MB * 1048576,
   "F2: every pixmap cache is inside its BYTE cap (files %.1f/%.0f MB, "
   "decoded %.1f/%.0f, tiles %.1f/%.0f, placeholders %.1f/%.0f) — capping by "
   "ENTRIES was quietly 95 MB of tiles at 220 px"
   % (gridmod._bytes_bytes / 1048576.0, gridmod._BYTES_CACHE_MB,
      gridmod._source_bytes / 1048576.0, gridmod._SOURCE_CACHE_MB,
      gridmod._tile_bytes / 1048576.0, gridmod._TILE_CACHE_MB,
      gridmod._placeholder_bytes / 1048576.0, gridmod._PLACEHOLDER_CACHE_MB))

gridmod._decode_file = _real_decode_file
view2.deleteLater()

# ⚠ M1 — the settings dialog must not import addon_bundle: 38,559 lines and
# +4.1 MB resident, for one version string it reads from
# bridge.EXPECTED_ADDON_VERSION instead (pinned to the add-on source by
# al_panel_test and to the bundle by app_updater_test).
_dlg = mainmod.LibrarySettingsDialog(win, win.cfg)
_dlg.deleteLater()
ok("addon_bundle" not in sys.modules,
   "M1: building the settings dialog does not import addon_bundle (4.1 MB "
   "resident for a version string the bridge constant already carries)")

# ===================================================== batch-3 ceilings (C+D)
# ⚠ The first lazy tab (Anim Layers). Its modules must not even be IMPORTED
# by a startup that never opens it (option D moved them inside the builder),
# and opening it through the real path must build the page — lazy must never
# mean broken. verify_exe.py proves the frozen build still ships the modules.
ok("anim_layers" not in sys.modules and "markers" not in sys.modules,
   "C/D: anim_layers and markers are not imported at startup (their import "
   "cost moved inside _build_anim_layers)")
ok(win.anim_layers is None,
   "C: the Anim Layers page does not exist after startup (its 359 widgets "
   "were a fifth of the whole window)")
_before_c = len(win.findChildren(QWidget))
_al_index = next(i for i in range(win.main_tabs.count())
                 if win.main_tabs.tabText(i) == "Anim Layers")
win.main_tabs.setCurrentIndex(_al_index)
_after_c = len(win.findChildren(QWidget))
ok(win.anim_layers is not None and win.layers_page is not None
   and win.markers_tool is not None and "anim_layers" in sys.modules,
   "C: first open builds the real page (+%d widgets, imports included)"
   % (_after_c - _before_c))


# ============================================================== linecache
cached = sum(sum(len(line) for line in entry[2])
             for entry in linecache.cache.values() if len(entry) == 4)
print("linecache holds %.1f MB of source text" % (cached / 1048576.0))
# ⚠ TIGHTEN TO ~1.0 when the quick set lands (clearcache after startup).
LINECACHE_CEILING_MB = 6.0
ok(cached / 1048576.0 <= LINECACHE_CEILING_MB,
   "linecache holds %.1f MB of source text (ceiling %.1f) — pure overhead "
   "unless a traceback is being formatted"
   % (cached / 1048576.0, LINECACHE_CEILING_MB))

# ⚠ The release itself happens in `main()`, which this suite never runs (it
# builds a MainWindow directly), so the number above is what an UNRELEASED
# process holds. Checked in the source instead, or the win would be silently
# deletable while this suite went on passing.
_main_src = io.open(os.path.join(APP, "main.py"), encoding="utf-8").read()
ok("linecache.clearcache()" in _main_src,
   "main() releases the cached source text once the window is up (~3.6 MB, "
   "and it refills itself if a traceback is ever formatted)")

print()
print("---- summary (timings are FYI; the checks above are on work done) ----")
print("  import main        %7.0f ms  %+6.1f MB" % (import_ms, import_mb))
print("  MainWindow         %7.0f ms  %+6.1f MB" % (build_ms, build_mb))
print("  LibraryView (%3d)  %7.0f ms" % (ITEMS, lib_ms))
print("  RSS now            %7.0f MB" % rss())

print("%d passed, %d failed" % (len(PASS), len(FAIL)))
sys.exit(1 if FAIL else 0)
