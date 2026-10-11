# FaceID@NIM master audit report — 2026-10-11

**What this is:** an AI-assisted self-audit of the `phase2-remediation`
branch, performed in the working tree by an agent with the project's
full history. It is **not an independent security audit** — do not
describe it as one. Every claim below cites a file:line or a command
whose output was actually observed. Statuses used: `PASS` (observed),
`FAIL` (observed broken), `BLOCKED` (external prerequisite missing),
`UNTESTED` (not run). Findings use `OPEN` / `VERIFIED-FIXED` /
`BLOCKED` / `ACCEPTED-RISK`.

**Environment:** Ubuntu 24.04 (kernel), systemd 255, GNOME Shell 46.0 /
gjs 1.80.2 (Wayland session, user 1000), rustc 1.97.0, zbus 4.4.0
(locked), nix 0.29.0 (locked), Python 3.12.3, onnxruntime 1.30.0, node
v22.23.1. Cameras `/dev/video0-4` present. No sudo password available
to the auditor — anything requiring root is `BLOCKED`, not passed.
Package `faceid-nim 1.1.1-1 amd64` installed.

## 1. Baseline (Phase A)

| Command | Result |
|---|---|
| `git status --porcelain` (start) | clean |
| `git branch --show-current` | `phase2-remediation` |
| `cargo test --manifest-path daemon/Cargo.toml` | `41 passed; 0 failed` (later 45 after new tests) |
| `PYTHONPATH=vision:app pytest vision/tests app/tests eval/tests` | `174 passed, 1 skipped` (later 186) |
| `cargo clippy --all-targets -- -D warnings` | clean |
| `cargo fmt --check` | clean |
| `gcc -shared -fPIC -std=c11 -Wall -Wextra -Werror … pam/pam_faceid.c` | clean |
| `node --input-type=module --check` (all ext JS) | clean |
| `sh shell-ext/check-listen-only.sh` | `listen-only OK` |
| shellcheck | unavailable (`UNTESTED` for shell lint; `sh -n` clean) |

No pre-existing failures: the baseline was fully green, so every
red-to-green below is attributable to the change it guards.

## 2. Supplied-context triage (leads, not proof — verified here)

| Lead | Verdict |
|---|---|
| Installed 1.1.1-1 lacks Phase 2 fixes | **TRUE** — live polkit shows 5 actions, no `org.faceidnim.test` (`scripts/live-proofs.sh` output) |
| Rust never compiled in build env | **FALSE** — builds + tests green on rustc 1.97 |
| `require_polkit()` stub returning `Ok(())` | **FALSE** — real `CheckAuthorization`, `system-bus-name` subject, fail-closed denial (`daemon/src/authz.rs:36-76`) |
| `REPLACE_ME` antispoof checksum | **FALSE** — pinned `d7b3…`, byte-identical to the on-disk model (`sha256sum` observed) |
| `tau = 0.40` placeholder | **TRUE** — still unmeasured, still documented as such (`config.rs:114`, `postinst`, README) |
| Duplicate `faceid-nim-dynamic-island/` | **TRUE, now fixed** — stale fork carried pre-fix vulnerabilities; ported + sync-tested (`89480f8`) |
| 3 s recency guard | **TRUE, now tested** — extracted to `scanGuard.js`, 7 node tests, gjs-runtime verified |
| No-enabled-enrollments check | **TRUE** — `Store::any_enabled` + pre-scan refusal, tested |
| 41 Rust tests at report time | **TRUE** — 41 at baseline, 45 after this pass |

## 3. §6 known-defect disposition

| # | Item | Status |
|---|---|---|
| 1 | zbus/nix API drift compile failures | No longer applicable — compiles on locked zbus 4.4.0 / nix 0.29.0 |
| 2 | polkit declared but `require_polkit()` stub | Already fixed — real check verified (`authz.rs:36-76`) |
| 3 | Placeholder antispoof checksum | Already fixed — `d7b3…` verified against disk |
| 4 | Uncalibrated `tau = 0.40` | Still open, documented everywhere; MUST FIX before any security claim (needs a face + time, `B-16`) |
| 5 | `R_OK` collision | Already fixed — `FID_` prefix with rationale (`pam/protocol.h:13`) |
| 6 | Duplicate island tree | Fixed this pass — ported listen-only + guard, sync test added |
| 7 | `Enrollment1` registration | Already fixed — registered in `main.rs:142-143`; new source↔policy agreement test pins it |
| 8 | D-Bus policy missing enrollment interface | Already fixed — 6 enumerated members, no blanket (element-level test) |
| 9 | `deliver_preview` not called | Already fixed — called from `enroll_pose` pumps (`enroll.rs:377,389`) |
| 10 | Installed .deb lacks remediation | Still open — rebuilt 2026-10-11, artifact verified, **not installed** (needs sudo) |
| 11 | Camera activation with no enrollments | Fixed this pass — pre-scan refusal (`session.rs:238`, `dbus.rs:370`), tested red-to-green |
| 12 | Overlapping terminal signals | Fixed — `scanGuard.js` filter, 7 node tests + gjs runtime check |
| 13 | `glance`→`iPhone` migration | Fixed — Python + GJS migration, sync + migration tests green |
| 14 | Archive validation / frame limits | Fixed + hardened — traversal/size/count already capped; encrypted-entry crash + non-http schemes fixed this pass with 6 new tests |
| 15 | Doc/code mismatches | Fixed — README FAQ added, stale hover-retry/default corrected |

## 4. Defect ledger (this pass)

### F-1 — Animation `download()` accepted any URL scheme (Medium)
- Location: `app/faceid_app/openings.py`, `download()`.
- Expected: only `http(s)` install URLs. Actual: `urllib` also
  fetches `file://`, `ftp://`, `data://` from an arbitrary user-typed
  URL. Impact capped (2 MB, zip must still validate, declarative-only
  content, errors don't exfiltrate) — hardening, not a vuln fix.
- Fix: scheme allowlist before any fetch. Test:
  `test_download_rejects_non_http_schemes` — **FAILED pre-fix**,
  passes post-fix. Commit `8d5f2ae`. Status: `VERIFIED-FIXED`.

### F-2 — Encrypted zip entry crashed the installer (Low)
- Location: `install_package()`, `z.read("opening.json")` caught only
  `(KeyError, ValueError)`; an encrypted entry raises `RuntimeError`
  → uncaught traceback instead of "not installable".
- Fix: catch `RuntimeError` → `ValueError`. Test:
  `test_install_rejects_encrypted_entry_cleanly` — **FAILED pre-fix**,
  passes post-fix. Commit `8d5f2ae`. Status: `VERIFIED-FIXED`.
- Adjacent (already correct, now pinned): absolute/`..` paths,
  oversize, file-count, missing `opening.json` — 4 tests, all passed
  pre- and post-fix (characterization of existing guards).

### F-3 — `PreviewAnimation` callable by any local session (Low)
- Location: `daemon/src/dbus.rs`, `preview_animation()` — no polkit
  check, so any process (including remote/inactive sessions) could
  paint a fake "matched + Welcome \<name\>" on a lock screen.
  Display-only (no auth impact), but a fake success display aids
  social engineering and (with the recency guard) could briefly mask
  a real rejection display.
- Fix: gate on `org.faceidnim.test` (promptless locally, denied
  remote/inactive) + bus-policy comment corrected. No unit test
  possible without a bus (documented); live coverage assigned to
  checklist item 4. Commit `cea1c06`. Status: `FIXED-UNVERIFIED`
  (code-verified, live `BLOCKED`).

### F-4 — Stale island fork (High, fixed)
- `faceid-nim-dynamic-island/.../dbusClient.js` (+extension/pill)
  retained the pre-Fix-2a hover-to-retry TestScan path. Any install of
  that fork reintroduced the exact closed hole.
- Fix: ported listen-only + guard verbatim; `check-listen-only.sh`
  now scans both copies; `test_scan_guard_sync.py` pins the two
  `scanGuard.js` files byte-identical. Commit `89480f8`. Status:
  `VERIFIED-FIXED`. The tree is intentionally **not deleted** (§5H:
  sync tests + docs reference it; deletion needs owner approval).

### F-5 — PAM profile/install drift (Informational)
- Live `common-auth` carries `timeout=4500` while the shipped profile
  says `timeout=10000`: generated from an older profile, preserved by
  the no-regenerate-on-upgrade rule. Fail-safe direction (module gives
  up first → password; daemon scan orphaned briefly). Status:
  `ACCEPTED-RISK`, documented in `known-gaps.md`.

### Carried Phase 2 findings (closure evidence re-verified this pass)
1B-1 timeline scoping, 1B-2a/2b TestScan + extension, 1D-3 contact,
1E-2 eval discipline, 1B-3/1B-4 conf + docs — all tests re-run green
(§7). Installed package contains **none** of them (see §9).

## 5. Security/correctness audit (§5) — area results

- **A. PAM/recovery — PASS (code) / BLOCKED (live).** No AI in
  `pam_faceid.c` (socket + `kind`/`text` scan only; strict build
  clean). Return mapping: `ok→SUCCESS`, `denied→AUTH_ERR`,
  everything else→`UNAVAIL`. Live stack verified:
  `auth [success=3 default=ignore] pam_faceid.so` skips
  unix/sss/deny on success, `ignore` falls to `pam_unix` password on
  any failure — recovery preserved by construction. Installed profile
  matches source, `Default: no`. Daemon-down/worker-dead/timeout/
  camera-unplug/malformed/corrupt-config all map to `unavail`
  (unit-tested: `worker_killed_mid_scan…`, `idle_connection…`, new
  `malformed_requests…`, `no_enrollments…`). Lockout is per-uid,
  serialization-tested, in-memory (accepted, root-only reset).
  **Live password-fallthrough drill: BLOCKED (needs sudo).**
- **B. Daemon/storage — PASS.** AES-256-GCM + AAD-bound uid/name/
  model/version, 0600/0700 under 0711 root, atomic rename + tmp
  cleanup, non-finite embeddings refused, tau/vote/timeout clamped
  fail-closed, NaN worker scores can't pass (`NaN >= tau` is false),
  audit 50 MB writer cap + accounted drops agreeing with logrotate
  `size 10M × rotate 4`. Deletion verified by new test (load,
  load_enabled, any_enabled all agree; file gone; idempotent).
  Multi-identity-per-uid semantics documented (any enrolled identity
  opens that uid's account — by design, trust warning in README).
- **C. D-Bus/polkit — PASS (code) / BLOCKED (live prompt).** Real
  `CheckAuthorization`, `system-bus-name` subject from sender,
  `AllowUserInteraction`, fail-closed denial; every writer gated;
  `Enrollment1` registered + enumerated (source↔policy test);
  preview pipes fd-scoped with uid checks and cancel/finish/drop
  cleanup; `org.faceidnim.test` exposes no frames/templates/scores —
  coarse strings only, detail root-log-only. **Live prompt + inactive
  denial: BLOCKED.**
- **D. Worker/IPC — PASS.** Runs as `faceid:video`, templates
  root-only (unreadable — `/var/lib/faceid-nim` not even listable as
  user), auth.sock 0666 root-owned in 0755 dir (worker cannot touch
  it), vision.sock in 0770 `root:faceid`, 0660 on bind. Bounded
  8 KB lines, 4 s idle reap, 64-connection cap, scan budget
  timeout+3 s. No templates/scores/frames in logs or signals
  (audit-content test exists). Camera ACLs observed
  (`root:video` + `user:zang:rw-` seat ACL) — same-user direct camera
  access predates and exceeds the TestScan gate (accepted residual
  risk, unchanged).
- **E. Liveness — PASS (code) / UNTESTED (live attacks).** Veto
  conjunction pinned by mutation-tested regression
  (`perfect_match_with_liveness_veto_is_denied` FAILS when `&&
  liveness_ok` is removed). Missing/invalid models degrade loudly
  (new `test_antispoof.py`: `available=False` + reason, `denies()`
  False, `spoof_probability()` None). Checksums real + verified
  pre-bind; `--no-verify` gated behind `FACEID_DEV=1`. Eval scripts
  refuse small-n claims (Wilson CI + rule-of-three + min-n refusal,
  tested). `tau = 0.40` still evidence-free — labeled as such
  everywhere, never as calibrated. **No photo/screen/video attack has
  been run here: resistance is implemented, not measured.**
- **F. Extension/animation — PASS (static+unit) / BLOCKED (nested
  runtime).** Listen-only in both copies (CI grep + node tests);
  no private-prompt hooking; only `(state, progress, reason)` consumed
  with clamping; single active animation; stale-terminal guard
  extracted, 7 node tests green, **executed under real gjs 1.80.2**
  (`show=false` observed); `glance`→`iPhone` migration tested both
  sides (Python behavioral + JS source-shape via sync test).
  Sequence numbers evaluated and rejected with rationale in
  `scanGuard.js` (display-only consumer; wire format frozen).
  **Nested-shell pill run: BLOCKED** (Wayland socket contention,
  exact error recorded in known-gaps M-2). GDM-separate-config
  behavior documented accurately.
- **G. Assets/import — PASS.** Caps enforced pre-extraction (count,
  uncompressed bytes, traversal, basename flattening, symlink-safe
  regular-file writes, `z.close()` on all paths, URL size cap +
  timeout); HTML/CSS/JS never executed (declarative spec only; SVG is
  an image reference, not inline markup in shell context); frames
  ~37×216 px bounded by the checked-in set + count-aware extractor;
  failed imports leave existing config untouched (validation precedes
  all writes — tested). Fixed this pass: scheme allowlist,
  encrypted-entry handling.
- **H. Packaging/systemd — PASS (build) / BLOCKED (install).**
  `make deb DEB_FLAGS=-d` builds clean; artifact verified by
  extraction (arch, 6 polkit actions, 20 enumerated bus members, new
  binary strings); `debian/control` auto-restored, tree clean.
  Units, `PartOf` restart chain, `RuntimeDirectoryPreserve=yes`,
  `ExecStartPre` ownership, stale-socket unlinking all verified
  against the installed (old) units + source. **Installed files
  predate all fixes — reinstall BLOCKED on sudo.** Island tree kept
  deliberately (referenced, unshipped). No secrets/biometrics in the
  artifact (models + code only; `verify-models` re-checks postinst).
- **I. Quality gates — PASS.** 45 Rust + 186 Python + 7 node green;
  clippy `-D warnings`, `cargo fmt --check`, strict GCC, all JS
  `node --check`, `sh -n` on scripts; shellcheck unavailable
  (UNTESTED). No new dependencies added by this pass.

## 6. Tests added this pass (§7 mapped)

| Requirement | Test | Red→green evidence |
|---|---|---|
| No identities → no camera | `no_enrollments_refuses_without_scanning` | FAILED with gate neutralized |
| Deletion prevents use | `deleted_identity_stays_deleted` | passes (pins present behavior) |
| Match + liveness-fail → deny | `perfect_match_with_liveness_veto_is_denied` | FAILED with veto conjunction removed (after fixing vote params so the test isolates the veto) |
| Missing/invalid model → no false approval | `test_antispoof.py` (3) | pins present-loud behavior |
| Malformed IPC → unavail, never ok | `malformed_requests_are_unavailable_never_ok` | FAILED with `ok` reply mutated in |
| Out-of-order animation events | `test-scan-guard.mjs` (7) + gjs run | green; delayed/dup/reordered/legit-alternation covered |
| Migration (Py + GJS) | `test_opening_migration.py` (3) + sync test | green (rename itself was the red state) |
| Archive traversal/invalid | 5 new `test_openings.py` tests | 2 FAILED pre-fix (scheme, encrypted), all pass post-fix |
| Enrollment reg ↔ policy | `enrollment_methods_match_bus_policy` | FAILED on unclassified-method insertion |
| Audit bounded under load | pre-existing `capped_append_bounds_the_file` | green |
| Upgrade/removal PAM preservation | **BLOCKED** (needs root + real apt) — code-reviewed only |
| Password fallback, live | **BLOCKED** — daemon-level equivalents green |
| Worker socket perms post-restart | **BLOCKED** (needs sudo to stat + restart) |

Negative tests: every security fix above has one (denied/refused/not-found direction asserted, plus mutation checks where thekte behavior pre-existed).

## 7. Live proofs (§8)

`scripts/live-proofs.sh` inspected: read-only probes assert nothing
spuriously (all informational `|| true` collectors); the soak branch
requires an interactive tty password and fails loudly otherwise.
Executed read-only half: units active, `RuntimeDirectoryPreserve=yes`,
bus name owned, models verified, **installed polkit lacks
`org.faceidnim.test`** (drift proof). Proofs 1–4 (prompt, restart,
PAM drill, nested pill): **BLOCKED** — no sudo / no disposable
graphical session. Proof 5 (purge): **BLOCKED** — needs a throwaway
VM, must not run here. Proof 6 (soak): **BLOCKED** — needs hours +
sudo. Nothing promoted from unit to end-to-end.

## 8. Remaining risks & decisions

- Installed-vs-source drift is the sharpest risk: same version number
  (1.1.1-1), different contents. Reinstall is step 1 of anything.
- Spoof resistance implemented-but-unmeasured; `tau` placeholder.
- Display-only residual: gated `PreviewAnimation` + guard; accepted.
- In-memory lockout, PAM timeout drift, legacy `Enroll`, GDM default
  animation: accepted/documented, not downgraded silently.
- No new deps, no arch changes, no threshold changes, no PAM flag changes.

## 9. Recovery instructions (unchanged, re-verified)

Password always works (`sufficient`-equivalent `success=3` +
`ignore` fallthrough to `pam_unix`). If locked out: `Ctrl+Alt+F3`,
log in, `sudo apt remove faceid-nim`. Keep a root shell open during
any PAM work. Purge only in a disposable VM.

## 10. Release recommendation: **NO-GO for v0.1.0-alpha today**

Not for lack of code quality — the tree is green, guarded, and honest
— but an alpha must be installable, recoverable, and minimally
proven, and three items are one session away: **(1)** install the
verified rebuilt `.deb` and re-run `scripts/live-proofs.sh`;
**(2)** live password-fallthrough drill (proof 3) — the recovery
guarantee must be observed, not inferred; **(3)** either measure one
tau/spectrum pair or ship the placeholder explicitly labeled (it
already is — confirm that labeling counts as the alpha decision, or
measure). Spoof measurement can follow in-alpha (honest tooling
exists), but must not be claimed before it runs. Next commands:

```sh
sudo apt install ./dist/faceid-nim_1.1.1-1_amd64.deb
sudo scripts/live-proofs.sh 2>&1 | tee live-proof-results.log
# then checklist items 1-4 in docs/live-proof-checklist.md
```
