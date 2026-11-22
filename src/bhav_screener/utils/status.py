import json
from datetime import datetime
from pathlib import Path

from bhav_screener.utils.config import Settings, load_settings


def write_status(layer: str, status: str, settings: Settings | None = None, **details) -> Path:
    """Write data/status/<layer>_status.json (overwritten each run) for the next step to read.

    `status` is "ok", "empty" (ran fine, nothing found) or "failed". Extra keyword arguments are
    stored as-is (values that are not JSON types are converted to strings).
    """
    folder = (settings or load_settings()).data.status_path
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{layer}_status.json"
    payload = {
        "layer": layer,
        "status": status,
        "run_at": datetime.now().isoformat(timespec="seconds"),
        **details,
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)
    return path
