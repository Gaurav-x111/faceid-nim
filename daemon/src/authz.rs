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
}
