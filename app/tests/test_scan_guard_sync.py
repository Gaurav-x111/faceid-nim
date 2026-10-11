"""The terminal-state guard ships in two extension copies (canonical
shell-ext/ and the dynamic-island prototype fork): GJS resolves relative
imports per extension directory, so each needs its own scanGuard.js.
They must stay byte-identical -- a fix applied to one copy and not the
other reopens stale-overwrite displays on whichever lock screen runs
the stale fork. The node suite (shell-ext/tests/test-scan-guard.mjs)
exercises the canonical copy's behavior.
"""
import pathlib

REPO = pathlib.Path(__file__).resolve().parents[2]
CANON = REPO / "shell-ext" / "faceid@nim" / "scanGuard.js"
ISLAND = (REPO / "faceid-nim-dynamic-island" / "faceid-nim"
          / "shell-ext" / "faceid@nim" / "scanGuard.js")


def test_scan_guard_copies_are_identical():
    assert CANON.is_file(), "canonical scanGuard.js missing"
    assert ISLAND.is_file(), "island scanGuard.js missing"
    assert CANON.read_bytes() == ISLAND.read_bytes(), (
        "scanGuard.js copies diverged -- port the fix to both")
