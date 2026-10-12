/* Pure terminal-state filter for ScanState (master audit 5F).
 *
 * Two scans can overlap on the bus: a slow older scan finishing after
 * a newer one. Without filtering, a stale 'rejected' overwrites a
 * fresh 'matched' display or vice versa. A full scan takes seconds,
 * so an opposite terminal within TERMINAL_GUARD_MS of the last one is
 * near-certainly the stale loser, not a new attempt.
 *
 * Deliberately dependency-free (no Gio/Main) so it runs under plain
 * node for unit tests: shell-ext/tests/test-scan-guard.mjs.
 * Display-only: PAM decisions never flow through here, so a wrong
 * call drops or shows a stale animation frame at worst -- it can
 * never authenticate or deny anyone.
 *
 * Why recency, not sequence numbers: the daemon wire format carries
 * no per-scan id, and adding one would change the D-Bus protocol both
 * consumers (app, extension) parse. Revisit only if overlapping scans
 * from the same second ever become legitimate, which the 2 s TestScan
 * throttle plus multi-second scan times currently rule out.
 */

export const TERMINAL = ['matched', 'rejected', 'timeout', 'camera_error'];
export const TERMINAL_GUARD_MS = 3000;

/**
 * @param {string} state incoming ScanState
 * @param {number} nowMs Date.now()
 * @param {{state:string, at:number}|null} last result of the previous call
 * @returns {{show:boolean, last:{state:string,at:number}|null}}
 */
export function filterTerminal(state, nowMs, last) {
    if (TERMINAL.includes(state)) {
        if (last && last.state !== state && nowMs - last.at < TERMINAL_GUARD_MS)
            return { show: false, last };
        return { show: true, last: { state, at: nowMs } };
    }
    if (state === 'waking' || state === 'searching')
        return { show: true, last: null };
    return { show: true, last };
}
