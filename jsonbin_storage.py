"""
jsonbin_storage.py - Persists small JSON blobs (like the paper wallet
state) to JSONBin.io instead of local disk. Needed because Render's free
tier wipes local files on every redeploy/restart.

Verified against JSONBin's own official API docs (v3):
  Create: POST https://api.jsonbin.io/v3/b
  Read:   GET  https://api.jsonbin.io/v3/b/<id>/latest
  Update: PUT  https://api.jsonbin.io/v3/b/<id>
  Auth header: X-Master-Key: <your key>
"""

import requests

BASE_URL = "https://api.jsonbin.io/v3/b"


def create_bin(data: dict, api_key: str, name: str = "sol-bot-state") -> str:
    """Creates a new bin, returns its ID, or None on failure (bad key,
    network issue, etc.) - callers should handle None gracefully rather
    than assuming this always succeeds."""
    try:
        resp = requests.post(
            BASE_URL,
            json=data,
            headers={"Content-Type": "application/json", "X-Master-Key": api_key, "X-Bin-Name": name},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()["metadata"]["id"]
    except requests.exceptions.RequestException as e:
        print(f"⚠️  JSONBin create_bin failed: {e}")
        return None


def read_bin(bin_id: str, api_key: str) -> dict:
    """Returns the bin's current data, or {} if it can't be read."""
    try:
        resp = requests.get(
            f"{BASE_URL}/{bin_id}/latest",
            headers={"X-Master-Key": api_key},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json().get("record", {})
    except requests.exceptions.RequestException:
        return {}


def update_bin(bin_id: str, data: dict, api_key: str) -> bool:
    """Overwrites the bin's data. Returns True on success."""
    try:
        resp = requests.put(
            f"{BASE_URL}/{bin_id}",
            json=data,
            headers={"Content-Type": "application/json", "X-Master-Key": api_key},
            timeout=10,
        )
        resp.raise_for_status()
        return True
    except requests.exceptions.RequestException:
        return False
