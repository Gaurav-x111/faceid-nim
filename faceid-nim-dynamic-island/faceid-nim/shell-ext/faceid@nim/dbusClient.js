/* Subscribes to org.faceidnim.Daemon1's ScanState signal.
 *
 * This is the whole reason the extension survives GNOME upgrades: it
 * does not patch AuthPrompt._onShowMessage or any other private shell
 * internal, it just listens on the system bus. It also never receives
 * a camera frame, an embedding or a similarity score -- only a state
 * name, a progress number and a short reason string.
 *
 * `TestScan` and `Retry` are described here so the proxy knows the
 * interface, but only the signal is relied on for the animation. */

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
    <method name="PreviewAnimation"><arg type="s" direction="in"/></method>
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

    // Hover-to-retry: the daemon has no Retry method, so a hover
    // simply re-exercises the same camera path via TestScan.
    // Never throws out of the hover handler.
    retry() {
        return this.testScan().then(() => true, () => false);
    }

    testScan() {
        if (!this._proxy)
            return Promise.resolve([false, 'daemon unreachable']);
        try {
            return this._proxy.TestScan().catch(e => {
                logError(e, 'faceid@nim: TestScan');
                return [false, String(e)];
            });
        } catch (e) {
            logError(e, 'faceid@nim: TestScan');
            return Promise.resolve([false, String(e)]);
        }
    }

    // Developer-only preview: asks the daemon to play the full success
    // sequence (scan -> ring -> check -> Verified -> Welcome -> name)
    // with no camera and no auth. Signals-only, so it can never unlock
    // anything; used to tune the animation without standing at the screen.
    preview(userId = '') {
        if (!this._proxy)
            return Promise.resolve(false);
        try {
            return this._proxy.PreviewAnimation(
                String(userId).slice(0, 24)).then(() => true, () => false);
        } catch (e) {
            logError(e, 'faceid@nim: PreviewAnimation');
            return Promise.resolve(false);
        }
    }

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