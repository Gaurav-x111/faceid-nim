# Architecture

```
 GTK4 app (Python) ----+
 GNOME pill (GJS) -----+---- D-Bus (system bus) ----> faceid-nimd (Rust, root)
 sudo / lock screen ---+                                   |     |
        |                                                  |     +--> template store (encrypted)
   pam_faceid.so (C) --- /run/faceid-nim/auth.sock --------+
                                                           |
                                    /run/faceid-nim/vision.sock (JSON lines)
                                                           v
                               vision worker (Python), user `faceid`, group `video`
                               camera -> detect -> quality -> align -> embed -> liveness
```

## Trust boundaries

| Boundary | Enforced by | Why |
|---|---|---|
| PAM → daemon | `SO_PEERCRED`, uid check, service allow-list | A caller may only request a face unlock for itself, unless it is root (gdm, sudo). |
| daemon → worker | separate uid, socket mode 0660 | A compromised worker cannot read templates: it never receives them. |
| daemon → UI | D-Bus signal carrying `(state, progress, reason)` | The extension is untrusted. It gets no frames, embeddings or scores. |
| app → daemon | polkit (**not yet wired — see HANDOFF.md**) | Enrolling must require the account password. |

## State machine

```
IDLE -> WAKING -> SEARCHING -> VERIFYING -> MATCHED
                     |             |------> REJECTED
                     |             '------> TIMEOUT
                     '--------------------> CAMERA_ERROR
```

`CAMERA_ERROR` and a dead worker both map to `PAM_AUTHINFO_UNAVAIL`, so
a broken camera falls through to the password instead of counting as a
failed attempt. `REJECTED` counts toward the lockout.

## The decision

In `daemon/src/session.rs`, liveness and recognition are computed
separately and combined only at the end:

```rust
let liveness_ok = /* deny cues, attention, confirm cues */;
let m = vote(&ev.embeddings, &templates, cfg.tau, cfg.vote_k, cfg.vote_n);
if m.accepted && liveness_ok { /* unlock */ }
```

The ordering matters less than the independence. `vote()` never sees a
liveness cue and the liveness fusion never sees a similarity score, so
neither can be talked into excusing the other.
