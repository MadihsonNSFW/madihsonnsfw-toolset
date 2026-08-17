# The Scene Optimizer, inside Blender: the stand-in cache, adaptive sizing from
# the camera, decimation, the memory estimate, and the licence gate.
#
#   blender.exe -b --factory-startup --python tests\optimizer_test.py
#
# Real files, real OpenImageIO, real projection maths. Nothing here is mocked
# except the licence: the whole point of this module is what it does to files on
# disk, and a test that stubbed the resize would prove nothing about the one
# thing that can destroy someone's textures.
#
# ⚠ NOTHING IS WRITTEN OUTSIDE A TEMP FOLDER, and no image in this file comes
# from Marty's library.
import importlib.util
import os
import shutil
import sys
import tempfile

import bpy
from mathutils import Vector

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

sys.path.insert(0, os.path.join(_ROOT, "tests"))
import _branding  # noqa: E402

_FORBIDDEN, _STUDIED = _branding.words(_ROOT)

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
opt = sys.modules["madi_pkg.optimizer"]
core = sys.modules["madi_pkg.core"]
class _NoEntitlement:
    """⚠ THERE IS NO ENTITLEMENT MODULE ANY MORE (add-on 0.47.0).

    This suite's whole point in the gating era was to drive the gate LOCKED and
    prove the tool still worked. The gate is gone, so `unlocked()` is
    permanently true and `_STATE.update` is a no-op that keeps the surrounding
    checks readable rather than deleting the scenarios they cover.
    """

    _STATE = {}

    @staticmethod
    def unlocked():
        return True

    @staticmethod
    def lock(*_a, **_k):
        return {"unlocked": True}


ent = _NoEntitlement()
ent._STATE = type("_S", (), {"update": staticmethod(lambda **_k: None)})()

server = sys.modules["madi_pkg.server"]

TMP = tempfile.mkdtemp(prefix="madi_opt_test_")
SRC = os.path.join(TMP, "textures")
CACHE = os.path.join(TMP, "cache")
os.makedirs(SRC)


def make_png(name, width, height):
    import OpenImageIO as oiio
    path = os.path.join(SRC, name)
    folder = os.path.dirname(path)
    if not os.path.isdir(folder):
        os.makedirs(folder)
    buf = oiio.ImageBuf(oiio.ImageSpec(width, height, 3, "uint8"))
    oiio.ImageBufAlgo.fill(buf, (0.3, 0.6, 0.9))
    buf.write(path)
    return path


# =========================================================== versions / wiring
with open(os.path.join(ADDON, "blender_manifest.toml"), encoding="utf-8") as fh:
    manifest = fh.read()
ok('version = "%s"' % core.ADDON_VERSION in manifest,
   "version: core.ADDON_VERSION %s matches the manifest" % core.ADDON_VERSION)
with open(os.path.join(ROOT, "app", "bridge.py"), encoding="utf-8") as fh:
    app_bridge = fh.read()
ok('EXPECTED_ADDON_VERSION = "%s"' % core.ADDON_VERSION in app_bridge,
   "version: and the app expects the same")
ok('"scene_optimizer"' in app_bridge and '"opt_status", "0.11.0"' in app_bridge,
   "version: the optimizer declares a FEATURE_REQUIREMENTS entry, so an older "
   "add-on costs this one tab and not the app")

# ⚠ THE ATTRIBUTION RULE. This engine was written from scratch after studying
# how an existing add-on solves the same problem; what ships must name neither
# that add-on nor the toolchain used to build it. The word list is local-only
# (see `_branding`), so a clone skips this rather than failing it.
with open(os.path.join(ADDON, "optimizer.py"), encoding="utf-8") as fh:
    engine_src = fh.read()
lowered = engine_src.lower()
ok(not any(word in lowered for word in _STUDIED + _FORBIDDEN),
   "branding: the engine names neither the studied add-on nor the toolchain")

# =========================================================== registration
pkg.register()
if "madi_pkg" not in bpy.context.preferences.addons:
    bpy.context.preferences.addons.new().module = "madi_pkg"

# ⚠ Blender names an operator class from its bl_idname, NOT from the Python
# class name: `madi_optimizer.preview` registers as MADI_OPTIMIZER_OT_preview.
# Looking for the Python name finds nothing and reads as "never registered".
ok("MADI_OPTIMIZER_OT_preview" in dir(bpy.types),
   "register: the preview operator is registered")
ok(hasattr(bpy.ops.madi_optimizer, "preview"),
   "register: and it is reachable as madi_optimizer.preview")
handlers = [h for h in bpy.app.handlers.load_post
            if getattr(h, "__name__", "") == "_optimizer_load_post"
            and getattr(h, "__module__", "").endswith("optimizer")]
ok(len(handlers) == 1, "register: exactly one load_post handler (got %d)"
   % len(handlers))

# A reload must not leave the old handler firing against a dead module. The
# functions are DIFFERENT OBJECTS after a reload, so identity cannot find them -
# this is the qualified-name sweep jiggle.py and picker.py both needed. The real
# reload shape is unregister-then-register, not register twice.
opt.unregister()
opt.register()
handlers = [h for h in bpy.app.handlers.load_post
            if getattr(h, "__name__", "") == "_optimizer_load_post"
            and getattr(h, "__module__", "").endswith("optimizer")]
ok(len(handlers) == 1,
   "register: a reload still leaves exactly ONE handler (got %d)"
   % len(handlers))

ok(opt.default_cache_dir().endswith("madi_optimizer_cache"),
   "register: the default cache folder is ours, not a shared one")

# ================================================ the licence gate is GONE
# ⚠ REMOVED 2026-08-14: every tab went free and all three prefix gates
# (opt_*, madiref_*) came OUT of server.py — premium packs are the
# paid thing now, gated in the app's licence server, not in Blender. These
# checks prove the ABSENCE with entitlement LOCKED, because a leftover gate
# would silently re-lock a free tool for anyone whose old licence lapsed.
ent.lock("test")
leaked = []
for cmd in ("opt_plan", "opt_resize", "opt_adaptive", "opt_decimate",
            "opt_regenerate", "opt_estimate", "opt_preview_start",
            "opt_preview_stop"):
    try:
        server.BridgeServer()._handle({"cmd": cmd, "params": {}})
    except RuntimeError as exc:
        if "locked" in str(exc).lower():
            leaked.append(cmd)
    except Exception:                       # noqa: BLE001
        pass
ok(not leaked,
   "gate: NO opt_ command refuses for licence reasons while locked - the "
   "prefix gate is really gone (leaked: %r)" % leaked)

ok('cmd.startswith("opt_")' not in open(
    os.path.join(ADDON, "server.py"), encoding="utf-8").read(),
   "gate: the opt_ prefix check has LEFT server.py, not merely stopped firing")

try:
    status = server.BridgeServer()._handle({"cmd": "opt_status", "params": {}})
    ok(isinstance(status, dict) and "managed" in status,
       "gate: opt_status answers as it always did, gate or none")
except Exception as exc:                    # noqa: BLE001
    ok(False, "gate: opt_status answers (%s)" % exc)

for cmd in ("opt_revert_images", "opt_revert_meshes"):
    try:
        server.BridgeServer()._handle({"cmd": cmd, "params": {}})
        ok(True, "gate: %s answers while locked - reverting was never a paid "
                 "action and still is not" % cmd)
    except Exception as exc:                # noqa: BLE001
        ok(False, "gate: %s answers while locked (%s)" % (cmd, exc))

try:
    report = server.BridgeServer()._handle({"cmd": "opt_progress",
                                            "params": {}})
    ok(isinstance(report, dict) and "active" in report,
       "gate: opt_progress still reports")
except Exception as exc:                    # noqa: BLE001
    ok(False, "gate: opt_progress still reports (%s)" % exc)

# The preview operator's poll consulted the licence until 2026-08-14; now it
# is the camera alone, asserted against the scene so the check is honest
# whichever way this scene happens to be set up.
ok(bpy.ops.madi_optimizer.preview.poll()
   == (bpy.context.scene.camera is not None),
   "gate: the preview operator polls on the camera alone, locked or not")
ent._STATE.update(unlocked=True, sub="test", not_after=2 ** 31, reason="ok")

# =========================================================== paths and naming
ok(opt.split_frame_number("walk_0042.png") == ("walk_", 42, 4, ".png"),
   "paths: a frame number is read off the END of the name")
ok(opt.split_frame_number("cam2_0007.png") == ("cam2_", 7, 4, ".png"),
   "paths: the LAST digit run wins, so cam2_0007 numbers on 0007 not on the 2")
ok(opt.split_frame_number("plain.png") is None,
   "paths: a name with no digits is not a sequence")
ok(opt.tile_token("skin.<UDIM>.png") == "<UDIM>",
   "paths: <UDIM> is recognised")
ok(opt.tile_token("skin.<UVTILE>.png") == "<UVTILE>",
   "paths: <UVTILE> is recognised too")
ok(opt.tile_token("skin.png") is None, "paths: a plain name has no tile token")

a = opt.standin_path(CACHE, os.path.join(SRC, "x.png"), 512)
b = opt.standin_path(CACHE, os.path.join(SRC, "x.png"), 1024)
c = opt.standin_path(CACHE, os.path.join(SRC, "y.png"), 512)
ok(a != b and a != c and os.path.dirname(a) == CACHE,
   "paths: one source keeps a separate stand-in per size, and two sources "
   "never collide")
ok(a.endswith("_512.png"),
   "paths: the size is IN the name, so switching sizes reuses the cache")
ok(opt.standin_path(CACHE, os.path.join(SRC, "walk_0001.png"), 256, frame=7)
   .endswith("_256_0007.png"),
   "paths: a sequence stand-in keeps the frame number, zero-padded as it was")
ok("<UDIM>" in opt.standin_path(CACHE, os.path.join(SRC, "sk.<UDIM>.png"), 256),
   "paths: a tiled stand-in KEEPS the token, so Blender still resolves tiles")

# =========================================================== the size maths
ok(opt.next_power_of_two(1000) == 1024 and opt.next_power_of_two(1024) == 1024
   and opt.next_power_of_two(1025) == 2048,
   "size: rounds UP to a power of two, and leaves an exact one alone")
ok(opt.size_for_bounds(None, 1.0, 256, 4096) == 256,
   "size: an object out of frame gets the MINIMUM, not zero - it can still "
   "show up in a reflection or a shadow")
bounds = {"width": 900.0, "height": 400.0, "depth": 5.0}
ok(opt.size_for_bounds(bounds, 1.0, 32, 4096) == 1024,
   "size: 900 px across -> 1024")
ok(opt.size_for_bounds(bounds, 0.25, 32, 4096) == 256,
   "size: the quality factor scales it (0.25 -> 256)")
ok(opt.size_for_bounds(bounds, 1.0, 32, 512) == 512,
   "size: the maximum clamps before the power-of-two rounding")
ok(opt.size_for_bounds({"width": 4.0, "height": 4.0, "depth": 1.0},
                       1.0, 1, 4096) == opt.MIN_SIDE,
   "size: nothing is ever generated below the %d px floor" % opt.MIN_SIDE)

ok(opt.decimate_ratio_for(5.0, 20.0, 200.0, 0.2) == 1.0,
   "decimate: closer than the full-quality distance means no decimation")
ok(opt.decimate_ratio_for(500.0, 20.0, 200.0, 0.2) == 0.2,
   "decimate: past the far distance it sits at the lowest ratio")
mid = opt.decimate_ratio_for(110.0, 20.0, 200.0, 0.2)
ok(abs(mid - 0.6) < 1e-6, "decimate: halfway is halfway along the ramp (%.3f)"
   % mid)

# =========================================================== one image
big = make_png("big.png", 2048, 1024)
image = bpy.data.images.load(big)
ok(opt.can_manage(image)[0], "image: a normal PNG on disk can be managed")
ok(opt.source_dimensions(image) == (2048, 1024),
   "image: the source size is read from the file header")

tally = opt.Tally()
opt.set_image_size(image, 512, CACHE, tally=tally)
ok(len(tally.changed) == 1, "resize: the image was changed")
ok(os.path.dirname(bpy.path.abspath(image.filepath)) == CACHE,
   "resize: the datablock now points into the cache")
ok(os.path.isfile(bpy.path.abspath(image.filepath)),
   "resize: and the stand-in is really there")
ok(opt.read_dimensions(bpy.path.abspath(image.filepath)) == (512, 256),
   "resize: 2048x1024 -> 512x256, aspect kept")
ok(image.get(opt.PROP_ORIGINAL) == big and image.get(opt.PROP_SIZE) == 512,
   "resize: the three marks are on the datablock, so this survives a save")
ok(os.path.isfile(big) and opt.read_dimensions(big) == (2048, 1024),
   "resize: THE ORIGINAL FILE IS UNTOUCHED")
ok(tally.bytes_after < tally.bytes_before,
   "resize: the tally reports less memory than before")

# Idempotence: running again changes nothing.
again = opt.Tally()
opt.set_image_size(image, 512, CACHE, tally=again)
ok(len(again.changed) == 0 and len(again.unchanged) == 1,
   "resize: a second run at the same size is a no-op")

# ⚠ THE RULE THE WHOLE DESIGN RESTS ON: a stand-in is never the source of the
# next one. Going 512 -> 1024 must re-read the 2048 original, not upscale 512.
opt.set_image_size(image, 1024, CACHE)
ok(opt.read_dimensions(bpy.path.abspath(image.filepath)) == (1024, 512),
   "resize: 512 -> 1024 is generated from the ORIGINAL, never from the 512 copy")

# Never upscale.
opt.set_image_size(image, 4096, CACHE)
ok(not opt.is_managed(image) and bpy.path.abspath(image.filepath) == big,
   "resize: asking for more than the source has reverts to the original "
   "instead of making a bigger copy of a smaller one")

opt.set_image_size(image, 256, CACHE)
before_revert = image.get(opt.PROP_ORIGINAL)
opt.revert_image(image)
ok(bpy.path.abspath(image.filepath) == big and before_revert == big,
   "revert: the filepath goes back to exactly the user's own file")
ok(opt.PROP_ORIGINAL not in image.keys() and opt.PROP_SIZE not in image.keys(),
   "revert: and all three marks are gone")
second = opt.Tally()
opt.revert_image(image, tally=second)
ok(len(second.changed) == 0, "revert: reverting twice is a harmless no-op")

# Tamper detection: the user re-pointed it by hand, so let go of it.
opt.set_image_size(image, 256, CACHE)
image.filepath = big
ok(opt.check_tampered(image), "tamper: a hand-edited filepath is detected")
ok(not opt.is_managed(image),
   "tamper: and we stop managing it rather than swapping their choice back")

# Staleness: a source edited on disk means the stand-in has to be re-made.
opt.set_image_size(image, 256, CACHE)
standin = bpy.path.abspath(image.filepath)
make_png("big.png", 2048, 1024)              # rewrite the source
# Age the STAND-IN rather than dating the source into the future: a source with
# a future mtime is stale forever, which would make this assert something the
# code can never satisfy rather than what it actually does.
old = os.path.getmtime(big) - 600
os.utime(standin, (old, old))
ok(opt._is_stale(standin, big),
   "stale: a stand-in no newer than its source is stale")
regen = opt.Tally()
opt.regenerate_image(image, tally=regen)
ok(len(regen.changed) == 1 and not opt._is_stale(
    bpy.path.abspath(image.filepath), big),
   "stale: and Check & Regenerate re-makes it")

# =========================================================== what is skipped
gen = bpy.data.images.new("generated", 64, 64)
allowed, why = opt.can_manage(gen)
ok(not allowed and "no file" in why.lower() or "generated" in (why or "").lower(),
   "skip: a GENERATED image has no file to shrink (%s)" % why)

packed_path = make_png("packed.png", 512, 512)
packed = bpy.data.images.load(packed_path)
packed.pack()
allowed, why = opt.can_manage(packed)
ok(not allowed and "packed" in why, "skip: a packed image is skipped, with a "
                                    "reason the UI can show (%s)" % why)

odd = make_png("odd.png", 64, 64)
weird = os.path.join(SRC, "odd.psd")
shutil.copyfile(odd, weird)
odd_image = bpy.data.images.load(weird)
allowed, why = opt.can_manage(odd_image)
ok(not allowed and "rewrite" in why,
   "skip: an unsupported format says so instead of failing later (%s)" % why)

missing = bpy.data.images.load(odd)
missing.filepath = os.path.join(SRC, "not-here.png")
allowed, why = opt.can_manage(missing)
ok(not allowed and "missing" in why, "skip: a missing file is reported")

skip_tally = opt.Tally()
opt.set_image_size(packed, 128, CACHE, tally=skip_tally)
ok(len(skip_tally.skipped) == 1 and skip_tally.skipped[0][1].startswith("packed"),
   "skip: a batch records WHY each image was left alone")

# =========================================================== sequences
for frame in range(1, 4):
    make_png("seq/shot_%04d.png" % frame, 800, 400)
seq_first = os.path.join(SRC, "seq", "shot_0001.png")
found = opt.sequence_files(seq_first)
ok(len(found) == 3 and found[0][1] == 1 and found[2][1] == 3,
   "sequence: every sibling frame on disk is discovered (%d)" % len(found))
seq_image = bpy.data.images.load(seq_first)
seq_image.source = 'SEQUENCE'
opt.set_image_size(seq_image, 256, CACHE)
made = [f for f in os.listdir(CACHE) if "_256_" in f]
ok(len(made) == 3,
   "sequence: a stand-in is generated for EVERY frame, not just the one the "
   "datablock points at (%d)" % len(made))
ok(opt.is_managed(seq_image) and os.path.basename(seq_image.filepath)
   == seq_image.get(opt.PROP_STANDIN),
   "sequence: the datablock points at its own frame's stand-in")

# =========================================================== UDIM tiles
for tile in (1001, 1002):
    make_png("udim/skin.%d.png" % tile, 1024, 1024)
udim_pattern = os.path.join(SRC, "udim", "skin.<UDIM>.png")
tiles = opt.tile_files(udim_pattern)
ok(len(tiles) == 2, "udim: both tiles are found on disk (%d)" % len(tiles))
udim_image = bpy.data.images.load(os.path.join(SRC, "udim", "skin.1001.png"))
udim_image.source = 'TILED'
udim_image.filepath = udim_pattern
opt.set_image_size(udim_image, 256, CACHE)
tile_files = [f for f in os.listdir(CACHE)
              if f.endswith(".png") and ".100" in f]
ok(len(tile_files) == 2,
   "udim: one stand-in per real tile, each keeping its own number (%d)"
   % len(tile_files))
ok("<UDIM>" in udim_image.filepath,
   "udim: the assigned path keeps the token, so Blender resolves the set")

# =========================================================== the scene scan
scene = bpy.context.scene
bpy.ops.mesh.primitive_plane_add(size=2, location=(0, 0, -5))
near = bpy.context.active_object
near.name = "NearPlane"
material = bpy.data.materials.new("MatNear")
material.use_nodes = True
tex = material.node_tree.nodes.new("ShaderNodeTexImage")
near_image = bpy.data.images.load(make_png("near.png", 4096, 4096))
tex.image = near_image
near.data.materials.append(material)

found = opt.images_for_object(near)
ok(near_image in found, "scan: an image node in a material is found")

# ⚠ THE GAP WE CLOSE. Geometry nodes are invisible to the tool this replaces,
# and Marty's scenes are geonode-heavy - without this half his textures would
# look unused and get sized as if nothing referenced them.
group = bpy.data.node_groups.new("GN", "GeometryNodeTree")
gn_image = bpy.data.images.load(make_png("gn.png", 512, 512))
gn_node = group.nodes.new("GeometryNodeImageTexture")
# ⚠ A geometry-nodes image texture has NO `.image` property - the image arrives
# on an input SOCKET. A scan that only reads `node.image` finds every shader
# texture and not one geonode texture.
image_socket = next(s for s in gn_node.inputs
                    if s.bl_idname == "NodeSocketImage")
image_socket.default_value = gn_image
ok(not hasattr(gn_node, "image"),
   "scan: a geonode image texture really has no .image property - which is why "
   "the socket walk exists")
modifier = near.modifiers.new("GN", 'NODES')
modifier.node_group = group
found = opt.images_for_object(near)
ok(gn_image in found,
   "scan: an image inside a GEOMETRY NODES tree is found (the gap in the "
   "original)")

# An image fed to the MODIFIER rather than set inside the tree - a different
# storage entirely (an ID property on the modifier), so a different code path.
socket_image = bpy.data.images.load(make_png("socket.png", 256, 256))
group.interface.new_socket("Tex", in_out='INPUT', socket_type='NodeSocketImage')
near.modifiers.remove(modifier)
modifier = near.modifiers.new("GN2", 'NODES')
modifier.node_group = group
identifier = next(item.identifier for item in group.interface.items_tree
                  if getattr(item, "in_out", "") == 'INPUT'
                  and getattr(item, "socket_type", "") == 'NodeSocketImage')
# ⚠ Blender 5.x moved these off the modifier itself - `modifier[identifier]`
# raises "this type doesn't support IDProperties" and they live on
# `modifier.properties.inputs` instead (assets.py hit the same wall).
#
# ⚠ AND EACH ENTRY IS AN IDPropertyGroup, NOT THE VALUE. Writing the image
# straight onto the key (`inputs[identifier] = image`) REPLACES that group with
# a bare pointer - a shape Blender itself never writes. A reader tested against
# that passes while finding nothing in a real file. So this sets it the way the
# modifier panel does, through ["value"].
entry = modifier.properties.inputs[identifier]
ok(hasattr(entry, "keys") and "value" in entry.keys(),
   "scan: a modifier input really is a group with a 'value' inside, which is "
   "what has to be unwrapped")
entry["type"] = 1
entry["value"] = socket_image
ok(socket_image in opt.images_for_object(near),
   "scan: an image fed to the geonode MODIFIER is found too")
ok(socket_image in opt.modifier_inputs(modifier),
   "scan: modifier_inputs unwraps 5.x's properties.inputs[...]['value']")

# ⚠ A modifier whose group has no interface inputs makes keys() RAISE rather
# than return nothing - unguarded, one of those aborts the whole scan.
bare_group = bpy.data.node_groups.new("Bare", "GeometryNodeTree")
bare = near.modifiers.new("Bare", 'NODES')
bare.node_group = bare_group
try:
    opt.images_for_object(near)
    ok(True, "scan: a modifier with no interface inputs does not abort the scan")
except Exception as exc:                    # noqa: BLE001
    ok(False, "scan: a modifier with no interface inputs aborted it (%s)" % exc)
near.modifiers.remove(bare)

# =========================================================== adaptive sizing
bpy.ops.object.camera_add(location=(0, 0, 0), rotation=(0, 0, 0))
camera = bpy.context.active_object
scene.camera = camera

bpy.ops.mesh.primitive_plane_add(size=2, location=(0, 0, -50))
mid_plane = bpy.context.active_object
mid_plane.name = "MidPlane"
mid_mat = bpy.data.materials.new("MatMid")
mid_mat.use_nodes = True
mid_tex = mid_mat.node_tree.nodes.new("ShaderNodeTexImage")
mid_image = bpy.data.images.load(make_png("mid.png", 4096, 4096))
mid_tex.image = mid_image
mid_plane.data.materials.append(mid_mat)

bpy.ops.mesh.primitive_plane_add(size=2, location=(0, 0, -500))
far_plane = bpy.context.active_object
far_plane.name = "FarPlane"
far_mat = bpy.data.materials.new("MatFar")
far_mat.use_nodes = True
far_tex = far_mat.node_tree.nodes.new("ShaderNodeTexImage")
far_image = bpy.data.images.load(make_png("far.png", 4096, 4096))
far_tex.image = far_image
far_plane.data.materials.append(far_mat)

bpy.context.view_layer.update()
plan = opt.plan_adaptive(target="SCENE", quality=1.0, min_size=32,
                         max_size=4096)
near_side = plan["images"][near_image.name]
mid_side = plan["images"][mid_image.name]
far_side = plan["images"][far_image.name]
ok(near_side > mid_side > far_side,
   "adaptive: nearer objects get bigger textures (%d > %d > %d)"
   % (near_side, mid_side, far_side))
ok(all(s & (s - 1) == 0 for s in (near_side, mid_side, far_side)),
   "adaptive: every size is a power of two")
ok(far_side >= opt.MIN_SIDE,
   "adaptive: even the far one stays above the floor (%d)" % far_side)

# A behind-the-camera object gets the MINIMUM, not nothing.
bpy.ops.mesh.primitive_plane_add(size=2, location=(0, 0, 40))
behind = bpy.context.active_object
behind.name = "BehindPlane"
behind_mat = bpy.data.materials.new("MatBehind")
behind_mat.use_nodes = True
behind_tex = behind_mat.node_tree.nodes.new("ShaderNodeTexImage")
behind_image = bpy.data.images.load(make_png("behind.png", 2048, 2048))
behind_tex.image = behind_image
behind.data.materials.append(behind_mat)
bpy.context.view_layer.update()
plan = opt.plan_adaptive(target="SCENE", quality=1.0, min_size=256,
                         max_size=4096)
ok(plan["images"][behind_image.name] == 256,
   "adaptive: an object behind the camera gets the MINIMUM size (%d) - it can "
   "still appear in a reflection" % plan["images"][behind_image.name])

# A texture shared by a near object and a far one takes the NEAR one's size.
far_plane.data.materials.clear()
far_plane.data.materials.append(material)      # the near plane's material
bpy.context.view_layer.update()
plan = opt.plan_adaptive(target="SCENE", quality=1.0, min_size=32,
                         max_size=4096)
ok(plan["images"][near_image.name] == near_side,
   "adaptive: a shared texture keeps the NEAR object's size, never the far "
   "object's (%d)" % plan["images"][near_image.name])
far_plane.data.materials.clear()
far_plane.data.materials.append(far_mat)

# Nothing is ever asked for above the source's real resolution.
small_image = bpy.data.images.load(make_png("small.png", 128, 128))
small_tex = material.node_tree.nodes.new("ShaderNodeTexImage")
small_tex.image = small_image
bpy.context.view_layer.update()
plan = opt.plan_adaptive(target="SCENE", quality=4.0, min_size=32,
                         max_size=8192)
ok(plan["images"][small_image.name] <= 128,
   "adaptive: an adaptive size is capped at what the file actually holds (%d)"
   % plan["images"][small_image.name])

# resolution_percentage is honoured (the original ignores it).
scene.render.resolution_percentage = 50
bpy.context.view_layer.update()
half = opt.plan_adaptive(target="SCENE", quality=1.0, min_size=32,
                         max_size=4096)["images"][near_image.name]
scene.render.resolution_percentage = 100
ok(half < near_side,
   "adaptive: rendering at 50%% asks for smaller textures (%d < %d)"
   % (half, near_side))

# Animation mode: the frame where the object is CLOSEST decides.
scene.frame_start, scene.frame_end = 1, 10
far_plane.location = (0, 0, -500)
far_plane.keyframe_insert("location", frame=1)
far_plane.location = (0, 0, -6)
far_plane.keyframe_insert("location", frame=10)
scene.frame_set(1)
bpy.context.view_layer.update()
still = opt.plan_adaptive(target="SCENE", quality=1.0, min_size=32,
                          max_size=4096)["images"][far_image.name]
moving = opt.plan_adaptive(target="SCENE", quality=1.0, min_size=32,
                           max_size=4096, animation=True,
                           frame_step=1)["images"][far_image.name]
ok(moving > still,
   "animation: the closest frame decides, so a texture that walks up to the "
   "camera is sized for that (%d > %d)" % (moving, still))
ok(scene.frame_current == 1,
   "animation: the current frame is put back afterwards")
stepped = opt.plan_adaptive(target="SCENE", quality=1.0, min_size=32,
                            max_size=4096, animation=True,
                            frame_step=4)["images"][far_image.name]
ok(stepped >= still, "animation: a frame step still covers the range (%d)"
   % stepped)

# No camera is a clear refusal, not a crash.
scene.camera = None
try:
    opt.plan_adaptive(target="SCENE")
    ok(False, "adaptive: no camera should refuse")
except RuntimeError as exc:
    ok("camera" in str(exc), "adaptive: no camera refuses with a plain reason")
scene.camera = camera

# =========================================================== decimation
bpy.ops.mesh.primitive_grid_add(x_subdivisions=80, y_subdivisions=80,
                                size=2, location=(0, 0, -400))
heavy = bpy.context.active_object
heavy.name = "HeavyFar"
bpy.ops.mesh.primitive_grid_add(x_subdivisions=80, y_subdivisions=80,
                                size=2, location=(0, 0, -6))
heavy_near = bpy.context.active_object
heavy_near.name = "HeavyNear"
bpy.context.view_layer.update()

plan = opt.plan_adaptive(target="SCENE", quality=1.0, min_size=32,
                         max_size=4096)
objects = [bpy.data.objects[n] for n in plan["objects"]]
decimation = opt.plan_decimation(objects, plan["depths"], face_floor=5000,
                                 full_distance=20.0, low_distance=200.0,
                                 low_ratio=0.2)
ok(heavy.data.name in decimation,
   "decimate: the far high-poly mesh is in the plan")
ok(heavy_near.data.name not in decimation,
   "decimate: the near one is left alone")
ok(near.data.name not in decimation,
   "decimate: a mesh under the face floor is skipped (%d faces)"
   % len(near.data.polygons))
ok(abs(decimation[heavy.data.name] - 0.2) < 1e-6,
   "decimate: past the far distance it is the lowest ratio (%.3f)"
   % decimation[heavy.data.name])

opt.apply_decimation(objects, decimation)
mod = heavy.modifiers.get(opt.DECIMATE_MOD)
ok(mod is not None and mod.type == 'DECIMATE',
   "decimate: the managed modifier is on the object")
ok(mod is not None and abs(mod.ratio - 0.2) < 1e-6,
   "decimate: with the planned ratio")
ok(heavy.modifiers[-1].name == opt.DECIMATE_MOD,
   "decimate: appended at the END of the stack")
ok(heavy_near.modifiers.get(opt.DECIMATE_MOD) is None,
   "decimate: nothing is added to the near object")

# ⚠ THE STALE SWEEP - the flaw in the original this fixes. An object that has
# since moved closer keeps a modifier from a run that no longer applies, and
# nothing about the scene shows it.
heavy.location = (0, 0, -6)
bpy.context.view_layer.update()
plan = opt.plan_adaptive(target="SCENE", quality=1.0, min_size=32,
                         max_size=4096)
objects = [bpy.data.objects[n] for n in plan["objects"]]
decimation = opt.plan_decimation(objects, plan["depths"], face_floor=5000,
                                 full_distance=20.0, low_distance=200.0,
                                 low_ratio=0.2)
opt.apply_decimation(objects, decimation)
ok(heavy.modifiers.get(opt.DECIMATE_MOD) is None,
   "decimate: an object that moved close has its stale modifier SWEPT, not "
   "left behind")

# A Decimate the user added themselves is never touched.
heavy.location = (0, 0, -400)
theirs = heavy_near.modifiers.new("MyOwnDecimate", 'DECIMATE')
theirs.ratio = 0.5
bpy.context.view_layer.update()
plan = opt.plan_adaptive(target="SCENE", quality=1.0, min_size=32,
                         max_size=4096)
objects = [bpy.data.objects[n] for n in plan["objects"]]
opt.apply_decimation(objects, opt.plan_decimation(objects, plan["depths"]))
ok(heavy_near.modifiers.get("MyOwnDecimate") is not None
   and abs(heavy_near.modifiers["MyOwnDecimate"].ratio - 0.5) < 1e-6,
   "decimate: the user's own Decimate is matched by NAME and left alone")

revert_tally = opt.Tally()
opt.clear_decimation(objects, tally=revert_tally)
ok(all(ob.modifiers.get(opt.DECIMATE_MOD) is None for ob in objects),
   "decimate: reverting removes every one of ours")
ok(heavy_near.modifiers.get("MyOwnDecimate") is not None,
   "decimate: and still leaves the user's alone")

# =========================================================== memory estimate
report = opt.estimate_memory()
kinds = {row["kind"] for row in report["rows"]}
ok("Mesh" in kinds and "Image" in kinds,
   "estimate: both meshes and images are counted")
ok(report["total_bytes"] > 0 and report["total_human"],
   "estimate: a total is reported in readable units (%s)"
   % report["total_human"])
sizes = [row["bytes"] for row in report["rows"]]
ok(sizes == sorted(sizes, reverse=True),
   "estimate: biggest first, which is the only order worth reading")
ok(abs(sum(row["share"] for row in report["rows"]) - 1.0) < 0.01
   or report["shown"] < report["counted"],
   "estimate: the shares add up")

hidden = bpy.data.objects.new("HiddenMesh", bpy.data.meshes.new("HiddenData"))
scene.collection.objects.link(hidden)
hidden.hide_render = True
after = opt.estimate_memory()
ok(not any(row["name"] == "HiddenData" for row in after["rows"]),
   "estimate: something hidden from the render is not listed - it costs "
   "nothing and would send people optimising the wrong thing")

ok(opt.human_bytes(1536) == "1.5 KB" and opt.human_bytes(2 * 1024 ** 3)
   == "2.0 GB", "estimate: byte sizes read as people write them")

# =========================================================== targets / status
ok(set(opt.TARGETS) >= {"SELECTED", "SCENE", "ALL_OBJECTS", "IMAGES_HDR"},
   "targets: all six sets are offered")
hdrs = opt.target_images("IMAGES_HDR")
ok(all(os.path.splitext(opt.original_of(im))[1].lower() in opt.HDR_EXTS
       for im in hdrs),
   "targets: the HDR set holds only .exr/.hdr - the one-click way to cap a "
   "world HDRI, which no object target can ever reach")
no_hdr = opt.target_images("IMAGES_NO_HDR")
ok(not (set(hdrs) & set(no_hdr)),
   "targets: the two image sets do not overlap")

status = opt.opt_status()
ok(status["camera"] == camera.name and status["objects"] > 0,
   "status: reports the camera and the scene")
ok(isinstance(status["managed"], list) and "default_cache" in status,
   "status: lists what is currently managed, and where the cache lives")
ok(status["addon_can_resize"] is True,
   "status: OpenImageIO is present, so resizing is possible")

# The status must never CHANGE anything - it is polled.
before = (len(bpy.data.images), len(bpy.data.objects),
          [ob.modifiers.get(opt.DECIMATE_MOD) is not None
           for ob in scene.objects])
opt.opt_status()
after = (len(bpy.data.images), len(bpy.data.objects),
         [ob.modifiers.get(opt.DECIMATE_MOD) is not None
          for ob in scene.objects])
ok(before == after, "status: a poll changes nothing at all")

# =========================================================== whole-run plumbing
result = opt.opt_adaptive({"target": "SCENE", "quality": 1.0, "min_size": 32,
                           "max_size": 4096, "meshes": True,
                           "cache_dir": CACHE})
ok("managed" in result and "result" in result,
   "run: every command answers with the WHOLE status, results under their own "
   "key - a bare result dict would blank the app's panels")
ok(result["result"]["counts"]["changed"] > 0,
   "run: the adaptive pass actually changed images")
ok("mesh_result" in result,
   "run: mesh work is reported separately from image work")

reverted = opt.opt_revert_images({"target": "ALL_IMAGES"})
ok(all(not entry for entry in [reverted["managed"]]) or
   len(reverted["managed"]) == 0,
   "run: reverting the whole file leaves nothing managed")

planned = opt.opt_plan({"target": "SCENE", "quality": 1.0, "min_size": 32,
                        "max_size": 4096, "meshes": True})
ok("plan" in planned and isinstance(planned["plan"]["images"], list),
   "run: a plan can be read without changing a thing")
ok(len(opt.opt_status()["managed"]) == 0,
   "run: and planning really did change nothing")

# An image target has nothing to measure against a camera, and says so.
try:
    opt.opt_adaptive({"target": "ALL_IMAGES"})
    ok(False, "run: an image set should refuse adaptive sizing")
except RuntimeError as exc:
    ok("fixed size" in str(exc).lower() or "object target" in str(exc).lower(),
       "run: an image set refuses adaptive sizing and points at the fixed-size "
       "tool instead")

# =========================================================== progress
# ⚠ A run owns Blender's main thread from start to finish, so the ONLY way it
# can report how far along it is is off a plain record read from a socket
# thread. Everything below is about that record being safe to read that way.

import json as _json                                   # noqa: E402
import threading as _threading                         # noqa: E402
import time                                            # noqa: E402

# ⚠ Checked against the COMPILED CODE, not the source text. `co_names` holds
# every global and attribute the function actually references, so it cannot be
# satisfied - or broken - by a docstring that happens to mention bpy, which is
# exactly what a grep over the source did on the first attempt.
ok("bpy" not in opt.opt_progress.__code__.co_names,
   "progress: opt_progress touches NO bpy - it is answered on a socket thread "
   "while the main thread is writing Blender's data, and reading bpy from "
   "there is how you crash Blender rather than how you report progress "
   "(references: %s)" % (opt.opt_progress.__code__.co_names,))
ok("bpy" not in opt._progress_set.__code__.co_names
   and "bpy" not in opt._progress_step.__code__.co_names,
   "progress: nor does anything that writes the record, so the reader can "
   "never be handed a bpy object it is not safe to touch")

report = opt.opt_progress()
ok(set(report) >= {"active", "phase", "done", "total", "item", "serial",
                   "elapsed"},
   "progress: the reply carries everything the bar needs in one read")
ok(report["active"] is False,
   "progress: nothing is running, and it says so rather than guessing")

before_serial = opt.opt_progress()["serial"]
first = opt._progress
opt._progress_begin("Testing")
ok(opt._progress is not first,
   "progress: the record is REPLACED, never mutated - rebinding the global is "
   "atomic under the GIL, so a reader gets a whole record or the previous one, "
   "never half of one")
ok(opt.opt_progress()["serial"] == before_serial + 1,
   "progress: the serial rises once per run, so a reply still in flight when a "
   "run ends cannot drive the next run's bar")
opt._progress_phase("Resizing textures", 5)
opt._progress_step("skin.png")
opt._progress_step("wall.png")
mid = opt.opt_progress()
ok(mid["active"] and mid["done"] == 2 and mid["total"] == 5
   and mid["item"] == "wall.png",
   "progress: a stage counts items and names the one it is on")
ok(mid["phase"] == "Resizing textures",
   "progress: and says which stage of the run it is in")
opt._progress_end()
ok(opt.opt_progress()["active"] is False,
   "progress: ending clears it")

# Outside a run the markers must do nothing at all, or a headless call would
# leave an 'active' run behind that nothing is ever going to finish.
opt._progress_phase("Should not stick", 99)
opt._progress_step("nor this")
ok(opt.opt_progress()["active"] is False
   and opt.opt_progress()["total"] == 0,
   "progress: the stage markers are no-ops outside a run - a direct headless "
   "call cannot strand an 'active' run")

opt.plan_adaptive(target="SCENE", quality=1.0, min_size=32, max_size=4096)
ok(opt.opt_progress()["active"] is False,
   "progress: and calling the planner straight leaves nothing active either")

# The context manager must clear the record even when the run raises, or one
# failure leaves the bar up for the rest of the session.
try:
    with opt._progress_run("Failing"):
        opt._progress_phase("Resizing textures", 3)
        raise RuntimeError("boom")
except RuntimeError:
    pass
ok(opt.opt_progress()["active"] is False,
   "progress: a run that RAISES still clears the record - one failure must not "
   "leave the bar up for the rest of the session")

# Sampled from another thread across a real run, which is exactly how it is
# read in production.
samples = []
stop_sampling = _threading.Event()


def _sampler():
    # Paced, not a spin: this mirrors the app's 300 ms bar poll, and a tight
    # loop here would just burn a core fighting the run for the GIL.
    while not stop_sampling.is_set():
        samples.append(opt.opt_progress())
        time.sleep(0.002)


sampler = _threading.Thread(target=_sampler, daemon=True)
sampler.start()
opt.opt_adaptive({"target": "SCENE", "quality": 1.0, "min_size": 32,
                  "max_size": 4096, "meshes": True, "cache_dir": CACHE})
stop_sampling.set()
sampler.join(5.0)
ok(len(samples) > 0, "progress: a socket thread can read it during a run")
ok(all(isinstance(s, dict) and set(s) >= {"active", "done", "total", "serial"}
       for s in samples),
   "progress: and every one of the %d samples was a COMPLETE record - never a "
   "half-written one" % len(samples))
ok(opt.opt_progress()["active"] is False,
   "progress: the record is clear once the run is over")
opt.opt_revert_images({"target": "ALL_IMAGES"})

# ⚠ THE BYPASS ITSELF. `_dispatch` normally hands work to the main thread and
# waits - and during a run the main thread is not draining that queue, so a
# queued progress request would answer only once the run it is reporting on had
# finished. This proves the command skips the queue: nothing is draining it
# here either, so an answer coming back at all is the bypass working.
dispatched = {}


def _ask():
    dispatched["reply"] = server.BridgeServer()._dispatch(
        _json.dumps({"cmd": "opt_progress", "params": {}}).encode("utf-8"))


asker = _threading.Thread(target=_ask, daemon=True)
asker.start()
asker.join(5.0)
ok(not asker.is_alive() and dispatched.get("reply", {}).get("ok") is True,
   "progress: opt_progress is answered WITHOUT the main-thread queue - with "
   "nothing draining that queue it would otherwise have blocked, which is "
   "exactly what it would do mid-run")

ok("opt_progress" in server.BridgeServer.capabilities(),
   "progress: and it is advertised, so an app can tell whether this add-on can "
   "be asked at all")

# =========================================================== linked libraries
# ⚠ A linked object's modifier stack cannot be edited, and ONE linked user
# vetoes the whole mesh - decimating for the local users only would leave the
# same mesh at two densities depending on which object you look through.
lib_blend = os.path.join(TMP, "linked.blend")
bpy.ops.mesh.primitive_grid_add(x_subdivisions=80, y_subdivisions=80, size=2,
                                location=(0, 0, -400))
donor = bpy.context.active_object
donor.name = "DonorMesh"
bpy.ops.wm.save_as_mainfile(filepath=lib_blend, copy=True)
bpy.data.objects.remove(donor, do_unlink=True)
bpy.ops.wm.link(filepath=os.path.join(lib_blend, "Object", "DonorMesh"),
                directory=os.path.join(lib_blend, "Object"),
                filename="DonorMesh")
linked = bpy.data.objects.get("DonorMesh")
if linked is not None and linked.library is not None:
    bpy.context.view_layer.update()
    plan = opt.plan_adaptive(target="SCENE", quality=1.0, min_size=32,
                             max_size=4096)
    objects = [bpy.data.objects[n] for n in plan["objects"]
               if n in bpy.data.objects]
    decimation = opt.plan_decimation(objects, plan["depths"], face_floor=5000)
    ok(linked.data.name not in decimation,
       "linked: a linked object's mesh is never decimated")
    tally = opt.Tally()
    opt.apply_decimation(objects, decimation, tally=tally)
    ok(linked.modifiers.get(opt.DECIMATE_MOD) is None,
       "linked: and nothing is added to its stack")
else:
    ok(False, "linked: could not link a test object (linked=%r)" % linked)

# =========================================================== self-heal
image2 = bpy.data.images.load(make_png("heal.png", 1024, 1024))
opt.set_image_size(image2, 256, CACHE)
gone = bpy.path.abspath(image2.filepath)
os.remove(gone)
ok(not os.path.isfile(gone), "heal: the stand-in was deleted")
opt._optimizer_load_post(None)
ok(os.path.isfile(bpy.path.abspath(image2.filepath)),
   "heal: opening the file re-makes a stand-in that went missing")
ok(opt.is_managed(image2),
   "heal: and the image is still managed afterwards")

# ⚠ The cache folder for the heal comes from WHERE THE IMAGE POINTS, never from
# a setting - a .blend opened on another machine has to keep working.
ok("dirname" in engine_src.split("def _optimizer_load_post")[1][:1400],
   "heal: the folder is taken from the image's own path, so the .blend stays "
   "portable")

# ⚠ Two modules in this package must not share a handler name: every by-name
# sweep and by-name count then reads one module's handler as the other's
# duplicate. picker.py already owns `_on_load_post`.
picker_names = [getattr(h, "__name__", "") for h in bpy.app.handlers.load_post
                if getattr(h, "__module__", "").endswith("picker")]
opt_names = [getattr(h, "__name__", "") for h in bpy.app.handlers.load_post
             if getattr(h, "__module__", "").endswith("optimizer")]
ok(not (set(picker_names) & set(opt_names)),
   "handlers: the optimizer's load_post name does not collide with the "
   "picker's (%s vs %s)" % (opt_names, picker_names))

# =========================================================== VRAM estimate
# Marty, 2026-08-04: "a estimate on how much vram would it take to render from
# blender and from CMD (with Render queue)". Two figures, and the DIFFERENCE
# between them is the point - a background render opens no window.
est = opt.estimate_memory()
vram = est.get("vram") or {}
ok(set(vram) >= {"headless_bytes", "interactive_bytes", "buffer_bytes",
                 "bvh_bytes", "ui_bytes", "resolution", "engine"},
   "vram: the estimate carries both figures and what they are made of")
ok(vram["interactive_bytes"] > vram["headless_bytes"],
   "vram: rendering inside Blender needs MORE than the command line - the "
   "viewport and interface sit on the card too (%d vs %d)"
   % (vram["interactive_bytes"], vram["headless_bytes"]))
ok(vram["interactive_bytes"] - vram["headless_bytes"] == vram["ui_bytes"],
   "vram: and the whole difference is the interface, nothing else - that is "
   "what makes the Render Queue worth using on a tight card")
ok(vram["headless_bytes"] > est["total_bytes"],
   "vram: even the headless figure is above the raw scene data, because "
   "render buffers and mesh acceleration structures are not free")

# The render buffers must follow the actual output size, or the figure is
# meaningless for anyone rendering at anything but the default.
_scene = bpy.context.scene
_before = vram["buffer_bytes"]
_old_x = _scene.render.resolution_x
_scene.render.resolution_x = _old_x * 2
_after = (opt.estimate_memory().get("vram") or {})["buffer_bytes"]
ok(_after > _before,
   "vram: doubling the render width raises the buffer figure (%d -> %d)"
   % (_before, _after))
_scene.render.resolution_x = _old_x

_scene.render.resolution_percentage = 50
_half = (opt.estimate_memory().get("vram") or {})["buffer_bytes"]
ok(_half < _before,
   "vram: and resolution_percentage counts - rendering at 50%% really does "
   "need less (%d < %d)" % (_half, _before))
_scene.render.resolution_percentage = 100

# =========================================================== texture sets
# Marty, 2026-08-04: resizing should leave a NAMED SET behind, renameable, so
# several can exist and be cycled between - "one resolution for one scene but
# other for other".
scene = bpy.context.scene
for _g in list(opt.group_state(scene)):
    opt.group_delete(scene, _g["name"])

_first = opt.opt_resize({"target": "ALL_IMAGES", "size": 128,
                         "cache_dir": CACHE})
sets = opt.opt_status()["groups"]
ok(len(sets) == 1,
   "sets: a resize leaves a named set behind without being asked (%d)"
   % len(sets))
ok(sets[0]["name"] == "128 px",
   "sets: named for what it did (%r)" % sets[0]["name"])
ok(sets[0]["active"] and opt.opt_status()["active_group"] == "128 px",
   "sets: and the scene is marked as being on it")
ok(sets[0]["count"] > 0 and sets[0]["missing"] == 0,
   "sets: with its cached files present (%d entries, %d missing)"
   % (sets[0]["count"], sets[0]["missing"]))

_second = opt.opt_resize({"target": "ALL_IMAGES", "size": 64,
                          "cache_dir": CACHE})
sets = opt.opt_status()["groups"]
ok(len(sets) == 2,
   "sets: a SECOND resize makes a second set rather than overwriting the "
   "first - that is the whole point (%d)" % len(sets))
ok([s["active"] for s in sets] == [False, True],
   "sets: exactly one is active, and it is the newest")

opt.group_rename(scene, "64 px", "Hero close-ups")
names = [s["name"] for s in opt.opt_status()["groups"]]
ok("Hero close-ups" in names, "sets: they can be renamed (%s)" % names)
ok(opt.opt_status()["active_group"] == "Hero close-ups",
   "sets: renaming the ACTIVE one keeps it active, rather than quietly "
   "deselecting it")

opt.group_apply(scene, "128 px")
ok(opt.opt_status()["active_group"] == "128 px",
   "sets: switching back to an earlier set works")
_sizes = {opt.managed_size(im) for im in bpy.data.images
          if opt.is_managed(im)}
ok(_sizes == {128},
   "sets: and every texture really went back to that size (%s)" % _sizes)

# ⚠ THE ONE THAT MUST NEVER REGRESS. Marty: "this has nothing to do with
# Original textures (since we always need to have the ability to restore
# originals)". A set is a note; deleting every one of them may not cost anybody
# the way back to their own files.
opt.group_delete(scene, "128 px")
opt.group_delete(scene, "Hero close-ups")
ok(opt.opt_status()["groups"] == [],
   "sets: they can be forgotten")
_still = [im for im in bpy.data.images if opt.is_managed(im)]
ok(len(_still) > 0,
   "sets: forgetting a set does NOT un-manage the textures - it deletes a "
   "note, not a state (%d still managed)" % len(_still))
_reverted = opt.opt_revert_images({"target": "ALL_IMAGES"})
ok(len(opt.opt_status()["managed"]) == 0,
   "sets: AND RESTORE STILL WORKS WITH EVERY SET DELETED - the originals were "
   "never part of a set")

# A queue: several jobs in one run, and ONE SET EACH.
#
# ⚠ THIS ASSERTION USED TO BE THE EXACT OPPOSITE, and the old one was wrong.
# The queue recorded a whole run as a single set, on the reasoning that it was
# one decision. Marty queued two jobs and could not then do the one thing sets
# exist for: "after queing two jobs it only gave me one entry i can switch on,
# when i queued two, i need to be able to switch inbetween them."
#
# It was also unsound. Two jobs whose images overlap both landed in that one
# set at DIFFERENT SIZES FOR THE SAME IMAGE - entries the set could never
# satisfy, and a Restore list showing only the last job's size.
_queued = opt.opt_resize({"cache_dir": CACHE,
                          "jobs": [{"target": "ALL_IMAGES", "size": 256,
                                    "name": "Preview"},
                                   {"target": "ALL_IMAGES", "size": 512,
                                    "name": "Render"}]})
sets = opt.opt_status()["groups"]
ok(len(sets) == 2,
   "queue: two queued jobs make TWO sets, one each, so there is something to "
   "switch between (%d)" % len(sets))
ok([s["name"] for s in sets] == ["Preview", "Render"],
   "queue: each set is called what the job was called - the name typed into "
   "the queue row (%s)" % [s["name"] for s in sets])
ok([s["sizes"] for s in sets] == [[256], [512]],
   "queue: and each holds ONE size, its own. Overlapping jobs used to put "
   "both sizes for the same image in one set (%s)"
   % [s["sizes"] for s in sets])
ok(_queued["result"]["groups"] == ["Preview", "Render"],
   "queue: the run reports every set it made, so the app can name them back")

# And they really are alternatives, not a record of a mixture.
opt.group_apply(scene, "Preview")
_sizes = {opt.managed_size(im) for im in bpy.data.images if opt.is_managed(im)}
ok(_sizes == {256},
   "queue: switching to the first job's set puts the whole scene on that "
   "job's size (%s)" % _sizes)

_jobs = _first.get("result", {}).get("jobs")
ok(isinstance(_jobs, list) and len(_jobs) == 1,
   "queue: a plain single resize still reports as one job, so the app can "
   "report either the same way")

# A cleared cache has to be NOTICED, or someone switches to a set and nothing
# visibly happens.
_target_set = opt.opt_status()["groups"][0]
for _f in os.listdir(CACHE):
    os.remove(os.path.join(CACHE, _f))
_after = opt.opt_status()["groups"][0]
ok(_after["missing"] == _after["count"] and _after["count"] > 0,
   "sets: a cleared cache is reported as missing on the POLL, so the user is "
   "told before they try to use the set (%d/%d)"
   % (_after["missing"], _after["count"]))
opt.opt_revert_images({"target": "ALL_IMAGES"})
for _g in list(opt.group_state(scene)):
    opt.group_delete(scene, _g["name"])

# =========================================================== Save As
# ⚠ THE ONE THAT LOSES SOMEBODY'S TEXTURES IF IT REGRESSES. PROP_ORIGINAL keeps
# the path in the form the user wrote it, and a `//relative` path is resolved
# AGAINST THE .BLEND - so saving the file into a different folder makes that
# same string point somewhere else. Restore would write it back happily and the
# texture would come up missing, with the real one no longer recorded anywhere.
# Marty asked for this explicitly: "user need to always be able to restore
# original textures".
saveas_dir = os.path.join(TMP, "saveas")
saveas_src = os.path.join(saveas_dir, "textures")
os.makedirs(saveas_src, exist_ok=True)
_here = os.path.join(saveas_dir, "shot.blend")

bpy.ops.wm.read_homefile(use_empty=True)
_tex = os.path.join(saveas_src, "skin.png")
_img_src = bpy.data.images.new("saveas_src", 256, 256)
_img_src.filepath_raw = _tex
_img_src.file_format = 'PNG'
_img_src.save()
bpy.data.images.remove(_img_src)

bpy.ops.wm.save_as_mainfile(filepath=_here)
img = bpy.data.images.load(_tex)
img.filepath = bpy.path.relpath(_tex)          # `//textures/skin.png`
ok(img.filepath.startswith("//"),
   "saveas: the texture is stored RELATIVE, the way Blender writes it by "
   "default (%s)" % img.filepath)

opt.set_image_size(img, 64, os.path.join(TMP, "saveas_cache"))
ok(opt.is_managed(img), "saveas: and it optimizes normally")
ok(img.get(opt.PROP_ORIGINAL_ABS),
   "saveas: the ABSOLUTE original is stamped alongside the relative one, "
   "while the .blend is still where the relative path was written")

# Now the move that used to break it: same file, saved into another folder.
_moved_dir = os.path.join(TMP, "moved")
os.makedirs(_moved_dir, exist_ok=True)
bpy.ops.wm.save_as_mainfile(filepath=os.path.join(_moved_dir, "shot.blend"))
img = bpy.data.images.get(os.path.basename(_tex)) or img
ok(not os.path.isfile(opt._abs_path(img, img.get(opt.PROP_ORIGINAL))),
   "saveas: the stored RELATIVE path now resolves to nothing - this is the "
   "breakage, and it is silent")

_raw, _resolved = opt.resolve_original(img)
ok(os.path.isfile(_resolved),
   "saveas: resolve_original still finds the user's real file (%s)" % _resolved)

status = opt.opt_status()
entry = next((m for m in status["managed"] if m["name"] == img.name), None)
ok(entry is not None and entry.get("original_missing") is False,
   "saveas: and the status does NOT cry wolf about a missing original")

opt.revert_image(img)
ok(not opt.is_managed(img), "saveas: Restore lets go of the image")
ok(os.path.isfile(opt._abs_path(img)),
   "saveas: AND THE IMAGE POINTS AT A FILE THAT EXISTS - the whole point "
   "(%s)" % img.filepath)

# A genuinely missing original is still reported as missing - the fallback must
# not paper over a texture that really has gone.
opt.set_image_size(img, 64, os.path.join(TMP, "saveas_cache"))
_gone = os.path.join(saveas_src, "gone.png")
os.replace(_tex, _gone)
entry = next((m for m in opt.opt_status()["managed"] if m["name"] == img.name),
             None)
ok(entry is not None and entry.get("original_missing") is True,
   "saveas: a texture that really HAS been moved away is reported missing, so "
   "the fallback cannot hide a real problem")
os.replace(_gone, _tex)

bpy.ops.wm.read_homefile(use_empty=True)

# ================================================ a queued job's OWN objects
# ⚠ THE BUG MARTY ACTUALLY HIT, AND IT IS SUBTLE. "SELECTED" is resolved when a
# run STARTS, so two jobs both queued as "the selected objects" saw the same
# final selection. The second then re-sized the first one's images from their
# originals and won outright: two queued jobs, one result, one size, and one
# set. A queued job now carries the object names it was queued WITH.
qcache = os.path.join(TMP, "queued_cache")


def textured(obj_name, image_name):
    """A cube with an image only it uses, so two jobs can be told apart."""
    path = make_png(image_name, 1024, 1024)
    bpy.ops.mesh.primitive_cube_add()
    obj = bpy.context.active_object
    obj.name = obj_name
    material = bpy.data.materials.new(obj_name + "_mat")
    material.use_nodes = True
    node = material.node_tree.nodes.new("ShaderNodeTexImage")
    node.image = bpy.data.images.load(path)
    obj.data.materials.append(material)
    return obj, node.image


hero, hero_img = textured("Hero", "q_hero.png")
prop, prop_img = textured("Prop", "q_prop.png")

# What the app does the moment Add to queue is pressed: read the selection NOW.
bpy.ops.object.select_all(action='DESELECT')
hero.select_set(True)
first = opt.opt_status()["selected_objects"]
ok(first == ["Hero"],
   "queue: opt_status reports WHICH objects are selected, not just how many - "
   "the app cannot snapshot a job without that (%s)" % first)

bpy.ops.object.select_all(action='DESELECT')
prop.select_set(True)
second = opt.opt_status()["selected_objects"]
ok(second == ["Prop"], "queue: and it follows the selection (%s)" % second)

# Both jobs now run with only Prop selected. That is the trap, and the whole
# point: the first job must still act on what it was queued with.
opt.opt_resize({"cache_dir": qcache,
                "jobs": [{"target": "SELECTED", "size": 128,
                          "objects": first, "name": "Hero small"},
                         {"target": "SELECTED", "size": 512,
                          "objects": second, "name": "Prop big"}]})
ok(opt.managed_size(hero_img) == 128,
   "queue: the job queued with Hero selected resized HERO - although Hero was "
   "not selected by the time the queue ran (%d)" % opt.managed_size(hero_img))
ok(opt.managed_size(prop_img) == 512,
   "queue: and the job queued with Prop selected resized Prop, at its own "
   "size (%d)" % opt.managed_size(prop_img))
_names = [s["name"] for s in opt.opt_status()["groups"]]
ok(_names == ["Hero small", "Prop big"],
   "queue: two sets, each named from its own queue row (%s)" % _names)

# Queued, then deleted before the run. Reported, not silently skipped.
_ghost = opt.opt_resize({"cache_dir": qcache,
                         "jobs": [{"target": "SELECTED", "size": 256,
                                   "objects": ["Hero", "Ghost"],
                                   "name": "With a ghost"}]})
ok(any(s["name"] == "Ghost" for s in _ghost["result"]["skipped"]),
   "queue: an object queued and then deleted is REPORTED, so a job quietly "
   "doing less than it was asked to cannot be blamed on the optimizer")
ok(opt.managed_size(hero_img) == 256,
   "queue: and the objects that are still there are done anyway (%d)"
   % opt.managed_size(hero_img))

# ⚠ An empty list is a job with nothing to do, NOT a job with no opinion. If it
# fell through to `target` it would resize the entire scene instead.
_before = {im.name: opt.managed_size(im) for im in bpy.data.images}
opt.opt_resize({"cache_dir": qcache, "target": "ALL_IMAGES",
                "jobs": [{"target": "SELECTED", "size": 64, "objects": [],
                          "name": "Nothing"}]})
_after = {im.name: opt.managed_size(im) for im in bpy.data.images}
ok(_before == _after,
   "queue: a job queued with nothing selected does NOTHING - it must never "
   "fall through to the target and resize the whole scene")

# ================================================== clearing the cache folder
# Marty: "in Restore make sure to add a button to 'clear cache folder'".
# Two rules, and both are here because both can hurt somebody.
_stranger = os.path.join(qcache, "my_holiday_photo.png")
shutil.copyfile(make_png("q_stranger.png", 64, 64), _stranger)
_ours = [f for f in os.listdir(qcache) if f != os.path.basename(_stranger)]
ok(len(_ours) > 0,
   "clear: there are stand-ins in the cache to clear (%d)" % len(_ours))

# Every shape `standin_path` can produce has to be recognised, or files are
# left behind — and nothing else may be, or files are destroyed.
for _made in (opt.standin_path(qcache, "/t/skin.png", 512),
              opt.standin_path(qcache, "/t/shot_0007.png", 512, frame=7),
              opt.standin_path(qcache, "/t/skin.<UDIM>.png", 512),
              opt._tile_destination(qcache, "/t/skin.<UDIM>.png", 512,
                                    "/t/skin.1002.png", "<UDIM>")):
    ok(opt._STANDIN_NAME.match(os.path.basename(_made)) is not None,
       "clear: the delete pattern recognises our own %s"
       % os.path.basename(_made))
for _bad in ("my_holiday_photo.png", "skin.png", "0123abc_512.png",
             "a" * 64 + ".png", "render.exr"):
    ok(opt._STANDIN_NAME.match(_bad) is None,
       "clear: and does NOT recognise %r, which is somebody's own file" % _bad)

_cleared = opt.opt_clear_cache({"cache_dir": qcache})["cache"]
ok(len(opt.opt_status()["managed"]) == 0,
   "clear: EVERY texture is put back before anything is deleted - they point "
   "AT the files being removed, so that part is not optional")
ok(os.path.isfile(opt._abs_path(hero_img))
   and os.path.isfile(opt._abs_path(prop_img)),
   "clear: and every image ends up on a file that exists")
ok(os.path.isfile(_stranger),
   "clear: A FILE WE DID NOT WRITE IS LEFT ALONE. The cache folder is a path "
   "the user can type into a box, and one day it will be a real folder of "
   "theirs")
ok(_cleared["kept"] == 1,
   "clear: and it is counted rather than passed over in silence (%d)"
   % _cleared["kept"])
ok(_cleared["removed"] == len(_ours),
   "clear: every stand-in is gone (%d of %d)"
   % (_cleared["removed"], len(_ours)))
ok(_cleared["bytes"] > 0 and _cleared["restored"] > 0,
   "clear: it reports the space freed and how many textures it put back")
ok(len(opt.opt_status()["groups"]) > 0,
   "clear: the texture sets SURVIVE - a set is a note about sizes, and it "
   "re-makes its files from the originals when you switch to it")

# Clearing a folder that is not there at all must be a no-op, not a traceback.
_missing = opt.clear_cache(os.path.join(TMP, "never_existed"))
ok(_missing["removed"] == 0 and _missing["kept"] == 0,
   "clear: a cache folder that does not exist clears to nothing quietly")

bpy.ops.wm.read_homefile(use_empty=True)

# =========================================================== teardown
shutil.rmtree(TMP, ignore_errors=True)

print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
for label in FAIL:
    print("  FAILED: " + label)
