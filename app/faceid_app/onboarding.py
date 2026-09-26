"""Guided onboarding and enrollment.

welcome -> camera check -> guided enrollment -> test scan -> done

Enabling face unlock is deliberately NOT the last step of this wizard.
A wizard that ends by switching on an authentication method trains
people to click through it; turning it on is a separate, explicit
choice on the Settings page.

Three rules this window follows:

  * Nothing blocks the main loop. Each pose capture holds the camera
    for seconds, so every one goes out as an async D-Bus call and the
    reply arrives on the main loop.
  * A daemon that is down produces a message, never a traceback. Every
    entry point is wrapped, because "I opened the settings app and it
    crashed" is how people end up with a half-enrolled account.
  * Preview frames are drawn and dropped. They are never written
    anywhere, and the pipe is closed on cancel, finish and window close.
"""
from __future__ import annotations

import math
import struct

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Adw, Gdk, GdkPixbuf, Gio, GLib, Graphene, Gsk, Gtk  # noqa: E402

from .dbus_client import Daemon, DaemonError, Enrollment  # noqa: E402
from . import prefs  # noqa: E402

# Status strings from daemon/src/enroll.rs. Kept as constants so a
# rename on either side shows up as a NameError, not a silent no-op.
ST_ACQUIRING = "acquiring"
ST_GOOD = "good"
ST_POSE_COMPLETE = "pose_complete"
ST_FAILED = "failed"
ST_CANCELLED = "cancelled"
ST_FINISHED = "finished"


class CircularPreview(Gtk.Widget):
    """A live camera texture clipped to a PERFECT circle, cover-fitted,
    without ever writing a frame to disk.

    Cover-fit means the texture is scaled up until it fills the widget
    and cropped: you always see the whole face area, never letterboxed
    bars. CSS border-radius does not clip a Gtk.Picture's texture
    reliably, so the roundness comes from a snapshot clip. For a square
    widget a Gsk.RoundedRect whose corner radius equals half the side
    length is exactly a circle -- identical on every GTK renderer,
    including the software renderer used by remote sessions.
    """

    __gtype_name__ = "FaceidCircularPreview"

    def __init__(self, size: int = 320):
        super().__init__(width_request=size, height_request=size,
                         halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
        self._texture = None
        self._background = Gdk.RGBA()
        self._background.parse("#0e1522")

    def set_texture(self, texture) -> None:
        self._texture = texture
        self.queue_draw()

    def do_snapshot(self, snapshot) -> None:
        width, height = self.get_width(), self.get_height()
        if width <= 0 or height <= 0:
            return
        side = min(width, height)
        bounds = Graphene.Rect().init(0, 0, width, height)
        circle = Gsk.RoundedRect()
        circle.init_from_rect(bounds, side / 2)
        snapshot.push_rounded_clip(circle)
        snapshot.append_color(self._background, bounds)

        if self._texture is not None:
            tw, th = self._texture.get_width(), self._texture.get_height()
            if tw > 0 and th > 0:
                scale = max(width / tw, height / th)
                draw_w, draw_h = tw * scale, th * scale
                texture_bounds = Graphene.Rect().init(
                    (width - draw_w) / 2, (height - draw_h) / 2,
                    draw_w, draw_h)
                snapshot.append_texture(self._texture, texture_bounds)
        snapshot.pop()


class PreviewReader:
    """Reads the daemon's live preview pipe.

    Framing is one type byte, then a 4-byte big-endian length, then
    that many payload bytes -- the same framing daemon/src/enroll.rs
    and daemon/src/dbus.rs (StartPreview) write:

        b'J' + u32be + jpeg bytes          -> on_frame(texture)
        b'G' + u32be + "status|reason"    -> on_guidance(status, reason)

    Reads are async so a stalled camera never freezes the UI, and EOF
    is treated as a normal end of session rather than an error.
    """

    TYPE = 1
    HEADER = 4
    MAX_FRAME = 256 * 1024      # refuse anything absurd rather than allocate it
    TYPE_FRAME = b"J"
    TYPE_GUIDE = b"G"

    def __init__(self, fd: int, on_frame, on_guidance=None, on_eof=None):
        self._stream = Gio.UnixInputStream.new(fd, True)  # takes ownership
        self._on_frame = on_frame
        self._on_guidance = on_guidance
        self._on_eof = on_eof
        self._buf = b""
        self._want = self.TYPE
        self._stage = "type"
        self._closed = False
        self._read()

    def _read(self) -> None:
        if self._closed:
            return
        self._stream.read_bytes_async(
            65536, GLib.PRIORITY_DEFAULT, None, self._on_read, None)

    def _on_read(self, stream, res, _user) -> None:
        if self._closed:
            return
        try:
            data = stream.read_bytes_finish(res).get_data()
        except GLib.Error:
            self.close()
            return
        if not data:
            self.close()
            return

        self._buf += data
        while len(self._buf) >= self._want:
            chunk, self._buf = self._buf[:self._want], self._buf[self._want:]
            if self._stage == "type":
                if chunk not in (self.TYPE_FRAME, self.TYPE_GUIDE):
                    self.close()
                    return
                self._kind = chunk
                self._want = self.HEADER
                self._stage = "length"
            elif self._stage == "length":
                (length,) = struct.unpack(">I", chunk)
                if length == 0 or length > self.MAX_FRAME:
                    self.close()
                    return
                self._want = length
                self._stage = "payload"
            else:
                self._emit(self._kind, chunk)
                self._want = self.TYPE
                self._stage = "type"
        self._read()

    def _emit(self, kind: bytes, payload: bytes) -> None:
        if kind == self.TYPE_FRAME:
            try:
                loader = GdkPixbuf.PixbufLoader.new_with_type("jpeg")
                loader.write(payload)
                loader.close()
                pixbuf = loader.get_pixbuf()
            except GLib.Error:
                return      # a torn frame is not worth interrupting the scan
            if pixbuf is not None and self._on_frame:
                self._on_frame(Gdk.Texture.new_for_pixbuf(pixbuf))
        elif kind == self.TYPE_GUIDE and self._on_guidance:
            line = payload.split(b"|", 1)
            status = line[0].decode("utf-8", "replace")
            reason = line[1].decode("utf-8", "replace") if len(line) > 1 else ""
            self._on_guidance(status, reason)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._stream.close(None)
        except GLib.Error:
            pass
        if self._on_eof:
            cb, self._on_eof = self._on_eof, None
            cb()


GUIDANCE = {
    "face too small": "Move closer to the camera",
    "blurry": "Hold still — your face is blurry",
    "too dark": "Too dark — raise the light",
    "overexposed": "Too bright — move out of direct light",
    "head turned too far": "Look straight at the camera",
    "head tilted too far": "Tilt your head level",
    "low detection confidence": "Center your face in the circle",
    "box outside frame": "Move your face into the circle",
}


def guidance_for(status: str, reason: str) -> str:
    """Friendly live guidance derived from the worker's own quality gate."""
    if status == ST_GOOD:
        return "Face detected — hold still"
    if reason == "no face":
        return "Center your face in the circle"
    return GUIDANCE.get(reason, "Position your face inside the circle")


class OnboardingWindow(Adw.Window):
    """The wizard. Owns one Enrollment session at a time."""

    _css_installed = False

    def __init__(self, parent: Gtk.Window, daemon: Daemon,
                 identity: str = "default", on_done=None,
                 camera_summary: str | None = None):
        super().__init__(transient_for=parent, modal=True,
                         title="Set up face unlock",
                         default_width=560, default_height=640)
        self.daemon = daemon
        self.identity = identity
        self.on_done = on_done
        self.camera_summary = camera_summary

        self.enroll = Enrollment()
        self.preview: PreviewReader | None = None
        self.poses: list[str] = []
        self.pose_index = 0
        self.captured = 0
        self._progress_sub = 0
        self._running = False
        self._scan_phase = 0.0
        self._scan_fraction = 0.0
        self._pose_progress = 0.0
        self._scan_detected = False
        self._scan_success = False
        self._scan_error = False
        self._scan_complete = False
        self._last_anim_ts = 0.0
        self._scan_source = 0

        self._install_scan_css()
        self.add_css_class("face-scan-window")

        self.toasts = Adw.ToastOverlay()
        self.stack = Adw.ViewStack()
        self.stack.add_named(self._welcome_page(), "welcome")
        self.stack.add_named(self._camera_page(), "camera")
        self.stack.add_named(self._enroll_page(), "enroll")
        self.stack.add_named(self._test_page(), "test")
        self.stack.add_named(self._done_page(), "done")

        view = Adw.ToolbarView()
        self.header = Adw.HeaderBar(show_end_title_buttons=True)
        self.header.set_title_widget(self._notch())
        view.add_top_bar(self.header)
        view.set_content(self.stack)
        self.toasts.set_child(view)
        self.set_content(self.toasts)

        self.connect("close-request", self._on_close)

    @classmethod
    def _install_scan_css(cls) -> None:
        """Install the small, app-local visual language once per process."""
        if cls._css_installed:
            return
        css = b"""
        window.face-scan-window { background: #0a0f16; }
        window.face-scan-window headerbar {
          background: transparent;
          box-shadow: none;
          min-height: 52px;
        }
        #faceid-notch {
          background: #06080d;
          border: 1px solid rgba(255,255,255,.10);
          border-radius: 999px;
          box-shadow: 0 8px 24px rgba(0,0,0,.35);
          padding: 7px 15px;
        }
        #faceid-notch label { color: #e8edf7; font-weight: 700; }
        #faceid-notch image { color: #85d9ff; }
        .scan-title {
          color: #8d99ad;
          font-weight: 600;
          font-size: 13px;
          letter-spacing: 0.08em;
        }
        .scan-prompt {
          color: #f2f6ff;
          font-weight: 800;
          font-size: 25px;
          letter-spacing: 0.2px;
        }
        .scan-status { color: #aeb9ca; font-size: 15px; }
        .scan-counter { color: #8d99ad; font-weight: 600; }
        .pose-dots {
          margin-top: 8px;
          margin-bottom: 2px;
        }
        .pose-dot {
          min-width: 8px;
          min-height: 8px;
          border-radius: 4px;
          background: #273246;
        }
        .pose-dot.done {
          background: #82dcff;
          box-shadow: 0 0 8px 1px rgba(130, 220, 255, .55);
        }
        .pose-dot.current {
          background: #5b6c85;
          min-width: 10px;
          min-height: 10px;
          border-radius: 5px;
        }
        .scan-cancel { color: #b9c2d1; }
        .scan-retry { border-radius: 999px; padding: 7px 18px; }
        .scan-error-card {
          background: #161d2b;
          border: 1px solid rgba(255,120,110,.20);
          border-radius: 16px;
          padding: 14px 20px;
        }
        .scan-error-card .err-title { color: #ff9d94; font-weight: 700; }
        .scan-error-card .err-desc { color: #aeb9ca; font-size: 0.9em; }
        """
        provider = Gtk.CssProvider()
        provider.load_from_data(css)
        display = Gdk.Display.get_default()
        if display is not None:
            Gtk.StyleContext.add_provider_for_display(
                display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        cls._css_installed = True

    def _notch(self) -> Gtk.Widget:
        notch = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=7)
        notch.set_name("faceid-notch")
        notch.append(Gtk.Image(icon_name="face-smile-symbolic", pixel_size=16))
        self.notch_label = Gtk.Label(label="Face scan")
        notch.append(self.notch_label)
        return notch

    # ---- pages ---------------------------------------------------------
    def _page_shell(self, title, body, icon="camera-photo-symbolic"):
        status = Adw.StatusPage(title=title, description=body, icon_name=icon)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                      margin_start=24, margin_end=24, margin_bottom=24)
        box.append(status)
        return box, status

    def _welcome_page(self) -> Gtk.Widget:
        box, _ = self._page_shell(
            "Set up face unlock",
            "You will be guided through nine head positions. It takes about "
            "twenty seconds.\n\n"
            "No photographs are kept. Enrollment stores only numeric "
            "templates, encrypted and readable by root alone.\n\n"
            "This is a convenience feature, not Face ID. On an ordinary "
            "webcam it can be defeated by a good video replay, and your "
            "password always works.",
            "avatar-default-symbolic")
        btn = Gtk.Button(label="Continue", halign=Gtk.Align.CENTER)
        btn.add_css_class("suggested-action")
        btn.add_css_class("pill")
        btn.connect("clicked", lambda _b: self._go_camera())
        box.append(btn)
        return box

    def _camera_page(self) -> Gtk.Widget:
        box, self.camera_status = self._page_shell(
            "Checking your camera", "One moment.", "camera-web-symbolic")
        self.camera_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE,
                                       margin_top=8)
        self.camera_list.add_css_class("boxed-list")
        box.append(self.camera_list)

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8,
                      halign=Gtk.Align.CENTER, margin_top=12)
        again = Gtk.Button(label="Check again")
        again.connect("clicked", lambda _b: self._go_camera())
        self.camera_next = Gtk.Button(label="Start enrollment")
        self.camera_next.add_css_class("suggested-action")
        self.camera_next.add_css_class("pill")
        self.camera_next.set_sensitive(False)
        self.camera_next.connect("clicked", lambda _b: self._go_enroll())
        row.append(again)
        row.append(self.camera_next)
        box.append(row)
        return box

    def _enroll_page(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=7,
                      margin_start=24, margin_end=24,
                      margin_top=2, margin_bottom=20,
                      valign=Gtk.Align.CENTER)

        self.scan_title = Gtk.Label(label="Face enrollment")
        self.scan_title.add_css_class("scan-title")
        self.scan_title.set_halign(Gtk.Align.CENTER)
        box.append(self.scan_title)

        self.pose_label = Gtk.Label(label="Look straight ahead")
        self.pose_label.add_css_class("scan-prompt")
        self.pose_label.set_halign(Gtk.Align.CENTER)
        box.append(self.pose_label)

        self.pose_status = Gtk.Label(
            label="Position your face inside the circle")
        self.pose_status.add_css_class("scan-status")
        self.pose_status.set_halign(Gtk.Align.CENTER)
        box.append(self.pose_status)

        self.pose_counter = Gtk.Label(label="Scan 1 of 9")
        self.pose_counter.add_css_class("scan-counter")
        self.pose_counter.set_halign(Gtk.Align.CENTER)
        box.append(self.pose_counter)

        self.pose_dots = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL,
                                 spacing=8, halign=Gtk.Align.CENTER)
        self.pose_dots.add_css_class("pose-dots")
        box.append(self.pose_dots)

        self.stage = Gtk.Overlay(halign=Gtk.Align.CENTER, margin_top=4)
        self.picture = CircularPreview()
        self.stage.set_child(self.picture)
        self.scan_ring = Gtk.DrawingArea(
            width_request=320, height_request=320,
            halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER,
            can_target=False)
        self.scan_ring.set_draw_func(self._draw_scan_guide)
        self.stage.add_overlay(self.scan_ring)
        box.append(self.stage)

        self.camera_footer = Gtk.Label(
            label=self.camera_summary or "",
            halign=Gtk.Align.CENTER, margin_top=2)
        self.camera_footer.add_css_class("scan-counter")
        box.append(self.camera_footer)

        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10,
                           halign=Gtk.Align.CENTER, margin_top=6)
        self.cancel_btn = Gtk.Button(label="Cancel")
        self.cancel_btn.add_css_class("flat")
        self.cancel_btn.add_css_class("scan-cancel")
        self.cancel_btn.connect("clicked", lambda _b: self._cancel_enrollment())
        self.retry_btn = Gtk.Button(label="Retry this pose")
        self.retry_btn.add_css_class("suggested-action")
        self.retry_btn.add_css_class("scan-retry")
        self.retry_btn.set_visible(False)
        self.retry_btn.connect("clicked", lambda _b: self._capture_pose())
        controls.append(self.cancel_btn)
        controls.append(self.retry_btn)
        box.append(controls)

        box.append(self._error_card())
        return box

    def _error_card(self) -> Gtk.Widget:
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8,
                       halign=Gtk.Align.CENTER)
        card.add_css_class("scan-error-card")
        card.set_visible(False)
        icon = Gtk.Image(icon_name="dialog-warning-symbolic", pixel_size=20)
        icon.set_halign(Gtk.Align.CENTER)
        card.append(icon)
        self.err_title = Gtk.Label(label="Authentication unavailable")
        self.err_title.add_css_class("err-title")
        self.err_title.set_halign(Gtk.Align.CENTER)
        card.append(self.err_title)
        self.err_desc = Gtk.Label(label="", wrap=True,
                                  justify=Gtk.Justification.CENTER)
        self.err_desc.add_css_class("err-desc")
        self.err_desc.set_max_width_chars(46)
        self.err_desc.set_halign(Gtk.Align.CENTER)
        card.append(self.err_desc)
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8,
                      halign=Gtk.Align.CENTER, margin_top=2)
        self.err_retry = Gtk.Button(label="Try again")
        self.err_retry.add_css_class("suggested-action")
        self.err_retry.add_css_class("scan-retry")
        self.err_retry.connect("clicked", lambda _b: self._retry_enrollment())
        self.err_close = Gtk.Button(label="Close")
        self.err_close.add_css_class("flat")
        self.err_close.add_css_class("scan-cancel")
        self.err_close.connect("clicked", lambda _b: self.close())
        row.append(self.err_retry)
        row.append(self.err_close)
        card.append(row)
        self.error_card = card
        return card

    def _test_page(self) -> Gtk.Widget:
        box, self.test_status = self._page_shell(
            "Try it out",
            "Run a scan to see whether you are recognised. This does not "
            "unlock anything.", "emblem-ok-symbolic")
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8,
                      halign=Gtk.Align.CENTER)
        t = Gtk.Button(label="Test scan")
        t.add_css_class("pill")
        t.connect("clicked", lambda _b: self._test_scan())
        nxt = Gtk.Button(label="Finish")
        nxt.add_css_class("suggested-action")
        nxt.add_css_class("pill")
        nxt.connect("clicked", lambda _b: self.stack.set_visible_child_name("done"))
        row.append(t)
        row.append(nxt)
        box.append(row)
        return box

    def _done_page(self) -> Gtk.Widget:
        box, _ = self._page_shell(
            "Your face is enrolled",
            "Face unlock is still switched OFF.\n\n"
            "Turn it on from the Settings page when you are ready. On "
            "Wayland, log out and back in once so GNOME loads the "
            "lock-screen indicator.",
            "emblem-default-symbolic")
        btn = Gtk.Button(label="Close", halign=Gtk.Align.CENTER)
        btn.add_css_class("pill")
        btn.connect("clicked", lambda _b: self.close())
        box.append(btn)
        return box

    # ---- scanner presentation ----------------------------------------
    def _draw_scan_guide(self, _area, cr, width, height, _data=None) -> None:
        """Draw the circular biometric scanner.

        The ring is the scanner: a thin low-opacity track, an arc that
        brightens and lengthens as a pose is captured (it climbs while a
        detection is held, and rotates so the sweep looks alive without
        ever becoming a spinner), a green completion arc for the whole
        enrollment, subtle detection dots, and compact success / error
        markers. No oval, no silhouette, no drawn face: the user's real
        face from the camera stays visible through the middle.
        """
        cx, cy = width / 2, height / 2
        radius = min(width, height) / 2 - 12
        tau = math.tau
        error = self._scan_error
        complete = self._scan_complete
        success = self._scan_success or complete
        busy = self._running and not error and not success

        if error:
            accent = (1.0, 0.45, 0.42)
        elif complete:
            accent = (0.41, 0.91, 0.68)
        elif self._scan_detected:
            accent = (0.47, 0.89, 1.0)
        else:
            accent = (0.47, 0.82, 1.0)

        # Dark bezel band just inside the feed's edge, so the viewport
        # has a calm container instead of raw webcam framing.
        cr.set_line_width(11.0)
        cr.set_source_rgba(0.02, 0.04, 0.08, 0.92)
        cr.arc(cx, cy, radius + 2, 0, tau)
        cr.stroke()
        cr.set_line_width(1.0)
        cr.set_source_rgba(0.55, 0.66, 0.82, 0.10)
        cr.arc(cx, cy, radius - 0.5, 0, tau)
        cr.stroke()

        # Outer low-opacity track (the whole circle).
        cr.set_line_width(2.0)
        cr.set_source_rgba(0.42, 0.55, 0.72, 0.22)
        cr.arc(cx, cy, radius, 0, tau)
        cr.stroke()

        # Overall enrollment progress: a quiet green arc that fills up
        # as poses complete. This is the circular progress bar.
        if self._scan_fraction > 0.0:
            cr.set_line_width(2.2)
            cr.set_source_rgba(0.41, 0.91, 0.68, 0.55)
            cr.arc(cx, cy, radius, -math.pi / 2,
                   -math.pi / 2 + tau * self._scan_fraction)
            cr.stroke()

        # The live scanning arc. It rotates slowly and lengthens with
        # the detection held inside the circle (0.10 .. ~0.95 of the
        # circumference). A short faint tail follows it.
        if busy:
            length = 0.10 + 0.85 * min(max(self._pose_progress, 0.0), 1.0)
            a0 = -math.pi / 2 + self._scan_phase * tau
            a1 = a0 - tau * length
            cr.set_line_width(3.0)
            cr.set_source_rgba(*accent, 0.95)
            cr.set_line_cap(1)          # round caps: smooth, no spikes
            cr.arc(cx, cy, radius, a1, a0)
            cr.stroke()
            cr.set_line_width(1.6)
            cr.set_source_rgba(*accent, 0.20)
            cr.arc(cx, cy, radius, a1 - tau * 0.16, a1)
            cr.stroke()

        # While a face is held, the whole ring lights up a notch and a
        # few restrained tracking dots pulse inside the ring band.
        if self._scan_detected and not error and not complete:
            cr.set_line_width(2.2)
            cr.set_source_rgba(*accent, 0.65)
            cr.arc(cx, cy, radius, 0, tau)
            cr.stroke()
            for k in range(8):
                ang = tau * k / 8 + math.pi / 8
                px = cx + (radius - 7) * math.cos(ang)
                py = cy + (radius - 7) * math.sin(ang)
                pulse = 0.5 + 0.5 * math.sin(self._scan_phase * tau * 2 + k)
                cr.set_source_rgba(*accent, 0.30 + 0.25 * pulse)
                cr.arc(px, py, 1.7, 0, tau)
                cr.fill()

        # Compact confirmation: a small disc + checkmark in the middle.
        # The face stays fully visible around it; it is only shown for
        # the brief success beat between poses and on the final screen.
        if success and not error:
            cr.set_line_cap(1)
            cr.set_source_rgba(0.01, 0.03, 0.06, 0.78)
            cr.arc(cx, cy, 18, 0, tau)
            cr.fill()
            cr.set_line_width(2.2)
            cr.set_source_rgba(0.41, 0.91, 0.68, 0.95)
            cr.arc(cx, cy, 16, 0, tau)
            cr.stroke()
            cr.set_line_width(3.0)
            cr.set_source_rgba(0.41, 0.91, 0.68, 1.0)
            cr.move_to(cx - 6.5, cy)
            cr.line_to(cx - 1.5, cy + 5.5)
            cr.line_to(cx + 7, cy - 5)
            cr.stroke()

        # Auth/scan error: the ring freezes, dims and turns red, but the
        # viewport itself is untouched -- an error never destroys the
        # scanner. Guidance lives in the card below the circle.
        if error:
            cr.set_line_width(2.6)
            cr.set_source_rgba(*accent, 0.85)
            cr.arc(cx, cy, radius, 0, tau)
            cr.stroke()
            cr.set_line_width(1.4)
            cr.set_source_rgba(*accent, 0.35)
            cr.arc(cx, cy, radius + 4, 0, tau)
            cr.stroke()

    def _set_scan_progress(self, fraction: float) -> None:
        self._scan_fraction = max(0.0, min(1.0, fraction))
        self.scan_ring.queue_draw()

    def _motion_ok(self) -> bool:
        settings = Gtk.Settings.get_default()
        return bool(settings.get_property("gtk-enable-animations"))

    def _start_scan_animation(self) -> None:
        if self._scan_source:
            return
        if not self._motion_ok():
            # Reduced motion: no rotation, but state changes still
            # redraw the ring so progress is never conveyed by
            # animation alone.
            self.scan_ring.queue_draw()
            return
        self._last_anim_ts = GLib.get_monotonic_time() / 1e6
        self._scan_source = GLib.timeout_add(33, self._animate_scan)

    def _animate_scan(self) -> bool:
        now = GLib.get_monotonic_time() / 1e6
        dt = min(max(now - self._last_anim_ts, 0.0), 0.1)
        self._last_anim_ts = now
        rotating = self._running and not self._scan_error \
            and not self._scan_success and not self._scan_complete
        if rotating:
            # About one revolution every two seconds.
            self._scan_phase = (self._scan_phase + dt * 0.5) % 1.0
        self.scan_ring.queue_draw()
        return GLib.SOURCE_CONTINUE

    def _stop_scan_animation(self) -> None:
        if self._scan_source:
            GLib.source_remove(self._scan_source)
            self._scan_source = 0

    # ---- flow ----------------------------------------------------------
    @staticmethod
    def _friendly_diag(key: str, value) -> str:
        if value is None:
            return "not set"
        if value is True:
            return "yes"
        if value is False:
            return "no"
        if key == "ir_camera" and not str(value):
            return "not set"
        return str(value)

    def _toast(self, text: str) -> None:
        self.toasts.add_toast(Adw.Toast(title=text))

    def _go_camera(self) -> None:
        self.stack.set_visible_child_name("camera")
        child = self.camera_list.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self.camera_list.remove(child)
            child = nxt

        try:
            diag = self.daemon.diagnostics()
        except DaemonError as e:
            self.camera_status.set_title("Cannot reach the service")
            self.camera_status.set_description(
                f"{e}\n\nCheck it with: faceid-nim status")
            self.camera_next.set_sensitive(False)
            return

        ok = bool(diag.get("worker_reachable"))
        for key, value in diag.items():
            row = Adw.ActionRow(title=prefs.diag_title(key),
                                subtitle=self._friendly_diag(key, value))
            self.camera_list.append(row)

        if ok:
            self.camera_status.set_title("Camera looks fine")
            self.camera_status.set_description(
                "Sit facing the screen in even light. Take off anything "
                "covering your face.")
        else:
            self.camera_status.set_title("The vision worker is not running")
            self.camera_status.set_description(
                "Models may not be downloaded yet.\n"
                "Run:  sudo faceid-nim fetch-models")
        self.camera_next.set_sensitive(ok)

    def _go_enroll(self) -> None:
        self.stack.set_visible_child_name("enroll")
        self.notch_label.set_text("Face scan")
        self._reset_scanner()
        try:
            self.poses = self.enroll.list_poses()
        except DaemonError:
            self.poses = []
        if not self.poses:
            self.poses = ["Look straight ahead"]
        self._start_enroll_session()

    def _reset_scanner(self) -> None:
        self._scan_error = False
        self._scan_complete = False
        self._scan_success = False
        self._scan_detected = False
        self._pose_progress = 0.0
        self._set_scan_progress(0.0)
        self.pose_status.set_text("Position your face inside the circle")
        self.scan_ring.queue_draw()

    def _start_enroll_session(self) -> None:
        self._hide_error_card()
        self._reset_scanner()
        try:
            fd = self.enroll.start(self.identity)
        except DaemonError as e:
            self._fail(f"Could not start enrollment: {e}")
            return

        self._progress_sub = self.enroll.subscribe_progress(self._on_progress)
        self.preview = PreviewReader(fd, self._on_frame,
                                     self._on_guidance, self._on_preview_eof)
        self.pose_index = 0
        self.captured = 0
        self._build_pose_dots()
        self._update_pose_labels()
        self._start_scan_animation()
        self._capture_pose()

    def _retry_enrollment(self) -> None:
        """Try again after a fatal start/service error."""
        self._stop_scan_animation()
        self._close_preview()
        try:
            self.enroll.cancel()
        except Exception:
            pass
        self._start_enroll_session()

    def _build_pose_dots(self) -> None:
        child = self.pose_dots.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self.pose_dots.remove(child)
            child = nxt
        for _i in range(len(self.poses)):
            dot = Gtk.Label(label="", width_request=8, height_request=8)
            dot.add_css_class("pose-dot")
            self.pose_dots.append(dot)

    def _update_pose_labels(self) -> None:
        name = self.poses[min(self.pose_index, len(self.poses) - 1)]
        self.pose_label.set_text(name)
        # pose_index is incremented to len(poses) when the last pose
        # finishes; keep the label on "Scan 9 of 9" (never "Scan 10 of 9").
        self.pose_counter.set_text(
            f"Scan {min(self.pose_index + 1, len(self.poses))} of {len(self.poses)}")
        dot = self.pose_dots.get_first_child()
        i = 0
        while dot is not None:
            next_dot = dot.get_next_sibling()
            dot.remove_css_class("done")
            dot.remove_css_class("current")
            if i < self.captured:
                dot.add_css_class("done")
            elif i == self.pose_index:
                dot.add_css_class("current")
            dot = next_dot
            i += 1

    def _capture_pose(self) -> None:
        if self._running:
            return
        self._running = True
        self._scan_error = False
        self._scan_complete = False
        self._scan_success = False
        self._scan_detected = False
        self._pose_progress = 0.0
        self.notch_label.set_text("Scanning")
        self.retry_btn.set_visible(False)
        self.pose_status.set_text("Scanning face…")
        self._update_pose_labels()
        self._start_scan_animation()
        self.scan_ring.queue_draw()
        self.enroll.enroll_pose_async(self.pose_index, self._on_pose_done)

    def _on_pose_done(self, status: str, progress: float,
                      error: str | None) -> None:
        self._running = False
        if error:
            self.pose_status.set_text("Enrollment service error")
            self._fail(error)
            return
        if status == ST_CANCELLED:
            return
        if status == ST_FAILED:
            self._scan_error = True
            self.notch_label.set_text("Scan paused")
            self.pose_status.set_text(
                "Could not capture that pose — more light, or come closer")
            self.retry_btn.set_visible(True)
            self.scan_ring.queue_draw()
            return

        # Success beat: green ring + checkmark for half a second, then
        # move on, so the user can read the next prompt before the
        # camera opens again.
        self._scan_success = True
        self._pose_progress = 1.0
        self.captured += 1
        self.pose_status.set_text("Face captured")
        self._set_scan_progress(min(1.0, self.captured / len(self.poses)))
        self.scan_ring.queue_draw()

        self.pose_index += 1
        if self.pose_index >= len(self.poses):
            GLib.timeout_add(520, self._advance_finish)
        else:
            GLib.timeout_add(450, self._capture_next)

    def _capture_next(self) -> bool:
        self._capture_pose()
        return GLib.SOURCE_REMOVE

    def _advance_finish(self) -> bool:
        self._finish()
        return GLib.SOURCE_REMOVE

    def _finish(self) -> None:
        self._scan_complete = True
        self._scan_success = False
        self.notch_label.set_text("Scan complete")
        self._set_scan_progress(1.0)
        self.captured = len(self.poses)
        self._update_pose_labels()
        self.pose_label.set_text("Face ID setup complete")
        self.pose_status.set_text("Securing your enrollment…")
        self._stop_scan_animation()
        self.scan_ring.queue_draw()
        self.enroll.finish_async(self._on_finished)

    def _on_finished(self, saved: int, error: str | None) -> None:
        self._close_preview()
        self._stop_scan_animation()
        if error:
            self._fail(error)
            return
        self.test_status.set_title("Enrolled")
        self.test_status.set_description(
            f"Saved {saved} templates. Run a test scan to check it works.")
        self.stack.set_visible_child_name("test")
        if self.on_done:
            self.on_done()

    def _test_scan(self) -> None:
        try:
            ok, msg = self.daemon.test_scan()
        except DaemonError as e:
            self._toast(str(e))
            return
        self.test_status.set_title("Recognised" if ok else "Not recognised")
        self.test_status.set_description(
            "That is the behaviour you will get at the lock screen."
            if ok else (msg or "Try again in better light."))

    def _cancel_enrollment(self) -> None:
        self._stop_scan_animation()
        self._close_preview()
        self.enroll.cancel()
        self.close()

    def _fail(self, message: str) -> None:
        """A fatal (usually polkit/service) error.

        The scanner itself is preserved: the ring just freezes red, the
        pose guidance makes room, and a compact card below explains the
        error and offers a retry. No huge persistent toast over the
        viewport.
        """
        friendly = self._friendly_error(message)
        if "polkit" in friendly.lower() or "authorization" in friendly.lower():
            agents = self.daemon.polkit_agent_available()
            if agents is False:
                friendly = (
                    "No authentication agent is running in this session, "
                    "so a password prompt cannot appear. Log out and back "
                    "in, or use the Settings app for this step.")
            elif agents is None:
                friendly = (
                    "PolicyKit itself is not responding. The system auth "
                    "service may be starting up -- try again in a moment.")
        title, desc = self._error_card_text(friendly)
        self._scan_error = True
        self._running = False
        self._stop_scan_animation()
        self._close_preview()
        self.notch_label.set_text("Enrollment paused")
        self.retry_btn.set_visible(False)
        self.err_title.set_text(title)
        self.err_desc.set_text(desc)
        self.error_card.set_visible(True)
        self.scan_ring.queue_draw()

    def _hide_error_card(self) -> None:
        self.error_card.set_visible(False)

    @staticmethod
    def _friendly_error(message: str) -> str:
        """Keep implementation details out of an already stressful flow."""
        lower = message.lower()
        if "polkit" in lower or "authorization" in lower:
            return ("Authentication could not be started. Make sure a "
                    "PolicyKit authentication prompt is available, then try again.")
        if "org.freedesktop.dbus.error" in lower:
            return "The face unlock service did not accept the enrollment request."
        return message

    @staticmethod
    def _error_card_text(message: str) -> tuple[str, str]:
        """Title + description pair for the compact error card."""
        lower = message.lower()
        if "polkit" in lower or "authorization" in lower:
            return ("Authentication unavailable",
                    "Make sure a PolicyKit authentication prompt is "
                    "available, then try again.")
        if "org.freedesktop.dbus.error" in lower:
            return ("Enrollment service unavailable",
                    "The face unlock service did not accept the "
                    "enrollment request.")
        if len(message) > 140:
            message = message[:139] + "…"
        return ("Enrollment could not continue", message)

    # ---- preview -------------------------------------------------------
    def _on_frame(self, texture) -> None:
        self.picture.set_texture(texture)

    def _on_guidance(self, status: str, reason: str) -> None:
        if self._scan_success or self._scan_complete or self._scan_error:
            return
        if status == ST_GOOD:
            self._scan_detected = True
        self.pose_status.set_text(guidance_for(status, reason))
        self.scan_ring.queue_draw()

    def _on_preview_eof(self) -> None:
        self.picture.set_texture(None)

    def _close_preview(self) -> None:
        if self.preview is not None:
            self.preview.close()
            self.preview = None

    def _on_progress(self, _session, pose_index, pose_name, status,
                     progress) -> None:
        status = str(status)
        if status in (ST_FINISHED, ST_CANCELLED):
            return
        if pose_name:
            self.pose_label.set_text(str(pose_name))
            shown = min(int(pose_index) + 1, len(self.poses) or 9)
            self.pose_counter.set_text(
                f"Scan {shown} of {len(self.poses) or 9}")
        if status == ST_ACQUIRING:
            self.pose_status.set_text("Scanning face…")
        elif status == ST_GOOD:
            p = min(max(float(progress or 0.0), 0.0), 1.0)
            self._scan_detected = True
            self._pose_progress = p
            self.pose_status.set_text("Face detected — hold still")
            overall = min(1.0, (int(pose_index) + p) / max(1, len(self.poses)))
            self._set_scan_progress(max(self._scan_fraction, overall))
        elif status == ST_POSE_COMPLETE:
            self.pose_status.set_text("Face captured")
        elif status == ST_FAILED:
            self.pose_status.set_text("Could not capture that pose")
        self.scan_ring.queue_draw()

    # ---- teardown ------------------------------------------------------
    def _on_close(self, *_a) -> bool:
        # Closing the window must release the camera and the pipe, not
        # leave a session open in the daemon.
        self._stop_scan_animation()
        self._close_preview()
        try:
            self.enroll.close()
        except Exception:
            pass
        return False
