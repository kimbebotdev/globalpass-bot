import json
from datetime import datetime
from typing import Any


def make_run_id() -> str:
    return datetime.utcnow().strftime("%Y%m%d_%H%M%S")


def build_route_string(input_data: dict[str, Any]) -> str:
    trips = input_data.get("trips", [])
    if not trips:
        return "N/A"
    if len(trips) == 1:
        trip = trips[0]
        return f"{trip.get('origin', '?')} -> {trip.get('destination', '?')}"
    return " | ".join(f"{trip.get('origin', '?')} -> {trip.get('destination', '?')}" for trip in trips)


def extract_json_from_text(text: str) -> Any:
    try:
        return json.loads(text)
    except Exception:
        pass
    start = text.find("[")
    end = text.rfind("]")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except Exception:
            pass
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except Exception:
            pass
    return None


def normalize_google_time(time_str: str | None) -> str | None:
    if not time_str:
        return None
    try:
        cleaned = time_str.replace("\u202f", " ").strip()
        return datetime.strptime(cleaned, "%I:%M %p").strftime("%H:%M")
    except (ValueError, TypeError):
        return None


def to_minutes(duration_str: str | None) -> int:
    if not duration_str:
        return 1440
    try:
        clean = (
            duration_str.lower()
            .replace("hr", "h")
            .replace("min", "m")
            .replace(" ", "")
        )
        h = int(clean.split("h")[0]) if "h" in clean else 0
        m_part = clean.split("h")[-1] if "h" in clean else clean
        m = int(m_part.replace("m", "")) if "m" in m_part else 0
        return h * 60 + m
    except (ValueError, TypeError):
        return 1440
