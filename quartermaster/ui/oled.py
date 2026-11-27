"""Status screen for a 128x64 SSD1309 (or SSD1306/SH1106-style) I2C OLED on the headless collector.

Monochrome and tiny, so it shows only the counts, the GPS and LoRa state and any problem. OLEDs burn in, so the page
drifts by a pixel every minute and the panel dims after a while.
"""

import time

from PIL import Image, ImageDraw

from .epaper import _clock, _font

SIZE = (128, 64)
ADDRESS = 0x3C
SHIFTS = ((0, 0), (1, 0), (1, 1), (0, 1))
BRIGHT, DIM = 0xCF, 0x10


def render(status, hat_state="", shift=(0, 0)):
    """Draw one status frame as a 1-bit image; shift moves the content to spread the wear."""
    img = Image.new("1", SIZE, 0)
    draw = ImageDraw.Draw(img)
    small, big = _font(9), _font(15)
    dx, dy = shift
    state = str(status.get("state", "unknown")).upper()
    draw.text((dx, dy), state, font=small, fill=1)
    draw.text((SIZE[0] - 2 + dx, dy), _clock(status.get("elapsed")), font=small, fill=1, anchor="ra")

    for x, label, key in ((0, "WIFI", "wifi"), (44, "BLE", "ble"), (80, "ROWS", "rows")):
        draw.text((x + dx, 11 + dy), label, font=small, fill=1)
        draw.text((x + dx, 20 + dy), str(status.get(key, 0)), font=big, fill=1)

    gps = status.get("gps") or {}
    fix = "FIX" if gps.get("valid") else "STALE" if gps.get("status") == "stale" else "NO FIX"
    lora = status.get("lora")
    radio = str(lora.get("state", "?")).upper() if lora else hat_state or "off"
    draw.text((dx, 40 + dy), "GPS %s  LoRa %s" % (fix, radio), font=small, fill=1)
    problem = status.get("failed") or status.get("error")
    draw.text((dx, 52 + dy), ("! %s" % problem if problem else "Skipped %d" % status.get("skipped_no_fix", 0))[:24],
              font=small, fill=1)
    return img


class OledDisplay:
    """Refresh at most every interval seconds; shift each minute and dim after dim_after seconds of no change."""

    def __init__(self, device=None, interval=1.0, shift_every=60.0, dim_after=300.0, clock=time.monotonic,
                 address=ADDRESS):
        self.interval, self.shift_every, self.dim_after, self.clock = interval, shift_every, dim_after, clock
        try:
            if device is None:
                from luma.core.interface.serial import i2c
                from luma.oled.device import ssd1309

                device = ssd1309(i2c(port=1, address=address))
            self.device = device
            self.device.contrast(BRIGHT)
        except Exception as exc:
            raise OSError("Cannot open the I2C OLED at 0x%02X: %s" % (address, exc)) from exc
        self.start = self.last = self.changed = self.last_bytes = None
        self.dimmed = False

    def shift(self, now):
        return SHIFTS[int((now - self.start) // self.shift_every) % len(SHIFTS)]

    def show(self, status, hat_state=""):
        now = self.clock()
        if self.start is None:
            self.start = self.changed = now
        if self.last is not None and now - self.last < self.interval:
            return False
        self.last = now
        data = render(status, hat_state).tobytes()  # unshifted, so drifting does not count as a change
        if data != self.last_bytes:
            self.last_bytes, self.changed = data, now
            if self.dimmed:
                self.device.contrast(BRIGHT)
                self.dimmed = False
        elif not self.dimmed and now - self.changed >= self.dim_after:
            self.device.contrast(DIM)
            self.dimmed = True
        self.device.display(render(status, hat_state, self.shift(now)))
        return True

    def close(self):
        try:
            self.device.hide()
        except Exception:
            pass


class OledStatus:
    """Sink for headless.run: draws each status snapshot on the OLED."""

    def __init__(self, display):
        self.display = display

    def update(self, status, hat_state=""):
        return self.display.show(status, hat_state)

    def close(self):
        self.display.close()
