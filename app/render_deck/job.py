"""Data model for a single render job."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional

from .util import format_bytes as _format_bytes


class JobStatus(str, Enum):
    QUEUED = "Queued"
    PROBING = "Reading range…"
    RENDERING = "Rendering"
    PAUSED = "Paused"
    DONE = "Done"
    FAILED = "Failed"
    CANCELLED = "Cancelled"


@dataclass
class RenderJob:
    filepath: str
    # The user's explicit override. None => use the range saved in the .blend.
    # NOTE: the probe must never write these — it fills probed_start/probed_end
    # instead, so "use the .blend's range" stays the user's choice and survives
    # a restart (and picks up a range edited in Blender since).
    start_frame: Optional[int] = None
    end_frame: Optional[int] = None
    engine: str = ""   # "" => use the engine stored in the .blend file
    camera: str = ""   # "" => use the scene's active camera
    # Collections to leave OUT of this job's render (Marty, 2026-08-04).
    # Per job, not per queue: the whole point is that the same .blend can be
    # rendered twice with different things switched off.
    # ⚠ Applied with hide_render at render time and NEVER written back to the
    # .blend — the file on disk is untouched, so the same job list can sit
    # alongside someone else working in that file.
    disabled_collections: List[str] = field(default_factory=list)
    # What the probe found in the file, so the picker can offer real names
    # instead of asking the user to type them.
    probed_collections: List[str] = field(default_factory=list)

    status: JobStatus = JobStatus.QUEUED
    frames_total: int = 0
    frames_done: int = 0
    sample_done: int = 0     # progress within the current frame (e.g. Cycles samples)
    sample_total: int = 0
    # (Console output is stored per-job on disk by joblog.JobLogStore, not here.)
    # Absolute frame to resume from (set when paused; survives app restart so a
    # paused job comes back and continues where it left off).
    resume_frame: Optional[int] = None
    # Per-frame render timing (runtime only, not persisted).
    frame_started_at: Optional[float] = None    # monotonic clock at current frame start
    last_frame_seconds: Optional[float] = None  # how long the last finished frame took

    # ---- values read out of the .blend by a probe (cached, see probe_fresh) --
    # The frame range saved in the file; only used when the user left the
    # Start/End columns blank. Runtime only — never persisted, so the file is
    # always re-read and a range changed in Blender is picked up.
    probed_start: Optional[int] = None
    probed_end: Optional[int] = None
    # Resolved output directory (scene.render.filepath). Persisted, so clicking
    # "Open export folder" after a restart doesn't have to launch Blender again.
    export_dir: Optional[str] = None
    # Cycles' per-frame Time Limit in seconds (scene.cycles.time_limit; 0/None =
    # no limit). Persisted so the UI can show it before a render starts.
    time_limit: Optional[float] = None
    # Descriptive info shown in the job table, all read by the same probe.
    blend_version: Optional[list] = None    # version the FILE was saved with
    file_engine: str = ""                   # engine saved in the .blend
    packed: Optional[bool] = None           # has packed images/sounds/fonts
    samples: Optional[int] = None           # Cycles max samples
    eevee_samples: Optional[int] = None     # EEVEE render samples
    adaptive: Optional[bool] = None         # Cycles adaptive sampling on
    noise: Optional[float] = None           # Cycles noise threshold
    fps: Optional[float] = None
    # Modification time of the .blend when the values above were read. The cache
    # is only trusted while this still matches the file on disk.
    probe_mtime: Optional[float] = None

    # Runtime-only: this run is a solid-mode (Workbench) H.264 preview, not the
    # real render — never persisted, a preview must not survive a restart.
    preview: bool = False

    # ---- background playblast (Studio Library's 🎬, run off the live session)
    # Runtime-only, and deliberately NOT in session._FIELDS: `filepath` is a
    # throwaway snapshot of the user's scene that gets deleted the moment the
    # render ends, so restoring one of these jobs after a restart would only
    # queue a .blend that no longer exists.
    # A non-empty playblast_out is what MAKES a job a playblast.
    playblast_out: Optional[str] = None   # the mp4 the user actually asked for
    playblast_pct: int = 50               # resolution %, from the dialog
    playblast_label: str = ""             # shown in the File column
    temp_blend: bool = False              # delete `filepath` when the job ends

    @property
    def is_playblast(self) -> bool:
        return bool(self.playblast_out)

    # ---- effective frame range --------------------------------------
    @property
    def render_start(self) -> Optional[int]:
        """First frame to render: the user's override, else the .blend's own."""
        return self.start_frame if self.start_frame is not None else self.probed_start

    @property
    def render_end(self) -> Optional[int]:
        """Last frame to render: the user's override, else the .blend's own."""
        return self.end_frame if self.end_frame is not None else self.probed_end

    @property
    def first_frame(self) -> int:
        """`render_start` with a safe default — the base for all frame math."""
        s = self.render_start
        return s if s is not None else 1

    def file_mtime(self) -> Optional[float]:
        try:
            return os.path.getmtime(self.filepath)
        except OSError:
            return None

    def probe_fresh(self) -> bool:
        """True when the cached probe values still describe the file on disk.
        Saving the .blend in Blender invalidates them."""
        if self.probe_mtime is None:
            return False
        mt = self.file_mtime()
        return mt is not None and abs(mt - self.probe_mtime) < 1e-6

    def clear_probe_cache(self) -> None:
        self.probed_start = self.probed_end = None
        self.export_dir = None
        self.time_limit = None
        self.blend_version = None
        self.file_engine = ""
        self.packed = self.samples = self.eevee_samples = None
        self.adaptive = self.noise = self.fps = None
        self.probe_mtime = None

    def apply_probe(self, info: dict, mtime=None) -> None:
        """Fold a probe's JSON payload into this job. The frame range lands in
        probed_* — NEVER in start_frame/end_frame — so "use the .blend's range"
        stays the user's choice and is re-read on every run."""
        if info.get("start") is not None and info.get("end") is not None:
            self.probed_start, self.probed_end = int(info["start"]), int(info["end"])
            # Only trust the cache when the payload was complete enough to be
            # worth keeping.
            self.probe_mtime = mtime
        if info.get("out"):
            self.export_dir = info["out"]
        for key, attr, cast in (
            ("time_limit", "time_limit", float), ("version", "blend_version", list),
            ("engine", "file_engine", str), ("packed", "packed", bool),
            ("samples", "samples", int), ("eevee_samples", "eevee_samples", int),
            ("adaptive", "adaptive", bool), ("noise", "noise", float),
            ("fps", "fps", float),
        ):
            if info.get(key) is not None:
                try:
                    setattr(self, attr, cast(info[key]))
                except (TypeError, ValueError):
                    pass
        if isinstance(info.get("collections"), list):
            self.probed_collections = [str(n) for n in info["collections"]]
            # ⚠ A collection RENAMED OR DELETED in Blender since the job was set
            # up must not stay on the disable list: the render would silently
            # not hide it, and the queue would still claim it had. Drop anything
            # the file no longer has.
            known = set(self.probed_collections)
            self.disabled_collections = [n for n in self.disabled_collections
                                         if n in known]

    # ---- display helpers for the job table --------------------------
    @property
    def version_label(self) -> str:
        """Blender version the .blend was saved with, as 'major.minor'."""
        v = self.blend_version
        if not v or len(v) < 2:
            return "—"
        return f"{v[0]}.{v[1]}"

    @property
    def version_tooltip(self) -> str:
        v = self.blend_version
        return ("Saved with Blender " + ".".join(str(x) for x in v)) if v else ""

    @property
    def size_label(self) -> str:
        try:
            return _format_bytes(os.path.getsize(self.filepath))
        except OSError:
            return "—"

    @property
    def packed_label(self) -> str:
        if self.packed is None:
            return "—"
        return "packed" if self.packed else "linked"

    @property
    def samples_label(self) -> str:
        """Max samples for whichever engine this job will actually use."""
        engine = self.engine or self.file_engine
        if engine == "BLENDER_EEVEE":
            return str(self.eevee_samples) if self.eevee_samples else "—"
        if engine == "BLENDER_WORKBENCH":
            return "—"
        return str(self.samples) if self.samples else "—"

    @property
    def noise_label(self) -> str:
        """Cycles noise threshold — meaningless unless adaptive sampling is on."""
        engine = self.engine or self.file_engine
        if engine and engine != "CYCLES":
            return "—"
        if self.adaptive is False:
            return "off"
        if not self.noise:
            return "—"
        return f"{self.noise:g}"

    @property
    def name(self) -> str:
        # A playblast's filepath is a temp snapshot with a machine-made name —
        # show what the user called it instead.
        return self.playblast_label or os.path.basename(self.filepath)

    @property
    def range_label(self) -> str:
        if self.start_frame is not None and self.end_frame is not None:
            return f"{self.start_frame}-{self.end_frame}"
        return "file default"

    @property
    def has_time_limit(self) -> bool:
        return bool(self.time_limit and self.time_limit > 0)

    @property
    def frame_fraction(self) -> float:
        """Progress of the current single frame, 0..1 (0 if unknown)."""
        if self.sample_total:
            return max(0.0, min(1.0, self.sample_done / self.sample_total))
        return 0.0

    def frame_progress(self, elapsed: Optional[float] = None):
        """How far through the current frame we are, as (fraction, time_capped).

        With a Cycles Time Limit the frame ends at whichever comes first — the
        sample count completing, or the clock running out — so the honest
        estimate is whichever of the two is further along. Returns (None, False)
        when nothing is known (e.g. EEVEE with no limit).
        """
        frac = self.frame_fraction if self.sample_total else None
        capped = False
        if self.has_time_limit and elapsed is not None:
            t = max(0.0, min(1.0, elapsed / float(self.time_limit)))
            if frac is None or t > frac:
                frac, capped = t, True
        return frac, capped

    @property
    def current_frame(self) -> int:
        """Absolute number of the frame being rendered right now."""
        return self.first_frame + max(self.frames_done - 1, 0)

    @property
    def progress_label(self) -> str:
        if self.status in (JobStatus.RENDERING, JobStatus.PAUSED) and self.frames_total:
            return f"{self.frames_done}/{self.frames_total}"
        if self.status == JobStatus.DONE:
            return "100%"
        return "—"

    @property
    def progress_percent(self) -> int:
        if not self.frames_total:
            return 0
        return int(100 * self.frames_done / self.frames_total)
