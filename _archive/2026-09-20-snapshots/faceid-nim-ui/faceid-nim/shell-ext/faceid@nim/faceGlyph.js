/* faceGlyph.js -- the Face ID glyph, drawn with Cairo.
 *
 * Four things live in one St.DrawingArea, because they are really one
 * continuous morph rather than four separate widgets:
 *
 *   SCANNING  radial rays sweeping around a face outline
 *   MATCHED   the rays collapse inward and become a checkmark
 *   UNLOCKED  the checkmark becomes an opening padlock
 *   FAILED    the rays go red and stop
 *
 * Everything is drawn from scratch instead of using icons so the
 * transitions can actually interpolate. A checkmark that pops into
 * existence reads as a different widget appearing; a checkmark whose
 * stroke is drawn over 220ms reads as the same object changing state,
 * which is the whole trick behind how the iPhone animation feels.
 *
 * The glyph is told the state and a normalized time. It owns no
 * timers -- pill.js drives it -- so there is exactly one animation
 * clock in the extension and nothing can drift out of sync.
 */

import Cairo from 'gi://cairo';
import GObject from 'gi://GObject';
import St from 'gi://St';

export const State = {
    IDLE: 'idle',
    SCANNING: 'scanning',
    MATCHED: 'matched',
    UNLOCKED: 'unlocked',
    FAILED: 'failed',
};

const RAY_COUNT = 36;
const TAU = Math.PI * 2;

// Palette. Kept here rather than in CSS because Cairo cannot read
// stylesheet colors for arbitrary sub-paths, and splitting the two
// would guarantee they drift apart.
const C_IDLE   = [0.82, 0.86, 0.92, 1.0];
const C_SCAN   = [0.48, 0.78, 1.00, 1.0];
const C_OK     = [0.36, 0.86, 0.52, 1.0];
const C_FAIL   = [1.00, 0.42, 0.42, 1.0];

function setColor(cr, [r, g, b, a], alpha = 1.0) {
    cr.setSourceRGBA(r, g, b, a * alpha);
}

/* Easing. easeOutBack overshoots slightly, which is what stops the
 * checkmark feeling mechanical. */
function easeOutCubic(t) {
    return 1 - Math.pow(1 - t, 3);
}
function easeInOutQuad(t) {
    return t < 0.5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2;
}
function easeOutBack(t) {
    const c1 = 1.70158, c3 = c1 + 1;
    return 1 + c3 * Math.pow(t - 1, 3) + c1 * Math.pow(t - 1, 2);
}
function clamp01(v) {
    return Math.max(0, Math.min(1, v));
}

export const FaceGlyph = GObject.registerClass(
class FaceGlyph extends St.DrawingArea {
    _init(size = 60) {
        super._init({
            style_class: 'faceid-glyph',
            width: size,
            height: size,
        });

        this.state = State.IDLE;
        this.t = 0;            // 0..1 progress *within* the current state
        this.phase = 0;        // free-running rotation, radians
        this.confidence = 0;   // 0..1, drives how many rays light up

        this.connect('repaint', () => this._repaint());
    }

    setState(state, t = 0) {
        this.state = state;
        this.t = t;
        this.queue_repaint();
    }

    tick(phase, t, confidence) {
        this.phase = phase;
        this.t = t;
        if (confidence !== undefined)
            this.confidence = confidence;
        this.queue_repaint();
    }

    _repaint() {
        const cr = this.get_context();
        const [w, h] = this.get_surface_size();
        const s = Math.min(w, h);

        cr.save();
        cr.translate(w / 2, h / 2);     // everything is drawn around the origin

        switch (this.state) {
        case State.SCANNING:
            this._drawRays(cr, s, 1.0, C_SCAN);
            this._drawFace(cr, s, 1.0, C_SCAN);
            break;

        case State.MATCHED: {
            // Rays collapse inward while the face fades out; the
            // checkmark draws itself in the space they leave.
            const collapse = easeInOutQuad(clamp01(this.t / 0.45));
            const check = clamp01((this.t - 0.35) / 0.65);
            if (collapse < 1)
                this._drawRays(cr, s, 1 - collapse, C_OK, collapse);
            this._drawFace(cr, s, 1 - collapse, C_OK);
            if (check > 0)
                this._drawCheck(cr, s, easeOutCubic(check));
            break;
        }

        case State.UNLOCKED: {
            // The checkmark shrinks away as the padlock scales in and
            // its shackle swings open.
            const fade = clamp01(this.t / 0.35);
            const lock = clamp01((this.t - 0.2) / 0.8);
            if (fade < 1) {
                cr.save();
                cr.scale(1 - fade * 0.4, 1 - fade * 0.4);
                this._drawCheck(cr, s, 1 - fade);
                cr.restore();
            }
            if (lock > 0)
                this._drawLock(cr, s, easeOutBack(clamp01(lock * 1.2)), lock);
            break;
        }

        case State.FAILED:
            this._drawRays(cr, s, 1.0, C_FAIL);
            this._drawFace(cr, s, 1.0, C_FAIL);
            break;

        default:
            this._drawFace(cr, s, 0.45, C_IDLE);
            break;
        }

        cr.restore();
        cr.$dispose();
    }

    /* Radial rays. Ray length is modulated by a travelling wave, so the
     * sweep reads as one bright arc moving around the circle rather
     * than all 36 rays blinking together. */
    _drawRays(cr, s, alpha, color, collapse = 0) {
        const rInner = s * (0.33 + collapse * 0.10);
        const rOuter = s * (0.46 - collapse * 0.13);
        cr.setLineWidth(Math.max(1.5, s * 0.026));
        cr.setLineCap(Cairo.LineCap.ROUND);

        for (let i = 0; i < RAY_COUNT; i++) {
            const a = (i / RAY_COUNT) * TAU;
            // Distance of this ray from the travelling crest, wrapped.
            let d = Math.abs(((a - this.phase) % TAU + TAU) % TAU);
            if (d > Math.PI)
                d = TAU - d;
            const wave = Math.pow(Math.max(0, 1 - d / (Math.PI * 0.7)), 2);
            const lit = 0.18 + 0.82 * wave;
            const len = (rOuter - rInner) * (0.45 + 0.55 * wave);

            setColor(cr, color, lit * alpha);
            cr.moveTo(Math.cos(a) * rInner, Math.sin(a) * rInner);
            cr.lineTo(Math.cos(a) * (rInner + len), Math.sin(a) * (rInner + len));
            cr.stroke();
        }
    }

    /* The face itself: corner brackets plus two eyes and a smile.
     * Same silhouette as the iOS glyph, simplified enough to stay
     * legible at 60px on a HiDPI lock screen. */
    _drawFace(cr, s, alpha, color) {
        if (alpha <= 0.01)
            return;
        const r = s * 0.26;
        const arm = r * 0.52;
        cr.setLineWidth(Math.max(2, s * 0.038));
        cr.setLineCap(Cairo.LineCap.ROUND);
        cr.setLineJoin(Cairo.LineJoin.ROUND);
        setColor(cr, color, alpha * 0.95);

        // Four L-shaped brackets.
        const corners = [[-1, -1], [1, -1], [-1, 1], [1, 1]];
        for (const [sx, sy] of corners) {
            cr.moveTo(sx * r, sy * r - sy * arm);
            cr.lineTo(sx * r, sy * r);
            cr.lineTo(sx * r - sx * arm, sy * r);
            cr.stroke();
        }

        // Eyes and smile, slightly inset from the brackets.
        const ex = r * 0.42, ey = r * 0.28;
        cr.setLineWidth(Math.max(2, s * 0.034));
        setColor(cr, color, alpha * 0.85);
        cr.moveTo(-ex, -ey);
        cr.lineTo(-ex, -ey + s * 0.055);
        cr.stroke();
        cr.moveTo(ex, -ey);
        cr.lineTo(ex, -ey + s * 0.055);
        cr.stroke();

        cr.moveTo(-ex, r * 0.38);
        cr.curveTo(-ex * 0.35, r * 0.62, ex * 0.35, r * 0.62, ex, r * 0.38);
        cr.stroke();
    }

    /* Checkmark drawn progressively. Splitting it into two segments and
     * advancing them in sequence is what makes it look drawn rather
     * than revealed. */
    _drawCheck(cr, s, t) {
        if (t <= 0)
            return;
        const p0 = [-s * 0.17, s * 0.02];
        const p1 = [-s * 0.05, s * 0.15];
        const p2 = [s * 0.19, -s * 0.13];

        cr.setLineWidth(Math.max(2.5, s * 0.075));
        cr.setLineCap(Cairo.LineCap.ROUND);
        cr.setLineJoin(Cairo.LineJoin.ROUND);
        setColor(cr, C_OK, 1.0);

        // First third of the animation draws the short stroke.
        const seg1 = clamp01(t / 0.38);
        cr.moveTo(p0[0], p0[1]);
        cr.lineTo(p0[0] + (p1[0] - p0[0]) * seg1, p0[1] + (p1[1] - p0[1]) * seg1);
        cr.stroke();

        if (t > 0.38) {
            const seg2 = clamp01((t - 0.38) / 0.62);
            cr.moveTo(p1[0], p1[1]);
            cr.lineTo(p1[0] + (p2[0] - p1[0]) * seg2, p1[1] + (p2[1] - p1[1]) * seg2);
            cr.stroke();
        }
    }

    /* Padlock whose shackle swings open. `scale` handles the pop-in,
     * `open` rotates and lifts the shackle. */
    _drawLock(cr, s, scale, open) {
        cr.save();
        cr.scale(scale, scale);

        const bw = s * 0.30, bh = s * 0.24;
        const by = s * 0.02;
        const radius = s * 0.045;

        setColor(cr, C_OK, 1.0);
        // Rounded body.
        cr.newPath();
        cr.arc(-bw / 2 + radius, by + radius, radius, Math.PI, Math.PI * 1.5);
        cr.arc(bw / 2 - radius, by + radius, radius, Math.PI * 1.5, 0);
        cr.arc(bw / 2 - radius, by + bh - radius, radius, 0, Math.PI * 0.5);
        cr.arc(-bw / 2 + radius, by + bh - radius, radius, Math.PI * 0.5, Math.PI);
        cr.closePath();
        cr.fill();

        // Shackle: pivots at its right leg and lifts as `open` goes 1.
        const sr = s * 0.105;
        const lift = open * s * 0.045;
        const tilt = open * 0.55;
        cr.save();
        cr.translate(sr * 0.75, by - lift);
        cr.rotate(tilt);
        cr.setLineWidth(Math.max(2, s * 0.045));
        cr.setLineCap(Cairo.LineCap.ROUND);
        setColor(cr, C_OK, 1.0);
        cr.newPath();
        cr.arc(-sr * 0.75, 0, sr, Math.PI, 0);
        cr.stroke();
        // Left leg, which shortens as the shackle swings clear.
        cr.moveTo(-sr * 1.5, 0);
        cr.lineTo(-sr * 1.5, s * 0.035 * (1 - open * 0.5));
        cr.stroke();
        cr.restore();

        cr.restore();
    }
});
