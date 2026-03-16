# Rainshine — Pi 5 DMX LED Shader

A GLSL rainbow rain shader running headlessly on a Raspberry Pi 5, outputting to WS2812B LEDs via sACN/E1.31 through an ENTTEC Pixel OCTO.

## Hardware

- **Raspberry Pi 5** — Trixie Lite 64-bit (headless)
- **ENTTEC Pixel OCTO** — Art-Net/sACN to WS2812B pixel driver (10.0.0.123)
- **WS2812B LED strip** — 10 columns × 30 rows (300 pixels), zigzag wired, GRB color order

## Files

| File | Description |
|---|---|
| `rainshine.frag` | GLSL ES 3.0 fragment shader — rainbow rain effect |
| `rainshine_dmx.py` | Python host — renders shader headlessly, sends DMX via OLA (sACN) |
| `rainshine.conf` | Config file — default shader params, output settings, OSC port |
| `rainshine.service` | systemd unit — runs the shader on boot |
| `setup.sh` | One-time Pi setup script |

## Setup

### 1. Install dependencies

```bash
sudo apt update && sudo apt install -y python3-pip python3-venv ola libegl1-mesa-dev libgles2-mesa-dev mesa-utils
```

### 2. Create Python environment

```bash
python3 -m venv --system-site-packages ~/rainshine-env
source ~/rainshine-env/bin/activate
pip3 install moderngl python-osc numpy
```

### 3. Fix OLA Python 3.13 compatibility

```bash
sudo sed -i 's/\.tostring()/.tobytes()/g' $(python3 -c "import ola.OlaClient; print(ola.OlaClient.__file__)")
```

### 4. Configure OLA

Start OLA and patch sACN universes:

```bash
sudo systemctl enable olad
sudo systemctl start olad
ola_patch --device 7 --port 0 --universe 1
ola_patch --device 7 --port 1 --universe 2
ola_patch --device 7 --port 2 --universe 3
```

Verify: http://PiDMX.local:9090

### 5. Configure ENTTEC Pixel OCTO

Via http://10.0.0.123:
- Input Protocol: **sACN**
- Universes: **1, 2, 3, 4**
- Pixel Protocol: **WS2812B**
- Color Order: **GRB**
- DMX Start Address: **1**

### 6. Test

```bash
source ~/rainshine-env/bin/activate
python3 ~/rainshine_dmx.py --preview
```

### 7. Autostart on boot

```bash
sudo cp ~/rainshine.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable rainshine
sudo systemctl start rainshine
```

## Configuration

The config file (`rainshine.conf`) is created automatically on first run:

```ini
[shader]
speed = 4.0
trail = 10
density = 3.0

[output]
fps = 30.0
universe = 1
color_order = grb

[osc]
port = 7700
```

Edit the file and restart the service to apply changes:

```bash
sudo systemctl restart rainshine
```

## Live OSC Control

Shader parameters can be adjusted in real time via OSC on port **7700**.

| OSC Address | Type | Range |
|---|---|---|
| `/rainshine/speed` | float | 0.5 – 15.0 |
| `/rainshine/trail` | int | 1 – 25 |
| `/rainshine/density` | float | 0.5 – 5.0 |
| `/rainshine/fps` | float | 15 – 60 |

### From TouchDesigner

Use an **OSC Out CHOP**:
- Network Address: `PiDMX.local`
- Port: `7700`

### From command line (Mac)

```bash
pip3 install python-osc
python3 -c "from pythonosc.udp_client import SimpleUDPClient; SimpleUDPClient('PiDMX.local', 7700).send_message('/rainshine/speed', 8.0)"
```

## Service Management

```bash
sudo systemctl status rainshine    # Check status
sudo systemctl stop rainshine      # Stop
sudo systemctl restart rainshine   # Restart
sudo systemctl disable rainshine   # Disable autostart
journalctl -u rainshine -f         # View live logs
```

## Pixel Mapping

The LED strip is wired as a single zigzag strip, column-major:
- Column 0: bottom → top (pixels 1–30)
- Column 1: top → bottom (pixels 31–60)
- Column 2: bottom → top, etc.

DMX channels are split across sACN universes at pixel boundaries (510 channels / 170 pixels per universe) to avoid splitting a pixel's RGB values across universes.

## Network

| Device | IP | Purpose |
|---|---|---|
| Pi 5 | 10.0.0.127 | Shader rendering + sACN source |
| ENTTEC OCTO | 10.0.0.123 | sACN → WS2812B pixel driver |
| OLA Web UI | http://PiDMX.local:9090 | DMX universe management |
| ENTTEC Web UI | http://10.0.0.123 | OCTO configuration |
