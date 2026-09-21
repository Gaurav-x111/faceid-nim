"""Thin D-Bus client for org.faceidnim.Daemon1 (system bus)."""
from __future__ import annotations

import json

from gi.repository import Gio, GLib

BUS = "org.faceidnim.Daemon1"
PATH = "/org/faceidnim/Daemon1"

ENROLL_BUS = "org.faceidnim.Daemon1"
ENROLL_PATH = "/org/faceidnim/Enrollment1"
ENROLL_IFACE = "org.faceidnim.Enrollment1"


class DaemonError(RuntimeError):
    pass


class Daemon:
    def __init__(self):
        self._proxy: Gio.DBusProxy | None = None

    @property
    def proxy(self) -> Gio.DBusProxy:
        if self._proxy is None:
            try:
                self._proxy = Gio.DBusProxy.new_for_bus_sync(
                    Gio.BusType.SYSTEM, Gio.DBusProxyFlags.NONE, None,
                    BUS, PATH, BUS, None)
            except GLib.Error as e:
                raise DaemonError(f"cannot reach faceid-nimd: {e.message}") from e
        return self._proxy

    def _call(self, method: str, variant: GLib.Variant | None = None,
              timeout_ms: int = 60000):
        try:
            return self.proxy.call_sync(
                method, variant, Gio.DBusCallFlags.NONE, timeout_ms, None)
        except GLib.Error as e:
            raise DaemonError(e.message) from e

    def list_identities(self) -> list[tuple[str, bool, str]]:
        return list(self._call("ListIdentities")[0])

    def enroll(self, identity: str, poses: int = 9) -> int:
        return int(self._call("Enroll",
                              GLib.Variant("(su)", (identity, poses)))[0])

    def delete_identity(self, name: str) -> None:
        self._call("DeleteIdentity", GLib.Variant("(s)", (name,)))

    def delete_all_data(self) -> None:
        self._call("DeleteAllData")

    def set_identity_enabled(self, name: str, enabled: bool) -> None:
        self._call("SetIdentityEnabled", GLib.Variant("(sb)", (name, enabled)))

    def test_scan(self) -> tuple[bool, str]:
        ok, msg = self._call("TestScan")
        return bool(ok), str(msg)

    def get_settings(self) -> dict:
        return json.loads(self._call("GetSettings")[0])

    def set_settings(self, settings: dict) -> None:
        self._call("SetSettings", GLib.Variant("(s)", (json.dumps(settings),)))

    def diagnostics(self) -> dict:
        return json.loads(self._call("Diagnostics")[0])

    def subscribe_state(self, cb) -> int:
        """cb(state: str, progress: float, reason: str)"""
        def _on(_p, _sender, signal, params):
            if signal == "ScanState":
                cb(*params)
        return self.proxy.connect("g-signal", _on)


class Enrollment:
    """Client for org.faceidnim.Enrollment1.

    Everything here is async except StartEnrollment, which has to be
    synchronous to receive the preview file descriptor -- but it
    returns as soon as the session is opened, before any camera work,
    so it does not block the UI for a perceptible time. Each pose
    capture holds the camera for seconds and is therefore always
    dispatched asynchronously.
    """

    def __init__(self):
        self._proxy: Gio.DBusProxy | None = None
        self.session: str | None = None
        self.preview_fd: int | None = None

    @property
    def proxy(self) -> Gio.DBusProxy:
        if self._proxy is None:
            try:
                self._proxy = Gio.DBusProxy.new_for_bus_sync(
                    Gio.BusType.SYSTEM, Gio.DBusProxyFlags.NONE, None,
                    ENROLL_BUS, ENROLL_PATH, ENROLL_IFACE, None)
            except GLib.Error as e:
                raise DaemonError(
                    f"cannot reach the enrollment service: {e.message}") from e
        return self._proxy

    # ---- session lifecycle --------------------------------------------
    def list_poses(self) -> list[str]:
        try:
            return list(self.proxy.call_sync(
                "ListPoses", None, Gio.DBusCallFlags.NONE, 5000, None)[0])
        except GLib.Error as e:
            raise DaemonError(e.message) from e

    def start(self, identity: str = "default") -> int:
        """Open a session. Returns the preview pipe's file descriptor."""
        try:
            reply, fdlist = self.proxy.call_with_unix_fd_list_sync(
                "StartEnrollment", GLib.Variant("(s)", (identity,)),
                Gio.DBusCallFlags.NONE, 20000, None, None)
        except GLib.Error as e:
            raise DaemonError(e.message) from e

        self.session = str(reply[0])
        # The fd arrives as an index into the message's fd list, which
        # is why a plain call_sync cannot be used here.
        try:
            self.preview_fd = fdlist.steal_fds()[reply[1]]
        except (AttributeError, IndexError) as e:
            raise DaemonError("daemon did not send a preview descriptor") from e
        return self.preview_fd

    def enroll_pose_async(self, pose_index: int, callback) -> None:
        """callback(status: str, progress: float, error: str | None)"""
        if self.session is None:
            callback("failed", 0.0, "no enrollment session")
            return

        def done(proxy, res, _user):
            try:
                status, progress = proxy.call_finish(res)
                callback(str(status), float(progress), None)
            except GLib.Error as e:
                callback("failed", 0.0, e.message)

        # Generous timeout: one pose is a full camera scan, and the
        # daemon's own scan timeout is the real bound.
        self.proxy.call(
            "EnrollPose",
            GLib.Variant("(su)", (self.session, pose_index)),
            Gio.DBusCallFlags.NONE, 60000, None, done, None)

    def finish_async(self, callback) -> None:
        """callback(templates_saved: int, error: str | None)"""
        if self.session is None:
            callback(0, "no enrollment session")
            return

        def done(proxy, res, _user):
            try:
                callback(int(proxy.call_finish(res)[0]), None)
            except GLib.Error as e:
                callback(0, e.message)

        self.proxy.call("FinishEnrollment",
                        GLib.Variant("(s)", (self.session,)),
                        Gio.DBusCallFlags.NONE, 30000, None, done, None)

    def cancel(self) -> None:
        """Fire and forget. Cancelling must work even if the daemon has
        already gone away, so every failure here is swallowed."""
        if self.session is None:
            return
        try:
            self.proxy.call("CancelEnrollment",
                            GLib.Variant("(s)", (self.session,)),
                            Gio.DBusCallFlags.NONE, 5000, None, None, None)
        except GLib.Error:
            pass
        finally:
            self.session = None

    def session_info(self) -> tuple[int, int, str, int]:
        if self.session is None:
            return (0, 0, "", 0)
        try:
            r = self.proxy.call_sync(
                "GetSessionInfo", GLib.Variant("(s)", (self.session,)),
                Gio.DBusCallFlags.NONE, 5000, None)
            return (int(r[0]), int(r[1]), str(r[2]), int(r[3]))
        except GLib.Error as e:
            raise DaemonError(e.message) from e

    def subscribe_progress(self, cb) -> int:
        """cb(session, pose_index, pose_name, status, progress)"""
        def _on(_p, _sender, signal, params):
            if signal == "EnrollProgress":
                cb(*params)
        return self.proxy.connect("g-signal", _on)

    def close(self) -> None:
        self.cancel()
        if self.preview_fd is not None:
            try:
                import os
                os.close(self.preview_fd)
            except OSError:
                pass
            self.preview_fd = None
