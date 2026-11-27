import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from quartermaster import __main__, hardware, lora, updates_check
from tools import build_deb


@pytest.mark.parametrize("profile", ["st7735-128", "st7789-240"])
def test_pi_probe_uses_usb_gps_without_probing_the_hat(profile, monkeypatch, capsys):
    monkeypatch.setattr(__main__.os, "umask", lambda _: None)
    def ports(*, cardputer):
        assert not cardputer
        return [{"path": "/dev/ttyACM0", "label": "USB GPS"}]

    monkeypatch.setattr(hardware, "gps_candidates", ports)
    monkeypatch.setattr(hardware, "wifi_adapters", lambda: [])
    monkeypatch.setattr(hardware, "stalled_usb_adapters", lambda: [])
    monkeypatch.setattr(lora, "Cap", lambda: pytest.fail("Pi probe tried to open the Cap"))
    assert __main__.main(["--display", profile, "--probe"]) == 0
    assert capsys.readouterr().out.splitlines() == ["display  " + profile, "gps   /dev/ttyACM0  USB GPS"]


@pytest.mark.parametrize("autostart", [False, True])
@pytest.mark.parametrize("profile", ["st7735-128", "st7789-240"])
def test_handheld_entry_opens_selected_hat_and_closes_it_on_exit(profile, autostart, monkeypatch, tmp_path):
    from unittest.mock import Mock
    from quartermaster import config, engine
    from quartermaster.gnss import GPS
    from quartermaster.ui import app, hat

    panel, buttons = Mock(), Mock()
    original_dir = Path.cwd()
    opened_from = []

    def open_panel(selected):
        assert selected == profile
        assert "LG_WD" not in os.environ
        directory = Path.cwd()
        assert directory != original_dir and directory.stat().st_mode & 0o777 == 0o700
        os.mkfifo(directory / ".lgd-nfy0")
        opened_from.append(directory)
        return panel

    monkeypatch.setattr(hat, "HatDisplay", open_panel)
    monkeypatch.setenv("LG_WD", "old-gpio-directory")
    monkeypatch.setattr(hat, "HatInput", lambda: buttons)
    monkeypatch.setattr(__main__.os, "umask", lambda _: None)
    monkeypatch.setattr(__main__.signal, "signal", lambda *_: None)
    monkeypatch.setattr(engine, "DEFAULT_ROOT", tmp_path)
    monkeypatch.setattr(config, "load", lambda: {"gps_source": "/dev/ttyS0"})
    monkeypatch.setattr(GPS, "run", lambda self, stopped: stopped.wait(5))
    seen = []

    def create(display, keyboard, gps, cfg, *, root, splash, hw, cap, updates):
        assert display is panel and keyboard is buttons
        assert Path.cwd() == original_dir and opened_from[0].is_dir()
        assert gps.mode == "auto" and not gps.rails.paths and not cap.cardputer
        assert gps.candidates is hw._gps_ports and gps.candidates.keywords == {"cardputer": False}
        assert updates is updates_check.check_pi and not splash
        instance = Mock(logs_dir=tmp_path)
        seen.append(instance)
        return instance

    monkeypatch.setattr(app, "App", create)
    assert __main__.main(["--display", profile, "--no-splash", *(["--autostart"] if autostart else [])]) == 0
    if autostart:
        seen[0].start_wardrive.assert_called_once_with(ask=False)
    else:
        seen[0].start_wardrive.assert_not_called()
    assert len(opened_from) == 1 and not opened_from[0].exists() and Path.cwd() == original_dir
    assert os.environ["LG_WD"] == "old-gpio-directory"
    seen[0].run.assert_called_once_with()
    buttons.close.assert_called_once_with()
    panel.close.assert_called_once_with()


def test_pi_update_checks_only_releases_with_a_pi_artifact():
    def github(*releases):
        def fetch(url, _):
            assert url == updates_check.PI_RELEASES
            return json.dumps(releases).encode()
        return fetch
    def release(tag, asset, **extra):
        return dict(tag_name=tag, assets=[{"name": asset}], **extra)
    cardputer = release("v2.0.0", "quartermaster.deb")
    missing = release("pi-v2.0.0", "quartermaster-pi_1.9.0_all.deb")
    draft = release("pi-v3.0.0", "quartermaster-pi_3.0.0_all.deb", draft=True)
    pi = release("pi-v1.3.0", "quartermaster-pi_1.3.0_all.deb", prerelease=True)
    older = release("pi-v1.2.5", "quartermaster-pi_1.2.5_all.deb", prerelease=True)
    assert updates_check.check_pi("1.2.5", github(cardputer, missing, draft))[0] == updates_check.UNKNOWN
    assert updates_check.check_pi("1.2.5", github(cardputer, older, pi, missing))[:2] == (updates_check.GITHUB, "1.3.0")
    assert updates_check.check_pi("1.3.0", github(pi))[0] == updates_check.LATEST


@pytest.mark.skipif(not shutil.which("dpkg-deb"), reason="building a Debian package requires dpkg-deb")
def test_pi_installer_contains_both_profiles_and_an_unprivileged_launcher(tmp_path):
    deb = build_deb.build(tmp_path, "pi")
    meta = subprocess.check_output(["dpkg-deb", "-f", str(deb)], text=True)
    assert "Package: quartermaster-pi\n" in meta and "Conflicts: quartermaster\n" in meta
    assert "python3-luma.lcd" in meta and "python3-gpiozero" in meta
    stage = tmp_path / "unpack"
    subprocess.run(["dpkg-deb", "--raw-extract", str(deb), str(stage)], check=True)
    launcher = stage / "usr/bin/quartermaster"
    assert launcher.stat().st_mode & 0o111
    assert 'quartermaster --display "$display" "$@"' in launcher.read_text()
    assert "[ -e /etc/quartermaster/autostart ]" in launcher.read_text()
    text = launcher.read_text()
    assert subprocess.run(["sh", "-n", str(launcher)]).returncode == 0
    assert "--apply-settings /etc/quartermaster/settings" in text
    assert "--headless-wardrive --epaper 2in13-v4" in text and "--headless-wardrive --oled ssd1309" in text
    # Settings and the status screen apply only to the bare service start, not to --stop or --status.
    assert text.index('if [ "$#" -eq 0 ]') < text.index("--apply-settings") < text.index("--display")
    assert (stage / "etc/quartermaster/display").read_text().strip() == "st7735-128"
    assert (stage / "DEBIAN/conffiles").read_text().strip() == "/etc/quartermaster/display"
    assert (stage / "usr/lib/quartermaster/app/quartermaster/ui/hat.py").is_file()
    unit = (stage / "usr/lib/systemd/system/quartermaster@.service").read_text()
    assert "User=%i\n" in unit and "%I" not in unit and "ExecStart=/usr/bin/quartermaster\n" in unit
    doc = stage / "usr/share/doc/quartermaster-pi"
    guide, source = (doc / "diy.md").read_text(encoding="utf-8"), (build_deb.HERE / "docs/diy.md").read_text()
    assert build_deb.LINK.sub("]()", guide) == build_deb.LINK.sub("]()", source)
    version = json.loads((build_deb.HERE / "app-builder.json").read_text())["version"]
    on_github = "%s/blob/pi-v%s/" % (build_deb.HOMEPAGE, version)
    for target in build_deb.LINK.findall(guide) + re.findall(r'<img src="([^"]+)"', guide):
        if target.startswith(on_github):
            path = build_deb.HERE / target[len(on_github):].partition("#")[0]
            assert path.is_file() and path.suffix == ".md", target
        elif not target.startswith(("#", "https://")):
            assert (doc / target).is_file(), target
    assert on_github + "docs/guide/alerts.md#on-a-diy-raspberry-pi" in guide
    assert (doc / "screens/waveshare-preview.png").is_file()
    source, shipped = build_deb.HERE / "docs/screens/diy", stage / "usr/share/doc/quartermaster-pi/screens/diy"
    pictures = sorted(path.relative_to(source) for path in source.rglob("*.png"))
    assert {path.parent.name for path in pictures} == {"128", "240"}
    assert sorted(path.relative_to(shipped) for path in shipped.rglob("*") if path.is_file()) == pictures
    assert all((shipped / path).read_bytes() == (source / path).read_bytes() for path in pictures)
    assert not (stage / "usr/share/APPLaunch").exists()
    assert not (stage / "usr/lib/tmpfiles.d/quartermaster.conf").exists()
