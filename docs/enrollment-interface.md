# org.faceidnim.Enrollment1 — interface members

Object path `/org/faceidnim/Enrollment1`, system bus, owned by
`org.faceidnim.Daemon1`.

Bind polkit to the three marked members. `GetSessionInfo`, `ListPoses`
and `CancelEnrollment` are deliberately ungated: reading where a
session is, or abandoning it, must never be harder than starting it.

| Member | Signature | Polkit action | Used by the app for |
|---|---|---|---|
| `ListPoses` | `() → (as)` | — | Pose prompts, so app and daemon cannot drift |
| `StartEnrollment` | `(s identity) → (s session, h preview_fd)` | **`org.faceidnim.enroll`** | Opens the session, returns the preview pipe |
| `EnrollPose` | `(s session, u pose_index) → (s status, d progress)` | — (session is uid-scoped) | One camera scan per pose |
| `FinishEnrollment` | `(s session) → (u templates_saved)` | **`org.faceidnim.enroll`** | Persists the templates |
| `CancelEnrollment` | `(s session) → ()` | — | Abort; closes the pipe, discards everything |
| `GetSessionInfo` | `(s session) → (u pose_index, u total_poses, s pose_name, u captured)` | — | Resync after a UI reconnect |
| `EnrollProgress` *(signal)* | `(s session, u pose_index, s pose_name, s status, d progress)` | — | Live status and progress bar |

`status` is a closed set: `acquiring`, `good`, `pose_complete`,
`failed`, `cancelled`, `finished`. Defined once in
`daemon/src/enroll.rs` and mirrored in `vision/faceid_vision/enrollpreview.py`
and `app/faceid_app/onboarding.py`.

## Preview transport

`StartEnrollment` returns a unix fd — the read end of a private pipe —
rather than emitting frames as signals. System-bus signals are
broadcast, so a signal-based preview would be readable by anything
allowed to receive from the daemon, including the lock-screen
extension, which must never see a frame. Handing the fd back as the
return value of the caller's own method call scopes it to exactly one
client.

Framing: 4-byte big-endian length, then that many JPEG bytes
(~320px, quality 70, under 20 KB). The daemon drops the write end on
cancel, finish, or a uid mismatch, so the app's reader sees a clean EOF.

## Where polkit goes in

`daemon/src/enroll.rs` has one seam:

```rust
async fn require_polkit(conn, hdr, action) -> zbus::fdo::Result<()>
```

It currently returns `Ok(())` with a TODO and is called at the top of
`StartEnrollment` and `FinishEnrollment`. Replace the body with
`CheckAuthorization`; nothing else in the file changes.

**Until that lands, enrollment is authenticated by uid but not by
password** — someone at an unlocked laptop could add their own face.
