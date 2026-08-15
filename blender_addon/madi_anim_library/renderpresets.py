# MADI Anim Library — render presets engine (add-on 0.17.0).
#
# Reads and writes a declared catalogue of render settings so the app can save
# them under a name and put them back later — Blender's own preset system
# (which is literally a .py file full of `scene.cycles.samples = 128` lines),
# but driven from the Toolset's UI and stored as JSON.
#
# Kept OUT of core.py for the same reason as jiggle/picker/optimizer: it
# shares nothing with the library logic and core.py is already 228 KB.
#
# THE TWO RULES THIS MODULE EXISTS TO ENFORCE
#
# 1. ⚠ ONLY A PATH IN THE CATALOGUE IS EVER WRITTEN. The app hands back a dict
#    it read out of a JSON file on disk — a file a user can open and edit — and
#    `apply()` is a `setattr` loop. Without the whitelist, an edited preset
#    could poke anything reachable from a Scene. Anything unknown lands in
#    `rejected` and is skipped, never written.
# 2. ⚠ ONE BAD VALUE MUST NEVER TAKE THE WHOLE APPLY DOWN. Every path is
#    written in its own try/except: a property this Blender does not have, a
#    read-only one, an enum item that was renamed between versions — each is
#    one line in `failed`, and the other ninety settings still land. This is the
#    same lesson as `core.abc_options()`: these values become live API writes,
#    so they get sanitised, not trusted.
#
# ORDER IS PART OF THE DATA. The catalogue is a list, not a dict, and
# `_PRIORITY` runs ahead of it, because several of these settings only take
# when something else is set first:
#   * `render.engine` first — the engine decides which of the rest even apply.
#   * `image_settings.media_type` before `file_format` (5.x gates the format
#     enum on the media type — the same trap the playblast hit; BLENDER_NOTES).
#   * `display_settings.display_device` before `view_settings.view_transform`
#     before `look` — a look belongs to a view transform, so setting them the
#     other way round raises "enum item not found" on a perfectly good preset.

import bpy

# Written before the catalogue, whatever order the caller sends. Everything
# here must also appear in a group below, or it would never be captured.
_PRIORITY = (
    "render.engine",
    "render.image_settings.media_type",
    "display_settings.display_device",
    "view_settings.view_transform",
)

# key, label, default-ticked in the app's save dialog, paths (in apply order).
#
# ⚠ `default` False is not decoration: those two groups are per-SHOT settings
# living inside a per-LOOK feature. A preset that silently retimed the scene or
# repointed the output folder would be reported as a bug, and rightly.
GROUPS = [
    {
        "key": "engine",
        "label": "Engine & device",
        "default": True,
        "paths": [
            "render.engine",
            "cycles.device",
        ],
    },
    {
        "key": "sampling",
        "label": "Sampling (Cycles)",
        "default": True,
        "paths": [
            "cycles.samples",
            "cycles.preview_samples",
            "cycles.use_adaptive_sampling",
            "cycles.adaptive_threshold",
            "cycles.adaptive_min_samples",
            "cycles.use_preview_adaptive_sampling",
            "cycles.preview_adaptive_threshold",
            "cycles.time_limit",
            "cycles.sampling_pattern",
            "cycles.scrambling_distance",
            "cycles.seed",
            "cycles.use_animated_seed",
            "cycles.sample_offset",
            "cycles.use_light_tree",
            "cycles.light_sampling_threshold",
            "cycles.min_light_bounces",
            "cycles.min_transparent_bounces",
        ],
    },
    {
        "key": "denoise",
        "label": "Denoising (Cycles)",
        "default": True,
        "paths": [
            "cycles.use_denoising",
            "cycles.denoiser",
            "cycles.denoising_input_passes",
            "cycles.denoising_prefilter",
            "cycles.denoising_quality",
            "cycles.denoising_use_gpu",
            "cycles.use_preview_denoising",
            "cycles.preview_denoiser",
            "cycles.preview_denoising_input_passes",
            "cycles.preview_denoising_prefilter",
            "cycles.preview_denoising_start_sample",
        ],
    },
    {
        "key": "light_paths",
        "label": "Light paths (Cycles)",
        "default": True,
        "paths": [
            "cycles.max_bounces",
            "cycles.diffuse_bounces",
            "cycles.glossy_bounces",
            "cycles.transmission_bounces",
            "cycles.volume_bounces",
            "cycles.transparent_max_bounces",
            "cycles.caustics_reflective",
            "cycles.caustics_refractive",
            "cycles.blur_glossy",
            "cycles.use_fast_gi",
            "cycles.sample_clamp_direct",
            "cycles.sample_clamp_indirect",
        ],
    },
    {
        "key": "eevee",
        "label": "EEVEE",
        "default": True,
        "paths": [
            "eevee.taa_render_samples",
            "eevee.taa_samples",
            "eevee.use_taa_reprojection",
            "eevee.use_shadows",
            "eevee.shadow_ray_count",
            "eevee.shadow_step_count",
            "eevee.shadow_resolution_scale",
            "eevee.use_raytracing",
            "eevee.ray_tracing_method",
            "eevee.use_fast_gi",
            "eevee.fast_gi_method",
            "eevee.fast_gi_resolution",
            "eevee.fast_gi_ray_count",
            "eevee.fast_gi_step_count",
            "eevee.fast_gi_distance",
            "eevee.use_volumetric_shadows",
            "eevee.volumetric_tile_size",
            "eevee.volumetric_samples",
            "eevee.volumetric_start",
            "eevee.volumetric_end",
            "eevee.clamp_surface_direct",
            "eevee.clamp_surface_indirect",
            "eevee.clamp_volume_direct",
            "eevee.clamp_volume_indirect",
            "eevee.use_overscan",
            "eevee.overscan_size",
        ],
    },
    {
        "key": "film",
        "label": "Film & motion blur",
        "default": True,
        "paths": [
            "render.film_transparent",
            "render.filter_size",
            "cycles.film_exposure",
            "cycles.pixel_filter_type",
            "render.use_motion_blur",
            "render.motion_blur_shutter",
            "render.motion_blur_position",
            "cycles.rolling_shutter_type",
            "cycles.rolling_shutter_duration",
            "eevee.motion_blur_steps",
        ],
    },
    {
        "key": "performance",
        "label": "Performance",
        "default": True,
        "paths": [
            "cycles.use_auto_tile",
            "cycles.tile_size",
            "cycles.debug_use_spatial_splits",
            "cycles.debug_bvh_type",
            "cycles.use_camera_cull",
            "cycles.use_distance_cull",
            "render.threads_mode",
            "render.threads",
            "render.use_persistent_data",
        ],
    },
    {
        "key": "format",
        "label": "Resolution & frame rate",
        "default": True,
        "paths": [
            "render.resolution_x",
            "render.resolution_y",
            "render.resolution_percentage",
            "render.pixel_aspect_x",
            "render.pixel_aspect_y",
            "render.fps",
            "render.fps_base",
            "render.use_border",
            "render.use_crop_to_border",
        ],
    },
    {
        "key": "output",
        "label": "Output & file format",
        "default": True,
        "paths": [
            # media_type FIRST — see _PRIORITY. It is repeated there rather
            # than moved, because a group has to be able to capture it.
            "render.image_settings.media_type",
            "render.image_settings.file_format",
            "render.image_settings.color_mode",
            "render.image_settings.color_depth",
            "render.image_settings.compression",
            "render.image_settings.quality",
            "render.image_settings.exr_codec",
            "render.image_settings.tiff_codec",
            "render.image_settings.jpeg2k_codec",
            "render.image_settings.use_preview",
            "render.image_settings.color_management",
            "render.image_settings.views_format",
            "render.use_file_extension",
            "render.use_overwrite",
            "render.use_placeholder",
            "render.use_render_cache",
        ],
    },
    {
        "key": "ffmpeg",
        "label": "Video encoding",
        "default": True,
        "paths": [
            "render.ffmpeg.format",
            "render.ffmpeg.codec",
            "render.ffmpeg.constant_rate_factor",
            "render.ffmpeg.ffmpeg_preset",
            "render.ffmpeg.ffmpeg_prores_profile",
            "render.ffmpeg.gopsize",
            "render.ffmpeg.use_max_b_frames",
            "render.ffmpeg.max_b_frames",
            "render.ffmpeg.video_bitrate",
            "render.ffmpeg.minrate",
            "render.ffmpeg.maxrate",
            "render.ffmpeg.buffersize",
            "render.ffmpeg.use_autosplit",
            "render.ffmpeg.use_lossless_output",
            "render.ffmpeg.audio_codec",
            "render.ffmpeg.audio_bitrate",
            "render.ffmpeg.audio_channels",
            "render.ffmpeg.audio_mixrate",
            "render.ffmpeg.audio_volume",
        ],
    },
    {
        "key": "color",
        "label": "Colour management",
        "default": True,
        "paths": [
            "display_settings.display_device",
            "view_settings.view_transform",
            "view_settings.look",
            "view_settings.exposure",
            "view_settings.gamma",
            "view_settings.use_curve_mapping",
            "view_settings.use_white_balance",
            "view_settings.white_balance_temperature",
            "view_settings.white_balance_tint",
            "sequencer_colorspace_settings.name",
        ],
    },
    {
        "key": "simplify",
        "label": "Simplify",
        "default": True,
        "paths": [
            "render.use_simplify",
            "render.simplify_subdivision",
            "render.simplify_subdivision_render",
            "render.simplify_child_particles",
            "render.simplify_child_particles_render",
            "render.simplify_volumes",
            "render.use_simplify_normals",
            "render.simplify_gpencil",
            "render.simplify_gpencil_antialiasing",
            "cycles.texture_limit",
            "cycles.texture_limit_render",
        ],
    },
    {
        "key": "post",
        "label": "Post processing",
        "default": True,
        "paths": [
            "render.use_compositing",
            "render.use_sequencer",
            "render.dither_intensity",
            "render.hair_type",
            "render.hair_subdiv",
            "render.use_high_quality_normals",
            "render.use_freestyle",
            "render.use_stamp",
        ],
    },
    {
        "key": "filepath",
        "label": "Output path",
        "default": False,
        "paths": [
            "render.filepath",
        ],
    },
    {
        "key": "frames",
        "label": "Frame range",
        "default": False,
        "paths": [
            "frame_start",
            "frame_end",
            "frame_step",
        ],
    },
]

# path -> group key, built once. Also the whitelist apply() checks against.
_PATH_GROUP = {}
for _grp in GROUPS:
    for _path in _grp["paths"]:
        _PATH_GROUP.setdefault(_path, _grp["key"])

# Only these come back out of Blender. Everything in the catalogue is one of
# them today; the guard is here so that adding a vector or a pointer property
# to a group shows up as a skip rather than as JSON that will not serialise.
_SIMPLE = (bool, int, float, str)


def _scene():
    return bpy.context.scene


def _resolve(scene, path):
    """(owner, attribute) for a dotted path, or None if any step is missing.

    `scene.cycles` is absent when the Cycles add-on is disabled and every
    `eevee.*` name moved in 4.2, so a missing step is ordinary, not an error.
    """
    owner = scene
    parts = path.split(".")
    for part in parts[:-1]:
        owner = getattr(owner, part, None)
        if owner is None:
            return None
    if not hasattr(owner, parts[-1]):
        return None
    return owner, parts[-1]


def _coerce(current, value):
    """Cast a JSON value to the type Blender already holds at that path.

    JSON has one number type, so a preset written for an int property comes
    back as a float the moment anything re-serialises it — and Blender raises
    `TypeError: expected an int` rather than rounding. bool is checked first
    because it is a subclass of int.
    """
    if isinstance(current, bool):
        return bool(value)
    if isinstance(current, int):
        return int(value)
    if isinstance(current, float):
        return float(value)
    if isinstance(current, str):
        return str(value)
    return value


def schema():
    """The catalogue, without values — what the app builds its tick list from."""
    return {
        "groups": [{"key": g["key"], "label": g["label"],
                    "default": g["default"], "count": len(g["paths"])}
                   for g in GROUPS],
    }


def capture(groups=None):
    """Read the scene's render settings.

    `groups` = the group keys to read, or None for every one. Paths this
    Blender does not have are listed in `skipped` and simply left out — that is
    how one preset file stays readable across versions and both engines.
    """
    scene = _scene()
    wanted = None if groups is None else set(groups)
    out = {}
    skipped = []
    for grp in GROUPS:
        if wanted is not None and grp["key"] not in wanted:
            continue
        values = {}
        for path in grp["paths"]:
            found = _resolve(scene, path)
            if found is None:
                skipped.append({"path": path, "reason": "not in this Blender"})
                continue
            value = getattr(*found)
            if not isinstance(value, _SIMPLE):
                skipped.append({"path": path, "reason": "unsupported type %s"
                                % type(value).__name__})
                continue
            values[path] = value
        out[grp["key"]] = {"label": grp["label"], "values": values}
    return {
        "blender": bpy.app.version_string,
        "scene": scene.name,
        "engine": scene.render.engine,
        "groups": out,
        "skipped": skipped,
    }


def _ordered(paths):
    """Catalogue order, with the must-go-first paths ahead of everything."""
    todo = list(paths)
    order = [p for p in _PRIORITY if p in todo]
    seen = set(order)
    for grp in GROUPS:
        for path in grp["paths"]:
            if path in todo and path not in seen:
                order.append(path)
                seen.add(path)
    # Anything left is not in the catalogue; apply() rejects it, but it still
    # has to reach the loop to be reported rather than silently dropped.
    order.extend(p for p in todo if p not in seen)
    return order


def apply(data, groups=None):
    """Write a captured preset back onto the scene.

    `data` is what `capture` returned (or the JSON the app stored from it), and
    `groups` narrows it further. Nothing outside the catalogue is written; a
    path that fails is one entry in `failed` and the rest still land.
    """
    scene = _scene()
    wanted = None if groups is None else set(groups)
    stored = (data or {}).get("groups") or {}

    flat = {}
    for key, block in stored.items():
        if wanted is not None and key not in wanted:
            continue
        for path, value in (block.get("values") or {}).items():
            flat[path] = value

    applied, unchanged = [], []
    skipped, failed, rejected = [], [], []
    for path in _ordered(flat):
        if path not in _PATH_GROUP:
            # ⚠ The whitelist. A preset file is editable text on disk and this
            # is a setattr loop — nothing the catalogue does not name is ever
            # written, however it got into the file.
            rejected.append(path)
            continue
        found = _resolve(scene, path)
        if found is None:
            skipped.append({"path": path, "reason": "not in this Blender"})
            continue
        owner, attr = found
        try:
            current = getattr(owner, attr)
            value = _coerce(current, flat[path])
            if current == value:
                unchanged.append(path)
                continue
            setattr(owner, attr, value)
        except Exception as exc:            # noqa: BLE001 — see rule 2 up top
            failed.append({"path": path, "reason": str(exc)})
            continue
        applied.append(path)

    return {
        "scene": scene.name,
        "engine": scene.render.engine,
        "applied": applied,
        "unchanged": unchanged,
        "skipped": skipped,
        "failed": failed,
        "rejected": rejected,
        "summary": _summary(applied, unchanged, skipped, failed, rejected),
    }


def _summary(applied, unchanged, skipped, failed, rejected):
    bits = ["%d setting%s changed" % (len(applied),
                                      "" if len(applied) == 1 else "s")]
    if unchanged:
        bits.append("%d already matched" % len(unchanged))
    if skipped:
        bits.append("%d not in this Blender" % len(skipped))
    if failed:
        bits.append("%d refused" % len(failed))
    if rejected:
        bits.append("%d unknown setting(s) ignored" % len(rejected))
    return ", ".join(bits) + "."
