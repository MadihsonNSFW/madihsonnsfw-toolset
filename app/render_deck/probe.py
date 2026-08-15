"""Background reader for the descriptive columns in the job table.

Adding a .blend should immediately show what's inside it — the range it will
render, the Blender version it was saved with, packed data, samples, noise
threshold, time limit. All of that lives inside the file, so the only way to get
it is to open the file, which means launching Blender.

This runs those reads ONE AT A TIME in the background, and stands aside entirely
while a render is running so it never competes with it for CPU. Results are
cached on the job (and persisted, keyed to the .blend's mtime), so a file is
normally read once and never again until it's re-saved.
"""
from __future__ import annotations

from typing import List, Optional

from PySide6.QtCore import QObject, QProcess, Signal

from . import util
from .job import RenderJob


class InfoProbe(QObject):
    """Sequential queue of "open this .blend and tell me about it" jobs."""

    updated = Signal(object)      # a RenderJob whose info fields were filled
    finished = Signal()           # the backlog is empty

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self._queue: List[RenderJob] = []
        self._proc: Optional[QProcess] = None
        self._job: Optional[RenderJob] = None
        self._mtime = None
        self._buf = b""
        self._held = False        # True while a render owns the machine

    # ---- public API -------------------------------------------------
    @property
    def busy(self) -> bool:
        return self._proc is not None

    def enqueue(self, jobs) -> None:
        """Queue jobs whose cached info is missing or stale. Already-queued and
        already-fresh jobs are skipped, so this is cheap to call often."""
        for job in jobs:
            if job.probe_fresh() or job in self._queue or job is self._job:
                continue
            self._queue.append(job)
        self._pump()

    def drop(self, job: RenderJob) -> None:
        """Forget a job that was removed from the list. A read already in
        flight is left to finish — its result is simply discarded."""
        try:
            self._queue.remove(job)
        except ValueError:
            pass

    def hold(self, held: bool) -> None:
        """Pause/resume the backlog. Called with True while a render runs so
        probing never steals CPU from it."""
        self._held = held
        if not held:
            self._pump()

    def clear(self) -> None:
        self._queue = []

    # ---- internals --------------------------------------------------
    def _pump(self) -> None:
        if self._held or self._proc is not None or not self._queue:
            return
        blender = self.settings.blender_path
        if not blender:
            return
        self._job = self._queue.pop(0)
        self._mtime = self._job.file_mtime()
        if self._mtime is None:          # file went missing — skip it
            self._job = None
            self._pump()
            return
        self._buf = b""
        proc = QProcess(self)
        proc.setProcessChannelMode(QProcess.MergedChannels)
        proc.readyReadStandardOutput.connect(self._on_output)
        proc.finished.connect(self._on_finished)
        proc.errorOccurred.connect(self._on_error)
        self._proc = proc
        proc.start(blender, util.probe_args(self._job.filepath))

    def _on_output(self) -> None:
        if self._proc is not None:
            self._buf += bytes(self._proc.readAllStandardOutput())

    def _on_error(self, _err) -> None:
        # Bad Blender path or a file that won't open: give up on this one
        # quietly rather than spamming the UI — the columns just stay "—".
        self._finish_current()

    def _on_finished(self, _code, _status) -> None:
        job, info = self._job, util.parse_probe(self._buf)
        if job is not None and info:
            job.apply_probe(info, self._mtime)
            self.updated.emit(job)
        self._finish_current()

    def _finish_current(self) -> None:
        if self._proc is not None:
            self._proc.deleteLater()
        self._proc = None
        self._job = None
        self._buf = b""
        if self._queue:
            self._pump()
        else:
            self.finished.emit()
