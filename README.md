# faceid-nim

Face unlock for Ubuntu 24.04+ / GNOME, with a Dynamic-Island style
lock-screen animation and customizable opening animations.
Supports **RGB**, **IR**, and **hybrid (RGB + IR)** cameras.

> **This is a convenience feature, not Face ID.**
> On an ordinary RGB webcam it can be defeated by a decent video replay.
> With an IR camera (or hybrid IR liveness) it is substantially
> stronger. Twins and siblings raise false accepts. Your password
> always works.

## What it is

| Component | Language | Job |
|---|---|---|
| `pam_faceid.so` | C | Asks the daemon, returns a PAM code. No AI, no camera. |
| `faceid-nimd` | Rust | Root daemon: policy, template storage, matching, D-Bus. |
| vision worker | Python | Unprivileged: owns the camera, produces embeddings + liveness cues. |
| `faceid@nim` | GJS | GNOME Shell lock-screen island (scanner) + result cards. |
| Face Unlock app | Python + GTK4 | Enrollment, identities, settings, opening animations, camera modes. |

Design rules: the PAM module does no AI; the worker never sees
templates; the UI never sees frames or embeddings; and the PAM profile
is `sufficient`, so **your password always works**.

## Summary

**Technical:** A Linux face-unlock system built in five languages. The
Rust daemon handles D-Bus auth, encrypted template storage, and
template-match voting (best-of-n similarity scores must reach threshold).
The Python vision worker runs YuNet for detection, a landmark-based
similarity warp to 112x112, SFace for 128-d embeddings via ONNX Runtime,
and MediaPipe's 478-point face mesh for liveness cues. A C PAM module
bridges to system auth. A GTK4 settings app provides enrollment and
animation editing. A GNOME Shell extension renders the Dynamic-Island
pill. Packaged as a universal `.deb` with arch detection, auto-model
fetch, and systemd units.

**Theory:** Dual-spectrum (RGB + IR) enrollment with spectrum-gated
voting: the worker measures room light per scan and picks the sensor
automatically — dark rooms use IR, lit rooms use RGB. Liveness combines
7 classical cues (glare, moire FFT, IR skin response, homography
planarity, EAR blink, parallax, challenge-response). A passive ML
anti-spoof vote exists in the design but ships **off**: no verified
export passes on real hardware yet (measured 2026-10-12, see
[ML anti-spoof](#ml-anti-spoof-model-minifasnet-v2-optional)), so the
worker runs the classical cues and every surface says the ML check is
unavailable. The pill lives in top chrome above the screen
shield so the success animation survives the unlock.

**Threat model:** Defends against printed photos, phone-screen replay,
and video replay (classical liveness cues + IR; a learned CNN vote
joins only when a model proves itself on the target camera — treat
video-replay defense as best-effort). Does NOT defend against
masked attackers, twins, or adversarial perturbations. RGB-only mode is
fundamentally spoofable — IR is what makes it credible. Any failure
(camera, GPU, emitter, model) falls back to password; the system never
fails open.

**The three hardest problems:**

1. **Spectrum-gated enrollment** — capturing and matching faces under
   both RGB and IR, then voting per-spectrum so a dark room doesn't
   force a re-enrollment.
2. **The liveness stack** — 7 classical CV cues running in real-time
   on CPU with no false rejects on legitimate users, plus a
   deliberately empty slot for an ML vote that only ships when a
   model earns it with hardware evidence.
3. **The PAM/daemon security boundary** — the worker is unprivileged
   and never sees templates; the daemon never sees frames; the PAM
   module does no AI. Password always works (`sufficient` flag).

## Before you download the `.deb` (prerequisites)

- **Ubuntu 22.04+ (jammy) or Debian 12+**, on **amd64 or arm64**, with
  **GNOME Shell 45+** (the lock-screen pill needs it; the PAM unlock
  itself works on any session)
  (24.04 ships Shell 46 — supported).
- A working **webcam** (`/dev/video*`). An IR sensor is optional but
  recommended — the app auto-detects it.
- **~1 GB free disk** (app + ~500 MB recognition models) and
  **internet access** for the one-time model download.
- `sudo` access (install touches PAM, systemd services, `/etc`).
- Know that **GNOME Keyring still needs your real password** after a
  face login — that is a GNOME limitation, not a bug.
- Keep a **root shell or recovery option** handy while enabling
  anything under `/etc/pam.d/` (see [Recovery](#recovery)).

## Install

Pick the file for your machine from the [releases
page](https://github.com/Gaurav-x111/faceid-nim/releases) — `amd64` or
`arm64` — and:

```sh
sudo apt install ./faceid-nim_*_amd64.deb     # or _arm64.deb
```

That is the whole procedure. The install itself downloads and
checksum-verifies the models, detects your cameras (colour and infrared
if you have one), sizes the inference threads to the machine, starts the
services and prints what it actually managed to set up:

```
faceid-nim installed. Face unlock is DISABLED.

  models    : ready
  landmarks : blink / 3D / attention checks active
  anti-spoof: unavailable (no verified ML model for this hardware — classical cues active)
  device   : CPU only
  camera   : /dev/video0 present (the app picks RGB/IR)
```

If the machine was offline during the install, the models are fetched
automatically on a later boot — or run `sudo faceid-nim fetch-models`.

Prefer a one-liner? It detects your architecture and picks the right
package:

```sh
curl -fsSL https://github.com/Gaurav-x111/faceid-nim/releases/latest/download/install.sh | sudo sh
```

Check what is running at any time with `sudo faceid-nim status`, which
reports the live liveness backends rather than just what is installed.

Then **log out and back in once** — GNOME Shell caches extension
code, and the lock-screen island only loads the new copy after a
fresh login.

Face unlock installs **disabled** — enabling it is your explicit
opt-in. The app switch asks for your password through polkit, calls the
daemon’s protected `SetPamEnabled` operation, and then saves the daemon
setting. `SetSettings` alone never enables PAM. `pam-auth-update` inserts
the `pam_faceid.so` line (sufficient) into `/etc/pam.d/common-auth`.

**Upgrade:** install the newer file the same way; dpkg upgrades in
place and keeps your config. Never ship two builds with the same
version — always bump `packaging/debian/changelog` per release.

**Uninstall:** `sudo dpkg -r faceid-nim` (keep config) or
`sudo dpkg --purge faceid-nim` (remove config too).

## How to use

1. Open **Face Unlock** → the wizard checks the service, camera and
   models, then walks you through **9 head poses (~20 s)**.
2. Use **Test scan** before enabling: it captures a face and compares it
   against your enrolled templates with the normal voting rule, but does
   not unlock anything or change the failure counter.
3. Back in the app, flip the **enable switch** in Overview. (It warns
   you if no face is enrolled yet.)
4. Test without locking first: `sudo -k && sudo true` — you should see
   the island verify silently (no greeting speech for sudo, by design).
5. Lock with `Super+L`: the island springs open at the top, scans,
   then shows `✓ Verified / Welcome / <your name>` and unlocks.
6. Optional: Settings → **Voice greeting** speaks
   `Welcome {name}` on laptop unlock only (`spd-say`/`espeak-ng`).

## The lock-screen animation

Locking shows a black **Dynamic-Island notch** pinned to the top
edge, horizontally centred — never the screen centre. Inside it: the
2.5 cm scanner (thin track + progress arc + smiling face), and a
status line: **"Looking for your face"** → **"Face ID · Verified"**.
A very fast match briefly shows **"Face ID · Locking on"** so the
scan never flashes past unseen. On success the ring resolves green,
a ✓ draws with a particle burst, and a small card drops **below**
the island — `✓ Verified` / **Welcome** / your name at login,
`✓ Verified` alone for sudo/PAM. On failure: `× Not recognized` /
"Use your password" — the password prompt is the retry path; hovering
the island does nothing by design (a hover rescan would let anything
that moves the mouse fire the camera from the lock screen).

Everything animates off a single 16 ms clock keyed to actual elapsed
time (scan ≈ 1.5 s minimum, full sequence ≈ 3.4 s), so a fast or slow
auth still lands correctly. Animations respect GNOME's
**enable-animations** accessibility setting (reduced motion shows the
outcome without the sweep/springs).

Driving the island without a working daemon (nested-shell test): lock
the nested session, then emit the daemon's `ScanState` signal by hand
(state, progress, reason) and watch the shell log:

```sh
loginctl lock-session
sudo dbus-send --system --type=signal /org/faceidnim/Daemon1 \
  org.faceidnim.Daemon1.ScanState string:"searching" double:0.0 string:""
sleep 3
sudo dbus-send --system --type=signal /org/faceidnim/Daemon1 \
  org.faceidnim.Daemon1.ScanState string:"matched" double:1.0 string:""
```

## Adding your own animation in GNOME

Customization lives in the app → **Settings → Appearance & Animation**,
stored per user in `~/.config/faceid-nim/openings.json`:

```json
{
  "active": "iphone",      // which opening animation plays at scan start
  "variants": {},          // user specs indexed by id
  "scan_face": "iphone"    // scanner style: iphone | apple (same) | arena | classic
}
```

**Scanner style** (`Settings → Appearance & Animation → Scanner`): shown as **iPhone
(default)** = thin track + progress arc + smiling face (same language as the
iphone opening video), **Smile** = identical face under its historic name,
**Orbit** = halo/crest/comet scanner, **Classic** = original ring.

**The opening** (`Settings → Appearance & Animation → Active animation`): built-ins `iphone`
(default, the unlock video), `logo`, `glow`, `none` — or **Create an opening animation**: pick
a headline, colours, easing, duration, optional logo → **Publish to
GitHub** exports a `.faceopen` package with ready `gh` commands.
Others install via **Install from file** / **Install from URL**, or
the **Browse community** catalogue. Your choice applies at the very
next lock — no extension reload, no restart. Replacing an existing id
asks for confirmation; deleting offers a 5 s Undo (spec restored, logo
files not recoverable).

`.faceopen` is a zip with `opening.json` (+ optional `logo.svg`/`png`):

| Field | Meaning | Values |
|---|---|---|
| `id`, `name`, `description` | identity | any text |
| `text` | headline | any text (`{name}` = scanned identity) |
| `bg`, `accent` | colours | `#rrggbb` or blank = keep default |
| `ease` | easing | `outCubic`, `outBack`, `outExpo` |
| `fade_ms` | duration | 120–3000 (clamped) |
| `scale`, `ring` | size / halo-vs-logo | number > 0; `true`/`false` |

Animations are **declarative only** — data, never code, so an
installed package can repaint the island and nothing else. Corrupt or
non-object `opening.json` metadata is rejected with an install error.
To add yours to the catalogue, PR your `.faceopen` into
`openings/packages/` plus one entry in `openings/catalog.json`
(see `openings/README.md`).

## App navigation & GitHub

Sidebar: **Overview, Identities, Cameras, Security, Settings,
Diagnostics**, plus a separated **GitHub ↗** link at the bottom
(`View source code, releases, and development updates.`). The link
opens `https://github.com/Gaurav-x111/faceid-nim` in the system
browser via `Gio.AppInfo.launch_default_for_uri` — no embedded
browser, no startup network request.

Appearance & Animation lives under **Settings** (not a top-level
page): Animations toggle, Active animation (`iphone` video pill,
`logo`, `glow`, `none`), Scanner style (`iPhone`, `Smile`, `Orbit`,
`Classic`), a **Try it live** row that runs a real daemon TestScan
through your camera (the lock pill animates exactly as on unlock;
Recognised / Not recognised, nothing unlocked), then Make your own /
Get animations / Your animations. The old canned scanning/recognized/
denied previews were removed — they drifted from what the pill
actually plays. No control here can invoke privileged auth actions;
the lock-screen pill is listen-only (no bus method calls). Full
pipeline diagrams: `docs/animation-pipeline.md`.

Video: 432×432 H.264 60 fps source clips in
`linux-anim/glance_anim/assets/`; the lock screen plays 216×216 PNG
frames (`shell-ext/faceid@nim/glance-frames/`, success clip only —
denied is glyph-only by design). The app itself plays no clips —
verification is the Try-it-live real scan. No WebKit/HTML exists by
design.

**Update checking is deferred, not implemented.** The GitHub link does
not check versions, and no background request ever runs. To check
manually, compare the installed version (`faceid-nim status`) with the
published Releases page linked from the repo.

## Camera modes (RGB / IR / Hybrid)

Pick a mode in the app → **Camera**:

| Mode | Behavior |
|---|---|
| **Automatic** | Uses IR if an IR sensor is found, else RGB. |
| **Normal camera (RGB)** | Recognition on the color camera only; no IR checks. |
| **IR camera** | Recognition on the IR feed; IR liveness active. If no IR sensor exists, the app falls back to RGB with an in-app note instead of labeling an RGB scan as IR. |
| **Hybrid (RGB + IR)** | Recognition on RGB **plus** live IR anti-spoof (`mode both`). Strongest option when both sensors exist. |

The app discovers cameras itself (no hardcoded paths): combo webcams
register both sensors under one UVC card name, and the mono-only node
(e.g. `Integrated_Webcam_FHD` → `/dev/video3 · 640x360 GREY`) is
recognized as the IR camera. If no IR sensor exists, Automatic,
explicit IR, and Hybrid all resolve to RGB with an in-app note — the
app never silently burns a timeout on empty IR.

## ML anti-spoof model (MiniFASNet-V2, optional)

Most users can skip this section — face unlock works without the
model, on the classical cues alone, and that is the shipping state:
the entry is currently **unpinned**, so fresh installs skip it and
`status` reports `ML anti-spoof: unavailable`. It exists for those
who want to know what the extra liveness vote would do, and what it
would take to turn it on.

**What it is.** A passive anti-spoof CNN: given one 80×80 face crop it
scores whether the face is live or a presentation attack (printed photo
/ screen replay). It is one vote among the 7 classical cues, never the
authority — a scan it flags still needs nothing else to pass, and a
scan it clears can still be vetoed by glare, moiré, planarity or IR.

| | |
|---|---|
| Model | MiniFASNet-V2 (`2.7_80x80`), ONNX opset 11, ~1.7 MB |
| Source | `garciafido/minifasnet-v2-anti-spoofing-onnx` (Hugging Face) — a format conversion of the upstream `minivision-ai/Silent-Face-Anti-Spoofing` weights, bit-equivalent |
| License | Apache-2.0, same as upstream |
| Pin | SHA-256 in `models/manifest.toml`; the worker refuses to load a file that does not match. Currently **unpinned** (see measurement below), so fresh installs skip it |

**Why use it.** The classical cues catch most print/screen replay, but
they are hand-written heuristics. The CNN adds a learned
texture/frequency opinion on top — defense in depth for machines in
shared spaces, or for RGB-only laptops where there is no IR sensor to
back the decision. If you have IR + `light`/`heavy` strictness on a
personal laptop, the classical cues plus IR are already the strong
combination; the CNN is a smaller gain there.

**What changes when it is active.**

* `sudo faceid-nim status` shows `antispoof: ok` and
  `ML anti-spoof : active` instead of `unavailable`.
* A scan that looks like a screen or print gets an `antispoof_model`
  deny veto in the audit log (`sudo faceid-nim audit 20`) — same as any
  other liveness veto: the attempt fails closed to password.
* Cost: one extra CPU ONNX session per scan (~1.7 MB on disk,
  milliseconds on CPU). No GPU, network, or account needed.

**Setup (only if a model earns a pin).** While unpinned, postinst and
`fetch-models` skip the entry on every install and boot:

```sh
sudo faceid-nim fetch-models     # prints: antispoof unpinned, not fetching
sudo faceid-nim status           # expect: antispoof unpinned / ML anti-spoof unavailable
```

To activate a candidate export, pin its SHA-256 in
`models/manifest.toml`, reinstall (or copy the manifest to
`/usr/share/faceid-nim/models/`), then:

```sh
sudo faceid-nim fetch-models     # downloads minifasnet_v2.onnx, verifies SHA-256
sudo systemctl restart faceid-vision.service
sudo faceid-nim status           # expect: antispoof: ok / ML anti-spoof: active
```

To prove it is really wired up, hold a phone screen with a photo of
your face to the camera and run Test Scan — expect a reject with the
`antispoof_model` cue in the audit log. Your real face must still pass.

**Opting out.** Delete the file and restart the worker:

```sh
sudo rm /var/lib/faceid-nim/models/minifasnet_v2.onnx
sudo systemctl restart faceid-vision.service
```

`status` then reports `ML anti-spoof: unavailable`, the deny cue never
fires, and `verify-models` still passes (optional models are allowed to
be absent). It stays off across reboots; only an explicit
`sudo faceid-nim fetch-models` downloads it again.

**Measured on real hardware (2026-10-12, Integrated_Webcam_FHD
MJPG/YUYV 640x480).** The pinned export had to come out: it scored a
live face as replay attack at **p ≈ 0.994, stable across every frame**,
crop scale (1.0x–3.5x), channel order and pixel format — while pinned,
no unlock could succeed (`liveness_deny:antispoof_model`,
`valid_frames: 1`). Follow-up hunt, same live face, same bar (clearly
live on most frames, then a phone-screen spoof must score clearly
spoof under the same mapping before anything gets pinned):

| Candidate | Result |
|---|---|
| MiniFASNet-V2, independent export (same weights) | 0.994 spoof — the export was never the problem |
| MiniFASNet-V1SE (scale 4.0) | 0.973 stable spoof — family-wide failure on this sensor |
| facenox `best_model.onnx` (Apache-2.0, 2-class, 128 RGB) | logit diff swinging −5.1…+6.3 frame-to-frame — unusable as a gate |

Every tested CNN reads this webcam's live faces as attacks, so `ML
anti-spoof: unavailable` is the honest state here, not a bug. The
classical cues (moiré, glare, device-frame, blink, planarity,
attention) plus landmarks stay active, and unlock is proven working
without the CNN. One variable was never controlled: bright frontal
light — retest in it before declaring ML liveness impossible on a
given machine. Do not re-pin any export without live-face **and**
spoof-sample evidence measured on the target camera.

**Honest limits.** These models lose accuracy on cameras and lighting
they were not trained on, which is exactly why it is one vote, not the
decision. The veto threshold is fixed (spoof probability > 0.7) with no
config knob today — if it ever false-rejects your real face in bad
light, that is the opt-out above, plus an audit line to prove it. It
does not replace IR: RGB-only mode stays fundamentally spoofable, CNN
or not.

## Config reference

`/etc/faceid-nim/config.toml` (app-editable, daemon saves as root):

```toml
enabled = false        # face unlock is OFF until you enable it
strictness = "light"   # off: match only; light: match plus no spoof veto;
                       # heavy: match, no veto, plus a live-face confirmation
mode = "rgb"           # rgb | ir | both | hybrid ("both" and "hybrid" behave alike)
tau = 0.40             # similarity threshold — measure yours (see eval/)
vote_k = 3
vote_n = 5
scan_timeout_ms = 4000 # keep short: PAM converses sequentially
max_failures = 5
lockout_secs = 60
require_attention = true
camera = "/dev/video1"
ir_camera = "/dev/video3" # optional; missing IR falls back safely to RGB
timeline_enabled = false  # optional lock/unlock log
allowed_services = ["gdm-password", "sudo", "polkit-1", "login", "gnome-screensaver"]
exclude_users = []        # users who always fall through to password
```

Over D-Bus every field is all-or-nothing — an omitted `enabled`
disables the daemon. The app sends the full set. Changing settings,
enrolling, deleting data, or enabling PAM requires polkit; running Test Scan
does not write anything and therefore does
not require a password prompt.

## Build from source / make the `.deb`

```sh
make            # daemon + PAM module
make test       # Rust tests plus vision and app Python tests
sudo make install

make deb        # release .deb -> dist/faceid-nim_<ver>_amd64.deb
                # needs debhelper 13, cargo >= 1.75, libpam0g-dev
```

`debian/` at the repo root is a symlink to `packaging/debian/`
(that is what `dpkg-buildpackage` reads). Release checklist:
1. Bump `packaging/debian/changelog` — never reuse a version.
2. `make deb`, install the artifact on a clean 24.04 VM, run the
   Install steps above end-to-end (models, status, enroll, enable,
   lock, sudo).
3. Upload the `.deb` to the releases page.

## Measure your own thresholds

Before trusting the default `tau`, measure it:

```sh
mkdir -p eval/data/genuine/me eval/data/impostor/someone-else
python eval/collect.py  --root eval/data --out eval/out/embeddings.npz
python eval/far_frr.py  --npz eval/out/embeddings.npz --enroll-label me --target-far 1e-4
python eval/apcer_bpcer.py --root eval/data --strict heavy
```

`far_frr.py` reports EER + the threshold for your target FAR/FRR and
degrades honestly when you have too few samples. Publish measured
numbers, never "99% accurate".

## Troubleshooting (problems & fixes)

| Symptom | Fix |
|---|---|
| Nothing changes on lock after install/update | Log out and back in once — GNOME caches extension code |
| Island never appears, even after re-login | `journalctl --user -b \| grep faceid` — a `TypeError` there means the shell copy is stale; reinstall the `.deb` and log out/in again |
| `worker_reachable: false` | `sudo systemctl restart faceid-nimd faceid-vision`, then `sudo faceid-nim status` |
| Camera page says "vision worker is not running" | Models are missing: `sudo faceid-nim fetch-models` (the install does this for you, and retries on boot if it could not reach the network) |
| No IR camera in the app | Refresh the camera page; combo webcams list the mono node as IR (e.g. `/dev/video3`) |
| `apt` ends with `_apt Permission denied` | Benign — apt falls back from the sandbox when the .deb is in your home dir |
| Face not recognized repeatedly | Improve light (face the screen, even light), re-enroll, or lower strictness; check `tau` via [eval](#measure-your-own-thresholds) |
| Spoken greeting is silent | Install a speech engine (`sudo apt install speech-dispatcher` or `espeak-ng`); test from Settings → Voice greeting → Hear |
| App opens to a blank window | Update to the latest build (fixed libadwaita 1.4 incompatibility) |

## Behavior questions (quick answers)

- **I deleted my face — why did the camera still turn on?** It
  shouldn't anymore: with nothing enrolled the daemon refuses *before*
  the sensor powers on and falls through to your password. If the
  light still comes on, you're running an older installed build —
  reinstall the current `.deb`.
- **Only the animation I picked ever plays.** If success and failure
  (or splash, scan and result) seem to overlap, that's two scans
  finishing out of order, not two animations selected — a stale result
  can no longer overwrite a fresh one. At the GDM login screen the
  default iPhone animation always plays; your choice applies on your
  own lock screen.
- **Adding your own:** Settings → Appearance & Animation → Create (format above); swapping
  the iPhone video itself takes the mp4 *plus* re-extracted
  lock-screen frames — steps and sizes in `docs/animations.md`.
- **How many faces?** No fixed identity cap (15 templates each). With
  several enrolled people in frame at once, any one matching unlocks —
  and every identity under your account opens *your* account, so only
  enrol people you trust with it.
- **Photo or video held up to the camera?** No. Print dies on
  planarity/parallax + blink; screens add moiré + glare; video adds
  challenge-response at heavy; IR checks live skin; a learned ML
  verdict would join as one more vote only once a model proves
  itself on the target camera (none has yet — see the ML anti-spoof
  section) — and any single cue
  vetoes the unlock whatever the match. Honestly: resistance is
  unmeasured with real attack samples here, so run
  `eval/apcer_bpcer.py` with your own clips (it refuses small-n
  claims) before quoting numbers. Full detail: `docs/animations.md`.

## Recovery

The PAM line is `sufficient`, so the password always works. If you
get locked out: `Ctrl+Alt+F3` → log in → `sudo apt remove
faceid-nim`. Keep a root shell open while editing anything under
`/etc/pam.d/`.

## Known limits

- PAM's auth conversation is sequential, so the password box is
  usually unusable until the face attempt finishes — keep the timeout
  short.
- GNOME Keyring needs the real password; it can't be unlocked by a face.
- Screen recording/capture may crop the top-centre island in 16:9
  captures — capture full-screen when demoing the lock animation.
- Model licensing in `models/manifest.toml` is **unverified** — check
  each (notably InsightFace non-commercial restriction) before
  shipping.

## Credit

Glance (MIT) is a design reference; no code copied. Model sources and
licenses are listed in `models/manifest.toml` — verify each before
shipping.
