"""Importing things INTO a Studio Library — the backend.

Marty, 2026-08-05: *"we need have an 'Import' button somewhere in top … This
will let us import every item we can have in library now, this should be main
way users can import things to studio library — and for that reason the backend
infrastructure should be in a way that everything will just work including
thumbnails, we should also be able to import multiple files or a zip that has a
lot of different studio library items."*

So this module knows about **items**, never about item TYPES. A library item is
a folder whose name ends in one of `library.ITEM_EXTS`; importing one is
copying that folder whole. That is the whole reason "everything just works
including thumbnails" — `thumbnail.jpg`, `sequence\`, `versions\`, `tags.json`
and every payload file ride along because nothing here inspects them. A type
added tomorrow imports with no change here.

WHAT CAN BE IMPORTED
- an item folder (`Wave.anim`)
- a folder full of them, at any depth — the relative folders are preserved
- a **zip**, ours (the library's own "Zip for sharing") or one somebody made by
  zipping a library folder
- loose `.abc` / `.mp4` files, which the library already shows as **bare**
  items, so they land as themselves

⚠ **NOTHING IS EVER OVERWRITTEN.** A name that is taken gets a number, exactly
like Blender does. An import is someone else's data arriving; silently
replacing an item of your own with it is the one outcome that cannot be undone.

⚠ **A ZIP IS UNTRUSTED INPUT.** Two real traps, both handled in `_zip_entries`:
1. **Path traversal** — an entry named `..\..\Windows\evil.dll` would be
   written outside the library. Every entry is normalised and rejected if it
   escapes.
2. **Backslash entry names** — PowerShell 5.1's `Compress-Archive` writes
   `a\b\c.json` rather than `a/b/c.json`, and Python's zipfile treats that as
   ONE flat filename. A zip Marty made in PowerShell would import as garbage
   file names instead of items, so separators are normalised before anything
   looks at them.
"""

import os
import shutil
import zipfile

import library as librarymod

# Loose files the library already understands without an item folder around
# them (`scan` calls them "bare"). Keep in step with library.scan.
BARE_EXTS = (".abc", ".mp4")
ZIP_EXTS = (".zip",)


class Candidate:
    """One thing that will be imported: where it comes from, where it lands."""

    __slots__ = ("kind", "source", "member", "name", "type", "relfolder")

    def __init__(self, kind, source, name, typ, relfolder="", member=None):
        self.kind = kind            # "item" | "bare" | "zip_item" | "zip_bare"
        self.source = source        # path on disk (the zip, for zip_*)
        self.member = member        # entry prefix inside the zip
        self.name = name            # display name, no extension
        self.type = typ             # library item type, or "" for a bare file
        self.relfolder = relfolder  # folders to recreate under the destination

    @property
    def from_zip(self):
        return self.kind.startswith("zip_")

    def __repr__(self):                                     # pragma: no cover
        return "<Candidate %s %s/%s.%s>" % (self.kind, self.relfolder,
                                            self.name, self.type)


def _item_type(basename):
    for ext in librarymod.ITEM_EXTS:
        if basename.lower().endswith(ext):
            return ext[1:]
    return None


def _split_item(relpath):
    """(folder-above, item folder name, type) for a path with an item in it.

    Handles both shapes we see: our own share zips put items at the TOP level,
    while a zip of a library folder carries `Lily/Face/Smile.pose/...`.
    """
    parts = [p for p in relpath.split("/") if p not in ("", ".")]
    # ⚠ `parts[:-1]`, never the whole list: `.abc` is BOTH an item folder
    # extension and a loose-file one, so a zip entry `loose.abc` would other-
    # wise be read as an item FOLDER called "loose.abc" — and imported as an
    # empty directory of that name. Every entry here is a FILE, so a real item
    # folder is always one of the components ABOVE the last.
    for i, part in enumerate(parts[:-1]):
        typ = _item_type(part)
        if typ:
            return "/".join(parts[:i]), part, typ
    return None


def _zip_entries(path):
    """(safe relative name, ZipInfo) for every FILE in the zip.

    ⚠ Separators normalised and traversal rejected — see the module docstring.
    """
    out = []
    with zipfile.ZipFile(path) as archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            name = info.filename.replace("\\", "/")
            if name.startswith("/") or ":" in name.split("/")[0]:
                continue                     # absolute or drive-qualified
            parts = []
            for part in name.split("/"):
                if part in ("", "."):
                    continue
                if part == "..":
                    parts = None
                    break
                parts.append(part)
            if not parts:
                continue
            out.append(("/".join(parts), info))
    return out


def scan_zip(path):
    """What a zip would import: [Candidate], plus a list of ignored entries."""
    found = {}
    bare = {}
    ignored = []
    for name, _info in _zip_entries(path):
        split = _split_item(name)
        if split is not None:
            relfolder, folder, typ = split
            key = (relfolder, folder)
            if key not in found:
                found[key] = Candidate(
                    "zip_item", path, os.path.splitext(folder)[0], typ,
                    relfolder=relfolder,
                    member=(relfolder + "/" + folder if relfolder else folder))
            continue
        ext = os.path.splitext(name)[1].lower()
        if ext in BARE_EXTS:
            bare[name] = Candidate("zip_bare", path,
                                   os.path.splitext(os.path.basename(name))[0],
                                   "", relfolder=os.path.dirname(name),
                                   member=name)
        else:
            ignored.append(name)
    return list(found.values()) + list(bare.values()), ignored


def scan_folder(path):
    """Every item under a folder. The folder itself may BE an item."""
    base = os.path.basename(os.path.normpath(path))
    typ = _item_type(base)
    if typ:
        return [Candidate("item", path, os.path.splitext(base)[0], typ)], []

    out, ignored = [], []
    for root, dirs, files in os.walk(path):
        keep = []
        for d in dirs:
            dtyp = _item_type(d)
            if dtyp:
                rel = os.path.relpath(root, path).replace(os.sep, "/")
                out.append(Candidate("item", os.path.join(root, d),
                                     os.path.splitext(d)[0], dtyp,
                                     relfolder="" if rel == "." else rel))
            else:
                keep.append(d)
        dirs[:] = keep                       # never descend INTO an item
        for f in files:
            if os.path.splitext(f)[1].lower() in BARE_EXTS:
                rel = os.path.relpath(root, path).replace(os.sep, "/")
                out.append(Candidate("bare", os.path.join(root, f),
                                     os.path.splitext(f)[0], "",
                                     relfolder="" if rel == "." else rel))
    return out, ignored


def scan(paths):
    """Everything importable in a mixed list of files and folders.

    Returns (candidates, ignored) — `ignored` is [(path, why)], so the dialog
    can say what it is not going to do rather than quietly dropping it.
    """
    found, ignored = [], []
    for path in paths:
        if os.path.isdir(path):
            items, _skip = scan_folder(path)
            if items:
                found.extend(items)
            else:
                ignored.append((path, "no library items in this folder"))
            continue
        ext = os.path.splitext(path)[1].lower()
        if ext in ZIP_EXTS:
            try:
                items, skipped = scan_zip(path)
            except (zipfile.BadZipFile, OSError) as exc:
                ignored.append((path, "not a readable zip (%s)" % exc))
                continue
            if items:
                found.extend(items)
            else:
                ignored.append((path, "no library items in this zip"))
            continue
        if ext in BARE_EXTS:
            found.append(Candidate("bare", path,
                                   os.path.splitext(os.path.basename(path))[0],
                                   ""))
            continue
        ignored.append((path, "not a library item, a zip, an .abc or an .mp4"))
    return found, ignored


def _free_name(folder, base, suffix):
    """`base+suffix`, numbered if taken. Never returns an existing path."""
    target = os.path.join(folder, base + suffix)
    n = 2
    while os.path.exists(target):
        target = os.path.join(folder, "%s %d%s" % (base, n, suffix))
        n += 1
    return target


def destination(root, dest_folder, candidate):
    """Where this candidate lands — folder only, name resolved at copy time."""
    parts = [root]
    if dest_folder:
        parts.append(dest_folder.replace("/", os.sep))
    if candidate.relfolder:
        parts.append(candidate.relfolder.replace("/", os.sep))
    return os.path.join(*parts)


def run(candidates, root, dest_folder="", on_progress=None):
    """Copy/extract every candidate. Returns a report; never raises for one
    bad entry — a half-finished import must still say what it managed."""
    report = {"imported": [], "renamed": [], "failed": [], "types": {}}
    zips = {}
    try:
        for n, cand in enumerate(candidates):
            if on_progress is not None:
                on_progress(n, len(candidates), cand.name)
            folder = destination(root, dest_folder, cand)
            try:
                os.makedirs(folder, exist_ok=True)
                if cand.kind == "item":
                    suffix = "." + cand.type
                    target = _free_name(folder, cand.name, suffix)
                    shutil.copytree(cand.source, target)
                elif cand.kind == "bare":
                    suffix = os.path.splitext(cand.source)[1]
                    target = _free_name(folder, cand.name, suffix)
                    shutil.copy2(cand.source, target)
                elif cand.kind == "zip_item":
                    suffix = "." + cand.type
                    target = _free_name(folder, cand.name, suffix)
                    _extract_item(zips, cand, target)
                else:                                   # zip_bare
                    suffix = os.path.splitext(cand.member)[1]
                    target = _free_name(folder, cand.name, suffix)
                    _extract_file(zips, cand, target)
            except (OSError, shutil.Error, zipfile.BadZipFile,
                    KeyError) as exc:
                report["failed"].append({"name": cand.name,
                                         "reason": str(exc)})
                continue
            report["imported"].append(target)
            key = cand.type or os.path.splitext(target)[1].lstrip(".")
            report["types"][key] = report["types"].get(key, 0) + 1
            if os.path.basename(target) != cand.name + suffix:
                report["renamed"].append(os.path.basename(target))
    finally:
        for archive in zips.values():
            archive.close()
    report["summary"] = _summary(report)
    return report


def _archive(zips, path):
    if path not in zips:
        zips[path] = zipfile.ZipFile(path)
    return zips[path]


def _extract_item(zips, cand, target):
    """Every entry under the item's prefix, into `target`.

    Extracted by hand rather than with `ZipFile.extract`, because that resolves
    the entry's ORIGINAL name against the destination — the whole point here is
    that the item is being re-rooted, and re-normalised (see `_zip_entries`).
    """
    archive = _archive(zips, cand.source)
    prefix = cand.member + "/"
    os.makedirs(target, exist_ok=True)
    for name, info in _zip_entries(cand.source):
        if not name.startswith(prefix):
            continue
        rel = name[len(prefix):]
        out = os.path.join(target, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with archive.open(info) as src, open(out, "wb") as dst:
            shutil.copyfileobj(src, dst)


def _extract_file(zips, cand, target):
    archive = _archive(zips, cand.source)
    for name, info in _zip_entries(cand.source):
        if name == cand.member:
            with archive.open(info) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
            return
    raise KeyError(cand.member)


def _summary(report):
    n = len(report["imported"])
    if not n:
        return "Nothing imported."
    kinds = ", ".join("%d %s" % (c, t)
                      for t, c in sorted(report["types"].items()))
    text = "Imported %d item%s (%s)." % (n, "" if n == 1 else "s", kinds)
    if report["renamed"]:
        text += ("  %d renamed to avoid replacing something: %s."
                 % (len(report["renamed"]), ", ".join(report["renamed"][:3])))
    if report["failed"]:
        text += "  %d failed." % len(report["failed"])
    return text
