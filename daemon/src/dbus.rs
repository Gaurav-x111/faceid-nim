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

use crate::config::Config;
use crate::session::{Engine, ScanState};
use crate::store::{Identity, Store};
use std::collections::HashMap;
use std::io::Write;
use std::os::fd::{AsRawFd, FromRawFd, OwnedFd};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
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
        let cfg = self.cfg.lock().await.clone();
        let mut collected: Vec<Vec<f32>> = Vec::new();
        let mut model_id = String::new();

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
                )
                .await
                .map_err(|e| zbus::fdo::Error::Failed(e.to_string()))?;
            model_id = ev.model_id;
            for e in ev.embeddings {
                // Skip near-duplicates: a template that adds no new
                // information adds no coverage, only attack surface.
                let dup = collected
                    .iter()
                    .any(|t| crate::matcher::cosine_max(&e, std::slice::from_ref(t)) > 0.92);
                if !dup {
                    collected.push(e);
                }
            }
        }
        if collected.is_empty() {
            return Err(zbus::fdo::Error::Failed("no usable frames captured".into()));
        }
        collected.truncate(15);
        let n = collected.len() as u32;
        self.store
            .save(
                uid,
                &Identity {
                    name: identity,
                    model_id,
                    enabled: true,
                    templates: collected,
                },
            )
            .map_err(|e| zbus::fdo::Error::Failed(e.to_string()))?;
        Ok(n)
    }

    /// Run a scan and report the result without unlocking anything.
    /// A direct worker scan on purpose: it must work even when faceid
    /// is disabled or the service list has no "test" entry, and it must
    /// never touch the failure counter or the PAM decision path.
    async fn test_scan(
        &self,
        #[zbus(header)] hdr: zbus::message::Header<'_>,
    ) -> zbus::fdo::Result<(bool, String)> {
        let uid = self.caller_uid(&hdr).await?;
        let cfg = self.cfg.lock().await.clone();
        let ev = self
            .engine
            .worker
            .scan(
                &format!("test-{uid}"),
                cfg.mode.as_str(),
                "off",
                cfg.scan_timeout_ms,
                false,
                None,
                None,
                cfg.ir_camera.clone(),
                None,
            )
            .await;
        match ev {
            Ok(e) => Ok((
                !e.embeddings.is_empty(),
                format!("captured {} embeddings", e.embeddings.len()),
            )),
            Err(e) => Ok((false, e.to_string())),
        }
    }

    async fn get_settings(&self) -> zbus::fdo::Result<String> {
        let cfg = self.cfg.lock().await;
        serde_json::to_string(&*cfg).map_err(|e| zbus::fdo::Error::Failed(e.to_string()))
    }

    async fn set_settings(&self, json: String) -> zbus::fdo::Result<()> {
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
        *cur = incoming;
        cur.save(&self.cfg_path)
            .map_err(|e| zbus::fdo::Error::Failed(e.to_string()))
    }

    async fn diagnostics(&self) -> zbus::fdo::Result<String> {
        let cfg = self.cfg.lock().await.clone();
        let worker_up = self.engine.worker.ping().await;
        Ok(serde_json::json!({
            "enabled": cfg.enabled,
            "worker_reachable": worker_up,
            "strictness": cfg.strictness.as_str(),
            "mode": cfg.mode.as_str(),
            "tau": cfg.tau,
            "vote": format!("{}-of-{}", cfg.vote_k, cfg.vote_n),
            "camera": cfg.camera,
            "ir_camera": cfg.ir_camera,
        })
        .to_string())
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
            loop {
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
                ));
                let result = loop {
                    tokio::select! {
                        jpeg = pv_rx.recv() => {
                            let Some(jpeg) = jpeg else { break Ok(()) as Result<(), anyhow::Error>; };
                            let mut buf = Vec::with_capacity(5 + jpeg.len());
                            buf.push(b'J');
                            buf.extend_from_slice(&(jpeg.len() as u32).to_be_bytes());
                            buf.extend_from_slice(&jpeg);
                            if writer.write_all(&buf).is_err() { break Ok(()) as Result<(), anyhow::Error>; }
                        }
                        g = gd_rx.recv() => {
                            let Some((st, r)) = g else { continue; };
                            let line = format!("{st}|{r}");
                            let mut buf = vec![b'G'];
                            buf.extend_from_slice(&(line.len() as u32).to_be_bytes());
                            buf.extend_from_slice(line.as_bytes());
                            if writer.write_all(&buf).is_err() { break Ok(()) as Result<(), anyhow::Error>; }
                        }
                        res = &mut scan => {
                            break match res {
                                Ok(_) => Ok(()),
                                Err(e) => Err(e),
                            };
                        }
                    }
                };

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
