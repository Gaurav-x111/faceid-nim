#!/usr/bin/env bash
# Build a .deb for Ubuntu 24.04+ (GNOME, Wayland+X11) with dpkg-deb. No root needed.
# Output: dist/glance-anim-test_*.deb — installs /usr/share/glance-anim + /usr/bin/glance-anim-test
set -euo pipefail
cd "$(dirname "$0")"
VERSION="${1:-0.1.0}"
ARCH="all"
PKG="dist/glance-anim-test_${VERSION}_${ARCH}"
rm -rf "$PKG"
mkdir -p "$PKG/DEBIAN" "$PKG/usr/share/glance-anim" "$PKG/usr/bin" "$PKG/usr/share/applications"
find . -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
cp -r glance_anim test_anim.py run.sh README.md "$PKG/usr/share/glance-anim/"
find "$PKG" -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
find "$PKG" -name "*.pyc" -delete 2>/dev/null || true
cat > "$PKG/DEBIAN/control" <<EOF
Package: glance-anim-test
Version: ${VERSION}
Section: utils
Priority: optional
Architecture: ${ARCH}
Depends: python3, python3-gi, gir1.2-gtk-4.0, libgtk-4-1, libgtk-4-media-gstreamer, gir1.2-gtklayershell-0.1, libgtk-layer-shell0, gstreamer1.0-plugins-base, gstreamer1.0-plugins-good, gstreamer1.0-libav
Maintainer: Glance <test@glance>
Description: Glance face-unlock animation test harness (Linux port)
 Isolated GTK4/LayerShell pill overlay reproducing the macOS notch
 animation. Test here before integrating into the main face-unlock app.
EOF
cat > "$PKG/usr/bin/glance-anim-test" <<'EOF'
#!/usr/bin/env bash
exec python3 /usr/share/glance-anim/test_anim.py "$@"
EOF
chmod 755 "$PKG/usr/bin/glance-anim-test"
cat > "$PKG/usr/share/applications/glance-anim-test.desktop" <<'EOF'
[Desktop Entry]
Name=Glance Anim Test
Comment=Test face-unlock pill animation
Exec=glance-anim-test
Terminal=false
Type=Application
Categories=Utility;
EOF
dpkg-deb --build "$PKG"
echo "built $PKG.deb"
echo "install: sudo dpkg -i $PKG.deb && glance-anim-test --self-test"
