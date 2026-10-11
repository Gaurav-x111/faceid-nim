# Known gaps — consolidated BLOCKED / UNTESTED (Rounds 1 + 2 + master audit)

Running list. An entry leaves only with a measured result, never with
an assumption. "Round 1" items are as recorded in the Round 2 brief
(the Round 1 raw report is not in the repo). "Master" items are from
the 2026-10-11 master audit (branch `phase2-remediation`); the full
ledger is `docs/testing/master-audit-report.md`.

## Blocked — needs root (no root in audit sessions)

| ID | Item | Rounds | Reason | Closes when |
|---|---|---|---|---|
| B-1 | live fd counts of daemon/worker (`/proc/<pid>/fd` denied) | 1, 2 | root-owned processes | read fd counts during idle + scan as root |
| B-2 | real `dpkg -i` upgrade 1.1.0→1.1.1 / downgrade | 1, 2 | needs root | install both directions, check units + PAM + config after each |
| B-3 | `pam-auth-update --package/--enable/--remove` live run | 1 | needs root | run each, diff `common-auth` before/after |
| B-4 | `systemctl restart faceid-nimd` mid-scan | 1, 2 | needs root | restart during a live scan; client must EOF, unit must be back in ≤ ~5 s |
| B-5 | USB unbind/replug of the internal webcam | 1, 2 | needs root (`/sys/bus/usb/.../unbind`) | replug during idle + during scan; expect clean unavail + recovery |
| B-6 | create a second user (real two-uid auth) | 1, 2 | needs root (`useradd`) | two uids authenticate simultaneously; expect FIFO completion, no cross-uid success |
| B-7 | audit-log write path as root | 1 | dir is root-only | confirm rotation + `create 0600` after `logrotate -f` |
| B-8 | install `python3-pyatspi` for a live AT-SPI tree walk | 2 | needs root (apt) | walk the app tree: names, roles, focus order |

## Blocked — needs a password / would interrupt the user

| ID | Item | Rounds | Reason | Closes when |
|---|---|---|---|---|
| B-9 | live enrollment (real face → template) | 1, 2 | `auth_self` polkit → password dialog | enroll with password; verify template file appears 0600 + audit line |
| B-10 | live `SetSettings` camera change | 1 | same gate | change camera via app; verify daemon picks it up without restart |
| B-11 | lock screen + `sudo` at the same instant | 2 | must lock the session; password unknown | attempt both; expect FIFO + budget-timeout deny on the loser |
| B-12 | live text-scaling 1.5× + high-contrast pass | 2 | mutates the live session | screenshots of Overview/Cameras/Diagnostics at 1.5× |

## Blocked — needs hardware / physical presence

| ID | Item | Rounds | Reason | Closes when |
|---|---|---|---|---|
| B-13 | prove the IR path (emitter on/off, dark-room unlock) | 1, 2 | `linux-enable-ir-emitter` + a dark room | IR unlock in the dark, RGB when lit, spectrum in audit |
| B-14 | second physical camera / USB camera matrix row | 2 | no spare camera | enumerate + scan on a UVC USB camera |
| B-15 | Orca screen-reader audio output | 1, 2 | needs ears + a reader session | full enroll + unlock driven by audio alone |
| B-16 | measure τ per spectrum (`eval/far_frr.py`) | 1, 2 | needs a face + time | genuine/impostor separation; replace the 0.40 placeholder |

## Watch — needs time, not access

| ID | Item | Rounds | Reason | Closes when |
|---|---|---|---|---|
| W-1 | live worker RSS +26 MB / 2 h (arena vs leak) | 2 | burst warmup favoured: flat at ~337 MB across a controlled 12-min soak with 11 scans; needs a quiet overnight soak to be sure | flat idle RSS ± noise over ≥ 8 h, or a growing trend |
| W-2 | audit-log growth under multi-client spam | 2 | single-client measured; N-client extrapolated | N×64-connection spam rate + logrotate `size` cap behaviour |

## Resolved since Round 1

| ID | Item | How |
|---|---|---|
| X-1 | MiniFASNet licence/pinning | user pinned `d7b3…`, fetched, restarted worker 2026-10-07 16:43 (journal-proven); cue live |
| X-2 | installed-vs-tree drift for the daemon/worker | `strings` + live `GetSettings` confirm 1.1.1-1 ≈ current source for those binaries (CLI script still stale — see R2-12). **STALE AGAIN after Phase 2: the installed 1.1.1-1 predates every Phase 2 + master-audit fix (no `org.faceidnim.test` action live, old TestScan strings, old extension). Reinstall the rebuilt .deb, then re-verify — see M-1.** |
| X-3 | user's personal non-recognition cause | narrowed to a liveness (antispoof) veto by two live `gdm-password` denials with `valid_frames=1`; user to confirm with `sudo faceid-nim audit 20` |

## Master audit (2026-10-11) — new blocked / noted items

| ID | Item | Reason | Closes when |
|---|---|---|---|
| M-1 | installed 1.1.1-1 predates all Phase 2 + master fixes | reinstall needs sudo; auditor has none | `sudo apt install ./dist/faceid-nim_1.1.1-1_amd64.deb` (rebuilt 2026-10-11, artifact verified to contain the fixes), then re-run `scripts/live-proofs.sh` + live proofs 1–4 |
| M-2 | nested-shell pill runtime test | nested `gnome-shell --nested --wayland` fails to take a display in the live session (`WL: unable to lock lockfile /run/user/1000/wayland-0.lock`); needs a disposable graphical session or VT | run item 4 of `docs/live-proof-checklist.md` there; any Cairo traceback pasted verbatim |
| M-3 | purge test | must not run on the daily driver; needs a throwaway VM | item 5 of the checklist in a VM |
| M-4 | overnight soak | needs hours + sudo for restart loops | 200-attempt soak per checklist item 6, or ≥8 h idle RSS |

## Master audit — noted limitations (no test blocked, documented here)

- Lockout counters are in-memory: a daemon restart clears them. Only
  root can restart the daemon, and root is outside the threat model,
  so this is accepted — but a crash loop would also clear them.
- `common-auth` carries `timeout=4500` while the shipped profile says
  `timeout=10000`: the line was generated from an older profile and
  `postinst` deliberately skips regeneration on upgrade (preserves
  admin tuning). Effect is fail-safe (module gives up first, password
  follows); the daemon-side scan is then orphaned for a few seconds.
- Legacy `Daemon1.Enroll` (blocking multi-pose) is still exposed but
  unused by the app (onboarding uses `Enrollment1`); a 12-pose call can
  outlast the app's 60 s D-Bus timeout while the server keeps working.
  Kept for compatibility, not removed.
- `PreviewAnimation` is now polkit-gated (`org.faceidnim.test`) so a
  remote/inactive session cannot paint a fake success on a lock screen;
  local dev use is unchanged (promptless for the active session).

## Still open (design work, not test-blocked)

- First-run automatic τ calibration (`auto_tune.py` exists but is not
  wired into the enrollment wizard; fresh installs ship τ = 0.40).
- Stale comment in the manifest ("While it stays unpinned…", line 53).
- `setup-auth.sh` unconditionally overwrites the config (unshipped;
  document or retire).
