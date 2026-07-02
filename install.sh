#!/usr/bin/env bash
# One-shot installer for the Looq Video Recorder.
#   1. creates the Python venv + installs Flask
#   2. installs & starts the systemd service (auto-start on boot)
#   3. (optional) configures the WiFi hotspot
#
# Usage:
#   ./install.sh            # app + auto-start service
#   ./install.sh --hotspot  # also set up the WiFi access point
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_USER="$(id -un)"
cd "$PROJECT_DIR"

echo "=== 1/3  Python environment ==="
bash scripts/setup_venv.sh

echo "=== 2/3  systemd service (auto-start on boot) ==="
SERVICE_TMP="$(mktemp)"
sed -e "s#__USER__#${RUN_USER}#g" \
    -e "s#__PROJECT_DIR__#${PROJECT_DIR}#g" \
    scripts/looq-recorder.service > "$SERVICE_TMP"
sudo cp "$SERVICE_TMP" /etc/systemd/system/looq-recorder.service
rm -f "$SERVICE_TMP"
sudo systemctl daemon-reload
sudo systemctl enable looq-recorder.service
sudo systemctl restart looq-recorder.service
echo "[install] Service status:"
sudo systemctl --no-pager --lines=0 status looq-recorder.service || true

if [[ "${1:-}" == "--hotspot" ]]; then
  echo "=== 3/3  WiFi hotspot ==="
  bash scripts/setup_hotspot.sh
else
  echo "=== 3/3  WiFi hotspot SKIPPED (run: ./install.sh --hotspot) ==="
fi

echo
echo "Done!  Once the hotspot is up, connect to it and open:"
echo "    http://10.42.0.1:8000"
echo "On your home network you can also reach it at http://<pi-ip>:8000"
