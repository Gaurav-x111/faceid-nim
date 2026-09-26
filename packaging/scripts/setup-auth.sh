#!/bin/bash
# FaceID@NIM Authentication Setup Script
# Run as root: sudo ./setup-auth.sh

set -euo pipefail

CONFIG_FILE="/etc/faceid-nim/config.toml"
PAM_PROFILE="/usr/share/pam-configs/faceid-nim"
DAEMON_SERVICE="faceid-nimd"
VISION_SERVICE="faceid-vision"

echo "=== FaceID@NIM Authentication Setup ==="
echo

# Check if running as root
if [[ $EUID -ne 0 ]]; then
   echo "This script must be run as root (use sudo)"
   exit 1
fi

# 1. Install daemon config
echo "[1/6] Installing daemon configuration..."
if [[ -f "$CONFIG_FILE" ]]; then
    cp "$CONFIG_FILE" "${CONFIG_FILE}.bak.$(date +%s)"
    echo "  Backed up existing config"
fi
# Resolve source files relative to this script, so setup works from a
# checkout, a tarball, or anywhere -- never a hardcoded home directory.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PACKAGING_DIR="$(dirname "$SCRIPT_DIR")"
cp "$PACKAGING_DIR/faceid-nim-config.toml" "$CONFIG_FILE"
chmod 644 "$CONFIG_FILE"
echo "  Installed $CONFIG_FILE"

# 2. Install PAM profile
echo "[2/6] Installing PAM profile..."
if [[ -f "$PAM_PROFILE" ]]; then
    cp "$PAM_PROFILE" "${PAM_PROFILE}.bak.$(date +%s)"
    echo "  Backed up existing PAM profile"
fi
cp "$PACKAGING_DIR/pam-configs/faceid-nim" "$PAM_PROFILE"
echo "  Installed $PAM_PROFILE"

# 3. Update PAM configuration using pam-auth-update
echo "[3/6] Updating PAM configuration..."
pam-auth-update --package --enable faceid-nim
echo "  PAM configuration updated"

# 4. Restart services
echo "[4/6] Restarting FaceID services..."
systemctl daemon-reload
systemctl restart "$DAEMON_SERVICE"
systemctl restart "$VISION_SERVICE"
sleep 3

# Check service status
if systemctl is-active --quiet "$DAEMON_SERVICE"; then
    echo "  $DAEMON_SERVICE: RUNNING"
else
    echo "  $DAEMON_SERVICE: FAILED - check logs with: journalctl -u $DAEMON_SERVICE"
    exit 1
fi

if systemctl is-active --quiet "$VISION_SERVICE"; then
    echo "  $VISION_SERVICE: RUNNING"
else
    echo "  $VISION_SERVICE: FAILED - check logs with: journalctl -u $VISION_SERVICE"
    exit 1
fi

# 5. Verify daemon is reachable
echo "[5/6] Verifying daemon connectivity..."
if faceid-nim status 2>/dev/null | grep -q "worker_reachable.*true"; then
    echo "  Daemon and worker: CONNECTED"
else
    echo "  WARNING: Worker may not be reachable yet"
    faceid-nim status
fi

# 6. Check enrolled users
echo "[6/6] Checking enrolled users..."
USERS_DIR="/var/lib/faceid-nim/users"
if [[ -d "$USERS_DIR" ]] && [[ -n "$(ls -A "$USERS_DIR" 2>/dev/null)" ]]; then
    echo "  Enrolled users found:"
    for uid_dir in "$USERS_DIR"/*/; do
        uid=$(basename "$uid_dir")
        user=$(getent passwd "$uid" | cut -d: -f1)
        echo "    - UID $uid ($user)"
    done
else
    echo "  No enrolled users found"
    echo "  Run the FaceID app to enroll: faceid-app"
fi

echo
echo "=== Setup Complete ==="
echo
echo "Next steps:"
echo "  1. Run the FaceID app to enroll your face: faceid-app"
echo "  2. Test sudo authentication: sudo -k && sudo whoami"
echo "  3. Test lock screen: Super+L"
echo "  4. Test GDM login: log out and back in"
echo
echo "If anything fails, check logs:"
echo "  journalctl -u faceid-nimd -u faceid-vision -f"
echo
echo "To disable face authentication:"
echo "  pam-auth-update --package --disable faceid-nim"
echo "  systemctl stop faceid-nimd faceid-vision"