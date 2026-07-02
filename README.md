# Looq Video Recorder

A headless Raspberry Pi camera recorder. The Pi broadcasts its own WiFi
hotspot; you connect a phone or laptop and use a clean web interface to:

- **Live preview** (low-latency MJPEG stream)
- **Take photos** (full-sensor resolution when not recording)
- **Record video** (H.264 → MP4, with a recording timer)
- **Browse / download / delete** captured media in a gallery
- **Adjust settings**: resolution, frame rate, autofocus, rotation, flips
- **Toggle the WiFi hotspot** on/off from Settings (when configured)
- **Ad-attention analytics** (Hailo-8L): tracks people live, estimates head
  pose from face landmarks, and measures who *looks at the ad* and for how
  long — with a color-coded overlay on the preview (green = looking) and an
  hourly-rollup CSV log

Built for a Raspberry Pi (tested on Pi 5 / Camera Module 3, Pi OS Trixie)
with `Picamera2` + `Flask` + `NetworkManager`.

---

## Quick start

```bash
cd ~/projects/looq-video-recorder

# App + auto-start on boot + WiFi hotspot, all in one:
chmod +x install.sh scripts/*.sh
./install.sh --hotspot
```

Then:

1. On your phone/laptop, connect to WiFi **`LooqCamera`** (password `looqcamera`).
2. Open **http://10.42.0.1:8000**

That's it.

> Change the SSID/password by editing the top of
> `scripts/setup_hotspot.sh` before running, or re-run that script anytime.

---

## What gets installed

| Step | What it does |
|------|--------------|
| `scripts/setup_venv.sh` | Creates `venv/` (with `--system-site-packages` so it can see the apt-installed `picamera2`) and installs Flask. |
| `scripts/looq-recorder.service` | systemd unit so the app starts on every boot and restarts on failure. |
| `scripts/setup_hotspot.sh` | NetworkManager access point (`ipv4.method shared` = built-in DHCP + NAT, gateway `10.42.0.1`). |

## Manual run (no service)

```bash
./venv/bin/python app.py
# serves on http://0.0.0.0:8000
```

## Useful commands

```bash
sudo systemctl status looq-recorder      # is it running?
sudo journalctl -u looq-recorder -f      # live logs
sudo systemctl restart looq-recorder     # restart after code changes
nmcli con up looq-hotspot                # bring the hotspot up manually
```

---

## Camera not detected?

If the UI shows **"Camera offline / No camera detected"**:

1. Power off the Pi completely.
2. Re-seat the Camera Module 3 ribbon cable at **both** ends — the silver/
   gold contacts must face the right way (toward the cable's contact side on
   the camera, and per the connector on the Pi).
3. Power on and check:
   ```bash
   rpicam-hello --list-cameras
   ```
   You should see `imx708`. Then hit **Reconnect** in the web UI (or restart
   the service).

---

## Media storage

Captured files live in `media/photos/` and `media/videos/` inside the project
folder. Download them from the gallery, or copy off over SSH/`scp`.

## Notes & limits

- Preview is always 720p for smooth streaming; recording uses the resolution
  you pick in Settings (up to 1080p by default — add larger sizes in
  `RESOLUTIONS` in `camera_manager.py`).
- 90°/270° rotation is applied in the browser (CSS); 180° and flips are done
  on the sensor. The analytics overlay aligns at 0°/180° (it reads the preview
  frame); at 90°/270° the boxes are not rotated to match.
- **Ad-attention analytics** needs the Hailo runtime (`sudo apt install
  hailo-all`, which the venv sees via `--system-site-packages`) and the SCRFD
  model at `/usr/share/hailo-models/scrfd_2.5g_h8l.hef` (override with
  `LOOQ_FACE_HEF`). Detection runs on the preview frames, so it works during
  recording too. It is face-based: people facing fully away aren't counted,
  and someone who leaves and returns is counted as a new visitor (IOU
  tracking, not re-identification — no embeddings or images are ever stored).

  **How "looking at the ad" works.** SCRFD's 5-point landmarks feed a
  `cv2.solvePnP` on the CPU to get yaw/pitch/roll per face — no second Hailo
  model is needed (the original design considered a 3DMM/6DRepNet head-pose
  net, but a landmark-based PnP is just as good at this range and costs
  microseconds instead of another inference pass). The camera is assumed to
  sit at/near the ad; whatever small residual angle separates "looking at the
  camera" from "looking at the ad" (mounting offset, ad slightly to one side,
  etc.) is measured on-site instead of computed by hand: tap **🎯 Calibrate**
  in Settings, stand where a viewer normally would, and look straight at the
  ad for the 5-second countdown. The median yaw/pitch sampled from the
  largest face during that window becomes the new offset. A per-track
  debounce (0.2s) and a minimum accumulated look time (1.0s) suppress
  single-frame pose noise before anything counts as a "view".

  **Storage.** Results are written to a SQLite database
  (`media/analytics/analytics.db`, WAL mode): a `tracks` table with one row
  per finalized visitor (presence time, attention time, viewed flag, longest
  look streak — no images, embeddings, or per-frame positions) and an
  `hourly` rollup table recomputed from it. Raw track rows older than 7 days
  are pruned automatically on startup; the rollup is kept indefinitely. The
  **⬇ CSV log** button in Settings downloads the hourly rollup for a given day.

  **Performance.** Inference is capped at 10 fps — dwell-time resolution at
  that rate is far below the noise floor of the pose estimate, and halving
  the duty cycle vs. running flat out meaningfully helps power draw on
  battery deployments.

  **A field-deployment note:** even with aggregate-only storage, camera-based
  audience measurement in public/semi-public space usually calls for visible
  signage (and in some jurisdictions is a legal requirement) — a small
  "camera in use for anonymous audience counting; no video stored" notice
  near the ad is cheap insurance.
- The hotspot is isolated (no internet). To also give the Pi internet, connect
  it to your home WiFi on a second band or via Ethernet; the hotspot keeps
  running on `wlan0`.
