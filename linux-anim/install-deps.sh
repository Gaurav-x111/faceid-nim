#!/usr/bin/env bash
# Install runtime deps on Ubuntu 24.04+ / GNOME / .deb distros.
set -euo pipefail
sudo apt update
sudo apt install -y \
  python3 python3-gi \
  gir1.2-gtk-4.0 libgtk-4-1 libgtk-4-media-gstreamer \
  gir1.2-gtklayershell-0.1 libgtk-layer-shell0 \
  gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
  gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly \
  gstreamer1.0-libav \
  ffmpeg
echo "deps installed. Run: ./run.sh --self-test"
