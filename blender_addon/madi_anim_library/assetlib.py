"""Blender assets stored in a Studio Library — the Blender half.

Marty, 2026-08-22: *"we also need to store assets in studio library"*, and he
picked option **C** from three mockups: the library IS a Blender asset library.
What you mark here appears in Blender's own Asset Browser; what you mark in
Blender can be pushed here. One store, two front ends.

⚠⚠ **`assets.py` IS A DIFFERENT MODULE AND A DIFFERENT MEANING OF THE WORD.**
That one builds a geometry-node rig from a spec for NSFW Tools. This one stores
Blender datablocks. They share nothing, and the collision is why this file is
`assetlib.py` — do not merge them, and do not rename either into the other's
territory.

WHAT AN ASSET ITEM IS ON DISK. The same shape as every other library item — a
folder whose name carries the type — with the payload being a real .blend:

    <library>/<folder>/<name>.object/asset.blend      ← Blender reads this
                                    /object.json      ← the app reads this
                                    /thumbnail.jpg    ← the grid draws this

⚠⚠ **THE SIDECAR EXISTS BECAUSE THE APP MUST WORK WITH BLENDER CLOSED.**
Reading which datablocks a .blend holds needs Blender. The Studio Library grid
does not have Blender — the render queue and the library browser are both
usable with it shut. So everything the grid needs is mirrored into a JSON
sidecar at save time. Blender never reads the sidecar and the app never opens
the .blend; neither can be the other's bottleneck.

⚠⚠ **NO ROUTE HERE TAKES A FILE PATH, AND THAT IS THE SECURITY PROPERTY.**
`docs\\security.md` is explicit: a web page can reach the bridge socket, so
`save_blend` takes no parameters at all and `tex_export` refuses any extension
but `.png`. Writing a `.blend` is exactly the capability that rule withholds.
The way through is the shape `save_pose` already uses — the caller names a
library root, a folder and a NAME, and this module composes everything else.
The filename (`asset.blend`) and the extension are constants in this file. A
caller cannot ask for `.exe`, cannot escape the item folder, and cannot name
the file at all.

⚠ **`asset_generate_preview()` RETURNS BEFORE THE PREVIEW EXISTS.** Measured on
5.2.0: the first read afterwards is a 128x128 image with every pixel 0, and
`preview is not None` is ALREADY TRUE — so testing for None reports success on
an empty image. It fills after a `view_layer.update()`, inside ~250 ms. Hence
`_wait_for_preview`, which gates on the PIXELS and gives up rather than writing
a transparent thumbnail.
"""

import json
import os
import time

import bpy

from . import core

# The four id types Marty picked. Key = our folder extension (without the dot)
# and the app's type key; value = (bpy.data collection, Blender's id_type enum).
KINDS = {
    "object": ("objects", "OBJECT"),
    "collection": ("collections", "COLLECTION"),
    "material": ("materials", "MATERIAL"),
    "nodegroup": ("node_groups", "NODETREE"),
}

BLEND_FILE = "asset.blend"          # ⚠ constant on purpose — see the header
THUMB_FILE = "thumbnail.jpg"
CATALOG_FILE = "blender_assets.cats.txt"

# How long a save is willing to hold Blender's main thread waiting for a
# preview to render. Measured at ~250 ms; 1.5 s is six times that and still
# short enough not to read as a freeze.
_PREVIEW_TIMEOUT = 1.5
_PREVIEW_STEP = 0.05


def _redraw():
    """⚠ A bridge write has no notifier behind it, so nothing repaints on its
    own. Appending an object that never appears is indistinguishable from an
    append that failed."""
    window_manager = bpy.context.window_manager
    if window_manager is None:
        return
    wanted = {"VIEW_3D", "OUTLINER", "PROPERTIES", "FILE_BROWSER"}
    for window in window_manager.windows:
        screen = getattr(window, "screen", None)
        if screen is None:
            continue
        for area in screen.areas:
            if area.type in wanted:
                area.tag_redraw()


def _collection_for(kind):
    attr = KINDS[kind][0]
    return getattr(bpy.data, attr)


def _find(kind, name):
    if kind not in KINDS:
        raise RuntimeError("'%s' is not an asset kind - it is one of %s"
                           % (kind, ", ".join(sorted(KINDS))))
    db = _collection_for(kind).get(name)
    if db is None:
        raise RuntimeError("there is no %s called '%s' in this file"
                           % (kind, name))
    return db


def _wait_for_preview(db):
    """True once the preview has actual pixels in it.

    ⚠ The obvious check — `db.preview is not None` — is true the instant
    `asset_generate_preview()` returns and stays true forever, whether or not
    anything was ever drawn. It is not a readiness test; it is an allocation
    test. Only the pixels know.

    ⚠⚠ **SOME KINDS NEVER GET A PREVIEW AT ALL**, and then `db.preview` is
    None rather than empty. A node group under `blender -b` is the case that
    found this: with the gate removed the very next line raised
    `'NoneType' object has no attribute 'image_size'`. Allocation is immediate
    when it happens at all, so a preview still None after the grace below is
    never coming and waiting the full timeout for it is 1.5 s spent on Blender's
    main thread for nothing.
    """
    deadline = time.time() + _PREVIEW_TIMEOUT
    grace = time.time() + _PREVIEW_STEP * 3
    while time.time() < deadline:
        bpy.context.view_layer.update()
        pv = db.preview
        if pv is None:
            if time.time() > grace:
                return False
        else:
            pixels = pv.image_pixels_float
            if len(pixels) and max(pixels) > 0.0:
                return True
        time.sleep(_PREVIEW_STEP)
    return False


def _write_thumbnail(db, path, size=256):
    """Blender's own asset preview, written out as the JPEG the grid reads.

    Returns True if a real picture landed. A preview that never rendered is
    reported as a miss rather than saved as a transparent square: the grid
    already draws a decent placeholder for an item with no thumbnail, and a
    blank JPEG would look like a broken asset instead of a pending one.
    """
    if not _wait_for_preview(db):
        return False
    pv = db.preview
    w, h = pv.image_size
    if not w or not h:
        return False
    img = bpy.data.images.new("MADI_asset_thumb", width=w, height=h,
                              alpha=True)
    try:
        # ⚠ Blender's preview pixels are top-down; an image datablock is
        # bottom-up, so the thumbnail lands upside down without the flip.
        # Flipped BEFORE assignment — assigning twice copies 65,536 floats
        # into Blender and reads them back for nothing.
        img.pixels = _flip_rows(list(pv.image_pixels_float), w, h)
        img.file_format = "JPEG"
        img.filepath_raw = path
        scene = bpy.context.scene
        was = scene.render.image_settings.quality
        scene.render.image_settings.quality = 92
        try:
            img.save_render(path, scene=scene)
        finally:
            scene.render.image_settings.quality = was
    finally:
        bpy.data.images.remove(img)
    return os.path.isfile(path)


def _flip_rows(pixels, width, height):
    row = width * 4
    out = []
    for y in range(height - 1, -1, -1):
        out.extend(pixels[y * row:(y + 1) * row])
    return out


# ------------------------------------------------------------- catalogs
def _catalog_path(library_root):
    return os.path.join(library_root, CATALOG_FILE)


def read_catalogs(library_root):
    """Blender's catalog file, parsed. Plain text by design on Blender's side,
    which is what lets the app read the same tree with Blender closed."""
    path = _catalog_path(library_root)
    rows = []
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or line.startswith("VERSION"):
                    continue
                # UUID:catalog/path/for/assets:simple catalog name
                parts = line.split(":", 2)
                if len(parts) == 3:
                    rows.append({"uuid": parts[0], "path": parts[1],
                                 "name": parts[2]})
    return {"ok": True, "root": library_root, "catalogs": rows,
            "file": path, "exists": os.path.isfile(path)}


def _ensure_catalog(library_root, catalog_path):
    """Return the uuid for `catalog_path`, appending it to the catalog file if
    Blender has never seen it. Blender writes this file itself; appending to it
    is how the Asset Browser learns a catalog we invented."""
    if not catalog_path:
        return ""
    existing = read_catalogs(library_root)["catalogs"]
    for row in existing:
        if row["path"] == catalog_path:
            return row["uuid"]
    import uuid as uuidmod
    new = str(uuidmod.uuid4())
    path = _catalog_path(library_root)
    fresh = not os.path.isfile(path)
    os.makedirs(library_root, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        if fresh:
            fh.write("# This is an Asset Catalog Definition file for Blender.\n"
                     "#\n"
                     "# Empty lines and lines starting with `#` will be "
                     "ignored.\n"
                     "# The first non-ignored line should be the version "
                     "indicator.\n"
                     '# Other lines are of the format '
                     '"UUID:catalog/path/for/assets:simple catalog name"\n'
                     "\n"
                     "VERSION 1\n"
                     "\n")
        fh.write("%s:%s:%s\n" % (new, catalog_path,
                                 catalog_path.replace("/", "-")))
    return new


# ------------------------------------------------------- the library itself
def register_library(library_root, name="MadihsonNSFW Toolset"):
    """Make Blender's Asset Browser aware of this folder.

    ⚠ Idempotent by PATH, not by name: registering the same folder twice gives
    two entries in the Asset Browser showing identical contents, and Blender
    does not stop you.
    """
    libs = bpy.context.preferences.filepaths.asset_libraries
    target = os.path.normcase(os.path.abspath(library_root))
    for lib in libs:
        if os.path.normcase(os.path.abspath(bpy.path.abspath(lib.path))) == target:
            return {"ok": True, "already": True, "name": lib.name,
                    "path": lib.path, "count": len(libs)}
    bpy.ops.preferences.asset_library_add(directory=library_root)
    added = libs[-1]
    added.name = name
    return {"ok": True, "already": False, "name": added.name,
            "path": added.path, "count": len(libs)}


def list_libraries():
    """Every asset library Blender knows, and whether it is really there.

    ⚠ A registered library whose folder is missing reports as registered.
    Marty's own "User Library" was exactly that — pointing at a
    `Documents\\Blender\\Assets` that does not exist — so `exists` is reported
    rather than assumed.
    """
    rows = []
    for lib in bpy.context.preferences.filepaths.asset_libraries:
        path = bpy.path.abspath(lib.path) if lib.path else ""
        rows.append({"name": lib.name, "path": path,
                     "exists": bool(path) and os.path.isdir(path)})
    return {"ok": True, "libraries": rows}


# ----------------------------------------------------- what can be saved
def candidates():
    """What the current selection offers, per kind — the panel's preview of
    what a save would actually take."""
    sel = [o for o in bpy.context.selected_objects]
    active_coll = None
    try:
        active_coll = bpy.context.view_layer.active_layer_collection.collection
    except Exception:
        active_coll = None

    materials, groups = [], []
    for ob in sel:
        for slot in getattr(ob, "material_slots", []):
            if slot.material and slot.material.name not in materials:
                materials.append(slot.material.name)
        for mod in getattr(ob, "modifiers", []):
            ng = getattr(mod, "node_group", None)
            if ng is not None and ng.name not in groups:
                groups.append(ng.name)

    return {
        "ok": True,
        "object": [o.name for o in sel],
        "collection": ([active_coll.name] if active_coll is not None
                       and active_coll is not bpy.context.scene.collection
                       else []),
        "material": materials,
        "nodegroup": groups,
        "active": bpy.context.active_object.name
        if bpy.context.active_object else "",
    }


def marked():
    """Datablocks in THIS file that are already assets.

    Marty's open file had 14 of these — marked once and never curated (no
    preview, no tags, an all-zero catalog id). They are the reason this route
    exists: pushing what is already marked into the library is one click, not
    a re-mark.
    """
    rows = []
    for kind, (attr, _idtype) in KINDS.items():
        for db in getattr(bpy.data, attr):
            data = getattr(db, "asset_data", None)
            if data is None:
                continue
            rows.append({
                "kind": kind, "name": db.name,
                "catalog": getattr(data, "catalog_id", "") or "",
                "author": getattr(data, "author", "") or "",
                "description": getattr(data, "description", "") or "",
                "tags": [t.name for t in data.tags],
                "has_preview": bool(db.preview is not None
                                    and len(db.preview.image_pixels_float)
                                    and max(db.preview.image_pixels_float) > 0),
            })
    return {"ok": True, "marked": rows, "count": len(rows)}


# --------------------------------------------------------------- saving
def save_asset(library_root, relfolder, name, kind, datablock,
               catalog="", author="", description="", tags=(),
               overwrite=False):
    """Mark a datablock as an asset and store it as a library item.

    ⚠ The path is composed here and nowhere else. `library_root`, `relfolder`
    and `name` are the only things a caller contributes, `name` goes through
    `core.safe_name`, and the file inside is always `asset.blend`.
    """
    db = _find(kind, datablock)

    item_dir = os.path.join(library_root, relfolder,
                            core.safe_name(name) + "." + kind)
    if os.path.isdir(item_dir):
        if not overwrite:
            raise RuntimeError("Item already exists: %s (use overwrite)"
                               % item_dir)
        core.version_item(item_dir)
    os.makedirs(item_dir, exist_ok=True)

    was_marked = db.asset_data is not None
    if not was_marked:
        db.asset_mark()
    data = db.asset_data
    if author:
        data.author = author
    if description:
        data.description = description
    for tag in tags:
        if tag and tag not in [t.name for t in data.tags]:
            data.tags.new(tag)
    if catalog:
        data.catalog_id = _ensure_catalog(library_root, catalog)

    db.asset_generate_preview()
    thumb = _write_thumbnail(db, os.path.join(item_dir, THUMB_FILE))

    blend_path = os.path.join(item_dir, BLEND_FILE)
    # fake_user so the datablock survives in a file where nothing points at it
    # — without it a linked-and-dropped asset can be purged on the next load.
    bpy.data.libraries.write(blend_path, {db}, fake_user=True)

    payload = {
        "type": kind,
        "format": core.FORMAT_VERSION,
        "metadata": {"author": author or "", "description": description or ""},
        # ⚠ The DATABLOCK name, kept separately from the item name. They are
        # allowed to differ — the item is what Marty typed, the datablock is
        # what Blender will show in its own Asset Browser — and code that
        # assumes they match will append the wrong thing.
        "datablock": db.name,
        "kind": kind,
        "id_type": KINDS[kind][1],
        "catalog": catalog or "",
        "tags": list(tags),
        "blend": BLEND_FILE,
        "was_already_marked": was_marked,
    }
    with open(os.path.join(item_dir, kind + ".json"), "w",
              encoding="utf-8") as fh:
        json.dump(payload, fh, indent=1)

    return {"ok": True, "path": item_dir, "datablock": db.name, "kind": kind,
            "thumbnail": thumb, "bytes": os.path.getsize(blend_path),
            "was_already_marked": was_marked,
            "catalog": catalog or ""}


# -------------------------------------------------------------- applying
def apply_asset(item_path, link=False, reuse=True):
    """Bring a stored asset into the open file.

    `link` False appends a copy (the default, and what a double-click does);
    True links it, so the library file stays the source of truth.

    ⚠ `reuse` is Blender's own `reuse_local_id`: with it on, an append that
    brings a material already present by name uses the local one instead of
    making `Wet skin.001`. Checked against 5.2 rather than assumed — it is a
    keyword of `bpy.data.libraries.load`, so no operator and no UI context is
    needed. `clear_asset_data` is the other half: Blender's own drag-drop
    un-marks an APPENDED copy, otherwise every append quietly adds a duplicate
    to the Asset Browser. A LINKED one keeps it, because it is not local.

    ⚠⚠ **THIS IS THE ONE ROUTE THAT TAKES A PATH**, which `apply_pose` has
    always done too. Loading a `.blend` is a bigger capability than reading a
    JSON, so it is narrowed rather than trusted: the path must be a folder
    named `<something>.<one of our four kinds>` AND hold both `asset.blend`
    and that kind's sidecar. A caller cannot point it at an arbitrary .blend
    on disk, only at something shaped exactly like an item this tool wrote.
    """
    folder = os.path.basename(os.path.normpath(item_path))
    ext = folder.rsplit(".", 1)[-1] if "." in folder else ""
    if ext not in KINDS:
        raise RuntimeError(
            "'%s' is not an asset item - an item folder ends in %s"
            % (folder, " / ".join("." + k for k in sorted(KINDS))))
    blend_path = os.path.join(item_path, BLEND_FILE)
    if not os.path.isfile(blend_path):
        raise RuntimeError("%s holds no %s" % (item_path, BLEND_FILE))
    sidecar_path = os.path.join(item_path, ext + ".json")
    if not os.path.isfile(sidecar_path):
        raise RuntimeError("%s has no %s.json - it was not saved by this tool"
                           % (item_path, ext))
    with open(sidecar_path, "r", encoding="utf-8") as fh:
        sidecar = json.load(fh)

    kind = sidecar.get("kind", ext)
    if kind != ext:
        raise RuntimeError(
            "the folder says '%s' and the sidecar says '%s' - one of them was "
            "edited by hand" % (ext, kind))
    attr = KINDS[kind][0]
    wanted = sidecar["datablock"]

    before = set(getattr(bpy.data, attr).keys())
    # ⚠ `reuse_local_id` is REFUSED alongside link: Blender raises
    # "`link` must be False if `reuse_local_id` is True". It is not an
    # oversight — reusing a local copy is the opposite of linking, so the two
    # cannot both be asked for. Dropped rather than errored, because a caller
    # asking to link has already said what it wants.
    load_kwargs = {"link": True} if link else {
        "link": False, "reuse_local_id": bool(reuse), "clear_asset_data": True}
    with bpy.data.libraries.load(blend_path, assets_only=True,
                                 **load_kwargs) as (src, dst):
        available = list(getattr(src, attr))
        if wanted not in available:
            raise RuntimeError(
                "'%s' is not in %s - it holds %s. The sidecar and the .blend "
                "disagree, which means one of them was edited by hand."
                % (wanted, BLEND_FILE, ", ".join(available) or "nothing"))
        setattr(dst, attr, [wanted])
    after = set(getattr(bpy.data, attr).keys())
    landed = sorted(after - before)

    linked_name = landed[0] if landed else wanted
    db = getattr(bpy.data, attr).get(linked_name)

    # An object or a collection has to reach the SCENE to be visible; a
    # material and a node group do not, and forcing them in would litter it.
    placed = False
    if kind == "object" and db is not None:
        bpy.context.scene.collection.objects.link(db)
        placed = True
    elif kind == "collection" and db is not None:
        bpy.context.scene.collection.children.link(db)
        placed = True

    _redraw()
    return {"ok": True, "kind": kind, "datablock": linked_name,
            "linked": bool(link), "placed": placed,
            "renamed": linked_name != wanted,
            "path": item_path}
