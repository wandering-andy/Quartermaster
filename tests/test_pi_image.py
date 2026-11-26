import ast
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

import pytest

from tools import imager_os_list, pi_release_notes

ROOT = Path(__file__).resolve().parents[1]
IMAGE = ROOT / "packaging" / "image"
SCRIPT = ROOT / "tools" / "build_image.sh"
BOOT = ROOT / "tools" / "boot_image.sh"
WORKFLOWS = ROOT / ".github" / "workflows"
SHA = "ab" * 32


def load_image_module(name):
    spec = importlib.util.spec_from_file_location(name, IMAGE / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


boot_config = load_image_module("boot_config")
first_login = load_image_module("first_login")
SERVICE_USER = re.search(r"^service_user=(\S+)$", SCRIPT.read_text(), re.M).group(1)


def settings(text):
    found = {}
    for line in text.splitlines():
        if "=" in line and not line.startswith("#"):
            key, _, value = line.partition("=")
            found.setdefault(key.strip(), []).append(value.strip())
    return found


def test_the_boot_file_knows_exactly_the_hat_profiles_the_app_accepts():
    source = (ROOT / "quartermaster" / "__main__.py").read_text()
    listed = re.search(r'"--display", choices=\(([^)]*)\)', source).group(1)
    profiles = tuple(name.strip().strip('"') for name in listed.split(","))
    assert ("cardputer",) + boot_config.PROFILES == profiles
    assert set(boot_config.ALIASES.values()) == set(boot_config.PROFILES)


@pytest.mark.parametrize("text, profile", [
    ("display=st7735-128\n", "st7735-128"),
    ("# display=st7735-128\ndisplay = ST7789-240  # the big one\n", "st7789-240"),
    ("display=st7735-128\r\ndisplay=\"st7789-240\"\r\n", "st7789-240"),
    ("Display=1.44\n", "st7735-128"),
    ("display='1.3in'\n", "st7789-240"),
    ("# nothing chosen\nbrightness=3\n", None),
    ("display=st7735-128\nst7789-240\n", "st7789-240"),
    ("  ST7735-128  \n", "st7735-128"),
    ("display: st7789-240\n", "st7789-240"),
    ("#   1.3 inch HAT (240x240):   st7789-240\ndisplay=st7735-128\n", "st7735-128"),
    ("1.3 inch HAT (240x240):   st7789-240\n", "st7789-240"),
    ("display=st7789-240\nmy HAT is the \"st7735-128\"\n", "st7735-128"),
    ("brightness=128\nsize: 240\nst7789-240 is not my HAT\n", None),
    ("240\n", "st7789-240"),
    ("", None),
])
def test_the_boot_file_parser_takes_the_last_display_line(text, profile):
    assert boot_config.parse(text) == profile


@pytest.mark.parametrize("text", ["display=st7735\n", "display=\n", "display=cardputer\n", "display: big\n",
                                  "display=st7789-240\ndisplay=hdmi\n"])
def test_the_boot_file_parser_refuses_an_unknown_display(text):
    with pytest.raises(ValueError, match="unknown display"):
        boot_config.parse(text)


def test_applying_the_boot_file_changes_the_display_only_for_a_valid_choice(tmp_path):
    boot, display = tmp_path / "quartermaster.txt", tmp_path / "display"
    display.write_text("st7735-128\n")
    assert "no " in boot_config.apply(str(boot), str(display))
    boot.write_bytes(b"\xef\xbb\xbfdisplay=st7789-240\r\n")
    assert boot_config.apply(str(boot), str(display)) == "display changed from st7735-128 to st7789-240"
    assert display.read_text() == "st7789-240\n" and display.stat().st_mode & 0o777 == 0o644
    assert boot_config.apply(str(boot), str(display)) == "display is st7789-240"
    boot.write_text("display=st7735-129\n")
    assert "keeping the current display" in boot_config.apply(str(boot), str(display))
    boot.write_text("# edited by hand\n")
    assert "names no display" in boot_config.apply(str(boot), str(display))
    assert display.read_text() == "st7789-240\n"
    display.unlink()
    boot.write_text("display=1.44\n")
    assert boot_config.apply(str(boot), str(display)) == "display changed from none to st7735-128"
    assert not list(tmp_path.glob("*.new"))


def test_autostart_is_off_until_the_boot_file_asks_for_it(tmp_path):
    boot, marker = tmp_path / "quartermaster.txt", tmp_path / "autostart"
    assert "no " in boot_config.apply_autostart(str(boot), str(marker)) and not marker.exists()
    boot.write_text("display=st7789-240\n")
    assert "stays off" in boot_config.apply_autostart(str(boot), str(marker)) and not marker.exists()
    boot.write_text("display=st7789-240\nAutostart = Wardrive  # at boot\n")
    assert boot_config.apply_autostart(str(boot), str(marker)) == "autostart is on"
    assert marker.read_text() == "wardrive\n" and marker.stat().st_mode & 0o777 == 0o644
    boot.write_text("autostart=maybe\n")
    assert "keeping the current autostart" in boot_config.apply_autostart(str(boot), str(marker))
    assert marker.exists()
    boot.write_text("display=st7789-240\n")
    assert "keeping autostart on" in boot_config.apply_autostart(str(boot), str(marker))
    boot.write_text("autostart=off\n")
    assert boot_config.apply_autostart(str(boot), str(marker)) == "autostart is off" and not marker.exists()
    assert boot_config.apply_autostart(str(boot), str(marker)) == "autostart is off"


def test_an_autostart_line_never_changes_the_display_choice():
    assert boot_config.parse("autostart=wardrive\ndisplay=st7735-128\n") == "st7735-128"
    assert boot_config.parse("autostart=wardrive\n") is None


@pytest.mark.parametrize("profile", boot_config.IMAGE_PROFILES)
def test_the_boot_partition_template_selects_its_hat(profile):
    template = (IMAGE / "quartermaster.txt").read_text()
    assert template.count("@HAT@") == 1
    text = template.replace("@HAT@", profile)
    assert boot_config.parse(text) == profile
    for other in boot_config.IMAGE_PROFILES:
        assert other in text
    lines = text.splitlines()
    assert lines[-1] == "display=" + profile and all(line.startswith("#") for line in lines[:-1])
    assert "display=" not in "".join(lines[:-1]).replace("after display= on the last line", "")


def test_the_boot_config_unit_runs_before_the_app_and_points_at_the_installed_script():
    unit = settings((IMAGE / "quartermaster-boot-config.service").read_text())
    script = SCRIPT.read_text()
    assert unit["Type"] == ["oneshot"] and unit["WantedBy"] == ["multi-user.target"]
    assert unit["Before"] == ["quartermaster@%s.service" % SERVICE_USER]
    assert unit["ConditionPathExists"] == [boot_config.BOOT_FILE]
    installed = unit["ExecStart"][0].split()[-1]
    assert unit["ExecStart"] == ["/usr/bin/python3 -I " + installed]
    assert '"$mnt%s"' % installed in script
    assert '"$mnt/etc/systemd/system/quartermaster-boot-config.service"' in script


def test_the_polkit_rule_only_lets_the_service_user_request_wifi_scans():
    rule = (IMAGE / "50-quartermaster-wifi-scan.rules").read_text()
    assert re.findall(r'action\.id == "([^"]+)"', rule) == ["org.freedesktop.NetworkManager.wifi.scan"]
    assert re.findall(r'subject\.user == "([^"]+)"', rule) == [SERVICE_USER]
    assert rule.count("polkit.Result.YES") == 1 and "indexOf" not in rule and "isInGroup" not in rule


def test_the_image_script_is_strict_cleans_up_and_keeps_imager_customisation_working():
    script = SCRIPT.read_text()
    assert script.startswith("#!/usr/bin/env bash\n") and "\nset -euo pipefail\n" in script
    assert SCRIPT.stat().st_mode & 0o111
    assert "trap cleanup EXIT" in script and "losetup -d" in script and "umount" in script
    assert "sha256sum --check --strict" in script and "https://downloads.raspberrypi.com/" in script
    assert "raspios-trixie-armhf-lite" in script
    assert "dtparam=spi=on" in script and "--no-install-recommends install /tmp/quartermaster-pi.deb" in script
    assert "useradd --system" not in script and "--shell /usr/sbin/nologin" in script
    assert 'useradd --uid "$service_id"' in script and "radiod.peer_allowed" in script
    for unit in ("quartermaster-radio.socket", "quartermaster-boot-config.service",
                 '"quartermaster@$service_user.service"'):
        assert unit in re.search(r"^units=\(([^)]*)\)", script, re.M).group(1)
    groups = re.search(r"^groups_wanted=\(([^)]*)\)", script, re.M).group(1).split()
    postinst = (ROOT / "packaging" / "postinst").read_text()
    postinst_groups = re.search(r"for grp in ([^;]*); do", postinst).group(1).split()
    assert set(postinst_groups) <= set(groups) and "quartermaster-usbreset" not in groups
    for forbidden in ("chpasswd", "passwd -d", "boot/firmware/ssh", "PermitRootLogin", "sudoers", "ssh-keygen -A",
                      "cloud-init clean", "systemctl mask", "cloud-init.disabled"):
        assert forbidden not in script, forbidden
    assert not re.search(r"(>|sed -i[^\n]*)\s*\"?\$mnt/boot/firmware/(cmdline\.txt|user-data|network-config)", script)
    assert 'check "cmdline.txt and cloud-init seed" "$(seeds)" "$seed_before"' in script
    fetch = re.search(r"fetch=\((curl [^)]*)\)", script).group(1).split()
    assert {"-fsSL", "--connect-timeout", "--speed-limit", "--speed-time", "--retry"} <= set(fetch)
    assert script.count('"${fetch[@]}"') == 3 and script.count("curl -") == 1


@pytest.mark.parametrize("name", ["first_user", "probe"])
def test_a_lookup_that_fails_in_the_base_image_reaches_the_message_written_for_it(name):
    line, = [line for line in SCRIPT.read_text().splitlines() if line.startswith(name + "=$(")]
    run = subprocess.run(["bash", "-c", "set -euo pipefail\nin_chroot() { return 2; }\nlogin_id=1000\n%s\n"
                          'echo "reached [$%s]"' % (line, name)], capture_output=True, text=True)
    assert (run.returncode, run.stdout) == (0, "reached []\n"), run


def shell_list(script, name):
    return re.search(r"^%s=\(([^)]*)\)" % name, script, re.M).group(1).split()


def test_the_image_logs_in_as_pi_with_the_default_password_and_keeps_the_other_accounts_locked():
    script = SCRIPT.read_text()
    assert re.search(r"^login_user=pi login_password=pi login_id=1000 host_name=quartermaster$", script, re.M)
    groups = shell_list(script, "login_groups")
    assert {"sudo", "adm", "gpio", "spi", "netdev", '"$service_user"'} <= set(groups)
    assert first_login.DEFAULT_USER == "pi"
    assert 'login_hash=$(openssl passwd -6 "$login_password")' in script
    assert 'in_chroot usermod --password "$login_hash" "$login_user"' in script
    assert 'in_chroot usermod --shell /bin/bash "$login_user"' in script
    assert 'die "the first user of the base image is $first_user, not $login_user"' in script
    assert """awk -F: '$2 !~ /^[!*]/ {print $1}' "$mnt/etc/shadow" | xargs)" "$login_user\"""" in script
    assert 'openssl passwd -6 -salt "$salt" "$login_password")" = "$stored"' in script
    for label in ('"root password locked"', '"$service_user password locked"', '"$service_user shell"',
                  '"$login_user in $group"', '"$login_user uid and shell"', '"default login record mode"',
                  '"first start keeps the default login"'):
        assert "check " + label in script, label
    assert "print(first_login.plan(*first_login.gather()))')\" \"[]\"" in script
    assert "umask 077 && printf" in script and first_login.HASH_FILE in script.replace('$mnt', "")


@pytest.mark.skipif(not shutil.which("openssl"), reason="needs openssl")
def test_the_password_check_accepts_the_right_password_only():
    stored = subprocess.run(["openssl", "passwd", "-6", "pi"], capture_output=True, text=True, check=True).stdout.strip()
    salt = subprocess.run(["cut", "-d$", "-f3"], input=stored, capture_output=True, text=True, check=True).stdout.strip()

    def again(word):
        return subprocess.run(["openssl", "passwd", "-6", "-salt", salt, word], capture_output=True, text=True,
                              check=True).stdout.strip()

    assert "salt=$(cut -d'$' -f3 <<< \"$stored\")" in SCRIPT.read_text()
    assert stored.startswith("$6$") and again("pi") == stored and again("raspberry") != stored


def test_the_image_has_ssh_on_and_never_ships_host_keys():
    script = SCRIPT.read_text()
    assert "in_chroot systemctl enable ssh.service avahi-daemon.service getty@tty1.service" in script
    assert 'rm -f "$mnt"/etc/ssh/ssh_host_*' in script
    clean, checks = script.index('rm -f "$mnt"/etc/ssh/ssh_host_*'), script.index('log "Checking the image"')
    assert script.index("full-upgrade") < clean < checks
    for label in ('"ssh.service" "$(enabled ssh.service)" enabled', '"ssh host keys in the image"',
                  '"host keys are made on first start" "$(enabled regenerate_ssh_host_keys.service)" enabled',
                  '"first start marker" "$(cat "$mnt/etc/machine-id")" uninitialized', '"ssh password login"',
                  '"machine-id as in the base image"', '"ssh.socket off"'):
        assert "check " + label in script, label
    assert 'rm -f "$mnt"/tmp/probe_key*' in script and "etc/machine-id" in script
    keys = settings((IMAGE / "quartermaster-ssh-keys.service").read_text())
    assert keys["ConditionPathExistsGlob"] == ["!/etc/ssh/ssh_host_*_key"] and keys["Before"] == ["ssh.service"]
    assert keys["ExecStart"] == ["/usr/bin/ssh-keygen -A"] and keys["After"] == ["regenerate_ssh_host_keys.service"]
    assert "quartermaster-ssh-keys.service" in shell_list(script, "units")


def test_the_first_start_never_waits_for_a_user_prompt_but_still_takes_userconf_txt():
    script = SCRIPT.read_text()
    condition = settings((IMAGE / "userconfig-needs-userconf.conf").read_text())
    assert condition == {"ConditionPathExists": ["|/boot/firmware/userconf.txt", "|/boot/firmware/userconf",
                                                 "|" + first_login.BANNER]}
    assert 'grep -c \'^ConditionPathExists=|/boot/firmware/userconf\' ' in script
    assert '"$mnt/etc/systemd/system/userconfig.service.d/quartermaster.conf"' in script
    assert 'rm -f "$mnt/etc/ssh/sshd_config.d/rename_user.conf"' in script
    assert 'check "getty@tty1.service" "$(enabled getty@tty1.service)" enabled' in script
    assert "systemctl disable userconfig" not in script and "cancel-rename" not in script
    assert '"$mnt/etc/cloud/cloud.cfg.d/99_zz_quartermaster.cfg"' in script
    assert 'check "our cloud-init settings are read last" "$last_cfg" 99_zz_quartermaster.cfg' in script
    assert sorted(["99_raspberry-pi.cfg", "99_zz_quartermaster.cfg", "05_logging.cfg"])[-1] == "99_zz_quartermaster.cfg"
    for label in ('"cloud-init keeps SSH password logins"', '"the cloud-init seed sets no hostname"',
                  '"start order was examined"', '"start order has no cycle"'):
        assert "check " + label in script, label
    assert "systemd-analyze verify --man=no cloud-init.target default.target" in script
    cloud = (IMAGE / "99_zz_quartermaster.cfg").read_text()
    yaml = pytest.importorskip("yaml")
    assert yaml.safe_load(cloud) == {"users": []}


def test_the_first_login_unit_runs_once_after_cloud_init_made_its_users_and_before_ssh():
    unit = settings((IMAGE / "quartermaster-first-login.service").read_text())
    assert unit["ConditionPathExists"] == ["!" + first_login.STAMP]
    assert unit["After"] == ["cloud-init-network.service cloud-init.service"]
    assert unit["Before"] == ["ssh.service systemd-user-sessions.service userconfig.service"]
    assert unit["Type"] == ["oneshot"] and unit["WantedBy"] == ["multi-user.target"]
    installed = unit["ExecStart"][0].split()[-1]
    assert unit["ExecStart"] == ["/usr/bin/python3 -I " + installed]
    script = SCRIPT.read_text()
    assert '"$mnt%s"' % installed in script and "quartermaster-first-login.service" in shell_list(script, "units")
    service_id = int(re.search(r"^service_id=(\d+)$", script, re.M).group(1))
    assert (first_login.FIRST_UID, first_login.SERVICE_UID) == (1000, service_id)
    assert first_login.DATA == re.search(r"^data=(\S+)$", script, re.M).group(1)
    assert first_login.BANNER == "/etc/ssh/sshd_config.d/rename_user.conf"


@pytest.mark.parametrize("text, wanted", [
    ("", None),
    ("#cloud-config\n# only comments\n", None),
    ("hostname: \"qm\"\nmanage_etc_hosts: true\n", None),
    ("users:\n- default\n", None),
    ("user:\n  name: \"kal\"\n  shell: /bin/bash\n  lock_passwd: false\n  passwd: \"$6$abc$def\"\n", ("kal", "$6$abc$def")),
    ("user:\n  name: pi\n  lock_passwd: true\n  ssh_authorized_keys:\n    - \"ssh-ed25519 AAAA\"\n", ("pi", None)),
    ("user: kal\n", ("kal", None)),
    ("users:\n- default\n- name: robot\n  hashed_passwd: $6$r$s\n", ("robot", "$6$r$s")),
    ("users:\n- name: [1]\n", None),
    ("users:\n  kal:\n    lock_passwd: false\n    ssh_authorized_keys: [\"ssh-ed25519 AAAA\"]\n", ("kal", None)),
    ("users:\n  default: true\n  robot: {passwd: $6$r$s}\n", ("robot", "$6$r$s")),
    ("users:\n  off: false\n  bare: {}\n", ("bare", None)),
    ("users: pi\n", ("pi", None)),
    ("users:\n  pi:\n    shell: /bin/bash\n", ("pi", None)),
    ("users: \"default, kal\"\n", ("kal", None)),
    ("users:\n- default,kal\n", ("kal", None)),
    ("users:\n- name: default\n  lock_passwd: false\n", None),
    ("users:\n  1: true\n", None),
    ("user: [unclosed\n", None),
    ("- just\n- a list\n", None),
])
def test_the_first_login_reads_the_user_imager_asked_for(text, wanted):
    pytest.importorskip("yaml")
    assert first_login.requested(text) == wanted


PI = {"pi": (1000, "/home/pi", "/bin/bash"), "root": (0, "/root", "/bin/bash"),
      SERVICE_USER: (59999, "/var/lib/quartermaster", "/usr/sbin/nologin"),
      "nobody": (65534, "/nonexistent", "/usr/sbin/nologin")}
DEFAULT, OWN = "$6$salt$default", "$6$other$own"
LOCKED = {"root": "*", SERVICE_USER: "!"}


def login_plan(users, hashes, wanted, members=("pi",), links=("/home/pi/quartermaster",), banner=False):
    return first_login.plan(users, dict(LOCKED, **hashes), set(members), DEFAULT, wanted, set(links), banner)


def test_the_default_login_stays_when_imager_set_no_user():
    assert login_plan(PI, {"pi": DEFAULT}, None) == []
    assert login_plan(PI, {"pi": "!" + DEFAULT}, None) == [["usermod", "-U", "pi"]]
    assert login_plan(PI, {"pi": OWN}, None) == []
    assert login_plan(PI, {"pi": "!" + OWN}, None) == []


def test_a_user_from_imager_switches_the_default_login_off():
    users = dict(PI, kal=(1001, "/home/kal", "/bin/bash"))
    steps = login_plan(users, {"pi": DEFAULT, "kal": OWN}, ("kal", OWN))
    assert steps == [["usermod", "-L", "pi"], ["usermod", "-aG", SERVICE_USER, "kal"],
                     ["ln", "-s", first_login.DATA, "/home/kal/quartermaster"],
                     ["chown", "-h", "kal:", "/home/kal/quartermaster"]]
    assert login_plan(users, {"pi": DEFAULT, "kal": "!"}, ("kal", None))[0] == ["usermod", "-L", "pi"]
    assert login_plan(users, {"pi": "!" + DEFAULT, "kal": OWN}, ("kal", OWN), members=("pi", "kal"),
                      links=("/home/pi/quartermaster", "/home/kal/quartermaster")) == []
    assert ["usermod", "-L", "pi"] in login_plan(users, {"pi": DEFAULT, "kal": OWN}, None)
    assert ["usermod", "-L", "pi"] not in login_plan(users, {"pi": OWN, "kal": OWN}, ("kal", OWN))


def test_a_renamed_first_user_keeps_its_group_and_link_and_gets_the_chosen_password():
    users = {name: entry for name, entry in PI.items() if name != "pi"}
    users["kal"] = (1000, "/home/kal", "/bin/bash")
    assert login_plan(users, {"kal": OWN}, ("kal", OWN), members=("kal",), links=("/home/kal/quartermaster",)) == []
    assert login_plan(users, {"kal": DEFAULT}, ("kal", OWN), members=("kal",),
                      links=("/home/kal/quartermaster",)) == [["usermod", "-p", OWN, "kal"]]


def test_a_new_password_for_pi_from_imager_replaces_the_default():
    assert login_plan(PI, {"pi": DEFAULT}, ("pi", OWN)) == [["usermod", "-p", OWN, "pi"]]
    assert login_plan(PI, {"pi": "!" + DEFAULT}, ("pi", OWN)) == [["usermod", "-p", OWN, "pi"]]
    assert login_plan(PI, {"pi": OWN}, ("pi", OWN)) == []
    assert login_plan(PI, {"pi": "!" + DEFAULT}, ("pi", None)) == []
    assert login_plan(PI, {"pi": DEFAULT}, ("kal", OWN)) == []


def test_a_login_asked_for_without_a_password_never_keeps_the_default_one():
    assert login_plan(PI, {"pi": DEFAULT}, ("pi", None)) == [["usermod", "-L", "pi"]]
    users = {name: entry for name, entry in PI.items() if name != "pi"}
    users["kal"] = (1000, "/home/kal", "/bin/bash")
    renamed = dict(members=("kal",), links=("/home/kal/quartermaster",))
    assert login_plan(users, {"kal": DEFAULT}, ("kal", None), **renamed) == [["usermod", "-L", "kal"]]
    assert login_plan(users, {"kal": "!" + DEFAULT}, ("kal", None), **renamed) == []
    assert login_plan(users, {"kal": "!" + DEFAULT}, None, **renamed) == []
    assert login_plan(users, {"kal": DEFAULT}, ("kal", DEFAULT), **renamed) == [["usermod", "-L", "kal"]]
    assert login_plan(users, {"kal": DEFAULT}, None, **renamed) == [["usermod", "-L", "kal"]]
    assert login_plan(users, {"kal": DEFAULT}, ("pi", OWN), **renamed) == [["usermod", "-L", "kal"]]
    assert login_plan(PI, {"pi": "!" + DEFAULT}, ("pi", None)) == []


def test_pi_set_up_in_imager_gets_its_group_and_link_back():
    assert login_plan(PI, {"pi": OWN}, ("pi", OWN), members=()) == [["usermod", "-aG", SERVICE_USER, "pi"]]
    assert login_plan(PI, {"pi": DEFAULT}, ("pi", OWN), members=(), links=()) == [
        ["usermod", "-p", OWN, "pi"], ["usermod", "-aG", SERVICE_USER, "pi"],
        ["ln", "-s", first_login.DATA, "/home/pi/quartermaster"], ["chown", "-h", "pi:", "/home/pi/quartermaster"]]


def test_a_login_without_a_home_folder_gets_no_link(tmp_path, monkeypatch):
    home = tmp_path / "home" / "pi"
    home.mkdir(parents=True)
    files = {"/etc/passwd": "pi:x:1000:1000::%s:/bin/bash\nkal:x:1001:1001::%s:/bin/bash\n" % (home, tmp_path / "gone"),
             "/etc/shadow": "pi:%s:1::::::\nkal:!:1::::::\n" % OWN,
             "/etc/group": "%s:x:59999:pi,kal\n" % SERVICE_USER}
    monkeypatch.setattr(first_login, "read", lambda path: files.get(path, ""))
    monkeypatch.setattr(first_login, "BANNER", str(tmp_path / "banner"))
    assert first_login.plan(*first_login.gather()) == [
        ["ln", "-s", first_login.DATA, "%s/quartermaster" % home], ["chown", "-h", "pi:", "%s/quartermaster" % home]]


def test_the_first_login_ignores_system_accounts_and_tidies_the_rename_banner():
    users = dict(PI, wizard=(999, "/home/w", "/bin/bash"), ghost=(1001, "/home/ghost", "/usr/sbin/nologin"))
    assert login_plan(users, {"pi": DEFAULT}, None) == []
    assert login_plan(PI, {"pi": DEFAULT}, None, banner=True) == [
        ["rm", "-f", first_login.BANNER], ["systemctl", "enable", "getty@tty1.service"],
        ["systemctl", "--no-block", "start", "getty@tty1.service"]]
    assert first_login.plan(PI, dict(LOCKED, pi=""), {"pi"}, "", None, {"/home/pi/quartermaster"}) == []


def test_the_first_login_marks_itself_done_only_when_every_step_worked(tmp_path, monkeypatch):
    stamp = tmp_path / "state" / "done"
    monkeypatch.setattr(first_login, "STAMP", str(stamp))
    monkeypatch.setattr(first_login, "gather", lambda: (PI, dict(LOCKED, pi="!" + DEFAULT), {"pi"}, DEFAULT, None,
                                                         {"/home/pi/quartermaster"}, False))
    ran = []
    monkeypatch.setattr(first_login.subprocess, "run",
                        lambda step, check: ran.append(step) or type("r", (), {"returncode": 1})())
    assert first_login.main() == 1 and ran == [["usermod", "-U", "pi"]] and not stamp.exists()
    monkeypatch.setattr(first_login.subprocess, "run", lambda step, check: type("r", (), {"returncode": 0})())
    assert first_login.main() == 0 and stamp.read_text() == "done\n"


def test_a_password_hash_is_not_printed(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(first_login, "STAMP", str(tmp_path / "done"))
    monkeypatch.setattr(first_login, "gather", lambda: (PI, dict(LOCKED, pi=DEFAULT), {"pi"}, DEFAULT, ("pi", OWN),
                                                         {"/home/pi/quartermaster"}, False))
    monkeypatch.setattr(first_login.subprocess, "run", lambda step, check: type("r", (), {"returncode": 0})())
    assert first_login.main() == 0
    assert capsys.readouterr().out == "usermod -p <hash> pi\n"


NOTICE = IMAGE / "default_password_notice.sh"


def notice(tmp_path, shadow, record=DEFAULT + "\n"):
    (tmp_path / "shadow").write_text(shadow)
    paths = [str(tmp_path / "shadow"), str(tmp_path / "record")]
    if record is not None:
        (tmp_path / "record").write_text(record)
    result = subprocess.run(["sh", str(NOTICE), *paths], capture_output=True, text=True)
    assert result.returncode == 0 and result.stderr == ""
    return result.stdout


@pytest.mark.skipif(not shutil.which("awk"), reason="needs awk")
def test_the_login_notice_names_only_logins_that_still_have_the_default_password(tmp_path):
    rest = ":20000:0:99999:7:::\n"
    system = "root:*" + rest + SERVICE_USER + ":!" + rest
    assert notice(tmp_path, system + "pi:" + DEFAULT + rest) == \
        "\nThe login pi still has the default password. Type passwd to set your own.\n\n"
    assert notice(tmp_path, system + "pi:!" + DEFAULT + rest) == ""
    assert notice(tmp_path, system + "pi:" + OWN + rest) == ""
    assert "The login pi kal still has" in notice(tmp_path, "pi:" + DEFAULT + rest + "kal:" + DEFAULT + rest)
    assert notice(tmp_path, "pi:" + DEFAULT + rest, record="") == ""
    assert notice(tmp_path, "pi:" + rest, record="\n") == ""
    (tmp_path / "record").unlink()
    assert notice(tmp_path, "pi:" + DEFAULT + rest, record=None) == ""


def test_the_login_notice_is_installed_where_every_login_shows_it():
    script = SCRIPT.read_text()
    assert NOTICE.read_text().startswith("#!/bin/sh\n") and first_login.HASH_FILE in NOTICE.read_text()
    assert ('install -D -m 755 "$here/packaging/image/default_password_notice.sh" '
            '"$mnt/etc/update-motd.d/20-quartermaster-password"') in script
    assert re.fullmatch(r"[a-zA-Z0-9_-]+", "20-quartermaster-password")
    assert 'check "default password notice" "$(in_chroot /etc/update-motd.d/20-quartermaster-password | xargs)"' in script
    assert '"The login $login_user still has the default password. Type passwd to set your own."' in script
    assert 'check "the notice is shown at login"' in script and "pam_motd" in script


def test_pi_reaches_the_logs_through_the_group_and_a_link_in_its_home():
    script = SCRIPT.read_text()
    folders = shell_list(script, "log_folders")
    assert folders == ["logs", "recon", "aircraft"] and shell_list(BOOT.read_text(), "log_folders") == folders
    assert 'install -d -m 750 -o "$service_id" -g "$service_id" "$mnt$data"\n' in script
    assert 'install -d -m 750 -o "$service_id" -g "$service_id" "$mnt$data/$folder"' in script
    assert 'ln -sfn "$data" "$mnt$login_home/quartermaster"' in script
    assert 'check "logs link"' in script and 'check "$login_user reads $folder"' in script
    path = settings((IMAGE / "quartermaster-logs.path").read_text())
    assert path["PathChanged"] == [first_login.DATA + "/" + folder for folder in folders]
    assert path["WantedBy"] == ["multi-user.target"]
    service = settings((IMAGE / "quartermaster-logs.service").read_text())
    assert service["User"] == [SERVICE_USER] and service["Type"] == ["oneshot"]
    command = service["ExecStart"][0].split()
    assert command[:1 + len(folders)] == ["-/usr/bin/find"] + path["PathChanged"]
    assert command[-6:] == "-exec chmod -f g+rX {} +".split()
    assert 'check "$login_user reads a newly saved log in $folder"' in script


def test_every_folder_of_the_readme_logs_table_opens_to_the_login_on_the_image():
    readme = (ROOT / "README.md").read_text()
    table = readme[readme.index("## Logs\n"):]
    table = table[:table.index("\n\n", table.index("| File |"))]
    folders = set(re.findall(r"^\|\s*`([^`/\s]+)/", table, re.M))
    assert folders == set(shell_list(SCRIPT.read_text(), "log_folders"))


def test_a_log_saved_while_nothing_watched_or_in_a_burst_still_opens_to_the_group():
    service = settings((IMAGE / "quartermaster-logs.service").read_text())
    first, pause, second = service["ExecStart"]
    assert first == second and first.startswith("-/usr/bin/find ") and "-ignore_readdir_race" in first.split()
    assert pause == "/usr/bin/sleep 2"
    assert service["StartLimitIntervalSec"] == ["0"] and "WantedBy" not in service
    path = settings((IMAGE / "quartermaster-logs.path").read_text())
    assert path["Wants"] == ["quartermaster-logs.service"] and path["WantedBy"] == ["multi-user.target"]
    script = SCRIPT.read_text()
    assert "quartermaster-logs.path" in shell_list(script, "units") and "quartermaster-logs.service" in script


def test_the_folder_is_watched_again_before_the_logs_rule_takes_its_last_look():
    path = settings((IMAGE / "quartermaster-logs.path").read_text())
    assert path["Unit"] == ["quartermaster-logs-changed.service"]
    changed = settings((IMAGE / "quartermaster-logs-changed.service").read_text())
    assert changed["Type"] == ["oneshot"] and changed["StartLimitIntervalSec"] == ["0"]
    assert changed["ExecStart"] == ["/usr/bin/sleep 0.2",
                                    "/usr/bin/systemctl --no-block restart quartermaster-logs.service"]
    assert "User" not in changed and "WantedBy" not in changed and "RemainAfterExit" not in changed
    service = settings((IMAGE / "quartermaster-logs.service").read_text())
    pause = float(service["ExecStart"][1].split()[-1])
    assert pause >= 5 * float(changed["ExecStart"][0].split()[-1])
    assert changed["LogLevelMax"] == service["LogLevelMax"] == ["notice"]
    script = SCRIPT.read_text()
    assert "quartermaster-logs-changed.service quartermaster-logs.service; do" in script
    assert "quartermaster-logs-changed.service" not in shell_list(script, "units")


def test_a_restart_of_the_logs_rule_during_its_pause_is_a_clean_stop_and_the_first_start_test_proves_it():
    service = settings((IMAGE / "quartermaster-logs.service").read_text())
    changed = settings((IMAGE / "quartermaster-logs-changed.service").read_text())
    assert changed["ExecStart"][-1].split()[-2:] == ["restart", "quartermaster-logs.service"]
    assert service["Type"] == ["oneshot"] and service["SuccessExitStatus"] == ["SIGTERM"]
    script = re.sub(r"\s*\\\n\s*", " ", BOOT.read_text())
    wait = re.search(r'\n    sleep (\d+)\n    check "\$when: saving a log leaves no warning in the journal" '
                     r'"\$\(as_root "journalctl -b -q -p warning -o cat --no-pager -u quartermaster-logs\.service '
                     r'-u quartermaster-logs-changed\.service && echo none"\)" none\n', script)
    assert wait and script.index("a newly saved log in $folder opens to the login") < wait.start()
    chain = float(changed["ExecStart"][0].split()[-1]) + float(service["ExecStart"][1].split()[-1])
    assert int(wait.group(1)) >= 2 * chain


def test_the_image_check_finds_every_program_the_units_start():
    script = SCRIPT.read_text()
    pattern = re.search(r"done < <\(sed -n '([^']+)' ", script).group(1)
    assert 'in_chroot test -x "$program" < /dev/null || missing+=" $program"' in script
    units = sorted(IMAGE.glob("quartermaster*.service")) + sorted((ROOT / "packaging").glob("quartermaster*.service"))
    assert len(units) == 7
    found = subprocess.run(["sed", "-n", pattern, *map(str, units)], capture_output=True, text=True, check=True)
    assert set(found.stdout.split()) == {"/usr/bin/python3", "/usr/bin/quartermaster", "/usr/bin/find",
                                         "/usr/bin/sleep", "/usr/bin/ssh-keygen", "/usr/bin/systemctl"}


def test_the_image_check_loads_every_screen_and_button_module_the_hat_code_imports():
    imports = set()
    for node in ast.walk(ast.parse((ROOT / "quartermaster" / "ui" / "hat.py").read_text())):
        if isinstance(node, ast.Import):
            imports |= {"import " + alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            imports.add("from %s import %s" % (node.module, ", ".join(alias.name for alias in node.names)))
    outside = {line for line in imports if line.split()[1].split(".")[0] not in sys.stdlib_module_names}
    assert "import RPi.GPIO" in outside and len(outside) > 1
    check = re.search(r'check "the screen and button modules load as \$service_user" "\$\(in_chroot runuser -u '
                      r'"\$service_user" -- python3 -c \'\n(.*?)\' 2>&1 \| tail -n 1\)" ok\n', SCRIPT.read_text(),
                      re.S)
    lines = check.group(1).splitlines()
    assert outside - {"import RPi.GPIO"} <= set(lines)
    assert 'print("ok" if importlib.util.find_spec("RPi.GPIO") else "RPi.GPIO is missing")' in lines


@pytest.mark.skipif(not shutil.which("find"), reason="needs find")
def test_the_logs_rule_opens_new_logs_to_the_group_and_then_changes_nothing(tmp_path):
    service = settings((IMAGE / "quartermaster-logs.service").read_text())
    parts = service["ExecStart"][0].lstrip("-").split()
    folders = {part: tmp_path / part.rsplit("/", 1)[1] for part in parts if part.startswith(first_login.DATA + "/")}
    assert len(folders) == 3
    for folder in folders.values():
        (folder / "sub").mkdir(parents=True)
        for name in ("a.log", "sub/b.json"):
            (folder / name).write_text("x")
            (folder / name).chmod(0o600)
        (folder / "sub").chmod(0o700)
        folder.chmod(0o700)
    command = [str(folders.get(part, part)) for part in parts]
    subprocess.run(command, check=True)
    for folder in folders.values():
        assert [(folder / name).stat().st_mode & 0o777 for name in (".", "sub", "a.log", "sub/b.json")] == \
            [0o750, 0o750, 0o640, 0o640]
    changed = subprocess.run(command[:-6] + ["-print"], capture_output=True, text=True, check=True).stdout
    assert changed == ""


def test_the_image_is_named_quartermaster_and_answers_to_quartermaster_local():
    script = SCRIPT.read_text()
    assert """printf '%s\\n' "$host_name" > "$mnt/etc/hostname\"""" in script
    assert "avahi-daemon" in script.split("--no-install-recommends install /tmp/quartermaster-pi.deb")[1].split("\n")[0]
    for label in ('"hostname" "$(cat "$mnt/etc/hostname")" "$host_name"', '"hostname in hosts"',
                  '"avahi-daemon.service" "$(enabled avahi-daemon.service)" enabled'):
        assert "check " + label in script, label


def test_the_image_only_turns_off_units_a_handheld_does_not_need():
    script = SCRIPT.read_text()
    unneeded, kept = shell_list(script, "unneeded"), shell_list(script, "kept")
    assert set(unneeded) == {"ModemManager.service", "triggerhappy.service", "triggerhappy.socket", "man-db.timer",
                             "apt-daily.timer", "apt-daily-upgrade.timer", "e2scrub_all.timer", "e2scrub_reap.service"}
    assert {"NetworkManager.service", "bluetooth.service", "systemd-timesyncd.service", "rpi-resize.service",
            "regenerate_ssh_host_keys.service", "cloud-init-local.service", "cloud-init-network.service",
            "cloud-config.service", "cloud-final.service"} <= set(kept)
    assert not set(unneeded) & set(kept)
    for word in ("NetworkManager", "bluetooth", "timesyncd", "cloud", "resize", "ssh", "avahi", "getty", "quartermaster"):
        assert not [unit for unit in unneeded if word in unit], word
    assert re.findall(r"systemctl disable (\S+)", script) == ['"$unit"']
    assert "printf 'disable %s\\n' \"${unneeded[@]}\" > \"$mnt/etc/systemd/system-preset/50-quartermaster.preset\"" in script
    assert 'check "$unit still on" "$(enabled "$unit")" enabled' in script
    assert 'check "root file system grows on first start" "$(enabled rpi-resize.service)" enabled' in script
    assert "overlay" not in script and "governor" not in script


@pytest.mark.skipif(not shutil.which("bash"), reason="needs bash")
def test_the_image_script_parses_and_refuses_bad_arguments():
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)
    for args in ([], ["--hat", "hdmi", "--deb", "x.deb"], ["--wat"]):
        result = subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True)
        assert result.returncode == 2 and "--hat st7735-128" in result.stderr
    if shutil.which("shellcheck"):
        subprocess.run(["shellcheck", str(SCRIPT)], check=True)


def test_the_first_start_test_checks_the_names_the_image_really_uses():
    script, build = BOOT.read_text(), SCRIPT.read_text()
    assert script.startswith("#!/usr/bin/env bash\n") and "\nset -euo pipefail\n" in script
    assert BOOT.stat().st_mode & 0o111
    assert "trap cleanup EXIT" in script and "losetup -d" in script and "umount" in script
    assert re.search(r"^service_user=(\S+)$", script, re.M).group(1) == SERVICE_USER
    assert re.search(r"^data=(\S+)$", script, re.M).group(1) == first_login.DATA
    login = re.search(r"^login_user=(\S+) login_password=(\S+) login_id=\d+ host_name=(\S+)$", build, re.M)
    assert "\nuser=%s password=%s name=%s\n" % login.groups() in script
    assert first_login.STAMP in script
    notice = "still has the default password"
    assert notice in (IMAGE / "default_password_notice.sh").read_text() and "grep -c '%s'" % notice in script
    for unit in ("quartermaster-logs.path", "quartermaster-first-login.service", "quartermaster-boot-config.service",
                 "quartermaster-logs.service"):
        assert unit in script and (IMAGE / unit).is_file(), unit
    assert 'is-enabled "quartermaster@$service_user.service"' in script
    for name in ("kernel8.img", "initramfs8", "bcm2710-rpi-3-b.dtb", "cmdline.txt"):
        assert name in script
    assert "-machine raspi3b" in script and "-no-reboot" in script
    assert 'hostfwd=tcp:127.0.0.1:$port-:22' in script and "0.0.0.0" not in script
    assert script.count("\ntake_boot_files\n") == 2 and "partscan" not in script
    assert 'mcopy -n -i "$disk@@$(partition 1)" "::$file" "$work/$file"' in script
    assert (IMAGE / "quartermaster.txt").read_text().splitlines()[-1] == "display=@HAT@"
    assert "::quartermaster.txt" in script and "sed -n 's/^display=//p'" in script
    assert '"Cannot open %s display or controls: %s"' in (ROOT / "quartermaster" / "__main__.py").read_text()
    assert 'grep -c "^Cannot open $hat display or controls: "' in script
    assert "grep -q 'Permission denied'" in script and "<<< 'exit 0'" in script
    assert script.count('"$(refused ') == 3 and "&& echo open || echo refused" not in script


def test_a_failed_first_start_test_ends_with_its_failed_checks_and_never_asks_for_a_password():
    script = BOOT.read_text()
    assert 'failures+=("$1: expected \'$3\', got \'$2\'")' in script
    assert 'printf \'FAIL  %s\\n\' "${failures[@]}"' in script
    halt = re.search(r"^halted\(\) \{ grep -aqE '([^']+)' ", script, re.M).group(1)
    assert "sed -nE '1,/%s/p' \"$console\"" % halt in script
    assert '[ "$starts" -gt 0 ] && [ "$booted" = no ] && [ -f "$disk" ] && show_card_journal' in script
    assert not re.search(r"\bsudo (?!-n |-S -p '' )", script.replace("command -v sudo >", ""))


@pytest.mark.skipif(not shutil.which("bash"), reason="needs bash")
def test_the_first_start_test_parses_and_refuses_bad_arguments(tmp_path):
    subprocess.run(["bash", "-n", str(BOOT)], check=True)
    for args in (["--wat"], ["--image"], ["--image", str(BOOT), "--wait"]):
        result = subprocess.run(["bash", str(BOOT), *args], capture_output=True, text=True)
        assert result.returncode == 2 and "--image build/images/" in result.stderr, args
    for args, message in (([], "no image at ''"), (["--image", str(tmp_path / "none.img.xz")], "no image at"),
                          (["--image", str(BOOT), "--port", "ssh"], "whole numbers"),
                          (["--image", str(BOOT), "--wait", "1.5"], "whole numbers")):
        result = subprocess.run(["bash", str(BOOT), *args], capture_output=True, text=True)
        assert result.returncode == 1 and message in result.stderr, args
    assert not list(tmp_path.iterdir())
    if shutil.which("shellcheck"):
        subprocess.run(["shellcheck", str(BOOT)], check=True)


def test_the_service_account_is_one_the_radio_helper_accepts_and_leaves_room_for_login_users():
    script = SCRIPT.read_text()
    service_id = int(re.search(r"^service_id=(\d+)$", script, re.M).group(1))
    spec = importlib.util.spec_from_file_location("radiod_for_image", ROOT / "packaging" / "radiod.py")
    radiod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(radiod)
    import grp
    try:
        target = grp.getgrnam(radiod.GROUP).gr_gid
    except KeyError:
        target = 4242
        radiod.grp = type("grp", (), {"getgrnam": staticmethod(lambda name: type("g", (), {"gr_gid": target}))})
    assert radiod.peer_allowed(service_id, service_id, members=[service_id, target])
    assert not radiod.peer_allowed(999, 999, members=[999, target])
    assert service_id >= 1000 and service_id + 1 >= 60000
    assert "[UG]ID_MAX" in script and "LAST_[UG]ID" in script and "$((service_id - 1))" in script


def test_the_app_account_has_a_name_no_login_is_given_by_accident():
    script = SCRIPT.read_text()
    assert SERVICE_USER == first_login.GROUP == "_quartermaster"
    assert SERVICE_USER.startswith("_") and re.fullmatch(r"[a-z_][a-z0-9_]*", SERVICE_USER)
    assert not re.fullmatch(r"[a-z][a-z0-9-]*", SERVICE_USER)
    assert 'groupadd --gid "$service_id" "$service_user"' in script
    assert 'useradd --uid "$service_id" --gid "$service_user" --home-dir /var/lib/quartermaster' in script
    assert 'for taken in "$service_user" quartermaster; do' in script
    for label in ('"$service_user uid and group"', '"the name quartermaster is free for a login"',
                  '"the logs rule runs as $service_user"', '"the Wi-Fi scan rule is for $service_user"',
                  '"the boot file is read before the app starts"', '"radio helper accepts $service_user"',
                  '"systemd accepts the Quartermaster units"'):
        assert "check " + label in script, label
    assert not re.search(r"(useradd|groupadd|getent (passwd|group)|runuser -u|id -\w+|shadow_of) quartermaster\b",
                         script.replace('in "$service_user" quartermaster; do', "").replace(
                             "in_chroot getent passwd quartermaster; in_chroot getent group quartermaster", ""))
    unit = (ROOT / "packaging" / "quartermaster@.service").read_text()
    assert "User=%i\n" in unit and "%I" not in unit


@pytest.mark.skipif(not shutil.which("systemd-analyze"), reason="needs systemd-analyze")
@pytest.mark.parametrize("user", ["_quartermaster", "pi", "kal-pi", "my_user"])
def test_systemd_takes_the_app_unit_for_any_user_name(tmp_path, user):
    units = tmp_path / "etc" / "systemd" / "system"
    units.mkdir(parents=True)
    shutil.copy(ROOT / "packaging" / "quartermaster@.service", units)
    result = subprocess.run(["systemd-analyze", "verify", "--man=no", "--root=%s" % tmp_path,
                             "quartermaster@%s.service" % user], capture_output=True, text=True)
    report = result.stdout + result.stderr
    if "nitialize manager" in report:
        pytest.skip("systemd-analyze cannot run here")
    assert "Invalid user" not in report and "fatal error" not in report, report


def test_the_image_unblocks_wifi_that_raspberry_pi_os_blocks_until_a_country_is_set():
    script = SCRIPT.read_text()
    assert '"$mnt"/var/lib/systemd/rfkill/*:wlan' in script and 'echo 0 > "$state"' in script
    assert "Wi-Fi rfkill saved state" in script


def test_the_radios_start_switched_on_and_the_image_sets_no_country():
    script = SCRIPT.read_text()
    rule = [line for line in (IMAGE / "radios-on.conf").read_text().splitlines() if not line.startswith("#")]
    assert rule == ["options rfkill default_state=1"]
    installed = re.search(r'radios-on\.conf" "\$mnt/etc/modprobe\.d/([^"]+)"', script).group(1)
    assert sorted(["rfkill_default.conf", installed])[-1] == installed and installed.endswith(".conf")
    assert "--show-depends rfkill" in script and "grep -o 'default_state=[0-9]*' | tail -n 1)\" default_state=1" in script
    assert "sed -i 's/^WirelessEnabled=.*/WirelessEnabled=true/'" in script
    for label in ('"radios start switched on with $kernel"', '"a kernel was checked for the radio switch"',
                  '"NetworkManager has Wi-Fi switched on"', '"the image sets no Wi-Fi country"'):
        assert "check " + label in script, label
    for forbidden in ("iw reg set", "do_wifi_country \"", "ieee80211_regdom=$", "REGDOMAIN="):
        assert forbidden not in script, forbidden
    assert "Wi-Fi and Bluetooth start switched on. No Wi-Fi country is set" in script


COUNTRY_NOTICE = IMAGE / "wifi_country_notice.sh"


def country_notice(*rules):
    result = subprocess.run(["sh", str(COUNTRY_NOTICE), *rules], capture_output=True, text=True)
    assert result.returncode == 0 and result.stderr == ""
    return result.stdout


def test_the_login_hint_for_the_wifi_country_shows_only_while_none_is_set():
    world = "global\ncountry 00: DFS-UNSET\n\t(2402 - 2472 @ 40), (N/A, 20), (N/A)\n\nphy#0\ncountry US: DFS-FCC\n"
    shown = country_notice(world)
    assert "No Wi-Fi country is set" in shown and shown.endswith("  sudo raspi-config nonint do_wifi_country US\n\n")
    assert shown.startswith("\n") and len(shown.splitlines()) == 6
    assert country_notice("global\ncountry US: DFS-FCC\n\nphy#0\ncountry 00: DFS-UNSET\n") == ""
    assert country_notice("country DE: DFS-ETSI\n") == ""
    assert country_notice("") == "" and country_notice("nl80211 not found.\n") == ""


def test_the_wifi_country_hint_is_installed_next_to_the_password_notice():
    script = SCRIPT.read_text()
    assert COUNTRY_NOTICE.stat().st_mode & 0o111 and COUNTRY_NOTICE.read_text().startswith("#!/bin/sh\n")
    assert ('install -D -m 755 "$here/packaging/image/wifi_country_notice.sh" '
            '"$mnt/etc/update-motd.d/21-quartermaster-wifi-country"') in script
    assert re.fullmatch(r"[a-zA-Z0-9_-]+", "21-quartermaster-wifi-country")
    for label in ('"Wi-Fi country hint without a country"', '"Wi-Fi country hint with a country"',
                  '"raspi-config sets the Wi-Fi country"'):
        assert "check " + label in script, label
    assert "iw reg get" in COUNTRY_NOTICE.read_text() and "iw reg set" not in COUNTRY_NOTICE.read_text()


REPLACE = ROOT / "tools" / "replace_release_assets.sh"
FAKE_GH = r"""#!/usr/bin/env bash
set -euo pipefail
store=$FAKE_RELEASE
echo "$*" >> "$store/calls"
case "$1 $2" in
    "api repos/o/r/releases/tags/pi-v1.2.8") echo 7 ;;
    "release upload")
        [ -z "${FAIL_ON:-}" ] || [[ $4 != *"$FAIL_ON"* ]] || exit 1
        name=$(basename "$4"); id=$(($(cat "$store/next_id") + 1)); echo "$id" > "$store/next_id"
        cp -L "$4" "$store/assets/$id"; echo "$name" > "$store/assets/$id.name" ;;
    "api --paginate")
        python3 -I -c 'import json, pathlib, sys
d = pathlib.Path(sys.argv[1])
print(json.dumps([{"id": int(f.stem), "name": f.read_text().strip()} for f in sorted(d.glob("*.name"))]))' \
            "$store/assets" > "$store/listing"
        want=$(sed -n 's/.*select(.name == "\(.*\)").*/\1/p' <<< "$5")
        python3 -I -c 'import json, sys
print("\n".join(str(a["id"]) for a in json.load(open(sys.argv[1])) if a["name"] == sys.argv[2]))' "$store/listing" "$want" ;;
    "api -X")
        id=${4##*/}
        if [ "$3" = DELETE ]; then rm "$store/assets/$id" "$store/assets/$id.name"
        else echo "${6#name=}" > "$store/assets/$id.name"; fi ;;
    *) echo "unexpected gh $*" >&2; exit 3 ;;
esac
"""


def fake_release(tmp_path, files):
    store = tmp_path / "release"
    (store / "assets").mkdir(parents=True)
    (store / "next_id").write_text("0")
    for number, (name, body) in enumerate(files.items(), 1):
        (store / "assets" / str(number)).write_text(body)
        (store / "assets" / ("%d.name" % number)).write_text(name)
    (store / "next_id").write_text(str(len(files)))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "gh").write_text(FAKE_GH)
    (bin_dir / "gh").chmod(0o755)
    return store, bin_dir


def assets(store):
    return {f.read_text().strip(): (store / "assets" / f.stem).read_text() for f in (store / "assets").glob("*.name")}


@pytest.mark.skipif(not shutil.which("bash"), reason="needs bash")
@pytest.mark.parametrize("fail_on", ["", "quartermaster-imager.json"])
def test_release_assets_are_only_swapped_after_every_upload_succeeded(tmp_path, fail_on):
    import os
    store, bin_dir = fake_release(tmp_path, {"a.img.xz": "old a", "quartermaster-imager.json": "old list",
                                             "SHA256SUMS": "keep"})
    new = tmp_path / "new"
    new.mkdir()
    (new / "a.img.xz").write_text("new a")
    (new / "quartermaster-imager.json").write_text("new list")
    env = dict(os.environ, PATH="%s:%s" % (bin_dir, os.environ["PATH"]), GH_REPO="o/r", FAKE_RELEASE=str(store),
               FAIL_ON=fail_on)
    result = subprocess.run(["bash", str(REPLACE), "pi-v1.2.8", str(new / "a.img.xz"),
                             str(new / "quartermaster-imager.json")], env=env, capture_output=True, text=True)
    found = assets(store)
    if fail_on:
        assert result.returncode != 0
        assert found["a.img.xz"] == "old a" and found["quartermaster-imager.json"] == "old list"
    else:
        assert result.returncode == 0, result.stderr
        assert found == {"a.img.xz": "new a", "quartermaster-imager.json": "new list", "SHA256SUMS": "keep"}
        calls = (store / "calls").read_text().splitlines()
        uploads = [i for i, call in enumerate(calls) if call.startswith("release upload")]
        deletes = [i for i, call in enumerate(calls) if call.startswith("api -X DELETE")]
        renames = [i for i, call in enumerate(calls) if call.startswith("api -X PATCH")]
        assert len(uploads) == 2 and len(deletes) == 2 and len(renames) == 2
        assert max(uploads) < min(deletes)
        assert calls[renames[-1]].endswith("name=quartermaster-imager.json --silent")


def meta(hat, version="1.2.8", **extra):
    if hat in imager_os_list.HATS:
        name = imager_os_list.image_name(hat, version)
    else:
        name = "quartermaster-pi-%s-%s" % (version, hat)
    found = dict(name=name, file=name + ".img.xz", version=version, hat=hat, init_format="cloudinit-rpi",
                 extract_size=4 << 30, extract_sha256=SHA, image_download_size=700 << 20,
                 image_download_sha256="cd" * 32, release_date="2026-11-26", base_image="raspios.img.xz")
    found.update(extra)
    return found


def test_the_imager_list_points_at_the_release_assets():
    listing = imager_os_list.os_list([meta("st7789-240"), meta("st7735-128")], "owner/repo", "pi-v1.2.8")
    assert list(listing) == ["os_list"]
    first, second = listing["os_list"]
    assert first["url"] == ("https://github.com/owner/repo/releases/download/pi-v1.2.8/"
                            "quartermaster-pi-1.2.8-waveshare-1.44inch-lcd-hat.img.xz")
    assert second["url"] == ("https://github.com/owner/repo/releases/download/pi-v1.2.8/"
                             "quartermaster-pi-1.2.8-waveshare-1.3inch-lcd-hat.img.xz")
    assert first["name"] == "Quartermaster 1.2.8 for the Waveshare 1.44inch LCD HAT (128\u00d7128)"
    assert second["name"] == "Quartermaster 1.2.8 for the Waveshare 1.3inch LCD HAT (240\u00d7240)"
    assert "ST7735S" in first["description"] and "ST7789" in second["description"]
    for item in (first, second):
        assert set(item) >= {"name", "description", "icon", "url", "extract_size", "extract_sha256",
                             "image_download_size", "image_download_sha256", "release_date", "init_format"}
        assert item["init_format"] == "cloudinit-rpi" and item["image_download_size"] < 2 << 30
        assert item["icon"].endswith("/pi-v1.2.8/packaging/icon.png")
        assert set(item["devices"]) == {"pi4-32bit", "pi3-32bit", "pi1-32bit"}
    assert (ROOT / "packaging" / "icon.png").is_file()


@pytest.mark.parametrize("bad, message", [
    (meta("st7735-128", version="1.2.7"), "not pi-v1.2.8"),
    (meta("hdmi"), "unknown HAT"),
    (meta("st7735-128", extract_sha256="xyz"), "extract_sha256"),
    (meta("st7735-128", image_download_size=0), "image_download_size"),
    (meta("st7735-128", file="quartermaster-pi-1.2.8-st7735-128.img.xz"), "not named after the Waveshare 1.44inch"),
    (meta("st7789-240", file=meta("st7735-128")["file"]), "not named after the Waveshare 1.3inch"),
])
def test_the_imager_list_refuses_metadata_that_does_not_match(bad, message):
    with pytest.raises(ValueError, match=message):
        imager_os_list.os_list([bad], "owner/repo", "pi-v1.2.8")


def test_the_imager_list_refuses_duplicates_and_writes_json(tmp_path, capsys):
    with pytest.raises(ValueError, match="twice"):
        imager_os_list.os_list([meta("st7735-128"), meta("st7735-128")], "o/r", "pi-v1.2.8")
    with pytest.raises(ValueError, match="no image"):
        imager_os_list.os_list([], "o/r", "pi-v1.2.8")
    paths = []
    for hat in boot_config.IMAGE_PROFILES:
        paths.append(tmp_path / (hat + ".json"))
        paths[-1].write_text(json.dumps(meta(hat)))
    assert imager_os_list.main(["--repo", "o/r", "--tag", "pi-v1.2.8", *map(str, paths)]) == 0
    assert len(json.loads(capsys.readouterr().out)["os_list"]) == 2
    assert imager_os_list.main(["--repo", "o/r", "--tag", "pi-v1.2.8", "--manifest"]) == 0
    assert json.loads(capsys.readouterr().out) == imager_os_list.manifest("o/r", "pi-v1.2.8")
    for wrong in ([], ["--manifest", str(paths[0])]):
        with pytest.raises(SystemExit):
            imager_os_list.main(["--repo", "o/r", "--tag", "pi-v1.2.8", *wrong])
    capsys.readouterr()


def test_each_image_is_named_after_its_waveshare_hat(tmp_path, capsys):
    names = [imager_os_list.image_name(hat, "1.2.9") for hat in boot_config.IMAGE_PROFILES]
    assert names == ["quartermaster-pi-1.2.9-waveshare-1.44inch-lcd-hat", "quartermaster-pi-1.2.9-waveshare-1.3inch-lcd-hat"]
    for hat, version in (("hdmi", "1.2.9"), ("st7735-128", ""), ("st7735-128", "1.2.9 x"), ("st7735-128", "../1")):
        with pytest.raises(ValueError):
            imager_os_list.image_name(hat, version)
    assert imager_os_list.main(["--image-name", "st7789-240", "1.2.9"]) == 0
    assert capsys.readouterr().out == names[1] + "\n"
    for wrong in (["--image-name", "hdmi", "1.2.9"], ["--image-name", "st7735-128", "1.2.9", "--manifest"],
                  ["--image-name", "st7735-128", "1.2.9", "--repo", "o/r", "--tag", "pi-v1.2.9"], ["--manifest"]):
        with pytest.raises(SystemExit):
            imager_os_list.main(wrong)
    capsys.readouterr()
    script = SCRIPT.read_text()
    assert 'name=$(python3 "$here/tools/imager_os_list.py" --image-name "$hat" "$version")' in script
    assert "quartermaster-pi-$version" not in script
    yaml = pytest.importorskip("yaml")
    steps = yaml.safe_load((WORKFLOWS / "pi-images.yml").read_text())["jobs"]["image"]["steps"]
    named = next(step["run"] for step in steps if step.get("name") == "Name the package and the image")
    (tmp_path / "tools").symlink_to(ROOT / "tools")
    (tmp_path / "build" / "pi").mkdir(parents=True)
    (tmp_path / "build" / "pi" / "quartermaster-pi_9.9.9_all.deb").write_bytes(b"")
    for hat in (*boot_config.IMAGE_PROFILES, "hdmi"):
        found = tmp_path / ("env-" + hat)
        result = subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", named], cwd=tmp_path,
                                env={"PATH": os.environ["PATH"], "HAT": hat, "VERSION": "9.9.9", "GITHUB_ENV": str(found)},
                                capture_output=True, text=True)
        written = found.read_text().splitlines() if found.exists() else []
        if hat in imager_os_list.HATS:
            assert result.returncode == 0, result.stderr
            assert written == ["DEB=build/pi/quartermaster-pi_9.9.9_all.deb",
                               "IMAGE=%s.img.xz" % imager_os_list.image_name(hat, "9.9.9")]
        else:
            assert result.returncode != 0 and not any(line.startswith("IMAGE=") for line in written)


def imager_shows(manifest, fetched, device):
    """What Raspberry Pi Imager lists for a chosen Pi model: it puts each fetched list in place of the entry that
    links to it, then keeps the images whose devices share a tag with the model and the folders that still hold one."""
    def merged(items):
        return [dict({k: v for k, v in item.items() if k != "subitems_url"},
                     subitems=fetched[item["subitems_url"]]["os_list"]) if item.get("subitems_url") in fetched
                else item for item in items]

    def kept(items):
        found = []
        for item in items:
            if "subitems" in item:
                inside = kept(item["subitems"])
                if inside:
                    found.append(dict(item, subitems=inside))
            elif "devices" in item:
                if set(item["devices"]) & set(device["tags"]):
                    found.append(item)
            elif device.get("matching_type") == "inclusive":
                found.append(item)
        return found
    return kept(merged(manifest["os_list"]))


def test_the_imager_manifest_names_the_pi_models_and_fetches_the_image_list():
    manifest = imager_os_list.manifest("owner/repo", "pi-v1.2.8")
    assert list(manifest) == ["imager", "os_list"] and list(manifest["imager"]) == ["devices"]
    devices = manifest["imager"]["devices"]
    names = [device["name"] for device in devices]
    assert names == ["Raspberry Pi Zero 2 W", "Raspberry Pi 3", "Raspberry Pi 4", "Raspberry Pi Zero W"]
    for device in devices:
        assert set(device) == {"name", "description", "tags"} and device["description"] and device["tags"]
    folder, = manifest["os_list"]
    assert folder["name"] == "Quartermaster 1.2.8" and folder["description"] and "subitems" not in folder
    assert folder["subitems_url"] == ("https://github.com/owner/repo/releases/download/pi-v1.2.8/"
                                     "quartermaster-imager.json")
    assert folder["icon"].endswith("/pi-v1.2.8/packaging/icon.png")
    assert "sha256" not in json.dumps(manifest) and "size" not in json.dumps(manifest)
    images = imager_os_list.os_list([meta("st7789-240"), meta("st7735-128")], "owner/repo", "pi-v1.2.8")
    fetched = {folder["subitems_url"]: images}
    for device in devices:
        shown, = imager_shows(manifest, fetched, device)
        assert [item["url"] for item in shown["subitems"]] == [item["url"] for item in images["os_list"]], device
    assert imager_shows(manifest, {}, devices[0]) == []
    assert imager_shows(manifest, fetched, {"tags": ["pi5-64bit"]}) == []
    for tag in ("v1.2.8", "pi-v1.2", "pi-v1.2.8-rc1"):
        with pytest.raises(ValueError, match="not a Pi release tag"):
            imager_os_list.manifest("owner/repo", tag)


def test_every_workflow_action_is_pinned_by_commit():
    for workflow in WORKFLOWS.glob("*.yml"):
        for line in workflow.read_text().splitlines():
            if line.strip().startswith(("uses:", "- uses:")):
                assert re.search(r"uses: [\w.-]+/[\w./-]+@[0-9a-f]{40} # v\d", line), (workflow.name, line)


def test_the_image_workflow_builds_both_hats_on_tags_monthly_and_on_request():
    yaml = pytest.importorskip("yaml")
    workflow = yaml.safe_load((WORKFLOWS / "pi-images.yml").read_text())
    triggers = workflow[True]
    assert triggers["push"] == {"tags": ["pi-v*"]} and "workflow_dispatch" in triggers
    assert re.fullmatch(r"\d+ \d+ \d+ \* \*", triggers["schedule"][0]["cron"])
    assert workflow["permissions"] == {"contents": "write", "actions": "read"}
    assert workflow["concurrency"] == {"group": "pi-images${{ inputs.practice && '-practice' || '' }}",
                                       "cancel-in-progress": False}
    image = workflow["jobs"]["image"]
    assert image["needs"] == "release" and image["runs-on"] == "ubuntu-24.04"
    assert tuple(image["strategy"]["matrix"]["hat"]) == boot_config.IMAGE_PROFILES
    steps = "\n".join(step.get("run", "") for step in image["steps"])
    assert 'sudo tools/build_image.sh --hat "$HAT" --deb "$DEB"' in steps
    assert "2147483648" in steps and "qemu-user-static" in steps
    build = steps.index("sudo tools/build_image.sh")
    assert build < steps.index('sudo chown -R "$(id -u):$(id -g)" build/images') < steps.index('> "$IMAGE.sha256"')
    assert "gh release" not in steps.replace("gh release download", "")
    publish, boot = workflow["jobs"]["publish"], workflow["jobs"]["boot"]
    assert publish["needs"] == ["release", "image", "boot"] and "imager-list" not in workflow["jobs"]
    tagged = "${{ needs.release.outputs.tag && format('refs/tags/{0}', needs.release.outputs.tag) || '' }}"
    for job in (image, boot, publish):
        assert job["steps"][0]["uses"].startswith("actions/checkout@") and job["steps"][0]["with"] == {"ref": tagged}
    for job in (image, publish):
        assert job["env"]["TAG"] == "${{ needs.release.outputs.tag }}"
    assert workflow["jobs"]["release"]["outputs"] == {"tag": "${{ steps.pick.outputs.tag }}"}
    listing = "\n".join(step.get("run", "") for step in publish["steps"])
    assert "tools/imager_os_list.py" in listing and "--clobber" not in listing
    assert listing.index("tools/imager_os_list.py") < listing.index("tools/replace_release_assets.sh")
    assert listing.count("tools/imager_os_list.py") == 2 and "--manifest" in listing
    assert '--manifest \\\n  > build/release/quartermaster.rpi-imager-manifest' in listing
    assert re.search(r"replace_release_assets\.sh \"\$TAG\" \*\.img\.xz \*\.img\.xz\.sha256 quartermaster-imager\.json"
                     r" \\\n\s+quartermaster\.rpi-imager-manifest$", listing, re.M)
    release = (WORKFLOWS / "release-pi.yml").read_text()
    assert 'index($package) != null and index("SHA256SUMS") != null' in release
    assert triggers["workflow_dispatch"]["inputs"]["tag"]["required"] is False


def test_a_practice_run_builds_the_images_from_the_branch_and_publishes_nothing():
    yaml = pytest.importorskip("yaml")
    workflow = yaml.safe_load((WORKFLOWS / "pi-images.yml").read_text())
    practice = workflow[True]["workflow_dispatch"]["inputs"]["practice"]
    assert practice["type"] == "boolean" and practice["default"] is False
    jobs = workflow["jobs"]
    assert jobs["publish"]["if"] == "${{ !inputs.practice }}" and "if" not in jobs["image"]
    pick = jobs["release"]["steps"][0]["run"]
    assert pick.index('[ "$PRACTICE" = true ]') < pick.index("exit 0") < pick.index('[ "$EVENT" = push ]')
    assert jobs["release"]["env"]["PRACTICE"] == "${{ inputs.practice }}"
    steps = {step.get("name"): step for step in jobs["image"]["steps"]}
    own = steps["Build the Pi package from this branch"]
    assert own["if"] == "inputs.practice" and "tools/build_deb.py --target pi --out build/pi" in own["run"]
    assert "gh " not in own["run"]
    assert steps["Wait for the Pi package"]["if"] == "${{ !inputs.practice }}"
    wait = steps["Wait for the Pi package"]["run"]
    assert wait.index("if gh release download") < wait.index('[ -s "build/pi/quartermaster-pi_${version}_all.deb" ] &&') < \
        wait.index("sha256sum --check --ignore-missing --strict SHA256SUMS); then") < wait.index("break")
    assert wait.count("sha256sum") == 1 and wait.rstrip().endswith('echo "VERSION=$version" >> "$GITHUB_ENV"')
    assert set(workflow["jobs"]) == {"release", "image", "boot", "publish"} and "if" not in jobs["boot"]
    named = steps["Name the package and the image"]
    assert "if" not in named and 'test -f "build/pi/quartermaster-pi_${VERSION}_all.deb"' in named["run"]
    order = [step.get("name") for step in jobs["image"]["steps"]]
    assert order.index("Wait for the Pi package") < order.index("Name the package and the image") < \
        order.index("Build the image")


RUNNER_UBUNTU = """Types: deb
URIs: mirror+file:/etc/apt/apt-mirrors.txt
Suites: noble noble-updates noble-backports
Components: main universe restricted multiverse
Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg

Types: deb
URIs: mirror+file:/etc/apt/apt-mirrors.txt
Suites: noble-security
Components: main universe restricted multiverse
Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg
"""
PLAIN_UBUNTU = "Types: deb\nURIs: http://archive.ubuntu.com/ubuntu/\nSuites: noble noble-updates\n"


@pytest.mark.parametrize("ubuntu", [RUNNER_UBUNTU, PLAIN_UBUNTU], ids=["runner mirror list", "plain archive"])
@pytest.mark.parametrize("broken, works", [("microsoft-prod.list", True), ("ubuntu.sources", False)])
def test_a_broken_package_source_of_the_runner_does_not_stop_the_image_build(tmp_path, ubuntu, broken, works):
    yaml = pytest.importorskip("yaml")
    jobs = yaml.safe_load((WORKFLOWS / "pi-images.yml").read_text())["jobs"]
    for job, name in (("image", "Install the image tools"), ("boot", "Install the emulator")):
        run = next(step["run"] for step in jobs[job]["steps"] if step.get("name") == name)
        update = run[:run.index("sudo apt-get install -y ")]
        assert update.startswith("sudo apt-get update || {\n") and update.rstrip().endswith("sudo apt-get update\n}")
        sources, bin_dir = tmp_path / job / "sources", tmp_path / job / "bin"
        sources.mkdir(parents=True)
        bin_dir.mkdir()
        (sources / "ubuntu.sources").write_text(ubuntu)
        (sources / "microsoft-prod.list").write_text("deb [arch=amd64] https://packages.microsoft.com/ubuntu/24.04/prod noble main\n")
        (sources / "github_git-lfs.list").write_text("deb https://packagecloud.io/github/git-lfs/ubuntu/ noble main\n")
        (bin_dir / "sudo").write_text('#!/bin/sh\nexec "$@"\n')
        (bin_dir / "apt-get").write_text(
            '#!/bin/sh\necho "$*" >> "$LOG"\n[ "$1" = update ] || exit 0\n'
            '[ -e "$SOURCES/$BROKEN" ] && exit 100\nexit 0\n')
        for tool in ("sudo", "apt-get"):
            (bin_dir / tool).chmod(0o755)
        log = tmp_path / job / "apt.log"
        env = dict(os.environ, PATH="%s:%s" % (bin_dir, os.environ["PATH"]), LOG=str(log), SOURCES=str(sources),
                   BROKEN=broken)
        script = update.replace("/etc/apt/sources.list.d", str(sources))
        result = subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", script], env=env,
                                capture_output=True, text=True)
        assert log.read_text().splitlines() == ["update", "update"], name
        assert "::warning::A package source of the runner failed." in result.stdout
        assert sorted(path.name for path in sources.iterdir()) == ["ubuntu.sources"], name
        assert (sources / "ubuntu.sources").read_text() == ubuntu
        assert result.returncode == (0 if works else 100), (name, result.stdout, result.stderr)


def test_no_image_is_published_before_it_started_in_an_emulated_pi():
    yaml = pytest.importorskip("yaml")
    jobs = yaml.safe_load((WORKFLOWS / "pi-images.yml").read_text())["jobs"]
    boot = jobs["boot"]
    assert boot["needs"] == ["release", "image"] and "boot" in jobs["publish"]["needs"]
    assert boot["runs-on"] == "ubuntu-24.04" and 60 <= boot["timeout-minutes"] <= 150
    rows = boot["strategy"]["matrix"]["include"]
    assert tuple(row["hat"] for row in rows) == boot_config.IMAGE_PROFILES
    assert sorted(row["options"] for row in rows) == ["", "--imager"]
    assert boot["env"]["HAT"] == "${{ matrix.hat }}" and boot["env"]["OPTIONS"] == "${{ matrix.options }}"
    steps = {step.get("name"): step.get("run", "") for step in boot["steps"]}
    for tool in ("qemu-system-arm", "qemu-utils", "mtools", "sshpass", "xz-utils"):
        assert tool in steps["Install the emulator"].split(), tool
    upload = [step for step in jobs["image"]["steps"] if step.get("uses", "").startswith("actions/upload-artifact@")]
    assert upload[0]["with"]["name"] == "image-${{ matrix.hat }}"
    assert steps["Fetch the image"] == 'gh run download "$GITHUB_RUN_ID" --name "image-$HAT" --dir build/boot'
    start = steps["Start the image in an emulated Pi and log in over SSH"]
    assert start.count('tools/boot_image.sh --image build/boot/*.img.xz --hat "$HAT" $OPTIONS') == 2
    assert "sudo" not in start and "--work" not in start
    script = BOOT.read_text()
    assert "--hat) wanted_hat=$2 ;;" in script and 'die "the card is set for $hat, not for $wanted_hat"' in script


RELEASE_CASES = [
    ("workflow_dispatch", "behind", True, 1, "already exists on another commit", None),
    ("workflow_dispatch", "behind", False, 1, "already exists on another commit", None),
    ("workflow_dispatch", "here", True, 0, "already has its Pi package", None),
    ("workflow_dispatch", "none", False, 0, "", "--target"),
    ("push", "here", False, 0, "", "--verify-tag"),
    ("push", "here", True, 0, "already has its Pi package", None),
]


@pytest.mark.skipif(not all(shutil.which(tool) for tool in ("bash", "git", "jq")), reason="needs bash, git and jq")
@pytest.mark.parametrize("event, tag_at, released, status, message, created_with", RELEASE_CASES)
def test_a_pi_release_is_only_made_or_reused_for_the_commit_that_was_tested(
        tmp_path, monkeypatch, event, tag_at, released, status, message, created_with):
    yaml = pytest.importorskip("yaml")
    steps = yaml.safe_load((WORKFLOWS / "release-pi.yml").read_text())["jobs"]["release"]["steps"]
    publish = next(step["run"] for step in steps if step.get("name") == "Publish the Pi prerelease")

    def git(*args, cwd):
        return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false",
                               "-c", "tag.gpgsign=false", *args], cwd=cwd, check=True, capture_output=True,
                              text=True).stdout.strip()

    origin, work, tools = tmp_path / "origin.git", tmp_path / "work", tmp_path / "tools"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    subprocess.run(["git", "init", "-q", str(work)], check=True)
    git("remote", "add", "origin", str(origin), cwd=work)
    for path in [*(pi_release_notes.screen(hat, name) for hat, *_ in pi_release_notes.HATS
                   for name, _ in pi_release_notes.GALLERY), pi_release_notes.PICTURE]:
        (work / path).parent.mkdir(parents=True, exist_ok=True)
        (work / path).write_bytes(b"\x89PNG")
    git("add", "--all", cwd=work)
    git("commit", "-q", "-m", "first", cwd=work)
    if tag_at == "behind":
        git("push", "-q", "origin", "HEAD:refs/tags/pi-v9.9.9", cwd=work)
    git("commit", "-q", "--allow-empty", "-m", "second", cwd=work)
    if tag_at == "here":
        git("push", "-q", "origin", "HEAD:refs/tags/pi-v9.9.9", cwd=work)
    head = git("rev-parse", "HEAD", cwd=work)
    (work / "tools").mkdir()
    shutil.copy(ROOT / "tools" / "pi_release_notes.py", work / "tools")
    monkeypatch.setattr(pi_release_notes, "ROOT", work)
    (work / "build" / "pi").mkdir(parents=True)
    tools.mkdir()
    (tools / "python").write_text('#!/bin/sh\nexec "%s" "$@"\n' % sys.executable)
    (tools / "python").chmod(0o755)
    calls, notes = tmp_path / "calls", tmp_path / "notes.md"
    view = ('{"isPrerelease": true, "isDraft": false, "assets": [{"name": "quartermaster-pi_9.9.9_all.deb"},'
            ' {"name": "SHA256SUMS"}]}')
    (tools / "gh").write_text("#!/bin/sh\necho \"$1 $2 $*\" >> %s\n"
                              "[ \"$1 $2\" != 'release view' ] || { %s; }\n"
                              "[ \"$1 $2\" != 'release create' ] || while [ $# -gt 1 ]; do\n"
                              "  [ \"$1\" != --notes-file ] || cat \"$2\" > %s\n"
                              "  shift\n"
                              "done\n"
                              % (calls, "echo '%s'" % view if released else "exit 1", notes))
    (tools / "gh").chmod(0o755)
    env = {"PATH": "%s:/usr/bin:/bin" % tools, "HOME": str(tmp_path), "GITHUB_EVENT_NAME": event,
           "TAG": "pi-v9.9.9", "VERSION": "9.9.9", "GITHUB_REPOSITORY": "owner/repo"}
    result = subprocess.run(["bash", "-e", "-c", publish], cwd=work, env=env, capture_output=True, text=True)
    made = [line for line in calls.read_text().splitlines() if line.startswith("release create")] if calls.exists() else []
    assert result.returncode == status, result.stdout + result.stderr
    assert message in result.stdout
    if created_with is None:
        assert not made and not notes.exists()
    else:
        assert len(made) == 1 and " %s" % created_with in made[0] and "--prerelease --latest=false" in made[0]
        assert (head in made[0]) == (created_with == "--target")
        assert " --title Quartermaster 9.9.9 DIY for Raspberry Pi --notes-file build/notes.md " in made[0]
        assert notes.read_text() == pi_release_notes.notes("owner/repo", "pi-v9.9.9", head)
        assert "/%s/docs/screens/diy/240/aircraft.png' width='200'" % head in notes.read_text()
        assert "The images are added to this release shortly after it is published." in notes.read_text()
        for hat in boot_config.IMAGE_PROFILES:
            assert "`%s.img.xz`" % imager_os_list.image_name(hat, "9.9.9") in notes.read_text(), hat


def test_the_pi_release_can_be_run_by_hand_and_then_starts_the_image_build(monkeypatch):
    yaml = pytest.importorskip("yaml")
    workflow = yaml.safe_load((WORKFLOWS / "release-pi.yml").read_text())
    triggers = workflow[True]
    assert triggers["push"] == {"tags": ["pi-v*"]}
    tag = triggers["workflow_dispatch"]["inputs"]["tag"]
    assert tag["required"] is True and "created from this branch if it does not exist" in tag["description"]
    assert workflow["permissions"] == {"contents": "write", "actions": "write"}
    assert workflow["concurrency"]["group"] == "release-${{ inputs.tag || github.ref_name }}"
    job = workflow["jobs"]["release"]
    assert job["env"]["TAG"] == "${{ inputs.tag || github.ref_name }}"
    steps = job["steps"]
    names = [step.get("name") for step in steps]
    assert names[-3:] == ["Test the app and build the Pi package", "Publish the Pi prerelease", "Start the image build"]
    check = steps[names.index("Check the Pi release tag")]["run"]
    assert 'test "$TAG" = "pi-v$version"' in check
    publish = steps[names.index("Publish the Pi prerelease")]["run"]
    by_hand, pushed = publish.index('[ "$GITHUB_EVENT_NAME" = workflow_dispatch ]'), publish.index("tag_option=(--verify-tag)")
    target = publish.index('tag_option=(--target "$(git rev-parse HEAD)")')
    assert by_hand < target < pushed < publish.index('gh release create "$TAG" "${tag_option[@]}" --prerelease --latest=false')
    assert publish.count("--verify-tag") == 1 and publish.count("gh release create") == 1
    write = 'python tools/pi_release_notes.py --repo "$GITHUB_REPOSITORY" --tag "$TAG" --ref "$(git rev-parse HEAD)"'
    assert publish.index(write + " \\\n  > build/notes.md\n") < publish.index("gh release create")
    assert publish.count("pi_release_notes.py") == 1
    assert "--assets" not in publish and "--notes " not in publish
    assert '--title "Quartermaster $VERSION DIY for Raspberry Pi" --notes-file build/notes.md \\\n' in publish
    monkeypatch.setattr(pi_release_notes, "committed", lambda ref: {pi_release_notes.PICTURE})
    notes = pi_release_notes.notes("owner/repo", "pi-v9.9.9", "0" * 40)
    guide = (ROOT / "docs" / "diy.md").read_text()
    for hat, (label, pixels, chip) in imager_os_list.HATS.items():
        row = next(line for line in notes.splitlines() if "<br>**Waveshare %s**<br>" % label in line)
        photo = "https://www.waveshare.com/media/catalog/product/1/_/%s-1.jpg" % label.lower().replace(" ", "-")
        assert "<img src='%s'" % photo in row and photo in guide, hat
        assert "<sub>%s screen, %s</sub>" % (pixels, chip) in row, hat
        assert row.endswith("| `%s.img.xz` |" % imager_os_list.image_name(hat, "9.9.9")), hat
    assert 'git fetch --no-tags origin "refs/tags/$TAG"' in publish
    assert publish.index("already exists on another commit") < publish.index('gh release view "$TAG"')
    assert "pyyaml" in steps[names.index("Test the app and build the Pi package")]["run"].split()
    start = steps[-1]
    assert start["if"] == "github.event_name == 'workflow_dispatch'"
    assert start["run"] == 'gh workflow run pi-images.yml --ref "$TAG" -f tag="$TAG"'
    assert start["env"] == {"GH_REPO": "${{ github.repository }}"}
    images = yaml.safe_load((WORKFLOWS / "pi-images.yml").read_text())
    assert "tag" in images[True]["workflow_dispatch"]["inputs"] and images[True]["push"] == {"tags": ["pi-v*"]}
    pick = images["jobs"]["release"]["steps"][0]["run"]
    assert 'elif [ -n "$REQUESTED" ]; then' in pick and images["jobs"]["release"]["env"]["REQUESTED"] == "${{ inputs.tag }}"


def test_the_diy_guide_offers_the_ready_image_before_the_manual_route():
    guide = (ROOT / "docs" / "diy.md").read_text()
    easy, manual = guide.index("## Easiest way: flash a ready image"), guide.index("## Assemble and install manually")
    assert easy < manual
    section = guide[easy:manual]
    version = json.loads((ROOT / "app-builder.json").read_text())["version"]
    for text in ("releases/download/pi-v%s/quartermaster.rpi-imager-manifest) and open it" % version,
                 "Use the newest version.", "Click **Quartermaster %s**" % version, "**Skip customisation**",
                 "**Localisation**: choose your capital city", "**Use custom file**", "**Apply & Restart**",
                 "shows no set-up pages for a file chosen this way", "`sudo nmtui`",
                 "`%s.img.xz`" % imager_os_list.image_name("st7735-128", version),
                 "`%s.img.xz`" % imager_os_list.image_name("st7789-240", version),
                 "each named after its HAT", "Use custom", "quartermaster.txt",
                 "quartermaster@%s.service" % SERVICE_USER, "`/var/lib/quartermaster/quartermaster/`", "Wi-Fi country",
                 "can only join a 2.4 GHz network", "type the 2.4 GHz name", "Click **Cancel**",
                 "it fills in both password boxes too", "clear both password boxes and type its password in each",
                 "Some other tools, such as Win32 Disk Imager, need it unpacked first",
                 "The images of the newest Pi release are rebuilt every month",
                 "built-in `%s` account" % SERVICE_USER,
                 "8 GB or more", "switched on from the first start", "does not choose a country for you",
                 "leaves out 6 GHz", "`sudo raspi-config nonint do_wifi_country US`", "reminds you at each login",
                 "there **Next** works only after you choose a city", "It throws away what you entered on every page",
                 "On the last page, **Remote access**", "fill in the **User** page too"):
        assert text in section, text
    for hat in imager_os_list.HATS:
        listed = imager_os_list.entry(meta(hat, version), "owner/repo", "pi-v" + version)["name"]
        assert "| %s |" % listed in section, listed
    assert not re.search(r"st77\d\d-\d+\.img", guide)
    assert "| Waveshare 1.44inch LCD HAT | Waveshare 1.3inch LCD HAT |" in guide


def test_every_pi_release_link_in_the_guide_and_the_readme_is_for_this_version():
    version = json.loads((ROOT / "app-builder.json").read_text())["version"]
    for name in ("docs/diy.md", "README.md"):
        text = (ROOT / name).read_text()
        assert set(re.findall(r"pi-v\d+\.\d+\.\d+", text)) == {"pi-v" + version}, name
        assert set(re.findall(r"quartermaster-pi[_-](\d+\.\d+\.\d+)", text)) <= {version}, name
    assert "release [Quartermaster %s DIY for Raspberry Pi](" % version in (ROOT / "docs" / "diy.md").read_text()


def test_the_diy_guide_gives_the_default_login_and_says_to_change_the_password():
    guide = (ROOT / "docs" / "diy.md").read_text()
    section = guide[guide.index("## Easiest way: flash a ready image"):guide.index("## Assemble and install manually")]
    login = section[section.index("### Log in"):section.index("### Good to know")]
    for text in ("user `pi` with password `pi`", "SSH is on", "`ssh pi@quartermaster.local`", "`ssh pi@<address>`",
                 "Change the password right away", "`passwd`", "shared or public Wi-Fi",
                 "reminds you each time you log in",
                 "set in Raspberry Pi Imager replace the default", "`~/quartermaster/logs`", "host keys",
                 "`~/quartermaster/recon`", "`~/quartermaster/aircraft`", "**Terminal** or **PowerShell**",
                 "Nothing shows on the screen while you type a password", "your 2.4 GHz network", "Type `yes` to go on",
                 "`sudo usermod -aG %s <user>`" % SERVICE_USER):
        assert text in login, text
    assert login.index("user `pi` with password `pi`") < login.index("Change the password right away")
    assert "no password and SSH is off" not in guide
    script = SCRIPT.read_text()
    assert "login_user=pi login_password=pi" in script and "host_name=quartermaster" in script


def test_the_diy_guide_says_where_each_command_is_typed_and_copies_every_folder_the_login_reads():
    guide = (ROOT / "docs" / "diy.md").read_text()
    login = guide[guide.index("### Log in"):guide.index("### Good to know")]
    for text in ("type `ssh pi@quartermaster.local` on your computer", "What you type there runs on the Pi",
                 "Only `ssh`, `scp` and `ssh-keygen -R` are typed on your computer", "Type `exit` to leave the Pi",
                 "which you type on your computer, not on the Pi", "type `exit` first, or open a second Terminal window",
                 "the folder that the Terminal window is in", "`C:\\Users\\<your name>`",
                 "type `sudo usermod -aG %s <user>` on the Pi" % SERVICE_USER):
        assert text in login, text
    copies = re.findall(r"^scp -r pi@quartermaster\.local:quartermaster/(\S+) \.$", login, re.M)
    assert copies == shell_list(SCRIPT.read_text(), "log_folders")
    for line in guide.splitlines():
        if "`ssh-keygen -R" in line:
            assert "on your computer" in line, line


def test_the_diy_guide_says_what_to_copy_before_a_card_is_written_again():
    guide = (ROOT / "docs" / "diy.md").read_text()
    good = guide[guide.index("### Good to know"):guide.index("## Assemble and install manually")]
    flashing = next(line for line in good.splitlines() if line.startswith("- **Flashing the newest image.**"))
    folders = shell_list(SCRIPT.read_text(), "log_folders")
    assert len(folders) == 3 and folders[0] == "logs"
    named = ", ".join("`%s`" % folder for folder in folders[:-1]) + " and `%s`" % folders[-1]
    assert "Flashing erases the card. Before you flash, copy all three folders, %s, to your computer" % named in flashing
    mode = settings((IMAGE / "quartermaster-logs.service").read_text())["ExecStart"][0].split()[-3]
    opened = " ".join("~/quartermaster/" + folder for folder in folders[1:])
    assert "On a card that was flashed with an image older than 1.2.9" in flashing
    assert "`sudo chmod -R %s %s` on the Pi" % (mode, opened) in flashing
    assert "Your settings and upload keys are erased too" in flashing
    for line in guide.splitlines():
        if "write the card again" in line:
            assert "erases the card" in line, line
    differences = guide[guide.index("## How the DIY version differs"):guide.index("## Troubleshooting")]
    assert "**Flashing the newest image** under [Good to know](#good-to-know)" in differences


@pytest.mark.skipif(not shutil.which("dpkg-deb"), reason="building a Debian package requires dpkg-deb")
def test_only_a_new_image_opens_recon_and_aircraft_to_the_login_as_the_diy_guide_says(tmp_path):
    """The rule that lets the login read the folders is set up by the image and is not in the package, so a card
    flashed with an older image keeps its older rule when the package is updated."""
    from tools import build_deb
    script = SCRIPT.read_text()
    rule = sorted(path.name for path in IMAGE.glob("quartermaster-logs*"))
    assert rule == ["quartermaster-logs-changed.service", "quartermaster-logs.path", "quartermaster-logs.service"]
    assert all(unit in script for unit in rule)
    stage = tmp_path / "unpack"
    subprocess.run(["dpkg-deb", "--raw-extract", str(build_deb.build(tmp_path, "pi")), str(stage)], check=True)
    shipped = {path.name for path in stage.rglob("*") if path.is_file()}
    assert not shipped & {path.name for path in IMAGE.iterdir() if path.is_file()}
    guide = (ROOT / "docs" / "diy.md").read_text()
    updating = next(line for line in guide.splitlines() if line.startswith("- **Updating.**"))
    folders = shell_list(script, "log_folders")
    assert "The package does not change what the image set up." in updating
    assert "image older than 1.2.9, your login can still read only `~/quartermaster/%s`" % folders[0] in updating
    assert "To read %s too, flash the newest image." % " and ".join(
        "`~/quartermaster/%s`" % folder for folder in folders[1:]) in updating


def test_the_diy_guide_names_the_keys_of_a_usb_keyboard_that_follow_the_hat(monkeypatch, tmp_path):
    from quartermaster.ui import hat, keys, screens
    from tests.fakes import Display
    from tests.test_ui import make_app, press
    guide = (ROOT / "docs" / "diy.md").read_text()
    controls = guide[guide.index("## Controls"):guide.index("### Where the CardputerZero keys are")]
    for text in ("**Tab** and the keys for up and down work differently, to match the HAT. **Tab** does what **K3** does.",
                 "Up and down, on the arrow keys or on **F** and **X**, do what the joystick does: "
                 "on the menu they move one item at a time",
                 "There, only left and right change the choice.", "The arrow keys or **F X Z C** move"):
        assert text in controls, text
    assert "Three keys" not in controls
    assert hat.BUTTONS[16] == "TAB"
    assert keys.ARROWS == {"f": "UP", "x": "DOWN", "z": "LEFT", "c": "RIGHT"}
    moved = {}
    for profile in ("cardputer", "st7735-128"):
        if profile != "cardputer":
            monkeypatch.setattr(Display, "profile", profile, raising=False)
            monkeypatch.setattr(Display, "width", 128, raising=False)
            monkeypatch.setattr(Display, "height", 128, raising=False)
        (tmp_path / profile).mkdir()
        app = make_app(tmp_path / profile)
        steps = []
        for key in ("DOWN", "X", "F", "UP"):
            press(app, key)
            steps.append(app.screen.index)
        press(app, "ESC")
        assert isinstance(app.screen, screens.Confirm) and app.screen.choice == 0
        for key in ("DOWN", "X"):
            press(app, key)
            steps.append(app.screen.choice)
        moved[profile] = steps
    row = screens.Home.COLUMNS
    assert moved == {"cardputer": [row, 0, row, 0, 1, 0], "st7735-128": [1, 2, 1, 0, 0, 0]}
    for key, choice in (("RIGHT", 1), ("Z", 0), ("C", 1)):
        press(app, key)
        assert app.screen.choice == choice, key
    press(app, "TAB")
    assert app.square.mode == "actions"


def test_the_manual_install_downloads_each_version_into_a_folder_of_its_own():
    """A file that is already there is not replaced by wget, and SHA256SUMS lists only the package of its release.
    So a download next to the files of an older version would check the old list."""
    version = json.loads((ROOT / "app-builder.json").read_text())["version"]
    guide = (ROOT / "docs" / "diy.md").read_text()
    manual = guide[guide.index("## Assemble and install manually"):guide.index("## Controls")]
    step = manual[manual.index("\n3. "):manual.index("\n4. ")]
    start = step.index("```sh\n") + len("```sh\n")
    block = step[start:step.index("\n```", start)].splitlines()
    folder = "~/quartermaster-pi-%s" % version
    assert block[0] == "mkdir -p %s && cd %s" % (folder, folder)
    assert [line for line in block if line.startswith("wget ")] == [
        'wget "$release/quartermaster-pi_%s_all.deb" "$release/SHA256SUMS"' % version]
    assert block[-1] == "sha256sum --check SHA256SUMS && sudo apt install ./quartermaster-pi_%s_all.deb" % version
    for text in ("Then run these commands on the Pi. The first one makes a folder for this version and moves into it.",
                 "Each version gets its own folder"):
        assert text in step, text
    assert "in a new directory" not in step
    release = (WORKFLOWS / "release-pi.yml").read_text()
    assert 'sha256sum "quartermaster-pi_${VERSION}_all.deb" > SHA256SUMS' in release


def test_the_diy_guide_warns_about_formatting_before_the_card_is_opened():
    guide = (ROOT / "docs" / "diy.md").read_text()
    wrong = next(line for line in guide.splitlines() if line.startswith("- **Wrong HAT?**"))
    assert wrong.index("Put the card in your computer.") < wrong.index("Click **Cancel**")
    assert wrong.index("Click **Cancel**") < wrong.index("**bootfs**") < wrong.index("put the card back in the Pi")
    assert wrong.endswith("put the card back in the Pi.")
    assert (IMAGE / "quartermaster.txt").read_text().splitlines()[-1] == "display=@HAT@"


def test_the_diy_guide_says_what_to_type_in_place_of_each_placeholder():
    guide = (ROOT / "docs" / "diy.md").read_text()
    found = set()
    for line in guide.splitlines():
        for command in re.findall(r"`((?:ssh|scp|sudo) [^`]*)`", line):
            for word in re.findall(r"<[^>]+>", command):
                assert "in place of `%s`" % word in line, (word, line)
                found.add(word)
    assert found == {"<address>", "<your user>", "<user>"}
    assert "`ssh pi@192.168.1.23`" in guide
    login = guide[guide.index("### Log in"):guide.index("### Good to know")]
    assert "If you log in with the Pi's address, type the address in place of `quartermaster.local`." in login
    forget = [line for line in guide.splitlines() if "`ssh-keygen -R " in line]
    assert len(forget) == 2
    for line in forget:
        assert ("If you connect with another name or with the Pi's address, type that in place of "
                "`quartermaster.local`, for example `ssh-keygen -R 192.168.1.23`.") in line, line


def test_the_diy_guide_names_the_line_under_the_menu_and_what_it_says_during_a_wardrive(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from quartermaster.ui import screens
    from tests.fakes import Display
    from tests.test_ui import make_app, placed
    guide = (ROOT / "docs" / "diy.md").read_text()
    assert guide.count("line at the bottom") == 1
    assert "The line at the bottom of each screen shows what they do there." in guide
    assert guide.count("The line under the list") == 4
    menu = guide[guide.index("### The menu\n"):guide.index("### Hardware\n")]
    for text in ("The line under the list tells you about the item you are on.",
                 "or **BLE only** (Bluetooth only) when there is no Wi-Fi adapter or node.",
                 "During a wardrive, it says **No fix yet** until the GPS has a fix, and then **REC** and the time."):
        assert text in menu, text
    first = guide[guide.index("### Your first wardrive\n"):guide.index("### Stopping a wardrive\n")]
    assert "the menu then shows **No fix yet** until the GPS has a fix, and then **REC** and the time." in first
    home = screens.Home()
    app = SimpleNamespace(wardriving=False, hw=SimpleNamespace(adapters=[]), nodes=SimpleNamespace(in_use=lambda: []))
    assert home.subtitle("Wardrive", app)[0] == "BLE only"
    app.hw.adapters = ["wlan0", "wlan1", "wlan2"]
    assert home.subtitle("Wardrive", app)[0] == "3 radios"
    app.wardriving, app.drive = True, SimpleNamespace(waiting=True, elapsed=lambda: 65)
    assert home.subtitle("Wardrive", app)[0] == "No fix yet"
    app.drive.waiting = False
    assert home.subtitle("Wardrive", app)[0].startswith("REC ")
    for size, profile in ((128, "st7735-128"), (240, "st7789-240")):
        monkeypatch.setattr(Display, "profile", profile, raising=False)
        monkeypatch.setattr(Display, "width", size, raising=False)
        monkeypatch.setattr(Display, "height", size, raising=False)
        (tmp_path / profile).mkdir()
        hat_app = make_app(tmp_path / profile)
        shown = placed(hat_app, monkeypatch)
        line = shown[hat_app.screen.subtitle("Wardrive", hat_app)[0]][0]
        footer = shown["OK select · K3 actions"][0]
        assert line < footer == max(y for y, _ in shown.values()), profile


def test_after_address_and_pin_the_first_k2_goes_back_to_the_code_as_the_diy_guide_says(monkeypatch, tmp_path):
    from quartermaster.ui import screens
    from tests.fakes import Display
    from tests.test_ui import make_app, press
    guide = (ROOT / "docs" / "diy.md").read_text()
    said = "While the address and PIN show, the first **K2** goes back to the QR code, so press **K2** twice."
    for start in (guide.index("**Phone setup.**"), guide.index("- **Phone / PC setup**")):
        part = guide[start:guide.index("\n", start)]
        assert "**Address and PIN**" in part and said in part, part[:40]
    monkeypatch.setattr(Display, "profile", "st7789-240", raising=False)
    monkeypatch.setattr(Display, "width", 240, raising=False)
    monkeypatch.setattr(Display, "height", 240, raising=False)
    app = make_app(tmp_path)
    press(app, "K", "ENTER")
    setup = app.screen
    assert isinstance(setup, screens.PhoneSetup) and setup.server is not None
    press(app, "TAB")
    assert app.square.actions[0] == ("Address and PIN", "address")
    press(app, "ENTER")
    assert app.square.mode == "address"
    press(app, "ESC")
    assert app.screen is setup and setup.server is not None and app.square.mode == ""
    press(app, "ESC")
    assert isinstance(app.screen, screens.Keys) and setup.server is None


def test_the_diy_guide_says_a_new_pi_release_is_a_prerelease_until_it_is_tested():
    guide = (ROOT / "docs" / "diy.md").read_text()
    differences = guide[guide.index("## How the DIY version differs"):guide.index("## Troubleshooting")]
    row = next(line for line in differences.splitlines() if line.startswith("| Releases on GitHub |"))
    cardputer, diy = row.split(" | ")[1:3]
    assert "A new Pi release is marked **Pre-release** until it has been tested." in diy
    assert "Pre-release" not in cardputer
    created = (WORKFLOWS / "release-pi.yml").read_text()
    promoted = (WORKFLOWS / "promote-pi.yml").read_text()
    assert 'gh release create "$TAG" "${tag_option[@]}" --prerelease --latest=false' in created
    assert "--prerelease=false --latest=false" in promoted
