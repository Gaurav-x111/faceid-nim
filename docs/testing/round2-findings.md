# Round 2 findings — plain-language summary (2026-10-07)

Second black-box pass over faceid-nim, covering concurrency, resource
exhaustion, config/data fuzzing, upgrades, usability, cameras, secrets
and supply chain. Method: every claim below was measured or read in
code; anything needing root, a second user, or hardware is listed at
the bottom as not tested, not assumed.

## What held up

- **Logging in still always works.** Killed the daemon mid-scan: the
  login flow got an instant end-of-connection and fell through to the
  password with no hang. Real `sudo` ran a 4.7-second face scan, then
  asked for the password. Real `su` (not on the allowed list) was
  refused in 0 ms without ever opening the camera.
- **Two people at once don't break anything.** Three simultaneous
  unlock attempts queued politely and all finished; enrollment can
  never interleave frames with an unlock. If scans are slow, the
  second waiter gets a clean "face not recognised" after ~7 s and the
  password prompt — never a bypass, never a hang.
- **Garbage input bounces off.** 20 Rust fuzz tests (since reverted)
  plus live probes: broken configs fall back to *disabled*, tampered
  templates fail decryption, truncated files error instead of
  panicking, 100 pipelined requests get one scan and a closed
  connection, 5000-deep JSON nesting is rejected.
- **Models are now fully pinned.** The anti-spoof model got its hash
  pinned and fetched today, the worker was restarted, and the
  Diagnostics page truthfully reports "ML anti-spoof: active".
- **No secrets leak.** Two hours of logs contain no keys, embeddings
  or scores. File permissions match the design. Both vulnerability
  scans (Rust: 152 deps, Python: 34 pins) came back clean.

## What needs fixing, in order

1. **Any local user can watch the camera (HIGH).** The app's camera
   preview has no password check — this audit pulled real JPEG frames
   out of it with zero authentication. Needs the same password gate
   as enrollment.
2. **One program can fill the disk with log lines (MED-HIGH).** A
   single client wrote 1.7 MB/s of refusal lines; nothing caps the
   file between weekly rotations (~1 TB/week pace). Needs a size cap.
3. **Anyone can run unlimited face scans (MEDIUM).** The "test scan"
   button's backend has no password check and jumps the queue ahead of
   real unlock attempts. Gate it or count it in the existing scan
   limiter.
4. **Two packaging paper cuts (MED-LOW).** Uninstalling with `purge`
   leaves a dangling reference in the login config (harmless only
   because of the fall-through design), and upgrading wipes any local
   login-timeout tuning without warning.
5. **Small stuff (LOW).** Impossible embedding values aren't rejected
   at save time; the lock-screen animation defaults to on even with
   system reduced-motion; the Diagnostics page cuts off long lines;
   Python dependencies aren't locked to exact versions.

## Open questions (not bugs yet)

- The live worker's memory grew ~26 MB over two hours of testing.
  Probably just the allocator holding on, but it needs a quiet
  overnight run to be sure.
- Two real lock-screen attempts today were vetoed by the anti-spoof
  model with a face clearly in frame. If that was you at the screen,
  that's the recognition issue from Round 1 — it's the liveness
  check saying no, not the password, lockout, or threshold. Run
  `sudo faceid-nim audit 20` while looking at the camera to confirm.

## Not tested (no root / no second user / no hardware)

Real two-user simultaneous unlock, lock-screen-while-`sudo`, USB
camera unplug/replug, screen-reader audio, the live install/upgrade
itself, password-gated buttons pressed for real, and text scaling —
all listed with reasons in `known-gaps.md`.
