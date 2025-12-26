import json
from pathlib import Path
from typing import Any, Dict

import pandas as pd

BOT_OUTPUTS = {
    "myidtravel": "myidtravel_flightschedule.json",
    "google_flights": "google_flights_results.json",
    "stafftraveler": "stafftraveler_results.json",
}


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def consolidate(run_id: str, run_dir: Path) -> Dict[str, Any]:
    bots: Dict[str, Any] = {}
    for bot, filename in BOT_OUTPUTS.items():
        bot_file = run_dir / filename
        if bot_file.exists():
            bots[bot] = _load_json(bot_file)

    return {
        "run_id": run_id,
        "run_directory": str(run_dir),
        "bots": bots,
    }


def write_json(consolidated: Dict[str, Any], path: Path) -> None:
    path.write_text(json.dumps(consolidated, indent=2))


def _as_dataframe(payload: Any):
    if isinstance(payload, list):
        if payload and isinstance(payload[0], dict):
            return pd.json_normalize(payload)
        return pd.DataFrame({"values": payload})
    if isinstance(payload, dict):
        return pd.json_normalize(payload)
    return pd.DataFrame({"values": [payload]})


def write_excel(consolidated: Dict[str, Any], path: Path) -> None:
    with pd.ExcelWriter(path) as writer:
        bots = consolidated.get("bots") or {}
        if not bots:
            pd.DataFrame([{"message": "No bot output available"}]).to_excel(writer, index=False, sheet_name="summary")
            return

        for bot, payload in bots.items():
            df = _as_dataframe(payload)
            df.to_excel(writer, index=False, sheet_name=bot[:31] or "data")
