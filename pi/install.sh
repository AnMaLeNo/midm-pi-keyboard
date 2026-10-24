#!/usr/bin/env bash
# Install btkbd on a Raspberry Pi running Raspberry Pi OS (Debian 12, BlueZ 5.66). Run as root: sudo ./install.sh
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"

[ "$(id -u)" -eq 0 ] || { echo "run as root (sudo)" >&2; exit 1; }

missing=()
for m in dbus gi evdev; do python3 -c "import $m" 2>/dev/null || missing+=("$m"); done
if [ "${#missing[@]}" -gt 0 ]; then
    echo "missing python modules: ${missing[*]}  ->  apt install python3-dbus python3-gi python3-evdev" >&2
    exit 1
fi

install -d /opt/btkbd
install -m 755 "$here/btkbd.py"        /opt/btkbd/btkbd.py
install -m 755 "$here/btkbd-send"      /usr/local/bin/btkbd-send
install -m 644 "$here/btkbd.service"   /etc/systemd/system/btkbd.service

# BlueZ's own `input` plugin owns L2CAP PSM 17/19 (HID host). A keyboard *device* needs them free.
install -d /etc/systemd/system/bluetooth.service.d
install -m 644 "$here/bluetooth-noinput.conf" /etc/systemd/system/bluetooth.service.d/btkbd.conf

# Advertise the device class "Peripheral / Keyboard" (0x000540). Keep a copy of the original once.
cp -n /etc/bluetooth/main.conf /etc/bluetooth/main.conf.orig
if ! grep -qE '^Class\s*=' /etc/bluetooth/main.conf; then
    sed -i '0,/^\[General\]/s//[General]\nClass = 0x000540/' /etc/bluetooth/main.conf
fi

systemctl daemon-reload
systemctl restart bluetooth
systemctl enable --now btkbd
echo "installed. Next: sudo btkbd-send pairing on   (then pair from the PC), see README.md"
