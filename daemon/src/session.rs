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
use std::sync::Arc;
use std::time::{Duration, Instant};
use tokio::sync::{mpsc, Mutex};

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

        let log = |result: &str,
                   reason: &str,
                   cues: &[String],
                   strict: &str,
                   notes: Option<&serde_json::Map<String, serde_json::Value>>,
                   valid: u64| {
            self.audit.log(Event {
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
        };

        if !cfg.enabled {
            log(
                "refused",
                "disabled",
                &no_cues,
                cfg.strictness.as_str(),
                None,
                0,
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
            );
            return AuthOutcome::unavailable(Verdict::ServiceNotAllowed.user_message());
        }
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
                );
                return AuthOutcome::deny(Verdict::LockedOut.user_message());
            }
        }

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
        let sid = format!("{uid}-{}", t0.elapsed().as_nanos());
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
            )
            .await;
        pump.abort();

        let ev = match evidence {
            Ok(e) => e,
            Err(e) => {
                let reason = e.to_string();
                // A broken camera or a dead worker must fall through to
                // the password, never count as a failed attempt.
                let unavailable = reason.contains("camera")
                    || reason.contains("connect worker")
                    || reason.contains("closed the connection");
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
            );
            return AuthOutcome::unavailable(Verdict::NoIdentities.user_message());
        }
        // Templates carry no name, so build a parallel name index that
        // maps the best template back to the identity it enrolled under.
        let templates: Vec<Vec<f32>> = identities
            .iter()
            .flat_map(|i| i.templates.iter().cloned())
            .collect();
        let names: Vec<String> = identities
            .iter()
            .flat_map(|i| (0..i.templates.len()).map(move |_| i.name.clone()))
            .collect();
        let m = vote(&ev.embeddings, &templates, cfg.tau, cfg.vote_k, cfg.vote_n);

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
