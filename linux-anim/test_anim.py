#!/usr/bin/env python3
"""Standalone animation test harness — run first, integrate later.

Usage:
    ./run.sh                  # overlay + control panel
    ./run.sh --self-test      # headless logic check (no display needed)
    ./run.sh --style minimal  # start in minimal lock style

Buttons mirror NotchOverlayController: present/begin scanning,
finish(success/failure), collapse, dismiss, plus style switch
original/minimal/none and scan-timeout spin. Watch the top-center
pill: slide-in -> spring expand -> breathing pulse -> video resolve.
"""
import argparse
import sys
import time


def self_test() -> int:
    from glance_anim.spring import Spring1D
    from glance_anim.controller import AnimController, Phase
    from glance_anim.geometry import LinuxGeometry as G

    # 1. spring settles open then close
    s = Spring1D(0.0, G.OPEN_RESPONSE, G.OPEN_DAMPING)
    s.set_target(1.0, G.OPEN_RESPONSE, G.OPEN_DAMPING)
    for _ in range(600):
        s.step(1 / 120)
    assert abs(s.x - 1.0) < 0.02, f"open spring did not settle: {s.x}"
    s.set_target(0.0, G.CLOSE_RESPONSE, G.CLOSE_DAMPING)
    for _ in range(600):
        s.step(1 / 120)
    assert abs(s.x) < 0.02, f"close spring did not settle: {s.x}"

    # 2. controller phase flow (short holds)
    import glance_anim.controller as C
    C.SUCCESS_HOLD, C.FAILURE_HOLD, C.COLLAPSE_DURATION = 0.15, 0.15, 0.15
    ctl = AnimController(style="original", scan_timeout=10)
    seen = []
    ctl.on_change(lambda p, m, st: seen.append(p))
    ctl.present()
    assert ctl.phase == Phase.SCANNING, ctl.phase
    ctl.finish(True)
    assert ctl.phase == Phase.SUCCESS, ctl.phase
    time.sleep(0.4)
    assert ctl.phase == Phase.COLLAPSING or ctl.phase == Phase.CLOSED, ctl.phase
    time.sleep(0.3)
    assert ctl.phase == Phase.CLOSED, ctl.phase
    ctl.present()
    ctl.finish(False)
    assert ctl.media.value == "failure", ctl.media

    # 3. assets resolvable (dev fallback to ../glance/Resources)
    from glance_anim import video as V
    assert V.media_path("success") is not None, "unlockanimation.mp4 not found"
    assert V.media_path("failure") is not None, "unsuccessfulunlockanimation.mp4 not found"
    assert V.media_path("idle") is not None, "unlockstatic.png not found"

    print("self-test OK: spring + controller + assets")
    return 0


def gui_test(style: str):
    import gi
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk, GLib
    from glance_anim.overlay import GlanceAnimOverlay

    overlay = GlanceAnimOverlay(style=style)

    def build_panel(app):
        ctl_win = Gtk.ApplicationWindow(application=app, title="Glance anim test")
        ctl_win.set_default_size(300, 340)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_margin_top(12)
        box.set_margin_bottom(12)
        box.set_margin_start(12)
        box.set_margin_end(12)
        ctl_win.set_child(box)

        status = Gtk.Label(label="phase: scanning")
        box.append(status)
        overlay.controller.on_change(
            lambda p, m, s: GLib.idle_add(
                status.set_text, f"phase: {p.value}  media: {m.value}  style: {s.value}")
        )

        def btn(label, fn):
            b = Gtk.Button(label=label)
            b.connect("clicked", lambda *_: fn())
            box.append(b)
            return b

        btn("Present / rescan", lambda: overlay.present())
        btn("Finish: success", lambda: overlay.finish(True))
        btn("Finish: failure", lambda: overlay.finish(False))
        btn("Collapse", lambda: overlay.collapse())
        btn("Dismiss now", lambda: overlay.dismiss_immediately())

        style_row = Gtk.Box(spacing=6, orientation=Gtk.Orientation.HORIZONTAL)
        box.append(Gtk.Label(label="Style (UnlockAnimationStyle)"))
        for st in ("original", "minimal", "none"):
            b = Gtk.Button(label=st)
            b.connect("clicked", lambda _, s=st: (overlay.set_style(s), overlay.present()))
            style_row.append(b)
        box.append(style_row)
        box.append(Gtk.Label(label="Tip: 'none' keeps expansion,\nskips video (like macOS)."))
        ctl_win.present()

    overlay.add_extra_builder(build_panel)
    overlay.run()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--style", default="original", choices=["original", "minimal", "none"])
    args = ap.parse_args()
    if args.self_test:
        sys.exit(self_test())
    gui_test(args.style)


if __name__ == "__main__":
    main()
