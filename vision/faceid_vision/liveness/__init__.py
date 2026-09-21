"""Liveness cues.

Two kinds, and the distinction is the whole design:

  deny cues    -- positive evidence of a spoof. Any one of them vetoes
                  the unlock, regardless of how good the face match was.
  confirm cues -- positive evidence of a real 3D person. Their absence
                  is not a failure; a real user may sit perfectly still.

Nothing in here is allowed to look at similarity scores. Recognition
and liveness must fail independently, or a strong match starts
excusing a weak liveness result.
"""
from .blink import BlinkDetector, eye_aspect_ratio
from .geometry import PlanarityTracker, planarity_residual
from .screen import ScreenCues, screen_report
from .passive import PassiveAntiSpoof
from .challenge import Challenge, ChallengeRunner

__all__ = [
    "BlinkDetector", "eye_aspect_ratio",
    "PlanarityTracker", "planarity_residual",
    "ScreenCues", "screen_report",
    "PassiveAntiSpoof",
    "Challenge", "ChallengeRunner",
]
