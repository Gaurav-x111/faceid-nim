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
  anti-spoof: no ML model (screen/print/IR cues still active)
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
"Use password to continue" (hover the island to retry).

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

Customization lives in the app → **Opening** page, stored per user in
`~/.config/faceid-nim/openings.json`:

```json
{
  "active": "logo",        // which opening animation plays at scan start
  "variants": {},          // user specs indexed by id
  "scan_face": "apple"     // scanner style: apple | arena | classic
}
```

**Scanner style** (`Opening → Scanner`): shown as **Smile
(default)** = thin track + progress arc + smiling face,
**Orbit** = halo/crest/comet scanner, **Classic** = original ring.

**The opening** (`Opening → Active animation`): built-ins `logo`
(default), `glow`, `none` — or **Create an opening animation**: pick
a headline, colours, easing, duration, optional logo → **Publish to
GitHub** exports a `.faceopen` package with ready `gh` commands.
Others install via **Install from file** / **Install from URL**, or
the **Browse community** catalogue. Your choice applies at the very
next lock — no extension reload, no restart.

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
enrolling, deleting data, or enabling PAM requires polkit; opening a
preview or running Test Scan does not write anything and therefore does
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
