# Lock-screen animation spec

The choreography behind what happens on screen in the ~1.5 seconds
between your face being recognised and your desktop appearing. This is
the design reference for `shell-ext/faceid@nim/pill.js` and
`faceGlyph.js`; use it to retune timings without re-deriving the
reasoning. Every number below is a starting point, not a law.

## The five phases

The five phases are `waking/scanning` (scan), `matched` (checkmark),
`unlock` (padlock), plus rejection and the hold/contract epilogue.

### 1. Idle -> Waking (the trigger)

Something has to make the pill appear: wake from sleep, a key at the
lock screen, or the lock event itself. The pill does not exist before
this -- no ghost outline, no placeholder. It starts 52px wide, fully
transparent, scaled to 72% height, sitting where it will finally rest.

> Why start collapsed instead of fading in at full size: a widget that
> fades in at full size reads as "appearing"; one that grows out of a
> small dot reads as "waking up". That distinction is the actual trick
> behind Dynamic Island, and most clones get it wrong by fading in.

### 2. Expand (340ms)

Width animates 52px -> 268px, opacity 0 -> 255, scale_y 0.72 -> 1.0,
using `EASE_OUT_BACK` -- slightly overshoots past 268px and springs
back. The text label fades in ~90ms after the expansion starts, not
simultaneously.

> Staggering the text tells the visual system "this box opened, and now
> something is inside it". A plain ease-out decelerates smoothly and is
> emotionally flat; the overshoot mimics a spring and is what makes
> motion feel alive. Current code: `easeOutBack` in `pill.js`.

### 3. Scanning (open-ended, loops until a result)

The Face ID circular scan: a ring of 56 arc segments around a face, with
one bright crest travelling around the ring at ~2.6 rad/s.

- Each segment's brightness is `wave = (1 - distance_from_crest)^2`, so
  exactly one bright arc sweeps around -- not all segments pulsing in
  unison (which reads as a loading spinner, not a scan).
- **Progress feeds brightness, never position.** As recognition
  accumulates (k-of-n voting filling), the brightness floor of the whole
  ring rises from ~0.3 toward 1.0 -- a "getting warmer" cue.

The subtitle changes too: "Waking the camera…" -> "Looking for you…" ->
"Verifying… / Hold still", so someone not watching the ring gets the
story from the words.

> Implemented in `faceGlyph.js::_drawRing` (the ring/crest/floor) and
> `pill.js` `_tick` (the label staging). Tune `SWEEP_MS` for speed,
> `halfW` (crest width) for look.

### 4. Match -> Checkmark (620ms, the make-or-break moment)

Swapping the spinner for a checkmark icon is a *substitution*, and
substitutions look cheap. Real sequence:

- **0-280ms:** the ring collapses radially inward (radius shrinks toward
  center) while fading out, `EASE_IN_OUT_QUAD`. The face fades out in
  the same window.
- **~350-620ms:** the checkmark draws itself as **two separate line
  segments in sequence**: first the short downstroke (0 -> 38% of the
  phase), then the long upstroke (38% -> 100%), each `EASE_OUT_CUBIC`.

> Two strokes instead of one dashoffset path: a single revealed path
> looks like a progress bar tracing a shape. Splitting with a near
> imperceptible pause at the vertex reads as "drawn" rather than
> "rendered".

**Color:** set the accent green the instant `matched` fires -- do not
crossfade the hue. A hue shift during the collapse reads as a system
error correcting itself and undercuts the success.

### 5. Unlock morph (720ms, the actual "you're in" moment)

The iPhone-style "checkmark becomes the unlocked padlock".

- **0-35%:** checkmark shrinks to 60% scale while fading out.
- **20-100%:** a padlock body scales in with `EASE_OUT_BACK` (same
  overshoot language as expansion).
- **The shackle is the payoff.** Drawn as a separate arc pivoted at its
  own right leg; as `open` goes 0 -> 1 it rotates ~31 degrees and lifts
  slightly -- a swinging-open motion, not a fade or icon swap. The left
  leg visually shortens as it swings clear so it never clips the body.

> The swing is the single frame your eye locks onto as "the moment it
> happened". A static icon swap has no such frame.

After the unlock, the pill holds ~300ms so the completed state is
perceivable, then contracts back to the 52px capsule and fades --
mirroring the expand in reverse, slightly faster (260ms). Contractions
should always be quicker than expansions; it is an asymmetry the eye
expects from physical objects settling.

## Failure path

Rays go red immediately (no fade transition on the color -- a color that
eases from blue to red looks like a bug, not a rejection). The pill does
a short, tight, decaying shake -- roughly five steps, amplitude
10 -> 8 -> 6 -> 4 -> 2 -> 0 -- then holds 1.5s with "Hover to try
again", and only then contracts. Hovering backs out to the daemon and
rescans.

> A shake that doesn't decay looks like an animation stuck in a loop;
> decay is what says "this is settling". Same physical principle as the
> checkmark draw -- motion resolves toward rest.

Current implementation: `pill.js` feedback phase (`translation_x` with a
decaying sine) and `Rejected` phase in the glyph (frozen red ring +
cross through the face).

## The one architectural rule

**Exactly one clock drives everything.** A single 16ms timer advances a
phase value and a normalized `t` (0 -> 1 progress within the current
state), and every visual -- ring position, checkmark stroke length,
padlock rotation -- is a pure function of those two numbers. Nothing has
its own independent timer.

> The moment two animations run on separate clocks, you will eventually
> see one finish before the container has stopped resizing, or the
> padlock start growing while the checkmark is still fading -- a
> half-frame of incoherence that reads as a bug even if nobody can say
> why.

The pill owns the one `GLib.timeout_add(16)` and hands each frame's
normalized values to the glyph; `faceGlyph.js` owns no timers, no state
machine, and no easing state of its own -- it only draws what it is
told.

## Timing table

| Phase          | Duration | Easing                        | Note                        |
|----------------|----------|-------------------------------|-----------------------------|
| Waking/expand  | 340ms    | EASE_OUT_BACK (overshoot)     | text in at +90ms            |
| Sweep period   | 2000ms   | linear (crest)                | brightness floor <- progress|
| Match lead-in  | ~280ms   | EASE_IN_OUT_QUAD              | ring + face collapse        |
| Checkmark      | 620ms    | 2 strokes, EASE_OUT_CUBIC     | downstroke 0-38%, up 38-100%|
| Unlock         | 720ms    | EASE_OUT_BACK body / 31deg    | shackle swings open         |
| Hold           | 300ms    | -                             | completed state perceivable |
| Reject         | 1500ms   | decaying shake                | then hold "Hover to retry"  |
| Contract       | 260ms    | EASE_OUT                      | faster than expand          |