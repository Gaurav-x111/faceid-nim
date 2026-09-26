"""Port of glance/NotchOverlay/NotchGeometry.swift — pill style only.

On Linux/GNOME there is no physical notch, so we always use the
Dynamic-Island 'pill' fallback from the macOS codebase. Values below are
copied 1:1 so the feel matches macOS; tweak here.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Size:
    w: float
    h: float


class LinuxGeometry:
    # -- pill silhouette (NotchGeometry.pillClosedSize / pillOpenSize) --
    PILL_CLOSED = Size(80, 24)
    PILL_OPEN = Size(180, 180)

    PILL_TOP_GAP = 3.0
    PILL_OPEN_CORNER_RADIUS = 48.0
    PILL_OFFSCREEN_SLACK = 20.0

    PILL_CONTENT_PADDING = 32.0  # uniform; macOS has per-edge 32

    # -- springs (NotchGeometry.openSpring* / closeSpring*) --
    OPEN_RESPONSE = 0.45
    OPEN_DAMPING = 0.7
    CLOSE_RESPONSE = 0.45
    CLOSE_DAMPING = 1.0

    # -- pill choreography (NotchGeometry.pillSlide* / pillEnter* / pillExit*) --
    SLIDE_DURATION = 0.25
    ENTER_EXPANSION_DELAY = 0.16
    EXIT_SLIDE_DELAY = 0.18

    # -- minimal style (NotchGeometry.minimalPill*) --
    MINIMAL_OPEN_W = 150.0
    MINIMAL_OPEN_H = 40.0
    MINIMAL_LOCK_ANIMATION_DURATION = 0.4

    # -- breathing pulse (NotchGeometry.scanPulse*) --
    PULSE_SCALE = 0.97
    PULSE_OPACITY = 0.65
    PULSE_HALF = 0.4
    PULSE_HOLD = 0.05
    PULSE_SETTLE = 0.2
    PULSE_START_DELAY = 0.6

    HOVER_BUMP = 6.0

    @classmethod
    def open_size(cls, style: str) -> Size:
        if style == "minimal":
            return Size(cls.MINIMAL_OPEN_W, cls.MINIMAL_OPEN_H)
        return Size(cls.PILL_OPEN.w, cls.PILL_OPEN.h)

    @classmethod
    def closed_size(cls) -> Size:
        return Size(cls.PILL_CLOSED.w, cls.PILL_CLOSED.h)
