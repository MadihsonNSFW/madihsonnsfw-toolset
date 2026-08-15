"""Local licence storage, protected with Windows DPAPI.

WHERE IT LIVES: %LOCALAPPDATA%\\MadihsonNSFW Toolset\\license.bin — deliberately
NOT next to the exe. The exe folder gets copied to another drive, zipped, moved
to a new PC; a licence must not travel with it. DPAPI additionally ties the blob
to the Windows account, so copying the file to another machine or another user
yields nothing.

This is not a hardware root of trust — the owner of a machine can always recover
their own DPAPI secrets. It is not meant to be: it costs legitimate users
nothing and it stops the file being passed around, which is the actual problem.
"""

import ctypes
import json
import os
import sys
import time

APP_FOLDER = "MadihsonNSFW Toolset"
FILE_NAME = "license.bin"

# Mixed into DPAPI so another process cannot unprotect this blob just by asking
# Windows nicely. Not a secret — a speed bump with a specific purpose.
_ENTROPY = b"MadihsonNSFW-Toolset-licence-v1"


def storage_dir():
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, APP_FOLDER)


def storage_path():
    return os.path.join(storage_dir(), FILE_NAME)


# ------------------------------------------------------------------ DPAPI

class _Blob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(data):
    buf = ctypes.create_string_buffer(data, len(data))
    return _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf


def _take(blob):
    """Copy a DPAPI output blob out and free what Windows allocated."""
    try:
        return ctypes.string_at(blob.pbData, blob.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob.pbData)


def protect(data):
    if sys.platform != "win32":
        return data
    src, _keep = _blob(data)
    ent, _keep2 = _blob(_ENTROPY)
    out = _Blob()
    ok = ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(src), None, ctypes.byref(ent), None, None, 0, ctypes.byref(out)
    )
    if not ok:
        raise OSError("CryptProtectData failed")
    return _take(out)


def unprotect(data):
    if sys.platform != "win32":
        return data
    src, _keep = _blob(data)
    ent, _keep2 = _blob(_ENTROPY)
    out = _Blob()
    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(src), None, ctypes.byref(ent), None, None, 0, ctypes.byref(out)
    )
    if not ok:
        raise OSError("CryptUnprotectData failed")
    return _take(out)


# ------------------------------------------------------------------ record

def load():
    """The stored licence record, or {} if there is none / it is unreadable.

    An unreadable blob is treated as "no licence", never as an error the user
    has to deal with: it means a different Windows account, a restored profile,
    or a corrupted file, and in every one of those cases linking again is the
    fix and the app should just offer it.
    """
    try:
        with open(storage_path(), "rb") as handle:
            raw = handle.read()
        if not raw:
            return {}
        record = json.loads(unprotect(raw).decode("utf-8"))
        return record if isinstance(record, dict) else {}
    except Exception:
        return {}


def save(record):
    """Write the record. Atomic: a crash mid-write must not eat the licence."""
    try:
        os.makedirs(storage_dir(), exist_ok=True)
        payload = protect(json.dumps(record).encode("utf-8"))
        temp = storage_path() + ".tmp"
        with open(temp, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, storage_path())
        return True
    except Exception:
        return False


def clear():
    """Unlink this machine. The seat stays claimed server-side until released —
    forgetting a token locally is not the same as giving the seat back."""
    try:
        os.remove(storage_path())
    except OSError:
        pass


def touch_clock(record):
    """Advance the clock high-water mark.

    Users set the system clock back to freeze a timer. The defence is to record
    the highest time ever seen: time can never legitimately go backwards by
    much, so a large step back is evidence, not weather. Kept inside the
    protected blob so it cannot simply be edited.
    """
    now = int(time.time())
    record["clock_high_water"] = max(int(record.get("clock_high_water") or 0), now)
    return record
