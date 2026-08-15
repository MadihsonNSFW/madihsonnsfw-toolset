"""Per-job console logs backed by temp files.

Each job's streamed Blender output goes to its own `.log` file (full history on
disk, not held in RAM). When you click a job we load only the TAIL of its file
into the console widget, so switching is fast no matter how huge the render log
got — this is what fixes the multi-second freeze that came from calling
setPlainText() on a multi-megabyte in-memory string.

Files live in one temp dir for the app session and are removed on close.
"""
from __future__ import annotations

import itertools
import os
import shutil
import tempfile

# How much of the end of a log to show on switch (~ the widget's display cap).
TAIL_BYTES = 500_000


class JobLogStore:
    def __init__(self):
        self._dir = tempfile.mkdtemp(prefix="renderdeck_logs_")
        self._counter = itertools.count()
        self._paths: dict[int, str] = {}   # id(job) -> file path
        self._files: dict[int, object] = {}  # id(job) -> open append handle

    def _path_for(self, job) -> str:
        k = id(job)
        p = self._paths.get(k)
        if p is None:
            p = os.path.join(self._dir, f"job_{next(self._counter)}.log")
            self._paths[k] = p
        return p

    def append(self, job, text: str) -> None:
        k = id(job)
        f = self._files.get(k)
        if f is None:
            try:
                f = open(self._path_for(job), "a", encoding="utf-8", buffering=1)
            except OSError:
                return
            self._files[k] = f
        try:
            f.write(text)
        except OSError:
            pass

    def reset(self, job) -> None:
        """Clear a job's log for a fresh run."""
        self._close(job)
        p = self._paths.get(id(job))
        if p and os.path.exists(p):
            try:
                os.remove(p)
            except OSError:
                pass

    def tail(self, job, max_bytes: int = TAIL_BYTES) -> str:
        p = self._paths.get(id(job))
        if not p:
            return ""
        f = self._files.get(id(job))
        if f:
            try:
                f.flush()
            except OSError:
                pass
        if not os.path.exists(p):
            return ""
        try:
            size = os.path.getsize(p)
            with open(p, "r", encoding="utf-8", errors="replace") as rf:
                if size > max_bytes:
                    rf.seek(size - max_bytes)
                    rf.readline()  # drop the partial first line
                return rf.read()
        except OSError:
            return ""

    def remove(self, job) -> None:
        """Drop a job entirely (on delete/clear)."""
        self.reset(job)
        self._paths.pop(id(job), None)

    def _close(self, job) -> None:
        f = self._files.pop(id(job), None)
        if f:
            try:
                f.close()
            except OSError:
                pass

    def cleanup(self) -> None:
        for f in list(self._files.values()):
            try:
                f.close()
            except OSError:
                pass
        self._files.clear()
        shutil.rmtree(self._dir, ignore_errors=True)
