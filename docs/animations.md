# Lock-screen animations: choosing, adding, sizing — and how faces are matched

Answers to the questions every new user asks. File paths are
source-tree paths unless marked otherwise.

## Which animation plays? Only the selected one

Exactly ONE opening plays per scan: the id in
`~/.config/faceid-nim/openings.json` → `"active"`. The extension
re-reads that file on **every** scan (`shell-ext/faceid@nim/opening.js`,
`load()`), so changing the pick in the app applies at the very next
lock — no reload, no logout.

Built-ins (`app/faceid_app/openings.py`, `BUILTIN`):

| id | shown as | what it is |
|----|----------|------------|
| `iphone` | iPhone Video Pill (default) | the real unlock video |
| `logo` | App Logo | logo fade-in as the capsule grows |
| `glow` | Soft Glow | accent halo, no lettering |
| `none` | No splash | straight to the face scan |

If you ever see several animations "at once" (splash + scan glyph +
result card overlapping, or a success greeting replaced by a failure a
moment later), that is two scans overlapping on the bus — a slow older
scan finishing after a newer one — not two animations selected. The
extension now drops a terminal state (`matched`/`rejected`/`timeout`/
`camera_error`) that arrives within 3 s of an *opposite* terminal
(`shell-ext/faceid@nim/extension.js`, `_onState`), so the stale loser
can no longer overwrite the fresh result. The pill itself is
display-only: overlapping displays never changed an unlock decision.

One honest limitation: at the **GDM login screen** the extension runs
as the `gdm` user, which has no `openings.json`, so it plays the
`iphone` default there regardless of your choice. Your choice always
applies on your own lock screen.

## Adding or changing an animation

Two routes, both declarative — **no code (HTML/CSS/JS) ever runs on
the lock screen**. The extension interprets a fixed set of colours,
easing curves and one logo image; an installed animation can repaint
the pill but can never execute in the shell.

**Route 1 — a custom variant (logo + text + colours).** In the app's
Opening page, create a variant: name, status text, background/accent
colours (`#rrggbb`), easing (`outCubic`/`outBack`/`outExpo`),
fade 120–3000 ms, optional scale pop and progress ring, plus a logo
image (`SVG or PNG`, stored at
`~/.config/faceid-nim/openings/<id>/logo.svg`). That is the whole
format — the spec keys are `kind, name, description, logo, text, bg,
accent, ease, fade_ms, scale, ring` (`_clean_spec`).

**Route 2 — a `.faceopen` package (sharing).** A zip containing
`opening.json` + optional `logo.svg` + optional `preview.svg`, at most
2 MB / 20 files (`MAX_UNPACK_BYTES`, `MAX_UNPACK_FILES`). Install from
file or raw URL in the app. Publishing is just exporting the zip.

**Route 3 — replacing the iPhone video itself (advanced).** The app
previews `linux-anim/glance_anim/assets/unlockanimation.mp4` directly
(`Gtk.Video`). The lock screen cannot play mp4, so it plays an
extracted PNG frame sequence:
`shell-ext/faceid@nim/glance-frames/` (~37 tiny frames). To swap the
video: replace the mp4 (+ `unsuccessfulunlockanimation.mp4` for the
failure clip, `unlockstatic.png` for idle), then re-run
`packaging/scripts/extract-glance-frames` and keep the frame count
similar — every frame is decoded synchronously during the splash, so a
longer/heavier sequence slows the unlock animation. `app/tests/
test_glance_video.py` guards the mp4↔frames pairing in CI.

**Sizes that matter:** island capsule 320 px wide
(`ISLAND_W`, `pill.js`); scan glyph rendered at a physical 2.5 cm
(`glyphSizePx()`); status text is a single short line (state +
≤120-char reason from the daemon). Keep custom logos square-ish SVG —
they render inside the glyph square.

## How many faces? What happens with 5 people in frame?

- **Enrolling:** each identity holds **up to 15 templates**
  (`MAX_TEMPLATES`, `daemon/src/enroll.rs`); template files cap at 64.
  There is no fixed cap on identities per account — but every identity
  enrolled under your uid unlocks **your** account, so only enrol faces
  you want opening your machine.
- **Matching:** each scan collects up to 8 query embeddings
  (`max_embeddings`, need ≥3) from however many faces are visible,
  then k-of-n votes them against **all** your enabled templates
  (`daemon/src/matcher.rs`, `vote()`): each embedding takes its best
  cosine similarity across every template, and ≥3-of-5 (defaults
  `vote_k`/`vote_n`) above `tau` unlocks. So with 5 enrolled people in
  frame at once, **any one of them matching is enough** — the best
  template wins, strangers in the frame are simply outvoted, and the
  greeting names the winning identity.
- **After deleting:** with zero enabled identities the daemon now
  refuses **before the camera ever opens** (`Store::any_enabled`,
  `daemon/src/session.rs` + `test_scan`) and falls through to your
  password. If your camera light still comes on with nothing enrolled,
  you are running the old installed build — reinstall the current
  `.deb` (the fix is in source, not in 1.1.1-1 as installed).

## Photos and videos: will they unlock? (presentation attacks)

No — that is what the liveness stack is for, and it runs on **every**
unlock scan (never on enrollment, where a blink requirement would just
fail real users). Independent cues, any one of which vetoes the unlock
no matter how good the face match is (`vision/faceid_vision/fuse.py`,
`permits()`):

- **Printed photo:** fails planarity/parallax (a flat sheet has no 3D
  head motion), fails the blink/attention check (no live eyes), and at
  `heavy` strictness there is no positive live-face confirmation.
- **Phone-screen replay:** additionally trips moiré (FFT screen-pattern
  detector) and glare/device-frame cues.
- **Video replay:** same as screen replay, plus the challenge-response
  (head-motion prompt) at `heavy`.
- **IR sensor:** screen-dark and skin-response cues separate live skin
  from an emissive display.
- **ML anti-spoof:** MiniFASNet (`minifasnet_v2.onnx`, verified loaded
  when `faceid-nim status` says `antispoof: ok`) adds a learned
  print/replay verdict; when its model is absent the daemon records an
  `antispoof_model` cue instead of pretending the check ran.

Strictness `light` (default) = match + no veto. `heavy` = match + no
veto + positive confirmation (blink/parallax/challenge/IR skin). `off`
= match only, not recommended. **Plainly stated:** these cues are
implemented and wired, but resistance has NOT been measured with real
attack samples here (`eval/data/` is empty) — run
`eval/apcer_bpcer.py` with your own clips before quoting any number,
and expect small-n honesty (`INSUFFICIENT SAMPLES`) until you have
≥30 clips per attack type. Look-alikes (twins, family) are handled by
threshold, not liveness: measure your own `tau` with
`eval/far_frr.py`; the shipped 0.40 is a placeholder.
