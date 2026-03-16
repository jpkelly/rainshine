#!/bin/bash
# Setup script for Rainshine DMX on Raspberry Pi 5 (Trixie Lite 64-bit)

set -e

echo "=== Installing system packages ==="
sudo apt update
sudo apt install -y \
    python3-pip \
    python3-venv \
    ola \
    libegl1-mesa-dev \
    libgles2-mesa-dev \
    mesa-utils

echo "=== Creating Python venv ==="
python3 -m venv --system-site-packages ~/rainshine-env
source ~/rainshine-env/bin/activate

echo "=== Installing Python packages ==="
pip3 install moderngl ola

echo "=== Enabling OLA service ==="
sudo systemctl enable olad
sudo systemctl start olad

echo ""
echo "=== Setup complete ==="
echo ""
echo "Next steps:"
echo "  1. Configure OLA universes via the web UI at http://$(hostname -I | awk '{print $1}'):9090"
echo "     - Add an Art-Net output plugin or E1.31 (sACN) device"
echo "     - Patch universe 0 and universe 1 to your output"
echo ""
echo "  2. Activate the venv and run:"
echo "     source ~/rainshine-env/bin/activate"
echo "     python3 ~/rainshine_dmx.py --preview"
echo ""
echo "  3. For autostart on boot, add a systemd service (see README)."
