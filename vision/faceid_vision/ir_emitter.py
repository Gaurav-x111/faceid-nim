"""IR emitter detection.

Passive IR cues (analyse_ir_face) work without an emitter, but the
strongest test -- emitter_difference() -- needs the emitter actually
on, and switching it on takes a vendor-specific UVC control that this
package cannot issue generically.

This module used to point at linux-enable-ir-emitter, but that tool is
not packaged for Ubuntu (apt has no candidate), so the old "sudo apt
install ..." hint could never work and is gone. Detection is kept --
the tool can exist from a source build -- but nothing here names an
uninstallable package.

This module only *detects*; it never touches hardware. The honest
machine-readable report lives in packaging/scripts/ir-emitter
(probe-only; there is deliberately no --enable).
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass


@dataclass
class EmitterStatus:
    tool_present: bool = False
    hint: str = ""


def tool_present() -> bool:
    """True when a linux-enable-ir-emitter binary is on PATH.

    The tool is not packaged for Ubuntu, so this is only true for a
    source build or a foreign package -- and even then, enabling the
    LEDs stays vendor-specific and out of scope here.
    """
    return shutil.which("linux-enable-ir-emitter") is not None


def status() -> EmitterStatus:
    present = tool_present()
    if present:
        hint = ("emitter tool found on PATH, but driving the LEDs is still "
                "vendor-specific; see 'faceid-nim ir-emitter --probe'")
    else:
        hint = ("no emitter tool on PATH (not in Ubuntu apt); an IR sensor "
                "runs dark, passive IR only. "
                "See 'faceid-nim ir-emitter --probe'")
    return EmitterStatus(tool_present=present, hint=hint)
