/* faceid@nim -- lock-screen scan pill.
 *
 * Drives the Dynamic-Island pill from the daemon's ScanState signal.
 *
 * The pill lives in Main.uiGroup top chrome -- NOT inside the unlock
 * dialog. The screen shield destroys the unlock dialog the moment
 * authentication succeeds, so any success animation attached to it is
 * torn down before a frame plays (that was the "no animation after
 * verifying" bug). Being top chrome, the pill floats above the shield
 * and its curtain, and the whole success sequence -- final sweep,
 * checkmark, ✓ Verified, Welcome + identity -- plays out even while
 * the dialog slides away behind it.
 *
 * `session-modes` in metadata.json must include "unlock-dialog" or
 * GNOME disables the extension the moment the screen locks. */

import { Extension } from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';

import { DaemonClient } from './dbusClient.js';
import { FaceIdPill } from './pill.js';

export default class FaceIdExtension extends Extension {
    enable() {
        this._pill = new FaceIdPill();
        this._client = new DaemonClient();
        this._lockId = Main.screenShield?.connect?.('locked-changed',
            (_, locked) => this._onLocked(locked)) ?? 0;

        // Hover-to-retry on a rejected scan hands back to the daemon.
        this._pill.setRetryHandler(() => this._client?.retry());
        this._client.connect((state, progress, reason) =>
            this._onState(state, progress, reason));

        // Above the screen shield (and the curtain it raises when it
        // locks), horizontally centred. The accessibility input region
        // shrinks to nothing while the pill is hidden.
        Main.layoutManager.addTopChrome(this._pill, { affectsInputRegion: true });
    }

    disable() {
        // Must fully tear down or the actor leaks into the shell.
        if (this._lockId && Main.screenShield) {
            Main.screenShield.disconnect(this._lockId);
            this._lockId = 0;
        }
        this._client?.destroy();
        this._client = null;
        if (this._pill) {
            Main.layoutManager?.removeChrome?.(this._pill);
            this._pill.destroy();
            this._pill = null;
        }
    }

    _onLocked(locked) {
        if (!this._pill || !locked)
            return;
        // A fresh lock: drop any stale overlay and sit back on top of
        // the shield's curtain (the shield raises its lightbox above
        // everything when locking, and locked-changed fires afterwards).
        this._pill.reset();
        Main.uiGroup?.set_child_above_sibling?.(this._pill, null);
    }

    _onState(state, progress, reason) {
        try { log(`faceid@nim: ScanState ${state} ${progress} ${(reason || '').slice(0, 24)} visible=${this._pill?.visible} state=${this._pill?._state}`); } catch (e) {}
        if (!this._pill)
            return;

        switch (state) {
        case 'waking':
        case 'searching':
            this._pill.startScanning(reason || null);
            break;
        case 'verifying':
            this._pill.setProgress(progress);
            break;
        case 'matched':
            // The daemon puts the winning identity's name in `reason`,
            // so the pill can greet the user by the id they chose.
            this._pill.showMatched(reason || null);
            break;
        case 'rejected':
            this._pill.showRejected('Face not recognised');
            break;
        case 'timeout':
            this._pill.showRejected('Timed out');
            break;
        case 'camera_error':
            this._pill.showRejected('Camera unavailable');
            break;
        default:
            this._pill.reset();
        }
    }
}