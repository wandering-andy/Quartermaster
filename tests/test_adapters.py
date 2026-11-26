from pathlib import Path
import re
import sys
import types
try:
    import tomllib
except ImportError:
    import tomli as tomllib

from quartermaster import ble, hardware
from quartermaster.ui import app as app_module, gfx, screens
from tests.fakes import NO_FIX, Hardware
from tests.test_ui import make_app, press, screen_text

ROOT = Path(__file__).resolve().parents[1]
PI = "6.18.39+rpt-rpi-v8"
POWER_LINE = "[   12.3] mt7921u 1-1.2:1.0: vendor request req:63 off:0000 failed:-110\n"


class Run:
    def __init__(self, stdout="", returncode=0):
        self.stdout, self.returncode, self.calls = stdout, returncode, []

    def __call__(self, command, **kwargs):
        self.calls.append(command[0])
        return self


def device(root, port, usb_id, product="", interfaces=(), net=False, driver=None):
    node = root / port
    node.mkdir(parents=True)
    vendor, model = usb_id.split(":")
    (node / "idVendor").write_text(vendor + "\n")
    (node / "idProduct").write_text(model + "\n")
    (node / "product").write_text(product + "\n")
    (node / "devnum").write_text("7\n")
    for number, cls in enumerate(interfaces or ("ff",)):
        face = node / ("%s:1.%d" % (port, number))
        face.mkdir()
        (face / "bInterfaceClass").write_text(cls + "\n")
        if net and number == 0:
            (face / "net" / "wlan1").mkdir(parents=True)
        if driver and number == 0:
            (root / "drivers" / driver).mkdir(parents=True, exist_ok=True)
            (face / "driver").symlink_to(root / "drivers" / driver)
    return node


def system(tmp_path, aliases=(), firmware=()):
    modules = tmp_path / "modules"
    (modules / PI).mkdir(parents=True)
    (modules / PI / "modules.alias").write_text("".join(
        "alias usb:v%sp%sd*dc*dsc*dp*icFFiscFFipFFin* %s\n" % (usb_id[:4].upper(), usb_id[5:].upper(), module)
        for usb_id, module in aliases) + "alias usb:v*p*d*dc*dsc*dp*ic08isc06ip50in* usb_storage\n")
    lib = tmp_path / "firmware"
    lib.mkdir()
    for name in firmware:
        (lib / name).parent.mkdir(parents=True, exist_ok=True)
        (lib / name).write_text("fw")
    return {"modules": modules, "firmware": lib, "release": PI, "hwmon": tmp_path / "hwmon",
            "dpkg_info": tmp_path / "dpkg"}


def why(item, where, runner=None):
    return hardware.explain(item, runner=runner or Run(returncode=1), **where)


def item(usb_id, chip=None, port="1-1.2", **extra):
    return dict({"usb_id": usb_id, "port": port, "label": "x", "disc": False, "bound": [],
                 "chip": hardware.USB_CHIPS.get(usb_id, "") if chip is None else chip}, **extra)


def test_the_chip_table_is_well_formed_and_knows_the_adapters_people_carry():
    ids = [usb_id for ids in hardware.CHIP_IDS.values() for usb_id in ids.split()]
    assert len(ids) == len(set(ids)) == len(hardware.USB_CHIPS)
    assert all(re.fullmatch(r"[0-9a-f]{4}:[0-9a-f]{4}", usb_id) for usb_id in ids)
    assert set(hardware.CHIP_IDS) == set(hardware.CHIPS)
    for name, first, firmware, package in hardware.CHIPS.values():
        assert re.fullmatch(r"[A-Z0-9]+", name) and len(first) == 2 and bool(firmware) == bool(package)
    expected = {"0e8d:7961": "mt7921au", "0e8d:7925": "mt7925u", "0bda:8812": "rtl8812au", "2357:0138": "rtl8822bu",
                "0bda:c811": "rtl8821cu", "0bda:818b": "rtl8192eu", "0b05:1997": "rtl8852au",
                "37ad:0103": "rtl8852cu", "0cf3:9271": "ar9271", "148f:5572": "rt2800"}
    assert {usb_id: hardware.USB_CHIPS[usb_id] for usb_id in expected} == expected
    assert all(hardware.CHIPS[chip][1] <= (6, 18) for chip in ("rtl8814au", "rtl8851bu", "rtl8852bu", "mt7925u"))
    assert all(hardware.CHIPS[chip][1] > (6, 18) for chip in ("rtl8852au", "rtl8852cu", "rtl8922au"))
    assert not set(hardware.DRIVER_DISC) & set(hardware.USB_CHIPS)
    assert set(hardware.ID_FIRST) <= set(hardware.USB_CHIPS)
    for usb_id, first in hardware.ID_FIRST.items():
        assert first > hardware.CHIPS[hardware.USB_CHIPS[usb_id]][1]
    assert "2001:3318" not in hardware.USB_CHIPS
    assert [hardware.USB_CHIPS[usb_id] for usb_id in ("0846:9065", "2357:0135", "2001:3319")] == [
        "mt7921au", "rtl8192fu", "rtl8192eu"]
    assert "CF-952AX" in hardware.USB_ADAPTERS["3574:6211"] and "A8000" in hardware.USB_ADAPTERS["0846:9060"]
    assert "TXE50UH" in hardware.USB_ADAPTERS["35bc:0107"]


def test_an_adapter_showing_its_driver_disc_is_found_and_a_card_reader_is_not(tmp_path):
    device(tmp_path, "1-1.1", "0bda:1a2b", "DISK", interfaces=("08",))
    device(tmp_path, "1-1.2", "0bda:1a2c", "Realtek DISK", interfaces=("08",))
    device(tmp_path, "1-1.3", "0bda:0153", "USB2.0-CRW", interfaces=("08",))
    device(tmp_path, "1-1.4", "0bda:9210", "RTL9210", interfaces=("08",))
    device(tmp_path, "1-1.5", "0bda:c811", "802.11ac NIC", net=True)
    found = hardware.stalled_usb_adapters(tmp_path)
    assert [(s["port"], s["disc"], s["label"]) for s in found] == [
        ("1-1.1", True, "Wi-Fi adapter"), ("1-1.2", True, "Wi-Fi adapter")]
    explained = hardware.explain(found[0], runner=Run())
    assert explained["why"] == hardware.SETUP
    assert hardware.stall_words(explained) == ("Wi-Fi adapter in setup mode", "It shows as a driver disc. Replug it.")


def test_a_known_chip_without_an_interface_is_found_by_its_id_and_named_by_its_chip(tmp_path):
    device(tmp_path, "1-1.1", "37ad:0103", "")
    device(tmp_path, "1-1.2", "0bda:b832", "", net=True)
    device(tmp_path, "1-1.3", "0bda:c82c", "", interfaces=("e0", "ff"), driver="btusb")
    found = hardware.stalled_usb_adapters(tmp_path)
    assert [(s["port"], s["chip"], s["label"], s["bound"], s["plugged"]) for s in found] == [
        ("1-1.1", "rtl8852cu", "RTL8852CU adapter", [], "7"), ("1-1.3", "rtl8822cu", "RTL8822CU adapter", [], "7")]


def test_each_reason_an_adapter_does_not_start_is_told_apart(tmp_path):
    where = system(tmp_path, aliases=[("0bda:b832", "rtw89_8852bu"), ("0e8d:7961", "mt7921u")],
                   firmware=["mediatek/WIFI_RAM_CODE_MT7961_1.bin"])
    no_driver = why(item("0bda:c832"), where)
    assert (no_driver["why"], no_driver["needs"]) == (hardware.NO_DRIVER, "6.19")
    assert why(item("0bda:8912"), where)["needs"] == "7.2"
    assert why(item("37ad:0103"), where)["needs"] == "7.0"
    assert why(item("37ad:0103"), dict(where, release="6.19.2"))["needs"] == "7.0"
    assert why(item("0bda:c832"), dict(where, release="6.19.2"))["why"] != hardware.NO_DRIVER
    assert why(item("2c4e:0128"), where)["why"] == hardware.TOO_NEW
    unknown = why(item("1234:5678"), where)
    assert (unknown["why"], unknown["needs"]) == (hardware.NO_DRIVER, "")
    assert why(item("1234:5678", bound=["vendor_wifi"]), where)["why"] == hardware.STALLED
    firmware = why(item("0bda:b832"), where)
    assert (firmware["why"], firmware["package"]) == (hardware.FIRMWARE, "firmware-realtek")
    assert hardware.stall_words(firmware)[1] == "Firmware missing. Install firmware-realtek."
    where["dpkg_info"].mkdir()
    (where["dpkg_info"] / "firmware-realtek.list").write_text("/lib/firmware/rtw89\n")
    installed = why(item("0bda:b832"), where)
    assert (installed["why"], installed["package"]) == (hardware.FIRMWARE, "")
    assert hardware.stall_words(installed)[1] == "Firmware missing. This system needs newer firmware."
    runner = Run(returncode=1)
    assert why(item("0e8d:7961"), where, runner)["why"] == hardware.STALLED and runner.calls == ["dmesg", "journalctl"]
    assert why(item("0e8d:7961"), where, Run(POWER_LINE))["why"] == hardware.POWER
    assert why(item("0e8d:7961", port="1-1"), where, Run(POWER_LINE))["why"] == hardware.STALLED
    assert why(item("0e8d:7961", port="1-1.3"), where, Run(POWER_LINE))["why"] == hardware.STALLED
    assert why(item("0e8d:7961"), where, Run("hwmon hwmon1: Undervoltage detected!\n"))["why"] == hardware.POWER
    alarm = tmp_path / "hwmon" / "hwmon1"
    alarm.mkdir(parents=True)
    (alarm / "in0_lcrit_alarm").write_text("1\n")
    assert why(item("0e8d:7961"), where)["why"] == hardware.POWER


def test_without_the_module_list_only_the_kernel_version_decides_there_is_no_driver(tmp_path):
    where = dict(system(tmp_path), modules=tmp_path / "nowhere")
    assert why(item("0bda:c832"), where)["why"] == hardware.NO_DRIVER
    assert why(item("0bda:b832"), where)["why"] == hardware.FIRMWARE
    assert why(item("1234:5678"), where)["why"] == hardware.STALLED


def test_firmware_is_found_plain_or_compressed(tmp_path):
    (tmp_path / "rtw88").mkdir()
    (tmp_path / "rtw88" / "rtw8821c_fw.bin.zst").write_text("fw")
    (tmp_path / "rt2870.bin.xz").write_text("fw")
    assert hardware.has_firmware("rtw88/rtw8821c_fw", tmp_path) and hardware.has_firmware("rt2870", tmp_path)
    assert not hardware.has_firmware("rtw88/rtw8822b_fw", tmp_path)
    assert not hardware.has_firmware("rtw89/rtw8852b_fw", tmp_path)


def test_every_reason_fits_on_its_card():
    canvas = gfx.Canvas()
    room = gfx.W - 57
    reasons = [dict(why=hardware.SETUP), dict(why=hardware.TOO_NEW), dict(why=hardware.POWER),
               dict(why=hardware.FIRMWARE, package=""), dict(why=hardware.NO_DRIVER, needs="7.0"),
               dict(why=hardware.STALLED), dict(why=hardware.NO_DRIVER), dict(why=hardware.NO_DRIVER, needs="6.19")]
    reasons += [dict(why=hardware.FIRMWARE, package=chip[3]) for chip in hardware.CHIPS.values() if chip[3]]
    for reason in reasons:
        title, detail = hardware.stall_words(dict(reason, label="RTL8852CU adapter"))
        assert canvas.text_width(detail, gfx.font("ui", 8)) <= room, detail
        assert canvas.text_width(title, gfx.font("bold", 10)) <= gfx.W - 52, title
        assert "—" not in detail and detail.endswith(".")
    for name, *_ in hardware.CHIPS.values():
        assert canvas.text_width(name + " adapter", gfx.font("bold", 10)) <= gfx.W - 52


def test_the_hardware_screen_says_why_an_adapter_does_not_start(tmp_path, drawn):
    stalled = [dict(item("0bda:c832"), label="RTL8852CU adapter", why=hardware.NO_DRIVER, needs="6.19"),
               dict(item("0bda:1a2b"), label="Wi-Fi adapter", why=hardware.SETUP)]
    app = make_app(tmp_path, fix=NO_FIX, hw=Hardware(stalled=stalled))
    press(app, "H")
    assert isinstance(app.screen, screens.Hardware)
    text = screen_text(app, drawn)
    assert "RTL8852CU adapter" in text and "No driver for it here. It needs Linux 6.19." in text
    assert "Wi-Fi adapter in setup mode" in text and "Replug it." in text


def test_the_reason_is_worked_out_once_each_time_an_adapter_is_plugged_in(monkeypatch):
    calls, plugged = [], [dict(item("0bda:c832"), plugged="7")]
    monkeypatch.setattr(app_module.power, "battery", lambda: None)
    for name in ("wifi_adapters", "bluetooth_adapters", "gps_candidates", "rtl_sdr_dongles"):
        monkeypatch.setattr(hardware, name, lambda: [])
    monkeypatch.setattr(app_module, "serial_ports", lambda: [])
    monkeypatch.setattr(hardware, "stalled_usb_adapters", lambda: [dict(entry) for entry in plugged])
    monkeypatch.setattr(hardware, "explain", lambda entry: calls.append(entry["plugged"]) or dict(entry, why="x"))
    clock = [100.0]
    monkeypatch.setattr(app_module.time, "monotonic", lambda: clock[0])
    watch = app_module.HardwareWatch()
    watch.refresh()
    assert watch.stalled == [] and calls == []
    for _ in range(3):
        clock[0] += app_module.STALL_SECONDS
        watch.refresh()
    assert calls == ["7"] and watch.stalled[0]["why"] == "x"
    plugged[0]["plugged"] = "8"
    watch.refresh()
    assert calls == ["7"] and watch.stalled == []
    clock[0] += app_module.STALL_SECONDS
    watch.refresh()
    assert calls == ["7", "8"]
    plugged.clear()
    watch.refresh()
    assert watch.stalled == [] and watch._why == {}


def test_python_package_declares_console_entrypoint_and_optional_pi_hardware():
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = metadata["project"]
    assert project["name"] == "quartermaster-pi-zero-2w"
    assert project["scripts"]["quartermaster"] == "quartermaster.__main__:main"
    assert "Pillow>=9.4" in project["dependencies"]
    assert "pyserial>=3.5" in project["dependencies"]
    assert "gpiod>=2.0" in project["optional-dependencies"]["pi-hat"]
    assert "spidev>=3.6" in project["optional-dependencies"]["pi-hat"]


def test_ble_probe_reports_missing_dbus_and_visible_adapter(monkeypatch):
    monkeypatch.setitem(sys.modules, "dbus", None)
    assert ble.probe() == "python3-dbus is unavailable"

    manager = types.SimpleNamespace(GetManagedObjects=lambda: {"/org/bluez/hci0": {"org.bluez.Adapter1": {}}})
    bus = types.SimpleNamespace(get_object=lambda *args: object())
    dbus = types.SimpleNamespace(SystemBus=lambda: bus, Interface=lambda *args: manager,
                                 exceptions=types.SimpleNamespace(DBusException=Exception))
    monkeypatch.setitem(sys.modules, "dbus", dbus)
    assert ble.probe() == "adapter /org/bluez/hci0"


def test_only_power_trouble_since_the_adapter_was_plugged_in_counts(tmp_path):
    where = system(tmp_path, aliases=[("0e8d:7961", "mt7921u")], firmware=["mediatek/WIFI_RAM_CODE_MT7961_1.bin"])
    plugged = item("0e8d:7961", plugged="9")
    new = "[   40.0] usb 1-1.2: new high-speed USB device number 9 using dwc2\n"
    gone = "[   30.0] usb 1-1.2: USB disconnect, device number 7\n"
    undervoltage = "[    5.0] hwmon hwmon1: Undervoltage detected!\n"
    assert why(plugged, where, Run(POWER_LINE + gone + new))["why"] == hardware.STALLED
    assert why(plugged, where, Run(undervoltage + new))["why"] == hardware.STALLED
    assert why(plugged, where, Run(new + POWER_LINE))["why"] == hardware.POWER
    assert why(plugged, where, Run(new + undervoltage))["why"] == hardware.POWER
    assert why(item("0e8d:7961"), where, Run(POWER_LINE + gone))["why"] == hardware.STALLED
    assert why(item("0e8d:7961"), where, Run(gone + POWER_LINE))["why"] == hardware.POWER
    other = "[   41.0] usb 1-1.3: new high-speed USB device number 9 using dwc2\n"
    assert why(plugged, where, Run(new + POWER_LINE + other))["why"] == hardware.POWER


def test_an_adapter_that_leaves_its_driver_disc_gets_its_own_few_seconds(monkeypatch):
    calls, plugged = [], [dict(item("0bda:1a2b"), plugged="7", disc=True)]
    monkeypatch.setattr(app_module.power, "battery", lambda: None)
    for name in ("wifi_adapters", "bluetooth_adapters", "gps_candidates", "rtl_sdr_dongles"):
        monkeypatch.setattr(hardware, name, lambda: [])
    monkeypatch.setattr(app_module, "serial_ports", lambda: [])
    monkeypatch.setattr(hardware, "stalled_usb_adapters", lambda: [dict(entry) for entry in plugged])
    monkeypatch.setattr(hardware, "explain", lambda entry: calls.append(entry["usb_id"]) or dict(entry, why="x"))
    clock = [100.0]
    monkeypatch.setattr(app_module.time, "monotonic", lambda: clock[0])
    watch = app_module.HardwareWatch()
    watch.refresh()
    clock[0] += app_module.STALL_SECONDS - 1
    watch.refresh()
    plugged[0] = dict(item("0bda:c811"), plugged="8")
    clock[0] += 2
    watch.refresh()
    assert watch.stalled == [] and calls == []
    clock[0] += app_module.STALL_SECONDS
    watch.refresh()
    assert calls == ["0bda:c811"] and watch.stalled[0]["usb_id"] == "0bda:c811"
