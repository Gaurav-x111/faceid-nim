use std::collections::HashMap;

use zbus::fdo::Error as FdoError;
use zbus::zvariant::{OwnedValue, Value};
use zbus::{proxy, Connection};

pub const ACTION_ENROLL: &str = "org.faceidnim.enroll";
pub const ACTION_SETTINGS: &str = "org.faceidnim.settings";
pub const ACTION_DELETE: &str = "org.faceidnim.delete";
pub const ACTION_ENABLE: &str = "org.faceidnim.enable";
/// Watching the camera is reading a sensor in the user's room, so the
/// preview pipe needs the same account-password re-auth as enrollment.
/// (Round-2 audit: StartPreview handed camera JPEGs to any local
/// caller with only a uid check.)
pub const ACTION_PREVIEW: &str = "org.faceidnim.preview";
/// A diagnostic scan still opens the camera, so it needs a polkit
/// decision -- but `allow_active = yes` (no password prompt) so the
/// Test button stays smooth. Remote and inactive sessions are denied:
/// the camera must not open for someone not at this machine.
pub const ACTION_TEST: &str = "org.faceidnim.test";

type PolkitSubject = (String, HashMap<String, OwnedValue>);
type AuthzResult = zbus::Result<(bool, bool, HashMap<String, String>)>;

#[proxy(
    interface = "org.freedesktop.PolicyKit1.Authority",
    default_service = "org.freedesktop.PolicyKit1",
    default_path = "/org/freedesktop/PolicyKit1/Authority"
)]
trait Authority {
    fn check_authorization(
        &self,
        subject: &PolkitSubject,
        action_id: &str,
        details: &HashMap<String, String>,
        flags: u32,
        cancellation_id: &str,
    ) -> AuthzResult;
}

pub async fn require_polkit(
    conn: &Connection,
    hdr: &zbus::message::Header<'_>,
    action: &str,
) -> zbus::fdo::Result<()> {
    let sender = hdr
        .sender()
        .ok_or_else(|| FdoError::AccessDenied("no sender".into()))?;

    let mut subject_properties = HashMap::new();
    subject_properties.insert(
        "name".to_string(),
        Value::from(sender.as_str())
            .try_into()
            .map_err(|e| FdoError::Failed(format!("polkit subject: {e}")))?,
    );
    let subject = ("system-bus-name".to_string(), subject_properties);

    tracing::debug!(action_id = action, sender = sender.as_str(), "polkit check");

    let authority = AuthorityProxy::new(conn)
        .await
        .map_err(|e| FdoError::Failed(format!("polkit: {e}")))?;
    let result = authority
        .check_authorization(&subject, action, &HashMap::new(), 1, "")
        .await;

    let (authorized, challenge, details) = match result {
        Ok(v) => v,
        Err(e) => return Err(FdoError::Failed(format!("polkit: {e}"))),
    };
    tracing::debug!(authorized, challenge, ?details, "polkit result");

    if authorized {
        Ok(())
    } else {
        Err(FdoError::AccessDenied(
            "not authorized to perform this action".into(),
        ))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn action_ids_match_installed_policy() {
        assert_eq!(ACTION_ENROLL, "org.faceidnim.enroll");
        assert_eq!(ACTION_SETTINGS, "org.faceidnim.settings");
        assert_eq!(ACTION_DELETE, "org.faceidnim.delete");
        assert_eq!(ACTION_ENABLE, "org.faceidnim.enable");
        assert_eq!(ACTION_PREVIEW, "org.faceidnim.preview");
        assert_eq!(ACTION_TEST, "org.faceidnim.test");
        let policy = std::fs::read_to_string("../packaging/polkit/org.faceidnim.policy")
            .expect("installed polkit policy");
        for action in [
            ACTION_ENROLL,
            ACTION_SETTINGS,
            ACTION_DELETE,
            ACTION_ENABLE,
            ACTION_PREVIEW,
            ACTION_TEST,
        ] {
            assert!(policy.contains(&format!("action id=\"{action}\"")));
        }
        let bus = std::fs::read_to_string("../packaging/dbus/org.faceidnim.Daemon1.conf")
            .expect("installed D-Bus policy");
        for member in [
            "SetSettings",
            "SetPamEnabled",
            "GetTimeline",
            "SetIdentityEnabled",
            "DeleteIdentity",
            "DeleteAllData",
            "Enroll",
            "TestScan",
            "PreviewAnimation",
        ] {
            assert!(bus.contains(&format!("send_member=\"{member}\"")));
        }
        // 1B-3 regression: the Enrollment1 interface must be an
        // enumerated member list, never a blanket interface allow --
        // a blanket allow silently exposes every future method added
        // to the interface. Allow elements span lines, so scan element
        // by element rather than line by line.
        for member in [
            "ListPoses",
            "StartEnrollment",
            "EnrollPose",
            "FinishEnrollment",
            "CancelEnrollment",
            "GetSessionInfo",
        ] {
            assert!(
                bus.contains(&format!("send_member=\"{member}\"")),
                "Enrollment1 member {member} must be explicitly allowed"
            );
        }
        let mut idx = 0;
        let mut enumerated = 0;
        while let Some(pos) = bus[idx..].find("send_interface=\"org.faceidnim.Enrollment1\"") {
            let abs_pos = idx + pos;
            let elem_start = bus[..abs_pos].rfind("<allow").expect("allow element");
            let elem_end = bus[abs_pos..].find('>').expect("element end") + abs_pos;
            let elem = &bus[elem_start..=elem_end];
            assert!(
                elem.contains("send_member="),
                "blanket Enrollment1 allow (no send_member): {elem}"
            );
            enumerated += 1;
            idx = elem_end + 1;
        }
        assert_eq!(enumerated, 6, "expected 6 enumerated Enrollment1 members");
    }

    /// 1B-2 regression: the TestScan action must stay promptless for
    /// the local active session but closed for remote/inactive ones.
    /// `auth_self` here would password-prompt every Test-button click;
    /// `yes` for inactive/any would open the camera for sessions not
    /// at this machine.
    #[test]
    fn test_action_is_promptless_local_only() {
        let policy = std::fs::read_to_string("../packaging/polkit/org.faceidnim.policy")
            .expect("installed polkit policy");
        let start = policy
            .find("action id=\"org.faceidnim.test\"")
            .expect("test action present");
        let block = &policy[start..];
        let end = block.find("</action>").expect("test action closed");
        let block = &block[..end];
        assert!(block.contains("<allow_any>no</allow_any>"), "remote: no");
        assert!(
            block.contains("<allow_inactive>no</allow_inactive>"),
            "inactive: no"
        );
        assert!(
            block.contains("<allow_active>yes</allow_active>"),
            "active: yes"
        );
    }

    /// Master-audit registration check: every D-Bus method implemented
    /// on Enrollment1 in src/enroll.rs must be enumerated in the bus
    /// policy tested above -- a method that exists in code but is
    /// missing from the policy is either unreachable (dead) or
    /// reachable without an explicit allow (policy hole). Helpers and
    /// the EnrollProgress signal are not method calls and are excluded.
    #[test]
    fn enrollment_methods_match_bus_policy() {
        let src = std::fs::read_to_string("src/enroll.rs").expect("enroll.rs source");
        let mut methods = std::collections::BTreeSet::new();
        for line in src.lines() {
            let line = line.trim();
            let rest = match line.strip_prefix("async fn ") {
                Some(r) => r,
                None => continue,
            };
            // Strip generic params: with_session<T> is one helper.
            let name: String = rest
                .chars()
                .take_while(|c| *c != '(' && *c != '<')
                .collect();
            match name.as_str() {
                // Internal helpers, not D-Bus methods. A new helper
                // added here must be classified in this list, or the
                // test fails until it is -- that is the point.
                "caller_uid"
                | "with_session"
                | "next_session_id"
                | "deliver_preview_map"
                | "deliver_guidance_map" => continue,
                // A signal, not a method call.
                "enroll_progress" => continue,
                _ => {}
            }
            let pascal: String = name
                .split('_')
                .map(|w| {
                    let mut c = w.chars();
                    match c.next() {
                        Some(f) => f.to_uppercase().collect::<String>() + c.as_str(),
                        None => String::new(),
                    }
                })
                .collect();
            methods.insert(pascal);
        }
        let expected: std::collections::BTreeSet<String> = [
            "ListPoses",
            "StartEnrollment",
            "EnrollPose",
            "FinishEnrollment",
            "CancelEnrollment",
            "GetSessionInfo",
        ]
        .into_iter()
        .map(str::to_string)
        .collect();
        assert_eq!(methods, expected, "enroll.rs methods vs bus policy");
    }
}
