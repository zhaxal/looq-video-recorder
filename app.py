"""Looq Video Recorder - headless Raspberry Pi camera web app.

Run:  python app.py
Then point a browser at the Pi's hotspot IP (default http://10.42.0.1:8000).
"""

import os
import mimetypes
from datetime import datetime

from flask import (
    Flask,
    Response,
    abort,
    jsonify,
    render_template,
    request,
    send_from_directory,
)

import hotspot
from analytics import AnalyticsEngine, export_csv
from attention_sync import attention_sync
from camera_manager import (
    CameraManager,
    PHOTO_DIR,
    VIDEO_DIR,
    RESOLUTIONS,
)

app = Flask(__name__)
camera = CameraManager()
analytics = AnalyticsEngine(camera, auto_start=True)
attention_sync.start()


# ---------------------------------------------------------------------- #
# Pages
# ---------------------------------------------------------------------- #
@app.route("/")
def index():
    return render_template(
        "index.html",
        resolutions=list(RESOLUTIONS.keys()),
    )


# ---------------------------------------------------------------------- #
# Live preview
# ---------------------------------------------------------------------- #
@app.route("/stream")
def stream():
    return Response(
        camera.frames(),
        mimetype="multipart/x-mixed-replace; boundary=frame",
    )


# ---------------------------------------------------------------------- #
# Capture controls
# ---------------------------------------------------------------------- #
@app.route("/api/status")
def api_status():
    return jsonify(camera.status())


@app.route("/api/photo", methods=["POST"])
def api_photo():
    try:
        name = camera.capture_photo()
        return jsonify({"ok": True, "file": name})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.route("/api/record/start", methods=["POST"])
def api_record_start():
    try:
        return jsonify({"ok": True, **camera.start_recording()})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.route("/api/record/stop", methods=["POST"])
def api_record_stop():
    try:
        return jsonify({"ok": True, **camera.stop_recording()})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.route("/api/settings", methods=["POST"])
def api_settings():
    data = request.get_json(force=True, silent=True) or {}
    try:
        settings = camera.update_settings(data)
        return jsonify({"ok": True, "settings": settings})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.route("/api/camera/reconnect", methods=["POST"])
def api_reconnect():
    ok = camera.reinitialise()
    return jsonify({"ok": ok, **camera.status()})


# ---------------------------------------------------------------------- #
# Hotspot (WiFi access point)
# ---------------------------------------------------------------------- #
@app.route("/api/hotspot")
def api_hotspot_status():
    return jsonify(hotspot.status())


@app.route("/api/hotspot", methods=["POST"])
def api_hotspot_set():
    data = request.get_json(force=True, silent=True) or {}
    try:
        result = hotspot.set_enabled(bool(data.get("enabled")))
        return jsonify({"ok": True, **result})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


# ---------------------------------------------------------------------- #
# People analytics (Hailo face detection)
# ---------------------------------------------------------------------- #
@app.route("/api/analytics")
def api_analytics():
    return jsonify(analytics.snapshot())


@app.route("/api/analytics", methods=["POST"])
def api_analytics_set():
    data = request.get_json(force=True, silent=True) or {}
    if data.get("reset"):
        return jsonify({"ok": True, **analytics.reset_session()})
    if data.get("calibrate"):
        seconds = float(data.get("seconds", 5.0))
        return jsonify({"ok": True, **analytics.calibrate(seconds)})
    if any(k in data for k in ("yaw_offset", "pitch_offset", "yaw_tol", "pitch_tol")):
        return jsonify({"ok": True, **analytics.update_attention_settings(data)})
    if "enabled" in data:
        return jsonify({"ok": True, **analytics.set_enabled(data["enabled"])})
    return jsonify({"ok": True, **analytics.snapshot()})


@app.route("/api/attention-sync")
def api_attention_sync():
    return jsonify(attention_sync.status())


@app.route("/api/analytics/log.csv")
def api_analytics_log():
    day = request.args.get("date") or datetime.now().strftime("%Y-%m-%d")
    csv_text = export_csv(day)
    return Response(
        csv_text,
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="analytics_{day}.csv"'},
    )


# ---------------------------------------------------------------------- #
# Gallery
# ---------------------------------------------------------------------- #
def _listing(directory, kind):
    items = []
    if os.path.isdir(directory):
        for fn in os.listdir(directory):
            full = os.path.join(directory, fn)
            if os.path.isfile(full):
                items.append(
                    {
                        "name": fn,
                        "kind": kind,
                        "size": os.path.getsize(full),
                        "mtime": os.path.getmtime(full),
                    }
                )
    return items


@app.route("/api/media")
def api_media():
    items = _listing(PHOTO_DIR, "photo") + _listing(VIDEO_DIR, "video")
    items.sort(key=lambda x: x["mtime"], reverse=True)
    return jsonify(items)


def _safe_dir(kind):
    return PHOTO_DIR if kind == "photo" else VIDEO_DIR


@app.route("/media/<kind>/<path:filename>")
def media_file(kind, filename):
    if kind not in ("photo", "video"):
        abort(404)
    if "/" in filename or "\\" in filename or filename.startswith("."):
        abort(404)
    directory = _safe_dir(kind)
    if not os.path.isfile(os.path.join(directory, filename)):
        abort(404)
    download = request.args.get("download") == "1"
    return send_from_directory(directory, filename, as_attachment=download)


@app.route("/api/media/<kind>/<path:filename>", methods=["DELETE"])
def media_delete(kind, filename):
    if kind not in ("photo", "video"):
        abort(404)
    if "/" in filename or "\\" in filename or filename.startswith("."):
        abort(404)
    path = os.path.join(_safe_dir(kind), filename)
    if not os.path.isfile(path):
        return jsonify({"ok": False, "error": "not found"}), 404
    os.remove(path)
    return jsonify({"ok": True})


if __name__ == "__main__":
    mimetypes.add_type("video/mp4", ".mp4")
    app.run(host="0.0.0.0", port=8000, threaded=True)
