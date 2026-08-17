"""Texture Maps — the reference implementation. The shaders' oracle.

⚠ **THIS IS NOT DEAD CODE AND IT IS NOT A FALLBACK.** It is the second
implementation that makes the first one testable. A shader can only be checked
against something that computed the same answer a different way; without this,
`app_texmaps_test.py` could only assert that a render "looks like a map",
which is the kind of check that stays green while the maths rots.

Deliberately:
  * **pure Python, no numpy, no Qt** — so it runs anywhere, including the
    offscreen suite where there is no GL context at all;
  * **written from the algorithm, not from the GLSL** — a transcription would
    reproduce a mistake on both sides and prove nothing;
  * **used on tiny images** (16x16 to 64x64). It is O(pixels) in Python and AO
    is 36 taps each: a 64x64 AO is 147 456 taps, which is a fraction of a
    second, and a 1024x1024 one would be minutes. The suite never asks for a
    big one.

The maths, once, in words — the shaders in `texmaps_gl.py` implement exactly
this and the test asserts they agree within 2/255:
  * every map works on **display-space tone**, never linearised;
  * luma is Rec.709 `0.2126 R + 0.7152 G + 0.0722 B`;
  * every neighbour tap is **clamped to the edge**, never wrapped;
  * "smoothing / blur = N px" is a 3x3 box mean with taps N texels apart;
  * `contrast(v, c) = (v - 0.5)(1 + c) + 0.5`;
  * `levels(v, b, w, g) = clamp(((v - b)/(w - b)) ** (1/g))`.
"""
import math

from texmaps_gl import MAPS, defaults_for

LUMA = (0.2126, 0.7152, 0.0722)


def clamp(v, lo=0.0, hi=1.0):
    return lo if v < lo else (hi if v > hi else v)


def smoothstep(edge0, edge1, x):
    if edge1 == edge0:
        return 0.0
    t = clamp((x - edge0) / (edge1 - edge0))
    return t * t * (3.0 - 2.0 * t)


class Image(object):
    """A tiny float RGB image. `pixels` is a list of (r, g, b) rows."""

    def __init__(self, width, height, pixels):
        self.width = width
        self.height = height
        self.pixels = pixels

    @classmethod
    def from_qimage(cls, qimage):
        """Only used by the test, to feed the SAME bytes to both sides."""
        from PySide6.QtGui import QImage
        img = qimage.convertToFormat(QImage.Format_RGB888)
        rows = []
        for y in range(img.height()):
            data = bytes(img.constScanLine(y))[:img.width() * 3]
            rows.append([(data[x * 3] / 255.0, data[x * 3 + 1] / 255.0,
                          data[x * 3 + 2] / 255.0) for x in range(img.width())])
        return cls(img.width(), img.height(), rows)

    def rgb(self, x, y):
        x = 0 if x < 0 else (self.width - 1 if x >= self.width else x)
        y = 0 if y < 0 else (self.height - 1 if y >= self.height else y)
        return self.pixels[y][x]

    def lum(self, x, y):
        r, g, b = self.rgb(x, y)
        return LUMA[0] * r + LUMA[1] * g + LUMA[2] * b


def levels(v, black, white, gamma):
    span = max(1e-5, white - black)
    return clamp(clamp((v - black) / span) ** (1.0 / max(gamma, 1e-3)))


def contrast(v, c):
    return (v - 0.5) * (1.0 + c) + 0.5


def box(sample, x, y, n):
    """3x3 mean of `sample(x, y)` with taps n pixels apart.

    ⚠ **Taps are ROUNDED to whole pixels; the shader samples with bilinear
    filtering.** For a whole-number spacing the two are identical (the tap
    lands on a texel centre), and for a fractional one they differ by however
    much the image changes between neighbours. That is not a bug in either —
    it is why `app_texmaps_test.py` cross-checks on whole-number spacings and
    on smooth images, and says so where it does.
    """
    if n <= 0:
        return sample(x, y)
    step = int(round(n))
    if step <= 0:
        return sample(x, y)
    total = 0.0
    for j in (-1, 0, 1):
        for i in (-1, 0, 1):
            total += sample(x + i * step, y + j * step)
    return total / 9.0


def _params(key, over):
    p = dict(defaults_for(key))
    p.update(over or {})
    return p


# --------------------------------------------------------------- the maps

def normal(image, params=None):
    """Returns rows of (r, g, b) floats."""
    p = _params("normal", params)
    scharr = int(p["operator"]) == 1
    w1, w2 = (3.0, 10.0) if scharr else (1.0, 2.0)
    k = 1.0 / (2.0 * w1 + w2)
    strength = p["strength"]
    sy = 1.0 if p["invert_y"] else -1.0

    def grad(x, y, step):
        s = max(1, int(round(step)))
        tl, t, tr = (image.lum(x - s, y - s), image.lum(x, y - s),
                     image.lum(x + s, y - s))
        left, right = image.lum(x - s, y), image.lum(x + s, y)
        bl, b, br = (image.lum(x - s, y + s), image.lum(x, y + s),
                     image.lum(x + s, y + s))
        dx = ((w1 * tr + w2 * right + w1 * br)
              - (w1 * tl + w2 * left + w1 * bl)) * k
        dy = ((w1 * bl + w2 * b + w1 * br)
              - (w1 * tl + w2 * t + w1 * tr)) * k
        return dx, dy

    out = []
    for y in range(image.height):
        row = []
        for x in range(image.width):
            step = p["detail"]
            dx, dy = grad(x, y, step)
            if p["blur"] > 0:
                wx, wy = grad(x, y, step * (1.0 + p["blur"]))
                mix = clamp(p["blur"])
                dx, dy = dx + (wx - dx) * mix, dy + (wy - dy) * mix
            if p["sharpen"] > 0:
                wx, wy = grad(x, y, step * 2.0)
                dx += (dx - wx) * p["sharpen"]
                dy += (dy - wy) * p["sharpen"]
            nx, ny, nz = -dx * strength, sy * dy * strength, 1.0
            length = math.sqrt(nx * nx + ny * ny + nz * nz) or 1.0
            nx, ny, nz = nx / length, ny / length, nz / length
            row.append(tuple(
                levels(c * 0.5 + 0.5, p["black"], p["white"], p["gamma"])
                for c in (nx, ny, nz)))
        out.append(row)
    return out


def height(image, params=None):
    p = _params("height", params)
    out = []
    for y in range(image.height):
        row = []
        for x in range(image.width):
            v = box(image.lum, x, y, p["smoothing"])
            v = contrast(v, p["contrast"]) + p["brightness"]
            v = clamp(v)
            if p["invert"]:
                v = 1.0 - v
            row.append(levels(v, p["black"], p["white"], p["gamma"]))
        out.append(row)
    return out


def bump(image, params=None):
    p = _params("bump", params)
    out = []
    for y in range(image.height):
        row = []
        for x in range(image.width):
            base = image.lum(x, y)
            if p["blur"] > 0:
                blurred = box(image.lum, x, y, p["blur"])
                v = base + (blurred - base) * clamp(p["blur"])
            else:
                v = base
            v = contrast(v, p["contrast"]) + p["brightness"]
            v = (v - 0.5) * p["strength"] + 0.5
            v = clamp(v)
            if p["invert"]:
                v = 1.0 - v
            row.append(levels(v, p["black"], p["white"], p["gamma"]))
        out.append(row)
    return out


def _channel(image, index):
    def pick(x, y):
        r, g, b = image.rgb(x, y)
        if index == 1:
            return r
        if index == 2:
            return g
        if index == 3:
            return b
        if index == 4:
            mx, mn = max(r, g, b), min(r, g, b)
            return (mx - mn) / mx if mx > 0 else 0.0
        return LUMA[0] * r + LUMA[1] * g + LUMA[2] * b
    return pick


def roughness(image, params=None):
    p = _params("roughness", params)
    pick = _channel(image, int(p["channel"]))
    out = []
    for y in range(image.height):
        row = []
        for x in range(image.width):
            v = box(pick, x, y, p["smoothing"])
            v = clamp(contrast(v, p["contrast"]) + p["brightness"])
            if p["bright_smooth"]:
                v = 1.0 - v
            v = levels(v, p["black"], p["white"], p["gamma"])
            row.append(p["rough_min"] + (p["rough_max"] - p["rough_min"]) * v)
        out.append(row)
    return out


def ao(image, params=None):
    p = _params("ao", params)
    dirs = [(math.cos(math.radians(a)), math.sin(math.radians(a)))
            for a in range(0, 360, 30)]
    rings = (1 / 3.0, 2 / 3.0, 1.0)
    weight = sum(1.0 / f for _d in dirs for f in rings)
    radius = p["radius"]
    out = []
    for y in range(image.height):
        row = []
        for x in range(image.width):
            centre = image.lum(x, y)
            occ = 0.0
            for dx, dy in dirs:
                for f in rings:
                    sx = int(round(x + dx * radius * f))
                    sy = int(round(y + dy * radius * f))
                    occ += max(0.0, image.lum(sx, sy) - centre) * (1.0 / f)
            value = 1.0 - clamp((occ / weight) * p["intensity"])
            value = value ** p["falloff"]
            value = 1.0 + (value - 1.0) * p["amount"]
            if p["invert"]:
                value = 1.0 - value
            row.append(levels(value, p["black"], p["white"], p["gamma"]))
        out.append(row)
    return out


def metallic(image, params=None):
    p = _params("metallic", params)
    mode = int(p["mode"])

    def pick(x, y):
        r, g, b = image.rgb(x, y)
        if mode == 1:
            mx, mn = max(r, g, b), min(r, g, b)
            sat = (mx - mn) / mx if mx > 0 else 0.0
            return (1.0 - sat) * smoothstep(0.0, 0.15, mx)
        if mode == 2:
            return r
        if mode == 3:
            return g
        if mode == 4:
            return b
        return LUMA[0] * r + LUMA[1] * g + LUMA[2] * b

    out = []
    for y in range(image.height):
        row = []
        for x in range(image.width):
            v = box(pick, x, y, p["smoothing"])
            m = smoothstep(p["threshold"] - p["softness"],
                           p["threshold"] + p["softness"], v)
            if p["invert"]:
                m = 1.0 - m
            row.append(p["non_metal"] + (p["metal"] - p["non_metal"]) * m)
        out.append(row)
    return out


RENDERERS = {"normal": normal, "height": height, "bump": bump,
             "roughness": roughness, "ao": ao, "metallic": metallic}


def render(key, image, params=None):
    return RENDERERS[key](image, params)
