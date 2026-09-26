"""IR emitter detection.

Passive IR cues (analyse_ir_face) work without an emitter, but the
strongest test -- emitter_difference() -- needs the emitter actually
on. Most vendors need a UVC quirk via linux-enable-ir-emitter.

This module only *detects*; it never touches hardware. Enabling stays
in packaging/scripts/ir-emitter (root, explicit opt-in).
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass


@dataclass
class EmitterStatus:
    tool_present: bool = False
    hint: str = ""


def tool_present() -> bool:
    """True when linux-enable-ir-emitter is on PATH."""
    return shutil.which("linux-enable-ir-emitter") is not None


def status() -> EmitterStatus:
    present = tool_present()
    if present:
        hint = "emitter tool present; run: sudo faceid-nim ir-emitter --enable"
    else:
        hint = ("linux-enable-ir-emitter not installed; "
                "passive IR only (sudo apt install linux-enable-ir-emitter)")
    return EmitterStatus(tool_present=present, hint=hint)
