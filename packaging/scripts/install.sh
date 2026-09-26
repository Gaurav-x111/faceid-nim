#!/bin/sh
# faceid-nim installer -- pick the .deb that matches this machine and
# install it.
#
#   curl -fsSL https://<host>/install.sh | sudo sh
#
# Deliberately does three things and nothing else:
#   1. works out the CPU architecture from dpkg, not from uname, so a
#      multi-arch or foreign-architecture system is handled correctly;
#   2. downloads ONE pinned .deb over HTTPS and refuses to run anything
#      it did not get from the URL it was told to use;
#   3. hands the rest to dpkg, which runs our postinst -- and postinst is
#      what fetches the models, detects the cameras and starts the
#      services. No commands for you to type afterwards.
#
# Face unlock is installed DISABLED and stays that way until you enable
# it in the app. Your password always works.
set -eu

VERSION="${FACEID_NIM_VERSION:-}"
BASE_URL="${FACEID_NIM_URL:-https://github.com/Gaurav-x111/faceid-nim/releases/latest/download}"
# Single source of truth for the default .deb filename. Bump together with
# packaging/debian/changelog so a fresh `latest/download` URL works.
PKG_VERSION="1.1.1-1"

say()  { printf '%s\n' "$*"; }
warn() { printf '%s\n' "$*" >&2; }
die()  { printf 'faceid-nim: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "needs root; re-run with: curl -fsSL $0 | sudo sh"

command -v dpkg >/dev/null 2>&1 || die "dpkg not found -- this installer is for Debian/Ubuntu (and derivatives)"
command -v dpkg-deb >/dev/null 2>&1 || die "dpkg-deb not found; install the 'dpkg' package"

ARCH="$(dpkg --print-architecture 2>/dev/null || true)"
[ -n "$ARCH" ] || die "could not determine the package architecture"
case "$ARCH" in
    amd64|arm64|armhf|i386) : ;;
    *) die "unsupported architecture '$ARCH'.
       This project ships amd64 and arm64 packages. On anything else,
       build from source:  sudo make install" ;;
esac

if [ -z "$VERSION" ]; then
    # `latest/download` only works for tags, not for a plain branch.
    VERSION="$(curl -fsSLI -o /dev/null -w '%{url_effective}' \
        "$BASE_URL/faceid-nim_${PKG_VERSION}_${ARCH}.deb" 2>/dev/null \
        | sed 's|.*/tag/||' || true)"
    [ -n "$VERSION" ] || VERSION="latest"
fi

if [ "$VERSION" = "latest" ]; then
    URL="$BASE_URL/faceid-nim_${PKG_VERSION}_${ARCH}.deb"
else
    # Tags are `v1.1.1` but the .deb is `1.1.1-1`: strip a leading `v`
    # so FACEID_NIM_VERSION=v1.1.1 and 1.1.1 both resolve.
    DEB_VERSION="$(printf '%s' "$VERSION" | sed 's/^v//')"
    URL="https://github.com/Gaurav-x111/faceid-nim/releases/download/${VERSION}/faceid-nim_${DEB_VERSION}_${ARCH}.deb"
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT INT TERM

say "faceid-nim installer"
say "  architecture : $ARCH"
say "  downloading  : $URL"

DL=''
if command -v curl >/dev/null 2>&1; then
    DL='curl -fsSL --retry 3 --retry-delay 2 -o'
elif command -v wget >/dev/null 2>&1; then
    DL='wget -q -O'
else
    die "need curl or wget to download the package"
fi

# shellcheck disable=SC2086
$DL "$TMP/faceid-nim.deb" "$URL" \
    || die "download failed.
       Check your connection, or download manually from:
       $URL"

dpkg-deb --info "$TMP/faceid-nim.deb" >/dev/null 2>&1 \
    || die "the downloaded file is not a valid .deb -- refusing to install it"

INSTALLED_ARCH="$(dpkg-deb -f "$TMP/faceid-nim.deb" Architecture)"
[ "$INSTALLED_ARCH" = "$ARCH" ] \
    || die "downloaded a '$INSTALLED_ARCH' package onto a '$ARCH' system"

say "  installing   : faceid-nim $INSTALLED_ARCH"
if dpkg -i "$TMP/faceid-nim.deb"; then
    :
else
    # A dependency missing is the common case on a fresh machine. Let apt
    # resolve it rather than leaving a half-configured package.
    say "  resolving dependencies with apt..."
    if command -v apt-get >/dev/null 2>&1; then
        apt-get install -f -y || die "apt could not satisfy the dependencies"
    else
        die "dpkg failed and apt-get is not available; see the error above"
    fi
fi

say ""
say "Installed. Next:"
say "  1. open \"Face Unlock\" from your applications"
say "  2. enroll your face, then turn face unlock on there"
say "  3. on Wayland, log out and back in once so the lock-screen pill loads"
say ""
say "Check anything at any time with:  faceid-nim status"
say "Your password always works. Removing it:  sudo apt remove faceid-nim"
