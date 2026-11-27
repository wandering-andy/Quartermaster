import csv
import io
import sys
import threading
import types

from quartermaster import config, gnss, hardware, wigle
from quartermaster.gnss import GPS, nmea_fix
from quartermaster.power import Rails
from quartermaster.tracker import BLE, Ends, Tracker, WIFI
from quartermaster.wifi import parse_iw_scan

SCAN = """BSS aa:bb:cc:dd:ee:01(on wlan1)
\tlast seen: 120 ms ago
\tfreq: 2437.0
\tcapability: ESS Privacy ShortSlotTime (0x0411)
\tsignal: -61.00 dBm
\tSSID: Black Pearl, "Guest"
\tDS Parameter set: channel 6
\tRSN:\t * Version: 1
\t\t * Group cipher: CCMP
\t\t * Pairwise ciphers: CCMP
\t\t * Authentication suites: PSK SAE
\t\t * Capabilities: 16-PTKSA-RC 1-GTKSA-RC MFP-capable (0x008c)
\tWPS:\t * Version: 1.0
BSS aa:bb:cc:dd:ee:02(on wlan1)
\tlast seen: 90000 ms ago
\tfreq: 5180
\tcapability: ESS (0x0001)
\tsignal: -80.00 dBm
\tSSID: \\x00\\x00\\x00
BSS aa:bb:cc:dd:ee:03(on wlan1)
\tlast seen: 10 ms ago
\tfreq: 6135
\tcapability: ESS Privacy (0x0011)
\tsignal: -70.00 dBm
\tSSID: caf\\xc3\\xa9
\tRSN:\t * Version: 1
\t\t * Group cipher: CCMP
\t\t * Pairwise ciphers: CCMP
\t\t * Authentication suites: IEEE 802.1X
\tWPA:\t * Version: 1
\t\t * Group cipher: TKIP
\t\t * Pairwise ciphers: TKIP
\t\t * Authentication suites: PSK
"""

PHY = """Wiphy phy1
\tBand 1:
\t\tFrequencies:
\t\t\t* 2412.0 MHz [1] (20.0 dBm)
\tBand 2:
\t\tFrequencies:
\t\t\t* 5180.0 MHz [36] (20.0 dBm)
\tBand 4:
\t\tFrequencies:
\t\t\t* 5955.0 MHz [1] (disabled)
\t\t\t* 6115.0 MHz [33] (12.0 dBm) (no IR)
\tSupported interface modes:
\t\t * managed
\t\t * monitor
\tsoftware interface modes (can always be added):
"""

FIX = {"valid": True, "latitude": 25.1234567, "longitude": -77.7654321, "altMSL": 101.26, "hdop": 0.9,
       "age_seconds": 0.4}
NOW = 1795000000


def test_iw_scan_capabilities_and_bands():
    a, b, c = parse_iw_scan(SCAN)
    assert a["ssid"] == 'Black Pearl, "Guest"' and a["channel"] == 6 and a["band"] == "2.4"
    assert a["capabilities"] == "[WPA2-PSK-CCMP][RSN-SAE-CCMP][WPS][ESS]" and a["security"] == "WPA2/3"
    assert b["hidden"] and b["ssid"] == "" and b["capabilities"] == "[ESS]" and b["channel"] == 36
    assert c["ssid"] == "café" and c["band"] == "6" and c["channel"] == 37
    assert c["capabilities"] == "[WPA-PSK-TKIP][WPA2-EAP-CCMP][ESS]"


def test_phy_bands():
    info = hardware.parse_iw_phy(PHY)
    assert info == {"bands": ["2.4", "5", "6"], "monitor": True}
    assert hardware.band_label(info["bands"]) == "tri-band"


def test_tracker_dedup_strongest_weakest():
    t = Tracker(relog_seconds=30, relog_meters=25, rssi_step=4)
    fix = dict(FIX)
    d, new, why = t.observe(WIFI, "AA", -70, NOW, fix, name="x", band="5", security="WPA2")
    assert new and why == "new"
    assert t.observe(WIFI, "AA", -69, NOW + 1, fix)[1:] == (False, None)
    assert t.observe(WIFI, "AA", -50, NOW + 2, fix)[2] == "stronger"
    far = dict(fix, latitude=25.1244567)
    assert t.observe(WIFI, "AA", -90, NOW + 3, far, name="")[2] == "weaker"
    assert t.observe(WIFI, "AA", -80, NOW + 40, fix)[2] == "moved"
    assert d.best[0] == -50 and d.worst[0] == -90 and d.worst[1] == far["latitude"] and d.name == "x"
    assert d.sightings == 5 and d.rows == 4
    assert t.summary()["wifi"] == 1 and t.summary()["bands"]["5"] == 1
    assert t.observe(WIFI, "BB", -60, NOW, {"valid": False})[2] is None


def test_slow_signal_changes_still_log_both_extremes():
    t = Tracker(relog_seconds=30, relog_meters=25, rssi_step=4)
    logged = []
    for i, rssi in enumerate(list(range(-80, -49)) + list(range(-51, -96, -1))):
        d, _, why = t.observe(WIFI, "AA", rssi, NOW + i, dict(FIX))
        if why:
            logged.append(rssi)
    assert logged[:4] == [-80, -76, -72, -68] and -52 in logged and -84 in logged
    assert d.best[0] == -50 and d.worst[0] == -95
    # Whatever the steps skipped is handed out exactly once, to be written at the end.
    extremes = [(point[0], point[3]) for _, point in t.take_extremes()]
    assert extremes == [(-50, NOW + 30), (-95, NOW + 75)]
    assert t.take_extremes() == []
    assert max(logged + [-50]) == d.best[0] and min(logged + [-95]) == d.worst[0]


def test_extremes_wait_until_a_device_is_left_behind():
    t = Tracker(rssi_step=4)
    t.observe(WIFI, "AA", -70, NOW, dict(FIX))
    t.observe(WIFI, "AA", -68, NOW + 1, dict(FIX))
    t.observe(WIFI, "BB", -70, NOW + 100, dict(FIX))
    t.observe(WIFI, "BB", -73, NOW + 101, dict(FIX))
    assert [(d.mac, p[0]) for d, p in t.take_extremes(idle_before=NOW + 50)] == [("AA", -68)]
    assert [(d.mac, p[0]) for d, p in t.take_extremes()] == [("BB", -73)]


def test_recent_devices_come_newest_first_however_many_there_are():
    tracker = Tracker()
    macs = ["AA:BB:CC:00:%02X:%02X" % (i >> 8, i & 255) for i in range(1000)]
    for i, mac in enumerate(macs):
        tracker.observe(WIFI if i % 2 else BLE, mac, -60, NOW + i // 10, FIX)
    tracker.observe(WIFI, macs[3], -50, NOW + 500, FIX)
    assert [d.mac for d in tracker.recent(3)] == [macs[3], macs[990], macs[991]]
    assert [d.mac for d in tracker.recent(2, (BLE,))] == [macs[990], macs[992]]
    assert [d.mac for d in tracker.found()] == macs
    assert tracker.forget(2000, NOW + 600) == [] and len(tracker.forget(900, NOW + 50)) == 280
    assert tracker.summary()["wifi"] == 500 and macs[3] in [d.mac for d in tracker.found()]


def test_ignored_devices_are_counted_but_never_logged():
    t = Tracker()
    d, new, why = t.observe(WIFI, "AA", -60, NOW, dict(FIX), name="Home", ignored=True)
    assert new and why is None and d.best is None and t.summary()["wifi"] == 1
    assert t.take_extremes() == []


def test_wigle_file_is_spec_exact(tmp_path):
    path = tmp_path / wigle.log_filename(NOW)
    w = wigle.WigleWriter(path, "13", display_profile="headless")
    net = parse_iw_scan(SCAN)[0]
    dev = {"mac": net["bssid"], "name": net["ssid"], "capabilities": net["capabilities"],
           "channel": net["channel"], "frequency": net["frequency"]}
    assert w.write("WIFI", dev, -61.0, FIX, NOW)
    assert w.write("BLE", {"mac": "11:22:33:44:55:66", "name": "bad\nname", "mfgr": 76}, -88, FIX, NOW)
    assert not w.write("WIFI", dev, -61, {"valid": False}, NOW)            # no fix
    assert not w.write("WIFI", dev, -61, FIX, 1000)                         # clock not set
    assert not w.write("WIFI", dev, -61, dict(FIX, latitude=0.0, longitude=0.0), NOW)
    w.close()
    lines = path.read_text(encoding="utf-8").split("\n")
    assert lines[0].startswith("WigleWifi-1.6,appRelease=Quartermaster ")
    assert "model=CardputerZero" not in lines[0] and "board=M5Stack CardputerZero" not in lines[0]
    assert "display=headless" in lines[0] and "device=Quartermaster" in lines[0]
    assert lines[1] == ",".join(wigle.COLUMNS)
    rows = list(csv.reader(io.StringIO("\n".join(lines[2:]))))
    assert rows[0] == ["aa:bb:cc:dd:ee:01", 'Black Pearl, "Guest"', "[WPA2-PSK-CCMP][RSN-SAE-CCMP][WPS][ESS]",
                       "2026-11-18 11:06:40", "6", "2437", "-61", "25.1234567", "-77.7654321", "101.3", "4.5",
                       "", "", "WIFI"]
    assert rows[1][1] == "bad name" and rows[1][12] == "76" and rows[1][13] == "BLE"
    assert len(rows) == 2 and all(len(r) == 14 for r in rows) and path.name.startswith("quartermaster_")


def test_gps_keeps_fix_and_hdop():
    gga = b"$GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,*47\r\n"
    report = nmea_fix(gga)
    gps = GPS()
    gps.update(report, now=100)
    snap = gps.snapshot(now=101)
    assert snap["valid"] and snap["hdop"] == 0.9 and round(snap["latitude"], 3) == 48.117
    assert not gps.snapshot(now=200)["valid"]


def test_gps_remembers_the_time_it_told_after_the_fix_is_lost():
    gps = GPS()
    assert gps.told() is None
    gps.update({"class": "TPV", "mode": 3, "lat": 25.07, "lon": -77.34}, now=90)
    assert gps.told() is None and gps.snapshot(now=91)["valid"]
    gps.update({"class": "TPV", "mode": 3, "lat": 25.07, "lon": -77.34, "time": "2026-03-04T05:06:07.000Z"}, now=100)
    assert gps.snapshot(now=101)["utc"] == "2026-03-04T05:06:07.000Z"
    gps.update({"class": "TPV", "mode": 1, "time": "2026-03-04T05:06:09.000Z"}, now=102)
    assert "utc" not in gps.snapshot(now=103) and "utc" not in gps.snapshot(now=5000)
    assert gps.told() == ("2026-03-04T05:06:07.000Z", 100)
    gps.update({"class": "TPV", "mode": 2, "lat": 25.08, "lon": -77.35, "time": "2026-03-04T06:00:00.000Z"}, now=3333)
    assert gps.told() == ("2026-03-04T06:00:00.000Z", 3333)


def test_gps_candidates_orders_usb_first(tmp_path):
    tty, dev = tmp_path / "tty", tmp_path / "dev"
    usb = tmp_path / "usb1"
    (usb / "1-1:1.0").mkdir(parents=True)
    (usb / "idVendor").write_text("1a86\n")
    (usb / "idProduct").write_text("7523\n")
    (tty / "ttyUSB0").mkdir(parents=True)
    (tty / "ttyUSB0" / "device").symlink_to(usb / "1-1:1.0")
    dev.mkdir()
    (dev / "ttyS0").write_text("")
    found = hardware.gps_candidates(tty, dev)
    assert [c["kind"] for c in found] == ["usb", "uart"] and found[0]["label"] == "CH340 USB GPS"
    assert found[1]["path"] == str(dev / "ttyS0")
    assert found[1]["label"] == "Cap / Grove UART"
    assert hardware.gps_candidates(tty, dev, cardputer=False, pi_uart=True)[1]["label"].startswith("Pi GPIO UART")
    assert [c["kind"] for c in hardware.gps_candidates(tty, dev, cardputer=False)] == ["usb"]


def test_pi_gpio_uart_gps_source_round_trips_in_settings(tmp_path):
    path = tmp_path / "config.json"
    cfg = config.load(path)
    cfg["gps_source"] = "/dev/serial0"
    config.save(cfg, path)
    assert config.load(path)["gps_source"] == "/dev/serial0"


def test_gps_candidates_never_include_the_bluetooth_uart(tmp_path):
    dev = tmp_path / "dev"
    dev.mkdir()
    (dev / "ttyS0").write_text("")
    (dev / "ttyAMA0").write_text("")
    (dev / "serial0").symlink_to(dev / "ttyAMA0")
    (dev / "serial1").symlink_to(dev / "ttyS0")
    assert [c["path"] for c in hardware.gps_candidates(tmp_path / "tty", dev)] == [str(dev / "ttyAMA0")]
    (dev / "serial0").unlink()
    assert hardware.gps_candidates(tmp_path / "tty", dev) == []


def test_rails_power_a_uart_gps_and_are_put_back(tmp_path):
    cap, grove, missing = tmp_path / "cap", tmp_path / "grove", tmp_path / "missing"
    cap.write_text("0\n")
    grove.write_text("1\n")
    rails = Rails([cap, grove, missing])
    assert rails.on() and not rails.on()
    assert cap.read_text() == "1" and grove.read_text() == "1\n"
    rails.restore()
    assert cap.read_text() == "0" and grove.read_text() == "1\n"


def test_gps_powers_rails_only_for_a_port_that_is_not_usb(tmp_path, monkeypatch):
    rail = tmp_path / "rail"
    rail.write_text("0")
    opened = []

    class Port:
        def __init__(self, path, baud, **_):
            opened.append((path, rail.read_text()))
            raise OSError("no such port")
    monkeypatch.setitem(sys.modules, "serial", types.SimpleNamespace(Serial=Port))
    monkeypatch.setattr(gnss, "WAKE_SECONDS", 0)
    stopped = threading.Event()
    gps = GPS("auto", candidates=lambda: [{"path": "/dev/ttyUSB0"}, {"path": "/dev/ttyS0"}], rails=Rails([rail]))
    gps._serial(stopped)
    assert opened == [("/dev/ttyUSB0", "0"), ("/dev/ttyS0", "1")]
    gps.power = False
    stopped.set()
    gps.run(stopped)
    assert rail.read_text() == "0"
    gps._serial(threading.Event())
    assert rail.read_text() == "0"


def test_managed_sweep_waits_for_results(monkeypatch):
    from quartermaster import wifi
    monkeypatch.setattr(wifi.shutil, "which", lambda name: "/usr/bin/" + name)
    now = [0.0]
    calls = []

    class Result:
        def __init__(self, code=0, out="", err=""):
            self.returncode, self.stdout, self.stderr = code, out, err

    def runner(args, **_):
        calls.append(list(args))
        if args[:1] == ["iw"] and args[-1] == "scan":
            return Result(255, "", "command failed: Operation not permitted (-1)")
        if args[0] == "nmcli":
            return Result()
        dumps = sum(1 for c in calls if c[-1] == "dump")
        # first poll: only stale cache; later polls: the fresh sweep
        return Result(0, SCAN.replace("120 ms ago", "60000 ms ago").replace("10 ms ago", "70000 ms ago")
                      if dumps == 1 else SCAN)

    scanner = wifi.AdapterScanner({"interface": "wlan1"}, runner=runner, clock=lambda: now[0])
    scanner.sleep = lambda s: now.__setitem__(0, now[0] + s)
    found = scanner.scan()
    assert [n["bssid"][-2:] for n in found] == ["01", "03"]      # the 90 s old entry is dropped
    assert scanner.method == "nm+iw" and scanner.direct is False
    scanner.scan()
    assert sum(1 for c in calls if c[-1] == "scan") == 1          # direct trigger is not retried


def test_closest_and_furthest_are_the_strongest_and_the_weakest_sighting_that_can_be_trusted():
    def ends(*points):
        found = Ends()
        for rssi, accuracy, time in points:
            found.add((rssi, 0, 0, time, None, accuracy))
        return [(p[0], p[5], p[3]) for p in found.chosen()]
    assert ends() == [] and ends((-60, 4.5, 1)) == [(-60, 4.5, 1)]
    assert ends((-60, 4.5, 1), (-80, 4.5, 2), (-70, 4.5, 3)) == [(-60, 4.5, 1), (-80, 4.5, 2)]
    assert ends((-60, 4.5, 1), (-93, 4.5, 2), (-92, 25, 3), (-85, 25.5, 4)) == [(-60, 4.5, 1), (-92, 25, 3)]
    assert ends((-60, 4.5, 1), (-95, 4.5, 2), (-80, 30, 3)) == [(-60, 4.5, 1)]
    assert ends((-60, 4.5, 1), (-80, "", 2), (-70, 15, 3)) == [(-60, 4.5, 1), (-70, 15, 3)]
    assert ends((-60, "", 1), (-80, "", 2), (-70, 15, 3)) == [(-60, "", 1), (-80, "", 2)]
    assert ends((-60, 15, 1), (-60, 4.5, 2), (-60, 4.5, 3)) == [(-60, 4.5, 2)]
    assert ends((-60, 4.5, 1), (-80, 15, 2), (-80, 4.5, 3), (-80, 4.5, 4)) == [(-60, 4.5, 1), (-80, 4.5, 3)]
    assert ends((-60, 30, 1), (-60, 4.5, 2)) == [(-60, 4.5, 2)]
    assert ends((-60, 30, 1), (-70, 30, 2)) == [(-60, 30, 1)]


def test_with_extremes_each_device_hands_out_its_closest_and_furthest_sighting_once():
    t = Tracker(relog_seconds=30, relog_meters=25, rssi_step=4, accuracy=wigle.accuracy_m, extremes=True)
    handed = []
    for i, rssi in enumerate([-70, -69, -50, -51, -95, -90, -80]):
        d, _, why = t.observe(WIFI, "AA", rssi, NOW + i, dict(FIX, latitude=25 + i / 1000))
        if why:
            handed.append((why, rssi))
    assert handed == [("new", -70), ("stronger", -50), ("weaker", -90)]
    assert t.observe(WIFI, "AA", -80, NOW + 40, dict(FIX, latitude=25.1))[2] is None
    assert (d.logged_best, d.logged_worst, t.kept) == (-50, -90, 2) and t.take_extremes() == []
    assert t.observe(WIFI, "AA", -91, NOW + 50, dict(FIX))[2] is None and t.forget(0, NOW + 100) == []
    assert [(p[0], p[3]) for _, p in t.take_extremes()] == [(-91, NOW + 50)] and t.take_extremes() == []
    assert not d.fresh and t.observe(WIFI, "AA", -70, NOW + 51, dict(FIX))[2] is None and not d.fresh
    assert t.log_rows(d) == 2 and t.kept == 2 and d.rows == 0
    t.new_part({(WIFI, "aa"): 2})
    assert t.kept == 0 and d.rows == 2 and t.log_rows(d) == 2
    assert t.observe(WIFI, "AA", -45, NOW + 60, dict(FIX))[2] == "stronger" and t.kept == 1 and t.log_rows(d) == 3
    t.observe(WIFI, "BB", -60, NOW, dict(FIX))
    t.observe(WIFI, "BB", -62, NOW + 1, dict(FIX))
    assert [d.mac for d in t.forget(0, NOW + 100)] == ["AA"]
    assert [(d.mac, p[0]) for d, p in t.take_extremes()] == [("BB", -62)]
    assert [d.mac for d in t.forget(0, NOW + 100)] == ["BB"] and t.kept == 3
    t.new_part({})
    assert t.kept == 0 and t.log_rows(d) == 2


def test_with_extremes_handing_out_the_ends_looks_only_at_devices_whose_ends_changed():
    t = Tracker(relog_seconds=30, relog_meters=25, rssi_step=4, accuracy=wigle.accuracy_m, extremes=True)
    for i in range(50):
        t.observe(WIFI, "%02X" % i, -60, NOW, dict(FIX))
    t.take_extremes()
    t.observe(WIFI, "07", -58, NOW + 1, dict(FIX))

    class Unwalked(dict):
        def values(self):
            raise AssertionError("every device was looked at")
    t.devices = Unwalked(t.devices)
    assert [(d.mac, p[0]) for d, p in t.take_extremes()] == [("07", -58)] and t.take_extremes() == []


def test_with_extremes_moving_or_a_sighting_too_weak_to_trust_does_not_hold_back_the_next_row():
    t = Tracker(relog_seconds=30, relog_meters=25, rssi_step=4, accuracy=wigle.accuracy_m, extremes=True)
    why = [t.observe(WIFI, "AA", rssi, NOW + 40 * i, dict(FIX, latitude=25 + i / 100))[2]
           for i, rssi in enumerate([-70, -67, -64, -61, -58])]
    assert why == ["new", None, "stronger", None, "stronger"]
    why = [t.observe(WIFI, "BB", rssi, NOW + i, dict(FIX))[2] for i, rssi in enumerate([-60, -99, -88, -86])]
    assert why == ["new", None, "weaker", None]
    far = dict(FIX, hdop=9)
    heard = [(-60, far), (-90, far), (-62, FIX)]
    why = [t.observe(WIFI, "CC", rssi, NOW + i, fix)[2] for i, (rssi, fix) in enumerate(heard)]
    assert why == ["new", None, "weaker"]


def test_boot_settings_win_over_saved_ones_and_bad_keys_are_named_without_their_values(tmp_path):
    import json

    from quartermaster import accounts, bootconf, config

    cfg_path, acct_path = tmp_path / "config.json", tmp_path / "accounts.json"
    config.save(dict(config.load(cfg_path), splash=False, country="GB"), cfg_path)
    accounts.save({"wdgwars_key": "keepkeepkeep"}, acct_path)
    handed = tmp_path / "settings"
    handed.write_text(json.dumps({
        "settings": {"country": "us", "ble": "off", "lora": "maybe", "nonsense": "1", "nodes": "x"},
        "accounts": {"wigle_name": "AID1234567890", "wigle_token": "supersecrettoken", "wardrift_key": "",
                     "bogus": "hunter2hunter2"}}))
    orig_path, orig_acct = config.PATH, accounts.PATH
    config.PATH, accounts.PATH = cfg_path, acct_path
    try:
        applied, problems = bootconf.apply(handed)
        saved, keys = config.load(cfg_path), accounts.load(acct_path)
    finally:
        config.PATH, accounts.PATH = orig_path, orig_acct
    assert sorted(applied) == ["ble", "country", "wigle_name", "wigle_token"]
    assert saved["country"] == "US" and saved["ble"] is False and saved["splash"] is False and saved["country_set"]
    assert keys["wigle_token"] == "supersecrettoken" and keys["wdgwars_key"] == "keepkeepkeep"
    assert len(problems) == 4 and not any("hunter2" in line or "maybe" in line for line in problems)
    assert bootconf.apply(tmp_path / "missing")[0] == []
