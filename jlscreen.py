#!/usr/bin/env python3
"""
jlscreen - system monitor for the Jungle Leopard 9.16" USB mini screen on Linux.

Reads CPU / GPU / memory / fan / disk / network data from the Linux kernel
(hwmon via psutil, sysfs, nvidia-smi), draws a 1920 x 462 dashboard and
streams it to the screen as JPEG frames over the screen's USB serial link.

Protocol (Jungle Leopard / Hongtai firmware, USB CDC-ACM, VID 0x33C3):
  * Command frame: 55 AA LEN_L LEN_H CMD PAYLOAD... CHK_L CHK_H
      LEN = 7 + len(PAYLOAD) (whole frame, little-endian)
      CHK = 16-bit sum of every byte before the checksum, little-endian
  * Commands: 0x03 brightness (1 byte 0-100), 0x06 get info (JSON reply),
              0x11 live / keep-alive (must be sent at least every 1.5 s),
              0x21 close (firmware 3.1+)
  * Live JPEG, firmware < 2.8 ("raw"):      the JPEG bytes as they are
  * Live JPEG, firmware >= 2.8 ("envelope"): SIZE(LE32) JPEG SUM(LE16) FF D9 FF D9
Protocol details credit: nickadam/jungle-leopard (Go, MIT) and
Selflocking/InfoPanel-linux (JlPanel driver), both reverse-engineered from the
vendor's Windows application.
"""

import argparse
import configparser
import json
import math
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
from collections import deque

try:
    import psutil
except ImportError:
    sys.exit("psutil missing: sudo apt install python3-psutil")
try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    sys.exit("Pillow missing: sudo apt install python3-pil")
try:
    import serial
    import serial.tools.list_ports
except ImportError:
    serial = None  # only needed when talking to the screen

VERSION = "1.0.0"

JL_VID = 0x33C3
BAUD = 2_000_000

CMD_RESTART = 0x01
CMD_BRIGHTNESS = 0x03
CMD_GET_INFO = 0x06
CMD_LIVE = 0x11
CMD_CLOSE = 0x21

EOI_TERMINATOR = b"\xFF\xD9\xFF\xD9"
PIPELINE_RESET = b"\xFF\xD9\xFF\xD9\x00\x00\x00\x00"

PANEL_W, PANEL_H = 1920, 462

DEFAULT_CONFIG = """
[display]
# Serial port of the screen, or "auto" to find it by its USB vendor ID (33c3)
port = auto
# Rotation (degrees anticlockwise) applied before sending. The 9.16" panel
# needs 90 to read upright in landscape. Try 0, 180 or 270 if yours differs.
rotate = 90
# Backlight 0-100
brightness = 80
# Seconds between dashboard updates
interval = 1.0
# Largest JPEG the firmware accepts (kB); quality drops until a frame fits
max_kb = 80
quality = 88
# auto (pick from firmware version), raw (firmware < 2.8) or envelope (2.8+)
protocol = auto

[sensors]
# CPU temperature sensor as chip:label, e.g. k10temp:Tctl or coretemp:Package id 0
cpu_temp = auto
# Fans to show. "auto" lists every fan that is spinning. Or give a list:
#   fans = nct6799:fan1=CPU, nct6799:fan2=Pump, nct6799:fan3=Front
fans = auto
# auto, nvidia, amd or none
gpu = auto
# Mount points to show (comma separated)
disks = /
# Network interface, or auto for all non-loopback interfaces added together
net = auto

[look]
# Title shown in the system panel; blank = hostname
title =
"""


# --------------------------------------------------------------------------
# Protocol
# --------------------------------------------------------------------------

def build_frame(cmd, payload=b""):
    total = 7 + len(payload)
    body = bytes([0x55, 0xAA, total & 0xFF, (total >> 8) & 0xFF, cmd]) + bytes(payload)
    chk = sum(body) & 0xFFFF
    return body + bytes([chk & 0xFF, (chk >> 8) & 0xFF])


def extract_frame(buf):
    """Return (frame, rest) for the first complete 55 AA frame in buf."""
    while True:
        i = buf.find(b"\x55\xAA")
        if i < 0:
            return None, buf[-1:]
        buf = buf[i:]
        if len(buf) < 4:
            return None, buf
        flen = buf[2] | (buf[3] << 8)
        if flen < 7 or flen > 65536:
            buf = buf[2:]
            continue
        if len(buf) < flen:
            return None, buf
        return buf[:flen], buf[flen:]


def build_envelope(jpeg):
    """Firmware 2.8+ live frame: SIZE(LE32) JPEG SUM(LE16) then FF D9 FF D9."""
    n = len(jpeg)
    head = bytes([n & 0xFF, (n >> 8) & 0xFF, (n >> 16) & 0xFF, (n >> 24) & 0xFF])
    s = (sum(head) + sum(jpeg)) & 0xFFFF
    return head + jpeg + bytes([s & 0xFF, (s >> 8) & 0xFF]) + EOI_TERMINATOR


def parse_version(v):
    nums = re.findall(r"\d+", str(v or ""))
    return tuple(int(x) for x in nums[:3]) if nums else ()


def find_port():
    if serial is None:
        return None
    ports = list(serial.tools.list_ports.comports())
    for p in ports:
        if p.vid == JL_VID:
            return p.device
    acm = [p.device for p in ports if "ttyACM" in p.device]
    return acm[0] if len(acm) == 1 else None


class JLScreen:
    def __init__(self, port, protocol="auto", verbose=False):
        if serial is None:
            raise RuntimeError("pyserial missing: sudo apt install python3-serial")
        self.verbose = verbose
        self.port_name = port
        self.ser = serial.Serial(port, BAUD, timeout=0.05, write_timeout=5)
        try:
            self.ser.dtr = True
            self.ser.rts = True
        except (OSError, serial.SerialException):
            pass
        time.sleep(0.2)
        self.ser.reset_input_buffer()
        self.info = self.get_info() or {}
        self.version = parse_version(self.info.get("version"))
        if protocol == "auto":
            self.protocol = "envelope" if self.version and self.version >= (2, 8) else "raw"
        else:
            self.protocol = protocol
        if self.protocol == "envelope":
            self.ser.write(PIPELINE_RESET)
            time.sleep(0.2)
            self.ser.reset_input_buffer()
        self.send(CMD_LIVE)
        time.sleep(0.05)

    def log(self, msg):
        if self.verbose:
            print(msg, file=sys.stderr)

    def send(self, cmd, payload=b""):
        f = build_frame(cmd, payload)
        self.log("TX " + f.hex())
        self.ser.write(f)

    def get_info(self, timeout=3.0):
        self.ser.reset_input_buffer()
        self.send(CMD_GET_INFO)
        buf = b""
        end = time.time() + timeout
        while time.time() < end:
            buf += self.ser.read(512)
            frame, _ = extract_frame(buf)
            if frame:
                self.log("RX " + frame.hex())
                text = frame[5:-2].decode("utf-8", "replace").strip("\x00 \r\n")
                try:
                    j = json.loads(text)
                except ValueError:
                    return {"raw": text}
                if isinstance(j, dict) and isinstance(j.get("data"), dict):
                    merged = dict(j["data"])
                    merged.setdefault("status", j.get("status"))
                    return merged
                return j if isinstance(j, dict) else {"raw": j}
        return None

    def set_brightness(self, level):
        self.send(CMD_BRIGHTNESS, bytes([max(0, min(100, int(level)))]))

    def keepalive(self):
        self.send(CMD_LIVE)

    def send_jpeg(self, jpeg):
        if self.protocol == "envelope":
            self.keepalive()
            self.ser.write(build_envelope(jpeg))
        else:
            # drop any half-sent previous frame first, then keep-alive + frame
            self.ser.reset_output_buffer()
            self.keepalive()
            self.ser.write(jpeg)
        self.ser.flush()

    def close(self):
        try:
            if self.version >= (3, 1):
                self.send(CMD_CLOSE)
            elif self.protocol == "envelope":
                self.ser.write(EOI_TERMINATOR)
            self.ser.flush()
        except Exception:
            pass
        try:
            self.ser.close()
        except Exception:
            pass


# --------------------------------------------------------------------------
# Sensors
# --------------------------------------------------------------------------

CPU_CHIPS = ["k10temp", "zenpower", "coretemp", "cpu_thermal", "cpu-thermal",
             "soc_thermal", "acpitz"]
CPU_LABEL_PREF = ["Tctl", "Tdie", "Package id 0", "CPU", ""]


def read_file(path, default=None):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return default


def cpu_model():
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.lower().startswith("model name"):
                    name = line.split(":", 1)[1].strip()
                    name = re.sub(r"\(R\)|\(TM\)|\(tm\)", "", name)
                    name = re.sub(r"\s+\d+-Core Processor", "", name)
                    name = re.sub(r"\s+CPU\s+@.*", "", name)
                    return re.sub(r"\s+", " ", name).strip()
    except OSError:
        pass
    return "CPU"


class RaplReader:
    """CPU package power from the powercap interface (needs root to read)."""

    def __init__(self):
        self.path = None
        for base in ("/sys/class/powercap/intel-rapl:0", "/sys/class/powercap/amd-rapl:0"):
            p = base + "/energy_uj"
            if read_file(p) is not None:
                self.path = p
                self.max = int(read_file(base + "/max_energy_range_uj", "0") or 0)
                break
        self.last = None

    def read(self):
        if not self.path:
            return None
        e = read_file(self.path)
        if e is None:
            return None
        now, e = time.time(), int(e)
        out = None
        if self.last:
            de = e - self.last[1]
            if de < 0 and self.max:
                de += self.max
            dt = now - self.last[0]
            if dt > 0 and de >= 0:
                out = de / dt / 1e6
        self.last = (now, e)
        return out


def nvidia_stats():
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None
    q = "name,temperature.gpu,utilization.gpu,memory.used,memory.total,power.draw,fan.speed,clocks.gr"
    try:
        out = subprocess.run([exe, "--query-gpu=" + q, "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    line = out.strip().splitlines()[0] if out.strip() else ""
    parts = [p.strip() for p in line.split(",")]
    if len(parts) < 8:
        return None

    def num(x):
        try:
            return float(x)
        except ValueError:
            return None
    return {
        "name": parts[0].replace("NVIDIA ", "").replace("GeForce ", ""),
        "temp": num(parts[1]), "load": num(parts[2]),
        "mem_used": (num(parts[3]) or 0) / 1024, "mem_total": (num(parts[4]) or 0) / 1024,
        "power": num(parts[5]), "fan": num(parts[6]), "clock": num(parts[7]),
        "fan_unit": "%",
    }


def amd_gpu_card():
    best, best_vram = None, -1
    base = "/sys/class/drm"
    try:
        cards = sorted(c for c in os.listdir(base) if re.fullmatch(r"card\d+", c))
    except OSError:
        return None
    for c in cards:
        dev = f"{base}/{c}/device"
        if read_file(dev + "/vendor") != "0x1002":
            continue
        vram = int(read_file(dev + "/mem_info_vram_total", "0") or 0)
        if vram > best_vram:
            best, best_vram = dev, vram
    return best


def amd_stats(dev):
    hw = None
    try:
        hws = os.listdir(dev + "/hwmon")
        hw = dev + "/hwmon/" + hws[0] if hws else None
    except OSError:
        pass

    def hwval(name, scale):
        if not hw:
            return None
        v = read_file(f"{hw}/{name}")
        return int(v) / scale if v and v.lstrip("-").isdigit() else None

    temp = hwval("temp2_input", 1000) or hwval("temp1_input", 1000)  # junction, else edge
    power = hwval("power1_average", 1e6) or hwval("power1_input", 1e6)
    load = read_file(dev + "/gpu_busy_percent")
    used = int(read_file(dev + "/mem_info_vram_used", "0") or 0) / 2**30
    total = int(read_file(dev + "/mem_info_vram_total", "0") or 0) / 2**30
    clock = None
    sclk = read_file(dev + "/pp_dpm_sclk")
    if sclk:
        for line in sclk.splitlines():
            if line.endswith("*"):
                m = re.search(r"(\d+)\s*Mhz", line, re.I)
                clock = float(m.group(1)) if m else None
    card = os.path.basename(os.path.dirname(dev))
    name = read_file(dev + "/product_name") or f"AMD Radeon ({card})"
    return {
        "name": name.replace("AMD ", ""), "temp": temp,
        "load": float(load) if load and load.isdigit() else None,
        "mem_used": used, "mem_total": total, "power": power,
        "fan": hwval("fan1_input", 1), "clock": clock, "fan_unit": "rpm",
    }


class Sensors:
    def __init__(self, cfg):
        self.cfg = cfg
        self.cpu_name = cpu_model()
        self.cores = psutil.cpu_count() or 1
        self.rapl = RaplReader()
        self.hist_cpu_t = deque(maxlen=180)
        self.hist_cpu_l = deque(maxlen=180)
        self.hist_gpu_t = deque(maxlen=180)
        self.hist_ram = deque(maxlen=180)
        self.fan_max = {}
        self.last_net = None
        self.gpu_mode = cfg.get("sensors", "gpu", fallback="auto").lower()
        self.amd_dev = amd_gpu_card() if self.gpu_mode in ("auto", "amd") else None
        psutil.cpu_percent(percpu=True)  # prime the counters
        self.fan_spec = self._parse_fans(cfg.get("sensors", "fans", fallback="auto"))

    @staticmethod
    def _parse_fans(spec):
        spec = spec.strip()
        if not spec or spec.lower() == "auto":
            return None
        out = []
        for item in spec.split(","):
            item = item.strip()
            if not item:
                continue
            key, _, name = item.partition("=")
            chip, _, label = key.strip().partition(":")
            out.append((chip.strip(), label.strip(), (name or label or chip).strip()))
        return out

    def cpu_temp(self, temps):
        want = self.cfg.get("sensors", "cpu_temp", fallback="auto").strip()
        if want and want.lower() != "auto":
            chip, _, label = want.partition(":")
            for e in temps.get(chip, []):
                if not label or e.label == label:
                    return e.current
            return None
        for chip in CPU_CHIPS:
            entries = temps.get(chip)
            if not entries:
                continue
            for lab in CPU_LABEL_PREF:
                for e in entries:
                    if e.label == lab:
                        return e.current
            return max(e.current for e in entries)
        return None

    def fans(self):
        try:
            raw = psutil.sensors_fans()
        except (AttributeError, OSError):
            raw = {}
        rows = []
        if self.fan_spec:
            for chip, label, name in self.fan_spec:
                rpm = None
                for i, e in enumerate(raw.get(chip, [])):
                    lab = e.label or f"fan{i + 1}"
                    if lab == label or (not label and i == 0):
                        rpm = e.current
                        break
                rows.append((name, rpm))
        else:
            for chip, entries in raw.items():
                for i, e in enumerate(entries):
                    if e.current and e.current > 0:
                        rows.append((e.label or f"{chip} fan{i + 1}", e.current))
        for name, rpm in rows:
            if rpm:
                self.fan_max[name] = max(self.fan_max.get(name, 1500), rpm)
        return [(n, r, self.fan_max.get(n, 1500)) for n, r in rows]

    def gpu(self):
        if self.gpu_mode == "none":
            return None
        if self.gpu_mode in ("auto", "nvidia"):
            g = nvidia_stats()
            if g:
                return g
        if self.amd_dev:
            return amd_stats(self.amd_dev)
        return None

    def net(self):
        want = self.cfg.get("sensors", "net", fallback="auto").strip()
        counters = psutil.net_io_counters(pernic=True)
        rx = tx = 0
        for nic, c in counters.items():
            if want.lower() != "auto":
                if nic != want:
                    continue
            elif nic == "lo" or nic.startswith(("docker", "veth", "br-", "virbr")):
                continue
            rx += c.bytes_recv
            tx += c.bytes_sent
        now = time.time()
        down = up = 0.0
        if self.last_net:
            dt = now - self.last_net[0]
            if dt > 0:
                down = max(0.0, (rx - self.last_net[1]) / dt)
                up = max(0.0, (tx - self.last_net[2]) / dt)
        self.last_net = (now, rx, tx)
        return down, up

    def disks(self):
        out = []
        for m in self.cfg.get("sensors", "disks", fallback="/").split(","):
            m = m.strip()
            if not m:
                continue
            try:
                u = psutil.disk_usage(m)
                out.append((m, u.used / 2**30, u.total / 2**30, u.percent))
            except OSError:
                pass
        return out

    def collect(self):
        try:
            temps = psutil.sensors_temperatures()
        except (AttributeError, OSError):
            temps = {}
        per = psutil.cpu_percent(percpu=True)
        load = sum(per) / len(per) if per else 0.0
        freq = None
        try:
            fr = psutil.cpu_freq(percpu=True)
            if fr:
                freq = max(f.current for f in fr) / 1000.0
        except (OSError, AttributeError, ValueError):
            pass
        ct = self.cpu_temp(temps)
        vm = psutil.virtual_memory()
        sw = psutil.swap_memory()
        g = self.gpu()
        down, up = self.net()
        s = {
            "cpu_name": self.cpu_name, "cpu_load": load, "cpu_per": per,
            "cpu_temp": ct, "cpu_freq": freq, "cpu_power": self.rapl.read(),
            "load_avg": os.getloadavg(),
            "ram_used": (vm.total - vm.available) / 2**30, "ram_total": vm.total / 2**30,
            "ram_pct": vm.percent, "swap_used": sw.used / 2**30,
            "swap_total": sw.total / 2**30,
            "gpu": g, "fans": self.fans(), "disks": self.disks(),
            "net_down": down, "net_up": up,
            "uptime": time.time() - psutil.boot_time(),
        }
        self.hist_cpu_l.append(load)
        if ct is not None:
            self.hist_cpu_t.append(ct)
        if g and g.get("temp") is not None:
            self.hist_gpu_t.append(g["temp"])
        self.hist_ram.append(vm.percent)
        s["hist_cpu_t"] = list(self.hist_cpu_t)
        s["hist_cpu_l"] = list(self.hist_cpu_l)
        s["hist_gpu_t"] = list(self.hist_gpu_t)
        s["hist_ram"] = list(self.hist_ram)
        return s


def demo_stats(t):
    """Plausible values for previewing the layout on a machine without sensors."""
    n = 24
    per = [max(2, min(100, 35 + 30 * math.sin(t / 3 + i) + (i % 5) * 6)) for i in range(n)]
    ht = [55 + 12 * math.sin(i / 14) + 4 * math.sin(i / 3) for i in range(180)]
    return {
        "cpu_name": "AMD Ryzen 9 9900X", "cpu_load": sum(per) / n, "cpu_per": per,
        "cpu_temp": ht[-1], "cpu_freq": 5.42, "cpu_power": 118.4, "load_avg": (6.2, 5.8, 4.9),
        "ram_used": 41.7, "ram_total": 125.2, "ram_pct": 33.3, "swap_used": 0.2, "swap_total": 8.0,
        "gpu": {"name": "RTX 4070 SUPER", "temp": 61, "load": 74, "mem_used": 7.9,
                "mem_total": 12.0, "power": 182, "fan": 46, "clock": 2610, "fan_unit": "%"},
        "fans": [("CPU", 1240, 1800), ("Pump", 2710, 3000), ("Front 1", 980, 1600),
                 ("Front 2", 975, 1600), ("Rear", 1100, 1600), ("Top", 860, 1600)],
        "disks": [("/", 412.5, 1862.0, 22.2), ("/data", 3120.0, 3725.0, 83.8)],
        "net_down": 2.4e6, "net_up": 3.1e5, "uptime": 3 * 86400 + 5 * 3600 + 17 * 60,
        "hist_cpu_t": ht, "hist_cpu_l": [40 + 25 * math.sin(i / 9) for i in range(180)],
        "hist_gpu_t": [58 + 5 * math.sin(i / 11) for i in range(180)],
        "hist_ram": [33 + 2 * math.sin(i / 20) for i in range(180)],
    }


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

BG = (8, 11, 16)
PANEL = (17, 23, 31)
TRACK = (34, 43, 55)
TEXT = (228, 233, 240)
DIM = (128, 140, 156)
C_CPU = (255, 138, 61)
C_GPU = (70, 209, 140)
C_RAM = (155, 123, 255)
C_FAN = (61, 200, 255)
C_SYS = (240, 200, 80)

FONT_CANDIDATES = {
    "bold": ["/usr/share/fonts/truetype/ubuntu/Ubuntu-B.ttf",
             "/usr/share/fonts/truetype/ubuntu/Ubuntu[wdth,wght].ttf",
             "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"],
    "regular": ["/usr/share/fonts/truetype/ubuntu/Ubuntu-R.ttf",
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"],
    "cond": ["/usr/share/fonts/truetype/ubuntu/Ubuntu-C.ttf",
             "/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed-Bold.ttf",
             "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"],
}
_font_cache = {}


def font(kind, size):
    key = (kind, size)
    if key not in _font_cache:
        f = None
        for path in FONT_CANDIDATES[kind]:
            if os.path.exists(path):
                try:
                    f = ImageFont.truetype(path, size)
                    break
                except OSError:
                    continue
        _font_cache[key] = f or ImageFont.load_default(size)
    return _font_cache[key]


def temp_colour(t):
    if t is None:
        return DIM
    if t < 60:
        return (80, 220, 130)
    if t < 75:
        return (250, 196, 60)
    if t < 85:
        return (255, 130, 50)
    return (255, 70, 70)


def lerp(a, b, f):
    return tuple(int(a[i] + (b[i] - a[i]) * f) for i in range(3))


def human_rate(bps):
    for unit, div in (("GB/s", 1e9), ("MB/s", 1e6), ("kB/s", 1e3)):
        if bps >= div:
            return f"{bps / div:.1f} {unit}"
    return f"{bps:.0f} B/s"


def fmt_uptime(sec):
    d, rem = divmod(int(sec), 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    return f"{d}d {h}h {m:02d}m" if d else f"{h}h {m:02d}m"


class Canvas:
    def __init__(self):
        self.img = Image.new("RGB", (PANEL_W, PANEL_H), BG)
        self.d = ImageDraw.Draw(self.img)

    def text(self, xy, s, f, fill=TEXT, anchor="la"):
        self.d.text(xy, s, font=f, fill=fill, anchor=anchor)

    def width(self, s, f):
        return self.d.textlength(s, font=f)

    def fit(self, s, f, maxw):
        if self.width(s, f) <= maxw:
            return s
        while s and self.width(s + "…", f) > maxw:
            s = s[:-1]
        return s + "…"

    def panel(self, x, y, w, h, title, sub, colour):
        self.d.rounded_rectangle((x, y, x + w, y + h), radius=18, fill=PANEL)
        self.d.rounded_rectangle((x + 18, y + 18, x + 24, y + 46), radius=3, fill=colour)
        self.text((x + 36, y + 14), title, font("bold", 30), colour)
        if sub:
            tw = self.width(title, font("bold", 30))
            self.text((x + 46 + tw, y + 22), self.fit(sub, font("regular", 21), w - tw - 70),
                      font("regular", 21), DIM)

    def bar(self, x, y, w, h, frac, colour, track=TRACK):
        frac = 0.0 if frac is None else max(0.0, min(1.0, frac))
        r = h // 2
        self.d.rounded_rectangle((x, y, x + w, y + h), radius=r, fill=track)
        if frac > 0:
            self.d.rounded_rectangle((x, y, x + max(h, int(w * frac)), y + h), radius=r, fill=colour)

    def _arc_poly(self, cx, cy, r, width, a0, a1, colour):
        """Thick arc with rounded ends, drawn as a polygon (PIL's arc looks poor when short)."""
        if a1 - a0 < 0.5:
            a1 = a0 + 0.5
        ro, ri = r, r - width
        steps = max(4, int((a1 - a0) / 3))
        pts = []
        for i in range(steps + 1):
            a = math.radians(a0 + (a1 - a0) * i / steps)
            pts.append((cx + ro * math.cos(a), cy + ro * math.sin(a)))
        for i in range(steps, -1, -1):
            a = math.radians(a0 + (a1 - a0) * i / steps)
            pts.append((cx + ri * math.cos(a), cy + ri * math.sin(a)))
        self.d.polygon(pts, fill=colour)
        rc, hw = r - width / 2, width / 2
        for a in (a0, a1):
            ar = math.radians(a)
            ex, ey = cx + rc * math.cos(ar), cy + rc * math.sin(ar)
            self.d.ellipse((ex - hw, ey - hw, ex + hw, ey + hw), fill=colour)

    def gauge(self, cx, cy, r, frac, colour, label, sublabel, width=18):
        frac = 0.0 if frac is None else max(0.0, min(1.0, frac))
        start, sweep = 135, 270
        self._arc_poly(cx, cy, r, width, start, start + sweep, TRACK)
        if frac > 0.005:
            self._arc_poly(cx, cy, r, width, start, start + sweep * frac, colour)
        self.text((cx, cy - 4), label, font("bold", int(r * 0.52)), TEXT, "mm")
        self.text((cx, cy + r * 0.48), sublabel, font("regular", 20), DIM, "mm")

    def spark(self, x, y, w, h, data, colour, lo=None, hi=None, fill=True):
        self.d.rounded_rectangle((x, y, x + w, y + h), radius=8, fill=(12, 17, 24))
        data = [v for v in data if v is not None]
        if len(data) < 2:
            return
        lo = min(data) - 2 if lo is None else lo
        hi = max(data) + 2 if hi is None else hi
        if hi - lo < 1:
            hi = lo + 1
        n = len(data)
        pts = []
        for i, v in enumerate(data):
            px = x + 4 + (w - 8) * i / max(1, n - 1)
            py = y + h - 4 - (h - 8) * (v - lo) / (hi - lo)
            pts.append((px, max(y + 4, min(y + h - 4, py))))
        if fill:
            poly = pts + [(pts[-1][0], y + h - 4), (pts[0][0], y + h - 4)]
            self.d.polygon(poly, fill=lerp((12, 17, 24), colour, 0.22))
        self.d.line(pts, fill=colour, width=3, joint="curve")


def stat_line(c, x, y, label, value, colour=TEXT, size=28):
    c.text((x, y), label, font("regular", 21), DIM)
    c.text((x, y + 24), value, font("bold", size), colour)


def render(s, title):
    c = Canvas()
    M, G, Y, H = 10, 10, 10, PANEL_H - 20
    widths = [500, 380, 270, 340]
    widths.append(PANEL_W - 2 * M - 4 * G - sum(widths))
    xs = []
    x = M
    for w in widths:
        xs.append(x)
        x += w + G

    # ---- CPU ----
    x, w = xs[0], widths[0]
    c.panel(x, Y, w, H, "CPU", s["cpu_name"], C_CPU)
    load = s["cpu_load"]
    c.gauge(x + 120, Y + 160, 92, load / 100, C_CPU, f"{load:.0f}%", "load")
    t = s["cpu_temp"]
    tx = x + 236
    c.text((tx, Y + 62), "temperature", font("regular", 21), DIM)
    c.text((tx, Y + 86), "--" if t is None else f"{t:.0f}°C", font("bold", 78), temp_colour(t))
    fr = s["cpu_freq"]
    pw = s["cpu_power"]
    stat_line(c, tx, Y + 180, "clock", "--" if fr is None else f"{fr:.2f} GHz", size=26)
    stat_line(c, tx + 160, Y + 180, "power", "--" if pw is None else f"{pw:.0f} W", size=26)
    c.text((x + 22, Y + 270), "temperature history (3 min)", font("regular", 18), DIM)
    c.spark(x + 20, Y + 292, w - 40, 62, s["hist_cpu_t"], temp_colour(t))
    per = s["cpu_per"] or [0]
    n = len(per)
    bx, bw, by, bh = x + 20, w - 40, Y + 366, 58
    gap = 3 if n <= 32 else 1
    cw = max(1.0, (bw - gap * (n - 1)) / n)
    c.d.rounded_rectangle((bx - 4, by - 4, bx + bw + 4, by + bh + 4), radius=8, fill=(12, 17, 24))
    for i, p in enumerate(per):
        cx0 = bx + i * (cw + gap)
        hh = max(2, bh * p / 100)
        col = lerp((90, 60, 40), C_CPU, min(1, p / 100 + 0.25))
        c.d.rectangle((cx0, by + bh - hh, cx0 + cw - 1, by + bh), fill=col)

    # ---- GPU ----
    x, w = xs[1], widths[1]
    g = s["gpu"]
    c.panel(x, Y, w, H, "GPU", g["name"] if g else "not found", C_GPU)
    if g:
        gl = g.get("load")
        c.gauge(x + 104, Y + 160, 80, (gl or 0) / 100, C_GPU,
                "--" if gl is None else f"{gl:.0f}%", "load")
        gt = g.get("temp")
        tx = x + 206
        c.text((tx, Y + 62), "temperature", font("regular", 21), DIM)
        c.text((tx, Y + 90), "--" if gt is None else f"{gt:.0f}°C", font("bold", 58), temp_colour(gt))
        pw = g.get("power")
        stat_line(c, tx, Y + 180, "power", "--" if pw is None else f"{pw:.0f} W")
        mu, mt = g.get("mem_used") or 0, g.get("mem_total") or 0
        c.text((x + 22, Y + 272), "VRAM", font("regular", 21), DIM)
        c.text((x + w - 22, Y + 272), f"{mu:.1f} / {mt:.0f} GB", font("bold", 22), TEXT, "ra")
        c.bar(x + 22, Y + 302, w - 44, 16, mu / mt if mt else 0, C_GPU)
        fan = g.get("fan")
        fu = g.get("fan_unit", "%")
        clk = g.get("clock")
        stat_line(c, x + 22, Y + 336, "fan", "--" if fan is None else (f"{fan:.0f}%" if fu == "%" else f"{fan:.0f} rpm"))
        stat_line(c, x + 190, Y + 336, "core clock", "--" if clk is None else f"{clk:.0f} MHz")
    else:
        c.text((x + w / 2, Y + H / 2), "No GPU data", font("bold", 30), DIM, "mm")
        c.text((x + w / 2, Y + H / 2 + 40), "(nvidia-smi or amdgpu needed)", font("regular", 20), DIM, "mm")

    # ---- Memory ----
    x, w = xs[2], widths[2]
    c.panel(x, Y, w, H, "Memory", "", C_RAM)
    c.gauge(x + w // 2, Y + 160, 88, s["ram_pct"] / 100, C_RAM, f"{s['ram_pct']:.0f}%", "used")
    c.text((x + w // 2, Y + 270), f"{s['ram_used']:.1f} / {s['ram_total']:.0f} GB",
           font("bold", 28), TEXT, "ma")
    c.text((x + 22, Y + 316), "swap", font("regular", 21), DIM)
    st = s["swap_total"]
    c.text((x + w - 22, Y + 316), f"{s['swap_used']:.1f} / {st:.0f} GB" if st else "off",
           font("bold", 22), TEXT, "ra")
    c.bar(x + 22, Y + 346, w - 44, 14, s["swap_used"] / st if st else 0, C_RAM)
    c.spark(x + 22, Y + 374, w - 44, 50, s["hist_ram"], C_RAM, 0, 100)

    # ---- Fans ----
    x, w = xs[3], widths[3]
    fans = s["fans"]
    c.panel(x, Y, w, H, "Fans", f"{len(fans)} active" if fans else "", C_FAN)
    if fans:
        rows = fans[:7]
        top, avail = Y + 66, H - 80
        rh = min(62, avail / len(rows))
        for i, (name, rpm, fmax) in enumerate(rows):
            ry = top + i * rh
            c.text((x + 22, ry), c.fit(name, font("regular", 22), w - 170), font("regular", 22), TEXT)
            c.text((x + w - 22, ry - 3), "--" if rpm is None else f"{rpm:.0f} rpm",
                   font("bold", 26), C_FAN if rpm else DIM, "ra")
            c.bar(x + 22, ry + 30, w - 44, 10, (rpm or 0) / fmax if fmax else 0, C_FAN)
    else:
        c.text((x + w / 2, Y + H / 2 - 10), "No fan sensors", font("bold", 30), DIM, "mm")
        c.text((x + w / 2, Y + H / 2 + 30), "run: sudo sensors-detect", font("regular", 20), DIM, "mm")

    # ---- System ----
    x, w = xs[4], widths[4]
    c.panel(x, Y, w, H, title, "", C_SYS)
    now = time.localtime()
    c.text((x + 22, Y + 56), time.strftime("%H:%M", now), font("bold", 92), TEXT)
    c.text((x + 24, Y + 160), time.strftime("%a %d %b %Y", now), font("regular", 24), DIM)
    c.text((x + 24, Y + 198), f"up {fmt_uptime(s['uptime'])}   load {s['load_avg'][0]:.2f}",
           font("regular", 21), TEXT)
    c.text((x + 24, Y + 234), "↓ " + human_rate(s["net_down"]), font("bold", 23), C_FAN)
    c.text((x + 24 + (w - 48) / 2, Y + 234), "↑ " + human_rate(s["net_up"]), font("bold", 23), C_GPU)
    dy = Y + 278
    for mount, used, total, pct in s["disks"][:3]:
        c.text((x + 24, dy), c.fit(mount, font("regular", 20), 120), font("regular", 20), DIM)
        tot = f"{total / 1024:.1f} TB" if total >= 1024 else f"{total:.0f} GB"
        c.text((x + w - 22, dy), f"{pct:.0f}% of {tot}", font("bold", 20), TEXT, "ra")
        c.bar(x + 24, dy + 28, w - 46, 12, pct / 100, C_SYS if pct < 90 else (255, 70, 70))
        dy += 52
    return c.img


def rotate_for_panel(img, deg):
    deg = int(deg) % 360
    return img.rotate(deg, expand=True) if deg else img


def encode_jpeg(img, quality, max_kb):
    import io
    q = quality
    sub = 0  # 4:4:4 keeps small text crisp; drop to 4:2:0 if too big
    while True:
        b = io.BytesIO()
        img.save(b, "JPEG", quality=q, subsampling=sub, optimize=False, progressive=False)
        data = b.getvalue()
        if not max_kb or len(data) <= max_kb * 1024 or q <= 20:
            return data
        if sub == 0:
            sub = 2
        else:
            q -= 5


def test_pattern(text="jlscreen test"):
    c = Canvas()
    cols = [C_CPU, C_GPU, C_RAM, C_FAN, C_SYS, (255, 70, 70)]
    sw = PANEL_W // len(cols)
    for i, col in enumerate(cols):
        c.d.rectangle((i * sw, 0, (i + 1) * sw, 60), fill=col)
    c.d.rectangle((2, 2, PANEL_W - 3, PANEL_H - 3), outline=TEXT, width=4)
    c.text((PANEL_W / 2, PANEL_H / 2), text, font("bold", 110), TEXT, "mm")
    c.text((PANEL_W / 2, PANEL_H - 70), "TOP OF SCREEN IS THE COLOUR STRIP  ·  arrow points right →",
           font("regular", 34), DIM, "mm")
    c.d.polygon([(PANEL_W - 160, PANEL_H / 2 - 50), (PANEL_W - 40, PANEL_H / 2),
                 (PANEL_W - 160, PANEL_H / 2 + 50)], fill=C_FAN)
    return c.img


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def load_config(path):
    cfg = configparser.ConfigParser()
    cfg.read_string(DEFAULT_CONFIG)
    paths = [path] if path else ["/etc/jlscreen.conf",
                                 os.path.expanduser("~/.config/jlscreen.conf")]
    for p in paths:
        if p and os.path.exists(p):
            cfg.read(p)
    return cfg


def resolve_port(cfg, args):
    p = args.port or cfg.get("display", "port", fallback="auto")
    if p and p.lower() != "auto":
        return p
    found = find_port()
    if not found:
        sys.exit("Screen not found. Check it is plugged in (lsusb should show 33c3:xxxx),\n"
                 "or give the port with --port /dev/ttyACM0")
    return found


def open_screen(cfg, args):
    port = resolve_port(cfg, args)
    proto = args.protocol or cfg.get("display", "protocol", fallback="auto")
    scr = JLScreen(port, proto, args.verbose)
    return scr


def cmd_list(cfg, args):
    if serial is None:
        sys.exit("pyserial missing: sudo apt install python3-serial")
    for p in serial.tools.list_ports.comports():
        tag = "  <-- Jungle Leopard" if p.vid == JL_VID else ""
        vidpid = f"{p.vid:04x}:{p.pid:04x}" if p.vid is not None else "----:----"
        print(f"{p.device:16} {vidpid}  {p.description}{tag}")


def cmd_info(cfg, args):
    scr = open_screen(cfg, args)
    print(f"Port:     {scr.port_name}")
    print(f"Protocol: {scr.protocol}")
    print(json.dumps(scr.info, indent=2) if scr.info else "No reply to getInfo")
    scr.close()


def cmd_sensors(cfg, args):
    try:
        temps = psutil.sensors_temperatures()
    except AttributeError:
        temps = {}
    print("Temperatures (use chip:label in cpu_temp =):")
    for chip, entries in temps.items():
        for e in entries:
            print(f"  {chip}:{e.label or ''}".ljust(40) + f"{e.current:6.1f} °C")
    print("\nFans (use chip:label=Name in fans =):")
    try:
        fans = psutil.sensors_fans()
    except AttributeError:
        fans = {}
    if not fans:
        print("  none found - install lm-sensors and run sudo sensors-detect (see README)")
    for chip, entries in fans.items():
        for i, e in enumerate(entries):
            print(f"  {chip}:{e.label or f'fan{i + 1}'}".ljust(40) + f"{e.current:6.0f} rpm")
    s = Sensors(cfg)
    time.sleep(0.5)
    st = s.collect()
    g = st["gpu"]
    print(f"\nCPU:  {st['cpu_name']}  temp={st['cpu_temp']}  load={st['cpu_load']:.0f}%")
    print(f"GPU:  {g['name'] + '  temp=' + str(g['temp']) if g else 'none found'}")
    print(f"RAPL: {'available' if s.rapl.path else 'not readable (run as root for CPU power)'}")


def cmd_preview(cfg, args):
    title = cfg.get("look", "title", fallback="").strip() or socket.gethostname()
    if args.demo:
        st = demo_stats(time.time())
    else:
        s = Sensors(cfg)
        s.collect()
        time.sleep(1.0)
        st = s.collect()
    img = render(st, title)
    img.save(args.output)
    rot = rotate_for_panel(img, cfg.getint("display", "rotate", fallback=90))
    data = encode_jpeg(rot, cfg.getint("display", "quality", fallback=88),
                       cfg.getint("display", "max_kb", fallback=80))
    print(f"Wrote {args.output} (frame as sent would be {rot.size[0]}x{rot.size[1]}, {len(data) // 1024} kB)")


def cmd_brightness(cfg, args):
    scr = open_screen(cfg, args)
    scr.set_brightness(args.level)
    time.sleep(0.2)
    scr.close()


def cmd_test(cfg, args):
    scr = open_screen(cfg, args)
    rot = args.rotate if args.rotate is not None else cfg.getint("display", "rotate", fallback=90)
    data = encode_jpeg(rotate_for_panel(test_pattern(f"rotate = {rot}"), rot), 85,
                       cfg.getint("display", "max_kb", fallback=80))
    print(f"Port {scr.port_name}, protocol {scr.protocol}, firmware {scr.info.get('version', '?')}")
    print(f"Showing test pattern with rotate = {rot} for {args.seconds} s ...")
    end = time.time() + args.seconds
    while time.time() < end:
        scr.send_jpeg(data)
        time.sleep(1.0)
    scr.close()


RUNNING = True


def _stop(*_):
    global RUNNING
    RUNNING = False


def cmd_run(cfg, args):
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    title = cfg.get("look", "title", fallback="").strip() or socket.gethostname()
    interval = max(0.3, cfg.getfloat("display", "interval", fallback=1.0))
    rot = cfg.getint("display", "rotate", fallback=90)
    quality = cfg.getint("display", "quality", fallback=88)
    max_kb = cfg.getint("display", "max_kb", fallback=80)
    bright = cfg.getint("display", "brightness", fallback=80)
    sensors = Sensors(cfg)
    scr = None
    while RUNNING:
        try:
            if scr is None:
                scr = open_screen(cfg, args)
                scr.set_brightness(bright)
                print(f"jlscreen: connected on {scr.port_name}, firmware "
                      f"{scr.info.get('version', '?')}, protocol {scr.protocol}", flush=True)
            t0 = time.time()
            st = demo_stats(t0) if args.demo else sensors.collect()
            frame = encode_jpeg(rotate_for_panel(render(st, title), rot), quality, max_kb)
            scr.send_jpeg(frame)
            # keep the live link alive between frames if the interval is long
            while RUNNING and time.time() - t0 < interval:
                left = interval - (time.time() - t0)
                time.sleep(min(1.0, max(0.0, left)))
                if left > 1.0:
                    scr.keepalive()
        except SystemExit as e:
            print(f"jlscreen: {e}", file=sys.stderr, flush=True)
            time.sleep(5)
        except (OSError, Exception) as e:  # unplugged, suspended, etc.
            print(f"jlscreen: link error ({e}); retrying in 3 s", file=sys.stderr, flush=True)
            if scr:
                try:
                    scr.ser.close()
                except Exception:
                    pass
            scr = None
            time.sleep(3)
    if scr:
        scr.close()


def main():
    ap = argparse.ArgumentParser(description="System monitor for the Jungle Leopard 9.16\" USB screen")
    ap.add_argument("--config", help="config file (default /etc/jlscreen.conf, then ~/.config/jlscreen.conf)")
    ap.add_argument("--port", help="serial port, e.g. /dev/ttyACM0 (default: auto)")
    ap.add_argument("--protocol", choices=["auto", "raw", "envelope"], help="override frame protocol")
    ap.add_argument("-v", "--verbose", action="store_true", help="print serial traffic")
    ap.add_argument("--version", action="version", version=VERSION)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="list serial ports and flag the screen")
    sub.add_parser("info", help="query the screen's firmware info")
    sub.add_parser("sensors", help="list the sensors this PC exposes")
    p = sub.add_parser("preview", help="render the dashboard to an image file (no screen needed)")
    p.add_argument("-o", "--output", default="jlscreen-preview.png")
    p.add_argument("--demo", action="store_true", help="use made-up values")
    p = sub.add_parser("test", help="show an orientation test pattern on the screen")
    p.add_argument("--rotate", type=int, help="rotation to try (0, 90, 180, 270)")
    p.add_argument("--seconds", type=int, default=15)
    p = sub.add_parser("brightness", help="set backlight 0-100")
    p.add_argument("level", type=int)
    p = sub.add_parser("run", help="run the monitor (what the service does)")
    p.add_argument("--demo", action="store_true", help="send made-up values")
    args = ap.parse_args()
    cfg = load_config(args.config)
    {"list": cmd_list, "info": cmd_info, "sensors": cmd_sensors, "preview": cmd_preview,
     "test": cmd_test, "brightness": cmd_brightness, "run": cmd_run}[args.cmd](cfg, args)


if __name__ == "__main__":
    main()
