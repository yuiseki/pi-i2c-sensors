#!/usr/bin/env bash
# Install the pi-i2c-sensors tools into /usr/local/bin and grant I2C access.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
dest="/usr/local/bin"

echo "Installing tools to $dest ..."
for f in "$here"/bin/*; do
    name="$(basename "$f")"
    sudo install -m 0755 "$f" "$dest/$name"
    echo "  $name"
done

# Ensure i2c-dev is available and the invoking user can use it without sudo.
user="${SUDO_USER:-$USER}"
if getent group i2c >/dev/null 2>&1; then
    if ! id -nG "$user" | tr ' ' '\n' | grep -qx i2c; then
        echo "Adding $user to the i2c group (log out/in for it to take effect) ..."
        sudo usermod -aG i2c "$user"
    fi
fi

echo
echo "Done. Quick check:"
echo "  i2cdetect -y 1        # list devices (need i2c-tools: sudo apt install i2c-tools)"
echo "  pi-calib-mag          # live field strength, find a magnet-free spot"
echo "  pi-calib              # tumble 30s -> ~/tmp/pi-calib.json"
echo "  pi-orient             # publish orientation to /dev/shm/pi-orientation"
echo "  pi-set-north / -south # set the bearing reference at the current facing"
