#!/bin/sh
# faceid-nim installer.
#
# Piping any script into `sudo sh` is a trust decision, so this one is
# deliberately boring and short enough to read in full before you run
# it. It does four things: check the platform, download the .deb and
# SHA256SUMS from the GitHub release, verify the hash, apt install.
#
# The two-step alternative, which you should prefer:
#   curl -fsSLO https://raw.githubusercontent.com/Gaurav-x111/faceid-nim/main/packaging/scripts/install.sh
#   less install.sh && sudo sh install.sh
set -eu

REPO="Gaurav-x111/faceid-nim"
TAG="${FACEID_TAG:-latest}"

die() { echo "error: $*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run as root (sudo sh install.sh)"
[ "$(uname -m)" = "x86_64" ] || die "only amd64 is supported right now"
command -v apt-get >/dev/null || die "this installer is for Debian/Ubuntu"

. /etc/os-release
case "${VERSION_ID:-}" in
  24.*|25.*|26.*) ;;
  *) die "Ubuntu 24.04 or newer required (found ${PRETTY_NAME:-unknown})" ;;
esac

if [ "$TAG" = "latest" ]; then
  BASE="https://github.com/$REPO/releases/latest/download"
else
  BASE="https://github.com/$REPO/releases/download/$TAG"
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT INT TERM
cd "$TMP"

echo "Downloading from $BASE ..."
curl -fsSLO "$BASE/SHA256SUMS"
DEB="$(awk '/_amd64\.deb$/ {print $2}' SHA256SUMS | head -1)"
[ -n "$DEB" ] || die "no amd64 .deb listed in SHA256SUMS"
curl -fsSLO "$BASE/$DEB"

echo "Verifying checksum ..."
grep " $DEB\$" SHA256SUMS | sha256sum -c - || die "CHECKSUM MISMATCH -- aborting"

# Signature check, if you have the release key imported. Not fatal,
# but the README should tell people how to import it and why.
if command -v gpg >/dev/null && curl -fsSLO "$BASE/SHA256SUMS.asc" 2>/dev/null; then
  gpg --verify SHA256SUMS.asc SHA256SUMS 2>/dev/null \
    && echo "signature OK" \
    || echo "warning: signature not verified (release key not imported?)"
fi

echo "Installing $DEB ..."
apt-get install -y "./$DEB"

cat <<'MSG'

Installed. Face unlock is DISABLED until you turn it on.

  1. Open "Face Unlock" and enroll (you will be asked for your password).
  2. Enable it there.
  3. On Wayland, log out and back in once so GNOME loads the extension.

Your password always works. If anything breaks, switch to a text
console (Ctrl+Alt+F3), log in, and run: sudo apt remove faceid-nim
MSG
