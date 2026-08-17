"""Pack the Blender add-on into the app, so it can be installed from there.

Run by `app\\build_exe.ps1` BEFORE PyInstaller, straight from the add-on source
folder. That ordering is the point: the exe can never ship an add-on zip that
is older than the source it was built from, because there is no separate
"remember to rebuild the zip" step to forget.

    python tools\\pack_addon.py

Writes app\\addon_bundle.py. Do not hand-edit it.
"""
import base64
import hashlib
import io
import os
import re
import textwrap
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ADDON = os.path.join(ROOT, "blender_addon", "madi_anim_library")
OUT = os.path.join(ROOT, "app", "addon_bundle.py")


def build_zip():
    """The extension zip, exactly as Blender wants it.

    Manifest at the ROOT, never __pycache__, and FORWARD-SLASH entry names -
    PowerShell's Compress-Archive writes backslashes and the result extracts
    flat on anything that is not Windows.

    ⚠ **THIS WALKS THE FOLDER. It used to be `os.listdir(...)` filtered to
    `.py`**, which meant a subdirectory was invisible to it - and on 2026-08-13
    that silently shipped an add-on with **a whole subfolder missing**: 19 files
    where there should have been 47, and the install would have reported the
    dependency absent on a machine that had never seen the source. Same lesson
    the release bundler learned twice. **Never enumerate what you can walk.**
    """
    names = []
    for folder, dirs, files in os.walk(ADDON):
        dirs[:] = sorted(d for d in dirs if d != "__pycache__")
        rel = os.path.relpath(folder, ADDON)
        for name in sorted(files):
            if name.endswith((".pyc", ".pyo")):
                continue
            names.append(name if rel == "." else
                         "/".join(rel.split(os.sep) + [name]))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name in names:
            path = os.path.join(ADDON, *name.split("/"))
            # Fixed timestamp: the same source must pack to the same bytes, or
            # every build looks like a new add-on to anything comparing hashes.
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            # The engine programs must stay executable once extracted on a
            # platform that has a permission bit at all.
            info.external_attr = (0o755 if name.endswith(".exe")
                                  else 0o644) << 16
            with open(path, "rb") as fh:
                z.writestr(info, fh.read())
    return buf.getvalue(), names


def _check_built_zip(data, version):
    """The last gate: read the manifest back OUT of the finished zip.

    Checking the source folder proves the source is right. This proves the
    thing that ships is - the manifest is at the root where Blender looks for
    it, it survived being written, and it carries the version everything else
    is about to be labelled with. Cheap, and it closes the gap between "what we
    checked" and "what we send".
    """
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        if "blender_manifest.toml" not in z.namelist():
            raise SystemExit("the packed zip has no blender_manifest.toml at "
                             "its root - Blender would refuse it")
        raw = z.read("blender_manifest.toml")
    if raw[:3] == b"\xef\xbb\xbf":
        raise SystemExit("the packed manifest carries a UTF-8 BOM")
    found = re.search(r'^\s*version\s*=\s*"([^"]+)"', raw.decode("utf-8"), re.M)
    if not found or found.group(1) != version:
        raise SystemExit("the packed manifest says %r, not %r"
                         % (found and found.group(1), version))


def addon_version():
    """The version, from a manifest that has been CHECKED FIRST.

    ⚠⚠ **THE BUILD MUST FAIL RATHER THAN SHIP A MANIFEST BLENDER CANNOT READ,
    and that is why this raises instead of coping.** On 2026-08-14 the 0.45.0
    manifest was re-saved with a UTF-8 BOM. This function still found the
    version - the regex neither knew nor cared - so the pack, the bundle and
    the release all reported 0.45.0 while the zip was one Blender refuses at
    line 1, column 1. Nothing downstream can catch that: the hash matches, the
    files are all present, and `package_install_files` returns {'FINISHED'}
    having installed nothing. Three bytes, no error, an evening gone.

    A build that stops here costs a minute. A build that ships costs everyone
    who installs it.
    """
    path = os.path.join(ADDON, "blender_manifest.toml")
    with open(path, "rb") as fh:
        raw = fh.read()

    if raw[:3] == b"\xef\xbb\xbf":
        raise SystemExit(
            "blender_manifest.toml starts with a UTF-8 BOM.\n"
            "  Blender parses it with tomllib, which refuses one: the install\n"
            "  fails with \"Invalid statement (at line 1, column 1)\" and\n"
            "  silently does nothing.\n"
            "  FIX: re-save %s as UTF-8 WITHOUT a byte-order mark\n"
            "  (PowerShell's Set-Content and Out-File both add one)." % path)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SystemExit("blender_manifest.toml is not valid UTF-8: %s" % exc)

    # Parse it the way Blender will, when this interpreter can.
    try:
        import tomllib
    except ImportError:
        tomllib = None
    if tomllib is not None:
        try:
            parsed = tomllib.loads(text)
        except Exception as exc:                            # noqa: BLE001
            raise SystemExit("blender_manifest.toml is not valid TOML "
                             "(Blender would refuse it): %s" % exc)
        for key in ("schema_version", "id", "version", "type"):
            if not parsed.get(key):
                raise SystemExit("blender_manifest.toml declares no %r" % key)
        return parsed["version"]

    m = re.search(r'^\s*version\s*=\s*"([^"]+)"', text, re.M)
    if not m:
        raise SystemExit("no version in blender_manifest.toml")
    return m.group(1)


def main():
    data, names = build_zip()
    version = addon_version()
    # And once more against the ZIP THAT WAS ACTUALLY BUILT, because that is
    # the artefact Blender receives - not the folder it was built from.
    _check_built_zip(data, version)
    digest = hashlib.sha256(data).hexdigest()
    # Already-compressed bytes; zlib on top buys nothing, so this is plain
    # base64 of the zip.
    packed = base64.b64encode(data).decode("ascii")

    text = '''"""The Blender add-on, carried inside the app so it can be installed from it.

Generated by tools\\\\pack_addon.py from blender_addon\\\\madi_anim_library - run by
build_exe.ps1 before every build, so this can never be older than the source.
DO NOT HAND-EDIT.

Why the app carries it at all: updating the extension used to mean a published
release, and the first install cannot come over the bridge anyway - an add-on
too old to have `addon_update` cannot be told to update itself. Shipping the zip
means the app can always offer the matching add-on, with no server involved.
"""

import base64
import hashlib

VERSION = "%s"
SHA256 = "%s"
FILES = %r

_PACKED = (
''' % (version, digest, names)
    for line in textwrap.wrap(packed, 96):
        text += '    "%s"\n' % line
    text += ''')


def zip_bytes():
    """The extension zip. Verified against its own hash on the way out, so a
    corrupted build is caught here rather than by Blender refusing to install."""
    data = base64.b64decode(_PACKED)
    if hashlib.sha256(data).hexdigest() != SHA256:
        raise ValueError("the packed add-on does not match its hash")
    return data


def file_name():
    return "madi_anim_library-%s.zip" % VERSION
'''

    with open(OUT, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    print("packed add-on %s (%d files, %.0f KB zip -> %.0f KB base64)"
          % (version, len(names), len(data) / 1024, len(packed) / 1024))
    print("  sha256 %s" % digest[:16])
    print("  -> %s" % OUT)


if __name__ == "__main__":
    main()
