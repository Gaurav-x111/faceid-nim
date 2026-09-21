/* The Face ID glyph, drawn in Cairo on a St.DrawingArea.
 *
 * This is the "premium arena" scanner: concentric detail rings around
 * a framed face, a bright gradient crest travelling around the outer
 * ring while a head-dot rides its tip, a counter-rotating secondary
 * dash, a slow dotted inner ring and a horizontal beam sweeping the
 * face. Recognition progress raises the brightness floor of the ring
 * so the scan never looks like a detached spinner.
 *
 * The glyph owns no timers and no state machine. It is handed a phase
 * and normalized timeline positions on every animation tick and just
 * draws what it is told:
 *
 *   rays    -- arena scanner. `ramp` scales the ring in (waking),
 *              `progress` raises the floor while verifying.
 *   matched -- the ring resolves to solid Apple green, collapses
 *              inward, the face fades, a checkmark (circle + stroke)
 *              springs in, a radial glow pulses and 8 particles burst
 *              outward. Mirrors the Apple Face ID success morph.
 *   rejected-- the ring frozen, failure tint, cross through the face.
 *
 * Colours: the live ring blends toward Apple cyan; the success phase
 * uses its own green. The pill's ok / fail stylesheet tint still
 * drives the capsule chrome around this glyph.
 *
 * Everything below is a pure function of the phase + `t`/`scanT` the
 * pill computes on its single 16ms clock. Nothing here counts frames. */

import Cairo from 'gi://cairo';
import GObject from 'gi://GObject';
import St from 'gi://St';

const TAU = Math.PI * 2;

const clamp01 = x => (x < 0 ? 0 : x > 1 ? 1 : x);
const easeOutCubic = t => 1 - Math.pow(1 - t, 3);
const easeInCubic = t => Math.pow(clamp01(t), 3);
const easeOutBack = t => {
    const c1 = 1.70158, c3 = c1 + 1;
    const x = clamp01(t) - 1;
    return 1 + c3 * x * x * x + c1 * x * x;
};

// How much of the matched phase is the final accelerating sweep before
// the ring starts collapsing. Must match the pill's RING_RESOLVE_AT.
const RING_RESOLVE_AT = 0.30;

const wrapPi = a => {
    let x = a % TAU;
    if (x > Math.PI)
        x -= TAU;
    if (x < -Math.PI)
        x += TAU;
    return x;
};

// Ring geometry as fractions of the glyph side (spec: 68px scanner).
const PRIMARY_R = 0.40;   // 27/68  crest ring
const OUTER_R = 0.44;     // 30/68  faint halo ring
const INNER_R = 0.32;     // 22/68  dotted inner ring

// Secondary ring rotates the other way, ~1.7x slower than the crest;
// the inner dotted ring crawls backwards. All derived from the same
// `sweep` so one clock drives every layer.
const SECONDARY_REV = 2000 / 1800;
const INNER_REV = 2000 / 7000;

// The "spiral energy" comet (spec .spiral): a bright wedge that orbits
// 1.15s/rev, slightly faster than the 2s crest sweep, hugging the band
// between the outer halo and the primary ring.
const SPIRAL_REV = 2000 / 1150;
const SPIRAL_R = 0.415;         // (34-4)/68 .. (30-1.5)/68 band
const SPIRAL_FADE_AT = 0.22;    // successful: gone in the first 22%

// Apple scan language: cyan family for the live ring, solid green at
// match (#30d158).
const SCAN_CYAN = [0.22, 0.60, 1.0];
const SCAN_WHITE = [0.95, 0.98, 1.0];
const SUCCESS_GREEN = [48 / 255, 209 / 255, 88 / 255];

// Number of arc segments around the primary ring. 56 keeps the steps
// below a pixel wide on a 52px glyph, so the ring reads as smooth but
// still lets each segment be lit individually by the crest.
const RING_SEGS = 56;

export const FaceGlyph = GObject.registerClass(
class FaceGlyph extends St.DrawingArea {
    _init() {
        super._init({
            style_class: 'faceid-glyph',
            width: 52,
            height: 52,
            reactive: false,
            can_focus: false,
        });
        this._phase = 'rays';
        this._scanT = 0;
        this._t = 0;
        this._progress = 0;
        this._ramp = 0;
        this._failed = false;
        this._face = 'arena';       // 'arena' (premium) | 'classic'
        this.connect('repaint', a => this._redraw(a));
    }

    /* Select which scan face to draw. Set by the pill from the user's
     * config each scan: 'arena' (default premium) or 'classic'. */
    setScanFace(face) {
        this._face = (face === 'classic') ? 'classic' : 'arena';
    }

    /* phase: 'rays' | 'matched' | 'rejected'
     * scanT: continuous sweep angle, 0..1 wrapping
     * t:     phase-local progress for one-shot animations, 0..1
     * progress / ramp / failed: brightness inputs
     */
    animate({ phase = 'rays', scanT = 0, t = 0,
              progress = 0, ramp = 1, failed = false } = {}) {
        if (this._phase !== phase || this._scanT !== scanT ||
            this._t !== t || this._progress !== progress ||
            this._ramp !== ramp || this._failed !== failed) {
            this._phase = phase;
            this._scanT = scanT;
            this._t = t;
            this._progress = progress;
            this._ramp = ramp;
            this._failed = failed;
            this.queue_repaint();
        }
    }

    _color(area) {
        const [ok, c] = area.get_theme_node().lookup_color('color', true);
        if (ok)
            return [c.red / 255, c.green / 255, c.blue / 255];
        return [0.49, 0.77, 1.0];
    }

    _alpha(cr, col, a) {
        cr.setSourceRGBA(col[0], col[1], col[2], clamp01(a));
    }

    _redraw(area) {
        const cr = area.get_context();
        const [w, h] = area.get_surface_size();
        const S = Math.min(w, h);
        const cx = S / 2, cy = S / 2;
        const thm = this._color(area);
        cr.setLineCap(Cairo.LineCap.ROUND);
        cr.setLineJoin(Cairo.LineJoin.ROUND);
        cr.setFillRule(Cairo.FillRule.EVEN_ODD);

        if (this._face === 'classic') {
            // Original ring/sweep/face pill, drawn in the stylesheet
            // colour (no arena chrome, no particles).
            if (this._phase === 'matched')
                this._drawClassicMatched(cr, cx, cy, S, thm);
            else
                this._drawClassicRing(cr, cx, cy, S, thm);
            cr.$dispose();
            return;
        }

        // Live scanning reads bluer than the resting theme colour.
        const scan = this._phase === 'rays' && !this._failed;
        const col = scan ? thm.map((v, i) => v + (SCAN_CYAN[i] - v) * 0.7) : thm;
        cr.setLineCap(Cairo.LineCap.ROUND);
        cr.setLineJoin(Cairo.LineJoin.ROUND);
        cr.setFillRule(Cairo.FillRule.EVEN_ODD);

        switch (this._phase) {
        case 'matched':
            this._drawMatched(cr, cx, cy, S, col);
            break;
        case 'rejected':
            this._drawRing(cr, cx, cy, S, col);
            break;
        default:
            this._drawRing(cr, cx, cy, S, col);
        }
        cr.$dispose();
    }

    /* ---- the arena scanner ---------------------------------------- */

    _drawRing(cr, cx, cy, S, col) {
        const matched = this._phase === 'matched';

        // During the matched lead-in the crest completes one accelerating
        // rotation from where it paused, so the ring visibly "resolves"
        // before it collapses.
        const sweepP = matched ? easeInCubic(clamp01(this._t / RING_RESOLVE_AT)) : 0;
        const scanT = this._scanT + (matched ? sweepP : 0);
        const sweep = scanT * TAU;
        const collapse = matched
            ? 1 - easeOutCubic(clamp01((this._t - RING_RESOLVE_AT) / 0.34))
            : 1;
        // Success flips the crest from cyan to solid green (spec §6):
        // the instant the resolution sweep ends.
        const flip = matched
            ? clamp01((this._t - RING_RESOLVE_AT * 0.98) / 0.06)
            : 0;
        const ringCol = matched
            ? [SUCCESS_GREEN[0] * flip + col[0] * (1 - flip),
               SUCCESS_GREEN[1] * flip + col[1] * (1 - flip),
               SUCCESS_GREEN[2] * flip + col[2] * (1 - flip)]
            : col;

        // Soft radial glow behind the whole scanner (the "spiral" layer
        // reads better as a diffuse colour field in Cairo).
        const glowIn = this._failed ? 0 :
            this._ramp * (matched ? (0.35 + 0.65 * flip) : 1) * collapse;
        if (glowIn > 0.01) {
            const grad = new Cairo.RadialGradient(cx, cy, 0, cx, cy, S * 0.86);
            const gc = matched ? SUCCESS_GREEN : SCAN_CYAN;
            grad.addColorStop(0, [gc[0], gc[1], gc[2], 0.16 * glowIn]);
            grad.addColorStop(1, [gc[0], gc[1], gc[2], 0]);
            cr.setSource(grad);
            cr.paint();
        }

        // Waking: rings scale in quickly as the capsule drops.
        const kIn = 0.45 + 0.55 * clamp01((this._ramp - 0.0) / 0.5);

        const sweepA = matched ? sweep : sweep * (1.0);
        const rev = matched ? 0 : sweep * SECONDARY_REV;
        const iRev = matched ? 0 : sweep * INNER_REV;

        // Outer faint halo (spec: outer-ring).
        if (collapse > 0.02) {
            this._alpha(cr, SCAN_WHITE, 0.09 * this._ramp * collapse);
            cr.setLineWidth(Math.max(1, S * 0.018));
            cr.newPath();
            cr.arc(cx, cy, OUTER_R * S * kIn, 0, TAU);
            cr.stroke();
        }

        // Secondary dash, counter-rotating (spec: scan-ring-secondary).
        if (!this._failed && collapse > 0.02) {
            const arcLen = S * 0.62;
            this._alpha(cr, col, 0.38 * this._ramp * collapse);
            cr.setLineWidth(Math.max(1, S * 0.02));
            cr.newPath();
            cr.arc(cx, cy, PRIMARY_R * S * kIn, rev, rev + arcLen * collapse);
            cr.stroke();
        }

        // Spiral energy: the conic-gradient comet from the spec, drawn
        // as a trailing wedge (white-hot head -> cyan -> blue -> clear)
        // orbiting just inside the outer halo.
        if (!this._failed && collapse > 0.02) {
            this._drawSpiral(cr, cx, cy, S, sweepA, kIn, collapse, matched);
        }

        // Primary crest ring: the bright travelling arc plus its tip
        // head-dot and wake glow.
        if (collapse > 0.02) {
            this._drawCrest(cr, cx, cy, S, ringCol, sweepA, kIn, collapse, matched);
        }

        // Dotted inner ring, crawling backwards (spec: inner-ring).
        if (!this._failed && collapse > 0.02) {
            const dashes = 18;
            const seg = TAU / dashes;
            const dashAng = Math.max(0.03, S * 0.048 / (INNER_R * S));
            this._alpha(cr, SCAN_WHITE, 0.16 * this._ramp * collapse);
            cr.setLineWidth(Math.max(1, S * 0.018));
            for (let i = 0; i < dashes; i++) {
                const a = iRev + i * seg;
                cr.newPath();
                cr.arc(cx, cy, INNER_R * S * kIn, a, a + dashAng);
                cr.stroke();
            }
        }

        // Horizontal beam sweeping the face (spec: scan-beam).
        if (!matched && !this._failed && this._ramp > 0.99) {
            const b = 0.5 + 0.5 * Math.cos(sweep * SECONDARY_REV);
            const y = cy + S * 0.37 * (b - 0.5) * 2;
            const beamA = 0.15 + 0.55 * (0.5 - 0.5 * Math.cos(sweep * SECONDARY_REV * 2));
            this._alpha(cr, SCAN_WHITE, 0.25 * beamA * this._ramp);
            cr.setLineWidth(1);
            cr.newPath();
            cr.moveTo(cx - S * 0.34, y);
            cr.lineTo(cx + S * 0.34, y);
            cr.stroke();
        }

        // The face, framed by corner brackets.
        const fade = this._failed ? 1 : collapse;
        this._drawFace(cr, cx, cy, S, matched ? col : col, fade,
                       matched ? clamp01((this._t - 0.34) / 0.10) : 0);

        if (this._failed) {
            this._alpha(cr, col, 0.45);
            cr.setLineWidth(Math.max(2, S * 0.06));
            const arm = S * 0.20;
            cr.moveTo(cx - arm, cy - arm);
            cr.lineTo(cx + arm, cy + arm);
            cr.moveTo(cx - arm, cy + arm);
            cr.lineTo(cx + arm, cy - arm);
            cr.stroke();
        }
    }

    /* The orbiting spiral comet (spec .spiral): a ~2px band at radius
     * SPIRAL_R whose brightness falls off behind a white-hot head. The
     * head leads the primary crest by its own rate; on success the
     * whole wedge fades out fast, exactly like the secondary and beam
     * layers. */
    _drawSpiral(cr, cx, cy, S, sweepA, kIn, collapse, matched) {
        const head = sweepA * SPIRAL_REV + Math.PI / 3;
        const fade = matched
            ? 1 - easeOutCubic(clamp01(this._t / SPIRAL_FADE_AT))
            : 1;
        const a0 = fade * this._ramp * collapse;
        if (a0 < 0.01)
            return;

        const r = SPIRAL_R * S * kIn;
        const seg = TAU / 48;
        const litSpan = TAU * 0.24;      // ~85deg of visible tail
        const bw = Math.max(1.2, S * 0.022);
        cr.setLineWidth(bw);

        for (let i = 0; i < 48; i++) {
            const a = i * seg;
            // angular distance behind the head, 0 at the tip
            const behind = wrapPi(head - a);
            const light = clamp01((litSpan - behind) / litSpan);
            if (light < 0.02)
                continue;
            // ramp the hue white -> cyan -> darker blue for the trailing
            // tail, matching the conic stops near the head.
            let mr, mg, mb;
            if (light > 0.6) {
                const t = (light - 0.6) / 0.4;      // 1 at the head
                mr = SCAN_CYAN[0] + (SCAN_WHITE[0] - SCAN_CYAN[0]) * t;
                mg = SCAN_CYAN[1] + (SCAN_WHITE[1] - SCAN_CYAN[1]) * t;
                mb = SCAN_CYAN[2] + (SCAN_WHITE[2] - SCAN_CYAN[2]) * t;
            } else {
                const t = light / 0.6;
                mr = 0.16 + (SCAN_CYAN[0] - 0.16) * t;
                mg = 0.46 + (SCAN_CYAN[1] - 0.46) * t;
                mb = 0.86 + (SCAN_CYAN[2] - 0.86) * t;
            }
            const alpha = a0 * (0.25 * light * light + 0.75 * light ** 4);
            this._alpha(cr, [mr, mg, mb], alpha);
            cr.newPath();
            cr.arc(cx, cy, r, a - seg * 1.2, a + seg * 1.2);
            cr.stroke();
        }

        // White-hot head dot closing the comet.
        this._alpha(cr, SCAN_WHITE, 0.85 * a0);
        const hr = Math.max(1.4, S * 0.022);
        cr.newPath();
        cr.arc(cx + r * Math.cos(head), cy + r * Math.sin(head), hr, 0, TAU);
        cr.fill();
    }

    /* The bright crest: a wide arc of lit segments with a soft wake
     * glow, plus a white head-dot riding the leading tip (spec §3-5). */
    _drawCrest(cr, cx, cy, S, col, sweepA, kIn, collapse, matched) {
        const ringR = OUTER_R * S * kIn * (matched ? 0.93 : 1);
        if (ringR < S * 0.05)
            return;

        const halfW = TAU / 3.2;      // ~ the lit arc span
        const seg = TAU / RING_SEGS;
        const bw = Math.max(2, S * 0.058 * (0.55 + 0.45 * collapse));

        // Wake glow wedge gives the crest depth.
        if (!this._failed && collapse > 0.02) {
            this._alpha(cr, col, 0.09 * this._ramp * collapse);
            cr.moveTo(cx, cy);
            cr.arc(cx, cy, ringR * 1.06, sweepA - halfW, sweepA + halfW);
            cr.closePath();
            cr.fill();
        }

        cr.setLineWidth(bw);
        for (let i = 0; i < RING_SEGS; i++) {
            const a = i * seg;
            const mid = a + seg / 2;
            const dist = Math.abs(wrapPi(mid - sweepA));
            const wave = clamp01(1 - dist / halfW);
            const fadeA = clamp01(1 - dist / (TAU / 1.7));
            let bright;
            if (this._failed) {
                bright = 1;
            } else {
                const floor = (0.30 + 0.70 * this._ramp) *
                              (0.30 + 0.70 * this._progress);
                bright = floor + (1 - floor) * wave * wave * 0.92 * fadeA;
            }
            bright *= collapse;
            if (bright < 0.02)
                continue;
            // Near the crest tip the segment runs white-hot over the
            // base cyan, matching the gradient's brightest stops.
            const tipmix = wave * wave;
            const segCol = [
                col[0] + (SCAN_WHITE[0] - col[0]) * tipmix * 0.8,
                col[1] + (SCAN_WHITE[1] - col[1]) * tipmix * 0.8,
                col[2] + (SCAN_WHITE[2] - col[2]) * tipmix * 0.8,
            ];
            this._alpha(cr, segCol, bright);
            cr.newPath();
            cr.arc(cx, cy, ringR, a, a + seg * 1.15);
            cr.stroke();
        }

        // Head-dot riding the tip (spec: scan-head), brightest, with a
        // small glow.
        const tipB = this._failed ? 1 : 0.55 + 0.45 * this._progress;
        const dotR = Math.max(1.6, S * 0.045) * (0.7 + 0.3 * collapse);
        this._alpha(cr, SCAN_WHITE, 0.92 * tipB * collapse);
        cr.newPath();
        cr.arc(cx + ringR * Math.cos(sweepA),
               cy + ringR * Math.sin(sweepA), dotR, 0, TAU);
        cr.fill();
    }

    /* The framed face: corner brackets around an abstract head, eyes
     * and a nose -- so the ring reads as a real face scan and never a
     * compass. Fades and withdraws on success. */
    _drawFace(cr, cx, cy, S, col, fade, shrink) {
        const s = 1 - 0.30 * shrink;
        const scaleIn = this._ramp > 0 ? clamp01(this._ramp / 0.6) : 1;
        const sh = s * scaleIn;

        cr.save();
        cr.translate(cx, cy);
        cr.scale(sh, sh);
        cr.translate(-cx, -cy);

        // Corner brackets (spec: face-corners).
        if (!this._failed && fade > 0.01) {
            const ci = S * 0.22, cl = S * 0.13, cw = Math.max(1.2, S * 0.026);
            this._alpha(cr, SCAN_WHITE, 0.9 * fade);
            cr.setLineWidth(cw);
            cr.setLineCap(Cairo.LineCap.ROUND);
            const cs = [[-1, -1], [1, -1], [-1, 1], [1, 1]];
            for (const [sx, sy] of cs) {
                cr.newPath();
                cr.moveTo(cx + sx * ci, cy + sy * ci);
                cr.lineTo(cx + sx * (ci + cl), cy + sy * ci);
                cr.stroke();
                cr.newPath();
                cr.moveTo(cx + sx * ci, cy + sy * ci);
                cr.lineTo(cx + sx * ci, cy + sy * (ci + cl));
                cr.stroke();
            }
        }

        if (fade <= 0.02)
            return;

        // Head: a calm vertical capsule, centred on the scan ring.
        const fw = S * 0.26, fh = S * 0.32;
        const r = fh / 2;
        const y0 = cy - fh / 2;
        this._alpha(cr, SCAN_WHITE, 0.92 * fade);
        cr.setLineWidth(Math.max(1.2, S * 0.024));
        cr.newPath();
        cr.moveTo(cx - fw / 2, y0);
        cr.lineTo(cx - fw / 2, y0 + fh - r);
        cr.arc(cx, y0 + fh - r, r, Math.PI, 0);
        cr.lineTo(cx + fw / 2, y0);
        cr.closePath();
        cr.stroke();

        // Eyes (two dots).
        this._alpha(cr, SCAN_WHITE, 0.8 * fade);
        const er = Math.max(1.2, S * 0.026);
        cr.newPath();
        cr.arc(cx - S * 0.064, y0 + S * 0.14, er, 0, TAU);
        cr.arc(cx + S * 0.064, y0 + S * 0.14, er, 0, TAU);
        cr.fill();

        // Nose tick.
        this._alpha(cr, SCAN_WHITE, 0.6 * fade);
        cr.setLineWidth(Math.max(1, S * 0.018));
        cr.newPath();
        cr.moveTo(cx, y0 + S * 0.21);
        cr.lineTo(cx, y0 + S * 0.27);
        cr.stroke();

        cr.restore();
    }

    /* ---- success morph -------------------------------------------- */

    _drawMatched(cr, cx, cy, S, col) {
        this._drawRing(cr, cx, cy, S, col);
        this._drawGlow(cr, cx, cy, S, col);

        const green = SUCCESS_GREEN;

        // The success halo the Apple island-icon shows behind the
        // checkmark: a soft persistent radial green field.
        const halo = easeOutCubic(clamp01((this._t - 0.40) / 0.14));
        if (halo > 0) {
            const grad = new Cairo.RadialGradient(cx, cy, S * 0.05, cx, cy, S * 0.60);
            grad.addColorStop(0, [green[0], green[1], green[2], 0.26 * halo]);
            grad.addColorStop(1, [green[0], green[1], green[2], 0]);
            cr.setSource(grad);
            cr.paint();
        }

        // Success circle drawing around the check (spec: check-circle).
        const c1 = easeOutCubic(clamp01((this._t - 0.50) / 0.26));
        if (c1 > 0) {
            this._alpha(cr, [green[0], green[1], green[2], 0.25], 0.6 * c1);
            cr.setLineWidth(Math.max(1.2, S * 0.024));
            cr.newPath();
            cr.arc(cx, cy, S * 0.30, -Math.PI / 2, -Math.PI / 2 + TAU * c1);
            cr.stroke();
        }

        // The checkmark stroked in green with a soft outer glow, then a
        // springy pop.
        const s1 = easeOutCubic(clamp01((this._t - 0.54) / 0.22));
        const s2 = easeOutCubic(clamp01((this._t - 0.76) / 0.22));
        if (s1 > 0) {
            const pop = easeOutBack(clamp01((this._t - 0.50) / 0.28));
            const k = 0.82 + 0.18 * pop;
            cr.save();
            cr.translate(cx, cy);
            cr.scale(k, k);
            cr.translate(-cx, -cy);

            const p0 = [S * 0.24, S * 0.52];
            const p1 = [S * 0.42, S * 0.68];
            const p2 = [S * 0.76, S * 0.34];
            const d1 = [p1[0] - p0[0], p1[1] - p0[1]];
            const d2 = [p2[0] - p1[0], p2[1] - p1[1]];

            const glow = Math.max(3.5, S * 0.11);
            for (const [gx, gy, ga] of [[p0[0], p0[1], 0.35], [p0[0] + d1[0] * s1, p0[1] + d1[1] * s1, 0.45], [p1[0] + d2[0] * s2, p1[1] + d2[1] * s2, 0.45]]) {
                this._alpha(cr, green, ga * 0.22);
                cr.setLineWidth(glow);
                cr.newPath();
                cr.moveTo(gx, gy);
                cr.lineTo(gx, gy);
                cr.stroke();
            }

            this._alpha(cr, green, 0.95);
            cr.setLineWidth(Math.max(2.5, S * 0.07));
            cr.newPath();
            cr.moveTo(p0[0], p0[1]);
            cr.lineTo(p0[0] + d1[0] * s1, p0[1] + d1[1] * s1);
            cr.lineTo(p1[0] + d2[0] * s2, p1[1] + d2[1] * s2);
            cr.stroke();
            cr.restore();
        }

        // Micro success particles bursting outward (spec: particles).
        this._drawParticles(cr, cx, cy, S, green);
    }

    _drawParticles(cr, cx, cy, S, green) {
        const t0 = 0.62, span = 0.34;
        const pt = clamp01((this._t - t0) / span);
        if (pt <= 0)
            return;
        const burst = 8;
        for (let i = 0; i < burst; i++) {
            const ang = (i / burst) * TAU + 0.1 * Math.sin(i);
            const r = S * (0.06 + 0.58 * easeOutCubic(pt));
            const a = (pt < 0.25 ? pt / 0.25 : 1 - (pt - 0.25) / 0.75) * 0.9;
            if (a <= 0)
                continue;
            const d = Math.max(1, S * (0.045 - 0.026 * pt));
            this._alpha(cr, pt > 0.7 ? green : SCAN_WHITE, a);
            cr.newPath();
            cr.arc(cx + r * Math.cos(ang), cy + r * Math.sin(ang), d, 0, TAU);
            cr.fill();
        }
    }

    /* Apple-style success glow (spec §3): a soft radial pulse that
     * expands from the ring and fades away as the checkmark lands. */
    _drawGlow(cr, cx, cy, S, col) {
        const inT = clamp01((this._t - 0.36) / 0.16);
        const outT = clamp01((this._t - 0.60) / 0.30);
        const g = inT * (1 - outT);
        if (g <= 0.01)
            return;
        const r0 = S * 0.50;
        const r1 = r0 + S * 1.15 * easeOutCubic(inT);
        const grad = new Cairo.RadialGradient(cx, cy, r0, cx, cy, r1);
        grad.addColorStop(0, [col[0], col[1], col[2], 0.30 * g]);
        grad.addColorStop(1, [col[0], col[1], col[2], 0]);
        cr.setSource(grad);
        cr.paint();
    }

    /* ---- classic face (the original pill) --------------------------- */

    _drawClassicRing(cr, cx, cy, S, col) {
        const matched = this._phase === 'matched';

        const sweepP = matched ? easeInCubic(clamp01(this._t / RING_RESOLVE_AT)) : 0;
        const scanT = this._scanT + (matched ? sweepP : 0);
        const sweep = scanT * TAU;
        const collapsing = matched;
        const collapse = collapsing
            ? 1 - easeOutCubic(clamp01((this._t - RING_RESOLVE_AT) / 0.34))
            : 1;
        const ringR = S * 0.40 * (0.24 + 0.76 * collapse);
        const halfW = TAU / 6 * (matched ? 1.7 : 1);
        const seg = TAU / RING_SEGS;

        if (ringR < S * 0.06)
            return;

        if (!this._failed && collapse > 0.02) {
            this._alpha(cr, col, 0.09 * this._ramp * collapse);
            cr.moveTo(cx, cy);
            cr.arc(cx, cy, ringR * 1.06, sweep - halfW, sweep + halfW);
            cr.closePath();
            cr.fill();
        }

        const bw = Math.max(2, S * 0.058 * (0.55 + 0.45 * collapse));
        cr.setLineWidth(bw);
        for (let i = 0; i < RING_SEGS; i++) {
            const a = i * seg;
            const mid = a + seg / 2;
            const dist = Math.abs(wrapPi(mid - sweep));
            const wave = clamp01(1 - dist / halfW);
            let bright;
            if (this._failed) {
                bright = 1;
            } else {
                const floor = (0.30 + 0.70 * this._ramp) *
                              (0.38 + 0.62 * this._progress);
                bright = floor + (1 - floor) * wave * wave * 0.94;
            }
            bright *= collapse;
            if (bright < 0.02)
                continue;
            this._alpha(cr, col, bright);
            cr.newPath();
            cr.arc(cx, cy, ringR, a, a + seg * 1.15);
            cr.stroke();
        }

        this._drawClassicFace(cr, cx, cy, S, col, collapse);

        if (this._failed) {
            this._alpha(cr, col, 0.45);
            cr.setLineWidth(Math.max(2, S * 0.06));
            const arm = S * 0.20;
            cr.moveTo(cx - arm, cy - arm);
            cr.lineTo(cx + arm, cy + arm);
            cr.moveTo(cx - arm, cy + arm);
            cr.lineTo(cx + arm, cy - arm);
            cr.stroke();
        }
    }

    _drawClassicFace(cr, cx, cy, S, col, fade) {
        if (fade <= 0.01)
            return;
        const fw = S * 0.30, fh = S * 0.34;
        const r = fh / 2;
        const y0 = cy - fh / 2;

        this._alpha(cr, col, 0.95 * fade);
        cr.newPath();
        cr.moveTo(cx - fw / 2, y0);
        cr.lineTo(cx - fw / 2, y0 + fh - r);
        cr.arc(cx, y0 + fh - r, r, Math.PI, 0);
        cr.lineTo(cx + fw / 2, y0);
        cr.lineTo(cx - fw / 2, y0);
        cr.closePath();
        cr.fill();

        this._alpha(cr, col, 0.8 * fade);
        const er = Math.max(1.4, S * 0.030);
        cr.newPath();
        cr.arc(cx - S * 0.078, y0 + S * 0.145, er, 0, TAU);
        cr.arc(cx + S * 0.078, y0 + S * 0.145, er, 0, TAU);
        cr.fill();

        this._alpha(cr, col, 0.75 * fade);
        cr.setLineWidth(Math.max(1.5, S * 0.028));
        cr.newPath();
        cr.arc(cx, y0 + S * 0.235, S * 0.055, Math.PI * 0.15, Math.PI * 0.85);
        cr.stroke();
    }

    _drawClassicMatched(cr, cx, cy, S, col) {
        this._drawClassicRing(cr, cx, cy, S, col);

        const s1 = easeOutCubic(clamp01((this._t - 0.50) / 0.24));
        const s2 = easeOutCubic(clamp01((this._t - 0.74) / 0.24));
        if (s1 <= 0)
            return;

        const p0 = [S * 0.28, S * 0.54];
        const p1 = [S * 0.44, S * 0.70];
        const p2 = [S * 0.74, S * 0.32];
        const d1 = [p1[0] - p0[0], p1[1] - p0[1]];
        const d2 = [p2[0] - p1[0], p2[1] - p1[1]];

        this._alpha(cr, col, 0.95);
        cr.setLineWidth(Math.max(3, S * 0.085));
        cr.setLineCap(Cairo.LineCap.ROUND);
        cr.newPath();
        cr.moveTo(p0[0], p0[1]);
        cr.lineTo(p0[0] + d1[0] * s1, p0[1] + d1[1] * s1);
        cr.lineTo(p1[0] + d2[0] * s2, p1[1] + d2[1] * s2);
        cr.stroke();
    }
});