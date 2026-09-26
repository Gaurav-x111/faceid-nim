use std::collections::HashMap;

use zbus::fdo::Error as FdoError;
use zbus::zvariant::{OwnedValue, Value};
use zbus::{proxy, Connection};

pub const ACTION_ENROLL: &str = "org.faceidnim.enroll";
pub const ACTION_SETTINGS: &str = "org.faceidnim.settings";
pub const ACTION_DELETE: &str = "org.faceidnim.delete";
pub const ACTION_ENABLE: &str = "org.faceidnim.enable";

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
        let policy = std::fs::read_to_string("../packaging/polkit/org.faceidnim.policy")
            .expect("installed polkit policy");
        for action in [ACTION_ENROLL, ACTION_SETTINGS, ACTION_DELETE, ACTION_ENABLE] {
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
        ] {
            assert!(bus.contains(&format!("send_member=\"{member}\"")));
        }
    }
}
