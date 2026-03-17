#!/usr/bin/env python3
"""
rainshine_dmx.py — Render rainshine.frag headlessly via moderngl (EGL)
and output pixel data as DMX through OLA (sACN/E1.31).
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
    sudo apt install ola
"""

import argparse
import array
import configparser
import logging
import os
import signal
import subprocess
import sys
import threading
import time

import numpy as np

import moderngl
from ola.ClientWrapper import ClientWrapper
from pythonosc.dispatcher import Dispatcher
from pythonosc.osc_server import BlockingOSCUDPServer

try:
    import sdnotify
    _notifier = sdnotify.SystemdNotifier()
except ImportError:
    _notifier = None


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
    def __init__(self, speed=4.0, trail=10, density=3.0, fps=30.0):
        self.speed = speed
        self.trail = trail
        self.density = density
        self.fps = fps
        self.lock = threading.Lock()

    def update(self, **kwargs):
        with self.lock:
            for k, v in kwargs.items():
                if hasattr(self, k):
                    setattr(self, k, v)

    def snapshot(self):
        with self.lock:
            return self.speed, self.trail, self.density, self.fps


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

    dispatcher.map("/rainshine/speed", on_speed)
    dispatcher.map("/rainshine/trail", on_trail)
    dispatcher.map("/rainshine/density", on_density)
    dispatcher.map("/rainshine/fps", on_fps)

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
    parser = argparse.ArgumentParser(description="Rainshine shader → DMX via OLA")
    parser.add_argument("--config", type=str, default="rainshine.conf", help="Config file path")
    parser.add_argument("--universe", type=int, default=None, help="Override OLA universe from config")
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
    universe_base = args.universe if args.universe is not None else cfg.getint("output", "universe", fallback=1)
    osc_port = args.osc_port if args.osc_port is not None else cfg.getint("osc", "port", fallback=7700)
    color_order_name = cfg.get("output", "color_order", fallback="grb").lower()
    color_order = COLOR_ORDERS.get(color_order_name, (1, 0, 2))
    log.info("Color order: %s", color_order_name.upper())

    # ── Live params ──────────────────────────────────────────────────────────
    params = Params(speed=speed, trail=trail, density=density, fps=fps)

    # ── GL context (headless EGL, OpenGL ES 3.1) ────────────────────────────
    ctx = moderngl.create_standalone_context(backend="egl", libgl="libGLESv2.so", require=310)
    fbo = ctx.framebuffer(color_attachments=[ctx.renderbuffer((COLS, ROWS))])

    frag_src = load_frag_shader(args.shader)
    prog = ctx.program(vertex_shader=VERT_SRC, fragment_shader=frag_src)

    # Empty VAO for the full-screen triangle (3 vertices, no attributes)
    vao = ctx.vertex_array(prog, [])

    # ── Pixel → DMX mapping ──────────────────────────────────────────────────
    pixel_map = build_pixel_map(COLS, ROWS)

    # ── OLA setup ────────────────────────────────────────────────────────────
    def create_ola_client():
        w = ClientWrapper()
        return w, w.Client()

    def ensure_olad():
        """Restart olad if it's not running."""
        result = subprocess.run(
            ["systemctl", "is-active", "--quiet", "olad"],
            capture_output=True,
        )
        if result.returncode != 0:
            log.warning("olad is not active, restarting it")
            subprocess.run(["sudo", "systemctl", "restart", "olad"],
                           capture_output=True, timeout=15)
            time.sleep(2)  # give olad time to initialize

    wrapper, client = create_ola_client()

    # ── OSC server ───────────────────────────────────────────────────────────
    osc_server = start_osc_server(params, osc_port)
    log.info("OSC listening on port %d", osc_port)

    # Notify systemd we're ready
    if _notifier:
        _notifier.notify("READY=1")
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
    log.info("Rainshine DMX running — %dx%d → OLA universe %d+", COLS, ROWS, universe_base)
    consecutive_errors = 0
    MAX_CONSECUTIVE_ERRORS = 50
    OLA_HEALTH_INTERVAL = 60  # seconds between OLA health checks
    last_ola_health_check = time.perf_counter()

    while running:
        frame_start = time.perf_counter()
        t = frame_start - t0

        # Read live params
        speed, trail, density, fps = params.snapshot()
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
            ctx.finish()  # ensure GPU is done before readback

            # Read back pixels and remap via numpy LUT
            raw = np.frombuffer(fbo.read(components=3, alignment=1), dtype=np.uint8)
            dmx_data = raw[remap_lut]
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
            # to avoid splitting a pixel's RGB across two universes
            CHAN_PER_UNI = 510
            num_universes = (NUM_CHANNELS + CHAN_PER_UNI - 1) // CHAN_PER_UNI
            for u in range(num_universes):
                start = u * CHAN_PER_UNI
                end = min(start + CHAN_PER_UNI, NUM_CHANNELS)
                chunk = np.zeros(512, dtype=np.uint8)
                chunk[:end - start] = dmx_data[start:end]
                data = array.array("B", chunk.tobytes())
                client.SendDmx(universe_base + u, data)
        except Exception:
            log.exception("OLA SendDmx failed, reconnecting")
            try:
                ensure_olad()
                wrapper, client = create_ola_client()
                log.info("OLA reconnected")
            except Exception:
                log.exception("OLA reconnect failed")
            consecutive_errors += 1
            if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                log.error("Too many consecutive errors (%d), exiting", consecutive_errors)
                break
            time.sleep(0.5)
            continue

        consecutive_errors = 0

        # Ping systemd watchdog
        if _notifier:
            _notifier.notify("WATCHDOG=1")

        # Periodic OLA health check — reconnect proactively if olad died
        now = time.perf_counter()
        if now - last_ola_health_check >= OLA_HEALTH_INTERVAL:
            last_ola_health_check = now
            try:
                ensure_olad()
                wrapper, client = create_ola_client()
            except Exception:
                log.exception("OLA periodic health check failed")

        # Optional ASCII preview
        if args.preview:
            preview_lines = []
            for row in range(ROWS - 1, -1, -1):
                line = ""
                for col in range(COLS):
                    px = (row * COLS + col) * 3
                    r, g, b = raw[px], raw[px + 1], raw[px + 2]
                    brightness = (r + g + b) // 3
                    if brightness > 180:
                        line += "#"
                    elif brightness > 80:
                        line += "+"
                    elif brightness > 20:
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
    black = array.array("B", [0] * 512)
    num_universes = (NUM_CHANNELS + 509) // 510
    for u in range(num_universes):
        client.SendDmx(universe_base + u, black)
    log.info("Blackout sent. Exiting.")


if __name__ == "__main__":
    main()
