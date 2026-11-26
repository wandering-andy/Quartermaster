import threading

from quartermaster import __main__ as cli, accounts, background, config, engine, faults, headless


class GPS:
    instance = None

    def __init__(self, source, rails, power, candidates=None):
        self.source, self.rails, self.power, self.candidates = source, rails, power, candidates
        self.started = self.stopped = False
        type(self).instance = self

    def run(self, stopped):
        self.started = True
        stopped.wait()
        self.stopped = True


class Wardrive:
    instance = None

    def __init__(self, cfg, gps, root, nodes, cap, display_profile="cardputer"):
        self.cfg, self.gps, self.root, self.nodes, self.cap = cfg, gps, root, nodes, cap
        self.display_profile = display_profile
        self.rows, self.log_paths, self.failed = 3, [], ""
        self.aircraft_path = self.mesh_path = None
        self.started = self.stopped = False
        type(self).instance = self

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True
        self.mesh_path = self.root / "logs" / "sample.mesh.json.gz"

    def status(self):
        return {"state": "running", "elapsed": 65, "wifi": 12, "ble": 4, "aircraft": None, "mesh": 2,
                "rows": 9, "sightings": 25, "skipped_no_fix": 3,
                "gps": {"valid": False, "status": "no_fix"},
                "adapters": [{"interface": "wlan0", "label": "Built-in", "sweeps": 5}],
                "lora": {"state": "listening", "packets": 7, "detail": "US 915"},
                "log": "/tmp/quartermaster/logs/drive.wigle.csv",
                "upload": {"keys": 2, "waiting": 1, "auto": True}}

    def active_log(self):
        return None


class Uploader:
    instance = None

    def __init__(self, root, cfg, active):
        self.root, self.cfg, self.active = root, cfg, active
        self.started = self.flushed = self.stopped = False
        type(self).instance = self

    def start(self):
        self.started = True

    def status(self):
        return {"keys": 2, "waiting": 1, "auto": True}

    def run_pass(self):
        self.flushed = True
        assert Wardrive.instance.stopped

    def stop(self):
        self.stopped = True


class Hat:
    instance = None

    def __init__(self):
        self.started = self.found = False
        type(self).instance = self

    def start(self, wanted=True):
        self.started, self.found = True, wanted

    def radio(self):
        raise AssertionError("the fake HAT does not access hardware")


def test_headless_runner_uses_gps_and_wardrive_without_cardputer_hardware(tmp_path, monkeypatch):
    original_fault_path = faults.path
    monkeypatch.setattr(faults, "path", original_fault_path)
    stop = threading.Event()
    stop.set()
    output = []
    ready = []

    def on_started():
        assert Wardrive.instance.started and Uploader.instance.started
        ready.append(True)

    assert headless.run(stop, tmp_path, cfg={"gps_source": "auto", "auto_upload": True},
                        gps_factory=GPS, drive_factory=Wardrive,
                        hat_factory=Hat, uploader_factory=Uploader, output=output.append,
                        on_started=on_started) == 0

    assert faults.path == tmp_path / "errors.log"
    assert GPS.instance.rails is None and not GPS.instance.power
    assert GPS.instance.started and GPS.instance.stopped
    assert Hat.instance.started and Hat.instance.found
    assert Wardrive.instance.cap is Hat.instance and Wardrive.instance.nodes is None
    assert Wardrive.instance.display_profile == "headless"
    assert Wardrive.instance.started and Wardrive.instance.stopped
    assert Uploader.instance.active == Wardrive.instance.active_log
    assert Uploader.instance.started and Uploader.instance.flushed and Uploader.instance.stopped
    assert ready == [True]
    assert output == ["Wardrive stopped: 3 Wi-Fi/Bluetooth rows logged.",
                      "Saved %s" % (tmp_path / "logs" / "sample.mesh.json.gz")]


def test_dashboard_shows_totals_gps_wait_state_radio_and_hat_status():
    text = headless.dashboard(Wardrive(None, None, None, None, None).status(), "present")

    assert "Wi-Fi             12" in text
    assert "Bluetooth          4" in text
    assert "Log rows           9" in text
    assert "STALE FIX" not in text
    assert "NO FIX | WiGLE rows wait for a fresh GPS fix" in text
    assert "Built-in: wlan0 | 5 sweeps" in text
    assert "LoRa: LISTENING | 7 packets | US 915" in text
    assert "2 keys | 1 pending | automatic on" in text
    assert "/tmp/quartermaster/logs/drive.wigle.csv" in text


def test_default_cli_starts_headless_wardrive(monkeypatch):
    called = []

    def start(root):
        called.append(root)
        return 0

    monkeypatch.setattr(background, "start", start)
    assert cli.main([]) == 0
    assert len(called) == 1


def test_probe_includes_ble_and_adsb_diagnostics(monkeypatch, capsys):
    from quartermaster import adsb, ble, hardware, lora

    monkeypatch.setattr(hardware, "wifi_adapters", lambda: [])
    monkeypatch.setattr(hardware, "stalled_usb_adapters", lambda: [])
    monkeypatch.setattr(hardware, "gps_candidates", lambda **kwargs: [])
    monkeypatch.setattr(hardware, "rtl_sdr_dongles", lambda: [{"usb_id": "0bda:2838"}])
    monkeypatch.setattr(adsb, "find_decoder", lambda: "/usr/bin/rtl_adsb")
    monkeypatch.setattr(ble, "probe", lambda: "adapter /org/bluez/hci0")

    class HatProbe:
        found = True

        def start(self):
            pass

    monkeypatch.setattr(lora, "WaveshareHat", HatProbe)
    assert cli.main(["--probe"]) == 0
    output = capsys.readouterr().out
    assert "adsb  1 RTL-SDR device(s); decoder /usr/bin/rtl_adsb" in output
    assert "ble   adapter /org/bluez/hci0" in output
    assert "lora  Waveshare SX1262 HAT: SPI device present" in output


def test_dashboard_cli_runs_foreground(monkeypatch):
    called = []

    def run(stopped):
        called.append(stopped)
        return 0

    monkeypatch.setattr(background, "acquire_foreground", lambda root: 19)
    monkeypatch.setattr(background, "release_foreground", lambda root, fd: called.append(fd))
    monkeypatch.setattr(headless, "run", run)
    assert cli.main(["--dashboard"]) == 0
    assert len(called) == 2 and called[1] == 19


def test_startup_error_is_reported_and_gps_worker_stops(tmp_path):
    class BrokenWardrive(Wardrive):
        def start(self):
            raise RuntimeError("Storage is almost full")

    stop = threading.Event()
    output = []

    assert headless.run(stop, tmp_path, cfg={}, gps_factory=GPS, drive_factory=BrokenWardrive,
                        hat_factory=Hat, output=output.append) == 1

    assert stop.is_set() and GPS.instance.stopped
    assert output == ["Cannot start wardrive: Storage is almost full"]


def test_new_config_disables_auto_upload_but_preserves_explicit_choice(tmp_path):
    path = tmp_path / "config.json"
    assert config.load(path)["auto_upload"] is True
    path.write_text('{"auto_upload": true}', encoding="utf-8")
    assert config.load(path)["auto_upload"] is True


def test_cli_sets_only_validated_settings_and_redacts_cluster_key(monkeypatch, tmp_path, capsys):
    path = tmp_path / "config.json"
    monkeypatch.setattr(config, "PATH", path)
    assert cli.set_config(config, [["adsb", "off"], ["auto_upload", "false"]]) == 0
    saved = config.load(path)
    assert saved["adsb"] is False and saved["auto_upload"] is False
    assert "Updated" in capsys.readouterr().out

    previous = path.read_text()
    assert cli.set_config(config, [["adsb", "maybe"]]) == 2
    assert path.read_text() == previous

    saved["cluster_key"] = "a" * 32
    config.save(saved, path)
    assert cli.show_config(config) == 0
    shown = capsys.readouterr().out
    assert "[configured]" in shown and "a" * 32 not in shown


def test_manual_upload_without_keys_is_actionable(monkeypatch, capsys):
    monkeypatch.setattr(accounts, "load", lambda: {field: "" for field, _, _ in accounts.FIELDS})
    assert cli.upload_finished("unused") == 2
    assert "quartermaster --configure-uploads" in capsys.readouterr().out


def test_upload_and_export_refuse_to_run_during_collection(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(engine, "DEFAULT_ROOT", tmp_path)
    fd = background.acquire_foreground(tmp_path)
    try:
        assert cli.main(["--upload"]) == 1
        assert "Stop the wardrive before uploading" in capsys.readouterr().out
        assert cli.main(["--export", str(tmp_path / "usb")]) == 1
        assert "Stop the wardrive before exporting" in capsys.readouterr().out
    finally:
        background.release_foreground(tmp_path, fd)


def test_data_operation_holds_lock_until_completion(tmp_path):
    def operation():
        assert background.is_running(tmp_path)
        return 7

    assert background.run_if_idle(tmp_path, operation, "busy", lambda message: None) == 7
    assert not background.is_running(tmp_path)


def test_data_operation_fails_closed_when_lock_cannot_be_opened(monkeypatch, tmp_path):
    output = []
    attempted = []

    def denied(*args, **kwargs):
        raise PermissionError("denied")

    monkeypatch.setattr(background.os, "open", denied)
    assert background.run_if_idle(tmp_path, lambda: attempted.append(True), "busy", output.append) == 1
    assert not attempted
    assert output and output[0].startswith("Cannot safely access the Quartermaster lock:")


def test_manual_upload_runs_one_pass_with_saved_keys(monkeypatch, capsys):
    monkeypatch.setattr(accounts, "load", lambda: {"wigle_name": "name", "wigle_token": "token",
                                                    "wdgwars_key": "", "wardrift_key": ""})
    monkeypatch.setattr(accounts, "ready", lambda saved: ["wigle"])

    class OneShotUploader:
        def __init__(self, root, cfg):
            self.passes = 0

        def run_pass(self):
            self.passes += 1
            return False

        def status(self):
            return {"files": 1, "waiting": 0, "services": {"wigle": {"name": "WiGLE", "last": "WiGLE has it"}}}

    import quartermaster.uploads as upload_module
    monkeypatch.setattr(upload_module, "Uploader", OneShotUploader)
    assert cli.upload_finished("unused") == 0
    output = capsys.readouterr().out
    assert "Checked 1 finished log file(s); 0 upload(s) remain pending." in output
    assert "WiGLE: WiGLE has it" in output


def test_upload_status_shows_queue_without_starting_uploader(monkeypatch, capsys):
    class StatusOnlyUploader:
        def __init__(self, root, cfg):
            pass

        def status(self):
            return {"files": 2, "waiting": 1, "auto": True, "services": {
                "wigle": {"name": "WiGLE", "configured": True, "waiting": 1, "sent": 0, "refused": 0},
                "wdgwars": {"name": "WDGWars", "configured": False, "waiting": 0, "sent": 0, "refused": 0},
                "wardrift": {"name": "Wardrift", "configured": False, "waiting": 0, "sent": 0,
                             "refused": 0}}}

    monkeypatch.setattr("quartermaster.uploads.Uploader", StatusOnlyUploader)
    assert cli.upload_status("unused") == 0
    output = capsys.readouterr().out
    assert "2 finished file(s), 1 pending upload(s)" in output
    assert "WiGLE: 1 pending" in output and "WDGWars: no key configured" in output


def test_default_headless_shutdown_does_not_attempt_upload(tmp_path):
    stop = threading.Event()
    stop.set()
    headless.run(stop, tmp_path, cfg={"auto_upload": False}, gps_factory=GPS, drive_factory=Wardrive,
                 hat_factory=Hat, uploader_factory=Uploader, output=lambda message: None)
    assert Uploader.instance.started and not Uploader.instance.flushed and Uploader.instance.stopped


def test_configure_uploads_keeps_blank_fields_and_does_not_echo_secrets(tmp_path, monkeypatch, capsys):
    accounts_path = tmp_path / "accounts.json"
    accounts.save({"wdgwars_key": "old-secret"}, accounts_path)
    monkeypatch.setattr(accounts, "PATH", accounts_path)
    answers = iter(("", "", "new-secret", ""))
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    monkeypatch.setattr("getpass.getpass", lambda prompt: next(answers))

    assert cli.configure_uploads() == 0
    assert accounts.load(accounts_path)["wdgwars_key"] == "new-secret"
    assert "new-secret" not in capsys.readouterr().out