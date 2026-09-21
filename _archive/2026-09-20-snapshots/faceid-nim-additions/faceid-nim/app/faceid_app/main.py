#!/usr/bin/env python3
"""faceid-nim settings app (GTK4 + libadwaita).

Deliberately not required at runtime: the daemon does the unlocking.
This app enrolls, manages identities, changes settings and runs
diagnostics. Closing it changes nothing about how the machine
authenticates.
"""
from __future__ import annotations

import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from .dbus_client import Daemon, DaemonError  # noqa: E402
from .onboarding import OnboardingWindow  # noqa: E402

APP_ID = "org.faceidnim.App"


class Window(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application, daemon: Daemon):
        super().__init__(application=app, title="Face Unlock",
                         default_width=620, default_height=680)
        self.daemon = daemon
        self.toasts = Adw.ToastOverlay()

        view = Adw.ToolbarView()
        view.add_top_bar(Adw.HeaderBar())
        self.stack = Adw.ViewStack()
        switcher = Adw.ViewSwitcherBar(stack=self.stack, reveal=True)

        self.stack.add_titled_with_icon(self._identities_page(), "ids",
                                        "Identities", "avatar-default-symbolic")
        self.stack.add_titled_with_icon(self._settings_page(), "settings",
                                        "Settings", "preferences-system-symbolic")
        self.stack.add_titled_with_icon(self._diagnostics_page(), "diag",
                                        "Diagnostics", "utilities-system-monitor-symbolic")

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(self.stack)
        box.append(switcher)
        view.set_content(box)
        self.toasts.set_child(view)
        self.set_content(self.toasts)

        self.refresh()

    # ---- pages ---------------------------------------------------------
    def _identities_page(self) -> Gtk.Widget:
        page = Adw.PreferencesPage()
        self.id_group = Adw.PreferencesGroup(
            title="Enrolled faces",
            description="Add several identities for the conditions you "
                        "actually use: glasses on and off, a dim room, a beard. "
                        "Matching takes the best of all enabled identities.")
        page.add(self.id_group)

        actions = Adw.PreferencesGroup()
        row = Adw.ActionRow(title="Add a new identity",
                            subtitle="Nine poses, about 20 seconds")
        btn = Gtk.Button(label="Enroll", valign=Gtk.Align.CENTER)
        btn.add_css_class("suggested-action")
        btn.connect("clicked", self.on_enroll)
        row.add_suffix(btn)
        actions.add(row)

        test = Adw.ActionRow(title="Test a scan",
                             subtitle="Runs a real scan without unlocking anything")
        tbtn = Gtk.Button(label="Test", valign=Gtk.Align.CENTER)
        tbtn.connect("clicked", self.on_test)
        test.add_suffix(tbtn)
        actions.add(test)

        danger = Adw.PreferencesGroup(title="Data")
        drow = Adw.ActionRow(
            title="Remove all face data",
            subtitle="Deletes every encrypted template for your account immediately")
        dbtn = Gtk.Button(label="Delete", valign=Gtk.Align.CENTER)
        dbtn.add_css_class("destructive-action")
        dbtn.connect("clicked", self.on_delete_all)
        drow.add_suffix(dbtn)
        danger.add(drow)

        page.add(actions)
        page.add(danger)
        return page

    def _settings_page(self) -> Gtk.Widget:
        page = Adw.PreferencesPage()
        g = Adw.PreferencesGroup(title="Face unlock")

        self.sw_enabled = Adw.SwitchRow(
            title="Enable face unlock",
            subtitle="Your password always works as well")
        self.sw_enabled.connect("notify::active", self.on_setting_changed)
        g.add(self.sw_enabled)

        self.cb_strict = Adw.ComboRow(
            title="Strictness",
            subtitle="Heavy requires positive evidence of a real 3D face "
                     "(a blink or head motion) before unlocking",
            model=Gtk.StringList.new(["off", "light", "heavy"]))
        self.cb_strict.connect("notify::selected", self.on_setting_changed)
        g.add(self.cb_strict)

        self.cb_mode = Adw.ComboRow(
            title="Camera",
            model=Gtk.StringList.new(["rgb", "ir", "both"]))
        self.cb_mode.connect("notify::selected", self.on_setting_changed)
        g.add(self.cb_mode)

        self.sp_timeout = Adw.SpinRow.new_with_range(500, 15000, 250)
        self.sp_timeout.set_title("Scan timeout (ms)")
        self.sp_timeout.set_subtitle(
            "Keep this short: the password box usually cannot be used "
            "until the face attempt finishes")
        self.sp_timeout.connect("notify::value", self.on_setting_changed)
        g.add(self.sp_timeout)

        warn = Adw.PreferencesGroup(title="What this protects against")
        warn.add(Adw.ActionRow(
            title="This is a convenience feature, not Face ID",
            subtitle="On an ordinary RGB webcam a good video replay can defeat "
                     "it. An IR camera is substantially stronger. Twins and "
                     "siblings raise false accepts."))
        page.add(g)
        page.add(warn)
        return page

    def _diagnostics_page(self) -> Gtk.Widget:
        page = Adw.PreferencesPage()
        self.diag_group = Adw.PreferencesGroup(title="System check")
        page.add(self.diag_group)
        g = Adw.PreferencesGroup()
        row = Adw.ActionRow(title="Re-run checks")
        b = Gtk.Button(label="Refresh", valign=Gtk.Align.CENTER)
        b.connect("clicked", lambda _b: self.refresh())
        row.add_suffix(b)
        g.add(row)
        page.add(g)
        return page

    # ---- behaviour -----------------------------------------------------
    def toast(self, text: str) -> None:
        self.toasts.add_toast(Adw.Toast(title=text))

    def _clear(self, group: Adw.PreferencesGroup, rows: list) -> None:
        for r in rows:
            group.remove(r)
        rows.clear()

    def refresh(self) -> None:
        self._id_rows = getattr(self, "_id_rows", [])
        self._diag_rows = getattr(self, "_diag_rows", [])
        self._clear(self.id_group, self._id_rows)
        self._clear(self.diag_group, self._diag_rows)

        try:
            for name, enabled, model_id in self.daemon.list_identities():
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
            if not self._id_rows:
                r = Adw.ActionRow(title="No faces enrolled",
                                  subtitle="Face unlock stays off until you add one")
                self.id_group.add(r)
                self._id_rows.append(r)

            s = self.daemon.get_settings()
            self.sw_enabled.set_active(bool(s.get("enabled")))
            for combo, key, opts in (
                    (self.cb_strict, "strictness", ["off", "light", "heavy"]),
                    (self.cb_mode, "mode", ["rgb", "ir", "both"])):
                val = str(s.get(key, opts[1 if key == "strictness" else 0]))
                combo.set_selected(opts.index(val) if val in opts else 0)
            self.sp_timeout.set_value(float(s.get("scan_timeout_ms", 4000)))
            self._settings_cache = s

            for k, v in self.daemon.diagnostics().items():
                r = Adw.ActionRow(title=k.replace("_", " "), subtitle=str(v))
                self.diag_group.add(r)
                self._diag_rows.append(r)
        except DaemonError as e:
            self.toast(str(e))

    def on_setting_changed(self, *_a) -> None:
        s = getattr(self, "_settings_cache", None)
        if s is None:
            return
        s = dict(s)
        s["enabled"] = self.sw_enabled.get_active()
        s["strictness"] = ["off", "light", "heavy"][self.cb_strict.get_selected()]
        s["mode"] = ["rgb", "ir", "both"][self.cb_mode.get_selected()]
        s["scan_timeout_ms"] = int(self.sp_timeout.get_value())
        try:
            # The daemon re-checks polkit, so an unauthorised change is
            # refused here rather than silently applied.
            self.daemon.set_settings(s)
            self._settings_cache = s
        except DaemonError as e:
            self.toast(str(e))

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
        except Exception as e:                # never crash the settings app
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
    return App().run(argv if argv is not None else sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
