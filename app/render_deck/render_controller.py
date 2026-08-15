"""Runs the render queue by driving Blender's command line in background mode.

Each job goes through up to two Blender invocations:
  1. (optional) a fast 'probe' that opens the .blend and prints its frame range,
     so we can show an accurate progress bar. Skipped if the user set an explicit
     range on the job.
  2. the actual headless render (`-b file … -a` for animation, `-f n` for still).

We use QProcess so stdout streams live into the UI without freezing it.
"""
from __future__ import annotations

import os
import re
import time
from typing import List, Optional

from PySide6.QtCore import QObject, QProcess, Signal

from . import util
from .job import JobStatus, RenderJob
from .settings import Settings

# Blender prints the current frame while rendering. Format varies by build:
# classic "Fra:400 Mem:…" (no space) and newer "Fra: 400 | …" (with a space) —
# tolerate optional whitespace after the colon so both match.
_FRA_RE = re.compile(rb"Fra:\s*(\d+)")
# Within-frame progress. Cycles: "Sample 128/256"; some builds: "128/256 samples".
_SAMPLE_RE = re.compile(rb"Sample (\d+)/(\d+)|(\d+)/(\d+) samples")

# Cap crash auto-resume so a frame that instantly recrashes can't loop forever.
MAX_RESUME_ATTEMPTS = 20


class RenderController(QObject):
    log = Signal(str)                       # a general (non-job) log line
    job_log = Signal(object, str)           # (RenderJob, text) streamed output
    job_status = Signal(object)             # a RenderJob whose fields changed
    probe_done = Signal(object)             # a RenderJob whose probe cache was filled
    queue_finished = Signal()               # the whole queue is done/stopped
    paused_changed = Signal(bool)           # render paused (True) / resumed (False)

    def __init__(self, settings: Settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self._queue: List[RenderJob] = []
        self._current: Optional[RenderJob] = None
        self._proc: Optional[QProcess] = None
        self._stopping = False
        self._paused = False
        self._phase = "idle"  # "probe" | "render"
        self._attempt = 0            # crash-resume attempts for the current job
        self._resume_from: Optional[int] = None  # frame to restart at after a crash/pause
        self._probe_buf = b""        # probe stdout, parsed once the probe exits
        self._probe_mtime: Optional[float] = None  # .blend mtime when the probe ran

    # ---- public API -------------------------------------------------
    @property
    def running(self) -> bool:
        return self._proc is not None

    @property
    def current(self) -> Optional[RenderJob]:
        """The job in progress (rendering/probing, or held while paused);
        None when idle."""
        return self._current

    def remove_from_queue(self, job: RenderJob) -> bool:
        """Drop a still-pending job from the queue so it won't be rendered.
        Never removes the job currently in progress (`_current`). Returns True
        if the job was pending and got removed."""
        if job is self._current:
            return False
        try:
            self._queue.remove(job)
            return True
        except ValueError:
            return False

    @staticmethod
    def pending(jobs: List[RenderJob], first: Optional[RenderJob] = None
                ) -> List[RenderJob]:
        """The jobs Start would actually render: everything that isn't already
        finished. Failed/Cancelled ones are retried; Done ones are left alone so
        reopening the app and hitting Start continues the queue instead of
        re-rendering hours of finished frames. If *everything* is Done there's
        nothing to continue, so Start means "run the lot again".

        `first` (the row the user pressed Resume on) is moved to the front; the
        rest keep list order, so resuming one paused job still continues down
        the queue afterwards — including any other paused job, from its own
        frame."""
        todo = [j for j in jobs if j.status != JobStatus.DONE] or list(jobs)
        if first is not None and first in todo:
            todo = [first] + [j for j in todo if j is not first]
        elif first is not None:
            todo = [first] + todo          # e.g. resuming an already-Done job
        return todo

    def start(self, jobs: List[RenderJob], first: Optional[RenderJob] = None,
              preview: bool = False, only: bool = False) -> None:
        """Render the queue top to bottom, optionally starting at `first`.

        `preview` renders ONLY `first`, as a solid-mode H.264 video. `only`
        does the same one-job-and-stop for a job that already knows what it is
        (a background playblast) — it must never drag the rest of the user's
        queue along behind it."""
        # Pausing leaves the controller idle, so a fresh start must clear the
        # paused flag or the next process exit gets misread as another pause.
        self._paused = False
        self._stopping = False
        if preview or only:
            if first is None:
                return
            self._queue = [first]
        else:
            self._queue = self.pending(jobs, first)
        for j in self._queue:
            j.preview = preview
            # Leave a paused job (incl. one restored from a previous session)
            # exactly as it is: it keeps PAUSED plus its frames_done and
            # resume_frame, and _next_job picks that up when it reaches it. That
            # matters for jobs further down the queue too — starting scene B must
            # not quietly un-pause scene A and lose where it stopped. A preview
            # always starts over; it writes a different file entirely.
            if not preview and (j.status == JobStatus.PAUSED
                                and j.resume_frame is not None):
                continue
            j.frames_done = 0
            j.resume_frame = None
            j.status = JobStatus.QUEUED
            self.job_status.emit(j)
        self._next_job()

    def stop(self) -> None:
        self._stopping = True
        self._paused = False
        if self._proc is not None:
            self._emit_log("\n>>> Stopping — killing Blender process…\n")
            self._proc.kill()
        elif self._current is not None:
            # Defensive: a current job with no live process. Pausing no longer
            # leaves one behind (it goes idle), but abort cleanly if it happens.
            self._current.status = JobStatus.CANCELLED
            self.job_status.emit(self._current)
            self._current = None
            self.paused_changed.emit(False)
            self.queue_finished.emit()

    @property
    def paused(self) -> bool:
        return self._paused

    def hold_for_resume(self) -> None:
        """Called when the app is closing mid-render. Marks the in-progress job
        as a resumable PAUSED job — capturing the frame it reached — and stops
        Blender WITHOUT cancelling, so the next launch continues where it left
        off instead of restarting. No-op when idle or already paused (a paused
        job already carries its resume frame)."""
        job = self._current
        if job is None or self._paused:
            return
        if self._phase == "render":
            # Same frame math the pause/crash paths use.
            job.resume_frame = job.current_frame
            job.status = JobStatus.PAUSED
        # Tear down the live process without the stop() cancel path firing.
        if self._proc is not None:
            try:
                self._proc.finished.disconnect()
                self._proc.readyReadStandardOutput.disconnect()
                self._proc.errorOccurred.disconnect()
            except (TypeError, RuntimeError):
                pass
            self._proc.kill()
            self._proc = None

    def pause(self) -> None:
        """Stop the running render and go idle, leaving the job PAUSED with the
        frame it reached. The controller does NOT hold the job — that's what
        lets you pause one scene, render another, then come back and resume the
        first (see `start(first=…)`)."""
        if self._phase == "render" and self._proc is not None and not self._stopping:
            self._paused = True
            self._emit_log("\n>>> Pausing render…\n")
            self._proc.kill()

    # ---- queue driving ----------------------------------------------
    def _next_job(self) -> None:
        if self._stopping or not self._queue:
            self._current = None
            self.queue_finished.emit()
            return
        self._current = self._queue.pop(0)
        self._attempt = 0
        # Honour a stored resume point (from an in-session pause or one restored
        # from a previous run); consume it so a natural restart isn't offset.
        self._resume_from = self._current.resume_frame
        self._current.resume_frame = None
        # Probe when we don't know the whole range, or when what we cached no
        # longer matches the file on disk (it also carries the output folder and
        # the Cycles time limit, so a re-saved .blend refreshes all three).
        # A playblast snapshot was written seconds ago and the app already read
        # its range/fps off the live scene, so probing it is a pointless extra
        # Blender launch on a file that will be deleted either way.
        needs_probe = (self._current.render_start is None
                       or self._current.render_end is None
                       or (not self._current.is_playblast
                           and not self._current.probe_fresh()))
        if needs_probe:
            self._run_probe(self._current)
        else:
            self._current.frames_total = (self._current.render_end
                                          - self._current.render_start + 1)
            self._run_render(self._current)

    def _blender(self) -> str:
        return self.settings.blender_path

    def _emit_log(self, text: str) -> None:
        """Route a log line to the current job (so each file keeps its own
        console), or to the general log if no job is active."""
        if self._current is not None:
            self.job_log.emit(self._current, text)
        else:
            self.log.emit(text)

    def _spawn(self, args: List[str]) -> None:
        self._proc = QProcess(self)
        self._proc.setProcessChannelMode(QProcess.MergedChannels)
        self._proc.readyReadStandardOutput.connect(self._on_output)
        self._proc.finished.connect(self._on_finished)
        self._proc.errorOccurred.connect(self._on_error)
        self._emit_log(f"$ blender {' '.join(args)}\n")
        self._proc.start(self._blender(), args)

    def _run_probe(self, job: RenderJob) -> None:
        self._phase = "probe"
        job.status = JobStatus.PROBING
        # Start clean: a probe that was stopped mid-flight never got parsed, and
        # its leftovers must not be read as this file's answer.
        self._probe_buf = b""
        # Stamp the file we're about to read so probe_fresh() can tell later
        # whether the cache still describes it.
        self._probe_mtime = job.file_mtime()
        self.job_status.emit(job)
        self._spawn(util.probe_args(job.filepath))

    def _apply_probe(self, job: RenderJob) -> None:
        """Fold a finished probe's output into the job."""
        data, self._probe_buf = self._probe_buf, b""
        info = util.parse_probe(data)
        if info:
            job.apply_probe(info, self._probe_mtime)
        if job.render_start is not None and job.render_end is not None:
            job.frames_total = max(0, job.render_end - job.render_start + 1)
        self.job_status.emit(job)
        self.probe_done.emit(job)

    def _run_render(self, job: RenderJob) -> None:
        self._phase = "render"
        job.status = JobStatus.RENDERING
        job.frame_started_at = None  # first Fra: line of this segment starts the clock
        self.job_status.emit(job)

        args: List[str] = ["-b", job.filepath]
        base = os.path.splitext(os.path.basename(job.filepath))[0]

        if job.preview or job.is_playblast:
            # Solid-mode preview: Workbench IS solid shading, and unlike
            # bpy.ops.render.opengl() it renders headlessly through the normal
            # pipeline from the scene camera.
            args += ["-E", "BLENDER_WORKBENCH"]
        else:
            engine = job.engine or self.settings.default_engine
            if engine:
                args += ["-E", engine]

        if job.is_playblast:
            # The user named this file in the Playblast dialog, so the queue's
            # global output_dir must NOT redirect it. Blender decorates video
            # names with the frame range, hence the trailing "_" — the app
            # renames the result to exactly what was asked for.
            out_dir = os.path.dirname(job.playblast_out)
            pb_base = os.path.splitext(os.path.basename(job.playblast_out))[0]
            args += ["-o", os.path.join(out_dir, pb_base + "_")]
        else:
            out_dir = (self.settings.output_dir
                       or (job.export_dir if job.preview else ""))
            if job.preview:
                # Keep previews beside the real frames but clearly separate, and
                # never let one overwrite a rendered sequence.
                out_dir = out_dir or os.path.dirname(job.filepath)
                args += ["-o", os.path.join(out_dir, base + util.PREVIEW_SUFFIX)]
            elif out_dir:
                args += ["-o", os.path.join(out_dir, base + "_")]

        if not job.is_playblast:
            # ⚠ FORCE THE FILE EXTENSION ON. Without this, Blender obeys the
            # .blend's own `use_file_extension`, and a scene with "File
            # Extensions" unticked in Output properties renders frames named
            # `0298` with NO `.png` at all — reported 2026-08-14 by someone
            # rendering a PNG sequence, seven frames of 6 MB each, every one
            # extensionless.
            #
            # ⚠ It is not only cosmetic. Nothing in this tool can SEE such a
            # file: `util.IMAGE_EXTS` / `VIDEO_EXTS` drive Preview Video, View
            # Anim. and the output scan, and all of them match on extension —
            # so a render that worked looks to the queue like one that produced
            # nothing.
            #
            # Playblasts and previews already force it through their
            # `--python-expr`; this is the plain-render path that never did.
            # `-x` must come before the render trigger, which it does.
            args += ["-x", "1"]

        # extra_args is the user's queue-wide render tweak (device flags and
        # such). A playblast isn't one of their renders — it's a viewport
        # substitute — so it deliberately doesn't inherit them.
        if self.settings.extra_args.strip() and not job.is_playblast:
            args += self.settings.extra_args.split()

        if job.is_playblast:
            # After -E/-o so nothing downstream resets the format.
            args += ["--python-expr", util.playblast_expr(job.playblast_pct)]
        elif job.preview:
            args += ["--python-expr", util.PREVIEW_EXPR]

        # Collections this job leaves out (Marty, 2026-08-04). Applied to the
        # loaded copy only — `-b` never writes the .blend back, so the file on
        # disk is untouched and the same file can be queued again with a
        # different set switched off.
        hide_expr = util.disable_collections_expr(job.disabled_collections)
        if hide_expr:
            args += ["--python-expr", hide_expr]

        # Pick a specific camera (blank => keep the scene's active camera).
        if job.camera:
            expr = (f"import bpy;_o=bpy.data.objects.get({job.camera!r});"
                    f"_o and setattr(bpy.context.scene,'camera',_o)")
            args += ["--python-expr", expr]

        # After a crash we restart at the frame it died on, not the beginning.
        start = (self._resume_from if self._resume_from is not None
                 else job.render_start)
        end = job.render_end
        if start is not None:
            args += ["-s", str(start)]
        if end is not None:
            args += ["-e", str(end)]

        if job.is_playblast:
            self._emit_log(f">>> Background playblast: Workbench engine, "
                           f"{job.playblast_pct}% resolution → "
                           f"{job.playblast_out}\n")
        elif job.preview:
            self._emit_log(f">>> Solid-mode preview: Workbench engine → H.264 "
                           f"MP4 in {out_dir}\n")
        elif job.has_time_limit:
            # Explain up front why a frame can stop well before 100% samples.
            self._emit_log(f">>> Cycles Time Limit: {util.format_seconds(job.time_limit)} "
                           f"per frame — frames end at whichever comes first, the "
                           f"sample count or the clock.\n")

        # Always render the frame range (`-a`). A single frame is just a range
        # where start == end, so there's no separate "still" mode to conflict
        # with the per-job frame range.
        args += ["-a"]

        self._spawn(args)

    # ---- process signals --------------------------------------------
    def _on_output(self) -> None:
        if self._proc is None:
            return
        data = bytes(self._proc.readAllStandardOutput())
        job = self._current

        if self._phase == "probe":
            # Collect and parse once it exits — a marker line can be split
            # across two reads, which a per-chunk search would miss.
            self._probe_buf += data

        if self._phase == "render" and job is not None and job.frames_total:
            changed = False
            for m in _FRA_RE.finditer(data):
                cur = int(m.group(1))
                done = cur - job.first_frame + 1
                done = max(0, min(done, job.frames_total))
                if done != job.frames_done:
                    # New frame boundary: the previous frame is now finished, so
                    # bank its duration and (re)start the current-frame clock.
                    now = time.monotonic()
                    if job.frame_started_at is not None:
                        job.last_frame_seconds = now - job.frame_started_at
                    job.frame_started_at = now
                    job.frames_done = done
                    job.sample_done = job.sample_total = 0  # new frame → reset
                    changed = True
            # within-frame progress (last sample count in this chunk)
            last = None
            for m in _SAMPLE_RE.finditer(data):
                last = m
            if last is not None:
                d = last.group(1) or last.group(3)
                t = last.group(2) or last.group(4)
                job.sample_done, job.sample_total = int(d), int(t)
                changed = True
            if changed:
                self.job_status.emit(job)

        # Console is always on: stream everything to the job's own log.
        try:
            self._emit_log(data.decode("utf-8", errors="replace"))
        except Exception:
            pass

    def _on_error(self, err) -> None:
        if err == QProcess.FailedToStart:
            self._emit_log("\n!!! Failed to start Blender. Check the exe path "
                           "in Settings.\n")

    def _on_finished(self, code: int, _status) -> None:
        proc = self._proc
        self._proc = None
        job = self._current

        if self._phase == "probe" and job is not None and not self._stopping:
            self._apply_probe(job)
            # Probe done — kick off the real render for the same job.
            self._run_render(job)
            return

        if self._paused and self._phase == "render" and job is not None:
            # User paused: bank the frame on the JOB (not on the controller), then
            # go fully idle so another scene can be started meanwhile. The job
            # carries its own resume point, so coming back to it later — this
            # session or after a restart — picks up exactly here.
            job.resume_frame = job.current_frame
            job.status = JobStatus.PAUSED
            self.job_status.emit(job)
            self._emit_log(f">>> {job.name} paused at frame {job.resume_frame}. "
                           f"Select it and press Resume to continue.\n\n")
            if proc is not None:
                proc.deleteLater()
            self._current = None
            self._resume_from = None
            self._queue = []
            self._phase = "idle"
            self.paused_changed.emit(True)
            self.queue_finished.emit()
            return

        if job is not None:
            if self._stopping:
                job.status = JobStatus.CANCELLED
            elif code == 0:
                job.status = JobStatus.DONE
                job.frames_done = job.frames_total
                # Bank the final frame's duration (no later Fra: line closes it).
                if job.frame_started_at is not None:
                    job.last_frame_seconds = time.monotonic() - job.frame_started_at
                    job.frame_started_at = None
            elif self._should_resume(job):
                # Crashed mid-render — restart from the frame it was on.
                self._attempt += 1
                resume = job.current_frame
                self._resume_from = resume
                job.status = JobStatus.RENDERING
                self.job_status.emit(job)
                self._emit_log(f"\n>>> {job.name} crashed (exit {code}). "
                               f"Auto-resume {self._attempt}/{MAX_RESUME_ATTEMPTS} "
                               f"from frame {resume}…\n\n")
                if proc is not None:
                    proc.deleteLater()
                self._run_render(job)
                return
            else:
                job.status = JobStatus.FAILED
            self.job_status.emit(job)
            self._emit_log(f"\n>>> {job.name}: {job.status.value} "
                           f"(exit code {code})\n\n")

        if proc is not None:
            proc.deleteLater()
        self._next_job()

    def _should_resume(self, job: RenderJob) -> bool:
        # Never auto-resume a playblast: restarting mid-range writes a SECOND,
        # partial mp4 under a different frame-range name rather than continuing
        # the first. Failing cleanly is the honest outcome.
        return (self.settings.auto_resume
                and not job.is_playblast
                and not self._stopping
                and job.frames_total > 0
                and job.frames_done < job.frames_total
                and self._attempt < MAX_RESUME_ATTEMPTS)
