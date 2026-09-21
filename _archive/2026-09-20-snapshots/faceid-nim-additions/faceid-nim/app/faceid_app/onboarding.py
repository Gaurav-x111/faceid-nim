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

import struct

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Adw, Gdk, GdkPixbuf, Gio, GLib, Gtk  # noqa: E402

from .dbus_client import Daemon, DaemonError, Enrollment  # noqa: E402

# Status strings from daemon/src/enroll.rs. Kept as constants so a
# rename on either side shows up as a NameError, not a silent no-op.
ST_ACQUIRING = "acquiring"
ST_GOOD = "good"
ST_POSE_COMPLETE = "pose_complete"
ST_FAILED = "failed"
ST_CANCELLED = "cancelled"
ST_FINISHED = "finished"

HUMAN_STATUS = {
    ST_ACQUIRING: "Looking for your face...",
    ST_GOOD: "Hold it there",
    ST_POSE_COMPLETE: "Got it",
    ST_FAILED: "Could not capture that pose",
    ST_CANCELLED: "Cancelled",
}


class PreviewReader:
    """Reads length-prefixed JPEGs off the daemon's preview pipe.

    Framing is 4 bytes big-endian length, then that many JPEG bytes --
    the same framing daemon/src/enroll.rs writes. Reads are async so a
    stalled camera never freezes the UI, and EOF is treated as a normal
    end of session rather than an error.
    """

    HEADER = 4
    MAX_FRAME = 256 * 1024      # refuse anything absurd rather than allocate it

    def __init__(self, fd: int, on_frame, on_eof=None):
        self._stream = Gio.UnixInputStream.new(fd, True)  # takes ownership
        self._on_frame = on_frame
        self._on_eof = on_eof
        self._buf = b""
        self._want = self.HEADER
        self._need_header = True
        self._closed = False
        self._read()

    def _read(self) -> None:
        if self._closed:
            return
        self._stream.read_bytes_async(
            max(self._want - len(self._buf), 1),
            GLib.PRIORITY_DEFAULT, None, self._on_read, None)

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
            if self._need_header:
                (length,) = struct.unpack(">I", chunk)
                if length == 0 or length > self.MAX_FRAME:
                    self.close()
                    return
                self._want = length
                self._need_header = False
            else:
                self._emit(chunk)
                self._want = self.HEADER
                self._need_header = True
        self._read()

    def _emit(self, jpeg: bytes) -> None:
        try:
            loader = GdkPixbuf.PixbufLoader.new_with_type("jpeg")
            loader.write(jpeg)
            loader.close()
            pixbuf = loader.get_pixbuf()
        except GLib.Error:
            return      # a torn frame is not worth interrupting enrollment
        if pixbuf is not None:
            self._on_frame(Gdk.Texture.new_for_pixbuf(pixbuf))

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


class OnboardingWindow(Adw.Window):
    """The wizard. Owns one Enrollment session at a time."""

    def __init__(self, parent: Gtk.Window, daemon: Daemon,
                 identity: str = "default", on_done=None):
        super().__init__(transient_for=parent, modal=True,
                         title="Set up face unlock",
                         default_width=560, default_height=640)
        self.daemon = daemon
        self.identity = identity
        self.on_done = on_done

        self.enroll = Enrollment()
        self.preview: PreviewReader | None = None
        self.poses: list[str] = []
        self.pose_index = 0
        self.captured = 0
        self._progress_sub = 0
        self._running = False

        self.toasts = Adw.ToastOverlay()
        self.stack = Adw.ViewStack()
        self.stack.add_named(self._welcome_page(), "welcome")
        self.stack.add_named(self._camera_page(), "camera")
        self.stack.add_named(self._enroll_page(), "enroll")
        self.stack.add_named(self._test_page(), "test")
        self.stack.add_named(self._done_page(), "done")

        view = Adw.ToolbarView()
        self.header = Adw.HeaderBar(show_end_title_buttons=True)
        view.add_top_bar(self.header)
        view.set_content(self.stack)
        self.toasts.set_child(view)
        self.set_content(self.toasts)

        self.connect("close-request", self._on_close)

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
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14,
                      margin_start=24, margin_end=24,
                      margin_top=12, margin_bottom=24)

        self.pose_label = Gtk.Label(label="Look straight ahead")
        self.pose_label.add_css_class("title-1")
        box.append(self.pose_label)

        self.pose_counter = Gtk.Label(label="1 of 9")
        self.pose_counter.add_css_class("dim-label")
        box.append(self.pose_counter)

        frame = Gtk.Frame(halign=Gtk.Align.CENTER)
        frame.add_css_class("card")
        self.picture = Gtk.Picture(content_fit=Gtk.ContentFit.CONTAIN,
                                   width_request=320, height_request=240)
        self.picture.set_can_shrink(True)
        frame.set_child(self.picture)
        box.append(frame)

        self.preview_hint = Gtk.Label(label="Starting the camera...")
        self.preview_hint.add_css_class("dim-label")
        box.append(self.preview_hint)

        self.bar = Gtk.ProgressBar(show_text=False, fraction=0.0)
        box.append(self.bar)

        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8,
                           halign=Gtk.Align.CENTER)
        self.cancel_btn = Gtk.Button(label="Cancel")
        self.cancel_btn.connect("clicked", lambda _b: self._cancel_enrollment())
        self.retry_btn = Gtk.Button(label="Retry this pose")
        self.retry_btn.set_visible(False)
        self.retry_btn.connect("clicked", lambda _b: self._capture_pose())
        controls.append(self.cancel_btn)
        controls.append(self.retry_btn)
        box.append(controls)
        return box

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

    # ---- flow ----------------------------------------------------------
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
            row = Adw.ActionRow(title=str(key).replace("_", " "),
                                subtitle=str(value))
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
        try:
            self.poses = self.enroll.list_poses()
        except DaemonError:
            self.poses = []
        if not self.poses:
            self.poses = ["Look straight ahead"]

        try:
            fd = self.enroll.start(self.identity)
        except DaemonError as e:
            self._fail(f"Could not start enrollment: {e}")
            return

        self._progress_sub = self.enroll.subscribe_progress(self._on_progress)
        self.preview = PreviewReader(fd, self._on_frame, self._on_preview_eof)
        self.pose_index = 0
        self.captured = 0
        self._update_pose_labels()
        self._capture_pose()

    def _update_pose_labels(self) -> None:
        name = self.poses[min(self.pose_index, len(self.poses) - 1)]
        self.pose_label.set_text(name)
        self.pose_counter.set_text(f"{self.pose_index + 1} of {len(self.poses)}")

    def _capture_pose(self) -> None:
        if self._running:
            return
        self._running = True
        self.retry_btn.set_visible(False)
        self.preview_hint.set_text(HUMAN_STATUS[ST_ACQUIRING])
        self._update_pose_labels()
        self.enroll.enroll_pose_async(self.pose_index, self._on_pose_done)

    def _on_pose_done(self, status: str, progress: float,
                      error: str | None) -> None:
        self._running = False
        if error:
            self.preview_hint.set_text("Enrollment service error")
            self._fail(error)
            return
        if status == ST_CANCELLED:
            return
        if status == ST_FAILED:
            self.preview_hint.set_text(
                "No usable frames for that pose. Try more light, or move "
                "a little closer.")
            self.retry_btn.set_visible(True)
            return

        self.captured += 1
        self.bar.set_fraction(min(1.0, (self.pose_index + 1) / len(self.poses)))
        self.pose_index += 1
        if self.pose_index >= len(self.poses):
            self._finish()
        else:
            self._update_pose_labels()
            # A short beat so the user can read the next prompt before
            # the camera opens again.
            GLib.timeout_add(450, self._capture_next)

    def _capture_next(self) -> bool:
        self._capture_pose()
        return GLib.SOURCE_REMOVE

    def _finish(self) -> None:
        self.preview_hint.set_text("Saving...")
        self.enroll.finish_async(self._on_finished)

    def _on_finished(self, saved: int, error: str | None) -> None:
        self._close_preview()
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
        self._close_preview()
        self.enroll.cancel()
        self.close()

    def _fail(self, message: str) -> None:
        self._close_preview()
        self._toast(message)
        self.pose_label.set_text("Enrollment stopped")
        self.preview_hint.set_text(message)
        self.retry_btn.set_visible(False)
        self.cancel_btn.set_label("Close")

    # ---- preview -------------------------------------------------------
    def _on_frame(self, texture) -> None:
        self.picture.set_paintable(texture)
        if self.preview_hint.get_text().startswith("Starting"):
            self.preview_hint.set_text(HUMAN_STATUS[ST_ACQUIRING])

    def _on_preview_eof(self) -> None:
        self.picture.set_paintable(None)

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
            self.pose_counter.set_text(
                f"{int(pose_index) + 1} of {len(self.poses) or 9}")
        self.preview_hint.set_text(HUMAN_STATUS.get(status, status))
        if status == ST_GOOD:
            self.bar.set_fraction(
                max(self.bar.get_fraction(), min(1.0, float(progress))))

    # ---- teardown ------------------------------------------------------
    def _on_close(self, *_a) -> bool:
        # Closing the window must release the camera and the pipe, not
        # leave a session open in the daemon.
        self._close_preview()
        try:
            self.enroll.close()
        except Exception:
            pass
        return False
