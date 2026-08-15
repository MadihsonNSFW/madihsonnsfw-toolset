"""Building an asset - a node group, an object and its modifiers - from a spec.

THIS MODULE IS DELIBERATELY GENERIC. It knows nothing about what it is
building: no torus, no deformer, no node names. It takes a spec and follows it.
That is the whole point - the add-on ships as readable Python, so anything it
knows is public. The recipe lives in the licensed app and arrives over the
bridge, so the add-on can be read all day without giving anything away.

⚠ It cannot make a built node group SECRET, and does not try. Once a group
exists in a user's file they can open the Geometry Node editor and read it;
Blender has no encryption, no lock, and a linked library is still a file they
hold. What this buys is that the recipe never SHIPS in readable form. Everything
it builds is ordinary and fully visible in Blender - the user tunes the rig on
its own modifier panel.

Order matters in three places, and each one is a silent failure if got wrong:
  * the interface is built BEFORE the nodes, or Group Input/Output have no
    sockets to link,
  * node properties are set BEFORE socket defaults, because data_type / domain /
    mode rebuild the socket list and throw away anything written first,
  * modifier values are carried BY SOCKET NAME, never by identifier - a rebuilt
    group gets its own Socket_N numbering and copying by identifier lands values
    on the wrong inputs without erroring.
"""

import base64
import json
import zlib

import bpy

# Blender 5.2: a geometry-nodes modifier's inputs are NOT mod[identifier] -
# that raises "this type doesn't support IDProperties". They live here, and the
# handle must be re-fetched after anything that changes the interface.
def _mod_inputs(mod):
    return mod.properties.inputs


def unpack(payload):
    """A spec as sent over the bridge: base64 of zlib of JSON."""
    if isinstance(payload, dict):
        return payload
    return json.loads(zlib.decompress(base64.b64decode(payload)).decode("utf-8"))


# ------------------------------------------------------------- node groups

def build_group(spec, name=None):
    """Rebuild a node group from its spec. Returns (group, problems)."""
    ng = bpy.data.node_groups.new(name or spec["group_name"], spec["bl_idname"])
    problems = []

    for item in spec["interface"]:
        if item.get("item_type") != "SOCKET":
            continue
        sock = ng.interface.new_socket(
            item["name"], in_out=item["in_out"], socket_type=item["socket_type"])
        for attr in ("description", "default_value", "min_value", "max_value",
                     "subtype", "default_attribute_name", "hide_value",
                     "hide_in_modifier", "force_non_field", "default_input"):
            if attr not in item or not hasattr(sock, attr):
                continue
            try:
                setattr(sock, attr, item[attr])
            except Exception as err:
                problems.append("interface %s.%s: %s" % (item["name"], attr, err))

    made = []
    for nd in spec["nodes"]:
        n = ng.nodes.new(nd["bl_idname"])
        n.name = nd["name"]
        n.label = nd.get("label", "")
        n.location = nd["location"]
        n.width = nd.get("width", n.width)
        n.hide = nd.get("hide", False)
        n.mute = nd.get("mute", False)
        for key, value in nd.get("props", {}).items():
            if isinstance(value, dict):
                continue            # an ID pointer; specs carry none today
            try:
                setattr(n, key, value)
            except Exception as err:
                problems.append("%s.%s = %r: %s" % (nd["name"], key, value, err))
        made.append(n)

    for nd, n in zip(spec["nodes"], made):
        for s in nd.get("inputs", []):
            if s["value"] is None or s["i"] >= len(n.inputs):
                continue
            sock = n.inputs[s["i"]]
            if not hasattr(sock, "default_value") or isinstance(s["value"], dict):
                continue
            try:
                sock.default_value = s["value"]
            except Exception as err:
                problems.append("%s.in[%d]: %s" % (nd["name"], s["i"], err))

    for l in spec["links"]:
        try:
            ng.links.new(made[l["from"]].outputs[l["from_socket"]],
                         made[l["to"]].inputs[l["to_socket"]])
        except Exception as err:
            problems.append("link %d->%d: %s" % (l["from"], l["to"], err))

    return ng, problems


# ------------------------------------------------------------------ meshes

def build_mesh(name, mesh_spec):
    """A mesh from flat vertex and polygon index lists."""
    verts = mesh_spec["verts"]
    polys = mesh_spec["polys"]
    size = mesh_spec.get("poly_size", 4)
    coords = [tuple(verts[i:i + 3]) for i in range(0, len(verts), 3)]
    faces = [tuple(polys[i:i + size]) for i in range(0, len(polys), size)]
    me = bpy.data.meshes.new(name)
    me.from_pydata(coords, [], faces)
    me.update()
    if mesh_spec.get("shade_smooth"):
        for p in me.polygons:
            p.use_smooth = True
    return me


# ------------------------------------------------------------------ assets

def _link(ob, collection_name):
    coll = bpy.data.collections.get(collection_name)
    if coll is None:
        coll = bpy.data.collections.new(collection_name)
        bpy.context.scene.collection.children.link(coll)
    elif coll.name not in {c.name for c in bpy.context.scene.collection.children_recursive}:
        try:
            bpy.context.scene.collection.children.link(coll)
        except RuntimeError:
            pass
    coll.objects.link(ob)
    return coll


def set_inputs(obj_name, modifier_name, values):
    """Set geometry-node modifier inputs BY SOCKET NAME.

    Returns what was actually written, so the caller can tell a silently
    ignored value from an applied one.
    """
    ob = bpy.data.objects.get(obj_name)
    if ob is None:
        raise ValueError("no object %r" % obj_name)
    mod = ob.modifiers.get(modifier_name)
    if mod is None or mod.type != 'NODES' or mod.node_group is None:
        raise ValueError("no geometry-nodes modifier %r on %r" % (modifier_name, obj_name))

    ids = {i.name: i.identifier for i in mod.node_group.interface.items_tree
           if i.item_type == "SOCKET" and i.in_out == "INPUT"}
    applied, skipped = {}, {}
    for key, value in (values or {}).items():
        ident = ids.get(key)
        if ident is None:
            skipped[key] = "no such input"
            continue
        try:
            if isinstance(value, str) and value:
                target = bpy.data.collections.get(value) or bpy.data.objects.get(value)
                if target is None:
                    skipped[key] = "no datablock named %r" % value
                    continue
                _mod_inputs(mod)[ident]["value"] = target
                applied[key] = value
            elif value is None:
                _mod_inputs(mod)[ident]["value"] = None
                applied[key] = None
            else:
                _mod_inputs(mod)[ident]["value"] = value
                applied[key] = value
        except Exception as err:
            skipped[key] = str(err)
    # A GN modifier does not re-evaluate on an input write by itself.
    ob.update_tag()
    return {"applied": applied, "skipped": skipped}


def get_inputs(obj_name, modifier_name):
    ob = bpy.data.objects.get(obj_name)
    if ob is None:
        return {}
    mod = ob.modifiers.get(modifier_name)
    if mod is None or mod.type != 'NODES' or mod.node_group is None:
        return {}
    out = {}
    for i in mod.node_group.interface.items_tree:
        if i.item_type != "SOCKET" or i.in_out != "INPUT":
            continue
        try:
            v = _mod_inputs(mod)[i.identifier]["value"]
            out[i.name] = v.name if hasattr(v, "name") else v
        except Exception:
            pass
    return out


def build_asset(payload, collection=None):
    """Create the object, its node group and its modifiers from a spec.

    The result is an ORDINARY object with an ordinary Geometry Nodes modifier -
    the group under its own name, every input where Blender puts it. There was
    briefly a "hide it" mode here (a "." name prefix, and the modifier's group
    selector and manage panel switched off); it is gone, because a rig the user
    tunes on its own modifier panel cannot have that panel taken away, and
    nothing in Blender could have made the group secret anyway.
    """
    spec = unpack(payload)
    objspec = spec["object"]
    group_spec = spec["group"]

    ng, problems = build_group(group_spec, group_spec["group_name"])

    me = build_mesh(objspec["name"], objspec["mesh"])
    ob = bpy.data.objects.new(objspec["name"], me)
    _link(ob, collection or spec.get("collection") or "MADI")

    for m in spec.get("modifiers", []):
        if m["kind"] == "DECIMATE":
            mod = ob.modifiers.new(m["name"], 'DECIMATE')
            mod.decimate_type = m.get("decimate_type", 'UNSUBDIV')
            mod.iterations = m.get("iterations", 0)
        elif m["kind"] == "NODES":
            mod = ob.modifiers.new(m["name"], 'NODES')
            mod.node_group = ng

    gn_name = next((m["name"] for m in spec.get("modifiers", []) if m["kind"] == "NODES"), None)
    written = {}
    if gn_name and spec.get("defaults"):
        written = set_inputs(ob.name, gn_name, spec["defaults"])

    return {
        "object": ob.name,
        "group": ng.name,
        "modifier": gn_name,
        "nodes": len(ng.nodes),
        "links": len(ng.links),
        "verts": len(me.vertices),
        "problems": problems,
        "defaults": written,
    }


def asset_status(obj_name, modifier_name):
    """Is the asset in the scene, and what are its inputs set to."""
    ob = bpy.data.objects.get(obj_name)
    out = {"present": ob is not None, "object": obj_name}
    if ob is None:
        # A re-added asset gets Blender's .001 suffix; find those too.
        matches = [o.name for o in bpy.data.objects
                   if o.name == obj_name or o.name.startswith(obj_name + ".")]
        out["candidates"] = matches
        return out
    mod = ob.modifiers.get(modifier_name)
    out["modifier"] = modifier_name if mod else None
    out["inputs"] = get_inputs(obj_name, modifier_name)
    out["collections"] = [c.name for c in ob.users_collection]
    out["affector_collections"] = [c.name for c in bpy.data.collections
                                   if any(o.type == 'MESH' for o in c.objects)]
    return out
