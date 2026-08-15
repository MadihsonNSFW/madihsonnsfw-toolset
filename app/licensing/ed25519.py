"""Ed25519 signature verification, pure Python, standard library only.

Verify only — this side never signs anything. Roughly 30 ms per check on a
desktop, and the app does one a week, so speed is a non-issue.

WHY NOT A LIBRARY: the app's venv is PySide6 + psutil and adding a crypto wheel
would put a new native DLL inside an unsigned exe that reads hardware IDs and
phones home. That is a textbook Defender/SmartScreen detection, and it would
also be one more PyInstaller hidden-import to get wrong. Everything here is
hashlib plus integer arithmetic.

Follows RFC 8032 section 6 (extended homogeneous coordinates) — the same shape
as the reference implementation, so it is checkable against the RFC's own test
vectors. `tests\\lic_crypto_test.py` does exactly that.
"""

import hashlib

# Curve constants (RFC 8032, section 5.1).
P = 2 ** 255 - 19
L = 2 ** 252 + 27742317777372353535851937790883648493
D = -121665 * pow(121666, P - 2, P) % P
SQRT_M1 = pow(2, (P - 1) // 4, P)

# Points are (X, Y, Z, T) with x = X/Z, y = Y/Z, x*y = T/Z. Working projective
# keeps modular inversions out of the inner loop — the naive affine version is
# ~50x slower because every addition inverts.
_IDENTITY = (0, 1, 1, 0)


def _recover_x(y, sign):
    """The x that goes with this y, or None if the point is not on the curve."""
    if y >= P:
        return None
    xx = (y * y - 1) * pow(D * y * y + 1, P - 2, P)
    x = pow(xx, (P + 3) // 8, P)
    if (x * x - xx) % P != 0:
        x = x * SQRT_M1 % P
    if (x * x - xx) % P != 0:
        return None
    if x % 2 != sign:
        x = P - x
    return x


def _add(p, q):
    a = (p[1] - p[0]) * (q[1] - q[0]) % P
    b = (p[1] + p[0]) * (q[1] + q[0]) % P
    c = 2 * p[3] * q[3] * D % P
    e = 2 * p[2] * q[2] % P
    f, g, h, i = b - a, e - c, e + c, b + a
    return (f * g % P, h * i % P, g * h % P, f * i % P)


def _mul(point, scalar):
    """Double-and-add. Iterative on purpose: a recursive version needs ~512
    frames for a 512-bit hash scalar, which is uncomfortably close to Python's
    default recursion limit."""
    result = _IDENTITY
    while scalar > 0:
        if scalar & 1:
            result = _add(result, point)
        point = _add(point, point)
        scalar >>= 1
    return result


_BY = 4 * pow(5, P - 2, P) % P
_BX = _recover_x(_BY, 0)
BASE = (_BX, _BY, 1, _BX * _BY % P)


def _equal(p, q):
    """x1/z1 == x2/z2 and y1/z1 == y2/z2, without dividing."""
    if (p[0] * q[2] - q[0] * p[2]) % P != 0:
        return False
    return (p[1] * q[2] - q[1] * p[2]) % P == 0


def _is_small_order(point):
    """True if the point has order dividing 8.

    There are eight such points on this curve, and they are the reason a
    zeroed key with a zeroed signature otherwise verifies about a quarter of
    the time: [h]A collapses to something that cancels R for a whole class of
    message hashes. RFC 8032's basic scheme does not require this check;
    libsodium does it, and so do we, because "all zeros is a valid licence"
    is not a sentence anyone wants to read later.
    """
    doubled = _add(point, point)
    doubled = _add(doubled, doubled)
    doubled = _add(doubled, doubled)
    return _equal(doubled, _IDENTITY)


def _decompress(data):
    if len(data) != 32:
        return None
    value = int.from_bytes(data, "little")
    sign = value >> 255
    y = value & ((1 << 255) - 1)
    x = _recover_x(y, sign)
    if x is None:
        return None
    return (x, y, 1, x * y % P)


def verify(public_key, message, signature):
    """True if *signature* is a genuine Ed25519 signature over *message*.

    public_key: raw 32 bytes. signature: raw 64 bytes. message: bytes.
    Never raises — a malformed key or signature is simply not a valid one.
    """
    try:
        if len(public_key) != 32 or len(signature) != 64:
            return False
        a = _decompress(public_key)
        if a is None or _is_small_order(a):
            return False
        r = _decompress(signature[:32])
        if r is None or _is_small_order(r):
            return False
        s = int.from_bytes(signature[32:], "little")
        # A scalar at or above the group order is a malleability trick, not a
        # signature. RFC 8032 requires this check.
        if s >= L:
            return False
        h = int.from_bytes(
            hashlib.sha512(signature[:32] + public_key + message).digest(), "little"
        ) % L
        return _equal(_mul(BASE, s), _add(r, _mul(a, h)))
    except Exception:
        return False
