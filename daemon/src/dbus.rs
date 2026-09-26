//! org.faceidnim.Daemon1 on the system bus.
//!
//! The ScanState signal is what drives the lock-screen pill. It
//! carries a state name, a progress number and a short reason string:
//! no frames, no embeddings, no scores. That is what lets an untrusted
//! GNOME extension subscribe to it safely.
//!
//! Writes (settings, enrollment, deletion) are gated by polkit, which
//! is what stops someone at your unlocked laptop enrolling their own
//! face onto your account.

use crate::authz;
use crate::config::{Config, Strictness};
use crate::matcher::vote;
use crate::session::{Engine, ScanState};
use crate::store::{Identity, Store};
use crate::worker::Progress;
use std::collections::HashMap;
use std::io::Write;
use std::os::fd::{AsRawFd, FromRawFd, OwnedFd};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::Duration;
use tokio::sync::{mpsc, Mutex};
use zbus::{interface, object_server::SignalContext, zvariant, Connection};

pub struct Daemon1 {
    pub cfg: Arc<Mutex<Config>>,
    pub cfg_path: std::path::PathBuf,
    pub store: Arc<Store>,
    pub engine: Arc<Engine>,
    /// Used to ask the bus who the sender really is. Never trust a uid
    /// supplied in the method arguments.
    pub conn: Connection,
    /// Active unprivileged preview sessions (view-only, no store write),
    /// keyed by `pv{uid}-{n}` mapping to a cancel flag.
    previews: Arc<Mutex<HashMap<String, Arc<AtomicBool>>>>,
    /// Monotonic id source for preview session names.
    preview_counter: Arc<Mutex<u64>>,
}

impl Daemon1 {
    async fn caller_uid(&self, hdr: &zbus::message::Header<'_>) -> zbus::fdo::Result<u32> {
        let sender = hdr
            .sender()
            .ok_or_else(|| zbus::fdo::Error::AccessDenied("no sender".into()))?;
        let proxy = zbus::fdo::DBusProxy::new(&self.conn)
            .await
            .map_err(|e| zbus::fdo::Error::Failed(e.to_string()))?;
        proxy.get_connection_unix_user(sender.clone().into()).await
    }

    pub fn new(
        cfg: Arc<Mutex<Config>>,
        cfg_path: std::path::PathBuf,
        store: Arc<Store>,
        engine: Arc<Engine>,
        conn: Connection,
    ) -> Self {
        Self {
            cfg,
            cfg_path,
            store,
            engine,
            conn,
            previews: Arc::new(Mutex::new(HashMap::new())),
            preview_counter: Arc::new(Mutex::new(0)),
        }
    }
}

#[interface(name = "org.faceidnim.Daemon1")]
impl Daemon1 {
    /// Identities enrolled for the calling user.
    async fn list_identities(
        &self,
        #[zbus(header)] hdr: zbus::message::Header<'_>,
    ) -> zbus::fdo::Result<Vec<(String, bool, String)>> {
        let uid = self.caller_uid(&hdr).await?;
        let mut out = Vec::new();
        for name in self.store.list(uid) {
            match self.store.load(uid, &name) {
                Ok(i) => out.push((i.name, i.enabled, i.model_id)),
                Err(e) => tracing::warn!("unreadable identity {name}: {e}"),
            }
        }
        Ok(out)
    }

    async fn set_identity_enabled(
        &self,
        name: String,
        enabled: bool,
        #[zbus(header)] hdr: zbus::message::Header<'_>,
    ) -> zbus::fdo::Result<()> {
        let uid = self.caller_uid(&hdr).await?;
        authz::require_polkit(&self.conn, &hdr, authz::ACTION_SETTINGS).await?;
        let mut i = self
            .store
            .load(uid, &name)
            .map_err(|e| zbus::fdo::Error::Failed(e.to_string()))?;
        i.enabled = enabled;
        self.store
            .save(uid, &i)
            .map_err(|e| zbus::fdo::Error::Failed(e.to_string()))
    }

    async fn delete_identity(
        &self,
        name: String,
        #[zbus(header)] hdr: zbus::message::Header<'_>,
    ) -> zbus::fdo::Result<()> {
        let uid = self.caller_uid(&hdr).await?;
        authz::require_polkit(&self.conn, &hdr, authz::ACTION_DELETE).await?;
        // Deletion removes the encrypted file immediately; there is no
        // "disabled but retained" state hiding biometric data.
        self.store
            .delete(uid, &name)
            .map_err(|e| zbus::fdo::Error::Failed(e.to_string()))
    }

    async fn delete_all_data(
        &self,
        #[zbus(header)] hdr: zbus::message::Header<'_>,
    ) -> zbus::fdo::Result<()> {
        let uid = self.caller_uid(&hdr).await?;
        authz::require_polkit(&self.conn, &hdr, authz::ACTION_DELETE).await?;
        self.store
            .delete_all(uid)
            .map_err(|e| zbus::fdo::Error::Failed(e.to_string()))
    }

    /// Enroll from a live scan. The caller must already have passed
    /// polkit + password re-auth in the app; the daemon re-checks
    /// polkit before writing anything.
    async fn enroll(
        &self,
        identity: String,
        poses: u32,
        #[zbus(header)] hdr: zbus::message::Header<'_>,
    ) -> zbus::fdo::Result<u32> {
        let uid = self.caller_uid(&hdr).await?;
        authz::require_polkit(&self.conn, &hdr, authz::ACTION_ENROLL).await?;
        let cfg = self.cfg.lock().await.clone();
        let mut collected: Vec<Vec<f32>> = Vec::new();
        let mut spectra: Vec<u8> = Vec::new();
        let mut model_id = String::new();
        let push = |collected: &mut Vec<Vec<f32>>,
                    spectra: &mut Vec<u8>,
                    fresh: Vec<Vec<f32>>,
                    tag: u8| {
            for e in fresh {
                if collected.len() >= 15 {
                    break;
                }
                // Dedup within spectrum only.
                let dup = collected.iter().enumerate().any(|(ix, t)| {
                    spectra
                        .get(ix)
                        .copied()
                        .unwrap_or(crate::store::SPECTRUM_ANY)
                        == tag
                        && crate::matcher::cosine_max(&e, std::slice::from_ref(t)) > 0.92
                });
                if !dup {
                    collected.push(e);
                    spectra.push(tag);
                }
            }
        };

        for pose in 0..poses.clamp(1, 12) {
            let ev = self
                .engine
                .worker
                .scan(
                    &format!("enroll-{uid}-{pose}"),
                    cfg.mode.as_str(),
                    "off",
                    cfg.scan_timeout_ms,
                    false,
                    None,
                    None,
                    cfg.ir_camera.clone(),
                    None,
                    Some(cfg.camera.clone()),
                )
                .await
                .map_err(|e| zbus::fdo::Error::Failed(e.to_string()))?;
            if model_id.is_empty() {
                model_id = ev.model_id.clone();
            }
            let tag = {
                let t = crate::store::spectrum_tag(&ev.spectrum);
                if t != crate::store::SPECTRUM_ANY {
                    t
                } else if cfg.mode.as_str() == "ir" {
                    crate::store::SPECTRUM_IR
                } else {
                    crate::store::SPECTRUM_RGB
                }
            };
            push(&mut collected, &mut spectra, ev.embeddings, tag);
            // Dual-spectrum second capture (quiet, other sensor only).
            let other_mode = if tag == crate::store::SPECTRUM_IR {
                "rgb"
            } else {
                "ir"
            };
            let has_other = if other_mode == "ir" {
                cfg.ir_camera.is_some()
            } else {
                true
            };
            if has_other && collected.len() < 15 {
                let (o_ir, o_dev) = if other_mode == "ir" {
                    (cfg.ir_camera.clone(), Some(cfg.camera.clone()))
                } else {
                    (None, Some(cfg.camera.clone()))
                };
                if let Ok(e2) = self
                    .engine
                    .worker
                    .scan(
                        &format!("enroll-{uid}-{pose}-x"),
                        other_mode,
                        "off",
                        cfg.scan_timeout_ms,
                        false,
                        None,
                        None,
                        o_ir,
                        None,
                        o_dev,
                    )
                    .await
                {
                    if e2.model_id.is_empty() || e2.model_id == model_id {
                        let tag2 = {
                            let t = crate::store::spectrum_tag(&e2.spectrum);
                            if t != crate::store::SPECTRUM_ANY {
                                t
                            } else if other_mode == "ir" {
                                crate::store::SPECTRUM_IR
                            } else {
                                crate::store::SPECTRUM_RGB
                            }
                        };
                        push(&mut collected, &mut spectra, e2.embeddings, tag2);
                    }
                }
            }
        }
        if collected.is_empty() {
            return Err(zbus::fdo::Error::Failed("no usable frames captured".into()));
        }
        collected.truncate(15);
        spectra.truncate(15);
        let n = collected.len() as u32;
        self.store
            .save(
                uid,
                &Identity {
                    name: identity,
                    model_id,
                    enabled: true,
                    templates: collected,
                    spectra,
                },
            )
            .map_err(|e| zbus::fdo::Error::Failed(e.to_string()))?;
        Ok(n)
    }

    /// Run a scan and report the result without unlocking anything.
    /// A direct worker scan on purpose: it must work even when faceid
    /// is disabled or the service list has no "test" entry, and it must
    /// never touch the failure counter or the PAM decision path. It
    /// runs the SAME pipeline as a real unlock (same mode, strictness,
    /// attention and challenge settings) and judges the evidence with
    /// the same liveness rule as session.rs, so a "recognized" here
    /// means an unlock would succeed too. It still drives the pill's
    /// ScanState (waking → searching → verifying with live progress,
    /// then matched or rejected), so the whole success sequence plays
    /// from the settings app exactly as it does at a real unlock —
    /// the only difference is that nothing was unlocked.
    async fn test_scan(
        &self,
        #[zbus(header)] hdr: zbus::message::Header<'_>,
    ) -> zbus::fdo::Result<(bool, String)> {
        let uid = self.caller_uid(&hdr).await?;
        let cfg = self.cfg.lock().await.clone();
        let stx = self.engine.state_tx.clone();

        let _ = stx.send((ScanState::Waking, 0.0, String::new())).await;
        let _ = stx.send((ScanState::Searching, 0.05, String::new())).await;

        let (ptx, mut prx) = mpsc::channel::<Progress>(16);
        let pump_stx = stx.clone();
        let pump = tokio::spawn(async move {
            while let Some(ev) = prx.recv().await {
                let msg = match ev {
                    Progress::FaceFound => (ScanState::Verifying, 0.15, String::new()),
                    Progress::Value(p) => (ScanState::Verifying, p, String::new()),
                    Progress::Challenge(p) => (ScanState::Searching, 0.1, p),
                };
                let _ = pump_stx.send(msg).await;
            }
        });

        let ev = self
            .engine
            .worker
            .scan(
                &format!("test-{uid}"),
                cfg.mode.as_str(),
                // Same evidence as a real unlock: the old "off" here
                // skipped liveness cue collection, so the test could say
                // "recognized" while every real attempt died on a veto.
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

        match ev {
            Ok(e) if !e.embeddings.is_empty() => {
                // A captured face is not the same thing as a recognized
                // face. Compare against this user's enabled templates so
                // the diagnostic cannot give a false "recognized" result.
                // Spectrum-gated like Engine::authenticate.
                let identities = self.store.load_enabled(uid, &e.model_id);
                let qtag = crate::store::spectrum_tag(&e.spectrum);
                let mut templates: Vec<Vec<f32>> = Vec::new();
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
                    }
                }
                let cfg = self.cfg.lock().await.clone();
                let m = vote(&e.embeddings, &templates, cfg.tau, cfg.vote_k, cfg.vote_n);
                // Same liveness rule as Engine::authenticate: a match
                // the real pipeline would veto must not report success.
                let strict = cfg.strictness;
                let liveness_ok = if strict == Strictness::Off {
                    true
                } else if !e.liveness.deny.is_empty()
                    || (cfg.require_attention && !e.liveness.attention_ok)
                {
                    false
                } else {
                    strict != Strictness::Heavy || !e.liveness.confirm.is_empty()
                };

                if m.accepted && liveness_ok {
                    let _ = stx
                        .send((ScanState::Matched, 1.0, "Test scan".to_string()))
                        .await;
                    Ok((true, "recognized enrolled face".to_string()))
                } else {
                    let _ = stx.send((ScanState::Rejected, 0.0, String::new())).await;
                    // Tell the user WHY the unlock would fail: match
                    // problems and liveness problems have different
                    // fixes (re-enroll / lower tau vs light, pose,
                    // strictness). Reason strings mirror session.rs.
                    let msg = if templates.is_empty() {
                        format!(
                            "face captured ({}), but no {} templates enrolled — re-enroll needed",
                            if e.spectrum.is_empty() {
                                "unknown spectrum"
                            } else {
                                &e.spectrum
                            },
                            if e.spectrum.is_empty() {
                                "matching"
                            } else {
                                &e.spectrum
                            },
                        )
                    } else if !m.accepted {
                        format!(
                            "face captured, but not recognized ({} of {} votes)",
                            m.passes, m.considered
                        )
                    } else if !e.liveness.deny.is_empty() {
                        format!(
                            "face matches, but unlock would fail: liveness veto ({})",
                            e.liveness.deny.join("+")
                        )
                    } else if cfg.require_attention && !e.liveness.attention_ok {
                        "face matches, but unlock would fail: eyes closed or looking away"
                            .to_string()
                    } else {
                        "face matches, but unlock would fail: no live-face confirmation (try Heavy blink/head motion)".to_string()
                    };
                    Ok((false, msg))
                }
            }
            Ok(_) => {
                let _ = stx.send((ScanState::Rejected, 0.0, String::new())).await;
                Ok((false, "no face captured".to_string()))
            }
            Err(e) => {
                let _ = stx.send((ScanState::Rejected, 0.0, String::new())).await;
                Ok((false, e.to_string()))
            }
        }
    }

    /// Developer-only preview: plays the full success sequence on the
    /// pill with NO camera, NO scan, NO template read and NO auth
    /// decision. It only emits ScanState signals, so a call can never
    /// unlock anything, touch the failure counter or change a setting.
    /// The requested identity name is sanitised and merely shown in the
    /// greeting; it is never trusted for anything.
    async fn preview_animation(
        &self,
        user: String,
        #[zbus(header)] hdr: zbus::message::Header<'_>,
    ) -> zbus::fdo::Result<()> {
        let _uid = self.caller_uid(&hdr).await?;
        let name: String = user.chars().filter(|c| !c.is_control()).take(24).collect();
        let ctxt = SignalContext::new(&self.conn, "/org/faceidnim/Daemon1")
            .map_err(|e| zbus::fdo::Error::Failed(e.to_string()))?;

        tokio::spawn(async move {
            // A deterministic WAKING -> SEARCHING -> VERIFYING (ramp) ->
            // MATCHED timeline. The pill drives everything that follows
            // (check, Verified, Welcome, identity) off the matched event.
            let emit = |state: &str, progress: f64, reason: &str| {
                let ctxt = &ctxt;
                let reason = reason.to_string();
                let state = state.to_string();
                async move {
                    let _ = Daemon1::scan_state(ctxt, &state, progress, &reason).await;
                }
            };
            let _ = emit("waking", 0.0, "").await;
            tokio::time::sleep(Duration::from_millis(90)).await;
            let _ = emit("searching", 0.05, "").await;
            tokio::time::sleep(Duration::from_millis(130)).await;
            for (progress, delay_ms) in [
                (0.15f64, 0u64),
                (0.45f64, 240u64),
                (0.78f64, 470u64),
                (0.95f64, 620u64),
            ] {
                let _ = emit("verifying", progress, "").await;
                tokio::time::sleep(Duration::from_millis(delay_ms)).await;
            }
            let _ = emit("matched", 1.0, &name).await;
            // The pill contracts on its own clock; nothing more to say.
        });
        Ok(())
    }

    async fn get_settings(&self) -> zbus::fdo::Result<String> {
        let cfg = self.cfg.lock().await;
        serde_json::to_string(&*cfg).map_err(|e| zbus::fdo::Error::Failed(e.to_string()))
    }

    async fn set_settings(
        &self,
        json: String,
        #[zbus(header)] hdr: zbus::message::Header<'_>,
    ) -> zbus::fdo::Result<()> {
        authz::require_polkit(&self.conn, &hdr, authz::ACTION_SETTINGS).await?;
        let mut incoming: Config = serde_json::from_str(&json)
            .map_err(|e| zbus::fdo::Error::InvalidArgs(e.to_string()))?;
        incoming.validate();
        // Paths are daemon-owned. Accepting them over D-Bus would let a
        // caller point the template store anywhere.
        let mut cur = self.cfg.lock().await;
        incoming.data_dir = cur.data_dir.clone();
        incoming.worker_socket = cur.worker_socket.clone();
        incoming.auth_socket = cur.auth_socket.clone();
        incoming.audit_log = cur.audit_log.clone();
        incoming.timeline_log = cur.timeline_log.clone();
        *cur = incoming;
        cur.save(&self.cfg_path)
            .map_err(|e| zbus::fdo::Error::Failed(e.to_string()))?;
        self.engine
            .audit
            .configure_timeline(&cur.timeline_log, cur.timeline_enabled);
        Ok(())
    }

    async fn set_pam_enabled(
        &self,
        enabled: bool,
        #[zbus(header)] hdr: zbus::message::Header<'_>,
    ) -> zbus::fdo::Result<()> {
        authz::require_polkit(&self.conn, &hdr, authz::ACTION_ENABLE).await?;
        let action = if enabled { "--enable" } else { "--disable" };
        let status = tokio::process::Command::new("pam-auth-update")
            .args(["--package", action, "faceid-nim"])
            .status()
            .await
            .map_err(|e| zbus::fdo::Error::Failed(format!("pam-auth-update: {e}")))?;
        if !status.success() {
            return Err(zbus::fdo::Error::Failed(
                "pam-auth-update could not change the PAM profile".into(),
            ));
        }
        Ok(())
    }

    async fn diagnostics(&self) -> zbus::fdo::Result<String> {
        let cfg = self.cfg.lock().await.clone();
        let worker_up = self.engine.worker.ping().await;
        // Only positive evidence counts: the worker resolves and verifies
        // the detector + recognizer before it binds its socket, so a
        // capabilities reply proves the models are in place. No reply
        // means "not known ready", never an optimistic guess.
        let caps = self.engine.worker.capabilities().await.ok();
        let models_ready = caps.is_some();
        let mut out = serde_json::json!({
            "enabled": cfg.enabled,
            "worker_reachable": worker_up,
            "models_ready": models_ready,
            "strictness": cfg.strictness.as_str(),
            "mode": cfg.mode.as_str(),
            "tau": cfg.tau,
            "vote": format!("{}-of-{}", cfg.vote_k, cfg.vote_n),
            "camera": cfg.camera,
            "ir_camera": cfg.ir_camera,
            "timeline_enabled": cfg.timeline_enabled,
        });
        if let Some(c) = caps {
            out["liveness"] = serde_json::json!({
                "landmarks": c.landmarks,
                "landmarks_reason": c.landmarks_reason,
                "landmarks_ok": c.mesh,
                "antispoof": c.antispoof,
                "antispoof_reason": c.antispoof_reason,
                "summary": c.liveness_summary(),
            });
            out["accel"] = serde_json::json!({
                "providers": c.accel.providers,
                "summary": c.accel.summary(),
                "arch": c.accel.arch,
                "cpu_count": c.accel.cpu_count,
                "threads": c.accel.threads,
                "features": c.accel.features,
                "unverified": c.accel.unverified,
            });
            // Which model the worker actually loaded, and whether it was
            // started with an IR device. Enrolled templates record a
            // model_id, so a mismatch here is what "re-enroll needed"
            // means, and the app can now show it before the user hits
            // that wall.
            out["worker"] = serde_json::json!({
                "model_id": c.model_id,
                "embedding_dim": c.embedding_dim,
                "ir_device": c.ir,
            });
        }
        Ok(out.to_string())
    }

    /// Opt-in lock/unlock timeline as JSON (newest last, max 200).
    /// Only lock/unlock services are ever stored; sudo/polkit never is.
    async fn get_timeline(&self) -> zbus::fdo::Result<String> {
        let cfg = self.cfg.lock().await.clone();
        let path = cfg.timeline_log;
        let text = std::fs::read_to_string(&path).unwrap_or_default();
        let lines: Vec<&str> = text.lines().collect();
        let tail = if lines.len() > 200 {
            &lines[lines.len() - 200..]
        } else {
            &lines[..]
        };
        let mut out = Vec::with_capacity(tail.len());
        for l in tail {
            if let Ok(v) = serde_json::from_str::<serde_json::Value>(l) {
                out.push(v);
            }
        }
        serde_json::to_string(&out).map_err(|e| zbus::fdo::Error::Failed(e.to_string()))
    }

    #[zbus(signal)]
    pub async fn scan_state(
        ctxt: &SignalContext<'_>,
        state: &str,
        progress: f64,
        reason: &str,
    ) -> zbus::Result<()>;

    /// Open a view-only preview stream. Unprivileged by design: this is
    /// the same "look through the camera" the user gets from any webcam
    /// app, so it must NEVER require a password. Frames and pose_status
    /// guidance arrive on a private pipe, framed as:
    ///   b'J' + u32be + jpeg  (frame)
    ///   b'G' + u32be + "status|reason"  (live pose guidance)
    /// The stream runs until StopPreview (or EOF when the daemon or
    /// worker is unavailable). No templates are read or written.
    async fn start_preview(
        &self,
        #[zbus(header)] hdr: zbus::message::Header<'_>,
    ) -> zbus::fdo::Result<zvariant::OwnedFd> {
        let uid = self.caller_uid(&hdr).await?;
        let (read_pipe, write_pipe) =
            make_pipe2().map_err(|e| zbus::fdo::Error::Failed(format!("preview pipe: {e}")))?;

        let cfg = self.cfg.lock().await.clone();
        let cancel = Arc::new(AtomicBool::new(false));
        let mut counter = self.preview_counter.lock().await;
        *counter += 1;
        let id = format!("pv{uid}-{counter}");
        drop(counter);

        // UID-scoped: a second preview from the same user replaces the
        // first, so a stray pipe can never accumulate.
        {
            let mut map = self.previews.lock().await;
            for (k, c) in map.iter() {
                if k.starts_with(&format!("pv{uid}-")) {
                    c.store(true, Ordering::Relaxed);
                }
            }
            map.insert(id.clone(), cancel.clone());
        }

        let engine = self.engine.clone();
        let cancel2 = cancel.clone();
        let previews = self.previews.clone();
        let id2 = id.clone();

        // SAFETY: write_pipe is a fresh owned descriptor from pipe2.
        let mut writer = unsafe { std::fs::File::from_raw_fd(write_pipe.as_raw_fd()) };
        std::mem::forget(write_pipe);

        tokio::spawn(async move {
            let mut round = 0u64;
            'outer: loop {
                if cancel2.load(Ordering::Relaxed) {
                    break;
                }
                round += 1;
                let (pv_tx, mut pv_rx) = mpsc::channel::<Vec<u8>>(32);
                let (gd_tx, mut gd_rx) = mpsc::channel::<(String, String)>(8);

                let name = format!("{id2}-{round}");
                let mut scan = Box::pin(engine.worker.scan(
                    &name,
                    cfg.mode.as_str(),
                    "off",
                    cfg.scan_timeout_ms,
                    false,
                    None,
                    Some(pv_tx),
                    cfg.ir_camera.clone(),
                    Some(gd_tx),
                    Some(cfg.camera.clone()),
                ));
                let mut reader_closed = false;
                let result = loop {
                    tokio::select! {
                        jpeg = pv_rx.recv() => {
                            let Some(jpeg) = jpeg else { break Ok(()) as Result<(), anyhow::Error>; };
                            let mut buf = Vec::with_capacity(5 + jpeg.len());
                            buf.push(b'J');
                            buf.extend_from_slice(&(jpeg.len() as u32).to_be_bytes());
                            buf.extend_from_slice(&jpeg);
                            if writer.write_all(&buf).is_err() {
                                reader_closed = true;
                                break Ok(()) as Result<(), anyhow::Error>;
                            }
                        }
                        g = gd_rx.recv() => {
                            let Some((st, r)) = g else { continue; };
                            let line = format!("{st}|{r}");
                            let mut buf = vec![b'G'];
                            buf.extend_from_slice(&(line.len() as u32).to_be_bytes());
                            buf.extend_from_slice(line.as_bytes());
                            if writer.write_all(&buf).is_err() {
                                reader_closed = true;
                                break Ok(()) as Result<(), anyhow::Error>;
                            }
                        }
                        res = &mut scan => {
                            break match res {
                                Ok(_) => Ok(()),
                                Err(e) => Err(e),
                            };
                        }
                    }
                };

                if reader_closed {
                    // A closed reader is terminal: retrying would keep
                    // the camera open forever after the app exits.
                    cancel2.store(true, Ordering::Relaxed);
                    break 'outer;
                }
                if result.is_err() {
                    // Camera or worker temporary failure: give the app a
                    // moment, then keep trying. The app decides whether
                    // that reads as "busy" or "off".
                    tokio::time::sleep(std::time::Duration::from_millis(400)).await;
                }
            }
            drop(writer);
            let mut map = previews.lock().await;
            map.remove(&id2);
        });

        Ok(zvariant::OwnedFd::from(read_pipe))
    }

    /// Stop a preview session by closing its pipe from the daemon side.
    async fn stop_preview(
        &self,
        #[zbus(header)] hdr: zbus::message::Header<'_>,
    ) -> zbus::fdo::Result<()> {
        let uid = self.caller_uid(&hdr).await?;
        let prefix = format!("pv{uid}-");
        let mut map = self.previews.lock().await;
        let live: Vec<String> = map
            .iter()
            .filter(|(k, _)| k.starts_with(&prefix))
            .map(|(k, _)| k.clone())
            .collect();
        for k in live {
            if let Some(c) = map.remove(&k) {
                c.store(true, Ordering::Relaxed);
            }
        }
        Ok(())
    }
}

fn make_pipe2() -> std::io::Result<(OwnedFd, OwnedFd)> {
    use nix::fcntl::OFlag;
    use nix::unistd::pipe2;
    let (r, w) = pipe2(OFlag::O_CLOEXEC | OFlag::O_NONBLOCK)
        .map_err(|e| std::io::Error::from_raw_os_error(e as i32))?;
    Ok((r, w))
}

/// Pump ScanState updates from the engine onto the bus.
pub async fn run_signal_pump(conn: Connection, mut rx: mpsc::Receiver<(ScanState, f32, String)>) {
    let ctxt = match SignalContext::new(&conn, "/org/faceidnim/Daemon1") {
        Ok(c) => c,
        Err(e) => {
            tracing::error!("signal context: {e}");
            return;
        }
    };
    while let Some((state, progress, reason)) = rx.recv().await {
        if let Err(e) = Daemon1::scan_state(&ctxt, state.as_str(), progress as f64, &reason).await {
            tracing::debug!("scan_state emit failed: {e}");
        }
    }
}
