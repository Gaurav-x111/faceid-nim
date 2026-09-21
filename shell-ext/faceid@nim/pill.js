/* The pill: a Dynamic-Island style capsule that expands from 52px to
 * 268px around the FaceGlyph and a status line, all driven by a single
 * GLib.timeout_add(16ms) clock.
 *
 * Nothing in the pill uses Clutter easing for the sequenced parts of
 * the animation. One tick advances an elapsed timer; a phase machine
 * decides what a tick means, and the glyph is handed a phase and a
 * normalized `t` every tick. That is what guarantees the checkmark can
 * never finish before the container has stopped growing -- the glyph
 * does not own a timer to race.
 *
 * Opening animation section
 * -------------------------
 * When a scan starts the pill runs an *opening* phase first: the
 * active opening variant (app logo splash, glow, 'none' to skip, or a
 * user-made variant) plays while the capsule grows, then hands off to
 * the scanning glyph. Everything about the splash is delegated to
 * OpeningScene (./opening.js), which reads the user's choice from
 * ~/.config/faceid-nim/openings.json and renders declarative specs
 * only -- never code shipped by anyone else. The welcome phase once a
 * match lands shows "Welcome <uid>" with a pop-in before the capsule
 * contracts. */

import Clutter from 'gi://Clutter';
import GLib from 'gi://GLib';
import GObject from 'gi://GObject';
import St from 'gi://St';

import { FaceGlyph } from './faceGlyph.js';
import { OpeningScene } from './opening.js';

const TICK_MS = 16;

const CLOSED_WIDTH = 52;
const EXPANDED_WIDTH = 268;
const GLYPH_SIZE = 52;

const EXPAND_MS = 340;      // pill grows 52 -> 268, overshooting
const SWEEP_MS = 2000;      // one full ray sweep while scanning
const MATCHED_MS = 620;     // rays collapse + checkmark draws
const UNLOCK_MS = 720;      // checkmark out, padlock in, shackle opens
const HOLD_MS = 300;        // brief beat on the unlocked padlock
const WELCOME_MS = 1200;    // "Welcome <uid>" pop-in + hold
const REJECT_MS = 1500;     // shake + "Hover to try again"
const CONTRACT_MS = 260;    // shrink back to the closed capsule

const clamp01 = x => (x < 0 ? 0 : x > 1 ? 1 : x);
const easeOutCubic = t => 1 - Math.pow(1 - t, 3);
// The pill expands with a spring (slight overshoot past 268px) and
// contracts with a plain ease-out. Growth needs the settle, a shrink
// reads best as a drop.
const easeOutBack = t => {
    const c1 = 1.70158, c3 = c1 + 1;
    const x = clamp01(t) - 1;
    return 1 + c3 * x * x * x + c1 * x * x;
};

export const FaceIdPill = GObject.registerClass(
class FaceIdPill extends St.BoxLayout {
    _init() {
        super._init({
            style_class: 'faceid-pill',
            vertical: false,
            x_align: Clutter.ActorAlign.CENTER,
            reactive: true,     // hover-to-retry and the hover tint
            visible: false,
        });

        this._glyph = new FaceGlyph();
        this._glyph.width = GLYPH_SIZE;
        this._glyph.height = GLYPH_SIZE;
        this.add_child(this._glyph);

        // The opening splash is owned by OpeningScene; the pill only
        // hosts its logo widget so the capsule, glyph and splash share
        // one clipping box.
        this._opening = new OpeningScene();
        this.insert_child_at_index(this._opening.widget, 0);

        this._textBox = new St.BoxLayout({
            vertical: true,
            y_align: Clutter.ActorAlign.CENTER,
            x_expand: true,
        });
        this._label = new St.Label({ style_class: 'faceid-label',
                                     y_align: Clutter.ActorAlign.CENTER });
        this._detail = new St.Label({ style_class: 'faceid-detail',
                                      y_align: Clutter.ActorAlign.CENTER,
                                      text: '' });
        this._textBox.add_child(this._label);
        this._textBox.add_child(this._detail);
        this.add_child(this._textBox);

        this._state = 'idle';
        this._elapsed = 0;
        this._startMs = 0;
        this._progress = 0;
        this._rejectSweep = 0;
        this._reason = '';
        this._ticker = 0;
        this._retryHandler = null;

        this.connect('enter-event', () => this._onEnter());
        this.connect('leave-event', () => this._onLeave());

        this.set_width(CLOSED_WIDTH);
        this.set_height(GLYPH_SIZE - 4);
    }

    // ---- public API ---------------------------------------------------

    startScanning(reason = null) {
        this._reason = reason || '';
        this.translation_x = 0;     // clear any shake from a rejection
        this._show();
        if (this._state === 'opening' || this._state === 'scanning' ||
            this._state === 'verifying')
            return;
        this._setClasses(false);
        // Opening animation: reload the active variant (the app's
        // choice applies to the very next scan), then either play the
        // splash or skip straight to the glyph.
        this._opening.load();
        if (this._opening.isNone()) {
            this._enter('scanning');
            this._setLabel('Looking for you…', this._reason || '');
            this._glyph.visible = true;
            return;
        }
        this._enter('opening');
        this._setLabel('face-id', 'Opening camera…');
        this._glyph.visible = false;
        this._opening.enter(this);
    }

    setProgress(p) {
        this._progress = clamp01(Number(p) || 0);
        this._show();
        if (this._state === 'matched' || this._state === 'unlock' ||
            this._state === 'welcome' || this._state === 'rejected')
            return;
        this._enter('verifying');
        this._opening.handoff(this);    // splash gone, glyph takes over
        this._glyph.visible = true;
        this._setLabel('Verifying…', this._reason || 'Hold still');
    }

    showMatched(detail = null) {
        this._show();
        this._enter('matched');
        this._setClasses(true);
        this._setLabel('Welcome', detail || '');
    }

    showRejected(reason = null) {
        this._rejectSweep = (GLib.get_monotonic_time() / 1000) % SWEEP_MS / SWEEP_MS;
        this._reason = reason || '';
        this._show();
        this._enter('rejected');
        this._setClasses(false);
        this._setLabel('Hover to try again', this._reason);
    }

    reset() {
        this._enter('idle');
        this.remove_style_class_name('faceid-ok');
        this.remove_style_class_name('faceid-fail');
        this.remove_style_class_name('faceid-pill-hover');
        this._label.remove_style_class_name('faceid-welcome');
        this._detail.remove_style_class_name('faceid-uid');
        this._label.scale_x = 1;
        this._label.scale_y = 1;
        this._setLabel('', '');
        this.translation_x = 0;
        this.opacity = 255;
        this.set_width(CLOSED_WIDTH);
        this._opening.reset(this);
        this._glyph.visible = true;
        this._glyph.animate({ phase: 'rays', scanT: 0, t: 0,
                              progress: 0, ramp: 0, failed: false });
        this.hide();
        this._stopClock();
    }

    setRetryHandler(fn) {
        this._retryHandler = fn;
    }

    destroy() {
        this._stopClock();
        this._retryHandler = null;
        super.destroy();
    }

    // ---- internals ----------------------------------------------------

    _enter(state) {
        this._state = state;
        this._startMs = this._elapsed;
    }

    _show() {
        if (this._state === 'idle' || !this.visible) {
            this._elapsed = 0;
            this._startMs = 0;
        }
        this._progress = this._state === 'verifying' ? this._progress : 0;
        if (!this.visible) {
            this.visible = true;
            this.opacity = 255;
        }
        this._startClock();
    }

    _setClasses(ok) {
        this.remove_style_class_name('faceid-ok');
        this.remove_style_class_name('faceid-fail');
        this.add_style_class_name(ok ? 'faceid-ok' : 'faceid-fail');
    }

    _setLabel(primary, detail) {
        this._label.text = primary;
        this._detail.text = detail;
        this._detail.visible = Boolean(detail);
    }

    _onEnter() {
        this.add_style_class_name('faceid-pill-hover');
        if (this._state === 'rejected') {
            // Hover-to-retry: hand back to the daemon and rescan.
            if (this._retryHandler)
                this._retryHandler();
            this.startScanning(this._reason || null);
        }
    }

    _onLeave() {
        this.remove_style_class_name('faceid-pill-hover');
    }

    // ---- the one clock -------------------------------------------------

    _startClock() {
        if (this._ticker)
            return;
        this._ticker = GLib.timeout_add(GLib.PRIORITY_DEFAULT, TICK_MS, () => {
            try {
                this._tick();
            } catch (e) {
                logError(e, 'faceid@nim: pill tick');
            }
            return GLib.SOURCE_CONTINUE;
        });
    }

    _stopClock() {
        if (this._ticker) {
            GLib.source_remove(this._ticker);
            this._ticker = 0;
        }
    }

    _tick() {
        this._elapsed += TICK_MS;

        switch (this._state) {
        case 'opening':
        case 'scanning':
        case 'verifying': {
            const tt = (this._elapsed - this._startMs) / EXPAND_MS;
            const grow = easeOutBack(clamp01(tt));
            this.set_width(CLOSED_WIDTH +
                Math.max(0, (EXPANDED_WIDTH - CLOSED_WIDTH) * grow));

            if (this._state === 'opening') {
                // Splash: fade the active variant in while the capsule
                // grows, and hand off to the glyph once both the pill
                // has settled and the splash has finished.
                const sinceMs = this._elapsed - this._startMs;
                this._opening.tick(sinceMs);
                if (grow >= 1 && sinceMs >= this._opening.duration()) {
                    this._state = 'scanning';
                    this._opening.handoff(this);
                    this._glyph.visible = true;
                    this._setLabel('Looking for you…', this._reason || '');
                }
            }

            const scanT = (this._elapsed % SWEEP_MS) / SWEEP_MS;
            this._glyph.animate({
                phase: 'rays', scanT,
                t: 0,
                progress: this._state === 'verifying' ? this._progress : 0,
                ramp: clamp01(tt),
                failed: false,
            });
            break;
        }

        case 'matched': {
            const tt = clamp01((this._elapsed - this._startMs) / MATCHED_MS);
            this._glyph.animate({ phase: 'matched', scanT: 0, t: tt,
                                  progress: 1, ramp: 1, failed: false });
            if (tt >= 1)
                this._enter('unlock');
            break;
        }

        case 'unlock': {
            const tt = clamp01((this._elapsed - this._startMs) / UNLOCK_MS);
            this._glyph.animate({ phase: 'unlock', scanT: 0, t: tt,
                                  progress: 1, ramp: 1, failed: false });
            if (tt >= 1) {
                this._state = 'welcome';
                this._startMs = this._elapsed;
                // The daemon puts the winning identity's name in
                // `this._reason`, so we can greet them by their id.
                this._setLabel('Welcome', this._reason);
                this._label.add_style_class_name('faceid-welcome');
                this._detail.add_style_class_name('faceid-uid');
            }
            break;
        }

        case 'welcome': {
            const tt = clamp01((this._elapsed - this._startMs) / WELCOME_MS);
            this._glyph.animate({ phase: 'unlock', scanT: 0, t: 1,
                                  progress: 1, ramp: 1, failed: false });
            // Pop-in: the greeting springs out to full size.
            const s = 0.6 + 0.4 * easeOutBack(tt);
            this._label.scale_x = s;
            this._label.scale_y = s;
            if (tt >= 1) {
                this._label.scale_x = 1;
                this._label.scale_y = 1;
                this._state = 'contracting';
                this._startMs = this._elapsed + HOLD_MS;
            }
            break;
        }

        case 'rejected': {
            const tt = clamp01((this._elapsed - this._startMs) / REJECT_MS);
            const decay = 1 - tt;
            this.translation_x = 8 * decay * decay * Math.sin(tt * Math.PI * 5);
            this._glyph.animate({ phase: 'rejected', scanT: this._rejectSweep,
                                  t: 0, progress: 0, ramp: 1, failed: true });
            if (tt >= 1)
                this._enter('contracting');
            break;
        }

        case 'contracting': {
            const tt = clamp01((this._elapsed - this._startMs) / CONTRACT_MS);
            this.set_width(EXPANDED_WIDTH -
                (EXPANDED_WIDTH - CLOSED_WIDTH) * easeOutCubic(tt));
            this.opacity = Math.round(255 * (1 - tt));
            if (tt >= 1)
                this.reset();
            break;
        }

        default:
            this._stopClock();
        }
    }
});