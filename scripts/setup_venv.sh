#!/usr/bin/env bash
# Create the Python virtualenv with access to the system picamera2/libcamera
# packages, then install Flask into it.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

echo "[setup] Ensuring system camera packages are present..."
if ! python3 -c "import picamera2" 2>/dev/null; then
  echo "[setup] Installing python3-picamera2 (needs sudo)..."
  sudo apt-get update
  sudo apt-get install -y python3-picamera2 ffmpeg
fi

echo "[setup] Creating virtualenv (--system-site-packages)..."
python3 -m venv --system-site-packages venv

echo "[setup] Installing Python requirements..."
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt

echo "[setup] Done. Run with: ./venv/bin/python app.py"
