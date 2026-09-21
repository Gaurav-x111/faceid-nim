#!/usr/bin/env python3
"""faceid-nim settings app (GTK4 + libadwaita).

Deliberately not required at runtime: the daemon does the unlocking.
This app enrolls, manages identities, changes settings and runs
diagnostics. Closing it changes nothing about how the machine
authenticates.

The layout follows Adwaita conventions -- sidebar + boxed lists +
Adw.Clamp -- and deliberately does not imitate Glance. Glance is a macOS
menu-bar app; a Linux settings app that copies macOS chrome looks wrong
next to GNOME Settings. The only custom CSS loaded is the three things
Adwaita has no widget for: the hero card, the status dots and the
rounded camera preview frame.

Pages:
  Overview    -- hero on/off card + enrolled faces + enroll / test
  Settings    -- strictness with a live explanation banner, camera
                 source, scan timeout, attention
  Opening     -- pick the lock-screen opening animation, make your own,
                 install community packages, share yours on GitHub
  Security    -- the honest limitations, plus delete-everything
  Diagnostics -- daemon state + copyable CLI commands
"""
from __future__ import annotations

import logging
import logging.handlers
import math
import os
import re
import sys
from threading import Thread

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from . import camera_discovery, openings, prefs  # noqa: E402
from .camera_test import CameraTestWindow  # noqa: E402
from .dbus_client import Daemon, DaemonError  # noqa: E402
from .onboarding import OnboardingWindow  # noqa: E402

APP_ID = "org.faceidnim.App"

# User-facing camera choices (stored in the app's own prefs, resolved
# against actual discovered hardware before touching the daemon).
CAMERA_MODE_LABELS = ["Automatic", "Normal camera (RGB)", "IR camera"]
CAMERA_MODE_VALUES = ["auto", "rgb", "ir"]


def _rgba_tuple(text: str) -> tuple:
    """'#rrggbb' / 'rgba(...)' -> (r, g, b, a) floats, dark fallback."""
    c = Gdk.RGBA()
    if c.parse(str(text or "")):
        return (c.red, c.green, c.blue, c.alpha)
    return (0.082, 0.11, 0.15, 1.0)


def _rgba_hex(rgba) -> str:
    def ch(v: float) -> str:
        return f"{max(0, min(255, int(round(v * 255)))):02x}"

    return f"#{ch(rgba.red)}{ch(rgba.green)}{ch(rgba.blue)}"


def _rgba_parse(text: str) -> Gdk.RGBA:
    c = Gdk.RGBA()
    c.parse(str(text or ""))
    return c


def _field_row(title: str, widget, subtitle: str = "") -> Gtk.Box:
    """A compact labelled row for use inside dialogs (not PreferencesPage)."""
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
    row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
    lab = Gtk.Label(label=title, xalign=0, hexpand=True,
                    width_request=130)
    lab.add_css_class("opening-field")
    row.append(lab)
    row.append(widget)
    box.append(row)
    if subtitle:
        sub = Gtk.Label(label=subtitle, xalign=0)
        sub.add_css_class("dim-label")
        box.append(sub)
    return box

LOG_DIR = os.path.join(
    os.environ.get("XDG_STATE_HOME") or os.path.join(
        os.path.expanduser("~"), ".local", "state"),
    "faceid-nim")
LOG_FILE = os.path.join(LOG_DIR, "faceid-nim.log")


def setup_logging(debug: bool = False) -> None:
    """Log to ~/.local/state/faceid-nim/faceid-nim.log (and console in
    debug mode). Never requires an existing directory or root."""
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            LOG_FILE, maxBytes=512 * 1024, backupCount=2,
            encoding="utf-8")
    except OSError:
        handler = logging.NullHandler()
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s")
    handler.setFormatter(formatter)
    root = logging.getLogger("faceid_app")
    root.setLevel(logging.DEBUG if debug else logging.INFO)
    root.addHandler(handler)
    if debug:
        cons = logging.StreamHandler()
        cons.setFormatter(formatter)
        root.addHandler(cons)
    root.info("app starting (pid %s)", os.getpid())

STRICTNESS_NOTES = {
    "off": ("Off: faces are never used to refuse access. Enrolled "
            "templates are still stored, but nothing is ever gatekept "
            "by them."),
    "light": ("Light: a single matching template unlocks right away. "
              "Fast, and the default -- most convenient, least strict."),
    "heavy": ("Heavy: positive evidence of a live 3D face (a blink or "
              "head motion) is required before unlocking. Harder to "
              "fool with a photo or video replay, at the cost of a "
              "slower unlock."),
}

CLI_COMMANDS = [
    ("Service status", "faceid-nim status"),
    ("Fetch / verify models", "sudo faceid-nim fetch-models"),
    ("Re-check installed models", "sudo faceid-nim verify-models"),
    ("Watch daemon + worker logs", "journalctl -fu faceid-nimd -u faceid-vision"),
    ("Live D-Bus signal trace", "gdbus monitor --system --dest org.faceidnim.Daemon1"),
]

def run_diagnose() -> int:
    """Terminal cameras+service report, no GUI needed (--diagnose)."""
    print("faceid-nim diagnostics")
    print("=" * 60)
    try:
        daemon = Daemon()
        try:
            diag = daemon.diagnostics()
            print(f"daemon diagnostics: {diag}")
        except DaemonError as e:
            print(f"daemon: UNREACHABLE — {e}")
        try:
            settings = daemon.get_settings()
            print(f"daemon camera config: "
                  f"mode={settings.get('mode')} camera={settings.get('camera')} "
                  f"ir_camera={settings.get('ir_camera')}")
        except DaemonError:
            pass
    except DaemonError as e:
        print(f"system bus: {e}")

    print("\ncameras:")
    for c in camera_discovery.discover():
        flag = "BUSY" if c.busy else ("OK" if c.readable else "NOACCESS")
        kinds = ", ".join(f"{f.fivecc.strip()}({f.description})"
                          for f in c.formats[:6])
        w, h = c.max_resolution
        res = f"{w}x{h}" if w else "?"
        fps = f"{c.fps_estimate:.0f}fps" if c.fps_estimate > 0 else "-"
        print(f"  {c.path:<12} [{flag:<8}] kind={c.kind:<5} "
              f"res={res:<9} {fps:<5} {c.card.strip()}")
        if kinds:
            print(f"             formats: {kinds}")
        if c.probe_error:
            print(f"             note: {c.probe_error}")
    return 0


PAGES = [
    ("overview", "Overview", "avatar-default-symbolic"),
    ("settings", "Settings", "preferences-system-symbolic"),
    ("opening", "Opening", "applications-multimedia-symbolic"),
    ("cameras", "Cameras", "camera-web-symbolic"),
    ("security", "Security", "dialog-password-symbolic"),
    ("diagnostics", "Diagnostics", "utilities-system-monitor-symbolic"),
]


def _load_css() -> None:
    """Load style.css from beside this module (dev tree and installed
    layout keep the same relative path). Non-fatal if unavailable."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "style.css")
    if not os.path.exists(path):
        return
    provider = Gtk.CssProvider()
    provider.load_from_path(path)
    display = Gdk.Display.get_default()
    if display is not None:
        Gtk.StyleContext.add_provider_for_display(
            display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)


class Window(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application, daemon: Daemon):
        super().__init__(application=app, title="Face Unlock",
                         default_width=860, default_height=620)
        self.daemon = daemon
        self.toasts = Adw.ToastOverlay()
        log = logging.getLogger("faceid_app")

        self._settings_cache: dict = {}
        self._id_rows: list = []
        self._diag_rows: list = []
        self._cam_rows: list = []
        self._opening_rows: list = []
        self._suspend_changes = False
        self._refresh_generation = 0
        self._prefs = prefs.load()
        self._openings = openings.load()
        self._editor_logo_path = ""
        self._plan = None
        self._discovery: list = []

        self._build()
        self.refresh()
        log.info("settings window ready (camera_mode=%s)", self._prefs["camera_mode"])

    # ---- chrome -------------------------------------------------------
    def _build(self) -> None:
        root = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL,
                       hexpand=True, vexpand=True)
        root.append(self._sidebar())

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                          hexpand=True, vexpand=True)
        self._title = Gtk.Label(label="Overview")
        self._title.add_css_class("title")
        header = Adw.HeaderBar(title_widget=self._title)
        content.append(header)

        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.stack.add_named(self._overview_page(), "overview")
        self.stack.add_named(self._settings_page(), "settings")
        self.stack.add_named(self._opening_page(), "opening")
        self.stack.add_named(self._cameras_page(), "cameras")
        self.stack.add_named(self._security_page(), "security")
        self.stack.add_named(self._diagnostics_page(), "diagnostics")
        self.stack.set_hexpand(True)
        self.stack.set_vexpand(True)
        # Do not rely on Gtk.Stack's implicit first-child behaviour.  It
        # differs across GTK 4 point releases and can present an empty stack.
        self.stack.set_visible_child_name("overview")
        content.append(self.stack)

        root.append(content)
        self.toasts.set_child(root)
        self.set_content(self.toasts)
        # Selecting a row emits row-selected.  It must happen only after the
        # stack and title label above exist, otherwise app startup can leave a
        # live but empty window behind.
        self.nav_list.select_row(self.nav_list.get_row_at_index(0))

    def _sidebar(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                      width_request=236)
        box.add_css_class("navigation-sidebar")

        brand = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2,
                        margin_start=16, margin_end=16, margin_top=20,
                        margin_bottom=14)
        icon = Gtk.Image(icon_name="org.faceidnim.App", pixel_size=44)
        title = Gtk.Label(label="Face Unlock")
        title.add_css_class("title-1")
        sub = Gtk.Label(label="faceid-nim")
        sub.add_css_class("dim-label")
        brand.append(icon)
        brand.append(title)
        brand.append(sub)
        box.append(brand)

        self.nav_list = Gtk.ListBox()
        self.nav_list.add_css_class("navigation-sidebar")
        self.nav_list.add_css_class("navigation-sidebar-list")
        for name, label, icon_name in PAGES:
            row = Gtk.ListBoxRow()
            item = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10,
                           margin_start=12, margin_end=12,
                           margin_top=8, margin_bottom=8)
            item.append(Gtk.Image(icon_name=icon_name, pixel_size=18))
            item.append(Gtk.Label(label=label, xalign=0))
            row.set_child(item)
            row.page = name                  # plain attribute; set_data is unsupported
            row.page_title = label
            self.nav_list.append(row)
        self.nav_list.connect("row-selected", self._on_nav)
        box.append(self.nav_list)

        box.append(Gtk.Box(vexpand=True))

        credit = Gtk.Label(
            label="Created by zang aka Gaurav-x111",
            halign=Gtk.Align.CENTER,
            margin_start=14, margin_end=14, margin_top=4)
        credit.add_css_class("dim-label")
        box.append(credit)
        v = GLib.getenv("VERSION") or ""
        foot = Gtk.Label(label=f"faceid-nim{v}",
                         halign=Gtk.Align.CENTER,
                         margin_top=2, margin_bottom=10)
        foot.add_css_class("dim-label")
        box.append(foot)
        return box

    def _on_nav(self, listbox, row) -> None:
        if not row:
            return
        page = getattr(row, "page", "overview")
        self.stack.set_visible_child_name(page)
        # Gtk4 removed Gtk.Container.get_children(); keeping metadata on the
        # row is simpler and works on every supported PyGObject version.
        self._title.set_text(getattr(row, "page_title", "Overview"))

    # ---- pages --------------------------------------------------------
    def _overview_page(self) -> Gtk.Widget:
        page = Adw.PreferencesPage()
        # PreferencesPage.add() only accepts PreferencesGroup on this
        # libadwaita; the hero is a plain widget, so park it inside a
        # titleless group.
        hero_group = Adw.PreferencesGroup()
        hero_group.add(self._hero())
        page.add(hero_group)

        self.id_group = Adw.PreferencesGroup(
            title="Enrolled faces",
            description="Add several identities for the conditions you "
                        "actually use: glasses on and off, a dim room, a "
                        "beard. Matching takes the best of all enabled "
                        "identities.")
        page.add(self.id_group)

        actions = Adw.PreferencesGroup(title="Try it out")
        row = Adw.ActionRow(title="Add a new identity",
                            subtitle="Nine poses, about 20 seconds")
        btn = Gtk.Button(label="Enroll", valign=Gtk.Align.CENTER)
        btn.add_css_class("suggested-action")
        btn.connect("clicked", self.on_enroll)
        row.add_suffix(btn)
        actions.add(row)

        test = Adw.ActionRow(
            title="Test a scan",
            subtitle="Runs a real scan and plays the full unlock "
                     "animation — without unlocking anything")
        tbtn = Gtk.Button(label="Test", valign=Gtk.Align.CENTER)
        tbtn.connect("clicked", self.on_test)
        test.add_suffix(tbtn)
        actions.add(test)

        cam_test = Adw.ActionRow(
            title="Test the camera",
            subtitle="Live preview to check video, light and focus")
        cbtn = Gtk.Button(label="Open", valign=Gtk.Align.CENTER)
        cbtn.connect("clicked", self.on_camera_test)
        cam_test.add_suffix(cbtn)
        actions.add(cam_test)
        page.add(actions)
        return page

    def _hero(self) -> Gtk.Widget:
        hero = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        hero.add_css_class("hero-card")

        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        top.append(Gtk.Image(icon_name="org.faceidnim.App", pixel_size=56))
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        t = Gtk.Label(label="Face Unlock", xalign=0)
        t.add_css_class("hero-title")
        s = Gtk.Label(label="Scan your face to unlock this computer.",
                      xalign=0)
        s.add_css_class("hero-subtitle")
        texts.append(t)
        texts.append(s)
        top.append(texts)
        hero.append(top)

        status = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8,
                         margin_top=2)
        self.status_dot = Gtk.Label(label=" ", width_request=12,
                                    height_request=12)
        self.status_dot.add_css_class("status-dot")
        self.status_dot.add_css_class("warn")
        self.status_text = Gtk.Label(label="…", xalign=0)
        self.status_text.add_css_class("hero-status")
        status.append(self.status_dot)
        status.append(self.status_text)
        hero.append(status)

        self.cam_chip = Gtk.Label(label="", xalign=0, wrap=True)
        self.cam_chip.add_css_class("dim-label")
        self.cam_chip.add_css_class("hero-status")
        hero.append(self.cam_chip)

        toggle = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        toggle.add_css_class("hero-toggle")
        label = Gtk.Label(label="Enable face unlock", hexpand=True, xalign=0)
        toggle.append(label)
        self.sw_enabled = Gtk.Switch()
        self.sw_enabled.connect("notify::active", self.on_setting_changed)
        toggle.append(self.sw_enabled)
        hero.append(toggle)
        return hero

    def _settings_page(self) -> Gtk.Widget:
        page = Adw.PreferencesPage()

        g = Adw.PreferencesGroup(title="Face unlock")
        self.cb_strict = Adw.ComboRow(
            title="Strictness",
            subtitle="How much evidence an unlock needs",
            model=Gtk.StringList.new(["off", "light", "heavy"]))
        self.cb_strict.connect("notify::selected", self.on_setting_changed)
        g.add(self.cb_strict)

        note = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                       margin_top=2, margin_bottom=2)
        self.strict_note = Gtk.Label(label="", wrap=True, xalign=0)
        self.strict_note.add_css_class("explanation")
        self.strict_note.add_css_class("dim-label")
        note.append(self.strict_note)
        g.add(note)

        self.sp_timeout = Adw.SpinRow.new_with_range(500, 15000, 250)
        self.sp_timeout.set_title("Scan timeout (ms)")
        self.sp_timeout.set_subtitle(
            "Keep this short: the password box usually cannot be used "
            "until the face attempt finishes")
        self.sp_timeout.connect("notify::value", self.on_setting_changed)
        g.add(self.sp_timeout)
        page.add(g)

        cam = Adw.PreferencesGroup(
            title="Camera",
            description="The daemon scans with the camera shown here. "
                        "Changing mode or refreshing takes effect on the "
                        "next scan, without restarting anything.")
        self.cb_camera_mode = Adw.ComboRow(
            title="Camera mode",
            subtitle="Automatic picks the best camera it can find",
            model=Gtk.StringList.new(CAMERA_MODE_LABELS))
        self.cb_camera_mode.set_selected(
            CAMERA_MODE_VALUES.index(self._prefs.get("camera_mode", "auto")))
        self.cb_camera_mode.set_subtitle("Resolving…")
        self.cb_camera_mode.connect("notify::selected",
                                    self.on_camera_mode_changed)
        cam.add(self.cb_camera_mode)

        self.rgb_det_row = Adw.ActionRow(title="RGB video camera")
        self.rgb_det_row.add_suffix(Gtk.Label(label="—"))
        cam.add(self.rgb_det_row)
        self.ir_det_row = Adw.ActionRow(title="IR camera")
        self.ir_det_row.add_suffix(Gtk.Label(label="—"))
        cam.add(self.ir_det_row)

        refresh_row = Adw.ActionRow(
            title="Refresh cameras",
            subtitle="Re-enumerate /dev/video* and re-resolve the mode")
        rbtn = Gtk.Button(label="Refresh", valign=Gtk.Align.CENTER)
        rbtn.connect("clicked", lambda _b: self.refresh())
        refresh_row.add_suffix(rbtn)
        cam.add(refresh_row)

        advanced = Adw.PreferencesGroup(
            title="Manual overrides",
            description="Leave these empty to let detection choose. "
                        "Type a device path to pin that specific camera.")
        self.camera_entry = Adw.EntryRow(title="RGB device (optional)")
        if hasattr(self.camera_entry, "set_placeholder_text"):
            self.camera_entry.set_placeholder_text("/dev/video1")
        self._camera_timer = 0
        self.camera_entry.connect("notify::text", self._on_camera_edited)
        advanced.add(self.camera_entry)
        self.ir_entry = Adw.EntryRow(title="IR device (optional)")
        if hasattr(self.ir_entry, "set_placeholder_text"):
            self.ir_entry.set_placeholder_text("/dev/video3")
        self.ir_entry.connect("notify::text", self._on_camera_edited)
        advanced.add(self.ir_entry)

        att = Adw.SwitchRow(
            title="Require attention",
            subtitle="Behaviour for people browsing their phone while "
                     "it unlocks")
        self.sw_attention = att
        att.connect("notify::active", self.on_setting_changed)
        cam.add(att)
        page.add(cam)
        page.add(advanced)
        return page

    def _cameras_page(self) -> Gtk.Widget:
        page = Adw.PreferencesPage()
        self.cam_group = Adw.PreferencesGroup(
            title="Detected cameras",
            description="Every /dev/videoN this user can at least open, "
                        "classified by what the device actually reports. "
                        "'IR' means its only formats are monochrome; the "
                        "app never labels a camera IR on suspicion alone.")
        page.add(self.cam_group)

        actions = Adw.PreferencesGroup()
        row = Adw.ActionRow(
            title="Rescan devices",
            subtitle="Re-enumerate after plugging in or unplugging a camera")
        b = Gtk.Button(label="Refresh", valign=Gtk.Align.CENTER)
        b.connect("clicked", lambda _b: self.refresh())
        row.add_suffix(b)
        actions.add(row)
        page.add(actions)
        return page

    def _security_page(self) -> Gtk.Widget:
        page = Adw.PreferencesPage()
        grp = Adw.PreferencesGroup(title="What Face Unlock is not")
        grp.add(Adw.ActionRow(
            title="This is a convenience feature, not Face ID",
            subtitle="On an ordinary RGB webcam a good video replay can "
                     "defeat it. An IR camera is substantially stronger."))
        grp.add(Adw.ActionRow(
            title="Photos and videos can trick it",
            subtitle="Heavy strictness detects a blink or head motion, "
                     "but nothing here is a medical-grade liveness detector."))
        grp.add(Adw.ActionRow(
            title="Twins and lookalikes raise false accepts",
            subtitle="If someone could get in with your password, treat "
                     "this as no stronger than they are."))
        grp.add(Adw.ActionRow(
            title="Your password always works",
            subtitle="Face unlock never replaces or weakens your "
                     "password. It only adds a convenience path."))
        grp.add(Adw.ActionRow(
            title="Nothing leaves the machine",
            subtitle="Only numeric templates are stored, encrypted, and "
                     "readable by root alone. No photographs are kept."))
        page.add(grp)

        danger = Adw.PreferencesGroup(title="Data")
        drow = Adw.ActionRow(
            title="Remove all face data",
            subtitle="Deletes every encrypted template for your account "
                     "immediately")
        dbtn = Gtk.Button(label="Delete", valign=Gtk.Align.CENTER)
        dbtn.add_css_class("destructive-action")
        dbtn.connect("clicked", self.on_delete_all)
        drow.add_suffix(dbtn)
        danger.add(drow)
        page.add(danger)
        return page

    def _diagnostics_page(self) -> Gtk.Widget:
        page = Adw.PreferencesPage()
        self.diag_group = Adw.PreferencesGroup(title="Service state")
        page.add(self.diag_group)
        g = Adw.PreferencesGroup()
        row = Adw.ActionRow(title="Re-run checks")
        b = Gtk.Button(label="Refresh", valign=Gtk.Align.CENTER)
        b.connect("clicked", lambda _b: self.refresh())
        row.add_suffix(b)
        g.add(row)
        page.add(g)

        c = Adw.PreferencesGroup(
            title="Useful commands",
            description="Copy any line into a terminal. Diagnostics "
                        "calls run as your user; model and log commands "
                        "prefixed with sudo need an admin.")
        for title, cmd in CLI_COMMANDS:
            r = Adw.ActionRow(title=title)
            r.set_subtitle(cmd)
            lab = Gtk.Label(label=cmd, hexpand=True)
            lab.add_css_class("cli-command")
            lab.add_css_class("dim-label")
            r.add_suffix(lab)
            cop = Gtk.Button(icon_name="edit-copy-symbolic",
                             valign=Gtk.Align.CENTER)
            cop.add_css_class("flat")
            cop.connect("clicked", lambda _b, t=cmd: self._copy(t))
            r.add_suffix(cop)
            c.add(r)
        page.add(c)
        return page

    # ---- behaviour -----------------------------------------------------
    def toast(self, text: str) -> None:
        self.toasts.add_toast(Adw.Toast(title=text))

    def _copy(self, text: str) -> None:
        display = Gdk.Display.get_default()
        if display is not None:
            display.get_clipboard().set(text)
        self.toast("Copied to clipboard")

    def _clear(self, group: Adw.PreferencesGroup, rows: list) -> None:
        for r in rows:
            group.remove(r)
        rows.clear()

    def _set_status(self, level: str, text: str) -> None:
        for cls in ("ok", "warn", "bad"):
            self.status_dot.remove_css_class(cls)
        self.status_dot.add_css_class(level)
        self.status_text.set_text(text)

    def _update_strict_note(self, strictness: str) -> None:
        note = STRICTNESS_NOTES.get(strictness, "")
        self.strict_note.set_text(
            f"{strictness.capitalize()}: {note}" if note else "")
        self.sw_attention.set_subtitle(
            "Forced in Heavy, which already requires a live-dimension "
            "test. You can require it in Light too." if strictness == "heavy" else
            "A blink or head motion before the match succeeds")

    def refresh(self) -> None:
        """Refresh daemon-backed values without ever blocking GTK's loop.

        The system bus is allowed to be slow while services are starting, or
        absent entirely on a freshly installed machine.  Calling its 60s
        synchronous methods from Window.__init__ was why the application
        looked like a blank, spinning window before it ever painted.
        """
        self._refresh_generation += 1
        generation = self._refresh_generation
        self._set_status("warn", "Checking the face unlock service…")
        Thread(target=self._load_refresh, args=(generation,), daemon=True).start()

    def _load_refresh(self, generation: int) -> None:
        # Do not share a GDBusProxy between the worker and GTK threads.  A
        # short-lived client also keeps a stale daemon connection from making
        # the settings window unresponsive forever.
        daemon = Daemon()
        errors: list[str] = []
        try:
            ids = daemon.list_identities()
        except DaemonError as e:
            ids = []
            errors.append(str(e))

        try:
            settings = daemon.get_settings()
        except DaemonError as e:
            settings = {}
            errors.append(str(e))

        try:
            diagnostics = daemon.diagnostics()
        except DaemonError as e:
            diagnostics = {}
            errors.append(str(e))

        # Camera discovery is stdlib-only and fast; do it in this worker
        # thread so slow /dev/video* behaviour can never stall the UI.
        try:
            cams = camera_discovery.discover()
        except OSError:
            cams = []
        plan = camera_discovery.resolve_plan(prefs.load(), cams)

        GLib.idle_add(self._apply_refresh, generation, ids, settings,
                      diagnostics, cams, plan, errors)

    def _apply_refresh(self, generation: int, ids: list, s: dict,
                       diag: dict, cams: list, plan: dict,
                       errors: list[str]) -> bool:
        # A newer click on Refresh won the race; its data is the only state
        # that may update the page.
        if generation != self._refresh_generation:
            return GLib.SOURCE_REMOVE

        self._discovery = cams
        self._plan = plan
        self._prefs = prefs.load()

        self._clear(self.id_group, self._id_rows)
        self._clear(self.diag_group, self._diag_rows)
        self._clear(self.cam_group, self._cam_rows)

        for name, enabled, model_id in ids:
            row = Adw.SwitchRow(title=name, subtitle=f"model {model_id}",
                                active=enabled)
            row.connect("notify::active",
                        lambda r, _p, n=name: self._toggle(n, r.get_active()))
            d = Gtk.Button(icon_name="user-trash-symbolic",
                           valign=Gtk.Align.CENTER)
            d.add_css_class("flat")
            d.connect("clicked", lambda _b, n=name: self._delete(n))
            row.add_suffix(d)
            self.id_group.add(row)
            self._id_rows.append(row)
        if not ids:
            r = Adw.ActionRow(title="No faces enrolled",
                              subtitle="Face unlock stays off until you add one")
            self.id_group.add(r)
            self._id_rows.append(r)

        self._settings_cache = s
        self._suspend_changes = True
        self.sw_enabled.set_active(bool(s.get("enabled")))
        strict = str(s.get("strictness", "light"))
        if strict in STRICTNESS_NOTES:
            ind = ["off", "light", "heavy"].index(strict)
            self.cb_strict.set_selected(ind)
        self._update_strict_note(strict)
        self.sp_timeout.set_value(float(s.get("scan_timeout_ms", 4000)))
        self.sw_attention.set_active(bool(s.get("require_attention", True)))

        # Camera block: selector reflects user intent (prefs), while the
        # detection rows and the daemon config reflect the resolved plan.
        mode_pref = self._prefs.get("camera_mode", "auto")
        self.cb_camera_mode.set_selected(
            CAMERA_MODE_VALUES.index(mode_pref)
            if mode_pref in CAMERA_MODE_VALUES else 0)
        self._set_camera_field(self.camera_entry,
                               self._prefs.get("rgb_dev", ""))
        self._set_camera_field(self.ir_entry,
                               self._prefs.get("ir_dev", ""))
        self._suspend_changes = False

        self._update_camera_ui(plan)

        # Push the resolved mode/devices to the daemon whenever the live
        # discovery disagrees with what it currently has configured. Only
        # do this while the worker is healthy: a degraded discovery plan
        # computed during a worker outage (e.g. a failed /dev/video probe)
        # must never overwrite the daemon's working config.
        if bool(diag.get("worker_reachable")) and \
           (s.get("mode") != plan["mode"] or \
            str(s.get("camera") or "") != str(plan["camera"] or "") or \
            str(s.get("ir_camera") or "") != str(plan["ir_camera"] or "")):
            self._push_plan(plan)

        worker_up = bool(diag.get("worker_reachable"))
        if not s.get("enabled"):
            self._set_status("warn", "Face unlock is off")
        elif not ids:
            self._set_status("warn", "Enabled but no faces enrolled")
        elif not worker_up:
            self._set_status("bad", "Worker is not responding — check the logs")
        else:
            self._set_status("ok",
                             f"{len(ids)} face{'s' if len(ids) != 1 else ''} "
                             "enrolled — ready")

        for k, v in diag.items():
            r = Adw.ActionRow(title=str(k).replace("_", " "),
                              subtitle=self._diag_value(v))
            self.diag_group.add(r)
            self._diag_rows.append(r)
        if not diag:
            r = Adw.ActionRow(title="Cannot reach the daemon",
                              subtitle="Run: faceid-nim status")
            self.diag_group.add(r)
            self._diag_rows.append(r)
        if errors:
            # One concise toast is enough; a down daemon must not make a
            # newly opened app look like it is throwing several errors.
            self.toast("Some service information could not be loaded")
        self._refresh_openings()
        return GLib.SOURCE_REMOVE

    # ---- opening animation UI ----------------------------------------
    def _opening_page(self) -> Gtk.Widget:
        page = Adw.PreferencesPage()

        sel = Adw.PreferencesGroup(
            title="Opening animation",
            description="Plays while the lock-screen pill grows at the "
                        "start of a scan. The choice applies at the very "
                        "next lock — no extension reload, no restart.")
        self.cb_opening = Adw.ComboRow(
            title="Active animation",
            subtitle="Pick how the lock screen begins",
            model=Gtk.StringList.new([]))
        self.cb_opening.connect("notify::selected", self.on_opening_selected)
        sel.add(self.cb_opening)

        prev = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10,
                       margin_top=4)
        self.preview_area = Gtk.DrawingArea(width_request=46,
                                            height_request=30)
        self.preview_area.set_draw_func(self._draw_opening_preview, None)
        prev.append(self.preview_area)
        self.preview_label = Gtk.Label(label="", xalign=0, hexpand=True,
                                       wrap=True)
        self.preview_label.add_css_class("dim-label")
        prev.append(self.preview_label)
        sel.add(prev)
        page.add(sel)

        faceg = Adw.PreferencesGroup(
            title="Scanner",
            description="Which face scanner draws inside the pill. Both "
                        "shapes share the same timing, spring and "
                        "Verified → Welcome sequence — this only picks "
                        "the look.")
        self.cb_scan_face = Adw.ComboRow(
            title="Scan face",
            subtitle="Arena (premium halo/crest) or the classic sweep",
            model=Gtk.StringList.new(["Arena (default)", "Classic"]))
        self.cb_scan_face.connect("notify::selected",
                                  self.on_scan_face_selected)
        faceg.add(self.cb_scan_face)
        page.add(faceg)

        pvgroup = Adw.PreferencesGroup(
            title="Preview",
            description="Watch the full Verified → Welcome sequence on the "
                        "lock pill right now. No camera and no "
                        "authentication — a developer preview only, so you "
                        "can tune the animation without standing at the "
                        "screen.")
        prow = Adw.ActionRow(
            title="Preview success animation",
            subtitle="Ring sweep, check, Verified, Welcome, identity")
        pb = Gtk.Button(label="Preview", valign=Gtk.Align.CENTER)
        pb.add_css_class("suggested-action")
        pb.connect("clicked", self._preview_success)
        prow.add_suffix(pb)
        pvgroup.add(prow)
        page.add(pvgroup)

        make = Adw.PreferencesGroup(title="Make your own")
        row = Adw.ActionRow(
            title="Create an opening animation",
            subtitle="Pick colours, a headline and an optional logo — "
                     "saved on this machine only")
        b = Gtk.Button(label="Create", valign=Gtk.Align.CENTER)
        b.add_css_class("suggested-action")
        b.connect("clicked", lambda _b: self._open_editor())
        row.add_suffix(b)
        make.add(row)
        page.add(make)

        get = Adw.PreferencesGroup(
            title="Get animations",
            description="Animation packages (.faceopen) are small zips "
                        "holding colours and one logo. They repaint the "
                        "pill — they can never run code on your machine.")
        frow = Adw.ActionRow(
            title="Install from file",
            subtitle="A .faceopen package you downloaded or were sent")
        fb = Gtk.Button(label="Choose…", valign=Gtk.Align.CENTER)
        fb.connect("clicked", self._pick_package)
        frow.add_suffix(fb)
        get.add(frow)

        urow = Adw.ActionRow(
            title="Install from URL",
            subtitle="Paste the raw link to a .faceopen package, e.g. one "
                     "shared on GitHub")
        ub = Gtk.Button(label="Add…", valign=Gtk.Align.CENTER)
        ub.connect("clicked", self._open_url_install)
        urow.add_suffix(ub)
        get.add(urow)

        brow = Adw.ActionRow(
            title="Browse community",
            subtitle="Animations people share on GitHub, listed from the "
                     "project catalogue")
        bb = Gtk.Button(label="Browse", valign=Gtk.Align.CENTER)
        bb.connect("clicked", self._browse_community)
        brow.add_suffix(bb)
        get.add(brow)
        page.add(get)

        self.opening_group = Adw.PreferencesGroup(
            title="Your animations",
            description="Edit, export for sharing, publish to GitHub, "
                        "or delete.")
        page.add(self.opening_group)
        return page

    @staticmethod
    def _opening_summary(s: dict) -> str:
        parts = [str(s.get("ease") or "outCubic"),
                 f"{int(s.get('fade_ms') or 420)}ms"]
        if s.get("logo"):
            parts.append("logo")
        if s.get("ring"):
            parts.append("halo")
        if s.get("text"):
            parts.append(f'“{s["text"]}”')
        return " · ".join(parts)

    def on_scan_face_selected(self, row, _param=None):
        face = "classic" if row.get_selected() == 1 else "arena"
        self._openings["scan_face"] = face
        openings.save(self._openings)
        logging.getLogger("faceid_app").info("scan face set to %s", face)

    def _refresh_openings(self) -> None:
        cfg = self._openings
        ids = openings.all_ids(cfg)

        self._suspend_changes = True
        self.cb_scan_face.set_selected(
            1 if cfg.get("scan_face", "arena") == "classic" else 0)
        names = []
        for vid in ids:
            s = openings.spec(cfg, vid) or {}
            parts = [s.get("name") or vid]
            if s.get("kind") == "builtin":
                parts.append("(built-in)")
            names.append(" ".join(parts))
        self.cb_opening.set_model(Gtk.StringList.new(names))
        active = cfg.get("active") or "logo"
        if active in ids:
            self.cb_opening.set_selected(ids.index(active))
        self._suspend_changes = False

        s = openings.spec(cfg, active) or {}
        desc = s.get("description") or ""
        if s.get("kind") == "custom":
            desc = f"{desc}  ·  {self._opening_summary(s)}" if desc \
                else self._opening_summary(s)
        self.preview_label.set_text(desc)
        self.preview_area.queue_draw()

        self._clear(self.opening_group, self._opening_rows)
        for vid in ids:
            if vid in openings.BUILTIN:
                continue
            s = openings.spec(cfg, vid) or {}
            row = Adw.ActionRow(title=s.get("name") or vid,
                                subtitle=self._opening_summary(s))
            for icon, tip, fn in (
                    ("preferences-system-symbolic", "Edit",
                     lambda b, v=vid: self._open_editor(v)),
                    ("document-send-symbolic", "Publish to GitHub",
                     lambda b, v=vid: self._open_share(v)),
                    ("user-trash-symbolic", "Delete",
                     lambda b, v=vid: self._delete_opening(v))):
                btn = Gtk.Button(icon_name=icon, tooltip_text=tip,
                                 valign=Gtk.Align.CENTER)
                btn.add_css_class("flat")
                btn.connect("clicked", fn)
                row.add_suffix(btn)
            self.opening_group.add(row)
            self._opening_rows.append(row)
        if not self._opening_rows:
            r = Adw.ActionRow(
                title="No own animations yet",
                subtitle="Create one above, or install a community package")
            self.opening_group.add(r)
            self._opening_rows.append(r)

    def _draw_opening_preview(self, area, cr, w, h, _data) -> None:
        s = openings.spec(self._openings,
                          self._openings.get("active") or "logo") or {}
        bg = _rgba_tuple(s.get("bg") or "rgba(14, 18, 26, 0.82)")
        accent = _rgba_tuple(s.get("accent") or "#a5d8ff")
        cr.scale(w, h)
        radius = 0.5
        cr.move_to(radius, 0.0)
        cr.line_to(1.0 - radius, 0.0)
        cr.arc(1.0 - radius, 0.5, radius, -math.pi / 2, math.pi / 2)
        cr.line_to(radius, 1.0)
        cr.arc(radius, 0.5, radius, math.pi / 2, 3 * math.pi / 2)
        cr.close_path()
        cr.set_source_rgba(*bg)
        cr.fill_preserve()
        cr.set_line_width(0.02)
        cr.set_source_rgba(*accent)
        cr.stroke()
        if s.get("ring"):
            cr.set_source_rgba(*accent)
            cr.arc(0.36, 0.5, 0.22, 0, 2 * math.pi)
            cr.set_line_width(0.03)
            cr.stroke()
        else:
            cr.set_source_rgba(*accent)
            cr.arc(0.36, 0.5, 0.12, 0, 2 * math.pi)
            cr.fill()

    def on_opening_selected(self, *_a) -> None:
        if self._suspend_changes:
            return
        idx = self.cb_opening.get_selected()
        ids = openings.all_ids(self._openings)
        if 0 <= idx < len(ids):
            vid = ids[idx]
            if openings.set_active(self._openings, vid):
                self._refresh_openings()
                s = openings.spec(self._openings, vid) or {}
                self.toast(f"Opening animation: {s.get('name') or vid} "
                           "(next lock)")

    def _preview_success(self, _b=None) -> None:
        """Developer-only: ask the daemon to play the full success
        sequence on the lock pill (ring, check, Verified, Welcome,
        identity). The daemon only emits ScanState -- it can never
        unlock anything or touch a decision path."""

        def run():
            identity = ""
            try:
                for name, enabled, _model in self.daemon.list_identities():
                    if enabled:
                        identity = name
                        break
                self.daemon.preview_animation(identity)
                return None
            except Exception:
                return "unavailable"

        def then(res):
            if res is None:
                self.toast("Success animation previewing now")
            else:
                self.toast("Preview unavailable — is the daemon up to date?")

        self._thread(run, then)

    def _thread(self, fn, then) -> None:
        """Run blocking work off the UI thread, then apply on the main one."""
        def run():
            try:
                result = fn()
            except Exception as e:          # never crash GTK from a thread
                result = e
            GLib.idle_add(lambda: then(result))
        Thread(target=run, daemon=True).start()

    # -- create / edit -------------------------------------------------
    def _open_editor(self, variant_id: str | None = None) -> None:
        cfg = self._openings
        existing = openings.spec(cfg, variant_id) if variant_id else None
        self._editor_logo_path = ""
        if existing and existing.get("logo"):
            self._editor_logo_path = os.path.join(
                openings.variant_dir(variant_id), existing["logo"])

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                      width_request=440)

        name = Gtk.Entry()
        name.set_placeholder_text("e.g. Sunset Pop")
        if existing:
            name.set_text(existing["name"])
        box.append(_field_row("Name", name,
                              "Shown in lists and used as the package id."))

        text = Gtk.Entry()
        text.set_placeholder_text("Opening camera… (blank uses the default)")
        if existing and existing.get("text"):
            text.set_text(existing["text"])
        box.append(_field_row("Headline", text,
                              "Words shown while the pill opens."))

        logo_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._logo_btn_label = Gtk.Label(label="None", xalign=0, hexpand=True)
        self._logo_btn_label.add_css_class("dim-label")
        pick = Gtk.Button(label="Choose…")
        pick.connect("clicked", self._choose_logo)
        logo_row.append(self._logo_btn_label)
        logo_row.append(pick)
        box.append(_field_row(
            "Logo", logo_row,
            "Optional SVG or PNG shown beside the headline."))
        if self._editor_logo_path:
            self._logo_btn_label.set_text(
                os.path.basename(self._editor_logo_path))

        chosen = {"bg": existing.get("bg") if existing else "",
                  "accent": existing.get("accent") if existing else ""}

        def color_row(key: str) -> Gtk.Box:
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            btn = Gtk.ColorButton()
            c = Gdk.RGBA()
            c.parse(chosen[key] or "#2a2a2a")
            btn.set_rgba(c)
            clear = Gtk.Button(icon_name="edit-clear-symbolic")
            clear.add_css_class("flat")
            clear.add_css_class("circular")
            btn.connect("color-set",
                        lambda _b, k=key: chosen.__setitem__(
                            k, _rgba_hex(btn.get_rgba())))
            clear.connect("clicked", lambda _b, k=key: (
                chosen.__setitem__(k, ""),
                btn.set_rgba(_rgba_parse("#2a2a2a"))))
            row.append(btn)
            row.append(clear)
            return row

        box.append(_field_row(
            "Background", color_row("bg"),
            "Fill colour while opening. Clear = keep the dark pill."))
        box.append(_field_row(
            "Accent", color_row("accent"),
            "Halo, text and border tint while opening."))

        ease = Gtk.DropDown()
        ease.set_model(Gtk.StringList.new(openings.EASINGS))
        ease.set_selected(openings.EASINGS.index(existing["ease"])
                          if existing and existing.get("ease")
                          in openings.EASINGS else 0)
        box.append(_field_row("Easing", ease,
                              "How the splash fades and pops."))

        spin = Gtk.SpinButton.new_with_range(200, 3000, 50)
        spin.set_value(int(existing.get("fade_ms", 420)) if existing else 420)
        box.append(_field_row("Duration (ms)", spin))

        ring = Gtk.Switch()
        ring.set_active(bool(existing and existing.get("ring")))
        box.append(_field_row(
            "Halo instead of logo", ring,
            "A soft coloured glow, no image. Disables the logo."))

        scale = Gtk.Switch()
        scale.set_active(bool(existing.get("scale") if existing else True))
        box.append(_field_row(
            "Logo pop-in", scale,
            "Logo springs in from 85% while it fades."))
        if ring.get_active():
            scale.set_sensitive(False)
        ring.connect("notify::active",
                     lambda _w, _p: scale.set_sensitive(not ring.get_active()))

        scroll = Gtk.ScrolledWindow()
        scroll.set_max_content_height(560)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_child(box)

        dlg = Adw.AlertDialog(
            heading="Create an opening animation" if not existing
                    else f"Edit “{existing['name']}”",
            body="Colours, a headline and one optional logo. Your creation "
                 "lives on this machine only.",
            extra_child=scroll)
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("save", "Save")
        dlg.set_default_response("save")
        dlg.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)

        def on_response(_d, resp: str) -> None:
            if resp != "save":
                return
            na = name.get_text().strip() or "Opening"
            tx = text.get_text().strip()
            try:
                if existing:
                    openings.update_variant(
                        cfg, variant_id, name=na, text=tx,
                        bg=chosen["bg"], accent=chosen["accent"],
                        ease=openings.EASINGS[ease.get_selected()],
                        fade_ms=int(spin.get_value()),
                        scale=scale.get_active(), ring=ring.get_active(),
                        logo_path=self._editor_logo_path or None)
                    self.toast("Opening animation updated")
                else:
                    vid = openings.create_variant(
                        cfg, name=na, logo_path=self._editor_logo_path or None,
                        text=tx, bg=chosen["bg"], accent=chosen["accent"],
                        ease=openings.EASINGS[ease.get_selected()],
                        fade_ms=int(spin.get_value()),
                        scale=scale.get_active(), ring=ring.get_active())
                    self.toast(f"Created “{na}” — select it below")
                self._refresh_openings()
            except ValueError as e:
                self.toast(str(e))
                return

        dlg.connect("response", on_response)
        dlg.present(self)

    def _choose_logo(self, _btn) -> None:
        chooser = Gtk.FileChooserNative.new(
            "Choose a logo", self, Gtk.FileChooserAction.OPEN,
            "Select", "Cancel")
        filt = Gtk.FileFilter()
        filt.set_name("Images (SVG, PNG)")
        filt.add_mime_type("image/svg+xml")
        filt.add_mime_type("image/png")
        chooser.add_filter(filt)
        chooser.connect("response", self._on_logo_picked)
        self._chooser = chooser
        chooser.show()

    def _on_logo_picked(self, chooser, resp) -> None:
        if resp == Gtk.ResponseType.ACCEPT:
            f = chooser.get_file()
            if f:
                self._editor_logo_path = f.get_path()
                self._logo_btn_label.set_text(
                    os.path.basename(self._editor_logo_path))

    # -- install -------------------------------------------------------
    def _pick_package(self, _b=None) -> None:
        chooser = Gtk.FileChooserNative.new(
            "Choose an opening package", self, Gtk.FileChooserAction.OPEN,
            "Install", "Cancel")
        filt = Gtk.FileFilter()
        filt.set_name("faceid-nim packages (*.faceopen)")
        filt.add_pattern("*.faceopen")
        filt.add_mime_type("application/zip")
        chooser.add_filter(filt)
        chooser.connect("response", self._on_package_picked)
        self._chooser = chooser
        chooser.show()

    def _on_package_picked(self, chooser, resp) -> None:
        if resp != Gtk.ResponseType.ACCEPT:
            return
        f = chooser.get_file()
        if not f:
            return
        try:
            vid = openings.install_package(f.get_path())
        except ValueError as e:
            self.toast(str(e))
            return
        self._openings = openings.load()
        openings.set_active(self._openings, vid)
        self._refresh_openings()
        self.toast(f"Installed {vid} — now active")

    def _open_url_install(self, _b=None) -> None:
        entry = Gtk.Entry()
        entry.set_placeholder_text(
            "https://…/animation.faceopen")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4,
                      width_request=430)
        box.append(entry)
        dlg = Adw.AlertDialog(
            heading="Install from URL",
            body="A raw link to a .faceopen package. GitHub raw links "
                 "(raw.githubusercontent.com/…) work best.",
            extra_child=box)
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("install", "Install")
        dlg.set_default_response("install")
        dlg.set_response_appearance("install", Adw.ResponseAppearance.SUGGESTED)

        def on_response(_d, resp: str) -> None:
            if resp == "install":
                u = entry.get_text().strip()
                if u:
                    self._install_url(u, u.rsplit("/", 1)[-1])

        dlg.connect("response", on_response)
        dlg.present(self)

    def _browse_community(self, _b=None) -> None:
        self.toast("Fetching community animations…")
        self._thread(lambda: openings.fetch_catalog(), self._show_catalog)

    def _show_catalog(self, result) -> None:
        if isinstance(result, Exception):
            self.toast(f"Cannot reach the catalogue: {result}")
            return
        if not result:
            self.toast("No community animations found — the catalogue may "
                       "be empty or you are offline")
            return
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8,
                      width_request=460)
        for e in result:
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            txt = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1,
                          hexpand=True)
            nm = Gtk.Label(label=e["name"], xalign=0)
            nm.add_css_class("title-4")
            ds = Gtk.Label(label=e["description"] or "Opening animation",
                           xalign=0, wrap=True)
            ds.add_css_class("dim-label")
            txt.append(nm)
            txt.append(ds)
            btn = Gtk.Button(label="Install", valign=Gtk.Align.CENTER)
            btn.connect("clicked", lambda _b, u=e["url"], n=e["name"]:
                        self._install_url(u, n))
            row.append(txt)
            row.append(btn)
            box.append(row)
        scroll = Gtk.ScrolledWindow()
        scroll.set_max_content_height(420)
        scroll.set_child(box)
        dlg = Adw.AlertDialog(
            heading="Community animations",
            body="Declarative packages only: colours, easing and one logo. "
                 "They cannot run code on your machine.",
            extra_child=scroll)
        dlg.add_response("close", "Close")
        dlg.present(self)

    def _install_url(self, url: str, label: str = "") -> None:
        self.toast(f"Installing {label or url}…")
        self._thread(lambda: openings.download_and_install(url), self._done_url)

    def _done_url(self, result) -> None:
        if isinstance(result, Exception):
            self.toast(f"Install failed: {result}")
            return
        self._openings = openings.load()
        openings.set_active(self._openings, result)
        self._refresh_openings()
        self.toast(f"Installed {result} — now active")

    # -- share / delete ------------------------------------------------
    def _open_share(self, variant_id: str) -> None:
        cfg = self._openings
        s = openings.spec(cfg, variant_id)
        if not s:
            return
        dest = os.path.join(os.path.expanduser("~"), "Downloads")
        try:
            path = openings.export_package(cfg, variant_id, dest)
        except ValueError as e:
            self.toast(str(e))
            return
        name = s["name"]
        gist = (f"gh gist create '{path}' --public "
                f"--desc '{name} — faceid-nim opening animation'")
        rel = (f"gh release create v1 '{path}' "
               f"--repo <you>/<repo> --generate-notes")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10,
                      width_request=470)
        head = Gtk.Label(label=f"Exported to\n{path}", xalign=0, wrap=True)
        head.add_css_class("dim-label")
        box.append(head)
        for title, cmd in (("One-line GitHub gist", gist),
                           ("Attach to a GitHub release", rel)):
            lab_t = Gtk.Label(label=title, xalign=0)
            lab_t.add_css_class("opening-field")
            lab_c = Gtk.Label(label=cmd, xalign=0, wrap=True, selectable=True,
                              hexpand=True)
            lab_c.add_css_class("cli-command")
            lab_c.add_css_class("dim-label")
            hb = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            hb.append(lab_c)
            cop = Gtk.Button(icon_name="edit-copy-symbolic",
                             valign=Gtk.Align.CENTER)
            cop.add_css_class("flat")
            cop.connect("clicked", lambda _b, t=cmd: self._copy(t))
            hb.append(cop)
            box.append(lab_t)
            box.append(hb)
        note = Gtk.Label(
            label="Other people install it with the app's “Install from "
                  "file”, or paste a raw .faceopen link into “Install "
                  "from URL”.",
            wrap=True, xalign=0)
        note.add_css_class("dim-label")
        box.append(note)
        dlg = Adw.AlertDialog(
            heading=f"Share “{name}”",
            body="Put the package file anywhere on GitHub — a repo, a "
                 "gist, a release. Nothing about it depends on this machine.",
            extra_child=box)
        dlg.add_response("close", "Done")
        dlg.present(self)

    def _delete_opening(self, variant_id: str) -> None:
        dlg = Adw.AlertDialog(
            heading="Delete this animation?",
            body="The animation and its logo files are removed from this "
                 "machine. Other people's copies are unaffected.")
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("delete", "Delete")
        dlg.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)

        def on_response(_d, resp: str) -> None:
            if resp == "delete":
                if openings.remove_variant(self._openings, variant_id):
                    self._refresh_openings()
                    self.toast("Animation deleted")

        dlg.connect("response", on_response)
        dlg.present(self)

    # ---- camera UI ---------------------------------------------------
    def on_camera_mode_changed(self, *_a) -> None:
        if self._suspend_changes:
            return
        mode = CAMERA_MODE_VALUES[self.cb_camera_mode.get_selected()]
        self._prefs["camera_mode"] = mode
        prefs.save(self._prefs)
        self._push_plan(camera_discovery.resolve_plan(self._prefs,
                                                      self._discovery))
        logging.getLogger("faceid_app").info("camera mode set to %s", mode)

    def _push_plan(self, plan: dict) -> None:
        s = dict(self._settings_cache or {})
        if not s:
            return
        s["mode"] = plan["mode"]
        s["camera"] = plan["camera"] or ""
        s["ir_camera"] = plan["ir_camera"]
        try:
            self.daemon.set_settings(s)
            self._settings_cache = s
        except DaemonError as e:
            self.toast(str(e))
        self._update_camera_ui(plan)

    def _update_camera_ui(self, plan: dict) -> None:
        rgb = plan.get("rgb_info")
        ir = plan.get("ir_info")
        mode = plan.get("mode", "rgb")
        want = plan.get("choosing", "auto")

        rgb_lab = "none detected"
        ir_lab = "none detected"
        if rgb is not None:
            w, h = rgb.max_resolution
            rgb_lab = f"{rgb.path} · {rgb.card.strip() or 'RGB'} · {w}x{h}"
        if ir is not None:
            w, h = ir.max_resolution
            ir_lab = f"{ir.path} · {ir.card.strip() or 'IR'} · {w}x{h}"
        self.rgb_det_row.set_subtitle(rgb_lab)
        self.ir_det_row.set_subtitle(ir_lab)

        used = f"{'IR' if mode == 'ir' else 'RGB'} camera"
        used_path = plan.get("ir_camera") if mode == "ir" else plan.get("camera")
        if used_path:
            subtitle = f"{used} · {used_path}"
        else:
            subtitle = f"{used} · none available"
        prefix = "Automatic: " if want == "auto" else ""
        self.cb_camera_mode.set_subtitle(prefix + subtitle)

        self._update_camera_page(plan)

        # Hero chip: never claim IR without evidence. It states what the
        # discovery actually found and what the plan is using.
        parts = [f"Using {used}" + (f" ({used_path})" if used_path else "")]
        for r in plan.get("reminders", []):
            parts.append(r)
        if not parts:
            parts = ["No camera data yet"]
        self.cam_chip.set_text(" · ".join(parts))

    def _update_camera_page(self, plan: dict) -> None:
        for cam in plan.get("cameras", []):
            w, h = cam.max_resolution
            res = f"{w}x{h}" if w else "n/a"
            fps = f"{cam.fps_estimate:.0f} fps" if cam.fps_estimate > 0 else ""
            kinds = ", ".join(f.fivecc.strip() for f in cam.formats[:8])
            state = "in use" if cam.busy else (
                "accessible" if cam.readable else "no access")
            kind_txt = {"ir": "IR", "rgb": "RGB", "none": "?"}.get(cam.kind, "?")
            title = f"{cam.path} · {kind_txt}"
            subtitle = (f"{cam.card.strip() or cam.driver}\n"
                        f"{res} · {fps} · {state} · {kinds}").strip(" ·") \
                if kinds else \
                (f"{cam.card.strip() or cam.driver}\n{res} · {state}")
            if cam.probe_error:
                subtitle = f"{subtitle} · {cam.probe_error}"
            row = Adw.ActionRow(title=title, subtitle=subtitle)
            t = Gtk.Button(label="Test", valign=Gtk.Align.CENTER)
            t.connect("clicked",
                      lambda _b, c=cam: self._open_camera_test(c))
            row.add_suffix(t)
            self.cam_group.add(row)
            self._cam_rows.append(row)
        if not plan.get("cameras"):
            r = Adw.ActionRow(title="No video devices found",
                              subtitle="Plug in a camera, then Refresh")
            self.cam_group.add(r)
            self._cam_rows.append(r)

    def _set_camera_field(self, entry: Adw.EntryRow, value: str) -> None:
        # Avoid re-entering the debounced apply while we load.
        if entry.get_text() != value:
            entry.set_text(value)

    @staticmethod
    def _diag_value(v) -> str:
        if v is None:
            return "not set"
        if v is True:
            return "yes"
        if v is False:
            return "no"
        return str(v)

    def _on_camera_edited(self, _row, _pspec) -> None:
        if self._camera_timer:
            GLib.source_remove(self._camera_timer)
        self._camera_timer = GLib.timeout_add(600, self._apply_camera)

    def _apply_camera(self) -> bool:
        self._camera_timer = 0
        rgb = self.camera_entry.get_text().strip()
        ir = self.ir_entry.get_text().strip()
        changed = (self._prefs.get("rgb_dev") != rgb
                   or self._prefs.get("ir_dev") != ir)
        self._prefs["rgb_dev"] = rgb
        self._prefs["ir_dev"] = ir
        prefs.save(self._prefs)
        if changed:
            self._push_plan(camera_discovery.resolve_plan(self._prefs,
                                                          self._discovery))
        return GLib.SOURCE_REMOVE

    def on_setting_changed(self, *_a) -> None:
        if self._suspend_changes:
            return
        s = getattr(self, "_settings_cache", None)
        if s is None:
            return
        s = dict(s)
        s["enabled"] = self.sw_enabled.get_active()
        strict = ["off", "light", "heavy"][self.cb_strict.get_selected()]
        s["strictness"] = strict
        s["scan_timeout_ms"] = int(self.sp_timeout.get_value())
        s["require_attention"] = self.sw_attention.get_active()
        self._update_strict_note(strict)
        try:
            self.daemon.set_settings(s)
            self._settings_cache = s
            self.refresh_status_only()
        except DaemonError as e:
            self.toast(str(e))

    def refresh_status_only(self) -> None:
        try:
            ids = self.daemon.list_identities()
        except DaemonError:
            ids = []
        enabled = bool(self._settings_cache.get("enabled"))
        try:
            diag = self.daemon.diagnostics()
        except DaemonError:
            diag = {}
        worker_up = bool(diag.get("worker_reachable"))
        if not enabled:
            self._set_status("warn", "Face unlock is off")
        elif not ids:
            self._set_status("warn", "Enabled but no faces enrolled")
        elif not worker_up:
            self._set_status("bad", "Worker is not responding — check the logs")
        else:
            self._set_status("ok",
                             f"{len(ids)} face{'s' if len(ids) != 1 else ''} "
                             "enrolled — ready")

    def on_enroll(self, _b) -> None:
        """Ask for a name (the unlock id), then open the wizard.

        The old blocking Enroll() call froze the window for half a
        minute with no preview and no way out. Everything that used to
        happen here now lives in OnboardingWindow, which streams the
        camera and can be cancelled at any point. The name chosen here
        becomes the identity name -- the id the lock screen shows when
        that face unlocks.
        """
        dlg = Adw.AlertDialog(
            heading="Give this face a name",
            body="This becomes your unlock id, shown on the lock screen "
                 "when this face unlocks. Letters, digits, dashes and "
                 "underscores, up to 48 characters.")
        entry = Gtk.Entry()
        entry.set_placeholder_text("e.g. face-office")
        entry.set_max_length(48)
        dlg.set_extra_child(entry)
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("start", "Start enrollment")
        dlg.set_default_response("start")
        dlg.set_response_appearance("start", Adw.ResponseAppearance.SUGGESTED)

        def on_response(dialog, response: str) -> None:
            if response != "start":
                return
            name = re.sub(r"[^A-Za-z0-9_-]", "_",
                          entry.get_text().strip()).strip("_")
            if not name or len(name) > 48:
                self.toast("Use a short id of letters, digits, dashes "
                           "or underscores")
                return
            try:
                used = ""
                plan = self._plan
                if plan:
                    used = (f"IR camera · {plan.get('ir_camera')}"
                            if plan.get("mode") == "ir" and plan.get("ir_camera")
                            else f"RGB camera · {plan.get('camera')}"
                            if plan.get("mode") == "rgb" and plan.get("camera")
                            else "no usable camera yet")
                win = OnboardingWindow(self, self.daemon, identity=name,
                                       on_done=self.refresh,
                                       camera_summary=used)
            except Exception as e:            # never crash the settings app
                self.toast(f"Cannot open enrollment: {e}")
                return
            win.present()

        dlg.connect("response", on_response)
        dlg.present(self)

    def on_test(self, _b) -> None:
        # Off the UI thread: the daemon drives the full scan + success
        # animation on the pill while this call is in flight, and the
        # window must keep responding.
        def run():
            try:
                return self.daemon.test_scan()
            except DaemonError as e:
                return (False, str(e))

        def then(res):
            try:
                ok, msg = res
            except (TypeError, ValueError):
                ok, msg = False, "Daemon unreachable"
            self.toast("Recognised" if ok else (msg or "Not recognised"))

        self._thread(run, then)

    def on_camera_test(self, _b) -> None:
        self._open_camera_test(None)

    def _open_camera_test(self, cam) -> None:
        if cam is not None:
            summary = camera_discovery.summary(cam)
        else:
            summary = camera_discovery.summary(
                (self._plan or {}).get("rgb_info")
                if (self._plan or {}).get("mode") == "rgb"
                else (self._plan or {}).get("ir_info")
                or (self._plan or {}).get("rgb_info"))
        try:
            win = CameraTestWindow(self, self.daemon, camera_summary=summary)
        except Exception as e:            # never crash the settings app
            self.toast(f"Cannot open the camera test: {e}")
            return
        win.present()

    def _toggle(self, name: str, enabled: bool) -> None:
        try:
            self.daemon.set_identity_enabled(name, enabled)
        except DaemonError as e:
            self.toast(str(e))

    def _delete(self, name: str) -> None:
        try:
            self.daemon.delete_identity(name)
            self.toast(f"Deleted {name}")
            self.refresh()
        except DaemonError as e:
            self.toast(str(e))

    def on_delete_all(self, _b) -> None:
        dlg = Adw.AlertDialog(
            heading="Remove all face data?",
            body="Every encrypted template for your account is deleted "
                 "immediately. This cannot be undone.")
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("delete", "Delete everything")
        dlg.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dlg.connect("response", lambda _d, r: r == "delete" and self._wipe())
        dlg.present(self)

    def _wipe(self) -> None:
        try:
            self.daemon.delete_all_data()
            self.toast("All face data removed")
            self.refresh()
        except DaemonError as e:
            self.toast(str(e))


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID)
        self.daemon = Daemon()

    def do_activate(self):
        win = self.props.active_window or Window(self, self.daemon)
        win.present()


def main(argv=None) -> int:
    argv = list(argv if argv is not None else sys.argv)
    debug = "--debug" in argv
    setup_logging(debug)
    if "--diagnose" in argv:
        return run_diagnose()
    log = logging.getLogger("faceid_app")
    if debug:
        log.debug("debug logging enabled")
    _load_css()
    return App().run(argv)


if __name__ == "__main__":
    raise SystemExit(main())
