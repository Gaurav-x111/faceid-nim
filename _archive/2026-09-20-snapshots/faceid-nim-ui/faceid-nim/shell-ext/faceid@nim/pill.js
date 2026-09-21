/* pill.js -- the Dynamic-Island-style container.
 *
 * Lifecycle, and why it is shaped this way:
 *
 *   closed pill  a small capsule, barely there
 *        |       width + opacity ease, 340ms
 *   expanded     glyph + label, rays sweeping
 *        |
 *   matched      rays collapse into a checkmark
 *        |
 *   unlocked     checkmark becomes an opening padlock, pill contracts
 *        |
 *   gone
 *
 * The failure path forks at `matched` into a red shake and a retry
 * hint, then contracts the same way.
 *
 * One animation clock. Every frame, `_tick` advances a single phase
 * and hands it to the glyph. Two timers animating the same widget is
 * how you get a checkmark that finishes before its container has
 * stopped growing.
 */

import Clutter from 'gi://Clutter';
import GLib from 'gi://GLib';
import GObject from 'gi://GObject';
import St from 'gi://St';

import { FaceGlyph, State } from './faceGlyph.js';

const FRAME_MS = 16;              // ~60fps
const EXPAND_MS = 340;
const CONTRACT_MS = 260;
const MATCH_MS = 620;
const UNLOCK_MS = 720;
const FAIL_HOLD_MS = 1500;
const OK_HOLD_MS = 520;

const RAY_SPEED = 2.6;            // radians per second

const GLYPH_PX = 60;
const PILL_CLOSED_W = 52;
const PILL_OPEN_W = 268;

export const FaceIdPill = GObject.registerClass(
class FaceIdPill extends St.BoxLayout {
    _init() {
        super._init({
            style_class: 'faceid-pill',
            vertical: false,
            x_align: Clutter.ActorAlign.CENTER,
            y_align: Clutter.ActorAlign.CENTER,
            reactive: true,              // for hover-to-retry
            track_hover: true,
            visible: false,
            width: PILL_CLOSED_W,
        });

        this._state = State.IDLE;
        this._stateStart = 0;
        this._phase = 0;
        this._timer = 0;
        this._holdTimer = 0;
        this._retryCb = null;

        this.glyph = new FaceGlyph(GLYPH_PX);
        this.add_child(this.glyph);

        this._textBox = new St.BoxLayout({
            vertical: true,
            y_align: Clutter.ActorAlign.CENTER,
            style_class: 'faceid-textbox',
            opacity: 0,
        });
        this._title = new St.Label({ style_class: 'faceid-title', text: '' });
        this._subtitle = new St.Label({ style_class: 'faceid-subtitle', text: '' });
        this._textBox.add_child(this._title);
        this._textBox.add_child(this._subtitle);
        this.add_child(this._textBox);

        // Hover to retry, matching Glance's affordance. Only wired up
        // in the failed state so a stray hover cannot restart a scan
        // that is already running.
        this.connect('notify::hover', () => {
            if (this._state === State.FAILED && this.hover && this._retryCb)
                this._retryCb();
        });
    }

    setRetryCallback(cb) {
        this._retryCb = cb;
    }

    // ---- animation clock ----------------------------------------------
    _startClock() {
        if (this._timer)
            return;
        this._timer = GLib.timeout_add(GLib.PRIORITY_DEFAULT, FRAME_MS, () => {
            this._tick();
            return GLib.SOURCE_CONTINUE;
        });
    }

    _stopClock() {
        if (this._timer) {
            GLib.source_remove(this._timer);
            this._timer = 0;
        }
    }

    _clearHold() {
        if (this._holdTimer) {
            GLib.source_remove(this._holdTimer);
            this._holdTimer = 0;
        }
    }

    _elapsed() {
        return GLib.get_monotonic_time() / 1000 - this._stateStart;
    }

    _enter(state) {
        this._state = state;
        this._stateStart = GLib.get_monotonic_time() / 1000;
    }

    _tick() {
        // Rays only spin while we are actually looking. Freezing them
        // on success or failure is a surprisingly strong signal that
        // the decision has been made.
        if (this._state === State.SCANNING)
            this._phase = (this._phase + RAY_SPEED * (FRAME_MS / 1000)) % (Math.PI * 2);

        const e = this._elapsed();
        let t = 0;
        switch (this._state) {
        case State.SCANNING: t = 1; break;
        case State.MATCHED:  t = Math.min(1, e / MATCH_MS); break;
        case State.UNLOCKED: t = Math.min(1, e / UNLOCK_MS); break;
        case State.FAILED:   t = 1; break;
        }
        this.glyph.tick(this._phase, t, this._confidence ?? 0);
    }

    // ---- public states -------------------------------------------------
    showScanning(title, subtitle) {
        this._clearHold();
        this._title.text = title || 'Face ID';
        this._subtitle.text = subtitle || 'Looking for you\u2026';
        this.remove_style_class_name('faceid-pill-ok');
        this.remove_style_class_name('faceid-pill-fail');

        if (!this.visible) {
            // Closed capsule expands outward from its own centre.
            this.opacity = 0;
            this.width = PILL_CLOSED_W;
            this.scale_y = 0.72;
            this.set_pivot_point(0.5, 0.5);
            this.show();
            this.ease({
                opacity: 255,
                width: PILL_OPEN_W,
                scale_y: 1,
                duration: EXPAND_MS,
                mode: Clutter.AnimationMode.EASE_OUT_BACK,
            });
            this._textBox.ease({
                opacity: 255,
                duration: EXPAND_MS,
                delay: 90,
                mode: Clutter.AnimationMode.EASE_OUT_QUAD,
            });
        }

        this._enter(State.SCANNING);
        this.glyph.setState(State.SCANNING, 1);
        this._startClock();
    }

    setProgress(p, subtitle) {
        this._confidence = Math.max(0, Math.min(1, p));
        if (subtitle !== undefined && subtitle !== null && subtitle !== '')
            this._subtitle.text = subtitle;
    }

    showMatched(username) {
        this._clearHold();
        this._title.text = username ? `Hi, ${username}` : 'Face recognised';
        this._subtitle.text = 'Unlocking\u2026';
        this.add_style_class_name('faceid-pill-ok');
        this._enter(State.MATCHED);
        this.glyph.setState(State.MATCHED, 0);
        this._startClock();

        // Chain into the unlock animation once the checkmark lands.
        this._holdTimer = GLib.timeout_add(
            GLib.PRIORITY_DEFAULT, MATCH_MS + OK_HOLD_MS, () => {
                this._holdTimer = 0;
                this._showUnlocked();
                return GLib.SOURCE_REMOVE;
            });
    }

    _showUnlocked() {
        this._subtitle.text = 'Unlocked';
        this._enter(State.UNLOCKED);
        this.glyph.setState(State.UNLOCKED, 0);
        this._holdTimer = GLib.timeout_add(
            GLib.PRIORITY_DEFAULT, UNLOCK_MS + 260, () => {
                this._holdTimer = 0;
                this._contract();
                return GLib.SOURCE_REMOVE;
            });
    }

    showFailed(title, subtitle) {
        this._clearHold();
        this._title.text = title || 'Face not recognised';
        this._subtitle.text = subtitle || 'Hover to try again';
        this.add_style_class_name('faceid-pill-fail');
        this._enter(State.FAILED);
        this.glyph.setState(State.FAILED, 1);
        this._startClock();
        this._shake();
        this._holdTimer = GLib.timeout_add(GLib.PRIORITY_DEFAULT, FAIL_HOLD_MS, () => {
            this._holdTimer = 0;
            this._contract();
            return GLib.SOURCE_REMOVE;
        });
    }

    /* Short, tight, decaying. A long shake reads as a bug rather than
     * a rejection. */
    _shake() {
        const base = this.translation_x;
        const steps = [10, -8, 6, -4, 2, 0];
        let i = 0;
        const next = () => {
            if (i >= steps.length) {
                this.translation_x = base;
                return;
            }
            const dx = steps[i++];
            this.ease({
                translation_x: base + dx,
                duration: 52,
                mode: Clutter.AnimationMode.EASE_IN_OUT_QUAD,
                onComplete: next,
            });
        };
        next();
    }

    _contract() {
        this._textBox.ease({
            opacity: 0,
            duration: 140,
            mode: Clutter.AnimationMode.EASE_IN_QUAD,
        });
        this.ease({
            opacity: 0,
            width: PILL_CLOSED_W,
            scale_y: 0.72,
            duration: CONTRACT_MS,
            mode: Clutter.AnimationMode.EASE_IN_QUAD,
            onComplete: () => this.reset(),
        });
    }

    reset() {
        this._clearHold();
        this._stopClock();
        this.remove_all_transitions();
        this._textBox.remove_all_transitions();
        this.remove_style_class_name('faceid-pill-ok');
        this.remove_style_class_name('faceid-pill-fail');
        this._state = State.IDLE;
        this._confidence = 0;
        this.glyph.setState(State.IDLE, 0);
        this._title.text = '';
        this._subtitle.text = '';
        this._textBox.opacity = 0;
        this.translation_x = 0;
        this.width = PILL_CLOSED_W;
        this.scale_y = 1;
        this.opacity = 255;
        this.hide();
    }

    destroy() {
        this._clearHold();
        this._stopClock();
        this._retryCb = null;
        super.destroy();
    }
});
