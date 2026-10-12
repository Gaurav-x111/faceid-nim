"""The D-Bus system-bus policy must be valid XML.

Regression test for the 2026-10-12 outage: an illegal `--` inside an
XML comment (from the Enrollment1 member enumeration) made the whole
policy file unparseable, so dbus-daemon never loaded the
`<allow own="org.faceidnim.Daemon1"/>` rule. The running daemon then
got AccessDenied on its name claim, kept serving PAM without the UI
bus, and every app preview failed with ServiceUnknown. expat/xmllint
reject double hyphens in comments; this test fails the build if one
ever comes back, and asserts the load-bearing rules are present.
"""
import pathlib
import re
import xml.dom.minidom

REPO = pathlib.Path(__file__).resolve().parents[2]
CONF = REPO / "packaging" / "dbus" / "org.faceidnim.Daemon1.conf"


def _text() -> str:
    return CONF.read_text(encoding="utf-8")


def test_policy_file_exists():
    assert CONF.is_file(), "D-Bus policy file missing from source tree"


def test_policy_parses_as_xml():
    # Raises xml.parsers.expat.ExpatError on malformed XML, e.g. `--`
    # inside a comment. dbus-daemon would drop the entire file.
    xml.dom.minidom.parse(str(CONF))


def test_no_double_hyphen_in_comments():
    for m in re.finditer(r"<!--(.*?)-->", _text(), re.DOTALL):
        assert "--" not in m.group(1), (
            "illegal '--' inside XML comment: "
            + m.group(1).strip()[:80])


def test_root_may_own_name():
    assert '<allow own="org.faceidnim.Daemon1"/>' in _text()


def test_enrollment_members_enumerated():
    text = _text()
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
