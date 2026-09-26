//! Attempt limiting and service allow-listing.
//!
//! Callers get success or failure and nothing else. No similarity
//! score, no "close", no per-cue breakdown: anything that tells an
//! attacker whether they are getting warmer turns a biometric into a
//! hill-climbing target.

use std::collections::HashMap;
use std::time::{Duration, Instant};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Verdict {
    Allow,
    LockedOut,
    ServiceNotAllowed,
    Disabled,
    NoIdentities,
}

impl Verdict {
    /// The only strings a caller ever sees.
    pub fn user_message(self) -> &'static str {
        match self {
            Verdict::Allow => "",
            Verdict::LockedOut => "Face unlock temporarily disabled after repeated failures",
            Verdict::ServiceNotAllowed => "Face unlock is not enabled for this prompt",
            Verdict::Disabled => "Face unlock is not enabled",
            Verdict::NoIdentities => "No face is enrolled for this account",
        }
    }
}

#[derive(Debug, Default)]
struct Attempts {
    failures: u32,
    locked_until: Option<Instant>,
}

#[derive(Default)]
pub struct Policy {
    per_uid: HashMap<u32, Attempts>,
}

impl Policy {
    pub fn check(&mut self, uid: u32, max_failures: u32) -> Verdict {
        let a = self.per_uid.entry(uid).or_default();
        if let Some(until) = a.locked_until {
            if Instant::now() < until {
                return Verdict::LockedOut;
            }
            // Cooldown expired: clear it, and reset the counter so a
            // legitimate user is not one mistake from another lockout.
            a.locked_until = None;
            a.failures = 0;
        }
        let _ = max_failures;
        Verdict::Allow
    }

    pub fn record_success(&mut self, uid: u32) {
        self.per_uid.insert(uid, Attempts::default());
    }

    pub fn record_failure(&mut self, uid: u32, max_failures: u32, lockout: Duration) -> bool {
        let a = self.per_uid.entry(uid).or_default();
        a.failures += 1;
        // Clamp attacker-influenced values: max_failures comes from
        // SetSettings, lockout from config. Saturating add never panics.
        let max_failures = max_failures.clamp(1, 20);
        let lockout = lockout.min(Duration::from_secs(3600));
        if a.failures >= max_failures {
            a.locked_until = Instant::now().checked_add(lockout);
            return true;
        }
        false
    }

    #[cfg(test)]
    pub fn failures(&self, uid: u32) -> u32 {
        self.per_uid.get(&uid).map(|a| a.failures).unwrap_or(0)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn locks_out_after_max_failures_then_recovers() {
        let mut p = Policy::default();
        assert_eq!(p.check(1000, 3), Verdict::Allow);
        assert!(!p.record_failure(1000, 3, Duration::from_millis(30)));
        assert!(!p.record_failure(1000, 3, Duration::from_millis(30)));
        assert!(p.record_failure(1000, 3, Duration::from_millis(30)));
        assert_eq!(p.check(1000, 3), Verdict::LockedOut);
        std::thread::sleep(Duration::from_millis(50));
        assert_eq!(p.check(1000, 3), Verdict::Allow);
        assert_eq!(p.failures(1000), 0);
    }

    #[test]
    fn success_clears_the_counter() {
        let mut p = Policy::default();
        p.record_failure(1000, 5, Duration::from_secs(60));
        p.record_success(1000);
        assert_eq!(p.failures(1000), 0);
    }

    #[test]
    fn lockout_is_per_uid() {
        let mut p = Policy::default();
        for _ in 0..3 {
            p.record_failure(1000, 3, Duration::from_secs(60));
        }
        assert_eq!(p.check(1000, 3), Verdict::LockedOut);
        assert_eq!(p.check(1001, 3), Verdict::Allow);
    }

    #[test]
    fn verdict_messages_leak_no_scores() {
        for v in [
            Verdict::LockedOut,
            Verdict::ServiceNotAllowed,
            Verdict::Disabled,
            Verdict::NoIdentities,
        ] {
            let m = v.user_message();
            assert!(
                !m.contains('.') || !m.chars().any(|c| c.is_ascii_digit()),
                "message must not contain a numeric score: {m}"
            );
        }
    }
}
