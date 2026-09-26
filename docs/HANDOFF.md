# Handoff: where this code stops

Written so you can pick up without re-reading everything.

## Open work: universal / zero-config setup

A full audit of the tech stack and a step-by-step plan for making the
`.deb` install-and-work on any Linux laptop with a camera — automatic
model provisioning, GPU-or-CPU selection, camera/IR auto-detection,
honest liveness reporting — is in **[UNIVERSAL-SETUP.md](UNIVERSAL-SETUP.md)**.

Nothing in it is implemented yet. The highest-severity finding it records
is that **MediaPipe Face Mesh and the MiniFASNet anti-spoof model do not
actually run in the shipped build**, so blink, planarity,
challenge-response and the attention check are silently inert. Fix that
first; it is a correctness bug, not a polish item.

## Verified in this session

- `vision/` — all modules compile; **15 unit tests pass** (`pytest -q vision/tests`).
- `eval/far_frr.py` — smoke-tested end to end on synthetic embeddings.
  It produced distributions, an EER, a tau, a per-impostor table and a
  sweep. The logic works; it just needs real data.
- `pam/pam_faceid.c` — **compiles clean** with `-Wall -Wextra -Werror`.
  One real bug was caught and fixed: the reply enum used `R_OK`, which
  collides with `unistd.h`'s `access()` mode constant. Renamed to `FID_*`.
- `shell-ext/faceid@nim/*.js` — all three files parse as ES modules.

## NOT verified — do this first

**The Rust daemon has never been compiled.** There was no `cargo` in the
environment. Expect API drift, especially in `dbus.rs`:

- `zbus` 4.x: `#[interface]`, `connection::Builder::system()`,
  `SignalContext`. If you are on zbus 5, the attribute is still
  `#[interface]` but `SignalContext` became `SignalEmitter` and
  `connection::Builder` moved. Check the version you resolve.
- `Daemon1::caller_uid` calls `DBusProxy::get_connection_unix_user`.
  Confirm the argument type — it wants a `BusName`, and the `.into()`
  there may need adjusting.
- `nix` 0.29 `getsockopt(&BorrowedFd, PeerCredentials)` in `ipc.rs` —
  the borrowed-fd signature changed around 0.27; if you pin an older
  nix, pass the raw fd instead.
- `ipc.rs` has a long inline `<std::fs::Permissions as ...>::from_mode`
  cast. Add `use std::os::unix::fs::PermissionsExt;` and simplify it.

So: `cd daemon && cargo build` is step one, and budget an hour for
compile errors in `dbus.rs`.

**Polkit is declared but not enforced.** `packaging/polkit/org.faceidnim.policy`
defines the three actions, and the D-Bus methods are documented as
polkit-gated, but `dbus.rs` does not actually call
`org.freedesktop.PolicyKit1.Authority.CheckAuthorization` yet. Until it
does, `Enroll`, `SetSettings` and `DeleteIdentity` are authenticated by
uid but **not** by password. That is the single biggest gap: right now
someone at your unlocked laptop could enroll their own face. Wire it
before M4 ships.

**Model checksums are placeholders.** `models/manifest.toml` has
`REPLACE_ME_WITH_REAL_SHA256` in three places. Download the files, hash
them, paste the real values. Until then everything must run with
`--no-verify`, which is exactly the state you do not want to ship in.

## Milestone status

| M | What | State |
|---|---|---|
| M0 | offline pipeline + FAR/FRR | **code complete**, needs real clips and real model files |
| M1 | daemon + worker + enroll | code complete, daemon **never compiled** |
| M2 | real PAM module | **compiles clean**, never loaded by PAM |
| M3 | D-Bus ScanState + pill | code complete, never run in a shell |
| M4 | GTK4 app | code complete, never run |
| M5 | liveness | deny cues (glare, bezel, moiré) + confirm cues (blink, parallax, challenge, IR) all implemented; MiniFASNet wrapper degrades to "no opinion" without a model |
| M6 | packaging | .deb scaffolding, installer, CI, release workflow written; **never built** |
| M7 | hardening | IR module and challenge mode exist; TPM sealing, logind mode and the C++ worker are not started |

## Suggested order from here

1. `cd daemon && cargo build` — fix compile errors, run `cargo test`
   (store, matcher and policy all have tests waiting).
2. Download the two OpenCV Zoo models, fill in real SHA-256 values.
3. `python -m faceid_vision --oneshot --source clip.mp4 --strict off` —
   confirm the pipeline produces embeddings from a video file.
4. Record 3–4 genuine clips and 3–4 impostor clips, run the eval, and
   **put the real tau into `/etc/faceid-nim/config.toml`.** The default
   0.40 is a placeholder with no evidence behind it.
5. Wire polkit in `dbus.rs`.
6. Only then touch `/etc/pam.d/`. Root shell open first, `pamtester`
   before the real stack.

## Things I deliberately did not do

- Did not commit model binaries. They get fetched at install time.
- Did not implement logind unlock mode (Glance mode). It bypasses PAM,
  so it does not cover sudo and leaves the keyring locked. Ship PAM first.
- Did not implement template adaptation. It helps with slow appearance
  changes but lets an attacker who gets one borderline accept poison the
  template set. If you add it, make it opt-in and capped.
- Did not write the C++ worker. The Python one holds the same socket
  protocol, so it can drop in later without touching anything else.
