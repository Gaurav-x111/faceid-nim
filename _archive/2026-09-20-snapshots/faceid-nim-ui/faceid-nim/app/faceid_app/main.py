#!/usr/bin/env python3
"""faceid-nim settings app (GTK4 + libadwaita).

Deliberately not required at runtime: the daemon does the unlocking.
This app enrolls, manages identities, changes settings and runs
diagnostics. Closing it changes nothing about how the machine
authenticates.

Layout follows GNOME Settings rather than inventing its own shell: a
sidebar of pages, boxed lists, and one hero card at the top of the
first page that answers the only question most people open this app
to ask -- is it on, and is it working?
"""
from __future__ import annotations

import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from .dbus_client import Daemon, DaemonError  # noqa: E402
from .onboarding import OnboardingWindow  # noqa: E402

APP_ID = "org.faceidnim.App"

STRICTNESS = ["off", "light", "heavy"]
MODES = ["rgb", "ir", "both"]

STRICTNESS_BLURB = {
    "off": "Recognition only. No spoof checks at all — not recommended.",
    "light": "Spoof cues (screen glare, a device bezel, moiré) veto an unlock.",
    "heavy": "Also requires positive evidence of a real 3D face: a blink, "
             "or head motion with real depth.",
}


class HeroCard(Gtk.Box):
    """The one thing worth seeing at a glance: on/off plus health."""

    def __init__(self, on_toggle):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        self.add_css_class("faceid-hero")

        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        self.icon = Gtk.Image.new_from_icon_name("avatar-default-symbolic")
        self.icon.set_pixel_size(44)
        top.append(self.icon)

        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2,
                       hexpand=True, valign=Gtk.Align.CENTER)
        self.title = Gtk.Label(label="Face unlock is off", xalign=0)
        self.title.add_css_class("faceid-hero-title")
        self.subtitle = Gtk.Label(label="", xalign=0, wrap=True)
        self.subtitle.add_css_class("dim-label")
        text.append(self.title)
        text.append(self.subtitle)
        top.append(text)

        self.switch = Gtk.Switch(valign=Gtk.Align.CENTER)
        self.switch.connect("notify::active", on_toggle)
        top.append(self.switch)
        self.append(top)

        self.health = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=18)
        self.append(self.health)

    def set_state(self, enabled: bool, identities: int, worker_ok: bool):
        self.set_css_classes(
            ["faceid-hero"] if enabled else ["faceid-hero", "faceid-hero-off"])
        self.icon.set_from_icon_name(
            "emblem-ok-symbolic" if enabled else "avatar-default-symbolic")
        self.title.set_text(
            "Face unlock is on" if enabled else "Face unlock is off")

        if not identities:
            self.subtitle.set_text(
                "No face enrolled yet. Your password is the only way in.")
        elif enabled:
            self.subtitle.set_text(
                "Your password always works as well.")
        else:
            self.subtitle.set_text(
                f"{identities} face{'s' if identities != 1 else ''} enrolled. "
                "Switch it on when you are ready.")

        # Toggling requires something to actually match against.
        self.switch.set_sensitive(bool(identities) and worker_ok)

        child = self.health.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self.health.remove(child)
            child = nxt
        for label, ok in (("Service", worker_ok),
                          ("Enrolled", bool(identities)),
                          ("Camera", worker_ok)):
            self.health.append(self._pill(label, ok))

    @staticmethod
    def _pill(label: str, ok: bool) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=7)
        dot = Gtk.Box()
        dot.add_css_class("faceid-dot")
        dot.add_css_class("faceid-dot-ok" if ok else "faceid-dot-bad")
        dot.set_valign(Gtk.Align.CENTER)
        box.append(dot)
        lab = Gtk.Label(label=label)
        lab.add_css_class("caption")
        lab.add_css_class("dim-label")
        box.append(lab)
        return box


class Window(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application, daemon: Daemon):
        super().__init__(application=app, title="Face Unlock",
                         default_width=900, default_height=720)
        self.daemon = daemon
        self._settings_cache: dict | None = None
        self._loading = False
        self._id_rows: list = []
        self._diag_rows: list = []

        self.toasts = Adw.ToastOverlay()
        self.split = Adw.NavigationSplitView()

        # --- sidebar ---
        self.stack = Adw.ViewStack()
        sidebar_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        sidebar_list.add_css_class("navigation-sidebar")
        for name, title, icon in (
                ("overview", "Overview", "avatar-default-symbolic"),
                ("settings", "Settings", "preferences-system-symbolic"),
                ("security", "Security", "channel-secure-symbolic"),
                ("diagnostics", "Diagnostics", "utilities-system-monitor-symbolic")):
            row = Adw.ActionRow(title=title)
            row.add_prefix(Gtk.Image.new_from_icon_name(icon))
            row.set_name(name)
            sidebar_list.append(row)
        sidebar_list.connect(
            "row-selected",
            lambda _lb, row: row and self.stack.set_visible_child_name(row.get_name()))
        sidebar_list.select_row(sidebar_list.get_row_at_index(0))

        side_view = Adw.ToolbarView()
        side_view.add_top_bar(Adw.HeaderBar())
        side_view.set_content(sidebar_list)
        self.split.set_sidebar(
            Adw.NavigationPage(child=side_view, title="Face Unlock"))

        # --- content ---
        self.stack.add_named(self._overview_page(), "overview")
        self.stack.add_named(self._settings_page(), "settings")
        self.stack.add_named(self._security_page(), "security")
        self.stack.add_named(self._diagnostics_page(), "diagnostics")

        content_view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        refresh = Gtk.Button(icon_name="view-refresh-symbolic",
                             tooltip_text="Refresh")
        refresh.connect("clicked", lambda _b: self.refresh())
        header.pack_end(refresh)
        content_view.add_top_bar(header)
        content_view.set_content(self.stack)
        self.split.set_content(
            Adw.NavigationPage(child=content_view, title="Face Unlock"))

        self.toasts.set_child(self.split)
        self.set_content(self.toasts)
        self.refresh()

    # ---- pages ---------------------------------------------------------
    def _scroller(self, child) -> Gtk.Widget:
        clamp = Adw.Clamp(maximum_size=680, tightening_threshold=560,
                          child=child, margin_top=24, margin_bottom=24,
                          margin_start=18, margin_end=18)
        return Gtk.ScrolledWindow(child=clamp, vexpand=True)

    def _overview_page(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=22)
        self.hero = HeroCard(self._on_enabled_toggled)
        box.append(self.hero)

        self.id_group = Adw.PreferencesGroup(
            title="Enrolled faces",
            description="Add one identity per situation you actually use — "
                        "glasses on and off, a beard, a dim room. Matching "
                        "takes the best of all enabled identities.")
        add = Gtk.Button(icon_name="list-add-symbolic", valign=Gtk.Align.CENTER,
                         tooltip_text="Enroll a new face")
        add.add_css_class("flat")
        add.connect("clicked", self.on_enroll)
        self.id_group.set_header_suffix(add)
        box.append(self.id_group)

        actions = Adw.PreferencesGroup()
        enroll_row = Adw.ActionRow(
            title="Enroll a face",
            subtitle="Guided, nine positions, about twenty seconds")
        enroll_row.add_prefix(
            Gtk.Image.new_from_icon_name("camera-photo-symbolic"))
        b = Gtk.Button(label="Start", valign=Gtk.Align.CENTER)
        b.add_css_class("suggested-action")
        b.connect("clicked", self.on_enroll)
        enroll_row.add_suffix(b)
        enroll_row.set_activatable_widget(b)
        actions.add(enroll_row)

        test_row = Adw.ActionRow(
            title="Test a scan",
            subtitle="Runs a real scan without unlocking anything")
        test_row.add_prefix(Gtk.Image.new_from_icon_name("emblem-ok-symbolic"))
        tb = Gtk.Button(label="Test", valign=Gtk.Align.CENTER)
        tb.connect("clicked", self.on_test)
        test_row.add_suffix(tb)
        test_row.set_activatable_widget(tb)
        actions.add(test_row)
        box.append(actions)
        return self._scroller(box)

    def _settings_page(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=22)

        g = Adw.PreferencesGroup(title="Recognition")
        self.cb_strict = Adw.ComboRow(
            title="Strictness", model=Gtk.StringList.new(STRICTNESS))
        self.cb_strict.connect("notify::selected", self._on_strictness_changed)
        g.add(self.cb_strict)

        self.strict_banner = Adw.Banner(revealed=False)
        box.append(g)
        box.append(self.strict_banner)

        g2 = Adw.PreferencesGroup(title="Camera")
        self.cb_mode = Adw.ComboRow(title="Source",
                                    model=Gtk.StringList.new(MODES))
        self.cb_mode.set_subtitle(
            "Infrared is much harder to spoof: screens emit almost no infrared.")
        self.cb_mode.connect("notify::selected", self.on_setting_changed)
        g2.add(self.cb_mode)

        self.sp_timeout = Adw.SpinRow.new_with_range(500, 15000, 250)
        self.sp_timeout.set_title("Scan timeout")
        self.sp_timeout.set_subtitle(
            "Milliseconds. Keep it short: the password box usually cannot be "
            "used until the face attempt finishes.")
        self.sp_timeout.connect("notify::value", self.on_setting_changed)
        g2.add(self.sp_timeout)

        self.sw_attention = Adw.SwitchRow(
            title="Require attention",
            subtitle="Eyes open and facing the camera. Stops the laptop being "
                     "held up to a sleeping user.")
        self.sw_attention.connect("notify::active", self.on_setting_changed)
        g2.add(self.sw_attention)
        box.append(g2)
        return self._scroller(box)

    def _security_page(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=22)

        warn = Adw.PreferencesGroup(title="What this protects against")
        for title, sub, icon in (
            ("This is not Face ID",
             "Your laptop has no depth sensor. A webcam sees a flat image, "
             "not a 3D map of your face.", "dialog-warning-symbolic"),
            ("A video replay can defeat it on an RGB webcam",
             "Printed photos and photos on a screen are caught. A video of "
             "you played back is not reliably caught.", "camera-video-symbolic"),
            ("Close look-alikes raise false accepts",
             "Siblings and twins score far higher than strangers.",
             "system-users-symbolic"),
            ("Your password always works",
             "The PAM profile is 'sufficient', never 'required'. If face "
             "unlock breaks, you log in normally.", "dialog-password-symbolic"),
        ):
            row = Adw.ActionRow(title=title, subtitle=sub)
            row.add_prefix(Gtk.Image.new_from_icon_name(icon))
            warn.add(row)
        box.append(warn)

        privacy = Adw.PreferencesGroup(
            title="Your data",
            description="Photographs are never stored. Enrollment keeps only "
                        "numeric templates, encrypted and readable by root "
                        "alone. Nothing leaves this machine.")
        wipe = Adw.ActionRow(
            title="Remove all face data",
            subtitle="Deletes every encrypted template for your account "
                     "immediately. This cannot be undone.")
        wipe.add_prefix(Gtk.Image.new_from_icon_name("user-trash-symbolic"))
        db = Gtk.Button(label="Delete", valign=Gtk.Align.CENTER)
        db.add_css_class("destructive-action")
        db.connect("clicked", self.on_delete_all)
        wipe.add_suffix(db)
        privacy.add(wipe)
        box.append(privacy)
        return self._scroller(box)

    def _diagnostics_page(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=22)
        self.diag_group = Adw.PreferencesGroup(
            title="System check",
            description="If something here is wrong, face unlock simply will "
                        "not be offered — it never falls back to something "
                        "less safe.")
        box.append(self.diag_group)

        hint = Adw.PreferencesGroup(title="From a terminal")
        for cmd, desc in (("faceid-nim status", "Services, sockets, models, config"),
                          ("sudo faceid-nim fetch-models", "Download and verify models"),
                          ("faceid-nim verify", "Re-check model checksums")):
            r = Adw.ActionRow(title=cmd, subtitle=desc)
            r.add_css_class("monospace")
            copy = Gtk.Button(icon_name="edit-copy-symbolic",
                              valign=Gtk.Align.CENTER)
            copy.add_css_class("flat")
            copy.connect("clicked", lambda _b, c=cmd: self._copy(c))
            r.add_suffix(copy)
            hint.add(r)
        box.append(hint)
        return self._scroller(box)

    # ---- behaviour -----------------------------------------------------
    def _copy(self, text: str) -> None:
        Gdk.Display.get_default().get_clipboard().set(text)
        self.toast("Copied")

    def toast(self, text: str) -> None:
        self.toasts.add_toast(Adw.Toast(title=text))

    def _clear(self, group, rows: list) -> None:
        for r in rows:
            group.remove(r)
        rows.clear()

    def refresh(self) -> None:
        self._loading = True          # stop the widget callbacks writing back
        try:
            self._refresh_inner()
        except DaemonError as e:
            self.toast(str(e))
            self.hero.set_state(False, 0, False)
        finally:
            self._loading = False

    def _refresh_inner(self) -> None:
        identities = self.daemon.list_identities()
        self._clear(self.id_group, self._id_rows)
        for name, enabled, model_id in identities:
            row = Adw.SwitchRow(title=name, subtitle=f"model {model_id}",
                                active=enabled)
            row.add_prefix(Gtk.Image.new_from_icon_name("avatar-default-symbolic"))
            row.connect("notify::active",
                        lambda r, _p, n=name: self._toggle(n, r.get_active()))
            d = Gtk.Button(icon_name="user-trash-symbolic",
                           valign=Gtk.Align.CENTER)
            d.add_css_class("flat")
            d.connect("clicked", lambda _b, n=name: self._delete(n))
            row.add_suffix(d)
            self.id_group.add(row)
            self._id_rows.append(row)
        if not identities:
            r = Adw.ActionRow(
                title="No faces enrolled",
                subtitle="Face unlock stays off until you add one")
            r.add_prefix(Gtk.Image.new_from_icon_name("list-add-symbolic"))
            self.id_group.add(r)
            self._id_rows.append(r)

        s = self.daemon.get_settings()
        self._settings_cache = s
        strict = str(s.get("strictness", "light"))
        self.cb_strict.set_selected(
            STRICTNESS.index(strict) if strict in STRICTNESS else 1)
        self._update_strict_banner(strict)
        mode = str(s.get("mode", "rgb"))
        self.cb_mode.set_selected(MODES.index(mode) if mode in MODES else 0)
        self.sp_timeout.set_value(float(s.get("scan_timeout_ms", 4000)))
        self.sw_attention.set_active(bool(s.get("require_attention", True)))

        diag = self.daemon.diagnostics()
        self._clear(self.diag_group, self._diag_rows)
        for k, v in diag.items():
            row = Adw.ActionRow(title=str(k).replace("_", " ").capitalize(),
                                subtitle=str(v))
            ok = v is True or (isinstance(v, str) and v not in ("", "false"))
            if isinstance(v, bool) or k.endswith("reachable"):
                row.add_prefix(Gtk.Image.new_from_icon_name(
                    "emblem-ok-symbolic" if ok else "dialog-warning-symbolic"))
            self.diag_group.add(row)
            self._diag_rows.append(row)

        self.hero.switch.set_active(bool(s.get("enabled")))
        self.hero.set_state(bool(s.get("enabled")), len(identities),
                            bool(diag.get("worker_reachable")))

    def _update_strict_banner(self, strict: str) -> None:
        self.strict_banner.set_title(STRICTNESS_BLURB.get(strict, ""))
        self.strict_banner.set_revealed(bool(strict))

    def _on_strictness_changed(self, *_a) -> None:
        self._update_strict_banner(STRICTNESS[self.cb_strict.get_selected()])
        self.on_setting_changed()

    def _on_enabled_toggled(self, *_a) -> None:
        self.on_setting_changed()

    def on_setting_changed(self, *_a) -> None:
        if self._loading or self._settings_cache is None:
            return
        s = dict(self._settings_cache)
        s["enabled"] = self.hero.switch.get_active()
        s["strictness"] = STRICTNESS[self.cb_strict.get_selected()]
        s["mode"] = MODES[self.cb_mode.get_selected()]
        s["scan_timeout_ms"] = int(self.sp_timeout.get_value())
        s["require_attention"] = self.sw_attention.get_active()
        try:
            # The daemon re-checks polkit, so an unauthorised change is
            # refused here rather than silently applied.
            self.daemon.set_settings(s)
            self._settings_cache = s
        except DaemonError as e:
            self.toast(str(e))
            self.refresh()          # snap the widgets back to reality

    def on_enroll(self, _b) -> None:
        """Open the guided wizard.

        The old blocking Enroll() call froze the window for half a
        minute with no preview and no way out. Everything that used to
        happen here now lives in OnboardingWindow, which streams the
        camera and can be cancelled at any point.
        """
        try:
            win = OnboardingWindow(self, self.daemon, identity="default",
                                   on_done=self.refresh)
        except Exception as e:              # never crash the settings app
            self.toast(f"Cannot open enrollment: {e}")
            return
        win.present()

    def on_test(self, _b) -> None:
        try:
            ok, msg = self.daemon.test_scan()
        except DaemonError as e:
            self.toast(str(e))
            return
        self.toast("Recognised" if ok else (msg or "Not recognised"))

    def _toggle(self, name: str, enabled: bool) -> None:
        if self._loading:
            return
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

    def do_startup(self):
        Adw.Application.do_startup(self)
        provider = Gtk.CssProvider()
        try:
            provider.load_from_resource("/org/faceidnim/App/style.css")
        except GLib.Error:
            import pathlib
            css = pathlib.Path(__file__).with_name("style.css")
            if css.exists():
                provider.load_from_path(str(css))
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    def do_activate(self):
        win = self.props.active_window or Window(self, self.daemon)
        win.present()


def main(argv=None) -> int:
    return App().run(argv if argv is not None else sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
