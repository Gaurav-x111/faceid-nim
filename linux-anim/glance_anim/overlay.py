"""GTK4 + LayerShell 'dynamic island' overlay. Pill style only (no notch on Linux).

Window: top-center anchored layer-shell panel, transparent, click-through
except while failure/hover-retry. Content: Cairo rounded-rect pill whose
width/height/corner-radius animate with the same spring constants as macOS,
plus a Gtk.Video (success/failure .mp4, play-once hold-last-frame) or
Gtk.Picture (idle .png), plus breathing pulse while scanning.

This module imports GTK lazily so `controller.py` stays headless-testable.
If Gtk/LayerShell is missing, GlanceAnimOverlay raises RuntimeError with the
apt install hint — see install-deps.sh.

Public API for face-unlock app:
    overlay = GlanceAnimOverlay(style="original")
    overlay.present(); overlay.finish(True/False); overlay.collapse()
    overlay.set_style("minimal"); overlay.run() / overlay.show()
"""
import math
import time

from .controller import AnimController, Phase, ScanMedia, UnlockStyle
from .geometry import LinuxGeometry as G
from .spring import Spring1D, TimedLerp
from . import video as video_res


class _AnimState:
    """Visual mirrors of controller state (cf. NotchOverlayView visualIs*)."""

    def __init__(self):
        self.expand = Spring1D(0.0, G.OPEN_RESPONSE, G.OPEN_DAMPING)
        self.slide = TimedLerp()
        self.slide_v = 0.0  # 0=offscreen, 1=docked
        self.slide_done = True
        self.pending_expand: tuple[float, float, float] | None = None  # (at, to, resp, zeta)
        self.pending_slide: tuple[float, float] | None = None  # (at, to)
        self.pulse_t0: float | None = None
        self.pulse_on = False


def _pulse_value(now: float, t0: float) -> tuple[float, float]:
    """Breathing pulse: scale/opacity ping-pong. Port of startScanPulse()."""
    t = now - t0 - G.PULSE_START_DELAY
    if t < 0:
        return 1.0, 1.0
    cycle = 2 * (G.PULSE_HALF + G.PULSE_HOLD)
    m = t % cycle
    if m < G.PULSE_HOLD:
        return 1.0, 1.0
    m -= G.PULSE_HOLD
    if m < G.PULSE_HALF:  # full -> dimmed
        k = m / G.PULSE_HALF
        s = 1.0 + (G.PULSE_SCALE - 1.0) * k
        o = 1.0 + (G.PULSE_OPACITY - 1.0) * k
        return s, o
    m -= G.PULSE_HALF
    if m < G.PULSE_HOLD:
        return G.PULSE_SCALE, G.PULSE_OPACITY
    m -= G.PULSE_HOLD  # dimmed -> full
    k = min(m / G.PULSE_HALF, 1.0)
    s = G.PULSE_SCALE + (1.0 - G.PULSE_SCALE) * k
    o = G.PULSE_OPACITY + (1.0 - G.PULSE_OPACITY) * k
    return s, o


class GlanceAnimOverlay:
    def __init__(self, style: str = "original", width_hint: int = 520, height_hint: int = 320):
        self.controller = AnimController(style=UnlockStyle(style))
        self._st = _AnimState()
        self._width_hint = width_hint
        self._height_hint = height_hint
        self._gtk_ready = False
        self.controller.on_change(self._on_phase)

    # -- controller -> choreography (port of scheduleChoreography) --
    def _on_phase(self, phase: Phase, media: ScanMedia, style: UnlockStyle):
        now = time.monotonic()
        want_expanded = phase in (Phase.SCANNING, Phase.SUCCESS, Phase.FAILURE)
        want_positioned = True if want_expanded else True  # pill rests docked on Linux test
        # NOTE: on macOS undocked pill slides offscreen on disarm; in the test
        # harness we keep it docked so collapse == shrink only. Set
        # want_positioned=False here if your lock-screen flow wants slide-away.
        cur_e = self._st.expand.target > 0.5
        cur_s = self._st.slide_v > 0.5 or (self._st.pending_slide is not None)
        if want_expanded == cur_e and want_positioned == cur_s and self._st.pending_expand is None:
            pass  # still update pulse below
        else:
            self._schedule(now, want_expanded, want_positioned)
        # pulse + media swap
        if phase == Phase.SCANNING:
            if self._st.pulse_t0 is None:
                self._st.pulse_t0 = now
            self._st.pulse_on = True
        else:
            self._st.pulse_on = False
            self._st.pulse_t0 = None
        if self._gtk_ready:
            self._swap_media(media, style)

    def _schedule(self, now: float, want_expanded: bool, want_positioned: bool):
        st = self._st
        st.pending_expand = None
        st.pending_slide = None
        e_to = 1.0 if want_expanded else 0.0
        s_to = 1.0 if want_positioned else 0.0
        e_changing = (e_to > 0.5) != (st.expand.target > 0.5)
        s_changing = abs(s_to - st.slide_v) > 0.01
        if e_changing and s_changing:
            if want_expanded:  # enter: slide now, expand after delay
                self._start_slide(now, s_to)
                st.pending_expand = (now + G.ENTER_EXPANSION_DELAY, e_to,
                                     G.OPEN_RESPONSE, G.OPEN_DAMPING)
            else:  # exit: shrink now, slide after delay
                self._start_expand(e_to)
                st.pending_slide = (now + G.EXIT_SLIDE_DELAY, s_to)
        elif e_changing:
            self._start_expand(e_to)
        elif s_changing:
            self._start_slide(now, s_to)

    def _start_expand(self, to: float):
        if to > 0.5:
            self._st.expand.set_target(1.0, G.OPEN_RESPONSE, G.OPEN_DAMPING)
        else:
            self._st.expand.set_target(0.0, G.CLOSE_RESPONSE, G.CLOSE_DAMPING)

    def _start_slide(self, now: float, to: float):
        self._st.slide.start(now, self._st.slide_v, to, G.SLIDE_DURATION)
        self._st.slide_done = False

    # -- thin wrappers for the face-unlock app --
    def present(self, style: str | None = None):
        self.controller.present(UnlockStyle(style) if style else None)

    def finish(self, success: bool):
        self.controller.finish(success)

    def collapse(self):
        self.controller.collapse()

    def dismiss_immediately(self):
        self.controller.dismiss_immediately()

    def set_style(self, style: str):
        self.controller.style = UnlockStyle(style)

    # -- GTK front-end (only built on show/run) --
    # NOTE: windows are created in the GApplication::activate handler.
    # Creating/presenting before startup emits
    # "New application windows must be added after startup" and shows nothing.
    def show(self):
        import gi
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk
        if getattr(self, "_app", None) is None:
            app = Gtk.Application(application_id="app.glance.animtest")
            app.connect("activate", self._on_activate)
            self._app = app
        return self._app

    def run(self):
        app = self.show()
        app.run([])

    def _on_activate(self, app):
        if getattr(self, "win", None) is not None:
            self.win.present()
            return
        self._build_windows(app)
        self._gtk_ready = True
        self._swap_media(self.controller.media, self.controller.style)
        # start docked so first present() animates expansion only
        self._st.slide_v = 1.0
        self._st.slide.t0 = None
        self.win.present()
        # let any extra windows (test control panel) build now
        for cb in getattr(self, "_extra_builders", []):
            try:
                cb(app)
            except Exception as e:
                print(f"extra window builder failed: {e}")
        # kick off scanning once visible
        if self.controller.phase == Phase.CLOSED:
            self.controller.present()

    def add_extra_builder(self, cb):
        """Test harness hook: cb(app) runs after overlay window is up."""
        self._extra_builders = getattr(self, "_extra_builders", [])
        self._extra_builders.append(cb)
        # if already activated, run immediately
        if getattr(self, "win", None) is not None:
            try:
                cb(self._app)
            except Exception as e:
                print(f"extra window builder failed: {e}")

    def attach(self, app):
        """Embed into an existing Gtk.Application (face-unlock app owns it).

        Call before app.run(); overlay window is built on activate.
        """
        self._app = app
        app.connect("activate", self._on_activate)
        return app

    def _build_windows(self, app):
        import gi
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk, Gdk, GLib

        try:
            gi.require_version("GtkLayerShell", "0.1")
            from gi.repository import GtkLayerShell as Layer
            has_layer = True
        except Exception:
            Layer = None
            has_layer = False

        win = Gtk.ApplicationWindow(application=app)
        win.set_decorated(False)
        win.set_default_size(self._width_hint, self._height_hint)
        css = Gtk.CssProvider()
        css.load_from_data(b"window { background: transparent; }")
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        if has_layer:
            Layer.init_for_window(win)
            Layer.set_layer(win, Layer.Layer.OVERLAY)
            Layer.set_anchor(win, Layer.Edge.TOP, True)
            Layer.set_anchor(win, Layer.Edge.LEFT, True)
            Layer.set_anchor(win, Layer.Edge.RIGHT, True)
            Layer.set_keyboard_mode(win, Layer.KeyboardMode.NONE)
            # click-through: no exclusive zone
            try:
                Layer.set_exclusive_zone(win, 0)
            except Exception:
                pass
        self.win = win
        self._has_layer = has_layer

        fixed = Gtk.Fixed()
        win.set_child(fixed)
        self._fixed = fixed

        # pill background
        from gi.repository import Gtk as _Gtk
        area = _Gtk.DrawingArea()
        area.set_draw_func(self._draw_pill, None)
        area.set_size_request(self._width_hint, self._height_hint)
        fixed.put(area, 0, 0)
        self._area = area

        # media stack: Picture (idle) + Video (mp4)
        pic = Gtk.Picture()
        try:
            pic.set_can_shrink(True)
        except Exception:
            pass
        self._picture = pic
        vid = Gtk.Video()
        vid.set_autoplay(True)
        vid.set_loop(False)
        self._video = vid
        fixed.put(pic, 0, 0)
        fixed.put(vid, 0, 0)

        # minimal lock label (text glyph stands in for SF Symbol lock)
        lock = Gtk.Label(label="🔒")
        lock.add_css_class("glance-lock")
        css2 = Gtk.CssProvider()
        css2.load_from_data(
            b".glance-lock { color: white; font-size: 22px; background: transparent; }")
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), css2, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self._lock = lock
        fixed.put(lock, 0, 0)

        self._last = time.monotonic()
        GLib.timeout_add(16, self._tick)

    def _swap_media(self, media: ScanMedia, style: UnlockStyle):
        from gi.repository import Gtk, Gio
        key = media.value if isinstance(media, ScanMedia) else str(media)
        path = video_res.media_path(key)
        pic, vid = self._picture, self._video
        if key in ("success", "failure"):
            if path and path.suffix == ".mp4":
                try:
                    mf = Gtk.MediaFile.new_for_filename(str(path))
                    mf.set_loop(False)
                    mf.set_muted(True)
                    vid.set_media_stream(mf)
                    mf.play()
                except Exception:
                    pass
            vid.set_visible(True)
            pic.set_visible(False)
        else:
            if path and path.exists():
                try:
                    pic.set_filename(str(path))
                except Exception:
                    try:
                        pic.set_paintable(None)
                    except Exception:
                        pass
            vid.set_visible(False)
            pic.set_visible(True)
        # minimal style shows lock glyph; original hides it
        try:
            self._lock.set_visible(self.controller.style == UnlockStyle.MINIMAL)
            self._lock.set_label("🔓" if self.controller.phase == Phase.SUCCESS else "🔒")
        except Exception:
            pass

    def _tick(self):
        import math
        now = time.monotonic()
        dt = min(now - self._last, 0.05)
        self._last = now
        st = self._st
        # fire delayed halves
        if st.pending_expand and now >= st.pending_expand[0]:
            _, to, resp, zeta = st.pending_expand
            st.pending_expand = None
            st.expand.set_target(to, resp, zeta)
        if st.pending_slide and now >= st.pending_slide[0]:
            _, to = st.pending_slide
            st.pending_slide = None
            st.start_slide = None
            st.slide.start(now, st.slide_v, to, G.SLIDE_DURATION)
            st.slide_done = False
        st.expand.step(dt)
        if not st.slide_done:
            v, done = st.slide.value(now)
            st.slide_v = v
            st.slide_done = done
        # pulse
        if st.pulse_on and st.pulse_t0 is not None:
            ps, po = _pulse_value(now, st.pulse_t0)
        else:
            ps, po = 1.0, 1.0
        self._pulse_s, self._pulse_o = ps, po
        try:
            self._area.queue_draw()
            self._layout_media()
        except Exception:
            pass
        return True

    def _current_geom(self):
        e = min(max(self._st.expand.x, 0.0), 1.15)
        # allow slight overshoot like the macOS spring; clamp for layout
        ec = min(max(e, 0.0), 1.0)
        style = self.controller.style.value
        closed = G.closed_size()
        op = G.open_size(style)
        w = closed.w + (op.w - closed.w) * ec
        h = closed.h + (op.h - closed.h) * ec
        r = (closed.h / 2) + (G.PILL_OPEN_CORNER_RADIUS - closed.h / 2) * ec
        if style == "minimal":
            r = h / 2
        # slide: y from -h-slack (offscreen) to TOP_GAP
        y = (-h - G.PILL_OFFSCREEN_SLACK) * (1 - self._st.slide_v) + G.PILL_TOP_GAP * self._st.slide_v
        return w, h, r, y, getattr(self, "_pulse_s", 1.0), getattr(self, "_pulse_o", 1.0)

    def _draw_pill(self, area, cr, w_hint, h_hint, _data):
        ww = self._width_hint
        cx = ww / 2
        w, h, r, y, ps, po = self._current_geom()
        if self.controller.phase == Phase.CLOSED and self._st.expand.x < 0.01:
            return
        cr.save()
        cr.set_source_rgba(0, 0, 0, 0.92 * po if self.controller.phase == Phase.SCANNING else 0.92)
        x = cx - w / 2
        # rounded rect
        rr = min(r, w / 2, h / 2)
        cr.new_path()
        cr.arc(x + w - rr, y + rr, rr, -math.pi / 2, 0)
        cr.arc(x + w - rr, y + h - rr, rr, 0, math.pi / 2)
        cr.arc(x + rr, y + h - rr, rr, math.pi / 2, math.pi)
        cr.arc(x + rr, y + rr, rr, math.pi, 3 * math.pi / 2)
        cr.close_path()
        cr.fill()
        cr.restore()

    def _layout_media(self):
        try:
            w, h, r, y, ps, po = self._current_geom()
            cx = self._width_hint / 2
            style = self.controller.style.value
            pad = 12.0
            if style == "minimal":
                mw, mh = min(w - 2 * pad, 34), h - 16
                # video right, lock left
                vx = cx + w / 2 - pad - mw
                self._fixed.move(self._video, vx, y + (h - mh) / 2)
                self._video.set_size_request(int(mw), int(mh))
                self._fixed.move(self._lock, cx - w / 2 + pad, y + h / 2 - 14)
                self._picture.set_visible(False)
            else:
                side = max(w, h) * ps - 2 * 16
                side = max(side, 8)
                self._fixed.move(self._video, cx - side / 2, y + (h - side) / 2)
                self._video.set_size_request(int(side), int(side))
                self._fixed.move(self._picture, cx - side / 2, y + (h - side) / 2)
                self._picture.set_size_request(int(side), int(side))
                try:
                    self._video.set_opacity(po)
                    self._picture.set_opacity(po)
                except Exception:
                    pass
        except Exception:
            pass
