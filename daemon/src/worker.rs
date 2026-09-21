//! Talking to the vision worker, and keeping it alive.
//!
//! The worker owns the camera and runs unprivileged. It returns query
//! embeddings and liveness cues. It never receives templates, and its
//! answer is evidence, not a decision.

use anyhow::{anyhow, Context, Result};
use base64::Engine as _;
use serde::Deserialize;
use std::path::PathBuf;
use std::process::Stdio;
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::net::UnixStream;
use tokio::sync::mpsc;
use tokio::time::{timeout, Duration};

#[derive(Debug, Deserialize, Default, Clone)]
pub struct Liveness {
    #[serde(default)]
    pub deny: Vec<String>,
    #[serde(default)]
    pub confirm: Vec<String>,
    #[serde(default)]
    pub attention_ok: bool,
    // The raw similarity figure is deliberately never read here: audit
    // records events, never scores (see audit.rs). Kept because the
    // worker sends it and future tuning may want it.
    #[allow(dead_code)]
    #[serde(default)]
    pub score: f32,
    // Measured liveness diagnostics from the worker, e.g.
    // moire_score / moire_threshold / moire_window for audit. These are
    // *measurements*, never biometric templates: they describe the cue,
    // not the face, so carrying them into the audit is privacy-safe.
    #[serde(default)]
    pub notes: serde_json::Map<String, serde_json::Value>,
}

#[derive(Debug, Deserialize, Default)]
pub struct DoneStats {
    #[serde(default)]
    pub frames: u64,
    #[serde(default)]
    pub usable: u64,
    #[serde(default)]
    pub elapsed_ms: u64,
}

#[derive(Debug, Deserialize)]
#[serde(tag = "ev")]
enum Event {
    #[serde(rename = "face_found")]
    FaceFound,
    #[serde(rename = "progress")]
    Progress { p: f32 },
    #[serde(rename = "challenge")]
    Challenge { prompt: String },
    #[serde(rename = "done")]
    Done {
        embeddings: Vec<Vec<f32>>,
        #[serde(default)]
        model_id: String,
        #[serde(default)]
        liveness: Liveness,
        #[serde(default)]
        stats: Option<DoneStats>,
    },
    #[serde(rename = "error")]
    Error { reason: String },
    #[serde(rename = "capabilities")]
    Capabilities {
        // Received for completeness; the daemon never makes decisions
        // from the worker's capabilities list, so these are unread by
        // design.
        #[allow(dead_code)]
        #[serde(default)]
        model_id: String,
        #[allow(dead_code)]
        #[serde(default)]
        mesh: bool,
    },
    #[serde(rename = "pong")]
    Pong,
    #[serde(rename = "preview")]
    Preview { jpeg: String },
    #[serde(rename = "pose_status")]
    PoseStatus {
        // Pose guidance strings for the enrollment UI. The daemon
        // forwards status + reason verbatim; turning a reason like
        // "face too small" into a copy line ("Move closer") is the
        // app's business.
        #[serde(default)]
        status: String,
        #[serde(default)]
        reason: String,
    },
    #[serde(other)]
    Unknown,
}

#[derive(Debug, Clone)]
pub enum Progress {
    FaceFound,
    Value(f32),
    Challenge(String),
}

pub struct ScanEvidence {
    pub embeddings: Vec<Vec<f32>>,
    pub model_id: String,
    pub liveness: Liveness,
    /// Usable (quality-passing) frames, from the worker's done stats.
    pub usable: u64,
}

pub struct Worker {
    socket: PathBuf,
}

impl Worker {
    pub fn new(socket: PathBuf) -> Self {
        Self { socket }
    }

    /// Run one scan. Progress events are forwarded to `tx` so the
    /// animation can follow along; the animation never gets more than
    /// a state name and a number. When `preview_tx` is given, the
    /// worker is asked to stream enrollment preview frames, which are
    /// decoded and forwarded as raw JPEG bytes. When `guide_tx` is
    /// given, pose_status lines (status + human reason) are forwarded
    /// so the app can render alignment guidance.
    #[allow(clippy::too_many_arguments)]
    pub async fn scan(
        &self,
        id: &str,
        mode: &str,
        strict: &str,
        timeout_ms: u64,
        challenge: bool,
        tx: Option<mpsc::Sender<Progress>>,
        preview_tx: Option<mpsc::Sender<Vec<u8>>>,
        ir_device: Option<String>,
        guide_tx: Option<mpsc::Sender<(String, String)>>,
    ) -> Result<ScanEvidence> {
        let mut stream = UnixStream::connect(&self.socket)
            .await
            .with_context(|| format!("connect worker at {}", self.socket.display()))?;
        let mut req = serde_json::json!({
            "op": "scan", "id": id, "mode": mode,
            "timeout_ms": timeout_ms, "strict": strict, "challenge": challenge,
            "preview": preview_tx.is_some(),
        });
        if let Some(dev) = ir_device {
            req["ir_device"] = dev.into();
        }
        stream.write_all(format!("{req}\n").as_bytes()).await?;
        stream.flush().await?;

        let (rd, _wr) = stream.into_split();
        let mut lines = BufReader::new(rd).lines();
        // Generous slack over the worker's own timeout, so a hung
        // worker cannot hang the PAM stack indefinitely.
        let budget = Duration::from_millis(timeout_ms + 3000);

        loop {
            let line = match timeout(budget, lines.next_line()).await {
                Err(_) => return Err(anyhow!("worker timed out")),
                Ok(Ok(Some(l))) => l,
                Ok(Ok(None)) => return Err(anyhow!("worker closed the connection")),
                Ok(Err(e)) => return Err(anyhow!("worker read error: {e}")),
            };
            let ev: Event = match serde_json::from_str(&line) {
                Ok(e) => e,
                Err(e) => {
                    tracing::warn!("unparsable worker line: {e}");
                    continue;
                }
            };
            match ev {
                Event::FaceFound => {
                    if let Some(t) = &tx {
                        let _ = t.send(Progress::FaceFound).await;
                    }
                }
                Event::Progress { p } => {
                    if let Some(t) = &tx {
                        let _ = t.send(Progress::Value(p)).await;
                    }
                }
                Event::Challenge { prompt } => {
                    if let Some(t) = &tx {
                        let _ = t.send(Progress::Challenge(prompt)).await;
                    }
                }
                Event::Done {
                    embeddings,
                    model_id,
                    liveness,
                    stats,
                } => {
                    return Ok(ScanEvidence {
                        embeddings,
                        model_id,
                        liveness,
                        usable: stats.map(|s| s.usable).unwrap_or(0),
                    })
                }
                Event::Error { reason } => return Err(anyhow!("worker: {reason}")),
                Event::Preview { jpeg } => {
                    if let Some(t) = &preview_tx {
                        // Best-effort: a preview decode failure must not
                        // fail the scan, and a dropped pipe just means
                        // the app stopped reading.
                        use base64::engine::general_purpose::STANDARD as B64;
                        if let Ok(bytes) = B64.decode(jpeg) {
                            let _ = t.send(bytes).await;
                        }
                    }
                }
                Event::PoseStatus { status, reason } => {
                    if let Some(t) = &guide_tx {
                        let _ = t.send((status, reason)).await;
                    }
                }
                _ => {}
            }
        }
    }

    pub async fn ping(&self) -> bool {
        let Ok(mut s) = UnixStream::connect(&self.socket).await else {
            return false;
        };
        if s.write_all(b"{\"op\":\"ping\",\"id\":\"p\"}\n")
            .await
            .is_err()
        {
            return false;
        }
        let (rd, _w) = s.into_split();
        let mut lines = BufReader::new(rd).lines();
        matches!(
            timeout(Duration::from_millis(800), lines.next_line()).await,
            Ok(Ok(Some(_)))
        )
    }
}

/// Supervise the worker process, restarting it with backoff.
///
/// A crashed worker must degrade to "password only", never to "any
/// face works", so callers treat an unreachable worker as
/// PAM_AUTHINFO_UNAVAIL. Not wired into `main` because the packaged
/// deployment restarts the worker from its systemd unit instead; kept
/// for dev runs and as the documented fallback.
#[allow(dead_code)]
pub async fn supervise(cmd: Vec<String>) {
    let mut backoff = Duration::from_millis(500);
    loop {
        let mut c = tokio::process::Command::new(&cmd[0]);
        c.args(&cmd[1..]).stdin(Stdio::null());
        match c.spawn() {
            Ok(mut child) => {
                backoff = Duration::from_millis(500);
                let status = child.wait().await;
                tracing::warn!("vision worker exited: {status:?}");
            }
            Err(e) => tracing::error!("cannot spawn vision worker: {e}"),
        }
        tokio::time::sleep(backoff).await;
        backoff = (backoff * 2).min(Duration::from_secs(30));
    }
}
