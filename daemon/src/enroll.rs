//! org.faceidnim.Enrollment1 -- guided, previewable enrollment.
//!
//! Registered separately from Daemon1 so the preview path is easy to
//! audit in isolation. It mirrors dbus.rs's zbus-4 style:
//! `#[interface(name = ...)]`, `#[zbus(header)] hdr`, and a
//! `caller_uid` lookup against the bus rather than anything the caller
//! sends.
//!
//! POLKIT IS WIRED AT THE ONE PLACE THAT WRITES. Capturing poses and
//! streaming a preview writes nothing to disk: poses collect embeddings
//! in an in-process, uid-scoped session, so a user enrolling their own
//! face must be able to watch the scanner and be captured WITHOUT any
//! password prompt -- a camera failure must never look like an auth
//! failure. Only `finish_enrollment`, which persists templates into the
//! root-owned store, calls `require_polkit` behind the
//! "org.faceidnim.enroll" action. It fails closed: if polkitd is down
//! or says no, nothing is saved and the app shows a clear "authentication
//! service unavailable" card instead of a cryptic daemon error.
//!
//! Why the preview is a pipe and not a signal
//! ------------------------------------------
//! The design doc allows enrollment preview frames to reach the
//! enrolling app, and only that app. A D-Bus signal cannot express
//! that: signals on the system bus are broadcast, so every frame would
//! be readable by anything allowed to receive from us -- including the
//! lock-screen extension, which must never see a frame. Base64 JPEGs on
//! the bus would also balloon message size and sit in the dbus-daemon's
//! buffers.
//!
//! So `StartEnrollment` returns a unix fd: the read end of a private
//! pipe, handed to exactly one caller as the return value of their own
//! method call. Nothing else on the bus can obtain it. Framing is a
//! 4-byte big-endian length followed by that many JPEG bytes. The
//! daemon drops the write end on cancel, finish, or caller mismatch,
//! so the app's reader sees a clean EOF. Frames are never written to
//! disk and never logged.

use crate::config::Config;
use crate::store::{Identity, Store};
use crate::worker::{Progress, Worker};
use std::collections::HashMap;
use std::io::Write;
use std::os::fd::{AsRawFd, FromRawFd, OwnedFd};
use std::sync::Arc;
use tokio::sync::{mpsc, Mutex};
use zbus::{interface, object_server::SignalContext, proxy, zvariant, Connection};

/// The nine poses, in the order the app walks the user through them.
pub const POSES: [&str; 9] = [
    "Look straight ahead",
    "Turn your head left",
    "Turn your head right",
    "Look up",
    "Look down",
    "Up and to the left",
    "Up and to the right",
    "Down and to the left",
    "Down and to the right",
];

/// Status values the app renders. Kept as a closed set so the UI can
/// switch on them without string guessing.
pub const ST_ACQUIRING: &str = "acquiring";
pub const ST_GOOD: &str = "good";
pub const ST_POSE_COMPLETE: &str = "pose_complete";
pub const ST_FAILED: &str = "failed";
pub const ST_CANCELLED: &str = "cancelled";
pub const ST_FINISHED: &str = "finished";

/// Near-duplicate cutoff. A template this close to one we already have
/// adds no new coverage, only attack surface.
const DUP_SIMILARITY: f32 = 0.92;
const MAX_TEMPLATES: usize = 15;

struct Session {
    uid: u32,
    identity: String,
    /// Write end of the preview pipe. Dropped to close the channel.
    preview: Option<std::fs::File>,
    embeddings: Vec<Vec<f32>>,
    model_id: String,
    pose_index: u32,
    cancelled: bool,
}

impl Session {
    /// Best-effort frame delivery. A blocked or closed pipe must never
    /// stall or fail enrollment, so a write error just closes the
    /// channel and enrollment carries on without a preview.
    fn push_preview(&mut self, jpeg: &[u8]) {
        let Some(file) = self.preview.as_mut() else {
            return;
        };
        if jpeg.len() > (u32::MAX as usize) - 1 {
            return;
        }
        if file.write_all(b"J").is_err()
            || file.write_all(&(jpeg.len() as u32).to_be_bytes()).is_err()
            || file.write_all(jpeg).is_err()
        {
            // Reader went away, or the pipe filled. Drop it.
            self.preview = None;
        }
    }

    /// Push a pose_status line down the same pipe. Framed with a 'G'
    /// tag so the app can distinguish guidance from frames without a
    /// second channel or any cross-process framing.
    fn push_guidance(&mut self, status: &str, reason: &str) {
        let Some(file) = self.preview.as_mut() else {
            return;
        };
        let line = format!("{status}|{reason}");
        let bytes = line.as_bytes();
        if bytes.len() > (u32::MAX as usize) - 1 {
            return;
        }
        if file.write_all(b"G").is_err()
            || file.write_all(&(bytes.len() as u32).to_be_bytes()).is_err()
            || file.write_all(bytes).is_err()
        {
            self.preview = None;
        }
    }

    fn close_preview(&mut self) {
        self.preview = None;
    }
}

pub struct Enrollment1 {
    pub cfg: Arc<Mutex<Config>>,
    pub store: Arc<Store>,
    pub worker: Arc<Worker>,
    /// Used to ask the bus who a sender really is. Never trust a uid
    /// supplied in method arguments.
    pub conn: Connection,
    sessions: Arc<Mutex<HashMap<String, Session>>>,
    counter: Mutex<u64>,
}

impl Enrollment1 {
    pub fn new(
        cfg: Arc<Mutex<Config>>,
        store: Arc<Store>,
        worker: Arc<Worker>,
        conn: Connection,
    ) -> Self {
        Self {
            cfg,
            store,
            worker,
            conn,
            sessions: Arc::new(Mutex::new(HashMap::new())),
            counter: Mutex::new(0),
        }
    }

    async fn caller_uid(&self, hdr: &zbus::message::Header<'_>) -> zbus::fdo::Result<u32> {
        let sender = hdr
            .sender()
            .ok_or_else(|| zbus::fdo::Error::AccessDenied("no sender".into()))?;
        let proxy = zbus::fdo::DBusProxy::new(&self.conn)
            .await
            .map_err(|e| zbus::fdo::Error::Failed(e.to_string()))?;
        proxy.get_connection_unix_user(sender.clone().into()).await
    }

    /// Every session lookup goes through here, so there is exactly one
    /// place where "is this caller allowed to touch this session"
    /// is decided. A uid mismatch closes the preview channel rather
    /// than merely refusing the call.
    async fn with_session<T>(
        &self,
        session: &str,
        uid: u32,
        f: impl FnOnce(&mut Session) -> zbus::fdo::Result<T>,
    ) -> zbus::fdo::Result<T> {
        let mut map = self.sessions.lock().await;
        let s = map
            .get_mut(session)
            .ok_or_else(|| zbus::fdo::Error::Failed("no such enrollment session".into()))?;
        if s.uid != uid {
            s.close_preview();
            return Err(zbus::fdo::Error::AccessDenied(
                "that enrollment session belongs to another user".into(),
            ));
        }
        f(s)
    }

    async fn next_session_id(&self, uid: u32) -> String {
        let mut c = self.counter.lock().await;
        *c += 1;
        format!("e{uid}-{c}")
    }
}

/// CheckAuthorization via org.freedesktop.PolicyKit1.Authority.
///
/// D-Bus uses a tagged struct for a PolicyKit subject: `(sa{sv})`.
///
/// It is tempting to send just the `a{sv}` properties map here, but that
/// produces `(a{sv}sa{ss}us)`, while PolicyKit expects
/// `((sa{sv})sa{ss}us)`.  In particular, the `"unix-user"` property is not
/// enough on its own -- the `"unix-user"` *subject kind* is the first member
/// of the struct.
type PolkitSubject = (String, HashMap<String, zvariant::OwnedValue>);
type AuthzResult = zbus::Result<(bool, bool, HashMap<String, String>)>;

/// Fails closed: if polkitd is unreachable or answers "no", the method
/// is denied. Enrollment must never succeed without a password re-auth.
#[proxy(
    interface = "org.freedesktop.PolicyKit1.Authority",
    default_service = "org.freedesktop.PolicyKit1",
    default_path = "/org/freedesktop/PolicyKit1/Authority"
)]
trait Authority {
    fn check_authorization(
        &self,
        subject: &PolkitSubject,
        action_id: &str,
        details: &HashMap<String, String>,
        flags: u32,
        cancellation_id: &str,
    ) -> AuthzResult;
}

async fn require_polkit(
    conn: &Connection,
    hdr: &zbus::message::Header<'_>,
    action: &str,
) -> zbus::fdo::Result<()> {
    // Use the caller's system bus name as the subject. This lets polkitd
    // resolve the calling process itself via the bus, avoiding the
    // fragile PID/start-time lookup that a unix-process subject would need.
    let sender = hdr
        .sender()
        .ok_or_else(|| zbus::fdo::Error::AccessDenied("no sender".into()))?;

    let mut subject_properties: HashMap<String, zvariant::OwnedValue> = HashMap::new();
    subject_properties.insert(
        "name".into(),
        zvariant::Value::from(sender.as_str()).try_into().unwrap(),
    );
    let subject = ("system-bus-name".to_string(), subject_properties);

    eprintln!(
        "[polkit] action_id={:?} subject=system-bus-name:{}",
        action,
        sender.as_str()
    );

    let authority = AuthorityProxy::new(conn)
        .await
        .map_err(|e| zbus::fdo::Error::Failed(format!("polkit: {e}")))?;
    // flags 1 = POLKIT_CHECK_AUTHORIZATION_FLAGS_ALLOW_USER_INTERACTION,
    // so the auth agent is allowed to pop up a password prompt.
    let result = authority
        .check_authorization(&subject, action, &HashMap::new(), 1, "")
        .await;

    match &result {
        Ok((authorized, challenge, details)) => {
            eprintln!(
                "[polkit] authorized={} challenge={:?} details={:?}",
                authorized, challenge, details
            );
        }
        Err(e) => {
            eprintln!("[polkit] ERROR: {:?}", e);
        }
    }

    let (authorized, _, _) =
        result.map_err(|e| zbus::fdo::Error::Failed(format!("polkit: {e}")))?;

    if authorized {
        Ok(())
    } else {
        Err(zbus::fdo::Error::AccessDenied(
            "not authorized to perform this action".into(),
        ))
    }
}

#[interface(name = "org.faceidnim.Enrollment1")]
impl Enrollment1 {
    /// The pose prompts, in order. Returned rather than hardcoded in
    /// the app so both sides cannot drift.
    async fn list_poses(&self) -> Vec<String> {
        POSES.iter().map(|s| (*s).to_string()).collect()
    }

    /// Open a session and hand back the read end of a private preview
    /// pipe. Returns (session_id, preview_fd).
    async fn start_enrollment(
        &self,
        identity: String,
        #[zbus(header)] hdr: zbus::message::Header<'_>,
    ) -> zbus::fdo::Result<(String, zvariant::OwnedFd)> {
        // Deliberately NO polkit here: opening a camera and showing a
        // preview is unprivileged. Requiring a password just to let a
        // user see the scanner they are about to use is what broke
        // "face scan cannot start" when no auth agent was reachable.
        let uid = self.caller_uid(&hdr).await?;

        if identity.is_empty()
            || identity.len() > 48
            || !identity
                .chars()
                .all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_')
        {
            return Err(zbus::fdo::Error::InvalidArgs(
                "identity name must be 1-48 chars of [A-Za-z0-9_-]".into(),
            ));
        }

        // One in-flight session per user. Starting a second silently
        // would leave the first one's pipe open forever.
        {
            let mut map = self.sessions.lock().await;
            let stale: Vec<String> = map
                .iter()
                .filter(|(_, s)| s.uid == uid)
                .map(|(k, _)| k.clone())
                .collect();
            for k in stale {
                if let Some(mut s) = map.remove(&k) {
                    s.close_preview();
                }
            }
        }

        let (read_fd, write_fd) =
            make_pipe().map_err(|e| zbus::fdo::Error::Failed(format!("preview pipe: {e}")))?;
        // SAFETY: write_fd is a fresh owned descriptor from pipe2.
        let write_file = unsafe { std::fs::File::from_raw_fd(into_raw(write_fd)) };

        let id = self.next_session_id(uid).await;
        self.sessions.lock().await.insert(
            id.clone(),
            Session {
                uid,
                identity,
                preview: Some(write_file),
                embeddings: Vec::new(),
                model_id: String::new(),
                pose_index: 0,
                cancelled: false,
            },
        );

        Ok((id, zvariant::OwnedFd::from(read_fd)))
    }

    /// Capture one pose. Blocks for the length of one worker scan and
    /// streams preview frames down the pipe while it runs.
    async fn enroll_pose(
        &self,
        session: String,
        pose_index: u32,
        #[zbus(signal_context)] ctxt: SignalContext<'_>,
        #[zbus(header)] hdr: zbus::message::Header<'_>,
    ) -> zbus::fdo::Result<(String, f64)> {
        let uid = self.caller_uid(&hdr).await?;
        let idx = pose_index.min(POSES.len() as u32 - 1);
        let pose_name = POSES[idx as usize];

        // Confirm ownership before opening a camera for anyone.
        self.with_session(&session, uid, |s| {
            if s.cancelled {
                return Err(zbus::fdo::Error::Failed("session cancelled".into()));
            }
            s.pose_index = idx;
            Ok(())
        })
        .await?;

        let cfg = self.cfg.lock().await.clone();
        let _ = Self::enroll_progress(&ctxt, &session, idx, pose_name, ST_ACQUIRING, 0.0).await;

        // Forward worker progress as EnrollProgress so the app's bar
        // moves during the scan rather than jumping at the end.
        let (ptx, mut prx) = mpsc::channel::<Progress>(16);
        let sig_ctxt = ctxt.to_owned();
        let sig_session = session.clone();
        let pump = tokio::spawn(async move {
            while let Some(ev) = prx.recv().await {
                let (status, p) = match ev {
                    Progress::FaceFound => (ST_GOOD, 0.15),
                    Progress::Value(p) => (ST_GOOD, p as f64),
                    Progress::Challenge(_) => (ST_ACQUIRING, 0.1),
                };
                let _ = Enrollment1::enroll_progress(
                    &sig_ctxt,
                    &sig_session,
                    idx,
                    pose_name,
                    status,
                    p,
                )
                .await;
            }
        });

        // Stream worker preview frames into the caller's private pipe.
        // uid and session are owned strings copied from the method
        // args, and the sessions map is behind its own Arc, so this can
        // outlive the borrow of `self` for the duration of the scan.
        let (pv_tx, mut pv_rx) = mpsc::channel::<Vec<u8>>(16);
        let sessions = self.sessions.clone();
        let pv_session = session.clone();
        let pump_pv = tokio::spawn(async move {
            while let Some(jpeg) = pv_rx.recv().await {
                Enrollment1::deliver_preview_map(&sessions, &pv_session, uid, &jpeg).await;
            }
        });

        // Forward pose_status guidance (status + reason) to the same
        // pipe so the app can render "move closer / better light"
        // guidance that agrees with the worker's own quality gate.
        let (gd_tx, mut gd_rx) = mpsc::channel::<(String, String)>(8);
        let sessions = self.sessions.clone();
        let gd_session = session.clone();
        let pump_gd = tokio::spawn(async move {
            while let Some((status, reason)) = gd_rx.recv().await {
                Enrollment1::deliver_guidance_map(&sessions, &gd_session, uid, &status, &reason)
                    .await;
            }
        });

        // Liveness is "off" during enrollment on purpose: we are
        // building a reference for a user who is present and
        // authenticated, and a blink requirement here just makes
        // enrollment fail on people who blink at the wrong moment.
        // It stays fully enforced at unlock time.
        let result = self
            .worker
            .scan(
                &format!("{session}-p{idx}"),
                cfg.mode.as_str(),
                "off",
                cfg.scan_timeout_ms,
                false,
                Some(ptx),
                Some(pv_tx),
                cfg.ir_camera.clone(),
                Some(gd_tx),
            )
            .await;
        pump.abort();
        pump_pv.abort();
        pump_gd.abort();

        // Cancel may have arrived while the camera was open.
        let cancelled = self
            .with_session(&session, uid, |s| Ok(s.cancelled))
            .await
            .unwrap_or(true);
        if cancelled {
            let _ = Self::enroll_progress(&ctxt, &session, idx, pose_name, ST_CANCELLED, 0.0).await;
            return Ok((ST_CANCELLED.to_string(), 0.0));
        }

        let ev = match result {
            Ok(e) => e,
            Err(e) => {
                tracing::debug!("enroll pose {idx} failed: {e}");
                let _ =
                    Self::enroll_progress(&ctxt, &session, idx, pose_name, ST_FAILED, 0.0).await;
                return Ok((ST_FAILED.to_string(), 0.0));
            }
        };

        let (status, progress) = self
            .with_session(&session, uid, |s| {
                if s.model_id.is_empty() {
                    s.model_id = ev.model_id.clone();
                } else if s.model_id != ev.model_id {
                    // The worker restarted with a different model
                    // mid-enrollment. Mixing embeddings from two models
                    // produces a template set that matches nobody.
                    return Err(zbus::fdo::Error::Failed(
                        "recognition model changed mid-enrollment; start again".into(),
                    ));
                }
                for e in &ev.embeddings {
                    if s.embeddings.len() >= MAX_TEMPLATES {
                        break;
                    }
                    let dup = s.embeddings.iter().any(|t| {
                        crate::matcher::cosine_max(e, std::slice::from_ref(t)) > DUP_SIMILARITY
                    });
                    if !dup {
                        s.embeddings.push(e.clone());
                    }
                }
                let got = !ev.embeddings.is_empty();
                let p = (s.embeddings.len() as f64 / MAX_TEMPLATES as f64).min(1.0);
                Ok(if got {
                    (ST_POSE_COMPLETE.to_string(), p)
                } else {
                    (ST_FAILED.to_string(), p)
                })
            })
            .await?;

        let _ = Self::enroll_progress(&ctxt, &session, idx, pose_name, &status, progress).await;
        Ok((status, progress))
    }

    /// Persist what was collected and close the session.
    async fn finish_enrollment(
        &self,
        session: String,
        #[zbus(signal_context)] ctxt: SignalContext<'_>,
        #[zbus(header)] hdr: zbus::message::Header<'_>,
    ) -> zbus::fdo::Result<u32> {
        let uid = self.caller_uid(&hdr).await?;

        let mut map = self.sessions.lock().await;
        let mut s = map
            .remove(&session)
            .ok_or_else(|| zbus::fdo::Error::Failed("no such enrollment session".into()))?;
        if s.uid != uid {
            s.close_preview();
            return Err(zbus::fdo::Error::AccessDenied("not your session".into()));
        }
        s.close_preview();

        if s.embeddings.is_empty() {
            return Err(zbus::fdo::Error::Failed(
                "no usable frames were captured; try again in better light".into(),
            ));
        }
        let n = s.embeddings.len() as u32;
        self.store
            .save(
                uid,
                &Identity {
                    name: s.identity.clone(),
                    model_id: s.model_id.clone(),
                    enabled: true,
                    templates: std::mem::take(&mut s.embeddings),
                },
            )
            .map_err(|e| zbus::fdo::Error::Failed(e.to_string()))?;

        let _ = Self::enroll_progress(&ctxt, &session, s.pose_index, "", ST_FINISHED, 1.0).await;
        Ok(n)
    }

    /// Abandon a session. Closes the preview channel and discards every
    /// embedding collected so far; nothing is written to disk.
    async fn cancel_enrollment(
        &self,
        session: String,
        #[zbus(signal_context)] ctxt: SignalContext<'_>,
        #[zbus(header)] hdr: zbus::message::Header<'_>,
    ) -> zbus::fdo::Result<()> {
        let uid = self.caller_uid(&hdr).await?;
        let mut map = self.sessions.lock().await;
        if let Some(s) = map.get_mut(&session) {
            if s.uid != uid {
                s.close_preview();
                return Err(zbus::fdo::Error::AccessDenied("not your session".into()));
            }
            s.cancelled = true;
            s.close_preview();
            s.embeddings.clear();
            let idx = s.pose_index;
            drop(map);
            let _ = Self::enroll_progress(&ctxt, &session, idx, "", ST_CANCELLED, 0.0).await;
        }
        Ok(())
    }

    /// Where a session is, for a UI that reconnected mid-flow.
    async fn get_session_info(
        &self,
        session: String,
        #[zbus(header)] hdr: zbus::message::Header<'_>,
    ) -> zbus::fdo::Result<(u32, u32, String, u32)> {
        let uid = self.caller_uid(&hdr).await?;
        self.with_session(&session, uid, |s| {
            Ok((
                s.pose_index,
                POSES.len() as u32,
                POSES
                    .get(s.pose_index as usize)
                    .copied()
                    .unwrap_or("")
                    .to_string(),
                s.embeddings.len() as u32,
            ))
        })
        .await
    }

    #[zbus(signal)]
    pub async fn enroll_progress(
        ctxt: &SignalContext<'_>,
        session: &str,
        pose_index: u32,
        pose_name: &str,
        status: &str,
        progress: f64,
    ) -> zbus::Result<()>;
}

/// Hand a preview JPEG from the worker event loop to a session.
///
/// Called by whoever reads worker events (see worker.rs); kept here so
/// the only code that can write to a preview pipe lives in this file.
impl Enrollment1 {
    /// The same delivery, exposed as a static so a spawned pump task
    /// can hold a cloned `Arc` of the sessions map instead of an
    /// unbounded `&self`.
    async fn deliver_preview_map(
        sessions: &Mutex<HashMap<String, Session>>,
        session: &str,
        uid: u32,
        jpeg: &[u8],
    ) {
        let mut map = sessions.lock().await;
        let Some(s) = map.get_mut(session) else {
            return;
        };
        // Belt and braces: the session is already uid-scoped, but a
        // preview frame is the one thing in this daemon that would
        // actually matter if it went to the wrong place.
        if s.uid != uid || s.cancelled {
            return;
        }
        s.push_preview(jpeg);
    }

    /// Same uid-scoped delivery for pose guidance lines.
    async fn deliver_guidance_map(
        sessions: &Mutex<HashMap<String, Session>>,
        session: &str,
        uid: u32,
        status: &str,
        reason: &str,
    ) {
        let mut map = sessions.lock().await;
        let Some(s) = map.get_mut(session) else {
            return;
        };
        if s.uid != uid || s.cancelled {
            return;
        }
        s.push_guidance(status, reason);
    }
}

// ---- pipe helpers ------------------------------------------------------
// Kept tiny and in one place so the unsafe fd handling is auditable.

fn make_pipe() -> std::io::Result<(OwnedFd, OwnedFd)> {
    use nix::fcntl::OFlag;
    use nix::unistd::pipe2;
    // CLOEXEC so a preview descriptor can never leak into a child
    // process the daemon spawns.
    let (r, w) = pipe2(OFlag::O_CLOEXEC | OFlag::O_NONBLOCK)
        .map_err(|e| std::io::Error::from_raw_os_error(e as i32))?;
    Ok((r, w))
}

fn into_raw(fd: OwnedFd) -> std::os::fd::RawFd {
    let raw = fd.as_raw_fd();
    std::mem::forget(fd);
    raw
}
