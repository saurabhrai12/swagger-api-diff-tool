"""
Stores and retrieves API response snapshots for offline analysis.
Avoids hitting live APIs repeatedly.
"""

import json
import os
import hashlib
from datetime import datetime
from typing import Dict, Any, Optional


def _snapshot_key(endpoint_key: str, version: str) -> str:
    """Generate a filesystem-safe key for a snapshot."""
    raw = f"{version}_{endpoint_key}"
    safe = raw.replace("/", "_").replace(" ", "_").replace("{", "").replace("}", "")
    return safe


def save_snapshot(
    snapshots_dir: str,
    endpoint_key: str,
    version: str,
    response_data: Dict[str, Any],
    sample_used: Dict[str, Any] = None,
) -> str:
    """Save a response snapshot to disk."""
    os.makedirs(snapshots_dir, exist_ok=True)
    key = _snapshot_key(endpoint_key, version)
    filepath = os.path.join(snapshots_dir, f"{key}.json")

    snapshot = {
        "endpoint": endpoint_key,
        "version": version,
        "captured_at": datetime.now().isoformat(),
        "sample_input": sample_used,
        "response": response_data,
    }

    with open(filepath, "w") as f:
        json.dump(snapshot, f, indent=2, default=str)

    return filepath


def load_snapshot(
    snapshots_dir: str,
    endpoint_key: str,
    version: str,
) -> Optional[Dict[str, Any]]:
    """Load a previously saved snapshot."""
    key = _snapshot_key(endpoint_key, version)
    filepath = os.path.join(snapshots_dir, f"{key}.json")

    if not os.path.exists(filepath):
        return None

    with open(filepath, "r") as f:
        return json.load(f)


def list_snapshots(snapshots_dir: str) -> list:
    """List all saved snapshots."""
    if not os.path.exists(snapshots_dir):
        return []
    snapshots = []
    for fname in sorted(os.listdir(snapshots_dir)):
        if fname.endswith(".json"):
            fpath = os.path.join(snapshots_dir, fname)
            try:
                with open(fpath, "r") as f:
                    data = json.load(f)
                snapshots.append({
                    "file": fname,
                    "endpoint": data.get("endpoint"),
                    "version": data.get("version"),
                    "captured_at": data.get("captured_at"),
                })
            except Exception:
                pass
    return snapshots
