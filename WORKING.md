# WORKING.md — how faceid-nim is put together

The only design doc. Everything else (`docs/`, `CONTEXT.md`, test
reports) was removed 2026-10-12 to stop stale docs contradicting the
code. If it is not here, read the source.

## What this software is

Face unlock for Ubuntu/GNOME: enroll once in the app, then GDM login,
lock-screen unlock, `sudo` and polkit prompts can accept your face
instead of a password. Password always works (`sufficient` PAM flag).

| Piece | Language | Job |
|---|---|---|
| `pam_faceid.so` (`pam/`) | C | Asks the daemon over a socket, returns a PAM code. No AI, no camera. |
| `faceid-nimd` (`daemon/`) | Rust | Root daemon: policy, encrypted template store, matching votes, audit log, D-Bus API, spawns/talks to the worker. |
| vision worker (`vision/`) | Python | Unprivileged: owns the camera, runs YuNet+SFace/MediaPipe via ONNX Runtime, returns embeddings + liveness evidence (never sees templates). |
| `faceid@nim` (`shell-ext/`) | GJS | GNOME Shell lock-screen island: opening splash, scanner, result cards. Listen-only on D-Bus. |
| Face Unlock app (`app/`) | Python + GTK4/Adwaita | Enrollment wizard, identities, cameras, settings, opening animations. |
| `faceid-nim` CLI (`bin/` + `packaging/scripts/`) | POSIX shell | `status`, `audit`, `fetch-models`, `diagnose`, `tune`, `devices`. |
| Models (`models/manifest.toml`) | TOML | Every model pinned by SHA-256; worker refuses mismatches. |
| Packaging (`packaging/`, `debian/`) | make + shell | Builds the universal `.deb`, postinst wires services/models/cameras. |

## How a scan flows (the one path everything uses)

1. PAM (login/`sudo`/polkit) or the app's Try-it-live calls the daemon.
2. Daemon opens one scan: worker grabs `/dev/video1` (or auto/IR), detects (YuNet), embeds (SFace 128-d), collects liveness (moiré, glare, blink, planarity, attention; ML slot currently empty — see below).
3. Daemon votes templates (cosine, k-of-n, spectrum-gated), checks liveness, writes one audit line (`/var/log/faceid-nim/audit.log`, never biometrics), emits `ScanState` on the system bus.
4. The shell pill animates from the bus signal only. The app's Try-it-live toasts Recognised/Not recognised.

Enrollment is separate: the app wizard captures poses → daemon stores templates. **Enrollment never plays the animation.** The animation plays on every *scan*; it lives in the shell extension and re-reads `~/.config/faceid-nim/openings.json` each time. GNOME animations must be on or the iPhone video degrades to a logo pop:
`gsettings set org.gnome.desktop.interface enable-animations true`.

## How the .deb is made

```sh
make deb DEB_FLAGS=-d   # -d: cargo/rustc are rustup-managed, invisible to dpkg
```

`debian/rules` compiles the daemon + PAM module, builds the worker
venv, installs app/shell-ext/scripts/manifest, and runs guards: an
import check plus a `diff` proving installed files equal the source
(a stale `vision/build/` once shipped old code — the build now fails
instead). `Architecture` is stamped per-arch (`amd64`/`arm64`);
`Architecture: any` is wrong (ships binaries + arch wheels).
`postinst` fetches + verifies models, auto-detects cameras into the
generated config, and prints achieved state. Install with
`sudo apt install ./dist/faceid-nim_*_amd64.deb`.

## Folder tour — what lives where, and what changing it does

- `pam/` — the C PAM module. Change: affects every login path; rebuild + relogin to test. Compile must stay `-Wall -Wextra -Werror` clean.
- `daemon/src/` — Rust: `main` (wiring), `config` (TOML + defaults), `session` (scan→verdict→audit), `matcher`/`store`/`enroll` (templates), `worker` (protocol), `audit`, `policy` (lockout), `dbus` (API incl. `TestScan`), `ipc`, `authz`. Change: `cargo test`, `cargo fmt --check`, `cargo clippy -- -D warnings`.
- `vision/faceid_vision/` — `detect` (YuNet), `embed` (SFace), `align`, `landmarks` (MediaPipe Tasks backend registry), `liveness/` (blink/geometry/screen/passive/challenge/ir), `scan` (evidence loop), `fuse` (deny/confirm), `models` (manifest pins), `cameras` (discovery), `meter` (room light), `rt` (one ONNX session factory), `hardware` (providers/threads, never raises). Change: `pytest vision/tests`. Never let liveness look at similarity scores.
- `shell-ext/faceid@nim/` — `extension.js` (single pill controller), `pill.js` (one-clock choreography; iphone video holds as the whole wait), `opening.js` (variant engine), `faceGlyph.js` (apple/arena/classic renderers), `scanGuard.js` (terminal-state exclusivity). Change: `node --check`, `node --test shell-ext/tests/`, then copy to `/usr/share/gnome-shell/extensions/faceid@nim/` + shell restart (Alt+F2 `r` on X11, relogin on Wayland).
- `app/faceid_app/` — `main.py` (all pages), `openings.py` (animation registry), `prefs.py`, `dbus_client.py`, `camera_discovery.py`, `camera_test.py`, `onboarding.py`, `auto_tune.py`. Runs on system python (venv is worker-only, no PyGObject). Change: `pytest app/tests`; live copy is `/usr/share/faceid-nim/app` — copy + reopen app (kill stale `faceid_app.main` first). `~/.local/bin/faceid-app` runs the dev tree instead when invoked from a terminal.
- `models/` — `manifest.toml` pins. Unpinned (`optional`) entries are refused, never fetched. Re-pin only with live-face AND spoof-sample evidence on the target camera.
- `packaging/` + `debian/` — units, polkit policy, fetch/verify scripts, rules. Change: rebuild the `.deb`; never hand-edit `/usr/share` without mirroring source.
- `linux-anim/` — source clips (`glance_anim/assets/`) + `test_anim.py` harness + frame extractor. Change: re-extract PNG frames if the mp4 changes.
- `openings/` — community catalog for shareable animations.
- `eval/` — FAR/FRR + APCER/BPCER harnesses. `tau = 0.40` is still a placeholder: measure with `eval/far_frr.py`, never quote accuracy without it.
- `bin/` — dev copy of the CLI (source of truth: `packaging/scripts/faceid-nim`; keep them identical).
- `faceid-nim-dynamic-island/` — stale export copy, not shipped; do not edit (it already drifted once).
- `docs/demo/` — README demo gif + current app screenshots.
- `dist/` — built `.deb`s only (one kept: `faceid-nim_1.1.1-1_amd64.deb`).

## Session history carried over (why things are the way they are)

- Auto camera mode: worker meters room light per scan (dark→IR, lit→RGB), dual-spectrum enrollment, spectrum-gated voting. 24 Rust + 143 Python tests green at the time.
- 2026-10-12, live-face measurement killed the ML anti-spoof: the pinned MiniFASNet-V2 export scored a real webcam face **0.994 replay, every frame**, any crop scale/channel/format. Independent V2 export: same. V1SE: 0.973. facenox 2-class: erratic (−5.1…+6.3). Entry unpinned; worker runs classical cues; app says "ML anti-spoof off — classical checks active". Full table in README.
- Two animations at once: the iphone splash handed off to the rings scanner. Fixed: video holds as the whole wait; `iphone` added as a scan-face (renders the apple face); defaults are iphone+iphone.
- The app's canned scanning/recognized/denied previews were removed (they drifted from the pill); Appearance has a Try-it-live real TestScan instead.
- `tau = 0.40` still has no evidence behind it. MiniFASNet re-pin needs hardware evidence. First-run tau auto-calibration is still unwired.

## Verify

```sh
make test        # cargo test + pytest (PYTHONPATH=vision:app)
faceid-nim status && sudo faceid-nim audit 5
```
