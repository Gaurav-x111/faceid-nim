# faceid-nim — Session Context (save 2026-09-26, later)

## Objective
- **Auto dark/light camera mode** (DONE, code) + **universal one-command
  install** (DONE, code). The worker measures room light per scan and
  picks the sensor itself, so a dark room uses IR recognition and a lit
  room uses RGB. Plus dual-spectrum enrollment, a room-light readout in
  the app, and a light-calibration step in tune.

## What we are doing (active thread)
- **DONE — auto-mode plan (items 1-9 of the previous context).** The
  parallel pts/0 session implemented it: `meter.py`, `auto` in
  `protocol.py`/`__main__.py`, `spectrum` in `ev_done` → `worker.rs`,
  `CameraMode::Auto`, store v2 spectrum tags, spectrum-gated voting,
  dual-spectrum enrollment, room-light readout, tune calibration.
  All 24 Rust + 143 Python tests pass.
- **DONE — the "make it universal" phases (1-6, code half).** All code
  work is complete and verified, including a real `.deb` build. See
  "What I just did" below. This session found and fixed **four
  latent bugs that made the product non-functional or dishonest**, plus
  one packaging bug that shipped stale code.
- **LEFT — the user's half.** On-device verification, release accounts,
  and the maintainer email. Needs a real machine, a real face, and
  accounts I do not have. See "Left for the user".

## What I just did (verified, all green)
- `vision/faceid_vision/landmarks.py` rewritten as a backend registry:
  MediaPipe **Tasks API** first, legacy `solutions` second, and every
  failure carries a reason. `face_landmarker.task` pinned in the
  manifest (sha256 `64184e2…`, downloaded and hashed for real).
- `models.py`: new `optional` flag + `resolve_optional()`. Detector and
  recogniser stay required; optional models are refused when unpinned.
- `vision/faceid_vision/hardware.py`: provider selection by building a
  real session and checking `get_providers()` (ORT downgrades to CPU
  with only a log line, so its provider list is not evidence); cgroup
  and affinity-aware CPU count; threads capped at 4; caches to
  `/run/faceid-nim/worker/hardware.json`; **never raises**.
- `vision/faceid_vision/rt.py`: one session factory; `embed.py` and
  `liveness/passive.py` both use it, so they can no longer drift.
- `packaging/scripts/gpu-devices`: widens the worker sandbox **only** when
  a GPU is present, and removes a stale drop-in when it is not.
- `postinst` now really fetches the models (non-fatal), auto-detects the
  cameras into the generated config, and prints achieved state instead
  of instructions. New `faceid-nim-fetch-models.service` (oneshot,
  `ExecCondition` skips when verified) retries on every boot. `postrm`
  stops the units and removes the drop-in.
- `bin/faceid-nim status` reports the **live** landmark backend,
  anti-spoof state, execution provider, thread count and model — not just
  what is installed. The app warns when liveness is reduced and never
  shows a green state for a check that is not running.
- Discovery core moved to `vision/faceid_vision/cameras.py` with
  `auto_config()`; the app re-exports it (one implementation).
- `debian/rules`: liveness extra is arch-tolerant and non-fatal (this is
  what makes arm64 buildable at all), plus an import check and a
  `diff` guard proving the installed package matches the source.
- `install.sh` picks the `.deb` by `dpkg --print-architecture` and
  refuses a mismatched package. CI builds both amd64 and arm64.

## Last audit pass: what the earlier sessions left undone
Found and closed by a "what is actually left?" pass, not by reading plans:
- **The audit never recorded which sensor ran.** The worker measures room
  light, resolves a spectrum, and sends both; the daemon parsed `spectrum`
  but silently **discarded `luma`**, and neither reached the audit log. So
  "it did not unlock in the dark" was unanswerable from `faceid-nim audit`,
  which is exactly the on-device test this project depends on. Both are now
  in `audit::Event` (`spectrum`, `luma`) with tests, including one that
  asserts no biometric ever reaches a log line.
- **`app/data/gschema.xml` never learned about `mode = "auto"`** while
  `CameraMode` in config.rs had it. The schema's own comment says it mirrors
  the Config field names, so this was drift by its own definition. Fixed,
  and `app/tests/test_mode_sets_agree.py` now cross-checks the Rust enum,
  the gschema enum and the app's `CAMERA_MODE_VALUES` against each other
  (verified: it fails when the gschema entry is removed).
- `docs/UNIVERSAL-SETUP.md` still said "PLANNED, NOT IMPLEMENTED" -- a trap
  for the next session. Now marked implemented, with a new Part 7 listing
  the two genuinely open items.
- README still claimed "Ubuntu 24.04+ on amd64" after the jammy/arm64 work.
- `prefs.py` documented three `camera_mode` values while accepting four.

## Release engineering: two BLOCKING bugs found in review
- **`Architecture: any` was wrong, and it would have silently destroyed
  the arm64 work.** The package ships a compiled daemon, a compiled PAM
  module and arch-specific wheels (onnxruntime/opencv/mediapipe), so it
  is architecture-dependent. Worse, `dh_gencontrol` does NOT pass `-P` to
  dpkg-gencontrol, so the stanza is used verbatim: verified with
  `dpkg-deb` that an `any` stanza yields `faceid-nim_<ver>_any.deb` from
  *every* matrix leg, so one architecture replaces the other on the
  release page, and `install.sh` (which asks for `_amd64.deb`/`_arm64.deb`)
  404s on both. Fixed with `override_dh_gencontrol`, which stamps
  `$(DEB_HOST_ARCH)` and then **restores** debian/control so a build
  leaves no spurious edit in the tree. A two-arch stanza
  (`Architecture: amd64 arm64`) is also wrong: it yields a malformed
  `_amd64 arm64.deb` plus a control-parse warning. Verified by build:
  `dist/faceid-nim_1.1.1-1_amd64.deb`, field `amd64`, ELF X86-64.
- **`make deb` could not run anywhere.** `Build-Depends` lists `cargo` and
  `rustc`, but the only toolchain new enough for the daemon is rustup,
  which installs no dpkg package — so `dpkg-checkbuilddeps` fails on every
  machine, stock CI runners included. Added `DEB_FLAGS ?=` (pass `-d` in
  CI) plus a rustup step in both workflows. jammy's apt rustc is 1.59,
  below the declared 1.75, so `apt install rustc` is not an option either.
- Also fixed in ci.yml: the toolchain check ran *before* rustup was
  installed, and the post-build assertions globbed `../faceid-nim_*.deb`
  while the Makefile moves the artifact to `dist/`. Added an assertion
  that the declared Architecture matches both the matrix and the ELF
  header, so the `any` regression cannot come back.

## Four latent bugs found (all were silent)
1. **`models.py` had a syntax error** (`license: str,`). `build_engine`
   — the function that constructs the entire worker — could never run.
   No test imported that module, so the suite was green. Fixed, plus
   `vision/tests/test_imports.py` compiles and imports everything.
2. **MediaPipe Face Mesh was dead** in the shipped venv: the worker asked
   for `mp.solutions.face_mesh`, an API the installed wheel no longer
   ships, and the `except Exception` swallowed the AttributeError. Blink,
   parallax, challenge and the attention check were inert no-ops and
   `strictness = heavy` could never be satisfied. Verified directly
   against the real wheel and fixed via the Tasks API.
3. **Stale `vision/build/` shipped old code inside a fresh `.deb`.**
   setuptools reuses `build/lib/<pkg>/`; the build printed "venv OK"
   while the packaged `landmarks.py` was hours out of date. Fixed by
   deleting the tree before the build, plus a `diff` guard that fails the
   build on ANY divergence (not a grep for a marker, which would rot).
4. **`emitter_difference()` crashed on an out-of-frame box** — the
   `size == 0` guard ran *after* `cvtColor` on an empty array.

Also: the app's shim path resolution could not find `site-packages` in
the real installed layout, and `exec_module` without registering in
`sys.modules` broke `@dataclass`. Both fixed and tested for real
(`app/tests/test_discovery_layouts.py` builds each layout and loads it in
an isolated interpreter).

## Left for the user (needs a machine, a face, or accounts)
1. **Install and verify on the laptop.** `dist/faceid-nim_1.1.0-1_amd64.deb`
   is built. `sudo apt install ./dist/faceid-nim_1.1.0-1_amd64.deb`, then
   `sudo faceid-nim status` — expect `landmarks : mediapipe_tasks`, and
   the model fetched automatically with no `fetch-models` command.
2. **Dark room / lit room.** Auto mode must give IR unlock in the dark
   and RGB unlock when lit. `sudo faceid-nim audit 20` for the reasons.
3. **Measure tau per spectrum** (`eval/far_frr.py`). The shipped 0.40 is
   still a placeholder with no evidence behind it.
4. **MiniFASNet licence decision.** Still unpinned, so refused, and every
   surface says so. Either pin a permissively-licensed ONNX export or
   leave it reported as unavailable. Not a code blocker.
5. **Maintainer email.** DONE in worktree (2026-09-26): `control` now
   `Gaurav-x111 <gauravshah0777@gmail.com>` (from git config) +
   `Vcs-Git`/`Vcs-Browser`; `Architecture: amd64` → `any`; new
   `1.1.1-1` changelog entry (`jammy`) parses clean via dpkg-parsechangelog.
   `install.sh` pin moved to single `PKG_VERSION=1.1.1-1` with leading-`v`
   stripping. `release.yml` now builds amd64+arm64 on Ubuntu 22.04.
6. **Build the base distro.** DONE in worktree (see 5): release builds on
   22.04/jammy so one artifact installs on 22.04 and newer.
7. **Push a tag** — STILL LEFT (needs the user): existing tags are named
   `faceid@nim-v1.0.2`, which does NOT match release.yml's `v*` glob, so
   no release has ever built. After committing item 5, run
   `git tag -a v1.1.1 -m "faceid-nim 1.1.1" && git push origin v1.1.1`.
   Note: a `git stash` entry from a 2026-09-26 recovery is still kept
   (`stash@{0}`); drop it once the worktree is committed.
8. **IR emitter** (`linux-enable-ir-emitter`) if the IR path is to be
   proven rather than assumed on this laptop.

## Key findings (verified in code, do not re-derive)
- `debian/rules` is a makefile: make hands each recipe line to the shell
  separately, so an `if ... fi` block MUST be one logical line with `\`
  continuations. A multi-line block is a syntax error.
- The venv in a `.deb` uses *relative* symlinks, so extracting to a
  non-root DESTDIR leaves `venv/bin/python3` unrunnable. Not a bug.
- This repo is installed **editable into the system python**
  (`__editable__.faceid_vision-0.1.0.pth`). Any test of a packaged
  layout must use `python3 -I` or the ambient install answers first.
- `mediapipe` 0.10.35 has `tasks/` but no `solutions/`; LIVE_STREAM needs
  a result callback, `VIDEO` does not and is the right mode for camera
  frames (strictly increasing ms timestamps).
- The app runs on the *system* python; the venv is worker-only and has no
  PyGObject. So `app/faceid_app` cannot import anything from the venv by
  default — hence the roots list and `FACEID_VISION_ROOTS` override in
  the discovery shim.
- ONNX/CNN stack: YuNet (detect) + SFace 128-d (recognise) via
  onnxruntime, ArcFace 5-point similarity align to 112×112, cosine +
  k-of-n voting in Rust. Liveness is mostly classical CV (FFT moiré,
  glare, IR skin response, homography planarity, EAR blink), not deep
  learning.
- `faceid-vision.service` has `DevicePolicy=closed` with only
  `char-video4linux`; `CapabilityBoundingSet=` is empty and must stay so.

## How to verify
- `make test` (cargo test + pytest), `cargo fmt --check`, `cargo clippy
  --all-targets -- -D warnings`, `node --input-type=module --check` on
  the extension JS, `gcc -Wall -Wextra -Werror` on the PAM module.
- `.deb`: `dpkg-buildpackage -us -uc -b -d` (the `-d` is needed here;
  cargo/rustc are rustup-installed, not dpkg-installed). Then
  `dpkg-deb -x` and import the packaged venv's site-packages to prove the
  shipped code is current.

## Genuinely still open (not code-blocked)
1. **First-run automatic tau calibration.** `auto_tune.py` can measure tau
   from a `.npz`, but it is not wired into the enrollment wizard, so a fresh
   install still ships `tau = 0.40` with no evidence behind it. Needs the
   wizard to capture passes, compute the genuine/impostor separation, and
   write it through the existing polkit-gated `SetSettings`. Report the
   measured EER, never "99% accurate".
2. **MiniFASNet licence.** No stably-hosted permissively-licensed ONNX
   export is known, so the manifest entry stays unpinned and
   `resolve_optional()` refuses it. Every surface now reports the check as
   unavailable, so this is a known, honest state rather than a bug.

## Pending from the user's earlier request
- `sudo faceid-nim audit 20` + `faceid-nim status` to pinpoint their
  personal non-recognition cause (likely a liveness veto or lockout).
  The TestScan pipeline fix for that landed earlier.
