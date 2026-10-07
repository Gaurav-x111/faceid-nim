# Known gaps — consolidated BLOCKED / UNTESTED (Rounds 1 + 2)

Running list. An entry leaves only with a measured result, never with
an assumption. "Round 1" items are as recorded in the Round 2 brief
(the Round 1 raw report is not in the repo).

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
| X-2 | installed-vs-tree drift for the daemon/worker | `strings` + live `GetSettings` confirm 1.1.1-1 ≈ current source for those binaries (CLI script still stale — see R2-12) |
| X-3 | user's personal non-recognition cause | narrowed to a liveness (antispoof) veto by two live `gdm-password` denials with `valid_frames=1`; user to confirm with `sudo faceid-nim audit 20` |

## Still open (design work, not test-blocked)

- First-run automatic τ calibration (`auto_tune.py` exists but is not
  wired into the enrollment wizard; fresh installs ship τ = 0.40).
- Stale comment in the manifest ("While it stays unpinned…", line 53).
- `setup-auth.sh` unconditionally overwrites the config (unshipped;
  document or retire).
