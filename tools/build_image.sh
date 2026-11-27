#!/usr/bin/env bash
# Build a ready-to-flash Raspberry Pi OS Lite (Trixie, armhf) SD card image with Quartermaster for one Waveshare HAT.
#
#   sudo tools/build_image.sh --hat st7735-128 --deb build/pi/quartermaster-pi_<version>_all.deb
#
# Options: --base <raspios .img.xz or .img> (default: download the latest Lite armhf image and check its sha256),
# --out <dir> (default build/images), --grow-mb <MiB added to the root partition, default 1536>.
# Needs root, util-linux, e2fsprogs, xz, curl, openssl, python3 and qemu-user-static registered for ARM binaries.
# The image logs in as pi with the password pi, has SSH on and is named quartermaster. Each device makes its own
# SSH host keys on its first start. A user set in Raspberry Pi Imager replaces the default login.
# Wi-Fi and Bluetooth start switched on. No Wi-Fi country is set: the worldwide rules apply until the user sets one.
# Writes quartermaster-pi-<version>-waveshare-<HAT name>.img.xz, its Imager metadata (.json) and SHA256SUMS to --out.
set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
base_url=${QM_BASE_URL:-https://downloads.raspberrypi.com/raspios_lite_armhf_latest}
hat='' deb='' base='' out=$here/build/images grow_mb=1536
service_id=59999
service_user=_quartermaster
groups_wanted=(video input render dialout gpio spi i2c bluetooth netdev plugdev audio quartermaster-radio)
units=(quartermaster-radio.socket quartermaster-boot-config.service "quartermaster@$service_user.service"
       quartermaster-first-login.service quartermaster-ssh-keys.service quartermaster-logs.path)
login_user=pi login_password=pi login_id=1000 host_name=quartermaster
login_groups=(adm dialout cdrom audio users sudo video games plugdev input gpio spi i2c netdev render
              "$service_user")
unneeded=(ModemManager.service triggerhappy.service triggerhappy.socket man-db.timer apt-daily.timer
          apt-daily-upgrade.timer e2scrub_all.timer e2scrub_reap.service)
kept=(NetworkManager.service bluetooth.service systemd-timesyncd.service rpi-resize.service
      regenerate_ssh_host_keys.service cloud-init-local.service cloud-init-main.service cloud-init-network.service
      cloud-config.service cloud-final.service)
data=/var/lib/quartermaster/quartermaster
log_folders=(logs recon aircraft)

die() { echo "build_image: $*" >&2; exit 1; }
log() { echo "==> $*"; }
usage() { sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//' >&2; exit 2; }

while [ $# -gt 0 ]; do
    case $1 in
        --hat) hat=${2:-}; shift 2 ;;
        --deb) deb=${2:-}; shift 2 ;;
        --base) base=${2:-}; shift 2 ;;
        --out) out=${2:-}; shift 2 ;;
        --grow-mb) grow_mb=${2:-}; shift 2 ;;
        -h|--help) usage ;;
        *) usage ;;
    esac
done

case $hat in st7735-128|st7789-240) ;; *) usage ;; esac
[ -f "$deb" ] || die "no package at '$deb'"
[[ $grow_mb =~ ^[0-9]+$ ]] || die "--grow-mb takes a whole number"
[ "$(id -u)" = 0 ] || die "run as root (loop devices, mounts and chroot)"
for tool in losetup sfdisk e2fsck resize2fs mount umount chroot xz sha256sum dpkg-deb curl python3 truncate openssl; do
    command -v "$tool" >/dev/null || die "missing $tool"
done
[ -e /proc/sys/fs/binfmt_misc/qemu-arm ] || die "ARM binaries cannot run here: install qemu-user-static and binfmt-support"
[ "$(dpkg-deb -f "$deb" Package)" = quartermaster-pi ] || die "$deb is not the quartermaster-pi package"
version=$(dpkg-deb -f "$deb" Version)
[[ $version =~ ^[0-9A-Za-z.+~-]+$ ]] || die "unexpected package version '$version'"
name=$(python3 "$here/tools/imager_os_list.py" --image-name "$hat" "$version")

mkdir -p "$out"
out=$(cd "$out" && pwd)
deb=$(cd "$(dirname "$deb")" && pwd)/$(basename "$deb")
work=$(mktemp -d "$out/.work-$hat.XXXXXX")
img=$work/$name.img
mnt=$work/root
mounts=()
loops=()

cleanup() {
    local rc=$? i
    set +e
    for ((i = ${#mounts[@]} - 1; i >= 0; i--)); do
        umount "${mounts[i]}" 2>/dev/null || umount -l "${mounts[i]}"
    done
    for i in "${loops[@]}"; do
        losetup -d "$i"
    done
    rm -rf "$work"
    exit "$rc"
}
trap cleanup EXIT
trap 'exit 130' INT TERM HUP

mount_at() {
    mount "$@"
    mounts+=("${!#}")
}

unmount_all() {
    local i
    sync
    for ((i = ${#mounts[@]} - 1; i >= 0; i--)); do
        umount "${mounts[i]}"
    done
    mounts=()
}

attach() {
    attached=$(losetup --find --show --offset "$(($1 * 512))" --sizelimit "$(($2 * 512))" "$img")
    loops+=("$attached")
}

in_chroot() {
    local env=(HOME=/root PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin LC_ALL=C.UTF-8
               DEBIAN_FRONTEND=noninteractive)
    local var
    for var in http_proxy https_proxy no_proxy; do
        [ -n "${!var:-}" ] && env+=("$var=${!var}")
    done
    chroot "$mnt" /usr/bin/env -i "${env[@]}" "$@"
}

partitions() {
    sfdisk -J "$img" | python3 -I -c '
import json, sys
table = json.load(sys.stdin)["partitiontable"]
assert table.get("sectorsize", 512) == 512, "unexpected sector size"
parts = table["partitions"]
assert len(parts) == 2, "expected a boot and a root partition"
print(parts[0]["start"], parts[0]["size"], parts[1]["start"], parts[1]["size"])'
}

fetch_base() {
    local url file fetch=(curl -fsSL --retry 3 --connect-timeout 30 --speed-limit 20000 --speed-time 60)
    url=$("${fetch[@]}" -I -o /dev/null -w '%{url_effective}' "$base_url")
    file=${url##*/}
    [[ $file =~ ^[0-9-]+raspios-trixie-armhf-lite\.img\.xz$ ]] || die "unexpected base image $url"
    log "Downloading $file"
    mkdir "$work/base"
    "${fetch[@]}" -o "$work/base/$file" "$url"
    "${fetch[@]}" -o "$work/base/$file.sha256" "$url.sha256"
    (cd "$work/base" && sha256sum --check --strict "$file.sha256")
    base=$work/base/$file
}

if [ -z "$base" ]; then
    fetch_base
else
    [ -f "$base" ] || die "no base image at '$base'"
    if [ -f "$base.sha256" ]; then
        (cd "$(dirname "$base")" && sha256sum --check --strict "$(basename "$base").sha256")
    fi
fi
base_name=$(basename "$base")

log "Unpacking $base_name"
case $base in
    *.xz) xz -dc "$base" > "$img" ;;
    *) cp --sparse=always "$base" "$img" ;;
esac

if [ "$grow_mb" -gt 0 ]; then
    log "Adding $grow_mb MiB to the root partition"
    truncate -s "+${grow_mb}M" "$img"
    echo ', +' | sfdisk --quiet --no-reread --no-tell-kernel -N 2 "$img"
fi
read -r boot_start boot_size root_start root_size < <(partitions)
attach "$boot_start" "$boot_size"
boot_dev=$attached
attach "$root_start" "$root_size"
root_dev=$attached
e2fsck -fy "$root_dev" >/dev/null || [ $? -le 1 ] || die "the root file system has errors"
resize2fs "$root_dev" >/dev/null

mkdir "$mnt"
mount_at "$root_dev" "$mnt"
[ -d "$mnt/boot/firmware" ] || die "no /boot/firmware in the base image"
mount_at "$boot_dev" "$mnt/boot/firmware"
config=$mnt/boot/firmware/config.txt
if [ ! -f "$config" ] || [ ! -f "$mnt/boot/firmware/cmdline.txt" ]; then
    die "the base image has no config.txt or cmdline.txt"
fi
mount_at -t proc proc "$mnt/proc"
mount_at -t sysfs sysfs "$mnt/sys"
mount_at --bind /dev "$mnt/dev"
mount_at --bind /dev/pts "$mnt/dev/pts"
mount_at -t tmpfs tmpfs "$mnt/run"
mount_at -t tmpfs tmpfs "$mnt/tmp"

log "Preparing the chroot"
keep=$work/keep
mkdir "$keep"
for path in etc/resolv.conf etc/ld.so.preload etc/machine-id; do
    if [ -e "$mnt/$path" ] || [ -L "$mnt/$path" ]; then
        mkdir -p "$keep/$(dirname "$path")"
        cp -a "$mnt/$path" "$keep/$path"
    fi
done
cloud_state_before=$([ -e "$mnt/var/lib/cloud" ] && echo yes || echo no)
seeds() {
    (cd "$mnt/boot/firmware" && { sha256sum cmdline.txt user-data network-config meta-data 2>/dev/null; true; })
}
seed_before=$(seeds)
mkdir "$keep/units"
for unit in "${kept[@]}"; do
    in_chroot systemctl is-enabled "$unit" > "$keep/units/$unit" 2>/dev/null || true
done
rm -f "$mnt/etc/resolv.conf" "$mnt/etc/ld.so.preload"
cp -L /etc/resolv.conf "$mnt/etc/resolv.conf"
[ ! -e "$mnt/usr/sbin/policy-rc.d" ] || die "the base image already has a policy-rc.d"
printf '#!/bin/sh\nexit 101\n' > "$mnt/usr/sbin/policy-rc.d"
chmod 755 "$mnt/usr/sbin/policy-rc.d"

log "Updating Raspberry Pi OS"
apt_options=(-y -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold)
in_chroot apt-get update
in_chroot apt-get "${apt_options[@]}" full-upgrade

log "Installing $(basename "$deb")"
cp "$deb" "$mnt/tmp/quartermaster-pi.deb"
in_chroot apt-get "${apt_options[@]}" --no-install-recommends install /tmp/quartermaster-pi.deb avahi-daemon
in_chroot apt-get -y autoremove --purge

log "Setting up the $service_user service account"
in_chroot getent passwd "$service_id" >/dev/null && die "uid $service_id is taken in the base image"
in_chroot getent group "$service_id" >/dev/null && die "gid $service_id is taken in the base image"
for taken in "$service_user" quartermaster; do
    if in_chroot getent passwd "$taken" >/dev/null || in_chroot getent group "$taken" >/dev/null; then
        die "the base image already has a user or group called $taken"
    fi
done
in_chroot groupadd --gid "$service_id" "$service_user"
in_chroot useradd --uid "$service_id" --gid "$service_user" --home-dir /var/lib/quartermaster --create-home \
    --shell /usr/sbin/nologin --comment Quartermaster "$service_user"
sed -i -E "s/^([[:space:]]*[UG]ID_MAX[[:space:]]+).*/\1$((service_id - 1))/" "$mnt/etc/login.defs"
if [ -f "$mnt/etc/adduser.conf" ]; then
    sed -i -E "s/^#?[[:space:]]*(LAST_[UG]ID)=.*/\1=$((service_id - 1))/" "$mnt/etc/adduser.conf"
fi
for group in "${groups_wanted[@]}"; do
    if in_chroot getent group "$group" >/dev/null; then
        in_chroot usermod -aG "$group" "$service_user"
    fi
done
chmod 750 "$mnt/var/lib/quartermaster"
install -d -m 750 -o "$service_id" -g "$service_id" "$mnt$data"
for folder in "${log_folders[@]}"; do
    install -d -m 750 -o "$service_id" -g "$service_id" "$mnt$data/$folder"
done

log "Setting up the $login_user login, SSH and the name $host_name"
first_user=$(in_chroot getent passwd "$login_id" | cut -d: -f1) || true
if [ -z "$first_user" ]; then
    in_chroot getent passwd "$login_user" >/dev/null && die "$login_user in the base image is not uid $login_id"
    in_chroot useradd --uid "$login_id" --user-group --create-home --shell /bin/bash "$login_user"
elif [ "$first_user" != "$login_user" ]; then
    die "the first user of the base image is $first_user, not $login_user"
fi
in_chroot usermod --shell /bin/bash "$login_user"
for group in "${login_groups[@]}"; do
    if in_chroot getent group "$group" >/dev/null; then
        in_chroot usermod -aG "$group" "$login_user"
    fi
done
login_hash=$(openssl passwd -6 "$login_password")
in_chroot usermod --password "$login_hash" "$login_user"
install -d -m 755 "$mnt/usr/local/lib/quartermaster-image"
(umask 077 && printf '%s\n' "$login_hash" > "$mnt/usr/local/lib/quartermaster-image/default-login")
login_home=$(in_chroot getent passwd "$login_user" | cut -d: -f6)
[ -d "$mnt$login_home" ] || die "$login_user has no home directory"
ln -sfn "$data" "$mnt$login_home/quartermaster"
in_chroot chown -h "$login_user:" "$login_home/quartermaster"
install -D -m 644 "$here/packaging/image/first_login.py" "$mnt/usr/local/lib/quartermaster-image/first_login.py"
for unit in quartermaster-first-login.service quartermaster-ssh-keys.service quartermaster-logs.path \
            quartermaster-logs-changed.service quartermaster-logs.service; do
    install -D -m 644 "$here/packaging/image/$unit" "$mnt/etc/systemd/system/$unit"
done
install -D -m 644 "$here/packaging/image/userconfig-needs-userconf.conf" \
    "$mnt/etc/systemd/system/userconfig.service.d/quartermaster.conf"
rm -f "$mnt/etc/ssh/sshd_config.d/rename_user.conf"
if [ -d "$mnt/etc/cloud/cloud.cfg.d" ]; then
    install -m 644 "$here/packaging/image/99_zz_quartermaster.cfg" "$mnt/etc/cloud/cloud.cfg.d/99_zz_quartermaster.cfg"
fi
install -D -m 755 "$here/packaging/image/default_password_notice.sh" "$mnt/etc/update-motd.d/20-quartermaster-password"
install -D -m 755 "$here/packaging/image/wifi_country_notice.sh" "$mnt/etc/update-motd.d/21-quartermaster-wifi-country"
in_chroot systemctl enable ssh.service avahi-daemon.service getty@tty1.service
printf '%s\n' "$host_name" > "$mnt/etc/hostname"
if grep -q '^127\.0\.1\.1[[:space:]]' "$mnt/etc/hosts"; then
    sed -i -E "s/^127\.0\.1\.1[[:space:]].*/127.0.1.1\t$host_name/" "$mnt/etc/hosts"
else
    printf '127.0.1.1\t%s\n' "$host_name" >> "$mnt/etc/hosts"
fi
for unit in "${unneeded[@]}"; do
    case $(in_chroot systemctl is-enabled "$unit" 2>/dev/null || true) in
        enabled|enabled-runtime) in_chroot systemctl disable "$unit" ;;
    esac
done
mkdir -p "$mnt/etc/systemd/system-preset"
printf 'disable %s\n' "${unneeded[@]}" > "$mnt/etc/systemd/system-preset/50-quartermaster.preset"

log "Configuring the $hat display and autostart"
printf '%s\n' "$hat" > "$mnt/etc/quartermaster/display"
sed "s/@HAT@/$hat/" "$here/packaging/image/quartermaster.txt" > "$mnt/boot/firmware/quartermaster.txt"
install -D -m 644 "$here/packaging/image/boot_config.py" "$mnt/usr/local/lib/quartermaster-image/boot_config.py"
install -D -m 644 "$here/packaging/image/boot_settings.py" "$mnt/usr/local/lib/quartermaster-image/boot_settings.py"
install -D -m 644 "$here/packaging/image/quartermaster-boot-config.service" \
    "$mnt/etc/systemd/system/quartermaster-boot-config.service"
install -D -m 644 "$here/packaging/image/50-quartermaster-wifi-scan.rules" \
    "$mnt/etc/polkit-1/rules.d/50-quartermaster-wifi-scan.rules"
in_chroot systemctl enable "${units[@]}"
printf 'VERSION=%s\nHAT=%s\nBASE_IMAGE=%s\nBUILT=%s\n' "$version" "$hat" "$base_name" "$(date -u +%Y-%m-%d)" \
    > "$mnt/etc/quartermaster/image-release"

log "Switching the radios on for the first start"
install -D -m 644 "$here/packaging/image/radios-on.conf" "$mnt/etc/modprobe.d/zz-quartermaster-radios.conf"
if [ -f "$mnt/var/lib/NetworkManager/NetworkManager.state" ]; then
    sed -i 's/^WirelessEnabled=.*/WirelessEnabled=true/' "$mnt/var/lib/NetworkManager/NetworkManager.state"
fi
for state in "$mnt"/var/lib/systemd/rfkill/*:wlan; do
    [ -e "$state" ] || continue
    echo 0 > "$state"
done

if grep -Eq '^[[:space:]]*dtparam=spi=on' "$config"; then
    :
elif grep -Eq '^[[:space:]]*#[[:space:]]*dtparam=spi=on' "$config"; then
    sed -i -E '0,/^[[:space:]]*#[[:space:]]*dtparam=spi=on/s//dtparam=spi=on/' "$config"
else
    printf '\n[all]\ndtparam=spi=on\n' >> "$config"
fi

log "Cleaning up"
in_chroot apt-get clean
rm -rf "$mnt"/var/lib/apt/lists/* "$mnt"/var/cache/apt/*.bin "$mnt/usr/sbin/policy-rc.d"
rm -f "$mnt/etc/resolv.conf" "$mnt/etc/machine-id"
for path in etc/resolv.conf etc/ld.so.preload etc/machine-id; do
    if [ -e "$keep/$path" ] || [ -L "$keep/$path" ]; then
        cp -a "$keep/$path" "$mnt/$path"
    fi
done
rm -f "$mnt"/etc/ssh/ssh_host_*
[ "$cloud_state_before" = yes ] || rm -rf "$mnt/var/lib/cloud"
find "$mnt/var/log" -type f \( -name '*.gz' -o -name '*.[0-9]' \) -delete

log "Checking the image"
check() {
    if [ "$2" = "$3" ]; then
        echo "ok    $1: $3"
    else
        die "$1: expected '$3', got '$2'"
    fi
}
# shellcheck disable=SC2016
check "package" "$(in_chroot dpkg-query -W -f '${Status} ${Version}' quartermaster-pi)" "install ok installed $version"
check "display" "$(cat "$mnt/etc/quartermaster/display")" "$hat"
check "boot file" "$(in_chroot python3 -I /usr/local/lib/quartermaster-image/boot_config.py)" "display is $hat"
check "SPI" "$(grep -Ec '^[[:space:]]*dtparam=spi=on' "$config")" 1
for unit in "${units[@]}"; do
    check "$unit" "$(in_chroot systemctl is-enabled "$unit" 2>/dev/null || true)" enabled
done
check "app import as $service_user" "$(in_chroot runuser -u "$service_user" -- /bin/sh -c \
    'cd /usr/lib/quartermaster/app && exec python3 -c "import quartermaster; print(quartermaster.__version__)"')" \
    "$version"
check "the screen and button modules load as $service_user" "$(in_chroot runuser -u "$service_user" -- python3 -c '
import importlib.util
from PIL import Image
from spidev import SpiDev
from luma.core.interface.serial import spi
from luma.lcd.device import st7735, st7789
from gpiozero import Button
print("ok" if importlib.util.find_spec("RPi.GPIO") else "RPi.GPIO is missing")' 2>&1 | tail -n 1)" ok
check "$service_user uid and group" "$(in_chroot id -u "$service_user"):$(in_chroot id -gn "$service_user")" \
    "$service_id:$service_user"
check "the name quartermaster is free for a login" \
    "$(in_chroot getent passwd quartermaster; in_chroot getent group quartermaster)" ""
check "the logs rule runs as $service_user" "$(grep '^User=' "$mnt/etc/systemd/system/quartermaster-logs.service")" \
    "User=$service_user"
check "the Wi-Fi scan rule is for $service_user" "$(grep -o 'subject\.user == "[^"]*"' \
    "$mnt/etc/polkit-1/rules.d/50-quartermaster-wifi-scan.rules")" "subject.user == \"$service_user\""
check "the boot file is read before the app starts" "$(grep '^Before=' \
    "$mnt/etc/systemd/system/quartermaster-boot-config.service")" "Before=quartermaster@$service_user.service"
check "radio helper accepts $service_user" "$(in_chroot python3 -I -c '
import pwd, sys
sys.path.insert(0, "/usr/lib/quartermaster")
import radiod
user = pwd.getpwnam(sys.argv[1])
print(radiod.peer_allowed(user.pw_uid, user.pw_gid))' "$service_user")" True
probe=$(in_chroot sh -c 'useradd -M -N -s /usr/sbin/nologin qm-probe && id -u qm-probe; userdel qm-probe' 2>/dev/null) || true
check "next login user below the service uid" "$([ "${probe:-0}" -ge 1000 ] && [ "$probe" -lt "$service_id" ] && echo yes || echo "no ($probe)")" yes
check "Wi-Fi rfkill saved state" "$(cat "$mnt"/var/lib/systemd/rfkill/*:wlan 2>/dev/null | grep -vx 0 | xargs)" ""
kernels=0
for index in "$mnt"/lib/modules/*/modules.dep; do
    [ -e "$index" ] || continue
    kernel=$(basename "$(dirname "$index")")
    kernels=$((kernels + 1))
    check "radios start switched on with $kernel" "$(in_chroot modprobe -S "$kernel" --show-depends rfkill 2>&1 |
        grep -o 'default_state=[0-9]*' | tail -n 1)" default_state=1
done
check "a kernel was checked for the radio switch" "$([ "$kernels" -gt 0 ] && echo yes || echo no)" yes
check "NetworkManager has Wi-Fi switched on" "$(grep -s '^WirelessEnabled=' \
    "$mnt/var/lib/NetworkManager/NetworkManager.state" | grep -v '=true$' | tr '\n' ' ')" ""
check "the image sets no Wi-Fi country" "$(grep -c 'cfg80211\.ieee80211_regdom=' "$mnt/boot/firmware/cmdline.txt")" 0
member_of=" $(in_chroot id -nG "$service_user") "
for group in video gpio spi dialout netdev bluetooth quartermaster-radio; do
    if in_chroot getent group "$group" >/dev/null; then
        check "$service_user in $group" "$([[ $member_of == *" $group "* ]] && echo yes || echo no)" yes
    fi
done
enabled() {
    in_chroot systemctl is-enabled "$1" 2>/dev/null || true
}
shadow_of() {
    awk -F: -v user="$1" '$1 == user {print $2}' "$mnt/etc/shadow"
}
check "accounts with a password" "$(awk -F: '$2 !~ /^[!*]/ {print $1}' "$mnt/etc/shadow" | xargs)" "$login_user"
stored=$(shadow_of "$login_user")
salt=$(cut -d'$' -f3 <<< "$stored")
check "$login_user password" "$([ "$(openssl passwd -6 -salt "$salt" "$login_password")" = "$stored" ] && echo set || echo wrong)" set
check "default login record" "$([ "$(cat "$mnt/usr/local/lib/quartermaster-image/default-login")" = "$stored" ] && echo same || echo differs)" same
check "default login record mode" "$(stat -c %a:%u "$mnt/usr/local/lib/quartermaster-image/default-login")" 600:0
check "$login_user uid and shell" "$(in_chroot getent passwd "$login_user" | cut -d: -f3,7)" "$login_id:/bin/bash"
login_member_of=" $(in_chroot id -nG "$login_user") "
for group in sudo "$service_user"; do
    check "$login_user in $group" "$([[ $login_member_of == *" $group "* ]] && echo yes || echo no)" yes
done
check "root password locked" "$(shadow_of root | cut -c1 | tr '*' '!')" '!'
check "$service_user password locked" "$(shadow_of "$service_user" | cut -c1 | tr '*' '!')" '!'
check "$service_user shell" "$(in_chroot getent passwd "$service_user" | cut -d: -f7)" /usr/sbin/nologin
check "logs link" "$(readlink "$mnt$login_home/quartermaster")" "$data"
for folder in "${log_folders[@]}"; do
    check "$login_user reads $folder" \
        "$(in_chroot runuser -u "$login_user" -- ls "$login_home/quartermaster/$folder" 2>&1)" ""
    in_chroot runuser -u "$service_user" -- sh -c "umask 077 && : > $data/$folder/probe.log"
done
share=$(sed -n '/^ExecStart=-/{s///p;q}' "$mnt/etc/systemd/system/quartermaster-logs.service")
# shellcheck disable=SC2086
in_chroot runuser -u "$service_user" -- $share
for folder in "${log_folders[@]}"; do
    check "$login_user reads a newly saved log in $folder" "$(in_chroot runuser -u "$login_user" -- \
        cat "$login_home/quartermaster/$folder/probe.log" 2>&1)" ""
    rm -f "$mnt$data/$folder/probe.log"
done
check "first start keeps the default login" "$(in_chroot python3 -I -c '
import sys
sys.path.insert(0, "/usr/local/lib/quartermaster-image")
import first_login
print(first_login.plan(*first_login.gather()))')" "[]"
check "ssh.service" "$(enabled ssh.service)" enabled
check "ssh.socket off" "$([ "$(enabled ssh.socket)" = enabled ] && echo enabled || echo off)" off
check "ssh password login" "$(in_chroot sh -c 'mkdir -p /run/sshd && ssh-keygen -q -t ed25519 -N "" -f /tmp/probe_key &&
    /usr/sbin/sshd -T -h /tmp/probe_key' | grep -x 'passwordauthentication yes')" "passwordauthentication yes"
rm -f "$mnt"/tmp/probe_key*
check "default password notice" "$(in_chroot /etc/update-motd.d/20-quartermaster-password | xargs)" \
    "The login $login_user still has the default password. Type passwd to set your own."
check "the notice is shown at login" "$(grep -c '^session.*pam_motd\.so.*motd=/run/motd\.dynamic' "$mnt/etc/pam.d/sshd")" 1
check "Wi-Fi country hint without a country" "$(in_chroot /etc/update-motd.d/21-quartermaster-wifi-country \
    'country 00: DFS-UNSET' | grep -c 'sudo raspi-config nonint do_wifi_country US')" 1
check "Wi-Fi country hint with a country" "$(in_chroot /etc/update-motd.d/21-quartermaster-wifi-country \
    'country US: DFS-FCC' | tr '\n' ' ')" ""
check "raspi-config sets the Wi-Fi country" "$(grep -c '^do_wifi_country()' "$mnt/usr/bin/raspi-config")" 1
check "ssh host keys in the image" "$(find "$mnt/etc/ssh" -name 'ssh_host_*' -printf '%f ')" ""
check "machine-id as in the base image" "$(cat "$mnt/etc/machine-id" 2>/dev/null)" "$(cat "$keep/etc/machine-id" 2>/dev/null)"
check "first-start prompt only with userconf.txt" \
    "$(grep -c '^ConditionPathExists=|/boot/firmware/userconf' "$mnt/etc/systemd/system/userconfig.service.d/quartermaster.conf")" 2
check "getty@tty1.service" "$(enabled getty@tty1.service)" enabled
check "ssh rename banner" "$(find "$mnt/etc/ssh/sshd_config.d" -name rename_user.conf -printf '%f' 2>/dev/null)" ""
check "hostname" "$(cat "$mnt/etc/hostname")" "$host_name"
check "hostname in hosts" "$(grep -c "^127\.0\.1\.1[[:space:]]$host_name\$" "$mnt/etc/hosts")" 1
check "avahi-daemon.service" "$(enabled avahi-daemon.service)" enabled
for unit in "${unneeded[@]}"; do
    check "$unit off" "$([ "$(enabled "$unit")" = enabled ] && echo enabled || echo off)" off
    check "$unit stays off" "$(grep -cx "disable $unit" "$mnt/etc/systemd/system-preset/50-quartermaster.preset")" 1
done
for unit in "${kept[@]}"; do
    if [ "$(cat "$keep/units/$unit")" = enabled ]; then
        check "$unit still on" "$(enabled "$unit")" enabled
    fi
done
if grep -qw resize "$mnt/boot/firmware/cmdline.txt"; then
    check "root file system grows on first start" "$(enabled rpi-resize.service)" enabled
    check "host keys are made on first start" "$(enabled regenerate_ssh_host_keys.service)" enabled
    check "first start marker" "$(cat "$mnt/etc/machine-id")" uninitialized
fi
init_format=systemd
if [ -x "$mnt/usr/bin/cloud-init" ]; then
    init_format=cloudinit-rpi
    last_cfg=$(find "$mnt/etc/cloud/cloud.cfg.d" -maxdepth 1 -name '*.cfg' -printf '%f\n' | LC_ALL=C sort | tail -n 1)
    check "our cloud-init settings are read last" "$last_cfg" 99_zz_quartermaster.cfg
    check "cloud-init keeps SSH password logins" "$(grep -rhiE '^[[:space:]]*ssh_pwauth:[[:space:]]*(false|no|0)' \
        "$mnt/etc/cloud/cloud.cfg" "$mnt/etc/cloud/cloud.cfg.d" "$mnt/boot/firmware/user-data" 2>/dev/null | tr '\n' ' ')" ""
    check "the cloud-init seed sets no hostname" "$(cd "$mnt/boot/firmware" &&
        grep -hE '^[[:space:]]*(hostname|fqdn|local-hostname):' user-data meta-data 2>/dev/null | tr '\n' ' ')" ""
fi
check "cmdline.txt and cloud-init seed" "$(seeds)" "$seed_before"
order=$(in_chroot systemd-analyze verify --man=no cloud-init.target default.target 2>&1) && order_status=0 || order_status=$?
check "start order was examined" "$([ "$order_status" -le 1 ] && ! grep -qi 'initialize manager' <<< "$order" && echo yes ||
    echo "no: $(head -n 3 <<< "$order" | tr '\n' ' ')")" yes
check "start order has no cycle" "$(grep -i -A 3 'ordering cycle' <<< "$order" | tr '\n' ' ')" ""
check "systemd accepts the Quartermaster units" "$(grep -i quartermaster <<< "$order" | tr '\n' ' ')" ""
missing=''
while read -r program; do
    in_chroot test -x "$program" < /dev/null || missing+=" $program"
done < <(sed -n 's/^Exec[A-Za-z]*=[-@:+!]*\([^ ]*\).*/\1/p' "$mnt"/etc/systemd/system/quartermaster*.service \
    "$mnt"/usr/lib/systemd/system/quartermaster*.service | sort -u)
check "every program the Quartermaster units start is in the image" "${missing# }" ""
echo "ok    Imager customisation: $init_format"
echo "ok    root file system: $(df -BM --output=used,avail "$mnt" | tail -n 1 | xargs | tr ' ' '/') used/free"

fstrim "$mnt" 2>/dev/null || true
unmount_all
e2fsck -fy "$root_dev" >/dev/null || [ $? -le 1 ] || die "the root file system has errors"
for loop in "${loops[@]}"; do
    losetup -d "$loop"
done
loops=()

log "Compressing $name.img.xz"
extract_size=$(stat -c %s "$img")
extract_sha256=$(sha256sum "$img" | cut -d' ' -f1)
xz -T0 -6 -c "$img" > "$work/$name.img.xz"
rm -f "$img"
download_size=$(stat -c %s "$work/$name.img.xz")
download_sha256=$(sha256sum "$work/$name.img.xz" | cut -d' ' -f1)
python3 -I -c '
import json, sys
keys = ("name", "version", "hat", "base_image", "init_format", "extract_size", "extract_sha256",
        "image_download_size", "image_download_sha256", "release_date")
meta = dict(zip(keys, sys.argv[1:]))
for key in ("extract_size", "image_download_size"):
    meta[key] = int(meta[key])
meta["file"] = meta["name"] + ".img.xz"
print(json.dumps(meta, indent=2))' "$name" "$version" "$hat" "$base_name" "$init_format" "$extract_size" \
    "$extract_sha256" "$download_size" "$download_sha256" "$(date -u +%Y-%m-%d)" > "$work/$name.json"
mv "$work/$name.img.xz" "$work/$name.json" "$out/"
(cd "$out" && sha256sum -- *.img.xz > SHA256SUMS)
log "Wrote $out/$name.img.xz ($((download_size / 1048576)) MiB, $((extract_size / 1048576)) MiB unpacked)"
