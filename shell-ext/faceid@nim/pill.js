/* The pill: a Dynamic-Island style capsule that expands from the 2.5cm
 * scan-square into a wide capsule around the FaceGlyph and a status
 * line, all driven by a single GLib.timeout_add(16ms) clock, positioned
 * at the TOP CENTRE of the primary monitor -- never inside the unlock
 * dialog.
 *
 * Why not the dialog: the screen shield destroys the unlock dialog the
 * moment authentication succeeds, so any success animation attached to
 * it is torn down before a single frame can play. That was the "no
 * animation after verifying" bug. This pill lives in Main.uiGroup top
 * chrome instead -- above the shield and its lightbox curtain -- so the
 * whole success sequence keeps playing while the dialog slides away
 * behind it.
 *
 * Nothing in the pill uses Clutter easing for the sequenced parts of
 * the animation. One tick advances an elapsed timer; a phase machine
 * decides what a tick means, and the glyph is handed a phase and a
 * normalized `t` every tick. That is what guarantees the checkmark can
 * never finish before the ring has completed -- the glyph does not own
 * a timer to race.
 *
 * ---- the UI state machine -----------------------------------------
 * Exported phases map onto the spec's AuthAnimationState exactly:
 *
 *   spec state     pill phase        what plays
 *   -----------    ------------      --------------------------------------
 *   idle           idle              nothing; capsule hidden
 *   verifying      opening /         splash then the ring + face scan
 *                 scanning /         ("Looking for you… / Verifying…"),
 *                 verifying          progress raises the ring floor
 *   success-ring   matched (0..0.3)  the crest accelerates to complete
 *                                    one clean rotation, then resolves
 *   success-check  matched           ring collapses inward as the check
 *                                    draws itself across the face
 *   pill-expand    matched           capsule springs to full width
 *   pill-settle    matched (>0.46)   "Verified" springs in, green tint,
 *                                    one subtle overshoot, then settles
 *   welcome        welcome           "Welcome" rises in
 *   identity       welcome (detail)  "<identity name>" slides in after
 *   complete       contracting       capsule contracts and fades away
 *   failure        rejected /        the ring freezes + cross, the pill
 *                  timeout /         shakes -- never the success colours
 *                  camera_error
 *
 * The overlap follows the spec timeline: the check starts while the
 * ring is still resolving, "Verified" pops as the capsule settles, and
 * "Welcome" + identity ride the tail of the hold -- nothing waits for
 * anything, no `await sleep()` chains, one elapsed clock per animation
 * (this._elapsed), one GLib clock per pill.
 *
 * Opening animation section
 * -------------------------
 * When a scan starts the pill runs an *opening* phase first: the
 * active opening variant (app logo splash, glow, 'none' to skip, or a
 * user-made variant) plays while the capsule grows and drops in from
 * above the top edge, then hands off to the scanning glyph. Everything
 * about the splash is delegated to OpeningScene (./opening.js), which
 * reads the user's choice from ~/.config/faceid-nim/openings.json and
 * renders declarative specs only -- never code shipped by anyone else.
 *
 * Success sequence
 * ----------------
 *   verifying  the ring scans, "Verifying… / Hold still"
 *   matched    the crest accelerates to complete one clean rotation,
 *              the circle brightens and collapses, the checkmark draws
 *              itself, the capsule springs and tints, label -> Verified
 *   welcome    "Welcome" fades/slides in, then the identity below it
 *   contracting the capsule contracts and fades, leaving no trace
 *
 * Since matched carries the winning identity's name, the very same
 * sequence plays at a screen unlock, at the GDM login screen and in the
 * middle of a desktop session when `sudo` asks for a face -- the pill
 * listens on the system bus, it has no idea (and no need to know) which
 * service asked.
 *
 * What it *can* tell is the shell's own state: captured at scan start,
 * `_loginContext` is true when the screen is locked or the shell is in
 * the gdm/login session mode. That decides the final act:
 *   - laptop-open (locked screen / GDM): "Verified" -> "Welcome <uid>"
 *   - sudo / polkit / test scan (unlocked session): "Verified" only,
 *     no personal greeting.
 *
 * Reduced motion: org.gnome.desktop.interface enable-animations scales
 * every phase to ~12% so the success still shows, without the sweep,
 * springs or motion. */

import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import GObject from 'gi://GObject';
import Meta from 'gi://Meta';
import St from 'gi://St';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';

import { FaceGlyph } from './faceGlyph.js';
import { OpeningScene } from './opening.js';

const TICK_MS = 16;

// Physical sizing: the scan face is a 2.5cm x 2.5cm square on screen --
// the spec's --hud-size. Convert 25mm to logical pixels once, from the
// primary monitor's physical size. Fall back to ~96dpi when Meta can't
// report the physical width.
function glyphSizePx() {
    try {
        const mon = Meta.MonitorManager.get().get_primary_monitor();
        const phys = mon.get_physical_width();   // mm
        if (phys > 0) {
            const gm = Main.layoutManager.primaryMonitor;
            if (gm && gm.geometry && gm.geometry.width > 0) {
                const px = 25 * gm.geometry.width / phys;  // logical px for 25mm
                return Math.min(190, Math.max(72, Math.round(px)));
            }
        }
    } catch (e) { /* fall through to the classic dpi estimate */ }
    return 94;   // 2.5cm at the classic 96dpi
}

const GLYPH_SIZE = glyphSizePx();
const CLOSED_WIDTH = GLYPH_SIZE;            // closed capsule = the 2.5cm scan square
const EXPANDED_WIDTH = GLYPH_SIZE + 190;    // + room for the status text

const TOP_MARGIN = 10;      // resting gap between the monitor top and the capsule
const DROP_PX = 96;         // the capsule enters from this far above the screen edge

const EXPAND_MS = 340;      // pill grows 52 -> 268 while dropping into place
const SWEEP_MS = 2000;      // one full ray sweep while scanning
const MATCHED_MS = 700;     // final sweep -> collapse -> checkmark
const RING_RESOLVE_AT = 0.30;   // sweep finishes, the ring starts collapsing
const VERIFY_POP_AT = 0.46;     // pill tints green + "Verified" springs in
const WELCOME_MS = 2200;    // "Welcome <uid>" reveal + hold (slow, calm)
const REJECT_MS = 1500;     // shake + "Hover to try again"
const CONTRACT_MS = 260;    // shrink back to the closed capsule
const HOLD_MS = 300;        // brief beat before the capsule contracts

// Capsule width spring (spec §11): stiffness 420, damping 32, mass 1.
// Critical damping would be 2*sqrt(420) ~ 41, so 32 sits just under it:
// one subtle overshoot past 268px as the pill settles, then done.
const SPRING_K = 420;
const SPRING_D = 32;
const SPRING_M = 1;

const clamp01 = x => (x < 0 ? 0 : x > 1 ? 1 : x);
const easeOutCubic = t => 1 - Math.pow(1 - t, 3);
const easeOutQuint = t => 1 - Math.pow(1 - t, 5);
// The pill expands with a spring (slight overshoot past 268px) and
// contracts with a plain ease-out. Growth needs the settle, a shrink
// reads best as a drop.
const easeOutBack = t => {
    const c1 = 1.70158, c3 = c1 + 1;
    const x = clamp01(t) - 1;
    return 1 + c3 * x * x * x + c1 * x * x;
};

// A tiny spring integrator for the capsule width. dt here is ~16ms,
// well inside the stability limit for this stiffness, so the motion
// stays smooth and never explodes.
class Spring {
    constructor(stiffness, damping, mass) {
        this.k = stiffness;
        this.d = damping;
        this.m = mass;
        this.x = 0;
        this.v = 0;
    }

    set(x, v = 0) {
        this.x = x;
        this.v = v;
    }

    step(dt, target) {
        const a = (-this.k * (this.x - target) - this.d * this.v) / this.m;
        this.v += a * dt;
        this.x += this.v * dt;
        return this.x;
    }
}

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

        this._a11y = new Gio.Settings({ schema_id: 'org.gnome.desktop.interface' });
        this._reduced = !this._a11y.get_boolean('enable-animations');
        this._a11yChangedId = this._a11y.connect('changed::enable-animations',
            s => this._reduced = !s.get_boolean('enable-animations'));

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
        this._dropStart = 0;
        this._progress = 0;
        this._rejectSweep = 0;
        this._reason = '';
        this._lastScanT = 0;
        this._widthTarget = CLOSED_WIDTH;
        this._verifiedPop = false;
        this._ticker = 0;
        this._retryHandler = null;
        this._spring = new Spring(SPRING_K, SPRING_D, SPRING_M);
        this._spring.set(CLOSED_WIDTH, 0);
        this._loginContext = false;

        this.connect('enter-event', () => this._onEnter());
        this.connect('leave-event', () => this._onLeave());

        this.set_width(CLOSED_WIDTH);
        this.set_height(GLYPH_SIZE - 4);
    }

    // ---- public API ---------------------------------------------------

    startScanning(reason = null) {
        // "Opening the laptop" -- the greeting only belongs to login and
        // lock-screen unlocks, captured *now* because the screen shield
        // drops mid-success and the check must not race it. A sudo/polkit
        // prompt in an unlocked session stays on "Verified".
        this._loginContext = this._detectLoginContext();
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
        this._glyph.setScanFace(this._opening.scanFace());
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
        if (this._state === 'matched' || this._state === 'welcome' ||
            this._state === 'rejected')
            return;
        this._enter('verifying');
        this._opening.handoff(this);    // splash gone, glyph takes over
        this._glyph.visible = true;
        this._setLabel('Verifying…', this._reason || 'Hold still');
    }

    showMatched(detail = null) {
        this._reason = detail || this._reason || '';
        this._show();
        this._enter('matched');
        this._verifiedPop = false;
        this._opening.handoff(this);
        this._glyph.visible = true;
        // The ring's final rotation runs against the scanning copy;
        // the pill flips to "Verified" once the checkmark starts. The
        // identity stays hidden until the welcome reveal.
        this._setLabel('Verifying…', 'Hold still');
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
        this._label.opacity = 255;
        this._label.translation_y = 0;
        this._detail.opacity = 255;
        this._detail.translation_y = 0;
        this._verifiedPop = false;
        this._setLabel('', '');
        this._widthTarget = CLOSED_WIDTH;
        this._spring.set(CLOSED_WIDTH, 0);
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
        if (this._a11yChangedId) {
            this._a11y.disconnect(this._a11yChangedId);
            this._a11yChangedId = 0;
        }
        this._a11y = null;
        this._stopClock();
        this._retryHandler = null;
        super.destroy();
    }

    // ---- internals ----------------------------------------------------

    _ms(n) {
        // Reduced-motion: skip the choreography, keep the outcome.
        return this._reduced ? Math.max(1, Math.round(n * 0.12)) : n;
    }

    _enter(state) {
        this._state = state;
        this._startMs = this._elapsed;
    }

    _show() {
        if (this._state === 'idle' || !this.visible) {
            this._elapsed = 0;
            this._startMs = 0;
            this._dropStart = 0;
        }
        this._progress = this._state === 'verifying' ? this._progress : 0;
        if (!this.visible) {
            this.visible = true;
            this.opacity = 255;
            this._spring.set(CLOSED_WIDTH, 0);
        }
        this._place();
        this._startClock();
    }

    _setWidth(w) {
        this._widthTarget = w;
        this.set_width(w);
    }

    // Keep the capsule pinned to the top centre of the primary monitor.
    // The drop-from-above is folded into `y` via an ease-out so the
    // capsule appears to descend from the top edge, Dynamic-Island style.
    _place() {
        const mon = Main.layoutManager.primaryMonitor;
        if (!mon)
            return;
        const w = this._widthTarget;
        const dropT = clamp01((this._elapsed - this._dropStart) / this._ms(EXPAND_MS));
        this.x = Math.round(mon.x + (mon.width - w) / 2);
        this.y = Math.round(mon.y + TOP_MARGIN - DROP_PX * (1 - easeOutQuint(dropT)));
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

    _detectLoginContext() {
        // True when the request arrives with the screen locked or at the
        // GDM login screen -- i.e. the user is opening the laptop. False
        // for sudo/polkit prompts asked from an unlocked session.
        try {
            if (Main.sessionMode && Main.sessionMode.currentMode !== 'user')
                return true;
        } catch (e) { }
        try {
            if (Main.screenShield && Main.screenShield.locked)
                return true;
        } catch (e) { }
        return false;
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
            const tt = clamp01((this._elapsed - this._startMs) / this._ms(EXPAND_MS));
            // The capsule width is a spring, not a canned ease: it
            // accelerates out of the closed state and settles into the
            // open one with one subtle overshoot (spec §10-11). In
            // reduced-motion the spring time is scaled with everything
            // else so the outcome still shows without the motion.
            this._spring.step(this._ms(TICK_MS) / 1000, EXPANDED_WIDTH);
            this._setWidth(Math.max(CLOSED_WIDTH, this._spring.x));
            const grow = clamp01(
                (this._widthTarget - CLOSED_WIDTH) / (EXPANDED_WIDTH - CLOSED_WIDTH));

            if (this._state === 'opening') {
                // Splash: fade the active variant in while the capsule
                // grows, and hand off to the glyph once both the pill
                // has settled and the splash has finished.
                const sinceMs = this._elapsed - this._startMs;
                this._opening.tick(sinceMs);
                if (grow >= 0.999 && sinceMs >= this._opening.duration()) {
                    this._state = 'scanning';
                    this._opening.handoff(this);
                    this._glyph.visible = true;
                    this._setLabel('Looking for you…', this._reason || '');
                }
            }

            const scanT = (this._elapsed % SWEEP_MS) / SWEEP_MS;
            this._lastScanT = scanT;
            this._glyph.animate({
                phase: 'rays', scanT,
                t: 0,
                progress: this._state === 'verifying' ? this._progress : 0,
                ramp: clamp01(tt),
                failed: false,
            });
            this._place();
            break;
        }

        case 'matched': {
            const tt = clamp01((this._elapsed - this._startMs) / this._ms(MATCHED_MS));
            // The crest completes one clean accelerating rotation from
            // where it paused when the match landed, then the ring
            // collapses and the checkmark draws itself.
            this._glyph.animate({ phase: 'matched', scanT: this._lastScanT,
                                  t: tt, progress: 1, ramp: 1, failed: false });

            // Once the checkmark is underway the pill resolves: green
            // tint + "Verified" springs in, and the capsule does one
            // small physical overshoot as it settles.
            const popT = clamp01((tt - VERIFY_POP_AT) / 0.16);
            if (popT > 0) {
                if (!this._verifiedPop) {
                    this._verifiedPop = true;
                    this._setClasses(true);
                    this._setLabel('Verified', '');
                }
                const s = 0.84 + 0.16 * easeOutBack(popT);
                this._label.scale_x = s;
                this._label.scale_y = s;
                this._label.opacity = Math.round(255 * easeOutCubic(popT));
            }
            const pulse = easeOutBack(clamp01((tt - VERIFY_POP_AT) / 0.22));
            this._setWidth(EXPANDED_WIDTH + (pulse > 0 ? 16 * (pulse - 1) : 0));
            this._place();

            if (tt >= 1) {
                // The label has settled at "Verified"; hand off to the
                // greeting while the checkmark stays on screen. Only a
                // laptop-open unlock gets the Welcome <uid> reveal; a
                // sudo prompt just holds on "Verified".
                this._label.scale_x = 1;
                this._label.scale_y = 1;
                this._label.opacity = 0;
                this._label.translation_y = 8;
                this._enter('welcome');
                if (this._loginContext) {
                    this._setLabel('Welcome', this._reason);
                    this._label.add_style_class_name('faceid-welcome');
                    this._detail.add_style_class_name('faceid-uid');
                    this._detail.opacity = 0;
                    this._detail.translation_y = 10;
                } else {
                    this._setLabel('Verified', '');
                    this._label.remove_style_class_name('faceid-welcome');
                    this._detail.remove_style_class_name('faceid-uid');
                    this._label.opacity = 255;
                }
            }
            break;
        }

        case 'welcome': {
            const tt = clamp01((this._elapsed - this._startMs) / this._ms(WELCOME_MS));
            this._glyph.animate({ phase: 'matched', scanT: this._lastScanT,
                                  t: 1, progress: 1, ramp: 1, failed: false });
            if (this._loginContext) {
                // "Welcome" fades up with a small rise; the identity follows
                // it a beat later. Both ride the slower WELCOME_MS clock.
                const s = easeOutCubic(clamp01(tt / 0.45));
                this._label.opacity = Math.round(255 * s);
                this._label.translation_y = 8 * (1 - s);
                this._label.scale_x = this._label.scale_y = 0.94 + 0.06 * s;
                if (this._detail.text) {
                    const s2 = easeOutCubic(clamp01((tt - 0.35) / 0.45));
                    this._detail.opacity = Math.round(255 * s2);
                    this._detail.translation_y = 10 * (1 - s2);
                }
            } else {
                // sudo / polkit / test scan: no greeting, just a calm
                // "Verified" hold, a tiny settle, then contract.
                const s = easeOutCubic(clamp01(tt / 0.15));
                this._label.opacity = 255;
                this._label.translation_y = 0;
                this._label.scale_x = this._label.scale_y = 0.94 + 0.06 * s;
            }
            this._place();
            if (tt >= 1) {
                this._state = 'contracting';
                this._startMs = this._elapsed + HOLD_MS;
                this._label.opacity = 255;
                this._label.translation_y = 0;
                this._detail.opacity = 255;
                this._detail.translation_y = 0;
            }
            break;
        }

        case 'rejected': {
            const tt = clamp01((this._elapsed - this._startMs) / this._ms(REJECT_MS));
            const decay = 1 - tt;
            this.translation_x = 8 * decay * decay * Math.sin(tt * Math.PI * 5);
            this._glyph.animate({ phase: 'rejected', scanT: this._rejectSweep,
                                  t: 0, progress: 0, ramp: 1, failed: true });
            this._place();
            if (tt >= 1)
                this._enter('contracting');
            break;
        }

        case 'contracting': {
            const tt = clamp01((this._elapsed - this._startMs) / this._ms(CONTRACT_MS));
            this._setWidth(EXPANDED_WIDTH -
                (EXPANDED_WIDTH - CLOSED_WIDTH) * easeOutCubic(tt));
            this.opacity = Math.round(255 * (1 - tt));
            this._place();
            if (tt >= 1)
                this.reset();
            break;
        }

        default:
            this._stopClock();
        }
    }
});