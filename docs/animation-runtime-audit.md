# Animation runtime audit (master remediation, 2026-10-12)

Source tree on branch `phase2-remediation`, inspected read-only before any
edit. All paths verified in code, not docs.

## 0. Source / artifact / runtime identity

| Layer | Evidence (2026-10-12) |
|---|---|
| Source | `phase2-remediation` @ `378d3bf`, clean (`git status --short` empty). `app/faceid_app/main.py` mtime 11:32, `pill.js`/`opening.js` 10:51, `.deb` built 13:13 same day |
| Artifact | `dist/faceid-nim_1.1.1-1_amd64.deb` (160 MB). Installed files for `main.py`, `openings.py`, `dbus_client.py`, `prefs.py`, `extension.js`, `pill.js`, `opening.js`, `scanGuard.js` are byte-identical to source (`cmp` SAME) |
| Installed app entry | System desktop `org.faceidnim.App.desktop` → `Exec=/usr/bin/faceid-app` → `PYTHONPATH=/usr/share/faceid-nim/app`. **Shadowed** by `~/.local/share/applications/org.faceidnim.App.desktop` → `Exec=/home/zang/.local/bin/faceid-app` → `PYTHONPATH=/home/zang/name/faceid@nim/app` (dev tree). Journal proves the running app is the dev tree: `File "/home/zang/name/faceid@nim/app/faceid_app/main.py", line 1121` |
| Installed shell entry | System `/usr/share/gnome-shell/extensions/faceid@nim/` v4 (pill `v4 loaded (glance-default)`, `filterTerminal`, `reset()` scrub). **Shadowed** by `~/.local/share/gnome-shell/extensions/faceid@nim/` (Sep 26 / Sep 28 / Oct 3). Journal: `Extension faceid@nim already installed in /home/zang/.local/... /usr/share/... will not be loaded` |
| Running shell | User copy wins. It predates: `filterTerminal` import, `_pill.reset()` in `startScanning`, `videoWidget` child, `Use your password` label, `set_child_above_sibling` re-top. So the lock screen runs the old state machine while the packaged code is never loaded |
| Running app process | Old process held pre-fix code: journal 10:57 shows `self.on_enroll()` TypeError from `_on_overview_next`, a function that no longer exists in source (`grep _on_overview_next` → only a test asserting its absence). Current `on_enroll(self, _b)` at `main.py:2803` takes the arg; the crash is stale-process, not current source |
| Package version | `1.1.1-1` both in `dpkg-query` and the rebuilt `.deb`. No version/content skew today, but `packaging/scripts/faceid-nim` is a stale minimal `status` that diverges from the installed `bin/faceid-nim` (rich `status` with D-Bus name, liveness, cameras). `debian/rules:183` installs `bin/faceid-nim`, so the scripts copy is dead but a future-edit trap |

Consequence: reinstalling the `.deb` cannot change what the user sees
until (a) the `~/.local` extension shadow is removed or updated, (b) the
`~/.local` desktop shadow is removed or updated, and (c) the old app
process is restarted (log out/in). No code change in the repo can bypass
this; `status` must therefore detect and report the shadow (see §6).

## 1. Execution-path table

| # | Path | Config read | Key/field | When read/cached | Canonical ID | Renderer | Asset | Terminal handling | Fallback | Live? |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | Session startup / login / GDM | `~/.config/faceid-nim/openings.json` via `opening.js:126-149 _readConfig`, re-read every `OpeningScene.load()` at `startScanning` (`opening.js:320-335`) | `active`, `scan_face` | per-scan, not cached across scans | `active` in `BUILTIN={iphone,logo,glow,none}` else `iphone`; `glance→iphone` migrate | Clutter `OpeningScene` + PNG frames (`glance-frames/frame-%03d.png`, `_ensureFrames`) or Cairo/St logo | `iphone`: frames 1..240 (`fps 30`), else procedural spring; `logo/glow`: `logo.svg`/halo | `handoff()` at `duration` or on `verifying/matched` (`pill.js:744-745,358,367`); `_onLocked` → `reset()` + `_lastTerminal=null` | unknown `active`→`iphone` silently; missing frames→spring pop; missing/bad JSON→`{active:iphone,scan_face:apple}` | live |
| 2 | Screen lock/unlock | same as 1 | same | same | same | same (`FaceIdPill` in `Main.uiGroup` top chrome, above shield curtain) | same | `filterTerminal` 3 s guard (`extension.js:78-80`, `scanGuard.js:31-39`); `matched/rejected/timeout/camera_error` → card, `reset()` on next `waking/searching` | same | live |
| 3 | PAM `sudo` | daemon `config.toml allowed_services` (`config.rs:183-185 service_allowed`, enforced `session.rs:159-171` before camera) + same pill path | `service` string from PAM module | per-auth | n/a (pill shows `Verified`, no identity, `_loginContext=false`) | same pill; HUD has no idea which service asked (`pill.js:43-49`) | same | same | service not in `allowed_services` → refused before camera | live (if allowed) |
| 4 | PAM `login`/GDM | same as 3; extension `session-modes` includes `gdm`, `unlock-dialog` (`metadata.json`) | same | per-auth | same, plus `Welcome <uid>` card when `_loginContext=true` | same | same | same | same | live |
| 5 | Daemon→shell `ScanState` | n/a (signal `ScanState(s,d,s)` only, `dbusClient.js:18-27`) | `state=waking/searching/verifying/matched/rejected/timeout/camera_error` | live signal; `filterTerminal` drops stale opposite terminal <3 s | n/a | `extension.js:_onState` → `pill.startScanning/setProgress/showMatched/showRejected/reset` | n/a | `filterTerminal`; `default→reset()` | unknown state→`reset()` | live |
| 6 | Selected variant + `scan_face` | app writes `openings.json` (`openings.py:211-221 save`, `set_active`, `set_scan_face`); shell only reads | `active`, `scan_face ∈ {apple,arena,classic}` | app: in-memory `self._openings` + deferred `_refresh_openings`; shell: per-scan re-read | `active` ∈ `BUILTIN + custom`; `scan_face` whitelist else `apple` (both sides) | n/a | n/a | migration `glance→iphone` written back (`openings.py:199-203`); shell migrates in memory (`opening.js:324-325`) | `active` unknown→`iphone`; bad `scan_face`→`apple` | live |
| 7 | App preview (local) | local `openings.json` + clip existence only (`preview_plan_for_state`, `main.py:136-191`) | `active` + preview `state ∈ {scanning,recognized,denied}` | on button click, no cache | same IDs as path 6 | `Gtk.Video` for `iphone` mp4, else Cairo `DrawingArea` (`_preview_plan_widget`, `main.py:1564-1598`) | `GLANCE_CLIPS` (`openings.py:68-73`) via `glance_asset` basename probe (`openings.py:76-83`); non-iphone always Cairo | dialog `response→_done` removes tick + closes; video has no signal cleanup (fixed §4) | missing clip→Cairo + explicit `(Unlock video missing …)` note; `active=none`→`kind:none` toast | preview-only, daemon-independent, never PAM/camera/unlock |
| 8 | App "Preview on lock pill" | daemon `PreviewAnimation(s)` (`dbus_client.py:86-92`, `daemon/dbus.rs:560-600`) | identity name | on click, in thread | n/a | real lock pill via `ScanState` signals | n/a | daemon emits full success sequence, no camera/auth | daemon down→friendly `SERVICE_DOWN_FRIENDLY` + technical expander | daemon-dependent by design, clearly labeled `(needs daemon)` |
| 9 | `linux-anim/test_anim.py` harness (`_preview_video_pill`) | hard-coded `glance_anim/assets/{unlockanimation.mp4,…}` | n/a | `Popen(start_new_session=True)` detached | n/a | external player | same mp4s | none (detached process can overlap dialog previews) | missing asset→toast | preview-only |
| 10 | Import/replace/delete/reset/undo/migration | `openings.json` + `openings/<id>/` dirs; `.faceopen` zips (`opening.json` + `logo.svg/png`) | variant spec (`SPEC_KEYS`, `openings.py:121`) | immediate `save()` | `_valid_id` sanitised; `glance→iphone` | n/a | `logo.svg/png` only; extras ignored, never executed | delete active→fallback `iphone` (`remove_variant`) | malformed config→defaults; missing asset→deterministic Cairo/glyph | live on next scan; app UI deferred via `idle_add` to avoid `notify::selected` re-entrancy hang |
| 11 | Packaged defaults vs user overrides | packaged: `BUILTIN` + `default_config()={active:iphone,scan_face:apple}`; user: `openings.json` + `app.json:animations_enabled` | `animations_enabled` read live every scan (`opening.js:56-72`), missing/bad→`true` | per-scan | n/a | toggle skips splash (`pill._animOff`, `reduced`) | n/a | n/a | n/a | live |

## 2. Configuration-source table

| Source | Writer | Reader | Fields | Validation | Reload | Corrupt/missing behaviour |
|---|---|---|---|---|---|---|
| `~/.config/faceid-nim/openings.json` | app only (`openings.save`) | app (`openings.load`) + shell (`opening.js:_readConfig`, `load()`) | `active`, `variants`, `scan_face` | `active` must be in `all_ids` else `iphone`; `scan_face` whitelist else `apple`; custom specs via `_clean_spec` | app: in-memory + `idle_add` refresh; shell: every `startScanning` | bad JSON/non-dict→defaults; migration written back so file cannot shadow memory forever |
| `~/.config/faceid-nim/openings/<id>/logo.*` | app (`create/update/install_package`) | shell (`OpeningScene`, builtin logo via `import.meta.url`) | one logo | basename only, no traversal (`_valid_id`, `install_package` rejects `..`/absolute, 2 MB/20-file caps) | per-scan | missing→procedural fallback, never fatal |
| `~/.config/faceid-nim/app.json` | app prefs | shell (`animationsEnabled`) | `animations_enabled` | `!== false` means on; missing/bad→`true` | per-scan | on (today's behaviour) |
| `/etc/faceid-nim/config.toml` | postinst (fresh only, `enabled=false`, auto-detected cameras) + daemon `SetSettings` (polkit-gated) | daemon (`Config::load`, fail-soft to defaults with log) | `enabled, strictness, tau, vote_k/n, mode, camera, ir_camera, allowed_services, …` (paths pinned, never settable over D-Bus) | `validate()` clamps `tau 0-1→0.9 fail-closed`, votes, timeouts | daemon in-memory + `save()`; app pushes resolved plan when `worker_reachable` and disagreeing | parse error→all defaults (loud log); postinst never overwrites an existing file |
| GSettings `org.faceidnim.settings` | app `prefs.py` | app | mirrors `Config` field names (schema comment); `mode` includes `auto` (fixed earlier; cross-checked by `test_mode_sets_agree.py`) | schema enums | live | n/a |

Authoritative contract (see §5): one canonical `active` ID
(`openings.json:active`), one `scan_face`, explicit migration
`glance→iphone`, unknown→`iphone` + bad `scan_face`→`apple` fallbacks that
are written back (app) or logged (shell), changes apply on the next scan
(no shell reload), never a silent switch of the displayed selector.

## 3. Renderer / asset compatibility table

| Variant | App preview renderer | Lock renderer | Asset used | Compatible? |
|---|---|---|---|---|
| `iphone` scanning | `Gtk.Video` `unlockanimation.mp4` (loop, 280×200, aspect preserved) | Clutter `Clutter.Image` PNG sequence `glance-frames/frame-001…240` (30 fps, `scale 216:216` extract of the **same** `unlockanimation.mp4`) | `linux-anim/glance_anim/assets/unlockanimation.mp4` → extracted frames | yes, same source; app plays video, lock plays frames (lock cannot play mp4) |
| `iphone` recognized | `Gtk.Video` same `unlockanimation.mp4` (`GLANCE_CLIPS.success` = same file) | green check + `Verified/Welcome` card (no clip) | same mp4 in app; no clip on lock | partial: app loops the unlock video, lock shows the card. Labelled as such after §4 fix |
| `iphone` denied | `Gtk.Video` `unsuccessfulunlockanimation.mp4` | red glyph + shake + `Use your password` card (failure mp4 is **never** extracted — `extract-glance-frames` only handles `unlockanimation.mp4`) | app: failure mp4; lock: procedural | partial: different but honest renderings of "denial"; never a success check (§4) |
| `logo` / `glow` / custom | Cairo `DrawingArea` splash with lock-screen timing (`fade_ms+700`, `iphone` 1600 ms period) | St/Clutter logo fade / halo (`fade_ms`, easing `outCubic/outBack/outExpo`) | `logo.svg/png` or none | yes, timing-matched; no clip exists and none is claimed |
| `none` | nothing (`kind:none` toast) | `isNone()` skip | n/a | yes |
| Codecs | mp4 via `Gtk.Video` (system codecs) | PNG frames via `GdkPixbuf→Clutter.Image` | mp4 never shipped to shell; frames shipped next to `opening.js` | boundary preserved: no WebKit, no parallel engine |

## 4. Root causes (file:line)

1. **Stale user-local extension shadows the package** — `~/.local/share/gnome-shell/extensions/faceid@nim/` (Sep 26–Oct 3) vs system v4. GNOME prefers the user copy: journal `Extension faceid@nim already installed in /home/zang/.local/… /usr/share/… will not be loaded`. The running copy lacks `filterTerminal` import (`extension.js:22`), `reset()` at `startScanning` (`pill.js:303`), `videoWidget` child (`pill.js:182-184`), re-top on lock (`extension.js:66`), and shows the old `Hover to try again` label. This is why selection and behaviour disagree and why reinstalls "do nothing".
2. **Stale user-local desktop shadows the package** — `~/.local/share/applications/org.faceidnim.App.desktop` → `~/.local/bin/faceid-app` (`PYTHONPATH=/home/zang/name/faceid@nim/app`) vs system `/usr/bin/faceid-app` (`/usr/share/faceid-nim/app`). The running app is the dev tree, and the old process held pre-fix code (`_on_overview_next` → `on_enroll()` TypeError, since removed). Reinstalls cannot affect it until the process restarts.
3. **Worker readiness race, not a crash** — `faceid-vision.service` is `active (running)` with `MainPID` but `vision.sock` MISSING at status time because model/mediapipe init takes ~60 s (`journalctl -u faceid-vision`: `listening on …/vision.sock` at 13:16:36, after the user's status run). `systemctl is-active` is not readiness. `status` must distinguish starting vs down (fixed in `bin/faceid-nim`).
4. **Preview dialog leaks players** — `_play_preview_plan` (`main.py:1528-1562`) creates a new `Adw.AlertDialog` per click with a per-dialog `tick_id`, never closes the previous dialog, never stops `Gtk.Video`, and has no single-controller guard. Rapid Play/Replay stacks dialogs/players; closing one leaves the others ticking. The Cairo denied preview also renders the same splash as scanning (state is dropped at `_preview_plan_widget`), so "denied" does not look like denial.
5. **Daemon-gated vs local previews share one error surface** — local clip/Cairo previews are already daemon-free (`preview_plan_for_state` touches only files; `test_ui_animation_wiring.py:100-129` asserts no `preview_animation`/`list_identities` in `_preview_selected_state`). The persistent `ServiceUnknown` comes from the explicitly daemon-gated `PreviewAnimation` path (`_preview_success`, `main.py:1841-1872`) or from a stale process holding the old callback — not from local previews. The UI labels the pill path `(needs daemon)` but the distinction was not enforced by a single controller or by stopping local playback first.
6. **Installer/status wording contradiction** — `postinst:256` unconditionally prints `Face unlock is DISABLED.` even on reinstall/upgrade where `/etc/faceid-nim/config.toml` already has `enabled=true` (preserved: `postinst:36 if [ ! -f … ]`). It is a fresh-install truth printed as a universal truth. `status` (rich `bin/faceid-nim`) correctly reports the live `enabled` flag, so the two disagree by design, not by state corruption.
7. **Dead `packaging/scripts/faceid-nim`** diverges from the installed `bin/faceid-nim` (minimal vs rich `status`). `debian/rules:183` installs the latter, so the former is an edit trap for the next session.
8. **Overlapping scans without a guard look like "all animations at once"** — fixed in source (`scanGuard.js`, `filterTerminal` 3 s; `pill.reset()` scrub; `setProgress` held during opening `pill.js:345-351,750-751`; single `GLib.timeout_add(16)` clock guarded at `pill.js:712`) but not live because of root cause 1.

## 5. Canonical selection contract (authoritative)

- One `active` ID in `openings.json` (`openings.set_active` validates
  against `all_ids`, `openings.py:239-244`). One `scan_face`
  (`set_scan_face`, `openings.py:247-253`).
- Migration: `glance→iphone` (app `load()` `openings.py:190-192` +
  write-back `199-203`; shell `opening.js:324-325`). Unknown
  `active`→`iphone`, bad `scan_face`→`apple` on both sides.
- Variant ID (`iphone/logo/glow/none/custom`) is distinct from
  `scan_face` (`apple/arena/classic`): the former picks the splash, the
  latter picks the pill glyph (`faceGlyph.js:153-171`).
- Missing/unsupported asset → deterministic fallback (video→Cairo hint,
  frames→spring pop, `none`→skip), with an explicit message, never a
  silent switch of the selector.
- Applies on the next scan (shell re-reads per scan; app defers model
  rebuild via `idle_add`). No shell reload needed. `animations_enabled`
  applies on the next scan too.
- The selector always shows the canonical ID; the renderer compatibility
  table above is shown in the UI (§4) where video vs frames differ.

## 6. Status truthfulness gaps closed by this remediation

- `active` ≠ ready: `status` now also reports the extension-shadow state
  (user copy present/stale vs system) and the app-entry state (which
  `faceid-app` the desktop file resolves to), because those decide what
  the user actually sees.
- `active` ≠ reachable: `status` keeps the `D-Bus name owned/MISSING`
  check (`bin/faceid-nim:56-69`) — an active daemon off the bus still
  serves PAM while every bus preview fails `ServiceUnknown`.
- Socket `MISSING` while the unit is `active (running)` is reported as
  `starting (service active, socket not bound yet)` when the unit is
  active, not as `worker not running`.
- Camera `present` ≠ selected: `status` already prints
  `camera`/`ir_camera` from config; the audit adds the RGB/IR
  classification (`devices` subcommand) and the animation-compat table
  so `present` is never read as `correct`.
- Models `verified` ≠ inference running: the `Liveness (what is
  actually running)` block queries the live worker capabilities, not the
  manifest.
