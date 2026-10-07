# faceid-nim — Black-Box Audit, Round 2 (Areas 8–15)

Date: 2026-10-07. Auditor: automated session, user `zang` (uid 1000),
**no root** (`sudo` password unknown; used only where it needs no
password). Host: Ubuntu 24.04, kernel 7.0.0-38-generic, GNOME Shell 46,
Wayland session. Installed package: `faceid-nim 1.1.1-1` (built
2026-09-26 — **predates the working tree**; several tree fixes,
including the Oct-06 `sock_state` status fix, are NOT in the installed
binaries; noted per finding).

Method: every verdict below is backed by a quoted `file:line` or a
verbatim command result. Anything needing root / a second user /
hardware / a password dialog is marked BLOCKED or UNTESTED, never
assumed. Live `auth.sock` was spammed only for single PAM attempts;
bulk traffic ran against an isolated harness (own daemon pid 23810 +
own worker pid 19975, config/socket/audit-log under
`/tmp/opencode/harness/`, real camera, debug build of the same source).

Live state during the round: daemon pid 1431 (root), worker pid 11352
(user `faceid`, started 16:43:49 after the user pinned MiniFASNet and
restarted it — see 15.7). Effective live config (via `Diagnostics` +
`GetSettings`): camera `/dev/video1`, mode `rgb`, strictness `light`,
tau 0.4, vote 3-of-5, enabled, allowed_services
`[gdm-password, sudo, polkit-1, login, gnome-screensaver]`.

---

## Area 8 — Concurrency, multi-user, PAM behaviour

### Ran
- Live cross-user request on the real `/run/faceid-nim/auth.sock`
  (`{"op":"auth","user":"nobody","service":"test"}` as uid 1000).
- Live disallowed-service (`su`: service `su` not in allowlist) and
  unknown-user requests.
- 3-way concurrent auth on the harness (fast scans): timings + audit.
- 4-way worker-level concurrency direct on `vision.sock`
  (auth + enroll + auth + slow heavy scan): order + wall time.
- 2-way concurrent SLOW scans on the harness daemon (no face in room,
  ~4.6 s scans): queue-timeout behaviour.
- SIGKILL of the harness daemon mid-scan: client behaviour, socket
  residue, worker fd settling, restart.
- 20 mixed cycles (10 full + 5 abandoned-mid-scan + 5 double-fire):
  before/after fd/RSS/socket counts + process list.
- Live PAM: `su zang -c true` (×2, empty password) and
  `printf '\n' | sudo -S -k true` (empty password): timing, prompts,
  audit lines.

### Found
- 8.1 Cross-user, live: `elapsed=0.000s
  '{"kind":"denied","text":"not permitted"}'` + journal
  `WARN faceid_nimd::ipc: uid 1000 tried to authenticate uid 65534`.
  Matches `daemon/src/ipc.rs:185-197`
  (`Some(c) if c == 0 || c == target_uid`, else "not permitted").
  **PASS.**
- 8.2 Disallowed service, live: `su` → audit
  `{"result":"refused","reason":"service_not_allowed","elapsed_ms":0}` —
  refused at 0 ms, camera never opened. Total `su` wall time 3.0–3.8 s
  is system PAM/nss overhead, not faceid (daemon answered in 0 ms).
  Unknown user → `{"kind":"unavail","text":"unknown user"}` instantly.
  **PASS.**
- 8.3 Real PAM fall-through, live: `sudo` (allowed) ran a genuine
  4.737 s face scan
  (`service: sudo, result: timeout, reason: worker: no_usable_face,
  elapsed_ms: 4737`) and THEN showed `[sudo] password for zang:`.
  Two password retries = two full rescans (4737 ms + 4600 ms audit
  lines). Deny/unavail both map to fall-through via the installed
  profile `auth [success=3 default=ignore] pam_faceid.so quiet
  timeout=4500` (`/etc/pam.d/common-auth` line 1) and the module's own
  mapping (`pam/pam_faceid.c:246-253`: denied → `PAM_AUTH_ERR`,
  unavail/timeout/EOF → `PAM_AUTHINFO_UNAVAIL`, both ignored by the
  stack). No hang, no lockout (no faillock in stack). **PASS.**
- 8.4 Concurrent scans serialize strictly FIFO at the worker.
  Worker `_serve` accepts one connection at a time
  (`vision/faceid_vision/__main__.py:249,264`: `srv.listen(4)` then a
  serial `srv.accept()` loop). Measured: 3 concurrent harness auths
  completed in order at 0.8 / 1.5 / 2.2 s (audit completions 0.7 s
  apart); 4-way direct probe (auth+enroll+auth+slow) finished at
  4.7 / 9.3 / 18.0 / 22.7 s, wall 22.7 s. Each request opens/closes the
  camera itself, so enrollment frames can never interleave with auth
  frames. Simulated "lock + sudo at once" = both queue, both complete
  while scans are fast. **PASS (with the 8.5 caveat).**
- 8.5 Queued slow scans hit the daemon budget: with ~4.6 s scans, two
  concurrent harness auths gave client 1 `no_identities` at 3.78 s and
  client 2 `denied "Face not recognised"` at 8.53 s, audit
  `result: timeout, reason: worker: no_usable_face, elapsed_ms: 8528`.
  Mechanism: `daemon/src/worker.rs:291`
  `let budget = Duration::from_millis(timeout_ms + 3000)` (7 s here);
  request 2 is served at ~4.6 s and replies at ~9.2 s, past budget, so
  the daemon denies while the worker is still scanning. User-visible
  text is the generic "Face not recognised" (denied → PAM ignores →
  password). Not a hang, not a bypass — but under load the *second*
  simultaneous user always loses their face attempt. **BEHAVIOUR
  DOCUMENTED (no fix demanded; PAM falls through cleanly).**
- 8.6 Daemon death mid-scan: SIGKILL → client got EOF, exit 1, no
  hang (PAM analog: `read_reply` `n <= 0` → `-1` →
  `PAM_AUTHINFO_UNAVAIL`, `pam_faceid.c:126-133`). Stale `auth.sock`
  remains after SIGKILL (only graceful shutdown unlinks,
  `daemon/src/main.rs:113`); restart works because `serve()` unlinks
  a stale socket first (`daemon/src/ipc.rs:53-55`). Worker finished
  the orphaned scan (fds 14 → 16 → settled 14), no leak, no orphan
  process. Units self-heal a real crash: daemon `Restart=on-failure`,
  worker `Restart=always` (`packaging/systemd/*.service:16-17,22-23`),
  2 s delay, PAM falls through meanwhile. `systemctl restart`
  mid-scan itself is BLOCKED (needs root); this is the closest
  equivalent. **PASS.**
- 8.7 20-cycle soak: daemon fds 10 → 10, worker fds 14 → 14, unix
  entries 2 → 2, exactly one daemon + one worker before and after,
  daemon RSS +144 kB (allocator noise), worker RSS +30 MB then flat
  (see 9.3). **PASS (no fd/socket/process leak).**

| # | Sub-item | Verdict |
|---|---|---|
| 8.1 | cross-user denied pre-scan | PASS |
| 8.2 | disallowed service / unknown user instant | PASS |
| 8.3 | live PAM fall-through (su + sudo) | PASS |
| 8.4 | concurrent scans serialize, enroll never interleaves | PASS |
| 8.5 | queued slow scan → budget timeout → clean deny | DOCUMENTED |
| 8.6 | daemon death mid-scan → EOF, stale sock, clean restart | PASS |
| 8.7 | 20 mixed cycles, no leaks/orphans | PASS |
| 8.8 | real two-uid simultaneous auth | BLOCKED (no 2nd account; engine has per-uid locks + shared `scan_slots: Semaphore(2)` at `session.rs:230`, `main.rs:77` — no lock-ordering hazard by inspection) |
| 8.9 | lock-screen + sudo at the same instant | BLOCKED (screen-lock risk without the password) |

---

## Area 9 — Resource exhaustion, busy camera, soak

### Ran
- Audit-log growth under `service_not_allowed` spam on the harness
  (500 requests, single client).
- Camera-busy: `v4l2-ctl --stream-mmap` holding `/dev/video1` during a
  scan; nonexistent device; no-signal loopback; O_EXCL hold; recovery.
- 20-cycle fd/RSS + 30-min soak (run 1) + 12-min controlled soak with
  explicit PIDs (run 2, still sampling at report time; interim numbers).
- CPU: idle (10 s window) and active-scan (3 s window) tick counts for
  both workers + live daemon.

### Found
- 9.1 **FAIL (medium-high): unbounded audit-log growth.** One client
  sustained **10,882 req/s**; the log grew **1.7 MiB/s ≈ 6.2 GiB/h**,
  i.e. ~1 TiB across the weekly rotation window, from a single
  client. Every pre-auth refusal logs a line; `MAX_CONNECTIONS = 64`
  (`daemon/src/ipc.rs:24`) bounds concurrency, not rate (each refusal
  completes instantly). `/etc/logrotate.d/faceid-nim` rotates
  **weekly, `rotate 4`, compress — with no `size` cap** (quoted
  verbatim in evidence). `/var/log` is on the root fs (`df`: 468 G,
  **99 % full, 7.2 G free at audit time**), so at the measured rate a
  single client fills the remaining disk in about an hour. Local
  log-partition DoS. Fix: `size` cap in the logrotate stanza
  and/or rate-limiting pre-auth refusals. (The journal copy of the
  same lines IS bounded by journald's own caps.)
- 9.2 Camera busy: UVC multiplexes, so the scan proceeded at a
  driver-downgraded **640x360 @15** with a loud warning
  (`camera … refused the requested advertised format MJPG 640x480@30;
  driver negotiated MJPG 640x360 @15fps`, `camera.py:313-318`),
  completed in 0.82 s, recovered cleanly after release. Degraded but
  honest — **PASS.**
- 9.3 Hard camera failures: nonexistent device and no-signal loopback
  both → `camera_unavailable: CameraError` in **0.16 s** (bounded:
  `_open_with_retry` = 2 attempts, 150 ms). Daemon maps any
  `*camera*` reason to unavailable (`session.rs:277`) → PAM UNAVAIL
  → password. **PASS.** (O_EXCL is not honoured by uvcvideo — both
  opens succeeded; exclusive-locking the camera is not a mechanism
  here. Informational.)
- 9.4 Soak: harness daemon fds 10 → 10, RSS flat (~15 MB debug);
  harness worker fds 14 → 14, unix entries 2 → 2 (transients 12 fds /
  4 entries = clients mid-scan at sample time, settled after), worker
  RSS +30 MB across the 20 forced cycles then ±600 kB over 30 min and
  +124 kB over a controlled 12-min run with explicit PIDs (11 scans):
  warmup/arena, not a leak. Live daemon RSS 6.6 → 7.2 MB flat; live
  worker flat at ~337 MB across the controlled run (the +26 MB in 9.5
  came from the active test burst, then held steady). **PASS except
  9.5 stays WATCH.**
- 9.5 **WATCH (not a verdict): live worker RSS 310 → 337 MB over ~2 h
  of test load** (~12 live scans + previews). CPython RSS retention
  vs genuine leak cannot be distinguished from outside; needs a
  controlled idle soak (or allocator introspection). Recorded for the
  next round, not filed as a leak.
- 9.6 CPU: 0% for all three processes over a 10 s idle window (no
  polling loops); ~54% of one core for the worker during an active
  scan on this 8-core box. Sane. **PASS.**

| # | Sub-item | Verdict |
|---|---|---|
| 9.1 | audit-log growth under spam | **FAIL** (no rate or size bound; ~1 TiB/week/client) |
| 9.2 | camera busy → degraded + warned + recovered | PASS |
| 9.3 | camera missing/no-signal → 0.16 s clean unavail | PASS |
| 9.4 | fd/socket counts stable over soak | PASS |
| 9.5 | live worker RSS +26 MB/2 h | WATCH (arena vs leak unresolved) |
| 9.6 | CPU idle 0 %, scan ~54 % of one core | PASS |

---

## Area 10 — Config, template, manifest and D-Bus fuzzing

### Ran
- 20 temporary Rust fuzz tests (appended to `config.rs` / `store.rs` /
  `matcher.rs`, run, then **reverted byte-identical** — md5 verified;
  suite back to 32/32 green, `fmt --check` + `clippy -D warnings`
  clean): NaN/Inf/negative tau, every clamp in both directions,
  garbage/duplicate/UTF-8-invalid/unknown-key/huge configs, exact
  allowlist matching, header magic/version/dim/count mutations,
  truncation at 10 lengths, ciphertext + model_id bit flips, corrupt
  files in `load_enabled`, non-finite embeddings, delete_all,
  NaN/Inf/τ-NaN matcher behaviour, vote windowing, tie order.
- Live D-Bus battery (system bus, app's own Gio client pattern):
  `TestScan`, `StartPreview` (+ JPEG decode), wrong-type
  `SetSettings`/`Enroll`/`SetPamEnabled`, `DeleteIdentity` with
  traversal / 1000-char / `..` / slash / empty names, `Diagnostics`
  after.
- Auth-socket battery on the harness: 20 KB line, 7 KB line, partial
  close, missing newline, 5000-deep nesting, unknown op, missing
  fields, wrong types, NUL byte, 100 pipelined requests.
- App string grep: `set_markup` (none), accuracy claims (none —
  only "Your password always works").

### Found
- 10.1 **HIGH: `StartPreview` streams the camera to any local caller
  with no polkit check** (`daemon/src/dbus.rs:611-615` — only
  `caller_uid`; cf. `Enroll` at :143 and `SetSettings` at :480 which
  both call `require_polkit`). Proven live: `StartPreview` returned a
  real fd, and the pipe delivered `G` gate records plus valid JPEG
  frames (6765 / 6667 bytes, SOI `ffd8`), repeatably, with no prompt.
  A user outside the `video` group can watch the camera. Fix: require
  `ACTION_ENROLL`-class auth (or at minimum active-session +
  camera-group membership) before handing out the fd.
- 10.2 **MEDIUM: `TestScan` is ungated AND bypasses `scan_slots`.**
  `dbus.rs:287-291` takes no polkit and calls `engine.worker.scan`
  directly; the `scan_slots` semaphore (`main.rs:77`, `session.rs:230`)
  is only taken in `authenticate()`. Proven live: full 4.71 s camera
  scan, `(False, 'worker: no_usable_face')`, zero auth. Any local user
  can burn camera/CPU at will and — worse — because the worker is
  strict FIFO, repeated `TestScan` calls starve real PAM auth scans
  into the 7 s budget timeout (8.5). Availability of *face unlock*
  only (login still falls through to password), but a deliberate
  multi-user DoS of the feature.
- 10.3 Wrong-type D-Bus args are rejected by zbus marshalling
  (`Signature mismatch … (36)`) in 0.00 s, before any handler or
  polkit runs — no dialog can be triggered this way. **PASS.**
- 10.4 `DeleteIdentity` (polkit `yes`, own uid only) rejects
  traversal/1000-char/`..`/slash/empty with `invalid identity name`
  (store `path()`, `store.rs:125-139`) in 0.00 s. **PASS.**
- 10.5 Valid-type `SetIdentityEnabled` hit `auth_self` and ran a real
  polkit flow (face scan 4705 ms for service `polkit-1`, then
  one-shot authorization granted at 17:36:54). Test-design error by
  the auditor (meant to use wrong types only); disclosed to the user.
  No further valid-type gated calls were made. Side observation: the
  flow works end-to-end. **NO VERDICT (procedure, not product).**
- 10.6 Rust fuzz: 19/20 pass. Config: NaN/±Inf/negative tau → clamp
  0.9 fail-closed (`config.rs:189-206`, called at startup
  `main.rs:48`); all clamps both directions; every garbage class →
  all-defaults with `enabled = false` (fail closed); unknown keys
  ignored; empty allowlist closed; allowlist exact-match
  (`"SUDO"`, `"sudo "`, `"sudo\n"` all refused). Store: save rejects
  empty/zero/oversize/inconsistent shapes and all bad names;
  magic/version/dim=0/count=0/count=65/dim=2049 mutations rejected;
  in-range-but-wrong dim caught by the body-length check (dim/count
  are not in the AAD — the length equation covers it for all
  single-field changes); truncation at all 10 lengths errors, never
  panics; ciphertext/model_id bit flips → `authentication failed`;
  `load_enabled` skips garbage files; `delete_all` total +
  idempotent. Matcher: NaN template/query/τ all fail closed; window
  is exactly last-n; ties pick index 0. **PASS.**
- 10.7 **LOW (defence in depth): `save()` accepts non-finite
  embeddings** — the `round2_non_finite_embeddings_are_rejected_on_save`
  test failed with `NaN embedding accepted` (`store.rs:143-156`
  checks shapes but never finiteness). Companion documentation test:
  an `Inf` query dot-products to +Inf against *any* same-dim
  template and clears τ. Inputs today come only from ONNX output over
  a root-owned socket (attacker needs root already), so severity is
  low — but a finite-check at save/load would close it for ~3 lines.
- 10.8 Socket battery: all clean. 20 KB line → `bad request`
  (`MAX_LINE` 8 KB, `ipc.rs:23`); 7 KB valid JSON → parsed
  (`unknown user`); partial/no-newline → silent close at the 4 s
  `IDLE_READ_TIMEOUT` (`ipc.rs:31,151-164`); 5000-deep nesting →
  `bad request` (serde recursion limit, no panic); unknown op /
  missing / wrong-typed / NUL → precise `unavail` errors; 100
  pipelined requests → `ECONNRESET` after the first, by design:
  `handle()` serves **one request per connection**
  (`ipc.rs:134-184`; extra bytes are discarded unread, so no request
  smuggling); daemon alive and serving afterwards. **PASS.**

| # | Sub-item | Verdict |
|---|---|---|
| 10.1 | StartPreview camera stream without auth | **FAIL (HIGH)** |
| 10.2 | TestScan ungated + bypasses scan_slots | **FAIL (MEDIUM)** |
| 10.3 | wrong-type args rejected pre-polkit | PASS |
| 10.4 | delete-name validation | PASS |
| 10.5 | valid-type gated call (procedure error) | NO VERDICT |
| 10.6 | Rust config/store/matcher fuzz (19/20) | PASS |
| 10.7 | non-finite embeddings accepted | **FAIL (LOW)** |
| 10.8 | auth-socket malformed battery | PASS |

---

## Area 11 — Upgrade, downgrade, migration, models pipeline

### Ran
- `apt-get -s install`, `apt-cache policy`, control-field diff of the
  1.0.2 / 1.1.0 / 1.1.1 debs in `dist/`.
- `prerm` / `postrm` / `postinst` argument handling (upgrade vs
  remove vs purge); `debian/rules` systemd overrides; shipped vs
  active PAM profile.
- `strings` probes of the 1.0.2 / 1.1.0 / 1.1.1 daemon binaries for
  config literals (downgrade compatibility).
- `fetch-models` lab in temp dirs (fetch / skip-verified / tamper →
  re-fetch); `verify-models` lab (match rc 0 / mismatch rc 1).
- Postinst config-keep rule; `.bak` file provenance; installed
  `/usr/bin/faceid-nim` vs tree `bin/faceid-nim` diff.

### Found
- 11.1 Upgrade mechanics sane: same package name, version-ordered,
  no Breaks/Replaces needed; 1.0.2 → 1.1.0 only adds app deps
  (python3-gi, gir1.2-*, libglib2.0-bin). `postinst` writes
  `/etc/faceid-nim/config.toml` **only if absent** (installed
  `postinst:36`: `if [ ! -f … ]`) — upgrades never clobber tuning;
  models fetched non-fatally; all three units `enable --now` on
  configure, so the `postrm`-on-upgrade `disable --now` (which defeats
  `rules:215-217` `--no-stop-on-upgrade`) self-heals. Brief
  interruption, ends enabled. **PASS with notes.**
- 11.2 **MEDIUM-LOW: `prerm` ignores `purge`.** `packaging/debian/prerm`
  matches only `remove|deconfigure`; on purge the PAM profile stays
  registered while the `.so` is deleted, leaving a dangling
  `pam_faceid.so` reference in `common-auth`. Saved from lockout only
  by `default=ignore` (module-unknown → falls through to password,
  with log spam). One-line fix: add `purge` to the case.
- 11.3 **MEDIUM-LOW: local PAM tuning is wiped on upgrade.**
  Active `/etc/pam.d/common-auth` carries
  `[success=3 default=ignore] pam_faceid.so quiet timeout=4500`,
  but the shipped profile (`/usr/share/pam-configs/faceid-nim`, and
  the 1.1.1 deb) says `[success=end …] timeout=10000` — someone tuned
  the live stack down. `postinst` runs `pam-auth-update --package`
  unconditionally on every configure, which regenerates `common-auth`
  from the shipped profile and discards the local edit (face wait
  4.5 s → 10 s, `success=3` → `end`). Behaviour change, not lockout —
  but the tuner gets no warning.
- 11.4 Downgrade: 1.0.2 binary lacks `auto` (has only `both`); 1.1.0
  already has `auto` + `timeline_enabled` + `exclude_users`. With the
  *current* live config (mode `rgb`, known enums) a 1.1.x → 1.0.2
  rollback still parses (unknown *keys* are ignored). A config using
  `mode = "auto"/"hybrid"` under a 1.0.2 daemon fails the whole
  parse → all-defaults → `enabled = false`: face unlock silently off
  after downgrade. Fail-closed, but silent. **DOCUMENTED.**
- 11.5 Model pipeline verified end-to-end in temp dirs:
  `fetch-models` fetched + verified (`verified -> …`), skipped
  verified on rerun, re-fetched after tampering (`present but
  checksum differs; re-fetching`, byte-identical restore), refused
  the unpinned entry honestly. `verify-models`: match → `ok`, rc 0;
  1-byte append → `CHECKSUM MISMATCH … run: sudo faceid-nim
  fetch-models`, rc 1. **PASS.**
- 11.6 Stale-script note (LOW): `packaging/scripts/setup-auth.sh`
  backs up then **unconditionally overwrites** the config
  (`cp …; chmod 644`) — it wrote the mystery
  `/etc/faceid-nim/config.toml.bak.1789931970` on Sep 21. It is NOT
  shipped in the .deb (only `pam-configs/faceid-nim` ships), so this
  bites only manual runs. Doc/deprecate it.
- 11.7 Installed CLI is stale (info): `/usr/bin/faceid-nim` predates
  the Oct-06 `sock_state` fix (diff proved), so as a plain user it
  still prints `vision.sock : MISSING` + `worker not running` for a
  live worker (the worker dir is `drwxrwx--- root faceid`,
  untraversable). `sudo faceid-nim status` is accurate. Refresh on
  next install.

| # | Sub-item | Verdict |
|---|---|---|
| 11.1 | upgrade keeps config, self-heals units | PASS (brief interruption noted) |
| 11.2 | prerm misses purge → dangling PAM ref | **FAIL (MED-LOW)** |
| 11.3 | local PAM tuning wiped on upgrade | **FAIL (MED-LOW)** |
| 11.4 | downgrade parse behaviour | DOCUMENTED (fail-closed) |
| 11.5 | fetch/verify models pipeline | PASS |
| 11.6 | setup-auth.sh overwrite (unshipped) | LOW note |
| 11.7 | installed CLI predates tree | INFO |

---

## Area 12 — App, usability, accessibility

### Ran
- Headless run of the tree app under Xvfb (`GDK_BACKEND=x11`,
  screenshots at 0/3/8 Tabs, xdotool-driven); compile check; string
  greps (markup, accuracy claims, animation guards, tooltips, fixed
  sizes); live Diagnostics + Cameras pages read from screenshots;
  desktop a11y settings; WCAG contrast math on the pill CSS.
- (Procedure incident, disclosed: the first two runs inherited
  `WAYLAND_DISPLAY` and opened the Face Unlock window on the user's
  live session; killed after 40/60 s. No input was sent, no settings
  changed.)

### Found
- 12.1 App launches clean headless (only benign Gtk/Adwaita theme
  warnings), reads live daemon state (Diagnostics page showed the
  real provider list, thread counts, `antispoof=True`,
  `landmarks=mediapipe_tasks`, camera, strictness). Tab keyboard
  navigation works across sidebar pages (reached Cameras and
  Diagnostics; selection highlight visible). **PASS.**
- 12.2 Copy is honest: "1 face enrolled — ready", "Your password
  always works", per-camera Test buttons, "IR means its only formats
  are monochrome; the app never labels a camera IR on suspicion
  alone." No accuracy percentages anywhere. **PASS.**
- 12.3 Pill contrast all ≥ WCAG AA on the dark pill (title 13.2,
  ok-green 12.2, err-red 7.2, faintest secondary 5.9:1). **PASS.**
- 12.4 Reduced motion is respected where it matters: the enrollment
  scan ring checks `gtk-enable-animations` and degrades to static
  progress redraws (`onboarding.py:660-672`, "progress is never
  conveyed by animation alone"); `Gtk.Stack` CROSSFADE is
  toolkit-gated by the same setting. **PASS.**
- 12.5 **LOW: the lock-screen animation preference defaults to on
  even under reduced motion.** `prefs.py:35`
  `"animations_enabled": True`, UI toggle at `main.py:992-999`, but
  nothing seeds it from the system `enable-animations` (this desktop
  has it `false`). Onboarding only uses the system setting for the
  scan ring. A reduced-motion user gets the lock-screen animation
  unless they find the toggle.
- 12.6 **LOW: Diagnostics rows don't wrap** — long values
  (`arch=x86_64; cpu_count=8; features=[…]`) run off the visible area
  at 860 px width. Copy affordance exists below, but the row itself
  is unreadable in place.
- 12.7 Cosmetic LOWs: camera names ellipsize mid-word
  ("Integrated_Webcam_FHD: Integrat" — app-side, the kernel name is
  longer); fixed 640×480 window / 280×280 preview px
  (`main.py:256,1417`) won't follow text scaling; enrolled identity
  is named `zangg` (double-g — user's own data, presumably a typo at
  enrollment; the daemon compares it fine).
- 12.8 AT-SPI property audit: BLOCKED (no pyatspi); Orca audio output:
  BLOCKED (unverifiable here); live text-scaling 1.5×: BLOCKED (would
  mutate the live session). Substituted with the static + screenshot
  evidence above.

| # | Sub-item | Verdict |
|---|---|---|
| 12.1 | headless launch + keyboard nav + live data | PASS |
| 12.2 | honest copy, no accuracy claims | PASS |
| 12.3 | pill contrast ≥ AA | PASS |
| 12.4 | reduced-motion paths | PASS |
| 12.5 | animation pref defaults on under reduced motion | **FAIL (LOW)** |
| 12.6 | diagnostics overflow | **FAIL (LOW)** |
| 12.7 | cosmetic (ellipsis, fixed px, identity name) | INFO |
| 12.8 | AT-SPI / Orca / live scaling | BLOCKED |

---

## Area 13 — Camera diversity, formats, resolutions

### Ran
- `v4l2-ctl --list-formats-ext` on video1 (UVC colour) and video3
  (GREY); app Cameras page (screenshot) vs kernel truth.
- Direct worker scans: `mode=ir` on `/dev/video3` (GREY 640x360@15);
  `mode=auto` on `/dev/video1` (lit room); loopback video0
  (no-signal) path; contention downgrade (Area 9).

### Found
- 13.1 video1 advertises MJPG first (`[0]: MJPG 1920x1080@30`, then
  smaller sizes) and YUYV second. The worker requests **MJPG
  640x480@30** — i.e. it picks the scored sane default, not the
  first enumerated format — and `_verify` warns loudly when the
  driver renegotiates (observed 640x360@15 under contention, scan
  still completed). **PASS.**
- 13.2 video3 is GREY-only 640x360@15; the app classifies it IR and
  says why (mono-only rule). A direct `mode=ir` scan negotiated
  `GREY 640x360 @15fps` ("frames arrive as 3-channel (BGR)" —
  converted internally) and ran the full 3 s pipeline, ending
  `no_usable_face` with no crash. **PASS** (1-channel path works).
- 13.3 `mode=auto` in a lit room logged `auto: luma=105.2 -> rgb`
  — quantitative proof the meter runs and resolves per scan.
  **PASS.**
- 13.4 Loopback video0 (many packed formats, no producer): worker
  requested advertised BGR3, retried once, then clean
  `camera_unavailable` (0.16 s). The app lists it as accessible with
  `n/a` resolution and its real format list — honest. **PASS.**
- 13.5 video2/video4 (same physical webcam's metadata nodes): app
  shows `?` + `no access` — honest, no bogus classification.
  **PASS.**
- 13.6 Physical USB unplug/replug of the internal webcam: BLOCKED
  (needs root unbind).

| # | Sub-item | Verdict |
|---|---|---|
| 13.1 | format scoring (not first-enumerated) + verify warning | PASS |
| 13.2 | GREY/IR path end-to-end | PASS |
| 13.3 | auto meter resolves (luma logged) | PASS |
| 13.4 | no-signal source fails clean, app honest | PASS |
| 13.5 | inaccessible nodes shown as such | PASS |
| 13.6 | physical replug | BLOCKED |

Camera matrix (this machine, 2026-10-07):

| node | kernel formats | app label | worker behaviour |
|---|---|---|---|
| video0 | BGR4/RGB4/BGR3/… (loopback, no signal) | RGB, accessible, n/a res | BGR3 requested, 1 retry, clean `camera_unavailable` |
| video1 | MJPG…1920x1080 first, YUYV second | RGB, 1920x1080, MJPG,YUYV | MJPG 640x480@30; contention → 640x360@15 + warning, still scans |
| video2 | (metadata node) | ?, no access | n/a (never selected) |
| video3 | GREY only, 640x360@15 | IR (mono-only rule) | GREY negotiated → BGR, full scan, no crash |
| video4 | (metadata node) | ?, no access | n/a |

---

## Area 14 — Secrets, data at rest, crash handling

### Ran
- 2 h journal grep (daemon + worker) for keys/scores/embeddings;
  audit-line field inventory; file/dir/mode audit
  (`/var/lib/faceid-nim`, `master.key`, `/var/log/faceid-nim/*`,
  `/etc/faceid-nim/*`, models); `/var/crash` + `/proc/swaps` +
  swappiness; `mlock` grep; SECURITY.md read; worker-stop journal
  noise.

### Found
- 14.1 No keys, embeddings, scores or frames in 2 h of journals;
  audit lines carry only cues/measurements
  (`deny_cues`, `moire_score`, `valid_frames`, `spectrum`). (Round 1
  noted identity names can appear in `reason` on success; no success
  occurred this round to re-verify.) Matches the design + Round-1's
  no-biometric-in-logs test. **PASS.**
- 14.2 On-disk layout matches the documented design exactly:
  state dir `711 root root`, `master.key` `600`, `audit.log` `600`
  root with rotation (`audit.log.1`, `audit.log.2.gz`, weekly per
  logrotate), config `600` (daemon runs `UMask=0077`,
  `faceid-nimd.service:71`, so D-Bus rewrites land 600),
  models `644` (public files by design). `timeline.jsonl` absent —
  the feature is off, so nothing extra is written. **PASS.**
- 14.3 **INFO: swap exposure without `mlock`.** No `mlock`/`VirtualLock`
  in `daemon/src/`; the box has a disk-backed `/swap.img` (4 GB,
  priority -1, 0 used) under a preferred `zram0` (priority 5).
  Daemon memory (key, plaintext templates) *can* reach spinning
  rust if tuned off zram. This is inside the documented threat model
  (SECURITY.md: FDE is the baseline; "attacks by root" and
  removed-disk reads are out of scope), so: no finding, recorded
  residual.
- 14.4 `/var/crash` is stock apport (`drwxrwsrwt root whoopsie`);
  `core_pattern` pipes to apport (Round-1 brief); units set no
  `LimitCORE` either way. Standard Ubuntu posture, no faceid-specific
  gap. Worker stops print a mediapipe `FaceLandmarker.__del__`
  traceback into the journal (interpreter-teardown noise, cosmetic).
- 14.5 SECURITY.md scope/invariants read and consistent with the
  code sampled this round (independent recognition/liveness —
  `session.rs` denies on either; no scores to callers — `MatchOutcome`
  only surfaces via a root-only debug line; worker never gets
  templates; UNAVAIL on failure). Invariant 7 ("every model
  checksum-pinned") is now true *including* antispoof (see 15.7).
- 14.6 Field observation (links Round-1 pending): two genuine live
  `gdm-password` denials today (17:33:53, 18:07:52), both
  `valid_frames=1` vetoed by `antispoof_model` in 0.8–1.6 s with a
  face in frame. If the person in front of the camera was the user,
  this is the personal non-recognition cause the earlier session
  wanted `audit 20` for: **a liveness (ML) veto, not a lockout or a
  threshold miss.** Recommend the user confirm with
  `sudo faceid-nim audit 20` while looking at the camera.

| # | Sub-item | Verdict |
|---|---|---|
| 14.1 | no secrets/scores in logs | PASS |
| 14.2 | on-disk perms match design | PASS |
| 14.3 | swap without mlock (in-model residual) | INFO |
| 14.4 | crash handling stock, stop-time traceback | INFO |
| 14.5 | SECURITY.md consistent with code | PASS |
| 14.6 | live liveness-veto observation | INFO (see Round-1 thread) |

---

## Area 15 — Supply chain, pins, licences

### Ran
- `cargo audit` (installed v0.22.2 for this round) on `daemon/`.
- OSV `querybatch` over all 34 pinned venv packages; `pip-audit -r`
  attempt on the frozen venv.
- License inventory over the 152-crate graph; crate + venv + shell-ext
  surface listing; manifest pin state (incl. today's antispoof change).

### Found
- 15.1 `cargo audit`: **clean** — 152 dependencies, rc 0, no
  advisories. **PASS.**
- 15.2 OSV over the installed venv pins (numpy 2.5.3, onnxruntime
  1.30.0, mediapipe 0.10.14, pillow 12.3.0, … — 34/34): **zero
  advisories.** **PASS.**
- 15.3 **LOW: the frozen venv is not pip-resolvable.**
  `pip-audit -r venv-freeze.txt` fails with
  `ResolutionImpossible … protobuf==7.36.2 … conflicting
  dependencies` — the venv was assembled with mixed installers
  (`--no-deps` or equivalent), so `pip install -r` cannot reproduce
  it. Reproducibility wart, not a vulnerability.
- 15.4 Venv bloat note (LOW): the worker venv carries jax/jaxlib,
  matplotlib, scipy, sounddevice and *both* opencv-contrib and
  opencv-headless. matplotlib already left a journal warning
  (`mkdir …/.config/matplotlib: Read-only file system`) on every
  worker start. Unused weight in a root-adjacent service.
- 15.5 Licences: 87× MIT OR Apache-2.0, 30× MIT, 24× Apache-2.0 OR
  MIT, rest permissive; the only copyleft-shaped entry is `r-efi`
  (MIT OR Apache-2.0 OR LGPL-2.1-or-later — permissive choice
  available). Crate itself MIT OR Apache-2.0. **PASS.**
- 15.6 **Python deps remain range-pinned** (`numpy>=1.24`,
  `opencv-python-headless>=4.9,<5`, `onnxruntime>=1.17`,
  `mediapipe>=0.10,<1.0` in `vision/pyproject.toml`; no committed
  requirements file; only `daemon/Cargo.lock` is locked). Drift is
  already visible (installed mediapipe 0.10.14 vs 0.10.35 named in
  the brief). Same standing finding as the brief's: silent rebuild
  drift. **FAIL (LOW — hygiene, unchanged).**
- 15.7 **Round-1 gap CLOSED by the user today: MiniFASNet is now
  pinned and live.** Journal-proven sequence 16:43:02–16:43:49
  (`sudo cp models/manifest.toml …`, `faceid-nim fetch-models`,
  `systemctl restart faceid-vision.service`, all from the repo dir):
  manifest now pins `sha256 = "d7b3cd9b…"` (`optional = true`),
  the on-disk file hashes exactly that, the restarted worker loads
  it, and the `antispoof_model` cue genuinely fires (Areas 8/12/14).
  Provenance comment names the garciafido HF export of upstream
  2.7 weights with a verify-it-yourself instruction. Remaining nit:
  the manifest comment "While it stays unpinned:
  resolve_optional() refuses…" (line 53) is now stale. **CLOSED
  (update the comment).**
- 15.8 Shell extension: pure GJS, no `package.json`, no
  `node_modules` — minimal surface. **PASS.**

| # | Sub-item | Verdict |
|---|---|---|
| 15.1 | cargo audit clean | PASS |
| 15.2 | OSV over venv pins clean | PASS |
| 15.3 | frozen venv not pip-resolvable | **FAIL (LOW)** |
| 15.4 | venv bloat | INFO |
| 15.5 | licence surface permissive | PASS |
| 15.6 | Python ranges unpinned, no lockfile | **FAIL (LOW, standing)** |
| 15.7 | MiniFASNet pinned + live (user, today) | CLOSED |
| 15.8 | shell-ext minimal surface | PASS |

---

## Consolidated findings (new in Round 2)

| ID | Severity | Finding | Evidence |
|---|---|---|---|
| R2-1 | HIGH | `StartPreview` streams camera JPEGs to any local caller, no polkit | live fd + decoded `ffd8` frames; `dbus.rs:611-615` vs polkit on `:143`,`:480` |
| R2-2 | MED-HIGH | audit log unbounded: ~10.9k req/s → 1.7 MiB/s → ~1 TiB/week/client (7.2 GiB free at audit time: ~1 h to fill); logrotate weekly, no `size` | harness measurement; `/etc/logrotate.d/faceid-nim` (no `size`); `ipc.rs:23-24`; `df /var/log` 99 % |
| R2-3 | MEDIUM | `TestScan` ungated + bypasses `scan_slots` (queue-starvation of real auth) | live 4.71 s scan, no auth; `dbus.rs:287-291`; slots only at `session.rs:230` |
| R2-4 | MED-LOW | `prerm` ignores `purge` → dangling `pam_faceid.so` ref (saved by `default=ignore`) | `packaging/debian/prerm` case list |
| R2-5 | MED-LOW | local PAM tuning (4500/`success=3` vs shipped 10000/`end`) wiped by `pam-auth-update --package` on every upgrade | active `common-auth:1` vs shipped profile; `postinst:196` unconditional |
| R2-6 | LOW | `save()` accepts NaN/Inf embeddings; Inf query clears τ vs any template | failing fuzz test `NaN embedding accepted`; matcher doc-test |
| R2-7 | LOW | frozen venv not pip-resolvable (protobuf conflict) | `pip-audit -r` `ResolutionImpossible` verbatim |
| R2-8 | LOW | Python ranges unpinned, no lockfile (standing) | `pyproject.toml` ranges; only `Cargo.lock` committed |
| R2-9 | LOW | animation pref defaults on under reduced motion | `prefs.py:35` vs no system seeding; desktop has `enable-animations=false` |
| R2-10 | LOW | Diagnostics rows overflow horizontally | screenshot `ui-3-tab8.png` |
| R2-11 | INFO/WATCH | live worker RSS +26 MB / 2 h under test load, then flat at ~337 MB across a controlled 12-min soak (11 scans) — burst warmup favoured, overnight soak still wanted | soak CSVs (run 1 + run 2) |
| R2-12 | INFO | `setup-auth.sh` overwrites config (unshipped); manifest "unpinned" comment stale; installed CLI predates tree; worker-stop mediapipe traceback; venv bloat; swap-without-mlock residual; downgrade silent-off behaviour | per-area notes |

## Prioritized fix order
1. R2-1 (gate the preview fd — camera surveillance by any local user).
2. R2-2 (size-cap the audit log + consider throttling pre-auth refusals).
3. R2-3 (gate `TestScan` or run it through `scan_slots`).
4. R2-4 + R2-5 (one-line prerm case; document/preserve PAM tuning).
5. R2-6 → R2-10 (all small, independent).
6. R2-11: controlled idle soak to close the RSS question.

## Verdicts by area
- Area 8: **PASS** (7/7 testable; 2 blocked; queue-timeout behaviour documented, falls through cleanly).
- Area 9: **CONDITIONAL FAIL** on 9.1 alone (log growth); everything else passes; 9.5 watches.
- Area 10: **FAIL** on 10.1 (HIGH) + 10.2 (MEDIUM) + 10.7 (LOW); socket/config/store/matcher fuzzing passes.
- Area 11: **PASS with two MED-LOW fixes** (11.2, 11.3); pipeline verified.
- Area 12: **PASS with two LOW fixes** (12.5, 12.6); rest passes or blocked-cleanly.
- Area 13: **PASS** (5/5 testable; replug blocked); matrix recorded above.
- Area 14: **PASS** (2 INFOs, no findings).
- Area 15: **PASS with two LOW hygiene items** (15.3, 15.6); antispoof gap closed.

Overall: the auth path itself (PAM fall-through, allowlist, serialization,
input validation, template crypto, model pinning) held up under every
probe. The round's yield is one HIGH (ungated preview), one MED-HIGH
(log growth), one MEDIUM (ungated TestScan) — all outside the
match-decision core — plus packaging/a11y/hygiene lows.
