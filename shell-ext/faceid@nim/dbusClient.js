/* Subscribes to org.faceidnim.Daemon1's ScanState signal.
 *
 * This is the whole reason the extension survives GNOME upgrades: it
 * does not patch AuthPrompt._onShowMessage or any other private shell
 * internal, it just listens on the system bus. It also never receives
 * a camera frame, an embedding or a similarity score -- only a state
 * name, a progress number and a short reason string.
 *
 * LISTEN-ONLY (1B-2): this proxy declares the ScanState signal and
 * nothing else. The extension must call NO daemon methods: TestScan
 * opens the camera, so a hover-to-retry from the lock screen was a
 * silent camera activation by anything that could move the mouse.
 * shell-ext/check-listen-only.sh enforces this in CI -- do not add a
 * <method> entry or a proxy invocation here. */

import Gio from 'gi://Gio';

const IFACE = `
<node>
  <interface name="org.faceidnim.Daemon1">
    <signal name="ScanState">
      <arg type="s" name="state"/>
      <arg type="d" name="progress"/>
      <arg type="s" name="reason"/>
    </signal>
  </interface>
</node>`;

const Proxy = Gio.DBusProxy.makeProxyWrapper(IFACE);

export class DaemonClient {
    constructor() {
        this._proxy = null;
        this._signalId = 0;
        this._watchId = 0;
        this._onState = null;
    }

    connect(onState) {
        this._onState = onState;
        this._watchId = Gio.bus_watch_name(
            Gio.BusType.SYSTEM,
            'org.faceidnim.Daemon1',
            Gio.BusNameWatcherFlags.NONE,
            () => this._appeared(),
            () => this._vanished());
    }

    _appeared() {
        this._disconnectProxy();
        try {
            this._proxy = new Proxy(
                Gio.DBus.system,
                'org.faceidnim.Daemon1',
                '/org/faceidnim/Daemon1');
            this._signalId = this._proxy.connectSignal(
                'ScanState',
                (_p, _sender, [state, progress, reason]) => {
                    // Anything on the bus is untrusted input, even from
                    // a service we expect. Clamp and coerce before use.
                    const p = Math.max(0, Math.min(1, Number(progress) || 0));
                    const s = String(state ?? 'idle').slice(0, 32);
                    const r = String(reason ?? '').slice(0, 120);
                    this._onState?.(s, p, r);
                });
        } catch (e) {
            logError(e, 'faceid@nim: cannot proxy the daemon');
        }
    }

    _vanished() {
        this._disconnectProxy();
        this._onState?.('idle', 0, '');
    }

    // Listen-only: no method calls. The retry path on the lock screen
    // is the password prompt, not a camera rescan (see extension.js).
    _disconnectProxy() {
        if (this._proxy && this._signalId) {
            try { this._proxy.disconnectSignal(this._signalId); } catch { }
        }
        this._signalId = 0;
        this._proxy = null;
    }

    destroy() {
        this._disconnectProxy();
        if (this._watchId) {
            Gio.bus_unwatch_name(this._watchId);
            this._watchId = 0;
        }
        this._onState = null;
    }
}