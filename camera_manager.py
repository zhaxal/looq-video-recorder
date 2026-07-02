"""Camera management for the Looq Video Recorder.

Wraps Picamera2 to provide:
  * a continuous low-res MJPEG preview stream (for the browser),
  * full/high-res photo capture,
  * H.264 video recording muxed to MP4 (via ffmpeg),
  * runtime adjustable settings (resolution, fps, autofocus, rotation, ...).

The preview encoder runs on the camera's `lores` stream so that the `main`
stream stays free for high quality recording and stills at the same time.

If no camera is attached the manager stays in an "offline" state instead of
crashing, so the web UI still loads and can report the problem.
"""

import concurrent.futures
import io
import os
import threading
import time
from datetime import datetime

MEDIA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "media")
PHOTO_DIR = os.path.join(MEDIA_DIR, "photos")
VIDEO_DIR = os.path.join(MEDIA_DIR, "videos")

# Resolution presets exposed in the UI.  Values are (width, height).
RESOLUTIONS = {
    "480p": (854, 480),
    "720p": (1280, 720),
    "1080p": (1920, 1080),
}

# Preview is always low-res to keep streaming smooth over WiFi.
PREVIEW_SIZE = (1280, 720)

DEFAULT_SETTINGS = {
    "resolution": "1080p",   # recording / main-stream size
    "fps": 30,
    "autofocus": "continuous",  # continuous | auto | manual
    "lens_position": 1.0,       # used when autofocus == manual (dioptres)
    "rotation": 0,              # 0 | 90 | 180 | 270
    "hflip": False,
    "vflip": False,
}


class StreamingOutput(io.BufferedIOBase):
    """Thread-safe holder for the latest MJPEG frame."""

    def __init__(self):
        self.frame = None
        self.condition = threading.Condition()

    def write(self, buf):
        with self.condition:
            self.frame = buf
            self.condition.notify_all()


class CameraManager:
    def __init__(self):
        self.lock = threading.Lock()
        self.picam2 = None
        self.available = False
        self.error = None

        # picamera2's FfmpegOutput launches ffmpeg with a parent-death signal
        # (PR_SET_PDEATHSIG) which, on Linux, fires when the *thread* that
        # spawned it exits -- not the whole process.  Flask's threaded server
        # handles each request on a short-lived worker thread, so starting the
        # recorder directly from a request thread gets ffmpeg killed the instant
        # the response is sent (no video file is ever written).  Run all encoder
        # start/stop calls on this single, long-lived worker thread instead so
        # ffmpeg's parent stays alive for the life of the app.
        self._worker = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="cam-worker"
        )

        self.streaming_output = StreamingOutput()
        self.preview_encoder = None
        self.video_encoder = None

        self.recording = False
        self.record_path = None
        self.record_start = None

        self.settings = dict(DEFAULT_SETTINGS)

        os.makedirs(PHOTO_DIR, exist_ok=True)
        os.makedirs(VIDEO_DIR, exist_ok=True)

        self._init_camera()

    def _on_worker(self, fn, *args, **kwargs):
        """Run a camera call on the long-lived worker thread and wait for it.

        See the comment in __init__ for why ffmpeg-backed recording must not be
        started from a transient Flask request thread.
        """
        return self._worker.submit(fn, *args, **kwargs).result()

    # ------------------------------------------------------------------ #
    # Setup / teardown
    # ------------------------------------------------------------------ #
    def _transform(self):
        from libcamera import Transform

        s = self.settings
        hflip, vflip = s["hflip"], s["vflip"]
        # 180 rotation == hflip + vflip on the sensor.  90/270 are not
        # supported by the sensor transform, so they are applied as a 180
        # here and the browser rotates the rest via CSS.
        if s["rotation"] == 180:
            hflip = not hflip
            vflip = not vflip
        return Transform(hflip=hflip, vflip=vflip)

    def _build_config(self):
        record_size = RESOLUTIONS[self.settings["resolution"]]
        return self.picam2.create_video_configuration(
            main={"size": record_size, "format": "RGB888"},
            lores={"size": PREVIEW_SIZE, "format": "YUV420"},
            controls={"FrameRate": float(self.settings["fps"])},
            transform=self._transform(),
        )

    def _apply_focus_controls(self):
        try:
            from libcamera import controls

            mode = self.settings["autofocus"]
            if mode == "continuous":
                self.picam2.set_controls(
                    {"AfMode": controls.AfModeEnum.Continuous}
                )
            elif mode == "auto":
                self.picam2.set_controls({"AfMode": controls.AfModeEnum.Auto})
                self.picam2.autofocus_cycle()
            elif mode == "manual":
                self.picam2.set_controls(
                    {
                        "AfMode": controls.AfModeEnum.Manual,
                        "LensPosition": float(self.settings["lens_position"]),
                    }
                )
        except Exception as exc:  # camera may not support autofocus
            print(f"[camera] focus controls not applied: {exc}")

    def _init_camera(self):
        try:
            from picamera2 import Picamera2

            cameras = Picamera2.global_camera_info()
            if not cameras:
                raise RuntimeError(
                    "No camera detected. Check that the ribbon cable is fully "
                    "seated (contacts toward the correct side) and reboot."
                )

            self.picam2 = Picamera2()
            self.picam2.configure(self._build_config())
            self.picam2.start()
            self._apply_focus_controls()

            # Start the always-on preview encoder on the lores stream.
            self._start_preview_encoder()

            self.available = True
            self.error = None
            print("[camera] initialised OK")
        except Exception as exc:
            self.available = False
            self.error = str(exc)
            print(f"[camera] init failed: {exc}")

    def _start_preview_encoder(self):
        from picamera2.encoders import MJPEGEncoder
        from picamera2.outputs import FileOutput

        self.preview_encoder = MJPEGEncoder()
        self.picam2.start_encoder(
            self.preview_encoder,
            FileOutput(self.streaming_output),
            name="lores",
        )

    def _stop_preview_encoder(self):
        if self.preview_encoder is not None:
            try:
                self.picam2.stop_encoder(self.preview_encoder)
            except TypeError:
                self.picam2.stop_encoder()
            self.preview_encoder = None

    def reinitialise(self):
        """Attempt to (re)start the camera, e.g. after fixing the cable."""
        with self.lock:
            self._teardown()
            self._init_camera()
            return self.available

    def _teardown(self):
        try:
            if self.recording:
                self._stop_recording_locked()
            if self.picam2 is not None:
                try:
                    self.picam2.stop_encoder()
                except Exception:
                    pass
                try:
                    self.picam2.stop()
                except Exception:
                    pass
                try:
                    self.picam2.close()
                except Exception:
                    pass
        finally:
            self.picam2 = None
            self.preview_encoder = None
            self.video_encoder = None
            self.recording = False
            self.available = False

    # ------------------------------------------------------------------ #
    # Preview
    # ------------------------------------------------------------------ #
    def frames(self):
        """Generator yielding multipart MJPEG chunks for the browser."""
        boundary = b"--frame"
        while True:
            if not self.available:
                time.sleep(0.5)
                continue
            with self.streaming_output.condition:
                self.streaming_output.condition.wait(timeout=1.0)
                frame = self.streaming_output.frame
            if frame is None:
                continue
            yield (
                boundary
                + b"\r\nContent-Type: image/jpeg\r\nContent-Length: "
                + str(len(frame)).encode()
                + b"\r\n\r\n"
                + frame
                + b"\r\n"
            )

    # ------------------------------------------------------------------ #
    # Photo
    # ------------------------------------------------------------------ #
    def capture_photo(self):
        with self.lock:
            if not self.available:
                raise RuntimeError("Camera offline")

            name = datetime.now().strftime("photo_%Y%m%d_%H%M%S.jpg")
            path = os.path.join(PHOTO_DIR, name)

            if self.recording:
                # Can't switch modes mid-recording; grab from the main stream.
                self.picam2.capture_file(path, name="main")
            else:
                # Switch to full-sensor still mode for a high-res photo. The
                # preview encoder must be stopped around the mode switch, then
                # restarted so the live feed resumes.
                self._stop_preview_encoder()
                try:
                    still = self.picam2.create_still_configuration(
                        transform=self._transform()
                    )
                    self.picam2.switch_mode_and_capture_file(still, path)
                finally:
                    self._start_preview_encoder()
            return name

    # ------------------------------------------------------------------ #
    # Video
    # ------------------------------------------------------------------ #
    def start_recording(self):
        with self.lock:
            if not self.available:
                raise RuntimeError("Camera offline")
            if self.recording:
                return self._status_locked()

            from picamera2.encoders import H264Encoder
            from picamera2.outputs import FfmpegOutput

            name = datetime.now().strftime("video_%Y%m%d_%H%M%S.mp4")
            path = os.path.join(VIDEO_DIR, name)

            self.video_encoder = H264Encoder()
            # FfmpegOutput muxes the H.264 stream straight into a .mp4.  Start
            # it on the persistent worker thread so ffmpeg isn't killed when the
            # request thread exits (see __init__).
            self._on_worker(
                self.picam2.start_encoder,
                self.video_encoder,
                FfmpegOutput(path),
                name="main",
            )
            self.recording = True
            self.record_path = path
            self.record_start = time.time()
            return self._status_locked()

    def stop_recording(self):
        with self.lock:
            return self._stop_recording_locked()

    def _stop_recording_locked(self):
        if not self.recording:
            return self._status_locked()
        try:
            self._on_worker(self.picam2.stop_encoder, self.video_encoder)
        except TypeError:
            # Older picamera2 stop_encoder takes no argument.
            self._on_worker(self.picam2.stop_encoder)
        self.recording = False
        self.video_encoder = None
        self.record_start = None
        result = self._status_locked()
        self.record_path = None
        return result

    # ------------------------------------------------------------------ #
    # Settings
    # ------------------------------------------------------------------ #
    def update_settings(self, new):
        with self.lock:
            if self.recording:
                raise RuntimeError("Stop recording before changing settings")

            needs_reconfig = False
            for key in ("resolution", "fps", "rotation", "hflip", "vflip"):
                if key in new and new[key] != self.settings[key]:
                    needs_reconfig = True

            self.settings.update(
                {k: v for k, v in new.items() if k in DEFAULT_SETTINGS}
            )

            if not self.available:
                return self.settings

            if needs_reconfig:
                self.picam2.stop_encoder()
                self.preview_encoder = None
                self.picam2.stop()
                self.picam2.configure(self._build_config())
                self.picam2.start()
                self._start_preview_encoder()
            self._apply_focus_controls()
            return self.settings

    # ------------------------------------------------------------------ #
    # Status
    # ------------------------------------------------------------------ #
    def status(self):
        with self.lock:
            return self._status_locked()

    def _status_locked(self):
        elapsed = 0
        if self.recording and self.record_start:
            elapsed = int(time.time() - self.record_start)
        return {
            "available": self.available,
            "error": self.error,
            "recording": self.recording,
            "elapsed": elapsed,
            "settings": self.settings,
        }
