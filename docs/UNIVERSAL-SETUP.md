# Universal / zero-config setup — findings and plan

Status: **IMPLEMENTED, 2026-09-26**, except the two items called out in
Part 7 below. This document was written as a plan after a full read of
the codebase and is kept as the record of *why* each change was made --
read Part 2 before changing any of it, because the failure it describes
is invisible from the code alone.

What landed: `hardware.py`, `rt.py`, `cameras.py`, the landmarks backend
registry, `optional` in the manifest, real model provisioning in postinst
plus `faceid-nim-fetch-models.service`, `models_ready` in Diagnostics,
the conditional GPU drop-in, the live `status`/app capability reporting,
and the arm64-tolerant venv build. See the 1.1.1-1 changelog entry.

Still open: first-run automatic tau calibration (Part 7), and the
MiniFASNet licence decision (Part 3, Step 3).

## Goal

Any Linux laptop with a camera installs the `.deb` and works. No
commands typed. Hardware is probed and configured at install time:

- GPU present and working → use it. Otherwise → CPU. Either way it runs.
- Camera auto-discovered, IR sensor auto-detected if it exists.
- Models provisioned automatically, verified, and re-provisioned on upgrade.
- Liveness features that *can* run on this machine are enabled; ones that
  cannot are reported honestly in the app rather than silently inert.

---

## Part 1 — What is already in place

Not everything needs building. These already exist:

| Asset | Path | Reuse for |
|---|---|---|
| Camera discovery incl. combo/IR sensors | `app/faceid_app/camera_discovery.py` (491 L) | headless provisioning, not just the app |
| Threshold auto-tuner | `app/faceid_app/auto_tune.py` (109 L) | first-run `tau` calibration |
| Hash-pinned model fetcher | `packaging/scripts/fetch-models` | run from postinst instead of by hand |
| Model verifier | `packaging/scripts/verify-models` | install gate |
| `capabilities` IPC op | `vision/faceid_vision/__main__.py:158-167` | already reports `mesh`, `antispoof`, `model_id`, `embedding_dim`, `ir` |
| Device sandbox | `packaging/systemd/faceid-vision.service` | must be *widened* for GPU, see 2.3 |
| Degradation doctrine | `vision/faceid_vision/fuse.py`, `liveness/__init__.py` | already correct; just make it visible |

---

## Part 2 — The blockers, in order of severity

### 2.1 Two of the four models never run. This is the real problem.

Verified by inspecting the shipped venv, not inferred.

**MediaPipe Face Mesh is dead.**
- Installed wheel is `mediapipe-0.10.35`.
- `grep -c solutions .../mediapipe-0.10.35.dist-info/RECORD` → **0**.
- `site-packages/mediapipe/` contains only `__init__.py`, `modules/`, `tasks/`.
- `mediapipe/modules/` contains only `hand_landmark/` and `objectron/`.
- No bundled `.tflite` anywhere.
- So `mp.solutions.face_mesh` → `AttributeError`.

The bare `except Exception` at `vision/faceid_vision/landmarks.py:45-46` swallows
it, `self.available = False`, and `scan.py:182`
(`dense = self.mesh(frame) if self.mesh.available else None`) short-circuits.

Consequence: **blink, homography planarity, challenge-response and the
attention check are all inert no-ops in the shipped build.** So
`strictness = "heavy"` can never be satisfied, and `require_attention`
is unenforceable.

**MiniFASNet is dead.**
- `models/manifest.toml:44-45` — `url = ""`, `sha256 = ""`.
- No `minifasnet_v2.onnx` on disk.
- `PassiveAntiSpoof.available` is `False` (`liveness/passive.py:35-36`), so the
  `antispoof_model` deny cue never fires.

**The good news:** the installed wheel *does* ship the modern Tasks API:
```
mediapipe/tasks/python/vision/face_landmarker.py
mediapipe/tasks/python/components/
```
That is the supported path going forward and it is what the fix should use.

### 2.2 Nothing provisions models automatically

`packaging/debian/postinst` only *checks*:

```sh
if ! "$LIBEXEC/verify-models" --quiet; then
    echo "faceid-nim: face recognition models are missing or unverified."
    echo "            Run:  sudo faceid-nim fetch-models"
```

So the documented first-run is `sudo faceid-nim fetch-models` +
`sudo faceid-nim status` + open the app. Three manual steps, and README §Install
lists them. That is the "user has to worry" part of the complaint.

Upgrade path has the same hole: a new version that adds a model to the
manifest leaves existing installs broken until the user reads the release note.

### 2.3 GPU is impossible today, for two independent reasons

**a. No provider is ever selected.** Both ORT sessions hard-code CPU:
- `vision/faceid_vision/embed.py:30` — `providers or ["CPUExecutionProvider"]`
- `vision/faceid_vision/liveness/passive.py:32` — `providers=["CPUExecutionProvider"]`

Grep for `CUDA|TensorRT` across the repo → **zero hits**. The shipped
`onnxruntime` has no `libonnxruntime_providers_cuda.so`. `Embedder.providers`
is an accepted kwarg but `build_engine` never passes it, so it is unreachable
in production. Threads are hard-coded at `intra_op_num_threads = 2`.

**b. systemd would block the GPU even if it were selected.**
`packaging/systemd/faceid-vision.service:34-35`:
```ini
DevicePolicy=closed
DeviceAllow=char-video4linux rw
```
`DevicePolicy=closed` gives the worker an empty device whitelist, and only
`/dev/video*` is added. **No `/dev/nvidia*`, no `/dev/dri`, no `/dev/kfd`.**
A CUDA or ROCm session would die at provider init. The daemon
(`faceid-nimd.service`) has no `DevicePolicy` at all, but it runs no ML.

### 2.4 Threading and CPU features are hard-coded

`embed.py:26` sets 2 threads for every machine. A 4-core ARM laptop and a
16-thread desktop get identical settings. No AVX2 / AVX512 / NEON probe
anywhere. `passive.py` sets no `sess_options` at all, so it gets ORT's
full default pool — an asymmetry with the embedder.

### 2.5 Misleading `status`

`faceid-nim status` prints service state and model presence. It says nothing
about whether liveness is actually operational, which EP is in use, or how
many cores were detected. So a user on a machine where blink/planarity are
dead sees a healthy-looking `status`.

### 2.6 Stale docs (cheap, do alongside)

- `docs/architecture.md:23` — claims polkit is "**not yet wired — see
  HANDOFF.md**". It is fully wired (`daemon/src/authz.rs`, `enroll.rs:435`,
  `dbus.rs:97,114,127,143,382,408`).
- `docs/architecture.md:10` — worker socket given as
  `/run/faceid-nim/vision.sock`; real path is
  `/run/faceid-nim/worker/vision.sock` (`config.rs:129`,
  `faceid-vision.service:15`).
- `matcher.rs:14-19` says `best` is "kept for the audit log" — it is not;
  it only reaches a `tracing::debug!` at `session.rs:311`.
- `liveness/ir.py:57` `emitter_difference()` is dead code, no caller, no test.
- `enroll.rs:20-36` describes the preview framing without the leading
  `'J'`/`'G'` tag byte that `dbus.rs:471-475` documents.

---

## Part 3 — The plan

### Step 1 — `vision/faceid_vision/hardware.py` (new)

One probe, used by the worker, the app, `status` and the tests.

```python
@dataclass(frozen=True)
class HardwareProfile:
    arch: str                  # x86_64 | aarch64 | ...
    cpu_count: int
    cpu_features: tuple[str, ...]   # avx2, avx512f, neon, f16c, sse4_2
    containerized: bool
    accel: str                 # "cuda" | "tensorrt" | "openvino" | "rocm" | "coreml" | "dml" | "cpu"
    accel_detail: str          # e.g. "CUDAExecutionProvider (sm_89), 2 EPs, probe OK"
    providers: list[str]       # ordered, what ORT will actually get
    intra_op_threads: int
    inter_op_threads: int
    notes: list[str]           # human-readable caveats for the UI
```

- Read CPU features from `/proc/cpuinfo` flags; detect container from
  `/.dockerenv`, `/run/.containerenv`, or cgroup `memory.max`/`cpu.max`.
  A container with 168 host cores and 2 cpuset members must not get 168.
- **Probe, do not trust `get_available_providers()`.** A provider can be
  listed and still fail at `InferenceSession` time (missing `libcuda.so`,
  version skew, no `nvidia-uvm`). Actually construct a tiny session and run
  one `Identity`-only model per candidate, in preference order
  `TensorRT > CUDA > ROCm > OpenVINO > CoreML > DirectML > CPU`, and keep
  the first that loads. Cache the result in `/var/lib/faceid-nim/hardware.json`
  so the ~100 ms probe runs once, not per scan.
- Thread sizing: `min(cpu_count, 4)` for `intra_op` on the first attempt
  (latency-bound, as the existing comment says) — then let
  `FACEID_INTRA_OP_THREADS` override.
- Never fail. Any exception in the probe → CPU with a `note`. A probe bug
  must not prevent face unlock.

Apply in `embed.py:23-30` and `passive.py:29-36` via one shared session
factory, and give `passive.py` the same `sess_options` the embedder has
(the current asymmetry).

### Step 2 — Make the landmark backend real and pluggable

Rewrite `vision/faceid_vision/landmarks.py` as a backend registry with
ordered candidates and a *specific* reason when each fails.

1. **MediaPipe Tasks API** — `mediapipe.tasks.python.vision.FaceLandmarker`
   with a downloaded `face_landmarker.task` bundle, `output_face_blendshapes=False`,
   `num_faces=1`, `running_mode=VIDEO`. This is the path 0.10.35 supports.
2. **MediaPipe legacy `solutions`** — kept for older wheels, wrapped in its
   own `try/except`.
3. **Unavailable** — report the reason string, not a bare `False`.

The Tasks API returns the same canonical MediaPipe index ordering
(1 = nose tip, 10 = forehead, 33/263 = eye corners, 152 = chin, plus the
Soukupová 6-point eye rings), so `blink.py`, `geometry.py` and
`challenge.py` need **no changes**. That is the reason to prefer Tasks over
inventing a new landmark source — the downstream index constants stay valid.

Add to `models/manifest.toml`:
```toml
[model.landmarks]
file = "face_landmarker.task"
url  = "<google storage url>"
sha256 = "<pin after first download>"
license = "Apache-2.0"
optional = true
```
New manifest key: `optional = true`, meaning "absent ⇒ log loudly, degrade,
keep going". `detector` and `recognizer` stay hard-required.

Also add a cheap **synthetic-landmark fallback** so the worker's
`capabilities` reply and the app's UI can distinguish "not installed" from
"this machine cannot run it".

### Step 3 — Make the antispoof model real, or stop pretending

Options, in preference order:
- (a) Find a stably-hosted, permissively-licensed MiniFASNet/Silent-Face-Anti-Spoofing
  ONNX export, pin its hash, add `optional = true`.
- (b) Keep it absent and **remove `antispoof` from every user-facing surface**,
  so the app stops implying a check that is not running.

Either way: today the manifest advertises a model that cannot be fetched
and the deny cue silently never fires. One of the two must change.

### Step 4 — Auto-provision at install

**postinst** (`packaging/debian/postinst`):
- Replace the "run `sudo faceid-nim fetch-models`" echo with an actual
  fetch, run synchronously but with a short timeout, then
  `systemctl enable --now` **after** the models land.
- Post-install message becomes the real state, not an instruction:
  ```
  faceid-nim installed. Face unlock is DISABLED.
    models : ready (yunet, sface, face_landmarker)
    liveness: reduced — no ML anti-spoof model on this install
    device  : CPU only, 8 threads
    camera  : /dev/video0 (RGB); no IR sensor found
  Next: open "Face Unlock" and follow the wizard.
  ```
- A fetch failure must be **non-fatal** (`set -e` has to be handled) and must
  leave a re-attempt path.

**New unit** `faceid-nim-fetch-models.service`:
- `Type=oneshot`, `After=network-online.target`, `WantedBy=multi-user.target`
  so it retries on machines that were offline at install time and on every boot.
- `ExecCondition` skips when `verify-models` already passes.
- Never required by `faceid-nimd.service` — a missing model must not block boot.

**Daemon first-start safety net** — a root daemon can do this trivially and it
covers `make install`, upgrades, and offline installs:
- On `Store::open`, if `verify` fails, log at WARN and set a `models_ready: bool`.
- Add `ModelsReady` to the existing `Diagnostics` JSON (`dbus.rs:423-438`) —
  no new D-Bus method, no new polkit action, no interface change.
- The app already calls `Diagnostics`; surface it in the wizard.

Keep `faceid-nim fetch-models` working. Auto ≠ only.

### Step 5 — Camera auto-detection headless

`camera_discovery.py` lives in the app and is not importable from the
worker. Move the discovery core to `vision/faceid_vision/cameras.py` and
re-export from the app so there is one implementation.

- Auto-pick primary: prefer a real colour sensor (a GREY-only node is IR), fall
  back to the largest/first working node.
- Auto-detect IR: the existing rule (`Integrated_Webcam_FHD` → `/dev/video3 ·
  640x360 GREY`) is already right; just move it.
- On first run with no `camera =` set, write the discovered node into
  `/etc/faceid-nim/config.toml` via the existing `Config::save`, behind the
  existing `org.faceidnim.settings` polkit action. **No new privileged path.**
- Log the decision: `info!("auto-selected /dev/video0 (colour, 640x480); IR: /dev/video3"`).

### Step 6 — Widen the sandbox for GPU, conditionally

`faceid-vision.service` must gain device access, but only when there is
something to reach. A drop-in, generated at install by the provisioner:

```ini
# /run/faceid-nim/gpu-devices.conf  (generated, only when accel != cpu)
[Service]
DeviceAllow=char-video4linux rw
DeviceAllow=char-nvidia-tty rw
DeviceAllow=char-nvidia-uvm rw
DeviceAllow=char-dri rw
```
`SystemCallFilter=@system-service` must also be widened to `@system-service
@aio @signal @chown @setuid @setgid @resource` or the CUDA driver ioctls get
`EPERM` under `SystemCallErrorNumber=EPERM`.

- Do **not** add GPU devices when no accelerator is present. Widening a
  sandbox for hardware that is not there is pure attack surface.
- `PrivateNetwork=yes` is fine for GPU (no host network needed) — keep it.
- `CapabilityBoundingSet=` stays **empty**.

### Step 7 — Surface the truth in the UI and CLI

- `faceid-nim status` gains a **Capabilities** block from the cached
  `hardware.json`: EP in use, cores, threads, AVX/NEON, per-backend landmark
  status, antispoof present/absent. Fixes 2.5.
- `faceid-nim doctor` (or extend `diagnose.sh`) prints a plain-language verdict:
  "Your machine will use the CPU. Liveness is reduced: blink and 3D checks
  are unavailable because the landmark model was not installed. Screen and
  print spoof detection still works."
- The app's Camera/Settings page reads the same profile and shows the same
  string. `capabilities` already returns `mesh` and `antispoof`; add
  `landmarks: "mediapipe_tasks" | "mediapipe_solutions" | "none"` and
  `accel`, `threads`.
- **Never** let the UI imply a check that is not running. This is the same
  honesty rule the README already follows ("this is a convenience feature,
  not Face ID") and the same rule `passive.py:1-9` already applies to itself.

### Step 8 — First-run auto-calibration

`app/faceid_app/auto_tune.py` already exists. Wire it into the wizard:
after enrollment, run a few capture passes and set `tau` from the measured
genuine/impostor separation instead of shipping a hard-coded `0.40`. Write it
via the existing polkit-gated `SetSettings`. Report the measured EER, not
"99% accurate" — `eval/far_frr.py` already degrades honestly on small samples
and that behaviour should be preserved in the wizard.

---

## Part 4 — File-by-file change list

```
NEW  vision/faceid_vision/hardware.py            probe + EP selection + thread sizing
NEW  vision/faceid_vision/cameras.py             camera/IR discovery, moved out of the app
NEW  vision/faceid_vision/rt.py                  shared ORT InferenceSession factory
NEW  packaging/systemd/faceid-nim-fetch-models.service
MOD  vision/faceid_vision/embed.py:23-30         use rt.py + hardware profile
MOD  vision/faceid_vision/liveness/passive.py:29-36  same, and get sess_options
MOD  vision/faceid_vision/landmarks.py           backend registry: Tasks API first
MOD  vision/faceid_vision/__main__.py:158-167    richer capabilities event
MOD  vision/faceid_vision/scan.py:182            land on a named backend
MOD  models/manifest.toml:38-48                  pin landmarks; `optional` flag
MOD  packaging/debian/postinst                   fetch + report, non-fatal
MOD  packaging/systemd/faceid-vision.service     conditional GPU drop-in
MOD  packaging/scripts/faceid-nim                status capabilities block
MOD  packaging/scripts/fetch-models              retry/backoff + offline tolerance
MOD  app/faceid_app/camera_discovery.py          re-export from faceid_vision.cameras
MOD  app/faceid_app/auto_tune.py                 wire into wizard
MOD  daemon/src/dbus.rs:423-438                  add models_ready to Diagnostics
MOD  docs/architecture.md:10,23                  fix stale socket path + polkit claim
MOD  README.md                                   install steps become "install, done"
```

## Part 5 — Test plan

- `vision/tests/test_hardware.py` — provider preference order, probe failure
  → CPU fallback, container cpuset clamping, thread sizing, never-raises.
- `vision/tests/test_landmarks_backends.py` — each backend selected when
  present, specific reason when absent, and the index constants that
  `blink.py`/`geometry.py` depend on are identical across backends.
- `vision/tests/test_cameras.py` — combo sensor picks the colour node as
  primary and the GREY node as IR.
- Post-install smoke test on a clean 24.04 VM: install, **type nothing**,
  `faceid-nim status` shows models ready, wizard enrolls, lock unlocks,
  `sudo -k && sudo true` works.
- Re-run the audit that found 2.1: assert `mesh`/landmark backend is non-`none`
  in the shipped venv, so this class of silent failure cannot return.

## Part 6 — Non-goals

- Do not change the trust boundary. The worker stays unprivileged and
  template-blind. Do not move inference into the daemon.
- Do not change the matching math or the strictness truth table while doing
  this; a behavioural change to `permit()`/`vote()` needs its own review.
- Do not widen the daemon's sandbox.
- Do not make a failed model fetch a hard failure. Face unlock must degrade,
  never lock the user out.

## Part 7 — What is still open

### 7.1 First-run automatic tau calibration (Step 8, not done)
`auto_tune.py` samples room light and suggests hardware defaults, and can
refine tau from a measured `.npz` (`--npz` / `--enroll-label`). It is
**not** wired into the enrollment wizard, so a fresh install still ships
`tau = 0.40` with no measurement behind it. Doing it properly means:
after enrollment, capture a few passes, compute the genuine/impostor
separation, and write tau through the existing polkit-gated
`SetSettings`. Report the measured EER, never "99% accurate" --
`eval/far_frr.py` already degrades honestly on small samples and that
behaviour must be preserved.

### 7.2 MiniFASNet licence (Step 3, still undecided)
No stably-hosted permissively-licensed ONNX export is known, so the
manifest entry is unpinned and `resolve_optional()` refuses it. The
`antispoof_model` deny cue therefore never fires. This is now reported
honestly in `faceid-nim status`, in Diagnostics and in the app -- no
surface claims the check is running. Either pin a licensed export or
leave it reported as unavailable; the second is the current, working
state.
