#!/usr/bin/env python3
"""Build Quartermaster for CardputerZero (default) or Raspberry Pi (--target pi).

The layout follows the CardputerZero AppBuilder convention, so the result installs with apt
and publishes to the store with: czdev publish --deb build/quartermaster_<version>_all.deb
"""

import argparse
import json
import os
from pathlib import Path
import posixpath
import re
import shutil
import subprocess
import tempfile

HERE = Path(__file__).resolve().parents[1]
HOMEPAGE = "https://github.com/valleytechsolutions/Quartermaster"
LINK = re.compile(r"\]\(([^)\s]+)\)")
PREFIX = "usr/share/APPLaunch"
MAINTAINER = "Valleytech Solutions <178694271+valleytechsolutions@users.noreply.github.com>"
COPYRIGHT = ("Quartermaster\nCopyright (c) 2026 Valleytech Custom Solutions (Your Pal Kal)\n"
             "License: GPL-3.0-only from 1.2.2. Earlier releases are MIT.\n\n"
             "Deep scan: deauth, 5 GHz slice and evil-twin logic ported from SquachWatch-CYD (GPL-3.0).\n"
             "Flock probe tiers and IE signature from flock-you by colonelpanichacks (MIT), with community research "
             "by @NitekryDPaul and DeFlockJoplin.\n\n")
UNITS = ("quartermaster-radio.socket", "quartermaster-radio.service")
# The AppStore installs with plain dpkg when Depends are met, so anything left in Recommends would never arrive.
# rtl-sdr brings rtl_adsb for aircraft, the stick's udev rules and the TV driver blacklist, and starts no service.
# usb-modeswitch turns adapters that first show a driver disc into Wi-Fi adapters. The firmware packages are the ones
# the CardputerZero image already has. firmware-misc-nonfree is left out: the image lacks it, and the AppStore
# never runs apt-get update, so asking for it could stop the whole install. The Hardware screen names it instead.
DEPENDS = ("python3 (>= 3.9), python3-pil, python3-numpy, python3-serial, python3-dbus, iw, iproute2, "
           "ca-certificates, network-manager, bluez, alsa-utils, rtl-sdr, python3-spidev, "
           "python3-smbus | python3-smbus2, python3-libgpiod, python3-cryptography, usb-modeswitch, "
           "firmware-realtek, firmware-mediatek, firmware-atheros")


def cardputer_layout(stage, manifest):
    pkg = manifest["package_name"]
    launcher = stage / PREFIX / "bin" / manifest["bin_name"]
    launcher.parent.mkdir(parents=True)
    launcher.write_text("#!/bin/sh\ncd /%s/apps/%s || exit 1\nexec python3 -m quartermaster \"$@\"\n"
                        % (PREFIX, pkg))
    launcher.chmod(0o755)
    images = stage / PREFIX / "share" / "images"
    images.mkdir(parents=True)
    shutil.copy2(HERE / "packaging" / "icon-launcher.png", images / (pkg + ".png"))
    applications = stage / PREFIX / "applications"
    applications.mkdir(parents=True)
    (applications / (pkg + ".desktop")).write_text(
        "[Desktop Entry]\nName=%s\nExec=/%s/bin/%s\nTerminal=false\nSysplause=true\n"
        "Icon=share/images/%s.png\nType=Application\n" % (manifest["app_name"], PREFIX, manifest["bin_name"], pkg))
    return stage / PREFIX / "apps" / pkg


def pi_layout(stage):
    launcher = stage / "usr/bin/quartermaster"
    launcher.parent.mkdir(parents=True)
    launcher.write_text('#!/bin/sh\nset -e\nIFS= read -r display < /etc/quartermaster/display\n'
                        'cd /usr/lib/quartermaster/app\n'
                        'if [ "$#" -eq 0 ]; then\n'
                        '  [ ! -r /etc/quartermaster/settings ] || /usr/bin/python3 -m quartermaster '
                        '--apply-settings /etc/quartermaster/settings || true\n'
                        '  if [ -r /etc/quartermaster/status_screen ]; then\n'
                        '    IFS= read -r screen < /etc/quartermaster/status_screen\n'
                        '    case $screen in\n'
                        '      epaper) exec /usr/bin/python3 -m quartermaster --headless-wardrive --epaper 2in13-v4 ;;\n'
                        '      oled) exec /usr/bin/python3 -m quartermaster --headless-wardrive --oled ssd1309 ;;\n'
                        '    esac\n'
                        '  fi\n'
                        'fi\n'
                        'if [ -e /etc/quartermaster/autostart ]; then set -- --autostart "$@"; fi\n'
                        'exec /usr/bin/python3 -m quartermaster --display "$display" "$@"\n')
    launcher.chmod(0o755)
    profile = stage / "etc/quartermaster/display"
    profile.parent.mkdir(parents=True)
    profile.write_text("st7735-128\n")
    return stage / "usr/lib/quartermaster/app"


def pi_guide(version):
    """docs/diy.md as the Pi package has it: links to pages that the package leaves out go to GitHub, at the Pi
    release of this version."""
    def link(match):
        target = match.group(1)
        if target.startswith(("#", "screens/", "https://", "http://", "mailto:")):
            return match.group(0)
        return "](%s/blob/pi-v%s/%s)" % (HOMEPAGE, version, posixpath.normpath("docs/" + target))
    return LINK.sub(link, (HERE / "docs/diy.md").read_text(encoding="utf-8"))


def build(out, target="cardputer"):
    manifest = json.loads((HERE / "app-builder.json").read_text())
    pkg, version = manifest["package_name"], manifest["version"]
    pi = target == "pi"
    deb_name = "quartermaster-pi" if pi else pkg
    stage = Path(tempfile.mkdtemp(prefix="deb-"))
    stage.chmod(0o755)
    try:
        app_dir = pi_layout(stage) if pi else cardputer_layout(stage, manifest)
        excluded = ["__pycache__", "*.pyc"]
        if not pi:
            excluded.extend(("hat.py", "square.py"))
        shutil.copytree(HERE / "quartermaster", app_dir / "quartermaster",
                        ignore=shutil.ignore_patterns(*excluded))
        helper = stage / "usr/lib" / pkg / "radiod.py"
        helper.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(HERE / "packaging" / "radiod.py", helper)
        units = stage / "usr/lib/systemd/system"
        units.mkdir(parents=True)
        for unit in UNITS:
            shutil.copy2(HERE / "packaging" / unit, units / unit)
        if pi:
            shutil.copy2(HERE / "packaging/quartermaster@.service", units)
        rules = stage / "usr/lib/udev/rules.d" / ("70-%s.rules" % pkg)
        rules.parent.mkdir(parents=True)
        shutil.copy2(HERE / "packaging" / "70-quartermaster.rules", rules)
        if not pi:
            tmpfiles = stage / "usr/lib/tmpfiles.d" / (pkg + ".conf")
            tmpfiles.parent.mkdir(parents=True)
            shutil.copy2(HERE / "packaging" / "quartermaster.tmpfiles", tmpfiles)
        doc = stage / "usr/share/doc" / deb_name
        doc.mkdir(parents=True)
        notices = HERE / "quartermaster" / "assets" / "node-firmware-notices.txt"
        (doc / "copyright").write_text(COPYRIGHT + (HERE / "LICENSE").read_text(encoding="utf-8") + "\n\n" +
                                       notices.read_text(encoding="utf-8"), encoding="utf-8")
        control = stage / "DEBIAN"
        control.mkdir()
        if pi:
            (doc / "diy.md").write_text(pi_guide(version), encoding="utf-8")
            (doc / "screens").mkdir()
            shutil.copy2(HERE / "docs/screens/waveshare-preview.png", doc / "screens")
            shutil.copytree(HERE / "docs/screens/diy", doc / "screens/diy")
            (control / "conffiles").write_text("/etc/quartermaster/display\n")
        size = sum(f.stat().st_size for f in stage.rglob("*") if f.is_file()) // 1024
        depends = DEPENDS
        if pi:
            depends += ", python3-luma.lcd, python3-rpi-lgpio | python3-rpi.gpio, python3-gpiozero"
        (control / "control").write_text(
            "Package: %s\nVersion: %s\nArchitecture: all\nMaintainer: %s\nInstalled-Size: %d\n"
            "Depends: %s\n%sSection: %s\nPriority: optional\n"
            "Homepage: %s\n"
            "Description: %s\n Wi-Fi and Bluetooth wardriver for %s.\n"
            % (deb_name, version, MAINTAINER, size, depends, "Conflicts: quartermaster\n" if pi else "",
               "net" if pi else "APPLaunch", HOMEPAGE,
               "GPS wardriver for Waveshare LCD HATs with WiGLE logs and uploads" if pi else manifest["description"],
               "Raspberry Pi with a Waveshare LCD HAT" if pi else "the M5Stack CardputerZero"))
        for script in ("postinst", "prerm"):
            shutil.copy2(HERE / "packaging" / script, control / script)
            (control / script).chmod(0o755)
        for path in stage.rglob("*"):
            if path.is_dir():
                path.chmod(0o755)
            elif path.parent.name != "DEBIAN" and not os.access(path, os.X_OK):
                path.chmod(0o644)
        out.mkdir(parents=True, exist_ok=True)
        deb = out / ("%s_%s_all.deb" % (deb_name, version))
        compression = ["--threads-max=1", "-z3"] if pi else []
        subprocess.run(["dpkg-deb", "--root-owner-group", "-Zxz", *compression, "-b", str(stage), str(deb)], check=True,
                       stdout=subprocess.DEVNULL)
        return deb
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=str(HERE / "build"))
    parser.add_argument("--target", choices=("cardputer", "pi"), default="cardputer")
    args = parser.parse_args()
    deb = build(Path(args.out), args.target)
    print(deb)


if __name__ == "__main__":
    main()
