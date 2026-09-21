//! faceid-nimd: the privileged face-unlock daemon.
//!
//! Runs as root because it must authenticate you before your session
//! exists, which also means it cannot keep anything under $HOME.
//!
//! Three sockets, three trust levels:
//!   /run/faceid-nim/auth.sock    any local user may ask (uid-checked)
//!   /run/faceid-nim/vision.sock  the unprivileged worker
//!   system bus                   the app and the shell pill
//!
//! Nothing in this process ever sends a camera frame or an embedding
//! to the UI, and nothing in the UI can influence the decision.

mod audit;
mod config;
mod dbus;
mod enroll;
mod ipc;
mod matcher;
mod policy;
mod session;
mod store;
mod worker;

use anyhow::{Context, Result};
use config::Config;
use session::{Engine, ScanState};
use std::path::{Path, PathBuf};
use std::sync::Arc;
use tokio::sync::{mpsc, Mutex};

const CONFIG_PATH: &str = "/etc/faceid-nim/config.toml";

#[tokio::main]
async fn main() -> Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_env("FACEID_LOG")
                .unwrap_or_else(|_| "info".into()),
        )
        .init();

    let cfg_path =
        PathBuf::from(std::env::var("FACEID_CONFIG").unwrap_or_else(|_| CONFIG_PATH.into()));
    let mut cfg = Config::load(&cfg_path);
    cfg.validate();
    tracing::info!(
        "starting faceid-nimd (enabled={}, strictness={})",
        cfg.enabled,
        cfg.strictness.as_str()
    );

    if !nix::unistd::geteuid().is_root() {
        tracing::warn!(
            "not running as root: template storage and PAM \
                        integration will not work"
        );
    }

    let store = Arc::new(store::Store::open(&cfg.data_dir).context("open template store")?);
    let audit = Arc::new(audit::Audit::new(&cfg.audit_log));
    let worker = Arc::new(worker::Worker::new(cfg.worker_socket.clone()));
    let auth_sock = cfg.auth_socket.clone();
    let cfg = Arc::new(Mutex::new(cfg));

    let (state_tx, state_rx) = mpsc::channel::<(ScanState, f32, String)>(64);
    let engine = Arc::new(Engine {
        cfg: cfg.clone(),
        store: store.clone(),
        worker: worker.clone(),
        policy: Arc::new(Mutex::new(policy::Policy::default())),
        audit: audit.clone(),
        state_tx,
    });

    // D-Bus is best-effort: if the bus is unavailable the pill and the
    // settings app stop working, but face unlock itself must not.
    // Build the connection first, then register the object on it --
    // Daemon1 needs the connection to ask the bus who a caller is.
    match connect_bus(&cfg, &cfg_path, &store, &engine, &worker).await {
        Ok(conn) => {
            tracing::info!("claimed org.faceidnim.Daemon1 on the system bus");
            tokio::spawn(dbus::run_signal_pump(conn, state_rx));
        }
        Err(e) => {
            tracing::error!("D-Bus unavailable: {e}; continuing without the UI");
            tokio::spawn(drain(state_rx));
        }
    }

    let sig = tokio::spawn(async {
        let mut term =
            tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate()).unwrap();
        tokio::select! {
            _ = term.recv() => {}
            _ = tokio::signal::ctrl_c() => {}
        }
        tracing::info!("shutting down");
    });

    tokio::select! {
        r = ipc::serve(&auth_sock, engine.clone()) => {
            if let Err(e) = r { tracing::error!("auth socket failed: {e}"); }
        }
        _ = sig => {}
    }

    let _ = std::fs::remove_file(&auth_sock);
    Ok(())
}

async fn connect_bus(
    cfg: &Arc<Mutex<Config>>,
    cfg_path: &Path,
    store: &Arc<store::Store>,
    engine: &Arc<Engine>,
    worker: &Arc<worker::Worker>,
) -> Result<zbus::Connection> {
    let conn = zbus::connection::Builder::system()?
        .name("org.faceidnim.Daemon1")?
        .build()
        .await?;
    conn.object_server()
        .at(
            "/org/faceidnim/Daemon1",
            dbus::Daemon1::new(
                cfg.clone(),
                cfg_path.to_path_buf(),
                store.clone(),
                engine.clone(),
                conn.clone(),
            ),
        )
        .await?;
    conn.object_server()
        .at(
            "/org/faceidnim/Enrollment1",
            enroll::Enrollment1::new(cfg.clone(), store.clone(), worker.clone(), conn.clone()),
        )
        .await?;
    Ok(conn)
}

async fn drain(mut rx: mpsc::Receiver<(ScanState, f32, String)>) {
    while rx.recv().await.is_some() {}
}
