/* opening.js -- the "Opening" animation engine.
 *
 * A small, declarative splash shown while the capsule grows at the start
 * of every scan.  Users pick a variant -- or make their own -- from the
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
 * Built-in variants are 'logo' (the current default), 'glow' (a soft
 * halo, for people who find a logo busy) and 'none' (skip the splash).
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
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import St from 'gi://St';

const OPENINGS_DIR = GLib.build_filenamev([
    GLib.get_home_dir(), '.config', 'faceid-nim', 'openings']);
const CONFIG_PATH = GLib.build_filenamev([
    GLib.get_home_dir(), '.config', 'faceid-nim', 'openings.json']);

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
    const fallback = { active: 'logo', variants: {} };
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
        return {
            active: String(data.active || 'logo'),
            variants: data.variants && typeof data.variants === 'object'
                ? data.variants : {},
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

// One opening splash.  The pill owns the clock and the capsule width;
// the scene owns the logo widget, the colours and the label.
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
        this._spec = null;
        this._fadeMs = 0;
    }

    get widget() {
        return this._logo;
    }

    // Re-read config and pick the active variant.  Called on every
    // startScanning so the app's choice applies at the next lock with
    // no extension reload.
    load() {
        const cfg = _readConfig();
        const id = cfg.variants[cfg.active] ? cfg.active : 'logo';
        this._spec = _specFor(cfg, id);
        this._fadeMs = Math.max(1, this._spec.fade_ms);
        this._id = id;
        return this;
    }

    id() {
        return this._id || 'logo';
    }

    name() {
        return this._spec ? this._spec.name : 'App Logo';
    }

    // How long the splash lasts before the pill may hand off to the
    // glyph. 'none' is skipped entirely (isNone()); anything with a
    // fade spans at least the pill-settle time so it never looks cut.
    duration() {
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

        this._logo.visible = !s.ring && Boolean(s.logoUrl);
        this._logo.opacity = 0;
        this._logo.scale_x = 1;
        this._logo.scale_y = 1;
        if (!s.ring && s.logoUrl) {
            this._logo.width = s.scale ? Math.round(LOGO_SIZE * 1.15) : LOGO_SIZE;
            this._logo.height = this._logo.width;
            this._logo.style =
                `background-image: url("${s.logoUrl}"); background-size: ${LOGO_SIZE}px ${LOGO_SIZE}px;`;
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
            pill._setLabel('face-id', s.text);

        // Colour the capsule while it opens; restore when we hand off.
        const bg = s.bg ? `background-color: ${s.bg}E6;` : '';
        const border = s.accent ? `border-color: ${s.accent}59;` : '';
        pill.style = `${bg}${border}`.trim() || null;
        pill._label.style = s.accent ? `color: ${s.accent};` : null;
    }

    // Advance the splash by `sinceMs` from its start.  Sets the logo's
    // opacity (and a small pop) along the chosen easing curve.
    tick(sinceMs) {
        if (!this._spec || this.isNone() || !this._logo.visible)
            return;
        const t = clamp01(sinceMs / this._fadeMs);
        const e = EASINGS[this._spec.ease] || easeOutCubic;
        this._logo.opacity = Math.round(255 * e(t));
        if (this._spec.scale && this._spec.logoUrl) {
            const pop = 0.85 + 0.15 * e(t);
            this._logo.scale_x = pop;
            this._logo.scale_y = pop;
        }
    }

    // Splash is done: hide the logo and let the pill's default styles
    // (and glyph) take over.
    handoff(pill) {
        this._logo.visible = false;
        this._logo.style = null;
        pill.style = null;
        pill._label.style = null;
        pill._glyph.style = null;
    }

    reset(pill) {
        this.handoff(pill);
        this._spec = null;
    }
}