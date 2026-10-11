//! The PAM-facing auth socket.
//!
//! Line protocol, so the PAM module in C stays tiny:
//!   client -> {"op":"auth","user":"zang","service":"sudo"}
//!   daemon -> {"kind":"info","text":"Looking for your face"}
//!   daemon -> {"kind":"ok"} | {"kind":"denied","text":...} | {"kind":"unavail","text":...}
//!
//! Two things make this safe to expose to an unprivileged caller:
//! SO_PEERCRED tells us who is really on the other end, and the
//! username is resolved to a uid here rather than trusted as sent.

use crate::session::Engine;
use anyhow::Result;
use serde::{Deserialize, Serialize};
use std::io;
use std::os::unix::io::AsRawFd;
use std::path::Path;
use std::sync::Arc;
use std::time::{Duration, Instant};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::{unix::OwnedReadHalf, UnixListener, UnixStream};

const MAX_LINE: usize = 8 * 1024;
const MAX_CONNECTIONS: usize = 64;
/// Idle-read budget for one auth request line (audit BLOCKER 2): well
/// under the PAM module's 8 s default deadline, so a client that
/// connects and sends nothing (slow-loris) is reaped and its
/// connection permit freed instead of wedging one of the 64 slots
/// forever. Sized to leave room for the scan itself after the line
/// arrives; the PAM side still enforces its own overall deadline.
const IDLE_READ_TIMEOUT: Duration = Duration::from_secs(4);

#[derive(Debug, Deserialize)]
struct Request {
    op: String,
    #[serde(default)]
    user: String,
    #[serde(default)]
    service: String,
}

#[derive(Debug, Serialize)]
struct Reply<'a> {
    kind: &'a str,
    #[serde(skip_serializing_if = "str::is_empty")]
    text: &'a str,
}

pub async fn serve(path: &Path, engine: Arc<Engine>) -> Result<()> {
    if let Some(d) = path.parent() {
        std::fs::create_dir_all(d)?;
    }
    if path.exists() {
        std::fs::remove_file(path)?;
    }
    let listener = UnixListener::bind(path)?;
    // World-writable on purpose: any local user may *ask*, and every
    // request is then authorised by uid. Restricting the socket instead
    // would break sudo for ordinary users.
    std::fs::set_permissions(
        path,
        <std::fs::Permissions as std::os::unix::fs::PermissionsExt>::from_mode(0o666),
    )?;
    tracing::info!("auth socket listening on {}", path.display());

    let connections = Arc::new(tokio::sync::Semaphore::new(MAX_CONNECTIONS));
    loop {
        let (stream, _) = listener.accept().await?;
        let permit = match connections.clone().acquire_owned().await {
            Ok(p) => p,
            Err(_) => return Ok(()),
        };
        let engine = engine.clone();
        // Snapshot occupancy for the slow-loris log line in handle():
        // permits held right now (this connection included).
        let in_use = MAX_CONNECTIONS.saturating_sub(connections.available_permits());
        tokio::spawn(async move {
            let _permit = permit;
            if let Err(e) = handle(stream, engine, in_use).await {
                tracing::debug!("auth session ended: {e}");
            }
        });
    }
}

fn peer_uid(stream: &UnixStream) -> Option<u32> {
    use nix::sys::socket::{getsockopt, sockopt::PeerCredentials};
    let fd = stream.as_raw_fd();
    // SAFETY: borrowed fd lives as long as `stream`.
    let borrowed = unsafe { std::os::fd::BorrowedFd::borrow_raw(fd) };
    getsockopt(&borrowed, PeerCredentials).ok().map(|c| c.uid())
}

fn uid_for_user(name: &str) -> Option<u32> {
    use nix::unistd::User;
    User::from_name(name).ok().flatten().map(|u| u.uid.as_raw())
}

async fn read_line_limited(rd: &mut OwnedReadHalf) -> io::Result<Option<Vec<u8>>> {
    let mut line = Vec::new();
    let mut chunk = [0u8; 1024];
    loop {
        let n = rd.read(&mut chunk).await?;
        if n == 0 {
            return if line.is_empty() {
                Ok(None)
            } else {
                Err(io::Error::new(
                    io::ErrorKind::UnexpectedEof,
                    "unterminated request line",
                ))
            };
        }
        if let Some(i) = chunk[..n].iter().position(|&b| b == b'\n') {
            if line.len() + i + 1 > MAX_LINE {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidData,
                    "request line too long",
                ));
            }
            line.extend_from_slice(&chunk[..=i]);
            return Ok(Some(line));
        }
        if line.len() + n > MAX_LINE {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "request line too long",
            ));
        }
        line.extend_from_slice(&chunk[..n]);
    }
}

async fn handle(stream: UnixStream, engine: Arc<Engine>, in_use: usize) -> Result<()> {
    let caller = peer_uid(&stream);
    let connected_at = Instant::now();
    let (mut rd, mut wr) = stream.into_split();

    async fn reply(
        wr: &mut tokio::net::unix::OwnedWriteHalf,
        kind: &str,
        text: &str,
    ) -> Result<()> {
        let line = serde_json::to_string(&Reply { kind, text })?;
        wr.write_all(line.as_bytes()).await?;
        wr.write_all(b"\n").await?;
        wr.flush().await?;
        Ok(())
    }

    let line = match tokio::time::timeout(IDLE_READ_TIMEOUT, read_line_limited(&mut rd)).await {
        // Slow-loris: connected but never completed a request line.
        // Close without replying; dropping the stream frees the permit.
        Err(_) => {
            tracing::info!(
                "auth idle timeout: caller={:?} held {}/{} connections for {:?}; \
                 connection closed, permit freed",
                caller,
                in_use,
                MAX_CONNECTIONS,
                connected_at.elapsed(),
            );
            return Ok(());
        }
        Ok(Ok(Some(line))) => line,
        Ok(Ok(None)) => return Ok(()),
        Ok(Err(e)) => {
            let _ = reply(&mut wr, "unavail", "bad request").await;
            tracing::debug!("auth request rejected: {e}");
            return Ok(());
        }
    };
    let Ok(req) = serde_json::from_slice::<Request>(&line) else {
        reply(&mut wr, "unavail", "bad request").await?;
        return Ok(());
    };
    if req.op != "auth" {
        reply(&mut wr, "unavail", "unknown op").await?;
        return Ok(());
    }
    let Some(target_uid) = uid_for_user(&req.user) else {
        reply(&mut wr, "unavail", "unknown user").await?;
        return Ok(());
    };
    // A caller may only request a face unlock for itself, unless it
    // is root (which is how gdm and sudo legitimately ask).
    match caller {
        Some(c) if c == 0 || c == target_uid => {}
        Some(c) => {
            tracing::warn!("uid {c} tried to authenticate uid {target_uid}");
            reply(&mut wr, "denied", "not permitted").await?;
            return Ok(());
        }
        None => {
            reply(&mut wr, "unavail", "no peer credentials").await?;
            return Ok(());
        }
    }

    let service = if req.service.is_empty() {
        "unknown"
    } else {
        &req.service
    };
    reply(&mut wr, "info", "Looking for your face").await?;
    let out = engine.authenticate(target_uid, service).await;
    if out.success {
        reply(&mut wr, "ok", "").await?;
    } else if out.unavailable {
        reply(&mut wr, "unavail", &out.message).await?;
    } else {
        reply(&mut wr, "denied", &out.message).await?;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::audit::Audit;
    use crate::config::Config;
    use crate::policy::Policy;
    use crate::store::Store;
    use crate::worker::Worker;
    use std::collections::HashMap;
    use tokio::sync::{mpsc, Mutex, Semaphore};

    #[tokio::test]
    async fn reads_a_bounded_line() {
        let (mut client, server) = UnixStream::pair().unwrap();
        let writer = tokio::spawn(async move {
            client.write_all(b"{\"op\":\"ping\"}\n").await.unwrap();
        });
        let (mut rd, _wr) = server.into_split();
        let line = read_line_limited(&mut rd).await.unwrap().unwrap();
        assert_eq!(line, b"{\"op\":\"ping\"}\n");
        writer.await.unwrap();
    }

    #[tokio::test]
    async fn rejects_an_oversized_line() {
        let (mut client, server) = UnixStream::pair().unwrap();
        let writer = tokio::spawn(async move {
            client.write_all(&vec![b'x'; MAX_LINE + 1]).await.unwrap();
        });
        let (mut rd, _wr) = server.into_split();
        let err = read_line_limited(&mut rd).await.unwrap_err();
        assert_eq!(err.kind(), io::ErrorKind::InvalidData);
        writer.await.unwrap();
    }

    /// Audit BLOCKER 2 verification: a client that connects and sends
    /// nothing must be reaped within IDLE_READ_TIMEOUT (permit freed,
    /// no hang), not held forever. The worker socket is bogus on
    /// purpose: authenticate() must never run on this path.
    #[tokio::test]
    async fn idle_connection_is_closed_within_timeout() {
        let dir = std::env::temp_dir().join(format!("faceid-idle-test-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let (state_tx, _state_rx) = mpsc::channel(4);
        let engine = Arc::new(Engine {
            cfg: Arc::new(Mutex::new(Config::default())),
            store: Arc::new(Store::open(&dir.join("store")).unwrap()),
            worker: Arc::new(Worker::new(dir.join("no-worker.sock"))),
            policy: Arc::new(Mutex::new(Policy::default())),
            audit: Arc::new(Audit::new(&dir.join("audit.log"))),
            state_tx,
            scan_slots: Arc::new(Semaphore::new(2)),
            auth_locks: Arc::new(Mutex::new(HashMap::new())),
        });

        let (mut client, server) = UnixStream::pair().unwrap();
        let start = Instant::now();
        handle(server, engine, 1).await.expect("handle returns Ok");
        let held = start.elapsed();
        assert!(
            held >= IDLE_READ_TIMEOUT,
            "must wait out the idle budget, held only {held:?}"
        );
        assert!(
            held < IDLE_READ_TIMEOUT + Duration::from_secs(5),
            "must not hang past the budget, held {held:?}"
        );
        // Server closed its end: the client must observe EOF.
        let mut buf = [0u8; 8];
        let n = client.read(&mut buf).await.expect("client read");
        assert_eq!(n, 0, "idle connection must be closed (EOF), not left open");
        let _ = std::fs::remove_dir_all(&dir);
    }
}
