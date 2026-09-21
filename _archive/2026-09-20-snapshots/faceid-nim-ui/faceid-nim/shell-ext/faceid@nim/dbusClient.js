/* Subscribes to org.faceidnim.Daemon1's ScanState signal.
 *
 * This is the whole reason the extension survives GNOME upgrades: it
 * does not patch AuthPrompt._onShowMessage or any other private shell
 * internal, it just listens on the system bus. It also never receives
 * a camera frame, an embedding or a similarity score -- only a state
 * name, a progress number and a short reason string. */

import Gio from 'gi://Gio';

const IFACE = `
<node>
  <interface name="org.faceidnim.Daemon1">
    <signal name="ScanState">
      <arg type="s" name="state"/>
      <arg type="d" name="progress"/>
      <arg type="s" name="reason"/>
    </signal>
    <method name="Diagnostics"><arg type="s" direction="out"/></method>
    <method name="TestScan">
      <arg type="b" direction="out"/>
      <arg type="s" direction="out"/>
    </method>
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

    _disconnectProxy() {
        if (this._proxy && this._signalId) {
            try { this._proxy.disconnectSignal(this._signalId); } catch { }
        }
        this._signalId = 0;
        this._proxy = null;
    }

    /* Ask the daemon to run another scan. Used by hover-to-retry.
     * Fire and forget: a failed retry must never throw into the shell,
     * and the daemon's own rate limiting is what actually governs
     * whether a new scan is allowed. */
    retry() {
        if (!this._proxy)
            return;
        try {
            this._proxy.call('TestScan', null, Gio.DBusCallFlags.NONE, 8000,
                             null, null);
        } catch (e) {
            logError(e, 'faceid@nim: retry failed');
        }
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
