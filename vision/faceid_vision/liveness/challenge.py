"""Active challenge-response for strict mode.

A pre-recorded video cannot know which action will be asked for, or
when. Both the choice and the order must be random per attempt, or an
attacker just records all of them and plays the right clip.
"""
from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from enum import Enum


class Challenge(str, Enum):
    TURN_LEFT = "turn_left"
    TURN_RIGHT = "turn_right"
    LOOK_UP = "look_up"
    BLINK_TWICE = "blink_twice"

    @property
    def prompt(self) -> str:
        return {
            Challenge.TURN_LEFT: "Turn your head left",
            Challenge.TURN_RIGHT: "Turn your head right",
            Challenge.LOOK_UP: "Look up",
            Challenge.BLINK_TWICE: "Blink twice",
        }[self]


@dataclass
class ChallengeRunner:
    """Issues one random challenge and judges it within a window.

    `yaw_signed` is positive when the head turns toward image-right.
    Using the same cheap proxy as the quality gate keeps the whole
    system honest about what it can actually measure.
    """
    window_s: float = 2.5
    yaw_delta: float = 0.16
    pitch_delta: float = 0.14
    challenge: Challenge = field(default_factory=lambda: secrets.choice(list(Challenge)))
    _t0: float | None = None
    _base_yaw: float | None = None
    _base_pitch: float | None = None
    _base_blinks: int = 0
    passed: bool = False
    expired: bool = False

    def start(self, yaw_signed: float, pitch: float, blinks: int) -> Challenge:
        self._t0 = time.monotonic()
        self._base_yaw, self._base_pitch, self._base_blinks = yaw_signed, pitch, blinks
        return self.challenge

    def update(self, yaw_signed: float, pitch: float, blinks: int) -> bool:
        if self.passed or self._t0 is None:
            return self.passed
        if time.monotonic() - self._t0 > self.window_s:
            self.expired = True
            return False
        dy = yaw_signed - (self._base_yaw or 0.0)
        dp = pitch - (self._base_pitch or 0.0)
        if self.challenge is Challenge.TURN_LEFT:
            self.passed = dy <= -self.yaw_delta
        elif self.challenge is Challenge.TURN_RIGHT:
            self.passed = dy >= self.yaw_delta
        elif self.challenge is Challenge.LOOK_UP:
            self.passed = dp <= -self.pitch_delta
        elif self.challenge is Challenge.BLINK_TWICE:
            self.passed = (blinks - self._base_blinks) >= 2
        return self.passed
