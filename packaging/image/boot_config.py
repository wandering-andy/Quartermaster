"""Apply the display named in quartermaster.txt on the boot partition to /etc/quartermaster/display."""

import os
import re
import sys

IMAGE_PROFILES = ("st7735-128", "st7789-240")
PROFILES = IMAGE_PROFILES + ("st7789-320x240",)
ALIASES = {"1.44": "st7735-128", "1.44in": "st7735-128", "128": "st7735-128",
           "1.3": "st7789-240", "1.3in": "st7789-240", "240": "st7789-240",
           "2": "st7789-320x240", "2in": "st7789-320x240", "320": "st7789-320x240"}
BOOT_FILE = "/boot/firmware/quartermaster.txt"
DISPLAY_FILE = "/etc/quartermaster/display"
AUTOSTART_FILE = "/etc/quartermaster/autostart"
YES = ("wardrive", "yes", "on", "true", "1")
NO = ("no", "off", "false", "0", "none", "")


def parse(text):
    """The profile the last setting line names, None when no line names one; ValueError for an unknown display.

    A setting line is display=<name>, or any other line that ends with a HAT name."""
    chosen = None
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip().lower()
        setting = re.fullmatch(r"display\s*[=:]\s*(.*)", line)
        value = (setting.group(1) if setting else line).strip().strip("\"'").strip()
        value = ALIASES.get(value, value)
        if not setting and value not in PROFILES:
            value = re.split(r"[\s=:\"']+", value)[-1]
        if value in PROFILES:
            chosen = value
        elif setting:
            raise ValueError("unknown display %r, expected one of %s" % (value, ", ".join(PROFILES)))
    return chosen


def apply(boot_file=BOOT_FILE, display_file=DISPLAY_FILE):
    try:
        with open(boot_file, "rb") as handle:
            text = handle.read(65536).decode("utf-8-sig", "replace")
    except FileNotFoundError:
        return "no %s, keeping the current display" % boot_file
    try:
        profile = parse(text)
    except ValueError as exc:
        return "%s: %s, keeping the current display" % (boot_file, exc)
    if profile is None:
        return "%s names no display, keeping the current one" % boot_file
    try:
        with open(display_file) as handle:
            current = handle.readline().strip()
    except FileNotFoundError:
        current = None
    if current == profile:
        return "display is %s" % profile
    temporary = display_file + ".new"
    with open(temporary, "w") as handle:
        handle.write(profile + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, 0o644)
    os.replace(temporary, display_file)
    return "display changed from %s to %s" % (current or "none", profile)


def parse_autostart(text):
    """True or False for the last autostart=<value> line, None when there is none; ValueError for an unknown value."""
    chosen = None
    for raw in text.splitlines():
        match = re.fullmatch(r"autostart\s*[=:]\s*(.*)", raw.split("#", 1)[0].strip().lower())
        if match:
            value = match.group(1).strip().strip("\"'").strip()
            if value not in YES + NO:
                raise ValueError("unknown autostart %r, expected wardrive or off" % value)
            chosen = value in YES
    return chosen


def apply_autostart(boot_file=BOOT_FILE, autostart_file=AUTOSTART_FILE):
    """Create or remove the marker file that makes the launcher start the wardrive; off when nothing says on."""
    try:
        with open(boot_file, "rb") as handle:
            text = handle.read(65536).decode("utf-8-sig", "replace")
        wanted = parse_autostart(text)
    except FileNotFoundError:
        return "no %s, keeping the current autostart" % boot_file
    except ValueError as exc:
        return "%s: %s, keeping the current autostart" % (boot_file, exc)
    if wanted is None:
        return "%s has no autostart line, autostart stays off" % boot_file if not os.path.exists(autostart_file) \
            else "%s has no autostart line, keeping autostart on" % boot_file
    if wanted:
        with open(autostart_file, "w") as handle:
            handle.write("wardrive\n")
        os.chmod(autostart_file, 0o644)
        return "autostart is on"
    try:
        os.unlink(autostart_file)
    except FileNotFoundError:
        pass
    return "autostart is off"


def main():
    print(apply())
    print(apply_autostart())
    # python3 -I leaves this directory off sys.path
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    try:
        import boot_settings

        for line in boot_settings.apply(BOOT_FILE):
            print(line)
    except Exception as exc:
        print("quartermaster.txt settings were not applied: %s" % exc.__class__.__name__)
    return 0


if __name__ == "__main__":
    sys.exit(main())
