"""Apply the key=value lines of quartermaster.txt that go beyond display= and autostart=.

Runs as root from boot_config.py, standard library only. Each key present in the file wins at every boot; a key that is
absent or blank leaves the device alone, so a file can be copied to many devices. Secrets are never printed.
"""

import json
import os
import pwd
import re
import shutil
import subprocess
import uuid

BOOT_FILE = "/boot/firmware/quartermaster.txt"
ETC = "/etc/quartermaster"
NM_DIR = "/etc/NetworkManager/system-connections"
SERVICE_USER_FILE = "/etc/quartermaster/service_user"
DEFAULT_SERVICE_USER = "_quartermaster"
WIFI_NAME = "quartermaster-wifi"

# Handled by boot_config.py itself, or here with their own files.
HARDWARE = ("display", "autostart")
SCREENS = ("epaper", "oled", "none")
WIFI = ("wifi_ssid", "wifi_password", "wifi_hidden")
ACCOUNTS = ("wigle_name", "wigle_token", "wigle_encoded", "wdgwars_key", "wardrift_key")
SECRETS = ("wifi_password", "wigle_token", "wigle_encoded", "wdgwars_key", "wardrift_key")
OTHER = ("status_screen", "scrub")
RESERVED = HARDWARE + WIFI + ACCOUNTS + OTHER
YES = ("yes", "on", "true", "1")
NO = ("no", "off", "false", "0")


def parse(text):
    """{key: value} for every key=value line. A later line wins. Secrets are kept exactly as typed."""
    found = {}
    for raw in text.lstrip("\ufeff").splitlines():
        line = raw.strip()
        if line.startswith("#"):
            continue
        match = re.fullmatch(r"([A-Za-z][A-Za-z0-9_]*)\s*[=:]\s*(.*)", line)
        if not match:
            continue
        key, value = match.group(1).lower(), match.group(2).strip()
        if key not in SECRETS:
            value = re.split(r"\s+#", value, maxsplit=1)[0].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        found[key] = value
    return found


def service_user():
    try:
        with open(SERVICE_USER_FILE) as handle:
            name = handle.readline().strip()
    except OSError:
        name = DEFAULT_SERVICE_USER
    try:
        return pwd.getpwnam(name)
    except KeyError:
        return None


def _write(path, text, mode, owner=None):
    temporary = path + ".new"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, mode)
    if owner is not None:
        os.chown(temporary, owner.pw_uid, owner.pw_gid)
    os.replace(temporary, path)


def apply_status_screen(pairs, etc=ETC):
    value = pairs.get("status_screen", "").lower()
    if not value:
        return None
    if value not in SCREENS:
        return "status_screen is not one of %s, keeping the current one" % ", ".join(SCREENS)
    path = os.path.join(etc, "status_screen")
    if value == "none":
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        return "status screen is off"
    _write(path, value + "\n", 0o644)
    return "status screen is %s" % value


def settings_payload(pairs):
    settings = {key: value for key, value in pairs.items() if value and key not in RESERVED
                and re.fullmatch(r"[a-z][a-z0-9_]*", key)}
    accounts = {key: pairs[key] for key in ACCOUNTS if pairs.get(key)}
    return {"settings": settings, "accounts": accounts}


def apply_settings(pairs, etc=ETC, user=None):
    """Hand the settings and upload keys to the app, which checks them as the service user at its next start."""
    payload = settings_payload(pairs)
    path = os.path.join(etc, "settings")
    if not payload["settings"] and not payload["accounts"]:
        return "no settings or upload keys in the file"
    owner = user or service_user()
    if owner is None:
        return "no service user to hand the settings to, skipping them"
    _write(path, json.dumps(payload), 0o600, owner)
    return "%d setting(s) and %d upload key(s) handed to the app" % (len(payload["settings"]), len(payload["accounts"]))


def _keyfile_text(ssid, password, hidden):
    ids = ";".join(str(byte) for byte in ssid.encode("utf-8")) + ";"
    lines = ["[connection]", "id=" + WIFI_NAME, "uuid=%s" % uuid.uuid5(uuid.NAMESPACE_DNS, WIFI_NAME),
             "type=wifi", "autoconnect=true", "", "[wifi]", "mode=infrastructure", "ssid=" + ids]
    if hidden:
        lines.append("hidden=true")
    if password:
        lines += ["", "[wifi-security]", "key-mgmt=wpa-psk", "psk=" + password.replace("\\", "\\\\")]
    lines += ["", "[ipv4]", "method=auto", "", "[ipv6]", "method=auto", ""]
    return "\n".join(lines)


def apply_wifi(pairs, nm_dir=NM_DIR, reload=True):
    ssid, password = pairs.get("wifi_ssid", ""), pairs.get("wifi_password", "")
    if not ssid:
        return None
    if "wifi_password" in pairs and not password:
        return None  # a blanked password line (scrub=yes) keeps the profile it made; omit the line for an open network
    if not 1 <= len(ssid.encode("utf-8")) <= 32 or any(ord(c) < 32 for c in ssid + password):
        return "wifi_ssid or wifi_password has an invalid character or length, keeping the current Wi-Fi"
    if password and not (8 <= len(password) <= 63 or re.fullmatch(r"[0-9A-Fa-f]{64}", password)):
        return "wifi_password must be 8 to 63 characters, keeping the current Wi-Fi"
    hidden = pairs.get("wifi_hidden", "").lower() in YES
    os.makedirs(nm_dir, exist_ok=True)
    _write(os.path.join(nm_dir, WIFI_NAME + ".nmconnection"), _keyfile_text(ssid, password, hidden), 0o600)
    if reload and shutil.which("nmcli"):
        subprocess.run(["nmcli", "connection", "reload"], check=False, capture_output=True, timeout=20)
    return "Wi-Fi profile %s is set for %r%s" % (WIFI_NAME, ssid, " (hidden)" if hidden else "")


def apply_country(pairs):
    code = pairs.get("country", "").upper()
    if not re.fullmatch(r"[A-Z]{2}", code) or not shutil.which("raspi-config"):
        return None
    subprocess.run(["raspi-config", "nonint", "do_wifi_country", code], check=False, capture_output=True, timeout=30)
    return "Wi-Fi country is %s" % code


def scrub(text):
    """The file with every secret value blanked. The keys stay, so the file still shows what to fill in."""
    out = []
    for raw in text.splitlines():
        match = re.fullmatch(r"(\s*)([A-Za-z][A-Za-z0-9_]*)(\s*[=:]\s*)(.*)", raw)
        if match and match.group(2).lower() in SECRETS and not raw.lstrip().startswith("#"):
            raw = match.group(1) + match.group(2) + match.group(3).rstrip()
        out.append(raw)
    return "\n".join(out) + ("\n" if text.endswith("\n") else "")


def apply(boot_file=BOOT_FILE, etc=ETC, nm_dir=NM_DIR, user=None, reload=True):
    """One line per thing done. Nothing fatal: a bad key is reported and the rest still applies."""
    try:
        with open(boot_file, "rb") as handle:
            text = handle.read(65536).decode("utf-8-sig", "replace")
    except FileNotFoundError:
        return []
    pairs = parse(text)
    messages = [apply_status_screen(pairs, etc), apply_settings(pairs, etc, user),
                apply_wifi(pairs, nm_dir, reload), apply_country(pairs)]
    if pairs.get("scrub", "").lower() in YES and any(pairs.get(key) for key in SECRETS):
        with open(boot_file, "w", encoding="utf-8", newline="") as handle:  # FAT: no chmod, no rename
            handle.write(scrub(text))
            handle.flush()
            os.fsync(handle.fileno())
        messages.append("secret values blanked in %s" % boot_file)
    return [line for line in messages if line]
