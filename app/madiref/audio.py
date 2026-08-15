"""Reference audio — app-side only, and off unless asked for.

Nothing is extracted at ingest. The audio comes from the ORIGINAL source file
through a second QMediaPlayer with its video ignored, which means no ffmpeg
dependency for sound, no WAV sitting beside every proxy, and no second format
to get wrong. The proxy stays picture-only.

⚠ Sound follows PLAYBACK, not scrubbing. Dragging the timeline would otherwise
fire a seek per frame, which stutters horribly and tells you nothing useful;
scrubbing mutes instead. That is the stated scope — scrub-synced ("scrub audio"
in the DAW sense) is a different and much larger problem.

Blender is never asked to make a sound: it does not have the file, and two
audio engines fighting over one clip is worse than one.
"""

import time

from PySide6.QtCore import QObject, QUrl, Signal
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer

# How far the audio may drift from the reference before we re-seek. Too small
# and normal jitter causes constant re-seeking (which is audible); too large and
# lip-sync visibly slips. 120 ms is about two frames at 24 fps.
DRIFT_TOLERANCE_MS = 120
# Playback is inferred from the timeline moving. One still frame is not
# playback -- this many consecutive advances is.
_ADVANCE_TO_PLAY = 2
# ⚠ How big a FORWARD step still counts as playback. This started at 0.5 s and
# that was the bug: FRAME_DROP is on by default, so a heavy scene legitimately
# jumps several frames at once, every jump over the limit reset the counter,
# and the audio could never reach two consecutive advances -- the tickbox
# looked dead. Only a BACKWARD step or a real leap is a scrub now.
_SCRUB_JUMP_S = 2.0
# ⚠ How long without a new frame counts as STOPPED. `sync()` is only called
# when a frame arrives, so a paused timeline produces no calls at all — and
# without this the audio simply kept playing after you hit pause. Absence of
# events is the only signal that playback ended, so something has to watch for
# it; `tab._on_tick` polls this every 100 ms.
_IDLE_STOP_S = 0.25


class ReferenceAudio(QObject):
    """Keeps the source file's audio alongside a reference time in seconds."""

    unavailable = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._out = QAudioOutput(self)
        self._player = QMediaPlayer(self)
        self._player.setAudioOutput(self._out)
        self._player.setVideoOutput(None)
        self._player.errorOccurred.connect(self._on_error)
        self._enabled = False
        self._source = None
        self._advances = 0
        self._last_seconds = None
        self._volume = 0.8
        self._out.setVolume(self._volume)
        self._last_sync_at = 0.0

    # ------------------------------------------------------------------

    def set_source(self, path):
        self._source = path
        self._player.setSource(QUrl.fromLocalFile(path) if path else QUrl())
        self._advances = 0
        self._last_seconds = None

    def set_enabled(self, on):
        self._enabled = bool(on)
        if not self._enabled:
            self._player.pause()
            self._advances = 0

    @property
    def enabled(self):
        return self._enabled

    def set_volume(self, v):
        self._volume = max(0.0, min(1.0, float(v)))
        self._out.setVolume(self._volume)

    def stop(self):
        self._player.stop()
        self._advances = 0
        self._last_seconds = None

    # ------------------------------------------------------------------

    def sync(self, seconds):
        """Follow the reference clock.

        Called every time the served frame changes. Whether that counts as
        playback is inferred here rather than asked of Blender, because the
        add-on has no reliable "am I playing" to give: `screen.is_animation_
        playing` is per-window and says nothing about a user dragging the
        playhead, which should be silent.
        """
        if not self._enabled or not self._source:
            return
        self._last_sync_at = time.monotonic()
        last = self._last_seconds
        self._last_seconds = seconds
        if last is None:
            return
        step = seconds - last
        # Moving forward at a plausible rate = playing. Going backwards, or
        # leaping, is a scrub and stays silent.
        if 0.0 < step < _SCRUB_JUMP_S:
            self._advances += 1
        else:
            self._advances = 0
            self._player.pause()
            return
        if self._advances < _ADVANCE_TO_PLAY:
            return
        want_ms = int(seconds * 1000)
        if self._player.playbackState() != QMediaPlayer.PlayingState:
            self._player.setPosition(want_ms)
            self._player.play()
            return
        if abs(self._player.position() - want_ms) > DRIFT_TOLERANCE_MS:
            self._player.setPosition(want_ms)

    def check_idle(self):
        """Stop when the frames stop arriving.

        ⚠ Nothing else can notice a pause. `sync()` is driven by frames, and a
        paused timeline delivers none — so playback ending looks exactly like
        silence from this class's point of view, and the audio ran on. Polled
        from the tab's 100 ms tick.
        """
        if not self._enabled:
            return
        if self._player.playbackState() != QMediaPlayer.PlayingState:
            return
        if time.monotonic() - self._last_sync_at > _IDLE_STOP_S:
            self._player.pause()
            self._advances = 0

    def _on_error(self, *_a):
        # A clip with no audio track is the common case, not a fault.
        msg = self._player.errorString() or "no audio track"
        self._enabled = False
        self.unavailable.emit(msg)
