"""Texture Maps — the GPU engine: shaders, meshes, and the map spec.

Every map is a fragment shader over the source image. Nothing here loops over
pixels in Python, because the app venv has **no numpy** (kept out on purpose,
`requirements.txt`) and 4096x4096 is 16.7 million pixels — AO alone takes 36
taps each. Qt already ships the GPU: `PySide6.QtOpenGL` is in the wheel and
`Qt6OpenGL.dll` + the `opengl32sw.dll` software fallback are already in the
frozen build, so this costs no new dependency.

Measured on this machine (RTX 4080S, GL 3.3 core), and the numbers the design
leans on:
  * 4096^2 render + readback  15.1 ms   -> live preview at slider rate
  * 1024^2 texture upload      4.4 ms   -> once per source, not per render
  * RGBA16 FBO -> toImage() -> Grayscale16 -> save() writes a REAL 16-bit
    grayscale PNG (IHDR bitdepth=16, colortype=0), with zero per-pixel Python.

⚠ **THERE IS NO `QOpenGLWidget` AND NO `AA_ShareOpenGLContexts`.** The obvious
design shares this context with an on-screen GL widget, and that attribute must
be set BEFORE the QApplication exists — an app-global constraint that every
test suite building this tab would also have to know about, enforced from a
module that has no way to check it. Instead everything renders to an FBO and is
read back as a QImage, and the preview is an ordinary QWidget painting a
pixmap. The readback is the price (a few ms at preview size); what it buys is
that this module cannot break anything outside itself, and that a machine which
refuses a GL context loses this ONE tab rather than the app's startup.

⚠ **GL IS GUI-THREAD ONLY.** A context has thread affinity: never call a runner
method from a worker. The slow part is not the rendering, it is PNG encoding
(368 ms for a 4096^2 PNG) — that runs off-thread on QImages already read back,
which is safe because a QImage is not a GL object.

⚠ **NO sRGB DECODE ANYWHERE.** Every map works on display-space bytes, the
values as you see them, exactly like the source site's shaders. Uploading the
source as an sRGB texture (or linearising in the shader) makes every map darker
and crushed compared to what people expect from these tools. The maps are DATA
— Non-Color in Blender — and the manual says so.

⚠ **ORIENTATION.** The source is uploaded unflipped, so `v = 0` is image row 0
(the top). The full-screen pass derives `uv` straight from clip space, so
`uv.y = 0` is the bottom of the framebuffer — and `QOpenGLFramebufferObject.
toImage()` flips on the way out, which lands source row 0 back on output row 0.
Two flips that cancel: change either one and every map comes out upside down.
`app_texmaps_test.py` pins it with a deliberately asymmetric image.
"""
import math
from array import array

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import (QImage, QOffscreenSurface, QOpenGLContext,
                           QSurfaceFormat)
from PySide6.QtOpenGL import (QOpenGLBuffer, QOpenGLFramebufferObject,
                              QOpenGLFramebufferObjectFormat, QOpenGLShader,
                              QOpenGLShaderProgram, QOpenGLTexture,
                              QOpenGLVertexArrayObject)

# ---------------------------------------------------------------- GL enums
# Named here rather than imported: PyOpenGL is NOT a dependency (checked — it
# is absent from the venv), and Qt's own QOpenGLFunctions covers everything
# below. A handful of integers is a much smaller thing to ship than a binding.
GL_TRIANGLES = 0x0004
GL_FLOAT = 0x1406
GL_DEPTH_TEST = 0x0B71
GL_CULL_FACE = 0x0B44
GL_COLOR_BUFFER_BIT = 0x4000
GL_DEPTH_BUFFER_BIT = 0x0100
GL_TEXTURE0 = 0x84C0
GL_TEXTURE_2D = 0x0DE1
GL_RGBA16 = 0x805B
GL_MAX_TEXTURE_SIZE = 0x0D33
GL_TEXTURE_WRAP_S = 0x2802
GL_TEXTURE_WRAP_T = 0x2803
GL_TEXTURE_MIN_FILTER = 0x2801
GL_TEXTURE_MAG_FILTER = 0x2800
GL_LINEAR = 0x2601
GL_CLAMP_TO_EDGE = 0x812F
GL_REPEAT = 0x2901

PREVIEW_MAX = 1024      # long side of a live preview render (export is full)
STATS_SIZE = 64         # the reduction grid Seamless reads its means from


class GLUnavailable(RuntimeError):
    """No usable OpenGL context. The tab says so and disables itself; nothing
    else in the app is affected."""


# ===========================================================================
# The map spec — ONE table, read by the shaders, the UI and the reference
# ===========================================================================
# ⚠ This table is the single source of truth for every default, range and
# label. The UI builds its forms from it, `texmaps_ref.py` reads the same
# defaults, and config.json stores exactly these keys. Three copies of a
# default is how the app and its own tests end up disagreeing about what
# "Contrast 0.15" means, so there is one.

def _f(key, label, lo, hi, default, decimals=2, suffix="", tip=""):
    return {"key": key, "label": label, "kind": "float", "lo": lo, "hi": hi,
            "default": default, "decimals": decimals, "suffix": suffix,
            "tip": tip}


def _b(key, label, default=False, tip=""):
    return {"key": key, "label": label, "kind": "bool", "default": default,
            "tip": tip}


def _c(key, label, choices, default=0, tip=""):
    return {"key": key, "label": label, "kind": "choice", "choices": choices,
            "default": default, "tip": tip}


_LEVELS = [
    _f("black", "Levels black", 0.0, 1.0, 0.0, 2,
       tip="Input black point — everything below this becomes 0"),
    _f("white", "Levels white", 0.0, 1.0, 1.0, 2,
       tip="Input white point — everything above this becomes 1"),
    _f("gamma", "Levels gamma", 0.1, 3.0, 1.0, 2,
       tip="Midtone bend applied after the black/white points"),
]

# Roughness presets, measured from the site by switching between them. Brightness
# stays 0, levels stay 0/1/1, the channel stays Luminance and Bright = Smooth
# stays on — a preset that changed those would be a different tool, not a preset.
ROUGH_PRESETS = {
    0: {},                                                    # Standard = the defaults
    1: {"contrast": 0.10, "smoothing": 1.0, "rough_min": 0.55,
        "rough_max": 1.00, "gamma": 0.85},                    # Matte / Rough
    2: {"contrast": 0.35, "smoothing": 1.0, "rough_min": 0.05,
        "rough_max": 0.45, "gamma": 1.25},                    # Polished / Glossy
    3: {"contrast": 0.50, "smoothing": 0.0, "rough_min": 0.25,
        "rough_max": 1.00, "gamma": 1.00},                    # Worn / High Contrast
}

MAPS = {
    "normal": {
        "label": "Normal", "out": "rgb", "order": 0,
        "hint": "Surface direction. Wire through a Normal Map node.",
        "params": [
            _f("strength", "Strength", 0.1, 3.0, 1.5, 2,
               tip="How far the surface tilts — the depth of the detail"),
            _f("detail", "Detail scale", 0.5, 2.0, 1.0, 2,
               tip="How far apart the samples sit; higher reads broader shapes"),
            _c("operator", "Operator", ["Sobel", "Scharr (sharper)"], 0),
            _b("invert_y", "Invert Y  (DirectX)", False,
               tip="Off = OpenGL / Blender. On = DirectX / Unreal."),
            _f("blur", "Blur", 0.0, 5.0, 0.5, 1,
               tip="Widens the sampling stencil — softens noise into shape"),
            _f("sharpen", "Sharpen", 0.0, 2.0, 0.0, 1,
               tip="Adds back the difference between a narrow and a wide read"),
        ] + _LEVELS,
    },
    "roughness": {
        "label": "Roughness", "out": "gray", "order": 1,
        "hint": "Not gloss: white is rough, black is polished.",
        "params": [
            _c("preset", "Preset", ["Standard", "Matte / Rough",
                                    "Polished / Glossy", "Worn / High Contrast"], 0),
            _c("channel", "Source channel", ["Luminance", "Red", "Green",
                                             "Blue", "Saturation"], 0),
            _b("bright_smooth", "Bright = Smooth", True,
               tip="Bright parts of the photo read as polished"),
            _f("contrast", "Contrast", -1.0, 1.0, 0.15, 2),
            _f("brightness", "Brightness", -0.5, 0.5, 0.0, 2),
            _f("smoothing", "Smoothing", 0.0, 8.0, 1.0, 1, " px"),
            _f("rough_min", "Min roughness", 0.0, 1.0, 0.15, 2,
               tip="Nothing exports below this — the single biggest reason "
                   "converted photos look wrong is a 0..1 roughness"),
            _f("rough_max", "Max roughness", 0.0, 1.0, 0.95, 2),
        ] + _LEVELS,
    },
    "ao": {
        "label": "Ambient Occlusion", "out": "gray", "order": 2,
        "hint": "Contact shadow estimate. Multiply into base colour, never "
                "into a light.",
        "params": [
            _f("radius", "Radius", 2, 80, 24, 0, " px",
               tip="~10-20 px fabric weave, 25-45 brick mortar, >60 broad dents"),
            _f("intensity", "Intensity", 0.0, 20.0, 6.0, 2),
            _f("falloff", "Falloff", 0.2, 3.0, 1.0, 2),
            _f("amount", "Amount", 0.0, 1.0, 1.0, 2),
            _b("invert", "Invert", False),
        ] + _LEVELS,
    },
    "height": {
        "label": "Height", "out": "gray", "order": 3,
        "hint": "Displacement. 16-bit by default — 8-bit bands on smooth slopes.",
        "params": [
            _f("contrast", "Contrast", -1.0, 1.0, 0.25, 2),
            _f("brightness", "Brightness", -0.5, 0.5, 0.0, 2),
            _f("smoothing", "Smoothing", 0.0, 8.0, 1.5, 1, " px"),
            _b("invert", "Invert height", False),
        ] + _LEVELS,
    },
    "metallic": {
        "label": "Metallic", "out": "gray", "order": 4,
        "hint": "A mask, not a shade: 0 or 1, rarely between.",
        "params": [
            _c("mode", "Detection", ["Brightness", "Desaturation", "Red",
                                     "Green", "Blue"], 0),
            _f("threshold", "Threshold", 0.0, 1.0, 0.55, 2),
            _f("softness", "Softness", 0.0, 0.5, 0.12, 2),
            _f("smoothing", "Smoothing", 0.0, 8.0, 1.0, 1, " px"),
            _b("invert", "Invert mask", False),
            _f("non_metal", "Non-metal value", 0.0, 1.0, 0.0, 2),
            _f("metal", "Metal value", 0.0, 1.0, 1.0, 2),
        ],
    },
    "bump": {
        "label": "Bump", "out": "gray", "order": 5,
        "hint": "Shading-only detail. A normal map is better unless you need "
                "a Bump node.",
        "params": [
            _f("strength", "Strength", 0.1, 2.0, 1.0, 2),
            _f("contrast", "Contrast", -1.0, 1.0, 0.20, 2),
            _f("brightness", "Brightness", -1.0, 1.0, 0.0, 2),
            _f("blur", "Blur", 0.0, 5.0, 0.5, 1, " px"),
            _b("invert", "Invert height", False),
        ] + _LEVELS,
    },
    "seamless": {
        "label": "Seamless", "out": "rgb", "order": 6,
        "hint": "Makes the SOURCE tile. Tick it and the other maps are made "
                "from the tiling version.",
        "params": [
            _f("size", "Output size", 128, 4096, 1024, 0, " px",
               tip="Square, cropped from the short side"),
            _f("crop_x", "Crop X", 0.0, 1.0, 0.5, 2),
            _f("crop_y", "Crop Y", 0.0, 1.0, 0.5, 2),
            _f("blend", "Blend width", 6, 120, 64, 0, " px", tip="Try 36-80"),
            _f("strength", "Strength", 0, 100, 60, 0, " %"),
            _b("eq_light", "Equalize lighting", True,
               tip="Divides out uneven lighting before the seams are blended"),
            _b("eq_edges", "Equalize edges", True,
               tip="Matches the average colour of opposite edges"),
        ],
    },
}

MAP_ORDER = [k for k, _v in sorted(MAPS.items(), key=lambda kv: kv[1]["order"])]

# What a fresh install exports. ⚠ Metallic is OFF on purpose and this is a
# reconciliation, not an oversight: the site's standalone METAL page defaults
# to a live mask (threshold 0.55, metal 1.0) while its Full PBR set defaults
# Metal Amount to 0.00 — "most surfaces are not metal", which produces a black
# map. Both are right about different things. Keeping the live mask values but
# leaving the map unticked means the safe default is "no metallic map", and
# ticking it gives you a real one rather than a black square.
DEFAULT_ENABLED = {"normal": True, "roughness": True, "ao": True,
                   "height": True, "metallic": False, "bump": False,
                   "seamless": False}

# Preview-only dials. ⚠ They are in their own group in the UI and say "not
# exported" on the group box, because a Displacement slider that silently did
# not affect the exported height map is the single most confusing thing about
# the original tool.
PREVIEW_PARAMS = [
    _c("tiling", "Tiling", ["1x1", "2x2", "3x3"], 1),
    _f("normal_depth", "Normal depth", 0.0, 3.0, 1.0, 2),
    _f("ao_intensity", "AO intensity", 0.0, 2.0, 1.0, 2),
    _f("displacement", "Displacement", 0.0, 0.4, 0.12, 3),
]


def defaults_for(key):
    """Every parameter of one map at its default."""
    return {p["key"]: p["default"] for p in MAPS[key]["params"]}


def all_defaults():
    out = {k: defaults_for(k) for k in MAPS}
    out["preview"] = {p["key"]: p["default"] for p in PREVIEW_PARAMS}
    out["enabled"] = dict(DEFAULT_ENABLED)
    return out


def apply_rough_preset(params, index):
    """Roughness presets write the dials they own and leave the rest alone."""
    out = dict(params)
    out.update(defaults_for("roughness"))
    out.update(ROUGH_PRESETS.get(int(index), {}))
    out["preset"] = int(index)
    out["channel"] = params.get("channel", 0)
    out["bright_smooth"] = params.get("bright_smooth", True)
    return out


# ===========================================================================
# GLSL
# ===========================================================================
# One vertex shader for every map: a full-screen triangle generated from
# gl_VertexID, so there is no quad buffer and no attribute plumbing at all.
VS_FULLSCREEN = """#version 330 core
out vec2 vUv;
void main() {
    vec2 p = vec2(float((gl_VertexID << 1) & 2), float(gl_VertexID & 2));
    vUv = p;
    gl_Position = vec4(p * 2.0 - 1.0, 0.0, 1.0);
}
"""

# Shared helpers. `cuv` is why a photo does not smear its far edge into its
# near one: every neighbour tap is CLAMPED to the image, never wrapped. A photo
# is not a tile; a real tile is unaffected because its edges already agree.
FS_COMMON = """#version 330 core
in vec2 vUv;
out vec4 fragColor;
uniform sampler2D uSrc;
uniform vec2 uTexel;
uniform float uBlack, uWhite, uGamma;
uniform int uInvert;

float luma3(vec3 c) { return dot(c, vec3(0.2126, 0.7152, 0.0722)); }
vec2 cuv(vec2 uv) { return clamp(uv, uTexel * 0.5, vec2(1.0) - uTexel * 0.5); }
vec3 rgbAt(vec2 uv) { return texture(uSrc, cuv(uv)).rgb; }
float lumAt(vec2 uv) { return luma3(rgbAt(uv)); }

/* "Smoothing / Blur = N px" is a 3x3 box mean whose taps sit N texels apart.
   Cheap, and it is what gives these tools their look — a real gaussian reads
   noticeably softer at the same number. */
float boxAt(vec2 uv, float n) {
    if (n <= 0.0) return lumAt(uv);
    vec2 o = uTexel * n;
    float s = 0.0;
    for (int j = -1; j <= 1; ++j)
        for (int i = -1; i <= 1; ++i)
            s += lumAt(uv + vec2(float(i) * o.x, float(j) * o.y));
    return s / 9.0;
}
float levelsf(float v) {
    float d = max(1.0e-5, uWhite - uBlack);
    return clamp(pow(clamp((v - uBlack) / d, 0.0, 1.0), 1.0 / max(uGamma, 1.0e-3)),
                 0.0, 1.0);
}
float contrastf(float v, float c) { return (v - 0.5) * (1.0 + c) + 0.5; }
float invertf(float v) { return uInvert == 1 ? 1.0 - v : v; }
"""

FS_NORMAL = FS_COMMON + """
uniform float uStrength, uDetail, uBlur, uSharpen;
uniform int uScharr, uInvertY;

vec2 gradAt(vec2 uv, vec2 px) {
    float w1 = uScharr == 1 ? 3.0 : 1.0;
    float w2 = uScharr == 1 ? 10.0 : 2.0;
    float tl = lumAt(uv + vec2(-px.x, -px.y));
    float t  = lumAt(uv + vec2(0.0,   -px.y));
    float tr = lumAt(uv + vec2( px.x, -px.y));
    float l  = lumAt(uv + vec2(-px.x,  0.0));
    float r  = lumAt(uv + vec2( px.x,  0.0));
    float bl = lumAt(uv + vec2(-px.x,  px.y));
    float b  = lumAt(uv + vec2(0.0,    px.y));
    float br = lumAt(uv + vec2( px.x,  px.y));
    /* Normalised by the weight sum so Sobel (1/2/1) and Scharr (3/10/3) mean
       the same thing at the same Strength — otherwise switching operator
       quadruples the depth and reads as a bug. */
    float k = 1.0 / (2.0 * w1 + w2);
    float dx = ((w1 * tr + w2 * r + w1 * br) - (w1 * tl + w2 * l + w1 * bl)) * k;
    float dy = ((w1 * bl + w2 * b + w1 * br) - (w1 * tl + w2 * t + w1 * tr)) * k;
    /* ⚠ NEGATED, because the source is uploaded MIRRORED (see set_source) so
       v runs UP the image while the algorithm — and `texmaps_ref.py` — define
       dy top-to-bottom. Without this the green channel is inverted, which is
       precisely a DirectX normal map: it looks completely plausible and lights
       backwards. The cross-check against the reference is what catches it. */
    return vec2(dx, -dy);
}

void main() {
    vec2 px = uTexel * uDetail;
    vec2 g = gradAt(vUv, px);
    if (uBlur > 0.0) {
        vec2 gw = gradAt(vUv, px * (1.0 + uBlur));
        g = mix(g, gw, clamp(uBlur, 0.0, 1.0));
    }
    if (uSharpen > 0.0) {
        /* Unsharp on the GRADIENT, not on the image: filtering the image first
           cancels out in the difference and does nothing at all. */
        vec2 gw = gradAt(vUv, px * 2.0);
        g += (g - gw) * uSharpen;
    }
    float sy = uInvertY == 1 ? 1.0 : -1.0;
    vec3 n = normalize(vec3(-g.x * uStrength, sy * g.y * uStrength, 1.0));
    vec3 rgb = n * 0.5 + 0.5;
    fragColor = vec4(levelsf(rgb.r), levelsf(rgb.g), levelsf(rgb.b), 1.0);
}
"""

FS_HEIGHT = FS_COMMON + """
uniform float uContrast, uBrightness, uSmoothing;
void main() {
    float v = boxAt(vUv, uSmoothing);
    v = contrastf(v, uContrast) + uBrightness;
    v = invertf(clamp(v, 0.0, 1.0));
    v = levelsf(v);
    fragColor = vec4(v, v, v, 1.0);
}
"""

FS_BUMP = FS_COMMON + """
uniform float uStrength, uContrast, uBrightness, uBlur;
void main() {
    float base = lumAt(vUv);
    float v = uBlur > 0.0 ? mix(base, boxAt(vUv, uBlur), clamp(uBlur, 0.0, 1.0))
                          : base;
    v = contrastf(v, uContrast) + uBrightness;
    v = (v - 0.5) * uStrength + 0.5;
    v = invertf(clamp(v, 0.0, 1.0));
    v = levelsf(v);
    fragColor = vec4(v, v, v, 1.0);
}
"""

FS_ROUGH = FS_COMMON + """
uniform float uContrast, uBrightness, uSmoothing, uMin, uMax;
uniform int uChannel, uBrightSmooth;

float pick(vec2 uv) {
    vec3 c = rgbAt(uv);
    if (uChannel == 1) return c.r;
    if (uChannel == 2) return c.g;
    if (uChannel == 3) return c.b;
    if (uChannel == 4) {
        float mx = max(c.r, max(c.g, c.b));
        float mn = min(c.r, min(c.g, c.b));
        return mx > 0.0 ? (mx - mn) / mx : 0.0;
    }
    return luma3(c);
}
float pickBox(vec2 uv, float n) {
    if (n <= 0.0) return pick(uv);
    vec2 o = uTexel * n;
    float s = 0.0;
    for (int j = -1; j <= 1; ++j)
        for (int i = -1; i <= 1; ++i)
            s += pick(uv + vec2(float(i) * o.x, float(j) * o.y));
    return s / 9.0;
}
void main() {
    float v = pickBox(vUv, uSmoothing);
    v = contrastf(v, uContrast) + uBrightness;
    v = clamp(v, 0.0, 1.0);
    if (uBrightSmooth == 1) v = 1.0 - v;
    v = levelsf(v);
    /* The output-range clamp. Photos converted without it are the single most
       common bad-looking roughness map there is. */
    v = mix(uMin, uMax, v);
    fragColor = vec4(v, v, v, 1.0);
}
"""

FS_AO = FS_COMMON + """
uniform float uRadius, uIntensity, uFalloff, uAmount;
void main() {
    float c = lumAt(vUv);
    float occ = 0.0;
    float w = 0.0;
    /* 12 directions 30 degrees apart, 3 ring distances each: a heightfield
       occlusion estimate, not a ray trace. Nearer rings count for more. */
    for (int i = 0; i < 12; ++i) {
        float a = float(i) * 0.5235987755982988;
        vec2 d = vec2(cos(a), sin(a));
        for (int k = 1; k <= 3; ++k) {
            float f = float(k) / 3.0;
            float s = lumAt(vUv + d * (uRadius * f) * uTexel);
            occ += max(0.0, s - c) * (1.0 / f);
            w += 1.0 / f;
        }
    }
    float o = occ / w;
    float ao = 1.0 - clamp(o * uIntensity, 0.0, 1.0);
    ao = pow(ao, uFalloff);
    ao = mix(1.0, ao, uAmount);
    ao = levelsf(invertf(ao));
    fragColor = vec4(ao, ao, ao, 1.0);
}
"""

FS_METAL = FS_COMMON + """
uniform float uThreshold, uSoftness, uSmoothing, uNonMetal, uMetal;
uniform int uMode;

float pick(vec2 uv) {
    vec3 c = rgbAt(uv);
    if (uMode == 1) {
        float mx = max(c.r, max(c.g, c.b));
        float mn = min(c.r, min(c.g, c.b));
        float sat = mx > 0.0 ? (mx - mn) / mx : 0.0;
        /* The smoothstep is load-bearing: without it every near-black pixel is
           perfectly desaturated and scores as metal. */
        return (1.0 - sat) * smoothstep(0.0, 0.15, mx);
    }
    if (uMode == 2) return c.r;
    if (uMode == 3) return c.g;
    if (uMode == 4) return c.b;
    return luma3(c);
}
float pickBox(vec2 uv, float n) {
    if (n <= 0.0) return pick(uv);
    vec2 o = uTexel * n;
    float s = 0.0;
    for (int j = -1; j <= 1; ++j)
        for (int i = -1; i <= 1; ++i)
            s += pick(uv + vec2(float(i) * o.x, float(j) * o.y));
    return s / 9.0;
}
void main() {
    float v = pickBox(vUv, uSmoothing);
    float m = smoothstep(uThreshold - uSoftness, uThreshold + uSoftness, v);
    m = invertf(m);
    float outv = mix(uNonMetal, uMetal, m);
    fragColor = vec4(outv, outv, outv, 1.0);
}
"""

# ---- Seamless: a multi-pass CPU algorithm moved onto the GPU ---------------
FS_CROP = """#version 330 core
in vec2 vUv;
out vec4 fragColor;
uniform sampler2D uSrc;
uniform vec2 uOrigin;      /* top-left of the square crop, in uv */
uniform float uSpan;       /* its side, in uv */
void main() {
    fragColor = vec4(texture(uSrc, uOrigin + vUv * uSpan).rgb, 1.0);
}
"""

FS_BLUR1D = """#version 330 core
in vec2 vUv;
out vec4 fragColor;
uniform sampler2D uSrc;
uniform vec2 uStep;        /* one texel along the blur axis */
uniform int uRadius;
void main() {
    vec3 s = vec3(0.0);
    float n = 0.0;
    for (int i = -24; i <= 24; ++i) {
        if (abs(i) > uRadius) continue;
        s += texture(uSrc, clamp(vUv + uStep * float(i), vec2(0.001), vec2(0.999))).rgb;
        n += 1.0;
    }
    fragColor = vec4(s / n, 1.0);
}
"""

FS_EQUALIZE = """#version 330 core
in vec2 vUv;
out vec4 fragColor;
uniform sampler2D uSrc;    /* the cropped image */
uniform sampler2D uBlur;   /* its blurred luminance */
uniform float uMean;       /* mean luminance of the whole crop */
uniform int uDoLight;
uniform int uDoEdges;
uniform vec3 uDiffX;       /* right band mean - left band mean */
uniform vec3 uDiffY;       /* bottom band mean - top band mean */
uniform float uBandX, uBandY;   /* band widths in uv */
float luma3(vec3 c) { return dot(c, vec3(0.2126, 0.7152, 0.0722)); }
void main() {
    vec3 c = texture(uSrc, vUv).rgb;
    if (uDoLight == 1) {
        float bl = max(1.0e-4, luma3(texture(uBlur, vUv).rgb));
        /* ^0.75 rather than 1.0: a full division flattens the material's own
           shading along with the lighting and reads as plastic. */
        c *= pow(uMean / bl, 0.75);
    }
    if (uDoEdges == 1) {
        /* Half the difference, ramped across the band on each side: the two
           edges meet in the middle instead of one being dragged to the other. */
        float rx = clamp(1.0 - vUv.x / uBandX, 0.0, 1.0)
                 - clamp(1.0 - (1.0 - vUv.x) / uBandX, 0.0, 1.0);
        float ry = clamp(1.0 - vUv.y / uBandY, 0.0, 1.0)
                 - clamp(1.0 - (1.0 - vUv.y) / uBandY, 0.0, 1.0);
        c += uDiffX * 0.5 * rx + uDiffY * 0.5 * ry;
    }
    fragColor = vec4(clamp(c, 0.0, 1.0), 1.0);
}
"""

FS_OFFSET_BLEND = """#version 330 core
in vec2 vUv;
out vec4 fragColor;
uniform sampler2D uSrc;
uniform vec2 uTexel;
uniform float uWidth;      /* blend half-width, in uv */
uniform float uStrength;
uniform int uOffset;       /* 1 = swap quadrants first (only the first pass) */

vec3 fetch(vec2 uv) {
    vec2 p = uOffset == 1 ? fract(uv + vec2(0.5)) : fract(uv);
    return texture(uSrc, p).rgb;
}
void main() {
    vec3 c = fetch(vUv);
    /* After the half-offset the seams cross the middle. Blend each pixel with
       its MIRROR across the seam, falling off linearly over the blend width. */
    float tx = vUv.x - 0.5;
    if (abs(tx) < uWidth && uWidth > 0.0) {
        vec3 m = fetch(vec2(1.0 - vUv.x, vUv.y));
        c = mix(c, m, (1.0 - abs(tx) / uWidth) * 0.5 * uStrength);
    }
    float ty = vUv.y - 0.5;
    if (abs(ty) < uWidth && uWidth > 0.0) {
        vec3 m = fetch(vec2(vUv.x, 1.0 - vUv.y));
        c = mix(c, m, (1.0 - abs(ty) / uWidth) * 0.5 * uStrength);
    }
    /* A small feathering box across the seam only — a full-image blur here
       would soften the whole tile to hide a join in 2% of it. */
    float fx = 1.0 - smoothstep(0.0, uWidth * 0.25, abs(tx));
    float fy = 1.0 - smoothstep(0.0, uWidth * 0.25, abs(ty));
    float f = max(fx, fy);
    if (f > 0.0) {
        vec3 s = vec3(0.0);
        for (int j = -1; j <= 1; ++j)
            for (int i = -1; i <= 1; ++i)
                s += fetch(vUv + vec2(float(i), float(j)) * uTexel * 2.0);
        c = mix(c, s / 9.0, f * 0.6);
    }
    fragColor = vec4(c, 1.0);
}
"""

FS_DOWNSCALE = """#version 330 core
in vec2 vUv;
out vec4 fragColor;
uniform sampler2D uSrc;
uniform vec2 uStep;        /* a source texel */
uniform int uTaps;
void main() {
    vec3 s = vec3(0.0);
    float n = 0.0;
    for (int j = 0; j < 8; ++j)
        for (int i = 0; i < 8; ++i) {
            if (i >= uTaps || j >= uTaps) continue;
            s += texture(uSrc, vUv + vec2(float(i), float(j)) * uStep).rgb;
            n += 1.0;
        }
    fragColor = vec4(s / max(n, 1.0), 1.0);
}
"""

# 2D view: the finished map, tiled.
FS_FLAT = """#version 330 core
in vec2 vUv;
out vec4 fragColor;
uniform sampler2D uSrc;
uniform float uTiling;
void main() { fragColor = vec4(texture(uSrc, vUv * uTiling).rgb, 1.0); }
"""

# ---- the lit material preview ---------------------------------------------
VS_MESH = """#version 330 core
layout(location = 0) in vec3 aPos;
layout(location = 1) in vec3 aNormal;
layout(location = 2) in vec3 aTangent;
layout(location = 3) in vec2 aUv;
uniform mat4 uMVP;
uniform mat3 uNormalMat;
uniform sampler2D uHeight;
uniform float uDisplace;
uniform float uTiling;
uniform int uHasHeight;
out vec3 vNormal;
out vec3 vTangent;
out vec2 vUv;
out vec3 vPos;
void main() {
    vec2 uv = aUv * uTiling;
    vec3 p = aPos;
    if (uHasHeight == 1 && uDisplace > 0.0)
        p += aNormal * (texture(uHeight, uv).r - 0.5) * uDisplace;
    vNormal = uNormalMat * aNormal;
    vTangent = uNormalMat * aTangent;
    vUv = uv;
    vPos = p;
    gl_Position = uMVP * vec4(p, 1.0);
}
"""

FS_MESH = """#version 330 core
in vec3 vNormal;
in vec3 vTangent;
in vec2 vUv;
in vec3 vPos;
out vec4 fragColor;
uniform sampler2D uBase, uNormal, uRough, uAO, uMetal;
uniform int uHasNormal, uHasRough, uHasAO, uHasMetal;
uniform float uNormalDepth, uAOIntensity;
uniform vec3 uEye;

const float PI = 3.14159265;

float D_GGX(float NoH, float a) {
    float a2 = a * a;
    float d = NoH * NoH * (a2 - 1.0) + 1.0;
    return a2 / max(1.0e-6, PI * d * d);
}
float V_Smith(float NoV, float NoL, float a) {
    float k = a * 0.5;
    return 0.25 / max(1.0e-4, (NoL * (1.0 - k) + k) * (NoV * (1.0 - k) + k));
}
void main() {
    vec3 N = normalize(vNormal);
    if (uHasNormal == 1 && uNormalDepth > 0.0) {
        vec3 T = normalize(vTangent - N * dot(N, vTangent));
        vec3 B = cross(N, T);
        vec3 t = texture(uNormal, vUv).rgb * 2.0 - 1.0;
        t.xy *= uNormalDepth;
        N = normalize(mat3(T, B, N) * normalize(t));
    }
    vec3 base = texture(uBase, vUv).rgb;
    float rough = uHasRough == 1 ? clamp(texture(uRough, vUv).r, 0.04, 1.0) : 0.45;
    float metal = uHasMetal == 1 ? texture(uMetal, vUv).r : 0.0;
    float ao = uHasAO == 1 ? mix(1.0, texture(uAO, vUv).r, uAOIntensity) : 1.0;

    vec3 V = normalize(uEye - vPos);
    vec3 L = normalize(vec3(-0.45, 0.62, 0.72));
    vec3 H = normalize(V + L);
    float NoL = max(dot(N, L), 0.0);
    float NoV = max(dot(N, V), 1.0e-4);
    float NoH = max(dot(N, H), 0.0);
    float VoH = max(dot(V, H), 0.0);

    vec3 f0 = mix(vec3(0.04), base, metal);
    vec3 F = f0 + (1.0 - f0) * pow(1.0 - VoH, 5.0);
    float a = rough * rough;
    vec3 spec = F * D_GGX(NoH, a) * V_Smith(NoV, NoL, a);
    vec3 diff = base * (1.0 - metal) / PI;

    /* One directional light plus a sky/ground ambient — enough to judge a map,
       and deliberately not a renderer. */
    vec3 sky = mix(vec3(0.20, 0.21, 0.25), vec3(0.52, 0.55, 0.62),
                   N.y * 0.5 + 0.5);
    vec3 col = (diff + spec) * NoL * 1.05 + base * sky * ao * (1.0 - metal * 0.6)
             + f0 * sky * ao * metal * 0.5;
    /* A flat tonemap + the display transfer, so what is on screen is roughly
       what a render would show rather than a raw linear buffer. */
    col = col / (col + vec3(0.85));
    fragColor = vec4(pow(clamp(col, 0.0, 1.0), vec3(1.0 / 2.2)), 1.0);
}
"""


# ===========================================================================
# Meshes — built once, in Python, as flat triangle lists
# ===========================================================================
# ⚠ NOT INDEXED. `glDrawElements` needs a void* offset that PySide6's
# QOpenGLFunctions wrapper does not take cleanly, and an expanded buffer for a
# 64x32 sphere is 540 KB — which is nothing, built once, at first open of the
# tab. Trading half a megabyte for "no binding-specific pointer arithmetic" is
# the right trade in a module that must not be fragile.

def _sphere(segments=64, rings=32):
    """Position, normal, tangent, uv — tangent is analytic (d(pos)/du)."""
    data = array("f")
    for j in range(rings):
        for i in range(segments):
            quad = []
            for (di, dj) in ((0, 0), (1, 0), (1, 1), (0, 0), (1, 1), (0, 1)):
                u = (i + di) / float(segments)
                v = (j + dj) / float(rings)
                phi = u * 2.0 * math.pi
                theta = v * math.pi
                sx = math.sin(theta) * math.cos(phi)
                sy = math.cos(theta)
                sz = math.sin(theta) * math.sin(phi)
                tx = -math.sin(phi)
                tz = math.cos(phi)
                # v flipped so the texture reads the right way up on the ball
                quad.extend((sx, sy, sz, sx, sy, sz, tx, 0.0, tz, u, 1.0 - v))
            data.extend(quad)
    return data


def _cube():
    faces = (
        ((0, 0, 1), (1, 0, 0)), ((0, 0, -1), (-1, 0, 0)),
        ((1, 0, 0), (0, 0, -1)), ((-1, 0, 0), (0, 0, 1)),
        ((0, 1, 0), (1, 0, 0)), ((0, -1, 0), (1, 0, 0)),
    )
    data = array("f")
    for normal, tangent in faces:
        nx, ny, nz = normal
        bx = ny * tangent[2] - nz * tangent[1]
        by = nz * tangent[0] - nx * tangent[2]
        bz = nx * tangent[1] - ny * tangent[0]
        for (u, v) in ((0, 0), (1, 0), (1, 1), (0, 0), (1, 1), (0, 1)):
            su, sv = u * 2.0 - 1.0, v * 2.0 - 1.0
            px = nx + tangent[0] * su + bx * sv
            py = ny + tangent[1] * su + by * sv
            pz = nz + tangent[2] * su + bz * sv
            data.extend((px, py, pz, nx, ny, nz,
                         tangent[0], tangent[1], tangent[2], u, 1.0 - v))
    return data


_MESH_CACHE = {}


def mesh_for(kind):
    """Vertex data for 'sphere' or 'cube', built once per process."""
    if kind not in _MESH_CACHE:
        _MESH_CACHE[kind] = _sphere() if kind == "sphere" else _cube()
    return _MESH_CACHE[kind]


# ---- tiny matrix helpers (no numpy) ---------------------------------------

def _mat_mul(a, b):
    out = [0.0] * 16
    for r in range(4):
        for c in range(4):
            out[r * 4 + c] = sum(a[r * 4 + k] * b[k * 4 + c] for k in range(4))
    return out


def _perspective(fov_deg, aspect, near, far):
    f = 1.0 / math.tan(math.radians(fov_deg) * 0.5)
    return [f / aspect, 0, 0, 0,
            0, f, 0, 0,
            0, 0, (far + near) / (near - far), -1.0,
            0, 0, (2 * far * near) / (near - far), 0]


def _look_at(eye, target, up):
    fx, fy, fz = (target[i] - eye[i] for i in range(3))
    fl = math.sqrt(fx * fx + fy * fy + fz * fz) or 1.0
    fx, fy, fz = fx / fl, fy / fl, fz / fl
    sx = fy * up[2] - fz * up[1]
    sy = fz * up[0] - fx * up[2]
    sz = fx * up[1] - fy * up[0]
    sl = math.sqrt(sx * sx + sy * sy + sz * sz) or 1.0
    sx, sy, sz = sx / sl, sy / sl, sz / sl
    ux = sy * fz - sz * fy
    uy = sz * fx - sx * fz
    uz = sx * fy - sy * fx
    return [sx, ux, -fx, 0,
            sy, uy, -fy, 0,
            sz, uz, -fz, 0,
            -(sx * eye[0] + sy * eye[1] + sz * eye[2]),
            -(ux * eye[0] + uy * eye[1] + uz * eye[2]),
            (fx * eye[0] + fy * eye[1] + fz * eye[2]), 1]


# ===========================================================================
# The runner
# ===========================================================================

class MapRunner(object):
    """Owns the context, the programs, the FBOs and the source texture.

    Created lazily on first use and shared by the whole tab. Every method here
    must be called from the GUI thread.
    """

    def __init__(self):
        self._ctx = None
        self._surface = None
        self._fn = None
        self._programs = {}
        self._fbos = {}
        self._vao = None
        self._mesh_vao = None
        self._mesh_buf = {}
        self._src_tex = None
        self._src_size = (0, 0)
        self._max_texture = 8192
        self._current = False

    # ------------------------------------------------------------- context

    def ensure(self):
        """Create the context on first use. Raises GLUnavailable."""
        if self._ctx is not None:
            return
        fmt = QSurfaceFormat()
        fmt.setVersion(3, 3)
        fmt.setProfile(QSurfaceFormat.CoreProfile)
        fmt.setRenderableType(QSurfaceFormat.OpenGL)
        fmt.setDepthBufferSize(24)

        surface = QOffscreenSurface()
        surface.setFormat(fmt)
        surface.create()
        if not surface.isValid():
            raise GLUnavailable("this machine would not give us an offscreen "
                                "drawing surface")
        ctx = QOpenGLContext()
        ctx.setFormat(fmt)
        if not ctx.create():
            raise GLUnavailable("OpenGL 3.3 is not available here")
        if not ctx.makeCurrent(surface):
            raise GLUnavailable("the OpenGL context could not be made current")
        version = ctx.format().version()
        if version < (3, 3):
            ctx.doneCurrent()
            raise GLUnavailable("OpenGL %d.%d is too old — 3.3 is needed"
                                % version)
        self._surface = surface
        self._ctx = ctx
        self._fn = ctx.functions()
        self._current = True
        try:
            self._max_texture = int(self._fn.glGetIntegerv(GL_MAX_TEXTURE_SIZE))
        except Exception:                                     # noqa: BLE001
            self._max_texture = 8192
        vao = QOpenGLVertexArrayObject()
        vao.create()
        self._vao = vao

    @property
    def max_texture(self):
        return self._max_texture

    def _bind(self):
        """Make our context current. ⚠ Anything else in the app that used GL
        would have swapped it out from under us; nothing does today, and this
        is what makes that assumption cheap to hold rather than load-bearing."""
        self.ensure()
        if not self._ctx.makeCurrent(self._surface):
            raise GLUnavailable("lost the OpenGL context")

    def release(self):
        """Drop everything. Called when the tab is destroyed."""
        if self._ctx is None:
            return
        try:
            self._ctx.makeCurrent(self._surface)
            for fbo in self._fbos.values():
                del fbo
            self._fbos.clear()
            if self._src_tex is not None:
                self._src_tex.destroy()
                self._src_tex = None
            self._ctx.doneCurrent()
        except Exception:                                     # noqa: BLE001
            pass

    # ------------------------------------------------------------ programs

    def _program(self, name, vertex, fragment):
        prog = self._programs.get(name)
        if prog is not None:
            return prog
        prog = QOpenGLShaderProgram()
        if not prog.addShaderFromSourceCode(QOpenGLShader.Vertex, vertex):
            raise GLUnavailable("vertex shader %s: %s" % (name, prog.log()))
        if not prog.addShaderFromSourceCode(QOpenGLShader.Fragment, fragment):
            raise GLUnavailable("fragment shader %s: %s" % (name, prog.log()))
        if not prog.link():
            raise GLUnavailable("shader %s did not link: %s" % (name, prog.log()))
        self._programs[name] = prog
        return prog

    _FRAGMENTS = {
        "normal": FS_NORMAL, "height": FS_HEIGHT, "bump": FS_BUMP,
        "roughness": FS_ROUGH, "ao": FS_AO, "metallic": FS_METAL,
        "flat": FS_FLAT, "crop": FS_CROP, "blur1d": FS_BLUR1D,
        "equalize": FS_EQUALIZE, "offset": FS_OFFSET_BLEND,
        "downscale": FS_DOWNSCALE,
    }

    def _full(self, name):
        return self._program(name, VS_FULLSCREEN, self._FRAGMENTS[name])

    # ---------------------------------------------------------------- FBOs

    def _fbo(self, key, width, height, deep=False, depth=False):
        """A cached framebuffer. ⚠ CACHED BY KEY, not allocated per render:
        a 4096^2 FBO is 64 MB of VRAM and allocating one per slider tick is
        both slow and a fragmentation source."""
        entry = self._fbos.get(key)
        if entry is not None and entry.size() == QSize(width, height):
            return entry
        fmt = QOpenGLFramebufferObjectFormat()
        if deep:
            fmt.setInternalTextureFormat(GL_RGBA16)
        if depth:
            fmt.setAttachment(QOpenGLFramebufferObject.Depth)
        fbo = QOpenGLFramebufferObject(QSize(width, height), fmt)
        self._fbos[key] = fbo
        return fbo

    def _draw_full(self):
        self._vao.bind()
        self._fn.glDrawArrays(GL_TRIANGLES, 0, 3)
        self._vao.release()

    def _bind_texture(self, unit, texture_id, repeat=False):
        fn = self._fn
        fn.glActiveTexture(GL_TEXTURE0 + unit)
        fn.glBindTexture(GL_TEXTURE_2D, texture_id)
        wrap = GL_REPEAT if repeat else GL_CLAMP_TO_EDGE
        fn.glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, wrap)
        fn.glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, wrap)
        fn.glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR)
        fn.glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR)

    # -------------------------------------------------------------- source

    def set_source(self, image):
        """Upload the source once. Returns (width, height).

        ⚠ Uploaded as plain bytes with NO sRGB tag — see the module docstring.
        """
        self._bind()
        if image.isNull():
            raise ValueError("empty image")
        w, h = image.width(), image.height()
        if max(w, h) > self._max_texture:
            raise GLUnavailable(
                "%dx%d is larger than this GPU's %d px texture limit"
                % (w, h, self._max_texture))
        # ⚠⚠ **MIRRORED ON UPLOAD, AND EVERYTHING DEPENDS ON IT.** OpenGL's
        # framebuffer origin is bottom-left and a QImage's is top-left, so
        # uploading a QImage as-is puts every generated map upside down.
        # Mirroring HERE (rather than flipping uv in a shader) makes source
        # space and framebuffer space the same space, which keeps the multi-
        # pass chains — seamless especially, where an FBO's texture is read by
        # the next pass — consistent for free; flipping in the vertex shader
        # would flip each intermediate again.
        #
        # Measured, not assumed: `texmaps_gl_test.py` renders a source with a
        # bright strip along its TOP and asserts the strip is at the top of
        # the map. That check caught this exact bug — and note that the
        # normal-map cross-check did NOT, because its test gradient has a
        # constant slope, which a flip leaves unchanged.
        converted = image.convertToFormat(QImage.Format_RGBA8888).mirrored(
            False, True)
        if self._src_tex is not None:
            self._src_tex.destroy()
        tex = QOpenGLTexture(QOpenGLTexture.Target2D)
        tex.setFormat(QOpenGLTexture.RGBA8_UNorm)
        tex.setSize(w, h)
        tex.setMipLevels(1)
        tex.allocateStorage()
        tex.setData(QOpenGLTexture.RGBA, QOpenGLTexture.UInt8,
                    converted.constBits())
        tex.setWrapMode(QOpenGLTexture.ClampToEdge)
        tex.setMinificationFilter(QOpenGLTexture.Linear)
        tex.setMagnificationFilter(QOpenGLTexture.Linear)
        self._src_tex = tex
        self._src_size = (w, h)
        # a new source invalidates every derived buffer's contents
        return w, h

    @property
    def source_size(self):
        return self._src_size

    def has_source(self):
        return self._src_tex is not None

    # --------------------------------------------------------------- maps

    def _render_map_to(self, key, params, fbo, source_id, source_size):
        """Run one map's shader into *fbo*, reading *source_id*."""
        fn = self._fn
        prog = self._full(key)
        fbo.bind()
        fn.glViewport(0, 0, fbo.width(), fbo.height())
        fn.glDisable(GL_DEPTH_TEST)
        prog.bind()
        self._bind_texture(0, source_id)
        prog.setUniformValue1i(prog.uniformLocation("uSrc"), 0)
        prog.setUniformValue(prog.uniformLocation("uTexel"),
                             1.0 / source_size[0], 1.0 / source_size[1])
        self._set_map_uniforms(prog, key, params)
        self._draw_full()
        prog.release()
        fbo.release()

    def _set_map_uniforms(self, prog, key, params):
        p = dict(defaults_for(key))
        p.update(params or {})

        def f(name, value):
            prog.setUniformValue1f(prog.uniformLocation(name), float(value))

        def i(name, value):
            prog.setUniformValue1i(prog.uniformLocation(name), int(value))

        # the shared levels/invert block — every map's FS_COMMON declares them
        f("uBlack", p.get("black", 0.0))
        f("uWhite", p.get("white", 1.0))
        f("uGamma", p.get("gamma", 1.0))
        i("uInvert", 1 if p.get("invert") else 0)

        if key == "normal":
            f("uStrength", p["strength"])
            f("uDetail", p["detail"])
            f("uBlur", p["blur"])
            f("uSharpen", p["sharpen"])
            i("uScharr", 1 if int(p["operator"]) == 1 else 0)
            i("uInvertY", 1 if p["invert_y"] else 0)
        elif key == "height":
            f("uContrast", p["contrast"])
            f("uBrightness", p["brightness"])
            f("uSmoothing", p["smoothing"])
        elif key == "bump":
            f("uStrength", p["strength"])
            f("uContrast", p["contrast"])
            f("uBrightness", p["brightness"])
            f("uBlur", p["blur"])
        elif key == "roughness":
            f("uContrast", p["contrast"])
            f("uBrightness", p["brightness"])
            f("uSmoothing", p["smoothing"])
            f("uMin", p["rough_min"])
            f("uMax", p["rough_max"])
            i("uChannel", int(p["channel"]))
            i("uBrightSmooth", 1 if p["bright_smooth"] else 0)
        elif key == "ao":
            f("uRadius", p["radius"])
            f("uIntensity", p["intensity"])
            f("uFalloff", p["falloff"])
            f("uAmount", p["amount"])
        elif key == "metallic":
            f("uThreshold", p["threshold"])
            f("uSoftness", p["softness"])
            f("uSmoothing", p["smoothing"])
            f("uNonMetal", p["non_metal"])
            f("uMetal", p["metal"])
            i("uMode", int(p["mode"]))

    def map_texture(self, key, params, size, source_id=None, source_size=None):
        """Render a map and leave it on the GPU. Returns the texture id.

        This is what the lit preview binds — reading a map back to a QImage
        just to upload it again is the obvious waste in a pipeline like this.
        """
        self._bind()
        src_id = source_id if source_id is not None else self._src_tex.textureId()
        src_size = source_size or self._src_size
        w, h = size
        fbo = self._fbo(("map", key), w, h)
        self._render_map_to(key, params, fbo, src_id, src_size)
        return fbo.texture()

    def render_map(self, key, params, size=None, deep=False,
                   source_id=None, source_size=None):
        """Render a map and read it back as a QImage.

        `deep=True` renders into an RGBA16 buffer, which is what makes a real
        16-bit height PNG possible without touching a single pixel in Python.
        """
        self._bind()
        src_id = source_id if source_id is not None else self._src_tex.textureId()
        src_size = source_size or self._src_size
        w, h = size or src_size
        fbo = self._fbo(("read", key, bool(deep)), w, h, deep=deep)
        self._render_map_to(key, params, fbo, src_id, src_size)
        image = fbo.toImage()
        if deep:
            return image.convertToFormat(QImage.Format_Grayscale16)
        if MAPS[key]["out"] == "gray":
            return image.convertToFormat(QImage.Format_Grayscale8)
        return image.convertToFormat(QImage.Format_RGB888)

    # ------------------------------------------------------------ seamless

    def _stats(self, texture_id, size):
        """Mean colour of the image and of its four edge bands.

        ⚠ Read off a 64x64 box-downscale rather than the full image: the four
        band means and the overall mean are soft statistics, and reading 4 096
        pixels instead of 16.7 million is the difference between a live slider
        and a stutter. The outermost row/column of that grid is a band of
        size/64 source pixels — about 16 px at 1024, against the 14 px the
        original uses. Documented rather than hidden: it is within noise for
        what the number is used for.
        """
        fn = self._fn
        taps = max(1, min(8, size[0] // STATS_SIZE))
        fbo = self._fbo(("stats",), STATS_SIZE, STATS_SIZE)
        prog = self._full("downscale")
        fbo.bind()
        fn.glViewport(0, 0, STATS_SIZE, STATS_SIZE)
        fn.glDisable(GL_DEPTH_TEST)
        prog.bind()
        self._bind_texture(0, texture_id)
        prog.setUniformValue1i(prog.uniformLocation("uSrc"), 0)
        prog.setUniformValue(prog.uniformLocation("uStep"),
                             1.0 / size[0], 1.0 / size[1])
        prog.setUniformValue1i(prog.uniformLocation("uTaps"), taps)
        self._draw_full()
        prog.release()
        fbo.release()
        img = fbo.toImage().convertToFormat(QImage.Format_RGB888)

        # ⚠ **THE REDUCTION IS QT'S, NOT PYTHON'S.** This began as one pass
        # over the 64x64 grid in Python — 4 096 iterations of a dozen float
        # operations — and profiling put it at **1.0 ms per seamless preview,
        # 36 % of that path's CPU time and the single largest pure-Python cost
        # in the tab**. Scaling to 1x1 with a smooth transform is the same box
        # average done in C++: five scale calls instead of four thousand
        # interpreter loops. Measured 1.00 ms -> 0.05 ms.
        n = STATS_SIZE

        def mean_of(image):
            one = image.scaled(1, 1, Qt.IgnoreAspectRatio,
                               Qt.SmoothTransformation)
            colour = one.pixelColor(0, 0)
            return [colour.redF(), colour.greenF(), colour.blueF()]

        mean = mean_of(img)
        left = mean_of(img.copy(0, 0, 1, n))
        right = mean_of(img.copy(n - 1, 0, 1, n))
        top = mean_of(img.copy(0, 0, n, 1))
        bottom = mean_of(img.copy(0, n - 1, n, 1))
        mean_l = 0.2126 * mean[0] + 0.7152 * mean[1] + 0.0722 * mean[2]
        return mean_l, left, right, top, bottom

    def _seamless_fbo(self, params):
        """Crop -> equalize -> offset+mirror-blend twice. Returns (fbo, size)."""
        self._bind()
        fn = self._fn
        p = dict(defaults_for("seamless"))
        p.update(params or {})
        out = int(max(64, min(self._max_texture, int(p["size"]))))
        sw, sh = self._src_size
        short = min(sw, sh)
        span_x = short / float(sw)
        span_y = short / float(sh)
        ox = (1.0 - span_x) * float(p["crop_x"])
        oy = (1.0 - span_y) * float(p["crop_y"])

        # --- 1. crop + scale
        crop = self._fbo(("sm", "crop"), out, out)
        prog = self._full("crop")
        crop.bind()
        fn.glViewport(0, 0, out, out)
        fn.glDisable(GL_DEPTH_TEST)
        prog.bind()
        self._bind_texture(0, self._src_tex.textureId())
        prog.setUniformValue1i(prog.uniformLocation("uSrc"), 0)
        prog.setUniformValue(prog.uniformLocation("uOrigin"), ox, oy)
        # a square crop of the SHORT side, so a non-square photo keeps its
        # aspect instead of being squashed into the tile
        prog.setUniformValue1f(prog.uniformLocation("uSpan"), min(span_x, span_y))
        self._draw_full()
        prog.release()
        crop.release()

        current = crop
        do_light = bool(p["eq_light"])
        do_edges = bool(p["eq_edges"])
        if do_light or do_edges:
            blur_src = current
            blur_a = self._fbo(("sm", "blurA"), out, out)
            blur_b = self._fbo(("sm", "blurB"), out, out)
            if do_light:
                radius = max(1, min(24, int(round(out / 56.0))))
                for target, source, step in (
                        (blur_a, blur_src, (1.0 / out, 0.0)),
                        (blur_b, blur_a, (0.0, 1.0 / out))):
                    prog = self._full("blur1d")
                    target.bind()
                    fn.glViewport(0, 0, out, out)
                    prog.bind()
                    self._bind_texture(0, source.texture())
                    prog.setUniformValue1i(prog.uniformLocation("uSrc"), 0)
                    prog.setUniformValue(prog.uniformLocation("uStep"), *step)
                    prog.setUniformValue1i(prog.uniformLocation("uRadius"), radius)
                    self._draw_full()
                    prog.release()
                    target.release()
            mean_l, left, right, top, bottom = self._stats(current.texture(),
                                                           (out, out))
            eq = self._fbo(("sm", "eq"), out, out)
            prog = self._full("equalize")
            eq.bind()
            fn.glViewport(0, 0, out, out)
            prog.bind()
            self._bind_texture(0, current.texture())
            self._bind_texture(1, blur_b.texture())
            prog.setUniformValue1i(prog.uniformLocation("uSrc"), 0)
            prog.setUniformValue1i(prog.uniformLocation("uBlur"), 1)
            prog.setUniformValue1f(prog.uniformLocation("uMean"), mean_l)
            prog.setUniformValue1i(prog.uniformLocation("uDoLight"),
                                   1 if do_light else 0)
            prog.setUniformValue1i(prog.uniformLocation("uDoEdges"),
                                   1 if do_edges else 0)
            prog.setUniformValue(prog.uniformLocation("uDiffX"),
                                 right[0] - left[0], right[1] - left[1],
                                 right[2] - left[2])
            prog.setUniformValue(prog.uniformLocation("uDiffY"),
                                 bottom[0] - top[0], bottom[1] - top[1],
                                 bottom[2] - top[2])
            band = 14.0 / out
            prog.setUniformValue1f(prog.uniformLocation("uBandX"), band)
            prog.setUniformValue1f(prog.uniformLocation("uBandY"), band)
            self._draw_full()
            prog.release()
            eq.release()
            current = eq

        # --- 4. offset + mirror blend, twice (the second pass is narrower and
        # weaker: it cleans up what the first pass's own blend introduced)
        width = float(p["blend"])
        strength = float(p["strength"]) / 100.0
        passes = ((width, strength, 1),
                  (max(10.0, width / 3.0), min(0.5, 0.7 * strength), 0))
        for index, (w_px, s, offset) in enumerate(passes):
            target = self._fbo(("sm", "blend%d" % index), out, out)
            prog = self._full("offset")
            target.bind()
            fn.glViewport(0, 0, out, out)
            prog.bind()
            self._bind_texture(0, current.texture())
            prog.setUniformValue1i(prog.uniformLocation("uSrc"), 0)
            prog.setUniformValue(prog.uniformLocation("uTexel"),
                                 1.0 / out, 1.0 / out)
            prog.setUniformValue1f(prog.uniformLocation("uWidth"), w_px / out)
            prog.setUniformValue1f(prog.uniformLocation("uStrength"), s)
            prog.setUniformValue1i(prog.uniformLocation("uOffset"), offset)
            self._draw_full()
            prog.release()
            target.release()
            current = target
        return current, (out, out)

    def render_seamless_texture(self, params):
        """The tiling image, left on the GPU as a texture id."""
        fbo, size = self._seamless_fbo(params)
        return fbo.texture(), size

    def render_seamless(self, params):
        """The tiling image, read back. ⚠ Reads the fbo the pipeline actually
        ENDED on rather than a hard-coded key — the pass list above is meant to
        be edited, and a name pinned here would go on silently returning the
        second-to-last result."""
        fbo, _size = self._seamless_fbo(params)
        return fbo.toImage().convertToFormat(QImage.Format_RGB888)

    # ------------------------------------------------------------- preview

    def render_flat(self, texture_id, size, tiling=1.0):
        """The 2D view: a map, tiled."""
        self._bind()
        fn = self._fn
        w, h = size
        fbo = self._fbo(("view", "flat"), w, h)
        prog = self._full("flat")
        fbo.bind()
        fn.glViewport(0, 0, w, h)
        fn.glDisable(GL_DEPTH_TEST)
        prog.bind()
        self._bind_texture(0, texture_id, repeat=True)
        prog.setUniformValue1i(prog.uniformLocation("uSrc"), 0)
        prog.setUniformValue1f(prog.uniformLocation("uTiling"), float(tiling))
        self._draw_full()
        prog.release()
        fbo.release()
        return fbo.toImage().convertToFormat(QImage.Format_RGB888)

    def _mesh_program(self):
        return self._program("mesh", VS_MESH, FS_MESH)

    def _mesh_buffer(self, kind):
        entry = self._mesh_buf.get(kind)
        if entry is not None:
            return entry
        data = mesh_for(kind)
        buf = QOpenGLBuffer(QOpenGLBuffer.VertexBuffer)
        buf.create()
        buf.bind()
        raw = data.tobytes()
        buf.allocate(raw, len(raw))
        buf.release()
        entry = (buf, len(data) // 11)
        self._mesh_buf[kind] = entry
        return entry

    def render_mesh(self, kind, textures, size, preview, rot=(0.5, 0.25)):
        """The lit preview. `textures` is {slot: texture_id} — any missing slot
        is simply not used, so a preview works with one map or all five."""
        self._bind()
        fn = self._fn
        w, h = size
        fbo = self._fbo(("view", "mesh"), w, h, depth=True)
        prog = self._mesh_program()
        buf, count = self._mesh_buffer(kind)

        if self._mesh_vao is None:
            vao = QOpenGLVertexArrayObject()
            vao.create()
            self._mesh_vao = vao

        fbo.bind()
        fn.glViewport(0, 0, w, h)
        fn.glEnable(GL_DEPTH_TEST)
        fn.glClearColor(0.0, 0.0, 0.0, 0.0)
        fn.glClear(GL_COLOR_BUFFER_BIT | GL_DEPTH_BUFFER_BIT)
        prog.bind()
        self._mesh_vao.bind()
        buf.bind()
        stride = 11 * 4
        for loc, offset, tuple_size in ((0, 0, 3), (1, 12, 3), (2, 24, 3),
                                        (3, 36, 2)):
            prog.enableAttributeArray(loc)
            prog.setAttributeBuffer(loc, GL_FLOAT, offset, tuple_size, stride)

        yaw, pitch = rot
        dist = 3.4 if kind == "sphere" else 4.4
        eye = (math.cos(pitch) * math.sin(yaw) * dist,
               math.sin(pitch) * dist,
               math.cos(pitch) * math.cos(yaw) * dist)
        mvp = _mat_mul(_look_at(eye, (0.0, 0.0, 0.0), (0.0, 1.0, 0.0)),
                       _perspective(38.0, w / float(h), 0.1, 40.0))
        prog.setUniformValue(prog.uniformLocation("uEye"), *eye)
        loc = prog.uniformLocation("uMVP")
        # column-major, the order Qt wants
        from PySide6.QtGui import QMatrix4x4, QMatrix3x3
        m = QMatrix4x4(mvp[0], mvp[4], mvp[8], mvp[12],
                       mvp[1], mvp[5], mvp[9], mvp[13],
                       mvp[2], mvp[6], mvp[10], mvp[14],
                       mvp[3], mvp[7], mvp[11], mvp[15])
        prog.setUniformValue(loc, m)
        prog.setUniformValue(prog.uniformLocation("uNormalMat"), QMatrix3x3())

        slots = (("uBase", "base", 0), ("uNormal", "normal", 1),
                 ("uRough", "roughness", 2), ("uAO", "ao", 3),
                 ("uMetal", "metallic", 4))
        for uniform, name, unit in slots:
            tex = textures.get(name)
            self._bind_texture(unit, tex if tex else 0, repeat=True)
            prog.setUniformValue1i(prog.uniformLocation(uniform), unit)
            if name != "base":
                prog.setUniformValue1i(
                    prog.uniformLocation("uHas" + {
                        "normal": "Normal", "roughness": "Rough",
                        "ao": "AO", "metallic": "Metal"}[name]),
                    1 if tex else 0)
        height_tex = textures.get("height")
        self._bind_texture(5, height_tex if height_tex else 0, repeat=True)
        prog.setUniformValue1i(prog.uniformLocation("uHeight"), 5)
        prog.setUniformValue1i(prog.uniformLocation("uHasHeight"),
                               1 if height_tex else 0)

        tiling = float(int(preview.get("tiling", 1)) + 1)
        prog.setUniformValue1f(prog.uniformLocation("uTiling"), tiling)
        prog.setUniformValue1f(prog.uniformLocation("uDisplace"),
                               float(preview.get("displacement", 0.0)))
        prog.setUniformValue1f(prog.uniformLocation("uNormalDepth"),
                               float(preview.get("normal_depth", 1.0)))
        prog.setUniformValue1f(prog.uniformLocation("uAOIntensity"),
                               float(preview.get("ao_intensity", 1.0)))

        fn.glDrawArrays(GL_TRIANGLES, 0, count)
        buf.release()
        self._mesh_vao.release()
        prog.release()
        fn.glDisable(GL_DEPTH_TEST)
        fbo.release()
        return fbo.toImage()


_RUNNER = None


def runner():
    """The one runner, created on first use."""
    global _RUNNER
    if _RUNNER is None:
        _RUNNER = MapRunner()
    return _RUNNER


def available():
    """True if a context can be made. Never raises — the tab asks this to
    decide whether to disable itself with a sentence."""
    try:
        runner().ensure()
        return True
    except Exception:                                         # noqa: BLE001
        return False
