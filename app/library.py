"""Disk-side model of a library: folders + items (mirrors the add-on's core.py)."""

import json
import os
import re
import shutil
import time

# ⚠ MUST MATCH core.ITEM_EXTS on the add-on side (asserted by a test) — this is
# the same tree scanned without a bridge, so a type missing here saves fine and
# then never shows up in the grid.
# ⚠ THIS TUPLE IS ONE OF THREE. core.ITEM_EXTS is the second (the add-on scans
# the same tree) and `panels.Sidebar.type_checks` is the third — and the third
# is the one whose absence is SILENT: refilter() drops any type with no
# checkbox, so the item saves, scans and never draws. See core.py's comment.
ITEM_EXTS = (".pose", ".set", ".anim", ".mirror", ".shapes", ".remap", ".abc",
             ".picker", ".vgroups", ".renderpreset",
             # Blender assets (2026-08-22). ⚠ The first four types whose
             # payload is a `.blend`, not JSON — the JSON beside it is a
             # SIDECAR, and it exists so this module can describe an asset
             # with Blender closed. See blender_addon\...\assetlib.py.
             ".object", ".collection", ".material", ".nodegroup")
DATA_FILES = {"pose": "pose.json", "set": "set.json", "anim": "anim.json",
              "mirror": "mirror.json", "shapes": "shapes.json",
              "remap": "remap.json", "abc": "abc.json",
              "picker": "picker.json", "vgroups": "vgroups.json",
              "renderpreset": "renderpreset.json",
              "object": "object.json", "collection": "collection.json",
              "material": "material.json", "nodegroup": "nodegroup.json"}

# The kinds whose payload is a Blender file. Kept as a set rather than spelled
# out at each call site: three places already ask "is this an asset?" and a
# fourth that forgets would be a silent divergence.
ASSET_KINDS = ("object", "collection", "material", "nodegroup")
ASSET_BLEND = "asset.blend"
CATALOG_FILE = "blender_assets.cats.txt"


def read_catalogs(root):
    """Blender's asset catalogs, read straight off disk.

    ⚠⚠ THE POINT IS THAT THIS NEEDS NO BLENDER. Blender stores catalogs as
    plain text at the library root — `UUID:catalog/path:simple name` — which is
    what lets the Assets half of the tab work with Blender closed, exactly like
    the rest of the library does. The add-on has the same parser for the case
    where Blender is open and has rewritten the file since the last scan; if
    one of the two ever changes, both change.

    Returns [{uuid, path, name}], sorted by path.
    """
    rows = []
    path = os.path.join(root, CATALOG_FILE)
    if not os.path.isfile(path):
        return rows
    try:
        with open(path, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return rows
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("VERSION"):
            continue
        parts = line.split(":", 2)
        if len(parts) == 3:
            rows.append({"uuid": parts[0], "path": parts[1], "name": parts[2]})
    rows.sort(key=lambda r: r["path"].lower())
    return rows

# versioning layout — MUST match core.py's version_item() on the add-on side
VERSIONS_DIR = "versions"
_PAYLOAD_FILES = ("pose.json", "set.json", "anim.json", "mirror.json",
                  "shapes.json", "remap.json", "abc.json", "picker.json",
                  "vgroups.json", "renderpreset.json", "thumbnail.jpg",
                  # ⚠⚠ An asset's payload is its .blend AND its sidecar. Miss
                  # `asset.blend` here and versioning an asset keeps the
                  # description and throws away the thing itself — a version
                  # you cannot restore, which is worse than no version.
                  "asset.blend", "object.json", "collection.json",
                  "material.json", "nodegroup.json",
                  # ⚠ the picker's CLEAN reference picture. Every thumbnail
                  # compose starts from this file rather than from
                  # thumbnail.jpg, so a version that kept only the composite
                  # could never be re-drawn (app\picker.compose_thumbnail).
                  "reference.jpg")
_VERSION_RE = re.compile(r"v\d+$")


TAGS_FILE = "tags.json"   # sidecar: NOT in _PAYLOAD_FILES, so tags/color survive
                          # overwrite-saves and versioning untouched
_meta_cache = {}          # (path, mtime) -> metadata dict (author/frames filters)


# Item types where "one or many?" is a real question the user answered at save
# time, because both have a "save each as its own item" option (Marty,
# 2026-08-05: "we need another icon near the thumbnail indicating it's a bulk
# export and not just one").
#
# ⚠ Deliberately NOT every type that happens to hold a list. A .pose holds forty
# bones and a .set holds a selection — those are not "bulk", that is simply what
# the type IS, and badging them would make the badge mean nothing.
_BULK_UNITS = {"vgroups": "groups", "shapes": "keys"}


def _bulk_count(data):
    key = _BULK_UNITS.get(data.get("type"))
    if not key:
        return 0
    return sum(len(m.get(key) or []) for m in data.get("meshes") or [])


# Badges an .anim tile can carry, in draw order (Marty, 2026-08-05: "add small
# icon that indicate this near thumbnail preview, also an icon when animation is
# baked" / "also make another small icon near the thumbnail if this is chosen").
# (flag key, metadata key, what it means)
ANIM_FLAGS = (
    ("baked", "baked", "Baked: sampled on every frame, IK/constraints included"),
    ("modifiers", "fcurve_modifiers",
     "F-curve modifiers and graph-editor data kept"),
    ("props", "bone_props", "Bone properties stored with the animation"),
)

_META_HEAD_BYTES = 16384    # enough for any metadata block ever written
_fastmeta_cache = {}        # (path, mtime) -> metadata dict
_catalog_cache = {}         # (path, mtime) -> Blender catalog path, "" if none


def _peek_metadata(path):
    """The `metadata` block of an item json, WITHOUT parsing the whole file.

    ⚠ THIS EXISTS FOR ONE REASON: the anim badges are drawn on every tile
    PAINT, and an .anim for a 461-bone rig over a few hundred frames is
    megabytes of curve data. `json.load`-ing every visible one on a scroll is
    the exact cost `Item.bulk_count` was written to avoid, and it would be paid
    by the commonest item type in the library rather than by two rare ones.

    `metadata` is written SECOND (`{"type": …, "metadata": …, "curves": …}` and
    json.dump preserves dict order), so the first few KB hold all of it.
    `raw_decode` parses exactly that one object and stops. If anything at all
    does not fit the fast path — an older layout, a metadata block past the
    read window — it falls back to a full parse, so this is slower in the worst
    case and never wrong.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            head = f.read(_META_HEAD_BYTES)
    except OSError:
        return {}
    idx = head.find('"metadata"')
    if idx != -1:
        colon = head.find(":", idx + len('"metadata"'))
        if colon != -1:
            try:
                obj, _end = json.JSONDecoder().raw_decode(head, colon + 1)
            except ValueError:
                obj = None
            if isinstance(obj, dict):
                return obj
    try:                                   # fallback: parse the whole thing
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f).get("metadata", {}) or {}
    except (OSError, ValueError, AttributeError):
        return {}


class Item:
    __slots__ = ("path", "relpath", "folder", "name", "type", "mtime",
                 "tags", "color", "bare")

    def __init__(self, path, relpath, folder, name, typ, mtime, bare=False):
        self.path = path
        self.relpath = relpath
        self.folder = folder      # library-relative folder ("" = root, "Lily/Face")
        self.name = name
        self.type = typ
        self.mtime = mtime
        self.bare = bare          # loose .abc FILE (not an item folder): no
                                  # tags/versions/previews until converted
        self.tags = []
        self.color = None
        self._load_tags()

    def _load_tags(self):
        try:
            with open(os.path.join(self.path, TAGS_FILE), "r", encoding="utf-8") as f:
                data = json.load(f)
            self.tags = [t for t in data.get("tags", []) if isinstance(t, str)]
            self.color = data.get("color") or None
        except (OSError, ValueError):
            pass

    def save_tags(self):
        with open(os.path.join(self.path, TAGS_FILE), "w", encoding="utf-8") as f:
            json.dump({"tags": self.tags, "color": self.color}, f, indent=1)

    def _parsed(self):
        """(metadata, bulk_count), cached per (path, mtime) — anim jsons can be
        big, so filters only pay the parse once per file version.

        ⚠ The count is derived HERE, in the parse `meta()` was already paying
        for, rather than from a field written at save time. That is what makes
        the bulk badge appear on items saved before the badge existed — a
        metadata field would only ever mark items saved after it, and an old
        item that holds forty groups is exactly the one worth marking."""
        key = (self.path, self.mtime)
        entry = _meta_cache.get(key)
        if entry is None:
            data = self.read_data()
            entry = (data.get("metadata", {}) or {}, _bulk_count(data))
            _meta_cache[key] = entry
            while len(_meta_cache) > 2048:
                _meta_cache.pop(next(iter(_meta_cache)))
        return entry

    def meta(self):
        return self._parsed()[0]

    def bulk_count(self):
        """How many units this item holds — 0 for types where the question is
        meaningless, so `> 1` is the whole test for "this is a bulk one".

        ⚠ THE TYPE CHECK COMES FIRST, BEFORE ANY PARSE, and that is the whole
        performance story. This runs on every tile PAINT, whereas `meta()` only
        ran when an author/date/length filter was set — so without the guard,
        merely scrolling the grid would parse every `.anim` json in view, and
        those are the ones `read_data` warns are big. Only the two bulk-capable
        types ever pay, and then only once per item version.
        """
        if self.type not in _BULK_UNITS:
            return 0
        return self._parsed()[1]

    def catalog(self):
        """The Blender catalog this asset is filed under, or "".

        ⚠ THE TYPE CHECK COMES FIRST, for the same reason `bulk_count` does:
        this is called once per item on every refilter, and without the guard
        a catalog filter would parse every `.anim` json in the library to ask
        a question only four types can answer.

        ⚠ Read from the SIDECAR rather than from the folder path. An asset's
        folder is where Marty put it; its catalog is what Blender files it
        under, and the two are deliberately independent — a `Props/Barrel`
        catalog can live in a `Shot 12` folder.

        ⚠ Cached per (path, mtime), like every other per-item read here. The
        catalog sits at the TOP level of the sidecar, not inside `metadata`,
        so `meta()`'s cache cannot serve it — and reaching for `read_data()`
        on every refilter would re-parse the whole library on each keystroke
        in the search box.
        """
        if self.type not in ASSET_KINDS:
            return ""
        key = (self.path, self.mtime)
        cached = _catalog_cache.get(key)
        if cached is None:
            try:
                cached = self.read_data().get("catalog", "") or ""
            except (OSError, ValueError):
                cached = ""
            _catalog_cache[key] = cached
            while len(_catalog_cache) > 2048:
                _catalog_cache.pop(next(iter(_catalog_cache)))
        return cached

    def meta_fast(self):
        """Just the metadata, cheap enough for a paint path.

        Reuses the full parse when a filter has already paid for it, so the two
        caches never disagree about the same file version."""
        key = (self.path, self.mtime)
        entry = _meta_cache.get(key)
        if entry is not None:
            return entry[0]
        cached = _fastmeta_cache.get(key)
        if cached is None:
            fname = DATA_FILES.get(self.type)
            cached = _peek_metadata(os.path.join(self.path, fname)) if fname else {}
            _fastmeta_cache[key] = cached
            while len(_fastmeta_cache) > 2048:
                _fastmeta_cache.pop(next(iter(_fastmeta_cache)))
        return cached

    def anim_flags(self):
        """Which ANIM_FLAGS this item carries, in draw order.

        ⚠ THE TYPE CHECK COMES FIRST, before any read — same rule, and for the
        same reason, as `bulk_count`. ⚠ An item saved before these flags existed
        has none of the keys and so gets no badges, which is right: nobody knows
        what those older files kept, and guessing "probably modifiers" would put
        a badge on items it might be wrong about."""
        if self.type != "anim" or self.bare:
            return ()
        meta = self.meta_fast()
        return tuple(flag for flag, key, _tip in ANIM_FLAGS if meta.get(key))

    @property
    def thumbnail(self):
        p = os.path.join(self.path, "thumbnail.jpg")
        return p if os.path.isfile(p) else None

    def sequence_frames(self):
        seq = os.path.join(self.path, "sequence")
        if not os.path.isdir(seq):
            return []
        return [os.path.join(seq, f) for f in sorted(os.listdir(seq))
                if f.lower().endswith((".jpg", ".png"))]

    def read_data(self):
        """Full item json (bones etc.) — only call on selection, files can be big."""
        fname = DATA_FILES.get(self.type)
        if not fname:
            return {}
        try:
            with open(os.path.join(self.path, fname), "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}


def snapshot_item(item_path):
    """Move the item's current payload into versions/vNNN (same logic as the
    add-on's core.version_item). Returns the version dir or None."""
    files = [f for f in _PAYLOAD_FILES if os.path.isfile(os.path.join(item_path, f))]
    files += [f for f in os.listdir(item_path) if f.endswith((".bin", ".abc"))]
    has_seq = os.path.isdir(os.path.join(item_path, "sequence"))
    if not files and not has_seq:
        return None
    vroot = os.path.join(item_path, VERSIONS_DIR)
    os.makedirs(vroot, exist_ok=True)
    nums = [int(d[1:]) for d in os.listdir(vroot) if _VERSION_RE.match(d)]
    vdir = os.path.join(vroot, "v%03d" % (max(nums) + 1 if nums else 1))
    os.makedirs(vdir)
    for f in files:
        shutil.move(os.path.join(item_path, f), os.path.join(vdir, f))
    if has_seq:
        shutil.move(os.path.join(item_path, "sequence"), os.path.join(vdir, "sequence"))
    return vdir


def list_versions(item):
    """Newest-first [{dir, label, created, thumbnail}] for an item."""
    vroot = os.path.join(item.path, VERSIONS_DIR)
    out = []
    if not os.path.isdir(vroot):
        return out
    data_file = DATA_FILES.get(item.type)
    for d in sorted((d for d in os.listdir(vroot) if _VERSION_RE.match(d)),
                    key=lambda d: int(d[1:]), reverse=True):
        full = os.path.join(vroot, d)
        # Head-read, not a full parse: a version of a big .anim is megabytes
        # of curves, wanted here for one `created` string.
        created = (_peek_metadata(os.path.join(full, data_file)).get("created")
                   if data_file else None)
        if not created:
            try:
                created = time.strftime("%Y-%m-%dT%H:%M:%S",
                                        time.localtime(os.path.getmtime(full)))
            except OSError:
                created = "?"
        thumb = os.path.join(full, "thumbnail.jpg")
        out.append({"dir": full, "label": d,
                    "created": created.replace("T", "  "),
                    "thumbnail": thumb if os.path.isfile(thumb) else None})
    return out


def restore_version(item, vdir):
    """Roll the item back to a version. The current payload is snapshotted as a
    new version first (nothing is ever lost); the chosen version is COPIED back
    so it stays in the history."""
    snapshot_item(item.path)
    payload = list(_PAYLOAD_FILES) + [f for f in os.listdir(vdir)
                                      if f.endswith((".bin", ".abc"))]
    for f in payload:
        src = os.path.join(vdir, f)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(item.path, f))
    seq = os.path.join(vdir, "sequence")
    if os.path.isdir(seq):
        shutil.copytree(seq, os.path.join(item.path, "sequence"))


def item_type(dirname):
    for ext in ITEM_EXTS:
        if dirname.endswith(ext):
            return ext[1:]
    return None


def scan(root):
    """Return (folders, items). folders: sorted list of library-relative paths
    (using '/'); items: list of Item. Item folders are not descended into."""
    folders = []
    items = []
    if not os.path.isdir(root):
        return folders, items
    for cur, dirs, files in os.walk(root):
        rel = os.path.relpath(cur, root)
        rel = "" if rel == "." else rel.replace("\\", "/")
        keep = []
        for d in sorted(dirs, key=str.lower):
            typ = item_type(d)
            full = os.path.join(cur, d)
            if typ:
                name = d[:-(len(typ) + 1)]
                relp = (rel + "/" + d) if rel else d
                try:
                    mtime = os.path.getmtime(full)
                except OSError:
                    mtime = 0
                items.append(Item(full, relp, rel, name, typ, mtime))
            else:
                keep.append(d)
                folders.append((rel + "/" + d) if rel else d)
        dirs[:] = keep
        # loose FILES show up as bare items: .abc caches from other tools.
        #
        # ⚠ .mp4 PLAYBLASTS DELIBERATELY DO NOT (Marty, 2026-08-05: "remove
        # 'playblasts' from showing in anim library, remove the filter too").
        # A playblast is a thing you watch once, not a library item, and a
        # folder of them buried the poses. THIS LINE IS THE WHOLE REMOVAL — the
        # type filter went with it (`panels.Sidebar.type_checks`), and the mp4
        # frame-extraction machinery (`video_preview.py`, `grid`'s playblast
        # branches, `theme.TYPE_COLORS["playblast"]`) is left in place but now
        # unreachable, so putting them back is this one `elif` again. They are
        # still findable: the playblast dialog opens the finished file, and both
        # Watch buttons play the newest one.
        for fn in sorted(files, key=str.lower):
            low = fn.lower()
            if low.endswith(".abc"):
                typ = "abc"
            else:
                continue
            full = os.path.join(cur, fn)
            relp = (rel + "/" + fn) if rel else fn
            try:
                mtime = os.path.getmtime(full)
            except OSError:
                mtime = 0
            items.append(Item(full, relp, rel, os.path.splitext(fn)[0], typ,
                              mtime, bare=True))
    folders.sort(key=str.lower)
    return folders, items
