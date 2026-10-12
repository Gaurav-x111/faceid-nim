/* opening.js -- the "Opening" animation engine.
 *
 * A small, declarative splash shown at the start of every scan, played
 * inside the centred Face ID HUD.  Users pick a variant -- or make their
 * own -- from the
 * faceid-nim settings app; the choice lives in
 * ~/.config/faceid-nim/openings.json next to any custom logo asset the
 * user copied in.  The shell extension only *reads* that file; the app
 * is the one that writes it.
 *
 * Security model: everything here is data, never code.  The app and this
 * extension agree on a fixed set of colour keys, easing curves and one
 * optional raster/SVG logo.  A downloaded "animation" can repaint this
 * one capsule and nothing else -- it can never execute in the shell.
 *
 * Built-in variants are 'iphone' (the default: the real unlock-video
 * from linux-anim, shipped as a PNG frame sequence next to this file
 * because Clutter/St in the lock screen cannot play mp4; falls back
 * to a spring pop + breathing pulse when the frames are missing),
 * 'logo' (the classic app-logo fade), 'glow' (a soft halo, for people
 * who find a logo busy) and 'none' (skip the splash).
 * Custom variants share the same schema the app's editor writes:
 *
 *   {
 *     kind:   'custom',
 *     name:   string,                 // human readable
 *     logo:   '' | 'path/to/logo.svg' // relative to the openings dir
 *     text:   string | null,          // headline shown while opening
 *     bg:     '#rrggbb' | '',         // pill fill while opening
 *     accent: '#rrggbb' | '',         // halo + text + border tint
 *     ease:   'outCubic' | 'outBack' | 'outExpo',
 *     fade_ms: integer,               // how long the splash lasts
 *     scale:  boolean,                // logo pops in (0.85 -> 1)
 *     ring:   boolean,                // accent halo instead of the logo
 *   }
 */

import Clutter from 'gi://Clutter';
import Cogl from 'gi://Cogl';
import GdkPixbuf from 'gi://GdkPixbuf';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import St from 'gi://St';

const OPENINGS_DIR = GLib.build_filenamev([
    GLib.get_home_dir(), '.config', 'faceid-nim', 'openings']);
const CONFIG_PATH = GLib.build_filenamev([
    GLib.get_home_dir(), '.config', 'faceid-nim', 'openings.json']);
const APP_JSON = GLib.build_filenamev([
    GLib.get_home_dir(), '.config', 'faceid-nim', 'app.json']);

// User-facing animation on/off (Face Unlock app → Settings →
// Appearance & Animation → Play
// animations). Read live on every scan so the toggle applies at the
// next lock with no reload. Missing file or bad JSON = animations on
// (today's behaviour); the app writes animations_enabled: false.
export function animationsEnabled() {
    try {
        const file = Gio.File.new_for_path(APP_JSON);
        if (!file.query_exists(null))
            return true;
        const [ok, bytes] = file.load_contents(null);
        if (!ok)
            return true;
        const data = JSON.parse(new TextDecoder().decode(bytes));
        if (!data || typeof data !== 'object' ||
            !('animations_enabled' in data))
            return true;
        return data.animations_enabled !== false;
    } catch (e) {
        return true;
    }
}

const LOGO_SIZE = 36;
const RING_SIZE = 48;       // fits the pill's 48px height

// The bundled app logo, resolved relative to this module so the
// builtin 'logo' variant works no matter where it is installed.
const _here = GLib.path_get_dirname(
    GLib.filename_from_uri(import.meta.url)[0]);
const BUILTIN_LOGO_URL = GLib.filename_to_uri(
    GLib.build_filenamev([_here, 'logo.svg']), null);

const clamp01 = x => (x < 0 ? 0 : x > 1 ? 1 : x);
const easeOutCubic = t => 1 - Math.pow(1 - t, 3);
const easeOutBack = t => {
    const c1 = 1.70158, c3 = c1 + 1;
    const x = clamp01(t) - 1;
    return 1 + c3 * x * x * x + c1 * x * x;
};
const easeOutExpo = t => (t >= 1 ? 1 : 1 - Math.pow(2, -10 * t));

const EASINGS = { outCubic: easeOutCubic, outBack: easeOutBack,
                  outExpo: easeOutExpo };

const BUILTIN = {
    iphone: {
        kind: 'builtin', name: 'iPhone Video Pill',
        description: 'The real unlock video, playing on the lock screen.',
        logo: BUILTIN_LOGO_URL, text: 'Looking for your face…',
        bg: '', accent: '#9ad0ff', ease: 'outBack', fade_ms: 900,
        scale: true, ring: false, glance: true,
    },
    logo: {
        kind: 'builtin', name: 'App Logo',
        description: 'The Face Unlock logo fades in as the capsule grows.',
        logo: BUILTIN_LOGO_URL, text: 'Opening camera…',
        bg: '', accent: '', ease: 'outCubic', fade_ms: 360,
        scale: false, ring: false,
    },
    glow: {
        kind: 'builtin', name: 'Soft Glow',
        description: 'A calm accent halo, no logo lettering.',
        logo: '', text: 'Opening camera…',
        bg: '', accent: '#9ad0ff', ease: 'outCubic', fade_ms: 460,
        scale: false, ring: true,
    },
    none: {
        kind: 'builtin', name: 'No splash',
        description: 'Go straight to the face scan, nothing in between.',
        logo: '', text: null, bg: '', accent: '',
        ease: 'outCubic', fade_ms: 0, scale: false, ring: false,
    },
};

function _readConfig() {
    const fallback = { active: 'iphone', variants: {}, scan_face: 'apple' };
    try {
        const file = Gio.File.new_for_path(CONFIG_PATH);
        if (!file.query_exists(null))
            return fallback;
        const [ok, bytes] = file.load_contents(null);
        if (!ok)
            return fallback;
        const data = JSON.parse(new TextDecoder().decode(bytes));
        if (!data || typeof data !== 'object')
            return fallback;
        const face = String(data.scan_face || 'apple');
        return {
            active: String(data.active || 'iphone'),
            variants: data.variants && typeof data.variants === 'object'
                ? data.variants : {},
            scan_face: (face === 'classic' || face === 'arena') ? face : 'apple',
        };
    } catch (e) {
        logError(e, 'faceid@nim: opening config');
        return fallback;
    }
}

function _logoUrl(path) {
    // Accept an absolute path, an absolute file:// URI (builtin), or a
    // path relative to the per-user openings dir.
    if (!path)
        return null;
    if (path.startsWith('file://'))
        return path;
    let p = path;
    if (!p.startsWith('/'))
        p = GLib.build_filenamev([OPENINGS_DIR, path]);
    return GLib.filename_to_uri(p, null);
}

function _color(c) {
    return /^#[0-9a-fA-F]{6}$/.test(String(c || '')) ? String(c).toUpperCase()
        : '';
}

function _int(v, lo, hi, def) {
    v = Number(v);
    if (!Number.isFinite(v))
        return def;
    return Math.max(lo, Math.min(hi, Math.round(v)));
}

// Resolve an id to a fully-realised spec (builtin defaults merged under
// whatever the user/app stored).
function _specFor(cfg, id) {
    const entry = (cfg.variants || {})[id];
    const base = BUILTIN[id] || {
        kind: 'custom', name: id, logo: '', text: null,
        bg: '', accent: '', ease: 'outCubic', fade_ms: 420,
        scale: true, ring: false,
    };
    if (!entry)
        return Object.assign({}, base, { id });
    const spec = Object.assign({}, base, entry);
    spec.id = id;
    spec.kind = entry.kind === 'custom' ? 'custom' : base.kind;
    spec.bg = _color(spec.bg);
    spec.accent = _color(spec.accent);
    spec.ease = EASINGS[spec.ease] ? spec.ease : 'outCubic';
    spec.fade_ms = _int(spec.fade_ms, 120, 3000, 420);
    spec.scale = Boolean(spec.scale);
    spec.ring = Boolean(spec.ring);
    if (spec.ring)
        spec.logo = '';
    spec.logoUrl = spec.logo ? _logoUrl(spec.logo) : null;
    return spec;
}

// Glance video frames: unlockanimation.mp4 extracted at 30fps/216px
// (see packaging/scripts/extract-glance-frames). Clutter/St cannot
// play mp4, so the lock screen flips through these PNGs on the pill's
// own 16ms clock — the actual video content, not an imitation.
const GLANCE_FRAME_FPS = 30;
const GLANCE_FRAME_DIR = GLib.build_filenamev([_here, 'glance-frames']);
const GLANCE_FRAME_CAP = 240;

// One opening splash.  The pill owns the clock and the capsule width;
// the scene owns the logo widget, the video actor, the colours and
// the label.
export class OpeningScene {
    constructor() {
        this._logo = new St.Widget({
            style_class: 'faceid-logo',
            visible: false,
            x_align: Clutter.ActorAlign.CENTER,
            y_align: Clutter.ActorAlign.CENTER,
            width: LOGO_SIZE,
            height: LOGO_SIZE,
        });
        this._video = new Clutter.Actor({
            visible: false,
            x_align: Clutter.ActorAlign.CENTER,
            y_align: Clutter.ActorAlign.CENTER,
            width: LOGO_SIZE,
            height: LOGO_SIZE,
        });
        this._spec = null;
        this._fadeMs = 0;
        // Video-frame cache: null = not tried yet, false = unavailable
        // (procedural fallback), array = decoded frames ready to play.
        this._frames = null;
        this._videoImage = null;
        this._videoOn = false;
        this._lastFrame = -1;
    }

    get widget() {
        return this._logo;
    }

    get videoWidget() {
        return this._video;
    }

    // Load glance frames once, then cache. Sync reads of ~37 tiny
    // PNGs (<1MB total) — fast enough on first scan, never retried
    // when missing so a bad install costs one probe, not one per lock.
    _ensureFrames() {
        if (this._frames !== null)
            return Array.isArray(this._frames) ? this._frames : null;
        let frames = null;
        try {
            const dir = Gio.File.new_for_path(GLANCE_FRAME_DIR);
            if (dir.query_exists(null)) {
                const found = [];
                for (let i = 1; i <= GLANCE_FRAME_CAP; i++) {
                    const name = 'frame-' + String(i).padStart(3, '0') + '.png';
                    const child = dir.get_child(name);
                    if (!child.query_exists(null))
                        break;
                    const pix = GdkPixbuf.Pixbuf.new_from_file(
                        child.get_path());
                    if (!pix || pix.get_width() < 8)
                        break;
                    found.push({
                        bytes: pix.get_pixels(),
                        format: pix.get_has_alpha()
                            ? Cogl.PixelFormat.RGBA_8888
                            : Cogl.PixelFormat.RGB_888,
                        width: pix.get_width(),
                        height: pix.get_height(),
                        stride: pix.get_rowstride(),
                    });
                }
                if (found.length >= 8)
                    frames = found;
            }
        } catch (e) {
            frames = null;
        }
        this._frames = frames === null ? false : frames;
        return frames;
    }

    _paintFrame(idx) {
        const frames = Array.isArray(this._frames) ? this._frames : null;
        if (!frames || idx < 0 || idx >= frames.length)
            return;
        try {
            if (!this._videoImage) {
                this._videoImage = new Clutter.Image();
                this._video.set_content(this._videoImage);
            }
            const f = frames[idx];
            this._videoImage.set_data(
                f.bytes, f.format, f.width, f.height, f.stride);
            this._lastFrame = idx;
        } catch (e) {
            // A bad frame must never kill the scan animation: drop to
            // the procedural fallback from here on.
            this._frames = false;
            this._videoOn = false;
            this._video.visible = false;
        }
    }

    // Full clip length at the extraction fps; the splash holds the
    // last frame until the pill hands off to the scanner.
    _videoMs() {
        const n = Array.isArray(this._frames) ? this._frames.length : 0;
        return n > 0 ? Math.round(n * 1000 / GLANCE_FRAME_FPS) : 0;
    }

    // Re-read config and pick the active variant.  Called on every
    // startScanning so the app's choice applies at the next lock with
    // no extension reload.
    load() {
        const cfg = _readConfig();
        // 'glance' was renamed to 'iphone'; old configs migrate here
        // so a stored choice keeps working instead of falling back.
        if (cfg.active === 'glance')
            cfg.active = 'iphone';
        // Parens are load-bearing: without them this still parses as
        // (a || b) ? c : d, but the next reader will "fix" it into a bug.
        const id = (cfg.variants[cfg.active] || BUILTIN[cfg.active])
            ? cfg.active : 'iphone';
        this._spec = _specFor(cfg, id);
        this._fadeMs = Math.max(1, this._spec.fade_ms);
        this._id = id;
        this._scanFace = cfg.scan_face || 'apple';
        return this;
    }

    id() {
        return this._id || 'iphone';
    }

    // 'apple' (default iPhone-like) | 'arena' (premium) | 'classic'.
    scanFace() {
        return this._scanFace || 'apple';
    }

    name() {
        return this._spec ? this._spec.name : 'Glance Video Pill';
    }

    // How long the splash lasts before the pill may hand off to the
    // glyph. 'none' is skipped entirely (isNone()); glance plays the
    // whole clip, then holds its last frame until handoff. Anything
    // with a fade spans at least the pill-settle time so it never
    // looks cut.
    duration() {
        if (this._videoOn) {
            const vms = this._videoMs();
            if (vms > 0)
                return vms;
        }
        return Math.max(this._fadeMs || 0, 1);
    }

    // 'none' still expands the capsule; it just has no splash phase.
    isNone() {
        return (this._id || '') === 'none';
    }

    enter(pill) {
        if (this.isNone())
            return;
        const s = this._spec;

        // Glance plays the real video frames unless reduced motion /
        // animations-off is set, or the frames are missing — then it
        // falls back to the spring pop below, same as before.
        this._videoOn = false;
        this._lastFrame = -1;
        this._video.visible = false;
        const reduced = Boolean(pill._reduced || pill._animOff);
        if (s.glance && !reduced && this._ensureFrames()) {
            this._videoOn = true;
            this._logo.visible = false;
            this._video.visible = true;
            this._paintFrame(0);
        } else {
            this._logo.visible = !s.ring && Boolean(s.logoUrl);
        }
        this._logo.opacity = 0;
        this._logo.scale_x = 1;
        this._logo.scale_y = 1;
        if (!s.ring && s.logoUrl) {
            const glance = Boolean(s.glance);
            this._logo.width = s.scale ? Math.round(LOGO_SIZE * 1.15) : LOGO_SIZE;
            this._logo.height = this._logo.width;
            if (glance) {
                const halo = s.accent ? `${s.accent}44` : '#9ad0ff44';
                this._logo.style =
                    `background-image: url("${s.logoUrl}"), radial-gradient(circle at center, ${halo} 0%, transparent 70%);` +
                    ` background-size: ${LOGO_SIZE}px ${LOGO_SIZE}px, ${RING_SIZE}px ${RING_SIZE}px;` +
                    ` background-position: center; background-repeat: no-repeat; border-radius: 50%;`;
            } else {
                this._logo.style =
                    `background-image: url("${s.logoUrl}"); background-size: ${LOGO_SIZE}px ${LOGO_SIZE}px;`;
            }
        } else if (s.ring) {
            this._logo.width = RING_SIZE;
            this._logo.height = RING_SIZE;
            const halo = s.accent ? `${s.accent}44` : '#9ad0ff44';
            this._logo.style =
                `background-color: ${s.accent ? `${s.accent}22` : '#9ad0ff22'};` +
                ` background-image: radial-gradient(circle at center, ${halo} 0%, transparent 68%);` +
                ` background-size: ${RING_SIZE}px ${RING_SIZE}px; background-position: center; border-radius: 50%;`;
        } else {
            this._logo.visible = false;
        }

        if (s.text != null)
            pill._setLabel(s.text || '');

        // The HUD stays a clean transparent square; only the status line
        // and logo pick up the accent while the splash plays.
        pill._label.style = s.accent ? `color: ${s.accent};` : null;
    }

    // Advance the splash by `sinceMs` from its start. Glance flips
    // through the real video frames and holds the last one; every
    // other splash fades its logo along the chosen easing curve (and
    // glance falls back to a spring pop + breathing pulse when the
    // frames are missing, so it stays visibly distinct from 'logo').
    tick(sinceMs) {
        if (!this._spec || this.isNone())
            return;
        if (this._videoOn) {
            if (!this._video.visible)
                return;
            const frames = Array.isArray(this._frames) ? this._frames : [];
            if (!frames.length)
                return;
            const per = 1000 / GLANCE_FRAME_FPS;
            const idx = Math.min(
                Math.floor(sinceMs / per), frames.length - 1);
            if (idx !== this._lastFrame)
                this._paintFrame(idx);
            return;
        }
        if (!this._logo.visible)
            return;
        const t = clamp01(sinceMs / this._fadeMs);
        const e = EASINGS[this._spec.ease] || easeOutCubic;
        if (this._spec.glance) {
            const enter = e(clamp01(sinceMs / 420));
            this._logo.opacity = Math.round(255 * enter);
            // Spring pop 0.72 -> 1 on the same curve as the pill welcome.
            const pop = 0.72 + 0.28 * enter;
            // Breathing starts after the pop lands (~420ms): ±6% scale,
            // ±10% opacity at ~2Hz, decaying slightly into handoff.
            let breathe = 0;
            if (sinceMs > 420)
                breathe = Math.sin((sinceMs - 420) / 1000 * Math.PI * 2 * 2) * 0.06 * (1 - t * 0.4);
            this._logo.scale_x = pop + breathe;
            this._logo.scale_y = pop + breathe;
            if (breathe < 0)
                this._logo.opacity = Math.max(0, Math.round(255 * enter * (1 + breathe * 1.6)));
            return;
        }
        this._logo.opacity = Math.round(255 * e(t));
        if (this._spec.scale && this._spec.logoUrl) {
            const pop = 0.85 + 0.15 * e(t);
            this._logo.scale_x = pop;
            this._logo.scale_y = pop;
        }
    }

    // Splash is done: hide the logo and the video frames and let the
    // pill's default styles (and glyph) take over. The decoded frames
    // stay cached for the next scan.
    handoff(pill) {
        this._logo.visible = false;
        this._logo.style = null;
        this._video.visible = false;
        this._videoOn = false;
        this._lastFrame = -1;
        pill.style = null;
        pill._label.style = null;
        pill._glyph.style = null;
    }

    reset(pill) {
        this.handoff(pill);
        this._spec = null;
    }
}