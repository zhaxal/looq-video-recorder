"""Ad-attention people analytics for the Looq Video Recorder.

Runs SCRFD face detection on the Hailo-8L accelerator over the live preview
frames and derives privacy-friendly "looking at the ad" metrics:

  * live count     -- people visible right now
  * looking now    -- of those, how many are currently looking at the ad
  * unique total   -- distinct people seen this session (IOU tracking)
  * viewed total   -- distinct people whose look accumulated >= MIN_ATTENTION_S
  * dwell / attention time per tracked person

Design notes
------------
* Frames are read from the camera's existing MJPEG preview output, so we never
  touch Picamera2 from a second thread (no contention with recording).
* The Hailo device is separate from the Pi's H.264 encoder, so analytics and
  video recording run happily at the same time.
* Head pose (yaw/pitch/roll) is derived on the CPU with cv2.solvePnP from
  SCRFD's built-in 5-point landmarks -- no second Hailo model (e.g. a 3DMM or
  6DRepNet head-pose net) is needed. This mirrors the approach validated in
  the looq-prototype sister project: it is fast (microseconds/face) and
  avoids a second inference pass per face.
* "Looking at the ad" is a head-pose offset from the camera axis rather than
  full 3D ad-position geometry: the camera is assumed to sit at/near the ad,
  and the small residual offset (ad slightly to one side, camera height,
  mounting orientation, etc.) is measured empirically with the on-site
  Calibrate action (median yaw/pitch while a person looks straight at the ad)
  instead of being hand-computed. This is the same trick used for the
  looq-prototype OAK-D attention build.
* Storage is SQLite (WAL mode): a `tracks` table with one row per finalized
  visitor (no images, no per-frame positions, no embeddings) and an `hourly`
  rollup table recomputed from it, so raw rows can be pruned on a retention
  schedule while long-term aggregates survive. The CSV download in Settings
  is generated from the `hourly` table on demand.
"""

import csv
import io
import json
import os
import sqlite3
import statistics
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Optional

import cv2
import numpy as np

MEDIA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "media")
ANALYTICS_DIR = os.path.join(MEDIA_DIR, "analytics")
DB_PATH = os.path.join(ANALYTICS_DIR, "analytics.db")
SETTINGS_PATH = os.path.join(ANALYTICS_DIR, "attention_settings.json")

# scrfd_2.5g compiled for hailo8l; ships with the `hailo-all` apt package.
DEFAULT_HEF = "/usr/share/hailo-models/scrfd_2.5g_h8l.hef"
HEF_PATH = os.environ.get("LOOQ_FACE_HEF", DEFAULT_HEF)

FACE_CONFIDENCE = 0.55   # SCRFD score threshold
MIN_FACE_AREA = 0.0015   # normalized bbox area; drops tiny far-away blobs
TARGET_FPS = 10          # capped per the field power/latency budget (8-10 fps
                         # is plenty for dwell resolution and roughly halves
                         # CPU/Hailo duty cycle vs. running flat out)
DEBOUNCE_SECS = 0.2      # looking/not-looking must be stable this long to commit
MIN_ATTENTION_S = 1.0    # accumulated looking time to count a visitor as "viewed"
MIN_TRACK_LIFETIME = 0.5  # tracks shorter than this are detector flicker, not people
RETENTION_DAYS = 7        # raw per-track rows older than this are pruned on start


# --------------------------------------------------------------------------- #
# SCRFD face detector on Hailo (5-point landmarks -> CPU head pose via PnP)
# --------------------------------------------------------------------------- #
@dataclass
class FaceDetection:
    x1: float
    y1: float
    x2: float
    y2: float
    conf: float
    landmarks: Optional[list] = field(default=None)
    """5 x (x, y) normalized coords: [left_eye, right_eye, nose, left_mouth, right_mouth]"""

    @property
    def bbox(self):
        return (self.x1, self.y1, self.x2, self.y2)

    @property
    def area(self):
        return (self.x2 - self.x1) * (self.y2 - self.y1)


def _iou(a, b):
    ix1, iy1 = max(a[0], b[0]), max(a[1], b[1])
    ix2, iy2 = min(a[2], b[2]), min(a[3], b[3])
    iw, ih = max(0.0, ix2 - ix1), max(0.0, iy2 - iy1)
    inter = iw * ih
    if inter <= 0:
        return 0.0
    area_a = (a[2] - a[0]) * (a[3] - a[1])
    area_b = (b[2] - b[0]) * (b[3] - b[1])
    return inter / (area_a + area_b - inter)


class FaceDetector:
    """Wraps scrfd_2.5g_h8l.hef via Hailo's create_infer_model API."""

    _STRIDES = [
        (8, "scrfd_2_5g/conv42", "scrfd_2_5g/conv43", "scrfd_2_5g/conv44"),
        (16, "scrfd_2_5g/conv49", "scrfd_2_5g/conv50", "scrfd_2_5g/conv51"),
        (32, "scrfd_2_5g/conv55", "scrfd_2_5g/conv56", "scrfd_2_5g/conv57"),
    ]

    def __init__(self, hef_path=HEF_PATH, conf_threshold=FACE_CONFIDENCE):
        from hailo_platform import (
            HEF,
            FormatType,
            HailoSchedulingAlgorithm,
            VDevice,
        )

        if not os.path.exists(hef_path):
            raise RuntimeError(f"HEF not found: {hef_path}")

        self.conf_threshold = conf_threshold
        params = VDevice.create_params()
        params.scheduling_algorithm = HailoSchedulingAlgorithm.ROUND_ROBIN
        self._target = VDevice(params)
        self._hef = HEF(hef_path)
        self._infer_model = self._target.create_infer_model(hef_path)
        self._infer_model.set_batch_size(1)
        self._infer_model.input().set_format_type(
            self._hef.get_input_vstream_infos()[0].format.type
        )
        for out in self._infer_model.outputs:
            out.set_format_type(FormatType.FLOAT32)
        self._configured = self._infer_model.configure()
        self._output_names = list(self._infer_model.output_names)
        info = self._hef.get_input_vstream_infos()[0]
        self._input_h, self._input_w = info.shape[0], info.shape[1]

    def close(self):
        try:
            del self._configured
        except Exception:
            pass
        try:
            self._target.release()
        except Exception:
            pass

    def _infer(self, image):
        if image.shape[0] != self._input_h or image.shape[1] != self._input_w:
            image = cv2.resize(image, (self._input_w, self._input_h))
        buffers = {
            name: np.empty(self._infer_model.output(name).shape, dtype=np.float32)
            for name in self._output_names
        }
        bindings = self._configured.create_bindings(output_buffers=buffers)
        bindings.input().set_buffer(np.expand_dims(image, 0))
        self._configured.run([bindings], timeout=5000)
        return buffers

    def detect(self, frame_bgr):
        outputs = self._infer(frame_bgr)
        return _decode_scrfd(
            outputs, self._STRIDES, self.conf_threshold,
            self._input_w, self._input_h,
        )


def _decode_scrfd(outputs, strides, conf_threshold, input_w, input_h,
                  iou_threshold=0.4):
    """Decode raw SCRFD feature maps (score/box/landmark per stride) into
    NMS-filtered FaceDetection objects, including the 5-point landmarks used
    for head-pose PnP.
    """
    all_boxes, all_scores, all_landmarks = [], [], []
    for stride, score_key, box_key, lmk_key in strides:
        score_map = np.squeeze(outputs[score_key])   # (H, W, 2)
        box_map = np.squeeze(outputs[box_key])        # (H, W, 8)
        lmk_map = np.squeeze(outputs[lmk_key])        # (H, W, 20)
        H, W = score_map.shape[:2]
        gy, gx = np.mgrid[0:H, 0:W]
        cx = (gx.astype(np.float32) + 0.5) * stride
        cy = (gy.astype(np.float32) + 0.5) * stride
        for anchor in range(2):
            s = score_map[:, :, anchor]
            mask = s >= conf_threshold
            if not np.any(mask):
                continue
            l = box_map[:, :, anchor * 4 + 0] * stride
            t = box_map[:, :, anchor * 4 + 1] * stride
            r = box_map[:, :, anchor * 4 + 2] * stride
            b = box_map[:, :, anchor * 4 + 3] * stride
            x1 = np.clip((cx - l) / input_w, 0.0, 1.0)
            y1 = np.clip((cy - t) / input_h, 0.0, 1.0)
            x2 = np.clip((cx + r) / input_w, 0.0, 1.0)
            y2 = np.clip((cy + b) / input_h, 0.0, 1.0)

            lmk_off = anchor * 10
            lmk_pts = []
            for k in range(5):
                lx = np.clip((cx + lmk_map[:, :, lmk_off + k * 2 + 0] * stride) / input_w, 0.0, 1.0)
                ly = np.clip((cy + lmk_map[:, :, lmk_off + k * 2 + 1] * stride) / input_h, 0.0, 1.0)
                lmk_pts.append((lx, ly))

            for row, col in zip(*np.where(mask)):
                xs, ys = float(x1[row, col]), float(y1[row, col])
                xe, ye = float(x2[row, col]), float(y2[row, col])
                all_boxes.append([xs, ys, xe - xs, ye - ys])
                all_scores.append(float(s[row, col]))
                all_landmarks.append([
                    (float(lmk_pts[k][0][row, col]), float(lmk_pts[k][1][row, col]))
                    for k in range(5)
                ])

    if not all_boxes:
        return []
    idx = cv2.dnn.NMSBoxes(all_boxes, all_scores, conf_threshold, iou_threshold)
    idx = idx.flatten() if len(idx) > 0 else []
    results = []
    for i in idx:
        x, y, w, h = all_boxes[i]
        results.append(FaceDetection(
            x1=x, y1=y, x2=x + w, y2=y + h,
            conf=all_scores[i], landmarks=all_landmarks[i],
        ))
    return results


# --------------------------------------------------------------------------- #
# Head pose from 5-point landmarks (PnP) -- no extra Hailo model needed
# --------------------------------------------------------------------------- #
# 3D face model matching SCRFD's 5-point landmarks (mm, approximate):
#   [left_eye, right_eye, nose_tip, left_mouth_corner, right_mouth_corner]
_FACE_3D = np.array([
    [-65.0, -70.0, -40.0],
    [65.0, -70.0, -40.0],
    [0.0, 0.0, 0.0],
    [-45.0, 60.0, -40.0],
    [45.0, 60.0, -40.0],
], dtype=np.float64)


def head_pose_pnp(landmarks, frame_w, frame_h):
    """Estimate (yaw, pitch, roll) in degrees from 5 normalized landmarks."""
    pts_2d = np.array(
        [(lx * frame_w, ly * frame_h) for lx, ly in landmarks], dtype=np.float64,
    )
    focal = float(frame_w)
    cam_mx = np.array([[focal, 0, frame_w / 2],
                        [0, focal, frame_h / 2],
                        [0, 0, 1]], dtype=np.float64)
    dist = np.zeros((4, 1), dtype=np.float64)

    ok, rvec, _ = cv2.solvePnP(
        _FACE_3D, pts_2d, cam_mx, dist, flags=cv2.SOLVEPNP_EPNP
    )
    if not ok:
        return 0.0, 0.0, 0.0

    rmat, _ = cv2.Rodrigues(rvec)
    sy = np.sqrt(rmat[0, 0] ** 2 + rmat[1, 0] ** 2)
    if sy > 1e-6:
        pitch = np.degrees(np.arctan2(rmat[2, 1], rmat[2, 2]))
        yaw = np.degrees(np.arctan2(-rmat[2, 0], sy))
        roll = np.degrees(np.arctan2(rmat[1, 0], rmat[0, 0]))
    else:
        pitch = np.degrees(np.arctan2(-rmat[1, 2], rmat[1, 1]))
        yaw = np.degrees(np.arctan2(-rmat[2, 0], sy))
        roll = 0.0
    return float(yaw), float(pitch), float(roll)


class LookState:
    """Commits looking/not-looking only after DEBOUNCE_SECS of stable readings.

    This is the noise-suppression layer: a single flickery frame (pose noise,
    a missed detection) never toggles the committed state.
    """

    __slots__ = ("committed", "tentative", "since")

    def __init__(self):
        self.committed = False
        self.tentative = False
        self.since = 0.0

    def update(self, looking, now):
        if looking != self.tentative:
            self.tentative = looking
            self.since = now
        elif now - self.since >= DEBOUNCE_SECS:
            self.committed = self.tentative
        return self.committed


def is_looking_at_ad(yaw, pitch, settings):
    """True when the head pose points at the ad rather than at the camera.

    The camera is assumed to sit at/near the ad; `settings.yaw_offset` /
    `pitch_offset` are the small residual angles measured by Calibrate.
    """
    return (abs(yaw - settings.yaw_offset) < settings.yaw_tol and
            abs(pitch - settings.pitch_offset) < settings.pitch_tol)


@dataclass
class AttentionSettings:
    yaw_offset: float = 0.0
    pitch_offset: float = 0.0
    yaw_tol: float = 20.0
    pitch_tol: float = 15.0

    @classmethod
    def load(cls, path=SETTINGS_PATH):
        if not os.path.isfile(path):
            return cls()
        try:
            with open(path) as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            return cls()
        known = {k: data[k] for k in cls().__dict__ if k in data}
        return cls(**known)

    def save(self, path=SETTINGS_PATH):
        try:
            with open(path, "w") as f:
                json.dump(asdict(self), f, indent=2)
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# IOU tracker -- stable integer IDs across frames, carries landmarks through
# --------------------------------------------------------------------------- #
class _Tracker:
    _IOU_MIN = 0.25
    _MAX_LOST = 15   # ~1.5s at TARGET_FPS before a track is finalized

    def __init__(self):
        self._tracks = {}
        self._next_id = 0

    def update(self, detections):
        prev_ids = set(self._tracks)
        used = set()
        updated = {}
        for tid, track in self._tracks.items():
            best_i, best_score = -1, self._IOU_MIN
            for i, det in enumerate(detections):
                if i in used:
                    continue
                score = _iou(track["bbox"], det.bbox)
                if score > best_score:
                    best_i, best_score = i, score
            if best_i >= 0:
                d = detections[best_i]
                updated[tid] = {"bbox": d.bbox, "landmarks": d.landmarks, "lost": 0}
                used.add(best_i)
            elif track["lost"] < self._MAX_LOST:
                updated[tid] = {**track, "lost": track["lost"] + 1, "landmarks": None}
        for i, det in enumerate(detections):
            if i not in used:
                updated[self._next_id] = {"bbox": det.bbox, "landmarks": det.landmarks, "lost": 0}
                self._next_id += 1
        self._tracks = updated
        dropped = prev_ids - set(updated)
        active = [
            (tid, *t["bbox"], t["landmarks"])
            for tid, t in updated.items() if t["lost"] == 0
        ]
        return active, dropped

    def clear(self):
        self._tracks.clear()
        self._next_id = 0


# --------------------------------------------------------------------------- #
# SQLite storage: per-track rows + hourly rollup (no images, no embeddings,
# no per-frame positions -- see module docstring)
# --------------------------------------------------------------------------- #
def _db():
    conn = sqlite3.connect(DB_PATH, timeout=5.0)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _init_db():
    os.makedirs(ANALYTICS_DIR, exist_ok=True)
    with _db() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS tracks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_start REAL NOT NULL,
            ts_end REAL NOT NULL,
            presence_s REAL NOT NULL,
            attention_s REAL NOT NULL,
            viewed INTEGER NOT NULL,
            max_streak_s REAL NOT NULL
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS hourly (
            hour TEXT PRIMARY KEY,
            passersby INTEGER NOT NULL,
            viewers INTEGER NOT NULL,
            view_rate REAL,
            mean_attention_s REAL,
            p90_attention_s REAL
        )""")
        conn.execute(
            "DELETE FROM tracks WHERE ts_end < ?",
            (time.time() - RETENTION_DAYS * 86400,),
        )


def export_csv(day=None):
    """CSV text of the hourly rollup for a local date (YYYY-MM-DD), default today."""
    day = day or datetime.now().strftime("%Y-%m-%d")
    with _db() as conn:
        rows = conn.execute(
            "SELECT hour, passersby, viewers, view_rate, mean_attention_s, p90_attention_s "
            "FROM hourly WHERE hour LIKE ? ORDER BY hour",
            (day + "%",),
        ).fetchall()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["hour", "passersby", "viewers", "view_rate", "mean_attention_s", "p90_attention_s"])
    w.writerows(rows)
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# Analytics engine
# --------------------------------------------------------------------------- #
@dataclass
class _State:
    enabled: bool = False
    available: bool = False
    error: Optional[str] = None
    live_count: int = 0
    looking_now: int = 0
    unique_total: int = 0
    viewed_total: int = 0
    avg_dwell: float = 0.0
    avg_attention: float = 0.0
    fps: float = 0.0
    boxes: list = field(default_factory=list)  # [{id,x,y,w,h,looking}] normalized
    calibrating: bool = False
    calib_remaining: float = 0.0


class AnalyticsEngine:
    def __init__(self, camera, auto_start=True):
        self._camera = camera
        self._lock = threading.Lock()
        self._state = _State()
        self._detector = None
        self._tracker = _Tracker()
        self._settings = AttentionSettings.load()

        # per-track bookkeeping (worker thread only)
        self._first_seen = {}      # tid -> ts first seen (this stint)
        self._look_state = {}      # tid -> LookState
        self._pose_cache = {}      # tid -> (yaw, pitch)
        self._looking_ids = set()
        self._look_since = {}      # tid -> ts current look streak started
        self._look_accum = {}      # tid -> accumulated looking seconds (closed streaks)
        self._max_streak = {}      # tid -> longest continuous look streak seen
        self._all_ids = set()      # every tid ever created this session
        self._viewed_ids = set()   # tids whose attention ever reached MIN_ATTENTION_S

        self._calib_pending = None
        self._calib_until = 0.0
        self._calib_samples = []

        self._stop = threading.Event()
        self._want = auto_start
        _init_db()
        self._thread = threading.Thread(target=self._run, daemon=True, name="analytics")
        self._thread.start()

    # -- public API ------------------------------------------------------- #
    def snapshot(self):
        with self._lock:
            s = self._state
            return {
                "enabled": s.enabled,
                "available": s.available,
                "error": s.error,
                "live_count": s.live_count,
                "looking_now": s.looking_now,
                "unique_total": s.unique_total,
                "viewed_total": s.viewed_total,
                "avg_dwell": round(s.avg_dwell, 1),
                "avg_attention": round(s.avg_attention, 1),
                "fps": round(s.fps, 1),
                "boxes": list(s.boxes),
                "calibrating": s.calibrating,
                "calib_remaining": round(s.calib_remaining, 1),
                "yaw_offset": self._settings.yaw_offset,
                "pitch_offset": self._settings.pitch_offset,
                "yaw_tol": self._settings.yaw_tol,
                "pitch_tol": self._settings.pitch_tol,
            }

    def set_enabled(self, enabled):
        self._want = bool(enabled)
        if not enabled:
            with self._lock:
                self._state.enabled = False
                self._state.boxes = []
                self._state.live_count = 0
                self._state.looking_now = 0
        return self.snapshot()

    def reset_session(self):
        with self._lock:
            self._tracker.clear()
            self._first_seen.clear()
            self._look_state.clear()
            self._pose_cache.clear()
            self._looking_ids.clear()
            self._look_since.clear()
            self._look_accum.clear()
            self._max_streak.clear()
            self._all_ids.clear()
            self._viewed_ids.clear()
            self._state.unique_total = 0
            self._state.viewed_total = 0
            self._state.avg_dwell = 0.0
            self._state.avg_attention = 0.0
            self._state.live_count = 0
            self._state.looking_now = 0
            self._state.boxes = []
        return self.snapshot()

    def calibrate(self, seconds=5.0):
        """Start an on-site calibration window: point every tracked face at
        the ad for `seconds`, and the median yaw/pitch of the largest face
        becomes the new looking-at-ad offset."""
        self._calib_pending = float(seconds)
        return self.snapshot()

    def update_attention_settings(self, data):
        for key in ("yaw_offset", "pitch_offset", "yaw_tol", "pitch_tol"):
            if key in data:
                try:
                    setattr(self._settings, key, float(data[key]))
                except (TypeError, ValueError):
                    pass
        self._settings.save()
        return self.snapshot()

    # -- worker ----------------------------------------------------------- #
    def _ensure_detector(self):
        if self._detector is not None:
            return True
        try:
            self._detector = FaceDetector()
            with self._lock:
                self._state.available = True
                self._state.error = None
            return True
        except Exception as exc:
            with self._lock:
                self._state.available = False
                self._state.error = str(exc)
                self._state.enabled = False
            self._want = False
            return False

    def _read_frame(self):
        out = self._camera.streaming_output
        with out.condition:
            out.condition.wait(timeout=1.0)
            jpeg = out.frame
        if jpeg is None:
            return None
        arr = np.frombuffer(jpeg, dtype=np.uint8)
        return cv2.imdecode(arr, cv2.IMREAD_COLOR)

    def _run(self):
        period = 1.0 / TARGET_FPS
        while not self._stop.is_set():
            if not self._want or not self._camera.available:
                with self._lock:
                    if self._state.enabled and not self._want:
                        self._state.enabled = False
                time.sleep(0.2)
                continue
            if not self._ensure_detector():
                time.sleep(1.0)
                continue

            t0 = time.time()
            frame = self._read_frame()
            if frame is None:
                continue
            try:
                dets = [d for d in self._detector.detect(frame)
                        if d.area >= MIN_FACE_AREA]
            except Exception as exc:
                with self._lock:
                    self._state.error = str(exc)
                time.sleep(0.5)
                continue

            active, dropped = self._tracker.update(dets)
            now = time.time()
            h, w = frame.shape[:2]

            for tid in dropped:
                self._finalize_track(tid, now)

            boxes = []
            for tid, x1, y1, x2, y2, landmarks in active:
                is_new = tid not in self._first_seen
                self._first_seen.setdefault(tid, now)
                if is_new:
                    self._all_ids.add(tid)
                if landmarks:
                    yaw, pitch, _roll = head_pose_pnp(landmarks, w, h)
                    self._pose_cache[tid] = (yaw, pitch)
                pose = self._pose_cache.get(tid)
                looking = pose is not None and is_looking_at_ad(pose[0], pose[1], self._settings)

                state = self._look_state.setdefault(tid, LookState())
                committed = state.update(looking, now)
                if committed:
                    self._looking_ids.add(tid)
                    self._look_since.setdefault(tid, now)
                else:
                    self._looking_ids.discard(tid)
                    since = self._look_since.pop(tid, None)
                    if since is not None:
                        streak = now - since
                        self._look_accum[tid] = self._look_accum.get(tid, 0.0) + streak
                        self._max_streak[tid] = max(self._max_streak.get(tid, 0.0), streak)

                if self._live_attention(tid, now) >= MIN_ATTENTION_S:
                    self._viewed_ids.add(tid)

                boxes.append({
                    "id": tid,
                    "x": round(x1, 4), "y": round(y1, 4),
                    "w": round(x2 - x1, 4), "h": round(y2 - y1, 4),
                    "looking": committed,
                })

            self._update_calibration(active, now)

            presences = [now - ts for ts in self._first_seen.values()]
            avg_dwell = sum(presences) / len(presences) if presences else 0.0
            viewed_now_attn = [
                self._live_attention(tid, now) for tid in self._viewed_ids
                if tid in self._first_seen
            ]
            avg_attention = (sum(viewed_now_attn) / len(viewed_now_attn)
                             if viewed_now_attn else 0.0)

            dt = time.time() - t0
            with self._lock:
                self._state.enabled = True
                self._state.live_count = len(active)
                self._state.looking_now = len(self._looking_ids)
                self._state.unique_total = len(self._all_ids)
                self._state.viewed_total = len(self._viewed_ids)
                self._state.avg_dwell = avg_dwell
                self._state.avg_attention = avg_attention
                self._state.boxes = boxes
                self._state.fps = (1.0 / dt) if dt > 0 else 0.0
                self._state.calibrating = now < self._calib_until
                self._state.calib_remaining = max(0.0, self._calib_until - now)

            sleep = period - (time.time() - t0)
            if sleep > 0:
                time.sleep(sleep)

    def _live_attention(self, tid, now):
        total = self._look_accum.get(tid, 0.0)
        since = self._look_since.get(tid)
        if since is not None:
            total += now - since
        return total

    def _finalize_track(self, tid, now):
        first = self._first_seen.pop(tid, now)
        presence = now - first
        since = self._look_since.pop(tid, None)
        attention = self._look_accum.pop(tid, 0.0)
        if since is not None:
            streak = now - since
            attention += streak
            self._max_streak[tid] = max(self._max_streak.get(tid, 0.0), streak)
        max_streak = self._max_streak.pop(tid, attention)
        self._look_state.pop(tid, None)
        self._pose_cache.pop(tid, None)
        self._looking_ids.discard(tid)

        if presence < MIN_TRACK_LIFETIME:
            return  # detector flicker, not a real passerby

        if attention >= MIN_ATTENTION_S:
            self._viewed_ids.add(tid)
        viewed = attention >= MIN_ATTENTION_S

        try:
            with _db() as conn:
                conn.execute(
                    "INSERT INTO tracks (ts_start, ts_end, presence_s, attention_s, "
                    "viewed, max_streak_s) VALUES (?, ?, ?, ?, ?, ?)",
                    (first, now, presence, attention, int(viewed), max_streak),
                )
            self._update_hourly(now)
        except sqlite3.Error:
            pass

    def _update_hourly(self, ts):
        hour = datetime.fromtimestamp(ts).strftime("%Y-%m-%dT%H")
        start = datetime.strptime(hour, "%Y-%m-%dT%H").timestamp()
        end = start + 3600
        with _db() as conn:
            rows = conn.execute(
                "SELECT attention_s, viewed FROM tracks WHERE ts_end >= ? AND ts_end < ?",
                (start, end),
            ).fetchall()
            passersby = len(rows)
            viewers = sum(1 for _, v in rows if v)
            attentions = sorted(a for a, v in rows if v)
            mean_att = sum(attentions) / len(attentions) if attentions else 0.0
            p90 = attentions[int(0.9 * (len(attentions) - 1))] if attentions else 0.0
            view_rate = viewers / passersby if passersby else 0.0
            conn.execute(
                "INSERT INTO hourly (hour, passersby, viewers, view_rate, "
                "mean_attention_s, p90_attention_s) VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(hour) DO UPDATE SET passersby=excluded.passersby, "
                "viewers=excluded.viewers, view_rate=excluded.view_rate, "
                "mean_attention_s=excluded.mean_attention_s, "
                "p90_attention_s=excluded.p90_attention_s",
                (hour, passersby, viewers, view_rate, mean_att, p90),
            )

    def _update_calibration(self, active, now):
        if self._calib_pending is not None:
            self._calib_until = now + self._calib_pending
            self._calib_samples = []
            self._calib_pending = None

        if now < self._calib_until:
            big_area, big_pose = 0.0, None
            for tid, x1, y1, x2, y2, _lmk in active:
                area = (x2 - x1) * (y2 - y1)
                pose = self._pose_cache.get(tid)
                if pose is not None and area > big_area:
                    big_area, big_pose = area, pose
            if big_pose is not None:
                self._calib_samples.append(big_pose)
        elif self._calib_samples:
            yaw = statistics.median(s[0] for s in self._calib_samples)
            pitch = statistics.median(s[1] for s in self._calib_samples)
            self._settings.yaw_offset = round(yaw, 1)
            self._settings.pitch_offset = round(pitch, 1)
            self._settings.save()
            self._calib_samples = []
            self._calib_until = 0.0
