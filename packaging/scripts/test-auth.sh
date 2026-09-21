#!/bin/bash
# FaceID@NIM Authentication Test Script
# Tests the PAM authentication flow for sudo, lock screen, and GDM

set -euo pipefail

echo "=== FaceID@NIM Authentication Test ==="
echo

# Check if daemon is running
if ! systemctl is-active --quiet faceid-nimd; then
    echo "ERROR: faceid-nimd is not running"
    exit 1
fi

if ! systemctl is-active --quiet faceid-vision; then
    echo "ERROR: faceid-vision is not running"
    exit 1
fi

# Check if user has enrolled faces
UID=$(id -u)
USER_DIR="/var/lib/faceid-nim/users/$UID"
if [[ ! -d "$USER_DIR" ]] || [[ -z "$(ls -A "$USER_DIR" 2>/dev/null)" ]]; then
    echo "ERROR: No enrolled face found for user $(whoami) (UID $UID)"
    echo "Run the FaceID app to enroll: faceid-app"
    exit 1
fi

echo "User: $(whoami) (UID $UID)"
echo "Enrolled identities:"
for tpl in "$USER_DIR"/*.tpl; do
    if [[ -f "$tpl" ]]; then
        name=$(basename "$tpl" .tpl)
        echo "  - $name"
    fi
done
echo

# Test 1: Direct daemon test scan via D-Bus
echo "Test 1: Daemon test scan (via D-Bus)..."
python3 -c "
import gi
gi.require_version('Gio', '2.0')
from gi.repository import Gio, GLib
try:
    proxy = Gio.DBusProxy.new_for_bus_sync(
        Gio.BusType.SYSTEM, Gio.DBusProxyFlags.NONE, None,
        'org.faceidnim.Daemon1', '/org/faceidnim/Daemon1',
        'org.faceidnim.Daemon1', None)
    result = proxy.call_sync('TestScan', None,
        Gio.DBusCallFlags.ALLOW_INTERACTIVE_AUTHORIZATION, 15000, None)
    ok, msg = result[0], result[1]
    print(f'  Result: {\"SUCCESS\" if ok else \"FAILED\"} - {msg}')
except Exception as e:
    print(f'  ERROR: {e}')
"
echo

# Test 2: PAM authentication via sudo
echo "Test 2: PAM authentication via sudo..."
echo "  Running: sudo -k && sudo -v"
echo "  (Watch for face scan animation - should appear on lock screen pill)"
echo "  If face scan succeeds, sudo will not ask for password"
echo

# We can't easily test sudo interactively here, so just show the command
echo "  To test manually, run:"
echo "    sudo -k"
echo "    sudo whoami"
echo

# Test 3: PAM authentication via direct PAM test
echo "Test 3: PAM module test (direct)..."
if command -v pamtester &>/dev/null; then
    echo "  pamtester found - can test PAM stack directly"
    echo "  Run: pamtester -v sudo $(whoami) authenticate"
else
    echo "  pamtester not installed - install with: sudo apt install pamtester"
    echo "  Then run: pamtester -v sudo \$(whoami) authenticate"
fi
echo

echo "=== Test Summary ==="
echo "If all tests pass, face authentication should work for:"
echo "  - sudo (terminal)"
echo "  - Lock screen (Super+L)"
echo "  - GDM login (logout and back in)"
echo
echo "To monitor authentication in real-time:"
echo "  journalctl -u faceid-nimd -f"
echo
echo "To watch the lock screen pill animation:"
echo "  (Lock screen with Super+L and observe the pill)"