"""Machine fingerprint components.

Only HASHES leave this machine — the server stores and compares hashes and never
sees a serial number. Missing components are normal (a VM often has no
baseboard serial); the server scores what it gets against what that machine can
report, so a partial fingerprint is not a lockout.

Two deliberate choices:

* **SMBIOS is read by raw firmware-table parse**, not WMI. WMI is the path every
  tutorial uses and therefore the path every spoofer hooks; it also spawns a
  service call that can block for seconds. `GetSystemFirmwareTable` is a single
  in-process call.
* **No MAC address, at all.** Virtual NICs from VPNs, Hyper-V and VirtualBox
  appear and vanish on their own and are the biggest single cause of false
  lockouts. Not collecting it removes the failure mode instead of managing it.

Physical disk serial is not collected either — it needs an IOCTL against
\\\\.\\PhysicalDrive0, and the four components here already total 90 of the
server's 100 points, enough that every realistic upgrade path still matches.
The server keeps the slot open if it is ever wanted.
"""

import ctypes
import hashlib
import os
import socket
import sys

_SALT = "madi-fp-v1"

# SMBIOS structure types we read.
_TYPE_SYSTEM = 1  # System Information  -> UUID at offset 0x08
_TYPE_BOARD = 2  # Baseboard Information -> Serial Number (string index 0x07)

_cache = None


def _hash(key, value):
    """Salted, truncated SHA-256. Matches the server's accepted character set."""
    return hashlib.sha256(("%s|%s|%s" % (_SALT, key, value)).encode("utf-8")).hexdigest()[:32]


# --------------------------------------------------------------- SMBIOS

def _raw_smbios():
    """The firmware's SMBIOS table, or None."""
    try:
        kernel32 = ctypes.windll.kernel32
        provider = 0x52534D42  # 'RSMB'
        size = kernel32.GetSystemFirmwareTable(provider, 0, None, 0)
        if not size:
            return None
        buf = ctypes.create_string_buffer(size)
        got = kernel32.GetSystemFirmwareTable(provider, 0, buf, size)
        if not got or got > size:
            return None
        # RawSMBIOSData header is 8 bytes; the table follows it.
        return buf.raw[8:got]
    except Exception:
        return None


def _walk(table):
    """Yield (type, formatted_bytes, [strings]) for each SMBIOS structure."""
    i = 0
    n = len(table)
    while i + 4 <= n:
        stype = table[i]
        length = table[i + 1]
        if length < 4 or i + length > n:
            return
        formatted = table[i : i + length]
        j = i + length
        strings = []
        # The string table is NUL-separated and ends with an empty string.
        if j + 1 < n and table[j] == 0 and table[j + 1] == 0:
            j += 2
        else:
            while j < n:
                end = table.find(b"\x00", j)
                if end < 0:
                    return
                if end == j:  # empty string terminates the table
                    j = end + 1
                    break
                strings.append(table[j:end].decode("latin-1", "replace"))
                j = end + 1
        if stype == 127:  # end-of-table
            return
        yield stype, formatted, strings
        i = j


def _string_at(formatted, strings, offset):
    """SMBIOS stores text as a 1-based index into the structure's string list."""
    if offset >= len(formatted):
        return None
    index = formatted[offset]
    if index == 0 or index > len(strings):
        return None
    value = strings[index - 1].strip()
    return value or None


def _smbios_values():
    table = _raw_smbios()
    out = {}
    if not table:
        return out
    try:
        for stype, formatted, strings in _walk(table):
            if stype == _TYPE_SYSTEM and len(formatted) >= 0x18 and "smbios_uuid" not in out:
                uuid = formatted[0x08:0x18]
                # All-zero or all-FF is the firmware saying "not set".
                if uuid != b"\x00" * 16 and uuid != b"\xff" * 16:
                    out["smbios_uuid"] = uuid.hex()
            elif stype == _TYPE_BOARD and "board_serial" not in out:
                serial = _string_at(formatted, strings, 0x07)
                # OEMs ship these placeholders in bulk; they identify nothing.
                if serial and serial.lower() not in (
                    "none", "n/a", "to be filled by o.e.m.", "default string",
                    "not specified", "not applicable", "0", "123456789",
                ):
                    out["board_serial"] = serial
    except Exception:
        pass
    return out


# ------------------------------------------------------- registry + volume

def _machine_guid():
    """Per-Windows-install GUID. Changes on reinstall, nothing else."""
    try:
        import winreg

        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Cryptography",
            0,
            winreg.KEY_READ | winreg.KEY_WOW64_64KEY,
        )
        try:
            value, _ = winreg.QueryValueEx(key, "MachineGuid")
            return str(value).strip() or None
        finally:
            winreg.CloseKey(key)
    except Exception:
        return None


def _volume_serial():
    """Serial of the system volume. Cheap, weak, and free."""
    try:
        root = os.environ.get("SystemDrive", "C:") + "\\"
        serial = ctypes.c_ulong(0)
        ok = ctypes.windll.kernel32.GetVolumeInformationW(
            ctypes.c_wchar_p(root), None, 0,
            ctypes.byref(serial), None, None, None, 0,
        )
        return "%08x" % serial.value if ok and serial.value else None
    except Exception:
        return None


# ------------------------------------------------------------------ public

def components(refresh=False):
    """The hashed fingerprint components for this machine.

    Cached: the underlying calls are milliseconds, but this is read on every
    licence check and there is no reason to hit the firmware repeatedly.
    """
    global _cache
    if _cache is not None and not refresh:
        return dict(_cache)

    raw = {}
    if sys.platform == "win32":
        raw.update(_smbios_values())
        guid = _machine_guid()
        if guid:
            raw["machine_guid"] = guid
        volume = _volume_serial()
        if volume:
            raw["volume_serial"] = volume

    _cache = {key: _hash(key, value) for key, value in raw.items()}
    return dict(_cache)


def label():
    """A human name for this PC, so support can say WHICH machine holds the seat."""
    try:
        name = os.environ.get("COMPUTERNAME") or socket.gethostname()
        return str(name)[:64] or "this PC"
    except Exception:
        return "this PC"


def describe():
    """Which components were readable — for the developer console, not the UI."""
    got = components()
    return "fingerprint: %s" % (", ".join(sorted(got)) or "NOTHING READABLE")
