#!/usr/bin/env python3
"""Fake Jungle Leopard screen on a pty: answers getInfo, records every byte sent."""
import io, json, os, pty, sys, threading, time, tty
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import jlscreen as jl
from PIL import Image


def fake_device(fd, version, log, stop):
    buf = b""
    while not stop.is_set():
        try:
            chunk = os.read(fd, 65536)
        except OSError:
            break
        log.append(chunk)
        buf += chunk
        while True:
            fr, rest = jl.extract_frame(buf)
            if not fr:
                break
            buf = rest
            if fr[4] == jl.CMD_GET_INFO:
                payload = json.dumps({"status": 200, "data": {"model": "D215-FL7707N-9.16inch-hor",
                          "version": version, "width": 1920, "height": 462}}).encode()
                os.write(fd, jl.build_frame(jl.CMD_GET_INFO, payload))


def run_case(version, expect):
    master, slave = pty.openpty()
    tty.setraw(slave)
    log, stop = [], threading.Event()
    t = threading.Thread(target=fake_device, args=(master, version, log, stop), daemon=True)
    t.start()
    scr = jl.JLScreen(os.ttyname(slave))
    assert scr.protocol == expect, (scr.protocol, expect)
    assert scr.info["model"].startswith("D215"), scr.info
    scr.set_brightness(70)
    img = jl.rotate_for_panel(jl.render(jl.demo_stats(time.time()), "test"), 90)
    jpeg = jl.encode_jpeg(img, 88, 80)
    scr.send_jpeg(jpeg)
    time.sleep(0.5)
    stop.set()
    data = b"".join(log)

    assert jl.build_frame(jl.CMD_BRIGHTNESS, bytes([70])) in data
    assert data.count(jl.build_frame(jl.CMD_LIVE)) >= 2
    i = data.find(jpeg)
    assert i > 0, "JPEG not found in stream"
    if expect == "envelope":
        assert jl.PIPELINE_RESET in data
        n = int.from_bytes(data[i - 4:i], "little")
        assert n == len(jpeg)
        chk = int.from_bytes(data[i + n:i + n + 2], "little")
        assert chk == (sum(data[i - 4:i]) + sum(jpeg)) & 0xFFFF
        assert data[i + n + 2:i + n + 6] == jl.EOI_TERMINATOR
    else:
        assert jl.PIPELINE_RESET not in data
        assert data.endswith(jpeg)
    im = Image.open(io.BytesIO(jpeg))
    assert im.size == (462, 1920) and im.format == "JPEG" and len(jpeg) <= 80 * 1024
    scr.close()
    print(f"firmware {version}: protocol {scr.protocol}, jpeg {len(jpeg)} B, {im.size} OK")


# checksum against the worked example used by the Go tool: brightness 50
f = jl.build_frame(3, bytes([50]))
assert f == bytes([0x55, 0xAA, 0x08, 0x00, 0x03, 0x32, 0x3C, 0x01]), f.hex()
run_case("2.2", "raw")
run_case("3.1.2", "envelope")
print("all tests passed")
