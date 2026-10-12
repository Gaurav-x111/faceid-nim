#!/usr/bin/env bash
# Install runtime deps on Ubuntu 24.04+ / GNOME / .deb distros.
set -euo pipefail
sudo apt update
sudo apt install -y \
  python3 python3-gi \
  gir1.2-gtk-4.0 libgtk-4-1 libgtk-4-media-gstreamer \
  gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
  gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly \
  gstreamer1.0-libav \
  ffmpeg
# NOTE: gir1.2-gtklayershell-0.1 / libgtk-layer-shell0 are DELIBERATELY
# absent. Every version Ubuntu ships (noble 0.8.2-1build2 included) binds
# Gtk-3.0, while this overlay is GTK4, so installing it only buys a
# typelib that can never load. The overlay detects that, prints a loud
# warning (see overlay.py), and continues as a plain window. A GTK4
# layer-shell must be built from upstream source; until then there is no
# on-top pill from this harness on Ubuntu.
echo "deps installed. Run: ./run.sh --self-test"
