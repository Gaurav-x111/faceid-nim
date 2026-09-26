/* The Face ID HUD: a black Dynamic-Island notch pinned to the monitor
 * top edge, horizontally centred. The scan glyph (a physical 2.5cm
 * square from faceid-demo.html, the user's Apple-inspired HUD) sits near
 * the island top, the status line below it inside the island, and the
 * only other top-area UI is a small card that drops in *below* the
 * island after a result:
 *
 *   success    compact dark card, rounded-rect (not an oval):
 *              [✓]  Verified / Welcome / <uid>
 *   failure    [×]  Not recognized / Use password to continue
 *
 * Why not attached to the unlock dialog: the screen shield destroys the
 * unlock dialog the moment authentication succeeds, so any success
 * animation attached to it is torn down before a single frame can play.
 * That was the "no animation after verifying" bug. This HUD lives in
 * Main.uiGroup top chrome instead -- above the shield and its lightbox
 * curtain -- so the whole sequence keeps playing while the dialog slides
 * away behind it.
 *
 * One clock, no setTimeout chains, no `await sleep()`:
 *   GLib.timeout_add(16ms) -> this._elapsed -> phase + normalized `t`
 * Authentication can succeed at any instant; the visual state adapts to
 * the actual elapsed time instead of restarting a canned A->B->C script.
 * The glyph never owns a timer to race -- it is handed a phase and a `t`
 * every tick and just draws what it is told.
 *
 * Timeline (single clock, measured from scan start):
 *   scanning      ring + spiral + beam scan, "Looking for your face"
 *   verifying     verification raises the ring floor, same idle text
 *   matched       if elapsed < MIN_SCAN: "Face ID · Locking on" and the
 *                 ring keeps scanning so a blast-fast match never looks
 *                 like a glitch; from MIN_SCAN the crest resolves green,
 *                 the ring collapses, the checkmark draws and 8 particles
 *                 burst (all inside the glyph, driven by `t`).
 *                 from MIN_SCAN+250: the success card drops in from the
 *                 top edge with a spring.
 *   complete      at COMPLETE_MS the HUD settles: dims, shrinks (.78),
 *                 fades away (loginContext greet shown on the card).
 *   rejected      the ring freezes red + cross, the HUD shakes, the
 *                 failure card drops in after FAIL_CARD_AT; hover to try
 *                 again, then it fades out.
 *
 * Since matched carries the winning identity's name, the very same
 * sequence plays at a screen unlock, at the GDM login screen and when
 * `sudo` asks for a face -- the HUD listens on the system bus, it has no
 * idea (and no need to know) which service asked. What it *can* tell is
 * the shell's own state, captured at scan start: `_loginContext` is true
 * on a locked screen / GDM login, so the card greets "Welcome <uid>";
 * a sudo/polkit/test scan just shows "Verified".
 *
 * Reduced motion: org.gnome.desktop.interface enable-animations scales
 * every phase to ~12% so the outcome still shows, without the sweep,
 * springs or motion.
 *
 * FaceGlyph (./faceGlyph.js) draws the whole circular scanner -- the
 * arena rings, spiral comet, orbit dot, beam and the success morph --
 * so this file is pure choreography: where things sit, when things
 * happen, what the labels say. */

import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import GObject from 'gi://GObject';
import Meta from 'gi://Meta';
import Pango from 'gi://Pango';
import St from 'gi://St';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';

import { FaceGlyph } from './faceGlyph.js';
import { OpeningScene, animationsEnabled } from './opening.js';

// Import-time marker: proves which pill build the shell actually loaded
// (journal: "pill top-island v3 loaded"). The shell caches extension
// modules, so a changed file with no marker is indistinguishable from
// a stale cached copy when diagnosing position bugs remotely.
try { log('faceid@nim: pill top-island v4 loaded (glance-default)'); } catch (e) {}

const TICK_MS = 16;

// Physical sizing: the scan face is a 2.5cm x 2.5cm square on screen --
// the spec's --hud-size. Convert 25mm to logical pixels once, from the
// primary monitor's physical size. Fall back to ~96dpi when Meta can't
// report the physical width.
function glyphSizePx() {
    try {
        const mm = Main.layoutManager.primaryMonitor;
        const geoW = mm && mm.geometry ? mm.geometry.width : 0;
        // get_primary_monitor() returns an index on GNOME 45-48, an
        // object with get_physical_width() only on older shells.
        let phys = 0;
        try {
            const mgr = Meta.MonitorManager.get();
            const prim = mgr.get_primary_monitor();
            if (prim && typeof prim.get_physical_width === 'function')
                phys = prim.get_physical_width();   // mm
            else if (typeof prim === 'number' && mgr.get_monitors) {
                const infos = mgr.get_monitors();
                if (infos && infos[prim] && infos[prim].width_mm > 0)
                    phys = infos[prim].width_mm;
            }
        } catch (e) { /* fall through to dpi estimate */ }
        if (phys > 0 && geoW > 0) {
            const px = 25 * geoW / phys;  // logical px for 25mm
            return Math.min(190, Math.max(72, Math.round(px)));
        }
    } catch (e) { /* fall through to the classic dpi estimate */ }
    return 94;   // 2.5cm at the classic 96dpi
}

let GLYPH_SIZE = glyphSizePx();

// Single animation timeline -- ported from the project's Apple-inspired
// reference HUD (hudWelcome 900ms spring, scannerWelcome 750ms,
// 750ms, faceWelcome 700ms, statusWelcome 600ms, scanRotate 1.35s,
// spiralRotate 1.1s, beamScan 1.55s, TIMELINE MIN_SCAN 480 / SUCCESS_RING
// 680 / CARD MIN+250 / COMPLETE 1900). Values below are that same timeline
// slowed ~2x so a real (often 200-600ms) match stays visible instead of
// flashing past, and the whole HUD lives in a top-centre Dynamic Island
// notch instead of the screen centre.
const MIN_SCAN_MS = 1500;     // min believable scan before ring resolves (was 480)
const RING_MS = 1100;         // crest resolves + collapses, check + particles (was 680)
const CARD_AT_MS = 1900;      // MIN_SCAN + 400: success card drops below island
const CARD_DROP_MS = 800;     // card springs down (demo: 800ms spring)
const COMPLETE_MS = 3400;     // whole visual sequence done (was 1900)
const FADE_MS = 650;          // HUD settles: dims, shrinks, fades (was 420)
const SWEEP_MS = 2200;        // one full ray sweep while scanning (demo avg ~1.35-2.2s)

const FAIL_CARD_AT_MS = 1400; // failure card enters after the shake (was 900)
const REJECT_MS = 2600;       // shake + "Hover to try again" hold (was 1500)

// Dynamic-Island notch geometry: the pill is a black capsule pinned to
// the monitor top edge, horizontally centred -- never the screen centre.
const ISLAND_TOP_GAP = 10;    // gap between monitor top edge and island
const ISLAND_W = 320;         // expanded island width (collapsed notch ~126)
const ISLAND_PAD_TOP = 10;    // glyph sits 10px below island top
const ISLAND_STATUS_GAP = 10; // status line sits 10px below the glyph
const ISLAND_RADIUS = 26;
const WELCOME_MS = 950;       // hudWelcome 900ms spring from the demo

const TOP_GAP = 20;           // (legacy, kept for comment refs)
const STATUS_GAP = 72;        // (legacy: centre-HUD status offset, unused now)
const SUCCESS_CARD_W = 268;
const FAILURE_CARD_W = 244;

const SCAN_TEXT = 'Looking for your face';
const LOCKON_TEXT = 'Face ID · Locking on';
const VERIFIED_TEXT = 'Face ID · Verified';

const clamp01 = x => (x < 0 ? 0 : x > 1 ? 1 : x);
const easeOutCubic = t => 1 - Math.pow(1 - t, 3);
const easeOutBack = t => {
    const c1 = 1.70158, c3 = c1 + 1;
    const x = clamp01(t) - 1;
    return 1 + c3 * x * x * x + c1 * x * x;
};

export const FaceIdPill = GObject.registerClass(
class FaceIdPill extends St.Widget {
    _init() {
        super._init({
            style_class: 'faceid-pill',
            reactive: true,     // the circular HUD is the hover-to-retry target
            can_focus: false,
            visible: false,
        });

        this._a11y = new Gio.Settings({ schema_id: 'org.gnome.desktop.interface' });
        this._reduced = !this._a11y.get_boolean('enable-animations');
        this._a11yChangedId = this._a11y.connect('changed::enable-animations',
            s => this._reduced = !s.get_boolean('enable-animations'));
        this._animOff = false;   // user switch, re-read on every scan

        this._glyph = new FaceGlyph();
        this._glyph.width = GLYPH_SIZE;
        this._glyph.height = GLYPH_SIZE;
        this.add_child(this._glyph);

        // The opening splash is owned by OpeningScene; the HUD hosts its
        // logo widget so it plays centred inside the scan square.
        this._opening = new OpeningScene();
        this.add_child(this._opening.widget);

        // Status line under the scanner.
        this._label = new St.Label({ style_class: 'faceid-status',
                                     y_align: Clutter.ActorAlign.CENTER,
                                     x_align: Clutter.ActorAlign.CENTER });
        try {
            if (this._label.clutter_text)
                this._label.clutter_text.line_alignment = Pango.Alignment.CENTER;
        } catch (e) { /* centred via x_align already */ }
        this._label.width = Math.round(GLYPH_SIZE * 1.6);
        this.add_child(this._label);

        // ---- success card (drops in only after a match) -----------
        this._card = new St.BoxLayout({
            style_class: 'faceid-success-card',
            vertical: false,
            visible: false,
            reactive: false,
            width: SUCCESS_CARD_W,
        });
        this._cardIcon = new St.Widget({ style_class: 'faceid-card-icon',
                                         width: 34, height: 34,
                                         reactive: false,
                                         x_align: Clutter.ActorAlign.CENTER,
                                         y_align: Clutter.ActorAlign.CENTER });
        this._cardCheck = new St.Label({ style_class: 'faceid-card-check',
                                         text: '✓' });
        this._cardIcon.add_child(this._cardCheck);
        this._card.add_child(this._cardIcon);

        this._cardText = new St.BoxLayout({ vertical: true });
        this._cardText.set_style('spacing: 2px;');
        this._cardTitle = new St.Label({ style_class: 'faceid-card-title',
                                         text: 'Verified' });
        this._cardSub = new St.Label({ style_class: 'faceid-card-sub',
                                       text: 'Welcome' });
        this._cardName = new St.Label({ style_class: 'faceid-card-name',
                                        text: '' });
        try {
            if (this._cardName.clutter_text)
                this._cardName.clutter_text.ellipsize = Pango.EllipsizeMode.END;
        } catch (e) { /* single-line greeting, truncation optional */ }
        this._cardText.add_child(this._cardTitle);
        this._cardText.add_child(this._cardSub);
        this._cardText.add_child(this._cardName);
        this._card.add_child(this._cardText);
        this.add_child(this._card);

        // ---- failure card (drops in after a rejection) ------------
        this._failCard = new St.BoxLayout({
            style_class: 'faceid-failure-card',
            vertical: false,
            visible: false,
            reactive: false,
            width: FAILURE_CARD_W,
        });
        this._failIcon = new St.Widget({ style_class: 'faceid-fail-icon',
                                         width: 32, height: 32,
                                         reactive: false,
                                         x_align: Clutter.ActorAlign.CENTER,
                                         y_align: Clutter.ActorAlign.CENTER });
        this._failGlyph = new St.Label({ style_class: 'faceid-fail-glyph',
                                         text: '×' });
        this._failIcon.add_child(this._failGlyph);
        this._failCard.add_child(this._failIcon);
        this._failText = new St.BoxLayout({ vertical: true });
        this._failText.set_style('spacing: 3px;');
        this._failTitle = new St.Label({ style_class: 'faceid-fail-title',
                                         text: 'Not recognized' });
        this._failSub = new St.Label({ style_class: 'faceid-fail-sub',
                                       text: 'Use password to continue' });
        this._failText.add_child(this._failTitle);
        this._failText.add_child(this._failSub);
        this._failCard.add_child(this._failText);
        this.add_child(this._failCard);

        this._state = 'idle';
        this._elapsed = 0;
        this._startMs = 0;
        this._welcomeStart = 0;
        this._progress = 0;
        this._reason = '';
        this._lastScanT = 0;
        this._rejectSweep = 0;
        this._ticker = 0;
        this._retryHandler = null;
        this._loginContext = false;

        this.connect('enter-event', () => this._onEnter());
        this.connect('leave-event', () => this._onLeave());

        // Module-load sizing may have run before the monitor was known;
        // recompute now that we exist (keeps the 2.5cm spec true).
        try {
            const s = glyphSizePx();
            if (s !== GLYPH_SIZE) {
                GLYPH_SIZE = s;
                this._glyph.width = s;
                this._glyph.height = s;
                this._label.width = Math.round(s * 1.6);
            }
        } catch (e) { /* fallback size stands */ }
        // Dynamic Island springs from its top-centre edge (the notch),
        // like hudWelcome's translateY(14px) scale(.72) in the demo.
        try { this.set_pivot_point(0.5, 0.0); } catch (e) {}
        try { this._glyph.set_pivot_point(0.5, 0.5); } catch (e) {}
        this.queue_relayout();
    }

    // ---- public API ---------------------------------------------------

    startScanning(reason = null) {
        // "Opening the laptop" -- the greeting only belongs to login and
        // lock-screen unlocks, captured *now* because the screen shield
        // drops mid-success and the check must not race it. A sudo/polkit
        // prompt in an unlocked session stays on "Verified".
        this._loginContext = this._detectLoginContext();
        // User animation switch (app → Opening → Play animations).
        // Read live on every scan: toggling needs no reload. Off
        // behaves like reduced motion — result shows, motion collapses.
        try { this._animOff = !animationsEnabled(); }
        catch (e) { this._animOff = false; }
        this._reason = reason || '';
        this.translation_x = 0;     // clear any shake from a rejection
        this._show();
        if (this._state === 'opening' || this._state === 'scanning' ||
            this._state === 'verifying')
            return;
        this._setClasses(null); // neutral scan: no green, no red
        // Opening animation: reload the active variant (the app's choice
        // applies to the very next scan), then either play the splash or
        // skip straight to the glyph.
        this._opening.load();
        this._glyph.setScanFace(this._opening.scanFace());
        if (this._opening.isNone()) {
            this._enter('scanning');
            this._setLabel(SCAN_TEXT);
            this._glyph.visible = true;
            return;
        }
        this._enter('opening');
        this._setLabel('Opening camera…');
        this._glyph.visible = false;
        this._opening.enter(this);
    }

    setProgress(p) {
        this._progress = clamp01(Number(p) || 0);
        this._show();
        if (this._state === 'success' || this._state === 'rejected')
            return;
        this._enter('verifying');
        this._opening.handoff(this);    // splash gone, glyph takes over
        this._glyph.visible = true;
        this._setLabel(SCAN_TEXT);
    }

    showMatched(detail = null) {
        this._reason = detail || this._reason || '';
        this._show();
        this._opening.handoff(this);
        this._glyph.visible = true;
        this._setClasses(true);
        this._cardName.text = this._reason;
        this._cardSub.visible = this._loginContext;
        this._cardName.visible = this._loginContext;
        this._enter('success');
        this._setLabel(VERIFIED_TEXT);
        this._speakGreeting(this._reason);
    }

    // Opt-in spoken greeting, user-customisable — laptop unlock only.
    // sudo / polkit / test scans play the same verification animation
    // but stay silent. App writes ~/.config/faceid-nim/app.json:
    //   speak_greeting: bool, greeting_text: "Welcome {name}",
    //   greeting_voice: "" (spd-say/espeak voice, blank = default),
    //   greeting_rate: -100..100 (0 = normal).
    // Calm defaults (spd-say first, espeak-ng soft/slow) so it sounds
    // simple and normal instead of robotic. Never throws into the clock.
    _speakGreeting(name) {
        // _loginContext is captured at scan start: true on lock screen /
        // GDM login, false for sudo / polkit prompts in an unlocked
        // session. Speech belongs to opening the laptop only.
        if (!this._loginContext)
            return;
        try {
            const cfg = GLib.build_filenamev([
                GLib.get_home_dir(), '.config', 'faceid-nim', 'app.json']);
            const file = Gio.File.new_for_path(cfg);
            if (!file.query_exists(null))
                return;
            const [ok, bytes] = file.load_contents(null);
            if (!ok)
                return;
            const data = JSON.parse(new TextDecoder().decode(bytes));
            if (!data || !data.speak_greeting)
                return;
            const who = String(name || '').slice(0, 24).replace(/[^A-Za-z0-9 _-]/g, '').trim();
            let template = String(data.greeting_text ?? 'Welcome {name}').slice(0, 120);
            let text;
            if (template.includes('{name}'))
                text = template.split('{name}').join(who);
            else if (who)
                text = `${template} ${who}`;
            else
                text = template;
            // Keep speech safe + simple: plain words and light punctuation.
            text = text.replace(/[^\p{L}\p{N} .,!?'\-]/gu, '').replace(/\s+/g, ' ').trim().slice(0, 160);
            if (!text)
                text = 'Verified';
            const voice = String(data.greeting_voice ?? '').slice(0, 48).replace(/[^A-Za-z0-9+_.-]/g, '');
            let rate = Number(data.greeting_rate ?? 0);
            if (!Number.isFinite(rate))
                rate = 0;
            rate = Math.max(-100, Math.min(100, Math.round(rate)));
            // spd-say understands -r -100..100 directly; espeak wants wpm.
            const espeakSpeed = String(Math.max(80, Math.min(300, 150 + rate)));
            const attempts = [];
            if (voice && rate)
                attempts.push(['spd-say', '-v', voice, '-r', String(rate), text]);
            else if (voice)
                attempts.push(['spd-say', '-v', voice, text]);
            else if (rate)
                attempts.push(['spd-say', '-r', String(rate), text]);
            attempts.push(['spd-say', text]);
            if (voice)
                attempts.push(['espeak-ng', '-s', espeakSpeed, '-a', '80', '-v', voice, text]);
            else
                attempts.push(['espeak-ng', '-s', espeakSpeed, '-a', '80', text]);
            attempts.push(['espeak', '-s', espeakSpeed, '-a', '80', text]);
            for (const argv of attempts) {
                try {
                    Gio.Subprocess.new(argv,
                        Gio.SubprocessFlags.STDOUT_SILENCE | Gio.SubprocessFlags.STDERR_SILENCE);
                    return;
                } catch (e) { /* try next backend */ }
            }
        } catch (e) { /* speech is optional */ }
    }

    showRejected(reason = null) {
        this._rejectSweep = (GLib.get_monotonic_time() / 1000) % SWEEP_MS / SWEEP_MS;
        this._reason = reason || '';
        this._show();
        this._setClasses(false);
        this._setLabel('Hover to try again');
        this._enter('rejected');
    }

    reset() {
        this._state = 'idle';
        this._startMs = 0;
        this._welcomeStart = 0;
        this._progress = 0;
        this._setClasses(null); // idle: clear ok/fail tint
        this._setLabel('');
        this._card.visible = false;
        this._card.translation_y = 0;
        this._card.scale_x = 1;
        this._card.scale_y = 1;
        this._card.opacity = 255;
        this._cardIcon.opacity = 255;
        this._cardIcon.scale_x = 1;
        this._cardIcon.scale_y = 1;
        this._cardTitle.opacity = 255;
        this._cardTitle.translation_y = 0;
        this._cardSub.opacity = 255;
        this._cardSub.translation_y = 0;
        this._cardName.opacity = 255;
        this._cardName.translation_y = 0;
        this._failCard.visible = false;
        this._failCard.translation_y = 0;
        this._failCard.scale_x = 1;
        this._failCard.scale_y = 1;
        this._failCard.opacity = 255;
        this._failIcon.opacity = 255;
        this._failIcon.scale_x = 1;
        this._failIcon.scale_y = 1;
        this._failTitle.opacity = 255;
        this._failTitle.translation_y = 0;
        this._failSub.opacity = 255;
        this._failSub.translation_y = 0;
        this.opacity = 255;
        this.translation_x = 0;
        this.translation_y = 0;
        this.scale_x = 1;
        this.scale_y = 1;
        this._label.translation_y = 0;
        this._label.opacity = 255;
        this._glyph.opacity = 255;
        this._glyph.scale_x = 1;
        this._glyph.scale_y = 1;
        this._glyph.translation_y = 0;
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
        // Reduced-motion or user-switched-off: skip the choreography,
        // keep the outcome. Both collapse every phase to ~12% so the
        // result still shows, without sweeps, springs or shake.
        if (this._reduced || this._animOff)
            return Math.max(1, Math.round(n * 0.12));
        return n;
    }

    _enter(state) {
        this._state = state;
        this._startMs = this._elapsed;
    }

    _show() {
        if (this._state === 'idle' || !this.visible) {
            this._elapsed = 0;
            this._startMs = 0;
            this._welcomeStart = 0;
        }
        // NOTE: do not touch this._progress here -- setProgress() sets it
        // before entering 'verifying', and zeroing it would kill the
        // verification floor that fills the Apple ring.
        if (!this.visible) {
            this.visible = true;
            this.opacity = 255;
        }
        this._refreshSize();
        this._layout();
        this._startClock();
    }

    // Recompute the physical 2.5cm size (monitor may change / module may
    // have loaded before layoutManager knew the monitor).
    _refreshSize() {
        const s = glyphSizePx();
        if (s !== GLYPH_SIZE) {
            GLYPH_SIZE = s;
            this._glyph.width = s;
            this._glyph.height = s;
            this._label.width = Math.round(s * 1.6);
        }
    }

    // Neutral when ok is null/undefined (scanning/idle): neither green
    // nor red. true = matched green, false = rejected red.
    _setClasses(ok) {
        this.remove_style_class_name('faceid-ok');
        this.remove_style_class_name('faceid-fail');
        if (ok === true)
            this.add_style_class_name('faceid-ok');
        else if (ok === false)
            this.add_style_class_name('faceid-fail');
    }

    _setLabel(text) {
        this._label.text = text || '';
        this._cardSub.visible =
            this._loginContext && this._state !== 'rejected';
        // Centre the status line inside the island (not the glyph square).
        const [min, nat] = this._label.get_preferred_width(-1);
        const w = Math.max(nat, min);
        this._label.width = w;
        this._label.x = Math.round((ISLAND_W - w) / 2);
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
        if (this._state === 'rejected') {
            // Hover-to-retry: hand back to the daemon and rescan.
            if (this._retryHandler)
                this._retryHandler();
            this.startScanning(this._reason || null);
        }
    }

    _onLeave() { }

    // Dynamic Island: a black capsule pinned to the monitor top edge,
    // horizontally centred (Apple notch position). The pill actor itself
    // IS the island (see .faceid-pill in stylesheet.css); the glyph sits
    // near its top, the status line below the glyph, and the result cards
    // stack just under the island -- nothing ever sits at screen centre.
    _layout() {
        const mon = Main.layoutManager.primaryMonitor;
        if (!mon)
            return;
        const islandH = ISLAND_PAD_TOP + GLYPH_SIZE + ISLAND_STATUS_GAP + 22;
        this.width = ISLAND_W;
        this.height = islandH;
        this.x = Math.round(mon.x + (mon.width - ISLAND_W) / 2);
        this.y = Math.round(mon.y + ISLAND_TOP_GAP);

        this._glyph.x = Math.round((ISLAND_W - GLYPH_SIZE) / 2);
        this._glyph.y = ISLAND_PAD_TOP;

        // Opening splash logo, centred in the scan square.
        const lw = this._opening.widget.width;
        const lh = this._opening.widget.height;
        this._opening.widget.x = Math.round((ISLAND_W - lw) / 2);
        this._opening.widget.y = ISLAND_PAD_TOP + Math.round((GLYPH_SIZE - lh) / 2);

        // Centre the ✓ inside the circular success icon and the × inside
        // the failure icon (plain St.Widget children sit at 0,0).
        this._centerChild(this._cardCheck, this._cardIcon, 34, 34);
        this._centerChild(this._failGlyph, this._failIcon, 32, 32);

        // Status line inside the island, below the glyph.
        this._label.y = ISLAND_PAD_TOP + GLYPH_SIZE + ISLAND_STATUS_GAP;

        // Result cards stack just below the island (drop-in animates
        // translation_y in the phase handlers).
        this._card.x = Math.round((ISLAND_W - SUCCESS_CARD_W) / 2);
        this._card.y = islandH + 12;
        this._failCard.x = Math.round((ISLAND_W - FAILURE_CARD_W) / 2);
        this._failCard.y = islandH + 12;
    }

    // hudWelcome from faceid-demo.html: rise 14px + scale .72 -> 1 with
    // an outBack spring over ~950ms, glyph staggered +180ms/750ms
    // (scannerWelcome), status +600ms (statusWelcome). Driven by the same
    // single clock so a fast match never cuts the entrance.
    _welcomeTick() {
        const e = this._ms(this._elapsed - this._welcomeStart);
        const wT = clamp01(e / this._ms(WELCOME_MS));
        const spring = easeOutBack(wT);
        const fade = easeOutCubic(clamp01(e / this._ms(500)));
        this.opacity = Math.round(255 * fade);
        this.translation_y = Math.round(14 * (1 - easeOutCubic(wT)));
        const s = 0.72 + 0.28 * spring;
        this.scale_x = s;
        this.scale_y = s;
        // Glyph pops in slightly after the island (demo .18s stagger).
        const gT = easeOutBack(clamp01((e - this._ms(180)) / this._ms(750)));
        const gs = 0.82 + 0.18 * gT;
        this._glyph.scale_x = gs;
        this._glyph.scale_y = gs;
        this._glyph.opacity = Math.round(255 * easeOutCubic(clamp01((e - this._ms(180)) / this._ms(450))));
        // Status line fades/slides last (demo .6s stagger).
        const sT = easeOutCubic(clamp01((e - this._ms(600)) / this._ms(600)));
        this._label.opacity = Math.round(255 * sT);
        this._label.translation_y = Math.round(6 * (1 - sT));
        if (wT >= 1) {
            this.translation_y = 0;
            this.scale_x = 1;
            this.scale_y = 1;
            this._glyph.scale_x = 1;
            this._glyph.scale_y = 1;
            this._glyph.opacity = 255;
            this._label.opacity = 255;
            this._label.translation_y = 0;
            return true;
        }
        return false;
    }

    // ---- the one clock -------------------------------------------------

    _centerChild(child, parent, pw, ph) {
        const [minw, natw] = child.get_preferred_width(-1);
        const [minh, nath] = child.get_preferred_height(-1);
        const w = Math.max(1, Math.max(natw, minw));
        const h = Math.max(1, Math.max(nath, minh));
        child.width = w;
        child.height = h;
        child.x = Math.round((pw - w) / 2);
        child.y = Math.round((ph - h) / 2);
    }

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

    // A single elapsed clock drives every visual state (the spec's
    // rAF loop compiled to one GLib timeout).
    _tick() {
        this._elapsed += TICK_MS;
        this._layout();

        switch (this._state) {
        case 'opening': {
            const sinceMs = this._elapsed - this._startMs;
            this._welcomeTick();
            this._opening.tick(sinceMs);
            this._glyph.animate({ phase: 'rays', scanT: 0, t: 0,
                                  progress: 0, ramp: 0, failed: false });
            if (sinceMs >= this._opening.duration()) {
                this._state = 'scanning';
                this._opening.handoff(this);
                this._glyph.visible = true;
                this._setLabel(SCAN_TEXT);
            }
            break;
        }

        case 'scanning':
        case 'verifying': {
            const welcomeDone = this._welcomeTick();
            const scanT = (this._elapsed % SWEEP_MS) / SWEEP_MS;
            this._lastScanT = scanT;
            // Ramp scales the rings in while the island springs open.
            const ramp = welcomeDone ? 1 : clamp01(this._elapsed / this._ms(500));
            this._glyph.animate({
                phase: 'rays', scanT,
                t: 0,
                progress: this._state === 'verifying' ? this._progress : 0,
                ramp,
                failed: false,
            });
            break;
        }

        case 'success': {
            // Elapsed is measured from scan start, so a lightning-fast
            // match still gets a believable MIN_SCAN of visible scanning
            // ("Locking on") before the ring resolves -- the ring phase
            // only starts after MIN_SCAN, never before.
            const e = this._ms(this._elapsed - this._startMs);
            if (e < MIN_SCAN_MS) {
                this._welcomeTick();
                this._setLabel(LOCKON_TEXT);
                const scanT = (this._elapsed % SWEEP_MS) / SWEEP_MS;
                this._lastScanT = scanT;
                this._glyph.animate({ phase: 'rays', scanT, t: 0,
                                      progress: 1, ramp: 1, failed: false });
            } else {
                this._setLabel(VERIFIED_TEXT);
                const t = clamp01((e - MIN_SCAN_MS) / this._ms(RING_MS));
                this._glyph.animate({ phase: 'matched', scanT: this._lastScanT,
                                      t, progress: 1, ramp: 1, failed: false });
                const cd = e - this._ms(CARD_AT_MS);
                if (cd >= 0)
                    this._cardTick(cd);
            }
            if (e >= this._ms(COMPLETE_MS)) {
                this._enter('fading');
                this._startMs += Math.max(0, e - this._ms(COMPLETE_MS));
            }
            break;
        }

        case 'rejected': {
            const e = this._ms(this._elapsed - this._startMs);
            const decay = 1 - clamp01(e / this._ms(REJECT_MS));
            this.translation_x = 8 * decay * decay * Math.sin(e / 100 * Math.PI * 0.9);
            this._glyph.animate({ phase: 'rejected', scanT: this._rejectSweep,
                                  t: 0, progress: 0, ramp: 1, failed: true });
            if (e >= this._ms(FAIL_CARD_AT_MS))
                this._failCardTick(e - this._ms(FAIL_CARD_AT_MS));
            if (e >= this._ms(REJECT_MS))
                this._enter('fading');
            break;
        }

        case 'fading': {
            const tt = clamp01((this._elapsed - this._startMs) / this._ms(FADE_MS));
            const out = easeOutCubic(tt);
            const cardOut = clamp01((tt - 0.55) / 0.45);   // cards leave last
            this.opacity = Math.round(255 * (1 - out * 0.9));
            // Demo .settled: island drifts down 5px and shrinks toward .78
            // while the glyph fades -- kept subtle so the notch doesn't pop.
            this.translation_y = Math.round(5 * out);
            this.scale_x = 1 - 0.10 * out;
            this.scale_y = 1 - 0.10 * out;
            this._glyph.scale_x = 1 - 0.22 * out;
            this._glyph.scale_y = 1 - 0.22 * out;
            this._glyph.opacity = Math.round(255 * (1 - out));
            if (this._card.visible)
                this._card.opacity = Math.round(255 * (1 - cardOut));
            if (this._failCard.visible)
                this._failCard.opacity = Math.round(255 * (1 - cardOut));
            if (tt >= 1)
                this.reset();
            break;
        }

        default:
            this._stopClock();
        }
    }

    // ---- the floating result cards -------------------------------------

    _cardTick(cd) {
        if (!this._card.visible)
            this._card.visible = true;

        // Entire card: opacity 300ms, spring drop from above the top edge.
        const inT = easeOutCubic(clamp01(cd / this._ms(320)));
        const drop = easeOutBack(clamp01(cd / this._ms(CARD_DROP_MS)));
        this._card.opacity = Math.round(255 * inT);
        this._card.translation_y = -30 * (1 - drop);
        this._card.scale_x = 0.96 + 0.04 * drop;
        this._card.scale_y = 0.96 + 0.04 * drop;

        // Icon pops in first; the lines follow with a stagger.
        const it = clamp01((cd - this._ms(140)) / this._ms(300));
        this._cardIcon.opacity = Math.round(255 * easeOutCubic(it));
        this._cardIcon.scale_x = 0.6 + 0.4 * easeOutBack(clamp01((cd - this._ms(140)) / this._ms(560)));
        this._cardIcon.scale_y = this._cardIcon.scale_x;

        this._cardLineOpacity(this._cardTitle, cd, 260);
        if (this._cardSub.visible)
            this._cardLineOpacity(this._cardSub, cd, 390);
        if (this._cardName.visible)
            this._cardLineOpacity(this._cardName, cd, 500);
    }

    _failCardTick(fd) {
        if (!this._failCard.visible)
            this._failCard.visible = true;
        const inT = easeOutCubic(clamp01(fd / this._ms(260)));
        const drop = easeOutBack(clamp01(fd / this._ms(550)));
        this._failCard.opacity = Math.round(255 * inT);
        this._failCard.translation_y = -25 * (1 - drop);
        this._failCard.scale_x = 0.97 + 0.03 * drop;
        this._failCard.scale_y = 0.97 + 0.03 * drop;
        this._failIcon.opacity = Math.round(255 * easeOutCubic(clamp01((fd - this._ms(120)) / this._ms(240))));
        this._failTitle.opacity = Math.round(255 * easeOutCubic(clamp01((fd - this._ms(200)) / this._ms(260))));
        this._failSub.opacity = Math.round(255 * easeOutCubic(clamp01((fd - this._ms(320)) / this._ms(260))));
    }

    _cardLineOpacity(actor, cd, delay) {
        const t = clamp01((cd - this._ms(delay)) / this._ms(260));
        actor.opacity = Math.round(255 * easeOutCubic(t));
        actor.translation_y = Math.round(5 * (1 - easeOutCubic(t)));
    }
});