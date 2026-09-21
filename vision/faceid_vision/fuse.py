"""Fusion of liveness evidence into a deny/confirm verdict.

  deny    = glare or device_frame or moire or antispoof or ir_screen_dark
            or planarity says FLAT
  confirm = parallax_ok or blink_seen or challenge_passed or ir_skin_ok

  light : unlock = match and not deny
  heavy : unlock = match and not deny and confirm
  off   : unlock = match                      (not recommended)

The fusion never receives similarity scores. That is deliberate: a
strong face match must never be allowed to soften a liveness failure.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Strictness(str, Enum):
    OFF = "off"
    LIGHT = "light"
    HEAVY = "heavy"


@dataclass
class LivenessState:
    deny: list[str] = field(default_factory=list)
    confirm: list[str] = field(default_factory=list)
    attention_ok: bool = False       # eyes open and roughly facing the camera
    notes: dict = field(default_factory=dict)

    def add_deny(self, *cues: str) -> None:
        for c in cues:
            if c and c not in self.deny:
                self.deny.append(c)

    def add_confirm(self, *cues: str) -> None:
        for c in cues:
            if c and c not in self.confirm:
                self.confirm.append(c)

    def score(self) -> float:
        """Rough confidence for display only. Never used for policy."""
        if self.deny:
            return 0.0
        return min(1.0, 0.35 + 0.25 * len(self.confirm))

    def to_json(self) -> dict:
        return {"deny": self.deny, "confirm": self.confirm,
                "attention_ok": self.attention_ok,
                "score": round(self.score(), 3), "notes": self.notes}


def permits(state: LivenessState, strictness: Strictness,
            require_attention: bool = True) -> tuple[bool, str]:
    """Liveness verdict, independent of any recognition result."""
    if strictness is Strictness.OFF:
        return True, ""
    if state.deny:
        return False, "spoof cues: " + ", ".join(state.deny)
    if require_attention and not state.attention_ok:
        return False, "no attention (eyes closed or looking away)"
    if strictness is Strictness.HEAVY and not state.confirm:
        return False, "no liveness confirmation"
    return True, ""
