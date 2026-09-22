"""
RDP Status API
--------------
No external database - state is kept in an in-memory dict, keyed by vm_name.
This resets on every redeploy / process restart (Render free tier restarts
are rare in practice as long as something pings it regularly, which your
Power Automate flow does every minute).

Endpoints:
  POST /status              - client machines report their RDP status
  GET  /status              - full current snapshot of all VMs
  GET  /status/changes      - only VMs whose status changed since ?since=<iso ts>
  GET  /health              - simple liveness check (also usable as keep-alive)
"""

import os
import threading
from datetime import datetime, timezone

from flask import Flask, request, jsonify

app = Flask(__name__)

# ============================================================
# CONFIG
# ============================================================

API_KEY = os.environ.get("API_KEY", "CHANGE_ME_TO_A_LONG_RANDOM_STRING")

# ============================================================
# STATE
# ============================================================
# Structure:
# {
#   "<vm_name>": {
#       "vm_name": str,
#       "computer_name": str,
#       "status": "connected" | "disconnected",
#       "last_updated": iso8601 str (UTC),   # every ping, changed or not
#       "last_changed": iso8601 str (UTC),   # only when status flips
#   },
#   ...
# }
state_lock = threading.Lock()
vm_state = {}


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def parse_iso(ts: str):
    """Parses an ISO8601 timestamp, tolerant of 'Z' suffix. Returns None if invalid."""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


# ============================================================
# AUTH
# ============================================================

def check_api_key():
    key = request.headers.get("X-API-Key")
    return key == API_KEY


@app.before_request
def enforce_api_key():
    # /health stays open so it's easy to use as an uptime/keep-alive check
    if request.path == "/health":
        return None
    if not check_api_key():
        return jsonify({"error": "unauthorized"}), 401


# ============================================================
# ROUTES
# ============================================================

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "time": now_iso()}), 200


@app.route("/status", methods=["POST"])
def post_status():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "invalid or missing JSON body"}), 400

    vm_name = data.get("vm_name")
    computer_name = data.get("computer_name")
    status = data.get("status")

    if not vm_name or not status:
        return jsonify({"error": "vm_name and status are required"}), 400

    status = str(status).lower()
    if status not in ("connected", "disconnected"):
        return jsonify({"error": "status must be 'connected' or 'disconnected'"}), 400

    received_at = now_iso()

    with state_lock:
        existing = vm_state.get(vm_name)
        status_changed = existing is None or existing.get("status") != status

        entry = {
            "vm_name": vm_name,
            "computer_name": computer_name,
            "status": status,
            "last_updated": received_at,
            "last_changed": received_at if status_changed else existing.get("last_changed", received_at),
            # "notified" is False whenever there's a status change Power Automate
            # hasn't posted to Teams yet. Heartbeats (no change) don't touch it.
            "notified": False if status_changed else existing.get("notified", True),
        }
        vm_state[vm_name] = entry

    return jsonify({"ok": True, "status_changed": status_changed, "entry": entry}), 200


@app.route("/status", methods=["GET"])
def get_status():
    with state_lock:
        snapshot = list(vm_state.values())
    return jsonify({"count": len(snapshot), "vms": snapshot}), 200


@app.route("/status/changes", methods=["GET"])
def get_status_changes():
    """Returns VMs whose status change hasn't been acknowledged yet
    (see POST /status/ack). This is what Power Automate should poll -
    no need to track a 'since' cursor between runs."""
    with state_lock:
        snapshot = list(vm_state.values())

    changed = [entry for entry in snapshot if not entry.get("notified", True)]

    return jsonify({
        "count": len(changed),
        "checked_at": now_iso(),
        "vms": changed,
    }), 200


@app.route("/status/ack", methods=["POST"])
def ack_status_changes():
    """Marks the given VMs' current status as acknowledged/notified, so they
    stop showing up in /status/changes. Call this right after successfully
    posting to Teams.

    Body: {"vm_names": ["VM01", "VM02"]}  or  {"ack_all": true}
    """
    data = request.get_json(silent=True) or {}
    ack_all = data.get("ack_all", False)
    vm_names = data.get("vm_names", [])

    acked = []
    with state_lock:
        if ack_all:
            for name, entry in vm_state.items():
                if not entry.get("notified", True):
                    entry["notified"] = True
                    acked.append(name)
        else:
            for name in vm_names:
                entry = vm_state.get(name)
                if entry is not None and not entry.get("notified", True):
                    entry["notified"] = True
                    acked.append(name)

    return jsonify({"ok": True, "acknowledged": acked}), 200


@app.route("/status/<vm_name>", methods=["GET"])
def get_single_status(vm_name):
    with state_lock:
        entry = vm_state.get(vm_name)
    if entry is None:
        return jsonify({"error": f"no data for vm_name '{vm_name}'"}), 404
    return jsonify(entry), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)