#!/usr/bin/env bash
# Configure the Raspberry Pi to broadcast its own always-on WiFi access point
# using NetworkManager (the default on Pi OS Bookworm/Trixie).
#
# After running, connect a phone/laptop to the SSID below, then browse to:
#     http://10.42.0.1:8000
#
# Edit these three values to taste:
SSID="LooqCamera"
PASSWORD="looqcamera"      # must be at least 8 characters
COUNTRY="US"               # set your 2-letter WiFi regulatory country code

set -euo pipefail

if [[ ${#PASSWORD} -lt 8 ]]; then
  echo "ERROR: WiFi password must be at least 8 characters." >&2
  exit 1
fi

CON="looq-hotspot"
IFACE="wlan0"

echo "[hotspot] Setting WiFi country to $COUNTRY..."
sudo raspi-config nonint do_wifi_country "$COUNTRY" 2>/dev/null || \
  sudo iw reg set "$COUNTRY" 2>/dev/null || true

# Remove any previous hotspot connection so this script is re-runnable.
sudo nmcli con delete "$CON" 2>/dev/null || true

echo "[hotspot] Creating access point '$SSID'..."
sudo nmcli con add type wifi ifname "$IFACE" mode ap con-name "$CON" ssid "$SSID"
sudo nmcli con modify "$CON" 802-11-wireless.band bg
sudo nmcli con modify "$CON" 802-11-wireless.channel 6
sudo nmcli con modify "$CON" 802-11-wireless-security.key-mgmt wpa-psk
sudo nmcli con modify "$CON" 802-11-wireless-security.proto rsn
sudo nmcli con modify "$CON" 802-11-wireless-security.group ccmp
sudo nmcli con modify "$CON" 802-11-wireless-security.pairwise ccmp
sudo nmcli con modify "$CON" 802-11-wireless-security.psk "$PASSWORD"
sudo nmcli con modify "$CON" ipv4.method shared          # built-in DHCP + NAT
sudo nmcli con modify "$CON" connection.autoconnect yes
sudo nmcli con modify "$CON" connection.autoconnect-priority 100

echo "[hotspot] Bringing up the hotspot..."
sudo nmcli con up "$CON"

echo
echo "[hotspot] Done."
echo "  SSID:     $SSID"
echo "  Password: $PASSWORD"
echo "  Web app:  http://10.42.0.1:8000"
