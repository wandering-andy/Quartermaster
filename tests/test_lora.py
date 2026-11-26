"""LoRa mesh wardriving with the Cap LoRa-1262: the radio, the packets, the nodes, the file and the uploads."""

import base64
import errno
import gzip
import hashlib
import hmac
import importlib.util
import inspect
import json
from pathlib import Path
import random
import struct
import sys
import time
import types
import uuid

import pytest

from quartermaster import config, engine, lora, mesh, meshpackets, uploads, wigle
from quartermaster.engine import Wardrive
from quartermaster.power import Rails
from tests.fakes import (ADAPTERS, FIX, NO_FIX, FakeCap, FakeRadio, Fleet, Gps, NoBle, meshcore_advert,
                         meshtastic_packet, nodeinfo, position)
from tests.test_core import NOW
from tests.test_uploads import KEYS, Site, make_log, uploader

NO_CRYPTO = importlib.util.find_spec("cryptography") is None
needs_crypto = pytest.mark.skipif(NO_CRYPTO, reason="the cryptography package is not installed")
US = lora.plan("US")


@pytest.fixture
def site():
    server = Site()
    yield server
    server.close()


def folder(path):
    path.mkdir(parents=True, exist_ok=True)
    return path


def test_the_country_gives_meshtastic_longfast_and_meshcore_public_where_the_cap_reaches():
    countries = ("US", "CA", "DE", "GB", "AU", "NZ", "KR", "BR")
    shown = {country: [(p.protocol, p.frequency_hz, p.bandwidth_hz, p.spreading_factor, p.coding_rate, p.sync_word,
                        p.preamble_symbols) for p in lora.plan(country)] for country in countries}
    longfast = ("meshtastic", 250000, 11, 5, 0x2B, 16)
    assert shown["US"] == shown["CA"] == [("meshtastic", 906875000) + longfast[1:],
                                          ("meshcore", 910525000, 62500, 7, 5, 0x12, 32)]
    assert shown["DE"] == shown["GB"] == [("meshtastic", 869525000) + longfast[1:],
                                          ("meshcore", 869618000, 62500, 8, 8, 0x12, 32)]
    assert shown["AU"] == [("meshtastic", 919875000) + longfast[1:], ("meshcore", 915800000, 250000, 10, 5, 0x12, 16)]
    assert shown["NZ"] == [("meshtastic", 919875000) + longfast[1:], ("meshcore", 917375000, 62500, 7, 5, 0x12, 32)]
    assert shown["NZ"][0] == shown["AU"][0]
    assert shown["KR"] == [("meshtastic", 922875000) + longfast[1:]]
    assert shown["BR"] == [("meshtastic", 903875000) + longfast[1:]]
    for country in ("JP", "TW", "IN", "CN", "HK", "01", "XX"):
        assert lora.plan(country) == [], country
    assert lora.region_name("DE") == "EU 868" and lora.region_name("01") == ""
    assert lora.region_name("AU") == "AU 915" and lora.region_name("NZ") == "NZ 915"


class Chip:
    """The SX1262, its antenna switch and its GPIO lines, behind stand-ins for spidev, smbus and gpiod."""

    def __init__(self, monkeypatch, tmp_path, expander_id=0xA2, gpio_error=None, modules=True):
        self.supply, self.mux, self.spi_path = tmp_path / "supply", tmp_path / "mux", tmp_path / "spidev0.1"
        self.supply.write_text("0")
        self.mux.write_text("1")
        self.spi_path.write_text("")
        self.expander = {0x01: expander_id, 0x03: 0x40, 0x05: 0xC0, 0x07: 0x0F}
        self.registers, self.sent, self.irq, self.packet = {}, [], 0, b""
        self.released = self.spi_closed = self.bus_closed = 0
        chip = self

        class Lines:
            def get_value(self, pin):
                return "ACTIVE" if pin == lora.IRQ and chip.irq else "INACTIVE"

            def reconfigure_lines(self, config):
                pass

            def set_value(self, pin, value):
                pass

            def release(self):
                chip.released += 1

        def request_lines(*args, **kwargs):
            if gpio_error is not None:
                raise gpio_error
            return Lines()
        gpiod = types.SimpleNamespace(request_lines=request_lines, LineSettings=lambda **kw: kw,
                                      line=types.SimpleNamespace(Direction=types.SimpleNamespace(INPUT="in",
                                                                                                 OUTPUT="out"),
                                                                 Value=types.SimpleNamespace(ACTIVE="ACTIVE",
                                                                                             INACTIVE="INACTIVE")))

        class Spi:
            mode = max_speed_hz = None

            def open(self, bus, device):
                assert (bus, device) == (0, 1)

            def xfer2(self, data):
                return chip.transfer(data)

            def close(self):
                chip.spi_closed += 1

        class Bus:
            def __init__(self, number):
                assert number == 1

            def read_byte_data(self, address, register):
                assert address == 0x43
                return chip.expander[register]

            def write_byte_data(self, address, register, value):
                chip.expander[register] = value

            def close(self):
                chip.bus_closed += 1
        if modules:
            monkeypatch.setitem(sys.modules, "gpiod", gpiod)
            monkeypatch.setitem(sys.modules, "spidev", types.SimpleNamespace(SpiDev=Spi))
            monkeypatch.setitem(sys.modules, "smbus", types.SimpleNamespace(SMBus=Bus))
        else:
            for name in ("gpiod", "spidev", "smbus", "smbus2"):
                monkeypatch.setitem(sys.modules, name, None)
        monkeypatch.setattr(lora.time, "sleep", lambda seconds: None)
        self.rails = Rails([self.supply])

    def radio(self):
        return lora.CapRadio(self.rails, self.supply, self.spi_path, self.mux)

    def transfer(self, data):
        self.sent.append(data[0])
        out = bytearray(len(data))
        opcode = data[0]
        if opcode in (0x0D, 0x1D):
            address = int.from_bytes(bytes(data[1:3]), "big")
            if opcode == 0x0D:
                self.registers[address] = bytes(data[3:])
            else:
                out[4:] = self.registers.get(address, bytes(len(data) - 4))
        elif opcode == 0x11:
            out[2] = 1
        elif opcode == 0x12:
            out[2:] = self.irq.to_bytes(2, "big")
        elif opcode == 0x02:
            self.irq &= ~int.from_bytes(bytes(data[1:]), "big")
        elif opcode == 0x13:
            out[2:] = bytes((len(self.packet), 42))
        elif opcode == 0x1E:
            assert data[1] == 42
            out[3:] = self.packet
        elif opcode == 0x14:
            out[2:] = bytes((177, 246, 155))
        return list(out)


def test_the_radio_is_powered_for_lora_and_the_gps_never_cuts_that_power(monkeypatch, tmp_path):
    chip = Chip(monkeypatch, tmp_path)
    radio = chip.radio().open(US[0])
    assert chip.supply.read_text() == "1" and chip.mux.read_text() == "0"
    assert chip.expander == {0x01: 0xA2, 0x03: 0x41, 0x05: 0xC1, 0x07: 0x0E}
    assert chip.registers[0x0740] == b"\x24\xb4"
    chip.rails.on("gps")
    chip.rails.restore("gps")
    assert chip.supply.read_text() == "1"
    radio.tune(US[1])
    assert chip.registers[0x0740] == b"\x14\x24"
    chip.packet, chip.irq = bytes(range(40)), 0x0002
    assert radio.receive(0) == (bytes(range(40)), -88.5, -2.5, True)
    chip.irq = 0x0042
    assert radio.receive(0)[3] is False and radio.counts == {"received": 2, "crc_errors": 1, "header_errors": 0}
    chip.irq = 0x0020
    assert radio.receive(0) is None and radio.counts["header_errors"] == 1
    radio.close()
    radio.close()
    assert chip.supply.read_text() == "0" and chip.mux.read_text() == "1"
    assert chip.expander == {0x01: 0xA2, 0x03: 0x40, 0x05: 0xC0, 0x07: 0x0F}
    assert chip.released == chip.spi_closed == chip.bus_closed == 1 and chip.sent[-1] == 0x80
    with pytest.raises(lora.RadioError):
        radio.receive(0)


def test_waveshare_hat_uses_spi0_ce0_and_documented_bcm_gpio(monkeypatch, tmp_path):
    device = tmp_path / "spidev0.0"
    device.write_text("")
    requests, opened, closed, outputs = [], [], [], []

    class Lines:
        def get_value(self, pin):
            assert pin in next(iter(requests[-1][2]))
            return "INACTIVE"

        def reconfigure_lines(self, config):
            outputs.append(config)

        def set_value(self, pin, value):
            pass

        def release(self):
            closed.append("gpio")

    gpiod = types.SimpleNamespace(
        request_lines=lambda path, consumer, config: requests.append((path, consumer, config)) or Lines(),
        LineSettings=lambda **values: values,
        line=types.SimpleNamespace(Direction=types.SimpleNamespace(INPUT="in", OUTPUT="out"),
                                   Value=types.SimpleNamespace(ACTIVE="ACTIVE", INACTIVE="INACTIVE")))

    class Spi:
        mode = max_speed_hz = None

        def __init__(self):
            self.registers = {}

        def open(self, bus, chip_select):
            opened.append((bus, chip_select))

        def xfer2(self, data):
            result = [0] * len(data)
            if data[0] == 0x0D:
                address = int.from_bytes(bytes(data[1:3]), "big")
                self.registers[address] = bytes(data[3:])
            elif data[0] == 0x1D:
                address = int.from_bytes(bytes(data[1:3]), "big")
                result[4:] = self.registers.get(address, bytes(len(data) - 4))
            elif data[0] == 0x11:
                result[2] = 1
            return result

        def close(self):
            closed.append("spi")

    def modules(antenna_switch):
        assert antenna_switch is False
        return gpiod, types.SimpleNamespace(SpiDev=Spi), None

    monkeypatch.setattr(lora, "_modules", modules)
    monkeypatch.setattr(lora.time, "sleep", lambda seconds: None)
    radio = lora.WaveshareRadio(device)
    assert radio.open(US[0]) is radio
    assert opened == [(0, 0)]
    assert list(requests[0][2]) == [(18, 20, 16, 6)]
    assert (lora.WAVESHARE_RESET, lora.WAVESHARE_BUSY, lora.WAVESHARE_IRQ) == (18, 20, 16)
    assert lora.WAVESHARE_TXEN == 6
    assert any(config.get(6, {}).get("output_value") == "ACTIVE" for config in outputs)
    assert radio._spi.registers[0x08AC] == b"\x94"
    assert radio.receive(0) is None
    radio.close()
    assert closed == ["spi", "gpio"]


def test_a_supply_the_gps_switched_on_stays_on_while_the_radio_uses_it(monkeypatch, tmp_path):
    chip = Chip(monkeypatch, tmp_path)
    chip.rails.on("gps")
    radio = chip.radio().open(US[0])
    chip.rails.restore("gps")
    assert chip.supply.read_text() == "1"
    radio.close()
    assert chip.supply.read_text() == "0"
    chip.rails.on("gps")
    chip.radio().open().close()
    assert chip.supply.read_text() == "1"
    chip.rails.restore_all()
    assert chip.supply.read_text() == "0"


def test_the_radio_never_sends_a_transmit_command():
    radio = lora.CapRadio(Rails([]))
    for opcode in (0x0E, 0x83, 0x8E, 0x95, 0xD1, 0xD2):
        assert opcode not in lora.COMMANDS
        with pytest.raises(lora.RadioError):
            radio._command(opcode)


def test_a_waiting_receive_ends_as_soon_as_the_wardrive_stops(monkeypatch, tmp_path):
    chip = Chip(monkeypatch, tmp_path)
    radio = chip.radio().open(US[0])
    waits = []
    assert radio.receive(5, lambda seconds: waits.append(seconds) or True) is None and waits == [lora.POLL]
    radio.close()


@pytest.mark.parametrize("trouble, state", [
    ("expander", lora.ABSENT), ("busy", lora.BUSY_ELSEWHERE), ("denied", lora.DENIED), ("modules", lora.MISSING)])
def test_a_cap_that_is_not_there_or_not_usable_is_left_as_it_was(monkeypatch, tmp_path, trouble, state):
    chip = Chip(monkeypatch, tmp_path, expander_id=0 if trouble == "expander" else 0xA2,
                gpio_error={"busy": OSError(errno.EBUSY, "busy"), "denied": PermissionError(13, "no")}.get(trouble),
                modules=trouble != "modules")
    with pytest.raises(lora.RadioError) as raised:
        chip.radio().open(US[0])
    assert str(raised.value) == state
    assert chip.supply.read_text() == "0" and chip.mux.read_text() == "1" and chip.sent == []
    assert chip.expander == {0x01: chip.expander[0x01], 0x03: 0x40, 0x05: 0xC0, 0x07: 0x0F}


def test_without_spidev_nothing_is_touched_and_no_thread_is_started(tmp_path):
    supply = tmp_path / "supply"
    supply.write_text("0")
    cap = lora.Cap(Rails([supply]), spi=tmp_path / "spidev0.1", supply=supply)
    cap.start()
    assert cap.state == lora.ABSENT and cap._thread is None and supply.read_text() == "0"
    off = lora.Cap(Rails([supply]), spi=tmp_path / "spidev0.1", supply=supply)
    off.start(False)
    assert off.state == lora.OFF and off._thread is None


def test_the_cap_is_looked_for_once_with_a_real_answer(monkeypatch, tmp_path):
    chip = Chip(monkeypatch, tmp_path)
    cap = lora.Cap(chip.rails, spi=chip.spi_path, supply=chip.supply, mux=chip.mux)
    cap.start()
    cap.wait(5)
    assert cap.found and chip.supply.read_text() == "0" and 0x11 in chip.sent
    quiet = Chip(monkeypatch, folder(tmp_path / "quiet"))
    quiet.transfer = lambda data: [0] * len(data)
    absent = lora.Cap(quiet.rails, spi=quiet.spi_path, supply=quiet.supply, mux=quiet.mux)
    absent.start()
    absent.wait(5)
    assert absent.state == lora.ABSENT and quiet.supply.read_text() == "0"


def test_the_cap_is_only_called_found_once_the_look_has_let_the_radio_go(tmp_path):
    seen = []

    class Held(FakeRadio):
        def close(self):
            seen.append(cap.state)
            super().close()

    (tmp_path / "spidev0.1").write_text("")
    cap = lora.Cap(Rails([]), spi=tmp_path / "spidev0.1", radio=lambda *args: Held())
    cap.start()
    cap.wait(5)
    assert seen == [lora.CHECKING] and cap.found


@needs_crypto
def test_meshtastic_nodeinfo_and_position_are_read_on_the_default_key_only():
    info = meshpackets.decode(meshtastic_packet(0x1A2B3C4D, 4, nodeinfo(0x1A2B3C4D, "Hilltop Rptr", "HTR", 2)),
                              "meshtastic")
    assert info == {"network": "meshtastic", "node": "1a2b3c4d", "via_mqtt": False, "hops": 0,
                    "name": "Hilltop Rptr", "short_name": "HTR", "role": "ROUTER"}
    client = meshpackets.decode(meshtastic_packet(0x1A2B3C4D, 4, nodeinfo(0x1A2B3C4D, "Pocket", "PKT")), "meshtastic")
    assert client["role"] == "CLIENT"
    place = meshpackets.decode(meshtastic_packet(0x0BADCAFE, 3, position(37.7749, -122.4194), hop_start=3,
                                                 hop_limit=1), "meshtastic")
    assert place["position"] == (37.7749, -122.4194) and place["hops"] == 2
    other = meshpackets.decode(meshtastic_packet(0x1A2B3C4D, 4, nodeinfo(0x1A2B3C4D, "Secret", "S"),
                                                 channel_hash=0x55), "meshtastic")
    assert other == {"network": "meshtastic", "node": "1a2b3c4d", "via_mqtt": False, "hops": 0}
    for bad in ((0x7FFFFFFF, 0), (0, 0)):
        body = struct.pack("<BiBi", 0x0D, bad[0], 0x15, bad[1])
        assert "position" not in meshpackets.decode(meshtastic_packet(5, 3, body), "meshtastic")
    mqtt = meshpackets.decode(meshtastic_packet(5, 4, nodeinfo(5, "Far", "F"), mqtt=True), "meshtastic")
    assert mqtt["via_mqtt"] is True


@needs_crypto
def test_a_payload_that_the_default_key_turns_into_nonsense_gives_no_name_or_position():
    from tests.fakes import pb_bytes, pb_uint
    plain = {"network": "meshtastic", "node": "1a2b3c4d", "via_mqtt": False, "hops": 0}
    for body in (nodeinfo(0x0BADCAFE, "Someone Else", "SE"), pb_bytes(1, b"!1a2b3c4d") + pb_bytes(2, b"\xff\xfe"),
                 pb_uint(2, 7), pb_bytes(2, b"x" * 81)):
        assert meshpackets.decode(meshtastic_packet(0x1A2B3C4D, 4, body), "meshtastic") == plain
    assert meshpackets.decode(meshtastic_packet(0x1A2B3C4D, 0, b""), "meshtastic") == plain
    assert meshpackets.decode(meshtastic_packet(0x1A2B3C4D, 3, pb_uint(1, 5)), "meshtastic") == plain


def test_a_meshtastic_header_names_its_sender_without_any_key():
    header = struct.pack("<IIIBBBB", 0xFFFFFFFF, 0x89ABCDEF, 0x12345678, 0x63, 0x55, 0, 0) + bytes(20)
    assert meshpackets.decode(header, "meshtastic") == {"network": "meshtastic", "node": "89abcdef",
                                                        "via_mqtt": False, "hops": 0}
    old = struct.pack("<IIIBBBB", 0xFFFFFFFF, 0x89ABCDEF, 1, 0x03, 0x55, 0, 0) + bytes(4)
    assert meshpackets.decode(old, "meshtastic")["hops"] is None
    for source in (0, 0xFFFFFFFF):
        assert meshpackets.decode(struct.pack("<IIIBBBB", 1, source, 1, 3, 8, 0, 0) + b"x", "meshtastic") is None


@needs_crypto
def test_a_meshcore_advert_counts_only_with_a_valid_signature():
    packet, key = meshcore_advert(name="Hilltop", role=2, lat=35.0, lon=-80.0, hops=2)
    advert = meshpackets.decode(packet, "meshcore")
    assert advert == {"network": "meshcore", "node": key[:16], "public_key": key, "via_mqtt": False, "hops": 2,
                      "role": "REPEATER", "position": (35.0, -80.0), "name": "Hilltop"}
    forged = bytearray(packet)
    forged[-1] ^= 1
    assert meshpackets.decode(bytes(forged), "meshcore") is None
    hidden, _ = meshcore_advert(seed=9, name="", role=1, hops=0)
    assert meshpackets.decode(hidden, "meshcore") == {"network": "meshcore", "node": _[:16], "public_key": _,
                                                      "via_mqtt": False, "hops": 0, "role": "COMPANION"}
    assert meshpackets.decode(b"\x09\x00" + bytes(40), "meshcore") is None


def test_no_ed25519_means_no_meshcore_node(monkeypatch):
    monkeypatch.setattr(meshpackets, "_verify", lambda *args: None)
    assert meshpackets.decode(b"\x11\x00" + bytes(100), "meshcore") is None


def test_random_bytes_never_break_the_decoder():
    rng = random.Random(7)
    for _ in range(3000):
        payload = bytes(rng.randrange(256) for _ in range(rng.randrange(0, 270)))
        for protocol in ("meshtastic", "meshcore"):
            found = meshpackets.decode(payload, protocol)
            assert found is None or found["network"] == protocol


def sighting(network="meshtastic", node="1a2b3c4d", **fields):
    return dict({"network": network, "node": node, "via_mqtt": False, "hops": 0}, **fields)


def test_each_node_keeps_its_closest_and_furthest_sighting_and_a_direct_one_wins():
    store = mesh.Mesh()
    here, there, far = (25.07, -77.34, 3.0), (25.08, -77.35, 4.0), (25.20, -77.50, 5.0)
    store.hear(sighting(hops=1), -80, 5, NOW, here)
    node = store.nodes[("meshtastic", "1a2b3c4d")]
    assert node.near == node.far == (-80.0, 5.0, 1, 25.07, -77.34, 3.0, NOW)
    store.hear(sighting(hops=0), -110, -4, NOW + 10, far)
    assert node.near == node.far == (-110.0, -4.0, 0, 25.2, -77.5, 5.0, NOW + 10)
    store.hear(sighting(hops=0), -70, 9, NOW + 20, there)
    store.hear(sighting(hops=2), -40, 12, NOW + 30, here)
    store.hear(sighting(hops=0), -50, 10, NOW + 40, None)
    assert node.near[0] == -70 and node.far[0] == -110 and node.best[0] == -50 and node.heard == 5
    assert (node.first, node.last) == (NOW, NOW + 40)
    store.hear(sighting(name="Hilltop", short_name="HTR", role="ROUTER", position=(25.1, -77.4)), -90, 1, NOW + 50)
    assert (node.name, node.short_name, node.role, node.own) == ("Hilltop", "HTR", "ROUTER", (25.1, -77.4, NOW + 50))
    assert store.hear(sighting(node="0badcafe", via_mqtt=True), -60, 5, NOW, here) is None
    assert store.hear(sighting(node="0badcafe"), -60, None, NOW, here) is None
    assert store.hear(sighting(node="!0badcafe"), -60, 1, NOW, here) is None
    assert len(store) == 1


def heard_store():
    store = mesh.Mesh()
    place = (25.07805, -77.34312, 4.0)
    key = "ab" * 32
    store.hear(sighting(name="Hilltop Rptr", short_name="HTR", role="ROUTER", position=(25.1, -77.4)), -92.5, 6.25,
               NOW, place)
    store.hear(sighting(node="0badcafe", name="Pocket"), -101, -3.5, NOW + 5, None)
    store.hear(sighting("meshcore", key[:16], public_key=key, role="REPEATER", name="Ridge",
                        position=(25.3, -77.6), hops=2), -110, -8, NOW + 9, place)
    return store


def test_the_mesh_file_and_its_summary(tmp_path):
    path = tmp_path / "quartermaster_20261118-110640.mesh.json.gz"
    assert not mesh.write(path, mesh.Mesh(), NOW) and not path.exists()
    assert mesh.write(path, heard_store(), NOW + 60, ["Meshtastic LongFast 906.875 MHz"])
    data = json.loads(gzip.decompress(path.read_bytes()))
    assert data == mesh.load(path)
    listened = ["Meshtastic LongFast 906.875 MHz"]
    assert (data["format"], data["now"], data["listened"]) == ("quartermaster-mesh-1", NOW + 60, listened)
    first, second, third = data["nodes"]
    assert first["node_id"] == "1a2b3c4d" and first["closest"] == first["furthest"] == {
        "lat": 25.07805, "lon": -77.34312, "accuracy": 4.0, "rssi": -92.5, "snr": 6.25, "hops": 0, "time": NOW}
    assert first["reported"] == {"lat": 25.1, "lon": -77.4, "time": NOW} and first["role"] == "ROUTER"
    assert second["node_id"] == "0badcafe" and "closest" not in second and second["rssi"] == -101
    assert third["network"] == "meshcore" and third["public_key"] == "ab" * 32 and third["hops"] == 2
    summary = mesh.summary_path(path).read_text().splitlines()
    assert summary[0].split(",") == list(mesh.SUMMARY_COLUMNS) and len(summary) == 4
    assert summary[1].startswith("meshtastic,1a2b3c4d,Hilltop Rptr,HTR,ROUTER,2026-11-18 ")


def test_a_mesh_journal_left_by_a_power_cut_becomes_its_file(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    name = "quartermaster_20261118-110640.mesh.json.gz"
    mesh.save_journal(mesh.journal_path(logs / name), heard_store(), name, NOW + 30)
    assert engine.leftovers(logs)
    assert engine.recover(logs) == 1
    assert [n["node_id"] for n in mesh.load(logs / name)["nodes"]] == ["1a2b3c4d", "0badcafe", "ab" * 8]
    assert not list(logs.glob(".*")) and not engine.leftovers(logs)


def test_wdgwars_records_take_each_node_where_it_says_it_is(tmp_path):
    path = tmp_path / "quartermaster_20261118-110640.mesh.json.gz"
    mesh.write(path, heard_store(), NOW + 60)
    records = mesh.wdgwars_records(mesh.load(path))
    assert records == [
        {"node_id": "1a2b3c4d", "node_type": "REPEATER", "name": "Hilltop Rptr", "lat": 25.1, "lon": -77.4,
         "rssi": -92.5, "first_seen": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(NOW)), "type": "MESHCORE",
         "network": "meshtastic", "path_hops": 0},
        {"node_id": "ab" * 8, "node_type": "REPEATER", "name": "Ridge", "lat": 25.3, "lon": -77.6, "rssi": -110.0,
         "first_seen": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(NOW + 9)), "type": "MESHCORE",
         "network": "meshcore", "public_key": "ab" * 32, "path_hops": 2}]
    assert [mesh.node_type({"network": "meshtastic", "role": role}) for role in
            ("CLIENT", "TRACKER", "ROUTER_LATE", "LOST_AND_FOUND", "", "CLIENT_BASE")] == \
        ["COMPANION", "COMPANION", "REPEATER", "SENSOR", "COMPANION", "COMPANION"]
    assert [mesh.node_type({"network": "meshcore", "role": role}) for role in ("ROOM_SERVER", "", "SENSOR")] == \
        ["ROOM_SERVER", "REPEATER", "SENSOR"]


def mesh_file(tmp_path, store=None):
    path = folder(tmp_path) / "quartermaster_20261118-110640.mesh.json.gz"
    mesh.write(path, store or heard_store(), NOW + 60)
    return path


def test_wdgwars_gets_the_nodes_signed_and_says_what_it_took(site, tmp_path):
    path = mesh_file(tmp_path)
    site.replies[("POST", "/api/upload")] = (200, {"ok": True, "meshcore_imported": 1, "meshcore_already_seen": 0,
                                                   "meshcore_rejected": 1,
                                                   "meshcore_reject_reasons": {"no_gps": 1}})
    outcome = uploads.upload("wdgwars", path, KEYS, site.endpoints)
    assert (outcome.state, outcome.detail) == (uploads.DONE, "WDGWars took 1 new, 1 refused (no_gps 1). "
                                                             "1 gave no position")
    request = site.requests[0]
    assert request["headers"]["x-api-key"] == KEYS["wdgwars_key"]
    assert request["headers"]["content-type"] == "application/json"
    body = json.loads(request["body"])
    assert set(body) == {"data", "nonce", "sig"} and len(body["nonce"]) == 16 and int(body["nonce"], 16) >= 0
    expected = hmac.new(KEYS["wdgwars_key"].encode(), (body["nonce"] + body["data"]).encode(), hashlib.sha256)
    assert body["sig"] == expected.hexdigest()
    payload = json.loads(base64.b64decode(body["data"]))
    assert payload == {"networks": [], "aircraft": [], "meshcore_nodes": mesh.wdgwars_records(mesh.load(path))}
    assert KEYS["wdgwars_key"].encode() not in request["body"]


@pytest.mark.parametrize("reply, state, detail", [
    ({"ok": True}, uploads.REJECTED, "WDGWars answered but took none of the 2 nodes"),
    ({"ok": True, "meshcore_imported": 0, "meshcore_already_seen": 0, "info": "Already uploaded recently"},
     uploads.DUPLICATE, "WDGWars already had the 2 nodes"),
    ({"ok": True, "meshcore_already_seen": 2}, uploads.DUPLICATE, "WDGWars already had the 2 nodes"),
    ({"ok": True, "meshcore_imported": 1, "meshcore_already_seen": 1}, uploads.DONE,
     "WDGWars took 1 new, 1 already seen. 1 gave no position"),
    ({"ok": True, "meshcore_rejected": 2, "meshcore_reject_reasons": {"bad_public_key": 1, "no_gps": 1}},
     uploads.REJECTED, "WDGWars refused all 2 nodes (bad_public_key 1, no_gps 1)"),
    ({"ok": False, "error": "Invalid data format"}, uploads.REJECTED, "WDGWars refused the nodes: Invalid data format"),
])
def test_wdgwars_answers_about_mesh_nodes(site, tmp_path, reply, state, detail):
    site.replies[("POST", "/api/upload")] = (200, reply)
    outcome = uploads.upload("wdgwars", mesh_file(tmp_path), KEYS, site.endpoints)
    assert (outcome.state, outcome.detail) == (state, detail)


def test_wdgwars_already_having_every_node_counts_as_sent(site, tmp_path):
    site.replies[("POST", "/api/upload")] = (200, {"ok": True, "meshcore_already_seen": 2})
    store = heard_store()
    del store.nodes[("meshtastic", "0badcafe")]
    outcome = uploads.upload("wdgwars", mesh_file(tmp_path, store), KEYS, site.endpoints)
    assert (outcome.state, outcome.detail) == (uploads.DUPLICATE, "WDGWars already had the 2 nodes")


def test_wdgwars_gets_1000_nodes_a_request_and_a_file_with_no_position_is_not_sent(site, tmp_path):
    store = mesh.Mesh()
    for i in range(2300):
        store.hear(sighting(node="%08x" % (i + 1), position=(25.0 + i / 1e4, -77.0)), -90, 1, NOW, None)
    site.replies[("POST", "/api/upload")] = (200, {"ok": True, "meshcore_imported": 1000})
    outcome = uploads.upload("wdgwars", mesh_file(tmp_path, store), KEYS, site.endpoints)
    sizes = [len(json.loads(base64.b64decode(json.loads(r["body"])["data"]))["meshcore_nodes"]) for r in site.requests]
    assert sizes == [1000, 1000, 300] and outcome.detail == "WDGWars took 3000 new"
    blind = mesh.Mesh()
    blind.hear(sighting(), -90, 1, NOW, (25.0, -77.0, 3.0))
    outcome = uploads.upload("wdgwars", mesh_file(tmp_path / "x", blind), KEYS, site.endpoints)
    assert (outcome.state, outcome.detail) == (uploads.NOTHING, "Nothing to send. No node gave its own position")
    assert len(site.requests) == 3


def test_a_mesh_file_with_nothing_for_a_site_is_settled_and_not_counted_as_refused(site, tmp_path):
    blind = mesh.Mesh()
    blind.hear(sighting(), -90, 1, NOW, (25.0, -77.0, 3.0))
    path = mesh_file(tmp_path / "logs", blind)
    WardriftMesh(site, lambda request: (200, receipt(request)))
    up = uploader(tmp_path, site)
    assert up.run_pass() is False and up.run_pass() is False
    assert [r["path"] for r in site.requests] == ["/v1/ingest/mesh"]
    assert up.marks(path.name) == {"wigle": "none", "wdgwars": "none", "wardrift": "done"}
    status = up.status()["services"]["wdgwars"]
    assert (status["refused"], status["waiting"], status["sent"]) == (0, 0, 0)
    assert up.ledger.entry(path.name, "wdgwars")["detail"] == "Nothing to send. No node gave its own position"


@pytest.mark.parametrize("reply, state, detail", [
    ({"error": "payload-too-large"}, uploads.REJECTED, "WDGWars says the file is too large"),
    ({}, uploads.RETRY, "WDGWars is busy (413). Will try again"),
    (b"<html>413 Request Entity Too Large</html>", uploads.RETRY, "WDGWars is busy (413). Will try again"),
])
def test_wdgwars_413_on_mesh_nodes_is_tried_again_unless_it_says_too_large(site, tmp_path, reply, state, detail):
    site.replies[("POST", "/api/upload")] = (413, reply)
    outcome = uploads.upload("wdgwars", mesh_file(tmp_path), KEYS, site.endpoints)
    assert (outcome.state, outcome.detail) == (state, detail)


def receipt(request, **fields):
    body = json.loads(request["body"])
    return dict({"ok": True, "receipt_id": str(uuid.uuid4()), "batch_id": body["batch_id"], "reports_accepted": 0,
                 "samples": []}, **fields)


class WardriftMesh:
    """Wardrift's mesh route, answering each request with a receipt for its own batch."""

    def __init__(self, site, answer=receipt):
        self.site = site
        handler = site.server.RequestHandlerClass
        original = handler.answer

        def answer_mesh(handler_self):
            if handler_self.path != "/v1/ingest/mesh":
                return original(handler_self)
            body = handler_self.rfile.read(int(handler_self.headers.get("Content-Length") or 0))
            request = {"path": handler_self.path, "body": body,
                       "headers": {k.lower(): v for k, v in handler_self.headers.items()}}
            site.requests.append(request)
            status, reply = answer(request)
            data = reply if isinstance(reply, bytes) else json.dumps(reply).encode()
            handler_self.send_response(status)
            handler_self.send_header("Content-Length", str(len(data)))
            handler_self.end_headers()
            handler_self.wfile.write(data)
        handler.do_POST = answer_mesh


def test_wardrift_gets_every_node_heard_in_its_mesh_shape(site, tmp_path):
    WardriftMesh(site, lambda request: (200, receipt(request)))
    cfg, changes = {}, []
    reporter = uploads.Reporter(cfg, clock=lambda: NOW)
    reporter.on_change = lambda: changes.append(dict(cfg))
    outcome = uploads.upload("wardrift", mesh_file(tmp_path), KEYS, site.endpoints, reporter=reporter)
    assert outcome.state == uploads.DONE and outcome.detail == "Wardrift has it (3 nodes)"
    request = site.requests[0]
    assert request["headers"]["user-agent"].startswith("Mozilla/5.0") and \
        request["headers"]["x-api-key"] == KEYS["wardrift_key"]
    body = json.loads(request["body"])
    assert set(body) == {"now", "nodes", "batch_id", "reporter_id", "sequence_no"}
    assert body["reporter_id"] == cfg["wardrift_reporter"] and uuid.UUID(body["batch_id"])
    assert body["sequence_no"] == NOW == cfg["wardrift_sequence"] and changes
    assert body["now"].endswith("Z") and "." in body["now"]
    assert body["nodes"] == [
        {"network": "meshtastic", "node_id": "1a2b3c4d", "node_type": "REPEATER", "name": "Hilltop Rptr",
         "lat": 25.1, "lon": -77.4, "snr": 6.25, "hops": 0, "last_heard": mesh._iso(NOW)},
        {"network": "meshtastic", "node_id": "0badcafe", "node_type": "COMPANION", "name": "Pocket", "snr": -3.5,
         "hops": 0, "last_heard": mesh._iso(NOW + 5)},
        {"network": "meshcore", "node_id": "ab" * 8, "public_key": "ab" * 32, "node_type": "REPEATER",
         "name": "Ridge", "lat": 25.3, "lon": -77.6, "snr": -8.0, "hops": 2, "last_heard": mesh._iso(NOW + 9)}]
    uploads.upload("wardrift", mesh_file(tmp_path), KEYS, site.endpoints, reporter=reporter)
    again = json.loads(site.requests[1]["body"])
    assert again["sequence_no"] == NOW + 1 and again["reporter_id"] == body["reporter_id"]
    assert again["batch_id"] != body["batch_id"]


def test_wardrift_gets_100_nodes_a_request_each_with_a_higher_number(site, tmp_path):
    WardriftMesh(site, lambda request: (200, receipt(request)))
    store = mesh.Mesh()
    for i in range(250):
        store.hear(sighting(node="%08x" % (i + 1)), -90, 1, NOW, None)
    cfg = {"wardrift_sequence": NOW + 500}
    outcome = uploads.upload("wardrift", mesh_file(tmp_path, store), KEYS, site.endpoints,
                             reporter=uploads.Reporter(cfg, clock=lambda: NOW))
    bodies = [json.loads(r["body"]) for r in site.requests]
    assert [len(b["nodes"]) for b in bodies] == [100, 100, 50] and outcome.detail == "Wardrift has it (250 nodes)"
    assert [b["sequence_no"] for b in bodies] == [NOW + 501, NOW + 502, NOW + 503]
    assert all(len(r["body"]) <= mesh.WARDRIFT_MAX_BYTES for r in site.requests)


@pytest.mark.parametrize("answer, state, detail", [
    (lambda r: (200, receipt(r, batch_id=str(uuid.uuid4()))), uploads.REJECTED,
     "Wardrift did not confirm the nodes"),
    (lambda r: (200, {"ok": True}), uploads.REJECTED, "Wardrift did not confirm the nodes"),
    (lambda r: (409, {"error": "sequence_conflict"}), uploads.RETRY,
     "Wardrift is busy with another upload. Will try again"),
    (lambda r: (403, {"error": "mesh endpoint requires a node device"}), uploads.REJECTED,
     "Wardrift takes mesh nodes only from a node key"),
    (lambda r: (403, b"<html>error code: 1010</html>"), uploads.RETRY,
     "Wardrift is blocking the upload. Will try again"),
    (lambda r: (403, {"error": "forbidden"}), uploads.REJECTED, "Wardrift refused the nodes (403): forbidden"),
    (lambda r: (401, {"error": "invalid key"}), uploads.AUTH, "Wardrift did not accept the key"),
    (lambda r: (422, {"error": "invalid", "message": "bad node_id"}), uploads.REJECTED,
     "Wardrift refused the file (422): bad node_id"),
    (lambda r: (503, {}), uploads.RETRY, "Wardrift is busy (503). Will try again"),
])
def test_wardrift_answers_about_mesh_nodes(site, tmp_path, answer, state, detail):
    WardriftMesh(site, answer)
    outcome = uploads.upload("wardrift", mesh_file(tmp_path), KEYS, site.endpoints,
                             reporter=uploads.Reporter({}, clock=lambda: NOW))
    assert (outcome.state, outcome.detail) == (state, detail)


def test_a_wardrift_403_on_mesh_nodes_does_not_stop_the_wifi_logs(site, tmp_path):
    site.accept_everything()
    site.replies[("POST", "/api/upload")] = (200, {"ok": True, "meshcore_imported": 2})
    WardriftMesh(site, lambda request: (403, {"error": "mesh endpoint requires a node device"}))
    log = make_log(tmp_path / "logs", "quartermaster_20261118-110640.wigle.csv")
    later = make_log(tmp_path / "logs", "quartermaster_20261118-120000.wigle.csv")
    path = mesh_file(tmp_path / "logs")
    up = uploader(tmp_path, site)
    up.run_pass()
    sent = [r["path"] for r in site.requests if r["path"].startswith("/v1/")]
    assert sent == ["/v1/ingest/mesh", "/v1/ingest/signals", "/v1/ingest/signals"]
    assert "wardrift" not in up.blocked and up.marks(path.name)["wardrift"] == "rejected"
    assert up.marks(log.name)["wardrift"] == up.marks(later.name)["wardrift"] == "done"


def test_wardrift_mesh_batches_already_confirmed_are_not_sent_again(site, tmp_path):
    failing = [True]

    def answer(request):
        if len(json.loads(request["body"])["nodes"]) == 50 and failing[0]:
            return 503, {}
        return 200, receipt(request)
    WardriftMesh(site, answer)
    store = mesh.Mesh()
    for i in range(250):
        store.hear(sighting(node="%08x" % (i + 1), position=(25.0, -77.0)), -90, 1, NOW, None)
    path = mesh_file(tmp_path / "logs", store)
    site.replies[("POST", "/api/upload")] = (200, {"ok": True, "meshcore_imported": 250})
    up = uploader(tmp_path, site)
    assert up.run_pass() is True
    assert up.ledger.entry(path.name, "wardrift")["sent"] == 200
    failing[0] = False
    assert up.run_pass() is False
    bodies = [json.loads(r["body"]) for r in site.requests if r["path"] == "/v1/ingest/mesh"]
    assert [len(b["nodes"]) for b in bodies] == [100, 100, 50, 50]
    assert bodies[3]["nodes"] == bodies[2]["nodes"] and bodies[3]["batch_id"] != bodies[2]["batch_id"]
    entry = up.ledger.entry(path.name, "wardrift")
    assert entry["state"] == uploads.DONE and entry["detail"] == "Wardrift has it (250 nodes)" and "sent" not in entry
    rejected = uploads.Outcome(uploads.REJECTED, "Wardrift did not confirm the nodes", sent=100)
    up.ledger.record(path.name, "wardrift", rejected, NOW)
    up.ledger.reopen(path.name)
    assert up.ledger.entry(path.name, "wardrift") == {}


def test_mesh_files_never_go_to_wigle_and_the_uploader_sends_them_to_the_others(site, tmp_path):
    WardriftMesh(site, lambda request: (200, receipt(request)))
    site.replies[("POST", "/api/upload")] = (200, {"ok": True, "meshcore_imported": 2})
    path = mesh_file(tmp_path / "logs")
    assert uploads.upload("wigle", path, KEYS, site.endpoints).detail == "Not for WiGLE" and not site.requests
    up = uploader(tmp_path, site)
    up.run_pass()
    assert sorted(r["path"] for r in site.requests) == ["/api/upload", "/v1/ingest/mesh"]
    assert up.marks(path.name) == {"wigle": "none", "wdgwars": "done", "wardrift": "done"}
    assert up.cfg["wardrift_sequence"] and uuid.UUID(up.cfg["wardrift_reporter"])
    assert uploads.sites(path.name) == ("wdgwars", "wardrift")


def test_the_wardrift_reporter_and_its_number_are_kept_in_the_settings(tmp_path):
    path = tmp_path / "config.json"
    cfg = config.load(path)
    assert cfg["lora"] == "auto" and "wardrift_reporter" not in cfg
    reporter = uploads.Reporter(cfg, clock=lambda: 5)
    first, number = reporter.id(), reporter.next()
    config.save(cfg, path)
    again = config.load(path)
    assert again["wardrift_reporter"] == first and again["wardrift_sequence"] == number == 5
    assert uploads.Reporter(again, clock=lambda: 1).next() == 6
    path.write_text(json.dumps({"wardrift_reporter": "x; rm", "wardrift_sequence": "9", "lora": "maybe"}))
    broken = config.load(path)
    assert "wardrift_reporter" not in broken and "wardrift_sequence" not in broken and broken["lora"] == "auto"


def drive(tmp_path, cap=None, fix=FIX, **settings):
    cfg = dict(config.DEFAULTS, ble=False, **settings)
    return Wardrive(cfg, Gps(fix), root=tmp_path, adapters=ADAPTERS, ble=NoBle(), fleet=Fleet, clock=lambda: NOW,
                    cap=cap)


def wait_for(test, seconds=5):
    deadline = time.monotonic() + seconds
    while not test() and time.monotonic() < deadline:
        time.sleep(0.002)
    assert test()


@needs_crypto
def test_a_wardrive_listens_on_auto_once_the_cap_is_found_and_saves_the_nodes_beside_the_log(tmp_path):
    cap = FakeCap()
    radio = cap.the_radio
    wardrive = drive(tmp_path, cap)
    wardrive.start()
    assert wardrive.mesh is not None and wardrive.mesh.plan == US
    radio.hear(meshtastic_packet(0x1A2B3C4D, 4, nodeinfo(0x1A2B3C4D, "Hilltop", "HTR", 2)), rssi=-88, snr=7.5)
    radio.hear(meshtastic_packet(0x0BADCAFE, 3, position(25.1, -77.4), mqtt=True))
    radio.hear(b"\x00" * 30, crc=False)
    wait_for(lambda: not radio.packets)
    wait_for(lambda: wardrive.status()["mesh"] == 1)
    status = wardrive.status()["lora"]
    assert status["state"] == mesh.LISTENING and status["packets"] == 2
    assert status["detail"] == "Meshtastic LongFast 906.875 MHz" and radio.opened == [US[0]]
    wardrive._wifi_sink([{"bssid": "AA:BB:CC:DD:EE:01", "ssid": "net", "rssi": -60, "channel": 6,
                          "frequency": 2437, "security": "WPA2", "capabilities": "[WPA2-PSK-CCMP][ESS]",
                          "band": "2.4"}], ADAPTERS[0])
    wardrive.stop()
    assert radio.closed == 1
    log = wardrive.log_path
    assert wardrive.mesh_path == mesh.file_for(log) and wardrive.status()["mesh_log"] == str(wardrive.mesh_path)
    node = mesh.load(wardrive.mesh_path)["nodes"][0]
    assert (node["name"], node["closest"]["rssi"], node["closest"]["lat"]) == ("Hilltop", -88.0, FIX["latitude"])


def test_the_two_meshes_take_turns_on_the_one_radio(tmp_path):
    cap = FakeCap()
    listener = mesh.Listener(tmp_path, Gps(FIX), cap.radio, US, clock=lambda: NOW, dwell=0.05)
    listener.start(NOW)
    wait_for(lambda: len(cap.the_radio.tuned) >= 3)
    listener.stop()
    assert cap.the_radio.tuned[:3] == [US[1], US[0], US[1]] and cap.the_radio.closed == 1
    assert lora.DWELL_SECONDS == inspect.signature(mesh.Listener).parameters["dwell"].default == 30.0


def test_without_the_cap_a_wardrive_is_as_it_was(tmp_path):
    for cap in (None, FakeCap(lora.ABSENT), FakeCap(lora.CHECKING)):
        wardrive = drive(tmp_path, cap)
        wardrive.start()
        assert wardrive.mesh is None and wardrive.status()["mesh"] is None and wardrive.status()["lora"] is None
        wardrive.stop()
        assert not list((tmp_path / "logs").glob("*.mesh.*"))
    assert drive(tmp_path, FakeCap(), lora=False)._mesh_wanted() is False
    assert drive(tmp_path, FakeCap(), country="JP")._mesh_wanted() is False
    with pytest.raises(RuntimeError):
        Wardrive(dict(config.DEFAULTS, ble=False, adsb=False), Gps(FIX), root=tmp_path, adapters=[], ble=NoBle(),
                 fleet=Fleet, clock=lambda: NOW, cap=FakeCap(lora.ABSENT)).start()


def test_a_cap_found_just_after_the_start_is_listened_to(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "UPKEEP_SECONDS", 0.01)
    cap = FakeCap(lora.CHECKING)
    wardrive = drive(tmp_path, cap)
    wardrive.start()
    assert wardrive.mesh is None
    cap.state = lora.FOUND
    wait_for(lambda: wardrive.mesh is not None)
    wardrive.stop()
    assert cap.the_radio.closed == 1


def test_on_waits_for_the_look_for_the_cap_before_it_listens(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "UPKEEP_SECONDS", 0.01)
    cap = FakeCap(lora.CHECKING)
    wardrive = drive(tmp_path, cap, lora=True)
    assert wardrive._mesh_wanted() is False
    wardrive.start()
    assert wardrive.mesh is None and cap.the_radio.opened == []
    cap.state = lora.FOUND
    wait_for(lambda: wardrive.mesh is not None)
    wardrive.stop()
    alone = Wardrive(dict(config.DEFAULTS, ble=False, adsb=False, lora=True), Gps(FIX), root=tmp_path / "alone",
                     adapters=[], ble=NoBle(), fleet=Fleet, clock=lambda: NOW, cap=FakeCap(lora.CHECKING))
    alone.start()
    assert alone.state == "running" and alone.mesh is None
    alone.stop()


def test_on_without_a_cap_says_why_and_tries_again(tmp_path):
    cap = FakeCap(lora.ABSENT, FakeRadio(error=lora.ABSENT))
    wardrive = drive(tmp_path, cap, lora=True)
    wardrive.start()
    wait_for(lambda: wardrive.status()["lora"]["state"] == mesh.FAILED)
    assert wardrive.status()["lora"]["detail"] == lora.WORDS[lora.ABSENT]
    wardrive.stop()
    assert wardrive.mesh_path is None


@needs_crypto
def test_the_nodes_are_saved_every_30_seconds_while_the_wardrive_runs(tmp_path, later):
    cap = FakeCap()
    wardrive = drive(tmp_path, cap)
    wardrive.start()
    listener = wardrive.mesh
    cap.the_radio.hear(meshtastic_packet(0x1A2B3C4D, 3, position(35.0, -80.0)))
    wait_for(lambda: listener.count == 1)
    assert not listener.journal.exists()
    later(mesh.JOURNAL_SECONDS + 1)
    wait_for(listener.journal.exists)
    saved = json.loads(listener.journal.read_text())
    assert saved["nodes"][0][1] == "1a2b3c4d"
    wardrive.stop()
    assert not listener.journal.exists() and mesh.load(wardrive.mesh_path)["nodes"][0]["reported"]["lat"] == 35.0
    assert wardrive.mesh_path.name == wigle.log_filename(NOW).replace(".wigle.csv", ".mesh.json.gz")


@needs_crypto
def test_mesh_nodes_heard_before_the_first_fix_are_not_kept(tmp_path):
    cap = FakeCap()
    radio = cap.the_radio
    wardrive = drive(tmp_path, cap, fix=NO_FIX)
    wardrive.start()
    radio.hear(meshtastic_packet(0x1A2B3C4D, 4, nodeinfo(0x1A2B3C4D, "Hilltop", "HTR", 2)))
    wait_for(lambda: not radio.packets)
    time.sleep(0.05)
    status = wardrive.status()
    assert status["waiting_for_fix"] and status["mesh"] == 0 and status["lora"]["packets"] == 0
    wardrive.gps.fix = FIX
    radio.hear(meshtastic_packet(0x1A2B3C4D, 5, nodeinfo(0x1A2B3C4D, "Hilltop", "HTR", 2)))
    wait_for(lambda: wardrive.status()["mesh"] == 1)
    assert not wardrive.status()["waiting_for_fix"]
    wardrive.stop()
    assert wardrive.mesh_path is not None


@needs_crypto
def test_a_wardrive_stopped_before_the_first_fix_leaves_no_mesh_file(tmp_path):
    cap = FakeCap()
    wardrive = drive(tmp_path, cap, fix=NO_FIX)
    wardrive.start()
    cap.the_radio.hear(meshtastic_packet(0x1A2B3C4D, 4, nodeinfo(0x1A2B3C4D, "Hilltop", "HTR", 2)))
    wait_for(lambda: not cap.the_radio.packets)
    wardrive.stop()
    assert wardrive.mesh_path is None and list((tmp_path / "logs").iterdir()) == []


def test_mesh_listening_costs_little(tmp_path):
    cap = FakeCap()
    listener = mesh.Listener(tmp_path, Gps(FIX), cap.radio, US, clock=lambda: NOW)
    header = struct.pack("<IIIBBBB", 0xFFFFFFFF, 0x89ABCDEF, 1, 0x63, 0x55, 0, 0) + bytes(40)
    started = time.process_time()
    listener.prepare(NOW)
    listener.radio = cap.the_radio
    for i in range(2000):
        listener._heard(header[:4] + struct.pack("<I", i + 1) + header[8:], -90, 2, True)
    assert time.process_time() - started < 2.0 and listener.count == 2000


def test_a_checkpoint_writes_the_mesh_journal_at_once_and_a_wardrive_thrown_away_leaves_none(tmp_path, monkeypatch):
    logs, settled, settle = folder(tmp_path), [], mesh.adsb.settle
    listener = mesh.Listener(logs, Gps(FIX), None, [])
    listener.prepare(NOW)
    monkeypatch.setattr(mesh.adsb, "settle", lambda where: settled.append(where) or settle(where))
    listener.checkpoint()
    assert not listener.journal.exists() and settled == []
    listener.mesh = heard_store()
    listener.checkpoint()
    assert len(json.loads(listener.journal.read_text())["nodes"]) == 3 and settled == [logs]
    assert listener.discard() is True
    assert listener.count == 0 and list(logs.iterdir()) == [] and listener.finish() is None
    assert list(logs.iterdir()) == []


def test_a_mesh_journal_that_cannot_be_removed_is_told_when_the_wardrive_is_thrown_away(tmp_path, monkeypatch):
    listener = mesh.Listener(folder(tmp_path), Gps(FIX), None, [])
    listener.prepare(NOW)
    listener.mesh = heard_store()
    listener.checkpoint()
    unlink = Path.unlink

    def refuse(path, missing_ok=False):
        if path == listener.journal:
            raise OSError(errno.EROFS, "Read-only file system")
        unlink(path, missing_ok=missing_ok)
    monkeypatch.setattr(Path, "unlink", refuse)
    assert listener.discard() is False and listener.journal.exists() and listener.count == 0
