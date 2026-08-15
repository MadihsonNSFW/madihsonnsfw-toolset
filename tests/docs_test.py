# The docs police themselves (2026-08-05).
#
#   python tests\docs_test.py
#
# WHY: on 2026-08-05 the regression count lived in six files and was
# search-and-replaced five times in one day; the add-on version was stale in
# four places at once; and HANDOFF — the file whose own first instruction is
# "keep context lean" — had grown to 1076 lines of which 85% was history.
# None of that is a coding mistake anyone would catch by reading. It is exactly
# the sort of thing a machine should check, so it does.
#
# Every assertion here failed at least once against the repo as it stood that
# morning. Nothing is hypothetical.
import importlib.util
import io
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCS = os.path.join(ROOT, "docs")
HANDOFF = os.path.join(ROOT, "HANDOFF.md")
RESUME = os.path.join(ROOT, "RESUME_PROMPT.md")

PASS, FAIL = [], []


def ok(cond, label):
    (PASS if cond else FAIL).append(label)
    print(("ok   " if cond else "FAIL ") + label, flush=True)


def read(path):
    with io.open(path, encoding="utf-8") as fh:
        return fh.read()


handoff = read(HANDOFF)
resume = read(RESUME)
docs = sorted(n for n in os.listdir(DOCS) if n.endswith(".md"))

# ===================================================== the router routes =====
# Generated / narrative docs are not routed TO by task, but must still be
# named somewhere in HANDOFF so nobody has to discover them by listing a folder.
for name in docs:
    ok(name in handoff, "HANDOFF names docs\\%s" % name)

ok("## FILE → DOC map" in handoff, "HANDOFF carries a FILE -> DOC map")
ok("PITFALLS.md" in handoff and "history.md" in handoff,
   "...and points at the two indexes")

# ------- every module is in the map ------------------------------------------
# Generated or data-only files are exempt: nobody debugs into them.
GENERATED = {"addon_bundle.py", "nsfw_spec.py", "__init__.py"}
modules = [n for n in os.listdir(os.path.join(ROOT, "app"))
           if n.endswith(".py") and n not in GENERATED]
addon_dir = os.path.join(ROOT, "blender_addon", "madi_anim_library")
modules += [n for n in os.listdir(addon_dir)
            if n.endswith(".py") and n not in GENERATED]
missing = [m for m in modules if m not in handoff]
ok(not missing,
   "every source module appears in HANDOFF's FILE -> DOC map (missing: %r)"
   % missing)

# ============================================== the router stays a router ====
# ⚠ 1076 lines is what this cap exists to prevent. A router that has to be
# skim-read is not a router.
lines = handoff.count("\n") + 1
ok(lines <= 280,
   "⚠ HANDOFF is still a ROUTER: %d lines (cap 280 — it hit 1076 before the "
   "history was split out)" % lines)
rlines = resume.count("\n") + 1
ok(rlines <= 90,
   "⚠ RESUME_PROMPT is still a PROMPT: %d lines (cap 90 — it hit 481, a second "
   "changelog in second person)" % rlines)

hist = os.path.join(DOCS, "history.md")
ok(os.path.isfile(hist), "the history lives in docs\\history.md")
ok(read(hist).count("\n") > 400,
   "...and it really is the bulk of what HANDOFF used to carry")

# ==================================== volatile numbers live in ONE place =====
# ⚠ Anchor on the ▣ HEADING, not the phrase — prose above the block ("every
# version and count is in CURRENT STATE") made a bare find() slice the intro
# instead of the table, and three checks failed on a healthy file (2026-08-07).
state = handoff[handoff.find("## ▣ CURRENT STATE"):]
state = state[:state.find("\n## ", 10)]
ok("CURRENT STATE" in handoff and "| Fact | Value |" in state,
   "HANDOFF has the CURRENT STATE table")


def only_place(pattern, what, unit):
    """The number in CURRENT STATE must appear in no other doc.

    ⚠ MATCHED WITH ITS UNIT, not as bare digits. A plain substring search for
    the value cries wolf the moment any doc mentions that number about anything
    else — which happened the day the marker count reached 128, colliding with
    `optimizer.md`'s "128 px" textures and `updater.md`'s "128 checks" for p5.
    A freshness test that fails for unrelated reasons is one people start
    ignoring, so it has to be precise about which FACT is being restated.
    """
    match = re.search(pattern, state)
    if not match:
        ok(False, "CURRENT STATE states the %s" % what)
        return
    value = match.group(1)
    restated = re.compile(r"\b%s\b[^\S\n]*(?:\*\*)?[^\S\n]*%s" % (value, unit))
    elsewhere = []
    for name in docs + ["..\\RESUME_PROMPT.md", "..\\tests\\README.md"]:
        # history.md is a frozen record: its old numbers are correct FOR THEIR
        # DATE and must not be rewritten.
        if name in ("history.md", "PITFALLS.md"):
            continue
        if name.endswith("RESUME_PROMPT.md"):
            path = RESUME
        elif name.startswith(".."):
            path = os.path.join(ROOT, "tests", "README.md")
        else:
            path = os.path.join(DOCS, name)
        if restated.search(read(path)):
            elsewhere.append(name)
    ok(not elsewhere,
       "⚠ the %s (%s) is in CURRENT STATE and NOWHERE else (also in: %r)"
       % (what, value, elsewhere))


only_place(r"\*\*(\d{4}) checks", "regression check count", r"checks?\b")
only_place(r"\*\*(\d{2,4}) markers\*\*", "verify_exe marker count", r"markers?\b")

# ------- HANDOFF prose carries no version of OURS ----------------------------
# ⚠ Added 2026-08-07, the day the intro paragraph was found still saying
# "APP_VERSION 1.0.2 ... NINE outer tabs ... only three gated" while CURRENT
# STATE, fifteen lines below it, was right. Prose versions escaped only_place()
# because they carry no unit. Our versions all start 0. (add-on) or 1.
# (app/exe); Blender's own (5.2.0) and PySide6's (6.11.1) are environment
# facts and deliberately still allowed. Widen the regex when APP_VERSION
# reaches 2.x.
cs_start = handoff.find("## ▣ CURRENT STATE")
cs_end = handoff.find("\n## ", cs_start + 10)
outside = handoff[:cs_start] + handoff[cs_end:]
stray = sorted(set(re.findall(r"\b[01]\.\d+\.\d+\b", outside)))
ok(not stray,
   "⚠ no add-on/app version literal outside CURRENT STATE in HANDOFF "
   "(found: %r)" % stray)
ok("APP_VERSION" not in outside,
   "⚠ HANDOFF prose never names APP_VERSION (CURRENT STATE and version.py "
   "own it)")

# ================================================ every doc is labelled ======
for name in docs:
    if name in ("PITFALLS.md",):
        continue
    text = read(os.path.join(DOCS, name))
    ok(text.startswith("# "), "docs\\%s opens with a title" % name)
    ok("Master: `..\\HANDOFF.md`" in text or "Master: `..\\\\HANDOFF.md`" in text,
       "docs\\%s points back at the router" % name)
    if name != "history.md":
        ok("Last updated" in text or "Status:" in text,
           "docs\\%s says how current it is" % name)

# ======================== every path a doc names must EXIST (2026-08-14) =====
# The audit that added this found: a FILE -> DOC map row whose file had moved,
# two paths mangled into literal control bytes, and doc references to modules
# that had been renamed. A doc that points at a file that is not there teaches
# the next session a wrong name - and Bridge._explain turns wrong names into
# "your add-on is too old", which has already cost a wrong diagnosis once.
#
# ⚠ TUNED AGAINST CRYING WOLF, deliberately: only backtick-quoted references
# with a real file extension (or a trailing backslash = a folder) are checked;
# placeholders (<>, *, ?, {}, …) are prose; history.md and PITFALLS.md are
# frozen/generated records whose old paths were right for their date.
WORKSPACE = os.path.dirname(ROOT)
PATHY = re.compile(r"`((?:\.\.\\)*(?:app|tools|tests|docs|blender_addon|"
                   r"license-server|specs|assets)\\[^`*\n]+?)`")
CHECKED_EXT = (".py", ".md", ".ps1", ".js", ".json", ".toml", ".zip", ".exe",
               ".bat", ".ico", ".blend")
ATTR = re.compile(r"^((?:\.\.\\)*(?:app|blender_addon)\\[\w\\-]+)\.(\w+)(\(\))?$")
MODULE_DIRS = ["app", "app\\updater", "app\\licensing", "app\\madiref",
               "app\\render_deck", "blender_addon\\madi_anim_library"]


def _module_source(modname):
    for d in MODULE_DIRS:
        p = os.path.join(ROOT, *d.split("\\"), modname + ".py")
        if os.path.isfile(p):
            return read(p)
    return None


def _resolves(ref, hint):
    rel = ref.replace("..\\", "")
    for base in (hint, ROOT, WORKSPACE,
                 os.path.join(WORKSPACE, "license-server")):
        if base and os.path.exists(os.path.join(base, *rel.split("\\"))):
            return True
    return False


_doc_sources = [("HANDOFF.md", HANDOFF, ROOT),
                ("RESUME_PROMPT.md", RESUME, ROOT),
                ("tests\\README.md", os.path.join(ROOT, "tests", "README.md"),
                 os.path.join(ROOT, "tests"))]
_doc_sources += [("docs\\" + n, os.path.join(DOCS, n), ROOT)
                 for n in docs if n not in ("history.md", "PITFALLS.md")]

_path_bad = []
_ctrl_bad = []
for _label, _path, _hint in _doc_sources:
    _text = read(_path)
    # ⚠ Control bytes: three separate docs were found carrying them, all from
    # `\t`/`\a`/`\202` in a path being interpreted by some tool along the way.
    # A path with a TAB in it silently fails every search anyone runs for it.
    for _ch in _text:
        if ord(_ch) < 32 and _ch != "\n":
            _ctrl_bad.append("%s (0x%02x)" % (_label, ord(_ch)))
            break
    for _ref in set(PATHY.findall(_text)):
        _clean = _ref.strip().rstrip("\\")
        if any(c in _clean for c in "<>*?{}…"):
            continue
        _m = ATTR.match(_clean)
        if _m and not _clean.lower().endswith(CHECKED_EXT):
            # `app\bridge.EXPECTED_ADDON_VERSION` style: module must exist and
            # carry the name. Renaming either breaks the reference.
            _src = _module_source(_m.group(1).split("\\")[-1])
            if _src is not None and _m.group(2) not in _src:
                _path_bad.append("%s -> `%s`" % (_label, _clean))
            continue
        if not (_clean.lower().endswith(CHECKED_EXT) or _ref.endswith("\\")):
            continue
        if not _resolves(_clean, _hint):
            _path_bad.append("%s -> `%s`" % (_label, _clean))

ok(not _ctrl_bad,
   "⚠ no doc carries a control byte — a TAB inside a path came from `\\t` "
   "being interpreted, and that path fails every search (%r)" % _ctrl_bad)
ok(not _path_bad,
   "⚠ every file a doc names EXISTS (and every `module.ATTR` reference names "
   "a real attribute) — stale: %r" % _path_bad[:8])

# =================== every run_all suite has a tests\README.md row ===========
# The README's own warning: twelve suites accumulated without rows and a doc
# sweep found them, not a test. Now a test does. Exact backticked name, or a
# range row like `al_bake_test6-9.py` (matched by the digit-stripped stem).
_run_all = read(os.path.join(ROOT, "tests", "run_all.ps1"))
_readme = read(os.path.join(ROOT, "tests", "README.md"))
_suites = sorted(set(re.findall(r'"(\w+\.py)"', _run_all)) - {"main.py"})
_rowless = []
for _s in _suites:
    if "`%s`" % _s in _readme:
        continue
    _stem = re.sub(r"\d+\.py$", "", _s)
    if _stem != _s and _stem in _readme:
        continue
    _rowless.append(_s)
ok(len(_suites) > 60, "run_all.ps1 parsed (%d suites)" % len(_suites))
ok(not _rowless,
   "⚠ every suite in run_all.ps1 has a tests\\README.md row (missing: %r — "
   "a suite without a row is invisible to anyone reading the README)"
   % _rowless)

# ============ the FILE -> DOC map covers the SUBPACKAGES too =================
# The top-level module check above predates the subpackages; their modules are
# covered by FOLDER rows, so assert the folder rows exist rather than listing
# forty files.
_map_at = handoff.find("## FILE → DOC map")
_map_text = handoff[_map_at:handoff.find("\n## ", _map_at + 10)]
for _folder in ("app\\updater\\", "app\\licensing\\", "app\\madiref\\",
                "app\\render_deck\\"):
    ok(_folder in _map_text,
       "the FILE -> DOC map carries the folder row `%s`" % _folder)

# ==================================================== PITFALLS is fresh ======
spec = importlib.util.spec_from_file_location(
    "gen_pitfalls", os.path.join(ROOT, "tools", "gen_pitfalls.py"))
gen = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gen)

expected = gen.build()
actual = read(os.path.join(DOCS, "PITFALLS.md"))
ok(expected == actual,
   "⚠ docs\\PITFALLS.md is up to date — run `python tools\\gen_pitfalls.py` "
   "(a hand-maintained index of warnings is exactly the thing that silently "
   "stops matching what it indexes)")
ok(actual.count("\n- **L") > 200,
   "...and it really indexes the warnings (%d entries)"
   % actual.count("\n- **L"))
ok("GENERATED" in actual.split("\n")[1],
   "...and says it is generated, on the second line")

print("\n%d passed, %d failed" % (len(PASS), len(FAIL)), flush=True)
sys.exit(1 if FAIL else 0)
