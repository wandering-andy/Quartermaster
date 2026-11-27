"""Status screen for the Waveshare 2.13-inch e-Paper HAT (V4, 250x122) on the headless collector.

The panel is slow and black and white, so this is a counts-and-status page, not the interactive UI. It shows the same
numbers as the terminal dashboard and refreshes only when they change, at most once per interval.
"""

import time

from PIL import Image, ImageDraw, ImageFont

SIZE = (250, 122)  # landscape; the driver turns it onto the 122x250 glass
FONTS = ("DejaVuSansMono.ttf", "DejaVuSansMono-Bold.ttf", "LiberationMono-Regular.ttf")


def _font(size):
    for name in FONTS:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size)
    except TypeError:
        return ImageFont.load_default()


def _clock(seconds):
    hours, rest = divmod(max(0, int(seconds or 0)), 3600)
    return "%d:%02d" % (hours, rest // 60)  # no seconds, so a quiet minute does not trigger a refresh


def render(status, hat_state=""):
    """Draw one status frame as a 1-bit image."""
    img = Image.new("1", SIZE, 1)
    draw = ImageDraw.Draw(img)
    small, big = _font(11), _font(20)
    state = str(status.get("state", "unknown")).upper()
    draw.rectangle((0, 0, SIZE[0], 15), fill=0)
    draw.text((3, 1), "QUARTERMASTER", font=small, fill=1)
    draw.text((SIZE[0] - 3, 1), "%s %s" % (state, _clock(status.get("elapsed"))), font=small, fill=1, anchor="ra")

    def count(x, y, label, value):
        draw.text((x, y), label, font=small, fill=0)
        draw.text((x, y + 12), str(value), font=big, fill=0)

    count(4, 20, "WI-FI", status.get("wifi", 0))
    count(66, 20, "BLUETOOTH", status.get("ble", 0))
    count(154, 20, "ROWS", status.get("rows", 0))
    aircraft, mesh = status.get("aircraft"), status.get("mesh")
    draw.text((4, 62), "AIR %s   MESH %s" % ("off" if aircraft is None else aircraft,
                                           "-" if mesh is None else mesh), font=small, fill=0)

    gps = status.get("gps") or {}
    if gps.get("valid"):
        fix = "GPS FIX"
        if gps.get("hdop") is not None:
            fix += "  HDOP %.1f" % gps["hdop"]
    else:
        fix = "GPS STALE" if gps.get("status") == "stale" else "GPS NO FIX"
    draw.text((4, 78), fix, font=small, fill=0)

    lora = status.get("lora")
    radio = "LoRa %s" % (str(lora.get("state", "?")).upper() if lora else hat_state or "off")
    draw.text((SIZE[0] - 4, 78), radio, font=small, fill=0, anchor="ra")
    draw.line((0, 95, SIZE[0], 95), fill=0)

    problem = status.get("failed") or status.get("error")
    if problem:
        footer = "! %s" % problem
    else:
        footer = "Skipped (no GPS): %d" % status.get("skipped_no_fix", 0)
    draw.text((4, 100), footer[:38], font=small, fill=0)
    return img


class EpaperDisplay:
    """Throttled partial refresh with a periodic full refresh to clear ghosting."""

    def __init__(self, driver=None, interval=10.0, full_every=60, full_after=3600.0, clock=time.monotonic):
        self.interval, self.full_every, self.full_after, self.clock = interval, full_every, full_after, clock
        try:
            if driver is None:
                from waveshare_epd import epd2in13_V4

                driver = epd2in13_V4.EPD()
            self.epd = driver
            self.epd.init()
            self.epd.Clear(0xFF)
        except Exception as exc:
            raise OSError("Cannot open the 2.13inch e-Paper HAT: %s" % exc) from exc
        self.last = self.last_bytes = None
        self.partials = 0
        self.last_full = self.clock()

    def show(self, img):
        data = img.tobytes()
        now = self.clock()
        if data == self.last_bytes or (self.last is not None and now - self.last < self.interval):
            return False
        buffer = self.epd.getbuffer(img)
        if self.last is None or self.partials >= self.full_every or now - self.last_full >= self.full_after:
            self.epd.init()
            self.epd.display_Base(buffer)
            self.partials, self.last_full = 0, now
        else:
            self.epd.displayPartial(buffer)
            self.partials += 1
        self.last, self.last_bytes = now, data
        return True

    def close(self):
        try:
            self.epd.sleep()
        except Exception:
            pass


class EpaperStatus:
    """Sink for headless.run: draws each status snapshot on the panel."""

    def __init__(self, display):
        self.display = display

    def update(self, status, hat_state=""):
        return self.display.show(render(status, hat_state))

    def close(self):
        self.display.close()
