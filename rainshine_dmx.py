#!/usr/bin/env python3
"""
rainshine_dmx.py — Render rainshine.frag headlessly via moderngl (EGL)
and output pixel data as DMX via sACN/E1.31 (direct UDP).
Uniforms can be set via config file and adjusted live via OSC.

Usage:
    python3 rainshine_dmx.py [--config rainshine.conf] [--osc-port 7700]
                             [--universe 1] [--preview]

OSC addresses:
    /rainshine/speed    float   (e.g. 4.0)
    /rainshine/trail    int     (e.g. 10)
    /rainshine/density  float   (e.g. 3.0)
    /rainshine/fps      float   (e.g. 30.0)

Requires:
    pip3 install moderngl python-osc
"""

import argparse
import configparser
import gc
import logging
import os
import signal
import socket
import struct
import sys
import threading
import time
import uuid

import numpy as np

import moderngl
from pythonosc.dispatcher import Dispatcher
from pythonosc.osc_server import BlockingOSCUDPServer


# ── Grid size ────────────────────────────────────────────────────────────────
COLS = 10
ROWS = 30
NUM_PIXELS = COLS * ROWS       # 300
NUM_CHANNELS = NUM_PIXELS * 3  # 900  → 2 DMX universes

# ── Fullscreen triangle vertex shader ────────────────────────────────────────
VERT_SRC = """#version 300 es
precision mediump float;
out vec2 vUV;
void main() {
    // Produces a full-screen triangle from vertex ID (0,1,2)
    float x = float((gl_VertexID & 1) << 2) - 1.0;
    float y = float((gl_VertexID & 2) << 1) - 1.0;
    vUV = vec2(x, y) * 0.5 + 0.5;
    gl_Position = vec4(x, y, 0.0, 1.0);
}
"""


def load_frag_shader(path="rainshine.frag"):
    with open(path, encoding="utf-8-sig") as f:
        return f.read().lstrip()


def build_pixel_map(cols, rows):
    """
    Returns a list mapping GL pixel index → DMX channel offset.
    Layout: zigzag strip, column-major.
      Col 0: bottom-to-top (pixel 0 at bottom = GL row 0)
      Col 1: top-to-bottom (pixel 30 at top = GL row 29)
      Col 2: bottom-to-top, etc.
    """
    pixel_map = [0] * (cols * rows)
    dmx_idx = 0
    for col in range(cols):
        for step in range(rows):
            if col % 2 == 0:
                # Even columns: bottom-to-top → GL row 0..29
                gl_row = step
            else:
                # Odd columns: top-to-bottom → GL row 29..0
                gl_row = (rows - 1) - step
            pixel_map[gl_row * cols + col] = dmx_idx * 3
            dmx_idx += 1
    return pixel_map


def build_remap_lut(pixel_map, color_order, num_pixels):
    """
    Build a numpy lookup table for fast pixel remapping.
    Returns an array where lut[i] is the source index in the raw pixel buffer
    for DMX byte i.
    """
    r_idx, g_idx, b_idx = color_order
    lut = np.zeros(num_pixels * 3, dtype=np.int32)
    for px_idx in range(num_pixels):
        src = px_idx * 3
        dst = pixel_map[px_idx]
        lut[dst]     = src + r_idx
        lut[dst + 1] = src + g_idx
        lut[dst + 2] = src + b_idx
    return lut


# ── Live parameters (shared between main loop and OSC thread) ────────────────
class Params:
    def __init__(self, speed=4.0, trail=10, density=3.0, fps=30.0, brightness=1.0):
        self.speed = speed
        self.trail = trail
        self.density = density
        self.fps = fps
        self.brightness = brightness
        self.lock = threading.Lock()

    def update(self, **kwargs):
        with self.lock:
            for k, v in kwargs.items():
                if hasattr(self, k):
                    setattr(self, k, v)

    def snapshot(self):
        with self.lock:
            return self.speed, self.trail, self.density, self.fps, self.brightness


DEFAULT_CONFIG = """\
[shader]
speed = 4.0
trail = 10
density = 3.0

[output]
fps = 30.0
universe = 1
# Color order to match your LED hardware (rgb, grb, bgr, etc.)
color_order = grb
# Overall brightness (0.0 – 1.0)
brightness = 1.0
# sACN destination IP (your pixel controller)
sacn_dest = 10.0.0.123

[osc]
port = 7700
"""


COLOR_ORDERS = {
    "rgb": (0, 1, 2),
    "grb": (1, 0, 2),
    "bgr": (2, 1, 0),
    "rbg": (0, 2, 1),
    "gbr": (1, 2, 0),
    "brg": (2, 0, 1),
}


# ── sACN / E1.31 sender (replaces OLA) ──────────────────────────────────────
class SacnSender:
    """Minimal E1.31 (sACN) unicast sender with pre-allocated packets."""

    SACN_PORT = 5568

    def __init__(self, destination, source_name="rainshine"):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.dest = (destination, self.SACN_PORT)
        self._packets = {}
        self._sequences = {}
        self._cid = uuid.uuid4().bytes
        self._source_name = source_name.encode("utf-8")[:63].ljust(64, b"\x00")

    def activate(self, universe):
        """Pre-build the packet template for a universe."""
        pkt = bytearray(638)

        # Root Layer
        struct.pack_into("!HH", pkt, 0, 0x0010, 0x0000)
        pkt[4:16] = b"ASC-E1.17\x00\x00\x00"
        struct.pack_into("!H", pkt, 16, 0x7000 | 622)
        struct.pack_into("!I", pkt, 18, 0x00000004)
        pkt[22:38] = self._cid

        # Framing Layer
        struct.pack_into("!H", pkt, 38, 0x7000 | 600)
        struct.pack_into("!I", pkt, 40, 0x00000002)
        pkt[44:108] = self._source_name
        pkt[108] = 100  # Priority
        struct.pack_into("!H", pkt, 109, 0)  # Sync address
        pkt[111] = 0  # Sequence (updated per send)
        pkt[112] = 0  # Options
        struct.pack_into("!H", pkt, 113, universe)

        # DMP Layer
        struct.pack_into("!H", pkt, 115, 0x7000 | 523)
        pkt[117] = 0x02  # Vector
        pkt[118] = 0xA1  # Address type & data type
        struct.pack_into("!H", pkt, 119, 0)  # First property address
        struct.pack_into("!H", pkt, 121, 1)  # Address increment
        struct.pack_into("!H", pkt, 123, 513)  # Property count (1 start code + 512)
        pkt[125] = 0x00  # DMX start code

        self._packets[universe] = pkt
        self._sequences[universe] = 0

    def send(self, universe, data):
        """Send DMX data (bytes-like or numpy array, up to 512 bytes) to a universe."""
        pkt = self._packets[universe]
        seq = self._sequences[universe]
        pkt[111] = seq
        self._sequences[universe] = (seq + 1) & 0xFF

        n = min(len(data), 512)
        pkt[126:126 + n] = data[:n].tobytes()
        self.sock.sendto(pkt, self.dest)

    def blackout(self, universe):
        """Send all zeros for a universe."""
        pkt = self._packets[universe]
        pkt[111] = self._sequences[universe]
        self._sequences[universe] = (self._sequences[universe] + 1) & 0xFF
        pkt[126:638] = b"\x00" * 512
        self.sock.sendto(pkt, self.dest)

    def close(self):
        self.sock.close()


def load_config(path):
    """Load config file, creating a default one if it doesn't exist."""
    cfg = configparser.ConfigParser()
    if not os.path.exists(path):
        with open(path, "w") as f:
            f.write(DEFAULT_CONFIG)
        log.info("Created default config: %s", path)
    cfg.read(path)
    return cfg


def start_osc_server(params, port):
    """Start an OSC listener in a background thread."""
    dispatcher = Dispatcher()

    def on_speed(address, value):
        params.update(speed=float(value))
        log.info("OSC: speed = %s", value)

    def on_trail(address, value):
        params.update(trail=int(value))
        log.info("OSC: trail = %s", value)

    def on_density(address, value):
        params.update(density=float(value))
        log.info("OSC: density = %s", value)

    def on_fps(address, value):
        params.update(fps=float(value))
        log.info("OSC: fps = %s", value)

    def on_brightness(address, value):
        params.update(brightness=max(0.0, min(1.0, float(value))))
        log.info("OSC: brightness = %s", value)

    dispatcher.map("/rainshine/speed", on_speed)
    dispatcher.map("/rainshine/trail", on_trail)
    dispatcher.map("/rainshine/density", on_density)
    dispatcher.map("/rainshine/fps", on_fps)
    dispatcher.map("/rainshine/brightness", on_brightness)

    server = BlockingOSCUDPServer(("0.0.0.0", port), dispatcher)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("rainshine")


def main():
    parser = argparse.ArgumentParser(description="Rainshine shader → DMX via sACN")
    parser.add_argument("--config", type=str, default="rainshine.conf", help="Config file path")
    parser.add_argument("--universe", type=int, default=None, help="Override sACN universe from config")
    parser.add_argument("--osc-port", type=int, default=None, help="Override OSC port from config")
    parser.add_argument("--shader", type=str, default="rainshine.frag", help="Fragment shader path")
    parser.add_argument("--preview", action="store_true", help="Print ASCII preview to terminal")
    args = parser.parse_args()

    # ── Load config ──────────────────────────────────────────────────────────
    cfg = load_config(args.config)
    speed = cfg.getfloat("shader", "speed", fallback=4.0)
    trail = cfg.getint("shader", "trail", fallback=10)
    density = cfg.getfloat("shader", "density", fallback=3.0)
    fps = cfg.getfloat("output", "fps", fallback=30.0)
    brightness = max(0.0, min(1.0, cfg.getfloat("output", "brightness", fallback=1.0)))
    universe_base = args.universe if args.universe is not None else cfg.getint("output", "universe", fallback=1)
    osc_port = args.osc_port if args.osc_port is not None else cfg.getint("osc", "port", fallback=7700)
    color_order_name = cfg.get("output", "color_order", fallback="grb").lower()
    color_order = COLOR_ORDERS.get(color_order_name, (1, 0, 2))
    sacn_dest = cfg.get("output", "sacn_dest", fallback="10.0.0.123")
    log.info("Color order: %s", color_order_name.upper())

    # ── Live params ──────────────────────────────────────────────────────────
    params = Params(speed=speed, trail=trail, density=density, fps=fps, brightness=brightness)

    # ── GL context (headless EGL, OpenGL ES 3.1) ────────────────────────────
    ctx = moderngl.create_standalone_context(backend="egl", libgl="libGLESv2.so", require=310)
    fbo = ctx.framebuffer(color_attachments=[ctx.renderbuffer((COLS, ROWS))])

    frag_src = load_frag_shader(args.shader)
    prog = ctx.program(vertex_shader=VERT_SRC, fragment_shader=frag_src)

    # Empty VAO for the full-screen triangle (3 vertices, no attributes)
    vao = ctx.vertex_array(prog, [])

    # ── Pixel → DMX mapping ──────────────────────────────────────────────────
    pixel_map = build_pixel_map(COLS, ROWS)

    # ── sACN sender (direct E1.31 over UDP) ──────────────────────────────────
    CHAN_PER_UNI = 510
    num_universes = (NUM_CHANNELS + CHAN_PER_UNI - 1) // CHAN_PER_UNI
    sender = SacnSender(sacn_dest)
    for u in range(num_universes):
        sender.activate(universe_base + u)
    log.info("sACN sender → %s (universes %d–%d)", sacn_dest, universe_base, universe_base + num_universes - 1)

    # ── OSC server ───────────────────────────────────────────────────────────
    osc_server = start_osc_server(params, osc_port)
    log.info("OSC listening on port %d", osc_port)

    # ── Graceful shutdown ────────────────────────────────────────────────────
    running = True

    def stop(sig, frame):
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    # ── Main render loop ─────────────────────────────────────────────────────
    t0 = time.perf_counter()
    remap_lut = build_remap_lut(pixel_map, color_order, NUM_PIXELS)
    log.info("Rainshine DMX running — %dx%d → sACN universe %d+", COLS, ROWS, universe_base)
    consecutive_errors = 0
    MAX_CONSECUTIVE_ERRORS = 50
    RSS_MAX_MB = 400  # exit cleanly if RSS exceeds this (systemd restarts us)
    STATUS_LOG_INTERVAL = 300  # log status every 5 minutes
    last_status_log = time.perf_counter()
    frame_count = 0
    send_errors = 0

    # Pre-allocate ALL buffers to avoid per-frame allocation
    raw_buf = bytearray(COLS * ROWS * 3)
    raw_view = np.frombuffer(raw_buf, dtype=np.uint8)
    dmx_buf = np.zeros(NUM_CHANNELS, dtype=np.uint8)
    dmx_scaled = np.zeros(NUM_CHANNELS, dtype=np.uint8)

    while running:
        frame_start = time.perf_counter()
        t = frame_start - t0

        # Read live params
        speed, trail, density, fps, brightness = params.snapshot()
        frame_dur = 1.0 / fps

        try:
            # Update uniforms
            prog["uTime"].value = t
            if "uSpeed" in prog:
                prog["uSpeed"].value = speed
            if "uTrailLen" in prog:
                prog["uTrailLen"].value = trail
            if "uDensity" in prog:
                prog["uDensity"].value = density

            fbo.use()
            vao.render(mode=moderngl.TRIANGLES, vertices=3)

            # read_into implicitly waits for GPU completion
            fbo.read_into(raw_buf, components=3, alignment=1)
            np.take(raw_view, remap_lut, out=dmx_buf)

            # Apply brightness scaling
            if brightness < 1.0:
                np.multiply(dmx_buf, brightness, out=dmx_scaled, casting="unsafe")
            else:
                np.copyto(dmx_scaled, dmx_buf)
        except Exception:
            log.exception("Render/readback failed")
            consecutive_errors += 1
            if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                log.error("Too many consecutive render errors (%d), exiting", consecutive_errors)
                break
            time.sleep(0.1)
            continue

        try:
            # Split across universes on pixel boundaries (510 = 170 pixels × 3)
            for u in range(num_universes):
                start = u * CHAN_PER_UNI
                end = min(start + CHAN_PER_UNI, NUM_CHANNELS)
                sender.send(universe_base + u, dmx_scaled[start:end])
        except Exception:
            log.exception("sACN send failed")
            send_errors += 1
            consecutive_errors += 1
            if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                log.error("Too many consecutive errors (%d), exiting", consecutive_errors)
                break
            time.sleep(0.1)
            continue

        consecutive_errors = 0
        frame_count += 1

        # Periodic status log
        now = time.perf_counter()
        if now - last_status_log >= STATUS_LOG_INTERVAL:
            elapsed = now - last_status_log
            actual_fps = frame_count / elapsed if elapsed > 0 else 0
            rss_mb = 0
            try:
                with open("/proc/self/status") as f:
                    for line in f:
                        if line.startswith("VmRSS:"):
                            rss_mb = int(line.split()[1]) // 1024
                            break
            except OSError:
                pass
            log.info(
                "Status: %d frames in %.0fs (%.1f fps), %d send errors, %d consecutive errors, RSS %dMB",
                frame_count, elapsed, actual_fps, send_errors, consecutive_errors, rss_mb,
            )
            gc.collect()
            frame_count = 0
            send_errors = 0
            last_status_log = now

            # Self-check: exit if RSS exceeds threshold (systemd restarts us)
            if rss_mb > RSS_MAX_MB:
                log.warning("RSS %dMB exceeds limit %dMB, exiting for clean restart", rss_mb, RSS_MAX_MB)
                break

        # Optional ASCII preview
        if args.preview:
            preview_lines = []
            for row in range(ROWS - 1, -1, -1):
                line = ""
                for col in range(COLS):
                    px = (row * COLS + col) * 3
                    r, g, b = raw_view[px], raw_view[px + 1], raw_view[px + 2]
                    avg = (r + g + b) // 3
                    if avg > 180:
                        line += "#"
                    elif avg > 80:
                        line += "+"
                    elif avg > 20:
                        line += "."
                    else:
                        line += " "
                preview_lines.append(line)
            sys.stdout.write("\033[H\033[2J")  # clear terminal
            sys.stdout.write("\n".join(preview_lines) + "\n")
            sys.stdout.flush()

        # Frame pacing: sleep most of the wait, then spin-wait for precision
        remaining = frame_dur - (time.perf_counter() - frame_start)
        if remaining > 0.002:
            time.sleep(remaining - 0.001)
        while time.perf_counter() - frame_start < frame_dur:
            pass

    # Blackout on exit
    for u in range(num_universes):
        sender.blackout(universe_base + u)
    sender.close()
    log.info("Blackout sent. Exiting.")


if __name__ == "__main__":
    main()
