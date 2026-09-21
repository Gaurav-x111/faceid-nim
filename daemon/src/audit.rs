//! Append-only audit log.
//!
//! Records what happened, never the biometric that made it happen: no
//! embeddings, no similarity scores, no frames. "uid 1000 unlocked
//! sudo at 12:04 after 2 failures" is useful forensics. "uid 1000
//! scored 0.417" is a gift to an attacker who can read the log.

use serde::Serialize;
use std::fs::OpenOptions;
use std::io::Write;
use std::os::unix::fs::OpenOptionsExt;
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

#[derive(Debug, Serialize)]
pub struct Event<'a> {
    pub ts: u64,
    pub uid: u32,
    pub service: &'a str,
    pub result: &'a str,
    pub reason: &'a str,
    pub elapsed_ms: u64,
    pub strictness: &'a str,
    pub deny_cues: &'a [String],
    // Cue measurements, not biometrics: the worker's moire prominence
    // score and its deny threshold, plus how many frames were clean
    // enough to judge. These make "why did auth refuse" answerable
    // without ever logging a face.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub moire_score: Option<f32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub moire_threshold: Option<f32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub valid_frames: Option<u64>,
}

pub struct Audit {
    path: PathBuf,
}

impl Audit {
    pub fn new(path: &Path) -> Self {
        if let Some(d) = path.parent() {
            let _ = std::fs::create_dir_all(d);
        }
        Self {
            path: path.to_path_buf(),
        }
    }

    pub fn log(&self, mut ev: Event<'_>) {
        ev.ts = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_secs())
            .unwrap_or(0);
        let Ok(line) = serde_json::to_string(&ev) else {
            return;
        };
        // Logging must never be able to break authentication, so every
        // failure here is swallowed after a trace.
        match OpenOptions::new()
            .create(true)
            .append(true)
            .mode(0o600)
            .open(&self.path)
        {
            Ok(mut f) => {
                let _ = writeln!(f, "{line}");
            }
            Err(e) => tracing::warn!("audit write failed: {e}"),
        }
        tracing::info!(target: "audit", "{line}");
    }
}
