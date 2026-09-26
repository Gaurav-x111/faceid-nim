# Glance animation — Linux port (test folder)

Isolated port of the macOS notch animation (`glance/NotchOverlay/*`,
`glance/Resources/*.mp4`) for **Ubuntu 24.04+ / GNOME / .deb distros**.
Test here, then copy `glance_anim/` into your face-unlock app.

## What was converted

| macOS | Linux here |
|---|---|
| `ScanAnimationView` (AVPlayer play-once, hold last frame) | `glance_anim/overlay.py` + `Gtk.Video` + `Gtk.MediaFile` (no loop, muted) |
| `unlockstatic.png` idle still | `Gtk.Picture` |
| `NotchGeometry` springs/choreography/pulse | `glance_anim/geometry.py` (same numbers, pill-only — no notch on Linux) |
| SwiftUI `.spring(response,damping)` | `glance_anim/spring.py` harmonic integrator |
| `NotchOverlayController` phases/holds | `glance_anim/controller.py` (GTK-free, unit-testable) |
| Notch window (AppKit overlay) | GTK4 `GtkLayerShell` top-center `OVERLAY` panel, Cairo rounded pill; falls back to normal window on X11/without layer-shell |

Assets in `glance_anim/assets/` are copies of `../glance/Resources/`
(`unlockanimation.mp4` 1.22s/60fps/432px, `unsuccessfulunlockanimation.mp4`,
`unlockstatic.png`, + `idleanimation/logoanimation` for onboarding later).

## Test

```bash
./install-deps.sh        # apt deps (gtk4, layershell, gstreamer, ffmpeg)
./run.sh --self-test     # headless: spring + controller + assets, no display
./run.sh                 # overlay pill + control panel (needs GNOME/Wayland|X11)
./run.sh --style minimal # capsule lock style
./package-deb.sh 0.1.0   # → dist/glance-anim-test_0.1.0_all.deb
sudo dpkg -i dist/*.deb && glance-anim-test
```

Control panel buttons = `NotchOverlayController` API: Present, Success,
Failure, Collapse, Dismiss, style `original/minimal/none` (`none` expands
but skips video, like macOS).

## Integrate into your face-unlock app

```python
from glance_anim.overlay import GlanceAnimOverlay
overlay = GlanceAnimOverlay(style="original")
overlay.run()             # standalone: builds windows on activate, then scans
# -- or, inside your app that already owns a Gtk.Application --
# overlay.attach(app)     # call before app.run(); window built on activate
overlay.present()         # scanning: slide -> expand -> breathing pulse
overlay.finish(success=True)   # plays unlockanimation.mp4, auto-collapses (1.7s)
overlay.finish(success=False)  # unsuccessfulunlockanimation.mp4, holds 5s
overlay.collapse()
```

Only dependency is system GTK (`install-deps.sh` list) — no pip packages,
works on stock Ubuntu 24.04 GNOME. `controller.py`/`geometry.py`/`spring.py`
have zero GTK imports, so headless unlock daemons can drive them and render
elsewhere (e.g. GDM/PAM prompt) if needed.

## GNOME notes

- Wayland: layer-shell gives a true top-center overlay above apps (not above
  lock screen — use your unlock daemon's privileged layer for that).
- Click-through: exclusive-zone 0 + `KeyboardMode.NONE`; make interactive
  only on failure-retry like macOS (`updateInteractivity`).
- If `GtkLayerShell` is missing (X11/odd WM), overlay falls back to a
  borderless window — same animation, just window-managed.
