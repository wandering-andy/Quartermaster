import socket
import sys
from types import SimpleNamespace
from unittest.mock import Mock

from PIL import Image
import pytest

from quartermaster.ui import hat
from quartermaster.ui.keys import Key


@pytest.fixture
def panel_drivers(monkeypatch):
    gpio, bus, serial, small, large = (Mock() for _ in range(5))
    small.return_value.size, large.return_value.size = (128, 128), (240, 240)
    hooks = []

    def controller(calls):
        class LCD:
            def __init__(self, *args, **kwargs):
                hooks.append(lambda: self.cleanup())
                panel = calls(*args, **kwargs)
                self.size, self.display, self.backlight = panel.size, panel.display, panel.backlight

            def cleanup(self):
                pytest.fail("Luma tried to access the released display at interpreter shutdown")
        return LCD

    for name, module in {
        "RPi": SimpleNamespace(GPIO=gpio),
        "RPi.GPIO": gpio,
        "spidev": SimpleNamespace(SpiDev=lambda: bus),
        "luma.core.interface.serial": SimpleNamespace(spi=serial),
        "luma.lcd.device": SimpleNamespace(st7735=controller(small), st7789=controller(large)),
    }.items():
        monkeypatch.setitem(sys.modules, name, module)
    return gpio, bus, serial, small, large, hooks


@pytest.mark.parametrize("profile,side", [("st7735-128", 128), ("st7789-240", 240)])
def test_waveshare_uses_its_native_frame_and_only_releases_display_pins(panel_drivers, profile, side):
    gpio, bus, serial, small, large, hooks = panel_drivers
    display = hat.HatDisplay(profile)
    assert display.profile == profile
    assert (display.width, display.height) == (side, side)
    spi_pins = serial.call_args.kwargs
    assert [spi_pins[k] for k in ("port", "device", "gpio_DC", "gpio_RST")] == [0, 0, 25, 27]
    driver = small if side == 128 else large
    options = driver.call_args.kwargs
    assert options["gpio_LIGHT"] == 24 and options["active_low"] is False
    if side == 128:
        assert (options["h_offset"], options["v_offset"], options["bgr"]) == (1, 2, True)
    frame = Image.new("RGB", (side, side), "red")
    display.show(frame)
    driver.return_value.display.assert_called_once_with(frame)
    with pytest.raises(ValueError):
        display.show(Image.new("RGB", (320, 170)))
    display.close()
    display.close()
    hooks[0]()
    driver.return_value.backlight.assert_called_once_with(False)
    bus.close.assert_called_once()
    gpio.cleanup.assert_called_once_with([25, 27, 24])


def test_failed_display_releases_the_bus_and_pins(panel_drivers):
    gpio, bus, serial, _, _, _ = panel_drivers
    serial.side_effect = OSError("SPI disabled")
    with pytest.raises(OSError, match="SPI disabled"):
        hat.HatDisplay("st7735-128")
    bus.close.assert_called_once()
    gpio.cleanup.assert_called_once_with([25, 27, 24])


def test_failed_controller_cannot_touch_released_pins_at_shutdown(panel_drivers):
    gpio, bus, _, small, _, hooks = panel_drivers
    small.side_effect = OSError("panel initialization failed")
    with pytest.raises(OSError, match="panel initialization failed"):
        hat.HatDisplay("st7735-128")
    hooks[0]()
    bus.close.assert_called_once()
    gpio.cleanup.assert_called_once_with([25, 27, 24])


def test_hat_buttons_share_input_with_a_usb_keyboard(monkeypatch):
    pins = {}

    def button(pin, pull_up, bounce_time):
        assert pull_up and bounce_time > 0
        pins[pin] = Mock()
        return pins[pin]

    monkeypatch.setitem(sys.modules, "gpiozero", SimpleNamespace(Button=button))
    keyboard = Mock()
    monkeypatch.setattr(hat, "EvdevInput", lambda: keyboard)
    read, write = socket.socketpair()
    with read, write:
        keyboard.devices = {read: False}
        keyboard.poll.side_effect = lambda _: [Key(read.recv(1).decode())]
        inputs = hat.HatInput()
        try:
            for pin in (6, 19, 5, 26, 13, 21, 20, 16):
                pins[pin].when_pressed()
            write.sendall(b"A")
            assert inputs.poll(0) == ["UP", "DOWN", "LEFT", "RIGHT", "ENTER", "ENTER", "ESC", "TAB", "A"]
            pins[13].when_pressed()
            inputs.flush()
            keyboard.flush.assert_called_once()
            assert inputs.poll(0) == []
        finally:
            inputs.close()
            inputs.close()
    keyboard.close.assert_called_once()
    for pin in pins.values():
        pin.close.assert_called_once()


def test_the_two_inch_module_is_landscape_with_its_own_backlight_and_centres_the_wide_frame(panel_drivers):
    gpio, bus, serial, small, large, hooks = panel_drivers
    large.return_value.size = (320, 240)
    display = hat.HatDisplay("st7789-320x240")
    options = large.call_args.kwargs
    assert (options["width"], options["height"], options["rotate"]) == (240, 320, 1)
    assert options["gpio_LIGHT"] == 24
    assert [serial.call_args.kwargs[k] for k in ("gpio_DC", "gpio_RST")] == [25, 27]
    assert (display.width, display.height) == (320, 240)
    display.show(Image.new("RGB", (320, 170), "red"))
    shown = large.return_value.display.call_args.args[0]
    assert shown.size == (320, 240) and shown.getpixel((0, 0)) == (0, 0, 0)
    assert shown.getpixel((160, 120)) == (255, 0, 0) and shown.getpixel((160, 239)) == (0, 0, 0)
    gpio.cleanup.assert_not_called()
    display.close()
    gpio.cleanup.assert_called_once_with([25, 27, 24])


def test_the_buttonless_module_opens_no_gpio_buttons_and_keeps_the_keyboard(monkeypatch):
    monkeypatch.setitem(sys.modules, "gpiozero", SimpleNamespace(Button=lambda *a, **k: pytest.fail("no buttons")))
    keyboard = Mock()
    monkeypatch.setattr(hat, "EvdevInput", lambda: keyboard)
    inputs = hat.HatInput(buttons=False)
    inputs.close()
    keyboard.close.assert_called_once()


class FakeEpd:
    def __init__(self):
        self.calls = []

    def init(self):
        self.calls.append("init")

    def Clear(self, colour):
        self.calls.append("clear")

    def getbuffer(self, img):
        return img.tobytes()

    def display_Base(self, buffer):
        self.calls.append("full")

    def displayPartial(self, buffer):
        self.calls.append("partial")

    def sleep(self):
        self.calls.append("sleep")


def test_epaper_refreshes_partially_only_on_change_and_after_the_interval_with_periodic_full_refreshes():
    from quartermaster.ui import epaper

    now = [0.0]
    epd = FakeEpd()
    panel = epaper.EpaperDisplay(epd, interval=10, full_every=2, full_after=1000, clock=lambda: now[0])
    shown = []
    for wifi in (1, 1, 2, 3, 4):
        now[0] += 11
        shown.append(panel.show(epaper.render({"wifi": wifi, "state": "running"})))
        if shown[-1]:
            now[0] += 1
            assert not panel.show(epaper.render({"wifi": wifi + 100}))  # inside the interval
            now[0] -= 1
    assert shown == [True, False, True, True, True]
    assert [c for c in epd.calls if c in ("full", "partial")] == ["full", "partial", "partial", "full"]
    panel.close()
    assert epd.calls[-1] == "sleep"


def test_epaper_frame_is_the_panel_size_and_shows_the_counts():
    from quartermaster.ui import epaper

    blank = epaper.render({})
    busy = epaper.render({"wifi": 1234, "ble": 56, "gps": {"valid": True, "hdop": 0.9}, "failed": "No adapter"})
    assert blank.size == busy.size == epaper.SIZE and blank.mode == "1"
    assert blank.tobytes() != busy.tobytes()


class FakeOled:
    def __init__(self):
        self.calls = []

    def contrast(self, level):
        self.calls.append(("contrast", level))

    def display(self, img):
        self.calls.append(("display", img.tobytes()))

    def hide(self):
        self.calls.append(("hide",))


def test_oled_throttles_shifts_every_minute_and_dims_when_nothing_changes():
    from quartermaster.ui import oled

    now = [0.0]
    device = FakeOled()
    panel = oled.OledDisplay(device, interval=1, shift_every=60, dim_after=300, clock=lambda: now[0])
    status = {"state": "running", "wifi": 5}
    assert panel.show(status) and not panel.show(status)  # second call is inside the interval
    now[0] = 61
    assert panel.show(status)
    frames = [call[1] for call in device.calls if call[0] == "display"]
    assert frames[0] != frames[1]  # the page drifted by a pixel
    assert ("contrast", oled.DIM) not in device.calls
    now[0] = 400
    panel.show(status)
    assert device.calls.count(("contrast", oled.DIM)) == 1
    now[0] = 402
    panel.show({"state": "running", "wifi": 6})  # a change wakes the panel
    assert device.calls[-2] == ("contrast", oled.BRIGHT)
    panel.close()
    assert device.calls[-1] == ("hide",)


def test_oled_frame_is_128x64_and_changes_with_the_counts():
    from quartermaster.ui import oled

    assert oled.render({}).size == oled.SIZE
    assert oled.render({"wifi": 99}).tobytes() != oled.render({}).tobytes()
