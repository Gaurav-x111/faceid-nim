//! The scan state machine and the authentication decision.
//!
//! IDLE -> WAKING -> SEARCHING -> VERIFYING -> MATCHED
//!                      |             |------> REJECTED
//!                      |             '------> TIMEOUT
//!                      '--------------------> CAMERA_ERROR
//!
//! One rule governs this whole file: recognition and liveness are
//! evaluated independently, and a deny cue vetoes the unlock no matter
//! how good the match was.

use crate::audit::{Audit, Event};
use crate::config::{Config, Strictness};
use crate::matcher::vote;
use crate::policy::{Policy, Verdict};
use crate::store::Store;
use crate::worker::{Progress, Worker};
use std::collections::HashMap;
use std::sync::Arc;
use std::time::{Duration, Instant};
use tokio::sync::{mpsc, Mutex, Semaphore};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ScanState {
    #[allow(dead_code)]
    Idle,
    Waking,
    Searching,
    Verifying,
    Matched,
    Rejected,
    Timeout,
    CameraError,
}

impl ScanState {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Idle => "idle",
            Self::Waking => "waking",
            Self::Searching => "searching",
            Self::Verifying => "verifying",
            Self::Matched => "matched",
            Self::Rejected => "rejected",
            Self::Timeout => "timeout",
            Self::CameraError => "camera_error",
        }
    }
}

pub struct AuthOutcome {
    pub success: bool,
    /// Safe to show a user. Contains no score and no cue detail.
    pub message: String,
    pub unavailable: bool,
}

impl AuthOutcome {
    fn deny(msg: &str) -> Self {
        Self {
            success: false,
            message: msg.into(),
            unavailable: false,
        }
    }
    fn unavailable(msg: &str) -> Self {
        Self {
            success: false,
            message: msg.into(),
            unavailable: true,
        }
    }
}

pub struct Engine {
    pub cfg: Arc<Mutex<Config>>,
    pub store: Arc<Store>,
    pub worker: Arc<Worker>,
    pub policy: Arc<Mutex<Policy>>,
    pub audit: Arc<Audit>,
    /// Broadcast of (state, progress, reason) for the shell pill.
    pub state_tx: mpsc::Sender<(ScanState, f32, String)>,
    pub scan_slots: Arc<Semaphore>,
    /// Per-uid serialization of whole attempts (option (a) of the
    /// 2026-10-06 audit: one physical camera per machine, so same-user
    /// attempts gain nothing from running concurrently, while concurrent
    /// check-then-record let N parallel attempts each read a pre-lockout
    /// counter and blow past max_failures). The guard is taken before
    /// the policy check and held through record_success/record_failure,
    /// so check -> scan -> record is atomic per uid. Different uids use
    /// different locks and still run in parallel. Entries are kept
    /// (one per uid ever seen); the map is bounded by the uid space.
    pub auth_locks: Arc<Mutex<HashMap<u32, Arc<Mutex<()>>>>>,
}

impl Engine {
    async fn emit(&self, s: ScanState, p: f32, reason: &str) {
        let _ = self.state_tx.send((s, p, reason.to_string())).await;
    }

    pub async fn authenticate(&self, uid: u32, service: &str) -> AuthOutcome {
        let cfg = { self.cfg.lock().await.clone() };
        let t0 = Instant::now();
        let no_cues: Vec<String> = Vec::new();

        let note_f32 = |notes: &Option<&serde_json::Map<String, serde_json::Value>>, key: &str| {
            notes
                .and_then(|m| m.get(key))
                .and_then(|v| v.as_f64())
                .map(|v| v as f32)
        };

        // `spectrum` and `luma` are "&str"/Option borrowed from a local,
        // so they are resolved per call below rather than captured here:
        // before a scan runs there is no evidence to report. Empty means
        // "a decision was not reached" and the field is then omitted.
        let log = |result: &str,
                   reason: &str,
                   cues: &[String],
                   strict: &str,
                   notes: Option<&serde_json::Map<String, serde_json::Value>>,
                   valid: u64,
                   spectrum: &str,
                   luma: Option<f32>| {
            self.audit.log(Event {
                spectrum,
                luma,
                ts: 0,
                uid,
                service,
                result,
                reason,
                elapsed_ms: t0.elapsed().as_millis() as u64,
                strictness: strict,
                deny_cues: cues,
                moire_score: note_f32(&notes, "moire"),
                moire_threshold: note_f32(&notes, "moire_threshold"),
                valid_frames: (valid > 0).then_some(valid),
            });
            // Opt-in timeline: lock/unlock only, never sudo/polkit.
            // "success"/"failure" for real attempts, "refused"/"unavailable"
            // pass through so the JSON shows when the machine opened.
            self.audit.timeline(uid, service, result);
        };

        if !cfg.enabled {
            log(
                "refused",
                "disabled",
                &no_cues,
                cfg.strictness.as_str(),
                None,
                0,
                "unknown",
                None,
            );
            return AuthOutcome::unavailable(Verdict::Disabled.user_message());
        }
        if !cfg.service_allowed(service) {
            log(
                "refused",
                "service_not_allowed",
                &no_cues,
                cfg.strictness.as_str(),
                None,
                0,
                "unknown",
                None,
            );
            return AuthOutcome::unavailable(Verdict::ServiceNotAllowed.user_message());
        }
        // Guest / excluded users fall through to password before the
        // camera ever opens. Lookup is best-effort: an unresolvable uid
        // is never excluded (fail open to normal flow, fail closed at
        // match time as usual).
        if !cfg.exclude_users.is_empty() {
            let name = nix::unistd::User::from_uid(nix::unistd::Uid::from_raw(uid))
                .ok()
                .flatten()
                .map(|u| u.name);
            if name
                .as_deref()
                .is_some_and(|n| cfg.exclude_users.iter().any(|e| e == n))
            {
                log(
                    "refused",
                    "user_excluded",
                    &no_cues,
                    cfg.strictness.as_str(),
                    None,
                    0,
                    "unknown",
                    None,
                );
                return AuthOutcome::unavailable(Verdict::Disabled.user_message());
            }
        }
        // Per-uid attempt lock (audit BLOCKER 1, option (a)): held
        // from the policy check below through record_success /
        // record_failure at the end of this function, so two
        // concurrent attempts as the same uid cannot both pass check()
        // before either records. Owned guard: 'static, lives until
        // authenticate() returns. The map lock is released before
        // taking the per-uid lock, so lock order is always map-then-uid
        // and no deadlock is possible. Different uids take different
        // locks and still run in parallel.
        let _uid_guard = {
            let uid_lock = {
                let mut locks = self.auth_locks.lock().await;
                locks.entry(uid).or_default().clone()
            };
            uid_lock.lock_owned().await
        };
        {
            let mut pol = self.policy.lock().await;
            if pol.check(uid, cfg.max_failures) == Verdict::LockedOut {
                log(
                    "refused",
                    "locked_out",
                    &no_cues,
                    cfg.strictness.as_str(),
                    None,
                    0,
                    "unknown",
                    None,
                );
                return AuthOutcome::deny(Verdict::LockedOut.user_message());
            }
        }
        // Nothing enrolled (or everything disabled): refuse BEFORE the
        // camera ever opens. A scan with no templates to match can only
        // waste power and confuse ("why is it scanning when I deleted
        // my face?"). Falls through to the password like every other
        // unavailable path, and never touches the failure counter.
        // (Stale-model identities still scan: the running model_id is
        // only known after the worker answers, and that path already
        // refuses post-scan further below.)
        if !self.store.any_enabled(uid) {
            self.emit(ScanState::Rejected, 0.0, "").await;
            log(
                "refused",
                "no_identities",
                &no_cues,
                cfg.strictness.as_str(),
                None,
                0,
                "unknown",
                None,
            );
            return AuthOutcome::unavailable(Verdict::NoIdentities.user_message());
        }
        let _scan_permit = match self.scan_slots.acquire().await {
            Ok(permit) => permit,
            Err(_) => return AuthOutcome::unavailable("Face scan is unavailable"),
        };

        self.emit(ScanState::Waking, 0.0, "").await;

        let (ptx, mut prx) = mpsc::channel::<Progress>(16);
        let state_tx = self.state_tx.clone();
        let pump = tokio::spawn(async move {
            while let Some(ev) = prx.recv().await {
                let msg = match ev {
                    Progress::FaceFound => (ScanState::Verifying, 0.15, String::new()),
                    Progress::Value(p) => (ScanState::Verifying, p, String::new()),
                    Progress::Challenge(p) => (ScanState::Searching, 0.1, p),
                };
                let _ = state_tx.send(msg).await;
            }
        });

        self.emit(ScanState::Searching, 0.05, "").await;
        let sid = format!("{uid}-{}-{}", std::process::id(), t0.elapsed().as_nanos());
        let evidence = self
            .worker
            .scan(
                &sid,
                cfg.mode.as_str(),
                cfg.strictness.as_str(),
                cfg.scan_timeout_ms,
                cfg.strictness == Strictness::Heavy,
                Some(ptx),
                None,
                cfg.ir_camera.clone(),
                None,
                Some(cfg.camera.clone()),
            )
            .await;
        pump.abort();

        let ev = match evidence {
            Ok(e) => e,
            Err(e) => {
                let reason = e.to_string();
                // A broken camera or a dead worker must fall through to
                // the password, never count as a failed attempt.
                // Case-insensitive: worker sends "camera_unavailable".
                let lower = reason.to_lowercase();
                let unavailable = lower.contains("camera")
                    || lower.contains("connect worker")
                    || lower.contains("closed the connection")
                    || lower.contains("no such file")
                    || lower.contains("connection refused");
                let state = if unavailable {
                    ScanState::CameraError
                } else {
                    ScanState::Timeout
                };
                self.emit(state, 0.0, "").await;
                log(
                    if unavailable {
                        "unavailable"
                    } else {
                        "timeout"
                    },
                    &reason,
                    &no_cues,
                    cfg.strictness.as_str(),
                    None,
                    0,
                    "unknown",
                    None,
                );
                return if unavailable {
                    AuthOutcome::unavailable("Camera unavailable")
                } else {
                    AuthOutcome::deny("Face not recognised")
                };
            }
        };

        // ---- liveness first, and on its own ----------------------------
        let strict = cfg.strictness;
        let liveness_ok = if strict == Strictness::Off {
            true
        } else if !ev.liveness.deny.is_empty()
            || (cfg.require_attention && !ev.liveness.attention_ok)
        {
            false
        } else {
            strict != Strictness::Heavy || !ev.liveness.confirm.is_empty()
        };

        // ---- recognition, computed regardless, combined after ----------
        let identities = self.store.load_enabled(uid, &ev.model_id);
        if identities.is_empty() {
            self.emit(ScanState::Rejected, 0.0, "").await;
            log(
                "refused",
                "no_identities",
                &ev.liveness.deny,
                strict.as_str(),
                Some(&ev.liveness.notes),
                ev.usable,
                ev.spectrum.as_str(),
                ev.luma,
            );
            return AuthOutcome::unavailable(Verdict::NoIdentities.user_message());
        }
        // Templates carry no name, so build a parallel name index that
        // maps the best template back to the identity it enrolled under.
        // Spectrum gate: in auto mode the worker resolved rgb/ir per
        // scan; only same-spectrum templates (plus legacy ANY) vote.
        // Empty query spectrum (legacy worker) matches everything.
        let qtag = crate::store::spectrum_tag(&ev.spectrum);
        let mut templates: Vec<Vec<f32>> = Vec::new();
        let mut names: Vec<String> = Vec::new();
        for ident in &identities {
            for (ix, t) in ident.templates.iter().enumerate() {
                let tag = ident.spectrum_at(ix);
                if qtag != crate::store::SPECTRUM_ANY
                    && tag != crate::store::SPECTRUM_ANY
                    && tag != qtag
                {
                    continue;
                }
                templates.push(t.clone());
                names.push(ident.name.clone());
            }
        }
        let m = if templates.is_empty() {
            // No same-spectrum reference: report as no-match with a
            // re-enroll flavour downstream (reason carries counts).
            vote(&ev.embeddings, &[], cfg.tau, cfg.vote_k, cfg.vote_n)
        } else {
            vote(&ev.embeddings, &templates, cfg.tau, cfg.vote_k, cfg.vote_n)
        };

        if m.accepted && liveness_ok {
            self.policy.lock().await.record_success(uid);
            let winner = m
                .best_index
                .and_then(|ix| names.get(ix))
                .map(String::as_str)
                .unwrap_or("");
            self.emit(ScanState::Matched, 1.0, winner).await;
            tracing::debug!(uid, best = m.best, passes = m.passes, "accepted");
            log(
                "success",
                winner,
                &ev.liveness.deny,
                strict.as_str(),
                Some(&ev.liveness.notes),
                ev.usable,
                ev.spectrum.as_str(),
                ev.luma,
            );
            return AuthOutcome {
                success: true,
                message: String::new(),
                unavailable: false,
            };
        }

        let reason = if !liveness_ok && !ev.liveness.deny.is_empty() {
            format!("liveness_deny:{}", ev.liveness.deny.join("+"))
        } else if !liveness_ok {
            "liveness_unconfirmed".to_string()
        } else if templates.is_empty() {
            format!("no_match:no_{}_templates_reenroll_needed", ev.spectrum)
        } else {
            format!("no_match:{}of{}", m.passes, m.considered)
        };
        let locked = self.policy.lock().await.record_failure(
            uid,
            cfg.max_failures,
            Duration::from_secs(cfg.lockout_secs),
        );
        self.emit(ScanState::Rejected, 0.0, "").await;
        log(
            "failure",
            &reason,
            &ev.liveness.deny,
            strict.as_str(),
            Some(&ev.liveness.notes),
            ev.usable,
            ev.spectrum.as_str(),
            ev.luma,
        );

        // The user is told it failed, not why. Cue detail goes to the
        // audit log, where the attacker cannot read it during an attempt.
        AuthOutcome::deny(if locked {
            Verdict::LockedOut.user_message()
        } else {
            "Face not recognised"
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::audit::Audit;
    use crate::config::Config;
    use crate::store::Identity;
    use std::time::Duration as StdDuration;
    use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
    use tokio::net::UnixListener;

    const TEST_UID: u32 = 424242;
    const TEST_MODEL: &str = "test-model";

    /// Fake vision worker: answers every scan with a non-matching face
    /// after a delay, so concurrent attempts overlap in the scan phase.
    /// Without per-uid serialization every attempt passes the lockout
    /// check before any of them records, and the counter overshoots.
    async fn spawn_denying_worker(sock: std::path::PathBuf, delay: StdDuration) {
        let _ = std::fs::remove_file(&sock);
        let listener = UnixListener::bind(&sock).expect("bind fake worker");
        tokio::spawn(async move {
            loop {
                let Ok((stream, _)) = listener.accept().await else {
                    break;
                };
                tokio::spawn(async move {
                    let (rd, mut wr) = stream.into_split();
                    let mut lines = BufReader::new(rd).lines();
                    // One request per connection; ignore what it says.
                    let _ = lines.next_line().await;
                    tokio::time::sleep(delay).await;
                    let reply = serde_json::json!({
                        "ev": "done",
                        "embeddings": [[0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]],
                        "model_id": TEST_MODEL,
                        "liveness": {"deny": [], "confirm": [],
                                      "attention_ok": true},
                        "spectrum": "",
                    });
                    let _ = wr.write_all(format!("{reply}\n").as_bytes()).await;
                });
            }
        });
    }

    fn test_engine(sock: std::path::PathBuf, dir: &std::path::Path) -> Arc<Engine> {
        let store = Arc::new(Store::open(&dir.join("store")).expect("open test store"));
        // Enrolled template [1,0,...]: cosine 0.0 against the worker's
        // [0,1,...], far below tau, so every attempt is a clean no-match
        // that must increment the failure counter exactly once.
        let mut t = vec![0.0f32; 8];
        t[0] = 1.0;
        store
            .save(
                TEST_UID,
                &Identity {
                    name: "test".into(),
                    model_id: TEST_MODEL.into(),
                    enabled: true,
                    templates: vec![t],
                    spectra: vec![crate::store::SPECTRUM_ANY],
                },
            )
            .expect("save test identity");
        let cfg = Config {
            enabled: true,
            // Strictness off: liveness always passes, so the only failure
            // path is the recognition no-match that records an attempt.
            strictness: Strictness::Off,
            tau: 0.9,
            max_failures: 3,
            lockout_secs: 60,
            ..Config::default()
        };
        let audit = Arc::new(Audit::new(&dir.join("audit.log")));
        let (state_tx, mut state_rx) = mpsc::channel(64);
        // Drain pill broadcasts so a full channel never blocks a scan.
        tokio::spawn(async move { while state_rx.recv().await.is_some() {} });
        Arc::new(Engine {
            cfg: Arc::new(Mutex::new(cfg)),
            store,
            worker: Arc::new(Worker::new(sock)),
            policy: Arc::new(Mutex::new(Policy::default())),
            audit,
            state_tx,
            scan_slots: Arc::new(Semaphore::new(2)),
            auth_locks: Arc::new(Mutex::new(HashMap::new())),
        })
    }

    fn test_dir(name: &str) -> std::path::PathBuf {
        let d =
            std::env::temp_dir().join(format!("faceid-lockout-test-{name}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&d);
        std::fs::create_dir_all(&d).expect("test dir");
        d
    }

    /// Audit BLOCKER 1 verification: max_failures + 2 concurrent
    /// same-uid attempts against an always-denying worker must leave
    /// exactly max_failures recorded, with the extras refused as
    /// locked-out without consuming another scan-record.
    #[tokio::test]
    async fn concurrent_same_uid_attempts_lock_out_at_exactly_max_failures() {
        let dir = test_dir("concurrent");
        let sock = dir.join("vision.sock");
        spawn_denying_worker(sock.clone(), StdDuration::from_millis(200)).await;
        let engine = test_engine(sock, &dir);

        let mut handles = Vec::new();
        for _ in 0..5 {
            let e = engine.clone();
            handles.push(tokio::spawn(async move {
                e.authenticate(TEST_UID, "sudo").await
            }));
        }
        let mut locked_out = 0;
        let mut plain_fail = 0;
        for h in handles {
            let out = h.await.expect("attempt panicked");
            assert!(!out.success, "denying worker must never authenticate");
            if out.message == Verdict::LockedOut.user_message() {
                locked_out += 1;
            } else {
                plain_fail += 1;
            }
        }
        // Attempts 1-2 fail plainly, attempt 3 trips the lockout, and
        // attempts 4-5 are refused at the check without recording again.
        assert_eq!(plain_fail, 2, "exactly max_failures-1 plain failures");
        assert_eq!(locked_out, 3, "trip + 2 refusals carry the lockout message");
        assert_eq!(
            engine.policy.lock().await.failures(TEST_UID),
            3,
            "counter must stop at exactly max_failures, not overshoot"
        );
        // The lockout is real: one more attempt is refused immediately.
        let out = engine.authenticate(TEST_UID, "sudo").await;
        assert!(!out.success);
        assert_eq!(out.message, Verdict::LockedOut.user_message());
        let _ = std::fs::remove_dir_all(&dir);
    }

    /// Same-uid attempts also serialize when run back-to-back: the
    /// counter still trips at exactly max_failures.
    #[tokio::test]
    async fn sequential_attempts_lock_out_at_max_failures() {
        let dir = test_dir("sequential");
        let sock = dir.join("vision.sock");
        spawn_denying_worker(sock.clone(), StdDuration::from_millis(10)).await;
        let engine = test_engine(sock, &dir);

        for _ in 0..3 {
            let out = engine.authenticate(TEST_UID, "sudo").await;
            assert!(!out.success);
        }
        assert_eq!(engine.policy.lock().await.failures(TEST_UID), 3);
        let out = engine.authenticate(TEST_UID, "sudo").await;
        assert_eq!(out.message, Verdict::LockedOut.user_message());
        let _ = std::fs::remove_dir_all(&dir);
    }

    /// Fake worker that dies mid-scan: accepts the connection, reads
    /// the request, then closes without a reply (what SIGKILL looks
    /// like from the daemon side).
    async fn spawn_dying_worker(sock: std::path::PathBuf) {
        let _ = std::fs::remove_file(&sock);
        let listener = UnixListener::bind(&sock).expect("bind dying worker");
        tokio::spawn(async move {
            while let Ok((stream, _)) = listener.accept().await {
                tokio::spawn(async move {
                    let (rd, _wr) = stream.into_split();
                    let mut lines = BufReader::new(rd).lines();
                    let _ = lines.next_line().await;
                    tokio::time::sleep(StdDuration::from_millis(100)).await;
                    // Drop without replying: connection closed mid-scan.
                });
            }
        });
    }

    /// Audit HIGH 2 verification (daemon half): a worker killed
    /// mid-scan must surface as UNAVAILABLE (fall through to
    /// password), never as success, inside the PAM deadline -- and a
    /// dead worker must not consume a lockout attempt.
    #[tokio::test]
    async fn worker_killed_mid_scan_is_unavailable_not_a_failure() {
        let dir = test_dir("dying");
        let sock = dir.join("vision.sock");
        spawn_dying_worker(sock.clone()).await;
        let engine = test_engine(sock, &dir);
        // Short scan so the test does not wait out the 4 s default.
        engine.cfg.lock().await.scan_timeout_ms = 500;

        let start = std::time::Instant::now();
        let out = engine.authenticate(TEST_UID, "sudo").await;
        let elapsed = start.elapsed();

        assert!(!out.success, "a dead worker must never authenticate");
        assert!(
            out.unavailable,
            "worker death must be UNAVAIL (password fall-through), got {:?}",
            out.message,
        );
        assert!(
            elapsed < StdDuration::from_secs(8),
            "fall-through must land inside the 8 s PAM deadline, took {elapsed:?}",
        );
        assert_eq!(
            engine.policy.lock().await.failures(TEST_UID),
            0,
            "a dead worker is not a failed attempt"
        );
        let _ = std::fs::remove_dir_all(&dir);
    }

    /// No enrollments: the daemon must refuse WITHOUT opening the
    /// camera ("deleted my face and it still scans"). The worker
    /// socket here is bogus on purpose: any scan attempt would fail
    /// as a camera/worker error, so a clean no_identities refusal
    /// proves no scan was attempted. The failure counter stays at 0.
    #[tokio::test]
    async fn no_enrollments_refuses_without_scanning() {
        let dir = test_dir("empty-store");
        let sock = dir.join("no-worker.sock");
        let store = Arc::new(Store::open(&dir.join("store")).expect("open test store"));
        let cfg = Config {
            enabled: true,
            ..Config::default()
        };
        let audit = Arc::new(Audit::new(&dir.join("audit.log")));
        let (state_tx, mut state_rx) = mpsc::channel(64);
        tokio::spawn(async move { while state_rx.recv().await.is_some() {} });
        let engine = Arc::new(Engine {
            cfg: Arc::new(Mutex::new(cfg)),
            store,
            worker: Arc::new(Worker::new(sock)),
            policy: Arc::new(Mutex::new(Policy::default())),
            audit,
            state_tx,
            scan_slots: Arc::new(Semaphore::new(2)),
            auth_locks: Arc::new(Mutex::new(HashMap::new())),
        });

        let out = engine.authenticate(TEST_UID, "sudo").await;
        assert!(!out.success);
        assert!(out.unavailable, "must fall through to password");
        assert_eq!(out.message, Verdict::NoIdentities.user_message());
        assert_eq!(engine.policy.lock().await.failures(TEST_UID), 0);
        let log = std::fs::read_to_string(dir.join("audit.log")).expect("audit written");
        assert!(log.contains("no_identities"), "audit must show {log}");
        assert!(
            !log.to_lowercase().contains("camera") && !log.to_lowercase().contains("worker"),
            "no camera/worker error may appear: {log}"
        );
        let _ = std::fs::remove_dir_all(&dir);
    }
}
