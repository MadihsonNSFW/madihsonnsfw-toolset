"""Developer console — an in-app log window for troubleshooting.

Why this exists: the app normally runs as a **windowed** exe, which has no
console attached. Anything printed, logged or raised where nobody caught it
goes straight to nowhere, so "it did something weird" has no evidence behind
it. This captures that stream inside the app instead.

Two deliberate design points:

1. **The recorder always runs; only the UI is opt-in.** Capturing into a
   bounded deque costs nothing measurable, and a console you have to switch on
   *before* the bug happens is useless — by the time you want it, the
   interesting lines are already gone. So we always record, and the setting
   only controls whether the window and its status-bar button exist.

2. **Nothing is swallowed.** stdout/stderr are TEE'd (still written to the
   real stream) and sys.excepthook chains to the previous handler, so running
   from source behaves exactly as it always did.
"""

import io
import logging
import os
import sys
import time
import traceback
from collections import deque

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (QCheckBox, QDialog, QHBoxLayout, QLabel,
                               QPlainTextEdit, QPushButton, QVBoxLayout)

import widgets

MAX_LINES = 5000          # ring buffer: plenty of history, bounded memory


class LogBuffer(QObject):
    """Process-wide record of everything worth seeing, newest last."""

    appended = Signal(str, str)     # (level, line)

    def __init__(self):
        super().__init__()
        self.lines = deque(maxlen=MAX_LINES)
        self.error_count = 0
        self._installed = False
        self._prev_excepthook = None

    # ---- recording ---------------------------------------------------
    def add(self, level, text):
        for raw in str(text).splitlines():
            if not raw.strip():
                continue
            stamp = time.strftime("%H:%M:%S")
            line = "%s  %-5s  %s" % (stamp, level, raw)
            self.lines.append((level, line))
            if level in ("ERROR", "CRIT"):
                self.error_count += 1
            self.appended.emit(level, line)

    def text(self):
        return "\n".join(line for _lvl, line in self.lines)

    def clear(self):
        self.lines.clear()
        self.error_count = 0

    # ---- capture plumbing --------------------------------------------
    def install(self):
        """Start recording. Safe to call once; later calls are no-ops."""
        if self._installed:
            return
        self._installed = True

        logging.getLogger().addHandler(_BufferHandler(self))
        logging.getLogger().setLevel(logging.INFO)

        # A windowed exe has no console, so this is the only place print() and
        # library warnings can be seen at all.
        sys.stdout = _Tee(sys.stdout, self, "INFO")
        sys.stderr = _Tee(sys.stderr, self, "ERROR")

        self._prev_excepthook = sys.excepthook
        sys.excepthook = self._on_exception

    def _on_exception(self, exc_type, exc, tb):
        """Unhandled exception: record it, then let the original handler run so
        behaviour outside this window is unchanged."""
        try:
            self.add("CRIT", "Unhandled exception:\n" +
                     "".join(traceback.format_exception(exc_type, exc, tb)))
        except Exception:
            pass
        if self._prev_excepthook is not None:
            self._prev_excepthook(exc_type, exc, tb)


class _BufferHandler(logging.Handler):
    def __init__(self, buffer):
        super().__init__()
        self.buffer = buffer

    def emit(self, record):
        level = {"WARNING": "WARN", "CRITICAL": "CRIT"}.get(
            record.levelname, record.levelname)
        try:
            self.buffer.add(level, self.format(record))
        except Exception:
            pass


class _Tee(io.TextIOBase):
    """Write to the real stream AND the buffer — capturing must not silence."""

    def __init__(self, stream, buffer, level):
        self.stream = stream
        self.buffer = buffer
        self.level = level

    def write(self, text):
        try:
            if self.stream is not None:
                self.stream.write(text)
        except Exception:
            pass          # a frozen exe can have no real stdout at all
        try:
            self.buffer.add(self.level, text)
        except Exception:
            pass
        return len(text)

    def flush(self):
        try:
            if self.stream is not None:
                self.stream.flush()
        except Exception:
            pass

    def isatty(self):
        return False


# One recorder for the whole process.
BUFFER = LogBuffer()


class DevConsoleDialog(widgets.GuardedDialog):
    """The window itself: non-modal, so it can sit open while you work."""

    LEVEL_COLORS = {"ERROR": "#e06c60", "CRIT": "#e06c60", "WARN": "#d8c74f"}

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Developer Console")
        self.resize(860, 460)
        # Non-modal AND independently closable/raisable.
        self.setWindowFlag(Qt.Window, True)
        self.setModal(False)

        lay = QVBoxLayout(self)
        head = QLabel("Everything the app logged this session — errors, "
                      "warnings, tracebacks and anything printed. Recording "
                      "starts at launch, so this includes what happened "
                      "before you opened the window.")
        head.setObjectName("dim")
        head.setWordWrap(True)
        lay.addWidget(head)

        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        self.view.setMaximumBlockCount(MAX_LINES + 100)
        self.view.setLineWrapMode(QPlainTextEdit.NoWrap)
        font = self.view.font()
        font.setFamily("Consolas")
        self.view.setFont(font)
        lay.addWidget(self.view, 1)

        row = QHBoxLayout()
        self.chk_follow = QCheckBox("Follow new output")
        self.chk_follow.setChecked(True)
        row.addWidget(self.chk_follow)
        row.addStretch(1)
        for label, slot in (("Copy all", self._copy), ("Save…", self._save),
                            ("Clear", self._clear)):
            btn = QPushButton(label)
            btn.setObjectName("flat")
            btn.clicked.connect(slot)
            row.addWidget(btn)
        btn_close = QPushButton("Close")
        btn_close.clicked.connect(self.hide)
        row.addWidget(btn_close)
        lay.addLayout(row)

        self.view.setPlainText(BUFFER.text())
        self._scroll_to_end()
        BUFFER.appended.connect(self._on_line)

    def _on_line(self, level, line):
        color = self.LEVEL_COLORS.get(level)
        if color:
            self.view.appendHtml(
                '<span style="color:%s;white-space:pre">%s</span>'
                % (color, _escape(line)))
        else:
            self.view.appendPlainText(line)
        if self.chk_follow.isChecked():
            self._scroll_to_end()

    def _scroll_to_end(self):
        bar = self.view.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _copy(self):
        from PySide6.QtWidgets import QApplication
        QApplication.clipboard().setText(BUFFER.text())

    def _save(self):
        from PySide6.QtWidgets import QFileDialog
        default = os.path.join(
            os.path.expanduser("~"),
            time.strftime("madi_toolset_log_%Y%m%d_%H%M%S.txt"))
        path, _ = QFileDialog.getSaveFileName(
            self, "Save log", default, "Text files (*.txt)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(BUFFER.text())
        except OSError as exc:
            BUFFER.add("ERROR", "Could not save log: %s" % exc)

    def _clear(self):
        BUFFER.clear()
        self.view.clear()


def _escape(text):
    return (text.replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;"))
