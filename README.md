# faceid-nim

Face unlock for Ubuntu 24.04+ / GNOME, with a lock-screen scan pill.

> **This is a convenience feature, not Face ID.**
> On an ordinary RGB webcam it can be defeated by a decent video replay.
> With an infrared camera it is substantially stronger. Twins and
> siblings raise false accepts. Your password always works.
>
> **Status: pre-alpha.** The code here compiles and is unit-tested, but
> it has not been run end-to-end on real hardware. Every threshold in
> the config is a starting point to measure, not a promise.

## What it is

| Component | Language | Job |
|---|---|---|
| `pam_faceid.so` | C (~300 lines) | Asks the daemon, returns a PAM code. No AI, no camera, no ML libraries. |
| `faceid-nimd` | Rust | Root daemon: policy, template storage, matching, D-Bus. |
| vision worker | Python | Unprivileged: owns the camera, produces embeddings + liveness cues. |
| `faceid@nim` | GJS | GNOME Shell pill on the lock screen. |
| Face Unlock app | Python + GTK4 | Enrollment, identities, settings, diagnostics. |

Four rules hold the design together:

1. **The PAM module does no AI.** If the ML stack crashes, logins are unaffected.
2. **The worker never sees templates.** It returns evidence; the daemon decides.
3. **The UI never sees frames or embeddings.** Only a state name and a number.
4. **Password always works.** The PAM profile uses `sufficient`, never `required`.

## Install

Not yet released. When it is:

```sh
sudo apt install ./faceid-nim_0.1.0_amd64.deb
```

Face unlock is installed **disabled**. Open the Face Unlock app, enroll,
then turn it on.

## Build from source

```sh
make            # daemon + PAM module
make test       # cargo test + pytest
sudo make install
```

## Measure your own thresholds

The single most important thing you can do before trusting this:

```sh
mkdir -p eval/data/genuine/me eval/data/impostor/someone-else
# record short clips into those directories, then:
python eval/collect.py  --root eval/data --out eval/out/embeddings.npz
python eval/far_frr.py  --npz eval/out/embeddings.npz --enroll-label me \
                        --target-far 1e-4
python eval/apcer_bpcer.py --root eval/data --strict heavy
```

`far_frr.py` prints genuine and impostor distributions, an EER, the
threshold for your target FAR and the FRR it costs — and it tells you
when you have too few impostor samples to measure the tail you asked
for, instead of quoting a number it cannot support. Put the results in
this README. Publish measured FAR/FRR, never "99% accurate".

## Recovery

The PAM line is `sufficient`, so your password still works if face
unlock breaks. If something goes badly wrong:

```
Ctrl+Alt+F3         # text console
# log in with your password
sudo apt remove faceid-nim
```

**While developing, always keep a root shell open before editing
anything under `/etc/pam.d/`.** A broken PAM stack can lock you out of
your own machine.

## Known limits

- Lock screen only. GDM (the login screen) runs a separate shell
  instance and is out of scope for v1.
- PAM's auth conversation is sequential, so the password box is usually
  unusable until the face attempt finishes. Keep the timeout short.
- GNOME Keyring cannot be unlocked by a face; it needs the real password.
- Model licenses in `models/manifest.toml` are **unverified**. Check each
  one before shipping a public package. InsightFace's pretrained models
  are, as far as I know, non-commercial research use only.

## Credit

Glance (MIT) is a design reference; no code is copied. Model authors are
credited in `docs/credits.md`.
