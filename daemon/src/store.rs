//! Encrypted template storage.
//!
//! Layout on disk:
//!   /var/lib/faceid-nim                     root:root 0711 (traverse only)
//!   /var/lib/faceid-nim/models/*.onnx       root:root 0755 (public model files)
//!   /var/lib/faceid-nim/master.key          root:root 0600
//!   /var/lib/faceid-nim/users/<uid>/<id>.tpl  root:root 0600
//!
//! The state root is deliberately 0711, not 0700: the unprivileged
//! worker lives in its own little world and must be able to *traverse*
//! to models/ without ever being able to list the root or read the
//! master key or the template directory (both stay 0700/0600 on their
//! own leaves). Only x for other, never r.
//!
//! File format:
//!   magic "FIDN" | u8 version | u8 name_len | name | u16 dim | u16 count
//!   | 12-byte nonce | AES-256-GCM ciphertext
//!
//! AAD = uid || identity || model_id || version. Authenticated but not
//! secret, so moving a template file to another user or editing the
//! header fails decryption instead of silently working.
//!
//! Be honest about what this buys: with the key on the same disk it
//! protects against someone copying one template file, not against
//! root. Full-disk encryption is the real baseline; TPM2 sealing is
//! the upgrade.

use aes_gcm::aead::{Aead, KeyInit, Payload};
use aes_gcm::{Aes256Gcm, Nonce};
use anyhow::{anyhow, bail, Context, Result};
use rand::RngCore;
use std::fs;
use std::io::Write;
use std::os::unix::fs::{OpenOptionsExt, PermissionsExt};
use std::path::{Path, PathBuf};
use zeroize::Zeroize;

const MAGIC: &[u8; 4] = b"FIDN";
const VERSION: u8 = 1;
const NONCE_LEN: usize = 12;
const MAX_DIM: usize = 2048;
const MAX_COUNT: usize = 64;

pub struct Identity {
    pub name: String,
    pub model_id: String,
    pub enabled: bool,
    pub templates: Vec<Vec<f32>>,
}

pub struct Store {
    root: PathBuf,
    key: [u8; 32],
}

impl Drop for Store {
    fn drop(&mut self) {
        self.key.zeroize();
    }
}

impl Store {
    pub fn open(root: &Path) -> Result<Self> {
        fs::create_dir_all(root).with_context(|| format!("create {}", root.display()))?;
        fs::set_permissions(root, fs::Permissions::from_mode(0o711))?;
        let key_path = root.join("master.key");
        let key = if key_path.exists() {
            let raw = fs::read(&key_path)?;
            if raw.len() != 32 {
                bail!("master.key has wrong length");
            }
            let mut k = [0u8; 32];
            k.copy_from_slice(&raw);
            k
        } else {
            let mut k = [0u8; 32];
            rand::thread_rng().fill_bytes(&mut k);
            let mut f = fs::OpenOptions::new()
                .write(true)
                .create_new(true)
                .mode(0o600)
                .open(&key_path)?;
            f.write_all(&k)?;
            f.sync_all()?;
            tracing::info!("generated new master key at {}", key_path.display());
            k
        };
        Ok(Self {
            root: root.to_path_buf(),
            key,
        })
    }

    fn user_dir(&self, uid: u32) -> PathBuf {
        self.root.join("users").join(uid.to_string())
    }

    fn path(&self, uid: u32, name: &str) -> Result<PathBuf> {
        // Identity names become filenames. Anything that could escape
        // the directory is rejected outright.
        if name.is_empty()
            || name.len() > 48
            || !name
                .chars()
                .all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_')
        {
            bail!("invalid identity name");
        }
        Ok(self.user_dir(uid).join(format!("{name}.tpl")))
    }

    fn aad(uid: u32, name: &str, model_id: &str) -> Vec<u8> {
        format!("{uid}|{name}|{model_id}|{VERSION}").into_bytes()
    }

    pub fn save(&self, uid: u32, ident: &Identity) -> Result<()> {
        if ident.templates.is_empty() {
            bail!("refusing to save an empty identity");
        }
        let dim = ident.templates[0].len();
        if dim == 0 || dim > MAX_DIM {
            bail!("bad embedding dimension {dim}");
        }
        if ident.templates.len() > MAX_COUNT {
            bail!("too many templates");
        }
        if ident.templates.iter().any(|t| t.len() != dim) {
            bail!("inconsistent embedding dimensions");
        }

        let mut plain = Vec::with_capacity(dim * ident.templates.len() * 4 + 1);
        plain.push(u8::from(ident.enabled));
        for t in &ident.templates {
            for v in t {
                plain.extend_from_slice(&v.to_le_bytes());
            }
        }

        let cipher = Aes256Gcm::new_from_slice(&self.key).map_err(|e| anyhow!("{e}"))?;
        let mut nonce_bytes = [0u8; NONCE_LEN];
        rand::thread_rng().fill_bytes(&mut nonce_bytes);
        let aad = Self::aad(uid, &ident.name, &ident.model_id);
        let ct = cipher
            .encrypt(
                Nonce::from_slice(&nonce_bytes),
                Payload {
                    msg: &plain,
                    aad: &aad,
                },
            )
            .map_err(|e| anyhow!("encrypt failed: {e}"))?;
        plain.zeroize();

        let mut out = Vec::new();
        out.extend_from_slice(MAGIC);
        out.push(VERSION);
        let mid = ident.model_id.as_bytes();
        if mid.len() > 255 {
            bail!("model_id too long");
        }
        out.push(mid.len() as u8);
        out.extend_from_slice(mid);
        out.extend_from_slice(&(dim as u16).to_le_bytes());
        out.extend_from_slice(&(ident.templates.len() as u16).to_le_bytes());
        out.extend_from_slice(&nonce_bytes);
        out.extend_from_slice(&ct);

        let dir = self.user_dir(uid);
        fs::create_dir_all(&dir)?;
        fs::set_permissions(&dir, fs::Permissions::from_mode(0o700))?;
        let final_path = self.path(uid, &ident.name)?;
        let tmp = final_path.with_extension("tpl.tmp");
        {
            let mut f = fs::OpenOptions::new()
                .write(true)
                .create(true)
                .truncate(true)
                .mode(0o600)
                .open(&tmp)?;
            f.write_all(&out)?;
            f.sync_all()?;
        }
        fs::rename(&tmp, &final_path)?;
        Ok(())
    }

    pub fn load(&self, uid: u32, name: &str) -> Result<Identity> {
        let path = self.path(uid, name)?;
        let raw = fs::read(&path).with_context(|| format!("read {}", path.display()))?;
        if raw.len() < 4 + 1 + 1 + 2 + 2 + NONCE_LEN {
            bail!("template truncated");
        }
        if &raw[0..4] != MAGIC {
            bail!("not a faceid template");
        }
        if raw[4] != VERSION {
            bail!("unsupported template version {}", raw[4]);
        }

        let mut off = 5usize;
        let mid_len = raw[off] as usize;
        off += 1;
        if raw.len() < off + mid_len + 4 + NONCE_LEN {
            bail!("template truncated");
        }
        let model_id = String::from_utf8(raw[off..off + mid_len].to_vec())?;
        off += mid_len;
        let dim = u16::from_le_bytes([raw[off], raw[off + 1]]) as usize;
        off += 2;
        let count = u16::from_le_bytes([raw[off], raw[off + 1]]) as usize;
        off += 2;
        if dim == 0 || dim > MAX_DIM || count == 0 || count > MAX_COUNT {
            bail!("template header out of range");
        }
        let nonce = &raw[off..off + NONCE_LEN];
        off += NONCE_LEN;

        let cipher = Aes256Gcm::new_from_slice(&self.key).map_err(|e| anyhow!("{e}"))?;
        let aad = Self::aad(uid, name, &model_id);
        let mut plain = cipher
            .decrypt(
                Nonce::from_slice(nonce),
                Payload {
                    msg: &raw[off..],
                    aad: &aad,
                },
            )
            .map_err(|_| {
                anyhow!(
                    "template authentication failed \
                                  (tampered, wrong user, or wrong key)"
                )
            })?;

        if plain.len() != 1 + dim * count * 4 {
            bail!("template body length mismatch");
        }
        let enabled = plain[0] != 0;
        let mut templates = Vec::with_capacity(count);
        for i in 0..count {
            let base = 1 + i * dim * 4;
            let mut v = Vec::with_capacity(dim);
            for j in 0..dim {
                let o = base + j * 4;
                v.push(f32::from_le_bytes([
                    plain[o],
                    plain[o + 1],
                    plain[o + 2],
                    plain[o + 3],
                ]));
            }
            templates.push(v);
        }
        plain.zeroize();
        Ok(Identity {
            name: name.to_string(),
            model_id,
            enabled,
            templates,
        })
    }

    pub fn list(&self, uid: u32) -> Vec<String> {
        let dir = self.user_dir(uid);
        let Ok(rd) = fs::read_dir(&dir) else {
            return Vec::new();
        };
        rd.filter_map(|e| e.ok())
            .filter_map(|e| {
                let p = e.path();
                (p.extension()?.to_str()? == "tpl")
                    .then(|| p.file_stem()?.to_str().map(str::to_string))?
            })
            .collect()
    }

    /// Every enabled identity whose model_id matches the running model.
    /// A stale identity from an old model is skipped, not compared.
    pub fn load_enabled(&self, uid: u32, model_id: &str) -> Vec<Identity> {
        self.list(uid)
            .into_iter()
            .filter_map(|n| match self.load(uid, &n) {
                Ok(i) => Some(i),
                Err(e) => {
                    tracing::warn!("skipping identity {n}: {e}");
                    None
                }
            })
            .filter(|i| {
                if i.model_id != model_id {
                    tracing::warn!(
                        "identity {} was enrolled with model {} \
                                    but {} is running; re-enroll needed",
                        i.name,
                        i.model_id,
                        model_id
                    );
                    return false;
                }
                i.enabled
            })
            .collect()
    }

    pub fn delete(&self, uid: u32, name: &str) -> Result<()> {
        let p = self.path(uid, name)?;
        if p.exists() {
            fs::remove_file(p)?;
        }
        Ok(())
    }

    pub fn delete_all(&self, uid: u32) -> Result<()> {
        let d = self.user_dir(uid);
        if d.exists() {
            fs::remove_dir_all(d)?;
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ident() -> Identity {
        Identity {
            name: "default".into(),
            model_id: "sface_v1".into(),
            enabled: true,
            templates: vec![vec![0.1, 0.2, 0.3], vec![0.4, 0.5, 0.6]],
        }
    }

    #[test]
    fn roundtrip() {
        let d = tempfile::tempdir().unwrap();
        let s = Store::open(d.path()).unwrap();
        s.save(1000, &ident()).unwrap();
        let back = s.load(1000, "default").unwrap();
        assert_eq!(back.templates.len(), 2);
        assert!((back.templates[1][2] - 0.6).abs() < 1e-6);
        assert_eq!(s.list(1000), vec!["default".to_string()]);
    }

    #[test]
    fn another_users_file_does_not_decrypt() {
        let d = tempfile::tempdir().unwrap();
        let s = Store::open(d.path()).unwrap();
        s.save(1000, &ident()).unwrap();
        let src = d.path().join("users/1000/default.tpl");
        let dstdir = d.path().join("users/1001");
        fs::create_dir_all(&dstdir).unwrap();
        fs::copy(src, dstdir.join("default.tpl")).unwrap();
        assert!(s.load(1001, "default").is_err());
    }

    #[test]
    fn tampering_is_detected() {
        let d = tempfile::tempdir().unwrap();
        let s = Store::open(d.path()).unwrap();
        s.save(1000, &ident()).unwrap();
        let p = d.path().join("users/1000/default.tpl");
        let mut raw = fs::read(&p).unwrap();
        let n = raw.len();
        raw[n - 1] ^= 0xff;
        fs::write(&p, raw).unwrap();
        assert!(s.load(1000, "default").is_err());
    }

    #[test]
    fn path_traversal_is_rejected() {
        let d = tempfile::tempdir().unwrap();
        let s = Store::open(d.path()).unwrap();
        assert!(s.path(1000, "../../etc/shadow").is_err());
        assert!(s.path(1000, "").is_err());
    }

    #[test]
    fn model_mismatch_identities_are_skipped() {
        let d = tempfile::tempdir().unwrap();
        let s = Store::open(d.path()).unwrap();
        s.save(1000, &ident()).unwrap();
        assert_eq!(s.load_enabled(1000, "arcface_v2").len(), 0);
        assert_eq!(s.load_enabled(1000, "sface_v1").len(), 1);
    }
}
