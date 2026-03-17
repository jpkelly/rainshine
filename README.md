# Rainshine — Pi 5 DMX LED Shader

GLSL rainbow rain shader rendered headlessly on a Raspberry Pi 5, driving WS2812B LEDs via sACN/E1.31 unicast UDP through an ENTTEC Pixel OCTO.

## Hardware

- **Raspberry Pi 5** — Trixie Lite 64-bit, headless
- **ENTTEC Pixel OCTO** — sACN to WS2812B pixel driver (10.0.0.123)
- **WS2812B LED strip** — 10 columns × 30 rows (300 pixels), zigzag wired column-major, GRB color order

## Files

| File | Description |
|---|---|
| `rainshine.frag` | GLSL ES 3.0 fragment shader (rainbow rain effect) |
| `rainshine_dmx.py` | Python host — renders shader, remaps pixels, sends sACN |
| `rainshine.conf` | Config — shader params, sACN destination, brightness, OSC port |
| `rainshine.service` | systemd unit — runs on boot |
| `setup.sh` | One-time Pi setup script |

## Setup

### 1. Install dependencies

```bash
sudo apt update && sudo apt install -y python3-pip python3-venv libegl1-mesa-dev libgles2-mesa-dev mesa-utils
```

### 2. Create Python environment

```bash
python3 -m venv --system-site-packages ~/rainshine-env
source ~/rainshine-env/bin/activate
pip3 install moderngl python-osc numpy
```

### 3. Configure ENTTEC Pixel OCTO

Via http://10.0.0.123:
- Input Protocol: **sACN**
- Universes: **1, 2, 3, 4**
- Pixel Protocol: **WS2812B**
- Color Order: **GRB**
- DMX Start Address: **1**

### 4. Clone the repo

```bash
cd ~
git clone https://github.com/<your-user>/rainshine.git
```

### 5. Test

```bash
source ~/rainshine-env/bin/activate
cd ~/rainshine
python3 rainshine_dmx.py --preview
```

### 6. Autostart on boot

```bash
sudo cp ~/rainshine/rainshine.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable rainshine
sudo systemctl start rainshine
```

## Configuration

`rainshine.conf` is created automatically on first run:

```ini
[shader]
speed = 4.0       # rain speed
trail = 10        # trail length
density = 3.0     # drops per column

[output]
fps = 30.0
universe = 1           # first sACN universe
color_order = grb      # match LED hardware
brightness = 1.0       # 0.0–1.0
sacn_dest = 10.0.0.123 # pixel controller IP

[osc]
port = 7700
```

Apply changes:

```bash
sudo systemctl restart rainshine
```

## Live OSC Control

Parameters can be adjusted in real time via OSC on port **7700**.

| OSC Address | Type | Range |
|---|---|---|
| `/rainshine/speed` | float | 0.5 – 15.0 |
| `/rainshine/trail` | int | 1 – 25 |
| `/rainshine/density` | float | 0.5 – 5.0 |
| `/rainshine/fps` | float | 15 – 60 |
| `/rainshine/brightness` | float | 0.0 – 1.0 |

### From TouchDesigner

OSC Out CHOP → `PiDMX.local:7700`

### From command line

```bash
pip3 install python-osc
python3 -c "from pythonosc.udp_client import SimpleUDPClient; SimpleUDPClient('PiDMX.local', 7700).send_message('/rainshine/speed', 8.0)"
```

## Service Management

```bash
sudo systemctl status rainshine                  # status
sudo systemctl stop rainshine                    # stop
sudo systemctl restart rainshine                 # restart
sudo systemctl disable rainshine                 # disable autostart
journalctl -u rainshine -f                       # live logs
systemctl show rainshine --property=NRestarts    # restart count
```

Redeploy after editing files:

```bash
cd ~/rainshine && git pull
sudo cp rainshine.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl restart rainshine
```

## Monitoring

Status is logged every 5 minutes with frame count, FPS, errors, and RSS:

```
2026-03-17 18:03:38 [INFO] Status: 18001 frames in 300s (60.0 fps), 0 send errors, 0 consecutive errors, RSS 101MB
```

```bash
ssh pi@PiDMX.local 'journalctl -u rainshine -f'
journalctl -u rainshine --since "1 hour ago" --no-pager | grep -E "ERROR|WARNING|Status:"
```

## Error Handling

- **Render/GPU errors** — caught and retried; exits after 50 consecutive failures for clean systemd restart
- **sACN send errors** — caught and retried with backoff
- **RSS watchdog** — exits cleanly if RSS exceeds 400MB; systemd restarts the process
- **Blackout on exit** — sends all-zero DMX data before shutting down

## Pixel Mapping

The strip is wired as a zigzag, column-major:
- Column 0: bottom → top (pixels 1–30)
- Column 1: top → bottom (pixels 31–60)
- Column 2: bottom → top, etc.

Universes are split at 510-channel (170-pixel) boundaries to keep RGB triplets intact.

## Network

| Device | IP | Purpose |
|---|---|---|
| Pi 5 | 10.0.0.127 | Shader rendering + sACN source |
| ENTTEC OCTO | 10.0.0.123 | sACN → WS2812B pixel driver |
| OLA Web UI | http://PiDMX.local:9090 | DMX universe management |
| ENTTEC Web UI | http://10.0.0.123 | OCTO configuration |

## Git Workflow

The project runs from the git repo at `~/rainshine/` on the Pi. After making changes:

```bash
cd ~/rainshine
git add -A
git commit -m "description of changes"
git push
```

If the service file changed, also run the redeploy commands above.
