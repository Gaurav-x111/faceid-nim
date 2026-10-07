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
    // Which sensor the scan actually ran on, as resolved by the worker:
    // "rgb", "ir", or "" for a legacy worker that does not say. This is
    // a hardware fact, not a biometric, and it is the one field that
    // makes auto mode debuggable: "it did not unlock in the dark" is
    // unanswerable without knowing whether the IR sensor was chosen.
    // In auto mode it is also how a miscalibrated brightness gate shows
    // up -- a room logged as "ir" at 09:00 in daylight.
    #[serde(skip_serializing_if = "str::is_empty")]
    pub spectrum: &'a str,
    // Measured room light behind an auto-mode decision, when the worker
    // reported one. Absent for a fixed rgb/ir mode.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub luma: Option<f32>,
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

#[derive(Debug, Serialize)]
pub struct TimelineEvent<'a> {
    pub ts: u64,
    pub uid: u32,
    pub service: &'a str,
    pub result: &'a str,
}

pub struct Audit {
    path: PathBuf,
    timeline_path: std::sync::RwLock<PathBuf>,
    timeline_enabled: std::sync::atomic::AtomicBool,
    /// Lines dropped while the file sat at MAX_AUDIT_BYTES (reset when
    /// logging resumes). Reported to the journal, never to a caller.
    dropped: std::sync::atomic::AtomicU64,
}

/// Hard ceiling on the audit file. Every refused attempt logs a line
/// and refusals complete instantly, so without a writer-side bound one
/// local client can fill the disk between logrotate runs (round-2
/// audit measured ~10k lines/s). Past the ceiling, lines are dropped
/// *with accounting* instead of growing the file: a 50 MB file of
/// refusals is already the signal, and the journal copy (bounded
/// separately by journald) still fires. External rotation shrinks the
/// file and logging resumes on its own. Legitimate use is bytes a day;
/// 50 MB holds on the order of 250k lines.
pub const MAX_AUDIT_BYTES: u64 = 50 * 1024 * 1024;
const _: () = assert!(
    MAX_AUDIT_BYTES >= 1024 * 1024,
    "audit cap must stay generous"
);

/// Append one line unless the file already exceeds `cap`.
/// Ok(true) = written, Ok(false) = at cap (drop with accounting),
/// Err = the write itself failed.
fn append_capped(path: &Path, line: &str, cap: u64) -> std::io::Result<bool> {
    if std::fs::metadata(path)
        .map(|m| m.len() > cap)
        .unwrap_or(false)
    {
        return Ok(false);
    }
    let mut f = OpenOptions::new()
        .create(true)
        .append(true)
        .mode(0o600)
        .open(path)?;
    writeln!(f, "{line}")?;
    Ok(true)
}

impl Audit {
    pub fn new(path: &Path) -> Self {
        if let Some(d) = path.parent() {
            let _ = std::fs::create_dir_all(d);
        }
        Self {
            path: path.to_path_buf(),
            timeline_path: std::sync::RwLock::new(PathBuf::from(
                "/var/log/faceid-nim/timeline.jsonl",
            )),
            timeline_enabled: std::sync::atomic::AtomicBool::new(false),
            dropped: std::sync::atomic::AtomicU64::new(0),
        }
    }

    pub fn configure_timeline(&self, path: &Path, enabled: bool) {
        if let Ok(mut p) = self.timeline_path.write() {
            *p = path.to_path_buf();
        }
        self.timeline_enabled
            .store(enabled, std::sync::atomic::Ordering::Relaxed);
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
        match append_capped(&self.path, &line, MAX_AUDIT_BYTES) {
            Ok(true) => {
                let dropped = self.dropped.swap(0, std::sync::atomic::Ordering::Relaxed);
                if dropped > 0 {
                    tracing::warn!(
                        "audit log resumed after rotation \
                         ({dropped} lines dropped at the cap)"
                    );
                }
            }
            Ok(false) => {
                let n = self
                    .dropped
                    .fetch_add(1, std::sync::atomic::Ordering::Relaxed)
                    + 1;
                if n == 1 || n % 10_000 == 0 {
                    tracing::warn!(
                        "audit log at cap ({MAX_AUDIT_BYTES} bytes); \
                         dropping lines (#{n})"
                    );
                }
            }
            Err(e) => tracing::warn!("audit write failed: {e}"),
        }
        tracing::info!(target: "audit", "{line}");
    }

    /// Opt-in lock/unlock timeline: JSON lines, lock/unlock services
    /// only (gdm-password/login/gnome-screensaver). Sudo/polkit/test
    /// scans are never written here — this is "when did this machine
    /// lock/unlock", not a command history.
    pub fn timeline(&self, uid: u32, service: &str, result: &str) {
        if !self
            .timeline_enabled
            .load(std::sync::atomic::Ordering::Relaxed)
        {
            return;
        }
        const LOCK_SERVICES: &[&str] = &["gdm-password", "login", "gnome-screensaver"];
        if !LOCK_SERVICES.contains(&service) {
            return;
        }
        let ts = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_secs())
            .unwrap_or(0);
        let ev = TimelineEvent {
            ts,
            uid,
            service,
            result,
        };
        let Ok(line) = serde_json::to_string(&ev) else {
            return;
        };
        let path = self
            .timeline_path
            .read()
            .map(|p| p.clone())
            .unwrap_or_else(|_| PathBuf::from("/var/log/faceid-nim/timeline.jsonl"));
        if let Some(d) = path.parent() {
            let _ = std::fs::create_dir_all(d);
        }
        match OpenOptions::new()
            .create(true)
            .append(true)
            .mode(0o600)
            .open(&path)
        {
            Ok(mut f) => {
                let _ = writeln!(f, "{line}");
            }
            Err(e) => tracing::warn!("timeline write failed: {e}"),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ev<'a>(spectrum: &'a str, luma: Option<f32>) -> Event<'a> {
        Event {
            ts: 0,
            uid: 1000,
            service: "sudo",
            result: "success",
            reason: "",
            elapsed_ms: 1200,
            strictness: "light",
            deny_cues: &[],
            spectrum,
            luma,
            moire_score: None,
            moire_threshold: None,
            valid_frames: None,
        }
    }

    fn line(e: &Event<'_>) -> serde_json::Value {
        serde_json::from_str(&serde_json::to_string(e).unwrap()).unwrap()
    }

    /// The whole reason these fields exist: "it did not unlock in the
    /// dark" is unanswerable unless the log says which sensor ran and
    /// how bright the room was measured to be.
    #[test]
    fn the_audit_records_which_sensor_was_used() {
        let v = line(&ev("ir", Some(12.0)));
        assert_eq!(v["spectrum"], "ir");
        assert_eq!(v["luma"], 12.0);
    }

    /// A pre-scan refusal has no evidence, so it must say "unknown"
    /// rather than claiming a sensor was chosen or omitting the key in a
    /// way that reads as "rgb".
    #[test]
    fn an_unreached_decision_says_unknown() {
        let v = line(&ev("unknown", None));
        assert_eq!(v["spectrum"], "unknown");
        assert!(v.get("luma").is_none(), "no measurement, no luma key");
    }

    /// A legacy worker that does not send a spectrum must not be recorded
    /// as an empty or bogus sensor.
    #[test]
    fn an_empty_spectrum_is_omitted_entirely() {
        let v = line(&ev("", None));
        assert!(v.get("spectrum").is_none(), "empty must be skipped");
    }

    /// Still no biometric: the log records hardware and cue
    /// measurements, never a face.
    #[test]
    fn no_biometric_ever_reaches_the_log() {
        let text = serde_json::to_string(&ev("rgb", Some(200.0))).unwrap();
        for forbidden in ["embedding", "score", "cosine", "distance"] {
            assert!(
                !text.contains(forbidden),
                "audit line leaked {forbidden}: {text}"
            );
        }
    }

    /// The disk-exhaustion bound (round-2 audit): past the cap, lines
    /// are dropped instead of growing the file, and logging resumes
    /// after something external (logrotate, an admin) shrinks it.
    #[test]
    fn capped_append_bounds_the_file() {
        let dir = tempfile::tempdir().unwrap();
        let p = dir.path().join("audit.log");
        assert!(append_capped(&p, "one", 100).unwrap());
        assert!(append_capped(&p, "two", 100).unwrap());
        let len = std::fs::metadata(&p).unwrap().len();
        assert!(!append_capped(&p, "three", len - 1).unwrap());
        assert_eq!(std::fs::metadata(&p).unwrap().len(), len);
        // Rotation (truncate) resumes logging.
        std::fs::write(&p, b"").unwrap();
        assert!(append_capped(&p, "four", len - 1).unwrap());
    }
}
