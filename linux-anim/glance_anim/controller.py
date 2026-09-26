"""GTK-free phase state machine. Port of NotchOverlayController.swift.

Keeps timing/state testable without a display. The GTK overlay subscribes
via callbacks; the face-unlock app only needs:

    ctl = AnimController(style="original")
    ctl.present() / ctl.begin_scanning() / ctl.finish(ok) / ctl.collapse()

Durations mirror the Swift source:
  success hold ~1.7s (1.22s asset + beat), failure hold 5s,
  collapse 0.7s, scan timeout = faceDetectionSeconds (default 5s).
"""
import threading
from enum import Enum


class Phase(str, Enum):
    CLOSED = "closed"
    SCANNING = "scanning"
    SUCCESS = "success"
    FAILURE = "failure"
    COLLAPSING = "collapsing"


class UnlockStyle(str, Enum):
    ORIGINAL = "original"
    MINIMAL = "minimal"
    NONE = "none"


class ScanMedia(str, Enum):
    IDLE = "idle"
    SUCCESS = "success"
    FAILURE = "failure"


SUCCESS_HOLD = 1.7
FAILURE_HOLD = 5.0
COLLAPSE_DURATION = 0.7


class AnimController:
    def __init__(self, style: UnlockStyle | str = UnlockStyle.ORIGINAL,
                 scan_timeout: float = 5.0):
        self.style = UnlockStyle(style)
        self.scan_timeout = scan_timeout
        self.phase: Phase = Phase.CLOSED
        self.media: ScanMedia = ScanMedia.IDLE
        self._timer: threading.Timer | None = None
        self._listeners: list = []

    # -- observer --
    def on_change(self, cb):
        self._listeners.append(cb)
        return cb

    def _emit(self):
        for cb in list(self._listeners):
            try:
                cb(self.phase, self.media, self.style)
            except Exception:
                pass

    def _after(self, delay: float, fn):
        self._cancel()
        t = threading.Timer(delay, fn)
        t.daemon = True
        self._timer = t
        t.start()

    def _cancel(self):
        if self._timer is not None:
            try:
                self._timer.cancel()
            except Exception:
                pass
            self._timer = None

    # -- API (mirrors NotchOverlayController) --
    def present(self, style: UnlockStyle | str | None = None):
        if style is not None:
            self.style = UnlockStyle(style)
        self._cancel()
        self.media = ScanMedia.IDLE
        self.phase = Phase.SCANNING
        self._emit()
        self._after(self.scan_timeout, self._on_scan_timeout)

    begin_scanning = present

    def _on_scan_timeout(self):
        if self.phase == Phase.SCANNING:
            self.collapse()

    def finish(self, success: bool):
        self._cancel()
        animate = self.style != UnlockStyle.NONE
        if animate:
            self.media = ScanMedia.SUCCESS if success else ScanMedia.FAILURE
        else:
            self.media = ScanMedia.IDLE
        self.phase = Phase.SUCCESS if success else Phase.FAILURE
        self._emit()
        hold = (SUCCESS_HOLD if success else FAILURE_HOLD) if animate else 0.4
        self._after(hold, self.collapse)

    def collapse(self):
        if self.phase in (Phase.CLOSED, Phase.COLLAPSING):
            return
        self.phase = Phase.COLLAPSING
        self._emit()
        self._after(COLLAPSE_DURATION, self._to_closed)

    def _to_closed(self):
        self.phase = Phase.CLOSED
        self.media = ScanMedia.IDLE
        self._emit()

    def dismiss_immediately(self):
        if self.phase in (Phase.SUCCESS, Phase.COLLAPSING):
            return
        self._cancel()
        self.phase = Phase.CLOSED
        self.media = ScanMedia.IDLE
        self._emit()

    @property
    def target_expanded(self) -> bool:
        return self.phase in (Phase.SCANNING, Phase.SUCCESS, Phase.FAILURE)
