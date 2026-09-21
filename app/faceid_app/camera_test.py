"""Camera Test window.

Shows the live, unprivileged preview stream the daemon opens for any
user (StartPreview). It is the "point the webcam at something and look"
check from the Overview and Cameras pages: no face is needed, nothing
is recognised, and nothing is written anywhere. Live guidance from the
vision worker ("move closer", "too dark"...) is shown as it happens.
"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from .onboarding import CircularPreview, PreviewReader, guidance_for  # noqa: E402


class CameraTestWindow(Adw.Window):
    def __init__(self, parent: Gtk.Window, daemon, camera_summary: str = ""):
        super().__init__(transient_for=parent, modal=False,
                         title="Camera test",
                         default_width=430, default_height=560)
        self.daemon = daemon
        self.reader: PreviewReader | None = None
        self._frames = 0
        self._timer = 0
        self._fps = 0.0

        atm = Adw.ToastOverlay()
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                          spacing=12, margin_top=16, margin_bottom=16,
                          margin_start=24, margin_end=24,
                          valign=Gtk.Align.CENTER)

        title = Gtk.Label(label="Camera test")
        title.add_css_class("title-1")
        content.append(title)

        self.summary = Gtk.Label(label=camera_summary, wrap=True,
                                 xalign=0.5)
        self.summary.add_css_class("dim-label")
        content.append(self.summary)

        self.picture = CircularPreview(size=280)
        content.append(self.picture)

        self.kind = Gtk.Label(label="")
        self.kind.add_css_class("scan-counter")
        content.append(self.kind)

        self.guidance = Gtk.Label(label="Starting the camera…", wrap=True,
                                  xalign=0.5)
        content.append(self.guidance)

        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL,
                           spacing=10, halign=Gtk.Align.CENTER)
        self.start_btn = Gtk.Button(label="Restart preview")
        self.start_btn.add_css_class("pill")
        self.start_btn.connect("clicked", lambda _b: self._start())
        self.close_btn = Gtk.Button(label="Close")
        self.close_btn.add_css_class("suggested-action")
        self.close_btn.add_css_class("pill")
        self.close_btn.connect("clicked", lambda _b: self.close())
        controls.append(self.start_btn)
        controls.append(self.close_btn)
        content.append(controls)
        atm.set_child(content)
        self.set_content(atm)

        self.connect("close-request", self._on_close)
        self._start()

    def _start(self) -> None:
        self._stop_reader()
        self.guidance.set_text("Starting the camera…")
        self.start_btn.set_sensitive(False)
        try:
            fd = self.daemon.start_preview()
        except Exception as e:
            self.guidance.set_text(f"The preview could not be started.\n{e}")
            self.start_btn.set_sensitive(True)
            return
        self.reader = PreviewReader(fd, self._on_frame,
                                    self._on_guidance, self._on_eof)
        self._frames = 0
        self._fps = 0.0
        if self._timer:
            GLib.source_remove(self._timer)
        self._timer = GLib.timeout_add(1000, self._tick_fps)
        self.start_btn.set_sensitive(True)

    def _tick_fps(self) -> bool:
        self._fps = float(self._frames)
        self._frames = 0
        self.kind.set_text(f"{self._fps:.0f} frames/second" if self._fps >= 1
                           else "no frames yet")
        return GLib.SOURCE_CONTINUE

    def _on_frame(self, texture) -> None:
        self._frames += 1
        self.picture.set_texture(texture)

    def _on_guidance(self, status: str, reason: str) -> None:
        self.guidance.set_text(guidance_for(status, reason))

    def _on_eof(self) -> None:
        self.guidance.set_text(
            "The preview ended (the service may be busy). Restart to "
            "try again.")

    def _stop_reader(self) -> None:
        if self.reader is not None:
            self.reader.close()
            self.reader = None
        if self._timer:
            GLib.source_remove(self._timer)
            self._timer = 0

    def _on_close(self, *_a) -> bool:
        self._stop_reader()
        self.daemon.stop_preview()
        return False