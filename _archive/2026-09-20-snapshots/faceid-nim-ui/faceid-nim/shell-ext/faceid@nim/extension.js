/* faceid@nim -- lock-screen Face ID pill.
 *
 * Attaches the pill to the unlock dialog's prompt box and drives it
 * from the daemon's ScanState signal.
 *
 * Why not hook AuthPrompt._onShowMessage (as the earliest prototype
 * did): that is a private GNOME internal whose name and signature
 * change between releases, and it only carries whatever text PAM
 * happens to emit. Listening on our own D-Bus signal gives real
 * states and survives shell upgrades.
 *
 * `session-modes` in metadata.json must include "unlock-dialog" or
 * GNOME disables the extension the moment the screen locks.
 */

import GLib from 'gi://GLib';
import { Extension } from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';

import { DaemonClient } from './dbusClient.js';
import { FaceIdPill } from './pill.js';

export default class FaceIdExtension extends Extension {
    enable() {
        this._pill = new FaceIdPill();
        this._client = new DaemonClient();
        this._attachedTo = null;
        this._lastState = 'idle';

        this._pill.setRetryCallback(() => this._client?.retry?.());

        this._lockId = Main.screenShield?.connect?.('locked-changed',
            () => this._reattach()) ?? 0;
        this._client.connect((state, progress, reason) =>
            this._onState(state, progress, reason));
        this._reattach();
    }

    disable() {
        // Must fully tear down: an extension left holding actors across
        // a lock/unlock cycle leaks them into the unlock dialog.
        if (this._lockId && Main.screenShield) {
            Main.screenShield.disconnect(this._lockId);
            this._lockId = 0;
        }
        if (this._reattachId) {
            GLib.source_remove(this._reattachId);
            this._reattachId = 0;
        }
        this._client?.destroy();
        this._client = null;
        this._detach();
        this._pill?.destroy();
        this._pill = null;
    }

    _promptBox() {
        // The unlock dialog is recreated on every lock, so this is
        // looked up fresh rather than cached across sessions.
        const dialog = Main.screenShield?._dialog;
        return dialog?._authPrompt?._mainBox ?? dialog?._authPrompt ?? null;
    }

    _detach() {
        if (this._attachedTo && this._pill?.get_parent() === this._attachedTo)
            this._attachedTo.remove_child(this._pill);
        this._attachedTo = null;
    }

    _reattach() {
        this._detach();
        const box = this._promptBox();
        if (!box || !this._pill)
            return;
        this._pill.reset();
        box.insert_child_at_index(this._pill, 0);   // above the password field
        this._attachedTo = box;
    }

    _userName() {
        // Purely cosmetic ("Hi, Zang"). Never used for any decision.
        try {
            return GLib.get_real_name()?.split(' ')[0] || GLib.get_user_name();
        } catch {
            return null;
        }
    }

    _onState(state, progress, reason) {
        if (!this._pill)
            return;
        if (!this._attachedTo)
            this._reattach();
        this._lastState = state;

        switch (state) {
        case 'waking':
            this._pill.showScanning('Face ID', 'Waking the camera\u2026');
            break;
        case 'searching':
            this._pill.showScanning('Face ID', reason || 'Looking for you\u2026');
            break;
        case 'verifying':
            this._pill.showScanning('Face ID', reason || 'Hold still\u2026');
            this._pill.setProgress(progress);
            break;
        case 'matched':
            this._pill.showMatched(this._userName());
            break;
        case 'rejected':
            this._pill.showFailed('Face not recognised', 'Hover to try again');
            break;
        case 'timeout':
            this._pill.showFailed('Timed out', 'Use your password');
            break;
        case 'camera_error':
            this._pill.showFailed('Camera unavailable', 'Use your password');
            break;
        default:
            this._pill.reset();
        }
    }
}
