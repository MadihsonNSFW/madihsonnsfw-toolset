# Export Abc options (Marty, 2026-08-05: "Add some options for Export ABC in
# studio library").
#
#   blender.exe -b --factory-startup --python tests\abc_export_test.py
#
# ⚠ THE SANITISER IS THE POINT OF THIS SUITE, not the checkbox count. These
# values go straight into `bpy.ops.wm.alembic_export` as keyword arguments, so
# a key it has never heard of, a string where it wants a float, or an enum
# value that is not in its list all take the WHOLE EXPORT down with a
# TypeError - losing a cache that may have taken minutes over a checkbox. Every
# bad-input case below is one that a newer app, an edited config.json or a
# hand-written bridge call can really produce.
import ast
import importlib.util
import json
import os
import shutil
import sys
import tempfile

import bpy

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

ROOT = _ROOT
ADDON = os.path.join(ROOT, "blender_addon", "madi_anim_library")
PASS, FAIL = [], []


def ok(cond, label):
    (PASS if cond else FAIL).append(label)
    print(("ok   " if cond else "FAIL ") + label, flush=True)


spec = importlib.util.spec_from_file_location(
    "madi_pkg", os.path.join(ADDON, "__init__.py"),
    submodule_search_locations=[ADDON])
pkg = importlib.util.module_from_spec(spec)
sys.modules["madi_pkg"] = pkg
spec.loader.exec_module(pkg)
core = sys.modules["madi_pkg.core"]

# --------------------------------------- the defaults are BLENDER'S, verified
props = bpy.ops.wm.alembic_export.get_rna_type().properties
mismatched = []
for key, ours in core.ABC_OPTIONS.items():
    prop = props.get(key)
    if prop is None:
        mismatched.append("%s: not an alembic_export property at all" % key)
        continue
    if key == "selected":
        continue                    # the one deliberate difference, documented
    theirs = prop.default
    if isinstance(ours, float):
        same = abs(float(theirs) - ours) < 1e-6
    else:
        same = theirs == ours
    if not same:
        mismatched.append("%s: ours=%r blender=%r" % (key, ours, theirs))
ok(not mismatched,
   "defaults: every option is a real alembic_export property AND carries "
   "Blender's own default (%s)" % ("; ".join(mismatched) or "all match"))
ok(core.ABC_OPTIONS["selected"] is True and props["selected"].default is False,
   "defaults: `selected` is the ONE deliberate difference - a library item has "
   "always exported the selection, and flipping that silently would turn "
   "'export this character' into 'export the whole scene'")

for key, allowed in core.ABC_ENUMS.items():
    real = tuple(i.identifier for i in props[key].enum_items)
    ok(tuple(allowed) == real,
       "defaults: %s's allowed values match Blender's (%s)" % (key, real))

# ---------------------------------------------------------- the sanitiser
ok(core.abc_options() == core.ABC_OPTIONS,
   "sanitise: nothing in means every default out")
ok(core.abc_options({}) == core.ABC_OPTIONS,
   "sanitise: an empty dict is the same as nothing")

out = core.abc_options({"vcolors": True, "global_scale": 2.5})
ok(out["vcolors"] is True and abs(out["global_scale"] - 2.5) < 1e-9,
   "sanitise: what is passed is taken")
ok(out["normals"] is True and out["evaluation_mode"] == "RENDER",
   "sanitise: and everything else keeps its default")

out = core.abc_options({"nonsense": True, "filepath": "/etc/passwd",
                        "as_background_job": True})
ok("nonsense" not in out and "filepath" not in out
   and "as_background_job" not in out,
   "sanitise: UNKNOWN KEYS ARE DROPPED - a newer app must not be able to hand "
   "the operator a keyword this Blender has never heard of, and must not be "
   "able to reach filepath or as_background_job either")

out = core.abc_options({"uvs": "yes please", "xsamples": "lots"})
ok(out["uvs"] is True,
   "sanitise: a string for a checkbox is coerced, not passed through (%r)"
   % out["uvs"])
ok(out["xsamples"] == 1,
   "sanitise: an uncoercible number falls back to the default rather than "
   "failing the export over a field")

out = core.abc_options({"quad_method": "NOT_A_METHOD",
                        "evaluation_mode": "SIDEWAYS"})
ok(out["quad_method"] == "SHORTEST_DIAGONAL"
   and out["evaluation_mode"] == "RENDER",
   "sanitise: a bogus enum value falls back - the operator RAISES on one")
ok(core.abc_options({"evaluation_mode": "VIEWPORT"})["evaluation_mode"]
   == "VIEWPORT", "sanitise: and a real one is honoured")

out = core.abc_options({"global_scale": 99999.0, "xsamples": 9999,
                        "sh_open": -40.0, "gsamples": 0})
ok(abs(out["global_scale"] - 1000.0) < 1e-6 and out["xsamples"] == 128
   and abs(out["sh_open"] + 1.0) < 1e-6 and out["gsamples"] == 1,
   "sanitise: out-of-range numbers are CLAMPED to what the operator accepts "
   "(scale=%s xsamples=%s sh_open=%s gsamples=%s)"
   % (out["global_scale"], out["xsamples"], out["sh_open"], out["gsamples"]))

# ⚠ isinstance(True, int) is True in Python, so a bool checked after int comes
# back as 0/1 and the operator refuses it.
out = core.abc_options({"uvs": False, "xsamples": 4})
ok(out["uvs"] is False and isinstance(out["uvs"], bool),
   "sanitise: a checkbox stays a BOOL, not 0/1 - bool has to be tested before "
   "int or every one of them turns into a number")
ok(isinstance(out["xsamples"], int) and not isinstance(out["xsamples"], bool),
   "sanitise: and a count stays an int")
ok(core.abc_options({"selected": False})["selected"] is False,
   "sanitise: whole-scene export is reachable")

# ------------------------------------------------------ a real export, twice
TMP = tempfile.mkdtemp(prefix="madi_abc_test_")
bpy.ops.wm.read_homefile(use_empty=True)
bpy.ops.mesh.primitive_cube_add()
cube = bpy.context.active_object
cube.name = "Hero"
cube.keyframe_insert("location", frame=1)
cube.location.x = 4.0
cube.keyframe_insert("location", frame=3)
bpy.ops.mesh.primitive_uv_sphere_add(location=(10, 0, 0))
extra = bpy.context.active_object
extra.name = "Bystander"
bpy.ops.object.select_all(action='DESELECT')
cube.select_set(True)
bpy.context.view_layer.objects.active = cube

res = core.save_abc(TMP, "", "hero_cache", frame_start=1, frame_end=3,
                    options={"vcolors": True, "global_scale": 2.0,
                             "evaluation_mode": "VIEWPORT"})
cache = os.path.join(res["path"], "cache.abc")
ok(os.path.isfile(cache) and os.path.getsize(cache) > 0,
   "export: a real .abc came out with non-default options (%d bytes)"
   % os.path.getsize(cache))
ok(res["objects"] == 1,
   "export: selected-only exported just the one object (%d)" % res["objects"])

with open(os.path.join(res["path"], "abc.json"), "r", encoding="utf-8") as f:
    meta = json.load(f)["metadata"]
ok(meta["options"]["vcolors"] is True
   and abs(meta["options"]["global_scale"] - 2.0) < 1e-9
   and meta["options"]["evaluation_mode"] == "VIEWPORT",
   "export: the options USED are recorded in abc.json - a cache that came out "
   "wrong can be read rather than guessed at")
ok(meta["options"]["normals"] is True,
   "export: including the ones that were left at their default")
ok(meta["objects"] == ["Hero"], "export: and which objects went in")

# ⚠ Whole-scene export has no single source object, and `_metadata` used to
# take `sel[0]` unconditionally - which is an IndexError with nothing selected.
bpy.ops.object.select_all(action='DESELECT')
res = core.save_abc(TMP, "", "whole_scene", frame_start=1, frame_end=2,
                    options={"selected": False})
ok(os.path.isfile(os.path.join(res["path"], "cache.abc")),
   "export: a WHOLE-SCENE export works with nothing selected at all")
ok(res["objects"] == 2,
   "export: and reports both objects (%d)" % res["objects"])

try:
    core.save_abc(TMP, "", "nope", frame_start=1, frame_end=2)
    refused = ""
except RuntimeError as exc:
    refused = str(exc)
ok("Nothing selected" in refused,
   "export: but selected-only with nothing selected still refuses (%r)"
   % refused)

shutil.rmtree(TMP, ignore_errors=True)

# ------------------------------------------- the app's copy must not drift
# ⚠ The dialog cannot be imported here (it needs PySide6), so the four tables
# it is built from are read out of main.py with `ast`. Without this the app
# could show one default while Blender used another, and nothing would say so.
tables = {}
with open(os.path.join(ROOT, "app", "main.py"), "r", encoding="utf-8") as f:
    tree = ast.parse(f.read())
for node in tree.body:
    if not isinstance(node, ast.Assign) or len(node.targets) != 1:
        continue
    target = node.targets[0]
    if isinstance(target, ast.Name) and target.id.startswith("ABC_"):
        try:
            tables[target.id] = ast.literal_eval(node.value)
        except ValueError:
            pass

app_defaults = {}
for _title, rows in tables.get("ABC_OPTION_GROUPS", ()):
    for key, _label, default, _tip in rows:
        app_defaults[key] = default
app_defaults.update(tables.get("ABC_NUMBERS", {}))
app_defaults.update(tables.get("ABC_EXTRA_DEFAULTS", {}))
app_defaults.update(tables.get("ABC_CHOICE_DEFAULTS", {}))

ok(bool(app_defaults),
   "parity: the app's option tables were found in main.py (%d entries)"
   % len(app_defaults))
ok(set(app_defaults) == set(core.ABC_OPTIONS),
   "parity: the dialog offers EXACTLY the options the add-on accepts "
   "(app-only=%s addon-only=%s)"
   % (sorted(set(app_defaults) - set(core.ABC_OPTIONS)),
      sorted(set(core.ABC_OPTIONS) - set(app_defaults))))
differing = [k for k, v in app_defaults.items()
             if (abs(v - core.ABC_OPTIONS[k]) > 1e-9
                 if isinstance(v, float) else v != core.ABC_OPTIONS[k])]
ok(not differing,
   "parity: and with the same defaults, so the dialog cannot show one thing "
   "while Blender does another (%s)" % (differing or "all match"))
for key, allowed in tables.get("ABC_CHOICES", {}).items():
    ok(tuple(allowed) == tuple(core.ABC_ENUMS[key]),
       "parity: the %s picker lists exactly what the add-on will accept" % key)

print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
for label in FAIL:
    print("  FAILED: " + label)
sys.exit(1 if FAIL else 0)
