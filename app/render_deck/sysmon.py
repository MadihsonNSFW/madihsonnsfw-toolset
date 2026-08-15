"""Lightweight system RAM + GPU VRAM sampling for the stat cards.

Both dependencies are optional and imported defensively — if psutil isn't
present RAM reads as unknown, and if there's no NVIDIA driver / nvidia-ml-py
VRAM reads as unknown (the cards just show "—"). Queries are cheap (no
subprocess), so they're safe to poll on the UI thread once a second.
"""
from __future__ import annotations

from typing import Optional, Tuple

try:
    import psutil
except Exception:  # pragma: no cover
    psutil = None  # type: ignore

# ⚠⚠ **NVML IS NOT TOUCHED UNTIL SOMETHING ASKS FOR VRAM.** This used to
# `import pynvml` and call `nvmlInit()` at module import — and this module is
# reached from `render_tools` -> `queue_tool`, which `main.py` imports at
# startup. So **every session paid 23 MB and 38 ms to initialise the NVIDIA
# management library**, including the ones that never open the Render Queue
# (measured 2026-08-15, PERF_PLAN.md). The stat cards call `vram()` on a timer
# that only starts with the tool, so deferring it costs the first poll a few
# milliseconds and nothing else.
_NVML = None
_nvml_tried = False


def _nvml():
    """The NVML module if this machine has a working NVIDIA stack, else None.

    Tried exactly once — a machine without the driver must not pay for the
    failed import on every poll.
    """
    global _NVML, _nvml_tried
    if _nvml_tried:
        return _NVML
    _nvml_tried = True
    try:
        import pynvml as mod
        mod.nvmlInit()
        if mod.nvmlDeviceGetCount() > 0:
            _NVML = mod
    except Exception:  # no NVIDIA driver / lib not installed / etc.
        _NVML = None
    return _NVML

# (used_bytes, total_bytes, percent)
MemStat = Optional[Tuple[int, int, float]]


def ram() -> MemStat:
    if psutil is None:
        return None
    try:
        m = psutil.virtual_memory()
        return (m.used, m.total, float(m.percent))
    except Exception:
        return None


def vram() -> MemStat:
    """VRAM usage of GPU 0 (whole-device, across all processes)."""
    nvml = _nvml()
    if nvml is None:
        return None
    try:
        h = nvml.nvmlDeviceGetHandleByIndex(0)
        mi = nvml.nvmlDeviceGetMemoryInfo(h)
        used, total = int(mi.used), int(mi.total)
        pct = (100.0 * used / total) if total else 0.0
        return (used, total, pct)
    except Exception:
        return None
