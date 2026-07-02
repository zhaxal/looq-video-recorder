"""WiFi hotspot (access point) control for the Looq Video Recorder.

Toggles the NetworkManager connection created by scripts/setup_hotspot.sh so the
web UI can switch the Pi's own access point on and off.  NetworkManager changes
need root, so commands are run via `sudo -n nmcli`; the installer gives the
service user passwordless sudo.

If the hotspot connection has never been configured (setup_hotspot.sh not run)
the manager reports configured=False and the UI hides the toggle.
"""

import subprocess

# Must match CON in scripts/setup_hotspot.sh.
HOTSPOT_CON = "looq-hotspot"


def _nmcli(*args, sudo=False):
    cmd = (["sudo", "-n"] if sudo else []) + ["nmcli", *args]
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=20
    )


def _connections():
    """Return the set of NetworkManager connection names, or None on error."""
    res = _nmcli("-t", "-f", "NAME", "con", "show")
    if res.returncode != 0:
        return None
    return {line for line in res.stdout.splitlines() if line}


def _active_connections():
    res = _nmcli("-t", "-f", "NAME", "con", "show", "--active")
    if res.returncode != 0:
        return set()
    return {line for line in res.stdout.splitlines() if line}


def status():
    """Report whether the hotspot is configured and currently up."""
    cons = _connections()
    if cons is None:
        return {"configured": False, "active": False, "error": "nmcli unavailable"}
    configured = HOTSPOT_CON in cons
    active = HOTSPOT_CON in _active_connections() if configured else False
    return {"configured": configured, "active": active, "error": None}


def set_enabled(enabled):
    """Bring the hotspot up or down.  Returns the resulting status() dict.

    Raises RuntimeError with nmcli's message on failure.
    """
    cons = _connections()
    if cons is None or HOTSPOT_CON not in (cons or set()):
        raise RuntimeError(
            "Hotspot not configured. Run scripts/setup_hotspot.sh first."
        )
    action = "up" if enabled else "down"
    res = _nmcli("con", action, HOTSPOT_CON, sudo=True)
    if res.returncode != 0:
        msg = (res.stderr or res.stdout or "nmcli failed").strip()
        raise RuntimeError(msg)
    return status()
