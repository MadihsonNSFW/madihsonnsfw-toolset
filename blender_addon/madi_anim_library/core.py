# MADI Anim Library — core library logic (no UI).
# Library on disk mirrors Studio Library: plain folders for navigation, item
# folders named  <name>.pose / <name>.set  containing json (+ thumbnail/sequence later).

import bpy
import fnmatch
import hashlib
import json
import math
import os
import random
import re
import shutil
import struct
import tempfile
import time
import zlib

import numpy as np

POSE_EXT = ".pose"
SET_EXT = ".set"
ANIM_EXT = ".anim"
MIRROR_EXT = ".mirror"
SHAPES_EXT = ".shapes"
REMAP_EXT = ".remap"
ABC_EXT = ".abc"
PICKER_EXT = ".picker"
VGROUPS_EXT = ".vgroups"
RENDERPRESET_EXT = ".renderpreset"
# ⚠ A NEW ITEM TYPE GOES IN **THREE** LISTS, NOT TWO — and the third is the one
# that fails silently:
#   1. this tuple                     (the add-on's scan)
#   2. app\library.py ITEM_EXTS       (the app scans the same tree, no bridge)
#   3. app\panels.py Sidebar.type_checks  ← ⚠ SILENT. `LibraryView.refilter`
#      drops any item whose type has no checkbox, so the item saves, scans and
#      round-trips perfectly and is simply never drawn. `.vgroups` and `.picker`
#      were both invisible for exactly this reason until 2026-08-05.
# `tests\app_picker_test.py` asserts 1 == 2; `tests\app_vgroups_test.py`
# asserts 3 covers 2.
ITEM_EXTS = (POSE_EXT, SET_EXT, ANIM_EXT, MIRROR_EXT, SHAPES_EXT, REMAP_EXT,
             ABC_EXT, PICKER_EXT, VGROUPS_EXT, RENDERPRESET_EXT)

FORMAT_VERSION = 1

# Add-on version reported over the bridge (ping/status) so the app can warn
# when the installed extension is older than the app expects — the classic
# "rebuilt the exe but forgot to reinstall the add-on" confusion.
# MUST match blender_manifest.toml's `version`; tests\bridge_version_test.py
# asserts that, and app\bridge.py carries the version the app expects.
ADDON_VERSION = "0.52.1"

_INVALID_FS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


# ---------------------------------------------------------------- helpers

def safe_name(name):
    name = _INVALID_FS.sub("_", name).strip().rstrip(".")
    return name or "untitled"


VERSIONS_DIR = "versions"
_PAYLOAD_FILES = ("pose.json", "set.json", "anim.json", "mirror.json",
                  "shapes.json", "remap.json", "abc.json", "thumbnail.jpg")
_VERSION_RE = re.compile(r"v\d+$")


def version_item(item_dir):
    """Move the item's current payload (json + previews + shape-key .bin
    sidecars) into versions/vNNN. Called before every overwrite-save so no
    version of an item is ever lost. The app reads/restores these folders
    directly — keep the layout in sync with library.py on the app side.
    Returns the version dir or None."""
    files = [f for f in _PAYLOAD_FILES if os.path.isfile(os.path.join(item_dir, f))]
    files += [f for f in os.listdir(item_dir) if f.endswith((".bin", ".abc"))]
    has_seq = os.path.isdir(os.path.join(item_dir, "sequence"))
    if not files and not has_seq:
        return None
    vroot = os.path.join(item_dir, VERSIONS_DIR)
    os.makedirs(vroot, exist_ok=True)
    nums = [int(d[1:]) for d in os.listdir(vroot) if _VERSION_RE.match(d)]
    vdir = os.path.join(vroot, "v%03d" % (max(nums) + 1 if nums else 1))
    os.makedirs(vdir)
    for f in files:
        shutil.move(os.path.join(item_dir, f), os.path.join(vdir, f))
    if has_seq:
        shutil.move(os.path.join(item_dir, "sequence"), os.path.join(vdir, "sequence"))
    return vdir


def bone_is_selected(pb):
    # Blender 5.2 moved the select flag onto PoseBone; older builds keep it on Bone.
    if hasattr(pb, "select"):
        return pb.select
    return pb.bone.select


def bone_set_selected(pb, value):
    if hasattr(pb, "select"):
        pb.select = value
    else:
        pb.bone.select = value


def get_armature(context=None):
    ob = (context or bpy.context).active_object
    if ob is None or ob.type != 'ARMATURE':
        raise RuntimeError("Active object is not an armature")
    return ob


def _pose_bones_to_save(ob, use_selected=True):
    bones = list(ob.pose.bones)
    if use_selected:
        sel = [pb for pb in bones if bone_is_selected(pb)]
        if sel:
            return sel
    return bones


def _custom_props(pb):
    props = {}
    for key in pb.keys():
        if key.startswith("_"):
            continue
        val = pb[key]
        if isinstance(val, (int, float, bool, str)):
            props[key] = val
        elif hasattr(val, "to_list"):
            lst = val.to_list()
            if all(isinstance(v, (int, float)) for v in lst):
                props[key] = lst
    return props


def item_type(path):
    for ext in ITEM_EXTS:
        if path.endswith(ext):
            return ext[1:]
    return None


def list_items(library_root):
    """Walk the library; return items + plain folders (for the app / panel enum)."""
    items = []
    if not os.path.isdir(library_root):
        return items
    for root, dirs, _files in os.walk(library_root):
        keep = []
        for d in dirs:
            full = os.path.join(root, d)
            typ = item_type(d)
            if typ:
                items.append({
                    "path": full,
                    "relpath": os.path.relpath(full, library_root),
                    "name": os.path.splitext(d)[0],
                    "type": typ,
                })
            else:
                keep.append(d)
        dirs[:] = keep  # don't descend into item folders
    return items


def _metadata(ob, extra=None):
    rd = bpy.context.scene.render
    meta = {
        "format_version": FORMAT_VERSION,
        "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "author": os.environ.get("USERNAME") or os.environ.get("USER") or "",
        "blender_version": bpy.app.version_string,
        # Blank rather than a crash: an Alembic export of the WHOLE scene has no
        # single source object to name, and metadata must never be the thing
        # that fails an export that otherwise worked.
        "source_armature": ob.name if ob is not None else "",
        "source_file": bpy.data.filepath,
        "fps": round(rd.fps / rd.fps_base, 3),  # scene rate the item was made at
    }
    if extra:
        meta.update(extra)
    return meta


# ---------------------------------------------------------------- poses

def save_pose(library_root, relfolder, name, use_selected=True, description="",
              overwrite=False):
    """Save the active armature's pose as <library>/<relfolder>/<name>.pose/pose.json."""
    ob = get_armature()
    bones = _pose_bones_to_save(ob, use_selected)

    data = {"type": "pose", "metadata": _metadata(ob, {"description": description}),
            "bones": {}}
    for pb in bones:
        entry = {
            "rotation_mode": pb.rotation_mode,
            "location": list(pb.location),
            "scale": list(pb.scale),
        }
        if pb.rotation_mode == 'QUATERNION':
            entry["rotation_quaternion"] = list(pb.rotation_quaternion)
        elif pb.rotation_mode == 'AXIS_ANGLE':
            entry["rotation_axis_angle"] = list(pb.rotation_axis_angle)
        else:
            entry["rotation_euler"] = list(pb.rotation_euler)
        props = _custom_props(pb)
        if props:
            entry["props"] = props
        data["bones"][pb.name] = entry

    item_dir = os.path.join(library_root, relfolder, safe_name(name) + POSE_EXT)
    if os.path.isdir(item_dir):
        if not overwrite:
            raise RuntimeError("Item already exists: %s (use overwrite)" % item_dir)
        version_item(item_dir)
    os.makedirs(item_dir, exist_ok=True)
    with open(os.path.join(item_dir, "pose.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
    return {"path": item_dir, "bones": len(data["bones"])}


def load_pose_file(item_path):
    with open(os.path.join(item_path, "pose.json"), "r", encoding="utf-8") as f:
        return json.load(f)


def _lerp(a, b, t):
    return a + (b - a) * t


def apply_pose(item_path, selected_only=False, blend=1.0, key=False,
               mirror=False, mirror_table=None, remap_table=None):
    """Apply a saved pose to the active armature by bone-name match.

    blend: 0..1 factor between the CURRENT channel values and the saved ones
    (a live drag session caches its own base — see server begin/set/end_blend).
    mirror: flip the pose L<->R using *mirror_table* (a .mirror item path) or
    auto-detected pairs.
    remap_table: a .remap item path — item bone names are resolved through it
    BEFORE the pb lookup (and before mirror, which then runs on the target rig).
    """
    from mathutils import Quaternion

    ob = get_armature()
    data = load_pose_file(item_path)
    if data.get("type") != "pose":
        raise RuntimeError("Not a pose item: %s" % item_path)

    bones_data = data["bones"]
    remapped = 0
    if remap_table:
        resolve = _remap_resolver(remap_table)
        renamed = {}
        for n, entry in bones_data.items():
            t = resolve(n)
            if t != n:
                remapped += 1
            renamed.setdefault(t, entry)  # two sources on one target: first wins
        bones_data = renamed
    mirror_skipped = []
    if mirror:
        prop_flip = None
        if mirror_table:
            mdata = load_mirror_file(mirror_table)
            mirror_map = mdata.get("map", {})
            prop_flip = _prop_flip_matcher(mdata.get("prop_flips"))
        else:
            mirror_map, _center, _un = build_mirror_map(ob)
        bones_data, mirror_skipped = mirror_entries(ob, bones_data, mirror_map,
                                                    prop_flip=prop_flip)

    applied = 0
    missing = []
    keyed_paths = []
    for name, entry in bones_data.items():
        pb = ob.pose.bones.get(name)
        if pb is None:
            missing.append(name)
            continue
        if selected_only and not bone_is_selected(pb):
            continue

        pb.rotation_mode = entry["rotation_mode"]
        t = max(0.0, min(1.0, blend))

        loc = entry["location"]
        pb.location = [_lerp(pb.location[i], loc[i], t) for i in range(3)]
        scl = entry["scale"]
        pb.scale = [_lerp(pb.scale[i], scl[i], t) for i in range(3)]

        if "rotation_quaternion" in entry:
            target = Quaternion(entry["rotation_quaternion"])
            pb.rotation_quaternion = pb.rotation_quaternion.slerp(target, t)
            rot_path = "rotation_quaternion"
        elif "rotation_axis_angle" in entry:
            aa = entry["rotation_axis_angle"]
            cur = list(pb.rotation_axis_angle)
            pb.rotation_axis_angle = [_lerp(cur[i], aa[i], t) for i in range(4)]
            rot_path = "rotation_axis_angle"
        else:
            eul = entry["rotation_euler"]
            cur = pb.rotation_euler
            pb.rotation_euler = [_lerp(cur[i], eul[i], t) for i in range(3)]
            rot_path = "rotation_euler"

        for pkey, pval in entry.get("props", {}).items():
            try:
                if t >= 1.0 or not isinstance(pval, (int, float)):
                    pb[pkey] = pval
                else:
                    pb[pkey] = _lerp(float(pb.get(pkey, pval)), float(pval), t)
            except Exception:
                pass

        if key:
            for path in ("location", "scale", rot_path):
                pb.keyframe_insert(path, group=name)
            for pkey in entry.get("props", {}):
                try:
                    pb.keyframe_insert('["%s"]' % pkey, group=name)
                except Exception:
                    pass
            keyed_paths.append(name)
        applied += 1

    bpy.context.view_layer.update()
    return {"applied": applied, "missing": len(missing),
            "missing_names": missing[:20], "keyed": len(keyed_paths),
            "mirror_skipped": len(mirror_skipped), "remapped": remapped}


# ---------------------------------------------------------------- mirror tables

# Daz G8-style prefixes ("lShldrBend"/"rShldrBend") — bpy.utils.flip_name can't
# flip these (no separator); verified on the Lily rig 2026-07-31.
_L_PREFIX = re.compile(r'^l(?=[A-Z])')
_R_PREFIX = re.compile(r'^r(?=[A-Z])')
_WORD_SWAPS = (("Left", "Right"), ("left", "right"), ("LEFT", "RIGHT"))


def flip_bone_name(name):
    """Best-effort mirrored counterpart name. Returns *name* itself for centers."""
    flipped = bpy.utils.flip_name(name)
    if flipped != name:
        return flipped
    if _L_PREFIX.match(name):
        return "r" + name[1:]
    if _R_PREFIX.match(name):
        return "l" + name[1:]
    for a, b in _WORD_SWAPS:
        if a in name:
            return name.replace(a, b, 1)
        if b in name:
            return name.replace(b, a, 1)
    return name


def build_mirror_map(ob):
    """Auto-detect pairs on an armature. Returns (map, center, unmatched):
    map has BOTH directions (l->r and r->l); center bones map to themselves."""
    names = {pb.name for pb in ob.pose.bones}
    mapping = {}
    center = []
    unmatched = []
    for n in sorted(names):
        f = flip_bone_name(n)
        if f == n:
            center.append(n)
        elif f in names:
            mapping[n] = f
        else:
            unmatched.append(n)
    return mapping, center, unmatched


def save_mirror(library_root, relfolder, name, description="", overwrite=False):
    ob = get_armature()
    mapping, center, unmatched = build_mirror_map(ob)
    data = {"type": "mirror",
            "metadata": _metadata(ob, {"description": description}),
            "map": mapping, "center": center, "unmatched": unmatched,
            "prop_flips": []}  # custom-prop patterns negated on mirror (app edits)
    item_dir = os.path.join(library_root, relfolder, safe_name(name) + MIRROR_EXT)
    if os.path.isdir(item_dir):
        if not overwrite:
            raise RuntimeError("Item already exists: %s (use overwrite)" % item_dir)
        version_item(item_dir)
    os.makedirs(item_dir, exist_ok=True)
    with open(os.path.join(item_dir, "mirror.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
    return {"path": item_dir, "pairs": len(mapping) // 2, "center": len(center),
            "unmatched": len(unmatched)}


def load_mirror_file(item_path):
    with open(os.path.join(item_path, "mirror.json"), "r", encoding="utf-8") as f:
        return json.load(f)


def _compose_entry_matrix(entry):
    from mathutils import Matrix, Quaternion, Euler, Vector
    loc = Vector(entry["location"])
    scale = Vector(entry["scale"])
    if "rotation_quaternion" in entry:
        rot = Quaternion(entry["rotation_quaternion"])
    elif "rotation_axis_angle" in entry:
        aa = entry["rotation_axis_angle"]
        rot = Quaternion((aa[1], aa[2], aa[3]), aa[0])
    else:
        rot = Euler(entry["rotation_euler"], entry["rotation_mode"]).to_quaternion()
    return Matrix.LocRotScale(loc, rot, scale)


def _entry_from_matrix(m, rotation_mode, props=None):
    loc, quat, scale = m.decompose()
    entry = {"rotation_mode": rotation_mode,
             "location": list(loc), "scale": list(scale)}
    if rotation_mode == 'QUATERNION':
        entry["rotation_quaternion"] = list(quat)
    elif rotation_mode == 'AXIS_ANGLE':
        axis, angle = quat.to_axis_angle()
        entry["rotation_axis_angle"] = [angle, axis[0], axis[1], axis[2]]
    else:
        entry["rotation_euler"] = list(quat.to_euler(rotation_mode))
    if props:
        entry["props"] = props
    return entry


def _prop_flip_matcher(patterns):
    """Case-insensitive fnmatch matcher for a mirror table's prop_flips
    patterns ('*twist*', 'Bend'). Empty/None -> never matches."""
    pats = [p.lower() for p in patterns or () if p]
    if not pats:
        return None
    return lambda name: any(fnmatch.fnmatch(name.lower(), p) for p in pats)


def _flip_props(props, prop_flip):
    if not props or prop_flip is None:
        return props
    return {k: (-v if prop_flip(k) and isinstance(v, (int, float))
                and not isinstance(v, bool) else v)
            for k, v in props.items()}


def mirror_entries(ob, bones_data, mirror_map, prop_flip=None):
    """Mirror saved pose entries across armature X using REST matrices:
    M_tgt = C_tgt^-1 · X · (C_src · M_src · C_src^-1) · X · C_tgt
    Exact for properly mirrored rigs — no per-rig calibration needed.
    prop_flip: matcher from the table's prop_flips — matching numeric custom
    props are NEGATED on the way across (twist-style props).
    Returns (mirrored_bones_dict, skipped_names)."""
    from mathutils import Matrix

    X = Matrix.Scale(-1.0, 4, (1.0, 0.0, 0.0))
    out = {}
    skipped = []
    for name, entry in bones_data.items():
        target = mirror_map.get(name, name if flip_bone_name(name) == name else None)
        if target is None:
            skipped.append(name)
            continue
        src_rest = ob.data.bones.get(name)
        tgt_rest = ob.data.bones.get(target)
        if src_rest is None or tgt_rest is None:
            skipped.append(name)
            continue
        C_src = src_rest.matrix_local
        C_tgt = tgt_rest.matrix_local
        M_src = _compose_entry_matrix(entry)
        P = C_src @ M_src @ C_src.inverted()
        M_tgt = C_tgt.inverted() @ (X @ P @ X) @ C_tgt
        out[target] = _entry_from_matrix(M_tgt, entry["rotation_mode"],
                                         _flip_props(entry.get("props"), prop_flip))
    return out, skipped


_TRANSFORM_CHANNELS = {"location": 3, "rotation_quaternion": 4,
                       "rotation_euler": 3, "rotation_axis_angle": 4, "scale": 3}
_CHANNEL_DEFAULTS = {"location": (0.0, 0.0, 0.0), "scale": (1.0, 1.0, 1.0),
                     "rotation_quaternion": (1.0, 0.0, 0.0, 0.0),
                     "rotation_euler": (0.0, 0.0, 0.0),
                     "rotation_axis_angle": (0.0, 0.0, 1.0, 0.0)}


def _sample_curve_keys(keys, t):
    """Value of a saved key list at frame t (exact key, else linear interp,
    else endpoint hold). keys are sorted by frame (fcurve order)."""
    lo = None
    for k in keys:
        if k[0] == t:
            return k[1]
        if k[0] < t:
            lo = k
        else:
            if lo is None:
                return k[1]
            return lo[1] + (k[1] - lo[1]) * ((t - lo[0]) / (k[0] - lo[0]))
    return lo[1] if lo is not None else 0.0


_PROP_PATH = re.compile(r'\["((?:[^"\\]|\\.)*)"\]$')


def _negate_prop_key(k):
    """Negate a saved key's value + handle Y values (indices 1 / 6 / 9)."""
    k = list(k)
    k[1] = -k[1]
    if len(k) > 6 and k[6] is not None:
        k[6] = -k[6]
    if len(k) > 9 and k[9] is not None:
        k[9] = -k[9]
    return k


def mirror_anim_curves(ob, data, mirror_map, prop_flip=None):
    """Mirror saved anim curves L<->R by sampling each bone's transform at every
    key time and conjugating through the rest matrices (same math as poses).
    Baked keys get BEZIER/AUTO_CLAMPED handles (source handle shapes are not
    carried across the mirror). Custom-prop curves whose prop name matches
    prop_flip get their key values negated. Returns (curves, bones_meta,
    skipped)."""
    from mathutils import Matrix

    X = Matrix.Scale(-1.0, 4, (1.0, 0.0, 0.0))
    bones_meta_src = data.get("bones", {})
    groups = {}
    for cur in data["curves"]:
        groups.setdefault(cur["bone"], []).append(cur)

    out_curves = []
    out_bones = {}
    skipped = []
    for bone, curves in groups.items():
        target = mirror_map.get(bone, bone if flip_bone_name(bone) == bone else None)
        src_rest = ob.data.bones.get(bone)
        tgt_rest = ob.data.bones.get(target) if target else None
        if target is None or src_rest is None or tgt_rest is None:
            skipped.append(bone)
            continue
        rot_mode = bones_meta_src.get(bone, {}).get("rotation_mode", 'QUATERNION')
        out_bones[target] = {"rotation_mode": rot_mode}
        # Stored (non-keyed) custom props follow the bone across, negated by the
        # same prop_flips rule the pose mirror uses — one rule, not two.
        src_props = bones_meta_src.get(bone, {}).get("props")
        if src_props:
            out_bones[target]["props"] = _flip_props(src_props, prop_flip)

        transform_curves = {}
        for cur in curves:
            ch = next((n for n in _TRANSFORM_CHANNELS
                       if cur["data_path"].endswith("." + n)), None)
            if ch:
                transform_curves[(ch, cur["array_index"])] = cur
            else:
                # custom-prop / other curves: retarget verbatim (values kept,
                # unless the prop name matches a prop_flips pattern -> negated)
                new_path = cur["data_path"].replace(
                    'pose.bones["%s"]' % bone, 'pose.bones["%s"]' % target, 1)
                keys = cur["keys"]
                if prop_flip is not None:
                    m = _PROP_PATH.search(cur["data_path"])
                    if m and prop_flip(m.group(1).replace('\\"', '"')):
                        keys = [_negate_prop_key(k) for k in keys]
                extra = {k: cur[k] for k in ("extrapolation", "auto_smoothing",
                                             "modifiers") if k in cur}
                out_curves.append({"bone": target, "data_path": new_path,
                                   "array_index": cur["array_index"],
                                   "keys": keys, **extra})
        if not transform_curves:
            continue

        times = sorted({k[0] for cur in transform_curves.values() for k in cur["keys"]})
        keyed_groups = {ch for (ch, _i) in transform_curves}
        rot_channel = ("rotation_quaternion" if rot_mode == 'QUATERNION' else
                       "rotation_axis_angle" if rot_mode == 'AXIS_ANGLE' else
                       "rotation_euler")
        write_groups = set()
        if any(ch == "location" for ch in keyed_groups):
            write_groups.add("location")
        if any(ch == "scale" for ch in keyed_groups):
            write_groups.add("scale")
        if any(ch.startswith("rotation") for ch in keyed_groups):
            write_groups.add(rot_channel)

        C_src = src_rest.matrix_local
        C_tgt = tgt_rest.matrix_local
        Ci = C_src.inverted()
        Cti = C_tgt.inverted()

        new_keys = {}
        prev_quat = None
        prev_eul = None
        for t in times:
            entry = {"rotation_mode": rot_mode}
            for ch, n in _TRANSFORM_CHANNELS.items():
                if ch not in ("location", "scale") and ch != rot_channel:
                    continue
                vals = list(_CHANNEL_DEFAULTS[ch])
                for i in range(n):
                    cur = transform_curves.get((ch, i))
                    if cur:
                        vals[i] = _sample_curve_keys(cur["keys"], t)
                entry[ch] = vals
            M_src = _compose_entry_matrix(entry)
            M_tgt = Cti @ X @ C_src @ M_src @ Ci @ X @ C_tgt
            loc, quat, scale = M_tgt.decompose()
            if prev_quat is not None and prev_quat.dot(quat) < 0.0:
                quat = -quat  # keep quaternion series continuous
            prev_quat = quat

            values = {}
            if "location" in write_groups:
                values["location"] = list(loc)
            if "scale" in write_groups:
                values["scale"] = list(scale)
            if rot_channel in write_groups:
                if rot_channel == "rotation_quaternion":
                    values[rot_channel] = list(quat)
                elif rot_channel == "rotation_axis_angle":
                    axis, angle = quat.to_axis_angle()
                    values[rot_channel] = [angle, axis[0], axis[1], axis[2]]
                else:
                    eul = quat.to_euler(rot_mode, prev_eul) if prev_eul is not None \
                        else quat.to_euler(rot_mode)
                    prev_eul = eul
                    values[rot_channel] = list(eul)
            for ch, vals in values.items():
                for i, v in enumerate(vals):
                    new_keys.setdefault((ch, i), []).append(
                        [t, v, 'BEZIER', 'AUTO',
                         'AUTO_CLAMPED', None, None, 'AUTO_CLAMPED', None, None])

        for (ch, i), keys in new_keys.items():
            entry = {"bone": target,
                     "data_path": 'pose.bones["%s"].%s' % (target, ch),
                     "array_index": i, "keys": keys}
            # carry curve-level graph-editor state from the matching source curve;
            # only CYCLES modifiers survive a mirror (value-shaping ones — Noise,
            # Generator, Envelope — cannot be meaningfully conjugated)
            src = transform_curves.get((ch, i)) or next(iter(transform_curves.values()))
            if "extrapolation" in src:
                entry["extrapolation"] = src["extrapolation"]
            if "auto_smoothing" in src:
                entry["auto_smoothing"] = src["auto_smoothing"]
            cycles = [m for m in src.get("modifiers", []) if m["type"] == 'CYCLES']
            if cycles:
                entry["modifiers"] = cycles
            out_curves.append(entry)

    # ⚠ A BONE WITH PROPS BUT NO CURVES WOULD VANISH HERE. The loop above walks
    # `groups`, which is built from the curves — so a switch bone carrying only
    # stored properties (the whole point of `include_props`) would be dropped by
    # a mirror. Carry those over separately, after, so a mirrored paste sets the
    # same switches as an unmirrored one.
    for bone, bmeta in bones_meta_src.items():
        if bone in groups or not bmeta.get("props"):
            continue
        target = mirror_map.get(bone,
                                bone if flip_bone_name(bone) == bone else None)
        if target is None or target in out_bones:
            continue
        out_bones[target] = {
            "rotation_mode": bmeta.get("rotation_mode", 'QUATERNION'),
            "props": _flip_props(bmeta["props"], prop_flip)}
    return out_curves, out_bones, skipped


# ---------------------------------------------------------------- remap tables
# Rig-to-rig transfer: a .remap item resolves SOURCE-rig bone names to
# TARGET-rig names. remap.json = {"rules": [...], "map": {src: dst},
# "unmatched": [...]}. Rules (applied in order) generate map candidates at
# build time and act as the fallback at apply time; the stored map always
# wins on conflict.

_NORM_SEP = re.compile(r"[\s_.\-]+")
_SIDE_WORDS = {"l": "left", "r": "right", "left": "left", "right": "right"}


def normalize_bone_name(name):
    """Case/space/underscore/dot/dash-insensitive form with the side marker
    canonicalized to a left|/right| prefix token. Daz prefixes ('lShldrBend'),
    Left/Right words, and .L/_R-style suffixes all normalize the same way, so
    'lForearm', 'Forearm.L', 'left_forearm' and 'LeftForearm' all match."""
    side = ""
    if _L_PREFIX.match(name):
        side, name = "left", name[1:]
    elif _R_PREFIX.match(name):
        side, name = "right", name[1:]
    n = name.lower()
    if not side:
        m = re.match(r"^(left|right)[\s_.\-]*(.+)$", n)
        if m is None:
            m = re.match(r"^([lr])[\s_.\-]+(.+)$", n)
        if m:
            side, n = _SIDE_WORDS[m.group(1)], m.group(2)
        else:
            m = re.match(r"^(.+?)[\s_.\-]+(left|right|l|r)$", n)
            if m:
                side, n = _SIDE_WORDS[m.group(2)], m.group(1)
    n = _NORM_SEP.sub("", n)
    return (side + "|" + n) if side else n


def apply_remap_rules(name, rules):
    """Run the rule list in order over one name. Rule = {"op": "prefix_strip"|
    "prefix_add"|"replace", "value"} ("find"/"replace" for replace)."""
    for r in rules or ():
        op = r.get("op")
        if op == "prefix_strip":
            v = r.get("value", "")
            if v and name.startswith(v):
                name = name[len(v):]
        elif op == "prefix_add":
            name = r.get("value", "") + name
        elif op == "replace":
            f = r.get("find", "")
            if f:
                name = name.replace(f, r.get("replace", ""))
    return name


def build_remap(source_names, target_names, rules=None):
    """Auto-match source bone names onto target names. Passes per source name:
    exact match (after rules) -> normalized match. Returns (map, unmatched).
    Pure name logic — the server feeds it the active armature's bones."""
    tset = set(target_names)
    norm_to_target = {}
    for t in target_names:
        norm_to_target.setdefault(normalize_bone_name(t), t)
    mapping = {}
    unmatched = []
    for s in source_names:
        cand = apply_remap_rules(s, rules)
        if cand in tset:
            mapping[s] = cand
        else:
            t = norm_to_target.get(normalize_bone_name(cand))
            if t is not None:
                mapping[s] = t
            else:
                unmatched.append(s)
    return mapping, unmatched


def save_remap(library_root, relfolder, name, rules=None, mapping=None,
               unmatched=None, source="", description="", overwrite=False):
    """Write a .remap item. The map/unmatched come from the app's build flow
    (auto-match + hand edits); the active object is the TARGET armature."""
    ob = get_armature()
    data = {"type": "remap",
            "metadata": _metadata(ob, {"description": description,
                                       "source": source,
                                       "target_armature": ob.name}),
            "rules": list(rules or []), "map": dict(mapping or {}),
            "unmatched": list(unmatched or [])}
    item_dir = os.path.join(library_root, relfolder, safe_name(name) + REMAP_EXT)
    if os.path.isdir(item_dir):
        if not overwrite:
            raise RuntimeError("Item already exists: %s (use overwrite)" % item_dir)
        version_item(item_dir)
    os.makedirs(item_dir, exist_ok=True)
    with open(os.path.join(item_dir, "remap.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
    return {"path": item_dir, "mapped": len(data["map"]),
            "unmatched": len(data["unmatched"])}


def load_remap_file(item_path):
    with open(os.path.join(item_path, "remap.json"), "r", encoding="utf-8") as f:
        return json.load(f)


def _remap_resolver(remap_table):
    """name -> target-name function for a .remap item: map wins, rules are the
    fallback, unknown names pass through unchanged (same-named bones just work)."""
    data = load_remap_file(remap_table)
    if data.get("type") != "remap":
        raise RuntimeError("Not a remap table: %s" % remap_table)
    mapping = data.get("map", {})
    rules = data.get("rules", [])

    def resolve(name):
        if name in mapping:
            return mapping[name]
        return apply_remap_rules(name, rules)
    return resolve


def remap_anim_data(data, resolve):
    """Rename bones in saved anim data through a resolver. Returns
    (new_data, n_renamed). Curve data_paths are rewritten to the target name."""
    renamed = 0
    curves = []
    for cur in data["curves"]:
        b = cur["bone"]
        t = resolve(b)
        if t != b:
            renamed += 1
            cur = dict(cur)
            cur["data_path"] = cur["data_path"].replace(
                'pose.bones["%s"]' % b, 'pose.bones["%s"]' % t, 1)
            cur["bone"] = t
        curves.append(cur)
    bones = {}
    for b, bmeta in data.get("bones", {}).items():
        bones.setdefault(resolve(b), bmeta)
    return ({"type": "anim", "metadata": data["metadata"],
             "bones": bones, "curves": curves}, renamed)


# ---------------------------------------------------------------- live blending

def snapshot_pose(ob, bone_names):
    """Exact channel snapshot for a blend session's base (and cancel-restore)."""
    base = {}
    for name in bone_names:
        pb = ob.pose.bones.get(name)
        if pb is None:
            continue
        entry = {
            "rotation_mode": pb.rotation_mode,
            "location": list(pb.location),
            "scale": list(pb.scale),
            "rotation_quaternion": list(pb.rotation_quaternion),
            "rotation_euler": list(pb.rotation_euler),
            "rotation_axis_angle": list(pb.rotation_axis_angle),
        }
        props = {}
        for key in pb.keys():
            if not key.startswith("_") and isinstance(pb[key], (int, float)):
                props[key] = pb[key]
        if props:
            entry["props"] = props
        base[name] = entry
    return base


def blend_pose(ob, data, base, t, selected_only=False):
    """Set every bone to lerp(base, saved, t). Safe to call repeatedly (the base
    is fixed), which is what live slider drags need."""
    from mathutils import Quaternion

    t = max(0.0, min(1.0, float(t)))
    applied = 0
    for name, entry in data["bones"].items():
        pb = ob.pose.bones.get(name)
        b = base.get(name)
        if pb is None or b is None:
            continue
        if selected_only and not bone_is_selected(pb):
            continue
        pb.rotation_mode = entry["rotation_mode"]
        pb.location = [_lerp(b["location"][i], entry["location"][i], t) for i in range(3)]
        pb.scale = [_lerp(b["scale"][i], entry["scale"][i], t) for i in range(3)]
        if "rotation_quaternion" in entry:
            pb.rotation_quaternion = Quaternion(b["rotation_quaternion"]).slerp(
                Quaternion(entry["rotation_quaternion"]), t)
        elif "rotation_axis_angle" in entry:
            aa = entry["rotation_axis_angle"]
            ba = b["rotation_axis_angle"]
            pb.rotation_axis_angle = [_lerp(ba[i], aa[i], t) for i in range(4)]
        else:
            eul = entry["rotation_euler"]
            be = b["rotation_euler"]
            pb.rotation_euler = [_lerp(be[i], eul[i], t) for i in range(3)]
        for pkey, pval in entry.get("props", {}).items():
            if isinstance(pval, (int, float)):
                bval = b.get("props", {}).get(pkey, pval)
                try:
                    pb[pkey] = _lerp(float(bval), float(pval), t)
                except Exception:
                    pass
        applied += 1
    bpy.context.view_layer.update()
    return applied


def restore_pose(ob, base):
    """Put every snapshotted bone back exactly (blend-cancel)."""
    for name, b in base.items():
        pb = ob.pose.bones.get(name)
        if pb is None:
            continue
        pb.rotation_mode = b["rotation_mode"]
        pb.location = b["location"]
        pb.scale = b["scale"]
        pb.rotation_quaternion = b["rotation_quaternion"]
        pb.rotation_euler = b["rotation_euler"]
        pb.rotation_axis_angle = b["rotation_axis_angle"]
        for pkey, pval in b.get("props", {}).items():
            try:
                pb[pkey] = pval
            except Exception:
                pass
    bpy.context.view_layer.update()


def key_current_pose(ob, bone_names):
    """Insert keyframes on the standard channels of the given bones (post-blend)."""
    keyed = 0
    for name in bone_names:
        pb = ob.pose.bones.get(name)
        if pb is None:
            continue
        rot_path = {"QUATERNION": "rotation_quaternion",
                    "AXIS_ANGLE": "rotation_axis_angle"}.get(pb.rotation_mode,
                                                             "rotation_euler")
        for path in ("location", "scale", rot_path):
            pb.keyframe_insert(path, group=name)
        keyed += 1
    return keyed


# ---------------------------------------------------------------- animations

_BONE_PATH = re.compile(r'^pose\.bones\["((?:[^"\\]|\\.)*)"\]')


def _bone_of_path(data_path):
    m = _BONE_PATH.match(data_path)
    return m.group(1).replace('\\"', '"') if m else None


def _action_fcurves(ob):
    """Read access to the fcurves animating *ob* (legacy or slotted action)."""
    ad = ob.animation_data
    if ad is None or ad.action is None:
        return []
    act = ad.action
    if hasattr(act, "fcurves"):  # pre-5.2 legacy shim
        return list(act.fcurves)
    fcurves = []
    slot = ad.action_slot
    if slot is None:
        return []
    for layer in act.layers:
        for strip in layer.strips:
            if strip.type == 'KEYFRAME':
                cb = strip.channelbag(slot)
                if cb is not None:
                    fcurves.extend(cb.fcurves)
    return fcurves


def _ensure_fcurve_container(ob):
    """Find-or-create path: returns an object with .find(path, index=) / .new(...)."""
    ad = ob.animation_data or ob.animation_data_create()
    act = ad.action
    if act is None:
        act = bpy.data.actions.new(ob.name + "Action")
        ad.action = act
    if hasattr(act, "fcurves"):
        return act.fcurves
    slot = ad.action_slot
    if slot is None:
        slot = act.slots.new('OBJECT', ob.name) if not len(act.slots) else act.slots[0]
        ad.action_slot = slot
    layer = act.layers[0] if len(act.layers) else act.layers.new("Layer")
    strip = None
    for s in layer.strips:
        if s.type == 'KEYFRAME':
            strip = s
            break
    if strip is None:
        strip = layer.strips.new(type='KEYFRAME')
    return strip.channelbag(slot, ensure=True).fcurves


def _serialize_fmodifiers(fc):
    """Graph-editor F-curve modifiers (Cycles, Noise, Generator, …) — generic
    introspection of all writable simple props, plus the two collection specials."""
    mods = []
    for m in fc.modifiers:
        entry = {"type": m.type, "props": {}}
        for p in m.bl_rna.properties:
            if p.is_readonly or p.type not in ('FLOAT', 'INT', 'BOOLEAN', 'ENUM'):
                continue
            val = getattr(m, p.identifier)
            if hasattr(val, "__len__") and not isinstance(val, str):
                val = list(val)
            entry["props"][p.identifier] = val
        if m.type == 'GENERATOR':
            entry["coefficients"] = list(m.coefficients)
        if m.type == 'ENVELOPE':
            entry["control_points"] = [[cp.frame, cp.min, cp.max]
                                       for cp in m.control_points]
        mods.append(entry)
    return mods


def _apply_fmodifiers(fc, mods):
    for m in list(fc.modifiers):
        fc.modifiers.remove(m)
    for md in mods:
        try:
            m = fc.modifiers.new(type=md["type"])
        except Exception:
            continue
        for key, val in md.get("props", {}).items():
            try:
                setattr(m, key, val)
            except Exception:
                pass
        if "coefficients" in md:
            try:
                m.coefficients = md["coefficients"]
            except Exception:
                for i, v in enumerate(md["coefficients"][:len(m.coefficients)]):
                    m.coefficients[i] = v
        for f, mn, mx in md.get("control_points", []):
            try:
                cp = m.control_points.add(f)
                cp.min = mn
                cp.max = mx
            except Exception:
                pass


def _bake_key(f, v):
    # every-frame samples: LINEAR keys, AUTO handles (positions computed by Blender)
    return [f, v, 'LINEAR', 'AUTO', 'AUTO_CLAMPED', None, None,
            'AUTO_CLAMPED', None, None]


def _bake_bone_curves(ob, bone_names, fs, fe):
    """Sample the EVALUATED (visual) local transform of each bone every frame —
    IK/constraint results become plain keys. Constant channels collapse to one key."""
    scene = bpy.context.scene
    saved_frame = scene.frame_current
    samples = {n: {"loc": [], "rot": [], "scale": [], "props": {}} for n in bone_names}
    prev_quat = {}
    prev_eul = {}
    try:
        for f in range(fs, fe + 1):
            scene.frame_set(f)
            for name in bone_names:
                pb = ob.pose.bones.get(name)
                if pb is None:
                    continue
                m = ob.convert_space(pose_bone=pb, matrix=pb.matrix,
                                     from_space='POSE', to_space='LOCAL')
                loc, quat, scale = m.decompose()
                pq = prev_quat.get(name)
                if pq is not None and pq.dot(quat) < 0.0:
                    quat = -quat
                prev_quat[name] = quat
                s = samples[name]
                s["loc"].append(list(loc))
                s["scale"].append(list(scale))
                if pb.rotation_mode == 'QUATERNION':
                    s["rot"].append(list(quat))
                elif pb.rotation_mode == 'AXIS_ANGLE':
                    axis, angle = quat.to_axis_angle()
                    s["rot"].append([angle, axis[0], axis[1], axis[2]])
                else:
                    pe = prev_eul.get(name)
                    eul = quat.to_euler(pb.rotation_mode, pe) if pe is not None \
                        else quat.to_euler(pb.rotation_mode)
                    prev_eul[name] = eul
                    s["rot"].append(list(eul))
                for key in pb.keys():
                    if not key.startswith("_") and isinstance(pb[key], (int, float)):
                        s["props"].setdefault(key, []).append(float(pb[key]))
    finally:
        scene.frame_set(saved_frame)

    curves = []
    bones_meta = {}

    def emit(bone, data_path, index, series):
        if all(v == series[0] for v in series):
            keys = [_bake_key(fs, series[0])]
        else:
            keys = [_bake_key(fs + i, v) for i, v in enumerate(series)]
        curves.append({"bone": bone, "data_path": data_path,
                       "array_index": index, "keys": keys})

    for name in bone_names:
        pb = ob.pose.bones.get(name)
        if pb is None or not samples[name]["loc"]:
            continue
        bones_meta[name] = {"rotation_mode": pb.rotation_mode}
        rot_channel = {"QUATERNION": "rotation_quaternion",
                       "AXIS_ANGLE": "rotation_axis_angle"}.get(pb.rotation_mode,
                                                                "rotation_euler")
        base = 'pose.bones["%s"].' % name
        s = samples[name]
        for i in range(3):
            emit(name, base + "location", i, [v[i] for v in s["loc"]])
        for i in range(len(s["rot"][0])):
            emit(name, base + rot_channel, i, [v[i] for v in s["rot"]])
        for i in range(3):
            emit(name, base + "scale", i, [v[i] for v in s["scale"]])
        for pkey, series in s["props"].items():
            emit(name, '%s["%s"]' % (base[:-1], pkey), 0, series)
    return curves, bones_meta


def _add_bone_props(ob, bones_meta, include):
    """Stamp every saved bone's CURRENT custom-property values into bones_meta.

    Marty, 2026-08-05: "When saving animations also add an option to inherit
    every bone property."

    ⚠ WHAT THIS IS *NOT*: the keyed ones. A custom property that is animated is
    already saved — it is an fcurve like any other (`_PROP_PATH`), and it always
    was. What was missing is the rig state that is NOT keyed: the IK/FK switch,
    the space switch, the twist amount someone set once and never keyframed. A
    pasted animation of an FK arm looks wrong on a rig currently set to IK, and
    nothing in the curves says so.

    ⚠ It runs over the SAVE SCOPE, not over the bones that happen to have
    curves. A switch bone frequently has no keys at all — it is exactly the case
    this exists for — so keying off `curves` would miss the ones that matter.
    """
    for bone in include:
        pb = ob.pose.bones.get(bone)
        if pb is None:
            continue
        props = _custom_props(pb)
        if not props:
            continue
        entry = bones_meta.setdefault(
            bone, {"rotation_mode": pb.rotation_mode})
        entry["props"] = props
    return bones_meta


def save_anim(library_root, relfolder, name, frame_start=None, frame_end=None,
              use_selected=True, description="", overwrite=False, bake=False,
              keep_modifiers=True, include_props=False):
    """Save keyframes of the active armature's bones over a frame range.
    bake=True samples the evaluated (visual) pose EVERY frame instead of copying
    existing keys — captures IK/constraint motion; graph-editor data not kept.

    keep_modifiers: store each curve's F-modifiers (Noise, Cycles, …) so a
    re-import looks the same in the graph editor. Off writes an empty list, and
    the item then pastes as plain keys.
    include_props: also store every saved bone's custom properties — see
    `_add_bone_props`.

    ⚠ Both are ECHOED in the reply (`options`). `save_anim` exists in every
    add-on version ever shipped, so the app's usual command-name capability
    check cannot see that it grew parameters — an older add-on would ignore
    them SILENTLY and write an item that quietly disagrees with the tickboxes
    that were on screen. Same trap, same fix, as `save_abc`'s options.
    """
    ob = get_armature()
    scene = bpy.context.scene
    fs = int(frame_start) if frame_start is not None else scene.frame_start
    fe = int(frame_end) if frame_end is not None else scene.frame_end
    if fe < fs:
        fs, fe = fe, fs
    include = {pb.name for pb in _pose_bones_to_save(ob, use_selected)}
    # A baked curve is sampled every frame — there are no source F-modifiers
    # left to keep, so the flag records what the FILE holds, not what was asked.
    stored_modifiers = bool(keep_modifiers) and not bake
    options = {"keep_modifiers": stored_modifiers,
               "include_props": bool(include_props)}

    def _write(data):
        item_dir = os.path.join(library_root, relfolder,
                                safe_name(name) + ANIM_EXT)
        if os.path.isdir(item_dir):
            if not overwrite:
                raise RuntimeError("Item already exists: %s (use overwrite)"
                                   % item_dir)
            version_item(item_dir)
        os.makedirs(item_dir, exist_ok=True)
        with open(os.path.join(item_dir, "anim.json"), "w",
                  encoding="utf-8") as f:
            json.dump(data, f, indent=1)
        return item_dir

    if bake:
        curves, bones_meta = _bake_bone_curves(ob, sorted(include), fs, fe)
        if not curves:
            raise RuntimeError("Nothing to bake for those bones")
        if include_props:
            _add_bone_props(ob, bones_meta, include)
        data = {"type": "anim",
                "metadata": _metadata(ob, {"description": description,
                                           "frame_start": fs, "frame_end": fe,
                                           "baked": True,
                                           "fcurve_modifiers": stored_modifiers,
                                           "bone_props": options["include_props"]}),
                "bones": bones_meta, "curves": curves}
        item_dir = _write(data)
        return {"path": item_dir, "curves": len(curves), "bones": len(bones_meta),
                "frame_start": fs, "frame_end": fe, "baked": True,
                "options": options}

    curves = []
    bones_meta = {}
    for fc in _action_fcurves(ob):
        bone = _bone_of_path(fc.data_path)
        if bone is None or bone not in include:
            continue
        keys = []
        for kp in fc.keyframe_points:
            f = kp.co[0]
            if fs <= f <= fe:
                keys.append([f, kp.co[1], kp.interpolation, kp.easing,
                             kp.handle_left_type, kp.handle_left[0], kp.handle_left[1],
                             kp.handle_right_type, kp.handle_right[0], kp.handle_right[1],
                             # graph-editor extras (older files stop at 10)
                             kp.type, kp.amplitude, kp.back, kp.period])
        if keys:
            curves.append({"bone": bone, "data_path": fc.data_path,
                           "array_index": fc.array_index, "keys": keys,
                           "extrapolation": fc.extrapolation,
                           "auto_smoothing": fc.auto_smoothing,
                           "modifiers": (_serialize_fmodifiers(fc)
                                         if stored_modifiers else [])})
            if bone not in bones_meta:
                pb = ob.pose.bones.get(bone)
                bones_meta[bone] = {"rotation_mode": pb.rotation_mode if pb else 'QUATERNION'}
    if not curves:
        raise RuntimeError("No keyframes found in frames %d-%d for those bones" % (fs, fe))
    if include_props:
        _add_bone_props(ob, bones_meta, include)

    data = {"type": "anim",
            "metadata": _metadata(ob, {"description": description,
                                       "frame_start": fs, "frame_end": fe,
                                       "fcurve_modifiers": stored_modifiers,
                                       "bone_props": options["include_props"]}),
            "bones": bones_meta, "curves": curves}
    item_dir = _write(data)
    return {"path": item_dir, "curves": len(curves), "bones": len(bones_meta),
            "frame_start": fs, "frame_end": fe, "options": options}


def load_anim_file(item_path):
    with open(os.path.join(item_path, "anim.json"), "r", encoding="utf-8") as f:
        return json.load(f)


_EULER_ORDERS = {'XYZ', 'XZY', 'YXZ', 'YZX', 'ZXY', 'ZYX'}


def _rot_state_quat(pb):
    """The bone's CURRENT rotation as a Quaternion, whatever its rotation_mode
    (raw channels of the OTHER modes can be stale — never read those)."""
    from mathutils import Quaternion
    if pb.rotation_mode == 'QUATERNION':
        return pb.rotation_quaternion.copy()
    if pb.rotation_mode == 'AXIS_ANGLE':
        aa = pb.rotation_axis_angle
        return Quaternion((aa[1], aa[2], aa[3]), aa[0])
    return pb.rotation_euler.to_quaternion()


def _blend_anim_curves(ob, data, t, offset, fcontainer):
    """Blended copy of data['curves']: every key value pulled t of the way from
    the rig's CURRENT state to the saved animation.

    Base per key = the bone's existing fcurve evaluated at the destination
    frame (pre-paste), else its current channel value (rotations converted into
    the item's rotation space). Quaternions slerp as whole quats at each key
    time (sign-fixed for continuity like mirror); other channels lerp.
    Explicit handle Y's are offset-scaled around the new value so the curve
    shape survives; F-modifiers are NOT scaled (copied as saved)."""
    from mathutils import Quaternion

    bones_meta = data.get("bones", {})

    def live_eval(path, index, dst_f):
        fc = fcontainer.find(path, index=index)
        return fc.evaluate(dst_f) if fc is not None else None

    # quats must blend as a unit — per-component lerp is not a rotation blend
    quat_groups = {}
    for cur in data["curves"]:
        if cur["data_path"].endswith(".rotation_quaternion"):
            quat_groups.setdefault((cur["bone"], cur["data_path"]), {})[
                cur["array_index"]] = cur
    quat_blend = {}  # (bone, src_frame) -> [w, x, y, z] blended
    for (bone, path), comps in quat_groups.items():
        pb = ob.pose.bones.get(bone)
        if pb is None:
            continue
        state_q = _rot_state_quat(pb)
        frames = sorted({k[0] for cur in comps.values() for k in cur["keys"]})
        prev_out = None
        for f in frames:
            base = []
            anim = []
            for i in range(4):
                live = live_eval(path, i, f + offset)
                if live is None:
                    live = state_q[i]
                base.append(live)
                cur = comps.get(i)
                anim.append(_sample_curve_keys(cur["keys"], f) if cur else live)
            bq = Quaternion(base).normalized()
            aq = Quaternion(anim).normalized()
            if bq.dot(aq) < 0.0:
                aq = -aq  # shortest arc
            out_q = bq.slerp(aq, t)
            if prev_out is not None and prev_out.dot(out_q) < 0.0:
                out_q = -out_q  # keep the written series continuous
            prev_out = out_q
            quat_blend[(bone, f)] = list(out_q)

    out = []
    for cur in data["curves"]:
        pb = ob.pose.bones.get(cur["bone"])
        if pb is None:
            out.append(cur)  # the write loop skips missing bones anyway
            continue
        path, idx = cur["data_path"], cur["array_index"]
        ch = next((n for n in _TRANSFORM_CHANNELS if path.endswith("." + n)), None)

        static = None  # fallback when the bone has no live fcurve on this path
        if ch == "location":
            static = pb.location[idx]
        elif ch == "scale":
            static = pb.scale[idx]
        elif ch == "rotation_euler":
            rot_mode = bones_meta.get(cur["bone"], {}).get(
                "rotation_mode", pb.rotation_mode)
            order = rot_mode if rot_mode in _EULER_ORDERS else 'XYZ'
            static = _rot_state_quat(pb).to_euler(order)[idx]
        elif ch == "rotation_axis_angle":
            axis, angle = _rot_state_quat(pb).to_axis_angle()
            static = (angle, axis[0], axis[1], axis[2])[idx]
        elif ch is None:
            m = _PROP_PATH.search(path)
            if m:
                v = pb.get(m.group(1).replace('\\"', '"'))
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    static = float(v)

        new_keys = []
        for k in cur["keys"]:
            k = list(k)
            f, v = k[0], k[1]
            if ch == "rotation_quaternion":
                bl = quat_blend.get((cur["bone"], f))
                nv = bl[idx] if bl is not None else v
            else:
                base = live_eval(path, idx, f + offset)
                if base is None:
                    base = static
                nv = v if base is None else base + (v - base) * t
            k[1] = nv
            if len(k) > 6 and k[6] is not None:
                k[6] = nv + (k[6] - v) * t
            if len(k) > 9 and k[9] is not None:
                k[9] = nv + (k[9] - v) * t
            new_keys.append(k)
        out.append({**cur, "keys": new_keys})
    return out


def apply_anim(item_path, mode='replace', start_at='current', selected_only=False,
               mirror=False, mirror_table=None, remap_table=None, blend=1.0):
    """Paste a saved animation onto the active armature.

    mode: 'replace' (clear keys in the pasted range first), 'merge' (overlay keys),
          'insert' (ripple: push existing keys on affected curves right by the
          anim's span, then paste).
    start_at: 'current' (paste so the anim starts at the playhead) or 'original'.
    mirror: flip the animation L<->R (baked at key times via the mirror table
    or auto-detected pairs).
    remap_table: a .remap item path — bone names resolved through it FIRST,
    then mirror runs on the target rig's names.
    blend: 0..1 influence — keys land pulled that fraction from the rig's
    PRE-PASTE state (existing curves / current pose) toward the saved anim.
    1.0 is the exact legacy path; F-modifiers always copy at full strength.
    """
    ob = get_armature()
    data = load_anim_file(item_path)
    if data.get("type") != "anim":
        raise RuntimeError("Not an anim item: %s" % item_path)
    meta = data["metadata"]
    fs, fe = meta["frame_start"], meta["frame_end"]

    remapped = 0
    if remap_table:
        data, remapped = remap_anim_data(data, _remap_resolver(remap_table))
    mirror_skipped = []
    if mirror:
        prop_flip = None
        if mirror_table:
            mdata = load_mirror_file(mirror_table)
            mirror_map = mdata.get("map", {})
            prop_flip = _prop_flip_matcher(mdata.get("prop_flips"))
        else:
            mirror_map, _c, _u = build_mirror_map(ob)
        m_curves, m_bones, mirror_skipped = mirror_anim_curves(
            ob, data, mirror_map, prop_flip=prop_flip)
        data = {"type": "anim", "metadata": meta,
                "bones": m_bones, "curves": m_curves}
    offset = (bpy.context.scene.frame_current - fs) if start_at == 'current' else 0
    span = fe - fs + 1

    fcontainer = _ensure_fcurve_container(ob)
    t_blend = max(0.0, min(1.0, blend))
    if t_blend < 1.0:  # must run BEFORE any curve is cleared/shifted
        data = {**data,
                "curves": _blend_anim_curves(ob, data, t_blend, offset, fcontainer)}
    applied_curves = 0
    missing = set()
    touched_bones = set()

    for cur in data["curves"]:
        bone = cur["bone"]
        pb = ob.pose.bones.get(bone)
        if pb is None:
            missing.add(bone)
            continue
        if selected_only and not bone_is_selected(pb):
            continue
        fc = fcontainer.find(cur["data_path"], index=cur["array_index"])
        fc_created = fc is None
        if fc is None:
            fc = fcontainer.new(cur["data_path"], index=cur["array_index"])
        kps = fc.keyframe_points

        dst_start = fs + offset
        dst_end = fe + offset
        if mode == 'insert':
            # move rightmost first so re-sorting can't reorder around us
            for kp in sorted(kps, key=lambda k: -k.co[0]):
                if kp.co[0] >= dst_start:
                    kp.co[0] += span
                    kp.handle_left[0] += span
                    kp.handle_right[0] += span
        elif mode == 'replace':
            for i in range(len(kps) - 1, -1, -1):
                if dst_start <= kps[i].co[0] <= dst_end:
                    kps.remove(kps[i], fast=True)

        for k in cur["keys"]:
            (f, v, interp, easing, hlt, hlx, hly, hrt, hrx, hry) = k[:10]
            kp = kps.insert(f + offset, v, options={'FAST'})
            kp.interpolation = interp
            kp.easing = easing
            kp.handle_left_type = hlt
            kp.handle_right_type = hrt
            if hlx is not None:  # baked (mirrored) keys carry AUTO handles only
                kp.handle_left = (hlx + offset, hly)
            if hrx is not None:
                kp.handle_right = (hrx + offset, hry)
            if len(k) > 10:  # graph-editor extras (key type, elastic/bounce/back params)
                kp.type = k[10]
                kp.amplitude = k[11]
                kp.back = k[12]
                kp.period = k[13]
        # curve-level graph-editor state
        if "extrapolation" in cur:
            fc.extrapolation = cur["extrapolation"]
        if "auto_smoothing" in cur:
            fc.auto_smoothing = cur["auto_smoothing"]
        # modifiers: replace-mode owns the curve; merge/insert only dress NEW curves
        if "modifiers" in cur and (mode == 'replace' or fc_created):
            _apply_fmodifiers(fc, cur["modifiers"])
        fc.update()
        applied_curves += 1
        touched_bones.add(bone)

    props_set = 0
    for bone, bmeta in data.get("bones", {}).items():
        pb = ob.pose.bones.get(bone)
        if pb is None:
            continue
        if bone in touched_bones:
            pb.rotation_mode = bmeta.get("rotation_mode", pb.rotation_mode)
        # Stored custom properties (save-time "inherit every bone property").
        # ⚠ NOT gated on `touched_bones`, unlike rotation_mode: the bones this
        # exists for — IK/FK and space switches — routinely carry no keys at
        # all, so requiring a curve would skip exactly the ones that matter.
        # `selected_only` still applies, because that is a scope the user asked
        # for out loud.
        props = bmeta.get("props")
        if not props:
            continue
        if selected_only and not bone_is_selected(pb):
            continue
        for key, value in props.items():
            try:
                pb[key] = value
            except (TypeError, KeyError, AttributeError):
                # A property the rig no longer has, or now holds a different
                # type. One bad property must not cost the whole paste.
                continue
            props_set += 1

    bpy.context.view_layer.update()
    return {"curves": applied_curves, "bones": len(touched_bones),
            "missing": len(missing), "missing_names": sorted(missing)[:20],
            "pasted_range": [fs + offset, fe + offset],
            "mirror_skipped": len(mirror_skipped), "remapped": remapped,
            "props": props_set}


# ---------------------------------------------------------------- previews

def _find_view3d():
    wm = bpy.context.window_manager
    for window in wm.windows:
        for area in window.screen.areas:
            if area.type == 'VIEW_3D':
                for region in area.regions:
                    if region.type == 'WINDOW':
                        return window, area, region
    return None, None, None


def _resolve_preview_keys(item_path):
    """Live key blocks for a .shapes item's saved keys (same matching rules as
    apply_shapes: object name, single-mesh fallback to the active mesh)."""
    with open(os.path.join(item_path, "shapes.json"), "r", encoding="utf-8") as f:
        data = json.load(f)
    if data.get("type") != "shapes":
        raise RuntimeError("Not a shapes item: %s" % item_path)
    kbs = []
    for m in data["meshes"]:
        ob = bpy.data.objects.get(m["object"])
        if (ob is None or ob.type != 'MESH') and len(data["meshes"]) == 1:
            ao = bpy.context.active_object
            if ao is not None and ao.type == 'MESH':
                ob = ao
        if ob is None or ob.type != 'MESH' or ob.data.shape_keys is None:
            continue
        for k in m["keys"]:
            kb = ob.data.shape_keys.key_blocks.get(k["name"])
            if kb is not None:
                kbs.append(kb)
    if not kbs:
        raise RuntimeError("none of the item's keys are on the mesh right now — "
                           "Add Keys back before capturing a preview")
    return kbs


def _preview_render_state(scene):
    """Everything `capture_preview` has to put back, read BEFORE it changes
    anything. Split out so the vertex-group capture below cannot drift from it
    — the media_type ordering in particular is a 5.x trap that is only obvious
    once you have hit it."""
    rd = scene.render
    has_media_type = hasattr(rd.image_settings, "media_type")
    return {
        "filepath": rd.filepath,
        "resolution_x": rd.resolution_x,
        "resolution_y": rd.resolution_y,
        "resolution_percentage": rd.resolution_percentage,
        "media_type": rd.image_settings.media_type if has_media_type else None,
        "file_format": rd.image_settings.file_format,
        "quality": rd.image_settings.quality,
        "color_mode": rd.image_settings.color_mode,
        "use_file_extension": rd.use_file_extension,
        "use_overwrite": rd.use_overwrite,
        "frame": scene.frame_current,
    }


def _preview_render_begin(scene, width, height):
    """Square JPEG output at `width`x`height`, ready for render.opengl."""
    rd = scene.render
    rd.resolution_x = width
    rd.resolution_y = height
    rd.resolution_percentage = 100
    # Blender 5.x: file_format's ACCEPTED values are gated by media_type
    # (a VIDEO scene only accepts 'FFMPEG') — switch to IMAGE first.
    if hasattr(rd.image_settings, "media_type"):
        rd.image_settings.media_type = 'IMAGE'
    rd.image_settings.file_format = 'JPEG'
    rd.image_settings.quality = 85
    rd.image_settings.color_mode = 'RGB'
    rd.use_file_extension = False
    rd.use_overwrite = True


def _preview_render_end(scene, saved, restore_frame=True):
    rd = scene.render
    rd.filepath = saved["filepath"]
    rd.resolution_x = saved["resolution_x"]
    rd.resolution_y = saved["resolution_y"]
    rd.resolution_percentage = saved["resolution_percentage"]
    if saved["media_type"] is not None:
        rd.image_settings.media_type = saved["media_type"]  # BEFORE file_format
    rd.image_settings.file_format = saved["file_format"]
    rd.image_settings.quality = saved["quality"]
    rd.image_settings.color_mode = saved["color_mode"]
    rd.use_file_extension = saved["use_file_extension"]
    rd.use_overwrite = saved["use_overwrite"]
    if restore_frame:
        scene.frame_set(saved["frame"])


def _clear_sequence(item_path):
    """Empty (and create) an item's sequence/ folder, returning its path."""
    seq_dir = os.path.join(item_path, "sequence")
    os.makedirs(seq_dir, exist_ok=True)
    for old in os.listdir(seq_dir):
        if old.lower().endswith((".jpg", ".png")):
            os.remove(os.path.join(seq_dir, old))
    return seq_dir


def capture_preview(item_path, width=256, height=256, frames=None,
                    shape_steps=None):
    """OpenGL-render the current viewport into an item folder.

    frames=None            -> single thumbnail.jpg (poses)
    frames=(start, end)    -> thumbnail.jpg (first frame) + sequence/frame_####.jpg
    shape_steps=N          -> .shapes items: ramp the saved keys 0 -> 1 -> 0 over
                              ~N frames + sequence/ (thumbnail = the peak frame)
    """
    if not os.path.isdir(item_path):
        raise RuntimeError("Item does not exist: %s" % item_path)
    window, area, region = _find_view3d()
    if area is None:
        raise RuntimeError("No 3D Viewport found to render from")

    scene = bpy.context.scene
    rd = scene.render
    saved = _preview_render_state(scene)
    written = []
    shape_state = []  # (key_block, value, mute) to restore afterwards
    # ⚠ OVERLAYS OFF FOR THE CAPTURE (Marty, 2026-08-05: "Disable overlays when
    # making a preview when saving animations in studio library"). Bone
    # octahedrons, wires, gizmos and the grid floor are all overlays, and a
    # thumbnail of a rigged character was mostly bone.
    #
    # ⚠ Applied to EVERY capture_preview type, not only to anims. A pose and an
    # anim of the same rig would otherwise be shot with different settings and
    # look like different characters side by side in the grid.
    #
    # ⚠ And deliberately NOT to `capture_vgroup_preview` below: weight colours
    # are drawn by the paint mode itself and must not be gambled on here. That
    # capture is untouched.
    space = area.spaces.active
    saved_overlays = space.overlay.show_overlays
    try:
        space.overlay.show_overlays = False
        _preview_render_begin(scene, width, height)

        def render_to(path):
            rd.filepath = path
            with bpy.context.temp_override(window=window, area=area, region=region):
                bpy.ops.render.opengl(write_still=True)
            written.append(path)

        if shape_steps:
            kbs = _resolve_preview_keys(item_path)
            shape_state = [(kb, kb.value, kb.mute) for kb in kbs]
            seq_dir = _clear_sequence(item_path)
            n_up = max(2, int(shape_steps) // 2)
            vals = [i / n_up for i in range(n_up + 1)]
            vals += vals[-2:0:-1]  # 0..1..back (loops cleanly on hover)
            for si, v in enumerate(vals):
                for kb in kbs:
                    kb.mute = False  # a muted key would preview as nothing
                    kb.value = v * min(1.0, kb.slider_max)
                bpy.context.view_layer.update()
                render_to(os.path.join(seq_dir, "frame_%04d.jpg" % si))
            shutil.copyfile(os.path.join(seq_dir, "frame_%04d.jpg" % n_up),
                            os.path.join(item_path, "thumbnail.jpg"))
        elif frames is None:
            render_to(os.path.join(item_path, "thumbnail.jpg"))
        else:
            start, end = int(frames[0]), int(frames[1])
            seq_dir = _clear_sequence(item_path)
            for f in range(start, end + 1):
                scene.frame_set(f)
                render_to(os.path.join(seq_dir, "frame_%04d.jpg" % f))
            shutil.copyfile(written[0], os.path.join(item_path, "thumbnail.jpg"))
    finally:
        space.overlay.show_overlays = saved_overlays
        _preview_render_end(scene, saved, restore_frame=frames is not None)
        for kb, val, mute in shape_state:
            kb.value = val
            kb.mute = mute
        if shape_state:
            bpy.context.view_layer.update()
    return {"written": len(written), "overlays": False}


def capture_vgroup_preview(item_path, width=256, height=256, max_groups=24):
    """Weight-paint stills of a .vgroups item's groups, in the item folder.

    Marty, 2026-08-05: "When exporting vertex paint i need a thumbnail/preview
    (still) of the said vertex paint from the viewport, can briefly switch to
    take weight paint thumbnail or find a better way if possible."

    ⚠ BRIEFLY SWITCHING *IS* THE BETTER WAY, and it is worth saying why so
    nobody spends an afternoon looking for the clever version. The weight
    colours are not a material, an overlay flag or a shading mode that can be
    turned on from the outside — Blender only draws them while the object is in
    Weight Paint mode with that group active. The alternatives are worse: a
    colour attribute baked from the weights changes the user's mesh data, and
    an offscreen GPU draw would have to reimplement Blender's own weight ramp
    and get it subtly wrong. So: enter the mode, render, leave it exactly as it
    was found.

    One group -> thumbnail.jpg. Several -> sequence/ too, so the tile plays
    through them on hover like an anim, which is the only way to actually see a
    multi-group item without opening it.

    ⚠ Capped at `max_groups` FRAMES, not groups: the item keeps everything it
    stored, only the preview stops. A rigged character has a hundred-odd bone
    groups and each frame is a real viewport render — an uncapped preview would
    lock Blender up for minutes for a picture nobody can read anyway.
    """
    path = os.path.join(item_path, "vgroups.json")
    if not os.path.isfile(path):
        raise RuntimeError("Not a vertex group item: %s" % item_path)
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    window, area, region = _find_view3d()
    if area is None:
        raise RuntimeError("No 3D Viewport found to render from")

    # (object, group name) in stored order, skipping what is no longer here.
    jobs = []
    missing = []
    for mesh in data.get("meshes", []):
        ob = bpy.data.objects.get(mesh.get("object", ""))
        if ob is None or ob.type != 'MESH':
            missing.append(mesh.get("object", "?"))
            continue
        for entry in mesh.get("groups", []):
            if ob.vertex_groups.get(entry["name"]) is None:
                missing.append("%s / %s" % (ob.name, entry["name"]))
                continue
            jobs.append((ob, entry["name"]))
    if not jobs:
        raise RuntimeError(
            "None of this item's groups are on a mesh in the scene right now — "
            "nothing to weight-paint a preview from")
    capped = len(jobs) > max_groups
    jobs = jobs[:max_groups]

    scene = bpy.context.scene
    rd = scene.render
    view_layer = bpy.context.view_layer
    saved = _preview_render_state(scene)
    start_active = view_layer.objects.active
    # ⚠ Object.mode, not bpy.context.mode: context reports 'PAINT_WEIGHT' and
    # 'EDIT_MESH', which mode_set does not accept. Object.mode speaks the same
    # enum mode_set does, so it can be handed straight back.
    start_mode = start_active.mode if start_active is not None else 'OBJECT'
    start_selected = [o for o in view_layer.objects if o.select_get()]
    start_indices = {}
    written = []
    try:
        with bpy.context.temp_override(window=window, area=area, region=region):
            if start_active is not None and start_mode != 'OBJECT':
                bpy.ops.object.mode_set(mode='OBJECT')
            _preview_render_begin(scene, width, height)
            seq_dir = _clear_sequence(item_path) if len(jobs) > 1 else None
            for index, (ob, group) in enumerate(jobs):
                if ob.name not in start_indices:
                    start_indices[ob.name] = ob.vertex_groups.active_index
                for other in view_layer.objects:
                    other.select_set(other is ob)
                view_layer.objects.active = ob
                ob.vertex_groups.active_index = ob.vertex_groups[group].index
                try:
                    bpy.ops.object.mode_set(mode='WEIGHT_PAINT')
                except RuntimeError as exc:
                    # Hidden, unselectable, linked from a library… name it and
                    # keep going: one awkward mesh must not cost the previews
                    # of every other group in the item.
                    missing.append("%s / %s (%s)" % (ob.name, group, exc))
                    continue
                view_layer.update()
                if seq_dir is None:
                    target = os.path.join(item_path, "thumbnail.jpg")
                else:
                    target = os.path.join(seq_dir, "frame_%04d.jpg" % index)
                rd.filepath = target
                bpy.ops.render.opengl(write_still=True)
                written.append(target)
                bpy.ops.object.mode_set(mode='OBJECT')
            if seq_dir is not None and written:
                shutil.copyfile(written[0],
                                os.path.join(item_path, "thumbnail.jpg"))
    finally:
        _preview_render_end(scene, saved, restore_frame=False)
        for name, index in start_indices.items():
            ob = bpy.data.objects.get(name)
            if ob is not None and index < len(ob.vertex_groups):
                ob.vertex_groups.active_index = index
        for o in view_layer.objects:
            o.select_set(o in start_selected)
        if start_active is not None and start_active.name in bpy.data.objects:
            view_layer.objects.active = start_active
            if start_mode != 'OBJECT':
                try:
                    with bpy.context.temp_override(window=window, area=area,
                                                   region=region):
                        bpy.ops.object.mode_set(mode=start_mode)
                except RuntimeError:
                    pass          # the mode is gone; OBJECT is the safe landing
    if not written:
        raise RuntimeError("Could not enter Weight Paint on any of the item's "
                           "meshes: %s" % ", ".join(missing[:4]))
    return {"written": len(written), "groups": len(jobs),
            "capped": capped, "missing": missing[:20]}


# ---------------------------------------------------------------- selection sets

def save_set(library_root, relfolder, name, description="", overwrite=False):
    ob = get_armature()
    bones = [pb.name for pb in ob.pose.bones if bone_is_selected(pb)]
    if not bones:
        raise RuntimeError("No bones selected — select the bones for the set first")

    data = {"type": "set", "metadata": _metadata(ob, {"description": description}),
            "bones": bones}
    item_dir = os.path.join(library_root, relfolder, safe_name(name) + SET_EXT)
    if os.path.isdir(item_dir):
        if not overwrite:
            raise RuntimeError("Item already exists: %s (use overwrite)" % item_dir)
        version_item(item_dir)
    os.makedirs(item_dir, exist_ok=True)
    with open(os.path.join(item_dir, "set.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
    return {"path": item_dir, "bones": len(bones)}


def apply_set(item_path, extend=False):
    """Select the set's bones on the active armature."""
    ob = get_armature()
    with open(os.path.join(item_path, "set.json"), "r", encoding="utf-8") as f:
        data = json.load(f)
    if data.get("type") != "set":
        raise RuntimeError("Not a selection set: %s" % item_path)

    if not extend:
        for pb in ob.pose.bones:
            bone_set_selected(pb, False)
    selected = 0
    missing = []
    for name in data["bones"]:
        pb = ob.pose.bones.get(name)
        if pb is None:
            missing.append(name)
            continue
        bone_set_selected(pb, True)
        selected += 1
    if selected:
        last = ob.pose.bones.get(data["bones"][-1])
        if last is not None:
            ob.data.bones.active = ob.data.bones.get(last.name)
    return {"selected": selected, "missing": len(missing), "missing_names": missing[:20]}


# ---------------------------------------------------------------- shape keys
# Vault: store shape keys OUTSIDE the .blend. Per key one .bin sidecar =
# zlib(<u32 total_verts><u32 stored><u32 idx[stored]><f32 delta[stored*3]>) —
# sparse deltas vs Basis, so keys that only move part of the mesh cost almost
# nothing. shapes.json holds per-mesh vert count + Basis checksum for safety.

_SPARSE_EPS = 1e-6


def _mesh_objects(names=None):
    if names:
        obs = []
        for n in names:
            ob = bpy.data.objects.get(n)
            if ob is None or ob.type != 'MESH':
                raise RuntimeError("Mesh object not found: %s" % n)
            obs.append(ob)
        return obs
    obs = [ob for ob in bpy.context.selected_objects if ob.type == 'MESH']
    if not obs:
        ao = bpy.context.active_object
        if ao is not None and ao.type == 'MESH':
            obs = [ao]
    if not obs:
        raise RuntimeError("No mesh objects selected")
    return obs


def _basis_coords(ob):
    """Basis (reference key) positions as a flat float32 array; falls back to
    the mesh vertices when the object has no shape keys yet."""
    me = ob.data
    arr = np.empty(len(me.vertices) * 3, dtype=np.float32)
    if me.shape_keys is not None:
        me.shape_keys.reference_key.data.foreach_get("co", arr)
    else:
        me.vertices.foreach_get("co", arr)
    return arr


def _topo_checksum(ob):
    return hashlib.md5(_basis_coords(ob).tobytes()).hexdigest()[:16]


def _key_driver_text(ob, key_name):
    sk = ob.data.shape_keys
    ad = sk.animation_data if sk else None
    if ad is None:
        return None
    for fc in ad.drivers:
        if fc.data_path == 'key_blocks["%s"].value' % key_name:
            d = fc.driver
            return "%s: %s" % (d.type, d.expression or "(no expression)")
    return None


def _key_is_animated(ob, key_name):
    """Does this key's value carry KEYFRAMES (as opposed to a driver)?

    ⚠ Drivers and animation are different things and live in different places:
    a driver is in `animation_data.drivers`, keyframes are on the ACTION's
    fcurves. Something driven has no fcurve in the action, and something
    keyframed has no driver — so the two questions cannot be answered by
    looking in one place, and the app offers them as separate filters because
    they mean genuinely different things about a key.

    ⚠ **`Action.fcurves` DOES NOT EXIST ON 5.x** (slotted actions — the curves
    live in slot > layer > strip > channelbag). Reading it here raised
    *"'Action' object has no attribute 'fcurves'"* and took the whole Save
    Shape Keys dialog down, because `list_shape_keys` calls this for every key.
    It only bit once shape keys were actually KEYFRAMED: with no action on the
    Key datablock the loop was never reached, which is why every test and every
    earlier use missed it. Reported by Marty, 2026-08-05.
    """
    sk = ob.data.shape_keys
    ad = sk.animation_data if sk else None
    if ad is None:
        return False
    path = 'key_blocks["%s"].value' % key_name
    action = getattr(ad, "action", None)
    if action is not None:
        for fc in _al_action_fcurves_ro(action, getattr(ad, "action_slot", None)):
            if fc.data_path == path and len(fc.keyframe_points):
                return True
    # NLA strips hold actions too — a key animated only inside a strip is still
    # animated, and reporting it as free would be wrong. Each strip carries its
    # own slot, so the same walk needs the strip's, not the AnimData's.
    for track in getattr(ad, "nla_tracks", []) or []:
        for strip in track.strips:
            strip_action = getattr(strip, "action", None)
            if strip_action is None:
                continue
            for fc in _al_action_fcurves_ro(strip_action,
                                            getattr(strip, "action_slot", None)):
                if fc.data_path == path and len(fc.keyframe_points):
                    return True
    return False


def list_shape_keys(objects=None):
    """Selected (or named) mesh objects with their key blocks — the app's
    save-checklist source."""
    out = []
    for ob in _mesh_objects(objects):
        sk = ob.data.shape_keys
        keys = []
        if sk is not None:
            ref = sk.reference_key
            for kb in sk.key_blocks:
                keys.append({"name": kb.name, "value": round(kb.value, 4),
                             "muted": kb.mute, "is_basis": kb == ref,
                             "has_driver": _key_driver_text(ob, kb.name) is not None,
                             "has_animation": _key_is_animated(ob, kb.name)})
        out.append({"object": ob.name, "verts": len(ob.data.vertices), "keys": keys})
    return out


def save_shapes(library_root, relfolder, name, objects=None, keys=None,
                delete_after=False, description="", overwrite=False):
    """keys: {object_name: [key names]} or None = every non-Basis key.
    delete_after: remove the saved keys from the mesh once the item is written
    and re-verified on disk (the vault 'move' — .blend gets lighter)."""
    obs = _mesh_objects(objects)
    meshes = []
    blobs = []  # (fname, compressed bytes)
    for mi, ob in enumerate(obs):
        sk = ob.data.shape_keys
        if sk is None:
            continue
        ref = sk.reference_key
        want = set(keys.get(ob.name, [])) if keys else None
        n = len(ob.data.vertices)
        base = _basis_coords(ob).reshape(-1, 3)
        entry_keys = []
        for ki, kb in enumerate(sk.key_blocks):
            if kb == ref or (want is not None and kb.name not in want):
                continue
            co = np.empty(n * 3, dtype=np.float32)
            kb.data.foreach_get("co", co)
            delta = co.reshape(-1, 3) - base
            mask = np.abs(delta).max(axis=1) > _SPARSE_EPS
            idx = np.nonzero(mask)[0].astype(np.uint32)
            dl = np.ascontiguousarray(delta[mask], dtype=np.float32)
            fname = "m%d_k%d.bin" % (mi, ki)
            raw = struct.pack("<II", n, len(idx)) + idx.tobytes() + dl.tobytes()
            blobs.append((fname, zlib.compress(raw, 6)))
            entry_keys.append({
                "name": kb.name, "file": fname, "value": kb.value,
                "min": kb.slider_min, "max": kb.slider_max,
                "vertex_group": kb.vertex_group,
                "relative_to": kb.relative_key.name if kb.relative_key else "",
                "mute": kb.mute, "stored": int(len(idx)),
                "driver": _key_driver_text(ob, kb.name),
            })
        if entry_keys:
            meshes.append({"object": ob.name, "mesh": ob.data.name, "verts": n,
                           "checksum": _topo_checksum(ob), "keys": entry_keys})
    if not meshes:
        raise RuntimeError("No shape keys to save on the chosen meshes")

    data = {"type": "shapes",
            "metadata": _metadata(obs[0], {"description": description,
                                           "objects": [m["object"] for m in meshes]}),
            "meshes": meshes}
    item_dir = os.path.join(library_root, relfolder, safe_name(name) + SHAPES_EXT)
    if os.path.isdir(item_dir):
        if not overwrite:
            raise RuntimeError("Item already exists: %s (use overwrite)" % item_dir)
        version_item(item_dir)
    os.makedirs(item_dir, exist_ok=True)
    with open(os.path.join(item_dir, "shapes.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
    for fname, blob in blobs:
        with open(os.path.join(item_dir, fname), "wb") as f:
            f.write(blob)

    deleted = 0
    if delete_after:
        # never delete before the vault copy proves readable
        _verify_shapes_item(item_dir)
        for m in meshes:
            deleted += delete_shape_keys(
                m["object"], [k["name"] for k in m["keys"]])["deleted"]
    total = sum(len(m["keys"]) for m in meshes)
    return {"path": item_dir, "meshes": len(meshes), "keys": total,
            "deleted": deleted}


def _verify_shapes_item(item_dir):
    with open(os.path.join(item_dir, "shapes.json"), "r", encoding="utf-8") as f:
        data = json.load(f)
    for m in data["meshes"]:
        for k in m["keys"]:
            raw = zlib.decompress(
                open(os.path.join(item_dir, k["file"]), "rb").read())
            n, cnt = struct.unpack_from("<II", raw, 0)
            if len(raw) != 8 + cnt * 4 + cnt * 12:
                raise RuntimeError("Corrupt sidecar: %s" % k["file"])
    return data


def delete_shape_keys(object_name, key_names):
    """Remove key blocks from a live mesh. Basis is always protected."""
    ob = bpy.data.objects.get(object_name)
    if ob is None or ob.type != 'MESH':
        raise RuntimeError("Mesh object not found: %s" % object_name)
    sk = ob.data.shape_keys
    if sk is None:
        return {"deleted": 0, "remaining": 0}
    ref = sk.reference_key
    deleted = 0
    for nm in key_names:
        kb = sk.key_blocks.get(nm)
        if kb is None or kb == ref:
            continue
        ob.shape_key_remove(kb)
        deleted += 1
    sk = ob.data.shape_keys  # removing the last key can null the datablock
    return {"deleted": deleted, "remaining": len(sk.key_blocks) if sk else 0}


def apply_shapes(item_path, mode="replace", force=False, to_active=False,
                 blend=1.0):
    """Recreate stored keys on the scene's meshes (matched by object name;
    single-mesh items fall back to the active mesh). mode 'replace' overwrites
    an existing same-name key IN PLACE (keeps ordering + relative_key pointers
    of other keys); 'add' leaves existing keys untouched. Vertex count must
    match; a Basis checksum mismatch (mesh edited since saving) raises unless
    force=True. to_active=True targets the ACTIVE mesh regardless of the saved
    object name (rig-to-rig transfer; vert-count/checksum guards still apply).
    blend: 0..1 influence — the stored deltas are scaled by it, so the key at
    value 1.0 gives blend×the saved shape (baked in; 1.0 = exact restore)."""
    data = _verify_shapes_item(item_path)
    t_blend = max(0.0, min(1.0, blend))
    active = None
    if to_active:
        active = bpy.context.active_object
        if active is None or active.type != 'MESH':
            raise RuntimeError("apply to active object: the active object "
                               "is not a mesh")
        if len(data["meshes"]) != 1:
            raise RuntimeError(
                "item stores keys for %d meshes — the active-object override "
                "needs a single-mesh item" % len(data["meshes"]))
    applied = 0
    skipped = []
    for m in data["meshes"]:
        if active is not None:
            ob = active
        else:
            ob = bpy.data.objects.get(m["object"])
            if (ob is None or ob.type != 'MESH') and len(data["meshes"]) == 1:
                ao = bpy.context.active_object
                if ao is not None and ao.type == 'MESH':
                    ob = ao
        if ob is None or ob.type != 'MESH':
            skipped.append("%s: object not in scene" % m["object"])
            continue
        n = len(ob.data.vertices)
        if n != m["verts"]:
            skipped.append("%s: vertex count %d != saved %d (refused)"
                           % (ob.name, n, m["verts"]))
            continue
        if not force and _topo_checksum(ob) != m["checksum"]:
            raise RuntimeError(
                "checksum mismatch on '%s' — mesh was edited since saving; "
                "apply with force to override" % ob.name)
        if ob.data.shape_keys is None:
            ob.shape_key_add(name="Basis", from_mix=False)
        sk = ob.data.shape_keys
        base = _basis_coords(ob).reshape(-1, 3)
        for k in m["keys"]:
            kb = sk.key_blocks.get(k["name"])
            if kb is not None and mode == "add":
                skipped.append("%s/%s: already exists" % (ob.name, k["name"]))
                continue
            if kb is None:
                kb = ob.shape_key_add(name=k["name"], from_mix=False)
            raw = zlib.decompress(
                open(os.path.join(item_path, k["file"]), "rb").read())
            _n, cnt = struct.unpack_from("<II", raw, 0)
            idx = np.frombuffer(raw, dtype=np.uint32, count=cnt, offset=8)
            dl = np.frombuffer(raw, dtype=np.float32, count=cnt * 3,
                               offset=8 + cnt * 4).reshape(-1, 3)
            co = base.copy()
            co[idx] += dl if t_blend >= 1.0 else dl * np.float32(t_blend)
            kb.data.foreach_set("co", np.ascontiguousarray(co).ravel())
            kb.slider_max = k["max"]
            kb.slider_min = k["min"]
            kb.value = k["value"]
            kb.mute = k["mute"]
            kb.vertex_group = k.get("vertex_group") or ""
            rel = sk.key_blocks.get(k.get("relative_to") or "")
            if rel is not None:
                kb.relative_key = rel
            applied += 1
        ob.data.update()
    bpy.context.view_layer.update()
    return {"applied": applied, "skipped": skipped}


# ---------------------------------------------------------------- denoising

DENOISE_TREE_NAME = "MADI Denoise"
_DENOISE_TAG = "madi_denoise"   # id-prop marking a tree WE built (safe to rebuild)
_DENOISE_BACKUP = "madi_denoise_backup"   # scene id-prop: state before setup


def _snapshot_denoise_state(scene, layers, tree_name):
    """Record what the scene looked like BEFORE the setup ran, so the undo can
    put it back exactly. Written once — re-running setup must not overwrite the
    snapshot with already-modified state."""
    if scene.get(_DENOISE_BACKUP):
        return False
    prev = scene.compositing_node_group
    data = {
        "tree": tree_name,
        "prev_group": prev.name if prev is not None else None,
        "use_nodes": bool(scene.use_nodes),
        "layers": {},
    }
    for vl in layers:
        entry = {
            "denoising_store_passes": bool(vl.cycles.denoising_store_passes),
            "use_denoising": bool(vl.cycles.use_denoising),
            "passes": {p: bool(getattr(vl, p)) for p in _PASS_PROPS
                       if hasattr(vl, p)},
            "cycles_passes": {p: bool(getattr(vl.cycles, p))
                              for p in _CYCLES_PASS_PROPS
                              if hasattr(vl.cycles, p)},
        }
        data["layers"][vl.name] = entry
    scene[_DENOISE_BACKUP] = json.dumps(data)
    return True


def clear_denoise(restore_passes=True):
    """Undo setup_denoise: drop the tree we built and put the render settings
    back the way they were.

    restore_passes=False leaves the pass toggles alone and only removes the
    node tree (handy if you want to keep the passes for your own comp).
    """
    scene = bpy.context.scene
    raw = scene.get(_DENOISE_BACKUP)
    data = None
    if raw:
        try:
            data = json.loads(raw)
        except ValueError:
            data = None

    tree_name = (data or {}).get("tree", DENOISE_TREE_NAME)
    removed_tree = None
    restored_layers = []

    # --- unhook + delete OUR tree (never touch a tree we didn't build)
    current = scene.compositing_node_group
    ours = None
    for ng in (current, bpy.data.node_groups.get(tree_name)):
        if ng is not None and ng.get(_DENOISE_TAG):
            ours = ng
            break
    if ours is not None:
        if current == ours:
            scene.compositing_node_group = None
        removed_tree = ours.name
        try:
            bpy.data.node_groups.remove(ours)
        except (RuntimeError, ReferenceError):
            removed_tree = None

    if data:
        # --- put the scene's compositor selection back
        prev_name = data.get("prev_group")
        prev = bpy.data.node_groups.get(prev_name) if prev_name else None
        if scene.compositing_node_group is None:
            scene.compositing_node_group = prev
        scene.use_nodes = bool(data.get("use_nodes", scene.use_nodes))

        # --- and the per-layer render settings
        if restore_passes:
            for name, entry in data.get("layers", {}).items():
                vl = scene.view_layers.get(name)
                if vl is None:
                    continue
                vl.cycles.denoising_store_passes = entry["denoising_store_passes"]
                vl.cycles.use_denoising = entry["use_denoising"]
                for prop, val in entry.get("passes", {}).items():
                    if hasattr(vl, prop):
                        setattr(vl, prop, val)
                for prop, val in entry.get("cycles_passes", {}).items():
                    if hasattr(vl.cycles, prop):
                        setattr(vl.cycles, prop, val)
                restored_layers.append(name)
        del scene[_DENOISE_BACKUP]

    if removed_tree is None and not restored_layers:
        raise RuntimeError(
            "Nothing to undo — no MADI denoise tree in this scene "
            "(a compositor tree you built yourself is never touched)")
    return {"removed_tree": removed_tree,
            "restored_layers": restored_layers,
            "had_snapshot": bool(data),
            "passes_restored": bool(restore_passes and data)}


def _denoise_link(ng, from_node, out_name, to_node, in_name):
    """Link two sockets by name, ignoring ones this build doesn't expose."""
    src = from_node.outputs.get(out_name)
    dst = to_node.inputs.get(in_name)
    if src is not None and dst is not None:
        ng.links.new(src, dst)
        return True
    return False


# Light passes rebuilt into the beauty as (Direct + Indirect) * Color.
# Socket names verified on 5.2.0; Subsurface has no sockets any more (Cycles
# folds SSS into the diffuse passes), so it is deliberately absent.
_LIGHT_COMPONENTS = (
    ("Diffuse", "Diffuse Direct", "Diffuse Indirect", "Diffuse Color"),
    ("Glossy", "Glossy Direct", "Glossy Indirect", "Glossy Color"),
    ("Transmission", "Transmission Direct", "Transmission Indirect",
     "Transmission Color"),
    ("Volume", "Volume Direct", "Volume Indirect", None),  # no colour pass
)
# already noise-free — summed straight back in, never denoised
_FLAT_PASSES = ("Emission", "Environment")

_PASS_PROPS = ("use_pass_diffuse_direct", "use_pass_diffuse_indirect",
               "use_pass_diffuse_color", "use_pass_glossy_direct",
               "use_pass_glossy_indirect", "use_pass_glossy_color",
               "use_pass_transmission_direct", "use_pass_transmission_indirect",
               "use_pass_transmission_color", "use_pass_emit",
               "use_pass_environment")
_CYCLES_PASS_PROPS = ("use_pass_volume_direct", "use_pass_volume_indirect")


def _enable_light_passes(vl):
    """Turn on every pass the per-pass rebuild needs."""
    for prop in _PASS_PROPS:
        if hasattr(vl, prop):
            setattr(vl, prop, True)
    for prop in _CYCLES_PASS_PROPS:
        if hasattr(vl.cycles, prop):
            setattr(vl.cycles, prop, True)


def _mix(ng, blend_type, location, label=""):
    """RGBA Mix node at full factor. 5.x compositor has no MixRGB/Math of its
    own — the unified ShaderNodeMix is what's addable to a compositor tree."""
    n = ng.nodes.new('ShaderNodeMix')
    n.data_type = 'RGBA'
    n.blend_type = blend_type
    n.location = location
    n.label = label
    for s in n.inputs:
        if s.type == 'VALUE' and s.name == "Factor":
            s.default_value = 1.0   # ADD/MULTIPLY must apply in full
            break
    return n


def _mix_io(n):
    """(A, B, Result) of an RGBA Mix — the node carries one socket set per
    data_type, so pick the RGBA ones rather than trusting indices."""
    ins = [s for s in n.inputs if s.type == 'RGBA']
    outs = [s for s in n.outputs if s.type == 'RGBA']
    return ins[0], ins[1], outs[0]


def _sum_sockets(ng, sockets, x, y0, label="Combine"):
    """Chain of ADD mixes summing every socket. Returns the final socket."""
    total = sockets[0]
    for i, sock in enumerate(sockets[1:]):
        add = _mix(ng, 'ADD', (x + i * 190, y0), label)
        a, b, res = _mix_io(add)
        ng.links.new(total, a)
        ng.links.new(sock, b)
        total = res
    return total


def _build_pass_denoise(ng, rl, denoise_nodes, y_base):
    """Per-LIGHT-PASS denoising for one Render Layers node.

    Each light pass gets its OWN Denoise node (they carry very different noise),
    then the beauty is rebuilt: (Direct + Indirect) * Color per component, plus
    Volume, Emission and Environment. Returns (final_socket, info).
    """
    def denoise_of(socket_name, loc, label):
        src = rl.outputs.get(socket_name)
        if src is None:
            return None
        dn = ng.nodes.new('CompositorNodeDenoise')
        dn.location = loc
        dn.label = label
        hdr = dn.inputs.get("HDR")
        if hdr is not None and hasattr(hdr, "default_value"):
            try:
                hdr.default_value = True
            except (TypeError, AttributeError):
                pass
        ng.links.new(src, dn.inputs["Image"])
        _denoise_link(ng, rl, "Denoising Normal", dn, "Normal")
        _denoise_link(ng, rl, "Denoising Albedo", dn, "Albedo")
        denoise_nodes.append(dn)
        return dn.outputs["Image"]

    parts = []
    denoised = []
    skipped = []
    row = 0
    for comp, direct, indirect, color in _LIGHT_COMPONENTS:
        d_out = denoise_of(direct, (340, y_base - row * 210), "Denoise " + direct)
        i_out = denoise_of(indirect, (340, y_base - (row + 1) * 210),
                           "Denoise " + indirect)
        present = [s for s in (d_out, i_out) if s is not None]
        if not present:
            skipped.append(comp)
            continue
        denoised.extend([direct, indirect][:len(present)])
        if len(present) == 2:
            add = _mix(ng, 'ADD', (620, y_base - row * 210), comp + " light")
            a, b, light = _mix_io(add)
            ng.links.new(present[0], a)
            ng.links.new(present[1], b)
        else:
            light = present[0]
        col_sock = rl.outputs.get(color) if color else None
        if col_sock is not None:
            mul = _mix(ng, 'MULTIPLY', (840, y_base - row * 210), comp)
            a, b, res = _mix_io(mul)
            ng.links.new(light, a)
            ng.links.new(col_sock, b)   # colour passes are clean: never denoised
            parts.append(res)
        else:
            parts.append(light)
        row += 2

    flat = []
    for name in _FLAT_PASSES:
        sock = rl.outputs.get(name)
        if sock is not None:
            parts.append(sock)
            flat.append(name)

    if not parts:
        raise RuntimeError(
            "No light passes on the Render Layers node — enable them on the "
            "view layer (or use the per-view-layer mode instead)")

    total = _sum_sockets(ng, parts, 1080, y_base, "Rebuild beauty")

    # the ADD chain sums alpha too; restore the layer's real alpha
    alpha = rl.outputs.get("Alpha")
    if alpha is not None and hasattr(bpy.types, "CompositorNodeSetAlpha"):
        sa = ng.nodes.new('CompositorNodeSetAlpha')
        sa.location = (1080 + len(parts) * 190, y_base)
        sa.label = "Restore alpha"
        img_in = sa.inputs.get("Image") or sa.inputs[0]
        a_in = sa.inputs.get("Alpha")
        if a_in is None and len(sa.inputs) > 1:
            a_in = sa.inputs[1]
        ng.links.new(total, img_in)
        if a_in is not None:
            ng.links.new(alpha, a_in)
        total = sa.outputs[0]

    return total, {"denoised_passes": denoised, "flat_passes": flat,
                   "skipped_components": skipped}


def setup_denoise(view_layers=None, disable_render_denoise=True,
                  tree_name=DENOISE_TREE_NAME, combine="ALPHA_OVER",
                  split="PASSES"):
    """Enable the denoising passes and build the compositor tree.

    split='PASSES' (default): every LIGHT PASS is denoised on its own —
    Diffuse/Glossy/Transmission Direct and Indirect each get their own Denoise
    node (they carry very different noise), then the beauty is rebuilt as
    (Direct + Indirect) * Color per component plus Volume, Emission and
    Environment, with the layer's alpha restored at the end. Colour passes are
    never denoised (they are already clean).

    split='LAYERS': the simpler shape — one Render Layers -> Denoise per view
    layer on the combined image.

    Blender 5.x note: the Composite output node is GONE — the compositor is a
    node group on scene.compositing_node_group with a NodeGroupOutput.

    view_layers: names to include (None = every layer with .use on).
    disable_render_denoise: also switch the layer's render-time denoise OFF, so
    Cycles doesn't denoise once at render and again in comp.
    combine: how to merge multiple layers ('ALPHA_OVER' or 'NONE' = leave the
    per-layer Denoise nodes unconnected for manual wiring).
    """
    scene = bpy.context.scene
    if scene.render.engine != 'CYCLES':
        raise RuntimeError(
            "Compositor denoising needs the Cycles engine (denoising data "
            "passes are Cycles-only) — current engine is %s"
            % scene.render.engine)

    if view_layers:
        layers = [vl for vl in scene.view_layers if vl.name in view_layers]
    else:
        layers = [vl for vl in scene.view_layers if vl.use]
    if not layers:
        raise RuntimeError("No enabled view layers to set up")

    # snapshot BEFORE anything changes, so the undo can restore it exactly
    _snapshot_denoise_state(scene, layers, tree_name)

    passes_on = []
    for vl in layers:
        vl.cycles.denoising_store_passes = True
        if disable_render_denoise:
            vl.cycles.use_denoising = False
        if split == 'PASSES':
            _enable_light_passes(vl)
        passes_on.append(vl.name)

    # --- the node group (rebuild ours in place; never clobber a user's tree)
    ng = bpy.data.node_groups.get(tree_name)
    if ng is not None and (ng.bl_idname != 'CompositorNodeTree'
                           or not ng.get(_DENOISE_TAG)):
        ng = None
        tree_name = tree_name + " (MADI)"
        ng = bpy.data.node_groups.get(tree_name)
        if ng is not None and not ng.get(_DENOISE_TAG):
            ng = None
    if ng is None:
        ng = bpy.data.node_groups.new(tree_name, 'CompositorNodeTree')
    ng[_DENOISE_TAG] = True
    ng.nodes.clear()

    has_output = any(getattr(s, "in_out", None) == 'OUTPUT'
                     for s in ng.interface.items_tree)
    if not has_output:
        ng.interface.new_socket("Image", in_out='OUTPUT',
                                socket_type='NodeSocketColor')

    sources = {}
    denoise_nodes = []
    layer_outs = []      # one final socket per view layer
    pass_info = {}
    for i, vl in enumerate(layers):
        y = -i * 1500 if split == 'PASSES' else -i * 420
        rl = ng.nodes.new('CompositorNodeRLayers')
        rl.location = (0, y)
        rl.scene = scene
        rl.layer = vl.name
        rl.label = vl.name

        if split == 'PASSES':
            final, info = _build_pass_denoise(ng, rl, denoise_nodes, y)
            pass_info[vl.name] = info
            sources[vl.name] = "light passes"
            layer_outs.append(final)
            continue

        dn = ng.nodes.new('CompositorNodeDenoise')
        dn.location = (330, y)
        dn.label = "Denoise — %s" % vl.name
        # HDR is a socket in 5.x (was a property): renders are HDR, keep it on
        hdr = dn.inputs.get("HDR")
        if hdr is not None and hasattr(hdr, "default_value"):
            try:
                hdr.default_value = True
            except (TypeError, AttributeError):
                pass
        # 'Noisy Image' only EXISTS while the layer's render-time denoise is on
        # (it's the pre-denoise copy). With render denoise off, the beauty
        # 'Image' is itself the noisy one — that's the input we want.
        src = "Noisy Image" if rl.outputs.get("Noisy Image") else "Image"
        _denoise_link(ng, rl, src, dn, "Image")
        sources[vl.name] = src
        _denoise_link(ng, rl, "Denoising Normal", dn, "Normal")
        _denoise_link(ng, rl, "Denoising Albedo", dn, "Albedo")
        denoise_nodes.append(dn)
        layer_outs.append(dn.outputs["Image"])

    out = ng.nodes.new('NodeGroupOutput')
    out.location = (2400 if split == 'PASSES' else 1000, 0)

    combined = layer_outs[0] if layer_outs else None
    if combine == 'ALPHA_OVER' and len(layer_outs) > 1:
        for i, sock in enumerate(layer_outs[1:], start=1):
            over = ng.nodes.new('CompositorNodeAlphaOver')
            over.location = (2100, -i * 220)
            over.label = "Combine layers"
            # 5.x names these Background/Foreground (older builds: two "Image")
            ins = [over.inputs.get("Background"), over.inputs.get("Foreground")]
            if None in ins:
                ins = [s for s in over.inputs if s.type == 'RGBA'][:2]
            if len(ins) == 2 and None not in ins:
                ng.links.new(combined, ins[0])
                ng.links.new(sock, ins[1])
            combined = over.outputs["Image"]
    elif combine != 'ALPHA_OVER' and len(layer_outs) > 1:
        combined = None   # leave the per-layer results for manual wiring

    if combined is not None and out.inputs:
        ng.links.new(combined, out.inputs[0])

    scene.use_nodes = True
    scene.compositing_node_group = ng
    return {"tree": ng.name, "layers": passes_on, "split": split,
            "denoise_nodes": len(denoise_nodes),
            "render_denoise_disabled": bool(disable_render_denoise),
            "image_sources": sources, "passes": pass_info,
            "combined": combine if len(layer_outs) > 1 else "single layer"}


# ---------------------------------------------------------------- alembic

def _op_context():
    """Bridge requests run from a timer, where context.window is None — hand
    the io operators a real window or they can misbehave."""
    wm = bpy.context.window_manager
    if wm.windows:
        return bpy.context.temp_override(window=wm.windows[0])
    import contextlib
    return contextlib.nullcontext()


# Every Alembic export option the app offers, with BLENDER'S OWN DEFAULTS —
# read off `wm.alembic_export`'s RNA on 5.2, not copied out of the manual.
#
# ⚠ `selected` IS THE ONE DELIBERATE DIFFERENCE. Blender defaults it off; this
# item type has always exported the selection, and quietly flipping that would
# turn "export this character" into "export the whole scene" for anyone who had
# already saved one.
ABC_OPTIONS = {
    "selected": True,
    "flatten": False,
    "use_instancing": True,
    "uvs": True,
    "packuv": True,
    "normals": True,
    "vcolors": False,
    "orcos": True,
    "face_sets": False,
    "curves_as_mesh": False,
    "export_custom_properties": True,
    "apply_subdiv": False,
    "subdiv_schema": False,
    "global_scale": 1.0,
    "triangulate": False,
    "quad_method": 'SHORTEST_DIAGONAL',
    "ngon_method": 'BEAUTY',
    "export_hair": True,
    "export_particles": True,
    "evaluation_mode": 'RENDER',
    "xsamples": 1,
    "gsamples": 1,
    "sh_open": 0.0,
    "sh_close": 1.0,
}

# What an enum will actually accept, and what a number may be. Both are needed:
# the operator RAISES on a value outside them, and one bad field would take the
# whole export down rather than the setting it belongs to.
ABC_ENUMS = {
    "quad_method": ('BEAUTY', 'FIXED', 'FIXED_ALTERNATE', 'SHORTEST_DIAGONAL',
                    'LONGEST_DIAGONAL'),
    "ngon_method": ('BEAUTY', 'CLIP'),
    "evaluation_mode": ('RENDER', 'VIEWPORT'),
}
ABC_RANGES = {
    "global_scale": (0.0001, 1000.0),
    "xsamples": (1, 128),
    "gsamples": (1, 128),
    "sh_open": (-1.0, 1.0),
    "sh_close": (-1.0, 1.0),
}


def abc_options(options=None):
    """The caller's choices merged over the defaults, sanitised.

    ⚠ AN UNKNOWN KEY IS DROPPED, NOT PASSED THROUGH. A newer app would
    otherwise hand `wm.alembic_export` a keyword this Blender has never heard
    of and the whole export would die with a TypeError — exactly the "an update
    must never break anything" case this project is built around. Same for a
    value of the wrong type or outside the operator's range: keep the default
    and export, rather than fail over a checkbox.
    """
    out = dict(ABC_OPTIONS)
    for key, default in ABC_OPTIONS.items():
        if not options or key not in options:
            continue
        value = options[key]
        try:
            # ⚠ bool BEFORE int — `isinstance(True, int)` is True in Python, so
            # the other order turns every checkbox into 0/1 and the operator
            # then refuses the ones it declares as booleans.
            if isinstance(default, bool):
                out[key] = bool(value)
            elif isinstance(default, int):
                out[key] = int(value)
            elif isinstance(default, float):
                out[key] = float(value)
            elif str(value) in ABC_ENUMS.get(key, ()):
                out[key] = str(value)
        except (TypeError, ValueError):
            continue                     # keep the default
        low, high = ABC_RANGES.get(key, (None, None))
        if low is not None:
            out[key] = type(default)(min(max(out[key], low), high))
    return out


def save_abc(library_root, relfolder, name, frame_start=None, frame_end=None,
             description="", overwrite=False, options=None):
    """Export the SELECTED objects to <name>.abc/cache.abc (evaluated results —
    sims/constraints/GN are sampled per frame by the Alembic exporter).

    `options` is any subset of ABC_OPTIONS; everything else takes Blender's own
    default. What was actually used is written into abc.json, so a cache that
    came out wrong can be read rather than guessed at."""
    opts = abc_options(options)
    sel = list(bpy.context.selected_objects)
    if opts["selected"] and not sel:
        raise RuntimeError("Nothing selected — select the object(s) to export")
    scene = bpy.context.scene
    fs = scene.frame_start if frame_start is None else int(frame_start)
    fe = scene.frame_end if frame_end is None else int(frame_end)
    if fe < fs:
        fs, fe = fe, fs
    item_dir = os.path.join(library_root, relfolder, safe_name(name) + ABC_EXT)
    if os.path.isfile(item_dir):
        raise RuntimeError("A loose .abc file with this name is already there: "
                           "%s — rename or convert it first" % item_dir)
    if os.path.isdir(item_dir):
        if not overwrite:
            raise RuntimeError("Item already exists: %s (use overwrite)" % item_dir)
        version_item(item_dir)
    os.makedirs(item_dir, exist_ok=True)
    filepath = os.path.join(item_dir, "cache.abc")
    cur = scene.frame_current
    try:
        with _op_context():
            bpy.ops.wm.alembic_export(filepath=filepath, start=fs, end=fe,
                                      as_background_job=False, **opts)
    finally:
        if scene.frame_current != cur:
            scene.frame_set(cur)
    if not os.path.isfile(filepath):
        raise RuntimeError("Alembic export produced no file")
    size = os.path.getsize(filepath)
    exported = ([o.name for o in sel] if opts["selected"]
                else [o.name for o in scene.objects])
    anchor = sel[0] if sel else (scene.objects[0] if len(scene.objects) else None)
    data = {"type": "abc",
            "metadata": _metadata(anchor, {"description": description,
                                           "objects": exported,
                                           "frame_start": fs, "frame_end": fe,
                                           "size_bytes": size,
                                           "options": opts})}
    with open(os.path.join(item_dir, "abc.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
    return {"path": item_dir, "objects": len(exported), "frame_start": fs,
            "frame_end": fe, "size_bytes": size, "options": opts}


def apply_abc(path):
    """Import a library .abc into the scene as new objects. `path` is either a
    <name>.abc item FOLDER (cache.abc inside) or a loose .abc file."""
    abc_file = os.path.join(path, "cache.abc") if os.path.isdir(path) else path
    if not os.path.isfile(abc_file):
        raise RuntimeError("No .abc cache found at: %s" % path)
    ob = bpy.context.active_object
    if ob is not None and bpy.context.mode != 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')
    before = {o.name for o in bpy.data.objects}
    with _op_context():
        # set_frame_range=False: adding a cache must not stomp the scene range
        bpy.ops.wm.alembic_import(filepath=abc_file, set_frame_range=False,
                                  as_background_job=False)
    new = [o.name for o in bpy.data.objects if o.name not in before]
    return {"imported": len(new), "objects": new}


# ---------------------------------------------------------------- playblast

def scene_output_dir():
    """Blender's OWN render output folder, absolute, or None if it can't be
    resolved. Reported in `status` so a playblast lands where this scene's
    renders land (Marty, 2026-08-05).

    ⚠ `render.filepath` IS A PREFIX, NOT A FOLDER. `//renders/shot_` means the
    folder `renders` holding files called `shot_0001.png`. Only a trailing
    separator (or an existing directory) makes the whole string a folder.
    Reading it as one either invents a folder named after his file prefix or
    drops the playblast one level up from where his renders go.
    """
    raw = bpy.context.scene.render.filepath or ""
    if not raw:
        return None
    # A '//' path in a file that was never saved has nothing to resolve
    # against — better to say "don't know" and let the app keep its own
    # default than to hand back a fragment it would then create a folder for.
    if raw.startswith("//") and not bpy.data.filepath:
        return None
    path = bpy.path.abspath(raw)
    out = path if (raw.endswith(("/", "\\")) or os.path.isdir(path)) \
        else os.path.dirname(path)
    out = os.path.normpath(out) if out else ""
    return out if out and os.path.isabs(out) else None


def save_blend():
    """Save the .blend Blender currently has open. -> {path, size, was_dirty}

    Built for the Render Queue's "Save & Queue" (Marty, 2026-08-10): the queue
    renders FILES ON DISK, so queueing the open scene without saving it first
    would render whatever was last written — silently the wrong thing, and the
    hardest kind of wrong to notice, because the job runs and produces frames.

    ⚠ REFUSES A FILE THAT HAS NEVER BEEN SAVED, rather than inventing a path.
    `wm.save_mainfile()` with no filepath raises inside Blender, and even if it
    did not, picking a folder on the user's behalf is not this command's call —
    the app turns this into "save it once in Blender first".

    ⚠ `was_dirty` is read BEFORE the save. Afterwards `is_dirty` is always False,
    so asking after the fact can only ever answer "it was clean", and the app's
    message would tell the user nothing happened when it had.
    """
    path = bpy.data.filepath
    if not path:
        raise RuntimeError(
            "This .blend has never been saved, so it has no path to save to. "
            "Save it once in Blender (File > Save As) and try again.")
    was_dirty = bool(bpy.data.is_dirty)
    bpy.ops.wm.save_mainfile()
    # Re-read rather than reusing `path`: a save is where a relative path
    # becomes absolute, and the queue needs the real one.
    saved = bpy.data.filepath
    try:
        size = os.path.getsize(saved)
    except OSError:
        size = 0
    return {"path": saved, "size": size, "was_dirty": was_dirty}


# The newest viewport render made through the Toolset, recorded so BOTH sides
# can offer a "watch it" button (Marty, 2026-08-05).
LAST_RENDER_FILE = "last_render.json"
_APP_FOLDER = "MadihsonNSFW Toolset"


def _shared_state_dir():
    """⚠ Must stay byte-identical to `app\\lastrender.py`'s copy — it is the
    same file on disk, written by whichever side made the render.
    `tests\\anim_options_test.py` compares the two."""
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, _APP_FOLDER)


_last_render_cache = [0.0, None]   # [checked_at (monotonic), path or None]


def note_last_render(path):
    """Record `path` as the newest viewport render.

    ⚠ A SHARED FILE, not a module global and not a scene property, because
    TWO DIFFERENT PROCESSES produce these. This add-on writes the blocking
    playblast; the APP writes the background one, which a headless Blender
    renders and this session never sees at all. A global would know about half
    of them and would forget those on the next restart.
    """
    try:
        d = _shared_state_dir()
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, LAST_RENDER_FILE), "w", encoding="utf-8") as f:
            json.dump({"path": path, "written": time.time()}, f)
    except (OSError, TypeError):
        pass    # a missing record only costs a greyed-out button
    _last_render_cache[0] = 0.0     # our own write invalidates the read cache


def last_render(max_age=2.0):
    """The recorded newest render, or None. Checks the file is still THERE —
    the record long outlives a playblast somebody tidied away, and a button
    that opens a missing file is worse than one that is greyed out.

    ⚠ CACHED FOR max_age SECONDS BECAUSE A PANEL `poll` CALLS IT. Blender
    re-draws an N-panel on mouse movement over the region, and an uncached
    version would do a json read plus a stat every one of those. `max_age=0`
    forces a fresh read (what the bridge command uses).
    """
    now = time.monotonic()
    if max_age and 0.0 < now - _last_render_cache[0] < max_age:
        return _last_render_cache[1]
    try:
        with open(os.path.join(_shared_state_dir(), LAST_RENDER_FILE),
                  "r", encoding="utf-8") as f:
            path = json.load(f).get("path")
    except (OSError, ValueError):
        path = None
    path = path if path and os.path.isfile(path) else None
    _last_render_cache[0] = now
    _last_render_cache[1] = path
    return path


def playblast(output, frame_start=None, frame_end=None, use_camera=False,
              resolution_percent=50, overlays=False):
    """Maya-style playblast: Viewport Render Animation to an H.264 mp4.

    use_camera=False -> the first 3D viewport exactly as shown (its shading;
    overlays togglable). use_camera=True -> the active scene camera (solid
    display, no overlays). Single-instance render: Blender BLOCKS until done.
    """
    scene = bpy.context.scene
    rd = scene.render
    fs = scene.frame_start if frame_start is None else int(frame_start)
    fe = scene.frame_end if frame_end is None else int(frame_end)
    if fe < fs:
        fs, fe = fe, fs
    if use_camera and scene.camera is None:
        raise RuntimeError("No active camera in the scene")
    window, area, region = _find_view3d()
    if area is None:
        raise RuntimeError("No 3D Viewport found to render from")
    space = area.spaces.active
    out_dir = os.path.dirname(output)
    os.makedirs(out_dir, exist_ok=True)

    ff = rd.ffmpeg
    has_media_type = hasattr(rd.image_settings, "media_type")
    saved = {
        "filepath": rd.filepath,
        "resolution_percentage": rd.resolution_percentage,
        "media_type": rd.image_settings.media_type if has_media_type else None,
        "file_format": rd.image_settings.file_format,
        "color_mode": rd.image_settings.color_mode,
        "ffmpeg_format": ff.format,
        "ffmpeg_codec": ff.codec,
        "ffmpeg_crf": ff.constant_rate_factor,
        "ffmpeg_audio": ff.audio_codec,
        "use_file_extension": rd.use_file_extension,
        "use_overwrite": rd.use_overwrite,
        "frame_start": scene.frame_start,
        "frame_end": scene.frame_end,
        "frame": scene.frame_current,
        "overlays": space.overlay.show_overlays,
    }
    try:
        rd.filepath = output
        rd.resolution_percentage = int(resolution_percent)
        # media_type gates file_format in 5.x — VIDEO first, then FFMPEG
        if has_media_type:
            rd.image_settings.media_type = 'VIDEO'
        rd.image_settings.file_format = 'FFMPEG'
        rd.image_settings.color_mode = 'RGB'
        ff.format = 'MPEG4'   # container first: changing it can reset the codec
        ff.codec = 'H264'
        ff.constant_rate_factor = 'MEDIUM'
        ff.audio_codec = 'NONE'
        rd.use_file_extension = True
        rd.use_overwrite = True
        scene.frame_start = fs   # render.opengl(animation) renders scene range
        scene.frame_end = fe
        if not use_camera:
            space.overlay.show_overlays = bool(overlays)
        with bpy.context.temp_override(window=window, area=area, region=region):
            bpy.ops.render.opengl(animation=True, view_context=not use_camera)
    finally:
        space.overlay.show_overlays = saved["overlays"]
        scene.frame_start = saved["frame_start"]
        scene.frame_end = saved["frame_end"]
        scene.frame_set(saved["frame"])
        rd.filepath = saved["filepath"]
        rd.resolution_percentage = saved["resolution_percentage"]
        if has_media_type:
            rd.image_settings.media_type = saved["media_type"]  # BEFORE file_format
        rd.image_settings.file_format = saved["file_format"]
        rd.image_settings.color_mode = saved["color_mode"]
        ff.format = saved["ffmpeg_format"]
        ff.codec = saved["ffmpeg_codec"]
        ff.constant_rate_factor = saved["ffmpeg_crf"]
        ff.audio_codec = saved["ffmpeg_audio"]
        rd.use_file_extension = saved["use_file_extension"]
        rd.use_overwrite = saved["use_overwrite"]
    # Blender may decorate the container name (frame range) — find what it wrote
    if os.path.isfile(output):
        written = output
    else:
        stem = os.path.splitext(os.path.basename(output))[0]
        cands = [os.path.join(out_dir, f) for f in os.listdir(out_dir)
                 if f.startswith(stem) and f.lower().endswith(".mp4")]
        if not cands:
            raise RuntimeError("Playblast produced no file in %s" % out_dir)
        written = max(cands, key=os.path.getmtime)
    note_last_render(written)
    return {"path": written, "frame_start": fs, "frame_end": fe,
            "size_bytes": os.path.getsize(written),
            "fps": round(rd.fps / rd.fps_base, 3)}


def snapshot_blend(path=None):
    """Write a throwaway COPY of the current file, for the app to render
    headlessly (background playblast) while this Blender stays free.

    `copy=True` is the whole trick: the .blend is written, but the running
    session is NOT re-pointed at it — bpy.data.filepath stays whatever the user
    is actually working on, so "Save" still goes to their real file. Unsaved
    edits DO land in the copy, which is the point: a playblast has to show the
    scene as it is right now, not as it was last saved.

    relative_remap rewrites // paths for the new location, or the headless
    render would open the snapshot with every linked texture missing.

    Returns what the queue needs to build the job (range, fps, camera) — the
    caller is responsible for deleting the file when the render is done.
    """
    scene = bpy.context.scene
    rd = scene.render
    if scene.camera is None:
        # The background path renders through the normal pipeline, which needs
        # a camera. Say so here rather than failing 30 seconds into a render.
        raise RuntimeError("No active camera in the scene")
    if path:
        out = bpy.path.abspath(path)
    else:
        stem = os.path.splitext(os.path.basename(bpy.data.filepath))[0] or "untitled"
        out = os.path.join(
            tempfile.gettempdir(),
            "madi_playblast_%s_%s.blend" % (safe_name(stem),
                                            time.strftime("%Y%m%d_%H%M%S")))
    out_dir = os.path.dirname(out)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    bpy.ops.wm.save_as_mainfile(filepath=out, copy=True, relative_remap=True,
                                compress=False)
    if not os.path.isfile(out):
        raise RuntimeError("Snapshot was not written to %s" % out)
    return {"path": out, "frame_start": scene.frame_start,
            "frame_end": scene.frame_end,
            "fps": round(rd.fps / rd.fps_base, 3),
            "camera": scene.camera.name,
            "resolution": [rd.resolution_x, rd.resolution_y,
                           rd.resolution_percentage],
            "size_bytes": os.path.getsize(out),
            "source": bpy.data.filepath}


# ---------------------------------------------------------------- node tools
# Ported from the Image Node Tools add-on (madi-image-node-tools v1.3.0) so the
# external app can drive Relink / Sequence Setup over the bridge. The upstream
# operators read context.space_data.edit_tree; here the bridge runs in a timer
# with no editor context, so the tree comes from whichever Node Editor is open
# (the user's intent), with the scene compositor group as the sequence
# fallback. Selection/active flags live on the tree datablock, so they are
# readable without any editor. Upstream invariants kept: snapshot links before
# relinking (links.new unplugs mid-iteration), match sockets by name THEN type
# (Mix repeats names per data type), frame_offset = first_file - 1, and NO
# disk scan in status calls (renders live on a network drive).

def _node_outgoing(node):
    """All links leaving this node's output sockets."""
    return [link for socket in node.outputs for link in socket.links]


def _open_node_trees():
    """Every node tree currently open in a Node Editor, editor order."""
    trees = []
    wm = bpy.context.window_manager
    for win in wm.windows:
        for area in win.screen.areas:
            if area.type != 'NODE_EDITOR':
                continue
            space = area.spaces.active
            tree = getattr(space, "edit_tree", None)
            if tree is not None and tree not in trees:
                trees.append(tree)
    return trees


def _relink_tree():
    """The tree Relink acts on: the open Node Editor's tree (any type)."""
    trees = _open_node_trees()
    if not trees:
        raise RuntimeError("Open a Node Editor on the tree you want to relink")
    if len(trees) > 1:
        # Several editors open — take the one with a selection, else refuse.
        with_sel = [t for t in trees if any(n.select for n in t.nodes)]
        if len(with_sel) == 1:
            return with_sel[0]
        raise RuntimeError("Several Node Editors are open — select nodes in "
                           "just one of them")
    return trees[0]


def _sequence_tree():
    """The compositor tree for Sequence Setup: an open compositor editor's
    tree first, else the scene's compositor group directly."""
    for tree in _open_node_trees():
        if tree.bl_idname == 'CompositorNodeTree':
            return tree
    tree = getattr(bpy.context.scene, "compositing_node_group", None)
    if tree is None:
        raise RuntimeError("Scene has no compositor node group yet")
    return tree


def resolve_relink_pair(tree):
    """Which node donates its links and which nodes receive them.
    Returns (source, targets, error) — error is None on success."""
    if tree is None:
        return None, [], "No node tree open"
    if tree.library is not None:
        return None, [], "Node tree is linked from another file (read-only)"

    selected = [n for n in tree.nodes if n.select]
    if not selected:
        return None, [], "Select the connected node and the unconnected one"

    wired = [n for n in selected if _node_outgoing(n)]
    free = [n for n in selected if not _node_outgoing(n)]

    if len(wired) > 1:
        return None, [], "More than one selected node has outgoing links"

    if len(wired) == 1:
        if not free:
            return None, [], "Also select the unconnected node to link up"
        return wired[0], free, None

    # Nothing selected is wired: look for a single obvious donor of the same
    # node type elsewhere in the tree.
    kind = free[0].bl_idname
    if any(n.bl_idname != kind for n in free):
        return None, [], "Selected nodes are different types — select the source too"

    candidates = [
        n for n in tree.nodes
        if n.bl_idname == kind and not n.select and _node_outgoing(n)
    ]
    if len(candidates) == 1:
        return candidates[0], free, None
    if not candidates:
        return None, [], "Nothing selected has outgoing links to copy"
    return None, [], "Several %s nodes are wired — select the source too" % kind


def _match_node_socket(source_socket, source_index, sockets, match_mode,
                       index_fallback):
    """Find the socket in `sockets` that stands in for `source_socket`."""
    if match_mode == 'NAME':
        named = [s for s in sockets if s.name == source_socket.name]
        # Node types like Mix repeat a socket name once per data type, so
        # prefer the one whose type also matches before settling for any.
        for socket in named:
            if socket.enabled and socket.type == source_socket.type:
                return socket
        for socket in named:
            if socket.enabled:
                return socket
        if not index_fallback:
            return None

    if source_index < len(sockets):
        socket = sockets[source_index]
        if socket.enabled:
            return socket
    return None


def relink_nodes(match_mode='NAME', index_fallback=False, copy_inputs=False):
    """Move the wired node's links onto the unconnected selected node(s)."""
    tree = _relink_tree()
    source, targets, error = resolve_relink_pair(tree)
    if error:
        raise RuntimeError(error)

    made = 0
    missing = []
    for target in targets:
        for index, socket in enumerate(source.outputs):
            if not socket.links:
                continue
            replacement = _match_node_socket(
                socket, index, target.outputs, match_mode, index_fallback)
            if replacement is None:
                missing.append(socket.name)
                continue
            # Snapshot the links first: tree.links.new() unplugs the source
            # as it goes, mutating socket.links mid-iteration.
            for link in list(socket.links):
                new_link = tree.links.new(replacement, link.to_socket)
                new_link.is_muted = link.is_muted
                made += 1

        if not copy_inputs:
            continue

        for index, socket in enumerate(source.inputs):
            if not socket.links:
                continue
            replacement = _match_node_socket(
                socket, index, target.inputs, match_mode, index_fallback)
            if replacement is None:
                missing.append(socket.name)
                continue
            for link in list(socket.links):
                new_link = tree.links.new(link.from_socket, replacement)
                new_link.is_muted = link.is_muted
                made += 1

    if not made:
        raise RuntimeError("No matching sockets — nothing to relink")
    return {"made": made, "source": source.name,
            "targets": [n.name for n in targets],
            "tree": tree.name, "tree_type": tree.bl_idname,
            "missing": sorted(set(missing))}


# Splits "sq02_sc01.027_0180.exr" into "sq02_sc01.027_" / "0180" / ".exr".
# The base is non-greedy but the extension is anchored, so backtracking always
# lands on the last run of digits even when the name contains earlier numbers.
_FRAME_PATTERN = re.compile(r"^(?P<base>.*?)(?P<number>\d+)(?P<ext>\.[^.]*)$")


def scan_image_sequence(filepath):
    """Count the frames of the sequence `filepath` belongs to.
    Returns (info, error) — info has directory/base/ext/padding/first/last/
    count/gaps, or (None, message)."""
    if not filepath:
        return None, "Node has no image loaded"

    absolute = bpy.path.abspath(filepath)
    directory, filename = os.path.split(absolute)
    if not os.path.isdir(directory):
        return None, "Folder not found: %s" % directory

    match = _FRAME_PATTERN.match(filename)
    if match is None:
        return None, "No frame number in '%s'" % filename

    base = match.group("base")
    ext = match.group("ext")
    padding = len(match.group("number"))

    # Other renders and stray sub-folders live alongside the frames, so match
    # the exact name shape rather than trusting everything in the directory.
    frame_of = re.compile(
        r"^%s(\d+)%s$" % (re.escape(base), re.escape(ext)), re.IGNORECASE)
    numbers = []
    for entry in os.listdir(directory):
        found = frame_of.match(entry)
        if found and os.path.isfile(os.path.join(directory, entry)):
            numbers.append(int(found.group(1)))

    if not numbers:
        return None, "No frames matching '%s####%s'" % (base, ext)

    numbers.sort()
    return {
        "directory": directory,
        "base": base,
        "ext": ext,
        "padding": padding,
        "first": numbers[0],
        "last": numbers[-1],
        "count": len(numbers),
        "gaps": len(numbers) != numbers[-1] - numbers[0] + 1,
    }, None


def _render_root():
    """The project's Render folder, derived from the .blend location."""
    if not bpy.data.filepath:
        return None
    root = os.path.join(os.path.dirname(bpy.data.filepath), "Render")
    return root if os.path.isdir(root) else None


def _shot_folder(directory):
    """The shot folder a rendered frame sits under, e.g. 'sq02_sc01.027'.
    Frames live at Render/<shot>/exr/... or deeper, so climb until the parent
    is the Render root."""
    root = _render_root()
    if root is None:
        return None
    current = os.path.normpath(directory)
    root = os.path.normpath(root)
    while True:
        parent = os.path.dirname(current)
        if os.path.normcase(parent) == os.path.normcase(root):
            return os.path.basename(current)
        if parent == current:
            return None
        current = parent


def _short_shot(shot):
    """'sq02_sc01.027_active' -> 'sq02_sc01.27', matching the existing output
    files (the shot folder keeps its padding, the filename drops it)."""
    shot = re.sub(r"_active$", "", shot, flags=re.IGNORECASE)
    return re.sub(r"\.0*(\d+)$", lambda m: "." + m.group(1), shot)


def build_sequence_output_path(directory, folder, suffix):
    """The render output path for a sequence living in `directory`.
    Returns (path, error). Mirrors whichever style the scene already uses:
    a '//' relative path stays relative, an absolute one stays absolute."""
    root = _render_root()
    if root is None:
        return None, "Save the .blend next to a Render folder first"

    shot = _shot_folder(directory)
    if shot is None:
        return None, "Sequence is not inside the project's Render folder"

    name = _short_shot(shot) + suffix
    path = os.path.join(root, shot, folder, name)

    if bpy.context.scene.render.filepath.startswith("//"):
        path = bpy.path.relpath(path)
    return path, None


def _sequence_image_node(tree):
    """The Image node Sequence Setup should act on, or None.
    Prefers the active node, falls back to a lone selected Image node."""
    if tree is None:
        return None
    active = tree.nodes.active
    if (active is not None and active.bl_idname == 'CompositorNodeImage'
            and active.select):
        return active
    selected = [n for n in tree.nodes
                if n.select and n.bl_idname == 'CompositorNodeImage']
    return selected[0] if len(selected) == 1 else None


def setup_image_sequence(set_scene_range=True, start_at_one=True,
                         set_output=True, output_folder="exr_composited",
                         output_suffix="_exr_composited_"):
    """Count the frames on disk and fill in the sequence node, scene range and
    render output path (the Image Node Tools operator, bridge-driven)."""
    scene = bpy.context.scene
    tree = _sequence_tree()
    node = _sequence_image_node(tree)
    if node is None or node.image is None:
        raise RuntimeError("Select one Image node with an image loaded "
                           "(in Blender's compositor)")

    info, error = scan_image_sequence(node.image.filepath)
    if error:
        raise RuntimeError(error)

    node.image.source = 'SEQUENCE'
    start = 1 if start_at_one else scene.frame_start
    # CompositorNodeImage has no image_user — the frame properties sit
    # directly on the node (see BLENDER_NOTES).
    node.frame_start = start
    node.frame_duration = info["count"]
    # Blender resolves the file number as cfra - frame_start + 1 + frame_offset
    # so this offset puts the first file on the scene's start frame.
    node.frame_offset = info["first"] - 1
    node.use_auto_refresh = True

    if set_scene_range:
        scene.frame_start = start
        scene.frame_end = start + info["count"] - 1
        if not (scene.frame_start <= scene.frame_current <= scene.frame_end):
            scene.frame_current = scene.frame_start

    notes = []
    output = None
    if set_output:
        output, path_error = build_sequence_output_path(
            info["directory"], output_folder, output_suffix)
        if path_error:
            notes.append(path_error)
            output = None
        else:
            scene.render.filepath = output

    if info["gaps"]:
        notes.append("frames %d-%d have gaps" % (info["first"], info["last"]))

    return {"node": node.name, "count": info["count"],
            "first": info["first"], "last": info["last"],
            "scene_start": scene.frame_start, "scene_end": scene.frame_end,
            "range_set": bool(set_scene_range), "output": output,
            "notes": notes}


def node_tools_status(output_folder="exr_composited",
                      output_suffix="_exr_composited_"):
    """What Relink / Sequence Setup would act on right now. Pure read — no
    disk scan (the path preview is string math only) and nothing modified."""
    out = {"editors": [{"name": t.name, "type": t.bl_idname}
                       for t in _open_node_trees()]}

    # Relink preview
    try:
        tree = _relink_tree()
        source, targets, error = resolve_relink_pair(tree)
        relink = {"tree": tree.name, "tree_type": tree.bl_idname,
                  "error": error}
        if error is None:
            relink["source"] = source.name
            relink["links"] = len(_node_outgoing(source))
            relink["targets"] = [n.name for n in targets]
    except RuntimeError as exc:
        relink = {"error": str(exc)}
    out["relink"] = relink

    # Sequence preview
    try:
        tree = _sequence_tree()
        node = _sequence_image_node(tree)
        if node is None:
            seq = {"error": "Select one Image node (in Blender's compositor)"}
        elif node.image is None:
            seq = {"error": "%s has no image" % node.name}
        else:
            seq = {"error": None, "node": node.name,
                   "file": os.path.basename(node.image.filepath),
                   "frames_now": node.frame_duration,
                   "offset_now": node.frame_offset}
            directory = os.path.dirname(bpy.path.abspath(node.image.filepath))
            path, path_error = build_sequence_output_path(
                directory, output_folder, output_suffix)
            seq["output_preview"] = path
            seq["output_error"] = path_error
    except RuntimeError as exc:
        seq = {"error": str(exc)}
    out["sequence"] = seq
    return out


# ---------------------------------------------------------------- anim layers
# A layer is one NLA track holding exactly one strip with one action. The
# stack lives on the object's AnimData (data_type OBJECT) or on the shape-key
# datablock's own AnimData (data_type SHAPEKEY). Tracks that break the
# one-strip invariant (or are track-locked) surface as locked rows — every
# operation refuses them with a clear message instead of guessing.

AL_BLEND_TYPES = ('REPLACE', 'COMBINE', 'ADD', 'SUBTRACT', 'MULTIPLY')
AL_SOLO_PROP = "madi_al_solo"        # id-prop on the owning ID: solo snapshot
AL_MANAGED_PROP = "madi_al_managed"  # id-prop: this stack is ours / adopted
AL_INFL_PROP = "madi_al_infl_anim"   # id-prop: {track_name: True} = the user
#   WANTS animated influence there. Needed because use_animated_influence is
#   True even for our static influence (Blender reverts a plain property
#   write to 1.0 otherwise) — without this flag every influence change was
#   getting KEYED at the current frame, and playback made the value drift
#   (found live by Marty, 2026-08-01).
AL_RANGE_PROP = "madi_al_range"      # id-prop: {track_name: {"sync": bool}} =
#   this layer has a USER-SET frame range. Those strips are exempt from the
#   scene-range auto-extend in _al_ensure_ranges — extending a repeating
#   strip rewrites its `repeat`, so without the exemption the auto-repair
#   would fight every custom range. "sync" is upstream's Always Sync: keep
#   the span at action length x speed x repeat on every op.
#   All three flag dicts are keyed by TRACK NAME (NlaStrip / NlaTrack cannot
#   hold id-properties at all — verified 5.2), so renames, deletes and merges
#   must maintain them.


def _al_flag_dict(idb, prop):
    raw = idb.get(prop)
    if not raw:
        return {}
    try:
        flags = json.loads(raw)
        return flags if isinstance(flags, dict) else {}
    except (TypeError, ValueError):
        return {}


def _al_write_flag_dict(idb, prop, flags):
    if flags:
        idb[prop] = json.dumps(flags)
    elif prop in idb.keys():
        del idb[prop]


def _al_range_flags(idb):
    return _al_flag_dict(idb, AL_RANGE_PROP)


def _al_set_range_flag(idb, track_name, custom, always_sync=None):
    """Turn the custom frame range on/off for a layer (and optionally set its
    always-sync flag). custom=None leaves the on/off state alone."""
    flags = _al_range_flags(idb)
    entry = flags.get(track_name)
    if custom is False:
        flags.pop(track_name, None)
    else:
        if entry is None and (custom or always_sync is not None):
            entry = {"sync": False}
        if entry is not None:
            if always_sync is not None:
                entry["sync"] = bool(always_sync)
            flags[track_name] = entry
    _al_write_flag_dict(idb, AL_RANGE_PROP, flags)


def _al_rename_flag(idb, prop, old, new):
    flags = _al_flag_dict(idb, prop)
    if old in flags:
        flags[new] = flags.pop(old)
        _al_write_flag_dict(idb, prop, flags)


def _al_infl_flags(idb):
    raw = idb.get(AL_INFL_PROP)
    if not raw:
        return {}
    try:
        flags = json.loads(raw)
        return flags if isinstance(flags, dict) else {}
    except (TypeError, ValueError):
        return {}


def _al_set_infl_flag(idb, track_name, on):
    flags = _al_infl_flags(idb)
    if on:
        flags[track_name] = True
    else:
        flags.pop(track_name, None)
    if flags:
        idb[AL_INFL_PROP] = json.dumps(flags)
    elif AL_INFL_PROP in idb.keys():
        del idb[AL_INFL_PROP]


def _al_object(name=None):
    """The object whose layer stack we operate on (active by default)."""
    if name:
        ob = bpy.data.objects.get(name)
        if ob is None:
            raise RuntimeError("Object not found: %s" % name)
        return ob
    ob = bpy.context.active_object
    if ob is None:
        raise RuntimeError("No active object — select the rig/object first")
    return ob


def _al_id(ob, data_type='OBJECT'):
    """The ID datablock that owns the layer stack."""
    if data_type == 'SHAPEKEY':
        key = getattr(ob.data, "shape_keys", None) if ob.data else None
        if key is None:
            raise RuntimeError("%s has no shape keys" % ob.name)
        return key
    return ob


def _al_animdata(ob, data_type='OBJECT', create=False):
    idb = _al_id(ob, data_type)
    ad = idb.animation_data
    if ad is None and create:
        ad = idb.animation_data_create()
    return ad


def _al_layers(ad):
    """[(track, strip_or_None, locked_reason_or_None)] bottom -> top."""
    rows = []
    if ad is None:
        return rows
    for track in ad.nla_tracks:
        strips = track.strips
        if len(strips) == 1:
            strip, reason = strips[0], None
        elif len(strips) == 0:
            strip, reason = None, "empty track"
        else:
            strip, reason = None, "%d strips (one per layer)" % len(strips)
        if reason is None and track.lock:
            reason = "track locked"
        rows.append((track, strip, reason))
    return rows


def _al_solo_state(idb):
    """Name of the solo'd track, or None. Stored as a JSON id-prop so solo can
    restore the exact mute set it overrode (written by the solo op)."""
    raw = idb.get(AL_SOLO_PROP)
    if not raw:
        return None
    try:
        return json.loads(raw).get("track") or None
    except (TypeError, ValueError):
        return None


def _al_layer_by_index(ad, index, need_strip=True):
    """The (track, strip, locked_reason) row at *index*, with range checking."""
    rows = _al_layers(ad)
    if not 0 <= index < len(rows):
        raise RuntimeError("No layer %d (stack has %d)" % (index, len(rows)))
    track, strip, reason = rows[index]
    if need_strip and strip is None:
        raise RuntimeError("Layer '%s' is locked: %s" % (track.name, reason))
    return track, strip, reason


def _al_new_action(idb, name):
    """A fresh action with a slot for *idb* — a strip whose action has no slot
    for its owner evaluates as NOTHING, so the slot is not optional."""
    action = bpy.data.actions.new(name)
    action.slots.new(idb.id_type, idb.name)
    return action


def _al_slot_for(action, idb, like=None):
    """The slot a strip should use: same identifier as *like* (a slot from the
    action this one was copied from), else the first suitable one."""
    if like is not None:
        for slot in action.slots:
            if slot.identifier == like.identifier:
                return slot
    for slot in action.slots:
        if slot.target_id_type in (idb.id_type, 'UNSPECIFIED'):
            return slot
    return action.slots[0] if action.slots else None


def _al_exit_tweak(ad):
    if ad and ad.use_tweak_mode:
        ad.use_tweak_mode = False


def _al_touch():
    """Force the animation system to re-evaluate NOW. Several NlaStrip
    properties (influence above all — its RNA update is nulled in Blender
    itself) don't tag the depsgraph when written from a script, so a bridge
    change would only show after the user moves to another frame."""
    sc = bpy.context.scene
    sc.frame_set(sc.frame_current)


def _al_influence_fcurve(strip):
    return strip.fcurves.find("influence")


def _al_write_influence(strip, value, animate):
    """Blender only honours a custom influence when Animated Influence is on
    (off = auto-recomputed to 1.0 at the next evaluation, silently undoing a
    plain property write). So a static influence = animated influence with
    exactly ONE key; `animate` upserts at the current frame instead."""
    strip.use_animated_influence = True
    strip.influence = value
    fc = _al_influence_fcurve(strip)
    if fc is None:                      # paranoia — the toggle creates it
        strip.keyframe_insert("influence")
        fc = _al_influence_fcurve(strip)
    if animate:
        strip.keyframe_insert("influence")
    else:
        while len(fc.keyframe_points):
            fc.keyframe_points.remove(fc.keyframe_points[0])
        fc.keyframe_points.insert(float(bpy.context.scene.frame_current),
                                  value)
        fc.update()


def _al_sync_to_action(strip):
    """Upstream's Sync to Action: the strip spans exactly
    action length x speed x repeat from its start. Writing the action window
    back to the action's own range is what does it — Blender then recomputes
    frame_end from scale and repeat (verified 5.2)."""
    action = strip.action
    if action is None:
        return False
    fs, fe = action.frame_range
    if fe <= fs:
        return False
    strip.action_frame_start = fs
    strip.action_frame_end = fe
    return True


def _al_reset_to_scene_range(strip):
    """Put a strip back on the plain scene range — what 'custom frame range
    off' means. _al_ensure_ranges only ever EXTENDS (it must never shorten a
    layer behind the user's back), so a strip left long or offset by a custom
    range has to be reset explicitly. Playback settings go neutral; reverse
    and extrapolation are the layer's, not the range's, and stay."""
    sc = bpy.context.scene
    strip.use_sync_length = False
    strip.scale = 1.0
    strip.repeat = 1.0
    strip.frame_start_ui = float(sc.frame_start)
    strip.frame_end_ui = float(max(sc.frame_end, sc.frame_start + 1))


def _al_ensure_ranges(ad):
    """Managed layers follow the scene range — unless the user gave the layer
    its own frame range (AL_RANGE_PROP), which is exempt from the auto-extend
    below. That exemption is load-bearing: frame_end_ui on a repeating strip
    REWRITES `repeat`, so without it every op would quietly add cycles to a
    deliberately short strip. Two more standing hazards, both seen live:
    - strips.new defaults use_sync_length ON, so the first tweak-mode exit
      SNAPS the strip down to just the keyed frames — the layer then covers
      a fraction of the timeline. Forced off, always.
    - NLA's own track solo (is_solo) isolates one track and silently kills
      the rest of the stack — never part of the layers workflow (our solo is
      the mute-snapshot kind), so any stray flag is cleared.
    Strip ENDS are extended to the scene end; starts are left alone — moving
    frame_start without shifting action_frame_start would retime the keys."""
    sc = bpy.context.scene
    # The AnimData "NLA evaluation" toggle (speaker icon on the object's
    # channel row — one accidental click) silences the WHOLE stack while the
    # active action keeps playing: every layer except the selected one goes
    # dead. Found live on Marty's rig after two days of flag-chasing.
    if not ad.use_nla:
        ad.use_nla = True
    if ad.nla_tracks:
        # UNCONDITIONAL: the RNA setter clears the track flags AND the
        # AnimData-level solo bit. The pair can go inconsistent (no track
        # solo'd but the AnimData bit stuck ON) — that state silences every
        # track except the tweaked strip: "only the clicked layer plays".
        # Reading track.is_solo alone can NOT detect it.
        ad.nla_tracks[0].is_solo = False
    idb = ad.id_data
    # Someone else's NLA: repair the AnimData-level flags above (harmless, and
    # they keep evaluation sane) but do NOT touch their strips. Merely looking
    # at a foreign stack must never restretch it — the user gets to choose
    # "use as layers" (al_adopt_nla) or "start fresh" first.
    if ad.nla_tracks and not idb.get(AL_MANAGED_PROP):
        return
    custom = _al_range_flags(idb)
    for track, strip, _reason in _al_layers(ad):
        if strip is None or track.lock:
            continue
        strip.use_sync_length = False
        entry = custom.get(track.name)
        if entry is not None:
            # the user owns this strip's span — only keep it synced to the
            # action length if they asked for that (Always Sync)
            if entry.get("sync"):
                _al_sync_to_action(strip)
            continue
        end = float(max(sc.frame_end, sc.frame_start + 1))
        if strip.frame_end < end:
            # _ui variant: the action window grows with the strip (an
            # edge-drag). Raw frame_end distorts the time mapping instead.
            strip.frame_end_ui = end


def _al_activate(ad, index, tweak=True):
    """Select layer *index* (flags + collection-active), then enter tweak mode
    on it so normal keying lands in that layer. Locked rows select without
    tweak. Recipe verified on 5.2: selection alone is refused — the collection
    active track must be set too (BLENDER_NOTES 2026-08-01)."""
    track, strip, reason = _al_layer_by_index(ad, index, need_strip=False)
    _al_exit_tweak(ad)
    _al_ensure_ranges(ad)
    for tr in ad.nla_tracks:
        tr.select = False
        for st in tr.strips:
            st.select = False
    track.select = True
    if strip is not None:
        strip.select = True
    ad.nla_tracks.active = track
    if tweak and strip is not None and strip.action is not None and reason is None:
        _al_deselect_other_nla(ad)
        _al_enter_tweak_full(ad)
    return {"index": index, "tweak": bool(ad.use_tweak_mode),
            "locked": reason}


def _al_tweak_guard(ad):
    """Remember tweak/selection across a structural change. Track NAMES are the
    stable handle — pointers die when tracks are rebuilt."""
    active = None
    for tr in ad.nla_tracks:
        for st in tr.strips:
            if getattr(st, "active", False) or st.select:
                active = tr.name
                break
        if active:
            break
    was_tweak = ad.use_tweak_mode
    _al_exit_tweak(ad)
    return was_tweak, active


def _al_reactivate(ad, was_tweak, track_name):
    if not track_name:
        return
    for i, (tr, st, reason) in enumerate(_al_layers(ad)):
        if tr.name == track_name:
            _al_activate(ad, i, tweak=was_tweak)
            return


def _al_copy_strip(dst_track, src, action, idb):
    """Clone *src* into *dst_track* carrying every per-layer property,
    including the strip's own influence/strip-time animation."""
    strip = dst_track.strips.new(src.name, max(int(src.frame_start), 0), action)
    strip.action_slot = _al_slot_for(action, idb, like=src.action_slot)
    # order matters: sync/animated flags first, then the frame values they gate
    strip.use_sync_length = False  # sync ON = tweak exit snaps the range
    strip.use_animated_influence = src.use_animated_influence
    strip.use_animated_time = src.use_animated_time
    strip.frame_start = src.frame_start
    strip.frame_end = src.frame_end
    for attr in ("blend_type", "extrapolation", "influence", "mute",
                 "repeat", "scale", "use_reverse", "use_auto_blend",
                 "strip_time"):
        setattr(strip, attr, getattr(src, attr))
    try:
        strip.action_frame_start = src.action_frame_start
        strip.action_frame_end = src.action_frame_end
    except (AttributeError, TypeError, ValueError):
        pass  # clamped by the action range — the defaults are already right
    for src_fc in src.fcurves:
        dst_fc = strip.fcurves.find(src_fc.data_path)
        if dst_fc is None:
            continue
        # wipe autogenerated points, then mirror the source curve
        while len(dst_fc.keyframe_points):
            dst_fc.keyframe_points.remove(dst_fc.keyframe_points[0])
        for kp in src_fc.keyframe_points:
            new = dst_fc.keyframe_points.insert(kp.co.x, kp.co.y)
            new.interpolation = kp.interpolation
        dst_fc.update()
    return strip


def _al_rebuild_after(ad, index, prev_track, idb):
    """Recreate layer *index*'s track directly above *prev_track* (the only way
    to reorder NLA tracks from RNA). Returns the new track."""
    track, strip, reason = _al_layer_by_index(ad, index)
    name, mute = track.name, track.mute
    track.name = name + ".__madi_moving"  # free the name for the rebuild
    new_track = ad.nla_tracks.new(prev=prev_track)
    new_track.name = name
    new_track.mute = mute
    _al_copy_strip(new_track, strip, strip.action, idb)
    ad.nla_tracks.remove(track)
    return new_track


_AL_TWEAK_DEBUG = {}


def _al_enter_tweak_full(ad):
    """Enter tweak mode on the already-selected strip. In a windowed session
    go through the NLA operator so ADT_NLA_EVAL_UPPER_TRACKS is set (the full
    stack keeps evaluating while you edit — the flag is not RNA-exposed, see
    BLENDER_NOTES 2026-08-01). Headless falls back to the RNA setter.
    Every attempt is recorded in _AL_TWEAK_DEBUG (surfaced by status) so a
    machine where the operator path fails tells us WHY instead of silently
    degrading."""
    global _AL_TWEAK_DEBUG
    dbg = _AL_TWEAK_DEBUG = {"path": None, "errors": []}
    wm = bpy.context.window_manager
    windows = list(wm.windows) if wm else []
    dbg["windows"] = len(windows)
    for window in windows:
        areas = list(window.screen.areas)
        if not areas:
            dbg["errors"].append("window with no areas")
            continue
        # an already-open NLA editor needs no flip at all
        nla_areas = [a for a in areas if a.type == 'NLA_EDITOR']
        area = nla_areas[0] if nla_areas else max(
            areas, key=lambda a: a.width * a.height)
        old_type = None if nla_areas else area.type
        dbg["area"] = "existing NLA" if nla_areas else "flip %s" % old_type
        try:
            if old_type is not None:
                area.type = 'NLA_EDITOR'
            region = next((r for r in area.regions if r.type == 'WINDOW'), None)
            with bpy.context.temp_override(window=window, area=area,
                                           region=region,
                                           screen=window.screen):
                # The enter operator's poll REFUSES while the scene-level
                # tweak flag is on, and only the operator exit clears that
                # flag — an RNA-only exit (ours) leaves it stuck and every
                # later operator enter fails "context is incorrect". Clear
                # it properly first. (Found live on Marty's session.)
                if bpy.context.scene.is_nla_tweakmode:
                    bpy.ops.nla.tweakmode_exit()
                    dbg["errors"].append("cleared stuck scene tweak flag")
                # isolate_action explicitly False: unspecified operator props
                # reuse LAST-USED values — a leftover True would solo-isolate
                # every layer the user clicks
                bpy.ops.nla.tweakmode_enter(use_upper_stack_evaluation=True,
                                            isolate_action=False)
        except Exception as exc:
            dbg["errors"].append("%s: %s" % (type(exc).__name__, exc))
            break
        finally:
            if old_type is not None:
                try:
                    area.type = old_type
                except Exception as exc:
                    dbg["errors"].append("area restore: %s" % exc)
        if ad.use_tweak_mode:
            dbg["path"] = "operator"
            _al_kick_foreign_tweak(ad)
            return True
        dbg["errors"].append("operator ran but tweak did not engage")
        break
    ad.use_tweak_mode = True
    dbg["path"] = "rna"
    _al_kick_foreign_tweak(ad)
    return ad.use_tweak_mode


def _al_kick_foreign_tweak(keep_ad):
    """The tweak operator enters tweak mode on EVERY AnimData visible in the
    NLA editor, not just ours — kick everyone else straight back out."""
    seen = set()
    for ob in bpy.context.scene.objects:
        for idb in (ob, getattr(ob.data, "shape_keys", None) if ob.data else None):
            if idb is None or idb.name_full in seen:
                continue
            seen.add(idb.name_full)
            ad2 = idb.animation_data
            if ad2 is not None and ad2 != keep_ad and ad2.use_tweak_mode:
                ad2.use_tweak_mode = False


def _al_deselect_other_nla(keep_ad):
    """The tweak operator enters tweak on EVERY AnimData with a selected strip
    — clear strip selection everywhere else so it only grabs ours."""
    seen = set()
    for ob in bpy.context.scene.objects:
        for idb in (ob, getattr(ob.data, "shape_keys", None) if ob.data else None):
            if idb is None or idb.name_full in seen:
                continue
            seen.add(idb.name_full)
            ad = idb.animation_data
            if ad is None or ad == keep_ad:
                continue
            for tr in ad.nla_tracks:
                for st in tr.strips:
                    st.select = False


def al_select_layer(index, data_type='OBJECT', object_name=None):
    """Make layer *index* the one being edited: keys/auto-key land in it while
    the whole stack keeps playing. Locked layers select without tweak (same as
    the add-on dropping out of tweak mode on a locked layer)."""
    ob = _al_object(object_name)
    ad = _al_animdata(ob, data_type)
    if ad is None:
        raise RuntimeError("%s has no animation layers yet" % ob.name)
    track, _strip, _reason = _al_layer_by_index(ad, index, need_strip=False)
    _al_activate(ad, index, tweak=True)
    _al_touch()
    status = anim_layers_status(data_type, object_name)
    status["selected"] = track.name
    return status


def _al_action_fcurves_ro(action, slot=None):
    """Read-only fcurve walk of an arbitrary action datablock (channelbag
    layout on 5.x, legacy flat list before).

    Pass `slot` to read ONLY the channels belonging to that slot. One 5.x
    action can carry slots for several IDs, and a data path is only unique
    within a slot — `key_blocks["Smile"].value` under another mesh's slot is a
    different curve with the same string. Omitted = every slot, which is what
    the anim-layers callers want (they ask about the action as a whole).
    """
    if hasattr(action, "layers"):
        for layer in action.layers:
            for strip in layer.strips:
                if slot is not None:
                    bag = strip.channelbag(slot)
                    bags = (bag,) if bag is not None else ()
                else:
                    bags = strip.channelbags
                for bag in bags:
                    for fc in bag.fcurves:
                        yield fc
    elif hasattr(action, "fcurves"):
        for fc in action.fcurves:
            yield fc


def al_guess_blend(action):
    """The Auto Blend heuristic: an action whose scale / quaternion-w values
    sit near 0 stores DELTAS (made in an additive layer) -> 'ADD'; values near
    1 are absolute poses -> 'REPLACE'. No signal channels -> None (keep)."""
    absolute, total = 0, 0
    for fc in _al_action_fcurves_ro(action):
        path = fc.data_path
        is_scale = path.endswith("scale")
        is_quat_w = path.endswith("rotation_quaternion") and fc.array_index == 0
        if not (is_scale or is_quat_w):
            continue
        for kp in fc.keyframe_points:
            total += 1
            if abs(kp.co.y - 1.0) < abs(kp.co.y):
                absolute += 1
    if total == 0:
        return None
    return 'REPLACE' if absolute / total >= 0.5 else 'ADD'


def al_set_layer_action(index, action_name, auto_blend=False, sync_name=False,
                        data_type='OBJECT', object_name=None):
    """Load an existing action into the layer (the panel's action selector)."""
    ob = _al_object(object_name)
    idb = _al_id(ob, data_type)
    ad = _al_animdata(ob, data_type)
    track, strip, _reason = _al_layer_by_index(ad, index)
    action = bpy.data.actions.get(action_name)
    if action is None:
        raise RuntimeError("Action not found: %s" % action_name)
    was_tweak, active_name = _al_tweak_guard(ad)
    strip.action = action
    strip.action_slot = _al_slot_for(action, idb)
    # a fresh strip span that at least covers the action keeps it audible;
    # degenerate/void ranges confuse every downstream tool
    fs, fe = action.frame_range
    if fe > fs and strip.frame_end <= strip.frame_start + 0.01:
        strip.frame_start, strip.frame_end = fs, fe
    guessed = None
    if auto_blend:
        guessed = al_guess_blend(action)
        if guessed:
            strip.blend_type = guessed
    if sync_name:
        track.name = action.name
        strip.name = action.name
    _al_reactivate(ad, was_tweak, active_name)
    _al_touch()
    status = anim_layers_status(data_type, object_name)
    status["action_set"] = {"layer": track.name, "action": action.name,
                            "auto_blend": guessed}
    return status


def al_sync_layer_names(data_type='OBJECT', object_name=None):
    """Sync Layer/Action Name, the 'the other way' half: a layer whose action
    was renamed elsewhere (Action editor, Blender's own uniquifying) takes the
    action's name back.

    Deliberately narrow, because renaming a track is not free — it has to
    carry three name-keyed flag dicts with it, and a SHARED action would make
    two layers fight over the same name forever. So: only layers whose action
    has exactly one user, and only when the rename actually converges."""
    ob = _al_object(object_name)
    idb = _al_id(ob, data_type)
    ad = _al_animdata(ob, data_type)
    renamed = []
    if ad is None:
        return renamed
    for i, (track, strip, reason) in enumerate(_al_layers(ad)):
        if strip is None or reason is not None or track.lock:
            continue
        action = strip.action
        if action is None or action.name == track.name:
            continue
        # REAL users only: tweak mode holds a reference of its own on the
        # action it is editing, and a fake user counts too. Without both
        # corrections the layer you are editing — the one you just renamed —
        # would look shared and never sync.
        users = action.users
        if ad.use_tweak_mode and ad.action == action:
            users -= 1
        if action.use_fake_user:
            users -= 1
        if users > 1:
            continue          # genuinely shared: renaming would ping-pong
        old = track.name
        track.name = action.name
        if track.name != action.name:
            track.name = old  # Blender uniquified it — leave well alone
            continue
        _al_rename_flag(idb, AL_INFL_PROP, old, track.name)
        _al_rename_flag(idb, AL_RANGE_PROP, old, track.name)
        raw = idb.get(AL_SOLO_PROP)
        if raw:
            try:
                solo = json.loads(raw)
            except (TypeError, ValueError):
                solo = {}
            if solo.get("track") == old:
                solo["track"] = track.name
            restore = solo.get("restore", {})
            if old in restore:
                restore[track.name] = restore.pop(old)
            idb[AL_SOLO_PROP] = json.dumps(solo)
        if strip is not None:
            strip.name = track.name
        renamed.append({"index": i, "from": old, "to": track.name})
    return renamed


def al_list_actions():
    """Every action in the file, for the app's per-layer action dropdown."""
    out = []
    for action in bpy.data.actions:
        fs, fe = action.frame_range
        out.append({"name": action.name, "users": action.users,
                    "frame_start": round(fs, 1), "frame_end": round(fe, 1)})
    return out


def al_set_layer_state(index, mute=None, lock=None, blend_type=None,
                       influence=None, key_influence=False,
                       data_type='OBJECT', object_name=None):
    """Partial state update — only the fields that were passed change.
    Mute works on any row (it's how layers are excluded from view/bakes);
    blend/influence need a healthy strip and respect the lock."""
    ob = _al_object(object_name)
    idb = _al_id(ob, data_type)
    ad = _al_animdata(ob, data_type)
    track, strip, reason = _al_layer_by_index(ad, index, need_strip=False)

    if mute is not None:
        track.mute = bool(mute)
    if lock is not None:
        was_locked = track.lock
        track.lock = bool(lock)
        if track.lock and not was_locked:
            # the add-on drops out of tweak mode when the edited layer locks
            if ad.use_tweak_mode and (getattr(strip, "active", False)
                                      or (strip and strip.select)):
                _al_exit_tweak(ad)
        elif was_locked and not track.lock and track.select:
            _al_activate(ad, index, tweak=True)

    if blend_type is not None or influence is not None or key_influence:
        if strip is None:
            raise RuntimeError("Layer '%s' is locked: %s" % (track.name, reason))
        if track.lock:
            raise RuntimeError("Layer '%s' is locked — unlock it first"
                               % track.name)
        if blend_type is not None:
            if blend_type not in AL_BLEND_TYPES:
                raise RuntimeError("Bad blend type: %r" % blend_type)
            strip.blend_type = blend_type
        if influence is not None:
            # key only when the user has animated influence ON for this layer
            # (or explicitly asked) — a static layer keeps exactly one key,
            # so the value can never drift during playback
            animate = key_influence or _al_infl_flags(idb).get(track.name,
                                                               False)
            _al_write_influence(strip, max(0.0, min(1.0, float(influence))),
                                animate=animate)
        elif key_influence:
            if not strip.use_animated_influence:
                raise RuntimeError("Influence isn't animated on '%s' — turn "
                                   "the key toggle on first" % track.name)
            strip.keyframe_insert("influence")
    _al_ensure_ranges(ad)
    _al_touch()
    return anim_layers_status(data_type, object_name)


def al_set_influence_animated(index, animated, data_type='OBJECT',
                              object_name=None):
    """The (+) key toggle: animated influence on/off. Enabling seeds a key at
    the current frame — an EMPTY influence fcurve evaluates to 0 and would
    silently kill the layer. Disabling COLLAPSES the influence back to a
    static value (one key at the current evaluated influence) instead of
    letting Blender revert it to 1.0."""
    ob = _al_object(object_name)
    idb = _al_id(ob, data_type)
    ad = _al_animdata(ob, data_type)
    track, strip, _reason = _al_layer_by_index(ad, index)
    if track.lock:
        raise RuntimeError("Layer '%s' is locked — unlock it first" % track.name)
    if animated:
        strip.use_animated_influence = True
        fc = strip.fcurves.find("influence")
        if fc is not None and len(fc.keyframe_points) == 0:
            strip.keyframe_insert("influence")
    else:
        # keep the CURRENT value as the static influence (use_animated stays
        # on with exactly one key — that's how a static value survives)
        _al_write_influence(strip, strip.influence, animate=False)
    _al_set_infl_flag(idb, track.name, bool(animated))
    _al_touch()
    return anim_layers_status(data_type, object_name)


def al_key_influence(index, delete=False, data_type='OBJECT',
                     object_name=None):
    """Insert (or delete) an influence key at the current frame."""
    ob = _al_object(object_name)
    ad = _al_animdata(ob, data_type)
    track, strip, _reason = _al_layer_by_index(ad, index)
    if not strip.use_animated_influence:
        raise RuntimeError("Influence isn't animated on '%s'" % track.name)
    if delete:
        try:
            strip.keyframe_delete("influence")
        except RuntimeError:
            raise RuntimeError("No influence key at frame %d"
                               % bpy.context.scene.frame_current)
    else:
        strip.keyframe_insert("influence")
    _al_touch()
    status = anim_layers_status(data_type, object_name)
    status["influence_keys"] = len(strip.fcurves.find("influence").keyframe_points)
    return status


# ------------------------------------------------- keying from the app (0.14)
#
# ⚠ WHAT "KEY WHATEVER I'M HOVERING OVER" CAN AND CANNOT MEAN FROM HERE.
# Marty asked (2026-08-05) for a button in the Anim Layers tab that sets a key
# "on whatever I hover over", with "the key depending on whatever it is set in
# blender for the user". The second half is exactly reproducible. The first half
# is NOT, and it is worth writing down why so nobody re-attempts it:
#   • Blender does not expose the mouse outside an event. `bpy.types.Window`
#     carries x / y / width / height and NO cursor position (checked on 5.2),
#     and the property under the cursor lives on `context.button_prop`, which
#     exists only while a UI event is being handled — never inside the app timer
#     this bridge is drained on.
#   • By the time the button is clicked the cursor is over the APP anyway, so
#     even a cursor readout would be pointing at the wrong window.
# So this does what pressing I in the viewport does, which is the behaviour the
# ask was really describing: `anim.keyframe_insert` uses the active Keying Set
# when there is one and the user's own Preferences ▸ Animation ▸ Default Key
# Channels when there is not, on the selected objects and bones. Removing is
# Alt+I's operator, `anim.keyframe_delete_v3d`. NOTHING HERE DECIDES WHICH
# CHANNELS — that is the whole point of it.
#
# ⚠ Both operators poll against an area and a bpy timer has none
# (`bpy.context.area` is None there — measured, in a real GUI session), so the
# call goes inside a VIEW_3D override. With no 3D viewport open at all the
# refusal is a sentence.
#
# ⚠ No `_al_touch()` afterwards, deliberately. The operators tag the depsgraph
# themselves, and a frame_set on the 461-bone rig is a full scene re-evaluation
# — on a button meant to be pressed as often as the I key that is the one cost
# that would be felt.

def _al_key_channel_info(delete):
    """What the key was made FROM, so the app can say it in a sentence rather
    than claiming credit for a decision Blender made."""
    scene = bpy.context.scene
    keying_set = scene.keying_sets_all.active if scene else None
    channels = []
    if keying_set is None:
        # 4.1+ preference. Absent on older Blender, where the operator falls
        # back to its own built-in set — so an empty list means "Blender's
        # choice", never "nothing".
        prefs = getattr(bpy.context.preferences, "edit", None)
        channels = sorted(getattr(prefs, "key_insert_channels", None) or ())
    ob = bpy.context.active_object
    bones = 0
    if ob is not None and ob.type == 'ARMATURE' and bpy.context.mode == 'POSE':
        bones = sum(1 for pb in ob.pose.bones if bone_is_selected(pb))
    return {"deleted": bool(delete),
            "keying_set": keying_set.bl_label if keying_set else None,
            "channels": channels,
            "objects": len(bpy.context.selected_objects or ()),
            "bones": bones,
            "frame": scene.frame_current if scene else None}


def _al_key_shapekey(ob, delete):
    """The SHAPEKEY stack's answer to the same button.

    Shape keys have no selection and no keying set — the ACTIVE key slot is the
    analogue, exactly as it is for every other tool on this stack
    (`_al_scope_shapekeys`). The Basis is refused: its value drives nothing, so
    a key on it is silently useless rather than wrong."""
    key = getattr(ob.data, "shape_keys", None) if ob.data else None
    if key is None:
        raise RuntimeError("%s has no shape keys" % ob.name)
    block = ob.active_shape_key
    if block is None or ob.active_shape_key_index <= 0:
        raise RuntimeError(
            "Pick a shape key on %s first — the Basis has no value to key."
            % ob.name)
    frame = bpy.context.scene.frame_current
    if delete:
        # ⚠ `keyframe_delete` RETURNS FALSE for "there was nothing there" — it
        # does not raise. Catching only the exception made a second press report
        # a successful removal of a key that never existed.
        removed = False
        try:
            removed = bool(block.keyframe_delete("value"))
        except (RuntimeError, TypeError):
            removed = False
        if not removed:
            raise RuntimeError("No key on '%s' at frame %d" % (block.name, frame))
    elif not block.keyframe_insert("value"):
        raise RuntimeError("Blender would not key '%s'." % block.name)
    return {"deleted": bool(delete), "keying_set": None, "channels": ["Value"],
            "objects": 1, "bones": 0, "shape_key": block.name, "frame": frame}


def al_key_selection(delete=False, data_type='OBJECT', object_name=None):
    """Insert or remove a keyframe the way Blender itself would, at the current
    frame. Keys land in the selected Anim Layer because selecting one puts its
    strip into NLA tweak mode — this adds no opinion of its own about where."""
    if data_type == 'SHAPEKEY':
        info = _al_key_shapekey(_al_object(object_name), delete)
    else:
        window, area, region = _find_view3d()
        if area is None:
            raise RuntimeError(
                "Open a 3D viewport in Blender — its own keying operators need "
                "one to run against.")
        info = _al_key_channel_info(delete)
        if not info["objects"]:
            raise RuntimeError("Nothing is selected in Blender.")
        op = (bpy.ops.anim.keyframe_delete_v3d if delete
              else bpy.ops.anim.keyframe_insert)
        with bpy.context.temp_override(window=window, area=area, region=region):
            result = op()
        if 'CANCELLED' in result:
            raise RuntimeError(
                "Blender %s a keyframe here — check the frame, the selection "
                "and your key channel settings."
                % ("removed no" if delete else "inserted no"))
    status = anim_layers_status(data_type, object_name)
    status["keyed"] = info
    return status


def al_set_frame_range(index=None, custom=None, frame_start=None,
                       frame_end=None, extrapolation=None, reverse=None,
                       repeat=None, scale=None, sync=False, always_sync=None,
                       data_type='OBJECT', object_name=None):
    """Per-layer custom frame range. With it OFF a layer just spans the scene
    range (and is kept there by the auto-repair); ON, the user owns the span
    and these settings shape how the action plays inside it.

    Blender ties the three together — end = start + action length x speed x
    repeat — so writing one moves another. Order matters and is fixed here:
    speed, repeat, start (moves the whole strip, keeping its length), then
    end LAST (which re-derives repeat on a repeating strip, exactly like
    dragging the strip edge). The values that actually resulted come back in
    the status row, never the ones that were asked for.

    Passing any geometry value turns the custom range ON — the auto-extend
    would undo it within one op otherwise."""
    ob = _al_object(object_name)
    idb = _al_id(ob, data_type)
    ad = _al_animdata(ob, data_type)
    if ad is None:
        raise RuntimeError("%s has no animation layers yet" % ob.name)
    index = _al_resolve_index(ad, index)
    track, strip, _reason = _al_layer_by_index(ad, index)
    if track.lock:
        raise RuntimeError("Layer '%s' is locked — unlock it first" % track.name)

    geometry = any(v is not None for v in (frame_start, frame_end, repeat,
                                           scale)) or sync
    if custom is None and geometry:
        custom = True
    if repeat is not None and float(repeat) <= 0.0:
        raise RuntimeError("Repeat must be greater than 0")
    if scale is not None and float(scale) <= 0.0:
        raise RuntimeError("Speed must be greater than 0")
    if (frame_start is not None and frame_end is not None
            and float(frame_end) <= float(frame_start)):
        raise RuntimeError("The end frame must be after the start frame")

    strip.use_sync_length = False
    if extrapolation is not None:
        if extrapolation not in ('NOTHING', 'HOLD', 'HOLD_FORWARD'):
            raise RuntimeError("Bad extrapolation: %r" % extrapolation)
        strip.extrapolation = extrapolation
    if reverse is not None:
        strip.use_reverse = bool(reverse)
    if sync:
        if not _al_sync_to_action(strip):
            raise RuntimeError("Layer '%s' has no keyed action to sync to"
                               % track.name)
    if scale is not None:
        strip.scale = float(scale)
    if repeat is not None:
        strip.repeat = float(repeat)
    if frame_start is not None:
        strip.frame_start_ui = float(frame_start)
    if frame_end is not None:
        strip.frame_end_ui = float(frame_end)

    if custom is not None or always_sync is not None:
        _al_set_range_flag(idb, track.name, custom, always_sync)
    if custom is False:
        _al_reset_to_scene_range(strip)
    _al_touch()
    status = anim_layers_status(data_type, object_name)
    status["frame_range"] = {
        "layer": track.name, "custom": bool(_al_range_flags(idb)
                                            .get(track.name) is not None),
        "always_sync": bool((_al_range_flags(idb).get(track.name) or {})
                            .get("sync")),
        "frame_start": strip.frame_start, "frame_end": strip.frame_end,
        "repeat": strip.repeat, "scale": strip.scale,
        "reversed": strip.use_reverse, "extrapolation": strip.extrapolation}
    return status


def al_influence_keys(index=None, scope='LOCAL', select=None, hide=None,
                      mute=None, lock=None, data_type='OBJECT',
                      object_name=None):
    """Influence key settings: select / hide / mute / lock the INFLUENCE
    f-curve of one layer (scope LOCAL) or of the whole stack (GLOBAL).

    Only the strip's own influence curve is ever touched — the layer's
    animation curves keep their own flags. Select works on the keys (control
    point + both handles, so a graph-editor transform grabs the whole key);
    hide / mute / lock are curve-level, same as the channel toggles."""
    if scope not in ('LOCAL', 'GLOBAL'):
        raise RuntimeError("Scope must be LOCAL or GLOBAL")
    if all(v is None for v in (select, hide, mute, lock)):
        raise RuntimeError("Nothing to change — pick select, hide, mute or "
                           "lock")
    ob = _al_object(object_name)
    ad = _al_animdata(ob, data_type)
    if ad is None:
        raise RuntimeError("%s has no animation layers yet" % ob.name)
    rows = _al_layers(ad)
    if scope == 'LOCAL':
        index = _al_resolve_index(ad, index)
        track, strip, _reason = _al_layer_by_index(ad, index)
        if track.lock:
            raise RuntimeError("Layer '%s' is locked — unlock it first"
                               % track.name)
        targets = [(track, strip)]
    else:
        targets = [(tr, st) for tr, st, reason in rows
                   if st is not None and reason is None and not tr.lock]

    changed, skipped, keys = [], [], 0
    for track, strip in targets:
        fc = _al_influence_fcurve(strip)
        if fc is None:
            skipped.append(track.name)     # influence was never animated
            continue
        if hide is not None:
            fc.hide = bool(hide)
        if mute is not None:
            fc.mute = bool(mute)
        if select is not None:
            for kp in fc.keyframe_points:
                kp.select_control_point = bool(select)
                kp.select_left_handle = bool(select)
                kp.select_right_handle = bool(select)
                keys += 1
        # lock LAST: a locked curve refuses the writes above
        if lock is not None:
            fc.lock = bool(lock)
        changed.append(track.name)
    if not changed:
        raise RuntimeError("No influence curves to change — influence is "
                           "only keyed once a layer has an influence value")
    _al_touch()
    status = anim_layers_status(data_type, object_name)
    status["influence_settings"] = {
        "scope": scope, "layers": changed, "skipped": skipped, "keys": keys,
        "select": select, "hide": hide, "mute": mute, "lock": lock}
    return status


def al_solo(index=None, data_type='OBJECT', object_name=None):
    """Our own solo (the add-on avoids NLA's is_solo on purpose): soloing
    snapshots every track's mute state into an id-prop, mutes the rest, and
    un-soloing restores the exact snapshot. index=None (or the solo'd layer's
    own index) turns solo off."""
    ob = _al_object(object_name)
    idb = _al_id(ob, data_type)
    ad = _al_animdata(ob, data_type)
    if ad is None:
        raise RuntimeError("%s has no animation layers yet" % ob.name)
    rows = _al_layers(ad)
    current = _al_solo_state(idb)

    def restore():
        raw = idb.get(AL_SOLO_PROP)
        if not raw:
            return
        try:
            saved = json.loads(raw).get("restore", {})
        except (TypeError, ValueError):
            saved = {}
        for tr, _s, _r in rows:
            if tr.name in saved:
                tr.mute = saved[tr.name]
        del idb[AL_SOLO_PROP]

    if index is None:
        restore()
    else:
        if not 0 <= index < len(rows):
            raise RuntimeError("No layer %d (stack has %d)" % (index, len(rows)))
        target = rows[index][0]
        if current == target.name:
            restore()
        else:
            if current is not None:
                restore()
                rows = _al_layers(ad)
            snapshot = {tr.name: tr.mute for tr, _s, _r in rows}
            idb[AL_SOLO_PROP] = json.dumps({"track": target.name,
                                            "restore": snapshot})
            for tr, _s, _r in rows:
                tr.mute = tr != target
    _al_ensure_ranges(ad)
    _al_touch()
    return anim_layers_status(data_type, object_name)


def al_add_layer(data_type='OBJECT', object_name=None, name=None,
                 blend_type='COMBINE'):
    """New layer on top (and on first run, adopt the current action as the
    base layer first — the add-on's signature behaviour)."""
    if blend_type not in AL_BLEND_TYPES:
        raise RuntimeError("Bad blend type: %r" % blend_type)
    ob = _al_object(object_name)
    idb = _al_id(ob, data_type)
    ad = _al_animdata(ob, data_type, create=True)
    scene = bpy.context.scene
    _al_exit_tweak(ad)

    adopted = None
    if not ad.nla_tracks and ad.action is not None:
        action = ad.action
        slot = ad.action_slot
        track = ad.nla_tracks.new()
        track.name = action.name
        start = int(action.frame_range[0]) if action.frame_range else 1
        strip = track.strips.new(action.name, max(start, 0), action)
        if slot is not None:
            strip.action_slot = _al_slot_for(action, idb, like=slot)
        strip.blend_type = 'REPLACE'
        strip.extrapolation = 'HOLD'
        strip.use_sync_length = False
        ad.action = None
        adopted = track.name

    count = len(ad.nla_tracks)
    layer_name = name or ("Base Layer" if count == 0 else "Layer %d" % (count + 1))
    action = _al_new_action(idb, layer_name)
    track = ad.nla_tracks.new()
    track.name = layer_name
    strip = track.strips.new(layer_name, scene.frame_start, action)
    strip.action_slot = _al_slot_for(action, idb)
    strip.use_sync_length = False
    strip.frame_end_ui = float(max(scene.frame_end, scene.frame_start + 1))
    strip.blend_type = 'REPLACE' if count == 0 else blend_type
    strip.extrapolation = 'HOLD'
    idb[AL_MANAGED_PROP] = True

    _al_activate(ad, len(ad.nla_tracks) - 1, tweak=True)
    _al_touch()
    status = anim_layers_status(data_type, object_name)
    status["added"] = track.name
    status["adopted_base"] = adopted
    return status


def al_adopt_nla(data_type='OBJECT', object_name=None):
    """Use the object's EXISTING NLA tracks as the layer stack instead of
    starting a fresh one — the answer to 'you already have NLA tracks here'.

    Every healthy strip keeps its exact span: adoption flags them all as
    custom-range, so the scene-range auto-extend can never quietly restretch
    (or re-repeat) someone's hand-built NLA. Tracks that break the
    one-strip-per-layer rule are left exactly as they are and stay visible as
    locked rows."""
    ob = _al_object(object_name)
    idb = _al_id(ob, data_type)
    ad = _al_animdata(ob, data_type)
    if ad is None or not ad.nla_tracks:
        raise RuntimeError("%s has no NLA tracks to adopt" % ob.name)
    adopted, locked = [], []
    for track, strip, reason in _al_layers(ad):
        if strip is None or reason is not None:
            locked.append({"name": track.name, "reason": reason})
            continue
        _al_set_range_flag(idb, track.name, True)
        adopted.append(track.name)
    idb[AL_MANAGED_PROP] = True
    _al_ensure_ranges(ad)
    _al_touch()
    status = anim_layers_status(data_type, object_name)
    status["adopted"] = {"layers": adopted, "locked": locked}
    return status


def al_clear_nla(confirm=False, data_type='OBJECT', object_name=None):
    """Start the layer stack fresh: remove every NLA track on this datablock.
    The actions themselves stay in the file (and the object's active action is
    untouched, so the next New Layer adopts it as the base layer). Destructive
    enough to need an explicit confirmation."""
    if not confirm:
        raise RuntimeError("Clearing the NLA needs an explicit confirmation")
    ob = _al_object(object_name)
    idb = _al_id(ob, data_type)
    ad = _al_animdata(ob, data_type)
    if ad is None or not ad.nla_tracks:
        raise RuntimeError("%s has no NLA tracks" % ob.name)
    removed = [track.name for track in ad.nla_tracks]
    _al_exit_tweak(ad)
    # always index 0, looked up fresh — a held reference into an RNA
    # collection is invalid the moment a sibling is removed
    while len(ad.nla_tracks):
        ad.nla_tracks.remove(ad.nla_tracks[0])
    for prop in (AL_SOLO_PROP, AL_INFL_PROP, AL_RANGE_PROP, AL_MANAGED_PROP):
        if prop in idb.keys():
            del idb[prop]
    _al_touch()
    status = anim_layers_status(data_type, object_name)
    status["cleared"] = {"layers": removed}
    return status


def al_delete_layer(index, data_type='OBJECT', object_name=None):
    ob = _al_object(object_name)
    idb = _al_id(ob, data_type)
    ad = _al_animdata(ob, data_type)
    track, strip, reason = _al_layer_by_index(ad, index, need_strip=False)
    # deleting an empty track is harmless housekeeping; anything else locked
    # (track lock, foreign multi-strip NLA) is not ours to destroy
    if reason is not None and reason != "empty track":
        raise RuntimeError("Layer '%s' is locked: %s" % (track.name, reason))
    was_tweak, active_name = _al_tweak_guard(ad)
    deleted = track.name
    if _al_solo_state(idb) == deleted:
        del idb[AL_SOLO_PROP]
    _al_set_infl_flag(idb, deleted, False)   # a later same-named layer must
    _al_set_range_flag(idb, deleted, False)  # not inherit stale flags
    ad.nla_tracks.remove(track)
    if active_name == deleted:
        # fall back to the layer that took its place (or the new top)
        remaining = len(ad.nla_tracks)
        if remaining:
            _al_activate(ad, min(index, remaining - 1), tweak=was_tweak)
    else:
        _al_reactivate(ad, was_tweak, active_name)
    _al_touch()
    status = anim_layers_status(data_type, object_name)
    status["deleted"] = deleted
    return status


def al_duplicate_layer(index, linked=False, data_type='OBJECT',
                       object_name=None):
    """Copy of the layer directly above the source. linked=True shares the
    action; otherwise the action is copied too."""
    ob = _al_object(object_name)
    idb = _al_id(ob, data_type)
    ad = _al_animdata(ob, data_type)
    track, strip, _reason = _al_layer_by_index(ad, index)
    if strip.action is None:
        raise RuntimeError("Layer '%s' has no action to duplicate" % track.name)
    was_tweak, _n = _al_tweak_guard(ad)
    action = strip.action if linked else strip.action.copy()
    new_track = ad.nla_tracks.new(prev=track)
    new_track.name = track.name  # Blender uniquifies to .001
    new_track.mute = track.mute
    _al_copy_strip(new_track, strip, action, idb)
    if not linked:
        action.name = new_track.name
    entry = _al_range_flags(idb).get(track.name)
    if entry is not None:
        # the copy carries the source's span — without the flag the next op
        # would auto-extend it back to the scene range
        _al_set_range_flag(idb, new_track.name, True, entry.get("sync"))
    # the duplicate sits at index+1; select it like the add-on does
    _al_activate(ad, index + 1, tweak=was_tweak)
    _al_touch()
    status = anim_layers_status(data_type, object_name)
    status["duplicated"] = new_track.name
    return status


def al_rename_layer(index, name, sync_action=True, data_type='OBJECT',
                    object_name=None):
    if not name or not name.strip():
        raise RuntimeError("Layer name can't be empty")
    ob = _al_object(object_name)
    idb = _al_id(ob, data_type)
    ad = _al_animdata(ob, data_type)
    track, strip, reason = _al_layer_by_index(ad, index, need_strip=False)
    if reason == "track locked":
        raise RuntimeError("Layer '%s' is locked — unlock it first" % track.name)
    old = track.name
    track.name = name.strip()
    # every per-layer flag dict is keyed by track name — follow the rename
    _al_rename_flag(idb, AL_INFL_PROP, old, track.name)
    _al_rename_flag(idb, AL_RANGE_PROP, old, track.name)
    raw = idb.get(AL_SOLO_PROP)
    if raw:  # the solo snapshot tracks layers by name — follow the rename
        try:
            solo = json.loads(raw)
        except (TypeError, ValueError):
            solo = {}
        if solo.get("track") == old:
            solo["track"] = track.name
        restore = solo.get("restore", {})
        if old in restore:
            restore[track.name] = restore.pop(old)
        idb[AL_SOLO_PROP] = json.dumps(solo)
    if strip is not None:
        strip.name = track.name
        if sync_action and strip.action is not None:
            strip.action.name = track.name
    status = anim_layers_status(data_type, object_name)
    status["renamed"] = {"from": old, "to": track.name}
    return status


def al_move_layer(index, direction, data_type='OBJECT', object_name=None):
    """Move a layer one slot UP or DOWN. NLA tracks can only be created above
    an existing track, so a move rebuilds whichever of the two swap partners
    lets the swap be expressed as 'insert above'."""
    ob = _al_object(object_name)
    idb = _al_id(ob, data_type)
    ad = _al_animdata(ob, data_type)
    rows = _al_layers(ad)
    if not 0 <= index < len(rows):
        raise RuntimeError("No layer %d (stack has %d)" % (index, len(rows)))
    track, strip, reason = rows[index]
    if direction not in ('UP', 'DOWN'):
        raise RuntimeError("direction must be UP or DOWN")
    if direction == 'UP' and index == len(rows) - 1:
        raise RuntimeError("'%s' is already the top layer" % track.name)
    if direction == 'DOWN' and index == 0:
        raise RuntimeError("'%s' is already the bottom layer" % track.name)

    was_tweak, active_name = _al_tweak_guard(ad)
    if direction == 'UP':
        if reason:
            raise RuntimeError("Layer '%s' is locked: %s" % (track.name, reason))
        neighbour = rows[index + 1][0]
        _al_rebuild_after(ad, index, neighbour, idb)
    else:
        n_track, n_strip, n_reason = rows[index - 1]
        if n_reason is None:
            # swap by lifting the healthy neighbour above us
            _al_rebuild_after(ad, index - 1, track, idb)
        elif reason is None and index >= 2:
            _al_rebuild_after(ad, index, rows[index - 2][0], idb)
        else:
            raise RuntimeError(
                "Can't move '%s' below locked layer '%s'"
                % (track.name, n_track.name))
    _al_reactivate(ad, was_tweak, active_name)
    _al_touch()
    status = anim_layers_status(data_type, object_name)
    status["moved"] = {"layer": track.name, "direction": direction}
    return status


def anim_layers_status(data_type='OBJECT', object_name=None):
    """Everything the app's layer stack UI needs. PURE READ — this is polled,
    it must never touch the scene. Errors come back as a field, not a raise."""
    try:
        ob = _al_object(object_name)
    except RuntimeError as exc:
        return {"error": str(exc)}
    out = {"error": None, "object": ob.name, "object_type": ob.type,
           "data_type": data_type,
           "has_shapekeys": bool(getattr(ob.data, "shape_keys", None)
                                 if ob.data else None),
           "mode": bpy.context.mode,
           "frame": bpy.context.scene.frame_current,
           "frame_start": bpy.context.scene.frame_start,
           "frame_end": bpy.context.scene.frame_end}
    try:
        idb = _al_id(ob, data_type)
    except RuntimeError as exc:
        out["error"] = str(exc)
        return out
    ad = idb.animation_data
    solo = _al_solo_state(idb) if ad else None
    out.update({
        "has_animdata": ad is not None,
        "nla_evaluation": bool(ad is None or ad.use_nla),
        "in_tweak": bool(ad and ad.use_tweak_mode),
        "active_action": ad.action.name if ad and ad.action else None,
        "managed": bool(idb.get(AL_MANAGED_PROP)) or not (ad and ad.nla_tracks),
        # tracks we never made: the app offers "use as layers" vs "start fresh"
        "foreign_nla": bool(ad and ad.nla_tracks
                            and not idb.get(AL_MANAGED_PROP)),
        "solo": solo,
        "tweak_debug": dict(_AL_TWEAK_DEBUG),
        "layers": [],
        "active_index": None,
    })
    infl_flags = _al_infl_flags(idb)
    range_flags = _al_range_flags(idb)
    for i, (track, strip, reason) in enumerate(_al_layers(ad)):
        row = {"index": i, "name": track.name, "mute": track.mute,
               "lock": track.lock, "solo": track.name == solo,
               "nla_solo": track.is_solo,
               "locked_reason": reason}
        if strip is not None:
            infl_fc = strip.fcurves.find("influence")
            infl_keys = len(infl_fc.keyframe_points) if infl_fc else 0
            # the user-facing "animated influence" state: our explicit flag,
            # else (legacy stacks) more-than-one key. use_animated_influence
            # alone is meaningless — it is True for static values too.
            if track.name in infl_flags:
                infl_anim = bool(infl_flags[track.name])
            else:
                infl_anim = strip.use_animated_influence and infl_keys > 1
            keys = sum(len(fc.keyframe_points)
                       for fc in _al_action_fcurves_ro(strip.action)) \
                if strip.action else 0
            row.update({
                "action": strip.action.name if strip.action else None,
                "action_users": strip.action.users if strip.action else 0,
                "keys": keys,
                "blend_type": strip.blend_type,
                "influence": round(strip.influence, 4),
                "animated_influence": infl_anim,
                "influence_keys": infl_keys,
                "influence_keyed": infl_anim and infl_keys > 0,
                "frame_start": strip.frame_start,
                "frame_end": strip.frame_end,
                "repeat": strip.repeat,
                "scale": strip.scale,
                "reversed": strip.use_reverse,
                "extrapolation": strip.extrapolation,
                "strip_mute": strip.mute,
                # influence-curve channel state, so the app's influence-key
                # toggles show what IS rather than being write-only
                "influence_hide": bool(infl_fc and infl_fc.hide),
                "influence_mute": bool(infl_fc and infl_fc.mute),
                "influence_lock": bool(infl_fc and infl_fc.lock),
                "influence_selected": bool(
                    infl_fc and infl_keys
                    and all(kp.select_control_point
                            for kp in infl_fc.keyframe_points)),
                "custom_range": track.name in range_flags,
                "always_sync": bool(range_flags.get(track.name, {})
                                    .get("sync")),
                "action_length": round(strip.action_frame_end
                                       - strip.action_frame_start, 4),
            })
            if getattr(strip, "active", False) or strip.select:
                if out["active_index"] is None or getattr(strip, "active", False):
                    out["active_index"] = i
        out["layers"].append(row)
    return out


# --- bake / merge -----------------------------------------------------------
# The "AL bake" engine: sample the NLA-EVALUATED value of every channel the
# source layers animate, straight off the animated properties after
# frame_set. Exact by construction (COMBINE quaternions, influence curves,
# repeat/speed all come out of Blender's own evaluator) and it deliberately
# does NOT capture constraints/drivers — those stay live on top of the baked
# layer, so nothing double-applies. Visual (constraint) baking is the
# NLA-native path's job.

AL_BAKE_RESULT_NAME = "Baked Layer"


def _al_active_index(ad):
    """Row index of the selected/active layer, else the top healthy row."""
    rows = _al_layers(ad)
    fallback = None
    for i, (track, strip, reason) in enumerate(rows):
        if strip is not None and strip.action is not None and reason is None:
            fallback = i
        if strip is not None and (getattr(strip, "active", False)
                                  or strip.select):
            return i
    return fallback


def _al_bake_targets(rows, indices, selected_only, ob, data_type='OBJECT'):
    """Union of animated channels across those layers' actions, insertion-
    ordered [(data_path, array_index)] so baked fcurves stay grouped by bone.
    selected_only drops bone channels of unselected bones (on a shape-key
    stack: every key but the active one); non-bone channels (object/custom)
    always pass."""
    sel = shapes = None
    if selected_only and data_type == 'SHAPEKEY':
        active = ob.active_shape_key
        shapes = {active.name} if active is not None else set()
    elif selected_only and ob.type == 'ARMATURE' and ob.pose:
        sel = {pb.name for pb in ob.pose.bones if bone_is_selected(pb)}
    targets, seen = [], set()
    for i in indices:
        strip = rows[i][1]
        if strip is None or strip.action is None:
            continue
        for fc in _al_action_fcurves_ro(strip.action):
            if sel is not None:
                bone = _bone_of_path(fc.data_path)
                if bone is not None and bone not in sel:
                    continue
            if shapes is not None:
                name = _al_shapekey_of_path(fc.data_path)
                if name is not None and name not in shapes:
                    continue
            key = (fc.data_path, fc.array_index)
            if key not in seen:
                seen.add(key)
                targets.append(key)
    return targets


def _al_sample_channels(idb, targets, frames):
    """{(path, index): {frame: value}} for every target channel at every
    frame (frames may be subframe floats — smart-bake key times can be).
    Channels that no longer resolve (deleted bone/prop) drop out instead of
    failing the whole bake."""
    scene = bpy.context.scene
    saved = scene.frame_current
    by_path = {}
    for path, index in targets:
        by_path.setdefault(path, []).append(index)
    samples = {t: {} for t in targets}
    dropped = set()
    try:
        for f in frames:
            whole = int(math.floor(f))
            scene.frame_set(whole, subframe=float(f) - whole)
            for path, indices in by_path.items():
                if path in dropped:
                    continue
                try:
                    val = idb.path_resolve(path)
                except (ValueError, KeyError):
                    dropped.add(path)
                    continue
                for index in indices:
                    try:
                        v = float(val[index]) if hasattr(val, "__len__") \
                            else float(val)
                    except (TypeError, IndexError):
                        v = float(val)
                    samples[(path, index)][f] = v
    finally:
        scene.frame_set(saved)
    for path in dropped:
        for index in by_path[path]:
            samples.pop((path, index), None)
    return samples


def _al_strip_scene_times(strip, action_frame):
    """Every scene time where *action_frame* plays, honouring the strip's
    scale, repeat and reverse (mirrors Blender's strip time mapping: each
    cycle spans action-length x scale, cycles run until the strip end)."""
    afs, afe = strip.action_frame_start, strip.action_frame_end
    if not (afs - 1e-4 <= action_frame <= afe + 1e-4):
        return []          # outside the action window — that key never plays
    length = max(afe - afs, 1e-6)
    scale = strip.scale or 1.0
    out = []
    k = 0
    while k < 10000:
        base = strip.frame_start + k * length * scale
        if base > strip.frame_end + 1e-4:
            break
        if strip.use_reverse:
            t = base + (afe - action_frame) * scale
        else:
            t = base + (action_frame - afs) * scale
        if strip.frame_start - 1e-4 <= t <= strip.frame_end + 1e-4:
            out.append(min(max(t, strip.frame_start), strip.frame_end))
        k += 1
    return out


def _al_tile_cycles(entries, cyc, strip):
    """A Cycles F-modifier repeats the keyed span; smart bake expands that
    into real key TIMES across the strip's action window (the modifier is
    dropped from the result — its motion lives in the keys). entries =
    [(action_frame, meta)]; REPEAT/REPEAT_OFFSET tile straight, MIRROR
    alternates flipped cycles."""
    first = min(e[0] for e in entries)
    last = max(e[0] for e in entries)
    span = last - first
    if span <= 1e-6:
        return entries
    out = {round(af, 4): m for af, m in entries}

    def place(af, m, t):
        out.setdefault(round(t, 4), m)

    if cyc.mode_after != 'NONE':
        limit = min(int(cyc.cycles_after) or 100000, 100000)
        hi = strip.action_frame_end + span
        for k in range(1, limit + 1):
            start = last + (k - 1) * span
            if start > hi:
                break
            for af, m in entries:
                if cyc.mode_after == 'MIRROR' and k % 2 == 1:
                    place(af, m, start + (last - af))
                else:
                    place(af, m, start + (af - first))
    if cyc.mode_before != 'NONE':
        limit = min(int(cyc.cycles_before) or 100000, 100000)
        lo = strip.action_frame_start - span
        for k in range(1, limit + 1):
            end = first - (k - 1) * span
            if end < lo:
                break
            for af, m in entries:
                if cyc.mode_before == 'MIRROR' and k % 2 == 1:
                    place(af, m, end - (af - first))
                else:
                    place(af, m, end - span + (af - first))
    return sorted(out.items())


def _al_scene_to_action_frame(strip, scene_frame):
    """Inverse of _al_strip_scene_times: the ACTION frame playing at
    *scene_frame* (scale / repeat / reverse aware). Keys written by the layer
    tools MUST go in at this frame — writing raw scene frames silently
    misplaces every key on a strip that is offset, scaled or repeated.
    Outside the strip the value clamps to the nearest end."""
    afs, afe = strip.action_frame_start, strip.action_frame_end
    length = max(afe - afs, 1e-6)
    scale = strip.scale or 1.0
    total = (strip.frame_end - strip.frame_start) / scale
    pos = (float(scene_frame) - strip.frame_start) / scale
    pos = max(0.0, min(pos, total))
    offset = math.fmod(pos, length)
    if offset < 0.0:
        offset += length
    # On an exact cycle boundary fmod returns 0 — correct when another cycle
    # really starts there, WRONG at the strip's last frame, where it would
    # wrap the end of the animation back onto its first key (that collision
    # silently ate the final marker/key until this was fixed).
    if offset < 1e-9 and pos > 1e-9 and pos + 1e-9 >= total:
        offset = length
    return (afe - offset) if strip.use_reverse else (afs + offset)


def _al_smart_keys(rows, bake_set, targets, scene, merge_modifiers=True):
    """Smart bake: per-channel key times = the union of the source layers'
    key frames mapped through their strips into scene time. Key METADATA
    (interpolation/easing/handle types — not handle positions) rides along;
    when two layers key the same channel at the same time the higher layer
    wins. A Cycles modifier being merged in gets its cycles expanded into
    real keys first. Channels with no keys in range fall back to the range
    ends."""
    tset = set(targets)
    frame_map = {t: set() for t in targets}
    meta = {}
    for i in bake_set:                       # bottom -> top: higher overwrites
        strip = rows[i][1]
        for fc in _al_action_fcurves_ro(strip.action):
            key = (fc.data_path, fc.array_index)
            if key not in tset:
                continue
            entries = [(kp.co.x, (kp.interpolation, kp.easing,
                                  kp.handle_left_type, kp.handle_right_type))
                       for kp in fc.keyframe_points]
            if not entries:
                continue
            if merge_modifiers:
                cyc = next((m for m in fc.modifiers
                            if m.type == 'CYCLES' and not m.mute), None)
                if cyc is not None:
                    entries = _al_tile_cycles(entries, cyc, strip)
            for af, kmeta in entries:
                for t in _al_strip_scene_times(strip, af):
                    if not (scene.frame_start - 1e-4 <= t
                            <= scene.frame_end + 1e-4):
                        continue
                    t = round(t, 4)
                    frame_map[key].add(t)
                    meta[(key[0], key[1], t)] = kmeta
    # every channel also keys the range ends: a strip cut mid-cycle by the
    # timeline (or one that only covers part of it) would otherwise leave the
    # last segment to constant extrapolation while the stack keeps moving
    fs_f, fe_f = float(scene.frame_start), float(scene.frame_end)
    for frames in frame_map.values():
        frames.add(fs_f)
        frames.add(fe_f)
    return {k: sorted(v) for k, v in frame_map.items()}, meta


def _al_action_fcurve_container(action, idb):
    """Writable fcurves collection of a standalone action (channelbag layout
    on 5.x, flat list before) — the bake writes through this."""
    if hasattr(action, "fcurves"):
        return action.fcurves
    slot = _al_slot_for(action, idb)
    if slot is None:
        slot = action.slots.new(idb.id_type, idb.name)
    layer = action.layers[0] if len(action.layers) else action.layers.new("Layer")
    strip = None
    for s in layer.strips:
        if s.type == 'KEYFRAME':
            strip = s
            break
    if strip is None:
        strip = layer.strips.new(type='KEYFRAME')
    return strip.channelbag(slot, ensure=True).fcurves


def _al_write_baked(action, idb, samples, targets, frame_map, key_meta):
    """Samples -> keys. Non-smart keys are plain LINEAR; smart keys restore
    the source key's interpolation/easing/handle TYPES (positions recomputed
    by fc.update()). Constant channels collapse to one key (same policy as
    the library's anim bake — a 461-bone rig would otherwise key every
    static channel every frame). Returns (channels, keys) written."""
    fcurves = _al_action_fcurve_container(action, idb)
    channels = keys = 0
    for key in targets:
        vals = samples.get(key)
        if not vals:
            continue
        pts = [(f, vals[f]) for f in frame_map[key] if f in vals]
        if not pts:
            continue
        if all(abs(v - pts[0][1]) < 1e-12 for _f, v in pts):
            pts = pts[:1]
        path, index = key
        group = _bone_of_path(path)
        try:
            fc = fcurves.new(path, index=index, action_group=group or "")
        except (RuntimeError, TypeError):
            try:
                fc = fcurves.new(path, index=index)
            except RuntimeError:
                continue  # e.g. a path the action layout refuses
        fc.keyframe_points.add(len(pts))
        for kp, (f, v) in zip(fc.keyframe_points, pts):
            kp.co = (f, v)
            m = key_meta.get((path, index, f))
            if m:
                kp.interpolation, kp.easing = m[0], m[1]
                kp.handle_left_type, kp.handle_right_type = m[2], m[3]
            else:
                kp.interpolation = 'LINEAR'
        fc.update()
        channels += 1
        keys += len(pts)
    return channels, keys


# --- layer tools ------------------------------------------------------------
# Every tool below shares two scoping controls, exactly like the panel: the
# loc/rot/scale x W/X/Y/Z channel filter, and "affect only selected bones".
# Both are plain arguments so the app can pass whatever its filter row shows.

AL_CHANNEL_TYPES = ('LOCATION', 'ROTATION', 'SCALE')
_AL_ROT_PROPS = ("rotation_quaternion", "rotation_euler", "rotation_axis_angle")
_AL_QUAT_PROPS = ("rotation_quaternion", "rotation_axis_angle")


def _al_channel_of(data_path):
    """'LOCATION' / 'ROTATION' / 'SCALE' for a transform channel, else None
    (custom properties, shape-key values, anything else)."""
    if data_path.endswith("location"):
        return 'LOCATION'
    if data_path.endswith("scale"):
        return 'SCALE'
    if any(data_path.endswith(p) for p in _AL_ROT_PROPS):
        return 'ROTATION'
    return None


def _al_axis_of(data_path, array_index):
    """'W'/'X'/'Y'/'Z' for a transform channel index. Quaternion and
    axis-angle carry W at index 0; every other channel starts at X."""
    if any(data_path.endswith(p) for p in _AL_QUAT_PROPS):
        names = ("W", "X", "Y", "Z")
    else:
        names = ("X", "Y", "Z")
    return names[array_index] if 0 <= array_index < len(names) else None


def _al_filter_ok(data_path, array_index, channels=None, axes=None):
    """The shared filter. Both None = no filtering at all (custom props and
    other non-transform channels pass). As soon as either is set the tool is
    explicitly about transforms, so non-transform channels drop out."""
    if channels is None and axes is None:
        return True
    chan = _al_channel_of(data_path)
    if chan is None:
        return False
    if channels is not None and chan not in channels:
        return False
    if axes is not None:
        axis = _al_axis_of(data_path, array_index)
        if axis is None or axis not in axes:
            return False
    return True


def _al_resolve_index(ad, index):
    """An explicit layer index, else the one currently being edited."""
    if index is not None:
        return index
    found = _al_active_index(ad)
    if found is None:
        raise RuntimeError("No layer selected — click a layer first")
    return found


def _al_layer_fcurve(action, idb, data_path, array_index, create=True):
    """Find-or-create an fcurve inside a layer's action."""
    fcurves = _al_action_fcurve_container(action, idb)
    fc = fcurves.find(data_path, index=array_index)
    if fc is None and create:
        group = _bone_of_path(data_path)
        try:
            fc = fcurves.new(data_path, index=array_index,
                             action_group=group or "")
        except (RuntimeError, TypeError):
            fc = fcurves.new(data_path, index=array_index)
    return fc


def _al_scope_bones(ob, strip, selected_only, channels=None, axes=None):
    """Which bones a layer tool acts on: the selected ones, else every bone
    the layer already animates. None = not an armature (object channels)."""
    if ob.type != 'ARMATURE' or not ob.pose:
        return None
    if selected_only:
        return [pb.name for pb in ob.pose.bones if bone_is_selected(pb)]
    names = []
    if strip is not None and strip.action is not None:
        for fc in _al_action_fcurves_ro(strip.action):
            if not _al_filter_ok(fc.data_path, fc.array_index, channels, axes):
                continue
            bone = _bone_of_path(fc.data_path)
            if bone and bone not in names:
                names.append(bone)
    return names


_AL_SHAPEKEY_PATH = re.compile(r'^key_blocks\["((?:[^"\\]|\\.)*)"\]')


def _al_shapekey_of_path(data_path):
    m = _AL_SHAPEKEY_PATH.match(data_path)
    return m.group(1).replace('\\"', '"') if m else None


def _al_scope_shapekeys(ob, strip, selected_only):
    """Which shape keys a tool acts on. Shape keys have no multi-selection —
    the ACTIVE key slot is the analogue of the bone selection, so
    'only selected' means 'only the active shape key'. Otherwise: every key
    the layer already animates. None = no scoping possible (no shape keys)."""
    key = getattr(ob.data, "shape_keys", None) if ob.data else None
    if key is None:
        return None
    if selected_only:
        active = ob.active_shape_key
        return [active.name] if active is not None else []
    names = []
    if strip is not None and strip.action is not None:
        for fc in _al_action_fcurves_ro(strip.action):
            name = _al_shapekey_of_path(fc.data_path)
            if name and name not in names:
                names.append(name)
    return names


def _al_rest_value(owner, data_path, array_index):
    """Rest value of a transform channel, straight from the RNA default —
    exact for every rotation mode (axis-angle's rest axis is not all-zero)."""
    prop = data_path.rsplit(".", 1)[-1]
    try:
        rna = owner.bl_rna.properties[prop]
    except KeyError:
        return None
    if getattr(rna, "is_array", False):
        try:
            return float(rna.default_array[array_index])
        except (IndexError, TypeError):
            return None
    return float(rna.default)


def _al_reset_shape_channels(ob, names):
    """[(data_path, array_index, rest_value)] for shape keys — rest is the
    RNA default of ShapeKey.value (0.0), i.e. the key contributing nothing.
    The transform filter never applies here: a shape key is not a channel of
    Loc/Rot/Scale, so a filtered call has nothing to say about it."""
    key = getattr(ob.data, "shape_keys", None) if ob.data else None
    out = []
    if key is None:
        return out
    for name in names or ():
        kb = key.key_blocks.get(name)
        if kb is None or kb == key.reference_key:
            continue          # the Basis has no value to reset
        path = 'key_blocks["%s"].value' % name
        rest = _al_rest_value(kb, path, 0)
        if rest is not None:
            out.append((path, 0, rest))
    return out


def _al_reset_channels(ob, bone_names, channels, axes):
    """[(data_path, array_index, rest_value)] — the full transform set of
    every scope bone (object-level channels when the target isn't an
    armature), honouring each owner's rotation mode and the filter."""
    if bone_names is None:
        owners = [("", ob)]
    else:
        owners = []
        for name in bone_names:
            pb = ob.pose.bones.get(name)
            if pb is not None:
                owners.append(('pose.bones["%s"].' % name, pb))
    out = []
    for prefix, owner in owners:
        rot = {"QUATERNION": ("rotation_quaternion", 4),
               "AXIS_ANGLE": ("rotation_axis_angle", 4)}.get(
                   owner.rotation_mode, ("rotation_euler", 3))
        for prop, count in (("location", 3), rot, ("scale", 3)):
            path = prefix + prop
            for i in range(count):
                if not _al_filter_ok(path, i, channels, axes):
                    continue
                rest = _al_rest_value(owner, path, i)
                if rest is not None:
                    out.append((path, i, rest))
    return out


def al_select_bones_in_layer(index=None, extend=False, channels=None,
                             axes=None, data_type='OBJECT', object_name=None):
    """Select the bones a layer animates. Filter-scoped: with a channel or
    axis filter set, only bones animated in those channels are picked."""
    ob = _al_object(object_name)
    if ob.type != 'ARMATURE' or not ob.pose:
        raise RuntimeError("%s is not an armature" % ob.name)
    ad = _al_animdata(ob, data_type)
    if ad is None:
        raise RuntimeError("%s has no animation layers yet" % ob.name)
    index = _al_resolve_index(ad, index)
    track, strip, _reason = _al_layer_by_index(ad, index)
    names = []
    if strip.action is not None:
        for fc in _al_action_fcurves_ro(strip.action):
            if not _al_filter_ok(fc.data_path, fc.array_index, channels, axes):
                continue
            bone = _bone_of_path(fc.data_path)
            if bone and bone not in names:
                names.append(bone)
    if not extend:
        for pb in ob.pose.bones:
            bone_set_selected(pb, False)
    selected = []
    for name in names:
        pb = ob.pose.bones.get(name)
        if pb is not None:
            bone_set_selected(pb, True)
            selected.append(name)
    status = anim_layers_status(data_type, object_name)
    status["selected_bones"] = {
        "layer": track.name, "bones": selected,
        "missing": [n for n in names if n not in selected]}
    return status


def al_reset_layer(index=None, selected_only=True, channels=None, axes=None,
                   data_type='OBJECT', object_name=None):
    """Reset Key Layer: key REST values into THIS layer only, at the current
    frame. The layer stops contributing for those bones/channels while every
    other layer keeps its animation untouched."""
    ob = _al_object(object_name)
    idb = _al_id(ob, data_type)
    ad = _al_animdata(ob, data_type)
    if ad is None:
        raise RuntimeError("%s has no animation layers yet" % ob.name)
    index = _al_resolve_index(ad, index)
    track, strip, _reason = _al_layer_by_index(ad, index)
    if track.lock:
        raise RuntimeError("Layer '%s' is locked — unlock it first" % track.name)

    if data_type == 'SHAPEKEY':
        # a shape-key stack resets its KEYS to 0, never the mesh object's
        # transforms (those channels don't even exist on a Key datablock)
        shapes = _al_scope_shapekeys(ob, strip, selected_only)
        if not shapes:
            raise RuntimeError(
                "No shape keys in scope — make one active, or turn 'only the "
                "active shape key' off to reset every key this layer animates")
        bones = shapes
        targets = _al_reset_shape_channels(ob, shapes)
        if not targets:
            raise RuntimeError("Nothing to reset — the scope is only the "
                               "Basis key")
    else:
        bones = _al_scope_bones(ob, strip, selected_only, channels, axes)
        if bones is not None and not bones:
            raise RuntimeError("No bones in scope — select some bones, or "
                               "turn 'only selected bones' off")
        targets = _al_reset_channels(ob, bones, channels, axes)
        if not targets:
            raise RuntimeError("Nothing to reset with that filter")

    action = strip.action
    if action is None:
        action = _al_new_action(idb, track.name)
        strip.action = action
        strip.action_slot = _al_slot_for(action, idb)
    # keys go in at the STRIP's action time, not the scene frame — a strip
    # that starts later (or is scaled) maps them differently
    frame = _al_scene_to_action_frame(strip, bpy.context.scene.frame_current)
    for path, i, rest in targets:
        fc = _al_layer_fcurve(action, idb, path, i)
        if fc is None:
            continue
        fc.keyframe_points.insert(frame, rest)
        fc.update()
    _al_touch()
    status = anim_layers_status(data_type, object_name)
    status["reset"] = {"layer": track.name, "channels": len(targets),
                       "bones": len(bones) if bones is not None else 0,
                       "frame": bpy.context.scene.frame_current}
    return status


def _al_tool_curves(ob, strip, selected_only, channels, axes,
                    data_type='OBJECT'):
    """The fcurves of a layer that a tool should act on — scoped by the bone
    selection (or, on a shape-key stack, the active shape key) and the
    channel/axis filter."""
    if strip.action is None:
        return []
    bones = shapes = None
    if selected_only and data_type == 'SHAPEKEY':
        shapes = set(_al_scope_shapekeys(ob, strip, True) or ())
        if not shapes:
            raise RuntimeError("No active shape key — pick one in the mesh's "
                               "Shape Keys list, or turn 'only the active "
                               "shape key' off")
    elif selected_only and ob.type == 'ARMATURE' and ob.pose:
        bones = {pb.name for pb in ob.pose.bones if bone_is_selected(pb)}
        if not bones:
            raise RuntimeError("No bones selected — select some, or turn "
                               "'only selected bones' off")
    out = []
    for fc in _al_action_fcurves_ro(strip.action):
        if not _al_filter_ok(fc.data_path, fc.array_index, channels, axes):
            continue
        if bones is not None:
            bone = _bone_of_path(fc.data_path)
            if bone is None or bone not in bones:
                continue
        if shapes is not None:
            name = _al_shapekey_of_path(fc.data_path)
            if name is None or name not in shapes:
                continue
        out.append(fc)
    return out


def al_cyclic_fcurves(index=None, enable=True, selected_only=True,
                      channels=None, axes=None, data_type='OBJECT',
                      object_name=None):
    """Add (or remove) a Cycles F-modifier on the scoped curves of a layer,
    so a keyed cycle repeats forever. Appended BELOW any existing modifiers
    — a Noise on top of a cycle must keep evaluating last."""
    ob = _al_object(object_name)
    ad = _al_animdata(ob, data_type)
    if ad is None:
        raise RuntimeError("%s has no animation layers yet" % ob.name)
    index = _al_resolve_index(ad, index)
    track, strip, _reason = _al_layer_by_index(ad, index)
    if track.lock:
        raise RuntimeError("Layer '%s' is locked — unlock it first" % track.name)
    curves = _al_tool_curves(ob, strip, selected_only, channels, axes,
                             data_type)
    if not curves:
        raise RuntimeError("No curves in scope — nothing to make cyclic")
    changed = 0
    for fc in curves:
        existing = [m for m in fc.modifiers if m.type == 'CYCLES']
        if enable:
            if existing:
                continue
            if len(fc.keyframe_points) < 2:
                continue          # a single key has no cycle to repeat
            fc.modifiers.new(type='CYCLES')
            changed += 1
        else:
            for m in existing:
                fc.modifiers.remove(m)
                changed += 1
        fc.update()
    _al_touch()
    status = anim_layers_status(data_type, object_name)
    status["cyclic"] = {"layer": track.name, "enabled": bool(enable),
                        "curves": changed, "scoped": len(curves)}
    return status


def _al_neighbour_keys(fc, frame):
    """(previous, next) keyframe points around *frame*. A key sitting
    exactly on the frame counts as the previous one, so pressing the tool
    repeatedly keeps working from the same pair."""
    prev_kp = next_kp = None
    for kp in fc.keyframe_points:
        if kp.co.x <= frame + 1e-6:
            if prev_kp is None or kp.co.x > prev_kp.co.x:
                prev_kp = kp
        if kp.co.x > frame + 1e-6:
            if next_kp is None or kp.co.x < next_kp.co.x:
                next_kp = kp
    return prev_kp, next_kp


def al_inbetween(amount, index=None, selected_only=True, channels=None,
                 axes=None, data_type='OBJECT', object_name=None):
    """Inbetweener: key each scoped curve at the current frame a fraction
    *amount* of the way from its previous key toward its next one. Values
    outside 0..1 overshoot on purpose (that's the point of the tool)."""
    ob = _al_object(object_name)
    idb = _al_id(ob, data_type)
    ad = _al_animdata(ob, data_type)
    if ad is None:
        raise RuntimeError("%s has no animation layers yet" % ob.name)
    index = _al_resolve_index(ad, index)
    track, strip, _reason = _al_layer_by_index(ad, index)
    if track.lock:
        raise RuntimeError("Layer '%s' is locked — unlock it first" % track.name)
    curves = _al_tool_curves(ob, strip, selected_only, channels, axes,
                             data_type)
    if not curves:
        raise RuntimeError("No curves in scope for the inbetweener")
    frame = _al_scene_to_action_frame(strip,
                                      bpy.context.scene.frame_current)
    t = float(amount)
    written = skipped = 0
    for fc in curves:
        prev_kp, next_kp = _al_neighbour_keys(fc, frame)
        if prev_kp is None or next_kp is None:
            skipped += 1          # needs a key on BOTH sides
            continue
        # read everything off the neighbours FIRST: inserting reallocates the
        # keyframe array, and the old references then read freed memory
        prev_v, next_v = prev_kp.co.y, next_kp.co.y
        interp = prev_kp.interpolation
        kp = fc.keyframe_points.insert(frame, prev_v + (next_v - prev_v) * t)
        kp.interpolation = interp
        fc.update()
        written += 1
    if not written:
        raise RuntimeError("The inbetweener needs a key on BOTH sides of "
                           "the current frame — none of the scoped curves "
                           "have that")
    _al_touch()
    status = anim_layers_status(data_type, object_name)
    status["inbetween"] = {"layer": track.name, "amount": t,
                           "curves": written, "skipped": skipped,
                           "frame": bpy.context.scene.frame_current}
    return status


def _al_snapshot_fcurve(fc):
    """Everything about one fcurve as plain Python. Snapshotting FIRST is
    load-bearing: removing or inserting invalidates every other RNA
    reference into the same collection (freed-memory reads, not errors)."""
    return {
        "data_path": fc.data_path,
        "array_index": fc.array_index,
        "extrapolation": fc.extrapolation,
        "modifiers": _serialize_fmodifiers(fc),
        "keys": [(kp.co.x, kp.co.y, kp.interpolation, kp.easing,
                  kp.handle_left_type, kp.handle_right_type,
                  (kp.handle_left.x, kp.handle_left.y),
                  (kp.handle_right.x, kp.handle_right.y))
                 for kp in fc.keyframe_points],
    }


def _al_write_fcurve(action, idb, snap):
    """Re-create a snapshotted fcurve inside *action*."""
    dst = _al_layer_fcurve(action, idb, snap["data_path"],
                           snap["array_index"])
    if dst is None:
        return None
    while len(dst.keyframe_points):
        dst.keyframe_points.remove(dst.keyframe_points[0])
    dst.keyframe_points.add(len(snap["keys"]))
    for kp, p in zip(dst.keyframe_points, snap["keys"]):
        kp.co = (p[0], p[1])
        kp.interpolation, kp.easing = p[2], p[3]
        kp.handle_left_type, kp.handle_right_type = p[4], p[5]
        kp.handle_left = p[6]
        kp.handle_right = p[7]
    if snap["modifiers"]:
        _apply_fmodifiers(dst, snap["modifiers"])
    dst.extrapolation = snap["extrapolation"]
    dst.update()
    return dst


def _al_fcurve_containers(action):
    """Every writable fcurves collection in an action."""
    if hasattr(action, "fcurves"):
        yield action.fcurves
        return
    for layer in action.layers:
        for strip in layer.strips:
            for bag in strip.channelbags:
                yield bag.fcurves


def _al_remove_channels(action, keys):
    """Delete the (data_path, array_index) channels from *action*, looking
    each one up fresh — collection references die as soon as one is gone."""
    removed = 0
    for path, array_index in keys:
        for container in _al_fcurve_containers(action):
            fc = container.find(path, index=array_index)
            if fc is not None:
                container.remove(fc)
                removed += 1
                break
    return removed


def al_extract_bones(index=None, name=None, selected_only=True,
                     channels=None, axes=None, data_type='OBJECT',
                     object_name=None):
    """Extract Selected Bones: move the scoped curves out of a layer into a
    NEW layer directly above it. The combined result is unchanged — the
    curves simply live one layer up, ready to be worked on alone."""
    ob = _al_object(object_name)
    idb = _al_id(ob, data_type)
    ad = _al_animdata(ob, data_type)
    if ad is None:
        raise RuntimeError("%s has no animation layers yet" % ob.name)
    index = _al_resolve_index(ad, index)
    track, strip, _reason = _al_layer_by_index(ad, index)
    if track.lock:
        raise RuntimeError("Layer '%s' is locked — unlock it first" % track.name)
    curves = _al_tool_curves(ob, strip, selected_only, channels, axes,
                             data_type)
    if not curves:
        raise RuntimeError("Nothing in scope to extract from '%s'" % track.name)

    was_tweak, _active = _al_tweak_guard(ad)
    snaps = [_al_snapshot_fcurve(fc) for fc in curves]
    _al_remove_channels(strip.action,
                        [(s["data_path"], s["array_index"]) for s in snaps])
    layer_name = name or (track.name + " Extracted")
    action = _al_new_action(idb, layer_name)
    moved = sum(1 for s in snaps
                if _al_write_fcurve(action, idb, s) is not None)

    new_track = ad.nla_tracks.new(prev=track)
    new_track.name = layer_name
    new_strip = new_track.strips.new(layer_name, int(strip.frame_start),
                                     action)
    new_strip.action_slot = _al_slot_for(action, idb)
    new_strip.use_sync_length = False
    # the extracted curves must keep playing exactly as before: same span,
    # same time mapping, and REPLACE so they own those channels outright
    new_strip.frame_start = strip.frame_start
    new_strip.frame_end = strip.frame_end
    for attr in ("action_frame_start", "action_frame_end", "scale", "repeat",
                 "use_reverse", "extrapolation", "blend_type"):
        try:
            setattr(new_strip, attr, getattr(strip, attr))
        except (AttributeError, TypeError, ValueError):
            pass
    _al_reactivate(ad, was_tweak, new_track.name)
    _al_touch()
    status = anim_layers_status(data_type, object_name)
    status["extracted"] = {"from": track.name, "layer": new_track.name,
                           "curves": moved}
    return status


def al_share_keys(source_index, index=None, selected_only=True,
                  channels=None, axes=None, data_type='OBJECT',
                  object_name=None):
    """Share Layer Keys: give the target layer keys at the SOURCE layer's
    key positions, holding its own current values — so both layers have
    matching key timing and can be edited in step."""
    ob = _al_object(object_name)
    idb = _al_id(ob, data_type)
    ad = _al_animdata(ob, data_type)
    if ad is None:
        raise RuntimeError("%s has no animation layers yet" % ob.name)
    index = _al_resolve_index(ad, index)
    if source_index == index:
        raise RuntimeError("Pick a DIFFERENT layer to take the key timing from")
    track, strip, _reason = _al_layer_by_index(ad, index)
    src_track, src_strip, _r = _al_layer_by_index(ad, source_index)
    if track.lock:
        raise RuntimeError("Layer '%s' is locked — unlock it first" % track.name)
    if src_strip.action is None:
        raise RuntimeError("Layer '%s' has no keys to share" % src_track.name)

    # source key frames -> scene time -> the target strip's action time, so
    # shared keys line up on the TIMELINE even when the strips differ
    bones = None
    if selected_only and ob.type == 'ARMATURE' and ob.pose:
        bones = {pb.name for pb in ob.pose.bones if bone_is_selected(pb)}
        if not bones:
            raise RuntimeError("No bones selected — select some, or turn "
                               "'only selected bones' off")
    wanted = {}
    for fc in _al_action_fcurves_ro(src_strip.action):
        if not _al_filter_ok(fc.data_path, fc.array_index, channels, axes):
            continue
        bone = _bone_of_path(fc.data_path)
        if bones is not None and (bone is None or bone not in bones):
            continue
        key = (fc.data_path, fc.array_index)
        for kp in fc.keyframe_points:
            for scene_t in _al_strip_scene_times(src_strip, kp.co.x):
                wanted.setdefault(key, set()).add(
                    round(_al_scene_to_action_frame(strip, scene_t), 4))
    if not wanted:
        raise RuntimeError("No keys in scope on '%s'" % src_track.name)

    action = strip.action
    if action is None:
        action = _al_new_action(idb, track.name)
        strip.action = action
        strip.action_slot = _al_slot_for(action, idb)
    added = 0
    for (path, array_index), frames in wanted.items():
        fc = _al_layer_fcurve(action, idb, path, array_index)
        if fc is None:
            continue
        existing = {round(kp.co.x, 4) for kp in fc.keyframe_points}
        # hold the curve's own value at each new frame — sharing timing must
        # never change what the layer is doing
        for f in sorted(frames):
            if f in existing:
                continue
            fc.keyframe_points.insert(f, fc.evaluate(f))
            added += 1
        fc.update()
    _al_touch()
    status = anim_layers_status(data_type, object_name)
    status["shared"] = {"from": src_track.name, "layer": track.name,
                        "keys": added, "channels": len(wanted)}
    return status


def al_extract_markers(index=None, name=None, selected_only=True,
                       channels=None, axes=None, mute_source=True,
                       data_type='OBJECT', object_name=None):
    """Extract Marked Keyframes — the mocap cleanup tool. Samples a dense
    layer ONLY at the timeline markers and writes those poses into a new
    layer on top, so a per-frame mocap curve becomes a handful of editable
    keys. The source is muted, never destroyed."""
    ob = _al_object(object_name)
    idb = _al_id(ob, data_type)
    ad = _al_animdata(ob, data_type)
    if ad is None:
        raise RuntimeError("%s has no animation layers yet" % ob.name)
    scene = bpy.context.scene
    markers = sorted({int(m.frame) for m in scene.timeline_markers})
    if not markers:
        raise RuntimeError("No timeline markers — add markers at the poses "
                           "you want to keep")
    index = _al_resolve_index(ad, index)
    track, strip, _reason = _al_layer_by_index(ad, index)
    curves = _al_tool_curves(ob, strip, selected_only, channels, axes,
                             data_type)
    if not curves:
        raise RuntimeError("Nothing in scope to extract from '%s'" % track.name)
    targets = [(fc.data_path, fc.array_index) for fc in curves]

    # sample the layer ALONE so the extracted keys carry its motion only,
    # not whatever the rest of the stack contributes
    was_tweak, active_name = _al_tweak_guard(ad)
    saved_mute = {tr.name: tr.mute for tr, _s, _r in _al_layers(ad)}
    try:
        for tr, _s, _r in _al_layers(ad):
            tr.mute = tr != track
        samples = _al_sample_channels(idb, targets, [float(f) for f in markers])
    finally:
        for tr, _s, _r in _al_layers(ad):
            if tr.name in saved_mute:
                tr.mute = saved_mute[tr.name]

    layer_name = name or (track.name + " Markers")
    action = _al_new_action(idb, layer_name)
    new_track = ad.nla_tracks.new(prev=track)
    new_track.name = layer_name
    new_strip = new_track.strips.new(layer_name, scene.frame_start, action)
    new_strip.action_slot = _al_slot_for(action, idb)
    new_strip.use_sync_length = False
    new_strip.frame_end_ui = float(max(scene.frame_end, scene.frame_start + 1))
    new_strip.blend_type = strip.blend_type
    new_strip.extrapolation = 'HOLD'

    written = keys = 0
    for key in targets:
        vals = samples.get(key)
        if not vals:
            continue
        fc = _al_layer_fcurve(action, idb, key[0], key[1])
        if fc is None:
            continue
        for f in markers:
            kp = fc.keyframe_points.insert(
                _al_scene_to_action_frame(new_strip, f), vals[float(f)])
            # smooth by default: the point of the tool is a clean, editable
            # curve through the marked poses
            kp.interpolation = 'BEZIER'
            kp.handle_left_type = kp.handle_right_type = 'AUTO_CLAMPED'
            keys += 1
        fc.update()
        written += 1
    if mute_source:
        track.mute = True
    _al_reactivate(ad, was_tweak, new_track.name)
    _al_touch()
    status = anim_layers_status(data_type, object_name)
    status["markers"] = {"from": track.name, "layer": new_track.name,
                         "curves": written, "keys": keys,
                         "markers": len(markers),
                         "source_muted": bool(mute_source)}
    return status


AL_MULTIKEY_OPS = ('OFFSET', 'REPLACE', 'SCALE', 'RANDOMIZE')


def _al_multikey_points(fc, selected_keys):
    """Indices of the keyframe points a multikey op should touch. Indices,
    not references: the ops read and write the same array repeatedly."""
    if not selected_keys:
        return list(range(len(fc.keyframe_points)))
    return [i for i, kp in enumerate(fc.keyframe_points)
            if kp.select_control_point]


def al_multikey(op='OFFSET', value=0.0, index=None, selected_only=True,
                selected_keys=True, channels=None, axes=None,
                pivot='AVERAGE', seed=0, data_type='OBJECT',
                object_name=None):
    """Multikey: edit many keys of the current layer at once, without
    touching their timing.

    - OFFSET     add *value* to every targeted key
    - REPLACE    set them all to *value*
    - SCALE      multiply by *value* around a pivot — per-curve AVERAGE of
                 the targeted keys, or ZERO (on an additive layer zero is the
                 neutral value, so scaling about it scales the layer's effect)
    - RANDOMIZE  add a random offset in +/- *value*, seeded so the same seed
                 gives the same result twice

    Scoped by the shared filter + bone selection, and by default limited to
    the keys SELECTED in Blender's dope sheet / graph editor."""
    if op not in AL_MULTIKEY_OPS:
        raise RuntimeError("Unknown multikey op: %r" % op)
    if pivot not in ('AVERAGE', 'ZERO'):
        raise RuntimeError("Pivot must be AVERAGE or ZERO")
    ob = _al_object(object_name)
    ad = _al_animdata(ob, data_type)
    if ad is None:
        raise RuntimeError("%s has no animation layers yet" % ob.name)
    index = _al_resolve_index(ad, index)
    track, strip, _reason = _al_layer_by_index(ad, index)
    if track.lock:
        raise RuntimeError("Layer '%s' is locked — unlock it first" % track.name)
    curves = _al_tool_curves(ob, strip, selected_only, channels, axes,
                             data_type)
    if not curves:
        raise RuntimeError("No curves in scope for Multikey")
    # stable order so a seeded randomize is reproducible whatever order the
    # channelbag hands the curves back in
    curves.sort(key=lambda fc: (fc.data_path, fc.array_index))
    rng = random.Random(seed)
    amount = float(value)

    touched = keys = 0
    for fc in curves:
        if fc.lock:
            continue
        points = _al_multikey_points(fc, selected_keys)
        if not points:
            continue
        base = 0.0
        if op == 'SCALE' and pivot == 'AVERAGE':
            base = sum(fc.keyframe_points[i].co.y for i in points) / len(points)
        for i in points:
            kp = fc.keyframe_points[i]
            old = kp.co.y
            if op == 'OFFSET':
                new = old + amount
            elif op == 'REPLACE':
                new = amount
            elif op == 'SCALE':
                new = base + (old - base) * amount
            else:
                new = old + rng.uniform(-amount, amount)
            if op == 'SCALE':
                # scaling the handles around the same pivot scales the whole
                # curve vertically; shifting them would flatten the tangents
                kp.handle_left.y = base + (kp.handle_left.y - base) * amount
                kp.handle_right.y = base + (kp.handle_right.y - base) * amount
            else:
                delta = new - old
                kp.handle_left.y += delta
                kp.handle_right.y += delta
            kp.co.y = new
            keys += 1
        fc.update()
        touched += 1
    if not keys:
        raise RuntimeError(
            "No selected keyframes in scope — select keys in the Dope Sheet "
            "or Graph Editor, or turn 'only selected keyframes' off"
            if selected_keys else "No keys in scope")
    _al_touch()
    status = anim_layers_status(data_type, object_name)
    status["multikey"] = {"layer": track.name, "op": op, "value": amount,
                          "pivot": pivot if op == 'SCALE' else None,
                          "curves": touched, "keys": keys,
                          "selected_keys": bool(selected_keys)}
    return status


def _al_copy_source_modifiers(rows, bake_set, action):
    """merge_modifiers=False: the result keeps the sources' F-modifiers LIVE
    (they were muted during sampling so their effect isn't in the keys too).
    Stacked bottom->top when several sources modify the same channel."""
    per_channel = {}
    for i in bake_set:
        for fc in _al_action_fcurves_ro(rows[i][1].action):
            if len(fc.modifiers):
                per_channel.setdefault(
                    (fc.data_path, fc.array_index),
                    []).extend(_serialize_fmodifiers(fc))
    if not per_channel:
        return
    for fc in _al_action_fcurves_ro(action):
        mods = per_channel.get((fc.data_path, fc.array_index))
        if mods:
            _apply_fmodifiers(fc, mods)


def _al_nla_bake_action(ob, ad, rows, include, steps, selected_only,
                        clear_constraints):
    """The Blender-native path: bpy.ops.nla.bake with visual keying over the
    timeline, layers outside the range muted. The op leaves its result as the
    active action — we pop that off and hand it back for the result layer."""
    scene = bpy.context.scene
    include_set = set(include)
    saved_mute = {tr.name: tr.mute for tr, _s, _r in rows}
    saved_sel = [o for o in bpy.context.view_layer.objects if o.select_get()]
    saved_active = bpy.context.view_layer.objects.active
    try:
        for i, (tr, _s, _r) in enumerate(rows):
            if i not in include_set:
                tr.mute = True
        for o in saved_sel:
            if o != ob:
                o.select_set(False)
        ob.select_set(True)
        bpy.context.view_layer.objects.active = ob
        bake_types = {'POSE'} if ob.type == 'ARMATURE' else {'OBJECT'}
        with bpy.context.temp_override(active_object=ob, object=ob,
                                       selected_objects=[ob],
                                       selected_editable_objects=[ob]):
            bpy.ops.nla.bake(frame_start=scene.frame_start,
                             frame_end=scene.frame_end,
                             step=max(1, int(steps)),
                             only_selected=bool(selected_only),
                             visual_keying=True,
                             clear_constraints=bool(clear_constraints),
                             use_current_action=False,
                             bake_types=bake_types)
        action = ad.action
        if action is None:
            raise RuntimeError("NLA bake produced no action")
        ad.action = None
        return action
    finally:
        for tr, _s, _r in _al_layers(ad):
            if tr.name in saved_mute:
                tr.mute = saved_mute[tr.name]
        for o in saved_sel:
            try:
                o.select_set(True)
            except RuntimeError:
                pass
        bpy.context.view_layer.objects.active = saved_active


def al_bake(mode='NEW', direction='ALL', index=None, bake_type='AL',
            smart=False, steps=1, selected_only=False, merge_modifiers=True,
            clear_constraints=False, copy_original=False,
            data_type='OBJECT', object_name=None):
    """Bake/merge the layer stack. mode NEW = result layer added above the
    baked range (sources kept); MERGE = sources deleted, result spliced in at
    the bottom source's position. direction DOWN bakes layers 0..index,
    ALL bakes the whole stack. Muted layers are excluded — and survive a
    MERGE untouched. Range = the timeline."""
    if mode not in ('NEW', 'MERGE'):
        raise RuntimeError("mode must be NEW or MERGE")
    if direction not in ('DOWN', 'UP', 'ALL'):
        raise RuntimeError("direction must be DOWN, UP or ALL")
    if bake_type not in ('AL', 'NLA'):
        raise RuntimeError("bake_type must be AL or NLA")
    if bake_type == 'NLA' and direction == 'UP':
        raise RuntimeError("Upward bake works with the Anim Layers engine "
                           "only — switch Bake Type to Anim Layers")
    if bake_type == 'NLA' and smart:
        raise RuntimeError("Smart bake works with the Anim Layers engine "
                           "only — switch Bake Type or turn Smart Bake off")
    if bake_type == 'NLA' and data_type != 'OBJECT':
        raise RuntimeError("Blender's NLA bake can't bake shape-key layers "
                           "— use the Anim Layers engine")

    ob = _al_object(object_name)
    idb = _al_id(ob, data_type)
    ad = _al_animdata(ob, data_type)
    if ad is None or not ad.nla_tracks:
        raise RuntimeError("%s has no animation layers yet" % ob.name)
    rows = _al_layers(ad)

    additive = direction == 'UP'
    if direction == 'ALL':
        include = list(range(len(rows)))
    else:
        ref = index if index is not None else _al_active_index(ad)
        if ref is None or not 0 <= ref < len(rows):
            raise RuntimeError("No layer to bake from")
        if direction == 'DOWN':
            include = list(range(0, ref + 1))
        else:
            # UP: from the reference layer up to (not including) the next
            # REPLACE layer — an upper Replace wipes whatever we'd merge, so
            # it is the natural ceiling. Foreign multi-strip tracks also
            # stop the walk (we can't reason about their blending).
            end = len(rows)
            for i in range(ref + 1, len(rows)):
                strip = rows[i][1]
                if strip is None:
                    if rows[i][2] == "empty track":
                        continue
                    end = i
                    break
                if strip.blend_type == 'REPLACE':
                    end = i
                    break
            include = list(range(ref, end))

    bake_set = []
    for i in include:
        track, strip, reason = rows[i]
        if track.mute:
            continue
        healthy = strip is not None and strip.action is not None
        if mode == 'MERGE' and (not healthy or reason):
            raise RuntimeError("Layer '%s' can't be merged: %s"
                               % (track.name, reason or "no action"))
        if healthy:
            bake_set.append(i)
    if not bake_set:
        raise RuntimeError("Nothing to bake — every layer in range is muted "
                           "or empty")
    if additive:
        # the upward result is a pure ADD delta — only additive blending is
        # representable that way (COMBINE quats multiply, MULTIPLY scales)
        for i in bake_set:
            bt = rows[i][1].blend_type
            if bt not in ('ADD', 'SUBTRACT'):
                raise RuntimeError(
                    "Upward bake needs additive layers — '%s' is %s; "
                    "bake down instead" % (rows[i][0].name, bt))

    was_tweak, _active_name = _al_tweak_guard(ad)
    _al_ensure_ranges(ad)
    scene = bpy.context.scene

    if bake_type == 'NLA':
        # Blender's own visual bake: constraints/drivers/sims land in the
        # keys (and can then be cleared). Per-frame or stepped only.
        action = _al_nla_bake_action(ob, ad, rows, include, steps,
                                     selected_only, clear_constraints)
        wrote = sum(1 for _fc in _al_action_fcurves_ro(action))
        key_count = sum(len(fc.keyframe_points)
                        for fc in _al_action_fcurves_ro(action))
        frames = [float(f) for f in
                  range(scene.frame_start, scene.frame_end + 1,
                        max(1, int(steps)))]
    else:
        targets = _al_bake_targets(rows, bake_set, selected_only, ob,
                                   data_type)
        if not targets:
            raise RuntimeError(
                "Nothing to bake — no animated channels found"
                + (" on the selected bones" if selected_only else ""))
        if smart:
            # per-channel key times from the sources; ensure_ranges ran
            # first so the strip state the mapper reads is what evaluates
            frame_map, key_meta = _al_smart_keys(rows, bake_set, targets,
                                                 scene, merge_modifiers)
            frames = sorted({f for fl in frame_map.values() for f in fl})
        else:
            step = max(1, int(steps))
            grid = list(range(scene.frame_start, scene.frame_end + 1, step))
            if grid[-1] != scene.frame_end:
                grid.append(scene.frame_end)  # the range end is always keyed
            frames = [float(f) for f in grid]
            frame_map = {t: frames for t in targets}
            key_meta = {}
        include_set = set(include)
        saved_mute = {tr.name: tr.mute for tr, _s, _r in rows}
        muted_mods = []
        if not merge_modifiers:
            # keep-as-modifiers: silence them while sampling so their effect
            # isn't baked into the keys AND re-applied live afterwards
            for i in bake_set:
                for fc in _al_action_fcurves_ro(rows[i][1].action):
                    for m in fc.modifiers:
                        if not m.mute:
                            m.mute = True
                            muted_mods.append(m)
        try:
            # everything above the baked range is silenced; for an absolute
            # (DOWN/ALL) bake everything outside it is. Below an UP range the
            # stack keeps playing — the delta subtracts it back out.
            for i, (tr, _s, _r) in enumerate(rows):
                if i > max(include) or (not additive and i not in include_set):
                    tr.mute = True
            samples = _al_sample_channels(idb, targets, frames)
            if additive:
                # second pass, additive set muted: result = with - without
                for i in bake_set:
                    rows[i][0].mute = True
                base_samples = _al_sample_channels(idb, targets, frames)
                for key, vals in samples.items():
                    base = base_samples.get(key, {})
                    for f in list(vals):
                        vals[f] = vals[f] - base.get(f, 0.0)
        finally:
            for m in muted_mods:
                m.mute = False
            for tr, _s, _r in _al_layers(ad):
                if tr.name in saved_mute:
                    tr.mute = saved_mute[tr.name]

    merged_names = [rows[i][0].name for i in bake_set]
    if mode == 'MERGE':
        result_name = rows[min(bake_set)][0].name
        anchor = rows[min(bake_set)][0]
    else:
        result_name = AL_BAKE_RESULT_NAME
        anchor = rows[max(bake_set)][0]

    if bake_type == 'NLA':
        action.name = result_name
    else:
        action = _al_new_action(idb, result_name)
        wrote, key_count = _al_write_baked(action, idb, samples, targets,
                                           frame_map, key_meta)
        if not merge_modifiers:
            _al_copy_source_modifiers(rows, bake_set, action)

    backups = []
    if mode == 'MERGE' and copy_original:
        # recoverable merge: fake-user copies of every source action
        for i in bake_set:
            src = rows[i][1].action
            if src is not None:
                cp = src.copy()
                cp.name = src.name + ".orig"
                cp.use_fake_user = True
                backups.append(cp.name)

    new_track = ad.nla_tracks.new(prev=anchor)
    # MERGE wants the bottom source's name, which is taken until the sources
    # are gone — park on a temp name, claim it after the removal below
    new_track.name = result_name + (".__madi_baking" if mode == 'MERGE' else "")
    strip = new_track.strips.new(result_name, scene.frame_start, action)
    strip.action_slot = _al_slot_for(action, idb)
    strip.use_sync_length = False
    strip.frame_end_ui = float(max(scene.frame_end, scene.frame_start + 1))
    strip.blend_type = 'ADD' if additive else 'REPLACE'
    strip.extrapolation = 'HOLD'

    if mode == 'MERGE':
        if _al_solo_state(idb) in merged_names:
            del idb[AL_SOLO_PROP]
        for name in merged_names:
            _al_set_infl_flag(idb, name, False)   # no stale flags on reuse
            _al_set_range_flag(idb, name, False)
        for i in sorted(bake_set, reverse=True):
            ad.nla_tracks.remove(rows[i][0])
        new_track.name = result_name
        strip.name = result_name
        action.name = result_name

    for i, (tr, _s, _r) in enumerate(_al_layers(ad)):
        if tr == new_track:
            _al_activate(ad, i, tweak=was_tweak)
            break
    _al_touch()
    status = anim_layers_status(data_type, object_name)
    status["baked"] = {"mode": mode, "direction": direction,
                       "bake_type": bake_type, "result": new_track.name,
                       "result_blend": 'ADD' if additive else 'REPLACE',
                       "merged": merged_names, "channels": wrote,
                       "keys": key_count, "smart": bool(smart),
                       "steps": max(1, int(steps)), "backups": backups,
                       "frames": len(frames), "frame_start": frames[0],
                       "frame_end": frames[-1]}
    return status


# ---------------------------------------------------------------- vertex groups
# Marty, 2026-08-04: store vertex groups, and be able to put them on a DIFFERENT
# mesh too.
#
# ⚠ THESE ARE TWO DIFFERENT OPERATIONS AND THEY MUST NEVER SHARE A BUTTON.
#   * apply_vgroups(mode="EXACT")    - index-based. Lossless, and only possible
#     when the vertex count matches: weight N goes on vertex N, exactly as it
#     was saved. This is a RESTORE.
#   * apply_vgroups(mode="TRANSFER") - Blender's own data_transfer, nearest
#     face interpolated. Works on any topology and is an ESTIMATE. On a
#     character it always needs cleanup.
# Labelling them the same is how somebody ships a rig with quietly wrong
# weights, so the app shows them as separate actions with separate warnings.

def save_vgroups(library_root, relfolder, name, objects=None, groups=None,
                 description="", overwrite=False):
    """Store the vertex groups of the selected (or named) mesh objects.

    Only vertices that are actually IN a group are written — an unassigned
    vertex is absent rather than stored as a zero, which keeps a 100k mesh with
    three small groups a small file.
    """
    obs = _mesh_objects(objects)
    meshes = []
    total_groups = 0
    for ob in obs:
        want = set(groups.get(ob.name, [])) if groups else None
        entries = []
        for vg in ob.vertex_groups:
            if want is not None and vg.name not in want:
                continue
            indices = []
            weights = []
            for v in ob.data.vertices:
                for g in v.groups:
                    if g.group == vg.index:
                        indices.append(v.index)
                        weights.append(round(g.weight, 6))
                        break
            entries.append({"name": vg.name, "indices": indices,
                            "weights": weights,
                            "lock": bool(getattr(vg, "lock_weight", False))})
            total_groups += 1
        meshes.append({"object": ob.name, "verts": len(ob.data.vertices),
                       "groups": entries})
    if not total_groups:
        raise RuntimeError("No vertex groups on the selected mesh(es)")

    data = {"type": "vgroups",
            "metadata": _metadata(obs[0], {"description": description}),
            "meshes": meshes}
    item_dir = os.path.join(library_root, relfolder,
                            safe_name(name) + VGROUPS_EXT)
    if os.path.isdir(item_dir):
        if not overwrite:
            raise RuntimeError("Item already exists: %s (use overwrite)" % item_dir)
        version_item(item_dir)
    os.makedirs(item_dir, exist_ok=True)
    with open(os.path.join(item_dir, "vgroups.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
    return {"path": item_dir, "groups": total_groups, "meshes": len(meshes)}


def list_vertex_groups(objects=None):
    """Selected (or named) meshes with their vertex groups — the app's save
    checklist source."""
    out = []
    for ob in _mesh_objects(objects):
        out.append({"object": ob.name, "verts": len(ob.data.vertices),
                    "groups": [vg.name for vg in ob.vertex_groups]})
    return out


def _apply_exact(ob, entry, replace):
    """Weight N onto vertex N. Refuses rather than guessing."""
    vg = ob.vertex_groups.get(entry["name"])
    if vg is not None and replace:
        ob.vertex_groups.remove(vg)
        vg = None
    if vg is None:
        vg = ob.vertex_groups.new(name=entry["name"])
    count = len(ob.data.vertices)
    added = 0
    for index, weight in zip(entry["indices"], entry["weights"]):
        if index >= count:
            continue
        vg.add([index], float(weight), 'REPLACE')
        added += 1
    if hasattr(vg, "lock_weight"):
        vg.lock_weight = bool(entry.get("lock", False))
    return added


def apply_vgroups(item_path, mode="EXACT", to_active=False, replace=True,
                  source_object=None):
    """Put stored vertex groups back.

    `mode="EXACT"`  — index-based restore. Requires the vertex count to match.
    `mode="TRANSFER"` — spatial, via a temporary copy of the saved mesh and
                        Blender's data_transfer. Any topology; approximate.

    ⚠ EXACT REFUSES ON A VERTEX-COUNT MISMATCH rather than doing its best. The
    failure mode of guessing here is weights that look plausible and are wrong,
    which is far worse than being told no — the user can choose TRANSFER, which
    at least says what it is.
    """
    with open(os.path.join(item_path, "vgroups.json"), "r", encoding="utf-8") as f:
        data = json.load(f)
    meshes = data.get("meshes") or []
    if not meshes:
        raise RuntimeError("This item has no vertex groups in it")

    targets = []
    if to_active:
        ob = bpy.context.active_object
        if ob is None or ob.type != 'MESH':
            raise RuntimeError("Select the mesh to receive the groups first")
        entry = next((m for m in meshes
                      if m["object"] == (source_object or m["object"])), meshes[0])
        targets.append((ob, entry))
    else:
        for m in meshes:
            ob = bpy.data.objects.get(m["object"])
            if ob is not None and ob.type == 'MESH':
                targets.append((ob, m))
        if not targets:
            raise RuntimeError(
                "None of the meshes in this item are in the scene. Select a "
                "mesh and use 'Apply to active object' instead.")

    if mode == "TRANSFER":
        return _transfer_vgroups(meshes, source_object)

    applied = 0
    skipped = []
    for ob, entry in targets:
        if mode == "EXACT" and len(ob.data.vertices) != int(entry.get("verts", -1)):
            skipped.append((ob.name,
                            "vertex count differs (%d here, %d saved) - use "
                            "Transfer for a different mesh"
                            % (len(ob.data.vertices), entry.get("verts", 0))))
            continue
        for group in entry.get("groups") or []:
            _apply_exact(ob, group, replace)
            applied += 1
    if skipped and not applied:
        raise RuntimeError(skipped[0][1])
    return {"applied": applied, "objects": len(targets),
            "skipped": [{"object": n, "reason": r} for n, r in skipped]}


def _transfer_vgroups(meshes, source_object=None):
    """Spatially transfer stored weights onto the ACTIVE mesh.

    ⚠ THE HONEST LIMITATION, and the app has to say it out loud: this item
    stores weights BY VERTEX INDEX, not geometry. A spatial transfer needs
    something with a shape to sample from, so the mesh the groups were saved
    FROM has to still be in the scene. When it is, the flow is exact-restore
    onto that mesh, then Blender's own data_transfer onto the target.

    Storing a copy of the mesh instead would make this self-contained — and
    would also turn a 20 KB item into a multi-megabyte one for a case that
    mostly does not come up. Refusing clearly beats bloating every item for it.
    """
    target = bpy.context.active_object
    if target is None or target.type != 'MESH':
        raise RuntimeError("Select the mesh that should RECEIVE the weights")

    entry = None
    for m in meshes:
        if source_object and m["object"] != source_object:
            continue
        if bpy.data.objects.get(m["object"]) is not None:
            entry = m
            break
    if entry is None:
        raise RuntimeError(
            "Transferring needs the mesh these groups came from to be in the "
            "scene - the item stores weights per vertex, not the shape they "
            "were painted on. Append that mesh and try again, or use the exact "
            "restore onto a mesh with the same vertex count.")

    source = bpy.data.objects.get(entry["object"])
    if source is target:
        raise RuntimeError("The source and the target are the same mesh - use "
                           "the exact restore instead.")

    # Make sure the source really carries the weights we mean to transfer.
    for group in entry.get("groups") or []:
        _apply_exact(source, group, True)

    names = [g["name"] for g in entry.get("groups") or []]
    for name in names:
        if target.vertex_groups.get(name) is None:
            target.vertex_groups.new(name=name)

    previous = bpy.context.view_layer.objects.active
    try:
        # ⚠ THE ACTIVE OBJECT IS THE SOURCE, the selected ones are written to —
        # that is `data_transfer`'s normal direction, so no reverse flag.
        # `use_reverse_transfer=True` swaps which object each of
        # `layers_select_src` / `layers_select_dst` is validated against, and
        # those two enums are DIFFERENT and DYNAMIC: src is ('ALL', <every
        # layer name>) while dst is ('ACTIVE', 'NAME', 'INDEX'). Getting the
        # direction wrong therefore fails as a baffling "enum not found" on a
        # value that is perfectly valid for the property you thought you were
        # setting. Keeping the plain direction keeps the two enums where the
        # documentation says they are.
        bpy.ops.object.select_all(action='DESELECT')
        target.select_set(True)
        source.select_set(True)
        bpy.context.view_layer.objects.active = source
        bpy.ops.object.data_transfer(
            data_type='VGROUP_WEIGHTS', vert_mapping='POLYINTERP_NEAREST',
            layers_select_src='ALL', layers_select_dst='NAME',
            mix_mode='REPLACE')
    finally:
        bpy.context.view_layer.objects.active = previous
    return {"applied": len(names), "objects": 1, "skipped": [],
            "transferred_from": source.name, "approximate": True}
