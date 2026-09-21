/* The Face ID glyph, drawn in Cairo on a St.DrawingArea.
 *
 * This is the iPhone-style circular scan: a ring of segments around a
 * face, with one bright "crest" travelling around the ring while a scan
 * runs. Recognition progress raises the brightness floor of the whole
 * ring -- a "getting warmer" cue -- so the scan never looks like a
 * detached spinner.
 *
 * The glyph owns no timers and no state machine. It is handed a phase
 * and normalized timeline positions on every animation tick and just
 * draws what it is told:
 *
 *   rays    -- the ring + travelling crest + face. `ramp` scales it in
 *              (waking), `progress` raises the floor of the ring while
 *              verifying.
 *   matched -- the ring collapses inward and the face fades out, then
 *              the checkmark draws itself in two strokes across `t`
 *              (0..1 over ~620ms).
 *   unlock  -- the checkmark shrinks away and a padlock scales in, its
 *              shackle swinging open, across `t` (0..1 over ~720ms).
 *   rejected-- the ring frozen at the sweep angle it had, in the failure
 *              tint, with a cross through the face. The pill shakes.
 *
 * Colours come from the pill's inherited `color` CSS property, so the
 * ok / fail tint is a pure stylesheet change and the glyph never has to
 * know which state family it is drawing for.
 *
 * Everything below is a pure function of the phase + `t`/`scanT` the
 * pill computes on its single 16ms clock. Nothing here counts frames. */

import Cairo from 'gi://cairo';
import GObject from 'gi://GObject';
import St from 'gi://St';

const TAU = Math.PI * 2;

const clamp01 = x => (x < 0 ? 0 : x > 1 ? 1 : x);
const easeOutCubic = t => 1 - Math.pow(1 - t, 3);
const easeOutBack = t => {
    const c1 = 1.70158, c3 = c1 + 1;
    const x = clamp01(t) - 1;
    return 1 + c3 * x * x * x + c1 * x * x;
};
const easeInOutQuad = t => {
    const x = clamp01(t);
    return x < 0.5 ? 2 * x * x : 1 - Math.pow(-2 * x + 2, 2) / 2;
};

const wrapPi = a => {
    let x = a % TAU;
    if (x > Math.PI)
        x -= TAU;
    if (x < -Math.PI)
        x += TAU;
    return x;
};

// Number of arc segments around the scanning ring. 56 keeps the steps
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
        this.connect('repaint', a => this._redraw(a));
    }

    /* phase: 'rays' | 'matched' | 'unlock' | 'rejected'
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
        const col = this._color(area);
        cr.setLineCap(Cairo.LineCap.ROUND);
        cr.setLineJoin(Cairo.LineJoin.ROUND);
        cr.setFillRule(Cairo.FillRule.EVEN_ODD);

        switch (this._phase) {
        case 'matched':
            this._drawMatched(cr, cx, cy, S, col);
            break;
        case 'unlock':
            this._drawUnlock(cr, cx, cy, S, col);
            break;
        case 'rejected':
            this._drawRing(cr, cx, cy, S, col);
            break;
        default:
            this._drawRing(cr, cx, cy, S, col);
        }
        cr.$dispose();
    }

    /* The Face ID scan ring: a circle of arc segments whose brightness
     * peaks right behind the travelling crest, plus the face inside. */
    _drawRing(cr, cx, cy, S, col) {
        // The ring only collapses during the matched/unlock lead-in;
        // everywhere else it is fully open.
        const collapsing = this._phase === 'matched' || this._phase === 'unlock';
        const collapse = collapsing
            ? 1 - easeOutCubic(clamp01(this._t / 0.45))
            : 1;
        const ringR = S * 0.40 * (0.24 + 0.76 * collapse);
        const sweep = this._scanT * TAU;
        const halfW = TAU / 6;               // the crest is ~60 degrees wide
        const seg = TAU / RING_SEGS;

        if (ringR < S * 0.06)
            return;

        // Soft glow wedge in the crest's wake gives the sweep depth.
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
                bright = 1;                            // frozen, all lit
            } else {
                const floor = (0.30 + 0.70 * this._ramp) *
                              (0.38 + 0.62 * this._progress);   // getting warmer
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

        // The face being scanned. Fades with the ring on collapse.
        const fade = collapsing
            ? 1 - easeOutCubic(clamp01(this._t / 0.45))
            : 1;
        this._drawFace(cr, cx, cy, S, col, fade);

        if (this._failed) {
            // A frozen scan reads as failure when a cross runs through
            // the face -- instantly recognisable, no text needed.
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

    /* A calm abstract head, so the ring reads as a real face scan and
     * not a compass. Same visual language as Face ID's glyph. */
    _drawFace(cr, cx, cy, S, col, fade) {
        if (fade <= 0.01)
            return;
        const fw = S * 0.30, fh = S * 0.34;
        const r = fh / 2;
        const y0 = cy - fh / 2;

        // Head: a vertical capsule, centred on the scan ring.
        this._alpha(cr, col, 0.95 * fade);
        cr.newPath();
        cr.moveTo(cx - fw / 2, y0);
        cr.lineTo(cx - fw / 2, y0 + fh - r);
        cr.arc(cx, y0 + fh - r, r, Math.PI, 0);
        cr.lineTo(cx + fw / 2, y0);
        cr.lineTo(cx - fw / 2, y0);
        cr.closePath();
        cr.fill();

        // Eyes.
        this._alpha(cr, col, 0.8 * fade);
        const er = Math.max(1.4, S * 0.030);
        cr.newPath();
        cr.arc(cx - S * 0.078, y0 + S * 0.145, er, 0, TAU);
        cr.arc(cx + S * 0.078, y0 + S * 0.145, er, 0, TAU);
        cr.fill();

        // A small calm smile.
        this._alpha(cr, col, 0.75 * fade);
        cr.setLineWidth(Math.max(1.5, S * 0.028));
        cr.newPath();
        cr.arc(cx, y0 + S * 0.235, S * 0.055, Math.PI * 0.15, Math.PI * 0.85);
        cr.stroke();
    }

    /* matched: ring + face collapse over the first half, then the
     * checkmark draws itself in two strokes across the second half. */
    _drawMatched(cr, cx, cy, S, col) {
        this._drawRing(cr, cx, cy, S, col);

        const s1 = easeOutCubic(clamp01((this._t - 0.30) / 0.30));
        const s2 = easeOutCubic(clamp01((this._t - 0.60) / 0.40));
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

    /* unlock: checkmark shrinks away as the padlock scales in; its
     * shackle then swings open. */
    _drawUnlock(cr, cx, cy, S, col) {
        const shrink = clamp01(this._t / 0.35);
        if (shrink < 1) {
            const p0 = [S * 0.28, S * 0.54];
            const p1 = [S * 0.44, S * 0.70];
            const p2 = [S * 0.74, S * 0.32];
            const m = 1 - shrink;
            const mid = [cx, cy];
            const a = 1 - shrink;
            if (a > 0.02) {
                this._alpha(cr, col, a);
                cr.setLineWidth(Math.max(3, S * 0.085));
                cr.setLineCap(Cairo.LineCap.ROUND);
                cr.newPath();
                cr.moveTo(mid[0] + (p0[0] - mid[0]) * m, mid[1] + (p0[1] - mid[1]) * m);
                cr.lineTo(mid[0] + (p1[0] - mid[0]) * m, mid[1] + (p1[1] - mid[1]) * m);
                cr.lineTo(mid[0] + (p2[0] - mid[0]) * m, mid[1] + (p2[1] - mid[1]) * m);
                cr.stroke();
            }
        }

        const s = easeOutBack((this._t - 0.15) / 0.50);
        if (s <= 0)
            return;
        // ~31 degrees of shackle swing is enough to read as "open"
        // without ever looking like the lock is falling apart.
        const open = easeInOutQuad((this._t - 0.30) / 0.70) * 0.54;
        this._drawPadlock(cr, cx, cy, S, col, s, open);
    }

    _drawPadlock(cr, cx, cy, S, col, s, open) {
        const bodyW = S * 0.52, bodyH = S * 0.38, r = S * 0.09;
        const bw = Math.max(2.5, S * 0.06);

        cr.save();
        cr.translate(cx, cy);
        cr.scale(s, s);

        // Shackle first so it sits visually behind the body.
        cr.save();
        cr.translate(-bodyW / 2 + S * 0.05, -bodyH / 2);
        if (open > 0)
            cr.rotate(-open);
        cr.setLineWidth(bw);
        cr.setLineCap(Cairo.LineCap.ROUND);
        this._alpha(cr, col, 0.92);
        cr.newPath();
        cr.moveTo(0, 0);
        cr.lineTo(0, -S * 0.06);
        cr.arc(S * 0.05, -S * 0.06, S * 0.05, Math.PI, 0);
        cr.lineTo(S * 0.10, 0);
        cr.stroke();
        cr.restore();

        // The body.
        const bx = -bodyW / 2, by = -bodyH / 2;
        this._alpha(cr, col, 0.95);
        cr.newPath();
        cr.moveTo(bx + r, by);
        cr.arc(bx + bodyW - r, by + r, r, -Math.PI / 2, 0);
        cr.arc(bx + bodyW - r, by + bodyH - r, r, 0, Math.PI / 2);
        cr.arc(bx + r, by + bodyH - r, r, Math.PI / 2, Math.PI);
        cr.arc(bx + r, by + r, r, Math.PI, 3 * Math.PI / 2);
        cr.closePath();
        cr.fill();

        // Keyhole notch.
        this._alpha(cr, col, 0);
        cr.setSourceRGBA(0.06, 0.09, 0.12, 0.85);
        const kw = S * 0.045, kh = S * 0.115;
        cr.newPath();
        cr.arc(0, -kh / 2 + S * 0.02, kw * 0.9, 0, TAU);
        cr.rect(-kw / 2, -kh / 2 + S * 0.05, kw, kh * 0.62);
        cr.fill();

        cr.restore();
    }
});