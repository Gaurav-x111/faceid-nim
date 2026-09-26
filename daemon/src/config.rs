//! Configuration, read from /etc/faceid-nim/config.toml.
//!
//! Every field has a defensible default, because a daemon that refuses
//! to start on a malformed config is a daemon that can lock you out.
//! Parse failures are logged and defaults are used.

use serde::{Deserialize, Serialize};
use std::path::PathBuf;
use std::sync::atomic::{AtomicU64, Ordering};

static SAVE_SEQUENCE: AtomicU64 = AtomicU64::new(0);

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct Config {
    pub enabled: bool,
    pub strictness: Strictness,
    /// Recognition threshold. There is no sensible universal default:
    /// measure it with eval/far_frr.py on your own camera.
    pub tau: f32,
    pub vote_k: usize,
    pub vote_n: usize,
    pub scan_timeout_ms: u64,
    pub max_failures: u32,
    pub lockout_secs: u64,
    /// Primary (colour) camera node. `rgb_device` is accepted as an
    /// alias in the config file; `camera` stays the canonical key so
    /// existing deployments and save() keep working.
    #[serde(alias = "rgb_device")]
    pub camera: String,
    #[serde(alias = "ir_device")]
    pub ir_camera: Option<String>,
    pub mode: CameraMode,
    pub require_attention: bool,
    /// PAM services allowed to ask for a face unlock. Anything not on
    /// this list is refused before the camera is ever opened.
    pub allowed_services: Vec<String>,
    pub data_dir: PathBuf,
    pub worker_socket: PathBuf,
    pub auth_socket: PathBuf,
    pub audit_log: PathBuf,
    /// Opt-in lock/unlock timeline (JSON, no sudo/polkit entries).
    /// Off by default: nothing extra is written until the user ticks
    /// the checkbox in the app.
    #[serde(default)]
    pub timeline_enabled: bool,
    #[serde(default = "default_timeline_log")]
    pub timeline_log: PathBuf,
    /// Users never offered face unlock (e.g. ["guest"]). They fall
    /// through to password immediately, before the camera opens.
    /// Empty by default: no one is excluded.
    #[serde(default)]
    pub exclude_users: Vec<String>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Strictness {
    Off,
    Light,
    Heavy,
}

impl Strictness {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Off => "off",
            Self::Light => "light",
            Self::Heavy => "heavy",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum CameraMode {
    Rgb,
    Ir,
    Both,
    /// Explicit alias for "RGB matches, IR liveness": recognition runs
    /// on the colour camera, spoof cues on the infrared one. Behaviours
    /// as `both` (RGB-primary) at the worker.
    Hybrid,
    /// Per-scan room-light decision in the worker: dark -> IR primary,
    /// lit -> RGB primary. The worker reports the resolved spectrum in
    /// `ev_done.spectrum` so voting can gate templates by spectrum.
    Auto,
}

impl CameraMode {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Rgb => "rgb",
            Self::Ir => "ir",
            Self::Both => "both",
            Self::Hybrid => "hybrid",
            Self::Auto => "auto",
        }
    }
}

fn default_timeline_log() -> PathBuf {
    PathBuf::from("/var/log/faceid-nim/timeline.jsonl")
}

impl Default for Config {
    fn default() -> Self {
        Self {
            // Off until the user turns it on, deliberately. Installing
            // the package must never change how the machine
            // authenticates.
            enabled: false,
            strictness: Strictness::Light,
            tau: 0.40,
            vote_k: 3,
            vote_n: 5,
            // Short, because with PAM the password box is typically
            // unusable until the face attempt finishes.
            scan_timeout_ms: 4000,
            max_failures: 5,
            lockout_secs: 60,
            camera: "/dev/video0".into(),
            ir_camera: None,
            mode: CameraMode::Rgb,
            require_attention: true,
            allowed_services: vec![
                "gdm-password".into(),
                "sudo".into(),
                "polkit-1".into(),
                "login".into(),
                "gnome-screensaver".into(),
            ],
            data_dir: PathBuf::from("/var/lib/faceid-nim"),
            worker_socket: PathBuf::from("/run/faceid-nim/worker/vision.sock"),
            auth_socket: PathBuf::from("/run/faceid-nim/auth.sock"),
            audit_log: PathBuf::from("/var/log/faceid-nim/audit.log"),
            timeline_enabled: false,
            timeline_log: default_timeline_log(),
            exclude_users: Vec::new(),
        }
    }
}

impl Config {
    pub fn load(path: &std::path::Path) -> Self {
        match std::fs::read_to_string(path) {
            Ok(text) => match toml::from_str::<Config>(&text) {
                Ok(c) => c,
                Err(e) => {
                    tracing::error!(
                        "config parse error in {}: {e}; using defaults",
                        path.display()
                    );
                    Config::default()
                }
            },
            Err(_) => {
                tracing::info!("no config at {}; using defaults", path.display());
                Config::default()
            }
        }
    }

    pub fn save(&self, path: &std::path::Path) -> anyhow::Result<()> {
        if let Some(dir) = path.parent() {
            std::fs::create_dir_all(dir)?;
        }
        let tmp = path.with_extension(format!(
            "toml.tmp.{}.{}",
            std::process::id(),
            SAVE_SEQUENCE.fetch_add(1, Ordering::Relaxed)
        ));
        let result = (|| -> anyhow::Result<()> {
            std::fs::write(&tmp, toml::to_string_pretty(self)?)?;
            Ok(std::fs::rename(&tmp, path)?)
        })();
        if result.is_err() {
            let _ = std::fs::remove_file(&tmp);
        }
        result
    }

    pub fn service_allowed(&self, service: &str) -> bool {
        self.allowed_services.iter().any(|s| s == service)
    }

    /// Fail closed on a nonsense threshold rather than authenticating
    /// everyone who walks past.
    pub fn validate(&mut self) {
        if !(0.0..=1.0).contains(&self.tau) {
            tracing::error!(
                "tau {} out of range; clamping to 0.9 (fail closed)",
                self.tau
            );
            self.tau = 0.9;
        }
        if self.vote_k == 0 {
            self.vote_k = 1;
        }
        if self.vote_n < self.vote_k {
            self.vote_n = self.vote_k;
        }
        self.scan_timeout_ms = self.scan_timeout_ms.clamp(500, 15_000);
        self.max_failures = self.max_failures.clamp(1, 20);
        self.lockout_secs = self.lockout_secs.clamp(10, 3600);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;

    fn parse(text: &str) -> Config {
        use std::sync::atomic::{AtomicU64, Ordering};
        static SEQ: AtomicU64 = AtomicU64::new(0);
        let mut p = std::env::temp_dir();
        p.push(format!(
            "faceid-cfg-test-{}-{}.toml",
            std::process::id(),
            SEQ.fetch_add(1, Ordering::Relaxed)
        ));
        let mut f = std::fs::File::create(&p).unwrap();
        f.write_all(text.as_bytes()).unwrap();
        let c = Config::load(&p);
        let _ = std::fs::remove_file(&p);
        c
    }

    #[test]
    fn hybrid_mode_parses() {
        let c = parse("mode = \"hybrid\"\n");
        assert_eq!(c.mode, CameraMode::Hybrid);
        assert_eq!(c.mode.as_str(), "hybrid");
    }

    #[test]
    fn rgb_ir_device_aliases_parse() {
        // The new key spelling from the hybrid-mode docs...
        let c = parse(concat!(
            "mode = \"hybrid\"\n",
            "rgb_device = \"/dev/video1\"\n",
            "ir_device = \"/dev/video3\"\n"
        ));
        assert_eq!(c.mode, CameraMode::Hybrid);
        assert_eq!(c.camera, "/dev/video1");
        assert_eq!(c.ir_camera.as_deref(), Some("/dev/video3"));
    }

    #[test]
    fn legacy_keys_still_parse() {
        // ...and the original spelling keeps working, so an untouched
        // installed config never silently changes behaviour.
        let c = parse(concat!(
            "camera = \"/dev/video0\"\n",
            "ir_camera = \"/dev/video2\"\n"
        ));
        assert_eq!(c.camera, "/dev/video0");
        assert_eq!(c.ir_camera.as_deref(), Some("/dev/video2"));
        assert_eq!(c.mode, CameraMode::Rgb); // default unchanged
    }

    #[test]
    fn save_roundtrips_hybrid() {
        let c = Config {
            mode: CameraMode::Hybrid,
            camera: "/dev/video1".into(),
            ir_camera: Some("/dev/video3".into()),
            ..Config::default()
        };
        let mut p = std::env::temp_dir();
        p.push(format!("faceid-cfg-save-{}.toml", std::process::id()));
        assert!(c.save(&p).is_ok());
        let back = parse(&std::fs::read_to_string(&p).unwrap());
        let _ = std::fs::remove_file(&p);
        assert_eq!(back.mode, CameraMode::Hybrid);
        assert_eq!(back.camera, "/dev/video1");
        assert_eq!(back.ir_camera.as_deref(), Some("/dev/video3"));
    }

    #[test]
    fn auto_mode_parses() {
        let c = parse("mode = \"auto\"\n");
        assert_eq!(c.mode, CameraMode::Auto);
        assert_eq!(c.mode.as_str(), "auto");
    }

    /// The exact config postinst generates on a fresh install must parse
    /// and keep its auto-detected cameras.
    ///
    /// This test exists because `Config::load` degrades to ALL defaults
    /// on a parse error, silently. A malformed generated config would
    /// therefore not look like a broken install -- it would quietly put
    /// `camera` back to /dev/video0 and `mode` back to rgb, and the user
    /// would be told the auto-detection worked. `ir_camera` is absent
    /// here on purpose: that is what a machine with no IR sensor gets,
    /// and it must stay absent rather than becoming a bogus node.
    #[test]
    fn generated_postinst_config_keeps_its_values() {
        let c = parse(concat!(
            "enabled = false\n",
            "strictness = \"light\"\n",
            "mode = \"auto\"\n",
            "tau = 0.40\n",
            "vote_k = 3\n",
            "vote_n = 5\n",
            "scan_timeout_ms = 4000\n",
            "max_failures = 5\n",
            "lockout_secs = 60\n",
            "require_attention = true\n",
            "camera = \"/dev/video1\"\n",
            "ir_camera = \"/dev/video3\"\n",
            "worker_socket = \"/run/faceid-nim/worker/vision.sock\"\n",
            "auth_socket = \"/run/faceid-nim/auth.sock\"\n",
        ));
        assert_eq!(c.mode, CameraMode::Auto, "mode silently fell back");
        assert_eq!(c.camera, "/dev/video1", "camera silently fell back");
        assert_eq!(c.ir_camera.as_deref(), Some("/dev/video3"));
        assert!(!c.enabled, "install must never enable face unlock");
    }

    #[test]
    fn generated_config_without_ir_sensor_keeps_rgb_camera() {
        let c = parse(concat!(
            "enabled = false\n",
            "mode = \"auto\"\n",
            "camera = \"/dev/video0\"\n",
        ));
        assert_eq!(c.camera, "/dev/video0", "camera silently fell back");
        assert_eq!(c.mode, CameraMode::Auto);
        assert_eq!(c.ir_camera, None);
    }
}
