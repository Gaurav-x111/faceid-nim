/* faceid@nim -- lock-screen scan pill.
 *
 * Attaches the pill to the unlock dialog's prompt box and drives it
 * from the daemon's ScanState signal.
 *
 * Why not hook AuthPrompt._onShowMessage (as the earlier prototype
 * did): that is a private GNOME internal whose name and signature
 * change between releases, and it only carries whatever text PAM
 * happens to emit. Listening on our own D-Bus signal gives real states
 * and survives shell upgrades.
 *
 * `session-modes` in metadata.json must include "unlock-dialog" or
 * GNOME disables the extension the moment the screen locks.
 *
 * The one fragile seam left is _promptBox(): it walks into
 * Main.screenShield._dialog._authPrompt._mainBox, which is a private
 * path. Verify it on your GNOME version in a nested shell before
 * trusting the demo (see the README checklist). */

import { Extension } from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';

import { DaemonClient } from './dbusClient.js';
import { FaceIdPill } from './pill.js';

export default class FaceIdExtension extends Extension {
    enable() {
        this._pill = new FaceIdPill();
        this._client = new DaemonClient();
        this._attachedTo = null;
        this._lockId = Main.screenShield?.connect?.('locked-changed',
            () => this._reattach()) ?? 0;

        // Hover-to-retry on a rejected scan hands back to the daemon.
        this._pill.setRetryHandler(() => this._client?.retry());
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
        this._client?.destroy();
        this._client = null;
        this._detach();
        this._pill?.destroy();
        this._pill = null;
    }

    _promptBox() {
        // The unlock dialog is recreated on every lock, so this is
        // looked up fresh rather than cached across sessions.
        // Try multiple paths for different GNOME versions.
        const dialog = Main.screenShield?._dialog;
        if (!dialog)
            return null;

        // GNOME 45/46: Main.screenShield._dialog._authPrompt._mainBox
        if (dialog._authPrompt?._mainBox)
            return dialog._authPrompt._mainBox;

        // GNOME 46+: Main.screenShield._dialog._authPrompt._contentBox or similar
        if (dialog._authPrompt?._contentBox)
            return dialog._authPrompt._contentBox;

        // Some versions might have the auth prompt directly as _authDialog
        if (dialog._authDialog?._mainBox)
            return dialog._authDialog._mainBox;

        // Fallback: try to find any box that looks like an auth prompt
        // by checking for children that are St.Entry (password entry)
        const authPrompt = dialog._authPrompt || dialog._authDialog;
        if (authPrompt) {
            // Walk children to find a box with an entry
            const findBoxWithEntry = (actor) => {
                if (!actor || !actor.get_children)
                    return null;
                const children = actor.get_children();
                for (const child of children) {
                    if (child.constructor.name === 'St.Entry' || 
                        (child.constructor.name && child.constructor.name.includes('Entry'))) {
                        return actor;
                    }
                    const found = findBoxWithEntry(child);
                    if (found)
                        return found;
                }
                return null;
            };
            const found = findBoxWithEntry(authPrompt);
            if (found)
                return found;
        }

        return authPrompt ?? dialog ?? null;
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
        // Index 0 keeps the pill above the password entry. If your
        // unlock dialog puts the user avatar at the top of that box,
        // bump the index past it.
        box.insert_child_at_index(this._pill, 0);
        this._attachedTo = box;
    }

    _onState(state, progress, reason) {
        if (!this._pill)
            return;
        if (!this._attachedTo)
            this._reattach();

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