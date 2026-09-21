#!/bin/bash
# FaceID@NIM Diagnostic Script
# Run as: faceid-nim --diagnose (or sudo faceid-nim --diagnose for full output)

set -euo pipefail

echo "=== FaceID@NIM Diagnostics ==="
echo

# 1. Service status
echo "Services:"
for svc in faceid-nimd faceid-vision; do
    if systemctl is-active --quiet "$svc" 2>/dev/null; then
        echo "  $svc: ACTIVE"
    else
        echo "  $svc: INACTIVE"
    fi
done
echo

# 2. Socket status
echo "Sockets:"
for sock in "/run/faceid-nim/auth.sock" "/run/faceid-nim/worker/vision.sock"; do
    if [[ -S "$sock" ]]; then
        echo "  $sock: PRESENT"
    else
        echo "  $sock: MISSING"
    fi
done
echo

# 3. Daemon diagnostics (via D-Bus)
echo "Daemon diagnostics:"
if command -v faceid-nim &>/dev/null; then
    if faceid-nim status 2>/dev/null | grep -q "faceid-nimd.*active"; then
        # Try to get diagnostics via D-Bus
        python3 -c "
import gi
gi.require_version('Gio', '2.0')
from gi.repository import Gio, GLib
try:
    proxy = Gio.DBusProxy.new_for_bus_sync(
        Gio.BusType.SYSTEM, Gio.DBusProxyFlags.NONE, None,
        'org.faceidnim.Daemon1', '/org/faceidnim/Daemon1',
        'org.faceidnim.Daemon1', None)
    result = proxy.call_sync('Diagnostics', None,
        Gio.DBusCallFlags.NONE, 5000, None)
    import json
    print(json.dumps(json.loads(result[0]), indent=4))
except Exception as e:
    print(f'  D-Bus error: {e}')
" 2>/dev/null || echo "  Cannot reach daemon via D-Bus"
    else
        echo "  Daemon not running"
    fi
else
    echo "  faceid-nim command not found"
fi
echo

# 4. Camera status
echo "Cameras:"
for dev in /dev/video*; do
    if [[ -c "$dev" ]]; then
        name=$(v4l2-ctl -d "$dev" --info 2>/dev/null | grep "Card type" | cut -d: -f2 | xargs || echo "Unknown")
        formats=$(v4l2-ctl -d "$dev" --list-formats 2>/dev/null | grep -c "^\t\[" || echo 0)
        busy=""
        if fuser "$dev" 2>/dev/null; then
            busy=" [BUSY]"
        fi
        echo "  $dev: $name ($formats formats)$busy"
    fi
done
echo

# 5. Model status
echo "Models:"
MODEL_DIR="/var/lib/faceid-nim/models"
if [[ -d "$MODEL_DIR" ]]; then
    for model in detector recognizer antispoof; do
        if [[ -f "$MODEL_DIR/${model}.onnx" ]]; then
            echo "  $model: PRESENT"
        else
            echo "  $model: MISSING"
        fi
    done
else
    echo "  Model directory not found"
fi
echo

# 6. Enrolled users
echo "Enrolled users:"
USERS_DIR="/var/lib/faceid-nim/users"
if [[ -d "$USERS_DIR" ]]; then
    found=false
    for uid_dir in "$USERS_DIR"/*/; do
        if [[ -d "$uid_dir" ]]; then
            found=true
            uid=$(basename "$uid_dir")
            user=$(getent passwd "$uid" | cut -d: -f1)
            echo "  UID $uid ($user):"
            for tpl in "$uid_dir"/*.tpl; do
                if [[ -f "$tpl" ]]; then
                    name=$(basename "$tpl" .tpl)
                    echo "    - $name"
                fi
            done
        fi
    done
    if [[ "$found" == "false" ]]; then
        echo "  None"
    fi
else
    echo "  No users directory"
fi
echo

# 7. PAM configuration
echo "PAM configuration:"
if grep -q "pam_faceid.so" /etc/pam.d/common-auth 2>/dev/null; then
    echo "  common-auth: CONFIGURED"
    grep "pam_faceid.so" /etc/pam.d/common-auth
else
    echo "  common-auth: NOT CONFIGURED"
fi

if grep -q "pam_faceid.so" /etc/pam.d/gdm-password 2>/dev/null; then
    echo "  gdm-password: CONFIGURED (direct)"
elif grep -q "@include common-auth" /etc/pam.d/gdm-password 2>/dev/null; then
    echo "  gdm-password: VIA common-auth"
else
    echo "  gdm-password: NOT CONFIGURED"
fi

if grep -q "pam_faceid.so" /etc/pam.d/sudo 2>/dev/null; then
    echo "  sudo: CONFIGURED (direct)"
elif grep -q "@include common-auth" /etc/pam.d/sudo 2>/dev/null; then
    echo "  sudo: VIA common-auth"
else
    echo "  sudo: NOT CONFIGURED"
fi
echo

# 8. Config file
echo "Daemon config (/etc/faceid-nim/config.toml):"
if [[ -f /etc/faceid-nim/config.toml ]]; then
    cat /etc/faceid-nim/config.toml
else
    echo "  NOT FOUND (using defaults)"
fi
echo

# 9. Recent logs
echo "Recent daemon logs (last 10):"
journalctl -u faceid-nimd -n 10 --no-pager 2>/dev/null || echo "  No logs"
echo

echo "=== End Diagnostics ==="