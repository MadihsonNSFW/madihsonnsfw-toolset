"""What is actually taking up room inside a .blend — exactly, not estimated.

Qt-free on purpose: this is the file reader, `optimizer.FileSizeTool` is the
window. It never opens Blender and never needs the bridge, so it works on any
.blend on disk whether or not anything is running.

HOW IT KNOWS
------------
A .blend is a header followed by a run of file-blocks. Each block carries a
BHead: a 4-character code, the SDNA index of the C struct inside it, the old
memory address, the payload length and a count. Blender writes every ID
datablock immediately followed by that datablock's own DATA blocks, so summing
DATA into the last ID block seen gives the real on-disk cost of that mesh,
image or shape key — by name, to the byte.

Each DATA block also names the struct it holds, which is how the third level
works: not just that `Eve Body` is 134 MB but that 27% of it is Surface Deform
bind data.

⚠ THE SIZES ARE UNCOMPRESSED BLOCK BYTES. Almost every .blend written today is
zstd-compressed, and the file on disk is around a third of what the blocks add
up to. Both totals are reported and the UI must show both — a single figure
that disagrees with Explorer reads as a bug.

⚠ THE 5.x FILE FORMAT IS DIFFERENT IN TWO PLACES, and both are measured from
real files rather than derived. See `read_header` and `_bhead_for`.
"""
import os
import struct
import sys

# --------------------------------------------------------------------------
# What the two-letter block codes mean. Anything not here is shown as its own
# raw code rather than guessed at.
# --------------------------------------------------------------------------
ID_NAMES = {
    "OB": "Objects", "ME": "Meshes", "MA": "Materials", "IM": "Images",
    "SC": "Scenes", "AC": "Actions", "AR": "Armatures", "NT": "Node groups",
    "KE": "Shape keys", "TE": "Textures", "WM": "Window manager",
    "SR": "Screen layouts", "SN": "Screen layouts", "BR": "Brushes",
    "GR": "Collections", "PA": "Particle settings", "CU": "Curves",
    "LA": "Lights", "CA": "Cameras", "WO": "Worlds", "SO": "Sounds",
    "VF": "Fonts", "LS": "Line styles", "PT": "Point clouds",
    "VO": "Volumes", "GD": "Grease pencil", "GP": "Grease pencil",
    "LI": "Linked libraries", "LT": "Lattices", "MB": "Metaballs",
    "PAL": "Palettes", "TX": "Texts", "MC": "Movie clips", "MSK": "Masks",
    "CF": "Cache files", "WS": "Workspaces", "LP": "Light probes",
    "HA": "Hair", "CV": "Curves", "SI": "Simulations", "IP": "Ipo (legacy)",
    "PC": "Paint curves", "GH": "Grease pencil",
}

# Blocks that are the FILE's own bookkeeping rather than anybody's datablock.
# They are reported under one heading so the percentages still add to 100.
HOUSEKEEPING = {
    "DNA1": "Structure table (DNA)",
    "TEST": "Preview thumbnail",
    "GLOB": "File header data",
    "REND": "File header data",
    "USER": "Saved preferences",
}

# --------------------------------------------------------------------------
# Plain English for the structs that actually show up big. Several structs can
# share one label and their bytes are summed under it — "Drivers" is more use
# than ChannelDriver + DriverVar + DriverTarget listed separately.
#
# Anything not named here keeps its own struct name. That is deliberate: a
# wrong friendly label is worse than an unfamiliar true one, and an unfamiliar
# name that turns out to be big is exactly the thing worth looking up.
# --------------------------------------------------------------------------
STRUCT_LABELS = {
    "raw_data": "Raw arrays (vertex, pixel and packed data)",
    "SDefBind": "Surface Deform bind data",
    "SDefVert": "Surface Deform bind data",
    "MDeformVert": "Vertex groups",
    "MDeformWeight": "Vertex groups",
    "bDeformGroup": "Vertex group names",
    "KeyBlock": "Shape key headers",
    "PackedFile": "Packed file",
    "ImagePackedFile": "Packed file",
    "ImageTile": "Image tiles",
    "FCurve": "Animation curves",
    "BezTriple": "Animation curves",
    "FPoint": "Animation curves",
    "FModifier": "Animation curves",
    "AnimData": "Animation data",
    "ChannelDriver": "Drivers",
    "DriverVar": "Drivers",
    "DriverTarget": "Drivers",
    "PointCache": "Point cache",
    "PTCacheMem": "Point cache",
    "MDisps": "Multires displacement",
    "MultiresLevel": "Multires displacement",
    "CustomDataLayer": "Mesh data layers",
    "MLoopUV": "UV maps",
    "MLoopCol": "Colour attributes",
    "MPropCol": "Colour attributes",
    "bNode": "Nodes",
    "bNodeSocket": "Node sockets",
    "bNodeLink": "Node links",
    "bNodeTree": "Node trees",
    "bPoseChannel": "Pose bones",
    "Bone": "Bones",
    "bConstraint": "Constraints",
    "IDProperty": "Custom properties",
    "IDPropertyData": "Custom properties",
    "IDPropertyUIDataFloat": "Custom properties",
    "IDPropertyUIDataInt": "Custom properties",
    "IDPropertyUIDataString": "Custom properties",
    "ParticleData": "Particles",
    "ParticleSystem": "Particles",
    "ParticleKey": "Particles",
    "HairKey": "Particles",
    "Attribute": "Attributes",
    "AttributeArray": "Attributes",
}

_CONTAINERS = {
    b"\x1f\x8b": "gzip",
    b"\x28\xb5": "zstd",
}


class BlendSizeError(Exception):
    """Anything that stops a file being read. Carries a sentence for the UI."""


def human_bytes(size):
    """Bytes as people read them. Matches the optimizer's own formatting."""
    size = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(size) < 1024.0 or unit == "TB":
            if unit == "B":
                return "%d B" % size
            return "%.1f %s" % (size, unit)
        size /= 1024.0
    return "%.1f TB" % size


def zstd_available():
    """Whether compressed files can be read at all on this machine.

    ⚠ Nearly every .blend saved today is zstd, so this being False is the
    normal-case failure, not an edge case. The tool has to say so plainly
    rather than reporting an unreadable file.
    """
    try:
        import zstandard  # noqa: F401
    except ImportError:
        return False
    return True


def _open(path):
    """(stream, raw_handle, container). `raw_handle` is the file on disk, so
    progress can be reported against a size that is actually known."""
    try:
        raw = open(path, "rb")
    except OSError as exc:
        raise BlendSizeError("Could not open that file: %s" % exc)
    magic = raw.read(4)
    raw.seek(0)
    container = _CONTAINERS.get(magic[:2])
    if container == "gzip":
        import gzip
        return gzip.open(path, "rb"), raw, "gzip"
    if container == "zstd" and magic == b"\x28\xb5\x2f\xfd":
        try:
            import zstandard
        except ImportError:
            raw.close()
            raise BlendSizeError(
                "That .blend is zstd-compressed and this build has no zstd "
                "decoder. Save it with compression turned off to read it.")
        return zstandard.ZstdDecompressor().stream_reader(raw), raw, "zstd"
    return raw, raw, None


def read_header(stream):
    """(pointer_size, endian, version, form) for either header layout.

    Legacy, 12 bytes:  ``BLENDER`` + ``_``|``-`` + ``v``|``V`` + 3-digit version
    Blender 5.x, 17:   ``BLENDER`` + ``17`` + ``-`` + ``01`` + ``v`` + ``0501``

    ⚠ The ``17`` is the header's OWN length. Reading a fixed 12 bytes puts every
    later offset five bytes out. Detect it by testing whether byte 7 is a digit,
    which no legacy file has there.
    """
    first = stream.read(12)
    if len(first) < 12 or not first.startswith(b"BLENDER"):
        raise BlendSizeError("That is not a .blend file.")
    if first[7:8].isdigit():
        total = int(first[7:9])
        head = first + stream.read(max(0, total - 12))
        pointer = 8 if head[9:10] == b"-" else 4
        endian = "<" if head[12:13] == b"v" else ">"
        return (pointer, endian, head[13:17].decode("ascii", "replace"),
                "5.x", total)
    pointer = 8 if first[7:8] == b"-" else 4
    endian = "<" if first[8:9] == b"v" else ">"
    return (pointer, endian, first[9:12].decode("ascii", "replace"),
            "legacy", 12)


def _bhead_for(form, endian, pointer):
    """The block header, which changed shape in 5.x.

    ⚠ MEASURED FROM REAL FILES, NOT DERIVED. In 5.x every field went 64-bit and
    `len` sits at **+16, AFTER the old pointer**:

        char code[4] · int32 SDNAnr · uint64 old · int64 len · int64 nr

    ⚠ Reading `len` at +8 does not fail loudly. It walks pointer values as if
    they were block codes, invents millions of nonsense datablocks — and STILL
    finishes on the last byte of the file and reports the right total. A size
    that reconciles is not evidence the walk is right; legible codes and a
    final ENDB block are.
    """
    if form == "5.x":
        return struct.Struct(endian + "4siQqq"), "new"
    return (struct.Struct(endian + ("4siQii" if pointer == 8 else "4siIii")),
            "old")


def format_version(raw):
    """'0501' -> '5.1', '305' -> '3.5'. Blender packs these as %d%02d."""
    digits = "".join(ch for ch in raw if ch.isdigit())
    if len(digits) >= 4:
        return "%d.%d" % (int(digits[:2]), int(digits[2:]))
    if len(digits) == 3:
        return "%d.%02d" % (int(digits[0]), int(digits[1:]))
    return raw or "?"


class _Dna:
    """Just enough SDNA to name a datablock and the structs inside it.

    The DNA1 block is a table of every C struct in the file: field names, type
    names, type sizes and the struct layouts. Two things are wanted from it —
    the type name for a given SDNA index (level three of the tree), and the
    byte offset of `ID.name` so an ID block can say what it is called.
    """

    def __init__(self, data, pointer, endian):
        self.pointer = pointer
        self.endian = endian
        self.names = []
        self.types = []
        self.lengths = []
        self.structs = []
        self._id_offset = None
        self._parse(data)

    def _u32(self, data, offset):
        return struct.unpack_from(self.endian + "I", data, offset)[0]

    def _strings(self, data, offset):
        count = self._u32(data, offset)
        offset += 4
        out = []
        for _index in range(count):
            end = data.index(b"\0", offset)
            out.append(data[offset:end].decode("ascii", "replace"))
            offset = end + 1
        return out, (offset + 3) & ~3

    def _parse(self, data):
        offset = 8                                  # 'SDNA' then 'NAME'
        self.names, offset = self._strings(data, offset)
        offset += 4                                 # 'TYPE'
        self.types, offset = self._strings(data, offset)
        offset += 4                                 # 'TLEN'
        self.lengths = list(struct.unpack_from(
            self.endian + "%dH" % len(self.types), data, offset))
        offset = (offset + 2 * len(self.types) + 3) & ~3
        offset += 4                                 # 'STRC'
        count = self._u32(data, offset)
        offset += 4
        for _index in range(count):
            type_index, fields = struct.unpack_from(
                self.endian + "HH", data, offset)
            offset += 4
            spec = list(struct.unpack_from(
                self.endian + "%dH" % (fields * 2), data, offset))
            offset += fields * 4
            self.structs.append((type_index, list(zip(spec[0::2],
                                                     spec[1::2]))))

    def type_name(self, index):
        """The C struct in a DATA block, or a plain label for a raw array.

        SDNA index 0 is Blender's `void`-ish slot: blocks carrying it are raw
        arrays — vertex positions, pixel data, packed files — which is usually
        where the weight actually is.
        """
        if index <= 0 or index >= len(self.structs):
            return "raw_data"
        return self.types[self.structs[index][0]]

    def _field_size(self, type_index, name):
        size = self.pointer if name.startswith(("*", "(*")) \
            else self.lengths[type_index]
        while "[" in name:
            start = name.index("[")
            end = name.index("]")
            size *= int(name[start + 1:end])
            name = name[end + 1:]
        return size

    def id_name_offset(self, struct_index):
        """Offset of `ID.name` in the struct at *struct_index*, or None.

        Only structs that literally begin with an embedded `ID id` are real
        datablocks, which is the same test Blender uses.
        """
        try:
            _type, spec = self.structs[struct_index]
        except IndexError:
            return None
        if not spec:
            return None
        first_type, first_name = spec[0]
        if self.types[first_type] != "ID" or self.names[first_name] != "id":
            return None
        if self._id_offset is None:
            self._id_offset = self._find_id_name()
        return self._id_offset

    def _find_id_name(self):
        for type_index, spec in self.structs:
            if self.types[type_index] != "ID":
                continue
            offset = 0
            for field_type, field_name in spec:
                if self.names[field_name].startswith("name["):
                    return offset
                offset += self._field_size(field_type, self.names[field_name])
            break
        return None


def scan(path, progress=None, should_cancel=None):
    """Measure *path*. Returns the tree the window renders.

    `progress(done, total)` is called with the position in the file ON DISK, so
    it is meaningful for a compressed file too. `should_cancel()` is polled the
    same way; returning True abandons the scan and raises `BlendSizeError`.
    """
    disk = os.path.getsize(path)
    stream, raw, container = _open(path)
    try:
        return _walk(stream, raw, path, disk, container, progress,
                     should_cancel)
    finally:
        try:
            stream.close()
        except Exception:               # noqa: BLE001 - a reader we are done with
            pass
        if raw is not stream and not raw.closed:
            raw.close()


def _walk(stream, raw, path, disk, container, progress, should_cancel):
    pointer, endian, version, form, header_bytes = read_header(stream)
    bhead, order = _bhead_for(form, endian, pointer)

    dna = None
    blocks = []                 # every ID block, in file order
    struct_bytes = []           # parallel: {sdna index: bytes} per ID block
    current = None
    # ⚠ The file header and the closing ENDB go in here, not just the GLOB/REND
    # blocks. Without them the parts do not add up to the whole, and for an
    # uncompressed file the whole is EXACTLY the size on disk — an invariant
    # worth keeping, because it is the one check that proves nothing was
    # skipped or double-counted.
    house = {HOUSEKEEPING["GLOB"]: header_bytes}
    stray = 0
    total = header_bytes
    reached_end = False
    ticks = 0

    while True:
        buf = stream.read(bhead.size)
        if len(buf) < bhead.size:
            break
        if order == "new":
            code, sdna, _old, length, _count = bhead.unpack(buf)
        else:
            code, length, _old, sdna, _count = bhead.unpack(buf)
        if length < 0:
            raise BlendSizeError(
                "This .blend looks damaged — a block reports a negative size.")
        name = code.rstrip(b"\0").decode("ascii", "replace")
        size = bhead.size + length
        total += size

        ticks += 1
        # ⚠ `% 4096 == 1`, not `== 0`: a small file has fewer than 4096 blocks
        # in it, and a check that first fires on block 4096 never fires at all
        # on the files most people have. This way it fires on the first block
        # and then regularly.
        if ticks % 4096 == 1:
            if should_cancel is not None and should_cancel():
                raise BlendSizeError("Cancelled.")
            if progress is not None:
                progress(min(raw.tell(), disk), disk)

        if name == "ENDB":
            reached_end = True
            label = HOUSEKEEPING["GLOB"]
            house[label] = house.get(label, 0) + size
            break

        # Only two kinds of block need their bytes read: the DNA table, and the
        # head of an ID block (where the name lives). Everything else is
        # skipped over, which is what keeps a 2 GB file at a few seconds.
        head = b""
        if name == "DNA1":
            head = stream.read(length)
        elif name == "DATA" or name in HOUSEKEEPING:
            _skip(stream, length)
        else:
            head = stream.read(min(length, 512))
            _skip(stream, length - len(head))

        if name == "DNA1":
            dna = _Dna(head, pointer, endian)
            house["Structure table (DNA)"] = \
                house.get("Structure table (DNA)", 0) + size
        elif name in HOUSEKEEPING:
            label = HOUSEKEEPING[name]
            house[label] = house.get(label, 0) + size
        elif name == "DATA":
            if current is None:
                stray += size
            else:
                blocks[current]["bytes"] += size
                counts = struct_bytes[current]
                counts[sdna] = counts.get(sdna, 0) + size
        else:
            blocks.append({"code": name, "sdna": sdna, "bytes": size,
                           "head": head})
            # The ID block's OWN struct counts as one of its parts. Without it
            # a datablock's contents add up to less than the datablock, which
            # is the kind of small lie that makes people distrust the whole
            # table.
            struct_bytes.append({sdna: size})
            current = len(blocks) - 1

    if not blocks and not house:
        raise BlendSizeError("That .blend has no readable blocks in it.")
    if not reached_end:
        # Not fatal — a truncated file still says something useful — but the
        # caller has to be able to say so rather than presenting it as whole.
        pass

    for index, entry in enumerate(blocks):
        entry["name"] = _name_of(dna, entry)
        entry["parts"] = _parts_of(dna, struct_bytes[index], entry["bytes"])
        del entry["head"]

    return _tree(blocks, house, stray, total, path, disk, container, version,
                 reached_end)


def _skip(stream, count):
    """Forward only. A zstd reader can seek forward but never back, which is
    all this walk ever does."""
    if count <= 0:
        return
    try:
        stream.seek(count, os.SEEK_CUR)
    except (AttributeError, OSError, ValueError):
        while count > 0:
            chunk = stream.read(min(count, 1 << 20))
            if not chunk:
                return
            count -= len(chunk)


def _name_of(dna, entry):
    if dna is None:
        return "(unnamed)"
    offset = dna.id_name_offset(entry["sdna"])
    head = entry["head"]
    if offset is None or len(head) < offset + 3:
        return "(unnamed)"
    # `ID.name` is the two-character type code followed by the real name.
    raw = head[offset + 2:offset + 66].split(b"\0")[0]
    return raw.decode("utf-8", "replace") or "(unnamed)"


def _parts_of(dna, counts, total):
    """Level three: what a datablock is made of, biggest first."""
    if dna is None or not counts:
        return []
    merged = {}
    for sdna, size in counts.items():
        struct_name = dna.type_name(sdna)
        label = STRUCT_LABELS.get(struct_name, struct_name)
        slot = merged.setdefault(label, {"label": label, "bytes": 0,
                                         "structs": set()})
        slot["bytes"] += size
        slot["structs"].add(struct_name)
    out = []
    for slot in sorted(merged.values(), key=lambda part: -part["bytes"]):
        out.append({"label": slot["label"], "bytes": slot["bytes"],
                    "human": human_bytes(slot["bytes"]),
                    "share": (slot["bytes"] / total) if total else 0.0,
                    "structs": sorted(slot["structs"])})
    return out


def _tree(blocks, house, stray, total, path, disk, container, version,
          complete):
    groups = {}
    for entry in blocks:
        kind = ID_NAMES.get(entry["code"], entry["code"] or "Unknown")
        group = groups.setdefault(kind, {"kind": kind, "bytes": 0,
                                         "count": 0, "items": []})
        group["bytes"] += entry["bytes"]
        group["count"] += 1
        group["items"].append(entry)

    types = []
    for group in sorted(groups.values(), key=lambda g: -g["bytes"]):
        group["items"].sort(key=lambda e: -e["bytes"])
        for entry in group["items"]:
            entry["human"] = human_bytes(entry["bytes"])
            entry["share"] = (entry["bytes"] / total) if total else 0.0
            entry.pop("sdna", None)
        group["human"] = human_bytes(group["bytes"])
        group["share"] = (group["bytes"] / total) if total else 0.0
        types.append(group)

    overhead = []
    for label, size in sorted(house.items(), key=lambda kv: -kv[1]):
        overhead.append({"label": label, "bytes": size,
                         "human": human_bytes(size),
                         "share": (size / total) if total else 0.0})
    if stray:
        overhead.append({"label": "Unattached data", "bytes": stray,
                         "human": human_bytes(stray),
                         "share": (stray / total) if total else 0.0})

    return {
        "path": path,
        "name": os.path.basename(path),
        "mtime": os.path.getmtime(path),
        "disk_bytes": disk,
        "disk_human": human_bytes(disk),
        "total_bytes": total,
        "total_human": human_bytes(total),
        "ratio": (float(total) / disk) if disk else 0.0,
        "compression": container,
        "blender": format_version(version),
        "datablocks": len(blocks),
        "complete": complete,
        "types": types,
        "overhead": overhead,
    }


if __name__ == "__main__":                      # a quick look from a terminal
    result = scan(sys.argv[1])
    print("%s — %s on disk, %s uncompressed (%s)" % (
        result["name"], result["disk_human"], result["total_human"],
        result["compression"] or "not compressed"))
    for group in result["types"][:12]:
        print("  %-22s %10s  %5.1f%%  (%d)" % (
            group["kind"], group["human"], 100.0 * group["share"],
            group["count"]))
        for item in group["items"][:3]:
            print("      %-30s %10s" % (item["name"][:30], item["human"]))
