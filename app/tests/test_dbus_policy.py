"""System-bus and polkit policy files must be valid XML.

Regression tests for two outages with one root cause: an illegal `--`
inside an XML comment makes the whole policy file unparseable.

1. 2026-10-12 (D-Bus): the `--` in org.faceidnim.Daemon1.conf meant
   dbus-daemon never loaded the `<allow own>` rule. The daemon got
   AccessDenied on its name claim, kept serving PAM without the UI
   bus, and every app preview failed with ServiceUnknown.
2. 2026-10-12 (polkit): the `--` in org.faceidnim.policy's own
   comment block meant polkitd never registered org.faceidnim.test
   (5 of 6 actions visible in pkaction). Every Test-button scan then
   failed its authorization check on a local active session that
   should pass prompt-free.

expat/xmllint reject double hyphens in comments; these tests fail the
build if one ever comes back, and assert the load-bearing rules and
actions are present.
"""
import pathlib
import re
import xml.dom.minidom

REPO = pathlib.Path(__file__).resolve().parents[2]
CONF = REPO / "packaging" / "dbus" / "org.faceidnim.Daemon1.conf"
POLICY = REPO / "packaging" / "polkit" / "org.faceidnim.policy"


def _text(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8")


def _assert_no_double_hyphen_in_comments(path: pathlib.Path) -> None:
    for m in re.finditer(r"<!--(.*?)-->", _text(path), re.DOTALL):
        assert "--" not in m.group(1), (
            f"{path.name}: illegal '--' inside XML comment: "
            + m.group(1).strip()[:80])


def test_policy_file_exists():
    assert CONF.is_file(), "D-Bus policy file missing from source tree"
    assert POLICY.is_file(), "polkit policy file missing from source tree"


def test_policy_parses_as_xml():
    # Raises xml.parsers.expat.ExpatError on malformed XML, e.g. `--`
    # inside a comment. dbus-daemon would drop the entire file.
    xml.dom.minidom.parse(str(CONF))


def test_no_double_hyphen_in_comments():
    _assert_no_double_hyphen_in_comments(CONF)


def test_root_may_own_name():
    assert '<allow own="org.faceidnim.Daemon1"/>' in _text(CONF)


def test_enrollment_members_enumerated():
    text = _text(CONF)
    for member in ("ListPoses", "StartEnrollment", "EnrollPose",
                   "FinishEnrollment", "CancelEnrollment"):
        assert f'send_member="{member}"' in text, member
    # No blanket interface allow: every Enrollment1 <allow> must name
    # one member, so future methods are not silently exposed
    # (daemon/src/authz.rs asserts the same list).
    allows = re.findall(r"<allow\b[^>]*>", text)
    enrollment_allows = [a for a in allows
                         if "Enrollment1" in a and "receive_" not in a]
    assert enrollment_allows, "no Enrollment1 send rules found"
    for a in enrollment_allows:
        assert "send_member=" in a, f"member-less Enrollment1 allow: {a}"


def test_polkit_policy_parses_as_xml():
    # Same disease as the D-Bus file: polkitd silently skips what it
    # cannot parse, so org.faceidnim.test never registered and every
    # Test-button scan failed authorization on machines that should
    # pass prompt-free.
    xml.dom.minidom.parse(str(POLICY))


def test_polkit_no_double_hyphen_in_comments():
    _assert_no_double_hyphen_in_comments(POLICY)


def test_polkit_test_action_present_and_promptless():
    text = _text(POLICY)
    for action in ("org.faceidnim.enroll", "org.faceidnim.settings",
                   "org.faceidnim.settings-secure", "org.faceidnim.delete",
                   "org.faceidnim.enable",
                   "org.faceidnim.preview", "org.faceidnim.test"):
        assert f'action id="{action}"' in text, action
    block = text[text.index('action id="org.faceidnim.test"'):]
    block = block[:block.index("</action>")]
    assert "<allow_any>no</allow_any>" in block
    assert "<allow_inactive>no</allow_inactive>" in block
    assert "<allow_active>yes</allow_active>" in block


def test_polkit_privilege_tiers():
    """H3/O1/O5 + H9: secure settings and PAM enablement need
    auth_admin; deletion needs auth_self (never promptless)."""
    text = _text(POLICY)

    def block(action: str) -> str:
        start = text.index(f'action id="{action}"')
        seg = text[start:]
        return seg[:seg.index("</action>")]

    secure = block("org.faceidnim.settings-secure")
    assert "<allow_active>auth_admin</allow_active>" in secure
    delete = block("org.faceidnim.delete")
    assert "<allow_active>auth_self</allow_active>" in delete
    enable = block("org.faceidnim.enable")
    assert "<allow_active>auth_admin</allow_active>" in enable


def test_postinst_refreshes_polkit():
    """New polkit actions only register when polkitd re-reads them;
    postinst must refresh it (guarded, non-fatal), or the next added
    action silently fails authorization exactly like org.faceidnim.test
    did."""
    text = _text(REPO / "packaging" / "debian" / "postinst")
    assert "try-restart polkit.service" in text
    assert "try-restart polkit.service || true" in text
