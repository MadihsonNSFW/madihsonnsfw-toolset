"""Texture Maps — what the app needs from Blender, and nothing more.

TWO commands, and the split between them is the whole design:

  * `tex_list`   — a PURE READ. Which images exist, where their FILES are,
                   what each one is FOR, and which one the active object uses.
                   No pixels: the app opens the same files off the same disk.
  * `tex_export` — write ONE image's pixels to a PNG at a path the app names.
                   Only for images that have no file to read: packed into the
                   .blend, generated, or painted and unsaved.

⚠ **PIXELS DO NOT CROSS THE BRIDGE.** A 4096x4096 image is 50 MB of base64 in
a JSON line, assembled on Blender's main thread. The app is on the same
machine, so it reads the file. `tex_export` exists precisely because a few
images have no file — and it writes one rather than streaming bytes.

⚠ **`original` IS NOT DECORATION.** The Scene Optimizer replaces a texture's
filepath with a small STAND-IN and keeps the real path in a property. An app
generating maps from `filepath` would be reading a 512-px proxy of a 4K
texture and could not possibly tell. Every row carries `original` resolved
through `optimizer.resolve_original`, and the app prefers it.

Security note (`docs\\security.md`, "New commands since the pass"): a web page
can reach this socket, so `tex_export` is written as if a stranger were
calling it — it writes a PNG, only a PNG, only where told, refuses to overwrite
anything that is not already a PNG, and names no default path of its own.
"""
import os

import bpy

from . import optimizer

# What we will hand back a path for. Anything else the app asks us to write out.
READABLE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".bmp",
                 ".tga"}

# Which Principled input an image ends up feeding. The app sorts base colours
# first (they are what you generate maps FROM) and, later, could tell you which
# maps a material is still missing.
_SOCKET_ROLES = {
    "Base Color": "Base Color",
    "Roughness": "Roughness",
    "Metallic": "Metallic",
    "Specular": "Specular",
    "Alpha": "Alpha",
    "Emission": "Emission",
    "Emission Color": "Emission",
    "Normal": "Normal",
    "Height": "Displacement",
}

_MAX_FOLLOW = 6         # how far downstream we look for a recognisable socket


def _role_of(node, tree, depth=0):
    """What this Image Texture node's output eventually feeds.

    ⚠ Follows the links rather than reading the node's own name, because a
    node called "Roughness" can be wired anywhere and usually is. Passes
    THROUGH the two nodes that sit between a texture and its socket in nearly
    every real material — Normal Map and Displacement — and reports those by
    name, since that is what the texture is for.
    """
    if depth > _MAX_FOLLOW or node is None:
        return "other"
    for output in getattr(node, "outputs", []):
        for link in getattr(output, "links", []):
            target = link.to_node
            socket = link.to_socket
            if target is None:
                continue
            if target.type == "NORMAL_MAP":
                return "Normal"
            if target.type == "DISPLACEMENT":
                return "Displacement"
            if target.type == "BUMP":
                return "Displacement"
            name = getattr(socket, "name", "")
            if name in _SOCKET_ROLES:
                return _SOCKET_ROLES[name]
            role = _role_of(target, tree, depth + 1)
            if role != "other":
                return role
    return "other"


def _image_users(seen_limit=4000):
    """image name -> [{material, objects, role}], walked once.

    ⚠ ONE walk over the scene, not one per image. The obvious shape here is
    "for each image, find its users", which re-walks every material for every
    image — O(images x materials) on scenes where both numbers are in the
    hundreds. This builds the whole map in a single pass instead.
    """
    users = {}
    # material name -> [object names], so a material shared by six objects is
    # resolved once rather than six times
    mat_objects = {}
    for obj in bpy.data.objects:
        for slot in obj.material_slots:
            if slot.material is not None:
                mat_objects.setdefault(slot.material.name, []).append(obj.name)

    visited = set()

    def walk(tree, owner, objects=None, depth=0):
        """`owner` is what to show as the source (a material, or an object's
        modifier); `objects` is who wears it — passed in rather than looked up,
        because a geometry-nodes tree has no material name to look up BY."""
        if tree is None or depth > 8:
            return
        key = tree.as_pointer()
        if key in visited:
            return
        visited.add(key)
        if objects is None:
            objects = mat_objects.get(owner, [])
        for node in tree.nodes:
            image = getattr(node, "image", None)
            if isinstance(image, bpy.types.Image):
                users.setdefault(image.name, []).append({
                    "material": owner,
                    "objects": objects,
                    "role": _role_of(node, tree),
                })
            # A geometry-nodes image arrives on an input SOCKET and has no
            # `.image` at all — the same trap `optimizer._walk_tree` documents.
            for socket in getattr(node, "inputs", []):
                value = getattr(socket, "default_value", None)
                if isinstance(value, bpy.types.Image):
                    users.setdefault(value.name, []).append({
                        "material": owner,
                        "objects": objects,
                        "role": "other",
                    })
            walk(getattr(node, "node_tree", None), owner, objects, depth + 1)

    for material in bpy.data.materials:
        if material.use_nodes:
            visited.clear()
            walk(material.node_tree, material.name)

    # ⚠ **GEOMETRY NODES TOO.** Marty's scenes are geonode-heavy, and the
    # Optimizer learned this the hard way: an image used only by a geonode
    # tree appears in no material at all, so a material-only walk reports it
    # as belonging to nothing and the picker labels it with a blank. Both
    # routes in: the tree itself, and an image fed straight to the modifier's
    # own input (which never appears in the tree).
    for obj in bpy.data.objects:
        for mod in getattr(obj, "modifiers", []):
            if mod.type != "NODES":
                continue
            label = "%s / %s" % (obj.name, mod.name)
            visited.clear()
            walk(getattr(mod, "node_group", None), label, [obj.name])
            try:
                values = optimizer.modifier_inputs(mod)
            except Exception:                                 # noqa: BLE001
                values = []
            for value in values:
                if isinstance(value, bpy.types.Image):
                    users.setdefault(value.name, []).append({
                        "material": label,
                        "objects": [obj.name],
                        "role": "other",
                    })
    return users


def _active_image(context, users):
    """The image the active object's active material shows first.

    "Use active object" in the app is one click and no list, so this has to
    pick the RIGHT one: the base colour if there is one, else any image in
    that material.
    """
    obj = getattr(context, "active_object", None)
    if obj is None:
        return None
    material = None
    try:
        material = obj.active_material
    except AttributeError:
        material = None
    if material is None:
        for slot in getattr(obj, "material_slots", []):
            if slot.material is not None:
                material = slot.material
                break
    if material is None or not material.use_nodes:
        return None
    best = None
    for image_name, rows in users.items():
        for row in rows:
            if row.get("material") != material.name:
                continue
            if row.get("role") == "Base Color":
                return image_name
            if best is None:
                best = image_name
    return best


def tex_list(context=None):
    """Every image in the file, with where to read it and what it is for.

    PURE READ. ⚠ Called on demand — tab open, Refresh, Use active object —
    and deliberately NOT on the app's status poll: it walks every material in
    the scene, which is cheap but not free, and nothing about it changes
    between two ticks of a timer.
    """
    context = context or bpy.context
    users = _image_users()
    rows = []
    for image in bpy.data.images:
        if image.source in {"VIEWER"}:
            continue
        try:
            size = [int(image.size[0]), int(image.size[1])]
        except Exception:                                     # noqa: BLE001
            size = [0, 0]
        if size == [0, 0] and image.source == "FILE":
            # a file that has never been loaded still reports 0x0
            pass
        raw_original, original_abs = optimizer.resolve_original(image)
        filepath = optimizer._abs_path(image)
        rows.append({
            "name": image.name,
            "size": size,
            "filepath": filepath,
            # ⚠ For an image the Optimizer manages this is the REAL texture and
            # `filepath` is a small stand-in. Only ever equal for an unmanaged
            # image, which is why the app can prefer it unconditionally.
            "original": original_abs or filepath,
            "source": image.source,
            "packed": image.packed_file is not None,
            "dirty": bool(getattr(image, "is_dirty", False)),
            "colorspace": getattr(getattr(image, "colorspace_settings", None),
                                  "name", ""),
            "managed": optimizer.is_managed(image),
            "users": users.get(image.name, []),
        })
    rows.sort(key=lambda r: r["name"].lower())
    return {
        "file": bpy.data.filepath,
        "images": rows,
        "materials": len([m for m in bpy.data.materials if m.use_nodes]),
        "active": _active_image(context, users),
    }


def tex_export(image_name, path):
    """Write ONE image's current pixels to a PNG at *path*.

    ⚠ Every restriction below is deliberate; this is a command a web page can
    reach (`docs\\security.md` finding 4).

      * PNG only, enforced on the extension — not "whatever format the
        extension implies", which would make this a general image converter
        pointed at an arbitrary path.
      * It REFUSES to overwrite a file that is not already a .png, so it can
        never be aimed at someone's .blend or their source texture.
      * There is no default path. A caller must say where, so this can never
        invent a location the way an unsaved `save_blend` would have.
      * It writes a COPY's pixels; the datablock's own filepath, colour space
        and packed state are left exactly as they were.
    """
    if not image_name:
        raise RuntimeError("tex_export needs an image name")
    image = bpy.data.images.get(image_name)
    if image is None:
        raise RuntimeError("no image called %r in this file" % image_name)
    path = str(path or "")
    if not path:
        raise RuntimeError("tex_export needs a path to write to")
    path = os.path.abspath(bpy.path.abspath(path))
    if os.path.splitext(path)[1].lower() != ".png":
        raise RuntimeError("tex_export only writes .png files")
    if os.path.exists(path) and os.path.splitext(path)[1].lower() != ".png":
        raise RuntimeError("refusing to overwrite %s" % path)
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)

    width, height = int(image.size[0]), int(image.size[1])
    if width <= 0 or height <= 0:
        raise RuntimeError("%r has no pixels to write" % image_name)

    # ⚠ A COPY, and its settings, so nothing about the user's own datablock
    # moves. `save_render` writes through the scene's image settings, which is
    # why they are set on a throwaway copy rather than on the scene.
    copy = image.copy()
    try:
        copy.file_format = "PNG"
        try:
            copy.colorspace_settings.name = image.colorspace_settings.name
        except Exception:                                     # noqa: BLE001
            pass
        copy.save_render(filepath=path)
    finally:
        bpy.data.images.remove(copy)

    return {"path": path, "width": width, "height": height,
            "image": image_name}
