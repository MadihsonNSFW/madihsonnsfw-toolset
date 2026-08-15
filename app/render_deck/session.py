"""Persist the job list across runs — which .blend files were queued, their
per-job settings, and (for a paused job) the frame it stopped on — so the app
comes back exactly as it was left.

Stored as session.json next to settings.json (see util.data_dir)."""
from __future__ import annotations

import json
import os
from typing import List

from .job import JobStatus, RenderJob
from .util import data_dir

# Fields copied to/from disk. `log`, `sample_*` are runtime-only and skipped.
# start_frame/end_frame are the USER's range (None = "use the .blend's own"),
# so that choice survives a restart; the probed range is deliberately NOT saved
# — it's re-read from the file each run, which picks up a range edited since.
_FIELDS = ("filepath", "start_frame", "end_frame", "engine", "camera",
           # Persisted: which collections this job leaves out is a decision the
           # user made about this job, and losing it on restart would send a
           # render off with everything switched back on.
           "disabled_collections",
           "frames_total", "frames_done", "resume_frame",
           # Probe cache: saves relaunching Blender just to answer "where do
           # this job's frames go?" after a restart. `probe_mtime` ties it to
           # the .blend, so re-saving the file invalidates it (job.probe_fresh).
           "export_dir", "time_limit", "probe_mtime",
           "blend_version", "file_engine", "packed", "samples",
           "eevee_samples", "adaptive", "noise", "fps")


def _path() -> str:
    return os.path.join(data_dir(), "session.json")


def save_jobs(jobs: List[RenderJob]) -> None:
    payload = {"jobs": []}
    for j in jobs:
        d = {f: getattr(j, f) for f in _FIELDS}
        d["status"] = j.status.value
        payload["jobs"].append(d)
    try:
        with open(_path(), "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
    except OSError:
        pass


def load_jobs() -> List[RenderJob]:
    try:
        with open(_path(), "r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []

    jobs: List[RenderJob] = []
    for d in payload.get("jobs", []):
        fp = d.get("filepath")
        if not fp:
            continue
        job = RenderJob(filepath=fp)
        for f in _FIELDS:
            if f in d and f != "filepath":
                setattr(job, f, d[f])
        # Restore status; anything that was mid-flight becomes QUEUED again.
        try:
            status = JobStatus(d.get("status", JobStatus.QUEUED.value))
        except ValueError:
            status = JobStatus.QUEUED
        if status in (JobStatus.RENDERING, JobStatus.PROBING):
            status = JobStatus.QUEUED
        job.status = status
        # Drop a cache that no longer describes the file (edited/moved/deleted)
        # rather than opening a stale export folder or showing a stale limit.
        if not job.probe_fresh():
            job.clear_probe_cache()
        jobs.append(job)
    return jobs
