"""Pushes finalized attention tracks to the Ad Studio backend over the 4G
uplink, so the "real" leaderboard/stats can attribute them to whichever ad
was on screen at the time (Ad Studio does that join -- this module just
ships the raw per-visitor rows).

Off by default: set LOOQ_AD_STUDIO_API_BASE (and LOOQ_DEVICE_ID, which must
match the same device id configured on the Ad Studio / xixun side) to enable.
Safe to retry: each row is sent with a stable track_id so a resend after a
dropped connection can't double-count a visitor server-side.
"""

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request

from analytics import _db

log = logging.getLogger("looq.attention_sync")

DEVICE_ID = os.environ.get("LOOQ_DEVICE_ID", "").strip()
API_BASE = os.environ.get("LOOQ_AD_STUDIO_API_BASE", "").strip().rstrip("/")
SYNC_INTERVAL_S = float(os.environ.get("LOOQ_SYNC_INTERVAL_S", "15"))
BATCH_SIZE = 200
REQUEST_TIMEOUT_S = 10


class AttentionSync:
    def __init__(self):
        self.enabled = bool(API_BASE and DEVICE_ID)
        self._lock = threading.Lock()
        self._last_success = None
        self._last_error = None
        self._thread = None

    def start(self):
        if not self.enabled:
            log.info("attention sync disabled (set LOOQ_AD_STUDIO_API_BASE + LOOQ_DEVICE_ID to enable)")
            return
        self._thread = threading.Thread(target=self._run, daemon=True, name="attention-sync")
        self._thread.start()

    def status(self):
        with self._lock:
            pending = self._pending_count() if self.enabled else 0
            return {
                "enabled": self.enabled,
                "device_id": DEVICE_ID or None,
                "pending": pending,
                "last_success": self._last_success,
                "last_error": self._last_error,
            }

    def _pending_count(self):
        with _db() as conn:
            row = conn.execute("SELECT COUNT(*) FROM tracks WHERE synced = 0").fetchone()
            return row[0]

    def _run(self):
        while True:
            try:
                self._sync_once()
            except Exception as exc:  # never let a bad batch kill the thread
                log.exception("attention sync failed")
                with self._lock:
                    self._last_error = str(exc)
            time.sleep(SYNC_INTERVAL_S)

    def _sync_once(self):
        with _db() as conn:
            rows = conn.execute(
                "SELECT id, ts_start, ts_end, viewed, attention_s FROM tracks "
                "WHERE synced = 0 ORDER BY id ASC LIMIT ?",
                (BATCH_SIZE,),
            ).fetchall()
        if not rows:
            return

        events = [
            {
                "track_id": f"{DEVICE_ID}-{row[0]}",
                "started_at": row[1],
                "ended_at": row[2],
                "viewed": bool(row[3]),
                "attention_seconds": row[4],
            }
            for row in rows
        ]
        self._post(events)

        # Ad Studio's ingest is idempotent on track_id (INSERT OR IGNORE), so
        # marking the whole accepted batch synced is safe even on a retry.
        ids = [row[0] for row in rows]
        with _db() as conn:
            conn.executemany("UPDATE tracks SET synced = 1 WHERE id = ?", [(i,) for i in ids])

        with self._lock:
            self._last_success = time.time()
            self._last_error = None
        log.info("synced %d attention event(s)", len(events))

    def _post(self, events):
        url = f"{API_BASE}/api/device/{DEVICE_ID}/attention"
        body = json.dumps({"events": events}).encode("utf-8")
        req = urllib.request.Request(
            url, data=body, method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_S) as resp:
                resp.read()
        except (urllib.error.URLError, TimeoutError) as exc:
            raise RuntimeError(f"POST {url} failed: {exc}") from exc


attention_sync = AttentionSync()
