# faceid@nim — Session Context (save 2026-09-21)

## Objective
- Move the deployed system onto **hybrid mode** (RGB `/dev/video1` recognition + IR `/dev/video3` spoof liveness). DONE.
- Fixed moire false-positive (new prominence metric + soft temporal window). DONE but threshold 20.0 still needs genuine-face validation.
- Audit improvements (#19). DONE.
- Added shell-extension **opening animation section** (app-logo splash) + **"Welcome <uid>"** after successful scan. DONE (repo + user-level extension, deployed live).
- **Opening animations feature**: settings-app **Opening** page — pick builtin/user animations, create your own (logo+colours+easing), install `.faceopen` packages from file/URL/GitHub catalogue, publish yours to GitHub. DONE in repo + deployed user-level; system-wide GDM sync pending user sudo.

## Status of the running system
- Daemon + worker hybrid binary deployed (`/usr/libexec/faceid-nim/faceid-nimd` sha256 `02df37df...` == freshly built).
- **Hybrid applied** via DBus `SetSettings` (daemon saves atomically as root, no sudo needed):
  - `GetSettings` → `mode:"hybrid"`, `camera:"/dev/video1"`, `ir_camera:"/dev/video3"`.
  - `Diagnostics` → `mode:"hybrid"`, `worker_reachable:true`.
  - Live `TestScan` through the running daemon: **captured 1 embeddings** — hybrid path OK, no moire deny.
  - `/etc/faceid-nim/config.toml` now has canonical keys `camera`/`ir_camera` (`rgb_device`/`ir_device` are parse aliases).
  - No service restart required (mode read live per scan, worker is stateless).
- Config is user-settable over the bus: root cause was the file was never edited (root-owned); `set_settings` is NOT polkit-gated (unlike enroll/test_scan).

## Moire fix (core)
- `vision/faceid_vision/liveness/screen.py` `_moire` = peak/median prominence in annulus r∈(24,56) of 128×128 Hanning FFT, `INTER_AREA` resize, flat-crop guard → 0. `MOIRE_THRESHOLD = 20.0`.
- `vision/faceid_vision/scan.py`: soft `_MoireWindow` (`moire_frames=5`, `moire_required=3`); moire denied only when hits ≥ required in last N usable frames; `glare`/`device_frame` remain hard denies; IR mode skips `screen_report()`. Notes carry `moire`, `moire_threshold`, `moire_window{frames,hits,required}`.
- Live baseline (no face, office, /dev/video1): 352 frames min 4.326 / med 5.987 / max 7.358 < 20. Synthetic grids ≥ 19k, jpeg-lattice ~38, random scene ~13.17 (passes). Old metric 0.154–0.210 vs 0.055 → always denied.
- **Provisional:** no genuine-face crop measured yet — user must lock later and watch the pill not show moire-deny.

## Hybrid wiring
- `daemon/src/config.rs`: `CameraMode::Hybrid`; camera/ir_camera accept rgb_device/ir_device aliases (legacy keys OK, save() emits canonical). daemon `cargo test --release` → **17 passed**.
- `vision/faceid_vision/scan.py` + `__main__.py`: IR gate `mode in ("ir","both","hybrid")`; `--mode` choices include hybrid.
- `packaging/faceid-nim-config.toml` ships hybrid now.
- Live worker evidence (private worker with PYTHONPATH=repo, deployed venv): both /dev/video1 and /dev/video3 held during hybrid scan; done 1 RGB embedding; `deny:["ir_screen_dark"]` on dark empty IR; moire 11.09 < 20; stats `{frames:1,usable:1,elapsed_ms:177}`.

## Audit plumbing (#19)
- `daemon/src/worker.rs`: `Liveness.notes` (serde_json::Map), `DoneStats{frames,usable,elapsed_ms}`, `Event::Done.stats`, `ScanEvidence.usable`.
- `daemon/src/audit.rs`: `Event` optional `moire_score`, `moire_threshold`, `valid_frames` (`skip_serializing_if`).
- `daemon/src/session.rs`: log closure extended with notes + valid args, `note_f32` helper.

## Opening animations feature (#new)
- Declarative-only opening animation engine. `shell-ext/faceid@nim/opening.js` (new) + reworked `pill.js`. The engine reads `~/.config/faceid-nim/openings.json` each scan and renders builtin variants `logo` / `glow` / `none` plus user-made specs. **Security invariant: everything is data (colors/easing/one logo), never code — a package cannot execute in gnome-shell.**
- Custom spec keys (app editor ↔ engine): `kind, name, logo, text, bg, accent, ease(outCubic|outBack|outExpo), fade_ms(120–3000), scale, ring`.
- Opening handoff to glyph waits on `grow>=1 && sinceStart>=duration()`; `setProgress`→verifying force-handoffs via `_opening.handoff(pill)`; `reset()` calls `_opening.reset(pill)`.
- App: new **Opening** sidebar page (`main.py._opening_page`): Active-animation ComboRow (+pill preview swatch via cairo draw func), Create editor dialog, Install-from-file / Install-from-URL / Browse-community (fetches repo `openings/catalog.json`, default `DEFAULT_CATALOG_URL`), per-variant Edit / Publish-to-GitHub / Delete (confirm dialog).
- `app/faceid_app/openings.py` (new lib): config CRUD, `create_variant`/`update_variant` (logo swap), `export_package`→zip, `install_package` (zip sanitised: path-traversal + size/file-count caps), `download_and_install`, `fetch_catalog`. `.faceopen` = zip(`opening.json` + logo.svg/png). Publish dialog exports to `~/Downloads` and shows copyable `gh gist create` / `gh release upload` commands.
- Repo assets: `openings/README.md`, `openings/catalog.json` (lists `sunset`), `openings/packages/sunset.faceopen` (built via lib under scratch `XDG_CONFIG_HOME`).
- Verified: `node --input-type=module --check` on all 6 ext js files; `python3 -W error -m py_compile` on main/openings; library round-trip (create→export→install, logo swap/clear) via scratch XDG. User-level deployed + `gnome-extensions disable/enable` → STATE ACTIVE, no new journal errors.
- **GDM root cause found (the earlier “no animation at login”):** `/usr/share/gnome-shell/extensions/faceid@nim/pill.js` (from 15:30) still called `this._remove_style_class_name` → `reset()` threw on every lock, pill never attached at the GDM login. User-level copy was fixed at 15:37. **System-wide must be re-synced (pend user sudo): copy `opening.js` + `pill.js` to /usr/share too, then restart gdm.**

## Shell extension: opening animation + Welcome <uid>
- `shell-ext/faceid@nim/pill.js`: opening phase now delegated to `OpeningScene` (./opening.js) — capsule grows while the active variant plays, then hands off to glyph (`Looking for you…`). After matched→checkmark→padlock, **welcome** phase: `Welcome` (big, `faceid-welcome`) + uid (the identity name from matched `reason`, `faceid-uid`) pops in (scale 0.6→1 easeOutBack, `WELCOME_MS=1200`) then contracts.
- `logo.svg` ships in the extension dir (builtin `logo` variant); loaded via GLib file URI so it works user-level AND system-wide.
- `stylesheet.css`: `.faceid-logo` (36px bg-size), `.faceid-welcome` (20px/700), `.faceid-uid` (13px/600).
- Deployed to `~/.local/share/gnome-shell/extensions/faceid@nim/` and reloaded (`gnome-extensions disable/enable faceid@nim`) — no errors. The `TypeError: this._indicator is null` in the journal was ubuntu-appindicators, unrelated.
- Syntax check: `node --input-type=module --check < f.js` for each file (gjs has no --check).
- **GDM (system-wide) install — PENDING user's sudo steps (now incl. the opening.js + fixed pill.js):**
  1. `sudo cp shell-ext/faceid@nim/{pill.js,opening.js,extension.js,dbusClient.js,faceGlyph.js,logo.svg,stylesheet.css,metadata.json} /usr/share/gnome-shell/extensions/faceid@nim/`
  2. `sudo chown -R root:root /usr/share/gnome-shell/extensions/faceid@nim`
  3. `sudo -u gdm dbus-run-session -- gsettings set org.gnome.shell enabled-extensions "['faceid@nim']"` — DONE, dconf activated, no error.
  4. `sudo -u gdm dbus-run-session -- gsettings get org.gnome.shell enabled-extensions` → should print `['faceid@nim']`.
  5. `sudo systemctl restart gdm` — end current session, check pill at GDM login.

## Facts / reference
- GNOME Shell 46.0, Wayland, ubuntu:GNOME, gjs 1.80.2.
- Daemon DBus interface `org.faceidnim.Daemon1` @ `/org/faceidnim/Daemon1` on system bus. Methods: `GetSettings`, `SetSettings(json)` (ungated, persists to /etc/faceid-nim/config.toml; ignores daemon-owned paths), `Diagnostics`, `TestScan` (0 args; takes a real scan, error if no face), `Enroll` (polkit-gated), etc. Signal `ScanState(state, progress, reason)` → drives the pill; on matched `reason` = winning identity name.
- `daemon/src/main.rs:32`: `CONFIG_PATH = "/etc/faceid-nim/config.toml"`.
- `daemon/src/dbus.rs`: get_settings ~155, set_settings ~160, diagnostics ~176, scan_state signal.
- PAM: `/etc/pam.d/common-auth` line 17 `auth [success=3 default=ignore] pam_faceid.so quiet timeout=4500`; `pam_faceid.so` in `/usr/lib/security/`. Both `faceid-nimd` + `faceid-vision` systemd units enabled at boot.
- `faceid-vision.service` ExecStart uses `venv/bin/python3 -m faceid_vision --socket ... --device /dev/video1 --model-dir ... --manifest ...` (no `--mode`; daemon sends explicit mode per request).
- Extension is user-level (`~/.local/share/gnome-shell/extensions/faceid@nim/`); installed `extension.js` DIFFERS from repo (extra `_promptBox` fallbacks) — do NOT clobber the user-level extension.js wholesale. repo extension.js is 153 lines incl. multi-path `_promptBox`.
- `redeploy.sh` uses staged install (`mktemp -d`, cp vision/faceid_vision, pip `--force-reinstall --no-deps`) because in-tree `vision/build/` is root-owned.
- No passwordless sudo (`sudo -n` fails); user runs sudo interactively. I (agent) ran system-bus calls + writes only as zang.
- `bin/faceid-nim status`: worker socket shows "MISSING" for zang because `/run/faceid-nim/worker/` is `drwxrwx--- root faceid` (permission artifact, not a real failure).
- Vision suite 39 passed (`vision/tests/test_moire.py`, `test_liveness_mode.py`); daemon 17 passed.
- App logo: `app/data/icons/hicolor/scalable/apps/org.faceidnim.App.svg`.

## Constraints
- Never bypass moire, never lower the threshold as "the fix", never weaken tau. Mode is the sole authority — never promote a scan to IR analysis from negotiated format.
- `tau=0.40` is still a placeholder — measure with `eval/far_frr.py` when pairs data exists.

## Next steps for user
1. **Sync the system-wide extension** (so the GDM login screen gets the fixed + new file): `sudo cp shell-ext/faceid@nim/{pill.js,opening.js,extension.js,dbusClient.js,faceGlyph.js,logo.svg,stylesheet.css,metadata.json} /usr/share/gnome-shell/extensions/faceid@nim/ && sudo chown -R root:root /usr/share/gnome-shell/extensions/faceid@nim`, then `sudo systemctl restart gdm`.
2. **Sync the installed settings app** (the .deb copy at `/usr/share/faceid-nim/app/faceid_app` predates the Opening page): `sudo cp -r app/faceid_app/. /usr/share/faceid-nim/app/faceid_app/`.
3. **Test the new Opening page**: fresh app → **Opening**, switch active (e.g. `glow` or `none`), Create one with a logo, install the shipped `openings/packages/sunset.faceopen` (or from URL after the repo is pushed), and lock+unlock to see it at the next scan.
4. **Push the repo** so `openings/catalog.json` (→ `raw.githubusercontent.com/Gaurav-x111/faceid-nim/main/...`) resolves for “Browse community”.
5. Real-face validation: lock + unlock; check the chosen opening animation + `Welcome <uid>`; confirm no `liveness_deny:moire`.
6. Optional: `strictness="heavy"` + `require_attention=true` (was offered; not decided).