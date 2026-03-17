#!/bin/bash
# Setup script for Rainshine DMX on Raspberry Pi 5 (Trixie Lite 64-bit)
# Sends sACN/E1.31 directly over UDP — no OLA or other DMX middleware needed.

set -e

echo "=== Installing system packages ==="
sudo apt update
sudo apt install -y \
    python3-pip \
    python3-venv \
    libegl1-mesa-dev \
    libgles2-mesa-dev \
    mesa-utils

echo "=== Creating Python venv ==="
python3 -m venv --system-site-packages ~/rainshine-env
source ~/rainshine-env/bin/activate

echo "=== Installing Python packages ==="
pip3 install moderngl python-osc numpy

echo "=== Installing systemd service ==="
sudo cp ~/rainshine/rainshine.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable rainshine

echo ""
echo "=== Setup complete ==="
echo ""
echo "Next steps:"
echo "  1. Configure your ENTTEC Pixel OCTO (or other sACN receiver) — see README"
echo ""
echo "  2. Edit ~/rainshine/rainshine.conf to set sacn_dest to your receiver's IP"
echo ""
echo "  3. Test:"
echo "     source ~/rainshine-env/bin/activate"
echo "     cd ~/rainshine"
echo "     python3 rainshine_dmx.py"
echo ""
echo "  4. Start the service:"
echo "     sudo systemctl start rainshine"
echo "     journalctl -u rainshine -f"
