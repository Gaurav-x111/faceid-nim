"""Glance animation port for Linux (Ubuntu 24.04+, GNOME, .deb distros).

Public API for the face-unlock app:

    from glance_anim.overlay import GlanceAnimOverlay
    overlay = GlanceAnimOverlay(style="original")  # or "minimal" / "none"
    overlay.present()            # show + begin scanning
    overlay.finish(success=True) # play success/failure, auto-collapse
    overlay.collapse()           # shrink away

See ../README_LINUX_ANIM.md (linux-anim/README.md) for integration.
"""

from .geometry import LinuxGeometry
from .controller import AnimController, Phase, UnlockStyle

__all__ = ["LinuxGeometry", "AnimController", "Phase", "UnlockStyle"]
