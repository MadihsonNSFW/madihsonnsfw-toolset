# Render presets engine (blender_addon\madi_anim_library\renderpresets.py):
# catalogue integrity and the ORDER inside it, capture -> JSON -> apply round
# trips, the whitelist that stops an edited preset file writing anything it
# likes, per-path failures that do not take the apply down, and the int/float
# coercion a JSON round trip forces.
#
# ⚠ The three ordering checks are the ones that would catch a real regression:
# `render.engine` first, `media_type` before `file_format`, and
# `display_device` -> `view_transform` -> `look`. Each of those pairs raises on
# a perfectly good preset if written the other way round, and the failure looks
# like a bad preset rather than a bad loop.
#
# Run: blender.exe -b --factory-startup --python render_presets_test.py
import importlib.util
import json
import os
import sys

import bpy

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

MOD = os.path.join(_ROOT, "blender_addon", "madi_anim_library", "renderpresets.py")
spec = importlib.util.spec_from_file_location("madi_renderpresets", MOD)
rp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rp)

PASS = []
FAIL = []


def ok(cond, label):
    (PASS if cond else FAIL).append(label)
    print(("ok   " if cond else "FAIL ") + label, flush=True)


scene = bpy.context.scene


def index_of(paths, path):
    return paths.index(path) if path in paths else -1


# ========================================================== catalogue =====
keys = [g["key"] for g in rp.GROUPS]
ok(len(keys) == len(set(keys)), "group keys are unique")
ok(all(set(g) >= {"key", "label", "default", "paths"} for g in rp.GROUPS),
   "every group has key/label/default/paths")
ok(all(g["paths"] for g in rp.GROUPS), "no group is empty")

all_paths = [p for g in rp.GROUPS for p in g["paths"]]
ok(len(all_paths) == len(set(all_paths)),
   "no path appears in two groups (a duplicate would be applied twice)")
ok(len(all_paths) > 100, "the catalogue is the real thing (%d paths)"
   % len(all_paths))
ok(set(rp._PATH_GROUP) == set(all_paths),
   "the whitelist is exactly the catalogue")
ok(all(p in rp._PATH_GROUP for p in rp._PRIORITY),
   "every priority path is also captured by a group")

out_paths = next(g["paths"] for g in rp.GROUPS if g["key"] == "output")
ok(0 <= index_of(out_paths, "render.image_settings.media_type")
   < index_of(out_paths, "render.image_settings.file_format"),
   "output group: media_type is listed before file_format")
col_paths = next(g["paths"] for g in rp.GROUPS if g["key"] == "color")
ok(0 <= index_of(col_paths, "display_settings.display_device")
   < index_of(col_paths, "view_settings.view_transform")
   < index_of(col_paths, "view_settings.look"),
   "colour group: display device -> view transform -> look")

ok([g["key"] for g in rp.GROUPS if not g["default"]] == ["filepath", "frames"],
   "only the output path and the frame range start unticked")

# --- _ordered puts the priority paths first however they arrive ------------
shuffled = ["view_settings.look", "render.image_settings.file_format",
            "cycles.samples", "render.engine",
            "render.image_settings.media_type",
            "view_settings.view_transform"]
order = rp._ordered(shuffled)
ok(order[0] == "render.engine", "_ordered: the engine is written first")
ok(order.index("render.image_settings.media_type")
   < order.index("render.image_settings.file_format"),
   "_ordered: media_type beats file_format whatever order it came in")
ok(order.index("view_settings.view_transform")
   < order.index("view_settings.look"),
   "_ordered: the view transform beats the look")
ok(sorted(order) == sorted(shuffled), "_ordered loses nothing")
ok(rp._ordered(["render.nonsense"]) == ["render.nonsense"],
   "_ordered still passes an unknown path through, so apply can reject it")

# ============================================================= schema =====
schema = rp.schema()
ok(len(schema["groups"]) == len(rp.GROUPS), "schema lists every group")
ok(all(g["count"] > 0 for g in schema["groups"]), "schema counts the paths")
by_key = {g["key"]: g for g in schema["groups"]}
ok(by_key["frames"]["default"] is False and by_key["format"]["default"] is True,
   "schema carries the default tick state")
ok(by_key["output"]["label"] == "Output & file format", "schema carries labels")

# ============================================================ resolve =====
ok(rp._resolve(scene, "render.resolution_x") is not None,
   "_resolve finds a real path")
ok(rp._resolve(scene, "render.no_such_property") is None,
   "_resolve returns None for a missing leaf")
ok(rp._resolve(scene, "nonesuch.thing") is None,
   "_resolve returns None for a missing owner (this is how a scene with no "
   "Cycles skips every cycles.* path)")

ok(rp._coerce(5, 5.0) == 5 and isinstance(rp._coerce(5, 5.0), int),
   "_coerce: a float from JSON becomes an int for an int property")
ok(rp._coerce(True, 1) is True, "_coerce: bool before int")
ok(rp._coerce(1.5, 2) == 2.0 and isinstance(rp._coerce(1.5, 2), float),
   "_coerce: an int becomes a float for a float property")

# ============================================================ capture =====
scene.render.resolution_x = 1234
scene.render.resolution_y = 567
cap = rp.capture()
ok(set(cap) >= {"blender", "scene", "engine", "groups", "skipped"},
   "capture reports blender/scene/engine/groups/skipped")
ok(len(cap["groups"]) == len(rp.GROUPS), "capture reads every group by default")
fmt = cap["groups"]["format"]["values"]
ok(fmt["render.resolution_x"] == 1234 and fmt["render.resolution_y"] == 567,
   "capture reads the live values")
ok(cap["groups"]["format"]["label"] == "Resolution & frame rate",
   "capture carries the label, so an old preset file still reads well")

narrow = rp.capture(groups=["format"])
ok(list(narrow["groups"]) == ["format"], "capture honours the group filter")

flat = [v for g in cap["groups"].values() for v in g["values"].values()]
ok(flat and all(isinstance(v, (bool, int, float, str)) for v in flat),
   "every captured value is a JSON-simple type")
try:
    round_tripped = json.loads(json.dumps(cap))
    ok(True, "a capture survives a JSON round trip (that is the file format)")
except (TypeError, ValueError) as exc:
    round_tripped = None
    ok(False, "a capture survives a JSON round trip: %s" % exc)

# --- a path this Blender does not have is skipped, not fatal ---------------
rp.GROUPS.append({"key": "_probe", "label": "Probe", "default": True,
                  "paths": ["render.definitely_not_a_property"]})
rp._PATH_GROUP["render.definitely_not_a_property"] = "_probe"
probe = rp.capture(groups=["_probe"])
ok(probe["groups"]["_probe"]["values"] == {},
   "a missing property is left out of the values")
ok(any(s["path"] == "render.definitely_not_a_property"
       for s in probe["skipped"]), "...and reported in skipped")

# =============================================================== apply =====
scene.render.resolution_x = 999
report = rp.apply(round_tripped, groups=["format"])
ok(scene.render.resolution_x == 1234,
   "apply writes the stored value back onto the scene")
ok("render.resolution_x" in report["applied"], "...and reports it as applied")
ok(report["failed"] == [] and report["rejected"] == [],
   "a clean preset has nothing failed or rejected")

again = rp.apply(round_tripped, groups=["format"])
ok("render.resolution_x" in again["unchanged"] and not again["applied"],
   "applying the same preset twice changes nothing the second time")

ok(rp.apply(round_tripped, groups=["format"])["summary"].startswith(
    "0 settings changed"), "the summary counts what actually moved")

# --- the whitelist ---------------------------------------------------------
# `scene.name` is writable, reachable from the scene, and NOT in the
# catalogue. If the rejection ever regresses, this renames the scene.
before = scene.name
forged = {"groups": {"format": {"label": "x", "values": {
    "name": "HACKED", "render.resolution_x": 640}}}}
forged_report = rp.apply(forged)
ok(scene.name == before,
   "a path outside the catalogue is NOT written, however it got into the file")
ok(forged_report["rejected"] == ["name"], "...and is reported as rejected")
ok(scene.render.resolution_x == 640,
   "...while the legitimate setting beside it still lands")

# --- one bad value does not take the apply down ----------------------------
bad = {"groups": {"output": {"label": "x", "values": {
    "render.image_settings.file_format": "NOT_A_FORMAT",
    "render.image_settings.color_mode": "RGBA"}}}}
bad_report = rp.apply(bad)
ok(any(f["path"] == "render.image_settings.file_format"
       for f in bad_report["failed"]),
   "a bad enum value is reported as failed, not raised")
ok(scene.render.image_settings.color_mode == "RGBA",
   "...and the setting after it in the same group still applied")

# --- media_type first, live ------------------------------------------------
scene.render.image_settings.media_type = 'VIDEO'
scene.render.image_settings.file_format = 'FFMPEG'
still = {"groups": {"output": {"label": "x", "values": {
    # deliberately the wrong way round in the dict; _ordered fixes it
    "render.image_settings.file_format": "PNG",
    "render.image_settings.media_type": "IMAGE"}}}}
still_report = rp.apply(still)
ok(scene.render.image_settings.file_format == "PNG"
   and still_report["failed"] == [],
   "a video->stills preset applies because media_type goes first")

# --- the engine ------------------------------------------------------------
# ⚠ NOT asserted against `bl_rna.properties['engine'].enum_items`: that list
# reports only BLENDER_EEVEE even while the scene sits on CYCLES, because
# Cycles registers itself as a RenderEngine rather than as an enum item
# (probed on Marty's 5.2 file, 2026-08-05). Validating a preset's engine
# against it would refuse every Cycles preset. So the contract is the write.
items = [i.identifier
         for i in bpy.types.RenderSettings.bl_rna.properties["engine"].enum_items]
print("   (engine enum_items reports: %s)" % ", ".join(items), flush=True)
if hasattr(scene, "cycles"):
    eng = {"groups": {"engine": {"label": "x", "values": {
        "render.engine": "CYCLES"}}}}
    eng_report = rp.apply(eng)
    ok(scene.render.engine == "CYCLES" and not eng_report["failed"],
       "the engine is set by writing it, never by checking enum_items first")
    cyc = rp.capture(groups=["sampling"])["groups"]["sampling"]["values"]
    ok("cycles.samples" in cyc, "the Cycles groups capture when Cycles is on")
else:
    ok(False, "Cycles is expected to be available in factory startup")

# --- an empty / junk preset is harmless ------------------------------------
empty = rp.apply({})
ok(empty["applied"] == [] and empty["failed"] == [] and empty["rejected"] == [],
   "an empty preset does nothing at all")
ok(rp.apply({"groups": {"format": {}}})["applied"] == [],
   "a group with no values block does nothing")
ok("0 settings changed" in empty["summary"], "...and says so")

print("\n%d passed, %d failed" % (len(PASS), len(FAIL)), flush=True)
sys.exit(1 if FAIL else 0)
